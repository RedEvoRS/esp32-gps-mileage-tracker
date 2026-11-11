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
(comma-separated values) or XLSX (Excel) files.

Tracks and annotations are stored on your own server. These features
contact services outside it:

- The viewer fetches OpenStreetMap tiles to draw maps.
- If you turn on place lookup, the viewer sends the trip endpoint you click
  to the provider you configure. That provider can be your own Nominatim
  server on your network, which keeps lookups completely local; it needs
  disk space and memory for your area's map data. See
  [place suggestions](server/README.md#endpoint-place-suggestions).
- When GPS time isn't available yet, the tracker asks the public time
  servers `pool.ntp.org` (first) and `time.nist.gov` (backup) for the time,
  only while it's on your home WiFi to upload. These addresses are built
  into the firmware.

## Where it's at

The tracker and server are in daily use. **v1.00**, the first public
release, came out on 2026-11-01 (UTC) after a week of everyday driving in
which every trip was recorded, uploaded and exported correctly. Later fixes
are listed on the
[releases page](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases).

For maintainers, merging a `firmware/VERSION` bump publishes its firmware
release as **Latest** immediately, before the server is deployed or the board
is flashed. The PR includes the reviewed summary in `release-notes/`; the
workflow then uploads the install/flash scripts, source bundle and checksums.
Server `srv-` tags stay manual after a verified deploy. To finish a failed
run, re-run all Firmware release jobs, or manually run Release assets with
the published tag if only assets failed. See
[Firmware versions and releases](firmware/README.md#firmware-versions-and-releases).

Still unproven on the Supreme: trips that end in a parking garage, and
distance accuracy against the car's odometer. Open work is tracked in
[GitHub issues](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues).

**Have the older T-Beam V1.2?** This project started on the LilyGO TTGO
T-Beam V1.2 (ESP32 + NEO-M8N) and no longer supports it. Its last firmware
and instructions are on the frozen
[`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2);
nothing on `main` applies to it.

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

The board decides on its own when a trip starts and stops. Going faster than
5 mph for 10 seconds starts one. Staying under 2 mph for 5 minutes ends it,
so a long red light doesn't split a drive. A trip ended by that stop timer,
or by five minutes without a GPS fix, is discarded if it's shorter than
30 seconds of driving or about a tenth of a mile. Switching the car off keeps
even a short trip once it has started.

The board writes each GPS point to the trip file and closes the file straight
away, to limit data loss if power is cut. Recovery from a power cut during a
write hasn't been tested on the board.

The board gets power from the car's USB port, with the 18650 battery as
backup.
When the port goes dead, it closes the trip and stays on battery for 10 more
minutes to upload over home WiFi (longer if an upload is still running). Then
it switches itself off completely, and switches back on when the USB port
gets power the next time you start the car.

Alongside the trips, the board keeps a small journal of its own health:
numbered error codes such as a lost GPS antenna, a full card or a refused
upload. It uploads the journal after the trips, and the viewer's **Device
health** page shows it. The server can also send those faults to your phone
through a self-hosted ntfy notification service.
[DIAGNOSTICS.md](DIAGNOSTICS.md) describes the whole framework and lists
every code.

If no card mounts at boot, the board stores trips in its internal flash
instead (about 5–6 hours of driving between uploads) and uploads them from
there. If the board detects that the card has stopped responding while it
runs, new trips use internal flash, if available, until restart; the current
trip stays on the card for recovery.
Either way, Device health shows the missing card as error E4001.

The server adds up distance between GPS points. A step counts only when at
least one end is above 2 mph, so wobble while parked doesn't add miles. Bad
fixes and impossible jumps are skipped, and a gap in coverage, like a tunnel
or parking garage, is joined with a straight line. That line can miss road
distance, and GPS noise can add some, so the total is an estimate.

## What you need

- **LilyGO T-Beam Supreme with u-blox MAX-M10S GPS** (not L76K), a
  protected **18650 cell** (match its polarity to the holder), and a
  **microSD card formatted FAT32**. A SanDisk 32 GB Max Endurance is the
  tested card; exFAT is unsupported. See
  [microSD card](firmware/README.md#microsd-card).
- **An external GPS antenna (recommended).** The small GPS antenna that
  comes with the board has a short cable and sits inside the case, where it
  won't have a clear view of the sky from inside a car. Use an external
  active GPS antenna that works at 3.3 V, with an SMA plug, plus a short
  IPEX (u.FL) to SMA adapter cable for the board's **GPS** socket. Mount the
  antenna where it can see the sky, such as the dashboard or rear shelf.
  Tested with a Geekstory active GPS+GLONASS antenna (3–5 V, SMA male, 3 m
  cable, magnetic base); any antenna with those specs should work.
- **The board's 915 MHz LoRa antenna**, fitted before the board first gets
  power; see the warning below.
- **A car USB port that switches off with the ignition.** The board uses
  that power cut to end the trip and, later, the power coming back to
  switch on.
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
  cover preparation. Leave room for Docker images and trips, plus 10 GiB
  free for the first firmware build (its builder image stays at about
  2.7 GB). Automatic backups currently need the server login's numeric user
  ID to be 1000; see [backups](DEPLOY_DEBIAN.md#step-10--backups).
- **A Windows, macOS or Linux PC**, a **USB-C data cable**, and OpenSSH
  (`ssh` and `scp`) access to the server. macOS/Linux also need Bash, curl,
  tar and jq; see [flashing prerequisites](firmware/README.md#flash-from-the-server-build).
- **Visible 2.4 GHz home WiFi** reaching your parking spot; 5 GHz and hidden
  networks do not work.

> [!CAUTION]
> **Attach the 915 MHz LoRa antenna before the board gets any power:**
> before inserting the battery and before plugging in USB. The board ships
> with factory firmware that can transmit, and transmitting without an
> antenna can permanently damage the LoRa radio. This project doesn't use
> LoRa, but the antenna must stay on until this project's firmware is
> flashed (Step 2).

> [!CAUTION]
> **Hot climates: the 18650 battery can overheat in a parked car.** A car
> left in the sun can get hot enough inside to damage a lithium-ion cell,
> and a damaged or overheated cell can swell, leak, vent or catch fire. The
> firmware pauses charging when the board's power chip reports a high
> temperature, but that's an indirect estimate, not a reading of the cell,
> and it can't cool a cell that heats up while the car is parked.
>
> To lower the risk:
>
> - **Keep the board out of direct sunlight and mount it as low in the car
>   as you can**, such as a footwell or under a seat. The dashboard and rear
>   shelf get the hottest. With an external GPS antenna, the antenna can
>   sit where it sees the sky while the board stays low; check that your
>   home WiFi still reaches the board there.
> - **Replace the cell** if it's swollen, dented, leaking or its wrap is
>   torn.
>
> **You build and use this at your own risk.** The authors and contributors
> accept no liability for any damage, injury, fire or other loss arising
> from the hardware, the battery or the software. See [License](#license):
> it comes with no warranty. More on the battery in
> [Put it in the car](firmware/README.md#put-it-in-the-car).

## Setting it up

The setup scripts download from GitHub: the scripts and source bundle come
from **this repo's releases**, and the flash scripts fetch **esptool from
Espressif's releases**. Docker and the firmware toolchain need internet access
too.

Piping `curl` into Bash (or `irm` into `iex` on Windows) runs the script
immediately with your account's permissions, so only do it if you trust the
source. Each step below also shows how to download the script and read it
before running it. The bundle checksum checks the files the script downloads,
not the script itself.

<a id="1-get-the-code"></a>
<a id="2-set-up-the-server"></a>

### Step 1 — Set up the server

Debian 13 is the main path below. **Unraid 7.3+ (7.x)** is also supported;
follow [the Unraid runbook](DEPLOY_UNRAID.md) for root login, pool storage,
Docker prerequisites and cron backups. Automated tests cover the
Unraid-specific setup using fake host commands, but setup hasn't yet been
checked on a real Unraid server. Unraid 8.x is not supported yet.

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
`install.sh`. That asks for the server's LAN address, your timezone, the
admin login, an optional everyday login, backups and optional notifications,
then builds and checks the server. For `BIND_ADDR`, choose the server's LAN
address, not all interfaces: Docker's published ports bypass the UFW
firewall. See the [deployment runbook](DEPLOY_DEBIAN.md).

**Accept “Build your tracker firmware now”** to prepare Step 2. The first
build asks before downloading a **toolchain of about 1.8 GB** (2.7 GB once
installed); later builds reuse it. It asks for:

- your WiFi settings
- a name for the board (`DEVICE_ID`, default `tbeam-01`)
- the server address and port (default 8090)

It reads the upload token from the server's `.env` itself. If the server has
no saved diagnostics revision for this device name, it also asks whether the SD card is
fresh and whether the board has never been enrolled. Answer truthfully: only
two yeses build the one-time enrollment and cleanup firmware. Reusing a board,
card or device name? See
[enrollment recovery](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery).

**Keep `firmware-out/` private:** its owner-only directories/files (0700/0600)
contain firmware with your WiFi password and upload token built in. Do not
share or commit them. The settings are compiled in; changing WiFi, token or
server address requires another build and flash.

When setup finishes, note the Step 2 commands it prints, then type `exit` to
return to your PC.

<a id="3-flash-the-firmware"></a>

### Step 2 — Flash the tracker

**On the PC connected to the board**, close any serial monitor and run the
command below for your operating system. The commands printed by Step 1 select the release you
installed. For a custom server folder, use its Windows saved-script command
or its macOS/Linux command. The examples below use the latest release and the
default folder.

Only the Windows update path has been tested on a real board so far. First
installs, `-Cleanup`, and everything in `flash.sh` (macOS/Linux) have passed
script tests but not a hardware run.

**Windows PowerShell (5.1 or 7):**

```powershell
irm https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 | iex
```

It asks for the server as `<user>@<host>` or as an alias from your PC's SSH
config (`<alias>`). To read the script first, or to pass `-Server` yourself,
save it first (the `irm | iex` form can't pass arguments):

```powershell
Invoke-WebRequest https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 -OutFile flash.ps1
Get-Content .\flash.ps1
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server '<user>@<host>'
```

If copying fails with "Connection closed", see the
[flashing prerequisites](firmware/README.md#flash-from-the-server-build).

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

Both scripts copy your private firmware from the server over SSH and check
its hashes. `--server '<alias>'` works on macOS/Linux too. Each script
asks whether this is a first install.

**First install on a factory board:** answer **yes**, then:

1. When asked, put the board in download mode: hold **BOOT**, plug in USB
   (or tap **RESET**), wait 2–3 seconds, then release BOOT.
2. Select the port (`COMx` on Windows; it can change in download mode).
3. The script backs up the full 8 MB factory flash, reading it twice to
   check the copies match. Keep the backup private and outside the repo: it
   may contain device keys.
4. Type **`ERASE`** to confirm. This wipes all internal flash, including
   settings and any trips stored there, then writes the firmware. The SD
   card is not touched.

**Updates:** answer **no** and select the running board's port. Only the app
is rewritten; settings and trips are kept. The script tries to switch the
board into download mode over USB; if that fails, it shows the BOOT/RESET
steps and asks for the new port if it changed.

When writing finishes, the script resets the board. If it stays in download
mode, press RESET with BOOT released. See the
[firmware guide](firmware/README.md#flash-from-the-server-build) for details.

**After the first install, you can remove the LoRa antenna.** This
project's firmware switches the LoRa radio's power off at startup and checks
that it's off. Put the antenna back before flashing anything else onto the
board, such as the factory backup or Meshtastic.

<a id="4-check-it-works"></a>
<a id="5-put-it-in-the-car"></a>

### Step 3 — First drive and Device health

Fit the FAT32 card with the board switched off. Take a drive of a few minutes
outdoors, with the GPS antenna facing the sky and the battery fitted, and
park within home WiFi range.

The trip ends when you switch the car off (after 10 seconds without USB
power; the battery keeps the board running to upload), or after five minutes
stopped if the USB port stays powered. Allow a little extra time for the
upload. See [How it works](#how-it-works) for when trips start and when short
trips are discarded.

Open `http://<host>:8091/` and sign in with the admin login. Check the trip,
then open **Device health** and look for a newly received **current status
snapshot**. That snapshot is what shows diagnostics enrollment worked; a
listed device or a recent trip alone doesn't. See
[detailed checks](firmware/README.md#check-recording-and-uploads) and
[car installation](firmware/README.md#put-it-in-the-car).

If you made an everyday login, it can annotate, merge and export trips and
see Device health. Removing trips, managing logins and closing a tax year are
admin-only; the full table is in [Viewer Login](server/README.md#viewer-login).

**After the first diagnostics snapshot, do the one-time cleanup flash**,
before you rebuild firmware on the server. It needs the saved script from
Step 2; if you used the one-liner, download the script as shown there first.

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

This flashes the build in `firmware-out/cleanup/`, which leaves out the
one-use enrollment values. Like an update, it writes only the app, keeping
the board's settings, trip counter, diagnostics metadata and queued trips.
It tries download mode automatically and shows the button steps if needed.
[Enrollment and recovery](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery)
explains why those values must be removed.

## Updating

Update the server first, so it understands the new firmware's diagnostics.

1. Rerun **Step 1 on the server**, accept the firmware build, and confirm
   your settings. Your configuration and trips are kept.
2. Rerun **Step 2 on your PC** and answer **no** to first install. Never use
   `-FirstInstall` / `--first-install` on a tracker that's already set up:
   it erases the board's settings and internal trips. A normal update
   writes only the app.

See the [update runbook](DEPLOY_DEBIAN.md#updating).

## Advanced: build from source

To build locally, clone the repository (or use GitHub's **Code → Download ZIP**):

```bash
git clone https://github.com/RedEvoRS/esp32-gps-mileage-tracker.git
cd esp32-gps-mileage-tracker
```

Follow the [Arduino IDE / arduino-cli source route](firmware/README.md#advanced-build-from-source).
It is an ordered checklist from installing the tools to uploading, covering
the hardware-test sketches, libraries and Supreme board settings. It also
covers copying `firmware/secrets.h.example` to
`firmware/04_trip_logger/secrets.h` and stamping each build's version. Back
up the factory flash before the first erase and upload; keep erase disabled
for later uploads.

To install the server by hand instead of with the setup script, see the
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
out and read the files on a PC. Switch the board off first, following the
[card precautions](firmware/README.md#microsd-card), and don't press POWER
again before pulling the card: any press, even a tap, switches it back on.

Once a trip is on the server, its files are never edited. The viewer mounts
the trip folder read-only and keeps your notes in its own SQLite database, so
editing a trip's purpose can't touch the record underneath. Removing a trip
and merging trips are recorded in that same viewer database; the original
`.gpx` and `.json` files stay where they are. If the summary ever gets
lost, it can be rebuilt from the GPX.

Times are stored in UTC, which is what the GPS reports, and shown in your
local timezone everywhere a person reads them, so a 6pm drive shows up as
6pm on the right day instead of 1am the next one. The installer asks for your
timezone and saves it as `DISPLAY_TZ` in the server's `.env`. There's no
default, and both services refuse to start without it.

Trips are dated and counted by their local start time, but filed in the
folder for the local month they end. That only matters for a drive across
midnight at the end of a month: one that starts at 11:50pm on December 31,
2026 shows as December 31 and counts toward 2026, but its files are in
`2027-01/` (see "Years are based on trip start" in
[server/README.md](server/README.md#viewer--export-ui)).

## Security

See [SECURITY.md](SECURITY.md) for private vulnerability reporting, supported versions and the threat model.

Both services are meant for your home network only. Don't expose them to the
internet.

The upload endpoint checks a bearer token and the viewer has a login, but
both use plain HTTP, so anyone who can see the traffic can see the
credentials. The login keeps other people on your network out of your trips;
it isn't a replacement for TLS (Transport Layer Security, the encryption
behind HTTPS). Use a strong viewer password.

For HTTPS, put the viewer behind Caddy or Nginx Proxy Manager (NPM) using
the [private reverse-proxy guide](DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager).
It covers the viewer settings to change, Docker networking, certificates and
Tailscale access without internet exposure. Two gotchas:

- **NPM's defaults let LAN clients fake their address** and dodge the
  login lockout until you apply the guide's fix.
- **A proxy in front of ingest must allow large, slow uploads** (16 MiB
  bodies, 900 s). If it rejects a trip with HTTP 413, the board sets the
  trip aside as `.bad` on the card and never retries it, so it's missing
  from your mileage log. See
  [proxy settings and nginx's 1 MiB default trap](server/README.md#security-notes).

The viewer also protects its forms against CSRF (cross-site request
forgery), where another site tries to submit a form using your login. Five
wrong passwords lock out that IP for 15 minutes. If its login settings are
missing or invalid, the viewer refuses to start rather than run without a
login. Both containers run as a non-root user.

`secrets.h`, `.env`, `viewer.env` and `monitor.env` are in `.gitignore`.
Please keep them there.

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
