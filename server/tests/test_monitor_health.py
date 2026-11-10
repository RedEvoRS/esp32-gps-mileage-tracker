"""Monitor progress, safe unexpected-exception logs and the actual Compose probe."""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_monitor import monitor


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def worker(self, enabled=False):
        return monitor.Monitor(monitor.Config(enabled=enabled), self.root / "events.db",
                               self.root / "state/monitor.db")

    def test_disabled_tick_touches_state_volume_without_database_reads(self):
        worker = self.worker()
        with mock.patch.object(monitor, "read_devices", side_effect=AssertionError("no DB reads")), \
                mock.patch.object(worker, "_connect", side_effect=AssertionError("no state DB")):
            self.assertTrue(worker.tick())
        self.assertEqual(worker.heartbeat_path, self.root / "state/heartbeat")
        self.assertEqual(worker.heartbeat_path.read_bytes(), b"")
        self.assertFalse(worker.state_path.exists())
        self.assertFalse(worker.events_path.exists())
        os.utime(worker.heartbeat_path, (1, 1))
        self.assertTrue(worker.tick())
        self.assertGreater(worker.heartbeat_path.stat().st_mtime, 1)

    def test_missing_events_database_is_a_completed_tick(self):
        worker = self.worker(enabled=True)
        self.assertTrue(worker.tick())
        self.assertTrue(worker.heartbeat_path.is_file())
        self.assertFalse(worker.events_path.exists())

    def test_unreadable_events_does_not_refresh_heartbeat_and_recovery_does(self):
        worker = self.worker(enabled=True)
        worker.tick()
        os.utime(worker.heartbeat_path, (1, 1))
        with mock.patch.object(monitor, "read_devices", side_effect=PermissionError("private path")), \
                self.assertLogs("mileage-monitor", "WARNING"):
            self.assertFalse(worker.tick())
        self.assertEqual(worker.heartbeat_path.stat().st_mtime, 1)
        self.assertTrue(worker.tick())
        self.assertGreater(worker.heartbeat_path.stat().st_mtime, 1)

    def test_unexpected_exception_does_not_create_or_refresh_heartbeat(self):
        worker = self.worker()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    worker.heartbeat_path.parent.mkdir(parents=True)
                    worker.heartbeat_path.touch()
                    os.utime(worker.heartbeat_path, (1, 1))
                with mock.patch.object(worker, "_tick", side_effect=RuntimeError("private endpoint")), \
                        self.assertRaises(RuntimeError):
                    worker.tick()
                if existing:
                    self.assertEqual(worker.heartbeat_path.stat().st_mtime, 1)
                else:
                    self.assertFalse(worker.heartbeat_path.exists())


class MainLoopTests(unittest.TestCase):
    def run_loop(self, failures):
        worker = mock.Mock()
        worker.start_metrics.return_value = None
        worker.tick.side_effect = failures
        stop = mock.Mock()
        stop.is_set.side_effect = [False] * len(failures) + [True]
        with mock.patch.object(monitor.Config, "from_env", return_value=monitor.Config()), \
                mock.patch.object(monitor, "Monitor", return_value=worker), \
                mock.patch.object(monitor.threading, "Event", return_value=stop), \
                mock.patch.object(monitor.logging, "basicConfig"), \
                mock.patch.object(monitor.signal, "signal"), self.assertLogs("mileage-monitor", "INFO") as logs:
            monitor.main()
        return worker, stop, "\n".join(logs.output)

    def test_unexpected_exception_logs_safe_frames_and_waits_before_next_tick(self):
        error = RuntimeError("Bearer private-token https://user:private-password@example.invalid/push")
        error.__cause__ = ValueError("private chained exception")
        worker, stop, text = self.run_loop([error, True])
        self.assertEqual(worker.tick.call_count, 2)
        self.assertEqual(stop.wait.call_args_list, [mock.call(60), mock.call(60)])
        self.assertIn("Unexpected monitor tick failure (RuntimeError)", text)
        self.assertIn("Traceback", text)
        self.assertIn('File "monitor.py", line', text)
        self.assertIn("in main", text)
        for secret in ("private-token", "private-password", "example.invalid", "Bearer", "private chained",
                       str(Path(__file__).parent), 'worker.tick()'):
            self.assertNotIn(secret, text)

    def test_keyboard_interrupt_and_system_exit_still_stop_the_loop(self):
        for failure in (KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(exception=type(failure).__name__), self.assertRaises(type(failure)):
                self.run_loop([failure])


class ComposeHeartbeatTests(unittest.TestCase):
    def test_actual_probe_missing_fresh_boundary_stale_and_future(self):
        compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
        service = compose.split("  mileage-monitor:\n", 1)[1].split("  # Opt in separately:", 1)[0]
        check = service.split("    healthcheck:\n", 1)[1]
        test = ast.literal_eval(check.split("test: ", 1)[1].splitlines()[0])
        self.assertEqual(test[:3], ["CMD", "python", "-c"])
        for setting in ("interval: 30s", "timeout: 5s", "retries: 2", "start_period: 90s"):
            self.assertIn(setting, check)
        self.assertIn("monitor-state:/state", service)
        with tempfile.TemporaryDirectory() as root:
            heartbeat = Path(root) / "heartbeat"
            script = test[3].replace("/state/heartbeat", heartbeat.as_posix())
            for age, expected in ((None, 1), (0, 0), (60, 0), (180, 0), (181, 1), (-1, 1)):
                if age is not None:
                    heartbeat.touch()
                    os.utime(heartbeat, (1000 - age, 1000 - age))
                with self.subTest(age=age), mock.patch("time.time", return_value=1000), \
                        self.assertRaises(SystemExit) as caught:
                    exec(script, {})
                self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
