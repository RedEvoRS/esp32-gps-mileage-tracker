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
`creator` attribute. Timed uploads use the trip's start time in UTC
(2026-04-14T13-45-22Z), with a device hash suffix only for a collision with
another device; X-Trip-Id is used only for untimed uploads.

For each upload we write two files into /data/YYYY-MM/ (bucketed by the
trip's end time, or its first upload for an undated trip). All retries
reuse their original directory even if the month or DISPLAY_TZ has changed:

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
import io
import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, request, jsonify
from werkzeug.exceptions import ClientDisconnected

# Support both the source tree and the container's flat /app layout, including
# direct app.py execution and importlib-based service tests.
for import_root in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.version import server_version
from common.trip_summary import (
    parse_gpx, summarize, SUMMARY_VERSION, METERS_PER_MILE, MPS_TO_MPH,
    STATIONARY_MPS, MAX_PLAUSIBLE_MPS, MAX_HDOP, MAX_UPLOAD_TEXT, _haversine_m,
)

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
if (not AUTH_TOKEN or not AUTH_TOKEN.strip()
        or any(c.isspace() or unicodedata.category(c) == "Cc" for c in AUTH_TOKEN)):
    raise RuntimeError("INGEST_AUTH_TOKEN must be nonempty and contain no whitespace or control characters")
os.umask(0o077)
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
# reads 8 KB at a time, for at most three seconds. A slower rejected upload
# may see a connection reset instead of its HTTP status.
MAX_DISCARD_BYTES = 4 * MAX_UPLOAD_BYTES
DISCARD_SECONDS = 3.0

# Ensure the storage directory exists. This should normally be a
# volume mount, but creating it here makes the app robust to being
# run without a mount for testing.
DATA_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------

app = Flask(__name__)

# Reject an oversized Content-Length before buffering the body (Flask raises
# 413, answered by payload_too_large()). For lengthless uploads, the route
# reads at most cap + 1 bytes to detect overflow without truncating the GPX.
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Configure logging to go to stdout/stderr, which docker captures
# automatically. This way `docker compose logs mileage-ingest` shows
# everything the service is doing.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------


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

    Timezone changes and undated receipt times can change the month. Look
    for the ID before choosing a new month, including GPX-only failed
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
    _drain_input(remaining)


def _drain_input(remaining):
    """Hard wall-clock budget, including a stalled read or continuous trickle.

    Production uses POSIX Gunicorn sync workers: SIGALRM interrupts blocking
    Python socket reads. In a thread or on platforms without setitimer, only
    in-memory input is drained; returning early is safer than an unbounded read.
    """
    stream = request.environ["wsgi.input"]
    timed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not timed and not isinstance(stream, io.BytesIO):
        return

    def expired(*args):
        raise TimeoutError("rejected body drain deadline")

    if timed:
        previous_handler = signal.signal(signal.SIGALRM, expired)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, DISCARD_SECONDS)
    try:
        while remaining > 0:
            chunk = stream.read(min(remaining, 8192))
            if not chunk:
                break
            remaining -= len(chunk)
    except (OSError, ClientDisconnected):
        pass
    finally:
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


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
    _drain_input(remaining)


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
    return jsonify({"status": "ok", "service": "mileage-ingest", "version": server_version()}), 200


@app.errorhandler(413)
def payload_too_large(_error):
    """Answer an oversized upload in JSON, like every other error here.

    MAX_CONTENT_LENGTH makes Flask raise this from the Content-Length
    header, before upload_trip() reads any of the body, and Flask's own
    413 page is HTML. Without a Content-Length, upload_trip() reads at most
    cap + 1 bytes and explicitly aborts with 413 if the extra byte arrives;
    there's no length to discard by.
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
    if request.content_length is None:
        # Werkzeug otherwise truncates a terminated stream at the cap,
        # hiding overflow. Allow one extra byte on this request only,
        # before request.stream is first accessed and its limit is cached.
        request.max_content_length = MAX_UPLOAD_BYTES + 1
        raw = request.stream.read(MAX_UPLOAD_BYTES + 1)
    else:
        # Over MAX_CONTENT_LENGTH, get_data() raises 413 before reading,
        # so payload_too_large() can still discard the entire unread body.
        raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"error": "empty request body"}), 400
    if len(raw) > MAX_UPLOAD_BYTES:
        abort(413)

    try:
        # Receipt time supplies only an upper limit: years of old backlog are
        # valid. Compare in UTC before local bucketing can overflow datetime.
        earliest = datetime(2000, 1, 1, tzinfo=timezone.utc)
        latest = datetime.now(timezone.utc) + timedelta(days=1)
        parsed = parse_gpx(raw, time_bounds=(earliest, latest))
        for header in ("X-Device-Id", "X-Trip-Id"):
            if len(request.headers.get(header, "")) > MAX_UPLOAD_TEXT:
                raise ValueError(f"{header} exceeds 128 characters")
        summary = summarize(parsed)
    except (ValueError, OverflowError) as e:
        return jsonify({"error": f"invalid GPX: {e}"}), 400

    # A well-formed GPX with no track points is still not a trip. Accepting
    # it would file a phantom 0-mile record in the mileage log. The firmware
    # sets a 400'd file aside as .bad rather than retrying it.
    if not parsed["segments"]:
        return jsonify({"error": "invalid GPX: no track points"}), 400

    identity_dt = summary.pop("_identity_dt")

    # -----------------------------------------------------------------
    # Step 3: Identify the trip
    # -----------------------------------------------------------------
    device_id = request.headers.get("X-Device-Id") or (parsed["creator"] or "unknown")

    anchor = identity_dt or parsed["metadata_time"]
    trip_id = None if anchor else request.headers.get("X-Trip-Id")
    if not trip_id:
        # Fall back to the earliest recorded point time, then metadata time. This is
        # the T-Beam's path: it sends no X-Trip-Id. Build the id in UTC and
        # already filename-safe (2026-04-14T14-30-00Z), so the trip_id in
        # the summary and the export is the file's name, not a variant of
        # it that _safe_id() rewrote.
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
    if anchor is None and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z(?:-[0-9a-f]{12})?", safe_id
    ):
        return jsonify(error="untimed X-Trip-Id cannot use the timestamp trip ID namespace"), 400

    # -----------------------------------------------------------------
    # Step 4: Determine target directory (bucket by end time)
    # -----------------------------------------------------------------
    end_dt = summary.pop("_end_dt") or parsed["metadata_time"]
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
        json.dumps(record, allow_nan=False)
    except (ValueError, OverflowError):
        return jsonify(error="invalid GPX: summary cannot be serialized"), 400

    try:
        with _upload_lock():
            # Lookup and creation share the lock, including timed trips whose
            # local month changes after DISPLAY_TZ is corrected.
            for attempt in range(2):
                existing_dirs = _stored_trip_directories(safe_id)
                if len(existing_dirs) > 1:
                    return jsonify({"error": "trip ID exists in multiple month directories"}), 409
                target_dir = existing_dirs[0] if existing_dirs else DATA_ROOT / year_month
                gpx_file = target_dir / f"{safe_id}.gpx"
                json_file = target_dir / f"{safe_id}.json"
                if not gpx_file.exists() or gpx_file.read_bytes() == raw:
                    break
                # Only a different device with different GPX gets one stable
                # fallback. Missing/damaged identity and suffix collisions stay
                # conflicts; never overwrite a source or keep adding suffixes.
                existing = _stored_summary(json_file) if json_file.exists() else None
                if (anchor is None or attempt or existing is None
                        or existing["device_id"] == device_id):
                    return jsonify({"error": "trip ID already contains different GPX data"}), 409
                trip_id += "-" + hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]
                safe_id = trip_id
                record.update(trip_id=trip_id, gpx_file=f"{safe_id}.gpx")
            target_dir.mkdir(parents=True, exist_ok=True)
            _sync_directory(DATA_ROOT)
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
