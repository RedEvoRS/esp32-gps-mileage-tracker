"""Optional GELF export of diagnostics to an external Graylog (#135).

Graylog does its own alerting, so this sink forwards facts rather than ntfy's
rate-capped notification decisions:

- ``event`` messages: every journal event in events.db, each forwarded once,
  in rowid order, resuming from a cursor persisted in monitor.db.
- ``condition`` messages: a change in a device/code condition's state
  (active, clear or unknown), plus server-side silence (E7001). State is
  tracked per device and code, not per device episode, because every
  deep-sleep wake starts a new boot_id; a fault that persists across wakes is
  one active condition, not a new one per wake.

Only catalog fields, validated identifiers and catalog-bounded context leave
the server; never free text, coordinates, SSIDs or credentials. Delivery is at
least once with its own persistent retry state, independent of ntfy.
"""

import hashlib
import ipaddress
import json
import re
import socket
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from common.settings import boolean as _boolean


SOURCE = "mileage-tracker"  # GELF "host": one fixed value to filter streams on.
LEVELS = {"critical": 2, "error": 3, "warning": 4, "info": 6}  # syslog levels
UNKNOWN_CODE_LEVEL = 5  # notice: a code this server's catalog doesn't know yet
CLEAR_LEVEL = 6         # informational: clear/unknown states and clear events
MAX_PER_TICK = 200
TIMEOUT_SECONDS = 10
FIELD = re.compile(r"[A-Za-z0-9_.\-]{1,64}")  # GELF additional field names
SILENCE = 7001

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_cursor_markers (
    sink TEXT PRIMARY KEY, event_rowid INTEGER NOT NULL, fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gelf_cursor (singleton INTEGER PRIMARY KEY, event_rowid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gelf_conditions (
    device TEXT NOT NULL, code INTEGER NOT NULL, state TEXT NOT NULL, episode TEXT,
    PRIMARY KEY (device, code)
);
CREATE TABLE IF NOT EXISTS gelf_retry (
    singleton INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL
);
"""
SEED = ("INSERT OR IGNORE INTO gelf_cursor VALUES (1,0)",
        "INSERT OR IGNORE INTO gelf_retry VALUES (1,0,0)")


class DeliveryError(Exception):
    """A batch failed part way; ``delivered`` leading messages were accepted."""

    def __init__(self, delivered, cause):
        super().__init__(type(cause).__name__)
        self.delivered = delivered
        self.cause = cause


def _valid_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(host and len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.rstrip(".").split(".")))


@dataclass(frozen=True)
class GelfConfig:
    enabled: bool = False
    url: str = ""          # GELF HTTP input, e.g. http://<host>:12201/gelf
    tcp_host: str = ""     # or a GELF TCP input
    tcp_port: int = 0
    tls: bool = False      # TCP only; HTTPS URLs always use TLS
    tls_verify: bool = True
    ca_file: str = ""
    header_name: str = ""  # optional extra HTTP header (proxy/auth in front of Graylog)
    header_value: str = ""

    @classmethod
    def from_env(cls, env):
        if not _boolean(env, "GRAYLOG_ENABLED", "false"):
            return cls()
        url, tcp = env.get("GRAYLOG_GELF_URL", ""), env.get("GRAYLOG_GELF_TCP", "")
        if bool(url) == bool(tcp):
            raise ValueError("Set exactly one of GRAYLOG_GELF_URL or GRAYLOG_GELF_TCP")
        tls = _boolean(env, "GRAYLOG_GELF_TLS", "false")
        verify = _boolean(env, "GRAYLOG_GELF_TLS_VERIFY", "true")
        ca_file = env.get("GRAYLOG_GELF_CA_FILE", "")
        if ca_file and not Path(ca_file).is_file():
            raise ValueError("GRAYLOG_GELF_CA_FILE must name a readable certificate file")
        host, port = "", 0
        if url:
            try:
                parsed = urllib.parse.urlsplit(url)
                valid = (parsed.scheme in ("http", "https") and _valid_host(parsed.hostname or "")
                         and parsed.port != 0 and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment
                         and not any(c.isspace() for c in url))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("GRAYLOG_GELF_URL must be an http(s) URL without credentials, query or fragment")
            if tls:
                raise ValueError("GRAYLOG_GELF_TLS applies to GRAYLOG_GELF_TCP; use an https URL instead")
        else:
            match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([^:\[\]\s]+):(\d{1,5})", tcp)
            if match:
                host = match.group(1) or match.group(3)
                port = int(match.group(2) or match.group(4))
            if not match or not _valid_host(host) or not 1 <= port <= 65535:
                raise ValueError("GRAYLOG_GELF_TCP must be <host>:<port>")
        name, value = "", ""
        header = env.get("GRAYLOG_GELF_HEADER", "")
        if header:
            if not url:
                raise ValueError("GRAYLOG_GELF_HEADER applies to GRAYLOG_GELF_URL only")
            name, _, value = header.partition(":")
            name, value = name.strip(), value.strip()
            if (not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) or not value
                    or any(not 32 <= ord(c) <= 126 for c in value)):
                raise ValueError("GRAYLOG_GELF_HEADER must be 'Name: value' in printable ASCII")
        return cls(True, url, host, port, tls, verify, ca_file, name, value)


def _ssl_context(config):
    context = ssl.create_default_context(cafile=config.ca_file or None)
    if not config.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a configured header through a redirect.


class HttpTransport:
    """GELF HTTP input: one message per POST; Graylog answers 202 Accepted."""

    def __init__(self, config):
        self.config = config
        handlers = [_NoRedirect()]
        if config.url.startswith("https:"):
            handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(config)))
        self.opener = urllib.request.build_opener(*handlers)

    def __call__(self, payloads):
        headers = {"Content-Type": "application/json"}
        if self.config.header_name:
            headers[self.config.header_name] = self.config.header_value
        for delivered, payload in enumerate(payloads):
            request = urllib.request.Request(self.config.url, payload, method="POST", headers=headers)
            try:
                with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                    if not 200 <= response.status < 300:
                        raise OSError("Graylog rejected the message")
                    response.read(1024)
            except urllib.error.HTTPError as exc:
                exc.close()
                raise DeliveryError(delivered, exc) from None
            except Exception as exc:
                raise DeliveryError(delivered, exc) from None
        return len(payloads)


class TcpTransport:
    """GELF TCP input: null-terminated JSON frames on one connection per batch.

    TCP gives no per-message receipt, so a failure anywhere counts the whole
    batch as undelivered and it is resent (at least once). Prefer HTTP when
    the input supports it.
    """

    def __init__(self, config):
        self.config = config

    def __call__(self, payloads):
        try:
            raw = socket.create_connection((self.config.tcp_host, self.config.tcp_port),
                                           timeout=TIMEOUT_SECONDS)
            with closing(raw):
                stream = raw
                if self.config.tls:
                    stream = _ssl_context(self.config).wrap_socket(raw, server_hostname=self.config.tcp_host)
                with closing(stream):
                    for payload in payloads:
                        stream.sendall(payload + b"\0")
                    stream.shutdown(socket.SHUT_WR)
        except Exception as exc:
            raise DeliveryError(0, exc) from None
        return len(payloads)


def transport(config):
    return HttpTransport(config) if config.url else TcpTransport(config)


def _epoch(iso):
    stamp = datetime.fromisoformat(iso)
    if stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return stamp.timestamp()


def _describe(codes, code):
    definition = codes.get(code)
    if definition is None:
        return f"Unknown E{code}", f"E{code}", "unknown", UNKNOWN_CODE_LEVEL
    return definition["title"], definition["symbol"], definition["severity"], LEVELS[definition["severity"]]


def _encode(message):
    for key in message:
        if key.startswith("_") and (key == "_id" or not FIELD.fullmatch(key[1:])):
            raise ValueError("invalid GELF field name")
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def condition_message(codes, device, code, state, episode, now, contact=None):
    title, symbol, severity, level = _describe(codes, code)
    message = {"version": "1.1", "host": SOURCE, "timestamp": round(now, 3),
               "short_message": f"E{code} {title}: {state} on {device}",
               "level": level if state == "active" else CLEAR_LEVEL,
               "_kind": "condition", "_device_id": device, "_code": code, "_symbol": symbol,
               "_severity": severity, "_state": state, "_title": title}
    if episode:
        message["_episode_id"] = episode
    if code == SILENCE and state == "active" and contact is not None:
        message["_days_without_contact"] = max(0, int((now - contact) // 86400))
    return _encode(message)


def event_message(codes, row):
    """Build an event message, or return None for a damaged stored row."""
    _, device, boot_id, sequence, code, received_at, canonical = row
    try:
        event = json.loads(canonical)
        timestamp = _epoch(received_at)
        if not isinstance(event, dict) or not isinstance(event.get("context", {}), dict):
            return None
        title, symbol, severity, level = _describe(codes, code)
        transition = event.get("transition")
        if transition not in ("raise", "repeat", "clear", "notice"):
            return None
        message = {"version": "1.1", "host": SOURCE, "timestamp": round(timestamp, 3),
                   "short_message": f"E{code} {title} ({transition}) on {device}",
                   "level": CLEAR_LEVEL if transition == "clear" else level,
                   "_kind": "event", "_device_id": device, "_code": code, "_symbol": symbol,
                   "_severity": severity, "_title": title, "_transition": transition,
                   "_boot_id": boot_id, "_sequence": sequence, "_received_at": received_at}
        for key, field, kind in (("episode_id", "_episode_id", str), ("firmware_version", "_firmware_version", str),
                                 ("time_source", "_time_source", str), ("utc_ms", "_device_utc_ms", int),
                                 ("sync_age_ms", "_sync_age_ms", int), ("uptime_ms", "_uptime_ms", int)):
            value = event.get(key)
            if type(value) is kind and (kind is int or len(value) <= 80):
                message[field] = value
        for key, value in event.get("context", {}).items():
            # Context was allowlisted by the catalog at ingest; re-check shape
            # so a damaged row can't smuggle free text or odd field names out.
            if not (isinstance(key, str) and FIELD.fullmatch(key) and len(key) <= 32):
                continue
            if type(value) is bool:
                message["_ctx_" + key] = "true" if value else "false"
            elif type(value) is int or (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,32}", value)):
                message["_ctx_" + key] = value
        return _encode(message)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def desired_conditions(devices, known, now, stale_seconds, snapshot_max_age):
    """Return {(device, code): (state, episode, contact)} that differ from ``known``.

    ``known`` maps (device, code) to the (state, episode) last delivered. A
    condition known to this sink but absent from current evidence becomes
    unknown; an episode change while it stays active is not a change.
    """
    current = {}
    for device in devices:
        name, contact = device["device_id"], device.get("contact")
        if contact is None:
            continue  # damaged identity/contact: everything for it becomes unknown
        silent = now - contact >= stale_seconds
        current[(name, SILENCE)] = ("active" if silent else "clear",
                                    "silence:" + device["last_contact_at"] if silent else None, contact)
        snapshot = device.get("snapshot")
        if snapshot is None or not 0 <= now - device["snapshot_at"] <= snapshot_max_age:
            continue
        active = {item["code"]: item["episode_id"] for item in snapshot["active"]}
        for code in set(snapshot.get("observed", [])) - set(active):
            current[(name, code)] = ("clear", None, None)
        for code, episode in active.items():
            current[(name, code)] = ("active", episode, None)
    for key, (state, _) in known.items():
        if key not in current and state != "unknown":
            current[key] = ("unknown", None, None)
    changes = {}
    for key, (state, episode, contact) in current.items():
        previous = known.get(key, ("unknown", None))[0]
        if state != previous:
            changes[key] = (state, episode, contact)
    return changes


def read_events(path, after_rowid, limit):
    """Read rows and cursor identity from the same SQLite snapshot."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        highest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM events").fetchone()[0]
        rows = db.execute("SELECT rowid,device_id,boot_id,sequence,code,received_at,canonical_json "
                          "FROM events WHERE rowid>? ORDER BY rowid LIMIT ?", (after_rowid, limit)).fetchall()
        anchor = db.execute("SELECT rowid,device_id,boot_id,sequence,code,received_at,canonical_json "
                            "FROM events WHERE rowid=?", (after_rowid,)).fetchone()
        return highest, rows, event_fingerprint(anchor) if anchor else None


def event_fingerprint(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=True).encode("ascii")).hexdigest()


def cursor_changed(db, sink, cursor, fingerprint):
    if not cursor:
        return False
    stored = db.execute("SELECT event_rowid,fingerprint FROM event_cursor_markers WHERE sink=?",
                        (sink,)).fetchone()
    # A pre-marker monitor.db replays once on upgrade rather than trusting an
    # unverifiable cursor that may already refer to a restored database.
    return stored is None or tuple(stored) != (cursor, fingerprint)


def save_cursor_marker(db, sink, cursor, fingerprint):
    db.execute("INSERT INTO event_cursor_markers VALUES (?,?,?) ON CONFLICT(sink) DO UPDATE SET "
               "event_rowid=excluded.event_rowid,fingerprint=excluded.fingerprint",
               (sink, cursor, fingerprint))
