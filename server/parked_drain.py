"""Read-only, conservative parked battery estimates from diagnostics schema v1.

Run from the server directory: python parked_drain.py /data/diagnostics/events.db
No receipt-time substitution, current conversion, writes, or schema migration.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from common.diagnostics import load_catalog, validate_device_id, validate_request


def read_events(path):
    catalog = load_catalog()
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("SELECT version FROM schema_version WHERE singleton=1").fetchall() != [(1,)]:
            raise ValueError("unsupported diagnostics schema")
        rows = db.execute("SELECT device_id, canonical_json FROM events").fetchall()
        events = []
        for device, raw in rows:
            validate_device_id(device)
            event = json.loads(raw)
            validate_request({"schema_version": 1, "checkin": {
                "boot_id": event["boot_id"], "status_sequence": 1,
                "uptime_ms": event["uptime_ms"], "active": []}, "events": [event]}, catalog=catalog)
            events.append((device, event))
        return events
    finally:
        db.close()


def pair_events(events):
    """Pair adjacent UTC-ordered BOOTs, using counters only as a gap veto.

    Require unique positive consecutive counters, a power-on next boot and the
    last event of the previous boot to be E2009. Missing/ambiguous history is
    omitted. This cannot prove actual rail-off or rule out lost journal records.
    """
    devices = defaultdict(lambda: defaultdict(list))
    for device, event in events:
        devices[device][event["boot_id"]].append(event)
    pairs = []
    for device, histories in sorted(devices.items()):
        boots = [e for rows in histories.values() for e in rows if e["code"] == 1000]
        counters = Counter(e["boot_counter"] for e in boots)
        known = sorted((e for e in boots if e["utc_ms"] is not None), key=lambda e: e["utc_ms"])
        for previous, boot in zip(known, known[1:]):
            counter = previous["boot_counter"]
            if (counter <= 0 or boot["boot_counter"] != counter + 1
                    or counters[counter] != 1 or counters[counter + 1] != 1
                    or previous["boot_id"] == boot["boot_id"]
                    or boot["context"].get("reset_reason") != "power_on"):
                continue
            rows = histories[previous["boot_id"]]
            off = max(rows, key=lambda e: e["sequence"])
            if (off["code"] != 2009 or off["boot_counter"] != counter or off["utc_ms"] is None
                    or not previous["utc_ms"] <= off["utc_ms"] < boot["utc_ms"]):
                continue
            hours = (boot["utc_ms"] - off["utc_ms"]) / 3600000
            before, after = off["context"], boot["context"]
            mv = (before["battery_mv"] - after["battery_mv"]
                  if "battery_mv" in before and "battery_mv" in after else None)
            pct = (before["battery_pct"] - after["battery_pct"]
                   if "battery_pct" in before and "battery_pct" in after else None)
            pairs.append(dict(device_id=device, off_utc_ms=off["utc_ms"], boot_utc_ms=boot["utc_ms"],
                              hours_parked=hours, mv_drop=mv,
                              mv_per_hour=None if mv is None else mv / hours,
                              percentage_points_drop=pct))
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    try:
        pairs = pair_events(read_events(args.database))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "Diagnostics unavailable or invalid; no estimate produced.\n")
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["device_id", "off_utc", "boot_utc", "hours_parked", "mv_drop",
                     "mv_per_hour", "percentage_points_drop"])
    for pair in pairs:
        def utc(key):
            return datetime.fromtimestamp(pair[key] / 1000, timezone.utc).isoformat()
        def number(key):
            return "unknown" if pair[key] is None else f"{pair[key]:.3f}"
        writer.writerow([pair["device_id"], utc("off_utc_ms"), utc("boot_utc_ms"),
                         number("hours_parked"), number("mv_drop"), number("mv_per_hour"),
                         number("percentage_points_drop")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
