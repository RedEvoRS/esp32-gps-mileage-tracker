"""Viewer-owned log records for removals and changes to the trip log.

A removal is a row in annotations.db, never a change to ./data. The GPX and
JSON files stay exactly as ingest wrote them (and the viewer mounts ./data
read-only), so a removal can always be undone by deleting its row. History
rows share the same database and transaction as the change they describe.
"""

import json
from datetime import datetime, timezone


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def init_schema(conn):
    # Caller holds BEGIN IMMEDIATE, including during concurrent worker startup.
    # The key is always the canonical key the trip list shows: a merge key for
    # a merged trip, the original key otherwise.
    conn.execute("""CREATE TABLE IF NOT EXISTS trip_removals (
        trip_key TEXT PRIMARY KEY, reason TEXT NOT NULL,
        removed_by TEXT NOT NULL, removed_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trip_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trip_key TEXT NOT NULL,
        actor TEXT NOT NULL, actor_role TEXT NOT NULL, at TEXT NOT NULL,
        action TEXT NOT NULL, details TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS trip_history_trip_at ON trip_history (trip_key, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS trip_history_at ON trip_history (at)")


def removal(conn, key):
    """The removal record for one canonical key, or None."""
    row = conn.execute(
        "SELECT reason, removed_by, removed_at FROM trip_removals WHERE trip_key=?", (key,)
    ).fetchone()
    return dict(zip(("reason", "removed_by", "removed_at"), row)) if row else None


def removals(conn):
    """Every removal, keyed by canonical trip key."""
    return {r[0]: {"reason": r[1], "removed_by": r[2], "removed_at": r[3]}
            for r in conn.execute(
                "SELECT trip_key, reason, removed_by, removed_at FROM trip_removals")}


def add_removal(conn, key, reason, removed_by):
    conn.execute(
        "INSERT INTO trip_removals (trip_key, reason, removed_by, removed_at) VALUES (?, ?, ?, ?)",
        (key, reason, removed_by, now_utc()))


def delete_removal(conn, key):
    conn.execute("DELETE FROM trip_removals WHERE trip_key=?", (key,))


def record(conn, trip_key, action, details, *, actor, actor_role):
    """Write one change in the caller's transaction, with caller identity."""
    conn.execute(
        """INSERT INTO trip_history (trip_key, actor, actor_role, at, action, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trip_key, actor, actor_role, now_utc(), action,
         json.dumps(details, ensure_ascii=False, sort_keys=True)),
    )
