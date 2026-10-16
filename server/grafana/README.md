# External Grafana files

These files are starter material for Prometheus and Grafana that you run
elsewhere. The Compose project does not install either service.

- Merge `prometheus-scrape.yml` into Prometheus `scrape_configs`, changing
  `<host>` to the tracker server's LAN address or a host reachable over your
  VPN. Store the bearer token in the referenced private token file.
- Import `tracker-health.json` in Grafana and select the Prometheus data
  source that contains the `mileage-tracker` scrape job.
- `alert-rules.yml` contains Prometheus alert-rule examples and the same
  PromQL expressions can be used in Grafana-managed rules.
- Import `loki-panel.json` in Grafana after adding your Loki data source.
  `loki-alerts.md` uses ntfy-independent condition changes for the primary
  fault alert and includes a separate ntfy-delivered decision example.

The metrics names and labels are documented in [DIAGNOSTICS.md](../../DIAGNOSTICS.md).
