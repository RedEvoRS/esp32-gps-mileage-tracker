"""The persisted ntfy proxy switch reaches the rendered container environment."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SERVER = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is required")
class NtfyComposeTests(unittest.TestCase):
    def test_proxy_switch_defaults_off_and_reads_saved_env(self):
        version = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if version.returncode:
            self.skipTest("Docker Compose is required")
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            for source, target in (("docker-compose.yml", "docker-compose.yml"),
                                   ("viewer.env.example", "viewer.env"),
                                   ("monitor.env.example", "monitor.env.example")):
                shutil.copyfile(SERVER / source, root / target)
            env = dict(os.environ, INGEST_AUTH_TOKEN="synthetic-token", DISPLAY_TZ="UTC",
                       BIND_ADDR="", VIEWER_BIND_ADDR="", MONITOR_ENV_FILE="./monitor.env.example")
            env.pop("NTFY_BEHIND_PROXY", None)
            for setting, expected in (("", "false"), ("NTFY_BEHIND_PROXY=true\n", "true"),
                                      ("NTFY_BEHIND_PROXY=false\n", "false")):
                with self.subTest(setting=setting):
                    (root / ".env").write_text(setting, encoding="utf-8")
                    result = subprocess.run(["docker", "compose", "--profile", "ntfy",
                                             "config", "--format", "json"], cwd=root,
                                            env=env, capture_output=True, text=True, check=True)
                    services = json.loads(result.stdout)["services"]
                    self.assertEqual(services["ntfy"]["environment"]["NTFY_BEHIND_PROXY"], expected)
                    for name, service in services.items():
                        if name != "ntfy":
                            self.assertNotIn("NTFY_BEHIND_PROXY", service.get("environment", {}))
