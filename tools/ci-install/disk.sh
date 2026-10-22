#!/usr/bin/env bash
# Read-only sampling in the disposable lab. All numbers are allocated bytes.
set -euo pipefail
read -r used available < <(df -B1 --output=used,avail / | tail -n 1)
# /cache can be a different filesystem locally; include it in the explicit
# footprint even then. On Actions these all consume the runner's root disk.
# Stay on each filesystem: do not count merged executor rootfs mounts twice.
# Garbage collection can race du; an unavailable sample is not zero.
bytes=$(du -sx -B1 /var/lib/docker /var/lib/containerd /cache 2>/dev/null | awk '{n += $1} END {print n}') || bytes=unknown
cache_bytes=$(du -sx -B1 /cache 2>/dev/null | cut -f1) || cache_bytes=unknown
printf 'LAB_DISK %s %s used=%s available=%s docker_and_cache=%s cache=%s\n' \
    "$(date -u +%FT%TZ)" "${1:-sample}" "$used" "$available" "$bytes" "$cache_bytes"
if [[ ${1:-} == final ]]; then
    df -h /
    du -shx /var/lib/docker /var/lib/containerd /cache || true
    docker system df
fi
