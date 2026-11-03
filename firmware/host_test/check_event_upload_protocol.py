"""Check every complete production-worker request with the real validator/ingest.

No sockets: Flask's test client and disposable SQLite databases only. Replaying
each request proves real ingest's commit/dedupe acknowledgment matches its IDs.
"""
import json
import sqlite3
from contextlib import closing
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
card_cycle = [line[11:].encode() for line in result.stdout.splitlines() if line.startswith("CARD_CYCLE ")]
assert len(card_cycle) == 4
with tempfile.TemporaryDirectory(prefix="card-cycle-") as temp:
    ing = load_ingest(Path(temp))
    previous = 0
    previous_snapshot = None
    total_events = 0
    for index, raw in enumerate(card_cycle):
        request = parse_request(raw)
        snapshot = request["checkin"]
        if index == 3:  # the return boot drains SD then retained flash with one snapshot
            assert snapshot == previous_snapshot
        else:
            assert snapshot["status_sequence"] > previous
        previous = snapshot["status_sequence"]
        previous_snapshot = snapshot
        assert snapshot["storage_backend"] == ("littlefs" if index == 1 else "sd")
        assert any(item["code"] == 4001 for item in snapshot["active"]) == (index == 1)
        total_events += len(request["events"])
        if index == 3:
            assert all(event["boot_id"] != snapshot["boot_id"] for event in request["events"])
        for replay in range(2):
            response = ing.app.test_client().post("/v1/device-events", data=raw,
                                                  content_type="application/json", headers=HEADERS)
            assert response.status_code == 200, response.get_json()
            assert len(response.get_json()["accepted"]) == len(request["events"])
        with closing(sqlite3.connect(ing.DIAGNOSTICS_STORE.path)) as db:
            row = db.execute("SELECT status_sequence, snapshot_json FROM devices WHERE device_id=?",
                             (HEADERS["X-Device-Id"],)).fetchone()
            assert row[0] == previous
            stored = json.loads(row[1])
            assert stored["storage_backend"] == snapshot["storage_backend"]
            assert stored["active"] == snapshot["active"]
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == total_events
    assert total_events == 8
print("Card-cycle ingest: 3 increasing snapshots, 4 requests, E4001 active/clear, 8 retained events and dedupe passed")
failed_card = [line[12:].encode() for line in result.stdout.splitlines() if line.startswith("FAILED_CARD ")]
assert len(failed_card) == 4
with tempfile.TemporaryDirectory(prefix="failed-card-") as temp:
    ing = load_ingest(Path(temp))
    previous = total_events = 0
    for index, raw in enumerate(failed_card):
        request = parse_request(raw)
        snapshot = request["checkin"]
        assert snapshot["status_sequence"] > previous
        previous = snapshot["status_sequence"]
        assert snapshot["storage_backend"] == ("littlefs" if index in (1, 2) else "sd")
        assert any(item["code"] == 4006 for item in snapshot["active"]) == (index in (1, 2))
        if index == 1:
            assert any(event["code"] == 4006 and event["context"] == {"backend": "sd", "operation": "open"}
                       for event in request["events"])
        total_events += len(request["events"])
        for replay in range(2):
            response = ing.app.test_client().post("/v1/device-events", data=raw,
                                                  content_type="application/json", headers=HEADERS)
            assert response.status_code == 200, response.get_json()
            assert len(response.get_json()["accepted"]) == len(request["events"])
        with closing(sqlite3.connect(ing.DIAGNOSTICS_STORE.path)) as db:
            row = db.execute("SELECT status_sequence, snapshot_json FROM devices WHERE device_id=?",
                             (HEADERS["X-Device-Id"],)).fetchone()
            assert row[0] == previous and json.loads(row[1])["active"] == snapshot["active"]
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == total_events
    assert total_events == 7
print("Failed-card ingest: 4 increasing snapshots, E4006 active/clear, 7 events including retained SD history and dedupe passed")
print(f"Event-upload protocol: all {len(requests)} firmware bodies validated; "
      f"{2 * len(requests)} real local ingest commits/replays passed")
