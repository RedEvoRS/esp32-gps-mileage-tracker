# Mileage Tracker Ingest Service

A minimal HTTP service that receives trip uploads from a T-Beam GPS
mileage tracker and stores them as JSON files on disk, organized by
year-month.

## What This Service Does

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

`end_time` is the last moving fix, so the firmware's 3-minute cooldown
doesn't stretch every trip; `recorded_until` keeps the final timestamp.
Each summary records `summary_version`, and the raw GPX is never touched,
so summaries can be recomputed if the rules change. The tests are in
`server/tests/` (`python -m unittest discover -s server/tests`).

The service is intentionally minimal. It does not generate reports,
provide a web UI, or classify trips. Those are separate concerns handled
by the `mileage-viewer` service, which reads these files independently.

## Directory Layout

    mileage-tracker/
    ├── docker-compose.yml     # Compose definition
    ├── .env.example           # Template for secrets
    ├── .env                   # Your real config (created by you, never committed)
    ├── data/                  # Trip files live here (created on first run)
    │   └── 2026-04/
    │       └── 2026-04-14T13-45-22.json
    ├── backup/                # Scheduled backup + restore scripts, systemd timer
    ├── install.sh             # First-time install with checks; --check later
    └── mileage-ingest/
        ├── Dockerfile         # Container definition
        ├── requirements.txt   # Python dependencies
        └── app.py             # The Flask application

## First-Time Setup

The easy way is `bash install.sh` from this directory. It sets up both
services, the viewer login and backups, checks each step, and tests an
upload end to end (`DEPLOY_DEBIAN.md` → The quick way). The steps below
set up the ingest service by hand.

1. Copy the example environment file to `.env` so you can fill in
   the real auth token:

       cp .env.example .env

2. Generate a strong random auth token and put it in `.env`:

       openssl rand -hex 32

   Copy the output and set `INGEST_AUTH_TOKEN` in `.env` to that value.
   You'll use the same token in the T-Beam firmware later.

3. Build and start the service:

       docker compose up -d --build

4. Verify the service is responding:

       curl http://localhost:8090/health

   You should see `{"service":"mileage-ingest","status":"ok"}`.

5. Test an authenticated upload with the sample trip. A ready-made
   `sample.gpx` lives one directory up (in `server/`):

       TOKEN=$(grep INGEST_AUTH_TOKEN .env | cut -d= -f2)
       curl -X POST http://localhost:8090/upload \
         -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/gpx+xml" \
         -H "X-Device-Id: tbeam-01" \
         -H "X-Trip-Id: 2026-04-14T13-45-22" \
         --data-binary @../sample.gpx

   You should see a JSON success response with a `distance_miles` value,
   and find two files at `./data/2026-04/2026-04-14T13-45-22.gpx` and
   `.../2026-04-14T13-45-22.json`.

6. Bind both ports to the server's LAN IP by setting `BIND_ADDR` in
   `.env`, then run `docker compose up -d` again. UFW rules don't
   restrict Docker-published ports, so this is the setting that keeps
   them off other interfaces. After this, `localhost` no longer answers;
   curl the LAN IP instead. Details are in `DEPLOY_DEBIAN.md` Step 7.

## Daily Operation

Check the logs when you want to see what's happening:

    docker compose logs -f mileage-ingest

Restart the service after config changes:

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

Years are based on **trip start in `DISPLAY_TZ`**, including at the New Year
boundary. A trip crossing midnight belongs entirely to its start year; it
is not split. The storage folder is not used for filtering. Trips with
missing or invalid start dates appear only under All years. Direct exports
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

Trip times are **stored in UTC** and **displayed and exported in Pacific
Time** (`America/Los_Angeles`). The raw GPX and the summary JSON both keep
UTC; only what you read on screen and in the exports is converted.

This matters because the export is an IRS mileage record. A 6:00 pm drive
stored as `01:00Z` would otherwise appear on the *following* day, and a
New Year's Eve evening trip would land in the wrong tax year entirely.

Both services take a `DISPLAY_TZ` environment variable, set in
`docker-compose.yml`:

    - DISPLAY_TZ=America/Los_Angeles

Set it on **both** services to the same value. The viewer uses it to
render times; the ingest service uses it to decide which `data/YYYY-MM/`
folder a trip is filed under. If they disagree, a trip near a month
boundary sits in one folder while displaying a date in another.

Use a named IANA zone (`America/Denver`, `Europe/London`), never a fixed
offset like `-08:00` — Pacific observes DST, so a fixed offset is an hour
wrong from March to November. Both services refuse to start if the zone
name is unknown or the `tzdata` package is missing, rather than quietly
falling back to UTC.

`TZ` and `DISPLAY_TZ` are different knobs: `TZ` affects container log
timestamps, `DISPLAY_TZ` affects trip data. They're both set to Pacific
here, but changing one does not change the other.

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
files or their derived summaries.

Both containers run as a non-root user. The data directory is mounted
from the host, so trip files persist across container rebuilds; viewer
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

Setup, the restore test and the restore procedure are in
`DEPLOY_DEBIAN.md` Step 10.
