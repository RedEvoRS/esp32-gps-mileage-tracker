"""Verify actual container port mappings without attributing other HTTP services (#45).

Sources the real installer functions into Bash, replacing Compose's port
queries and the response from an unrelated loopback HTTP service.
"""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
LAN_ADDR = "192.168.1.10"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=$TEST_MODE
    BIND_ADDR=$TEST_BIND_ADDR
    VIEWER_BIND_ADDR=$TEST_VIEWER_BIND_ADDR
    compose() {
        case "$*" in
            'port mileage-ingest 8090') printf '%s\\n' "$INGEST_MAPPING" ;;
            'port mileage-viewer 8091') printf '%s\\n' "$VIEWER_MAPPING" ;;
            *) return 2 ;;
        esac
        [[ $2 != "$FAILED_SERVICE" ]]
    }
    # An HTTP response alone cannot identify the service that owns the socket.
    curl() { return "$LOOPBACK_STATUS"; }
    verify_binding
    printf 'FAILURES=%s\\n' "$FAILURES"
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerBinding(unittest.TestCase):
    def run_check(self, *, bind_addr=LAN_ADDR, ingest=None, viewer=None,
                  loopback_status=0, mode="check", failed_service="", viewer_bind_addr=""):
        env = {
            **os.environ, "INSTALL": str(INSTALL), "TEST_MODE": mode,
            "TEST_BIND_ADDR": bind_addr,
            "TEST_VIEWER_BIND_ADDR": viewer_bind_addr,
            "INGEST_MAPPING": f"{bind_addr}:8090" if ingest is None else ingest,
            "VIEWER_MAPPING": f"{bind_addr}:8091" if viewer is None else viewer,
            "LOOPBACK_STATUS": str(loopback_status), "FAILED_SERVICE": failed_service,
        }
        return subprocess.run(["bash", "-c", HARNESS], env=env,
                              capture_output=True, text=True, timeout=30)

    def assert_passes(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILURES=0", proc.stdout)

    def test_lan_mappings_pass_with_an_unrelated_loopback_http_service(self):
        for mode in ("install", "check"):
            with self.subTest(mode=mode):
                proc = self.run_check(mode=mode)
                self.assert_passes(proc)
                for port in (8090, 8091):
                    self.assertIn(f"Port {port} is published on {LAN_ADDR} only", proc.stdout)

    def test_lan_mappings_pass_without_a_loopback_service(self):
        self.assert_passes(self.run_check(loopback_status=7))

    def test_explicit_loopback_mappings_pass(self):
        self.assert_passes(self.run_check(bind_addr="127.0.0.1"))

    def test_wildcard_incorrect_missing_and_extra_mappings_fail(self):
        for service, port in (("ingest", 8090), ("viewer", 8091)):
            mappings = (
                f"0.0.0.0:{port}", f"[::]:{port}",
                f"192.168.1.11:{port}", f"127.0.0.1:{port}",
                f"{LAN_ADDR}:{port + 10}", "",
                f"{LAN_ADDR}:{port}\n0.0.0.0:{port}",
                f"{LAN_ADDR}:{port}\n[::]:{port}",
            )
            for mapping in mappings:
                with self.subTest(service=service, mapping=mapping):
                    proc = self.run_check(**{service: mapping}, loopback_status=7)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertIn("FAILURES=1", proc.stdout)
                    self.assertIn(f"FAIL  Port {port} is published on", proc.stdout)
                    self.assertIn("Recreate the containers", proc.stdout)

    def test_failed_mapping_queries_fail_even_if_they_print_the_expected_mapping(self):
        for service in ("mileage-ingest", "mileage-viewer"):
            with self.subTest(service=service):
                proc = self.run_check(failed_service=service, loopback_status=7)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("FAILURES=1", proc.stdout)
                self.assertIn("is published on nothing", proc.stdout)

    def test_install_stops_on_an_incorrect_mapping(self):
        proc = self.run_check(mode="install", ingest="0.0.0.0:8090", loopback_status=7)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Install stopped", proc.stdout)

    def test_unset_bind_address_skips_the_restriction_check(self):
        self.assert_passes(self.run_check(bind_addr="", ingest="", viewer=""))

    def test_viewer_override_mapping_checked_independently_of_ingest(self):
        for bind_addr in (LAN_ADDR, ""):
            self.assert_passes(self.run_check(bind_addr=bind_addr, viewer_bind_addr="127.0.0.1",
                                             viewer="127.0.0.1:8091"))
            proc = self.run_check(bind_addr=bind_addr, viewer_bind_addr="127.0.0.1",
                                  viewer=f"{LAN_ADDR}:8091")
            self.assertIn("FAILURES=1", proc.stdout)

    def test_ipv6_viewer_mapping_uses_brackets(self):
        self.assert_passes(self.run_check(viewer_bind_addr="::1", viewer="[::1]:8091"))


if __name__ == "__main__":
    unittest.main()
