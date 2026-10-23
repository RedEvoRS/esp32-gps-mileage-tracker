"""Restore/refill audit repro with independent GELF and Loki event cursors."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from test_monitor import BOOT, monitor, store_module
from common.diagnostics import load_catalog


class CursorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = store_module.DiagnosticsStore(self.root, load_catalog())
        self.gelf_seen, self.loki_seen = [], []
        config = monitor.Config(enabled=True,
            graylog=monitor.gelf.GelfConfig(enabled=True, url="http://example.invalid/gelf"),
            loki=monitor.loki_module.LokiConfig(enabled=True, url="http://example.invalid/loki/api/v1/push"))
        def send_gelf(payloads):
            self.gelf_seen.extend(json.loads(p) for p in payloads)
            return len(payloads)
        def send_loki(payload):
            self.loki_seen.extend(json.loads(line) for stream in json.loads(payload)["streams"]
                                  for stamp, line in stream["values"])
        self.worker = monitor.Monitor(config, self.store.path, self.root / "monitor.db",
                                     gelf_sender=send_gelf, loki_sender=send_loki)

    def post(self, sequence):
        self.store.accept("tracker-1", {"schema_version": 1,
            "checkin": {"boot_id": BOOT, "status_sequence": sequence, "uptime_ms": sequence, "active": []},
            "events": [{"schema_version": 1, "catalog_version": 6, "firmware_version": "test",
                "boot_id": BOOT, "boot_counter": 1, "sequence": sequence, "code": 1000,
                "transition": "notice", "episode_id": None, "uptime_ms": sequence,
                "utc_ms": None, "time_source": "unknown", "sync_age_ms": None, "context": {}}]})

    def backup(self, source, target):
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)

    def test_restore_then_refill_to_old_cursor_replays_both_sinks(self):
        # Exact issue repro: save row 1, export through row 5, restore, then
        # refill rowids 2..5 with events 6..9 before the monitor sees the DB.
        for sequence in range(1, 6):
            self.post(sequence)
            if sequence == 1:
                self.backup(self.store.path, self.root / "saved.db")
        self.assertTrue(self.worker.tick())
        self.backup(self.root / "saved.db", self.store.path)
        for sequence in range(6, 10):
            self.post(sequence)
        for _ in range(3):
            self.assertTrue(self.worker.tick())
        for seen, kind, sequence in ((self.gelf_seen, "_kind", "_sequence"),
                                     (self.loki_seen, "kind", "sequence")):
            events = [row[sequence] for row in seen if row[kind] == "event"]
            self.assertEqual(events, [1, 2, 3, 4, 5, 1, 6, 7, 8, 9])

    def test_existing_v1_state_without_markers_replays_once_and_keeps_schema(self):
        self.post(1)
        self.assertTrue(self.worker.tick())
        with closing(sqlite3.connect(self.worker.state_path)) as db, db:
            old_tables = {name: sql for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
                          if name != "event_cursor_markers"}
            # Simulate an upgrade from an existing v1 state file.
            db.execute("DROP TABLE IF EXISTS event_cursor_markers")
        self.worker = monitor.Monitor(self.worker.config, self.store.path, self.worker.state_path,
                                     gelf_sender=self.worker.gelf_sender, loki_sender=self.worker.loki_sender)
        self.assertTrue(self.worker.tick())  # Reset unverifiable cursors.
        self.assertTrue(self.worker.tick())  # Replay.
        self.assertTrue(self.worker.tick())  # No second replay.
        self.assertEqual(len([e for e in self.gelf_seen if e["_kind"] == "event"]), 2)
        self.assertEqual(len([e for e in self.loki_seen if e["kind"] == "event"]), 2)
        with closing(sqlite3.connect(self.worker.state_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM schema_version").fetchall(), [(1, 1)])
            self.assertEqual({name: sql for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
                              if name != "event_cursor_markers"}, old_tables)


if __name__ == "__main__":
    unittest.main()
