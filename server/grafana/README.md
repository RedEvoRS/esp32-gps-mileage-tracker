# External Grafana files

These files are starter material for Prometheus and Grafana that you run
elsewhere. The Compose project does not install either service.

Before importing: the monitor, metrics and Loki push are all off by default
in [`monitor.env.example`](../monitor.env.example). Enable the monitor with
`MONITOR_ENABLED=true`; metrics also need `METRICS_ENABLED=true` and a separate
`METRICS_TOKEN` of at least 32 printable ASCII characters (no spaces). The
`/metrics` endpoint is plain HTTP on port 8093, for the LAN or a VPN only.
Without configuring these outputs, Prometheus cannot scrape and Loki receives
no records. Follow the full setup in [DEPLOY_DEBIAN.md Step 11e
(Prometheus/Grafana)](../../DEPLOY_DEBIAN.md#11e--optional-prometheus-metrics-for-external-grafana)
and [Step 11f (Loki)](../../DEPLOY_DEBIAN.md#11f--optional-push-to-external-loki).

Tested with Grafana 13.2.4, Prometheus 3.15.1 (`promtool` 3.15.1) and Loki 3.7.9
on 2026-10-21 (UTC); the Loki examples also passed on Loki 2.9.17. Loki 2.4 or
newer is required, because the monitor sends records with their original
timestamps and older Loki releases reject out-of-order lines. To repeat the
test, run `python live-test/live_test.py` (see [`live-test/`](live-test/README.md)).

- Merge `prometheus-scrape.yml` into Prometheus `scrape_configs`, changing
  `<host>` to the tracker server's LAN address or a host reachable over your
  VPN. Store the bearer token in the referenced private token file.
- Import `tracker-health.json` in Grafana and select the Prometheus data
  source that contains the `mileage-tracker` scrape job.
- `alert-rules.yml` contains Prometheus alert-rule examples and the same
  PromQL expressions can be used in Grafana-managed rules.
- Import `loki-panel.json` in Grafana after adding your Loki data source.
  `loki-alerts.md` uses condition changes independently of ntfy (the optional
  notification service) for fault alerts, plus a separate ntfy-delivered example.

## Metric reference

These are all metrics exported by [`prometheus.py`](../mileage-monitor/prometheus.py).
`code` is the numeric event/condition code; `symbol` and `severity` come from
the diagnostic catalog. See [DIAGNOSTICS.md](../../DIAGNOSTICS.md) for those codes.

| Metric | Type | Labels | Meaning and freshness |
| --- | --- | --- | --- |
| `mileage_tracker_last_contact_age_seconds` | gauge | `device_id` | Seconds since the server last accepted contact from the device, including identified GPX uploads. |
| `mileage_tracker_snapshot_age_seconds` | gauge | `device_id` | Seconds since the server accepted the latest diagnostics snapshot; GPX contact does not refresh it. |
| `mileage_tracker_condition_state` | gauge | `device_id`, `code`, `symbol`, `severity`, `state` | Current condition evidence: one series of value 1 for the current `active`, `clear` or `unknown` state. Missing/unobserved evidence or a snapshot older than 24 h gives `unknown`, never an assumed recovery. |
| `mileage_tracker_pending_events` | gauge | `device_id` | Device's queued diagnostic event count from the latest snapshot. Retained even after that snapshot becomes stale. |
| `mileage_tracker_dropped_events` | gauge | `device_id` | Device's reported dropped diagnostic event count from the latest snapshot. Retained even when stale. |
| `mileage_tracker_checkin_counter` | gauge | `device_id`, `code`, `symbol`, `severity` | Latest snapshot value for a catalog counter, such as a missing Wi-Fi access point. Retained even when stale; a reported value, not a Prometheus counter. |
| `mileage_tracker_alert_decisions` | gauge | `device_id`, `code`, `symbol`, `severity`, `decision` | ntfy delivery history only: episode counts for `active` (delivered and still active), `sent` (delivered at least once), or `resolved` (resolution delivered). Not current device health; counts episodes, not reminder messages. |
| `mileage_tracker_db_read_errors_total` | counter | none | Diagnostics or monitor-state database read failures since this monitor process started. |
| `mileage_tracker_last_successful_read_timestamp_seconds` | gauge | none | Unix time of the last successfully published monitor data refresh; 0 before the first. Check this to detect retained data after read failures. |
| `mileage_tracker_monitor_stale_after_seconds` | gauge | none | Configured no-contact threshold (`MONITOR_STALE_DAYS` in seconds, seven days by default). E7001 is the monitor's no-contact silence condition, raised when the server has not heard from a device for this long; it is not proof of device failure. |

Scrapes use the monitor's latest published data. Ages increase at scrape time;
other values wait for a monitor refresh. Samples without a valid underlying
value are omitted, so absence is not zero or proof of health.
