"""Shared viewer theme markup, palette contrast and map isolation."""

import json
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from test_round_trip import load_service


class HeaderParser(HTMLParser):
    """Record header elements and their parents without depending on whitespace."""

    def __init__(self, html):
        super().__init__()
        self.parents = []
        self.elements = []
        self.feed(re.search(r"<header>.*?</header>", html, re.S).group())

    def handle_starttag(self, tag, attrs):
        element = (tag, dict(attrs))
        self.elements.append((element, list(self.parents)))
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.parents.append(element)

    def handle_endtag(self, tag):
        self.parents.pop()


class ViewerThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(root / "data"),
            "VIEWER_DB_PATH": str(root / "annotations.db"),
            "VIEWER_USERNAME": "driver",
            "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "theme-test-session-key-" * 3,
            "DISPLAY_TZ": "UTC",
        }):
            cls.viewer = load_service("mileage-viewer")
        month = cls.viewer.DATA_ROOT / "2026-10"
        month.mkdir(parents=True)
        (month / "sample.json").write_text(json.dumps({
            "trip_id": "sample", "start_time": "2026-10-01T12:00:00Z",
            "end_time": "2026-10-01T12:10:00Z", "distance_miles": 1,
            "duration_seconds": 600, "max_speed_mph": 20, "num_points": 2,
        }), encoding="utf-8")
        (month / "sample.gpx").write_text("<gpx/>", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.viewer.app.test_client()

    def assert_theme_page(self, route, authenticated=True):
        if authenticated:
            with self.client.session_transaction() as session:
                session["user"] = "driver"
                session["pw_tag"] = self.viewer._password_tag()
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count(self.viewer.THEME_HEAD), 1)
        init = html.index('<script id="theme-init">')
        self.assertLess(init, html.index("<style>"))
        for stylesheet in re.finditer(r'<link\b[^>]*rel="stylesheet"', html):
            self.assertLess(init, stylesheet.start())
        self.assertEqual(html.count(self.viewer.THEME_BUTTON), 1)
        self.assertRegex(html, r'<button\b[^>]*type="button"[^>]*data-theme-toggle>Theme: Auto</button>')
        css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertIn(':root:not([data-theme="light"])', css)
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn("color-scheme: light dark", css)
        self.assertIn("color-scheme: light;", css)
        self.assertIn("color-scheme: dark;", css)
        return html

    def test_login_theme_without_session(self):
        html = self.assert_theme_page("/login", authenticated=False)
        # The login page needs no static request, which would require login.
        self.assertNotRegex(html, r"<script\b[^>]*\bsrc=")
        self.assertEqual(self.client.get("/static/leaflet.js").status_code, 302)

    def test_list_theme(self):
        self.assert_theme_page("/")

    def test_trip_theme(self):
        self.assert_theme_page("/trip/2026-10/sample")

    def test_device_health_theme(self):
        self.assert_theme_page("/device-health")

    def assert_device_health_box(self, route, current=False):
        html = self.assert_theme_page(route)
        elements = HeaderParser(html).elements
        actions = next(element for element, _ in elements
                       if element[1].get("class") == "header-actions")
        children = [element for element, parents in elements
                    if parents and parents[-1] == actions]
        self.assertEqual([element[0] for element in children],
                         ["div", "span" if current else "a", "span", "a", "button", "form"])
        version, box, identity, account, theme, logout = children
        self.assertEqual(identity[1].get("class"), "account-name")
        self.assertEqual(account[1].get("class"), "account-link")
        self.assertEqual(account[1].get("href"), "/admin")
        self.assertEqual(version[1], {"class": "header-version"})
        self.assertEqual(box[1].get("class"), "device-health")
        self.assertIn("data-theme-toggle", theme[1])
        self.assertEqual(logout[1].get("class"), "logout")
        health_elements = [element for element, _ in elements
                           if element[1].get("class") == "device-health"]
        self.assertEqual(health_elements, [box])
        if current:
            self.assertEqual(box[1].get("aria-current"), "page")
            self.assertNotIn("href", box[1])
            # Both current-page and login identity spans stay in the action group.
            self.assertEqual([element for element, _ in elements if element[0] == "span"], [box, identity])
            self.assertFalse(any(element[1].get("href") == "/device-health"
                                 for element, _ in elements))
            self.assertIn('<a href="/">← Trips</a>', html)
            self.assertIn("<h1>Device health</h1>", html)
        else:
            self.assertEqual(box[1].get("href"), "/device-health")
            self.assertNotIn("aria-current", box[1])

    def test_list_device_health_box_precedes_theme(self):
        self.assert_device_health_box("/")

    def test_trip_device_health_box_precedes_theme(self):
        self.assert_device_health_box("/trip/2026-10/sample")

    def test_device_health_current_box_replaces_centered_label(self):
        self.assert_device_health_box("/device-health", current=True)

    def test_map_filter_only_affects_tiles(self):
        css = self.viewer.BASE_CSS
        self.assertRegex(css, r"\.leaflet-tile-pane\s*\{\s*filter:\s*var\(--tile-filter\)")
        self.assertIn("--tile-filter: invert(1) hue-rotate(180deg)", css)
        filtered_rules = re.findall(r"([^{}]+)\{[^{}]*(?<![\w-])filter\s*:[^{}]*\}", css)
        self.assertEqual([s.strip() for s in filtered_rules], [".leaflet-tile-pane"])
        self.assertNotRegex(css, r"\.leaflet-overlay-pane[^{}]*\{[^{}]*\bfilter\s*:")
        self.assertIn("color: '#d9480f'", self.viewer.TRIP_HTML)

    def test_styles_use_palette_properties(self):
        for template in (self.viewer.LOGIN_HTML, self.viewer.LIST_HTML,
                         self.viewer.TRIP_HTML, self.viewer.DEVICE_HEALTH_HTML):
            css = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
            without_palette = re.sub(r"--[\w-]+\s*:[^;]+;", "", css)
            self.assertNotRegex(without_palette, r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

    def test_palette_text_contrast(self):
        def properties(css):
            return dict(re.findall(r"--([\w-]+):\s*([^;]+);", css))

        def rgb(value):
            if value.startswith("#"):
                value = value[1:]
                if len(value) == 3:
                    value = "".join(c * 2 for c in value)
                return [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            return [float(n) for n in re.findall(r"[\d.]+", value)]

        def luminance(color):
            return sum(w / 10000 * (c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4)
                       for w, c in zip((2126, 7152, 722), color, strict=True))

        light = properties(self.viewer.THEME_CSS.split("@media")[0])
        dark = {**light, **properties(self.viewer.DARK_THEME_CSS)}
        version_rule = re.search(r"\.header-version\s*\{([^}]+)\}", self.viewer.BASE_CSS).group(1)
        version_style = dict(re.findall(r"([\w-]+):\s*([^;]+);", version_rule))
        self.assertEqual(version_style, {
            "color": "var(--header-text)", "opacity": ".85",
            "font-size": "12px", "white-space": "nowrap",
        })
        self.assertEqual(rgb(light["header-button-text"]), [1, 1, 1])
        for mode, palette in (("light", light), ("dark", dark)):
            with self.subTest(mode=mode, foreground="header-version", background="header-bg"):
                opacity = float(version_style["opacity"])
                header_bg = rgb(palette["header-bg"])
                version_color = [fg * opacity + bg * (1 - opacity)
                                 for fg, bg in zip(rgb(palette["header-text"]), header_bg, strict=True)]
                values = sorted((luminance(version_color), luminance(header_bg)))
                self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 4.5)

            def background(name, under, palette=palette):
                value = palette[name]
                if value == "transparent":
                    return under
                color = rgb(value)
                if len(color) == 4:
                    return [color[i] / 255 * color[3] + under[i] * (1 - color[3]) for i in range(3)]
                return color

            pairs = [
                ("text", bg) for bg in ("page-bg", "table-head-bg", "row-hover-bg", "card-bg", "tag-bg")
            ] + [
                ("muted", bg) for bg in ("page-bg", "row-hover-bg", "card-bg")
            ] + [
                ("header-text", "header-bg"),
                ("button-text", "button-bg"), ("button-text", "secondary-bg"),
                ("nav-text", "nav-bg"), ("nav-text", "nav-hover"),
                ("business-text", "business-bg"), ("personal-text", "personal-bg"),
                ("other-text", "other-bg"), ("warn-text", "warn-bg"),
                ("input-text", "input-bg"), ("input-text", "card-bg"), ("heading", "card-bg"),
                ("header-button-text", "header-button-bg"),
                ("header-button-text", "header-button-hover"),
                ("map-control-text", "map-control-bg"),
                ("map-control-text", "map-control-hover"),
                ("map-control-disabled", "map-control-hover"),
                ("attribution-text", "attribution-bg"), ("map-link", "attribution-bg"),
            ]
            for fg, bg in pairs:
                with self.subTest(mode=mode, foreground=fg, background=bg):
                    # Header button normal/hover overlays must be composited over
                    # each theme's blue header before measuring white text.
                    under = rgb(palette["header-bg"] if bg.startswith("header-button") else palette["page-bg"])
                    # Use the worst tile under the translucent attribution.
                    if bg == "attribution-bg":
                        under = [0, 0, 0] if mode == "light" else [1, 1, 1]
                    values = sorted((luminance(rgb(palette[fg])), luminance(background(bg, under))))
                    self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 4.5)

            with self.subTest(mode=mode, foreground="input-border", background="page-bg"):
                values = sorted(luminance(rgb(palette[name])) for name in ("input-border", "page-bg"))
                self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 3)


if __name__ == "__main__":
    unittest.main()
