# Mileage Tracker Server

This folder is the server half of the GPS mileage tracker. It accepts
finished trips as GPX (GPS Exchange Format) files over home WiFi, and lets
you review, tag and export them in a web page. The [top-level README](../README.md) explains
the complete setup.

The board is the LilyGO T-Beam Supreme with MAX-M10S GPS and microSD trip
storage, though the server accepts GPX uploads from any board. The server
was deployed with `install.sh` on Debian 13 on 2026-10-20;
`install.sh --check` passed. On 2026-10-21 (UTC), the Supreme's trip logger
firmware, running in debug mode, uploaded four real trips from microSD. Ingest logged acceptance
and HTTP 200 for each, wrote `data/YYYY-MM/<trip>.gpx` and `.json`, and
the viewer listed all four with distances. The board deleted each local
file after HTTP 200. For the new drive, the board reported 11.40 mi and
the server reported 11.41 mi; the two calculate distance separately.
Diagnostics followed in two HTTP 200 requests, storing all 115 pending
events and a current snapshot visible in Device health. Trip uploads with
debug mode off and server-down retry were also verified on 2026-10-21
(UTC): two `HTTP -1` failures kept the trip queued, then HTTP 200 after
ingest restarted deleted the local file.

As of 2026-11-01 (UTC), the deployed server and the board's firmware are
both **v1.00** (tag `fw-v1.00`), the first public release. The release
candidate, firmware `v0.11`, passed its week of normal driving on 2026-11-01
(UTC).

Bench checks on 2026-11-01 (UTC) also passed server-error recovery and
diagnostics deduplication (retried batches stored each event once). HTTP 503 kept both a small trip and a roughly
3 MB trip queued, showed E5008 ("Server unavailable"), then both uploaded
after recovery. A wrong token gave HTTP 401 and E5004 ("Upload unauthorized")
for the small file; the roughly 3 MB file instead saw a broken send,
`HTTP -3`, and E5003 ("Upload link failed"). Both stayed queued and uploaded
after the original token was restored.

Still untested on hardware: uploads from LittleFS (the internal flash
filesystem) without the SD card, HTTP 413 handling, a trip ending in a parking
garage (deferred past v1.00), the `flash.ps1` first-install and `-Cleanup`
paths, and `flash.sh`. Unraid has no real-box verification yet either.

It runs as two default Docker containers, with two optional services, all
defined in `docker-compose.yml`:

| Service | Port | What it does |
|---|---|---|
| `mileage-ingest` | 8090 | Accepts trip uploads from the board (bearer token) and stores each one as GPX plus a JSON (structured text) summary |
| `mileage-viewer` | 8091 | A web page, behind a login, to list trips, check them on a map, tag their purpose, and export CSV (comma-separated values) or XLSX (Excel workbook) files |
| `mileage-monitor` | 8093 | Optional `monitor` profile: authenticated Prometheus metrics; current-fault and seven-day no-contact alerts |
| `ntfy` | 8092 | Optional `ntfy` profile: self-hosted phone notifications, authenticated topic access |

**Install or update from a release:** on the Debian 13 server, as a normal
login with sudo and working Docker Engine / Compose v2:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh | bash
```

On Unraid 7.3+ (7.x), use root, a persistent pool path and Unraid-managed
Docker with Compose Manager Plus. Follow [the Unraid runbook](../DEPLOY_UNRAID.md)
instead of the Debian prerequisites. Unraid support has only fake-test
coverage so far; Unraid 8.x is not supported yet.

This verifies/copies the release, runs `install.sh` to configure, build and
check the services, then offers a personal firmware build. Read the
[trust note and download/read/run alternative](../README.md#setting-it-up)
before executing downloaded code. Follow [deployment Step 2](../DEPLOY_DEBIAN.md#step-2--run-setupsh)
for prerequisites, binding to your local network (LAN) with `BIND_ADDR`,
backups and optional notifications.
The builder downloads about 1.8 GB once (about 2.7 GB installed image; allow
10 GiB free for the first build) and asks for WiFi/device settings; its
owner-only `firmware-out/` contains your WiFi password and upload token in
the binaries. Flash from the USB-connected PC using
[README Step 2](../README.md#step-2--flash-the-tracker), then do the one-time
cleanup after the first diagnostics snapshot.

To update, rerun setup on the server and accept the firmware build, then
rerun the PC flash script **without first install** (answer no at that
prompt). Updates write only the app and preserve settings/trips. Setup's
source copy preserves private configuration/data; the builder replaces its
own output. See the [update procedure](../DEPLOY_DEBIAN.md#updating).

`install.sh --check` leaves configuration unchanged but uploads and removes
a temporary test trip, can test the admin login, and lists existing everyday
logins for information. On Debian, automatic backups
currently require the server login's numeric user ID to be **1000**
(`id -u`); the installer skips backups for other IDs. See
[the deployment prerequisites](../DEPLOY_DEBIAN.md).
Unraid runs backups as root and keeps the containers and `data/` at UID 1000.

## What the Ingest Service Does

When the tracker board completes a trip and detects it's on your home WiFi,
it POSTs the trip as a **GPX** file to this service. The service verifies
an auth token, parses the GPX, and writes two files using atomic writes
so they can never be left partially written:

- `/data/YYYY-MM/<id>.gpx`  — the raw upload, byte-for-byte (the source
  of truth that any map tool can open directly)
- `/data/YYYY-MM/<id>.json` — a derived summary (distance, duration,
  speeds, bounding box) so other tools can list and total trips without
  re-parsing every track

Ingest refuses trip and diagnostics folders or files that resolve outside its data folder.

Distance is the great-circle (haversine) sum over consecutive fixes, with
three rules, all in `summarize()`:

- **Stopped steps don't count.** If the device's speed at both ends of a
  step is under 2 mph, the vehicle was parked or at a light, and the step
  is GPS scatter. Without device speed, distance/time is used instead.
- **Bad fixes are skipped.** That means fixes with HDOP (a GPS accuracy figure; higher is worse)
  above 5, and any
  step implying more than 60 m/s.
- **GPS gaps are bridged.** A new `<trkseg>` marks a gap (tunnel, parking
  structure). The straight line across it is the shortest possible path,
  so bridging can only undercount, and leaving the gap out lost real miles.
  A bridge needs timestamps at both ends and a plausible speed.

For completely timed trips, `end_time` is the last moving fix, so the firmware's 5-minute cooldown
doesn't stretch every trip; `recorded_until` keeps the final timestamp.
Summary version 3 records `timing_status` as `complete`, `partial`, or
`untimed`. A partly timed trip retains its distance, but full-trip
`start_time`, `end_time`, `duration_seconds`, and `avg_speed_mph` are null.
`timed_from` and `recorded_until` describe only the timestamps actually
recorded. No timestamps are invented for the missing portion. The trip ID
and storage month keep using the same observed timestamps as before, so
retries update the same trip and preserve annotations.
Each summary records `summary_version`, and the raw GPX is never touched,
so summaries can be recomputed if the rules change. The tests are in
`server/tests/` (`python -m unittest discover -s server/tests`).
Existing summaries are not rewritten at startup; new uploads and retries
write the current version.

The service is intentionally minimal. It does not generate reports,
provide a web user interface (UI), or classify trips. Those are separate
concerns handled by `mileage-viewer`, which reads these files independently.

## Directory Layout

    mileage-tracker/           # this folder, copied to the server (main files only)
    ├── docker-compose.yml     # All Python builds use this server directory as context
    ├── .dockerignore          # Runtime allowlist; excludes secrets, tests and data
    ├── VERSION                # Server release number
    ├── COMMIT                 # Archive commit; literal placeholder in a Git checkout
    ├── stamp_version.py       # Build stamp from Git, then COMMIT, then VERSION alone
    ├── common/                # Shared code (required): diagnostics/ validators and catalog.json,
    │                          #   trip_summary.py (distance rules), version.py
    ├── .env.example           # Template for .env
    ├── .env                   # Upload token and BIND_ADDR (created by you or install.sh, never committed)
    ├── viewer.env.example     # Template for viewer.env
    ├── viewer.env             # The admin login (created by you or install.sh, never committed)
    ├── monitor.env.example    # Disabled defaults; template for private monitor.env
    ├── mileage-monitor/       # monitor.py, gelf.py, prometheus.py, loki.py + Dockerfile; stdlib only
    ├── grafana/               # Importable Prometheus/Grafana/Loki starter files; no bundled services
    ├── ntfy/server.yml        # Optional ntfy deny-all config; no credentials
    ├── install.sh             # First-time install with checks; --check later
    ├── parked_drain.py        # Read-only parked battery drain helper (see DIAGNOSTICS.md)
    ├── sample.gpx             # A short test trip for checking uploads
    ├── data/                  # Trip files (create it before the first start, see below)
    │   ├── diagnostics/events.db # Ingest-owned SQLite, created on first contact
    │   └── 2026-04/
    │       ├── 2026-04-14T13-45-22.gpx
    │       └── 2026-04-14T13-45-22.json
    ├── backup/                # Backup + restore; Debian timer / Unraid scheduled.sh cron
    ├── firmware-builder/      # Server-side tracker firmware build (build.sh, README.md)
    ├── firmware-src/          # Firmware source the builder compiles (created by setup.sh)
    ├── firmware-out/          # Your private firmware build output (created by the builder, never committed)
    ├── tests/                 # The server test suite (not needed to run it)
    ├── mileage-ingest/
    │   ├── Dockerfile         # Container definition
    │   ├── requirements.in    # Direct Python dependencies (maintainer input)
    │   ├── requirements.txt   # Complete dependency lock with hashes
    │   ├── app.py             # GPX uploads + device-event endpoint (Flask)
    │   ├── diag_store.py      # Diagnostics transactions and ordered health
    │   └── reset_high_water.py # Admin recovery for a device's snapshot high-water (see DIAGNOSTICS.md)
    └── mileage-viewer/
        ├── Dockerfile
        ├── requirements.in
        ├── requirements.txt
        ├── app.py             # The web UI (Flask)
        ├── manage_users.py    # Command-line management of everyday logins
        ├── users.py           # Login accounts and password hashing
        ├── diagnostics_settings.py # Retention setting and atomic ingest mirror
        ├── trip_log.py        # Trip removal, change history and tax-year records
        ├── trip_merges.py     # Merging recordings from one drive
        ├── geocoding.py       # Optional place lookup
        └── static/            # Leaflet and its GPX plugin, bundled

## Optional diagnostics notifications

`install.sh` asks which of them to turn on, in its Notifications step. To enable them by hand, follow
[DEPLOY_DEBIAN.md Step 11](../DEPLOY_DEBIAN.md#step-11--optional-device-notifications). No subscriptions or cloud accounts are required.
The `monitor` and `ntfy` Compose profiles are off by default, keeping ordinary
installation and `install.sh --check` unchanged. Copy `monitor.env.example` to
private `monitor.env` (mode 600), select it with `MONITOR_ENV_FILE=./monitor.env`
in `.env`, and enable monitoring/delivery there. The monitor receives neither
ingest nor viewer credentials. If explicitly started with disabled settings,
it logs that it is idling and does no database reads or sends.

The monitor reads every known device from `/data/diagnostics/events.db` using
SQLite `mode=ro`, schema v1 and a two-second busy timeout. A missing file means
nothing to monitor; unreadable/schema/lock errors are logged and retried next
minute, never treated as healthy. It cannot write trip data or annotations.
The monitor checks the resolved diagnostics folder and file against the configured events path's data root (its grandparent); escaping paths suspend sends and leave the heartbeat unchanged until a successful read.
Its versioned state DB, `/state/monitor.db` in `monitor-state`, persists
episodes, delivery intent, digest overflow, reminders, rolling-hour cap and
retry backoff. Use one replica and preserve the volume to retain deduplication.

Each completed monitor tick touches `/state/heartbeat` in that same volume.
Disabled ticks still refresh it without reading a database; a missing
`events.db` before the first upload is normal too. Unreadable diagnostics,
failed state access and unexpected exceptions leave the heartbeat unchanged.
Unexpected tick errors log the exception type and traceback frames without
exception text, source lines, local variables or private paths, then wait the
usual 60 seconds before trying again. KeyboardInterrupt and SystemExit still
stop the process.

Docker checks this local file every 30 seconds, with a five-second timeout,
90 seconds of startup grace and two failures allowed. A heartbeat at most
180 seconds old is healthy: three missed ticks plus the probe allowance
usually make a stalled running monitor unhealthy after about 3.5-4 minutes.
No network or credentials are used. `docker ps` shows `(unhealthy)`, and
`install.sh --check` reports it even for a running disabled monitor. A normal
deployment without the monitor container has no optional-health warning.
An older container with no heartbeat check prompts a rebuild and recreation.
Health measures tick progress, not successful ntfy, Graylog or Loki delivery;
a long-running export can also delay progress. Docker does not restart a
running container solely because it is unhealthy. Inspect
`docker compose --profile monitor logs --tail 50 mileage-monitor` to find the
cause. No extra outbound alive-ping service is configured.

Fault notifications require eligible severity from the current catalog and
a snapshot at most 24 hours old (including exactly 24 hours). Every output
uses the same state rule: an accepted snapshot determines faults, and server
contact age determines E7001 silence, independently of snapshot age. Stored
condition rows do not override a fresh snapshot. Catalog severity and `notify`
filter phone delivery only; Prometheus and Loki condition lines retain current
catalog codes, while Graylog also describes unknown codes. Hold defaults to
300 seconds (critical skips it), reminders to 24 hours, and the cap to six
successful posts per rolling hour. Overflow becomes bounded digests. Recovery needs an actually sent alert
and an ordered explicit clear; unknown, reboot or historical events do not
resolve faults. Delivery is at least once, with exponential retries from one
minute to one hour; a crash after acceptance can duplicate a message. Bodies
include code, catalog title/recovery and device label, with no links or context.

E7001, "No contact for N days", defaults to seven days of **server receipt
time**, including identified GPX uploads. It bypasses the device-fault severity
filter and hold, shares reminders/cap, and resolves on newer contact. There is
no watchlist; all devices remain monitored. After 24 hours without contact,
the tracker tries a heartbeat during its existing awake opportunities; it
does not wake just to send one. A tracker parked away from WiFi can therefore
still trigger a silence notification.

The bundled ntfy is locked down with `auth-default-access: deny-all`; create a
local user, grant only the alert topic, and give the monitor its access token.
Cache/auth DBs live in `ntfy-state`. Set `BIND_ADDR` before publishing port
8092. LAN-only delivery reaches the phone on home WiFi or a VPN; the owner
decides about remote access. iOS instant push needs
`upstream-base-url: https://ntfy.sh` plus the phone-facing base URL, relaying
only a free wake-up poke, not message text. Android needs no relay.
[ntfy documents the settings](https://docs.ntfy.sh/config/).
An existing self-hosted ntfy can be selected with `NTFY_URL` instead.

Installer questions and monitor startup use the same rules in
`mileage-monitor/settings_rules.py`. Topic names and bundled ntfy usernames
use 1–64 letters, digits, `_` or `-`. An ntfy token is nonempty printable
ASCII without spaces; a metrics token needs at least 32 such characters.
Real punctuation such as `$`, `#`, `<`, `>`, quotes and backslashes is allowed.
Loki bearer tokens have 1–1024 printable ASCII characters without spaces;
basic usernames/passwords have 1–1024 printable ASCII characters including
spaces (no colon in the username). Tenant IDs may be empty or up to 128
printable ASCII characters including spaces. Control characters are refused.
Silence accepts one second through 36500 days, including decimals or scientific
notation. URLs require valid DNS/IP hosts and optional ports from 1 to 65535,
without embedded credentials, queries, fragments, whitespace or controls.

The installer quotes these values for Compose's `env_file` parser, including
Compose 2.26. By hand, single-quote literal dollars, hashes, spaces and quotes;
escape an apostrophe as `\'`. For values with backslashes use double quotes,
escaping backslashes as `\\`, double quotes as `\"`, and dollars as `$$`.
Keep one setting per line and use literal values rather than `$OTHER` or
`${OTHER}` references: the installer reports those as ambiguous rather than
guessing Compose's interpolation context. Full examples and error behavior
are in DEPLOY_DEBIAN.md Step 11b. On Unraid, one disposable Python checker
serves all Notifications questions; secrets travel on stdin, never argv.
The checker stays off when `--check` has no selected monitor file and when a
fresh install chooses no outputs. Existing monitor heartbeat checks still run.

Optionally, the monitor also sends diagnostics as GELF (Graylog Extended Log
Format) to a Graylog you run elsewhere (`GRAYLOG_*` in `monitor.env`, off by
default; nothing here installs
Graylog). It forwards diagnostic events at least once, with duplicates possible
after an interrupted send or restoring an older diagnostics database. It also
forwards each change in a device/code condition's state (active/clear/unknown,
plus the no-contact fault E7001), and
Graylog's own Event Definitions do the alerting. GELF HTTP is preferred (each
message confirmed); GELF TCP with optional TLS also works. Graylog outages back
off independently and never affect ntfy. Setup: DEPLOY_DEBIAN.md Step 11d.

Prometheus metrics are another optional monitor output on port 8093
(`METRICS_ENABLED=false` by default). The endpoint requires a separate bearer
token and publishes only on `BIND_ADDR`; keep it LAN-only because that token is
the only access protection. A Prometheus server on another host scrapes it over
the LAN or VPN, then Grafana reads that Prometheus data source. The monitor does
not install Prometheus or Grafana. Dashboard, scrape and alert examples are in
`grafana/`; setup is in DEPLOY_DEBIAN.md Step 11e.

If you also run Loki, a nonempty `LOKI_PUSH_URL` can send new diagnostic
events, condition changes and successful monitor alert/reminder/resolution
decisions. Condition changes are independent of ntfy and deduplicated across
deep-sleep wakes; delivered-decision lines exist only when ntfy accepted an
alert. Loki is external and is not bundled in Compose. Its HTTP push has an
independent cursor, retry state and timestamp order per stream; a failed Loki
push does not stop ntfy or Graylog. Only HTTP 400 (including too-old lines)
and 422 (unusable content) skip the batch and advance its cursors. All other
HTTP errors retry without advancing, including wrong token/tenant/address
errors and 413 (a possibly temporary or adjustable size limit). A retryable
4xx logs the status, settings to check and delay on each retry, bounded by
backoff. Fixing the settings lets queued history resume. JSON lines use only
current catalog fields and bounded context, with
`job`, validated `device_id` and catalog `severity` as the only labels set by
the monitor. Loki 3 can add its own `service_name` label and `detected_level`
metadata to query results.
Importable panel and LogQL alert examples are in `grafana/`; see
DEPLOY_DEBIAN.md Step 11f.

Backups include consistent diagnostics and monitor-state database snapshots.
The diagnostics retention setting is included in the annotations snapshot.
The `ingest-settings` volume holds only a derived JSON copy and is not backed
up; restore restarts the default services and viewer startup recreates that
copy from the restored annotations database.
The optional ntfy volume (users, tokens and cached messages) needs a separate
backup. Full policy and schema details are in [DIAGNOSTICS.md](../DIAGNOSTICS.md).

## Server version and deployments

`VERSION` starts at `v0.01`, independently of the firmware version. Every
viewer page has a server version in the header (footer on sign-in and error
pages). **Device health** also puts the viewer's server version next to each
reported firmware version.
This version describes the viewer image; use both services' `/health` responses
or `bash install.sh --check` to confirm ingest is running the same build.
Both `/health` endpoints keep their `status` and `service` fields and add
`version`. They need no login: anyone who can reach ports 8090/8091 on the LAN
can read the build version. Keep the documented LAN-only binding in place.

For maintainer source deployments, bump `server/VERSION` in the PR that ships
the change; do not open a separate version-only PR. Use
`v<major>.<two-digit minor>` (major 0-99, for example `v0.02`). After the PR
merges and you verify the deployment, tag that exact merged commit `srv-v0.NN`
(for example `srv-v0.02`). The initial release
uses `srv-v0.01`. These server tags are separate from firmware tags. Do not
tag a dirty build as a release: its uncommitted changes cannot be recovered
from a Git tag.

Server `srv-` tagging stays manual after the verified deploy. Merging a
`firmware/VERSION` bump automatically publishes the firmware release as
Latest immediately, before the server is deployed or the board is flashed,
and then uploads its assets. That release can include a new server version.
If it fails, re-run all Firmware release jobs, or manually run Release assets
(`release.yml`) with the published tag if only assets failed. See
[Firmware versions and releases](../firmware/README.md#firmware-versions-and-releases)
for the reviewed release notes and recovery steps.

From this folder, `python3 stamp_version.py` prints the release plus the first
ten Git commit characters, for example `v0.01+abcdef1234`. Any uncommitted
tracked change anywhere in the repository appends `-dirty`; untracked and
ignored files (including credentials/data) do not. Run it again for every
build, as the commands below do. `install.sh` computes it automatically and
passes it through Compose to ingest, viewer and optional monitor build
arguments. Docker stores the stamp in each image's `SERVER_VERSION`
environment value; restarting an old image does not give it a new version.
The monitor reports its version in its startup log.

The manual build commands below require Python 3 on the build host
(`install.sh` installs it on Debian). On Unraid, the installer and firmware
builder use disposable Python containers if host Python is absent; follow
[the Unraid runbook](../DEPLOY_UNRAID.md).
A Git clone with Git installed provides the commit and dirty state. Without
usable Git metadata, the helper reads `COMMIT` if it contains a full 40- or
64-hex hash and uses its first ten characters. Commit/tag archives and GitHub
source downloads supply this hash; no Git install is needed on the server.
Archives start clean, so this fallback never adds `-dirty`; later hand edits
cannot be detected. Recopy clean source before rebuilding for an accurate stamp.
If `COMMIT` is missing, invalid or still contains `$Format:%H$`, the helper
uses VERSION alone with a reason on stderr. `COMMIT` is optional for hand
copies and stays outside the Docker images; the stamp arrives as a build arg.
A missing/invalid VERSION gives `unversioned`. Building without the argument
also gives `unversioned`; runtime values outside a 1-64 character ASCII token
(letters, digits, `.`, `_`, `+`, `-`, starting with a letter/digit) are replaced
by `unversioned` before HTML, JSON or monitor logs. `--check` prints both
running versions and warns about missing stamps or mismatches without
changing its exit code for those warnings. A mismatch means the two services
may contain different code; rebuild both with the same stamp and check again.

For an advanced source deployment, use the
[manual commit archive/copy route](../DEPLOY_DEBIAN.md#manual-alternative-copy-server).
It preserves the archive commit stamp and includes the required dotfiles.
Release users keep the supplied version; the bump/tag instructions above
are for maintainers deploying source changes.

## Updating Python dependencies

Each service's `requirements.in` is the short list of packages we use directly.
Its generated `requirements.txt` locks the complete dependency tree to exact
versions and SHA-256 hashes. Image builds and CI install that lock with hash
checking, so rebuilding does not silently pick up new Python packages. The
Python base image and operating-system packages are updated separately.

Edit direct pins in the `.in` files when needed, keeping shared pins in step.
From the repository root, regenerate **both** locks in the runtime's Linux
Python 3.12 environment (never resolve them with Windows Python):

```bash
docker run --rm --mount "type=bind,src=$(pwd),dst=/work" -w /work \
  python:3.12-slim sh server/update_python_dependencies.sh
```

In Git Bash on Windows, prefix the command with `MSYS_NO_PATHCONV=1`.
In PowerShell, use `src=${PWD}` instead of `src=$(pwd)` in the mount argument.
The helper installs `pip==25.2` and `pip-tools==7.5.3` in the throwaway container,
then runs `pip-compile --upgrade --generate-hashes --resolver=backtracking
--strip-extras --no-emit-index-url --no-emit-trusted-host
--output-file=requirements.txt requirements.in` in ingest first, then viewer.
The viewer command also adds `--constraint ../mileage-ingest/requirements.txt`
to keep shared packages at the ingest lock's versions. This constraint stays
in the helper, leaving Dependabot free to update each input/lock pair. Hashes include
all published wheels and source archives, including Linux amd64 and arm64 wheels.
The inputs and helper ship in releases for maintainers; only the compiled locks
are needed in image build contexts or by `install.sh`.

Review both lock diffs, build both images on amd64 and arm64, and run the server
suite using the locks together:

```bash
python -m pip install --require-hashes -r server/mileage-ingest/requirements.txt -r server/mileage-viewer/requirements.txt
python -m unittest discover -s server/tests
```

CI checks pins, hashes, direct versions and agreement between locks without
running the resolver. Dependabot's monthly grouped updates cover direct and
indirect packages in both services, with a seven-day version-update cooldown.
Keep the standard pip-compile Python 3.12 header: Dependabot uses it to select
Python and regenerates the hashes. If the shared-version test fails on an
update, regenerate both locks together
with the command above and review the resulting changes.

## First-Time Setup

The primary route is release setup above. For an already copied source
tree, run `bash install.sh` from this directory. It sets up both services and
backups, asks for the admin login and offers an optional everyday login,
checks each step, and tests an upload end to end
([deployment overview](../DEPLOY_DEBIAN.md#the-quick-way-setupsh)). The steps below
set up both services by hand, in the order Compose needs them, all run
from this folder. Docker has to be installed first (`DEPLOY_DEBIAN.md`
Step 1).

The manual steps below use Debian's login and sudo conventions. On Unraid,
run host commands as root without `sudo`; `data/` must still belong to
1000:1000, mode 700. Use [the Unraid install steps](../DEPLOY_UNRAID.md#step-1--install-with-setupsh)
for storage, Python fallback and service setup.

1. Create the `data/` folder yourself, owned by UID 1000, the user both
   containers run as. If Docker creates it on first start, root owns it
   and every upload fails with a 500:

       mkdir -p data
       chmod 700 data              # keep trip file names private
       id -u                       # if this isn't 1000:
       sudo chown 1000:1000 data

   Ingest runs as UID 1000 and creates private (mode 700) month folders
   inside `data/`. If your login's UID isn't 1000, you can't open `data/`
   afterwards without `sudo`; the commands below that look inside it say so.

2. Copy the example environment file to `.env` so you can fill in
   the real auth token:

       cp .env.example .env
       chmod 600 .env

3. Generate a strong random auth token and put it in `.env`:

       openssl rand -hex 32

   Copy the output and set `INGEST_AUTH_TOKEN` in `.env` to that value.
   You'll use the same token in the tracker firmware later. This token is
   separate from your SSH login and the viewer's admin or everyday login.

4. In `.env`, set `DISPLAY_TZ` to the IANA (Internet Assigned Numbers Authority)
   timezone name for where you drive, for example `DISPLAY_TZ=Europe/London`.
   Both services refuse to start without
   it; ingest uses it to choose month folders and the viewer uses it for
   displayed/exported times. See [Timezone](#timezone).

5. Before starting either service, set `BIND_ADDR` in `.env` to the server's
   stable LAN IP address (`BIND_ADDR=<host>`; replace `<host>` with the IP,
   not a hostname). Find it with `ip -4 addr show` and arrange a static IP or
   DHCP reservation. Leaving this blank publishes ports on every interface;
   UFW (Uncomplicated Firewall) does not restrict Docker-published ports. See
   [network setup](../DEPLOY_DEBIAN.md#step-7--firewall-and-network).

6. Create `viewer.env` and set the admin login, as described under
   [Viewer Login](#viewer-login) below. Compose won't build or start
   anything, either service, until `viewer.env` exists.

7. Build and start both services:

       SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build

   The `SERVER_VERSION` command computes a fresh stamp for this build.
   Set `BUILDX_NO_DEFAULT_ATTESTATIONS=1` on every manual build/rebuild so
   cached builds keep stable image IDs on Docker 29 and leave unchanged
   containers running. `install.sh` sets it automatically; this works with
   Compose 2.38 as well as newer versions.

8. Set `H` to that same server LAN IP and verify the service is responding.
   This also works if an existing install publishes on all interfaces:

       H='<host>'
       curl "http://$H:8090/health"

   You should see `{"service":"mileage-ingest","status":"ok","version":"v0.01+abcdef1234"}`.

9. Test an authenticated upload with the sample trip, `sample.gpx`, which
   is in this folder:

       TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
       printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -X POST "http://$H:8090/upload" \
         -H @- \
         -H "Content-Type: application/gpx+xml" \
         --data-binary @sample.gpx

   (`-H @-` reads the header from standard input. A token written into curl's
   arguments can be read by any other account on this server while the command
   runs.)

   You should see a JSON success response with a `distance_miles` value,
   and find two files at `./data/2026-04/2026-04-14T14-30-00Z.gpx` and
   `.../2026-04-14T14-30-00Z.json`. If `id -u` isn't 1000, look with
   `sudo ls data/2026-04`; a plain `ls` says "Permission denied", which looks
   like a failed upload. Delete both afterwards (with `sudo` in the same case)
   so the test trip doesn't end up in your mileage log, its viewer totals or
   its exports:

       sudo rm data/2026-04/2026-04-14T14-30-00Z.gpx data/2026-04/2026-04-14T14-30-00Z.json

   Leave out `X-Device-Id` for this synthetic upload. Adding it registers a
   device for diagnostics monitoring; deleting the trip files does not remove
   that device, which would later be reported as silent.

   The `^INGEST_AUTH_TOKEN=` match above reads only the setting, not
   comments that mention its name. It assumes the unquoted hexadecimal
   token generated in step 3.

   A timed GPX always gets its ID from its earliest point time (or metadata
   time when no point is timed), in UTC. `X-Trip-Id` is used only when the GPX
   has no usable point or metadata timestamp; otherwise it is ignored.
   It can then name that upload, except that timestamp-shaped IDs
   are reserved for timed GPX. Firmware never sends that header: local
   filenames (`t000001.gpx`, for example) use a counter kept in the board's
   flash settings (NVS, the ESP32's non-volatile storage), while each
   uploaded size-split piece starts at its own boundary fix. Retries search
   all month directories under the upload lock and keep the original folder,
   including after a `DISPLAY_TZ` change. Existing duplicate folders require
   operator review; ingest returns 409 rather than choosing one. An internal
   month-folder alias counts once. If any `YYYY-MM` folder links outside
   `data/` or loops, every GPX upload gets HTTP 503 and the ingest log names
   the folder. A new trip whose own month folder is a link to a folder with a
   different name (say `2026-04` → `2026-03`) gets the same 503, because it
   would otherwise be filed and shown under the other month. A link to a
   non-month folder (say `2026-04` → `archive`) also gets 503 for new trips:
   ingest refuses that link even though the viewer shows existing trips under
   `2026-04` and counts them. Replace the link with a real folder containing
   those trips, or mount the disk there. The
   tracker keeps trips until HTTP 200 and retries after repair; Device health
   shows server errors. Diagnostics uploads are unaffected.
   Older third-party timed uploads named with a custom header keep their old
   names on disk. Review those pairs before re-uploading: the new timestamp ID
   can create another record. Firmware uploads already use timestamp IDs.

   **More than one tracker on one server (not advised):** this project is
   built and tested for one tracker per server, and running two trackers at
   the same time isn't an advised use case. If you really want to do it:

   - Give each tracker its own `DEVICE_ID`, so the ingest log and Device
     health can tell them apart.
   - Know the limitation: trip IDs don't include the device. If two trackers
     start recording trips in the same second, the second upload gets HTTP
     409, the ingest log names the conflict, and Device health shows E5007
     for that tracker. The trip stays queued on that tracker and is retried
     on every visit home, but it never arrives on its own. Both cars have to
     start within the same second, so this is unlikely.
   - To clear a stuck trip, copy the file off the card and move it aside, as
     described under `HTTP 409` in
     [Check recording and uploads](../firmware/README.md#check-recording-and-uploads).

   Ingest rejects GPX document types/entities, unknown XML encodings, timestamps
   before 2000-01-01 or more than one day after server receipt, and non-finite
   derived mileage/speeds with 400 before writing trip files. Old backlog since
   2000 remains valid. GPX creator and the two identity headers are limited to
   128 characters; diagnostics device labels use the stricter 32-character rule.
   The board treats 400 and 413 as final: it renames that trip to `.bad` on its
   storage and doesn't retry it, so a trip rejected by one of these checks is
   missing from the mileage log until someone reads the card. See the
   [firmware guide's upload troubleshooting](../firmware/README.md#check-recording-and-uploads).

   `INGEST_AUTH_TOKEN` must be nonempty, with no whitespace or control characters;
   invalid settings stop ingest at startup. New data directories are private
   (0700) and files, including `events.db`, use 0600. The viewer and monitor run
   as the same UID 1000 and can read these through their read-only data mounts.

   Ingest keeps two Gunicorn sync workers and the 900-second upload timeout:
   an authenticated 8 MiB piece can take about 839 seconds at 10 kB/s. Rejected
   bodies are drained only within both the byte cap and a three-second absolute
   deadline; a slower rejected sender may see a connection reset. The deadline
   uses POSIX main-thread signals. A threaded or non-POSIX host skips draining
   non-memory streams. Changing to `gthread` would require a different hard
   deadline mechanism, even though POSIX upload `flock` locks use separate open
   descriptions and diagnostics uses request-local SQLite transactions.
   More sync workers increase capacity and memory use but do not prevent denial
   of service. Incomplete headers never reach the drain deadline, and slow
   authenticated bodies still occupy workers. For an untrusted network, configure
   a reverse proxy with an absolute header deadline and connection limits; keep
   its authenticated upload allowance long enough for the tracker.

## Daily Operation

Check the logs when you want to see what's happening:

    docker compose logs -f mileage-ingest

Every service, including optional monitor and ntfy, keeps at most three 10 MB
Docker log files per container. This protects the server's Docker storage from
growing logs. The limit applies after containers are recreated: the installer
update does this when the Compose settings change, including enabled optional
services. Existing logs are not trimmed in place; recreating a container
discards its old logs, so save any you need first. `install.sh --check` warns
when a running container still has older log settings.

After changing `.env` or `viewer.env`, recreate the containers so they
pick up the new values (Compose only recreates the ones that changed):

    docker compose up -d

`docker compose restart` is not enough for that: it restarts a container
with the settings it was created with, so a new token or login wouldn't
take effect. Use it only when nothing in the config changed:

    docker compose restart mileage-ingest

Stop the service entirely:

    docker compose down

To update code or dependency versions, first copy the new committed source
and its `COMMIT` using the
[manual copy route](../DEPLOY_DEBIAN.md#manual-alternative-copy-server) (or copy
a fresh GitHub source download). Then rebuild **on the server**, from this folder:

    SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build

## Inspecting Your Trip Data

The normal way to browse, audit, and export trips is the `mileage-viewer`
web UI (see its section in `docker-compose.yml`). But the summary files
are plain JSON, so you can also use any tool you like. The month folders are
private to UID 1000. If `id -u` isn't 1000, run each command through
`sudo bash -c '...'`, because your own shell can't expand `data/*` for `sudo`.

Count trips in the current month:

    ls data/$(date +%Y-%m)/*.gpx | wc -l

Sum total miles for a month with jq:

    jq -s 'map(.distance_miles) | add' data/2026-04/*.json

These commands read the raw files, so they include trips an admin removed from
the log, count the separate parts of a merged drive, and file trips by the
month the drive ended (the viewer uses the local start date). Use the viewer's
totals or export for your mileage record.

View the most recent trip summary:

    ls -t data/*/*.json | head -1 | xargs cat | jq .

Open a track on a map: any `.gpx` file opens directly in GPXSee,
gpx.studio, Google Earth, OsmAnd, or QGIS.

## Viewer / Export UI

A second service, `mileage-viewer`, gives you a web page to browse,
audit, and export your trips. It's defined in the same
`docker-compose.yml` and starts alongside ingest with
`SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build`.

The viewer follows your OS colour scheme; the Theme button on each page cycles
Auto, Light and Dark and remembers the choice in this browser.

Open it at:

    http://<server>:8091/

What it does:

- **Trip list** — trips with date, distance, duration, destination, and the
  purpose tag you've assigned. Select a year or All years to see matching
  trips and totals for business, personal, other, and unclassified miles.
  On a phone each trip shows as a card with its **Map / Edit** button next to
  the date, so nothing is off the edge of the screen; wider windows show the
  table.
- **Map audit** — click "Map / Edit" to see the track drawn on an
  OpenStreetMap map (Leaflet). This is how you eyeball a trip for GPS
  errors before trusting its mileage. Your trip coordinates are served
  from the LAN and drawn in your browser; only the base map tiles come
  from OpenStreetMap by default. The browser sends the tile host its IP
  address and the viewed areas and zoom levels, which over time can reveal
  where the car parks. To use your own tile server, set
  `MAP_TILE_URL=http://<host>/{z}/{x}/{y}.png` in `.env` and set
  `MAP_TILE_ATTRIBUTION` to its plain text credit. The URL must use HTTP or
  HTTPS, include `{z}`, `{x}`, `{y}` (optional `{s}`, `{r}`), be at most
  2048 characters and contain no quotes, backslashes, angle brackets,
  whitespace or control characters. Attribution is escaped as text, limited
  to 2000 characters without controls. Invalid settings stop the viewer
  at startup with the setting's name in the log. Unset or empty values keep
  today's OSM URL and credit. `MAP_TILE_URL=none` makes no tile requests:
  the track and endpoints still draw on a plain background that follows
  light/dark mode. Run `docker compose up -d` after changes. These settings
  go only to the viewer; installer reruns and release updates preserve them
  in `.env` without asking new questions.
- **Annotate** — tag each trip business / personal / other and add a
  note (e.g. "Client visit — Acme Corp") and destination / place. Annotations are stored in a
  SQLite database in a separate `viewer-db` volume, kept apart from the
  immutable trip files. A note or destination can hold up to 2,000
  characters, and the viewer refuses (and says why) one that contains a
  control character a spreadsheet can't store, which a paste from another
  program sometimes brings along. The XLSX export drops such a character from
  an older value and marks any value too long for one spreadsheet cell with
  `[TRUNCATED: ...]`; the CSV keeps the full text.
- **Export** — "Export CSV" / "Export XLSX" use the selected year and
  include annotations, recorded start/end latitude and longitude, and totals.
- **Device health** — open `/device-health` from the navigation after login.
  See last contact, snapshot age, firmware, storage, pending/dropped counts,
  counters and active / unknown / clear conditions with catalog recovery advice.
  History shows 50 events per page, with code/severity filters and device clock
  confidence separately from server receipt time, all in `DISPLAY_TZ`.
  Severity comes from the current catalog, including for older stored events.
  The diagnostics DB is opened read-only with a two-second lock timeout.
  Before it exists the page says "No diagnostics yet"; read/schema/lock errors
  say "Diagnostics unavailable" (503) and log a warning. Neither means healthy.
- **Diagnostics retention** — on **Admin** (`/admin`), set how many days of
  events to keep: **365** by default, whole numbers **30–3650**. Only an admin
  can save it; the page shows who last saved it and when. Ingest deletes older
  events on the next diagnostics upload in batches of 250, with a two-second
  budget checked between batches. Unfinished cleanup continues on the next
  upload; completed or failed attempts wait an hour. Current device health
  and the newest event are kept.
  Deleted pages are reused; cleanup does not shrink the database file.
  The viewer saves the setting in `annotations.db`, then publishes a tiny
  `diagnostics.json` in `ingest-settings`, mounted at `/settings` writable in
  viewer and read-only in ingest. Ingest cannot read the viewer database, and
  viewer still reads diagnostics only. A failed hand-off warns on Admin;
  fix the volume and save again or restart the viewer. A missing or invalid
  mirror makes ingest use 365 days. [Retention details](../DIAGNOSTICS.md#diagnostics-event-retention).

**Each year counts the miles driven in that year, in `DISPLAY_TZ`.** A drive
under way at the start of 1 January appears in both years, with only each
year's share of its miles. It stays one recording, map and annotation. Under
All years it appears once with its full miles. It counts as one trip in each
year, so adding yearly trip counts can exceed the All years count; the miles
still add up exactly. Closing either year locks the whole drive, so set its
purpose before closing the old year.

The viewer divides the saved trip total in proportion to counted GPX distance
on each side of local midnight. It uses the same distance rules as ingest,
including straight-line estimates across GPS gaps; a step crossing midnight
is divided by time. The old-year share is rounded to three decimals and the
new-year share is the remainder, rounded to three decimals. This preserves
the saved total even for older summaries and merged drives. Nothing is written
to the trip files or database. A step from the last moving fix to a stopped fix
can add mileage after midnight. A stationary cooldown with no counted distance
after midnight leaves the drive in its start year.

Splitting requires complete timing, a readable valid GPX and exactly one year
end. Otherwise the drive stays whole in its start year (or the first recorded
year for partial timing), with a short note explaining why it could not be
split. For older summaries without `recorded_until`, the viewer uses the saved
end time to check whether the drive crosses midnight. If one share rounds to
zero, the year with the miles counts all of it. That can be the new year: a drive that starts
just before midnight with no counted miles until after it shows its December
31 date but counts, and locks, only in the new year, with a note saying so.
Its `trip_distance_miles` stays blank because it is not split. This applies only to trips the
viewer can already read: a summary or GPX link refused by its safety checks
still makes the trip unreadable with no miles counted. Wholly undated trips
stay under All years without a New Year note; the viewer cannot guess whether
they crossed New Year. The year review lists
crossing drives with both shares or the reason they stayed whole. A year closed
before this change keeps its saved totals: its review compares them with the
current shares and names the drive. Reopen and close it again only when you
want to replace that saved snapshot with the current figures.

The storage folder is not used for filtering, except for unreadable summaries
(below). The viewer labels partly timed trips with
**Partial timing** and leaves
unknown duration and average speed blank. CSV/XLSX include `timing_status`,
`timed_from`, and `recorded_until`; older summaries without a status export
`unknown`. For partly timed trips, year selection and sorting
use `timed_from`, the first recorded timestamp in `DISPLAY_TZ`, so their miles
remain in year totals and exports. The date is labeled **Timed from** and
`year_basis` exports `first_recorded_time`. Their actual start may be in an
earlier year, which the viewer states explicitly. Wholly undated trips appear
only under All years; the admin year review lists them (count, miles and links) so
they can be dealt with before closing a year. Direct exports
accept `?year=2026`; omit `year` for all years. Empty years export headers
and zero totals. Totals cover the recorded trips in the selection, not an
odometer measurement of all vehicle use or a determination of deductibility.

CSV exports retain the existing columns and append destination, endpoint
coordinates, and `row_type`. Trip rows have `row_type=trip`; category totals
and the overall `all` total follow with `row_type=total`. **When importing
trip records into another tool, filter to `row_type=trip` to avoid counting
the totals as trips.** XLSX keeps one trip per row on the `Mileage` sheet
and puts year, display timezone, and totals on a separate `Totals` sheet.
Totals are summed before rounding to three decimal places, matching stored
distance precision. Unclassified totals include trips without a category.
CSV and the XLSX `Mileage` sheet append one last column, `trip_distance_miles`,
after `received_after_close`. It holds the drive's full miles when split,
including in All years; it is blank for other trips and other CSV row types.
With a year selected, `distance_miles` holds only that year's share. All older
columns keep their positions. Dates and `year_basis` still describe the trip's
date anchor, even when some miles count in the following year.

Each viewer worker keeps parsed summaries in memory, shared safely by its
threads. It checks file identity, size and nanosecond modification time on
each scan, rereads changed files and drops vanished entries. A restart clears
the cache. Annotations, removals and year locks always come from the database.
Ordinary reads use a consistent read transaction; only a changed merged
summary (or an older merge fingerprint being rewritten once) reserves the database
writer and rechecks the log before saving. SQLite can still briefly delay a
writer's commit until a read transaction finishes.

File discovery still covers the log: the year menu needs years from other
folders, every year review lists all undated trips, and a changed display
timezone or rebuilt summary can move a trip across New Year. A known-device
trip page also needs the full chronology for safe merge suggestions. Removed
trip pages, unknown-device pages and redirects read only the requested trip's
sources. The data mount remains read-only; none of these caches writes GPX
or JSON files.

A trip whose summary file can't be read (damaged on disk, edited by hand,
or missing next to its GPX) has no miles the viewer can count. The trip list shows a warning
naming it and why, the CSV lists it with `row_type=unreadable` and blank
miles, and the XLSX lists it on an `Unreadable trips` sheet; both keep any
purpose, note and destination already entered. With no start date to go
by, it's matched to a year by its storage folder, which is named for the
trip's local end month: that year, plus the year before for a January
folder. `DEPLOY_DEBIAN.md` → Troubleshooting says how to rebuild the
summary from its GPX.

A trip whose summary or track (GPX) is a link pointing outside
the data folder is treated the same way: the viewer never opens it, and the
trip is listed as unreadable ("the file could not be read") with no miles
counted. A track that is simply missing is different: the trip still counts
and just has no map. The admin's page for a trip with such a link says a file
is a link that leads outside the data folder or can't be followed safely;
replace the link with the real file. To an
everyday login it reads like an address with no trip.

Only first-level folders named `YYYY-MM` hold trips; the viewer ignores any
other folder in `data/` (such as `diagnostics/`). A month folder the viewer
can't use appears once as a folder (for example `2026-01/`) in both roles'
warning box, as `row_type=unreadable_folder` in CSV, on the XLSX `Unreadable
trips` sheet and in the year review: a link that leads outside `data/` (or to
`data/` itself or above), a link whose target is missing, a link that loops,
or a month entry the viewer could not inspect or list (it disappeared,
permissions deny access, or the filesystem reported an error). Its trips are
neither listed nor counted. The admin's hint fits the cause: replace the link
with the real folder or mount the other disk at that path; for a folder that
can't be read, check that it still exists, that its disk is available, and
that UID 1000 (the viewer reads `data/` read-only) may read it.

If any month link leads outside `data/`, to `data/` itself or above, or loops,
ingest refuses **every trip upload for every month** with HTTP 503, including
retries of already stored trips. An outside target need not exist. The folder
warning explains this, and a separate alert at the top of the list and in every
year review names the blocking folders, even when a different year is selected.
The tracker keeps the trips queued; Device health shows server errors.
Diagnostics uploads are unaffected. Replace all of those links with real month
folders (recovering existing files from their actual storage or a backup), or
mount their disks at the month paths. Do not delete their targets or trip files.
Then run `install.sh --check`, review the viewer totals and let the tracker retry.
A missing target **inside** `data/` is unreadable in the viewer. If its final
target name differs from the month link name, new trips for that month get
HTTP 503 and stay queued on the tracker until the link is replaced with a
real folder. Other months can still upload. A missing same-named target,
such as `2026-02` → `archive/2026-02`, does not cause that refusal; ingest can
create the target on upload. Disk I/O and other server faults still return 500.

One month folder that the server cannot read or enter also blocks **every trip
upload for every month**, including retries, with HTTP 503 naming that folder.
This includes a mode-100 or mode-300 folder: entering it is not enough;
both services must be able to list it to find every existing trip ID.
The viewer includes it in the "Uploads are blocked" alert. Both services run
as UID 1000. Copying or restoring folders by hand with `sudo` under a strict
umask can leave a root-owned mode-700 folder that neither service can use.
For a real month folder, run from the install directory containing `data/`:

```bash
sudo chown -R 1000:1000 -- data/2026-02
sudo find data/2026-02 -type d -exec chmod 700 {} +
sudo find data/2026-02 -type f -exec chmod 600 {} +
```

Use the folder named by the alert. For an internal link, inspect its real
target and each parent (`namei -l data/2026-02`) and fix their owner and search
permissions too; do not follow an outside link. On Unraid run these commands
as root without `sudo`. Reload the viewer and let a queued upload retry;
the alert clears and uploads resume once every blocking folder is usable.
Diagnostics uploads are unaffected.

A month folder that can be read and entered but cannot be written to (for
example, mode 500) refuses only that month's new trips with a named HTTP 503
and one log line, without a traceback. The tracker keeps them queued; other
months keep uploading. Existing trips are shown and counted. The viewer
shows a notice, with no missing-mileage count or merge block. Use the same
owner-and-mode repair commands above for the real folder, then reload and
let a queued trip retry. A root-owned mode-755 folder also needs repair unless
the services' group or other bits grant write access. Disk-full and I/O
write failures remain 500.

The viewer judges write permission from the owner, group and mode metadata
it already reads: owner bits take precedence, then the services' primary or
supplementary groups, then other bits. This adds no filesystem call per folder
and caches the services' identity once per worker. It never tests `W_OK` on
its read-only `data/` mount, so that normal mount causes no notice. ACLs and
the ingest mount's actual flags cannot be inferred from these mode bits;
ingest's real file-creation result is authoritative.

If a month name is a link to a file instead of a folder, remove only the
link, keeping its target. If the month name is a plain file, move it somewhere
safe outside the month names. The viewer reports either case as a folder
notice: a file cannot hold trips, so it adds no missing mileage, no year-close
unreadable count and no merge-suggestion block. New trips for that month get HTTP
503 and stay queued. Other months still upload. Once that name is free,
ingest can create a real month folder on retry.

The viewer checks containment before it lists a folder or reads a file, and on
Linux it lists each month through a directory handle that refuses a month
folder swapped for a link at the last moment. These checks can't stop someone
who can write to `data/` from swapping a *parent* directory in the instant
between the check and a listing or read; the Windows test fallback also lacks
the directory handle. Keep `data/` writable only by the containers' user and
root.

A month link to a folder inside `data/` that no other month name uses (for
example `2026-01` → `archive`) is read as that month. A folder notice says that
existing trips are shown and counted, but ingest refuses new trips for this
month until the link is replaced with a real folder (or a disk mounted there).
The tracker keeps new trips queued and retries after repair. This notice is
`row_type=folder_notice` in CSV, does not mean miles are missing, and does not
block merge suggestions or closing a year. If
two month names reach the same folder, its trips count once, under the real
month folder, and the other name
gets a notice saying where they are counted (`row_type=folder_notice` in CSV).
A notice doesn't mean miles are missing. If the link's final target has a
different name, its notice also says that the tracker cannot upload new trips
for that link's month and keeps them queued until the link is removed (HTTP
503). This includes `2026-12` → `2026-11`, both month links in
`2026-01` → `2026-02` → `archive`, and the unused name when two links reach one
non-month folder. The final target's name decides this, not which name the
viewer counts; a link to `archive/2026-02` can still accept February trips
under `2026-02`. Remove only the extra link, keeping the target and its files;
ingest can then create a real month folder for queued trips. If saved names
conflict, follow the steps below before removing links.
Opening a trip or GPX through an extra month link redirects to the counted
address, keeping the selected year. A change submitted to an extra-link address
gets HTTP 409 and saves nothing; reopen the trip from the list before editing,
removing, restoring, merging, splitting or looking up an address.

When only links reach that folder, the viewer keeps the name with saved trip
data: annotations, removals, edit history, merges and old merge addresses, or
trip keys in a closed-year snapshot. If none has saved data, it uses the first
name in sorted order. Adding an earlier link therefore keeps existing saved
data and year locks attached to the same trips. A real month folder still wins.
If several link names hold saved data, the viewer uses the first of those names
in sorted order and shows a warning naming all of them (`row_type=folder_warning`
in CSV). The trips are counted, but saved data under the other names is not
applied. The admin sees counts per name of annotations, removals, merges, merge
parts, old merge addresses, history entries and trip entries in closed-year
snapshots, with the name in use marked. Counts describe saved database entries,
including ones for files no longer present; they do not add miles. Every open
year's review shows the conflict beside a disabled Close button, even if the
folder belongs to another year. Closed-year reviews also show the conflict in
their status section. Closing any year waits until this conflict is resolved;
healthy trips under the name in use can still have merge suggestions. New
merges are saved under that name, with the same file and adjacency checks as
usual. If the admin keeps the other name, those new merges and merged
annotations stay saved but unapplied. When all replacement files still match,
they become unused-name notices under the rule below; otherwise they remain
damaged merges. Restoring the name brings its saved merge back. Existing year locks remain in
force; folder warnings are not counted as missing mileage in year snapshots.

To resolve competing saved names:

1. Make a normal backup of the trip files and `annotations.db`. Record each
   link's name and target so you can recreate it. Read the counts and the name
   in use in the admin warning. Choose which name's saved data you want to use;
   the viewer cannot combine the names' annotations, removals or merges.
2. Keep that name's link and remove only the other symbolic links, not their
   target folder or trip files. Removing a name stops applying the saved data
   under it. Removing a losing name leaves the current name's data in use.
   Removing the name in use quietly orphans its saved data: another remaining
   name is chosen and its annotations, removals and merges apply instead.
   Nothing is deleted from the database. Check the trip list, Removed trips,
   merges and year totals after changing the links.
3. To undo, recreate the same link name to the same target. Its saved data can
   be used again; if several saved names are present, the conflict returns and
   sorted order decides which is in use. To inspect a losing name's saved data
   in the viewer, temporarily leave only its link, then restore the recorded
   links to return to the original selection. Avoid editing or closing years
   while comparing names. Closed-year snapshots are left in place.
4. Once the desired name is the only link, replace it with a real month folder
   containing the existing trip files, or mount the disk at that month path.
   Keep the month name to preserve its saved-data keys. Verify the viewer and
   `install.sh --check`, then let the tracker retry queued uploads.

A merge is listed as saved data under an unused name only when every part
is readable and counted under another name, with both its GPX and summary
matching the saved file names, sizes and modification times. All parts from
one old month must match one unambiguous counted folder. A surviving alias
must reach that folder; after its link is removed, the saved file stamps
are the remaining evidence (the database stores no historical folder identity).
A deleted real folder, partial matches, changed files or unreadable parts
remain damaged merges. They count as missing mileage in the year review and
close snapshot of each year spanned by the saved recording. They block
uncertain merge suggestions and cannot be undone through **Undo saved merge**.
Ordinary **Undo merge (split back)** is also
refused for damage, for both roles. The admin can use **Discard damaged merge**
after confirming that parts with readable summaries return separately with
annotations saved under their names; parts without readable summaries do not.
The merged trip's saved miles and purpose stay out of the totals and are kept in history.
The same transaction preserves the complete saved summary, annotation and
part keys in history before deleting the merge. The year review permanently
lists the saved miles, purpose, date and login under **Discarded or undone saved merges**,
including which parts are now counted again, on their own or in a current merge.
This remains visible after closing. This deliberate confirmation and visible record are
enough to allow closing; there is no second acknowledgement. Reopen a closed
year before discarding. Healthy merge undo remains available to both roles.
CSV retains discarded and undone merges as `unreadable` rows with blank miles;
XLSX labels them on **Unreadable trips**. Their notes keep saved miles, purpose,
trip date, action date and login, and say which parts are counted again.
An unused-name notice adds no saved miles or
merged annotation and does not block merge suggestions for healthy trips.

**Never rename a month folder.** The viewer saves a trip's purpose, notes,
destination and removal under its address, `<month folder>/<trip id>`, so a
renamed folder leaves that saved data behind: a removed trip is listed and
counted again, and purposes and notes no longer show.

The viewer warns when a trip with a saved purpose, notes or removal turns up
under another month name and nothing is left at its old address. The warning
appears on the trip list, the year review and the exports, and it stays even if
new uploads have recreated the old folder. Deleting a folder without moving its
trips does not warn. Existing folder problems and alias conflicts keep their own
warnings.

While an item is unresolved, the years its trip counts in cannot be closed:

- both years for a drive split at New Year;
- only the new year when all of the drive's counted miles are after midnight;
- the start year when the drive stays whole there;
- for an unreadable trip, the same possible folder years as other unreadable
  trips;
- every year for a readable trip with no date.

The admin year review lists each unresolved item for that year: the trip's
current address (a link when the trip can open), its old address, and what was
saved (purpose, notes and destination, or the removal's reason, login and
time). Admins also see counts on the trip list and in the exports. Everyday
logins see the warning without details or counts.

To clear the warning, do one of these:

- **Rename the folder back**, or move those trips' files back into a folder
  with the old name. All the saved data applies again.
- **Save the purpose again under the new name.** For a plain trip, saving a
  purpose, notes or destination clears the purpose warning, even if those
  fields are cleared later. Saving with every field empty does not clear it
  unless an annotation was already saved there. For a trip that is now part of
  a merge, save a nonempty purpose, notes or destination on the merge; an
  unannotated merge still warns.
- **Remove the trip (or its merge) again** to keep it out of the log.
- **Choose Keep trip in log** beside the removal on the admin year review to
  keep the trip listed and counted. This deletes the old removal, records in
  history who restored it, when and the removal's reason, and returns to the
  year review. If a closed year locks the trip, the review names the years to
  reopen first; these are the same edit locks as for the current trip and its
  merge parts. A readable trip with no date is never locked by a closed year.

A removed or unreadable trip needs no purpose; that warning returns if the trip
is restored or becomes readable without an annotation. An old removal still
warns for an unreadable trip until the trip is removed again or the admin
chooses to keep it. Keeping it clears only the old removal; the unreadable
warning remains. History stays saved throughout.

After **Undo saved merge**, a former part's purpose or notes saved only under
the old name also needs this repair before any affected year can close.

Moving or renaming a real month folder with file times kept also produces an
unused-name merge when every recording matches under the new name.
To use that merge again, restore the original month name, as a link or by
renaming the folder back, and keep that name in use
(follow the conflict steps above before removing competing links). Or use
**Undo saved merge** on the trip list as admin and confirm removing that
merge and its merged annotation. The trips stay counted under the name in use with only annotations saved
under that name. Annotations under the unused name stay in the database but
are not applied. The merged trip's saved miles and purpose stay out of the totals;
the undo preserves its complete summary in history before deletion, in the
same transaction as discard, and appears in the year review with the same
current-count statement. Source files stay intact. Closed-year locks still apply;
reopen the affected year first. An everyday login must ask the admin.

CSV lists an unused merge as `row_type=folder_notice`, with its saved key,
category and recovery advice. Miles stay blank in CSV and XLSX numeric cells;
saved miles appear only in the note. XLSX shows the notice in **Unreadable
trips**, outside **Mileage** and **Totals**. The year review lists each merge
in **Saved merges under unused month names**, with its saved miles, purpose
and month name; its parts count separately. Closing its saved-summary year or
any year with counted mileage from a replacement part waits until the name is restored
or the admin uses **Undo saved merge**.
If the saved year is unknown, every year is blocked. Reopening stays possible.
Saved removals under an unused name are not applied, like annotations under
that name, so the parts of a formerly removed merge count again. Its saved
removal still hides the unused-merge notice, so it does not block year closing.
An unused merge adds no unreadable-file count. Older snapshots stay unchanged;
the review compares saved and current totals and names the former merged key
leaving and the currently counted trips joining. The undo checks all replacement recordings
again under its database write transaction; a stale form cannot discard a
merge whose replacement parts have gone missing.

The XLSX `Unreadable trips` sheet also lists folder notices and saved-data
warnings. Their row labels say whether trips are counted elsewhere, a link
blocks new uploads, or saved names conflict. Only a folder whose trips cannot
be read is labeled "whole month folder"; the CSV row types distinguish these
same cases.

`install.sh --check` reports each symbolic link directly inside `data/` as
a problem, with a hint to use a real folder or mount the other disk there.
Install mode warns and continues. If listing `data/` would require a new sudo
prompt, `--check` warns that it did not check the links.

### Admin year review and close

The admin-only `/admin/year/<YYYY>` page is linked from the trip list after
selecting a year, or from the year list on `/admin`, which shows whether each
year is open or closed. The review page brings together that year's category
totals (calculated the same way as exports), undated trips (which count in no
year), trips inside a merged trip (see below), trips without a category,
business trips missing a purpose note or destination, unreadable trips, removed
trips and their reasons, and edits made after the year ended. It also shows that
year's own close and reopen history. The review includes edits made at or
after local January 1 of the following year.

A split drive belongs to both years and each year counts only its share;
a drive with all counted miles after midnight belongs only to the new year.
Other trips use their local start or first recorded time. An unreadable trip
is assigned by its storage folder; a January folder can be listed under the
previous year. Folder entries follow the same rule, so a January folder
appears under both candidate years. A trip with no usable time is not locked
by any year.

The admin closes or reopens a year on its review page using a confirmation
checkbox. Both actions appear in change history. While a year
is closed, neither role can annotate, merge, undo a merge, remove or restore
a trip in that year. A merge that would include a trip from a closed year is
refused. The trip page identifies the closed year and who closed it, and the
trip list shows **Closed for edits**. Viewing trips, exporting, and place
lookup continue to work.

**Saved totals.** Closing a year saves its totals as they stand at that moment:
the trip count and miles for each category (business, personal, other,
no category) and overall, the same figures as the year's exports, plus the
list of trips that were counted. They are stored in `annotations.db` (so the
backups include them), and the close entry in change history shows them too.
A year closed before this feature existed has no saved totals; its review
page says so and keeps working. Reopening a year discards its saved totals (the
reopen entry in change history records what was discarded), and closing it
again saves new ones.

**Trips that arrive after the close.** Closing a year stops *viewer* edits only.
The tracker can still upload a trip that started in that year (for example a
backlog it was holding while it was out of WiFi range), and the server stores
it. That trip joins the year's list, totals and exports like any other, so
nothing is hidden, but it is not silent either:

- The year review has a **Received after this year was closed** section listing
  each such trip (when the server received it, its distance, its category) with
  a link. A trip counts as late when ingest stored it after the moment the year
  was closed. Closing takes a snapshot between uploads, and receipt time is
  set when ingest publishes the trip summary. Retrying the same upload keeps
  the original receipt time. A trip whose summary has no received time (stored
  by an older ingest) is never listed as late.
- The CSV and XLSX exports have a column, `received_after_close`, set to
  `yes` for those trips and blank for the rest. In the CSV it comes after
  `row_type`; `trip_distance_miles` follows it. With a year selected, the flag
  refers only to that year's close for a split drive. In All years, a split drive
  is flagged if it arrived after either affected year was closed. Closing either year locks
  the whole drive even when the other year is open.
- **Saved totals** on the review page shows the totals saved at the close next
  to today's, with a difference column. Rows that differ are in bold, and a
  warning appears when anything differs. A difference is not always a late
  upload: changing `DISPLAY_TZ` can move a trip near New Year into or out of the
  year, and rebuilding a summary can change its miles. The review lists those
  trips under "Other changes since the close".

A late trip is in a closed year, so it can't be categorized (the viewer answers
409) until the admin reopens the year. To fix that, reopen the year, categorize
the trip, and close the year again. That takes a fresh snapshot, so the new
totals become the saved ones and the late trip no longer shows as late.

Endpoint coordinates come from the first and last recorded GPX track points,
so they are also available for existing summaries. They describe the raw
recording, including any final stationary samples; they are not geocoded
addresses or a corrected route. Missing, corrupt, or invalid GPX files leave
coordinates blank without preventing the rest of the export. Destination
is entered manually or filled from an endpoint suggestion (see below). On startup,
the viewer automatically adds an empty
destination column to existing annotation databases while preserving their
categories and notes. Existing categories remain business / personal / other.

The viewer mounts `./data` **read-only** — it never modifies GPX or
summary files. The only thing it writes is the annotations database.

### Remove trips and review changes

The admin removes a trip from the log with a required reason; unreadable trips
can also be removed. Folder warning entries cannot be removed trip by trip;
fix the folder instead. A removed trip disappears from the trip list, totals,
CSV/XLSX
exports and merge suggestions for both roles. The GPX and JSON source files
stay in `./data`; removal is a reversible record in `annotations.db`, not a
file deletion. On `/admin/removed`, the admin can see each reason, who removed
it and when, then restore the trip. The admin's trip page shows a **Removed
from the log** notice and a Restore button. An everyday login using the old
trip address gets the same 404 page as an address with no trip ("No trip at
this address"), and an address lookup for it answers the same as one for a
missing trip, so neither says whether the trip was removed. Permanently
deleting source files remains a separate shell operation.

The viewer has no real-delete button because it cannot write to the read-only
`./data` mount, it could not undo a source-file deletion, and the session cookie
crosses the LAN over plain HTTP. Keeping the original GPS record also
preserves the source data behind a mileage record.

The admin-only `/admin/history` page lists changes newest first, 50 per page,
and can filter to one trip. Annotation saves, merges, undo merges, removals
and restores record the login, UTC time, action and relevant before/after
values; year closes and reopens are also recorded. The page displays times in
`DISPLAY_TZ`. Trips changed before history began are labeled as predating the
history; they have no backfilled change entries. CSV and XLSX append `last_edited_by` and
`last_edited_at` after the existing trip columns, so earlier column positions
stay the same. CSV's existing `row_type` marker follows them.

### Merge recordings from one drive

If a long stop split one drive into separate trips, open **Map / Edit** on
the earlier trip, save any annotation edits, then choose **Merge with next
trip**. The action is available only for the next trip from the same known
device, with both GPX recordings present and a start no more than 15 minutes
after the earlier trip's `recorded_until` (the final recorded timestamp).
The limit is the named `MERGE_MAX_GAP` constant in `trip_merges.py`. Older
summaries without that timestamp use `end_time`, the last moving point, as
a conservative fallback. Upload/receipt times are never used. Overlapping
recordings, unknown device identities or unknown start times cannot be merged.
A folder problem that leaves trips unreadable, or a conflict between saved
month names, blocks merge suggestions with a
folder-specific message until the folder is fixed. An unreadable summary blocks merging when its device is
unknown, or when it
belongs to this device and could fall between these recordings. Usable start
times or UTC timestamp trip IDs can rule out damage outside that interval;
damage known to belong to another device does not block the action. The trip
page explains when a damaged summary prevents establishing the next trip.

The merged trip replaces its parts in the list, map, both exports and totals.
For traceability, exported `trip_id` remains the first recording's trip ID;
`gpx_file` lists every original relative GPX filename in order, joined with
`; `. The list and trip page show those source filenames too. The merged URL
is a viewer record key, not a file in the data directory.
You can merge it with the next trip again to join a longer chain. Old trip
page links lead to the current merged trip. The map draws every part with a
visible break at unrecorded gaps. Mileage is recomputed over all points with
the same parser and `summarize()` rules as ingest, including straight-line
gap estimates and fix/movement filters; it is not the sum of the old rounded
distances. Duration runs from the first part's start to the last moving point
of the last part and includes the stop between recordings. The same New Year
rule divides the merged drive's saved total between years in `DISPLAY_TZ`.

A merged trip has one saved purpose, note and destination. If only one trip
has an annotation, that annotation is kept. If both do, merging is blocked:
choose which to retain by clearing **all three fields** on the other trip
and saving it first. Copy any text you want to retain before clearing it.
**Undo merge (split back)** restores every original trip and the annotations
saved on those trips when they were merged. Confirm the checkbox first:
annotation edits made to the merged trip are removed by Undo. The change
history records what was dropped (the merged trip's purpose, destination and
note) and what each original trip got back. Undo of a chain restores all
original recordings, rather than just its most recent pair.

A merge draws a straight line across the gap between its recordings, so that
stretch is already counted. If another recording from the same device, from
inside that gap, is uploaded after the merge (the tracker was away from home
when it was driven), the viewer would count the drive twice. The trip list
marks such a trip **Inside a merged trip**, and the admin year review lists
them in "Trips inside a merged trip". Totals and exports are not changed for
you: open the merged trip and undo the merge (then merge the three recordings
in order), or, as admin, remove the later recording.

An admin removes a merged trip as a whole; an original part cannot be removed
on its own. Undo merge is refused while the merged trip is removed from the
log. A merge that would include a trip from a closed year is refused, and a
closed year blocks edits to trips in that year.

Merging never edits or deletes anything under `./data`. The viewer adds
`trip_merges` (merged annotation and cached summary), `trip_merge_parts`
(ordered original keys), and `trip_merge_aliases` (earlier merged links) to
`annotations.db` in the `viewer-db` volume. Startup migrates existing databases
in a serialized SQLite transaction so concurrent workers preserve existing
annotations. The original annotation rows remain intact. Back up this database
alongside the trip files: losing it also loses merges.

The cached summary is refreshed only when a part's GPX/JSON size or nanosecond
modification time changes. File identity (the inode number) is not used,
because a restore or migration to another disk changes it for every file:
copying the trip files elsewhere with their times preserved does not refresh a
merged trip. Older merge fingerprints that carry an inode are rewritten once on
first use without recalculating their stored mileage. A change to the shared
summary rules (`SUMMARY_VERSION`) does not rebuild it by itself, the same as the
plain trips' stored summaries, which keep the rules of the day they were
written; otherwise one release would total merged trips under new rules and
everything else under old ones. Ordinary list views
read the cache without parsing every merged track. Both images ship the shared
rules in `common/trip_summary.py` through the default-deny Docker build context.
If a source disappears or becomes unreadable, the merged trip is flagged as
unreadable and excluded from totals, rather than showing stale mileage. The
list's warning includes Undo so the originals can still be restored. Merge,
Undo and annotation changes require a logged-in session and POSTs protected
against cross-site request forgery (CSRF).

### Endpoint place suggestions

The installer's **Address lookup** step configures this optional feature; see
[the installer choices and by-hand setup](../DEPLOY_DEBIAN.md#address-lookup).

The trip's **Map / Edit** page can suggest readable start/end places using a
Nominatim-compatible reverse-geocoding service. It is **disabled by default**.
Set `VIEWER_GEOCODER_URL` in the private `viewer.env` to the full reverse endpoint
of a self-hosted instance, for example `http://<geocoder-host>:8080/reverse`, or a
provider whose terms permit private vehicle-trip endpoints and indefinite local
caching. This first version accepts endpoints without URL credentials or API-key
query parameters. Restrict a self-hosted instance to your private network; use
HTTPS for an external provider. An optional `VIEWER_GEOCODER_USER_AGENT` identifies
your installation using the contact format required by your provider. Neither
setting is shown in the viewer. Restart the viewer after changing configuration:

    SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build mileage-viewer

HTTPS providers must have a certificate trusted by the container. A private
certificate authority (CA) can be supplied via `SSL_CERT_FILE` pointing to a
CA bundle mounted inside the
container; certificate verification is never disabled. Lookup requests prefer
English names (`accept-language=en`).

Suggestions are formatted as a standard address, not the provider's long
`display_name`. In the US that is `<house number> <road>, <city>, <ST> <postcode>`,
for example `123 Example Road, Sampletown, ST 00000`, with the two-letter state.
A business or airport name goes first when the provider returns one. Elsewhere the
state and country are spelled out. Neighbourhood, suburb and county names are always
left out, and so is the country for US addresses. Any part the provider doesn't
return is skipped. If the provider returns no structured address, the viewer shows
its `display_name` instead. After upgrading to this format, a suggestion cached in
the old format is looked up again the next time you click it. Destinations you
already saved are never changed.

**Do not configure the public `nominatim.openstreetmap.org` service.** Its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/) excludes
vehicle-tracking applications and says not to submit confidential material.
The viewer rejects `openstreetmap.org` hosts. Map tiles remain independent of
address lookup. Google Maps/Places is not a provider in this implementation.

On wide windows, the annotation panel sits left of the map; on narrower windows
it comes first, above the map. Choose **Business**, **Personal** or **Other** for
the purpose. Click the selected purpose again to clear it, or choose **None**
(which also works without JavaScript). Tab reaches the radio group; arrow keys
and Space select a choice. Destination and Note are saved with **Save**.
The endpoint places, split-drive controls and removal controls follow the map.
Closed years show a read-only summary in the panel; removed trips stay read-only.
On wide windows the date and stats sit beside Previous/Next, wrapping below when
space is limited. Phones use compact stats and a **Menu** button in the shared
header for version, device health, account, theme and logout controls. Without
JavaScript those header controls remain in a wrapped row.

Click **Look up start place** or **Look up end place** to send only the selected
recorded endpoint to your configured provider. **Look up both places** looks up
start then end, skipping results already shown (including no-match results).
Clicks made during a lookup wait in order; a waiting label confirms the click.
No lookup runs when viewing a page unless you opt in with
`VIEWER_GEOCODER_AUTO_LOOKUP=true` in `viewer.env` (empty/unset or `false` is off;
other values prevent startup). This is intended for a self-hosted provider:
with a third-party provider every trip opened sends its uncached endpoints
unless you turn automatic lookup off. When this setting is enabled and address
lookup is configured, an **Auto lookup: On/Off** switch appears between
**Previous** and **Next**, immediately left of **Next**. It starts **On** and
remembers your choice in this browser. Turning it **Off** stops new automatic
lookups; requests already running or waiting may finish. The manual lookup
buttons appear immediately when Off. While On they are hidden; a failed
endpoint shows **Try again**. Turning it **On** looks up the open trip's uncached endpoints
straight away. The switch appears only when the administrator allows automatic
lookup, and needs JavaScript.
Cached endpoints send nothing, and removed trips never look up. Exports, uploads
and background work never look anything up. The page explains this before
the action. No full track, trip/device ID, note, category or destination is sent.
Your provider can see the coordinate and server IP and may log the query; choose
a provider and retention policy appropriate for your location data.

Results are suggestions for the nearest mapped object, which may be on another
street or may omit the business name. They do not verify a visit or correct GPS
drift. Missing or invalid recordings cannot be looked up. The **End:** line beneath Destination shows the lookup status or suggestion.
Click its **Use** button to replace the editable Destination field, check it,
then **Save**. Use stays disabled until a suggestion is available.
With **Auto lookup: On**, an empty Destination is filled with the end
suggestion as soon as it is known, and a note under it says so. This happens
only on a trip that has never had an annotation saved: once **Save** has stored
a purpose, note or destination for a trip, its Destination is never filled
automatically again, even if you later clear it and save it empty. **Use**
still fills it in one click. A destination that was saved, typed or chosen
with **Use** is never replaced. The filled text is not saved until you click
**Save**, so check it first.
Lookup never changes a saved annotation. The saved destination appears in the
trip list and CSV/XLSX exports through the existing annotation flow. Exports do
not perform lookup or add unreviewed address suggestions.

Positive and no-match results are cached indefinitely in `annotations.db`, keyed
by provider URL and exact coordinates, across trips, restarts and workers. The
cache contains sensitive place text and is included in existing SQLite backups;
protect those backups as you protect trip data. Changing provider URL uses a
separate cache namespace. Clearing the URL disables lookup and hides suggestions
without deleting the cache or changing saved destinations. To refresh all results,
stop the viewer, back up the database, delete rows from `geocode_cache` using
SQLite, then restart. A future lookup will query the configured provider again.

A shared gate serializes requests across workers, with at least 0.1 seconds
between a successful call finishing and the next call by default. This default
suits self-hosted Nominatim. Set `VIEWER_GEOCODER_INTERVAL` in `viewer.env`
(0.1 to 60 seconds) to match a third-party provider's rate policy, for example
`1.1` for one request per second. **Existing third-party installs with no setting
will send lookups up to ten times faster after updating**; set the interval
before updating.

A lookup that arrives while the gate is closed waits instead of failing, within
a total budget of two seconds:

- **During the normal gap** after a successful lookup, it waits out the rest of
  the gap, if that is no more than the configured interval and no more than two
  seconds.
- **While another lookup is running** (for example, you clicked Previous/Next
  before the last page's automatic lookup finished), it checks again every
  0.05 seconds until that lookup finishes, then waits out the normal gap. A
  self-hosted provider normally answers well inside the budget.
- **After a provider failure**, it returns the busy message at once: the
  failure cooldown below applies.

If the budget runs out, it returns the usual busy message (HTTP 429). When the
gate is closed only by the normal gap after a success (for example, an interval
above two seconds, so **Look up both places** reaches the second endpoint
early), that reply also says how long the gap has left, as a `retry_after`
number of seconds in the JSON and a `Retry-After` header. The trip page then
shows **Waiting for the lookup provider…**, waits that long (up to 60 seconds)
and sends the same lookup once more; later lookups wait behind it. If that
retry is refused too, the page shows the error and **Try again**. A busy reply
during a running lookup or after a provider failure has no wait time, and the
page does not retry it. The database write
lock is released while waiting, so annotation saves can continue, and a cached
result never waits. The gate records whether it closed after a success, during a
running lookup or after a failure, so the viewer never has to guess from the
time left. A running lookup's reservation covers the lookup deadline and cleanup
plus five seconds of margin.
Provider failures trigger a five-minute cooldown (longer `Retry-After` values are
honoured up to seven days). The server never retries a provider call by itself,
and uses no redirects or ambient HTTP proxies. Responses have a 64 KiB size limit and a
ten-second whole-request deadline, including DNS resolution, connection,
TLS, response headers, body and chunked framing. Each lookup runs in a
short-lived subprocess; at the deadline the viewer kills it, closing its
sockets, and allows up to one additional second for cleanup. A timeout returns
"provider unavailable" with the usual failure cooldown, freeing the viewer
request thread. Each viewer worker allows at most one provider subprocess,
including any child still awaiting operating-system cleanup. This limit does
not depend on Gunicorn's worker timeout.
Ordinary busy/unavailable/no-match responses leave the map,
manual annotations and exports usable. Lookup is authenticated and CSRF-protected;
upstream errors, response bodies, coordinates and URLs are not logged by lookup.

An unusually long provider `Retry-After` can suspend new lookups for up to seven
days. After fixing the provider, an administrator can stop the viewer, back up
`annotations.db`, delete its `geocode_gate` row using SQLite, then restart to clear
that cooldown. Do not clear the gate while a lookup is running.

## Viewer Login

The viewer has two roles. The existing login in `viewer.env` is the admin
login; everyday logins for whoever annotates trips are stored in
`annotations.db`. The admin login stays outside that database, so losing the
viewer database does not remove the admin credentials.

| Permission | Everyday login | Admin login |
|---|---|---|
| Trip list, map, annotations, place lookup, merge and undo merge | Yes | Yes |
| CSV/XLSX export and Device health | Yes | Yes |
| Change own password | Yes, on `/account/password` | Through `install.sh` |
| Remove/restore trips | No | Yes |
| Manage everyday logins | No | Yes |
| Change history and year review | No | Yes |
| Close or reopen a year | No | Yes |

The admin page at `/admin` identifies the admin as **Admin (set in
viewer.env)** and lists everyday logins. The admin can add, reset, disable,
enable or remove an everyday login there. The admin account itself is not
managed on that page. Everyday logins can also be added by `install.sh` or
`manage_users.py`.

The admin credentials live in `viewer.env` (separate from `.env`, so the
viewer never holds a copy of the ingest token):

    cp viewer.env.example viewer.env
    chmod 600 viewer.env

Generate the password hash — you're prompted for the password, it isn't
echoed, and the plaintext is never written anywhere:

    SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose build mileage-viewer
    docker compose run --rm --no-deps mileage-viewer python -c \
      "from getpass import getpass; \
       from werkzeug.security import generate_password_hash as g; \
       print(g(getpass('password: ')))"

Generate a session key:

    openssl rand -hex 32

Put the admin username, hash and session key in `viewer.env`,
**single-quoting the hash** (it contains `$`, which shell and Compose
expansion will otherwise eat):

    VIEWER_USERNAME=<admin>
    VIEWER_PASSWORD_HASH='scrypt:32768:8:1$RxK2...'
    VIEWER_SECRET_KEY=<the openssl output>

The username and hash above are for the admin. The admin password is changed
by rerunning `install.sh`. Everyday logins have their own passwords and can
change them at `/account/password` after entering the current password.

To add or manage an everyday login from the server, run the CLI in the viewer
container. `list` shows each account as enabled or disabled; it never prints
passwords or hashes. The admin page is the other way to manage these accounts.

```bash
docker compose run --rm --no-deps -T mileage-viewer python manage_users.py list
```

The CLI commands are `list`, `add <user>`, `reset-password <user>`,
`disable <user>`, `enable <user>` and `remove <user>`. `add` and
`reset-password` read the new password from standard input, not from an
argument or environment variable. For example, prompt for it without echoing
it, then pipe it to the CLI:

```bash
read -rs -p 'Everyday password: ' new_password; printf '\n'
printf '%s\n' "$new_password" | docker compose run --rm --no-deps -T mileage-viewer python manage_users.py add '<user>'
unset new_password
```

Usernames are 1–64 ASCII letters or digits, periods, underscores, at signs
or hyphens, and must not start with `replace-me` (ignoring case).
An everyday username cannot match the admin username, ignoring
case. Passwords must be 8–1024 characters. The commands return 0 for success,
1 for an operational error, 2 for usage or an unknown command, 3 for invalid input,
4 when the login does not exist, and 5 when `add` finds that it already exists.

How authentication behaves:

- Every route except `/login` and `/health` needs a session. `/health` stays open
  for Docker's healthcheck. It exposes the server version without a login to
  anyone who can reach the port on the LAN; it contains no trip or device data.
- Session cookie is HttpOnly, SameSite=Lax, 30-day lifetime.
- All state-changing forms carry CSRF tokens.
- Five bad passwords from one IP lock that IP out for 15 minutes. Failures
  count until there has been a 15-minute pause without one; the next failure
  then starts a fresh count. The counter is in SQLite, not process memory,
  so both gunicorn workers and their threads share it across restarts. The count
  is per IP across all usernames. A successful login, or your own password change, clears
  only the failures typed against that same username, so an everyday login
  can't reset guesses at the admin password.
- The service **refuses to start** if the username, hash, or secret key
  is missing or malformed — a viewer that silently comes up with auth
  broken is worse than one that doesn't come up, because you'd never
  notice.

`VIEWER_SECRET_KEY` must stay stable. Changing it logs you out
everywhere, and it has to be identical across workers — which is why
it's configured rather than randomly generated at startup.

To change the admin password, rerun `bash install.sh` and answer `n` when it
asks whether to keep the existing login. You can also use the hash-generation
steps above, replace `VIEWER_PASSWORD_HASH` in `viewer.env`, and run
`docker compose up -d mileage-viewer`. Changing a password invalidates the
existing sessions for that login; a self-service password change keeps its
current session and ends the login's other sessions.

## Timezone

Trip times are **stored in UTC** and **displayed and exported in the
timezone you choose** (`DISPLAY_TZ`). The raw GPX and the summary JSON both
keep UTC; only what you read on screen and in the exports is converted.

This matters because the export is an IRS mileage record. A 6:00 pm drive
stored as `01:00Z` would otherwise appear on the *following* day, and a
New Year's Eve evening trip would land in the wrong tax year entirely.

There is no default zone. `install.sh` asks for one and writes it to
`.env`:

    DISPLAY_TZ=America/Chicago

`docker-compose.yml` passes that one value to **both** services. The viewer
uses it to render times; the ingest service uses it to decide which
`data/YYYY-MM/` folder a trip is filed under. A trip is filed under the
local month in which the drive **ends**, and dated by its local **start**.
A drive that starts on December 31 and ends after midnight sits in the January
folder and shows as December 31, but each year counts only its share of the
miles (see the New Year rule in the viewer section). `install.sh --check`
confirms both running containers have the value `.env` holds.

Use a named IANA zone (`America/Denver`, `Europe/London`), never a fixed
offset like `-08:00`: in zones with daylight saving time (DST), a fixed offset
is an hour wrong for part of the year. Both services refuse to start if
`DISPLAY_TZ` is unset, the zone name is unknown, or the `tzdata` package is missing,
rather than guessing a zone or quietly falling back to UTC.

After changing it, run `docker compose up -d` (a plain restart keeps the
old value). Trips already filed stay in the month folder they were filed
under, but the zone decides more than how times read:

- Displayed and exported times change.
- **Which year counts the miles follows `DISPLAY_TZ` too.** Local dates and
  New Year shares are worked out at read time. After a zone change, a drive
  near New Year can move to the neighbouring year, gain or lose a split, or
  have different shares. That includes a
  year you already closed: its live totals and exports change. Saved totals
  from the close stay unchanged; the admin year review compares them with the
  current figures and lists trips that joined or left the year. The timezone
  change itself is not logged. Pick the right zone before closing a year; if
  you must change it afterwards, check that comparison and the exports.

The containers' `TZ`, which only affects log timestamps, follows
`DISPLAY_TZ` too (UTC if it's unset).

## Security Notes

Both services are designed for LAN use only. Ingest authenticates with a
bearer token; the viewer with a session login. Neither should be exposed
to the internet: the session cookie and the ingest token both travel over
plain HTTP, so anyone able to observe the traffic can replay them. The
login is defence in depth against other devices and people on your
network, not a substitute for TLS (Transport Layer Security).

Every viewer response (login and error pages, static files and exports too)
carries `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY`
and `X-Content-Type-Options: nosniff`, so another web page on the same host
or home domain can't show a viewer page in a frame and steer the admin's clicks
onto its buttons. A reverse proxy must not remove these headers.

For HTTPS through Caddy or Nginx Proxy Manager (NPM), set `VIEWER_COOKIE_SECURE=true`
in `.env`, then run `docker compose up -d`. Set `VIEWER_TRUSTED_PROXIES` to
the proxy's peer address so login lockout can count each client separately.
NPM also needs the Advanced setting and forged-header check in the guide
below: its defaults let LAN clients forge their address and evade lockout.
Use a strong viewer password with either proxy.
`VIEWER_BIND_ADDR` overrides `BIND_ADDR` for port 8091 only; empty keeps the
existing binding. A host-networked proxy can use `127.0.0.1`, closing the
viewer HTTP port on the LAN; a bridge-networked proxy needs the host's LAN IP.
Keep ingest unproxied on `BIND_ADDR` port 8090. Follow
[Behind a reverse proxy](../DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager)
for private HTTPS names, trusted proxy discovery and Tailscale access.
If it also fronts ingest, it must accept request bodies of at least
`INGEST_MAX_UPLOAD_BYTES` (16 MiB by default; firmware keeps each piece under
8 MiB). Its request-body read and proxy timeouts must allow a slow upload;
gunicorn allows 900 s. nginx's `client_max_body_size 1m` default (1 MiB) is
a common trap: a proxy's HTTP 413 sets the trip aside as `.bad` on the card,
never retried, so it is missing from the mileage log. For example, in the
nginx `server` or `location` block serving ingest, set
`client_max_body_size 16m;`, `client_body_timeout 900s;` and
`proxy_read_timeout 900s;` (raise the body limit if you raise the ingest cap).
For diagnostics (`/v1/device-events`), a 413 instead retains events and retries
with smaller batches and backoff; even a single event still too large is kept.

Both containers run as a non-root user. The viewer mounts the trip data
read-only, so even a full compromise of the viewer cannot alter the GPX
files or their derived summaries. The data directory is mounted from the
host, so trip files persist across container rebuilds; viewer
annotations persist in the `viewer-db` volume.

## Backup

Back up the trip files in `./data` and the viewer's `annotations.db` in the
`viewer-db` volume. Annotations hold the business/personal tags, purposes and
destinations; they cannot be rebuilt from the trips. The database also holds
everyday login names and password hashes, trip removals, change history and
closed-year state. These records are included in the normal database backup
and restore. The admin login remains in `viewer.env`. Restoring an earlier
snapshot restores everyday logins and password hashes as they were at that
time, so a password reset or account removal made later can be undone by a
restore. Backups also preserve
`data/diagnostics/events.db` (device history and current health) and, when its
`monitor-state` volume exists, `/state/monitor.db` (alert delivery state and
export cursors).

On Debian, `backup/backup.sh` runs daily through the systemd timer in `backup/`.
On Unraid, a persistent cron file calls `backup/scheduled.sh` as root, with
nightly and hourly catch-up entries. Status and logs stay under `backup/state/`
and `backup/logs/` in the install folder. See [Unraid backups](../DEPLOY_UNRAID.md#step-4--set-up-and-check-backups)
for the schedule, mount guards, root-owned config and persistent SSH key.
The backup script uses
SQLite's backup API and checks each database's integrity. A throwaway viewer
container reads annotations and diagnostics (its `/data` mount is read-only);
a throwaway monitor container reads monitor state, even if the monitor service
is stopped. A missing diagnostics DB means no diagnostics yet. An absent
monitor volume or missing monitor DB is recorded and skipped. Discovery reads
the project name from `docker compose config --format json`, then lists volumes
with both `com.docker.compose.project=<project>` and
`com.docker.compose.volume=monitor-state` labels. This honors project overrides
and custom volume names without recent Compose flags. Config and parser errors
are suppressed to keep service credentials out of logs. If discovery fails or
returns an ambiguous result, one warning is logged, `INFO` records
`monitor=unknown`, and trips, annotations and diagnostics are still backed up.
No monitor helper is run for an absent or unknown volume during backup, and no
optional service is enabled by a backup.

The script copies trip files under ingest's upload lock, excluding the live
`diagnostics/events.db` and all its SQLite sidecars (`-journal`, `-wal`, `-shm`).
Database snapshots sit beside `annotations.db` as `events.db` and `monitor.db`,
when present. All database files are covered by `SHA256SUMS`; `INFO` records
annotation/event counts and monitor status. The script keeps dated local
snapshots, rsyncs them to another machine, and verifies that copy byte for byte.
The off-host destination must hold a marker file, `.mileage-backup-target`;
without it the backup unit fails rather than copying. `install.sh` writes the
marker for you, and `backup/backup.sh --mark-target` writes it if you set
`REMOTE` by hand. `REMOTE_PRUNE` defaults to `no`; its rules, when you turn it
on, are in `DEPLOY_DEBIAN.md` Step 10.
Links anywhere under `data/` resolving outside it are logged as
`WARNING: not backed up: data/<path> -> <target>` (first 20, then a count
of the rest). Their contents are missing, including from earlier snapshots
taken while the link existed. `INFO` records `links_not_backed_up=N` (missing
means zero in an older readable `INFO`). If the link check fails, the value is
`unknown`: linked items were **not checked**, so inspect the backup log, fix
the error and rerun the backup. Status and restore checks report this plainly;
an unchecked run always makes a new snapshot. A changed count also makes a new
snapshot. Working relative internal chains and `*.tmp` are not reported. Move the
outside contents into `data/` and rerun the backup. The
copy and deep check still run and `last-success` advances, but missing linked
contents make the run exit non-zero and withhold `PING_URL`. Unraid status
shows `last_backup_links_not_backed_up`; `install.sh --check` warns when it
is above zero or `not checked`. Missing or unreadable backup information shows
`unavailable` in status: check the backup disk/mount, `latest`, and `INFO` before
assuming a link check failed. With a recent success, the installer warns when
`latest/INFO` is unavailable, unless another backup/destination check already
explains the problem. A first backup failing only for missing linked contents
warns and install continues.

Absolute links inside `data/` get a separate warning: the link
itself will not be restored, but its target is saved under its real path
(or the warning says that path is missing or excluded). This warning does not
fail the backup, withhold the heartbeat or increase `links_not_backed_up`.
If restore advice cannot be checked, backup, check and restore log a warning
and continue; inspect the log and rerun for that advice. Its scratch stays
outside snapshots, and failure preserves the outside-link coverage result.

Restore checks list links that will be skipped and can pass with a warning;
restore reports them too. For an absolute link, a relocated snapshot cannot
establish whether its original target is saved elsewhere in the backup or
absent, so the warning explains that uncertainty.

`INFO` also preserves the backup run's coverage result: if its count is positive,
both check and restore state that the backup recorded that many linked items
whose contents were not saved. Recover them from the original target or another
backup, move them into `data/`, and make a fresh backup. `unknown` means the
backup could not check; absent/unreadable `INFO` means that information is
unavailable. Zero or an absent field in older readable `INFO` leaves the
per-link warning neutral. A passing restore test verifies the saved copy;
it does not recover omitted contents.

Restore uses rsync
`--safe-links`: it skips absolute links and relative paths rejected by its text
check, keeping existing items at those paths. Too many leading `../` parts are
rejected; current rsync also rejects `..` after a named path component. The report
uses the installed rsync to decide which links survive. Rsync does not resolve other
links when deciding which links to recreate. For example, `relative ->
absolute/trip.gpx` is recreated even if `absolute` is an absolute link that is
skipped. Backup and restore warn that `relative` depends on `data/absolute` and
will dangle on an empty `data/`. Backup also reports whether the original target
was saved at its real path; a relocated restore cannot establish that through
an absolute link. Existing items may keep a chain usable on an existing install.
Recreated links with target problems are counted separately from skipped links.

Proven loops are named with a warning that the link loops and has nothing behind
it to back up; remove or correct the link. They do not fail the run, withhold the
heartbeat or increase `links_not_backed_up`. Check and restore give the same
explanation for saved relative loops, which are recreated. An unresolved error
or a walk exceeding 256 link steps without proving a loop still fails the
coverage check. Restore does not follow an absolute link back to the original
host to diagnose its target. A relative chain whose text passes rsync's check can
still lead outside `data/`; it is recreated and warned about, while its outside contents
still fail the backup run, withhold the heartbeat and count in
`links_not_backed_up`. Backup never follows links to copy their contents.
Replace an alias you need with a working relative internal chain or a real
folder or file, then make a fresh backup.

Snapshots contain standard GNU text `SHA256SUMS` checksums and a sorted,
NUL-delimited `SYMLINKS` list of copied link paths/text. Spaces, line breaks,
backslashes and leading dashes round-trip. GNU coreutils is required; Debian and
Unraid ship it. A tiny startup probe reads a one-byte file twice to test checksum
escaping; an incompatible tool fails any backup needing escaped names, without
rehashing trip data. Older snapshots remain restorable. The first run after
updating makes one extra snapshot only if the previous one lacks `SYMLINKS`.
Copied link adds, removals, retargets or swaps with real folders also create a
snapshot; unchanged links dedupe and excluded links do not affect comparison.
Failure to write either manifest fails the run and withholds the heartbeat;
inspect the log and rerun.

Checks compare the links on disk with `SYMLINKS`, naming each added, removed or
retargeted link. Older snapshots without that list still pass with a note that
their links are not checked.

To check files by hand with GNU coreutils, run `cd <snapshot>` then
`sha256sum -c SHA256SUMS`. Every listed file must say `OK` and the command must
exit 0. For the complete check, including unlisted files, links and databases, use
`restore.sh check <snapshot>`.

Local cleanup and pruning make only directories writable before removing them;
kept local snapshots retain file modes. Remote pruning uses only rsync, limited
to selected snapshot names, and never mirrors or deletes the remote root.
A read-only file shared between snapshots may become owner-writable on the
remote after pruning; contents and checksums are unaffected.

The nightly local trip copy compares contents with rsync `--checksum`, reading
live files and their previous copies even when size and time match: a local
warm-cache test of 3,000 files (1 GB) took 0.46 s versus 0.14 s without it.
A slow mounted destination reading 10–30 MB/s would need about 33–100 s per GB
read; off-host copying keeps its metadata test, with the newest copy checked
every run and older copies checked in the weekly rotation.
The comparison holds the ingest upload lock while reading every live file
(perhaps 15–30 s on a cold disk with years of trips), so an arriving upload may
wait past the tracker's 10 s reply timeout and be retried later.

Each SQLite snapshot is consistent individually; the databases and trip files
are captured in sequence, rather than in one shared transaction.

After today's backup is copied, a periodic deep check reads older snapshots in
full. `BACKUP_VERIFY_DAYS=7` defaults to weekly; whole days 0 to 36500 are allowed,
and `0` disables periodic checking. `BACKUP_VERIFY_COUNT=3` selects up to three
older local snapshots, always the oldest plus rotation through the rest. Counts
must be 2 to 1000 to leave room for rotation. UTC snapshot names keep the rotation
working after pruning. Each selected copy must pass `SHA256SUMS` (unlisted files
also fail, except root `INFO` and `SHA256SUMS`), then a checksum rsync dry run
checks REMOTE against that verified local copy. If REMOTE's oldest snapshot is
no longer local, it is fetched into private scratch under `BACKUP_DIR`, checked
against its manifest and removed. This supports both mounted paths and SSH
REMOTE, with no extra remote shell commands. Allow space for that extra snapshot.

Whole-file reads can take minutes or hours; the existing 600-second rsync I/O
timeout and six-hour systemd/Unraid wrapper limit still apply. Scratch is cleaned
on exit or termination, or by the next backup after a forced kill. An atomic
`BACKUP_DIR/verify-state` records UTC check time, result, rotation cursor and
pending retries. `scheduled.sh --status` exposes `last_deep_check` and
`last_deep_check_result`; `install.sh --check` and `restore.sh check` also show it.
`BACKUP_DIR/last-success` advances once today's backup has completed and been
copied off-host, before the deep check. A failed check still exits non-zero:
the Debian unit fails or Unraid records a failed `last_attempt_status`, and
`install.sh --check` reports the failure as a problem. The `PING_URL` heartbeat
is withheld, so an external monitor can alert. Failed or interrupted checks
retry on the next nightly backup run. Unraid's hourly overdue catch-up does
nothing while `last-success` is fresh. Retries keep the rotation
position and protect failed snapshots from pruning until success (or checking
is disabled).

One damaged file can affect every snapshot sharing its hard link. A failure
names the local or remote snapshot and file paths; check other copies with
`bash backup/restore.sh check <snapshot-name>` (REMOTE), or give a full local
snapshot path. After verifying a good REMOTE snapshot, use `rsync -ac` to copy
the matching good file back into the affected local snapshot and recheck it;
avoid writing through shared hard links in place or changing the manifest to
hide damage. The [recovery example in Debian Step 10b](../DEPLOY_DEBIAN.md#10b--configure-it)
shows the command and cautions. Run `bash backup/backup.sh --verify` to complete
a normal backup and force a deep check now, even if not due or periodic checks
are off. Check other affected snapshots by name too: one rotating run samples
history and does not verify every snapshot.

`backup/restore.sh check` fetches the newest off-host snapshot, verifies every
file and database, and reports annotation/event counts and monitor state tables
without changing live data. `backup/restore.sh restore` runs those checks,
requires `RESTORE` confirmation (or `--yes`), stops ingest, viewer and any
installed monitor, and saves current databases and replaced trip files under
`pre-restore-<time>/` before restoring. Database writes use the owning service's
throwaway container; an already running monitor is restarted afterward. On a
replacement server, a saved monitor DB can populate its volume without enabling
the monitor. Older snapshots still restore trips and annotations: missing new
DB snapshots are reported and current databases are left unchanged. Any old
live diagnostics copy under `data/` is excluded from restore too.
After a restore run as root, files have mode 600 and folders mode 700; if the
snapshot's modes differed, the next backup makes one new snapshot, as expected.

If monitor discovery fails during restore, the script warns and restores trips,
annotations and diagnostics, leaving the monitor service and its database alone.
To recover monitor state separately, fix discovery and rerun the restore, or
follow the manual monitor recovery procedure in `DEPLOY_DEBIAN.md` Step 10.

The Debian timer runs as the server login, while ingest writes private trip files
as UID 1000. Use a UID 1000 login with this setup; giving `data/` to UID
1000 lets ingest write but does not make the files readable by another
account's backup timer.

Setup, the restore test and the restore procedure are in
`DEPLOY_DEBIAN.md` Step 10.

## Device diagnostics

Ingest also accepts protocol-v1 diagnostics at `POST /v1/device-events`. The
same bearer token authenticates it; `X-Device-Id` is required and must be a
non-personal lowercase label of 1-32 letters/digits, `_` or `-`, starting with
a letter/digit. Bodies are capped at 32 KiB even for chunked requests.

To check that diagnostics work, look at **Device health** in the viewer after
the tracker's first real upload; it lists the device and its latest snapshot.
Don't test with a hand-made request. Any new `X-Device-Id` creates a device
that cannot be removed later, and with the optional monitor on it would
eventually raise a "no contact for seven days" alert for a tracker that doesn't
exist. Using your real device ID is no safer: a made-up `status_sequence` would
set that device's snapshot high-water and interfere with the one-time
diagnostics enrollment (see [DIAGNOSTICS.md](../DIAGNOSTICS.md)).

Devices must durably increase `status_sequence` for each new snapshot across
boots; reuse it only for identical retries. Event retries are acknowledged
again; conflicting event IDs or equal revisions with changed snapshots return
409 and commit nothing. Validation errors return 400, oversized bodies 413,
authentication failures 401, and storage/commit failures 5xx without an ack.
Snapshots more than 1,000,000 above the stored revision (or zero for a new
device) are discarded without changing current health. Valid events still
commit and receive a normal 200 acknowledgment so firmware can drain its queue.
See the explicit [admin recovery command](../DIAGNOSTICS.md#admin-high-water-recovery)
for a mistaken enrollment or poisoned high-water.
See [DIAGNOSTICS.md](../DIAGNOSTICS.md) for the full protocol and ordering rules.

`data/diagnostics/events.db` belongs to ingest, alongside trip data. Valid
`X-Device-Id` headers on successful GPX uploads refresh contact, even before
firmware sends diagnostics. Missing/invalid headers create no diagnostic device;
GPX creator is never used to guess one. Diagnostics failures do not fail GPX.
Open **Device health** in the viewer header after signing in to see current
status, active faults and event history at `/device-health`. The backup
procedure above preserves diagnostics through SQLite snapshots rather than
copying the live file.

Build both images from this directory with
`SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose build`. Both service
Dockerfiles use this directory as their context and include `common/`, including
`catalog.json`. Copy the whole `server/` tree and its `.dockerignore` when
installing; the allowlist keeps `.env`, `viewer.env`, data and backups out of
both build contexts. Ingest refuses to start without a valid catalog.
