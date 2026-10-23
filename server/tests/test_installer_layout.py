"""Installer and --check require the shared-package build inputs."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1]
INSTALL = (SERVER / "install.sh").read_text(encoding="utf-8")


class InstallerLayout(unittest.TestCase):
    def test_preflight_checks_shared_build_inputs_in_both_modes(self):
        preflight = INSTALL.split("preflight() {", 1)[1].split("\n}\n", 1)[0]
        for required in (".dockerignore", "common/__init__.py",
                         "common/diagnostics/{__init__.py,catalog.py,protocol.py,catalog.json}",
                         "mileage-ingest/{Dockerfile,app.py,diag_store.py,reset_high_water.py,requirements.txt}",
                         "mileage-viewer/{Dockerfile,app.py,geocoding.py,requirements.txt}",
                         "mileage-monitor/{Dockerfile,monitor.py,gelf.py,prometheus.py,loki.py}",
                         "monitor.env.example", "ntfy/server.yml"):
            self.assertIn(required, preflight)
        main = INSTALL.split("main() {", 1)[1]
        self.assertRegex(main, r"preflight\s+setup_docker\s+configure\s+if.*MODE.*install")

    @unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
    def test_required_file_loop_rejects_missing_runtime_inputs(self):
        # Run the actual preflight file loop without OS, sudo or Docker actions.
        loop = re.search(r"    local f missing=\(\).*?    ok \"All the server files are here\"",
                         INSTALL, re.S).group(0)
        script = 'set -e\ndie() { echo "$1"; exit 1; }; ok() { :; }; check_layout() {\n' + loop + '\n}; check_layout\n'
        with tempfile.TemporaryDirectory() as root:
            tree = Path(root) / "server"
            shutil.copytree(SERVER, tree, ignore=shutil.ignore_patterns(
                ".env", "viewer.env", "monitor.env", "data", "backups", "__pycache__"))
            for mode in ("install", "check"):
                env = dict(os.environ, MODE=mode, SCRIPT_DIR=str(tree))
                result = subprocess.run(["bash", "-c", script], cwd=tree, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for missing in (".dockerignore", "common/diagnostics/catalog.json", "mileage-ingest/diag_store.py",
                                "mileage-viewer/geocoding.py", "mileage-viewer/static/endpoint-places.js",
                                "mileage-monitor/monitor.py", "mileage-monitor/gelf.py",
                                "mileage-monitor/prometheus.py", "mileage-monitor/loki.py",
                                "mileage-monitor/Dockerfile", "monitor.env.example", "ntfy/server.yml"):
                    file = tree / missing
                    content = file.read_bytes()
                    file.unlink()
                    try:
                        result = subprocess.run(["bash", "-c", script], cwd=tree, env=env,
                                                capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(missing, result.stdout)
                    finally:
                        file.write_bytes(content)
