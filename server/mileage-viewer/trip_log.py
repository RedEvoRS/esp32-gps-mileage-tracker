"""Viewer-owned log records for removals, closed years and changes to the trip log.

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
    # A row means the year is closed for edits; reopening deletes it. The
    # history rows (trip_key "year/<YYYY>") keep the record of who and when.
    # snapshot is the year's totals as JSON, taken at the close. It is NULL for
    # a year closed before snapshots existed, so the column is added in place.
    conn.execute("""CREATE TABLE IF NOT EXISTS tax_years (
        year INTEGER PRIMARY KEY, closed_by TEXT NOT NULL, closed_at TEXT NOT NULL,
        snapshot TEXT
    )""")
    if "snapshot" not in {row[1] for row in conn.execute("PRAGMA table_info(tax_years)")}:
        conn.execute("ALTER TABLE tax_years ADD COLUMN snapshot TEXT")


def removal(conn, key):
    """The removal record for one canonical key, or None."""
    row = conn.execute(
        "SELECT reason, removed_by, removed_at FROM trip_removals WHERE trip_key=?", (key,)
    ).fetchone()
    return dict(zip(("reason", "removed_by", "removed_at"), row, strict=True)) if row else None


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


def parse_utc(text):
    """An ISO-8601 stamp as an aware datetime (a naive one is UTC), or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def latest_stamp(stamps):
    """The latest of several ISO-8601 stamps, or None if none can be read."""
    parsed = [(parse_utc(s), s) for s in stamps]
    parsed = [pair for pair in parsed if pair[0]]
    return max(parsed)[1] if parsed else None


def _snapshot_dict(text):
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def closed_years(conn):
    """Every closed year, as {year: {"closed_by", "closed_at", "snapshot"}}.

    snapshot is the saved totals dict, or None for a year closed before
    snapshots existed (or whose saved text can't be read).
    """
    return {r[0]: {"closed_by": r[1], "closed_at": r[2], "snapshot": _snapshot_dict(r[3])}
            for r in conn.execute("SELECT year, closed_by, closed_at, snapshot FROM tax_years")}


def close_year(conn, year, closed_by, snapshot=None):
    conn.execute("INSERT INTO tax_years (year, closed_by, closed_at, snapshot) VALUES (?, ?, ?, ?)",
                 (year, closed_by, now_utc(),
                  json.dumps(snapshot, sort_keys=True) if snapshot is not None else None))


def reopen_year(conn, year):
    conn.execute("DELETE FROM tax_years WHERE year=?", (year,))


def record(conn, trip_key, action, details, *, actor, actor_role):
    """Write one change in the caller's transaction, with caller identity."""
    conn.execute(
        """INSERT INTO trip_history (trip_key, actor, actor_role, at, action, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trip_key, actor, actor_role, now_utc(), action,
         json.dumps(details, ensure_ascii=False, sort_keys=True)),
    )
