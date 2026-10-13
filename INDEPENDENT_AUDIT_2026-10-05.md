# Independent code audit — 2026-10-05

Original audit baseline: `7a9d787f2ae26b3c47dc847efa1fe07d4a1fe3dc`.
Updated against `main` at `444b2a537f3d479818540cad8f6dbebef8788131`,
which includes merged [PR #30](https://github.com/RedEvoRS/GPS-Milage-Tracker-without-a-Phone--ESP32-/pull/30).
This report accompanies proposed fixes on `codex/independent-audit`.
The pull request is for human review and must remain unmerged unless the
owner subsequently authorizes a merge.

## Approach and scope

Three independent passes examined the production firmware, ingest service,
and viewer. Each started with source and executable tests before consulting
the previous audit reports. The integration pass checked the boundary from
firmware acknowledgement to durable server storage, and from stored GPX to
authenticated viewing and exports. Existing audit documents were compared
afterwards: some findings below reconfirm issues that those reports left open.

Priority means urgency in this project's context: P1 is potential loss or
replacement of irreplaceable trip data; P2 is a functional or security defect
with a narrower trigger. This is a source review with targeted regression
testing, not a claim that every defect has been found.

## Integration with PR #30

Independent review of PR #30 found no new runtime regression in its changes.
Its 28 server tests passed before integration. The audit branch preserves
all 14 tests it added and retains these upstream fixes:

- XLSX literal-text export through `_xlsx_literal_text()`.
- UTF-8 authentication comparisons, including the viewer's `_safe_equal()`.
- CSV escaping for leading minus and carriage return.
- HTTP 400 rejection of GPX with no usable track points, before storage.

The audit adds nonempty CSRF validation around `_safe_equal()` because an
absent session token and absent form token would otherwise still compare
equal. It also retains the additional CSV newline guard, login-counter
serialization, waypoint protection, upload integrity, and firmware fixes.
Conflicts in both server `app.py` files were resolved without discarding
either PR's protections. Empty-track acceptance is no longer outstanding.

## Original findings and current resolution

| ID | Priority | Trigger and observed problem | Proposed change and evidence |
| --- | --- | --- | --- |
| F1 | P1 | `LittleFS.begin(true)` formats the trip partition after a mount failure. A recoverable mount problem can therefore erase all queued trips. | Mount without formatting, halt with a preservation message, and document explicit provisioning for new/empty devices. Fault injection checks that mount failure retains queued bytes. |
| F2 | P1 | Reset/stale NVS `tripno` selects an existing trip name; opening it with `FILE_WRITE` truncates an unuploaded trip. | Select a free name while holding the existing filesystem mutex, including names reserved by `.bad` and `.fix` evidence. Repeated counter resets no longer overwrite previous trips. |
| F3 | P1 | Crash recovery removes the original before an unchecked rename. A failed rename or power cut leaves no normal `.gpx` entry. | Checked replacement rename preserves the original on failure. Tests retain both copies on an injected rename error, then successfully repair on the next pass. |
| S1 | P1 | Reusing a trip ID with different GPX returns 200 and silently replaces the stored original. Independent baseline test replaced a two-point trip with three points. | Return 409 for conflicting bytes, raw IDs sharing a sanitized path, or device IDs. Identical retries succeed and retain receipt metadata. The existing month/path identity remains unchanged. |
| S2 | P1 | Two Gunicorn workers share the same `.tmp` names and can interleave GPX and summary writes. | A process-shared file lock spans conflict checks and both writes; temporary files have unique names. Four-process tests verify one consistent winner for conflicts and success for identical retries. |
| S3 | P1 | Upload returns 200 after close/rename without syncing storage; firmware then deletes its only local copy. | Flush and fsync file contents, and sync directory entries on POSIX before success. Injected sync failures return 500; a retry repairs a missing summary. Hardware/storage durability still requires deployment testing. |
| S4 | P2 | `NaN`, infinity and impossible coordinates contaminate summaries or fail calculations. A baseline NaN upload returned 200 with nonstandard NaN JSON. | Reject nonfinite/out-of-range coordinates and nonfinite/negative speed or HDOP; serialize JSON with `allow_nan=False`. Missing optional measurements retain fallback behavior. |
| S5 | P2 | Non-ASCII bearer tokens, usernames, or CSRF tokens raise `TypeError` in string `compare_digest`, producing 500 responses. | Fixed upstream by PR #30; retained unchanged in ingest and reused through `_safe_equal()` in the viewer. Both regression suites verify normal unauthorized/invalid-token responses. |
| S6 | P2 | Unicode IDs survive ingest sanitization but the viewer accepts only ASCII IDs, so the stored trip cannot be opened. | Match the viewer's filename allowlist; retain the original ID in the summary. |
| V1 | P2 | A new browser session can POST valid login credentials without a CSRF token because two empty strings compare equal. | Require nonempty matching session/form tokens for login and authenticated POSTs. Successful login, token rotation and logout remain covered. |
| V2 | P2 | SQLite failed-login accounting uses an unlocked read/modify/write. Concurrent failures lose increments; an already-running failure can clear a newly created lock. | Reserve the SQLite writer before reading, and preserve active locks. Forty concurrent baseline failures recorded only one in a reproduction; the fixed test records all forty. |
| V3 | P2 | XLSX notes and metadata beginning with `=` become formula cells. CSV protection also omits several dangerous prefixes. | XLSX and minus/CR CSV protection fixed upstream by PR #30 and retained. The audit adds the newline guard. Both suites inspect literal XLSX text, numeric cell types, and CSV fields. |
| V4 | P2 | GPX waypoint names/descriptions are concatenated into popup HTML by the active map plugin. An uploader holding the ingest token can supply markup viewed in an authenticated browser. | Configure the viewer to parse tracks and routes only; waypoints are unused for mileage. A Node regression executes the active integration and real plugin parsing branch, rejecting hostile popup HTML while retaining track parsing. The baseline fails this regression. |

The deployment notes also correct the claim that all `env_file` values are
passed through literally: it is single-quoting that protects the password
hash from Compose interpolation. The annotation backup recipe now uses
SQLite's backup API rather than copying a database while it may be changing.

## Validation

- The baseline's 14 existing server tests passed before changes.
- All 49 combined server tests pass locally on Python 3.12: existing distance
  tests, PR #30's 14 hardening tests, upload fault/concurrency tests, viewer
  security/export tests, and a full Flask-client round trip.
- Firmware filesystem/state-machine host tests pass 101 assertions across
  T0–T23. The new regressions fail 13 assertions against the original sketch.
- GPS tests pass 17 assertions across G1–G6 using real TinyGPSPlus 1.0.3.
- The actual Stage 4 sketch compiles with the installed ESP32 core 3.3.11,
  TinyGPSPlus 1.0.3 and XPowersLib 0.3.3, using an isolated copy with example
  credentials. Compiler output: 1,034,784 program bytes and 55,816 global
  variable bytes. Nothing was flashed.
- `node server/tests/test_viewer_map.js` passes. Leaflet rendering, XML DOM,
  network requests and track geometry are stubbed; the vendor waypoint
  parsing branch and viewer options are exercised directly.
- GitHub Actions runs the server tests on Linux and both host firmware
  suites, with the TinyGPSPlus test dependency pinned to its 1.0.3 commit.
- `git diff --check` is clean.

Server regression command:

```bash
python -m pip install -r server/mileage-ingest/requirements.txt -r server/mileage-viewer/requirements.txt
python -m unittest discover -s server/tests -v
node server/tests/test_viewer_map.js
```

Firmware host command (C++20 compiler and TinyGPSPlus required):

```bash
TINYGPS_DIR=/path/to/TinyGPSPlus/src bash firmware/host_test/run.sh
```

The integration test checks returned map HTML and GPX bytes; it does not
claim browser rendering, real WiFi upload, Excel execution, or hardware
power-loss behavior. PMU and WiFi in the host harness are stubs. No device
was flashed and no deployed service or real trip database was modified.

## Remaining limits and follow-up work

- **Hardware shakedown remains necessary:** interrupted flash writes,
  brownouts, sleep during network activity, full flash, GPS reacquisition,
  and the charge-temperature approximation need measurements on the board.
  A host test of temperature logic does not validate the relationship
  between PMU die temperature and battery-cell temperature in a parked car.
- **Recovery of existing orphan files:** the new rename ordering prevents
  the identified future loss window. It does not recover standalone `.fix`
  files previously stranded by an older firmware version. Preserve such
  files for a deliberate recovery operation.
- **Untimed uploads:** IDs derived from file content are stable, but files
  without any timestamp are still bucketed by receipt month. A retry across
  a month boundary can create a duplicate. PR #30's empty-track rejection
  is retained and covered by the combined tests.
- **Conflict handling:** a 409 preserves the server original and the
  firmware copy, but firmware will retry it until an operator resolves the
  conflicting identity. This PR does not introduce a multi-device key or
  rename/migrate existing records and annotations.
- **Distance/time model:** existing distance behavior is retained, including
  straight-line GPS-gap estimates. Clock resynchronization after deep sleep
  and complete tax-record/export requirements need separate review; this
  audit does not certify mileage accuracy or legal sufficiency.
- **Deployment boundary:** services remain HTTP and LAN-only by design.
  There was no live network-exposure assessment, Docker deployment test,
  browser penetration test, or comprehensive dependency-vulnerability scan.

## Supporting references

- [Python file synchronization](https://docs.python.org/3/library/os.html#os.fsync)
- [Docker Compose interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
- [OWASP spreadsheet formula injection](https://community.owasp.org/attacks/CSV_Injection)

Source-specific behavior and severity above come from this repository's
code and reproductions, rather than being inferred solely from these references.
