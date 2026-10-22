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
`-FirstInstall` to inspect backup and four-part commands; use a normal-kind
folder with `--cleanup` / `-Cleanup` to inspect app-only cleanup. Dry run skips
all serial commands, including chip identification, while checking the host
executable version. Real operation confirms the ESP32-S3 with chip identification
before reading or writing flash.

Flash safety fences reflect the checked-in Supreme `partitions.csv`: NVS starts
at `0x9000` and LittleFS at `0x190000`. A future partition-layout change must
review these fences along with the builder; offsets used in commands always
come from the manifest. The scripts never use a padded merged image or erase-all.

First installs intentionally follow the two-piece interface's no-erase-all rule.
The current firmware README separately calls for erasing factory LittleFS data
on first Stage 4 provisioning. Four-part flashing alone may leave that partition
unavailable; real-board acceptance and a coordinated provisioning decision are
still required. Do not resolve that by adding erase-all to these scripts.
