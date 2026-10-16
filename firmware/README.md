# T-Beam GPS Mileage Tracker — Firmware

This directory contains the firmware for the LilyGO TTGO T-Beam V1.2
with NEO-M8N GPS module. The firmware is organized into progressive
stages that let you verify each hardware subsystem before building
the full mileage tracker.

This is the board half of the project. The [top-level README](../README.md)
gives the whole setup in order (server first, then the board, then the
car); this file has the firmware details it points to.

## Hardware

- **Board:** LilyGO TTGO T-Beam V1.2 (ESP32 + SX1262 LoRa + NEO-M8N GPS)
- **PMU:** AXP2101 (NOT AXP192 — older code examples need updating)
- **GPS:** u-blox NEO-M8N (GPS/GLONASS/BeiDou, IPEX antenna)
- **LoRa:** SX1262 at 915MHz (unused in this project)
- **Battery:** 18650 cell in the onboard holder. Needed in practice: it
  runs the board for the upload after the car turns off (see Stage 4 →
  Normal operation)
- **USB:** Micro-USB via CH9102F serial converter

### Identify the controls and connectors

Use the markings on the board; do not substitute a pinout from a T-Beam
Supreme or an older AXP192 board. LilyGO's
[T-Beam hardware guide](https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series/blob/master/docs/en/t_beam/t_beam_hw.md#button-description)
and [V1.2 schematic](https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series/blob/master/schematic/LilyGo_TBeam_V1.2.pdf)
identify these controls:

| Marking / connection | What it is for |
|---|---|
| **PWR** | Button connected to the power-management chip; Stage 4 uses it for sleep/wake |
| **IO38** | User button on GPIO 38; unused by this firmware, not an ESP32 BOOT button |
| **RST** | Restarts the ESP32 program; useful for seeing startup messages again |
| **GPS antenna** | Small IPEX/u.FL connection for the GPS antenna; needed to receive satellites |
| **LoRa antenna** | Separate radio antenna connection; not the GPS antenna |
| **Micro-USB** | Power and the computer's serial connection for flashing and diagnostics |

The board has charging and GPS indicator lights, but none reports that
this firmware has saved or uploaded a trip.

## Development Environment Setup

### 1. Install the CH9102 USB-Serial Driver

**Windows** needs a driver. Download it from Adafruit's guide:
https://learn.adafruit.com/how-to-install-drivers-for-wch-usb-to-serial-chips-ch9102f-ch9102/windows-driver-installation

After installing, plug in the T-Beam and check Device Manager for
"Ports (COM & LPT)" → "USB-Enhanced-SERIAL CH9102 (COMx)".
Note the COM number — you'll need it in the Arduino IDE.

**macOS:** first check whether a new serial port appears when you plug in
the board. If none appears with a known data cable, install the
[WCH macOS driver linked by LilyGO](https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html)
and follow its installation instructions.

**Linux** normally has the driver built in. If the port appears but the
upload fails with "permission denied", Debian/Ubuntu users can join the
`dialout` group (`sudo usermod -aG dialout $USER`), then log out and back
in. Other distributions may use a different serial-port access group.

### 2. Install Arduino IDE

Download from https://www.arduino.cc/en/software and install the version
for your OS. On Windows, say yes to the USB drivers during install.

### 3. Add ESP32 Board Support

1. Open Arduino IDE
2. File → Preferences (Arduino IDE → Settings on macOS)
3. In "Additional boards manager URLs" paste:
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
4. Click OK
5. Tools → Board → Boards Manager
6. Search "esp32", find "esp32 by Espressif Systems", pick version
   **3.3.11** in its version list, and install (~300MB)

The trip logger is tested on core 3.3.11. It relies on 3.x behaviour, so
don't use a 2.x core. A newer 3.x will probably work but hasn't been
tested. The Boards Manager installs the newest version unless you pick one.

### 4. Configure Board Settings

- Tools → Board → esp32 → ESP32 Dev Module
- Tools → Port → (the board's port from step 1)
- Tools → Upload Speed → 921600
- Tools → Flash Size → 4MB (32Mb)
- Tools → Partition Scheme → Default 4MB with spiffs. Stages 1-3 use
  this; Stage 4 brings its own `partitions.csv` and ignores the menu.
- Tools → Erase All Flash Before Sketch Upload → **Disabled** (the
  default). Enabled, it wipes any trips waiting on the board, and the
  trip storage has to be set up again (Stage 4 → First flash on a new
  board).

### 5. Install Required Libraries

Open Sketch → Include Library → Manage Libraries and install these,
picking the version from each one's version list:

1. **XPowersLib** by lewisxhe, version **0.3.3** — for the AXP2101 power manager
2. **TinyGPSPlus** by Mikal Hart, version **1.0.3** — for NMEA GPS parsing

Those are the versions the trip logger is tested with. The Library Manager
installs the newest unless you pick one.

### 6. Upload a sketch and read its messages

A *sketch* is an Arduino program, stored in an `.ino` file inside a folder
with the same name. *Firmware* is the program installed on the board.
*Uploading* or *flashing* compiles the sketch and writes that firmware to
the board's flash memory. A *partition* is a reserved area of that memory;
Stage 4 stores trips in its LittleFS filesystem partition.

1. Connect the board with a data-capable USB cable and open the stage's
   `.ino` file with **File → Open**. Keep it in its supplied folder.
2. Select **ESP32 Dev Module** and the board's **Tools → Port** entry.
3. Click **Upload**, the right-arrow button (or **Sketch → Upload**).
   The checkmark button only compiles; it does not install the program.
   Wait for the upload to finish before disconnecting anything.
4. Open **Tools → Serial Monitor** and select **115200 baud**, the speed
   used by the board's diagnostic messages. Garbled text usually means
   the monitor is set to a different speed.
5. If the startup messages have already scrolled past or you opened the
   monitor too late, briefly press **RST** to restart the sketch. With
   Stage 4, do this before a test trip, not during recording.

Each upload replaces the previous stage's program. Stages 1–3 are hardware
checks; they do not record trips. **Leave Stage 4 installed for use in the
car.** When updating Stage 4 later, keep its `partitions.csv` and leave
**Erase All Flash Before Sketch Upload → Disabled** to preserve trips.

#### If uploading fails

The T-Beam V1.2 has **PWR, IO38 and RST**, not a GPIO 0 BOOT button.
Holding IO38 while pressing RST does not select the ESP32 bootloader.

1. Close Serial Monitor and any other program using the port. Reconnect
   the USB cable, select the port again, and retry Upload.
2. If there is no port, check the data cable and
   [USB driver](#1-install-the-ch9102-usb-serial-driver). If there is a
   port but transfers fail, try **Tools → Upload Speed →
   115200** and a direct USB connection instead of a hub.
3. If it still stops at `Connecting...`, keep the exact upload error and
   check the board's automatic reset/boot wiring against the V1.2
   schematic above and
   [Espressif's boot-mode troubleshooting](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html).
   Manual bootloader entry uses GPIO 0, not IO38 or PWR; do not follow a
   generic "hold BOOT" sequence or guess which pads to connect.

## Firmware Stages

Work through these in order. Each stage builds on the previous one
and verifies a specific hardware subsystem before moving on.

### Stage 1: Hello World (`01_hello_world/`)

**Verifies:** USB serial communication, compiler, upload process

Open `01_hello_world.ino`, upload it, then open Serial Monitor at
115200 baud. You should see "T-Beam is alive!" and heartbeat messages.

If uploading fails, follow [If uploading fails](#if-uploading-fails).

### Stage 2: Power Manager (`02_pmu_test/`)

**Verifies:** I2C bus, AXP2101 communication, GPS power rail

Open `02_pmu_test.ino` and upload. Serial Monitor should show the
PMU initializing, GPS power being enabled, and battery status.

If "Failed to find AXP2101" appears, check that XPowersLib is
installed and your board is genuinely a V1.2 (check PCB silkscreen).

### Stage 3: GPS Fix (`03_gps_test/`)

**Verifies:** GPS UART, NMEA parsing, satellite acquisition

Open `03_gps_test.ino` and upload. Take the board OUTSIDE — GPS
does not work indoors. Watch Serial Monitor for:
- "Chars from GPS" incrementing (proves UART works)
- Satellite count climbing (proves antenna works)
- Lat/Lon coordinates appearing (proves everything works)

Cold start takes 30-60 seconds outdoors. If "Chars from GPS"
stays at 0, GPS power didn't enable (go back to Stage 2).

### Stage 4: Full Mileage Tracker (`04_trip_logger/`)

**Purpose:** Trip detection, mileage accumulation, crash-safe GPX
logging to LittleFS, and WiFi upload to your home server.

Before flashing:

1. **Create `secrets.h`.** From the repo's top folder, copy
   `firmware/secrets.h.example` to `firmware/04_trip_logger/secrets.h`,
   next to the `.ino` (the build won't find it anywhere else), and fill
   it in. The top-level README has a table of
   the settings under [Flash the board](../README.md#3-flash-the-board).
   The ones that catch people out:
   - The WiFi has to be 2.4 GHz (the ESP32 has no 5 GHz radio), and
     **hidden networks won't work**: the board only uploads when it sees
     your network's name in a scan. `WIFI_SSID` must match exactly,
     capitals included.
   - It's C source, so a `"` or `\` in the WiFi password is written `\"`
     or `\\`.
   - `AUTH_TOKEN` must match `INGEST_AUTH_TOKEN` in the server's `.env`
     exactly. This is the upload token, not the viewer or SSH password.
   - `SERVER_HOST` contains only an IP address or hostname. The firmware
     supplies `http://`, the port and `/upload` itself.
   - **These settings are built into the firmware.** If the server's
     address, the token, or your WiFi name or password changes, the board
     can't upload until you edit `secrets.h` and flash again. Nothing on
     the board shows it: trips just stop appearing in the viewer and wait
     on the board.
2. **Partitions are handled for you.** The sketch folder includes a
   `partitions.csv` (no OTA: 1.5 MB app, 2.375 MB of trip storage), and
   the ESP32 core uses it in place of whatever Tools -> Partition Scheme
   says. At ~118 bytes per second of driving that's roughly 5-6 hours
   between uploads. **Once it's full, the board stops recording**: it
   ends the trip in progress and won't start another until it uploads.
3. **On a new board, do the first flash below** before anything else.

#### First flash on a new board

On a new, empty board the trip partition may not be formatted yet. The
firmware never formats it on its own. That's deliberate: on a board in
use, a mount error mustn't be able to wipe trips
that are waiting to upload. If the first run cannot mount it, the Serial
Monitor shows

```
ERROR: LittleFS mount failed. Storage preserved; halting. See firmware README for first-time provisioning/recovery.
```

and it stops there. (The battery's charge-temperature guard keeps running
while it's halted.) **Use these steps only for a new, empty board.** If it
has recorded trips, follow [storage recovery](#if-the-trip-storage-wont-mount-later)
instead. If it already reaches `Setup complete. Logging trips.`, no
formatting is needed. Otherwise set the partition up once:

1. In `04_trip_logger.ino`, find `mountTripStorage()` and change
   `LittleFS.begin(false)` to `LittleFS.begin(true)`. That lets it format
   the partition when it can't mount it.
2. Flash, and watch the Serial Monitor until it says
   `Setup complete. Logging trips.`
3. Change it back to `LittleFS.begin(false)` and flash again. **Don't skip
   this.** The `true` build formats the partition, erasing any trips
   waiting on it, whenever a mount fails, so it must never go in the car.
   Don't commit it either.

After that, the trip storage survives reflashing. You only do this again
if you change the partition layout or erase the whole flash, and only once
every trip on the board has been uploaded or independently backed up.

#### If the trip storage won't mount later

On a board that's been working, `LittleFS mount failed` doesn't mean the
trips are gone, so don't reach for the first-flash steps. Check that you
flashed with this sketch's `partitions.csv`, and try the firmware that last
worked. Before anything that could erase the partition, copy it to a file
with esptool (it comes with the ESP32 core, or `pip install esptool`):

```
esptool --port <port> read-flash 0x190000 0x260000 trips-backup.bin
```

Close Serial Monitor before running esptool. Replace `<port>` with the
board's port, such as `COM5` or `/dev/ttyACM0`. The offset and size above
come from this project's `spiffs` line in `partitions.csv`; if a different
layout was previously installed, use that layout's offset and size instead.
Keep the backup outside the repo. Older esptool versions spell the command
`read_flash`. A backup image preserves the bytes but does not prove they
are recoverable; keep it before attempting repairs or formatting.

#### Normal operation

How it behaves once flashed:

- **Trip detection:** logging starts after you sustain >5 mph for
  10 s and ends after you're below 2 mph for 3 minutes (stop-and-go
  traffic won't split a drive). Trips shorter than ~30 s (not counting
  the 3 minutes parked at the end) or 0.1 mi are discarded as noise.
- **Power-cut recovery:** once the clock is set, each GPS point is written
  to a `.gpx` file and the file is closed immediately to limit data loss.
  Points waiting for the clock are still in memory and can be lost on a
  reset. On boot the firmware attempts to repair incomplete files, keeping
  ambiguous or damaged files for inspection. Hardware power-cut testing
  is still needed; there is no verified one-point loss limit.
- **Time:** comes from the GPS (UTC). Points captured before the first
  fix are buffered and back-dated once UTC arrives. A trip that ends
  before the clock was ever set (by GPS or NTP) writes the points still
  in its 90-point buffer without times rather than guessing them; earlier
  points may already have been dropped. The viewer lists it without a date.
- **Upload:** the board checks for finished trips every 30 s and scans
  for your home WiFi when a retry is due. Failed scans, connections, or
  uploads double the retry wait (60 s, 120 s, 240 s, then at most 5 min).
  The wait starts after the attempt finishes; the next 30 s check runs it
  once due. A successful upload resets the wait to 30 s, and an empty
  queue clears it so the next trip can try promptly. When connected, it
  POSTs each trip to the ingest server as GPX and deletes it locally once
  the server accepts it. GPS
  parsing runs on core 1; networking runs on core 0 and never blocks
  it.
- **Power loss:** when the car turns off (USB power off for 10 s), the
  current trip is finalized cleanly and kept for the next upload. The
  board stays awake on battery for 10 minutes, so a trip that ends at
  home uploads right away, then deep-sleeps until the car turns on. An
  upload still running at 10 minutes gets up to 6 more to finish; one
  cut off then stays queued and is sent next time. Without the 18650,
  the board loses power with the car, so it can only upload while the
  car is on. On a USB port that stays powered with the car off, it never
  sees the car turn off and never sleeps.
- **Buttons:** a long press of PWR finalizes any trip and puts the board
  into deep sleep; a short press, or USB power coming back, wakes it.
  Holding PWR for 6 seconds is the power chip's own power-off (LilyGO's
  default), which cuts power completely; a short press turns it back on.
- **Charging:** the firmware pauses charging at high or low temperatures
  reported by the AXP2101 power-management chip. Its temperature is an
  indirect estimate, not a measurement of the battery cell or a guarantee
  of safe charging in a hot car. Logging continues while charging is paused.
- **Storage full:** below 64 KB free it won't start a trip, and below
  24 KB it ends the one in progress. An upload frees the space.
- **No display:** the firmware doesn't use a screen or a status LED. The
  Serial Monitor shows what it's doing; once it's in the car, trips
  appearing in the viewer are how you know it works.

Watch the Serial Monitor (115200) for state transitions:
`Trip started` → (drive) → `Trip finalized`, then
`Upload ... -> HTTP 200 (deleted)` when you get home.

If a trip does not appear, first confirm `Trip started` and `Trip finalized`:
an active or discarded trip cannot upload. `HTTP 401` means the upload
token does not match. A negative result means a connection or transfer
failure, including timeouts after reaching the server. `HTTP 400/413`
normally sets the file aside as `.bad` without further retries, while
`409` and `5xx` leave it queued. No upload result can mean WiFi trouble,
but also an incomplete file or one set aside during repair. The firmware
does not log WiFi scan/join failures yet. See the
[upload troubleshooting steps](../README.md#4-check-it-works) for what to
check and preserve in each case.

Boot repair also examines `.gpx.fix` files left by interrupted repairs.
It reclaims a temporary copy only when its bytes match the original or its
expected repaired contents. A finished repair can replace its original
without allocating another copy. Orphans with the logger's GPX header and
at least one complete trackpoint are renamed to `.gpx` and repaired normally.
Unknown contents, unequal copies, and names that collide with a quarantined
`.gpx.bad` file are retained and reported in the Serial Monitor for manual
inspection; they may continue to consume storage. Failed storage operations
leave recoverable copies available for the next boot. Hardware power-cut
testing remains necessary to verify the flash driver's behavior.

> Note: this sketch **compiles cleanly on ESP32 core 3.3.11** with
> XPowersLib 0.3.3 and TinyGPSPlus 1.0.3 (~1.03 MB of the 1.5 MB app
> partition). Host-side tests: `bash firmware/host_test/run.sh`. It still needs an on-hardware shakedown — verify the GPS
> rail with a meter and watch the Serial Monitor through a real trip and
> upload before relying on it in the car.

### Network stack shakedown (issue #6)

This section is for validating the firmware on hardware. You don't need it
to build or use the tracker.

The network task reserves 12,288 bytes, matching the Stage 4 brief. This is
a conservative starting point; it has **not** been sized from board measurements.
Serial diagnostics report the task's lifetime minimum free stack at startup,
after each HTTP upload (including failures), and after each network cycle.
ESP-IDF reports this watermark in **bytes**; do not multiply it by four.
See [Espressif's stack measurement guidance](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/performance/ram-usage.html).

For the hardware shakedown, capture the serial log while uploading a long
real trip with GPS logging active, then repeat with multiple queued files,
an unavailable server, rejected requests, and a dropped WiFi connection.
Record the firmware commit, core/library versions, file sizes, HTTP outcomes,
upload times, and lowest `Net stack ... minimum free` value. Each `Upload`
line prints the file's size and how long it took. Size over time is the
link speed, and the server's worker timeout (300 s) sets the slowest link
the largest trip (~2.4 MB) can upload over, about 8 kB/s. A warning at
1,024 bytes or less means the allocation needs review; absence of a warning
is not proof that every path has been exercised. Keep issue #6 open until these measurements are
recorded and the final allocation is justified. Host tests check diagnostics
only and cannot measure ESP32 stack use.

## Pin Reference (T-Beam V1.2)

| Function        | GPIO Pin | Notes                          |
|-----------------|----------|--------------------------------|
| I2C SDA (PMU)   | 21       | AXP2101 power manager          |
| I2C SCL (PMU)   | 22       | AXP2101 power manager          |
| GPS RX (from GPS)| 34      | GPS module TX → ESP32 RX       |
| GPS TX (to GPS) | 12       | ESP32 TX → GPS module RX       |
| GPS Baud Rate   | —        | 9600 (default for NEO-M8N)     |
| LED             | 4        | Onboard LED (some boards: 14/25)|
| LoRa MOSI       | 27       | SX1262 (unused in this project)|
| LoRa SCK        | 5        | SX1262 (unused in this project)|
| LoRa CS         | 18       | SX1262 (unused in this project)|
| User button (IO38) | 38    | Unused; not a BOOT button      |
| PWR button      | —        | Connected to the AXP2101 PMU  |
| RST button      | —        | Resets the ESP32              |

## Important Notes

- The T-Beam V1.2 uses **AXP2101**, not AXP192. Many online
  tutorials and code examples reference AXP192 — they won't work
  without modification. Use XPowersLib which supports both.

- The GPS antenna (IPEX connector) must be attached for satellite
  reception. Without it, the GPS module will never get a fix.

- A **data-capable** micro-USB cable is required for programming.
  Charge-only cables will power the board but won't appear in
  Device Manager. Test with your phone if unsure.

- Use a **protected 18650 cell** for the battery. It keeps the board
  running for 10 minutes after the car turns off, which is when trips
  upload at home, then lets it sleep until the car starts again.
