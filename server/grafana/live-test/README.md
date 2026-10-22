# Live test for the Grafana starter files

`live_test.py` checks the files in the parent folder against real Prometheus,
Loki and Grafana running in throwaway Docker containers. Nothing is installed
on your machine or contacted on the network, apart from pulling the three
official images. All ports are bound to `127.0.0.1` and every credential is an
obvious fake.

```
python live_test.py          # about 6 minutes; needs Docker and Docker Compose v2
python live_test.py --keep   # leave the stack running to look at Grafana
```

With `--keep`, open `http://127.0.0.1:13000` (user `admin`, password
`live-test-admin`). The test has already imported both dashboards through
Grafana's API (uids `tracker-health` and `tracker-loki-logs`). To try the
browser import form, delete them first (**Dashboards**, the dashboard,
**Settings**, **Delete**) or change the UID on the form. Otherwise Grafana
asks whether to overwrite them. Remove the stack afterwards with
`docker compose -p grafana-live-test -f docker-compose.yml down -v --rmi local`
(set `LIVE_TEST_DIR` to any folder first). `--rmi local` also deletes the
two images the test built (`grafana-live-test-ingest` and
`grafana-live-test-monitor`, about 380 MB together). Without it they stay
on your machine.

What it does:

1. Builds the real `mileage-ingest` and `mileage-monitor` images from `server/`
   and starts them with the monitor's metrics, Loki push and ntfy enabled.
   A small fake ntfy server accepts the monitor's alert posts, and
   `MONITOR_STALE_DAYS` is about one minute so the no-contact rule can fire.
2. Posts a fake device (`tbeam-test`) to the real `POST /v1/device-events`:
   active error `1002`, active warning `4001`, several clear conditions and a
   check-in counter. It later posts a recovery.
3. Merges `prometheus-scrape.yml` into a Prometheus config, runs `promtool
   check config` and `check rules`, and checks the target is up, all ten
   `mileage_tracker_*` metrics and their documented labels exist, and both
   alert rules fire.
4. Runs every LogQL example from `loki-alerts.md` against Loki and checks the
   stream labels and the `event`, `condition_change` and `alert_decision` lines.
5. Imports both dashboards through Grafana's import API, with the data source
   input mapping, and runs each panel's queries through Grafana's query API.

Unless you pass `--keep`, the stack is removed at the end, including its
volumes and the two images the test built. The three official images it
pulled stay in Docker's cache (remove them with `docker rmi` if you want the
space back). Those images are pinned in
`docker-compose.yml`; to try another Loki, set `LOKI_IMAGE`, for example
`LOKI_IMAGE=grafana/loki:2.9.17 python live_test.py`.

The test does not draw the dashboards in a browser, so it cannot catch a panel
that queries correctly but renders badly.
