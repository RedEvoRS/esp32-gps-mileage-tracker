#!/usr/bin/env bash
# Install the mileage tracker server on Debian (or Ubuntu), checking each
# step as it goes.
#
#   bash install.sh           set everything up, or finish a setup that
#                             stopped partway
#   bash install.sh --check   leave configuration unchanged; upload and
#                             remove a test trip, optionally test login
#
# Run it as your normal login user, not root. If any step will need root,
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
#   Start           build, viewer login, start, wait for healthy (Step 5)
#   End to end      upload sample.gpx, find it in the viewer and the
#                   exports, then delete it again (Steps 6, 7)
#   Reboots         Docker comes back with the network (Step 9)
#   Backups         off-host copy, restore test, daily timer (Step 10)
# and finally the settings for the firmware's secrets.h (Step 8).
#
# Any question can be answered ahead of time with an INSTALL_<NAME>
# environment variable; the names are in the ask/confirm calls below.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
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
VIEWER_PASSWORD=
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

compose() { "${DOCKER[@]}" compose "$@"; }

container_state() {
    "${DOCKER[@]}" inspect -f '{{.State.Status}}' "$1" 2> /dev/null || echo missing
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
        TZ_NAMES=$(timedatectl list-timezones 2> /dev/null) || TZ_NAMES=
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
    local zone
    zone=$(timedatectl show -p Timezone --value 2> /dev/null) || zone=
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
    if (( EUID == 0 )); then
        die "Run this as your normal login user, not as root or with sudo." \
            "It asks for sudo itself when it needs it. The trip files, the" \
            "backups and the backup timer all belong to the user who runs it."
    fi
    ok "Running as $ME (UID $(id -u)) in $SCRIPT_DIR"

    OS_ID=$(os_release ID)
    OS_CODENAME=$(os_release VERSION_CODENAME)
    local os_name
    os_name=$(os_release PRETTY_NAME)
    case $OS_ID in
        debian | ubuntu) ok "${os_name:-$OS_ID}" ;;
        *) warn "${os_name:-This system} isn't Debian or Ubuntu" \
            "This script can't install Docker here, so it expects it installed already." ;;
    esac

    local f missing=()
    for f in docker-compose.yml .dockerignore .env.example viewer.env.example monitor.env.example sample.gpx \
        common/__init__.py common/diagnostics/{__init__.py,catalog.py,protocol.py,catalog.json} \
        mileage-ingest/{Dockerfile,app.py,diag_store.py,requirements.txt} \
        mileage-viewer/{Dockerfile,app.py,requirements.txt} \
        mileage-viewer/static/{leaflet.js,leaflet.css,gpx.js} \
        mileage-monitor/{Dockerfile,monitor.py,gelf.py,prometheus.py} ntfy/server.yml \
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

    if [[ $(id -u) == "$CONTAINER_UID" ]]; then
        ok "Your UID matches the containers' (1000)"
    else
        DATA_SUDO=(sudo)
        warn "Your UID is $(id -u), not $CONTAINER_UID" \
            "The containers write trip files as UID $CONTAINER_UID, so data/ gets handed to" \
            "that UID, and the backup timer, which runs as you, can't read them." \
            "Backups are skipped below. DEPLOY_DEBIAN.md Steps 3 and 10 explain."
    fi

    local free_kb
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
    if (( ${#DATA_SUDO[@]} )); then
        reasons+=("check and delete the test trip, whose files belong to UID $CONTAINER_UID")
    fi
    # Backups already set up, but the timer is off or runs another copy:
    # setup_backups puts it right without asking.
    if [[ $MODE == install && $(id -u) == "$CONTAINER_UID" ]] \
        && backups_configured && ! { timer_ok && timer_current; }; then
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

apt_get() {
    need_sudo
    sudo DEBIAN_FRONTEND=noninteractive apt-get -q -y "$@" < /dev/null
}

WANTED_PACKAGES=(ca-certificates curl gnupg jq openssl openssh-client rsync iproute2)

have_apt() { command -v apt-get > /dev/null && command -v dpkg-query > /dev/null; }
have_systemd() { [[ -d /run/systemd/system ]]; }

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
        for p in curl jq openssl rsync ssh ip flock; do
            command -v "$p" > /dev/null || missing+=("$p")
        done
        if (( ${#missing[@]} )); then
            die "Commands not found: ${missing[*]}" \
                "Install them with your package manager, then run this again."
        fi
        ok "Commands available: curl jq openssl rsync ssh ip flock"
    fi
}

# Docker's own packages, from Docker's apt repository, exactly as in
# DEPLOY_DEBIAN.md Step 1. Debian's docker.io doesn't ship Compose v2.
install_docker() {
    case $OS_ID in
        debian | ubuntu) ;;
        *) die "Docker isn't installed, and this script only installs it on Debian or Ubuntu." \
            "Install Docker Engine and its Compose plugin (https://docs.docker.com/engine/install/)," \
            "then run this again." ;;
    esac
    [[ -n $OS_CODENAME ]] || die "Couldn't tell which $OS_ID release this is (no VERSION_CODENAME in /etc/os-release)."
    need_sudo
    note "Installing Docker from Docker's apt repository"
    # A second entry for the same repository with a different key path
    # makes apt refuse to update, so reuse one set up some other way
    # (Docker's newer instructions write docker.sources).
    if grep -rqs --exclude=docker.list download.docker.com /etc/apt/sources.list /etc/apt/sources.list.d/; then
        note "Docker's repository is already in your apt sources; using that"
    else
        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" \
            | sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
        sudo chmod a+r /etc/apt/keyrings/docker.gpg
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$OS_ID" "$OS_CODENAME" \
            | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo chmod 644 /etc/apt/sources.list.d/docker.list
    fi
    apt_get update
    apt_get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker
}

setup_docker() {
    section "Docker"
    install_packages

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
    if [[ ${compose_version#v} != [2-9]* ]]; then
        die "Docker Compose $compose_version is too old; this needs v2 or newer."
    fi
    ok "$(docker --version | cut -d, -f1), Compose $compose_version"

    # Docker's daemon has to be running and start at boot.
    if [[ -d /run/systemd/system && $(systemctl is-active docker 2> /dev/null) != active ]]; then
        [[ $MODE == install ]] || die "The Docker service isn't running." "sudo systemctl enable --now docker"
        need_sudo
        sudo systemctl enable --now docker
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
                sudo usermod -aG docker "$ME"
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
    if [[ ! -d data ]]; then
        [[ $MODE == install ]] || { problem "There's no data/ directory" "Run install.sh."; return 0; }
        mkdir data
    fi
    if [[ $(stat -c %u data) != "$CONTAINER_UID" ]]; then
        if [[ $MODE == install ]]; then
            need_sudo
            sudo chown -R "$CONTAINER_UID:$CONTAINER_UID" data
        else
            problem "data/ doesn't belong to UID $CONTAINER_UID, so uploads will fail" \
                "sudo chown -R $CONTAINER_UID:$CONTAINER_UID $SCRIPT_DIR/data"
            return 0
        fi
    fi
    ok "data/ belongs to UID $CONTAINER_UID, which the containers run as"
}

setup_env() {
    if [[ ! -f .env ]]; then
        [[ $MODE == install ]] || { problem ".env is missing" "Run install.sh, or see DEPLOY_DEBIAN.md Step 4a."; return 0; }
        install -m 600 .env.example .env
        note "Created .env from .env.example"
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
        ok "The ports are published on $BIND_ADDR only (BIND_ADDR in .env)"
        dhcp_reminder
        return 0
    fi
    if [[ $MODE == check ]]; then
        if [[ -n $BIND_ADDR ]]; then
            problem "BIND_ADDR=$BIND_ADDR in .env isn't an address this server has" \
                "Docker can't publish the ports there, so the containers won't start." \
                "Compare with ip -4 addr show, fix .env or the DHCP reservation, then: docker compose up -d"
        else
            warn "BIND_ADDR isn't set, so ports 8090 and 8091 are open on every interface" \
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
    note "The T-Beam uploads to this server's LAN address, and both ports are" \
        "published on that address only. UFW can't do this for Docker's ports." \
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
        warn "Ports 8090 and 8091 will be open on every interface" \
            "Set BIND_ADDR in .env later to limit them (DEPLOY_DEBIAN.md Step 7)."
    else
        BIND_ADDR=$answer
        env_set .env BIND_ADDR "$BIND_ADDR"
        ok "The ports will be published on $BIND_ADDR only"
        dhcp_reminder
    fi
}

# The zone trip times are shown in, which also decides a trip's month
# folder. Both services read DISPLAY_TZ from .env and won't start without
# it, so nobody gets a zone picked for them.
setup_display_tz() {
    local zone
    zone=$(env_raw .env DISPLAY_TZ)
    if [[ -n $zone ]] && tz_ok "$zone"; then
        ok "Trips are filed and shown in $zone (DISPLAY_TZ in .env)"
        return 0
    fi
    if [[ $MODE == check ]]; then
        if [[ -n $zone ]]; then
            problem "DISPLAY_TZ=$zone in .env isn't a timezone name, so neither service will start" \
                "Use an IANA name like America/Chicago (timedatectl list-timezones lists them)," \
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
                "Use an IANA name like America/Chicago; timedatectl list-timezones lists them."
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
# The login itself is set after the build (setup_viewer_login), because
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
            ok "The viewer login is set up for $(env_raw viewer.env VIEWER_USERNAME)"
        else
            problem "The viewer login in viewer.env isn't set up" \
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
        ask_secret VIEWER_PASSWORD VIEWER_PASSWORD "Viewer password (at least 8 characters)"
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
    local user hash errors
    user=$(env_raw viewer.env VIEWER_USERNAME)
    if viewer_login_set; then
        if confirm KEEP_LOGIN "The viewer login for $user is already set up. Keep it?" y; then
            ok "Kept the viewer login for $user"
            return 0
        fi
    fi

    note "Choose a username and password for the web viewer. Only a scrypt hash of" \
        "the password is stored."
    # Suggest the name already in viewer.env, unless it's just the example's.
    local default=$ME
    if username_ok "$user" && [[ $user != "$(env_raw viewer.env.example VIEWER_USERNAME)" ]]; then
        default=$user
    fi
    while :; do
        ask user VIEWER_USER "Viewer username" "$default"
        if username_ok "$user"; then break; fi
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
    ok "Viewer login set for $user"
}

# Fail early if something else already listens where a port has to go.
check_port_free() {
    local svc=$1 port=$2 taken
    if [[ $(container_state "$svc") == running ]]; then return 0; fi
    taken=$(ss -Hltn "sport = :$port" 2> /dev/null | awk -v want="$BIND_ADDR" '{
        addr = $4
        sub(/:[0-9]+$/, "", addr)
        if (want == "" || addr == want || addr == "0.0.0.0" || addr == "*" || addr == "[::]") print $4
    }') || taken=
    if [[ -n $taken ]]; then
        die "Port $port is already in use (${taken//$'\n'/, }), so $svc can't start." \
            "See what has it: sudo ss -tlnp 'sport = :$port'" \
            "Stop that program, or change the port mapping in docker-compose.yml."
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
# sample.gpx ends on 2026-04-14, so it's filed under data/2026-04/.
#
# Cleanup deletes the test trip, so it may only ever be this run's: the
# ID is new each run, and its path has to be free beforehand. Then
# whatever turns up there came from this upload.
upload_test_trip() {
    local trip token response code body status miles stored
    TEST_ID=$TEST_PREFIX-$(openssl rand -hex 6)
    trip=data/2026-04/$TEST_ID
    if data_exists "$trip.gpx" "$trip.json"; then
        problem "$trip already exists, so the test upload can't use it" \
            "Nothing was uploaded or deleted. Run the check again for a new test ID."
        return 1
    fi
    if ! data_exists "${trip%/*}"; then TEST_NEW_DIR=${trip%/*}; fi
    token=$(env_raw .env INGEST_AUTH_TOKEN)
    TEST_TRIP=$trip
    # No X-Device-Id: this synthetic trip must not enroll a device that the
    # all-device monitor would later report silent. X-Trip-Id owns cleanup.
    response=$(curl -s -m 30 -w '\n%{http_code}' -X POST \
        -H @<(printf 'Authorization: Bearer %s\n' "$token") \
        -H 'Content-Type: application/gpx+xml' \
        -H "X-Trip-Id: $TEST_ID" \
        --data-binary @sample.gpx "$INGEST_URL/upload") || response=$'\n000'
    code=${response##*$'\n'}
    body=${response%$'\n'*}
    status=$(jq -r '.status // empty' <<< "$body" 2> /dev/null) || status=
    miles=$(jq -r '.distance_miles // empty' <<< "$body" 2> /dev/null) || miles=
    stored=$(jq -r '.stored_as // empty' <<< "$body" 2> /dev/null) || stored=
    if [[ $code == 200 && $status == accepted && $stored == */"$TEST_ID.gpx" ]]; then
        TEST_TRIP=data/${stored%.gpx}
        ok "Uploaded sample.gpx with the token; it was filed as $stored"
    else
        # A refusal (4xx) stored nothing, so there's nothing of ours to
        # delete. After a timeout or a 5xx some of it may have been
        # written; the path was free before, so cleanup can remove that.
        if [[ $code == 4?? ]]; then TEST_TRIP=; fi
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

verify_end_to_end() {
    section "End-to-end checks"
    if (( ${#DATA_SUDO[@]} )); then need_sudo; fi
    local url body code
    for url in "$INGEST_URL/health" "$VIEWER_URL/health"; do
        body=$(curl -sS -m 10 "$url" 2>&1) || true
        if [[ $(jq -r '.status // empty' <<< "$body" 2> /dev/null) == ok ]]; then
            ok "$url answers"
        else
            problem "$url doesn't answer from this server" "${body:-no response}" \
                "If BIND_ADDR is set, the containers may need recreating: docker compose up -d"
            return 1
        fi
    done

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
    if "${DATA_SUDO[@]}" cmp -s sample.gpx "$TEST_TRIP.gpx"; then
        ok "The GPX on disk matches the upload byte for byte"
    else
        problem "$TEST_TRIP.gpx is missing or differs from sample.gpx"
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
    if [[ -z $VIEWER_PASSWORD ]]; then
        ask_secret VIEWER_PASSWORD VIEWER_PASSWORD \
            "Viewer password for $user, to test logging in (Enter to skip)" optional
    fi
    if [[ -n $VIEWER_PASSWORD ]]; then
        verify_viewer "$user" || true
    else
        warn "Skipped the login test" "Log in at $VIEWER_URL/ from a browser to check it."
    fi

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
        302\ *) ok "Logged in to the viewer as $user" ;;
        401*)
            problem "The viewer turned down that password for $user" \
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
        && cmp -s "$file" sample.gpx; then
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

verify_binding() {
    local pair svc port mapped
    if [[ -z $BIND_ADDR ]]; then return 0; fi
    # Check these containers' mappings. A response from localhost could
    # belong to another service bound there, so it can't prove exposure.
    for pair in mileage-ingest:8090 mileage-viewer:8091; do
        svc=${pair%:*}
        port=${pair#*:}
        mapped=$(compose port "$svc" "$port" < /dev/null 2> /dev/null) || mapped=
        if [[ $mapped == "$BIND_ADDR:$port" ]]; then
            ok "Port $port is published on $BIND_ADDR only"
        else
            problem "Port $port is published on ${mapped:-nothing}, not $BIND_ADDR" \
                "Recreate the containers so they pick up BIND_ADDR: docker compose up -d"
        fi
    done
}

# --- Reboots -----------------------------------------------------------

verify_reboots() {
    section "Reboots"
    if [[ ! -d /run/systemd/system ]]; then
        warn "systemd isn't running here, so this can't check what starts at boot"
        return 0
    fi
    if [[ $(systemctl is-enabled docker 2> /dev/null) == enabled ]]; then
        ok "Docker starts at boot"
    elif [[ $MODE == install ]]; then
        need_sudo
        sudo systemctl enable --quiet docker
        ok "Docker now starts at boot"
    else
        problem "Docker doesn't start at boot" "sudo systemctl enable docker"
    fi

    local svc policy restart=yes
    for svc in mileage-ingest mileage-viewer; do
        policy=$("${DOCKER[@]}" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$svc" 2> /dev/null) || policy=
        if [[ $policy != unless-stopped && $policy != always ]]; then
            restart=no
            problem "$svc won't come back on its own (restart policy: ${policy:-none})" \
                "docker-compose.yml should say restart: unless-stopped; then docker compose up -d"
        fi
    done
    if [[ $restart == yes ]]; then ok "Both containers start again with Docker (restart: unless-stopped)"; fi

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
        sudo systemctl enable --quiet "$unit"
        ok "Docker waits for the network at boot ($unit)"
    else
        warn "At boot, Docker may start before $BIND_ADDR is up and leave the containers down" \
            "To change that later: sudo systemctl enable $unit"
    fi
}

# --- Backups -----------------------------------------------------------

# Where the backup units go, and where their template expects the files.
UNIT_DIR=/etc/systemd/system
DEFAULT_DIR=/home/$ME/mileage-tracker

backup_unit() { printf 'mileage-backup@%s' "$ME"; }

# The drop-in that points the service at an install outside DEFAULT_DIR.
backup_dropin() { printf '%s/%s.service.d/install-path.conf' "$UNIT_DIR" "$(backup_unit)"; }

dropin_text() {
    printf '[Service]\nExecStart=\nExecStart=/bin/bash "%s/backup/backup.sh"\n' "${SCRIPT_DIR//%/%%}"
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
        confirm SAME_DISK "Use it anyway?" n || return 1
    fi
    ok "Backups will go to $dir"
}

ssh_target_ok() {
    local host=${1%%:*} path=${1#*:} key=$HOME/.ssh/mileage_backup
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
    RSYNC_SSH_CMD="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes"
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

install_backup_timer() {
    local unit dropin
    unit=$(backup_unit)
    dropin=$(backup_dropin)
    need_sudo
    sudo install -m 644 backup/mileage-backup@.service backup/mileage-backup@.timer "$UNIT_DIR/"
    # The unit runs /home/<user>/mileage-tracker/backup/backup.sh. If the
    # files are somewhere else, a drop-in points it at them.
    if [[ $SCRIPT_DIR != "$DEFAULT_DIR" ]]; then
        sudo install -d -m 755 "${dropin%/*}"
        dropin_text | sudo tee "$dropin" > /dev/null
        sudo chmod 644 "$dropin"
        note "Pointed $unit.service at $SCRIPT_DIR"
    elif [[ -e $dropin ]]; then
        sudo rm -f "$dropin"
        note "Pointed $unit.service back at $DEFAULT_DIR"
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable --now --quiet "$unit.timer"
    note "Running the backup once through systemd, the way the timer will"
    if ! sudo systemctl start "$unit.service"; then
        sudo journalctl -u "$unit.service" -n 20 --no-pager 2>&1 | indent || true
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
        first_backup
        install_backup_timer
    else
        ok "backup.conf sends backups to $(conf_get REMOTE)"
        if [[ $MODE == install ]] && ! { timer_ok && timer_current; }; then
            if timer_ok; then
                note "The backup timer is on, but it runs backup.sh from somewhere else." \
                    "Setting it up again from $SCRIPT_DIR."
            fi
            first_backup
            install_backup_timer
        elif [[ $MODE == check ]]; then
            restore_test
        fi
    fi
    verify_backups
}

# --- Summary -----------------------------------------------------------

summary() {
    section "Summary"
    local item
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
        printf '%sEverything checks out.%s\n' "$GREEN" "$PLAIN"
        return 0
    fi

    local host=${BIND_ADDR:-$(default_addr)}
    printf '%sThe server is installed and working.%s\n' "$GREEN" "$PLAIN"
    printf 'The viewer is at http://%s:8091/\n\n' "${host:-<this server>}"
    printf 'For the firmware, copy firmware/secrets.h.example to\n'
    printf 'firmware/04_trip_logger/secrets.h, fill in your WiFi name and password,\n'
    printf 'and set these (the token is also in %s/.env):\n\n' "$SCRIPT_DIR"
    printf '    #define SERVER_HOST   "%s"\n' "${host:-<this server>}"
    printf '    #define SERVER_PORT   8090\n'
    printf '    #define AUTH_TOKEN    "%s"\n\n' "$(env_raw .env INGEST_AUTH_TOKEN)"
    printf 'These are built into the firmware. If this address or the token ever\n'
    printf 'changes, the board stops uploading until you update secrets.h and\n'
    printf 'flash it again.\n\n'
    printf 'Next:\n'
    if [[ $RELOGIN == yes ]]; then
        printf '  - Log out and back in, so docker works without sudo.\n'
    fi
    printf '  - Reboot (sudo reboot), then check it all came back:\n'
    printf '      bash %s/install.sh --check\n' "$SCRIPT_DIR"
    printf '  - Flash the board, check it works and put it in the car: README.md at\n'
    printf '    the top of the repo, "Setting it up", steps 3 to 5. A new board needs\n'
    printf '    a one-time storage setup on its first flash (firmware/README.md).\n'
    if [[ $FIRST_BACKUP == yes ]]; then
        # --check tests an existing snapshot; make one after real data
        # arrives instead of checking the empty install-time copy (#23).
        printf '  - After the first real drives, annotate one in the viewer, then run:\n'
        printf '      bash %q\n' "$SCRIPT_DIR/backup/backup.sh"
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
        start_services
    else
        section "Services"
    fi
    local host=${BIND_ADDR:-127.0.0.1}
    INGEST_URL=http://$host:8090
    VIEWER_URL=http://$host:8091
    if verify_services; then
        verify_end_to_end || true
    fi
    verify_reboots
    setup_backups
    summary
}

main
