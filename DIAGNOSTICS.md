# Device diagnostics framework

Diagnostics help explain what happened to the tracker and whether a fault is
still active. **Stage 4** means the main trip-logger sketch,
[`firmware/04_trip_logger/`](firmware/04_trip_logger/). It keeps a bounded event
history on the board and sends a current-health snapshot after trip uploads.
The server stores these reports for the viewer's **Device health** page and
optional notifications/exports. Diagnostics are separate from GPX trip files
(GPS Exchange Format; GPS means Global Positioning System); a rejected
diagnostic upload must not block a valid trip.

Start with the [operator guide](#operator-guide) to prepare/enroll a board,
read its screen, [look up a code](#looking-up-a-code), or troubleshoot delivery.
The [developer reference](#developer-reference) follows with delivery evidence,
catalog/protocol contracts, journal format, runtime/concurrency/memory details,
server internals and backups. Hardware acceptance limits remain explicit there.

## Operator guide

### What you need

- A T-Beam Supreme running **Stage 4**, the main trip-logger sketch in
  `firmware/04_trip_logger/`. Use a **working FAT32 SD (Secure Digital) card**
  for trip capacity (FAT32 is the required card filesystem format). Diagnostics
  revisions are mirrored to SD and LittleFS, the internal flash filesystem.
  After enrollment or migration, internal flash can send current health and
  events without a card, including active **E4001 (SD_UNAVAILABLE)**. If a
  mounted card stops responding, **E4001** reports that failure instead of
  claiming the card is full. New trips use the already-mounted internal flash
  queue once any current SD trip has ended; that current trip can fail with
  **E4006 (TRIP_WRITE_FAILED)**. A healthy enrolled flash copy can still upload
  these events. A failed flash
  journal likewise does not block a healthy enrolled card.
- A reachable ingest server and the same private bearer token and `DEVICE_ID`
  configured in the firmware. The device label is not authentication. See
  [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md) for setup and exact server commands, and
  [firmware setup](firmware/README.md) for board/flashing settings.
- **Deploy the server catalog before firmware that uses new codes/context.**
  Ingest validates against its installed catalog; an older server can reject
  new fields and the firmware may isolate/discard rejected history. Update and
  restart ingest, viewer and monitor together. The catalog reference below
  explains version changes; no installation method is assumed here.

### Snapshot revision enrollment and recovery

A **snapshot** is the board's current health report. Its `status_sequence` is
a device-wide revision that must keep increasing across boots. The **server
high-water** is the server's highest accepted `status_sequence` for that
`DEVICE_ID`; a missing device row or null revision means zero. There is no web
endpoint for discovering it. Read it from the ingest database using the
read-only command in [DEPLOY_DEBIAN.md: one-time diagnostics enrollment](DEPLOY_DEBIAN.md#one-time-diagnostics-enrollment).
If that read fails, resolve the error; do not substitute zero.

Both SD and internal flash store a revision. Either can be **ahead of the server**, because
firmware saves a revision before sending its snapshot. Before enrollment or
recovery, the value chosen for `DIAGNOSTICS_STATUS_HIGH_WATER` must be **at least
the server high-water and the highest stored revision on either mounted journal**.
A server or card read alone is insufficient for recovery. You can inspect SD
on a computer using the script below; internal flash cannot be read that way.
A refused enrollment prints `local minimum high-water <value>` on serial,
including both loaded copies and any revision reserved this boot. Use at least
that value and a fresh server read before trying a higher enrollment ID. If
that minimum was already met, check journal/NVS errors rather than raising it.

Enrollment is one-use: core 0 saves and verifies the ID in `diag/enroll` in
**NVS (non-volatile storage, the ESP32's settings memory)** before calling
`restoreStatusSequence(highWater)` under the journal lock. Enrollment attempts
every loaded journal and needs at least one verified save plus the separate
NVS recovery marker. Failed/absent stores are marked stale before it succeeds.
A restore below either loaded stored revision
is refused **after the ID has been consumed**. Raising only the ID while keeping
that too-low value repeats the failure and consumes another ID. Fix the lower
bound or storage problem first; only then retry with a higher ID and reboot.

1. Read the server high-water for the exact `DEVICE_ID`. With the tracker
   powered off, inspect the card's revision using the read-only procedure below
   and account for the internal-flash minimum printed if enrollment is refused.
   Keep the tracker off until choosing the value, so its allocator cannot advance
   after your inspection. Zero is appropriate only for verified fresh state,
   never as a guess after a failed read or unexplained metadata loss.
2. In private `firmware/04_trip_logger/secrets.h`, set
   `DIAGNOSTICS_STATUS_HIGH_WATER` to the verified lower bound above and
   `DIAGNOSTICS_ENROLLMENT_ID` to a positive value greater than **every ID
   previously used on this board**. Both must fit 0..2^63-1 (the ID cannot be 0).
   The example's `0ULL`/`1ULL` is only for verified fresh state. `ULL` means an
   unsigned long long integer in C++. Follow the flashing procedure in the
   deployment runbook; a used board normally keeps erase **Disabled**.
3. Let the normal awake network cycle finish eligible trip uploads, then check
   the viewer's **Device health** page for a newly received current diagnostics
   snapshot from this boot. GPX contact by itself does not prove enrollment succeeded.
4. **Remove both defines and reflash with Erase All Flash Before Sketch Upload
   set to Disabled.** Editing the header alone does not change the board's
   installed firmware. Normal boots then allocate from the existing journal metadata.

An empty/replacement card automatically adopts a current internal-flash copy;
no enrollment is needed for an ordinary card replacement when that copy is
healthy. This keeps the existing device identity and never resets its revision.
For recovery after losing all current copies or a confirmed snapshot conflict,
preserve the existing metadata, re-read the server and inspect the card before
choosing the recovery value. Never silently reset the server high-water or treat a
replacement card as a new device identity. A conflict suspends revision
allocation and blocks diagnostics in memory; explicit recovery with a new ID,
verified lower bound and reboot is required. The normal cleanup above applies
again after a successful current snapshot.

#### Admin high-water recovery

Use this only to repair a confirmed poisoned high-water or incorrect enrollment.
Ordinary card replacement with a current internal-flash copy needs no admin reset.
Stop the tracker first and preserve a backup of `events.db` and the SD metadata.
Read the current server high-water and inspect the stored revision bounds using
the procedures above, including the internal-flash minimum from serial. Choose
a replacement high-water at least as high as both local copies,
with room for another revision, then explicitly re-enroll with a new
enrollment ID and that value if the allocator needs recovery. Do not guess or
use the signed-integer maximum.

From the server folder, run (replace each bracketed value):

```sh
docker compose exec mileage-ingest python /app/reset_high_water.py \
  --device-id <device-id> --expect-high-water <current-server-revision> \
  --high-water <replacement-revision>
```

The command compares the current value inside the write transaction and refuses
if another request changed it. It preserves journal history and last contact,
clears the old snapshot, and marks all current conditions unknown. No resolved
fault is inferred. A later snapshot must exceed the replacement value by at most
1,000,000. Reboot after re-enrollment, confirm a current snapshot in Device health,
then remove the enrollment defines and reflash with erase disabled as above.
The script accepts values from 0 through 9223372036854775806. This is an explicit
local administration action, never an upload-triggered reset.

Leaving enrollment firmware installed normally cannot reuse a consumed ID
while NVS survives. **Erase-all clears `diag/enroll`**: firmware still carrying
the defines can then enroll again using its compiled-in, possibly stale value.
That is why both removing the defines and reflashing with erase Disabled matter.

**Upgrading from v1.00:** boot once with the enrolled card and internal flash
mounted. The first allocation copies the highest revision to both journals;
later boots without the card send LittleFS health and history. Updating while
the card is missing leaves no internal-flash authority yet: insert the enrolled
card for one successful allocation, or explicitly re-enroll internal flash
against a fresh server read and the local bounds above.

Every request requires a check-in. A mounted journal that cannot load, save or
verify its revision is skipped for that attempt, just like an absent store.
Diagnostics continue if at least one current, enrolled, nonsuspended authority
successfully saves the revision and NVS records which stores missed it before
transmission. Unreadable event journals are skipped too, so SD read errors
cannot hide LittleFS or RAM events; unreadable history remains for recovery.
If no current authority saves, the allocator is suspended, or NVS fails,
**all diagnostics requests stop and back off**. Refusal does not mark another
copy stale; any persisted but unsent revision is a harmless gap. Restore the
failed store alongside a current copy, or re-enroll if the only remaining copy
is stale. Local journaling and GPX uploads keep their existing storage limits.

NVS records suspension and which store missed allocations. A stale store alone
cannot resume; mount a current copy with it to synchronize, or explicitly
re-enroll. A conflict on a card-out boot also blocks a card that missed the
conflict. A recovered store clears its stale bit only after it saves alongside
current authority. This assumes NVS and checked journal writes survive;
rolling back NVS, downgrading firmware, restoring old media images or corruption
of both metadata copies can defeat that protection.

**Erase-all on an enrolled board:** this erases LittleFS and NVS, including the
recovery witness `diag/revstate`, but leaves the SD card intact. When that key
is genuinely missing, firmware adopts the recovery epoch from ready,
nonsuspended mounted journals and saves it to NVS before allocating. If several
ready journals are present, their epochs must agree. A suspended copy alone
stays blocked, an older epoch cannot authorize allocation after adoption, and
an NVS read error never counts as a missing key. An intact enrolled card can
therefore continue without re-enrollment and seed the erased internal flash.
With no ready copy, disagreeing epochs or suspension in the adopted epoch,
recover explicitly using the procedure above.

Erasing NVS loses evidence that an absent card was stale or missed a conflict.
A stale enrolled card may then issue low revisions: the server accepts history
and contact, but current health stays old until revisions exceed its high-water.
An exact high-water collision with different snapshot content suspends the
allocator and needs explicit recovery. Keep erase disabled for updates, and
check Device health after any erase. Do not clear server state to hide this.

<details>
<summary>Read the SD revision without changing it</summary>

With the tracker powered off, use a read-only mount or write-protected card
reader to copy `diag/ack-0` and `diag/ack-1` to a directory on your computer.
Inspect copies rather than editing the card. Save this Python code outside the
repository as `inspect_revision.py`, run `python inspect_revision.py`, and enter
that directory. It prints only slot validity, revision and local authority state;
it does not read journal events or personal data. The checks and newest-valid-
generation selection match `EventLog::load()` in `event_log.cpp`.

```python
from pathlib import Path
import struct
import zlib

directory = Path(input("Directory containing copied ack files: "))
valid = []
limit = (1 << 63) - 1
for slot in (0, 1):
    path = directory / f"ack-{slot}"
    if not path.exists():
        print(f"ack-{slot}: missing")
        continue
    data = path.read_bytes()[:80]  # read only; no writes to either copy
    if len(data) != 80:
        print(f"ack-{slot}: invalid length")
        continue
    generation, = struct.unpack_from("<Q", data, 8)
    segment, offset, next_segment = struct.unpack_from("<III", data, 16)
    revision, dropped, pending = struct.unpack_from("<QQQ", data, 32)
    discarded, = struct.unpack_from("<I", data, 56)
    crc, = struct.unpack_from("<I", data, 76)
    modern = data[:4] == b"ELG2"
    epoch = struct.unpack_from("<Q", data, 64)[0] if modern else 0
    suspended = bool(data[61]) if modern else not data[60] and revision != 0
    ok = (data[:4] in (b"ELG1", b"ELG2") and zlib.crc32(data[:76]) == crc
          and 0 < generation <= limit and generation % 2 == slot
          and segment <= 99999999 and offset <= 65536
          and 0 < next_segment <= 100000000 and revision <= limit
          and dropped <= limit and pending <= dropped
          and discarded <= 99999999 and data[60] <= 1
          and (not modern or (data[61] <= 1 and not (data[60] and data[61])
                              and epoch <= limit and epoch % 8 == 0)))
    print(f"ack-{slot}: {'valid' if ok else 'invalid'}")
    if ok:
        valid.append((generation, revision, bool(data[60]), suspended, epoch))
if not valid:
    raise SystemExit("No valid slot: lower bound unknown; investigate before recovery")
_, revision, enrolled, suspended, epoch = max(valid)
print(f"SD stored revision: {revision}; enrolled: {enrolled}; suspended: {suspended}; epoch: {epoch}")
```

A missing/invalid slot can coexist with a valid older slot after a torn write;
use the highest valid generation, not the largest revision found in arbitrary
bytes. If neither validates, or copying/reading fails, do not infer zero.
Distinguish verified unused media from lost/corrupt metadata before explicitly
rebuilding revision authority. Keep copies for that investigation.
An enrolled flag alone does not prove the card can allocate: the board's NVS
recovery epoch, suspension and stale-store mask must also permit it.

</details>

### What the screen shows

The **OLED (organic light-emitting diode) screen** starts in production mode
at each power-on. It normally sleeps. Cards are steady and last **30 seconds**
with the current default; they do not scroll or flash.

- A status card shows at boot, even before motion is known, unless the board
  is already judged moving. A finalized trip queues another card for when
  parked. Entering parked with unresolved faults also reminds once. Routine
  status cards blank when motion becomes moving.
- Each new accepted **error/critical continuing-condition episode** can show
  an automatic error card in any motion state, including while moving.
  Warnings/info and repeated observations do not wake another error card;
  the bounded queue can miss excess episodes. A historical reset notice is
  not an active episode.
- Tap **POWER** to request a status card in any motion state for its full
  duration. It shows `Sats: N` (satellites heard), or `Sats: --` when unknown,
  followed by `OK` when no faults are active or active warning-and-above codes,
  most severe first. Up to four slots fit; with more faults, the last slot is
  `+N`. `OK` describes the observed active list, not proof that every subsystem
  has been tested. An error card shows one large `E####`.
- While the board is running, **hold BOOT, then tap POWER** to toggle the live
  debug page. A two-second message confirms the change. POWER alone can overlay
  a status card in debug mode. Debug is not saved across power cycles; holding
  BOOT during power-up instead selects the chip's download mode.
- A long POWER press on USB leaves the board on and shows `USB power` /
  `Unplug USB` / `to switch off` for the normal card duration in either mode.
  Unplug USB, hold POWER for 1–2 s and release. Watch for `Switching off`,
  then a dark screen, and wait a few seconds for shutdown to finish. Pull
  the card **without pressing anything**. Any POWER press, even a tap,
  switches an off board on. A roughly six-second hold forces PMU power-off
  without closing files. If in doubt, use the 10-minute battery idle fallback
  and its conditions in the [card precautions](firmware/README.md#microsd-card).
  Failed displays and degraded startup skip the shutdown card.

Short-press actions need a successfully read power-management interrupt batch;
unknown reads cannot request a card or toggle debug. Degraded startup does not
run display updates or normal short-press actions. Either display adapter
failing disables both display paths for that boot; recording can continue.

The layout was approved on the bench on **2026-10-21**. Daytime readability
is accepted as a panel limit; OLED current was deliberately not measured.
Road checks, including production-card behavior, remain open in #206; #158's
remaining debug-screen checks were moved there. These are recorded hardware
decisions, not claims established by host tests.

### Looking up a code

Open the viewer's **Device health** page after logging in. Current conditions
are separate from history: an old event does not prove a fault is still active.
The page gives catalog explanations and recovery hints; unknown codes have no
invented severity. See the developer runtime-hook table for exact triggers.

This complete index is generated from catalog v6's **48 definitions**, with
each producer checked against Stage 4. A catalog entry alone does not prove
firmware emits it. Symbols use subsystem abbreviations: **PMU** is the power
management unit, **GPS** the satellite positioning receiver, **RTC** the real-time
clock, **UTC** coordinated universal time, **FS** filesystem, **NET** networking,
and **ACK/NACK** acknowledgment/negative acknowledgment. LittleFS is the internal
flash filesystem. A mutex is a lock protecting shared access.

Journal policy: `transitions` means raise/repeat/clear for an ongoing condition;
`per_occurrence` means a one-off history notice; `counter` means a boot-local
total instead of one event per occurrence. "Yes" means a current producer path
exists, not that every occurrence is guaranteed durable or already road-tested.

| Code | Symbol | Severity | Journal policy | Stage 4 emits today? |
|---|---|---|---|---|
| E1000 | `BOOT` | info | `per_occurrence` | Yes |
| E1001 | `FS_MUTEX_FAILED` | critical | `transitions` | No; catalog only (lock failure restarts) |
| E1002 | `NET_TASK_FAILED` | error | `transitions` | No; catalog only (serial failure message) |
| E1003 | `WATCHDOG_RESET` | error | `per_occurrence` | Yes |
| E1004 | `BROWNOUT_RESET` | warning | `per_occurrence` | Yes |
| E1005 | `PANIC_RESET` | error | `per_occurrence` | Yes |
| E1006 | `ID_STATE_FAILED` | warning | `transitions` | No; catalog only (counter failure uses zero) |
| E1007 | `NET_STACK_LOW` | warning | `transitions` | Yes |
| E2001 | `PMU_INIT_FAILED` | critical | `transitions` | Yes |
| E2002 | `BATTERY_LOW` | warning | `transitions` | Yes |
| E2003 | `BATTERY_CRITICAL` | error | `transitions` | Yes |
| E2004 | `CHARGE_HOT` | warning | `transitions` | Yes |
| E2005 | `CHARGE_COLD` | warning | `transitions` | Yes |
| E2006 | `CHARGE_TEMP_UNREADABLE` | error | `transitions` | Yes |
| E2007 | `PMU_STARTUP_RECOVERY` | warning | `per_occurrence` | Yes |
| E2008 | `CHARGE_CONTROL_FAILED` | error | `transitions` | Yes |
| E2009 | `BATTERY_POWER_OFF` | info | `per_occurrence` | Yes |
| E3001 | `GPS_NO_DATA` | error | `transitions` | Yes |
| E3002 | `GPS_NO_FIX` | warning | `transitions` | Yes |
| E3003 | `GPS_CONFIG_NACK` | warning | `per_occurrence` | Yes |
| E3004 | `GPS_CONFIG_TIMEOUT` | warning | `per_occurrence` | Yes |
| E3005 | `GPS_TRIP_FIX_LOST` | warning | `transitions` | Yes |
| E3006 | `UTC_UNAVAILABLE` | warning | `transitions` | Yes |
| E3007 | `RTC_UNAVAILABLE` | warning | `transitions` | Yes |
| E3008 | `RTC_TIME_INVALID` | warning | `transitions` | Yes |
| E3009 | `GPS_ANTENNA_SUSPECT` | warning | `transitions` | Yes |
| E4001 | `SD_UNAVAILABLE` | warning | `transitions` | Yes |
| E4002 | `LITTLEFS_UNAVAILABLE` | error | `transitions` | Yes |
| E4003 | `NO_RECORDING_STORAGE` | critical | `transitions` | Yes |
| E4004 | `STORAGE_LOW` | warning | `transitions` | Yes |
| E4005 | `STORAGE_FULL` | critical | `transitions` | Yes |
| E4006 | `TRIP_WRITE_FAILED` | critical | `transitions` | Yes |
| E4007 | `TRIP_RECOVERY_FAILED` | error | `per_occurrence` | Yes |
| E4008 | `EVENT_WRITE_FAILED` | error | `transitions` | Yes |
| E4009 | `EVENTS_DROPPED` | warning | `per_occurrence` | Yes |
| E4010 | `TRIP_RECOVERED` | info | `per_occurrence` | Yes |
| E5001 | `HOME_AP_ABSENT` | info | `counter` | Counter only; snapshot, no journal event |
| E5002 | `WIFI_JOIN_FAILED` | warning | `transitions` | Yes |
| E5003 | `UPLOAD_TRANSPORT_FAILED` | warning | `transitions` | Yes |
| E5004 | `UPLOAD_AUTH_FAILED` | error | `transitions` | Yes |
| E5005 | `UPLOAD_TOO_LARGE` | error | `transitions` | Yes |
| E5006 | `UPLOAD_INVALID` | error | `transitions` | Yes |
| E5007 | `UPLOAD_CONFLICT` | error | `transitions` | Yes |
| E5008 | `UPLOAD_SERVER_FAILED` | warning | `transitions` | Yes |
| E5009 | `UPLOAD_HTTP_OTHER` | warning | `transitions` | Yes |
| E5010 | `EVENT_ACK_INVALID` | error | `transitions` | Yes |
| E6001 | `DISPLAY_UNAVAILABLE` | warning | `transitions` | Yes |
| E7001 | `DEVICE_SILENT` | warning | `transitions` | No; server monitor only |

E4009 loss notices come from EventLog itself. Journal storage failures increment
its internal `storageFailures` counter and raise E4008 through the consumer's
normal transition producer. A healthy fallback store does not clear a failed
store; see [journal failure reporting](#journal-failure-reporting-e4008).
E7001 is derived on the server from time since accepted contact, not emitted by
the board. E5001 counts successful scans that miss the configured home access
point; it is expected away from home and does not flood journal history.

For Prometheus metric names, labels and meanings, use the
[Grafana metric reference](server/grafana/README.md); the code index is not a
second metric reference.

### Basic troubleshooting

| Symptom | What to check |
|---|---|
| No Device health entry | A successful GPX upload with a valid `X-Device-Id`, or an accepted diagnostics request, creates contact. Verify the configured identity, server reachability and upload result. Legacy GPX uploads without identity are not assigned to a guessed device. |
| Contact exists, but no current snapshot | GPX contact does not establish diagnostics health. Check for a current enrolled SD or LittleFS copy, NVS suspension/stale state, and the one-time upload-workspace allocation warning. An upgrade while the card was absent needs it once or explicit enrollment. |
| Diagnostics stay local while trips upload | Eligible completed GPX files always take priority, even after a failed trip attempt. Diagnostics use the next eligible awake network opportunity and independent retry backoff. Check at least one current enrolled journal can persist revisions, NVS is working, and the serial warning `diagnostics upload disabled: PSRAM allocation failed`; PSRAM is the board's external working memory. |
| Enrollment/recovery is refused or never produces a current snapshot | Check the server high-water, SD stored revision and serial `local minimum high-water` (includes internal flash) before consuming another ID. Failed attempts consume IDs. A snapshot conflict needs explicit recovery and reboot; do not clear server state to hide it. |
| History arrives but current health stays old | Lower-but-valid revisions can deliver history/contact without replacing current health. Compare revision bounds and use the recovery procedure above. An identical retry also does not refresh snapshot age. |
| An old fault is in history, or a condition says unknown | History is not current health. A reboot or omitted/unobserved condition is unknown, not measured recovery. Use the current snapshot and receipt age. |
| The screen is dark | Production normally sleeps between cards. Try POWER while running. E6001 means display failure disabled both display paths for this boot; missing pixels alone cannot diagnose the cause. |
| Serial prints `DEGRADED:` | No trips are being recorded and no diagnostics uploads run in that boot. Address the reported power/storage startup failure; bounded retries restart the board after recovery. See [degraded startup service](#degraded-startup-service-126). |
| Viewer says Diagnostics unavailable (503) | The database read, schema or lock failed; inspect the viewer warning and server storage/service state. This is unavailable evidence, not an empty healthy result. |

Successful diagnostics requests do not print a serial success line. Confirm
them in ingest logs or Device health's snapshot receipt, rather than assuming
a GPX `200` proves diagnostics delivery. Keep tokens and personal data out of
shared logs and troubleshooting reports.

### Optional monitoring and notifications

The optional monitor watches every device that has checked in. It can send
self-hosted ntfy phone notifications for eligible current faults and for server
silence (seven days by default), and can feed external Grafana/Prometheus,
Graylog or Loki. Outputs are off unless configured; setup and private settings
are in [DEPLOY_DEBIAN.md Step 11](DEPLOY_DEBIAN.md#step-11--optional-device-notifications).
The [Grafana README](server/grafana/README.md) is the metric reference.

Default fault notifications require a snapshot received within 24 hours,
error-or-higher severity and a five-minute hold (critical faults bypass the
hold). Silence uses server contact time, including accepted GPX, independently
of fault severity/hold. Notification limits, reminders and explicit recovery
rules reduce repeated alerts; a reboot is not a resolution. Delivery is at
least once, so duplicates are possible. On iOS, instant push from self-hosted
ntfy needs its public wake-up relay; Android does not (see the runbook).

The monitor waits 60 seconds **after each completed tick**. Slow exports can
delay database reads, alert checks and metrics refresh. Separate sink failure
handling does not provide separate timing. Monitoring does not wake the tracker
or replace its journal; the heartbeat uses an existing awake network opportunity.

## Developer reference

### Terms used in this reference

The operator guide introduces SD, NVS, OLED, PMU, GPS, RTC, UTC and ACK/NACK.
Additional abbreviations below: **CRC/CRC32** = cyclic redundancy check (a
32-bit corruption check); **FAT/FAT32** = File Allocation Table filesystem;
**DRAM** = dynamic RAM; **PSRAM** = pseudo-static RAM (external working memory);
**VFS** = virtual filesystem interface; **FS_LOCK** = the project's filesystem
lock wrapper; **FSM** = finite-state machine (trip lifecycle states); **ABI** =
application binary interface (layout/alignment conventions); **IRQ/ISR** =
interrupt request / interrupt service routine; **I/O** = input/output;
**I2C** = Inter-Integrated Circuit peripheral bus; **SPI** = Serial Peripheral
Interface; **UART** = Universal Asynchronous Receiver/Transmitter; **GPIO** =
general-purpose input/output; **ADC** = analog-to-digital converter; **SOC** =
state of charge; **VBUS** = USB supply voltage; **BCD** = binary-coded decimal;
**SNTP** = Simple Network Time Protocol; **NMEA** = National Marine Electronics
Association receiver sentences; **UBX** = u-blox binary receiver protocol;
**GSV** = satellites-in-view sentence; **RMC** = recommended minimum navigation
sentence; **NAV-PVT** = navigation position/velocity/time message; **AGC** =
automatic gain control; **RF** = radio frequency; **HDOP** = horizontal dilution
of precision; **C/N0** = carrier-to-noise density ratio; **HTTP** = Hypertext
Transfer Protocol; **JSON** = JavaScript Object Notation; **GELF** = Graylog
Extended Log Format; **TLS** = Transport Layer Security; **DNS** = Domain Name
System; **DMA** = direct memory access; **PII** = personally identifiable
information; **CI** = continuous integration. A **seqlock** is a versioned
publication scheme: readers detect a concurrent write and reject/retry a copy
instead of waiting for a writer lock. Code identifiers and units retain their
literal spelling in the reference.

### Recorded delivery evidence and acceptance limits

This is the reference for the diagnostics framework (issues #117 through
#130 and the later items in the delivery map below). Both halves are
implemented: the Stage 4 firmware journals error codes, shows them on the
production screen and uploads them after trips; the server stores them,
shows them on the viewer's Device health page and can optionally notify or
export them. What remains is hardware acceptance on the T-Beam Supreme
(#130 and the shakedown issues).

The first real diagnostics upload succeeded on 2026-10-21 (UTC), using
Stage 4's debug build and the SD journal. After the last GPX upload, two
`POST /v1/device-events` requests returned HTTP 200 at 04:36:43Z and
04:36:44Z. The server created `data/diagnostics/events.db` with all 115
pending events. Device health showed recent contact and snapshot receipt,
storage backend `sd`, zero dropped events, a boot counter and no active
conditions. Its history included the earlier E3009 antenna condition,
raised and cleared; the E5001 access-point-absence counter was 6 for this
boot. Successful diagnostics uploads print nothing on serial: confirm
them in the ingest log or Device health. That build showed
`Firmware: Not reported` because it predates version reporting; builds
stamped with `tools/stamp_firmware_version.py` report their version number
plus Git commit.

This confirms SD diagnostics delivery in the earlier debug build. Trip
uploads with debug mode off and server-down retry were verified separately
on 2026-10-21 (UTC): two `HTTP -1` failures kept the trip queued, then HTTP
200 after ingest restarted deleted the local file. On the bench on
2026-11-01 (UTC), a server error (5xx) kept trips queued with E5008 and they
uploaded after recovery; a wrong token gave E5004 on a small file and a broken
send (E5003, `HTTP -3`) on a roughly 3 MB file; retried diagnostics batches
created no duplicate events. LittleFS uploads and HTTP 413 remain untested on
the Supreme. Setup steps are in
[README.md](README.md) and [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md) Step 8.

The tracker normally runs unattended in a car. A serial console cannot explain
last week's missed trip. Diagnostics therefore have two separate parts:

- An **event journal** records what happened, including failures that recovered
  before the device next reached home WiFi. It is bounded so that diagnostics
  cannot fill recording storage indefinitely.
- A **current-health snapshot**, sent as a check-in, says which conditions are
  active now. Uploading an old error does not mean the device is still broken.
  Alerts use fresh snapshots, not just the newest event in the journal.

Neither part replaces trip files. A broken display, unavailable diagnostics
database, or rejected event upload must not stop an otherwise valid trip upload.
No diagnostic record contains credentials, SSID/BSSID, coordinates, trip paths,
raw HTTP bodies, or arbitrary error strings. The catalog permits only specific
numeric measurements and fixed enumerated values in context.

### Delivery map and prerequisites

- **#117:** catalog, protocol v1 validation, generator, host compile test, CI
  drift check, and this reference.
- **#118:** authenticated ingest endpoint, SQLite storage, GPX contact updates,
  snapshot ordering, and packaging the shared Python code into both services.
- **#119:** shipped Device health at `/device-health`, with current conditions
  and paginated history behind an authenticated viewer session.
- **#120:** shipped independent monitor service and optional ntfy fault notifications.
- **#121:** shipped server-side device-silence detection; heartbeat integration is #125.
- **#122:** implemented board-independent EventLog and host fault tests: bounded
  producer queue, CRC segments, durable acknowledgments/loss accounting and
  snapshot revision allocator. Part A of #124 now supplies its mounted-storage
  adapters, boot identity/counter and boot notices in Stage 4.
- **#123:** implemented pure header-only DisplayModel, bounded episode queue,
  structured card content and host policy tests for the production screen.
- **#124 part A:** implemented mounted-filesystem adapters, fresh boot identity/
  NVS boot counter, cooperative core-0 service, LittleFS trip reserve, shutdown
  sealing and boot/reset notices. **Part B is implemented:** runtime fault hooks
  and their host transition/protocol tests (see the hook list below).
  Supreme storage timing, power-cut and road acceptance remain hardware work.
- **#125:** implemented core-0 diagnostics upload worker, current snapshots,
  explicit revision enrollment/recovery, complete ack verification, bounded
  rejection isolation, trip priority and independent retries. Journal revision
  enrollment is required before the first diagnostic request (see below).
- **#126:** implemented bounded degraded service loops for PMU/LoRa-rail and
  trip-storage startup failures, with local journaling and restart on recovery.
- **#127:** implemented production-only SH1106 adapter, power save, POWER
  short-press status and latched E6001 transport failure. Layout was approved
  on the bench on 2026-10-21; daytime readability is accepted as a panel limit,
  OLED current was deliberately not measured, and road checks remain open (#206).
- **#128:** implemented diagnostics-only PCF8563 boot timestamps, bounded
  GPS/SNTP RTC setting and clock-confidence/runtime tests; the RTC keeps time
  across PMU power-off (bench, 2026-10-20).
- **#129:** shipped consistent diagnostics database and monitor-state backups/restores.
- **#130:** induced-fault, power-cut, backlog and display shakedown on hardware.
- **#134:** shipped Prometheus `/metrics` for the owner's external Grafana (not bundled).
- **#135:** shipped GELF export to the owner's external Graylog (not bundled).
- **#136:** shipped optional push to an external Loki (not bundled).
- **#137:** shipped installer output selection; see the deployment runbook for setup.
- **#151:** E2007 catalog entry, retained Stage 4 PMU startup result and boot
  journal emission in normal and degraded startup are implemented. This
  one-off notice stays in journal/server history; production cards list active
  conditions under #123/#127.
- **#164:** E3009 antenna warning uses MON-RF gain and fresh GSV strength in
  every build; the RF serial line and temporary OLED page stay debug-only.

Host tests prove catalog consistency and policy behavior. They do not prove bus
wiring, power rails, charging safety, FAT power-cut recovery or road behavior.

### Catalog and on-screen codes (#117)

`server/common/diagnostics/catalog.json` is the source of truth. It lives under
`server/` because deployment copies that directory to the host. The generated
header is `firmware/04_trip_logger/diag_catalog.h`: Arduino copies the sketch
into a temporary build directory, so an include reaching outside the sketch
with `../` would fail there. The generator needs only Python's standard library;
the header needs only standard C++14 headers, with no Arduino dependency.

Transmit an integer such as `4005`; display `E4005`. Ranges are reserved by
subsystem: 1xxx runtime, 2xxx power, 3xxx GPS/time, 4xxx storage, 5xxx network,
6xxx display and 7xxx server monitoring. Codes and symbols must never be reused
for another meaning. Severity is one of `info`, `warning`, `error`, `critical`.
The server derives severity and notification eligibility from its catalog,
never from a client-supplied severity.

Each definition has `code`, `symbol`, `severity`, `oled`, `title`, `explanation`,
`recovery`, `context`, `notify`, and `journal`. `context` maps allowed keys to
`integer` with inclusive `min`/`max`, `enum` with fixed `values`, or `boolean`.
There are at most eight allowed context keys per code; keys are optional in an
event because a measurement may be unavailable. Unknown keys are rejected.
Millivolts, milliseconds, bytes, and milli-degrees Celsius are integer units;
there are no unbounded strings or floating-point values in context.

Journal policy describes the producer behavior:

- `transitions`: a continuing condition uses `raise`, bounded `repeat`, then
  `clear`. Repeated checks do not create a new episode or a record every loop.
- `per_occurrence`: a one-off `notice`, with no active episode. This includes
  boot/reset history, configuration failures and recovery/loss reports.
- `counter`: increment a boot-local check-in counter instead of journaling each
  occurrence. **5001 HOME_AP_ABSENT is counter-only.** Normal driving away from
  home must not flood the journal. The catalog retains its OLED-text metadata.

**2007 PMU_STARTUP_RECOVERY** is a warning with `notify=false` and
`journal=per_occurrence`. It records a boot needing PMU/RTC bus recovery or
startup retries, including unsuccessful startup, as one `notice` with a null
`episode_id`. A clean first-attempt boot produces no event. This is a historical
fact, so it cannot appear in snapshot `active` or `observed` lists and cannot
trigger ntfy. It remains visible in Device health history and event exports.
Continuing PMU initialization failure belongs to **2001 PMU_INIT_FAILED**.

Its bounded context is `clock_count` (0-27 completed recovery clocks summed
across attempts, excluding STOP setup), `attempt_count` (1-3 startup attempts,
including attempts that fail before a PMU probe), `recovery_state`
(`not_needed|recovered|sda_stuck|scl_stuck` from the last completed recovery),
`pmu_ready` and `wire_end_failed` (booleans). The final recovery state alone
does not prove PMU readiness. Stage 4 retains the complete `PmuStartupResult`
in RAM; `makePmuStartupEvent` prepares these fields without I/O. #122/#124 must
emit the notice once the journal is available; part A of #124 now does so.
Failed PMU startup also journals the retained result in the degraded loop
when LittleFS is usable (otherwise RAM). OLED failure reporting is implemented by #127.

`notify` means eligible, not an instruction to send a notification immediately.
Info codes, 2007 PMU_STARTUP_RECOVERY, the high/low charge-guard warnings
(2004/2005) and 3002 GPS_NO_FIX
(normal in a garage or parking structure) are ineligible by
default. The unreadable-temperature error 2006 is eligible. Error reset notices
remain history; #120's fresh-current-fault rule does not alert on historical
notices by themselves. Silence code 7001 is derived by the server monitor;
the protocol rejects 7xxx codes from devices so one cannot forge it.

Catalog OLED text is printable ASCII, at most **three lines of 21 characters**.
These remain catalog validation rules in **catalog v6**; production cards do
not render the `oled` field. The historical compact-text geometry is U8g2
`u8g2_font_5x8_tf`, with 5-pixel advances and 8-pixel rows: 21 characters
occupy 105 pixels. Never scroll or shrink text to work around a catalog
violation. The C++ table includes code,
symbol, severity, OLED lines, title, notify and journal policy; longer recovery
and context definitions stay in the JSON for the server.

#### Updating the catalog

From the local repository root:

```sh
python tools/generate_error_catalog.py
python tools/generate_error_catalog.py --check
python -m unittest server.tests.test_diagnostics_catalog
```

`catalog_version` is an explicit integer (initially 1), independent of the
protocol's `schema_version`. Increment it when changing any published code
definition, regenerate the header, and append the new version and definitions'
SHA-256 fingerprint to `PUBLISHED_CATALOGS` in the tests. Obtain the fingerprint
from `kCatalogDigest` in the generated header. Preserve previous entries. This
records intentional evolution and catches edits without a version bump; it is
not a substitute for reviewing that existing code meanings have not been reused.
CI checks exact header bytes, and tests verify JSON/header version agreement.

Catalog v5 adds **2008 CHARGE_CONTROL_FAILED**, an error with `notify=true`
and `journal=transitions`. It reports three consecutive charger command/read-back
failures, with attempts 30 seconds apart, and clears only on a confirmed intended
state. Its context is empty. E2004/E2005 now say a temperature-driven pause is
required, rather than claiming the charger stopped; E2008 reports persistent
failure to confirm that command. Earlier published fingerprints are retained.

Catalog v6 adds **2009 BATTERY_POWER_OFF**, an INFO, non-notifying,
`per_occurrence` notice, and optional battery measurements to **1000 BOOT**.
Both accept `battery_mv` (1-6000 mV) and `battery_pct` (0-100 PMU charge
percentage). Failed/disconnected/invalid measurements are omitted, never
invented as zero; a successfully read 0% is valid. E2009 also carries `reason`
(`idle_battery`, `battery_critical` or `user_long_press`). Each event uses at
most three context keys, within the existing eight-key/502-byte event limits.
The catalog OLED text fits the usual limits; INFO history never produces a
production fault card or notification. Existing Device health history and
monitor exports render the new catalog fields without a new viewer interface.

**Upgrade the server before installing this firmware.** A v6 server accepts
older firmware, including BOOT without battery fields. An older server rejects
the new BOOT keys and context-bearing E2009, even though protocol/schema v1 is
unchanged. Rejected batches can enter the firmware's existing bounded isolation
path; those readings are not guaranteed to be delivered later. Update/restart
ingest, viewer and monitor together so all load catalog v6. No database migration
or new service dependency is needed.

### Protocol v1 (#117 contract, #118 and #125 integration)

The endpoint is `POST /v1/device-events` on ingest port 8090. It uses
the existing bearer token and an `X-Device-Id` header. Firmware already defines
`DEVICE_ID` in `secrets.h` and sends it on GPX uploads; diagnostics uploads use
the same identity and token. The public example is a non-personal identifier
such as `tracker-1`. Header identity is validated separately and is not repeated in
every record. It is a label, not authentication or a secret. In practice there
is one tracker, but retaining the label prevents future data collisions.

`common.diagnostics` is a stdlib-only Python package. With `server/` on
`sys.path`, consumers import `common.diagnostics`; importing
`server.common.diagnostics` also works from the repository root. The API is:

```python
from common.diagnostics import parse_request, validate_device_id

device_id = validate_device_id(header_value)
body = parse_request(bounded_body_bytes)
```

`validate_request(body)` validates an already-decoded object without changing it.
`parse_request(bytes)` additionally validates UTF-8, raw byte size and JSON
syntax, including duplicate keys. Both reject unknown fields. Validation errors
describe fields without echoing untrusted values. They do not authenticate,
persist data, establish snapshot order, or emit HTTP responses.

The entire raw request is at most **32 KiB**, including whitespace, and carries
at most **64 events** and 64 active conditions. #118 must count bytes while
reading the stream, including requests with no `Content-Length`; checking the
buffer after an unbounded read is insufficient. Reject oversize bodies with
413. Strict JSON rejects non-finite numbers, booleans in integer fields,
duplicate keys, invalid encoding and unsupported schema versions.

A minimal valid example (synthetic identifiers, unset device clock):

```json
{
  "schema_version": 1,
  "checkin": {
    "boot_id": "00000000000000000000000000000001",
    "status_sequence": 1,
    "uptime_ms": 1500,
    "active": [
      {"code": 4005, "episode_id": "00000000000000000000000000000001:1"}
    ],
    "observed": [4001, 4005],
    "counters": {"5001": 3}
  },
  "events": [
    {
      "schema_version": 1,
      "catalog_version": 1,
      "firmware_version": "test-1.0",
      "boot_id": "00000000000000000000000000000001",
      "boot_counter": 1,
      "sequence": 1,
      "code": 4005,
      "transition": "raise",
      "episode_id": "00000000000000000000000000000001:1",
      "uptime_ms": 1200,
      "utc_ms": null,
      "time_source": "unknown",
      "sync_age_ms": null,
      "context": {"backend": "sd", "free_bytes": 0}
    }
  ]
}
```

#### Identifiers, limits and record meaning

- `device_id`: 1-32 lowercase ASCII letters/digits, `_` or `-`, starting with a
  letter/digit. No personal names or network addresses.
- `boot_id`: exactly 32 lowercase hexadecimal characters, representing a new
  random 128-bit value at every boot, including each deep-sleep wake. It is not
  a timestamp and cannot be sorted to determine boot order.
- `sequence`: positive per-boot event counter. The server key is
  `(device_id, boot_id, sequence)`. `boot_counter` is an NVS diagnostic counter,
  not the record identity or an authoritative source of ordering.
- `episode_id`: `<boot_id>:<raise-sequence>` with no leading zero in the
  sequence. `raise` starts its own episode; `repeat`/`clear` refer to that same
  boot's earlier raise. `notice` requires null. An event may arrive before its
  raise due to retained-segment loss; the server retains the orphan for history.
- Counters, uptime and sync age are integers from 0 through 2^63-1; sequence and
  status revision start at 1. This fits SQLite INTEGER. `catalog_version` is
  1-65535. Firmware versions are 1-32 ASCII letters/digits/`.`, `_`, `+`, `-`,
  starting with a letter/digit; use build labels, never personal branch names.
- `utc_ms` is null, or Unix milliseconds from 0 through 253402300799999 (end of
  year 9999). `time_source` is `unknown`, `rtc`, `gps` or `sntp`. Unknown time
  requires both UTC and sync age to be null; other sources require UTC. A null
  sync age with a known source means its age is unavailable. Numeric validity
  does not prove clock accuracy. Never substitute uptime for UTC.
- A canonical compact JSON event is at most **502 bytes**. Its SD frame appends
  a TAB, eight CRC32 hex digits and LF, making the complete frame at most 512
  bytes. CRC covers the exact JSON bytes, excluding the delimiter/trailer.
  Producers must budget the complete encoded event, including escaping; context
  schema validity alone does not guarantee it fits. Request whitespace does
  not count toward an individual canonical event's frame size.

The validator accepts future unknown device codes in 1000-6999, even when their
`catalog_version` is newer. Unknown codes must have **empty context** until the
server knows their schema. The server retains them and displays `Unknown E####`
without inventing severity or enabling notifications. This deliberately trades
some forward-compatible context for the strict PII allowlist: deploy the server
catalog first when adding context-bearing codes. A known code's context and
journal policy are checked against the installed catalog. Incoming severity,
notify flags and free text are not protocol fields.

#### Snapshots and reboot ordering

The required check-in fields are `boot_id`, `status_sequence`, `uptime_ms`, and
`active`. Each active entry has only `code` and `episode_id`; codes are unique.
Only continuing conditions can be active. Optional fields are `firmware_version`,
`boot_counter`, `storage_backend` (`sd|littlefs|ram|none`), `pending_events`,
`dropped_events`, `counters`, and `observed`. Counts are nonnegative integers.
`counters` maps decimal catalog counter codes (currently only `"5001"`) to
boot-local totals, with at most 16 entries. They are not independent events.

Stage 4 sends `firmware_version`: `firmware/VERSION` plus `+` and the ten-character
Git commit (for example `v0.05+3f9a1c07be`), with `-dirty` for tracked edits. Run
`python tools/stamp_firmware_version.py` before compiling; without the generated
header it reports `unversioned`. Runtime debug does not change the version.
The boot banner, new events and current snapshot use the same 1–32 character protocol token.

`observed` is an optional, unique list of at most 64 condition codes evaluated
in this boot. When present, every active code must be included. An observed but
inactive code is clear. Omitted codes are unknown, not proof of recovery. When
`observed` is absent, only listed active conditions are known; absence cannot
resolve another condition. Firmware should send `observed` for useful recovery
reporting. This small extension makes the required post-reboot unknown state
representable instead of treating an untested backend as healthy.

**Implementation choice for #118/#122/#125:** `status_sequence` is a device-wide,
durably increasing snapshot revision, continuing across boots. Allocate/persist
it before transmitting a new snapshot; reuse it only when retransmitting the
identical snapshot. The server keeps the high-water revision per device. Lower
revisions can deliver journal history and refresh contact, but cannot change
current health. Equal revision with different snapshot content is a conflict;
an identical retry does not refresh snapshot observation time. This prevents a
delayed, previously unseen old boot from clearing a newer boot's faults. Random
boot IDs, receipt order and the diagnostic boot counter cannot guarantee that.

A new snapshot may advance at most **1,000,000** above the stored high-water
(zero when no snapshot has been accepted). The mirrored allocator advances by one per
drain opportunity, normally at most once per 30-second awake network tick, so
this allows about 347 days of continuous allocations between contacts. Normal
re-enrollment starts from the freshly read high-water and advances by one. If
restored server state is further behind either journal, use explicit admin recovery.
An excessive jump is refused only as a snapshot: current health and high-water
stay unchanged, a warning is logged, and validated journal events/contact still
commit with a normal **200 and exact event acknowledgment list**. A snapshot-only
request gets `accepted: []`. This follows stale-snapshot semantics: firmware
does not wedge its queue. Returning 400 would keep retrying the bad check-in;
409 on an empty check-in/probe suspends the firmware revision allocator until
explicit recovery. This bound limits one-request poisoning; a holder of the
shared token can still forge events or repeatedly advance revisions. Per-device
credentials remain a separate hardening option.

Each allocation adds one durable 80-byte metadata-slot write on each mounted
journal, including wear-levelled LittleFS. Allocations occur at most about once
per 30-second awake network tick while home; scans/events do not write revisions.
Segment reservations, acknowledgments and loss accounting also write metadata.
NVS holds a recovery epoch, suspension latch and stale-store mask, not the
per-snapshot revision. It is written on enrollment/suspension, when a store
first misses allocations or fails a write, and after synchronization clears its
stale bit. Steady allocations with the same available stores do not write NVS.
Host tests verify operation counts, not physical flash wear or power-cut durability.
A working allocator with lower-but-valid revisions can still upload journal
history and refresh contact; current health advances only above the server
high-water mark. Missing, unenrolled or failed revision authority instead
prevents snapshot creation and therefore **all diagnostics requests**: every
request requires a check-in. Local journaling and GPX uploads continue within
their existing storage limits. Recovery must explicitly restore the allocator
with the same identity; never silently reset the server high-water mark. See
[snapshot revision enrollment and recovery](#snapshot-revision-enrollment-and-recovery).

On a newer boot's accepted snapshot, old-boot conditions become unknown unless
the new boot observes them. Do not generate a recovery notification merely from
a reboot. Old events remain history. #118 must apply these rules in the same
transaction as the batch; the stateless validator cannot enforce ordering.

#### Acknowledgments and retries (#118/#125)

Commit the entire valid batch in one transaction, then return 200 with exactly
the accepted IDs: `{"accepted":[{"boot_id":"...","sequence":1}]}`. The
request's device header supplies their shared device ID. An empty event batch
has an empty accepted list. Identical previously stored events are acknowledged
again; the same ID with different canonical content returns 409 without partial
acceptance. Duplicate IDs inside one request are rejected by protocol validation.
Compare canonical decoded content, not incoming JSON whitespace/key order.

The firmware verifies the complete acknowledgment before persisting its local
acknowledgment cursor. Missing, extra, duplicate or malformed IDs raise 5010 and
retain the batch. Network timeouts and 5xx keep the queue; 401 backs off rather
than retrying rapidly; 400/409 isolate an offending record for inspection so it
does not permanently block the queue. Isolation itself is durable and bounded,
reports loss when discarded, and must not discard an otherwise valid batch.
413 shrinks the next batch until its full body fits. No event upload failure
changes trip-upload backoff or deletes a trip.

### On-device journal and degraded operation (#122/#124/#126)

Producers submit short bounded records into a RAM queue and never wait for SD,
networking or display operations. A core-0 service task performs filesystem
work using the project's lock discipline. No filesystem calls occur in an IRQ
or panic handler. The next boot reads reset reason and emits a boot/reset
notice; it does not attempt to write a panic record while the system is failing.
Hooks use transitions with rate-limited repeats so a storage or network failure
cannot recursively fill its own journal with the same failure.

Use flat rolling SD segments, for example `/diag/seg-00000001.open`, sealed as
`/diag/seg-00000001.ready`. The counter has exactly eight decimal digits. Keep
`boot_id` inside each record, **not in directory names**: deep sleep creates
many boots per day, and per-boot directories would leave thousands of FAT
directories. A segment can contain records from several boots. Segments roll at
about 64 KiB without splitting a frame; SD diagnostics have a 16 MiB total
quota (owner's choice, 2026-10-16), including acknowledgment and recovery metadata. #122 must define safe
counter-exhaustion handling (no unchecked wrap into an existing filename).

Keep acknowledgments in separate checksummed metadata using generation slots
and recovery rules that tolerate a torn write. Only durably acknowledged data
can normally be reclaimed. A damaged record is skipped using frame boundaries
and CRC, with bounded recovery scanning; a damaged final frame cannot hide all
later sealed segments. Never treat corruption as acknowledgment. Quota eviction
of unacknowledged diagnostic data increments dropped counts and produces a
coalesced EVENTS_DROPPED notice when writing becomes possible. A failed loss
notice must not recursively generate more loss notices.

If SD is unavailable, use a **64 KiB LittleFS diagnostic reserve**. Include that
reserve in trip free-space checks, so the same space is not promised to both
features. If both storage backends fail, use the bounded RAM queue; it is lost
on reset, so report that limitation and cumulative known drops. Do not format a
mount failure or delete unuploaded trips to make diagnostic space. #130 must
power-cut test frame writes, sealing, acknowledgments and quota reclamation.

#### EventLog module (#122 implementation)

`firmware/04_trip_logger/event_log.h` and `.cpp` build as standard C++14 on the
host and as sketch-folder sources on the ESP32-S3. Stage 4 instantiates the
journal in static DRAM and queues boot notices on core 1 without filesystem
work. `journal_fs.h` adapts the mounted SD/LittleFS stores; `journal_vfs.h`
uses their existing `/sd` and `/littlefs` VFS mounts. Direct checked VFS calls
propagate open, exact read/write, fsync, close, rename and enumeration errors;
Arduino's void `File::close()` would hide failures. Neither module nor adapter may
format media or remove trips. EventLog exclusively owns `/diag` and caches its
inventory, allocated bytes and validated open-tail sizes. Successful writes,
renames and removals update that cache; failed/uncertain I/O invalidates it.
Recovery relists the directory and reloads metadata, then validates open files
before appending again. Reconstruct EventLog after external changes or replacing/
remounting media; do not reuse cached state for a different card.

- `begin(boot_id, boot_counter, firmware_version)` accepts caller-owned identity
  before tasks start. Stage 4 passes its build version constant, also used by
  the check-in snapshot and boot banner; old journal records keep their original
  version. The caller supplies a new random boot ID on every wake;
  NVS boot-counter persistence belongs to that integration. Event sequences
  start at 1, are assigned only on acceptance, and stop at SQLite's signed
  64-bit maximum. They are never regenerated on retry or rollover.
- `submit(EventInput, optional_identity)` uses a 16-frame fixed RAM queue and
  one try-lock, with no waits or filesystem calls. It is task-safe, not ISR/
  panic-safe. Full/contention increments an atomic 32-bit drop accumulator
  (saturating near 2^31 between service calls); invalid input is rejected.
  Records validate catalog policy, all current context keys/types/ranges/enums,
  episode references, time consistency and the complete 502-byte JSON budget.
  Unknown device codes have empty context. No unbounded text enters a frame.
- `service(maxRecords=4)` is single-consumer work under the existing FS_LOCK.
  It tries SD, then LittleFS, retaining the queue in RAM when neither accepts
  a frame. It drains at most 16 ordinary records per call and attempts at most
  one coalesced E4009 notice. Storage errors increment a status counter and latch
  per-store failure observations without calling producers or a logging sink.
  The consumer reports E4008 after the journal call. Known drop totals survive in metadata
  once a backend is writable; RAM-only records/counts are lost on reset.
- `nextBatch` exports up to 64 unchanged protocol-v1 JSON events and their
  IDs, oldest segment first, normally on SD, then LittleFS, then RAM. Cross-backend order
  is not event-time order. The default 30 KiB events-array budget leaves room
  for the current check-in; the worker also enforces the full 32 KiB request.
  Re-reads preserve exact JSON, including original timestamps and identity.
  On the Windows host ABI, `sizeof(EventLog)` is **25,176 bytes** and
  `sizeof(EventLog::Batch)` is **35,288 bytes**; the host test prints both.
  ABI/alignment can differ on the ESP32-S3. #124 must place both objects in
  **static DRAM or PSRAM**, never on a task stack. Only one batch ticket is
  outstanding; a new read replaces it. A load/read failure increments storage
  failure accounting and skips that store for the call. Any partial batch and
  its uncommitted corruption count/cursor are discarded before trying the next
  store or RAM. Its records are retried after recovery; another store's ack
  cannot advance its cursor or charge its corrupt frames as losses.
  After an acknowledgment load/save failure, the other persistent store gets
  priority for the rest of the boot, including later drain opportunities. The
  failed store remains eligible when the preferred store has no events, so it
  can recover without rebooting. A successful durable ack on the deferred store
  restores normal ordering. This preference is RAM-only; failed acknowledgments
  leave that store's cursor and loss accounting unchanged. Keeping the preference
  across drains lets new flash history progress despite repeated SD ack failures,
  while the worker retains its existing independent retry backoff.
- `acknowledge(batch, accepted_ids)` requires the exact complete ID set, with
  no missing, unexpected or duplicate IDs. Persistent data advances only after
  the new metadata slot is durably written and read back. A failed ack retains
  retryability; RAM acknowledgments remove only matching queue heads. #125
  still owns parsing/validating the HTTP acknowledgment and E5010 policy.

##### Journal failure reporting (E4008)

`EventLog::fail(Store&)` latches the failed store and operation, alongside the
existing saturating `storageFailures` counter. `storageFault()` reads that state
without filesystem work or event submission. E4008 has exactly two context keys:
`operation` (`open|write|close|rename|ack`) and `backend` (`sd|littlefs`). Opening
or preparing a store (including inventory failure) uses `open`; event-frame
write/sync, quota refusal or reclamation failure uses `write`; a checked close
failure after a successful write/sync uses `close`; sealing/rolling uses `rename`;
metadata persistence or read-back failure uses `ack`, including filename
reservations, loss accounting and snapshot revisions. The VFS preserves the first
write/sync/close error. Read-only frame-export errors remain counter-only because
the catalog has no `read` operation. Cooperative cancellation for trip demand,
network hold/abort or the I/O deadline does not latch a media fault.

There is one aggregate condition per boot, with a latch for each store. Any
failed store keeps E4008 active. While both fail, SD supplies the context; a
changed operation/backend updates subsequent repeats without starting a new
episode. A later successful event append (including a diagnostic event) or
verified durable event acknowledgment clears **only that store's** latch.
Verified metadata persistence also recovers an `ack` failure on that store;
this lets a failing flash revision mirror recover while SD handles event frames.
An existing-segment append alone cannot recover an `ack` failure, since it need
not touch metadata. Metadata alone cannot recover an `open|write|close|rename` failure: otherwise
successful filename reservations could repeatedly clear failed event appends.
Reads, successful sealing and writes/acks on a different store do not establish
recovery. When every failed store has recovered,
the producer emits one clear, with the last recovered store and its successful
`write` or `ack` operation. A first healthy append silently observes the condition
healthy; stores that have never been exercised remain unobserved.

`journalStorageFaults()` uses **core 0's existing static `gEventFaults` builder**
after journal service and at upload-worker unlocks. That task owns both journal
I/O and its failure observations; core 1 sees only the builder's atomic
publication for the OLED. In degraded boots there is no network task: the sole
core-1 journal consumer instead uses its own static `gLoopFaults` builder. No
builder crosses cores, no new task/stack is allocated, and `fail()` never calls
the producer. Shutdown also publishes failures found while sealing; a queued
transition after the final seal makes the shutdown report incomplete durability.

An E4008 raise/repeat/clear goes through the same bounded `submit()` queue and
SD -> LittleFS -> RAM fallback as any event. Its own failed write only updates
the latches; it cannot recursively submit an event. The existing producer policy
allows at most one repeat per 15 minutes and retries a full/busy submission no
faster than once per minute while the observation is unchanged. The 16-frame
volatile queue and existing coalesced loss accounting remain the only fallback;
there is no extra persistence, retry loop or metadata write for these latches.
All-store failure therefore retains bounded RAM history, which is lost on reset.

Stage 4 runs `service(1)` on the **existing core-0 network task**, with a
zero-wait take of the same filesystem mutex used by FS_LOCK. It releases that
mutex before networking and between journal calls. During the network task's
existing 30-second idle interval it services in 100 ms slices; the network
cadence stays unchanged. No extra task or task stack is allocated. Journal
service waits until a blocking network cycle finishes, preserving trip-upload
priority. Ordinary FS_LOCK callers announce demand before taking the mutex.
The journal checks that demand before each adapter operation and each directory
entry, abandoning recovery when a trip/upload needs storage. Its next slice
reloads invalidated caches and retries retained identities. Consumer code must
use this zero-wait consumer lock path, not announce itself as ordinary demand.
Do not run journal recovery
or eviction while the GPS task is waiting on a trip write for long. Measure the
real adapter's latency before enabling it: operation counts bound work, not time.
The work under one `service()` lock hold is bounded as follows:

- Let `M = min(maxRecords, 16)`. There are at most `2*M` ordinary append attempts
  (SD then flash), one loss-notice append attempt, two additional store loads
  for loss accounting, and up to three bookkeeping metadata saves (two attempts
  to persist queued drops, one to clear a persisted notice).
- With valid caches and no roll/reclamation, each append uses **one write**,
  with no listing or frame reads, independent of segment fill. A normal roll
  adds one rename and one filename-reservation metadata save. Each save is one
  80-byte write plus one 80-byte read-back; clearing a loss notice adds a save.
- Each append's `makeRoom` can remove at most **512 segments**. Acknowledged or
  already-discarded files need no frame scan or loss save. Each unacknowledged
  eviction traverses at most **64 KiB** in `lostIn`, saves its loss/discard intent,
  then removes the file. Thus one append can perform at most 512 eviction scans,
  512 removals and 513 metadata saves including filename reservation. With a
  normal full-size journal this is usually one eviction at a quota-driven roll;
  tiny recovered segments can require more.
- Each uncached load uses one `begin`, at most **1025 list calls**, and two
  metadata reads. The next append validates at most **512 open segments**, each
  at most **64 KiB**, sealing at most 512 files. A failed operation may require
  this recovery again on a later attempt, even within the same service call.
  There are at most `2*M+3` loads in a call. Each segment scan advances its offset
  monotonically: at most 65,536 reads of at most 512 bytes even for pathological
  one-byte damaged lines (healthy frames need roughly one read per event).

These are conservative recovery limits, not a promise of a short lock hold in
damaged/full media. #124 must schedule maintenance away from trip writes; small
`maxRecords` only limits the number of append attempts in a call.

The frame is compact event JSON, TAB, eight lowercase CRC32 hex digits, LF;
CRC32/IEEE covers JSON only. Flat names have exactly eight decimal digits,
`/diag/seg-00000001.open` becoming `.ready`. A clean tail can be appended across
boots. Torn/damaged open files are sealed before further writes, and readers
skip invalid physical lines and continue into later segments. Scan buffers are
512 bytes, each segment is at most 64 KiB, inventory is capped at 512 segments
and 1024 directory entries; exceeding these recovery bounds fails that backend
closed. No recovery scratch files consume unbudgeted space. Unknown files in
`/diag` count against quota and are never removed.

Metadata is two alternating 80-byte binary files, `/diag/ack-0` and `ack-1`,
reserved inside both quotas even before creation. All integers are little
endian. New writes use magic `ELG2` at 0; generation (u64) at 8; ack segment/byte
offset (u32 each) at 16/20; next segment (u32) at 24; snapshot revision (u64)
at 32; cumulative drops/pending loss notice count (u64 each) at 40/48; explicit
quota-discard-through segment (u32) at 56; revision-enrolled flag at 60;
revision-suspended flag at 61; recovery epoch (u64, multiple of 8) at 64;
other reserved bytes zero; CRC32 over bytes 0-75 at 76. `ELG1` remains readable
with epoch zero and no new flags; an unenrolled nonzero legacy revision is
treated as suspended. Existing enrolled v1.00 cards migrate without enrollment.
Older firmware cannot read `ELG2`; do not downgrade without explicit recovery.
Recovery selects the highest valid
generation with matching slot parity. A torn newest slot falls back to the
older generation. If neither validates, no records are considered acknowledged
and that store cannot supply revision authority. It may adopt from another
current copy, or needs explicit enrollment. Filename allocation also scans existing
segments, persists reservations before creation, and stops after 99999999;
it never wraps or overwrites another segment.

Normally only sealed, durably acknowledged segments are removed. When quota
requires unacknowledged eviction, the oldest sealed segment's remaining frames
are counted and loss/discard intent is durably committed **before** deletion.
Interrupted deletion can finish after reboot without counting the loss twice.
Corrupt physical frames crossed by an acknowledged batch are explicitly counted
as loss in that same metadata update, never treated as successful delivery.
Unidentifiable damaged frames are conservatively one loss each. A failed E4009
write does not add losses, and retries within the running boot retain that
notice's ID. An ambiguous close/metadata failure may retain identical frame
copies; export coalesces identical IDs within a batch and the server dedupes
across batches/backends. Reset between notice persistence and clearing its
pending count can repeat a historical loss report; totals remain cumulative,
so consumers must not sum notices as a separate authoritative counter.

SD's allocated-file quota is **16 MiB**, superseding the original issue's 8 MiB.
Every file's size is rounded up to its allocation unit, including unknown files
and the two metadata slots (each slot is reserved before creation). Configure
`Config::sdCluster` to the mounted FAT cluster size (default **32 KiB**) and
`Config::littleFsBlock` to the LittleFS block size (default **4 KiB**); zero is
rejected. Nonempty files consume at least one unit. LittleFS's **64 KiB includes
both rounded metadata slots**, leaving at most **56 KiB** for segment allocation
without transport isolation. Stage 4 enables `Config::reserveRejected`, reserving
one additional allocation unit for `/diag/rejected` before any segment writes:
**52 KiB** remains on LittleFS, and one FAT cluster is reserved on SD.
LittleFS segments roll early to fit the remaining allocation. The limits bound
allocated file data with the correct units; media exhaustion or I/O errors still trigger
fallback. Part A of #124 protects the exported `kLittleFsJournalReserve` in
LittleFS trip free-space checks, including boot
capacity reporting and the start/stop guards. It subtracts
`max(0, 65536 - existing_diag_allocation)` from physical free space: already
allocated diagnostic bytes are already in filesystem usage. Failure to enumerate
allocation conservatively keeps the entire reserve. This applies even while SD
is healthy, so flash fallback has space available. Trip files, splitting,
recovery and upload policy otherwise retain their existing behavior.

`restoreStatusSequence(highWater)` checks every loaded revision (and this boot's
reserved floor), refuses a lower value, and attempts every loaded journal.
The caller supplies a fresh server bound too. NVS `diag/revstate` encodes bit 0
as suspended, bits 1/2 as SD/LittleFS stale, and bits 3-62 as the recovery epoch.
The NVS adapter uses typed ESP-IDF lookups to distinguish NOT_FOUND from I/O or
type errors. Only a genuinely missing key permits adoption: ready nonsuspended
loaded stores must agree on an epoch, which is durably written to the witness.
Suspension at that epoch or above stays blocked; a suspended-only survivor is
latched suspended. Lower epochs cannot authorize allocation. Legacy ELG1 ready
metadata adopts epoch zero by the same rule.
Enrollment chooses an epoch 8 above the highest witness or loaded-store epoch
seen this boot, persists a suspended marker first, then writes new authority.
At least one verified save is required before committing an unsuspended witness
with all absent/failed stores stale. Total save failure leaves suspension and
does not add stale bits. Absent copies cannot authorize themselves in the new
epoch; they can adopt only alongside a current authorized copy.

`allocateStatusSequence` needs at least one ready, nonsuspended, nonstale copy
in the current epoch. It takes the highest loaded revision plus one and writes
that value, ready flag and epoch to every loaded journal it can save.
Fresh/empty or older copies adopt only in the presence of that authority.
At least one copy that was current authority before the attempt must save and
pass read-back. Only then does NVS mark absent/load-failed/save-failed stores
stale and clear successfully synchronized stores' bits, before returning the
revision for transmission. Refusal never adds stale bits, even if a stale copy
saved but the last current authority failed. Successful writes are not rolled
back; the boot's reserved floor prevents reuse after uncertain read-back. An
uncertain NVS read/write latches allocation off for the boot; exhaustion refuses.
`suspendStatusSequence` latches the boot off, persists NVS suspension first,
then attempts every mounted journal even if one fails. The explicit metadata
flag distinguishes suspension from never enrolled. Only explicit enrollment
can clear it; an absent store alone remains blocked on a later boot.
These operations retain the single core-0 consumer and `FS_LOCK` contract.
Actual FAT/LittleFS power-cut durability and flash wear need hardware testing.

`firmware/host_test/run.sh` runs fault injection (including torn frames/acks,
CRC damage, full/read-only media, overflow, retention, failures and exhaustion)
and a stdlib Python bridge that feeds actual exported C++ events through
`common.diagnostics.validate_request`, covering every catalog context field,
integer endpoint and enum value. Stage 4 integration tests also exercise the
adapters against the trip harness's fake disks, reserve/quotas, NVS counter,
notices, lock contention and shutdown. Direct production VFS tests check sync,
close and enumeration errors. None of these establishes SD/FAT power-cut safety.
#124 part A wires boot producers (including the retained PMU startup helper) and storage;
#125 wires transport, status snapshots and durable isolation for rejected events;
#126 wires bounded degraded startup/retry behavior. Uploads and snapshots run
after explicit enrollment on normal boots; degraded boots never start networking.

#### Stage 4 boot and shutdown plumbing (#124 part A)

The boot-time trip-store selection is SD when mounted, otherwise LittleFS.
If the selected SD later fails its capacity query, core 1 latches it unavailable
until reboot and selects already-mounted LittleFS between trips. No trip changes
destination while it is open. Diagnostics skip that failed SD through its adapter
gate, then try mounted LittleFS and the existing 16-record RAM queue. The existing
revision allocator marks SD stale when a current flash copy saves a revision;
the availability latch does not reset revision metadata or the NVS witness.
An absent store is never mounted later just for diagnostics. SD allocation units
come from its FAT boot sector (including an MBR primary partition); unreadable
geometry disables SD journaling for this boot while leaving trip storage alone.
Flash uses the ESP32 LittleFS 4096-byte allocation block. Segments, 16 MiB SD
quota, 64 KiB flash quota and checksummed metadata use EventLog's format above.
Adapters only access `/diag`; they never format media or remove trip files.

`journal_boot.h` obtains a random 128-bit boot ID through `esp_fill_random` and
increments `diag/boots` in NVS Preferences once per boot, verifying persistence.
Failed NVS writes or exhaustion use counter zero with an unavailable status;
the fresh random identity remains usable. The counter is diagnostic information,
never the snapshot revision allocator or an ordering authority. No snapshot
revision is enrolled or allocated by part A.

Core 1 enqueues E1000 BOOT with `esp_reset_reason()` mapped to
`power_on|deep_sleep|software|watchdog|brownout|panic|other`; unknown/new reset
reasons map to `other`. Watchdog, brownout and panic also enqueue their existing
E1003, E1004 and E1005 notices. `makePmuStartupEvent(gPmuStartup)` adds E2007
only after recovery/retries or another non-clean startup result. All notices
have null episodes and use the same clock-confidence provider as runtime events.
A valid boot RTC can timestamp them before GPS/SNTP. A clean first-attempt PMU boot adds
no E2007. Failed PMU/storage startup now initializes the journal in degraded
service as described below. Debug builds print one journal boot line with
the active backend and physical unacknowledged valid-frame/segment counts;
uncertain inventory is labeled incomplete. Ambiguous writes can retain duplicate
IDs, so physical frame counts can exceed distinct event counts.

Firmware v1.04 enrolls the main loop in a 60-second task watchdog after
normal or degraded startup. Expiry requests a panic and restart; the next
boot records the existing E1003 notice and still runs ordinary trip-file
repair. Startup and the core-0 network task are outside this loop protection.
The watchdog and its intentional-hang bench hook are **not yet bench-checked**;
see [the firmware guide](firmware/README.md#watchdog-bench-check). No catalog or
diagnostics-protocol change is needed.

After trusted PMU/LoRa startup, charging is disabled and battery detection/ADC
are enabled before display/USB waits. After the existing one-second USB wait,
the raw battery registers are sampled once, before storage, GPS startup and the
charge guard's first charger-enable decision. BOOT retains that early reading;
its normal event timestamp is assigned when the journal starts after storage
mounting, so it can be seconds later than the measurement. Failed/untrusted PMU
startup omits battery context and retains its conservative degraded policy.
Residual bias remains: the PMU can charge before software disables it (including
across an ESP32 reset), a failed disable may leave charging on, the ADC/gauge
may be stale, and USB/load/temperature/voltage relaxation affect the reading.
This is an early sample, not a proven open-circuit voltage or current measurement.

Before PMU shutdown, core 1 ends the trip and stops networking, then asks the
core-0 consumer to drain the bounded queue and seal open segments. The consumer
closes each write before returning. Core 1 waits at most 15 seconds for this
handoff, reports incomplete drain/sealing when necessary, and takes FS_LOCK
through both unmounts and power-off. No diagnostic I/O can reach unmounted media.
Cancelled/failed shutdown remounts the same stores and invalidates journal
caches while retaining the boot identity and RAM queue. RAM-only history is lost
on reset/power-off. Hardware tests must measure individual VFS operation latency
and trip-write delay, plus induced failures during sealing and remount.

Immediately before requesting that seal, core 1 captures one E2009 battery
sample with the existing UTC/confidence provider and the known shutdown reason.
Core 0 drains a queue slot, submits the retained notice exactly once, then drains
and seals it with the earlier events. A full writable queue therefore does not
lose the sample. PMU reads stay on core 1; filesystem writes stay with the
existing consumer (the sole degraded-loop consumer handles no-storage mode).
There is no journal write after sealing/unmounting or between the final VBUS
check and `PMU.shutdown()`. Successful closed writes and sealing provide the
existing filesystem durability; physical power-loss guarantees still need bench
testing, particularly FAT metadata. Failed media, a stuck consumer, the existing
15-second deadline or RAM-only operation cannot guarantee persistence. Power
safety keeps its bounded wait and reports incomplete sealing instead of waiting
forever for diagnostics. A late VBUS return or failed PMU shutdown can leave an
E2009 for an attempted power-off that did not happen; it is not proof of rail-off.

#### Reading parked battery drain without a meter

On the Supreme, parked means PMU power-off: the 18650 still supplies the PMU,
RTC and GPS backup RAM. After updating the server and firmware, ordinary drives
and parked nights collect the two readings; no special driving procedure is
needed. Once the board next uploads its retained journal, run the helper on the
server. On a release install (`setup.sh`), the helper sits in the install
folder, and the database is under that folder's `data/` (the `/data/...` path
you see inside the containers is not a host path). `data/` is mode 700 and
belongs to UID 1000 (the user the containers run as), so run it as that login:

```sh
python3 ~/mileage-tracker/parked_drain.py ~/mileage-tracker/data/diagnostics/events.db
```

Use your own install folder if it isn't `~/mileage-tracker`. On a source install
(a copy of the repository), run `python3 server/parked_drain.py` from the
repository's top folder with the database's path on the host. If your login's
UID isn't 1000 (`id -u`), put `sudo` in front of the command, or you get
"Permission denied". The stdlib-only helper opens SQLite
with `mode=ro`, a two-second timeout, `query_only` and one read transaction. It
never initializes, migrates or changes the database. CSV output identifies the
device and off/boot UTC times, hours parked, raw mV drop, raw mV/hour and charge
**percentage points** lost. These first seven columns are unchanged; appended
columns classify pairs, show each device's rebound calibration and report
corrected drop, corrected mV/hour, resolution and drain status. Missing readings
print `unknown` independently. Raw voltage rises remain negative for inspection;
check `drain_status` before interpreting any rate. An empty result means no
usable pairs yet; it does not mean no drain.

The power-off sample is taken under load; the early boot sample has a lighter
load. The cell's voltage rebounds as the load drops: one eight-second park
measured **4137 → 4152 mV**, a 15 mV rise without gaining charge. Pairs parked
**under 10 minutes** are `short` calibration pairs, not drain measurements.
Change the cutoff with `--short-minutes MINUTES`; a pair exactly at the cutoff
is `long`. To calibrate, unplug USB, hold POWER for a normal shutdown, then plug
USB back in after a few seconds. Repeat a few times at similar charge levels
and temperatures, then let the journal upload.

For each device, the helper takes the **median boot-minus-power-off mV** from
short pairs with both voltage readings. Long pairs use
`corrected_mv_drop = mv_drop + rebound_offset_mv`; corrected mV/hour divides
that drop by hours parked. Percentages are unchanged. With no usable short
voltage pairs, the offset is zero and `rebound_status` explicitly says
`no short voltage pairs; uncorrected`. `rebound_pairs` counts usable samples;
`rebound_spread_mv` is their largest absolute deviation from the median.
Corrected drops at or below the larger of that spread and **3 mV** are flagged
`below resolution`, including zero and negative results. Their numeric columns
remain visible, but are not meaningful drain estimates. The 3 mV floor is a
reporting rule, not proven ADC accuracy; one short pair cannot establish
repeatability. Calibration does not remove charging, temperature or gauge bias.

The helper pairs adjacent UTC-ordered BOOTs of the same device and uses the
last event of the earlier boot only when it is E2009. It requires the next
reset reason to be `power_on`, positive unique consecutive boot counters as an
additional gap check, and increasing known UTC. Counter values are not an
ordering authority; server receipt times and random boot IDs are never clocks.
Gaps, software resets, ambiguous counters, unknown times and later same-boot
events after an attempted shutdown are skipped. Lost records, counter resets,
clock corrections or a cancelled shutdown without a later event can still make
a pair misleading. Inspect the history and clock confidence before using it;
allow retained history to finish uploading before drawing conclusions.

Collect at least **three parked pairs of eight hours or longer** and compare
them at similar temperatures and charge levels. At about **1 mA**, an overnight
park loses only a few mV, easily hidden by rebound and measurement variation;
**parks of two days or longer give the best estimates**. An 18650's voltage and stored
charge have a **non-linear** relationship: there is no universal conversion
from mV/hour to mA. Use the particular cell's discharge curve and usable
capacity for a rough current estimate. As a separate rough check,
`capacity_mAh * percentage_points_drop / 100 / hours_parked` estimates mA only
if the PMU gauge is accurate. On a 3000 mAh cell, each integer **1% step is about
30 mAh** (about 30 hours at 1 mA); gauge recalibration can overwhelm small changes.
Early USB charging can still hide drain. An estimated drain consistently above **about 2 mA**
is worth investigating from several pairs and the existing logs. A meter is a
last resort if those data cannot explain it. Firmware/helper tests do not
complete the real parked-data acceptance step.

#### Stage 4 runtime fault hooks (#124 part B)

`runtime_faults.h` holds fixed condition slots and one static event builder per
owning task. Core 1 owns GPS, power, trip storage/recovery and display hooks;
core 0 owns WiFi/HTTP/stack and the AP-absence counter. The builders only call
EventLog's bounded, nonblocking `submit`, never filesystem APIs or ISRs. Journal
adapter/service errors are latched by EventLog and reported as E4008 by the
owning consumer, as defined under [journal failure reporting](#journal-failure-reporting-e4008).
EventLog emits E4009 loss notices internally. The trip/FS operations being
measured keep their existing locks.

Continuing conditions emit one accepted `raise`, no hot-loop records, at most
one `repeat` per **15 minutes**, and one accepted `clear` on measured recovery.
All records in an episode retain the accepted raise identity. Failed submissions
retry no faster than **60 seconds** while the observation stays unchanged;
a physical state change may attempt immediately. A rejected raise does not
create an episode. A rejected clear retains the episode until a later observation
can queue it. Full/contention drops are counted by EventLog; notices are best
effort and never retried as invented new occurrences. This is bounded reporting,
not a guarantee that every transient failure survives queue exhaustion.

Healthy observations mark condition slots observed without producing events.
Unmeasured fields are omitted instead of guessed or clamped. Hook timestamps use
monotonic `millis()` plus the diagnostics clock-confidence provider. After a
trusted system-clock set this boot, events use system UTC and `gps` or `sntp`
(the most recent setter), with age since that sync. Otherwise a validated boot
RTC supplies `rtc` UTC, advanced by elapsed uptime; its age is elapsed time since
the last successful RTC write stored in NVS, or null if unavailable/future.
Without either source, UTC and age are null and the source is `unknown`.
The provider uses independent single-writer seqlocks made of 32-bit atomics;
readers make at most two attempts and use unknown time on concurrent publication,
without waiting, I2C, NVS or 64-bit atomic locks. Core 1 extends unsigned tick
differences each loop so elapsed time and sync age survive `millis()` wraps.
Boot/reset/PMU notices, both cores' runtime faults, upload/rejection faults and
the journal's internal loss notices all use this provider when built. Stored
events are never restamped. The RTC never sets the system clock, `gClockSet`
or GPX times; E3006 still requires GPS/SNTP this boot. The upload worker copies each
task's atomically published observed/active slots and accepted episode identities, plus core 0's saturating E5001 counter.
A version check rejects a concurrent publication without waiting. Native 32-bit
atomics avoid a 64-bit atomic lock in the producer. An active condition whose
raise has not been accepted remains unknown until it has a valid episode ID.

| Code | Trigger | Recovery / policy | Context |
|---|---|---|---|
| E1007 | Network lifetime minimum free stack **<2048 bytes** | Watermark >=2048; normally latches for the boot because a lifetime minimum never increases | `free_bytes` |
| E2001 | PMU initialization failed or LoRa ALDO3 could not be verified off at startup | Only a normal boot after successful PMU initialization and rail verification observes healthy; retry success restarts, never clears in degraded mode | none |
| E2002 | Verified AXP2101 level-1 IRQ or battery percentage <= successfully read configured low warning | Valid connected battery percentage above that threshold; failed threshold reads neither raise nor clear | `battery_mv`, if in catalog range |
| E2003 | Verified AXP2101 level-2 IRQ (before existing shutdown), or percentage <= successfully read configured critical level | Valid connected battery percentage above critical; failed threshold reads neither raise nor clear | `battery_mv`, if in range |
| E2004 | Guard requires a hot pause (die >=54 C); keeps requesting it above 47 C | Finite die sample <=47 C or another measured pause reason; describes required pause even if control is unconfirmed | `temperature_mc` = measured PMU die, if in range |
| E2005 | Guard requires a cold pause (die <=9 C); keeps requesting it below 14 C | Finite die sample >=14 C or another measured pause reason; describes required pause even if control is unconfirmed | `temperature_mc` = measured PMU die, if in range |
| E2006 | Non-finite charge-temperature sample | Finite sample; hot/cold conditions are not cleared by an unreadable sample | none |
| E2008 | Three consecutive charger command/read-back failures, 30 s apart | Checked 0x18 bit 1 read matches the intended charger state | none |
| E3001 | No checksum-valid NMEA for 10 s, starting at GPS configuration start | Next checksum-valid NMEA; noise/UBX/bad checksum do not recover it | `elapsed_ms` |
| E3002 | No trusted fix (one meeting the NAV-PVT start accuracy limits, or any valid fix in RMC-only fallback) for 120 s while idle | Trusted fix; acquisition grace starts again after leaving a trip | `elapsed_ms`, `satellites` when measured |
| E3003 | Matching checksum-checked VALSET NAK | One notice per rejected attempt, no active condition | `message_class=6`, `message_id=138` |
| E3004 | Final config readiness/ACK timeout after five attempts | One give-up notice; final NAK uses only E3003 | `message_class=6`, `message_id=138` |
| E3005 | Active/cooldown trip has no valid RMC fix for 30 s; any valid RMC counts, trusted or not, so weak accuracy while parked in cooldown does not cause an alert | Next valid RMC fix, even after the five-minute finalize; ending the trip alone is not recovery | `elapsed_ms` |
| E3006 | Trusted system clock `gClockSet` still false after 120 s | Successful GPS/SNTP clock sync; untimed GPX points retain their original status | none |
| E3007 | RTC has no ACK, or its initial boot register read fails | Verified RTC setting after trusted GPS/SNTP time; at most three attempts per boot, at least a minute apart after failures | none |
| E3008 | Readable RTC has STOP/VL, invalid BCD/calendar, unsupported year, or a later read/write/verification failure | Verified RTC setting from GPS/SNTP clears the condition; normal transition/repeat policy | `reason=stopped\|invalid_date\|out_of_range\|read_failed` on failure |
| E3009 | At 30 s MON-RF boundaries, valid AGC >=70% and fresh GSV (a complete group within 15 s) with zero satellites >=30 dBHz, continuously for >=120 s from the first suspect sample | One valid AGC <=50% sample **or** fresh GSV with at least one satellite >=30 dBHz; missing/stale/intermediate evidence resets the hold and retains an active warning | `agc_pct` (0-100) when MON-RF valid; `max_cno` (0-99) when GSV fresh |
| E4001 | SD did not mount, or the selected SD trip store returns zero total capacity at runtime (failed filesystem query) | Successful SD mount on a later boot; runtime failure stays latched until reboot | none |
| E4002 | LittleFS unavailable during no-storage degraded startup | RAM-only raise/repeat; retained as failed until restart discards the queue | none |
| E4003 | Neither trip store is usable at startup | Only a normal boot with a mounted trip store observes healthy; successful degraded retry restarts without clearing | none |
| E4004 | Available selected trip store has guarded free space <64 KiB start reserve | >=64 KiB on the selected store, or condition no longer applies because that store is unavailable | `backend`, `free_bytes` only when available |
| E4005 | Available selected trip store has guarded free space <24 KiB stop reserve | >=24 KiB on the selected store, or condition no longer applies because that store is unavailable | `backend`, `free_bytes` only when available |
| E4006 | Trip create/open, point commit or finalization fails | Next verified trip create/write/finalize or successful repair | `backend`, `operation=open\|write\|close` |
| E4007 | Boot fsck directory/read, ambiguous repair, write or publish fails; also runtime repair failure | Per-occurrence notice; no clear | `backend`, `reason=read\|parse\|write\|rename` |
| E4008 | Journal preparation, event append, sealing or metadata persistence fails | All failed stores must later append an event or durably ack an event batch; 15-minute repeats, no recursive submission | `backend`, `operation=open\|write\|close\|rename\|ack` |
| E4010 | Actual repaired trip, published verified repair or recovered orphan | Per-occurrence notice; complete untouched files and garbage cleanup stay silent | `backend`, `points` when counted |
| E5001 | Completed successful scan does not see the configured AP | **Counter only**, boot-local, saturates at 2^63-1; failed/aborted scans do not increment | none; future check-in counter |
| E5002 | Completed 15 s WiFi join attempt fails | Next successful join; AP absence is not recovery | `attempts` (saturates at 255, resets on join) |
| E5003 | Trip HTTP result <=0 | Next HTTP 200 trip upload | `attempts` (transport failures since success, saturates at 255), `target=trip` |
| E5004 | HTTP 401 | Next HTTP 200 trip upload | `http_status`, `target=trip` |
| E5005 | HTTP 413 | Next HTTP 200 trip upload | `http_status`, `target=trip` |
| E5006 | HTTP 400 | Next HTTP 200 trip upload | `http_status`, `target=trip` |
| E5007 | HTTP 409 | Next HTTP 200 trip upload | `http_status`, `target=trip` |
| E5008 | HTTP 500-599 | Next HTTP 200 trip upload | `http_status`, `target=trip` |
| E5009 | Other positive HTTP result except 200 | Next HTTP 200 trip upload | `http_status` when 100-599, `target=trip` |
| E6001 | Either OLED adapter latches I2C/init failure | Adapter healthy; either adapter disables display for the rest of the boot | none; both runtime modes |

Battery diagnostics use the **existing hardware SOC thresholds**, without adding
a new voltage cutoff or changing shutdown policy. XPowersLib 0.3.3 returns the
raw level-1 nibble, so the configured warning percentage is that value plus five;
level 2 returns the configured percentage directly. Connected-battery sampling
runs every 30 s, with immediate IRQ observations. The catalog's voltage wording
is generic; `battery_mv` is a measurement, not the threshold used by this PMU.
Both thresholds come from one checked LOW_BAT_WARN_SET read. Stage 4 never writes
them, so the first successful value is cached for the boot; earlier failures are
unmeasured and retried at the sampling cadence.

IRQ batches require all three INTSTS1..3 raw reads to succeed. Any failed byte
makes the entire batch unknown: no flag action (including power-off, battery
alerts, VBUS handling or a short-press card) and no status clear. The pending
retry survives without another falling edge and runs at most every 100 ms,
including if more edges arrive while reads fail. Successful batches use the
XPowersLib bit positions and the same enabled mask as IRQ setup, then clear as
before. The production short-press display hook uses this verified batch too.

The charge guard uses its existing die proxy and hysteresis; diagnostic context
never presents that die reading as a measured cell temperature. Each enable/disable
command in the normal temperature guard is followed by a checked CHARGE_GAUGE_WDT_CTRL (0x18)
bit-1 read. State is confirmed paused, confirmed charging or unconfirmed. A
mismatch or read failure leaves it unconfirmed, retains pause-request hysteresis
and retries at the 30 s temperature cadence. Only a matching read-back logs a
confirmed pause/resume. E2008 stays active until confirmation; the first two
unconfirmed attempts cannot manufacture recovery. The degraded PMU-untrusted
mode retains its separate one-time best-effort disable/read-back policy.

E3009 checks for a loose or damaged active-antenna cable. On one Supreme/MAX-M10S
and one antenna (2026-10-19), unplugging raised AGC to about 79% after a minute
with no satellite >=30 dBHz; outdoors stayed near 29% with 22 strong satellites,
and indoors stayed at 6-18% with 0-3 strong satellites. The thresholds are first
guesses from that bench evidence, not a universal antenna test. Receiver fix
status and MON-RF antenna status are not evidence: both can still look healthy
with this board's plug disconnected. Check the GPS plug inside the case and cable.
Low AGC alone clears even indoors with no strong satellites. Without either
healthy signal, missing MON-RF, stale GSV or AGC 51-69% cannot raise or clear.
The first poll follows GPS configuration success or give-up; each
30 s boundary evaluates the previous reply and starts a new poll without waiting.
Before a healthy sample or a completed suspect hold, E3009 is not observed in
snapshots and produces no journal event. Once raised it stays active through
missing evidence, with normal 15-minute repeats; only measured recovery clears.

Storage space is checked at boot, on admission, during trips and every 30 s while
idle so uploads can produce observed recovery on core 1. LittleFS free space
already excludes the unused journal reserve. Below 64 KiB a new trip is refused;
below 24 KiB a running trip is finalized. On ESP32 Arduino core 3.3.11,
`SD.totalBytes()` returns zero if its FAT free-space query fails. `cardType()`
keeps the cached mount-time type and is not a removal check. Zero total capacity
raises E4001; it never raises E4004/E4005 for that SD. A genuinely full readable
card still has nonzero total capacity and keeps the normal space warnings.
An earlier space condition is cleared when it no longer applies; that clear
does not mean an unavailable card has recovered, and carries no `free_bytes`.

If internal flash was mounted at boot, new trips use its queue after the current
SD trip ends. The current trip stays on SD and ends through E4006 on its next
attempted write; it is left for repair on a later boot with a working card.
Uploads and diagnostics skip the known failed SD, keeping its files untouched.
No card is remounted or formatted at runtime. Reinserting it does not resume SD
use: power off, check/reseat or replace the card, then restart. With no mounted
flash fallback, new trips are refused as storage unavailable. Flash still needs
the same free-space reserves; if it fills, E4004/E4005 name `littlefs`.
This runtime switch has host-test coverage; removal during a real trip, upload
timing, and recovery of the removed card still need a board check.
These hooks do not modify EventLog's revision/failure rules. Arduino's void close cannot independently
report close errors; E4006 `close` means the verified finalization commit failed.
HTTP error classes stay active across different errors and clear only on 200.
Existing quarantine/retry behavior is unchanged; intentional network aborts do
not generate transport/join faults.

Host tests exercise each hook's transitions and context through production paths,
the notice policies, checksum/readiness deadlines, charge hysteresis, queue-full
retry/episode handling, rollover, the counter without scan events and a normal
real-parser drive/upload with only E1000. Both runtime modes run, and actual
exports pass the server protocol validator. They cannot measure the network task's
watermark or storage latency. The 12 KiB allocation is unchanged; the 2 KiB warning
margin follows #6. Static builders avoid event-sized stack frames; compiler stack
reports and the build sizes supplement, but do not replace, the real-upload bench
measurement. Part A's observed 7,556 free bytes was a no-upload measurement.

E2001/E4002/E4003 degraded-startup hooks are implemented by #126. The existing
FS-mutex allocation failure still restarts, and network-task creation failure
remains non-fatal. RTC E3007/E3008 are implemented by #128; E5010 and diagnostic
uploads/snapshots by #125. Production error-code OLED pages are implemented by #127; antenna detection
is implemented by #164.

#### Degraded startup service (#126)

Every degraded boot prints `DEGRADED:` after the Stage 4 banner. These lines
mean **no trips are being recorded**. A separate loop returns before GPS parsing,
receiver configuration, the trip FSM, RTC service and display updates. No degraded
mode starts the network task, WiFi, or diagnostic uploads. The OLED is not needed
for this behavior; production error pages are implemented by #127.

The first retry is after 60 seconds; failed retries wait 120, 240, 480, then
600 seconds, capped at ten minutes forever. The next interval starts after the
bounded initialization attempt finishes, using wrap-safe `millis()` subtraction.
The loop yields between checks. Success requests `ESP.restart()` and runs all
normal boot ordering again; it never resumes logging in place. Each degraded
cycle lasts at least a minute, so a flapping fault cannot make a rapid restart
loop. No degraded retry formats media, deletes trips, or runs trip repair.
Normal first-boot provisioning of a proven fully erased flash partition is
unchanged; degraded probes explicitly disable that provisioning.

- **No usable trip storage (D), PMU healthy:** power/charge service remains
  active: IRQ/VBUS confirmation, power key, low-battery level-2 shutdown,
  temperature guard and the ten-minute battery idle power-off. GPS rail setup
  remains as at boot, but GPS UART and processing never start. The journal has
  no backends: BOOT, E4001/E4002 backend failures and E4003
  `NO_RECORDING_STORAGE` use the bounded RAM queue. They cannot be uploaded
  during this boot and are lost on restart or power-off. Storage retries use
  the existing BLDO1-off / 3.3 V / enable ordering for SD and never format.
  A successful mount is unmounted before restart. Shutdown safely handles no
  trip, no network task and no mounted media: the loop services the bounded
  journal seal itself, reports undurable RAM if necessary, then holds FS_LOCK
  through the final VBUS check and PMU shutdown. A returning/failed shutdown
  restores degraded service without claiming recording or remounting an absent
  store.
  VBUS decisions require successful STATUS1/STATUS2 reads: unknown never
  confirms car-off or authorizes shutdown. Pending car-off confirmation retries
  at most once per second; unknown boot/recovery seeds assume USB present and
  retry once per second in normal and no-storage loops. Persistent failures on
  battery keep the board awake until hardware under-voltage cutoff.

- **Power chip absent (A) or untrusted LoRa-rail state (B):** no ordinary PMU
  power, charge, IRQ or measurement service is allowed. In B only, charging is
  disabled exactly once, best effort. A single raw-register
  read-back through XPowersLib distinguishes disabled, still enabled and an
  unreadable/unknown state; Serial reports the result without promising safety.
  Apart from that exception, only scheduled normal startup probes are allowed:
  bounded `beginPmuWithRecovery()`, plus ALDO3 disable/verify in B after a
  successful begin. A begin success restarts A; B requires verified ALDO3 off
  before restart. Verification requires a successful disable and a successful
  raw ALDO3 enable-bit read showing off; an unreadable result remains untrusted.
  LittleFS alone is mounted without provisioning, solely for
  diagnostic BOOT, retained E2007 and E2001 `PMU_INIT_FAILED`. If it fails,
  diagnostics stay in RAM. SD, GPS and networking never start. The board stays
  awake retrying, as with the former halt; software cannot promise charge
  control or power-off. The AXP2101 hardware under-voltage cutoff still protects
  the cell. A later normal boot can drain those LittleFS journal segments even
  with SD selected: EventLog enumerates both mounted backends for uploads.

Core-1 conditions include 2001 and 4003. A normal boot explicitly observes 2001
healthy after PMU initialization/rail verification, and 4003 healthy after a trip
store mounts. These are silent `condition(false)` observations in the current
snapshot, allowing the server monitor to resolve an earlier alert. Degraded
boots raise their fault and never report either code clear, including after a
successful retry. Reboot, omitted/unknown status and old journal records alone
are not recovery. Networking stays off until a normal boot, so fault history
persisted on LittleFS reaches the server only later; D's RAM history is lost.

### Trip-first upload scheduling (#125, implemented)

`diag_worker.h` runs on the existing core-0 network task, after each GPX batch
on that connection. It uses `hasPending()` before snapshot preparation and
before **every** diagnostics request, including rejection probes. Eligibility
still means complete, non-active `.gpx` on either mounted store; incomplete,
`.tmp` and `.bad` files do not consume the queue limit. If another GPX becomes
eligible, diagnostics return so the next network cycle can upload it.

EventLog exports unchanged records in batches of at most **64**, with at most
30 KiB for the events array. The worker subtracts the actual snapshot/envelope
size from the **32 KiB complete-body cap** and checks the finished body again.
Old records keep their original boot IDs, sequences, UTC and JSON, alongside
a snapshot of this boot. The snapshot includes firmware version, boot
identity/counter, extended awake uptime, active episodes, observed codes, backend,
dropped count and the boot-local E5001 counter. Unobserved conditions stay unknown.
Duplicate network codes across trip/events targets merge into one active
condition when either target is unhealthy; clearing one target does not clear
the other. The optional
`pending_events` count is omitted to avoid a full journal scan per upload.

One newly persisted revision is allocated per drain opportunity (normally no
more than one per 30-second awake network tick). All batches and probes during
that opportunity reuse **identical snapshot bytes**, for at most ten seconds.
The next opportunity captures new observations and allocates another revision;
there are no snapshot allocations per scan, producer event or idle service slice.
Observed/active changes also request a new check-in on the next normal awake
network tick, even after their journal events have drained. Successful upload
recovery therefore cannot leave an active fault snapshot stranded until the
24-hour heartbeat. Unchanged observations do not mark a snapshot dirty.
Each allocation is one 80-byte metadata slot write/read-back per mounted SD or
LittleFS journal, in addition to normal ack/segment/loss metadata. NVS writes
the recovery marker only on topology/recovery/failure transitions, as described
above. FAT/LittleFS wear and power-cut acceptance still need hardware testing.

Stop when the queue is empty, a trip becomes eligible, `gNetHold`/`gNetAbort`
is set, or **10 seconds total** elapse. `diag_http.h` retains HTTPClient for
headers/auth and HTTP framing, but its `DeadlineClient` enforces an absolute
wall-clock deadline through all socket operations. Hostnames use one bounded
asynchronous lwIP DNS lookup (static callback storage survives a timeout),
connect uses the remaining time, and writes use nonblocking `send` with deadline
checks instead of NetworkClient's independent select/retry waits. Header reads,
slow trickles and ack-body reads share the deadline. HTTPClient can add its
short fixed retry/yield overhead at the boundary; no long call gets a fresh
10-second allowance. Filesystem/VFS operation latency remains a hardware
acceptance item; diagnostic I/O uses a zero-wait mutex and the existing
per-operation trip-demand gate, releasing the mutex before networking.

#### Acknowledgments, rejection and independent backoff

Only a 2xx with a completely parsed, bounded acknowledgment matching the exact
sent ID set can advance EventLog's durable cursor. The parser accepts either
ID/key order, rejects duplicates, extra/missing IDs, malformed integers/JSON,
unknown fields and trailing bytes, and never logs a response body. A reply must
have a Content-Length of at most 8192 bytes (the ingest endpoint supplies it).
Unsupported/chunked, oversize, incomplete or invalid replies retain the batch
and raise E5010 with `parse|missing_ids|unexpected_ids|duplicate_ids`. Failed
local ack persistence also retains retryability; an ambiguous response retries
the original IDs, relying on ingest deduplication.

Event failures use separate RuntimeFaults slots from GPX: E5003 transport,
E5004 401, E5005 413, E5006 400, E5007 409, E5008 5xx and E5009 other positive
status, with `target=events`. A complete valid reply clears only events-target
failures/E5010. Transitions/repeats retain the existing 15-minute/60-second
producer limits. The worker stops on its first failure, so it cannot drain an
unbounded chain of its own fault events.

Diagnostics retries start at **60 seconds**, double to **one hour**, and start
at one hour for 401. AP/join failures on diagnostics-only cycles use that same
independent backoff. GPX attempts, successes and failures never change it;
diagnostic outcomes never change GPX retry timing. Eligible GPX always wins,
even when its previous attempt failed. An absent placeholder server therefore
causes bounded radio attempts, no diagnostic response logging and no growing
request/response allocation.

413 halves both the next event-count and event-byte limits, down to one event
and a 1024-byte array budget; a singleton still rejected as too large is retained
with backoff. 400/409 halve the count until a singleton is isolated. A singleton
rejection first sends **the identical snapshot with an empty events array**.
Only a valid 2xx/empty ack on that probe proves the offending item is the event.
A rejected probe retains all records; a snapshot 409 suspends device-wide revision
allocation and requires explicit revision recovery.

`EventLog::isolateRejected` only accepts its outstanding singleton ticket.
It writes and reads back one CRC-protected `/diag/rejected` inspection frame,
then commits the discard cursor and one dropped/pending-loss increment together.
Both backend quotas reserve that one frame's allocation unit before writes;
replacing the inspection slot stays bounded. Loss is reported even while the
last rejected frame remains available for inspection. A torn archive or cursor
write retains retryability, never advances a valid multi-record batch and never
counts an HTTP rejection as successful delivery. RAM heads stay queued until
normal journal service persists them, so isolation never claims a volatile
cursor/count was durable. No trip is removed to make quarantine space.

#### Opportunistic heartbeat and memory

A GPX 200 or fully validated diagnostic 2xx records successful server contact.
After **24 hours** without it, an empty-event check-in can use the existing
awake network task opportunity. No wake timer or extra task is added. The timer
extends `millis()` across wrap while awake. Contact age is unknown after a
power-cycle, so the first awake network opportunity conservatively checks in;
sleep duration is not invented from boot uptime. Normal journal upload also
refreshes contact. The server continues to measure its own contact receipt time.

The batch (35,280 bytes on ESP32-S3), request (32,769), snapshot (6,144),
reply (8,193), and two observation arrays (1,536 each) share one **85,464-byte
PSRAM workspace**, including alignment padding. Setup allocates it once with
`heap_caps_malloc(..., MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)`, before starting the
network task; it is never freed or retried. There is no internal-heap fallback.
Absent PSRAM or allocation failure prints exactly one `WARNING:` and disables
diagnostics upload for that boot. Local journaling and GPX uploads continue.
No existing catalog code fits this failure, so it does not fabricate an event.

Small worker controls, the journal, producer builders and cross-core atomic
publications stay in internal RAM. Only setup and the core-0 worker access the
PSRAM workspace: the PMU ISR only latches its internal flag, DNS callbacks use
their separate internal storage, and observation copies use atomics without a
cache-disabled critical section. Journal flash I/O uses internal frame/metadata
scratch; the workspace is not passed to flash drivers or ISR/DMA callbacks.
ESP-IDF's normal flash guard suspends task execution on both cores while caches
are disabled. No application cache-disabled path accesses this workspace.

HTTPClient's small objects and bounded parser/socket scratch remain on the
existing **12 KiB** network stack; no large upload buffer goes there. Debug
mode prints free internal heap, its largest free block and free PSRAM on entry
and once after the first diagnostics HTTP attempt made with debug on (including
failure). These are measurement hooks for #6. During and after the real SD uploads on
2026-10-21 (UTC), the debug build's minimum free network stack was
**7,436 of 12,288 bytes**, with GPS logging active. The earlier no-upload
measurement after #182 was **7,356 free bytes** (2026-10-20), as recorded in
#6. The server-down/recovery run on 2026-10-21 recorded 7,556 free bytes;
#6 is closed with the 12,288-byte allocation retained. HTTP 401 and 5xx
were bench-checked on 2026-11-01 (UTC); HTTP 413 and visible-AP join failures
remain untested on hardware. Real-upload heap measurements and the
untested upload paths still need checks. No host test establishes ESP32
heap/stack headroom, SD latency or power-cut safety.

Earlier Supreme/core 3.3.11 compile results with PSRAM enabled: static RAM fell from
193,804 to **108,356 bytes** in the former debug build, and from 169,836 to
**84,388 bytes** in the former production build (85,448 bytes recovered in each build).
Both are 2,736 bytes above the pre-upload-worker baselines of 105,620/81,652
bytes; the remaining internal state includes the atomic publications and
upload fault producer. These are link-time sizes, not live heap measurements.

The host suite drives the actual worker/netCycle with fake WiFi/HTTP/filesystems,
including count/body boundaries, current/old boot snapshots, trip/hold/abort and
slow-response deadlines, exact acks, every error class, independent retries,
heartbeat, revision loss and durable isolation. PSRAM stubs cover successful
one-time allocation, missing PSRAM and allocation failure: one warning, no
diagnostics-driven radio activity, continued journaling and successful GPX
uploads. Every complete generated test
request passes the real `common.diagnostics.parse_request` validator and real
Flask ingest test-client commit plus duplicate replay, without opening sockets.

### Display policy and Supreme hardware (#123/#127/#128/#130)

DisplayModel (`display_model.h`) is pure, header-only C++ policy; only the
SH1106 adapters touch hardware, both exclusively on core 1. Stage 4 ships as
one firmware, starting with debug off at every power-on (never persisted).
While running, hold BOOT (GPIO 0), then tap POWER to toggle debug. BOOT is
INPUT_PULLUP, sampled only after boot, with a 40 ms low debounce and a fresh
sample when the verified short-key batch is handled. It has no other Stage 4
user. Holding BOOT during power-up still selects the ROM download mode.
A two-second OLED message and `Debug mode: on` / `off` serial line confirm
changes. No button or OLED work runs on core 0 or in the PMU ISR.

- **Debug on:** the existing continuously lit bench/shakedown page: FSM,
  satellites heard/used, GPS AGC, fix/HDOP, storage, queues, network and battery.
- **Debug off (production):** off by default using SH1106 power save, with
  steady cards lasting 30 seconds by default (`DISPLAY_CARD_MS=30000`, in
  milliseconds; build override `-DDISPLAY_CARD_MS=10000` for 10 seconds).
  The default is planned to return to 10 seconds after the first road tests.
  No flashing, scrolling or always-on content.
  The adapter uses maximum SH1106 contrast (255), per #166: the panel is about
  100 nits and lights only briefly. The owner approved the large-font layout
  on the bench on 2026-10-21 and accepted daytime readability as a panel limit
  (#206/#158). OLED current was deliberately not measured; road behavior
  remains open under #206. An always-on design would need a separate burn-in policy.

Production cards show **satellites heard**, from fresh
`DebugGsv::signal(now).heard`: unique GSV entries with C/N0 > 0, or `--` when
stale/unknown (15-second expiry). The predicted "in view" total includes
satellites the receiver cannot hear; on road test 1 it stayed at 30 indoors
with no fix. GSV already runs in both builds, so this adds no GPS traffic.

Core 1 evaluates motion in `loop()`. VBUS absent (`gVbusLost`) means Parked,
including while a trip is being finalized. Otherwise ARMING/ACTIVE/COOLDOWN,
or a fresh trusted speed >= the 2 mph end threshold, means Moving. IDLE
with a fresh trusted speed below that threshold means Parked. Anything else
means Unknown. Speed/fix must be valid, at most 2.5 seconds old, and have
passed the existing accuracy decision; a void RMC invalidates production
motion trust immediately. FINALIZING alone is not evidence of motion.

Production policy:

- Boot shows one StatusCard on the first model update, including Unknown motion:
  no trusted GPS fix is needed. It lasts `DISPLAY_CARD_MS` (default 30 seconds),
  unless moving is detected or an error card takes over. It consumes the boot
  trigger, so the first Parked observation does not show a second boot card.
  Finalization back to IDLE still queues a status card for Parked only.
  Entering Parked with unresolved faults still reminds once.
- Every new accepted ERROR/CRITICAL condition episode queues one ErrorCard in
  any motion state. Each gets `DISPLAY_CARD_MS` (default 30 seconds), in
  observation order. Repeats cannot extend or re-light it.
  The 64-entry queue drops excess new episodes once;
  it never loops through unresolved faults. WARNING/INFO cannot wake a card.
- A short POWER press explicitly requests a StatusCard in any motion state.
  The single build always includes `XPOWERS_AXP2101_PKEY_SHORT_IRQ` in the
  shared enabled/batch mask and tests `irq.has(...)` only after a known batch,
  outside degraded mode. BOOT held toggles debug instead. Unknown batches
  request no card and cannot toggle debug. Long press keeps the existing
  clean battery power-off behavior. Only a `user_long_press` refused with
  confirmed USB power requests a UsbPowerCard, through a core-1 flag consumed
  by the renderer. It uses three lines in the 14-point font and the normal
  `DISPLAY_CARD_MS` interval; it can replace a status card, and a later short
  tap can request status again. An interrupted error card resumes afterward,
  followed by queued errors. Debug off dismisses the message and pending
  errors, so they cannot replay after a mode switch.
  A new error preempts a routine status card;
  a user request can replace the current card and holds its full
  `DISPLAY_CARD_MS` (default 30 seconds) before queued errors resume.
  Routine status cards blank when motion becomes Moving; Unknown does not
  blank them. Every visible status card (boot, trip, reminder or POWER tap)
  refreshes its heard count and active codes without restarting its timer.
- StatusCard: `Sats: N` or `Sats: --`, then `OK` when no faults are active,
  otherwise only active warning-and-above `E####` codes, most severe first
  (code order breaks ties). Duplicate active codes from the two upload
  targets appear once. Up to four codes fit; if more are active, the final
  slot shows `+N` for all omitted codes (three visible codes, up to `+61`).
  There is no `Also:` label or catalog OLED description. ErrorCard shows
  only one large centred `E####`, including for unknown codes.
- Core 1 copies the same atomic observed/active/accepted-episode snapshots as
  the upload worker, from loop, trip-network and event-network producers.
  Each source is read once without waits or new cross-core locks. A concurrent
  publication retains that source's last complete snapshot. Episode tracking
  includes source identity and survives clears; unknown/unaccepted raises
  cannot invent display events. This is bounded snapshot observation, so a
  condition raised and cleared entirely between reads can be missed.
- Only one renderer owns the panel at a time. Toggle feedback uses the
  production adapter; after it expires, the owner draws fresh pixels before
  waking the panel. Debug off returns to power save unless a fresh card is due.
  The production model consumes episodes while debug owns the panel, without
  queuing stale cards. POWER alone overlays a full-duration status card in debug
  mode, then resumes the live page. Either adapter failing disables both paths
  for the boot. Runtime debug also enables RF, temperature and heap serial
  diagnostics; the charge guard, GPS trust and antenna checks remain active
  with debug off. The core-0 worker still services one journal record at a time.
- Shutdown blanks the model and puts the panel in power save before the final
  VBUS check/`PMU.shutdown()`. Cancelled/failed shutdown resumes blank until
  a later display trigger. Production initialization uses `initDisplay()`
  (which leaves SH1106 asleep), rather than `begin()` (which wakes it), before
  degraded startup branches. Degraded loops never run display updates.

The production adapter uses Wire SDA17/SCL18, 0x3C, 400 kHz and a 10 ms Wire
timeout, with checked write lengths and transaction release on short writes.
Initialization or transfer failure latches the adapter off for the boot and
raises **E6001 DISPLAY_UNAVAILABLE** through the normal diagnostic hook, with
one accepted raise and the existing bounded diagnostic repeat policy. It never
retries hardware initialization or blocks recording, uploads or power handling.
A disconnected panel cannot be commanded asleep after the bus has failed.
The screen is a brief aid; the server history remains the durable fault record.

#### Production fonts and pixel layout

U8g2 **2.36.18** supplies all three `_tr` fonts, including upper/lower case,
digits, `:`, `+`, `-` and `OK`. `display_font_metrics.h` records actual bitmap
bounds, offsets and advances decoded from the installed `u8g2_fonts.c` by
`firmware/host_test/check_display_fonts.py`. The model contains structured
content; the pure `production_display_layout.h` computes positions from
these metrics, and the adapter draws using baseline coordinates.

The 128x64 panel keeps at least **2 pixels of margin** on every edge.
Ink ranges below use `[left,right)` and `[top,bottom)`: right/bottom are
exclusive, so width/height are the difference of the bounds. Baselines are U8g2
draw coordinates. Horizontal centring uses actual ink bounds, including
glyph offsets, rather than nominal font sizes or character counts.

- **Sats: `u8g2_font_logisoso26_tr`.** `Sats: 99` advances 123 pixels;
  its ink is 119x27. Origin `(2,29)` gives ink x=[4,123), y=[2,29).
  The `3` glyph extends one pixel below baseline, so `Sats: 33` occupies
  y=[2,30). Digits advance 17, `S` 17, `a` 19, `t` 10, `s` 16,
  colon 10 and space 17 pixels. `Sats: --` advances 121 pixels and fits.
  The next logisoso size, 28, has 125 pixels of ink for `Sats: 99`,
  exceeding the 124-pixel inner width. Heard counts above 99 retain their
  numeric value using the smaller `helvB14_tr` font when needed.
- **No faults or one code:** the lower line also uses `logisoso26_tr`,
  baseline 61, centred across the panel. `OK` has 34-pixel advance and
  30x26 ink at origin `(47,61)` (ink x=[49,79), y=[35,61)).
  `E9999` advances 85 pixels with 81x26 ink; digit `3` can extend the bottom bound to 62 (last occupied y=61).
- **Multiple codes: `u8g2_font_helvB14_tr`.** Two 62-pixel cells per row
  start at x=2 and x=64, with at most two rows (four slots). `E9999`
  advances 53 pixels and has 50x14 ink (`E` advances 13; digits 10).
  With three/four tokens, baselines are 46 and 62, placing ink at
  y=[32,46) and y=[48,62). For `E9999`, draw origins are x=6 and x=68,
  placing ink at x=[8,58) and x=[70,120). With only two tokens, baseline
  55 centres the single row in the lower area. `+99` advances 31 pixels,
  with 29x13 ink; all `+N` values through two digits fit a full slot.
  Font 18 in the same bold Helvetica family cannot fit two codes per row.
- **Error: `u8g2_font_logisoso38_tr`.** `E9999` advances 121 pixels
  (`E` 25; digits 24) with 116x38 ink. Origin `(3,51)` centres it at
  x=[6,122), y=[13,51). Codes containing `3` have 39-pixel ink height;
  vertical centring accounts for this. The next size, 42, has 129-pixel
  ink width for `E9999`, exceeding the inner width.

Host tests exhaust all four-digit error values and status-code layouts,
satellite values 0..99, stale GSV, unknown codes, all 1..64 active-code
counts, and two-digit overflow strings. They check panel margins, glyph
coverage, row/column separation and adapter font/coordinate selection.
When U8g2 is installed, the host runner also compares the committed metrics
with the actual library (set `U8G2_DIR` for a nonstandard installation).
The catalog's OLED-text validation rules still apply.

The following are **Supreme port targets to verify against the board revision
and on the bench**, not pin changes for the existing T-Beam V1.2 sketch:

- SH1106 OLED, 128x64, on Wire SDA17/SCL18, address 0x3C (bench-confirmed with the debug adapter).
- PCF8563 RTC, address 0x51 on the PMU bus Wire1 SDA42/SCL41.
- SD MOSI35/SCK36/MISO37/CS47, powered by BLDO1; hold IMU CS34 high.
- Supreme ALDO3 is LoRa, ALDO4 is GPS. This differs from the V1.2's measured
  GPS rail; copying these assignments into that sketch would be wrong.

#128 reads Control_1 and the seven time registers once at boot, on core 1 after
PMU bus recovery/startup. STOP or seconds VL means `stopped` (expected after
power loss), before BCD/calendar validation including leap days. The century
bit selects 19xx (set) or 20xx (clear); only 2026 through 2099 is accepted.
RTC time is lower-confidence holdover, not evidence of a recent GPS/SNTP sync.
Once trusted time arrives, core 1 checks drift once: an invalid RTC or an offset
of at least two whole seconds schedules a write in the first 100 ms of a UTC
second, without waiting in the loop. All seven time registers are written in
one burst (VL cleared, 24-hour, 20xx), STOP is cleared, then registers are read
back and validated. Only one successful setting is allowed per boot; failures
have at most three total attempts spaced by at least a minute. The successful
write epoch is saved once in `diag/rtc_set` NVS for next boot's age. Both cores
can publish trusted clock syncs, but only core 1 touches Wire1 or RTC NVS.
Coordinate-free serial lines report boot validity/last-set age and verified
settings. On the bench (2026-10-20) the RTC kept counting through
`PMU.shutdown()`: it still held the factory firmware's setting, valid and 24 s
off, after several power-offs since 2026-10-18. Once set from GPS it drifted
2 s in 4.9 h (about 10 s/day). The PCF8563 has no offset register, so the
once-per-boot correction is the only adjustment.
Never rewrite older journal records' timestamps after synchronizing;
uptime and receipt time preserve their original meaning. #130 validates these
behaviors plus degraded storage, reset reporting, motion suppression, deep
sleep, upload interruption, backlog drain and notification recovery on hardware.

### Server storage and Device health (#118/#119)

Ingest owns `/data/diagnostics/events.db`, with events, boots and devices tables
(and schema/index metadata as needed). Use SQLite `synchronous=FULL` and DELETE
journal mode, not WAL. The viewer mounts `/data` read-only and opens the DB
with `mode=ro`; relying on writable WAL sidecars there would fail. Keep write
transactions short, enforce unique identities, and retain server receipt time
separately from optional device UTC. Diagnostics database failure must not turn
an accepted GPX upload into an error. Successful GPX uploads independently
refresh device contact time when their device identity is supplied/validated.
Legacy uploads with no identity must not be assigned to a guessed device.

The database path is derived from `INGEST_DATA_ROOT` (default `/data`), so it
lives inside the existing data mount. Initialization is lazy: the first event
batch or validated GPX contact creates it; a broken diagnostics database must
not prevent GPX service startup. The catalog is loaded and validated at ingest
startup; missing/invalid `catalog.json` fails startup clearly. Each connection
uses a 5000 ms busy timeout. Schema creation/versioning shares the same
`BEGIN IMMEDIATE` writer lock as updates, safe across Gunicorn workers.

Schema version 1 (all receipt timestamps are UTC ISO 8601 with `+00:00`):

- `schema_version`: singleton row `(singleton=1, version=1)`. Unsupported versions
  fail closed; migrations belong to the change that introduces a new version.
- `devices`: primary key `device_id`, `last_contact_at`, nullable `status_sequence`,
  `snapshot_json` (complete canonical latest check-in), `snapshot_received_at`.
  GPX-only contact creates a device with no snapshot. A retry can refresh contact
  without making the snapshot fresh. Conflicting batches refresh nothing.
- `boots`: primary key `(device_id, boot_id)`, `first_received_at`, `last_received_at`.
  Both event boots and check-in boots are recorded; receipt order is not boot order.
- `events`: primary key `(device_id, boot_id, sequence)`, `code`, nullable derived
  `severity`, nullable device `utc_ms`, server `received_at`, `canonical_json`
  (the complete event). Unknown codes have NULL severity. Event retries preserve
  the original receipt. History/code indexes support #119 filtering/pagination.
- `device_conditions`: primary key `(device_id, code)`, `state` (`active`, `clear`,
  `unknown`), nullable `episode_id`. A newer snapshot makes all previously known
  but omitted conditions unknown, even within one boot. Observed inactive codes
  become clear; active codes retain their supplied episode. Codes never seen in
  this table are unknown too. Historical events never change these states.

Canonical content sorts object keys and uses compact ASCII JSON. Array order
remains significant, so firmware must preserve list order when retrying the
same revision. Optional-field omission differs from an explicitly supplied
field; revisions can be reused only for identical decoded check-ins.

All three Python Docker builds use `server/` as context, copy their application plus
`common/` (including `catalog.json`) to `/app`, and set `PYTHONPATH=/app`.
`server/.dockerignore` defaults to denying everything and explicitly allows
only the required runtime files. Secrets, data and development artifacts stay
outside the build context. All three services load the catalog once at startup and
fail fast if it is missing or invalid.

#### Device health page (#119, shipped)

Open `/device-health` from the viewer navigation after logging in. It shows
each device's last server contact and age, latest snapshot receipt and age,
firmware, storage backend, pending/dropped events, boot-local counters, uptime
and status revision. Contact alone does not establish health or refresh a
snapshot's age. No fixed freshness threshold is imposed here; silence policy
belongs to #121.

Conditions are grouped active / unknown / clear from `device_conditions`.
Never-observed catalog conditions with `journal=transitions` also appear as
unknown (server-raised 7xxx codes excluded). Unknown and Clear start collapsed
because either can contain most of the catalog; click their headings to expand
them. Each heading keeps its condition count visible. Active stays expanded.
Each known code has its current catalog severity, title, OLED text,
explanation and recovery. Unknown codes show `Unknown E####` without severity.
Older events and boots never alter current conditions in the viewer.

History spans all devices, newest server receipt first, with stable identity
tie-breakers and 50 events per page (offset pagination). Code and severity
filters can be combined. Severity display and filtering use the **current
catalog by code**, never the stored `events.severity`, including stored NULL
or obsolete values. Query parameters are strictly validated; invalid or
duplicate parameters return 400. Event rows show transition, episode, shortened
boot ID, sequence, context key/value text, device time/source/sync age (or
device clock unknown), and server receipt. All timestamps use `DISPLAY_TZ`;
all stored text is autoescaped.

The viewer checks schema version 1 and reads a consistent transaction through
a `file:...?mode=ro` connection with a two-second busy timeout. It never creates,
migrates, writes or recovers the diagnostics DB. An absent file gives a normal
"No diagnostics yet" page. Open/read errors, unsupported schemas, lock timeout
and hot journals requiring recovery give **Diagnostics unavailable**, HTTP 503,
and a viewer warning log, with no healthy empty state substituted.

### Optional monitor and notifications (#120/#121, shipped)

The stdlib-only `mileage-monitor` service optionally publishes metrics on port
8093; the endpoint is disabled unless configured. Each tick reads `/data:ro`
and stores its own delivery/deduplication state in a separate writable volume,
then waits **60 seconds after the completed tick**. ntfy, GELF and Loki run
sequentially before metrics publication; slow synchronous exports delay the
next database read, alert check and metrics refresh. Sink failures and progress
are handled separately, but their timing is shared. It watches **every device that has ever checked in**; there is
no `MONITOR_DEVICE_IDS` watchlist. Removing an obsolete device requires a future
explicit administrative retirement policy, rather than an implicit
watchlist omission. The intended installation has a single tracker.

Notifications use [ntfy](https://ntfy.sh), **self-hosted** on the same
server: it is free, open-source software with no account or subscription, and
it is included as an optional Compose service. Phones subscribe with the free
ntfy app (Android: Play Store or F-Droid; iOS: App Store). Gotcha: on iOS,
instant push from a self-hosted server needs its `upstream-base-url` set to
`https://ntfy.sh`, which relays only a wake-up poke (no message text) through
the public instance, still free; Android needs no relay. ntfy allows anonymous
access by default, so #120 sets `auth-default-access: deny-all` and uses a token.
Notifications are off by default. Settings in the separate private `monitor.env`:

```dotenv
MONITOR_ENABLED=false
NOTIFY_ENABLED=false
NTFY_URL=https://<host>/<topic>
NTFY_TOKEN=<token>
NOTIFY_MIN_SEVERITY=error
NOTIFY_HOLD_SECONDS=300
NOTIFY_REPEAT_HOURS=24
NOTIFY_MAX_PER_HOUR=6
MONITOR_STALE_DAYS=7
```

Enabled settings are validated at startup; keep tokens in private environment
files, never event context or public examples. Snapshot freshness is **24 hours
inclusive**, aligned with the opportunistic heartbeat, measured from accepted snapshot receipt time.
GPX-only contact does not make an old health snapshot fresh.

Alert only for catalog-eligible current faults in a fresh snapshot. Hold a new
fault for 300 seconds by default; critical faults bypass that hold, while
deduplication and rate limits still apply. Keep one **open alert per device/code**
from its first successful send until an explicit clear and successful resolution.
Firmware creates a new `boot_id` on every boot, including each deep-sleep wake,
so a new device episode of the same code continues that open alert. Even after
unknown status, send only reminders on `NOTIFY_REPEAT_HOURS` measured from the
last successful alert/reminder, without a new hold. After resolution, a later
raise starts a new alert subject to hold. Above the hourly cap,
coalesce into a digest and persist pending work; avoid a later replay storm.
Send a resolution only if the corresponding device/code was actually alerted and
an ordered snapshot explicitly observes it clear. A reboot, stale snapshot,
missing code or delayed clear event alone is not recovery.

Deliver at least once: persist delivery intent and retry failed posts. A crash
between ntfy acceptance and state update can produce a duplicate; do not claim
exactly-once delivery. Notification text excludes location, raw context and
credentials. Missing database, stale data and ntfy outages do not affect trips.

The monitor compares last **server receipt time**, including accepted GPX contact,
against `MONITOR_STALE_DAYS`. Never use device UTC for silence. Derive 7001
locally with a stable monitor episode and resolve only after genuine newer
contact. Silence is a monitoring policy separate from the device-fault minimum
severity and hold: its warning-level alert is enabled by `MONITOR_ENABLED` with delivery
gated by `NOTIFY_ENABLED`, otherwise the default `error` threshold would
silently suppress it.

#### Shipped service and state policy

`install.sh` asks which outputs to turn on (its Notifications step). For manual enablement, use
[DEPLOY_DEBIAN.md Step 11](DEPLOY_DEBIAN.md#step-11--optional-device-notifications). `monitor` and `ntfy` are independent Compose profiles;
an existing self-hosted ntfy can replace the bundled one. Profile gating avoids
an idle container on a normal install. If explicitly started while disabled,
the monitor logs that it is idling, loads the required catalog, and does no
database reads or sends. Compose loads `monitor.env.example` by default; set
`MONITOR_ENV_FILE=./monitor.env` in `.env` to select the private file. Neither
the ingest token nor viewer secrets enter this service. No new Python packages
are needed. ntfy uses a pinned image, a deny-all config, `BIND_ADDR` port 8092,
and its own `ntfy-state` cache/auth volume.

The monitor opens schema-v1 `events.db` with `mode=ro`, a two-second busy
timeout and a consistent read transaction, closing every connection. No
initialization, migration or recovery is attempted there. An absent DB means
nothing to do. A read/schema/lock/hot-journal failure logs unavailable and
suspends all sends, including pending retries, until a successful read.
Snapshot parse/validation failures are isolated per device, including snapshots
that no longer satisfy the current catalog. That device's fault conditions
become unknown: they cannot trigger fault alerts, reminders or resolutions.
Silence detection still uses its valid last-contact receipt, and other devices
continue normally. A damaged identity/contact receipt also isolates that row;
without a valid contact, its silence cannot be evaluated. Log only the error
type once per device/revision, retaining that warning record across restarts.
Only accepted snapshots and contact receipt times drive policy; journal events
and their stored severity are never queried.

`monitor-state:/state/monitor.db` has its own version-1 schema, DELETE journal
and synchronous=FULL. `episodes` retains the original device/code/episode anchor, current
evidence, eligibility start, observed revision, silence contact anchor, first
alert, last successful alert/reminder and resolution delivery. A unique partial
index permits only one unresolved sent fault per device/code; later device
episodes update that open row. Silence (7001) keeps its contact-based identity.
`work` coalesces
one pending action per episode/kind; `outbox` commits the selected delivery
intent before posting; `deliveries` holds the rolling-hour successful POST
window; `retry` persists global retry attempts and the next attempt time.
`snapshot_warnings` deduplicates unavailable-snapshot warnings by device/revision.
Initialization and versioning share one transaction; an unsupported state
version fails startup. Run one monitor replica per volume.

Hold starts at the monitor's first eligible observation, survives restarts,
and restarts after observed unknown/stale/ineligible evidence. Critical faults
and server silence bypass hold. Unknown status retains open sent alerts;
neither a new boot nor a new episode emits a new alert or an invented resolution.
An explicit newer clear can resolve the open alert for that code. If the fault
returns before a deferred resolution is delivered, the same alert stays open.
Silence identity is stable from the last contact receipt and resolves only
when that receipt advances, not when a server clock adjustment changes age.

Every successful POST consumes a slot, including reminders, resolutions and
digests. At the rolling-hour cap, unsent work stays durable and marked for
digest delivery when a slot opens; a burst is coalesced rather than replayed
as individual delayed alerts. Digests are split at 3500 UTF-8 bytes, below
ntfy's default message limit; remaining entries stay queued. Recheck pending
fault alerts/reminders against current fresh eligibility before retrying:
withdraw them if they became unknown, stale, ineligible or clear. Never send a
resolution for an alert with no recorded successful delivery; a deferred fault
resolution also requires the latest fresh snapshot to explicitly observe it
clear. Retry failures
globally after 60 seconds, doubling to at most 3600 seconds, so an ntfy outage
does not cause one retry storm per condition. Posts have a ten-second timeout
and do not follow redirects with the token. At-least-once duplicates after an
ambiguous response/crash can exceed the recorded successful-POST cap.

Bodies contain only alert/reminder/resolution, E-code, catalog title, validated
device label and catalog recovery hint (silence adds whole days without contact).
No context, device clock, location, links or credentials are included. Configure
booleans and bounded numeric values as described in the deployment settings;
default silence is exactly seven days with an inclusive threshold. Firmware
heartbeat integration is implemented by #125; consistent state backups shipped
under #129 (see [backups and acceptance](#backups-and-acceptance-129130)).
Installer test uploads omit `X-Device-Id` so a routine `--check` does not enroll
an ever-watched synthetic device. A synthetic upload that will delete its files
must be untimed (no `<time>` elements) so its `X-Trip-Id` is honoured, and must
verify the returned `stored_as`: ingest ignores `X-Trip-Id` for timed GPX.

#### Prometheus metrics (#134)

The monitor can expose `/metrics` for a Prometheus server that you run
separately. The `monitor` Compose profile publishes host port 8093 on
`BIND_ADDR`; the HTTP listener starts only when `METRICS_ENABLED=true` and
requires its own `METRICS_TOKEN` (at least 32 printable ASCII characters).
Keep this port on a trusted LAN or VPN: bearer-token authentication is its
only protection and traffic is plain HTTP. Do not reuse the ingest token or
viewer secrets. Setup, scrape configuration, network path and Grafana starter
files are in [DEPLOY_DEBIAN.md Step 11e](DEPLOY_DEBIAN.md#11e--optional-prometheus-metrics-for-external-grafana).

The endpoint serves a published in-memory snapshot. The HTTP thread does not
open SQLite or block the monitor tick. Each scrape reports server contact and
snapshot ages, current catalog condition states (active/clear/unknown), the
latest pending/dropped counts and catalog check-in counters, alert episode
counts (active/sent/resolved), database read errors, last successful read time
and the configured silence threshold. Labels are limited to validated device
IDs, current catalog code/symbol/severity, and fixed state/decision values;
there is no event text, coordinates, SSIDs or credentials. Counter-like alert
values are gauges derived from monitor state, so restoring monitor-state can
reduce them.

#### Graylog export (#135, shipped)

Owner decision (2026-10-16): Graylog runs on a separate server the owner
manages; this repo never bundles it and only sends to it. Setup steps and
starter Event Definitions are in
[DEPLOY_DEBIAN.md Step 11d](DEPLOY_DEBIAN.md#11d--optional-send-diagnostics-to-your-own-graylog).
The code is `server/mileage-monitor/gelf.py`, called from each monitor tick
after the ntfy step. Graylog failures are caught separately and do not change
ntfy delivery state or trip uploads. A slow export still delays later work
and the next monitor tick; this is failure/progress isolation, not timing isolation.

**Why facts, not ntfy decisions.** ntfy's decisions are phone policy: the
hold, the hourly cap and digests, dropping a not-yet-sent alert when its fault
clears. Graylog has its own alerting (Event Definitions, grace periods), so
forwarding pre-throttled decisions would apply that policy twice, and with
ntfy off it would forward nothing. GELF therefore carries two kinds of facts,
independent of `NOTIFY_ENABLED` and `NOTIFY_*` settings (only
`MONITOR_STALE_DAYS` and the 24-hour snapshot freshness apply):

- `event`: cursor-based, at-least-once forwarding of `events.db` rows in rowid
  order. An interrupted send before cursor commit can produce duplicates. The
  cursor is the last delivered rowid (`gelf_cursor`). The ingest never deletes
  events, so rowids only grow; if the table's highest rowid is ever below the
  cursor, or the row at the cursor no longer has the stored fingerprint, the
  cursor resets to 0 and history is resent. This also detects a restore whose
  rowids were refilled before the next tick. Graylog may see duplicates.
  Fingerprints live in the additive `event_cursor_markers` table; existing v1
  cursor tables are unchanged. On upgrade, a nonzero cursor without a marker
  replays once because its old identity cannot be verified.
  A damaged stored row is skipped with a type-only warning and the cursor
  moves past it.
- `condition`: the state of each (device, code) as the monitor sees it:
  `active` or `clear` from a fresh (≤ 24 h), valid snapshot, `unknown` when a
  previously known condition has no current evidence, and E7001 `active` or
  `clear` from server last-contact age. `gelf_conditions` holds the last state
  delivered per (device, code); a message goes out only when it changes. A new
  episode while the state stays active is not a change: every deep-sleep wake
  has a new `boot_id`, and a persistent fault must not produce one message per
  wake. On first enable, every device's current states go out once as a baseline.

Level: catalog severity (critical 2, error 3, warning 4, info 6; unknown code
5) for active conditions and raise/repeat/notice events; 6 for clear/unknown
states and clear events. The trust boundary is **catalog/protocol validation
at ingest**, including identifiers, clock bounds and per-code context. GELF
export derives descriptive fields from the catalog but only checks stored
context key/value shape: short token strings, integers and booleans (sent as
`"true"`/`"false"`). It does not re-check per-code key membership, integer ranges
or enum membership; device-clock integers are type-checked, not range-checked.
A damaged database row can therefore export an unknown context key or an
out-of-range value. Loki instead filters context to the current code schema
and validates the resulting event before export. The source is the fixed
`mileage-tracker`; the reserved `_id` is never used.

Transport: GELF HTTP (one message per POST, success on 2xx; Graylog answers
202), redirects never followed, optional single extra header; or GELF TCP,
null-terminated frames on one connection per batch, optional TLS with
verification on by default and an optional CA file. TCP cannot confirm
receipt, so any TCP failure resends the whole batch. Delivery is at least once.
Each tick sends at most 200 messages, conditions first; progress is committed
for the messages delivered before a failure. Failures back off from 60 seconds,
doubling to an hour, in `gelf_retry`, separate from ntfy's `retry`. The GELF
tables are additive to monitor schema v1 (`CREATE TABLE IF NOT EXISTS`), so an
existing `monitor.db` gains them at startup without a migration.

#### Loki push (#136)

The monitor can push to an external Loki endpoint; Compose does not install
Loki. Set `LOKI_PUSH_URL` to its `/loki/api/v1/push` endpoint in `monitor.env`
to enable the sink. Optional basic auth (`LOKI_BASIC_AUTH_USER` and
`LOKI_BASIC_AUTH_PASSWORD`) or bearer auth (`LOKI_BEARER_TOKEN`) is supported,
but not both. `LOKI_TENANT_ID` sets `X-Scope-OrgID`. Credentials are not
accepted in the URL. Setup and sample LogQL are in
[DEPLOY_DEBIAN.md Step 11f](DEPLOY_DEBIAN.md#11f--optional-push-to-external-loki).

Each new `events.db` row becomes one JSON log line. A `condition_change` line
is sent when a device/code condition changes to active, clear or unknown,
including E7001 silence and recovery. `loki_conditions` stores the last state
per device/code, so persistent faults do not repeat on deep-sleep wakes. On
first export, and after that export state is lost, unseen state is treated as
unknown: current active/clear conditions are emitted as a **baseline**, including
E7001 clear for a device with recent contact. An active/clear line alone does
not prove a transition happened at its export time. These
condition changes are independent of `NOTIFY_ENABLED`. Each successful ntfy
alert, reminder or resolution remains a separate `alert_decision` line; it
records only the catalog title/symbol/severity, code, validated device ID and
decision, and exists only when ntfy accepted the notification. Event lines use
the current catalog fields and only context fields allowed for that event's
code; they never carry free text, coordinates, SSIDs or credentials. Loki
streams have exactly three labels: fixed `job=mileage-tracker`, validated
`device_id`, and current catalog `severity`. All other fields stay in the JSON
line to keep label cardinality low.

`loki_cursor` keeps event rowid and delivered-decision progress independently
from GELF. The condition and retry state are also stored in additive tables
in monitor schema v1. The first enable has no age filter: each tick sends up to
200 total condition changes, events and delivered decisions. Entries keep their
real timestamps, sorted within each label set (Loki 2.4+ accepts out-of-order
writes inside its window). While a full 200-event batch is still draining, only
events are sent, so the backlog stays oldest-first; condition changes and
decisions follow once it catches up. History Loki rejects as too old is skipped.

The monitor uses one HTTP push with a ten-second timeout and never follows
redirects. Network failures, HTTP 408, HTTP 429 and 5xx responses back off
from 60 seconds to one hour without advancing the batch. A permanent HTTP 4xx
other than 408 or 429 logs only the status code, skips the rejected batch and
advances its cursors; that history is not retried, and later rows continue.
Successful pushes advance the batch; an ambiguous response can be retried and
duplicated. If `events.db` has a lower maximum rowid or a different event
fingerprint at Loki's cursor, only that sink's event cursor resets and history
is sent again, even when new uploads refilled the old rowids between ticks.
Its marker is independent of GELF in `event_cursor_markers`; upgrading an old
nonzero cursor without a marker replays once. Loki runs
after ntfy and GELF; its failures are caught separately and do not change
either sink's progress or trip uploads.

### Backups and acceptance (#129/#130)

`server/backup/backup.sh` ships consistent SQLite backup-API snapshots of
viewer annotations, ingest `events.db`, and optional `monitor.db` when its
volume exists. Viewer helpers read annotations/diagnostics; the monitor helper
reads its own volume. Missing diagnostics/monitor databases are normal and
recorded in `INFO`. Monitor-volume discovery errors warn and record
`monitor=unknown`; trips, annotations and diagnostics still back up, without
running a helper against an absent/unknown monitor volume.

Trip data is rsynced under ingest's upload lock; the live diagnostics database
and its SQLite sidecars are excluded. Checked database snapshots sit beside
`annotations.db` and are covered by `SHA256SUMS`. Each database is consistent
individually, **not one transaction across services**. Never substitute a live
database-file copy for a consistent snapshot. Restrictive permissions remain
required; the shipped scripts use `umask 077` and validate database schemas.
Preserve catalog-version compatibility when restoring.

`restore.sh` checks manifests and saved databases, stops ingest/viewer and any
discovered monitor writer, saves safety snapshots of the current databases
before replacing any, and restores with the owning service's helpers. It then
restarts the default services and a monitor only if previously running. Unknown
monitor discovery skips monitor replacement and monitor service actions; manual
recovery is in [DEPLOY_DEBIAN.md Step 10](DEPLOY_DEBIAN.md#step-10--backups).
Older backups without the new snapshots leave those live databases unchanged;
an old live `data/diagnostics/events.db` or sidecar is never replayed by rsync.

Restored monitor state can replay notifications or exports; restored diagnostics
state can lower server high-water revisions. Use explicit revision recovery
when needed, without inventing liveness or clearing current faults. Optional
ntfy cache/auth state needs its own backup; never store ntfy tokens in the public
repository or ordinary journal snapshots. Restore tests and recovery commands
are in the deployment runbook.

Before deployment, #130 must exercise real fault/recovery episodes, all three
storage levels, corrupted/torn frames, segment quota exhaustion, lost responses,
duplicate/conflicting IDs, current snapshots accompanying old boots, catalog
upgrade mismatch, RTC invalidity and monitor restarts. Confirm the display
actually fits and blanks under motion/unknown motion, and that trips win over a
large diagnostic backlog. Keep synthetic fixtures and bench results separate
from evidence of real road or charging performance.
