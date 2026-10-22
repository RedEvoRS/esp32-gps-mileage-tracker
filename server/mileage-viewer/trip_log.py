"""Viewer-owned log records: which trips the admin removed from the log.

A removal is a row in annotations.db, never a change to ./data. The GPX and
JSON files stay exactly as ingest wrote them (and the viewer mounts ./data
read-only), so a removal can always be undone by deleting its row.
"""

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
