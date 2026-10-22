"""Exercise the release wrapper with local archives; never run a real install."""

import hashlib
import os
from pathlib import Path
import select
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest


SETUP = Path(__file__).resolve().parents[2] / "install" / "setup.sh"
RELEASES = "https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases"


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None,
                 "needs a POSIX bash (run in WSL on Windows)")
class ReleaseSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.destination = self.root / "installed server"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        HOME=str(self.root), TMPDIR=str(self.root),
                        FAKE_ASSETS=str(self.root / "assets"),
                        FAKE_DOWNLOAD_LOG=str(self.root / "downloads"))
        self.command("docker", 'case "$*" in\n'
                     '  "compose version --short") echo "${FAKE_COMPOSE_VERSION:-2.38.2}" ;;\n'
                     '  info) exit "${FAKE_DOCKER_EXIT:-0}" ;;\n'
                     '  *) echo "unexpected Docker call" >&2; exit 99 ;;\n'
                     'esac\n')
        self.command("id", 'printf "%s\\n" "user"\n')
        # A route lookup only; do not contact a network or expose host addresses.
        self.command("ip", 'echo "default src <host>"\n')
        self.command("df", 'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 9999999 0 9000000 0%% /\\n"\n')

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def bundle(self, tag="v1.00", version="v0.10", commit="a" * 40, builder=False,
               extra=None):
        assets = self.root / "assets"
        assets.mkdir(exist_ok=True)
        tree = self.root / f"tree-{tag}"
        tree.mkdir()
        files = {
            "server/install.sh": '#!/usr/bin/env bash\nset -eu\npwd > installer-ran\n'
                                 'exit "${FAKE_INSTALL_EXIT:-0}"\n',
            "server/VERSION": version + "\n",
            "server/COMMIT": commit + "\n",
            "server/.dockerignore": "private files excluded\n",
            "server/.env.example": "BIND_ADDR=\n",
            "server/docker-compose.yml": "services: {}\n",
            "server/backup/backup.sh": "# public backup helper\n",
            "firmware/VERSION": "v0.10\n",
            "firmware/COMMIT": commit + "\n",
            "firmware/secrets.h.example": "// template only\n",
            "firmware/04_trip_logger/04_trip_logger.ino": "// source\n",
            "tools/stamp_firmware_version.py": "# stamp helper\n",
            "tools/not-installed.py": "# unrelated tool\n",
        }
        if builder:
            files["server/firmware-builder/build.sh"] = (
                '#!/usr/bin/env bash\necho "builder unexpectedly ran" >&2\nexit 98\n')
        files.update(extra or {})
        for name, content in files.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755 if name.endswith(".sh") else 0o644)
        archive = assets / f"mileage-tracker-{tag}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tree, arcname=f"mileage-tracker-{tag}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        # Other release assets need not exist and must never be checked.
        (assets / "SHA256SUMS").write_text(
            f'{"0" * 64}  flash.ps1\n{digest}  {archive.name}\n', encoding="ascii")
        return archive

    def run_setup(self, *args, script=SETUP, yes=True):
        command = ["bash", str(script), "--dir", str(self.destination)]
        if yes:
            command.append("--yes")
        return subprocess.run(command + list(args), env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30,
                              start_new_session=True)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_layout_handoff_and_exact_piece_two_commands(self):
        archive = self.bundle()
        result = self.run_setup("--bundle", str(archive))
        self.assert_ok(result)
        dest = self.destination
        for name in ("install.sh", ".dockerignore", ".env.example", "backup/backup.sh",
                     "firmware-src/firmware/04_trip_logger/04_trip_logger.ino",
                     "firmware-src/firmware/COMMIT", "firmware-src/firmware/secrets.h.example",
                     "firmware-src/tools/stamp_firmware_version.py"):
            self.assertTrue((dest / name).is_file(), name)
        self.assertEqual((dest / "installer-ran").read_text().strip(), str(dest))
        self.assertFalse((dest / "server").exists())
        self.assertFalse((dest / "firmware-src/tools/not-installed.py").exists())
        self.assertIn("First server install: v0.10+aaaaaaaaaa", result.stdout)
        self.assertIn("The firmware builder isn't in this release", result.stdout)
        self.assertIn(f"irm {RELEASES}/download/v1.00/flash.ps1 | iex", result.stdout)
        self.assertIn("-Server 'user@<host>' -InstallDir '", result.stdout)
        self.assertIn("bash -s -- --server 'user@<host>' --install-dir ", result.stdout)

    def test_checksum_failure_leaves_existing_install_untouched(self):
        archive = self.bundle()
        with archive.open("ab") as stream:
            stream.write(b"corrupt")
        self.destination.mkdir()
        marker = self.destination / "VERSION"
        marker.write_text("old\n")
        result = self.run_setup("--bundle", str(archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Checksum verification failed", result.stderr)
        self.assertEqual(marker.read_text(), "old\n")
        self.assertFalse((self.destination / "installer-ran").exists())

    def test_private_paths_survive_update_and_deleted_source_is_retained(self):
        first = self.bundle(extra={"server/obsolete.py": "old source\n"})
        self.assert_ok(self.run_setup("--bundle", str(first)))
        private = (".env", "viewer.env", "monitor.env", "backup/backup.conf",
                   "data/trip.gpx", "firmware-out/current/app.bin")
        for name in private:
            path = self.destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep private\n")
            path.chmod(0o600)
        second = self.bundle("v1.01", "v0.11", "b" * 40,
                             extra={f"server/{name}": "must not replace\n" for name in private})
        result = self.run_setup("--bundle", str(second))
        self.assert_ok(result)
        for name in private:
            path = self.destination / name
            self.assertEqual(path.read_text(), "keep private\n", name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, name)
        self.assertEqual((self.destination / "obsolete.py").read_text(), "old source\n")
        self.assertIn("v0.10+aaaaaaaaaa -> v0.11+bbbbbbbbbb", result.stdout)
        self.assertIn("Files deleted upstream are not removed", result.stdout)

    def test_checkout_placeholder_requires_tag_or_bundle(self):
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Supply --tag <tag> or --bundle <file>", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_stamped_script_downloads_same_release_and_tag_can_override(self):
        self.bundle()
        self.command("curl", 'url= out=\nwhile (( $# )); do\n'
                     'case $1 in\n-o) out=$2; shift 2 ;;\n--retry) shift 2 ;;\n'
                     '-*) shift ;;\n*) url=$1; shift ;;\nesac\ndone\n'
                     'printf "%s\\n" "$url" >> "$FAKE_DOWNLOAD_LOG"\n'
                     'cp "$FAKE_ASSETS/${url##*/}" "$out"\n')
        stamped = self.root / "setup.sh"
        stamped.write_text(SETUP.read_text().replace("@RELEASE_TAG@", "v1.00"))
        self.assert_ok(self.run_setup(script=stamped))
        self.assertEqual((self.root / "downloads").read_text().splitlines(), [
            f"{RELEASES}/download/v1.00/mileage-tracker-v1.00.tar.gz",
            f"{RELEASES}/download/v1.00/SHA256SUMS"])
        self.bundle("v1.01")
        (self.root / "downloads").unlink()
        self.assert_ok(self.run_setup("--tag", "v1.01", script=stamped))
        self.assertTrue(all("/download/v1.01/" in url for url in
                            (self.root / "downloads").read_text().splitlines()))

    def test_no_terminal_has_download_read_run_advice(self):
        archive = self.bundle()
        result = self.run_setup("--bundle", str(archive), yes=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No terminal", result.stderr)
        self.assertIn("-o setup.sh", result.stderr)
        self.assertIn("bash setup.sh", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_yes_skips_interactive_builder_and_prints_manual_command(self):
        result = self.run_setup("--bundle", str(self.bundle(builder=True)))
        self.assert_ok(result)
        self.assertIn("Firmware build skipped", result.stdout)
        self.assertIn("firmware-builder/build.sh --install-dir", result.stdout)

    def test_bind_address_is_read_as_data_without_sourcing_private_env(self):
        self.destination.mkdir()
        (self.destination / ".env").write_text(
            "BIND_ADDR='tracker.invalid'\nOTHER=$(touch env-was-sourced)\n")
        result = self.run_setup("--bundle", str(self.bundle()))
        self.assert_ok(result)
        self.assertIn("-Server 'user@tracker.invalid'", result.stdout)
        self.assertNotIn("OTHER", result.stdout + result.stderr)
        self.assertFalse((self.destination / "env-was-sourced").exists())

    def test_installer_failure_stops_before_builder_and_flash_commands(self):
        archive = self.bundle(builder=True)
        self.env["FAKE_INSTALL_EXIT"] = "17"
        result = self.run_setup("--bundle", str(archive))
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertNotIn("Piece 2:", result.stdout)
        self.assertNotIn("Firmware build skipped", result.stdout)

    def test_duplicate_or_missing_bundle_checksum_aborts(self):
        archive = self.bundle()
        sums = archive.parent / "SHA256SUMS"
        original = sums.read_text()
        for contents in (original + original, f'{"0" * 64}  flash.ps1\n'):
            sums.write_text(contents)
            result = self.run_setup("--bundle", str(archive))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one entry", result.stderr)
            self.assertFalse(self.destination.exists())

    def test_mismatched_bundle_tag_aborts(self):
        result = self.run_setup("--bundle", str(self.bundle()), "--tag", "v1.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_preflight_explains_docker_access_compose_and_disk_failures(self):
        archive = self.bundle()
        for setting, value, message in (("FAKE_DOCKER_EXIT", "1", "docker group"),
                                        ("FAKE_COMPOSE_VERSION", "1.29.2", "v2 or newer")):
            with self.subTest(setting=setting):
                self.env[setting] = value
                result = self.run_setup("--bundle", str(archive))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr.lower() if setting == "FAKE_DOCKER_EXIT" else result.stderr)
                self.assertFalse(self.destination.exists())
                del self.env[setting]
        self.command("df", 'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 999 0 999 0%% /\\n"\n')
        result = self.run_setup("--bundle", str(archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Less than 1 GiB", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_source_symlink_is_rejected_before_install(self):
        archive = self.bundle()
        # Repack a checksum-valid but unsafe bundle containing a link.
        with tarfile.open(archive, "w:gz") as tar:
            link = tarfile.TarInfo("mileage-tracker-v1.00/server/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "/tmp"
            tar.addfile(link)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (archive.parent / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
        result = self.run_setup("--bundle", str(archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no links or devices", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_truncated_download_never_copies_or_runs_installer(self):
        archive = self.bundle()
        content = SETUP.read_text()
        for suffix in (content.rsplit('\nmain "$@"', 1)[0],
                       content.rsplit('\n}', 1)[0]):
            truncated = self.root / "truncated.sh"
            truncated.write_text(suffix + "\n")
            self.run_setup("--bundle", str(archive), script=truncated)
            self.assertFalse(self.destination.exists())

    def test_pipe_reads_wrapper_installer_and_builder_prompts_from_tty(self):
        # A real controlling pseudo-terminal, while bash reads the script from
        # a pipe. Only fixture install/build scripts and fake Docker are run.
        import errno
        import pty
        import signal

        archive = self.bundle(extra={
            "server/install.sh": '#!/usr/bin/env bash\nset -eu\n'
                                 'printf "Installer test answer: "\nIFS= read -r answer\n'
                                 '[[ $answer == from-tty ]]\nprintf "%s" "$answer" > installer-ran\n',
            "server/firmware-builder/build.sh": '#!/usr/bin/env bash\nset -eu\n'
                                                'printf "Builder test answer: " > /dev/tty\n'
                                                'IFS= read -r answer < /dev/tty\n'
                                                '[[ $answer == build-from-tty ]]\n'
                                                'printf "%s\\n" "$@" > "$2/builder-ran"\n',
        })
        pid, master = pty.fork()
        if pid == 0:
            os.execve(shutil.which("bash"), ["bash", "-c",
                      'cat "$1" | bash -s -- --bundle "$2" --dir "$3"',
                      "setup-test", str(SETUP), str(archive), str(self.destination)], self.env)
        self.addCleanup(os.close, master)
        answers = [(b"Copy this release and run the server installer? [y/N]:", b"y\n"),
                   (b"Installer test answer:", b"from-tty\n"),
                   (b"Build your tracker firmware now (about 2 GB extra disk)? [y/N]:", b"y\n"),
                   (b"Builder test answer:", b"build-from-tty\n"),
                   (b"(correct user@host if needed):", b"user@chosen.invalid\n")]
        output = b""
        pending = b""
        terminal_closed = False
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    block = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        terminal_closed = True
                        break
                    raise
                if not block:
                    terminal_closed = True
                    break
                output += block
                pending += block
                if answers and answers[0][0] in pending:
                    prompt, answer = answers.pop(0)
                    pending = pending.split(prompt, 1)[1]
                    os.write(master, answer)
            else:
                self.fail("TTY interaction timed out: " + output.decode(errors="replace"))
        finally:
            # Kill/reap on failure as well; never leave a waiting terminal child.
            if not terminal_closed:
                os.killpg(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(status, 0, output.decode(errors="replace"))
        self.assertEqual(answers, [])
        self.assertEqual((self.destination / "installer-ran").read_text(), "from-tty")
        self.assertEqual((self.destination / "builder-ran").read_text().splitlines(),
                         ["--install-dir", str(self.destination)])
        self.assertIn(b"-Server 'user@chosen.invalid'", output)
