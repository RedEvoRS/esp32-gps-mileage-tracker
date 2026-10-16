"""install.sh's Notifications step (issue #137).

Sources the real install.sh functions (everything but the final `main`)
into bash, runs setup_outputs against a scratch server directory, and
replaces every external command with a fake: compose/docker (which record
what they were asked and what they were sent on stdin), curl (the metrics
endpoint, which checks the bearer token it's given), and ss/ip. The fakes
also count test notifications, so a test can see whether anything was sent
to a phone, Graylog or Loki. Needs bash, openssl and awk, so it skips on
Windows.
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

METRICS_TOKEN = "m" * 32 + "0123456789abcdef"
NTFY_TOKEN = "tk_fakeTOKEN12345"
PHONE_PASSWORD = "correct-horse-1"
LOKI_BEARER = "bearer-token-value-1234"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=${FAKE_MODE:-install}
    SCRIPT_DIR=$WORK
    cd "$WORK"
    BIND_ADDR=${FAKE_BIND-}
    DOCKER=(docker)

    # Docker: every inspect answers with the state the test set.
    docker() {
        case $1 in
            inspect) printf '%s\\n' "${FAKE_STATUS:-running}" ;;
        esac
        return 0
    }

    # Compose: services listed in FAKE_RUNNING count as running; the rest
    # records the call and answers what the test scripted.
    compose() {
        local a script='' next=''
        printf '%s\\n' "$*" >> "$WORK/compose.log"
        for a; do
            if [[ $next == 1 ]]; then script=$a; next=; fi
            if [[ $a == -c ]]; then next=1; fi
        done
        case "$*" in
            *" ps -a -q "*)
                if [[ " ${FAKE_RUNNING-} " == *" ${!#} "* ]]; then printf 'cid-%s\\n' "${!#}"; fi
                return 0 ;;
            *" logs "*)
                if [[ -n ${FAKE_LOGS+set} && ( ! -f $WORK/upped || -n ${FAKE_LOGS_ALWAYS-} ) ]]; then
                    printf '%s\\n' "$FAKE_LOGS"
                else
                    startup_line
                fi
                return 0 ;;
            *"exec -T ntfy sh -c"*) cat > "$WORK/stdin.log"; return 0 ;;
            *"exec -T ntfy ntfy user list"*) printf '%s\\n' "${FAKE_USERS-}"; return 0 ;;
            *"exec -T ntfy ntfy token add"*)
                printf 'Created token %s for user\\n' "$FAKE_NTFY_TOKEN"; return 0 ;;
            *"exec -T ntfy ntfy access"*) return 0 ;;
            *"up -d ntfy"*) return 0 ;;
            *"up -d --build"*) touch "$WORK/upped"; return "${FAKE_UP_RC:-0}" ;;
            *" stop "*) return 0 ;;
            *"exec -T mileage-monitor python -c"*) python_fake "$script"; return ;;
        esac
        return 0
    }

    # The line monitor.py logs at start, derived from the file the test wrote.
    startup_line() {
        grep -q '^MONITOR_ENABLED=true' "$WORK/monitor.env" 2> /dev/null || return 0
        local flag
        flag() { if grep -q "^$1=true" "$WORK/monitor.env" 2> /dev/null; then printf enabled; else printf disabled; fi; }
        local loki=disabled
        if grep -Eq '^LOKI_PUSH_URL=[^[:space:]]' "$WORK/monitor.env" 2> /dev/null; then loki=enabled; fi
        printf 'mileage-monitor  | 2026-10-16 INFO Monitor enabled; ntfy %s, Graylog %s, Prometheus %s, Loki %s\\n' \\
            "$(flag NOTIFY_ENABLED)" "$(flag GRAYLOG_ENABLED)" "$(flag METRICS_ENABLED)" "$loki"
    }

    # The Python run inside the monitor container, told apart by its text.
    python_fake() {
        case $1 in
            *"poll=1"*)
                printf '%s\n' "${FAKE_ANON_OUT:-HTTP 401}"; return "${FAKE_ANON_RC:-0}" ;;
            *"socket.create_connection"*)
                if (( ${FAKE_REACH_RC:-0} )); then printf 'OSError\\n'; fi
                return "${FAKE_REACH_RC:-0}" ;;
            *"NTFY_TOKEN"*)
                printf 'ntfy-test\\n' >> "$WORK/sends.log"
                printf '%s\\n' "${FAKE_NTFY_OUT:-HTTP 200}"; return "${FAKE_NTFY_RC:-0}" ;;
            *"GelfConfig"*)
                printf 'gelf-test\\n' >> "$WORK/sends.log"
                printf '%s\\n' "${FAKE_GRAYLOG_OUT:-accepted}"; return "${FAKE_GRAYLOG_RC:-0}" ;;
            *"LokiConfig"*)
                printf 'loki-test\\n' >> "$WORK/sends.log"
                printf '%s\\n' "${FAKE_LOKI_OUT:-accepted}"; return "${FAKE_LOKI_RC:-0}" ;;
            *"urlopen"*) return "${FAKE_HEALTH_RC:-0}" ;;
        esac
        return 0
    }

    # The metrics endpoint: 401 without the token, 200 with the METRICS_TOKEN
    # the monitor was given in monitor.env.
    curl() {
        local out='' prev='' arg header='' expected=''
        printf '%s\\n' "$*" >> "$WORK/curl.log"
        for arg; do
            if [[ $prev == -o ]]; then out=$arg; fi
            if [[ $arg == @* ]]; then header=$(cat "${arg#@}"); fi
            prev=$arg
        done
        expected=$(grep '^METRICS_TOKEN=' "$WORK/monitor.env" 2> /dev/null | tail -n 1 | cut -d= -f2-) || true
        if [[ -n $expected && $header == *"Bearer $expected"* ]]; then
            if [[ -n $out ]]; then printf 'mileage_tracker_db_read_errors_total 0\\n' > "$out"; fi
            printf 200
        else
            if [[ -n $out ]]; then printf 'Unauthorized\\n' > "$out"; fi
            printf 401
        fi
    }

    ss() { return 0; }
    ip() { return 1; }

    setup_outputs
    printf 'FAILURES=%s\\n' "$FAILURES"
    """)

PRESET_PREFIX = "INSTALL_"


def tools_missing():
    if os.name == "nt":
        return "needs a POSIX shell"
    for tool in ("bash", "awk", "sed", "openssl"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    return None


@unittest.skipIf(tools_missing(), tools_missing())
class InstallerOutputs(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        (self.work / "grafana").mkdir()
        # Unix line endings, as preflight converts a Windows checkout to.
        (self.work / "ntfy").mkdir()
        for source, target in (("monitor.env.example", "monitor.env.example"),
                               ("grafana/prometheus-scrape.yml", "grafana/prometheus-scrape.yml"),
                               ("ntfy/server.yml", "ntfy/server.yml")):
            text = (SERVER / source).read_text(encoding="utf-8")
            (self.work / target).write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        self.env_file = self.work / ".env"
        self.env_file.write_text("INGEST_AUTH_TOKEN=" + "a" * 64 + "\nBIND_ADDR=\nDISPLAY_TZ=UTC\n")
        os.chmod(self.env_file, 0o600)

    def run_step(self, answers="", mode="install", drop=(), **fakes):
        env = {key: value for key, value in os.environ.items() if not key.startswith(PRESET_PREFIX)}
        # Answers the tests don't care about, so no question waits for input.
        env.update({"INSTALL": str(INSTALL), "WORK": str(self.work), "FAKE_MODE": mode,
                    "FAKE_RUNNING": "mileage-monitor ntfy", "FAKE_NTFY_TOKEN": NTFY_TOKEN,
                    "INSTALL_STALE_DAYS": "7", "INSTALL_NOTIFY_MIN_SEVERITY": "error",
                    "INSTALL_LOKI_TENANT_ID": ""})
        env.update(fakes)
        for key in drop:
            env.pop(key, None)
        return subprocess.run(["bash", "-c", HARNESS], input=answers, env=env, cwd=self.work,
                              capture_output=True, text=True, timeout=60)

    def text(self, name):
        return (self.work / name).read_text(encoding="utf-8")

    def setting(self, key, name="monitor.env"):
        values = [line.split("=", 1)[1] for line in self.text(name).splitlines()
                  if line.startswith(key + "=")]
        return values[-1] if values else None

    def env_lines(self):
        return [line for line in self.env_file.read_text(encoding="utf-8").splitlines() if line]

    def sent(self):
        path = self.work / "sends.log"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def compose_log(self):
        path = self.work / "compose.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def assert_no_secret(self, result, *secrets):
        for secret in secrets:
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_none_writes_a_disabled_monitor_env_and_one_pointer(self):
        result = self.run_step(INSTALL_OUTPUTS="none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Notification outputs: off", result.stdout)
        self.assertEqual(self.setting("MONITOR_ENABLED"), "false")
        self.assertEqual(self.setting("NOTIFY_ENABLED"), "false")
        self.assertEqual(oct((self.work / "monitor.env").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.setting("MONITOR_ENV_FILE", ".env"), "./monitor.env")
        added = set(self.env_lines()) - {"INGEST_AUTH_TOKEN=" + "a" * 64, "BIND_ADDR=", "DISPLAY_TZ=UTC"}
        self.assertEqual(added, {"MONITOR_ENV_FILE=./monitor.env"})
        self.assertFalse((self.work / "viewer.env").exists())
        self.assertNotIn("up -d --build", self.compose_log())

    def test_metrics_only_by_environment_variables(self):
        result = self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("METRICS_ENABLED"), "true")
        self.assertEqual(self.setting("MONITOR_ENABLED"), "true")
        self.assertEqual(self.setting("MONITOR_STALE_DAYS"), "7")
        self.assertEqual(self.setting("NOTIFY_ENABLED"), "false")
        token = self.setting("METRICS_TOKEN")
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertNotIn("METRICS_TOKEN", self.text(".env"))
        self.assertIn("scrape_configs", result.stdout)
        self.assertNotIn("<host>", result.stdout)
        self.assertIn("Metrics refuse a request without the token (401)", result.stdout)
        self.assertIn("Metrics answer with the token (200)", result.stdout)
        self.assertIn("up -d --build --no-deps mileage-monitor", self.compose_log())
        self.assertNotIn("ntfy", self.compose_log().split("up -d --build")[1].split("\n")[0])
        self.assert_no_secret(result, token)

    def test_ntfy_existing_and_loki_answers_and_their_tests(self):
        result = self.run_step(
            INSTALL_OUTPUTS="ntfy, loki",
            INSTALL_NTFY_MODE="existing",
            INSTALL_NTFY_URL="https://ntfy.invalid/mileage-tracker",
            INSTALL_NTFY_TOKEN=NTFY_TOKEN,
            INSTALL_NTFY_READY="y",
            INSTALL_NTFY_ARRIVED="y",
            INSTALL_STALE_DAYS="14",
            INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push",
            INSTALL_LOKI_AUTH="bearer",
            INSTALL_LOKI_BEARER_TOKEN=LOKI_BEARER,
            INSTALL_LOKI_TENANT_ID="tenant-a",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("NOTIFY_ENABLED"), "true")
        self.assertEqual(self.setting("NTFY_URL"), "https://ntfy.invalid/mileage-tracker")
        self.assertEqual(self.setting("NTFY_TOKEN"), NTFY_TOKEN)
        self.assertEqual(self.setting("LOKI_PUSH_URL"), "https://loki.invalid/loki/api/v1/push")
        self.assertEqual(self.setting("LOKI_BEARER_TOKEN"), f"'{LOKI_BEARER}'")
        self.assertEqual(self.setting("LOKI_TENANT_ID"), "'tenant-a'")
        self.assertEqual(self.setting("LOKI_BASIC_AUTH_USER"), "")
        self.assertEqual(self.setting("GRAYLOG_ENABLED"), "false")
        self.assertEqual(self.setting("METRICS_ENABLED"), "false")
        self.assertEqual(self.sent(), ["ntfy-test", "loki-test"])
        self.assertIn("Did it arrive", result.stdout)
        self.assertIn("accepted", result.stdout)
        # ntfy and Loki secrets reach the file, never the output or the .env.
        self.assert_no_secret(result, NTFY_TOKEN, LOKI_BEARER)
        self.assertEqual(self.env_lines()[-1], "MONITOR_ENV_FILE=./monitor.env")
        self.assertFalse(any("NTFY" in line or "LOKI" in line for line in self.env_lines()))

    def test_bundled_ntfy_creates_the_user_with_the_password_on_stdin(self):
        result = self.run_step(
            INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled", INSTALL_NTFY_TOPIC="mileage-tracker",
            INSTALL_NTFY_USER="mileage", INSTALL_NTFY_PASSWORD=PHONE_PASSWORD,
            INSTALL_NTFY_READY="y", INSTALL_NTFY_ARRIVED="y", FAKE_USERS="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "stdin.log").read_text(encoding="utf-8").strip(), PHONE_PASSWORD)
        log = self.compose_log()
        self.assertNotIn(PHONE_PASSWORD, log)
        self.assertIn("up -d ntfy", log)
        self.assertIn("exec -T ntfy ntfy access mileage mileage-tracker rw", log)
        self.assertEqual(self.setting("NTFY_URL"), "http://ntfy:80/mileage-tracker")
        self.assertEqual(self.setting("NTFY_TOKEN"), NTFY_TOKEN)
        self.assertIn("Phone setup (one time)", result.stdout)
        self.assert_no_secret(result, PHONE_PASSWORD, NTFY_TOKEN)

    def test_bundled_rerun_keeps_the_user_password_and_token(self):
        setup = self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled", INSTALL_NTFY_TOPIC="mileage-tracker",
                              INSTALL_NTFY_USER="mileage", INSTALL_NTFY_PASSWORD=PHONE_PASSWORD,
                              INSTALL_NTFY_READY="n", FAKE_USERS="")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        (self.work / "compose.log").unlink()
        result = self.run_step(
            INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled",
            INSTALL_NTFY_TOPIC="mileage-tracker", INSTALL_NTFY_USER="mileage", INSTALL_NTFY_PASSWORD="",
            INSTALL_NTFY_READY="n",
            FAKE_USERS="user mileage (role: user, tier: none)", FAKE_NTFY_TOKEN="tk_newNOTUSED999")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.compose_log()
        self.assertNotIn("token add", log)
        self.assertNotIn("user add", log)
        self.assertNotIn("change-pass", log)
        self.assertIn("exec -T ntfy ntfy access mileage mileage-tracker rw", log)
        self.assertEqual(self.setting("NTFY_TOKEN"), NTFY_TOKEN)
        self.assertIn("Skipped the ntfy test", result.stdout)
        self.assert_no_secret(result, NTFY_TOKEN, PHONE_PASSWORD)

    def test_keep_yes_applies_without_asking_or_sending(self):
        self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        before = {name: self.text(name) for name in ("monitor.env", ".env")}
        (self.work / "compose.log").unlink()
        result = self.run_step(INSTALL_KEEP_OUTPUTS="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Notification outputs now: metrics", result.stdout)
        self.assertNotIn("Outputs to turn on", result.stdout)
        self.assertNotIn("silence alert", result.stdout)
        self.assertEqual({name: self.text(name) for name in before}, before)
        self.assertIn("up -d --build --no-deps mileage-monitor", self.compose_log())
        self.assertNotIn("up -d ntfy", self.compose_log())
        self.assertIn("Metrics answer with the token (200)", result.stdout)
        self.assertEqual(self.sent(), [])

    def test_keep_yes_starts_the_bundled_ntfy_too(self):
        setup = self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled", INSTALL_NTFY_TOPIC="mileage-tracker",
                              INSTALL_NTFY_USER="mileage", INSTALL_NTFY_PASSWORD=PHONE_PASSWORD,
                              INSTALL_NTFY_READY="n", FAKE_USERS="")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        (self.work / "compose.log").unlink()
        result = self.run_step(INSTALL_KEEP_OUTPUTS="y", FAKE_USERS="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.compose_log()
        self.assertIn("up -d ntfy", log)
        self.assertIn("up -d --build --no-deps mileage-monitor", log)
        self.assertNotIn("user add", log)
        self.assertEqual(self.sent(), [])

    def test_keep_yes_recreates_the_monitor_after_a_hand_edit(self):
        # The monitor is running with the settings from before the owner added
        # Graylog by hand. Keeping the outputs must apply that, not report it.
        self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        with (self.work / "monitor.env").open("a", encoding="utf-8") as handle:
            handle.write("GRAYLOG_ENABLED=true\nGRAYLOG_GELF_URL=https://graylog.invalid/gelf\n")
        (self.work / "upped").unlink(missing_ok=True)
        (self.work / "compose.log").unlink()
        result = self.run_step(
            INSTALL_KEEP_OUTPUTS="y",
            FAKE_LOGS="mileage-monitor  | 2026-10-16 INFO Monitor enabled; ntfy disabled, Graylog disabled, "
                      "Prometheus enabled, Loki disabled")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("other settings than monitor.env", result.stdout)
        self.assertIn("The monitor is running with the settings in monitor.env", result.stdout)
        self.assertIn("up -d --build --no-deps mileage-monitor", self.compose_log())
        self.assertEqual(self.sent(), [])

    def test_keep_yes_with_outputs_none_stops_a_running_monitor_and_starts_nothing(self):
        self.run_step(INSTALL_OUTPUTS="none")
        (self.work / "compose.log").unlink()
        result = self.run_step(INSTALL_KEEP_OUTPUTS="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Notification outputs: off", result.stdout)
        log = self.compose_log()
        self.assertNotIn("up -d", log)
        self.assertIn("stop mileage-monitor", log)
        self.assertEqual(self.sent(), [])

    def test_keep_no_with_empty_secrets_keeps_the_existing_ones(self):
        self.run_step(INSTALL_OUTPUTS="loki,metrics", INSTALL_STALE_DAYS="7",
                      INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push",
                      INSTALL_LOKI_AUTH="bearer", INSTALL_LOKI_BEARER_TOKEN=LOKI_BEARER)
        token_before = self.setting("METRICS_TOKEN")
        result = self.run_step(
            INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="loki,metrics", INSTALL_STALE_DAYS="7",
            INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push", INSTALL_LOKI_AUTH="bearer",
            INSTALL_LOKI_BEARER_TOKEN="", INSTALL_LOKI_TENANT_ID="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("LOKI_BEARER_TOKEN"), f"'{LOKI_BEARER}'")
        self.assertEqual(self.setting("METRICS_TOKEN"), token_before)
        self.assert_no_secret(result, LOKI_BEARER, token_before)

    def test_metrics_token_is_not_replaced_when_valid(self):
        self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        first = self.setting("METRICS_TOKEN")
        self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        self.assertEqual(self.setting("METRICS_TOKEN"), first)

    def test_monitor_env_is_mode_600_even_when_it_was_not(self):
        (self.work / "monitor.env").write_text("MONITOR_ENABLED=false\n")
        os.chmod(self.work / "monitor.env", 0o644)
        self.env_file.write_text(self.env_file.read_text() + "MONITOR_ENV_FILE=./monitor.env\n")
        result = self.run_step(INSTALL_KEEP_OUTPUTS="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(oct((self.work / "monitor.env").stat().st_mode & 0o777), "0o600")

    def test_turning_an_output_off_keeps_its_values_but_loki_is_commented(self):
        self.run_step(INSTALL_OUTPUTS="ntfy,loki", INSTALL_NTFY_MODE="existing",
                      INSTALL_NTFY_URL="https://ntfy.invalid/mileage-tracker", INSTALL_NTFY_TOKEN=NTFY_TOKEN,
                      INSTALL_NTFY_READY="n", INSTALL_STALE_DAYS="7",
                      INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push", INSTALL_LOKI_AUTH="bearer",
                      INSTALL_LOKI_BEARER_TOKEN=LOKI_BEARER)
        result = self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="existing",
                               INSTALL_NTFY_URL="https://ntfy.invalid/mileage-tracker", INSTALL_NTFY_TOKEN="",
                               INSTALL_NTFY_READY="n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("NOTIFY_ENABLED"), "true")
        self.assertEqual(self.setting("NTFY_TOKEN"), NTFY_TOKEN)
        self.assertIsNone(self.setting("LOKI_PUSH_URL"))
        self.assertIn(f"#LOKI_BEARER_TOKEN='{LOKI_BEARER}'", self.text("monitor.env"))
        self.assert_no_secret(result, LOKI_BEARER)

    def test_graylog_tcp_drops_a_header_that_only_applies_to_http(self):
        (self.work / "monitor.env").write_text(
            (self.work / "monitor.env.example").read_text(encoding="utf-8")
            + "GRAYLOG_GELF_HEADER=X-Proxy-Auth: header-secret-7\n")
        result = self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="graylog",
                               INSTALL_GRAYLOG_TARGET="graylog.invalid:12201",
                               INSTALL_GRAYLOG_ARRIVED="y", INSTALL_STALE_DAYS="7")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("GRAYLOG_GELF_TCP"), "graylog.invalid:12201")
        self.assertEqual(self.setting("GRAYLOG_GELF_URL"), "")
        self.assertIn("#GRAYLOG_GELF_HEADER=X-Proxy-Auth: header-secret-7", self.text("monitor.env"))
        self.assertIn("GRAYLOG_GELF_HEADER was commented out", result.stdout)
        self.assertEqual(self.sent(), ["gelf-test"])
        self.assert_no_secret(result, "header-secret-7")

    def test_loki_switching_auth_type_blanks_the_other_kind(self):
        self.run_step(INSTALL_OUTPUTS="loki", INSTALL_STALE_DAYS="7",
                      INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push", INSTALL_LOKI_AUTH="basic",
                      INSTALL_LOKI_BASIC_AUTH_USER="pusher", INSTALL_LOKI_BASIC_AUTH_PASSWORD="basic-pass-99")
        result = self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="loki", INSTALL_STALE_DAYS="7",
                               INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push",
                               INSTALL_LOKI_AUTH="bearer", INSTALL_LOKI_BEARER_TOKEN=LOKI_BEARER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("LOKI_BEARER_TOKEN"), f"'{LOKI_BEARER}'")
        self.assertEqual(self.setting("LOKI_BASIC_AUTH_USER"), "")
        self.assertEqual(self.setting("LOKI_BASIC_AUTH_PASSWORD"), "")
        self.assert_no_secret(result, "basic-pass-99")

    def test_a_failed_ntfy_test_is_a_problem(self):
        result = self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="existing",
                               INSTALL_NTFY_URL="https://ntfy.invalid/mileage-tracker",
                               INSTALL_NTFY_TOKEN=NTFY_TOKEN, INSTALL_NTFY_READY="y",
                               FAKE_NTFY_OUT="HTTP 403", FAKE_NTFY_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("ntfy didn't accept the test notification (HTTP 403)", result.stdout)
        self.assert_no_secret(result, NTFY_TOKEN)

    def test_monitor_refusing_its_settings_stops_with_the_log_line(self):
        result = self.run_step(
            INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7", FAKE_LOGS_ALWAYS="1",
            FAKE_LOGS="mileage-monitor | ERROR Invalid monitor configuration: NTFY_URL must be a topic URL")
        self.assertEqual(result.returncode, 1)
        self.assertIn("The monitor refused the settings in monitor.env", result.stdout)
        self.assertIn("Invalid monitor configuration: NTFY_URL must be a topic URL", result.stdout)

    def test_invalid_presets_stop_with_a_clear_message(self):
        cases = [
            (dict(INSTALL_OUTPUTS="bogus"), "INSTALL_OUTPUTS=bogus isn't a list of outputs."),
            (dict(INSTALL_OUTPUTS="none,metrics"), "INSTALL_OUTPUTS=none,metrics isn't a list of outputs."),
            (dict(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="0"),
             "INSTALL_STALE_DAYS=0 isn't usable."),
            (dict(INSTALL_OUTPUTS="loki", INSTALL_STALE_DAYS="7",
                  INSTALL_LOKI_URL="https://loki.invalid/push"),
             "INSTALL_LOKI_URL=https://loki.invalid/push isn't usable."),
            (dict(INSTALL_OUTPUTS="loki", INSTALL_STALE_DAYS="7",
                  INSTALL_LOKI_URL="https://loki.invalid/loki/api/v1/push", INSTALL_LOKI_AUTH="bearer",
                  INSTALL_LOKI_BEARER_TOKEN="has'quote"),
             "INSTALL_LOKI_BEARER_TOKEN isn't usable."),
        ]
        for presets, message in cases:
            with self.subTest(presets=presets):
                # Each case starts from a fresh server directory, as a first install does.
                (self.work / "monitor.env").unlink(missing_ok=True)
                self.env_file.write_text("INGEST_AUTH_TOKEN=" + "a" * 64 + "\nBIND_ADDR=\nDISPLAY_TZ=UTC\n")
                result = self.run_step(**presets)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)

    def test_check_with_everything_off_passes_and_says_off(self):
        result = self.run_step(mode="check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Notification outputs: off", result.stdout)
        self.assertNotIn("  FAIL  ", result.stdout)
        self.assertNotIn("  warn  ", result.stdout)
        self.assertIn("FAILURES=0", result.stdout)

    def test_check_with_outputs_off_in_a_configured_file_is_off_too(self):
        self.run_step(INSTALL_OUTPUTS="none")
        result = self.run_step(mode="check")
        self.assertIn("Notification outputs: off", result.stdout)
        self.assertIn("FAILURES=0", result.stdout)

    def test_check_verifies_outputs_and_never_changes_or_sends_anything(self):
        self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="ntfy,metrics", INSTALL_NTFY_MODE="existing",
                      INSTALL_NTFY_URL="https://ntfy.invalid/mileage-tracker", INSTALL_NTFY_TOKEN=NTFY_TOKEN,
                      INSTALL_NTFY_READY="n", INSTALL_STALE_DAYS="7")
        before = {name: self.text(name) for name in ("monitor.env", ".env")}
        (self.work / "compose.log").unlink()
        result = self.run_step(mode="check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Metrics answer with the token (200)", result.stdout)
        self.assertIn("ntfy server", result.stdout)
        self.assertEqual({name: self.text(name) for name in before}, before)
        self.assertEqual(self.sent(), [])
        log = self.compose_log()
        for action in ("up -d", " stop ", "user add", "token add", "access "):
            self.assertNotIn(action, log)
        self.assertFalse((self.work / "stdin.log").exists())

    def test_graylog_url_comments_out_a_tls_setting_that_only_applies_to_tcp(self):
        (self.work / "monitor.env").write_text(
            (self.work / "monitor.env.example").read_text(encoding="utf-8")
            + "GRAYLOG_GELF_TLS=true\nGRAYLOG_GELF_TCP=old.invalid:12201\n")
        result = self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="graylog",
                               INSTALL_GRAYLOG_TARGET="https://graylog.invalid/gelf",
                               INSTALL_GRAYLOG_ARRIVED="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("GRAYLOG_GELF_URL"), "https://graylog.invalid/gelf")
        self.assertEqual(self.setting("GRAYLOG_GELF_TCP"), "")
        self.assertIn("#GRAYLOG_GELF_TLS=true", self.text("monitor.env"))
        self.assertIn("GRAYLOG_GELF_TLS=true was commented out", result.stdout)

    def test_bundled_ntfy_password_change_for_an_existing_user(self):
        setup = self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled",
                              INSTALL_NTFY_TOPIC="mileage-tracker", INSTALL_NTFY_USER="mileage",
                              INSTALL_NTFY_PASSWORD=PHONE_PASSWORD, INSTALL_NTFY_READY="n", FAKE_USERS="")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        result = self.run_step(INSTALL_KEEP_OUTPUTS="n", INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled",
                               INSTALL_NTFY_TOPIC="mileage-tracker", INSTALL_NTFY_USER="mileage",
                               INSTALL_NTFY_PASSWORD="a-new-phone-password", INSTALL_NTFY_READY="n",
                               FAKE_USERS="user mileage (role: user, tier: none)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.work / "stdin.log").read_text(encoding="utf-8").strip(), "a-new-phone-password")
        log = self.compose_log()
        self.assertIn("change-pass", log)
        self.assertNotIn("a-new-phone-password", log)
        self.assertNotIn("a-new-phone-password", result.stdout + result.stderr)

    def test_check_verifies_the_bundled_ntfy(self):
        self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled", INSTALL_NTFY_TOPIC="mileage-tracker",
                      INSTALL_NTFY_USER="mileage", INSTALL_NTFY_PASSWORD=PHONE_PASSWORD,
                      INSTALL_NTFY_READY="n", FAKE_USERS="")
        result = self.run_step(mode="check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("The bundled ntfy is running", result.stdout)
        self.assertIn("The bundled ntfy's health check passes from the monitor", result.stdout)
        self.assertEqual(self.sent(), [])
        broken = self.run_step(mode="check", FAKE_HEALTH_RC="1")
        self.assertIn("The bundled ntfy's health check fails from the monitor", broken.stdout)
        self.assertIn("FAILURES=1", broken.stdout)

    def test_check_reports_a_monitor_that_is_not_running(self):
        self.run_step(INSTALL_OUTPUTS="metrics", INSTALL_STALE_DAYS="7")
        result = self.run_step(mode="check", FAKE_RUNNING="")
        self.assertIn("FAIL", result.stdout)
        self.assertIn("The monitor isn't running", result.stdout)
        self.assertIn("FAILURES=1", result.stdout)

    def test_check_reports_an_unreachable_graylog(self):
        self.run_step(INSTALL_OUTPUTS="graylog", INSTALL_GRAYLOG_TARGET="graylog.invalid:12201",
                      INSTALL_GRAYLOG_ARRIVED="y", INSTALL_STALE_DAYS="7")
        result = self.run_step(mode="check", FAKE_REACH_RC="1")
        self.assertIn("The monitor can't connect to the Graylog input", result.stdout)
        self.assertIn("FAILURES=1", result.stdout)
        self.assertEqual(self.sent(), ["gelf-test"])


    def test_a_fresh_install_offers_bundled_and_shows_no_placeholder(self):
        # The example's <host>, <topic> and <token> values must not become
        # defaults. Every answer comes from stdin, in prompt order: mode, topic,
        # user, password, password again, severity, phone ready, arrived.
        answers = "\n\n\n" + f"{PHONE_PASSWORD}\n{PHONE_PASSWORD}\n" + "\n\n\n"
        result = self.run_step(
            answers=answers, INSTALL_OUTPUTS="ntfy", FAKE_USERS="",
            drop=("INSTALL_NTFY_MODE", "INSTALL_NTFY_TOPIC", "INSTALL_NTFY_USER", "INSTALL_NTFY_PASSWORD",
                  "INSTALL_NOTIFY_MIN_SEVERITY", "INSTALL_NTFY_READY", "INSTALL_NTFY_ARRIVED",
                  "INSTALL_NTFY_URL", "INSTALL_NTFY_TOKEN"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[bundled]", result.stdout)
        self.assertNotIn("[existing]", result.stdout)
        self.assertNotIn("<host>", result.stdout)
        self.assertNotIn("<token>", result.stdout)
        self.assertNotIn("<topic>", result.stdout)
        self.assertNotIn("Enter keeps the current one", result.stdout)
        self.assertEqual(self.setting("NTFY_URL"), "http://ntfy:80/mileage-tracker")

    def test_the_graylog_question_offers_no_placeholder(self):
        result = self.run_step(answers="\n", drop=("INSTALL_GRAYLOG_TARGET",), INSTALL_OUTPUTS="graylog")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("GELF HTTP URL", result.stdout)
        self.assertNotIn("<host>", result.stdout)
        self.assertNotIn("[http", result.stdout)

    def test_bundled_ntfy_must_refuse_anonymous_reads(self):
        setup = self.run_step(INSTALL_OUTPUTS="ntfy", INSTALL_NTFY_MODE="bundled",
                              INSTALL_NTFY_TOPIC="mileage-tracker", INSTALL_NTFY_USER="mileage",
                              INSTALL_NTFY_PASSWORD=PHONE_PASSWORD, INSTALL_NTFY_READY="n", FAKE_USERS="")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        passed = self.run_step(mode="check")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("The bundled ntfy refuses anonymous reads (deny-all)", passed.stdout)
        self.assertIn("FAILURES=0", passed.stdout)

        leaked = self.run_step(mode="check", FAKE_ANON_RC="1", FAKE_ANON_OUT="HTTP 200 (anonymous read allowed)")
        self.assertIn("The bundled ntfy answers anonymous reads", leaked.stdout)
        self.assertIn("HTTP 200 (anonymous read allowed)", leaked.stdout)
        self.assertIn("FAILURES=1", leaked.stdout)

        config = self.work / "ntfy" / "server.yml"
        config.write_text(config.read_text(encoding="utf-8").replace('"deny-all"', '"allow-all"'), encoding="utf-8")
        open_config = self.run_step(mode="check")
        self.assertIn("ntfy/server.yml doesn't set auth-default-access to deny-all", open_config.stdout)
        self.assertIn("FAILURES=1", open_config.stdout)
        self.assertEqual(self.sent(), [])

    def test_monitor_env_values_lose_one_matching_pair_of_quotes(self):
        (self.work / "monitor.env").write_text(textwrap.dedent("""\
            MONITOR_ENABLED="true"
            NOTIFY_ENABLED='true'
            NTFY_URL="http://ntfy:80/x"
            NTFY_TOKEN=plain
            A="it's"
            B='"quoted"'
            C="mismatched'
            D=<host>
            E="<token>"
            F=""
            """), encoding="utf-8")
        snippet = textwrap.dedent("""\
            source <(sed '$d' "$INSTALL")
            SCRIPT_DIR=$WORK
            cd "$WORK"
            MONITOR_FILE=$WORK/monitor.env
            for key in MONITOR_ENABLED NOTIFY_ENABLED NTFY_URL NTFY_TOKEN A B C D E F; do
                printf '%s=[%s]\\n' "$key" "$(mon_value "$MONITOR_FILE" "$key")"
            done
            output_on ntfy && echo ntfy-on
            """)
        env = {**os.environ, "INSTALL": str(INSTALL), "WORK": str(self.work)}
        result = subprocess.run(["bash", "-c", snippet], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "MONITOR_ENABLED=[true]", "NOTIFY_ENABLED=[true]", "NTFY_URL=[http://ntfy:80/x]",
            "NTFY_TOKEN=[plain]", "A=[it's]", 'B=["quoted"]', "C=[\"mismatched']",
            "D=[]", "E=[]", "F=[]", "ntfy-on"])


if __name__ == "__main__":
    unittest.main()
