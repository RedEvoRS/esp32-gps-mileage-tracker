#!/usr/bin/env bash
# Personal firmware never enters the Docker build context or image layers.
set -euo pipefail
set +x
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=$(dirname -- "$SCRIPT_DIR")
IMAGE=${TRACKER_BUILDER_IMAGE:-mileage-firmware-builder:local}
PRIVATE_DIR=
CONTAINER=
ENV_WRITER=
PYTHON_IMAGE=
HELPER_UID=
HELPER_GID=

is_unraid() {
    case ${MILEAGE_PLATFORM:-} in
        unraid) return 0 ;;
        debian) return 1 ;;
    esac
    local f=${MILEAGE_UNRAID_VERSION_FILE:-/etc/unraid-version}
    [[ -r $f ]] && grep -Eq '^version="?[0-9]' "$f"
}

private_tree() {
    local path=$1
    [[ -d $path && ! -L $path ]] || { echo 'Expected a real builder-owned directory.' >&2; return 1; }
    if [[ -n $(find -P "$path" ! -type d ! -type f -print -quit) ]]; then
        echo 'Builder private folders must contain only real directories and regular files, without symlinks.' >&2
        return 1
    fi
    find -P "$path" -exec chown --no-dereference "$HELPER_UID:$HELPER_GID" {} +
    find -P "$path" -type d -exec chmod 700 {} +
    find -P "$path" -type f -exec chmod 600 {} +
}

python_route() {
    if command -v python3 >/dev/null; then
        echo 'Firmware builder Python: host python3.' >&2
    else
        PYTHON_IMAGE=$(awk '$1 == "FROM" { sub(/\r$/, "", $2); print $2; exit }' "$SCRIPT_DIR/../mileage-ingest/Dockerfile")
        [[ -n $PYTHON_IMAGE ]] || { echo 'Cannot read the ingest Python image for the fallback.' >&2; return 1; }
        printf 'Firmware builder Python: throwaway %s container (no network).\n' "$PYTHON_IMAGE" >&2
    fi
}

python_step() {
    local step=$1 noninteractive=${2:-1} name summary status
    local -a options=(--rm -i --network none --user "$HELPER_UID:$HELPER_GID"
        --read-only --cap-drop ALL --security-opt no-new-privileges -w /builder)
    local -a command=(python3 /builder/builder.py "$step")
    if [[ -z $PYTHON_IMAGE ]]; then
        case $step in
            inputs) python3 - "$SCRIPT_DIR" ;;
            configure) python3 "$SCRIPT_DIR/builder.py" configure "$INSTALL_DIR" "$PRIVATE_DIR" "$noninteractive" ;;
            enrollment) python3 "$SCRIPT_DIR/builder.py" enrollment "$PRIVATE_DIR" "$noninteractive" ;;
            publish) python3 "$SCRIPT_DIR/builder.py" publish "$INSTALL_DIR" "$PRIVATE_DIR" ;;
        esac
        return
    fi
    if [[ $step == inputs ]]; then
        docker run "${options[@]}" --mount "type=bind,src=$PRIVATE_DIR/python,dst=/builder,readonly" \
            "$PYTHON_IMAGE" python3 - /builder
        return
    fi
    options+=(--mount "type=bind,src=$PRIVATE_DIR/python/builder.py,dst=/builder/builder.py,readonly")
    case $step in
        configure|enrollment)
            # Preserve the existing automation interface; values are never argv.
            for name in TRACKER_ACCEPT_DOWNLOAD TRACKER_WIFI_SSID TRACKER_WIFI_PASSWORD \
                TRACKER_WIFI_BSSID TRACKER_DEVICE_ID TRACKER_SERVER_HOST TRACKER_SERVER_PORT \
                TRACKER_NTP_SERVER_1 TRACKER_NTP_SERVER_2 \
                TRACKER_FRESH_SD TRACKER_NEW_BOARD; do
                if [[ -v $name ]]; then options+=(--env "$name"); fi
            done
            if [[ $noninteractive == 0 && -t 0 ]]; then options+=(-t); fi
            options+=(--mount "type=bind,src=$PRIVATE_DIR,dst=/private")
            if [[ $step == configure ]]; then
                # .env stays root-only. Stream it through a private pipe instead
                # of changing its owner/mode or putting the token in Docker env.
                mkfifo -m 600 "$PRIVATE_DIR/env.pipe"
                if is_unraid && [[ $(id -u) == 0 ]]; then
                    chown --no-dereference "$HELPER_UID:$HELPER_GID" "$PRIVATE_DIR/env.pipe"
                fi
                cat -- "$INSTALL_DIR/.env" >"$PRIVATE_DIR/env.pipe" &
                ENV_WRITER=$!
                options+=(--mount "type=bind,src=$PRIVATE_DIR/env.pipe,dst=/install/.env,readonly"
                    --mount "type=bind,src=$INSTALL_DIR/firmware-src/firmware/04_trip_logger/04_trip_logger.ino,dst=/install/firmware-src/firmware/04_trip_logger/04_trip_logger.ino,readonly")
                command+=(/install /private "$noninteractive")
            else
                command+=(/private "$noninteractive")
            fi
            docker run "${options[@]}" "$PYTHON_IMAGE" "${command[@]}"
            if [[ -n $ENV_WRITER ]]; then
                wait "$ENV_WRITER"
                ENV_WRITER=
                rm -- "$PRIVATE_DIR/env.pipe"
            fi
            ;;
        publish)
            [[ ! -L $INSTALL_DIR/firmware-out ]] || { echo 'firmware-out must not be a symlink.' >&2; return 1; }
            options+=(--mount "type=bind,src=$PRIVATE_DIR/result,dst=/private/result,readonly")
            if [[ -e $INSTALL_DIR/firmware-out ]]; then
                options+=(--mount "type=bind,src=$INSTALL_DIR/firmware-out,dst=/install/firmware-out,readonly")
            fi
            summary=$(docker run "${options[@]}" "$PYTHON_IMAGE" "${command[@]}" /install /private --prepare)
            if [[ -e $INSTALL_DIR/firmware-out ]]; then mv -T -- "$INSTALL_DIR/firmware-out" "$PRIVATE_DIR/previous"; fi
            if mv -T -- "$PRIVATE_DIR/result" "$INSTALL_DIR/firmware-out"; then
                printf '%s\n' "$summary"
            else
                status=$?
                if [[ -e $PRIVATE_DIR/previous ]]; then mv -T -- "$PRIVATE_DIR/previous" "$INSTALL_DIR/firmware-out"; fi
                return "$status"
            fi
            ;;
    esac
}

cleanup() {
    if [[ -n $ENV_WRITER ]]; then kill "$ENV_WRITER" 2>/dev/null || true; wait "$ENV_WRITER" 2>/dev/null || true; fi
    if [[ -n $CONTAINER ]]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
    if [[ -n $PRIVATE_DIR ]]; then rm -rf -- "$PRIVATE_DIR"; fi
}

usage() {
    cat <<'HELP'
Usage: build.sh [--install-dir DIR] [--non-interactive] [--help]
Builds private firmware-out/current and, for first enrollment, cleanup.
Requires Docker with Buildx/BuildKit, flock, and the installed firmware-src/ bundle.
Uses host Python 3 when available, otherwise a throwaway Python container.
Non-interactive settings: TRACKER_WIFI_SSID, TRACKER_WIFI_PASSWORD,
TRACKER_WIFI_BSSID (optional), TRACKER_DEVICE_ID, TRACKER_SERVER_HOST,
TRACKER_SERVER_PORT (default 8090). Token is read only from .env.
Advanced optional settings (both modes): TRACKER_NTP_SERVER_1, TRACKER_NTP_SERVER_2.
Empty/unset uses the firmware's default time servers; see firmware-builder/README.md.
TRACKER_ACCEPT_DOWNLOAD=yes accepts the first download (about 1.8 GB; image about 2.7 GB).
TRACKER_FRESH_SD=yes AND TRACKER_NEW_BOARD=yes explicitly authorize ID 1;
otherwise builds normal firmware. Never infer fresh state from a failed read.
For isolated fake-install tests only: --test-data-dir DIR reads a fake
DIR/diagnostics/events.db, still read-only, instead of the install's data/.
HELP
}

main() {
    local noninteractive=0 test_data='' arg uid gid image_inputs cached_inputs platforms
    while (($#)); do
        arg=$1; shift
        case "$arg" in
            --install-dir) [[ $# -gt 0 ]] || { usage >&2; return 2; }; INSTALL_DIR=$1; shift ;;
            --non-interactive) noninteractive=1 ;;
            --test-data-dir) [[ $# -gt 0 ]] || { usage >&2; return 2; }; test_data=$1; shift ;;
            --help|-h) usage; return ;;
            *) usage >&2; return 2 ;;
        esac
    done
    for arg in docker flock awk find chown chmod mkfifo id dirname mktemp rm cat mkdir basename tr mv grep cp; do
        if ! command -v "$arg" >/dev/null; then
            printf 'Required command missing: %s\n' "$arg" >&2
            echo 'On Unraid, start the array/Docker and restore missing stock shell tools before building.' >&2
            return 1
        fi
    done
    uid=$(id -u); gid=$(id -g)
    if [[ $uid == 0 ]]; then
        is_unraid || { echo 'Run the builder as a normal user with Docker access, not root.' >&2; return 1; }
    fi
    HELPER_UID=$uid; HELPER_GID=$gid
    if is_unraid; then HELPER_UID=1000; HELPER_GID=1000; fi
    if ! docker buildx version >/dev/null 2>&1 || ! platforms=$(docker buildx inspect --bootstrap 2>/dev/null); then
        echo 'Docker Buildx/BuildKit is required. On Unraid, start the array and Docker, then install/enable Docker Buildx.' >&2
        return 1
    fi
    if ! grep -Eq '(^|[[:space:],])linux/amd64([[:space:],/*]|$)' <<<"$platforms"; then
        echo 'Docker BuildKit must support linux/amd64. On Unraid use x86-64 Docker; on other hosts enable amd64 emulation.' >&2
        return 1
    fi
    python_route
    INSTALL_DIR=$(cd -- "$INSTALL_DIR" && pwd)
    # Never overlap builds or replace a set another invocation is preparing.
    exec 9>"$INSTALL_DIR/.firmware-builder.lock"
    flock -n 9 || { echo 'Another firmware build is running.' >&2; return 1; }
    PRIVATE_DIR=$(mktemp -d "$INSTALL_DIR/.firmware-build.XXXXXXXX")
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    chmod 700 "$PRIVATE_DIR"
    if [[ -n $PYTHON_IMAGE ]]; then
        # Install source files may be root-only under a restrictive umask.
        # Give helpers private copies rather than widening those permissions.
        mkdir -m 700 "$PRIVATE_DIR/python"
        cp -- "$SCRIPT_DIR/"{.dockerignore,Dockerfile,builder.py,pins.env} "$PRIVATE_DIR/python/"
    fi
    if is_unraid && [[ $uid == 0 ]]; then
        private_tree "$INSTALL_DIR/firmware-src"
        private_tree "$PRIVATE_DIR"
    fi
    python_step configure "$noninteractive"
    if is_unraid && [[ $uid == 0 ]]; then private_tree "$PRIVATE_DIR"; fi
    # Only public builder files are in this context. Toolchain layers are cached.
    # A saved/loaded image can lose backend-specific build cache. Reuse the
    # image itself only when every allowed context input matches its label.
    image_inputs=$(python_step inputs <<'PY'
import hashlib
from pathlib import Path
import sys

root = Path(sys.argv[1])
digest = hashlib.sha256()
for name in (".dockerignore", "Dockerfile", "builder.py", "pins.env"):
    digest.update(name.encode() + b"\0" + hashlib.sha256((root / name).read_bytes()).digest())
print(digest.hexdigest())
PY
    )
    cached_inputs=$(docker image inspect --format '{{ index .Config.Labels "tracker.builder-inputs" }}' "$IMAGE" 2>/dev/null || true)
    if [[ $cached_inputs != "$image_inputs" ]]; then
        DOCKER_BUILDKIT=1 docker build --platform linux/amd64 --cache-from "$IMAGE" \
            --build-arg BUILDKIT_INLINE_CACHE=1 --label "tracker.builder-inputs=$image_inputs" \
            -t "$IMAGE" "$SCRIPT_DIR"
    fi
    CONTAINER="mileage-firmware-build-$(basename -- "$PRIVATE_DIR" | tr '[:upper:].' '[:lower:]-')"
    if [[ -n $test_data ]]; then
        test_data=$(cd -- "$test_data" && pwd)
        echo 'TEST MODE: using the explicitly supplied fake data directory.'
    else
        test_data=$INSTALL_DIR/data
    fi
    # Compose's ingest storage is ./data:/data (not a named volume). --mount
    # refuses a missing source instead of silently creating an empty directory.
    docker run --rm --name "$CONTAINER" --platform linux/amd64 --network none \
        --user "$HELPER_UID:$HELPER_GID" --read-only --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$test_data,dst=/data,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR,dst=/private" \
        "$IMAGE" high-water /data /private
    CONTAINER=
    python_step enrollment "$noninteractive"
    mkdir -m 700 "$PRIVATE_DIR/result"
    if is_unraid && [[ $uid == 0 ]]; then private_tree "$PRIVATE_DIR"; fi
    CONTAINER="mileage-firmware-build-$(basename -- "$PRIVATE_DIR" | tr '[:upper:].' '[:lower:]-')"
    # Secrets are separate read-only mounts. All compiler scratch is ephemeral.
    docker run --rm --name "$CONTAINER" --platform linux/amd64 --network none \
        --user "$HELPER_UID:$HELPER_GID" --read-only --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,exec,mode=1777 \
        --mount "type=bind,src=$INSTALL_DIR/firmware-src,dst=/source,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR,dst=/private,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR/result,dst=/result" \
        "$IMAGE" compile
    CONTAINER=
    if is_unraid && [[ $uid == 0 && -e $INSTALL_DIR/firmware-out ]]; then private_tree "$INSTALL_DIR/firmware-out"; fi
    python_step publish
}

main "$@"
