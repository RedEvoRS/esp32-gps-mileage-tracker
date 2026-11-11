"""Ingest-owned diagnostics journal and ordered health snapshots.

Connections are request-local and always closed. BEGIN IMMEDIATE serializes
writers across processes, including first-use schema creation. No network or
JSON validation work is performed while holding the write lock.
"""

import json
import logging
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from common.retention import DEFAULT_DAYS, SETTINGS_PATH, valid_days


SCHEMA_VERSION = 1
# The firmware saves +1 per drain opportunity (normally at most every 30 s).
# This permits about 347 days of continuous awake allocations without contact.
MAX_STATUS_STEP = 1_000_000
PRUNE_INTERVAL_SECONDS = 3600
PRUNE_BATCH_SIZE = 250
PRUNE_BUDGET_SECONDS = 2.0
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
        self.data_root = Path(data_root)
        self.path = self.data_root / "diagnostics" / "events.db"
        self.definitions = {row["code"]: row for row in catalog["codes"]}
        self.settings_path = SETTINGS_PATH
        self._settings_problems = set()
        self._last_prune = float("-inf")
        self._prune_pending = False
        self._prune_lock = Lock()

    def retention_days(self):
        problem = None
        try:
            with self.settings_path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                problem = "oversized file"
            else:
                value = json.loads(raw)
                if isinstance(value, dict) and valid_days(value.get("events_retention_days")):
                    return value["events_retention_days"]
                problem = "invalid days"
        except FileNotFoundError:
            return DEFAULT_DAYS
        except (ValueError, RecursionError):
            problem = "malformed JSON"
        except OSError as exc:
            problem = "unreadable file (" + type(exc).__name__ + ")"
        if problem not in self._settings_problems:
            self._settings_problems.add(problem)
            logging.getLogger(__name__).warning("Diagnostics retention: %s; using 365 days", problem)
        return DEFAULT_DAYS

    def prune_if_due(self):
        """Best effort; unfinished cleanup resumes next upload, failures wait an hour."""
        if not self._prune_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if not self._prune_pending and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
                return
            self._last_prune = now  # Failed attempts are throttled too.
            self._prune_pending = False  # Only a successful budget pause bypasses the throttle.
            self.prune(self.retention_days())
        finally:
            self._prune_lock.release()

    def prune(self, days):
        deadline = time.monotonic() + PRUNE_BUDGET_SECONDS
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        removed = 0
        more = False
        while time.monotonic() < deadline:
            with self._transaction() as db:
                # Receipt times are datetime.isoformat() UTC (+00:00), with
                # either six fractional digits or no fraction at exact seconds.
                # Preserve MAX(rowid): events has no AUTOINCREMENT, and sinks
                # rely on future inserts never reusing an exported rowid.
                result = db.execute(
                    "DELETE FROM events WHERE rowid IN (SELECT rowid FROM events "
                    "WHERE received_at<? AND rowid<(SELECT MAX(rowid) FROM events) "
                    "ORDER BY rowid LIMIT ?)", (cutoff, PRUNE_BATCH_SIZE))
                count = result.rowcount
            removed += count
            if count < PRUNE_BATCH_SIZE:
                break
            time.sleep(0.01)  # Yield between committed batches to waiting readers/writers.
        else:
            # A full last batch may have finished cleanup exactly at the budget.
            # Check before bypassing the hourly throttle on the next upload.
            with closing(self._connect()) as db:
                more = bool(db.execute(
                    "SELECT EXISTS(SELECT 1 FROM events WHERE received_at<? "
                    "AND rowid<(SELECT MAX(rowid) FROM events))", (cutoff,)).fetchone()[0])
        self._prune_pending = more
        logging.getLogger(__name__).info(
            "Diagnostics retention removed %s event row(s); more remain: %s", removed, more)
        return removed

    def _connect(self):
        # Resolve lazily: an unusable diagnostics path must not stop GPX startup.
        root = self._resolved_path(self.data_root)
        directory = self._resolved_path(self.path.parent)
        path = self._resolved_path(self.path)
        if any(root not in candidate.parents for candidate in (directory, path)):
            raise ValueError("Diagnostics database path escapes data root")
        directory.mkdir(parents=True, exist_ok=True)
        # Explicit creation mode also protects callers that import this store
        # independently of app.py's private umask. Existing files are tightened.
        fd = os.open(directory / self.path.name,
                     os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        # Python's sqlite3 does not expose SQLITE_OPEN_NOFOLLOW. Use the checked
        # path; this preflight does not defend against a concurrent local writer
        # replacing paths before SQLite reopens the file.
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _resolved_path(path):
        resolved = path.resolve()
        # Windows can retain the extended prefix when another worker has the
        # file open. Compare all resolved paths in the same namespace.
        if os.name == "nt" and str(resolved).startswith("\\\\?\\"):
            text = str(resolved)[4:]
            resolved = Path("\\\\" + text[4:] if text.startswith("UNC\\") else text)
        return resolved

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

    def reset_high_water(self, device_id, high_water, expected):
        """Explicit operator recovery; never retain health from the old revision."""
        if not 0 <= high_water < (1 << 63) - 1:
            raise ValueError("high-water must be between 0 and 9223372036854775806")
        with self._transaction() as db:
            row = db.execute("SELECT status_sequence FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if row is None or row[0] != expected:
                raise ConflictError("device high-water changed or device does not exist")
            db.execute("UPDATE devices SET status_sequence=?,snapshot_json=NULL,snapshot_received_at=NULL "
                       "WHERE device_id=?", (high_water, device_id))
            db.execute("UPDATE device_conditions SET state='unknown',episode_id=NULL WHERE device_id=?",
                       (device_id,))

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
            snapshot_allowed = snapshot["status_sequence"] <= (revision or 0) + MAX_STATUS_STEP
            if not snapshot_allowed:
                # 400 keeps retrying the same bad check-in; 409 permanently
                # suspends firmware revision authority. Ack durable events with
                # 200 while refusing only this health snapshot, like stale ones.
                logging.getLogger(__name__).warning("Ignored diagnostics snapshot: status_sequence step exceeds limit")

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

            if snapshot_allowed and (revision is None or snapshot["status_sequence"] > revision):
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
