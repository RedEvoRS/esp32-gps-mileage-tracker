#!/usr/bin/env bash
# Piece 1: install the server from this repository's release assets.
# Keep all executable code in main so a truncated curl download cannot install.
main() {
    set -euo pipefail
    RELEASE_TAG="@RELEASE_TAG@"
    local releases=https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases
    local install_dir=$HOME/mileage-tracker tag='' bundle='' yes=no
    local tty_available=no filename base root source old_version new_version
    local arg parent available compose_version os_id os_like

    fail() { printf 'setup: %s\n' "$@" >&2; exit 1; }
    usage() {
        cat <<'HELP'
Usage: bash setup.sh [--dir DIR] [--tag TAG] [--bundle FILE] [--yes]

Install or update the server (Debian 13 recommended). Run as your login user.
  --dir DIR       Install directory (default: ~/mileage-tracker)
  --tag TAG       Select a release instead of this script's stamped release
  --bundle FILE   Use mileage-tracker-<tag>.tar.gz and its adjacent SHA256SUMS
                  without downloading (the filename supplies the tag)
  --yes          Accept the source copy; skip wrapper prompts and the builder.
                  install.sh still needs its INSTALL_* environment presets for
                  unanswered configuration; missing answers fail, never guessed.
  --help         Show this help

Interactive prompts use /dev/tty, including when run as curl ... | bash.
Without a terminal, use --yes with install.sh's INSTALL_* presets, or download
and run in a terminal. Safer download-read-run route:
  curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh -o setup.sh
  less setup.sh
  bash setup.sh
Only this repository's GitHub release assets are downloaded by this wrapper.
The server installer and optional firmware builder install their dependencies.
HELP
    }
    confirm() {
        local answer
        while :; do
            printf '%s [y/N]: ' "$1" > /dev/tty
            IFS= read -r answer < /dev/tty || fail 'No answer from the terminal.'
            case $answer in
                y|Y|yes|YES) return 0 ;;
                ''|n|N|no|NO) return 1 ;;
                *) printf 'Please answer y or n.\n' > /dev/tty ;;
            esac
        done
    }
    version() {
        local value=unversioned commit=
        if [[ -f $1/VERSION ]]; then IFS= read -r value < "$1/VERSION" || true; fi
        if [[ -f $1/COMMIT ]]; then IFS= read -r commit < "$1/COMMIT" || true; fi
        value=${value%$'\r'}
        commit=${commit%$'\r'}
        [[ $value =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]] || value=unversioned
        if [[ $commit =~ ^([[:xdigit:]]{40}|[[:xdigit:]]{64})$ ]]; then
            value+=+${commit:0:10}
        fi
        printf '%s' "$value"
    }
    # Copy regular release files only; exclude private paths even on first use.
    # Reject destination symlinks rather than following them outside the install.
    copy_tree() {
        local from=$1 to=$2 scope=$3 prefix=$4 item name relative target
        [[ ! -L $to && ( ! -e $to || -d $to ) ]] \
            || fail "Source directory conflicts with an existing path: $to"
        mkdir -p -- "$to"
        for item in "$from"/* "$from"/.[!.]* "$from"/..?*; do
            [[ -e $item || -L $item ]] || continue
            name=${item##*/}
            relative=$prefix$name
            if [[ $scope == server ]]; then
                case $relative in
                    .env|viewer.env|monitor.env|backup/backup.conf|data|data/*|firmware-out|firmware-out/*)
                        continue ;;
                    firmware-src|firmware-src/*)
                        fail 'Bundle server/ contains reserved firmware-src/.' ;;
                esac
            fi
            target=$to/$name
            [[ ! -L $target ]] || fail "Source file conflicts with an existing symlink: $target"
            if [[ -d $item ]]; then
                copy_tree "$item" "$target" "$scope" "$relative/"
            elif [[ -f $item && ! -d $target ]]; then
                cp -p -- "$item" "$target"
            else
                fail "Unsupported source or destination path: $relative"
            fi
        done
    }
    flash_commands() {
        local host='' user server answer line value url ps_dir sh_dir
        user=$(id -un)
        # Read only BIND_ADDR; never source .env or print its other settings.
        if [[ -f $install_dir/.env ]]; then
            while IFS= read -r line || [[ -n $line ]]; do
                if [[ $line =~ ^[[:space:]]*BIND_ADDR=(.*)$ ]]; then
                    value=${BASH_REMATCH[1]%$'\r'}
                    value=${value#\"}; value=${value%\"}
                    value=${value#\'}; value=${value%\'}
                    host=$value
                fi
            done < "$install_dir/.env"
        fi
        if [[ -z $host || $host == 0.0.0.0 || $host == 127.* || $host == ::* ]]; then
            host=$(ip -4 route get 1.1.1.1 2>/dev/null \
                | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}') || host=
        fi
        host=${host:-<host>}
        server=$user@$host
        if [[ $yes == no ]]; then
            printf 'SSH server for piece 2 [%s] (correct user@host if needed): ' "$server" > /dev/tty
            IFS= read -r answer < /dev/tty || fail 'No SSH server answer from the terminal.'
            server=${answer:-$server}
        fi
        # Keep the printed commands copyable, including a nondefault install dir.
        [[ $server =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*@([A-Za-z0-9][A-Za-z0-9.:-]*|\<host\>)$ ]] \
            || fail 'SSH server must be user@host (no spaces or shell characters).'
        url=$releases/download/$tag
        ps_dir=${install_dir//\'/\'\'}
        printf -v sh_dir '%q' "$install_dir"
        printf '\nPiece 2: run on the computer connected to the tracker.\n'
        printf 'Confirm that %s is reachable over SSH from that computer.\n' "$server"
        printf 'Windows quick launch (asks for the server; the pipe cannot pass arguments):\n'
        printf '  irm %s/flash.ps1 | iex\n' "$url"
        printf 'Windows saved-script form with this server and install directory:\n'
        printf "  irm %s/flash.ps1 -OutFile flash.ps1; powershell -ExecutionPolicy Bypass -File flash.ps1 -Server '%s' -InstallDir '%s'\n" "$url" "$server" "$ps_dir"
        printf 'macOS / Linux:\n'
        printf "  curl -fsSL %s/flash.sh | bash -s -- --server '%s' --install-dir %s\n" "$url" "$server" "$sh_dir"
    }

    while (( $# )); do
        arg=$1
        case $arg in
            --dir|--tag|--bundle)
                (( $# >= 2 )) && [[ -n $2 && $2 != --* ]] || fail "$arg needs a value (see --help)."
                case $arg in
                    --dir) install_dir=$2 ;;
                    --tag) tag=$2 ;;
                    --bundle) bundle=$2 ;;
                esac
                shift 2 ;;
            --yes) yes=yes; shift ;;
            --help|-h) usage; return 0 ;;
            *) fail "Unknown option: $arg (see --help)." ;;
        esac
    done
    if [[ -n $bundle ]]; then
        [[ -f $bundle ]] || fail 'Local bundle does not exist; pass --bundle mileage-tracker-<tag>.tar.gz.'
        bundle=$(cd -- "$(dirname -- "$bundle")" && printf '%s/%s' "$PWD" "${bundle##*/}")
        filename=${bundle##*/}
        [[ $filename == mileage-tracker-*.tar.gz ]] || fail 'Bundle filename must be mileage-tracker-<tag>.tar.gz.'
        base=${filename#mileage-tracker-}; base=${base%.tar.gz}
        [[ -z $tag || $tag == "$base" ]] || fail '--tag does not match the local bundle filename.'
        tag=$base
    else
        tag=${tag:-$RELEASE_TAG}
    fi
    [[ $tag != @* ]] || fail 'This checkout script has no release tag. Supply --tag <tag> or --bundle <file>.'
    [[ $tag =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]] || fail 'Release tag must contain only letters, digits, dots, underscores, plus or minus.'
    if { : <> /dev/tty; } 2>/dev/null; then tty_available=yes; fi
    if [[ $yes == no && $tty_available == no ]]; then
        fail 'No terminal: prompts need /dev/tty. Download, inspect, then run in a terminal:' \
            "curl -fsSL $releases/download/$tag/setup.sh -o setup.sh" \
            'less setup.sh' 'bash setup.sh' \
            'For automation, use --yes and supply the existing install.sh INSTALL_* presets.'
    fi

    [[ $(uname -s) == Linux ]] || fail 'Use a Debian 13 Linux server for piece 1; run setup.sh there over SSH.'
    [[ -r /etc/os-release ]] || fail 'Cannot identify this Linux system. Use Debian 13 or a Debian-like distribution.'
    # shellcheck source=/dev/null
    os_id=$(. /etc/os-release; printf '%s' "${ID:-}")
    # shellcheck source=/dev/null
    os_like=$(. /etc/os-release; printf '%s' "${ID_LIKE:-}")
    if [[ $os_id != debian ]]; then
        case " $os_id $os_like " in
            *' debian '*|*' ubuntu '*) printf 'Warning: %s is Debian-like; Debian 13 is the recommended server.\n' "$os_id" ;;
            *) fail 'This wrapper requires Debian or a Debian-like distribution. Use Debian 13.' ;;
        esac
    fi
    for arg in curl tar sha256sum; do
        command -v "$arg" >/dev/null || fail "Missing $arg. Fix: sudo apt update && sudo apt install curl tar coreutils"
    done
    command -v docker >/dev/null || fail 'Docker Engine is missing. Install Engine and the Compose plugin using DEPLOY_DEBIAN.md Step 1 or https://docs.docker.com/engine/install/debian/.'
    compose_version=$(docker compose version --short 2>/dev/null) \
        || fail 'Compose v2 plugin is missing. Fix: sudo apt install docker-compose-plugin (Docker apt repository; DEPLOY_DEBIAN.md Step 1).'
    compose_version=${compose_version#v}
    if [[ ! $compose_version =~ ^([0-9]+)\. ]] || (( BASH_REMATCH[1] < 2 )); then
        fail 'Docker Compose v2 or newer is required. Install docker-compose-plugin; docker-compose v1 is unsupported.'
    fi
    docker info >/dev/null 2>&1 || fail 'Docker Engine is not usable by this login user.' \
        'Check: sudo systemctl enable --now docker; sudo docker info' \
        "If sudo works: sudo usermod -aG docker \"\$USER\", then log out and back in." \
        'Docker group access grants root-level privileges. Run setup as your normal user; install.sh asks for sudo when needed.'
    [[ $install_dir == /* ]] || install_dir=$PWD/$install_dir
    # Resolve the existing parent before measuring free space; do not create the
    # target until the bundle has passed checksum and layout validation.
    parent=$install_dir
    while [[ ! -d $parent ]]; do parent=$(dirname -- "$parent"); done
    available=$(df -Pk -- "$parent" | awk 'NR==2 {print $4}')
    [[ $available =~ ^[0-9]+$ ]] || fail 'Could not measure free disk space. Check: df -h'
    (( available >= 1048576 )) || fail 'Less than 1 GiB free at the install directory. Free space or select --dir on a larger disk; check df -h.'
    printf 'Free disk: %s KiB. The firmware builder image needs about 2.7 GB beyond the server; allow 10 GiB free for its first build.\n' "$available"
    if (( available < 10485760 )); then printf 'Warning: free at least 10 GiB before the first firmware build (download, extraction and image export).\n'; fi

    # Global within main for the EXIT trap, which also runs after main returns.
    temp_dir=$(mktemp -d)
    trap 'rm -rf -- "$temp_dir"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    filename=mileage-tracker-$tag.tar.gz
    if [[ -n $bundle ]]; then
        cp -- "$bundle" "$temp_dir/$filename"
        cp -- "${bundle%/*}/SHA256SUMS" "$temp_dir/SHA256SUMS" \
            || fail 'Local bundle needs an adjacent SHA256SUMS file.'
    else
        base=$releases/download/$tag
        curl -fsSL --retry 3 "$base/$filename" -o "$temp_dir/$filename" \
            || fail 'Bundle download failed. Check the tag, connection and release assets, then retry.'
        curl -fsSL --retry 3 "$base/SHA256SUMS" -o "$temp_dir/SHA256SUMS" \
            || fail 'Checksum download failed. Check that SHA256SUMS belongs to this release.'
    fi
    # Select exactly one checksum, ignoring every other asset in SHA256SUMS.
    # Pass only the expected basename to sha256sum; never trust paths from it.
    local hash='' count=0 digest entry extra
    while read -r digest entry extra; do
        if [[ $entry == "$filename" || $entry == "*$filename" ]]; then
            [[ $digest =~ ^[[:xdigit:]]{64}$ && -z $extra ]] || fail 'Malformed bundle checksum in SHA256SUMS.'
            hash=$digest
            count=$((count + 1))
        fi
    done < "$temp_dir/SHA256SUMS"
    (( count == 1 )) || fail 'SHA256SUMS must contain exactly one entry for this bundle.'
    (cd "$temp_dir"; printf '%s  %s\n' "$hash" "$filename" | sha256sum -c -) \
        || fail 'Checksum verification failed; nothing was installed. Download the bundle and SHA256SUMS again.'
    root=mileage-tracker-$tag
    tar -tzf "$temp_dir/$filename" > "$temp_dir/members"
    while IFS= read -r entry; do
        case $entry in
            "$root/"*) ;;
            *) fail 'Unexpected archive root; expected mileage-tracker-<tag>/.' ;;
        esac
        case /$entry/ in
            */../*|*/./*) fail 'Unsafe archive path.' ;;
        esac
    done < "$temp_dir/members"
    tar -tvzf "$temp_dir/$filename" | while IFS= read -r entry; do
        case $entry in
            [-d]*) ;;
            *) fail 'Bundle must contain only regular files and directories (no links or devices).' ;;
        esac
    done
    mkdir "$temp_dir/unpacked"
    tar -xzf "$temp_dir/$filename" --no-same-owner --no-same-permissions -C "$temp_dir/unpacked"
    source=$temp_dir/unpacked/$root
    for arg in server/install.sh server/VERSION server/COMMIT firmware/VERSION firmware/COMMIT \
        firmware/secrets.h.example tools/stamp_firmware_version.py; do
        [[ -f $source/$arg ]] || fail "Bundle is missing $arg. Use a complete release bundle."
    done
    [[ -d $source/firmware/04_trip_logger ]] || fail 'Bundle is missing firmware/04_trip_logger/.'
    old_version=$(version "$install_dir")
    new_version=$(version "$source/server")
    if [[ -f $install_dir/install.sh ]]; then
        printf 'Server update: %s -> %s\n' "$old_version" "$new_version"
    else
        printf 'First server install: %s\n' "$new_version"
    fi
    printf 'Source destination: %s\n' "$install_dir"
    printf 'Private .env, viewer.env, monitor.env, backup/backup.conf, data/ and firmware-out/ are excluded.\n'
    printf 'Files deleted upstream are not removed; review obsolete source files after updates.\n'
    if [[ $yes == no ]]; then
        confirm 'Copy this release and run the server installer?' || { printf 'Cancelled.\n'; return 0; }
    fi
    copy_tree "$source/server" "$install_dir" server ''
    mkdir -p "$temp_dir/firmware-src/tools"
    mv "$source/firmware" "$temp_dir/firmware-src/firmware"
    cp -p "$source/tools/stamp_firmware_version.py" "$temp_dir/firmware-src/tools/"
    copy_tree "$temp_dir/firmware-src" "$install_dir/firmware-src" firmware ''
    install_dir=$(cd -- "$install_dir" && pwd -P)
    chmod u+x "$install_dir/install.sh"
    printf 'Running install.sh to configure, rebuild and verify the server.\n'
    if [[ $yes == yes ]]; then
        printf '%s\n' '--yes: install.sh uses existing INSTALL_* presets; missing required answers fail.'
        (cd "$install_dir"; ./install.sh < /dev/null)
    else
        (cd "$install_dir"; ./install.sh < /dev/tty)
    fi
    if [[ -f $install_dir/firmware-builder/build.sh ]]; then
        if [[ $yes == no ]] && confirm 'Build your tracker firmware now (about 2.7 GB image; 10 GiB free for first build)?'; then
            bash "$install_dir/firmware-builder/build.sh" --install-dir "$install_dir" < /dev/tty
        else
            printf 'Firmware build skipped. Run when ready:\n  bash %q --install-dir %q\n' \
                "$install_dir/firmware-builder/build.sh" "$install_dir"
        fi
    else
        printf "The firmware builder isn't in this release.\n"
    fi
    flash_commands
}
main "$@"
