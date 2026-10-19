"""Exercise backup.sh and restore.sh against a fake `docker compose`.

The fake runs the scripts' Python snippets with the local interpreter,
mapping the container's /db to a temp directory, so the real SQLite
backup, check and restore code runs. The off-host REMOTE is a local
directory. Needs bash, rsync and GNU coreutils, so it skips on Windows.
"""

import os
import hashlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
BACKUP = SERVER / "backup"

FAKE_COMPOSE = textwrap.dedent("""\
    import json, os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_COMPOSE_LOG"], "a") as log:
        log.write(" ".join(a for a in args if "\\n" not in a) + "\\n")
    if args[:2] == ["--profile", "monitor"]:
        args = args[2:]
    state = os.environ["FAKE_STATE_DIR"]
    if args[0] == "config":
        assert args == ["config", "--format", "json"], args
        if os.environ.get("FAKE_CONFIG_ERROR") == "yes":
            print("private-config-sentinel", file=sys.stderr)
            sys.exit(1)
        print(os.environ.get("FAKE_CONFIG_RESULT", json.dumps({
            "name": os.environ.get("FAKE_PROJECT", "test"),
            "services": {"example": {"environment": {"TOKEN": "private-config-sentinel"}}}})))
    elif args[:2] == ["volume", "ls"]:
        assert args == ["volume", "ls", "--filter",
                        "label=com.docker.compose.project=" + os.environ.get("FAKE_PROJECT", "test"),
                        "--filter", "label=com.docker.compose.volume=monitor-state",
                        "--format", "{{.Name}}"], args
        if os.environ.get("FAKE_VOLUME_ERROR") == "yes":
            print("private-volume-sentinel", file=sys.stderr)
            sys.exit(1)
        if os.environ.get("FAKE_MONITOR") == "yes":
            print(os.environ.get("FAKE_VOLUME_RESULT", "custom-monitor-volume"))
    elif args[0] == "ps":
        if os.environ.get("FAKE_MONITOR_RUNNING", "yes") == "yes":
            print("monitor-container")
    elif args[0] == "stop":
        for service in args[1:]:
            open(state + "/" + service, "w").close()
    if args[0] == "run":
        service = args[args.index("python") - 1]
        args = args[args.index("python"):]
        if len(args) > 4:  # A restore must run only after its writer is stopped.
            writer = {"annotations": "mileage-viewer", "events": "mileage-ingest",
                      "monitor": "mileage-monitor"}[args[3]]
            if writer != "mileage-monitor" or os.environ.get("FAKE_MONITOR") == "yes":
                assert os.path.exists(state + "/" + writer), writer + " is running"
        mapped = []
        for arg in args:
            for prefix, key in (("/db/", "FAKE_DB_DIR"), ("/data/", "FAKE_DATA_DIR"),
                                ("/state/", "FAKE_MONITOR_DIR")):
                if arg.startswith(prefix):
                    if prefix == "/data/" and len(args) > 4:
                        assert service == "mileage-ingest", "viewer data mount is read-only"
                    arg = os.environ[key] + "/" + arg[len(prefix):]
                    break
            mapped.append(arg)
        args = mapped
        sys.exit(subprocess.call([sys.executable] + args[1:]))
    """)

SCHEMA = """
CREATE TABLE annotations (
    trip_key TEXT PRIMARY KEY, category TEXT, purpose_note TEXT,
    updated_at TEXT, destination TEXT NOT NULL DEFAULT '');
CREATE TABLE login_attempts (
    ip TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0, locked_until TEXT);
"""


def tools_missing():
    if os.name == "nt":
        return "needs a POSIX shell"
    for tool in ("bash", "rsync", "flock", "sha256sum"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    return None


@unittest.skipIf(tools_missing(), tools_missing())
class BackupScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.compose_dir = self.tmp / "mileage-tracker"
        self.data = self.compose_dir / "data"
        self.db_dir = self.tmp / "volume"
        self.monitor_dir = self.tmp / "monitor-volume"
        self.state_dir = self.tmp / "service-state"
        self.backups = self.tmp / "backups"
        self.remote = self.tmp / "nas"
        for d in (self.data / "2026-10", self.db_dir, self.remote,
                  self.monitor_dir, self.state_dir):
            d.mkdir(parents=True)
        self.add_trip("2026-10/2026-10-01T15-00-00", "first")
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO annotations VALUES (?, ?, ?, ?, ?)",
                         ("2026-10/2026-10-01T15-00-00", "business",
                          "client visit", "2026-10-01T23:00:00Z", "Acme"))
        conn.close()

        fake = self.tmp / "fake_compose.py"
        fake.write_text(FAKE_COMPOSE)
        self.compose_log = self.tmp / "compose.log"
        conf = self.tmp / "backup.conf"
        conf.write_text(textwrap.dedent(f"""\
            REMOTE={self.remote}
            BACKUP_DIR={self.backups}
            COMPOSE_DIR={self.compose_dir}
            KEEP_RECENT=3
            KEEP_MONTHLY=2
            """))
        self.env = dict(os.environ,
                        MILEAGE_BACKUP_CONF=str(conf),
                        MILEAGE_COMPOSE=f"{sys.executable} {fake}",
                        MILEAGE_DOCKER=f"{sys.executable} {fake}",
                        FAKE_DB_DIR=str(self.db_dir),
                        FAKE_DATA_DIR=str(self.data),
                        FAKE_MONITOR_DIR=str(self.monitor_dir),
                        FAKE_STATE_DIR=str(self.state_dir),
                        FAKE_COMPOSE_LOG=str(self.compose_log),
                        TMPDIR=str(self.tmp))
        self.env.pop("JOURNAL_STREAM", None)

    def add_events(self):
        path = self.data / "diagnostics/events.db"
        path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript("CREATE TABLE events (sequence INTEGER PRIMARY KEY);"
                               "INSERT INTO events VALUES (1);")
        conn.close()
        return path

    def add_monitor(self):
        self.env["FAKE_MONITOR"] = "yes"
        path = self.monitor_dir / "monitor.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("CREATE TABLE episodes (id INTEGER);"
                               "INSERT INTO episodes VALUES (1);"
                               "CREATE TABLE outbox (ids_json TEXT);"
                               "CREATE TABLE retry (next_attempt REAL);"
                               "CREATE TABLE gelf_cursor (rowid INTEGER);"
                               "INSERT INTO gelf_cursor VALUES (9);")
        conn.close()
        return path

    def refresh_manifest(self, snap):
        files = sorted(p for p in snap.rglob("*")
                       if p.is_file() and p.name not in ("SHA256SUMS", "INFO"))
        (snap / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(snap)}\n"
            for p in files))

    def add_trip(self, stem, content):
        for suffix in (".gpx", ".json"):
            path = self.data / (stem + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{content}{suffix}")

    def run_script(self, name, *args, check=True):
        result = subprocess.run(["bash", str(BACKUP / name), *args],
                                env=self.env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=60)
        if check and result.returncode != 0:
            self.fail(f"{name} {args} exited {result.returncode}:\n{result.stderr}")
        return result

    def snapshots(self, root=None):
        root = (root or self.backups) / "snapshots"
        return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))

    def test_backup_copies_everything_off_host_and_verifies(self):
        self.run_script("backup.sh")
        [name] = self.snapshots()
        self.assertEqual(self.snapshots(self.remote), [name])
        for root in (self.backups / "snapshots", self.remote / "snapshots"):
            snap = root / name
            self.assertEqual(
                (snap / "data/2026-10/2026-10-01T15-00-00.gpx").read_text(), "first.gpx")
            subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"],
                           cwd=snap, check=True)
            with sqlite3.connect(snap / "annotations.db") as conn:
                row = conn.execute("SELECT category, destination FROM annotations").fetchone()
            conn.close()
            self.assertEqual(row, ("business", "Acme"))
        self.assertIn("annotations=1", (self.backups / "snapshots" / name / "INFO").read_text())
        self.assertEqual(os.readlink(self.backups / "latest"), f"snapshots/{name}")
        self.assertTrue((self.backups / "last-success").exists())

    def test_unchanged_runs_add_nothing_and_changes_hard_link_old_files(self):
        self.run_script("backup.sh")
        self.run_script("backup.sh")
        self.assertEqual(len(self.snapshots()), 1)

        self.add_trip("2026-10/2026-10-02T08-00-00", "second")
        (self.data / ".upload.lock").touch()
        (self.data / "2026-10" / "x.gpx.abc.tmp").write_text("partial")
        self.run_script("backup.sh")
        first, second = self.snapshots()
        old = self.backups / "snapshots" / first / "data/2026-10/2026-10-01T15-00-00.gpx"
        new = self.backups / "snapshots" / second / "data/2026-10/2026-10-01T15-00-00.gpx"
        self.assertEqual(old.stat().st_ino, new.stat().st_ino)
        self.assertFalse((new.parent / "x.gpx.abc.tmp").exists())
        self.assertFalse((new.parents[1] / ".upload.lock").exists())
        remote_old = self.remote / "snapshots" / first / old.relative_to(old.parents[2])
        remote_new = self.remote / "snapshots" / second / new.relative_to(new.parents[2])
        self.assertEqual(remote_old.stat().st_ino, remote_new.stat().st_ino)

    def test_prune_keeps_recent_and_first_of_each_month(self):
        names = ["2026-05-01T030000Z", "2026-06-01T030000Z", "2026-06-02T030000Z",
                 "2026-07-01T030000Z", "2026-07-02T030000Z", "2026-07-03T030000Z"]
        for n in names:
            (self.backups / "snapshots" / n).mkdir(parents=True)
            (self.backups / "snapshots" / n / "SHA256SUMS").write_text("")
        (self.remote / "snapshots" / "2026-01-01T030000Z").mkdir(parents=True)
        self.run_script("backup.sh")
        current = self.snapshots()[-1]
        self.assertGreater(current, "2026-08")
        # Newest 3, plus the first of the newest 2 months (July and now).
        self.assertEqual(set(self.snapshots()), {
            "2026-07-01T030000Z", "2026-07-02T030000Z", "2026-07-03T030000Z", current})
        # The off-host copy is append-only by default...
        self.assertIn("2026-01-01T030000Z", self.snapshots(self.remote))

        # ...unless REMOTE_PRUNE=yes.
        conf = Path(self.env["MILEAGE_BACKUP_CONF"])
        conf.write_text(conf.read_text() + "REMOTE_PRUNE=yes\n")
        self.run_script("backup.sh")
        self.assertEqual(self.snapshots(self.remote), self.snapshots())

    def test_refuses_to_run_without_an_off_host_target(self):
        conf = Path(self.env["MILEAGE_BACKUP_CONF"])
        conf.write_text(conf.read_text().replace(f"REMOTE={self.remote}", "REMOTE="))
        result = self.run_script("backup.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REMOTE is not set", result.stderr)
        self.assertFalse((self.backups / "snapshots").exists())

    def test_check_passes_on_good_copy_and_catches_damage(self):
        self.run_script("backup.sh")
        result = self.run_script("restore.sh", "check")
        self.assertIn("1 annotations (1 tagged)", result.stderr)
        self.assertIn("Restore test passed", result.stderr)

        [name] = self.snapshots(self.remote)
        damaged = self.remote / "snapshots" / name / "data/2026-10/2026-10-01T15-00-00.json"
        damaged.write_text("tampered")
        result = self.run_script("restore.sh", "check", name, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("damaged", result.stderr)

    def test_check_rejects_files_not_in_the_manifest(self):
        self.run_script("backup.sh")
        [name] = self.snapshots(self.remote)
        (self.remote / "snapshots" / name / "data" / "extra.gpx").write_text("x")
        result = self.run_script("restore.sh", "check", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not in SHA256SUMS", result.stderr)

    def test_installer_advice_creates_a_populated_backup_before_checking(self):
        # The initial install has no trips or annotations to restore.
        for path in self.data.rglob("*"):
            if path.is_file():
                path.unlink()
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            conn.execute("DELETE FROM annotations")
        conn.close()
        self.run_script("backup.sh")

        # A first drive and annotation arrive before the nightly backup.
        self.add_trip("2026-10/2026-10-02T08-00-00", "first drive")
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            conn.execute("INSERT INTO annotations VALUES (?, ?, ?, ?, ?)",
                         ("2026-10/2026-10-02T08-00-00", "business", "client visit",
                          "2026-10-02T09:00:00Z", "Acme"))
        conn.close()
        stale = self.run_script("restore.sh", "check")
        self.assertIn("0 annotations", stale.stderr)
        self.assertIn("0 trip GPX files", stale.stderr)

        # Exercise the backup command the real install summary gives the user.
        shutil.copytree(BACKUP, self.compose_dir / "backup")
        (self.compose_dir / ".env").write_text("INGEST_AUTH_TOKEN=example-token\n")
        env = dict(self.env, INSTALL=str(SERVER / "install.sh"),
                   WORK=str(self.compose_dir))
        setup = textwrap.dedent("""\
            source <(sed '$d' "$INSTALL")
            cd "$WORK"
            SCRIPT_DIR=$WORK
            BIND_ADDR=192.0.2.10
            """)
        summary = subprocess.run(["bash", "-c", setup + "FIRST_BACKUP=yes\nsummary\n"],
                                 env=env, text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(summary.returncode, 0, summary.stdout + summary.stderr)
        commands = [line.strip() for line in summary.stdout.splitlines()
                    if line.strip().startswith("bash ")]
        backups = [command for command in commands if "/backup/backup.sh" in command]
        self.assertEqual(len(backups), 1, summary.stdout)
        self.assertTrue(commands[-1].endswith("/install.sh --check"), summary.stdout)
        self.assertLess(commands.index(backups[0]), len(commands) - 1)
        backup = subprocess.run(["bash", "-c", backups[0]], env=env, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(backup.returncode, 0, backup.stdout + backup.stderr)

        # --check's backup step now verifies the populated off-host snapshot.
        harness = setup + "MODE=check\nrestore_test\nsummary\n"
        checked = subprocess.run(["bash", "-c", harness], env=env, text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("1 annotations (1 tagged)", checked.stdout)
        self.assertIn("1 trip GPX files", checked.stdout)
        self.assertIn("Restore test passed", checked.stdout)

    def test_restore_rebuilds_a_wiped_server_and_keeps_what_it_replaced(self):
        self.run_script("backup.sh")
        shutil.rmtree(self.data)
        (self.db_dir / "annotations.db").unlink()
        self.data.mkdir()
        self.add_trip("2026-10/2026-10-01T15-00-00", "different")

        result = self.run_script("restore.sh", "restore", "latest", "--yes")
        self.assertIn("restored database: integrity ok, 1 annotations", result.stderr)
        self.assertEqual(
            (self.data / "2026-10/2026-10-01T15-00-00.gpx").read_text(), "first.gpx")
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            self.assertEqual(conn.execute("SELECT purpose_note FROM annotations").fetchone(),
                             ("client visit",))
        conn.close()
        [pre] = [p for p in self.backups.iterdir() if p.name.startswith("pre-restore-")]
        self.assertEqual((pre / "data/2026-10/2026-10-01T15-00-00.gpx").read_text(),
                         "different.gpx")
        calls = self.compose_log.read_text().splitlines()
        self.assertTrue(any(c.startswith("stop mileage-ingest mileage-viewer") for c in calls))
        self.assertEqual(calls[-1], "up -d")

    def test_restore_needs_confirmation(self):
        self.run_script("backup.sh")
        result = self.run_script("restore.sh", "restore", "latest", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--yes", result.stderr)
        self.assertNotIn("stop", self.compose_log.read_text())

    def test_events_snapshot_while_writer_has_uncommitted_transaction(self):
        path = self.add_events()
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO events VALUES (2)")
        self.assertTrue(Path(str(path) + "-journal").exists())
        self.run_script("backup.sh")
        snap = self.backups / "latest"
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.assertFalse((snap / ("data/diagnostics/events.db" + suffix)).exists())
        with sqlite3.connect(snap / "events.db") as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("SELECT * FROM events").fetchall(), [(1,)])
        conn.close()
        self.assertIn("events=1", (snap / "INFO").read_text())
        self.assertIn("./events.db", (snap / "SHA256SUMS").read_text())
        self.assertIn("1 events", self.run_script("restore.sh", "check").stderr)

    def test_missing_events_and_uninstalled_monitor_are_normal(self):
        result = self.run_script("backup.sh")
        snap = self.backups / "latest"
        self.assertFalse((snap / "events.db").exists())
        self.assertFalse((self.data / "diagnostics/events.db").exists())
        self.assertFalse((snap / "monitor.db").exists())
        self.assertIn("events=missing", (snap / "INFO").read_text())
        self.assertIn("monitor=not-installed", (snap / "INFO").read_text())
        self.assertIn("Monitor is not installed", result.stderr)
        self.assertNotIn("-T mileage-monitor", self.compose_log.read_text())

    def test_monitor_volume_is_backed_up_even_without_running_service(self):
        self.add_monitor()
        self.env["FAKE_MONITOR_RUNNING"] = "no"
        self.run_script("backup.sh")
        snap = self.backups / "latest"
        self.assertIn("monitor=snapshotted", (snap / "INFO").read_text())
        self.assertIn("./monitor.db", (snap / "SHA256SUMS").read_text())
        with sqlite3.connect(snap / "monitor.db") as conn:
            self.assertEqual(conn.execute("SELECT * FROM gelf_cursor").fetchone(), (9,))
        conn.close()
        self.assertIn("4 monitor state tables", self.run_script("restore.sh", "check").stderr)

    def test_installed_monitor_without_database_is_normal(self):
        self.env["FAKE_MONITOR"] = "yes"
        self.run_script("backup.sh")
        self.assertFalse((self.backups / "latest/monitor.db").exists())
        self.assertIn("monitor=missing", (self.backups / "latest/INFO").read_text())
        self.assertFalse((self.monitor_dir / "monitor.db").exists())

    def assert_complete_backup_with_unknown_monitor(self):
        result = self.run_script("backup.sh")
        self.assertEqual(result.stderr.count("WARNING:"), 1, result.stderr)
        self.assertIn("could not determine whether the monitor is installed", result.stderr)
        self.assertIn("skipping monitor.db", result.stderr)
        self.assertNotIn("private-config-sentinel", result.stdout + result.stderr)
        self.assertNotIn("private-volume-sentinel", result.stdout + result.stderr)
        self.assertNotIn("-T mileage-monitor", self.compose_log.read_text())
        self.assertTrue((self.backups / "last-success").exists())
        name = os.readlink(self.backups / "latest").split("/")[-1]
        for root in (self.backups, self.remote):
            snap = root / "snapshots" / name
            self.assertIn("monitor=unknown", (snap / "INFO").read_text())
            self.assertFalse((snap / "monitor.db").exists())
            self.assertEqual((snap / "data/2026-10/2026-10-01T15-00-00.gpx").read_text(),
                             "first.gpx")
            for kind, table in (("annotations", "annotations"), ("events", "events")):
                with sqlite3.connect(snap / f"{kind}.db") as conn:
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (1,))
                conn.close()
            subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=snap, check=True)

    def test_compose_config_failure_still_backs_up_without_monitor(self):
        self.add_events()
        self.env["FAKE_CONFIG_ERROR"] = "yes"
        self.assert_complete_backup_with_unknown_monitor()

    def test_docker_volume_failure_still_backs_up_with_unknown_monitor(self):
        self.add_events()
        self.add_monitor()
        self.env["FAKE_VOLUME_ERROR"] = "yes"
        self.assert_complete_backup_with_unknown_monitor()

    def test_invalid_config_or_ambiguous_volumes_are_unknown(self):
        self.add_events()
        self.add_monitor()
        cases = (("FAKE_CONFIG_RESULT", "invalid JSON private-config-sentinel"),
                 ("FAKE_CONFIG_RESULT", '{}'),
                 ("FAKE_CONFIG_RESULT", '{"name": "${UNRESOLVED}"}'),
                 ("FAKE_VOLUME_RESULT", "first-volume\nsecond-volume"))
        for key, value in cases:
            with self.subTest(value=value):
                self.env[key] = value
                self.assert_complete_backup_with_unknown_monitor()
                del self.env[key]

    def test_discovery_status_change_is_recorded_even_when_files_are_unchanged(self):
        self.add_events()
        self.run_script("backup.sh")
        self.env["FAKE_CONFIG_ERROR"] = "yes"
        self.assert_complete_backup_with_unknown_monitor()
        self.assertEqual(len(self.snapshots()), 2)

    def test_project_override_and_custom_volume_name_use_compose_labels(self):
        self.add_monitor()
        self.env["FAKE_PROJECT"] = "custom-project_2"
        result = self.run_script("backup.sh")
        self.assertIn("monitor=snapshotted", (self.backups / "latest/INFO").read_text())
        self.assertIn("label=com.docker.compose.project=custom-project_2", self.compose_log.read_text())
        self.assertNotIn("private-config-sentinel", result.stdout + result.stderr)

    def assert_restore_with_unknown_monitor(self, error):
        events = self.add_events()
        monitor = self.add_monitor()
        self.run_script("backup.sh")
        for path, table in ((events, "events"), (monitor, "episodes")):
            with sqlite3.connect(path) as conn:
                conn.execute(f"INSERT INTO {table} VALUES (2)")
            conn.close()
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            conn.execute("UPDATE annotations SET purpose_note = 'changed'")
        conn.close()
        self.add_trip("2026-10/2026-10-01T15-00-00", "changed")
        before = monitor.read_bytes()
        self.compose_log.write_text("")
        self.env[error] = "yes"
        self.env["COMPOSE_PROFILES"] = "monitor"
        result = self.run_script("restore.sh", "restore", "latest", "--yes")
        self.assertEqual(result.stderr.count("WARNING:"), 1, result.stderr)
        self.assertIn("skipping monitor.db", result.stderr)
        self.assertIn("To restore it by hand", result.stderr)
        self.assertIn("SQLite's backup API", result.stderr)
        self.assertNotIn("private-config-sentinel", result.stdout + result.stderr)
        self.assertNotIn("private-volume-sentinel", result.stdout + result.stderr)
        self.assertNotIn("mileage-monitor", self.compose_log.read_text())
        self.assertEqual(before, monitor.read_bytes())
        with sqlite3.connect(events) as conn:
            self.assertEqual(conn.execute("SELECT * FROM events").fetchall(), [(1,)])
        conn.close()
        with sqlite3.connect(self.db_dir / "annotations.db") as conn:
            self.assertEqual(conn.execute("SELECT purpose_note FROM annotations").fetchone(),
                             ("client visit",))
        conn.close()
        self.assertEqual((self.data / "2026-10/2026-10-01T15-00-00.gpx").read_text(), "first.gpx")
        [pre] = list(self.backups.glob("pre-restore-*"))
        self.assertFalse((pre / "monitor.db").exists())
        for kind in ("annotations", "events"):
            self.assertTrue((pre / f"{kind}.db").exists())
        self.assertEqual(self.compose_log.read_text().splitlines()[-1],
                         "up -d mileage-ingest mileage-viewer")

    def test_restore_config_failure_restores_everything_except_monitor(self):
        self.assert_restore_with_unknown_monitor("FAKE_CONFIG_ERROR")

    def test_restore_volume_failure_restores_everything_except_monitor(self):
        self.assert_restore_with_unknown_monitor("FAKE_VOLUME_ERROR")

    def test_restore_new_databases_stops_writers_and_saves_current_state(self):
        events = self.add_events()
        monitor = self.add_monitor()
        self.run_script("backup.sh")
        for path, table in ((events, "events"), (monitor, "episodes")):
            with sqlite3.connect(path) as conn:
                conn.execute(f"INSERT INTO {table} VALUES (2)")
            conn.close()
        self.run_script("restore.sh", "restore", "latest", "--yes")
        [pre] = list(self.backups.glob("pre-restore-*"))
        for path, table, name in ((events, "events", "events"),
                                  (monitor, "episodes", "monitor")):
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (1,))
            conn.close()
            with sqlite3.connect(pre / f"{name}.db") as conn:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (2,))
            conn.close()
        calls = self.compose_log.read_text().splitlines()
        self.assertIn("--profile monitor stop mileage-monitor", calls)
        self.assertEqual(calls[-1], "--profile monitor start mileage-monitor")

    def test_restore_to_replacement_server_creates_database_directories(self):
        self.add_events()
        self.add_monitor()
        self.run_script("backup.sh")
        shutil.rmtree(self.data / "diagnostics")
        (self.monitor_dir / "monitor.db").unlink()
        self.env.pop("FAKE_MONITOR")
        self.run_script("restore.sh", "restore", "latest", "--yes")
        self.assertTrue((self.data / "diagnostics/events.db").exists())
        self.assertTrue((self.monitor_dir / "monitor.db").exists())
        self.assertEqual(self.compose_log.read_text().splitlines()[-1], "up -d")

    def test_restore_leaves_previously_stopped_monitor_stopped(self):
        self.add_monitor()
        self.env["FAKE_MONITOR_RUNNING"] = "no"
        self.run_script("backup.sh")
        self.run_script("restore.sh", "restore", "latest", "--yes")
        self.assertNotIn("start mileage-monitor", self.compose_log.read_text())

    def test_restore_old_snapshot_ignores_live_sqlite_copy_in_data(self):
        events = self.add_events()
        monitor = self.add_monitor()
        self.run_script("backup.sh")
        [name] = self.snapshots(self.remote)
        snap = self.remote / "snapshots" / name
        (snap / "events.db").unlink()
        (snap / "monitor.db").unlink()
        legacy = snap / "data/diagnostics/events.db"
        legacy.write_bytes(b"unsafe old live copy")
        Path(str(legacy) + "-journal").write_bytes(b"unsafe journal")
        self.refresh_manifest(snap)
        before = (events.read_bytes(), monitor.read_bytes())
        result = self.run_script("restore.sh", "restore", name, "--yes")
        self.assertIn("No events.db in this snapshot", result.stderr)
        self.assertIn("No monitor.db in this snapshot", result.stderr)
        self.assertEqual(before, (events.read_bytes(), monitor.read_bytes()))
        self.assertFalse(Path(str(events) + "-journal").exists())

    def test_corrupt_new_database_rejected_before_stopping_services(self):
        self.add_events()
        self.add_monitor()
        self.run_script("backup.sh")
        [name] = self.snapshots(self.remote)
        snap = self.remote / "snapshots" / name
        for kind in ("events", "monitor"):
            path = snap / f"{kind}.db"
            good = path.read_bytes()
            path.write_bytes(b"not sqlite")
            self.refresh_manifest(snap)
            result = self.run_script("restore.sh", "restore", name, "--yes", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{kind} database failed", result.stderr)
            self.assertNotIn("stop ", self.compose_log.read_text())
            path.write_bytes(good)
            self.refresh_manifest(snap)

    def test_safety_snapshot_failure_prevents_restore_and_restarts_services(self):
        self.add_events()
        monitor = self.add_monitor()
        self.run_script("backup.sh")
        before = (self.db_dir / "annotations.db").read_bytes()
        monitor.write_bytes(b"broken live monitor database")
        result = self.run_script("restore.sh", "restore", "latest", "--yes", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, (self.db_dir / "annotations.db").read_bytes())
        self.assertEqual(self.compose_log.read_text().splitlines()[-1],
                         "--profile monitor start mileage-monitor")


if __name__ == "__main__":
    unittest.main()
