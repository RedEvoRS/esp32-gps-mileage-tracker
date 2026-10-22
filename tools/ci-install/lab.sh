#!/usr/bin/env bash
# Launch a nested Docker/systemd lab; EXIT always removes our container.
set -euo pipefail
[[ $# == 3 ]] || { echo 'Usage: lab.sh ASSETS_DIR CACHE_DIR REFERENCE_DIR' >&2; exit 2; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
assets=$(cd -- "$1" && pwd)
cache=$(mkdir -p -- "$2" && cd -- "$2" && pwd)
reference=$(mkdir -p -- "$3" && cd -- "$3" && pwd)
container=two-piece-ci-$(date -u +%s)-$$
image=$container:local
# Git Bash can drive Docker Desktop locally; keep Docker's bind paths native.
if command -v cygpath >/dev/null; then
    root=$(cygpath -am "$root")
    assets=$(cygpath -am "$assets")
    cache=$(cygpath -am "$cache")
    reference=$(cygpath -am "$reference")
fi
# Invoked by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    if [[ -n ${sampler:-} ]]; then
        kill "$sampler" 2>/dev/null || true
        wait "$sampler" 2>/dev/null || true
    fi
    if docker inspect "$container" >/dev/null 2>&1; then
        docker exec "$container" bash /tests/tools/ci-install/disk.sh final || true
    fi
    docker rm -f -v "$container" >/dev/null 2>&1 || true
    docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker build -t "$image" "$root/tools/ci-install"
docker run -d --name "$container" --hostname two-piece-ci --privileged \
    --cgroupns private --tmpfs /run --tmpfs /run/lock \
    --volume /var/lib/docker --volume /var/lib/containerd \
    --mount "type=bind,src=$assets,dst=/assets,readonly" \
    --mount "type=bind,src=$root,dst=/tests,readonly" \
    --mount "type=bind,src=$cache,dst=/cache" \
    --mount "type=bind,src=$reference,dst=/reference,readonly" "$image"
# Native overlayfs/containerd snapshots live on fresh volumes, rather than
# overlay-on-overlay or vfs's full copies. rm -v removes only these volumes.
# Samples survive a test failure in the log, and final runs before removal.
(
    while docker inspect "$container" >/dev/null 2>&1; do
        docker exec "$container" bash /tests/tools/ci-install/disk.sh sample || true
        sleep 10
    done
) &
sampler=$!
mode=server
[[ ${RUN_BUILDER:-false} != true ]] || mode=builder
status=0
docker exec -e SAVE_BUILDER_CACHE="${SAVE_BUILDER_CACHE:-true}" "$container" bash /tests/tools/ci-install/run.sh "$mode" || status=$?
if ((status)); then
    docker logs "$container"
    docker exec "$container" journalctl -u docker --no-pager -n 30 || true
fi
exit "$status"
