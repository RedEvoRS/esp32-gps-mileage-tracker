"""Ingest-owned diagnostics journal and ordered health snapshots.

Connections are request-local and always closed. BEGIN IMMEDIATE serializes
writers across processes, including first-use schema creation. No network or
JSON validation work is performed while holding the write lock.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS devices ("
    "device_id TEXT PRIMARY KEY, last_contact_at TEXT NOT NULL, "
    "status_sequence INTEGER, snapshot_json TEXT, snapshot_received_at TEXT)",
    "CREATE TABLE IF NOT EXISTS boots ("
    "device_id TEXT NOT NULL REFERENCES devices(device_id), boot_id TEXT NOT NULL, "
    "first_received_at TEXT NOT NULL, last_received_at TEXT NOT NULL, "
    "PRIMARY KEY(device_id, boot_id))",
    "CREATE TABLE IF NOT EXISTS events ("
    "device_id TEXT NOT NULL, boot_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
    "code INTEGER NOT NULL, severity TEXT, utc_ms INTEGER, "
    "received_at TEXT NOT NULL, canonical_json TEXT NOT NULL, "
    "PRIMARY KEY(device_id, boot_id, sequence), "
    "FOREIGN KEY(device_id, boot_id) REFERENCES boots(device_id, boot_id))",
    "CREATE INDEX IF NOT EXISTS events_history ON events(device_id, received_at, boot_id, sequence)",
    "CREATE INDEX IF NOT EXISTS events_code ON events(device_id, code, severity)",
    "CREATE TABLE IF NOT EXISTS device_conditions ("
    "device_id TEXT NOT NULL REFERENCES devices(device_id), code INTEGER NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('active','clear','unknown')), episode_id TEXT, "
    "PRIMARY KEY(device_id, code))",
)


class ConflictError(ValueError):
    """A previously committed identity/revision has different content."""


def canonical(value):
    """Compare decoded content independently of whitespace and object key order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


class DiagnosticsStore:
    def __init__(self, data_root, catalog):
        self.path = Path(data_root) / "diagnostics" / "events.db"
        self.definitions = {row["code"]: row for row in catalog["codes"]}

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
        except Exception:
            db.close()
            raise
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            # Initialization is lazy so a broken diagnostics disk never stops
            # GPX service startup. DDL and versioning share the writer lock.
            db.execute(SCHEMA[0])
            version = db.execute("SELECT version FROM schema_version WHERE singleton=1").fetchone()
            if version is None:
                for statement in SCHEMA[1:]:
                    db.execute(statement)
                db.execute("INSERT INTO schema_version VALUES (1, ?)", (SCHEMA_VERSION,))
            elif version[0] != SCHEMA_VERSION:
                raise RuntimeError("unsupported diagnostics database schema version")
            yield db
            db.commit()  # No acknowledgment can escape until this succeeds.
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _contact(db, device_id, received_at):
        db.execute(
            "INSERT INTO devices(device_id, last_contact_at) VALUES (?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "last_contact_at=MAX(last_contact_at, excluded.last_contact_at)",
            (device_id, received_at))

    def refresh_contact(self, device_id):
        with self._transaction() as db:
            self._contact(db, device_id, datetime.now(timezone.utc).isoformat())

    def accept(self, device_id, body):
        snapshot = body["checkin"]
        snapshot_json = canonical(snapshot)
        events = [(event, canonical(event)) for event in body["events"]]
        accepted = [{"boot_id": event["boot_id"], "sequence": event["sequence"]}
                    for event, _ in events]
        with self._transaction() as db:
            received_at = datetime.now(timezone.utc).isoformat()
            self._contact(db, device_id, received_at)
            revision, previous = db.execute(
                "SELECT status_sequence, snapshot_json FROM devices WHERE device_id=?",
                (device_id,)).fetchone()
            if revision == snapshot["status_sequence"] and previous != snapshot_json:
                raise ConflictError("status_sequence conflicts with stored snapshot")

            boot_ids = {snapshot["boot_id"]} | {event["boot_id"] for event, _ in events}
            for boot_id in boot_ids:
                db.execute(
                    "INSERT INTO boots VALUES (?, ?, ?, ?) ON CONFLICT(device_id, boot_id) "
                    "DO UPDATE SET last_received_at=excluded.last_received_at",
                    (device_id, boot_id, received_at, received_at))
            for event, encoded in events:
                identity = (device_id, event["boot_id"], event["sequence"])
                existing = db.execute(
                    "SELECT canonical_json FROM events WHERE device_id=? AND boot_id=? AND sequence=?",
                    identity).fetchone()
                if existing:
                    if existing[0] != encoded:
                        raise ConflictError("event identity conflicts with stored content")
                    continue
                definition = self.definitions.get(event["code"])
                db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (*identity, event["code"], definition["severity"] if definition else None,
                            event["utc_ms"], received_at, encoded))

            if revision is None or snapshot["status_sequence"] > revision:
                db.execute(
                    "UPDATE devices SET status_sequence=?, snapshot_json=?, snapshot_received_at=? "
                    "WHERE device_id=?",
                    (snapshot["status_sequence"], snapshot_json, received_at, device_id))
                # An omitted condition is unknown, including across reboot.
                # Journal clear records never alter health on their own.
                db.execute("UPDATE device_conditions SET state='unknown', episode_id=NULL "
                           "WHERE device_id=?", (device_id,))
                active = {fault["code"]: fault["episode_id"] for fault in snapshot["active"]}
                for code in set(snapshot.get("observed", [])) | set(active):
                    db.execute(
                        "INSERT INTO device_conditions VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(device_id, code) DO UPDATE SET "
                        "state=excluded.state, episode_id=excluded.episode_id",
                        (device_id, code, "active" if code in active else "clear", active.get(code)))
        return accepted
