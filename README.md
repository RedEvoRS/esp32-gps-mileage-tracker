# GPS Mileage Tracker (without a phone)

Deducting business mileage requires a log: the date, distance, destination,
and business purpose of each trip. Phone apps will keep one, but they depend
on the phone being in the car with location access, most are subscriptions,
and the location history ends up on the vendor's servers.

This project moves the logging onto dedicated hardware. A LilyGO T-Beam
(ESP32 with a u-blox NEO-M8N) runs from a USB port in the car, detects the
start and end of each trip from GPS speed, and writes the track as GPX to
on-board flash. Once it sees the home WiFi network, it uploads whatever
trips it has stored.

The server side is two small Flask services on the LAN. One accepts the
uploads and computes trip distance. The other shows each trip on a map,
records its business purpose, and exports the log as CSV or XLSX. Tracks and
annotations stay on the local network; the only outside request is for
OpenStreetMap tiles when the viewer draws a map.

## Where it's at

Honestly: not finished. The server side works in testing and the firmware
compiles, but neither has done a real drive yet.

- Firmware stages 1 to 3 (toolchain, power chip, GPS) work on the actual
  hardware.
- Stage 4, the real trip logger, compiles but hasn't been road-tested. If
  you build one, put a meter on the GPS power rail and watch the serial
  monitor through a full trip and upload before you trust it.
- The ingest and viewer services pass their test suites locally but aren't
  deployed anywhere yet.

Next up is deploying the server on a Debian box, running `sample.gpx`
through it end to end, then flashing the board and taking it for a drive.

## How it works

```
   ┌──────────────────┐        drive        ┌──────────────────┐
   │  T-Beam in car   │  ── logs GPX to ──▶ │    LittleFS      │
   │  (ESP32 + GPS)   │                     │  (on-board flash)│
   └──────────────────┘                     └────────┬─────────┘
            │                                        │
            │  park at home, home WiFi seen          │
            ▼                                        ▼
   ┌─────────────────────────────────────────────────────────┐
   │  POST /upload  (bearer token, GPX body)   :8090 ingest  │
   │      ├── stores raw  data/YYYY-MM/<id>.gpx              │
   │      └── derives     data/YYYY-MM/<id>.json  (summary)  │
   └─────────────────────────────────────────────────────────┘
                              │  read-only
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │  Viewer / export UI                        :8091 viewer │
   │  trip list · Leaflet map audit · annotate · CSV / XLSX  │
   └─────────────────────────────────────────────────────────┘
```

The board decides on its own when a trip starts and stops. Going faster than
5 mph for 10 seconds starts one; staying under 2 mph for 3 minutes ends it,
which is long enough that a red light doesn't chop one drive into six.
Anything shorter than 30 seconds or about a tenth of a mile gets thrown away.

Once the clock is set, each GPS point is written to flash and the file is
closed right after to limit data loss if power is cut. Points waiting for
the clock are buffered in memory and can be lost on a reset. On the next
boot the firmware attempts to repair incomplete files. Hardware power-cut
testing is still needed; there is no verified one-point loss limit.

The board runs off the car's USB port, with an 18650 battery as backup. When
you turn the car off and the port goes dead, the board closes out the trip
and keeps running on the battery for 10 more minutes. If the car is parked
in range of your home WiFi, that's when it uploads. Then it goes to sleep
until the car starts again. Trips recorded away from home wait on the board
until it's next in range.

The server adds up distance point to point, with a few corrections. Wobble
while you're parked doesn't count (at least one end of a step must be over
2 mph), bad fixes and physically impossible jumps get skipped, and a gap in
coverage like a tunnel or a parking garage gets filled with a straight line.
A straight line is the shortest way between two points, so if the number is
wrong, it's wrong in the low direction, which is the side you want to err on
with the IRS.

## What you need

The hardware:

- **LilyGO TTGO T-Beam V1.2.** ESP32, u-blox NEO-M8N GPS, and an SX1262 LoRa
  radio at 915 MHz. Make sure it's the V1.2 with the AXP2101 power chip.
  Most tutorials out there are for older boards with the AXP192, and the
  code for one doesn't work on the other.
- **An 18650 cell**, preferably a protected one. Treat it as required: it
  keeps the board running for the upload after you park. Without it the
  board shuts off with the car, so it can only upload while the engine is
  running within range of your WiFi.
- **A USB port in the car that turns off with the ignition.** That's how
  the board knows the car is off. See
  [Put it in the car](#5-put-it-in-the-car).
- **A micro-USB cable that carries data**, not a charge-only one, or you
  won't be able to flash it.
- **The GPS antenna**, attached.
- An enclosure. Mine is 3D printed.

The LoRa radio isn't used, but don't power the board without its LoRa
antenna attached either. Running the SX1262 without a load can damage it.

And around it:

- **A server**: a Debian 12/13 or Ubuntu machine on your home network
  that stays on, with SSH access and a login that can use sudo (run
  administrative commands). Everything runs in Docker, which the installer
  sets up. For automatic backups, that account currently needs numeric
  user ID **1000**; run `id -u` on the server to check. The installer skips
  backups for other IDs. See [backup setup](DEPLOY_DEBIAN.md#step-10--backups).
- **Home WiFi on 2.4 GHz that reaches where you park.** The ESP32 has no
  5 GHz radio. A network that uses one name for both bands is fine.
  **Hidden networks won't work**: the board only uploads when it sees
  your network's name in a scan.
- **A computer with the Arduino IDE** (Windows, macOS or Linux) to flash
  the board.

## Setting it up

Go in this order: get the code, set up the server, flash the board, check
that it works, then put it in the car. The server comes first because the
firmware is built with the server's address and upload token, so you need
those before you can flash.

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
reservation in your router, or a static IP. The board is flashed with this
address, so if it ever changes, uploads stop until you reflash.

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
viewer's session key, asks for a username and password for the web page,
and starts both services. Then it checks the whole thing actually works:
it uploads the sample trip, logs in to the viewer, finds the trip on the
list, the map and in both exports, and deletes it again. It also sets up
nightly backups to another machine if you let it. If any step fails it
tells you what to fix, and you just run it again; whatever's already done
stays done.

At the end it prints the viewer's address and three lines for the
firmware's `secrets.h`: `SERVER_HOST`, `SERVER_PORT` and `AUTH_TOKEN`. Keep
them for the next step. The token is also in `~/mileage-tracker/.env` on
the server, as `INGEST_AUTH_TOKEN`.

`bash ~/mileage-tracker/install.sh --check` reruns the checks later
without changing the configuration. It uploads and removes a temporary
test trip and can test the viewer login, so it is not a read-only check.

There are three separate credentials to keep straight:

| Credential | Where you use it |
|---|---|
| Server SSH login | To connect to and administer the Linux server |
| Viewer username and password | To sign in to the trips web page |
| Upload token | In the board's `AUTH_TOKEN` and the server's `INGEST_AUTH_TOKEN`; it is not either login password |

When the installer finishes and you have saved its firmware settings,
leave the SSH session to return to your computer:

```bash
exit
```

If you'd rather do it by hand, [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md) goes
through the same steps one at a time.

### 3. Flash the board

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

**First flash on a new, empty board.** If the Serial Monitor shows
`ERROR: LittleFS mount failed. Storage preserved; halting.`, nothing gets
logged. On a new board this usually means the trip storage hasn't been
set up yet. The firmware never formats it on its own, so
a glitch can't wipe trips that are waiting to upload. Set it up once by
following [First flash on a new board](firmware/README.md#first-flash-on-a-new-board)
in the firmware README: one temporary edit and two flashes. A normal
reflash with the same partition layout and erasing disabled preserves it.
**If the board has already recorded trips, do not format it:** follow
[storage recovery](firmware/README.md#if-the-trip-storage-wont-mount-later).

### 4. Check it works

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
- No upload result after a saved trip: WiFi is one possible cause. Check
  the name and password, and that the network is 2.4 GHz and not hidden;
  the firmware doesn't print scan/join failures yet. Also check earlier
  serial messages for storage errors, incomplete files or files set aside
  as `.bad`, which are not eligible for normal upload.

Once it's in the car, the viewer is the only sign the board is working.
Look at it every so often after you've driven. If trips stop showing up,
check whether any of the `secrets.h` settings have changed, then plug in a
laptop and watch the Serial Monitor.

### 5. Put it in the car

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
  port comes on. A short press of **PWR** wakes it from sleep, and a long
  press puts it to sleep. Unplugged from USB, on your desk say, it runs
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

Times are stored in UTC, which is what the GPS reports, and shown in Pacific
time everywhere a person reads them. That way a 6pm drive shows up as 6pm
on the right day instead of 1am the next one. Trips are also filed by local
month, so a New Year's Eve drive lands in `2026-12/` and not `2027-01/`. To
use a different timezone, change `DISPLAY_TZ` in
[docker-compose.yml](server/docker-compose.yml). Change it on both services,
or the folder a trip lands in and the date the viewer shows can disagree
near the end of a month.

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
