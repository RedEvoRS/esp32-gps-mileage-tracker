# Code Review — 2026-06-09

> This is a historical record, kept as written. It covers the retired T-Beam V1.2 and the code as it was then.
> Its pin, power-rail and storage details do not apply to the T-Beam Supreme.
> Any PR numbers, commit hashes and branch names refer to the project's earlier private repository and will not resolve here.
> For current status, see this repository's GitHub issues; see the [history index](README.md) for context.

Full review of Stage 4 firmware, ingest service, viewer service, and Docker
config, ahead of deployment and the on-hardware shakedown. Both server
services were exercised end-to-end with the Flask test client (auth, upload,
malformed input, traversal, XSS, exports), and the firmware's fsck scanner
was ported to host C and unit-tested. All fixes below are **applied**.

## Verified working (tested, no changes needed)

- **Ingest:** 401 on missing/bad token; `sample.gpx` produces the documented
  summary (0.295 mi, 45 s, 23.6 avg / 23.9 max mph, 4 pts); bad/empty GPX →
  400; duplicate upload of a timestamped trip is idempotent; correct
  `YYYY-MM` bucketing by end time; atomic writes.
- **Viewer:** trip list, map page, GPX serving; path traversal blocked
  (strict regex + resolve check → 404); script injection in notes is
  escaped by Jinja autoescaping; invalid category coerced to unset;
  CSV/XLSX exports correct.
- **Firmware fsck:** `findLastTrkptEnd()` carry-buffer scan verified correct
  on a truncated file, a complete file, a `</trkpt>` token straddling the
  256-byte read boundary, and garbage input.

## Issues found and fixed

### Firmware (`04_trip_logger.ino`)

1. **FS mutex held across the entire HTTP upload** (the one real bug).
   `uploadFile()` took `gFsMutex` before `http.sendRequest()` and released
   it after — up to ~15 s. A trip starting during an upload would block
   core 1 in `writeOrBufferTrkpt()`; the 2 KB GPS UART buffer holds only
   ~2 s of NMEA at 9600 baud, so fixes would be dropped. Realistic trigger:
   trip finalizes at home after the 3-min cooldown, upload starts on the
   next 30 s net tick just as you pull out again.
   **Fix:** a `LockedFileStream` proxy that takes the mutex only for each
   individual chunk read (HTTPClient reads ~1.4 KB at a time, each read is
   milliseconds). The GPS task now interleaves freely with uploads. The
   "mutex serializes all FS access" invariant is preserved per-operation;
   the esp_littlefs driver additionally has its own internal lock.

2. **Battery drain after the car turns off.** After VBUS loss the device
   stayed awake — GPS rail powered, WiFi scanning every 30 s — until the
   battery hit WARNING_LEVEL2 (days of pointless 18650 wear).
   **Fix:** after `IDLE_BATT_SLEEP_MS` (10 min) on battery with no active
   trip, enter deep sleep. VBUS insert (car on) wakes it. 10 min is ample
   for the post-drive upload (net task ticks every 30 s). Also,
   `gVbusLost` is now initialized from `PMU.isVbusIn()` at boot, so a
   battery-only boot doesn't stay awake forever.

3. **First ~10 s of every trip was never logged.** Points seen during
   ARMING were discarded; logging began only on the ACTIVE transition, so
   distance undercounted slightly (conservative for IRS, but data lost).
   **Fix:** a 16-slot ring buffers fixes during ARMING; `beginTrip()` now
   writes them into the file right after the prologue and counts their
   distance into the trip total.

4. **GPS jitter inflated distance while stopped.** Distance accumulated
   unconditionally in ACTIVE/COOLDOWN; 3 min stationary in COOLDOWN adds
   phantom meters from fix scatter.
   **Fix:** segment distance only accumulates when the current fix's speed
   is above `END_MPS` (2 mph). Conservative in the IRS-safe direction.

5. **FINALIZING waited for the next GPS fix.** If fix was lost exactly as
   cooldown expired (e.g. parked in a garage), the trip stayed open until
   a VBUS event. **Fix:** finalization now runs in the same FSM pass that
   triggers it, and a fix-loss watchdog ends any ACTIVE/COOLDOWN trip
   after `FIX_LOSS_END_MS` (5 min) without a valid fix. ARMING also resets
   to IDLE on a fix gap, so a stale arm timer can't instantly start a trip.

6. **Stale header comment** said the sketch had never been compiled; it
   compiles on ESP32 core 3.3.9 (CLAUDE.md). Comment refreshed — still
   needs the on-hardware shakedown.

   *Noted, not changed:* `gMaxSpeedMps` is diagnostics-only (the server
   computes max speed from the GPX), and it updates in every state; left
   as-is since it is never persisted.

### Ingest (`server/mileage-ingest/app.py`)

7. **Body size enforced only after reading the full body.**
   **Fix:** `MAX_CONTENT_LENGTH` set on the Flask app so oversized uploads
   are rejected during the read; the explicit check stays as a backstop.

8. **Token comparison wasn't constant-time.** Negligible on a LAN, but
   `hmac.compare_digest` is free. **Fix:** applied.

9. **GPX with no `<time>` elements got a wall-clock trip_id**, so a
   retried upload (e.g. device missed the 200) created a duplicate file.
   Nearly impossible in practice — the firmware back-dates points — but
   cheap to close. **Fix:** the no-timestamp fallback id is now
   `untimed-<sha256(body)[:12]>`, making retries idempotent.

   *Noted, not changed:* stdlib `xml.etree` is used on authenticated
   LAN-only input; modern expat mitigates billion-laughs. `defusedxml`
   would be the belt-and-braces option if this ever faces the internet.

### Viewer (`server/mileage-viewer/app.py`)

10. **SQLite connections were never closed** (`with sqlite3.connect(...)`
    commits but does not close). Harmless at this scale via refcounting,
    but sloppy. **Fix:** `contextlib.closing()` on all three DB helpers.

11. **CSV formula injection.** A note beginning with `=`, `+`, `@`, or a
    tab would execute as a formula when the CSV is opened in Excel. Your
    own notes, so low risk — but the export is the IRS artifact.
    **Fix:** text fields starting with those characters are prefixed with
    `'` in the CSV export (XLSX is unaffected; openpyxl writes strings as
    strings).

12. **Map error path used `innerHTML`** with the error message. **Fix:**
    builds the element via `textContent`.

### Housekeeping

- Removed stale `__pycache__/` directories from both server folders.
- Not a git repo (per your call — left alone). If that changes, the root
  `.gitignore` should cover `secrets.h`, `.env`, `server/data/`, and
  `__pycache__/`.

## Outstanding before "done"

1. **Re-compile firmware** on ESP32 core 3.3.9 — this review changed
   `04_trip_logger.ino` and it has not been recompiled since.
2. Deploy `server/` to the Debian box once SSH is ready (unchanged plan in
   CLAUDE.md).
3. End-to-end curl test with `server/sample.gpx`, then flash and road-test.
