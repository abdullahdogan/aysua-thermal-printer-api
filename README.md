# Aysua Thermal Printer API

PT-210 gibi Bluetooth üzerinden ESC/POS destekleyen termal yazıcıları Aysua kiosk yazılımına eklemek için bağımsız Linux servisidir.

Servis battery API gibi ayrıca kurulur ve varsayılan olarak `8096` portunda çalışır.

```bash
git clone https://github.com/abdullahdogan/aysua-thermal-printer-api.git
cd aysua-thermal-printer-api
chmod +x install_thermal_printer_api.sh
sudo bash install_thermal_printer_api.sh
```

## Temel davranış

- Sürekli Bluetooth durum kontrolü yapmaz.
- Baskıdan önce kayıtlı yazıcıyı hazırlar.
- Gerekirse eşleştirir, bağlanır ve `rfcomm` portunu oluşturur.
- PIN varsayılanı `0000` değeridir; çoğu PT-210 cihazda `0000` veya `1234` kullanılır.

## Endpoint'ler

```text
GET  /api/thermal/status
GET  /api/thermal/settings
POST /api/thermal/settings
GET  /api/thermal/devices
POST /api/thermal/pair
POST /api/thermal/reconnect
POST /api/thermal/test_print
POST /api/thermal/print_report
```

## Ayar dosyası

Kurulumdan sonra:

```text
/opt/aysua-thermal-printer-api/config.json
```

Örnek:

```json
{
  "enabled": true,
  "printer_name": "PT-210",
  "mac_address": "XX:XX:XX:XX:XX:XX",
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
  "qr_mode": "text",
  "qr_max_chars": 900,
  "qr_render": "image",
  "qr_image_pixels": 192,
  "signature_space": true
}
```

## Manuel test

```bash
curl http://127.0.0.1:8096/api/thermal/status
curl http://127.0.0.1:8096/api/thermal/devices
curl -X POST http://127.0.0.1:8096/api/thermal/settings \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"mac_address":"XX:XX:XX:XX:XX:XX","pin":"0000"}'
curl -X POST http://127.0.0.1:8096/api/thermal/pair
curl -X POST http://127.0.0.1:8096/api/thermal/test_print
```

PDF rapor içeriğiyle termal çıktı almak için servis `pdftotext` kullanır. Kurulum scripti bunun için `poppler-utils` paketini yükler.

Türkçe karakterlerde bozuk sembol çıkmaması için varsayılan olarak `turkish_ascii=true` kullanılır. Bu mod `ş, ı, ğ, ü, ö, ç` karakterlerini termal yazıcıda güvenli ASCII karşılıklarına çevirir.

Termal fiş şablonu:

- Başlık varsayılanı `Yakut Dedektörü`
- PDF rapor metni
- QR kod (`qr_mode=text` ise rapor özeti metni, `qr_mode=link` ise rapor linki veya dosya adı)
- Personel imzası alanı

QR baskısı varsayılan olarak `qr_render=image` ile küçük raster görsel olarak gönderilir. Bu yöntem, native ESC/POS QR komutunu desteklemeyen PT-210 türevlerinde daha uyumludur. İstenirse config içinde `qr_render=native` yapılabilir.

```bash
curl -X POST http://127.0.0.1:8096/api/thermal/print_report \
  -H 'Content-Type: application/json' \
  -d '{"files":["scan.pdf"],"pdf_urls":["http://127.0.0.1:8080/files/saved_scans/scan.pdf"]}'
```

## Linux notları

Bluetooth servisi açık olmalıdır:

```bash
sudo systemctl enable --now bluetooth
bluetoothctl show
```

Yakındaki cihazları manuel görmek için:

```bash
bluetoothctl
scan on
devices
```

Servis logları:

```bash
journalctl -u aysua-thermal-printer-api.service -n 100 --no-pager
```

## Web entegrasyonu

Web tarafı servisle şu adres üzerinden konuşur:

```js
http://${window.location.hostname}:8096/api/thermal/...
```

Bu servis mevcut Aysua backend dosyalarını değiştirmez.
