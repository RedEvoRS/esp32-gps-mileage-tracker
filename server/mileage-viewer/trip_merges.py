"""Viewer-owned merge records; original files and annotations stay untouched."""

import json
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from datetime import datetime, timedelta, timezone

from common.trip_summary import parse_gpx, summarize, SUMMARY_VERSION, _parse_iso

MERGE_MAX_GAP = timedelta(minutes=15)
ANNOTATION_FIELDS = ("category", "purpose_note", "destination")


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
    stamps = [SUMMARY_VERSION]
    for path in source_paths(data_root, keys):
        for source in (path, path.with_suffix(".json")):
            st = source.stat()
            stamps.append((source.name, st.st_mtime_ns, st.st_size))
    return json.dumps(stamps)


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


def apply_merges(conn, data_root, trips, unreadable):
    """Use persisted summaries, reparsing only when a source stamp/rules change.

    A broken/missing part makes the whole merged trip unreadable, rather than
    silently reverting to individual parts or counting an old cached distance.
    Caller owns a database transaction for a consistent membership/annotation view.
    """
    rows = conn.execute("SELECT * FROM trip_merges").fetchall()
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
            if stamp != row["fingerprint"]:
                summary = combined_summary(data_root, keys)
                if fingerprint(data_root, keys) != stamp:
                    raise ValueError("Merge part changed during refresh")
                conn.execute("UPDATE trip_merges SET summary=?, fingerprint=? WHERE trip_key=?",
                             (json.dumps(summary), stamp, key))
            else:
                summary = json.loads(row["summary"])
            t = {**summary, **t, "start_time": summary.get("start_time")}
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


def next_trip(trip, trips, unreadable):
    """Return (eligible next trip, blocking reason) for a known device.

    Damage blocks adjacency only for an unidentified device or a same-device
    trip whose start could fall between this trip and the next visible one.
    """
    device = trip.get("device_id")
    # Ingest uses the literal "unknown" when neither header nor GPX creator
    # identifies a device; two such recordings do not establish one device.
    if not _known_device(device):
        return None, ""
    same = [t for t in trips if t.get("device_id") == device]
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
        current_start = _parse_iso(trip["start_time"])
        for damaged in unreadable:
            damaged_device = damaged.get("device_id")
            if not _known_device(damaged_device):
                return None, "Cannot establish the next trip: an unreadable summary has an unknown device. Repair the summary before merging."
            if damaged_device != device:
                continue
            damaged_start = _unreadable_start(damaged)
            if damaged_start is None or current_start <= damaged_start <= start:
                return None, "Cannot establish the next trip: an unreadable summary from this device may fall between these recordings. Repair the summary before merging."
        return candidate, ""
    return None, ""


def annotated(trip):
    return any(trip.get(f) for f in ANNOTATION_FIELDS)
