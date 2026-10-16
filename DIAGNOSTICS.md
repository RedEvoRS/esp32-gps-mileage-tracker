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
- **#122:** host-testable EventLog, bounded queues, segmented persistence and
  acknowledgment recovery. Also owns durable identity/revision allocation.
- **#123:** pure DisplayModel and host tests for display policy.
- **#124:** firmware fault/reset hooks, **after Supreme port #107/#109**.
- **#125:** diagnostics upload worker, trip priority and independent retries.
- **#126:** degraded service loop, **after Supreme port #107/#109**. Replaces
  permanent halts on storage/PMU failure with bounded reporting and retries.
- **#127:** SH1106 hardware adapter on the Supreme, following #123 and the port.
- **#128:** PCF8563 clock integration on the Supreme and time-confidence tests.
- **#129:** consistent diagnostics database and monitor-state backups/restores.
- **#130:** induced-fault, power-cut, backlog and display shakedown on hardware.
- **#134:** Prometheus `/metrics` for the owner's external Grafana (not bundled).
- **#135:** shipped GELF export to the owner's external Graylog (not bundled).
- **#136:** optional push to an external Loki (not bundled).
- **#137:** installer questions choosing these outputs.

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

`notify` means eligible, not an instruction to send a notification immediately.
Info codes, the high/low charge-guard warnings (2004/2005) and 3002 GPS_NO_FIX
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
`DEVICE_ID` in `secrets.h` and sends it on GPX uploads; diagnostics uploads follow
in #125. The public example is a non-personal identifier such
as `tracker-1`. Header identity is validated separately and is not repeated in
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
do not write it for every scan or event. #122 must measure the write rate and
recover the allocator before #125 relies on it. After NVS rollback/failure the
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

After the Supreme port, #124 wires PMU, charge guard, GPS/config/time, storage,
trip recovery, networking, display and reset paths to these codes. Thresholds
belong with the actual subsystem policy, not hard-coded catalog text. #126
keeps a bounded service loop alive if PMU/storage initialization fails: show or
upload failures when possible and retry safely, without claiming trips are
recorded when no backend works. Unknown PMU state must not enable unsafe charge
or rail operations. Display failure never changes the logging lifecycle.

## Trip-first upload scheduling (#125)

After a trip upload batch, drain diagnostic batches of up to **64 records**
within the **32 KiB complete-request cap**. Continue until the queue is empty,
a trip becomes eligible, `gNetHold`/`gNetAbort` is set, or about **10 seconds
total** have elapsed. Check these stop conditions between batches and bound
connection/read timeouts by the remaining budget. A 10-second loop around a
long blocking socket call would not enforce this rule. Recheck trip eligibility
before each batch so a backlog cannot starve recording or trips. Send a current
snapshot alongside old journal records; their boot IDs need not match.

#125 adds a heartbeat if no contact has occurred for 24 hours, using an existing
awake/network opportunity. Do not wake a parked car solely for a heartbeat.
Being parked away from WiFi can legitimately trigger the optional silence alert.

## Display policy and Supreme hardware (#123/#127/#128/#130)

DisplayModel is pure C++ policy; the SH1106 adapter alone touches hardware.
**The screen design is provisional** (owner, 2026-10-16): it will get more
thought once the board is in the car. For now there are two modes, chosen by
a firmware build toggle, `DISPLAY_DEBUG` in the sketch (on for the first
builds):

- **Debug on:** a status page for bench and shakedown work: FSM state,
  satellites in view and used, fix/HDOP, storage backend and free space,
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

#128 validates the RTC stopped/invalid-time flags and date fields before use.
RTC time is lower-confidence holdover, not evidence of a recent GPS/SNTP sync.
When trusted time becomes available, set the RTC and track sync age where
known. Never rewrite older journal records' timestamps after synchronizing;
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

Use [DEPLOY_DEBIAN.md Step 11](DEPLOY_DEBIAN.md#step-11--optional-device-notifications)
for manual enablement. `monitor` and `ntfy` are independent Compose profiles;
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
heartbeat integration remains #125, and consistent state backups remain #129.
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
from GELF. The condition state, retry state and per-stream timestamp watermark
are also stored in additive tables in monitor schema v1. The first enable has
no age filter: each tick sends up to 200 total condition changes, events and
delivered decisions. Lines are sorted by timestamp within each label set; if
an event timestamp ties or predates one already sent for that stream, its Loki
entry timestamp is advanced by one nanosecond. The original server receipt time
remains in the JSON line.

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
