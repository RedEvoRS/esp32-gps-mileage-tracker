# GPS Mileage Tracker (without a phone)

Deducting business mileage requires a log: the date, distance, destination,
and business purpose of each trip. Phone apps will keep one, but they depend
on the phone being in the car with location access, most are subscriptions,
and the location history ends up on the vendor's servers.

This project moves mileage logging onto a small GPS board that lives in the
car: a LilyGO **T-Beam Supreme with the u-blox MAX-M10S GPS** (choose the
MAX-M10S version, not the L76K version). It detects trips from GPS speed,
saves each track as a GPX file on a **SanDisk 32 GB Max Endurance** microSD
card, and uploads the stored trips over your home WiFi when you park.

The server side is two small Flask services on the LAN. One accepts the
uploads and computes trip distance. The other shows each trip on a map,
records its business purpose, and exports the log as CSV or XLSX. Tracks and
annotations stay on the local network; the only outside request is for
OpenStreetMap tiles when the viewer draws a map.

## Where it's at

The firmware runs on the T-Beam Supreme. The bring-up sketches and the trip
logger were checked on a real board in October 2026: the logger switches
the board off when the car is parked and back on when the car starts, and
it has recorded real drives on the microSD card, which were read back on a
PC. If no card mounts, trips go to the board's internal flash instead,
which holds about 6 hours of driving. The server has been deployed with its
installer on a real Debian machine.
<!-- TODO(first upload): replace the next sentence once the first upload
from the Supreme to the deployed server succeeds. -->
Uploading trips from the board to that server hasn't been tested end to
end yet.

Still to check on the road: the everyday (production) screen, the board's
current draw while switched off, and a few car-power cases. Project progress
is tracked in GitHub issues.

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
flash instead, and uploads them from there.

Alongside the trips, the board keeps a small journal of its own health:
numbered error codes such as a lost GPS antenna, a full card or a refused
upload. It uploads the journal after the trips, and the viewer's **Device
health** page shows it. The server can optionally send those faults to your
phone through a self-hosted ntfy service. [DIAGNOSTICS.md](DIAGNOSTICS.md)
describes the whole framework and lists every code.

The logger decides on its own when a trip starts and stops. Going faster than 5 mph for 10 seconds starts one; staying under
2 mph for 3 minutes ends it, which is long enough that a red light doesn't
chop one drive into six. Anything shorter than 30 seconds or about a tenth
of a mile gets thrown away.

The firmware writes each GPS point to the microSD card (or the internal
flash fallback) and closes the file right after to limit data loss if
power is cut. The card's behavior when power is cut still needs testing on
the bench.

The firmware runs from the car's USB port, with an 18650 battery as backup.
When the car's port goes dead, it closes out the trip and keeps running on
battery for 10 more minutes to upload over home WiFi. Then it switches
itself off completely, and power coming back to the USB port switches it on
again when you next start the car.

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
- **A microSD card of 32 GB or less, formatted FAT32.** This project uses
  a SanDisk 32 GB Max Endurance, which is built for constant writing and
  comes formatted FAT32. The firmware can't read exFAT, which most cards of
  64 GB and up ship with. See [microSD card](firmware/README.md#microsd-card)
  for checking the format and spotting fake cards.
- **A protected 18650 cell** for the onboard holder. It keeps the board
  running after the car switches off, which is when trips upload at home.
  Without it the board can only upload while the engine is running.
- **A car USB port that switches off with the ignition.** The board
  learns that the car is off when that port loses power. On a port that
  stays powered, it never switches itself off and keeps drawing from the
  car's battery the whole time the car is parked.
- **A USB-C data cable**, not a charge-only one, for flashing and for
  reading the board's messages on a computer.
- **The GPS antenna**, attached to the socket marked **GPS**. The board
  powers an active antenna, so an external active antenna works too.
- **The 915 MHz LoRa antenna** (see the warning below).
<!-- TODO(enclosure): link the owner's published remix here, and credit
the original Supreme case it's based on (name, author, link, licence). If
the original is CC BY-SA, the remix must carry the same licence. -->
- An enclosure. Mine is a heavily modified remix of a published T-Beam
  Supreme case, printed in PETG, which copes with a hot car better than
  PLA. A case made for the older T-Beam V1.2 doesn't fit the Supreme.

The LoRa radio isn't used by this project, but the Supreme may arrive with
factory firmware that transmits. Attach the correct 915 MHz LoRa antenna
**before connecting USB or inserting the 18650 battery**, and leave it
attached while the board is powered.

And around it:

- **A server**: a Debian 12/13 or Ubuntu machine on your home network
  that stays on, with SSH access and a login that can use sudo (run
  administrative commands). Everything runs in Docker, which the installer
  sets up. For automatic backups, that account currently needs numeric
  user ID **1000**; run `id -u` on the server to check. The installer skips
  backups for other IDs. See [backup setup](DEPLOY_DEBIAN.md#step-10--backups).
- **Home WiFi on 2.4 GHz** that reaches where you park. The ESP32-S3 does
  not have 5 GHz WiFi.
- **A computer with the Arduino IDE** (Windows, macOS or Linux) to build
  and flash the firmware. Step 3 below says which board support and
  library versions to install.

## Setting it up

Get the code and set up the server first: the firmware needs the server's
address and upload token, and they're built into it when you flash.

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
reservation in your router, or a static IP. The firmware has this address
built in; if it changes, the board can't upload until you flash it again
with the new one.

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

At the end it prints the viewer's address and three lines for the
firmware's settings file, `secrets.h`: `SERVER_HOST`, `SERVER_PORT` and
`AUTH_TOKEN`. Copy them somewhere for step 3. The token is also in
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

### 3. Flash the firmware

**Back on your computer.** A few words first: a *sketch* is an Arduino
program (an `.ino` file in a folder of the same name); *uploading* or
*flashing* compiles it and installs it on the board. The *Serial Monitor* is
the Arduino IDE's window for the board's diagnostic messages over USB.

**Before the board's first power-up, screw on the 915 MHz LoRa antenna and
press the GPS antenna onto the socket marked GPS** (see the warning under
[What you need](#what-you-need)). Fit the 18650 with its + end matching the
holder's marking.

**Set up the Arduino IDE.** [firmware/README.md](firmware/README.md#development-environment-setup)
walks through it: the ESP32 board support and three libraries, and the
board settings for the Supreme. Use the versions the firmware is tested
with: ESP32 core **3.3.11**, XPowersLib **0.3.3**, TinyGPSPlus **1.0.3** and
U8g2 **2.36.18**. The Boards Manager and Library Manager install the newest
version unless you pick one from the list. The Supreme uses the ESP32-S3's
built-in USB, so no USB driver is needed.

**Back up the factory firmware first.** The board ships with factory
firmware (Meshtastic), and the first upload replaces it for good. If you
might ever want it back, read it off with esptool before uploading
anything: [back up the factory flash](firmware/README.md#back-up-the-factory-flash-first).
That section also covers *download mode*, which you may have to enter by
hand while the factory firmware is still on the board.

**Work through the test stages.** If the board is new to you, flash stages
1 to 3 first. Each gets one piece of hardware working (USB and memory, then
the power chip and microSD slot, then the GPS) before the next depends on
it, and it's a lot easier to debug that way:
[Firmware stages](firmware/README.md#firmware-stages). Each stage replaces
the previous program. Only Stage 4, the trip logger, records trips, and
it's the one you leave on the board.

**Fill in `secrets.h`.** Stage 4 reads your settings from `secrets.h`,
which has to sit next to `04_trip_logger.ino`; the build doesn't look for
it anywhere else. Start from the example:

```bash
cd firmware/04_trip_logger
cp ../secrets.h.example secrets.h
```

(PowerShell accepts `cp`; in Windows `cmd`, use
`copy ..\secrets.h.example secrets.h`.) Then edit it:

| Setting | What goes in it |
|---|---|
| `WIFI_SSID` | Your home network's name, exactly as it appears, capitals included. It has to be 2.4 GHz, and not hidden: the board only uploads when it sees this name in a WiFi scan. |
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
> (a new router, say), the board can't upload any more. Trips just stop
> showing up in the viewer. Fix `secrets.h` and flash again. Trips recorded
> in the meantime wait on the board and upload once it can reach the
> server.

**Turn on diagnostics uploads (optional, once).** Trips upload without
this step. The board's health journal (see [How it works](#how-it-works))
only uploads after a one-time *enrollment*, which tells the board where the
server's count of its status reports stands, so the two can never disagree.
The firmware can't ask the server for it, so you read it yourself.
**On the server**, in an SSH session, run this with your `DEVICE_ID` in
place of `<DEVICE_ID>`:

```bash
cd ~/mileage-tracker
docker compose exec -T mileage-viewer python -c "import os,sqlite3,sys;p='/data/diagnostics/events.db';r=sqlite3.connect('file:'+p+'?mode=ro',uri=True).execute('SELECT status_sequence FROM devices WHERE device_id=?',(sys.argv[1],)).fetchone() if os.path.exists(p) else None;print((r and r[0]) or 0)" <DEVICE_ID>
```

It only reads: it runs in the viewer container, which sees the data
read-only. It prints a number, `0` on a new server. In `secrets.h`, remove
the `//` from the two `DIAGNOSTICS_` lines and set them:

```c
#define DIAGNOSTICS_ENROLLMENT_ID 1ULL
#define DIAGNOSTICS_STATUS_HIGH_WATER <the number it printed>ULL
```

The board uses an enrollment ID once and remembers it, so after the first
diagnostics upload has succeeded (the viewer's **Device health** page lists
the board), **delete those two lines again** and flash once more. Left in,
they'd enroll the board again from a stale number if its memory were ever
erased (see *Erase All Flash* below). To enroll again later, after a new
card for example, read the number again and use a higher ID, `2ULL` and
up. [DIAGNOSTICS.md](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery)
has the details.

**Fit the microSD card** with the board switched off. It has to be FAT32;
the firmware never formats it. The firmware chooses where to store trips
once, at startup, so a card inserted while it runs isn't used until the
next start.

**Pick the screen.** Near the top of `04_trip_logger.ino`,
`#define DISPLAY_DEBUG 1` gives a status page that's always on and redraws
every second: satellites, speed, the trip so far, storage and WiFi. It's
for testing, so use it for your first drives. `0` gives the everyday
screen: blank, except for a brief card with the satellite count and `OK`
or error codes such as `E3009` after you park, or when you short-press
**POWER**. [firmware/README.md](firmware/README.md#the-screen) says what
each shows. The everyday screen hasn't been road-tested yet.

**Flash it.** Open `firmware/04_trip_logger/04_trip_logger.ino`, check the
[Supreme board settings](firmware/README.md#board-settings) and the board's
port under **Tools → Port**, then click **Upload** (the right-arrow button)
and wait for it to finish. The sketch folder's own `partitions.csv` sets
aside 2.375 MB of the board's internal flash for trips; it overrides the
Partition Scheme menu, so leave that menu alone.

> **The first Stage 4 upload only:** set **Tools → Erase All Flash Before
> Sketch Upload → Enabled**, upload, then set it straight back to
> **Disabled**. The internal trip storage is only set up on fully erased
> flash, and the factory firmware leaves data there. Erasing also wipes the
> board's settings memory (NVS): the trip counter, the boot counter and the
> used enrollment ID. That's fine on a new board. **Never erase again** on
> a board in use: trips waiting in internal flash would be lost, and the
> `DIAGNOSTICS_` lines, if still there, would enroll again.

Every later upload, with erase disabled, keeps the trips waiting on the
card and in internal flash, and the board's settings memory. See
[First flash on a new board](firmware/README.md#first-flash-on-a-new-board)
for the messages to expect and what to do if storage won't mount.

**How much it holds.** A 32 GB card holds far more driving than you'll do
between uploads. Without a card the board uses internal flash, which holds
about 5 to 6 hours of driving. **Once that's full, the board stops
recording**: it ends the trip in progress and won't start another until it
gets home and uploads.

### 4. Check it works

Open **Tools → Serial Monitor** in the Arduino IDE and set its speed to
**115200** baud. With the board in the case (screen on the left, USB-C on
the right), **RESET** is the left of the close pair of buttons along the
bottom edge; press it to see the startup messages again. Opening the
Serial Monitor can itself restart the board, so messages may show twice.
Do this before a test drive, not during a trip.

1. After flashing, look for these lines:
   - `Trip storage: microSD (29.72 GiB free); internal flash queue mounted.`
     Trips go to the card. If it says `WARNING: microSD unavailable -- using
     internal flash`, the card didn't mount: switch off, check it's FAT32 and
     seated, and switch on again.
   - `Setup complete. Logging trips.`
   - A line starting `DEGRADED:` means the board is **not recording trips**,
     because neither storage worked or the power chip didn't answer. It
     retries by itself; the message says why. See
     [Stage 4 on the Supreme](firmware/README.md#stage-4-the-trip-logger).
2. Take it for a drive of a few minutes, powered from a laptop with the
   Serial Monitor open. (Anything under 30 seconds or a tenth of a mile is
   thrown away as noise.) GPS doesn't work indoors, and the first fix after
   the battery was out can take a minute outside. After 10 seconds above
   5 mph you'll see `Trip started`.
3. Park at home and wait. Three minutes below 2 mph ends the trip
   (`Trip finalized`). Within about a minute the board finds your WiFi and
   uploads it: `Upload ... -> HTTP 200 (deleted)`. The file is deleted from
   the board only after the server has accepted it.
   <!-- TODO(first upload): confirm this step against the first real
   Supreme upload, including the timing. -->
4. Open the viewer at `http://<server>:8091/` and log in. The trip should
   be on the list. If you enrolled diagnostics, **Device health** shows
   when the board last checked in and any active error codes.

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
  tries; the message says how long. Press **RESET** to try again at once.
- No upload result and no `Network:` line after a saved trip: check
  earlier serial messages for storage errors, incomplete files or files
  set aside as `.bad`, which are not eligible for normal upload.

Once it's in the car, the viewer (and the screen's `OK` or error codes
after you park) are the signs the board is working. Look at the viewer
every so often after you've driven. If trips stop showing up, check
whether any of the `secrets.h` settings have changed, then plug in a
laptop and watch the Serial Monitor.

### 5. Put it in the car

- **Power.** Use a USB port that turns off with the ignition. To check,
  plug in a phone, turn the car off, and see whether it stops charging. A
  port that stays on for a few minutes is fine. On a port that's always
  on, the board never knows the car is off: it never switches itself off,
  and it draws on the car's battery the whole time it's parked.
- **Battery.** Keep the 18650 fitted. It charges from USB while you drive,
  and it's what lets the board close its files, upload at home and switch
  off cleanly once the car's USB power goes. The firmware pauses charging
  when the power-management chip reports a high or low temperature. That's
  an indirect estimate, not a measurement of the cell's temperature or a
  guarantee of safe charging in a hot car, and its calibration was measured
  on the older T-Beam V1.2; re-measuring it on the Supreme is still to do.
  Logging carries on while charging is paused.
- **Antenna.** The GPS antenna needs a view of the sky. The dashboard or
  the rear shelf works; the glovebox, under a seat or the trunk doesn't.
- **WiFi range.** The board uploads from wherever you park, so your
  2.4 GHz WiFi has to reach that spot. A phone on the same network,
  standing where the car parks, is a quick test.
- **Switching off and on.** The board switches on by itself when the car's
  USB port comes on. Once that power has been gone for 10 seconds, it ends
  the trip, stays on battery for 10 minutes to upload, then switches itself
  off completely. Unplugged from USB, on your desk say, it does the same:
  10 minutes, then off. It hasn't died; plug USB back in, or press
  **POWER** for about a second, to switch it on. On battery, holding
  **POWER** for a second or two (let go well before 6 s) switches it off
  cleanly. **With USB plugged in, that press is ignored**, because USB
  power only switches the board on at the moment it arrives: a board
  switched off with USB still in would stay off until the car's port next
  went off and on. Holding **POWER** for 6 s cuts power without closing
  anything first; use it only if the board stops responding.
  [firmware/README.md](firmware/README.md#stage-4-the-trip-logger) has the
  details.

**Check the installed power setup.** With the battery fitted and Stage 4
running from the intended car USB port, take another short drive and park
within home WiFi range. Turn the ignition off and confirm the port loses
power after any normal delay. Leave the board connected to the car, not
the laptop: laptop USB power would mask this test. Confirm that the new
trip reaches the viewer during the battery-powered upload window. On the
next start, take another short drive and confirm a second trip appears
after parking. This checks the installed recording and upload path across
power-off and restart. It doesn't measure how much current the board draws
while switched off, which still needs a meter.

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

Once a trip is on the server, nothing changes it. The viewer mounts the trip
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
