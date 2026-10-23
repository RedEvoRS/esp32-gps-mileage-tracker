"""Viewer response headers and login redirect safety (2026-10-23 audit)."""

import io
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.parse import quote

from openpyxl import load_workbook
from zoneinfo import ZoneInfo

import test_viewer_roles as roles


BS = chr(92)


class ViewerHardeningTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in
    login = roles.ViewerRolesTests.login

    # -- framing and sniffing headers -------------------------------------

    def assert_hardened(self, response):
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_every_kind_of_response_refuses_framing(self):
        anonymous = self.client.get("/login")
        self.assert_hardened(anonymous)
        self.assert_hardened(self.client.get("/health"))
        self.assert_hardened(self.client.get("/"))  # login redirect
        self.assert_hardened(self.client.post("/login", data={}))  # CSRF failure page
        self.signed_in("admin")
        for path in ("/", "/trip/2026-01/drive", "/static/gpx.js", "/export.csv", "/export.xlsx",
                     "/admin", "/admin/year/2026", "/gpx/2026-01/drive", "/no/such/page"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assert_hardened(response)
                response.close()
        self.assert_hardened(self.client.post("/logout", data={"csrf_token": "known-token"}))

    def test_existing_csp_is_merged_not_replaced(self):
        @self.viewer.app.route("/csp-test")
        def csp_test():
            response = self.viewer.app.response_class("ok")
            response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors *"
            return response

        self.viewer.PUBLIC_ENDPOINTS = self.viewer.PUBLIC_ENDPOINTS | {"csp_test"}
        policy = self.client.get("/csp-test").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertEqual(policy.count("frame-ancestors"), 1)
        self.assertIn("frame-ancestors 'none'", policy)

    # -- open redirect after login ----------------------------------------

    def next_after_login(self, target, raw=False):
        query = target if raw else quote(target, safe="")
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            token = sess["csrf"]
        return self.client.post(f"/login?next={query}", data={
            "username": "driver", "password": "test-password", "csrf_token": token})

    def test_next_rejects_control_characters_and_backslashes(self):
        bad = ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/" + BS + "evil.example",
               "/" + BS + "/evil.example", "/ok\x0b/evil.example", "//evil.example", "https://evil.example",
               "evil.example", "/%5Cevil.example", "/%09/evil.example"]
        for target in bad:
            for raw in (False, True):
                with self.subTest(target=target, raw=raw):
                    response = self.next_after_login(target, raw)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], "/")
                    self.client.post("/logout", data={"csrf_token": "x"})
                    with self.client.session_transaction() as sess:
                        sess.clear()

    def test_next_accepts_plain_local_paths(self):
        for target in ("/export.csv", "/trip/2026-01/drive", "/?year=2026", "/admin/year/2026"):
            with self.subTest(target=target):
                response = self.next_after_login(target)
                self.assertEqual(response.headers["Location"], target)
                with self.client.session_transaction() as sess:
                    sess.clear()

    def test_unit_safe_next(self):
        safe = self.viewer._safe_next
        self.assertIsNone(safe(None))
        self.assertIsNone(safe(""))
        self.assertIsNone(safe("/\t/evil.example"))
        self.assertIsNone(safe("/%0A/evil.example"))
        self.assertIsNone(safe("/%5Cevil.example"))
        self.assertEqual(safe("/trip/a b"), "/trip/a b")

    def test_logged_out_request_for_a_tab_path_does_not_become_an_open_redirect(self):
        for path in ("/%09/evil.example", "/%0A/evil.example", "/%0D/evil.example"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].startswith("/login?next="))
                follow = self.next_after_login_from(response.headers["Location"])
                self.assertEqual(follow.status_code, 302)
                self.assertEqual(follow.headers["Location"], "/")
                with self.client.session_transaction() as sess:
                    sess.clear()

    def next_after_login_from(self, location):
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            token = sess["csrf"]
        return self.client.post(location, data={
            "username": "driver", "password": "test-password", "csrf_token": token})

    # -- control characters and length in notes (XLSX export) -------------

    ILLEGAL = {"U+0001": chr(1), "U+000B": chr(0x0B), "U+FFFE": chr(0xFFFE), "U+FFFF": chr(0xFFFF)}

    def annotate(self, **fields):
        self.signed_in("admin")
        data = {"csrf_token": "known-token", "category": "business",
                "purpose_note": "", "destination": ""}
        data.update(fields)
        return self.client.post("/trip/2026-01/drive/annotate", data=data)

    def stored(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT purpose_note, destination FROM annotations "
                                "WHERE trip_key='2026-01/drive'").fetchone()

    def test_save_rejects_characters_xml_cannot_hold_and_says_why(self):
        for name, char in self.ILLEGAL.items():
            for field in ("purpose_note", "destination"):
                with self.subTest(char=name, field=field):
                    response = self.annotate(**{field: "Client" + char + "visit"})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(name.encode(), response.data)
        # Nothing was saved: the seeded annotation is untouched.
        self.assertEqual(self.stored()[0], "example")

    def test_save_keeps_ordinary_text_including_tabs_and_line_breaks(self):
        note = "Line one" + chr(13) + chr(10) + chr(9) + "line two " + chr(0xE9) + chr(0x2014)
        response = self.annotate(purpose_note=note, destination="Caf" + chr(0xE9))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), (note, "Caf" + chr(0xE9)))

    def test_xlsx_export_survives_old_bad_values(self):
        # A value saved before the check, or a device ID from an upload.
        bad = {name: "Client" + char + "visit" for name, char in self.ILLEGAL.items()}
        for index, (name, text) in enumerate(bad.items()):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE annotations SET purpose_note=?, destination=? WHERE trip_key='2026-01/drive'",
                             (text, text))
            with self.subTest(char=name):
                self.signed_in("admin")
                response = self.client.get("/export.xlsx")
                self.assertEqual(response.status_code, 200)
                sheet = load_workbook(io.BytesIO(response.data))["Mileage"]
                values = [cell.value for row in sheet.iter_rows() for cell in row]
                self.assertIn("Clientvisit", values)

    def test_xlsx_cleans_uploaded_text_such_as_device_ids(self):
        summary = self.viewer.Path(self.viewer.DATA_ROOT / "2026-01/drive.json")
        import json
        data = json.loads(summary.read_text(encoding="utf-8"))
        data["device_id"] = "tr" + chr(0x0B) + "acker" + chr(0xFFFE)
        summary.write_text(json.dumps(data), encoding="utf-8")
        self.signed_in("admin")
        response = self.client.get("/export.xlsx")
        self.assertEqual(response.status_code, 200)
        values = [c.value for row in load_workbook(io.BytesIO(response.data))["Mileage"].iter_rows() for c in row]
        self.assertIn("tracker", values)

    # -- length ------------------------------------------------------------

    def test_notes_and_destinations_have_a_server_side_limit_and_field_maxlength(self):
        limit = self.viewer.ANNOTATION_MAX_CHARS
        self.assertLess(limit, self.viewer.XLSX_CELL_MAX_CHARS)
        for field in ("purpose_note", "destination"):
            with self.subTest(field=field):
                over = self.annotate(**{field: "x" * (limit + 1)})
                self.assertEqual(over.status_code, 400)
                self.assertIn(str(limit).encode(), over.data)
                self.assertEqual(self.annotate(**{field: "x" * limit}).status_code, 302)
        page = self.client.get("/trip/2026-01/drive").data.decode()
        self.assertEqual(page.count(f'maxlength="{limit}"'), 2)

    def test_xlsx_flags_a_stored_value_too_long_for_a_cell(self):
        long_text = "a" * 40000
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE annotations SET purpose_note=? WHERE trip_key='2026-01/drive'", (long_text,))
        self.signed_in("admin")
        sheet = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))["Mileage"]
        cells = [c.value for row in sheet.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("aaa")]
        self.assertEqual(len(cells), 1)
        self.assertLessEqual(len(cells[0]), 32767)
        self.assertIn("TRUNCATED: 40000 characters", cells[0])
        csv_text = self.client.get("/export.csv").data.decode()
        self.assertIn(long_text, csv_text)

    # -- stored times with no local equivalent (year 1, year 9999) --------

    def write_trip(self, trip_id, **summary):
        month = self.viewer.DATA_ROOT / "2026-01"
        base = {"trip_id": trip_id, "device_id": "example", "distance_miles": 3,
                "duration_seconds": 300, "start_time": "2026-01-03T12:00:00Z",
                "end_time": "2026-01-03T12:05:00Z"}
        base.update(summary)
        (month / f"{trip_id}.json").write_text(json.dumps(base), encoding="utf-8")
        (month / f"{trip_id}.gpx").write_text("<gpx><trk><trkseg/></trk></gpx>", encoding="utf-8")

    def test_times_with_no_local_equivalent_are_unknown_not_a_500(self):
        cases = [
            ("America/Chicago", "year1", {"start_time": "0001-01-01T00:00:00Z"}),
            ("Asia/Tokyo", "year9999", {"recorded_until": "9999-12-31T23:59:59Z",
                                          "end_time": "9999-12-31T23:59:59Z"}),
            ("Asia/Tokyo", "start9999", {"start_time": "9999-12-31T23:00:00Z"}),
        ]
        for zone, trip_id, summary in cases:
            with self.subTest(zone=zone, trip=trip_id):
                self.write_trip(trip_id, **summary)
                with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo(zone)):
                    self.assertIsNone(self.viewer._to_local(self.viewer._parse(
                        summary.get("start_time") or summary["recorded_until"])))
                    self.assertEqual(self.viewer.fmt_local_iso(
                        summary.get("start_time") or summary["recorded_until"]), "")
                    self.signed_in("admin")
                    for path in ("/", "/?year=2026", "/export.csv", "/export.xlsx", "/admin",
                                 "/admin/year/2026", f"/trip/2026-01/{trip_id}"):
                        response = self.client.get(path)
                        self.assertEqual(response.status_code, 200, path)
                        response.close()
                    # The admin can still take it out of the log.
                    removed = self.client.post(f"/trip/2026-01/{trip_id}/remove", data={
                        "csrf_token": "known-token", "reason": "bad timestamp"})
                    self.assertEqual(removed.status_code, 302)

    def test_year_1_trip_is_listed_as_undated_and_stays_out_of_every_year(self):
        self.write_trip("year1", start_time="0001-01-01T00:00:00Z")
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("America/Chicago")):
            trips, _ = self.viewer.load_trips()
            trip = next(t for t in trips if t["id"] == "year1")
            self.assertIsNone(self.viewer._trip_local_year(trip))
            self.assertEqual(self.viewer.trip_years(trip), set())
            # With a year closed, the closed-year check runs on every edit and
            # must cope with the unconvertible date too.
            self.signed_in("admin")
            self.assertEqual(self.client.post("/admin/year/2026/close", data={
                "csrf_token": "known-token", "confirm": "yes"}).status_code, 302)
            self.assertEqual(self.client.get("/trip/2026-01/year1").status_code, 200)
            self.assertEqual(self.client.post("/trip/2026-01/year1/annotate", data={
                "csrf_token": "known-token", "category": "business"}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
