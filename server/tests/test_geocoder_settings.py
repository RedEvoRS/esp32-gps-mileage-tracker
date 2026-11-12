"""Shared address lookup rules: no HTTP requests or DNS resolution."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.settings import geocoder_endpoint, geocoder_interval, geocoder_public_host


class GeocoderSettingsTests(unittest.TestCase):
    def test_interval_defaults_and_bounds(self):
        for raw, expected in (("", 0.1), (" 1.1 ", 1.1), ("0.1", 0.1), ("60", 60)):
            self.assertEqual(geocoder_interval(raw), expected)
        for raw in ("nan", "inf", "0.09", "61", "bad"):
            with self.assertRaisesRegex(ValueError, "finite number from 0.1 to 60"):
                geocoder_interval(raw)

    def test_endpoint_and_agent(self):
        geocoder_endpoint("", "")  # Disabled viewer retains existing behavior.
        geocoder_endpoint("http://provider/reverse", "Tracker/1.0")
        for url in ("ftp://example.com/reverse", "https://user@example.com/reverse",
                    "https://example.com/reverse?q=x", "https://example.com/reverse#x",
                    "https://openstreetmap.org/reverse", "https://NOMINATIM.OpenStreetMap.org./reverse",
                    "http://example.com:0/reverse", "http://[broken/reverse", "http://example.com/ bad"):
            with self.assertRaisesRegex(ValueError, "does not permit vehicle tracking"):
                geocoder_endpoint(url, "Tracker/1.0")
        for agent in ("", "bad\nagent", "x" * 257, "nonascii\u00e9"):
            with self.assertRaises(ValueError):
                geocoder_endpoint("https://example.com/reverse", agent)

    def test_host_classification(self):
        for host in ("localhost", "provider", "provider.", "127.0.0.1", "10.0.0.1",
                     "172.16.0.1", "192.168.0.1", "100.64.0.1", "100.127.255.254",
                     "[::1]", "[fd00::1]", "[fe80::1]", "[::ffff:10.0.0.1]"):
            with self.subTest(host=host):
                self.assertFalse(geocoder_public_host(f"http://{host}/reverse"))
        for host in ("example.com", "provider.example.com."):
            self.assertTrue(geocoder_public_host(f"https://{host}/reverse"))
