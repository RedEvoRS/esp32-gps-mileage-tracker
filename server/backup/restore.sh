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
#       Puts a snapshot back. Runs the same checks first, stops both
#       services, saves the current database and any trip files it would
#       replace under BACKUP_DIR/pre-restore-<time>/, restores, and starts
#       the services again. Trip files that aren't in the snapshot are
#       left alone; nothing is deleted.
#
# SNAPSHOT is one of:
#   (nothing) or latest      the newest snapshot on REMOTE
#   2026-10-06T033012Z       that snapshot on REMOTE
#   a path or host:path      any snapshot directory, local or remote

set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

usage() {
    sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}" >&2
    exit 2
}

resolve_snapshot() {
    local arg=${1:-latest}
    if [[ $arg == latest || $arg =~ $SNAPSHOT_RE ]]; then
        [[ -n $REMOTE ]] || die "REMOTE is not set in $CONF; give a snapshot path instead"
        if [[ $arg == latest ]]; then
            arg=$(rsync_remote --list-only "$REMOTE/snapshots/" \
                | awk '{ print $NF }' | grep -E "$SNAPSHOT_RE" | LC_ALL=C sort | tail -n 1 || true)
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
    rsync_remote -a "$source/" "$scratch/snapshot/"
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
    viewer_python "$PY_CHECK_RESTORE" < "$snap/annotations.db" >&2 \
        || die "the snapshot's database failed its check"

    local gpx json
    gpx=$(find "$snap/data" -type f -name '*.gpx' | wc -l)
    json=$(find "$snap/data" -type f -name '*.json' | wc -l)
    log "Snapshot holds $gpx trip GPX files and $json summaries"
    [[ -f $snap/INFO ]] && log "Snapshot INFO: $(tr '\n' ' ' < "$snap/INFO")"
}

cleanup() {
    local status=$?
    if [[ ${services_stopped:-no} == yes ]]; then
        log "Restore did not finish; starting the services again"
        compose up -d >&2 || log "Could not start the services; run: docker compose up -d"
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

scratch=$(mktemp -d "${TMPDIR:-/tmp}/mileage-restore.XXXXXX")
trap cleanup EXIT

fetch_and_check "$snapshot_arg"

if [[ $command == check ]]; then
    log "Restore test passed"
    exit 0
fi

if [[ $yes != yes ]]; then
    [[ -t 0 ]] || die "not a terminal; pass --yes to restore without asking"
    printf 'This replaces the live annotations database and restores trip files\nin %s/data. Type RESTORE to continue: ' "$COMPOSE_DIR" >&2
    read -r answer
    [[ $answer == RESTORE ]] || die "cancelled"
fi

pre=$BACKUP_DIR/pre-restore-$(date -u +%Y-%m-%dT%H%M%SZ)
mkdir -p "$pre" "$COMPOSE_DIR/data"

log "Stopping the services"
services_stopped=yes
compose stop mileage-ingest mileage-viewer >&2

log "Saving the current database to $pre"
if viewer_python "$PY_SNAPSHOT" /db/annotations.db > "$pre/annotations.db" 2> "$pre/snapshot.log"; then
    rm "$pre/snapshot.log"
else
    status=$?
    if [[ $status -eq 3 ]]; then
        log "No current database to save"
        rm "$pre/annotations.db" "$pre/snapshot.log"
    else
        cat "$pre/snapshot.log" >&2
        die "could not save the current database, so nothing was restored"
    fi
fi

log "Restoring the database"
viewer_python "$PY_CHECK_RESTORE" /db/annotations.db < "$scratch/snapshot/annotations.db" >&2

log "Restoring trip files (anything replaced goes to $pre/data)"
rsync -a --backup --backup-dir="$pre/data" --itemize-changes \
    "$scratch/snapshot/data/" "$COMPOSE_DIR/data/" >&2

log "Starting the services again"
compose up -d >&2
services_stopped=no

log "Restore complete. Check the viewer, then delete $pre once you're happy."
