"""install.sh picks the LAN bind address without needing a default route (#47).

Sources the real install.sh functions (everything but the final `main`)
into bash, with `ip` replaced by a fake that knows one LAN address and
may have no default route, then runs setup_bind_addr against a scratch
.env. Needs bash, so it skips on Windows.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


SERVER = Path(__file__).resolve().parents[1]
INSTALL = SERVER / "install.sh"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=install
    ip() {
        case "$*" in
            *"addr show"*)
                printf '2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\\n' ;;
            *"route get"*)
                if [[ -n ${FAKE_ROUTE:-} ]]; then
                    printf '1.1.1.1 via 192.168.1.1 dev eth0 src %s uid 1000\\n' "$FAKE_ROUTE"
                else
                    echo 'RTNETLINK answers: Network is unreachable' >&2
                    return 2
                fi ;;
        esac
    }
    cd "$WORK"
    setup_bind_addr
    printf 'DONE\\n'
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerBindAddr(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.env = self.work / ".env"
        self.env.write_text("INGEST_AUTH_TOKEN=x\nBIND_ADDR=\n")

    def run_step(self, answers="", route="", **preset):
        env = {**os.environ, "INSTALL": str(INSTALL), "WORK": str(self.work),
               "FAKE_ROUTE": route}
        env.pop("INSTALL_BIND_ADDR", None)
        env.update(preset)
        return subprocess.run(["bash", "-c", HARNESS], input=answers, env=env,
                              capture_output=True, text=True, timeout=30)

    def bind_addr(self):
        lines = [l for l in self.env.read_text().splitlines()
                 if l.startswith("BIND_ADDR=")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split("=", 1)[1]

    def test_preset_without_default_route(self):
        proc = self.run_step(INSTALL_BIND_ADDR="192.168.1.10")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        self.assertEqual(self.bind_addr(), "192.168.1.10")

    def test_interactive_without_default_route(self):
        # No suggestion to accept, so Enter asks again; then a typed address.
        proc = self.run_step(answers="\n192.168.1.10\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Type one of the addresses above", proc.stdout)
        self.assertEqual(self.bind_addr(), "192.168.1.10")

    def test_route_suggestion_is_still_offered(self):
        proc = self.run_step(answers="\n", route="192.168.1.10")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("[192.168.1.10]", proc.stdout)
        self.assertEqual(self.bind_addr(), "192.168.1.10")

    def test_route_source_that_isnt_a_lan_address_isnt_suggested(self):
        proc = self.run_step(answers="\n192.168.1.10\n", route="10.99.0.2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("[10.99.0.2]", proc.stdout)
        self.assertEqual(self.bind_addr(), "192.168.1.10")

    def test_invalid_preset_is_still_rejected(self):
        proc = self.run_step(INSTALL_BIND_ADDR="10.0.0.99")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("DONE", proc.stdout)
        self.assertIn("INSTALL_BIND_ADDR=10.0.0.99 isn't an address of this server",
                      proc.stdout)
        self.assertEqual(self.bind_addr(), "")


if __name__ == "__main__":
    unittest.main()
