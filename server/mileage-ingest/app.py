"""
Mileage Tracker Ingest Service

A minimal HTTP service that receives trip uploads from the T-Beam GPS
mileage tracker and persists them as GPX files on disk, alongside a
derived JSON summary. It also receives bounded diagnostic event batches and
ordered health check-ins in an independent SQLite database (DIAGNOSTICS.md).

This service accepts a GPX upload, stores the raw GPX unchanged (it is the
immutable source of truth), and computes a small JSON summary so other tools can list and
total trips without re-parsing every track. It does not generate reports,
serve a UI, or classify trips. Those responsibilities belong to the
separate viewer/export service that reads these files independently.

The device uploads the GPX body directly (Content-Type: application/gpx+xml)
and identifies the trip with two optional headers:

    X-Device-Id: tbeam-01
    X-Trip-Id:   2026-04-14T13-45-22

The T-Beam sends only X-Device-Id. Without X-Device-Id we use the GPX
`creator` attribute; without X-Trip-Id, the trip's start time in UTC
(2026-04-14T13-45-22Z), which is also the stored file's name.

For each upload we write two files into /data/YYYY-MM/ (bucketed by the
trip's end time, or its first upload for an undated trip). Undated retries
reuse their original directory even if the month has changed:

    <id>.gpx    the raw upload, byte-for-byte
    <id>.json   { trip_id, device_id, start_time, end_time, recorded_until,
                  duration_seconds, distance_miles, max_speed_mph,
                  avg_speed_mph, timing_status, timed_from, num_points, bbox{...}, gpx_file,
                  summary_version, _received_at, _received_from }

Distance is the haversine sum over consecutive fixes, with three rules
(see summarize()): steps where the vehicle wasn't moving are GPS jitter
and don't count; poor fixes (high HDOP) and physically impossible jumps
are skipped; and a GPS gap between <trkseg>s is bridged with the straight
line across it, which can only undercount what was really driven. The
raw GPX is kept byte-for-byte, so these rules can change and every
summary be recomputed.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, request, jsonify
from werkzeug.exceptions import ClientDisconnected

# Support both the source tree and the container's flat /app layout, including
# direct app.py execution and importlib-based service tests.
for import_root in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.diagnostics import CatalogError, ProtocolError, load_catalog, parse_request, validate_device_id
from common.diagnostics.protocol import MAX_REQUEST_BYTES
from diag_store import ConflictError, DiagnosticsStore


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
# All configuration comes from environment variables so the same image
# can run in different environments without modification. This is the
# "twelve factor app" pattern for config.

AUTH_TOKEN = os.environ.get("INGEST_AUTH_TOKEN")
DATA_ROOT = Path(os.environ.get("INGEST_DATA_ROOT", "/data"))
try:
    DIAGNOSTICS_CATALOG = load_catalog()
except CatalogError as exc:
    raise RuntimeError("Cannot start ingest: missing or invalid diagnostics catalog.json") from exc
DIAGNOSTICS_STORE = DiagnosticsStore(DATA_ROOT, DIAGNOSTICS_CATALOG)
MAX_DIAGNOSTICS_DISCARD_BYTES = 4 * MAX_REQUEST_BYTES

# Timestamps inside the stored files stay UTC -- that's what the GPX
# carries and it's the unambiguous form. This zone controls one thing:
# which YYYY-MM/ folder a trip is filed under. It has to match the zone
# the viewer displays in, or a late-December drive lands in next year's
# folder while showing December on screen. Keep it in step with
# DISPLAY_TZ in mileage-viewer/app.py. Both come from DISPLAY_TZ in .env,
# which install.sh asks for.
#
# Named IANA zone only, never a fixed offset -- most zones observe DST.
# There's no default: guessing a zone files trips under the wrong month.
DISPLAY_TZ_NAME = os.environ.get("DISPLAY_TZ", "").strip()
if not DISPLAY_TZ_NAME:
    raise RuntimeError(
        "DISPLAY_TZ is not set. Set it in .env to your IANA timezone, e.g. "
        "DISPLAY_TZ=America/Chicago (install.sh asks for it), then run "
        "docker compose up -d."
    )

try:
    DISPLAY_TZ = ZoneInfo(DISPLAY_TZ_NAME)
except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment fault
    raise RuntimeError(
        f"Timezone database missing or {DISPLAY_TZ_NAME!r} is not a valid "
        "IANA zone. The 'tzdata' package is in requirements.txt for this "
        "reason -- rebuild the image (docker compose build mileage-ingest)."
    ) from exc

# Reject absurdly large bodies early. The whole body is held in memory and
# parsed there, peaking at ~9x its size (a 9.2 MiB trip peaks at ~86 MiB),
# so the cap protects the server's memory. Without one, a trip that never
# ended (a parked car whose USB port stays on, GPS drift keeping the trip
# open) could get the worker killed for memory; the device sees that as a
# dropped connection, keeps the file, and resends it on every visit home.
# The cap turns it into one 413, and the device sets the file aside.
# 16 MiB is ~39 hours of nonstop driving at ~119 B per second, far past any
# real trip; the firmware has to keep its trips under it, and
# test_upload_limits.py checks that it does.
MAX_UPLOAD_BYTES = int(os.environ.get("INGEST_MAX_UPLOAD_BYTES", 16 * 1024 * 1024))

# A reply sent before the body is read (bad token, too large) only reaches
# the device if the rest of the body is read first. The firmware sends the
# whole file before it reads the reply; gunicorn closes the socket with the
# body still queued, the kernel resets the connection, and the device sees
# a send error instead of the status. Read and discard up to this much; a
# bigger body gets the reset. It must stay well above the cap: if it
# didn't, an over-cap upload would get the reset, not the 413, and the
# device would keep retrying it instead of setting it aside. Discarding
# reads 64 KB at a time, so this costs time, not memory.
MAX_DISCARD_BYTES = 4 * MAX_UPLOAD_BYTES

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

# Fail loudly at startup if the auth token wasn't provided. We'd rather
# crash immediately than run unauthenticated and silently accept uploads
# from anyone who can reach the port.
if not AUTH_TOKEN:
    print(
        "FATAL: INGEST_AUTH_TOKEN environment variable not set. "
        "Refusing to start without authentication.",
        file=sys.stderr,
    )
    sys.exit(1)

# Ensure the storage directory exists. This should normally be a
# volume mount, but creating it here makes the app robust to being
# run without a mount for testing.
DATA_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------

app = Flask(__name__)

# Enforce the size cap during request reading (Flask raises 413, answered
# by payload_too_large()), rather than only after buffering the whole body
# in memory. The explicit length check in the route remains as a backstop.
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Configure logging to go to stdout/stderr, which docker captures
# automatically. This way `docker compose logs mileage-ingest` shows
# everything the service is doing.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------
# GPX parsing helpers
# ---------------------------------------------------------------------
# GPX documents are namespaced (default xmlns plus gpxtpx for the Garmin
# TrackPointExtension that carries speed/course). Rather than wire up
# namespace maps everywhere, we match on the local tag name -- the part
# after the '}' that ElementTree prepends. This tolerates whatever
# namespace prefixes a given writer happens to use.


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


def parse_gpx(raw_bytes):
    """Parse a GPX document into the segments and summary fields we need.

    Returns a dict:
        {
          "creator": str | None,
          "segments": [ [ {lat, lon, time, speed_mps}, ... ], ... ],
          "metadata_time": datetime | None,
        }

    Raises ValueError if the bytes are not a parseable <gpx> document.
    """
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as e:
        raise ValueError(f"not valid XML: {e}")

    if _local(root.tag) != "gpx":
        raise ValueError(f"root element is <{_local(root.tag)}>, expected <gpx>")

    creator = root.get("creator")

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
        firmware keeps logging through every red light and the 3-minute
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
    firmware's 3-minute stationary cooldown doesn't stretch every trip;
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
        "max_speed_mph": round(max_speed_mps * MPS_TO_MPH, 1),
        "avg_speed_mph": round(avg_speed_mph, 1) if avg_speed_mph is not None else None,
        "num_points": num_points,
        "bbox": bbox,
        "_end_dt": end_time,  # internal: used for path bucketing, stripped before write
        "_identity_dt": start_time,  # internal: keep existing mixed-timing trip IDs stable
    }


def _safe_id(raw):
    """Sanitize a string into something safe to use as a filename."""
    # Match the viewer's ASCII route allowlist, including for GPX creators
    # and explicit IDs supplied by third-party uploaders.
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_"
                   for c in str(raw))


def _sync_directory(directory):
    """Persist directory entries before acknowledging uploads on POSIX."""
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _upload_lock():
    """Serialize record updates across Gunicorn workers (and local tests).

    Lock the whole GPX/JSON transaction, not just individual renames. Kernel
    locks are released if a worker exits; no stale lock cleanup is needed.
    """
    with open(DATA_ROOT / ".upload.lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _atomic_write_bytes(target, data):
    """Write bytes to `target` atomically via temp-file-then-rename.

    Renames within a directory are atomic on POSIX filesystems, so a
    reader never sees a half-written file -- it sees either the old
    version or the complete new one.
    """
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(target)
        _sync_directory(target.parent)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(target, obj):
    payload = json.dumps(obj, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    _atomic_write_bytes(target, payload)


def _stored_summary(json_file):
    """A trip's stored summary as a dict, or None if it's damaged.

    Damaged means it isn't a JSON object with the trip and device IDs as
    text, so it can't say whose trip this is. Failing to read the file at
    all (permissions, I/O) isn't damage: that raises, and the upload 500s.
    """
    try:
        summary = json.loads(json_file.read_bytes())
    except ValueError:  # bad JSON or bad UTF-8
        return None
    if not isinstance(summary, dict) or not all(
        isinstance(summary.get(key), str) for key in ("trip_id", "device_id")
    ):
        return None
    return summary


def _stored_trip_directories(safe_id):
    """Find either half of a stored pair; caller must hold _upload_lock.

    Undated trips have no stable timestamp to choose a month. Look for
    their ID before using the receipt month, including GPX-only failed
    writes and summary-only records whose GPX can be restored by a retry.
    Check known filenames in each month instead of reading every trip.
    """
    matches = []
    for directory in DATA_ROOT.iterdir():
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", directory.name):
            continue
        for suffix in (".gpx", ".json"):
            try:
                # stat() propagates permission/I/O errors: an unreadable
                # original must not be mistaken for a missing record.
                (directory / f"{safe_id}{suffix}").stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            matches.append(directory)
            break
    return matches


def _discard_body():
    """Read and drop an unread request body, so an early reply arrives.

    See MAX_DISCARD_BYTES. Call it only before anything has read the body:
    it reads Content-Length bytes from the raw input, and a server that
    doesn't bound wsgi.input itself would wait for bytes that never come.
    """
    remaining = request.content_length or 0
    if remaining > MAX_DISCARD_BYTES:
        return
    stream = request.environ["wsgi.input"]
    try:
        while remaining > 0:
            chunk = stream.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass  # the client went away mid-body; nobody is left to answer


def _authenticate(discard):
    """Shared constant-time bearer check; callers select their bounded drain."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        discard()
        return jsonify({"error": "missing or malformed Authorization header"}), 401

    presented_token = auth_header[7:]  # strip "Bearer " prefix
    # Constant-time comparison; on a LAN the timing channel is mostly
    # theoretical, but compare_digest costs nothing to use. Compare bytes:
    # on str, compare_digest raises TypeError for any non-ASCII character,
    # so "Bearer café" would be a 500 instead of a 401.
    if not hmac.compare_digest(
        presented_token.encode("utf-8"), AUTH_TOKEN.encode("utf-8")
    ):
        app.logger.warning(
            "Rejected upload with bad token from %s", request.remote_addr
        )
        discard()
        return jsonify({"error": "unauthorized"}), 401


def _discard_diagnostics_body(consumed=0):
    """Drain raw/dechunked WSGI input within a per-request byte budget.

    Avoid Flask's GPX length cap and missing-length safe fallback here. Gunicorn
    supplies a framed, dechunked wsgi.input, including for chunked requests.
    A lengthless input is read only up to the bounded budget, never unbounded.
    """
    length = request.content_length
    if length is not None and length > MAX_DIAGNOSTICS_DISCARD_BYTES:
        return
    remaining = MAX_DIAGNOSTICS_DISCARD_BYTES - consumed
    if length is not None:
        remaining = min(remaining, max(0, length - consumed))
    try:
        while remaining > 0:
            chunk = request.environ["wsgi.input"].read(min(remaining, 8192))
            if not chunk:
                break
            remaining -= len(chunk)
    except (OSError, ClientDisconnected):
        pass


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@app.route("/v1/device-events", methods=["POST"])
def device_events():
    auth_error = _authenticate(_discard_diagnostics_body)
    if auth_error is not None:
        return auth_error
    try:
        device_id = validate_device_id(request.headers.get("X-Device-Id"))
    except ProtocolError as exc:
        _discard_diagnostics_body()
        return jsonify(error=str(exc)), 400

    length = request.content_length
    if length is not None and length > MAX_REQUEST_BYTES:
        _discard_diagnostics_body()
        return jsonify(error="payload too large (limit 32768 bytes)"), 413
    chunks, consumed = [], 0
    try:
        while length is None or consumed < length:
            size = min(8192, MAX_REQUEST_BYTES + 1 - consumed)
            if length is not None:
                size = min(size, length - consumed)
            chunk = request.environ["wsgi.input"].read(size)
            if not chunk:
                if length is not None and consumed < length:
                    return jsonify(error="incomplete request body"), 400
                break
            consumed += len(chunk)
            if consumed > MAX_REQUEST_BYTES:
                _discard_diagnostics_body(consumed)
                return jsonify(error="payload too large (limit 32768 bytes)"), 413
            chunks.append(chunk)
    except (OSError, ClientDisconnected):
        return jsonify(error="cannot read request body"), 400
    try:
        body = parse_request(b"".join(chunks), catalog=DIAGNOSTICS_CATALOG)
    except ProtocolError as exc:
        return jsonify(error=str(exc)), 400
    try:
        accepted = DIAGNOSTICS_STORE.accept(device_id, body)
    except ConflictError as exc:
        return jsonify(error=str(exc)), 409
    except Exception:
        app.logger.exception("Failed to commit device diagnostics")
        return jsonify(error="server error storing diagnostics"), 500
    return jsonify(accepted=accepted), 200


@app.route("/health", methods=["GET"])
def health():
    """
    Simple health check endpoint used by Docker's healthcheck and for
    manual verification that the service is up. Returns 200 with a
    trivial JSON body. Intentionally does not require authentication
    so monitoring tools can poll it freely.
    """
    return jsonify({"status": "ok", "service": "mileage-ingest"}), 200


@app.errorhandler(413)
def payload_too_large(_error):
    """Answer an oversized upload in JSON, like every other error here.

    MAX_CONTENT_LENGTH makes Flask raise this from the Content-Length
    header, before upload_trip() reads any of the body, and Flask's own
    413 page is HTML. Without a Content-Length it's raised partway through
    the body instead, and there's no length to discard by.
    """
    _discard_body()
    return jsonify({"error": f"payload too large (limit {MAX_UPLOAD_BYTES} bytes)"}), 413


@app.route("/upload", methods=["POST"])
def upload_trip():
    """
    Receive a GPX trip from the T-Beam, store it, and write a summary.

    Flow:
      1. Verify the bearer token
      2. Read and parse the GPX body
      3. Determine trip_id / device_id (headers, then GPX fallbacks)
      4. Compute the summary; bucket the path by the trip's end time
      5. Write the .gpx and .json atomically
      6. Return a success response
    """

    # -----------------------------------------------------------------
    # Step 1: Authentication
    # -----------------------------------------------------------------
    # Simple bearer token: LAN-appropriate security, not internet-grade.
    # A 401 discards the unread body first, so the device sees the 401
    # rather than a reset connection (see MAX_DISCARD_BYTES).
    auth_error = _authenticate(_discard_body)
    if auth_error is not None:
        return auth_error

    # -----------------------------------------------------------------
    # Step 2: Read and parse the GPX body
    # -----------------------------------------------------------------
    # Over MAX_CONTENT_LENGTH, get_data() raises 413 (payload_too_large).
    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"error": "empty request body"}), 400
    if len(raw) > MAX_UPLOAD_BYTES:
        abort(413)

    try:
        parsed = parse_gpx(raw)
    except ValueError as e:
        return jsonify({"error": f"invalid GPX: {e}"}), 400

    # A well-formed GPX with no track points is still not a trip. Accepting
    # it would file a phantom 0-mile record in the mileage log. The firmware
    # sets a 400'd file aside as .bad rather than retrying it.
    if not parsed["segments"]:
        return jsonify({"error": "invalid GPX: no track points"}), 400

    summary = summarize(parsed)
    identity_dt = summary.pop("_identity_dt")

    # -----------------------------------------------------------------
    # Step 3: Identify the trip
    # -----------------------------------------------------------------
    device_id = request.headers.get("X-Device-Id") or (parsed["creator"] or "unknown")

    trip_id = request.headers.get("X-Trip-Id")
    if not trip_id:
        # Fall back to the earliest recorded point time, then metadata time. This is
        # the T-Beam's path: it sends no X-Trip-Id. Build the id in UTC and
        # already filename-safe (2026-04-14T14-30-00Z), so the trip_id in
        # the summary and the export is the file's name, not a variant of
        # it that _safe_id() rewrote.
        anchor = identity_dt or parsed["metadata_time"]
        if anchor:
            trip_id = anchor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        else:
            # No usable point or metadata timestamps: derive the id from the
            # body content rather than the wall clock, so a retried
            # upload of the same file is idempotent instead of creating
            # a duplicate under a new received-at name.
            trip_id = "untimed-" + hashlib.sha256(raw).hexdigest()[:12]

    safe_id = _safe_id(trip_id) or (
        "untimed-" + hashlib.sha256(raw).hexdigest()[:12]
    )

    # -----------------------------------------------------------------
    # Step 4: Determine target directory (bucket by end time)
    # -----------------------------------------------------------------
    end_dt = summary.pop("_end_dt") or parsed["metadata_time"]
    untimed = end_dt is None
    end_dt = end_dt or datetime.now(timezone.utc)
    # Bucket by LOCAL month, matching how the viewer displays the trip.
    # Bucketing in UTC while displaying local would file a Dec 31 evening
    # drive under 2027-01/ with a 2026-12-31 date on screen -- the folder
    # and the record disagreeing at exactly the tax-year boundary where it
    # matters most. Timestamps inside the files stay UTC. (end_dt is always
    # aware here: _parse_iso normalizes naive times to UTC.)
    year_month = end_dt.astimezone(DISPLAY_TZ).strftime("%Y-%m")
    target_dir = DATA_ROOT / year_month

    # -----------------------------------------------------------------
    # Step 5: Assemble the summary record and write both files
    # -----------------------------------------------------------------
    record = {
        "trip_id": trip_id,
        "device_id": device_id,
        "gpx_file": f"{safe_id}.gpx",
        "_received_at": datetime.now(timezone.utc).isoformat(),
        "_received_from": request.remote_addr,
    }
    record.update(summary)

    try:
        with _upload_lock():
            if untimed:
                # Lookup and creation share the lock so workers handling
                # opposite sides of midnight cannot create two records.
                existing_dirs = _stored_trip_directories(safe_id)
                if len(existing_dirs) > 1:
                    return jsonify({"error": "undated trip ID exists in multiple month directories"}), 409
                if existing_dirs:
                    target_dir = existing_dirs[0]
            gpx_file = target_dir / f"{safe_id}.gpx"
            json_file = target_dir / f"{safe_id}.json"
            target_dir.mkdir(parents=True, exist_ok=True)
            _sync_directory(DATA_ROOT)
            # An ID can be retried, but must never replace another trip's
            # immutable source (including sanitized-ID/device collisions).
            if gpx_file.exists() and gpx_file.read_bytes() != raw:
                return jsonify({"error": "trip ID already contains different GPX data"}), 409
            if json_file.exists():
                existing = _stored_summary(json_file)
                if existing is None:
                    # A damaged summary can't say whose trip this is, but a
                    # stored GPX identical to this upload proves it's the
                    # same trip, so rebuild the summary from it. With no
                    # stored GPX, nothing proves it.
                    if not gpx_file.exists():
                        return jsonify({"error": "trip ID has a damaged summary and no GPX to match"}), 409
                    app.logger.warning("Rebuilding the damaged summary of trip %s", safe_id)
                elif existing["trip_id"] != trip_id or existing["device_id"] != device_id:
                    return jsonify({"error": "trip ID belongs to another trip or device"}), 409
                else:
                    # Keep the original receipt metadata on harmless retries.
                    record["_received_at"] = existing.get("_received_at", record["_received_at"])
                    record["_received_from"] = existing.get("_received_from", record["_received_from"])
            # GPX first: failed summary writes can be repaired by a retry.
            # Each write is durable before a 200 lets firmware delete its copy.
            if not gpx_file.exists():
                _atomic_write_bytes(gpx_file, raw)
            _atomic_write_json(json_file, record)
    except Exception as e:
        app.logger.error("Failed to write trip %s: %s", safe_id, e)
        return jsonify({"error": "server error writing trip"}), 500

    # -----------------------------------------------------------------
    # Step 6: Success
    # -----------------------------------------------------------------
    # Only the validated header identifies diagnostics contact; GPX creator
    # and the summary's legacy fallback are never used to guess a device.
    try:
        contact_device = validate_device_id(request.headers.get("X-Device-Id"))
    except ProtocolError:
        contact_device = None
    if contact_device is not None:
        try:
            DIAGNOSTICS_STORE.refresh_contact(contact_device)
        except Exception:
            app.logger.exception("Could not refresh diagnostics contact after GPX upload")

    app.logger.info(
        "Accepted trip %s (%.2f mi, %d pts) from %s",
        trip_id,
        record["distance_miles"],
        record["num_points"],
        request.remote_addr,
    )

    return (
        jsonify(
            {
                "status": "accepted",
                "trip_id": trip_id,
                "stored_as": str(gpx_file.relative_to(DATA_ROOT)),
                "distance_miles": record["distance_miles"],
            }
        ),
        200,
    )


@app.route("/", methods=["GET"])
def index():
    """
    Minimal root response so anyone poking at the service can see it's
    alive, without exposing details of what it does. Security through
    information minimization.
    """
    return (
        jsonify({"service": "mileage-ingest", "endpoints": ["/health", "/upload", "/v1/device-events"]}),
        200,
    )


# ---------------------------------------------------------------------
# Development-only entry point. In production we use gunicorn via the
# Dockerfile CMD; this block only runs if you execute `python app.py`
# directly, which is useful for local smoke testing.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False)
