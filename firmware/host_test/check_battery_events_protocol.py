"""Validate actual battery-event exports against the installed server catalog."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from common.diagnostics import validate_request

result = subprocess.run([str(Path(sys.argv[1]).resolve()), "--export"],
                        capture_output=True, text=True, check=True)
events = [json.loads(line[6:]) for line in result.stdout.splitlines() if line.startswith("EVENT ")]
assert {e["code"] for e in events} == {1000, 2009}
for event in events:
    validate_request({"schema_version": 1, "checkin": {
        "boot_id": event["boot_id"], "status_sequence": 1,
        "uptime_ms": event["uptime_ms"], "active": []}, "events": [event]})
print(f"Battery-event protocol: {len(events)} actual records validated")
