#!/usr/bin/env bash
# Persistent cron runner; Debian's systemd service still runs backup.sh directly.
# Usage: scheduled.sh [--delay SECONDS] [--if-overdue HOURS] | --status
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=$SCRIPT_DIR/state
LOG_DIR=$SCRIPT_DIR/logs
LOG=$LOG_DIR/backup.log
ATTEMPT=$STATE_DIR/last-attempt

delay=0
overdue=
status_only=no
usage() {
    echo 'Usage: scheduled.sh [--delay SECONDS] [--if-overdue HOURS] | --status' >&2
    exit 2
}
while (( $# )); do
    case $1 in
        --status) status_only=yes; shift ;;
        --delay|--if-overdue)
            (( $# >= 2 )) && [[ $2 =~ ^[0-9]+$ && ${#2} -le 9 ]] || usage
            if [[ $1 == --delay ]]; then delay=$((10#$2)); else overdue=$((10#$2)); fi
            shift 2 ;;
        *) usage ;;
    esac
done
[[ $status_only != yes || ( $delay == 0 && -z $overdue ) ]] || usage

# Resolve config in a subshell: missing/unreadable config means overdue,
# and --status must still report an empty success and the persistent log path.
# Do not echo config, SSH options, or errors from sourcing shell settings.
backup_dir=$(
    # shellcheck source=lib.sh
    . "$SCRIPT_DIR/lib.sh" > /dev/null 2>&1
    printf '%s' "$BACKUP_DIR"
) || backup_dir=
last_success=
if [[ -n $backup_dir && -r $backup_dir/last-success ]]; then
    last_success=$(cat "$backup_dir/last-success" 2>/dev/null) || last_success=
fi

if [[ $status_only == yes ]]; then
    start='' end='' result=''
    if [[ -r $ATTEMPT ]]; then
        while IFS='=' read -r key value; do
            case $key in
                start) start=$value ;;
                end) end=$value ;;
                status) result=$value ;;
            esac
        done < "$ATTEMPT"
    fi
    printf 'last_attempt_start=%s\nlast_attempt_end=%s\nlast_attempt_status=%s\nlast_success=%s\nlog=%s\n' \
        "$start" "$end" "$result" "$last_success" "$LOG"
    (
        # shellcheck source=lib.sh
        . "$SCRIPT_DIR/lib.sh" > /dev/null 2>&1
        print_verify_status
    ) || printf 'last_deep_check=\nlast_deep_check_result=\n'
    exit 0
fi

(( delay == 0 )) || sleep "$(( ((RANDOM << 15) | RANDOM) % (delay + 1) ))"

is_due() {
    local success_epoch now
    [[ -n $overdue ]] || return 0
    [[ -n $last_success ]] || return 0
    success_epoch=$(date -u -d "$last_success" +%s 2>/dev/null) || return 0
    now=$(date -u +%s)
    (( now - success_epoch > overdue * 3600 ))
}
is_due || exit 0

mkdir -p "$STATE_DIR" "$LOG_DIR"
chmod 700 "$STATE_DIR" "$LOG_DIR"
exec 8> "$STATE_DIR/.lock"
chmod 600 "$STATE_DIR/.lock"
# Nightly and catch-up never run backup.sh together, including after a delay.
flock -n 8 || exit 0
# Another scheduled run may have succeeded while this one was delayed.
if [[ -n $backup_dir && -r $backup_dir/last-success ]]; then
    last_success=$(cat "$backup_dir/last-success" 2>/dev/null) || last_success=
fi
is_due || exit 0

rotate_log() {
    local n
    [[ -f $LOG ]] || return 0
    (( $(stat -c %s "$LOG") > 1048576 )) || return 0
    rm -f "$LOG.3"
    for n in 2 1; do
        [[ ! -f $LOG.$n ]] || mv -f "$LOG.$n" "$LOG.$((n + 1))"
    done
    mv -f "$LOG" "$LOG.1"
    touch "$LOG"
    chmod 600 "$LOG" "$LOG".[123] 2>/dev/null || true
}
rotate_log
touch "$LOG"
chmod 600 "$LOG"

append_log() {
    local before after
    # Consume output in bounded chunks, reopening after each rotation. Even a
    # noisy or stalled run keeps only three rotations and the current log.
    while :; do
        before=$(stat -c %s "$LOG")
        dd bs=65536 count=1 conv=notrunc status=none >> "$LOG"
        after=$(stat -c %s "$LOG")
        (( after != before )) || break
        rotate_log
    done
}

write_attempt() {
    local tmp
    tmp=$(mktemp "$STATE_DIR/.last-attempt.XXXXXX")
    printf 'start=%s\nend=%s\nstatus=%s\n' "$start" "$end" "$result" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$ATTEMPT"
}

# Retain the previous record if a direct backup.sh owns its separate lock.
previous=
if [[ -f $ATTEMPT ]]; then
    previous=$(mktemp "$STATE_DIR/.previous.XXXXXX")
    cp "$ATTEMPT" "$previous"
    chmod 600 "$previous"
fi
start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
end=
result=running
write_attempt
command=(nice -n 10)
if command -v ionice > /dev/null 2>&1; then command+=(ionice -c3); fi
command+=(timeout 6h bash "$SCRIPT_DIR/backup.sh")
if "${command[@]}" 2>&1 | append_log; then result=0; else result=${PIPESTATUS[0]}; fi
if [[ -n $overdue && $result == 75 ]]; then
    if [[ -n $previous ]]; then mv -f "$previous" "$ATTEMPT"; else rm -f "$ATTEMPT"; fi
    result=0
else
    end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    write_attempt
fi
[[ -z $previous ]] || rm -f "$previous"
rotate_log
exit "$result"
