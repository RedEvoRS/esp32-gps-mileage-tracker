# GPS Mileage Tracker — Project Instructions

## What This Is

A DIY GPS mileage tracker for IRS tax deduction logging. Plug-and-forget
USB device for a car. Logs trips automatically; user annotates purpose later.

**Hardware:** LilyGO TTGO T-Beam V1.2 (ESP32 + NEO-M8N GPS + SX1262 LoRa +
AXP2101 PMU) at 915 MHz. 18650 battery. Enclosure 3D-printed and in use.

## Architecture

Two-component system:
- **Firmware** (Arduino/C++) — runs on T-Beam, logs trips to LittleFS, uploads
  over home WiFi
- **Server** (Flask + Gunicorn, Docker, Debian Linux) — two services. Ingest
  accepts authenticated GPX uploads and writes `/data/YYYY-MM/<trip>.gpx`
  plus a derived `<trip>.json` summary; the viewer lists, maps, annotates
  and exports them

## Project Layout

```
firmware/
  01_hello_world/        Stage 1: toolchain check
  02_pmu_test/           Stage 2: AXP2101 PMU + GPS power rail
  03_gps_test/           Stage 3: TinyGPSPlus NMEA parsing
  04_trip_logger/        Stage 4: the trip logger, plus its partitions.csv
  host_test/             Host-side tests for Stage 4 (run.sh, stubs/)
  STAGE4_TECHNICAL_BRIEF.md  Stage 4 design brief
  secrets.h.example      Copy to secrets.h and fill in WiFi + server creds
  README.md              Full setup guide + pin reference

server/
  docker-compose.yml     Compose definition (ports 8090 + 8091)
  .env.example           Copy to .env, set INGEST_AUTH_TOKEN and BIND_ADDR
  viewer.env.example     Copy to viewer.env, set the viewer login
  install.sh             First-time install with checks; --check re-verifies
  mileage-ingest/        Flask ingest service (app.py, Dockerfile)
  mileage-viewer/        Flask viewer/export service (app.py, static/)
  backup/                backup.sh + restore.sh, systemd timer, backup.conf.example
  tests/                 Server tests (unittest) + map-parser check (node)
  sample.gpx             Test upload for the end-to-end check
  README.md              Deployment + curl test guide

tools/check_pii.py                  Personal-info scan (CI runs it; see Do Not)
.github/workflows/audit-tests.yml   CI: PII scan + server tests + firmware host tests
.github/dependabot.yml              Dependabot: monthly pip, Docker and Actions bumps
CODE_REVIEW_2026-06-09.md, AUDIT_2026-08-04.md, AUDIT_2026-10-05.md,
INDEPENDENT_AUDIT_2026-10-05.md      Review history (see Current Status)
```

## Current Status (as of 2026-10-15)

Code is written and tested on both sides. Nothing is deployed, and the
firmware hasn't been road-tested.

- **History and visibility:** this repo starts from a clean snapshot
  taken 2026-10-13. Earlier commits, PRs and closed issues stay in a
  private, archived repo, so commit hashes and PR numbers in the audit
  docs point there. This repo is private for now; the owner plans to make
  it public once the server is deployed and the tracker works on the road.
- **Where status lives:** GitHub issues, not this file. Every open
  finding or check is its own issue, labelled by audit
  (`audit-2026-08-04`, `audit-2026-10-05`), area and severity. The old
  trackers **#1** (October, N1–N14) and **#2** (August, B1–B5, F1–F11) are
  closed, but still record what was fixed. Check `gh issue list` before
  assuming something is open or fixed.
- **Audits:** `CODE_REVIEW_2026-06-09.md` → `AUDIT_2026-08-04.md` →
  `AUDIT_2026-10-05.md` (findings, fix-status table, bench results) →
  `INDEPENDENT_AUDIT_2026-10-05.md`. The fix-status table in
  `AUDIT_2026-10-05.md` covers only the first round of fixes; the closed
  trackers #1 and #2 have the rest.
- **Fixed:** every August finding except F11 (#6), and October N1–N12.
  N5 shipped without a commuting category, per the owner. The August
  low-severity firmware issues #16–#18 (WiFi retry backoff, trip-path
  helper, mutex check) and installer #19 and #23 are fixed too.
  Firmware #56, #60 and #77 fix upload queue starvation, retention of long
  untimed trips, and WiFi failure diagnostics.
  Firmware #76 provisions fully erased trip storage on first boot without
  enabling format-on-mount-failure.
- **Still open:** #4
  (N13: the backup tooling is in `server/backup/`, but it isn't set up on
  the server or restore-tested from a real off-host copy yet), #6 (F11, network stack sizing; needs
  measurements from a real upload), the shakedown checks #25 (deep-sleep
  current), #26 (GPS hot start) and #27 (road test), and
  the low-severity issues, one per finding:
  installer #20 and #21 (`gh issue list --label "severity: low"`).
- **Firmware:** Stages 1–3 verified on hardware. Stage 4 compiles on ESP32
  core 3.3.11 (XPowersLib 0.3.3, TinyGPSPlus 1.0.3) with the bundled
  `partitions.csv` (2.375 MB LittleFS). Trip files are **GPX 1.0** at ~119 B/point (1.0 allows `<speed>` and
  `<hdop>` directly on `<trkpt>`). The M8N is configured over UBX at boot
  for RMC+GGA only, 1 Hz, automotive dynamic model.
- **Bench test (2026-10-05, board on USB):** current build flashed, the
  partition mounts, the M8N ACKs the UBX config, ALDO3 measured as the GPS
  rail, and the charge guard calibrated against the die temperature.
  Details in `AUDIT_2026-10-05.md` → Bench results. Still needs the car or
  a meter: deep-sleep current (#25), hot-start time (#26), the F11 stack
  high-water mark (#6), and the road test (#27).
- **Tests (CI runs all of these on every PR):**
  - PII: `python tools/check_pii.py` (add `--commits origin/main..HEAD`
    before pushing) and `python -m unittest discover -s tools`.
  - Server: `python -m unittest discover -s server/tests` and
    `node server/tests/test_viewer_map.js`. Needs the service requirements
    installed. On Windows, `test_viewer_security` reports a tearDown error
    because SQLite still holds the temp DB; the tests themselves pass.
    `test_backup` needs bash and rsync, so it skips on Windows; run it in
    WSL (`wsl -d Ubuntu -- python3 -m unittest server/tests/test_backup.py`).
  - Firmware: `bash firmware/host_test/run.sh` (`test_fs`, and `test_gps`
    against the real TinyGPSPlus). No host compiler on this PC: set
    `CXX="<venv python> -m ziglang c++"`.
- **Server deployment: pending.** Runbook is `DEPLOY_DEBIAN.md`.

## Key Technical Details

- GPS power: AXP2101 **ALDO3** rail at 3.3V (measured 2026-10-05: ALDO3 off
  silences the GPS, ALDO4 off doesn't; ALDO3 is on at power-up). VBACKUP
  (button-battery charge) is enabled for GPS hot starts. ALDO2 = LoRa + OLED
  header, on at power-up — leave it alone
- GPS UART: GPIO 34 (RX from GPS), GPIO 12 (TX to GPS), 9600 baud
- I2C (PMU): GPIO 21 (SDA), GPIO 22 (SCL)
- Ingest port: 8090, bearer token auth, GPX body in → raw `.gpx` + derived
  `.json` summary out (atomic writes); distance = haversine over moving
  steps only (speed > 2 mph at either end), HDOP > 5 and > 60 m/s jumps
  skipped, GPS gaps bridged by straight line (`summarize()`, tests in
  `server/tests/`)
- Upload limits: 4 MiB body cap (`INGEST_MAX_UPLOAD_BYTES`); the device's
  largest possible trip is ~2.4 MB, its whole partition. gunicorn
  `--timeout 300`, because the timeout includes reading the body and 30 s
  dropped big trips on slow links. Early 401/413 replies read the body
  first so the device sees the status, not a reset.
  `server/tests/test_upload_limits.py` ties these to `partitions.csv`
- Viewer port: 8091, session login (LAN-only), reads `./data` read-only, owns
  `annotations.db` (SQLite) in the `viewer-db` volume
- Port binding: `BIND_ADDR` in `.env` sets the host IP both ports publish
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
- Trip detection: start >5 mph for 10s; end <2 mph for 3 min
- LittleFS: use `file.close()` not `file.flush()` for durability
- FreeRTOS: networking on core 0, GPS on core 1
- Five-state FSM for trip lifecycle

## What To Do Next

1. **Deploy on Debian** with `server/install.sh`: copy `server/` over,
   then `bash ~/mileage-tracker/install.sh`. It does `DEPLOY_DEBIAN.md`
   Steps 1 and 3–10 with checks, including the `sample.gpx` end-to-end
   test, and is safe to re-run. `install.sh --check` re-verifies later.
2. **Check the viewer by hand** at `http://<host>:8091/`: annotate a
   trip and confirm it persists (the installer doesn't write annotations).
3. **Set up backups** (#4): the installer's Backups step, or
   `DEPLOY_DEBIAN.md` Step 10 by hand. #4 closes once that's done on the
   real server and `restore.sh check` passes from the off-host copy.
4. **Road test and shakedown**, one issue per check: deep-sleep current
   (#25), hot start after sleep (#26), the road test for F10 through
   engine crank and start-stop and the N1 jitter figure (#27), and the F11
   stack measurement during a real upload (#6; procedure in
   `firmware/README.md` → Network stack shakedown).
5. **Remaining code:** the low-severity installer issues #20 and #21.

When a PR fixes an issue, say "Fixes #N" (one line per issue) so merging
it closes the issue. File every new finding as its own issue, however
small, never as an item on a checklist issue.

## Do Not

- Put IRS classification logic in firmware — server-side only
- Power SX1262 LoRa without antenna (RF damage risk)
- Commit `secrets.h`, `.env`, or `viewer.env` to version control
- Commit anything personal. The repo will go public, and that exposes
  everything from its private period too, so docs, examples, tests,
  commit messages, issues and PRs use placeholders (`<user>`, `<host>`,
  `<username>`, `~\Documents\...`), never a real name, account path,
  email, SSID/MAC, home coordinates, or a photo with EXIF. Commit with the
  GitHub noreply email. `tools/check_pii.py` enforces this in CI for files
  and commits (not issue or PR text); names come from the `PII_DENYLIST`
  secret or a gitignored `.pii-denylist`
- Change the repo's visibility. The owner decides when it goes public
- Bring commits over from the private archive repo: no merge, cherry-pick,
  or adding it as a remote. Its history carries a personal email and
  account paths, which is why this repo started from a clean snapshot.
  Copy file contents across instead, and run `check_pii.py` before pushing
