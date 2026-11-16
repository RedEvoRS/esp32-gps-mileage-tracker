"""One recording and annotation, with each local year's share of its miles."""

import csv
import io
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from test_viewer_years import YearTestBase


class NewYearTests(YearTestBase):
    CUT = datetime(2027, 1, 1, 8, tzinfo=timezone.utc)
    POINTS = [[(-30, 0, 10, 1), (10, 400, 10, 1), (30, 800, 10, 1)]]
    # All pre-existing columns, frozen independently of the implementation.
    COLUMNS = [
        "date", "trip_id", "device_id", "distance_miles", "duration_minutes", "category",
        "purpose_note", "start_time", "end_time", "max_speed_mph", "avg_speed_mph", "gpx_file",
        "destination", "start_latitude", "start_longitude", "end_latitude", "end_longitude",
        "timing_status", "timed_from", "recorded_until", "year_basis", "last_edited_by", "last_edited_at",
    ]

    def setUp(self):
        super().setUp()
        from common import trip_summary
        self.summary = trip_summary

    def recording(self, name="crossing", segments=None, miles=9.83, **overrides):
        segments = self.POINTS if segments is None else segments
        xml = ["<gpx><trk>"]
        for segment in segments:
            xml.append("<trkseg>")
            for seconds, east, speed, hdop in segment:
                lon = east * 180 / (math.pi * 6371000)
                xml.append(f'<trkpt lat="0" lon="{lon}">')
                if seconds is not None:
                    xml.append(f"<time>{(self.CUT + timedelta(seconds=seconds)).isoformat()}</time>")
                if speed is not None:
                    xml.append(f"<speed>{speed}</speed>")
                xml.append(f"<hdop>{hdop}</hdop></trkpt>")
            xml.append("</trkseg>")
        raw = ("".join(xml) + "</trk></gpx>").encode()
        summary = {k: v for k, v in self.summary.summarize(self.summary.parse_gpx(raw)).items()
                   if not k.startswith("_") and k != "distance_miles"}
        summary.update(overrides)
        begin = next((p[0] for seg in segments for p in seg if p[0] is not None), -30)
        start = (self.CUT + timedelta(seconds=begin)).isoformat()
        key = self.add_trip(name, start, ym="2027-01", miles=miles, **summary)
        (self.data / (key + ".gpx")).write_bytes(raw)
        return key

    def trip(self, key=None, include_removed=False):
        trips, _ = self.viewer.load_trips(include_removed=include_removed)
        return next(t for t in trips if key is None or t["trip_key"] == key)

    def figures(self, year):
        return self.viewer.year_figures(*self.viewer.year_members(*self.viewer.load_trips(), year))

    def review(self, year):
        response = self.admin.get(f"/admin/year/{year}")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def sheet(self, query):
        response = self.admin.get("/export.xlsx" + query)
        self.assertEqual(response.status_code, 200)
        with closing(load_workbook(io.BytesIO(response.data))) as workbook:
            return {s.title: list(s.values) for s in workbook}

    def test_shares_totals_counts_categories_and_full_trip_are_consistent(self):
        key = self.recording()
        before_files = {p.name: p.read_bytes() for p in (self.data / "2027-01").iterdir()}
        for category in ("business", "personal", "other", ""):
            with self.subTest(category=category):
                self.assertEqual(self.annotate(key, category=category).status_code, 302)
                trip = self.trip(key)
                old, new = self.figures(2026), self.figures(2027)
                self.assertEqual((old["totals"]["all"], new["totals"]["all"]), (3.686, 6.144))
                bucket = category or "unclassified"
                for figures in (old, new):
                    self.assertEqual(figures["counts"]["all"], 1)
                    self.assertEqual(figures["counts"][bucket], 1)
                    self.assertEqual(figures["totals"][bucket], figures["totals"]["all"])
                self.assertEqual(Decimal(str(old["totals"]["all"])) + Decimal(str(new["totals"]["all"])), Decimal("9.830"))
                self.assertEqual(self.viewer.mileage_totals([trip])["all"], 9.83)
                self.assertEqual(trip["distance_miles"], 9.83)  # year_members never mutates it
                selected, _ = self.viewer.year_members([trip], [], 2027)
                self.assertIsNot(selected[0], trip)
                self.assertEqual(selected[0]["other_year"], 2026)
        self.assertEqual(before_files, {p.name: p.read_bytes() for p in (self.data / "2027-01").iterdir()})

    def test_list_trip_page_year_picker_review_and_navigation(self):
        key = self.recording()
        newer = self.add_trip("later", "2027-01-02T12:00:00Z")
        for year, share, other in ((2026, "3.69", 2027), (2027, "6.14", 2026)):
            page = self.admin.get(f"/?year={year}").get_data(as_text=True)
            self.assertIn(f"{share} of 9.83 mi, rest in {other}", page)
            review = self.review(year)
            section = review.split('id="new-year-heading"')[1].split("</section>")[0]
            self.assertIn(key, section)
            self.assertIn("2026: 3.686 mi; 2027: 6.144 mi", section)
            self.assertEqual(self.viewer.year_figures(*self.viewer.year_members(*self.viewer.load_trips(), year))["counts"]["all"],
                             1 if year == 2026 else 2)
        all_page = self.admin.get("/").get_data(as_text=True)
        self.assertIn('value="2026"', all_page)
        self.assertIn('value="2027"', all_page)
        self.assertIn("2026: 3.686 mi; 2027: 6.144 mi", all_page)
        self.assertIn("2026: 3.686 mi; 2027: 6.144 mi", self.admin.get(f"/trip/{key}").get_data(as_text=True))
        self.assertIn(f'href="/trip/{key}?year=2027"', self.admin.get(f"/trip/{newer}?year=2027").get_data(as_text=True))
        self.assertEqual({r["year"] for r in self.viewer._year_list()}, {2026, 2027})

    def test_before_after_and_stationary_cooldown_stay_whole(self):
        before = self.recording("before", [[(-90, 0, 10, 1), (-60, 300, 10, 1)]])
        after = self.recording("after", [[(10, 0, 10, 1), (40, 300, 10, 1)]])
        cooldown = self.recording("cooldown", [[(-90, 0, 10, 1), (-60, 300, 10, 1),
                                                 (-30, 300, 0, 1), (30, 301, 0, 1)]])
        # Even the first stopped fix after midnight cannot extend movement.
        sparse = self.recording("sparse", [[(-30, 0, 10, 1), (-10, 200, 10, 1), (290, 201, 0, 1)]])
        crossing = self.recording()
        with patch.object(self.viewer.trip_merges, "parsed_parts", wraps=self.viewer.trip_merges.parsed_parts) as read:
            trips, _ = self.viewer.load_trips()
        self.assertEqual(read.call_count, 1)
        for trip in trips:
            if trip["trip_key"] != crossing:
                self.assertNotIn("year_shares", trip)
                self.assertNotIn("year_split_note", trip)
                self.assertEqual(trip["distance_miles"], 9.83)
        self.assertEqual({t["trip_key"] for t in self.viewer.year_members(trips, [], 2026)[0]},
                         {before, cooldown, sparse, crossing})
        self.assertEqual({t["trip_key"] for t in self.viewer.year_members(trips, [], 2027)[0]}, {after, crossing})

    def test_timezone_changes_the_cut_without_stale_shares(self):
        key = self.recording()
        self.assertEqual(self.figures(2026)["trips"], 1)
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("UTC")):
            self.assertEqual(self.figures(2026)["trips"], 0)
            self.assertEqual(self.figures(2027)["totals"]["all"], 9.83)
            self.assertNotIn("year_shares", self.trip(key))
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("Etc/GMT+9")):
            self.assertEqual(self.figures(2026)["totals"]["all"], 9.83)
            self.assertNotIn("year_shares", self.trip(key))
        self.assertEqual(self.figures(2027)["totals"]["all"], 6.144)

    def test_segment_gap_is_bridged_and_divided_by_time(self):
        self.recording(segments=[[(-30, 0, 10, 1)], [(10, 400, 10, 1)]], miles=8)
        self.assertEqual(self.figures(2026)["totals"]["all"], 6)
        self.assertEqual(self.figures(2027)["totals"]["all"], 2)

    def test_shared_rules_skip_hdop_stationary_and_implausible_steps(self):
        points = [[(-30, 0, 10, 1), (-20, 100000, 10, 9), (10, 400, 10, 1),
                   (20, 100000, 10, 1), (30, 800, 10, 1), (40, 900, 0, 1), (50, 901, 0, 1)]]
        key = self.recording(segments=points, miles=10)
        # 400 m across midnight (300 before), two impossible steps skipped,
        # then 100 m from moving to stopped; stopped scatter adds nothing.
        self.assertEqual(self.trip(key)["year_shares"], {2026: 6, 2027: 4})

    def test_stored_total_is_used_even_when_fresh_summary_differs(self):
        key = self.recording(miles=12.347)
        parsed = self.summary.parse_gpx((self.data / (key + ".gpx")).read_bytes())
        self.assertNotEqual(self.summary.summarize(parsed)["distance_miles"], 12.347)
        trip = self.trip(key)
        self.assertEqual(trip["year_shares"], {2026: 4.63, 2027: 7.717})
        self.assertEqual(sum(Decimal(str(s)) for s in trip["year_shares"].values()), Decimal("12.347"))

    def test_summary_fields_stay_identical_when_counting_each_side(self):
        key = self.recording()
        parsed = self.summary.parse_gpx((self.data / (key + ".gpx")).read_bytes())
        # Full output checked against the pre-refactor summary, including its
        # internal date anchors. Coordinates are invented equatorial points.
        expected = {
            "timing_status": "complete", "timed_from": "2027-01-01T07:59:30+00:00",
            "start_time": "2027-01-01T07:59:30+00:00", "end_time": "2027-01-01T08:00:30+00:00",
            "recorded_until": "2027-01-01T08:00:30+00:00", "duration_seconds": 60,
            "summary_version": 3, "distance_miles": .497, "max_speed_mph": 22.4,
            "avg_speed_mph": 29.8, "num_points": 3,
            "bbox": {"min_lat": 0, "max_lat": 0, "min_lon": 0, "max_lon": 800 * 180 / (math.pi * 6371000)},
            "_end_dt": self.CUT + timedelta(seconds=30),
            "_identity_dt": self.CUT - timedelta(seconds=30),
        }
        self.assertEqual(self.summary.summarize(parsed), expected)
        before, after = self.viewer.distance_sides(parsed, self.CUT)
        self.assertAlmostEqual(before / (before + after), 3 / 8)
        self.assertEqual(self.summary.summarize(parsed), expected)

    def test_naive_summary_end_uses_the_existing_utc_convention(self):
        key = self.recording(end_time="2027-01-01T08:00:30")
        self.assertEqual(self.trip(key).get("year_shares"), {2026: 3.686, 2027: 6.144})

    def test_non_increasing_counted_step_times_stay_whole_without_guessing(self):
        key = self.recording(segments=[[(-30, 0, 10, 1), (10, 400, 10, 1),
                                        (5, 500, 10, 1), (30, 800, 10, 1)]])
        self.assertEqual(self.trip(key)["distance_miles"], 9.83)
        self.assertIn("invalid", self.trip(key).get("year_split_note", ""))

    def test_incomplete_summary_timing_stays_whole_with_a_note(self):
        for status in ("partial", "untimed", None):
            with self.subTest(status=status):
                fields = {"start_time": None} if status == "partial" else {}
                key = self.recording(str(status), timing_status=status, **fields)
                trip = self.trip(key)
                self.assertNotIn("year_shares", trip)
                self.assertEqual(trip["distance_miles"], 9.83)
                self.assertIn("complete timing", trip.get("year_split_note", ""))
                self.assertIn("kept whole in 2026", self.admin.get(f"/trip/{key}").get_data(as_text=True))

    def test_partial_gpx_cannot_use_a_complete_but_stale_summary(self):
        key = self.recording(segments=[[(-30, 0, 10, 1), (None, 200, 10, 1), (30, 600, 10, 1)]],
                             timing_status="complete", start_time="2027-01-01T07:59:30+00:00",
                             end_time="2027-01-01T08:00:30+00:00")
        self.assertEqual(self.trip(key)["distance_miles"], 9.83)
        self.assertIn("invalid", self.trip(key).get("year_split_note", ""))

    def test_wholly_untimed_trip_stays_under_all_years_without_guessing(self):
        key = self.recording(segments=[[(None, 0, 10, 1), (None, 100, 10, 1)]])
        path = self.data / (key + ".json")
        data = json.loads(path.read_text())
        data.pop("start_time", None)
        path.write_text(json.dumps(data))
        self.assertEqual(self.figures(2026)["trips"], 0)
        self.assertEqual(self.figures(2027)["trips"], 0)
        self.assertIn("shares are unknown", self.admin.get(f"/trip/{key}").get_data(as_text=True))
        self.assertIn("shares are unknown", self.review(2026))

    def test_missing_invalid_or_unreadable_gpx_stays_whole_with_a_note(self):
        for name, raw in (("missing", None), ("invalid", b"<broken"), ("empty", b"<gpx/>"),
                          ("doctype", b'<!DOCTYPE gpx><gpx/>')):
            with self.subTest(name=name):
                key = self.recording(name)
                path = self.data / (key + ".gpx")
                path.unlink() if raw is None else path.write_bytes(raw)
                trip = self.trip(key)
                self.assertEqual(trip["distance_miles"], 9.83)
                self.assertIn("kept whole in 2026", trip.get("year_split_note", ""))
                self.assertIn(key, self.review(2027))
        key = self.recording("unreadable")
        with patch.object(self.viewer.trip_merges, "parsed_parts", side_effect=PermissionError):
            self.assertIn("unreadable", self.trip(key).get("year_split_note", ""))

    def test_more_than_one_year_end_and_a_rounded_zero_share_stay_whole(self):
        key = self.recording("long", recorded_until="2028-01-01T08:00:30+00:00")
        self.assertIn("more than one year end", self.trip(key).get("year_split_note", ""))
        key = self.recording("tiny", miles=0.001)
        self.assertIn("rounds to zero", self.trip(key).get("year_split_note", ""))
        self.assertEqual(self.figures(2027)["trips"], 0)

    def test_cache_rechecks_sources_and_uses_current_total(self):
        key = self.recording()
        with patch.object(self.viewer.trip_merges, "parsed_parts", wraps=self.viewer.trip_merges.parsed_parts) as read:
            self.assertIn("year_shares", self.trip(key))
            self.trip(key)
            self.assertEqual(read.call_count, 1)
            self.annotate(key, category="business")
            self.assertEqual(self.trip(key)["category"], "business")
            self.assertEqual(read.call_count, 1)
            path = self.data / (key + ".json")
            data = json.loads(path.read_text())
            data["distance_miles"] = 8
            path.write_text(json.dumps(data))
            self.assertEqual(self.trip(key)["year_shares"], {2026: 3, 2027: 5})
            self.assertEqual(read.call_count, 2)
            path = self.data / (key + ".gpx")
            stamp = path.stat()
            # A same-size edit changes the timed proportion; force a new ns stamp.
            path.write_bytes(path.read_bytes().replace(b"08:00:10", b"08:00:20"))
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
            self.assertEqual(self.trip(key)["year_shares"], {2026: 2.4, 2027: 5.6})
            self.assertEqual(read.call_count, 3)
        path.unlink()
        self.assertIn("missing", self.trip(key).get("year_split_note", ""))

    def test_refused_gpx_does_not_reuse_cached_shares_or_read_outside(self):
        key = self.recording()
        self.assertIn("year_shares", self.trip(key))
        path = self.data / (key + ".gpx")
        checked_path = self.viewer.trip_merges.checked_path

        def refuse_gpx(root, candidate, **kwargs):
            if candidate == path:
                raise ValueError("Invalid trip path")
            return checked_path(root, candidate, **kwargs)

        with patch.object(self.viewer.trip_merges, "checked_path", side_effect=refuse_gpx), \
                patch.object(self.viewer.trip_merges, "parsed_parts", side_effect=AssertionError("must refuse before reading")):
            trip = self.trip(key)
            self.assertNotIn("year_shares", trip)
            self.assertIn("cannot be opened safely", trip.get("year_split_note", ""))
            self.assertEqual(self.figures(2026)["totals"]["all"], 9.83)
            removed = self.post(f"/trip/{key}/remove", reason="Duplicate")
            self.assertEqual(removed.status_code, 302)
            self.assertEqual(removed.headers["Location"], "/?year=")
            restored = self.post(f"/trip/{key}/restore")
            self.assertEqual(restored.status_code, 302)
            self.assertEqual(restored.headers["Location"], "/?year=")

    def test_merged_track_uses_saved_total_and_part_that_itself_crosses_locks(self):
        first = self.recording()
        second = self.recording("second", [[(60, 900, 10, 1), (80, 1100, 10, 1)]])
        response = self.post(f"/trip/{first}/merge", next_trip=second)
        self.assertEqual(response.status_code, 302)
        merged = response.headers["Location"].split("/trip/")[1].split("?")[0]
        # Keep the GPX stamps, but emulate a summary saved under older rules.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            saved = json.loads(conn.execute("SELECT summary FROM trip_merges WHERE trip_key=?", (merged,)).fetchone()[0])
            saved["distance_miles"] = 11
            conn.execute("UPDATE trip_merges SET summary=? WHERE trip_key=?", (json.dumps(saved), merged))
        trip = self.trip(merged)
        self.assertEqual(trip["year_shares"], {2026: 3, 2027: 8})
        self.assertEqual(trip["trip_distance_miles"], 11)
        self.assertEqual(self.viewer._recording_years(first), {2026, 2027})
        self.close_year(2027)
        self.assertEqual(self.post(f"/trip/{merged}/split", confirm="yes").status_code, 409)
        self.assertEqual(self.annotate(merged, category="business").status_code, 409)

    def test_merge_with_parts_on_either_side_is_split(self):
        first = self.recording("first", [[(-40, 0, 10, 1), (-20, 200, 10, 1)]])
        second = self.recording("second", [[(20, 600, 10, 1), (40, 800, 10, 1)]])
        self.assertEqual(self.post(f"/trip/{first}/merge", next_trip=second).status_code, 302)
        trip = self.trip()
        self.assertEqual(trip["parts"], [first, second])
        self.assertEqual(set(trip["year_shares"]), {2026, 2027})
        self.assertEqual(sum(Decimal(str(s)) for s in trip["year_shares"].values()), Decimal(str(trip["distance_miles"])))

    def test_exports_keep_column_positions_and_append_full_miles_in_every_selection(self):
        self.recording()
        self.add_trip("ordinary", "2026-03-01T12:00:00Z")
        for query, expected in (("?year=2026", "3.686"), ("?year=2027", "6.144"), ("", "9.83")):
            with self.subTest(query=query):
                response = self.admin.get("/export.csv" + query)
                rows = list(csv.reader(io.StringIO(response.data.decode())))
                self.assertEqual(rows[0], self.COLUMNS + ["row_type", "received_after_close", "trip_distance_miles"])
                self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
                mapped = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
                crossing = next(r for r in mapped if r["trip_id"] == "crossing")
                self.assertEqual(crossing["distance_miles"], expected)
                self.assertEqual(crossing["trip_distance_miles"], "9.83")
                self.assertTrue(all(not r["trip_distance_miles"] for r in mapped if r is not crossing))
                sheet = self.sheet(query)["Mileage"]
                self.assertEqual(list(sheet[0]), self.COLUMNS + ["received_after_close", "trip_distance_miles"])
                crossing = next(r for r in sheet[1:] if r[1] == "crossing")
                self.assertEqual(crossing[3], float(expected))
                self.assertEqual(crossing[-1], 9.83)
                self.assertTrue(all(row[-1] is None for row in sheet[1:] if row[1] != "crossing"))

    def test_closing_either_year_locks_annotations_and_removal(self):
        key = self.recording()
        for year in (2026, 2027):
            self.close_year(year)
            for client in (self.admin, self.editor):
                self.assertEqual(self.annotate(key, client, category="business").status_code, 409)
            self.assertEqual(self.post(f"/trip/{key}/remove", reason="Duplicate").status_code, 409)
            self.assertIn(f"{year} is closed for edits", self.admin.get(f"/trip/{key}").get_data(as_text=True))
            self.reopen_year(year)

    def test_removal_restore_and_closed_year_snapshots_use_both_shares(self):
        key = self.recording()
        self.assertEqual(self.post(f"/trip/{key}/remove", reason="Duplicate").status_code, 302)
        self.assertEqual(self.figures(2026)["trips"], 0)
        self.assertEqual(self.figures(2027)["trips"], 0)
        for year in (2026, 2027):
            self.assertIn(key, self.review(year))
            self.close_year(year)
            self.assertEqual(self.post(f"/trip/{key}/restore").status_code, 409)
            self.reopen_year(year)
        self.assertEqual(self.post(f"/trip/{key}/restore").status_code, 302)
        for year, share in ((2026, 3.686), (2027, 6.144)):
            self.close_year(year)
            with closing(sqlite3.connect(self.db)) as conn:
                snapshot = json.loads(conn.execute("SELECT snapshot FROM tax_years WHERE year=?", (year,)).fetchone()[0])
            self.assertEqual(snapshot["trip_keys"], [key])
            self.assertEqual(snapshot["totals"]["all"], share)
            self.assertIn("The totals match", self.review(year))

    def test_old_full_miles_snapshot_differs_and_names_drive_without_false_departure(self):
        key = self.recording()
        trip = self.trip(key)
        old_snapshot = self.viewer.year_figures([{k: v for k, v in trip.items() if k != "year_shares"}], [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.viewer.trip_log.close_year(conn, 2026, "driver", old_snapshot)
        page = self.review(2026)
        self.assertIn("The totals now differ", page)
        self.assertIn("9.830", page)
        self.assertIn("3.686", page)
        self.assertIn(key, page.split('id="new-year-heading"')[1].split("</section>")[0])
        self.assertNotIn("No longer counted: " + key, page)
        self.assertNotIn("Now counted:", page)

    def test_late_flag_is_for_selected_year_and_all_years_checks_either_close(self):
        for closed_year in (2026, 2027):
            with self.subTest(closed_year=closed_year):
                self.close_year(closed_year)
                key = self.recording("late", _received_at="2030-01-01T12:00:00+00:00")
                for year in (2026, 2027, None):
                    query = f"?year={year}" if year else ""
                    expected = "yes" if year in (None, closed_year) else ""
                    row = next(r for r in self.csv_rows(query) if r["trip_id"] == "late")
                    self.assertEqual(row["received_after_close"], expected)
                    self.assertEqual(self.sheet(query)["Mileage"][1][-2], expected or None)
                self.assertIn("(1,", self.review(closed_year).split('id="after-close-heading"')[1])
                self.assertEqual(self.annotate(key, category="business").status_code, 409)
                self.reopen_year(closed_year)
                for suffix in (".json", ".gpx"):
                    (self.data / (key + suffix)).unlink()
