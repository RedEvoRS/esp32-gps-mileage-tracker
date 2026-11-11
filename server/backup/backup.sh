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
#   5. When due, deep-checks older local and off-host snapshots. A failure
#      fails the run only after today's backup has completed and been copied.
#
# Layout under BACKUP_DIR (the same under REMOTE, minus `latest`):
#   snapshots/2026-10-06T033012Z/{annotations.db,events.db?,monitor.db?,data/,SHA256SUMS,INFO}
#   latest -> snapshots/<newest>
#   last-success
#   verify-state (local only: last deep check, result, cursor, retry names)
# REMOTE also holds .mileage-backup-target.
#
# `backup.sh --mark-target` writes the marker file (.mileage-backup-target)
# the destination needs before a backup will copy to it, and
# `backup.sh --check-target` says whether it is there (exit 0 or 1).
# `backup.sh --verify` makes a normal backup, then forces a deep check,
# including when periodic verification is disabled.
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
validate_verify_settings
require_commands rsync flock sha256sum cmp
guard_backup_mounts

force_verify=no
case ${1:-} in
    --mark-target)
        mark_target || die "couldn't write $TARGET_MARKER to $REMOTE"
        log "$REMOTE is now the backup destination ($TARGET_MARKER written)"
        exit 0 ;;
    --check-target)
        if target_marked; then exit 0; else exit 1; fi ;;
    --verify) force_verify=yes ;;
    '') ;;
    *) die "unknown option $1 (backup.sh takes --mark-target, --check-target or --verify)" ;;
esac

[[ -d $COMPOSE_DIR/data ]] || die "no trip data directory at $COMPOSE_DIR/data"

mkdir -p "$SNAPSHOTS"
exec 9> "$BACKUP_DIR/.lock"
# 75 lets the scheduled catch-up distinguish contention from a failed backup.
flock -n 9 || { log "another backup is already running"; exit 75; }
read_verify_state

# A failed/interrupted deep check retries the same snapshots before retention
# can discard them. No pins apply when verification is explicitly disabled.
verify_pinned() {
    [[ $force_verify == yes ]] || (( BACKUP_VERIFY_DAYS != 0 )) || return 1
    [[ " $VERIFY_PENDING $VERIFY_REMOTE_PENDING " == *" $1 "* ]]
}

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
    if verify_pinned "$old"; then continue; fi
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
        if verify_pinned "$old"; then continue; fi
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

log "Backup complete: $current is on $REMOTE"

# --- 5. Periodic deep verification -------------------------------------

# Subshell keeps cleanup/traps local. Every fallible operation is explicitly
# checked: the caller captures failure so today's copied backup is preserved.
deep_verify() (
    local now epoch names oldest old next_cursor failed=0 remote_names remote_oldest
    local scratch='' selected='' failed_names='' failed_remote='' remote_checks='' checks
    [[ $force_verify == yes ]] || (( BACKUP_VERIFY_DAYS != 0 )) || return 0
    now=$(date -u +%s) || return 1
    epoch=$(date -u -d "$VERIFY_TIME" +%s 2>/dev/null) || epoch=0
    if [[ $force_verify != yes && $VERIFY_RESULT == passed && -n $VERIFY_TIME ]] \
            && (( epoch > 0 && now >= epoch && now - epoch < BACKUP_VERIFY_DAYS * 86400 )); then
        log "Deep check not due (last passed: $VERIFY_TIME)"
        return 0
    fi

    names=$(list_snapshots | grep -Fxv "$current" || true)
    oldest=$(head -n 1 <<< "$names")
    next_cursor=$VERIFY_CURSOR
    if [[ -n $oldest ]]; then selected=$oldest; fi
    checks=$(( ${#selected} > 0 ? 1 : 0 ))
    # Retry failures first, then advance by UTC name and wrap once. The
    # cursor need not still exist, unlike an array index after pruning.
    while read -r old; do
        [[ -n $old && $old != "$oldest" && $old != "$current" && -d $SNAPSHOTS/$old ]] || continue
        [[ " $selected " != *" $old "* ]] || continue
        (( checks < BACKUP_VERIFY_COUNT )) || break
        selected+=" $old"
        checks=$((checks + 1))
        next_cursor=$old
    done < <(tr ' ' '\n' <<< "$VERIFY_PENDING"
        awk -v cursor="$VERIFY_CURSOR" '$0 > cursor' <<< "$names"
        awk -v cursor="$VERIFY_CURSOR" 'NF && $0 <= cursor' <<< "$names")

    # List REMOTE with rsync, never ssh shell commands. Retry a previously
    # failing remote-only snapshot even if another name is now oldest.
    remote_checks=$VERIFY_REMOTE_PENDING
    if remote_names=$(list_remote_snapshots); then
        remote_oldest=$(head -n 1 <<< "$remote_names")
        if [[ -n $remote_oldest && $remote_oldest != "$current" && ! -d $SNAPSHOTS/$remote_oldest \
                && " $remote_checks " != *" $remote_oldest "* ]]; then
            remote_checks+="$remote_oldest "
        fi
    else
        log "Deep check FAILED remote: cannot list snapshots"
        failed=1
    fi
    VERIFY_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) || return 1
    VERIFY_RESULT=running
    VERIFY_PENDING=$selected
    VERIFY_REMOTE_PENDING=$remote_checks
    write_verify_state || { log "Deep check FAILED: cannot write verify-state"; return 1; }
    # Run even with no older local snapshots: REMOTE may retain older history.
    # Scratch stays on the backup disk, outside snapshots and the copied tree.
    trap '[[ -z $scratch ]] || rm -rf -- "$scratch"' EXIT
    trap 'exit 1' HUP INT TERM
    # Only this backup lock owns verification scratch. Even leftover cleanup
    # errors are deferred until after today's backup has been copied.
    rm -rf "$BACKUP_DIR"/.verify-scratch-* || return 1
    for old in $selected; do
        log "Deep check local $old"
        if ! verify_snapshot "$SNAPSHOTS/$old"; then
            log "Deep check FAILED local $old"
            failed=1
            failed_names+="$old "
            continue
        fi
        log "Deep check remote $old against verified local copy"
        if diffs=$(rsync_remote -anc --numeric-ids --delete --itemize-changes \
                "$SNAPSHOTS/$old/" "$REMOTE/snapshots/$old/") && [[ -z $diffs ]]; then
            :
        else
            log "Deep check FAILED remote $old (differences/paths below)"
            [[ -z $diffs ]] || printf '%s\n' "$diffs" >&2
            failed=1
            failed_names+="$old "
        fi
    done
    for old in $remote_checks; do
        log "Deep check remote-only $old (fetching into scratch)"
        scratch=$(mktemp -d "$BACKUP_DIR/.verify-scratch-XXXXXX") || return 1
        if rsync_remote -a --numeric-ids "$REMOTE/snapshots/$old/" "$scratch/snapshot/" < /dev/null \
                && verify_snapshot "$scratch/snapshot"; then
            :
        else
            log "Deep check FAILED remote-only $old"
            failed=1
            failed_remote+="$old "
        fi
        rm -rf -- "$scratch" || return 1
        scratch=
    done
    VERIFY_RESULT=failed
    if (( failed == 0 )); then VERIFY_RESULT=passed; VERIFY_CURSOR=$next_cursor; fi
    VERIFY_PENDING=$failed_names
    VERIFY_REMOTE_PENDING=$failed_remote
    write_verify_state || { log "Deep check FAILED: cannot write verify-state"; return 1; }
    log "Deep check $VERIFY_RESULT"
    return "$failed"
)

deep_verify || die "Deep check failed; today's backup was copied. Check the failed snapshots and rerun backup.sh."

# A deep failure must also withhold the success timestamp and heartbeat, so
# both existing reporting paths treat this as a failed run.
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/last-success"
if [[ -n $PING_URL ]]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$PING_URL" \
        || log "Warning: backup succeeded but the ping to PING_URL failed"
fi
