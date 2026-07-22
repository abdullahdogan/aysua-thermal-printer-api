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


HOST = os.getenv("THERMAL_API_HOST", "0.0.0.0")
PORT = int(os.getenv("THERMAL_API_PORT", "8096"))
CONFIG_PATH = os.getenv(
    "THERMAL_CONFIG_PATH",
    "/opt/aysua-thermal-printer-api/config.json",
)

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
    "copies": 1,
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
        if key == "enabled":
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
            "agent KeyboardDisplay",
            "default-agent",
            f"remove {mac}",
            f"pair {mac}",
        ]:
            write_line(command)
            time.sleep(0.35)
            read_available()

        while time.time() - start < timeout:
            text = read_available(0.35)
            all_text = clean_output("\n".join(output))
            lowered = all_text.lower()

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


def escpos_bytes_from_text(text, config):
    encoding = config.get("codepage") or "cp857"
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    lines = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if len(line) <= chars:
            lines.append(line)
        else:
            while line:
                lines.append(line[:chars])
                line = line[chars:]
    body = "\n".join(lines).encode(encoding, errors="replace")
    init = b"\x1b\x40"
    align_left = b"\x1b\x61\x00"
    feed_cut = b"\n\n\n\x1d\x56\x00"
    return init + align_left + body + feed_cut


def center(text, width):
    text = str(text or "")
    if len(text) >= width:
        return text[:width]
    pad = max(0, (width - len(text)) // 2)
    return (" " * pad) + text


def build_report_text(payload, config):
    chars = max(24, min(48, int(config.get("chars_per_line") or 32)))
    sep = "-" * chars
    files = payload.get("files")
    if not isinstance(files, list):
        files = [payload.get("file")] if payload.get("file") else []
    title = payload.get("title") or "AYSUA SPECT"
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


def write_to_printer(text, config):
    prepare_printer(config)
    data = escpos_bytes_from_text(text, config)
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
    server_version = "AysuaThermalPrinterAPI/1.0"

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
                    center("AYSUA SPECT", chars),
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
                write_to_printer(text, config)
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
