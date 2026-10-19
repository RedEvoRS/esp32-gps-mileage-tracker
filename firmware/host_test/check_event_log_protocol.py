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
    for row in load_catalog()["codes"]:
        if row["code"] >= 7000 or row["journal"] == "counter":
            continue
        mode = int(row["journal"] == "transitions")
        cases.append(f"{row['code']} {mode} - 0 0")
        for key, spec in row["context"].items():
            kind = spec["type"]
            values = ([spec["min"], spec["max"]] if kind == "integer"
                      else ["false", "true"] if kind == "boolean" else spec["values"])
            for value in values:
                cases.append(f"{row['code']} {mode} {key} "
                             f"{['integer', 'boolean', 'enum'].index(kind)} {value}")
    result = subprocess.run([str(Path(sys.argv[1]).resolve()), "--protocol"], input="\n".join(cases) + "\n",
                            text=True, capture_output=True, check=True)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == len(cases)
    for event in events:
        validate_request({"schema_version": 1,
                          "checkin": {"boot_id": event["boot_id"], "status_sequence": 1,
                                      "uptime_ms": 0, "active": []},
                          "events": [event]})
    print(f"EventLog protocol: {len(events)} exported catalog/context cases validated")


if __name__ == "__main__":
    main()
