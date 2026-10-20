# Device diagnostics framework

This is the reference for issues #117 through #130. It describes the complete
planned framework, including features that are not implemented yet. **#117 adds
the shared catalog, protocol validation, generated C++ header, tests and this
document; #118 adds the ingest endpoint, storage and GPX contact refresh;
#119 adds the viewer's Device health page; #120/#121 add the optional server
monitor, ntfy deployment and silence policy.**
Firmware diagnostics follow in later issues.
Existing firmware and server behavior remains described in
[README.md](README.md) and [DEPLOY_DEBIAN.md](DEPLOY_DEBIAN.md).

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

## Delivery map and prerequisites

- **#117:** catalog, protocol v1 validation, generator, host compile test, CI
  drift check, and this reference.
- **#118:** authenticated ingest endpoint, SQLite storage, GPX contact updates,
  snapshot ordering, and packaging the shared Python code into both services.
- **#119:** shipped Device health at `/device-health`, with current conditions
  and paginated history behind the viewer login.
- **#120:** shipped independent monitor service and optional ntfy fault notifications.
- **#121:** shipped server-side device-silence detection; heartbeat integration is #125.
- **#122:** implemented board-independent EventLog and host fault tests: bounded
  producer queue, CRC segments, durable acknowledgments/loss accounting and
  snapshot revision allocator. Part A of #124 now supplies its mounted-storage
  adapters, boot identity/counter and boot notices in Stage 4.
- **#123:** pure DisplayModel and host tests for display policy.
- **#124 part A:** implemented mounted-filesystem adapters, fresh boot identity/
  NVS boot counter, cooperative core-0 service, LittleFS trip reserve, shutdown
  sealing and boot/reset notices. **Part B is implemented:** runtime fault hooks
  and their host transition/protocol tests (see the hook list below).
  Supreme storage timing, power-cut and road acceptance remain hardware work.
- **#125:** implemented core-0 diagnostics upload worker, current snapshots,
  explicit revision enrollment/recovery, complete ack verification, bounded
  rejection isolation, trip priority and independent retries. SD revision
  enrollment is required before the first diagnostic request (see below).
- **#126:** degraded service loop, **after Supreme port #107/#109**. Replaces
  permanent halts on storage/PMU failure with bounded reporting and retries.
- **#127:** SH1106 hardware adapter on the Supreme, following #123 and the port.
- **#128:** implemented diagnostics-only PCF8563 boot timestamps, bounded
  GPS/SNTP RTC setting and clock-confidence/runtime tests; retention needs bench testing.
- **#129:** consistent diagnostics database and monitor-state backups/restores.
- **#130:** induced-fault, power-cut, backlog and display shakedown on hardware.
- **#134:** Prometheus `/metrics` for the owner's external Grafana (not bundled).
- **#135:** shipped GELF export to the owner's external Graylog (not bundled).
- **#136:** optional push to an external Loki (not bundled).
- **#137:** installer questions choosing these outputs.
- **#151:** E2007 catalog entry, retained Stage 4 PMU startup result and boot
  journal emission after successful startup are implemented. The failure halt
  remains until #126, and OLED reporting remains with #127.
- **#164:** E3009 antenna warning uses MON-RF gain and fresh GSV strength in
  every build; the RF serial line and temporary OLED page stay debug-only.

Host tests prove catalog consistency and policy behavior. They do not prove bus
wiring, power rails, charging safety, FAT power-cut recovery or road behavior.

## Catalog and on-screen codes (#117)

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
  home must not flood the journal. The catalog still supplies its display text.

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
Failed startup still halts before storage, so reporting that path also needs
#126's degraded loop. OLED failure reporting remains out of scope until #127.

`notify` means eligible, not an instruction to send a notification immediately.
Info codes, 2007 PMU_STARTUP_RECOVERY, the high/low charge-guard warnings
(2004/2005) and 3002 GPS_NO_FIX
(normal in a garage or parking structure) are ineligible by
default. The unreadable-temperature error 2006 is eligible. Error reset notices
remain history; #120's fresh-current-fault rule does not alert on historical
notices by themselves. Silence code 7001 is derived by the server monitor;
the protocol rejects 7xxx codes from devices so one cannot forge it.

OLED text is printable ASCII, at most **three lines of 21 characters**. The
SH1106 adapter uses a fixed 6x8-pixel font at scale 1 on a 128x64 screen: 21
characters occupy 126 pixels. An `E4005` heading uses one additional 8-pixel
line, leaving room for margins. The adapter must select that font explicitly;
larger or proportional fonts do not satisfy these bounds. Never scroll or
shrink text to work around a catalog violation. The C++ table includes code,
symbol, severity, OLED lines, title, notify and journal policy; longer recovery
and context definitions stay in the JSON for the server.

### Updating the catalog

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

## Protocol v1 (#117 contract, #118 and #125 integration)

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

### Identifiers, limits and record meaning

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

### Snapshots and reboot ordering

The required check-in fields are `boot_id`, `status_sequence`, `uptime_ms`, and
`active`. Each active entry has only `code` and `episode_id`; codes are unique.
Only continuing conditions can be active. Optional fields are `firmware_version`,
`boot_counter`, `storage_backend` (`sd|littlefs|ram|none`), `pending_events`,
`dropped_events`, `counters`, and `observed`. Counts are nonnegative integers.
`counters` maps decimal catalog counter codes (currently only `"5001"`) to
boot-local totals, with at most 16 entries. They are not independent events.

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

Persisting the revision once per new check-in has an NVS wear and recovery cost;
do not write it for every scan or event. EventLog's allocator uses one durable
80-byte SD metadata slot write per newly allocated revision, with no allocation
on scans/events. This is an operation count verified by host tests, not an NVS
wear measurement. The #125 worker defines its check-in rate and explicit
recovery policy below; any future NVS adapter needs its own wear review.
After allocator rollback/failure the
device may still upload events and GPX, but snapshots cannot advance until the
revision exceeds the server high-water mark. Recovery must explicitly restore
that allocator or re-enroll identity; never silently reset the server high-water
mark. This is a required integration review point, not functionality in #117.

On a newer boot's accepted snapshot, old-boot conditions become unknown unless
the new boot observes them. Do not generate a recovery notification merely from
a reboot. Old events remain history. #118 must apply these rules in the same
transaction as the batch; the stateless validator cannot enforce ordering.

### Acknowledgments and retries (#118/#125)

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

## On-device journal and degraded operation (#122/#124/#126)

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

### EventLog module (#122 implementation)

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
  before tasks start. The caller supplies a new random boot ID on every wake;
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
  one coalesced E4009 notice. Storage errors increment a status counter without
  calling producers or a logging sink. Known drop totals survive in metadata
  once a backend is writable; RAM-only records/counts are lost on reset.
- `nextBatch` exports up to 64 unchanged protocol-v1 JSON events and their
  IDs, oldest segment first on SD, then LittleFS, then RAM. Cross-backend order
  is not event-time order. The default 30 KiB events-array budget leaves room
  for the current check-in; the worker also enforces the full 32 KiB request.
  Re-reads preserve exact JSON, including original timestamps and identity.
  On the Windows host ABI, `sizeof(EventLog)` is **25,120 bytes** and
  `sizeof(EventLog::Batch)` is **35,288 bytes**; the host test prints both.
  ABI/alignment can differ on the ESP32-S3. #124 must place both objects in
  **static DRAM or PSRAM**, never on a task stack. Only one batch ticket is
  outstanding; a new read replaces it.
- `acknowledge(batch, accepted_ids)` requires the exact complete ID set, with
  no missing, unexpected or duplicate IDs. Persistent data advances only after
  the new metadata slot is durably written and read back. A failed ack retains
  retryability; RAM acknowledgments remove only matching queue heads. #125
  still owns parsing/validating the HTTP acknowledgment and E5010 policy.

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
endian. Offsets: magic `ELG1` at 0; generation (u64) at 8; ack segment/byte
offset (u32 each) at 16/20; next segment (u32) at 24; snapshot revision (u64)
at 32; cumulative drops/pending loss notice count (u64 each) at 40/48; explicit
quota-discard-through segment (u32) at 56; revision-enrolled flag at 60; reserved
bytes zero; CRC32 over bytes 0-75 at 76. Recovery selects the highest valid
generation with matching slot parity. A torn newest slot falls back to the
older generation. If neither validates, no records are considered acknowledged
and revision allocation is disabled. Filename allocation also scans existing
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

`restoreStatusSequence(highWater)` explicitly enrolls/restores the **SD-only**
revision authority after comparison with the server's high-water mark.
`allocateStatusSequence` persists the increment before returning it. It refuses
uninitialized/corrupt metadata, exhaustion or unavailable SD, even when event
journaling succeeds on flash/RAM. Never switch revision authorities during
fallback or silently reset the server mark. One allocation is one 80-byte slot
write; segment reservations, ack commits and loss bookkeeping also write slots.
Actual FAT power-cut durability and flash wear still require integration and
hardware testing.

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
#126 wires bounded degraded startup/retry behavior. Uploads and snapshots now
run after explicit enrollment; the degraded startup loop remains future work.

### Stage 4 boot and shutdown plumbing (#124 part A)

The boot-time trip-store selection is unchanged: SD when mounted, otherwise
LittleFS, with no destination changes during a trip. Diagnostics try that boot's
mounted SD first, then mounted LittleFS, then the existing 16-record RAM queue.
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
no E2007. Failed PMU/storage startup still halts before journal initialization;
reporting those paths needs #126. Debug builds print one journal boot line with
the active backend and physical unacknowledged valid-frame/segment counts;
uncertain inventory is labeled incomplete. Ambiguous writes can retain duplicate
IDs, so physical frame counts can exceed distinct event counts.

Before PMU shutdown, core 1 ends the trip and stops networking, then asks the
core-0 consumer to drain the bounded queue and seal open segments. The consumer
closes each write before returning. Core 1 waits at most 15 seconds for this
handoff, reports incomplete drain/sealing when necessary, and takes FS_LOCK
through both unmounts and power-off. No diagnostic I/O can reach unmounted media.
Cancelled/failed shutdown remounts the same stores and invalidates journal
caches while retaining the boot identity and RAM queue. RAM-only history is lost
on reset/power-off. Hardware tests must measure individual VFS operation latency
and trip-write delay, plus induced failures during sealing and remount.

### Stage 4 runtime fault hooks (#124 part B)

`runtime_faults.h` holds fixed condition slots and one static event builder per
owning task. Core 1 owns GPS, power, trip storage/recovery and display hooks;
core 0 owns WiFi/HTTP/stack and the AP-absence counter. The builders only call
EventLog's bounded, nonblocking `submit`, never filesystem APIs or ISRs. Journal
adapter/service errors do not enter these hooks; E4008/E4009 remain EventLog's
own policy. The trip/FS operations being measured keep their existing locks.

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
| E2002 | AXP2101 level-1 IRQ or battery percentage <= configured low warning | Valid connected battery percentage above that threshold | `battery_mv`, if in catalog range |
| E2003 | AXP2101 level-2 IRQ (before existing shutdown), or percentage <= configured critical level | Valid connected battery percentage above critical | `battery_mv`, if in range |
| E2004 | Existing charge guard pauses hot (die >=54 C); remains hot while paused above 47 C | Finite die sample <=47 C or another measured pause reason | `temperature_mc` = measured PMU die, if in range |
| E2005 | Guard pauses cold (die <=9 C); remains cold while paused below 14 C | Finite die sample >=14 C or another measured pause reason | `temperature_mc` = measured PMU die, if in range |
| E2006 | Non-finite charge-temperature sample | Finite sample; hot/cold conditions are not cleared by an unreadable sample | none |
| E3001 | No checksum-valid NMEA for 10 s, starting at GPS configuration start | Next checksum-valid NMEA; noise/UBX/bad checksum do not recover it | `elapsed_ms` |
| E3002 | No trusted fix (one meeting the NAV-PVT start accuracy limits, or any valid fix in RMC-only fallback) for 120 s while idle | Trusted fix; acquisition grace starts again after leaving a trip | `elapsed_ms`, `satellites` when measured |
| E3003 | Matching checksum-checked VALSET NAK | One notice per rejected attempt, no active condition | `message_class=6`, `message_id=138` |
| E3004 | Final config readiness/ACK timeout after five attempts | One give-up notice; final NAK uses only E3003 | `message_class=6`, `message_id=138` |
| E3005 | Active/cooldown trip has no valid RMC fix for 30 s; any valid RMC counts, trusted or not, so weak accuracy while parked in cooldown does not cause an alert | Next valid RMC fix, even after the five-minute finalize; ending the trip alone is not recovery | `elapsed_ms` |
| E3006 | Trusted system clock `gClockSet` still false after 120 s | Successful GPS/SNTP clock sync; untimed GPX points retain their original status | none |
| E3007 | RTC has no ACK, or its initial boot register read fails | Verified RTC setting after trusted GPS/SNTP time; at most three attempts per boot, at least a minute apart after failures | none |
| E3008 | Readable RTC has STOP/VL, invalid BCD/calendar, unsupported year, or a later read/write/verification failure | Verified RTC setting from GPS/SNTP clears the condition; normal transition/repeat policy | `reason=stopped\|invalid_date\|out_of_range\|read_failed` on failure |
| E3009 | At 30 s MON-RF boundaries, valid AGC >=70% and fresh GSV (a complete group within 15 s) with zero satellites >=30 dBHz, continuously for >=120 s from the first suspect sample | One valid AGC <=50% sample **or** fresh GSV with at least one satellite >=30 dBHz; missing/stale/intermediate evidence resets the hold and retains an active warning | `agc_pct` (0-100) when MON-RF valid; `max_cno` (0-99) when GSV fresh |
| E4001 | SD did not mount, trip logging uses LittleFS | Observed SD mount; current boot does not hot-switch backends | none |
| E4004 | Guarded trip free space <64 KiB start reserve | >=64 KiB | `backend`, `free_bytes` |
| E4005 | Guarded trip free space <24 KiB stop reserve | >=24 KiB | `backend`, `free_bytes` |
| E4006 | Trip create/open, point commit or finalization fails | Next verified trip create/write/finalize or successful repair | `backend`, `operation=open\|write\|close` |
| E4007 | Boot fsck directory/read, ambiguous repair, write or publish fails; also runtime repair failure | Per-occurrence notice; no clear | `backend`, `reason=read\|parse\|write\|rename` |
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
| E6001 | Debug OLED adapter latches I2C/init failure | Adapter healthy; current adapter disables display for the rest of the boot | none; DISPLAY_DEBUG=1 only |

Battery diagnostics use the **existing hardware SOC thresholds**, without adding
a new voltage cutoff or changing shutdown policy. XPowersLib 0.3.3 returns the
raw level-1 nibble, so the configured warning percentage is that value plus five;
level 2 returns the configured percentage directly. Connected-battery sampling
runs every 30 s, with immediate IRQ observations. The catalog's voltage wording
is generic; `battery_mv` is a measurement, not the threshold used by this PMU.
The charge guard uses its existing die proxy and hysteresis; diagnostic context
never presents that die reading as a measured cell temperature.

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
below 24 KiB a running trip is finalized. These hooks do not read or modify
EventLog's own storage failure path. Arduino's void close cannot independently
report close errors; E4006 `close` means the verified finalization commit failed.
HTTP error classes stay active across different errors and clear only on 200.
Existing quarantine/retry behavior is unchanged; intentional network aborts do
not generate transport/join faults.

Host tests exercise each hook's transitions and context through production paths,
the notice policies, checksum/readiness deadlines, charge hysteresis, queue-full
retry/episode handling, rollover, the counter without scan events and a normal
real-parser drive/upload with only E1000. Both DISPLAY_DEBUG modes run, and actual
exports pass the server protocol validator. They cannot measure the network task's
watermark or storage latency. The 12 KiB allocation is unchanged; the 2 KiB warning
margin follows #6. Static builders avoid event-sized stack frames; compiler stack
reports and the build sizes supplement, but do not replace, the real-upload bench
measurement. Part A's observed 7,556 free bytes was a no-upload measurement.

Startup halts E1001/E1002/E2001/E4002/E4003 remain #126; RTC E3007/E3008 are implemented by #128;
E5010 and diagnostic uploads/snapshots are implemented by #125. Error-code OLED
pages are #127; antenna detection is implemented by #164. All requested meaningful Stage 4 runtime
codes above are wired, including E3006's existing trusted-clock state.

#126 keeps a bounded service loop alive if PMU/storage initialization fails: show or
upload failures when possible and retry safely, without claiming trips are
recorded when no backend works. Unknown PMU state must not enable unsafe charge
or rail operations. Display failure never changes the logging lifecycle.

## Trip-first upload scheduling (#125, implemented)

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
a snapshot of this boot. The snapshot includes boot identity/counter, extended
awake uptime, active episodes, observed codes, backend, dropped count and the
boot-local E5001 counter. Unobserved conditions stay unknown. Duplicate network
codes across trip/events targets merge into one active condition when either
target is unhealthy; clearing one target does not clear the other. The optional
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
Each allocation is one 80-byte SD metadata slot write/read-back, in addition to
normal journal ack/segment/loss metadata. This rate still needs FAT wear and
power-cut acceptance; it is not an NVS endurance claim.

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

### Acknowledgments, rejection and independent backoff

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
A rejected probe retains all records; a snapshot 409 suspends SD revision
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

### Snapshot revision enrollment and recovery

The existing EventLog revision authority is **SD only**. There is no HTTP
high-water discovery endpoint. Before first use, an operator must inspect
`devices.status_sequence` in the ingest diagnostics database for the exact
`DEVICE_ID` (a missing row/null revision means zero). In private `secrets.h`,
set `DIAGNOSTICS_STATUS_HIGH_WATER` to that value and choose a positive
`DIAGNOSTICS_ENROLLMENT_ID` greater than any used on this board. The commented
example shows `0ULL`/`1ULL` for a verified fresh identity only.

Core 0 consumes and verifies the enrollment ID in `diag/enroll` NVS **before**
calling `restoreStatusSequence(highWater)` under the journal mutex. This writes
NVS once per explicit enrollment, not once per check-in. Normal boots allocate
from the existing SD metadata even if those defines are removed. A failed
attempt consumes the token: retry with a higher ID after checking the server
and storage. Reusing firmware after metadata loss cannot silently enroll at
zero. Never reset the server high-water or assume a replacement SD is new
identity enrollment. After a confirmed snapshot conflict, restore with a new
enrollment ID and verified high-water; reboot to clear the in-memory block.

With SD absent, unenrolled, corrupt or unable to persist a revision, diagnostic
requests remain suspended and back off; GPX remains available. Protocol v1
requires a check-in, so an events-only request cannot bypass this constraint.
LittleFS/RAM journaling still works within its existing durability limits. A
future alternate durable revision authority needs explicit migration design;
the worker never switches authorities automatically.

### Opportunistic heartbeat and memory

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
builds print free internal heap, its largest free block and free PSRAM at boot
and once after the first diagnostics HTTP attempt (including failure). These
are measurement hooks for #6; actual real-upload heap/stack measurements remain
open, including comparison with the previously observed **7468 free bytes with
no upload**. No host test establishes ESP32 heap/stack headroom, SD latency or
power-cut safety.

Supreme/core 3.3.11 compile results with PSRAM enabled: static RAM falls from
193,804 to **108,356 bytes** with `DISPLAY_DEBUG=1`, and from 169,836 to
**84,388 bytes** with `DISPLAY_DEBUG=0` (85,448 bytes recovered in each build).
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

## Display policy and Supreme hardware (#123/#127/#128/#130)

DisplayModel is pure C++ policy; the SH1106 adapter alone touches hardware.
**The screen design is provisional** (owner, 2026-10-16): it will get more
thought once the board is in the car. For now there are two modes, chosen by
a firmware build toggle, `DISPLAY_DEBUG` in the sketch (on for the first
builds):

- **Debug on:** a status page for bench and shakedown work: FSM state,
  satellites heard and used, GPS AGC, fix/HDOP, storage backend and free space,
  pending trips/events, WiFi/upload result, battery. It is the only mode that
  may stay lit, so it is for development, not everyday driving.
- **Debug off:** minimal: the number of satellites in view, plus the error
  policy below. Whether that count stays lit or blanks is part of the future
  screen design; OLEDs burn in, so a static always-on display needs a
  dim/shift/timeout rule before it ships.

The error policy applies in both modes. While parked, briefly show boot, trip and upload status. While moving **or when
motion is unknown**, suppress routine messages. Show each new ERROR/CRITICAL
episode once at low brightness for about 10 seconds, then blank. No flashing or
scrolling. Repeats do not keep it lit. Remind about unresolved conditions at the
next parked opportunity/boot, and turn the display off before deep sleep. A
failed adapter reports 6001 without blocking recording or retrying in a tight
loop. The screen is a brief status aid, not a substitute for the server history.

The following are **Supreme port targets to verify against the board revision
and on the bench**, not pin changes for the existing T-Beam V1.2 sketch:

- SH1106 OLED, 128x64, on Wire SDA17/SCL18, address 0x3C (check 0x3D if absent).
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
settings. Retention across `PMU.shutdown()` remains unverified on hardware.
Never rewrite older journal records' timestamps after synchronizing;
uptime and receipt time preserve their original meaning. #130 validates these
behaviors plus degraded storage, reset reporting, motion suppression, deep
sleep, upload interruption, backlog drain and notification recovery on hardware.

## Server storage and Device health (#118/#119)

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

### Device health page (#119, shipped)

Open `/device-health` from the viewer navigation after logging in. It shows
each device's last server contact and age, latest snapshot receipt and age,
firmware, storage backend, pending/dropped events, boot-local counters, uptime
and status revision. Contact alone does not establish health or refresh a
snapshot's age. No fixed freshness threshold is imposed here; silence policy
belongs to #121.

Conditions are grouped active / unknown / clear from `device_conditions`.
Never-observed catalog conditions with `journal=transitions` also appear as
unknown (server-raised 7xxx codes excluded), folded into a collapsed section
because it is usually most of the catalog. Each known code has its current
catalog severity, title, OLED text,
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

## Optional monitor and notifications (#120/#121, shipped)

The stdlib-only `mileage-monitor` service optionally publishes metrics on port
8093; the endpoint is disabled unless configured. Every 60 seconds
it reads `/data:ro` and stores its own delivery/deduplication state in a separate
writable volume. It watches **every device that has ever checked in**; there is
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

### Shipped service and state policy

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
heartbeat integration is implemented by #125, and consistent state backups remain #129.
Installer test uploads omit `X-Device-Id` so a routine `--check` does not enroll
an ever-watched synthetic device; their unique `X-Trip-Id` still owns cleanup.

### Prometheus metrics (#134)

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

### Graylog export (#135, shipped)

Owner decision (2026-10-16): Graylog runs on a separate server the owner
manages; this repo never bundles it and only sends to it. Setup steps and
starter Event Definitions are in
[DEPLOY_DEBIAN.md Step 11d](DEPLOY_DEBIAN.md#11d--optional-send-diagnostics-to-your-own-graylog).
The code is `server/mileage-monitor/gelf.py`, called from each monitor tick
after the ntfy step; any Graylog failure is caught there and never affects ntfy.

**Why facts, not ntfy decisions.** ntfy's decisions are phone policy: the
hold, the hourly cap and digests, dropping a not-yet-sent alert when its fault
clears. Graylog has its own alerting (Event Definitions, grace periods), so
forwarding pre-throttled decisions would apply that policy twice, and with
ntfy off it would forward nothing. GELF therefore carries two kinds of facts,
independent of `NOTIFY_ENABLED` and `NOTIFY_*` settings (only
`MONITOR_STALE_DAYS` and the 24-hour snapshot freshness apply):

- `event`: every row in `events.db`, in rowid order, each forwarded once. The
  cursor is the last delivered rowid (`gelf_cursor`). The ingest never deletes
  events, so rowids only grow; if the table's highest rowid is ever below the
  cursor (events.db replaced by a restore), the cursor resets to 0 and history
  is resent, so Graylog may see duplicates but never silently misses events.
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
states and clear events. Only catalog fields, validated identifiers, bounded
device-clock fields and catalog context (re-checked: short tokens, integers,
booleans as `"true"`/`"false"`) are sent; the source is the fixed
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

### Loki push (#136)

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
per device/code, so persistent faults do not repeat on deep-sleep wakes. These
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
duplicated. If `events.db` is restored with a lower rowid than Loki's cursor,
only the Loki event cursor resets and that history is sent again. Loki runs
after ntfy and GELF; its failures are caught separately and do not change
either sink's progress or trip uploads.

## Backups and acceptance (#129/#130)

Use SQLite's backup API (or a coordinated service pause) to take consistent
copies of events.db and the monitor's own SQLite state. `backup.sh` currently
rsyncs `/data`, which now includes `/data/diagnostics/events.db`; that live-file
copy is not a guaranteed consistent SQLite snapshot. Implementing and testing
the consistent snapshot is **#129's job**, still required before relying on
diagnostics backups. Do not blindly copy a live database file and assume it is
consistent. Preserve restrictive file
permissions and include schema/catalog versions in restore validation. Restored
monitor state can replay notifications; restored diagnostics state can lower
server high-water revisions. #129 must test those cases and document recovery
without inventing liveness or clearing current faults. Never store ntfy tokens
in the public repository or ordinary journal snapshots.

Before deployment, #130 must exercise real fault/recovery episodes, all three
storage levels, corrupted/torn frames, segment quota exhaustion, lost responses,
duplicate/conflicting IDs, current snapshots accompanying old boots, catalog
upgrade mismatch, RTC invalidity and monitor restarts. Confirm the display
actually fits and blanks under motion/unknown motion, and that trips win over a
large diagnostic backlog. Keep synthetic fixtures and bench results separate
from evidence of real road or charging performance.
