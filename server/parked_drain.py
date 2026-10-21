"""Read-only, conservative parked battery estimates from diagnostics schema v1.

Run from the server directory: python parked_drain.py /data/diagnostics/events.db
No receipt-time substitution, current conversion, writes, or schema migration.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
import sys

from common.diagnostics import load_catalog, validate_device_id, validate_request


MIN_RESOLUTION_MV = 3  # Reporting floor, not a claim about ADC accuracy.


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


def correct_pairs(pairs, short_minutes=10):
    """Use each device's short parks to estimate boot-minus-off load rebound.

    Spread is the largest absolute deviation from the median, so every observed
    calibration sample contributes to the resolution floor. A single sample has
    zero observed spread; it does not establish repeatability or ADC accuracy.
    Raw columns stay intact; short pairs have no corrected drain estimate.
    """
    if not math.isfinite(short_minutes) or short_minutes <= 0:
        raise ValueError("short-pair cutoff must be finite and positive")
    samples = defaultdict(list)
    for pair in pairs:
        if pair["hours_parked"] * 60 < short_minutes and pair["mv_drop"] is not None:
            samples[pair["device_id"]].append(-pair["mv_drop"])
    calibration = {}
    for device, changes in samples.items():
        offset = median(changes)
        calibration[device] = (len(changes), offset, max(abs(mv - offset) for mv in changes))
    corrected = []
    for pair in pairs:
        count, offset, spread = calibration.get(pair["device_id"], (0, 0, None))
        short = pair["hours_parked"] * 60 < short_minutes
        drop = None if short or pair["mv_drop"] is None else pair["mv_drop"] + offset
        resolution = max(MIN_RESOLUTION_MV, spread or 0)
        status = ("calibration" if short else "unknown" if drop is None else
                  "below resolution" if drop <= resolution else "estimate")
        corrected.append(dict(pair, pair_kind="short" if short else "long",
                              rebound_pairs=count, rebound_offset_mv=offset,
                              rebound_spread_mv=spread,
                              rebound_status="calibrated" if count else "no short voltage pairs; uncorrected",
                              corrected_mv_drop=drop,
                              corrected_mv_per_hour=None if drop is None else drop / pair["hours_parked"],
                              resolution_mv=resolution, drain_status=status))
    return corrected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--short-minutes", type=float, default=10, metavar="MINUTES",
                        help="parks shorter than this calibrate rebound (default: 10 minutes)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.short_minutes) or args.short_minutes <= 0:
        parser.error("--short-minutes must be finite and positive")
    try:
        pairs = correct_pairs(pair_events(read_events(args.database)), args.short_minutes)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "Diagnostics unavailable or invalid; no estimate produced.\n")
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["device_id", "off_utc", "boot_utc", "hours_parked", "mv_drop",
                     "mv_per_hour", "percentage_points_drop", "pair_kind", "rebound_pairs",
                     "rebound_offset_mv", "rebound_spread_mv", "rebound_status",
                     "corrected_mv_drop", "corrected_mv_per_hour", "resolution_mv", "drain_status"])
    for pair in pairs:
        def utc(key):
            return datetime.fromtimestamp(pair[key] / 1000, timezone.utc).isoformat()
        def number(key):
            return "unknown" if pair[key] is None else f"{pair[key]:.3f}"
        writer.writerow([pair["device_id"], utc("off_utc_ms"), utc("boot_utc_ms"),
                         number("hours_parked"), number("mv_drop"), number("mv_per_hour"),
                         number("percentage_points_drop"), pair["pair_kind"], pair["rebound_pairs"],
                         number("rebound_offset_mv"), number("rebound_spread_mv"), pair["rebound_status"],
                         number("corrected_mv_drop"), number("corrected_mv_per_hour"),
                         number("resolution_mv"), pair["drain_status"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
