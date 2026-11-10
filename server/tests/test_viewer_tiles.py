"""Private base-map configuration and safe Leaflet template rendering."""

import html
import json
import os
import re
import unittest
from unittest.mock import patch

import test_viewer_roles as roles
from common.settings import DEFAULT_MAP_TILE_URL, map_tiles
from test_round_trip import load_service


class TileSettingsTests(unittest.TestCase):
    def test_defaults_and_explicit_disable(self):
        for value in (None, ""):
            self.assertEqual(map_tiles(value, ""),
                             (DEFAULT_MAP_TILE_URL, "© OpenStreetMap contributors"))
        self.assertEqual(map_tiles("none", ""), (None, "© OpenStreetMap contributors"))

    def test_custom_template_and_attribution_are_text(self):
        url = "https://{s}.tiles.example.test/{z}/{x}/{y}{r}.png?style=plain&v=1"
        credit = '<img src=x onerror="alert(1)"> & <script>bad()</script>'
        self.assertEqual(map_tiles(url, credit), (url, html.escape(credit)))
        self.assertEqual(map_tiles("http://tiles.example.test/{z}/{x}/{y}", "Local tiles")[1],
                         "Local tiles")

    def test_reject_bad_templates(self):
        base = "https://tiles.example.test/{z}/{x}/{y}"
        for value in (" ", "NONE", "ftp://tiles.example.test/{z}/{x}/{y}",
                      "https:///{z}/{x}/{y}", base.replace("{y}", "{q}"),
                      base + "{bad}", base + "{", base + "#fragment",
                      base.replace("tiles.", "user@tiles."), base + "/" * 2048,
                      base.replace("test/", "test:bad/"), base.replace("test/", "test:0/"),
                      *(base + c for c in ('"', "'", "`", "\\", "<", ">", " ", "\t", "\n", "\0", "\x7f", "\x85"))):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "MAP_TILE_URL"):
                map_tiles(value, "")

    def test_reject_excessive_or_control_attribution(self):
        for value in ("x" * 2001, "line\nline", "control\0", "control\x85"):
            with self.assertRaisesRegex(ValueError, "MAP_TILE_ATTRIBUTION"):
                map_tiles("", value)

    def test_compose_passes_settings_only_to_viewer(self):
        compose = (roles.SERVER / "docker-compose.yml").read_text()
        viewer = compose.split("  mileage-viewer:", 1)[1].split("  mileage-monitor:", 1)[0]
        for key in ("MAP_TILE_URL", "MAP_TILE_ATTRIBUTION"):
            self.assertEqual(compose.count("${" + key + ":-}"), 1)
            self.assertIn("${" + key + ":-}", viewer)
            self.assertIn(key + "=", (roles.SERVER / ".env.example").read_text())


class TilePageTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in

    def test_rendered_configuration_is_json_and_credit_is_html_escaped(self):
        url, credit = map_tiles("https://tiles.example.test/{z}/{x}/{y}?v=1&layer=plain",
                                '</script><img src=x onerror="alert(1)"> & credit')
        with patch.object(self.viewer, "MAP_TILE_URL", url), patch.object(
                self.viewer, "MAP_TILE_ATTRIBUTION", credit):
            response = self.signed_in("admin").get("/trip/2026-01/drive")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(re.search(r"var tileUrl = (.*);", page)[1]), url)
        self.assertEqual(json.loads(re.search(r"attribution: (.*)", page)[1]), credit)
        self.assertNotIn('</script><img', page)
        self.assertNotIn('attribution: "<', page)
        self.assertEqual(response.headers["Content-Security-Policy"], "frame-ancestors 'none'")
        self.assertIn("new L.GPX", page)

    def test_none_retains_track_and_theme_background(self):
        with patch.object(self.viewer, "MAP_TILE_URL", None):
            page = self.signed_in("admin").get("/trip/2026-01/drive").get_data(as_text=True)
        self.assertIn("var tileUrl = null;", page)
        self.assertIn("if (tileUrl) L.tileLayer", page)
        self.assertIn("new L.GPX", page)
        self.assertIn("background: var(--map-bg)", page)
        self.assertIn("--map-bg: #20262d", page)
        self.assertIn("--map-bg: #ddd", page)

    def test_bad_setting_stops_startup(self):
        with patch.dict(os.environ, {"DISPLAY_TZ": "UTC", "MAP_TILE_URL": "javascript:bad"}):
            with self.assertRaisesRegex(RuntimeError, "MAP_TILE_URL"):
                load_service("mileage-viewer")
