"""Opt-in endpoint suggestions; no network activity during reads or exports."""

from contextlib import closing
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
import math
import os
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class LookupError(Exception):
    """A safe, fixed message suitable for display; never an upstream error."""

    def __init__(self, message, status=503, cooldown=300):
        super().__init__(message)
        self.status = status
        self.cooldown = cooldown


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Geocoder:
    MAX_RESPONSE = 65536
    MAX_LABEL = 1000
    INTERVAL = 1.1
    LEASE = 60

    def __init__(self, db_path):
        self.db_path = db_path
        self.url = os.environ.get("VIEWER_GEOCODER_URL", "").strip()
        self.user_agent = os.environ.get(
            "VIEWER_GEOCODER_USER_AGENT", "GPSMileageTracker/1.0"
        ).strip()
        if self.url:
            try:
                parts = urlsplit(self.url)
                host = (parts.hostname or "").lower().rstrip(".")
                valid = (parts.scheme in {"http", "https"} and host
                         and parts.username is None and parts.password is None
                         and not parts.query and not parts.fragment
                         and self.url.isascii() and len(self.url) <= 2048
                         and not any(ord(c) <= 32 or ord(c) == 127 for c in self.url)
                         and parts.port != 0
                         and host != "openstreetmap.org"
                         and not host.endswith(".openstreetmap.org")
                         and self.user_agent and self.user_agent.isascii() and len(self.user_agent) <= 256
                         and not any(ord(c) < 32 or ord(c) == 127 for c in self.user_agent))
            except ValueError:
                valid = False
            if not valid:
                raise RuntimeError(
                    "Invalid VIEWER_GEOCODER_URL or VIEWER_GEOCODER_USER_AGENT. "
                    "Use a self-hosted or approved Nominatim reverse endpoint, "
                    "without URL credentials, query or fragment. The public "
                    "OpenStreetMap service does not permit vehicle tracking."
                ) from None
        # Avoid ambient proxies and never forward an endpoint via a redirect.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    @property
    def enabled(self):
        return bool(self.url)

    @staticmethod
    def init_schema(conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS geocode_cache (
            cache_key TEXT PRIMARY KEY, label TEXT NOT NULL, looked_up_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS geocode_gate (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            next_allowed REAL NOT NULL, token TEXT NOT NULL
        )""")

    def key(self, latitude, longitude):
        if not (isinstance(latitude, (float, int)) and not isinstance(latitude, bool)
                and isinstance(longitude, (float, int)) and not isinstance(longitude, bool)
                and math.isfinite(latitude) and math.isfinite(longitude)
                and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise LookupError("This recording has no valid endpoint to look up.", 422)
        # Exact numeric positions; no rounding that could select a neighbour.
        raw = f"{self.url}\n{float(latitude)!r}\n{float(longitude)!r}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def cached(self, latitude, longitude):
        key = self.key(latitude, longitude)
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return None if row is None else row[0]

    def lookup(self, latitude, longitude):
        if not self.enabled:
            raise LookupError("Address lookup is not configured.", 503)
        key = self.key(latitude, longitude)
        token = os.urandom(16).hex()
        # A short transaction reserves the provider across all workers. Never
        # hold a SQLite write lock during HTTP or block manual annotation saves.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is not None:
                return row[0]
            gate = conn.execute("SELECT next_allowed FROM geocode_gate WHERE singleton = 1").fetchone()
            now = time.time()
            if gate and now < gate[0]:
                raise LookupError("Address lookup is busy or cooling down. Try again later.", 429)
            conn.execute(
                "INSERT OR REPLACE INTO geocode_gate VALUES (1, ?, ?)", (now + self.LEASE, token)
            )
        cooldown = self.INTERVAL
        try:
            label = self._fetch(latitude, longitude)
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache VALUES (?, ?, ?)",
                    (key, label, time.time()),
                )
            return label
        except LookupError as exc:
            # Back off on all provider failures, including rate limits, rather
            # than letting repeated clicks or another worker hammer a failure.
            cooldown = exc.cooldown
            raise
        finally:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "UPDATE geocode_gate SET next_allowed = ? WHERE singleton = 1 AND token = ?",
                    (time.time() + cooldown, token),
                )

    def _fetch(self, latitude, longitude):
        query = urlencode({
            "lat": latitude, "lon": longitude, "format": "jsonv2",
            "zoom": 18, "addressdetails": 0, "accept-language": "en",
        })
        req = Request(self.url + "?" + query, headers={
            "User-Agent": self.user_agent, "Accept": "application/json",
        })
        try:
            try:
                response = self.opener.open(req, timeout=5)
            except HTTPError as exc:
                # Some compatible providers return no coverage as HTTP 404.
                # Only the exact, bounded no-match body is cacheable.
                if exc.code != 404:
                    delay = self._retry_delay(exc.headers.get("Retry-After", ""))
                    exc.close()
                    raise LookupError("The address provider is unavailable. Try again later.",
                                      cooldown=delay) from None
                response = exc
            with response:
                raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise ValueError("oversized")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("invalid object")
            # Nominatim uses this successful JSON response for uncovered areas.
            if data.get("error") == "Unable to geocode":
                return ""
            if response.code != 200:
                raise ValueError("unexpected status")
            label = data.get("display_name")
            if ("error" in data or not isinstance(label, str) or not label.strip()
                    or len(label) > self.MAX_LABEL
                    or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                raise ValueError("invalid label")
            return label.strip()
        except (OSError, URLError, HTTPException, ValueError, UnicodeError, RecursionError):
            raise LookupError("The address provider is unavailable. Try again later.") from None

    @staticmethod
    def _retry_delay(value):
        try:
            if value.isdecimal():
                delay = int(value)
            else:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            return max(300, min(delay, 7 * 86400))
        except (ValueError, TypeError, OverflowError):
            return 300
