"""Shared settings rules, real config classes, installer IPC and Compose quoting."""

import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_monitor import monitor


rules = monitor.settings_rules
SERVER = Path(__file__).resolve().parents[1]
# Every installer rule has valid, invalid and boundary samples. Credentials
# here are synthetic and are sent on stdin, never in process arguments.
SAMPLES = {
    "NTFY_TOPIC": [("test-topic_1", True), ("a" * 64, True), ("a" * 65, False), ("", False), ("bad.topic", False)],
    "NTFY_USER": [("test_user-1", True), ("a" * 64, True), ("a" * 65, False), ("", False), ("bad.user", False)],
    "NTFY_MODE": [("bundled", True), ("existing", True), ("Existing", False), ("", False)],
    "NOTIFY_MIN_SEVERITY": [(s, True) for s in rules.SEVERITIES] + [("ERROR", False), ("", False)],
    "LOKI_AUTH": [("none", True), ("basic", True), ("bearer", True), ("Basic", False), ("", False)],
    "MONITOR_STALE_DAYS": [("7", True), ("1.5", True), ("1e-4", True), (str(1 / 86400), True),
                           ("0.000001", False), ("36500", True), ("36500.1", False), ("nan", False), ("inf", False), ("0", False), ("\n7", False)],
    "NTFY_TOKEN": [("x", True), ("fake$#<>!'\\\"", True), ("<token>", False), ("", False), ("has space", False)],
    "METRICS_TOKEN": [("f" * 31, False), ("f" * 32, True), ("fake$#<>!'\\\"" + "f" * 32, True), ("f" * 32 + " ", False)],
    "LOKI_BEARER_TOKEN": [("x", True), ("fake$#<>!'\\\"", True), ("f" * 1024, True), ("f" * 1025, False),
                          ("<token>", False), ("", False), ("has space", False)],
    "LOKI_BASIC_AUTH_USER": [("fake user$#<>!'\\\"", True), ("f" * 1024, True), ("f" * 1025, False), ("fake:user", False), ("", False)],
    "LOKI_BASIC_AUTH_PASSWORD": [("fake password$#<>!'\\\":", True), (" ", True), ("f" * 1024, True), ("f" * 1025, False), ("", False)],
    "LOKI_TENANT_ID": [("", True), ("-", True), ("fake tenant$#<>!'\\\"", True), ("f" * 128, True), ("f" * 129, False)],
    "NTFY_URL": [("https://ntfy.invalid/prefix/test-topic", True), ("http://[::1]:8092/test-topic", True),
                 ("https://bad_host/test-topic", False), ("https://host.invalid/", False), ("https://host.invalid/t?q=1", False)],
    "LOKI_PUSH_URL": [("https://loki.invalid/prefix/loki/api/v1/push/", True), ("https://loki.invalid/loki/api/v1/push", True),
                      ("https://loki.invalid/push", False), ("https://user@loki.invalid/loki/api/v1/push", False)],
    "GRAYLOG_GELF_URL": [("https://graylog.invalid/gelf", True), ("HTTP://localhost:12201/gelf", True),
                         ("http://[::1]:12201/gelf", True), ("http://host.invalid:0/gelf", False), ("http://host.invalid:65536/gelf", False)],
    "GRAYLOG_GELF_TCP": [("graylog.invalid:12201", True), ("[::1]:65535", True), ("bad_host:12201", False),
                         ("[:::]:12201", False), ("host.invalid:0", False), ("host.invalid:65536", False)],
}
for flag in ("MONITOR_ENABLED", "NOTIFY_ENABLED", "METRICS_ENABLED", "GRAYLOG_ENABLED", "GRAYLOG_GELF_TLS", "GRAYLOG_GELF_TLS_VERIFY"):
    SAMPLES[flag] = [("true", True), ("FALSE", True), ("1", False), ("", False), (" true", False)]
SAMPLES.update({
    "NOTIFY_HOLD_SECONDS": [("0", True), ("604800", True), ("604801", False), ("-1", False), ("nan", False)],
    "NOTIFY_REPEAT_HOURS": [(str(1 / 3600), True), ("8760", True), ("8761", False), ("0", False)],
    "NOTIFY_MAX_PER_HOUR": [("1", True), ("1000", True), ("1001", False), ("0", False), ("1.0", False)],
})

WRAPPERS = {
    "NTFY_TOPIC": "valid_topic", "NTFY_USER": "valid_ntfy_user", "NTFY_MODE": "valid_ntfy_mode",
    "NOTIFY_MIN_SEVERITY": "valid_severity", "LOKI_AUTH": "valid_loki_auth", "MONITOR_STALE_DAYS": "valid_days",
    "NTFY_TOKEN": "valid_ntfy_token", "METRICS_TOKEN": "valid_metrics_token", "LOKI_BEARER_TOKEN": "valid_secret",
    "LOKI_BASIC_AUTH_USER": "valid_basic_user", "LOKI_BASIC_AUTH_PASSWORD": "valid_basic_password",
    "LOKI_TENANT_ID": "valid_tenant", "NTFY_URL": "valid_ntfy_url", "LOKI_PUSH_URL": "valid_loki_url",
    "GRAYLOG_GELF_URL": "valid_graylog_target", "GRAYLOG_GELF_TCP": "valid_graylog_target",
}


def configured(name, value):
    env = {"MONITOR_ENABLED": "true", "NOTIFY_ENABLED": "true", "NTFY_URL": "https://ntfy.invalid/test-topic",
           "NTFY_TOKEN": "fake-token", "GRAYLOG_ENABLED": "true", "GRAYLOG_GELF_URL": "https://graylog.invalid/gelf",
           "METRICS_ENABLED": "true", "METRICS_TOKEN": "f" * 32,
           "LOKI_PUSH_URL": "https://loki.invalid/loki/api/v1/push"}
    if name.startswith("LOKI_BASIC_AUTH_"):
        env.update(LOKI_BASIC_AUTH_USER="fake-user", LOKI_BASIC_AUTH_PASSWORD="fake-password")
    if name in ("GRAYLOG_GELF_TCP", "GRAYLOG_GELF_TLS"):
        env.pop("GRAYLOG_GELF_URL")
        env["GRAYLOG_GELF_TCP"] = "graylog.invalid:12201"
    env[name] = value
    return monitor.Config.from_env(env)


class MonitorSettingsTests(unittest.TestCase):
    def test_shared_host_validator_is_the_standalone_settings_rule(self):
        self.assertIs(monitor.shared.valid_host, rules.host_valid)

    def test_settings_checker_runs_without_shared_or_common_modules(self):
        result = subprocess.run([sys.executable, "-I", str(SERVER / "mileage-monitor/settings_rules.py")],
                                input=b"NTFY_MODE\0existing\0", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"+ready\0+yes\0")

    def test_samples_and_config_classes_use_same_rules(self):
        for name, samples in SAMPLES.items():
            for index, (value, expected) in enumerate(samples):
                with self.subTest(setting=name, sample=index):
                    self.assertEqual(rules.valid(name, value), expected)
                    # Mode/topic/user/auth are installer-only choices. Feature
                    # flags have cross-setting requirements tested separately.
                    if name in ("NTFY_MODE", "NTFY_TOPIC", "NTFY_USER", "LOKI_AUTH", "MONITOR_ENABLED"):
                        continue
                    try:
                        configured(name, value)
                        accepted = True
                    except ValueError:
                        accepted = False
                    self.assertEqual(accepted, expected or (name == "LOKI_BEARER_TOKEN" and not value))

    def test_control_characters_in_credentials_and_urls_fail(self):
        for name in ("NTFY_TOKEN", "METRICS_TOKEN", "LOKI_BEARER_TOKEN", "LOKI_BASIC_AUTH_USER",
                     "LOKI_BASIC_AUTH_PASSWORD", "LOKI_TENANT_ID", "NTFY_URL", "LOKI_PUSH_URL", "GRAYLOG_GELF_URL"):
            for char in ("\0", "\n", "\r", "\t", "\x1f", "\x7f", "é"):
                value = "https://host.invalid/" + char + "/test-topic" if name.endswith("URL") else "fake" * 9 + char
                if char == "é" and name.endswith("URL"):
                    continue  # Existing URL paths can contain non-ASCII characters.
                with self.subTest(setting=name, codepoint=ord(char)):
                    self.assertFalse(rules.valid(name, value))

    def test_disabled_outputs_and_cross_setting_requirements(self):
        self.assertEqual(monitor.Config.from_env({"NTFY_TOKEN": "<token>"}), monitor.Config())
        for env in ({"METRICS_ENABLED": "true", "METRICS_TOKEN": "f" * 32},
                    {"LOKI_PUSH_URL": "https://loki.invalid/loki/api/v1/push"},
                    {"MONITOR_ENABLED": "true", "LOKI_BASIC_AUTH_USER": "fake-user"}):
            with self.assertRaises(ValueError):
                monitor.Config.from_env(env)

    def test_env_quotes_round_trip_all_printable_ascii_and_combinations(self):
        values = ["", "!invalid", "+", "fake<token>", " space # comment ", "fake$VALUE${VALUE}$$"]
        chars = "'\\\"$#<> "
        values += ["".join(part) for size in (1, 2, 3) for part in itertools.product(chars, repeat=size)]
        values += ["".join(chr(number) for number in range(32, 127))]
        for index, value in enumerate(values):
            with self.subTest(sample=index):
                self.assertEqual(rules.env_value(rules.env_quote(value)), value)

    def test_ambiguous_handwritten_values_fail_without_exposing_them(self):
        for raw in ("'unterminated", '"unterminated', "$FAKE_SECRET", '"${FAKE_SECRET}"'):
            with self.assertRaises(ValueError) as caught:
                rules.env_value(raw)
            self.assertNotIn(raw, str(caught.exception))


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "needs a POSIX shell; run under WSL")
class InstallerSettingsTests(unittest.TestCase):
    def run_checker(self, container=False):
        samples = [(WRAPPERS.get(name, "@" + name), value, expected)
                   for name, values in SAMPLES.items() for value, expected in values]
        script = r'''
source <(sed '$d' "$INSTALL")
SCRIPT_DIR=$SERVER
trap settings_stop EXIT
if [[ $CONTAINER == yes ]]; then
    PYTHON_ROUTE=container
    docker() {
        printf '%s\0' "$*" >> "$CALLS"
        # Run the real checker, replacing only Docker's process/mount boundary.
        python3 "$SERVER/mileage-monitor/settings_rules.py"
    }
fi
settings_start
while IFS= read -r -d '' rule && IFS= read -r -d '' value; do
    if [[ $rule == @* ]]; then
        if settings_valid "${rule:1}" "$value"; then printf 'yes\n'; else printf 'no\n'; fi
    elif "$rule" "$value"; then printf 'yes\n'; else printf 'no\n'; fi
done
'''
        with tempfile.TemporaryDirectory() as temp:
            calls = Path(temp) / "calls"
            result = subprocess.run(["bash", "-c", script],
                input=b"".join((rule + "\0" + value + "\0").encode() for rule, value, _ in samples),
                env={**os.environ, "INSTALL": str(SERVER / "install.sh"), "SERVER": str(SERVER),
                     "CONTAINER": "yes" if container else "no", "CALLS": str(calls)},
                capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout.decode().splitlines(), ["yes" if expected else "no" for _, _, expected in samples])
            if container:
                commands = calls.read_text().split("\0")[:-1]
                self.assertEqual(len(commands), 1)
                self.assertIn("--network none --user 1000:1000", commands[0])
                self.assertIn("settings_rules.py:/work/settings_rules.py:ro", commands[0])
                self.assertNotIn("monitor.env", commands[0])
                for _, value, _ in samples:
                    if value.startswith("fake"):
                        self.assertNotIn(value, commands[0])

    def test_host_checker_uses_config_rules_for_every_sample(self):
        self.run_checker()

    def test_unraid_starts_one_checker_without_secrets_in_arguments(self):
        self.run_checker(container=True)

    def test_checker_failure_stops_without_echoing_input(self):
        result = subprocess.run(["bash", "-c", r'''
source <(sed '$d' "$INSTALL")
host_python() { return 1; }
trap settings_stop EXIT
IFS= read -r value
valid_ntfy_token "$value"
'''], input="fake-private-value\n", text=True, capture_output=True, timeout=10,
            env={**os.environ, "INSTALL": str(SERVER / "install.sh")})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("fake-private-value", result.stdout + result.stderr)
        self.assertIn("settings checker stopped", result.stdout + result.stderr)

    def test_installer_writes_and_keeps_punctuation_and_spaces(self):
        values = {
            "NTFY_TOKEN_OUT": "fake$#<>!'\\\"", "LOKI_USER_NEW": "fake user$#<>!'\\\"",
            "LOKI_PW_NEW": "fake password$#<>!'\\\"", "LOKI_TENANT_NEW": "fake tenant$#<>!'\\\"",
            "FAKE_METRICS": "fake$#<>!'\\\"" + "f" * 32,
        }
        keys = ("NTFY_TOKEN", "LOKI_BASIC_AUTH_USER", "LOKI_BASIC_AUTH_PASSWORD", "LOKI_TENANT_ID", "METRICS_TOKEN")
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(["bash", "-c", r'''
source <(sed '$d' "$INSTALL")
SCRIPT_DIR=$SERVER
trap settings_stop EXIT
MONITOR_FILE=$WORK/monitor.env
cp "$SERVER/monitor.env.example" "$MONITOR_FILE"
settings_start
while IFS= read -r -d '' name && IFS= read -r -d '' value; do printf -v "$name" '%s' "$value"; done
CHOSEN=(ntfy loki metrics)
STALE_DAYS_NEW=7 NTFY_SEVERITY_NEW=error
NTFY_URL_NEW=https://ntfy.invalid/test-topic
LOKI_URL_NEW=https://loki.invalid/loki/api/v1/push
LOKI_AUTH_NEW=basic
monitor_env_set "$MONITOR_FILE" METRICS_TOKEN "$FAKE_METRICS"
for iteration in 1 2; do
    write_monitor_settings
    for key in NTFY_TOKEN LOKI_BASIC_AUTH_USER LOKI_BASIC_AUTH_PASSWORD LOKI_TENANT_ID METRICS_TOKEN; do
        printf '%s\0' "$(mon_value "$MONITOR_FILE" "$key")"
    done
done
'''], input=b"".join((key + "\0" + value + "\0").encode() for key, value in values.items()),
                capture_output=True, timeout=30, env={**os.environ, "SERVER": str(SERVER), "WORK": temp,
                                                     "INSTALL": str(SERVER / "install.sh")})
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout.decode().split("\0")[:-1], list(values.values()) * 2)
            self.assertEqual((Path(temp) / "monitor.env").stat().st_mode & 0o777, 0o600)
            env = {key: rules.env_value(line.split("=", 1)[1]) for line in (Path(temp) / "monitor.env").read_text().splitlines()
                   if line and not line.startswith("#") for key in [line.split("=", 1)[0]]}
            config = monitor.Config.from_env(env)
            self.assertEqual([config.token, config.loki.basic_user, config.loki.basic_password, config.loki.tenant, config.metrics.token],
                             [env[key] for key in keys])

    def test_unreadable_env_fails_before_it_can_look_like_an_empty_default(self):
        for key, raw, hint in (("NTFY_TOKEN", '"unterminated', "could not be read safely"),
                               ("NTFY_TOKEN", "$FAKE_REFERENCE", "could not be read safely"),
                               ("MONITOR_ENABLED", "yes", "must be true or false")):
            with tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "monitor.env").write_text("MONITOR_ENABLED=false\n" + key + "=" + raw + "\n")
                result = subprocess.run(["bash", "-c", r'''
source <(sed '$d' "$INSTALL")
SCRIPT_DIR=$SERVER
trap settings_stop EXIT
MONITOR_FILE=$WORK/monitor.env
settings_start
monitor_env_readable
printf 'unexpected success\n'
'''], capture_output=True, text=True, timeout=10,
                    env={**os.environ, "INSTALL": str(SERVER / "install.sh"), "SERVER": str(SERVER), "WORK": temp})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("unexpected success", result.stdout)
                if key == "NTFY_TOKEN":
                    self.assertNotIn(raw, result.stdout + result.stderr)
                self.assertIn(hint, result.stderr)


class ComposeSettingsTests(unittest.TestCase):
    def test_installer_quoting_with_real_compose_env_file_parser(self):
        executable = os.environ.get("COMPOSE_SETTINGS_TEST_BINARY")
        command = [executable] if executable else ["docker", "compose"]
        if not shutil.which(command[0]):
            self.skipTest("Docker Compose not installed")
        binary = Path(shutil.which(command[0])).resolve()
        if os.name != "nt" and (binary.suffix == ".exe" or binary.with_suffix(".exe").is_file()):
            self.skipTest("Windows Docker CLI cannot read POSIX scratch paths; run this test on Windows")
        version = subprocess.run(command + ["version"], capture_output=True, timeout=15)
        if version.returncode:
            self.skipTest("Docker Compose unavailable")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            values = {f"FAKE_{index}": value for index, value in enumerate([
                "fake$VALUE${VALUE}$$", "fake#hash", "fake <angle> spaces ", "fake'apostrophe",
                'fake"double', "fake\\slash", "fake\\'quote", "fake\\", "fake$\\", "fake\\\"$#<> ",
            ])}
            values["FAKE_DOLLAR"] = "$"
            (folder / "monitor.env").write_text("".join(f"{key}={rules.env_quote(value)}\n" for key, value in values.items()), encoding="utf-8")
            (folder / "compose.yml").write_text("services:\n  checker:\n    image: scratch\n    env_file: monitor.env\n", encoding="utf-8")
            result = subprocess.run(command + ["-p", "settings-test", "-f", str(folder / "compose.yml"), "config", "--format", "json"],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)["services"]["checker"]["environment"]
            # Newer Compose renders dollars escaped for reuse as Compose YAML;
            # 2.26 emits literal values. Detect rendering with a known sentinel.
            if actual["FAKE_DOLLAR"] == "$$":
                actual = {key: value.replace("$$", "$") for key, value in actual.items()}
            self.assertEqual(actual, values)
