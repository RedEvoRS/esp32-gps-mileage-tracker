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

CONF=${MILEAGE_BACKUP_CONF:-$SCRIPT_DIR/backup.conf}
[[ -f $CONF ]] || die "no config at $CONF (copy backup.conf.example to backup.conf)"
# shellcheck source=backup.conf.example
. "$CONF"

COMPOSE_DIR=${COMPOSE_DIR:-$(dirname "$SCRIPT_DIR")}
BACKUP_DIR=${BACKUP_DIR:-$HOME/mileage-backups}
REMOTE=${REMOTE:-}
RSYNC_SSH=${RSYNC_SSH:-}
KEEP_RECENT=${KEEP_RECENT:-14}
KEEP_MONTHLY=${KEEP_MONTHLY:-84}
REMOTE_PRUNE=${REMOTE_PRUNE:-no}
PING_URL=${PING_URL:-}

SNAPSHOTS=$BACKUP_DIR/snapshots
# Snapshot directories are named by UTC creation time, so they sort
# chronologically: 2026-10-06T033012Z
SNAPSHOT_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$'

# Tests swap in a fake; on the server this is plain `docker compose`.
read -r -a COMPOSE <<< "${MILEAGE_COMPOSE:-docker compose}"

compose() {
    (cd "$COMPOSE_DIR" && "${COMPOSE[@]}" "$@")
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

rsync_remote() {
    if [[ -n $RSYNC_SSH ]]; then
        rsync -e "$RSYNC_SSH" "$@"
    else
        rsync "$@"
    fi
}

list_snapshots() {
    [[ -d $SNAPSHOTS ]] || return 0
    find "$SNAPSHOTS" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | grep -E "$SNAPSHOT_RE" | LC_ALL=C sort || true
}

require_commands() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" > /dev/null || die "'$cmd' is not installed"
    done
}

# Snapshot the database at argv[1] to stdout using SQLite's online backup
# API, which is consistent even while the viewer is writing. Checks the
# copy before sending it and reports its hash on stderr so the caller can
# confirm nothing was lost on the way out. Exit 3 means no database yet.
PY_SNAPSHOT=$(cat <<'PY'
import hashlib, os, sqlite3, sys, tempfile
src_path = sys.argv[1]
if not os.path.exists(src_path):
    print(f"{src_path} does not exist", file=sys.stderr)
    sys.exit(3)
fd, tmp = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        sys.exit(f"snapshot failed integrity_check: {check}")
    has_table = dst.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotations'"
    ).fetchone()
    rows = dst.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] if has_table else 0
    dst.close()
    with open(tmp, "rb") as f:
        data = f.read()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    digest = hashlib.sha256(data).hexdigest()
    print(f"sha256={digest} bytes={len(data)} annotations={rows}", file=sys.stderr)
finally:
    os.unlink(tmp)
PY
)

# Read a database from stdin, check it, and report what it holds.
# With argv[1], also copy it over that path (the live database) using
# the backup API, then check the result.
PY_CHECK_RESTORE=$(cat <<'PY'
import os, sqlite3, sys, tempfile
def describe(conn, label):
    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        sys.exit(f"{label} failed integrity_check: {check}")
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
    if len(sys.argv) > 1:
        dst = sqlite3.connect(sys.argv[1])
        src.backup(dst)
        describe(dst, "restored database")
        dst.close()
    src.close()
finally:
    os.unlink(tmp)
PY
)
