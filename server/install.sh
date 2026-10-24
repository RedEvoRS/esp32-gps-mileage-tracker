#!/usr/bin/env bash
# Install the mileage tracker server on Debian, Ubuntu or Unraid, checking each
# step as it goes.
#
#   bash install.sh           set everything up, or finish a setup that
#                             stopped partway
#   bash install.sh --check   leave configuration unchanged; upload and
#                             remove a test trip, optionally test login
#
# Run as root on Unraid, or your normal login user on Debian. On Debian,
# it asks for sudo once, before the long steps: at the start, or for
# optional steps (backups, waiting for the network) as soon as you say
# yes to them. If none will, it doesn't ask at all. It works on the
# directory it's in, normally ~/mileage-tracker
# (your copy of server/). It's safe to run again: anything already set up
# is checked and kept, so after fixing a problem, just run it again.
#
# The steps follow DEPLOY_DEBIAN.md, which explains each one:
#   Preflight       files, sudo, UID
#   Docker          packages and Docker Engine (Step 1)
#   Configuration   data/, .env, viewer.env (Steps 3, 4a, 4b, 7)
#   Start           build, admin login, optional everyday login, start,
#                   wait for healthy (Step 5)
#   End to end      upload sample.gpx, find it in the viewer and the
#                   exports, then delete it again (Steps 6, 7)
#   Reboots         Docker comes back with the network (Step 9)
#   Backups         off-host copy, restore test, daily timer (Step 10)
#   Notifications   optional ntfy, metrics, Graylog and Loki outputs, last,
#                   so an unreachable destination can't hold up the backups
#                   above; the answers go to monitor.env (Step 11)
# and finally the next firmware build/flash steps (Step 8).
#
# Any question can be answered ahead of time with an INSTALL_<NAME>
# environment variable; the names are in the ask/confirm calls below.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
is_unraid() { local f=${MILEAGE_UNRAID_VERSION_FILE:-/etc/unraid-version}; [[ -r $f ]] && grep -Eq '^version="?[0-9]' "$f"; }
case ${MILEAGE_PLATFORM:-} in
    '') if is_unraid; then MILEAGE_PLATFORM=unraid; else MILEAGE_PLATFORM=debian; fi ;;
    debian|unraid) ;;
    *) printf 'MILEAGE_PLATFORM must be debian or unraid.\n' >&2; exit 1 ;;
esac
export MILEAGE_PLATFORM
# Both images run as a user called app, which gets UID 1000.
CONTAINER_UID=1000
# Trip and device IDs of the test upload start with this. Each run adds
# a random suffix, so nothing real or another run's test shares its path.
TEST_PREFIX=install-check
# The REMOTE line backup.conf.example ships with, i.e. "not set up yet".
EXAMPLE_REMOTE=backup@nas.lan:/volume1/backups/mileage-tracker

MODE=install
case ${1:-} in
    '') ;;
    --check) MODE=check ;;
    -h | --help)
        sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
        exit 0
        ;;
    *)
        printf 'Unknown option: %s (try --help)\n' "$1" >&2
        exit 2
        ;;
esac

ME=$(id -un)
DOCKER=(docker)     # becomes sudo-prefixed if this session can't use Docker
AS_ME=()            # runs a command as $ME with fresh group membership
DATA_SUDO=()        # sudo, if trip files (UID 1000) aren't ours to touch
BIND_ADDR=
VIEWER_BIND_ADDR=
VIEWER_COOKIE_SECURE=false
VIEWER_PASSWORD=    # the admin's password, when entered during this run
EVERYDAY_USER=      # an everyday login made by this run, and its password
EVERYDAY_PASSWORD=  # (only kept for the login test; never written anywhere)
EVERYDAY_LOGINS=()  # labels of all everyday logins, from list_everyday
RELOGIN=no
FIRST_BACKUP=no     # yes once this run has made the first backup
SUDO_READY=no
WANT_WAIT_ONLINE=   # yes/no once asked; see ask_root_options
WANT_BACKUPS=       # likewise
SUDO_KEEPALIVE=
TEST_ID=            # install-check-<random>, set by upload_test_trip
TEST_TRIP=          # data/<ym>/<TEST_ID>, while this run's test trip may exist
TEST_NEW_DIR=       # data/<ym>, if this run's upload is what created it
TMP_FILES=()
WARNINGS=()
FAILURES=0
BUILD_SERVER_VERSION=unversioned
BOOT_UNVERIFIED=no
PYTHON_ROUTE=

# --- Output ------------------------------------------------------------

if [[ -t 1 ]]; then
    BOLD=$'\e[1m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' PLAIN=$'\e[0m'
else
    BOLD='' RED='' GREEN='' YELLOW='' PLAIN=''
fi

section() { printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$PLAIN"; }
ok() { printf '  %sok%s    %s\n' "$GREEN" "$PLAIN" "$1"; }
note() { printf '        %s\n' "$@"; }

warn() {
    printf '  %swarn%s  %s\n' "$YELLOW" "$PLAIN" "$1"
    WARNINGS+=("$1")
    shift
    if (( $# )); then note "$@"; fi
}

# A check failed. Installing stops here, so the next run starts from a
# known state; --check notes it and carries on with the next check.
problem() {
    printf '  %sFAIL%s  %s\n' "$RED" "$PLAIN" "$1"
    shift
    if (( $# )); then note "$@"; fi
    FAILURES=$((FAILURES + 1))
    if [[ $MODE == install ]]; then stop; fi
}

# Something that makes going on pointless, in either mode.
die() {
    MODE=install problem "$@"
}

stop() {
    printf '\n%sInstall stopped.%s Fix the problem above, then run this script again.\n' "$RED" "$PLAIN"
    printf 'It keeps everything that is already done.\n'
    exit 1
}

# With set -E this also runs inside $(...) and pipeline subshells, where a
# failing command is often expected (a grep that finds nothing). Only the
# main shell reports: a failure that matters stops the main shell too.
on_error() {
    local status=$1 line=$2 command=$3
    [[ $BASHPID == "$$" ]] || return 0
    printf '\n  %sFAIL%s  Unexpected error (exit %s) on line %s of install.sh:\n' \
        "$RED" "$PLAIN" "$status" "$line"
    note "$command"
    stop
}

cleanup() {
    # A last try at deleting the test trip. If it's still there, say so and
    # exit non-zero, whatever the run reported before.
    local left=no
    if [[ -n $TEST_TRIP ]] && ! remove_test_trip; then
        printf '\n  %sFAIL%s  The test trip is still on disk\n' "$RED" "$PLAIN"
        test_trip_left
        left=yes
    fi
    if [[ -n $SUDO_KEEPALIVE ]]; then kill "$SUDO_KEEPALIVE" 2> /dev/null || true; fi
    if (( ${#TMP_FILES[@]} )); then rm -f "${TMP_FILES[@]}"; fi
    if [[ $left == yes ]]; then exit 1; fi
}

# Print a command's output indented, so it reads as part of a step.
indent() { sed 's/^/        | /'; }

# --- Questions ---------------------------------------------------------

no_answer() {
    echo
    die "No answer to \"$1\" (input ended)." \
        "Run this in a terminal, or answer it in advance with INSTALL_$2=..."
}

# ask VAR NAME "Question" [default]
# The locals have odd names so they can't hide the caller's VAR.
ask() {
    local __var=$1 __name=$2 __question=$3 __default=${4-} __preset="INSTALL_$2" __answer
    if [[ -n ${!__preset+set} ]]; then
        __answer=${!__preset}
        printf '  %s: %s (from %s)\n' "$__question" "$__answer" "$__preset"
    else
        printf '  %s%s: ' "$__question" "${__default:+ [$__default]}"
        IFS= read -r __answer || no_answer "$__question" "$__name"
    fi
    printf -v "$__var" '%s' "${__answer:-$__default}"
}

# ask_secret VAR NAME "Question" [optional]
# With "optional", running out of input means "skip" rather than stop.
ask_secret() {
    local __var=$1 __name=$2 __question=$3 __optional=${4-} __preset="INSTALL_$2" __answer=
    if [[ -n ${!__preset+set} ]]; then
        __answer=${!__preset}
        printf '  %s: (from %s)\n' "$__question" "$__preset"
    else
        printf '  %s: ' "$__question"
        if ! IFS= read -r -s __answer; then
            [[ -n $__optional ]] || no_answer "$__question" "$__name"
            __answer=
        fi
        echo
    fi
    printf -v "$__var" '%s' "$__answer"
}

# confirm NAME "Question" y|n  ->  success for yes
confirm() {
    local name=$1 question=$2 default=$3 preset="INSTALL_$1" answer hint=y/N
    if [[ $default == y ]]; then hint=Y/n; fi
    while :; do
        if [[ -n ${!preset+set} ]]; then
            answer=${!preset}
            printf '  %s [%s] %s (from %s)\n' "$question" "$hint" "$answer" "$preset"
        else
            printf '  %s [%s] ' "$question" "$hint"
            IFS= read -r answer || no_answer "$question" "$name"
        fi
        case ${answer:-$default} in
            [Yy] | [Yy][Ee][Ss]) return 0 ;;
            [Nn] | [Nn][Oo]) return 1 ;;
        esac
        if [[ -n ${!preset+set} ]]; then die "$preset should be y or n, not \"$answer\"."; fi
        note "Please answer y or n."
    done
}

# --- sudo and Docker access --------------------------------------------

# need_sudo [why]: make sure sudo works, asking for the password if it
# has to. Every step that runs sudo calls this first, though plan_sudo
# has usually asked already.
need_sudo() {
    [[ $MILEAGE_PLATFORM != unraid ]] || return 0
    [[ $SUDO_READY == no ]] || return 0
    command -v sudo > /dev/null \
        || die "sudo isn't installed." \
            "As root (su -): apt install sudo && usermod -aG sudo $ME" \
            "then log out, log back in, and run this again."
    # sudo -n works with NOPASSWD or a password sudo already has; -v
    # only when it doesn't, since -v asks unless every rule is NOPASSWD.
    if ! sudo -n true 2> /dev/null; then
        # sudo reads the password from the terminal, not stdin, so without
        # one (ssh <host> 'bash install.sh', no -t) it can't ask at all.
        if ! { : > /dev/tty; } 2> /dev/null; then
            die "sudo needs your password, but this isn't running in a terminal, so it can't ask." \
                "Run it from a login session, or over ssh with -t:" \
                "ssh -t <user>@<host> bash $SCRIPT_DIR/install.sh"
        fi
        note "${1:-The next step needs root}, so sudo asks for your password."
        sudo -v \
            || die "sudo didn't let you in (its reason is just above)." \
                "A mistyped password: just run this again." \
                "\"not in the sudoers file\": as root (su -), usermod -aG sudo $ME," \
                "then log out, log back in, and run this again."
    fi
    SUDO_READY=yes
    # Keep sudo's timestamp fresh through the long steps (image builds,
    # first backup), so it doesn't ask again halfway.
    (while sleep 50; do
        kill -0 "$$" 2> /dev/null || exit 0
        sudo -n true 2> /dev/null || exit 0
    done) > /dev/null 2>&1 &
    SUDO_KEEPALIVE=$!
}

# Unraid runs as root; Debian retains the existing sudo authentication flow.
as_root() {
    if [[ $MILEAGE_PLATFORM == unraid ]]; then "$@"; else sudo "$@"; fi
}

select_python() {
    [[ -z $PYTHON_ROUTE ]] || return 0
    if command -v python3 > /dev/null; then PYTHON_ROUTE=host; else PYTHON_ROUTE=container; fi
    note "Installer Python: $PYTHON_ROUTE" >&2
}

# Only public inputs needed by this step are visible to the Python container.
# Config values and address lists arrive through arguments/stdin, never mounts.
host_python() {  # stamp|proxy|address PYTHON_ARGUMENTS...
    local step=$1 image file mounts=()
    shift
    select_python
    if [[ $PYTHON_ROUTE == host ]]; then
        PYTHONPATH="$SCRIPT_DIR" python3 "$@"
        return
    fi
    image=$(awk 'toupper($1) == "FROM" { print $2; exit }' "$SCRIPT_DIR/mileage-ingest/Dockerfile")
    [[ -n $image ]] || die "No Python base image in mileage-ingest/Dockerfile."
    if [[ $step == stamp || $step == proxy ]]; then
        mounts+=(-v "$SCRIPT_DIR/common/__init__.py:/work/common/__init__.py:ro")
        if [[ $step == stamp ]]; then file=version.py; else file=settings.py; fi
        mounts+=(-v "$SCRIPT_DIR/common/$file:/work/common/$file:ro")
    fi
    if [[ $step == stamp ]]; then
        for file in stamp_version.py VERSION COMMIT; do
            if [[ -f $SCRIPT_DIR/$file ]]; then mounts+=(-v "$SCRIPT_DIR/$file:/work/$file:ro"); fi
        done
        shift
        set -- /work/stamp_version.py "$@"
    fi
    "${DOCKER[@]}" run --rm -i --network none --user "$CONTAINER_UID:$CONTAINER_UID" \
        "${mounts[@]}" -w /work -e PYTHONPATH=/work -e PYTHONDONTWRITEBYTECODE=1 "$image" python3 "$@"
}

# Default provenance changes image IDs on Docker's containerd store even for
# cached builds. Compose 2.38 lacks build.provenance; set this after any sudo
# hop so first installs and re-runs both keep unchanged images stable.
compose() {
    if (( ${#AS_ME[@]} )); then
        "${AS_ME[@]}" env BUILDX_NO_DEFAULT_ATTESTATIONS=1 SERVER_VERSION="$BUILD_SERVER_VERSION" docker compose "$@"
    else
        BUILDX_NO_DEFAULT_ATTESTATIONS=1 SERVER_VERSION="$BUILD_SERVER_VERSION" "${DOCKER[@]}" compose "$@"
    fi
}

container_state() {
    "${DOCKER[@]}" inspect -f '{{.State.Status}}' "$1" 2> /dev/null || echo missing
}

# The state of a Compose service's container, or missing. Services with no
# container_name (ntfy, mileage-monitor) are called <project>-<service>-1,
# so they're looked up by service. Needs the service's profile.
service_state() {
    local id
    id=$({ compose --profile "$2" ps -a -q "$1" < /dev/null 2> /dev/null || true; } | sed -n '1p')
    if [[ -z $id ]]; then printf missing; return 0; fi
    "${DOCKER[@]}" inspect -f '{{.State.Status}}' "$id" 2> /dev/null || printf missing
}

# --- Config files ------------------------------------------------------

# The value after KEY= on the last line setting it, exactly as written
# (quotes included). Empty if the file or the key is missing.
env_raw() {
    local file=$1 key=$2 line
    [[ -f $file ]] || return 0
    line=$(grep -E "^[[:space:]]*$key=" "$file" | tail -n 1) || true
    # Preflight reports CRLF endings once; without this, the \r would also
    # make every value read from the file look wrong.
    line=${line%$'\r'}
    printf '%s' "${line#*=}"
}

# Set KEY=VALUE in an env file. VALUE goes in exactly as given, so quote
# it first if it needs quotes. Replaces the first line setting KEY, drops
# any later ones, or appends if there's none. Written atomically, mode 600.
env_set() {
    local file=$1 key=$2 value=$3 tmp
    tmp=$(mktemp "$file.XXXXXX")
    KEY=$key LINE="$key=$value" awk '
        $0 ~ "^[[:space:]]*" ENVIRON["KEY"] "=" {
            if (!done) print ENVIRON["LINE"]
            done = 1
            next
        }
        { print }
        END { if (!done) print ENVIRON["LINE"] }' "$file" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$file"
}

# A setting from backup.conf, as backup.sh will see it.
conf_get() {
    [[ -f backup/backup.conf ]] || return 0
    (
        # shellcheck source=/dev/null
        . ./backup/backup.conf > /dev/null 2>&1
        printf '%s' "${!1:-}"
    ) || true
}

random_hex() { openssl rand -hex 32; }

# IPv4 addresses of this machine as "address interface [dynamic]",
# leaving out loopback and Docker's own bridges.
host_addrs() {
    ip -4 -o addr show scope global 2> /dev/null | awk '
        $2 !~ /^(lo|docker|br-|veth)/ {
            split($4, a, "/")
            print a[1], $2, (/ dynamic /) ? "dynamic" : ""
        }'
}

is_host_addr() {
    [[ -n $1 ]] && host_addrs | awk -v want="$1" '$1 == want { found = 1 } END { exit !found }'
}

# The address this machine uses to reach the internet, usually its LAN IP.
# Empty, not an error, on a LAN-only host with no default route: it's only
# ever a suggestion (#47).
default_addr() {
    { ip -4 route get 1.1.1.1 2> /dev/null || true; } \
        | sed -n 's/.* src \([0-9.]*\).*/\1/p;T;q'
}

# IANA timezone names this machine knows, one per line. Empty if it has
# no timezone data, in which case any well-formed name is accepted and a
# wrong one shows up when the services refuse to start.
tz_names() {
    if [[ -z ${TZ_NAMES+set} ]]; then
        TZ_NAMES=
        if [[ $MILEAGE_PLATFORM != unraid ]]; then
            TZ_NAMES=$(timedatectl list-timezones 2> /dev/null) || TZ_NAMES=
        fi
        if [[ -z $TZ_NAMES && -f /usr/share/zoneinfo/tzdata.zi ]]; then
            TZ_NAMES=$(awk '$1 == "Z" { print $2 } $1 == "L" { print $3 }' \
                /usr/share/zoneinfo/tzdata.zi | sort -u)
        fi
    fi
    if [[ -n $TZ_NAMES ]]; then printf '%s\n' "$TZ_NAMES"; fi
}

tz_ok() {
    [[ $1 =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]] || return 1
    tz_names > /dev/null
    [[ -z $TZ_NAMES ]] || grep -qxF -- "$1" <<< "$TZ_NAMES"
}

# This machine's own timezone as an IANA name, or nothing.
host_tz() {
    local zone=
    if [[ $MILEAGE_PLATFORM != unraid ]]; then
        zone=$(timedatectl show -p Timezone --value 2> /dev/null) || zone=
    fi
    if [[ -z $zone ]]; then zone=$(cat /etc/timezone 2> /dev/null) || zone=; fi
    if [[ -z $zone ]]; then
        zone=$(readlink /etc/localtime 2> /dev/null | sed -n 's|.*/zoneinfo/||p') || zone=
    fi
    printf '%s' "$zone"
}

token_ok() { [[ $1 =~ ^[A-Za-z0-9._~+/=-]{32,}$ && $1 != replace-me* ]]; }
username_ok() { [[ $1 =~ ^[A-Za-z0-9._@-]{1,64}$ && $1 != replace-me* ]]; }
# A werkzeug hash: method$salt$hex, e.g. scrypt:32768:8:1$abc...$0f3...
hash_ok() { [[ $1 =~ ^[a-z0-9:]+\$[A-Za-z0-9]+\$[0-9a-f]+$ ]]; }

# --- Preflight ---------------------------------------------------------

os_release() {
    (
        # shellcheck source=/dev/null
        . /etc/os-release 2> /dev/null && printf '%s' "${!1:-}"
    ) || true
}

preflight() {
    section "Preflight"
    [[ $(uname -s) == Linux ]] || die "This sets up the server on Linux, and this is $(uname -s)."
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        (( EUID == 0 )) || die "Run install.sh as root on Unraid."
        [[ $SCRIPT_DIR == /mnt/* && $SCRIPT_DIR =~ ^/[-a-zA-Z0-9_./]+$ ]] \
            || die "Unraid install directory must be a simple path under /mnt/ on persistent storage."
        check_unraid_commands
        local install_mount install_fs
        install_mount=$(findmnt -n -o TARGET --target "$SCRIPT_DIR")
        install_fs=$(findmnt -n -o FSTYPE --target "$SCRIPT_DIR")
        [[ -n $install_mount && $install_mount != / && $install_fs != tmpfs && $install_fs != rootfs ]] \
            || die "The install pool is not mounted on persistent storage. Mount it before installing."
    elif (( EUID == 0 )); then
        die "Run this as your normal login user, not as root or with sudo." \
            "It asks for sudo itself when it needs it. The trip files, the" \
            "backups and the backup timer all belong to the user who runs it."
    fi
    ok "Running as $ME (UID $(id -u)) in $SCRIPT_DIR"

    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        OS_ID=unraid OS_CODENAME=
        ok "Unraid (Docker is managed by Unraid)"
    else
        OS_ID=$(os_release ID)
        OS_CODENAME=$(os_release VERSION_CODENAME)
        local os_name
        os_name=$(os_release PRETTY_NAME)
        case $OS_ID in
            debian | ubuntu) ok "${os_name:-$OS_ID}" ;;
            *) warn "${os_name:-This system} isn't Debian or Ubuntu" \
                "This script can't install Docker here, so it expects it installed already." ;;
        esac
    fi

    # COMMIT is optional: archives supply it, but hand copies can use VERSION alone.
    local f missing=()
    for f in VERSION stamp_version.py common/version.py common/settings.py docker-compose.yml .dockerignore .env.example viewer.env.example monitor.env.example sample.gpx \
        common/__init__.py common/diagnostics/{__init__.py,catalog.py,protocol.py,catalog.json} \
        mileage-ingest/{Dockerfile,app.py,diag_store.py,reset_high_water.py,requirements.txt} \
        mileage-viewer/{Dockerfile,app.py,geocoding.py,requirements.txt} \
        mileage-viewer/static/{leaflet.js,leaflet.css,gpx.js,endpoint-places.js} \
        mileage-monitor/{Dockerfile,monitor.py,gelf.py,prometheus.py,loki.py} ntfy/server.yml \
        backup/{backup.sh,restore.sh,lib.sh,backup.conf.example} \
        backup/mileage-backup@.{service,timer}; do
        if [[ ! -e $f ]]; then missing+=("$f"); fi
    done
    if (( ${#missing[@]} )); then
        die "Files missing from $SCRIPT_DIR: ${missing[*]}" \
            "Copy the whole server/ directory here (DEPLOY_DEBIAN.md Step 2)." \
            "scp -r can skip dotfiles like .env.example; copy any that are missing one by one."
    fi
    ok "All the server files are here"
    if [[ $MILEAGE_PLATFORM == unraid && ! -f backup/scheduled.sh ]]; then
        die "Files missing: backup/scheduled.sh. Copy the complete server release."
    fi

    # Files copied from a Windows checkout can arrive with CRLF line
    # endings. Those break the shell scripts and systemd units, and leave
    # a stray \r on the end of every value in the env files.
    local crlf=()
    for f in backup/*.sh backup/*.service backup/*.timer backup/backup.conf* \
        .env.example .env viewer.env.example viewer.env monitor.env.example monitor.env; do
        if [[ -f $f ]] && grep -q $'\r' "$f"; then crlf+=("$f"); fi
    done
    if (( ${#crlf[@]} == 0 )); then
        ok "Line endings are Unix-style"
    elif [[ $MODE == install ]]; then
        sed -i 's/\r$//' "${crlf[@]}"
        ok "Converted Windows line endings to Unix in ${crlf[*]}"
    else
        problem "Windows (CRLF) line endings in ${crlf[*]}" \
            "Run install.sh to convert them, or: sed -i 's/\\r\$//' <file>"
    fi

    if [[ $MILEAGE_PLATFORM == unraid && $(id -u) == 0 ]]; then
        ok "Root can back up data/; containers stay UID $CONTAINER_UID"
    elif [[ $(id -u) == "$CONTAINER_UID" ]]; then
        ok "Your UID matches the containers' (1000)"
    else
        DATA_SUDO=(sudo)
        warn "Your UID is $(id -u), not $CONTAINER_UID" \
            "The containers write trip files as UID $CONTAINER_UID, so data/ gets handed to" \
            "that UID, and the backup timer, which runs as you, can't read them." \
            "Backups are skipped below. DEPLOY_DEBIAN.md Steps 3 and 10 explain."
    fi

    local free_kb
    if [[ $MILEAGE_PLATFORM == unraid ]]; then plan_sudo; return 0; fi
    free_kb=$(df -Pk /var/lib 2> /dev/null | awk 'NR == 2 { print $4 }') || free_kb=
    if [[ -n $free_kb ]] && (( free_kb < 3 * 1024 * 1024 )); then
        warn "Only $((free_kb / 1024)) MB free under /var/lib" \
            "The Docker images need about 1 GB, and trips and backups grow from there."
    fi

    plan_sudo
}

# Work out what this run will need root for before anything runs. If
# anything, ask for sudo now, so the password prompt doesn't come halfway
# through a long image build; if nothing, don't ask (#22). The optional
# steps that need root are asked about in ask_root_options, once the
# answers they depend on are in. Each step still calls need_sudo itself,
# in case this misses something.
plan_sudo() {
    [[ $MILEAGE_PLATFORM != unraid ]] || return 0
    local reasons=() missing
    if [[ $MODE == install ]]; then
        missing=$(missing_packages)
        if [[ -n $missing ]]; then reasons+=("install packages: $missing"); fi
        if ! command -v docker > /dev/null; then
            reasons+=("install Docker")
        elif have_systemd \
            && [[ $(systemctl is-active docker 2> /dev/null) != active \
                || $(systemctl is-enabled docker 2> /dev/null) != enabled ]]; then
            reasons+=("start Docker, and have it start at boot")
        fi
    fi
    if command -v docker > /dev/null && ! docker info > /dev/null 2>&1; then
        reasons+=("use Docker until you log in again (this session isn't in the docker group)")
    fi
    if [[ $MODE == install ]] \
        && { [[ -d data && $(stat -c %u data) != "$CONTAINER_UID" ]] \
            || [[ ! -d data && $(id -u) != "$CONTAINER_UID" ]]; }; then
        reasons+=("hand data/ to UID $CONTAINER_UID, which the containers run as")
    fi
    if [[ $MODE == install && -d data && $(stat -c %u data) != "$(id -u)" && $(stat -c %a data) != 700 ]]; then
        reasons+=("make data/ private to its owner (mode 700)")
    fi
    if (( ${#DATA_SUDO[@]} )); then
        reasons+=("check and delete the test trip, whose files belong to UID $CONTAINER_UID")
    fi
    # Backups already set up, but the timer is off, runs another copy, or
    # its systemd files are out of date: setup_backups puts it right
    # without asking.
    if [[ $MODE == install && $(id -u) == "$CONTAINER_UID" ]] \
        && backups_configured && ! { timer_ok && timer_current && backup_units_current; }; then
        reasons+=("set up the daily backup timer")
    fi

    if (( ${#reasons[@]} == 0 )); then
        ok "Nothing needs root so far, so no sudo"
        return 0
    fi
    sudo_for "${reasons[@]}"
}

# sudo_for REASON...: list why, then make sure sudo works.
sudo_for() {
    [[ $MILEAGE_PLATFORM != unraid ]] || return 0
    local reason
    note "Root is needed to:"
    for reason; do note "  - $reason"; done
    need_sudo "Asking once now, not partway through"
    ok "sudo works"
}

# The optional steps that need root, asked about after Configuration
# (which chooses BIND_ADDR) and before the image build. A yes gets its
# sudo prompt here rather than after the build; a no means no prompt.
ask_root_options() {
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        note "No wait-online setup: Unraid starts Docker after networking and the array."
        return 0
    fi
    local reasons=() unit
    if [[ -n $BIND_ADDR ]] && have_systemd; then
        unit=$(wait_online_unit)
        if [[ -n $unit ]] && ! wait_online_enabled "$unit"; then
            ask_wait_online "$unit"
            if [[ $WANT_WAIT_ONLINE == yes ]]; then
                reasons+=("make Docker wait for the network at boot ($unit)")
            fi
        fi
    fi
    if [[ $(id -u) == "$CONTAINER_UID" ]] && ! backups_configured; then
        ask_backups
        if [[ $WANT_BACKUPS == yes ]]; then reasons+=("set up the daily backup timer"); fi
    fi
    if (( ${#reasons[@]} )) && [[ $SUDO_READY == no ]]; then sudo_for "${reasons[@]}"; fi
}

# --- Packages and Docker -----------------------------------------------

check_unraid_commands() {
    local cmd
    for cmd in curl jq openssl rsync ssh ssh-keygen ip flock findmnt stat date \
        awk sed grep sort cut tail head cat cmp readlink realpath df dirname \
        mktemp install chmod chown mkdir mv rm cp touch id uname sleep nice timeout crontab; do
        command -v "$cmd" > /dev/null || die "Missing required Unraid command: $cmd" \
            "Restore the stock Unraid tools (OpenSSH for SSH commands); then rerun install.sh."
    done
    stat -c %u "$SCRIPT_DIR" > /dev/null 2>&1 || die "GNU stat -c is required. Restore Unraid's coreutils."
    date -d '2000-01-01T00:00:00Z' +%s > /dev/null 2>&1 || die "GNU date -d is required. Restore Unraid's coreutils."
    command -v "${MILEAGE_UPDATE_CRON:-/usr/local/sbin/update_cron}" > /dev/null \
        || die "Unraid update_cron is missing. Restore the Dynamix scheduler."
}

unraid_docker_enabled() {
    if grep -Eq '^DOCKER_ENABLED=("yes"|yes)[[:space:]]*$' "${MILEAGE_UNRAID_DOCKER_CFG:-/boot/config/docker.cfg}" 2> /dev/null; then
        ok "Docker is enabled in Unraid Settings > Docker"
    else
        warn "DOCKER_ENABLED isn't yes in Unraid's docker.cfg" "Enable Docker in Settings > Docker, then start the array."
    fi
}

setup_unraid_docker() {
    local version root path free
    command -v docker > /dev/null || die "Docker is missing. Enable Docker in Settings > Docker and start the array."
    version=$(docker compose version --short 2> /dev/null) \
        || die "docker compose is missing. Install the Compose Manager Plus plugin."
    version=${version#v}
    if [[ ! $version =~ ^([0-9]+)\. ]] || (( BASH_REMATCH[1] < 2 )); then
        die "Compose $version is too old; install Compose Manager Plus (Compose v2 or newer)."
    fi
    docker info > /dev/null 2>&1 || die "Docker isn't answering. Start the array and enable Docker in Settings > Docker."
    DOCKER=(docker) AS_ME=() DATA_SUDO=()
    ok "Unraid Docker is answering, Compose $version"
    root=$(docker info --format '{{.DockerRootDir}}')
    [[ $root == /* && -d $root ]] || die "Cannot find Docker storage. Check Settings > Docker."
    for path in "$root" "$SCRIPT_DIR"; do
        free=$(df -Pk -- "$path" | awk 'NR == 2 {print $4}')
        [[ $free =~ ^[0-9]+$ ]] || die "Cannot measure free space at $path. Check df -h."
        if (( free < 3 * 1024 * 1024 )); then
            warn "Only $((free / 1024)) MB free at $path" "Free space here; Docker storage and the install disk are checked separately."
        else
            ok "$((free / 1024)) MB free at $path"
        fi
    done
}

apt_get() {
    need_sudo
    as_root env DEBIAN_FRONTEND=noninteractive apt-get -q -y "$@" < /dev/null
}

WANTED_PACKAGES=(ca-certificates curl jq openssl openssh-client rsync iproute2 python3)

have_apt() { [[ $MILEAGE_PLATFORM != unraid ]] && command -v apt-get > /dev/null && command -v dpkg-query > /dev/null; }
have_systemd() { [[ $MILEAGE_PLATFORM != unraid && -d /run/systemd/system ]]; }

# The WANTED_PACKAGES that aren't installed, space-separated. Empty
# without apt, where install_packages only checks for the commands.
missing_packages() {
    local p missing=()
    have_apt || return 0
    for p in "${WANTED_PACKAGES[@]}"; do
        if [[ $(dpkg-query -W -f='${Status}' "$p" 2> /dev/null) != *'ok installed' ]]; then
            missing+=("$p")
        fi
    done
    printf '%s' "${missing[*]}"
}

install_packages() {
    if [[ $MILEAGE_PLATFORM == unraid ]]; then check_unraid_commands; return 0; fi
    local wanted=("${WANTED_PACKAGES[@]}")
    local p missing=()
    if have_apt; then
        read -r -a missing <<< "$(missing_packages)"
        if (( ${#missing[@]} )) && [[ $MODE == check ]]; then
            problem "Packages not installed: ${missing[*]}" "Run install.sh to install them."
            return 0
        fi
        if (( ${#missing[@]} )); then
            note "Installing ${missing[*]}"
            apt_get update
            apt_get install "${missing[@]}"
        fi
        ok "Packages installed: ${wanted[*]}"
    else
        for p in curl jq openssl rsync ssh ip flock python3; do
            command -v "$p" > /dev/null || missing+=("$p")
        done
        if (( ${#missing[@]} )); then
            die "Commands not found: ${missing[*]}" \
                "Install them with your package manager, then run this again."
        fi
        ok "Commands available: curl jq openssl rsync ssh ip flock python3"
    fi
}

# Docker's own packages, from Docker's apt repository, exactly as in
# DEPLOY_DEBIAN.md Step 1. Debian's docker.io doesn't ship Compose v2.
install_docker() {
    [[ $MILEAGE_PLATFORM != unraid ]] || die "Enable Docker in Settings > Docker and start the array."
    case $OS_ID in
        debian | ubuntu) ;;
        *) die "Docker isn't installed, and this script only installs it on Debian or Ubuntu." \
            "Install Docker Engine and its Compose plugin (https://docs.docker.com/engine/install/)," \
            "then run this again." ;;
    esac
    [[ -n $OS_CODENAME ]] || die "Couldn't tell which $OS_ID release this is (no VERSION_CODENAME in /etc/os-release)."
    need_sudo
    if ! command -v gpg > /dev/null; then apt_get update; apt_get install gnupg; fi
    note "Installing Docker from Docker's apt repository"
    # A second entry for the same repository with a different key path
    # makes apt refuse to update, so reuse one set up some other way
    # (Docker's newer instructions write docker.sources).
    if grep -rqs --exclude=docker.list download.docker.com /etc/apt/sources.list /etc/apt/sources.list.d/; then
        note "Docker's repository is already in your apt sources; using that"
    else
        as_root install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" \
            | as_root gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
        as_root chmod a+r /etc/apt/keyrings/docker.gpg
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$OS_ID" "$OS_CODENAME" \
            | as_root tee /etc/apt/sources.list.d/docker.list > /dev/null
        as_root chmod 644 /etc/apt/sources.list.d/docker.list
    fi
    apt_get update
    apt_get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    as_root systemctl enable --now docker
}

setup_docker() {
    section "Docker"
    install_packages
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        setup_unraid_docker
        select_python
        return 0
    fi

    local fresh=no
    if ! command -v docker > /dev/null; then
        [[ $MODE == install ]] || die "Docker isn't installed." "Run install.sh to install it."
        install_docker
        fresh=yes
    fi
    local compose_version
    if ! compose_version=$(docker compose version --short 2> /dev/null); then
        die "Docker is installed, but not its Compose v2 plugin (the docker compose command)." \
            "That's usually Debian's own docker.io package. Remove it with" \
            "sudo apt remove docker.io docker-compose, then run this again to get Docker's packages."
    fi
    compose_version=${compose_version#v}
    if [[ ! $compose_version =~ ^([0-9]+)\. ]] || (( BASH_REMATCH[1] < 2 )); then
        die "Docker Compose $compose_version is too old; this needs v2 or newer."
    fi
    ok "$(docker --version | cut -d, -f1), Compose $compose_version"

    # Docker's daemon has to be running and start at boot.
    if have_systemd && [[ $(systemctl is-active docker 2> /dev/null) != active ]]; then
        [[ $MODE == install ]] || die "The Docker service isn't running." "sudo systemctl enable --now docker"
        need_sudo
        as_root systemctl enable --now docker
    fi

    # Can this session talk to Docker? Being in the docker group only
    # counts after logging in again, so on a first install it can't yet.
    # sudo -u $ME starts a process with the new group, so commands go
    # through that until the next login.
    if docker info > /dev/null 2>&1; then
        ok "You can use Docker without sudo"
    else
        need_sudo
        if [[ " $(id -nG "$ME") " != *" docker "* ]]; then
            if [[ $MODE == install ]]; then
                as_root usermod -aG docker "$ME"
                ok "Added $ME to the docker group"
            else
                problem "$ME isn't in the docker group" \
                    "The backup timer runs as you and needs it: sudo usermod -aG docker $ME" \
                    "then log out and back in."
            fi
        fi
        if [[ " $(id -nG "$ME") " == *" docker "* ]]; then
            AS_ME=(sudo -u "$ME" -H)
        else
            AS_ME=(sudo)
        fi
        DOCKER=("${AS_ME[@]}" docker)
        RELOGIN=yes
        "${DOCKER[@]}" info > /dev/null 2>&1 \
            || die "Docker isn't answering." "Check it with: sudo systemctl status docker"
        ok "Docker works (through sudo until you log in again)"
    fi

    select_python
    if [[ $fresh == yes ]]; then
        if ! "${DOCKER[@]}" pull hello-world > /dev/null; then
            die "Docker couldn't download hello-world (output above)." \
                "It needs to reach Docker Hub; check this server's internet access and DNS."
        fi
        if "${DOCKER[@]}" run --rm hello-world > /dev/null; then
            ok "Docker can download and run a container (hello-world)"
        else
            die "Docker couldn't run hello-world (output above)." \
                "The image downloaded successfully; check Docker's error above for the cause."
        fi
    fi
}

# --- Configuration -----------------------------------------------------

setup_data_dir() {
    [[ ! -L data && ( ! -e data || -d data ) ]] || die "data/ must be a real directory, never a symlink."
    if [[ ! -d data ]]; then
        [[ $MODE == install ]] || { problem "There's no data/ directory" "Run install.sh."; return 0; }
        mkdir data
    fi
    if [[ $(stat -c %u data) != "$CONTAINER_UID" ]]; then
        if [[ $MODE == install ]]; then
            need_sudo
            as_root chown -hR -P "$CONTAINER_UID:$CONTAINER_UID" -- data
        else
            problem "data/ doesn't belong to UID $CONTAINER_UID, so uploads will fail" \
                "Run install.sh to repair data/ ownership safely."
            return 0
        fi
    fi
    ok "data/ belongs to UID $CONTAINER_UID, which the containers run as"
    # Trip file names are their start times, so other accounts on this
    # machine shouldn't be able to list the folder. The containers and the
    # login that runs the backups all use UID 1000, so nothing else changes.
    # Existing installs get this on the next run too.
    if [[ $(stat -c %a data) != 700 ]]; then
        if [[ $MODE == install ]]; then
            if [[ $(stat -c %u data) == "$(id -u)" ]]; then
                chmod 700 data
            else
                need_sudo
                as_root chmod 700 data
            fi
            ok "data/ is private (mode 700)"
        else
            warn "data/ can be listed by other accounts on this machine (mode $(stat -c %a data))" \
                "Trip file names show when each drive started. Run install.sh to make it private (mode 700)."
        fi
    else
        ok "data/ is private (mode 700)"
    fi
}

setup_env() {
    if [[ ! -f .env ]]; then
        [[ $MODE == install ]] || { problem ".env is missing" "Run install.sh, or see DEPLOY_DEBIAN.md Step 4a."; return 0; }
        install -m 600 .env.example .env
        note "Created .env from .env.example"
    fi
    if [[ $MILEAGE_PLATFORM == unraid && $MODE == install ]]; then
        env_set .env RESTART_POLICY always
    fi

    local token
    token=$(env_raw .env INGEST_AUTH_TOKEN)
    if [[ $MODE == install && ( -z $token || $token == replace-me* ) ]]; then
        token=$(random_hex)
        env_set .env INGEST_AUTH_TOKEN "$token"
        ok "Generated a new upload token in .env"
    elif token_ok "$token"; then
        ok "The upload token in .env looks right (${#token} characters)"
    else
        problem "INGEST_AUTH_TOKEN in .env isn't usable" \
            "It needs at least 32 characters, with no quotes, spaces or comments on the line." \
            "openssl rand -hex 32 makes one. The firmware's secrets.h needs the same value."
    fi

    setup_bind_addr
    setup_viewer_proxy
    setup_display_tz

    if [[ $(stat -c %a .env) != 600 ]]; then
        if [[ $MODE == install ]]; then
            chmod 600 .env
        else
            warn ".env can be read by other users on this machine" "chmod 600 $SCRIPT_DIR/.env"
        fi
    fi
}

setup_bind_addr() {
    BIND_ADDR=$(env_raw .env BIND_ADDR)
    if [[ -n $BIND_ADDR ]] && is_host_addr "$BIND_ADDR"; then
        ok "Default port binding is $BIND_ADDR only (BIND_ADDR in .env; viewer may override)"
        dhcp_reminder
        return 0
    fi
    if [[ $MODE == check ]]; then
        if [[ -n $BIND_ADDR ]]; then
            problem "BIND_ADDR=$BIND_ADDR in .env isn't an address this server has" \
                "Docker can't publish the ports there, so the containers won't start." \
                "Compare with ip -4 addr show, fix .env or the DHCP reservation, then: docker compose up -d"
        else
            warn "BIND_ADDR isn't set, so ports without a viewer override are open on every interface" \
                "UFW doesn't filter Docker's ports. Set BIND_ADDR in .env (DEPLOY_DEBIAN.md Step 7)."
        fi
        return 0
    fi

    if [[ -n $BIND_ADDR ]]; then
        note "BIND_ADDR=$BIND_ADDR in .env isn't an address this server has; choose again"
    fi
    local addrs suggested answer
    addrs=$(host_addrs | awk '{ printf "%s%s (%s)", sep, $1, $2; sep = ", " }')
    suggested=$(default_addr)
    if ! is_host_addr "$suggested"; then suggested=; fi
    note "The T-Beam uploads to this server's LAN address. BIND_ADDR limits service ports" \
        "to that address (the viewer may override it). UFW can't do this for Docker's ports." \
        "This server has: ${addrs:-no LAN address found}"
    while :; do
        ask answer BIND_ADDR "LAN address for the ports (or \"all\")" "$suggested"
        if [[ $answer == all ]] || is_host_addr "$answer"; then break; fi
        if [[ -n ${INSTALL_BIND_ADDR+set} ]]; then
            die "INSTALL_BIND_ADDR=$answer isn't an address of this server."
        fi
        if [[ -z $answer ]]; then
            note "Type one of the addresses above, or \"all\"."
        else
            note "$answer isn't one of this server's addresses."
        fi
    done
    if [[ $answer == all ]]; then
        BIND_ADDR=
        env_set .env BIND_ADDR ""
        warn "Ports without a viewer override will be open on every interface" \
            "Set BIND_ADDR in .env later to limit them (DEPLOY_DEBIAN.md Step 7)."
    else
        BIND_ADDR=$answer
        env_set .env BIND_ADDR "$BIND_ADDR"
        ok "Default port binding will be $BIND_ADDR only (viewer may override)"
        dhcp_reminder
    fi
}

# Read opt-in proxy settings without adding installer questions or rewriting
# them. env_set preserves every other key; setup.sh preserves the whole .env.
setup_viewer_proxy() {
    local trusted secure error addr
    VIEWER_BIND_ADDR=$(env_raw .env VIEWER_BIND_ADDR)
    trusted=$(env_raw .env VIEWER_TRUSTED_PROXIES)
    secure=$(env_raw .env VIEWER_COOKIE_SECURE)
    if ! error=$(host_python proxy - "$trusted" "$secure" <<'PY'
import sys
from common.settings import trusted_proxies, viewer_cookie_secure
try:
    trusted_proxies(sys.argv[1])
    viewer_cookie_secure(sys.argv[2])
except ValueError as exc:
    print(exc)
    sys.exit(1)
PY
    ); then
        problem "Invalid viewer proxy configuration: $error" \
            "Fix .env, then run docker compose up -d. See DEPLOY_DEBIAN.md Step 7."
    fi
    VIEWER_COOKIE_SECURE=${secure,,}
    VIEWER_COOKIE_SECURE=${VIEWER_COOKIE_SECURE:-false}
    if [[ -n $VIEWER_BIND_ADDR ]]; then
        if viewer_host_addr "$VIEWER_BIND_ADDR"; then
            ok "Viewer port 8091 is published on $VIEWER_BIND_ADDR only (VIEWER_BIND_ADDR in .env)"
        else
            problem "VIEWER_BIND_ADDR isn't an address this server has or loopback (127.0.0.1/::1)" \
                "Compare with ip addr show, fix .env, then: docker compose up -d"
        fi
    else
        note "Viewer port 8091 uses ${BIND_ADDR:-all interfaces} (VIEWER_BIND_ADDR is unset)"
    fi
    addr=${VIEWER_BIND_ADDR:-$BIND_ADDR}
    if [[ $VIEWER_COOKIE_SECURE == true && $addr != 127.0.0.1 && $addr != ::1 ]]; then
        warn "VIEWER_COOKIE_SECURE=true with viewer port 8091 on a non-loopback address" \
            "Plain-HTTP logins on 8091 cannot keep a browser session. Use the proxy's HTTPS name."
    fi
}

viewer_host_addr() {
    [[ $1 == 127.0.0.1 || $1 == ::1 ]] && return 0
    # Compare canonical addresses so IPv6 spelling differences are harmless.
    ip -o addr show 2> /dev/null | host_python address -c '
import ipaddress, sys
try:
    wanted = ipaddress.ip_address(sys.argv[1])
    addresses = [line.split()[3].split("/")[0] for line in sys.stdin if len(line.split()) > 3]
    valid = any(wanted == ipaddress.ip_address(addr) for addr in addresses)
except ValueError:
    valid = False
sys.exit(0 if valid else 1)' "$1"
}

# Brackets are required around IPv6 literals in URLs and Docker port output.
http_url() {
    local addr=$1 port=$2
    [[ $addr != 0.0.0.0 ]] || addr=127.0.0.1
    if [[ $addr == *:* ]]; then addr=[$addr]; fi
    printf 'http://%s:%s' "$addr" "$port"
}

set_service_urls() {
    INGEST_URL=$(http_url "${BIND_ADDR:-127.0.0.1}" 8090)
    VIEWER_URL=$(http_url "${VIEWER_BIND_ADDR:-${BIND_ADDR:-127.0.0.1}}" 8091)
}

# The zone trip times are shown in, which also decides a trip's month
# folder. Both services read DISPLAY_TZ from .env and won't start without
# it, so nobody gets a zone picked for them.
setup_display_tz() {
    local zone list_hint='timedatectl list-timezones'
    if [[ $MILEAGE_PLATFORM == unraid ]]; then list_hint='the names in /usr/share/zoneinfo/tzdata.zi'; fi
    zone=$(env_raw .env DISPLAY_TZ)
    if [[ -n $zone ]] && tz_ok "$zone"; then
        ok "Trips are filed and shown in $zone (DISPLAY_TZ in .env)"
        return 0
    fi
    if [[ $MODE == check ]]; then
        if [[ -n $zone ]]; then
            problem "DISPLAY_TZ=$zone in .env isn't a timezone name, so neither service will start" \
                "Use an IANA name like America/Chicago (see $list_hint)," \
                "then: docker compose up -d"
        else
            problem "DISPLAY_TZ isn't set in .env, so neither service will start" \
                "Set it to your timezone, e.g. DISPLAY_TZ=America/Chicago, then: docker compose up -d"
        fi
        return 0
    fi

    if [[ -n $zone ]]; then
        note "DISPLAY_TZ=$zone in .env isn't a timezone name; choose again"
    fi
    # A server clock on UTC says nothing about where the car is, so only
    # a real local zone is offered as the default.
    local suggested answer exact matches
    suggested=$(host_tz)
    case $suggested in UTC | Etc/* | Universal | Zulu) suggested= ;; esac
    note "Trip times are shown and exported in the timezone you pick here, and it" \
        "decides which month a trip is filed under, so pick the one you drive in." \
        "Give its IANA name, like America/Chicago or Europe/London, or type part" \
        "of one (a city, say) to search."
    tz_names > /dev/null
    while :; do
        ask answer DISPLAY_TZ "Timezone" "$suggested"
        # Accept any capitalisation of a real name, e.g. america/chicago.
        exact=$(grep -ixF -m 1 -- "$answer" <<< "$TZ_NAMES") || exact=
        answer=${exact:-$answer}
        if [[ -n $answer ]] && tz_ok "$answer"; then break; fi
        if [[ -n ${INSTALL_DISPLAY_TZ+set} ]]; then
            die "INSTALL_DISPLAY_TZ=$answer isn't a timezone name." \
                "Use an IANA name like America/Chicago; see $list_hint."
        fi
        if [[ -z $answer ]]; then
            note "There's no default timezone; type yours, or part of its name."
            continue
        fi
        matches=$(grep -iF -- "$answer" <<< "$TZ_NAMES") || matches=
        if [[ -z $TZ_NAMES ]]; then
            note "\"$answer\" isn't a timezone name. They look like America/Chicago."
        elif [[ -z $matches ]]; then
            note "No timezone name contains \"$answer\". Try a nearby big city, e.g. denver."
        elif [[ $matches != *$'\n'* ]]; then
            suggested=$matches
            note "That matches $matches; press Enter to use it."
        else
            note "\"$answer\" matches these (up to 20 shown):"
            head -n 20 <<< "$matches" | indent
        fi
    done
    env_set .env DISPLAY_TZ "$answer"
    ok "Trips will be filed and shown in $answer"
}

dhcp_reminder() {
    if host_addrs | awk -v want="$BIND_ADDR" '$1 == want && $3 == "dynamic" { found = 1 } END { exit !found }'; then
        note "$BIND_ADDR came from DHCP. Reserve it for this server in your router, since" \
            "the firmware and the port binding both stop working if it changes."
    fi
}

# viewer.env needs to exist before Compose will do anything, even build.
# The admin login itself is set after the build (setup_viewer_login), because
# the password hash is made with the viewer image's own werkzeug.
setup_viewer_env() {
    if [[ ! -f viewer.env ]]; then
        [[ $MODE == install ]] || { problem "viewer.env is missing" "Run install.sh, or see DEPLOY_DEBIAN.md Step 4b."; return 0; }
        install -m 600 viewer.env.example viewer.env
        note "Created viewer.env from viewer.env.example"
    fi

    local key
    key=$(env_raw viewer.env VIEWER_SECRET_KEY)
    if [[ ${#key} -ge 32 && $key != replace-me* ]]; then
        ok "The viewer's session key is set"
    elif [[ $MODE == install ]]; then
        env_set viewer.env VIEWER_SECRET_KEY "$(random_hex)"
        ok "Generated the viewer's session key"
    else
        problem "VIEWER_SECRET_KEY in viewer.env is missing or too short" \
            "Set it to the output of: openssl rand -hex 32"
    fi

    # An unquoted or double-quoted hash loses its $ parts to Compose's
    # variable expansion. The text in the file is still intact, so
    # single-quoting it is all the fix it needs.
    local raw value
    raw=$(env_raw viewer.env VIEWER_PASSWORD_HASH)
    value=${raw#\"}
    value=${value%\"}
    if [[ $raw != \'*\' ]] && hash_ok "$value"; then
        if [[ $MODE == install ]]; then
            env_set viewer.env VIEWER_PASSWORD_HASH "'$value'"
            ok "Single-quoted VIEWER_PASSWORD_HASH, so Compose leaves its \$ signs alone"
        else
            problem "VIEWER_PASSWORD_HASH in viewer.env isn't single-quoted" \
                "Compose will eat its \$ signs. Wrap it in single quotes, then: docker compose up -d"
        fi
    fi

    if [[ $(stat -c %a viewer.env) != 600 ]]; then
        if [[ $MODE == install ]]; then
            chmod 600 viewer.env
        else
            warn "viewer.env can be read by other users on this machine" "chmod 600 $SCRIPT_DIR/viewer.env"
        fi
    fi

    if [[ $MODE == check ]]; then
        if viewer_login_set; then
            ok "The admin login is set up for $(env_raw viewer.env VIEWER_USERNAME)"
        else
            problem "The admin login in viewer.env isn't set up" \
                "Run install.sh to set it, or see DEPLOY_DEBIAN.md Step 4b."
        fi
    fi
}

configure() {
    section "Configuration"
    setup_data_dir
    setup_env
    setup_viewer_env
}

# --- Build and start ---------------------------------------------------

build_images() {
    section "Build and start"
    BUILD_SERVER_VERSION=$(host_python stamp "$SCRIPT_DIR/stamp_version.py")
    note "Server build version: $BUILD_SERVER_VERSION"
    note "Building the images. The first build downloads Python and takes a few minutes."
    if ! compose build < /dev/null 2>&1 | indent; then
        die "Building the images failed (output above)." \
            "The build downloads from Docker Hub and PyPI; if names didn't resolve, check this server's DNS."
    fi
    ok "Built the mileage-ingest and mileage-viewer images"
}

viewer_login_set() {
    local user raw
    user=$(env_raw viewer.env VIEWER_USERNAME)
    raw=$(env_raw viewer.env VIEWER_PASSWORD_HASH)
    username_ok "$user" && [[ $raw == \'*\' ]] && hash_ok "${raw:1:${#raw}-2}"
}

new_password() {
    local again
    while :; do
        ask_secret VIEWER_PASSWORD VIEWER_PASSWORD "Admin password (at least 8 characters)"
        if (( ${#VIEWER_PASSWORD} < 8 )); then
            if [[ -n ${INSTALL_VIEWER_PASSWORD+set} ]]; then
                die "INSTALL_VIEWER_PASSWORD is shorter than 8 characters."
            fi
            note "That's too short."
            continue
        fi
        if [[ -n ${INSTALL_VIEWER_PASSWORD+set} ]]; then return 0; fi
        ask_secret again VIEWER_PASSWORD_AGAIN "Same password again"
        if [[ $VIEWER_PASSWORD == "$again" ]]; then return 0; fi
        note "Those didn't match. Try again."
    done
}

# The hash is made by the viewer image's own werkzeug, the same code that
# checks it at login. The password goes in on stdin, so it never lands in
# a file, an environment variable or a process list.
PY_HASH='import sys
from werkzeug.security import generate_password_hash
print(generate_password_hash(sys.stdin.read()))'

setup_viewer_login() {
    local user hash errors everyday_users name _state matching clash_message
    user=$(env_raw viewer.env VIEWER_USERNAME)
    if viewer_login_set; then
        if confirm KEEP_LOGIN "The admin login for $user is already set up. Keep it?" y; then
            ok "Kept the admin login for $user"
            return 0
        fi
    fi

    note "Choose a username and password for the web viewer's admin login. This is the" \
        "maintainer's login (it removes trips and manages logins), so it lives in" \
        "viewer.env, and only a scrypt hash of the password is stored. The login for" \
        "everyday use comes next."
    errors=$(mktemp)
    TMP_FILES+=("$errors")
    if ! everyday_users=$(manage_users list < /dev/null 2> "$errors"); then
        indent < "$errors"
        die "Couldn't list the everyday logins before changing the admin login (output above)."
    fi
    # Suggest the name already in viewer.env, unless it's just the example's.
    local default=admin
    if username_ok "$user" && [[ $user != "$(env_raw viewer.env.example VIEWER_USERNAME)" ]]; then
        default=$user
    fi
    while :; do
        ask user VIEWER_USER "Admin username" "$default"
        if username_ok "$user"; then
            matching=
            while IFS=$'\t' read -r name _state; do
                [[ -n $name ]] || continue
                if [[ ${name,,} == "${user,,}" ]]; then
                    matching=$name
                    break
                fi
            done <<< "$everyday_users"
            if [[ -z $matching ]]; then break; fi
            clash_message="Admin username $user matches an everyday login ($matching). Pick another name, or remove that everyday login on /admin first."
            if [[ -n ${INSTALL_VIEWER_USER+set} ]]; then die "$clash_message"; fi
            note "$clash_message"
            continue
        fi
        if [[ -n ${INSTALL_VIEWER_USER+set} ]]; then die "INSTALL_VIEWER_USER=$user isn't a usable username."; fi
        note "Use letters, digits and . _ @ - only."
    done
    new_password

    errors=$(mktemp)
    TMP_FILES+=("$errors")
    if ! hash=$(printf '%s' "$VIEWER_PASSWORD" \
        | compose run --rm --no-deps -T mileage-viewer python -c "$PY_HASH" 2> "$errors"); then
        indent < "$errors"
        die "Couldn't make the password hash (output above)."
    fi
    hash_ok "$hash" || die "The password hash came out in an unexpected format."
    env_set viewer.env VIEWER_USERNAME "$user"
    env_set viewer.env VIEWER_PASSWORD_HASH "'$hash'"
    ok "Admin login set for $user"
}

# --- Everyday login ----------------------------------------------------

# Run manage_users.py inside the viewer image. This works before the
# services start (the image and the viewer-db volume are enough; the script
# creates its table on first use), and it's how the everyday logins are
# made. Passwords go in on stdin, never as arguments or environment.
manage_users() {
    compose run --rm --no-deps -T mileage-viewer python manage_users.py "$@"
}

# Is there someone to ask? Tests replace this.
have_terminal() { [[ -t 0 ]]; }

# list_everyday running|image [errors-file]: fill EVERYDAY_LOGINS with one
# label per everyday login, "name" or "name (disabled)". "running" asks the
# running viewer; "image" starts a one-off container, so it also works
# before the services start. Fails if the list can't be had.
list_everyday() {
    local how=$1 out name state
    EVERYDAY_LOGINS=()
    if [[ $how == running ]]; then
        out=$(compose exec -T mileage-viewer python manage_users.py list < /dev/null 2> /dev/null) || return 1
    else
        out=$(manage_users list < /dev/null 2> "$2") || return 1
    fi
    while IFS=$'\t' read -r name state; do
        [[ -n $name ]] || continue
        if [[ $state == disabled ]]; then
            EVERYDAY_LOGINS+=("$name (disabled)")
        else
            EVERYDAY_LOGINS+=("$name")
        fi
    done <<< "$out"
}

# The everyday logins as "a, b".
everyday_text() {
    local text='' item
    for item in "${EVERYDAY_LOGINS[@]}"; do text+="${text:+, }$item"; done
    printf '%s' "$text"
}

# Optional: a second login for whoever annotates trips day to day. It can't
# remove trips or manage logins, so the admin's password isn't shared around.
setup_everyday_login() {
    local errors question="Add an everyday login for the person who'll annotate trips?" default=y
    errors=$(mktemp)
    TMP_FILES+=("$errors")
    if ! list_everyday image "$errors"; then
        indent < "$errors"
        die "Couldn't list the everyday logins (output above)." \
            "The viewer image should contain manage_users.py; rebuild it: docker compose build"
    fi
    note "An everyday login is for whoever annotates trips. It can't remove trips or" \
        "manage logins; the admin does that on the viewer's /admin page, where more" \
        "everyday logins can be added or turned off later."
    if (( ${#EVERYDAY_LOGINS[@]} )); then
        ok "Everyday logins already set up, and kept: $(everyday_text)"
        question="Add another everyday login?"
        default=n
    fi
    # An optional step must never fail an unattended install: with nobody to
    # ask and no preset, skip it.
    if [[ -z ${INSTALL_ADD_EDITOR+set} ]] && ! have_terminal; then
        note "Skipped adding an everyday login: there's no terminal to ask in." \
            "Add one on the viewer's /admin page, or run install.sh again with" \
            "INSTALL_ADD_EDITOR=y INSTALL_EDITOR_USER=... INSTALL_EDITOR_PASSWORD=..."
        return 0
    fi
    if confirm ADD_EDITOR "$question" "$default"; then
        add_everyday_login "$errors"
        list_everyday image "$errors" || true
    else
        note "No everyday login added."
    fi
}

# add_everyday_login errors-file
add_everyday_login() {
    local errors=$1 admin user pw rc
    admin=$(env_raw viewer.env VIEWER_USERNAME)
    while :; do
        while :; do
            ask user EDITOR_USER "Everyday username"
            if username_ok "$user" && [[ ${user,,} != "${admin,,}" ]]; then break; fi
            if [[ -n ${INSTALL_EDITOR_USER+set} ]]; then
                die "INSTALL_EDITOR_USER=$user isn't a usable username." \
                    "Use letters, digits and . _ @ - only, and not the admin's name ($admin)."
            fi
            note "Use letters, digits and . _ @ - only, and not the admin's name ($admin)."
        done
        while :; do
            ask_new_secret pw EDITOR_PASSWORD "Everyday password (at least 8 characters)" 8
            if (( ${#pw} <= 1024 )); then break; fi
            if [[ -n ${INSTALL_EDITOR_PASSWORD+set} ]]; then
                die "INSTALL_EDITOR_PASSWORD is longer than 1024 characters."
            fi
            note "That's too long; use at most 1024 characters."
        done

        rc=0
        printf '%s' "$pw" | manage_users add "$user" > /dev/null 2> "$errors" || rc=$?
        case $rc in
            0)
                ok "Everyday login added for $user"
                # Kept for the login test only; it was made by this run.
                EVERYDAY_USER=$user
                EVERYDAY_PASSWORD=$pw
                return 0
                ;;
            5)
                note "An everyday login named $user already exists, so it was kept as it is" \
                    "(its password wasn't changed). Reset it on the viewer's /admin page."
                return 0
                ;;
            3)
                indent < "$errors"
                if [[ -n ${INSTALL_EDITOR_USER+set} || -n ${INSTALL_EDITOR_PASSWORD+set} ]]; then
                    die "The viewer turned down the everyday login for $user (output above)." \
                        "Check INSTALL_EDITOR_USER and INSTALL_EDITOR_PASSWORD."
                fi
                note "The viewer turned that down. Try again."
                ;;
            *)
                indent < "$errors"
                die "Couldn't create the everyday login for $user (output above)."
                ;;
        esac
    done
}

# Fail early if something else already listens where a port has to go.
# With a profile, $svc is a Compose service (see service_state).
check_port_free() {
    local svc=$1 port=$2 profile=${3-} taken addr=$BIND_ADDR
    if [[ $svc == mileage-viewer ]]; then addr=${VIEWER_BIND_ADDR:-$BIND_ADDR}; fi
    if [[ -n $profile ]]; then
        if [[ $(service_state "$svc" "$profile") == running ]]; then return 0; fi
    elif [[ $(container_state "$svc") == running ]]; then
        return 0
    fi
    if ! command -v ss > /dev/null; then
        warn "ss is unavailable; Docker will check port $port when starting $svc" \
            "If startup reports a port conflict, stop or reconfigure the program using that port."
        return 0
    fi
    taken=$(ss -Hltn "sport = :$port" 2> /dev/null | awk -v want="$addr" '{
        addr = $4
        sub(/:[0-9]+$/, "", addr)
        gsub(/[][]/, "", addr)
        if (want == "" || addr == want || addr == "0.0.0.0" || addr == "*" || addr == "::") print $4
    }') || taken=
    if [[ -n $taken ]]; then
        die "Port $port is already in use (${taken//$'\n'/, }), so $svc can't start." \
            "See what has it: ss -tlnp 'sport = :$port' (run as root for process names)" \
            "Stop or reconfigure that program to free the port, then rerun install.sh."
    fi
}

start_services() {
    local errors
    if ! errors=$(compose config --quiet < /dev/null 2>&1); then
        printf '%s\n' "$errors" | indent
        die "Compose can't read docker-compose.yml with this .env (above)."
    fi
    check_port_free mileage-ingest 8090
    check_port_free mileage-viewer 8091
    if ! compose up -d < /dev/null 2>&1 | indent; then
        die "docker compose up failed (output above)."
    fi
    ok "Started both services"
}

# Wait for Docker's healthcheck to pass. Gives up early if the container
# stops or restarts, which is what a service refusing its config does.
wait_healthy() {
    local svc=$1 deadline=$((SECONDS + 180)) state status health restarts first='' waiting=no
    while :; do
        state=$("${DOCKER[@]}" inspect -f \
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' \
            "$svc" 2> /dev/null) || state="missing none 0"
        read -r status health restarts <<< "$state"
        first=${first:-$restarts}
        if [[ $status == running && $health == healthy ]]; then
            ok "$svc is healthy"
            return 0
        fi
        if [[ $status == missing ]]; then
            problem "$svc doesn't exist" "Start it with: docker compose up -d"
            return 1
        fi
        if [[ $status != running || $health == unhealthy || $restarts != "$first" ]] || (( SECONDS >= deadline )); then
            service_failed "$svc" "$status" "$health"
            return 1
        fi
        if [[ $waiting == no ]]; then
            note "Waiting for $svc to pass its health check (usually under a minute)"
            waiting=yes
        fi
        sleep 3
    done
}

service_failed() {
    local svc=$1 status=$2 health=$3 error exit_code hints=()
    error=$("${DOCKER[@]}" inspect -f '{{.State.Error}}' "$svc" 2> /dev/null) || error=
    exit_code=$("${DOCKER[@]}" inspect -f '{{.State.ExitCode}}' "$svc" 2> /dev/null) || exit_code=
    # A clean exit says the service stopped, not that its config is bad.
    # Don't print old logs or configuration hints as the cause of a stop.
    if [[ $status == exited && $exit_code == 0 && -z $error ]]; then
        problem "$svc is stopped (exit code 0)" \
            "Start it with: docker compose up -d, then run this script again."
        return 0
    fi
    note "The last lines of its log:"
    compose logs --no-color --tail 25 "$svc" < /dev/null 2>&1 | indent || true
    if [[ -n $error ]]; then note "Docker says: $error"; fi
    case $svc in
        mileage-ingest) hints=("The usual causes: INGEST_AUTH_TOKEN or DISPLAY_TZ missing from .env, or data/ not owned by UID $CONTAINER_UID.") ;;
        mileage-viewer) hints=("The usual cause is a missing or mangled setting in viewer.env, or DISPLAY_TZ in .env; the log names it.") ;;
    esac
    if [[ $error == *"assign requested address"* ]]; then
        hints+=("BIND_ADDR isn't on this server right now. After a reboot, that means Docker started"
            "before the network did; see 'Containers missing after a reboot' in DEPLOY_DEBIAN.md.")
    fi
    problem "$svc isn't healthy (state: $status, health: $health)" "${hints[@]}" \
        "Once it's fixed: docker compose up -d, then run this script again."
}

PY_HASH_INTACT='import os, sys
sys.exit(0 if os.environ.get("VIEWER_PASSWORD_HASH", "").count("$") >= 2 else 1)'

verify_services() {
    local healthy=yes
    wait_healthy mileage-ingest || healthy=no
    wait_healthy mileage-viewer || healthy=no
    [[ $healthy == yes ]] || return 1

    local tz_env tz_ingest tz_viewer
    tz_env=$(env_raw .env DISPLAY_TZ)
    tz_ingest=$(compose exec -T mileage-ingest printenv DISPLAY_TZ < /dev/null 2> /dev/null) || tz_ingest=
    tz_viewer=$(compose exec -T mileage-viewer printenv DISPLAY_TZ < /dev/null 2> /dev/null) || tz_viewer=
    if [[ -n $tz_ingest && $tz_ingest == "$tz_viewer" && $tz_ingest == "$tz_env" ]]; then
        ok "Both services file and show trips in $tz_ingest (DISPLAY_TZ)"
    else
        problem "The services aren't both using DISPLAY_TZ=${tz_env:-<unset>} from .env (ingest: ${tz_ingest:-unset}, viewer: ${tz_viewer:-unset})" \
            "Recreate them so they pick it up: docker compose up -d" \
            "If that doesn't fix it, docker-compose.yml was edited; compare it with the original."
    fi
    if compose exec -T mileage-viewer python -c "$PY_HASH_INTACT" < /dev/null > /dev/null 2>&1; then
        ok "The viewer's password hash reached the container intact"
    else
        problem "The viewer's password hash lost its \$ signs on the way into the container" \
            "Single-quote VIEWER_PASSWORD_HASH in viewer.env, then: docker compose up -d"
    fi
}

# --- End to end --------------------------------------------------------

# Keep TEST_TRIP set if removal fails or either file remains. A failed
# test -e can also mean the directory is inaccessible, so it cannot
# override a failed rm. Handle errors explicitly: the end-to-end check
# runs with errexit off (#44).
remove_test_trip() {
    local trip=$TEST_TRIP
    [[ -n $trip ]] || return 0
    if ! "${DATA_SUDO[@]}" rm -f -- "$trip.gpx" "$trip.json" 2>&1 | indent; then
        return 1
    fi
    if data_exists "$trip.gpx" "$trip.json"; then return 1; fi
    TEST_TRIP=
    # Only a month folder the test upload made, and only if it's empty:
    # another run's upload may be filing into one that already existed.
    if [[ ${trip%/*} == "$TEST_NEW_DIR" ]]; then
        "${DATA_SUDO[@]}" rmdir -- "$TEST_NEW_DIR" 2> /dev/null || true
    fi
}

# What to do about a test trip that couldn't be deleted.
test_trip_left() {
    local sudo=
    if (( ${#DATA_SUDO[@]} )); then sudo='sudo '; fi
    note "$SCRIPT_DIR/$TEST_TRIP.gpx (and .json) counts in the viewer's totals" \
        "and exports until it's gone. Fix what stopped it (output above), then:" \
        "${sudo}rm -f $SCRIPT_DIR/$TEST_TRIP.gpx $SCRIPT_DIR/$TEST_TRIP.json"
}

# data_exists PATH...: whether any of them exists in data/.
data_exists() {
    local path
    for path; do
        if "${DATA_SUDO[@]}" test -e "$path"; then return 0; fi
    done
    return 1
}

csrf_from() {
    sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p;T;q' <<< "$1"
}

# http_status [curl args] URL: print the HTTP status, 000 if no answer.
http_status() {
    http_fetch /dev/null "$@"
}

# http_fetch FILE [curl args] URL: the same, saving the body to FILE.
http_fetch() {
    local file=$1 code
    shift
    code=$(curl -s -m 30 -o "$file" -w '%{http_code}' "$@") || true
    printf '%s' "${code:-000}"
}

# The real thing: upload sample.gpx with the token from .env. The header
# comes from a pipe so the token stays out of the process list.
#
# What goes up is an untimed copy of sample.gpx (its <time> elements
# removed). Ingest names a trip with timestamps after those timestamps and
# ignores X-Trip-Id for it, so a timed upload couldn't be told apart from a
# real trip. An untimed one takes the ID we give it, is filed under the
# current month of DISPLAY_TZ, and has no date, so it can't land in a tax
# year (a closed one included).
#
# Cleanup deletes the test trip, so it may only ever be this run's: the
# ID is new each run, and its path has to be free beforehand. Then
# whatever turns up there came from this upload.
TEST_UPLOAD=          # the untimed copy of sample.gpx that was uploaded

# The month folder ingest files an untimed upload under right now.
test_month() {
    local zone
    zone=$(env_raw .env DISPLAY_TZ)
    TZ=$zone date +%Y-%m
}

upload_test_trip() {
    local trip token response code body status miles stored after
    TEST_ID=$TEST_PREFIX-$(openssl rand -hex 6)
    trip=data/$(test_month)/$TEST_ID
    if data_exists "$trip.gpx" "$trip.json"; then
        problem "$trip already exists, so the test upload can't use it" \
            "Nothing was uploaded or deleted. Run the check again for a new test ID."
        return 1
    fi
    TEST_UPLOAD=$(mktemp)
    TMP_FILES+=("$TEST_UPLOAD")
    sed 's#<time>[^<]*</time>##g' sample.gpx > "$TEST_UPLOAD"
    if [[ ! -s $TEST_UPLOAD ]] || grep -q '<time' "$TEST_UPLOAD"; then
        problem "Couldn't make the untimed test copy of sample.gpx" \
            "Copy server/sample.gpx here again (DEPLOY_DEBIAN.md Step 2)."
        return 1
    fi
    if ! data_exists "${trip%/*}"; then TEST_NEW_DIR=${trip%/*}; fi
    token=$(env_raw .env INGEST_AUTH_TOKEN)
    TEST_TRIP=$trip
    # No X-Device-Id: this synthetic trip must not enroll a device that the
    # all-device monitor would later report silent. X-Trip-Id owns cleanup
    # (it applies because the copy is untimed).
    response=$(curl -s -m 30 -w '\n%{http_code}' -X POST \
        -H @<(printf 'Authorization: Bearer %s\n' "$token") \
        -H 'Content-Type: application/gpx+xml' \
        -H "X-Trip-Id: $TEST_ID" \
        --data-binary @"$TEST_UPLOAD" "$INGEST_URL/upload") || response=$'\n000'
    code=${response##*$'\n'}
    body=${response%$'\n'*}
    status=$(jq -r '.status // empty' <<< "$body" 2> /dev/null) || status=
    miles=$(jq -r '.distance_miles // empty' <<< "$body" 2> /dev/null) || miles=
    stored=$(jq -r '.stored_as // empty' <<< "$body" 2> /dev/null) || stored=
    if [[ $code == 200 && $status == accepted && $stored == */"$TEST_ID.gpx" ]]; then
        TEST_TRIP=data/${stored%.gpx}
        ok "Uploaded sample.gpx (untimed copy) with the token; it was filed as $stored"
    else
        # A refusal (4xx) stored nothing, so there's nothing of ours to
        # delete. After a timeout or a 5xx some of it may have been
        # written; the path was free before, so cleanup can remove that.
        if [[ $code == 4?? ]]; then
            TEST_TRIP=
        else
            # The month may have turned over during the request.
            after=data/$(test_month)/$TEST_ID
            if [[ $after != "$trip" ]] && data_exists "$after.gpx" "$after.json"; then TEST_TRIP=$after; fi
        fi
        problem "The test upload failed: HTTP $code ${body}" \
            "401: the running service has a different token from .env; recreate it: docker compose up -d" \
            "500: data/ probably isn't writable by UID $CONTAINER_UID (DEPLOY_DEBIAN.md Step 3)."
        return 1
    fi
    if awk -v m="$miles" 'BEGIN { exit !(m >= 0.25 && m <= 0.35) }'; then
        ok "It came to $miles miles, as it should (sample.gpx is about 0.30)"
    else
        problem "sample.gpx came to ${miles:-no} miles instead of about 0.30" \
            "The upload works, but the distance calculation is off."
    fi
}

verify_health_versions() {
    local url body version versions=()
    for url in "$INGEST_URL/health" "$VIEWER_URL/health"; do
        body=$(curl -sS -m 10 "$url" 2>&1) || true
        if [[ $(jq -r '.status // empty' <<< "$body" 2> /dev/null) == ok ]]; then
            ok "$url answers"
        else
            problem "$url doesn't answer from this server" "${body:-no response}" \
                "If BIND_ADDR or VIEWER_BIND_ADDR changed, recreate the containers: docker compose up -d"
            return 1
        fi
        # Match common/version.py's ASCII token budget before printing data
        # from an unauthenticated endpoint (including older servers).
        version=$(jq -r '.version | if type == "string" then
            if test("\\A[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\\z") then . else "unversioned" end
            else "unversioned" end' <<< "$body" 2> /dev/null) || version=unversioned
        versions+=("$version")
    done
    ok "mileage-ingest version: ${versions[0]}"
    ok "mileage-viewer version: ${versions[1]}"
    if [[ ${versions[0]} != "${versions[1]}" ]]; then
        warn "Ingest and viewer report different server versions" \
            "Rebuild both with the same SERVER_VERSION stamp, then run --check again."
    fi
    if [[ ${versions[0]} == unversioned || ${versions[1]} == unversioned ]]; then
        warn "A service has no valid server version stamp" \
            "Rebuild with install.sh or the stamped build command in DEPLOY_DEBIAN.md."
    fi
}

verify_end_to_end() {
    section "End-to-end checks"
    if (( ${#DATA_SUDO[@]} )); then need_sudo; fi
    local body code
    verify_health_versions || return 1

    code=$(http_status -X POST -H 'Content-Type: application/gpx+xml' \
        --data-binary @sample.gpx "$INGEST_URL/upload")
    if [[ $code == 401 ]]; then
        ok "An upload without the token is refused (401)"
    else
        problem "An upload without the token got HTTP $code instead of 401" \
            "The ingest service has to refuse it. Its log: docker compose logs mileage-ingest"
    fi
    code=$(http_status -X POST -H 'Authorization: Bearer not-the-token' \
        -H 'Content-Type: application/gpx+xml' --data-binary @sample.gpx "$INGEST_URL/upload")
    if [[ $code == 401 ]]; then
        ok "An upload with the wrong token is refused (401)"
    else
        problem "An upload with the wrong token got HTTP $code instead of 401"
    fi

    upload_test_trip || return 1
    if "${DATA_SUDO[@]}" cmp -s "$TEST_UPLOAD" "$TEST_TRIP.gpx"; then
        ok "The GPX on disk matches the upload byte for byte"
    else
        problem "$TEST_TRIP.gpx is missing or differs from what was uploaded"
    fi
    if "${DATA_SUDO[@]}" cat "$TEST_TRIP.json" 2> /dev/null \
        | jq -e --arg id "$TEST_ID" '.trip_id == $id and .distance_miles > 0' > /dev/null 2>&1; then
        ok "Its summary (.json) is next to it"
    else
        problem "$TEST_TRIP.json is missing or doesn't describe the test trip"
    fi

    code=$(curl -s -m 10 -o /dev/null -w '%{http_code} %{redirect_url}' "$VIEWER_URL/") || code=000
    if [[ $code == 302\ */login* ]]; then
        ok "The viewer sends anyone not logged in to its login page"
    else
        problem "The viewer answered \"$code\" instead of redirecting to its login page" \
            "It mustn't show trips without a login. Its log: docker compose logs mileage-viewer"
    fi

    local user
    user=$(env_raw viewer.env VIEWER_USERNAME)
    if [[ $VIEWER_COOKIE_SECURE == true ]]; then
        warn "Skipped authenticated viewer tests because VIEWER_COOKIE_SECURE=true" \
            "The health and redirect checks use HTTP. Test logging in through the proxy's HTTPS name."
    elif [[ -z $VIEWER_PASSWORD ]]; then
        ask_secret VIEWER_PASSWORD VIEWER_PASSWORD \
            "Admin password for $user, to test logging in (Enter to skip)" optional
    fi
    if [[ $VIEWER_COOKIE_SECURE == true ]]; then
        : # A Secure cookie must not be sent over this plain-HTTP test connection.
    elif [[ -n $VIEWER_PASSWORD ]]; then
        verify_viewer "$user" || true
    else
        warn "Skipped the login test" "Log in at $VIEWER_URL/ from a browser to check it."
    fi
    if [[ -n $EVERYDAY_PASSWORD && $VIEWER_COOKIE_SECURE != true ]]; then
        verify_everyday_login "$EVERYDAY_USER" || true
    fi
    if [[ $MODE == check ]]; then report_everyday_logins; fi

    if remove_test_trip; then
        ok "Deleted the test trip"
    else
        problem "Couldn't delete the test trip"
        test_trip_left
    fi
    verify_binding
}

# Log in like a browser would, then check the test trip shows up
# everywhere the viewer shows trips, and log out again.
verify_viewer() {
    local user=$1 jar file page csrf code ym
    ym=${TEST_TRIP#data/}
    ym=${ym%/*}
    jar=$(mktemp)
    file=$(mktemp)
    TMP_FILES+=("$jar" "$file")

    page=$(curl -s -m 10 -c "$jar" -b "$jar" "$VIEWER_URL/login") || page=
    csrf=$(csrf_from "$page")
    code=$(printf '%s' "$VIEWER_PASSWORD" | curl -s -m 30 -o /dev/null \
        -w '%{http_code} %{redirect_url}' -c "$jar" -b "$jar" \
        --data-urlencode "csrf_token=$csrf" --data-urlencode "username=$user" \
        --data-urlencode "password@-" "$VIEWER_URL/login") || code=000
    case $code in
        302\ *) ok "Logged in to the viewer as the admin, $user" ;;
        401*)
            problem "The viewer turned down that password for the admin login $user" \
                "To set a new one, run install.sh and answer n to keeping the login."
            return 1
            ;;
        429*)
            problem "The viewer has locked this server out after too many wrong passwords" \
                "Wait 15 minutes, or clear it ('Locked out of the viewer' in DEPLOY_DEBIAN.md)."
            return 1
            ;;
        *)
            problem "Logging in to the viewer failed (HTTP $code)"
            return 1
            ;;
    esac

    page=$(curl -s -m 10 -b "$jar" "$VIEWER_URL/") || page=
    if [[ $page == *"/trip/$ym/$TEST_ID"* ]]; then
        ok "The test trip is in the viewer's trip list"
    else
        problem "The test trip isn't in the viewer's trip list" \
            "The viewer reads data/ read-only. Its log: docker compose logs mileage-viewer"
    fi
    if [[ $(http_status -b "$jar" "$VIEWER_URL/trip/$ym/$TEST_ID") == 200 ]]; then
        ok "Its map page loads"
    else
        problem "The test trip's map page didn't load"
    fi
    if [[ $(http_fetch "$file" -b "$jar" "$VIEWER_URL/gpx/$ym/$TEST_ID") == 200 ]] \
        && cmp -s "$file" "$TEST_UPLOAD"; then
        ok "The viewer serves its track for the map"
    else
        problem "The viewer didn't serve the test trip's GPX track"
    fi
    if [[ $(http_fetch "$file" -b "$jar" "$VIEWER_URL/export.csv") == 200 ]] \
        && grep -q ",$TEST_ID," "$file"; then
        ok "The CSV export includes it"
    else
        problem "The CSV export failed or left out the test trip"
    fi
    if [[ $(http_fetch "$file" -b "$jar" "$VIEWER_URL/export.xlsx") == 200 \
        && $(head -c 2 "$file") == PK ]]; then
        ok "The XLSX export downloads"
    else
        problem "The XLSX export failed"
    fi

    csrf=$(csrf_from "$page")
    code=$(curl -s -m 10 -o /dev/null -w '%{http_code} %{redirect_url}' -c "$jar" -b "$jar" \
        --data-urlencode "csrf_token=$csrf" "$VIEWER_URL/logout") || code=000
    if [[ $code == 302\ */login* \
        && $(curl -s -m 10 -o /dev/null -w '%{http_code}' -b "$jar" "$VIEWER_URL/") == 302 ]]; then
        ok "Logging out works"
    else
        problem "Logging out of the viewer didn't work (HTTP $code)"
    fi
}

# Log in as the everyday login this run created, with a cookie jar of its
# own: it must see the trip list, and /admin must refuse it. One login
# attempt, because the lockout counts failures per IP address.
verify_everyday_login() {
    local user=$1 jar file page csrf code
    jar=$(mktemp)
    file=$(mktemp)
    TMP_FILES+=("$jar" "$file")

    page=$(curl -s -m 10 -c "$jar" -b "$jar" "$VIEWER_URL/login") || page=
    csrf=$(csrf_from "$page")
    code=$(printf '%s' "$EVERYDAY_PASSWORD" | curl -s -m 30 -o /dev/null \
        -w '%{http_code} %{redirect_url}' -c "$jar" -b "$jar" \
        --data-urlencode "csrf_token=$csrf" --data-urlencode "username=$user" \
        --data-urlencode "password@-" "$VIEWER_URL/login") || code=000
    case $code in
        302\ *) ok "Logged in to the viewer as the everyday login $user" ;;
        401*)
            problem "The viewer turned down the password for the everyday login $user" \
                "It was created a moment ago. Reset it on the admin page ($VIEWER_URL/admin)."
            return 1
            ;;
        429*)
            problem "The viewer has locked this server out after too many wrong passwords" \
                "Wait 15 minutes, or clear it ('Locked out of the viewer' in DEPLOY_DEBIAN.md)."
            return 1
            ;;
        *)
            problem "Logging in to the viewer as the everyday login $user failed (HTTP ${code%% *})"
            return 1
            ;;
    esac

    code=$(http_fetch "$file" -b "$jar" "$VIEWER_URL/")
    if [[ $code == 200 ]]; then
        ok "The everyday login sees the trip list"
    else
        problem "The trip list didn't load for the everyday login $user (HTTP $code)" \
            "Its log: docker compose logs mileage-viewer"
    fi
    code=$(http_status -b "$jar" "$VIEWER_URL/admin")
    if [[ $code == 403 ]]; then
        ok "The admin page refuses the everyday login (403)"
    elif [[ $code == 200 ]]; then
        problem "The everyday login $user could open the admin page" \
            "Only the admin may. Its log: docker compose logs mileage-viewer"
    else
        problem "The admin page answered the everyday login $user with HTTP $code instead of 403"
    fi

    # Leave no session behind. Logging out was already checked for the admin.
    page=$(cat "$file")
    csrf=$(csrf_from "$page")
    curl -s -m 10 -o /dev/null -c "$jar" -b "$jar" \
        --data-urlencode "csrf_token=$csrf" "$VIEWER_URL/logout" || true
}

# --check: say which everyday logins exist. Information only, never a
# problem; if the viewer isn't running, another check says so.
report_everyday_logins() {
    [[ $(container_state mileage-viewer) == running ]] || return 0
    if ! list_everyday running; then
        note "Couldn't list the everyday logins. If this install predates them," \
            "run install.sh to rebuild the viewer."
    elif (( ${#EVERYDAY_LOGINS[@]} )); then
        ok "Everyday logins: $(everyday_text)"
    else
        note "No everyday logins yet. The admin adds them on the admin page:" \
            "$VIEWER_URL/admin"
    fi
}

verify_binding() {
    local pair svc port mapped addr expected
    # Check these containers' mappings. A response from localhost could
    # belong to another service bound there, so it can't prove exposure.
    for pair in mileage-ingest:8090 mileage-viewer:8091; do
        svc=${pair%:*}
        port=${pair#*:}
        addr=$BIND_ADDR
        if [[ $svc == mileage-viewer ]]; then addr=${VIEWER_BIND_ADDR:-$BIND_ADDR}; fi
        [[ -n $addr ]] || continue
        expected=$addr:$port
        if [[ $addr == *:* ]]; then expected=[$addr]:$port; fi
        mapped=$(compose port "$svc" "$port" < /dev/null 2> /dev/null) || mapped=
        if [[ $mapped == "$expected" ]]; then
            ok "Port $port is published on $addr only"
        else
            problem "Port $port is published on ${mapped:-nothing}, not $addr" \
                "Recreate the containers so they pick up BIND_ADDR / VIEWER_BIND_ADDR: docker compose up -d"
        fi
    done
}

# --- Reboots -----------------------------------------------------------

verify_reboots() {
    section "Reboots"
    local svc policy restart=yes id required='unless-stopped or always'
    if [[ $MILEAGE_PLATFORM == unraid ]]; then required=always; fi
    for svc in mileage-ingest mileage-viewer ntfy mileage-monitor; do
        id=$svc
        case $svc in
            ntfy|mileage-monitor)
                id=$({ compose --profile ntfy --profile monitor ps -a -q "$svc" < /dev/null 2> /dev/null || true; } | head -n 1)
                [[ -n $id ]] || continue ;;
        esac
        policy=$("${DOCKER[@]}" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$id" 2> /dev/null) || policy=
        if [[ $policy != always && ( $MILEAGE_PLATFORM == unraid || $policy != unless-stopped ) ]]; then
            restart=no
            problem "$svc won't come back on its own (restart policy: ${policy:-none}; expected $required)" \
                "Run install.sh to apply the platform's restart policy, then docker compose up -d with any enabled profiles."
        fi
    done
    if [[ $restart == yes ]]; then ok "Containers start again with Docker (restart: $required)"; fi

    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        unraid_docker_enabled
        note "Unraid starts Docker after networking and the array; no wait-online service is needed."
        BOOT_UNVERIFIED=yes
        note "Actual array stop/start and reboot recovery are unverified; test them on the Unraid host."
        return 0
    fi
    if ! have_systemd; then
        BOOT_UNVERIFIED=yes
        warn "systemd isn't running here, so this can't check what starts at boot"
        return 0
    fi
    if [[ $(systemctl is-enabled docker 2> /dev/null) == enabled ]]; then
        ok "Docker starts at boot"
    elif [[ $MODE == install ]]; then
        need_sudo
        as_root systemctl enable --quiet docker
        ok "Docker now starts at boot"
    else
        problem "Docker doesn't start at boot" "sudo systemctl enable docker"
    fi
    if [[ -n $BIND_ADDR ]]; then check_wait_online; fi
}

# Docker's unit already waits for network-online.target, but that only
# means something if a wait-online service for the network stack in use
# is enabled. Without one, Docker can start before BIND_ADDR exists and
# the containers don't come back after a reboot.
# The wait-online unit for the network stack in use, or nothing if it
# can't tell or that unit doesn't exist.
wait_online_unit() {
    local unit=''
    if systemctl is-active --quiet NetworkManager 2> /dev/null; then
        unit=NetworkManager-wait-online.service
    elif systemctl is-active --quiet systemd-networkd 2> /dev/null; then
        unit=systemd-networkd-wait-online.service
    elif systemctl is-active --quiet networking 2> /dev/null; then
        unit=ifupdown-wait-online.service
    fi
    if [[ -n $unit ]] && systemctl cat "$unit" > /dev/null 2>&1; then
        printf '%s' "$unit"
    fi
}

wait_online_enabled() {
    local state
    state=$(systemctl is-enabled "$1" 2> /dev/null) || true
    case $state in
        enabled | enabled-runtime | static | indirect | alias) return 0 ;;
    esac
    return 1
}

ask_wait_online() {
    note "With BIND_ADDR set, Docker can only publish the ports once $BIND_ADDR is up." \
        "If Docker starts first at boot, the containers stay down until you start them."
    if confirm WAIT_ONLINE "Make Docker wait for the network at boot ($1)?" y; then
        WANT_WAIT_ONLINE=yes
    else
        WANT_WAIT_ONLINE=no
    fi
}

check_wait_online() {
    local unit
    unit=$(wait_online_unit)
    if [[ -z $unit ]]; then
        warn "Couldn't tell whether Docker waits for the network at boot" \
            "If the containers are missing after a reboot, see 'Containers missing after" \
            "a reboot' in DEPLOY_DEBIAN.md."
        return 0
    fi
    if wait_online_enabled "$unit"; then
        ok "Docker waits for the network at boot ($unit)"
        return 0
    fi
    if [[ $MODE == check ]]; then
        warn "At boot, Docker may start before $BIND_ADDR is up and leave the containers down" \
            "sudo systemctl enable $unit"
        return 0
    fi
    if [[ -z $WANT_WAIT_ONLINE ]]; then ask_wait_online "$unit"; fi
    if [[ $WANT_WAIT_ONLINE == yes ]]; then
        need_sudo
        as_root systemctl enable --quiet "$unit"
        ok "Docker waits for the network at boot ($unit)"
    else
        warn "At boot, Docker may start before $BIND_ADDR is up and leave the containers down" \
            "To change that later: sudo systemctl enable $unit"
    fi
}

# --- Backups -----------------------------------------------------------

# Resolve before creating anything: a missing Unassigned Devices mount can
# leave an ordinary folder on the RAM root. backup.sh checks these recorded
# mounts again at run time, including when cron runs without the disk.
unraid_mount() {  # PATH; sets UNRAID_MOUNT and UNRAID_PATH
    local path=$1 probe fs target
    [[ $path == /* && $path != *[\'\"\\$'\n'$'\r']* ]] || {
        note "Use an absolute backup path without quotes, backslashes or newlines."; return 1;
    }
    UNRAID_PATH=$(realpath -m -- "$path") || return 1
    case $UNRAID_PATH in
        /boot|/boot/*|/root|/root/*|/tmp|/tmp/*)
            note "Backup paths must be on persistent storage, never /boot, /root or /tmp."; return 1 ;;
    esac
    probe=$UNRAID_PATH
    while [[ ! -e $probe ]]; do probe=${probe%/*}; probe=${probe:-/}; done
    target=$(findmnt -n -o TARGET --target "$probe") || return 1
    fs=$(findmnt -n -o FSTYPE --target "$probe") || return 1
    if [[ -z $target || $target == / || $target == /boot || $target == /boot/* || $fs == rootfs || $fs == tmpfs ]] \
        || ! findmnt -n --mountpoint "$target" > /dev/null; then
            note "Backup destination is not on a mounted persistent filesystem. Mount it first."; return 1;
    fi
    [[ $target != *[\'\"\\$'\n'$'\r']* ]] || return 1
    UNRAID_MOUNT=$target
}

prepare_unraid_backup_paths() {
    local dir remote mount data_mount
    dir=$(conf_get BACKUP_DIR)
    if [[ -n ${INSTALL_BACKUP_DIR+set} ]]; then dir=$INSTALL_BACKUP_DIR; fi
    if [[ -z $dir ]]; then
        ask dir BACKUP_DIR "Local snapshot folder on a mounted persistent disk (absolute path)"
    fi
    unraid_mount "$dir" || die "BACKUP_DIR isn't on persistent mounted storage. Set INSTALL_BACKUP_DIR to a mounted destination."
    dir=$UNRAID_PATH mount=$UNRAID_MOUNT
    data_mount=$(findmnt -n -o TARGET --target "$SCRIPT_DIR/data") || data_mount=
    if [[ $mount == "$data_mount" ]]; then
        warn "BACKUP_DIR is on the same filesystem as data/; it won't survive that disk failing"
    fi
    env_set backup/backup.conf BACKUP_DIR "'$dir'"
    env_set backup/backup.conf BACKUP_MOUNT "'$mount'"
    remote=$(conf_get REMOTE)
    if [[ $remote == /* ]]; then
        unraid_mount "$remote" || die "The local REMOTE isn't on persistent mounted storage. Mount it first."
        env_set backup/backup.conf REMOTE "'$UNRAID_PATH'"
        env_set backup/backup.conf REMOTE_MOUNT "'$UNRAID_MOUNT'"
        warn "The local REMOTE is on this Unraid host; it isn't an off-host copy"
        if [[ $UNRAID_MOUNT == "$data_mount" ]]; then
            warn "REMOTE is on the same filesystem as data/; it won't survive that disk failing"
        fi
    else
        env_set backup/backup.conf REMOTE_MOUNT "''"
        # Existing configurations must migrate away from volatile /root keys.
        if ! unraid_ssh_target_ok "$remote"; then die "The persistent backup SSH key couldn't reach the destination."; fi
        env_set backup/backup.conf RSYNC_SSH "\"$RSYNC_SSH_CMD\""
    fi
}

unraid_ssh_target_ok() {
    local host=${1%%:*} path=${1#*:} key=$SCRIPT_DIR/backup/ssh/id_ed25519 known=$SCRIPT_DIR/backup/ssh/known_hosts
    [[ ! -L backup/ssh && ( ! -e backup/ssh || -d backup/ssh ) ]] || die "backup/ssh must be a real private directory."
    mkdir -p -- "$SCRIPT_DIR/backup/ssh"
    chmod 700 "$SCRIPT_DIR/backup/ssh"
    [[ ! -L $key && ! -L $known && ! -L $key.pub ]] || die "Backup SSH files must not be symlinks."
    if [[ ! -f $key ]]; then
        ssh-keygen -q -t ed25519 -N '' -C mileage-backup -f "$key" < /dev/null
        ok "Made a persistent SSH key just for backups"
    fi
    touch "$known"
    chmod 600 "$key" "$known"
    local ssh=(ssh -i "$key" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$known" -o BatchMode=yes -o ConnectTimeout=10)
    if ! "${ssh[@]}" "$host" true < /dev/null 2> /dev/null; then
        if ! command -v ssh-copy-id > /dev/null; then
            note "ssh-copy-id isn't installed. Add backup/ssh/id_ed25519.pub to the destination's authorized_keys, then rerun."
            return 1
        fi
        note "Installing only the dedicated persistent backup key on $host; confirm its host key and password privately."
        ssh-copy-id -i "$key.pub" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$known" "$host" || return 1
    fi
    "${ssh[@]}" "$host" "mkdir -p -- $(printf %q "$path") && command -v rsync > /dev/null" < /dev/null || return 1
    RSYNC_SSH_CMD="ssh -i $key -o IdentitiesOnly=yes -o UserKnownHostsFile=$known"
}

unraid_cron_file() { printf '%s/mileage-tracker-backup.cron' "${MILEAGE_UNRAID_CRON_DIR:-/boot/config/plugins/dynamix}"; }

unraid_cron_text() {
    printf '# Mileage tracker backup. Written by install.sh; rerun it to change.\n'
    printf '30 3 * * * /bin/bash %s/backup/scheduled.sh --delay 900\n' "$SCRIPT_DIR"
    printf '15 * * * * /bin/bash %s/backup/scheduled.sh --if-overdue 26\n' "$SCRIPT_DIR"
}

unraid_cron_current() {
    cmp -s "$(unraid_cron_file)" <(unraid_cron_text) || return 1
    local loaded line
    loaded=$(crontab -l 2> /dev/null) || return 1
    while IFS= read -r line; do
        [[ $line == '#'* ]] && continue
        grep -qxF -- "$line" <<< "$loaded" || return 1
    done < <(unraid_cron_text)
}

install_unraid_cron() {
    local file tmp
    file=$(unraid_cron_file)
    mkdir -p -- "${file%/*}"
    tmp=$(mktemp "${file}.XXXXXX")
    unraid_cron_text > "$tmp"
    # /boot may be FAT: do not depend on Unix mode or execution bits here.
    mv -- "$tmp" "$file"
    "${MILEAGE_UPDATE_CRON:-/usr/local/sbin/update_cron}"
    unraid_cron_current || die "The backup cron file wasn't loaded. Check update_cron and crontab -l."
    ok "Persistent nightly backup and hourly catch-up cron entries are loaded"
}

unraid_backup_log() {
    local log=$SCRIPT_DIR/backup/logs/backup.log
    note "Backup log: $log"
    if [[ -f $log ]]; then tail -n 10 -- "$log" | indent; fi
}

verify_unraid_backups() {
    local status key value last='' attempt='' dir recorded
    if unraid_cron_current; then
        ok "Persistent backup cron file matches this install and both entries are loaded"
    else
        problem "Persistent backup cron file or loaded crontab doesn't match this install" "Run install.sh to reload it."
    fi
    for key in BACKUP REMOTE; do
        if [[ $key == BACKUP ]]; then dir=$(conf_get BACKUP_DIR); else dir=$(conf_get REMOTE); fi
        if [[ $dir != /* && $key == REMOTE ]]; then continue; fi
        recorded=$(conf_get "${key}_MOUNT")
        if ! unraid_mount "$dir" || [[ -z $recorded || $recorded != "$UNRAID_MOUNT" ]]; then
            problem "$key destination mount is missing or differs from backup.conf" "Mount the recorded disk, or rerun install.sh to configure the destination."
        fi
    done
    if ! status=$(bash "$SCRIPT_DIR/backup/scheduled.sh" --status); then
        unraid_backup_log
        problem "Couldn't read scheduled backup status" "See the backup log above."
        return 0
    fi
    while IFS='=' read -r key value; do
        case $key in last_attempt_status) attempt=$value ;; last_success) last=$value ;; esac
    done <<< "$status"
    if [[ $attempt == running ]]; then
        warn "A scheduled backup is running; its final status is not verified yet"
    elif [[ $attempt != 0 ]]; then
        unraid_backup_log
        problem "The last scheduled backup failed or has never run (status: ${attempt:-missing})" "Run: bash $SCRIPT_DIR/backup/scheduled.sh"
    else
        ok "The last scheduled backup attempt succeeded"
    fi
    local timestamp now
    timestamp=$(date -d "$last" +%s 2> /dev/null) || timestamp=0
    now=$(date +%s)
    if [[ -z $last || $timestamp == 0 ]] || (( now - timestamp > 2 * 86400 )); then
        unraid_backup_log
        problem "No good backup within the last two days" "Run: bash $SCRIPT_DIR/backup/scheduled.sh"
    else
        ok "Last good backup: $last"
    fi
}

setup_unraid_backups() {
    local first=no refresh=no
    [[ $SCRIPT_DIR =~ ^/[-a-zA-Z0-9_./]+$ && $SCRIPT_DIR != *'/../'* ]] \
        || die "The install path must be a simple absolute path for the persistent cron entries."
    if [[ $MODE == check ]]; then
        if ! backups_configured; then warn "Backups aren't set up. Run install.sh."; return 0; fi
        verify_unraid_backups
        restore_test
        return 0
    fi
    if ! backups_configured; then
        if [[ -z $WANT_BACKUPS ]]; then ask_backups; fi
        if [[ $WANT_BACKUPS != yes ]]; then warn "Backups aren't set up. Run install.sh to set them up."; return 0; fi
        choose_backup_target
        first=yes
    fi
    prepare_unraid_backup_paths
    ensure_backup_target "$([[ $first == yes ]] && echo new || echo rerun)"
    if ! unraid_cron_current; then refresh=yes; fi
    if [[ $first == yes || $refresh == yes ]]; then
        note "Running one backup through scheduled.sh, the same wrapper cron uses"
        if ! bash "$SCRIPT_DIR/backup/scheduled.sh" < /dev/null; then
            unraid_backup_log
            die "The scheduled backup failed. Fix the destination or backup.conf and rerun."
        fi
        FIRST_BACKUP=yes
        restore_test
        install_unraid_cron
    fi
    verify_unraid_backups
}

# Where the backup units go, and where their template expects the files.
UNIT_DIR=/etc/systemd/system
DEFAULT_DIR=/home/$ME/mileage-tracker

backup_unit() { printf 'mileage-backup@%s' "$ME"; }

# The drop-in that points the service at an install outside DEFAULT_DIR.
backup_dropin() { printf '%s/%s.service.d/install-path.conf' "$UNIT_DIR" "$(backup_unit)"; }

dropin_text() {
    printf '[Service]\nExecStart=\nExecStart=/bin/bash "%s/backup/backup.sh"\n' "${SCRIPT_DIR//%/%%}"
}

# The drop-in that makes systemd mount the backup destination (and
# BACKUP_DIR) before a run, when they are local folders. If a share isn't
# mounted, the run fails loudly instead of copying onto this disk.
backup_mounts_dropin() { printf '%s/%s.service.d/backup-mounts.conf' "$UNIT_DIR" "$(backup_unit)"; }

# Empty when there's nothing to require (an SSH destination).
mounts_text() {
    local paths=() path
    for path in "$(conf_get REMOTE)" "$(conf_get BACKUP_DIR)"; do
        # A path with spaces, quotes or % can't go in the unit file safely.
        if [[ $path == /* && $path != *[[:space:]\'\"\\%]* ]]; then paths+=("$path"); fi
    done
    if (( ${#paths[@]} )); then
        printf '[Unit]\nRequiresMountsFor=%s\n' "${paths[*]}"
    fi
}

# Whether /etc/systemd/system has the unit, timer and drop-ins this install
# would write. An update that changes them reaches existing servers here.
backup_units_current() {
    local expected
    cmp -s backup/mileage-backup@.service "$UNIT_DIR/mileage-backup@.service" 2> /dev/null || return 1
    cmp -s backup/mileage-backup@.timer "$UNIT_DIR/mileage-backup@.timer" 2> /dev/null || return 1
    if [[ $SCRIPT_DIR != "$DEFAULT_DIR" ]]; then
        [[ -f $(backup_dropin) && $(< "$(backup_dropin)") == "$(dropin_text)" ]] || return 1
    else
        [[ ! -e $(backup_dropin) ]] || return 1
    fi
    expected=$(mounts_text)
    if [[ -n $expected ]]; then
        [[ -f $(backup_mounts_dropin) && $(< "$(backup_mounts_dropin)") == "$expected" ]] || return 1
    else
        [[ ! -e $(backup_mounts_dropin) ]] || return 1
    fi
}

backups_configured() {
    local remote
    remote=$(conf_get REMOTE)
    [[ -n $remote && $remote != "$EXAMPLE_REMOTE" ]]
}

timer_ok() {
    local unit
    unit=$(backup_unit)
    [[ $(systemctl is-enabled "$unit.timer" 2> /dev/null) == enabled \
        && $(systemctl is-active "$unit.timer" 2> /dev/null) == active ]]
}

# Whether the service, as systemd has it loaded, runs this copy's
# backup.sh. After the install moves, the timer stays on but runs the old
# path (#46).
timer_current() {
    local exec
    exec=$(systemctl show -p ExecStart --value "$(backup_unit).service" 2> /dev/null) || return 1
    # e.g. { path=/bin/bash ; argv[]=/bin/bash /home/<user>/... ; ... }
    [[ $exec == *[=\ ]"$SCRIPT_DIR/backup/backup.sh ;"* ]]
}

local_target_ok() {
    local dir=$1
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        unraid_mount "$dir" || return 1
        warn "A local backup destination on this Unraid host isn't an off-host copy"
    fi
    if ! mkdir -p -- "$dir" 2> /dev/null; then
        note "Couldn't create $dir. Create it, make it writable by $ME, and try again."
        return 1
    fi
    if [[ ! -w $dir ]]; then
        note "$dir isn't writable by $ME."
        return 1
    fi
    if [[ $(stat -c %d -- "$dir") == "$(stat -c %d data)" ]]; then
        warn "$dir is on the same disk as the trip data" \
            "A backup there won't survive that disk failing. A NAS share or another machine is better."
        if [[ $MILEAGE_PLATFORM != unraid ]]; then confirm SAME_DISK "Use it anyway?" n || return 1; fi
    fi
    ok "Backups will go to $dir"
}

ssh_target_ok() {
    local host=${1%%:*} path=${1#*:} key=$HOME/.ssh/mileage_backup
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        unraid_ssh_target_ok "$1"
        return
    fi
    if [[ ! -f $key ]]; then
        if [[ ! -d $HOME/.ssh ]]; then mkdir -m 700 "$HOME/.ssh"; fi
        ssh-keygen -q -t ed25519 -N '' -C mileage-backup -f "$key" < /dev/null
        ok "Made an SSH key just for backups: $key"
    fi
    # IdentitiesOnly, so a key in your ssh-agent can't make this pass when
    # the timer, which has no agent, would fail.
    local ssh=(ssh -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10)
    if ! "${ssh[@]}" "$host" true < /dev/null 2> /dev/null; then
        note "Copying the backup key to $host. ssh asks for your password there once (and to" \
            "confirm the host key if it's new); after that, the key does the logging in."
        if ! ssh-copy-id -i "$key.pub" -o IdentitiesOnly=yes "$host"; then
            note "Couldn't copy the key to $host."
            return 1
        fi
    fi
    if ! "${ssh[@]}" "$host" "mkdir -p -- $(printf %q "$path") && command -v rsync > /dev/null" < /dev/null; then
        note "Logged in to $host, but couldn't create $path there, or rsync isn't installed there."
        return 1
    fi
    ok "$host takes the backup key without a password, has rsync, and has $path"
    # The keepalive and connect limits stop a NAS that vanished mid-backup
    # from holding the backup (and every later one) for hours.
    RSYNC_SSH_CMD="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=30"
}

choose_backup_target() {
    local remote ping rsync_ssh
    note "Backups go to user@host:/path on another machine over SSH (a NAS, or any" \
        "always-on machine with rsync), or to /path on a mounted share."
    while :; do
        ask remote BACKUP_REMOTE "Backup destination"
        remote=${remote%/}
        rsync_ssh=
        if [[ $remote == *[\'\"\\]* ]]; then
            note "Quotes and backslashes aren't supported in the destination."
        elif [[ $remote == /* ]]; then
            if local_target_ok "$remote"; then break; fi
        elif [[ $remote =~ ^[A-Za-z0-9._@-]+:.+ ]]; then
            if ssh_target_ok "$remote"; then
                rsync_ssh=$RSYNC_SSH_CMD
                break
            fi
        else
            note "That doesn't look like user@host:/path or /path."
        fi
        if [[ -n ${INSTALL_BACKUP_REMOTE+set} ]]; then
            die "INSTALL_BACKUP_REMOTE=$remote didn't work (above)."
        fi
    done

    note "Optional: a push-monitor URL (Uptime Kuma, healthchecks.io) to call after each" \
        "good backup, so you hear about it when they stop."
    while :; do
        ask ping PING_URL "Push-monitor URL (Enter to skip)"
        if [[ -z $ping ]] || [[ $ping == http*://* && $ping != *[[:space:]\'\"\\]* ]]; then break; fi
        if [[ -n ${INSTALL_PING_URL+set} ]]; then die "INSTALL_PING_URL isn't an http(s) URL."; fi
        note "That isn't an http:// or https:// URL."
    done

    if [[ ! -f backup/backup.conf ]]; then
        install -m 600 backup/backup.conf.example backup/backup.conf
    fi
    env_set backup/backup.conf REMOTE "'$remote'"
    if [[ -n $rsync_ssh ]]; then env_set backup/backup.conf RSYNC_SSH "\"$rsync_ssh\""; fi
    if [[ -n $ping ]]; then env_set backup/backup.conf PING_URL "'$ping'"; fi
    ok "Wrote backup/backup.conf"
}

# backup.sh won't copy to a destination without the marker file
# (.mileage-backup-target), so an unmounted share can't pass for it. Write
# it once the destination is known to be the real one: right after setup
# ("new"), or on a rerun for a backup set up before the marker existed
# ("rerun", which warns instead of stopping the update).
ensure_backup_target() {
    local how=$1 remote backup=$SCRIPT_DIR/backup/backup.sh
    remote=$(conf_get REMOTE)
    if "${AS_ME[@]}" bash "$backup" --check-target < /dev/null 2> /dev/null; then
        return 0
    fi
    if [[ $how == rerun && $remote == /* ]]; then
        note "Backups go to $remote, which has no marker file yet. Writing one says this" \
            "folder is the real backup destination, so it must be the mounted share or disk now."
        if ! local_target_ok "$remote"; then
            warn "Left $remote unmarked, so the nightly backup will refuse to copy there" \
                "When it is the right, mounted destination: bash $backup --mark-target"
            return 0
        fi
    fi
    if "${AS_ME[@]}" bash "$backup" --mark-target < /dev/null 2>&1 | indent; then
        ok "Marked $remote as the backup destination"
    elif [[ $how == rerun ]]; then
        warn "Couldn't write the backup marker file to $remote" \
            "Until it's there, the nightly backup refuses to copy and fails (its log says why)." \
            "When $remote is reachable: bash $backup --mark-target"
    else
        die "Couldn't write the backup marker file to $remote." \
            "Fix backup/backup.conf or the destination, then run this again."
    fi
}

first_backup() {
    note "Running the first backup"
    if ! "${AS_ME[@]}" bash "$SCRIPT_DIR/backup/backup.sh" < /dev/null 2>&1 | indent; then
        die "The first backup failed (output above)." \
            "Fix backup/backup.conf or the destination, then run this again."
    fi
    ok "Backed up, and checked the copy at $(conf_get REMOTE)"
    FIRST_BACKUP=yes
    restore_test
}

restore_test() {
    note "Restore test: pulling the newest backup back and checking every file in it"
    if "${AS_ME[@]}" bash "$SCRIPT_DIR/backup/restore.sh" check < /dev/null 2>&1 | indent; then
        ok "Restore test passed"
    else
        problem "The restore test failed (output above)" \
            "Until it passes, don't count on the backups."
    fi
}

# Put the unit, the timer and their drop-ins in place and tell systemd.
install_backup_units() {
    local unit dropin mounts mounts_dropin
    unit=$(backup_unit)
    dropin=$(backup_dropin)
    mounts_dropin=$(backup_mounts_dropin)
    need_sudo
    as_root install -m 644 backup/mileage-backup@.service backup/mileage-backup@.timer "$UNIT_DIR/"
    # The unit runs /home/<user>/mileage-tracker/backup/backup.sh. If the
    # files are somewhere else, a drop-in points it at them.
    if [[ $SCRIPT_DIR != "$DEFAULT_DIR" ]]; then
        as_root install -d -m 755 "${dropin%/*}"
        dropin_text | as_root tee "$dropin" > /dev/null
        as_root chmod 644 "$dropin"
        note "Pointed $unit.service at $SCRIPT_DIR"
    elif [[ -e $dropin ]]; then
        as_root rm -f "$dropin"
        note "Pointed $unit.service back at $DEFAULT_DIR"
    fi
    # A local destination or BACKUP_DIR (a mounted share or disk) must be
    # mounted before the backup runs.
    mounts=$(mounts_text)
    if [[ -n $mounts ]]; then
        as_root install -d -m 755 "${mounts_dropin%/*}"
        printf '%s\n' "$mounts" | as_root tee "$mounts_dropin" > /dev/null
        as_root chmod 644 "$mounts_dropin"
    elif [[ -e $mounts_dropin ]]; then
        as_root rm -f "$mounts_dropin"
    fi
    as_root systemctl daemon-reload
}

install_backup_timer() {
    local unit
    unit=$(backup_unit)
    install_backup_units
    as_root systemctl enable --now --quiet "$unit.timer"
    note "Running the backup once through systemd, the way the timer will"
    if ! as_root systemctl start "$unit.service"; then
        as_root journalctl -u "$unit.service" -n 20 --no-pager 2>&1 | indent || true
        die "The backup failed when systemd ran it (log above)." \
            "journalctl -u $unit.service has the whole log."
    fi
    ok "The backup works when systemd runs it"
}

verify_backups() {
    local unit backup_dir last age next
    unit=$(backup_unit)
    if ! timer_ok; then
        problem "The daily backup timer isn't running" "sudo systemctl enable --now $unit.timer"
    elif ! timer_current; then
        problem "The daily backup timer is on, but doesn't back up $SCRIPT_DIR" \
            "It runs backup.sh from somewhere else. Run install.sh from here again to fix it."
    else
        next=$(systemctl show -p NextElapseUSecRealtime --value "$unit.timer" 2> /dev/null) || next=
        ok "The daily backup timer is on${next:+ (next run: $next)}"
        if ! backup_units_current; then
            warn "The installed backup systemd files differ from the ones in $SCRIPT_DIR/backup" \
                "An update changed them. Run install.sh to install the new ones."
        fi
    fi
    # (Install mode already warned if it couldn't write the marker.)
    if [[ $MODE == check && -f backup/backup.conf ]] \
        && ! "${AS_ME[@]}" bash "$SCRIPT_DIR/backup/backup.sh" --check-target < /dev/null 2> /dev/null; then
        problem "The backup destination $(conf_get REMOTE) has no .mileage-backup-target file" \
            "Backups refuse to copy there until it's marked. If it is the right, mounted destination:" \
            "bash $SCRIPT_DIR/backup/backup.sh --mark-target"
    fi
    if systemctl is-failed --quiet "$unit.service" 2> /dev/null; then
        problem "The last backup failed" "To see why: journalctl -u $unit.service -n 30"
    fi
    backup_dir=$(conf_get BACKUP_DIR)
    backup_dir=${backup_dir:-$HOME/mileage-backups}
    last=$(cat "$backup_dir/last-success" 2> /dev/null) || last=
    if [[ -z $last ]]; then
        problem "No backup has finished yet" "Run one: bash $SCRIPT_DIR/backup/backup.sh"
    else
        age=$(($(date +%s) - $(date -d "$last" +%s 2> /dev/null || echo 0)))
        if (( age > 2 * 86400 )); then
            problem "The last good backup was $last, over two days ago" \
                "To see why: journalctl -u $unit.service -n 30"
        else
            ok "Last good backup: $last"
        fi
    fi
}

ask_backups() {
    note "If this disk dies, the trip files and the annotations can't be rebuilt, and the" \
        "annotations (business or personal, and why) are the record the IRS asks for." \
        "Backups copy both to another machine or a mounted share every night."
    if confirm BACKUPS "Set up backups?" y; then
        WANT_BACKUPS=yes
    else
        WANT_BACKUPS=no
    fi
}

setup_backups() {
    section "Backups"
    if [[ $MILEAGE_PLATFORM == unraid ]]; then setup_unraid_backups; return; fi
    if [[ $(id -u) != "$CONTAINER_UID" ]]; then
        warn "Skipped backups: they run as you (UID $(id -u)), and the trip files belong to UID $CONTAINER_UID" \
            "DEPLOY_DEBIAN.md Step 10 explains."
        return 0
    fi
    if ! backups_configured; then
        if [[ $MODE == check ]]; then
            warn "Backups aren't set up" "Run install.sh to set them up, or see DEPLOY_DEBIAN.md Step 10."
            return 0
        fi
        if [[ -z $WANT_BACKUPS ]]; then ask_backups; fi
        if [[ $WANT_BACKUPS != yes ]]; then
            warn "Backups aren't set up" "Run this script again to set them up, or see DEPLOY_DEBIAN.md Step 10."
            return 0
        fi
        choose_backup_target
        ensure_backup_target new
        first_backup
        install_backup_timer
    else
        ok "backup.conf sends backups to $(conf_get REMOTE)"
        # Backups set up before the marker file existed get it here.
        if [[ $MODE == install ]]; then ensure_backup_target rerun; fi
        if [[ $MODE == install ]] && ! { timer_ok && timer_current; }; then
            if timer_ok; then
                note "The backup timer is on, but it runs backup.sh from somewhere else." \
                    "Setting it up again from $SCRIPT_DIR."
            fi
            first_backup
            install_backup_timer
        elif [[ $MODE == install ]] && ! backup_units_current; then
            note "The backup's systemd files are out of date (this update changed them). Updating them."
            install_backup_units
            as_root systemctl try-restart "$(backup_unit).timer"
            ok "Updated the backup's systemd files"
        elif [[ $MODE == check ]]; then
            restore_test
        fi
    fi
    verify_backups
}

# --- Notifications -----------------------------------------------------

# The outputs, in the order they're offered. Each is switched on in
# monitor.env, which MONITOR_ENV_FILE in .env points at; the monitor reads
# that file when it starts (monitor.py Config.from_env). The only thing this
# section writes to .env is that pointer.
#
# The order of work: before the first question, monitor.env and the pointer
# are created if they're missing, with every output off (ensure_monitor_file).
# Then the questions. The one exception to "questions first" is the bundled
# ntfy: it's started once its topic is answered, before the user question,
# because the default phone user and the phone user are both looked up in
# the running container. Nothing else changes until
# every answer is in: then the phone user and token are made (prepare_ntfy),
# monitor.env is written (write_monitor_settings), and containers are started
# or stopped (apply_outputs).
OUTPUT_NAMES=(ntfy metrics graylog loki)
MONITOR_FILE=       # absolute path of monitor.env, from find_monitor_file
MONITOR_CONFIGURED=no
CHOSEN=()           # the outputs answered for this run, in OUTPUT_NAMES order
BAD_WORD=           # the word parse_outputs rejected
URL_PATH=           # the path of the URL valid_http_url last checked
NEW_TOKEN=          # the token ntfy_new_token made
NTFY_MODE_NEW='' NTFY_TOPIC_NEW='' NTFY_USER_NEW='' NTFY_PW_NEW=''
NTFY_USER_EXISTS=no
NTFY_URL_NEW='' NTFY_TOKEN_NEW='' NTFY_TOKEN_OUT='' NTFY_SEVERITY_NEW=''
STALE_DAYS_NEW='' GRAYLOG_KIND='' GRAYLOG_VALUE=''
LOKI_URL_NEW='' LOKI_AUTH_NEW='' LOKI_USER_NEW='' LOKI_PW_NEW='' LOKI_TOKEN_NEW='' LOKI_TENANT_NEW=''

# These run inside the monitor container, which has the network path and
# settings the monitor itself uses. Secrets come from the container's own
# environment, so they never appear in a command line or in this output.
PY_NTFY_HEALTH='import json, urllib.request
with urllib.request.urlopen("http://ntfy:80/v1/health", timeout=10) as response:
    healthy = json.load(response).get("healthy") is True
raise SystemExit(0 if healthy else 1)'

# deny-all check: an anonymous read of the topic's cache, which changes
# nothing. It must be refused (401 or 403). No credentials are sent, and a
# redirect isn't followed.
PY_NTFY_ANONYMOUS='import os, sys, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None
opener = urllib.request.build_opener(NoRedirect())
request = urllib.request.Request(os.environ["NTFY_URL"] + "/json?poll=1")
try:
    with opener.open(request, timeout=10) as response:
        print("HTTP", response.status, "(anonymous read allowed)")
        raise SystemExit(1)
except urllib.error.HTTPError as exc:
    print("HTTP", exc.code)
    raise SystemExit(0 if exc.code in (401, 403) else 1)
except OSError as exc:
    print("no answer (" + type(exc).__name__ + ")")
    raise SystemExit(1)'

# A TCP connect to the host and port of NTFY_URL, GRAYLOG_GELF_URL,
# GRAYLOG_GELF_TCP or LOKI_PUSH_URL, named as argv[1].
PY_REACH='import os, socket, sys, urllib.parse
name = sys.argv[1]
if name == "GRAYLOG_GELF_TCP":
    host, _, port = os.environ[name].rpartition(":")
    host, port = host.strip("[]"), int(port)
else:
    parts = urllib.parse.urlsplit(os.environ[name])
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
socket.create_connection((host, port), timeout=10).close()
print("reachable")'

# One test notification, posted the way the monitor posts alerts (bearer
# token, no redirects). Retries while ntfy is still starting.
PY_NTFY_TEST='import os, sys, time, urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None
opener = urllib.request.build_opener(NoRedirect())
request = urllib.request.Request(
    os.environ["NTFY_URL"], b"Mileage tracker test notification", method="POST",
    headers={"Authorization": "Bearer " + os.environ["NTFY_TOKEN"],
             "Title": "Mileage tracker test", "Content-Type": "text/plain; charset=utf-8"})
reason = "none"
for attempt in range(10):
    try:
        with opener.open(request, timeout=10) as response:
            print("HTTP", response.status)
            raise SystemExit(0 if 200 <= response.status < 300 else 1)
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code)
        raise SystemExit(1)
    except OSError as exc:
        reason = type(exc).__name__
        time.sleep(2)
print("no answer from ntfy (" + reason + ")")
raise SystemExit(1)'

# One GELF message through the monitor's own transport (HTTP, or TCP with
# the NUL frame and optional TLS, as configured).
PY_GRAYLOG_TEST='import json, os, time
import gelf
config = gelf.GelfConfig.from_env(os.environ)
message = {"version": "1.1", "host": gelf.SOURCE, "timestamp": round(time.time(), 3),
           "short_message": "Mileage tracker install test", "level": gelf.CLEAR_LEVEL,
           "_kind": "install_test"}
try:
    gelf.transport(config)([json.dumps(message, separators=(",", ":")).encode("ascii")])
except gelf.DeliveryError as exc:
    print("failed:", type(exc.cause).__name__, getattr(exc.cause, "code", ""))
    raise SystemExit(1)
print("accepted")'

# One line through the monitor's own Loki transport.
PY_LOKI_TEST='import json, os, time, urllib.error
import loki
config = loki.LokiConfig.from_env(os.environ)
line = json.dumps({"kind": "install_test", "message": "Mileage tracker install test"},
                  separators=(",", ":"))
payload = json.dumps({"streams": [{"stream": {"job": "mileage-tracker"},
                                   "values": [[str(time.time_ns()), line]]}]},
                     separators=(",", ":")).encode("utf-8")
try:
    loki.transport(config)(payload)
except loki.PermanentRejection as exc:
    print("rejected: HTTP", exc.status_code)
    raise SystemExit(1)
except urllib.error.HTTPError as exc:
    print("rejected: HTTP", exc.code)
    raise SystemExit(1)
except OSError as exc:
    print("failed:", type(exc).__name__)
    raise SystemExit(1)
print("accepted")'

# The value of KEY in monitor.env, as the monitor will see it, for defaults,
# kept secrets and mode decisions. One matching pair of quotes is removed
# (Compose strips either kind, and a hand-written file may use double quotes).
# A template placeholder such as <host> or <token>, which the example ships
# with, counts as unset, and so does any value containing < or >: valid
# answers never contain them (valid_secret and the URL and token checks
# refuse them), so nothing real is lost. Empty if unset.
mon_value() {
    local raw
    raw=$(env_raw "$1" "$2")
    if (( ${#raw} >= 2 )) && { [[ $raw == \'*\' ]] || [[ $raw == \"*\" ]]; }; then
        raw=${raw:1:${#raw}-2}
    fi
    if [[ $raw == *[\<\>]* ]]; then raw=; fi
    printf '%s' "$raw"
}

# Whether monitor.env sets KEY to true.
mon_flag() {
    local value
    value=$(mon_value "$MONITOR_FILE" "$1")
    [[ ${value,,} == true ]]
}

# Whether monitor.env turns this output on, counting the way the monitor
# does: only while MONITOR_ENABLED=true, and Loki only with a push URL.
output_on() {
    [[ -n $MONITOR_FILE && -f $MONITOR_FILE ]] || return 1
    mon_flag MONITOR_ENABLED || return 1
    case $1 in
        ntfy) mon_flag NOTIFY_ENABLED ;;
        metrics) mon_flag METRICS_ENABLED ;;
        graylog) mon_flag GRAYLOG_ENABLED ;;
        loki) [[ -n $(mon_value "$MONITOR_FILE" LOKI_PUSH_URL) ]] ;;
    esac
}

# The outputs monitor.env turns on, one per line.
outputs_on() {
    local name
    for name in "${OUTPUT_NAMES[@]}"; do
        if output_on "$name"; then printf '%s\n' "$name"; fi
    done
}

# Whether an output was answered for in this run.
chosen_output() { [[ " ${CHOSEN[*]-} " == *" $1 "* ]]; }

# Whether the ntfy in monitor.env is the bundled container.
ntfy_bundled() { [[ $(mon_value "$MONITOR_FILE" NTFY_URL) == http://ntfy:80/* ]]; }

# Set MONITOR_FILE from MONITOR_ENV_FILE in .env, which Compose reads
# relative to this directory. Returns 0 if that file exists.
find_monitor_file() {
    local pointer
    pointer=$(env_raw .env MONITOR_ENV_FILE)
    MONITOR_FILE=
    if [[ -z $pointer ]]; then return 1; fi
    if [[ $pointer == /* ]]; then MONITOR_FILE=$pointer; else MONITOR_FILE=$SCRIPT_DIR/${pointer#./}; fi
    [[ -f $MONITOR_FILE ]]
}

# Make sure .env points at a monitor.env, creating it from the example (all
# outputs off) if there's none. Sets MONITOR_CONFIGURED to yes when the
# pointer or a monitor.env the owner made already exists. The example itself
# is tracked in Git, so it's never the file secrets go into.
ensure_monitor_file() {
    local pointer
    pointer=$(env_raw .env MONITOR_ENV_FILE)
    MONITOR_CONFIGURED=no
    if [[ -n $pointer ]] && find_monitor_file && [[ $MONITOR_FILE != "$SCRIPT_DIR/monitor.env.example" ]]; then
        MONITOR_CONFIGURED=yes
    else
        if [[ -f monitor.env ]]; then
            # A monitor.env made by hand (DEPLOY_DEBIAN.md Step 11b) is used,
            # not overwritten.
            note "Using the monitor.env already here"
            MONITOR_CONFIGURED=yes
        else
            install -m 600 monitor.env.example monitor.env
            note "Created monitor.env from monitor.env.example, with every output off"
        fi
        if [[ $pointer != ./monitor.env ]]; then env_set .env MONITOR_ENV_FILE ./monitor.env; fi
        find_monitor_file
    fi
    if [[ $(stat -c %a "$MONITOR_FILE") != 600 ]]; then chmod 600 "$MONITOR_FILE"; fi
}

# "ntfy, metrics" for a list of outputs given as comma-separated words.
outputs_text() { local list=$1; printf '%s' "${list:-none}" | sed 's/,/, /g'; }

# The answers to "Outputs to turn on". Accepts commas or spaces, any case,
# and "none" only on its own. Sets CHOSEN and returns 1 with BAD_WORD set on
# a word it doesn't know.
parse_outputs() {
    local word name
    local -a words=()
    local -A seen=()
    CHOSEN=()
    read -r -a words <<< "$(printf '%s' "$1" | tr 'A-Z,' 'a-z ')" || true
    for word in "${words[@]}"; do
        case $word in
            none | ntfy | metrics | graylog | loki) seen[$word]=1 ;;
            *) BAD_WORD=$word; return 1 ;;
        esac
    done
    if [[ -n ${seen[none]-} ]]; then
        if (( ${#seen[@]} > 1 )); then BAD_WORD=none; return 1; fi
        return 0
    fi
    for name in "${OUTPUT_NAMES[@]}"; do
        if [[ -n ${seen[$name]-} ]]; then CHOSEN+=("$name"); fi
    done
    return 0
}

ask_outputs() {
    local answer
    while :; do
        ask answer OUTPUTS "Outputs to turn on (ntfy, metrics, graylog, loki; comma-separated, or none)" "$1"
        if parse_outputs "$answer"; then return 0; fi
        if [[ -n ${INSTALL_OUTPUTS+set} ]]; then
            die "INSTALL_OUTPUTS=$answer isn't a list of outputs." \
                "Use any of ntfy, metrics, graylog, loki (comma-separated), or none on its own."
        fi
        note "\"$BAD_WORD\" isn't one of those. Use ntfy, metrics, graylog, loki (comma-separated), or none on its own."
    done
}

# Checks for answers. Each takes one value and returns 0 if it's usable.
valid_topic() { [[ $1 =~ ^[A-Za-z0-9_-]{1,64}$ ]]; }
valid_ntfy_user() { [[ $1 =~ ^[A-Za-z0-9_-]{1,64}$ ]]; }
valid_ntfy_mode() { case $1 in bundled | existing) ;; *) return 1 ;; esac; }
valid_ntfy_token() { [[ $1 =~ ^[A-Za-z0-9._~+/=-]{8,}$ ]]; }
valid_severity() { case $1 in info | warning | error | critical) ;; *) return 1 ;; esac; }
valid_loki_auth() { case $1 in none | bearer | basic) ;; *) return 1 ;; esac; }
valid_days() { [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v d="$1" 'BEGIN { exit !(d > 0 && d <= 36500) }'; }

# An http or https URL with a host, optional port and optional path. Only
# characters Compose won't expand, and no credentials, query or fragment.
# Sets URL_PATH.
valid_http_url() {
    local port
    URL_PATH=
    [[ $1 =~ ^https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(:([0-9]{1,5}))?(/[A-Za-z0-9._~%/+-]*)?$ ]] || return 1
    port=${BASH_REMATCH[3]}
    if [[ -n $port ]] && (( 10#$port < 1 || 10#$port > 65535 )); then return 1; fi
    URL_PATH=${BASH_REMATCH[4]}
}

valid_ntfy_url() {
    valid_http_url "$1" || return 1
    [[ ${URL_PATH##*/} =~ ^[A-Za-z0-9_-]{1,64}$ ]]
}

valid_loki_url() {
    valid_http_url "$1" || return 1
    [[ ${URL_PATH%/} == */loki/api/v1/push ]]
}

# A GELF HTTP URL, or host:port of a GELF TCP input.
valid_graylog_target() {
    local port
    case $1 in
        http://* | https://*) valid_http_url "$1" ;;
        *)
            [[ $1 =~ ^([A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\]):([0-9]{1,5})$ ]] || return 1
            port=${BASH_REMATCH[2]}
            (( 10#$port >= 1 && 10#$port <= 65535 ))
            ;;
    esac
}

# Free-form secrets go into monitor.env in single quotes, which Compose reads
# literally (the viewer's hash works the same way). So they can't hold quotes,
# backslashes, spaces or control characters.
# < and > are refused too: mon_value reads them as a template placeholder.
valid_secret() { [[ -n $1 && $1 =~ ^[!-~]+$ && $1 != *\'* && $1 != *\\* && $1 != *[\<\>]* ]]; }
valid_basic_user() { valid_secret "$1" && [[ $1 != *:* ]]; }
valid_tenant() {
    [[ -z $1 || $1 == - ]] && return 0
    valid_secret "$1" && (( ${#1} <= 128 ))
}

# What the monitor accepts as METRICS_TOKEN: 32 or more printable characters,
# not a template placeholder.
valid_metrics_token() { [[ ${#1} -ge 32 && $1 =~ ^[!-~]+$ && $1 != *[\<\>]* ]]; }

# ask_checked VAR NAME "Question" DEFAULT CHECK "Hint"
# Asks until CHECK accepts the answer. A preset CHECK rejects is an error,
# as with the other steps.
ask_checked() {
    local __var=$1 __name=$2 __question=$3 __default=${4-} __check=$5 __hint=$6 __val preset="INSTALL_$2"
    while :; do
        ask __val "$__name" "$__question" "$__default"
        if "$__check" "$__val"; then printf -v "$__var" '%s' "$__val"; return 0; fi
        if [[ -n ${!preset+set} ]]; then die "$preset=$__val isn't usable. $__hint"; fi
        note "$__hint"
    done
}

# ask_secret_keep VAR NAME "Question" CURRENT CHECK "Hint"
# A secret: Enter keeps CURRENT. Asked again until CHECK accepts it. The
# value is never printed.
ask_secret_keep() {
    local __var=$1 __name=$2 __question=$3 __current=$4 __check=$5 __hint=$6 __got label preset="INSTALL_$2"
    label=$__question
    if [[ -n $__current ]]; then label="$__question (Enter keeps the current one)"; fi
    while :; do
        ask_secret __got "$__name" "$label"
        if [[ -z $__got ]]; then __got=$__current; fi
        if [[ -n $__got ]] && "$__check" "$__got"; then printf -v "$__var" '%s' "$__got"; return 0; fi
        if [[ -n ${!preset+set} ]]; then die "$preset isn't usable. $__hint"; fi
        note "$__hint"
    done
}

# ask_new_secret VAR NAME "Question" MINIMUM [optional]
# A password asked twice. With "optional", an empty first answer leaves VAR
# empty, meaning keep what's there. A preset skips the second question.
ask_new_secret() {
    local __var=$1 __name=$2 __question=$3 __min=$4 __optional=${5-} __first __again preset="INSTALL_$2"
    while :; do
        ask_secret __first "$__name" "$__question"
        if [[ -z $__first && -n $__optional ]]; then printf -v "$__var" '%s' ''; return 0; fi
        if (( ${#__first} < __min )); then
            if [[ -n ${!preset+set} ]]; then die "$preset is shorter than $__min characters."; fi
            note "That's too short; use at least $__min characters."
            continue
        fi
        if [[ -n ${!preset+set} ]]; then printf -v "$__var" '%s' "$__first"; return 0; fi
        ask_secret __again "$__name" "Same again"
        if [[ $__first == "$__again" ]]; then printf -v "$__var" '%s' "$__first"; return 0; fi
        note "Those didn't match. Try again."
    done
}

explain_outputs() {
    note "Optional alerts and dashboards. All are off unless you pick them, and each runs" \
        "on a server you control, with no cloud account or subscription:" \
        "  ntfy     phone notifications when a tracker reports a fault or goes silent" \
        "           (for days without an upload). Bundled runs ntfy here; or use one you run." \
        "  metrics  a token-protected Prometheus page on port 8093, for Grafana" \
        "  graylog  the diagnostics events, sent to a Graylog you already run" \
        "  loki     the same events, pushed to a Loki you already run" \
        "Pick none to leave them all off. Run install.sh again later to change it."
}

ask_stale_days() {
    local current
    current=$(mon_value "$MONITOR_FILE" MONITOR_STALE_DAYS)
    note "How long a tracker can go without uploading before it counts as silent. Trips" \
        "upload when you park somewhere with WiFi, so a car parked for weeks is normal."
    ask_checked STALE_DAYS_NEW STALE_DAYS "Days without an upload before a silence alert" "${current:-7}" \
        valid_days "Use a number of days above 0 and up to 36500 (decimals are fine)."
}

# ntfy: a bundled server here, or an existing one.
ask_ntfy() {
    local current_url default_mode current_sev keep
    current_url=$(mon_value "$MONITOR_FILE" NTFY_URL)
    default_mode=bundled
    if [[ -n $current_url && $current_url != http://ntfy:80/* ]]; then default_mode=existing; fi
    note "ntfy delivers the phone alerts. Bundled runs ntfy in a container here, with its own" \
        "login for the phone and nothing open to anonymous users. Existing sends to an ntfy" \
        "server you already run, given by its topic URL and an access token for that topic."
    ask_checked NTFY_MODE_NEW NTFY_MODE "ntfy: bundled or existing" "$default_mode" valid_ntfy_mode \
        "Answer bundled or existing."
    if [[ $NTFY_MODE_NEW == bundled ]]; then
        ask_bundled_ntfy "$current_url"
    else
        ask_checked NTFY_URL_NEW NTFY_URL "Topic URL of the existing ntfy (its address, then the topic name)" \
            "$current_url" valid_ntfy_url \
            "Use http or https, the server's address, then the topic: letters, digits, _ or -, with no username, ? or #."
        keep=
        if [[ $current_url != http://ntfy:80/* ]]; then keep=$(mon_value "$MONITOR_FILE" NTFY_TOKEN); fi
        ask_secret_keep NTFY_TOKEN_NEW NTFY_TOKEN "Access token for that topic" "$keep" valid_ntfy_token \
            "A token starts with tk_ and has no spaces."
    fi
    current_sev=$(mon_value "$MONITOR_FILE" NOTIFY_MIN_SEVERITY)
    ask_checked NTFY_SEVERITY_NEW NOTIFY_MIN_SEVERITY \
        "Lowest severity to send (info, warning, error, critical)" "${current_sev:-error}" valid_severity \
        "Answer info, warning, error or critical."
}

ask_bundled_ntfy() {
    local current_url=$1 topic_default=mileage-tracker user_default=mileage current_token owner
    if [[ $current_url == http://ntfy:80/* ]]; then topic_default=${current_url##*/}; fi
    ask_checked NTFY_TOPIC_NEW NTFY_TOPIC "Topic name (letters, digits, _ or -)" "$topic_default" valid_topic \
        "Use 1 to 64 letters, digits, _ or -."
    # The container has to be running before the user question: the default
    # user is the owner of the token already in monitor.env, and ntfy reports
    # that owner only from inside the container.
    start_bundled_ntfy
    current_token=$(mon_value "$MONITOR_FILE" NTFY_TOKEN)
    if [[ $current_url == http://ntfy:80/* && -n $current_token ]]; then
        owner=$(ntfy_token_owner "$current_token")
        if valid_ntfy_user "$owner"; then user_default=$owner; fi
    fi
    ask_checked NTFY_USER_NEW NTFY_USER "Username for the phone app" "$user_default" valid_ntfy_user \
        "Use 1 to 64 letters, digits, _ or -."
    if ntfy_user_exists "$NTFY_USER_NEW"; then
        NTFY_USER_EXISTS=yes
        note "$NTFY_USER_NEW already exists in the bundled ntfy. Its password stays as it is" \
            "unless you type a new one."
        ask_new_secret NTFY_PW_NEW NTFY_PASSWORD "New password for $NTFY_USER_NEW (Enter keeps it)" 8 optional
    else
        NTFY_USER_EXISTS=no
        note "The phone app logs in with this username and password. ntfy keeps only a hash."
        ask_new_secret NTFY_PW_NEW NTFY_PASSWORD "Password for $NTFY_USER_NEW (at least 8 characters)" 8
    fi
}

ask_graylog() {
    local current_url current_tcp
    current_url=$(mon_value "$MONITOR_FILE" GRAYLOG_GELF_URL)
    current_tcp=$(mon_value "$MONITOR_FILE" GRAYLOG_GELF_TCP)
    note "Graylog runs on a server you control. Give the URL of a GELF HTTP input (preferred:" \
        "Graylog confirms each message), or host:port of a GELF TCP input."
    ask_checked GRAYLOG_VALUE GRAYLOG_TARGET "GELF HTTP URL (its address, ending in /gelf) or TCP host:port" \
        "${current_url:-$current_tcp}" valid_graylog_target \
        "Use http(s)://host:port/path, or host:port for TCP."
    case $GRAYLOG_VALUE in
        http://* | https://*) GRAYLOG_KIND=url ;;
        *) GRAYLOG_KIND=tcp ;;
    esac
}

ask_loki() {
    local cur_url cur_user cur_pw cur_token cur_tenant cur_auth keep ukeep pkeep
    cur_url=$(mon_value "$MONITOR_FILE" LOKI_PUSH_URL)
    cur_user=$(mon_value "$MONITOR_FILE" LOKI_BASIC_AUTH_USER)
    cur_pw=$(mon_value "$MONITOR_FILE" LOKI_BASIC_AUTH_PASSWORD)
    cur_token=$(mon_value "$MONITOR_FILE" LOKI_BEARER_TOKEN)
    cur_tenant=$(mon_value "$MONITOR_FILE" LOKI_TENANT_ID)
    cur_auth=none
    if [[ -n $cur_token ]]; then
        cur_auth=bearer
    elif [[ -n $cur_user || -n $cur_pw ]]; then
        cur_auth=basic
    fi
    note "Loki is a log store you run; its push URL ends in /loki/api/v1/push."
    ask_checked LOKI_URL_NEW LOKI_URL "Loki push URL (its address, ending in /loki/api/v1/push)" "$cur_url" valid_loki_url \
        "Use http(s)://host[:port]/loki/api/v1/push, with no username, ? or #."
    ask_checked LOKI_AUTH_NEW LOKI_AUTH "Loki login: none, bearer or basic" "$cur_auth" valid_loki_auth \
        "Answer none, bearer or basic."
    case $LOKI_AUTH_NEW in
        bearer)
            keep=
            if [[ $cur_auth == bearer ]]; then keep=$cur_token; fi
            ask_secret_keep LOKI_TOKEN_NEW LOKI_BEARER_TOKEN "Loki bearer token" "$keep" valid_secret \
                "Use a token with no spaces, quotes or backslashes."
            ;;
        basic)
            ukeep=
            pkeep=
            if [[ $cur_auth == basic ]]; then ukeep=$cur_user; pkeep=$cur_pw; fi
            ask_checked LOKI_USER_NEW LOKI_BASIC_AUTH_USER "Loki username" "$ukeep" valid_basic_user \
                "Use a username with no colon, spaces, quotes or backslashes."
            ask_secret_keep LOKI_PW_NEW LOKI_BASIC_AUTH_PASSWORD "Loki password" "$pkeep" valid_secret \
                "Use a password with no spaces, quotes or backslashes."
            ;;
    esac
    ask_checked LOKI_TENANT_NEW LOKI_TENANT_ID \
        "Loki tenant ID for X-Scope-OrgID (Enter keeps the current one; - clears it; Enter if none)" \
        "$cur_tenant" valid_tenant "Use up to 128 characters without spaces, quotes or backslashes."
    if [[ $LOKI_TENANT_NEW == - ]]; then LOKI_TENANT_NEW=; fi
}

# Bundled ntfy: start the profile (its port is checked first). Its auth DB
# and users live in the ntfy-state volume, which nothing here removes.
start_bundled_ntfy() {
    local output
    check_port_free ntfy 8092 ntfy
    if ! output=$(compose --profile ntfy up -d ntfy < /dev/null 2>&1); then
        printf '%s\n' "$output" | indent
        die "docker compose couldn't start the bundled ntfy (output above)." \
            "Its log: docker compose --profile ntfy logs ntfy"
    fi
}

# Whether the bundled ntfy already has this user (ntfy user list).
ntfy_user_exists() {
    local listing
    listing=$(compose --profile ntfy exec -T ntfy ntfy user list < /dev/null 2> /dev/null) || return 1
    grep -Eq "(^|[^A-Za-z0-9_-])user[[:space:]]+$1([^A-Za-z0-9_-]|\$)" <<< "$listing"
}

# The user TOKEN belongs to, printed alone, or nothing. With no username,
# `ntfy token list` lists every user's tokens: a "user <name>" line, then
# "- tk_..." lines under it (cmd/token.go, v2.31.0). The listing holds live
# tokens, so it's captured here and never printed.
ntfy_token_owner() {
    local listing
    if [[ -z $1 ]]; then return 0; fi
    listing=$(compose --profile ntfy exec -T ntfy ntfy token list < /dev/null 2> /dev/null) || return 0
    awk -v want="$1" '
        $1 == "user" { owner = $2 }
        $1 == "-" && owner != "" {
            token = $2
            sub(/,$/, "", token)
            if (token == want) { print owner; exit }
        }' <<< "$listing"
}

# ntfy user add/change-pass USER PASSWORD. The password goes in on stdin,
# is exported as NTFY_PASSWORD inside the container, and never appears on a
# command line, in a file or in this output.
# The script run in the container: read the password, export it for ntfy,
# then run ntfy user with the subcommand and username it's given.
# shellcheck disable=SC2016
NTFY_PASSWORD_SCRIPT='IFS= read -r NTFY_PASSWORD; export NTFY_PASSWORD; exec ntfy user "$1" "$2"'

ntfy_set_password() {
    local output
    if ! output=$(printf '%s\n' "$3" | compose --profile ntfy exec -T ntfy sh -c \
        "$NTFY_PASSWORD_SCRIPT" sh "$1" "$2" 2>&1); then
        printf '%s\n' "$output" | indent
        die "ntfy couldn't set the password for $2 (output above)." \
            "Its log: docker compose --profile ntfy logs ntfy"
    fi
}

ntfy_grant() {
    local output
    if ! output=$(compose --profile ntfy exec -T ntfy ntfy access "$1" "$2" rw < /dev/null 2>&1); then
        printf '%s\n' "$output" | indent
        die "ntfy couldn't give $1 read-write access to $2 (output above)."
    fi
}

# Makes a token for USER and sets NEW_TOKEN. The token is never printed.
ntfy_new_token() {
    local output token
    NEW_TOKEN=
    if ! output=$(compose --profile ntfy exec -T ntfy ntfy token add "$1" < /dev/null 2>&1); then
        printf '%s\n' "$output" | indent
        die "ntfy couldn't make an access token for $1 (output above)."
    fi
    token=$(grep -Eo 'tk_[A-Za-z0-9]+' <<< "$output" | sed -n '1p') || true
    if [[ -z $token ]]; then
        die "ntfy didn't print an access token for $1." \
            "Check what exists with: docker compose --profile ntfy exec ntfy ntfy token list"
    fi
    NEW_TOKEN=$token
}

# Create or update the bundled phone user, give it the topic, and make the
# token the monitor will use. Only runs once every answer is in.
prepare_ntfy() {
    local current owner
    if [[ $NTFY_MODE_NEW != bundled ]]; then
        NTFY_TOKEN_OUT=$NTFY_TOKEN_NEW
        return 0
    fi
    NTFY_URL_NEW=http://ntfy:80/$NTFY_TOPIC_NEW
    if [[ $NTFY_USER_EXISTS == no ]]; then
        ntfy_set_password add "$NTFY_USER_NEW" "$NTFY_PW_NEW"
        ok "Created the phone user $NTFY_USER_NEW"
    elif [[ -n $NTFY_PW_NEW ]]; then
        ntfy_set_password change-pass "$NTFY_USER_NEW" "$NTFY_PW_NEW"
        ok "Changed the password of $NTFY_USER_NEW"
    fi
    ntfy_grant "$NTFY_USER_NEW" "$NTFY_TOPIC_NEW"
    ok "$NTFY_USER_NEW can read and post to $NTFY_TOPIC_NEW"
    # Keep the token already in monitor.env only if it's this user's: after a
    # rename, or a username that exists but owns a different token, its access
    # may not cover the topic.
    current=$(mon_value "$MONITOR_FILE" NTFY_TOKEN)
    if [[ $NTFY_USER_EXISTS == yes && -n $current ]] && ntfy_bundled; then
        owner=$(ntfy_token_owner "$current")
        if [[ $owner == "$NTFY_USER_NEW" ]]; then
            NTFY_TOKEN_OUT=$current
            ok "Kept the access token already in monitor.env (it belongs to $NTFY_USER_NEW)"
            return 0
        fi
        note "The token in monitor.env belongs to ${owner:-nobody listed}, not $NTFY_USER_NEW, so a new one is made."
    fi
    ntfy_new_token "$NTFY_USER_NEW"
    NTFY_TOKEN_OUT=$NEW_TOKEN
    ok "Made an access token for $NTFY_USER_NEW (saved in monitor.env, not printed)"
}

# Comment out KEY in an env file and keep its value, so it no longer applies.
# Empty lines are left alone.
env_comment() {
    local file=$1 key=$2 tmp
    tmp=$(mktemp "$file.XXXXXX")
    KEY=$key awk '
        $0 ~ "^[[:space:]]*" ENVIRON["KEY"] "=" {
            if (substr($0, index($0, "=") + 1) ~ /^[[:space:]]*$/) { print; next }
            print "#" $0
            next
        }
        { print }' "$file" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$file"
}

# Write the answers into monitor.env. Turning an output off flips its switch
# and keeps its URL and secrets in the file, with two exceptions: Loki's
# settings are commented out, because the monitor refuses Loki credentials
# without a push URL, and a value that conflicts with the chosen Graylog
# transport is commented out too.
write_monitor_settings() {
    local f=$MONITOR_FILE
    if (( ${#CHOSEN[@]} )); then
        env_set "$f" MONITOR_ENABLED true
        env_set "$f" MONITOR_STALE_DAYS "$STALE_DAYS_NEW"
    else
        env_set "$f" MONITOR_ENABLED false
    fi

    if chosen_output ntfy; then
        env_set "$f" NOTIFY_ENABLED true
        env_set "$f" NOTIFY_MIN_SEVERITY "$NTFY_SEVERITY_NEW"
        env_set "$f" NTFY_URL "$NTFY_URL_NEW"
        env_set "$f" NTFY_TOKEN "$NTFY_TOKEN_OUT"
    else
        env_set "$f" NOTIFY_ENABLED false
    fi

    if chosen_output metrics; then
        env_set "$f" METRICS_ENABLED true
        if ! valid_metrics_token "$(mon_value "$f" METRICS_TOKEN)"; then
            env_set "$f" METRICS_TOKEN "$(random_hex)"
            note "Made the metrics token (saved in monitor.env, not printed)"
        fi
    else
        env_set "$f" METRICS_ENABLED false
    fi

    if chosen_output graylog; then
        env_set "$f" GRAYLOG_ENABLED true
        if [[ $GRAYLOG_KIND == url ]]; then
            env_set "$f" GRAYLOG_GELF_URL "$GRAYLOG_VALUE"
            env_set "$f" GRAYLOG_GELF_TCP ""
            if [[ $(mon_value "$f" GRAYLOG_GELF_TLS) == true ]]; then
                env_comment "$f" GRAYLOG_GELF_TLS
                warn "GRAYLOG_GELF_TLS=true was commented out" \
                    "It only applies to TCP, and an https:// URL already uses TLS."
            fi
        else
            env_set "$f" GRAYLOG_GELF_TCP "$GRAYLOG_VALUE"
            env_set "$f" GRAYLOG_GELF_URL ""
            if [[ -n $(mon_value "$f" GRAYLOG_GELF_HEADER) ]]; then
                env_comment "$f" GRAYLOG_GELF_HEADER
                warn "GRAYLOG_GELF_HEADER was commented out" \
                    "It only applies to the HTTP URL. To keep it, use an http(s) URL (DEPLOY_DEBIAN.md Step 11d)."
            fi
        fi
    else
        env_set "$f" GRAYLOG_ENABLED false
    fi

    if chosen_output loki; then
        env_set "$f" LOKI_PUSH_URL "$LOKI_URL_NEW"
        case $LOKI_AUTH_NEW in
            bearer)
                env_set "$f" LOKI_BEARER_TOKEN "'$LOKI_TOKEN_NEW'"
                env_set "$f" LOKI_BASIC_AUTH_USER ""
                env_set "$f" LOKI_BASIC_AUTH_PASSWORD ""
                ;;
            basic)
                env_set "$f" LOKI_BASIC_AUTH_USER "'$LOKI_USER_NEW'"
                env_set "$f" LOKI_BASIC_AUTH_PASSWORD "'$LOKI_PW_NEW'"
                env_set "$f" LOKI_BEARER_TOKEN ""
                ;;
            none)
                env_set "$f" LOKI_BEARER_TOKEN ""
                env_set "$f" LOKI_BASIC_AUTH_USER ""
                env_set "$f" LOKI_BASIC_AUTH_PASSWORD ""
                ;;
        esac
        if [[ -n $LOKI_TENANT_NEW ]]; then
            env_set "$f" LOKI_TENANT_ID "'$LOKI_TENANT_NEW'"
        else
            env_set "$f" LOKI_TENANT_ID ""
        fi
    else
        local key
        for key in LOKI_PUSH_URL LOKI_BASIC_AUTH_USER LOKI_BASIC_AUTH_PASSWORD LOKI_BEARER_TOKEN LOKI_TENANT_ID; do
            env_comment "$f" "$key"
        done
    fi
}

# Make the containers match monitor.env. Works from the file, not from this
# run's answers, so it serves new answers and kept outputs alike. The bundled
# ntfy is started if it's on. The monitor is brought up with --build, which
# recreates it only when its image or settings changed (a re-run after new
# server code therefore picks the code up). Nothing is sent by this function,
# and the ingest and viewer are never restarted here.
apply_outputs() {
    if output_on ntfy && ntfy_bundled; then
        start_bundled_ntfy
    fi
    if [[ -n $(outputs_on) ]]; then
        if [[ $(service_state mileage-monitor monitor) != running ]]; then
            check_port_free mileage-monitor 8093 monitor
        fi
        note "Starting the monitor with these settings (its image is rebuilt if the code changed)"
        if ! compose --profile monitor up -d --build --no-deps mileage-monitor < /dev/null 2>&1 | indent; then
            die "docker compose couldn't start the monitor (output above)." \
                "Its settings are in $MONITOR_FILE"
        fi
    elif [[ $(service_state mileage-monitor monitor) == running ]]; then
        if ! compose --profile monitor stop mileage-monitor < /dev/null 2>&1 | indent; then
            die "docker compose couldn't stop the monitor (output above)."
        fi
        ok "Stopped the monitor, since no output is on"
    fi
    if [[ $(service_state ntfy ntfy) == running ]] && ! { output_on ntfy && ntfy_bundled; }; then
        if ! compose --profile ntfy stop ntfy < /dev/null 2>&1 | indent; then
            die "docker compose couldn't stop the bundled ntfy (output above)."
        fi
        ok "Stopped the bundled ntfy, since phone alerts don't use it now"
    fi
}

# What monitor.py logs when it starts, from monitor.env.
expected_startup() {
    printf 'Monitor enabled; ntfy %s, Graylog %s, Prometheus %s, Loki %s' \
        "$(onoff_word ntfy)" "$(onoff_word graylog)" "$(onoff_word metrics)" "$(onoff_word loki)"
}

onoff_word() {
    if output_on "$1"; then printf enabled; else printf disabled; fi
}

# The monitor's last startup line (or refusal) in its log, if any.
monitor_startup_line() {
    { compose --profile monitor logs --no-color mileage-monitor < /dev/null 2>&1 || true; } \
        | grep -E 'Monitor (enabled|disabled)|Invalid monitor configuration' | sed -n '$p' || true
}

# After the monitor is started: wait until it's running with these settings,
# or report the refusal or crash it gave.
wait_monitor_start() {
    local deadline=$((SECONDS + 90)) state line expected
    expected=$(expected_startup)
    while :; do
        line=$(monitor_startup_line)
        if [[ $line == *"Invalid monitor configuration"* ]]; then
            problem "The monitor refused the settings in monitor.env" "${line##*| }" \
                "Fix the setting it names in $MONITOR_FILE, then run install.sh again."
            return 1
        fi
        state=$(service_state mileage-monitor monitor)
        if [[ $state == running && $line == *"$expected"* ]]; then
            ok "The monitor is running with these settings"
            return 0
        fi
        if [[ $state == exited || $state == dead || $state == restarting ]] || (( SECONDS >= deadline )); then
            compose --profile monitor logs --no-color --tail 25 mileage-monitor < /dev/null 2>&1 | indent || true
            problem "The monitor didn't come up (state: $state)" \
                "Its last log lines are above. Fix the cause, then run install.sh again."
            return 1
        fi
        sleep 2
    done
}

# Run a check script in the monitor container. Prints ok, or a problem with
# the last line of its output.
monitor_check() {  # OK PROBLEM HINT SCRIPT [ARGUMENT]
    local output
    if output=$(compose --profile monitor exec -T mileage-monitor python -c "$4" "${5-}" < /dev/null 2>&1); then
        ok "$1"
    else
        problem "$2" "${output##*$'\n'}" "$3"
    fi
}

# Whether the monitor can connect to the host and port named by ENV_NAME.
reach_check() {  # ENV_NAME LABEL
    monitor_check "The monitor can connect to the $2" "The monitor can't connect to the $2" \
        "Check the address in $MONITOR_FILE, and that the server is up and reachable from this server." \
        "$PY_REACH" "$1"
}

# Metrics, from the host: refused without the token, served with it.
check_metrics() {
    local url token file code
    url="http://${BIND_ADDR:-127.0.0.1}:8093/metrics"
    token=$(mon_value "$MONITOR_FILE" METRICS_TOKEN)
    if ! valid_metrics_token "$token"; then
        problem "METRICS_TOKEN in monitor.env is missing or too short" \
            "Metrics need a token of at least 32 printable characters. Run install.sh with metrics on to make one."
        return 0
    fi
    code=$(http_status "$url")
    if [[ $code != 401 ]]; then
        if [[ $code == 000 ]]; then
            problem "Nothing answers on port 8093 (metrics)" \
                "Check the monitor's log: docker compose --profile monitor logs mileage-monitor"
        else
            problem "Metrics answered $code without a token, not 401" \
                "Anyone who can reach port 8093 could read them. Check: docker compose --profile monitor logs mileage-monitor"
        fi
        return 0
    fi
    ok "Metrics refuse a request without the token (401)"
    file=$(mktemp)
    TMP_FILES+=("$file")
    code=$(http_fetch "$file" -H @<(printf 'Authorization: Bearer %s\n' "$token") "$url")
    if [[ $code == 200 ]] && grep -q 'mileage_tracker_' "$file"; then
        ok "Metrics answer with the token (200) and report mileage_tracker_ series"
    else
        problem "Metrics didn't answer with the token (HTTP $code)" \
            "The token in $MONITOR_FILE should match the one the monitor loaded. Restart it: docker compose --profile monitor up -d mileage-monitor"
    fi
}

# Read-only checks of what monitor.env turns on, in both modes. Nothing is
# sent to a phone, Graylog or Loki from here.
check_outputs() {
    local list monitor_ok=no pointer
    if ! find_monitor_file; then
        pointer=$(env_raw .env MONITOR_ENV_FILE)
        if [[ -n $pointer ]]; then
            warn "MONITOR_ENV_FILE in .env points at $pointer, which doesn't exist" \
                "Create that file, or set MONITOR_ENV_FILE=./monitor.env, then run install.sh."
        else
            ok "Notification outputs: off"
        fi
        return 0
    fi
    list=$(outputs_on | paste -sd, -)
    if [[ -z $list ]]; then
        ok "Notification outputs: off"
        return 0
    fi
    ok "Notification outputs on: ${list//,/, }"
    if [[ $(stat -c %a "$MONITOR_FILE") != 600 ]]; then
        warn "monitor.env can be read by other users on this machine" "chmod 600 $MONITOR_FILE"
    fi

    if [[ $(service_state mileage-monitor monitor) != running ]]; then
        problem "The monitor isn't running" \
            "Start it with: docker compose --profile monitor up -d mileage-monitor"
    elif [[ $(monitor_startup_line) == *"$(expected_startup)"* ]]; then
        ok "The monitor is running with the settings in monitor.env"
        monitor_ok=yes
    else
        problem "The monitor is running with other settings than monitor.env" \
            "Apply them: docker compose --profile monitor up -d mileage-monitor"
    fi

    if output_on ntfy; then
        if ntfy_bundled; then
            if [[ $(service_state ntfy ntfy) == running ]]; then
                ok "The bundled ntfy is running"
            else
                problem "The bundled ntfy isn't running" "Start it with: docker compose --profile ntfy up -d ntfy"
            fi
            if grep -Eq '^auth-default-access:[[:space:]]*"?deny-all"?' "$SCRIPT_DIR/ntfy/server.yml" 2> /dev/null; then
                ok "ntfy/server.yml sets auth-default-access to deny-all"
            else
                problem "ntfy/server.yml doesn't set auth-default-access to deny-all" \
                    "Without it, ntfy may let anyone read the topics. Restore that setting, then: docker compose --profile ntfy up -d ntfy"
            fi
            if [[ $monitor_ok == yes ]]; then
                monitor_check "The bundled ntfy's health check passes from the monitor" \
                    "The bundled ntfy's health check fails from the monitor" \
                    "Its log: docker compose --profile ntfy logs ntfy" "$PY_NTFY_HEALTH"
                monitor_check "The bundled ntfy refuses anonymous reads (deny-all)" \
                    "The bundled ntfy answers anonymous reads, so anyone who can reach port 8092 can read the alerts" \
                    "Check ntfy/server.yml has auth-default-access: \"deny-all\", then: docker compose --profile ntfy up -d ntfy" \
                    "$PY_NTFY_ANONYMOUS"
            fi
        elif [[ $monitor_ok == yes ]]; then
            reach_check NTFY_URL "ntfy server"
        fi
    fi
    if output_on graylog && [[ $monitor_ok == yes ]]; then
        if [[ -n $(mon_value "$MONITOR_FILE" GRAYLOG_GELF_URL) ]]; then
            reach_check GRAYLOG_GELF_URL "Graylog input"
        else
            reach_check GRAYLOG_GELF_TCP "Graylog input"
        fi
    fi
    if output_on loki && [[ $monitor_ok == yes ]]; then
        reach_check LOKI_PUSH_URL "Loki server"
    fi
    if output_on metrics; then
        check_metrics
    fi
}

# Print the Prometheus job with <host> filled in, and say where its token is.
show_scrape_config() {
    local addr=${BIND_ADDR:-$(default_addr)}
    note "For Prometheus, add this job to its scrape_configs (the source is grafana/prometheus-scrape.yml):"
    if [[ -f grafana/prometheus-scrape.yml ]]; then
        sed -e '/^#/d' -e "s|<host>|${addr:-<this server>}|" grafana/prometheus-scrape.yml | indent
    fi
    note "The token it needs is in $MONITOR_FILE. Copy only its value into the token file on" \
        "the Prometheus host (bearer_token_file), not into the YAML. To see it:" \
        "grep '^METRICS_TOKEN=' $MONITOR_FILE"
}

# Send one test through each output that needs a real destination. These
# are the only steps here that send anything out.
test_ntfy() {
    local topic result status=0 last addr
    topic=$(mon_value "$MONITOR_FILE" NTFY_URL)
    topic=${topic##*/}
    addr=${BIND_ADDR:-$(default_addr)}
    if ntfy_bundled; then
        note "Phone setup (one time):" \
            "1. Install the free ntfy app (Android: Play Store or F-Droid; iOS: App Store)." \
            "2. Add the server http://${addr:-<this server>}:8092, then log in as $NTFY_USER_NEW with its password." \
            "3. Subscribe to the topic $topic." \
            "The phone only gets alerts on home WiFi or over a VPN into this network. Away from it," \
            "they wait in ntfy's cache for up to 72 hours. Instant push on iOS is an extra step:" \
            "DEPLOY_DEBIAN.md Step 11a."
    else
        note "On the phone, subscribe to $topic on that ntfy server, with an account that can read it."
    fi
    if ! confirm NTFY_READY "Is the phone set up? y sends a test notification now (n skips it)" y; then
        warn "Skipped the ntfy test" "Send one later with DEPLOY_DEBIAN.md Step 11c."
        return 0
    fi
    result=$(compose --profile monitor exec -T mileage-monitor python -c "$PY_NTFY_TEST" < /dev/null 2>&1) || status=$?
    last=${result##*$'\n'}
    if (( status == 0 )); then
        ok "ntfy accepted the test notification ($last)"
        if confirm NTFY_ARRIVED "Did it arrive on the phone?" y; then
            ok "The test notification reached the phone"
        else
            warn "The test notification didn't reach the phone" \
                "Likely causes: the phone isn't on home WiFi or the VPN, it isn't subscribed to $topic," \
                "or the app is logged in with the wrong username or password. Test again with DEPLOY_DEBIAN.md Step 11c."
        fi
    else
        problem "ntfy didn't accept the test notification ($last)" \
            "A 401 or 403 means the token or its access to $topic is wrong: docker compose --profile ntfy exec ntfy ntfy access" \
            "Anything else: docker compose --profile ntfy logs ntfy"
    fi
}

test_graylog() {
    local result status=0
    result=$(compose --profile monitor exec -T mileage-monitor python -c "$PY_GRAYLOG_TEST" < /dev/null 2>&1) || status=$?
    if (( status == 0 )); then
        ok "Graylog's input took the test message"
        if confirm GRAYLOG_ARRIVED "Does it show up in Graylog (search source:mileage-tracker)?" y; then
            ok "The test message shows up in Graylog"
        else
            warn "The test message didn't show up in Graylog" \
                "Check the input is running, that the search covers the time now, and that it's the right Graylog."
        fi
    else
        problem "Graylog didn't take the test message (${result##*$'\n'})" \
            "Check the address in $MONITOR_FILE and that the input is running."
    fi
}

test_loki() {
    local result status=0
    result=$(compose --profile monitor exec -T mileage-monitor python -c "$PY_LOKI_TEST" < /dev/null 2>&1) || status=$?
    if (( status == 0 )); then
        ok "Loki accepted a test line (search {job=\"mileage-tracker\"} in Grafana)"
    else
        problem "Loki didn't accept the test line (${result##*$'\n'})" \
            "Check LOKI_PUSH_URL and the login in $MONITOR_FILE, and that Loki is up."
    fi
}

send_tests() {
    if output_on ntfy; then test_ntfy; fi
    if output_on graylog; then test_graylog; fi
    if output_on loki; then test_loki; fi
    if output_on metrics; then show_scrape_config; fi
}

# The bundled ntfy's HTTP server can take a moment after its container starts.
# Wait (up to a minute) for its health check before the checks that need it;
# if it still doesn't answer, check_outputs reports that.
wait_for_bundled_ntfy() {
    local deadline=$((SECONDS + 60))
    if ! { output_on ntfy && ntfy_bundled; }; then return 0; fi
    until compose --profile monitor exec -T mileage-monitor python -c "$PY_NTFY_HEALTH" \
        < /dev/null > /dev/null 2>&1; do
        if (( SECONDS >= deadline )); then return 0; fi
        sleep 2
    done
}

# Check mode only checks. Install mode asks, applies and checks; a kept set of
# outputs is applied without any question and without any test being sent.
setup_outputs() {
    section "Notifications"
    if [[ $MODE == check ]]; then
        check_outputs
        return 0
    fi
    ensure_monitor_file
    local default=none current
    if [[ $MONITOR_CONFIGURED == yes ]]; then
        current=$(outputs_on | paste -sd, -)
        note "Notification outputs now: $(outputs_text "$current")"
        # A preset list means "set them to this", so it doesn't need the keep
        # question answered too. KEEP_OUTPUTS, if preset as well, still wins.
        if [[ -n ${INSTALL_OUTPUTS+set} && -z ${INSTALL_KEEP_OUTPUTS+set} ]]; then
            note "INSTALL_OUTPUTS is set, so the keep question is skipped and that list is used." \
                "Set INSTALL_KEEP_OUTPUTS as well to answer the keep question instead."
        elif confirm KEEP_OUTPUTS "Keep them as they are?" y; then
            ok "Kept the notification outputs"
            apply_outputs
            if [[ -n $(outputs_on) ]]; then wait_monitor_start; fi
            wait_for_bundled_ntfy
            check_outputs
            return 0
        fi
        default=${current:-none}
    else
        explain_outputs
    fi
    ask_outputs "$default"

    if (( ${#CHOSEN[@]} )); then
        ask_stale_days
        if chosen_output ntfy; then ask_ntfy; fi
        if chosen_output graylog; then ask_graylog; fi
        if chosen_output loki; then ask_loki; fi
    fi

    # Every answer is in. Now make the changes.
    if chosen_output ntfy; then prepare_ntfy; fi
    write_monitor_settings
    apply_outputs
    if (( ${#CHOSEN[@]} )); then
        wait_monitor_start
        wait_for_bundled_ntfy
        check_outputs
        send_tests
    else
        ok "Notification outputs: off"
    fi
}

# --- Summary -----------------------------------------------------------

summary() {
    section "Summary"
    local item
    if [[ $BOOT_UNVERIFIED == yes ]]; then
        note "Boot recovery remains unverified. The running-service and restart-policy checks above are separate."
    fi
    if (( ${#WARNINGS[@]} )); then
        printf 'Worth a look:\n'
        for item in "${WARNINGS[@]}"; do printf '  - %s\n' "$item"; done
        echo
    fi
    if [[ $MODE == check ]]; then
        if (( FAILURES )); then
            printf '%s%d check(s) failed.%s Each FAIL above says what to do.\n' "$RED" "$FAILURES" "$PLAIN"
            exit 1
        fi
        if [[ $BOOT_UNVERIFIED == yes ]]; then
            printf '%sThe checks above passed; boot recovery remains unverified.%s\n' "$GREEN" "$PLAIN"
        else
            printf '%sEverything checks out.%s\n' "$GREEN" "$PLAIN"
        fi
        return 0
    fi

    local host=${BIND_ADDR:-$(default_addr)} viewer_url
    viewer_url=$(http_url "${VIEWER_BIND_ADDR:-${host:-<this server>}}" 8091)
    printf '%sThe server is installed and working.%s\n' "$GREEN" "$PLAIN"
    local admin
    admin=$(env_raw viewer.env VIEWER_USERNAME)
    printf 'The viewer is at %s/\n' "$viewer_url"
    if [[ $VIEWER_COOKIE_SECURE == true ]]; then
        printf 'Log in through the proxy HTTPS name; Secure cookies require HTTPS.\n'
    fi
    printf 'The admin login is %s (kept in %s/viewer.env).\n' "${admin:-the one in viewer.env}" "$SCRIPT_DIR"
    if (( ${#EVERYDAY_LOGINS[@]} )); then
        printf 'Everyday logins: %s\n' "$(everyday_text)"
    else
        printf 'There are no everyday logins yet.\n'
    fi
    printf 'The admin adds, turns off and resets everyday logins at %s/admin\n\n' "$viewer_url"
    printf 'The upload token is INGEST_AUTH_TOKEN in %s/.env; keep it private.\n\n' "$SCRIPT_DIR"
    # setup.sh installs this bundle before invoking us. It also identifies
    # release installs when install.sh is rerun directly, without the wrapper.
    if [[ ! -d $SCRIPT_DIR/firmware-src ]]; then
        printf 'For the firmware, copy firmware/secrets.h.example to\n'
        printf 'firmware/04_trip_logger/secrets.h, fill in your WiFi name and password,\n'
        printf 'and set these:\n\n'
        printf '    #define SERVER_HOST   "%s"\n' "${host:-<this server>}"
        printf '    #define SERVER_PORT   8090\n\n'
        printf 'Set AUTH_TOKEN to INGEST_AUTH_TOKEN from the .env file above.\n'
        printf 'These are built into the firmware. If this address or the token ever\n'
        printf 'changes, the board stops uploading until you update secrets.h and\n'
        printf 'flash it again.\n\n'
    fi
    printf 'Next:\n'
    if [[ -d $SCRIPT_DIR/firmware-src ]]; then
        printf '  - setup.sh offers to build your tracker firmware next. If skipped,\n'
        printf '    run this on the server (the builder reads the token from .env):\n'
        printf '      bash %q --install-dir %q\n' "$SCRIPT_DIR/firmware-builder/build.sh" "$SCRIPT_DIR"
        printf '    Changing WiFi, server address or token requires a rebuild and flash.\n'
        printf '  - On the PC connected to the board, use the flash script in\n'
        printf '    README.md#step-2--flash-the-tracker (Step 2). For a first install,\n'
        printf '    it backs up factory flash, then asks you to type ERASE.\n'
        printf '  - Follow README.md#step-3--first-drive-and-device-health (Step 3):\n'
        printf '    first drive, Device health snapshot, then the one-time cleanup flash.\n'
    else
        printf '  - Build and flash from source: DEPLOY_DEBIAN.md#advanced-local-firmware-settings\n'
        printf '    (Step 8), including first-flash setup and diagnostics enrollment.\n'
    fi
    if [[ $RELOGIN == yes ]]; then
        printf '  - Log out and back in, so docker works without sudo.\n'
    fi
    if [[ $MILEAGE_PLATFORM == unraid ]]; then
        printf '  - At a suitable time, test array stop/start and a reboot from the Unraid UI.\n'
        printf '    After the array and Docker start, check it all came back:\n'
    else
        printf '  - Reboot (sudo reboot), then check it all came back:\n'
    fi
    printf '      bash %s/install.sh --check\n' "$SCRIPT_DIR"
    if [[ $FIRST_BACKUP == yes ]]; then
        # --check tests an existing snapshot; make one after real data
        # arrives instead of checking the empty install-time copy (#23).
        printf '  - After the first real drives, annotate one in the viewer, then run:\n'
        local backup_runner=$SCRIPT_DIR/backup/backup.sh
        if [[ $MILEAGE_PLATFORM == unraid ]]; then backup_runner=$SCRIPT_DIR/backup/scheduled.sh; fi
        printf '      bash %q\n' "$backup_runner"
        printf '      bash %q --check\n' "$SCRIPT_DIR/install.sh"
        printf '    Or wait for a successful nightly backup before running --check.\n'
        printf '    --check tests the newest existing backup; it does not make one.\n'
        printf '    Confirm its trip and annotation counts match the viewer. An empty\n'
        printf '    snapshot proves the plumbing, but not that real data comes back.\n'
    fi
}

main() {
    trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR
    trap cleanup EXIT
    cd "$SCRIPT_DIR"
    if [[ $MODE == check ]]; then
        printf '%sChecking the mileage tracker install in %s%s\n' "$BOLD" "$SCRIPT_DIR" "$PLAIN"
        printf 'This uploads a test trip and deletes it again. Nothing else changes.\n'
    else
        printf '%sInstalling the mileage tracker server in %s%s\n' "$BOLD" "$SCRIPT_DIR" "$PLAIN"
    fi

    preflight
    setup_docker
    configure
    if [[ $MODE == install ]]; then
        ask_root_options
        build_images
        setup_viewer_login
        setup_everyday_login
        start_services
    else
        section "Services"
    fi
    set_service_urls
    if verify_services; then
        verify_end_to_end || true
    fi
    verify_reboots
    setup_backups
    setup_outputs
    summary
}

main
