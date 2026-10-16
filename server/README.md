# Mileage Tracker Server

This folder is the server half of the GPS mileage tracker. The board in
the car uploads each finished trip here over your home WiFi, and you
review, tag and export the trips in a web page. The
[top-level README](../README.md) explains how the pieces fit together and
the order to set them up in.

It runs as two default Docker containers, with two optional services, all
defined in `docker-compose.yml`:

| Service | Port | What it does |
|---|---|---|
| `mileage-ingest` | 8090 | Accepts trip uploads from the board (bearer token) and stores each one as GPX plus a JSON summary |
| `mileage-viewer` | 8091 | A web page, behind a login, to list trips, check them on a map, tag their purpose, and export CSV/XLSX |
| `mileage-monitor` | 8093 | Optional `monitor` profile: authenticated Prometheus metrics; current-fault and seven-day no-contact alerts |
| `ntfy` | 8092 | Optional `ntfy` profile: self-hosted phone notifications, authenticated topic access |

**To set it up, run `bash install.sh`** on the server, from this folder
once it's copied over (normally `~/mileage-tracker`). It does everything
below and checks each step. `DEPLOY_DEBIAN.md` at the top of the repo
walks through the same steps by hand. This file is the reference for how
the services behave.

`install.sh --check` leaves configuration unchanged but uploads and removes
a temporary test trip and can test the viewer login. Automatic backups
currently require the server login's numeric user ID to be **1000**
(`id -u`); the installer skips backups for other IDs. See
[the deployment prerequisites](../DEPLOY_DEBIAN.md).

## What the Ingest Service Does

When your T-Beam completes a trip and detects it's on your home WiFi,
it POSTs the trip as a **GPX** file to this service. The service verifies
an auth token, parses the GPX, and writes two files using atomic writes
so they can never be left partially written:

- `/data/YYYY-MM/<id>.gpx`  — the raw upload, byte-for-byte (the source
  of truth that any map tool can open directly)
- `/data/YYYY-MM/<id>.json` — a derived summary (distance, duration,
  speeds, bbox) so other tools can list and total trips without
  re-parsing every track

Distance is the great-circle (haversine) sum over consecutive fixes, with
three rules, all in `summarize()`:

- **Stopped steps don't count.** If the device's speed at both ends of a
  step is under 2 mph, the vehicle was parked or at a light, and the step
  is GPS scatter. Without device speed, distance/time is used instead.
- **Bad fixes are skipped.** That means fixes with HDOP above 5, and any
  step implying more than 60 m/s.
- **GPS gaps are bridged.** A new `<trkseg>` marks a gap (tunnel, parking
  structure). The straight line across it is the shortest possible path,
  so bridging can only undercount, and leaving the gap out lost real miles.
  A bridge needs timestamps at both ends and a plausible speed.

For completely timed trips, `end_time` is the last moving fix, so the firmware's 3-minute cooldown
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
provide a web UI, or classify trips. Those are separate concerns handled
by the `mileage-viewer` service, which reads these files independently.

## Directory Layout

    mileage-tracker/           # this folder, copied to the server
    ├── docker-compose.yml     # All Python builds use this server directory as context
    ├── .dockerignore          # Runtime allowlist; excludes secrets, tests and data
    ├── common/diagnostics/    # Shared validators and catalog.json (required)
    ├── .env.example           # Template for .env
    ├── .env                   # Upload token and BIND_ADDR (created by you or install.sh, never committed)
    ├── viewer.env.example     # Template for viewer.env
    ├── viewer.env             # The viewer login (created by you or install.sh, never committed)
    ├── monitor.env.example    # Disabled defaults; template for private monitor.env
    ├── mileage-monitor/       # monitor.py, gelf.py, prometheus.py + Dockerfile; Python stdlib only
    ├── ntfy/server.yml        # Optional ntfy deny-all config; no credentials
    ├── install.sh             # First-time install with checks; --check later
    ├── sample.gpx             # A short test trip for checking uploads
    ├── data/                  # Trip files (create it before the first start, see below)
    │   ├── diagnostics/events.db # Ingest-owned SQLite, created on first contact
    │   └── 2026-04/
    │       ├── 2026-04-14T13-45-22.gpx
    │       └── 2026-04-14T13-45-22.json
    ├── backup/                # Scheduled backup + restore scripts, systemd timer
    ├── tests/                 # The server test suite (not needed to run it)
    ├── mileage-ingest/
    │   ├── Dockerfile         # Container definition
    │   ├── requirements.txt   # Python dependencies
    │   ├── app.py             # GPX uploads + device-event endpoint (Flask)
    │   └── diag_store.py      # Diagnostics transactions and ordered health
    └── mileage-viewer/
        ├── Dockerfile
        ├── requirements.txt
        ├── app.py             # The web UI (Flask)
        └── static/            # Leaflet and its GPX plugin, bundled

## Optional diagnostics notifications

Follow [DEPLOY_DEBIAN.md Step 11](../DEPLOY_DEBIAN.md#step-11--optional-device-notifications)
to enable them manually. No subscriptions or cloud accounts are required.
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
Its versioned state DB, `/state/monitor.db` in `monitor-state`, persists
episodes, delivery intent, digest overflow, reminders, rolling-hour cap and
retry backoff. Use one replica and preserve the volume to retain deduplication.

Fault notifications require eligible severity from the current catalog and
a snapshot at most 24 hours old. Hold defaults to 300 seconds (critical skips
it), reminders to 24 hours, and the cap to six successful posts per rolling
hour. Overflow becomes bounded digests. Recovery needs an actually sent alert
and an ordered explicit clear; unknown, reboot or historical events do not
resolve faults. Delivery is at least once, with exponential retries from one
minute to one hour; a crash after acceptance can duplicate a message. Bodies
include code, catalog title/recovery and device label, with no links or context.

E7001, "No contact for N days", defaults to seven days of **server receipt
time**, including identified GPX uploads. It bypasses the device-fault severity
filter and hold, shares reminders/cap, and resolves on newer contact. There is
no watchlist; all devices remain monitored. Firmware heartbeat work is #125.

The bundled ntfy is locked down with `auth-default-access: deny-all`; create a
local user, grant only the alert topic, and give the monitor its access token.
Cache/auth DBs live in `ntfy-state`. Set `BIND_ADDR` before publishing port
8092. LAN-only delivery reaches the phone on home WiFi or a VPN; the owner
decides about remote access. iOS instant push needs
`upstream-base-url: https://ntfy.sh` plus the phone-facing base URL, relaying
only a free wake-up poke, not message text. Android needs no relay.
[ntfy documents the settings](https://docs.ntfy.sh/config/).
An existing self-hosted ntfy can be selected with `NTFY_URL` instead.

Optionally, the monitor also sends diagnostics as GELF to a Graylog you run
elsewhere (`GRAYLOG_*` in `monitor.env`, off by default; nothing here installs
Graylog). It forwards every diagnostic event once and each change in a
device/code condition's state (active/clear/unknown, plus E7001 silence), and
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

Consistent DB backups/restores, including monitor state, remain #129. The
existing trip backup does not include either optional named volume. Full
policy and schema details are in [DIAGNOSTICS.md](../DIAGNOSTICS.md).

## First-Time Setup

The easy way is `bash install.sh` from this directory. It sets up both
services, the viewer login and backups, checks each step, and tests an
upload end to end (`DEPLOY_DEBIAN.md` → The quick way). The steps below
set up both services by hand, in the order Compose needs them, all run
from this folder. Docker has to be installed first (`DEPLOY_DEBIAN.md`
Step 1).

1. Create the `data/` folder yourself, owned by UID 1000, the user both
   containers run as. If Docker creates it on first start, root owns it
   and every upload fails with a 500:

       mkdir -p data
       id -u                       # if this isn't 1000:
       sudo chown 1000:1000 data

2. Copy the example environment file to `.env` so you can fill in
   the real auth token:

       cp .env.example .env

3. Generate a strong random auth token and put it in `.env`:

       openssl rand -hex 32

   Copy the output and set `INGEST_AUTH_TOKEN` in `.env` to that value.
   You'll use the same token in the T-Beam firmware later. This token is
   separate from your SSH login and the viewer's username/password.

4. Create `viewer.env` and set the viewer login, as described under
   [Viewer Login](#viewer-login) below. Compose won't build or start
   anything, either service, until `viewer.env` exists.

5. Build and start both services:

       docker compose up -d --build

6. Verify the service is responding:

       curl http://localhost:8090/health

   You should see `{"service":"mileage-ingest","status":"ok"}`.

7. Test an authenticated upload with the sample trip, `sample.gpx`, which
   is in this folder:

       TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
       curl -X POST http://localhost:8090/upload \
         -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/gpx+xml" \
         -H "X-Device-Id: tbeam-01" \
         -H "X-Trip-Id: 2026-04-14T13-45-22" \
         --data-binary @sample.gpx

   You should see a JSON success response with a `distance_miles` value,
   and find two files at `./data/2026-04/2026-04-14T13-45-22.gpx` and
   `.../2026-04-14T13-45-22.json`. Delete both afterwards so the test
   trip doesn't end up in your mileage log.

   The `^INGEST_AUTH_TOKEN=` match above reads only the setting, not
   comments that mention its name. It assumes the unquoted hexadecimal
   token generated in step 3.

8. Bind the two default ports to the server's LAN IP by setting `BIND_ADDR` in
   `.env`, then run `docker compose up -d` again. UFW rules don't
   restrict Docker-published ports, so this is the setting that keeps
   them off other interfaces. After this, `localhost` no longer answers;
   curl the LAN IP instead. Details are in `DEPLOY_DEBIAN.md` Step 7.

## Daily Operation

Check the logs when you want to see what's happening:

    docker compose logs -f mileage-ingest

After changing `.env` or `viewer.env`, recreate the containers so they
pick up the new values (Compose only recreates the ones that changed):

    docker compose up -d

`docker compose restart` is not enough for that: it restarts a container
with the settings it was created with, so a new token or login wouldn't
take effect. Use it only when nothing in the config changed:

    docker compose restart mileage-ingest

Stop the service entirely:

    docker compose down

Update to newer dependency versions (after editing requirements.txt):

    docker compose up -d --build

## Inspecting Your Trip Data

The normal way to browse, audit, and export trips is the `mileage-viewer`
web UI (see its section in `docker-compose.yml`). But the summary files
are plain JSON, so you can also use any tool you like.

Count trips in the current month:

    ls data/$(date +%Y-%m)/*.gpx | wc -l

Sum total miles for a month with jq:

    jq -s 'map(.distance_miles) | add' data/2026-04/*.json

View the most recent trip summary:

    ls -t data/*/*.json | head -1 | xargs cat | jq .

Open a track on a map: any `.gpx` file opens directly in GPXSee,
gpx.studio, Google Earth, OsmAnd, or QGIS.

## Viewer / Export UI

A second service, `mileage-viewer`, gives you a web page to browse,
audit, and export your trips. It's defined in the same
`docker-compose.yml` and starts alongside ingest with
`docker compose up -d --build`.

Open it at:

    http://<server>:8091/

What it does:

- **Trip list** — trips with date, distance, duration, destination, and the
  purpose tag you've assigned. Select a year or All years to see matching
  trips and totals for business, personal, other, and unclassified miles.
- **Map audit** — click "Map / Edit" to see the track drawn on an
  OpenStreetMap map (Leaflet). This is how you eyeball a trip for GPS
  errors before trusting its mileage. Your trip coordinates are served
  from the LAN and drawn in your browser; only the base map tiles come
  from OpenStreetMap.
- **Annotate** — tag each trip business / personal / other and add a
  note (e.g. "Client visit — Acme Corp") and destination / place. Annotations are stored in a
  SQLite database in a separate `viewer-db` volume, kept apart from the
  immutable trip files.
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

Years are based on **trip start in `DISPLAY_TZ`**, including at the New Year
boundary. A trip crossing midnight belongs entirely to its start year; it
is not split. The storage folder is not used for filtering, except for
unreadable summaries (below). The viewer labels partly timed trips with
**Partial timing** and leaves
unknown duration and average speed blank. CSV/XLSX include `timing_status`,
`timed_from`, and `recorded_until`; older summaries without a status export
`unknown`. For partly timed trips, year selection and sorting
use `timed_from`, the first recorded timestamp in `DISPLAY_TZ`, so their miles
remain in year totals and exports. The date is labeled **Timed from** and
`year_basis` exports `first_recorded_time`. Their actual start may be in an
earlier year, which the viewer states explicitly. Wholly undated trips appear
only under All years. Direct exports
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

A trip whose summary file can't be read (damaged on disk, edited by hand,
or missing next to its GPX) has no miles the viewer can count. The trip list shows a warning
naming it and why, the CSV lists it with `row_type=unreadable` and blank
miles, and the XLSX lists it on an `Unreadable trips` sheet; both keep any
purpose, note and destination already entered. With no start date to go
by, it's matched to a year by its storage folder, which is named for the
trip's local end month: that year, plus the year before for a January
folder. `DEPLOY_DEBIAN.md` → Troubleshooting says how to rebuild the
summary from its GPX.

Endpoint coordinates come from the first and last recorded GPX track points,
so they are also available for existing summaries. They describe the raw
recording, including any final stationary samples; they are not geocoded
addresses or a corrected route. Missing, corrupt, or invalid GPX files leave
coordinates blank without preventing the rest of the export. Destination
is entered manually. On startup, the viewer automatically adds an empty
destination column to existing annotation databases while preserving their
categories and notes. Existing categories remain business / personal / other.

The viewer mounts `./data` **read-only** — it never modifies GPX or
summary files. The only thing it writes is the annotations database.

## Viewer Login

The viewer requires a login. Credentials live in `viewer.env` (separate
from `.env`, so the viewer never holds a copy of the ingest token):

    cp viewer.env.example viewer.env
    chmod 600 viewer.env

Generate the password hash — you're prompted for the password, it isn't
echoed, and the plaintext is never written anywhere:

    docker compose build mileage-viewer
    docker compose run --rm --no-deps mileage-viewer python -c \
      "from getpass import getpass; \
       from werkzeug.security import generate_password_hash as g; \
       print(g(getpass('password: ')))"

Generate a session key:

    openssl rand -hex 32

Put both in `viewer.env`, **single-quoting the hash** (it contains `$`,
which shell and Compose expansion will otherwise eat):

    VIEWER_USERNAME=<username>
    VIEWER_PASSWORD_HASH='scrypt:32768:8:1$RxK2...'
    VIEWER_SECRET_KEY=<the openssl output>

How it behaves:

- Every route except `/health` needs a session. `/health` stays open
  because Docker's healthcheck calls it and it reveals nothing.
- Session cookie is HttpOnly, SameSite=Lax, 30-day lifetime.
- The annotate and logout forms carry CSRF tokens.
- Five bad passwords from one IP locks that IP out for 15 minutes. The
  counter is in SQLite, not process memory, so both gunicorn workers
  share one view of it and it survives restarts.
- The service **refuses to start** if the username, hash, or secret key
  is missing or malformed — a viewer that silently comes up with auth
  broken is worse than one that doesn't come up, because you'd never
  notice.

`VIEWER_SECRET_KEY` must stay stable. Changing it logs you out
everywhere, and it has to be identical across workers — which is why
it's configured rather than randomly generated at startup.

To change the password later: regenerate the hash, update `viewer.env`,
then `docker compose up -d mileage-viewer`. That logs out every
existing session, so anyone still logged in has to sign in again with
the new password.

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
`data/YYYY-MM/` folder a trip is filed under. Because both read the same
line, a trip near a month boundary can't sit in one folder while
displaying a date in another. `install.sh --check` confirms both running
containers have the value `.env` holds.

Use a named IANA zone (`America/Denver`, `Europe/London`), never a fixed
offset like `-08:00`: most zones observe DST, so a fixed offset is an hour
wrong for half the year. Both services refuse to start if `DISPLAY_TZ` is
unset, the zone name is unknown, or the `tzdata` package is missing,
rather than guessing a zone or quietly falling back to UTC.

After changing it, run `docker compose up -d` (a plain restart keeps the
old value). Trips already filed stay in the month folder they were filed
under; only the displayed and exported times change.

The containers' `TZ`, which only affects log timestamps, follows
`DISPLAY_TZ` too (UTC if it's unset).

## Security Notes

Both services are designed for LAN use only. Ingest authenticates with a
bearer token; the viewer with a session login. Neither should be exposed
to the internet: the session cookie and the ingest token both travel over
plain HTTP, so anyone able to observe the traffic can replay them. The
login is defence in depth against other devices and people on your
network, not a substitute for TLS.

If you ever do put a TLS reverse proxy in front, set
`SESSION_COOKIE_SECURE=True` in `mileage-viewer/app.py`.

Both containers run as a non-root user. The viewer mounts the trip data
read-only, so even a full compromise of the viewer cannot alter the GPX
files or their derived summaries. The data directory is mounted from the
host, so trip files persist across container rebuilds; viewer
annotations persist in the `viewer-db` volume.

## Backup

Two things need backing up: the trip files in `./data`, and the viewer's
annotations database in the `viewer-db` volume. The database is the one
that matters most. It holds the business/personal tags and purposes, and
it can't be rebuilt from the trips.

`backup/backup.sh` handles both. Run daily by the systemd timer in
`backup/`, it takes a consistent SQLite snapshot (never a copy of the
live file), copies `data/` under ingest's upload lock, keeps dated,
checksummed snapshots locally, and rsyncs them to another machine,
then reads the copy there back to verify it. `backup/restore.sh check`
is the restore test: it pulls the newest off-host snapshot and checks
every file and the database without touching anything live.
`backup/restore.sh restore` puts a snapshot back.

The timer runs as the server login, while ingest writes private trip files
as UID 1000. Use a UID 1000 login with this setup; giving `data/` to UID
1000 lets ingest write but does not make the files readable by another
account's backup timer.

Setup, the restore test and the restore procedure are in
`DEPLOY_DEBIAN.md` Step 10.

## Device diagnostics

Ingest also accepts protocol-v1 diagnostics at `POST /v1/device-events`. The
same bearer token authenticates it; `X-Device-Id` is required and must be a
non-personal lowercase label of 1-32 letters/digits, `_` or `-`, starting with
a letter/digit. Bodies are capped at 32 KiB even for chunked requests. This
synthetic empty-event heartbeat returns `{"accepted":[]}` after committing:

```sh
curl -X POST 'http://<host>:8090/v1/device-events' \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Device-Id: tracker-1' \
  -H 'Content-Type: application/json' \
  --data '{"schema_version":1,"checkin":{"boot_id":"00000000000000000000000000000001","status_sequence":1,"uptime_ms":1500,"active":[]},"events":[]}'
```

Devices must durably increase `status_sequence` for each new snapshot across
boots; reuse it only for identical retries. Event retries are acknowledged
again; conflicting event IDs or equal revisions with changed snapshots return
409 and commit nothing. Validation errors return 400, oversized bodies 413,
authentication failures 401, and storage/commit failures 5xx without an ack.
See [DIAGNOSTICS.md](../DIAGNOSTICS.md) for the full protocol and ordering rules.

`data/diagnostics/events.db` belongs to ingest, alongside trip data. Valid
`X-Device-Id` headers on successful GPX uploads refresh contact, even before
firmware sends diagnostics. Missing/invalid headers create no diagnostic device;
GPX creator is never used to guess one. Diagnostics failures do not fail GPX.
The viewer's `/device-health` page shipped in #119; consistent SQLite backups remain
#129 (`backup.sh` currently rsyncs the live data directory).

Build both images from this directory with `docker compose build`. Both service
Dockerfiles use this directory as their context and include `common/`, including
`catalog.json`. Copy the whole `server/` tree and its `.dockerignore` when
installing; the allowlist keeps `.env`, `viewer.env`, data and backups out of
both build contexts. Ingest refuses to start without a valid catalog.
