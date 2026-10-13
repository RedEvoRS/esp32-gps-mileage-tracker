# T-Beam GPS Mileage Tracker — Firmware

This directory contains the firmware for the LilyGO TTGO T-Beam V1.2
with NEO-M8N GPS module. The firmware is organized into progressive
stages that let you verify each hardware subsystem before building
the full mileage tracker.

## Hardware

- **Board:** LilyGO TTGO T-Beam V1.2 (ESP32 + SX1262 LoRa + NEO-M8N GPS)
- **PMU:** AXP2101 (NOT AXP192 — older code examples need updating)
- **GPS:** u-blox NEO-M8N (GPS/GLONASS/BeiDou, IPEX antenna)
- **LoRa:** SX1262 at 915MHz (unused in this project)
- **Battery:** 18650 cell in onboard holder (recommended for clean shutdown)
- **USB:** Micro-USB via CH9102F serial converter

## Development Environment Setup

### 1. Install the CH9102 USB-Serial Driver (Windows)

Download from Adafruit's guide:
https://learn.adafruit.com/how-to-install-drivers-for-wch-usb-to-serial-chips-ch9102f-ch9102/windows-driver-installation

After installing, plug in the T-Beam and check Device Manager for
"Ports (COM & LPT)" → "USB-Enhanced-SERIAL CH9102 (COMx)".

Note the COM number — you'll need it in the Arduino IDE.

### 2. Install Arduino IDE

Download from https://www.arduino.cc/en/software
Install the Windows version. Say yes to USB drivers during install.

### 3. Add ESP32 Board Support

1. Open Arduino IDE
2. File → Preferences
3. In "Additional boards manager URLs" paste:
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
4. Click OK
5. Tools → Board → Boards Manager
6. Search "esp32" and install "esp32 by Espressif Systems" (~300MB)

### 4. Configure Board Settings

- Tools → Board → esp32 → ESP32 Dev Module
- Tools → Port → (your COM port from step 1)
- Tools → Upload Speed → 921600
- Tools → Flash Size → 4MB (32Mb)
- Tools → Partition Scheme → Default 4MB with spiffs

### 5. Install Required Libraries

Open Sketch → Include Library → Manage Libraries and install:

1. **XPowersLib** by lewisxhe — for the AXP2101 power manager
2. **TinyGPSPlus** by Mikal Hart — for NMEA GPS parsing

## Firmware Stages

Work through these in order. Each stage builds on the previous one
and verifies a specific hardware subsystem before moving on.

### Stage 1: Hello World (`01_hello_world/`)

**Verifies:** USB serial communication, compiler, upload process

Open `01_hello_world.ino`, upload it, then open Serial Monitor at
115200 baud. You should see "T-Beam is alive!" and heartbeat messages.

If upload fails with "Connecting...": hold BOOT button, tap RST
button, release BOOT. This forces the ESP32 into bootloader mode.

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

1. Copy `secrets.h.example` to `04_trip_logger/secrets.h` (the sketch
   folder; the build won't find it anywhere else) and fill in your WiFi
   credentials, `SERVER_HOST`/`SERVER_PORT`, and `AUTH_TOKEN` (the
   token must match `INGEST_AUTH_TOKEN` in the server's `.env`).
2. Partitions are handled for you: the sketch folder includes a
   `partitions.csv` (no OTA: 1.5 MB app, 2.375 MB of trip storage), and
   the ESP32 core uses it in place of whatever Tools -> Partition Scheme
   says. At ~118 bytes per second of driving that's roughly 5-6 hours
   between uploads. Normal firmware never formats the trip partition
   automatically: a mount error halts logging and preserves queued data.
   A fresh device or a changed partition layout needs the explicit
   first-time provisioning below.

#### Storage provisioning and mount-error recovery

An existing device with this partition layout needs no formatting. If the
Serial Monitor reports `LittleFS mount failed`, preserve the flash before
troubleshooting; a mount error does not establish that the stored trips
are unrecoverable. Recheck the partition layout and try the previously
working firmware before considering a destructive reset.

For a **new, empty device**, or only after all existing trips have been
uploaded or independently backed up, provision the partition once:

1. In `mountTripStorage()`, temporarily change `LittleFS.begin(false)` to
   `LittleFS.begin(true)` and flash the sketch with the included partition
   layout. This permits formatting on mount failure and **erases stored
   trips** if formatting is needed.
2. Let the device reach `Setup complete. Logging trips.`
3. Restore `LittleFS.begin(false)` and flash again with the same partition
   layout before using it in the car. Do not retain or commit the temporary
   formatting-enabled build.

#### Normal operation

How it behaves once flashed:

- **Trip detection:** logging starts after you sustain >5 mph for
  10 s and ends after you're below 2 mph for 3 minutes (stop-and-go
  traffic won't split a drive). Trips shorter than ~30 s or 0.1 mi are
  discarded as noise.
- **Crash-safe logging:** each GPS point is written to a `.gpx` file
  and the file is closed immediately, so pulling USB power loses at
  most one point. On boot, any file left unclosed by a hard power cut
  is repaired automatically.
- **Time:** comes from the GPS (UTC). Points captured before the first
  fix are buffered and back-dated once UTC arrives.
- **Upload:** while parked on your home WiFi, finished trips are POSTed
  to the ingest server as GPX and deleted locally on success. GPS
  parsing runs on core 1; networking runs on core 0 and never blocks
  it.
- **Power loss:** when the car turns off (USB power removed), the
  current trip is finalized cleanly and kept for the next upload.

Watch the Serial Monitor (115200) for state transitions:
`Trip started` → (drive) → `Trip finalized`, then
`Upload ... -> HTTP 200 (deleted)` when you get home.

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
| BOOT Button     | 0        | Hold during upload if needed   |

## Important Notes

- The T-Beam V1.2 uses **AXP2101**, not AXP192. Many online
  tutorials and code examples reference AXP192 — they won't work
  without modification. Use XPowersLib which supports both.

- The GPS antenna (IPEX connector) must be attached for satellite
  reception. Without it, the GPS module will never get a fix.

- A **data-capable** micro-USB cable is required for programming.
  Charge-only cables will power the board but won't appear in
  Device Manager. Test with your phone if unsure.

- Use a **protected 18650 cell** for the battery. It provides a
  power buffer for clean shutdown when the car turns off.
