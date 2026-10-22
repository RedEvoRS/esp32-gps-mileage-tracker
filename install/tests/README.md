These tests use synthetic firmware and never open a serial device.

From the repo root:

```sh
# Existing tools CI discovery includes the bash tests on Linux with bash + jq.
python -m unittest discover -s tools -p test_flash_scripts.py -v
```

On Windows, run the dependency-free PowerShell tests under both shells:

```powershell
powershell.exe -ExecutionPolicy Bypass -File install/tests/flash.Tests.ps1
pwsh -File install/tests/flash.Tests.ps1
```

The bash tests require `jq`; they explicitly skip when bash/jq is unavailable
(including Windows, where the PowerShell suite is used). The existing Linux
tools CI runner provides jq. The separate two-piece installer CI can run the
same commands; this change does not edit its workflow.

For a manual dry run with the real checksum-verified standalone esptool, supply
a folder produced by the builder, `--path` / `-Path`, `--dry-run` / `-DryRun`,
and a placeholder `--port /dev/test` / `-Port COMx`. Add `--first-install` /
`-FirstInstall` to inspect backup, full erase and four-part commands; use a normal-kind
folder with `--cleanup` / `-Cleanup` to inspect app-only cleanup. Dry run skips
all serial commands, including chip identification, while checking the host
executable version. Real operation confirms the ESP32-S3 with chip identification
before reading or writing flash.

Flash safety fences reflect the checked-in Supreme `partitions.csv`: NVS starts
at `0x9000` and LittleFS at `0x190000`. A future partition-layout change must
review these fences along with the builder; offsets used in commands always
come from the manifest. The scripts never use a padded merged image.

First install performs two full 8 MB reads and verifies their size and contents,
requires the exact case-sensitive answer `ERASE`, erases the whole internal flash,
then writes all four parts at manifest offsets. This is required to provision
LittleFS over the factory firmware, as explained in `firmware/README.md`.
It wipes internal settings (including trip counter and diagnostics enrollment)
and internal trips. SD trips are untouched. Use the default update for a tracker
already running this project. `-FirstInstall -SkipBackup` / `--first-install
--skip-backup` is only for re-provisioning a board already backed up, and still
requires `ERASE`. Dry run prints the sequence and confirmation requirement without
prompting or accessing a device. Update and cleanup never erase. First
installs select manual BOOT/RESET download mode before chip confirmation;
updates and cleanup first try automatic USB reset, falling back to manual
instructions and a port correction prompt after a nonzero chip-ID exit.
The fallback retries once with no reset; a failed retry prevents the write.

The tests exercise the real control flow with fake esptool executables: v4/v5
erase spelling and backup/erase/write order; refusal of wrong confirmation;
read failure, short backup, mismatched reads and erase failure; explicit backup
skip; app-only update/cleanup with automatic reset success, fallback and
failure; safe SSH target validation; and development/release banners.
PowerShell also checks Windows OpenSSH selection and PATH fallback.
Bash uses a private pseudo-terminal for the
`/dev/tty` prompts. PowerShell supplies queued `Read-Host` replies. The real
pinned standalone tool is used separately only for dry runs. Its version is
pinned independently of the tracker release.

All fixtures and synthetic backup files are removed by test cleanup, including
assertion failures. Forced termination can leave `flash-tests-*` under the OS
temporary directory; the script copies are `tracker-flash-*` under `$env:TEMP`
(Windows) or `tracker-firmware.*` under `${TMPDIR:-/tmp}` (bash). Download folders
are `tracker-flash-*` under the Windows user profile or `esptool.*` under
`~/.cache/mileage-tracker`. Factory backups are deliberately retained in the
chosen backup directory. Report exact leftover paths after an interrupted run.
