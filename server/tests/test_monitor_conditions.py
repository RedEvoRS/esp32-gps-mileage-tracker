"""The same current evidence must mean the same state in every monitor output."""

import json
import re
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from test_monitor import BOOT, monitor, store_module
from common.diagnostics import load_catalog


class ConditionParityTests(unittest.TestCase):
    def test_all_outputs_share_snapshot_contact_and_unknown_states(self):
        now = 1800000000.0
        devices, expected = [], {}

        def device(name, *, active=True, observed=True, age=0, contact_age=0, snapshot=True, contact=True):
            row = {"device_id": name, "contact": now - contact_age if contact else None,
                   "last_contact_at": datetime.fromtimestamp(now - contact_age, timezone.utc).isoformat(),
                   "snapshot_at": now - age, "conditions": {4002: "clear"},
                   "snapshot": {"status_sequence": 2, "active": [{"code": 4002, "episode_id": BOOT + ":1"}]
                                if active else []}}
            if observed:
                row["snapshot"]["observed"] = [4002]
            if not snapshot:
                row["snapshot"] = None
            devices.append(row)
            expected[(name, 4002)] = ("unknown" if not contact or not snapshot or age > 86400 or age < 0
                                      or not (active or observed) else "active" if active else "clear")
            expected[(name, 7001)] = "unknown" if not contact else "active" if contact_age >= 7 * 86400 else "clear"

        device("active")  # Stored clear must not override an accepted active snapshot.
        device("clear", active=False)
        device("unknown", active=False, observed=False)
        device("boundary", age=86400)
        device("stale", age=86401)
        device("future", age=-1)
        device("missing", snapshot=False)
        device("silent", contact_age=7 * 86400)  # Fresh snapshot, old contact.
        device("gpx-only", age=86401)  # Recent contact does not refresh health.
        device("bad-contact", contact=False)
        device("legacy-active", observed=False)  # Older protocol omits observed.
        device("silence-claim-active")
        devices[-1]["snapshot"]["active"].append({"code": 7001, "episode_id": BOOT + ":2"})
        devices[-1]["snapshot"]["observed"].append(7001)
        device("silence-claim-clear", contact_age=7 * 86400)
        devices[-1]["snapshot"]["observed"].append(7001)
        # A device-supplied E7001 entry cannot override server contact age.
        expected[("removed", 4002)] = "unknown"

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            catalog = load_catalog()
            store = store_module.DiagnosticsStore(root / "data", catalog)
            store.accept("active", {"schema_version": 1, "checkin": {
                "boot_id": BOOT, "status_sequence": 1, "uptime_ms": 0, "active": []}, "events": []})
            gelf_payloads, loki_payloads = [], []

            def send_gelf(payloads):
                gelf_payloads.extend(json.loads(payload) for payload in payloads)
                return len(payloads)

            config = monitor.Config(enabled=True, min_severity="info", hold_seconds=0,
                graylog=monitor.gelf.GelfConfig(enabled=True, url="http://example.invalid/gelf"),
                loki=monitor.loki_module.LokiConfig(enabled=True, url="http://example.invalid/loki/api/v1/push"),
                metrics=monitor.prometheus.MetricsConfig(True, "m" * 32))
            worker = monitor.Monitor(config, store.path, root / "monitor.db", clock=lambda: now,
                                     gelf_sender=send_gelf, loki_sender=loki_payloads.append, catalog=catalog)
            # Prior active evidence lets all sinks show unknown/clear changes;
            # ntfy still requires an ordered clear and newer contact to resolve.
            with closing(worker._connect()) as db, db:
                for name, code in expected:
                    db.execute("INSERT INTO episodes(device,code,episode,state,revision,contact) "
                               "VALUES (?,?,?,'active',1,?)", (name, code, "old", now - 9 * 86400))
                    for sink in ("gelf", "loki"):
                        db.execute(f"INSERT INTO {sink}_conditions VALUES (?,?,'active','old')", (name, code))
            with mock.patch.object(monitor, "read_devices", return_value=devices):
                self.assertTrue(worker.tick())
            with closing(worker._connect()) as db:
                ntfy = {}
                for name, code, state in db.execute("SELECT device,code,state FROM episodes"):
                    key = name, code
                    if state == "active" or ntfy.get(key) != "active":
                        ntfy[key] = state
                for sink in ("gelf", "loki"):
                    states = {(name, code): state for name, code, state in
                              db.execute(f"SELECT device,code,state FROM {sink}_conditions")}
                    self.assertEqual(states, expected, sink)
            self.assertEqual(ntfy, expected)
            body = worker.metrics_snapshot.render(now)
            metrics = {(name, int(code)): state for name, code, state in re.findall(
                r'mileage_tracker_condition_state\{device_id="([^"]+)",code="(\d+)"[^\n]*state="([^"]+)"\} 1', body)}
            self.assertEqual(metrics, expected)
            self.assertTrue(gelf_payloads)
            self.assertTrue(loki_payloads)

    def test_output_filters_do_not_change_condition_evidence(self):
        catalog = load_catalog()
        codes = {row["code"]: row for row in catalog["codes"]}
        devices = [{"device_id": "tracker-1", "contact": 100, "snapshot_at": 100,
                    "snapshot": {"status_sequence": 1, "active": [
                        {"code": code, "episode_id": BOOT + ":1"} for code in (6999, 3002, 4002)]}}]
        states = monitor.shared.condition_states(devices, {}, 100, 604800)
        changes = monitor.shared.desired_conditions(devices, {}, 100, 604800)
        worker = monitor.Monitor(monitor.Config(), catalog=catalog)
        self.assertFalse(codes[3002]["notify"])
        for code in (6999, 3002, 4002):
            self.assertEqual(states[("tracker-1", code)][0], "active")
            self.assertEqual(changes[("tracker-1", code)][0], "active")
            self.assertEqual(worker.eligible(code), code == 4002)
            payload = json.loads(monitor.gelf.condition_message(codes, "tracker-1", code, "active", None, 100))
            self.assertEqual(payload["_state"], "active")
            line = monitor.loki_module.condition_log(codes, "tracker-1", code, "active", 100)
            if code in codes:
                self.assertEqual(json.loads(line[2])["state"], "active")
            else:
                self.assertIsNone(line)  # Loki condition lines remain catalog-bounded.
        metrics = monitor.prometheus.MetricsSnapshot(codes, 604800)
        metrics.publish(devices, (), 100)
        body = metrics.render(100)
        self.assertNotIn('code="6999"', body)
        for code in (3002, 4002):
            self.assertRegex(body, rf'code="{code}"[^\n]*state="active"')


if __name__ == "__main__":
    unittest.main()
