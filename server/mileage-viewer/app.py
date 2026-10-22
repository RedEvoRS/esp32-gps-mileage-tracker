"""
Mileage Tracker Viewer + Export Service

Reads the GPX trips and JSON summaries written by mileage-ingest and
provides a small web UI to:

  - list every trip with its date, distance, and duration
  - open a trip on an OpenStreetMap map (Leaflet) to audit the track
    for GPS errors
  - annotate each trip with a purpose (business / personal / other) and
    a free-text note and destination
  - select a local calendar year and export trips and totals to CSV or XLSX
  - inspect device health and diagnostic history at /device-health
  - (admin) remove a trip from the log and restore it, at /admin/removed
  - (admin) review a year's log and close or reopen it for edits, at /admin/year/<YYYY>

Design notes:
  - The GPX/JSON files are the immutable source of truth and are mounted
    read-only. This service never writes to them.
  - This service owns annotations.db (SQLite), including annotations and
    merge membership keyed by relative path "YYYY-MM/<id>". If that DB is lost, every
    trip still lists and maps fine -- annotations and merges are gone.
  - Removing a trip from the log is a row in annotations.db (trip_removals),
    never a change under the data root: the files stay, and a restore is
    deleting the row. _load_trips() is the one place removed trips leave the
    log, so the list, totals, exports and merge suggestions all agree.
  - A closed year is a tax_years row. While it exists, annotate, merge,
    undo merge, remove and restore refuse trips in that year (409) for both
    roles; viewing, exports and place lookup are untouched.
  - Trips are identified in URLs by <ym>/<id> (e.g. 2026-04/2026-04-14...).
    Both components are strictly validated to prevent path traversal.

Authentication:
  - The environment login is the admin; everyday editor logins live in
    annotations.db. Roles are resolved from current server state on every
    request. All registered private endpoints declare their allowed roles.
    Passwords are stored only as hashes, never as plaintext.
  - Sessions are signed with VIEWER_SECRET_KEY. That key MUST be supplied
    from the environment and be stable: gunicorn runs multiple workers,
    and a per-process random key would mean a session minted by worker A
    is rejected by worker B.
  - A session is tied to the password hash it logged in under, so setting
    a new VIEWER_PASSWORD_HASH logs out everyone already logged in.
  - Repeated failed logins lock out the source IP. The counter lives in
    SQLite rather than process memory so all workers share one view of it.
  - This remains a LAN-only tool. The login is defence in depth against
    other devices on your network, not a licence to expose port 8091 to
    the internet -- the session cookie still travels over plain HTTP.
"""

import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
import sys
import json
import math
from xml.parsers import expat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    g,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from openpyxl import Workbook
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException

# Support source-tree importlib tests and the container's flat /app layout.
for import_root in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.version import server_version

from common.diagnostics import CatalogError, load_catalog
from geocoding import Geocoder, GeocodeError
import trip_log
import trip_merges
import users

try:
    DIAGNOSTICS_CATALOG = load_catalog()
except CatalogError as exc:
    raise RuntimeError("Cannot start viewer: missing or invalid diagnostics catalog.json") from exc
DIAGNOSTICS_CODES = {row["code"]: row for row in DIAGNOSTICS_CATALOG["codes"]}
DIAGNOSTICS_SEVERITIES = ("info", "warning", "error", "critical")
DIAGNOSTICS_PAGE_SIZE = 50
TRIP_HISTORY_PAGE_SIZE = 50


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
DATA_ROOT = Path(os.environ.get("INGEST_DATA_ROOT", "/data"))
DB_PATH = Path(os.environ.get("VIEWER_DB_PATH", "/db/annotations.db"))
GEOCODER = Geocoder(DB_PATH)

# --- Display timezone -----------------------------------------------
# Trips are STORED in UTC (that's what the GPX carries and what the
# ingest summary records). Everything a human reads -- the trip list,
# the detail heading, both exports -- is rendered in local time, because
# the artifact this produces is an IRS mileage record and "what day was
# that drive" has to match the day you actually drove.
#
# Must be a named IANA zone, never a fixed offset: most zones observe
# DST, so a hardcoded -08:00 would be an hour wrong for half the year.
# It comes from DISPLAY_TZ in .env, which install.sh asks for. There's no
# default, because whichever zone was guessed would be wrong for someone.
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
    # Fail loudly at import. Silently falling back to UTC would reintroduce
    # the exact off-by-a-day bug this exists to prevent, and it would do it
    # invisibly.
    raise RuntimeError(
        f"Timezone database missing or {DISPLAY_TZ_NAME!r} is not a valid "
        "IANA zone. The 'tzdata' package is in requirements.txt for this "
        "reason -- rebuild the image (docker compose build mileage-viewer)."
    ) from exc

# --- Authentication -------------------------------------------------
VIEWER_USERNAME = os.environ.get("VIEWER_USERNAME", "").strip()
VIEWER_PASSWORD_HASH = os.environ.get("VIEWER_PASSWORD_HASH", "").strip()
VIEWER_SECRET_KEY = os.environ.get("VIEWER_SECRET_KEY", "").strip()

# Lockout policy: after MAX_FAILS bad passwords from one IP, refuse that
# IP for LOCKOUT_MINUTES regardless of whether the next guess is correct.
MAX_FAILS = 5
LOCKOUT_MINUTES = 15

# Only /health and the login page itself are reachable without a session.
# /health stays open for Docker and reports the public server build version.
# Everything else -- including /static and GPX files -- needs a session.
PUBLIC_ENDPOINTS = {"health", "login"}

# Keep all role decisions here. New endpoints are denied until declared.
ENDPOINT_ROLES = {
    "static": {"admin", "editor"},
    "index": {"admin", "editor"},
    "trip_view": {"admin", "editor"},
    "trip_gpx": {"admin", "editor"},
    "annotate": {"admin", "editor"},
    "lookup_place": {"admin", "editor"},
    "merge_trip": {"admin", "editor"},
    "split_trip": {"admin", "editor"},
    "export_csv": {"admin", "editor"},
    "export_xlsx": {"admin", "editor"},
    "device_health": {"admin", "editor"},
    "logout": {"admin", "editor"},
    "admin": {"admin"},
    "admin_removed": {"admin"},
    "admin_history": {"admin"},
    "admin_year": {"admin"},
    "close_year": {"admin"},
    "reopen_year": {"admin"},
    "remove_trip": {"admin"},
    "restore_trip": {"admin"},
    "account_password": {"admin", "editor"},
}
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def _startup_check():
    """Refuse to boot misconfigured rather than boot without a password.

    A viewer that silently starts with auth broken is worse than one that
    doesn't start at all, because you'd never notice.
    """
    problems = []
    if not VIEWER_USERNAME:
        problems.append("VIEWER_USERNAME is not set.")
    if not VIEWER_PASSWORD_HASH:
        problems.append("VIEWER_PASSWORD_HASH is not set.")
    elif "$" not in VIEWER_PASSWORD_HASH:
        # A werkzeug hash always contains '$' separators. If they're gone,
        # something ate them -- almost always shell or Compose variable
        # expansion on an unquoted value in viewer.env.
        problems.append(
            "VIEWER_PASSWORD_HASH looks mangled (no '$' found). Wrap the "
            "value in single quotes in viewer.env, e.g. "
            "VIEWER_PASSWORD_HASH='scrypt:32768:8:1$...'"
        )
    if len(VIEWER_SECRET_KEY) < 32:
        problems.append(
            "VIEWER_SECRET_KEY must be set to at least 32 characters "
            "(generate with: openssl rand -hex 32)."
        )
    if problems:
        raise RuntimeError(
            "mileage-viewer refusing to start:\n  - " + "\n  - ".join(problems)
        )

# These are user classifications for recordkeeping, not tax determinations.
CATEGORIES = ("business", "personal", "other")

# Strict patterns for the URL components, so a trip path can never escape
# DATA_ROOT via "..", absolute paths, or odd characters.
YM_RE = re.compile(r"^\d{4}-\d{2}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_startup_check()

app = Flask(__name__)
app.secret_key = VIEWER_SECRET_KEY
app.config.update(
    SESSION_COOKIE_NAME="mileage_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Deliberately NOT Secure: this is plain HTTP on the LAN. If you ever
    # put a TLS reverse proxy in front, set SESSION_COOKIE_SECURE=True.
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    # POST bodies hold login fields or short trip annotations. Anything larger
    # is either a mistake or an attack.
    MAX_CONTENT_LENGTH=64 * 1024,
)


# ---------------------------------------------------------------------
# Annotations database (the only thing this service writes)
# ---------------------------------------------------------------------


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # closing() actually closes the connection on exit; sqlite3's own
    # context manager only commits/rolls back the transaction.
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        # Multiple Gunicorn workers can start together. Serialize the schema
        # check and migration so neither tries to add the same column twice.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                trip_key    TEXT PRIMARY KEY,
                category    TEXT,
                purpose_note TEXT,
                updated_at  TEXT
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)")}
        if "destination" not in columns:
            conn.execute("ALTER TABLE annotations ADD COLUMN destination TEXT NOT NULL DEFAULT ''")
        users.init_schema(conn)
        Geocoder.init_schema(conn)
        trip_merges.init_schema(conn)
        trip_log.init_schema(conn)
        # Failed-login tracking. This lives in SQLite, not in a module
        # global, because gunicorn runs 2 workers -- an in-memory counter
        # would give an attacker MAX_FAILS attempts per worker and would
        # reset every restart.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip           TEXT PRIMARY KEY,
                fails        INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT
            )
            """
        )


def get_annotations():
    """Return categories, notes and destinations keyed by trip path."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT trip_key, category, purpose_note, destination FROM annotations"
        ).fetchall()
    return {r["trip_key"]: dict(r) for r in rows}


def upsert_annotation(trip_key, category, purpose_note, destination=""):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            INSERT INTO annotations (trip_key, category, purpose_note, destination, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trip_key) DO UPDATE SET
                category = excluded.category,
                purpose_note = excluded.purpose_note,
                destination = excluded.destination,
                updated_at = excluded.updated_at
            """,
            (trip_key, category, purpose_note, destination, datetime.now(timezone.utc).isoformat()),
        )


# ---------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------


def _client_ip():
    """Source IP for lockout accounting.

    No proxy header is trusted here. The service is meant to be reached
    directly on the LAN, and honouring X-Forwarded-For would let anyone
    dodge the lockout by inventing a new address on every request.
    """
    return request.remote_addr or "unknown"


def _lock_seconds_remaining(ip):
    """Seconds this IP is still locked out for; 0 if it may try again."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE ip = ?", (ip,)
        ).fetchone()
    if not row or not row[0]:
        return 0
    try:
        until = datetime.fromisoformat(row[0])
    except ValueError:
        return 0
    remaining = (until - datetime.now(timezone.utc)).total_seconds()
    return int(remaining) if remaining > 0 else 0


def _record_failure(ip):
    """Count a bad password and lock the IP once it hits MAX_FAILS."""
    now = datetime.now(timezone.utc)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        # Reserve the writer before reading: workers must not both read
        # the same count, then overwrite each other's increment.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT fails, locked_until FROM login_attempts WHERE ip = ?", (ip,)
        ).fetchone()
        # Another request may have established the lock while this request
        # was checking its password. Its failure must not clear that lock.
        if row and row[1] and datetime.fromisoformat(row[1]) > now:
            return
        fails = (row[0] if row else 0) + 1
        if fails >= MAX_FAILS:
            locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
            fails = 0  # start a fresh count once the lock expires
        else:
            locked_until = None
        conn.execute(
            """
            INSERT INTO login_attempts (ip, fails, locked_until)
            VALUES (?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                fails = excluded.fails,
                locked_until = excluded.locked_until
            """,
            (ip, fails, locked_until),
        )


def _clear_failures(ip):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))


def _safe_equal(a, b):
    """Constant-time string comparison that accepts any input.

    hmac.compare_digest raises TypeError on str containing non-ASCII
    characters, which turned a username like "phíl" into a 500. Comparing
    the UTF-8 bytes keeps it constant-time and makes it a plain mismatch.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _csrf_token():
    """Per-session CSRF token, minted lazily and reused."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _password_tag():
    """Short HMAC of the password hash, stored in the session at login.

    A session counts only while its tag matches, so a new
    VIEWER_PASSWORD_HASH logs out every existing session, not just future
    logins. Keyed with the secret key because the session cookie is signed
    but readable, and it shouldn't carry anything derived from the hash
    alone. The signature already stops forgery, so 128 bits is plenty.
    """
    return hmac.new(
        VIEWER_SECRET_KEY.encode("utf-8"),
        VIEWER_PASSWORD_HASH.encode("utf-8"),
        "sha256",
    ).hexdigest()[:32]


def _is_logged_in():
    # The cookie identifies a session, never grants a role on its own.
    g.role = None
    g.user = None
    if session.get("kind") == "editor":
        username = session.get("user", "")
        try:
            users.validate_username(username, VIEWER_USERNAME)
        except users.UserError:
            return False
        user = users.get_user(DB_PATH, username)
        if (user is not None and not user["disabled"]
                and _safe_equal(session.get("pw_tag", ""), users.session_tag(VIEWER_SECRET_KEY, user))):
            g.role, g.user = "editor", user["username"]
            return True
        return False
    if (bool(VIEWER_USERNAME) and session.get("user") == VIEWER_USERNAME
            and _safe_equal(session.get("pw_tag", ""), _password_tag())):
        g.role, g.user = "admin", VIEWER_USERNAME
        return True
    return False


def _valid_csrf():
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    # Empty strings must not authenticate a request with no prior session.
    # Compare bytes so malformed non-ASCII input is rejected, not a 500.
    return bool(sent and expected) and _safe_equal(sent, expected)


def _safe_next(target):
    """Only allow same-site relative redirects after login.

    Rejects absolute URLs and protocol-relative '//evil.com' so the login
    form can't be used as an open redirect.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    return target


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    # Unknown endpoint (404) -- let Flask produce the 404 itself, but only
    # for logged-in users, so an anonymous caller can't probe for paths.
    if _is_logged_in():
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    abort(401)


@app.before_request
def _require_role():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if g.role not in ENDPOINT_ROLES.get(request.endpoint, set()):
        abort(403)


@app.before_request
def _csrf_protect():
    """Reject state-changing requests without a matching session token.

    Registered after _require_login and _require_role, so it only ever sees authenticated
    requests. /login validates its own token inside the view, where it can
    show a friendly message instead of a bare 400.
    """
    # No route matched, so no view will run and there is nothing to
    # protect: let Flask answer 404 (or 405) instead of a misleading 400.
    # Anonymous callers never get here, so this can't be used to probe.
    if request.endpoint is None:
        return None
    if request.method == "POST" and request.endpoint not in PUBLIC_ENDPOINTS:
        if not _valid_csrf():
            abort(400)


# ---------------------------------------------------------------------
# Reading trips off disk
# ---------------------------------------------------------------------


def _safe_trip_paths(ym, trip_id):
    """Validate URL components and return (gpx_path, json_path) under DATA_ROOT.

    Aborts with 404 on anything that doesn't match the strict patterns or
    that would resolve outside DATA_ROOT.
    """
    if not YM_RE.match(ym or "") or not ID_RE.match(trip_id or ""):
        abort(404)
    base = (DATA_ROOT / ym).resolve()
    root = DATA_ROOT.resolve()
    if root != base and root not in base.parents:
        abort(404)
    gpx = base / f"{trip_id}.gpx"
    json_path = base / f"{trip_id}.json"
    return gpx, json_path


# The summary fields the pages and exports read. read_summary() keeps only
# these, so a new column that reads another summary field must add it here.
SUMMARY_TEXT_FIELDS = (
    "trip_id", "device_id", "gpx_file", "start_time", "end_time",
    "timing_status", "timed_from", "recorded_until",
)
SUMMARY_NUMBER_FIELDS = (
    "distance_miles", "duration_seconds", "avg_speed_mph", "max_speed_mph", "num_points",
)


def _is_number(value):
    """A finite int or float. Python counts bool as an int; this doesn't."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too big to be a float
        return False


def _summary_problem(summary):
    """Why a parsed summary can't be used, or None if it can."""
    if not isinstance(summary, dict):
        return "not a JSON object"
    for field in SUMMARY_TEXT_FIELDS:
        if summary.get(field) is not None and not isinstance(summary[field], str):
            return f"{field} is not text"
    for field in SUMMARY_NUMBER_FIELDS:
        if summary.get(field) is not None and not _is_number(summary[field]):
            return f"{field} is not a finite number"
    if summary.get("timing_status") not in (None, "complete", "partial", "untimed"):
        return "timing_status is not recognized"
    return None


def read_summary(json_path):
    """Load a summary: (summary, problem), retaining identity on rejected JSON.

    Ingest writes summaries atomically, so a bad one means disk damage or
    a hand edit. One bad file must not 500 the trip list and both exports,
    so callers leave its miles out and report the trip as unreadable.
    """
    summary = None
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            summary = json.load(fh)
    except FileNotFoundError:
        problem = "the file is missing"
    except (OSError, ValueError) as exc:  # ValueError: bad JSON or bad UTF-8
        problem = str(exc)
    else:
        problem = _summary_problem(summary)
    if problem:
        app.logger.warning("Unreadable trip summary %s: %s", json_path, problem)
        # Preserve only usable adjacency evidence, never rejected mileage or
        # other summary values. Non-object/unparseable JSON has no identity.
        identity = {field: summary[field] for field in ("device_id", "start_time")
                    if isinstance(summary, dict) and isinstance(summary.get(field), str)}
        return identity or None, problem
    return {
        field: summary[field]
        for field in SUMMARY_TEXT_FIELDS + SUMMARY_NUMBER_FIELDS
        if field in summary
    }, None


def load_trips(conn=None, include_removed=False):
    if conn is not None:
        return _load_trips(conn, include_removed)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        # Serialize cache refreshes with edits/merges across gunicorn workers.
        conn.execute("BEGIN IMMEDIATE")
        return _load_trips(conn, include_removed)


def _load_trips(conn, include_removed=False):
    """Scan DATA_ROOT for trips and join annotations.

    Returns (trips, unreadable). trips is sorted by its date anchor descending,
    and each dict carries the summary fields plus: id, ym, trip_key,
    category, purpose_note, destination, has_gpx. unreadable holds the
    trips whose summary read_summary() rejected or couldn't find, sorted
    by trip_key, retaining usable device/start identity but no mileage, plus
    problem. Their miles are unknown, so they're reported, never counted.

    Trips the admin removed from the log are left out of both lists. With
    include_removed (the admin's views) they stay, each carrying a "removal"
    dict (reason, removed_by, removed_at); callers that feed totals, exports
    or merge suggestions must not ask for them.
    """
    annotations = {r["trip_key"]: dict(r) for r in conn.execute("SELECT * FROM annotations")}
    trips, unreadable = [], []

    # A GPX with no summary next to it is a trip too. Ingest writes the GPX
    # first, so a failed or lost summary write leaves one behind. (A page
    # load in the middle of an upload can catch one for a moment, too.)
    json_paths = [*DATA_ROOT.glob("*/*.json"), *(
        gpx.with_suffix(".json") for gpx in DATA_ROOT.glob("*/*.gpx")
        if not gpx.with_suffix(".json").exists()
    )]
    for json_path in json_paths:
        ym = json_path.parent.name
        trip_id = json_path.stem
        trip_key = f"{ym}/{trip_id}"
        ann = annotations.get(trip_key, {})
        summary, problem = read_summary(json_path)
        trip = {
            **(summary or {}),
            "id": trip_id,
            "ym": ym,
            "trip_key": trip_key,
            "category": ann.get("category", ""),
            "purpose_note": ann.get("purpose_note", ""),
            "destination": ann.get("destination", ""),
            "has_gpx": (json_path.with_suffix(".gpx")).exists(),
        }
        if problem:
            unreadable.append({**trip, "problem": problem})
        else:
            trips.append(trip)

    trips, unreadable = trip_merges.apply_merges(conn, DATA_ROOT, trips, unreadable)
    # Removal is keyed by the canonical key the list shows (a merged trip's
    # merge key, a part never), so filtering here, after the merges, hides a
    # merged trip as a whole. This is the only place that filters: the list,
    # totals, exports and next_trip all take their input from this function.
    # An unreadable trip that is removed also leaves next_trip's "an
    # unreadable summary may fall between" guard. That is intended: removing
    # it is how the admin clears that warning.
    removed = trip_log.removals(conn)
    if include_removed:
        trips = [{**t, "removal": removed[t["trip_key"]]} if t["trip_key"] in removed else t
                 for t in trips]
        unreadable = [{**t, "removal": removed[t["trip_key"]]} if t["trip_key"] in removed else t
                      for t in unreadable]
    else:
        trips = [t for t in trips if t["trip_key"] not in removed]
        unreadable = [t for t in unreadable if t["trip_key"] not in removed]
    trips.sort(key=lambda t: _trip_date_anchor(t) or "", reverse=True)
    unreadable.sort(key=lambda t: t["trip_key"])
    return trips, unreadable


def load_trips_for_merging(conn=None):
    """(trips, unreadable, removed) for merge suggestions and the merge route.

    trips and unreadable are the standard lists. removed holds the readable
    trips the admin removed, which next_trip must still see so a merge never
    bridges one. A removed unreadable trip is dropped: its miles are never
    counted, and removing it is how the admin clears the unreadable warning.
    """
    return split_removed(*load_trips(conn, include_removed=True))[:3]


def split_removed(trips, unreadable):
    """Split include_removed lists: (trips, unreadable, removed trips, removed unreadable)."""
    return ([t for t in trips if not t.get("removal")],
            [u for u in unreadable if not u.get("removal")],
            [t for t in trips if t.get("removal")],
            [u for u in unreadable if u.get("removal")])


# ---------------------------------------------------------------------
# Read-only diagnostics (ingest owns schema creation and all writes)
# ---------------------------------------------------------------------


def diagnostics_filters():
    # Bound integers before conversion (including Python's huge-int limit and
    # SQLite's signed offset limit). Reject duplicate/unknown parameters too.
    if set(request.args) - {"page", "code", "severity"} or any(
        len(request.args.getlist(key)) != 1 for key in request.args
    ):
        abort(400)
    page = request.args.get("page", "1")
    code = request.args.get("code", "")
    severity = request.args.get("severity", "")
    if not re.fullmatch(r"[1-9][0-9]{0,6}", page):
        abort(400)
    if code and not re.fullmatch(r"[1-7][0-9]{3}", code):
        abort(400)
    if severity and severity not in DIAGNOSTICS_SEVERITIES:
        abort(400)
    return int(page), int(code) if code else None, severity


def diagnostics_definition(code):
    return DIAGNOSTICS_CODES.get(code, {
        "code": code, "title": f"Unknown E{code}", "severity": None,
        "oled": [], "explanation": "This code is absent from the current server catalog.",
        "recovery": "Update the server catalog to learn its meaning.",
    })


def diagnostics_age(iso, now):
    stamp = _parse(iso)
    if stamp is None:
        raise ValueError("invalid diagnostics receipt timestamp")
    seconds = int((now - stamp).total_seconds())
    if seconds < 0:
        return "in the future (check server clock)"
    if seconds < 60:
        return f"{seconds} seconds ago"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def read_diagnostics(page, code, severity):
    """One consistent read, never initialize, migrate or recover this DB.

    mode=ro deliberately preserves SQLite locking and hot-journal checks;
    immutable=1 would skip those checks and could show inconsistent health.
    Closing is explicit: sqlite3's context manager does not close handles.
    """
    path = DATA_ROOT / "diagnostics" / "events.db"
    try:
        path.stat()
    except FileNotFoundError:
        return {"state": "absent"}
    now = datetime.now(timezone.utc)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        versions = db.execute("SELECT singleton, version FROM schema_version").fetchall()
        if [tuple(row) for row in versions] != [(1, 1)]:
            raise ValueError("unsupported diagnostics database schema version")
        devices = []
        for row in db.execute("SELECT * FROM devices ORDER BY device_id"):
            device = dict(row)
            device["contact_age"] = diagnostics_age(row["last_contact_at"], now)
            snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] is not None else None
            if snapshot is not None and not isinstance(snapshot, dict):
                raise ValueError("invalid diagnostics snapshot")
            if snapshot is not None and not isinstance(snapshot.get("counters", {}), dict):
                raise ValueError("invalid diagnostics counters")
            device["snapshot"] = snapshot
            device["snapshot_age"] = diagnostics_age(row["snapshot_received_at"], now) if snapshot is not None else None
            conditions = {r["code"]: dict(r) for r in db.execute(
                "SELECT code, state, episode_id FROM device_conditions WHERE device_id=? ORDER BY code",
                (row["device_id"],))}
            # Never-observed continuing conditions are unknown, not clear.
            # 7xxx codes are raised by the server monitor, not device conditions.
            for number, definition in DIAGNOSTICS_CODES.items():
                if definition["journal"] == "transitions" and number < 7000:
                    conditions.setdefault(number, {"code": number, "state": "unknown", "episode_id": None})
            groups = {state: [] for state in ("active", "unknown", "clear")}
            for number, condition in sorted(conditions.items()):
                groups[condition["state"]].append({**diagnostics_definition(number), **condition})
            device["groups"] = groups
            devices.append(device)

        clauses, params = [], []
        if code is not None:
            clauses.append("code=?")
            params.append(code)
        if severity:
            codes = [n for n, definition in DIAGNOSTICS_CODES.items() if definition["severity"] == severity]
            clauses.append("code IN (" + ",".join("?" for _ in codes) + ")" if codes else "0")
            params.extend(codes)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = db.execute(
            "SELECT device_id, boot_id, sequence, code, received_at, canonical_json FROM events" + where +
            " ORDER BY received_at DESC, device_id DESC, boot_id DESC, sequence DESC LIMIT ? OFFSET ?",
            (*params, DIAGNOSTICS_PAGE_SIZE + 1, (page - 1) * DIAGNOSTICS_PAGE_SIZE)).fetchall()
        events = []
        for row in rows[:DIAGNOSTICS_PAGE_SIZE]:
            event = json.loads(row["canonical_json"])
            if not isinstance(event, dict) or not isinstance(event.get("context", {}), dict):
                raise ValueError("invalid diagnostics event")
            diagnostics_age(row["received_at"], now)
            device_time = None
            if event.get("utc_ms") is not None and event.get("time_source") != "unknown":
                # Avoid floating-point rounding at the protocol's year-9999 bound.
                stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=event["utc_ms"])
                device_time = fmt_date(stamp.isoformat())
            events.append({**event, **dict(row), "definition": diagnostics_definition(row["code"]),
                           "device_time": device_time})
        return {"state": "normal", "devices": devices, "events": events,
                "has_next": len(rows) > DIAGNOSTICS_PAGE_SIZE}


# ---------------------------------------------------------------------
# Display helpers (exposed to templates)
# ---------------------------------------------------------------------


def fmt_date(iso):
    """Render a stored UTC timestamp in the display timezone, labeled.

    The %Z suffix resolves to PST or PDT depending on the date, which is
    why it's rendered from the zone rather than hardcoded -- it also makes
    the offset self-evident on the exported record.
    """
    dt = _to_local(_parse(iso))
    return dt.strftime("%Y-%m-%d %H:%M %Z") if dt else "—"


def fmt_day(iso):
    """A stored UTC timestamp's date in the display timezone."""
    dt = _to_local(_parse(iso))
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _trip_date_anchor(trip):
    """Year/sort anchor; a partial trip's observed date is not its start."""
    if _parse(trip.get("start_time")):
        return trip["start_time"]
    if trip.get("timing_status") == "partial":
        return trip.get("timed_from")
    return None


def fmt_trip_date(trip):
    if not _parse(trip.get("start_time")) and trip.get("timing_status") == "partial":
        return "Timed from " + fmt_date(trip.get("timed_from"))
    return fmt_date(trip.get("start_time"))


def year_basis(trip):
    if _parse(trip.get("start_time")):
        return "trip_start"
    if trip.get("timing_status") == "partial" and _parse(trip.get("timed_from")):
        return "first_recorded_time"
    return "unknown"


def fmt_local_iso(iso):
    """Full ISO-8601 in the display timezone, offset included.

    Used for the export's start_time/end_time columns. Keeping the offset
    means the value is still unambiguous -- a reader (or a spreadsheet)
    can recover the exact instant without knowing our conventions.
    """
    dt = _to_local(_parse(iso))
    return dt.isoformat() if dt else ""


def fmt_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def fmt_speed(value):
    return "—" if value is None else f"{value:.1f}"


def _parse(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_local(dt):
    """Convert an instant to the display timezone.

    A naive datetime is treated as UTC. That's the storage convention, and
    guessing "local" for a bare timestamp is how dates silently drift.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


def _history_trip_keys(conn, trip_key, parts=()):
    """Keys that identify the current trip and its live merge aliases."""
    canonical = trip_merges.canonical_key(conn, trip_key)
    keys = {trip_key, canonical, *parts}
    if not parts:
        keys.update(trip_merges.parts(conn, canonical))
    for (alias_key,) in conn.execute("SELECT alias_key FROM trip_merge_aliases"):
        if trip_merges.canonical_key(conn, alias_key) == canonical:
            keys.add(alias_key)
    return canonical, keys


def _history_row_dict(row):
    fields = ("id", "trip_key", "actor", "actor_role", "at", "action", "details")
    return dict(row) if isinstance(row, sqlite3.Row) else dict(zip(fields, row))


def _history_order(value):
    stamp = _parse(value)
    if stamp is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _related_history_rows(conn, trip_key, parts=()):
    """History rows for a trip, its parts, aliases, and merge/split events."""
    _, keys = _history_trip_keys(conn, trip_key, parts)
    rows_by_id = {}
    ordered_keys = sorted(keys)
    # A merge can contain many recordings. Keep each IN list below SQLite's
    # conservative parameter limit instead of relying on a build-time limit.
    for start in range(0, len(ordered_keys), 400):
        chunk = ordered_keys[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT id, trip_key, actor, actor_role, at, action, details FROM trip_history "
            f"WHERE trip_key IN ({placeholders})", chunk,
        ):
            item = _history_row_dict(row)
            rows_by_id[item["id"]] = item
    for row in conn.execute(
        "SELECT id, trip_key, actor, actor_role, at, action, details FROM trip_history "
        "WHERE action IN ('merge', 'split')"
    ):
        item = _history_row_dict(row)
        try:
            details = json.loads(item["details"])
        except (TypeError, ValueError):
            details = {}
        item["parsed_details"] = details
        if set(details.get("parts") or ()) & keys:
            rows_by_id[item["id"]] = item
    result = list(rows_by_id.values())
    for item in result:
        if "parsed_details" not in item:
            try:
                item["parsed_details"] = json.loads(item["details"])
            except (TypeError, ValueError):
                item["parsed_details"] = {}
    result.sort(key=lambda item: (_history_order(item["at"]), item["id"]), reverse=True)
    return result


def _last_edited(conn, trip):
    """Find the latest edit across a trip's current and historical merge keys."""
    rows = _related_history_rows(conn, trip["trip_key"], trip.get("parts", ()))
    if rows:
        row = rows[0]
        return {"actor": row["actor"], "actor_role": row["actor_role"],
                "at": row["at"], "pre_history": False}
    _, keys = _history_trip_keys(conn, trip["trip_key"], trip.get("parts", ()))
    ordered_keys = sorted(keys)
    fallback = []
    for table in ("annotations", "trip_merges"):
        for start in range(0, len(ordered_keys), 400):
            chunk = ordered_keys[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            fallback.extend(row[0] for row in conn.execute(
                f"SELECT updated_at FROM {table} WHERE trip_key IN ({placeholders}) "
                "AND updated_at IS NOT NULL AND updated_at != ''", chunk,
            ))
    fallback = [stamp for stamp in fallback if _parse(stamp)]
    if not fallback:
        return None
    at = max(fallback, key=_history_order)
    return {"actor": None, "actor_role": None, "at": at, "pre_history": True}


def _set_export_last_edited(conn, trip):
    info = _last_edited(conn, trip)
    trip["last_edited"] = info
    trip["last_edited_by"] = (info["actor"] if info and not info["pre_history"] else
                              "unknown" if info else "")
    trip["last_edited_at"] = fmt_local_iso(info["at"]) if info else ""


def history_filters():
    """Validate bounded paging and a single optional canonical trip key."""
    if set(request.args) - {"page", "trip"} or any(
        len(request.args.getlist(key)) != 1 for key in request.args
    ):
        abort(400)
    raw_page = request.args.get("page", "1")
    if not re.fullmatch(r"[1-9][0-9]{0,6}", raw_page):
        abort(400)
    trip = request.args.get("trip", "")
    if trip and not re.fullmatch(r"[0-9]{4}-[0-9]{2}/[A-Za-z0-9_-]+", trip):
        abort(400)
    return int(raw_page), trip or None


def valid_year(raw_year):
    """A four-digit calendar year (not 0000) as an int, else 400."""
    if not re.fullmatch(r"[0-9]{4}", raw_year or "") or int(raw_year) == 0:
        abort(400, description="Year must be a four-digit calendar year.")
    return int(raw_year)


def selected_year():
    raw_year = request.args.get("year", "")
    return valid_year(raw_year) if raw_year else None


def _may_start_in(ym, year):
    """Whether a trip filed under folder ym could have started in year.

    Ingest files each trip under its local END month, and a New Year's
    Eve drive can end in January. A trip with an unreadable summary has no
    start time, so its folder is the only clue to its year.
    """
    return ym.startswith(f"{year}-") or ym == f"{year + 1}-01"


def _folder_years(ym):
    """The years a trip with an unreadable summary may belong to, by folder."""
    if not YM_RE.match(ym):
        return set()  # not an ingest folder: no evidence of a year
    folder_year = int(ym[:4])
    return {folder_year, folder_year - 1} if ym.endswith("-01") else {folder_year}


def _trip_local_year(trip):
    """The year the list and exports file a readable trip under, or None."""
    local = _to_local(_parse(_trip_date_anchor(trip)))
    return local.year if local else None


def year_members(trips, unreadable, year):
    """The trips of one local year: the single rule behind list, exports and review."""
    return ([t for t in trips if _trip_local_year(t) == year],
            [t for t in unreadable if _may_start_in(t["ym"], year)])


def selected_trips(include_history=False):
    """Use local start year, or first recorded time for partly timed trips.

    Returns (trips, unreadable, year, years). Unreadable trips have no
    start time, so they're the exception: they go by folder.
    """
    year = selected_year()
    trips, unreadable = load_trips()
    years = {y for y in map(_trip_local_year, trips) if y}
    selected = trips
    if year:
        years.add(year)
        selected, unreadable = year_members(trips, unreadable, year)
    if include_history:
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            for trip in selected + unreadable:
                _set_export_last_edited(conn, trip)
    return selected, unreadable, year, sorted(years, reverse=True)


def mileage_totals(trips):
    """Include unset classifications so every selected mile is accounted for."""
    totals = {category: 0.0 for category in (*CATEGORIES, "unclassified")}
    for trip in trips:
        category = trip.get("category")
        bucket = category if category in CATEGORIES else "unclassified"
        totals[bucket] += trip.get("distance_miles") or 0
    # Sum before rounding: per-trip display rounding must not lose mileage.
    totals["all"] = sum(totals.values())
    return {category: round(miles, 3) for category, miles in totals.items()}


# ---------------------------------------------------------------------
# Closed years
# ---------------------------------------------------------------------


def _recording_years(key):
    """Years the original recording filed under key may belong to.

    A merged trip hides its parts from the loaded lists, so a part's year has
    to come from its own summary file, by the same rule as any other trip.
    """
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}/[A-Za-z0-9_-]+", key or ""):
        return set()
    summary, problem = read_summary(DATA_ROOT / f"{key}.json")
    if problem:
        return _folder_years(key.split("/")[0])
    year = _trip_local_year(summary)
    return {year} if year else set()


def trip_years(trip):
    """Every year a trip counts as belonging to, for the closed-year lock.

    A readable trip belongs to exactly the year the list and exports file it
    under. An unreadable one goes by its folder: either candidate year counts.
    A merged trip also counts as belonging to the year of each of its parts,
    so a merge can't be used to edit a trip in a closed year. A trip with no
    date anchor has no year here, so it is never locked: the list and the
    exports file it only under "All years" either.
    """
    if "problem" in trip:
        years = _folder_years(trip["ym"])
    else:
        year = _trip_local_year(trip)
        years = {year} if year else set()
    for part in trip.get("parts", ()):
        years |= _recording_years(part)
    return years


def locked_year(trip, closed):
    """The earliest closed year the trip belongs to, or None. closed: closed_years()."""
    hit = trip_years(trip) & closed.keys()
    return min(hit) if hit else None


def _abort_if_year_closed(closed, *trips):
    """409 for an edit that touches a trip in a closed year."""
    for trip in trips:
        year = locked_year(trip, closed)
        if year:
            abort(409, description=(
                f"{year} is closed for edits. The admin can reopen it on the year review page."))


def _abort_if_key_in_closed_year(conn, *keys):
    """The same refusal for routes that only have the trip's canonical key.

    Caller holds its BEGIN IMMEDIATE transaction and has set conn.row_factory
    to sqlite3.Row. A key that matches no trip is left to the route's own
    404/409, and with no closed year nothing is loaded.
    """
    closed = trip_log.closed_years(conn)
    if not closed:
        return
    trips, unreadable = load_trips(conn, include_removed=True)
    by_key = {t["trip_key"]: t for t in trips + unreadable}
    _abort_if_year_closed(closed, *(by_key[key] for key in keys if key in by_key))


app.jinja_env.globals.update(
    fmt_date=fmt_date, fmt_day=fmt_day, fmt_trip_date=fmt_trip_date, fmt_duration=fmt_duration,
    fmt_speed=fmt_speed, csrf_token=_csrf_token
)


# ---------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------

THEME_HEAD = """
<script id="theme-init">
  (function () {
    var root = document.documentElement;
    var mode;
    try { mode = localStorage.getItem('viewer-theme'); } catch (e) {}
    if (mode === 'light' || mode === 'dark') root.dataset.theme = mode;
    document.addEventListener('DOMContentLoaded', function () {
      var button = document.querySelector('[data-theme-toggle]');
      function label() {
        var current = root.dataset.theme || 'auto';
        button.textContent = 'Theme: ' + current[0].toUpperCase() + current.slice(1);
      }
      label();
      button.addEventListener('click', function () {
        var next = {auto: 'light', light: 'dark', dark: 'auto'}[root.dataset.theme || 'auto'];
        try {
          if (next === 'auto') localStorage.removeItem('viewer-theme');
          else localStorage.setItem('viewer-theme', next);
        } catch (e) { next = 'auto'; }
        if (next === 'auto') delete root.dataset.theme;
        else root.dataset.theme = next;
        label();
      });
    });
  }());
</script>
"""

THEME_BUTTON = '<button type="button" class="theme-toggle" data-theme-toggle>Theme: Auto</button>'

ACCOUNT_BUTTON = """
{% if g.role %}
<span class="account-name">{{ g.user }} ({{ 'Admin' if g.role == 'admin' else 'Everyday' }})</span>
{% if g.role == 'admin' %}<a class="account-link" href="{{ url_for('admin') }}">Admin</a>
{% else %}<a class="account-link" href="{{ url_for('account_password') }}">Change password</a>{% endif %}
{% endif %}
"""

DEVICE_HEALTH_BUTTON = """
{% if request.endpoint == 'device_health' %}
<span class="device-health" aria-current="page">Device health</span>
{% else %}
<a class="device-health" href="{{ url_for('device_health') }}">Device health</a>
{% endif %}
"""

# Reuse the dark palette for OS preference and the explicit override.
DARK_THEME_CSS = """
    --page-bg: #15191e; --text: #e9ecef; --header-bg: #123b60;
    --table-head-bg: #252c34; --border: #414b57; --row-hover-bg: #20262d;
    --tag-bg: #343e49; --business-bg: #183d25; --business-text: #b2f2bb;
    --personal-bg: #173952; --personal-text: #a5d8ff;
    --other-bg: #493916; --other-text: #ffe69c; --muted: #adb5bd;
    --warn-bg: #421e24; --warn-text: #ffc9c9; --warn-border: #88434e;
    --button-bg: #1864ab; --secondary-bg: #495057;
    --input-bg: #20262d; --input-text: #e9ecef; --input-border: #66717e;
    --login-bg: #15191e; --card-bg: #20262d; --heading: #a5d8ff;
    --card-shadow: rgba(0,0,0,.35); --login-input-border: #66717e;
    --map-bg: #20262d; --map-border: #414b57;
    --tile-filter: invert(1) hue-rotate(180deg) brightness(.95) contrast(.9);
    --map-control-bg: #252c34; --map-control-text: #e9ecef;
    --map-control-hover: #343e49; --map-control-disabled: #adb5bd;
    --map-control-border: #66717e; --attribution-bg: rgba(32,38,45,.95);
    --attribution-text: #e9ecef; --map-link: #a5d8ff;
"""

THEME_CSS = """
  :root {
    color-scheme: light dark;
    --page-bg: #fff; --text: #1a1a1a; --header-bg: #1864ab; --header-text: #fff;
    --header-button-bg: rgba(0,0,0,.18); --header-button-text: #fff;
    --header-button-border: rgba(255,255,255,.55); --header-button-hover: rgba(0,0,0,.30);
    --table-head-bg: #f1f3f5; --border: #e0e0e0; --row-hover-bg: #f8f9fa;
    --tag-bg: transparent; --business-bg: #d3f9d8; --business-text: #277c38;
    --personal-bg: #e7f5ff; --personal-text: #1971c2;
    --other-bg: #fff3bf; --other-text: #ac5900; --muted: #6c747c;
    --warn-bg: #fff5f5; --warn-text: #c92a2a; --warn-border: #ffc9c9;
    --button-bg: #1864ab; --button-text: #fff; --secondary-bg: #495057;
    --input-bg: #fff; --input-text: #000; --input-border: #767676;
    --login-bg: #f1f3f5; --card-bg: #fff; --heading: #1864ab;
    --card-shadow: rgba(0,0,0,.10); --login-input-border: #ced4da;
    --map-bg: #ddd; --map-border: #ced4da; --tile-filter: none;
    --map-control-bg: #fff; --map-control-text: #000; --map-control-hover: #f4f4f4;
    --map-control-disabled: #707070; --map-control-border: #ccc;
    --attribution-bg: rgba(255,255,255,.8); --attribution-text: #333; --map-link: #005d83;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
""" + DARK_THEME_CSS + """
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
""" + DARK_THEME_CSS + """
  }
  :root[data-theme="light"] { color-scheme: light; }
  .server-version { color: var(--muted); font-size: 12px; text-align: center;
                    padding: 16px; overflow-wrap: anywhere; }
"""

BASE_CSS = THEME_CSS + """
  body { font-family: system-ui, sans-serif; margin: 0; color: var(--text); background: var(--page-bg); }
  header { background: var(--header-bg); color: var(--header-text); padding: 14px 22px;
           display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  header a { color: var(--header-text); text-decoration: none; font-weight: 600; }
  .header-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
                    justify-content: flex-end; margin-left: auto; }
  .header-version { color: var(--header-text); opacity: .85; font-size: 12px; white-space: nowrap; }
  header form.logout { margin: 0; }
  header form.logout button, header .theme-toggle, header .device-health, header .account-link {
           background: var(--header-button-bg); color: var(--header-button-text);
           border: 1px solid var(--header-button-border); padding: 5px 12px;
           border-radius: 5px; font: 13px/1.4 system-ui, sans-serif; white-space: nowrap; }
  header form.logout button, header .theme-toggle, header a.device-health, header .account-link { cursor: pointer; }
  header form.logout button:hover, header .theme-toggle:hover, header a.device-health:hover, header .account-link:hover {
           background: var(--header-button-hover); }
  header form.logout button:focus-visible, header .theme-toggle:focus-visible, header a.device-health:focus-visible, header .account-link:focus-visible {
           outline: 2px solid var(--header-text); outline-offset: 3px; }
  header .device-health[aria-current="page"] {
           background: var(--header-text); color: var(--header-bg); }
  .wrap { padding: 22px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { background: var(--table-head-bg); font-size: 13px; text-transform: uppercase; letter-spacing: .03em; }
  tr:hover td { background: var(--row-hover-bg); }
  .tag { background: var(--tag-bg); font-size: 12px; padding: 2px 8px; border-radius: 10px; }
  .business { background: var(--business-bg); color: var(--business-text); }
  .personal { background: var(--personal-bg); color: var(--personal-text); }
  .other    { background: var(--other-bg); color: var(--other-text); }
  .muted { color: var(--muted); }
  .warn { background: var(--warn-bg); color: var(--warn-text); border: 1px solid var(--warn-border);
          padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
  .warn ul { margin: 6px 0 0; padding-left: 20px; }
  .btn { display: inline-block; background: var(--button-bg); color: var(--button-text); padding: 8px 14px;
         border-radius: 6px; text-decoration: none; border: none; cursor: pointer; font-size: 14px; }
  .btn.secondary { background: var(--secondary-bg); }
  button.btn:disabled, button.btn[aria-disabled="true"] { opacity: .65; cursor: default; }
  .endpoint-place { margin: 16px 0; overflow-wrap: anywhere; }
  .toolbar { margin-bottom: 16px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .stats { display: flex; gap: 26px; flex-wrap: wrap; margin: 12px 0; }
  .stat b { display: block; font-size: 22px; }
  .stat span { color: var(--muted); font-size: 13px; }
  #map { height: 460px; border: 1px solid var(--map-border); border-radius: 8px; background: var(--map-bg); }
  .leaflet-tile-pane { filter: var(--tile-filter); }
  .leaflet-bar a { background: var(--map-control-bg); color: var(--map-control-text);
                   border-bottom-color: var(--map-control-border); }
  .leaflet-bar a:hover, .leaflet-bar a:focus { background: var(--map-control-hover); }
  .leaflet-bar a.leaflet-disabled { background: var(--map-control-hover); color: var(--map-control-disabled); }
  .leaflet-container .leaflet-control-attribution { background: var(--attribution-bg); color: var(--attribution-text); }
  .leaflet-control-attribution a { color: var(--map-link); }
  form.annotate { margin-top: 18px; max-width: 520px; }
  label { display: block; margin: 10px 0 4px; font-weight: 600; font-size: 14px; }
  select, textarea, input[type=text], input[type=password] { width: 100%; padding: 8px; font-size: 14px; box-sizing: border-box;
      background: var(--input-bg); color: var(--input-text);
      border: 1px solid var(--input-border); border-radius: 4px; }
"""

@app.context_processor
def version_context():
    return {"server_version": server_version()}


VERSION_FOOTER = '<footer class="server-version">Server {{ server_version }}</footer>'
VERSION_HEADER = '<div class="header-version">Server {{ server_version }}</div>'


ACCOUNT_HEADER = """
{% if g.role %}<header><a href="{{ url_for('index') }}">Trips</a><div class="header-actions">
""" + VERSION_HEADER + DEVICE_HEALTH_BUTTON + ACCOUNT_BUTTON + THEME_BUTTON + """
<form class="logout" method="post" action="{{ url_for('logout') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit">Log out</button>
</form></div></header>{% endif %}
"""

PASSWORD_FIELDS = """
<label>New password <input type="password" name="password" minlength="8" maxlength="1024" autocomplete="new-password" required></label>
<label>Confirm new password <input type="password" name="confirm_password" minlength="8" maxlength="1024" autocomplete="new-password" required></label>
"""

ACCOUNT_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>""" + THEME_HEAD + """<style>""" + BASE_CSS + """
.login-form { max-width: 420px; margin-bottom: 20px; }
.login-table { overflow-x: auto; }
.login-table td { vertical-align: top; }
.login-table form { margin: 8px 0; }
.login-table input[type=password] { min-width: 180px; }
.account-name { overflow-wrap: anywhere; }
</style></head><body>
""" + ACCOUNT_HEADER + """<main class="wrap"><h1>{{ title }}</h1>
{% if message %}<p role="status">{{ message }}</p>{% endif %}
{% if title == 'Admin' %}<p>{{ admin_username }}: Admin (set in viewer.env)</p>
<p><a class="btn secondary" href="{{ url_for('admin_removed') }}">Removed trips</a></p>
<p><a class="btn secondary" href="{{ url_for('admin_history') }}">Change history</a></p>
<h2>Year review</h2>
<p class="muted">Check a year's log before filing, then close it so nothing in it changes by accident.</p>
<table class="year-table"><tr><th>Year</th><th>Status</th><th></th></tr>
{% for item in year_list %}<tr><td>{{ item.year }}</td>
<td>{% if item.closed %}Closed by {{ item.closed.closed_by }} on {{ fmt_day(item.closed.closed_at) }}{% else %}Open{% endif %}</td>
<td><a class="btn secondary" href="{{ url_for('admin_year', year=item.year) }}">Review {{ item.year }}</a></td></tr>
{% else %}<tr><td colspan="3" class="muted">No trips yet.</td></tr>{% endfor %}</table>
<h2>Everyday logins</h2><div class="login-table">
<table><tr><th>Everyday login</th><th>Status</th><th>Created ({{ timezone }})</th><th>Actions</th></tr>
{% for user in logins %}<tr><td>{{ user.username }}</td><td>{{ 'disabled' if user.disabled else 'enabled' }}</td>
<td>{{ fmt_date(user.created_at) }}</td><td>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="username" value="{{ user.username }}">
<input type="hidden" name="action" value="reset-password">
""" + PASSWORD_FIELDS + """<button class="btn" type="submit">Reset password for {{ user.username }}</button></form>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="username" value="{{ user.username }}">
<input type="hidden" name="action" value="{{ 'enable' if user.disabled else 'disable' }}">
<button class="btn secondary" type="submit">{{ 'Enable' if user.disabled else 'Disable' }} {{ user.username }}</button></form>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="username" value="{{ user.username }}">
<input type="hidden" name="action" value="remove">
<label><input type="checkbox" name="confirm" value="yes" required> Confirm removing {{ user.username }}</label>
<button class="btn secondary" type="submit">Remove {{ user.username }}</button></form>
</td></tr>{% else %}<tr><td colspan="4">No everyday logins.</td></tr>{% endfor %}</table></div>
<h2>Add everyday login</h2><form method="post" class="login-form">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="action" value="add">
<label>Username <input type="text" name="username" maxlength="64" autocomplete="off" required></label>
""" + PASSWORD_FIELDS + """<button class="btn" type="submit">Add everyday login</button></form>
{% elif g.role == 'admin' %}<p>The admin password lives in viewer.env. Rerun <code>bash install.sh</code> and answer n to keeping the login to change it.</p>
{% else %}<form method="post" class="login-form">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label>Current password <input type="password" name="current_password" autocomplete="current-password" required></label>
""" + PASSWORD_FIELDS + """<button class="btn" type="submit">Change password</button></form>{% endif %}
</main></body></html>
"""


ERROR_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>{{ error.name }}</title>
""" + THEME_HEAD + """<style>""" + BASE_CSS + """</style></head><body>
""" + ACCOUNT_HEADER + """<main class="wrap"><h1>{{ error.code }} {{ error.name }}</h1>
<p>{{ error.description }}</p></main>
{% if not g.role %}""" + VERSION_FOOTER + """{% endif %}</body></html>
"""


@app.errorhandler(HTTPException)
def http_error(error):
    # Keep status and headers (such as Allow on 405), and include the footer
    # on error pages as well as the normal templates.
    response = error.get_response()
    response.set_data(render_template_string(ERROR_HTML, error=error))
    return response


LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Sign in · Mileage Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + THEME_HEAD + """<style>""" + THEME_CSS + """
  body { font-family: system-ui, sans-serif; background: var(--login-bg); color: var(--input-text); margin: 0;
         display: flex; min-height: 100vh; align-items: center; justify-content: center; }
  .card { background: var(--card-bg); padding: 30px 32px; border-radius: 10px; width: 320px;
          box-shadow: 0 2px 14px var(--card-shadow); }
  h1 { font-size: 19px; margin: 0 0 20px; color: var(--heading); }
  label { display: block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input { width: 100%; padding: 9px; font-size: 14px; box-sizing: border-box;
          border: 1px solid var(--login-input-border); border-radius: 5px;
          background: var(--input-bg); color: var(--input-text); }
  button { width: 100%; margin-top: 20px; background: var(--button-bg); color: var(--button-text); border: none;
           padding: 10px; border-radius: 6px; font-size: 15px; cursor: pointer; }
  .theme-toggle { width: auto; margin-top: 14px; padding: 5px 10px; font-size: 13px;
                  background: transparent; color: var(--muted);
                  border: 1px solid var(--login-input-border); border-radius: 6px; }
  .theme-toggle:hover { background: var(--row-hover-bg); border-color: var(--muted); }
  .err { background: var(--warn-bg); color: var(--warn-text); border: 1px solid var(--warn-border);
         padding: 9px 11px; border-radius: 5px; font-size: 13px; margin-bottom: 4px; }
</style></head><body>
  <div class="card">
    <h1>🚗 Mileage Tracker</h1>
    {% if error %}<p class="err">{{ error }}</p>{% endif %}
    <form method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" autofocus required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password"
             autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
    """ + THEME_BUTTON + VERSION_FOOTER + """
  </div>
</body></html>
"""

LIST_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Mileage Trips</title>
""" + THEME_HEAD + """<style>""" + BASE_CSS + """</style></head><body>
<header><a href="{{ url_for('index') }}">🚗 Mileage Tracker</a>
  <div class="header-actions">
    """ + VERSION_HEADER + DEVICE_HEALTH_BUTTON + ACCOUNT_BUTTON + THEME_BUTTON + """
    <form class="logout" method="post" action="{{ url_for('logout') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button type="submit">Log out</button>
    </form>
  </div>
</header>
<div class="wrap">
  {% if unreadable %}
  <div class="warn" role="alert">
    <b>{{ unreadable|length }} trip summary file(s) can't be read.</b>
    Their miles can't be counted, so the totals below may be short. The
    exports list these trips as unreadable, without miles. To rebuild a
    summary from its GPX, see DEPLOY_DEBIAN.md → Troubleshooting.
    <ul>
      {% for u in unreadable %}
      <li><code>{% if u.get('parts') %}{{ u.gpx_file }}{% else %}{{ u.trip_key }}.json{% endif %}</code>{% if not u.has_gpx %} (no GPX either){% endif %}: {{ u.problem }}
        {% if u.get('parts') %}
        <form method="post" action="{{ url_for('split_trip', ym=u.ym, trip_id=u.id, year=year or '') }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <label><input type="checkbox" name="confirm" value="yes" required>
            Restore original trips and annotations; remove the merged annotation.</label>
          <button class="btn" type="submit">Undo merge (split back)</button>
        </form>
        {% endif %}
        {% if g.role == 'admin' %}
        <form method="post" action="{{ url_for('remove_trip', ym=u.ym, trip_id=u.id, year=year or '') }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <label>Reason for removing it from the log
            <input type="text" name="reason" maxlength="500" required></label>
          <button class="btn secondary" type="submit">Remove from log</button>
        </form>
        {% endif %}
      </li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
  <div class="toolbar">
    <form method="get">
      <label for="year">Year (trip start or first recorded time in {{ timezone }})</label>
      <select name="year" id="year">
        <option value="">All years</option>
        {% for y in years %}<option value="{{ y }}" {% if year == y %}selected{% endif %}>{{ y }}</option>{% endfor %}
      </select>
      <button class="btn" type="submit">Apply</button>
    </form>
    {% if year_closed %}<span class="tag other">Closed for edits</span>{% endif %}
    {% if year and g.role == 'admin' %}<a class="btn secondary" href="{{ url_for('admin_year', year=year) }}">{{ year }} year review</a>{% endif %}
    <a class="btn" href="{{ url_for('export_csv', year=year or '') }}">Export CSV</a>
    <a class="btn secondary" href="{{ url_for('export_xlsx', year=year or '') }}">Export XLSX</a>
    <span class="muted">{{ trips|length }} trip(s) · {{ "%.3f"|format(totals.all) }} mi total</span>
  </div>
  <div class="stats" aria-label="Mileage totals for selected trips">
    {% for category, miles in totals.items() %}{% if category != 'all' %}
    <div class="stat"><b>{{ "%.3f"|format(miles) }}</b><span>{{ category }} miles</span></div>
    {% endif %}{% endfor %}
  </div>
  <p class="muted">Totals cover recorded trips in this selection. Partly timed trips use their first recorded timestamp
    for year selection; their actual start can be in an earlier year. Trips with no usable date appear only under All years.</p>
  <table>
    <tr><th>Date</th><th>Distance</th><th>Duration</th><th>Avg mph</th>
        <th>Purpose</th><th>Destination</th><th>Note</th><th></th></tr>
    {% for t in trips %}
    <tr>
      <td>{{ fmt_trip_date(t) }}{% if t.get('timing_status') == 'partial' %}<br><span class="muted">Partial timing</span>{% endif %}{% if t.get('parts') %}<br><code>{{ t.gpx_file }}</code>{% endif %}</td>
      <td>{{ "%.2f"|format(t.distance_miles or 0) }} mi</td>
      <td>{{ fmt_duration(t.duration_seconds) }}</td>
      <td>{{ fmt_speed(t.get('avg_speed_mph')) }}</td>
      <td>{% if t.category %}<span class="tag {{ t.category }}">{{ t.category }}</span>
          {% else %}<span class="muted">—</span>{% endif %}</td>
      <td>{{ t.destination or "" }}</td><td>{{ t.purpose_note or "" }}</td>
      <td><a class="btn" href="{{ url_for('trip_view', ym=t.ym, trip_id=t.id, year=year or '') }}">Map / Edit</a></td>
    </tr>
    {% endfor %}
    {% if not trips %}
    <tr><td colspan="8" class="muted">No trips in this selection.</td></tr>
    {% endif %}
  </table>
</div></body></html>
"""

TRIP_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Trip {{ t.get('trip_id') or t.id }}</title>
""" + THEME_HEAD + """
<link rel="stylesheet" href="{{ url_for('static', filename='leaflet.css') }}">
<style>""" + BASE_CSS + """</style></head><body>
<header><a href="{{ url_for('index', year=year or '') }}">← {{ year or 'All' }} trips</a>
  <div class="header-actions">
    """ + VERSION_HEADER + DEVICE_HEALTH_BUTTON + ACCOUNT_BUTTON + THEME_BUTTON + """
    <form class="logout" method="post" action="{{ url_for('logout') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button type="submit">Log out</button>
    </form>
  </div>
</header>
<div class="wrap">
  <h2>{{ fmt_trip_date(t) }}</h2>
  {% if last_edited %}
  <p class="muted">{% if last_edited.pre_history %}Edited before the change history began ({{ fmt_date(last_edited.at) }})
    {% else %}Last edited by {{ last_edited.actor }} on {{ fmt_date(last_edited.at) }}{% endif %}</p>
  {% endif %}
  {% if year_lock %}
  <div class="warn" role="status">{{ year_lock.year }} is closed for edits (closed by {{ year_lock.closed_by }} on {{ fmt_day(year_lock.closed_at) }}).
    Nobody can change, merge, remove or restore this trip until the admin reopens {{ year_lock.year }} on the year review page.</div>
  {% endif %}
  {% if removal %}
  <div class="warn" role="status">
    <b>Removed from the log</b> by {{ removal.removed_by }} on {{ fmt_date(removal.removed_at) }}: {{ removal.reason }}
    {% if not year_lock %}
    <form method="post" action="{{ url_for('restore_trip', ym=t.ym, trip_id=t.id, year=year or '') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn" type="submit">Restore to the log</button>
    </form>
    {% endif %}
  </div>
  {% endif %}
  {% if t.get('timing_status') == 'partial' %}
  <p class="muted">Partial timing: some points have no timestamp. Full-trip dates,
    duration and average speed are unknown. Recorded timestamps run from
    {{ fmt_date(t.timed_from) }} to {{ fmt_date(t.recorded_until) }}. Recorded distance is retained.
    Year selection uses the first recorded timestamp; the actual start can be in an earlier year.</p>
  {% endif %}
  <div class="stats">
    <div class="stat"><b>{{ "%.2f"|format(t.distance_miles or 0) }}</b><span>miles</span></div>
    <div class="stat"><b>{{ fmt_duration(t.duration_seconds) }}</b><span>duration</span></div>
    <div class="stat"><b>{{ fmt_speed(t.get('avg_speed_mph')) }}</b><span>avg mph</span></div>
    <div class="stat"><b>{{ "%.1f"|format(t.max_speed_mph or 0) }}</b><span>max mph</span></div>
    <div class="stat"><b>{{ t.num_points or 0 }}</b><span>points</span></div>
  </div>

  {% if t.has_gpx %}
  <div id="map"></div>
  {% else %}
  <p class="muted">GPX file missing — cannot draw the track.</p>
  {% endif %}

  {% if removal %}
  <p class="muted">This trip is out of the trip list, totals and exports, and can't be edited
    until it is restored. Its purpose, destination and note were: {{ t.category or '—' }},
    {{ t.destination or '—' }}, {{ t.purpose_note or '—' }}.</p>
  {% else %}
  <section aria-labelledby="places-heading">
    <h3 id="places-heading">Recorded endpoint places</h3>
    <p class="muted">Suggestions describe nearby mapped objects, not verified businesses or
      corrected parking locations. Check them before using them in a mileage record.</p>
    {% if geocoder_enabled %}
    <p>Each lookup sends only that endpoint's coordinates to the address provider configured
      by your server administrator. Results are saved locally. Nothing is sent until you click.</p>
    {% for endpoint, title in [('start', 'Start'), ('end', 'End')] %}
    <div class="endpoint-place" data-endpoint="{{ endpoint }}">
      <b>{{ title }}</b>
      {% if endpoint in places %}
      <p data-place-label role="status">{% if places[endpoint] is none %}Not looked up yet.{% elif places[endpoint] %}{{ places[endpoint] }}{% else %}No mapped address found.{% endif %}</p>
      <form data-place-lookup method="post" action="{{ url_for('lookup_place', ym=t.ym, trip_id=t.id) }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="endpoint" value="{{ endpoint }}">
        <button class="btn" type="submit" disabled>Look up {{ endpoint }} place</button>
      </form>
      {% else %}<p class="muted">No valid recorded endpoint is available.</p>{% endif %}
    </div>
    {% endfor %}
    <p class="muted">Address data: <a href="https://www.openstreetmap.org/copyright"
      target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors (ODbL)</a>.</p>
    <noscript>Enable JavaScript to look up places. You can still enter a destination below.</noscript>
    {% else %}
    <p class="muted">Address lookup is not configured. Ask your server administrator to set up
      a supported address provider, or enter a destination below.</p>
    {% endif %}
  </section>

  {% if year_lock %}
  {% if t.get('parts') %}
  <p>Merged from {{ t.parts|length }} recordings. Source GPX files: <code>{{ t.gpx_file }}</code></p>
  {% endif %}
  {% else %}
  <section aria-labelledby="merge-heading">
    <h3 id="merge-heading">Join a split drive</h3>
    {% if t.get('parts') %}
    <p>Merged from {{ t.parts|length }} recordings. Track breaks show unrecorded gaps;
      mileage bridges gaps using the same straight-line estimate as ingest.</p>
    <p>Source GPX files: <code>{{ t.gpx_file }}</code></p>
    <form method="post" action="{{ url_for('split_trip', ym=t.ym, trip_id=t.id, year=year or '') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <p><label><input type="checkbox" name="confirm" value="yes" required>
        Restore each original trip and annotation, removing the current merged annotation.</label></p>
      <button class="btn" type="submit">Undo merge (split back)</button>
    </form>
    {% endif %}
    {% if merge_next and t.has_gpx and merge_next.has_gpx %}
    <p>Next trip from this device: <a href="{{ url_for('trip_view', ym=merge_next.ym, trip_id=merge_next.id, year=year or '') }}">{{ fmt_trip_date(merge_next) }}</a>.
      Its start is within 15 minutes of this recording's end.</p>
    {% if merge_conflict %}
    <p>Both trips have annotations. Clear the purpose, note and destination on one trip and save before merging.</p>
    {% else %}
    <form method="post" action="{{ url_for('merge_trip', ym=t.ym, trip_id=t.id, year=year or '') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="next_trip" value="{{ merge_next.trip_key }}">
      <p>Save any annotation edits before merging. The saved annotation is kept;
        each original annotation is preserved for Undo.</p>
      <button class="btn" type="submit">Merge with next trip</button>
    </form>
    {% endif %}
    {% elif merge_block_reason %}
    <p class="warn" role="alert">{{ merge_block_reason }}</p>
    {% else %}
    <p class="muted">No eligible next trip. Both recordings need a known device and start time,
      with the next start no more than 15 minutes after this recording ends.</p>
    {% endif %}
  </section>
  {% endif %}

  {% if year_lock %}
  <section aria-labelledby="annotation-heading">
    <h3 id="annotation-heading">Purpose, destination and note</h3>
    <p>{{ t.category or '—' }}, {{ t.destination or '—' }}, {{ t.purpose_note or '—' }}</p>
  </section>
  {% else %}
  <form class="annotate" method="post"
        action="{{ url_for('annotate', ym=t.ym, trip_id=t.id, year=year or '') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label for="category">Purpose</label>
    <select name="category" id="category">
      <option value="">— unset —</option>
      {% for c in categories %}
      <option value="{{ c }}" {% if t.category == c %}selected{% endif %}>{{ c }}</option>
      {% endfor %}
    </select>
    <label for="destination">Destination / place</label>
    <input type="text" name="destination" id="destination" value="{{ t.destination or '' }}">
    {% if geocoder_enabled and 'end' in places %}
    <button type="button" class="btn secondary" id="use-end-place"
      {% if not places.get('end') %}disabled{% endif %}>Use end suggestion as destination</button>
    <p class="muted">Using a suggestion replaces the destination field above. Click Save to keep it.</p>
    {% endif %}
    <label for="purpose_note">Note (e.g. "Client visit — Acme Corp")</label>
    <textarea name="purpose_note" id="purpose_note" rows="3">{{ t.purpose_note or "" }}</textarea>
    <p><button class="btn" type="submit">Save</button></p>
  </form>
  {% endif %}
  {% if g.role == 'admin' and not year_lock %}
  <section aria-labelledby="remove-heading">
    <h3 id="remove-heading">Remove from log</h3>
    <p>Takes this trip out of the trip list, totals and exports for everyone. The GPX and
      summary files stay on the server, and you can restore the trip from the Removed trips page.</p>
    <form method="post" action="{{ url_for('remove_trip', ym=t.ym, trip_id=t.id, year=year or '') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label for="remove-reason">Reason (required)</label>
      <textarea name="reason" id="remove-reason" rows="2" maxlength="500" required></textarea>
      <p><button class="btn secondary" type="submit">Remove from log</button></p>
    </form>
  </section>
  {% endif %}
  {% endif %}
</div>

{% if geocoder_enabled %}
<script src="{{ url_for('static', filename='endpoint-places.js') }}"></script>
{% endif %}
{% if t.has_gpx %}
<script src="{{ url_for('static', filename='leaflet.js') }}"></script>
<script src="{{ url_for('static', filename='gpx.js') }}"></script>
<script>
  var map = L.map('map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  new L.GPX("{{ url_for('trip_gpx', ym=t.ym, trip_id=t.id) }}", {
    async: true,
    // Mileage maps need paths only. The GPX plugin renders waypoint
    // name/description as popup HTML, so never parse uploaded waypoints.
    gpx_options: { parseElements: ['track', 'route'], joinTrackSegments: false },
    marker_options: { startIconUrl: '', endIconUrl: '', shadowUrl: '' },
    polyline_options: { color: '#d9480f', weight: 4, opacity: 0.85 }
  }).on('loaded', function (e) {
    map.fitBounds(e.target.getBounds());
  }).on('error', function (e) {
    // textContent (not innerHTML) so the error string can't inject markup
    var p = document.createElement('p');
    p.style.padding = '1em';
    p.textContent = 'Could not load track: ' + e.err;
    var el = document.getElementById('map');
    el.replaceChildren(p);
  }).addTo(map);
</script>
{% endif %}
</body></html>
"""


# ---------------------------------------------------------------------
# Device health uses the same autoescaped inline templates as the trip pages.
# No stored value is markup, including JSON strings from a damaged/edited DB.
# ---------------------------------------------------------------------

DEVICE_HEALTH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Device health</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + THEME_HEAD + """<style>""" + BASE_CSS + """
  .health-table { overflow-x: auto; }
  .health-table td { vertical-align: top; overflow-wrap: anywhere; }
  .oled { white-space: pre-wrap; }
  .condition { border-bottom: 1px solid var(--border); padding: 8px 0; }
</style></head><body>
<header><a href="{{ url_for('index') }}">← Trips</a>
  <div class="header-actions">
    """ + VERSION_HEADER + DEVICE_HEALTH_BUTTON + ACCOUNT_BUTTON + THEME_BUTTON + """
    <form class="logout" method="post" action="{{ url_for('logout') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button type="submit">Log out</button>
    </form>
  </div>
</header><main class="wrap"><h1>Device health</h1>
{% if state == 'absent' %}
  <p>No diagnostics yet. No device diagnostics or identified trip contact has arrived.</p>
{% elif state == 'unavailable' %}
  <p class="warn" role="alert">Diagnostics unavailable. The diagnostics database could not be read.
    Current device health is unknown. Try again later; check the viewer logs if this persists.</p>
{% else %}
  <p class="muted">Times shown in {{ timezone }}. Contact can be recent while a snapshot is stale.
    Unknown means the condition has not been observed in the latest snapshot; it does not prove recovery.</p>
  {% for d in devices %}
  <section><h2>{{ d.device_id }}</h2>
    <p>Last server contact: {{ fmt_date(d.last_contact_at) }} ({{ d.contact_age }})</p>
    {% if d.snapshot is not none %}
    <p>Snapshot received: {{ fmt_date(d.snapshot_received_at) }} ({{ d.snapshot_age }}).
      Snapshot age measures staleness; contact does not refresh it.</p>
    <dl>
    {% for key, label in [('firmware_version', 'Firmware'), ('storage_backend', 'Storage backend'),
        ('pending_events', 'Pending events'), ('dropped_events', 'Dropped events'),
        ('boot_counter', 'Boot counter'), ('uptime_ms', 'Uptime (ms)')] %}
      <dt>{{ label }}</dt><dd>{{ d.snapshot.get(key, 'Not reported') }}</dd>
      {% if key == 'firmware_version' %}<dt>Server</dt><dd>{{ server_version }}</dd>{% endif %}
    {% endfor %}
      <dt>Status revision</dt><dd>{{ d.status_sequence }}</dd>
      <dt>Counters (boot-local)</dt><dd>
      {% for key, value in d.snapshot.get('counters', {}).items() %}
        <div>E{{ key }} = {{ value }}</div>
      {% else %}Not reported{% endfor %}</dd>
    </dl>
    {% else %}<p>No health snapshot received. Contact alone does not establish device health.</p><dl><dt>Server</dt><dd>{{ server_version }}</dd></dl>{% endif %}
    {% for state_name, conditions in d.groups.items() %}
    {# Unknown is usually most of the catalog; keep it folded away. #}
    {% if state_name == 'unknown' %}<details><summary>{% endif %}
    <h3 style="display:inline">{{ state_name|capitalize }} conditions ({{ conditions|length }})</h3>
    {% if state_name == 'unknown' %}</summary>{% endif %}
    {% for c in conditions %}
      <div class="condition"><b>E{{ c.code }} · {{ c.title }}</b>
        {% if c.severity %}<span class="tag">{{ c.severity }}</span>{% endif %}
        {% if c.episode_id %}<div>Episode: {{ c.episode_id }}</div>{% endif %}
        {% if c.oled %}<div>OLED: <span class="oled">{{ c.oled|join('\n') }}</span></div>{% endif %}
        <p>{{ c.explanation }} Recovery: {{ c.recovery }}</p>
      </div>
    {% else %}<p class="muted">None recorded.</p>{% endfor %}
    {% if state_name == 'unknown' %}</details>{% endif %}
    {% endfor %}
  </section>
  {% else %}<p>No devices recorded yet.</p>{% endfor %}
  <h2>Event history</h2>
  <form method="get" class="toolbar">
    <div><label for="code">Code (digits, e.g. 4005)</label>
      <input type="text" name="code" id="code" value="{{ code or '' }}" maxlength="4"></div>
    <div><label for="severity">Severity (current catalog)</label><select name="severity" id="severity">
      <option value="">All severities</option>
      {% for level in severities %}<option value="{{ level }}" {% if severity == level %}selected{% endif %}>{{ level }}</option>{% endfor %}
    </select></div><button class="btn" type="submit">Filter</button>
  </form>
  <div class="health-table"><table>
    <thead><tr><th>Device / code</th><th>Event identity</th><th>Context</th><th>Device time</th><th>Server receipt</th></tr></thead>
    <tbody>{% for e in events %}<tr class="diagnostic-event">
      <td>{{ e.device_id }}<br>E{{ e.code }} · {{ e.definition.title }}
        {% if e.definition.severity %}<br>{{ e.definition.severity }}{% endif %}</td>
      <td>{{ e.transition }}<br>Episode: {{ e.episode_id or '—' }}
        <br>Boot: {{ e.boot_id[:8] }}…<br>Sequence: {{ e.sequence }}</td>
      <td>{% for key, value in e.get('context', {}).items() %}<div>{{ key }} = {{ value }}</div>{% else %}—{% endfor %}</td>
      <td>{% if e.device_time %}{{ e.device_time }}<br>Source: {{ e.time_source }}
        <br>Sync age: {% if e.sync_age_ms is not none %}{{ e.sync_age_ms }} ms{% else %}unknown{% endif %}
        {% else %}Device clock unknown{% endif %}</td>
      <td>{{ fmt_date(e.received_at) }}</td>
    </tr>{% else %}<tr><td colspan="5">No events in this selection.</td></tr>{% endfor %}</tbody>
  </table></div>
  <p>Page {{ page }}</p><nav aria-label="Event history pages">
    {% if page > 1 %}<a class="btn" href="{{ url_for('device_health', page=page-1, code=code or '', severity=severity) }}">Newer</a>{% endif %}
    {% if has_next %}<a class="btn" href="{{ url_for('device_health', page=page+1, code=code or '', severity=severity) }}">Older</a>{% endif %}
  </nav>
{% endif %}</main></body></html>
"""


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "mileage-viewer", "version": server_version()}), 200


@app.route("/device-health")
def device_health():
    page, code, severity = diagnostics_filters()
    try:
        data = read_diagnostics(page, code, severity)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
        # Do not log stored JSON or device identifiers (even a damaged DB may
        # contain private text). Keep enough detail to diagnose schema/I/O faults.
        app.logger.warning("Diagnostics unavailable (%s)", type(exc).__name__)
        data = {"state": "unavailable"}
    return render_template_string(
        DEVICE_HEALTH_HTML, **data, page=page, code=code, severity=severity,
        severities=DIAGNOSTICS_SEVERITIES, timezone=DISPLAY_TZ_NAME,
    ), 503 if data["state"] == "unavailable" else 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if _is_logged_in():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        ip = _client_ip()
        locked = _lock_seconds_remaining(ip)
        if locked:
            minutes = max(1, round(locked / 60))
            error = f"Too many failed attempts. Try again in {minutes} minute(s)."
            return render_template_string(LOGIN_HTML, error=error), 429

        # Validated here rather than in the global hook so an expired
        # session shows "please try again" instead of a bare 400.
        if not _valid_csrf():
            session.clear()
            return (
                render_template_string(
                    LOGIN_HTML, error="Session expired — please try again."
                ),
                400,
            )

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        # Always run the hash comparison, even when the username is wrong,
        # so response time doesn't reveal whether the username exists.
        name_ok = _safe_equal(username, VIEWER_USERNAME)
        user = None if name_ok else users.get_user(DB_PATH, username)
        chosen_hash = VIEWER_PASSWORD_HASH if name_ok else (user["password_hash"] if user else DUMMY_PASSWORD_HASH)
        pass_ok = check_password_hash(chosen_hash, password)
        editor_ok = user is not None and not user["disabled"]
        if editor_ok:
            try:
                users.validate_username(user["username"], VIEWER_USERNAME)
            except users.UserError:
                editor_ok = False

        if pass_ok and (name_ok or editor_ok):
            _clear_failures(ip)
            session.clear()  # new session id on privilege change
            session["user"] = VIEWER_USERNAME
            session["pw_tag"] = _password_tag()
            if not name_ok:
                session["kind"] = "editor"
                session["user"] = user["username"]
                session["pw_tag"] = users.session_tag(VIEWER_SECRET_KEY, user)
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(_safe_next(request.args.get("next")) or url_for("index"))

        _record_failure(ip)
        app.logger.warning("Failed login for %r from %s", username[:40], ip)
        error = "Incorrect username or password."
        return render_template_string(LOGIN_HTML, error=error), 401

    return render_template_string(LOGIN_HTML, error=error)


def _confirmed_password():
    password = request.form.get("password", "")
    if password != request.form.get("confirm_password", ""):
        raise users.UserError("Passwords don't match.")
    users.validate_password(password)
    return password


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        try:
            action = request.form.get("action", "")
            username = request.form.get("username", "")
            password = _confirmed_password() if action in {"add", "reset-password"} else None
            if action == "remove" and request.form.get("confirm") != "yes":
                raise users.UserError("Confirm removing this login.")
            users.change_user(DB_PATH, action, username, password, VIEWER_USERNAME)
            session["account_message"] = "Everyday login updated."
        except users.UserError as exc:
            session["account_message"] = str(exc)
        return redirect(url_for("admin"))
    return render_template_string(ACCOUNT_HTML, title="Admin", admin_username=VIEWER_USERNAME,
                                  logins=users.list_users(DB_PATH), timezone=DISPLAY_TZ_NAME,
                                  year_list=_year_list(),
                                  message=session.pop("account_message", None))


REMOVED_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Removed trips</title>""" + THEME_HEAD + """<style>""" + BASE_CSS + """
.removed-table { overflow-x: auto; }
.removed-table td { vertical-align: top; }
</style></head><body>
""" + ACCOUNT_HEADER + """<main class="wrap"><h1>Removed trips</h1>
<p class="muted">The original files remain on the server. Restoring a trip returns it to the list,
totals, exports and merge suggestions.</p>
<div class="removed-table"><table>
<thead><tr><th>Date / folder</th><th>Distance</th><th>Reason</th><th>Removed by</th><th>Removed at ({{ timezone }})</th><th>Trip</th><th></th></tr></thead>
<tbody>{% for entry in entries %}<tr>
<td>{{ entry.date }}</td><td>{% if entry.unreadable %}unreadable{% else %}{{ "%.2f"|format(entry.distance or 0) }} mi{% endif %}</td>
<td>{{ entry.removal.reason }}</td><td>{{ entry.removal.removed_by }}</td><td>{{ fmt_date(entry.removal.removed_at) }}</td>
<td><a href="{{ url_for('trip_view', ym=entry.trip.ym, trip_id=entry.trip.id) }}">{{ entry.trip.trip_key }}</a></td>
<td><form method="post" action="{{ url_for('restore_trip', ym=entry.trip.ym, trip_id=entry.trip.id) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn" type="submit">Restore</button></form></td>
</tr>{% else %}<tr><td colspan="7" class="muted">No removed trips.</td></tr>{% endfor %}</tbody>
</table></div></main></body></html>
"""


@app.route("/admin/removed")
def admin_removed():
    trips, unreadable = load_trips(include_removed=True)
    entries = []
    for trip in trips:
        removal = trip.get("removal")
        if removal:
            entries.append({"trip": trip, "removal": removal,
                            "date": fmt_trip_date(trip), "distance": trip.get("distance_miles"),
                            "unreadable": False})
    for trip in unreadable:
        removal = trip.get("removal")
        if removal:
            entries.append({"trip": trip, "removal": removal, "date": trip["ym"],
                            "distance": None, "unreadable": True})
    entries.sort(key=lambda entry: entry["removal"]["removed_at"], reverse=True)
    return render_template_string(REMOVED_HTML, entries=entries, timezone=DISPLAY_TZ_NAME)


HISTORY_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change history</title>""" + THEME_HEAD + """<style>""" + BASE_CSS + """
.history-table { overflow-x: auto; }
.history-table td { vertical-align: top; overflow-wrap: anywhere; }
</style></head><body>
""" + ACCOUNT_HEADER + """<main class="wrap"><h1>Change history</h1>
<p class="muted">Times shown in {{ timezone }}. Newest changes appear first.</p>
<form method="get" class="toolbar">
  <label for="trip">Trip key (YYYY-MM/ID)</label>
  <input type="text" id="trip" name="trip" value="{{ trip_filter or '' }}" maxlength="80">
  <button class="btn" type="submit">Filter</button>
  {% if trip_filter %}<a class="btn secondary" href="{{ url_for('admin_history') }}">Clear</a>{% endif %}
</form>
<div class="history-table"><table>
<thead><tr><th>When ({{ timezone }})</th><th>Actor</th><th>Action</th><th>Trip</th><th>Change</th></tr></thead>
<tbody>{% for row in rows %}<tr class="history-row">
  <td>{{ fmt_date(row.at) }}</td><td>{{ row.actor }}<br><span class="muted">{{ row.actor_role }}</span></td>
  <td>{{ row.action_label }}</td><td><a href="{{ row.trip_url }}">{{ row.trip_label }}</a></td><td>{{ row.summary }}</td>
</tr>{% else %}<tr><td colspan="5" class="muted">No changes in this selection.</td></tr>{% endfor %}</tbody>
</table></div>
<div class="toolbar" aria-label="History pages">
  {% if has_newer %}<a class="btn secondary" href="{{ url_for('admin_history', page=page - 1, trip=trip_filter) }}">Newer</a>{% endif %}
  <span class="muted">Page {{ page }} · {{ total }} change(s)</span>
  {% if has_older %}<a class="btn secondary" href="{{ url_for('admin_history', page=page + 1, trip=trip_filter) }}">Older</a>{% endif %}
</div></main></body></html>
"""


def _history_summary(row):
    details = row["parsed_details"]
    action = row["action"]
    if action == "annotate":
        before, after = details.get("before", {}), details.get("after", {})
        labels = (("category", "Purpose"), ("purpose_note", "Note"), ("destination", "Destination"))
        changes = [f"{label}: {before.get(field) or 'unset'} → {after.get(field) or 'unset'}"
                   for field, label in labels if before.get(field) != after.get(field)]
        return "; ".join(changes) or "Annotation saved"
    if action == "merge":
        merged = ", ".join(details.get("merged_from", ()))
        source = details.get("annotation_from")
        return f"Merged {merged}; kept annotation from {source}."
    if action == "split":
        return "Restored original recordings: " + ", ".join(details.get("parts", ())) + "."
    if action == "remove":
        return "Removed from the log: " + str(details.get("reason", ""))
    if action == "restore":
        return "Restored to the log. Removal reason: " + str(details.get("reason", ""))
    if action == "year_close":
        return "Closed for edits."
    if action == "year_reopen":
        return "Reopened for edits."
    label = action.replace("_", " ").capitalize()
    return f"{label}: {json.dumps(details, ensure_ascii=False, sort_keys=True)}"


def _history_action_label(action):
    return {
        "annotate": "Annotation saved", "merge": "Merge", "split": "Undo merge",
        "remove": "Removed from log", "restore": "Restored to log",
        "year_close": "Year closed", "year_reopen": "Year reopened",
    }.get(action, action.replace("_", " ").capitalize())


def _history_trip_link(conn, row):
    key = row["trip_key"]
    details = row["parsed_details"]
    year_key = re.fullmatch(r"year/([0-9]{4})", key)
    if year_key:
        year = int(year_key.group(1))
        return url_for("index", year=year), f"Year {year}"
    target = key
    parts = details.get("parts") or ()
    if parts:
        target = parts[0]
    else:
        canonical = trip_merges.canonical_key(conn, key)
        if canonical != key:
            target = canonical
        else:
            old_merge = conn.execute(
                "SELECT details FROM trip_history WHERE trip_key=? AND action IN ('merge', 'split') "
                "ORDER BY at DESC, id DESC LIMIT 1", (key,),
            ).fetchone()
            if old_merge:
                try:
                    old_parts = json.loads(old_merge[0]).get("parts") or ()
                except (TypeError, ValueError):
                    old_parts = ()
                if old_parts:
                    target = old_parts[0]
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2})/([A-Za-z0-9_-]+)", target)
    if not match:
        return url_for("index"), key
    return url_for("trip_view", ym=match.group(1), trip_id=match.group(2)), key


@app.route("/admin/history")
def admin_history():
    page, trip_filter = history_filters()
    offset = (page - 1) * TRIP_HISTORY_PAGE_SIZE
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        if trip_filter:
            canonical = trip_merges.canonical_key(conn, trip_filter)
            parts = trip_merges.parts(conn, canonical)
            history_rows = _related_history_rows(conn, canonical, parts)
            total = len(history_rows)
            history_rows = history_rows[offset:offset + TRIP_HISTORY_PAGE_SIZE]
        else:
            total = conn.execute("SELECT count(*) FROM trip_history").fetchone()[0]
            history_rows = [_history_row_dict(row) for row in conn.execute(
                "SELECT id, trip_key, actor, actor_role, at, action, details FROM trip_history "
                "ORDER BY at DESC, id DESC LIMIT ? OFFSET ?", (TRIP_HISTORY_PAGE_SIZE, offset),
            )]
            for row in history_rows:
                try:
                    row["parsed_details"] = json.loads(row["details"])
                except (TypeError, ValueError):
                    row["parsed_details"] = {}
        for row in history_rows:
            row["action_label"] = _history_action_label(row["action"])
            row["summary"] = _history_summary(row)
            row["trip_url"], row["trip_label"] = _history_trip_link(conn, row)
    return render_template_string(
        HISTORY_HTML, rows=history_rows, page=page, total=total,
        has_newer=page > 1, has_older=offset + len(history_rows) < total,
        trip_filter=trip_filter, timezone=DISPLAY_TZ_NAME,
    )


def _year_list():
    """The years the admin can review, newest first, each with its closed record."""
    trips, unreadable = load_trips(include_removed=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        closed = trip_log.closed_years(conn)
    years = {y for y in map(_trip_local_year, trips) if y} | closed.keys()
    for trip in unreadable:
        years |= _folder_years(trip["ym"])
    return [{"year": year, "closed": closed.get(year)} for year in sorted(years, reverse=True)]


YEAR_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ year }} year review</title>""" + THEME_HEAD + """<style>""" + BASE_CSS + """
.review-table { overflow-x: auto; }
.review-table td { vertical-align: top; overflow-wrap: anywhere; }
</style></head><body>
""" + ACCOUNT_HEADER + """<main class="wrap">
{% macro trip_link(t) %}<a href="{{ url_for('trip_view', ym=t.ym, trip_id=t.id, year=year) }}">{% if t.problem %}{{ t.trip_key }}{% else %}{{ fmt_trip_date(t) }}{% endif %}</a>{% endmacro %}
<h1>{{ year }} year review</h1>
<p class="muted">Times shown in {{ timezone }}. A trip belongs to the year it starts in (or, if partly timed, its
first recorded time), the same as the trip list and exports.</p>
<div class="toolbar">
  <a class="btn secondary" href="{{ url_for('index', year=year) }}">{{ year }} trips</a>
  <a class="btn" href="{{ url_for('export_csv', year=year) }}">Export CSV</a>
  <a class="btn secondary" href="{{ url_for('export_xlsx', year=year) }}">Export XLSX</a>
</div>

<section aria-labelledby="status-heading">
<h2 id="status-heading">Status</h2>
{% if closed %}
<p><span class="tag other">Closed for edits</span> by {{ closed.closed_by }} on {{ fmt_date(closed.closed_at) }}.
Nobody can annotate, merge, undo a merge, remove or restore a trip in {{ year }} until you reopen it.
Viewing and exporting still work.</p>
<form method="post" action="{{ url_for('reopen_year', year=year) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label><input type="checkbox" name="confirm" value="yes" required> Confirm reopening {{ year }}</label>
<button class="btn secondary" type="submit">Reopen {{ year }}</button></form>
{% else %}
<p>{{ year }} is open. Close it once its return is filed, so its trips can't change by accident.</p>
<form method="post" action="{{ url_for('close_year', year=year) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<label><input type="checkbox" name="confirm" value="yes" required> Confirm closing {{ year }} for edits</label>
<button class="btn" type="submit">Close {{ year }}</button></form>
{% endif %}
</section>

<section aria-labelledby="totals-heading">
<h2 id="totals-heading">Totals by category</h2>
<p class="muted">The same figures as the {{ year }} CSV and XLSX exports.</p>
<div class="review-table"><table>
<thead><tr><th>Category</th><th>Trips</th><th>Miles</th></tr></thead>
<tbody>{% for category, miles in totals.items() %}<tr>
<td>{{ category }}</td><td>{{ counts[category] }}</td><td>{{ "%.3f"|format(miles) }}</td></tr>{% endfor %}</tbody>
</table></div>
</section>

<section aria-labelledby="uncategorized-heading">
<h2 id="uncategorized-heading">Trips with no category ({{ uncategorized|length }})</h2>
<div class="review-table"><table>
<thead><tr><th>Trip</th><th>Distance</th></tr></thead>
<tbody>{% for t in uncategorized %}<tr><td>{{ trip_link(t) }}</td><td>{{ "%.2f"|format(t.distance_miles or 0) }} mi</td></tr>
{% else %}<tr><td colspan="2" class="muted">Every trip has a category.</td></tr>{% endfor %}</tbody>
</table></div>
</section>

<section aria-labelledby="incomplete-heading">
<h2 id="incomplete-heading">Business trips missing a note or destination ({{ incomplete|length }})</h2>
<div class="review-table"><table>
<thead><tr><th>Trip</th><th>Distance</th><th>Missing</th></tr></thead>
<tbody>{% for item in incomplete %}<tr><td>{{ trip_link(item.trip) }}</td>
<td>{{ "%.2f"|format(item.trip.distance_miles or 0) }} mi</td><td>{{ item.missing|join(' and ') }}</td></tr>
{% else %}<tr><td colspan="3" class="muted">Every business trip has a purpose note and a destination.</td></tr>{% endfor %}</tbody>
</table></div>
</section>

<section aria-labelledby="unreadable-heading">
<h2 id="unreadable-heading">Trips whose summary can't be read ({{ unreadable|length }})</h2>
<p class="muted">Their miles can't be counted, so the totals above may be short.</p>
<div class="review-table"><table>
<thead><tr><th>Trip</th><th>Problem</th></tr></thead>
<tbody>{% for t in unreadable %}<tr><td>{{ trip_link(t) }}</td><td>{{ t.problem }}</td></tr>
{% else %}<tr><td colspan="2" class="muted">All summaries can be read.</td></tr>{% endfor %}</tbody>
</table></div>
</section>

<section aria-labelledby="removed-heading">
<h2 id="removed-heading">Removed trips ({{ removed|length }})</h2>
<div class="review-table"><table>
<thead><tr><th>Trip</th><th>Reason</th><th>Removed by</th><th>Removed at ({{ timezone }})</th></tr></thead>
<tbody>{% for t in removed %}<tr><td>{{ trip_link(t) }}</td><td>{{ t.removal.reason }}</td>
<td>{{ t.removal.removed_by }}</td><td>{{ fmt_date(t.removal.removed_at) }}</td></tr>
{% else %}<tr><td colspan="4" class="muted">No trips were removed.</td></tr>{% endfor %}</tbody>
</table></div>
</section>

<section aria-labelledby="late-heading">
<h2 id="late-heading">Edits after the year ended ({{ edits|length }})</h2>
<p class="muted">Changes to a {{ year }} trip made on or after {{ year_end }} ({{ timezone }}), and
every time {{ year }} was closed or reopened.</p>
<div class="review-table"><table>
<thead><tr><th>When ({{ timezone }})</th><th>Actor</th><th>Action</th><th>Trip</th><th>Change</th></tr></thead>
<tbody>{% for row in edits %}<tr>
<td>{{ fmt_date(row.at) }}</td><td>{{ row.actor }}<br><span class="muted">{{ row.actor_role }}</span></td>
<td>{{ row.action_label }}</td><td><a href="{{ row.trip_url }}">{{ row.trip_label }}</a></td><td>{{ row.summary }}</td>
</tr>{% else %}<tr><td colspan="5" class="muted">No edits after the year ended.</td></tr>{% endfor %}</tbody>
</table></div>
</section>
</main></body></html>
"""


def _late_edit_rows(conn, year, trips):
    """History rows that touch one of trips on or after local Jan 1 00:00 of year + 1.

    trips are every trip of the year (removed ones too). A trip is touched
    through its own key, its parts and its merge aliases, like its own
    history. The year's close/reopen rows come along whatever their date.
    One pass over the rows after the boundary serves all the trips, instead
    of one history scan per trip.
    """
    keys = set()
    aliases = {}
    for alias_key, merge_key in conn.execute("SELECT alias_key, merge_key FROM trip_merge_aliases"):
        aliases.setdefault(merge_key, []).append(alias_key)
    for trip in trips:
        keys.add(trip["trip_key"])
        keys.update(trip.get("parts", ()))
        keys.update(aliases.get(trip["trip_key"], ()))
    rows = {}
    try:
        boundary = datetime(year + 1, 1, 1, tzinfo=DISPLAY_TZ).astimezone(timezone.utc)
    except ValueError:  # year 9999: nothing can happen after it
        boundary = None
    if boundary:
        # The string compare only narrows the scan (a day of margin); the
        # real comparison below is on parsed instants.
        for raw in conn.execute(
            "SELECT id, trip_key, actor, actor_role, at, action, details FROM trip_history WHERE at >= ?",
            ((boundary - timedelta(days=1)).isoformat(),),
        ):
            row = _history_row_dict(raw)
            if _history_order(row["at"]) < boundary:
                continue
            try:
                details = json.loads(row["details"])
            except (TypeError, ValueError):
                details = {}
            row["parsed_details"] = details
            touched = row["trip_key"] in keys or (
                row["action"] in ("merge", "split") and set(details.get("parts") or ()) & keys)
            if touched:
                rows[row["id"]] = row
    for raw in conn.execute(
        "SELECT id, trip_key, actor, actor_role, at, action, details FROM trip_history WHERE trip_key=?",
        (f"year/{year}",),
    ):
        row = _history_row_dict(raw)
        try:
            row["parsed_details"] = json.loads(row["details"])
        except (TypeError, ValueError):
            row["parsed_details"] = {}
        rows[row["id"]] = row
    result = sorted(rows.values(), key=lambda r: (_history_order(r["at"]), r["id"]), reverse=True)
    for row in result:
        row["action_label"] = _history_action_label(row["action"])
        row["summary"] = _history_summary(row)
        row["trip_url"], row["trip_label"] = _history_trip_link(conn, row)
    return result


@app.route("/admin/year/<year>")
def admin_year(year):
    year = valid_year(year)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        # Serialize the merge-cache refresh in _load_trips, like load_trips().
        conn.execute("BEGIN IMMEDIATE")
        trips, unreadable = _load_trips(conn, include_removed=True)
        closed = trip_log.closed_years(conn).get(year)
        # Totals and the problem lists come from the same selection the list
        # and exports use, applied to the log without its removed trips.
        live = [t for t in trips if "removal" not in t]
        live_unreadable = [t for t in unreadable if "removal" not in t]
        members, broken = year_members(live, live_unreadable, year)
        gone, gone_unreadable = year_members(
            [t for t in trips if "removal" in t], [t for t in unreadable if "removal" in t], year)
        edits = _late_edit_rows(conn, year, members + broken + gone + gone_unreadable)
    totals = mileage_totals(members)
    counts = {category: 0 for category in totals}
    counts["all"] = len(members)
    for t in members:
        counts[t.get("category") if t.get("category") in CATEGORIES else "unclassified"] += 1
    incomplete = []
    for t in members:
        if t.get("category") == "business":
            missing = [label for label, field in (("purpose note", "purpose_note"), ("destination", "destination"))
                       if not (t.get(field) or "").strip()]
            if missing:
                incomplete.append({"trip": t, "missing": missing})
    removed = sorted(gone + gone_unreadable, key=lambda t: t["removal"]["removed_at"], reverse=True)
    return render_template_string(
        YEAR_HTML, year=year, closed=closed, totals=totals, counts=counts, edits=edits,
        uncategorized=[t for t in members if t.get("category") not in CATEGORIES],
        incomplete=incomplete, unreadable=broken, removed=removed, timezone=DISPLAY_TZ_NAME,
        year_end=f"{year + 1}-01-01 00:00",
    )


def _change_year(raw_year, closing_it):
    """Close or reopen a year: a tax_years row and its history row, in one transaction."""
    year = valid_year(raw_year)
    if request.form.get("confirm") != "yes":
        abort(400, description=f"Confirm {'closing' if closing_it else 'reopening'} {year}.")
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        is_closed = year in trip_log.closed_years(conn)
        if is_closed == closing_it:
            abort(409, description=f"{year} is already {'closed' if closing_it else 'open'}.")
        if closing_it:
            trip_log.close_year(conn, year, g.user)
        else:
            trip_log.reopen_year(conn, year)
        trip_log.record(conn, f"year/{year}", "year_close" if closing_it else "year_reopen", {},
                        actor=g.user, actor_role=g.role)
    return redirect(url_for("admin_year", year=year))


@app.route("/admin/year/<year>/close", methods=["POST"])
def close_year(year):
    return _change_year(year, True)


@app.route("/admin/year/<year>/reopen", methods=["POST"])
def reopen_year(year):
    return _change_year(year, False)


@app.route("/trip/<ym>/<trip_id>/remove", methods=["POST"])
def remove_trip(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    reason = request.form.get("reason", "").strip()
    if not 1 <= len(reason) <= 500:
        abort(400, description="Reason must be between 1 and 500 characters.")
    requested_key = f"{ym}/{trip_id}"
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        key = trip_merges.canonical_key(conn, requested_key)
        if key != requested_key:
            abort(409, description="This recording is part of a merged trip; remove the merged trip instead.")
        if trip_log.removal(conn, key):
            abort(409, description="This trip is already removed from the log.")
        trips, unreadable = load_trips(conn, include_removed=True)
        trip = next((t for t in trips + unreadable if t["trip_key"] == key), None)
        if trip is None:
            abort(404)
        _abort_if_year_closed(trip_log.closed_years(conn), trip)
        trip_log.add_removal(conn, key, reason, g.user)
        trip_log.record(conn, key, "remove", {"reason": reason}, actor=g.user, actor_role=g.role)
    return redirect(url_for("trip_view", ym=ym, trip_id=trip_id, year=year or ""))


@app.route("/trip/<ym>/<trip_id>/restore", methods=["POST"])
def restore_trip(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        key = trip_merges.canonical_key(conn, f"{ym}/{trip_id}")
        removal = trip_log.removal(conn, key)
        if not removal:
            abort(409, description="This trip is not removed from the log.")
        _abort_if_key_in_closed_year(conn, key)
        # Defense in depth: a merge made before merge suggestions saw removed
        # trips can already bridge this recording. Restoring it then counts
        # its stretch twice. A removed unreadable trip has no counted miles
        # and no reliable span, so it never blocks.
        trips, _ = load_trips(conn, include_removed=True)
        removed_trip = next((t for t in trips if t["trip_key"] == key), None)
        if removed_trip and trip_merges.covering_merge(removed_trip, trips):
            abort(409, description="A merged trip now covers this recording's time. "
                                   "Undo that merge first, then restore this trip.")
        trip_log.delete_removal(conn, key)
        trip_log.record(conn, key, "restore", {"reason": removal["reason"]},
                        actor=g.user, actor_role=g.role)
    return redirect(url_for("trip_view", ym=ym, trip_id=trip_id, year=year or ""))


@app.route("/account/password", methods=["GET", "POST"])
def account_password():
    if request.method == "POST" and g.role == "editor":
        ip = _client_ip()
        if _lock_seconds_remaining(ip):
            return render_template_string(ACCOUNT_HTML, title="Change password",
                                          message="Too many failed attempts. Try again later."), 429
        user = users.get_user(DB_PATH, g.user)
        # A concurrent removal still takes one hash check and fails closed.
        valid = check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH,
                                    request.form.get("current_password", ""))
        if not valid or user is None:
            _record_failure(ip)
            session["account_message"] = "Incorrect current password."
        else:
            try:
                password = _confirmed_password()
                updated = users.change_user(DB_PATH, "reset-password", g.user, password, VIEWER_USERNAME,
                                             expected_tag=session["pw_tag"], secret_key=VIEWER_SECRET_KEY)
                session["pw_tag"] = users.session_tag(VIEWER_SECRET_KEY, updated)
                _clear_failures(ip)
                session["account_message"] = "Password changed. Other sessions for this login have ended."
            except users.UserError as exc:
                session["account_message"] = str(exc)
        return redirect(url_for("account_password"))
    if request.method == "POST":
        return redirect(url_for("account_password"))
    return render_template_string(ACCOUNT_HTML, title="Change password",
                                  message=session.pop("account_message", None))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    trips, unreadable, year, years = selected_trips()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        year_closed = year in trip_log.closed_years(conn)
    return render_template_string(
        LIST_HTML, trips=trips, unreadable=unreadable, year=year, years=years,
        totals=mileage_totals(trips), timezone=DISPLAY_TZ_NAME, year_closed=year_closed,
    )


@app.route("/trip/<ym>/<trip_id>")
def trip_view(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    key = f"{ym}/{trip_id}"
    with closing(sqlite3.connect(DB_PATH)) as conn:
        key = trip_merges.canonical_key(conn, key)
        removed = trip_log.removal(conn, key)
    # Merge suggestions get the standard lists plus the removed readable
    # trips; only the admin may open a removed trip itself.
    trips, unreadable, removed_trips, removed_unreadable = split_removed(*load_trips(include_removed=True))
    t = next((t for t in trips if t["trip_key"] == key or key in t.get("parts", [])), None)
    if t is None and g.role == "admin" and removed is not None:
        t = next((t for t in removed_trips + removed_unreadable if t["trip_key"] == key), None)
    if t is None:
        problem = next((u["problem"] for u in unreadable if u["trip_key"] == key), "the file is missing")
        abort(404, description=(
            f"This trip's summary file can't be read ({problem}), so its "
            "miles can't be counted. The trip list and exports flag it. To "
            "rebuild it from the GPX, see DEPLOY_DEBIAN.md → Troubleshooting."
        ))
    if t["trip_key"] != f"{ym}/{trip_id}":
        return redirect(url_for("trip_view", ym=t["ym"], trip_id=t["id"], year=year or ""))
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        last_edited = _last_edited(conn, t)
        closed = trip_log.closed_years(conn)
    lock_year = locked_year(t, closed) if closed else None
    year_lock = {"year": lock_year, **closed[lock_year]} if lock_year else None
    if t.get("removal"):
        return render_template_string(TRIP_HTML, t=t, removal=t["removal"], categories=CATEGORIES,
                                      year=year, geocoder_enabled=False, last_edited=last_edited,
                                      year_lock=year_lock)
    merge_next, merge_block_reason = trip_merges.next_trip(t, trips, unreadable, removed_trips)
    places = {}
    if GEOCODER.enabled:
        endpoints = track_endpoints(t)
        for endpoint in ("start", "end"):
            if f"{endpoint}_latitude" in endpoints:
                places[endpoint] = GEOCODER.cached(
                    endpoints[f"{endpoint}_latitude"], endpoints[f"{endpoint}_longitude"]
                )
    return render_template_string(TRIP_HTML, t=t, categories=CATEGORIES, year=year,
                                  geocoder_enabled=GEOCODER.enabled, places=places, merge_next=merge_next,
                                  merge_block_reason=merge_block_reason,
                                  last_edited=last_edited, year_lock=year_lock,
                                  merge_conflict=bool(merge_next and trip_merges.annotated(t) and trip_merges.annotated(merge_next)))


@app.route("/trip/<ym>/<trip_id>/lookup-place", methods=["POST"])
def lookup_place(ym, trip_id):
    # Coordinates and provider are always server-selected. This is not an
    # arbitrary geocoding proxy, even for an authenticated browser.
    _safe_trip_paths(ym, trip_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        _abort_if_removed(conn, f"{ym}/{trip_id}")
    t = resolved_trip(ym, trip_id)
    endpoint = request.form.get("endpoint", "")
    if endpoint not in ("start", "end"):
        return jsonify(error="Choose the start or end endpoint."), 400
    endpoints = track_endpoints(t)
    if f"{endpoint}_latitude" not in endpoints:
        return jsonify(error="This recording has no valid endpoint to look up."), 422
    try:
        label = GEOCODER.lookup(endpoints[f"{endpoint}_latitude"], endpoints[f"{endpoint}_longitude"])
    except GeocodeError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(label=label)


@app.route("/gpx/<ym>/<trip_id>")
def trip_gpx(ym, trip_id):
    gpx_path, _ = _safe_trip_paths(ym, trip_id)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        removed = trip_log.removal(conn, trip_merges.canonical_key(conn, f"{ym}/{trip_id}"))
    # The track of a removed trip is the admin's to look at, nobody else's;
    # without this check the plain-file path below would still serve it.
    if removed and g.role != "admin":
        abort(404)
    t = resolved_trip(ym, trip_id, allow_original=True, include_removed=bool(removed))
    if t and t.get("parts"):
        try:
            raw = trip_merges.combined_gpx(DATA_ROOT, t["parts"])
        except (OSError, ValueError):
            abort(404, description="Merged track unavailable.")
        return send_file(io.BytesIO(raw), mimetype="application/gpx+xml", download_name=t["id"] + ".gpx")
    if not gpx_path.exists():
        abort(404)
    return send_file(gpx_path, mimetype="application/gpx+xml")


@app.route("/trip/<ym>/<trip_id>/annotate", methods=["POST"])
def annotate(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    category = request.form.get("category", "").strip()
    if category not in CATEGORIES:
        category = ""
    note = request.form.get("purpose_note", "").strip()
    destination = request.form.get("destination", "").strip()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        key = f"{ym}/{trip_id}"
        canonical = trip_merges.canonical_key(conn, key)
        if canonical != key:
            abort(409, description="This part has been merged. Reopen the merged trip before editing.")
        _abort_if_removed(conn, key)
        _abort_if_key_in_closed_year(conn, key)
        merged = conn.execute("SELECT * FROM trip_merges WHERE trip_key=?", (key,)).fetchone()
        after = {"category": category, "purpose_note": note, "destination": destination}
        if merged:
            before = {field: merged[field] or "" for field in trip_merges.ANNOTATION_FIELDS}
        else:
            if not (DATA_ROOT / (key + ".json")).exists():
                abort(404)
            annotation = conn.execute("SELECT * FROM annotations WHERE trip_key=?", (key,)).fetchone()
            before = ({field: annotation[field] or "" for field in trip_merges.ANNOTATION_FIELDS}
                      if annotation else {field: "" for field in trip_merges.ANNOTATION_FIELDS})
        if before != after:
            stamp = trip_log.now_utc()
            if merged:
                conn.execute("""UPDATE trip_merges SET category=?, purpose_note=?, destination=?, updated_at=?
                                WHERE trip_key=?""",
                             (category, note, destination, stamp, key))
            else:
                conn.execute("""INSERT INTO annotations (trip_key, category, purpose_note, destination, updated_at)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(trip_key) DO UPDATE SET
                    category=excluded.category, purpose_note=excluded.purpose_note,
                    destination=excluded.destination, updated_at=excluded.updated_at""",
                             (key, category, note, destination, stamp))
            trip_log.record(conn, key, "annotate", {"before": before, "after": after},
                            actor=g.user, actor_role=g.role)
    return redirect(url_for("trip_view", ym=ym, trip_id=trip_id, year=year or ""))


def _abort_if_removed(conn, key):
    """409 for anything that would change a trip the admin removed from the log."""
    if trip_log.removal(conn, trip_merges.canonical_key(conn, key)):
        abort(409, description="This trip is removed from the log. The admin has to restore it first.")


def resolved_trip(ym, trip_id, allow_original=False, include_removed=False):
    key = f"{ym}/{trip_id}"
    with closing(sqlite3.connect(DB_PATH)) as conn:
        key = trip_merges.canonical_key(conn, key)
    trips, _ = load_trips(include_removed=include_removed)
    t = next((t for t in trips if t["trip_key"] == key or key in t.get("parts", [])), None)
    if t is None and not allow_original:
        abort(404)
    return t


@app.route("/trip/<ym>/<trip_id>/merge", methods=["POST"])
def merge_trip(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _abort_if_removed(conn, f"{ym}/{trip_id}")
        trips, unreadable, removed_trips = load_trips_for_merging(conn)
        current = next((t for t in trips if t["trip_key"] == f"{ym}/{trip_id}"), None)
        if current is None:
            abort(409, description="Trip changed or is unavailable. Reopen the trip list.")
        closed = trip_log.closed_years(conn)
        _abort_if_year_closed(closed, current)
        following, block_reason = trip_merges.next_trip(current, trips, unreadable, removed_trips)
        if block_reason:
            abort(409, description=block_reason)
        if following is None or request.form.get("next_trip") != following["trip_key"]:
            abort(409, description="Only the next trip from the same device within 15 minutes can be merged. Reopen this trip.")
        _abort_if_year_closed(closed, following)
        if not current["has_gpx"] or not following["has_gpx"]:
            abort(409, description="Both recordings are required to merge.")
        if trip_merges.annotated(current) and trip_merges.annotated(following):
            abort(409, description="Both trips have annotations. Clear one annotation before merging; original annotations are restored by Undo.")
        keys = current.get("parts", [current["trip_key"]]) + following.get("parts", [following["trip_key"]])
        try:
            stamp = trip_merges.fingerprint(DATA_ROOT, keys)
            summary = trip_merges.combined_summary(DATA_ROOT, keys)
            if trip_merges.fingerprint(DATA_ROOT, keys) != stamp:
                raise ValueError("Source changed")
        except (OSError, ValueError):
            abort(409, description="A recording is unavailable or changed. Reopen this trip.")
        key = f"{current['ym']}/merge-{secrets.token_hex(12)}"
        annotation = current if trip_merges.annotated(current) else following
        for old in (current, following):
            conn.execute("UPDATE trip_merge_aliases SET merge_key=? WHERE merge_key=?", (key, old["trip_key"]))
            if old.get("parts"):
                conn.execute("INSERT INTO trip_merge_aliases (alias_key, merge_key) VALUES (?, ?)",
                             (old["trip_key"], key))
            conn.execute("DELETE FROM trip_merge_parts WHERE merge_key=?", (old["trip_key"],))
            conn.execute("DELETE FROM trip_merges WHERE trip_key=?", (old["trip_key"],))
        # `stamp` is the source fingerprint apply_merges compares on every list
        # load; the time of the merge needs its own name or the row looks stale.
        merged_at = trip_log.now_utc()
        conn.execute("""INSERT INTO trip_merges
            (trip_key, summary, fingerprint, category, purpose_note, destination, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (key, json.dumps(summary), stamp, *(annotation.get(f, "") for f in trip_merges.ANNOTATION_FIELDS),
                      merged_at))
        conn.executemany("INSERT INTO trip_merge_parts (part_key, merge_key, position) VALUES (?, ?, ?)",
                         [(part, key, index) for index, part in enumerate(keys)])
        trip_log.record(conn, key, "merge", {
            "parts": keys,
            "merged_from": [current["trip_key"], following["trip_key"]],
            "annotation_from": annotation["trip_key"],
        }, actor=g.user, actor_role=g.role)
    ym, trip_id = key.split("/")
    return redirect(url_for("trip_view", ym=ym, trip_id=trip_id, year=year or ""))


@app.route("/trip/<ym>/<trip_id>/split", methods=["POST"])
def split_trip(ym, trip_id):
    year = selected_year()
    _safe_trip_paths(ym, trip_id)
    if request.form.get("confirm") != "yes":
        abort(400, description="Confirm restoring original annotations and removing the merged annotation.")
    key = f"{ym}/{trip_id}"
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _abort_if_removed(conn, key)
        keys = trip_merges.parts(conn, key)
        if not keys:
            abort(409, description="This merge no longer exists. Reopen the trip list.")
        _abort_if_key_in_closed_year(conn, key)
        conn.execute("DELETE FROM trip_merge_aliases WHERE merge_key=?", (key,))
        conn.execute("DELETE FROM trip_merge_parts WHERE merge_key=?", (key,))
        conn.execute("DELETE FROM trip_merges WHERE trip_key=?", (key,))
        trip_log.record(conn, key, "split", {"parts": keys}, actor=g.user, actor_role=g.role)
    first_ym, first_id = keys[0].split("/")
    return redirect(url_for("trip_view", ym=first_ym, trip_id=first_id, year=year or ""))


# ---------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------

EXPORT_COLUMNS = [
    ("date", fmt_trip_date),
    ("trip_id", lambda t: t.get("trip_id", t.get("id"))),
    ("device_id", lambda t: t.get("device_id", "")),
    ("distance_miles", lambda t: t.get("distance_miles") or 0),
    ("duration_minutes", lambda t: round(t["duration_seconds"] / 60, 1)
     if t.get("duration_seconds") is not None else ""),
    ("category", lambda t: t.get("category", "")),
    ("purpose_note", lambda t: t.get("purpose_note", "")),
    ("start_time", lambda t: fmt_local_iso(t.get("start_time"))),
    ("end_time", lambda t: fmt_local_iso(t.get("end_time"))),
    ("max_speed_mph", lambda t: t.get("max_speed_mph") or 0),
    ("avg_speed_mph", lambda t: t.get("avg_speed_mph") if t.get("avg_speed_mph") is not None else ""),
    ("gpx_file", lambda t: t.get("gpx_file", "")),
    ("destination", lambda t: t.get("destination", "")),
    ("start_latitude", lambda t: t.get("start_latitude", "")),
    ("start_longitude", lambda t: t.get("start_longitude", "")),
    ("end_latitude", lambda t: t.get("end_latitude", "")),
    ("end_longitude", lambda t: t.get("end_longitude", "")),
    ("timing_status", lambda t: t.get("timing_status") or "unknown"),
    ("timed_from", lambda t: fmt_local_iso(t.get("timed_from"))),
    ("recorded_until", lambda t: fmt_local_iso(t.get("recorded_until"))),
    ("year_basis", year_basis),
    ("last_edited_by", lambda t: t.get("last_edited_by", "")),
    ("last_edited_at", lambda t: t.get("last_edited_at", "")),
]


def track_endpoints(trip):
    """First/last recorded track coordinates, including legacy GPX files.

    Stream without constructing an XML tree, reject DTDs/entities, and ignore
    waypoints. Invalid or absent tracks yield blanks rather than invented
    coordinates. These are recorded endpoints, not reverse-geocoded places.
    """
    if trip.get("parts"):
        start_ym, start_id = trip["parts"][0].split("/")
        end_ym, end_id = trip["parts"][-1].split("/")
        first = track_endpoints({"ym": start_ym, "id": start_id})
        last = track_endpoints({"ym": end_ym, "id": end_id})
        return {**{k: v for k, v in first.items() if k.startswith("start_")},
                **{k: v for k, v in last.items() if k.startswith("end_")}}
    path, _ = _safe_trip_paths(trip["ym"], trip["id"])
    parser = expat.ParserCreate(namespace_separator="}")
    stack = []
    first = last = None

    def start(name, attrs):
        nonlocal first, last
        stack.append(name.rsplit("}", 1)[-1])
        if stack == ["gpx", "trk", "trkseg", "trkpt"]:
            lat, lon = float(attrs["lat"]), float(attrs["lon"])
            if not (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Invalid track coordinates")
            if first is None:
                first = (lat, lon)
            last = (lat, lon)

    def reject_doctype(*args):
        raise ValueError("GPX document types are not supported")

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: stack.pop()
    parser.StartDoctypeDeclHandler = reject_doctype
    try:
        with path.open("rb") as source:
            parser.ParseFile(source)
    except (OSError, expat.ExpatError, ValueError, KeyError):
        return {}
    if first is None:
        return {}
    return dict(zip(
        ("start_latitude", "start_longitude", "end_latitude", "end_longitude"),
        (*first, *last),
    ))


def _csv_safe(value):
    """Neutralize spreadsheet formula injection in CSV text cells.

    Spreadsheet readers can interpret cells starting with = + - @ or
    control characters as formulas. Notes are user-entered free text and this
    export is the IRS artifact, so prefix risky cells with a quote. Only
    strings are touched, so negative numbers stay numbers. XLSX has its own
    guard in _xlsx_literal_text().
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + value
    return value


def _xlsx_literal_text(row):
    """Force every string cell in an openpyxl row to be stored as text.

    openpyxl turns any string starting with "=" into a live formula, so a
    note like "=-Client visit" exported as #NAME?. No export column is ever
    a formula, so mark them all as plain strings. Unlike the CSV guard this
    adds no quote: the cell shows exactly what was typed.
    """
    for cell in row:
        if isinstance(cell.value, str):
            cell.data_type = "s"


UNREADABLE_COLUMNS = (
    "trip_id", "gpx_file", "category", "purpose_note", "destination",
    "last_edited_by", "last_edited_at",
)


def _unreadable_values(t):
    """Export fields for a trip whose summary can't be read.

    Just what identifies it and what was typed for it. Its miles are
    unknown, so they stay blank rather than reading as zero.
    """
    return {
        "trip_id": t.get("trip_id") or t["id"],
        "gpx_file": t.get("gpx_file") or (f"{t['id']}.gpx" if t["has_gpx"] else ""),
        "category": t["category"],
        "purpose_note": t["purpose_note"],
        "destination": t["destination"],
        "last_edited_by": t.get("last_edited_by", ""),
        "last_edited_at": t.get("last_edited_at", ""),
    }


@app.route("/export.csv")
def export_csv():
    trips, unreadable, year, _ = selected_trips(include_history=True)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in EXPORT_COLUMNS] + ["row_type"])
    for t in trips:
        t = {**t, **track_endpoints(t)}
        writer.writerow([_csv_safe(fn(t)) for _, fn in EXPORT_COLUMNS] + ["trip"])
    for t in unreadable:
        values = _unreadable_values(t)
        writer.writerow(
            [_csv_safe(values.get(name, "")) for name, _ in EXPORT_COLUMNS] + ["unreadable"]
        )
    for category, miles in mileage_totals(trips).items():
        # Keep the CSV rectangular and mark totals explicitly, so consumers
        # can select row_type=trip without double-counting summary rows.
        values = {"category": category, "distance_miles": miles}
        writer.writerow([values.get(name, "") for name, _ in EXPORT_COLUMNS] + ["total"])
    data = buf.getvalue().encode("utf-8")
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"mileage-{year}.csv" if year else "mileage.csv",
    )


@app.route("/export.xlsx")
def export_xlsx():
    trips, unreadable, year, _ = selected_trips(include_history=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Mileage"
    ws.append([name for name, _ in EXPORT_COLUMNS])
    for t in trips:
        t = {**t, **track_endpoints(t)}
        ws.append([fn(t) for _, fn in EXPORT_COLUMNS])
        _xlsx_literal_text(ws[ws.max_row])
    totals = wb.create_sheet("Totals")
    totals.append(["year", "timezone", "category", "distance_miles"])
    for category, miles in mileage_totals(trips).items():
        totals.append([year or "All years", DISPLAY_TZ_NAME, category, miles])
        _xlsx_literal_text(totals[totals.max_row])
    if unreadable:
        # A sheet of their own: Mileage has no row_type column, so a row
        # with blank miles there would pass for a real trip.
        sheet = wb.create_sheet("Unreadable trips")
        sheet.append(list(UNREADABLE_COLUMNS))
        for t in unreadable:
            values = _unreadable_values(t)
            sheet.append([values[name] for name in UNREADABLE_COLUMNS])
            _xlsx_literal_text(sheet[sheet.max_row])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"mileage-{year}.xlsx" if year else "mileage.xlsx",
    )


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8091, debug=False)
