"""Viewer-owned retention setting and its disposable, ingest-readable mirror."""

import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone

from common.retention import DEFAULT_DAYS, SETTINGS_PATH, valid_days


KEY = "events_retention_days"
LOG = logging.getLogger(__name__)
WARNING = ("Diagnostics retention could not be handed to ingest. The saved setting is kept; "
           "check the settings volume and save again, or restart the viewer.")


class DiagnosticsSettings:
    def __init__(self, db_path, mirror_path=SETTINGS_PATH):
        self.db_path = db_path
        self.mirror_path = mirror_path
        self._mirror_error = None

    def _signature(self):
        try:
            stat = self.mirror_path.stat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @staticmethod
    def init_schema(db):
        db.execute("CREATE TABLE IF NOT EXISTS viewer_settings ("
                   "key TEXT PRIMARY KEY, value INTEGER NOT NULL, "
                   "updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)")

    @staticmethod
    def _current(db):
        row = db.execute("SELECT value,updated_by,updated_at FROM viewer_settings WHERE key=?", (KEY,)).fetchone()
        return dict(zip(("days", "updated_by", "updated_at"), row, strict=True)) if row else {
            "days": DEFAULT_DAYS, "updated_by": None, "updated_at": None}

    def current(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db:
            return self._current(db)

    def save(self, text, actor):
        if not re.fullmatch(r"[0-9]{1,4}", text) or not valid_days(int(text)):
            raise ValueError("Enter a whole number of days from 30 to 3650.")
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db, db:
            db.execute("INSERT INTO viewer_settings VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                       (KEY, int(text), actor, datetime.now(timezone.utc).isoformat()))
        return self.mirror()

    def mirror(self):
        """Serialize with saves/startups in every process and re-read the latest value.

        Unique temporary files plus replace keep ingest readers on a complete JSON
        file. The annotations writer lock prevents an older startup/save mirror
        from overwriting a later save. No diagnostics database is opened here.
        """
        temporary = None
        try:
            with closing(sqlite3.connect(self.db_path, timeout=30)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                days = self._current(db)["days"]
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".diagnostics-",
                                                 dir=self.mirror_path.parent, delete=False) as stream:
                    temporary = stream.name
                    json.dump({KEY: days}, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                # The images use the same UID; the viewer's private temp is
                # readable by ingest without making it world-writable.
                os.replace(temporary, self.mirror_path)
                temporary = None
                if hasattr(os, "O_DIRECTORY"):
                    fd = os.open(self.mirror_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            self._mirror_error = None
            return True
        except (OSError, sqlite3.Error) as exc:
            # A directory fsync can fail after replace succeeded. Keep that
            # durability warning until this file is successfully republished.
            self._mirror_error = self.mirror_path, self._signature()
            LOG.warning("Diagnostics settings mirror unavailable (%s)", type(exc).__name__)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def warning(self, days):
        if self._mirror_error == (self.mirror_path, self._signature()):
            return WARNING
        # Read the shared file, not a worker-local flag: another worker may have
        # repaired it, or this worker may not have observed a failed save.
        try:
            value = json.loads(self.mirror_path.read_text(encoding="utf-8"))
            if (isinstance(value, dict) and valid_days(value.get(KEY))
                    and value[KEY] == days):
                return None
        except (OSError, ValueError):
            pass
        return WARNING
