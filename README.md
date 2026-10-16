# GPS Mileage Tracker (without a phone)

Deducting business mileage requires a log: the date, distance, destination,
and business purpose of each trip. Phone apps will keep one, but they depend
on the phone being in the car with location access, most are subscriptions,
and the location history ends up on the vendor's servers.

This project is intended to move mileage logging onto a LilyGO **T-Beam
Supreme with the u-blox MAX-M10S GPS** (choose the MAX-M10S version, not the
L76K version). Its microSD slot will hold a **SanDisk 32 GB Max Endurance**
card for trip storage. Once the firmware port is complete, the board will
detect trips from GPS speed, save tracks as GPX, and upload stored trips over
home WiFi.

The server side is two small Flask services on the LAN. One accepts the
uploads and computes trip distance. The other shows each trip on a map,
records its business purpose, and exports the log as CSV or XLSX. Tracks and
annotations stay on the local network; the only outside request is for
OpenStreetMap tiles when the viewer draws a map.

## Where it's at

The hardware target has changed from the T-Beam V1.2 to the T-Beam Supreme
with MAX-M10S GPS and the SanDisk microSD card above. The checked-in firmware
and its detailed flashing guide still target the retired V1.2 with NEO-M8N
GPS and LittleFS storage. The Supreme firmware port, including MAX-M10S setup
and microSD trip storage, has not landed. **Do not flash the current sketches
to a T-Beam Supreme.** Project progress is tracked in GitHub issues.

## How it works

The planned device health, error-code and notification framework is described in
[DIAGNOSTICS.md](DIAGNOSTICS.md), with implementation tracked by issues #117-#130.
The shared catalog and protocol are available; device/server integration follows
in those issues.

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

This is the intended Supreme data path. The current firmware does not yet
implement the logger for this board.

The trip rules below describe the current V1.2 logger; the Supreme port has
not yet been implemented. The logger decides on its own when a trip starts
and stops. Going faster than 5 mph for 10 seconds starts one; staying under
2 mph for 3 minutes ends it, which is long enough that a red light doesn't
chop one drive into six. Anything shorter than 30 seconds or about a tenth
of a mile gets thrown away.

The V1.2 firmware writes each GPS point to LittleFS on the board's flash and
closes the file right after to limit data loss if power is cut. The target
Supreme firmware is intended to store GPX files on the microSD card; its
write, recovery, and power-loss behavior still needs to be implemented and
validated.

The V1.2 firmware runs from the car's USB port, with an 18650 battery as
backup. When the car's port goes dead, it closes out the trip and keeps
running on battery for 10 more minutes to upload over home WiFi, then sleeps.
The Supreme port's power and sleep behavior has not been established; do not
assume the V1.2 pin, rail, or button instructions apply to it.

The server adds up distance point to point, with a few corrections. Wobble
while you're parked doesn't count (at least one end of a step must be over
2 mph), bad fixes and physically impossible jumps get skipped, and a gap in
coverage like a tunnel or a parking garage gets filled with a straight line.
A straight line is the shortest way between two points, so if the number is
wrong, it's wrong in the low direction, which is the side you want to err on
with the IRS.

## What you need

The target hardware:

- **LilyGO T-Beam Supreme with MAX-M10S GPS.** Select the MAX-M10S version,
  not the L76K version. It uses an ESP32-S3 and has an onboard microSD slot.
  See LilyGO's [T-Beam Supreme hardware guide](https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series/blob/master/docs/en/t_beam_supreme/t_beam_supreme_hw.md).
- **SanDisk 32 GB Max Endurance microSD card.** This is the selected card
  for trip storage on the Supreme; card formatting and firmware setup will
  be documented with the Supreme firmware port.
- **An 18650 cell** for the onboard holder. The Supreme supports this cell;
  runtime, charging, and sleep behavior will be documented after the new
  firmware is validated.
- **A car USB power connection.** The legacy V1.2 firmware expects power
  from an ignition-switched port. The Supreme's vehicle-power requirements
  are part of the pending firmware shakedown.
- **A USB-C data cable**, not a charge-only one, for the Supreme's USB-C
  connection.
- **The GPS antenna**, attached.
- An enclosure. Mine is 3D printed.

The LoRa radio isn't used by this project. Follow LilyGO's hardware guide
for antenna requirements, and connect the correct LoRa antenna before
transmitting.

And around it:

- **A server**: a Debian 12/13 or Ubuntu machine on your home network
  that stays on, with SSH access and a login that can use sudo (run
  administrative commands). Everything runs in Docker, which the installer
  sets up. For automatic backups, that account currently needs numeric
  user ID **1000**; run `id -u` on the server to check. The installer skips
  backups for other IDs. See [backup setup](DEPLOY_DEBIAN.md#step-10--backups).
- **Home WiFi on 2.4 GHz** that reaches where you park. The ESP32-S3 does
  not have 5 GHz WiFi.
- **A computer for firmware development and flashing.** Supreme board
  settings and tool instructions will be published with the firmware port;
  the Arduino IDE steps below are for the V1.2 only.

## Setting it up

Get the code and set up the server first. The server's address and upload
token will be needed by the Supreme firmware once its port is available.
The firmware and vehicle setup steps below are explicitly marked as legacy
V1.2 instructions; they are not a procedure for the new board.

### 1. Get the code

Clone this repository, or on GitHub use **Code → Download ZIP** and unzip
it. On your computer, open PowerShell (Windows) or Terminal (macOS/Linux)
and change to the top folder of that copy:

```bash
cd "<path to your copy of the repo>"
```

Replace text in angle brackets, including the brackets, with your own
values. Keep the quotes around directory paths, especially if they contain
spaces. Commands below say when to switch between this computer and the
server.

### 2. Set up the server

Before you start, give the server a fixed address on your network: a DHCP
reservation in your router, or a static IP. The Supreme firmware will need
this address to upload; if it changes, the firmware configuration will need
to be updated.

**On your computer**, in the repo's top folder, copy `server/` over and
connect to the server. `<user>` is your Linux login on the server;
`<server>` is its IP address or hostname:

```bash
scp -r server <user>@<server>:~/mileage-tracker
ssh <user>@<server>
```

**In that SSH session on the server**, run the installer as your normal
login, not root:

```bash
bash ~/mileage-tracker/install.sh
```

It installs Docker if it isn't there, generates the upload token and the
viewer's session key, asks for your timezone and a username and password
for the web page, and starts both services. Then it checks the whole thing actually works:
it uploads the sample trip, logs in to the viewer, finds the trip on the
list, the map and in both exports, and deletes it again. It also sets up
nightly backups to another machine if you let it. If any step fails it
tells you what to fix, and you just run it again; whatever's already done
stays done.

At the end it prints the viewer's address and three settings for the current
V1.2 firmware's `secrets.h`: `SERVER_HOST`, `SERVER_PORT` and `AUTH_TOKEN`.
Keep the server address and token for the Supreme port instructions; the
V1.2-specific file settings may change. The token is also in
`~/mileage-tracker/.env` on the server, as `INGEST_AUTH_TOKEN`.

`bash ~/mileage-tracker/install.sh --check` reruns the checks later
without changing the configuration. It uploads and removes a temporary
test trip and can test the viewer login, so it is not a read-only check.

There are three separate credentials to keep straight:

| Credential | Where you use it |
|---|---|
| Server SSH login | To connect to and administer the Linux server |
| Viewer username and password | To sign in to the trips web page |
| Upload token | Used by the board firmware and set on the server as `INGEST_AUTH_TOKEN`; it is not either login password |

When the installer finishes and you have saved the server details and token,
leave the SSH session to return to your computer:

```bash
exit
```

If you'd rather do it by hand, [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md) goes
through the same steps one at a time.

### 3. Legacy V1.2 firmware (do not use with the Supreme)

The remaining flashing, check-it-works, and in-car instructions in this
section apply only to the retired T-Beam V1.2 with NEO-M8N GPS. They use
V1.2-specific USB, pin, power, and LittleFS settings. **Do not follow them
for the T-Beam Supreme.** The Supreme MAX-M10S firmware and microSD setup
instructions will be added when that port is ready.

**Back on your computer**, connect the T-Beam with the data-capable USB
cable. A *sketch* is an Arduino program (`.ino`); *uploading* or *flashing*
compiles it and installs it on the board. The *Serial Monitor* is the
Arduino IDE's window for the board's diagnostic messages over USB.

**Set up the Arduino IDE.** [firmware/README.md](firmware/README.md)
covers the USB driver, ESP32 board support and the two libraries. Use the
versions the firmware is tested with: ESP32 core 3.3.11, XPowersLib 0.3.3
and TinyGPSPlus 1.0.3. It needs a 3.x core, and the Boards Manager and
Library Manager install the newest version unless you pick one.

If the board is new to you, work through stages 1 to 3 first. Each one gets
a single piece of hardware working before the next stage depends on it, and
it's a lot easier to debug that way.
Each stage replaces the previous program; they do not stay installed
together. Finish with Stage 4, which is the only stage that records trips.

**Fill in `secrets.h`.** Stage 4, the trip logger, reads your settings
from `secrets.h`, which has to sit next to `04_trip_logger.ino`; the build
doesn't look for it anywhere else. Start from the example:

```bash
cd firmware/04_trip_logger
cp ../secrets.h.example secrets.h
```

(PowerShell accepts `cp`; in Windows `cmd`, use
`copy ..\secrets.h.example secrets.h`.) Then edit it:

| Setting | What goes in it |
|---|---|
| `WIFI_SSID` | Your home network's name, exactly as it appears, capitals included. It has to be 2.4 GHz, and not hidden. |
| `WIFI_PASSWORD` | Its password. If the password has a `"` or `\` in it, write them as `\"` and `\\`. |
| `SERVER_HOST` | Only the server's IP address or hostname, from the installer's output. Do not include `http://`, a port, or `/upload`. |
| `SERVER_PORT` | `8090`. |
| `AUTH_TOKEN` | The token from the installer's output. It has to match `INGEST_AUTH_TOKEN` in the server's `.env` exactly, or every upload is refused. |
| `DEVICE_ID` | A name for this board. The default, `tbeam-01`, is fine. |

Leave `USE_BSSID_FILTER` at 0 unless you want to lock the board to one
router; the comments in `secrets.h` explain it. `secrets.h` is in
`.gitignore`, so it stays out of git.

> **These settings are built into the firmware.** The board only knows
> what was in `secrets.h` when you flashed it. If the server's address
> changes, the token is regenerated, or your WiFi name or password changes
> (a new router, say), the board can't upload any more, and nothing on it
> tells you. Trips just stop showing up in the viewer. Fix `secrets.h` and
> flash again. Trips recorded in the meantime wait on the board and upload
> once it can reach the server, as long as it hasn't filled up (see below).

**Flash it.** Open `firmware/04_trip_logger/04_trip_logger.ino` in the
Arduino IDE. Pick **ESP32 Dev Module**, Flash Size **4MB**, and the
board's port. Keep **Erase All Flash Before Sketch Upload → Disabled**,
then click **Upload** (the right-arrow button) and wait for it to finish.
The sketch folder has its own `partitions.csv`,
which overrides the Partition Scheme menu and sets aside 2.375 MB for
trips.

That's about 5 to 6 hours of driving. **Once it's full, the board stops
recording.** It ends the trip in progress and won't start another until it
gets home and uploads, so any driving after that isn't logged. Keep it in
mind on a long trip away from home, or if uploads have been failing for a
while.

**First flash on a new, empty V1.2 board.** With the bundled partition
layout and erase-all disabled, Stage 4 checks every byte of the trip
partition and formats it only if the whole partition is erased. No sketch
edit or second flash is needed. A board sold as new may still contain
factory test data. If the Serial Monitor shows
`ERROR: LittleFS mount failed. Storage preserved; halting.`, the firmware
has left storage untouched; follow [storage recovery](firmware/README.md#if-the-trip-storage-wont-mount-later)
instead of trying to format it. See [First flash on a new board](firmware/README.md#first-flash-on-a-new-board)
for the startup messages and recovery details. A normal reflash with the
same partition layout and erase-all disabled preserves existing trips.
**If the board has already recorded trips, do not format it.**

### 4. Check it works (legacy V1.2 only)

The board has no screen and the firmware doesn't use any status light, so
open **Tools → Serial Monitor** in Arduino IDE and set its baud rate
(communication speed) to **115200**. The board's charging/GPS lights do
not confirm that a trip was saved or uploaded. If you missed the startup
messages, briefly press **RST** with the monitor open to restart the
program. Do this before the test drive, not during a trip.

1. After flashing you should see `Setup complete. Logging trips.` If it
   says `LittleFS mount failed`, use the first-flash setup only for a new,
   empty board. For a board that has recorded trips, use
   [storage recovery](firmware/README.md#if-the-trip-storage-wont-mount-later).
2. Take it for a drive of a few minutes, powered from a laptop with the
   Serial Monitor open. (Anything under 30 seconds or a tenth of a mile is
   thrown away as noise.) GPS doesn't work indoors, and the first fix
   after flashing can take a minute or so outside. After 10 seconds above
   5 mph you'll see `Trip started`.
3. Park at home and wait. Three minutes below 2 mph ends the trip
   (`Trip finalized`). Within about a minute the board finds your WiFi
   and uploads it: `Upload ... -> HTTP 200 (deleted)`.
4. Open the viewer at `http://<server>:8091/` and log in. The trip should
   be on the list.

If the trip does not appear, first confirm both `Trip started` and
`Trip finalized`. A trip still in progress cannot upload; a short trip
discarded as noise has nothing to upload. Then look for the upload result:

- `HTTP 401`: the token in `secrets.h` doesn't match the server's.
- A negative number, like `HTTP -1`: a connection or transfer failed.
  This can include a dropped connection or timeout after reaching the
  server. Check WiFi signal, `SERVER_HOST`, `SERVER_PORT` and server logs.
- `HTTP 400` or `HTTP 413`: the server rejected the file as invalid or
  too large. Look for `kept as .bad, not retried`. That file remains on
  the board for inspection and uses storage, but no longer uploads
  automatically. Preserve it and investigate the server logs; changing
  WiFi settings will not make it retry. If the rename fails, the log says
  `rename failed, will retry` instead.
- `HTTP 409` or a `5xx` status: the trip stays queued for retry. Check
  server logs for a conflicting trip identity (409) or a server error.
- `Network: home WiFi not found` or `Network: couldn't join home WiFi`:
  the message names the configured SSID; a join failure also reports
  `status N` (`4` can indicate a wrong password). Check the name and
  password, and that the network is 2.4 GHz and not hidden. See the
  [firmware status notes](firmware/README.md#normal-operation) for details.
  After repeated failures the board waits up to 5 minutes between
  tries; the message says how long. Press **RST** to try again at once.
- No upload result and no `Network:` line after a saved trip: check
  earlier serial messages for storage errors, incomplete files or files
  set aside as `.bad`, which are not eligible for normal upload.

Once it's in the car, the viewer is the only sign the board is working.
Look at it every so often after you've driven. If trips stop showing up,
check whether any of the `secrets.h` settings have changed, then plug in a
laptop and watch the Serial Monitor.

### 5. Put it in the car (legacy V1.2 only)

- **Power.** Use a USB port that turns off with the ignition. To check,
  plug in a phone, turn the car off, and see whether it stops charging. A
  port that stays on for a few minutes is fine. On a port that's always
  on, the board never knows the car is off: it doesn't sleep, and it draws
  on the car's battery the whole time it's parked.
- **Battery.** Fit the 18650, following the holder's polarity markings.
  It charges from USB while you drive. The firmware pauses charging when
  the power-management chip reports a high or low temperature. This is an
  indirect estimate, not a measurement of the cell's temperature or a
  guarantee of safe charging in a hot car. Logging carries on while
  charging is paused.
- **Antenna.** The GPS antenna needs a view of the sky. The dashboard or
  the rear shelf works; the glovebox, under a seat or the trunk doesn't.
- **WiFi range.** The board uploads from wherever you park, so your
  2.4 GHz WiFi has to reach that spot. A phone on the same network,
  standing where the car parks, is a quick test.
- **The power button.** The board starts by itself when the car's USB
  port comes on. A short press of **PWR** wakes it from sleep, and holding
  it for a couple of seconds puts it to sleep. Keep holding for 6 seconds
  and the power chip switches the board off completely; a short press
  turns it back on. Unplugged from USB, on your desk say, it runs
  on the battery for 10 minutes and then sleeps until USB power comes
  back. It hasn't died.

**Check the installed power setup.** With the battery fitted and Stage 4
running from the intended car USB port, take another short drive and park
within home WiFi range. Turn the ignition off and confirm the port loses
power after any normal delay. Leave the board connected to the car, not
the laptop: laptop USB power would mask this test. Confirm that the new
trip reaches the viewer during the battery-powered upload window. On the
next start, take another short drive and confirm a second trip appears
after parking. This checks the installed recording/upload path across
shutdown and restart; it does not measure deep-sleep current, which still
needs a meter.

## The data

Trips are stored as GPX, so you can open the raw files in GPXSee,
gpx.studio, Google Earth, QGIS, or pretty much any other map tool. Next to
each one the server writes a small JSON summary so the viewer doesn't have
to re-read every track to list them. Trimmed down, one looks like this:

On the target Supreme, the firmware port is intended to keep the GPX files
on the SanDisk 32 GB Max Endurance microSD card. The current V1.2 firmware
uses LittleFS on internal flash instead; that legacy storage layout and its
capacity limits do not describe the Supreme target.

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

Once a trip is written, nothing changes it. The viewer mounts the trip
folder read-only and keeps your notes in its own SQLite database, so editing
a trip's purpose can't touch the record underneath. If the summary ever gets
lost, it can be rebuilt from the GPX.

Times are stored in UTC, which is what the GPS reports, and shown in your
local timezone everywhere a person reads them. That way a 6pm drive shows
up as 6pm on the right day instead of 1am the next one. Trips are also
filed by local month, so a New Year's Eve drive lands in `2026-12/` and not
`2027-01/`. There's no built-in timezone: the server installer asks for
yours and saves it as `DISPLAY_TZ` in `server/.env`, and both services
refuse to start until it's set. Both read that one setting, so the folder
a trip lands in and the date the viewer shows always agree.

## Security

Both services are meant for your home network only. Don't expose them to the
internet.

The upload endpoint checks a bearer token and the viewer has a login, but
it's all plain HTTP, so anyone who can see the traffic can see the
credentials. The login keeps other people and devices on your network out of
your trips; it isn't a replacement for TLS. If you do put a TLS reverse proxy
in front, set `SESSION_COOKIE_SECURE=True` in `mileage-viewer/app.py`.

Beyond that, the viewer has CSRF protection on its forms and locks out an IP
for 15 minutes after five wrong passwords. It also refuses to start at all
if its login settings are missing or mangled, since a viewer that quietly
runs with no auth is worse than one that won't run. Both containers run as
a non-root user.

`secrets.h`, `.env`, and `viewer.env` are in `.gitignore`. Please keep them
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

The `AUDIT_*` and `CODE_REVIEW_*` files at the top level are past code
reviews, kept for the record.

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
