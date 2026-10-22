#!/usr/bin/env bash
# Launch a nested Docker/systemd lab; EXIT always removes our container.
set -euo pipefail
[[ $# == 3 ]] || { echo 'Usage: lab.sh ASSETS_DIR CACHE_DIR REFERENCE_DIR' >&2; exit 2; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
assets=$(cd -- "$1" && pwd)
cache=$(mkdir -p -- "$2" && cd -- "$2" && pwd)
reference=$(mkdir -p -- "$3" && cd -- "$3" && pwd)
image=two-piece-ci-lab:local
container=two-piece-ci-$(date -u +%s)-$$
# Invoked by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    docker rm -f -v "$container" >/dev/null 2>&1 || true
    docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build -t "$image" "$root/tools/ci-install"
docker run -d --name "$container" --hostname two-piece-ci --privileged \
    --cgroupns private --tmpfs /run --tmpfs /run/lock \
    --mount "type=bind,src=$assets,dst=/assets,readonly" \
    --mount "type=bind,src=$root,dst=/tests,readonly" \
    --mount "type=bind,src=$cache,dst=/cache" \
    --mount "type=bind,src=$reference,dst=/reference,readonly" "$image"
mode=server
[[ ${RUN_BUILDER:-false} != true ]] || mode=builder
status=0
docker exec "$container" bash /tests/tools/ci-install/run.sh "$mode" || status=$?
if ((status)); then
    docker logs "$container"
    docker exec "$container" journalctl -u docker --no-pager -n 30 || true
fi
exit "$status"
