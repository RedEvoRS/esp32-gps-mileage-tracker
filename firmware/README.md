# Firmware — Legacy T-Beam V1.2 Reference

> **Current hardware target:** LilyGO T-Beam Supreme with the u-blox
> **MAX-M10S GPS** version (not L76K), plus a **SanDisk 32 GB Max Endurance
> microSD card** for trip storage. See LilyGO's [T-Beam Supreme hardware
> guide](https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series/blob/master/docs/en/t_beam_supreme/t_beam_supreme_hw.md).
>
> **The Supreme port is in progress.** All four stages run on the Supreme:
> start with [T-Beam Supreme bring-up](#t-beam-supreme-bring-up). Stage 4,
> the trip logger, stores trips on the [microSD card](#microsd-card) and
> falls back to the internal LittleFS partition (about 6 hours of driving)
> when no card mounts. **The microSD support is written but not yet
> bench-tested.** Much of the rest of this README, including most of the
> Stage 4 section, still describes the retired T-Beam V1.2 with NEO-M8N
> GPS. Read [Stage 4 on the Supreme](#stage-4-on-the-supreme) for what
> differs.

The firmware is organized into progressive stages that let you verify
each hardware subsystem before building the full mileage tracker.

The [top-level README](../README.md) describes the Supreme MAX-M10S target
and server setup; its step-by-step flashing guide is still the V1.2 one.

## T-Beam Supreme bring-up

These steps were checked on a real T-Beam Supreme (MAX-M10S version) on
2026-10-18. They cover the factory backup and Stages 1–3. Stage 4 isn't
ported yet.

### Before the first power-up

- **Attach the 915 MHz LoRa antenna** to the SMA socket first. The board
  ships with Meshtastic, which can transmit, and transmitting without an
  antenna can damage the radio.
- **Connect the GPS antenna** to the small push-on socket marked **GPS**,
  which sits next to the one marked **LORA**. Press it on until it clicks.
  A loose GPS cable looks exactly like a dead antenna: no satellites at
  all, even outdoors.
- The board powers an **active** GPS antenna, so an external active antenna
  on an SMA cable works (through an adapter cable to the GPS socket).
- **18650:** match the + marking on the holder. While a cell is fitted,
  unplugging USB does not switch the board off.

### Buttons

Hold the board (or its case) with the screen on the left and the USB-C
port on the right. The buttons sit along the bottom edge:

| Button | Where | What it does |
|---|---|---|
| **RESET** | Left of the close pair near the middle | Restarts the ESP32 |
| **POWER** | Right of that pair | Power key on the PMU: ~1 s switches the board on, ~6 s switches it off |
| **BOOT** | On its own, further right | GPIO 0. Held while the board powers up, it selects download mode |

### Board settings

In the Arduino IDE pick **Tools → Board → esp32 → ESP32S3 Dev Module**, then:

- **USB CDC On Boot:** Enabled
- **USB Mode:** Hardware CDC and JTAG
- **Flash Size:** 8MB (64Mb)
- **PSRAM:** QSPI PSRAM

Install the libraries from [step 5](#5-install-required-libraries) below.
The same settings as an arduino-cli FQBN, which CI uses to compile every
sketch:

```text
esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
```

The Supreme uses the ESP32-S3's built-in USB port, so it needs no CH9102
driver. Windows lists it as **USB Serial Device (COMn)**, USB ID 303A:1001.
The COM number can change between a normal boot and download mode, so
check **Tools → Port** after each restart.

### Back up the factory flash first

Uploading anything replaces the factory firmware. Read the whole 8 MB
first, so the board can go back to stock. esptool comes with the ESP32
core, at
`%LOCALAPPDATA%\Arduino15\packages\esp32\tools\esptool_py\<version>\esptool.exe`.
Put the board in [download mode](#download-mode), then:

```text
esptool --chip esp32s3 --port COMn --before no-reset read-flash 0 0x800000 supreme-factory.bin
```

Read it a second time into another file and compare the two
(`certutil -hashfile <file> SHA256` on Windows). Keep the backup outside
this repo: it can hold the factory firmware's device keys. To go back to
stock later:

```text
esptool --chip esp32s3 --port COMn write-flash 0 supreme-factory.bin
```

**Windows read quirk:** on our board, reading the factory flash over the
built-in USB port failed every time at one 4 KB block (address 0x1A6000)
with `No more data to read from the serial port`. Reading that block as
two 2 KB halves worked. If a full read stops at the same address on every
try, read the flash in pieces (say 1 MB at a time) and split any piece
that fails into smaller reads. It's a USB transfer quirk, not a faulty
board.

### Download mode

- **While one of this project's sketches is running**, Upload switches the
  board into download mode by itself. No buttons needed.
- **Under the factory firmware** that automatic reset didn't work, so enter
  download mode by hand. With an 18650 fitted: hold POWER for about 6 s
  until the board is off, hold BOOT, press POWER for about 1 s, keep
  holding BOOT for 2–3 s more, then release. The screen stays dark.
  Without a battery: unplug USB, hold BOOT, plug USB back in, and release
  BOOT after 2–3 s.
- **After uploading from download mode entered by hand,** the board can
  restart straight back into download mode. The serial log then shows
  `boot:0x0 (DOWNLOAD(USB/UART0))` and `waiting for download`. Press RESET
  once to run the new sketch.
- Opening the serial port can restart the board, so startup messages may
  appear twice.

### microSD card

The trip logger writes trips to a microSD card in the Supreme's slot.
**Not yet bench-tested:** the card's pins come from LilyGO's source, and
Stage 2's `d` key is the first check to run when a card is fitted.

- **Format: FAT32.** The firmware can't read exFAT, which is what most
  cards of 64 GB and up ship with. Use a microSDHC card of 32 GB or less;
  this project uses a SanDisk 32 GB Max Endurance. These come formatted
  FAT32. On Windows, the card's **Properties** show the file system. If it
  says exFAT or NTFS, reformat it as FAT32 (this erases the card).
- **Fake cards** report a size they don't have and silently lose anything
  written past their real capacity, which here means lost trips. A full
  write-and-verify check with `f3` (Linux/WSL) or H2testw (Windows) takes
  about an hour for 32 GB and is worth doing once.
- **Only insert or remove the card with the board switched off.** The slot
  has no card-detect switch, and the firmware picks its trip storage once,
  at boot.
- **The firmware never formats the card.** A card that won't mount is left
  untouched, and trips go to internal flash instead.
- **At boot** the log says which storage it chose:
  - `Trip storage: microSD (29.72 GiB free); internal flash queue mounted.`
    Trips go to the card.
  - `WARNING: no microSD card or mount failed -- check power/wiring and FAT32 (exFAT unsupported); card never formatted.`
    followed by
    `WARNING: microSD unavailable -- using internal flash (2.30 MiB free).`
    No usable card: trips go to the internal partition, which holds about
    6 hours of driving.
- **Uploads** come from both places: trips on the card, and any left in
  internal flash from a boot without a card. Each is deleted only after
  the server accepts it.
- **Power cuts:** FAT copes with a sudden power loss worse than LittleFS
  does. The 18650 is what lets the logger close its files and unmount the
  card cleanly after the car's USB power goes away, so keep it fitted.
- **Very long trips:** firmware splits a trip into pieces below 8 MiB,
  half the server's 16 MiB upload limit. Recording continues immediately
  with the boundary fix shared by both pieces, and each uploads separately.
  A very long drive can therefore show up as more than one trip. This also
  keeps a parked, powered device's GPS drift from creating one oversized file.

### Supreme pin and power reference

Measured with Stages 1–3 unless marked.

| Function | Connection | Notes |
|---|---|---|
| PMU (AXP2101) | `Wire1`, SDA 42 / SCL 41, address 0x34 | Interrupt on GPIO 40, which can't wake the ESP32-S3 from deep sleep |
| RTC (PCF8563) | `Wire1`, address 0x51 | Shares the PMU's bus |
| OLED (SH1106), BME280 | `Wire`, SDA 17 / SCL 18, 0x3C / 0x77 | |
| GPS UART | ESP32 RX 9 / TX 8, 9600 baud | u-blox MAX-M10S, firmware SPG 5.10 |
| GPS_EN | GPIO 7 | Drive HIGH. Low or high made no difference on this GPS |
| GPS PPS | GPIO 6 | One pulse per second once the GPS has a fix |
| GPS power | ALDO4, 3.3 V | |
| LoRa power | ALDO3 | Keep it off. It comes up **on** after a power-on by USB |
| SD card power | BLDO1 | Comes up at 1.8 V: set 3.3 V before switching it on |
| SD card bus | SPI MOSI 35 / SCK 36 / MISO 37, CS 47 | From LilyGO's source; Stage 2's `d` key checks it. The IMU shares the bus: hold its CS (GPIO 34) high |
| GPS backup supply | The 18650 directly | Not the PMU's VBACKUP output, which can stay off |

**Rails at power-up (AXP2101 defaults):** DCDC1 3.3 V on (this powers the
ESP32: never switch it off), DCDC4 1.8 V on, DCDC5 3.3 V on, ALDO1 1.8 V on,
ALDO2 3.3 V on, ALDO3 on after a power-on by USB (off after one by the
POWER button), ALDO4 off, BLDO1 1.8 V on, VBACKUP off, the rest off.

**Power-off and wake:** `PMU.shutdown()` switches the whole board off. It
stays off while USB stays plugged in, and the next time USB power is
plugged in, the board switches on and boots normally.

**GPS starts:** with the 18650 fitted, the GPS keeps its satellite data
while it's powered down, and found a fix 2 s after being off for a minute
(a hot start). With the cell removed it forgets everything, and the next
fix took 33 s outdoors (a cold start). It also keeps its settings until
the cell comes out, including any left by the factory firmware.

## Legacy V1.2 Hardware

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
3. **U8g2** by olikraus, version **2.36.18** — for Stage 4's debug OLED page

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

Stages 1–3 are for the **T-Beam Supreme**. Use the
[Supreme board settings](#board-settings) and
[back up the factory flash](#back-up-the-factory-flash-first) before the
first upload. Stage 4 runs on the Supreme too; see
[Stage 4 on the Supreme](#stage-4-on-the-supreme) for where its section
still describes the V1.2. Every stage prints at 115200 baud; open the
Serial Monitor after uploading.

### Stage 1: Hello World (`01_hello_world/`)

**Verifies:** USB serial, the board settings, uploading

Prints the chip, the flash and PSRAM sizes and the reason for the last
restart, then a heartbeat every 2 s. Expect
`Flash: 8388608 bytes | PSRAM: 8388608 bytes`. A PSRAM size of 0 means
the PSRAM setting is wrong. Stage 1 doesn't touch the power chip.

### Stage 2: Power Manager (`02_pmu_test/`)

**Verifies:** both I2C buses, the AXP2101, the power rails, power-off and
wake

Prints every rail as it found them at boot, then switches ALDO3 (LoRa) off
(it stops if it can't) and ALDO4 (GPS) on. It scans both I2C buses
(expect 0x34 and 0x51 on `Wire1`, 0x3C and 0x77 on `Wire`) and repeats the
power status every 5 s. Keys: `h` help, `p` status, `i` scan again, `o`
sensor rails on/off, `d` microSD check, `s` then `Y` to power the board
off. Unplug and replug USB to switch it back on.

The `d` key powers the card at 3.3 V, mounts it, prints its type and size,
writes, reads back and deletes a small test file, then unmounts it and
switches its power off. It never formats the card. Expect
`SD write/read-back: PASS; delete: PASS`. `no SD card responding` means no
card, or a power or wiring fault. `card responds but mount failed` usually
means the card isn't FAT32.

An ESP32 reset during a PMU read can leave the battery-powered PMU holding
the shared PMU/RTC I2C bus low. Stages 2, 3 and 4 now check that bus before
startup. If needed, they send up to nine open-drain clocks (5 microseconds
low/high), then a STOP condition. Each SCL release waits at most 1 ms for
clock stretching; a line that stays low is reported as a failure.

PMU startup gets up to three attempts, releasing `Wire1` and waiting 10 ms
between attempts. Serial reports recovery results, clock counts and retries;
Stage 4 prints these after its early LoRa rail safety check. If all attempts
fail, the existing halt remains. Recovery is host-tested; repeatedly resetting
during PMU reads still needs a bench check. Stage 4 retains the boot result
and has a host-tested helper for E2007 PMU_STARTUP_RECOVERY (#151). Recording
that notice still needs the diagnostics journal and fault hooks (#122/#124);
failed-startup reporting also needs the degraded loop (#126). OLED reporting
remains with #127.

### Stage 3: GPS (`03_gps_test/`)

**Verifies:** GPS power, the GPS UART, the antenna, getting a fix, hot
starts

Needs the GPS antenna under open sky: outdoors, or at a window without
metal-coated glass. Every 2 s it prints whether NMEA is arriving, whether
there's a fix, satellites used, HDOP, PPS pulses and the time to first fix
since the GPS rail came on. Coordinates stay hidden unless you press `l`.
Don't paste logs with coordinates or raw NMEA anywhere public: they show
where you are.

Keys: `g` signal strength of every satellite heard plus the antenna/RF
status, `4` GPS power off/on (time a hot start), `v` GPS firmware version,
`u` the legacy UBX config Stage 4 sends, `e` GPS_EN, `b` VBACKUP, `3` LoRa
rail (needs the antenna attached and a confirmation), `r` raw NMEA, `h`
help.

What good looks like outdoors: a cold start in about 33 s, a hot start in
about 2 s, and `g` showing 19 or more satellites heard at 40+ dBHz with
the AGC around 30%. `heard=0` with the AGC stuck near 80% means no signal
reaches the receiver: check that the GPS cable is clicked onto the **GPS**
socket.

### Stage 4: Full Mileage Tracker (`04_trip_logger/`)

**Purpose:** Trip detection, mileage accumulation, crash-safe GPX
logging to LittleFS, and WiFi upload to your home server.

#### Stage 4 on the Supreme

The rest of this section was written for the V1.2. On the Supreme
(bench-tested 2026-10-18), these things differ:

- **Build** with the [Supreme board settings](#board-settings), not the
  V1.2 ones in [step 4](#4-configure-board-settings). Stage 4's
  `partitions.csv` still applies.
- **First flash:** trip storage is only set up on a fully erased partition
  (see [First flash on a new board](#first-flash-on-a-new-board)). The trip
  partition can still hold leftover data, from the factory firmware for
  example, and startup then halts rather than format over it. So erase the
  flash once for the first Stage 4 upload (Arduino IDE: **Tools → Erase All
  Flash Before Sketch Upload → Enabled** for that one upload, then set it
  back to Disabled so later uploads keep your trips).
- **Storage:** trips go to the [microSD card](#microsd-card) when one
  mounts at boot, and to the internal LittleFS partition otherwise (not yet
  bench-tested with a card). Trip storage in internal flash is still set up
  as described under First flash, because it's the fallback.
- **Pins and power** are the Supreme's: see the
  [Supreme pin and power reference](#supreme-pin-and-power-reference), not
  the V1.2 pin table further down. At boot the logger switches the LoRa
  rail off first, the SD rail and VBACKUP off, and the GPS rail on.
- **Parking:** after the car's USB power has been gone 10 s and the board
  has idled on battery for 10 minutes (the upload window), it **switches
  itself off completely**, not into deep sleep. Power returning to the USB
  port switches it back on. The boot log shows
  `PMU power-on source 0x04, power-off source 0x02` when that worked.
- **GPS settings:** after a cold power-up the MAX-M10S ignores
  configuration for a few seconds, so the logger retries in the
  background. Expect `GPS configured on attempt 2 (… ms after config
  start)` about 10 s after power-up.
  Both builds also request UBX NAV-PVT once per navigation epoch (1 Hz)
  to check the receiver's speed accuracy before recording movement.
- **Debug screen:** for road testing, the OLED shows a status page that
  redraws about once a second. Example:

  ```text
  ACTIVE 0h12m          trip state, time since boot
  SATS R:19 U:12 A31%   satellites heard / used in the fix, GPS AGC
  FIX OK H0.9 0s        fix yes/no, HDOP, seconds since last position
  Speed 42.3 mph
  Trip 12m 008.4mi      current trip: minutes, miles ("Trip --" if none)
  Store SD Q:3          trip store (SD, FLASH, NONE), trips waiting to upload
  Net OFF OK 20s        WiFi (OFF/SCAN/JOIN/ON), last upload, seconds ago
  B85% 4.02V U1 C1      battery, USB power in (U1), charging (C1)
  ```

  `--` means unknown or stale. The queue count (`Q`) is refreshed by the
  upload task about every 30 s, so a trip that just ended can take one
  cycle to appear, and an upload age stops counting at 999 s.

  Things to know:
  - It's switched by `DISPLAY_DEBUG` at the top of the sketch (default `1`).
    Set it to `0` to leave the screen untouched and disable GSV/MON-RF
    diagnostics. NAV-PVT speed accuracy gating stays enabled in both builds.
  - A question mark, such as `Speed 3.3? mph`, means the displayed speed
    failed the accuracy check; it cannot start or extend a trip and the
    point is omitted from the GPX.
  - `R` counts satellites actually **heard**: GSV entries with a positive
    C/N0 (signal strength), counted once even if several signals name the
    same satellite. The receiver's predicted "in view" total can stay high
    indoors with no signal, so it is not used. Each complete GSV group
    replaces its previous signal group; data expires after 15 s. The GPS
    sends GSV every 5th second, because a full set is about 1 KB and the
    link runs at just 9600 baud (~960 bytes/s).
  - `A` is GPS automatic gain control (AGC), as a percentage of its maximum.
    Near 100% means the receiver is using full gain; compare it with `R`
    and the serial RF log to investigate weak or missing signals. Counts
    stop at 999; the space before `A` is omitted if needed to fit 21 chars.
  - Every 30 s the serial log prints one `GPS RF:` line: jamming state,
    CW jamming indicator (0-255), noise per ms, AGC, antenna status/power,
    heard satellites (including how many reach 30 dBHz and the strongest
    C/N0), satellites used, HDOP and RMC fix status (`A` valid, `V` invalid).
    It also shows the latest fresh NAV-PVT `sAcc` (speed accuracy, m/s,
    two decimals) and `hAcc` (horizontal accuracy, metres, one decimal).
    It contains no coordinates. MON-RF polling starts after boot GPS
    configuration succeeds or gives up; each line reports the preceding
    poll's reply without waiting for it. A missing/invalid reply shows
    `rf --`, and unknown or stale NMEA values show `--`. Antenna status
    and power may read `DONTKNOW` without an antenna supervisor; that
    alone does not mean the antenna failed.
  - The screen can't stop logging. If it's missing or an I2C write fails,
    the serial log says `WARNING: debug OLED I2C failed` once and the
    screen stays off until the next boot.
  - It uses maximum SH1106 contrast (`DEBUG_OLED_CONTRAST = 255`). The old
    setting (`16`) was unreadable outdoors on the first road test
    (2026-10-19). Full contrast speeds OLED burn-in and draws a little more
    current, acceptable for this temporary shakedown-only page. It shifts
    sideways a few pixels each minute to limit burn-in and switches off
    before the board powers down. The future everyday display has its own
    burn-in rules (see the display policy in `DIAGNOSTICS.md`).
  - Not yet checked on the board: the screen's supply rail and current
    draw, whether full contrast is readable in daylight, and which GSV
    talkers the MAX-M10S actually sends.

  Bench check for the heard count: after a cold start under open sky, `R`
  should rise as signals arrive and `U` should rise when the receiver gets
  a fix. Indoors or with a blocked antenna, `R` should fall as signals
  disappear, even if the receiver still predicts satellites in view. Check
  that both show `--` when their input is stale, and record the RF line
  during a fix loss on the next drive. These checks still need the board.
- **Expected boot log:**

  ```text
  === Stage 4: Trip Logger ===
  LoRa rail (ALDO3) verified off; SD rail (BLDO1) off; VBACKUP off.
  PMU power-on source 0x04, power-off source 0x02.
  PMU ready, MAX-M10S GPS rail (ALDO4) on at 3.3 V, charge to 4.1 V. VBUS present.
  Trip storage: microSD (29.72 GiB free); internal flash queue mounted.
  GPS: waiting for checksum-valid NMEA (up to 3 s).
  Setup complete. Logging trips.
  GPS ready: checksum-valid NMEA received.
  GPS configured on attempt 2 (9874 ms after config start): RMC+GGA, GSV every 5 epochs, automotive model.
  ```

Before flashing:

1. **Create `secrets.h`.** From the repo's top folder, copy
   `firmware/secrets.h.example` to `firmware/04_trip_logger/secrets.h`,
   next to the `.ino` (the build won't find it anywhere else), and fill
   it in. The top-level README has a table of
   the settings in the
   [legacy V1.2 flashing section](../README.md#3-legacy-v12-firmware-do-not-use-with-the-supreme).
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
3. **On a new board, check the first-flash output below.** Storage is
   provisioned automatically only when the whole partition is erased.

#### First flash on a new board

Flash the sketch normally, with its bundled `partitions.csv`, and watch
the Serial Monitor. If the trip partition mounts, startup proceeds as
usual. If it doesn't mount, the firmware reads **every byte** of the
`spiffs` data partition. Only when every read succeeds and every byte is
erased (`0xFF`) does it format the partition once and mount it again. It
prints

```
LittleFS partition is fully erased; provisioning trip storage.
...
Setup complete. Logging trips.
```

No sketch edits or second flash are needed. A board sold as new may still
contain factory test data, so "new" alone is not enough to allow formatting.
Any non-erased byte, failed read, or missing partition leaves storage
untouched and halts startup with `LittleFS mount failed. Storage preserved`.
A format or subsequent mount failure also halts, with
`LittleFS provisioning failed`. The battery's charge-temperature guard
keeps running while halted. Follow [storage recovery](#if-the-trip-storage-wont-mount-later)
if startup halts; do not enable format-on-mount-failure.
An interrupted format may leave non-erased bytes, so the next boot can
also halt for recovery instead of trying another format.

After that, the trip storage survives reflashing. You only do this again
if the partition is fully erased again. Before changing the partition
layout or erasing flash, upload or independently back up every trip.

#### If the trip storage won't mount later

On a board that's been working, `LittleFS mount failed` doesn't mean the
trips are gone. Keep format-on-mount-failure disabled. Check that you
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

If the log says the partition is missing or empty, check the flashed
`partitions.csv` first. A partition read error needs investigation and a
backup, not an erase attempt. `contains non-erased data` can mean queued
trips, a damaged filesystem, or factory firmware data on a new board.

For a board that has never logged trips, or when you have independently
backed up the stored data and deliberately decided to reset trip storage,
you can erase **only the trip partition** once. This deletes everything
in that region. First finish the backup above and confirm the board is
running this sketch's bundled partition layout. If a previous layout put
trip data elsewhere, preserve that region too before changing layouts.
Do not use this step to recover trips you still need from the device.

With Serial Monitor closed, run:

```
esptool --port <port> erase-region 0x190000 0x260000
```

Older esptool versions spell it `erase_region`. The address and size are
for this project's bundled `spiffs` partition only. Reset the board (or
power-cycle it) after a successful erase; the unchanged firmware verifies
the erased partition, provisions it, and reaches `Setup complete. Logging
trips.` No second flash or format-on-mount-failure build is needed. If it
still halts, investigate the reported error rather than repeating erases.

#### Normal operation

How it behaves once flashed:

- **Trip detection:** logging starts after you sustain >5 mph for
  10 s and ends after you're below 2 mph for 3 minutes (stop-and-go
  traffic won't split a drive). Trips shorter than ~30 s (not counting
  the 3 minutes parked at the end) or 0.1 mi are discarded as noise.
  Speed must also pass the GPS receiver's accuracy check: a checksum-valid
  NAV-PVT received within 1.5 s, with `gnssFixOK`, a 3D or combined fix,
  and `sAcc` at most 0.5 m/s plus `hAcc` at most 5 m when starting,
  or 1.0 m/s plus 10 m during a trip.
  These are first guesses to calibrate from indoor and drive logs. The
  arrival window tolerates either message order at 1 Hz, so the latest
  sample may belong to the previous epoch; it does not change GPX times.
  Rejected speeds count as stopped: arming resets, or an active trip enters
  its normal three-minute cooldown. Rejected points enter neither GPX nor
  the distance total, and distance never bridges across them. Leaving
  cooldown requires three consecutive trusted fixes above 2 mph; any other
  fix resets that count, while the original three-minute deadline keeps
  running. Trusted cooldown fixes still enter GPX but add no distance or
  moving duration. The third moving fix resumes ACTIVE and establishes a
  new distance baseline, without bridging across cooldown. A gap over
  five seconds between accepted points starts a new track segment. The
  separate five-minute watchdog still ends a trip if all fixes disappear.
  Fallback is disabled until boot GPS configuration finishes, whether it
  succeeds or gives up after five attempts. Until then, speeds without
  fresh, accurate NAV-PVT stay untrusted. If NAV-PVT is absent for more
  than ten seconds after configuration ends (or since the last NAV-PVT), one
  `WARNING:` announces fallback to the previous RMC-only behavior. A single
  return message announces restored accuracy gating. Fallback can still
  record false movement: check the warning during the hardware shakedown.
- **Power-cut recovery:** each GPS point with valid RMC UTC is written
  to a `.gpx` file and the file is closed immediately to limit data loss.
  The buffered untimed tail is still in memory and can be lost on a
  reset. On boot the firmware attempts to repair incomplete files, keeping
  ambiguous or damaged files for inspection. Hardware power-cut testing
  is still needed; there is no verified one-point loss limit.
- **Time:** each trackpoint uses the UTC date and time from the same RMC
  sentence as its position, including centiseconds. Zero centiseconds use
  whole seconds (`...SSZ`); otherwise GPX writes three fractional digits
  (`...SS.sssZ`, for example `.010Z` or `.250Z`). Processing delays and
  system-clock corrections do not change the position's epoch. A fix
  without valid RMC date/time stays untimed even if GPS or NTP has set
  the system clock. Untimed points retain their order in a 90-point buffer;
  older points spill to flash, and the remaining tail is flushed before
  the next timed fix or at trip end, always without invented times.
  System-clock synchronization and local file naming are independent.
  The viewer lists a wholly untimed trip without a date.
  A partly timed trip is marked **Partial timing**; its full-trip dates,
  duration and average speed remain unknown while its distance is retained.
  GPS segment boundaries stay attached to the first fix after each gap,
  including fixes buffered before UTC.
- **Upload:** the board checks for finished trips every 30 s and scans
  for your home WiFi when a retry is due and a complete trip is waiting.
  Incomplete files are preserved for recovery and do not trigger radio work.
  Failed scans, connections, or
  uploads double the retry wait (60 s, 120 s, 240 s, then at most 5 min).
  The wait starts after the attempt finishes; the next 30 s check runs it
  once due. A successful upload resets the wait to 30 s, and an empty
  queue clears it. A trip ending clears it too, since the car may just
  have parked at home: the board tries within 30 s, then about 1, 3 and
  7 minutes later, all before it sleeps. Each failed try prints why and
  when the next one is, e.g.
  `Network: home WiFi not found "<ssid>" -- next try in 60 s.` When connected, it
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
but also an incomplete file or one set aside during repair. A failed WiFi
scan or join prints `Network: home WiFi not found "<ssid>"` or
`Network: couldn't join home WiFi "<ssid>" (status N)`, with the time until
the next try (up to 5 minutes). Check the SSID and 2.4 GHz coverage after
a scan miss. Join status `4` (`WL_CONNECT_FAILED`) can indicate a wrong
password; `1` is `WL_NO_SSID_AVAIL` and `6` is `WL_DISCONNECTED`. These are
connection states, not a definitive diagnosis. Press **RST** to try again
at once. See the
[legacy V1.2 troubleshooting steps](../README.md#4-check-it-works-legacy-v12-only)
for what to check and preserve in each case.

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

### Diagnostic journal module

Stage 4 carries a board-independent `event_log.h` / `event_log.cpp` module with
a fixed producer queue, CRC-protected rolling segments, durable acknowledgment
metadata, SD/LittleFS/RAM fallback and host fault tests. It is not instantiated
by the sketch yet, so it adds no runtime writes or tasks. The mounted-filesystem
adapters, producer/service hooks, trip-space reserve and upload worker remain
integration work. See [DIAGNOSTICS.md](../DIAGNOSTICS.md#eventlog-module-122-implementation)
for the API, on-disk format, quota and recovery rules. Run
`bash firmware/host_test/run.sh` for fault tests and the exported-event check
against the server's stdlib protocol validator.

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
link speed. The server's worker timeout (900 s) allows a full 8 MiB trip
piece at 10 kB/s (~839 s), with a margin for handling it. A warning at
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
