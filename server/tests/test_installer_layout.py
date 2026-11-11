"""Installer and --check require the shared-package build inputs."""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1]
INSTALL = (SERVER / "install.sh").read_text(encoding="utf-8")
SERVICES = ("mileage-ingest", "mileage-viewer", "mileage-monitor")


def expand_braces(word):
    """Expand the comma lists used by preflight, without executing shell code."""
    match = re.search(r"\{([^{}]+)\}", word)
    if match is None:
        return [word]
    return [item for choice in match[1].split(",")
            for item in expand_braces(word[:match.start()] + choice + word[match.end():])]


def preflight_files():
    preflight = INSTALL.split("preflight() {", 1)[1].split("\n}\n", 1)[0]
    loop = re.search(r"local f missing=\(\)\s+for f in (.*?); do", preflight, re.S)
    return {path for word in shlex.split(loop[1].replace("\\\n", " "))
            for path in expand_braces(word)}


def copy_sources():
    sources = set()
    for service in SERVICES:
        dockerfile = (SERVER / service / "Dockerfile").read_text(encoding="utf-8")
        for line in dockerfile.replace("\\\n", " ").splitlines():
            if line.startswith("COPY "):
                args = shlex.split(line[5:], comments=True)
                # Fail clearly if COPY syntax changes beyond these local sources.
                if len(args) < 2 or any(arg.startswith("--") or arg.startswith("[") for arg in args):
                    raise AssertionError(f"Extend the COPY parser for {service}: {line}")
                sources.update(args[:-1])
    return sources


def allowed_files():
    return {line[1:] for line in (SERVER / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.startswith("!") and not line.endswith("/")}


class InstallerLayout(unittest.TestCase):
    def test_monitor_image_copies_every_runtime_module(self):
        modules = {path.relative_to(SERVER).as_posix()
                   for path in (SERVER / "mileage-monitor").glob("*.py")}
        self.assertFalse(modules - copy_sources(),
                         f"Monitor modules missing from Dockerfile COPY: {sorted(modules - copy_sources())}")
        self.assertFalse(modules - allowed_files(),
                         f"Monitor modules excluded by .dockerignore: {sorted(modules - allowed_files())}")

    def test_preflight_checks_shared_build_inputs_in_both_modes(self):
        preflight = INSTALL.split("preflight() {", 1)[1].split("\n}\n", 1)[0]
        for required in (".dockerignore", "common/__init__.py",
                         "common/diagnostics/{__init__.py,catalog.py,protocol.py,catalog.json}",
                         "mileage-ingest/{Dockerfile,app.py,diag_store.py,reset_high_water.py,requirements.txt}",
                         "mileage-viewer/{Dockerfile,app.py,geocoding.py,trip_merges.py,trip_log.py,users.py,manage_users.py,diagnostics_settings.py,requirements.txt}",
                         "mileage-monitor/{Dockerfile,monitor.py,gelf.py,prometheus.py,loki.py,shared.py,settings_rules.py}",
                         "monitor.env.example", "ntfy/server.yml"):
            self.assertIn(required, preflight)
        main = INSTALL.split("main() {", 1)[1]
        self.assertRegex(main, r"preflight\s+setup_docker\s+configure\s+if.*MODE.*install")

    def test_preflight_covers_every_copy_source_and_service_python_module(self):
        required = preflight_files()
        modules = {path.relative_to(SERVER).as_posix()
                   for folder in ("common", *SERVICES)
                   for path in (SERVER / folder).rglob("*.py")}
        inputs = set(modules)
        for source in copy_sources():
            self.assertTrue((SERVER / source).exists(), source)
            if (SERVER / source).is_dir():
                copied = {path for path in allowed_files() if path.startswith(source.rstrip("/") + "/")}
                self.assertTrue(copied, f"No allowed build inputs under COPY {source}")
                inputs.update(copied)
            else:
                inputs.add(source)
        self.assertFalse(inputs - required, f"Missing preflight inputs: {sorted(inputs - required)}")
        self.assertTrue(all((SERVER / path).is_file() for path in required))

    def test_default_deny_context_includes_runtime_inputs_and_their_parents(self):
        rules = (SERVER / ".dockerignore").read_text(encoding="utf-8").splitlines()
        allowed = allowed_files()
        modules = {path.relative_to(SERVER).as_posix()
                   for folder in ("common", *SERVICES)
                   for path in (SERVER / folder).rglob("*.py")}
        inputs = modules | {source for source in copy_sources() if (SERVER / source).is_file()}
        self.assertFalse(inputs - allowed, f"Runtime files excluded by .dockerignore: {sorted(inputs - allowed)}")
        self.assertFalse(allowed - preflight_files(),
                         f"Allowed runtime files missing from preflight: {sorted(allowed - preflight_files())}")
        for path in allowed:
            self.assertTrue((SERVER / path).is_file(), path)
            for parent in Path(path).parents:
                if parent != Path("."):
                    self.assertIn("!" + parent.as_posix() + "/", rules)

    def test_release_archive_includes_all_preflight_inputs(self):
        bundle = (SERVER.parent / "tools/build_release_bundle.sh").read_text(encoding="utf-8")
        command = re.search(r"^git archive .*?(?=\n\n)", bundle, re.M | re.S)[0]
        paths = shlex.split(command.replace("\\\n", " "))
        paths = paths[paths.index("--") + 1:]
        required = {"server/" + path for path in preflight_files()}
        missing = {path for path in required if not any(
            path == source or (source.endswith("/") and path.startswith(source)) for source in paths)}
        self.assertFalse(missing, f"Preflight inputs missing from the release archive: {sorted(missing)}")

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
                for missing in (".dockerignore", "common/diagnostics/catalog.json", "common/trip_summary.py",
                                "mileage-ingest/diag_store.py", "mileage-viewer/trip_merges.py",
                                "mileage-viewer/trip_log.py", "mileage-viewer/users.py", "mileage-viewer/manage_users.py",
                                "common/retention.py", "mileage-viewer/diagnostics_settings.py",
                                "mileage-viewer/geocoding.py", "mileage-viewer/static/endpoint-places.js",
                                "mileage-monitor/monitor.py", "mileage-monitor/gelf.py",
                                "mileage-monitor/prometheus.py", "mileage-monitor/loki.py", "mileage-monitor/shared.py",
                                "mileage-monitor/settings_rules.py", "mileage-monitor/Dockerfile", "monitor.env.example", "ntfy/server.yml"):
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
