import unittest

import aysua_thermal_printer_api as api


class ThermalPrinterApiTests(unittest.TestCase):
    def test_report_text_contains_file(self):
        cfg = dict(api.DEFAULT_CONFIG)
        text = api.build_report_text({"files": ["scan.pdf"], "user": "admin"}, cfg)
        self.assertIn("scan.pdf", text)
        self.assertIn("admin", text)

    def test_escpos_payload_has_init(self):
        cfg = dict(api.DEFAULT_CONFIG)
        payload = api.escpos_bytes_from_text("hello", cfg)
        self.assertTrue(payload.startswith(b"\x1b\x40"))
        self.assertIn(b"hello", payload)


if __name__ == "__main__":
    unittest.main()
