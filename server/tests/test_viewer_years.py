"""Closing a tax year locks its trips; the year review page checks the year's log."""

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
        rows = self.history("year/2026")
        self.assertEqual([r[:4] for r in rows], [
            ("year/2026", "driver", "admin", "year_close"),
            ("year/2026", "driver", "admin", "year_reopen"),
        ])
        # The close records the totals it saved; the reopen records the ones it discarded.
        saved = json.loads(rows[0][4])["snapshot"]
        self.assertEqual((saved["trips"], saved["totals"]["all"]), (0, 0))
        self.assertEqual(json.loads(rows[1][4]), {"discarded_snapshot": saved})
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



class YearReviewTests(YearTestBase):
    def review(self, year, client=None):
        response = (client or self.admin).get(f"/admin/year/{year}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def section(self, page, name):
        start = page.index(f'id="{name}-heading"')
        return page[start:page.index("</section>", start)]

    def at(self, stamp):
        return patch.object(self.viewer.trip_log, "now_utc", return_value=stamp)

    def annotated_trip(self, name, start, category, miles):
        key = self.add_trip(name, start, miles=miles)
        if category:
            self.annotate(key, category=category, destination="Example Road", purpose_note="Visit")
        return key

    def test_totals_equal_the_csv_totals_for_the_same_year(self):
        self.annotated_trip("b1", "2026-03-01T12:00:00Z", "business", 2.5)
        self.annotated_trip("b2", "2026-03-02T12:00:00Z", "business", 1.25)
        self.annotated_trip("p1", "2026-03-03T12:00:00Z", "personal", 4.0)
        self.annotated_trip("u1", "2026-03-04T12:00:00Z", "", 0.75)
        self.annotated_trip("o1", "2026-03-05T12:00:00Z", "other", 0.5)
        self.annotated_trip("edge", "2027-01-01T03:00:00Z", "business", 3.0)   # Dec 31 local: 2026
        self.annotated_trip("next", "2027-01-01T09:00:00Z", "business", 9.0)   # Jan 1 local: 2027
        self.annotated_trip("old", "2025-06-01T12:00:00Z", "business", 7.0)
        removed = self.annotated_trip("gone", "2026-04-01T12:00:00Z", "business", 100.0)
        self.post(f"/trip/{removed}/remove", reason="Duplicate")
        self.add_broken("broken", "2026-03")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.review(2026)
            totals = {r["category"]: float(r["distance_miles"])
                      for r in self.csv_rows("?year=2026") if r["row_type"] == "total"}
        shown = {m.group(1): (int(m.group(2)), float(m.group(3))) for m in re.finditer(
            r"<td>(\w+)</td><td>(\d+)</td><td>([0-9.]+)</td>", self.section(page, "totals"))}
        self.assertEqual({k: v[1] for k, v in shown.items()}, totals)
        self.assertEqual(totals, {"business": 6.75, "personal": 4.0, "other": 0.5,
                                  "unclassified": 0.75, "all": 12.0})
        self.assertEqual({k: v[0] for k, v in shown.items()},
                         {"business": 3, "personal": 1, "other": 1, "unclassified": 1, "all": 6})

    def test_trips_with_no_category(self):
        plain = self.add_trip("plain", "2026-03-01T12:00:00Z")
        done = self.add_trip("done", "2026-03-02T12:00:00Z")
        self.annotate(done, category="personal")
        other_year = self.add_trip("old", "2025-03-01T12:00:00Z")
        section = self.section(self.review(2026), "uncategorized")
        self.assertIn("(1)", section)
        self.assertIn(f'href="/trip/{plain}?year=2026"', section)
        self.assertNotIn(done, section)
        self.assertNotIn(other_year, section)
        self.annotate(plain, category="business")
        self.assertIn("Every trip has a category.", self.section(self.review(2026), "uncategorized"))

    def test_business_trips_missing_a_note_or_destination(self):
        def trip(name, category, note, destination):
            key = self.add_trip(name, "2026-03-01T12:00:00Z")
            self.annotate(key, category=category, purpose_note=note, destination=destination)
            return key
        complete = trip("complete", "business", "Visit", "Example Road")
        no_note = trip("nonote", "business", "", "Example Road")
        no_destination = trip("nodest", "business", "Visit", "")
        neither = trip("neither", "business", "  ", "")
        personal = trip("personal", "personal", "", "")
        section = self.section(self.review(2026), "incomplete")
        self.assertIn("(3)", section)

        def row(key):
            match = re.search(rf'href="/trip/{key}\?year=2026".*?</tr>', section, re.S)
            return match.group(0) if match else None
        self.assertIsNone(row(complete))
        self.assertIsNone(row(personal))
        self.assertIn("<td>purpose note</td>", row(no_note))
        self.assertIn("<td>destination</td>", row(no_destination))
        self.assertIn("<td>purpose note and destination</td>", row(neither))

    def test_unreadable_trips_follow_the_list_and_export_rule(self):
        self.add_broken("broken-march", "2026-03")
        self.add_broken("broken-jan", "2027-01")
        self.add_broken("broken-other", "2025-05")
        pages, exported = {}, {}
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            for year in (2025, 2026, 2027):
                pages[year] = self.section(self.review(year), "unreadable")
                exported[year] = sorted(r["trip_id"] for r in self.csv_rows(f"?year={year}")
                                        if r["row_type"] == "unreadable")
        self.assertEqual(exported, {2025: ["broken-other"], 2026: ["broken-jan", "broken-march"],
                                    2027: ["broken-jan"]})
        for year, section in pages.items():
            shown = sorted(re.findall(r'href="/trip/[^/"]+/([^"?]+)\?year=', section))
            self.assertEqual(shown, exported[year])
        self.assertIn('href="/trip/2027-01/broken-jan?year=2026"', pages[2026])

    def test_undated_trips_are_listed_with_count_and_miles_but_stay_out_of_every_year(self):
        self.annotated_trip("dated", "2026-03-01T12:00:00Z", "business", 0.923)
        undated = self.add_trip("clockless", "2026-03-02T12:00:00Z", miles=2.787,
                                start_time=None, end_time=None, timing_status="untimed")
        removed = self.add_trip("clockless-gone", "2026-03-03T12:00:00Z", miles=5,
                                start_time=None, end_time=None, timing_status="untimed")
        self.assertEqual(self.post(f"/trip/{removed}/remove", reason="test").status_code, 302)
        for year in (2025, 2026):
            with self.subTest(year=year):
                page = self.review(year)
                section = self.section(page, "undated")
                self.assertIn("Undated trips (1, 2.787 mi)", section)
                self.assertIn(f'href="/trip/{undated}?year={year}"', section)
                self.assertNotIn("clockless-gone", section)
        # The year rule itself is unchanged: totals and exports leave them out.
        self.assertEqual(self.csv_rows("?year=2026")[-1]["distance_miles"], "0.923")
        self.assertEqual([r["trip_id"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"], ["dated"])
        all_years = [r for r in self.csv_rows() if r["row_type"] == "trip"]
        self.assertEqual({r["trip_id"] for r in all_years}, {"dated", "clockless"})
        self.close_year(2026)
        self.assertEqual(self.annotate(undated, category="business").status_code, 302)
        self.assertIn("Undated trips (1, 2.787 mi)", self.section(self.review(2026), "undated"))

    def test_review_without_undated_trips_says_so(self):
        self.annotated_trip("dated", "2026-03-01T12:00:00Z", "business", 1)
        section = self.section(self.review(2026), "undated")
        self.assertIn("Undated trips (0, 0.000 mi)", section)
        self.assertIn("Every trip has a date.", section)

    def test_removed_trips_with_reason_actor_and_time(self):
        key = self.add_trip("gone", "2026-03-01T12:00:00Z")
        other_year = self.add_trip("old", "2025-03-01T12:00:00Z")
        broken = self.add_broken("broken", "2026-05")
        with self.at("2026-02-03T20:30:00+00:00"), self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.post(f"/trip/{key}/remove", reason="Duplicate upload")
            self.post(f"/trip/{other_year}/remove", reason="Wrong year")
            self.post(f"/trip/{broken}/remove", reason="Damaged file")
            section = self.section(self.review(2026), "removed")
        self.assertIn("(2)", section)
        self.assertIn(f'href="/trip/{key}?year=2026"', section)
        self.assertIn(f'href="/trip/{broken}?year=2026"', section)
        self.assertIn("Duplicate upload", section)
        self.assertIn("Damaged file", section)
        self.assertIn("<td>driver</td>", section)
        self.assertIn("2026-02-03 12:30 PST", section)
        self.assertNotIn("Wrong year", section)

    def test_edits_after_the_year_ended_use_the_local_boundary(self):
        first = self.add_trip("first", "2025-06-01T12:00:00Z")
        in_2026 = self.add_trip("later", "2026-06-01T12:00:00Z")
        # 2026-01-01 00:00 local is 08:00 UTC.
        for stamp, note in (("2025-12-31T23:59:59+00:00", "Before one"),
                            ("2026-01-01T07:59:59+00:00", "Before two"),
                            ("2026-01-01T08:00:00+00:00", "At boundary"),
                            ("2026-02-01T12:00:00+00:00", "After year end")):
            with self.at(stamp):
                self.assertEqual(self.annotate(first, purpose_note=note).status_code, 302)
        with self.at("2026-02-01T12:00:00+00:00"):
            self.annotate(in_2026, purpose_note="Not a 2025 trip")
        section = self.section(self.review(2025), "late")
        self.assertIn("(2)", section)
        self.assertIn("Note: Before two → At boundary", section)
        self.assertIn("Note: At boundary → After year end", section)
        self.assertNotIn("Before one →", section)
        self.assertNotIn("Not a 2025 trip", section)
        self.assertIn("2026-01-01 00:00 PST", section)
        self.assertIn(f'href="/trip/{first}"', section)
        self.assertNotIn("Not a 2025 trip", self.section(self.review(2026), "late"))

    def test_late_merge_split_and_merged_annotation_are_found_by_parts_and_aliases(self):
        first = self.add_trip("first", "2025-06-01T12:00:00Z")
        second = self.add_trip("second", "2025-06-01T12:01:30Z")
        with self.at("2026-02-01T12:00:00+00:00"):
            merged = self.post(f"/trip/{first}/merge", next_trip=second).headers["Location"]
        merged = merged.split("/trip/")[1].split("?")[0]
        with self.at("2026-02-02T12:00:00+00:00"):
            self.annotate(merged, purpose_note="Merged note")
        section = self.section(self.review(2025), "late")
        self.assertIn("(2)", section)
        self.assertIn("Merged " + first, section)
        self.assertIn("Merged note", section)
        with self.at("2026-02-03T12:00:00+00:00"):
            self.post(f"/trip/{merged}/split", confirm="yes")
        section = self.section(self.review(2025), "late")
        self.assertIn("(2)", section)
        self.assertIn("Restored original recordings", section)

    def test_the_late_edit_scan_does_not_read_history_per_trip(self):
        for index in range(8):
            self.add_trip(f"trip{index}", f"2025-06-0{index + 1}T12:00:00Z")
        with patch.object(self.viewer, "_related_history_rows", side_effect=AssertionError("per-trip scan")), \
                patch.object(self.viewer, "_history_trip_keys", side_effect=AssertionError("per-trip scan")):
            self.review(2025)

    def test_year_close_and_reopen_rows_show_on_the_review_page(self):
        with self.at("2025-12-30T12:00:00+00:00"):
            self.close_year(2025)
        with self.at("2026-02-01T12:00:00+00:00"):
            self.reopen_year(2025)
            self.close_year(2026)
        section = self.section(self.review(2025), "late")
        self.assertIn("(2)", section)
        self.assertIn("Year closed", section)
        self.assertIn("Year reopened", section)
        self.assertIn('href="/?year=2025"', section)
        other = self.section(self.review(2026), "late")
        self.assertIn("(1)", other)
        self.assertEqual(other.count("Year reopened"), 0)

    def test_status_close_and_reopen_forms(self):
        page = self.review(2026)
        self.assertIn("2026 is open", page)
        self.assertIn('action="/admin/year/2026/close"', page)
        self.assertNotIn('action="/admin/year/2026/reopen"', page)
        self.assertIn('<input type="checkbox" name="confirm" value="yes" required>', page)
        self.close_year(2026)
        page = self.review(2026)
        self.assertIn("Closed for edits", page)
        self.assertIn("by driver on", page)
        self.assertIn('action="/admin/year/2026/reopen"', page)
        self.assertNotIn('action="/admin/year/2026/close"', page)
        self.assertIn("1999 is open", self.review(1999))

    def test_review_is_admin_only_and_validates_the_year(self):
        self.assertEqual(self.editor.get("/admin/year/2026").status_code, 403)
        for bad in ("0000", "26", "20266", "abcd"):
            self.assertEqual(self.admin.get(f"/admin/year/{bad}").status_code, 400, bad)
        self.assertIn("9999 is open", self.review(9999))

    def test_review_links_from_the_list_and_admin_page(self):
        self.add_trip("drive", "2026-03-01T12:00:00Z")
        self.add_trip("old", "2025-03-01T12:00:00Z")
        self.close_year(2026)
        self.close_year(2030)
        self.assertIn('href="/admin/year/2026"', self.admin.get("/?year=2026").get_data(as_text=True))
        self.assertNotIn("/admin/year/", self.admin.get("/").get_data(as_text=True))
        self.assertNotIn("/admin/year/", self.editor.get("/?year=2026").get_data(as_text=True))
        admin = self.admin.get("/admin").get_data(as_text=True)
        rows = {m.group(1): m.group(2) for m in re.finditer(r"<tr><td>(\d{4})</td>\s*<td>(.*?)</td>", admin, re.S)}
        self.assertEqual(list(rows), ["2030", "2026", "2025"])
        self.assertTrue(rows["2026"].startswith("Closed by driver on"))
        self.assertTrue(rows["2030"].startswith("Closed by driver on"))
        self.assertEqual(rows["2025"], "Open")
        for year in rows:
            self.assertIn(f'href="/admin/year/{year}"', admin)

    def test_merge_across_the_new_year_is_filed_under_its_first_part(self):
        first = self.add_trip("first", "2027-01-01T07:59:00Z", ym="2027-01")
        second = self.add_trip("second", "2027-01-01T08:00:30Z", ym="2027-01")
        merged = self.post(f"/trip/{first}/merge", next_trip=second).headers["Location"]
        merged = merged.split("/trip/")[1].split("?")[0]
        self.assertIn(f'href="/trip/{merged}?year=2026"', self.section(self.review(2026), "uncategorized"))
        self.assertIn("Every trip has a category.", self.section(self.review(2027), "uncategorized"))

    def test_review_and_closing_do_not_change_the_exports(self):
        self.add_trip("drive", "2026-03-01T12:00:00Z")
        before = self.admin.get("/export.csv?year=2026").data
        self.review(2026)
        self.close_year(2026)
        self.review(2026)
        self.assertEqual(self.admin.get("/export.csv?year=2026").data, before)


    # --- Snapshot at close, and trips received after the close (#449) ---

    CLOSE = "2026-10-01T00:00:00+00:00"
    BEFORE = "2026-09-01T00:00:00+00:00"
    AFTER = "2026-10-05T08:30:00.123456+00:00"

    def closed_year_with_two_trips(self):
        """Two business trips (2.5 + 1.25 mi) received before a close at CLOSE."""
        first = self.add_trip("b1", "2026-03-01T12:00:00Z", miles=2.5, _received_at=self.BEFORE)
        second = self.add_trip("b2", "2026-03-02T12:00:00Z", miles=1.25, _received_at=self.BEFORE)
        for key in (first, second):
            self.annotate(key, category="business", destination="Example Road", purpose_note="Visit")
        with self.at(self.CLOSE):
            self.close_year(2026)
        return first, second

    def sheet_values(self, path):
        workbook = load_workbook(io.BytesIO(self.admin.get(path).data))
        try:
            return {sheet.title: [[cell.value for cell in row] for row in sheet.iter_rows()]
                    for sheet in workbook}
        finally:
            workbook.close()

    def saved(self, year=2026):
        with closing(sqlite3.connect(self.db)) as conn:
            return json.loads(conn.execute("SELECT snapshot FROM tax_years WHERE year=?", (year,)).fetchone()[0])

    def test_close_saves_the_totals_the_review_and_exports_show(self):
        self.closed_year_with_two_trips()
        self.add_trip("p1", "2026-04-01T12:00:00Z", miles=4.0, _received_at=self.BEFORE)   # no category
        self.add_broken("broken", "2026-03")
        self.reopen_year(2026)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.close_year(2026)
            totals = {r["category"]: float(r["distance_miles"])
                      for r in self.csv_rows("?year=2026") if r["row_type"] == "total"}
        snapshot = self.saved()
        self.assertEqual(snapshot["totals"], totals)
        self.assertEqual(snapshot["counts"], {"business": 2, "personal": 0, "other": 0, "unclassified": 1, "all": 3})
        self.assertEqual((snapshot["trips"], snapshot["unreadable"]), (3, 1))
        self.assertEqual(snapshot["trip_keys"], ["2026-03/b1", "2026-03/b2", "2026-04/p1"])
        # The history row carries the figures (not the key list) for the audit trail.
        details = json.loads(self.history("year/2026")[-1][4])["snapshot"]
        self.assertEqual(details, {k: v for k, v in snapshot.items() if k != "trip_keys"})
        page = self.admin.get("/admin/history").get_data(as_text=True)
        self.assertIn("Saved totals: 3 trips, 7.750 mi", page)

    def test_a_trip_received_after_the_close_is_visible_everywhere(self):
        self.closed_year_with_two_trips()
        page = self.review(2026)
        self.assertIn("The totals match the ones saved at the close.", page)
        self.assertIn("No trip arrived after the close.", self.section(page, "after-close"))
        late = self.add_trip("late", "2026-09-20T12:00:00Z", miles=2.0, _received_at=self.AFTER)
        page = self.review(2026)
        section = self.section(page, "after-close")
        self.assertIn("(1, 2.000 mi)", section)
        self.assertIn(f'href="/trip/{late}?year=2026"', section)
        self.assertIn("2026-10-05", section)
        self.assertIn("reopen 2026", section)
        # Saved next to current, with the difference spelled out and highlighted.
        snapshot = self.section(page, "snapshot")
        self.assertIn("The totals now differ", snapshot)
        self.assertRegex(snapshot, r'<tr class="changed">\s*<td>all</td>\s*<td>2</td>\s*<td>3.750</td>\s*<td>3</td><td>5.750</td>')
        self.assertIn("+1 trips, +2.000 mi", snapshot)
        self.assertRegex(snapshot, r"<tr>\s*<td>personal</td>")           # unchanged rows stay plain
        # The late trip still counts: nothing is hidden, and the saved figures don't move.
        self.assertEqual(self.saved()["totals"]["all"], 3.75)
        csv_rows = self.csv_rows("?year=2026")
        trips = [r for r in csv_rows if r["row_type"] == "trip"]
        self.assertEqual({r["trip_id"]: r["received_after_close"] for r in trips}, {"b1": "", "b2": "", "late": "yes"})
        self.assertEqual(float(next(r for r in csv_rows if r["row_type"] == "total"
                                    and r["category"] == "all")["distance_miles"]), 5.75)
        values = self.sheet_values("/export.xlsx?year=2026")["Mileage"]
        self.assertEqual(values[0][-1], "received_after_close")
        self.assertEqual({row[1]: row[-1] for row in values[1:]}, {"b1": None, "b2": None, "late": "yes"})
        # All years marks it too, judged against its own year's close.
        self.add_trip("open-year", "2025-03-01T12:00:00Z", _received_at=self.AFTER)
        marks = {r["trip_id"]: r["received_after_close"] for r in self.csv_rows() if r["row_type"] == "trip"}
        self.assertEqual(marks, {"b1": "", "b2": "", "late": "yes", "open-year": ""})
        # It still can't be categorized until the year is reopened.
        self.assertEqual(self.annotate(late, category="business").status_code, 409)

    def test_existing_export_columns_keep_their_order_and_the_new_one_comes_last(self):
        self.closed_year_with_two_trips()
        text = self.admin.get("/export.csv?year=2026").data.decode("utf-8")
        header = next(csv.reader(io.StringIO(text)))
        self.assertEqual(header[-4:], ["last_edited_by", "last_edited_at", "row_type", "received_after_close"])
        self.assertEqual(header[:3], ["date", "trip_id", "device_id"])
        self.assertTrue(all(len(row) == len(header) for row in csv.reader(io.StringIO(text))))

    def test_reopen_and_close_again_takes_a_fresh_snapshot(self):
        self.closed_year_with_two_trips()
        self.add_trip("late", "2026-09-20T12:00:00Z", miles=2.0, _received_at=self.AFTER)
        self.reopen_year(2026)
        reopened = json.loads(self.history("year/2026")[-1][4])
        self.assertEqual(reopened["discarded_snapshot"]["totals"]["all"], 3.75)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tax_years").fetchone()[0], 0)
        self.assertNotIn("Saved totals (taken when", self.review(2026))
        with self.at("2026-10-10T00:00:00+00:00"):
            self.close_year(2026)
        self.assertEqual(self.saved()["totals"]["all"], 5.75)
        page = self.review(2026)
        self.assertIn("The totals match the ones saved at the close.", page)
        self.assertIn("No trip arrived after the close.", self.section(page, "after-close"))
        self.assertEqual({r["received_after_close"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"}, {""})
        history = self.admin.get("/admin/history").get_data(as_text=True)
        self.assertIn("Discarded totals: 2 trips, 3.750 mi", history)

    def test_a_year_closed_before_snapshots_existed_still_renders(self):
        self.closed_year_with_two_trips()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tax_years SET snapshot=NULL")
        late = self.add_trip("late", "2026-09-20T12:00:00Z", miles=2.0, _received_at=self.AFTER)
        page = self.review(2026)
        self.assertIn("No totals were saved for 2026", self.section(page, "snapshot"))
        self.assertIn(f"/trip/{late}", self.section(page, "after-close"))     # late detection needs no snapshot
        self.assertEqual([r["received_after_close"] for r in self.csv_rows("?year=2026")
                          if r["row_type"] == "trip" and r["trip_id"] == "late"], ["yes"])
        self.assertEqual(self.admin.get("/admin").status_code, 200)
        self.reopen_year(2026)                      # nothing to discard: the row has no figures
        self.assertEqual(json.loads(self.history("year/2026")[-1][4]), {})
        self.close_year(2026)
        self.assertEqual(self.saved()["totals"]["all"], 5.75)

    def test_unreadable_saved_text_is_treated_as_no_snapshot(self):
        self.closed_year_with_two_trips()
        for text in ("not json", "[1, 2]", ""):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE tax_years SET snapshot=?", (text,))
            self.assertIn("No totals were saved", self.section(self.review(2026), "snapshot"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tax_years SET snapshot=?", (json.dumps({"trips": 2}),))
        page = self.section(self.review(2026), "snapshot")
        self.assertIn("unknown", page)
        self.assertIn("The totals now differ", page)

    def test_old_annotations_db_gains_the_snapshot_column(self):
        path = self.root / "old.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE tax_years (year INTEGER PRIMARY KEY, closed_by TEXT NOT NULL, closed_at TEXT NOT NULL)")
            conn.execute("INSERT INTO tax_years VALUES (2025, 'driver', '2026-01-20T00:00:00+00:00')")
        for _ in range(2):                           # a second startup finds the column already there
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                self.viewer.trip_log.init_schema(conn)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(self.viewer.trip_log.closed_years(conn),
                             {2025: {"closed_by": "driver", "closed_at": "2026-01-20T00:00:00+00:00", "snapshot": None}})
            self.viewer.trip_log.close_year(conn, 2026, "driver", {"trips": 0})
            self.assertEqual(self.viewer.trip_log.closed_years(conn)[2026]["snapshot"], {"trips": 0})

    def test_a_summary_with_no_usable_received_at_is_never_late(self):
        self.closed_year_with_two_trips()
        self.add_trip("old", "2026-06-01T12:00:00Z")                         # an older ingest wrote no stamp
        self.add_trip("junk", "2026-06-02T12:00:00Z", _received_at=12345)    # damaged
        self.add_trip("text", "2026-06-03T12:00:00Z", _received_at="yesterday")
        page = self.review(2026)
        self.assertIn("(0, 0.000 mi)", self.section(page, "after-close"))
        self.assertEqual({r["received_after_close"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"}, {""})
        # A bad stamp doesn't make the trip unreadable: all five still count.
        self.assertEqual(len(self.viewer.load_trips()[0]), 5)

    def test_received_at_compares_as_an_instant_and_the_close_itself_is_not_late(self):
        self.closed_year_with_two_trips()
        self.add_trip("same", "2026-06-01T12:00:00Z", _received_at=self.CLOSE)
        self.add_trip("zoned", "2026-06-02T12:00:00Z", _received_at="2026-09-30T23:59:59-08:00")   # 07:59:59Z on Oct 1: after
        self.add_trip("early", "2026-06-03T12:00:00Z", _received_at="2026-09-30T15:00:00-08:00")   # 23:00:00Z on Sep 30: before
        late = {t["trip_id"] for t in self.viewer.load_trips()[0] if self.viewer.received_after(t, self.CLOSE)}
        self.assertEqual(late, {"zoned"})

    def test_a_merged_trip_arrived_when_its_last_part_did(self):
        first = self.add_trip("first", "2026-03-01T12:00:00Z", _received_at=self.BEFORE)
        second = self.add_trip("second", "2026-03-01T12:10:00Z", _received_at="2026-09-15T00:00:00+00:00")
        response = self.post(f"/trip/{first}/merge", next_trip=second)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        merged = [t for t in self.viewer.load_trips()[0] if t.get("parts")]
        self.assertEqual([t["received_at"] for t in merged], ["2026-09-15T00:00:00+00:00"])
        with self.at(self.CLOSE):
            self.close_year(2026)
        self.assertEqual([r["received_after_close"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"], [""])
        with self.at("2026-09-01T00:00:00+00:00"):
            self.reopen_year(2026)
            self.close_year(2026)
        self.assertEqual([r["received_after_close"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"], ["yes"])

    def test_a_display_tz_change_shows_as_a_difference_without_a_late_upload(self):
        stays = self.add_trip("stays", "2026-06-01T12:00:00Z", _received_at=self.BEFORE)
        leaves = self.add_trip("leaves", "2026-12-31T16:00:00Z", _received_at=self.BEFORE)   # LA: Dec 31, Tokyo: Jan 1
        with self.at(self.CLOSE):
            self.close_year(2026)
        joins = self.add_trip("joins", "2025-12-31T16:00:00Z", _received_at=self.BEFORE)     # LA: 2025, Tokyo: 2026
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("Asia/Tokyo")):
            page = self.review(2026)
            marks = {r["received_after_close"] for r in self.csv_rows("?year=2026") if r["row_type"] == "trip"}
        snapshot = self.section(page, "snapshot")
        # Same trip count and miles by coincidence, but not the same trips.
        self.assertIn("The totals match the ones saved at the close, but they are made of", snapshot)
        self.assertIn(f"No longer counted: {leaves}", snapshot)
        self.assertIn(f'href="/trip/{joins}?year=2026"', snapshot)
        self.assertNotIn(stays, snapshot)
        self.assertIn("No trip arrived after the close.", self.section(page, "after-close"))
        self.assertEqual(marks, {""})

    def test_everyday_login_cannot_see_the_review_snapshot(self):
        self.closed_year_with_two_trips()
        self.assertEqual(self.editor.get("/admin/year/2026").status_code, 403)


if __name__ == "__main__":
    unittest.main()
