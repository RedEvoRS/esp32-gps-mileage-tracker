"""Viewer-owned merge records; original files and annotations stay untouched."""

import json
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from datetime import datetime, timedelta, timezone

from common.trip_summary import parse_gpx, summarize, _parse_iso
import trip_log

MERGE_MAX_GAP = timedelta(minutes=15)
ANNOTATION_FIELDS = ("category", "purpose_note", "destination")


class MergeRefreshNeeded(Exception):
    """A read-only caller must restart under BEGIN IMMEDIATE and recheck."""


def init_schema(conn):
    # Caller holds BEGIN IMMEDIATE, including during concurrent worker startup.
    conn.execute("""CREATE TABLE IF NOT EXISTS trip_merges (
        trip_key TEXT PRIMARY KEY, summary TEXT NOT NULL, fingerprint TEXT NOT NULL,
        category TEXT, purpose_note TEXT, destination TEXT, updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trip_merge_parts (
        part_key TEXT PRIMARY KEY, merge_key TEXT NOT NULL, position INTEGER NOT NULL,
        UNIQUE(merge_key, position)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trip_merge_aliases (
        alias_key TEXT PRIMARY KEY, merge_key TEXT NOT NULL
    )""")


def parts(conn, key):
    return [r[0] for r in conn.execute(
        "SELECT part_key FROM trip_merge_parts WHERE merge_key=? ORDER BY position", (key,)
    )]


def canonical_key(conn, key):
    row = conn.execute("SELECT merge_key FROM trip_merge_parts WHERE part_key=?", (key,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT merge_key FROM trip_merge_aliases WHERE alias_key=?", (key,)).fetchone()
    return row[0] if row else key


def source_paths(data_root, keys):
    # Keys originate from validated trip paths, but validate persisted keys too.
    for key in keys:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}/[A-Za-z0-9_-]+", key):
            raise ValueError("Invalid merge part")
        path = (data_root / (key + ".gpx")).resolve()
        if data_root.resolve() not in path.parents:
            raise ValueError("Invalid merge part path")
        yield path


def fingerprint(data_root, keys):
    """The stamps of the source files a merged summary was built from.

    It deliberately leaves out SUMMARY_VERSION. Plain trips keep the summary
    ingest stored under the rules of the day, so a merged trip must too: a
    later change to the distance rules reaches merges only when a source
    file changes (or the merge is undone and redone), not on first view.
    """
    stamps = []
    for path in source_paths(data_root, keys):
        for source in (path, path.with_suffix(".json")):
            st = source.stat()
            stamps.append((source.name, st.st_mtime_ns, st.st_size, st.st_ino))
    return json.dumps(stamps)


def same_sources(fresh, stored):
    """Whether two fingerprints name the same source files and stamps.

    Rows saved before the rule version was dropped start with that number;
    it is ignored, so upgrading does not rebuild every merge under new rules.
    """
    def stamps(text):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, list):
            return None
        return value[1:] if value and isinstance(value[0], int) else value
    left, right = stamps(fresh), stamps(stored)
    if left is None or right is None or len(left) != len(right):
        return False
    # Upgrade older fingerprints without rebuilding under newer distance
    # rules. Once upgraded, an atomic replacement with identical size/mtime
    # also invalidates the merge. Keep the legacy version-prefix compatibility.
    return all(isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
               and len(a) in (3, 4) and len(b) in (3, 4)
               and a[:3] == b[:3] and (len(a) == 3 or len(b) == 3 or a[3] == b[3])
               for a, b in zip(left, right, strict=True))


def parsed_parts(data_root, keys):
    for path in source_paths(data_root, keys):
        raw = path.read_bytes()
        # Reject DTDs before ElementTree can expand entities. Ingest only writes
        # valid GPX, but older files or damaged storage must fail closed here.
        def reject_doctype(*args):
            raise ValueError("GPX document types are not supported")
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = reject_doctype
        try:
            parser.Parse(raw, True)
        except expat.ExpatError as exc:
            raise ValueError("Invalid GPX XML") from exc
        parsed = parse_gpx(raw)
        if not parsed["segments"]:
            raise ValueError("A merge part has no track points")
        yield parsed


def combined_summary(data_root, keys):
    segments = [seg for parsed in parsed_parts(data_root, keys) for seg in parsed["segments"]]
    result = summarize({"segments": segments})
    return {key: value for key, value in result.items() if not key.startswith("_")}


def combined_gpx(data_root, keys):
    # Build an in-memory response, never a file in the read-only data mount.
    # Keep every original segment separate so the map visibly breaks at gaps.
    root = ET.Element("gpx", version="1.0", creator="mileage-viewer")
    track = ET.SubElement(root, "trk")
    for parsed in parsed_parts(data_root, keys):
        for segment in parsed["segments"]:
            seg = ET.SubElement(track, "trkseg")
            for p in segment:
                pt = ET.SubElement(seg, "trkpt", lat=str(p["lat"]), lon=str(p["lon"]))
                if p["time"] is not None:
                    ET.SubElement(pt, "time").text = p["time"].isoformat()
                for tag, field in (("speed", "speed_mps"), ("hdop", "hdop")):
                    if p[field] is not None:
                        ET.SubElement(pt, tag).text = str(p[field])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def apply_merges(conn, data_root, trips, unreadable, *, refresh=True, only_key=None):
    """Use persisted summaries, reparsing only when a source stamp changes.

    A broken/missing part makes the whole merged trip unreadable, rather than
    silently reverting to individual parts or counting an old cached distance.
    Caller owns a database transaction for a consistent membership/annotation
    view. With refresh=False, request a fresh write transaction only if needed.
    """
    rows = (conn.execute("SELECT * FROM trip_merges WHERE trip_key=?", (only_key,)).fetchall()
            if only_key is not None else conn.execute("SELECT * FROM trip_merges").fetchall())
    originals = {t["trip_key"]: t for t in trips}
    identities = {t["trip_key"]: t for t in trips + unreadable}
    hidden = set()
    merged, broken = [], []
    for row in rows:
        row = dict(row)
        key = row["trip_key"]
        keys = parts(conn, key)
        hidden.update(keys)
        ym, trip_id = key.split("/")
        t = {"ym": ym, "id": trip_id, "trip_key": key, "parts": keys,
             "has_gpx": True, **{f: row[f] or "" for f in ANNOTATION_FIELDS}}
        if keys:
            first = identities.get(keys[0], {})
            t.update(trip_id=first.get("trip_id") or keys[0].split("/")[1],
                     gpx_file="; ".join(k + ".gpx" for k in keys),
                     device_id=first.get("device_id"), start_time=first.get("start_time"))
        try:
            if not keys or any(k not in originals for k in keys):
                raise ValueError("A merge part's summary is unavailable")
            stamp = fingerprint(data_root, keys)
            if not same_sources(stamp, row["fingerprint"]):
                if not refresh:
                    raise MergeRefreshNeeded()
                summary = combined_summary(data_root, keys)
                if fingerprint(data_root, keys) != stamp:
                    raise ValueError("Merge part changed during refresh")
                conn.execute("UPDATE trip_merges SET summary=?, fingerprint=? WHERE trip_key=?",
                             (json.dumps(summary), stamp, key))
            else:
                summary = json.loads(row["summary"])
                # Record inode metadata for legacy rows without changing the
                # stored summary's rules. This is a one-time cache upgrade.
                if stamp != row["fingerprint"]:
                    if not refresh:
                        raise MergeRefreshNeeded()
                    conn.execute("UPDATE trip_merges SET fingerprint=? WHERE trip_key=?", (stamp, key))
            t = {**summary, **t, "start_time": summary.get("start_time")}
            # The merged trip arrived when its last part did.
            received = trip_log.latest_stamp(originals[k].get("received_at") for k in keys)
            if received:
                t["received_at"] = received
            merged.append(t)
        except (OSError, ValueError):
            broken.append({**t, "problem": "A merge part is missing or unreadable; restore it or undo the merge."})
    return ([t for t in trips if t["trip_key"] not in hidden] + merged,
            [t for t in unreadable if t["trip_key"] not in hidden] + broken)


def _known_device(value):
    return isinstance(value, str) and value.strip().lower() not in ("", "unknown")


def _unreadable_start(trip):
    start = _parse_iso(trip.get("start_time"))
    if start:
        return start
    # Firmware timestamp IDs are UTC. Month buckets and upload times cannot
    # establish chronology. Legacy/non-timestamp IDs give no timing evidence.
    key = trip.get("parts", [trip["trip_key"]])[0]
    trip_id = key.split("/")[-1]
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z", trip_id):
        try:
            return datetime.strptime(trip_id, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


REMOVED_BETWEEN = ("A trip removed from the log lies between these recordings. "
                   "Restore it to merge them, or leave them separate.")


def next_trip(trip, trips, unreadable, removed=()):
    """Return (eligible next trip, blocking reason) for a known device.

    Damage blocks adjacency only for an unidentified device or a same-device
    trip whose start could fall between this trip and the next visible one.
    `removed` holds the readable trips the admin removed from the log. They
    still count as recordings here: joining the trips on either side of one
    would bridge its stretch with a straight line and count it twice once it
    is restored. Removed unreadable trips are left out on purpose; their
    miles are never counted, and removing one is how the admin clears the
    unreadable-summary warning.
    """
    device = trip.get("device_id")
    # Ingest uses the literal "unknown" when neither header nor GPX creator
    # identifies a device; two such recordings do not establish one device.
    if not _known_device(device):
        return None, ""
    same = [t for t in [*trips, *removed] if t.get("device_id") == device]
    if any(not _parse_iso(t.get("start_time")) for t in same):
        return None, ""
    same.sort(key=lambda t: (_parse_iso(t["start_time"]), t["trip_key"]))
    index = next(i for i, t in enumerate(same) if t["trip_key"] == trip["trip_key"])
    if index + 1 == len(same):
        return None, ""
    candidate = same[index + 1]
    # Legacy summaries predate recorded_until. end_time is the best available
    # boundary; never substitute the upload time, which can be days later.
    end = _parse_iso(trip.get("recorded_until")) or _parse_iso(trip.get("end_time"))
    start = _parse_iso(candidate.get("start_time"))
    if end and timedelta(0) <= start - end <= MERGE_MAX_GAP:
        if candidate.get("removal"):
            return None, REMOVED_BETWEEN
        current_start = _parse_iso(trip["start_time"])
        for damaged in unreadable:
            damaged_device = damaged.get("device_id")
            if _known_device(damaged_device) and damaged_device != device:
                continue
            # A damaged summary usually has no readable device, so its
            # timestamp ID is the evidence: one provably outside this gap
            # can't be the trip in between, whatever device recorded it.
            damaged_start = _unreadable_start(damaged)
            if damaged_start is not None and not current_start <= damaged_start <= start:
                continue
            if not _known_device(damaged_device):
                return None, "Cannot establish the next trip: an unreadable summary with an unknown device may fall between these recordings. Repair the summary before merging."
            return None, "Cannot establish the next trip: an unreadable summary from this device may fall between these recordings. Repair the summary before merging."
        return candidate, ""
    return None, ""


def recorded_span(trip):
    """(start, end) of a readable trip's recording, or None without both."""
    start = _parse_iso(trip.get("start_time"))
    ends = [e for e in (_parse_iso(trip.get("recorded_until")), _parse_iso(trip.get("end_time"))) if e]
    return (start, max(ends)) if start and ends else None


def covering_merge(removed_trip, trips):
    """A live merged trip whose recorded span overlaps a removed trip's, or None.

    Only data from before merge suggestions saw removed trips can have one.
    """
    span = recorded_span(removed_trip)
    if span is None:
        return None
    for t in trips:
        if not t.get("parts") or t.get("removal") or t.get("device_id") != removed_trip.get("device_id"):
            continue
        other = recorded_span(t)
        if other and other[0] <= span[1] and span[0] <= other[1]:
            return t
    return None


def trips_inside_merges(trips):
    """{trip_key: merged trip} for each unmerged trip a live merged trip already covers.

    The merge joined its parts with a straight line across any gap, so a
    same-device recording from inside that gap that was uploaded after the
    merge is counted a second time. Same overlap rule as covering_merge(),
    but each span is worked out once: this runs on every list page, and it
    reads only the stored summaries, never a GPX file.
    """
    merged = [(t, recorded_span(t)) for t in trips if t.get("parts") and not t.get("removal")]
    merged = [(t, span) for t, span in merged if span]
    inside = {}
    if not merged:
        return inside
    for trip in trips:
        if trip.get("parts"):
            continue
        span = recorded_span(trip)
        if span is None:
            continue
        for cover, other in merged:
            if cover.get("device_id") == trip.get("device_id") and other[0] <= span[1] and span[0] <= other[1]:
                inside[trip["trip_key"]] = cover
                break
    return inside


def annotated(trip):
    return any(trip.get(f) for f in ANNOTATION_FIELDS)
