"""Bounded device-event protocol v1; HTTP/auth/database work belongs to ingest.

parse_request enforces the raw UTF-8 byte limit and JSON syntax. The future
HTTP handler must ALSO cap the stream while reading, even without a length.
Validation exceptions never echo supplied values (context may contain PII).
"""

import json
import re

from .catalog import MAX_INTEGER, load_catalog, unique_object, validate_catalog

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 32 * 1024
MAX_EVENTS = 64
MAX_ACTIVE = 64
MAX_RECORD_BYTES = 512
MAX_EVENT_BYTES = MAX_RECORD_BYTES - 10  # TAB + eight CRC hex digits + LF
BOOT_PATTERN = r"[0-9a-f]{32}"
EPISODE_PATTERN = BOOT_PATTERN + r":[1-9][0-9]{0,18}"
TIME_SOURCES = ("unknown", "rtc", "gps", "sntp")


class ProtocolError(ValueError):
    """Invalid input. Messages describe fields, never their supplied values."""


def _require(condition, message):
    if not condition:
        raise ProtocolError(message)


def _integer(value, lo=0, hi=MAX_INTEGER):
    return type(value) is int and lo <= value <= hi


def _token(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_device_id(device_id):
    """Validate X-Device-Id separately from the body; never infer identity from it."""
    _require(_token(device_id, r"[a-z0-9][a-z0-9_-]{0,31}"), "invalid device_id")
    return device_id


def _object(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= set(value)
             and set(value) <= set(required) | set(optional), "invalid object fields")


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError("invalid JSON value") from exc


# 7xxx codes are raised by the server monitor (for example 7001 DEVICE_SILENT);
# a device reporting one would forge a server-side incident.
DEVICE_CODE_MAX = 6999


def _code(code):
    _require(_integer(code, 1000, DEVICE_CODE_MAX), "invalid device code")


def _episode(value, boot_id, sequence=None):
    _require(_token(value, EPISODE_PATTERN), "invalid episode_id")
    boot, number = value.split(":")
    _require(boot == boot_id and _integer(int(number), 1), "episode must belong to boot")
    if sequence is not None:
        _require(int(number) <= sequence, "episode cannot follow event")


def _context(context, definition):
    _require(isinstance(context, dict), "context must be an object")
    # Future codes survive rolling upgrades, but unrecognized free text must not
    # become a back door around the PII allowlist. Future firmware sends {} to
    # older catalogs (or upgrades the server first).
    schema = definition["context"] if definition else {}
    _require(set(context) <= set(schema), "unrecognized context key")
    for key, value in context.items():
        spec = schema[key]
        kind = spec["type"]
        valid = (kind == "integer" and _integer(value, spec["min"], spec["max"]))
        valid |= kind == "boolean" and type(value) is bool
        valid |= kind == "enum" and isinstance(value, str) and value in spec["values"]
        _require(valid, "invalid context value")


def _event(event, definitions):
    _object(event, {"schema_version", "catalog_version", "firmware_version", "boot_id",
                    "boot_counter", "sequence", "code", "transition", "episode_id",
                    "uptime_ms", "utc_ms", "time_source", "sync_age_ms", "context"})
    _require(type(event["schema_version"]) is int and event["schema_version"] == 1,
             "unsupported event schema_version")
    _require(_integer(event["catalog_version"], 1, 65535), "invalid catalog_version")
    _require(_token(event["firmware_version"], r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}"),
             "invalid firmware_version")
    _require(_token(event["boot_id"], BOOT_PATTERN), "invalid boot_id")
    for field in ("boot_counter", "uptime_ms"):
        _require(_integer(event[field]), "invalid " + field)
    _require(_integer(event["sequence"], 1), "invalid sequence")
    _code(event["code"])
    transition = event["transition"]
    _require(transition in ("raise", "repeat", "clear", "notice"), "invalid transition")
    if transition == "notice":
        _require(event["episode_id"] is None, "notice has no episode")
    else:
        _episode(event["episode_id"], event["boot_id"], event["sequence"])
        if transition == "raise":
            _require(event["episode_id"] == event["boot_id"] + ":" + str(event["sequence"]),
                     "raise must start its own episode")
    definition = definitions.get(event["code"])
    if definition:
        mode = definition["journal"]
        _require(mode != "counter", "counter conditions are check-in counters only")
        _require((mode == "per_occurrence") == (transition == "notice"),
                 "transition does not match journal policy")
    source = event["time_source"]
    _require(source in TIME_SOURCES, "invalid time_source")
    utc = event["utc_ms"]
    # 1970 through 9999; confidence/clock plausibility is distinct from syntax.
    _require(utc is None or _integer(utc, 0, 253402300799999), "invalid utc_ms")
    age = event["sync_age_ms"]
    _require(age is None or _integer(age), "invalid sync_age_ms")
    _require((source == "unknown") == (utc is None), "inconsistent UTC source")
    _require(source != "unknown" or age is None, "unknown time has no sync age")
    _context(event["context"], definition)
    _require(len(_encoded(event)) <= MAX_EVENT_BYTES, "event exceeds journal frame limit")


def _checkin(checkin, definitions):
    _object(checkin, {"boot_id", "status_sequence", "uptime_ms", "active"},
            {"firmware_version", "boot_counter", "storage_backend", "pending_events",
             "dropped_events", "counters", "observed"})
    _require(_token(checkin["boot_id"], BOOT_PATTERN), "invalid check-in boot_id")
    _require(_integer(checkin["status_sequence"], 1), "invalid status_sequence")
    for field in ("uptime_ms", "boot_counter", "pending_events", "dropped_events"):
        if field in checkin:
            _require(_integer(checkin[field]), "invalid check-in " + field)
    if "firmware_version" in checkin:
        _require(_token(checkin["firmware_version"], r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}"),
                 "invalid check-in firmware_version")
    if "storage_backend" in checkin:
        _require(checkin["storage_backend"] in ("sd", "littlefs", "ram", "none"),
                 "invalid storage_backend")
    active = checkin["active"]
    _require(isinstance(active, list) and len(active) <= MAX_ACTIVE, "invalid active list")
    codes = set()
    for fault in active:
        _object(fault, {"code", "episode_id"})
        _code(fault["code"])
        _require(fault["code"] not in codes, "duplicate active code")
        codes.add(fault["code"])
        definition = definitions.get(fault["code"])
        _require(not definition or definition["journal"] == "transitions",
                 "only conditions can be active")
        _episode(fault["episode_id"], checkin["boot_id"])
    if "observed" in checkin:
        observed = checkin["observed"]
        _require(isinstance(observed, list) and len(observed) <= MAX_ACTIVE,
                 "invalid observed list")
        seen = set()
        for code in observed:
            _code(code)
            _require(code not in seen, "duplicate observed code")
            seen.add(code)
            definition = definitions.get(code)
            _require(not definition or definition["journal"] == "transitions",
                     "only conditions can be observed")
        _require(codes <= seen, "active codes must be observed")
    counters = checkin.get("counters", {})
    _require(isinstance(counters, dict) and len(counters) <= 16, "invalid counters")
    counter_codes = {str(code) for code, row in definitions.items() if row["journal"] == "counter"}
    for code, count in counters.items():
        _require(code in counter_codes and _integer(count), "invalid check-in counter")


def validate_request(body, *, catalog=None):
    """Validate a decoded body without coercion/mutation; return it on success.

    Does not authenticate a sender or validate storage acknowledgments. Calls
    parse_request at the HTTP boundary to also bound whitespace/raw encoding.
    """
    _object(body, {"schema_version", "checkin", "events"})
    _require(type(body["schema_version"]) is int and body["schema_version"] == SCHEMA_VERSION,
             "unsupported schema_version")
    selected = load_catalog() if catalog is None else validate_catalog(catalog)
    definitions = {row["code"]: row for row in selected["codes"]}
    _checkin(body["checkin"], definitions)
    events = body["events"]
    _require(isinstance(events, list) and len(events) <= MAX_EVENTS, "invalid events list")
    ids = set()
    for event in events:
        _event(event, definitions)
        identity = (event["boot_id"], event["sequence"])
        _require(identity not in ids, "duplicate event id in request")
        ids.add(identity)
    _require(len(_encoded(body)) <= MAX_REQUEST_BYTES, "request exceeds byte limit")
    return body


def parse_request(raw, *, catalog=None):
    """Validate bounded raw UTF-8 JSON; reject duplicates, NaN, BOM, deep nesting."""
    _require(isinstance(raw, bytes), "request must be bytes")
    _require(len(raw) <= MAX_REQUEST_BYTES, "request exceeds byte limit")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda _: _require(False, "non-finite JSON number"))
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("invalid request JSON") from exc
    return validate_request(body, catalog=catalog)
