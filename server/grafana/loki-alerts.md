# Loki alert examples

The primary fault rule uses `condition_change` lines. They are sent on active
condition changes independently of ntfy delivery, so this rule works with
`NOTIFY_ENABLED=false`. These LogQL rules detect transitions in the selected
time window. For durable current condition state, use the Prometheus
`TrackerActiveErrorOrCriticalFault` rule, which reads
`mileage_tracker_condition_state`. `mileage_tracker_alert_decisions` and
`alert_decision` lines describe ntfy delivery state.

**An error or critical condition became active in the last five minutes:**

```logql
sum by (device_id, code) (
  count_over_time(
    {job="mileage-tracker", severity=~"error|critical"}
      | json
      | kind="condition_change"
      | state="active" [5m]
  )
) > 0
```

**A condition cleared in the last five minutes:**

```logql
sum by (device_id, code) (
  count_over_time(
    {job="mileage-tracker"}
      | json
      | kind="condition_change"
      | state="clear" [5m]
  )
) > 0
```

**E7001 no-contact silence began in the last five minutes:**

```logql
sum by (device_id) (
  count_over_time(
    {job="mileage-tracker"}
      | json
      | kind="condition_change"
      | code="7001"
      | state="active" [5m]
  )
) > 0
```

**An alert decision ntfy accepted in the last five minutes (only when ntfy is enabled):**

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

Choose an evaluation interval shorter than the range window and set Grafana's
contact point and notification policy in your own installation.
