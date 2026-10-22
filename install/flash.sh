#!/usr/bin/env bash
# Piece 2: run on the computer connected to the Supreme, not on the server.
tracker_flash() (
    set -euo pipefail
    umask 077
    RELEASE_TAG="@RELEASE_TAG@"
    ESPTOOL_VERSION=5.6.0
    # SHA-256 digests published in Espressif's GitHub release asset metadata:
    # https://api.github.com/repos/espressif/esptool/releases/tags/v5.6.0
    # https://github.com/espressif/esptool/releases/tag/v5.6.0

    # BEGIN PURE HELPERS (also exercised by tools/test_flash_scripts.py)
    flash_hash() {
        if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d ' ' -f1
        else shasum -a 256 "$1" | cut -d ' ' -f1; fi
    }
    flash_validate() {
        local dir=$1 cleanup=$2 name offset file digest start size end previous=0
        [ -f "$dir/manifest.json" ] && [ ! -L "$dir/manifest.json" ] || return 1
        jq -e '
          def hex: type == "string" and test("^0x[0-9a-fA-F]{1,8}$");
          .schema == 1 and .chip == "esp32s3" and
          (.kind == "enroll" or .kind == "normal") and
          (.version | type == "string") and (.commit | test("^[0-9a-fA-F]{40}$")) and
          (.fqbn | startswith("esp32:esp32:esp32s3:")) and
          (.flash_mode == "dio" or .flash_mode == "qio" or .flash_mode == "dout" or .flash_mode == "qout") and
          (.flash_freq == "80m" or .flash_freq == "40m" or .flash_freq == "20m") and
          .flash_size == "8MB" and (.app_offset | hex) and
          (.parts | type == "array" and length == 4) and
          ([.parts[].name] | sort == ["app","boot_app0","bootloader","partitions"]) and
          all(.parts[]; (.offset | hex) and .file == (.name + ".bin") and
              (.sha256 | test("^[0-9a-fA-F]{64}$"))) and
          .app_offset == (.parts[] | select(.name == "app") | .offset)
        ' "$dir/manifest.json" >/dev/null 2>&1 || return 1
        [ "$cleanup" != true ] || [ "$(jq -r .kind "$dir/manifest.json")" = normal ] || return 1
        # These are safety fences from the Supreme sketch's partitions.csv,
        # not flashing offsets. The actual offsets always come from the manifest.
        while IFS=$'\t' read -r name offset file digest; do
            [ -f "$dir/$file" ] && [ ! -L "$dir/$file" ] || return 1
            [ "$(flash_hash "$dir/$file")" = "$(printf '%s' "$digest" | tr 'A-F' 'a-f')" ] || return 1
            start=$((offset)); size=$(wc -c < "$dir/$file"); end=$((start + size))
            [ "$size" -gt 0 ] && [ $((start % 4096)) -eq 0 ] && [ "$start" -ge "$previous" ] || return 1
            case $name in
                bootloader) [ "$end" -le $((0x8000)) ] || return 1 ;;
                partitions) [ "$start" -ge $((0x8000)) ] && [ "$end" -le $((0x9000)) ] || return 1 ;;
                boot_app0) [ "$start" -ge $((0xe000)) ] && [ "$end" -le $((0x10000)) ] || return 1 ;;
                app) [ "$start" -ge $((0x10000)) ] && [ "$end" -le $((0x190000)) ] || return 1 ;;
            esac
            previous=$end
        done < <(flash_parts "$dir" true)
    }
    flash_parts() {
        local dir=$1 first=$2
        jq -r --arg first "$first" '
          .parts | sort_by(if .name == "bootloader" then 0 elif .name == "partitions" then 1
            elif .name == "boot_app0" then 2 else 3 end) | .[] |
          select($first == "true" or .name == "app") | [.name,.offset,.file,.sha256] | @tsv
        ' "$dir/manifest.json"
    }
    flash_choose_port() {
        local requested=$1 choice=$2; shift 2
        if [ -n "$requested" ]; then printf '%s\n' "$requested"; return; fi
        [ "$#" -gt 0 ] || return 1
        if [ "$#" -eq 1 ]; then printf '%s\n' "$1"; return; fi
        [[ $choice =~ ^[1-9][0-9]*$ ]] && [ "$choice" -le "$#" ] || return 1
        printf '%s\n' "${!choice}"
    }
    # END PURE HELPERS

    flash_prompt() { local reply; printf '%s' "$1" >/dev/tty; IFS= read -r reply </dev/tty; printf '%s' "$reply"; }
    flash_die() { printf 'Error: %s\n' "$1" >&2; exit 1; }
    flash_command() {
        if [ "$dry" = true ]; then printf 'DRY RUN:'; printf ' %q' "$esptool" "$@"; printf '\n'
        else "$esptool" "$@"; fi
    }
    flash_tool() {
        local platform archive expected cache unpack
        case "$(uname -s)-$(uname -m)" in
            Linux-x86_64) platform=linux-amd64; expected=1590723cfa9675983f49d0bf2895b8507f8d2879082e51bb7ef02a8bca88d354 ;;
            Linux-aarch64|Linux-arm64) platform=linux-aarch64; expected=7723120429394b4d6d974d694e5cf378e00259381a7f8856c2c88492041b9f59 ;;
            Linux-armv7l) platform=linux-armv7; expected=65e2f91fba2e79a75243454ded44da57532c74e242e17f9890aa7fe6cd588ee4 ;;
            Darwin-x86_64) platform=macos-amd64; expected=23921776a7df75f2e6f3ca10a604d74db4661e1b8a0ebb89bdddcd2d26f37055 ;;
            Darwin-arm64) platform=macos-arm64; expected=08a58829a3f14bb08f2e39f4cad48dcb14b7000384268e7f06689836332d6e25 ;;
            *) flash_die 'No pinned standalone esptool for this platform; supply --esptool.' ;;
        esac
        cache="$HOME/.cache/mileage-tracker"
        mkdir -p "$cache"; chmod 700 "$cache"
        unpack=$(mktemp -d "$cache/esptool.XXXXXXXX")
        tool_temp=$unpack
        archive="esptool-v${ESPTOOL_VERSION}-${platform}.tar.gz"
        printf 'Downloading esptool %s from Espressif GitHub releases (third-party host).\n' "$ESPTOOL_VERSION"
        curl -fsSL --connect-timeout 20 --max-time 300 --retry 3 "https://github.com/espressif/esptool/releases/download/v${ESPTOOL_VERSION}/$archive" -o "$unpack/archive.tar.gz"
        [ "$(flash_hash "$unpack/archive.tar.gz")" = "$expected" ] || flash_die 'esptool archive checksum mismatch.'
        tar -xzf "$unpack/archive.tar.gz" -C "$unpack"
        esptool=$(find "$unpack" -type f -name esptool -print -quit)
        [ -n "$esptool" ] || flash_die 'esptool executable missing from archive.'
        chmod 700 "$esptool"
    }
    # The literal tilde belongs to the remote user's home, not this computer.
    # shellcheck disable=SC2088
    local server='' install_dir='~/mileage-tracker' cleanup=false first=false port='' path='' dry=false esptool='' backup_dir='' skip_backup=false
    local temp='' tool_temp='' value version major before after chip read_flash write_flash hard_reset='hard-reset'
    local mode_flag=--flash-mode freq_flag=--flash-freq size_flag=--flash-size
    local erase_flash=erase-flash
    trap 'if [ -n "$temp" ]; then rm -rf -- "$temp"; fi; if [ -n "$tool_temp" ]; then rm -rf -- "$tool_temp"; fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    while [ "$#" -gt 0 ]; do
        case $1 in
            --help|-h) cat <<'HELP'
Usage: bash flash.sh [--server <user>@<host>] [--install-dir ~/mileage-tracker]
  [--path <folder>] [--cleanup] [--first-install] [--port <port>] [--dry-run]
  [--esptool <executable>] [--backup-dir <folder>] [--skip-backup] [--help]
Requires bash, curl, tar, jq, and OpenSSH scp (unless --path). No Python required.
Install jq: Debian/Ubuntu: sudo apt install jq; macOS: brew install jq.
Review before running: curl -fsSL <release-url>/flash.sh -o flash.sh; less flash.sh
Then: bash flash.sh --server '<user>@<host>'
Prompts use /dev/tty, including when curl is piped to bash.
--dry-run verifies firmware and esptool but never opens a serial device.
Default update and --cleanup write only the app and never erase settings or trips.
--first-install: verified double 8 MB backup, type ERASE, erase all flash, write four parts.
Use only for a new/factory board. Internal settings and trips are wiped; SD is untouched.
--first-install --skip-backup: re-provision a board already backed up; still type ERASE.
Dry run prints the backup/erase/write plan without requesting confirmation.
The standalone esptool version is pinned independently of the tracker release.
HELP
                return ;;
            --cleanup) cleanup=true ;;
            --first-install) first=true ;;
            --skip-backup) skip_backup=true ;;
            --dry-run) dry=true ;;
            --server|--install-dir|--port|--path|--esptool|--backup-dir)
                [ "$#" -ge 2 ] && [ -n "$2" ] || flash_die "Missing value for $1"
                value=$2
                case $1 in
                    --server) server=$value ;; --install-dir) install_dir=$value ;;
                    --port) port=$value ;; --path) path=$value ;;
                    --esptool) esptool=$value ;; --backup-dir) backup_dir=$value ;;
                esac
                shift ;;
            *) flash_die "Unknown option: $1" ;;
        esac
        shift
    done
    [ "$cleanup" != true ] || [ "$first" != true ] || flash_die 'Cleanup is app-only; cannot combine with first-install.'
    [ "$skip_backup" != true ] || [ "$first" = true ] || flash_die 'skip-backup requires explicit first-install.'
    command -v jq >/dev/null || flash_die 'jq is required. Debian/Ubuntu: sudo apt install jq; macOS: brew install jq.'
    temp=$(mktemp -d "${TMPDIR:-/tmp}/tracker-firmware.XXXXXXXX"); chmod 700 "$temp"
    if [ -n "$path" ]; then
        mkdir "$temp/payload"
        for value in manifest.json bootloader.bin partitions.bin boot_app0.bin app.bin; do
            [ ! -L "$path/$value" ] || flash_die 'Firmware symlinks are not allowed.'
            cp "$path/$value" "$temp/payload/$value"
        done
    else
        [ -n "$server" ] || server=$(flash_prompt 'SSH server (<user>@<host>): ')
        [[ $server =~ ^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+$ ]] || flash_die 'Use an SSH server in user@host form.'
        # Restrict remote paths to avoid shell interpretation with older scp.
        [[ $install_dir =~ ^(~/|/)[a-zA-Z0-9_./-]+$ ]] && [[ $install_dir != *'/../'* ]] || flash_die 'Install directory must be a simple absolute or ~/ path.'
        value=current; [ "$cleanup" != true ] || value=cleanup
        scp -rq "$server:${install_dir%/}/firmware-out/$value/" "$temp/payload" || flash_die 'Firmware copy failed.'
    fi
    chmod -R go-rwx "$temp"
    flash_validate "$temp/payload" "$cleanup" || flash_die 'Invalid firmware manifest, unsafe offsets, kind, size or checksum.'
    if [ -z "$esptool" ]; then flash_tool; fi
    version=$("$esptool" version 2>&1) || flash_die 'Cannot run esptool; use a standalone v4 or v5 executable.'
    if [[ $version =~ v?([45])\.[0-9]+ ]]; then major=${BASH_REMATCH[1]}; else flash_die 'esptool v4 or v5 required.'; fi
    before='no-reset'; after='no-reset'; chip='chip-id'; read_flash='read-flash'; write_flash='write-flash'
    if [ "$major" = 4 ]; then
        before=no_reset; after=no_reset; chip=chip_id; read_flash=read_flash; write_flash=write_flash
        hard_reset=hard_reset
        mode_flag=--flash_mode; freq_flag=--flash_freq; size_flag=--flash_size
        erase_flash=erase_flash
    fi
    printf 'Tracker flash (release %s). Keep the LoRa antenna attached; use a USB data cable.\n' "$RELEASE_TAG"
    printf 'Download mode: hold BOOT, plug in USB (or tap RESET), wait 2-3 seconds, release BOOT.\n'
    printf 'Close serial monitors. The port can change in download mode.\n'
    if [ "$dry" != true ]; then flash_prompt 'Enter download mode, then press Enter: ' >/dev/null; fi
    local ports=() candidate choice=''
    for candidate in /dev/cu.usbmodem* /dev/ttyACM*; do [ ! -e "$candidate" ] || ports+=("$candidate"); done
    if [ -z "$port" ]; then
        if [ "${#ports[@]}" -eq 0 ]; then flash_die 'No candidate ports. Re-enter download mode or supply --port.'; fi
        for ((value=0; value<${#ports[@]}; value++)); do printf '%s) %s\n' "$((value+1))" "${ports[value]}"; done
        if [ "${#ports[@]}" -gt 1 ]; then choice=$(flash_prompt 'Choose port number: '); fi
    fi
    port=$(flash_choose_port "$port" "$choice" "${ports[@]}") || flash_die 'Invalid port choice.'
    local common=(--chip esp32s3 --port "$port" --before "$before" --after "$after")
    flash_command "${common[@]}" "$chip" || flash_die 'Chip confirmation failed. On Linux check port permissions: Debian/Ubuntu use the dialout group; log out/in after joining. Close serial monitors and retry download mode.'
    if [ "$first" != true ] && [ "$cleanup" != true ] && [ "$dry" != true ]; then
        value=$(flash_prompt 'First install on a factory board? Backup, full erase, then write four parts? [y/N]: ')
        case $value in y|Y|yes|YES) first=true ;; esac
    fi
    if [ "$first" = true ]; then
        if [ "$skip_backup" != true ]; then
            if [ -z "$backup_dir" ]; then
                backup_dir="$HOME/mileage-tracker-backups"
                if [ "$dry" != true ]; then value=$(flash_prompt "Backup folder [$backup_dir]: "); backup_dir=${value:-$backup_dir}; fi
            fi
            local backup second
            backup="$backup_dir/supreme-factory-$(date -u +%Y%m%dT%H%M%SZ)-$$.bin"; second="$backup.verify.bin"
            if [ "$dry" != true ]; then mkdir -p "$backup_dir"; fi
            flash_command "${common[@]}" "$read_flash" 0 0x800000 "$backup"
            flash_command "${common[@]}" "$read_flash" 0 0x800000 "$second"
            if [ "$dry" != true ]; then
                [ "$(wc -c < "$backup")" -eq 8388608 ] && [ "$(wc -c < "$second")" -eq 8388608 ] || flash_die 'Factory backup is not 8 MB; no flash write performed.'
                cmp -s "$backup" "$second" || flash_die 'Factory backup reads differ; no flash write performed. Keep both for investigation.'
                rm -- "$second"
            fi
            printf 'Keep the factory backup privately outside the repo: %s (may contain device keys).\n' "$backup"
        else printf 'Skipping factory backup: re-provision only a board you already backed up.\n'; fi
        printf 'WARNING: This wipes everything on the board internal flash, including settings (trip counter, diagnostics enrollment record) and any trips on internal flash.\n'
        printf 'This is right for a brand-new board or one coming from factory firmware. It is wrong for a tracker already running this project; use the default update instead.\n'
        printf 'Trips on the SD card are not touched.\n'
        if [ "$dry" = true ]; then
            printf 'DRY RUN: A real first install requires typing ERASE. Backup verification is skipped only with explicit skip-backup.\n'
        else
            value=$(flash_prompt 'Type ERASE to erase the whole internal flash: ')
            [ "$value" = ERASE ] || flash_die 'Erase confirmation refused; nothing erased or written.'
        fi
        flash_command "${common[@]}" "$erase_flash"
        printf 'First install writes four separate parts after the full erase.\n'
    else
        printf 'App-only update: settings, NVS trip counter, diag/enroll, LittleFS trips and SD trips are kept.\n'
    fi
    local args=(--chip esp32s3 --port "$port" --before "$before" --after "$hard_reset" "$write_flash" "$mode_flag" "$(jq -r .flash_mode "$temp/payload/manifest.json")" "$freq_flag" "$(jq -r .flash_freq "$temp/payload/manifest.json")" "$size_flag" "$(jq -r .flash_size "$temp/payload/manifest.json")")
    local name offset file digest
    while IFS=$'\t' read -r name offset file digest; do args+=("$offset" "$temp/payload/$file"); done < <(flash_parts "$temp/payload" "$first")
    flash_command "${args[@]}"
    printf 'Press RESET once with BOOT released (hard reset) to run the sketch.\n'
    if [ "$first" = true ]; then
        printf 'Expect on the serial console: LittleFS partition is fully erased; provisioning trip storage.\n'
        printf 'Then: Setup complete. Logging trips.\n'
    fi
    printf 'Check the boot status card, GPS outdoors, WiFi uploads, and Device health.\n'
    if [ "$(jq -r .kind "$temp/payload/manifest.json")" = enroll ]; then
        printf 'Wait for the first snapshot in Device health, then run this script again with --cleanup (app only).\n'
    fi
)
tracker_flash "$@"
