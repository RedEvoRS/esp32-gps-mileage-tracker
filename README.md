# GPS Mileage Tracker (without a phone)

If you deduct business driving, the IRS wants a log: the date, how far you
went, where, and why. There are plenty of apps that will keep one for you,
but they need your phone in the car, usually want a subscription, and keep a
copy of everywhere you've driven on somebody else's server.

This is the version I wanted instead. It's a small ESP32 board with a GPS
receiver that sits in the car, powered from a USB port. When the car starts
moving it starts recording, and when I get home it finds the WiFi and sends
the trip to a little server on my network. Whenever I get around to it, I
open a web page, check each trip on a map, type in what it was for, and
export a spreadsheet at tax time. The location data never leaves the house.

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

Each GPS point is written to flash and the file is closed right after, so if
the power gets yanked you lose one point at most. A file left half-written by
a power cut gets repaired on the next boot.

The server adds up distance point to point, with a few corrections. Wobble
while you're parked doesn't count (both ends of a step have to be over
2 mph), bad fixes and physically impossible jumps get skipped, and a gap in
coverage like a tunnel or a parking garage gets filled with a straight line.
A straight line is the shortest way between two points, so if the number is
wrong, it's wrong in the low direction, which is the side you want to err on
with the IRS.

## Hardware

- **LilyGO TTGO T-Beam V1.2.** ESP32, u-blox NEO-M8N GPS, and an SX1262 LoRa
  radio at 915 MHz. Make sure it's the V1.2 with the AXP2101 power chip.
  Most tutorials out there are for older boards with the AXP192, and the
  code for one doesn't work on the other.
- **An 18650 cell**, preferably a protected one. It covers the gap between
  the car shutting off and the board finishing its last write.
- **A micro-USB cable that carries data**, not a charge-only one, or you
  won't be able to flash it.
- **The GPS antenna**, attached.
- An enclosure. Mine is 3D printed.

The LoRa radio isn't used, but don't power the board without its LoRa
antenna attached either. Running the SX1262 without a load can damage it.

## Setting it up

Do the server first so the board has somewhere to upload to.

### Server

You need a Debian (or Ubuntu) box on your home network and a login on it
with sudo. Copy the `server/` folder over and run the installer as
yourself, not root:

```bash
scp -r server <user>@<server>:~/mileage-tracker
ssh <user>@<server>
bash ~/mileage-tracker/install.sh
```

It installs Docker if it isn't there, generates the upload token and the
viewer's session key, asks for a username and password for the web page,
and starts both services. Then it checks the whole thing actually works:
it uploads the sample trip, logs in to the viewer, finds the trip on the
list, the map and in both exports, and deletes it again. It also sets up
nightly backups to another machine if you let it. If any step fails it
tells you what to fix, and you just run it again; whatever's already done
stays done. At the end it prints the server lines for the firmware's
`secrets.h`.

`bash ~/mileage-tracker/install.sh --check` reruns the checks later
without changing anything, which is handy after a reboot.

If you'd rather do it by hand, [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md) goes
through the same steps one at a time.

### Firmware

[firmware/README.md](firmware/README.md) covers drivers, board settings, and
libraries. I build with the Arduino IDE on ESP32 core 3.3.11, XPowersLib
0.3.3, and TinyGPSPlus 1.0.3.

```bash
cd firmware/04_trip_logger
cp ../secrets.h.example secrets.h
```

Put your WiFi details and server address in `secrets.h`. `AUTH_TOKEN` has to
match `INGEST_AUTH_TOKEN` in the server's `.env` exactly.

Pick ESP32 Dev Module with 4 MB flash. Stage 4 ships its own
`partitions.csv`, which overrides the Partition Scheme menu and sets aside
2.375 MB for trips. That's somewhere around 5 to 6 hours of driving before
it has to get home and upload.

If the board is new to you, work through stages 1 to 3 first. Each one gets
a single piece of hardware working before the next stage depends on it, and
it's a lot easier to debug that way.

## The data

Trips are stored as GPX, so you can open the raw files in GPXSee,
gpx.studio, Google Earth, QGIS, or pretty much any other map tool. Next to
each one the server writes a small JSON summary so the viewer doesn't have
to re-read every track to list them. Trimmed down, one looks like this:

```json
{
  "trip_id": "2026-04-14T13-45-22",
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
