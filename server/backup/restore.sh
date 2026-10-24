#!/usr/bin/env bash
# Test or perform a restore from a backup made by backup.sh.
#
#   restore.sh check [SNAPSHOT]
#       Restore test. Copies the snapshot into a scratch directory,
#       verifies every file against SHA256SUMS, and opens the database
#       to check its integrity and count the annotations. Touches
#       nothing live. Run it after setting up backups and before each
#       filing.
#
#   restore.sh restore SNAPSHOT [--yes]
#       Puts a snapshot back. Runs the same checks first, stops the database-writing
#       services, saves the current databases and any trip files it would
#       replace under BACKUP_DIR/pre-restore-<time>/, restores, and starts
#       the services again. Trip files that aren't in the snapshot are
#       left alone; nothing is deleted.
#
# SNAPSHOT is one of:
#   (nothing) or latest      the newest snapshot on REMOTE that holds trips
#                            (an empty one, such as the installer's first
#                            backup of a rebuilt server, is skipped while
#                            older ones exist)
#   2026-10-06T033012Z       that snapshot on REMOTE
#   a path or host:path      any snapshot directory, local or remote

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
guard_backup_mounts

usage() {
    sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}" >&2
    exit 2
}

# The newest snapshot on REMOTE, except that one with no trips is skipped
# when an older one has some. A rebuilt server's first backup is empty and
# is newer than the history it is about to be restored from.
newest_snapshot_with_trips() {
    local names name trips newest="" info
    names=$(rsync_remote --list-only "$REMOTE/snapshots/" \
        | awk '{ print $NF }' | grep -E "$SNAPSHOT_RE" | LC_ALL=C sort -r || true)
    while read -r name; do
        [[ -n $name ]] || continue
        [[ -n $newest ]] || newest=$name
        # Fresh file each time: rsync skips a copy whose size and modification
        # time match the previous INFO, and INFO files of neighbouring
        # snapshots often do. The ssh must not eat the rest of the name list.
        rm -f "$scratch/INFO"
        info=$(rsync_remote -q "$REMOTE/snapshots/$name/INFO" "$scratch/INFO" < /dev/null > /dev/null 2>&1 \
            && cat "$scratch/INFO" || true)
        trips=$(sed -n 's/^trips=//p' <<< "$info")
        if [[ $trips != 0 ]]; then
            [[ $name == "$newest" ]] || log "Skipping $newest and newer: no trips in them; restoring $name instead (give a snapshot name to choose another)"
            printf '%s\n' "$name"
            return 0
        fi
    done <<< "$names"
    printf '%s\n' "$newest"
}

resolve_snapshot() {
    local arg=${1:-latest}
    if [[ $arg == latest || $arg =~ $SNAPSHOT_RE ]]; then
        [[ -n $REMOTE ]] || die "REMOTE is not set in $CONF; give a snapshot path instead"
        if [[ $arg == latest ]]; then
            arg=$(newest_snapshot_with_trips)
            [[ -n $arg ]] || die "no snapshots found on $REMOTE"
        fi
        printf '%s\n' "$REMOTE/snapshots/$arg"
    else
        printf '%s\n' "${arg%/}"
    fi
}

# Fetch and verify a snapshot into $scratch/snapshot.
fetch_and_check() {
    local source
    source=$(resolve_snapshot "${1:-}")
    log "Fetching $source"
    rsync_remote -a --numeric-ids "$source/" "$scratch/snapshot/"
    local snap=$scratch/snapshot
    [[ -f $snap/SHA256SUMS && -f $snap/annotations.db && -d $snap/data ]] \
        || die "$source doesn't look like a backup snapshot"

    log "Verifying checksums"
    (cd "$snap" && sha256sum --quiet --strict -c SHA256SUMS) \
        || die "checksums don't match; this snapshot is damaged"
    # Every file must be listed, so nothing can be slipped in unverified.
    local listed actual
    listed=$(sed -E 's/^[0-9a-f]{64} [ *]//' "$snap/SHA256SUMS" | LC_ALL=C sort)
    actual=$(cd "$snap" && find . -type f ! -name SHA256SUMS ! -name INFO | LC_ALL=C sort)
    [[ $listed == "$actual" ]] || die "snapshot has files missing from or not in SHA256SUMS"

    log "Checking the database"
    viewer_python "$PY_CHECK_RESTORE" annotations < "$snap/annotations.db" >&2 \
        || die "the snapshot's database failed its check"

    local kind
    for kind in events monitor; do
        if [[ -f $snap/$kind.db ]]; then
            viewer_python "$PY_CHECK_RESTORE" "$kind" < "$snap/$kind.db" >&2 \
                || die "the snapshot's $kind database failed its check"
        else
            log "No $kind.db in this snapshot (older backup or database absent); leaving live database unchanged"
        fi
    done

    local gpx json
    gpx=$(find "$snap/data" -type f -name '*.gpx' | wc -l)
    json=$(find "$snap/data" -type f -name '*.json' | wc -l)
    log "Snapshot holds $gpx trip GPX files and $json summaries"
    [[ -f $snap/INFO ]] && log "Snapshot INFO: $(tr '\n' ' ' < "$snap/INFO")"
}

start_services() {
    if [[ ${MONITOR_PRESENT:-unknown} == unknown ]]; then
        # COMPOSE_PROFILES may enable monitor by default; leave it untouched
        # when discovery failed, including during error cleanup.
        compose up -d mileage-ingest mileage-viewer >&2 || return
    else
        compose up -d >&2 || return
    fi
    if [[ ${monitor_running:-no} == yes ]]; then
        compose --profile monitor start mileage-monitor >&2 || return
    fi
}

cleanup() {
    local status=$?
    if [[ ${services_stopped:-no} == yes ]]; then
        log "Restore did not finish; starting the services again"
        start_services || log "Could not start the services; check docker compose and the monitor profile"
    fi
    rm -rf "${scratch:-}"
    exit "$status"
}

command=${1:-}
[[ $command == check || $command == restore ]] || usage
shift
snapshot_arg=
yes=no
for arg in "$@"; do
    case $arg in
        --yes) yes=yes ;;
        -*) usage ;;
        *) [[ -z $snapshot_arg ]] || usage; snapshot_arg=$arg ;;
    esac
done
[[ $command == check || -n $snapshot_arg ]] || die "say which snapshot to restore (latest, a name, or a path)"
require_commands rsync sha256sum

# The snapshot, trip files included, is copied here. Debian 13 keeps /tmp
# in RAM, so by default this is next to the snapshots (same disk, mode 700).
# TMPDIR overrides it. backup.sh clears these after a day if one is left
# behind by a restore that was killed.
if [[ -n ${TMPDIR:-} ]]; then
    scratch=$(mktemp -d "$TMPDIR/mileage-restore.XXXXXX")
else
    mkdir -p "$BACKUP_DIR"
    scratch=$(mktemp -d "$BACKUP_DIR/.restore-scratch-XXXXXX")
fi
trap cleanup EXIT

fetch_and_check "$snapshot_arg"

if [[ $command == check ]]; then
    log "Restore test passed"
    exit 0
fi

if [[ $yes != yes ]]; then
    [[ -t 0 ]] || die "not a terminal; pass --yes to restore without asking"
    printf 'This replaces the live databases present in the snapshot and restores trip files\nin %s/data. Type RESTORE to continue: ' "$COMPOSE_DIR" >&2
    read -r answer
    [[ $answer == RESTORE ]] || die "cancelled"
fi

pre=$BACKUP_DIR/pre-restore-$(date -u +%Y-%m-%dT%H%M%SZ)
[[ ! -L $COMPOSE_DIR/data ]] || die "trip data directory must not be a symlink"
data_owner=1000:1000
[[ ! -d $COMPOSE_DIR/data ]] || data_owner=$(stat -c %u:%g "$COMPOSE_DIR/data")
mkdir -p "$pre" "$COMPOSE_DIR/data"
trip_owner=()
if (( EUID == 0 )); then
    chown "$data_owner" "$COMPOSE_DIR/data"
    chmod 700 "$COMPOSE_DIR/data"
    trip_owner=(--chown="$data_owner" '--chmod=D700,F600')
fi

find_monitor_volume
monitor_running=no
if [[ $MONITOR_PRESENT == yes ]]; then
    running=$(compose --profile monitor ps --status running --quiet mileage-monitor) \
        || die "could not inspect monitor service state"
    [[ -z $running ]] || monitor_running=yes
elif [[ $MONITOR_PRESENT == unknown ]]; then
    log "WARNING: could not determine whether the monitor is installed ($MONITOR_DISCOVERY_ERROR); skipping monitor.db and leaving the monitor running state unchanged." \
        "To restore it by hand, fix discovery and rerun this restore, or stop mileage-monitor, save its current /state/monitor.db," \
        "restore the checked snapshot monitor.db with SQLite's backup API, then start it only if previously running (DEPLOY_DEBIAN.md Step 10)."
fi

log "Stopping the services"
services_stopped=yes
compose stop mileage-ingest mileage-viewer >&2
if [[ $MONITOR_PRESENT == yes ]]; then
    compose --profile monitor stop mileage-monitor >&2
fi

# Save every current database before replacing any of them. The stopped
# ingest helper has /data writable; the viewer's /data is read-only.
# A current database that SQLite can't read (damaged: a main reason to
# restore) can't be snapshotted; its raw files are saved instead, and it
# is cleared out of the way just before its replacement goes in.
log "Saving the current databases to $pre"
unreadable=()
save_current_db() {
    local runner=$1 path=$2 dest=$3
    snapshot_db "$runner" "$path" "$dest" yes yes
    [[ $SNAPSHOT_REPORT != unreadable ]] || unreadable+=("$path")
}
# Restore one database over the live one, clearing a damaged one first.
restore_db() {
    local runner=$1 kind=$2 path=$3 file=$4 damaged
    for damaged in ${unreadable[@]+"${unreadable[@]}"}; do
        if [[ $damaged == "$path" ]]; then remove_raw_db "$runner" "$path"; fi
    done
    "$runner" "$PY_CHECK_RESTORE" "$kind" "$path" < "$file" >&2
}
save_current_db viewer_python /db/annotations.db "$pre/annotations.db"
save_current_db ingest_python /data/diagnostics/events.db "$pre/events.db"
if [[ $MONITOR_PRESENT == yes ]]; then
    save_current_db monitor_python /state/monitor.db "$pre/monitor.db"
fi

log "Restoring the annotations database"
restore_db viewer_python annotations /db/annotations.db "$scratch/snapshot/annotations.db"
if [[ -f $scratch/snapshot/events.db ]]; then
    log "Restoring the diagnostics database"
    restore_db ingest_python events /data/diagnostics/events.db "$scratch/snapshot/events.db"
fi
if [[ $MONITOR_PRESENT != unknown && -f $scratch/snapshot/monitor.db ]]; then
    # This may create the volume on a replacement server, without starting
    # the monitor or enabling notifications. Its image supplies UID ownership.
    log "Restoring the monitor database"
    restore_db monitor_python monitor /state/monitor.db "$scratch/snapshot/monitor.db"
fi

log "Restoring trip files (anything replaced goes to $pre/data)"
# Old backups may contain the unsafe live diagnostics copy. Never replay it
# (or its journal) through rsync; only restore the checked SQLite snapshot.
rsync -a --numeric-ids "${trip_owner[@]}" --backup --backup-dir="$pre/data" --itemize-changes \
    --exclude=/.upload.lock --exclude='/.upload-lock.*' \
    --exclude=/diagnostics/events.db --exclude='/diagnostics/events.db-*' \
    "$scratch/snapshot/data/" "$COMPOSE_DIR/data/" >&2

log "Starting the services again"
start_services
services_stopped=no

if (( ${#unreadable[@]} )); then
    log "WARNING: ${#unreadable[@]} damaged database(s) were replaced: ${unreadable[*]}"
    log "WARNING: their raw files are kept in $pre (the *-unreadable folders)."
fi
log "Restore complete. Check the viewer, then delete $pre once you're happy."
