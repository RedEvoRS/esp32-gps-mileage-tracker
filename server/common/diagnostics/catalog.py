"""Load and validate the single source of diagnostic definitions (stdlib only)."""

import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalog.json")
SEVERITIES = ("info", "warning", "error", "critical")
JOURNAL_MODES = ("per_occurrence", "transitions", "counter")
SUBSYSTEMS = {1: "runtime", 2: "power", 3: "gps_time", 4: "storage",
              5: "network", 6: "display", 7: "monitor"}
MAX_CONTEXT_KEYS = 8
MAX_INTEGER = (1 << 63) - 1  # SQLite INTEGER and cross-language counters


class CatalogError(ValueError):
    """Invalid local catalog; a packaging/development error, not client input."""


def _require(condition, message):
    if not condition:
        raise CatalogError(message)


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(32 <= ord(c) <= 126 for c in value))


def _schema(spec):
    _require(isinstance(spec, dict), "context specification must be an object")
    kind = spec.get("type")
    if kind == "integer":
        _require(set(spec) == {"type", "min", "max"}, "integer context needs bounds")
        lo, hi = spec["min"], spec["max"]
        _require(type(lo) is int and type(hi) is int
                 and -MAX_INTEGER <= lo <= hi <= MAX_INTEGER, "invalid context bounds")
    elif kind == "enum":
        _require(set(spec) == {"type", "values"}, "enum context needs values")
        values = spec["values"]
        _require(isinstance(values, list) and 1 <= len(values) <= 16
                 and all(_text(v, 32) for v in values), "invalid context enum")
        _require(len(set(values)) == len(values), "duplicate enum value")
    else:
        _require(kind == "boolean" and set(spec) == {"type"}, "unknown context type")


def validate_catalog(catalog):
    """Return catalog unchanged, or raise CatalogError; never silently coerce."""
    _require(isinstance(catalog, dict) and set(catalog) == {"catalog_version", "codes"},
             "invalid catalog fields")
    _require(type(catalog["catalog_version"]) is int
             and 1 <= catalog["catalog_version"] <= 65535, "invalid catalog_version")
    rows = catalog["codes"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= 1000, "invalid code list")
    codes, symbols = set(), set()
    fields = {"code", "symbol", "severity", "oled", "title", "explanation",
              "recovery", "context", "notify", "journal"}
    for row in rows:
        _require(isinstance(row, dict) and set(row) == fields, "invalid code fields")
        code, symbol = row["code"], row["symbol"]
        _require(type(code) is int and 1000 <= code <= 7999
                 and code // 1000 in SUBSYSTEMS, "invalid subsystem range")
        _require(isinstance(symbol, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", symbol),
                 "invalid symbol")
        _require(code not in codes and symbol not in symbols, "duplicate code or symbol")
        codes.add(code)
        symbols.add(symbol)
        _require(row["severity"] in SEVERITIES, "invalid severity")
        _require(row["journal"] in JOURNAL_MODES, "invalid journal mode")
        _require(type(row["notify"]) is bool, "notify must be boolean")
        lines = row["oled"]
        _require(isinstance(lines, list) and 1 <= len(lines) <= 3
                 and all(_text(line, 21) for line in lines), "OLED text does not fit")
        for key, limit in (("title", 80), ("explanation", 600), ("recovery", 600)):
            _require(_text(row[key], limit), "invalid " + key)
        context = row["context"]
        _require(isinstance(context, dict) and len(context) <= MAX_CONTEXT_KEYS,
                 "invalid context schema")
        for key, spec in context.items():
            _require(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key),
                     "invalid context key")
            _schema(spec)
    return catalog


def unique_object(pairs):
    """JSON hook: duplicate keys must not be silently replaced."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_catalog(path=None):
    """Load a fresh copy; callers cannot mutate another service's cached schema."""
    try:
        value = json.loads(Path(path or CATALOG_PATH).read_text(encoding="utf-8"),
                           object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        raise CatalogError("cannot load diagnostics catalog") from exc
    return validate_catalog(value)
