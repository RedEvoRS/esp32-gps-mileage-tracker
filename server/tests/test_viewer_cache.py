"""Summary cache identity, concurrent readers and merge-refresh transactions."""

import json
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_viewer_roles as roles
import test_viewer_merges as merges


class SummaryCacheTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in

    @property
    def path(self):
        return self.viewer.DATA_ROOT / "2026-01/drive.json"

    def test_cache_returns_copies_and_reads_only_changed_files(self):
        with patch.object(self.viewer, "read_summary", wraps=self.viewer.read_summary) as read:
            trips, _ = self.viewer.load_trips()
            trips[0]["distance_miles"] = 999
            trips[0]["category"] = "personal"
            trips, _ = self.viewer.load_trips()
            self.assertEqual(trips[0]["distance_miles"], 1)
            self.assertEqual(trips[0]["category"], "business")
            self.assertEqual(read.call_count, 1)
            # Annotation changes are never cached with the file summary.
            self.viewer.upsert_annotation("2026-01/drive", "other", "new note", "")
            self.assertEqual(self.viewer.load_trips()[0][0]["category"], "other")
            self.assertEqual(read.call_count, 1)

    def test_same_size_in_place_edit_uses_nanosecond_mtime(self):
        self.viewer.load_trips()
        stamp = self.path.stat()
        raw = self.path.read_text()
        updated = raw.replace('"distance_miles": 1', '"distance_miles": 2')
        self.assertNotEqual(raw, updated)
        self.path.write_text(updated)
        os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        self.assertEqual(self.path.stat().st_size, stamp.st_size)
        self.assertEqual(self.path.stat().st_ino, stamp.st_ino)
        self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 2)

    def test_atomic_replacement_uses_inode_even_with_identical_size_and_mtime(self):
        self.viewer.load_trips()
        stamp = self.path.stat()
        replacement = self.path.with_suffix(".tmp")
        replacement.write_text(self.path.read_text().replace('"distance_miles": 1', '"distance_miles": 3'))
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        replacement.replace(self.path)
        self.assertNotEqual(self.path.stat().st_ino, stamp.st_ino)
        self.assertEqual(self.path.stat().st_size, stamp.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp.st_mtime_ns)
        self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 3)

    def test_disappeared_file_and_month_are_evicted_new_month_is_discovered(self):
        self.viewer.load_trips()
        raw = self.path.read_bytes()
        self.path.unlink()
        self.path.with_suffix(".gpx").unlink()
        self.path.parent.rmdir()
        self.assertEqual(self.viewer.load_trips(), ([], []))
        self.assertEqual(self.viewer._summary_cache, {})
        month = self.viewer.DATA_ROOT / "2027-02"
        month.mkdir()
        (month / "new.json").write_bytes(raw)
        self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], "2027-02/new")
        self.assertEqual(set(self.viewer._summary_cache), {month / "new.json"})

    def test_missing_then_repaired_summary_and_gpx_presence_are_live(self):
        self.viewer.load_trips()
        raw = self.path.read_bytes()
        self.path.unlink()
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual(unreadable[0]["trip_key"], "2026-01/drive")
        self.assertNotIn(self.path, self.viewer._summary_cache)
        self.path.write_bytes(raw)
        self.assertTrue(self.viewer.load_trips()[0][0]["has_gpx"])
        self.path.with_suffix(".gpx").unlink()
        self.assertFalse(self.viewer.load_trips()[0][0]["has_gpx"])

    def test_bad_summary_is_cached_but_repair_is_reloaded(self):
        self.path.write_text("{broken")
        with patch.object(self.viewer, "read_summary", wraps=self.viewer.read_summary) as read:
            self.assertEqual(len(self.viewer.load_trips()[1]), 1)
            self.assertEqual(len(self.viewer.load_trips()[1]), 1)
            self.assertEqual(read.call_count, 1)
            self.path.write_text(json.dumps({"start_time": None, "distance_miles": 2}))
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 2)
            self.assertEqual(read.call_count, 2)

    def test_transient_read_failure_is_not_pinned_to_an_unchanged_stamp(self):
        stamp = self.path.stat()
        with patch.object(self.viewer, "read_summary", return_value=(None, "the file could not be read")):
            self.assertEqual(len(self.viewer.load_trips()[1]), 1)
        self.assertNotIn(self.path, self.viewer._summary_cache)
        self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 1)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp.st_mtime_ns)

    def test_replacement_during_parse_is_not_pinned_under_old_stamp(self):
        original_read = self.viewer.read_summary

        def replace_after_read(path):
            result = original_read(path)
            path.write_text(path.read_text().replace('"distance_miles": 1', '"distance_miles": 4'))
            stamp = path.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
            return result

        with patch.object(self.viewer, "read_summary", side_effect=replace_after_read):
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 1)
        self.assertNotIn(self.path, self.viewer._summary_cache)
        self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 4)

    def test_threads_share_parsing_but_not_mutable_results(self):
        def load(_):
            trips, _ = self.viewer.load_trips()
            value = trips[0]["distance_miles"]
            trips[0]["distance_miles"] = 999
            return value

        with patch.object(self.viewer, "read_summary", wraps=self.viewer.read_summary) as read:
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(load, range(24))), [1] * 24)
            self.assertEqual(read.call_count, 1)

    def test_reader_works_while_annotation_writer_holds_reserved_lock(self):
        self.viewer.load_trips()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE annotations SET category='other'")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.viewer.load_trips)
                # Always release the writer before shutting down the pool,
                # including if a regression leaves the reader blocked.
                try:
                    self.assertEqual(future.result(timeout=2)[0][0]["category"], "business")
                finally:
                    writer.rollback()

    def test_reader_does_not_reserve_writer_during_summary_io(self):
        original = self.viewer.read_summary

        def read(path):
            # A second connection can reserve the writer while this reader
            # holds its snapshot. In rollback journal mode its COMMIT can
            # still wait for the read snapshot; no WAL migration is required.
            with closing(sqlite3.connect(self.db, timeout=0.1)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE annotations SET category='other'")
                writer.rollback()
            return original(path)

        with patch.object(self.viewer, "read_summary", side_effect=read):
            self.assertEqual(len(self.viewer.load_trips()[0]), 1)

    def test_year_boundaries_and_global_year_menu_survive_cache_hits(self):
        data = json.loads(self.path.read_text())
        # Local New Year's Eve filed under the next January.
        data["start_time"] = "2026-01-01T00:30:00Z"
        self.path.write_text(json.dumps(data))
        self.viewer.load_trips()
        client = self.signed_in("admin")
        for _ in range(2):
            page = client.get("/?year=2025").get_data(as_text=True)
            self.assertIn("/trip/2026-01/drive", page)
            self.assertIn('<option value="2025"', page)
            self.assertNotIn("/trip/2026-01/drive", client.get("/?year=2026").get_data(as_text=True))
        # Changing display timezone uses cached UTC text, never a cached local
        # year. The folder selection cannot safely determine readable years.
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("Pacific/Kiritimati")):
            self.assertIn("/trip/2026-01/drive", client.get("/?year=2026").get_data(as_text=True))

    def test_year_selection_keeps_spanning_trips_rebuilt_dates_and_global_undated(self):
        data = json.loads(self.path.read_text())
        data.update(trip_id="long", gpx_file="2026-02/long.gpx",
                    start_time="2025-12-31T23:30:00Z", end_time="2026-02-01T00:30:00Z")
        # The end-month folder can be later than January for a long recording.
        folder = self.viewer.DATA_ROOT / "2026-02"
        folder.mkdir()
        long = folder / "long.json"
        long.write_text(json.dumps(data))
        untimed = self.path.with_name("untimed.json")
        untimed.write_text(json.dumps({"start_time": None, "distance_miles": 2}))
        client = self.signed_in("admin")
        self.assertIn("/trip/2026-02/long", client.get("/?year=2025").get_data(as_text=True))
        # A rebuilt stored summary can move to a year outside the folder's
        # heuristic; its actual time remains authoritative, even on a cache hit.
        data["start_time"] = "2024-12-31T23:30:00Z"
        long.write_text(json.dumps(data))
        self.assertIn("2026-02/long.gpx", client.get("/export.csv?year=2024").get_data(as_text=True))
        for year in (2024, 2025, 2026):
            page = client.get(f"/admin/year/{year}").get_data(as_text=True)
            self.assertIn(f"/trip/2026-01/untimed?year={year}", page)
        self.assertTrue(self.viewer._may_start_in("2026-01", 2025))
        self.assertTrue(self.viewer._may_start_in("2025-07", 2025))
        self.assertFalse(self.viewer._may_start_in("2026-02", 2025))

    def test_unknown_device_trip_and_removed_trip_use_only_requested_sources(self):
        data = json.loads(self.path.read_text())
        data["device_id"] = "unknown"
        self.path.write_text(json.dumps(data))
        other = self.path.with_name("other.json")
        other.write_text(json.dumps(data))
        client = self.signed_in("admin")
        with patch.object(self.viewer, "read_summary", wraps=self.viewer.read_summary) as read:
            self.assertEqual(client.get("/trip/2026-01/drive").status_code, 200)
            self.assertEqual([call.args[0] for call in read.call_args_list], [self.path])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.viewer.trip_log.add_removal(conn, "2026-01/drive", "test reason", "driver")
        with patch.object(self.viewer, "load_trips", side_effect=AssertionError("global scan")):
            self.assertEqual(client.get("/trip/2026-01/drive").status_code, 200)

    def test_targeted_reads_also_evict_unrelated_vanished_cache_entries(self):
        data = json.loads(self.path.read_text()) | {"device_id": "unknown"}
        self.path.write_text(json.dumps(data))
        other = self.path.with_name("other.json")
        other.write_text(json.dumps(data))
        self.viewer.load_trips()
        self.assertIn(other, self.viewer._summary_cache)
        other.unlink()
        with patch.object(self.viewer, "load_trips", side_effect=AssertionError("global scan")):
            self.assertEqual(self.signed_in("admin").get("/trip/2026-01/drive").status_code, 200)
        self.assertEqual(set(self.viewer._summary_cache), {self.path})

    def test_trip_missing_from_full_load_retries_once_then_returns_not_found(self):
        with patch.object(self.viewer, "load_trips", return_value=([], [])) as load, patch.object(
                self.viewer.app.logger, "warning") as warning:
            response = self.signed_in("admin").get("/trip/2026-01/drive?year=2026")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Location", response.headers)
        self.assertEqual(load.call_count, 2)
        warning.assert_called_once_with("Trip missing from full log after retry: %s", "2026-01/drive")

    def test_trip_missing_from_first_full_load_can_recover_on_one_retry(self):
        full = self.viewer.load_trips(include_removed=True)
        with patch.object(self.viewer, "load_trips", side_effect=[([], []), full]) as load, patch.object(
                self.viewer.app.logger, "warning") as warning:
            response = self.signed_in("admin").get("/trip/2026-01/drive?year=2026")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertEqual(load.call_count, 2)
        warning.assert_not_called()


class MergeCacheTransactionTests(unittest.TestCase):
    setUp = merges.ViewerMerges.setUp
    authenticate = merges.ViewerMerges.authenticate
    trip = merges.ViewerMerges.trip
    merge = merges.ViewerMerges.merge
    merged_key = merges.ViewerMerges.merged_key

    def make_merge(self):
        first, second = self.trip("a", 0), self.trip("b", 120)
        return first, second, self.merged_key(self.merge(first, second))

    def test_unchanged_merge_uses_only_read_transaction(self):
        self.make_merge()
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(self.viewer.sqlite3, "connect", side_effect=traced):
            self.assertEqual(len(self.viewer.load_trips()[0]), 1)
        self.assertIn("BEGIN", statements)
        self.assertNotIn("BEGIN IMMEDIATE", statements)
        self.assertFalse(any(sql.startswith("UPDATE trip_merges") for sql in statements))

    def test_changed_merge_restarts_snapshot_and_rechecks_under_write_lock(self):
        first, _, merged = self.make_merge()
        path = self.data / (first + ".json")
        data = json.loads(path.read_text())
        data["_received_at"] = "2026-04-15T00:00:00Z"
        path.write_text(json.dumps(data))
        modes = []
        apply = self.viewer.trip_merges.apply_merges

        def observe(conn, *args, **kwargs):
            modes.append(kwargs["refresh"])
            if not kwargs["refresh"]:
                # This state must be discarded when the snapshot restarts.
                with closing(sqlite3.connect(self.db)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.rollback()
            return apply(conn, *args, **kwargs)

        with patch.object(self.viewer.trip_merges, "apply_merges", side_effect=observe):
            trips, broken = self.viewer.load_trips()
        self.assertEqual(modes, [False, True])
        self.assertFalse(broken)
        self.assertEqual(trips[0]["trip_key"], merged)
        self.assertEqual(trips[0]["received_at"], data["_received_at"])

    def test_legacy_fingerprint_upgrades_without_recalculating_old_rules(self):
        first, second, merged = self.make_merge()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            summary, stamp = conn.execute("SELECT summary, fingerprint FROM trip_merges").fetchone()
            summary = json.loads(summary) | {"distance_miles": 123.0}
            legacy = [3, *(part[:3] for part in json.loads(stamp))]
            conn.execute("UPDATE trip_merges SET summary=?, fingerprint=?", (json.dumps(summary), json.dumps(legacy)))
        with patch.object(self.viewer.trip_merges, "combined_summary", side_effect=AssertionError("rules changed")):
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 123.0)
            self.assertEqual(self.viewer.load_trips()[0][0]["distance_miles"], 123.0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT fingerprint FROM trip_merges WHERE trip_key=?", (merged,)).fetchone()[0],
                             self.viewer.trip_merges.fingerprint(self.data, [first, second]))

    def test_merge_replacement_with_same_stamps_refreshes_and_missing_part_does_not_write(self):
        first, _, _ = self.make_merge()
        path = self.data / (first + ".gpx")
        stamp = path.stat()
        raw = path.read_bytes()
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        os.utime(temporary, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        temporary.replace(path)
        with patch.object(self.viewer.trip_merges, "combined_summary", wraps=self.viewer.trip_merges.combined_summary) as rebuild:
            self.viewer.load_trips()
            self.assertEqual(rebuild.call_count, 1)
        path.unlink()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.viewer.load_trips)
                try:
                    trips, broken = future.result(timeout=2)
                    self.assertFalse(trips)
                    self.assertEqual(len(broken), 1)
                finally:
                    writer.rollback()

    def test_two_readers_refresh_a_changed_merge_only_once(self):
        first, _, _ = self.make_merge()
        path = self.data / (first + ".json")
        data = json.loads(path.read_text()) | {"_received_at": "2026-04-15T00:00:00Z"}
        path.write_text(json.dumps(data))
        barrier = threading.Barrier(2)
        apply = self.viewer.trip_merges.apply_merges

        def synchronized(conn, *args, **kwargs):
            if not kwargs["refresh"]:
                barrier.wait(timeout=5)
            return apply(conn, *args, **kwargs)

        with patch.object(self.viewer.trip_merges, "apply_merges", side_effect=synchronized), patch.object(
                self.viewer.trip_merges, "combined_summary", wraps=self.viewer.trip_merges.combined_summary) as rebuild:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.viewer.load_trips(), range(2)))
            self.assertTrue(all(len(trips) == 1 and not broken for trips, broken in results))
            self.assertEqual(rebuild.call_count, 1)

    def test_refresh_rechecks_removal_and_annotations_after_releasing_read_snapshot(self):
        first, _, merged = self.make_merge()
        path = self.data / (first + ".json")
        path.write_text(json.dumps(json.loads(path.read_text()) | {"_received_at": "2026-04-15T00:00:00Z"}))
        connect = sqlite3.connect
        viewer, db = self.viewer, self.db
        injected = []

        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "BEGIN IMMEDIATE" and not injected:
                    with closing(connect(db)) as writer, writer:
                        writer.execute("BEGIN IMMEDIATE")
                        writer.execute("UPDATE trip_merges SET category='personal'")
                        viewer.trip_log.add_removal(writer, merged, "test reason", "driver")
                    injected.append(True)
                return super().execute(sql, *args, **kwargs)

        with patch.object(viewer.sqlite3, "connect", side_effect=lambda *a, **kw: connect(*a, factory=Connection, **kw)):
            trips, broken = viewer.load_trips(include_removed=True)
        self.assertEqual(injected, [True])
        self.assertFalse(broken)
        self.assertEqual(trips[0]["category"], "personal")
        self.assertEqual(trips[0]["removal"]["reason"], "test reason")
