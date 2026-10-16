"""Optional, durable JSON-line push to an external Loki (#136)."""

import base64
import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import gelf
from common.diagnostics import validate_device_id, validate_request


TIMEOUT_SECONDS = 10
MAX_PER_TICK = 200
SEVERITIES = ("info", "warning", "error", "critical", "unknown")
DECISIONS = ("alert", "reminder", "resolution")
SOURCE = "mileage-tracker"
CONDITION_STATES = ("active", "clear", "unknown")


class PermanentRejection(Exception):
    """A permanent HTTP 4xx rejection of one Loki batch."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(status_code)


SCHEMA = """
CREATE TABLE IF NOT EXISTS loki_cursor (
    singleton INTEGER PRIMARY KEY, event_rowid INTEGER NOT NULL, decision_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS loki_retry (
    singleton INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS loki_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at REAL NOT NULL,
    device TEXT NOT NULL, code INTEGER NOT NULL, decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loki_conditions (
    device TEXT NOT NULL, code INTEGER NOT NULL, state TEXT NOT NULL, episode TEXT,
    PRIMARY KEY (device, code)
);
CREATE TABLE IF NOT EXISTS loki_streams (
    device TEXT NOT NULL, severity TEXT NOT NULL, timestamp_ns INTEGER NOT NULL,
    PRIMARY KEY (device,severity)
);
"""
SEED = ("INSERT OR IGNORE INTO loki_cursor VALUES (1,0,0)",
        "INSERT OR IGNORE INTO loki_retry VALUES (1,0,0)")


def _valid_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(host and len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.rstrip(".").split(".")))


def _ascii_header(value, maximum=1024):
    return bool(value and len(value) <= maximum and all(32 <= ord(char) <= 126 for char in value))


@dataclass(frozen=True)
class LokiConfig:
    enabled: bool = False
    url: str = ""
    basic_user: str = ""
    basic_password: str = ""
    bearer_token: str = ""
    tenant: str = ""

    @classmethod
    def from_env(cls, env):
        url = env.get("LOKI_PUSH_URL", "")
        user = env.get("LOKI_BASIC_AUTH_USER", "")
        password = env.get("LOKI_BASIC_AUTH_PASSWORD", "")
        token = env.get("LOKI_BEARER_TOKEN", "")
        tenant = env.get("LOKI_TENANT_ID", "")
        if not url:
            if user or password or token or tenant:
                raise ValueError("LOKI_PUSH_URL is required when Loki credentials or tenant are set")
            return cls()
        try:
            parsed = urllib.parse.urlsplit(url)
            valid_url = (parsed.scheme in ("http", "https") and _valid_host(parsed.hostname or "")
                         and parsed.port != 0 and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment
                         and parsed.path.rstrip("/").endswith("/loki/api/v1/push")
                         and not any(char.isspace() for char in url))
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("LOKI_PUSH_URL must be an http(s) Loki /loki/api/v1/push URL without credentials or query")
        basic = bool(user or password)
        if basic and (not user or not password or not _ascii_header(user)
                      or ":" in user or not _ascii_header(password)):
            raise ValueError("Loki basic auth requires a printable username and password")
        if token and (not _ascii_header(token) or token == "<token>"
                      or any(char.isspace() for char in token)):
            raise ValueError("LOKI_BEARER_TOKEN must be a nonempty printable ASCII token")
        if basic and token:
            raise ValueError("Configure Loki basic auth or bearer auth, not both")
        if tenant and not _ascii_header(tenant, 128):
            raise ValueError("LOKI_TENANT_ID must be at most 128 printable ASCII characters")
        return cls(True, url, user, password, token, tenant)

    def headers(self):
        result = {"Content-Type": "application/json"}
        if self.bearer_token:
            result["Authorization"] = "Bearer " + self.bearer_token
        elif self.basic_user:
            raw = (self.basic_user + ":" + self.basic_password).encode("utf-8")
            result["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        if self.tenant:
            result["X-Scope-OrgID"] = self.tenant
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Loki credentials and tenant headers never follow redirects.


class HttpTransport:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, payload):
        request = urllib.request.Request(self.config.url, payload, method="POST",
                                         headers=self.config.headers())
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    raise OSError("Loki rejected the push")
                response.read(1024)
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            exc.close()
            if 400 <= status_code < 500 and status_code not in (408, 429):
                raise PermanentRejection(status_code) from None
            raise


def transport(config):
    return HttpTransport(config)


def _iso_timestamp_ns(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return int(stamp.timestamp()) * 1_000_000_000 + stamp.microsecond * 1_000


def _seconds_timestamp_ns(value):
    stamp = float(value)
    if not math.isfinite(stamp):
        raise ValueError("invalid timestamp")
    seconds = math.floor(stamp)
    return seconds * 1_000_000_000 + int((stamp - seconds) * 1_000_000_000)


def event_log(catalog, row):
    """Return (device, severity), timestamp, and a sanitized JSON log line."""
    try:
        device = row[1]
        validate_device_id(device)
        codes = {item["code"]: item for item in catalog["codes"]}
        event = json.loads(row[6])
        if (not isinstance(event, dict) or not isinstance(event.get("context"), dict)
                or (event.get("boot_id"), event.get("sequence"), event.get("code")) !=
                (row[2], row[3], row[4])):
            return None
        definition = codes.get(row[4])
        allowed_context = set(definition.get("context", {})) if definition else set()
        safe_event = dict(event)
        safe_event["context"] = {key: value for key, value in event["context"].items()
                                 if key in allowed_context}
        # Revalidate stored rows against the current protocol/catalog before
        # export. Unknown context keys are dropped; malformed protocol fields
        # are skipped instead of becoming log text.
        validate_request({"schema_version": 1,
                          "checkin": {"boot_id": row[2], "status_sequence": 1,
                                      "uptime_ms": 0, "active": []},
                          "events": [safe_event]}, catalog=catalog)
        safe_row = (*row[:6], json.dumps(safe_event, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False))
        payload = gelf.event_message(codes, safe_row)
        if payload is None:
            return None
        message = json.loads(payload.decode("ascii"))
        severity = message["_severity"]
        if severity not in SEVERITIES:
            return None
        timestamp = _iso_timestamp_ns(message["_received_at"])
        # gelf.event_message supplies catalog titles and bounded field shapes.
        fields = {key[1:] if key.startswith("_") else key: value
                  for key, value in message.items()
                  if key not in ("version", "host", "timestamp", "short_message", "level")
                  }
        line = json.dumps(fields, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return (device, severity), timestamp, line
    except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def condition_log(codes, device, code, state, now):
    """Return a bounded current-condition state-change line."""
    try:
        validate_device_id(device)
        if type(code) is not int or state not in CONDITION_STATES:
            return None
        definition = codes.get(code)
        if definition is None or definition.get("severity") not in SEVERITIES:
            return None
        timestamp = _seconds_timestamp_ns(now)
        fields = {"kind": "condition_change", "device_id": device, "code": code,
                  "symbol": definition["symbol"], "severity": definition["severity"],
                  "title": definition["title"], "state": state}
        line = json.dumps(fields, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return (device, definition["severity"]), timestamp, line
    except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def decision_log(codes, row):
    """Return a bounded alert decision line from the persisted monitor outbox."""
    _, occurred_at, device, code, decision = row
    try:
        validate_device_id(device)
        if decision not in DECISIONS or type(code) is not int:
            return None
        definition = codes.get(code)
        if definition is None:
            return None
        timestamp = _seconds_timestamp_ns(occurred_at)
        fields = {"kind": "alert_decision", "device_id": device, "code": code,
                  "symbol": definition["symbol"], "severity": definition["severity"],
                  "title": definition["title"], "decision": decision}
        line = json.dumps(fields, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return (device, definition["severity"]), timestamp, line
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def push_payload(items, previous_timestamps):
    """Build Loki streams with strictly ascending timestamps in each stream.

    Loki rejects older entries in an already-open stream. Row order is the
    durable cursor order, but restored clocks or an event batch can carry tied
    timestamps, so sort each push and advance by one nanosecond beyond the
    last timestamp already accepted for that label set.
    """
    grouped = defaultdict(list)
    for labels, timestamp, line in items:
        grouped[labels].append((timestamp, line))
    streams, updated = [], {}
    for (device, severity), entries in sorted(grouped.items()):
        entries.sort(key=lambda item: item[0])
        previous = previous_timestamps.get((device, severity), -1)
        values = []
        for timestamp, line in entries:
            previous = max(timestamp, previous + 1)
            values.append([str(previous), line])
        updated[(device, severity)] = previous
        streams.append({"stream": {"job": SOURCE, "device_id": device, "severity": severity},
                        "values": values})
    payload = json.dumps({"streams": streams}, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False).encode("utf-8")
    return payload, updated
