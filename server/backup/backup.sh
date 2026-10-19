#!/usr/bin/env bash
# Back up the trip files and the SQLite databases, and copy the
# backup off this machine. Meant to run daily from mileage-backup@.timer
# (DEPLOY_DEBIAN.md Step 10), but safe to run by hand at any time.
#
# Each run:
#   1. Snapshots the viewer's annotations DB with SQLite's backup API,
#      diagnostics DB and optional monitor state, and checks each copy.
#   2. Copies ./data while holding the ingest upload lock, so a trip's
#      .gpx and .json are never caught half-written. Files unchanged
#      since the last snapshot are hard links, so each snapshot is cheap.
#   3. Writes SHA256SUMS. If nothing changed since the last snapshot, the
#      new one is dropped. Old snapshots are pruned (KEEP_RECENT newest,
#      plus the first of each month for KEEP_MONTHLY months).
#   4. rsyncs every snapshot to REMOTE and re-reads the newest copy there
#      to confirm it matches byte for byte.
#
# Layout under BACKUP_DIR (the same under REMOTE, minus `latest`):
#   snapshots/2026-10-06T033012Z/{annotations.db,events.db?,monitor.db?,data/,SHA256SUMS,INFO}
#   latest -> snapshots/<newest>
#   last-success
#
# Restore and restore tests: restore.sh.

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

[[ -n $REMOTE ]] || die "REMOTE is not set in $CONF. A backup that stays on this box isn't one."
[[ $KEEP_RECENT =~ ^[1-9][0-9]*$ ]] || die "KEEP_RECENT must be a positive number"
[[ $KEEP_MONTHLY =~ ^[0-9]+$ ]] || die "KEEP_MONTHLY must be a number"
require_commands rsync flock sha256sum cmp
[[ -d $COMPOSE_DIR/data ]] || die "no trip data directory at $COMPOSE_DIR/data"

mkdir -p "$SNAPSHOTS"
exec 9> "$BACKUP_DIR/.lock"
flock -n 9 || die "another backup is already running"

name=$(date -u +%Y-%m-%dT%H%M%SZ)
work=$SNAPSHOTS/.partial-$name
rm -rf "$SNAPSHOTS"/.partial-*        # left behind by a run that died
prev=$(list_snapshots | tail -n 1)
mkdir "$work"

# --- 1. Annotations database -------------------------------------------

log "Snapshotting the annotations database"
snapshot_db viewer_python /db/annotations.db "$work/annotations.db" no
annotations=$(sed -E 's/.* annotations=([0-9]+).*/\1/' <<< "$SNAPSHOT_REPORT")

# The viewer already mounts /data read-only. SQLite mode=ro avoids creating
# a missing diagnostics database and needs no ingest credentials.
log "Snapshotting the diagnostics database"
snapshot_db viewer_python /data/diagnostics/events.db "$work/events.db" yes
events=missing
if [[ $SNAPSHOT_REPORT != missing ]]; then
    events=$(sed -E 's/.* events=([0-9]+).*/\1/' <<< "$SNAPSHOT_REPORT")
fi

find_monitor_volume
monitor=not-installed
if [[ $MONITOR_PRESENT == yes ]]; then
    log "Snapshotting the monitor database"
    snapshot_db monitor_python /state/monitor.db "$work/monitor.db" yes
    monitor=missing
    [[ $SNAPSHOT_REPORT == missing ]] || monitor=snapshotted
else
    log "Monitor is not installed; skipping monitor.db"
fi

# --- 2. Trip files -----------------------------------------------------

log "Copying trip files"
link=()
[[ -n $prev && -d $SNAPSHOTS/$prev/data ]] && link=(--link-dest="$SNAPSHOTS/$prev/data")
# Ingest holds this lock for the whole write of a trip's GPX + JSON.
flock -w 300 "$COMPOSE_DIR/data/.upload.lock" \
    rsync -a --exclude=/.upload.lock --exclude='*.tmp' \
        --exclude=/diagnostics/events.db --exclude='/diagnostics/events.db-*' "${link[@]}" \
        "$COMPOSE_DIR/data/" "$work/data/"
trips=$(find "$work/data" -type f -name '*.gpx' | wc -l)

# --- 3. Manifest, dedupe, prune ----------------------------------------

(cd "$work" && find . -type f ! -name SHA256SUMS -print0 \
    | LC_ALL=C sort -z | xargs -0 -r sha256sum) > "$work/SHA256SUMS"

if [[ -n $prev ]] && cmp -s "$work/SHA256SUMS" "$SNAPSHOTS/$prev/SHA256SUMS"; then
    log "Nothing changed since $prev; keeping that snapshot"
    rm -rf "$work"
    current=$prev
else
    # Names have one-second resolution; a second run within the same
    # second (by hand, straight after the timer) waits for a fresh name.
    while [[ -e $SNAPSHOTS/$name ]]; do
        sleep 1
        name=$(date -u +%Y-%m-%dT%H%M%SZ)
    done
    # INFO is for people. It sits outside SHA256SUMS so its timestamp
    # doesn't make every snapshot look changed.
    cat > "$work/INFO" <<EOF
created_utc=$name
host=$(hostname)
trips=$trips
annotations=$annotations
events=$events
monitor=$monitor
EOF
    mv "$work" "$SNAPSHOTS/$name"
    current=$name
    log "Created snapshot $name ($trips trips, $annotations annotations, events=$events, monitor=$monitor)"
fi
ln -sfn "snapshots/$current" "$BACKUP_DIR/latest"

# Keep the newest KEEP_RECENT, plus the first snapshot of each of the
# newest KEEP_MONTHLY months. The current snapshot is always among the
# newest, so it's never pruned.
list_snapshots | awk -v recent="$KEEP_RECENT" -v monthly="$KEEP_MONTHLY" '
    { name[NR] = $0; month = substr($0, 1, 7)
      if (!(month in seen)) { seen[month] = 1; firsts[++months] = NR } }
    END {
        for (i = NR - recent + 1; i <= NR; i++) if (i >= 1) keep[i] = 1
        for (j = months - monthly + 1; j <= months; j++) if (j >= 1) keep[firsts[j]] = 1
        for (i = 1; i <= NR; i++) if (!(i in keep)) print name[i]
    }' | while read -r old; do
    log "Pruning local snapshot $old"
    rm -rf "${SNAPSHOTS:?}/$old"
done

# --- 4. Off-host copy --------------------------------------------------

log "Copying snapshots to $REMOTE"
delete=()
[[ $REMOTE_PRUNE == yes ]] && delete=(--delete)
# -H keeps the hard links between snapshots, so the remote copy is as
# small as the local one.
rsync_remote -aH "${delete[@]}" "$SNAPSHOTS/" "$REMOTE/snapshots/"

log "Checking the off-host copy of $current"
diffs=$(rsync_remote -anc --delete --itemize-changes \
    "$SNAPSHOTS/$current/" "$REMOTE/snapshots/$current/")
[[ -z $diffs ]] || die "off-host copy of $current doesn't match:
$diffs"

date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"
log "Backup complete: $current is on $REMOTE"

if [[ -n $PING_URL ]]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$PING_URL" \
        || log "Warning: backup succeeded but the ping to PING_URL failed"
fi
