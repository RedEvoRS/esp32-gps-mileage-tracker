"""Closing a tax year locks its trips; the year review page checks the year's log."""

import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from test_round_trip import load_service

CLOSED = "is closed for edits. The admin can reopen it on the year review page."


class YearTestBase(unittest.TestCase):
    """Two logins and synthetic trips. DISPLAY_TZ is a UTC-8 zone in January."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = self.root / "annotations.db"
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(self.data), "VIEWER_DB_PATH": str(self.db),
            "VIEWER_USERNAME": "driver", "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "years-test-session-key-" * 3, "DISPLAY_TZ": "America/Los_Angeles",
            "VIEWER_GEOCODER_URL": "",
        }):
            self.viewer = load_service("mileage-viewer")
        self.admin = self.viewer.app.test_client()
        self.sign_in(self.admin, "admin")
        self.viewer.users.change_user(self.db, "add", "helper", "everyday-password", "driver")
        self.editor = self.viewer.app.test_client()
        self.sign_in(self.editor, "editor")

    def sign_in(self, client, role):
        with client.session_transaction() as session:
            session.update(csrf="test-csrf")
            if role == "admin":
                session.update(user="driver", pw_tag=self.viewer._password_tag())
            else:
                user = self.viewer.users.get_user(self.db, "helper")
                session.update(user="helper", kind="editor",
                               pw_tag=self.viewer.users.session_tag(self.viewer.VIEWER_SECRET_KEY, user))

    def add_trip(self, name, start, ym=None, device="device-a", miles=1.25, **summary):
        """Write a trip starting at the UTC ISO string start; folder defaults to its UTC month."""
        ym = ym or start[:7]
        folder = self.data / ym
        folder.mkdir(exist_ok=True)
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        points = "".join(
            f'<trkpt lat="0" lon="{index * 0.00001}"><time>{(begin + timedelta(seconds=offset)).isoformat()}'
            f'</time><speed>10</speed><hdop>1</hdop></trkpt>' for index, offset in enumerate((0, 10, 20)))
        (folder / f"{name}.gpx").write_text(
            "<gpx><trk><trkseg>" + points + "</trkseg></trk></gpx>", encoding="utf-8")
        data = {
            "trip_id": name, "device_id": device, "gpx_file": f"{ym}/{name}.gpx",
            "start_time": begin.isoformat(), "end_time": (begin + timedelta(seconds=20)).isoformat(),
            "recorded_until": (begin + timedelta(seconds=20)).isoformat(),
            "distance_miles": miles, "duration_seconds": 20, "avg_speed_mph": 20,
            "max_speed_mph": 25, "num_points": 3, **summary,
        }
        data = {k: v for k, v in data.items() if v is not None}
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        return f"{ym}/{name}"

    def add_broken(self, name, ym):
        folder = self.data / ym
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.json").write_text("[not a summary]", encoding="utf-8")
        (folder / f"{name}.gpx").write_text("<gpx/>", encoding="utf-8")
        return f"{ym}/{name}"

    def post(self, path, client=None, **data):
        return (client or self.admin).post(path, data={"csrf_token": "test-csrf", **data})

    def annotate(self, key, client=None, **fields):
        return self.post(f"/trip/{key}/annotate", client, **fields)

    def close_year(self, year):
        response = self.post(f"/admin/year/{year}/close", confirm="yes")
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))

    def reopen_year(self, year):
        response = self.post(f"/admin/year/{year}/reopen", confirm="yes")
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))

    def csv_rows(self, query=""):
        text = self.admin.get("/export.csv" + query).data.decode("utf-8")
        return list(csv.DictReader(io.StringIO(text)))

    def history(self, key=None):
        with closing(sqlite3.connect(self.db)) as conn:
            sql = "SELECT trip_key, actor, actor_role, action, details FROM trip_history"
            if key:
                return conn.execute(sql + " WHERE trip_key=? ORDER BY id", (key,)).fetchall()
            return conn.execute(sql + " ORDER BY id").fetchall()


class ClosedYearTests(YearTestBase):
    def test_year_follows_local_time_at_the_new_year_boundary(self):
        # Dec 31 19:00 local is already Jan 1 in UTC, and filed under 2026.
        late = self.add_trip("late", "2027-01-01T03:00:00Z")
        # Jan 1 00:00:00 local is 2027.
        early = self.add_trip("early", "2027-01-01T08:00:00Z")
        self.close_year(2026)
        self.assertEqual(self.annotate(late, category="business").status_code, 409)
        self.assertEqual(self.annotate(early, category="business").status_code, 302)
        self.reopen_year(2026)
        self.close_year(2027)
        self.assertEqual(self.annotate(early, category="personal").status_code, 409)
        self.assertEqual(self.annotate(late, category="personal").status_code, 302)
        # The lock agrees with where the list files each trip.
        self.assertEqual([r["trip_id"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"], ["late"])
        self.assertEqual([r["trip_id"] for r in self.csv_rows("?year=2027") if r["row_type"] == "trip"], ["early"])

    def test_untimed_trip_has_no_year_and_is_never_locked(self):
        key = self.add_trip("untimed", "2026-03-01T12:00:00Z", start_time=None, end_time=None,
                            recorded_until=None, timing_status="untimed")
        self.close_year(2026)
        self.close_year(2025)
        self.close_year(2027)
        self.assertEqual(self.annotate(key, category="business").status_code, 302)
        self.assertNotIn(CLOSED, self.admin.get(f"/trip/{key}").get_data(as_text=True))

    def test_partial_timing_trip_uses_first_recorded_time(self):
        key = self.add_trip("partial", "2026-03-01T12:00:00Z", start_time=None, end_time=None,
                            timing_status="partial", timed_from="2026-03-01T12:00:00+00:00",
                            recorded_until="2026-03-01T12:00:20+00:00")
        self.close_year(2026)
        self.assertEqual(self.annotate(key, category="business").status_code, 409)

    def test_unreadable_trip_goes_by_folder_and_january_counts_for_both_years(self):
        january = self.add_broken("broken-jan", "2027-01")
        march = self.add_broken("broken-mar", "2026-03")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.close_year(2026)
            self.assertEqual(self.annotate(january, category="business").status_code, 409)
            self.assertEqual(self.annotate(march, category="business").status_code, 409)
            self.reopen_year(2026)
            self.close_year(2027)
            self.assertEqual(self.annotate(january, category="business").status_code, 409)
            self.assertEqual(self.annotate(march, category="business").status_code, 302)
            self.reopen_year(2027)
            self.close_year(2028)
            self.close_year(2025)
            self.assertEqual(self.annotate(january, category="personal").status_code, 302)
            self.assertEqual(self.annotate(march, category="personal").status_code, 302)

    def merged_across_new_year(self):
        # A ends 23:59:20 local on Dec 31 (2026); B starts 00:00:30 local on Jan 1 (2027).
        first = self.add_trip("first", "2027-01-01T07:59:00Z", ym="2027-01")
        second = self.add_trip("second", "2027-01-01T08:00:30Z", ym="2027-01")
        response = self.post(f"/trip/{first}/merge", next_trip=second)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        merged = response.headers["Location"].split("/trip/")[1].split("?")[0]
        return first, second, merged

    def test_merged_trip_is_locked_when_any_part_year_is_closed(self):
        first, second, merged = self.merged_across_new_year()
        self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], merged)
        # Its own year is its first part's: 2026. Closing 2027 still locks it
        # through the part that started in 2027.
        self.assertEqual([r["trip_id"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"], ["first"])
        self.close_year(2027)
        for client in (self.admin, self.editor):
            self.assertEqual(self.annotate(merged, client, category="business").status_code, 409)
        for client in (self.admin, self.editor):
            self.assertEqual(self.post(f"/trip/{merged}/split", client, confirm="yes").status_code, 409)
        self.assertEqual(self.post(f"/trip/{merged}/remove", reason="Duplicate").status_code, 409)
        self.reopen_year(2027)
        self.close_year(2026)
        self.assertEqual(self.post(f"/trip/{merged}/split", confirm="yes").status_code, 409)
        self.reopen_year(2026)
        self.assertEqual(self.post(f"/trip/{merged}/split", confirm="yes").status_code, 302)
        self.assertEqual({t["trip_key"] for t in self.viewer.load_trips()[0]}, {first, second})

    def test_merge_refused_when_either_trip_is_in_a_closed_year(self):
        first = self.add_trip("first", "2027-01-01T07:59:00Z", ym="2027-01")
        second = self.add_trip("second", "2027-01-01T08:00:30Z", ym="2027-01")
        for year in (2026, 2027):
            self.close_year(year)
            for client in (self.admin, self.editor):
                response = self.post(f"/trip/{first}/merge", client, next_trip=second)
                self.assertEqual(response.status_code, 409)
                self.assertIn(f"{year} {CLOSED}", response.get_data(as_text=True))
            self.reopen_year(year)
        self.assertEqual(self.post(f"/trip/{first}/merge", next_trip=second).status_code, 302)

    def test_every_locked_action_is_refused_for_both_roles_until_reopened(self):
        keep = self.add_trip("keep", "2026-03-01T12:00:00Z")
        following = self.add_trip("following", "2026-03-01T12:01:30Z")
        merged_a = self.add_trip("merge-a", "2026-04-01T12:00:00Z")
        merged_b = self.add_trip("merge-b", "2026-04-01T12:01:30Z")
        merged = self.post(f"/trip/{merged_a}/merge", next_trip=merged_b).headers["Location"]
        merged = merged.split("/trip/")[1].split("?")[0]
        removed = self.add_trip("removed", "2026-05-01T12:00:00Z")
        self.assertEqual(self.post(f"/trip/{removed}/remove", reason="Duplicate").status_code, 302)
        self.close_year(2026)
        for client in (self.admin, self.editor):
            for path, data in ((f"/trip/{keep}/annotate", {"category": "business"}),
                               (f"/trip/{keep}/merge", {"next_trip": following}),
                               (f"/trip/{merged}/split", {"confirm": "yes"})):
                response = self.post(path, client, **data)
                self.assertEqual(response.status_code, 409, path)
                self.assertIn(f"2026 {CLOSED}", response.get_data(as_text=True))
        self.assertEqual(self.post(f"/trip/{keep}/remove", reason="Duplicate").status_code, 409)
        self.assertEqual(self.post(f"/trip/{removed}/restore").status_code, 409)
        self.assertEqual(self.history("2026-03/keep"), [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM annotations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_removals").fetchone()[0], 1)

        self.reopen_year(2026)
        self.assertEqual(self.annotate(keep, category="business").status_code, 302)
        self.assertEqual(self.post(f"/trip/{removed}/restore").status_code, 302)
        self.assertEqual(self.post(f"/trip/{keep}/remove", reason="Duplicate").status_code, 302)
        self.assertEqual(self.post(f"/trip/{merged}/split", confirm="yes").status_code, 302)
        self.assertEqual(self.post(f"/trip/{keep}/restore").status_code, 302)
        self.assertEqual(self.post(f"/trip/{keep}/merge", next_trip=following).status_code, 302)

    def test_other_years_stay_editable(self):
        old = self.add_trip("old", "2025-06-01T12:00:00Z")
        new = self.add_trip("new", "2026-06-01T12:00:00Z")
        self.close_year(2025)
        self.assertEqual(self.annotate(old, category="business").status_code, 409)
        self.assertEqual(self.annotate(new, category="business").status_code, 302)

    def test_place_lookup_stays_allowed_in_a_closed_year(self):
        key = self.add_trip("drive", "2026-03-01T12:00:00Z")
        self.close_year(2026)
        for client in (self.admin, self.editor):
            with patch.object(self.viewer.GEOCODER, "lookup", return_value="Example Road, Sampletown"):
                response = self.post(f"/trip/{key}/lookup-place", client, endpoint="start")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"label": "Example Road, Sampletown"})

    def test_close_and_reopen_write_history_in_the_same_transaction(self):
        self.close_year(2026)
        self.reopen_year(2026)
        self.assertEqual(self.history("year/2026"), [
            ("year/2026", "driver", "admin", "year_close", "{}"),
            ("year/2026", "driver", "admin", "year_reopen", "{}"),
        ])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tax_years").fetchone()[0], 0)
        page = self.admin.get("/admin/history").get_data(as_text=True)
        self.assertIn("Year closed", page)
        self.assertIn("Closed for edits.", page)
        self.assertIn("Year reopened", page)
        self.assertIn("Reopened for edits.", page)
        # A failed write leaves neither a year row nor a history row.
        with patch.object(self.viewer.trip_log, "record", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                self.admin.post("/admin/year/2026/close", data={"csrf_token": "test-csrf", "confirm": "yes"})
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tax_years").fetchone()[0], 0)
        self.assertEqual(len(self.history("year/2026")), 2)

    def test_close_and_reopen_validation(self):
        self.assertEqual(self.admin.post("/admin/year/2026/close", data={"confirm": "yes"}).status_code, 400)
        self.assertEqual(self.admin.post("/admin/year/2026/reopen", data={"confirm": "yes"}).status_code, 400)
        for confirm in ({}, {"confirm": "no"}, {"confirm": ""}):
            self.assertEqual(self.post("/admin/year/2026/close", **confirm).status_code, 400)
        self.assertEqual(self.history(), [])
        for bad in ("0000", "26", "20266", "abcd", "2026x"):
            self.assertEqual(self.post(f"/admin/year/{bad}/close", confirm="yes").status_code, 400, bad)
            self.assertEqual(self.post(f"/admin/year/{bad}/reopen", confirm="yes").status_code, 400, bad)
        self.assertEqual(self.post("/admin/year/2026/reopen", confirm="yes").status_code, 409)
        self.close_year(2026)
        self.assertEqual(self.post("/admin/year/2026/close", confirm="yes").status_code, 409)
        self.assertEqual(self.post("/admin/year/2026/reopen").status_code, 400)
        self.assertEqual(len(self.history()), 1)
        self.reopen_year(2026)
        self.assertEqual(self.post("/admin/year/2026/reopen", confirm="yes").status_code, 409)

    def test_everyday_login_cannot_close_or_reopen(self):
        self.assertEqual(self.post("/admin/year/2026/close", self.editor, confirm="yes").status_code, 403)
        self.close_year(2026)
        self.assertEqual(self.post("/admin/year/2026/reopen", self.editor, confirm="yes").status_code, 403)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tax_years").fetchone()[0], 1)
        self.assertEqual(len(self.history()), 1)

    def test_trip_page_shows_notice_and_no_edit_forms_for_both_roles(self):
        key = self.add_trip("drive", "2026-03-01T12:00:00Z")
        other = self.add_trip("open", "2025-03-01T12:00:00Z")
        removed = self.add_trip("removed", "2026-04-01T12:00:00Z")
        self.post(f"/trip/{removed}/remove", reason="Duplicate")
        self.close_year(2026)
        with closing(sqlite3.connect(self.db)) as conn:
            closed_at = conn.execute("SELECT closed_at FROM tax_years").fetchone()[0]
        notice = f"2026 is closed for edits (closed by driver on {self.viewer.fmt_day(closed_at)})"
        for client in (self.admin, self.editor):
            page = client.get(f"/trip/{key}").get_data(as_text=True)
            self.assertIn(notice, page)
            for action in ("annotate", "merge", "split", "remove", "restore"):
                self.assertNotIn(f"/trip/{key}/{action}", page)
            # Read-only parts remain: the map, the track and the place section.
            self.assertIn('id="map"', page)
            self.assertIn(f"/gpx/{key}", page)
            self.assertIn("Recorded endpoint places", page)
            self.assertNotIn(notice, client.get(f"/trip/{other}").get_data(as_text=True))
            self.assertIn("/annotate", client.get(f"/trip/{other}").get_data(as_text=True))
        page = self.admin.get(f"/trip/{removed}").get_data(as_text=True)
        self.assertIn(notice, page)
        self.assertIn("Removed from the log", page)
        self.assertNotIn(f"/trip/{removed}/restore", page)
        self.reopen_year(2026)
        page = self.admin.get(f"/trip/{key}").get_data(as_text=True)
        self.assertNotIn("closed for edits", page)
        self.assertIn(f"/trip/{key}/annotate", page)

    def test_viewing_and_exporting_a_closed_year_is_unchanged(self):
        key = self.add_trip("drive", "2026-03-01T12:00:00Z", miles=2.5)
        self.add_trip("walk", "2026-03-02T12:00:00Z", miles=1.0)
        self.annotate(key, category="business", destination="Example Road", purpose_note="Visit")
        before_csv = self.admin.get("/export.csv?year=2026").data
        before_xlsx = self.sheet_values("/export.xlsx?year=2026")
        before_gpx = self.gpx_bytes(key)
        self.close_year(2026)
        self.assertEqual(self.admin.get("/export.csv?year=2026").data, before_csv)
        self.assertEqual(self.editor.get("/export.csv?year=2026").data, before_csv)
        self.assertEqual(self.sheet_values("/export.xlsx?year=2026"), before_xlsx)
        self.assertEqual(self.gpx_bytes(key), before_gpx)
        for client in (self.admin, self.editor):
            self.assertEqual(client.get(f"/trip/{key}").status_code, 200)
            self.assertIn("3.500 mi total", client.get("/?year=2026").get_data(as_text=True))

    def gpx_bytes(self, key):
        response = self.admin.get(f"/gpx/{key}")
        try:
            return response.data
        finally:
            response.close()

    def sheet_values(self, path):
        workbook = load_workbook(io.BytesIO(self.admin.get(path).data))
        try:
            return {sheet.title: [[cell.value for cell in row] for row in sheet.iter_rows()]
                    for sheet in workbook}
        finally:
            workbook.close()

    def test_trip_list_badge_for_both_roles(self):
        self.add_trip("drive", "2026-03-01T12:00:00Z")
        self.add_trip("old", "2025-03-01T12:00:00Z")
        for client in (self.admin, self.editor):
            self.assertNotIn("Closed for edits", client.get("/?year=2026").get_data(as_text=True))
        self.close_year(2026)
        for client in (self.admin, self.editor):
            self.assertIn("Closed for edits", client.get("/?year=2026").get_data(as_text=True))
            self.assertNotIn("Closed for edits", client.get("/?year=2025").get_data(as_text=True))
            self.assertNotIn("Closed for edits", client.get("/").get_data(as_text=True))
        self.reopen_year(2026)
        self.assertNotIn("Closed for edits", self.admin.get("/?year=2026").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
