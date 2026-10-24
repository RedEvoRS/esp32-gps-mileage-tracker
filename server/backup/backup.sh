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
#   4. rsyncs every snapshot to REMOTE (only if REMOTE holds the marker
#      file, so an unmounted share can't pass for the off-host copy),
#      re-reads the newest copy there to confirm it matches byte for byte,
#      and, with REMOTE_PRUNE=yes, applies the same retention to REMOTE.
#
# Layout under BACKUP_DIR (the same under REMOTE, minus `latest`):
#   snapshots/2026-10-06T033012Z/{annotations.db,events.db?,monitor.db?,data/,SHA256SUMS,INFO}
#   latest -> snapshots/<newest>
#   last-success
# REMOTE also holds .mileage-backup-target.
#
# `backup.sh --mark-target` writes the marker file (.mileage-backup-target)
# the destination needs before a backup will copy to it, and
# `backup.sh --check-target` says whether it is there (exit 0 or 1).
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
guard_backup_mounts

case ${1:-} in
    --mark-target)
        mark_target || die "couldn't write $TARGET_MARKER to $REMOTE"
        log "$REMOTE is now the backup destination ($TARGET_MARKER written)"
        exit 0 ;;
    --check-target)
        if target_marked; then exit 0; else exit 1; fi ;;
    '') ;;
    *) die "unknown option $1 (backup.sh takes --mark-target or --check-target)" ;;
esac

[[ -d $COMPOSE_DIR/data ]] || die "no trip data directory at $COMPOSE_DIR/data"

mkdir -p "$SNAPSHOTS"
exec 9> "$BACKUP_DIR/.lock"
# 75 lets the scheduled catch-up distinguish contention from a failed backup.
flock -n 9 || { log "another backup is already running"; exit 75; }

name=$(date -u +%Y-%m-%dT%H%M%SZ)
work=$SNAPSHOTS/.partial-$name
rm -rf "$SNAPSHOTS"/.partial-*        # left behind by a run that died
# restore.sh's scratch folders, left behind if a restore test was killed
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -name '.restore-scratch-*' -mmin +1440 \
    -exec rm -rf {} + 2> /dev/null || true
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
elif [[ $MONITOR_PRESENT == unknown ]]; then
    monitor=unknown
    log "WARNING: could not determine whether the monitor is installed ($MONITOR_DISCOVERY_ERROR); skipping monitor.db"
else
    log "Monitor is not installed; skipping monitor.db"
fi

# --- 2. Trip files -----------------------------------------------------

log "Copying trip files"
link=()
[[ -n $prev && -d $SNAPSHOTS/$prev/data ]] && link=(--link-dest="$SNAPSHOTS/$prev/data")
# Ingest holds this lock for the whole write of a trip's GPX + JSON.
open_upload_lock
flock -w 300 7
rsync -a --numeric-ids --exclude=/.upload.lock --exclude='/.upload-lock.*' --exclude='*.tmp' \
        --exclude=/diagnostics/events.db --exclude='/diagnostics/events.db-*' "${link[@]}" \
        "$COMPOSE_DIR/data/" "$work/data/"
exec 7<&-
trips=$(find "$work/data" -type f -name '*.gpx' | wc -l)

# --- 3. Manifest, dedupe, prune ----------------------------------------

(cd "$work" && find . -type f ! -name SHA256SUMS -print0 \
    | LC_ALL=C sort -z | xargs -0 -r sha256sum) > "$work/SHA256SUMS"

# Preserve changes in discovery status even if the database/file bytes match.
if [[ -n $prev ]] && cmp -s "$work/SHA256SUMS" "$SNAPSHOTS/$prev/SHA256SUMS" \
        && grep -Fxq "monitor=$monitor" "$SNAPSHOTS/$prev/INFO"; then
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
list_snapshots | retention_drops | while read -r old; do
    log "Pruning local snapshot $old"
    rm -rf "${SNAPSHOTS:?}/$old"
done

# --- 4. Off-host copy --------------------------------------------------

if ! target_marked; then
    die "$REMOTE has no $TARGET_MARKER file, so this backup was not copied off this machine.
  That file marks the real backup destination. If the destination is a mounted share or
  disk, it probably isn't mounted right now (otherwise the copy would land on this
  machine's own disk). Mount it, or fix REMOTE in $CONF, and run this again.
  (Or $REMOTE can't be reached at all: a NAS that is down, a wrong address.)
  If $REMOTE is the right place and is reachable, mark it once with:
      bash $SCRIPT_DIR/backup.sh --mark-target"
fi

log "Copying snapshots to $REMOTE"
# No --delete: pruning the off-host copy is done by name, below.
# -H keeps the hard links between snapshots, so the remote copy is as
# small as the local one.
rsync_remote -aH --numeric-ids "$SNAPSHOTS/" "$REMOTE/snapshots/"

log "Checking the off-host copy of $current"
diffs=$(rsync_remote -anc --numeric-ids --delete --itemize-changes \
    "$SNAPSHOTS/$current/" "$REMOTE/snapshots/$current/")
[[ -z $diffs ]] || die "off-host copy of $current doesn't match:
$diffs"

# REMOTE_PRUNE=yes: apply the same retention rule to the off-host snapshots,
# on their own list of names. Only the names the rule drops are deleted.
# The remote is never made to mirror the local folder: a rebuilt server
# starts with few or no local snapshots, and the history on the remote
# must survive that.
prune_remote() {
    local names drops history_only newer empty old
    if ! names=$(list_remote_snapshots); then
        log "WARNING: couldn't list the snapshots on $REMOTE; not pruning it"
        return 0
    fi
    # Another writer, or a clock that went backwards, would make "newest"
    # mean something else here.
    newer=$(awk -v current="$current" '$0 > current' <<< "$names" | head -n 1)
    if [[ -n $newer ]]; then
        log "WARNING: not pruning $REMOTE: it has a snapshot ($newer) newer than this server's newest ($current)."
        log "WARNING: Is another server backing up to the same place, or did this server's clock go back? Nothing was deleted there."
        return 0
    fi
    drops=$(retention_drops <<< "$names")
    # What the rule would keep that this server doesn't have: history that
    # exists only off-host, as after a rebuild or a changed BACKUP_DIR.
    history_only=$(comm -23 <(comm -23 <(sort <<< "$names") <(sort <<< "$drops")) <(list_snapshots | sort))
    if [[ -n $history_only ]]; then
        log "WARNING: not pruning $REMOTE: it holds $(wc -l <<< "$history_only") snapshot(s) that this server doesn't (first: $(head -n 1 <<< "$history_only"))."
        log "WARNING: That looks like a rebuilt server or a changed BACKUP_DIR. Nothing was deleted there."
        log "WARNING: Off-host pruning stays off until this server has them again (copy them back with:"
        log "WARNING:   rsync -aH $REMOTE/snapshots/ $SNAPSHOTS/ ), or set REMOTE_PRUNE=no in $CONF."
        return 0
    fi
    [[ -n $drops ]] || return 0
    empty=$(mktemp -d "$BACKUP_DIR/.empty.XXXXXX")
    while read -r old; do
        [[ $old < $current ]] || continue            # never the snapshot just made
        log "Pruning off-host snapshot $old"
        # rsync deletes the one named folder because the empty source lacks
        # it; every other name is excluded, so it stays. Works the same
        # for a path and for user@host:/path.
        rsync_remote -r --delete --include="/$old/***" --exclude='*' \
            "$empty/" "$REMOTE/snapshots/" < /dev/null \
            || { log "WARNING: couldn't delete $old from $REMOTE"; break; }
    done <<< "$drops"
    rmdir "$empty"
}
if [[ $REMOTE_PRUNE == yes ]]; then
    prune_remote
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"
log "Backup complete: $current is on $REMOTE"

if [[ -n $PING_URL ]]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$PING_URL" \
        || log "Warning: backup succeeded but the ping to PING_URL failed"
fi
