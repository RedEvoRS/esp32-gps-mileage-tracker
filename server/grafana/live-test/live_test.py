"""Test server/grafana against real Prometheus, Loki and Grafana in Docker.

Usage: python live_test.py [--keep]   (needs Docker; takes about 6 minutes)
--keep leaves the stack running, to look at Grafana on http://127.0.0.1:13000
(admin / live-test-admin); remove it later with "docker compose down -v --rmi local".

Starts the throwaway stack in docker-compose.yml, feeds the real ingest a fake
device ("tbeam-test"), then checks the monitor's /metrics output, the
Prometheus config and alert rules, the Loki lines and every LogQL example in
loki-alerts.md, and both Grafana dashboards. It removes all containers,
networks and volumes it created. Exit status 0 means every check passed.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRAFANA_DIR = HERE.parent
INGEST, METRICS = "http://127.0.0.1:18090", "http://127.0.0.1:18093"
PROM, LOKI, GRAF = "http://127.0.0.1:19090", "http://127.0.0.1:13100", "http://127.0.0.1:13000"
METRICS_TOKEN = "live-test-metrics-token-0123456789abcdef"
DEVICE = "tbeam-test"
BOOT = "0" * 31 + "1"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)
    return ok


def http(url, body=None, headers=None, auth=None, method=None):
    headers = dict(headers or {})
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except OSError as exc:
        return 0, str(exc)


def get_json(url, **kwargs):
    status, text = http(url, **kwargs)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def wait_for(what, probe, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe():
            return True
        time.sleep(3)
    print("TIMEOUT waiting for " + what)
    return False


def compose(*args, check_result=True):
    return subprocess.run(["docker", "compose", "-f", str(HERE / "docker-compose.yml"), *args],
                          check=check_result, capture_output=True, text=True)


def snapshot(revision, active, events):
    """A device check-in. Codes: 1001 critical, 1002 error, 2003 error, 4001 warning."""
    return {
        "schema_version": 1,
        "checkin": {"boot_id": BOOT, "status_sequence": revision, "uptime_ms": 5000,
                    "firmware_version": "test-1", "storage_backend": "sd",
                    "pending_events": 4, "dropped_events": 0, "counters": {"5001": 3},
                    "active": [{"code": code, "episode_id": episode} for code, episode in active],
                    "observed": [1001, 1002, 2003, 4001]},
        "events": events,
    }


def event(sequence, code, transition, episode=None):
    return {"schema_version": 1, "catalog_version": 6, "firmware_version": "test-1",
            "boot_id": BOOT, "boot_counter": 1, "sequence": sequence, "code": code,
            "transition": transition, "episode_id": episode, "uptime_ms": 1000 * sequence,
            "utc_ms": None, "time_source": "unknown", "sync_age_ms": None, "context": {}}


def post_device(body):
    status, text = http(INGEST + "/v1/device-events", body,
                        {"Authorization": "Bearer live-test-ingest-token", "X-Device-Id": DEVICE})
    check("ingest accepts revision %d" % body["checkin"]["status_sequence"], status == 200, text[:120])


def prom_query(expr):
    status, data = get_json(PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    return data["data"]["result"] if status == 200 and data.get("status") == "success" else []


def loki_query(expr):
    status, data = get_json(LOKI + "/loki/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    if status != 200:
        return status, data
    return status, data["data"]["result"]


def loki_lines(expr):
    """Log lines (parsed JSON) from the last hour for a log (not metric) query."""
    start = int((time.time() - 3600) * 1e9)
    status, data = get_json(LOKI + "/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start, "limit": 1000}))
    if status != 200:
        return []
    return [(s["stream"], json.loads(v[1])) for s in data["data"]["result"] for v in s["values"]]


def logql_examples():
    return re.findall(r"```logql\n(.*?)```", (GRAFANA_DIR / "loki-alerts.md").read_text(), re.S)


# Metric -> labels README.md documents (besides job/instance added by Prometheus).
METRIC_LABELS = {
    "mileage_tracker_last_contact_age_seconds": {"device_id"},
    "mileage_tracker_snapshot_age_seconds": {"device_id"},
    "mileage_tracker_condition_state": {"device_id", "code", "symbol", "severity", "state"},
    "mileage_tracker_pending_events": {"device_id"},
    "mileage_tracker_dropped_events": {"device_id"},
    "mileage_tracker_checkin_counter": {"device_id", "code", "symbol", "severity"},
    "mileage_tracker_alert_decisions": {"device_id", "code", "symbol", "severity", "decision"},
    "mileage_tracker_db_read_errors_total": set(),
    "mileage_tracker_last_successful_read_timestamp_seconds": set(),
    "mileage_tracker_monitor_stale_after_seconds": set(),
}


def test_prometheus():
    print("== Prometheus")
    for what, cmd in (("promtool check config", ["check", "config", "/etc/prometheus/prometheus.yml"]),
                      ("promtool check rules", ["check", "rules", "/etc/prometheus/alert-rules.yml"])):
        result = compose("exec", "-T", "prometheus", "promtool", *cmd, check_result=False)
        check(what, result.returncode == 0, (result.stdout + result.stderr).strip().replace("\n", " | "))
    status, text = http(METRICS + "/metrics")
    check("/metrics without a token is refused", status == 401, status)
    status, text = http(METRICS + "/metrics", headers={"Authorization": "Bearer " + METRICS_TOKEN})
    check("/metrics with the token answers 200", status == 200, status)
    wait_for("target up", lambda: any(t["health"] == "up" for t in get_json(
        PROM + "/api/v1/targets")[1].get("data", {}).get("activeTargets", [])), 150)
    targets = get_json(PROM + "/api/v1/targets")[1]["data"]["activeTargets"]
    check("scrape target mileage-tracker is UP",
          any(t["health"] == "up" and t["labels"]["job"] == "mileage-tracker" for t in targets),
          [(t["scrapeUrl"], t["health"], t["lastError"]) for t in targets])
    for metric, labels in METRIC_LABELS.items():
        wait_for(metric, lambda metric=metric: bool(prom_query(metric)), 150)
        series = prom_query(metric)
        seen = [set(s["metric"]) - {"__name__", "job", "instance"} for s in series]
        check(metric + " present with documented labels",
              bool(series) and all(s == labels for s in seen), "%d series, labels %s" % (len(series), seen[:1]))
    for metric, value in (("mileage_tracker_pending_events", 4), ("mileage_tracker_dropped_events", 0),
                          ("mileage_tracker_checkin_counter", 3)):
        check(metric + " carries the reported value %d" % value,
              [float(s["value"][1]) for s in prom_query(metric)] == [value])
    decisions = {s["metric"]["decision"] for s in prom_query("mileage_tracker_alert_decisions")}
    check("alert_decisions only use the documented decision values",
          bool(decisions) and decisions <= {"active", "sent", "resolved"}, sorted(decisions))
    active = prom_query('mileage_tracker_condition_state{state="active"}')
    check("condition_state shows 1002 active", any(s["metric"]["code"] == "1002" for s in active))
    check("condition_state shows 1001 clear",
          bool(prom_query('mileage_tracker_condition_state{code="1001",state="clear"}')))


def test_alert_rules():
    print("== Prometheus alert rules")
    for name in ("TrackerActiveErrorOrCriticalFault", "TrackerNoContact"):
        fired = wait_for(name, lambda name=name: any(
            a["labels"]["alertname"] == name and a["state"] == "firing"
            for a in get_json(PROM + "/api/v1/alerts")[1]["data"]["alerts"]), 330)
        check("alert %s fires on the test data" % name, fired)
    groups = get_json(PROM + "/api/v1/rules")[1]["data"]["groups"]
    errors = [r["lastError"] for g in groups for r in g["rules"] if r.get("lastError")]
    check("no rule evaluation errors", not errors, errors)


def test_loki():
    print("== Loki")
    examples = logql_examples()
    check("loki-alerts.md has 4 LogQL examples", len(examples) == 4, len(examples))
    # (example index, label that must appear in the result, expected value)
    expected = [(0, "code", "1002"), (1, "code", "1001"), (2, "device_id", DEVICE), (3, "code", "1002")]
    for index, label, value in expected:
        query = examples[index]
        wait_for("LogQL example %d" % (index + 1), lambda label=label, value=value, query=query: any(
            r["metric"].get(label) == value for r in (loki_query(query)[1] or [])
            if isinstance(r, dict)), 330)
        status, result = loki_query(query)
        check("LogQL example %d parses and matches %s=%s" % (index + 1, label, value),
              status == 200 and any(r["metric"].get(label) == value for r in result),
              result if status == 200 else (status, result))
    lines = loki_lines('{job="mileage-tracker"}')
    # Loki 3 itself adds service_name and detected_level to every stream.
    loki_added = {"service_name", "detected_level"}
    check("every stream has the labels job, device_id and severity (plus Loki's own " + ", ".join(sorted(loki_added)) + ")",
          bool(lines) and all(set(stream) - loki_added == {"job", "device_id", "severity"}
                              for stream, _ in lines), sorted({k for stream, _ in lines for k in stream}))
    status, data = get_json(LOKI + "/loki/api/v1/labels")
    check("indexed Loki labels are job, device_id, severity (plus Loki 3's service_name)",
          set(data["data"]) - {"service_name"} == {"job", "device_id", "severity"}, data["data"])
    kinds = {line.get("kind") for _, line in lines}
    check("event, condition_change and alert_decision lines arrived",
          {"event", "condition_change", "alert_decision"} <= kinds, sorted(map(str, kinds)))


def test_recovery():
    print("== Recovery (device clears the fault)")
    post_device(snapshot(2, [], [event(4, 1002, "clear", BOOT + ":1")]))

    def cleared():
        return bool(prom_query('mileage_tracker_condition_state{code="1002",state="clear"}'))
    check("condition_state flips to clear", wait_for("1002 clear", cleared, 200))
    check("clear example now also matches code 1002",
          wait_for("loki clear 1002", lambda: any(r["metric"].get("code") == "1002"
                   for r in loki_query(logql_examples()[1])[1] or [])))
    check("a resolution alert_decision line was pushed", wait_for("resolution", lambda: any(
        line.get("decision") == "resolution"
        for _, line in loki_lines('{job="mileage-tracker"} |= "alert_decision"')), 150))


def grafana_request(path, body=None, method=None):
    return get_json(GRAF + path, body=body, auth="admin:live-test-admin", method=method)


def test_grafana():
    print("== Grafana")
    wait_for("grafana", lambda: http(GRAF + "/api/health")[0] == 200, 120)
    health = get_json(GRAF + "/api/health")[1]
    print("Grafana version", health.get("version"))
    uids = {}
    for name, kind, url in (("Prometheus", "prometheus", "http://prometheus:9090"),
                            ("Loki", "loki", "http://loki:3100")):
        status, data = grafana_request("/api/datasources", {"name": name, "type": kind, "url": url,
                                                             "access": "proxy"})
        check("create %s data source" % name, status == 200, data if status != 200 else "")
        uids[kind] = data["datasource"]["uid"]
    for filename, kind, input_name in (("tracker-health.json", "prometheus", "DS_PROMETHEUS"),
                                       ("loki-panel.json", "loki", "DS_LOKI")):
        dashboard = json.loads((GRAFANA_DIR / filename).read_text())
        status, data = grafana_request("/api/dashboards/import", {
            "dashboard": dashboard, "overwrite": True,
            "inputs": [{"name": input_name, "type": "datasource", "pluginId": kind, "value": uids[kind]}]})
        check("import %s" % filename, status == 200, data if status != 200 else data.get("importedUrl"))
        if status != 200:
            continue
        status, stored = grafana_request("/api/dashboards/uid/" + dashboard["uid"])
        text = json.dumps(stored)
        check("%s: %s resolved to the data source uid" % (filename, input_name),
              "${DS_" not in text and text.count(uids[kind]) >= len(dashboard["panels"]))
        for panel in stored["dashboard"]["panels"]:
            queries = [{"refId": t["refId"], "datasource": {"type": kind, "uid": uids[kind]},
                        "expr": t["expr"], "queryType": "range", "instant": False, "range": True,
                        "maxLines": 100, "intervalMs": 15000, "maxDataPoints": 500}
                       for t in panel["targets"]]
            status, data = grafana_request("/api/ds/query", {"queries": queries, "from": "now-1h", "to": "now"})
            results = data.get("results", {}) if status == 200 else {}
            for query in queries:
                result = results.get(query["refId"], {})
                rows = sum(len(f["data"]["values"][0]) for f in result.get("frames", []) if f["data"]["values"])
                check("%s panel '%s' query %s returns data" % (filename, panel["title"], query["refId"]),
                      status == 200 and not result.get("error") and rows > 0,
                      result.get("error") or "%d frames, %d rows" % (len(result.get("frames", [])), rows))


def main():
    workdir = tempfile.mkdtemp(prefix="grafana-live-test-")
    scrape = (GRAFANA_DIR / "prometheus-scrape.yml").read_text().replace("<host>", "monitor")
    Path(workdir, "prometheus.yml").write_text(scrape + "\nrule_files:\n  - /etc/prometheus/alert-rules.yml\n")
    token = Path(workdir, "mileage-tracker-token")
    token.write_text(METRICS_TOKEN)
    token.chmod(0o644)
    os.environ["LIVE_TEST_DIR"] = workdir
    try:
        compose("up", "-d", "--build")
        wait_for("ingest", lambda: http(INGEST + "/health")[0] == 200, 120)
        post_device(snapshot(1, [(1002, BOOT + ":1"), (4001, BOOT + ":3")],
                             [event(1, 1002, "raise", BOOT + ":1"), event(2, 1000, "notice"),
                              event(3, 4001, "raise", BOOT + ":3")]))
        test_prometheus()
        test_loki()
        test_alert_rules()
        test_recovery()
        test_grafana()
    finally:
        out = compose("logs", "--no-color", "monitor", check_result=False).stdout
        print("-- monitor log tail --\n" + "\n".join(out.splitlines()[-12:]))
        if "--keep" not in sys.argv:
            compose("down", "-v", "--rmi", "local", "--remove-orphans",
                    check_result=False)
    print("%d/%d checks passed" % (sum(RESULTS), len(RESULTS)))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
