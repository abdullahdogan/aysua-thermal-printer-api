#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aysua ESC/POS Bluetooth thermal printer API.

Designed for Raspberry Pi OS / Linux kiosk devices. The service does not poll
Bluetooth continuously; it prepares the configured printer only when status,
reconnect, test_print, or print_report is requested.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import select
import tempfile
import urllib.request


HOST = os.getenv("THERMAL_API_HOST", "0.0.0.0")
PORT = int(os.getenv("THERMAL_API_PORT", "8096"))
CONFIG_PATH = os.getenv(
    "THERMAL_CONFIG_PATH",
    "/opt/aysua-thermal-printer-api/config.json",
)
API_VERSION = "1.4.2"

DEFAULT_CONFIG = {
    "enabled": False,
    "printer_name": "PT-210",
    "mac_address": "",
    "pin": "0000",
    "device_path": "/dev/rfcomm0",
    "rfcomm_channel": 1,
    "paper_width": "58mm",
    "chars_per_line": 32,
    "codepage": "cp857",
    "turkish_ascii": True,
    "copies": 1,
    "saved_scans_dir": "/home/pmroot/AysuaSpect/files/saved_scans",
    "receipt_title": "Yakut Dedektörü",
    "print_qr": True,
    "qr_mode": "text",
    "qr_max_chars": 300,
    "qr_render": "native",
    "qr_image_pixels": 192,
    "signature_space": True,
}


class ThermalError(Exception):
    pass


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean_output(text):
    return ANSI_RE.sub("", text or "").replace("\r", "").strip()


def run_cmd(args, timeout=15, check=False):
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=check,
        )
        return proc.returncode, clean_output(proc.stdout), clean_output(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        raise ThermalError(f"Command timed out: {' '.join(args)}") from exc
    except FileNotFoundError as exc:
        raise ThermalError(f"Command not found: {args[0]}") from exc


def normalize_mac(mac):
    raw = (mac or "").strip().upper()
    if not raw:
        return ""
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", raw):
        raise ThermalError("Invalid Bluetooth MAC address")
    return raw


def ensure_config_dir():
    directory = os.path.dirname(CONFIG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            config.update(saved)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    config["mac_address"] = (config.get("mac_address") or "").strip().upper()
    config["pin"] = str(config.get("pin") or "0000")
    config["device_path"] = config.get("device_path") or "/dev/rfcomm0"
    config["chars_per_line"] = int(config.get("chars_per_line") or 32)
    config["rfcomm_channel"] = int(config.get("rfcomm_channel") or 1)
    config["copies"] = max(1, int(config.get("copies") or 1))
    config["turkish_ascii"] = bool(config.get("turkish_ascii", True))
    config["qr_image_pixels"] = max(120, min(320, int(config.get("qr_image_pixels") or 192)))
    return config


def save_config(updates):
    config = load_config()
    allowed = set(DEFAULT_CONFIG.keys())
    for key, value in (updates or {}).items():
        if key not in allowed:
            continue
        if key == "mac_address":
            value = normalize_mac(value)
        if key == "pin":
            value = str(value or "0000").strip()[:16]
        if key in {"chars_per_line", "rfcomm_channel", "copies"}:
            value = int(value)
        if key == "qr_max_chars":
            value = max(120, min(2500, int(value)))
        if key == "qr_image_pixels":
            value = max(120, min(320, int(value)))
        if key in {"enabled", "turkish_ascii", "print_qr", "signature_space"}:
            value = bool(value)
        config[key] = value
    ensure_config_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    return config


def bluetoothctl_script(commands, timeout=30):
    if shutil.which("bluetoothctl") is None:
        raise ThermalError("bluetoothctl not installed")
    script = "\n".join(commands) + "\n"
    proc = subprocess.run(
        ["bluetoothctl"],
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, clean_output(proc.stdout), clean_output(proc.stderr)


def bluetoothctl_expect_pair(mac, pin="0000", timeout=55):
    """Pair through bluetoothctl using a pseudo-terminal.

    Some BlueZ/bluetoothctl builds do not handle agent prompts correctly when
    stdin is a plain pipe. A PTY keeps bluetoothctl in interactive mode so
    PIN/passkey prompts can be answered.
    """
    if os.name != "posix":
        raise ThermalError("Interactive Bluetooth pairing is only supported on Linux")
    if shutil.which("bluetoothctl") is None:
        raise ThermalError("bluetoothctl not installed")

    import pty

    mac = normalize_mac(mac)
    pin = str(pin or "0000").strip()
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        text=False,
    )
    os.close(slave_fd)

    output = []
    sent_pin = False
    paired = False
    failed_reason = ""
    pair_attempts = 0
    start = time.time()

    def write_line(line):
        os.write(master_fd, (line + "\n").encode("utf-8", errors="replace"))
        output.append(f"> {line}")

    def read_available(wait=0.2):
        chunk_text = ""
        while True:
            ready, _, _ = select.select([master_fd], [], [], wait)
            if not ready:
                break
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            decoded = chunk.decode("utf-8", errors="replace")
            output.append(decoded)
            chunk_text += decoded
            wait = 0
        return clean_output(chunk_text)

    try:
        time.sleep(0.4)
        read_available()

        for command in [
            "power on",
            "scan on",
            "agent KeyboardDisplay",
            "default-agent",
        ]:
            write_line(command)
            time.sleep(0.35)
            read_available()

        time.sleep(2.0)
        read_available()
        write_line(f"pair {mac}")
        pair_attempts += 1

        while time.time() - start < timeout:
            text = read_available(0.35)
            all_text = clean_output("\n".join(output))
            lowered = all_text.lower()

            if "not available" in lowered and pair_attempts < 4:
                failed_reason = ""
                time.sleep(2.0)
                read_available()
                write_line(f"pair {mac}")
                pair_attempts += 1
                continue

            if not sent_pin and (
                "enter pin" in lowered
                or "pin code" in lowered
                or "passkey" in lowered
                or "request pin" in lowered
            ):
                write_line(pin)
                sent_pin = True
                continue

            if "confirm passkey" in lowered or "authorize service" in lowered:
                write_line("yes")
                continue

            if (
                "pairing successful" in lowered
                or "alreadyexists" in lowered
                or "already exists" in lowered
            ):
                paired = True
                break

            failed_match = re.search(r"failed to pair:[^\n]+", all_text, flags=re.IGNORECASE)
            if failed_match:
                failed_reason = failed_match.group(0)
                if "not available" in failed_reason.lower() and pair_attempts < 4:
                    time.sleep(2.0)
                    read_available()
                    write_line(f"pair {mac}")
                    pair_attempts += 1
                    continue
                break

            if proc.poll() is not None:
                break

            if text:
                continue

        write_line(f"trust {mac}")
        time.sleep(0.25)
        read_available()
        write_line(f"connect {mac}")
        time.sleep(2.0)
        read_available()
        write_line("scan off")
        time.sleep(0.2)
        read_available()
        write_line("quit")
        time.sleep(0.2)
        read_available()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    final_output = clean_output("\n".join(output))
    info = bluetooth_info(mac)
    if info.get("paired"):
        paired = True
    if not paired:
        if "Failed to register agent object" in final_output:
            raise ThermalError(
                "Bluetooth agent could not be registered. "
                "Restart bluetooth service and try again: sudo systemctl restart bluetooth"
            )
        if "not available" in final_output.lower():
            raise ThermalError(
                "Bluetooth printer is not available for pairing. "
                "Keep the printer on and in pairing mode, then scan devices and try Pair again."
            )
        raise ThermalError(failed_reason or final_output or "Bluetooth pairing failed")
    return {"ok": True, "message": "Paired", "info": info, "raw": final_output}


def list_devices(scan_seconds=7):
    if shutil.which("bluetoothctl") is None:
        raise ThermalError("bluetoothctl not installed")
    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.stdin.write("power on\nscan on\n")
        proc.stdin.flush()
        time.sleep(max(2, min(15, int(scan_seconds))))
        proc.stdin.write("devices\nscan off\nquit\n")
        proc.stdin.flush()
        out, err = proc.communicate(timeout=8)
        code = proc.returncode
    except Exception:
        proc.kill()
        raise
    if code != 0:
        raise ThermalError(clean_output(err or out) or "Bluetooth device list failed")
    devices = []
    for line in out.splitlines():
        match = re.match(r"Device\s+(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(.+)$", line.strip())
        if match:
            devices.append({"mac_address": match.group(1).upper(), "name": match.group(3).strip()})
    return devices


def bluetooth_info(mac):
    mac = normalize_mac(mac)
    if not mac:
        return {"paired": False, "trusted": False, "connected": False, "raw": ""}
    code, out, err = bluetoothctl_script([f"info {mac}"], timeout=10)
    text = out or err or ""
    return {
        "paired": "Paired: yes" in text,
        "trusted": "Trusted: yes" in text,
        "connected": "Connected: yes" in text,
        "raw": text,
    }


def pair_and_trust(mac, pin="0000"):
    mac = normalize_mac(mac)
    return bluetoothctl_expect_pair(mac, pin, timeout=55)


def release_rfcomm(device_path):
    if shutil.which("rfcomm") is None:
        return
    match = re.search(r"rfcomm(\d+)$", device_path or "")
    if not match:
        return
    run_cmd(["rfcomm", "release", match.group(1)], timeout=8)


def bind_rfcomm(mac, device_path="/dev/rfcomm0", channel=1):
    mac = normalize_mac(mac)
    if not mac:
        raise ThermalError("No Bluetooth printer selected")
    if os.path.exists(device_path):
        return
    if shutil.which("rfcomm") is None:
        raise ThermalError("rfcomm not installed")
    match = re.search(r"rfcomm(\d+)$", device_path or "")
    rfcomm_number = match.group(1) if match else "0"
    release_rfcomm(device_path)
    code, out, err = run_cmd(["rfcomm", "bind", rfcomm_number, mac, str(channel)], timeout=15)
    if code != 0:
        raise ThermalError(err or out or "rfcomm bind failed")
    time.sleep(0.5)
    if not os.path.exists(device_path):
        raise ThermalError(f"{device_path} was not created")


def prepare_printer(config=None):
    config = config or load_config()
    if not config.get("enabled"):
        raise ThermalError("Thermal printer is disabled")
    mac = normalize_mac(config.get("mac_address"))
    if not mac:
        raise ThermalError("No Bluetooth printer selected")

    info = bluetooth_info(mac)
    if not info["paired"]:
        pair_and_trust(mac, config.get("pin") or "0000")

    bluetoothctl_script(["power on", f"trust {mac}", f"connect {mac}", "quit"], timeout=20)
    bind_rfcomm(mac, config.get("device_path"), config.get("rfcomm_channel", 1))
    return True


TURKISH_ASCII_MAP = str.maketrans({
    "ç": "c", "Ç": "C",
    "ğ": "g", "Ğ": "G",
    "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O",
    "ş": "s", "Ş": "S",
    "ü": "u", "Ü": "U",
    "â": "a", "Â": "A",
    "î": "i", "Î": "I",
    "û": "u", "Û": "U",
    "°": " derece",
    "–": "-", "—": "-", "−": "-",
    "“": "\"", "”": "\"", "’": "'", "‘": "'",
    "…": "...",
})


def normalize_for_thermal_text(text, config):
    value = str(text or "")
    if config.get("turkish_ascii", True):
        value = value.translate(TURKISH_ASCII_MAP)
    return value


def escpos_text_body_bytes(text, config):
    encoding = config.get("codepage") or "cp857"
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    lines = []
    normalized_text = normalize_for_thermal_text(text, config)
    for raw in (normalized_text or "").splitlines():
        line = raw.rstrip()
        if len(line) <= chars:
            lines.append(line)
        else:
            while line:
                lines.append(line[:chars])
                line = line[chars:]
    return "\n".join(lines).encode(encoding, errors="replace")


def escpos_qr_bytes(qr_data, size=5):
    value = str(qr_data or "").strip()
    if not value:
        return b""
    data = value.encode("utf-8", errors="replace")
    store_len = len(data) + 3
    p_l = store_len % 256
    p_h = store_len // 256
    size = max(3, min(8, int(size or 5)))
    return b"".join([
        b"\n",
        b"\x1b\x61\x01",                              # center
        b"\x1d\x28\x6b\x04\x00\x31\x41\x32\x00",      # QR model 2
        b"\x1d\x28\x6b\x03\x00\x31\x43" + bytes([size]),
        b"\x1d\x28\x6b\x03\x00\x31\x45\x31",          # error correction M
        b"\x1d\x28\x6b" + bytes([p_l, p_h]) + b"\x31\x50\x30" + data,
        b"\x1d\x28\x6b\x03\x00\x31\x51\x30",          # print QR
        b"\n",
        b"\x1b\x61\x00",                              # left
    ])


def escpos_qr_image_bytes(qr_data, config):
    value = str(qr_data or "").strip()
    if not value:
        return b""
    try:
        import qrcode
        from PIL import Image
    except Exception:
        return escpos_qr_bytes(value, config.get("qr_size", 5))

    target = max(120, min(320, int(config.get("qr_image_pixels") or 192)))
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    paper_pixels = 384 if chars <= 32 else 576

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(value)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("1")
    img = img.resize((target, target), Image.Resampling.NEAREST)

    width = img.width
    height = img.height
    left_pad = max(0, (paper_pixels - width) // 2)
    total_width = width + left_pad
    width_bytes = (total_width + 7) // 8
    raster = bytearray(width_bytes * height)
    px = img.load()

    for y in range(height):
        for x in range(width):
            if px[x, y] == 0:
                tx = x + left_pad
                idx = y * width_bytes + (tx // 8)
                raster[idx] |= 0x80 >> (tx % 8)

    x_l = width_bytes % 256
    x_h = width_bytes // 256
    y_l = height % 256
    y_h = height // 256
    return b"".join([
        b"\n",
        b"\x1b\x61\x00",
        b"\x1d\x76\x30\x00" + bytes([x_l, x_h, y_l, y_h]) + bytes(raster),
        b"\n",
    ])


def escpos_bytes_from_text(text, config, qr_data="", footer_text=""):
    init = b"\x1b\x40"
    align_left = b"\x1b\x61\x00"
    body = escpos_text_body_bytes(text, config)
    footer = escpos_text_body_bytes(footer_text, config) if footer_text else b""
    feed_cut = b"\n\n\n\x1d\x56\x00"
    parts = [init, align_left, body]
    if qr_data:
        if str(config.get("qr_render") or "image").strip().lower() == "native":
            parts.append(escpos_qr_bytes(qr_data, config.get("qr_size", 5)))
        else:
            parts.append(escpos_qr_image_bytes(qr_data, config))
    if footer:
        parts.extend([b"\n", align_left, footer])
    parts.append(feed_cut)
    return b"".join(parts)


def center(text, width):
    text = str(text or "")
    if len(text) >= width:
        return text[:width]
    pad = max(0, (width - len(text)) // 2)
    return (" " * pad) + text


def safe_filename(name):
    raw = os.path.basename(str(name or "").replace("\\", "/"))
    if not raw or raw in {".", ".."}:
        return ""
    return raw


def extract_pdf_text_with_pdftotext(pdf_path):
    if shutil.which("pdftotext") is None:
        return ""
    code, out, err = run_cmd(["pdftotext", "-layout", pdf_path, "-"], timeout=20)
    if code != 0:
        return ""
    return out


def read_local_pdf_text(file_name, config):
    name = safe_filename(file_name)
    if not name:
        return ""
    base_dir = str(config.get("saved_scans_dir") or "").strip()
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, name))
    candidates.extend([
        os.path.join("/home/pmroot/AysuaSpect/files/saved_scans", name),
        os.path.join("/home/pmroot/AysuaSpect/web/files/saved_scans", name),
        os.path.join("/opt/AysuaSpect/files/saved_scans", name),
        os.path.join("/opt/aysua/files/saved_scans", name),
    ])
    for path in candidates:
        if path and os.path.exists(path) and os.path.isfile(path):
            text = extract_pdf_text_with_pdftotext(path)
            if text:
                return text
    return ""


def download_pdf_text(url):
    if not url:
        return ""
    parsed = str(url).strip()
    if not parsed.startswith(("http://", "https://")):
        return ""
    with tempfile.NamedTemporaryFile(prefix="aysua-report-", suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        req = urllib.request.Request(parsed, headers={"User-Agent": "AysuaThermalPrinterAPI/1.2"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read(8 * 1024 * 1024)
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        return extract_pdf_text_with_pdftotext(tmp_path)
    except Exception:
        return ""
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def clean_pdf_text_for_receipt(text):
    lines = []
    seen_blank = False
    for raw in (text or "").replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            if not seen_blank and lines:
                lines.append("")
            seen_blank = True
            continue
        seen_blank = False
        # Graph axis tick dumps can make receipts noisy. Keep report text compact.
        if re.fullmatch(r"[-+0-9., ]{12,}", line):
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def extract_report_text_from_payload(payload, config):
    explicit = payload.get("pdf_text")
    if explicit:
        return clean_pdf_text_for_receipt(str(explicit))

    for url in payload.get("pdf_urls") or []:
        text = download_pdf_text(url)
        if text:
            return clean_pdf_text_for_receipt(text)

    files = payload.get("files")
    if not isinstance(files, list):
        files = [payload.get("file")] if payload.get("file") else []
    for file_name in files:
        text = read_local_pdf_text(file_name, config)
        if text:
            return clean_pdf_text_for_receipt(text)
    return ""


def receipt_title(payload, config):
    return config.get("receipt_title") or payload.get("title") or "Yakut Dedektörü"


def qr_data_from_payload(payload):
    explicit = str(payload.get("qr_data") or "").strip()
    if explicit:
        return explicit
    for url in payload.get("pdf_urls") or []:
        value = str(url or "").strip()
        if value and "127.0.0.1" not in value and "localhost" not in value:
            return value
    for url in payload.get("pdf_urls") or []:
        value = str(url or "").strip()
        if value:
            return value
    files = payload.get("files")
    if not isinstance(files, list):
        files = [payload.get("file")] if payload.get("file") else []
    for file_name in files:
        value = str(file_name or "").strip()
        if value:
            return value
    return ""


def compact_qr_text(receipt_text, config, payload):
    title = normalize_for_thermal_text(receipt_title(payload, config), config)
    wanted = [
        "scan id", "scan result", "sonuc", "sonuç", "scan mode", "mod",
        "user", "kullanici", "kullanıcı", "unit", "date", "tarih", "time", "saat"
    ]
    lines = []
    for raw in clean_pdf_text_for_receipt(receipt_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        norm = line.lower()
        if any(key in norm for key in wanted):
            lines.append(line)
        if len(lines) >= 8:
            break

    if not lines:
        fallback = []
        if payload.get("date"):
            fallback.append(f"Tarih: {payload.get('date')}")
        if payload.get("user"):
            fallback.append(f"Kullanici: {payload.get('user')}")
        if payload.get("mode"):
            fallback.append(f"Mod: {payload.get('mode')}")
        if payload.get("result"):
            fallback.append(f"Sonuc: {payload.get('result')}")
        lines = fallback

    text = "\n".join([title] + lines).strip()
    return normalize_for_thermal_text(text, config)


def qr_data_for_receipt(payload, config, receipt_text):
    if not config.get("print_qr", True):
        return ""
    mode = str(config.get("qr_mode") or "text").strip().lower()
    if mode == "link":
        return qr_data_from_payload(payload)

    text = compact_qr_text(receipt_text, config, payload)
    max_chars = max(120, min(2500, int(config.get("qr_max_chars") or 900)))
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def signature_footer(config):
    if not config.get("signature_space", True):
        return ""
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    label = "Personel Imzasi:"
    return "\n".join([
        "-" * chars,
        label,
        "",
        "",
        "_" * min(chars, 24),
    ])


def build_report_text(payload, config):
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    sep = "-" * chars
    pdf_text = extract_report_text_from_payload(payload, config)
    if pdf_text:
        lines = [center(receipt_title(payload, config), chars), sep]
        lines.extend(pdf_text.splitlines())
        lines.append(sep)
        return "\n".join(lines)

    files = payload.get("files")
    if not isinstance(files, list):
        files = [payload.get("file")] if payload.get("file") else []
    title = receipt_title(payload, config)
    user = payload.get("user") or ""
    mode = payload.get("mode") or ""
    result = payload.get("result") or ""
    date = payload.get("date") or time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [center(title, chars), sep, f"Tarih: {date}"]
    if user:
        lines.append(f"Kullanici: {user}")
    if mode:
        lines.append(f"Mod: {mode}")
    if result:
        lines.extend([sep, "Sonuc:", str(result)])
    if files:
        lines.extend([sep, "Rapor:"])
        lines.extend([str(item) for item in files if item])
    extra_lines = payload.get("lines")
    if isinstance(extra_lines, list) and extra_lines:
        lines.extend([sep])
        lines.extend([str(item) for item in extra_lines])
    lines.append(sep)
    return "\n".join(lines)


def write_to_printer(text, config, qr_data="", footer_text=""):
    prepare_printer(config)
    data = escpos_bytes_from_text(text, config, qr_data=qr_data, footer_text=footer_text)
    path = config.get("device_path") or "/dev/rfcomm0"
    try:
        with open(path, "wb", buffering=0) as fh:
            for _ in range(max(1, int(config.get("copies") or 1))):
                fh.write(data)
                fh.flush()
    except OSError as exc:
        release_rfcomm(path)
        bind_rfcomm(config.get("mac_address"), path, config.get("rfcomm_channel", 1))
        with open(path, "wb", buffering=0) as fh:
            fh.write(data)


def status_payload():
    config = load_config()
    info = bluetooth_info(config.get("mac_address")) if config.get("mac_address") else {
        "paired": False,
        "trusted": False,
        "connected": False,
        "raw": "",
    }
    device_path = config.get("device_path") or "/dev/rfcomm0"
    return {
        "ok": True,
        "version": API_VERSION,
        "enabled": bool(config.get("enabled")),
        "configured": bool(config.get("mac_address")),
        "printer_name": config.get("printer_name"),
        "mac_address": config.get("mac_address"),
        "device_path": device_path,
        "rfcomm_ready": os.path.exists(device_path),
        "paired": info.get("paired", False),
        "trusted": info.get("trusted", False),
        "connected": info.get("connected", False),
    }


def parse_json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ThermalError("Invalid JSON body") from exc


class Handler(BaseHTTPRequestHandler):
    server_version = f"AysuaThermalPrinterAPI/{API_VERSION}"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, status, payload, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/thermal/status":
                self._send(200, status_payload())
            elif path == "/api/thermal/version":
                self._send(200, {"ok": True, "version": API_VERSION})
            elif path == "/api/thermal/settings":
                config = load_config()
                self._send(200, {"ok": True, "settings": config})
            elif path == "/api/thermal/devices":
                self._send(200, {"ok": True, "devices": list_devices()})
            else:
                self._send(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = parse_json_body(self)
            if path == "/api/thermal/settings":
                config = save_config(payload)
                self._send(200, {"ok": True, "settings": config})
            elif path == "/api/thermal/pair":
                updates = {}
                if payload.get("mac_address"):
                    updates["mac_address"] = payload.get("mac_address")
                if payload.get("pin"):
                    updates["pin"] = payload.get("pin")
                if updates:
                    save_config(updates)
                config = load_config()
                result = pair_and_trust(config.get("mac_address"), config.get("pin"))
                self._send(200, result)
            elif path == "/api/thermal/reconnect":
                prepare_printer(load_config())
                self._send(200, {"ok": True, "message": "Connected", "status": status_payload()})
            elif path == "/api/thermal/test_print":
                config = load_config()
                chars = int(config.get("chars_per_line") or 32)
                text = "\n".join([
                    center(config.get("receipt_title") or "Yakut Dedektörü", chars),
                    "-" * chars,
                    "PT-210 ESC/POS test",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    "-" * chars,
                ])
                write_to_printer(text, config)
                self._send(200, {"ok": True, "message": "Test print sent"})
            elif path == "/api/thermal/print_report":
                config = load_config()
                text = build_report_text(payload, config)
                qr_data = qr_data_for_receipt(payload, config, text)
                footer = signature_footer(config)
                write_to_printer(text, config, qr_data=qr_data, footer_text=footer)
                self._send(200, {"ok": True, "message": "Report sent"})
            else:
                self._send(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})


def main():
    ensure_config_dir()
    if not os.path.exists(CONFIG_PATH):
        save_config({})
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Aysua Thermal Printer API: http://{HOST}:{PORT}/api/thermal/status", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
