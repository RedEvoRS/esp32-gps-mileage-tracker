"""Exercise backup.sh and restore.sh against a fake `docker compose`.

The fake runs the scripts' Python snippets with the local interpreter,
mapping the container's /db to a temp directory, so the real SQLite
backup, check and restore code runs. The off-host REMOTE is a local
directory. Needs bash, rsync and GNU coreutils, so it skips on Windows.
"""

import os
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
    import os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_COMPOSE_LOG"], "a") as log:
        log.write(" ".join(a for a in args if "\\n" not in a) + "\\n")
    if args[0] == "run":
        args = args[args.index("python"):]
        db = os.environ["FAKE_DB_DIR"]
        args = [a.replace("/db/", db + "/", 1) if a.startswith("/db/") else a
                for a in args]
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
        self.backups = self.tmp / "backups"
        self.remote = self.tmp / "nas"
        for d in (self.data / "2026-10", self.db_dir, self.remote):
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
                        FAKE_DB_DIR=str(self.db_dir),
                        FAKE_COMPOSE_LOG=str(self.compose_log),
                        TMPDIR=str(self.tmp))
        self.env.pop("JOURNAL_STREAM", None)

    def add_trip(self, stem, content):
        for suffix in (".gpx", ".json"):
            path = self.data / (stem + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{content}{suffix}")

    def run_script(self, name, *args, check=True):
        result = subprocess.run(["bash", str(BACKUP / name), *args],
                                env=self.env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
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


if __name__ == "__main__":
    unittest.main()
