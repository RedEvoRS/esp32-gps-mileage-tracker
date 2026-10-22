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

cleanup() {
    if [[ -n $CONTAINER ]]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
    if [[ -n $PRIVATE_DIR ]]; then rm -rf -- "$PRIVATE_DIR"; fi
}

usage() {
    cat <<'HELP'
Usage: build.sh [--install-dir DIR] [--non-interactive] [--help]
Builds private firmware-out/current and, for first enrollment, cleanup.
Requires Docker, Python 3, flock, and the installed firmware-src/ bundle.
Non-interactive settings: TRACKER_WIFI_SSID, TRACKER_WIFI_PASSWORD,
TRACKER_WIFI_BSSID (optional), TRACKER_DEVICE_ID, TRACKER_SERVER_HOST,
TRACKER_SERVER_PORT (default 8090). Token is read only from .env.
TRACKER_ACCEPT_DOWNLOAD=yes accepts the first ~1-2 GB toolchain download.
TRACKER_FRESH_SD=yes AND TRACKER_NEW_BOARD=yes explicitly authorize ID 1;
otherwise builds normal firmware. Never infer fresh state from a failed read.
For isolated fake-install tests only: --test-data-dir DIR reads a fake
DIR/diagnostics/events.db, still read-only, instead of the install's data/.
HELP
}

main() {
    local noninteractive=0 test_data='' arg uid gid image_inputs cached_inputs
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
    for arg in docker python3 flock; do
        command -v "$arg" >/dev/null || { printf 'Required command missing: %s\n' "$arg" >&2; return 1; }
    done
    uid=$(id -u); gid=$(id -g)
    [[ $uid != 0 ]] || { echo 'Run the builder as a normal user with Docker access, not root.' >&2; return 1; }
    INSTALL_DIR=$(cd -- "$INSTALL_DIR" && pwd)
    # Never overlap builds or replace a set another invocation is preparing.
    exec 9>"$INSTALL_DIR/.firmware-builder.lock"
    flock -n 9 || { echo 'Another firmware build is running.' >&2; return 1; }
    PRIVATE_DIR=$(mktemp -d "$INSTALL_DIR/.firmware-build.XXXXXXXX")
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    chmod 700 "$PRIVATE_DIR"
    python3 "$SCRIPT_DIR/builder.py" configure "$INSTALL_DIR" "$PRIVATE_DIR" "$noninteractive"
    # Only public builder files are in this context. Toolchain layers are cached.
    # A saved/loaded image can lose backend-specific build cache. Reuse the
    # image itself only when every allowed context input matches its label.
    image_inputs=$(python3 - "$SCRIPT_DIR" <<'PY'
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
        docker build --platform linux/amd64 --cache-from "$IMAGE" \
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
        --user "$uid:$gid" --read-only --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$test_data,dst=/data,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR,dst=/private" \
        "$IMAGE" high-water /data /private
    CONTAINER=
    python3 "$SCRIPT_DIR/builder.py" enrollment "$PRIVATE_DIR" "$noninteractive"
    mkdir -m 700 "$PRIVATE_DIR/result"
    CONTAINER="mileage-firmware-build-$(basename -- "$PRIVATE_DIR" | tr '[:upper:].' '[:lower:]-')"
    # Secrets are separate read-only mounts. All compiler scratch is ephemeral.
    docker run --rm --name "$CONTAINER" --platform linux/amd64 --network none \
        --user "$uid:$gid" --read-only --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,exec,mode=1777 \
        --mount "type=bind,src=$INSTALL_DIR/firmware-src,dst=/source,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR,dst=/private,readonly" \
        --mount "type=bind,src=$PRIVATE_DIR/result,dst=/result" \
        "$IMAGE" compile
    CONTAINER=
    python3 "$SCRIPT_DIR/builder.py" publish "$INSTALL_DIR" "$PRIVATE_DIR"
}

main "$@"
