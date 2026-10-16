# Loki alert examples

Use these LogQL expressions in Grafana's alert-rule editor with the external
Loki data source. They detect decision lines delivered in the selected window;
they are event alerts, not a durable current-condition view. Use the Prometheus
`TrackerActiveErrorOrCriticalFault` rule for current monitor alert state.

**A new active error or critical alert decision in the last five minutes:**

```logql
sum by (device_id, code) (
  count_over_time(
    {job="mileage-tracker", severity=~"error|critical"}
      | json
      | kind="alert_decision"
      | decision="alert" [5m]
  )
) > 0
```

**A fault resolution decision in the last five minutes:**

```logql
sum by (device_id, code) (
  count_over_time(
    {job="mileage-tracker"}
      | json
      | kind="alert_decision"
      | decision="resolution" [5m]
  )
) > 0
```

For both rules, choose an evaluation interval shorter than the range window and
set Grafana's contact point and notification policy in your own installation.
