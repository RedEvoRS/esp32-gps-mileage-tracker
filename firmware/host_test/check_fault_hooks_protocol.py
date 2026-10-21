"""Validate actual runtime-hook exports, including measured context and policy."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from common.diagnostics import validate_request

result = subprocess.run([str(Path(sys.argv[1]).resolve()), "--export"],
                        capture_output=True, text=True, check=True)
events = [json.loads(line[6:]) for line in result.stdout.splitlines() if line.startswith("EVENT ")]
conditions = {1007, *range(2002, 2007), 2008, 3001, 3002, 3005, 3006, 3009,
              4001, 4004, 4005, 4006, *range(5002, 5010)}
if "runtime debug=1" in result.stdout:
    conditions.add(6001)
for code in conditions:
    rows = [e for e in events if e["code"] == code]
    assert {e["transition"] for e in rows} >= {"raise", "repeat", "clear"}, code
    for event in rows:
        if code in {1007, 2002, 2003, 2004, 2005, 3001, 3002, 3005, 4004, 4005, 4006, *range(5002, 5010)}:
            assert event["context"], code
        if 5003 <= code <= 5009:
            assert event["context"]["target"] == "trip"
for code in (3003, 3004, 4007, 4010):
    rows = [e for e in events if e["code"] == code]
    assert rows and all(e["transition"] == "notice" and e["episode_id"] is None for e in rows), code
assert not any(e["code"] == 5001 for e in events)
for event in events:
    validate_request({"schema_version": 1,
                      "checkin": {"boot_id": event["boot_id"], "status_sequence": 1,
                                  "uptime_ms": event["uptime_ms"], "active": []},
                      "events": [event]})
print(f"Runtime-hook protocol: {len(events)} actual records validated")
