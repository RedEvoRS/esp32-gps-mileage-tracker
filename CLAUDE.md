# GPS Mileage Tracker — Project Instructions

## What This Is

A DIY GPS mileage tracker for IRS tax deduction logging. Plug-and-forget
USB device for a car. Logs trips automatically; user annotates purpose later.

**Hardware:** LilyGO **T-Beam Supreme** with the u-blox **MAX-M10S GPS
version (not L76K)** (ESP32-S3 + SX1262 LoRa + AXP2101 PMU + microSD +
SH1106 OLED + PCF8563 RTC) at 915 MHz. 18650 battery. The selected trip
storage card is a **SanDisk 32 GB Max Endurance microSD**. Enclosure is a
PETG print of the owner's published remix of Alley Cat's Alley Chat TS2
Supreme case (CC BY-SA, MakerWorld; adds a GPS connector window). Links
and fitting steps: `firmware/README.md` "Fitting it in the case".

**Old hardware, frozen branch:** the project started on the LilyGO TTGO
T-Beam V1.2 (ESP32 + NEO-M8N). It's retired: its 2.375 MB trip partition
held only ~6 h of driving between uploads. Its last firmware is on the
`tbeam-v1.2` branch, which is frozen and won't be updated: no fixes, no
backports. Never commit to it or base work on it; all work targets the
Supreme on `main`.

**Supreme status:** Stages 1–3 and the trip logger (Stage 4) run on the
Supreme: pins, rails, PMU power-off and USB wake were bench-checked
2026-10-18. Stage 4 stores trips on the microSD card, with the internal
2.375 MB LittleFS partition as fallback. The card was bench-checked
2026-10-18, and by 2026-10-20 two real drives were recorded on it and read
back on a PC. The server was deployed on Debian 13 with `server/install.sh`
on 2026-10-20; `install.sh --check` passed. The first real uploads from SD
succeeded on 2026-10-21 (UTC), using main commit `a358eaf`'s Stage 4 debug
build, flashed at 20:39Z on 2026-10-20. Four trips received HTTP 200 and
were deleted locally; ingest stored GPX and JSON files, and the viewer
listed all four. Diagnostics then delivered all 115 pending events and a
current snapshot; Device health showed SD storage, zero dropped events
and no active conditions. Minimum free network stack was 7,436 of 12,288
bytes with GPS logging active. Trip uploads with debug mode off and
server-down retry were also verified on 2026-10-21 (UTC): two `HTTP -1`
failures kept the trip queued, then HTTP 200 after ingest restarted deleted
the local file. On the bench on 2026-11-01 (UTC), a server error (5xx) kept
trips queued (E5008) and they uploaded after recovery; a wrong token (401)
gave E5004 on a small file and a broken send (E5003, `HTTP -3`) on a ~3 MB
file; diagnostics dedupe held. The release-candidate driving week on firmware
v0.11 passed on 2026-11-01, and firmware and server v1.00, the first public
release, followed the same day. LittleFS uploads and HTTP 413 remain untested on
the Supreme. Other hardware acceptance is tracked under `supreme-port`.
Don't assume a Supreme pin, rail or peripheral works until its port issue
is closed and it's verified on the bench.

## Architecture

Two-component system:
- **Firmware** (Arduino/C++) — runs on the T-Beam, logs trips, uploads
  over home WiFi
- **Server** (Flask + Gunicorn, Docker, Debian or Unraid) — two default services. Ingest
  accepts authenticated GPX uploads and writes `/data/YYYY-MM/<trip>.gpx`
  plus a derived `<trip>.json` summary; the viewer lists, maps, annotates
  and exports them. Optional stdlib monitor and self-hosted ntfy services
  deliver device fault/silence notifications. Deploy with
  `server/install.sh` (runbooks: `DEPLOY_DEBIAN.md` / `DEPLOY_UNRAID.md`); `install.sh --check`
  re-verifies an install

## Project Layout

```
firmware/
  01_hello_world/        Stage 1: toolchain check
  02_pmu_test/           Stage 2: AXP2101 PMU, power rails, power-off, SD check
  03_gps_test/           Stage 3: TinyGPSPlus NMEA parsing
  04_trip_logger/        Stage 4: the trip logger, plus its partitions.csv
  host_test/             Host-side tests for Stage 4 (run.sh, stubs/)
  DESIGN.md              Why Stage 4 works the way it does
  secrets.h.example      Copy to 04_trip_logger/secrets.h; fill in WiFi + server creds
  README.md              Full setup guide + pin reference

install/
  setup.sh               Release-based server install/update wrapper
  flash.ps1              Windows tracker flash helper
  flash.sh               macOS/Linux tracker flash helper
  tests/                 Setup wrapper integration tests

server/
  firmware-builder/      Private server-side firmware build (build.sh, README.md)
  common/diagnostics/    Shared catalog and protocol validators (stdlib only)
  docker-compose.yml     Default ports 8090 + 8091; optional ntfy 8092 and monitor 8093
  .dockerignore          Default-deny runtime allowlist; excludes secrets and data
  .env.example           Copy to .env, set INGEST_AUTH_TOKEN and BIND_ADDR
  viewer.env.example     Copy to viewer.env, set the admin login
  monitor.env.example    Disabled defaults; private monitor.env selected via MONITOR_ENV_FILE
  install.sh             First-time install with checks; --check re-verifies
  mileage-ingest/        Flask ingest (app.py), diagnostics SQLite (diag_store.py), Dockerfile
  mileage-viewer/        Flask trip viewer/export + Device health
                        (app.py, users.py, manage_users.py, trip_log.py, geocoding.py, static/)
  mileage-monitor/       Stdlib monitor.py, gelf.py, prometheus.py, loki.py; own state volume
  grafana/               Importable Prometheus scrape config, dashboard, alert examples
  ntfy/server.yml        Optional self-hosted ntfy; deny-all access, ntfy-state volume
  backup/                backup.sh + restore.sh, systemd timer, scheduled.sh cron wrapper,
                         backup.conf.example
  tests/                 Server tests (unittest) + map-parser check (node)
  sample.gpx             Test upload for the end-to-end check
  README.md              Deployment + curl test guide

tools/check_pii.py                  Personal-info scan (CI runs it; see Do Not)
tools/generate_error_catalog.py     Generates sketch-local diag_catalog.h
DEPLOY_UNRAID.md                   Unraid install, cron backups and Debian migration
DIAGNOSTICS.md                     Framework reference and issue delivery map
.github/workflows/release.yml       Published-release bundle, scripts and checksums
.github/workflows/audit-tests.yml   CI: PII scan + server tests + firmware host tests
.github/dependabot.yml              Dependabot: monthly pip, Docker and Actions bumps
docs/history/                     Reviews and original V1.2 design brief (see Where Status Lives)
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
  (`docs/history/CODE_REVIEW_2026-06-09.md` → `docs/history/AUDIT_2026-08-04.md` →
  `docs/history/AUDIT_2026-10-05.md` → `docs/history/INDEPENDENT_AUDIT_2026-10-05.md`) record the
  findings and V1.2 bench results as they were at the time.
- **History and visibility:** this repo starts from a clean snapshot
  taken 2026-10-13. Earlier commits, PRs and closed issues stay in a
  private, archived repo, so commit hashes and PR numbers in the audit
  docs point there. This repo is private for now and will go public when
  the owner decides.
- **Conventions:** when a PR fixes an issue, say "Fixes #N" (one line per
  issue) so merging it closes the issue. File every new finding as its own
  issue, however small, never as an item on a checklist issue.

## Tests

CI runs all checks on PRs and manual runs; pushes to main run only the PII
scan. Strict up-to-date required checks mean main receives exactly the files
tested on the PR. A new push to a PR cancels its older run; main runs are not
cancelled. The PII/tools job is skipped for Dependabot PRs:
- PII: `python tools/check_pii.py` (add `--commits origin/main..HEAD`
  before pushing; it also checks every file version and path the range adds,
  even ones later removed) and `python -m unittest discover -s tools`.
  The scanner checks positions and new commit dates; commit with TZ=UTC.
- Server: `python -m unittest discover -s server/tests`,
  `python tools/generate_error_catalog.py --check`,
  `node server/tests/test_viewer_map.js` and `node server/tests/test_viewer_places.js`.
  Needs the service requirements installed. Monitor needs no additional
  dependencies and uses fake time, real SQLite, and local fake HTTP receivers
  for ntfy, GELF and Loki.
  `test_backup` needs bash and rsync, so it skips on Windows; run it in
  WSL (`wsl -d Ubuntu -- python3 -m unittest server/tests/test_backup.py`).
- Firmware: `bash firmware/host_test/run.sh` (filesystem, I2C/PMU, diagnostics
  journal/catalog, Python protocol bridge, and GPS against real TinyGPSPlus).
  No host compiler on this PC: set
  `CXX="<venv python> -m ziglang c++"`. Set `TINYGPS_DIR` to TinyGPSPlus's
  `src/` folder and `U8G2_DIR` to the U8g2 library folder; see
  `firmware/host_test/README.md`. A passing run can include `SKIPPED` suites;
  check the output.
- Firmware compile: all four sketches for the Supreme (ESP32-S3), with
  pinned Arduino tools and libraries.
- Installer: the `installer` job runs shellcheck, then a fresh install as a
  sudo user, reruns, config repair (unquoted hash, CRLF `.env`), a Docker
  restart, concurrent `--check` runs, turning on metrics, and `--check`
  reporting a stopped viewer.
  `server/tests/test_installer_unraid.py` and `test_backup_scheduled.py`
  cover Unraid installer paths and the cron wrapper. Unraid paths are tested
  only with fakes; real-host install, array/reboot recovery and migration
  remain unverified. Run Bash-based tests under WSL; Windows skips them.

## Key Technical Details

### Supreme (target hardware)

Bench-checked 2026-10-18 with Stages 1–3 unless marked; details in
`firmware/README.md`.
- **Rails differ from the V1.2:** ALDO3 = LoRa, ALDO4 = GPS (3.3 V);
  ALDO1 = OLED/BME280/magnetometer (Stage 4 sets 3.3 V). Never
  run V1.2 rail code on the Supreme. **ALDO3 comes up ON after a USB-power
  power-on**, so firmware switches it off first thing after `PMU.begin()`
- PMU (0x34) and PCF8563 RTC (0x51) share `Wire1`, SDA 42 / SCL 41. OLED and
  BME280 are on `Wire`, SDA 17 / SCL 18
- RTC: read at boot to anchor diagnostic timestamps only, never GPX;
  checked again when GPS/SNTP time is trusted. Up to three sync attempts
  per boot, at least 60 s apart; a failed write or read-back verification
  uses one attempt. Stops after one verified success.
  It keeps time across `PMU.shutdown()` (bench, 2026-10-20) and drifts
  about 10 s/day; the PCF8563 has no calibration register
- GPS: MAX-M10S, firmware SPG 5.10, 9600 baud on RX 9 / TX 8, GPS_EN GPIO 7
  driven HIGH (low/high made no difference). Bench tests showed that SPG
  5.10 still ACKs legacy CFG-MSG/CFG-NAV5. Stage 4 now uses the future-proof
  UBX-CFG-VALSET interface: one RAM-only message at every boot explicitly
  sets UART1 RMC+GGA+NAV-PVT to rate 1, GLL/GSA/VTG off, GSV to rate 5
  in all builds, and the automotive model. Navigation stays at the
  default 1 Hz. After checksum-valid NMEA proves readiness, each send gets
  a 2 s ACK window for the busy 9600-baud UART; up to three sends per
  attempt and five attempts with 8 s backoff run in the normal loop.
  After success or final failure, the NAV-PVT fallback wait starts. There
  is no legacy fallback; failure warns and keeps current receiver settings
- GPS backup RAM is fed from the 18650, not the PMU's VBACKUP (leave
  VBACKUP off): hot start 2 s, cold 33 s. The board powers an active antenna
- PMU interrupt is GPIO 40, which isn't an RTC GPIO on the ESP32-S3, so it
  can't wake deep sleep. Wake instead: `PMU.shutdown()` powers the board
  off, and the next USB power insert powers it on (not while USB stays in)
- microSD (pins from LilyGO's source, checked by Stage 2 `d`; Stage 4 has
  recorded real drives on the card): FAT32 only (exFAT unsupported),
  powered from BLDO1, SPI MOSI 35 / SCK 36 / MISO 37, CS 47, IMU CS 34
  held high. BLDO1 powers up
  ON at 1.8 V: switch it off, set 3.3 V, then enable. Stage 4 picks its
  trip store once at boot, never formats the card, falls back to LittleFS,
  attempts uploads from both stores, and unmounts both before `PMU.shutdown()`.
  SD uploads were verified on 2026-10-21 (UTC); LittleFS uploads remain untested
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

### Firmware (Supreme, main)

- Trip files are **GPX 1.0** at ~119 B/point (1.0 allows `<speed>` and
  `<hdop>` directly on `<trkpt>`). Trackpoint UTC uses the same RMC's date
  and time, with `.sssZ` for nonzero centiseconds or whole seconds otherwise;
  invalid/missing RMC epochs stay untimed, including buffered points after
  a system-clock sync
- Trip detection: start >5 mph for 10s; end <2 mph for 5 min. NAV-PVT
  speed/horizontal accuracy must be <=0.5 m/s / 5 m to start,
  <=1.0 m/s / 10 m during a trip. Limits were kept after the recorded
  road-test calibration; parking-garage behaviour remains unverified.
  Cooldown resumes after 3 consecutive trusted fixes >2 mph, keeping its
  original deadline until then. Cooldown adds no distance or moving duration;
  the confirming fix starts a new distance baseline without a bridge;
  rejected points count as stopped and are not recorded. Fallback is disabled
  until boot GPS configuration succeeds or gives up after five attempts;
  after >10s without NAV-PVT from config end or the last NAV-PVT, warn once
  and fall back to RMC-only speed
- FreeRTOS: networking on core 0, GPS on core 1
- Stage 4 diagnostics: static EventLog, random boot identity and a boot
  counter in nonvolatile storage (NVS); early BOOT/pre-seal power-off battery
  samples and a read-only parked-drain helper (deploy the server catalog
  first); boot/reset, retained PMU recovery and runtime fault hooks. GPS,
  battery/charge guard, trip storage/recovery, WiFi/HTTP, network stack and
  OLED faults use the catalog policy (production: brief heard-satellite/fault
  cards), including an antenna check using automatic gain control (AGC) and
  satellites in view (GSV) in all builds. Conditions raise/clear with
  15-minute repeats; WiFi access point (AP) absence is a boot-local counter,
  never a scan event. Per-core static producer builders keep event buffers
  off task stacks. Bounded degraded startup retries restart on recovery.
- Diagnostic journal: the network task services one queued record at a time
  using a zero-wait filesystem lock and yields maintenance to trip/upload
  demand. Mounted SD has a 16 MiB journal quota; LittleFS has a 64 KiB reserve
  included in trip free-space checks; both failing leaves a bounded volatile
  RAM queue.
  Journal writes use checked sync/close on the mounted virtual filesystem
  (VFS), never format media, and seal on core 0 before PMU unmount/power-off.
  Host tests do not establish storage timing or durability.
- Diagnostic uploads: after GPX batches, use exact validated acknowledgments,
  request buffers allocated once per boot in pseudo-static RAM (PSRAM; if
  that fails, uploads are off and the journal stays local), a ten-second
  deadline and independent retry state. Current snapshots include
  observed/active conditions and AP-absence counts. A single record the
  server still rejects after the batch is halved down to it uses a reserved
  bounded inspection slot and durable loss accounting. Heartbeats use existing awake opportunities
  after 24 hours without contact (first boot contact age is unknown).
  Successful diagnostics uploads print nothing on serial; confirm them
  in ingest logs or Device health.
- Snapshot enrollment: the journal snapshot revision allocator needs explicit
  one-use enrollment in `secrets.h` against the server high-water: the server's
  highest accepted `status_sequence` for the device (see `DIAGNOSTICS.md`).
  Remove `DIAGNOSTICS_ENROLLMENT_ID` and `DIAGNOSTICS_STATUS_HIGH_WATER`
  after the first good diagnostics upload and reflash with erase disabled;
  normal boots use existing journal metadata. Revisions mirror to usable SD
  and LittleFS journals, so either healthy authority can report storage failure.
  NVS preserves suspension/stale state; a missing witness adopts agreeing ready epochs.
  Re-enrollment needs a new ID
  greater than any used (at least 2 after ID 1) and a fresh server high-water
  read. Missing revision authority retains diagnostics locally without
  blocking GPX.
- Firmware identity: builds report a stamped version number plus Git commit.
  No generated `firmware_version.h` means `unversioned`; an existing header
  keeps its old stamp. Run `python tools/stamp_firmware_version.py` before
  every build. Boards flashed before version reporting show `Not reported`.
- Display/debug: one Stage 4 firmware starts with debug off at every power-on,
  never saved. While running, hold BOOT then tap POWER to toggle the live
  page and debug serial output. POWER alone shows a production status card
  in either mode. The verified PMU interrupt (IRQ) batch handles short presses
  outside degraded mode; long press is unchanged. BOOT held during power-up
  still means download mode. Core 1 owns the debounced GPIO 0 input and both
  OLED renderers; core 0 reads only an atomic mode flag. Switching off never
  replays stale cards.
- Five-state FSM for trip lifecycle

### Server

- Unraid: detect `/etc/unraid-version` (`version=` starts with a digit);
  `MILEAGE_PLATFORM=debian|unraid` overrides detection. Root-only, no sudo,
  apt or systemd. Installer writes `RESTART_POLICY=always`; unset Compose
  default remains `unless-stopped`. Persistent Dynamix cron calls
  `backup/scheduled.sh` (nightly + overdue catch-up); `--status` reads its
  state, log is `backup/logs/backup.log`. `BACKUP_MOUNT` / `REMOTE_MOUNT`
  guard missing destinations; root requires root-owned backup.conf without
  group/world write. Missing host Python uses disposable containers from
  ingest's base image. Builder high-water/compile helpers stay UID 1000.
  Runbook: `DEPLOY_UNRAID.md`; actual boot recovery remains unverified.
- Version: `server/VERSION`; `python3 server/stamp_version.py` prints release +
  ten-character Git commit (+ `-dirty` for tracked changes; VERSION alone if
  Git and archive COMMIT are unavailable). Pass as Compose build arg `SERVER_VERSION`; installer
  does this automatically for all Python images. Missing/invalid stamps are
  `unversioned`. Viewer header (footer on login/error pages), Device health
  and unauthenticated `/health` expose it on the LAN; `install.sh --check`
  warns on service mismatches.
  Bump VERSION in the PR that ships the change (no separate PR); after
  deployment is verified, tag the merged commit `srv-v0.NN` (see DEPLOY_DEBIAN.md).
- Ingest port: 8090, bearer token auth, GPX body in → raw `.gpx` + derived
  `.json` summary out (atomic writes); distance = haversine over moving
  steps only (speed > 2 mph at either end), HDOP > 5 and > 60 m/s jumps
  skipped, GPS gaps bridged by straight line (`summarize()`, tests in
  `server/tests/`). GPX with a DTD/entities, times before 2000 or more than a
  day ahead, or non-finite derived numbers gets 400 before anything is written.
  Timed GPX ignores `X-Trip-Id` and uses its timestamp-derived UTC ID; untimed
  uploads may use a custom ID outside that namespace (installer test uploads are
  untimed copies of `sample.gpx`). Every upload looks for its ID in all month
  folders under the upload lock, so a `DISPLAY_TZ` change can't duplicate a trip.
  Ingest runs with umask 077; `install.sh` keeps `data/` at mode 700
- Upload limits: 16 MiB body cap (`INGEST_MAX_UPLOAD_BYTES`, ~39 h of
  nonstop driving). It protects the server's memory: the ingest parses the
  whole body at once, peaking at ~9x its size. Firmware splits long trips
  into pieces below 8 MiB, half the server cap, reserving room for pending
  points and closing tags. Each piece uploads separately, with no arming
  gap between pieces. gunicorn `--timeout 900` lets an 8 MiB piece upload
  at 10 kB/s (~839 s), with a margin for handling it. The timeout includes
  reading the body. Early 401/413 replies drain up to 4x the cap for at most
  3 s (SIGALRM in the two sync workers), so the device normally sees the status;
  a slower rejected client may get a reset. Incomplete headers and slow
  authenticated bodies can still occupy a worker.
  `server/tests/test_upload_limits.py` ties the current upload limit to
  the firmware split limit and the flash fallback in `partitions.csv`
- Diagnostics: `POST /v1/device-events` on ingest, same bearer auth plus validated
  `X-Device-Id`, 32 KiB streaming cap (also without Content-Length), protocol v1
  from `common.diagnostics`, catalog cached at startup. Ingest owns
  `/data/diagnostics/events.db` under `INGEST_DATA_ROOT`: SQLite DELETE journal,
  synchronous=FULL, 5 s busy timeout, schema version 1, file mode 0600. One
  transaction dedupes events and applies the device-wide `status_sequence`
  high-water rule; a snapshot more than 1,000,000 above the high-water is
  ignored (its events still commit, 200). `reset_high_water.py` is the
  compare-and-set admin recovery (DIAGNOSTICS.md); ack only after commit. GPX successes refresh contact only for a valid device header;
  diagnostics failure never changes their response. See `DIAGNOSTICS.md` for
  schema/unknown-condition semantics.
- Viewer port: 8091, admin/everyday session logins (LAN-only), reads `./data`
  read-only, owns `annotations.db` (SQLite) in `viewer-db`. Removal only marks
  a trip in that DB; it never touches `./data`. Closing a year saves its totals
  in `tax_years.snapshot`; the year review compares saved and current totals,
  and lists trips received after the close (ingest `_received_at` later than
  `closed_at`), undated trips and trips inside a merge. Exports mark late trips
  in a last `received_after_close` column; late trips still count and stay
  un-annotatable (409) until the year is reopened. Every response sends
  `frame-ancestors 'none'`, `X-Frame-Options: DENY` and `nosniff`. Notes and
  destinations are limited to 2,000 characters without XML-illegal control
  characters. Merged summaries keep the rules they were built with until a
  source file changes. The DB includes `users`,
  `trip_removals`, `trip_history` and `tax_years`. Optional `geocoding.py`
  sends a selected endpoint to an explicitly configured provider only on an
  authenticated, CSRF-protected click and caches suggestions in that database.
  Configurable interval, default 1.1 s.
  No lookup on page views, exports or uploads; never use public OSMF Nominatim.
  `/device-health` reads
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
  volume. `.env` `NTFY_BASE_URL` and `NTFY_BEHIND_PROXY` (default false)
  survive updates; hand edits to shipped `ntfy/server.yml` do not.
  Self-hosted only; iOS instant push requires ntfy.sh wake-up relay
  (no message text), Android does not. install.sh's Notifications step turns them on
  (INSTALL_OUTPUTS); the by-hand route is DEPLOY_DEBIAN Step 11.
- Optional Graylog export (`gelf.py`): `GRAYLOG_ENABLED` plus exactly one
  of `GRAYLOG_GELF_URL` (GELF HTTP, preferred) or `GRAYLOG_GELF_TCP` (optional
  TLS) in `monitor.env`. Graylog runs on the owner's own server; never bundle it.
  Sends facts, not ntfy decisions: cursor-based, at-least-once forwarding of
  events.db rows (rowid cursor, reset if events.db is restored lower or the
  event stored at the cursor changes), and
  per-(device, code) condition state changes (active/clear/unknown, E7001 silence),
  so deep-sleep wakes don't repeat. Duplicates are possible after an
  interrupted send or a lower events.db restore. Independent of NOTIFY_*;
  own retry/backoff; failures never affect ntfy. Tables `gelf_cursor`,
  `gelf_conditions`, `gelf_retry` are additive to monitor schema v1.
  Setup and Event Definitions: DEPLOY_DEBIAN Step 11d.
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
  lower restored events.db rowid or a changed event at the cursor; real timestamps (Loki 2.4+), backlog first. Network
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
  The off-host copy goes only to a destination holding `.mileage-backup-target`
  (written by install.sh or `backup.sh --mark-target`; otherwise the unit fails).
  `REMOTE_PRUNE=yes` never uses rsync `--delete`: it applies the retention rule
  to the remote's own snapshot names and refuses, with a warning, when the
  remote has snapshots newer than or missing from the local set. rsync has a
  600 s I/O timeout, the unit 6 h. Restore saves a corrupt live DB's raw files
  in the pre-restore folder and continues; its scratch is under `$BACKUP_DIR`.
- Port binding: `BIND_ADDR` in `.env` sets the host IP published ports bind
  on (unset = all interfaces). Optional `VIEWER_BIND_ADDR` overrides only
  viewer port 8091 (empty falls back to BIND_ADDR, then all interfaces);
  host-networked proxies can use loopback, bridge-networked proxies need
  the host's LAN address. UFW does not filter Docker-published ports,
  so the bind address is the LAN-only control (`DEPLOY_DEBIAN.md` Step 7)
- Viewer auth: `viewer.env` holds the admin `VIEWER_USERNAME`, scrypt
  `VIEWER_PASSWORD_HASH` and `VIEWER_SECRET_KEY`; rerun `install.sh` to change
  the admin password. Everyday password hashes are in the `users` table in
  `annotations.db`. Keep `viewer.env` separate from `.env`: it keeps
  `INGEST_AUTH_TOKEN` out of the viewer, and its single-quoted hash survives
  Compose interpolation. Everything except `/health` and `/login` requires a
  session. Sessions carry HMAC tags. The admin's tag is an HMAC of the
  `viewer.env` password hash, so a new hash logs the admin out; an everyday
  login's tag covers its username, hash and `session_version`, so reset,
  disable, enable or remove logs it out at once. `ENDPOINT_ROLES` is checked
  server-side on every request; undeclared endpoints are denied.
  `manage_users.py` manages everyday accounts. CSRF on POSTs; 5 failures lock
  an IP for 15 min via shared SQLite `login_attempts`. Failures count until a
  15-minute pause without one; the next failure starts a fresh count. A success
  clears only its own username's failures. App fail-fasts if auth config is
  missing or the admin hash lost its `$` chars.
  Optional `.env` `VIEWER_TRUSTED_PROXIES` accepts comma-separated IPv4/IPv6
  IPs/CIDRs: only trusted peers may supply X-Forwarded-For; use the rightmost
  valid untrusted address, falling back to the peer if none. Untrusted XFF
  warns once per peer, capped at 256 peers per worker with a thread lock.
  `VIEWER_COOKIE_SECURE=true` enables Secure session cookies for private
  HTTPS proxies (empty/false preserves HTTP). Both settings fail fast on
  invalid syntax; Compose passes them only to the viewer. Ingest stays HTTP.
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
