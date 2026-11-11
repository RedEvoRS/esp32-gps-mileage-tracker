"""Viewer response headers and login redirect safety (2026-10-23 audit)."""

import io
import json
import sqlite3
from pathlib import Path
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

    def test_invalid_trip_url_components_never_read_or_change_trips(self):
        self.signed_in("admin")
        # Hand-made folders expose the old disagreement with merge-key checks.
        month = "\u0662\u0660\u0662\u0666-\u0660\u0661"
        original = self.viewer.DATA_ROOT / "2026-01"
        folder = self.viewer.DATA_ROOT / month
        folder.mkdir()
        for extension in ("json", "gpx"):
            (folder / f"drive.{extension}").write_bytes((original / f"drive.{extension}").read_bytes())
        routes = [("GET", "/trip/{key}"), ("GET", "/gpx/{key}")]
        routes += [("POST", "/trip/{key}/" + action) for action in
                   ("annotate", "remove", "restore", "lookup-place", "merge", "split")]
        tables = ("annotations", "trip_removals", "trip_history", "trip_merges", "trip_merge_parts")
        with closing(sqlite3.connect(self.db)) as conn:
            before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        files = {p: p.read_bytes() for p in self.viewer.DATA_ROOT.glob("*/*")}
        for key in ("2026-01/drive%0A", f"{quote(month)}/drive", "2026-01%0A/drive",
                    "2026-01/..", "2026-01/C:%5Cprivate", "2026-01/%5C%5Chost%5Cshare",
                    "2026-01/drive%2Fother", "2026-01/drive%5Cother"):
            for method, route in routes:
                with self.subTest(key=key, route=route):
                    with patch.object(self.viewer, "read_summary", side_effect=AssertionError("invalid URL read")), \
                            patch.object(self.viewer.GEOCODER, "lookup", side_effect=AssertionError("lookup")):
                        response = self.client.open(route.format(key=key), method=method, data={
                            "csrf_token": "known-token", "category": "business", "purpose_note": "changed",
                            "reason": "test removal", "confirm": "yes", "endpoint": "start",
                            "next_trip": "2026-01/drive",
                        })
                    self.assertEqual(response.status_code, 404)
                    response.close()
                    with closing(sqlite3.connect(self.db)) as conn:
                        self.assertEqual(before, {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                                                  for table in tables})
                    self.assertEqual(files, {p: p.read_bytes() for p in self.viewer.DATA_ROOT.glob("*/*")})
        self.assertEqual(self.viewer._folder_years(month), set())
        self.assertEqual(self.viewer._folder_years("2026-01\n"), set())

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

    def assert_unreadable_link(self, key, target):
        self.signed_in("admin")
        not_found = self.client.get("/no/such/page").get_data(as_text=True)
        # Catch application summary stats/opens, including a cache hit after
        # replacing a regular summary with a symlink.
        with patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside summary opened")), \
                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("outside summary stat")):
            trips, unreadable = self.viewer.load_trips()
            self.assertEqual(trips, [])
            self.assertEqual([t["trip_key"] for t in unreadable], [key])
            self.assertEqual(unreadable[0]["problem"], "the file could not be read")
            # A containment failure must reach the unreadable result without
            # probing presence through an escaping month or final-file link.
            with patch.object(self.viewer.os.path, "lexists", side_effect=AssertionError("unsafe presence probe")):
                with self.viewer._trip_read(trip_key=key) as (_, trips, targeted_unreadable):
                    self.assertEqual(trips, [])
                    self.assertEqual(targeted_unreadable, unreadable)
            self.assertEqual(self.viewer._recording_years(key), {2025, 2026})
            for route, status in (("/", 200), (f"/trip/{key}", 404), ("/export.csv", 200),
                                  ("/export.xlsx", 200), ("/admin/year/2026", 200)):
                with self.subTest(route=route):
                    response = self.client.get(route)
                    self.assertEqual(response.status_code, status)
                    if route.endswith(".xlsx"):
                        book = load_workbook(io.BytesIO(response.data))
                        try:
                            self.assertIn("Unreadable trips", book.sheetnames)
                            text = str([list(sheet.values) for sheet in book])
                        finally:
                            book.close()
                    else:
                        text = response.get_data(as_text=True)
                    self.assertNotIn("outside-summary-sentinel", text)
                    self.assertNotIn(str(target), text)
                    if route == "/export.csv":
                        self.assertIn("unreadable", text)
                    elif route == f"/trip/{key}":
                        self.assertEqual(text, not_found)
                    elif not route.endswith(".xlsx"):
                        self.assertIn("the file could not be read", text)
                    response.close()

    def test_summary_symlink_is_unreadable_in_lists_pages_exports_and_year_review(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        target = self.db.parent / "private-summary.json"
        target.write_text(json.dumps({"start_time": "2026-01-02T12:00:00Z", "distance_miles": 999,
                                      "device_id": "outside-summary-sentinel"}), encoding="utf-8")
        summary.unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_cached_summary_cannot_bypass_replaced_symlink_check(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        self.viewer.load_trips()
        self.assertIn(summary, self.viewer._summary_cache)
        target = self.db.parent / "private-summary.json"
        target.write_bytes(summary.read_bytes())
        summary.unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_symlinked_month_is_unreadable_and_has_no_downloadable_gpx(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "private-month"
        month.rename(target)
        self.symlink(month, target, directory=True)
        self.assert_unreadable_link("2026-01/drive", target)
        self.assertFalse(self.viewer.load_trips()[1][0]["has_gpx"])
        self.assertEqual(self.client.get("/gpx/2026-01/drive").status_code, 404)

    def test_dangling_escaping_summary_symlink_is_unreadable_without_gpx(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        target = self.db.parent / "missing-summary.json"
        summary.unlink()
        summary.with_suffix(".gpx").unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_dangling_escaping_month_symlink_is_unreadable_on_targeted_read(self):
        target = self.db.parent / "missing-month"
        self.symlink(self.viewer.DATA_ROOT / "2026-02", target, directory=True)
        with patch.object(self.viewer.os.path, "lexists", side_effect=AssertionError("unsafe presence probe")), \
                patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside summary opened")), \
                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("outside summary stat")):
            with self.viewer._trip_read(trip_key="2026-02/missing") as (_, trips, unreadable):
                self.assertEqual(trips, [])
                self.assertEqual([t["trip_key"] for t in unreadable], ["2026-02/missing"])
                self.assertEqual(unreadable[0]["problem"], "the file could not be read")

    def test_missing_trip_page_does_not_log_unreadable_summary(self):
        self.signed_in("admin")
        for key in ("2026-01/missing", "2026-02/missing"):
            with self.subTest(key=key), self.assertNoLogs(self.viewer.app.logger, "WARNING"):
                response = self.client.get(f"/trip/{key}")
                self.assertEqual(response.status_code, 404)
                response.close()

    def test_escaping_gpx_is_not_available_to_pages_or_exports(self):
        self.signed_in("admin")
        gpx = self.viewer.DATA_ROOT / "2026-01/drive.gpx"
        target = self.db.parent / "private-track.gpx"
        gpx.rename(target)
        self.symlink(gpx, target)
        self.assertFalse(self.viewer.load_trips()[0][0]["has_gpx"])
        for route in ("/", "/export.csv", "/export.xlsx"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                response.close()
        self.assertEqual(self.client.get("/gpx/2026-01/drive").status_code, 404)

    def test_unreadable_summary_hides_os_error_paths_but_logs_details(self):
        self.signed_in("admin")
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        real_open = open
        private_path = "/private-storage/example/drive.json"

        def read_error(path, *args, **kwargs):
            if Path(path) == summary:
                raise PermissionError(13, "Permission denied", private_path)
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=read_error), \
                self.assertLogs(self.viewer.app.logger, "WARNING") as logs:
            for path, status in (("/", 200), ("/trip/2026-01/drive", 404),
                                 ("/export.csv", 200), ("/export.xlsx", 200)):
                response = self.client.get(path)
                self.assertEqual(response.status_code, status)
                if path.endswith(".xlsx"):
                    workbook = load_workbook(io.BytesIO(response.data))
                    try:
                        text = str([list(sheet.values) for sheet in workbook])
                    finally:
                        workbook.close()
                else:
                    text = response.get_data(as_text=True)
                self.assertNotIn(private_path, text)
                self.assertNotIn("Permission denied", text)
                if not path.startswith("/export"):
                    self.assertIn("the file could not be read", text)
                response.close()
        self.assertIn(private_path, "\n".join(logs.output))
        self.assertIn("Permission denied", "\n".join(logs.output))

    def test_final_trip_symlinks_cannot_escape_data_root(self):
        self.signed_in("admin")
        outside = self.db.parent / "outside"
        outside.mkdir()
        for extension in ("gpx", "json"):
            target = outside / ("private." + extension)
            target.write_text("private-file-sentinel", encoding="utf-8")
            link = self.viewer.DATA_ROOT / f"2026-01/linked.{extension}"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            try:
                for route in ("trip", "gpx"):
                    with self.subTest(extension=extension, route=route):
                        response = self.client.get(f"/{route}/2026-01/linked")
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn(b"private-file-sentinel", response.data)
                        self.assertIn(b"The requested URL was not found on the server", response.data)
                        self.assertNotIn(b"the file could not be read", response.data)
                        response.close()
            finally:
                link.unlink()

    def test_safe_trip_paths_allow_missing_files_and_resolved_data_root(self):
        with self.viewer.app.test_request_context():
            paths = self.viewer._safe_trip_paths("2026-02", "missing")
        root = self.viewer.DATA_ROOT.resolve()
        self.assertEqual(paths, (root / "2026-02/missing.gpx", root / "2026-02/missing.json"))
        self.assertFalse(any(p.exists() for p in paths))

    def test_trip_path_checks_preserve_ascii_allowlists(self):
        merges = self.viewer.trip_merges
        root = self.viewer.DATA_ROOT.resolve()
        for trip_id in ("a", "AZaz09_-", "2026-01-02T12-00-00Z"):
            with self.subTest(trip_id=trip_id), self.viewer.app.test_request_context():
                paths = self.viewer._safe_trip_paths("2026-01", trip_id)
                self.assertEqual(paths, tuple(root / f"2026-01/{trip_id}{ext}"
                                              for ext in (".gpx", ".json")))
        for key in ("2026-01/../private", "2026-01/drive\n", "2026-01/drive.other",
                    "2026-01/C:" + BS + "private", "2026-01" + BS + "drive",
                    "\u0662\u0660\u0662\u0666-01/drive"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                list(merges.source_file_pairs(root, [key]))

    def test_trip_path_checks_reject_root_and_sibling_prefix(self):
        root = self.viewer.DATA_ROOT.resolve()
        for path in (root, root.parent / (root.name + "-other") / "drive.json",
                     root / ".." / "private.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.viewer.trip_merges.checked_path(root, path)

    def test_linked_data_root_and_internal_trip_links_still_work(self):
        root = self.viewer.DATA_ROOT
        alias = self.db.parent / "linked-data"
        self.symlink(alias, root, directory=True)
        link = root / "2026-01/linked.json"
        self.symlink(link, root / "2026-01/drive.json")
        with patch.object(self.viewer, "DATA_ROOT", alias), self.viewer.app.test_request_context():
            self.assertEqual(self.viewer._safe_trip_paths("2026-01", "linked")[1],
                             (root / "2026-01/drive.json").resolve())
            self.assertTrue(self.viewer._trip_file_exists(root.resolve(), alias / "2026-01/linked.json"))

    def test_targeted_presence_keeps_internal_dangling_summary_link(self):
        root = self.viewer.DATA_ROOT.resolve()
        summary = root / "2026-01/drive.json"
        summary.unlink()
        target = root / "2026-01/missing.json"
        self.symlink(summary, target)
        checked = self.viewer.trip_merges.checked_path(root, summary, preserve_final=True)
        self.assertEqual(checked, summary)
        self.assertTrue(self.viewer.os.path.lexists(checked))
        self.assertEqual(self.viewer.trip_merges.checked_path(root, summary), target)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            with self.viewer._trip_read(trip_key="2026-01/drive") as (_, trips, unreadable):
                self.assertEqual(trips, [])
                self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/drive"])
                self.assertEqual(unreadable[0]["problem"], "the file is missing")

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
        for name, text in bad.items():
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
