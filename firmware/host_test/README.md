# Host tests for the Stage 4 firmware

Stage 4 is the main Arduino sketch, `firmware/04_trip_logger/04_trip_logger.ino`.
These tests run its code and supporting components on your PC, using simulated
Arduino/ESP32 interfaces (stubs) instead of a connected board.

## Prerequisites

- Work from the repository root: the folder containing `firmware/`, `server/`
  and `tools/`. All commands below use **Bash**. On Windows, open **Git Bash**
  (installed with Git for Windows), not PowerShell or the Windows/WSL `bash`
  launcher, and run `cd "<repo-root>"` first. Use forward slashes in paths.
- Install Python 3 (CI uses 3.12) and create a fresh virtual environment
  (venv). Replace `<venv>` below with its path. **That path must contain no
  spaces**, including parent folders: `run.sh` splits `CXX` on whitespace,
  even when you quote the assignment. Put the venv outside the checkout if
  its path contains spaces.

  ```bash
  python -m venv "<venv>"
  export PYTHON="<venv>/Scripts/python.exe"  # Windows / Git Bash
  # On Linux/macOS, use this instead:
  # export PYTHON="<venv>/bin/python"
  "$PYTHON" -m pip install -r server/mileage-ingest/requirements.txt
  ```

  This matches the `firmware-host` job in
  [CI](../../.github/workflows/audit-tests.yml). Installing only Flask is
  insufficient: without `tzdata`, the upload protocol bridge fails on Windows
  when the real ingest app loads its time zones. `PYTHON` selects the venv
  interpreter for Python checks; activation is unnecessary.
- Use a **C++20 compiler**, either `g++` or `clang++` on Bash's `PATH`. If you
  have neither (common on Windows), install Zig's compiler in the same venv:

  ```bash
  "$PYTHON" -m pip install ziglang
  export CXX="$PYTHON -m ziglang c++"
  ```

  You can also select `CXX=g++` or `CXX=clang++`. Without an override, the
  runner tries `g++`, `clang++`, then `python -m ziglang c++`.
- For a **complete run with no skips**, install TinyGPSPlus 1.0.3 and U8g2
  2.36.18 and set their paths under
  [Optional libraries and skips](#optional-libraries-and-skips) before running.
  No board, private credentials or running server is needed; the runner
  copies `firmware/secrets.h.example` into its build include directory.

## Run

In the same Bash session, from the repository root:

```bash
mkdir -p firmware/host_test/build
set -o pipefail
bash firmware/host_test/run.sh 2>&1 | tee firmware/host_test/build/run.log
result=$?
echo "Run exit status: $result"
grep -n SKIPPED firmware/host_test/build/run.log
```

**Any non-zero run exit status means failure.** `pipefail` preserves failures
when saving output through `tee`. Fix the reported error and rerun; individual
`PASS` lines do not establish that the whole run passed. Exit 0 means the
checks that ran passed, but optional checks may still have been skipped.
Inspect every `SKIPPED` line. The final `grep` exits 1 when there are no matches;
that means no skips, not a test failure. Keep the displayed run status separate
from `grep`'s status.

## What a full run covers

`run.sh` builds **26 C++ test sources in 30 build variants**, plus six Python
checks. Extra builds cover stamped/fallback versions, a second display-card
duration and three sketch copies of bus recovery. Many integration binaries
also run with runtime debug off and on. **[TinyGPSPlus]** marks the eight
sources that require that optional library.

**Storage and trip lifecycle**

- `test_fs`: GPX (GPS track) file writing, trip states, buffered gaps/untimed points, write/full-store failures, safe first-use formatting, upload queues/retries, boot/shutdown recovery, microSD selection/flash fallback, FAT (card filesystem) interrupted-repair recovery and trip splitting at the size limit.
- `test_journal`: diagnostic journal integration with SD, LittleFS (internal-flash filesystem) and RAM fallback, quotas, trip priority, lock contention, shutdown sealing and remount recovery.
- `test_journal_vfs`: production filesystem adapter using temporary host files, including sync/close/list errors and path isolation.

**GPS input and signal health**

- `test_gps` **[TinyGPSPlus]**: real GPS sentence parsing through the sketch's loop, one point per second, receiver configuration/replies, fix-quality gates, gaps, UTC (universal time) timestamps/clock corrections and debug-mode GPX parity.
- `test_gps_antenna`: antenna-warning evidence, hold times, missing/stale observations, recovery and timer wraparound.
- `test_debug_status` **[TinyGPSPlus]**: real satellite-message parsing, satellite deduplication/freshness and bounded screen/serial formatting.

**Diagnostics and delivery**

- `test_build_version`: fallback and stamped firmware-version strings (two builds).
- `test_diag_catalog`: diagnostic codes, severity, display strings and event-storage policy.
- `test_event_log`: event identity, damaged/torn records, acknowledgment recovery, quotas, fallback, concurrency and bounded rejected-record/loss recovery.
- `test_battery_events`: checked boot/shutdown battery samples, failed reads and shutdown journal ordering with a full queue.
- `test_event_upload`: production delivery, PSRAM (external RAM) buffer allocation/reuse/failure with continued local journaling/GPX delivery, 64-record/32 KiB limits, consistent status snapshots, old-boot history, trip priority, deadlines, HTTP/acknowledgment failures, retries, heartbeat and rejected-record recovery.
- `test_fault_hooks` **[TinyGPSPlus]**: real sketch paths raising, repeating and clearing power/GPS/storage/network/display faults, plus a normal drive/upload without spurious faults.

Delivery tests exercise the **core-0 worker** (background networking task
assigned to ESP32 CPU core 0) and `netCycle` (one pass deciding whether to
connect and upload). They check E5001, **"Home access point not seen"**, as a
counter; one-use enrollment of diagnostic status numbering; and isolation of
a single rejected event so it cannot block the queue. See
[DIAGNOSTICS.md](../../DIAGNOSTICS.md) for enrollment and isolation details.

**Display**

- `test_debug_display`: simulated OLED (small screen) bus failures during initialization/drawing and safe display disablement.
- `test_display_model`: status-card timing, motion/fault queues and debug suppression, at default and 10-second durations (two builds).
- `test_production_display_layout`: text bounds/spacing from checked-in glyph metrics (character sizes), including worst-case satellite counts/error codes.
- `test_production_display`: renderer initialization, drawing/power-save bus failures and disablement.
- `test_display_runtime` **[TinyGPSPlus]**: sketch integration, button/debounce behavior, runtime debug switching, fault cards and continued logging with a failed display.

**Power management and peripheral bus**

- `test_i2c`: I2C (two-wire peripheral bus) recovery and PMU (power-management chip) startup failures, built against `02_pmu_test`, `03_gps_test` and `04_trip_logger` (three builds).
- `test_pmu_startup_event`: diagnostic boot-history policy from saved PMU startup/recovery results.
- `test_pmu_verification` **[TinyGPSPlus]**: checked PMU register reads, interrupt decoding, charging thresholds/control read-back, hysteresis and retry timing.
- `test_degraded_policy`: mode selection and bounded retry delays when power control or storage is unavailable/untrusted.
- `test_degraded` **[TinyGPSPlus]**: real startup/loop behavior with PMU/storage failures, verified supply settings, retry/restart recovery and preserved trips.
- `test_temperature`: BME280 temperature-sensor compensation, bounded bus operations and checked PMU temperature-input/battery probes.
- `test_temperature_runtime` **[TinyGPSPlus]**: debug temperature sampling/logging, failed/stale samples, charge-guard freshness and debug-off behavior.

**RTC (real-time clock) and diagnostic time**

- `test_rtc`: calendar encoding/validation, clock confidence, timestamp sources and timer wraparound.
- `test_rtc_runtime` **[TinyGPSPlus]**: real sketch clock reads, missing/invalid clocks, verified correction, retry limits and diagnostic timestamps.

**Python checks**

- `check_display_fonts.py` **[U8g2]**: compare installed font glyph metrics with checked-in metrics.
- `check_event_log_protocol.py`: validate C++ event exports and catalog/context boundaries against the real server protocol.
- `check_battery_events_protocol.py`: validate actual boot/shutdown battery-event exports.
- `check_event_upload_protocol.py`: validate requests and post/replay them through the real ingest app's disposable Flask test client/SQLite databases; no sockets or live server.
- `check_rtc_protocol.py` **[TinyGPSPlus]**: validate diagnostic events with unknown, RTC, GPS and network-synchronized time sources.
- `check_fault_hooks_protocol.py` **[TinyGPSPlus]**: validate actual fault transitions, measured context and catalog policy.

## Optional libraries and skips

In Arduino IDE's Library Manager, install **TinyGPSPlus** version **1.0.3**,
the version pinned by CI. Point to its **`src/` folder**, containing
`TinyGPS++.cpp`, using your library installation location:

```bash
export TINYGPS_DIR="<library-dir>/TinyGPSPlus/src"
```

Without it, the runner prints `SKIPPED` and can still exit 0: **all eight
[TinyGPSPlus] C++ suites above and the RTC/fault Python bridges are omitted**.
The default searched is `~/Documents/Arduino/libraries/TinyGPSPlus/src`;
if your IDE uses a different sketchbook folder, set the variable explicitly.

For the optional installed-font comparison, install **U8g2 2.36.18** through
Library Manager, then point to its **library root**, not `src/`:

```bash
export U8G2_DIR="<library-dir>/U8g2"
```

The default is `~/Documents/Arduino/libraries/U8g2`. Missing font sources mean
`SKIPPED`; an installed version other than **2.36.18** makes the comparison
fail (as do mismatched metrics). C++ layout tests still run using checked-in
metrics whether this comparison runs, skips or fails. Search the saved output
for `SKIPPED` before calling a run complete.

## Outputs

- `firmware/host_test/build/out/`: trip GPX files from `test_fs` and the GPS test with runtime debug off.
- `firmware/host_test/build/out-debug/`: GPS fixtures with runtime debug on.

`test_gps` is compiled **once**. The same binary runs with `HOST_DEBUG=0`
and `HOST_DEBUG=1`; `run.sh` automatically compares every `.gpx` in
`out-debug/` with its same-named file in `out/`, byte for byte. Files produced
only by `test_fs` are not part of that comparison. The runner reuses these
folders without clearing them, so old files may remain after later/skipped runs.

To check trip files with an ingest service, follow the `sample.gpx` curl-upload
example in [server/README.md](../../server/README.md), substituting a generated
GPX file. This is a separate manual check; `run.sh` does not upload these files.

## What these tests don't prove

The filesystem stub (`stubs/arduino_stub.h`) is a **simplified buffered-storage
model**, shared by simulated SD and LittleFS stores: writes report success into
a pending buffer; `flush()`/`close()` attempt to commit it to an in-memory file
map, and writes also auto-commit once **more than 4096 pending bytes** accumulate.
Capacity limits and injected failures can prevent or partially complete commits.
This does not prove real SD/FAT or LittleFS timing or power-loss durability.

Boot/power-off tests model control flow, not electrical behavior. Hardware
labels include ALDO3 (LoRa-radio supply), ALDO4/GPS_EN (GPS supply/enable),
BLDO1 (microSD supply), VBACKUP (backup supply) and Wire1 (the PMU/RTC bus).
Tests check requested settings and simulated read-back, not physical rails.

PMU, WiFi and HTTP inputs are simulated; the host suites do not exercise
the real drivers, radio, allocator or reset mechanism. Network time transport
is simulated, and sequential host calls do not prove real multicore timing.
This does not replace the on-hardware shakedown: it checks logic, not the flash
driver, GPS reception or power behavior.
