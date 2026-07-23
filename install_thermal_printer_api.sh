#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/aysua-thermal-printer-api"
SERVICE_FILE="/etc/systemd/system/aysua-thermal-printer-api.service"

echo "[1/5] Installing Linux packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y bluez python3 poppler-utils
fi

echo "[2/5] Copying service files"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp "${ROOT_DIR}/aysua_thermal_printer_api.py" "${INSTALL_DIR}/"
sudo chmod +x "${INSTALL_DIR}/aysua_thermal_printer_api.py"

if [ ! -f "${INSTALL_DIR}/config.json" ]; then
  sudo tee "${INSTALL_DIR}/config.json" >/dev/null <<'JSON'
{
  "enabled": false,
  "printer_name": "PT-210",
  "mac_address": "",
  "pin": "0000",
  "device_path": "/dev/rfcomm0",
  "rfcomm_channel": 1,
  "paper_width": "58mm",
  "chars_per_line": 32,
  "codepage": "cp857",
  "turkish_ascii": true,
  "copies": 1,
  "saved_scans_dir": "/home/pmroot/AysuaSpect/files/saved_scans",
  "receipt_title": "Yakut Dedektörü",
  "print_qr": true,
  "signature_space": true
}
JSON
fi

echo "[3/5] Installing systemd service"
sudo cp "${ROOT_DIR}/aysua-thermal-printer-api.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload

echo "[4/5] Enabling service"
sudo systemctl enable aysua-thermal-printer-api.service
sudo systemctl restart aysua-thermal-printer-api.service

echo "[5/5] Status"
sudo systemctl --no-pager status aysua-thermal-printer-api.service || true
echo
echo "Test:"
echo "  curl http://127.0.0.1:8096/api/thermal/status"
