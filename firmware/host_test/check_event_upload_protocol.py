"""Check every complete production-worker request with the real validator/ingest.

No sockets: Flask's test client and disposable SQLite databases only. Replaying
each request proves real ingest's commit/dedupe acknowledgment matches its IDs.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
from common.diagnostics import parse_request
from server.tests.test_ingest_diagnostics import HEADERS, load_ingest

result = subprocess.run([str(Path(sys.argv[1]).resolve()), "--export"],
                        capture_output=True, text=True, check=True)
requests = [line[8:].encode() for line in result.stdout.splitlines() if line.startswith("REQUEST ")]
assert requests
counts = []
mixed_boots = counters = active = observed = False
with tempfile.TemporaryDirectory(prefix="event-upload-") as temp:
    for index, raw in enumerate(requests):
        request = parse_request(raw)
        counts.append(len(request["events"]))
        assert len(raw) <= 32768 and len(request["events"]) <= 64
        snapshot = request["checkin"]
        assert snapshot["firmware_version"]
        mixed_boots |= any(event["boot_id"] != snapshot["boot_id"] for event in request["events"])
        counters |= snapshot.get("counters", {}).get("5001") == 5
        active |= bool(snapshot["active"])
        observed |= bool(snapshot.get("observed"))
        ing = load_ingest(Path(temp) / str(index))
        expected = [{"boot_id": event["boot_id"], "sequence": event["sequence"]}
                    for event in request["events"]]
        for attempt in range(2):
            response = ing.app.test_client().post("/v1/device-events", data=raw,
                                                  content_type="application/json", headers=HEADERS)
            assert response.status_code == 200, (index, response.status_code, response.get_json())
            assert response.get_json() == {"accepted": expected}
assert max(counts) == 64 and min(counts) == 0
assert mixed_boots and counters and active and observed
print(f"Event-upload protocol: all {len(requests)} firmware bodies validated; "
      f"{2 * len(requests)} real local ingest commits/replays passed")
