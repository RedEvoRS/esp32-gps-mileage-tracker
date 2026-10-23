"""Canonical GPX parsing and mileage rules shared by ingest and viewer."""

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.parsers import expat

# Unit conversions, named so the math below reads in plain English.
METERS_PER_MILE = 1609.344
MPS_TO_MPH = 2.2369362920544

# Distance and timing model. Bump SUMMARY_VERSION whenever these rules change, so
# summaries written under the old rules can be found and recomputed.
SUMMARY_VERSION = 3
# Below this speed at both ends of a step, the vehicle is parked or stopped
# at a light and the step is GPS scatter. 2 mph matches the firmware's
# END_MPS, the speed at which it decides a drive is winding down.
STATIONARY_MPS = 0.894
# Faster than this between two fixes is a bad fix, not driving (~134 mph).
MAX_PLAUSIBLE_MPS = 60.0
# Fixes with a worse horizontal dilution of precision than this are left
# out of the distance; their neighbours are joined directly instead.
MAX_HDOP = 5.0
MAX_UPLOAD_TEXT = 128


def _local(tag):
    """Return the local name of an ElementTree tag, stripping any namespace."""
    return tag.rsplit("}", 1)[-1]


def _find_local(elem, name):
    """First direct-or-descendant child whose local tag name matches `name`."""
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _parse_iso(value):
    """Parse an ISO 8601 timestamp, tolerating a trailing 'Z' for UTC.

    Python's fromisoformat before 3.11 doesn't accept 'Z', so we
    normalize it to '+00:00' first. Returns a timezone-aware datetime
    or None if the value is missing/unparseable.

    A timestamp with no offset is taken as UTC -- GPX times are UTC by
    spec. Returning it naive would make min()/max() over a track raise
    TypeError the moment one point disagrees with the rest, turning a
    recoverable quirk into a 500 the device retries forever.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in meters."""
    r = 6371000.0  # mean Earth radius, meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def parse_gpx(raw_bytes, *, time_bounds=None):
    """Parse a GPX document into the segments and summary fields we need.

    Returns a dict:
        {
          "creator": str | None,
          "segments": [ [ {lat, lon, time, speed_mps}, ... ], ... ],
          "metadata_time": datetime | None,
        }

    Raises ValueError if the bytes are not a parseable <gpx> document.
    Optional UTC time_bounds validate all parseable times before skipping points.
    """
    # Use Expat's declaration callbacks, as the viewer does, so UTF-16 and
    # other supported encodings cannot bypass a byte-pattern DTD check.
    def reject_declaration(*args):
        raise ValueError("GPX document types and entities are not supported")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    try:
        parser.Parse(raw_bytes, True)
        root = ET.fromstring(raw_bytes)
    except (ET.ParseError, expat.ExpatError, LookupError) as e:
        raise ValueError(f"not valid XML: {e}")

    if _local(root.tag) != "gpx":
        raise ValueError(f"root element is <{_local(root.tag)}>, expected <gpx>")

    if time_bounds is not None:
        earliest, latest = time_bounds
        # Validate before skipping malformed-coordinate points or selecting
        # just the first metadata time. No rejected timestamp may hide in raw
        # GPX that the viewer will subsequently parse independently.
        for element in root.iter():
            if _local(element.tag) == "time":
                instant = _parse_iso(element.text)
                if instant is not None and not earliest <= instant.astimezone(timezone.utc) <= latest:
                    raise ValueError("trip timestamps must be from 2000-01-01 through one day after receipt")

    creator = root.get("creator")
    if creator is not None and len(creator) > MAX_UPLOAD_TEXT:
        raise ValueError("GPX creator exceeds 128 characters")

    metadata_time = None
    for child in root:
        if _local(child.tag) == "metadata":
            t = _find_local(child, "time")
            if t is not None:
                metadata_time = _parse_iso(t.text)
            break

    segments = []
    for trk in root.iter():
        if _local(trk.tag) != "trk":
            continue
        for seg in trk:
            if _local(seg.tag) != "trkseg":
                continue
            points = []
            for pt in seg:
                if _local(pt.tag) != "trkpt":
                    continue
                lat = pt.get("lat")
                lon = pt.get("lon")
                if lat is None or lon is None:
                    continue
                try:
                    lat = float(lat)
                    lon = float(lon)
                except ValueError:
                    continue
                if not (math.isfinite(lat) and -90 <= lat <= 90
                        and math.isfinite(lon) and -180 <= lon <= 180):
                    raise ValueError("track point coordinates must be finite and in range")

                t_el = None
                speed_mps = None
                hdop = None
                for sub in pt.iter():
                    name = _local(sub.tag)
                    if name == "time" and t_el is None:
                        t_el = sub
                    elif name == "speed" and speed_mps is None:
                        # m/s both as GPX 1.0 <speed> and Garmin TPX v2
                        # gpxtpx:speed (GPX 1.1)
                        try:
                            speed_mps = float(sub.text)
                        except (TypeError, ValueError):
                            pass
                    elif name == "hdop" and hdop is None:
                        try:
                            hdop = float(sub.text)
                        except (TypeError, ValueError):
                            pass

                for name, value in (("speed", speed_mps), ("hdop", hdop)):
                    if value is not None and (not math.isfinite(value) or value < 0):
                        raise ValueError(f"track point {name} must be finite and nonnegative")

                points.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "time": _parse_iso(t_el.text) if t_el is not None else None,
                        "speed_mps": speed_mps,
                        "hdop": hdop,
                    }
                )
            if points:
                segments.append(points)

    return {"creator": creator, "segments": segments, "metadata_time": metadata_time}


def _step(a, b, require_time=False):
    """Distance in meters to count for the step a -> b, plus its derived speed.

    Returns (meters, derived_mps). meters is 0 when the step shouldn't count:
      - an impossible jump (> MAX_PLAUSIBLE_MPS): a bad fix, not driving;
      - neither end moving: the vehicle was stopped, so the step is GPS
        scatter. A parked receiver wanders a few metres a minute, and the
        firmware keeps logging through every red light and the 5-minute
        end-of-trip cooldown, so counting these inflated business miles.
    Movement comes from the device's Doppler speed where present, falling
    back to distance/time. With require_time (bridging a GPS gap), a step
    without timestamps can't be checked for plausibility and isn't counted.
    derived_mps is None when there's no usable time difference.
    """
    d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    dt = None
    if a["time"] and b["time"]:
        dt = (b["time"] - a["time"]).total_seconds()
        if dt <= 0:
            dt = None
    derived = d / dt if dt else None

    if dt is None and require_time:
        return 0.0, None
    if derived is not None and derived > MAX_PLAUSIBLE_MPS:
        return 0.0, None

    speeds = [s for s in (a["speed_mps"], b["speed_mps"]) if s is not None]
    if speeds:
        moving = max(speeds) > STATIONARY_MPS
    elif derived is not None:
        moving = derived > STATIONARY_MPS
    else:
        moving = True  # no speed, no time: nothing to judge by
    return (d if moving else 0.0), derived


def summarize(parsed):
    """Compute the trip summary fields from a parse_gpx() result.

    Distance follows _step() over consecutive fixes, skipping fixes with
    HDOP above MAX_HDOP. Across a <trkseg> break (a GPS gap: tunnel,
    parking structure) the last fix before and the first fix after are
    joined by a straight line. That's the shortest possible path, so it can
    only undercount what was driven -- dropping the gap entirely lost the
    whole gap's distance.

    end_time is the last fix where the vehicle was moving, so the
    firmware's 5-minute stationary cooldown doesn't stretch every trip;
    the final timestamp in the file is kept as recorded_until. Max speed
    prefers the device-reported speed, else distance/time. Average speed
    is total distance over start..end only when every point is timed.
    Partly timed trips retain distance and observed timestamps, but expose
    no full-trip bounds, duration or average speed.
    """
    all_points = [p for seg in parsed["segments"] for p in seg]
    num_points = len(all_points)

    times = [p["time"] for p in all_points if p["time"] is not None]
    start_time = min(times) if times else None
    timing_status = "complete" if len(times) == num_points else "partial" if times else "untimed"
    recorded_until = max(times) if times else None
    moving_times = [
        p["time"]
        for p in all_points
        if p["time"] is not None
        and p["speed_mps"] is not None
        and p["speed_mps"] > STATIONARY_MPS
    ]
    end_time = max(moving_times) if moving_times else recorded_until
    duration_seconds = (
        (end_time - start_time).total_seconds()
        if timing_status == "complete" and start_time and end_time else None
    )

    distance_m = 0.0
    max_speed_mps = 0.0
    lats, lons = [], []

    last_good = None  # last usable fix, carried across segment breaks
    for seg in parsed["segments"]:
        first_in_seg = True
        for p in seg:
            lats.append(p["lat"])
            lons.append(p["lon"])
            if p["hdop"] is not None and p["hdop"] > MAX_HDOP:
                continue
            if p["speed_mps"] is not None:
                max_speed_mps = max(max_speed_mps, p["speed_mps"])
            if last_good is not None:
                d, derived = _step(last_good, p, require_time=first_in_seg)
                distance_m += d
                # Derive speed only if the device didn't report one.
                if d and p["speed_mps"] is None and derived is not None:
                    max_speed_mps = max(max_speed_mps, derived)
            last_good = p
            first_in_seg = False

    distance_miles = distance_m / METERS_PER_MILE
    if duration_seconds and duration_seconds > 0:
        avg_speed_mph = (distance_m / duration_seconds) * MPS_TO_MPH
    else:
        avg_speed_mph = 0.0 if timing_status == "complete" else None

    max_speed_mph = max_speed_mps * MPS_TO_MPH
    for value in (distance_m, distance_miles, max_speed_mph, avg_speed_mph):
        if value is not None and not math.isfinite(value):
            raise ValueError("derived trip numbers must be finite")

    bbox = None
    if lats:
        bbox = {
            "min_lat": min(lats),
            "min_lon": min(lons),
            "max_lat": max(lats),
            "max_lon": max(lons),
        }

    return {
        # Partial timing cannot establish full-trip bounds. Keep observed
        # times separately and preserve the old identity/bucketing anchors.
        "timing_status": timing_status,
        "timed_from": start_time.isoformat() if start_time else None,
        "start_time": start_time.isoformat() if timing_status == "complete" and start_time else None,
        "end_time": end_time.isoformat() if timing_status == "complete" and end_time else None,
        "recorded_until": recorded_until.isoformat() if recorded_until else None,
        "duration_seconds": (
            round(duration_seconds) if duration_seconds is not None else None
        ),
        "summary_version": SUMMARY_VERSION,
        "distance_miles": round(distance_miles, 3),
        "max_speed_mph": round(max_speed_mph, 1),
        "avg_speed_mph": round(avg_speed_mph, 1) if avg_speed_mph is not None else None,
        "num_points": num_points,
        "bbox": bbox,
        "_end_dt": end_time,  # internal: used for path bucketing, stripped before write
        "_identity_dt": start_time,  # internal: keep existing mixed-timing trip IDs stable
    }
