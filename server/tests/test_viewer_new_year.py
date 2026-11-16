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
from html.parser import HTMLParser
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

    def moved_saved_recordings(self, segments=None):
        annotated = self.recording('annotated', segments=segments)
        removed = self.recording('removed', segments=segments)
        self.assertEqual(self.annotate(annotated, category='business', purpose_note='Example purpose').status_code, 302)
        self.assertEqual(self.post(f'/trip/{removed}/remove', reason='Example duplicate').status_code, 302)
        (self.data / '2027-01').rename(self.data / '2027-02')
        return annotated, removed

    def keep_moved(self, year=2026):
        return self.post(f'/admin/year/{year}/moved-removal/2027-01/removed/restore', confirm='yes')

    def check_moved_saved_years(self, shares):
        old_annotation, old_removal = self.moved_saved_recordings(
            segments=self.MOVED if len(shares) == 1 else None)
        entry, = [e for e in self.viewer.load_trips()[1] if e.get('moved_saved')]
        for trip in entry['affected_trips']:
            self.assertEqual(trip['year_shares'], shares)
        for item in entry['saved_items']:
            self.assertEqual(item['trips'][0]['year_shares'], shares)
        for year in (2026, 2027):
            expected = 2 * shares.get(year, 0)
            self.assertEqual(self.figures(year)['totals']['all'], expected)
            self.assertEqual(self.figures(year)['totals']['unclassified'], expected)
            review = self.review(year)
            if year in shares:
                details = review.split('<caption>Saved items')[1].split('</table>')[0]
                for old_key in (old_annotation, old_removal):
                    self.assertIn(f'<code>{old_key}</code>', details)
                    self.assertIn(f'href="/trip/{old_key.replace("2027-01/", "2027-02/")}?year={year}"', details)
                self.assertEqual(self.post(f'/admin/year/{year}/close', confirm='yes').status_code, 409)
            else:
                self.assertNotIn('<caption>Saved items', review)
                self.close_year(year)
        self.assertEqual(self.annotate('2027-02/annotated', category='business').status_code, 302)
        before_keep = {year: self.figures(year) for year in (2026, 2027)}
        self.assertEqual(self.keep_moved(max(shares)).status_code, 302)
        self.assertFalse(any(e.get('moved_saved') for e in self.viewer.load_trips()[1]))
        for year in (2026, 2027):
            figures = self.figures(year)
            self.assertEqual(figures, before_keep[year])
            self.assertEqual(figures['totals']['all'], 2 * shares.get(year, 0))
            self.assertEqual(figures['totals']['business'], shares.get(year, 0))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(self.viewer.trip_log.removal(conn, old_removal))

    def test_moved_annotated_and_removed_split_drive_blocks_and_lists_both_years(self):
        self.check_moved_saved_years({2026: 3.686, 2027: 6.144})

    def test_moved_annotated_and_removed_whole_drive_affects_only_the_new_year(self):
        self.check_moved_saved_years({2027: 9.83})

    def test_keep_moved_split_and_old_restore_refuse_either_closed_counted_year(self):
        _, old_removal = self.moved_saved_recordings()
        before = {year: self.figures(year) for year in (2026, 2027)}
        for closed_years in ((2026,), (2027,), (2026, 2027)):
            with self.subTest(closed_years=closed_years):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    for year in closed_years:
                        self.viewer.trip_log.close_year(conn, year, 'driver')
                for review_year in (2026, 2027):
                    self.assertEqual(self.keep_moved(review_year).status_code, 409)
                    details = self.review(review_year).split('<caption>Saved items')[1].split('</table>')[0]
                    self.assertNotIn('>Keep trip in log</button>', details)
                    names = ' and '.join(str(y) for y in closed_years)
                    self.assertIn(f'Reopen {names} to keep this trip in the log.', details)
                self.assertEqual(self.post(f'/trip/{old_removal}/restore').status_code, 409)
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    self.assertIsNotNone(self.viewer.trip_log.removal(conn, old_removal))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM trip_history WHERE action='restore'").fetchone()[0], 0)
                    for year in closed_years:
                        self.viewer.trip_log.reopen_year(conn, year)
        self.assertEqual(self.keep_moved().status_code, 302)
        for year in (2026, 2027):
            self.assertEqual(self.figures(year), before[year])

    def test_old_restore_of_moved_whole_drive_locks_only_the_new_year(self):
        _, old_removal = self.moved_saved_recordings(segments=self.MOVED)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.viewer.trip_log.close_year(conn, 2027, 'driver')
        self.assertEqual(self.post(f'/trip/{old_removal}/restore').status_code, 409)
        self.assertEqual(self.keep_moved(2027).status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertIsNotNone(self.viewer.trip_log.removal(conn, old_removal))
            self.viewer.trip_log.reopen_year(conn, 2027)
            self.viewer.trip_log.close_year(conn, 2026, 'driver')
        self.assertEqual(self.post(f'/trip/{old_removal}/restore').status_code, 302)
        self.assertEqual((self.figures(2026)['totals']['all'], self.figures(2027)['totals']['all']), (0, 19.66))

    def test_moved_saved_drive_kept_whole_uses_its_start_year(self):
        old = self.recording(timing_status='partial', start_time=None)
        self.assertEqual(self.annotate(old, category='business').status_code, 302)
        (self.data / '2027-01').rename(self.data / '2027-02')
        trip = self.trip()
        self.assertNotIn('year_shares', trip)
        self.assertIn('kept whole in 2026', trip['year_split_note'])
        self.assertEqual(self.post('/admin/year/2026/close', confirm='yes').status_code, 409)
        self.assertIn('<caption>Saved items', self.review(2026))
        self.assertNotIn('<caption>Saved items', self.review(2027))
        self.close_year(2027)
        self.assertEqual((self.figures(2026)['totals']['all'], self.figures(2027)['totals']['all']), (9.83, 0))

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

    def test_year_picker_describes_local_time(self):
        page = self.admin.get("/").get_data(as_text=True)
        self.assertIn('Year (local time in America/Los_Angeles)</label>', page)

    def test_year_note_is_with_trip_facts_outside_the_form_map_grid(self):
        class Structure(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.note_parents, self.grid_children = [], [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                classes = attrs.get("class", "").split()
                if self.stack and "trip-layout" in self.stack[-1][1]:
                    self.grid_children.append(classes)
                if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
                    self.stack.append((tag, classes))

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index][0] == tag:
                        del self.stack[index:]
                        break

            def handle_data(self, text):
                if "2026: 3.686 mi; 2027: 6.144 mi" in text:
                    self.note_parents.append([c for _, classes in self.stack for c in classes])

        key = self.recording()
        for client in (self.admin, self.editor):
            parsed = Structure()
            parsed.feed(client.get(f"/trip/{key}").get_data(as_text=True))
            self.assertEqual(len(parsed.note_parents), 1)
            self.assertIn("trip-info", parsed.note_parents[0])
            self.assertNotIn("trip-layout", parsed.note_parents[0])
            self.assertEqual(parsed.grid_children, [["annotation-panel"], ["trip-map"]])

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
        for status in ("partial", None):
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
        trip = self.trip(key)
        self.assertNotIn("year_split_note", trip)
        self.assertNotIn("year_shares", trip)
        self.assertEqual(trip["distance_miles"], 9.83)
        self.assertEqual(self.viewer.trip_years(trip), set())
        self.assertEqual(self.viewer.year_distance_note(trip), "")
        for page in (self.admin.get("/").get_data(as_text=True),
                     self.admin.get(f"/trip/{key}").get_data(as_text=True), self.review(2026)):
            self.assertNotIn("shares are unknown", page)
            self.assertNotIn('id="new-year-heading"', page)
        undated = self.review(2026).split('id="undated-heading"')[1].split("</section>")[0]
        self.assertIn(key, undated)
        self.assertEqual([r["trip_id"] for r in self.csv_rows() if r["row_type"] == "trip"], ["crossing"])

    def test_untimed_status_never_adds_a_new_year_note_even_with_stale_dates(self):
        key = self.recording(timing_status="untimed")
        self.assertNotIn("year_split_note", self.trip(key))
        self.assertNotIn('id="new-year-heading"', self.review(2026))

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

    def test_more_than_one_year_end_stays_whole(self):
        key = self.recording("long", recorded_until="2028-01-01T08:00:30+00:00")
        self.assertIn("more than one year end", self.trip(key).get("year_split_note", ""))
        self.assertEqual(self.figures(2027)["trips"], 0)

    def test_a_share_that_rounds_to_zero_leaves_the_drive_whole_in_the_other_year(self):
        # 300 m before and 500 m after: 0.001 mi cannot be divided, and the larger side keeps it.
        late = self.recording("tiny-late", miles=0.001)
        self.assertEqual(self.trip(late)["year_shares"], {2027: 0.001})
        # 295 m before and 5 m after: the start year keeps it, exactly as an ordinary trip.
        early = self.recording("tiny-early", miles=0.001, segments=[[(-30, 0, 10, 1), (-1, 290, 10, 1), (1, 300, 10, 1)]])
        trip = self.trip(early)
        self.assertNotIn("year_shares", trip)
        self.assertNotIn("year_split_note", trip)
        self.assertEqual([t["trip_key"] for t in self.viewer.year_members(*self.viewer.load_trips(), 2026)[0]], [early])
        self.assertEqual([t["trip_key"] for t in self.viewer.year_members(*self.viewer.load_trips(), 2027)[0]], [late])

    def test_drive_with_every_counted_mile_after_midnight_counts_in_the_new_year(self):
        # Both fixes before midnight are above the HDOP limit, so the only counted step is after it.
        key = self.recording("late", miles=0.186,
                             segments=[[(-30, 0, 10, 6), (-10, 100, 10, 6), (10, 400, 10, 1), (40, 700, 10, 1)]])
        trip = self.trip(key)
        self.assertEqual(trip["year_shares"], {2027: 0.186})
        self.assertNotIn("trip_distance_miles", trip)  # moved whole, not split
        self.assertEqual(trip["year_split_note"], "Starts in 2026; all of its counted miles are in 2027.")
        self.assertEqual(self.figures(2026)["trips"], 0)
        self.assertEqual((self.figures(2027)["trips"], self.figures(2027)["totals"]["all"]), (1, 0.186))
        self.assertEqual(self.viewer.mileage_totals([trip])["all"], 0.186)
        member = self.viewer.year_members([trip], [], 2027)[0][0]
        self.assertNotIn("other_year", member)
        self.assertEqual(self.viewer.year_distance_note(member), trip["year_split_note"])
        page = self.admin.get("/").get_data(as_text=True)
        self.assertIn('<option value="2027"', page)
        self.assertNotIn('<option value="2026"', page)
        for year in (2026, 2027):  # the old year's review explains where the 31 December drive went
            self.assertIn(trip["year_split_note"], self.review(year))
        rows = list(csv.reader(io.StringIO(self.admin.get("/export.csv?year=2027").get_data(as_text=True))))
        row = next(r for r in rows if r[1] == "late")
        self.assertEqual((float(row[3]), row[-1]), (0.186, ""))
        # The lock follows the year that holds the miles.
        self.close_year(2026)
        self.assertEqual(self.annotate(key, category="business").status_code, 302)
        self.reopen_year(2026)
        self.close_year(2027)
        self.assertEqual(self.annotate(key, category="personal").status_code, 409)

    # Both fixes before midnight are above the HDOP limit: every counted metre is after it.
    MOVED = [[(-30, 0, 10, 6), (-10, 100, 10, 6), (10, 400, 10, 1), (40, 700, 10, 1)]]

    def test_moved_drive_in_xlsx_late_flag_navigation_and_trip_page(self):
        key = self.recording("late", miles=0.186, segments=self.MOVED, _received_at="2030-01-01T12:00:00+00:00")
        newer = self.add_trip("later", "2027-01-02T12:00:00Z")
        row = next(r for r in self.sheet("?year=2027")["Mileage"][1:] if r[1] == "late")
        self.assertEqual((row[3], row[-1]), (0.186, None))
        self.assertFalse(any(r[1] == "late" for r in self.sheet("?year=2026")["Mileage"][1:]))
        self.assertIn(f'href="/trip/{key}?year=2027"', self.admin.get(f"/trip/{newer}?year=2027").get_data(as_text=True))
        self.assertIn("all of its counted miles are in 2027", self.admin.get(f"/trip/{key}").get_data(as_text=True))
        # It is late only for the year that counts it.
        self.close_year(2026)
        self.assertEqual(next(r for r in self.csv_rows() if r["trip_id"] == "late")["received_after_close"], "")
        self.reopen_year(2026)
        self.close_year(2027)
        for query in ("", "?year=2027"):
            self.assertEqual(next(r for r in self.csv_rows(query) if r["trip_id"] == "late")["received_after_close"], "yes")

    def test_merged_drive_with_every_counted_mile_after_midnight_counts_in_the_new_year(self):
        first = self.recording("first", [[(-40, 0, 10, 6), (-20, 200, 10, 6)]])
        second = self.recording("second", [[(20, 600, 10, 1), (40, 800, 10, 1)]])
        self.assertEqual(self.post(f"/trip/{first}/merge", next_trip=second).status_code, 302)
        trip = self.trip()
        self.assertEqual(trip["parts"], [first, second])
        self.assertGreater(trip["distance_miles"], 0)
        self.assertEqual(trip["year_shares"], {2027: trip["distance_miles"]})
        self.assertEqual((self.figures(2026)["trips"], self.figures(2026)["totals"]["all"]), (0, 0))
        self.assertEqual((self.figures(2027)["trips"], self.figures(2027)["totals"]["all"]), (1, trip["distance_miles"]))

    def test_zero_saved_total_stays_an_ordinary_start_year_trip(self):
        trip = self.trip(self.recording("zero", miles=0))
        self.assertNotIn("year_shares", trip)
        self.assertNotIn("year_split_note", trip)
        self.assertEqual((self.figures(2026)["trips"], self.figures(2027)["trips"]), (1, 0))

    def test_year_distance_cache_is_bounded(self):
        key = self.recording()
        cache = self.viewer._year_distance_cache
        cache.clear()
        cache.update({("filler", number): None for number in range(256)})
        self.assertEqual(self.trip(key)["year_shares"], {2026: 3.686, 2027: 6.144})
        self.assertEqual(len(cache), 1)

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

    def test_refused_gpx_crossing_and_ordinary_trips_are_unreadable_without_miles(self):
        crossing = self.recording()
        ordinary = self.recording("ordinary", [[(10, 0, 10, 1), (40, 300, 10, 1)]])
        self.assertIn("year_shares", self.trip(crossing))
        paths = {self.data / (key + ".gpx") for key in (crossing, ordinary)}
        checked_path = self.viewer.trip_merges.checked_path

        def refuse_gpx(root, candidate, **kwargs):
            if candidate in paths:
                raise ValueError("Invalid trip path")
            return checked_path(root, candidate, **kwargs)

        with patch.object(self.viewer.trip_merges, "checked_path", side_effect=refuse_gpx), \
                patch.object(self.viewer.trip_merges, "parsed_parts", side_effect=AssertionError("must refuse before reading")):
            trips, unreadable = self.viewer.load_trips()
            self.assertEqual(trips, [])
            self.assertEqual({t["trip_key"] for t in unreadable}, {crossing, ordinary})
            for trip in unreadable:
                self.assertEqual((trip["problem"], trip["problem_file"], trip["refused_file"]),
                                 ("the file could not be read", "gpx", "gpx"))
                self.assertNotIn("distance_miles", trip)
                self.assertNotIn("year_shares", trip)
                self.assertNotIn("year_split_note", trip)
            for year in (2026, 2027):
                self.assertEqual(self.figures(year)["totals"]["all"], 0)
                self.assertEqual(self.figures(year)["unreadable"], 2)
                self.assertNotIn('id="new-year-heading"', self.review(year))
            rows = self.csv_rows()
            self.assertEqual({r["trip_id"] for r in rows if r["row_type"] == "unreadable"},
                             {"crossing", "ordinary"})
            self.assertFalse(any(r["row_type"] == "trip" for r in rows))
            for key in (crossing, ordinary):
                self.assertEqual(self.editor.get(f"/trip/{key}").status_code, 404)
                removed = self.post(f"/trip/{key}/remove", reason="Duplicate")
                self.assertEqual((removed.status_code, removed.headers["Location"]), (302, "/?year="))
                restored = self.post(f"/trip/{key}/restore")
                self.assertEqual((restored.status_code, restored.headers["Location"]), (302, "/admin/removed?year="))

    def test_merge_history_unreadable_january_part_keeps_saved_year_narrowing(self):
        key = self.add_broken("part", "2027-01")
        saved = {"start_time": "2027-01-15T12:00:00Z", "end_time": "2027-01-15T12:05:00Z"}
        # A loaded merge still counts while a subsequent part read fails.
        current = [{"trip_key": "2027-01/merged", "parts": [key], **saved}]
        for recorded in (key, "2026-12/part"):
            with self.subTest(recorded=recorded), closing(sqlite3.connect(self.db)) as conn:
                row = {"parsed_details": {"parts": [recorded], "saved_summary": saved}}
                with self.assertLogs(self.viewer.app.logger, "WARNING"):
                    self.viewer._merge_history_counted_parts(conn, [row], current)
                self.assertEqual(row["part_years"], {2027})

    def test_merge_history_uses_loaded_ordinary_part_when_summary_has_no_year(self):
        for status in ("unreadable", "undated"):
            with self.subTest(status=status):
                key = self.add_broken(status, "2027-01") if status == "unreadable" else self.add_trip(
                    status, "2027-01-15T12:00:00Z", start_time=None, timing_status="untimed")
                saved = {"start_time": "2027-01-15T12:00:00Z", "end_time": "2027-01-15T12:05:00Z"}
                for loaded, expected in (({"start_time": "2026-12-31T12:00:00Z"}, {2026}), ({}, {2027})):
                    row = {"parsed_details": {"parts": [key], "saved_summary": saved}}
                    with closing(sqlite3.connect(self.db)) as conn:
                        self.viewer._merge_history_counted_parts(conn, [row], [{"trip_key": key, **loaded}])
                    self.assertEqual(row["part_years"], expected)

    def test_merge_history_includes_both_years_only_for_a_split_part(self):
        key = self.recording()
        saved = {"start_time": "2027-02-01T12:00:00Z", "end_time": "2027-02-01T12:05:00Z"}
        for current in ([self.trip(key)], [{"trip_key": "2027-02/merged", "parts": [key]}]):
            row = {"parsed_details": {"parts": [key], "saved_summary": saved}}
            with closing(sqlite3.connect(self.db)) as conn:
                self.viewer._merge_history_counted_parts(conn, [row], current)
            self.assertEqual(row["part_years"], {2026, 2027})

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

    def test_unsplit_merge_keeps_late_flag_from_any_closed_part_year(self):
        first = self.recording("first", [[(-40, 0, 10, 1), (-20, 200, 10, 1)]],
                               _received_at="2030-01-01T12:00:00+00:00")
        second = self.recording("second", [[(20, 600, 10, 1), (40, 800, 10, 1)]])
        response = self.post(f"/trip/{first}/merge", next_trip=second)
        self.assertEqual(response.status_code, 302)
        merged = response.headers["Location"].split("/trip/")[1].split("?")[0]
        # Emulate an older saved total too small to yield two positive shares.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            saved = json.loads(conn.execute("SELECT summary FROM trip_merges WHERE trip_key=?", (merged,)).fetchone()[0])
            saved["distance_miles"] = .001
            conn.execute("UPDATE trip_merges SET summary=? WHERE trip_key=?", (json.dumps(saved), merged))
        self.assertNotIn("year_shares", self.trip(merged))
        self.close_year(2027)
        self.assertEqual(self.viewer.trip_years(self.trip(merged)), {2026, 2027})
        for query in ("?year=2026", ""):
            row = next(r for r in self.csv_rows(query) if r["row_type"] == "trip")
            self.assertEqual(row["received_after_close"], "yes")
            self.assertEqual(self.sheet(query)["Mileage"][1][-2], "yes")

    def test_unsplit_merge_exports_do_not_look_up_part_years_without_a_close(self):
        first = self.recording("first", [[(-90, 0, 10, 1), (-60, 300, 10, 1)]])
        second = self.recording("second", [[(-40, 400, 10, 1), (-20, 600, 10, 1)]])
        response = self.post(f"/trip/{first}/merge", next_trip=second)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("year_shares", self.trip())
        with patch.object(self.viewer, "_recording_years", side_effect=AssertionError("no closed years need part lookups")):
            for query in ("?year=2026", ""):
                row = next(r for r in self.csv_rows(query) if r["row_type"] == "trip")
                self.assertEqual(row["received_after_close"], "")
                header, row = self.sheet(query)["Mileage"][:2]
                self.assertIsNone(dict(zip(header, row, strict=True))["received_after_close"])
