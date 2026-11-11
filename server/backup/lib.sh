# shellcheck shell=bash disable=SC2034  # its variables are used by the scripts sourcing it
# Shared by backup.sh and restore.sh. Sourced, not run.
#
# Expects SCRIPT_DIR to be set by the caller. Reads backup.conf (or
# $MILEAGE_BACKUP_CONF) and sets defaults for anything it leaves out.

log() {
    # Under systemd, journald timestamps every line already.
    if [[ -n ${JOURNAL_STREAM:-} ]]; then
        printf '%s\n' "$*" >&2
    else
        printf '%s %s\n' "$(date '+%F %T')" "$*" >&2
    fi
}

die() {
    log "ERROR: $*"
    exit 1
}

is_unraid() {
    case ${MILEAGE_PLATFORM:-} in
        unraid) return 0 ;;
        debian) return 1 ;;
    esac
    local f=${MILEAGE_UNRAID_VERSION_FILE:-/etc/unraid-version}
    [[ -r $f ]] && grep -Eq '^version="?[0-9]' "$f"
}

CONF=${MILEAGE_BACKUP_CONF:-$SCRIPT_DIR/backup.conf}
[[ -f $CONF ]] || die "no config at $CONF (copy backup.conf.example to backup.conf)"
if (( EUID == 0 )); then
    conf_owner=$(stat -Lc %u "$CONF")
    conf_mode=$(stat -Lc %a "$CONF")
    if [[ $conf_owner != 0 ]] || (( (8#$conf_mode & 0022) != 0 )); then
        die "refusing to source $CONF as root: backup.conf must be root-owned and not group/world-writable (use mode 600 or 644)"
    fi
fi
# shellcheck source=backup.conf.example
. "$CONF"

COMPOSE_DIR=${COMPOSE_DIR:-$(dirname "$SCRIPT_DIR")}
if is_unraid && [[ -z ${BACKUP_DIR:-} ]]; then
    die "BACKUP_DIR must be set in backup.conf on Unraid; choose persistent storage under /mnt"
fi
BACKUP_DIR=${BACKUP_DIR:-$HOME/mileage-backups}
BACKUP_MOUNT=${BACKUP_MOUNT:-}
REMOTE_MOUNT=${REMOTE_MOUNT:-}
REMOTE=${REMOTE:-}
RSYNC_SSH=${RSYNC_SSH:-}
KEEP_RECENT=${KEEP_RECENT:-14}
KEEP_MONTHLY=${KEEP_MONTHLY:-84}
REMOTE_PRUNE=${REMOTE_PRUNE:-no}
PING_URL=${PING_URL:-}
BACKUP_VERIFY_DAYS=${BACKUP_VERIFY_DAYS-7}
BACKUP_VERIFY_COUNT=${BACKUP_VERIFY_COUNT-3}

SNAPSHOTS=$BACKUP_DIR/snapshots
# Snapshot directories are named by UTC creation time, so they sort
# chronologically: 2026-10-06T033012Z
SNAPSHOT_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$'

# Tests swap in a fake; on the server this is plain `docker compose`.
read -r -a COMPOSE <<< "${MILEAGE_COMPOSE:-docker compose}"

compose() {
    (cd "$COMPOSE_DIR" && "${COMPOSE[@]}" "$@")
}

# Check before mkdir or rsync: a missing share can leave its empty folder
# on the host's root filesystem. Resolve a new folder via its existing parent.
guard_mount() {
    local path=$1 mount=$2 label=$3 target probe
    [[ -n $mount ]] || return 0
    findmnt -n --mountpoint "$mount" > /dev/null 2>&1 \
        || die "$label mount is not mounted; refusing backup/restore"
    probe=$(realpath -m -- "$path") || die "cannot resolve $label path"
    while [[ ! -e $probe ]]; do
        [[ $probe != / && $probe != . ]] || break
        probe=$(dirname -- "$probe")
    done
    target=$(findmnt -n -o TARGET --target "$probe" 2>/dev/null) \
        || die "cannot resolve $label onto its recorded mount"
    [[ $(readlink -f -- "$target") == "$(readlink -f -- "$mount")" ]] \
        || die "$label is not on its recorded mount; refusing backup/restore"
}

guard_backup_mounts() {
    guard_mount "$BACKUP_DIR" "$BACKUP_MOUNT" BACKUP_DIR
    if [[ -n $REMOTE && $REMOTE != *:* ]]; then
        guard_mount "$REMOTE" "$REMOTE_MOUNT" REMOTE
    fi
}

# Publish a new lock only after it belongs to the data owner. A concurrent
# ingest may create it first; link never replaces that inode. Open read-only
# afterwards, so root cannot accidentally create a root-owned upload lock.
open_upload_lock() {
    local data=$COMPOSE_DIR/data tmp owner
    [[ ! -L $data && ! -L $data/.upload.lock ]] || die "trip data and its upload lock must not be symlinks"
    if [[ ! -e $data/.upload.lock ]]; then
        owner=$(stat -c %u:%g "$data")
        tmp=$(mktemp "$data/.upload-lock.XXXXXX")
        if (( EUID == 0 )); then
            chown "$owner" "$tmp" || { rm -f "$tmp"; die "could not set upload lock ownership"; }
        fi
        chmod 600 "$tmp"
        ln -T "$tmp" "$data/.upload.lock" 2>/dev/null || {
            rm -f "$tmp"
            [[ -f $data/.upload.lock && ! -L $data/.upload.lock ]] || die "could not create upload lock"
        }
        rm -f "$tmp"
    fi
    exec 7< "$data/.upload.lock"
}

# Run a Python snippet in a throwaway viewer container. It mounts the
# viewer-db volume and runs as the image's app user, so it can read and
# write /db without caring which UID owns things on the host. Files move
# in and out over stdin/stdout rather than bind mounts for the same
# reason. Works whether or not the viewer itself is running.
viewer_python() {
    local script=$1
    shift
    compose run --rm --no-deps -T mileage-viewer python -c "$script" "$@"
}

ingest_python() {
    local script=$1
    shift
    compose run --rm --no-deps -T mileage-ingest python -c "$script" "$@"
}

monitor_python() {
    local script=$1
    shift
    compose run --rm --no-deps -T mileage-monitor python -c "$script" "$@"
}

# Use Compose's resolved project name and volume labels, not a guessed volume
# name or recent config flags. JSON stays in the pipe; discard command/parser
# errors because config/env_file diagnostics can contain service credentials.
# Unknown discovery must never abort a backup or be mistaken for absence.
# Backup must not run a monitor helper until its volume exists: run creates it.
find_monitor_volume() {
    local project volumes
    MONITOR_PRESENT=unknown
    MONITOR_DISCOVERY_ERROR=
    if ! project=$({ compose config --format json | viewer_python '
import json, re, sys
name = json.load(sys.stdin)["name"]
if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
    sys.exit(1)
print(name)
'; } 2>/dev/null); then
        MONITOR_DISCOVERY_ERROR="Compose project config failed or returned an invalid name"
        return 0
    fi
    read -r -a DOCKER <<< "${MILEAGE_DOCKER:-docker}"
    if ! volumes=$("${DOCKER[@]}" volume ls \
            --filter "label=com.docker.compose.project=$project" \
            --filter 'label=com.docker.compose.volume=monitor-state' \
            --format '{{.Name}}' 2>/dev/null); then
        MONITOR_DISCOVERY_ERROR="Docker volume listing failed"
        return 0
    fi
    if [[ -n $volumes && ! $volumes =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
        MONITOR_DISCOVERY_ERROR="Docker volume listing returned an ambiguous or invalid result"
        return 0
    fi
    MONITOR_PRESENT=no
    [[ -z $volumes ]] || MONITOR_PRESENT=yes
    return 0
}

db_containment_root() {
    # Only the shared trip-data mount has this boundary; the independent
    # annotations and monitor volumes keep their existing paths.
    if [[ $1 == /data/* ]]; then printf '/data'; fi
}

# Sets SNAPSHOT_REPORT to the checked container report, or 'missing'.
# Missing is only allowed for optional DBs (and pre-restore safety copies).
# With salvage=yes (restore's safety copies), a live file that can't be
# read as a database (exit 4) is saved raw instead of stopping the
# restore: SNAPSHOT_REPORT is 'unreadable' and the raw files are in
# DEST-unreadable/.
snapshot_db() {
    local runner=$1 path=$2 dest=$3 optional=$4 salvage=${5:-no} status expected actual root
    root=$(db_containment_root "$path")
    if "$runner" "$PY_SNAPSHOT" "$path" "$root" > "$dest" 2> "$dest.log"; then
        SNAPSHOT_REPORT=$(grep -E '^sha256=' "$dest.log" | tail -n 1 || true)
        expected=$(sed -E 's/^sha256=([0-9a-f]+) .*/\1/' <<< "$SNAPSHOT_REPORT")
        actual=$(sha256sum "$dest" | cut -d ' ' -f 1)
        [[ -n $SNAPSHOT_REPORT && $expected == "$actual" ]] \
            || die "$path snapshot doesn't match what the container sent"
        rm "$dest.log"
    else
        status=$?
        cat "$dest.log" >&2
        if [[ $status -eq 5 ]]; then
            die "refused unsafe database path $path; nothing more was restored or backed up"
        elif [[ $status -eq 3 && $optional == yes ]]; then
            rm "$dest" "$dest.log"
            SNAPSHOT_REPORT=missing
            log "$path: no database yet; skipped"
        elif [[ $status -eq 4 && $salvage == yes ]]; then
            rm -f "$dest" "$dest.log"
            salvage_raw_db "$runner" "$path" "$dest-unreadable"
            SNAPSHOT_REPORT=unreadable
        else
            die "could not snapshot $path; nothing more was restored or backed up"
        fi
    fi
}

# Copy a database file that SQLite can't read, and its -wal/-journal/-shm
# sidecars, byte for byte into a new folder. Nothing else can safely hold
# what the damaged file still has in it.
salvage_raw_db() {
    local runner=$1 path=$2 dir=$3 count root
    root=$(db_containment_root "$path")
    mkdir -p "$dir"
    "$runner" "$PY_RAW_COPY" "$path" "$root" | tar -x -f - -C "$dir" --no-same-owner \
        || die "$path can't be read as a database, and its raw files couldn't be saved either; nothing more was restored"
    count=$(find "$dir" -type f | wc -l)
    [[ $count -gt 0 ]] || die "$path can't be read as a database, and no raw file was found to save; nothing more was restored"
    log "WARNING: the current $path can't be read as a database (it is damaged)."
    log "WARNING: its raw files ($count) are saved in $dir. The restore replaces it."
}

# Delete a database file and its sidecars (inside the container's volume)
# so a restore can write a clean copy: SQLite can't restore over a damaged
# file, and a leftover journal could be replayed into the new one. Only
# for a file that salvage_raw_db has already saved.
remove_raw_db() {
    local runner=$1 path=$2
    "$runner" "$PY_REMOVE_DB" "$path" || die "could not clear the damaged $path"
}

# An unattended backup must not wait forever on a destination that
# vanished mid-transfer: rsync gives up after 10 minutes without any I/O,
# and an SSH connection is dropped after about two minutes of silence.
# These are added to the ssh command from RSYNC_SSH (or plain `ssh`), unless
# RSYNC_SSH already sets ServerAliveInterval. ssh uses the first value it
# sees for an option, so options already in RSYNC_SSH win.
RSYNC_TIMEOUT=600
SSH_KEEPALIVE="-o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=30"

rsync_remote() {
    local ssh_cmd=${RSYNC_SSH:-ssh}
    [[ $ssh_cmd == *ServerAliveInterval* ]] || ssh_cmd="$ssh_cmd $SSH_KEEPALIVE"
    rsync --timeout="$RSYNC_TIMEOUT" -e "$ssh_cmd" "$@"
}

# A file at the top of the destination that says "this is the real backup
# target". backup.sh refuses to copy without it, so an unmounted share or
# disk (an empty folder on this machine's own disk) can't be mistaken for
# the off-host copy. Written by install.sh or `backup.sh --mark-target`.
TARGET_MARKER=.mileage-backup-target

target_marked() {
    rsync_remote --list-only "$REMOTE/$TARGET_MARKER" > /dev/null 2>&1
}

mark_target() {
    local tmp
    tmp=$(mktemp)
    printf 'mileage-tracker backup destination\n' > "$tmp"
    rsync_remote -t "$tmp" "$REMOTE/$TARGET_MARKER" || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
}

list_snapshots() {
    [[ -d $SNAPSHOTS ]] || return 0
    find "$SNAPSHOTS" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | grep -E "$SNAPSHOT_RE" | LC_ALL=C sort || true
}

# The snapshot names on the off-host copy, oldest first. Fails if the
# destination can't be listed (unreachable, no snapshots/ folder, timeout).
list_remote_snapshots() {
    local listing
    if ! listing=$(rsync_remote --list-only "$REMOTE/snapshots/" 2> /dev/null); then
        return 1
    fi
    awk '{ print $NF }' <<< "$listing" | grep -E "$SNAPSHOT_RE" | LC_ALL=C sort || true
}

# Shared with restore: files not covered by the manifest are not verified.
# INFO and SHA256SUMS itself are the only exceptions, at the snapshot root.
# sha256sum reports failed paths, never file contents or expected hashes.
verify_snapshot() {
    local snap=$1 listed actual
    [[ -f $snap/SHA256SUMS && -f $snap/annotations.db && -d $snap/data ]] || {
        log "snapshot doesn't look like a backup (SHA256SUMS, annotations.db or data missing)"
        return 1
    }
    (cd "$snap" && sha256sum --quiet --strict -c SHA256SUMS) >&2 || {
        log "checksums don't match; this snapshot is damaged"
        return 1
    }
    listed=$(sed -E 's/^[0-9a-f]{64} [ *]//' "$snap/SHA256SUMS" | LC_ALL=C sort) || return 1
    actual=$(cd "$snap" && find . -type f ! -path ./SHA256SUMS ! -path ./INFO | LC_ALL=C sort) || return 1
    if [[ $listed != "$actual" ]]; then
        log "snapshot has files missing from or not in SHA256SUMS (paths below)"
        LC_ALL=C comm -3 <(printf '%s\n' "$listed") <(printf '%s\n' "$actual") >&2
        return 1
    fi
}

validate_verify_settings() {
    if [[ ! $BACKUP_VERIFY_DAYS =~ ^(0|[1-9][0-9]{0,4})$ ]] \
            || (( BACKUP_VERIFY_DAYS > 36500 )); then
        die "BACKUP_VERIFY_DAYS must be 0 (off) or 1..36500 days"
    fi
    if [[ ! $BACKUP_VERIFY_COUNT =~ ^[1-9][0-9]{0,3}$ ]] \
            || (( BACKUP_VERIFY_COUNT < 2 || BACKUP_VERIFY_COUNT > 1000 )); then
        die "BACKUP_VERIFY_COUNT must be 2..1000 (oldest plus rotation)"
    fi
}

# Data, never sourced shell code. The timestamp cursor remains meaningful
# even when pruning removes the snapshot it named. Pending names pin failed
# snapshots until retry; 'running' also retries after interruption/timeout.
read_verify_state() {
    VERIFY_TIME='' VERIFY_RESULT='' VERIFY_CURSOR='' VERIFY_PENDING='' VERIFY_REMOTE_PENDING=''
    local key value name
    [[ -r $BACKUP_DIR/verify-state ]] || return 0
    while IFS='=' read -r key value; do
        case $key in
            last_check) [[ $value =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] && VERIFY_TIME=$value ;;
            result) [[ $value == passed || $value == failed || $value == running ]] && VERIFY_RESULT=$value ;;
            cursor) [[ $value =~ $SNAPSHOT_RE ]] && VERIFY_CURSOR=$value ;;
            pending|remote_pending)
                for name in $value; do
                    [[ $name =~ $SNAPSHOT_RE ]] || continue
                    if [[ $key == pending ]]; then VERIFY_PENDING+="$name "; else VERIFY_REMOTE_PENDING+="$name "; fi
                done ;;
        esac
    done < "$BACKUP_DIR/verify-state"
    # Successful runs need no retry pins.
    if [[ $VERIFY_RESULT == passed ]]; then VERIFY_PENDING='' VERIFY_REMOTE_PENDING=''; fi
    return 0
}

print_verify_status() {
    read_verify_state
    printf 'last_deep_check=%s\nlast_deep_check_result=%s\n' "$VERIFY_TIME" "$VERIFY_RESULT"
}

show_verify_status() {
    read_verify_state
    log "Last deep check: ${VERIFY_TIME:-never} (result: ${VERIFY_RESULT:-not run})"
}

write_verify_state() {
    local tmp
    tmp=$(mktemp "$BACKUP_DIR/.verify-state.XXXXXX") || return 1
    if ! printf 'last_check=%s\nresult=%s\ncursor=%s\npending=%s\nremote_pending=%s\n' \
            "$VERIFY_TIME" "$VERIFY_RESULT" "$VERIFY_CURSOR" "$VERIFY_PENDING" "$VERIFY_REMOTE_PENDING" > "$tmp" \
            || ! mv -f "$tmp" "$BACKUP_DIR/verify-state"; then
        rm -f "$tmp"
        return 1
    fi
}

# Retention for both the local and the off-host snapshots: reads names
# (oldest first) on stdin and prints the ones to drop. Keeps the newest
# KEEP_RECENT, plus the first snapshot of each of the newest KEEP_MONTHLY
# months.
retention_drops() {
    awk -v recent="$KEEP_RECENT" -v monthly="$KEEP_MONTHLY" '
        { name[NR] = $0; month = substr($0, 1, 7)
          if (!(month in seen)) { seen[month] = 1; firsts[++months] = NR } }
        END {
            for (i = NR - recent + 1; i <= NR; i++) if (i >= 1) keep[i] = 1
            for (j = months - monthly + 1; j <= months; j++) if (j >= 1) keep[firsts[j]] = 1
            for (i = 1; i <= NR; i++) if (!(i in keep)) print name[i]
        }'
}

require_commands() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" > /dev/null || die "'$cmd' is not installed"
    done
}

# Shared preflight for the Python helpers. Callers explicitly pass /data for
# data-mount paths, or an empty root for the separate database volumes.
# Exit 5 is unsafe, never missing (3) or corrupt and eligible for salvage (4).
PY_DB_CONTAINMENT=$(cat <<'PY'
import sys
from pathlib import Path

def checked_db_paths(path, root, suffixes=("",)):
    paths = [Path(str(path) + suffix) for suffix in suffixes]
    if not root:
        return paths
    try:
        root = Path(root).resolve()
        directory = Path(path).parent.resolve()
        resolved = [candidate.resolve() for candidate in paths]
        if any(root not in candidate.parents for candidate in [directory, *resolved]):
            raise ValueError("outside data root")
    except (OSError, RuntimeError, ValueError):
        print(f"unsafe database path {path}: outside data root or unresolvable", file=sys.stderr)
        sys.exit(5)
    return resolved
PY
)

# Snapshot the database at argv[1] to stdout using SQLite's online backup
# API, which is consistent even while the viewer is writing. Checks the
# copy before sending it and reports its hash on stderr so the caller can
# confirm nothing was lost on the way out. Exit 3 means no database yet;
# exit 4 means the file exists but isn't a readable database (damaged).
# argv[2] is the explicit containment root, or empty for other volumes.
PY_SNAPSHOT=$PY_DB_CONTAINMENT$'\n'$(cat <<'PY'
import hashlib, os, sqlite3, sys, tempfile
from pathlib import Path
src_path = checked_db_paths(sys.argv[1], sys.argv[2])[0]
try:
    os.stat(src_path)
except FileNotFoundError:
    print(f"{src_path} does not exist", file=sys.stderr)
    sys.exit(3)
fd, tmp = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    try:
        src = sqlite3.connect(Path(src_path).as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        check = dst.execute("PRAGMA integrity_check").fetchall()
        if check != [("ok",)]:
            print(f"{src_path} failed integrity_check: {check}", file=sys.stderr)
            sys.exit(4)
    except sqlite3.DatabaseError as exc:
        # Busy, locked and can't-open errors are OperationalError: the file
        # may be fine, so those stay fatal. Corrupt and not-a-database aren't.
        # A leftover journal that needs rolling back can't be read-only
        # ("attempt to write a readonly database"): the file can't be read
        # as it stands either.
        if isinstance(exc, sqlite3.OperationalError) and "readonly" not in str(exc):
            raise
        print(f"{src_path} can't be read as a database: {exc}", file=sys.stderr)
        sys.exit(4)
    has_table = dst.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotations'"
    ).fetchone()
    rows = dst.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] if has_table else 0
    has_events = dst.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    events = dst.execute("SELECT COUNT(*) FROM events").fetchone()[0] if has_events else 0
    dst.close()
    with open(tmp, "rb") as f:
        data = f.read()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    digest = hashlib.sha256(data).hexdigest()
    print(f"sha256={digest} bytes={len(data)} annotations={rows} events={events}", file=sys.stderr)
finally:
    os.unlink(tmp)
PY
)

# Write the database file at argv[1] and any -wal/-journal/-shm sidecars
# to stdout as a tar stream, without opening them as a database.
PY_RAW_COPY=$PY_DB_CONTAINMENT$'\n'$(cat <<'PY'
import os, sys, tarfile
path = sys.argv[1]
suffixes = ("", "-wal", "-journal", "-shm")
paths = checked_db_paths(path, sys.argv[2], suffixes)
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
    for suffix, candidate in zip(suffixes, paths):
        if candidate.exists():
            tar.add(candidate, arcname=os.path.basename(path) + suffix, recursive=False)
PY
)

# Delete the database file at argv[1] and its -wal/-journal/-shm sidecars.
PY_REMOVE_DB=$(cat <<'PY'
import os, sys
for suffix in ("", "-wal", "-journal", "-shm"):
    try:
        os.unlink(sys.argv[1] + suffix)
    except FileNotFoundError:
        pass
PY
)

# Read a database from stdin, check it, and report what it holds.
# argv[1] selects annotations/events/monitor. With argv[2], also copy it
# over that path (the live database) using the backup API, then check it.
PY_CHECK_RESTORE=$(cat <<'PY'
import os, sqlite3, sys, tempfile
from pathlib import Path

kind = sys.argv[1] if len(sys.argv) > 1 else "annotations"
def describe(conn, label):
    check = conn.execute("PRAGMA integrity_check").fetchall()
    if check != [("ok",)]:
        sys.exit(f"{label} failed integrity_check: {check}")
    if kind == "events":
        # ingest creates the file before its first transaction commits, so a
        # backup can catch it with no tables: that's "no diagnostics yet".
        has_events = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if not has_events:
            print(f"{label}: integrity ok, no diagnostics yet (no events table)")
            return
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"{label}: integrity ok, {rows} events")
        return
    if kind == "monitor":
        # Enumerate stored tables, including additive export cursor/retry tables.
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {row[0] for row in tables}
        if not {"episodes", "outbox", "retry"} <= names:
            sys.exit(f"{label} has no monitor state tables")
        print(f"{label}: integrity ok, {len(tables)} monitor state tables")
        return
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotations'"
    ).fetchone()
    if not has_table:
        sys.exit(f"{label} has no annotations table")
    rows, tagged, newest = conn.execute(
        "SELECT COUNT(*), COUNT(NULLIF(category, '')), MAX(updated_at) FROM annotations"
    ).fetchone()
    print(f"{label}: integrity ok, {rows} annotations ({tagged} tagged), "
          f"last edited {newest or 'never'}")
data = sys.stdin.buffer.read()
if not data.startswith(b"SQLite format 3\x00"):
    sys.exit("input is not a SQLite database")
fd, tmp = tempfile.mkstemp(suffix=".db")
try:
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    src = sqlite3.connect(tmp)
    describe(src, "snapshot")
    if len(sys.argv) > 2:
        path = Path(sys.argv[2])
        path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(path)
        src.backup(dst)
        describe(dst, "restored database")
        dst.close()
    src.close()
finally:
    os.unlink(tmp)
PY
)
