# Personal firmware builder

Run `firmware-builder/build.sh --install-dir ~/mileage-tracker` after piece 1
has installed the server and its `firmware-src/` release bundle. Requires
Docker, Bash and `flock` (util-linux); no host ESP32 toolchain. Host
`python3` is used when present; without it, the builder runs its Python steps
in a throwaway container from the ingest image (no network).
Run as a normal user with Docker access; root invocation is refused, except
on Unraid, which runs everything as root (see [On Unraid](#on-unraid)).
The Linux x86-64 CLI is checksum pinned to CI, so the image explicitly uses
`linux/amd64`. Other hosts require Docker's amd64 emulation.

The first run downloads about 1.8 GB of toolchain archives after confirmation
and keeps an image of about 2.7 GB (Linux amd64, Docker Desktop's containerd
image store). Allow at least 10 GiB free for the first build: all chip packages
are downloaded and extracted before pruning, and image export needs space too.
Core and library installation runs as an unprivileged image user; subsequent
invocations reuse Docker's toolchain layers. A BuildKit download cache preserves
archives across interrupted installations and is cleared after a successful
install. Compilation runs as the invoking user's UID/GID,
without networking, privileges or a writable container root filesystem.
Only its temporary filesystem and private output mount are writable. CLI's
runtime downloads/staging directory is redirected into that temporary filesystem;
its installed core/libraries remain read-only and compilation has no network.

The image supports the Supreme's ESP32-S3 build only. In the same `RUN` as core
installation, an allowlist keeps these complete, pinned Espressif tool packages:

- `esp-x32`: the Xtensa compiler, linker, archiver and size tool.
- `esp32s3-libs`: S3 headers, precompiled SDK libraries, linker scripts and
  bootloader ELF files (including the selected `qio_qspi` configuration).
- `esptool_py`: creates the bootloader and app binaries and the core's merged
  image; this is needed even though the container never flashes hardware.

This list comes from core 3.3.11's `platform.txt` and the pinned FQBN's expanded
compile properties: `build.tarch=xtensa`, `build.mcu=esp32s3`, `compiler.path`
selects `esp-x32`, `compiler.sdk.path` selects `esp32s3-libs`, and the image
recipes select `esptool_py`. Keeping whole packages preserves their internal
layout. An allowlist also removes newly added chip packages on future installs;
revalidate it with an offline Stage 4 compile whenever the pins/FQBN change.

Removed packages are `esp-rv32` (RISC-V compiler; Stage 4 has no ULP program),
`esp32-libs`, `esp32c3-libs`, `esp32c5-libs`, `esp32c6-libs`, `esp32h2-libs`,
`esp32p4-libs`, `esp32p4_es-libs`, `esp32s2-libs` (other chips), both
`xtensa-esp-elf-gdb` and `riscv32-esp-elf-gdb`, and `openocd-esp32` (interactive
debugging), `mkspiffs` and `mklittlefs` (filesystem-image creation; Stage 4 builds
no filesystem image), and Arduino's `dfu-util` (DFU flashing). The core and
Arduino's builtin tools remain: `ctags` is used for sketch preprocessing, and
CLI initializes discovery/monitor dependencies even for offline compilation.
No empty tool stubs or package-index edits are needed with CLI 1.5.1. Pruning
in a later layer would retain the original bytes and defeat the size saving.

The full/slim comparison used the same clean source archive, fake settings,
absolute build paths, partitions and CI compile flags in containers with no
network, a read-only root, UID/GID 1000 and the normal builder mounts. Both
compiles set `SOURCE_DATE_EPOCH` to the source commit's Unix timestamp: otherwise
the core's `chip-debug-report.cpp` embeds different `__DATE__`/`__TIME__` strings
and changes the app's ELF hash, image checksum and trailing SHA-256. All four
published parts (`app.bin`, `bootloader.bin`, `partitions.bin`, `boot_app0.bin`)
were byte-identical. The baseline `v0.08+3f8518ce92` app used 1,158,995 bytes of
its 1,572,864-byte slot and 119,332 bytes of RAM. Docker reported 8.21 GB for
the full image and 2.68 GB for the slim image; the install layer shrank
from 6.09 GB to 1.72 GB. These are build results, not hardware validation.

The installed tree is:

```
<install-dir>/.env                        # INGEST_AUTH_TOKEN, BIND_ADDR
<install-dir>/data/diagnostics/events.db  # ingest's bind-mounted database
<install-dir>/firmware-src/firmware/...
<install-dir>/firmware-src/tools/stamp_firmware_version.py
<install-dir>/firmware-out/current/{bootloader,partitions,boot_app0,app}.bin
<install-dir>/firmware-out/current/manifest.json
<install-dir>/firmware-out/cleanup/...    # only when current is enroll
```

No token is prompted for, printed, passed as a command argument, or sent to the
Docker daemon as a build argument. `.env` is parsed as data, never sourced.
Installer-generated plain values and single quoted literals are supported;
ambiguous interpolation/escape syntax fails closed. The token must be at least
32 bytes. Other settings are gathered on `/dev/tty`, with password echo off.
Host defaults to `.env`'s BIND_ADDR or the primary LAN IPv4 route's source address;
the user confirms it. The ingest port defaults to 8090, as in Compose.

For automation, use `--non-interactive` and these environment variables:

| Variable | Meaning |
| --- | --- |
| `TRACKER_WIFI_SSID` | Required; 1-32 UTF-8 bytes |
| `TRACKER_WIFI_PASSWORD` | Required; may be empty for an open network |
| `TRACKER_WIFI_BSSID` | Optional six colon-separated hexadecimal byte pairs |
| `TRACKER_DEVICE_ID` | Required; `[a-z0-9][a-z0-9_-]{0,31}` |
| `TRACKER_SERVER_HOST` | Required IPv4 address or hostname, without URL/port |
| `TRACKER_SERVER_PORT` | Optional; defaults to 8090 |
| `TRACKER_ACCEPT_DOWNLOAD=yes` | Explicitly accepts the first toolchain download |
| `TRACKER_FRESH_SD=yes` | Confirms fresh/unused SD state for this tracker |
| `TRACKER_NEW_BOARD=yes` | Confirms ID 1 has never been consumed on the board |
| `TRACKER_BUILDER_IMAGE` | Optional local image tag; default `mileage-firmware-builder:local` |

Only a verified absent/null server revision, a fresh SD card, and an unused
enrollment ID 1 produce `current` kind `enroll` plus `cleanup` kind `normal`.
An existing device (even revision zero), reused card, or used board produces
normal firmware only. A database read error stops the build; it never means
zero. The throwaway query container mounts the install's `data/` read-only,
opens SQLite with `mode=ro`, enables `query_only`, and uses a read transaction.
It does not use `immutable=1`, which can hide committed WAL data. Missing `data/`
is an error. No running ingest/viewer container or named data volume is changed.
Recovery requires the server AND SD lower bounds and a new enrollment ID;
follow [the recovery procedure](../../DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery).

The stamp script runs from a private copy of `firmware-src/` before compiling
with CI's FQBN, warnings, jobs, required stamp flag and the smallest app slot
limit parsed from `partitions.csv`. Release bundles carry the archive-aware stamp
script and `firmware/COMMIT`. A complete
Git checkout in `firmware-src/` is also supported for development.

The manifest uses the core's **actual generated `flash_args`**: ESP32 core
3.3.11's `recipe.hooks.objcopy.postobjcopy.4` supplies flash mode, image frequency,
flash size and each offset/file pair. `--show-properties=expanded` supplies
`build.mcu` and `build.project_name`; `--output-dir` supplies the sketch binaries.
CLI 1.5.1 does not export `boot_app0.bin` there, so the writer takes that file
from the build directory where the core's `postobjcopy.5` hook copies it beside
`flash_args`. The writer validates all four
parts, computes SHA-256 hashes, and verifies the app contains the stamped
version. It never assumes the example manifest's offsets. No merged image is
published. `check_pins.py` and server unittests enforce CI pin/flag parity.

Output directories are 0700 and files 0600 because the binaries contain
credentials. Settings and compiler scratch are private, ephemeral, and removed
on exit (including failure/signals). Compiler diagnostics are withheld on error
because they can include credential-bearing source lines. Existing output is
preserved until both requested images have completed, then replaced with a
rollback on rename failure. A new normal build removes stale cleanup output.
The builder owns `firmware-out/`; do not put unrelated files there.

Run piece 2 on the PC connected to the tracker. For enrollment, wait until
Device health shows a newly received current snapshot, then run piece 2 with
`-Cleanup` (PowerShell) or `--cleanup` (Bash), preserving NVS/trips.

For isolated tests only, `--test-data-dir DIR` substitutes a fake
`DIR/diagnostics/events.db` for the install data directory. It still performs a
real read-only query and never bypasses failures. Use only fake settings and a
fake `.env`; the option does not disable any enrollment safety checks.

## On Unraid

Run the builder as root from a persistent install under `/mnt/<pool>/`.
Unraid is detected from `/etc/unraid-version`; `MILEAGE_PLATFORM=unraid|debian`
overrides detection (`MILEAGE_UNRAID_VERSION_FILE` overrides the test fixture).
Debian still requires a normal user. On Unraid, high-water and compile helpers
run as the image's `app` user, **1000:1000**. Only builder-owned source, settings
and output folders are assigned to that UID, with directories 0700 and files
0600; symlinks are refused. `.env` keeps its root-only ownership and permissions.

Host Python is optional: when `python3` is absent, the builder reads the first
`FROM` image in `mileage-ingest/Dockerfile` and runs disposable Python containers
with no network and only the mounts each step needs. Configure receives `.env`
through a private FIFO, without copying its token into arguments or Docker
environment variables. Interactive configure/enrollment allocate a Docker TTY;
`--non-interactive` keeps the settings interface above. Publication validates
read-only mounts before the host replaces the output with rollback on failure.

Start the Unraid array and Docker first. Docker Buildx/BuildKit must be enabled
and report `linux/amd64` support; the builder checks both before configuration.
The Python image must already be cached or Docker must be able to pull it.
Copy the private finished files from `root@<host>` with the PC flash helper.
They remain 0700/0600 and readable by root. A diagnostics read failure stops
the build and never authorizes enrollment.
