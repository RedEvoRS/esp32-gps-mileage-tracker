# Loki alert examples

The primary fault rule uses `condition_change` lines, exported independently
of ntfy (the optional notification service), so it works with
`NOTIFY_ENABLED=false`. These LogQL rules find states newly observed or
exported by the monitor in the selected time window. For durable current
condition state, use the Prometheus
`TrackerActiveErrorOrCriticalFault` rule, which reads
`mileage_tracker_condition_state`. `mileage_tracker_alert_decisions` and
`alert_decision` lines describe ntfy delivery state.

On first export, or for a device/condition the monitor has not seen before,
it emits a baseline record of the current `active` or `clear` state. A baseline
`clear` is not a confirmed recovery; an `active` may be a pre-existing fault.
The state queries below match these baselines as well as later changes.

**An error or critical `active` state newly exported in the last five minutes:**

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

**A `clear` state newly exported in the last five minutes:**

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

**An E7001 `active` state newly exported in the last five minutes:**

E7001 is the monitor's no-contact silence condition: the server has not heard
from a device for `MONITOR_STALE_DAYS` (seven days by default). It is not proof
of device failure. The JSON field uses the numeric code `7001`.

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

These `[5m]` windows use record timestamps, not arrival time. Records keep
their original timestamps: server receipt time for events, the monitor tick
for condition exports and successful ntfy decisions. The event backlog is
sent first; failed pushes retry with backoff up to an hour. A record delivered
after its timestamp has left the window never matches these rules. See
[DEPLOY_DEBIAN.md Step 11f](../../DEPLOY_DEBIAN.md#11f--optional-push-to-external-loki).
A wider window tolerates more delay but keeps old matches firing longer;
a narrower window clears sooner but misses more late records.

Choose an evaluation interval shorter than the window and set Grafana's
contact point and notification policy in your own installation.
