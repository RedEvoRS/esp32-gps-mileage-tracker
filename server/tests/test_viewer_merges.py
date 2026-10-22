"""Synthetic split drives exercise persisted merges through the viewer routes."""

import builtins
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from test_round_trip import load_service, SERVER


class ViewerMerges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.month = self.data / "2026-04"
        self.month.mkdir(parents=True)
        self.db = self.root / "viewer" / "annotations.db"
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(self.data), "VIEWER_DB_PATH": str(self.db),
            "VIEWER_USERNAME": "driver", "VIEWER_PASSWORD_HASH": generate_password_hash("test-password"),
            "VIEWER_SECRET_KEY": "merge-test-session-key-" * 3, "DISPLAY_TZ": "UTC",
            "VIEWER_GEOCODER_URL": "",
        }):
            self.viewer = load_service("mileage-viewer")
        self.client = self.viewer.app.test_client()
        self.authenticate(self.client)
        self.base = datetime(2026, 4, 14, 12, tzinfo=timezone.utc)

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(user="driver", pw_tag=self.viewer._password_tag(), csrf="test-csrf")

    def trip(self, name, seconds, offset=0, device="test-device", legacy=False):
        from common.trip_summary import parse_gpx, summarize
        # Equatorial ocean fixture: no real trip/location data.
        points = []
        for i, speed in ((0, 10), (30, 10), (60, 0)):
            points.append(f'<trkpt lat="0" lon="{offset + i * .00001}">'
                          f'<time>{(self.base + timedelta(seconds=seconds + i)).isoformat()}</time>'
                          f'<speed>{speed}</speed><hdop>1</hdop></trkpt>')
        raw = ('<gpx><trk><trkseg>' + ''.join(points) + '</trkseg></trk></gpx>').encode()
        summary = {k: v for k, v in summarize(parse_gpx(raw)).items() if not k.startswith("_")}
        summary.update(trip_id=name, device_id=device, gpx_file=f"2026-04/{name}.gpx")
        if legacy:
            summary.pop("recorded_until")
        (self.month / (name + ".gpx")).write_bytes(raw)
        (self.month / (name + ".json")).write_text(json.dumps(summary), encoding="utf-8")
        return f"2026-04/{name}"

    def merge(self, key, following, client=None):
        return (client or self.client).post(f"/trip/{key}/merge?year=2026", data={
            "csrf_token": "test-csrf", "next_trip": following,
        })

    def merged_key(self, response):
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers["Location"].split("/trip/")[1].split("?")[0]

    def annotate(self, key, category="", note="", destination=""):
        return self.client.post(f"/trip/{key}/annotate", data={
            "csrf_token": "test-csrf", "category": category,
            "purpose_note": note, "destination": destination,
        })

    def split(self, key):
        return self.client.post(f"/trip/{key}/split", data={"csrf_token": "test-csrf", "confirm": "yes"})

    def damage_summary(self, key, **fields):
        path = self.data / (key + ".json")
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.update(distance_miles="damaged", **fields)
        path.write_text(json.dumps(summary), encoding="utf-8")

    def assert_export_sources(self, row_type, sheet, trip_id, files):
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv").data.decode())))
        row = next(r for r in rows if r["row_type"] == row_type)
        self.assertEqual(row["trip_id"], trip_id)
        self.assertEqual(row["gpx_file"], files)
        book = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))
        try:
            headers, values = list(book[sheet].values)[:2]
            row = dict(zip(headers, values))
            self.assertEqual(row["trip_id"], trip_id)
            self.assertEqual(row["gpx_file"], files)
        finally:
            book.close()

    def test_merge_recomputes_summary_map_exports_totals_cache_and_undo(self):
        a = self.trip("a", 0)
        b = self.trip("b", 180, .01)
        self.annotate(b, "business", "Test purpose", "Test destination")
        original = {p.name: p.read_bytes() for p in self.month.iterdir()}
        parts, _ = self.viewer.load_trips()
        separate = sum(t["distance_miles"] for t in parts)
        self.assertIn("Merge with next trip", self.client.get(f"/trip/{a}").get_data(as_text=True))
        key = self.merged_key(self.merge(a, b))
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(len(trips), 1)
        self.assertFalse(unreadable)
        t = trips[0]
        from common.trip_summary import parse_gpx, summarize
        parsed = [parse_gpx((self.data / (k + ".gpx")).read_bytes()) for k in (a, b)]
        expected = summarize({"segments": [seg for p in parsed for seg in p["segments"]]})
        self.assertEqual(t["distance_miles"], expected["distance_miles"])
        self.assertGreater(t["distance_miles"], separate)
        self.assertEqual(t["duration_seconds"], 210)
        self.assertEqual(t["start_time"], self.base.isoformat())
        self.assertEqual(t["end_time"], (self.base + timedelta(seconds=210)).isoformat())
        self.assertEqual(t["purpose_note"], "Test purpose")
        self.assertEqual(t["trip_id"], "a")
        self.assertEqual(t["gpx_file"], f"{a}.gpx; {b}.gpx")
        for old in (a, b):
            r = self.client.get(f"/trip/{old}")
            self.assertEqual(r.status_code, 302)
            self.assertIn(key, r.headers["Location"])
            self.assertEqual(self.annotate(old, note="stale edit").status_code, 409)
        page = self.client.get(f"/trip/{key}").get_data(as_text=True)
        self.assertIn("Merged from 2 recordings", page)
        self.assertIn("<title>Trip a</title>", page)
        self.assertIn(f"{a}.gpx; {b}.gpx", page)
        self.assertIn(f"{a}.gpx; {b}.gpx", self.client.get("/").get_data(as_text=True))
        self.assertNotIn(key + ".gpx", page)
        self.assert_export_sources("trip", "Mileage", "a", f"{a}.gpx; {b}.gpx")
        self.assertIn("joinTrackSegments: false", page)
        raw = self.client.get(f"/gpx/{key}").data
        self.assertEqual(len(ET.fromstring(raw).findall("./trk/trkseg")), 2)
        self.assertEqual(len(ET.fromstring(self.client.get(f"/gpx/{b}").data).findall("./trk/trkseg")), 2)
        endpoints = self.viewer.track_endpoints(t)
        self.assertEqual(endpoints["start_longitude"], 0)
        self.assertAlmostEqual(endpoints["end_longitude"], .0106)
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv?year=2026").data.decode())))
        self.assertEqual(len([r for r in rows if r["row_type"] == "trip"]), 1)
        self.assertEqual(float(next(r for r in rows if r["row_type"] == "total" and r["category"] == "all")["distance_miles"]), t["distance_miles"])
        book = load_workbook(io.BytesIO(self.client.get("/export.xlsx?year=2026").data))
        self.addCleanup(book.close)
        self.assertEqual(book["Mileage"].max_row, 2)
        totals = {row[2]: row[3] for row in book["Totals"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(totals["business"], t["distance_miles"])
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("uncached")):
            self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.annotate(key, "personal", "Merged edit").status_code, 302)
        self.assertEqual(self.split(key).status_code, 302)
        trips, _ = self.viewer.load_trips()
        by_key = {t["trip_key"]: t for t in trips}
        self.assertEqual(set(by_key), {a, b})
        self.assertEqual(by_key[a]["purpose_note"], "")
        self.assertEqual(by_key[b]["purpose_note"], "Test purpose")
        self.assertEqual(by_key[b]["destination"], "Test destination")
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.month.iterdir()})

    def test_chain_and_old_merged_link_then_split_to_all_originals(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        self.annotate(a, "business", "Original A")
        ab = self.merged_key(self.merge(a, b))
        self.annotate(ab, "personal", "Merged AB")
        abc = self.merged_key(self.merge(ab, c))
        trips, _ = self.viewer.load_trips()
        self.assertEqual(trips[0]["parts"], [a, b, c])
        self.assertEqual(trips[0]["purpose_note"], "Merged AB")
        self.assertIn(abc, self.client.get(f"/trip/{ab}").headers["Location"])
        self.assertEqual(self.split(abc).status_code, 302)
        self.assertEqual(self.viewer.get_annotations()[a]["purpose_note"], "Original A")
        self.assertEqual(len(self.viewer.load_trips()[0]), 3)

    def test_merge_with_a_next_trip_that_is_already_merged(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        bc = self.merged_key(self.merge(b, c))
        abc = self.merged_key(self.merge(a, bc))
        self.assertEqual(self.viewer.load_trips()[0][0]["parts"], [a, b, c])
        self.assertIn(abc, self.client.get(f"/trip/{bc}").headers["Location"])
        self.assertEqual(self.split(abc).status_code, 302)

    def test_chained_export_sources_keep_order_across_months(self):
        self.base = datetime(2026, 4, 30, 23, 58, tzinfo=timezone.utc)
        a, b, c = self.trip("a", 0), self.trip("b", 120), self.trip("c", 240)
        next_month = self.data / "2026-05"
        next_month.mkdir()
        for key in (b, c):
            for suffix in (".gpx", ".json"):
                path = self.data / (key + suffix)
                path.rename(next_month / path.name)
        b, c = "2026-05/b", "2026-05/c"
        ab = self.merged_key(self.merge(a, b))
        abc = self.merged_key(self.merge(ab, c))
        files = f"{a}.gpx; {b}.gpx; {c}.gpx"
        self.assert_export_sources("trip", "Mileage", "a", files)
        self.assertIn(files, self.client.get(f"/trip/{abc}").get_data(as_text=True))
        self.assertIn(files, self.client.get("/").get_data(as_text=True))

    def test_annotations_conflict_requires_clearing_every_field(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        self.annotate(a, note="A text")
        self.annotate(b, destination="B text")
        page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn("Both trips have annotations", page)
        self.assertNotIn(">Merge with next trip</button>", page)
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.assertEqual(self.viewer.get_annotations()[a]["purpose_note"], "A text")
        self.annotate(a)
        key = self.merged_key(self.merge(a, b))
        self.assertEqual(self.viewer.load_trips()[0][0]["destination"], "B text")
        self.assertEqual(self.split(key).status_code, 302)

    def test_limit_boundary_legacy_fallback_and_refusals(self):
        a = self.trip("a", 0, legacy=True)
        b = self.trip("b", 931, .001)  # end_time=30: 901 seconds is too late
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("b", 930, .001)
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_recorded_until_not_last_movement_is_boundary(self):
        a = self.trip("a", 0)
        b = self.trip("b", 960, .001)  # exactly 15 minutes after final recorded point
        self.assertEqual(self.merge(a, b).status_code, 302)

    def test_different_device_nonadjacent_overlap_and_unknown_identity_refused(self):
        a, b, c = self.trip("a", 0), self.trip("b", 120, .001), self.trip("c", 240, .002)
        other = self.trip("other", 100, device="other-device")
        for target in (other, c, a, "../bad"):
            self.assertEqual(self.merge(a, target).status_code, 409)
        self.trip("b", 30, .001)
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("a", 0, device="")
        self.assertEqual(self.merge(a, b).status_code, 409)
        self.trip("a", 0, device="unknown")
        self.trip("b", 120, .001, device="unknown")
        self.assertEqual(self.merge(a, b).status_code, 409)

    def test_unknown_start_or_unreadable_summary_cannot_prove_adjacency(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        unknown = self.trip("unknown", 240)
        path = self.data / (unknown + ".json")
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary.update(start_time=None, timing_status="partial")
        path.write_text(json.dumps(summary), encoding="utf-8")
        self.assertEqual(self.merge(a, b).status_code, 409)
        path.write_text("[]", encoding="utf-8")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            self.assertEqual(self.merge(a, b).status_code, 409)
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn("an unreadable summary has an unknown device", page)
        self.assertNotIn("No eligible next trip", page)

    def test_damaged_summary_from_another_device_does_not_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        damaged = self.trip("damaged", 90, device="other-device")
        self.damage_summary(damaged)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
            self.assertIn(">Merge with next trip</button>", page)
            self.assertEqual(self.merge(a, b).status_code, 302)

    def test_same_device_damaged_summary_outside_window_does_not_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for name, seconds in (("before", -3600), ("after", 3600)):
            self.damage_summary(self.trip(name, seconds))
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
            self.assertIn(">Merge with next trip</button>", page)
            self.assertEqual(self.merge(a, b).status_code, 302)

    def test_same_device_damaged_summary_in_window_explains_block(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        # Include overlap and equal starts: none proves the damaged trip is
        # outside the chronological interval even if it starts before A ends.
        for seconds in (0, 30, 90, 120):
            with self.subTest(seconds=seconds):
                self.damage_summary(self.trip("damaged", seconds))
                with self.assertLogs(self.viewer.app.logger, "WARNING"):
                    page = self.client.get(f"/trip/{a}").get_data(as_text=True)
                    response = self.merge(a, b)
                reason = "an unreadable summary from this device may fall between these recordings"
                self.assertIn(reason, page)
                self.assertNotIn("No eligible next trip", page)
                self.assertNotIn(">Merge with next trip</button>", page)
                self.assertEqual(response.status_code, 409)
                self.assertIn(reason, response.get_data(as_text=True))

    def test_damaged_summary_timestamp_id_bounds_adjacency_across_months(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        old = self.trip("2026-03-14T12-00-00Z", -31 * 86400)
        self.damage_summary(old, start_time=None)
        prior_month = self.data / "2026-03"
        prior_month.mkdir()
        for suffix in (".gpx", ".json"):
            path = self.data / (old + suffix)
            path.rename(prior_month / path.name)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            page = self.client.get(f"/trip/{a}").get_data(as_text=True)
        self.assertIn(">Merge with next trip</button>", page)
        damaged = self.trip("2026-04-14T12-01-30Z", 90)
        self.damage_summary(damaged, start_time="invalid")
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            response = self.merge(a, b)
        self.assertEqual(response.status_code, 409)
        self.assertIn("may fall between these recordings", response.get_data(as_text=True))

    def test_same_device_damaged_summary_with_unusable_timing_blocks(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for name in ("legacy-id", "2026-02-30T12-00-00Z"):
            key = self.trip(name, 3600)
            self.damage_summary(key, start_time=None)
            with self.subTest(name=name), self.assertLogs(self.viewer.app.logger, "WARNING"):
                response = self.merge(a, b)
            self.assertEqual(response.status_code, 409)
            self.assertIn("may fall between these recordings", response.get_data(as_text=True))
            (self.data / (key + ".json")).unlink()
            (self.data / (key + ".gpx")).unlink()

    def test_unidentified_damaged_device_blocks_even_with_old_timestamp(self):
        a, b = self.trip("a", 0), self.trip("b", 120)
        for device in (None, "", "unknown", {"invalid": "device"}):
            key = self.trip("2026-03-14T12-00-00Z", -31 * 86400)
            self.damage_summary(key, device_id=device)
            with self.subTest(device=device), self.assertLogs(self.viewer.app.logger, "WARNING"):
                response = self.merge(a, b)
            self.assertEqual(response.status_code, 409)
            self.assertIn("unknown device", response.get_data(as_text=True))

    def test_malformed_tracks_are_refused_without_hiding_originals(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        for raw in (b"<gpx>", b"<gpx/>", b'<!DOCTYPE gpx [<!ENTITY x "text">]><gpx/>'):
            with self.subTest(raw=raw):
                (self.data / (b + ".gpx")).write_bytes(raw)
                self.assertEqual(self.merge(a, b).status_code, 409)
                self.assertEqual(len(self.viewer.load_trips()[0]), 2)

    def test_auth_csrf_and_explicit_undo_confirmation(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        anonymous = self.viewer.app.test_client()
        for suffix in ("merge", "split"):
            self.assertEqual(anonymous.post(f"/trip/{a}/{suffix}").status_code, 401)
            self.assertEqual(self.client.post(f"/trip/{a}/{suffix}").status_code, 400)
        key = self.merged_key(self.merge(a, b))
        self.assertEqual(self.client.post(f"/trip/{key}/split", data={"csrf_token": "test-csrf"}).status_code, 400)
        self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_cache_invalidates_and_missing_part_is_unreadable_but_undo_works(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        key = self.merged_key(self.merge(a, b))
        helper = self.viewer.trip_merges
        with patch.object(helper, "combined_summary", wraps=helper.combined_summary) as compute:
            self.trip("b", 120, .002)
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 1)
            self.viewer.load_trips()
            self.assertEqual(compute.call_count, 1)
        (self.data / (b + ".gpx")).unlink()
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], [key])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Undo merge (split back)", page)
        self.assertIn(f"{a}.gpx; {b}.gpx", page)
        self.assertNotIn(key + ".gpx", page)
        self.assert_export_sources("unreadable", "Unreadable trips", "a", f"{a}.gpx; {b}.gpx")
        self.assertEqual(self.split(key).status_code, 302)

    def test_old_schema_migrates_concurrently_preserving_rows(self):
        db = self.root / "old.db"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE annotations (trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO annotations VALUES ('2026-04/a', 'business', 'Original', 'old')")
        with patch.object(self.viewer, "DB_PATH", db):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.viewer.init_db(), range(8)))
            self.assertEqual(self.viewer.get_annotations()["2026-04/a"]["purpose_note"], "Original")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT destination FROM annotations").fetchone(), ("",))
            self.assertEqual(conn.execute("SELECT count(*) FROM trip_merges").fetchone(), (0,))

    def test_concurrent_merge_requests_only_create_one_merge(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        def attempt(_):
            client = self.viewer.app.test_client()
            self.authenticate(client)
            return self.merge(a, b, client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [302, 409])
        self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_read_only_data_mount_contract(self):
        a, b = self.trip("a", 0), self.trip("b", 120, .001)
        snapshot = {p: p.read_bytes() for p in self.month.iterdir()}
        original_open, original_path_open = builtins.open, Path.open
        def check(path, mode):
            if not isinstance(path, (str, os.PathLike)):
                return
            resolved = Path(path).resolve()
            if self.data.resolve() in resolved.parents and any(c in mode for c in "wax+"):
                raise AssertionError("Attempted write to read-only trip data")
        def guarded_open(path, mode="r", *args, **kwargs):
            check(path, mode)
            return original_open(path, mode, *args, **kwargs)
        def guarded_path_open(path, mode="r", *args, **kwargs):
            check(path, mode)
            return original_path_open(path, mode, *args, **kwargs)
        with patch("builtins.open", guarded_open), patch.object(Path, "open", guarded_path_open):
            key = self.merged_key(self.merge(a, b))
            for route in ("/", f"/trip/{key}", f"/gpx/{key}", "/export.csv", "/export.xlsx"):
                self.assertEqual(self.client.get(route).status_code, 200)
            self.annotate(key, note="Merged annotation")
            self.assertEqual(self.split(key).status_code, 302)
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.month.iterdir()})

    def test_shared_summary_ships_in_default_deny_build_context(self):
        dockerignore = (SERVER / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("\n**\n", dockerignore)
        self.assertIn("!common/trip_summary.py", dockerignore)
        self.assertIn("!mileage-viewer/trip_merges.py", dockerignore)
        for service in ("mileage-ingest", "mileage-viewer"):
            self.assertIn("COPY common/ /app/common/", (SERVER / service / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
