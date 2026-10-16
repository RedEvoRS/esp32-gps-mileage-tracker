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

Design notes:
  - The GPX/JSON files are the immutable source of truth and are mounted
    read-only. This service never writes to them.
  - The ONLY thing this service owns is annotations.db (SQLite), keyed by
    each trip's relative path "YYYY-MM/<id>". If that DB is lost, every
    trip still lists and maps fine -- only the annotations are gone.
  - Trips are identified in URLs by <ym>/<id> (e.g. 2026-04/2026-04-14...).
    Both components are strictly validated to prevent path traversal.

Authentication:
  - Every route except /health requires a logged-in session. Credentials
    come from the environment (VIEWER_USERNAME + VIEWER_PASSWORD_HASH);
    the plaintext password is never stored anywhere.
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
import json
import math
from xml.parsers import expat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
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
from werkzeug.security import check_password_hash


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
DATA_ROOT = Path(os.environ.get("INGEST_DATA_ROOT", "/data"))
DB_PATH = Path(os.environ.get("VIEWER_DB_PATH", "/db/annotations.db"))

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
# /health stays open because Docker's healthcheck calls it and it leaks
# nothing; everything else -- including /static and the GPX files -- is
# behind the session check.
PUBLIC_ENDPOINTS = {"health", "login"}


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
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
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
    return (
        bool(VIEWER_USERNAME)
        and session.get("user") == VIEWER_USERNAME
        and _safe_equal(session.get("pw_tag", ""), _password_tag())
    )


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
def _csrf_protect():
    """Reject state-changing requests without a matching session token.

    Registered after _require_login, so it only ever sees authenticated
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
    """Load a trip's summary JSON: (summary, None), or (None, why not).

    Ingest writes summaries atomically, so a bad one means disk damage or
    a hand edit. One bad file must not 500 the trip list and both exports,
    so callers leave its miles out and report the trip as unreadable.
    """
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
        return None, problem
    return {
        field: summary[field]
        for field in SUMMARY_TEXT_FIELDS + SUMMARY_NUMBER_FIELDS
        if field in summary
    }, None


def load_trips():
    """Scan DATA_ROOT for trips and join annotations.

    Returns (trips, unreadable). trips is sorted by its date anchor descending,
    and each dict carries the summary fields plus: id, ym, trip_key,
    category, purpose_note, destination, has_gpx. unreadable holds the
    trips whose summary read_summary() rejected or couldn't find, sorted
    by trip_key, with the same keys minus the summary fields, plus
    problem. Their miles are unknown, so they're reported, never counted.
    """
    annotations = get_annotations()
    trips, unreadable = [], []
    if not DATA_ROOT.exists():
        return trips, unreadable

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

    trips.sort(key=lambda t: _trip_date_anchor(t) or "", reverse=True)
    unreadable.sort(key=lambda t: t["trip_key"])
    return trips, unreadable


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


def selected_year():
    raw_year = request.args.get("year", "")
    if raw_year and (not re.fullmatch(r"[0-9]{4}", raw_year) or int(raw_year) == 0):
        abort(400, description="Year must be a four-digit calendar year.")
    return int(raw_year) if raw_year else None


def _may_start_in(ym, year):
    """Whether a trip filed under folder ym could have started in year.

    Ingest files each trip under its local END month, and a New Year's
    Eve drive can end in January. A trip with an unreadable summary has no
    start time, so its folder is the only clue to its year.
    """
    return ym.startswith(f"{year}-") or ym == f"{year + 1}-01"


def selected_trips():
    """Use local start year, or first recorded time for partly timed trips.

    Returns (trips, unreadable, year, years). Unreadable trips have no
    start time, so they're the exception: they go by folder.
    """
    year = selected_year()
    trips, unreadable = load_trips()
    years = set()
    selected = []
    for trip in trips:
        local = _to_local(_parse(_trip_date_anchor(trip)))
        if local:
            years.add(local.year)
        if year is None or (local and local.year == year):
            selected.append(trip)
    if year:
        years.add(year)
        unreadable = [t for t in unreadable if _may_start_in(t["ym"], year)]
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


app.jinja_env.globals.update(
    fmt_date=fmt_date, fmt_trip_date=fmt_trip_date, fmt_duration=fmt_duration,
    fmt_speed=fmt_speed, csrf_token=_csrf_token
)


# ---------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------

BASE_CSS = """
  body { font-family: system-ui, sans-serif; margin: 0; color: #1a1a1a; }
  header { background: #1864ab; color: #fff; padding: 14px 22px;
           display: flex; align-items: center; justify-content: space-between; }
  header a { color: #fff; text-decoration: none; font-weight: 600; }
  header form.logout { margin: 0; }
  header form.logout button { background: rgba(255,255,255,.15); color: #fff;
           border: 1px solid rgba(255,255,255,.45); padding: 5px 12px;
           border-radius: 5px; font-size: 13px; cursor: pointer; }
  header form.logout button:hover { background: rgba(255,255,255,.28); }
  .wrap { padding: 22px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e0e0e0; }
  th { background: #f1f3f5; font-size: 13px; text-transform: uppercase; letter-spacing: .03em; }
  tr:hover td { background: #f8f9fa; }
  .tag { font-size: 12px; padding: 2px 8px; border-radius: 10px; }
  .business { background: #d3f9d8; color: #2b8a3e; }
  .personal { background: #e7f5ff; color: #1971c2; }
  .other    { background: #fff3bf; color: #e67700; }
  .muted { color: #868e96; }
  .warn { background: #fff5f5; color: #c92a2a; border: 1px solid #ffc9c9;
          padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
  .warn ul { margin: 6px 0 0; padding-left: 20px; }
  .btn { display: inline-block; background: #1864ab; color: #fff; padding: 8px 14px;
         border-radius: 6px; text-decoration: none; border: none; cursor: pointer; font-size: 14px; }
  .btn.secondary { background: #495057; }
  .toolbar { margin-bottom: 16px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .stats { display: flex; gap: 26px; flex-wrap: wrap; margin: 12px 0; }
  .stat b { display: block; font-size: 22px; }
  .stat span { color: #868e96; font-size: 13px; }
  #map { height: 460px; border: 1px solid #ced4da; border-radius: 8px; }
  form.annotate { margin-top: 18px; max-width: 520px; }
  label { display: block; margin: 10px 0 4px; font-weight: 600; font-size: 14px; }
  select, textarea, input[type=text] { width: 100%; padding: 8px; font-size: 14px; box-sizing: border-box; }
"""

LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Sign in · Mileage Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; background: #f1f3f5; margin: 0;
         display: flex; min-height: 100vh; align-items: center; justify-content: center; }
  .card { background: #fff; padding: 30px 32px; border-radius: 10px; width: 320px;
          box-shadow: 0 2px 14px rgba(0,0,0,.10); }
  h1 { font-size: 19px; margin: 0 0 20px; color: #1864ab; }
  label { display: block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input { width: 100%; padding: 9px; font-size: 14px; box-sizing: border-box;
          border: 1px solid #ced4da; border-radius: 5px; }
  button { width: 100%; margin-top: 20px; background: #1864ab; color: #fff; border: none;
           padding: 10px; border-radius: 6px; font-size: 15px; cursor: pointer; }
  .err { background: #fff5f5; color: #c92a2a; border: 1px solid #ffc9c9;
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
  </div>
</body></html>
"""

LIST_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Mileage Trips</title>
<style>""" + BASE_CSS + """</style></head><body>
<header><a href="{{ url_for('index') }}">🚗 Mileage Tracker</a>
  <form class="logout" method="post" action="{{ url_for('logout') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit">Log out</button>
  </form>
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
      <li><code>{{ u.trip_key }}.json</code>{% if not u.has_gpx %} (no GPX either){% endif %}: {{ u.problem }}</li>
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
      <td>{{ fmt_trip_date(t) }}{% if t.get('timing_status') == 'partial' %}<br><span class="muted">Partial timing</span>{% endif %}</td>
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
<!doctype html><html><head><meta charset="utf-8"><title>Trip {{ t.id }}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='leaflet.css') }}">
<style>""" + BASE_CSS + """</style></head><body>
<header><a href="{{ url_for('index', year=year or '') }}">← {{ year or 'All' }} trips</a>
  <form class="logout" method="post" action="{{ url_for('logout') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit">Log out</button>
  </form>
</header>
<div class="wrap">
  <h2>{{ fmt_trip_date(t) }}</h2>
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
    <label for="purpose_note">Note (e.g. "Client visit — Acme Corp")</label>
    <textarea name="purpose_note" id="purpose_note" rows="3">{{ t.purpose_note or "" }}</textarea>
    <p><button class="btn" type="submit">Save</button></p>
  </form>
</div>

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
    gpx_options: { parseElements: ['track', 'route'] },
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
# Routes
# ---------------------------------------------------------------------


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "mileage-viewer"}), 200


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
        pass_ok = check_password_hash(VIEWER_PASSWORD_HASH, password)

        if name_ok and pass_ok:
            _clear_failures(ip)
            session.clear()  # new session id on privilege change
            session["user"] = VIEWER_USERNAME
            session["pw_tag"] = _password_tag()
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(_safe_next(request.args.get("next")) or url_for("index"))

        _record_failure(ip)
        app.logger.warning("Failed login for %r from %s", username[:40], ip)
        error = "Incorrect username or password."
        return render_template_string(LOGIN_HTML, error=error), 401

    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    trips, unreadable, year, years = selected_trips()
    return render_template_string(
        LIST_HTML, trips=trips, unreadable=unreadable, year=year, years=years,
        totals=mileage_totals(trips), timezone=DISPLAY_TZ_NAME,
    )


@app.route("/trip/<ym>/<trip_id>")
def trip_view(ym, trip_id):
    year = selected_year()
    gpx_path, json_path = _safe_trip_paths(ym, trip_id)
    if not json_path.exists() and not gpx_path.exists():
        abort(404)
    summary, problem = read_summary(json_path)
    if problem:
        abort(404, description=(
            f"This trip's summary file can't be read ({problem}), so its "
            "miles can't be counted. The trip list and exports flag it. To "
            "rebuild it from the GPX, see DEPLOY_DEBIAN.md → Troubleshooting."
        ))
    ann = get_annotations().get(f"{ym}/{trip_id}", {})
    t = {
        **summary,
        "id": trip_id,
        "ym": ym,
        "category": ann.get("category", ""),
        "purpose_note": ann.get("purpose_note", ""),
        "destination": ann.get("destination", ""),
        "has_gpx": gpx_path.exists(),
    }
    return render_template_string(TRIP_HTML, t=t, categories=CATEGORIES, year=year)


@app.route("/gpx/<ym>/<trip_id>")
def trip_gpx(ym, trip_id):
    gpx_path, _ = _safe_trip_paths(ym, trip_id)
    if not gpx_path.exists():
        abort(404)
    return send_file(gpx_path, mimetype="application/gpx+xml")


@app.route("/trip/<ym>/<trip_id>/annotate", methods=["POST"])
def annotate(ym, trip_id):
    year = selected_year()
    _, json_path = _safe_trip_paths(ym, trip_id)
    if not json_path.exists():
        abort(404)
    category = request.form.get("category", "").strip()
    if category not in CATEGORIES:
        category = ""  # allow clearing the tag
    note = request.form.get("purpose_note", "").strip()
    destination = request.form.get("destination", "").strip()
    upsert_annotation(f"{ym}/{trip_id}", category, note, destination)
    return redirect(url_for("trip_view", ym=ym, trip_id=trip_id, year=year or ""))


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
]


def track_endpoints(trip):
    """First/last recorded track coordinates, including legacy GPX files.

    Stream without constructing an XML tree, reject DTDs/entities, and ignore
    waypoints. Invalid or absent tracks yield blanks rather than invented
    coordinates. These are recorded endpoints, not reverse-geocoded places.
    """
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


UNREADABLE_COLUMNS = ("trip_id", "gpx_file", "category", "purpose_note", "destination")


def _unreadable_values(t):
    """Export fields for a trip whose summary can't be read.

    Just what identifies it and what was typed for it. Its miles are
    unknown, so they stay blank rather than reading as zero.
    """
    return {
        "trip_id": t["id"],
        "gpx_file": f"{t['id']}.gpx" if t["has_gpx"] else "",
        "category": t["category"],
        "purpose_note": t["purpose_note"],
        "destination": t["destination"],
    }


@app.route("/export.csv")
def export_csv():
    trips, unreadable, year, _ = selected_trips()
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
    trips, unreadable, year, _ = selected_trips()
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
