# Stage 4 firmware design

Stage 4 is the trip logger for the LilyGO T-Beam Supreme: an ESP32-S3 with
a MAX-M10S Global Positioning System (GPS) receiver and microSD (Secure
Digital) trip storage, with internal-flash LittleFS as fallback. This note
explains the current design; use [the firmware guide](README.md) for setup and
[the diagnostics reference](../DIAGNOSTICS.md) for diagnostic behavior.
The original V1.2 proposal is preserved in [history](../docs/history/STAGE4_TECHNICAL_BRIEF.md).

Unless another file is named, code references below refer to
[`04_trip_logger/04_trip_logger.ino`](04_trip_logger/04_trip_logger.ino).

## Keep networking away from GPS parsing

`setup()` pins `networkTask()` to core 0; the Arduino `loop()` on core 1
owns GPS reception, receiver configuration, the trip finite state machine
(FSM), power management and displays. WiFi scans, joining and Hypertext
Transfer Protocol (HTTP) requests can block for seconds. Running them in
the GPS loop would stop it draining National Marine Electronics
Association (NMEA) sentences and lose fixes. Receiver configuration is
also serviced incrementally by `serviceGpsConfiguration()`, without an
acknowledgment wait that blocks reception.

`setup()` sets the universal asynchronous receiver/transmitter (UART)
receive buffer to 2,048 bytes **before** `GPSSerial.begin()`. At 9,600 baud
that is only about two seconds of incoming data, so buffering supplements
the core split rather than making long stalls safe.

The cores have distinct owners and narrow sharing points:

- Core 1 constructs `Fix` objects, owns trip state and writes the active
  file. Core 0 owns WiFi, upload retries and normal diagnostic-journal
  servicing (`loop()`, `netCycle()`, `networkTask()`, `serviceJournal()`).
- `gFsMutex`, through `FS_LOCK()`/`FS_UNLOCK()`, serializes filesystem
  access. `LockedFileStream` locks each upload read separately, allowing
  GPS writes between chunks. `uploadPending()` selects complete files
  and excludes the active trip. Holding the lock for a whole HTTP request
  would defeat the core split by blocking the GPS writer.
- Atomic flags coordinate upload hold, busy, abort and retry requests
  (`gNetHold`, `gNetBusy`, `gNetAbort`, `gNetRetryNow`). The hold/busy
  ordering prevents new network work racing storage unmount.
- `gDebugNetMux` protects scalar network snapshots. Diagnostic producers
  have separate builders per core; `EventLog::submit()` uses a bounded
  try-lock for their shared queue (`event_log.cpp`). Clock publications
  use atomic copies (`diagnostic_clock.h`, `DiagnosticClock`).

`NET_STACK_BYTES` allocates 12,288 bytes to networking. `reportNetworkStack()`
reports the lifetime minimum free stack in bytes. The measurement in
[issue #6](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues/6)
recorded 7,436 bytes free during real trip and diagnostics uploads with GPS
logging active; the allocation was retained. `uploadFile()` uses plain
HTTP with `WiFiClient`, without Transport Layer Security (TLS). A future
TLS path would need its own stack measurement.

## Firmware identity on the debug screen

The live debug page's top row keeps state and uptime intact, adding the
release version at the right only if all text fits in 21 characters with
a separating space. The `Debug ON` card shows the full version, including
its commit, in the 5x8 font on up to two lines; `Debug OFF` has no version.
`debug_status.h` receives the version as data and stays independent of Arduino.

## Trip lifecycle favors conservative mileage

`TripState` defines five states, advanced by `tripFsmOnFix()`:

- **IDLE:** a trusted speed at least `START_MPS` (2.235 m/s, about 5 mph)
  enters **ARMING**.
- **ARMING:** speed must stay at or above that threshold for `ARM_MS`
  (10 seconds). A slower or untrusted fix resets to IDLE. `gArmBuf` retains
  the opening fixes; `beginTrip()` writes them and counts their distance.
  Only a successful file start with enough space enters **ACTIVE**.
- **ACTIVE:** trusted fixes are recorded. Moving fixes above `END_MPS`
  (0.894 m/s, about 2 mph) add distance; speed at or below it enters
  **COOLDOWN**. A rejected speed is treated as zero and is not recorded.
- **COOLDOWN:** trusted fixes still enter the file, but add no distance or
  moving duration. Three consecutive trusted fixes above `END_MPS` resume
  ACTIVE. Any other fix resets the count. The original `COOL_MS`
  (300-second) deadline stays in force while gathering those three fixes;
  expiry takes precedence over resuming. The confirming fix becomes a new
  distance baseline, without bridging across cooldown or rejected fixes.
- **FINALIZING:** `endTrip()` runs in the same FSM pass that requests it,
  rather than waiting for another fix. Ordinary completion returns to
  IDLE; size splitting can immediately start another piece. Write failures
  and low storage also force finalization.

The speed value comes from the NMEA Recommended Minimum Navigation
Information (RMC) sentence. `gpsSpeedTrusted()` gates its use with the
u-blox binary NAV-PVT (navigation position, velocity and time) message:
it must have arrived within 1,500 ms, report a valid navigation fix and a
3D or combined fix, and meet both accuracy limits. Starting requires speed
accuracy at most 0.5 m/s and horizontal accuracy at most 5 m; during a trip
the limits are 1.0 m/s and 10 m. This is arrival-time association at 1 Hz,
allowing either message order, rather than an exact epoch match.
The limits were kept after the road-test calibration in
[issue #178](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/issues/178);
that does not establish parking-garage behavior.

`serviceGpsSpeedFallback()` enables RMC-only speed after more than ten
seconds without NAV-PVT. The initial wait starts only after boot receiver
configuration succeeds or gives up after five attempts; thereafter it is
measured from the last NAV-PVT. A transition warning announces fallback,
and a return message restores accuracy gating. RMC-only mode lacks those
accuracy checks and can accept false movement.

`endTrip()` normally discards a micro-trip if **either** its duration is
under 30 seconds **or** its distance is under 160 m. Duration ends at the
last moving fix, excluding the final parked cooldown. Confirmed Universal
Serial Bus (USB) loss and size splits keep short pieces. Separately, `loop()`
ends a recording trip after more than five minutes without any valid RMC fix,
including when no next fix arrives to drive the FSM. Rejected speeds still
count as received fixes for this watchdog (`gLastFixMonoMs`). Its
five-minute interval equals `COOL_MS`, but the clocks are independent:
cooldown starts on the first stopped or rejected fix (`gCoolT0`) and is
checked on each fix; the watchdog starts at the last valid RMC and runs
even without another fix. Neither requires cooldown to expire first.

The power-management unit (PMU) supplies the USB bus-voltage (VBUS) state.
`observeVbusChange()` and `confirmVbusOff()` require a ten-second loss and
a fresh absent reading before ending a trip, preserving short drives and
ignoring brief dips. Unknown readings never authorize power-off. The board
then stays awake on battery for upload opportunities. After ten minutes
idle on battery, `loop()` holds new network work and lets an upload drain
within a deadline. `powerOffForVbus()` closes the trip, stops/aborts uploads,
requests journal sealing, locks and unmounts storage, rechecks VBUS and
requests PMU shutdown. USB insertion powers it on again. These handoffs
exclude filesystem access while storage is unmounted.

## Compact GPX with recoverable tails

The GPS Exchange Format (GPX) output is version 1.0 (`GPX_PROLOGUE`,
`appendTrkpt()`). It allows `<speed>` and horizontal dilution of precision
(HDOP, `<hdop>`) directly on `<trkpt>`, avoiding a speed-extension wrapper.
Each point keeps position, optional time, speed and HDOP; elevation,
course and satellite count are omitted to keep storage use small.

`beginTrip()`, `writeOrBufferTrkpt()` and `endTrip()` write incrementally.
Each point is formatted as a complete line. `FileWriter::commit()` flushes
the batch and checks the resulting size; callers also close the file.
An append failure ends the trip and attempts repair rather than appending
after a possibly truncated point.

`receiveGpsNmea()` marks a new `<trkseg>` after a gap greater than five
seconds from the last accepted fix. Segment markers travel with the fix,
including through buffers. A gap during ARMING resets the arming window.
This lets consumers recognize missing coverage instead of drawing one
continuous segment across it.

Untimed fixes use a 90-point ring (`gPend`, `writeOrBufferTrkpt()`). When
full, the oldest point is committed without a time before accepting more.
`flushPending()` writes the remaining tail before the next timed point or
at trip end. The in-memory tail can be lost on reset.

At boot, `setup()` calls `fsckAllTrips()` on both mounted stores before
networking starts. `repairTrip()` copies through the last complete
`</trkpt>` and adds closing tags; files with no complete points are removed.
`recoverRepairCopy()` handles interrupted `.gpx.fix` repairs, retaining
ambiguous copies. `publishRepair()` replaces the original only after the
repair is verified and closed. On SD it must remove the old destination
before renaming; File Allocation Table (FAT) metadata is not power-loss
atomic. Incomplete files are excluded from uploads (`isCompleteFile()`).

`mountTripStorage()` selects SD first and LittleFS fallback for new trips;
the destination does not change mid-trip. Both mounted stores can upload
old files. SD is never formatted. LittleFS mounts with format-on-failure
disabled: unreadable or non-erased storage is preserved. The sole
provisioning exception is `mountFlashStorage()`: after
`tripPartitionIsErased()` reads **every byte** as `0xFF`, it formats that
empty partition once. A mount failure alone never authorizes erasure.

`tripShouldSplit()` reserves pending points, another point and closing
tags to keep each finished piece below 8 MiB. `tripFsmOnFix()` closes the
old piece and starts the next without re-arming, sharing the boundary fix
and preserving any cooldown deadline, provided storage startup succeeds.

The host fault tests exercise write failures, repair, interrupted
replacement, buffering and splitting (`host_test/test_fs.cpp` and
`host_test/test_gps.cpp`). They model filesystem behavior; they do not prove
physical flash or SD/FAT power-cut durability. Real SD recording and uploads
are documented in [the firmware guide](README.md), but a worst-case
power-loss bound, including a one-point-loss guarantee, is **not hardware
verified**. Clean battery-backed shutdown remains part of the design.

## A trackpoint owns its timestamp

`receiveGpsNmea()` processes each committed RMC immediately.
`validRmcClockFields()` and `setClockFromGps()` validate that sentence's
date/time before `buildFix()` stores its Coordinated Universal Time (UTC)
epoch. The trackpoint epoch is captured before rate-limited system-clock
synchronization; receipt age is used only for the system clock.

`isoFromEpochMs()` writes whole seconds for zero centiseconds, otherwise
three fractional digits (`.sssZ`) preserving the RMC's centisecond value.
`appendTrkpt()` emits `<time>` only when that fix has its own valid epoch.
Missing or invalid epochs stay untimed even after clock synchronization,
including buffered points: `flushPending()` never invents their times.

The real-time clock (RTC) anchors diagnostic timestamps only
(`rtc_runtime.h`, `RtcRuntime::begin()`; `diagnostic_clock.h`,
`DiagnosticClock::fill()`). Neither RTC time nor the synchronized system
clock supplies GPX timestamps. A correct clock cannot reconstruct a
missing position epoch.
