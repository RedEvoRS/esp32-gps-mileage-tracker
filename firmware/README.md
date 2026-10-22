# Firmware

Stage 4 records an early BOOT battery reading and one INFO battery sample
before clean PMU power-off, then uploads them with the existing diagnostics.
Update the server to catalog v6 **before** installing this firmware; older
firmware remains compatible with the new server. No meter or new viewer UI is
needed. The read-only helper and interpretation guide are in
[Reading parked battery drain](../DIAGNOSTICS.md#reading-parked-battery-drain-without-a-meter).
Collect at least three parked intervals of eight hours or longer; voltage and
PMU percentage give rough estimates, not a measured off-state current.

The firmware targets the **LilyGO T-Beam Supreme with u-blox MAX-M10S GPS**
(not the L76K version), using a **SanDisk 32 GB Max Endurance microSD card**
for trips. LilyGO's [T-Beam Supreme hardware guide](https://github.com/Xinyuan-LilyGO/LilyGo-LoRa-Series/blob/master/docs/en/t_beam_supreme/t_beam_supreme_hw.md)
identifies the board and its connectors. The four progressive stages check
USB, power and GPS before installing the trip logger for use in the car.
The power-management unit (PMU) controls the board's rails, battery and
shutdown; Stage 2 checks it before the GPS and trip-logging stages.

All four stages run on the Supreme. On 2026-10-21 (UTC), Stage 4's debug
build uploaded four real trips from microSD to the deployed server, then
delivered all 115 pending diagnostic events and a current snapshot.
Uploads with debug mode off and server-down retry were also verified on
2026-10-21 (UTC). LittleFS uploads and server errors (401/413/5xx) remain
untested on the Supreme.

**Have a T-Beam V1.2?** That board is retired. Its last firmware and full
instructions are on the frozen [`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2),
which receives no updates. The instructions below are for the Supreme.
See the [top-level README](../README.md) for the complete setup sequence.

## Flash from the server build

This is the primary install/update route. Complete
[server setup](../README.md#step-1--set-up-the-server) first and accept its
firmware-build prompt. No Arduino IDE, Python or ESP32 toolchain is needed
on the PC connected to the board. The server's container downloads
about 1.8 GB of tools on the first build, keeps an image of about 2.7 GB,
and reuses it later (allow 10 GiB free for the first build); see the
[builder guide](../server/firmware-builder/README.md).

### PC prerequisites and commands

Use Windows PowerShell 5.1/7 with OpenSSH `scp`, or macOS/Linux with Bash,
curl, tar, jq and OpenSSH `scp`. Install jq with `brew install jq` on macOS
(Homebrew required), or `sudo apt install jq` on Debian/Ubuntu. The pinned
standalone esptool supports Windows x64, macOS Intel/Apple Silicon, and
Linux x86-64, ARM64 and ARMv7. Other platforms need a compatible standalone
esptool supplied with `-Esptool` / `--esptool`. Native USB needs no Windows
driver. On Linux your login needs serial-port access (on Debian/Ubuntu,
join `dialout` and log out/in). Check `ssh <user>@<host>` works from this PC.

Use the commands printed by setup for its release. The latest-release forms are:

```powershell
irm https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 | iex
```

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.sh | bash -s -- --server '<user>@<host>'
```

These execute downloaded code; the [README trust note and download/read/run
alternatives](../README.md#setting-it-up) apply. Saving the script also lets
you pass options on Windows:

```powershell
Invoke-WebRequest https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.ps1 -OutFile flash.ps1
Get-Content .\flash.ps1
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server '<user>@<host>' -FirstInstall -Port COMx
```

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/flash.sh -o flash.sh
less flash.sh
bash flash.sh --server '<user>@<host>' --first-install
```

Replace placeholders first. Omit `-Port` to choose from detected ports.
`-InstallDir '~/mileage-tracker'` / `--install-dir '~/mileage-tracker'` selects
the remote installation (use your actual path if nondefault). Remote paths
must be simple absolute or `~/` paths without spaces; SSH targets must be
`user@host` with a hostname or IPv4 address, rather than an IPv6 literal. For help run
`powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Help` or
`bash flash.sh --help`. PowerShell 7 can use `pwsh` instead of `powershell.exe`.

### First install, update and cleanup

Before power-up, attach both antennas and fit the 18650 with correct polarity;
see [hardware preparation](#before-the-first-power-up). Fit the FAT32 card
while switched off. Close serial monitors. The script fetches
`firmware-out/current/` over `scp`, validates its manifest and all four part
hashes, then downloads checksum-pinned standalone **esptool from Espressif's
GitHub releases**. The firmware is your private build, not a generic binary.
Server output directories/files are owner-only (0700/0600); binaries contain
your WiFi password and upload token. The PC's temporary firmware copy is
private and removed on exit; keep any manually copied binaries private too.

For **every script flash**, enter download mode as prompted: hold BOOT,
plug USB in (or tap RESET), wait 2–3 seconds, release BOOT. Select the port;
its name can change in download mode. See [download mode](#download-mode)
for factory-firmware and battery details. Press RESET with BOOT released
after flashing to run the program.

- **First install on a factory board:** use `-FirstInstall` / `--first-install`,
  or answer yes at the first-install prompt. The script reads all 8 MB of
  factory flash twice, verifies the sizes and matching hashes/content, and
  stops before any erase/write if verification fails. Windows retries failed
  reads in smaller pieces for the native-USB read quirk. The default backup
  location is `mileage-tracker-backups` under your PC home directory; the
  script asks for the location and prints the saved path. Keep it outside
  the repo: it can contain factory device keys. Type **`ERASE`** to confirm
  the full internal-flash erase, then it writes the bootloader, partition
  table, boot support image and app. All internal trips and settings memory
  (NVS), including counters and enrollment records, are erased. The SD card
  is untouched. The erase lets Stage 4 provision its internal trip filesystem;
  see [first-flash behavior](#first-flash-on-a-new-board).
- **Update an installed tracker:** update/build on the server first, then
  rerun the script without the first-install flag and answer **no** at that
  prompt. This writes only the app, preserving NVS, internal trips and SD
  trips. It does not update the partition table or bootloader. WiFi/address/token
  changes need another server build and flash; the builder asks for settings
  each time. Never use first install for a routine update.
- **One-time enrollment cleanup:** for a new board and fresh card with no
  saved server snapshot revision, the builder prepares both `current/`
  (enrollment) and `cleanup/` (normal). After Device health shows a newly
  received current snapshot, run the saved script with `-Cleanup` / `--cleanup`
  as below. Contact from a trip alone is insufficient. Cleanup writes only
  the app, removing enrollment defines and preserving settings/trips. Do it
  before the next server build: a normal build removes stale cleanup output.
  Used boards/cards or an existing server revision need the
  [diagnostics recovery procedure](../DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery),
  rather than claiming they are new.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server '<user>@<host>' -Cleanup
```

```bash
bash flash.sh --server '<user>@<host>' --cleanup
```

`-DryRun` / `--dry-run` still copies and validates firmware and obtains/runs
esptool's version command, but never opens the serial device. It prints the
plan; add the first-install flag to preview backup/erase/all-part writes.
`-Path` / `--path` can select a local firmware folder instead of SSH;
for cleanup select the local cleanup folder explicitly. `-SkipBackup` /
`--skip-backup` requires an explicit first-install flag and is only for
re-provisioning a board whose factory backup you already hold; it still
requires `ERASE`. Do not use it to work around a failed first backup.

## Advanced: build from source

The remaining hardware-test and build instructions are for a local source
build. Clone the repository or download its source ZIP, install the Arduino
tools below, and prepare Stage 4 settings as described under
[the trip logger](#stage-4-the-trip-logger). The scripts above build that
same logger on the server.

## T-Beam Supreme bring-up

These steps were checked on a real T-Beam Supreme (MAX-M10S version) on
2026-10-18. They cover the factory backup and Stages 1–3. Stage 4 also
runs on the Supreme and has recorded real drives on the microSD card.

### Before the first power-up

- **Use a data-capable USB-C cable.** A charge-only cable powers the board
  but provides no serial port for programming or messages.
- **Attach the 915 MHz LoRa antenna** to the SMA socket first. The board
  ships with Meshtastic, which can transmit, and transmitting without an
  antenna can damage the radio.
- **Connect the GPS antenna** to the small push-on socket marked **GPS**,
  which sits next to the one marked **LORA**. Press it on until it clicks.
  A loose GPS cable looks exactly like a dead antenna: no satellites at
  all, even outdoors.
- The board powers an **active** GPS antenna, so an external active antenna
  on an SMA cable works (through an adapter cable to the GPS socket).
- **18650:** use a protected cell and match the + marking on the holder.
  Unplugging USB does not switch the board off while a cell is fitted. It
  powers the upload window and clean storage shutdown after the car turns off.

### Buttons

Hold the board (or its case) with the screen on the left and the USB-C
port on the right. The buttons sit along the bottom edge:

| Button | Where | What it does |
|---|---|---|
| **RESET** | Left of the close pair near the middle | Restarts the ESP32 |
| **POWER** | Right of that pair | Tap for a status card. While running, hold BOOT then tap POWER to toggle debug. ~1 s switches the board on; a shorter long press on battery shuts down cleanly; ~6 s forces it off (see [Normal operation](#normal-operation)) |
| **BOOT** | On its own, further right | GPIO 0. While running, hold it then tap POWER to toggle debug. Held while powering up, it still selects download mode |

### Board settings

In the Arduino IDE pick **Tools → Board → esp32 → ESP32S3 Dev Module**, then:

- **USB CDC On Boot:** Enabled
- **USB Mode:** Hardware CDC and JTAG
- **Flash Size:** 8MB (64Mb)
- **PSRAM:** QSPI PSRAM

For Stage 4, its bundled `partitions.csv` overrides the **Partition Scheme**
menu. Leave **Erase All Flash Before Sketch Upload → Disabled**, except
for the [first Stage 4 upload](#first-flash-on-a-new-board).

Install the libraries from [Install required libraries](#install-required-libraries) below.
The same settings as an arduino-cli fully qualified board name (FQBN),
which automated compile checks (CI) use for every sketch:

```text
esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
```

The Supreme uses the ESP32-S3's built-in USB port, so it needs no separate
USB serial driver. Windows lists it as **USB Serial Device (COMx)**, USB ID 303A:1001.
The COM number can change between a normal boot and download mode, so
check **Tools → Port** after each restart.

### Back up the factory flash first

The flash scripts above automate the verified backup. For source builds,
uploading anything replaces the factory firmware. Read the whole 8 MB
first, so the board can go back to stock. esptool comes with the ESP32
core, at
`%LOCALAPPDATA%\Arduino15\packages\esp32\tools\esptool_py\<version>\esptool.exe`.
Put the board in [download mode](#download-mode), then:

```text
esptool --chip esp32s3 --port COMx --before no-reset read-flash 0 0x800000 supreme-factory.bin
```

Read it a second time into another file and compare the two
(`certutil -hashfile <file> SHA256` on Windows). Keep the backup outside
this repo: it can hold the factory firmware's device keys. To go back to
stock later:

```text
esptool --chip esp32s3 --port COMx write-flash 0 supreme-factory.bin
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
The card's pins come from LilyGO's source. Stage 2's `d` key checked them
on the bench on 2026-10-18, and it's the first check to run when you fit
a card. Stage 4 has since recorded two real drives on the card.

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
    5–6 hours of driving.
- **Upload behaviour in the code:** the logger attempts finished trips from
  both the card and internal flash, including trips left by a boot without
  a card. It deletes each only after an accepted server response. On
  2026-10-21 (UTC), four trips uploaded from the card with HTTP 200 and
  were deleted locally. Internal LittleFS was mounted but held no trips;
  uploading from that fallback remains untested on the Supreme.
- **Power cuts:** FAT copes with a sudden power loss worse than LittleFS
  does. The 18650 is what lets the logger close its files and unmount the
  card cleanly after the car's USB power goes away, so keep it fitted.
- **Very long trips:** firmware splits a trip into pieces below 8 MiB,
  half the server's 16 MiB upload limit. Recording continues immediately
  with the boundary fix shared by both pieces, and each is queued for a separate upload.
  A very long drive can therefore show up as more than one trip. This also
  keeps a parked, powered device's GPS drift from creating one oversized file.

### Supreme pin and power reference

Measured with Stages 1–3 unless marked.

| Function | Connection | Notes |
|---|---|---|
| PMU (AXP2101) | `Wire1`, SDA 42 / SCL 41, address 0x34 | Interrupt on GPIO 40, which can't wake the ESP32-S3 from deep sleep |
| RTC (PCF8563) | `Wire1`, address 0x51 | Shares the PMU's bus |
| OLED (SH1106), BME280 | `Wire`, SDA 17 / SCL 18, 0x3C / 0x77 | Powered by ALDO1, which also feeds the magnetometer and, per LilyGO's schematic, this bus's pull-ups. ALDO1 starts at 1.8 V; Stage 4 sets it to 3.3 V once the PMU checks out, as LilyGO's and Meshtastic's code do. At 1.8 V the bus's high level sits below the ESP32-S3's input spec (#274) |
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

## Development environment setup

### Install Arduino IDE

Download the Arduino IDE from https://www.arduino.cc/en/software and
install the version for your operating system.

### Add ESP32 board support

1. Open **File → Preferences** (**Arduino IDE → Settings** on macOS).
2. In **Additional boards manager URLs**, add:
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
3. Open **Tools → Board → Boards Manager**, search for `esp32`, and install
   **esp32 by Espressif Systems**, version **3.3.11**.

The project's automated compile checks (CI) pin core **3.3.11**. Select it
explicitly: Boards Manager otherwise installs the newest version. The
logger relies on 3.x behaviour; do not use a 2.x core.

### Configure board settings

Use the [Board settings](#board-settings) above: **ESP32S3 Dev Module**,
**USB CDC On Boot → Enabled**, **USB Mode → Hardware CDC and JTAG**,
**Flash Size → 8MB (64Mb)** and **PSRAM → QSPI PSRAM**. Select the board's
current entry under **Tools → Port**. Stage 4's `partitions.csv` overrides
the **Partition Scheme** menu. Keep **Erase All Flash Before Sketch
Upload → Disabled**, except once for the [first Stage 4 upload](#first-flash-on-a-new-board).

The Supreme uses native USB (USB ID **303A:1001**); no separate USB serial
driver is needed. Windows lists **USB Serial Device (COMx)**. Its COM
number can change between download mode and normal operation, so check
the port again after a restart.

On Linux, if the port appears but uploading fails with `permission denied`,
Debian/Ubuntu users can join the `dialout` group
(`sudo usermod -aG dialout $USER`), then log out and back in. Other
distributions may use a different serial-port access group.

### Install required libraries

Open **Sketch → Include Library → Manage Libraries** and install these,
picking the version from each library's version list:

1. **XPowersLib**, version **0.3.3** — controls the AXP2101 power manager.
2. **TinyGPSPlus**, version **1.0.3** — parses the GPS receiver's NMEA messages.
3. **U8g2**, version **2.36.18** — draws Stage 4's debug and production OLED screens.

These are the versions pinned by CI. Library Manager installs the newest
unless you select a version.

### Upload a sketch and read its messages

Before compiling Stage 4, run `python tools/stamp_firmware_version.py` from
the repository root. It combines `firmware/VERSION` with the ten-character Git
commit, for example `v0.05+3f9a1c07be`, adding `-dirty` for tracked edits.
Without usable Git, it uses the release archive's `firmware/COMMIT`; a missing,
invalid or unexpanded hash warns and uses `firmware/VERSION` alone.
There is one firmware; runtime debug mode does not change the version.
Without the generated header, Device health shows `unversioned`. Run the
script before every build; an old header keeps its old version.

A *sketch* is an Arduino program, stored in an `.ino` file inside a folder
with the same name. *Firmware* is the program installed on the board.
*Uploading* or *flashing* compiles the sketch and writes that firmware to
the board's flash memory. A *partition* is a reserved area of that memory;
Stage 4 uses a LittleFS filesystem partition as fallback when no microSD
card mounts.

1. Connect the board with a data-capable USB-C cable and open the stage's
   `.ino` file with **File → Open**. Keep it in its supplied folder.
2. Select **ESP32S3 Dev Module**, the [board settings](#board-settings),
   and the board's **Tools → Port** entry.
3. Click **Upload**, the right-arrow button (or **Sketch → Upload**).
   The checkmark only compiles; it does not install the program. Wait for
   uploading to finish before disconnecting anything.
4. Open **Tools → Serial Monitor** at **115200 baud**. Opening the port can
   reset the board, so startup messages may appear again.
5. If you missed startup, briefly press **RESET**. With Stage 4, do this
   before a test trip, not while recording.

Each upload replaces the previous stage's program. Stages 1–3 check
hardware; they do not record trips. **Leave Stage 4 installed for use in
the car.** When updating it later, keep its `partitions.csv` and leave
**Erase All Flash Before Sketch Upload → Disabled** to preserve internal
trips and settings. Reflashing does not erase trips on the card.

#### Firmware versions and releases

Versions `v0.xx` are beta; `v1.00` marks release once most firmware development
is finished. Only bump the number when preparing a build to flash for road use.
Builds between bumps show the last number plus their own commit.

For the next road build:

1. Change `firmware/VERSION` to the next number (for example `v0.06`) in its
   own commit, titled `Firmware v0.06`, and merge it.
2. On that merged commit, create and push an annotated tag:
   `git tag -a fw-v0.06 -m "Firmware v0.06"`, then `git push origin fw-v0.06`.
3. Run `python tools/stamp_firmware_version.py`, build from that commit, then flash.
4. Publish a GitHub release from the tag, titled `Supreme firmware v0.06 (beta)`,
   as a full release marked **Latest**:
   `gh release create fw-v0.06 --verify-tag --latest --title "..." --notes-file notes.md`.
   Publishing triggers the source bundle and checksum asset upload; `srv-v*` releases get the same bundle.
   Don't mark it pre-release: a pre-release can't be Latest, and the frozen
   "T-Beam V1.2 final" release would then look like the current firmware.
   Copy the previous release's notes and update them: what's new, what works
   and what's still unproven (the open `Shakedown:` issues), the build steps,
   and whether the server must be updated first. Attach no binary, because
   each owner's WiFi password and upload token are compiled in.

Keep `firmware/VERSION` to one line in the form `v<major>.<two-digit minor>`:
major 0–99 without leading zeros, such as `v0.05` or `v1.00`. The stamp script
tolerates surrounding whitespace, including LF/CRLF line endings, and stops
with an error if the file is missing or malformed. Git tags are release markers;
the build reads the file, so shallow checkouts work too.

#### If uploading fails

1. Close Serial Monitor and other programs using the port. Reconnect the
   USB cable, select the current port, and retry Upload.
2. If no port appears, try a known data-capable cable and a direct USB
   connection instead of a hub. Check the port again after restarting.
3. If automatic entry fails, follow [Download mode](#download-mode).
   The Supreme's **BOOT** button is **GPIO 0**. After uploading from
   manually entered download mode, press **RESET** if the log remains at
   `waiting for download`.
4. Keep the exact error if it still fails; check
   [Espressif's ESP32-S3 boot-mode troubleshooting](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/advanced-topics/boot-mode-selection.html).

## Firmware Stages

Work through these in order. Each stage builds on the previous one
and verifies a specific hardware subsystem before moving on.

All four stages are for the **T-Beam Supreme**. Use the
[Supreme board settings](#board-settings) and
[back up the factory flash](#back-up-the-factory-flash-first) before the
first upload. Every stage prints at 115200 baud; open the Serial Monitor
after uploading.

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
fail, Stages 2 and 3 halt; Stage 4 enters [degraded service](#startup-failures).
Recovery is host-tested; repeatedly resetting during PMU reads still needs
a bench check. Stage 4 retains the boot result
and has a host-tested helper for E2007 PMU_STARTUP_RECOVERY (#151). Stage 4
now queues that notice in its diagnostic journal after storage startup;
failed startup is now reported by the degraded loop (#126). OLED reporting
is implemented for normal operation (#127); the panel stays off in degraded mode.

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
`u` a legacy UBX config test (Stage 4 uses VALSET instead), `e` GPS_EN,
`b` VBACKUP, `3` LoRa rail (needs the antenna attached and a confirmation),
`r` raw NMEA, `h` help.

What good looks like outdoors: a cold start in about 33 s, a hot start in
about 2 s, and `g` showing 19 or more satellites heard at 40+ dBHz with
the AGC around 30%. `heard=0` with the AGC stuck near 80% means no signal
reaches the receiver: check that the GPS cable is clicked onto the **GPS**
socket.

### Stage 4: The trip logger

For why it works this way, see [the firmware design](DESIGN.md).

The `04_trip_logger/` sketch detects trips, accumulates mileage and writes
GPX files to the microSD card, with internal LittleFS as fallback. Its
first real uploads from microSD succeeded on 2026-10-21 (UTC): four trips
received HTTP 200, were deleted from the card and appeared in the viewer.
This used main commit `a358eaf`'s earlier Stage 4 debug firmware, flashed at
20:39Z on 2026-10-20 with real settings and one-time diagnostics enrollment.
A trip upload with debug off also succeeded, on the former production build
`v0.05+615a13ffae` (the server-down test below). `v0.06` has so far uploaded
diagnostics, but no trips yet. Since v0.06 there is one firmware build: debug
mode starts off at every power-on and can be toggled by holding BOOT and
tapping POWER. On 2026-10-21 (UTC), with `v0.05+615a13ffae`, ingest was
stopped with a test trip queued. Both upload attempts returned `HTTP -1` (connection failure),
and the trip stayed queued. The next retry after ingest restarted received
HTTP 200 and deleted the local file. LittleFS uploads remain untested on
the Supreme.

USB insertion powered the board on at 04:14:50Z on 2026-10-21. Boot was
clean, GPS configured on attempt 1, and the FAT32 card had 29.71 GiB free.
The journal reported `sd, 115 unacknowledged records`. The new trip started
at 04:15:21Z. At home, between 04:32:15Z and 04:32:27Z, three older,
finished trips uploaded while GPS logging continued. Upload order follows
the card's directory order, not trip number: `t000001.gpx` (16 KB, 0.8 s),
then `t000005.gpx` (176 KB, 9.1 s), then `t000004.gpx` (158 KB, 3.1 s).
Each line read `Upload /trips/<file> (<bytes> B, <seconds> s) -> HTTP 200 (deleted)`.

The current trip finalized at 04:35:09Z after three minutes below 2 mph:
`Trip finalized: /trips/t000006.gpx (11.40 mi)`. It uploaded at 04:36:41Z
(142 KB, 2.7 s, HTTP 200, deleted). Ingest logged acceptance and HTTP 200
for all four trips, and stored each as `data/YYYY-MM/<trip>.gpx` plus
`.json`. The viewer listed all four with distances; the new drive was
11.40 mi on the board and 11.41 mi on the server.

At boot it uses the [Supreme pins and rails](#supreme-pin-and-power-reference):
it verifies LoRa power off first, switches SD power and VBACKUP off, and
enables the GPS rail. Storage setup then powers and mounts the card. It
chooses its trip store once per boot and attempts uploads from both mounted
stores.

Before flashing:

1. **Create `secrets.h`.** From the repo's top folder, copy
   `firmware/secrets.h.example` to `firmware/04_trip_logger/secrets.h`,
   next to the `.ino` (the build won't find it anywhere else), and fill
   it in. The [source settings table](#source-build-settings) explains each value.
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
     can't upload until you edit `secrets.h` and flash again. Trips wait
     on the board; use the Serial Monitor and [screen](#the-screen)
     to investigate failures.
2. **Partitions are handled for you.** The sketch's `partitions.csv` gives
   a 1.5 MB app partition and 2.375 MB internal LittleFS fallback, with no
   over-the-air firmware update slot (no OTA). It overrides **Tools →
   Partition Scheme**. Internal fallback holds roughly 5–6 hours of driving;
   the [microSD card](#microsd-card) holds far more. A full trip store ends
   the current trip and prevents another until space is freed.
3. **Prepare diagnostics enrollment.** Follow [deployment Step 8](../DEPLOY_DEBIAN.md#step-8--point-the-firmware-at-it)
   to read the server's snapshot revision and set the two one-use enrollment
   defines. A snapshot is the board's current diagnostic state; its revision
   must be newer than the server's saved value. GPX delivery does not depend
   on this enrollment, but diagnostics stay local without revision authority.
4. **Stamp the version before every build.** Run
   `python tools/stamp_firmware_version.py` from the repository root to record
   `firmware/VERSION` plus the Git commit (for example `v0.05+3f9a1c07be`).
   See [Firmware versions and releases](#firmware-versions-and-releases) for
   road-use version bumps and tags.
5. **On a new board, erase once as described below.** The factory firmware
   can leave data where Stage 4 expects its internal trip partition.

#### Source build settings

| Setting | What goes in it |
|---|---|
| `WIFI_SSID` | Your home network's name, exactly as it appears, capitals included. It has to be 2.4 GHz, and not hidden: the board only uploads when it sees this name in a WiFi scan. |
| `WIFI_PASSWORD` | Its password. If the password has a `"` or `\` in it, write them as `\"` and `\\`. |
| `SERVER_HOST` | Only the server's IP address or hostname, from `install.sh` on the server. Do not include `http://`, a port, or `/upload`. |
| `SERVER_PORT` | `8090`. |
| `AUTH_TOKEN` | The token from `install.sh` on the server. It has to match `INGEST_AUTH_TOKEN` in the server's `.env` exactly, or every upload is refused. |
| `DEVICE_ID` | A name for this board. The default, `tbeam-01`, is fine. |

Leave `USE_BSSID_FILTER` at 0 unless you want to restrict uploads to one
router; the example explains it. Keep `secrets.h` private and gitignored.

#### First flash on a new board

Back up the factory flash before installing any project sketch. For the
**first Stage 4 upload on a Supreme**, use its bundled `partitions.csv`
and set **Tools → Erase All Flash Before Sketch Upload → Enabled** once.
After that upload, immediately set it back to **Disabled**. The factory
firmware leaves data in the area used for the trip partition, and Stage 4
will not overwrite non-erased data merely because it cannot mount it.

The standalone `install/flash.ps1 -FirstInstall` / `install/flash.sh --first-install`
performs the same one-time erase after a verified factory backup and an exact
`ERASE` confirmation, then writes the four firmware parts. Use the default
app-only update (or cleanup) for a tracker already running this project.

**Erase-all also wipes the board's settings memory (NVS):** the trip
counter (`tripno`), diagnostic journal boot counter and consumed enrollment
ID (`diag/enroll`), among other settings. If `DIAGNOSTICS_ENROLLMENT_ID`
and `DIAGNOSTICS_STATUS_HIGH_WATER` are still defined in `secrets.h`, the
next boot can consume that ID again and re-enroll from its possibly stale
high-water value. Follow [deployment Step 8](../DEPLOY_DEBIAN.md#step-8--point-the-firmware-at-it),
then remove both defines after the first successful diagnostics upload and
reflash with erase disabled. If you must erase an enrolled board later,
read a fresh server high-water and choose a higher enrollment ID first.

Watch the Serial Monitor. If LittleFS mounts, startup uses it normally.
Otherwise the firmware reads **every byte** of the `spiffs` data partition.
Only when every read succeeds and every byte is erased (`0xFF`) does it
format that partition once and mount it again. Successful provisioning
prints:

```text
LittleFS partition is fully erased; provisioning trip storage.
...
Setup complete. Logging trips.
```

No sketch edits or second flash are needed for provisioning. Any non-erased
byte, failed read or missing partition leaves the data untouched. A failed
mount prints `ERROR: LittleFS mount failed. Storage preserved.` followed
by the recovery pointer. A failed format or subsequent mount prints
`ERROR: LittleFS provisioning failed.` An interrupted format can leave
non-erased bytes, so the next boot preserves them rather than formatting again.

If the card mounts, recording can continue there even when LittleFS fails.
If **neither store works**, startup enters [DEGRADED service](#startup-failures):
no trips or networking, but power management and the charge guard remain
active when the PMU is trusted. Follow [storage recovery](#if-the-trip-storage-wont-mount-later);
do not enable format-on-mount-failure.

Later uploads with erase **Disabled** preserve trips on the card and in
internal flash, along with NVS and trip numbering. Before changing the
partition layout or erasing flash, upload or independently back up every
trip. Erase-all does not erase the removable card, but it does reset NVS.

#### If the trip storage won't mount later

For microSD trips, switch the board off and read or copy the card on a PC.
Check its seating and FAT32 format before trying it again; the firmware
never formats it. Preserve its data before any repair or reformat.

For internal flash on a board that's been working, `LittleFS mount failed`
doesn't mean the trips are gone. Keep format-on-mount-failure disabled. Check that you
flashed with this sketch's `partitions.csv`, and try the firmware that last
worked. Before anything that could erase the partition, copy it to a file
with esptool (it comes with the ESP32 core, or `pip install esptool`):

```
esptool --chip esp32s3 --port <port> read-flash 0x190000 0x260000 trips-backup.bin
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
esptool --chip esp32s3 --port <port> erase-region 0x190000 0x260000
```

Older esptool versions spell it `erase_region`. The address and size are
for this project's bundled `spiffs` partition only. Reset the board (or
power-cycle it) after a successful erase; the unchanged firmware verifies
the erased partition, provisions it, and reaches `Setup complete. Logging
trips.` No second flash or format-on-mount-failure build is needed. If it
still enters degraded service, investigate the reported error rather than
repeating erases.

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
  older points spill to the selected trip store, and the remaining tail
  is flushed before the next timed fix or at trip end, always without invented times.
  System-clock synchronization and local file naming are independent.
  The viewer lists a wholly untimed trip without a date.
  A partly timed trip is marked **Partial timing**; its full-trip dates,
  duration and average speed remain unknown while its distance is retained.
  GPS segment boundaries stay attached to the first fix after each gap,
  including fixes buffered before UTC.
- **Upload logic:** the board checks for finished
  trips every 30 s and scans for your home WiFi when a retry is due and a
  complete trip is waiting.
  Incomplete files are preserved for recovery and do not trigger radio work.
  Failed scans, connections, or
  uploads double the retry wait (60 s, 120 s, 240 s, then at most 5 min).
  The wait starts after the attempt finishes; the next 30 s check runs it
  once due. A successful upload resets the wait to 30 s, and an empty
  queue clears it. A trip ending clears it too, since the car may just
  have parked at home: the board tries within 30 s, then about 1, 3 and
  7 minutes later, within its normal battery upload window. Each failed
  try prints why and when the next one is, e.g.
  `Network: home WiFi not found "<ssid>" -- next try in 60 s.` When connected, it
  POSTs each trip to the ingest server as GPX and deletes it locally once
  the server accepts it. GPS
  parsing runs on core 1; networking runs on core 0 and never blocks
  it. On 2026-10-21 (UTC), absent home WiFi produced retry waits of
  60, 120, 240, 300 and 300 s. Once home, four SD trips received HTTP 200
  and were deleted locally. A scan at 04:35:35Z missed the access point;
  its 60 s retry led to the current trip's upload at 04:36:41Z. A scan
  miss at the edge of range can delay an upload; it need not mean the
  settings are wrong. Server-down retry was also verified on 2026-10-21
  (UTC): two `HTTP -1` failures kept the trip queued, then HTTP 200 after
  ingest restarted deleted the local file. Server errors (401/413/5xx)
  described here are code behaviour, still untested on the Supreme.
- **Power loss:** after USB power has been absent for 10 s, the firmware
  finalizes the current trip. Once idle on battery for 10 minutes, it
  closes files, stops networking, seals the diagnostic journal, unmounts
  storage and calls `PMU.shutdown()` to switch the board off completely.
  It does not deep-sleep. The next USB power insert switches it on again.
  An upload still running at 10 minutes gets up to **16 more minutes** to
  finish; a file cut off then remains queued. This is the opportunity to
  upload at home, subject to the pending end-to-end test. Without an 18650,
  the board loses power with the car and has no battery upload window or
  clean shutdown. A car USB port that stays powered never triggers this
  power-off. Unreadable USB-power state keeps the board awake for retries;
  it never counts as confirmed USB loss or permission to power off.
  Persistent read failures on battery leave the PMU's hardware
  under-voltage cutoff as the final protection.
- **Buttons:** on battery, hold **POWER** for a second or two and release
  well before 6 s for a clean shutdown: finalize the trip, stop networking,
  seal the journal and unmount storage before switching off. With USB
  plugged in, the request is ignored and Serial prints
  `VBUS present -- staying awake instead of powering off.` An unreadable
  USB state also keeps it awake and prints
  `VBUS state unreadable -- staying awake instead of powering off.`
  USB switches the board on only when power arrives: shutting it down
  while USB remained connected would leave it off until power was cut and
  restored. Unplug USB first to switch it off. Press POWER for about 1 s
  or insert USB power to switch it back on. A 6 s hold forces the PMU's
  hard power-off with no file closing; any open trip needs boot repair and
  unwritten diagnostics are lost. Use that only if the board stops responding.
  A short POWER press alone requests a status card in either mode. While
  running, hold BOOT then tap POWER to toggle debug; it starts off at every
  power-on. BOOT held during power-up still means download mode.
  See [The screen](#the-screen).
- **Charging:** the firmware pauses charging at high or low temperatures
  reported by the AXP2101 power-management chip. Its temperature is an
  indirect estimate using a 9 °C die-temperature offset measured on the
  retired V1.2; recalibration for the Supreme is still pending. Debug builds
  log the readings it needs (the `Temp:` line under [The screen](#the-screen)). It is not
  a measurement of the battery cell or a guarantee of safe charging in a
  hot car. Logging continues while charging is paused.
  The guard verifies every charger on/off command with a checked register read
  and retries unconfirmed commands every 30 s; E2008 means three attempts failed
  to confirm the requested state, so charging may still be on or remain off.
- **Storage full:** the selected trip store (microSD or internal flash)
  must have at least 64 KiB free to start a trip; below 24 KiB it ends
  the one in progress. For LittleFS, free space excludes the unused part
  of the separate 64 KiB diagnostics reserve. Card free space uses the
  card's own total and used bytes. Accepted uploads freed space by deleting
  the local SD files on 2026-10-21 (UTC). The same deletion path for
  LittleFS uploads remains untested on the Supreme.

Watch the Serial Monitor (115200) for state transitions:
`Trip started` → (drive) → `Trip finalized`, then
`Upload ... -> HTTP 200 (deleted)` if the server accepts it. On
2026-10-21 (UTC), the serial log showed
`Upload /trips/t000006.gpx (142443 B, 2.7 s) -> HTTP 200 (deleted)`.
The local SD file was deleted, and the trip appeared in the viewer.

Server-down retry was verified on 2026-10-21 (UTC): the trip stayed queued
after two `HTTP -1` failures and uploaded with HTTP 200 after ingest
restarted. Server errors (401/413/5xx) below describe the code and remain
untested on the Supreme.
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
connection states, not a definitive diagnosis. Press **RESET** to try again
at once. See the
[troubleshooting steps](#check-recording-and-uploads)
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

#### Startup failures

`DEGRADED:` Serial lines mean trips are **not being
recorded**. Without SD or LittleFS, the board keeps the charge guard, power
key, low-battery shutdown and ten-minute battery idle power-off active. If
the power chip is absent or its LoRa rail cannot be verified off, it avoids
ordinary PMU control (one best-effort charge-disable attempt for an answered
but untrusted chip); only internal-flash diagnostics and retries remain.
Neither mode starts GPS processing or networking. Retries wait 60 seconds,
double up to ten minutes, and restart on recovery. Check the Serial reason,
SD seating/FAT32 and BLDO1 supply for storage failures; check board variant,
power and Wire1 SDA 42 / SCL 41 for PMU failures. Do not assume charge control
works in a PMU-failure mode. Restore the fault and verify a recorded test trip
before relying on the tracker; see [degraded startup details](../DIAGNOSTICS.md#degraded-startup-service-126).

#### RTC

The PCF8563 real-time clock (RTC) supplies diagnostic event timestamps
before GPS/SNTP sync, never GPX times. The `RTC:` boot line reports whether its clock is
usable and, when known, how long ago it was set. `invalid (stopped/VL)` is
normal after the battery was removed; trusted GPS or network time (SNTP)
repairs it with one verified setting per boot. It keeps counting while the board is
switched off (checked on the bench, 2026-10-20) and runs about 10 s/day
slow, so a reset corrects it only when it's off by 2 s or more.

#### GPS settings

Stage 4 sends one UBX-CFG-VALSET (`06 8A`) at every
boot to the receiver's RAM layer, without saving to battery-backed RAM
or flash. It explicitly enables UART1 RMC, GGA and NAV-PVT at rate 1;
disables GLL, GSA and VTG; enables GSV every 5 epochs in all builds;
and selects the automotive dynamic model. The
navigation rate stays at the default 1 Hz. GGA supplies altitude, HDOP
and satellite count; NAV-PVT supplies speed/horizontal accuracy used
before recording movement.
E3009 means high receiver gain and no usable-strength satellite persisted
for two minutes, suggesting a loose or damaged GPS antenna cable; check the
plug inside the case and the cable. It clears on lower gain or a strong
satellite, including after reconnecting indoors; the thresholds come from
one board and antenna, so the warning is a prompt to inspect them.
The key IDs and one-byte values come from the [M10 SPG 5.10 interface
description (UBX-21035062 R03)](https://content.u-blox.com/sites/default/files/u-blox-M10-SPG-5.10_InterfaceDescription_UBX-21035062.pdf),
sections 3.10.5.1, 4.9.11 and 4.9.12. The bench receiver still ACKs
legacy CFG-MSG and CFG-NAV5, but VALSET is the future-proof interface.
After a cold power-up the MAX-M10S can emit NMEA before accepting UBX.
The logger waits for checksum-valid NMEA (up to 3 s per attempt), then
configures in the background while continuing to parse fixes. Each send
waits 2 s for an ACK: at 9600 baud (8N1, about 960 bytes/s), a ~1 KB GSV
burst takes ~1.1 s, with more time needed for other queued NMEA,
NAV-PVT (~100 bytes/s), and the ACK. Nothing is resent within that window.
Silence permits up to three sends per attempt; a NAK ends that attempt.
Failed attempts wait 8 s before retrying, up to five attempts. Success
logs `GPS configured on attempt N (... ms after config start)`; final
failure warns and continues with current settings, without a legacy
command fallback. The ten-second NAV-PVT fallback wait starts at success
or final failure (see [normal operation](#normal-operation)). Replies during
backoff, after the ACK deadline, or begun before the current send are
ignored. ACKs have no attempt ID: a complete old reply first arriving
after another identical VALSET send cannot be distinguished, but it
acknowledges the same complete set of settings.

#### The screen

There is one Stage 4 firmware. Debug is **off at every power-on** and is
never saved. While the board is running, **hold BOOT, then tap POWER** to
toggle debug. A two-second `Debug ON` / `Debug OFF` message and serial
`Debug mode: on` / `off` confirm the change. Holding BOOT while powering
up still selects download mode. Long POWER presses keep their usual meaning.

Debug off uses the everyday cards below. Debug on shows a live page that
redraws about once a second and enables `GPS RF:`, `Temp:`, heap and other
debug serial output. POWER alone shows the normal status card in either
mode; the live page resumes when that card expires. Turning debug off
returns to production power save after the brief feedback, unless a fresh
card is due; old cards and errors are not replayed. The toggle and shared
panel handoff still need bench verification. Illustrative debug values:

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

- With debug off, the production screen shows a status card for 30 seconds
  at every normal power-on, within a couple of seconds and without waiting
  for a GPS fix. A dark screen at power-on therefore means something is
  wrong (see E6001 in `DIAGNOSTICS.md`), not just that the GPS is still searching.
  It shows `Sats: N` for satellites **heard** (`Sats: --` until heard or when
  stale), then `OK` or only active warning-and-above `E####` codes. The satellite
  count and codes update while any status card is visible, without extending
  its timer. After a trip, the status card still waits until parked.
  The boot card does not repeat just because a trusted fix later confirms
  the board is parked. Routine status cards turn off when moving is detected;
  an unknown motion state does not turn them off. Codes are severity ordered and
  deduplicated. Four fit in a two-by-two grid; with more faults, three codes
  and `+N` show how many were omitted. Error cards show only a centred code.
  Production does not render catalog OLED text; catalog v5's text
  validation rules still apply. New
  ERROR/CRITICAL episodes show once even while moving; unresolved faults
  remind at the next parked opportunity. Cards last 30 seconds at maximum
  contrast, then SH1106 power save turns the panel off until another card is due.
  Degraded startup still leaves the screen off. `DISPLAY_CARD_MS`
  sets this duration in milliseconds (default `30000`, build override
  `-DDISPLAY_CARD_MS=10000` for 10 seconds). The default is planned to return
  to 10 seconds after the first road tests. A short press of
  **POWER** shows a status card in any motion state after a verified PMU
  INTSTS1..3 batch, outside degraded mode. The short-key IRQ is always enabled;
  BOOT is sampled and debounced on the loop task. Long press keeps its
  existing clean power-off behavior on battery and is ignored on USB.
  GSV/MON-RF antenna checking and NAV-PVT speed accuracy gating stay enabled
  in both runtime modes. On **2026-10-20**, the owner saw the working production
  screen on hardware and requested much larger text filling the panel,
  without descriptions. The revised layout uses U8g2 `logisoso26_tr`
  for sats/OK/a single status code, `helvB14_tr` for multiple status codes,
  and `logisoso38_tr` for error cards. Real font data gives `Sats: 99`
  a 123-pixel advance (119x27 ink), status-grid `E9999` 53 pixels
  (50x14 ink), and error `E9999` 121 pixels (116x38 ink).
  It keeps two-pixel panel margins and separated rows; `+N` fits through
  two digits. Full metrics, pixel positions and unchanged policy are in
  [DIAGNOSTICS.md](../DIAGNOSTICS.md#production-fonts-and-pixel-layout).
  The revised font layout and current consumption still need bench acceptance.
  The production screen has not been road-tested.
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
- Debug builds also print a `Temp:` line every 30 s: the guard's PMU die
  temperature with sample age and its die-minus-9 C estimate (the retired
  V1.2 calibration; samples older than 35 s show `--`),
  TS voltage/raw ADC count, BME280 temperature, actual charging/phase,
  battery voltage/percent and VBUS. Unavailable values show `--`. Compare
  TS voltage across temperatures to identify whether the Supreme's TS
  resistor is fixed (flat) or a thermistor (temperature-dependent); TS stays
  independent of the charger, with a 50 uA measurement bias. BME280 uses
  temperature-only forced mode on the OLED's core/bus; its ALDO1 supply
  (also OLED/magnetometer) is set to 3.3 V and enabled at trusted PMU startup.
  If that setup cannot be confirmed, boot continues with a serial warning;
  the display or sensors may be unavailable. These readings do not change
  the guard's offset or thresholds. The line and probes are debug-only.
- The screen can't stop logging. If it's missing or an I2C write fails,
  the serial log says `WARNING: debug OLED I2C failed` once and the
  screen stays off until the next boot.
- It uses maximum SH1106 contrast (`DEBUG_OLED_CONTRAST = 255`). The old
  setting (`16`) was unreadable outdoors on the first road test
  (2026-10-19). Full contrast speeds OLED burn-in and draws a little more
  current, acceptable for this temporary shakedown-only page. It shifts
  sideways a few pixels each minute to limit burn-in and switches off
  before the board powers down. The production display has its own
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

#### Expected boot log

Illustrative; free space, attempt count and timing vary:

```text
=== Stage 4: Trip Logger ===
LoRa rail (ALDO3) verified off; SD rail (BLDO1) off; VBACKUP off.
PMU power-on source 0x04, power-off source 0x02.
PMU ready, MAX-M10S GPS rail (ALDO4) on at 3.3 V, charge to 4.1 V. VBUS present.
Trip storage: microSD (29.72 GiB free); internal flash queue mounted.
GPS: waiting for checksum-valid NMEA (up to 3 s).
Setup complete. Logging trips.
GPS ready: checksum-valid NMEA received.
GPS configured on attempt 1 (1500 ms after config start): RMC+GGA+NAV-PVT, GSV every 5 epochs, automotive model.
```

> CI compiles this sketch for the Supreme on **ESP32 core 3.3.11**, with
> **XPowersLib 0.3.3**, **TinyGPSPlus 1.0.3** and **U8g2 2.36.18**, in both
> display modes. Its FQBN is
> `esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled`.
> Host tests run with `bash firmware/host_test/run.sh`. Compile and host
> checks do not establish hardware durability or live upload headroom. The
> real SD upload and network stack measurement above and below cover the
> debug build's successful-upload path; heap and failure-path checks remain.

### Diagnostic journal module

The first real diagnostics delivery succeeded on 2026-10-21 (UTC), using
the debug build and SD journal. After the last GPX, two
`POST /v1/device-events` requests returned HTTP 200 at 04:36:43Z and
04:36:44Z. The server created `data/diagnostics/events.db` with all 115
pending events. Device health showed recent contact and snapshot receipt,
storage backend `sd`, zero dropped events, a boot counter and no active
conditions. History included the earlier E3009 antenna episode, raised
and cleared; E5001's boot-local access-point-absence counter was 6.
Successful diagnostics uploads print nothing on serial. Confirm them in
the ingest log or Device health. That build showed `Firmware: Not reported`
because it predates version reporting; stamped builds report their version
number plus Git commit (see step 6). The behaviour below describes
the implementation; LittleFS uploads and server errors (401/413/5xx)
remain untested on the Supreme. Trip uploads with debug mode off and
server-down retry were verified separately (see [Stage 4](#stage-4-the-trip-logger)).

Stage 4 carries a board-independent `event_log.h` / `event_log.cpp` module with
a fixed producer queue, CRC-protected rolling segments, durable acknowledgment
metadata, SD/LittleFS/RAM fallback and host fault tests. Stage 4 now uses it
for boot history: E1000 records the reset reason, E1003/E1004/E1005 add
watchdog/brownout/panic history, and E2007 records PMU recovery or startup
retries. Clean PMU startup produces no E2007. Unknown reset reasons use `other`.
Every boot gets a fresh random identity and one verified NVS boot-counter
increment; NVS failure leaves a fresh identity with an unavailable counter.

The journal uses flat `/diag/seg-00000001.open` files, sealed as `.ready`,
with a 16 MiB SD quota. If SD cannot accept a record, it tries mounted
LittleFS within a 64 KiB reserve, then retains the bounded RAM queue. The
LittleFS trip start/stop guards protect the unused part of that reserve;
existing diagnostic allocation is already counted as used space. No trip is
deleted to make journal space, and diagnostics never mount or format media.
RAM records are lost on reset or power-off.

The existing core-0 network task drains one record per service slice, yielding
the filesystem lock to trip writes and uploads. It also handles shutdown
drain/sealing before the existing storage unmounts. Debug builds print one
boot line naming the active backend and unacknowledged record/segment counts;
an incomplete inventory is labeled. There is no extra task. Runtime hooks now
cover GPS data/fix loss and VALSET failures, unavailable UTC, the PMU's existing
battery warning levels, charge-guard pause/recovery, SD fallback, recording
space and trip-write failures, boot trip recovery, WiFi joins, upload status
classes, low network stack and debug OLED I2C failure. Conditions raise once,
repeat at most every 15 minutes and clear on observed recovery; ordinary driving
away from home increments E5001's boot-local counter without a journal event.
See the [hook list](../DIAGNOSTICS.md#stage-4-runtime-fault-hooks-124-part-b)
for exact thresholds, context fields and recovery semantics. Events and producer
state are bounded; a full/contended queue can lose records and counts those drops.
Both cores have static builders, so no event-sized buffer is added to their stacks.
After GPX batches, the existing network task sends diagnostics to
`POST /v1/device-events` using the same server, bearer token and `DEVICE_ID`.
Each request carries at most 64 events and a current snapshot, including E5001's
counter, within 32 KiB. Old history keeps its original boot identity. Trips win
before each request; hold/abort and an absolute ten-second transport deadline
stop a drain. Exact validated 2xx acknowledgments are required before journal
reclamation. Failures have separate 60-second-to-one-hour backoff from GPX;
401 starts at one hour. Rejected batches narrow before a singleton can enter
the bounded inspection slot, with durable loss accounting. Heartbeats use an
existing awake opportunity after 24 hours without successful server contact;
a new boot with unknown contact age checks in conservatively. They never wake
the board.

**Enable snapshot delivery deliberately:** read this device's current
`devices.status_sequence` in the ingest diagnostics database (zero only for a
verified new identity), using [deployment Step 8](../DEPLOY_DEBIAN.md#step-8--point-the-firmware-at-it).
Then uncomment/set `DIAGNOSTICS_STATUS_HIGH_WATER` and
`DIAGNOSTICS_ENROLLMENT_ID` in private `secrets.h`. The enrollment ID must be
positive and greater than any previously used on this board. NVS consumes it
before restoring the SD allocator; a failed enrollment requires a higher ID.
After the first successful diagnostics upload, remove both defines and
reflash with erase disabled. Existing SD metadata works on later boots
without them. The first real test consumed enrollment ID 1 after the
deployment Step 8 command returned high-water 0 (no `events.db` yet).
Re-enrollment needs a fresh server read and an ID of at least 2, higher
than every ID already used. Never reuse a fresh-identity zero after
changing/corrupting cards: inspect the server again.
Without an enrolled writable SD revision authority, events remain local and
GPX upload logic remains available independently of diagnostics enrollment.
See [revision enrollment and recovery](../DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery)
for the complete procedure and snapshot-conflict recovery.

The upload batch (35,280 bytes), request (32,769), snapshot (6,144), reply
(8,193) and two observation arrays (1,536 each) live in one 85,464-byte PSRAM
workspace on ESP32-S3, including alignment padding. Setup allocates it once,
before the network task starts, with PSRAM-only heap capabilities; it is never
freed, retried or replaced with internal RAM. If PSRAM is unavailable or the
allocation fails, one `WARNING:` disables diagnostics upload for that boot;
local journaling and GPX upload logic remain available. No existing catalog
code describes this allocation failure, so no event code is substituted.
Small controls, producer builders and cross-core atomic publications stay in
internal RAM. Workspace accesses run only in setup or the core-0 worker, never
in the PMU ISR, DNS callback or cache-disabled code. Journal flash operations
use internal frame/metadata scratch, and ESP-IDF suspends tasks during its
cache-disabled flash operations. The network stack remains 12 KiB.
Debug mode prints `Heap: internal ... free (largest ...), PSRAM ... free`
on entry and once after the first diagnostics HTTP attempt made with debug on,
whether it succeeds or fails. Record those lines during #6's real-upload
checks; host tests and compile sizes are not measurements of live heap pressure.
Earlier core 3.3.11 builds with PSRAM enabled used 108,356 bytes of static
RAM in debug and 84,388 in release, down from 193,804/169,836 with internal upload
buffers. Each build recovers 85,448 bytes and is 2,736 above its pre-upload
baseline (105,620/81,652). Cross-core publications and producer/control state
account for the remaining internal-memory overhead.
Compiler frames are 432 bytes for the HTTP wrapper, 672 for `nextBatch`, 240
for acknowledgment and 1,088 for singleton isolation, before callees. These
figures supplement, but do not replace, the real-upload watermark measurements
below. Degraded startup (#126) and production OLED code cards (#127)
are implemented. Failed PMU startup attempts a non-formatting LittleFS
mount for diagnostics; failure of both trip stores uses the bounded RAM
journal. Neither degraded mode starts networking, and the production panel
stays off there. SD/FAT power-cut safety and operation latency still need
hardware validation.
See [DIAGNOSTICS.md](../DIAGNOSTICS.md#eventlog-module-122-implementation)
for the API, on-disk format, quota and recovery rules. Run
`bash firmware/host_test/run.sh` for fault tests and the exported-event check
against the server's stdlib protocol validator.

### Network stack shakedown (issue #6)

This section is for validating the firmware on hardware. You don't need it
to build or use the tracker.

The network task reserves 12,288 bytes; issue #6 records how much stays free. During
and after the real SD uploads on 2026-10-21 (UTC), the debug build's
minimum free stack was **7,436 bytes**, with GPS logging active. This
measures the successful-upload path; it does not establish headroom for
server errors, server-down retries, LittleFS uploads or debug mode off.
Trip uploads with debug mode off and server-down retry were verified
separately; those results do not establish their heap/stack headroom.
Serial diagnostics report the task's lifetime minimum free stack at startup,
after each GPX HTTP upload (including failures), and after each network cycle.
Diagnostic requests sample the watermark silently between batches.
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
less than 2,048 bytes raises E1007 and means the allocation needs review; this
margin follows #6 and leaves room to investigate before exhausting the stack.
The lifetime watermark cannot increase, so E1007 normally remains active until
reboot (the next boot starts unknown). Absence of a warning
is not proof that every path has been exercised. Keep issue #6 open until these measurements are
recorded and the final allocation is justified. Host tests check diagnostics
only and cannot measure ESP32 stack use.

### Check recording and uploads

Open **Tools → Serial Monitor** in the Arduino IDE and set its speed to
**115200** baud. With the board in the case (screen on the left, USB-C on
the right), **RESET** is the left of the close pair of buttons along the
bottom edge; press it to see the startup messages again. Opening the
Serial Monitor can itself restart the board, so messages may show twice.
Do this before a test drive, not during a trip.

1. After flashing, look for these lines:
   - `Trip storage: microSD (... GiB free); internal flash queue mounted.`
     Trips go to the card (the free-space figure depends on your card). If it says `WARNING: microSD unavailable -- using
     internal flash`, the card didn't mount: switch off, check it's FAT32 and
     seated, and switch on again.
   - `Setup complete. Logging trips.`
   - A line starting `DEGRADED:` means the board is **not recording trips**,
     because neither storage worked or the power chip didn't answer. It
     retries by itself; the message says why. See
     [Stage 4 on the Supreme](#stage-4-the-trip-logger).
2. Take it for a drive of a few minutes, powered from a laptop with the
   Serial Monitor open. (Anything under 30 seconds or a tenth of a mile is
   thrown away as noise.) GPS doesn't work indoors, and the first fix after
   the battery was out can take a minute outside. After 10 seconds above
   5 mph you'll see `Trip started`.
3. Park at home and wait. Three minutes below 2 mph ends the trip
   (`Trip finalized`). The board then tries your WiFi and uploads it:
   `Upload ... -> HTTP 200 (deleted)`. The file is deleted from the board
   only after the server has accepted it. On 2026-10-21 (UTC), the trip
   finalized at 04:35:09Z and uploaded at 04:36:41Z. A scan at 04:35:35Z
   missed the access point and scheduled a retry in 60 s, so allow time
   for retries at the edge of WiFi range. Older finished trips can upload
   while the current trip is still recording; this happened on that drive.
4. Open the viewer at `http://<host>:8091/` and log in. The trip should
   be on the list. If you enrolled diagnostics, **Device health** shows
   when the board last checked in and any active error codes.

Server-down retry was verified on 2026-10-21 (UTC): two attempts returned
`HTTP -1`, the trip stayed queued, and the next retry after ingest restarted
received HTTP 200 and deleted the local file. Server errors (401/413/5xx)
below describe the code's behaviour and remain untested on the Supreme.

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
  [firmware status notes](#normal-operation) for details.
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

### Put it in the car

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
  the trip, stays on battery for 10 minutes to upload (up to 16 minutes more
  if an upload is still running), then switches itself off completely. Unplugged from USB, on your desk say, it does the same:
  10 minutes, then off. It hasn't died; plug USB back in, or press
  **POWER** for about a second, to switch it on. On battery, holding
  **POWER** for a second or two (let go well before 6 s) switches it off
  cleanly. **With USB plugged in, that press is ignored**, because USB
  power only switches the board on at the moment it arrives: a board
  switched off with USB still in would stay off until the car's port next
  went off and on. Holding **POWER** for 6 s cuts power without closing
  anything first; use it only if the board stops responding.
  [trip logger guide](#stage-4-the-trip-logger) has the
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

## Legacy T-Beam V1.2

**For reference only; do not use with the Supreme.** The T-Beam V1.2 is
retired and unsupported on `main`. Its last firmware and full setup
instructions live on the frozen [`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2),
which receives no fixes or backports.

- **Board:** LilyGO TTGO T-Beam V1.2 (ESP32 + SX1262 LoRa + NEO-M8N GPS)
- **PMU:** AXP2101 (NOT AXP192 — older code examples need updating)
- **GPS:** u-blox NEO-M8N (GPS/GLONASS/BeiDou, IPEX antenna)
- **LoRa:** SX1262 at 915MHz (unused in this project)
- **Battery:** 18650 cell in the onboard holder. Needed in practice: it
  runs the legacy firmware after the car turns off
- **USB:** Micro-USB via CH9102F serial converter

**Controls and connectors (V1.2 only):**

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

**V1.2 USB driver:** Windows uses the CH9102 driver; see
[Adafruit's CH9102 driver instructions](https://learn.adafruit.com/how-to-install-drivers-for-wch-usb-to-serial-chips-ch9102f-ch9102/windows-driver-installation).
Device Manager lists `USB-Enhanced-SERIAL CH9102 (COMx)`. On macOS, first
check for a port with a data cable; if none appears, use the
[WCH macOS driver linked by LilyGO](https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html).
Linux normally includes the driver; serial-port access permissions still apply.

**V1.2 board settings:** **ESP32 Dev Module**, **Flash Size → 4MB (32Mb)**.
Stages 1–3 on that branch use **Default 4MB with spiffs**; the legacy Stage 4
uses its bundled `partitions.csv`. Keep erase disabled to preserve stored
trips. These are not Supreme settings.

**V1.2 pin reference:**

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

The V1.2 here uses **AXP2101**, not AXP192. Older AXP192 tutorials and
examples need modification; XPowersLib supports both chips. The rails also
differ between boards: V1.2 ALDO3 powers GPS, while Supreme ALDO3 powers
LoRa and ALDO4 powers GPS. Never apply the V1.2 rail code to the Supreme.
