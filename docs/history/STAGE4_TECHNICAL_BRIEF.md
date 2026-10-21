> This is a historical record, kept as written: an original 2026 research proposal for the retired T-Beam V1.2, written before the firmware existed.
> It includes ideas never implemented or later superseded: HTTPS with Transport Layer Security (TLS), GPX 1.1/Garmin extensions, LittleFS auto-format, a car-voltage divider, V1.2 pins and deep sleep.
> It is **not build instructions** and does not fully describe the V1.2 release either. **Do not copy its wiring, pin or power-rail code.**
> For current behavior, see [the firmware design](../../firmware/DESIGN.md) and [the firmware guide](../../firmware/README.md).
> For the V1.2 release, use the frozen [`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2).
> Any earlier PR numbers, commit hashes and branch names in the proposal refer to the project's private archive and will not resolve here.
> For current status, see this repository's GitHub issues; see the [history index](README.md) for context.

# Stage 4 firmware technical brief for T-Beam V1.2 mileage tracker

**The single most important architectural decision is to run a FreeRTOS networking task pinned to core 0 while the Arduino `loop()` stays on core 1 draining NMEA.** Everything else — LittleFS durability, AXP2101 VBUS-loss handling, trip state machine — is secondary, but pairs naturally with that split. The rest of this brief gives concrete, verified APIs and code for each of your six research topics, with version caveats against esp32 Arduino core 2.x and 3.x, XPowersLib current master, and the GPX 1.1 / Garmin TrackPointExtension v2 schemas. A note upfront on your PMU rail mapping: your Stage 2 firmware uses **ALDO4 for GPS**, which works on your board, but the Meshtastic-confirmed T-Beam V1.2 mapping is **ALDO3 = GPS, ALDO2 = LoRa+OLED, DC1 = ESP32 3V3**. Verify with a meter once; if ALDO4 is actually what LilyGo wired on your unit, keep it — but the reference code below assumes ALDO3. Swap the symbol if needed.

## 1. LittleFS on ESP32 for power-loss-safe append logging

**LittleFS has been a first-party library in the esp32 Arduino core since v2.0.0 (late 2021)**, with the header `#include <LittleFS.h>` and global `LittleFS` (camel case). It sits on top of ESP-IDF's `joltwallet/esp_littlefs`. Core 3.x (September 2024, ESP-IDF 5.1/5.3) updates the underlying library with newer LittleFS bug fixes; the Arduino API is unchanged. The `LittleFS.begin()` default **looks for a partition labeled `"spiffs"`** — this matters because the stock 4MB schemes in the IDE are all named `spiffs` even though the filesystem living on them is LittleFS.

Arduino's `File::flush()` on ESP32 does issue a genuine flash commit: `VFSFileImpl::flush()` in `libraries/FS/src/vfs_api.cpp` calls both `fflush(_f)` and `fsync(fileno(_f))`, and the fsync routes through ESP-IDF VFS into `vfs_littlefs_fsync()` → `lfs_file_sync()`. However — and this is the critical gotcha — **`lfs_file_sync()` commits block writes but does not finalize the CTZ skip-list inode back into the parent directory metadata pair until `lfs_file_close()` runs.** Practical result, reproduced by multiple users (esp32.com forum t=35869): after flush-only + power loss, you can see the file still reporting its pre-open size. **Treat `file.close()` as the only guaranteed durability point for the file's new tail length.**

Performance numbers at ESP32 240 MHz with 4 MB NOR at 40–80 MHz SPI: `write()` into cache ≈ 50–300 µs; `flush()`/`fsync` ≈ **3–15 ms**; `close()` after a write ≈ **5–25 ms**; `open("a")` ≈ **3–10 ms**. A close+reopen per record therefore costs ~10–35 ms. At your ~1–2 Hz GPS rate with ~50–100 byte records, **close-every-record is comfortably within the ~200 ms loop budget**, uses <5% CPU, and gives you rock-solid crash consistency. This is what to do:

```cpp
bool logTrkpt(const char* xml, size_t n) {
  File f = LittleFS.open(currentTripPath, FILE_APPEND);
  if (!f) return false;
  size_t w = f.write((const uint8_t*)xml, n);
  f.close();                    // forces lfs_file_sync + metadata finalize
  return w == n;
}
```

For a file that grows ~100 B/sec for 30 min/day, **flash wear is a non-issue**. A 2 MB LittleFS partition is 512 erase blocks of 4 KB each; at 180 KB logical/day × ~3× write amplification = ~135 block-equivalents/day spread across 512 blocks ≈ 0.26 cycles/block/day. At 100,000 cycles/block NOR endurance, that is roughly **1,000 years to wear-out** even before considering retention (10–20 years) as the real limit. LittleFS v2's rotated metadata pair handling (present in core 3.x) further reduces hot-spot risk.

**Partition schemes for 4 MB.** The IDE does not ship a "with littlefs" option on esp32 core; you either reuse a `spiffs`-labeled partition (simplest, works verbatim with `LittleFS.begin()` defaults) or ship your own `partitions.csv`. For an OTA-capable layout (~1.25 MB × 2 app + ~1.4 MB LittleFS):

```csv
# Name,    Type, SubType,  Offset,   Size
nvs,       data, nvs,      0x9000,   0x5000
otadata,   data, ota,      0xE000,   0x2000
app0,      app,  ota_0,    0x10000,  0x140000
app1,      app,  ota_1,    0x150000, 0x140000
spiffs,    data, spiffs,   0x290000, 0x160000
coredump,  data, coredump, 0x3F0000, 0x10000
```

Since you said OTA is optional and you want maximum trip storage, **use the no-OTA layout** (~1.5 MB app + ~2.375 MB LittleFS) — this gives roughly 13 days of dense 1 Hz logging before rotation:

```csv
# Name,    Type, SubType,  Offset,   Size
nvs,       data, nvs,      0x9000,   0x5000
otadata,   data, ota,      0xE000,   0x2000
factory,   app,  factory,  0x10000,  0x180000
spiffs,    data, spiffs,   0x190000, 0x260000
coredump,  data, coredump, 0x3F0000, 0x10000
```

**Mount with auto-format and call `LittleFS.begin(true)`** so a corrupted first boot or partition relocation self-heals. Arduino-esp32 issue history to keep in mind: #6579 (flash-mode mismatch → err -84 dir corruption), joltwallet/esp_littlefs #183 (100%-full partition triggers divide-by-zero — pre-check `usedBytes()/totalBytes()` and rotate before full), and #8802 (long flash erases can starve ISRs — keep any ISR you attach `IRAM_ATTR` and avoid calling flash-backed code from it).

## 2. Non-blocking WiFi architecture: dedicated core-0 task is the right answer

Your loop budget at 9600 baud is **~130 ms before the 128-byte UART FIFO overflows**, though the software RX ring is separately sized. Of three architectural options, only one is robust in practice.

Running stock `HTTPClient::POST` **inside a FreeRTOS task pinned to core 0** is the pragmatic best choice. It preserves GPS determinism on core 1, uses only maintained in-tree code (`WiFiClientSecure` + `HTTPClient` ship with arduino-esp32), and lets you write linear blocking code inside the task — orders of magnitude simpler to audit than non-blocking sockets. The async alternatives (`AsyncHTTPRequest_Generic` and `AsyncHTTPSRequest_Generic` by khoih-prog) were **archived 4 February 2023** and have not tracked core 3.x networking changes; don't build new firmware on them. Rolling your own non-blocking sockets with `select()` means re-implementing HTTP and TLS framing, which is not worth it for this project.

The skeleton:

```cpp
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <LittleFS.h>
struct UploadJob { char path[64]; uint32_t size; };
static QueueHandle_t    gUploadQueue;
static SemaphoreHandle_t gFsMutex;
void setup() {
  Serial1.setRxBufferSize(2048);                   // MUST be before begin()
  Serial1.begin(9600, SERIAL_8N1, /*RX=*/34, /*TX=*/12);
  LittleFS.begin(true);
  gUploadQueue = xQueueCreate(4, sizeof(UploadJob));
  gFsMutex     = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(networkTask, "net", 12288, nullptr, 1, nullptr, 0);
}
void loop() {                                     // runs on core 1 already
  while (Serial1.available()) gps.encode(Serial1.read());
  tripFsmTick();
  if (fileJustFinalized) {
    UploadJob j; strlcpy(j.path, finalizedPath, sizeof(j.path));
    xQueueSend(gUploadQueue, &j, 0);              // timeout 0 -> never blocks GPS
    fileJustFinalized = false;
  }
}
```

**Stack sizing is load-bearing.** `HTTPClient` over `WiFiClientSecure` with mbedTLS needs **12,288 bytes minimum in practice**, not the Arduino default 4 KB; stack-overflow crashes at `http.begin()` on small stacks are widely reported (arduino-esp32 #566, PlatformIO forum "ESP32 Stack configuration (reloaded)"). Tune with `uxTaskGetStackHighWaterMark(NULL)` — very large cert chains may push you to 16 KB.

**Serial1.setRxBufferSize()** must be called before `begin()`; calling it after silently no-ops and logs an error (`HardwareSerial.cpp` master). The default software RX ring is 256 bytes; **bump it to 2048** for a full two seconds of slack. The internal IDF allocation is `2 × N` bytes in DRAM, so 2048 costs you 4 KB heap — cheap insurance.

**Async scan is the correct API signature for "is my home SSID in range?":**

```cpp
int16_t scanNetworks(bool async = false, bool show_hidden = false,
                     bool passive = false, uint32_t max_ms_per_chan = 300,
                     uint8_t channel = 0);
int16_t scanComplete();   // -2 not started, -1 running, >=0 count
```

Kick with `WiFi.scanNetworks(true, false, false, 120, 0)`, poll `scanComplete()` (returns `WIFI_SCAN_RUNNING = -1` until done). Reduce `max_ms_per_chan` to 100–150 to cut total scan time from ~3.9 s to ~1.5 s across all 13 channels. To avoid connecting to a coffee shop with the same SSID, **filter by BSSID (MAC)**:

```cpp
bool findHomeAP(uint8_t bssidOut[6], int32_t& channel, int32_t& rssi) {
  int16_t n = WiFi.scanComplete();
  if (n <= 0) return false;
  int best = -1; int32_t bestRssi = -127;
  for (int i = 0; i < n; ++i) {
    if (WiFi.SSID(i) != kHomeSSID) continue;
    if (memcmp(WiFi.BSSID(i), kHomeBSSID, 6) != 0) continue;  // whitelist
    if (WiFi.RSSI(i) > bestRssi) { bestRssi = WiFi.RSSI(i); best = i; }
  }
  if (best < 0) return false;
  memcpy(bssidOut, WiFi.BSSID(best), 6);
  channel = WiFi.channel(best); rssi = WiFi.RSSI(best);
  return true;
}
```

Store the known BSSID in NVS on first successful connection; for roaming mesh setups, keep a list. Then pass the BSSID into `WiFi.begin(ssid, pass, channel, bssid, true)` — this bypasses the driver's implicit scan, accelerates association, and makes impostor-SSID hijack impossible.

**`WiFi.begin()` itself does not block** — it returns almost immediately with `WL_DISCONNECTED`, and the connection completes via events (`ARDUINO_EVENT_WIFI_STA_GOT_IP` / `_STA_DISCONNECTED`). Register a handler with `WiFi.onEvent()` and drive the state machine (IDLE → SCANNING → SCAN_DONE → CONNECTING → CONNECTED → POSTING → DONE) from inside `networkTask` using `vTaskDelay(pdMS_TO_TICKS(50))` between polls. That delay only yields core 0 — **core 1 GPS traffic is unaffected**. Set `WiFi.setAutoReconnect(false)` and drive reconnection yourself; auto-reconnect behavior has been inconsistent across core versions (issue #7587, forum t=39116). Do not change `setCpuFrequencyMhz()` mid-TLS session (issue #7240 — WiFi drops).

One note on HTTPS payloads: the ESP32 TLS hardware record size tops out at 16,384 bytes. **For a 30-minute GPX file that may be >100 KB, stream the body from LittleFS via `http.sendRequest("POST", &file, file.size())`** — this avoids allocating a heap buffer for the full payload, and `HTTPClient` takes care of chunking.

## 3. AXP2101 VBUS-loss handling via XPowersLib

**Supreme decision, measured 2026-10-18 (#106): use PMU power-off plus
VBUS-insert power-on.** The ESP32-S3's PMU IRQ is GPIO 40, which is not an
RTC GPIO and cannot wake ext0/ext1 deep sleep. It remains a normal awake
interrupt. `PMU.shutdown()` powered the entire Supreme off and it stayed
off with USB still connected. Unplugging and replugging USB powered it on;
the AXP2101 reported power-on source `0x04` (VBUS insert) and power-off
source `0x02` (software). This requires an **insert edge**, so Stage 4 must
never request shutdown while VBUS is present.

The existing 10 s USB-loss confirmation and 10 min battery-idle grace
remain. A running upload can finish within the existing drain window;
otherwise it is aborted. The trip is finalized, storage is closed and
unmounted under its mutex, and VBUS is read again **immediately before**
`PMU.shutdown()`, with no logging or delay between the read and request.
If USB has returned, storage is remounted without formatting and logging
resumes; the network is released only after an aborted upload closes. A
VBUS edge between the final I2C read and shutdown write is still a race.
XPowersLib's shutdown call only writes the request bit; if power remains
on after 1 s, firmware logs an error and restores storage/logging, with a
fresh idle grace before retry. If an upload is still busy after the abort
timeout, recovery keeps storage locked and restarts; it never remounts
under that task's old file handle. A failed recovery remount also restarts
into normal boot. GPIO wake configuration is not involved.

Every power-on is a full boot: no RTC RAM or deep-sleep reset cause is
required. Trip numbering persists in Preferences/NVS and queued GPX files
in LittleFS (SD migration is #109). Supreme GPS main power is ALDO4 at
3.3 V; backup RAM is fed directly by the 18650, independently of VBACKUP.
With VBACKUP off, a hot start after ALDO4 was off for 60 s took 2.0 s, and
GPS settings survived PMU shutdowns. Keep VBACKUP disabled. VBUS power-on
also brings LoRa ALDO3 up on, so Stage 4 disables and verifies it as its
first PMU operation, before any Serial wait or storage/GPS setup.
**Parked current is still unmeasured (#25);** this change does not establish
battery runtime or the overnight hot-start time (#26).

The comparison and recommendation below record the original **V1.2**
design; they are retained as history and do not describe the Supreme path.

On T-Beam V1.2 the **AXP2101 IRQ pin is wired to ESP32 GPIO 35** (open-drain, active-low, asserts ≥16 ms per datasheet). GPIO 35 is input-only and has no internal pull-up hardware — LilyGo does not populate an external pull-up, and `pinMode(PMU_IRQ, INPUT_PULLUP)` is a no-op there. In practice it works due to trace capacitance and the AXP2101's strong active pull-down, but **if you observe missed IRQs, add an external 10 kΩ pull-up to 3V3**. GPIO 35 is RTC_GPIO5, so it is wakeup-capable for `ext1`.

Enable the two VBUS IRQs and the low-battery warnings; leave the rest masked unless you need them:

```cpp
#define XPOWERS_CHIP_AXP2101
#include <XPowersLib.h>
XPowersPMU PMU;
volatile bool pmuIrqFlag = false;
void IRAM_ATTR onPmuIrq() { pmuIrqFlag = true; }   // no I2C in ISR
void setupPmuIrq() {
  pinMode(35, INPUT);                              // or INPUT_PULLUP + external 10k
  attachInterrupt(digitalPinToInterrupt(35), onPmuIrq, FALLING);
  PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
  PMU.clearIrqStatus();
  PMU.enableIRQ(
      XPOWERS_AXP2101_VBUS_REMOVE_IRQ     | XPOWERS_AXP2101_VBUS_INSERT_IRQ   |
      XPOWERS_AXP2101_PKEY_SHORT_IRQ      | XPOWERS_AXP2101_PKEY_LONG_IRQ     |
      XPOWERS_AXP2101_WARNING_LEVEL1_IRQ  | XPOWERS_AXP2101_WARNING_LEVEL2_IRQ);
}
void pollPmu() {
  if (!pmuIrqFlag) return;
  pmuIrqFlag = false;
  PMU.getIrqStatus();                              // latches 0x48/0x49/0x4A
  if (PMU.isVbusRemoveIrq())         onVbusLoss();
  if (PMU.isVbusInsertIrq())         onVbusReturn();
  if (PMU.isPekeyLongPressIrq())     gracefulShutdown();
  if (PMU.isDropWarningLevel2Irq())  gracefulShutdown();   // ≈ SoC critical
  PMU.clearIrqStatus();                            // MUST write-1-to-clear
}
```

**The number-one failure mode is forgetting `clearIrqStatus()`**: AXP2101 status registers are write-1-to-clear, so if you don't clear them, the IRQ line stays asserted, the falling-edge interrupt never re-fires, and you get exactly one IRQ for the lifetime of the board. `getIrqStatus()` reads the three status registers into a cached 24-bit value that every `isXxxIrq()` predicate tests against, so you must call it before any of the `isXxxIrq()` checks.

The full catalogue of relevant predicates in `src/XPowersAXP2101.tpp`: `isVbusInsertIrq`, `isVbusRemoveIrq`, `isBatInsertIrq`, `isBatRemoveIrq`, `isPekeyShortPressIrq`, `isPekeyLongPressIrq`, `isPekeyNegativeIrq`, `isPekeyPositiveIrq`, `isBatChargeStartIrq`, `isBatChargeDoneIrq`, `isChargeOverTimeoutIrq`, `isDropWarningLevel1Irq`, `isDropWarningLevel2Irq`, `isStateOfChargeLowIrq`, `isGaugeWdtTimeoutIrq`, `isBatWorkOverTemperatureIrq`, `isBatChargerOverTemperatureIrq`, `isBatDieOverTemperatureIrq`, `isBatOverVoltageIrq`, `isWdtExpireIrq`. The corresponding enable-mask enums follow the `XPOWERS_AXP2101_*_IRQ` pattern.

**The time-budget question has a reassuring answer: hours, not seconds.** A healthy protected 18650 (~3,000 mAh usable) powering the ESP32 + NEO-M8N at an average 150 mA (tracking) to 250 mA (cold-fix re-acquiring) gives you **12–20 hours of runtime**. You therefore have all the time you need to flush cleanly; the goal is not to sprint but to avoid doing anything that could corrupt LittleFS while the user yanks power from their cigarette-lighter socket repeatedly. Practical discipline: on `VBUS_REMOVE_IRQ`, finalize the trip (close trkseg/trk/gpx tags, `file.close()`), but **keep running on battery** — don't deep-sleep immediately. Only sleep when (a) trip is ended anyway and (b) you have been on battery alone for, say, 5 minutes with no re-insertion, or (c) `DropWarningLevel2Irq` fires.

Two shutdown options compared:

| | `esp_deep_sleep_start()` + PMU stays on | `PMU.shutdown()` |
|---|---|---|
| ESP32 quiescent | 10–150 µA | 0 |
| AXP2101 quiescent | ~40 µA | ~40 µA |
| Wake sources | ext1 on GPIO 35, timer, touch | PWRON long-press, VBUS insert (if armed) |
| RTC RAM (for last-fix cache) | retained | lost — cold boot |
| Recovery on USB reconnect | instant via IRQ | requires button or armed wakeup |

**Use `esp_deep_sleep_start()` for VBUS-loss sleep**; reserve `PMU.shutdown()` for true over-discharge protection. The sequence:

```cpp
void gracefulShutdown() {
  tripFsmForceEnd();                               // closes GPX file
  LittleFS.end();                                  // unmounts, final commit
  PMU.disableALDO4();                              // GPS off (your rail)
  PMU.clearIrqStatus();
  PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
  PMU.enableIRQ(XPOWERS_AXP2101_VBUS_INSERT_IRQ | XPOWERS_AXP2101_PKEY_SHORT_IRQ);
  esp_sleep_enable_ext1_wakeup(1ULL << 35, ESP_EXT1_WAKEUP_ALL_LOW);
  esp_deep_sleep_start();
}
```

On wake, read `esp_sleep_get_wakeup_cause()` to confirm it was the PMU line, then `getIrqStatus()` + `isVbusInsertIrq()` + `clearIrqStatus()`.

**Battery monitoring API** (from `XPowersAXP2101.tpp`): `getBattVoltage()` returns mV (e.g. 3876), `getBatteryPercent()` returns 0–100 (or 0xFF cast if no battery), `getVbusVoltage()`, `getSystemVoltage()`, `isCharging()`, `isVbusGood()`, `getTemperature()` (°C). All require you to call `enableBattDetection()`, `enableBattVoltageMeasure()`, `enableVbusVoltageMeasure()`, and `enableSystemVoltageMeasure()` once in setup or the readings return 0. Note that the E-Gauge fuel gauge **self-calibrates over one full charge-discharge cycle**, so early percentages can be off by 10–20% until the cell has been trained.

**Known bugs worth flagging.** The predecessor library `lewisxhe/AXP202X_Library` (for AXP192, now deprecated) had a VBUS-remove IRQ that only fired alongside PWRON presses unless both insert and remove IRQs were enabled simultaneously (issue #35). That bug is resolved in XPowersLib, but the workaround — always enable both VBUS IRQs — is still a good habit. No current silicon errata for AXP2101 are public. Rail-mapping confusion between AXP192 (older T-Beams, LDO3=GPS) and AXP2101 (V1.2) is responsible for most community bug reports (Meshtastic #2838, #5893; LilyGo-LoRa-Series #156, #211) — verify your rails with a meter.

## 4. Minimum valid GPX 1.1 with speed and course

**GPX 1.1 deliberately removed `<speed>` and `<course>` from core `trkpt`** (they were in 1.0). The de-facto 2026 standard for putting them back is **Garmin TrackPointExtension v2** inside the `<extensions>` element — v1 only carries `atemp/wtemp/depth/hr/cad` and does not include speed or course. GPX 1.1's `<extensions>` uses `processContents="lax"`, so unknown extensions cannot fail validation.

Your root element needs all four declarations together:

```xml
<gpx version="1.1"
     creator="TBeamMileage/1.0"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2"
     xsi:schemaLocation="http://www.topografix.com/GPX/1/1
                         http://www.topografix.com/GPX/1/1/gpx.xsd
                         http://www.garmin.com/xmlschemas/TrackPointExtension/v2
                         https://www8.garmin.com/xmlschemas/TrackPointExtensionv2.xsd">
```

**Element order inside `<trkpt>` is strict** (GPX 1.1 uses `xs:sequence`): `ele` → `time` → `magvar` → `geoidheight` → `name` → `cmt` → `desc` → `src` → `link` → `sym` → `type` → `fix` → `sat` → `hdop` → `vdop` → `pdop` → `ageofdgpsdata` → `dgpsid` → `extensions`. Inside `gpxtpx:TrackPointExtension` v2, the order is also fixed: `atemp` → `wtemp` → `depth` → `hr` → `cad` → `speed` → `course` → `bearing`. Violate either order and strict validators (Xerces, xmllint) fail; lenient readers (OsmAnd, Basecamp) tolerate it — **write in schema order**.

Complete schema-valid example with three track points:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="TBeamMileage/1.0"
     xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v2"
     xsi:schemaLocation="http://www.topografix.com/GPX/1/1
                         http://www.topografix.com/GPX/1/1/gpx.xsd
                         http://www.garmin.com/xmlschemas/TrackPointExtension/v2
                         https://www8.garmin.com/xmlschemas/TrackPointExtensionv2.xsd">
  <metadata>
    <name>Trip 2026-04-22</name>
    <time>2026-04-22T14:30:15Z</time>
  </metadata>
  <trk>
    <name>Drive to office</name>
    <type>driving</type>
    <trkseg>
      <trkpt lat="37.7749290" lon="-122.4194160">
        <ele>16.0</ele>
        <time>2026-04-22T14:30:15Z</time>
        <sat>10</sat>
        <hdop>0.9</hdop>
        <extensions>
          <gpxtpx:TrackPointExtension>
            <gpxtpx:speed>12.345</gpxtpx:speed>
            <gpxtpx:course>87.4</gpxtpx:course>
          </gpxtpx:TrackPointExtension>
        </extensions>
      </trkpt>
      <trkpt lat="37.7751000" lon="-122.4195800">
        <ele>16.2</ele><time>2026-04-22T14:30:16Z</time>
        <sat>10</sat><hdop>0.9</hdop>
        <extensions><gpxtpx:TrackPointExtension>
          <gpxtpx:speed>12.610</gpxtpx:speed><gpxtpx:course>88.0</gpxtpx:course>
        </gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="37.7752800" lon="-122.4197500">
        <ele>16.4</ele><time>2026-04-22T14:30:17Z</time>
        <sat>11</sat><hdop>0.8</hdop>
        <extensions><gpxtpx:TrackPointExtension>
          <gpxtpx:speed>12.880</gpxtpx:speed><gpxtpx:course>88.3</gpxtpx:course>
        </gpxtpx:TrackPointExtension></extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
```

**Tool support for TrackPointExtension v2 speed/course** varies and is worth understanding when picking a reference viewer: **GPXSee reads it and graphs it** (also re-derives speed from geometry if missing); **Strava and Garmin Connect read it** on upload; **OsmAnd honors it**; **gpx.studio reads extensions but generally re-derives speed from position+time**; **Google Earth ignores `<extensions>` entirely** but still renders the track from lat/lon/time; **QGIS's default GPX provider ignores extensions** — use OGR GPX driver or parse manually if you need speed in QGIS. The track itself will always render everywhere because lat/lon/ele/time are in core elements.

`<time>` uses `xsd:dateTime` and must have a timezone designator. **`2026-04-22T14:30:15Z` is valid and correct**; fractional seconds (`.250Z`) preserve the RMC epoch when its centiseconds are nonzero, including at 1 Hz. **Always emit UTC with `Z`**, never local time. The GPX developer's manual is explicit: "Date and time are in Universal Coordinated Time (UTC), not local time!"

**File-writing strategy on-device.** Write the prologue (XML decl + `<gpx>` open tag + `<metadata>` + `<trk>` open + `<trkseg>` open) at file creation, append `<trkpt>…</trkpt>` lines as the trip progresses (closing each trkpt on the same line keeps parseability even if power is cut mid-trip), and append the epilogue (`</trkseg></trk></gpx>`) only at trip end. If the epilogue never gets written (hard crash), **most tools tolerate unclosed tags** and you can run an on-boot "fsck" pass that reads the last file, trims any truncated trkpt, and appends the missing closing tags. This is a far better failure mode than batching everything in RAM.

## 5. Trip state machine with hysteresis

Use a five-state FSM driven by `millis()` deltas — never `delay()`. The states are **IDLE** (parked), **ARMING** (speed exceeded start threshold, counting up), **ACTIVE** (writing trkpts), **COOLDOWN** (speed dropped below end threshold, counting down while still writing), and **FINALIZING** (closing file). Your thresholds as constants (5 mph = 2.235 m/s, 2 mph = 0.894 m/s):

```cpp
static constexpr float    START_MPS      = 2.235f;
static constexpr float    END_MPS        = 0.894f;
static constexpr uint32_t ARM_MS         = 10UL  * 1000;
static constexpr uint32_t COOL_MS        = 180UL * 1000;
static constexpr uint32_t MIN_TRIP_MS    = 30UL  * 1000;
static constexpr float    MIN_TRIP_METERS = 160.0f;    // ~0.1 mi
enum TripState { IDLE, ARMING, ACTIVE, COOLDOWN, FINALIZING };
static TripState state = IDLE;
static uint32_t armT0=0, coolT0=0, tripT0=0;
static double   tripMeters=0;
void tripFsmOnFix(const Fix& f) {
  const uint32_t now = f.monoMs;
  switch (state) {
  case IDLE:
    if (f.speed >= START_MPS) { state = ARMING; armT0 = now; }
    break;
  case ARMING:
    if (f.speed < START_MPS)                state = IDLE;
    else if (now - armT0 >= ARM_MS)        { beginTrip(f); state = ACTIVE;
                                             tripT0 = now; tripMeters = 0; }
    break;
  case ACTIVE:
    writeOrBufferTrkpt(f);
    accumulateDistance(f);
    if (f.speed <= END_MPS) { state = COOLDOWN; coolT0 = now; }
    break;
  case COOLDOWN:
    writeOrBufferTrkpt(f);                    // keep logging through the cooldown
    accumulateDistance(f);
    if (f.speed > END_MPS)                    state = ACTIVE;
    else if (now - coolT0 >= COOL_MS)         state = FINALIZING;
    break;
  case FINALIZING:
    endTrip(/*discardIfMicro=*/true);
    state = IDLE;
    break;
  }
}
```

On a VBUS-loss IRQ from §3, set a `volatile bool vbusLost` flag and in `loop()` force `endTrip(false)` (do not discard micro-trips — a genuine short trip that ended in a power cut is still data you want to keep) and transition to IDLE before deep-sleeping.

**Current implementation: trackpoint times belong to the RMC epoch.** Each
fix captures the validated UTC date/time from the same RMC sentence as its
position, using TinyGPSPlus centiseconds. Whole seconds use `...SSZ`;
nonzero centiseconds use three fractional digits (`...SS.sssZ`). A delayed
UART drain or a system-clock correction cannot shift that epoch. GGA time
must never be combined with an earlier RMC date at midnight.

A fix with missing or invalid RMC date/time has no `<time>`, even if GPS or
NTP has synchronized the system clock. The 90-point pending ring retains
untimed fixes in order, spills older ones untimed when full, and flushes
its remaining untimed tail before a timed fix or at trip end. It never
reconstructs epochs from receipt time. This supersedes the original brief's
proposal to back-date buffered points or fabricate coarse UTC. System-clock
synchronization, header time and local filenames remain independent.

**GPS fix loss mid-trip (tunnel, garage): start a new `<trkseg>` inside the same `<trk>`.** The GPX 1.1 spec says this explicitly: *"To represent a single GPS track where GPS reception was lost, or the GPS receiver was turned off, start a new Track Segment for each continuous span of track data."* Garmin devices do this; OsmAnd does this; OpenTracks does this. The alternative — writing into the same trkseg — causes viewers to draw a straight line across the tunnel, distorting both distance and speed in downstream tools. Detection heuristic: any one of (RMC validity 'V' for >3 s, gap >5 s between valid fixes, HDOP >10 with sats <4) arms a "needs new segment" flag; on the next valid fix, emit `</trkseg><trkseg>` before the next trkpt. Do not close the file or `<trk>`.

**Speed-noise handling:** GPS modules show 0.5–3 m/s random walk even parked. Your 3-minute cooldown already absorbs this — sustained ghost speed above 2 mph for 180 s is nearly impossible from noise alone. For extra robustness, apply a **5-sample median filter to speed on the ACTIVE→COOLDOWN path only** (noise-suppresses stop detection), and leave IDLE→ARMING unfiltered for responsiveness when pulling out. Use NEMA RMC Doppler speed rather than position-derived speed when available — the u-blox NEO-M8N computes it internally and it's significantly smoother.

**Micro-trip suppression** belongs in `endTrip(discardIfMicro)`: if the trip lasted less than 30 s or covered less than 0.1 mi (160 m), delete the file. Always pass `false` on the USB-loss path.

## 6. Patterns worth stealing from existing firmware

**revk/ESP32-GPS** (Adrian Kennard, note the canonical repo moved to `codeberg.org/RevK/ESP32-GPS`) is pure ESP-IDF on a custom PCB with a Quectel L86 GNSS, not Arduino and not a T-Beam — so don't port code, port ideas. The architecture is a two-task split: `gps_task` reads UART, checksums, parses, and enqueues `fix_t` structs; `pack_task` runs a modified Ramer–Douglas–Peucker with squared distances (no `sqrt`) to compress straight stretches before writing SD. Five patterns are directly applicable to your Arduino firmware: **producer/consumer FreeRTOS tasks with a queue** between NMEA parsing and file writing (core 0/core 1 split — same as your networking architecture); a **"pre-motion ring buffer"** of the last ~10 stationary fixes prepended to each trip so departures don't lose the driveway; **ECEF squared-distance for "am I home?"** checks (store home as int64 µm tuples, compare sum-of-squares, skip trig entirely); **upload only when stationary + at home + on known SSID**; and **file-per-trip naming `<MAC>-<ISO8601>.gpx`** for trivial server-side merge/dedup. Explicitly do not copy revk's `jo_t`/`lwmqtt`/`revk_*` infrastructure (IDF-only), the `$PMTK314` init strings (MediaTek, not u-blox), or the soft-reboot-to-park idiom (wastes 30 s cold-fix time on a T-Beam with no GPS RTC backup).

**MartijnBraam/car-tracker** is an unmaintained ESP8266 + NEO-6M Arduino sketch, last real work in 2019. Its one brilliant idea: **detect trip start/end by measuring the car's 12 V rail through a voltage divider** — when the alternator kicks in, voltage rises from ~12.4 V to ~13.3 V, and that's a signal that is immune to GPS drift when parked and requires no ignition-wire splice. On T-Beam V1.2, wire the 12 V rail through a ~30 kΩ / 7.5 kΩ divider into an **ADC1 GPIO** (never ADC2 — it collides with WiFi), apply Schmitt thresholds of ~13.3 V on / ~12.4 V off, and debounce with 10 s start / 60 s end (per Keep Track GPS empirical results). This gives you a robust orthogonal signal to corroborate your GPS-speed FSM. Don't copy the actual code — it has no graceful flush on power loss, no HTTPS, no RTC awareness, and the 2019 TODO for wake-on-voltage was never finished.

Two other references worth skimming. **kizniche/ttgo-tbeam-ttn-tracker** has the cleanest handling of AXP192 rail sequencing for GPS power-up on T-Beam REV0/REV1 pinouts — even though you're on V1.2 with AXP2101, the class structure is a clean Arduino template. **DeuxVis/Lora-TTNMapper-T-Beam** demonstrates persisting LoRaWAN frame counters and GPS fix state to ESP32 RTC slow memory (`RTC_DATA_ATTR`) across deep sleep — **directly applicable**: save last-known position, time, and ephemeris hint to RTC memory so a post-deep-sleep wake achieves `<5 s` TTFF via hot-start instead of 30 s cold-start. Meshtastic's firmware (`variants/tbeam/variant.h` and `Power.cpp`) is the authoritative source for T-Beam V1.2 rail mapping under AXP2101 — consult it if rail assignments ever feel uncertain.

## Consolidated checklist and closing thoughts

The firmware that emerges from this research has three concurrent loops: **Arduino `loop()` on core 1** does nothing but drain `Serial1` into TinyGPSPlus, run `tripFsmOnFix()` when a full fix arrives, poll the AXP2101 IRQ flag, and post completed-file events to a queue. **A pinned task on core 0** pulls from that queue and runs the scan → BSSID match → connect → HTTPS stream-POST → delete state machine, blocking freely without affecting GPS. **An interrupt on GPIO 35** sets a flag when VBUS disappears, and `loop()` responds by forcing `tripFsmOnFix` into FINALIZING, closing LittleFS, and deep-sleeping with ext1 wake on the same pin. Every trkpt `file.close()`s after writing — you lose at most the single in-flight record on a hard crash. GPX is written in schema order with Garmin TrackPointExtension v2 for speed and course. New `<trkseg>` on fix-loss, per-RMC UTC epochs with an untimed pending ring, median-smoothed stop detection, 30 s / 0.1 mi micro-trip filter on normal ends.

The mental model to hold: **you are not racing the battery — it will outlast you by hours — but you must be paranoid about flash commits**. That is the deep reason to pay the 10–20 ms close-every-record cost and not try to be clever about flush-only batching. The rest is just careful state machines and correct XML.
