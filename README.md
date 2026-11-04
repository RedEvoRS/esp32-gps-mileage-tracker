# GPS Mileage Tracker (without a phone)

Deducting business mileage requires a log: the date, distance, destination,
and business purpose of each trip. Phone apps will keep one, but they depend
on the phone being in the car with location access, and may require a
subscription or send your location history to the vendor's servers.

This project moves mileage logging onto a small GPS board that lives in the
car: a LilyGO **T-Beam Supreme with the u-blox MAX-M10S GPS** (choose the
MAX-M10S version, not the L76K version). It detects trips from GPS speed,
saves each track as a GPX (GPS Exchange Format) file on a **SanDisk 32 GB
Max Endurance** microSD card, and uploads the stored trips over your home
WiFi when you park.

The server side is two small Flask services on your local network (LAN).
One accepts the uploads and computes trip distance. The other shows each
trip on a map, records its business purpose, and exports the log as CSV
(comma-separated values) or XLSX (Excel) files. Tracks and annotations stay
on the local network by default. The viewer requests
OpenStreetMap tiles when it draws a map. If you enable place lookup, it also
sends the clicked trip endpoint to the provider you configure, which may be
outside your network. The tracker asks an internet time server for the time
when GPS time is unavailable.

## Where it's at

The tracker and server are in daily use. **v1.00**, the first public
release of both the firmware and the server, came out on 2026-11-01 (UTC),
after a week of everyday driving on the release candidate in which every trip
was recorded, uploaded and exported correctly. Firmware **v1.01** followed on
2026-11-03 (UTC): it keeps reporting the board's health when the microSD card
is missing or failing. Firmware **v1.02** followed on 2026-11-04 (UTC): a card
that stops responding while the board runs is reported as missing (E4001)
rather than full, and new trips move to internal flash. Still unproven on the Supreme: trips that end in a
parking garage, and distance accuracy against the car's odometer. Open work is tracked in
[GitHub issues](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues).

**Have the older T-Beam V1.2?** This project started on the LilyGO TTGO
T-Beam V1.2 (ESP32 + NEO-M8N) and no longer supports it. Its last firmware
and instructions are on the frozen
[`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2); nothing on `main` applies to
it.

## How it works

```
Target device: LilyGO T-Beam Supreme (ESP32-S3 + MAX-M10S GPS)
       | records GPX to
       v
SanDisk 32 GB Max Endurance microSD
       | uploads over home WiFi
       v
POST /upload (GPX) --> mileage-ingest --> GPX + JSON summary
                                      |
                                      v
                         read-only viewer and exports
```

If no card mounts at boot, the logger stores trips in the board's internal
flash instead (about 5–6 hours of driving between uploads) and uploads them
from there; that was checked on the road on 2026-11-03 (UTC). From firmware
v1.01 the health journal keeps uploading without the card too, so the viewer's
Device health shows the missing card as error E4001. From v1.02, a card that
fails or is pulled while the board is running is also reported as E4001, and
the next trip records to internal flash until the board is restarted.

Alongside the trips, the board keeps a small journal of its own health:
numbered error codes such as a lost GPS antenna, a full card or a refused
upload. It uploads the journal after the trips, and the viewer's **Device
health** page shows it. The server can optionally send those faults to your
phone through a self-hosted ntfy notification service.
[DIAGNOSTICS.md](DIAGNOSTICS.md) describes the whole framework and lists every code.

The logger decides on its own when a trip starts and stops. Going faster than 5 mph for 10 seconds starts one; staying under
2 mph for 5 minutes ends it, allowing longer red lights without splitting
a drive. When the stop timer or five minutes without a GPS fix ends a trip,
it is discarded if shorter than 30 seconds of driving or about a tenth of a
mile. Switching the car off keeps even a short trip that has started.

The firmware writes each GPS point to the microSD card (or the internal
flash fallback) and closes the file right after to limit data loss if
power is cut. Recovery from a power cut during a write has not been tested
on the board.

The firmware runs from the car's USB port, with an 18650 battery as backup.
When the car's port goes dead, it closes out the trip and keeps running on
battery for 10 more minutes to upload over home WiFi (longer if an upload
is still running). Then it switches itself off completely, and power coming back to the USB port switches it on
again when you next start the car.

The server adds up distance point to point, with a few corrections. Wobble
while you're parked doesn't count (at least one end of a step must be over
2 mph), bad fixes and physically impossible jumps get skipped, and a gap in
coverage like a tunnel or a parking garage gets filled with a straight line.
That gap estimate is the shortest path between the measured endpoints, so it
can miss road distance through the gap. The total is still an estimate: GPS
position noise can add distance even on accepted points. Accuracy has not
been measured against the car's odometer; the driving-week pass is not a
numeric accuracy guarantee.

## What you need

- **LilyGO T-Beam Supreme with u-blox MAX-M10S GPS** (not L76K), a
  protected **18650 cell**, and a **microSD card formatted FAT32**. A
  SanDisk 32 GB Max Endurance is the tested card; exFAT is unsupported.
  See [microSD card](firmware/README.md#microsd-card).
- Attach the GPS antenna to **GPS** and the correct **915 MHz LoRa antenna
  before inserting the battery or connecting USB**: factory firmware may
  transmit even though this project does not use LoRa. Match the battery's
  polarity to the holder. Use a car USB port that switches off with the ignition.
- **A case.** The tested one is a
  [remix of Alley Cat's T-Beam Supreme case with an external GPS window](https://makerworld.com/en/models/3496457-alley-chat-ts2-tbeam-supreme-remixed-external-gps)
  (CC BY-SA, like the [original](https://makerworld.com/models/574656-alley-chat-ts2-new-tbeam-supreme-case-by-alley-cat)),
  printed in PETG, which copes with a hot car better than PLA. The original
  case knocks the GPS antenna plug off; see
  [Fitting it in the case](firmware/README.md#fitting-it-in-the-case).
- **A Debian 13 server** on your home network, with a stable LAN address,
  SSH (Secure Shell) access, a normal login with sudo, and
  **Docker Engine with Compose v2** already usable by that login.
  [Deployment Steps 0–1](DEPLOY_DEBIAN.md#step-0--confirm-ssh-works)
  cover preparation. Allow space for Docker images and trips; the firmware
  builder image uses about 2.7 GB extra; allow 10 GiB free for its first build.
  Automatic backups currently require the
  server login's numeric user ID to be 1000; see [backups](DEPLOY_DEBIAN.md#step-10--backups).
- **A Windows, macOS or Linux PC**, a **USB-C data cable**, and OpenSSH
  (`ssh` and `scp`) access to the server. macOS/Linux also need Bash, curl,
  tar and jq; see [flashing prerequisites](firmware/README.md#flash-from-the-server-build).
- **Visible 2.4 GHz home WiFi** reaching your parking spot; 5 GHz and hidden
  networks do not work.

## Setting it up

Replace angle-bracket placeholders, including the brackets, with your values.
`<user>@<host>` is your server SSH login and address. The viewer has an admin
login and may also have an optional everyday login; both are separate from
the board upload token.

The one-liners download scripts and the source bundle from **this repo's
GitHub releases**, and the flash scripts download **esptool from Espressif's
GitHub releases**. They work starting with the first release carrying these
assets: [the release workflow](.github/workflows/release.yml) attaches them
when a release is published. Docker and toolchain dependencies need internet
access too. Piping `curl` into Bash (or `irm` into `iex`) executes downloaded
code with your account's permissions: do this only if you trust that source.
The download, read, then run alternatives below let you inspect the script
first; the bundle checksum does not authenticate the script you already ran.

<a id="1-get-the-code"></a>
<a id="2-set-up-the-server"></a>

### Step 1 — Set up the server

Debian 13 is the main path below. **Unraid 7.3+ (7.x)** is also supported;
follow [the Unraid runbook](DEPLOY_UNRAID.md) for root login, pool storage,
Docker prerequisites and cron backups. Unraid paths have only fake-test
coverage so far; real-host checks remain. Unraid 8.x is not supported yet.

Connect with `ssh <user>@<host>`. **In that SSH session on the Debian server**,
run as your normal login, not root:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh | bash
```

Or download, inspect, then run:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh -o setup.sh
less setup.sh
bash setup.sh
```

It verifies the release bundle, copies it into `~/mileage-tracker`, and runs
`install.sh` to configure, build and check the server. Answer its LAN address,
timezone, admin login, optional everyday login, backup and optional
notification questions. Docker's
published ports bypass UFW: choose the server's LAN address for `BIND_ADDR`,
not all interfaces. See the [deployment runbook](DEPLOY_DEBIAN.md).

**Accept “Build your tracker firmware now”** to prepare Step 2. The builder
asks before its first **toolchain download of about 1.8 GB** (about 2.7 GB
installed image); later builds reuse it.
Enter your WiFi settings, a name for the board (`DEVICE_ID`, default
`tbeam-01`), and confirm the server address and port (default 8090). It reads
the upload token from the server's `.env`. For a new device it also checks
the server's saved diagnostics revision and asks whether the SD card is
fresh and the board has never used an enrollment ID; answer truthfully.
It prepares the one-time enrollment and cleanup images when those checks
allow it. Recovery for a reused board/card is in
[DIAGNOSTICS.md](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery).

**Keep `firmware-out/` private:** its owner-only directories/files (0700/0600)
contain firmware with your WiFi password and upload token built in. Do not
share or commit them. The settings are compiled in; changing WiFi, token or
server address requires another build and flash. When setup finishes, use
its printed Step 2 commands (they select the same release), then type `exit`
to return to your PC.

<a id="3-flash-the-firmware"></a>

### Step 2 — Flash the tracker

The Windows app-only update path has been checked on the Supreme. The
`flash.ps1` first-install and `-Cleanup` paths, and all of `flash.sh`, remain
untested on the board; script tests do not establish that they work on hardware.

**On the PC connected to the board**, close any serial monitor and run the
command for your OS. These latest-release forms also work; use the commands
printed by Step 1 when selecting a specific release or installation directory.

**Windows PowerShell (5.1 or 7):**

```powershell
irm https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 | iex
```

It asks for `<user>@<host>` or a bare SSH config alias, `<alias>`, from your
PC's SSH config. To inspect it and supply `-Server`, save it first
(the `irm | iex` form cannot pass script arguments):

```powershell
Invoke-WebRequest https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 -OutFile flash.ps1
Get-Content .\flash.ps1
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server '<user>@<host>'
```

You can also use `-Server '<alias>'`. The script prefers Windows OpenSSH's
`scp.exe`, so launching PowerShell from Git Bash does not select Git's msys
`scp` when Windows OpenSSH is installed. If copying fails with "Connection
closed", retry in a normal PowerShell window and check that Windows OpenSSH
and your SSH agent are available there.

**macOS/Linux Terminal:**

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.sh | bash -s -- --server '<user>@<host>'
```

Or download, inspect, then run:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.sh -o flash.sh
less flash.sh
bash flash.sh --server '<user>@<host>'
```

The script copies your private firmware over SSH and checks its hashes.
Both scripts accept `<user>@<host>` or `<alias>` (`--server '<alias>'` on
macOS/Linux). Targets allow letters, digits, underscores, dots and hyphens;
each component must start with a letter, digit or underscore. Spaces, IPv6
literals and shell characters are rejected.

**For the first install on a factory board, answer yes to first install.**
Follow its manual download-mode prompt: hold **BOOT**, plug in USB (or tap
**RESET**), wait 2–3 seconds, release BOOT, then select the port (`COMx` on
Windows; it can change in download mode).
The script reads the full 8 MB factory flash twice and checks the copies
match. Keep the backup privately outside the repo; it may contain device
keys. You must then type **`ERASE`**: this erases all internal flash,
including settings and any internal trips, before writing all four firmware
parts. The SD card is untouched. Updates use the default app-only path:
select the running board's port and the script first tries automatic USB
reset into download mode. If that fails, follow the BOOT/RESET instructions
and enter the new port if it changed. Cleanup uses the same automatic attempt.
After writing, the script requests a hard reset; if the board stays in
download mode, press RESET with BOOT released. See the
[firmware guide](firmware/README.md#flash-from-the-server-build) for details.

<a id="4-check-it-works"></a>
<a id="5-put-it-in-the-car"></a>

### Step 3 — First drive and Device health

Fit the FAT32 card with the board switched off. Take a drive of a few minutes
outdoors, with the GPS antenna facing the sky and the battery fitted. Trips
start after 10 seconds above 5 mph. Park within home WiFi range. If USB stays
powered, allow five minutes below 2 mph for the trip to finish, plus time for
uploads/retries. Switching the car off ends the trip sooner, once USB loss is
confirmed after 10 seconds; the battery keeps the board running to upload.
Trips ended by the stop timer or lost GPS are discarded if shorter than
30 seconds of driving or about a tenth of a mile. Car power-off keeps short
trips that have started.

Open `http://<host>:8091/` and sign in with the admin login. You can add an
optional everyday login during setup for whoever annotates trips. The
everyday login can annotate, merge and export trips and see Device health. Only
the admin login can remove trips (reversibly), see the change history, manage
everyday logins, and close or reopen a tax year; a closed year blocks edits
for everyone. The full table is in
[Viewer Login](server/README.md#viewer-login). Check the
trip, then open **Device health** and check for a newly received
**current status snapshot**. A listed device or recent trip contact alone does not prove
that diagnostics enrollment succeeded. See
[detailed checks](firmware/README.md#check-recording-and-uploads) and
[car installation](firmware/README.md#put-it-in-the-car).

**After the first diagnostics snapshot, do the one-time cleanup flash**
using the saved Step 2 script, before rebuilding firmware on the server.
If you used the one-liner, download the script using Step 2 first.

The commands below use the default server directory, `~/mileage-tracker`.
For a custom directory or Unraid, also pass `-InstallDir '<install-dir>'`
on Windows or `--install-dir '<install-dir>'` on macOS/Linux, using the
same server path as Step 2.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server '<user>@<host>' -Cleanup
```

```bash
bash flash.sh --server '<user>@<host>' --cleanup
```

This selects `firmware-out/cleanup/`, removes the one-use enrollment values,
and writes only the app. It preserves the board's settings, trip counter,
diagnostics metadata and queued trips. It tries automatic download mode,
with button instructions if needed. [Enrollment and recovery](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery)
explains why those values must be removed.

## Updating

Rerun **Step 1 on the server**, accept the firmware build, and enter/confirm
your settings again. It updates supplied source files and rebuilds/checks
the services; private configuration and trip data are retained. Then rerun
**Step 2 on your PC**, answering **no** to first install. Do not use
`-FirstInstall` / `--first-install` on an installed tracker: a normal update
writes only the app and preserves settings and trips. Update the server
before the board so it understands the new firmware's diagnostics.
See the [update runbook](DEPLOY_DEBIAN.md#updating).

## Advanced: build from source

To build locally, clone the repository (or use GitHub's **Code → Download ZIP**):

```bash
git clone https://github.com/RedEvoRS/esp32-gps-mileage-tracker.git
cd esp32-gps-mileage-tracker
```

Follow the [Arduino IDE / arduino-cli source route](firmware/README.md#advanced-build-from-source).
It is an ordered checklist from installing the tools to uploading. That
route keeps the progressive hardware-test sketches, copying
`firmware/secrets.h.example` to `firmware/04_trip_logger/secrets.h`, version
stamping, and the Supreme board/library settings. First flash still requires
a factory backup and a one-time erase; later uploads keep erase disabled.

Separately, if you also want to install the server by hand rather than with
the setup script, see the
[manual server copy](DEPLOY_DEBIAN.md#manual-alternative-copy-server).

## The data

Trips are stored as GPX, so you can open the raw files in GPXSee,
gpx.studio, Google Earth, QGIS, or pretty much any other map tool. Next to
each one the server writes a small JSON summary so the viewer doesn't have
to re-read every track to list them. Trimmed down, one looks like this:

```json
{
  "trip_id": "2026-04-14T13-45-22Z",
  "start_time": "2026-04-14T13:45:22+00:00",
  "end_time": "2026-04-14T14:07:41+00:00",
  "distance_miles": 12.417,
  "max_speed_mph": 63.2,
  "num_points": 1287
}
```

On the board, the GPX files stay on the microSD card (or in internal flash,
without a card) until the server accepts them; see
[microSD card](firmware/README.md#microsd-card). You can also take the card
out, with the board switched off, and read the files on a PC.

Once a trip is on the server, its files are never edited. The viewer mounts
the trip folder read-only and keeps your notes in its own SQLite database, so
editing a trip's purpose can't touch the record underneath. Removing a trip
and merging trips are recorded in that same viewer database; the original
`.gpx` and `.json` files stay where they are. If the summary ever gets
lost, it can be rebuilt from the GPX.

Times are stored in UTC, which is what the GPS reports, and shown in your
local timezone everywhere a person reads them. That way a 6pm drive shows
up as 6pm on the right day instead of 1am the next one. Trips are also
filed by local month, so a New Year's Eve evening drive lands in `2026-12/` and
not `2027-01/`. There's no built-in timezone: the server installer asks for
yours and saves it as `DISPLAY_TZ` in the installed server's `.env`, and
both services refuse to start until it's set. Both read that one setting.
A trip is filed under the local month the drive ends, but dated and counted by its local
start, so a drive that starts just before midnight on December 31, 2026 and
ends after midnight sits in the `2027-01/` folder and shows as December 31
(see "Years are based on trip start" in
[server/README.md](server/README.md#viewer--export-ui)).

## Security

See [SECURITY.md](SECURITY.md) for private vulnerability reporting, supported versions and the threat model.

Both services are meant for your home network only. Don't expose them to the
internet.

The upload endpoint checks a bearer token and the viewer has a login, but
it's all plain HTTP, so anyone who can see the traffic can see the
credentials. The login keeps other people and devices on your network out of
your trips; it isn't a replacement for TLS (Transport Layer Security, the
encryption behind HTTPS). For HTTPS names through Caddy or Nginx Proxy
Manager (NPM), set `VIEWER_COOKIE_SECURE=true` in the installed server's
`.env` (`server/.env` in a source checkout) and run `docker compose up -d`
from the directory containing `docker-compose.yml`. `VIEWER_TRUSTED_PROXIES`
lets login lockout count each client when the proxy supplies its real address. NPM also
needs the Advanced setting and forged-header check in the guide below;
its defaults let LAN clients forge their address and evade that lockout.
Use a strong viewer password. `VIEWER_BIND_ADDR` can limit only the viewer's
8091 port to loopback while ingest stays on `BIND_ADDR` port 8090.
The [private reverse-proxy guide](DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager)
covers Docker networking, certificates and Tailscale access without internet exposure.
If it also fronts ingest, allow bodies of at least `INGEST_MAX_UPLOAD_BYTES`
(16 MiB by default; firmware pieces stay under 8 MiB) and slow uploads
(gunicorn allows 900 s). A proxy's HTTP 413 sets the trip aside as `.bad`
on the card, never retried and missing from the mileage log. See
[proxy settings and nginx's 1 MiB default trap](server/README.md#security-notes).

Beyond that, the viewer protects its forms against CSRF (cross-site request
forgery, where another site tries to submit a form using your login) and
locks out an IP for 15 minutes after five wrong passwords. It also refuses
to start at all if its login settings are missing or mangled, since a viewer
that quietly runs with no auth is worse than one that won't run. Both
containers run as a non-root user.

`secrets.h`, `.env`, `viewer.env` and `monitor.env` are in `.gitignore`. Please keep them
there.

## What's where

```
firmware/
  01_hello_world/   02_pmu_test/   03_gps_test/   the warm-up stages
  04_trip_logger/   the real firmware
  host_test/        runs the firmware logic on a PC
  README.md         toolchain setup and pinout
server/
  mileage-ingest/   upload endpoint
  mileage-viewer/   web UI
  tests/            server test suite
  install.sh        first-time install, and --check afterwards
  README.md         service details and curl tests
DEPLOY_DEBIAN.md    deployment walkthrough
```

Past code reviews are kept in [docs/history/](docs/history/); its
[README](docs/history/README.md) explains the historical records.

## A couple of notes

The board only records where you went. Deciding what a trip was *for* is
your call, made afterward in the viewer. That logic stays on the server,
where it's easy to change.

None of this is tax advice. Whether your log holds up with the IRS is on
you.

## License

GPLv3, see [LICENSE](LICENSE). Copyright (C) 2026 RedEvoRS. You're free to
use, change, and share it. If you distribute it, including on a device with
it flashed, you have to offer the source under the same license. It comes
with no warranty.

The viewer bundles two BSD-2-Clause libraries in
[server/mileage-viewer/static/](server/mileage-viewer/static/), with their
copyright notices kept in the files:

- Leaflet 1.9.4, © 2010–2023 Vladimir Agafonkin, © 2010–2011 CloudMade
- leaflet-gpx, © 2011–2012 Pavel Shramov, © 2013–2017 Maxime Petazzoni

Map tiles come from OpenStreetMap (© OpenStreetMap contributors, ODbL).
Your browser fetches them when you view a map; they aren't included here.
