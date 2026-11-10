"""Validate actual C++ exported events against the authoritative server contract.

Runs with stdlib Python only; no Flask or generated fixture dependency. All
catalog context keys, integer endpoints and enum values cross the language
boundary. Runtime server code and server tests remain unchanged.
"""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from common.diagnostics import load_catalog, validate_request


def main():
    cases = []
    accepted = 0
    def add(code, mode, key="-", kind=0, value=0, valid=True):
        nonlocal accepted
        cases.append(f"{code} {mode} {key} {kind} {value} {int(valid)}")
        accepted += valid

    for row in load_catalog()["codes"]:
        if row["code"] >= 7000 or row["journal"] == "counter":
            continue
        mode = int(row["journal"] == "transitions")
        add(row["code"], mode)
        add(row["code"], mode, "__unknown__", valid=False)
        for key, spec in row["context"].items():
            kind = spec["type"]
            values = ([spec["min"], spec["max"]] if kind == "integer"
                      else ["false", "true"] if kind == "boolean" else spec["values"])
            for value in values:
                add(row["code"], mode, key, ["integer", "boolean", "enum"].index(kind), value)
            if kind == "integer":
                for value in (spec["min"] - 1, spec["max"] + 1):
                    if -(1 << 63) <= value < (1 << 63):
                        add(row["code"], mode, key, 0, value, False)
            elif kind == "enum":
                add(row["code"], mode, key, 2, "__invalid__", False)
            add(row["code"], mode, key, 2 if kind == "integer" else 0, 0, False)
    result = subprocess.run([str(Path(sys.argv[1]).resolve()), "--protocol"], input="\n".join(cases) + "\n",
                            text=True, capture_output=True, check=True)
    lines = result.stdout.splitlines()
    assert len(lines) == len(cases)
    events = [json.loads(line) for line in lines if line != "invalid"]
    assert len(events) == accepted
    for event in events:
        validate_request({"schema_version": 1,
                          "checkin": {"boot_id": event["boot_id"], "status_sequence": 1,
                                      "uptime_ms": 0, "active": []},
                          "events": [event]})
    print(f"EventLog protocol: {len(events)} exported catalog/context cases validated; "
          f"{len(cases) - accepted} invalid cases rejected")


if __name__ == "__main__":
    main()
