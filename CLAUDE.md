# GPS Mileage Tracker — Project Instructions

## What This Is

A DIY GPS mileage tracker for IRS tax deduction logging. Plug-and-forget
USB device for a car. Logs trips automatically; user annotates purpose later.

**Hardware:** LilyGO **T-Beam Supreme** with the u-blox **MAX-M10S GPS
version (not L76K)** (ESP32-S3 + SX1262 LoRa + AXP2101 PMU + microSD +
SH1106 OLED + PCF8563 RTC) at 915 MHz. 18650 battery. The selected trip
storage card is a **SanDisk 32 GB Max Endurance microSD**. Enclosure is a
PETG print of a published Supreme case.

**Old hardware, frozen branch:** the project started on the LilyGO TTGO
T-Beam V1.2 (ESP32 + NEO-M8N). It's retired: its 2.375 MB trip partition
held only ~6 h of driving between uploads. Its last firmware is on the
`tbeam-v1.2` branch, which is frozen and won't be updated: no fixes, no
backports. Never commit to it or base work on it; all work targets the
Supreme on `main`.

**Port in progress:** Stages 1–3 and the trip logger (Stage 4) run on the
Supreme: pins, rails, PMU power-off and USB wake were bench-checked
2026-10-18. Stage 4 stores trips on the microSD card, with the internal
2.375 MB LittleFS partition as fallback; the SD support is not yet
bench-tested, and the rest of the port (label `supreme-port`) is open. Don't assume a Supreme pin, rail or peripheral works
until its port issue is closed and it's verified on the bench.

## Architecture

Two-component system:
- **Firmware** (Arduino/C++) — runs on the T-Beam, logs trips, uploads
  over home WiFi
- **Server** (Flask + Gunicorn, Docker, Debian Linux) — two default services. Ingest
  accepts authenticated GPX uploads and writes `/data/YYYY-MM/<trip>.gpx`
  plus a derived `<trip>.json` summary; the viewer lists, maps, annotates
  and exports them. Optional stdlib monitor and self-hosted ntfy services
  deliver device fault/silence notifications. Deploy with
  `server/install.sh` (runbook: `DEPLOY_DEBIAN.md`); `install.sh --check`
  re-verifies an install

## Project Layout

```
firmware/
  01_hello_world/        Stage 1: toolchain check
  02_pmu_test/           Stage 2: AXP2101 PMU, power rails, power-off, SD check
  03_gps_test/           Stage 3: TinyGPSPlus NMEA parsing
  04_trip_logger/        Stage 4: the trip logger, plus its partitions.csv
  host_test/             Host-side tests for Stage 4 (run.sh, stubs/)
  STAGE4_TECHNICAL_BRIEF.md  Stage 4 design brief (written for the V1.2)
  secrets.h.example      Copy to secrets.h and fill in WiFi + server creds
  README.md              Full setup guide + pin reference

server/
  common/diagnostics/    Shared catalog and protocol validators (stdlib only)
  docker-compose.yml     Default ports 8090 + 8091; optional ntfy 8092 and monitor 8093
  .dockerignore          Default-deny runtime allowlist; excludes secrets and data
  .env.example           Copy to .env, set INGEST_AUTH_TOKEN and BIND_ADDR
  viewer.env.example     Copy to viewer.env, set the viewer login
  monitor.env.example    Disabled defaults; private monitor.env selected via MONITOR_ENV_FILE
  install.sh             First-time install with checks; --check re-verifies
  mileage-ingest/        Flask ingest (app.py), diagnostics SQLite (diag_store.py), Dockerfile
  mileage-viewer/        Flask trip viewer/export + Device health (app.py, static/)
  mileage-monitor/       Stdlib monitor.py, gelf.py, prometheus.py, loki.py; own state volume
  grafana/               Importable Prometheus scrape config, dashboard, alert examples
  ntfy/server.yml        Optional self-hosted ntfy; deny-all access, ntfy-state volume
  backup/                backup.sh + restore.sh, systemd timer, backup.conf.example
  tests/                 Server tests (unittest) + map-parser check (node)
  sample.gpx             Test upload for the end-to-end check
  README.md              Deployment + curl test guide

tools/check_pii.py                  Personal-info scan (CI runs it; see Do Not)
tools/generate_error_catalog.py     Generates sketch-local diag_catalog.h
DIAGNOSTICS.md                     Framework reference and issue delivery map
.github/workflows/audit-tests.yml   CI: PII scan + server tests + firmware host tests
.github/dependabot.yml              Dependabot: monthly pip, Docker and Actions bumps
CODE_REVIEW_2026-06-09.md, AUDIT_2026-08-04.md, AUDIT_2026-10-05.md,
INDEPENDENT_AUDIT_2026-10-05.md      Review history (see Where Status Lives)
```

## Where Status Lives

GitHub issues are the only record of what's open, fixed or next. This file
names no issue numbers on purpose, because they go stale; keep it that way.

- **Before assuming anything is open or fixed**, check `gh issue list`.
  Filter by label: area (`area: firmware`, `area: backend`,
  `area: installer`), severity (`severity: high|medium|low`), project
  (`supreme-port`, `diagnostics`) or audit (`audit-2026-08-04`,
  `audit-2026-10-05`). Hands-on hardware checks are issues titled
  `Shakedown: ...`; they need the board, a meter or the car, so they can't
  be done from this PC alone.
- **Picking what's next:** look at the open issues, favouring whatever
  blocks deploying the server or road-testing the tracker, then severity.
- **Closed issues** record what was fixed and why. The audit docs
  (`CODE_REVIEW_2026-06-09.md` → `AUDIT_2026-08-04.md` →
  `AUDIT_2026-10-05.md` → `INDEPENDENT_AUDIT_2026-10-05.md`) record the
  findings and V1.2 bench results as they were at the time.
- **History and visibility:** this repo starts from a clean snapshot
  taken 2026-10-13. Earlier commits, PRs and closed issues stay in a
  private, archived repo, so commit hashes and PR numbers in the audit
  docs point there. This repo is private for now; the owner plans to make
  it public once the server is deployed and the tracker works on the road.
- **Conventions:** when a PR fixes an issue, say "Fixes #N" (one line per
  issue) so merging it closes the issue. File every new finding as its own
  issue, however small, never as an item on a checklist issue.

## Tests

CI runs all of these on every PR:
- PII: `python tools/check_pii.py` (add `--commits origin/main..HEAD`
  before pushing) and `python -m unittest discover -s tools`.
  The scanner checks positions and new commit dates; commit with TZ=UTC.
- Server: `python -m unittest discover -s server/tests` and
  `node server/tests/test_viewer_map.js`. Needs the service requirements
  installed. Monitor needs no additional dependencies and uses fake time,
  real SQLite, and local fake HTTP receivers for ntfy, GELF and Loki.
  `test_backup` needs bash and rsync, so it skips on Windows; run it in
  WSL (`wsl -d Ubuntu -- python3 -m unittest server/tests/test_backup.py`).
- Firmware: `bash firmware/host_test/run.sh` (filesystem, I2C/PMU, diagnostics
  journal/catalog, Python protocol bridge, and GPS against real TinyGPSPlus).
  No host compiler on this PC: set
  `CXX="<venv python> -m ziglang c++"`.
- Firmware compile: all four sketches for the Supreme (ESP32-S3), with
  pinned Arduino tools and libraries.

## Key Technical Details

### Supreme (target hardware)

Bench-checked 2026-10-18 with Stages 1–3 unless marked; details in
`firmware/README.md`.
- **Rails differ from the V1.2:** ALDO3 = LoRa, ALDO4 = GPS (3.3 V). Never
  run V1.2 rail code on the Supreme. **ALDO3 comes up ON after a USB-power
  power-on**, so firmware switches it off first thing after `PMU.begin()`
- PMU (0x34) and PCF8563 RTC (0x51) share `Wire1`, SDA 42 / SCL 41. OLED and
  BME280 are on `Wire`, SDA 17 / SCL 18
- GPS: MAX-M10S, firmware SPG 5.10, 9600 baud on RX 9 / TX 8, GPS_EN GPIO 7
  driven HIGH (low/high made no difference). It still ACKs the legacy UBX
  CFG-MSG and CFG-NAV5; UBX-CFG-VALSET is the future-proof interface. After
  a cold power-up it sends NMEA before it accepts UBX (config took ~10 s),
  so Stage 4 retries its config in the background
- GPS backup RAM is fed from the 18650, not the PMU's VBACKUP (leave
  VBACKUP off): hot start 2 s, cold 33 s. The board powers an active antenna
- PMU interrupt is GPIO 40, which isn't an RTC GPIO on the ESP32-S3, so it
  can't wake deep sleep. Wake instead: `PMU.shutdown()` powers the board
  off, and the next USB power insert powers it on (not while USB stays in)
- microSD (pins from LilyGO's source, not yet bench-checked; Stage 2 `d`
  checks them): FAT32 only (exFAT unsupported), powered from BLDO1, SPI
  MOSI 35 / SCK 36 / MISO 37, CS 47, IMU CS 34 held high. BLDO1 powers up
  ON at 1.8 V: switch it off, set 3.3 V, then enable. Stage 4 picks its
  trip store once at boot, never formats the card, falls back to LittleFS,
  uploads from both stores, and unmounts both before `PMU.shutdown()`
- Build: `esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled`
  (8 MB flash, 8 MB QSPI PSRAM, native USB). Download mode, the factory
  flash backup and its Windows read quirk: `firmware/README.md`
- Before first power, attach the LoRa antenna; before first flash, back up
  the factory flash

### V1.2 (retired; its last firmware is on the frozen `tbeam-v1.2` branch)

- GPS power: AXP2101 **ALDO3** rail at 3.3V (measured 2026-10-05). VBACKUP
  enabled for GPS hot starts. ALDO2 = LoRa + OLED header — leave it alone
- GPS UART: GPIO 34 (RX from GPS), GPIO 12 (TX to GPS), 9600 baud
- I2C (PMU): GPIO 21 (SDA), GPIO 22 (SCL)
- Builds on ESP32 core 3.3.11 (XPowersLib 0.3.3, TinyGPSPlus 1.0.3) with
  the bundled `partitions.csv` (2.375 MB LittleFS). The M8N is configured
  over UBX at boot for RMC+GGA only, 1 Hz, automotive dynamic model
- LittleFS: use `file.close()` not `file.flush()` for durability

### Firmware (both boards)

- Trip files are **GPX 1.0** at ~119 B/point (1.0 allows `<speed>` and
  `<hdop>` directly on `<trkpt>`)
- Trip detection: start >5 mph for 10s; end <2 mph for 3 min
- FreeRTOS: networking on core 0, GPS on core 1
- Five-state FSM for trip lifecycle

### Server

- Ingest port: 8090, bearer token auth, GPX body in → raw `.gpx` + derived
  `.json` summary out (atomic writes); distance = haversine over moving
  steps only (speed > 2 mph at either end), HDOP > 5 and > 60 m/s jumps
  skipped, GPS gaps bridged by straight line (`summarize()`, tests in
  `server/tests/`)
- Upload limits: 16 MiB body cap (`INGEST_MAX_UPLOAD_BYTES`, ~39 h of
  nonstop driving). It protects the server's memory: the ingest parses the
  whole body at once, peaking at ~9x its size. Firmware splits long trips
  into pieces below 8 MiB, half the server cap, reserving room for pending
  points and closing tags. Each piece uploads separately, with no arming
  gap between pieces. gunicorn `--timeout 900` lets an 8 MiB piece upload
  at 10 kB/s (~839 s), with a margin for handling it. The timeout includes
  reading the body. Early 401/413 replies read and
  discard up to 4x the cap first, so the device sees the status, not a reset.
  `server/tests/test_upload_limits.py` ties the current upload limit to
  the firmware split limit and the flash fallback in `partitions.csv`
- Diagnostics: `POST /v1/device-events` on ingest, same bearer auth plus validated
  `X-Device-Id`, 32 KiB streaming cap (also without Content-Length), protocol v1
  from `common.diagnostics`, catalog cached at startup. Ingest owns
  `/data/diagnostics/events.db` under `INGEST_DATA_ROOT`: SQLite DELETE journal,
  synchronous=FULL, 5 s busy timeout, schema version 1. One transaction dedupes
  events and applies the device-wide `status_sequence` high-water rule; ack only
  after commit. GPX successes refresh contact only for a valid device header;
  diagnostics failure never changes their response. See `DIAGNOSTICS.md` for
  schema/unknown-condition semantics.
- Viewer port: 8091, session login (LAN-only), reads `./data` read-only, owns
  `annotations.db` (SQLite) in the `viewer-db` volume. `/device-health` reads
  diagnostics schema v1 via `mode=ro` with a 2 s busy timeout, shows contact and
  snapshot ages, active/unknown/clear conditions and 50-event history pages with
  code/severity filters. Severity/text come from the current startup catalog,
  never stored severity. Missing DB means no diagnostics yet; read/schema/lock
  errors mean unavailable (503 + warning log). No diagnostics writes or recovery.
- Optional monitor: `monitor` Compose profile, stdlib only,
  catalog loaded at startup. `events.db` schema v1 is read with `mode=ro` and
  a 2 s busy timeout; missing means no work, unreadable logs unavailable and
  suspends sends until a successful read. Own `/state/monitor.db` schema v1 in
  `monitor-state` stores episodes, pending work/outbox, successful delivery
  window and retry state. Every 60 s, all devices are watched, with no watchlist.
  Freshness is 24 h of accepted snapshot receipt, independent of GPX contact.
  Catalog severity/notify control faults; 300 s hold (critical bypass), 24 h
  reminders, six successful posts per rolling hour including bounded digests
  and resolutions. Unknown/reboot is not recovery; explicit newer clear and
  recorded alert success are required. Delivery is at least once; persistent
  global retry doubles from 60 s to 3600 s, HTTP timeout 10 s, no redirects.
  E7001 uses server last-contact age >= seven days, bypasses fault severity/hold,
  resolves on newer contact. Separate `monitor.env` (selected by
  `MONITOR_ENV_FILE` in `.env`) holds only
  monitor settings/token, defaults to disabled `monitor.env.example` for normal
  install/--check. Disabled monitor idles without DB reads. Optional `ntfy`
  profile uses port 8092 with `BIND_ADDR`, deny-all auth and `ntfy-state` cache/auth
  volume. Self-hosted only; iOS instant push requires ntfy.sh wake-up relay
  (no message text), Android does not. install.sh's Notifications step turns them on
  (INSTALL_OUTPUTS); the by-hand route is DEPLOY_DEBIAN Step 11.
- Optional Graylog export (`gelf.py`): `GRAYLOG_ENABLED` plus exactly one
  of `GRAYLOG_GELF_URL` (GELF HTTP, preferred) or `GRAYLOG_GELF_TCP` (optional
  TLS) in `monitor.env`. Graylog runs on the owner's own server; never bundle it.
  Sends facts, not ntfy decisions: every events.db row once (rowid cursor,
  reset if events.db is restored lower), and per-(device, code) condition state
  changes (active/clear/unknown, E7001 silence), so deep-sleep wakes don't
  repeat. Independent of NOTIFY_*; own retry/backoff; failures never affect
  ntfy. Tables `gelf_cursor`, `gelf_conditions`, `gelf_retry` are additive to
  monitor schema v1. Setup and Event Definitions: DEPLOY_DEBIAN Step 11d.
- Optional Prometheus metrics (`prometheus.py`): `METRICS_ENABLED=true`
  plus a separate token of at least 32 printable ASCII characters in
  `monitor.env`. `/metrics` publishes on `BIND_ADDR` port 8093 with bearer auth.
  The HTTP thread serves the latest published snapshot with a 10-second request
  timeout and never queries SQLite. The Grafana fault rule reads current
  `condition_state`; `alert_decisions` reports ntfy delivery state and works
  only as delivery history. Importable scrape config, Tracker health dashboard
  and alert examples are in `server/grafana/`; setup is DEPLOY_DEBIAN Step 11e.
- Optional Loki push (`loki.py`): a nonempty `LOKI_PUSH_URL` enables
  JSON event lines, per-device/code condition changes (active/clear/unknown,
  including E7001 silence and recovery) independent of ntfy, and successful
  ntfy alert/reminder/resolution decisions. Fixed labels are `job`, validated
  `device_id` and catalog severity. The independent event cursor resets on a
  lower restored events.db rowid; real timestamps (Loki 2.4+), backlog first. Network
  failures, 408, 429 and 5xx retry; other HTTP 4xx rejections skip the batch and
  advance its cursors. The condition table and cursor are additive to monitor
  schema v1. Setup and LogQL examples: DEPLOY_DEBIAN Step 11f.
- Backups: `server/backup/backup.sh` saves trips plus consistent SQLite
  snapshots of viewer annotations, ingest diagnostics (`events.db`), and optional
  monitor delivery/export state (`monitor.db`, when its volume exists). Viewer
  helpers read annotations and diagnostics; the monitor helper reads its volume.
  Missing diagnostics/monitor databases are normal and recorded in `INFO`.
  Monitor discovery uses Compose config's project name and Docker's Compose
  project/volume labels, without recent config flags or logging credentials.
  Discovery errors warn once and record `monitor=unknown`; backup continues
  with trips, annotations and events. Backup never runs a monitor helper for
  an absent/unknown volume. Restore skips monitor state and service actions
  when discovery is unknown; manual recovery is in DEPLOY_DEBIAN Step 10.
  Live events.db and SQLite sidecars are excluded from data rsync; DB snapshots
  sit beside annotations.db and are covered by `SHA256SUMS`. Each DB is coherent
  individually, not a shared transaction across services. Restore validates all
  saved DBs, stops their writers, takes safety snapshots before replacement, and
  restarts the default services and any previously running monitor. Older backups
  still restore trips/annotations with a message for missing new DBs, leaving
  those live DBs unchanged and never replaying an old live diagnostics file.
  Optional ntfy state needs a separate backup. Runbook: DEPLOY_DEBIAN Step 10.
- Port binding: `BIND_ADDR` in `.env` sets the host IP published ports bind
  on (unset = all interfaces). UFW does not filter Docker-published ports,
  so the bind address is the LAN-only control (`DEPLOY_DEBIAN.md` Step 7)
- Viewer auth: `viewer.env` (separate from `.env` — keeps the ingest token out
  of the viewer and survives Compose interpolation) holds `VIEWER_USERNAME`,
  scrypt `VIEWER_PASSWORD_HASH`, `VIEWER_SECRET_KEY`. Everything except
  `/health` requires a session; CSRF on POSTs; 5 failures locks an IP for
  15 min via a `login_attempts` table (SQLite, so it's shared across workers).
  Sessions carry an HMAC of the password hash, so a new hash logs everyone out.
  App fail-fasts if auth config is missing or the hash lost its `$` chars.
- Timezone: stored UTC, displayed/exported in `DISPLAY_TZ` via `ZoneInfo`,
  DST-aware — never a fixed offset. No default: `install.sh` asks for it and
  writes it to `.env`, Compose passes it to **both** services (viewer
  renders it; ingest buckets `data/YYYY-MM/` by it), and both refuse to
  start without it. `tzdata` pinned in both requirements; services
  fail-fast if the zone db is missing. Distinct from `TZ`, which only
  affects log lines.

## Do Not

- Put IRS classification logic in firmware — server-side only
- Power SX1262 LoRa without antenna (RF damage risk)
- Commit to the frozen `tbeam-v1.2` branch, or port fixes back to it
- Commit `secrets.h`, `.env`, `viewer.env`, or `monitor.env` to version control
- Commit anything personal. The repo will go public, and that exposes
  everything from its private period too, so docs, examples, tests,
  commit messages, issues and PRs use placeholders (`<user>`, `<host>`,
  `<username>`, `~\Documents\...`), never a real name, account path,
  email, SSID/MAC, real coordinates, or a photo with EXIF. Use the
  GitHub noreply email and commit with TZ=UTC. `tools/check_pii.py` enforces
  this in CI for files and commits (not issue or PR text); names come from
  the `PII_DENYLIST` secret or a gitignored `.pii-denylist`
- Change the repo's visibility. The owner decides when it goes public
- Bring commits over from the private archive repo: no merge, cherry-pick,
  or adding it as a remote. Its history carries a personal email and
  account paths, which is why this repo started from a clean snapshot.
  Copy file contents across instead, and run `check_pii.py` before pushing
