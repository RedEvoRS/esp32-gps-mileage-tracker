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


class GeocodeError(Exception):
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
    RESPONSE_BUDGET = 10
    # Bump when the label format changes so older cached labels are not served.
    LABEL_FORMAT = 2

    def __init__(self, db_path):
        self.db_path = db_path
        self.url = os.environ.get("VIEWER_GEOCODER_URL", "").strip()
        self.user_agent = os.environ.get(
            "VIEWER_GEOCODER_USER_AGENT", "GPSMileageTracker/1.0"
        ).strip()
        try:
            self.interval = float(os.environ.get("VIEWER_GEOCODER_INTERVAL", "").strip() or self.INTERVAL)
            if not math.isfinite(self.interval) or not 0.1 <= self.interval <= 60:
                raise ValueError
        except ValueError:
            raise RuntimeError(
                "Invalid VIEWER_GEOCODER_INTERVAL. Use a finite number from 0.1 to 60 seconds."
            ) from None
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
            raise GeocodeError("This recording has no valid endpoint to look up.", 422)
        # Exact numeric positions; no rounding that could select a neighbour.
        raw = (f"{self.url}\nlabel-format-{self.LABEL_FORMAT}"
               f"\n{float(latitude)!r}\n{float(longitude)!r}")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def cached(self, latitude, longitude):
        key = self.key(latitude, longitude)
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return None if row is None else row[0]

    def lookup(self, latitude, longitude):
        if not self.enabled:
            raise GeocodeError("Address lookup is not configured.", 503)
        key = self.key(latitude, longitude)
        token = os.urandom(16).hex()
        # A short transaction reserves the provider across all workers. Never
        # hold a SQLite write lock during HTTP or block manual annotation saves.
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT label FROM geocode_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is not None:
                return row[0]
            gate = conn.execute("SELECT next_allowed FROM geocode_gate WHERE singleton = 1").fetchone()
            now = time.time()
            if gate and now < gate[0]:
                raise GeocodeError("Address lookup is busy or cooling down. Try again later.", 429)
            conn.execute(
                "INSERT OR REPLACE INTO geocode_gate VALUES (1, ?, ?)", (now + self.LEASE, token)
            )
        # Worker aborts (SystemExit/KeyboardInterrupt) still run finally. Treat
        # any incomplete provider call as failure, including those abort paths.
        cooldown = 300
        try:
            label = self._fetch(latitude, longitude)
            cooldown = self.interval
            with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache VALUES (?, ?, ?)",
                    (key, label, time.time()),
                )
            return label
        except GeocodeError as exc:
            # Back off on all provider failures, including rate limits, rather
            # than letting repeated clicks or another worker hammer a failure.
            cooldown = exc.cooldown
            raise
        finally:
            with closing(sqlite3.connect(self.db_path, timeout=30)) as conn, conn:
                conn.execute(
                    "UPDATE geocode_gate SET next_allowed = ? WHERE singleton = 1 AND token = ?",
                    (time.time() + cooldown, token),
                )

    def _fetch(self, latitude, longitude):
        query = urlencode({
            "lat": latitude, "lon": longitude, "format": "jsonv2",
            "zoom": 18, "addressdetails": 1, "accept-language": "en",
        })
        req = Request(self.url + "?" + query, headers={
            "User-Agent": self.user_agent, "Accept": "application/json",
        })
        deadline = time.monotonic() + self.RESPONSE_BUDGET
        # Keep the opener and its handlers local to this call, so threads share
        # only configuration. Avoid ambient proxies and never follow redirects.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            try:
                response = opener.open(req, timeout=5)
            except HTTPError as exc:
                # Some compatible providers return no coverage as HTTP 404.
                # Only the exact, bounded no-match body is cacheable.
                if exc.code != 404:
                    delay = self._retry_delay(exc.headers.get("Retry-After", ""))
                    exc.close()
                    raise GeocodeError("The address provider is unavailable. Try again later.",
                                      cooldown=delay) from None
                response = exc
            with response:
                raw = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("response deadline")
                    # read1 returns available bytes instead of waiting for a
                    # complete buffer. Slow-drip bodies cannot postpone the
                    # deadline check until all 64 KiB have arrived.
                    chunk = response.read1(min(8192, self.MAX_RESPONSE + 1 - len(raw)))
                    if time.monotonic() >= deadline:
                        raise TimeoutError("response deadline")
                    raw.extend(chunk)
                    if not chunk or len(raw) > self.MAX_RESPONSE:
                        break
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
            label = self._format_label(data)
            if ("error" in data or not isinstance(label, str) or not label.strip()
                    or len(label) > self.MAX_LABEL
                    or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                raise ValueError("invalid label")
            return label.strip()
        except (OSError, URLError, HTTPException, ValueError, UnicodeError, RecursionError):
            raise GeocodeError("The address provider is unavailable. Try again later.") from None

    @staticmethod
    def _format_label(data):
        """Standard address from the structured fields, else display_name."""
        fallback = data.get("display_name")
        address = data.get("address")
        if not isinstance(address, dict):
            return fallback

        def text(source, key):
            value = source.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else ""

        number, road = text(address, "house_number"), text(address, "road")
        city = next((text(address, k) for k in
                     ("city", "town", "village", "hamlet", "municipality")
                     if text(address, k)), "")
        street = f"{number} {road}".strip() if road else ""
        if not street and not city:
            return fallback
        state, postcode = text(address, "state"), text(address, "postcode")
        if text(address, "country_code").lower() == "us":
            code = text(address, "ISO3166-2-lvl4")
            if code.startswith("US-") and len(code) == 5 and code[3:].isalpha():
                state = code[3:].upper()
            tail = [city, f"{state} {postcode}".strip()]
        else:
            tail = [city, state, postcode, text(address, "country")]
        name = text(data, "name")
        lead = [] if name in {"", number, road, city} else [name]
        return ", ".join(part for part in lead + [street] + tail if part)

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
