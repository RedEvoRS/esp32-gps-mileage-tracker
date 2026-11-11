"""Output-independent condition evidence, event fields and delivery helpers."""

import hashlib
import json
import re
import sqlite3
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path

from common.diagnostics import validate_device_id
from settings_rules import host_valid


SNAPSHOT_MAX_AGE_SECONDS = 86400
SILENCE = 7001
FIELD = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


# Keep the public helper name while the standalone installer checker owns the rule.
valid_host = host_valid

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward configured credentials through a redirect.


def retry_delay(attempts):
    return min(3600, 60 * 2 ** min(attempts - 1, 6))


def condition_states(devices, known, now, stale_seconds,
                     snapshot_max_age=SNAPSHOT_MAX_AGE_SECONDS):
    """Return {(device, code): (state, episode, contact)} from current evidence.

    ``known`` supplies labels to retain as unknown when evidence disappears.
    Stored device-condition values only supply labels, never override an
    accepted snapshot. Catalog/notify filters and ordered ntfy resolutions
    belong to the consumers, not to this rule.
    """
    current = {key: ("unknown", None, None) for key in known}
    for device in devices:
        name = device.get("device_id")
        try:
            validate_device_id(name)
        except ValueError:
            continue
        persisted = device.get("conditions") or {}
        snapshot = device.get("snapshot")
        active = {item["code"]: item.get("episode_id") for item in snapshot.get("active", [])} if snapshot else {}
        observed = set(snapshot.get("observed", [])) if snapshot else set()
        for code in set(persisted) | observed | set(active) | {SILENCE}:
            current[(name, code)] = ("unknown", None, None)
        contact = device.get("contact")
        if contact is None:
            continue  # Damaged identity/contact cannot establish health.
        silent = now - contact >= stale_seconds
        current[(name, SILENCE)] = ("active" if silent else "clear",
                                   "silence:" + device.get("last_contact_at", str(contact)) if silent else None, contact)
        snapshot_at = device.get("snapshot_at")
        if snapshot is None or snapshot_at is None or not 0 <= now - snapshot_at <= snapshot_max_age:
            continue
        for code in observed | set(active):
            if code != SILENCE:  # Server silence always comes from contact age.
                current[(name, code)] = ("active", active[code], None) if code in active else ("clear", None, None)
    return current


def desired_conditions(devices, known, now, stale_seconds,
                       snapshot_max_age=SNAPSHOT_MAX_AGE_SECONDS):
    """Only state changes; unseen state starts unknown, episode changes alone do not send."""
    return {key: value for key, value in condition_states(
        devices, known, now, stale_seconds, snapshot_max_age).items()
        if value[0] != known.get(key, ("unknown", None))[0]}


def epoch(iso):
    stamp = datetime.fromisoformat(iso)
    if stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return stamp.timestamp()


def event_fields(codes, row):
    """Catalog descriptions and bounded stored event fields, or None for a damaged row.

    GELF retains its existing shape checks. Loki additionally validates the
    protocol and per-code context before calling this helper.
    """
    _, device, boot_id, sequence, code, received_at, canonical = row
    try:
        event = json.loads(canonical)
        epoch(received_at)
        if not isinstance(event, dict) or not isinstance(event.get("context", {}), dict):
            return None
        definition = codes.get(code)
        transition = event.get("transition")
        if transition not in ("raise", "repeat", "clear", "notice"):
            return None
        message = {"kind": "event", "device_id": device, "code": code,
                   "symbol": definition["symbol"] if definition else f"E{code}",
                   "severity": definition["severity"] if definition else "unknown",
                   "title": definition["title"] if definition else f"Unknown E{code}",
                   "transition": transition, "boot_id": boot_id, "sequence": sequence,
                   "received_at": received_at}
        for key, field, kind in (("episode_id", "episode_id", str), ("firmware_version", "firmware_version", str),
                                 ("time_source", "time_source", str), ("utc_ms", "device_utc_ms", int),
                                 ("sync_age_ms", "sync_age_ms", int), ("uptime_ms", "uptime_ms", int)):
            value = event.get(key)
            if type(value) is kind and (kind is int or len(value) <= 80):
                message[field] = value
        for key, value in event.get("context", {}).items():
            if not (isinstance(key, str) and FIELD.fullmatch(key) and len(key) <= 32):
                continue
            if type(value) is bool:
                message["ctx_" + key] = "true" if value else "false"
            elif type(value) is int or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,32}", value)):
                message["ctx_" + key] = value
        return message
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def checked_events_path(path):
    """Keep the configured <data root>/diagnostics/events.db inside its root.

    Derive the root before resolving the folder or file, so neither symlink
    can redefine the boundary. Custom paths used by tests follow the same rule.
    """
    path = Path(path)
    try:
        root = path.parent.parent.resolve()
        directory = path.parent.resolve()
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Unreadable diagnostics database path") from exc
    if any(root not in candidate.parents for candidate in (directory, resolved)):
        raise ValueError("Diagnostics database path escapes data root")
    return resolved


def read_events(path, after_rowid, limit):
    """Read rows and cursor identity from the same SQLite snapshot."""
    path = checked_events_path(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        highest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM events").fetchone()[0]
        rows = db.execute("SELECT rowid,device_id,boot_id,sequence,code,received_at,canonical_json "
                          "FROM events WHERE rowid>? ORDER BY rowid LIMIT ?", (after_rowid, limit)).fetchall()
        anchor = db.execute("SELECT rowid,device_id,boot_id,sequence,code,received_at,canonical_json "
                            "FROM events WHERE rowid=?", (after_rowid,)).fetchone()
        at_or_below = db.execute("SELECT 1 FROM events WHERE rowid<=? LIMIT 1", (after_rowid,)).fetchone()
        pruned = anchor is None and highest >= after_rowid and at_or_below is None
        return highest, rows, event_fingerprint(anchor) if anchor else None, pruned


def event_fingerprint(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=True).encode("ascii")).hexdigest()


def cursor_changed(db, sink, cursor, fingerprint, pruned=False):
    if not cursor:
        return False
    if pruned:
        # A stalled sink's whole delivered prefix aged out. Rows above its
        # cursor are exactly the unsent rows. Refresh the now-missing marker;
        # lower/refilled restores and holes inside retained history still reset.
        with db:
            save_cursor_marker(db, sink, cursor, "")
        return False
    stored = db.execute("SELECT event_rowid,fingerprint FROM event_cursor_markers WHERE sink=?",
                        (sink,)).fetchone()
    # Pre-marker monitor.db replays once rather than trusting an old cursor.
    return stored is None or tuple(stored) != (cursor, fingerprint)


def save_cursor_marker(db, sink, cursor, fingerprint):
    db.execute("INSERT INTO event_cursor_markers VALUES (?,?,?) ON CONFLICT(sink) DO UPDATE SET "
               "event_rowid=excluded.event_rowid,fingerprint=excluded.fingerprint",
               (sink, cursor, fingerprint))
