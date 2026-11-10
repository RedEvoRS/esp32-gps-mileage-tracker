# Debian Deployment Runbook — Mileage Tracker Server

Using Unraid? Follow [DEPLOY_UNRAID.md](DEPLOY_UNRAID.md) instead.

Deploys both services from `server/`:

| Service | Port | Auth | Purpose |
|---|---|---|---|
| `mileage-ingest` | 8090 | Bearer token | Accepts GPX uploads from the T-Beam |
| `mileage-viewer` | 8091 | Session login | Trip list, map audit, annotate, CSV/XLSX export |
| `ntfy` | 8092 | Topic token | Optional self-hosted phone notifications |
| `mileage-monitor` | 8093 | Bearer token when enabled | Optional Prometheus `/metrics` endpoint in the `monitor` profile |

GPX is GPS Exchange Format, the track file the board uploads. This is the
server half of the project. The [top-level README](README.md)
gives the complete setup. The board is the T-Beam Supreme with MAX-M10S
GPS and a SanDisk 32 GB Max Endurance microSD card. Step 8 covers its
server-built firmware, flashing and one-time diagnostics enrollment. The retired
T-Beam V1.2's last firmware and instructions are on the frozen
[`tbeam-v1.2` branch](https://github.com/RedEvoRS/esp32-gps-mileage-tracker/tree/tbeam-v1.2).

As of 2026-11-01 (UTC), firmware `v1.00` (tag `fw-v1.00`) and server v1.00
are the first public release. Unraid has not yet been verified on a real box.

Assumes: Debian 13, a non-root user with sudo, SSH reachable.
Placeholders: `<user>` = your Debian login, `<host>` = server IP or hostname,
`<admin>` = the viewer's admin username you choose and `<everyday-user>` = the
username of an everyday viewer login. Neither viewer username is your Debian login.
Replace the angle-bracket placeholders before running commands. The server
SSH login is separate from the viewer's admin and everyday logins and from the
upload token the board uses.

For the supplied automatic backups, the server login currently needs
numeric user ID **1000** (`id -u`). The installer skips backups for other
IDs; changing ownership of `data/` alone does not give that login access
to the private trip files. See Steps 3 and 10 before choosing the account.

<a id="the-quick-way-installsh"></a>

## The quick way: `setup.sh`

Prepare SSH and Docker in Steps 0–1, then run the release one-liner in
[Step 2](#step-2--run-setupsh). It downloads and verifies the release,
runs the existing interactive `install.sh`, and offers to build your
personal tracker firmware on the server. No source clone is needed.
The PC connected to the board then runs the flash script from
[README Step 2](README.md#step-2--flash-the-tracker).

`install.sh` configures the local network (LAN) bind address, timezone, admin
login, optional everyday login, backups and optional alert/dashboard outputs, and builds/starts
the services. It tests a sample upload, admin login, list, map and both
exports, then removes the sample. It also tests the everyday login when that
login is added during this run and its password is supplied. It asks for sudo when needed; run
setup as your normal login. A failure stops the process: fix the reported
problem and rerun. Existing valid settings are checked and can be kept.
The rest of this runbook explains those steps and the manual alternatives.

The closing summary points release installs to the firmware build offered
next by `setup.sh` (or the command to run it later), then README Steps 2–3
for flashing on the PC, the first drive, Device health and the one-time
cleanup flash. Manual source installs get `secrets.h` guidance instead.
The summary identifies `INGEST_AUTH_TOKEN` in the install directory's `.env`
without printing its value. It names the admin login, lists everyday logins
when present, and points to `/admin` to manage them.

To recheck later without changing configuration:

```bash
bash ~/mileage-tracker/install.sh --check
```

This uploads and removes a temporary test trip and can test the admin login;
when the viewer is running, it lists everyday login names for information.
It is not a read-only check. For unattended installs, `setup.sh --yes` skips wrapper prompts **and skips the firmware builder**. The inner
installer still needs its `INSTALL_*` presets for unanswered questions;
missing answers fail. The base presets are:

- `INSTALL_BIND_ADDR`: this server's LAN IP, needed if `.env` has no valid
  bind address. `all` deliberately publishes on every interface.
- `INSTALL_DISPLAY_TZ`: a named timezone such as `Europe/London`, needed if
  `.env` has no valid zone.
- `INSTALL_VIEWER_USER` and `INSTALL_VIEWER_PASSWORD`: the admin login,
  needed on first install or when replacing it. The password must have at
  least eight characters. A supplied password also tests the admin login.
- `INSTALL_ADD_EDITOR=y` or `n`: whether to add an optional everyday login.
  With an existing everyday login, the installer keeps it and defaults to
  not adding another. With no terminal and no preset, this optional step is
  skipped.
- `INSTALL_EDITOR_USER` and `INSTALL_EDITOR_PASSWORD`: the username and
  password for the everyday login when adding one. The password must have at
  least eight characters; a preset password is used to test that login too.
  If the username already exists, the installer keeps that login and does not
  change its password.
- `INSTALL_WAIT_ONLINE=y` or `n`: answers whether to make Docker wait for the
  network at boot, if the installer finds a supported network-wait service
  that is not already configured.
- `INSTALL_BACKUPS=y` or `n`: answers the backup question when backups need
  setup. They require a UID 1000 login. With `y`, also supply
  `INSTALL_BACKUP_REMOTE` (an absolute mounted-share path or
  `<user>@<host>:/<backup-path>`) and `INSTALL_PING_URL` (blank to skip the
  optional backup-success ping). The destination must already be usable
  without interactive SSH prompts. `INSTALL_SAME_DISK=y` accepts a destination
  on the server's own disk if asked; it does not protect against that disk
  failing. Use `n` to reject such a destination. The same question is asked
  on an update when an existing backup folder on this machine has no
  destination marker yet (see Step 10), so without `INSTALL_SAME_DISK` an
  unattended update stops at the Backups step. An unmounted share's empty
  folder looks like the server's own disk, so `n` is the safe answer there.
- `INSTALL_OUTPUTS=none`: skips optional notification/dashboard outputs on a
  fresh install. Step 11 lists the presets for enabling them.
- On a rerun, `INSTALL_KEEP_LOGIN=y` and `INSTALL_KEEP_OUTPUTS=y` answer the
  questions about keeping existing settings. Supplying `INSTALL_OUTPUTS`
  without `INSTALL_KEEP_OUTPUTS` chooses that output list instead.

For a fresh install with backups and optional outputs skipped, download and
read `setup.sh` as in Step 2, replace the placeholders, then run this in the
server's Bash shell. Read the password privately so it is not saved in shell
history. Sudo must already work without a prompt for the unattended run
(for example, authenticate with `sudo -v` in this shell first):

```bash
(  # Keep the password preset only for this run, out of command arguments.
IFS= read -rs -p 'Admin password (at least 8 characters): ' INSTALL_VIEWER_PASSWORD; echo
export INSTALL_VIEWER_PASSWORD
env INSTALL_BIND_ADDR='<host>' \
  INSTALL_DISPLAY_TZ=Europe/London \
  INSTALL_VIEWER_USER='<admin>' \
  INSTALL_WAIT_ONLINE=y \
  INSTALL_BACKUPS=n \
  INSTALL_OUTPUTS=none \
  bash setup.sh --yes
)
```

Here `<host>` must be the server's LAN IP, not its hostname, and
`<admin>` is the admin username you choose (not your Debian login). The CI installer job in
[audit-tests.yml](.github/workflows/audit-tests.yml) exercises these base
presets with backups enabled too. See `bash setup.sh --help` for
release/local-bundle options. Run the firmware builder interactively later.

---

## Step 0 — Confirm SSH works

From the computer you'll run the setup from (PowerShell on Windows,
Terminal on macOS or Linux):

```bash
ssh <user>@<host>
```

Once in, note two things you'll need later:

```bash
id -u          # your UID — should be 1000
hostname -I    # the LAN IP the T-Beam will POST to
```

If `id -u` is **not** 1000, see the note in Step 3.

---

## Step 1 — Update the box and install Docker Engine

Run this section **on the server**, in the SSH session from Step 0.

Use Docker's official repository for Engine and the `docker compose` plugin
(v2 or newer), matching `install.sh`. These commands assume Docker has not
already been installed; for an existing installation, check Docker's
[Debian prerequisites](https://docs.docker.com/engine/install/debian/#uninstall-old-versions)
for conflicting packages before continuing.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl gnupg git rsync jq \
                    openssl openssh-client iproute2 python3

# Docker's GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Docker's apt repo
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
                    docker-buildx-plugin docker-compose-plugin
```

Let your user run Docker without sudo, then **log out and back in** so the
group change takes effect (Docker group access grants root-level privileges):

```bash
sudo usermod -aG docker $USER
exit
```

Reconnect and verify:

```bash
ssh <user>@<host>
docker run --rm hello-world
docker compose version      # expect v2 or newer
```

---

<a id="step-2--copy-the-server-directory-over"></a>

## Step 2 — Run `setup.sh`

**On the server**, in the SSH session from Step 1, run as your normal login:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh | bash
```

This executes downloaded code with your account's permissions. Review the
[download sources and trust note](README.md#setting-it-up); to inspect first:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh -o setup.sh
less setup.sh
bash setup.sh
```

Docker Engine and Compose v2 must already be usable without sudo (Step 1).
The wrapper also checks curl, tar, sha256sum and at least 1 GiB of free space;
allow about 2.7 GB extra for the firmware builder image plus room for trip data.
Keep at least 10 GiB free for its first build, which extracts all chips before
pruning.
The builder image is linux/amd64; other server architectures require Docker
amd64 emulation (see the [builder README](server/firmware-builder/README.md)).

Setup verifies the bundle against that release's `SHA256SUMS`, shows the
old/new versions and asks before copying. Server files go directly into
`~/mileage-tracker/`; firmware sources and the version-stamping tool go in
`firmware-src/`. Use `bash setup.sh --dir "$HOME/mileage-tracker"` to choose
a different destination. The flash helpers accept only simple paths, so setup
refuses a destination that contains anything but letters, digits, `_`, `.`, `-`
and `/` (a space, for example) and installs nothing. They also need an SSH
hostname or IPv4 address.
It then runs `install.sh` to configure, rebuild,
start and verify the services. Choose the **LAN IP for `BIND_ADDR`**: UFW
(Uncomplicated Firewall) does not filter Docker-published ports; see
[Step 7](#step-7--firewall-and-network).
Backups and optional notification outputs are explained in Steps 10–11.

Accept the firmware-build offer to continue to the board. The builder asks
before its first toolchain download of about 1.8 GB (about 2.7 GB installed
image, reused for later builds), prompts
for WiFi settings, device ID and server address/port, and reads the upload
token from `.env`. For a new device it reads the server's diagnostics
revision and asks you to confirm fresh SD state and an unused board
enrollment ID. Only when those checks permit it does it build enrollment
and cleanup images; database errors stop the build. It does not automate
recovery of a used board/card. See [Step 8](#step-8--point-the-firmware-at-it).

`firmware-out/current/` is the image to flash; `firmware-out/cleanup/` is
created alongside a first-enrollment image. Directories are owner-only
0700 and files 0600 because the binaries contain the **WiFi password and
upload token**. Do not share them or store unrelated files in `firmware-out/`.
If you skip the build, run it later on the server:

```bash
bash ~/mileage-tracker/firmware-builder/build.sh --install-dir ~/mileage-tracker
```

After setup succeeds, Steps 3–6 and 9 have been performed by the installer;
review the network binding in Step 7, flash/check the board in Step 8, and
verify your backup and notification choices in Steps 10–11.

### Manual alternative: copy `server/`

This is the source-build route. Copying only `server/` does not provide the
`firmware-src/` bundle needed by the builder; build the firmware locally
using the [advanced firmware guide](firmware/README.md#advanced-build-from-source),
or use setup above to install both pieces.

Run the copy **on your computer**, where you cloned or unzipped the repo.
If you are still in the SSH session from Step 1, type `exit` first to
return to your computer's terminal.

**From a Git clone, use a commit archive.** Run this from the repository
root in **Git Bash on Windows**, or a macOS/Linux terminal:

```bash
cd "<path to your copy of the repo>"
git -c core.autocrlf=false archive HEAD server | ssh <user>@<host> 'mkdir -p ~/mileage-tracker && tar -x --strip-components=1 -C ~/mileage-tracker'
```

This copies the committed `server/` files, including `.env.example` and
`.dockerignore`, directly into `~/mileage-tracker` (so `docker-compose.yml`
is there, with no extra `server/` directory). It works for both a first copy
and updates. Only tracked files from `HEAD` are sent: commit your intended
changes first, or replace `HEAD` with the commit or tag you want to deploy.
Your server's `.env`, `viewer.env`, `monitor.env` and `data/` are not in Git
and are left in place. The copy overwrites supplied source files but does
not remove old files that are absent from the archive.

`-c core.autocrlf=false` keeps Linux line endings. Git for Windows turns
`core.autocrlf` on by default, and `git archive` applies it, so without
the flag every file arrives with Windows (CRLF) line endings. It's harmless
on macOS and Linux.

The archive also writes its full commit hash into `COMMIT`, allowing a build
on the server to display the exact source commit without a Git clone there.
Use **`git archive HEAD server`**, not **`git archive HEAD:server`**: the latter
selects a tree rather than a commit, so Git leaves the `$Format:%H$`
placeholder unexpanded and the server can only report the release number.
[Git's archive attributes](https://git-scm.com/docs/gitattributes#_export_subst)
require a commit or tag for this substitution.

Do not run this binary archive pipeline in **Windows PowerShell 5.1**: it can
corrupt the tar stream. Use Git Bash as above. PowerShell only preserves
native binary pipelines starting with 7.4; see
[Microsoft's pipeline documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pipelines#using-native-commands-in-the-pipeline).

**Without Git**, copy the `server/` folder from your unzipped GitHub source
download instead:

```bash
cd "<path to your copy of the repo>"
scp -r server <user>@<host>:~/mileage-tracker
```

`scp` comes with Windows 10 and later, macOS and Linux, and also works in
PowerShell 5.1. Use this form for the first copy only: if `~/mileage-tracker`
already exists, `scp -r` puts the new copy inside it as `server/`. For an
update to an existing install, copy the folder's contents, including dotfiles:

```bash
scp -r server/. <user>@<host>:~/mileage-tracker/
```

Use a fresh source download, without local credentials or data. GitHub's
source zip/tarball has an expanded `COMMIT`, so copying that folder preserves
the commit stamp. Copying a Git clone's working tree with `scp` leaves the
literal placeholder and gives **VERSION only, no commit**. A hand copy with
no `COMMIT` still works, with the same release-only fallback.

Reconnect from your computer:

```bash
ssh <user>@<host>
```

**On the server**, sanity-check the tree. Stay in this SSH session for
Steps 3–7:

```bash
cd ~/mileage-tracker
ls -a
ls -R | head -30
```

You should see `docker-compose.yml`, `.env.example`, `viewer.env.example`,
`sample.gpx`, `.dockerignore`, `common/diagnostics/` (including `catalog.json`),
`backup/`, `mileage-ingest/` (including `diag_store.py`), and `mileage-viewer/`
(with `static/` holding
`leaflet.js`, `leaflet.css`, `gpx.js` — those are vendored, so the viewer
works with no internet except OSM map tiles).

> **Watch for:** `scp -r` on Windows can silently skip dotfiles in some
> OpenSSH builds. Confirm `.env.example` and `.dockerignore` made it:
> `ls -a ~/mileage-tracker`. Both Docker builds use the whole server directory
> as context; `.dockerignore` allows only runtime files and excludes secrets/data.
> If missing, copy it explicitly:
> `scp server/.env.example <user>@<host>:~/mileage-tracker/`
> `scp server/.dockerignore <user>@<host>:~/mileage-tracker/`
> Run that copy from your computer in a separate local terminal, not from
> the server's SSH session.

---

## Updating

Before updating, take a backup and verify its destination using
[Step 10](#step-10--backups). On the server rerun the Step 2 one-liner to get
the latest release, selecting the same install directory (`--dir` if
nondefault). Setup shows the old/new versions, copies the supplied source,
and runs `install.sh` to rebuild, start and verify the server. The wrapper
excludes `.env`, `viewer.env`, `monitor.env`, `backup/backup.conf`, `data/`
and `firmware-out/` from the source copy. It does **not remove obsolete
source files**; review local source changes/obsolete files before updating.
It also replaces every other supplied file with the release copy, so an edit
you made to `ntfy/server.yml` (the iOS setting in [Step 11a](#11a--start-and-secure-ntfy))
is lost: redo it after the update and restart ntfy.
The installer may still change configuration when you answer its prompts.

When the update finishes, run `bash ~/mileage-tracker/install.sh --check`.
From server v0.07 the nightly backup refuses to copy to a destination that has
no `.mileage-backup-target` file, and nothing else flags a backup that then
fails every night. If the check reports that the backup destination has no
marker file, or that the installed backup systemd files differ from the
update's, follow [Backups set up before the destination marker
existed](#backups-set-up-before-the-destination-marker-existed) in Step 10.

Accept the firmware-build prompt and enter/confirm the WiFi/device settings
again; settings are not saved for reuse by the builder. A successful build
replaces its output, and a normal build removes stale cleanup images.
Finish a pending enrollment cleanup before rebuilding. Then run the printed
flash command on your PC, without `-FirstInstall` / `--first-install`, and
answer **no** to first install. App-only updates preserve settings and trips.
Always update the server before installing newer firmware.

For a specific release, download that release's `setup.sh` rather than
`latest`, or run a saved script with `--tag '<tag>'`. A saved script defaults
to its stamped release, not whatever is latest later. For offline source
copying, `--bundle <file>` requires an adjacent `SHA256SUMS`; the installer
and builder still need dependencies. A checkout's unstamped `install/setup.sh`
requires `--tag` or `--bundle`. These options are described by `--help`.

The manual source route is to repeat the commit archive copy in Step 2,
then run `bash ~/mileage-tracker/install.sh` on the server to rebuild/check.
For release installs, keep the published version stamps; the version bump
and deployment-tag instructions below apply to maintainers deploying source
changes.

---

## Step 3 — Create the data directory with the right ownership

Both containers run as a non-root user (`app`, UID 1000 inside the image).
The `./data` bind mount takes its ownership from the **host** directory — if
Docker auto-creates it as root, ingest can't write and every upload 500s.
Create it yourself first:

```bash
cd ~/mileage-tracker
mkdir -p data
chmod 700 data       # trip file names are drive start times; keep them private
ls -ld data          # should be owned by your user, drwx------
```

If your `id -u` from Step 0 was **not** 1000:

```bash
sudo chown -R 1000:1000 ~/mileage-tracker/data
sudo chmod 700 ~/mileage-tracker/data
```

This lets the containers write trips, but your non-1000 login still cannot
read their private files for the backup timer. The supplied automatic
backup setup requires a UID 1000 login; do not assume this ownership fix
also enables backups.

The viewer's annotations DB lives in a named Docker volume (`viewer-db`), not
here — nothing to prepare for it.

---

## Step 4a — Create `.env` and generate the ingest token

```bash
cd ~/mileage-tracker
cp .env.example .env
openssl rand -hex 32
```

Copy that 64-char hex string, then edit `.env`:

```bash
nano .env
```

Set the token, the timezone you drive in, and the server's LAN bind address:

```
INGEST_AUTH_TOKEN=<paste the hex string>
DISPLAY_TZ=America/Chicago
BIND_ADDR=<host>
```

Replace `<host>` with the server's LAN **IP address**, not a hostname. Find
it with `ip -4 addr show` and give it a static IP or DHCP reservation before
starting the services. With `BIND_ADDR` blank, Docker publishes their ports
on every interface, and UFW does not restrict those ports. Setting the LAN
address now keeps the first tests and admin sign-in check on that interface too;
Step 7 explains how to verify the mapping.

Lock it down and confirm it's not world-readable:

```bash
chmod 600 .env
```

**Save this token somewhere you can get at it** — the same value goes into
`firmware/04_trip_logger/secrets.h` in Step 8. It is an upload credential,
not the password used for SSH or for the viewer web page.

**`DISPLAY_TZ` has no default.** It's the timezone every trip time is
shown and exported in, and ingest uses it to pick the month folder a trip
is filed in. Use a timezone name, `America/Chicago` or
`Europe/London`, say; `timedatectl list-timezones` lists them all. Never
use a fixed offset like `-06:00`; it's an hour wrong for half the year
wherever clocks change for daylight saving. Leave it blank and both
services stop at startup with a log line saying so. `install.sh` asks for
it, offering the server's own zone unless that's UTC. Both services get
it from `.env` (and their log timestamps follow it too), so they can't
disagree. If you change it later, run `docker compose up -d`: trips
already filed stay in their folders, and every time shown switches to the
new zone.

---

## Step 4b — Create `viewer.env` and set the viewer login

The admin login lives in `viewer.env`. Everyday logins for whoever annotates
trips live in the viewer database and can be added on `/admin`, by `install.sh`,
or with `manage_users.py`. Keep `viewer.env` **separate** from `.env`: the
viewer has no business holding the ingest token, and single-quoting the hash
preserves its `$` separators.
Compose interpolates unquoted and double-quoted values, including values
in an `env_file`; follow the quoting in the example below.

```bash
cd ~/mileage-tracker
cp viewer.env.example viewer.env
chmod 600 viewer.env
```

Generate the admin password hash. You'll be prompted; the password isn't
echoed and the plaintext is never written to disk or into the container
environment:

```bash
docker run --rm -it python:3.12-slim sh -c \
  "pip install --quiet 'werkzeug==3.0.*' && python -c \"
from getpass import getpass
from werkzeug.security import generate_password_hash as g
print(g(getpass('password: ')))\""
```

Output looks like `scrypt:32768:8:1$RxK2mQ...$8f3c1e...`. Then generate a
session key:

```bash
openssl rand -hex 32
```

Edit `viewer.env` with both values:

```bash
nano viewer.env
```

```
VIEWER_USERNAME=<admin>
VIEWER_PASSWORD_HASH='scrypt:32768:8:1$RxK2mQ...$8f3c1e...'
VIEWER_SECRET_KEY=<the openssl rand output>
```

> **Single-quote the hash.** It contains `$` characters that shell or Compose
> expansion will otherwise eat, leaving you with a hash that can never match.
> The service detects this and refuses to start rather than failing silently,
> but quoting avoids the round trip.

`VIEWER_SECRET_KEY` signs the session cookie and must stay stable — changing
it logs you out everywhere, and it has to be identical across both gunicorn
workers, which is why it's configured rather than generated at startup.

These values configure the admin, not an everyday account. Step 5 includes
the optional everyday login after the viewer image is built.

---

## Step 5 — Build and start

### Server version for each deployment

The server release is `server/VERSION` (`v0.01` initially), independent of the
ESP32 firmware version. Release users keep the published version. For
maintainer source deployments, bump that file in the PR that ships the change;
do not open a separate version-only PR. Use `v<major>.<two-digit minor>`
(major 0-99). Build from the clean merged commit; after verifying deployment,
tag that exact merged commit `srv-v0.NN`, for example `srv-v0.02` for release
`v0.02` (`srv-v0.01` for the initial release). These tags identify server
releases separately from firmware.
Do not tag dirty builds as releases: a tag cannot preserve uncommitted code.

`python3 stamp_version.py`, run in the server folder, prints the release plus
ten Git commit characters, such as `v0.01+abcdef1234`. Tracked uncommitted
changes anywhere in the repository add `-dirty`; ignored/untracked credentials
and data do not. The manual commands below recompute it for every build.
`install.sh` does this automatically, including optional monitor builds.
Compose passes it as a build argument, and all three Python images store it
as `SERVER_VERSION`. A restart keeps the old image's stamp; rebuild to change
code/version. The monitor includes its version in its startup log.

Install Python 3 on a manual build host (`sudo apt install python3`);
`install.sh` installs it automatically. A build inside a Git clone uses Git
for its commit and dirty state. Without usable Git metadata, the helper reads
`COMMIT`: Step 2's commit archive and GitHub source downloads contain a full
40- or 64-hex hash, giving the same release-plus-ten-character-commit stamp.
Archives start clean, so this fallback adds no `-dirty`; later hand edits to
extracted files cannot be detected. Recopy a clean archive before rebuilding
to keep the stamp accurate. If `COMMIT` is missing, invalid or still an
unexpanded placeholder, the helper prints a reason on stderr and uses VERSION
alone. That identifies the release but not its commit or dirty state. A missing or
invalid VERSION, or a build without a stamp argument, reports `unversioned`.
Services replace invalid stamps with `unversioned` before displaying them;
only 1-64 ASCII letters/digits/`.`, `_`, `+`, `-` are accepted, starting with a
letter or digit.

Look in the header of each viewer page (footer on sign-in and error pages), or
on **Device health** next to the firmware version. This is the viewer's image
version.
Both ingest and viewer `/health` also return a `version` field alongside the
existing `status` and `service` fields. These endpoints require **no login**:
anyone who can reach their LAN ports can read the build version, so retain
the LAN-only binding set in Step 4a and checked in Step 7.
`bash install.sh --check` prints both running versions and warns if they
differ or have no valid stamp. Those warnings do not fail the check; a
mismatch means services may contain different code.
Rebuild both using the same stamp, then check again.

```bash
cd ~/mileage-tracker
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build
```

The `SERVER_VERSION` assignment computes the stamp before Compose builds.
Use `BUILDX_NO_DEFAULT_ATTESTATIONS=1` for every manual build or rebuild
(including `docker compose build`). It disables default provenance attestations,
which otherwise change image IDs on Docker 29's containerd store even when
every layer is cached, causing needless container recreation. `install.sh`
sets it automatically, including for monitor builds. This also works with
Compose 2.38, which does not support the `build.provenance` setting.

First build pulls `python:3.12-slim` and compiles wheels; a minute or two is
normal. Then:

```bash
docker compose ps
```

Wait for both containers to show `healthy` (the healthchecks have a 10s start
period and 30s interval, so give it ~45 seconds). If one says `unhealthy` or
`restarting`, jump to Troubleshooting.

To add an optional everyday login, run this from the same directory. The shell
reads the password privately and passes it on standard input to `manage_users.py`:

```bash
read -rs -p 'Everyday password (8–1024 characters): ' new_password; printf '\n'
printf '%s\n' "$new_password" | docker compose run --rm --no-deps -T mileage-viewer python manage_users.py add '<everyday-user>'
unset new_password
```

---

## Step 6 — Verify end to end

This manual check is for a fresh install, before any real trips exist. The
sample is a dated 2026 trip, so until you delete it, it counts in the 2026
totals. If 2026 is already closed in the viewer, the sample is flagged as
received after the close and cannot be annotated (the viewer answers 409), so
the annotate part of this step fails. To recheck a server that already holds
real trips, run `bash ~/mileage-tracker/install.sh --check` instead: it
uploads an untimed copy of the sample that cannot land in any tax year.

Run these commands from `~/mileage-tracker` on the server. Set `H` once to
the server's LAN IP address (the `BIND_ADDR` value chosen in Step 4a). Using
that address also works for an existing install with `BIND_ADDR` blank,
because it listens on all interfaces. `localhost` does not reach a port
bound only to the LAN IP.

```bash
cd ~/mileage-tracker
H='<host>'
```

**Health checks:**

```bash
curl "http://$H:8090/health"
curl "http://$H:8091/health"
```

Expect `{"service":"mileage-ingest","status":"ok","version":"v0.01+abcdef1234"}` and the viewer equivalent.

**Reject an unauthenticated upload** (this must fail — it's the test that
matters):

```bash
curl -i -X POST "http://$H:8090/upload" --data-binary @sample.gpx
```

Expect `401`.

**Real authenticated upload:**

```bash
cd ~/mileage-tracker
TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -X POST "http://$H:8090/upload" \
  -H @- \
  -H "Content-Type: application/gpx+xml" \
  --data-binary @sample.gpx
```

The token goes to curl on its standard input (`-H @-`), not on the command
line, because any other account on the server can read a running command's
arguments (`ps`). The same rule applies to every command in this guide that
sends a token. Don't paste a token into a command's arguments, and don't leave
it in your shell history.

Ingest names a trip with timestamps after those timestamps (here
`2026-04-14T14-30-00Z`, from the first point's time, in UTC). It ignores any
`X-Trip-Id` header on such a trip, so the test needs none. (The header only
names a GPX with no timestamps at all.)

Expect a JSON success body with `distance_miles` ≈ **0.30** over 45 seconds at
~24 mph (that's what `sample.gpx` encodes — if the number is wildly different,
the parse is wrong, not the network).

Leave out `X-Device-Id` for this synthetic upload. Adding it registers a
device for diagnostics monitoring; deleting the test trip does not remove
that device, which would later be reported as silent.

**Confirm both files landed:**

```bash
sudo ls -l data/2026-04/
sudo cat data/2026-04/2026-04-14T14-30-00Z.json | jq .
```

You want a `.gpx` (byte-for-byte raw) *and* a derived `.json` summary.
The files are private to UID 1000, so these commands use `sudo` to work
even when your server login has another numeric user ID.

**Confirm the viewer requires a login** (do this before opening a browser —
it's the check that actually matters):

```bash
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" "http://$H:8091/"
```

Expect `302 .../login`. If you get `200`, auth is not engaged — stop and
check `docker compose logs mileage-viewer`.

**Verify the hash separators survived into the container without printing it:**

```bash
docker compose exec mileage-viewer python -c 'import os; print("Hash separators present" if os.environ["VIEWER_PASSWORD_HASH"].count("$") == 2 else "Hash separators missing")'
```

Expect `Hash separators present`. If they're missing, re-quote the hash
in `viewer.env` and `docker compose up -d mileage-viewer`.

**Viewer, from a browser on your computer:**

```
http://<host>:8091/
```

You should land on a login page. Sign in with the admin username and password
from Step 4b. The trip should then be listed. Click **Map / Edit** → the track
draws on an OSM map. Tag it business/personal and add a note → reload → the
annotation persists. (Saving an annotation adds a permanent line to the
admin change history, which stays after the trip files are deleted. Skip the
annotation if you would rather not leave that line.) Then hit **Export CSV** and **Export XLSX** and confirm
both download with the annotation joined in. Finally click **Log out** and
confirm you're bounced back to the login page.

If you added an everyday login, sign out and sign in with it too. It should
see the same trip and normal trip actions; opening `/admin` as that login
should return `403`. The installer checks this for an everyday login it
created during that run.

If a long stop split a drive, open **Map / Edit** on the earlier trip, save
any edits, then use **Merge with next trip**. Only the next trip from the
same device is eligible, and it must start within 15 minutes of the previous
recording's final timestamp (`recorded_until`). Older summaries fall back to
`end_time` (last movement), never upload time. Both GPX files and reliable
start/device information are required. An unreadable summary blocks merging
only if its device is unknown or it could be an intervening trip from this
device; repair that summary first. The trip page explains this block.
You can repeat the action to build a chain. The merged trip replaces its parts
in the list, map, CSV/XLSX exports and totals. Old part links lead to it.
Export records remain traceable: `trip_id` is the first source trip ID and
`gpx_file` lists all original relative GPX filenames in order, separated by
`; `. Source filenames also appear on the list and trip page. Map
breaks show the unrecorded gaps; shared ingest rules recompute mileage using
straight-line gap estimates and the existing movement/fix filters. Duration
includes the intervening stop. Display and year selection still use `DISPLAY_TZ`.

One saved annotation is kept. If both trips have annotations, the viewer blocks
merging until you clear and save the purpose, note and destination of one trip;
copy any text you want to retain first. **Undo merge (split back)**, after its
confirmation checkbox, restores all original recordings and their annotations
as saved at merge time. It removes edits to the merged annotation. A chain
splits all the way back to its original recordings.

The viewer keeps `./data` read-only. Merges, original-part membership, link
aliases and cached summaries live in three additive tables in the existing
`viewer-db` volume's `annotations.db`; startup migration serializes concurrent
workers and retains existing rows. The normal database backup/restore includes
these tables. Back up both the database and trip files, since trip files alone
cannot restore merges. Cached summaries keep list views fast and refresh when
source GPX/JSON file stamps change. A rule-version change alone does not
recalculate saved mileage. Missing or unreadable sources flag a merge as
unreadable and exclude it from totals; the
list warning still offers Undo. All merge/Undo changes use authenticated,
CSRF-protected POSTs. See [Merge recordings from one drive](server/README.md#merge-recordings-from-one-drive)
for the annotation and timing details.

> Deliberately fail a login password five times and you'll be locked out of that
> browser's IP for 15 minutes — including with the *correct* password. Failures
> count until there has been a 15-minute pause without one; the next failure
> then starts a fresh count. If you trip this during setup, clear it with:
> `docker compose exec mileage-viewer python -c "import sqlite3;c=sqlite3.connect('/db/annotations.db');c.execute('DELETE FROM login_attempts');c.commit()"`

**Clean up the test trip** so it doesn't pollute your real mileage log:

```bash
sudo rm ~/mileage-tracker/data/2026-04/2026-04-14T14-30-00Z.{gpx,json}
sudo rmdir ~/mileage-tracker/data/2026-04   # only goes if nothing else is in it
```

If you annotated the sample, an annotation row stays in the viewer database
and a line stays in the admin change history. Neither appears in exports,
because the trip itself is gone, but the history line is permanent.

---

## Step 7 — Firewall and network

Both services are **LAN-only by design**. Ingest has a bearer token; the
viewer has admin and everyday session logins, with roles checked by the
server. Neither is safe to port-forward: all viewer session cookies (admin and
everyday) and the ingest token travel over plain HTTP, so anyone who can observe
the traffic can read or replay them. The logins are defence in depth against
other devices on your network, not a substitute for TLS.

**Give the server a static IP or DHCP reservation first.** The T-Beam
has the upload address compiled in, so a lease change silently breaks
uploads until you reflash it with the new address (Step 8). The bind
address below depends on it too.

**Restrict the ports with the bind address, not UFW.** Docker publishes
ports through its own iptables rules, which forward the traffic before UFW
ever sees it. A `ufw allow from 192.168.1.0/24 ...` rule for ports 8090, 8091,
8092 or 8093 has no effect, and neither does a `ufw deny`: the ports stay open on every
interface the box has (a VPN, a second NIC). UFW still governs
services running directly on the host, such as SSH; it just has no say
over Docker-published ports.

Step 4a set `BIND_ADDR` before the first start. Confirm it is still the
server's LAN IP in `.env`:

```bash
cd ~/mileage-tracker
ip -4 addr show          # find the LAN address
nano .env
```

```
BIND_ADDR=<host>
```

Here `<host>` means that LAN IP. If you changed it, recreate the containers
so the new port mapping takes effect:

```bash
docker compose up -d
docker compose ps
```

The `PORTS` column should read `<host>:8090->8090/tcp` and
`<host>:8091->8091/tcp`, with your LAN IP in place of `<host>`. When the monitor
profile is running, port 8093 should also be bound to the LAN IP. If a
published port says `0.0.0.0`, Compose didn't pick up `BIND_ADDR`; check
the spelling and that `.env` is next to
`docker-compose.yml`.

Confirm the mappings and health from the server:

```bash
docker compose port mileage-ingest 8090   # expect <host>:8090
docker compose port mileage-viewer 8091   # expect <host>:8091
docker compose port mileage-monitor 8093 # when the monitor profile is started
curl 'http://<host>:8090/health'    # expect {"service":"mileage-ingest","status":"ok","version":"v0.01+abcdef1234"}
```

Both mappings must show only the LAN address; a wildcard (`0.0.0.0` or
`[::]`) or an extra mapping means the ports aren't restricted to it.
Another application may listen on `localhost:8090` while the tracker is
bound only to the LAN address, so a response there doesn't tell you where
the tracker is published. Use the LAN IP for any curl tests from here on.

### Behind a reverse proxy (Caddy or Nginx Proxy Manager)

A reverse proxy accepts a browser's HTTPS connection and forwards the request
to a service such as the viewer over your private network. It lets you open
`https://viewer.home.<domain>` instead of remembering an IP address and port.
Use a domain you control and one name per service, for example
`viewer.home.<domain>` and `ntfy.home.<domain>`; the viewer needs the root of
its own name and cannot run under a sub-path such as `/viewer`.

This is an optional private-network setup. Keep the existing `BIND_ADDR`
for ingest: the tracker still posts plain HTTP to its compiled-in
`<server-ip>:8090`. All three new `.env` settings are empty by default, so
an existing install keeps its current HTTP behavior:

- `VIEWER_BIND_ADDR` changes only the viewer's published port 8091. Empty
  uses `BIND_ADDR`, or all interfaces if both are empty. Set `127.0.0.1`
  when the proxy is installed on the host or runs with host networking:
  it can reach `127.0.0.1:8091`, and plain HTTP is closed on the LAN.
  An IPv6 loopback bind (`::1`) is also supported if your Docker host has IPv6.
- `VIEWER_TRUSTED_PROXIES` is a comma-separated list of proxy peer IPs or
  CIDR network ranges (an address followed by a prefix length), IPv4 or
  IPv6, with optional spaces. Empty trusts no forwarded
  addresses. Trust only addresses under your control that really are proxies;
  trusting all clients or a whole LAN lets clients forge their lockout IP.
  Invalid entries stop the viewer at startup.
- `VIEWER_COOKIE_SECURE=true` marks the viewer's session cookie Secure,
  so browsers send it only over HTTPS. Empty or `false` preserves direct
  HTTP logins. Only `true`/`false` (case insensitive) is accepted; any other
  value stops the viewer. Once enabled, log in at the HTTPS name. A plain
  HTTP login on LAN port 8091 cannot keep a browser session.

For a proxy on the host, use `VIEWER_BIND_ADDR=127.0.0.1`,
`VIEWER_COOKIE_SECURE=true`, and trust only the observed proxy peer described
below. **A bridge-networked proxy container cannot reach the host's
127.0.0.1**: that address means the proxy container itself. In that case set
`VIEWER_BIND_ADDR=<server-ip>` to the host's LAN IP (or leave it empty if
`BIND_ADDR` already has that IP), forward to that IP on port 8091, and use
the trusted-proxy setting plus Secure cookies. Plain HTTP stays reachable
on the LAN in that combination; Secure cookies protect the browser session,
and trusted-proxy handling prevents forged forwarded addresses from direct
clients. To close that HTTP port on the LAN, move the proxy to host networking.
The proxy-to-viewer hop still uses HTTP, so keep that network private too.

Edit `.env` in the installed server folder (`server/.env` in a source
checkout), setting `VIEWER_BIND_ADDR` for your chosen networking mode and
`VIEWER_COOKIE_SECURE=true`. Leave `VIEWER_TRUSTED_PROXIES` empty until you
verify the peer below. Apply the bind and cookie settings first:

```bash
docker compose up -d mileage-viewer
```

On **Unraid**, move its web UI off ports 80/443 in
[Settings > Management Access](https://docs.unraid.net/unraid-os/system-administration/secure-your-server/securing-your-connection/)
before starting a proxy on those host ports. Then configure either Caddy or
Nginx Proxy Manager (NPM) below, including its certificate and Domain Name
System (DNS) records, before opening the viewer through it.

**Keep every service off the internet.** Add no router port forwards for
80, 443, 8090–8093 or NPM's 81; check that none already exist. DNS challenge
validates a public DNS TXT record and needs no inbound ports. Use a wildcard
certificate containing `*.home.<domain>` rather than individual service names
to keep those names out of public certificate transparency logs; the wildcard
domain itself remains public. DNS answers for service names must point only
at private addresses: use local DNS for the proxy's LAN IP, and a public
record pointing to its Tailscale 100.x address or [Tailscale split
DNS](https://tailscale.com/docs/reference/dns-in-tailscale#restricted-nameservers) for remote
clients. Tailscale must be installed on the proxy host and phone, with access
allowed by your tailnet policy; make sure the proxy listens on its Tailscale
interface too. A LAN IP instead needs an explicitly configured Tailscale
subnet route. Do not enable Tailscale Funnel.

**Caddy.** This minimal site block forwards to a viewer bound to loopback:

```caddyfile
viewer.home.<domain> {
    reverse_proxy 127.0.0.1:8091
}
```

Replace `<domain>` with your domain. This shows routing only: before using
it on a private network, configure a DNS-challenge certificate or load an
existing certificate. Caddy's normal HTTP/TLS challenges need public inbound
reachability; do not open router ports for them. DNS challenge needs a Caddy
build containing your DNS provider's module and that provider's credentials;
follow [Caddy's DNS challenge documentation](https://caddyserver.com/docs/automatic-https#dns-challenge).
For a wildcard certificate, configure issuance for `*.home.<domain>` and route
the individual names within that wildcard site (or load an existing wildcard
certificate into the site above). Requesting a separate certificate for the
viewer name publishes that name in certificate logs.

**Nginx Proxy Manager (NPM).** Add a Proxy Host. In **Details**, set
**Domain Names** to `viewer.home.<domain>`, **Scheme** to `http`,
**Forward Hostname / IP** to the host's LAN IP for a bridge-networked NPM
(or `127.0.0.1` for host networking), and **Forward Port** to `8091`.
Websockets Support is not needed for the viewer. In **SSL**, request/select
a wildcard certificate for `*.home.<domain>` using **Use a DNS Challenge**
and your DNS provider, then enable **Force SSL**. You can create the wildcard
under **SSL Certificates** first and select it for each Proxy Host.
NPM's admin UI is on port **81**: keep it off the internet and set a strong
login. See [NPM's setup guide](https://nginxproxymanager.com/guide/) for its
container and certificate storage.

In this viewer Proxy Host's **Advanced** field, add this line and save:

```nginx
set_real_ip_from unix:;
```

NPM 2.16.0 trusts incoming `X-Real-IP` from private IPv4 ranges by default.
A LAN client can choose that header, making NPM forward a made-up client
address; rotating it evades the viewer's five-failures/15-minute lockout
and puts false addresses in the failed-login log. The line above replaces
NPM's inherited trust list for this Proxy Host with Unix sockets only, so
TCP clients cannot rewrite their address. Keep NPM directly reachable by
the clients: if another proxy sits in front, this setup counts that proxy
as the client and needs a separate, carefully limited trust configuration.
Do not add server-level `proxy_set_header` overrides here: NPM's generated
location supplies its own headers, which replace inherited ones. See the
[NPM host template](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/templates/proxy_host.conf)
and [nginx real-IP directives](https://nginx.org/en/docs/http/ngx_http_realip_module.html).

Find the address to trust by opening the viewer through the proxy once,
then, in the server folder, running:

```bash
docker compose logs mileage-viewer
```

Look for `Ignoring X-Forwarded-For from untrusted peer ...; add it to
VIEWER_TRUSTED_PROXIES if it is your proxy`. Verify that peer belongs to your
proxy path before adding it. Docker often presents the bridge gateway's IP
instead of the proxy's own IP. The warning is logged once per peer for the
first 256 peers in each worker process; recreating the viewer resets it.
Trusting a bridge gateway also trusts other traffic that Docker presents
under that address, so keep other containers on that path under your control.
For example, set `VIEWER_TRUSTED_PROXIES=<proxy-peer-ip>` (replace the
placeholder; an individual IP is preferable to a broad CIDR). Then recreate
the viewer, since a restart alone retains the old environment:

```bash
docker compose up -d mileage-viewer
bash install.sh --check
```

The viewer ignores X-Forwarded-For from untrusted peers. From trusted peers,
it combines all header instances, walks right to left, skips invalid entries
and trusted hops, and uses the first valid untrusted address for login
lockout and failed-login logging. A missing, wholly invalid or entirely
trusted chain falls back to the direct peer IP. Caddy supplies X-Forwarded-For
from the connection by default, [without trusting client-supplied addresses](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers);
NPM needs the additional setting above before its headers can be trusted.
Use a strong viewer password with either proxy. `--check`
reports the selected viewer address and checks health there; with Secure
cookies enabled it skips authenticated HTTP tests and asks you to test
logging in through the HTTPS name yourself.

**Check the client address with either proxy.** From a separate LAN client,
open the HTTPS login page, then use the browser's developer tools **Network**
tab to copy one deliberately failed login request as curl. Use a made-up
username and password, never your real credentials. Run that copied command
promptly with `-H 'X-Real-IP: 198.51.100.77'` added; keep its session cookie
and `csrf_token` so the viewer accepts it as a login attempt. In
`docker compose logs --since 2m mileage-viewer`, find `Failed login ... from`:
it must show the client's connection address (or its network's NAT address),
not `198.51.100.77`. A 400 means the session/CSRF check failed; copy a fresh
request and retry. A 429 means that address is locked; wait 15 minutes.
If the forged address appears, fix the proxy configuration before relying
on per-client lockout. Repeat this check after proxy upgrades.

**Leave ingest unproxied for now.** If you ever proxy it, allow 16 MiB request
bodies and 900 s timeouts. Plain nginx defaults to `client_max_body_size 1m`
(1 MiB); NPM 2.16.0 instead sets `2000m` (2000 MiB) in its
[nginx configuration](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/docker/rootfs/etc/nginx/nginx.conf).
Check the effective proxy limit against ingest's 16 MiB cap; `16m` raises
plain nginx's default but lowers NPM's. Check the timeout settings too.
Follow the existing [upload/proxy limits and HTTP 413
consequences](server/README.md#security-notes) before changing that path.

**ntfy through a proxy.** Give it its own name, forwarding to the bundled
ntfy's host port 8092 (use the LAN IP from a bridge-networked proxy).
In `server/.env` in a source checkout, or `.env` in the installed server
folder, set:

```dotenv
NTFY_BASE_URL=https://ntfy.home.<domain>
NTFY_BEHIND_PROXY=true
```

These settings survive updates; hand edits to shipped `ntfy/server.yml` are
replaced. Compose passes them to ntfy and overrides the corresponding YAML
settings. `NTFY_BASE_URL` is ntfy's external URL; `NTFY_BEHIND_PROXY=true`
makes its visitor rate limiting use forwarded client addresses instead of
counting everyone as the proxy. Leave it unset or `false` without a proxy.
Restrict access to the backend to your private network/proxy and see [ntfy's proxy
settings](https://docs.ntfy.sh/config/#behind-a-proxy-tls-etc) for trusted-hop
controls; these are independent of `VIEWER_TRUSTED_PROXIES`. Recreate ntfy
with `docker compose --profile ntfy up -d --force-recreate ntfy`. Resubscribe
the phone to the topic using that exact HTTPS server URL and its ntfy login.
Keep the monitor's `NTFY_URL` topic destination reachable from its container;
it can still use the existing internal ntfy URL. For iOS instant push, retain
the [Step 11a requirements](#11a--start-and-secure-ntfy).

Test from a phone on mobile data with WiFi and Tailscale **off**: every
service name must fail to connect. Turn Tailscale **on** and check that the
HTTPS names connect and the viewer login works.

---

## Step 8 — Point the firmware at it

The Windows `flash.ps1` first-install and `-Cleanup` paths, and all of
`flash.sh`, still need testing on a real board. The steps below describe the
scripts' intended behavior; automated checks do not establish those hardware
results.

For the primary route, the builder in Step 2 has already read the upload
token and built the server address and WiFi settings into the firmware.
Exit SSH and use the printed flash command **on the PC connected by USB**;
see [README Step 2](README.md#step-2--flash-the-tracker) and the
[full flash guide](firmware/README.md#flash-from-the-server-build).
First install verifies a factory backup, requires typing `ERASE`, erases
all internal flash and writes four parts. Updates are app-only; do not
choose first install for an existing tracker.

For a fresh device/card with no saved server snapshot revision, the builder
prepares one-use enrollment values and a separate cleanup image. After the
first newly received **current snapshot** in Device health, run the flash
script with `-Cleanup` (Windows) / `--cleanup` (macOS/Linux), before another
server build. This removes enrollment values with an app-only flash. A
listed device or GPX contact alone is insufficient. Reused cards/boards,
lost metadata or snapshot conflicts need
[diagnostics recovery](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery);
do not answer the builder's new-device questions as if they were fresh.

### Advanced: local firmware settings

**On your computer**, create the firmware's settings file from the
example. If you are still in the server's SSH session, type `exit` first.
If `secrets.h` already exists, edit it instead of overwriting it with the example.

```bash
cd "<path to your copy of the repo>/firmware"
cp secrets.h.example 04_trip_logger/secrets.h
```

(In Windows `cmd`, use `copy secrets.h.example 04_trip_logger\secrets.h`;
PowerShell accepts `cp`.) It has to sit in the sketch folder,
`04_trip_logger/`, next to the `.ino`; that's the only place the build
looks for it. Fill in WiFi SSID/password, the server host/IP, port
`8090`, and set `AUTH_TOKEN` to the `INGEST_AUTH_TOKEN` from Step 4a — it
must match byte-for-byte. Put only the IP address or hostname in `SERVER_HOST`, with
no `http://`, port or `/upload`. For a manual source install, `install.sh`
prints the host and port lines ready to paste and points to `.env` for the
token; copy that value privately. The WiFi has to be 2.4 GHz, and hidden
networks won't work.

> **These settings are compiled into the firmware.** If the server's IP or
> the token changes later, or your WiFi name or password does, the board
> stops uploading until you update `secrets.h` and flash it again.

`secrets.h` is gitignored. Keep it that way.

Build and flash `firmware/04_trip_logger` (the trip logger sketch, the one
that records and uploads trips) for the **T-Beam Supreme**,
using **ESP32S3 Dev Module**, ESP32 core **3.3.11**, and the libraries and
board settings in the [source-build walkthrough](firmware/README.md#advanced-build-from-source).
The equivalent arduino-cli fully qualified board name (FQBN) is:

```text
esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
```

Before each trip logger build, run `python tools/stamp_firmware_version.py` from
the repository root so Device health shows `firmware/VERSION` plus the ten-character
Git commit (for example `v0.05+3f9a1c07be`), with `-dirty` for tracked edits.
Runtime debug mode does not change the version. Without the generated header
it shows `unversioned`; an old header keeps its old version. Road-use version bumps and tags are described
in [Firmware versions and releases](firmware/README.md#firmware-versions-and-releases).

Fit a **FAT32 microSD card** with the board switched off. The trip logger
records there when it mounts, otherwise it uses internal LittleFS. Its bundled
`partitions.csv` overrides the Arduino IDE's Partition Scheme menu.
Uploads from LittleFS without the SD card remain untested. A trip ending in a
parking garage also remains unverified and is deferred until after v1.00.
The first time you upload the sketch to a new board it needs **Erase All Flash
Before Sketch Upload → Enabled** once to clear factory data, then
**Disabled** for later uploads;
see [First flash on a new board](firmware/README.md#first-flash-on-a-new-board).
Later uploads with erase disabled keep trips on the card and in internal
flash and preserve NVS (the board's settings memory, including trip
numbering and the diagnostic boot counter). Erase-all wipes NVS and
internal trips, so back up existing data before using it again.

### One-time diagnostics enrollment

For a manual source build on a fresh board/card, before the first boot
with diagnostics enrollment enabled, read the
server's saved snapshot revision for this `DEVICE_ID`. A snapshot describes
the board's current conditions; the server accepts a newer revision so an
old upload cannot replace newer state. **Run this command on the server**,
replacing `<DEVICE_ID>` with the value in your `secrets.h`:

```bash
cd ~/mileage-tracker
docker compose exec -T mileage-viewer python - '<DEVICE_ID>' <<'PY'
import sqlite3
import sys
from pathlib import Path

root = Path('/data')
path = root / 'diagnostics/events.db'
revision = 0
# Directory listings must succeed: denied access is not an absent database.
if ('diagnostics' in {p.name for p in root.iterdir()}
        and 'events.db' in {p.name for p in path.parent.iterdir()}):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT status_sequence FROM devices WHERE device_id=?',
                         (sys.argv[1],)).fetchone()
    if row is not None and row[0] is not None:
        revision = row[0]
if type(revision) is not int or not 0 <= revision < (1 << 63):
    raise ValueError('Invalid saved snapshot revision; investigate before enrollment')
print(revision)
PY
```

This uses the viewer container because it mounts `data/` read-only, so the
command cannot write to ingest's database. It also opens SQLite with
`mode=ro`. It prints `0` if `events.db` does not exist yet or the device
has no row. If it reports an error, fix that error before proceeding;
do not substitute zero for an unreadable database.

**Back on your computer**, set these in `firmware/04_trip_logger/secrets.h`
before flashing, replacing `<printed>` with the number from the command:

```cpp
#define DIAGNOSTICS_ENROLLMENT_ID 1ULL
#define DIAGNOSTICS_STATUS_HIGH_WATER <printed>ULL
```

`1ULL` is only for a board that has never used an enrollment ID. The ID
must be positive and **greater than every ID previously used on this
board**; use a higher value if needed. `ULL` marks an unsigned long long
integer in C++. The firmware consumes the ID in NVS before restoring the
snapshot revision on the usable mounted journals, so even a failed attempt needs a higher ID.
For recovery, also inspect the SD stored revision and the serial
`local minimum high-water` (includes internal flash), then follow
[the recovery procedure](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery);
the server value alone is not a safe recovery bound.

After the **first successful diagnostics upload**, check the viewer's
**Device health** page for the board's contact and a current snapshot.
Then remove both defines and reflash with erase **Disabled**. Existing SD/LittleFS
revision metadata is used on later boots. Re-enrollment after losing
all current copies needs a higher ID (at least 2 after using ID 1) and a
fresh server read. A replacement card adopts a current internal-flash copy.
An enrolled v1.00 card seeds flash at the first allocation after upgrading;
upgrading without the card needs it once or explicit re-enrollment. Later
card-out boots send LittleFS health with E4001 active. Erase-all also clears
the recovery/suspension/stale-store marker (`diag/revstate`) and consumed ID (`diag/enroll`);
leaving the defines in the
firmware then re-enrolls from the compiled-in, possibly stale high-water.
If a mounted card starts failing reads or writes, a current healthy flash copy
can still send snapshots and its events, including E4006 trip-write faults.
Likewise, failed LittleFS does not block a healthy enrolled card. NVS must
record the skipped store as stale before sending; that store resynchronizes
alongside current authority when it recovers. Unreadable event stores do not
block another store's batches.
After erase-all, a genuinely missing witness adopts the epoch from an intact
enrolled card (or agreeing ready mounted journals), so re-enrollment is normally
unnecessary. Suspension and disagreeing epochs still block automatic recovery;
NVS read errors never permit adoption. Erasing loses stale-card protection:
an old card can send history without updating current health, and an exact
high-water collision suspends diagnostics until explicit recovery. Keep erase
disabled for updates, remove the enrollment defines and check Device health
after any erase.
See [snapshot revision enrollment and recovery](DIAGNOSTICS.md#snapshot-revision-enrollment-and-recovery).

Successful diagnostics uploads print nothing on the board's serial output;
check the ingest log or the viewer's **Device health** page instead. The
enrollment command above prints `0` on a brand-new server, because
no snapshot revision has been saved yet. An identified GPX upload can create
`data/diagnostics/events.db` before any diagnostics upload; a device row without
a snapshot revision also prints `0`. That is expected on a fresh board/card.

Then follow the firmware guide's [recording and upload checks](firmware/README.md#check-recording-and-uploads)
and [car installation](firmware/README.md#put-it-in-the-car). If storage
fails on a board that has already logged trips, use the firmware guide's
[storage recovery steps](firmware/README.md#if-the-trip-storage-wont-mount-later)
to preserve the data before considering an erase.

---

## Step 9 — Set it to survive reboots

Return to the server from your computer with `ssh <user>@<host>`.
Run Steps 9 and 10 **on the server**.

`restart: unless-stopped` is already in the compose file, and Docker's systemd
unit is enabled by default on Debian. Verify:

```bash
sudo systemctl is-enabled docker      # expect "enabled"
sudo reboot
```

Reconnect after a minute and confirm both containers came back:

```bash
docker compose -f ~/mileage-tracker/docker-compose.yml ps
```

With `BIND_ADDR` set, Docker can only start the containers once the server
holds that address. If they didn't come back, see "Containers missing after
a reboot" in Troubleshooting.

---

## Step 10 — Backups

Back up these files and databases:

- **Trip files** in `~/mileage-tracker/data`, the raw GPX and its summary.
  The GPX cannot be regenerated; a summary can be rebuilt from its GPX.
- **The annotations database** (business/personal, purpose, destination,
  merges and cached merged summaries, plus everyday logins and their
  password hashes, trip removals, the change history and the saved totals of
  closed tax years) in the `viewer-db` Docker volume. It's easy to forget,
  and it's the record the IRS cares about. It can't be rebuilt from the trips.
- **The diagnostics database** at `data/diagnostics/events.db`, containing
  device events, contact times and current health. It may not exist before the
  first device check-in.
- **Optional monitor state** at `/state/monitor.db` in the `monitor-state`
  volume: alert episodes, queued deliveries, retry state and export cursors.
  Losing it can replay alerts and exports.

`backup/backup.sh` covers these files and databases. It runs daily from a
systemd timer, keeps dated snapshots in `~/mileage-backups`, and copies them
to another machine.
Tax records need keeping for years, and a backup on the same disk as the
data won't survive the disk, so the script refuses to run without an
off-host destination. Each run:

- snapshots each database with SQLite's backup API and checks its integrity,
  even while services are writing. The throwaway viewer container reads both
  annotations and diagnostics, using its read-only data mount; the monitor's
  helper reads its own volume. A missing events DB is normal. If no monitor
  volume exists, or it has no DB yet, that is recorded and skipped without
  enabling the monitor. Discovery uses the project name from
  `docker compose config --format json` and Docker's Compose project/volume
  labels (`com.docker.compose.project` and `com.docker.compose.volume`), rather
  than guessing names or requiring newer Compose flags. Config/parser errors
  are suppressed to protect service credentials. Any discovery error or
  ambiguous result logs one warning and records `monitor=unknown` in `INFO`;
  trips, annotations and diagnostics still back up. No monitor helper is run
  unless its volume is known to exist. Snapshots are taken in sequence, so
  they do not share one transaction across databases.
- copies `data/` while holding ingest's upload lock, so it never catches
  a trip with its GPX written and its summary not. The live
  `diagnostics/events.db` and its SQLite sidecars are excluded from rsync.
- stores `annotations.db`, optional `events.db` and optional `monitor.db`
  beside `data/`, all covered by `SHA256SUMS`. Human-readable `INFO` records
  annotation/event counts and monitor status. Restore checks report database
  integrity, event counts and monitor state tables.
- writes a `SHA256SUMS` manifest. Unchanged files are hard links to the
  previous snapshot, and a run where nothing changed doesn't add a
  snapshot at all, so daily runs cost almost nothing.
- keeps the newest 14 snapshots plus the first of each month for 7 years.
- rsyncs to the off-host copy, then reads the newest snapshot back from
  there and checks it matches byte for byte. It copies only if the
  destination holds a file named `.mileage-backup-target` (see 10a): a share
  that isn't mounted is an empty folder on this machine's own disk, and
  without the file the run fails instead of filling that disk and calling it
  an off-host copy. The copy never deletes anything on its own, and by default
  (`REMOTE_PRUNE=no`) the script never prunes remote snapshots, so the
  off-host copy keeps every snapshot ever taken and grows for as long as
  backups run: delete old ones there yourself, or set `REMOTE_PRUNE=yes`. With
  `REMOTE_PRUNE=yes` it applies the same keep rule to the snapshot names
  already on the destination and deletes only the names that rule drops. It
  refuses to prune, and warns in the log, while the destination holds
  snapshots newer than this server's newest or ones this server doesn't have
  (a rebuilt server). The server still has write access: protection from
  deletion by a compromised server needs retention or snapshots enforced on
  the backup host, or a restricted backup account. See
  [backup.conf.example](server/backup/backup.conf.example).
- gives up instead of hanging when the destination vanishes mid-copy: rsync
  stops after 10 minutes without data moving, an SSH connection after about
  two minutes of silence, and the systemd unit after 6 hours in all.

The timer runs the script as your login user, which has to be able to read
the trip files. Ingest writes them as UID 1000 with mode 600, so that user
needs UID 1000 (Step 0).

### 10a — Pick the off-host destination

Anything you can rsync to: a network attached storage (NAS) device, another
always-on machine, or a mounted share. Over SSH, the far end needs `rsync`
installed and a login the backup
can use without a password. In these examples, `<backup-user>` and
`<backup-host>` are the login and host for that other machine, and
`/<backup-path>` is the absolute destination directory there. On this server:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mileage_backup -N '' -C mileage-backup
ssh-copy-id -i ~/.ssh/mileage_backup.pub <backup-user>@<backup-host>
ssh -i ~/.ssh/mileage_backup <backup-user>@<backup-host> 'mkdir -p /<backup-path> && rsync --version | head -1'
```

That last command must work without a password prompt, or the timer will
fail during its unattended run. It also creates the directory, which
the script won't do for you.

### 10b — Configure it

```bash
cd ~/mileage-tracker/backup
cp backup.conf.example backup.conf
chmod 600 backup.conf
nano backup.conf
```

Set `REMOTE`, and `RSYNC_SSH` if you made a dedicated key:

```
REMOTE=<backup-user>@<backup-host>:/<backup-path>
RSYNC_SSH="ssh -i /home/<user>/.ssh/mileage_backup"
```

The rest of the file explains the retention settings and an optional
`PING_URL`.

**Mark the destination.** A backup copies there only if the folder holds a
file named `.mileage-backup-target`. `install.sh` writes it when it sets up
backups; if you set them up by hand, write it once now, after checking that
`REMOTE` is the real destination (a mounted share is mounted right now):

```bash
bash ~/mileage-tracker/backup/backup.sh --mark-target
```

The file is the guard against an unmounted share. When the share is missing,
its mount point is an empty folder on this server's disk, with no marker, so
the backup stops with an error (the systemd unit fails, `last-success` isn't
updated and `PING_URL` isn't called) instead of copying onto the disk it's
meant to protect against. A local destination should be a mount that systemd
knows about, for example a line in `/etc/fstab` (add `nofail` if the machine
should boot without it): `install.sh` tells the backup unit to wait for that
mount before each run (`RequiresMountsFor=`).

### 10c — Run it once by hand

```bash
bash ~/mileage-tracker/backup/backup.sh
```

It should finish with `Backup complete: <snapshot> is on <REMOTE>`. Running
it a second time straight away should say `Nothing changed`. If it stops with
`has no .mileage-backup-target file`, the destination isn't marked or isn't
there: see the end of 10b.

### 10d — Test a restore from the off-host copy

A backup you haven't restored from is a hope, not a backup:

```bash
bash ~/mileage-tracker/backup/restore.sh check
```

This pulls the newest snapshot back **from the off-host copy** into a
scratch directory, `~/mileage-backups/.restore-scratch-<random>` (it is on the
backup disk, because `/tmp` is in RAM on Debian 13). The scratch copy holds the
whole snapshot, every trip file included, so you need about a full copy of
your trips in free space there. Set `TMPDIR=<folder>` to put it elsewhere. The
script removes it when it finishes, and a backup clears one left behind
by a killed run after a day. It checks every file against `SHA256SUMS`, opens
the database and checks its integrity, and prints what it holds:

```
snapshot: integrity ok, 1 annotations (1 tagged), last edited 2026-...
Snapshot holds 1 trip GPX files and 1 summaries
Restore test passed
```

The counts should match what the viewer shows. It changes nothing on the
live system. Do this again before each filing.

On a new install the first snapshot is empty (`0 annotations`, `0 trip
GPX files`): the installer deletes its test trip before it gets to
backups. That proves the SSH key, rsync and checksums, but not that real
trips and annotations come back. After the first real drives, annotate
one in the viewer, make a fresh backup, then check it:

```bash
bash ~/mileage-tracker/backup/backup.sh
bash ~/mileage-tracker/install.sh --check
```

Or wait for a successful nightly backup before running `--check` (or
`restore.sh check`). These checks only test the newest existing snapshot;
they do not create one. Confirm the trip and annotation counts match the
viewer and are no longer zero.

### 10e — Schedule it

```bash
sudo cp ~/mileage-tracker/backup/mileage-backup@.service \
        ~/mileage-tracker/backup/mileage-backup@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mileage-backup@$USER.timer
```

The unit runs `/home/<user>/mileage-tracker/backup/backup.sh` as `<user>`.
If the server files live somewhere else, edit `ExecStart` in the
`.service` file first. If you move them later, the timer keeps running the
old path: edit it again, or rerun `install.sh` from the new place, which
fixes it (`install.sh --check` reports the mismatch).

When `REMOTE` (or `BACKUP_DIR`) is a folder on this machine, such as a mounted
share, create a drop-in so systemd mounts it before each run:

```bash
sudo systemctl edit mileage-backup@$USER.service
```

and add (your folder paths):

```
[Unit]
RequiresMountsFor=/mnt/backup-share/mileage-tracker
```

`install.sh` writes the same thing itself, and rewrites the unit and timer
files in `/etc/systemd/system` whenever an update changes them (rerun it after
updating the server files); `install.sh --check` warns when the installed
files differ from the ones in `backup/`. If you copied the files by hand,
copy them again after an update and run `sudo systemctl daemon-reload`.

Run it once through systemd to prove the unattended path works (the SSH key,
the paths, the user):

```bash
sudo systemctl start mileage-backup@$USER.service
systemctl status mileage-backup@$USER.service      # expect "status=0/SUCCESS"
journalctl -u mileage-backup@$USER.service -n 20
systemctl list-timers 'mileage-backup*'            # next run ~03:30 server time
```

`Persistent=true` in the timer means a run missed while the box was off
happens at the next boot. The 03:30 schedule follows the server's own
timezone (`timedatectl` shows it), and each run starts up to 15 minutes late
on purpose (`RandomizedDelaySec`).

### 10f — Know when it stops working

A failed run shows up as a failed unit (`systemctl --failed`) and in the
journal, but nobody looks there. Two options:

- Set `PING_URL` in `backup.conf` to a push monitor (Uptime Kuma, or
  healthchecks.io). The script calls it only after a verified off-host copy,
  so if the pings stop, the backups have stopped.
- Or check by hand now and then: `cat ~/mileage-backups/last-success`.

### Backups set up before the destination marker existed

A server whose backups were set up by an older version has no
`.mileage-backup-target` file at its destination yet, and the nightly backup
refuses to copy until it's there. Until you do the steps below, every night's
run fails (`systemctl --failed` lists `mileage-backup@<user>.service`,
`last-success` stops moving and `PING_URL` stops being called). Nothing is lost;
the local snapshots in `~/mileage-backups` keep being made. After updating the
server files:

1. Rerun `bash ~/mileage-tracker/install.sh`. For a destination over SSH it
   writes the marker file itself. For a destination that is a folder on this
   machine it prints a reminder that the folder must be the mounted share right
   now. If the folder is on the same disk as the trip data, which is what an
   unmounted share's empty mount point looks like, it asks "Use it anyway?".
   Answer **n** unless you really mean to back up to the server's own disk:
   **y** marks the local folder, which is the failure the marker exists to
   prevent. The installer marks the folder only if it is writable and you
   accepted; otherwise it leaves it unmarked and prints the command from step 2.
   (An unattended update has no one to answer: set `INSTALL_SAME_DISK`, see the
   presets near the top.)
2. If the installer didn't mark it, make sure the destination is the right
   place and is mounted now (`mount | grep <share>`), then run:

   ```bash
   bash ~/mileage-tracker/backup/backup.sh --mark-target
   ```

3. Run `bash ~/mileage-tracker/install.sh --check`. It reports a missing marker
   as a problem and a backup systemd file that differs from `backup/` as a
   warning (the rerun in step 1 installs the new ones).
4. Run `sudo systemctl start mileage-backup@$USER.service` and confirm
   `systemctl status` shows `status=0/SUCCESS`.

### Restoring for real

After losing the disk, rebuild the server with Steps 1–9, put
`backup.conf` back, then restore. Two things to know first.

**Keep these files somewhere other than the server.** None of them is in a
snapshot (they hold credentials), so a lost disk loses them:

- `~/mileage-tracker/.env`: the ingest upload token, `DISPLAY_TZ`, `BIND_ADDR`
  and the other settings.
- `~/mileage-tracker/viewer.env`: the admin login and the viewer's session key.
- `~/mileage-tracker/monitor.env`, if you use notifications or metrics.
- `~/mileage-tracker/backup/backup.conf` and the SSH key it names
  (`~/.ssh/mileage_backup`), without which the rebuilt server can't reach the
  off-host copy at all.
- Your ntfy volume, if you use it (see below).

Keep them off this machine and off the backup destination (a password manager
or an encrypted USB stick). If you rebuild without the old `.env`, the server
generates a new upload token. The tracker still carries the old one, so every
upload gets `401` until you rebuild its firmware with the new token and flash
it again (Step 8). Putting the old `.env` back before you start the services
avoids that.

**Don't let the rebuild's own backup hide the history.** If you turn on
backups during the rebuild with the same destination, the installer's first
backup adds a new, empty snapshot there (the old history stays; the marker
file from the old server is already in place). `latest` skips a newest snapshot
with no trips while an older one has some and says so, but the safest way is to
name the snapshot you want. List what is there, then restore that one:

Use the same `-e` value as `RSYNC_SSH` in `backup.conf`: the backup logs in
with the dedicated key, and a plain `rsync` would try your default SSH
identities, which a rebuilt server doesn't have. If `REMOTE` is a mounted
folder instead, run `ls <REMOTE>/snapshots/`.

```bash
rsync -e "ssh -i ~/.ssh/mileage_backup" --list-only <backup-user>@<backup-host>:/<backup-path>/snapshots/   # your REMOTE
bash ~/mileage-tracker/backup/restore.sh restore 2026-10-06T033012Z
```

Or skip backups in the installer's question during the rebuild, restore, and
set backups up afterwards. Check the line `Snapshot holds N trip GPX files`
before you type `RESTORE`; with `--yes` there is no prompt to stop you. Without
a name, the command is:

```bash
bash ~/mileage-tracker/backup/restore.sh restore latest
```

`latest` means the newest snapshot on the off-host copy that holds trips. You
can also name one (`2026-10-06T103000Z`) or give a path. It runs the same checks as
`restore.sh check`, asks you to type `RESTORE`, and stops ingest, viewer and
any installed monitor. It saves all current databases before replacing any,
and saves trip files it would overwrite under
`~/mileage-backups/pre-restore-<time>/`. It restores databases through SQLite's
backup API with their writers stopped: the ingest helper writes events.db;
the viewer and monitor helpers write their own databases. It starts the default
services again and restarts the monitor if it was running. A saved monitor DB
can create the volume on a replacement server without enabling notifications.
It never deletes trip files that aren't in the snapshot.

If monitor discovery fails, restore warns, skips `monitor.db`, and leaves the
monitor service running or stopped as it was, while restoring trips, annotations
and diagnostics. Fix discovery and rerun the restore to include monitor state.
Alternatively, to restore **only monitor state by hand**, first run
`backup/restore.sh check <snapshot>` and keep the checked `monitor.db` from that
snapshot. In the server directory, note whether
`docker compose --profile monitor ps --status running --quiet mileage-monitor`
reports a running monitor, then stop it with
`docker compose --profile monitor stop mileage-monitor`. Save its current
`/state/monitor.db` using SQLite's backup API before replacing anything. With the
writer stopped, use a throwaway `mileage-monitor` container and SQLite's backup
API to copy the checked snapshot into `/state/monitor.db`, then run
`PRAGMA integrity_check` on the restored DB. Start it with
`docker compose --profile monitor start mileage-monitor` only if it was running
before. Do not copy a live SQLite file or its sidecars. Losing monitor state can
replay alerts/exports; it does not lose trips or annotations.

Old snapshots without `events.db` or `monitor.db` still restore trips and
annotations. The script clearly reports missing DB snapshots and leaves the
current corresponding databases unchanged. Old live copies of events.db and
its journal under `data/` are always excluded from restore; they are not safe
SQLite snapshots. Missing diagnostics or monitor state on a replacement server
starts fresh when the owning service next creates it. The optional ntfy volume
(users, tokens and cached messages) is not included; back it up separately.

`restore.sh` must run as the UID 1000 login. With another UID it cannot write
into `data/` (owned by UID 1000, mode 700): it fails after stopping the
services, restarts them on exit, and nothing is restored. Restored files keep
UID 1000 ownership, so no `chown` is needed afterwards.

**A restore also brings back everyday logins as they were in that snapshot.**
A password reset, account disable or removal made since then is undone. Open
the admin pages afterwards and redo any of those, and check that trip removals
and closed tax years match what you expect.

---

## Step 11 — Optional device notifications

Skip this step to keep notifications off, which is the default. The tracker
logs trips the same way either way; these outputs only tell you about faults
in the tracker and let you see its health.

### What the installer asks

`install.sh` asks in its **Notifications** step. That step runs last: after
the services are up and the backups are set up, so nothing optional here can
hold up the backups. You can pick none, or any of these. Each one runs on a
server you control, with no subscription or cloud account:

- **ntfy** sends phone notifications when a tracker reports a fault, or goes
  silent (no upload for the number of days you choose). Choose *bundled* to
  run ntfy in a container here (11a), or *existing* to send to an ntfy server
  you already run.
- **metrics** opens a token-protected Prometheus page on port 8093, for Grafana
  dashboards and alert rules (11e).
- **graylog** sends each diagnostics event to a Graylog you already run (11d).
- **loki** pushes the same events to a Loki you already run (11f).

Then the installer tests each output that reaches a destination. None of
these tests is a real alert:

- **ntfy** (bundled or existing): the monitor posts one test notification to
  the topic, and then asks whether it arrived on the phone. A "no" gets a
  warning with the likely causes. A refused post is a failure.
- **graylog**: the monitor sends one test message through its own GELF sender,
  then asks whether it shows up in Graylog (search `source:mileage-tracker`).
  A "no" gets a warning. A refused message is a failure.
- **loki**: the monitor pushes one test line and checks only the HTTP status:
  a 2xx passes. Nothing asks you to find the line in Grafana.
- **metrics**: nothing is sent. The installer checks from this server that
  `/metrics` answers 401 without the token and 200 with it.
- **bundled ntfy** also gets a deny-all check. The installer reads the topic
  from inside the monitor's container with no login. A refusal (401 or 403)
  passes. Anything else fails, because it would mean anyone who can reach
  port 8092 can read your alerts. The setting behind it is
  `auth-default-access: "deny-all"` in `ntfy/server.yml`, which the installer
  also checks.

A failure stops the install at that point, with the reason. The backups are
already done by then.

Your answers go to `monitor.env`, in the server directory. That file is
gitignored and mode 600, and it holds the tokens and passwords. `.env` gets
one line, pointing at `monitor.env`, and nothing else. Only the monitor (and
the bundled ntfy, when you choose it) is started or restarted, so trips and
the viewer keep running through the change. Nothing is ever deleted: the ntfy
users and the monitor's delivery state live in named volumes.

Running `install.sh` again shows the outputs that are on and asks whether to
keep them. If you keep them, the installer still applies `monitor.env`
without asking anything else and without sending any test. It recreates the
monitor only when its code or settings changed, so a re-run after copying new
server code picks that code up. It starts the bundled ntfy if that's on, and
it checks everything. Answers come back as defaults, and a secret you already
set is never wiped: press Enter at its question and it's kept. For the bundled
ntfy, the phone username offered is the owner of the token already in
`monitor.env`, so Enter keeps that user and its token. Typing a different
username gets that user a new token, since the old one doesn't grant it
access. Turning an output off keeps its URL and secrets in `monitor.env`, so turning it back on
needs no re-entry, except Loki's URL and login: those are commented out when
it's off, and you type them again when it's turned back on.

`install.sh --check` leaves your settings and the notification outputs alone:
it only checks them. (It does upload and remove a temporary test trip, as
described near the top of this runbook.) It verifies what is on: the monitor
must be running and its startup log must show the selected outputs, each
destination must be reachable from the monitor, metrics must refuse a request
without the token, and the bundled ntfy must refuse anonymous reads. Outputs that are off are reported as "off", never as
problems. A deployment without a monitor container has no monitor-health
warning. An existing running monitor is also checked for heartbeat health,
even when its settings disable every output.

It also compares the container's Compose configuration hash with the current
configuration, including URLs, credentials and tuning values in `monitor.env`,
without displaying them. Older settings are reported with a command to apply
them. If Compose cannot supply the hash, the message says only the selected
outputs were checked. After editing `monitor.env`, recreate the monitor as in
11c and check delivery at each destination; these checks do not prove delivery.

For an unattended install, every question has an environment variable. Set
`INSTALL_OUTPUTS` to `none` or a list such as `ntfy,metrics`. Set
`INSTALL_KEEP_OUTPUTS` too, to answer the keep question on a re-run: `y` keeps
what's on, `n` uses the list in `INSTALL_OUTPUTS`. If you preset
`INSTALL_OUTPUTS` but not `INSTALL_KEEP_OUTPUTS`, the keep question is skipped,
the preset list is used, and the installer says so. If you preset neither, a
re-run asks the keep question, so an unattended re-run has to preset one of
the two. The rest:

- `INSTALL_STALE_DAYS`: days without an upload before a silence alert (default 7)
- `INSTALL_NOTIFY_MIN_SEVERITY`: lowest severity to send, `info`, `warning`, `error` or `critical` (default `error`)
- ntfy: `INSTALL_NTFY_MODE` (`bundled` or `existing`), `INSTALL_NTFY_TOPIC`, `INSTALL_NTFY_USER` (on a re-run, the owner of the current token by default; otherwise `mileage`), `INSTALL_NTFY_PASSWORD` (the phone login), `INSTALL_NTFY_URL` and `INSTALL_NTFY_TOKEN` (existing server), `INSTALL_NTFY_READY` (`y` once the phone is set up, `n` to skip the test) and `INSTALL_NTFY_ARRIVED` (`y` or `n`)
- graylog: `INSTALL_GRAYLOG_TARGET` (a GELF HTTP URL, or `host:port` for TCP) and `INSTALL_GRAYLOG_ARRIVED` (`y` or `n`)
- loki: `INSTALL_LOKI_URL`, `INSTALL_LOKI_AUTH` (`none`, `bearer` or `basic`), `INSTALL_LOKI_BEARER_TOKEN`, `INSTALL_LOKI_BASIC_AUTH_USER`, `INSTALL_LOKI_BASIC_AUTH_PASSWORD` and `INSTALL_LOKI_TENANT_ID`

A preset that isn't usable stops the install with a message, as the other
questions do. Metrics has no question of its own: its token is generated and
kept in `monitor.env`, never printed.

**Phone alerts only reach the phone where it can reach this server.** The
bundled ntfy is published on your LAN address, port 8092, like the other
ports. So the phone gets alerts on home WiFi or over a VPN into your network.
Away from that, alerts wait in ntfy's cache (72 hours by default).

**Metrics on port 8093** is published on the same LAN address only. Its token
is its only protection, so never forward that port from the internet. Prometheus
has to be on your LAN, or reach it over a VPN, to scrape it.

Opening either port to the internet is your decision and isn't part of this
setup.

The sections below are the same steps by hand, for when you'd rather not use
the installer or want to see what it does.

Run these commands **on the Debian server**, in `~/mileage-tracker` (the copied
`server/` directory). Replace angle-bracket placeholders before running commands.

### 11a — Start and secure ntfy

Set `BIND_ADDR` in `.env` to the server's LAN IP first (Step 7). ntfy publishes
port **8092** on that address, just like the other ports; Docker ports bypass
UFW. A LAN-only ntfy reaches your phone only on home WiFi or over a VPN that
can reach this server. Away from that connection, messages wait in its cache
(72 hours by default). The owner decides about remote access; opening an
internet port is not part of this setup. Use HTTPS for remote ntfy endpoints:
HTTP sends credentials and messages unencrypted.

```bash
cd ~/mileage-tracker
docker compose --profile ntfy up -d ntfy
docker compose exec ntfy ntfy user add "<username>"
docker compose exec ntfy ntfy access "<username>" "<topic>" rw
docker compose exec ntfy ntfy token add "<username>"
```

`user add` prompts for a new password privately. Use an ordinary user, not an
administrator. `access` grants only the named topic; the token inherits that
user's permissions. Save the printed token privately for 11b; do not put it in
screenshots, logs or Git. The bundled `ntfy/server.yml` sets
`auth-default-access: deny-all`, preventing anonymous reading and publishing.
Its cache and auth databases persist in the `ntfy-state` named volume.
[ntfy's reference covers access and tokens](https://docs.ntfy.sh/config/#access-control).

Install the free ntfy phone app (Android: Play Store or F-Droid; iOS: App Store).
Add your server `http://<host>:8092`, log in with the local username/password,
and subscribe to the same `<topic>`.

**iOS instant push:** edit `ntfy/server.yml`, replacing `upstream-base-url: ""`
with `upstream-base-url: "https://ntfy.sh"`. Add `NTFY_BASE_URL=http://<host>:8092`
to `.env`, using exactly the server URL the phone subscribes to (HTTPS if you
provide it). Apply both changes with `docker compose --profile ntfy up -d ntfy`
then `docker compose restart ntfy`. The free upstream relays a wake-up poke,
not message text; the phone fetches text from your server and must still be
able to reach it. Android needs no relay. Without the setting, iOS delivery
can be delayed by hours. See [ntfy's iOS explanation](https://docs.ntfy.sh/config/#ios-instant-notifications).

An update replaces `ntfy/server.yml` with the release copy (`.env` is kept), so
the `upstream-base-url` edit is lost and iOS delivery quietly goes back to
being delayed. After each update, redo the edit and run
`docker compose --profile ntfy up -d ntfy` again.

### 11b — Enable the monitor

```bash
test -f monitor.env || install -m 600 monitor.env.example monitor.env
chmod 600 monitor.env
nano monitor.env
```

Set `MONITOR_ENABLED=true`, `NOTIFY_ENABLED=true`,
`NTFY_URL=http://ntfy:80/<topic>` and `NTFY_TOKEN=<token>` in that private file.
`ntfy` is the container name on the Compose network; the phone uses
`http://<host>:8092`. For an existing ntfy server, use its complete topic URL.
Keep the ingest token and viewer secrets out of this file.

Add the filename selector to `.env` (not the token):

```dotenv
MONITOR_ENV_FILE=./monitor.env
```

```bash
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose --profile monitor up -d --build mileage-monitor
docker compose logs --tail 30 mileage-monitor
```

Look for `Monitor enabled; ntfy enabled, Graylog disabled, Prometheus disabled, Loki disabled`. Bad enabled settings fail startup
with a message naming the setting. A missing diagnostics DB means no device
has checked in yet; an unreadable DB logs unavailable and retries next minute.
The monitor mounts `/data` read-only and persists policy and delivery state in
`monitor-state`. Port 8093 is published on `BIND_ADDR` for the optional
token-protected metrics endpoint; the listener stays off unless enabled. The
`monitor` and `ntfy` profiles keep
both optional services off unless selected. Once enabled in `monitor.env`,
`install.sh --check` verifies them too.
Once started, their restart policies also bring them back on reboot.

| Setting | Default | Meaning |
|---|---|---|
| `MONITOR_ENABLED` | `false` | Watch every device that has ever checked in; no watchlist |
| `NOTIFY_ENABLED` | `false` | Allow ntfy sends; false still tracks conditions locally |
| `NOTIFY_MIN_SEVERITY` | `error` | Minimum catalog severity for device faults |
| `NOTIFY_HOLD_SECONDS` | `300` | Hold from first eligible monitor observation; critical bypasses it |
| `NOTIFY_REPEAT_HOURS` | `24` | Minimum time after successful delivery before a reminder |
| `NOTIFY_MAX_PER_HOUR` | `6` | Rolling-hour successful POST cap, including digests and resolutions |
| `MONITOR_STALE_DAYS` | `7` | No-contact threshold, using server receipt time including identified GPX uploads |
| `METRICS_ENABLED` | `false` | Start token-protected Prometheus `/metrics` on port 8093 |
| `METRICS_TOKEN` | unset | Separate 32+ character bearer token, required when metrics are enabled |
| `LOKI_PUSH_URL` | blank | Enable event and condition-change pushes plus ntfy-delivered decisions to external Loki |
| `LOKI_BASIC_AUTH_*`, `LOKI_BEARER_TOKEN` | blank | Choose basic or bearer auth for Loki, never both |
| `LOKI_TENANT_ID` | blank | Optional value for Loki's `X-Scope-OrgID` header |

Booleans are `true`/`false`; severity is `info`, `warning`, `error` or `critical`.
Hold accepts 0–604800 seconds, repeat 1 second–8760 hours, cap an integer
1–1000, and silence 1 second–36500 days. Fractional hours/days are allowed.
Delivery requires an HTTP(S) topic URL without embedded credentials, query or
fragment, ending in a 1–64 character topic (letters, digits, `_` or `-`), plus
a token. `MONITOR_ENABLED=false` idles without DB reads.

Silence **E7001 DEVICE_SILENT** reads "No contact for N days", bypasses the
device-fault severity threshold and hold, and shares reminders and the cap.
It resolves on genuinely newer contact. Parking away from WiFi can cause it.
After 24 hours without contact, the tracker tries a heartbeat during existing
awake opportunities, with no extra wake-ups. It cannot send while switched off
or away from WiFi, so a parked tracker can still trigger this notification.
The monitor does not wake the tracker. Retiring an obsolete identity needs a
future explicit policy; all devices remain watched.

All monitor outputs use the same current-condition rule. Faults need a
snapshot at most **24 hours** old (including exactly 24 hours); GPX contact
alone does not refresh it. The accepted snapshot takes precedence over stored
condition rows; a missing or future snapshot means unknown. Severity and
`notify` filter phone alerts only; Prometheus and Loki condition lines show
current catalog codes, while Graylog also describes unknown codes. Old events, unknown conditions and a reboot do not imply recovery.
A resolution needs a successfully sent alert and a newer snapshot explicitly
observing that code clear. Overflow coalesces into digests of at most 3500
UTF-8 bytes; unsent work persists and is rechecked before delivery. Failed
posts retry after 60, 120, 240 seconds, and so on, capped at an hour. Delivery
is **at least once**: a crash after ntfy accepts a message but before success
is recorded can produce a duplicate.

The monitor also checks its own progress without a network request: each
completed tick touches `/state/heartbeat` in `monitor-state`. Disabled ticks
count as completed without reading databases, as does a missing `events.db`
before the first upload. Unreadable diagnostics or a failed tick do not
refresh it, so a persistent database problem also becomes unhealthy.
Unexpected tick exceptions log the type and traceback frames only (no
exception message, source lines, local variables or private paths), then
wait the usual 60 seconds and try again. KeyboardInterrupt and SystemExit
still stop the process.

Compose probes the local file every 30 seconds with a five-second timeout,
a 90-second startup grace and two consecutive failures required. The file
may be at most 180 seconds old, allowing three missed one-minute ticks:
a stalled running monitor normally turns `(unhealthy)` in `docker ps` about
3.5-4 minutes after its last completed tick. `install.sh --check` reports
that failure too, including a disabled monitor that is still running.
An old container without this check needs rebuilding and recreating.
Health measures tick progress; it does not confirm delivery, and a long
export can delay it. Docker does not restart a running container just for
being unhealthy. Check the cause with:

```bash
docker compose --profile monitor logs --tail 50 mileage-monitor
```

The health probe uses no credentials or network. An external alive-ping URL
is not configured; a separately chosen service could be a future addition.

### 11c — Check delivery and turn it off

Enter the topic token privately for an authenticated manual test:

```bash
read -rs -p 'ntfy token: ' NTFY_TEST_TOKEN; echo
printf 'Authorization: Bearer %s\n' "$NTFY_TEST_TOKEN" | curl -fsS -H @- \
  --data-binary 'Mileage tracker test notification' 'http://<host>:8092/<topic>'
printf 'Authorization: Bearer %s\n' "$NTFY_TEST_TOKEN" | curl -fsS -H @- \
  'http://<host>:8092/<topic>/json?poll=1'
unset NTFY_TEST_TOKEN
```

The poll response and phone should show the text. This checks ntfy; a synthetic
diagnostic batch with a current critical fault checks the whole ingest/monitor
path (batch format: `DIAGNOSTICS.md`).

After editing `monitor.env`, run `docker compose --profile monitor up -d
mileage-monitor` to apply it; restart alone keeps the old environment. To
disable sends, set `NOTIFY_ENABLED=false` and run that command. To stop both:

```bash
docker compose --profile monitor --profile ntfy stop mileage-monitor ntfy
```

Keep the named volumes: deleting monitor state loses deduplication and can
replay alerts; deleting ntfy state loses users, tokens and cached messages.
Step 10 backs up monitor state with SQLite's backup API and restores it with
the monitor stopped. The ntfy volume is not included and needs its own backup.

### 11d — Optional: send diagnostics to your own Graylog

Skip this unless you run [Graylog](https://graylog.org/) somewhere else on
your network. Nothing here installs Graylog; the monitor only sends to it.
It works with or without ntfy: Graylog gets the facts and does its own alerting.

**What it sends**, as [Graylog Extended Log Format (GELF)](https://go2docs.graylog.org/current/getting_in_log_data/gelf.html)
messages with source `mileage-tracker`. Field names below are as Graylog shows
and searches them; on the wire GELF prefixes each with `_` (`kind` is `_kind`),
and Graylog removes it:

- `kind:event`: every diagnostic event the tracker uploaded, at least once, at the
  time the server received it. On first enable, the whole stored history is
  sent, up to 200 messages a minute.
- `kind:condition`: a change in a fault's state on a device: `state` is
  `active`, `clear` or `unknown` (no fresh health report, so its state isn't
  known). State is per device and code, so a fault that persists across the
  tracker's deep-sleep wakes is one `active` message, not one per wake.
  E7001 `active` means no contact for `MONITOR_STALE_DAYS`
  (`days_without_contact` says how long); it turns `clear` on the next contact.
  On first enable, each device's current states are sent once as a baseline.

An interrupted send or restoring an older diagnostics database can cause
duplicates: a successful send can happen before its progress is saved.

Every message carries `device_id`, `code` (e.g. 4005), `symbol`,
`severity` and `title` from the server's catalog. `level` follows severity
(critical 2, error 3, warning 4, info 6; a code the server doesn't know yet is
5). `clear` and `unknown` states, and `clear` events, are always level 6, so
"level ≤ 3" finds only active errors and critical faults. Events also carry
`transition`, `episode_id`, `boot_id`, `sequence`, the device clock
(`device_utc_ms`, `time_source`) and catalog context as `ctx_<name>`. No
location, WiFi names, free text or credentials are ever sent.

**1. Create an input in Graylog** (*System → Inputs*): **GELF HTTP** is the
better choice, because Graylog confirms each message (HTTP 202) and the
monitor only moves on once it does. **GELF TCP** works too, but TCP can't
confirm receipt, so after a network fault a batch may arrive twice. Port 12201
is the usual choice. Don't use GELF UDP: it drops messages without telling anyone.

**2. Open the path** from this server to the Graylog host on that port (its
firewall, and Docker's if Graylog runs in a container). If the two hosts talk
over anything you don't trust, use TLS: an `https://` URL for HTTP, or
`GRAYLOG_GELF_TLS=true` for TCP. For a private certificate authority (CA), copy
its certificate into the monitor's state volume before enabling Graylog. Put your CA certificate
in `graylog-ca.pem` in the server directory first. The helper below works
even before a monitor container exists and writes the file as its `app` user:

```bash
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose --profile monitor build mileage-monitor
docker compose --profile monitor run --rm --no-deps -T mileage-monitor sh -c 'cat > /state/graylog-ca.pem' < graylog-ca.pem
```

Then set `GRAYLOG_GELF_CA_FILE=/state/graylog-ca.pem` in `monitor.env`.

**3. Configure the monitor.** If you skipped ntfy, create the private file
without overwriting any existing monitor settings:

```bash
cd ~/mileage-tracker
test -f monitor.env || install -m 600 monitor.env.example monitor.env
chmod 600 monitor.env
nano .env
```

Add or uncomment `MONITOR_ENV_FILE=./monitor.env` in `.env`; otherwise Compose
loads the disabled example and the monitor idles. In `monitor.env`, set
`MONITOR_ENABLED=true` and configure Graylog:

```dotenv
GRAYLOG_ENABLED=true
GRAYLOG_GELF_URL=http://<graylog-host>:12201/gelf
# or: GRAYLOG_GELF_TCP=<graylog-host>:12201
```

Set exactly one of `GRAYLOG_GELF_URL` and `GRAYLOG_GELF_TCP`. The shipped
`monitor.env.example` has `GRAYLOG_GELF_URL` uncommented with a placeholder, so
for TCP delete or comment out that line. With both set, the monitor refuses to
start, which also stops ntfy and Loki delivery, and the only trace is a
one-line "Set exactly one of GRAYLOG_GELF_URL or GRAYLOG_GELF_TCP" in its log.

`GRAYLOG_GELF_HEADER=<Header-Name>: <value>` adds one HTTP header, for a proxy
in front of the input. Build and apply with the same version stamp as the
other services:

```bash
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose --profile monitor up -d --build mileage-monitor
```

Then check the log for `Graylog enabled` and, once there is
something to send, `Graylog accepted N message(s)`. If Graylog is down, the
monitor logs the failure, keeps its place, and retries after 60 seconds,
doubling to an hour. ntfy alerts and trip uploads are unaffected.

**4. Alert in Graylog.** Create a stream for `source:mileage-tracker`, then
Event Definitions (*Alerts → Event Definitions*, a filter-and-aggregation
condition over that stream) with the notifications you want:

| Alert | Search query |
|---|---|
| Active error or critical fault | `kind:condition AND state:active AND level:<=3` |
| Tracker silent | `kind:condition AND code:7001 AND state:active` |
| Fault cleared (optional) | `kind:condition AND state:clear AND NOT code:7001` |

A condition message is sent only when the state changes, so a search window of
a few minutes, run every minute, catches each change once. Graylog's grace
period stops repeats. For a "still broken" reminder, build it from the
dashboard or use ntfy's daily reminder instead.

To stop sending, set `GRAYLOG_ENABLED=false` and apply. The monitor remembers
how far it got in `monitor-state`; turning it back on resumes from there.

### 11e — Optional: Prometheus metrics for external Grafana

This endpoint is for a Prometheus server you already run elsewhere. This
project does not install Prometheus or Grafana. Prometheus must be able to
connect to the tracker server's `BIND_ADDR` IP on TCP port 8093, directly on
the LAN or over a VPN route. Docker-published ports bypass UFW, so `BIND_ADDR`
is the important boundary. The bearer token is the only protection on this
plain-HTTP port; keep it LAN-only and never forward it from the internet.

Copy the monitor template if you have not already done so, and generate a
separate token. Do not reuse the ingest token or viewer credentials:

```bash
cd ~/mileage-tracker
test -f monitor.env || install -m 600 monitor.env.example monitor.env
chmod 600 monitor.env
openssl rand -hex 32
nano monitor.env
```

Set `MONITOR_ENABLED=true`, `METRICS_ENABLED=true` and put the random output in
`METRICS_TOKEN` in the private `monitor.env`. Keep the token out of shell
history, screenshots and Git. In the existing `.env`, select that file with
`MONITOR_ENV_FILE=./monitor.env`. Apply the settings:

```bash
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose --profile monitor up -d --build mileage-monitor
docker compose logs --tail 30 mileage-monitor
```

The log should say that Prometheus is enabled. If its token is missing or
shorter than 32 printable ASCII characters, the monitor refuses to start and
names `METRICS_TOKEN` in the error. The profile publishes port 8093 on the
`BIND_ADDR` host IP; when metrics are disabled, no HTTP listener accepts
scrapes.

On the Prometheus host, store the same token in the private file referenced by
[`grafana/prometheus-scrape.yml`](server/grafana/prometheus-scrape.yml), then
merge that file's job into Prometheus `scrape_configs`. Replace `<host>` with
the tracker server's LAN IP or a VPN-reachable host name. Do not put the token
directly in the scrape YAML. Prometheus reads the token file and sends it as a
bearer credential on each scrape.

Test the path from the Prometheus host (or another host on the same LAN/VPN):

```bash
read -rs -p 'metrics token: ' METRICS_TEST_TOKEN; echo
printf 'Authorization: Bearer %s\n' "$METRICS_TEST_TOKEN" | curl -i -H @- \
  'http://<host>:8093/metrics'
unset METRICS_TEST_TOKEN
```

An authenticated request returns Prometheus text; missing or incorrect tokens
return 401. Import [`grafana/tracker-health.json`](server/grafana/tracker-health.json)
in Grafana and select the Prometheus data source. The starter alert expressions
are in [`grafana/alert-rules.yml`](server/grafana/alert-rules.yml); the
error/critical rule reads `mileage_tracker_condition_state` and works with
`NOTIFY_ENABLED=false`. `mileage_tracker_alert_decisions` is ntfy delivery
state, so it can remain zero when ntfy is off. The no-contact rule uses the
configured `MONITOR_STALE_DAYS` threshold. The HTTP thread serves the monitor's
latest published snapshot and does not query SQLite during scrapes.

To stop exposing metrics, set `METRICS_ENABLED=false` and reapply the monitor
profile. Keep the separate token private; it is not the ingest token.

### 11f — Optional: push to external Loki

Skip this unless you run Loki elsewhere. Nothing in this Compose project
installs Loki or Grafana. The monitor container must be able to reach the Loki
host and port over the server's outbound LAN or VPN route. Use the Loki host's
LAN address or a reachable DNS name: `localhost` inside the monitor container
means the container itself. Permit that outbound path in the relevant host and
network firewalls. Use HTTPS when the path leaves a trusted network; the
request contains diagnostic facts, not credentials in its JSON body.

Create the private monitor file if it is missing; keep existing settings:

```bash
cd ~/mileage-tracker
test -f monitor.env || install -m 600 monitor.env.example monitor.env
chmod 600 monitor.env
```

Set `LOKI_PUSH_URL` to the full HTTP(S) `/loki/api/v1/push` endpoint in the
private `monitor.env`. Configure either bearer auth or basic auth, never both.
Add the tenant ID only if your Loki requires `X-Scope-OrgID`:

```dotenv
MONITOR_ENABLED=true
LOKI_PUSH_URL=https://<host>/loki/api/v1/push
LOKI_BEARER_TOKEN=<token>
# Or use these two lines instead of LOKI_BEARER_TOKEN:
# LOKI_BASIC_AUTH_USER=<user>
# LOKI_BASIC_AUTH_PASSWORD=<password>
# Optional multi-tenant header:
# LOKI_TENANT_ID=<tenant>
```

If Loki has no authentication, leave all three auth settings blank. Do not put
credentials in the URL or commit them. In `.env`, select the private file with
`MONITOR_ENV_FILE=./monitor.env`, then recreate the monitor:

```bash
cd ~/mileage-tracker
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose --profile monitor up -d --build mileage-monitor
docker compose logs --tail 30 mileage-monitor
```

The startup line should say `Loki enabled`; once it has new records, the log
reports how many lines Loki accepted. On first enable, the separate persisted
event cursor starts at rowid zero, so stored history is sent in batches of at
most 200 lines per tick without an age filter. Loki may reject history older
than its configured retention window. Only HTTP **400** (including too-old
lines) and **422** (unusable content) skip the entire rejected batch and
advance its cursors; that history is not retried, and later batches continue.
All other HTTP errors and network failures back off from one minute to one
hour without advancing event, condition or delivered-decision progress. This
includes 401/403 (token or tenant), 404/405 (push URL or proxy route), 407
(proxy authentication), 408, 429 and 5xx. HTTP 413 also retains the batch: a
proxy size limit can be adjusted, and this status can be temporary.

A wrong token or address produces a warning such as `Loki delivery failed
(HTTP 401); check token, tenant, push URL or proxy; retry in 60 seconds
without advancing cursors`. The full warning appears once per retry episode;
repeated rejections use debug logging until a successful push resets it.
Credentials, response bodies and the configured URL are never logged. Nothing
is discarded while these settings errors are retried. Correct `monitor.env`
and recreate `mileage-monitor` to apply it; retained history then resumes.
Loki can still reject old history with 400 after a long outage, under the
content-rejection rule above.

Loki also receives one `condition_change` line when a device/code condition
changes to active, clear or unknown, including E7001 silence and recovery.
This state-change export is independent of `NOTIFY_ENABLED` and is deduplicated
across repeated check-ins and deep-sleep wakes. Successful ntfy alerts,
reminders and resolutions remain separate `alert_decision` lines; those lines
exist only for notifications ntfy accepted while Loki was enabled. With ntfy
off, diagnostic events and condition changes still flow to Loki.

Each line is JSON. The monitor sets only the stream labels `job=mileage-tracker`,
validated `device_id`, and current-catalog `severity`; context and other fields
stay in the line. Loki 3 can add its own `service_name` label and
`detected_level` metadata, so queries can show more fields than the monitor
sends. The [LogQL examples](server/grafana/loki-alerts.md) use the monitor's
labels and do not depend on those additions. Each entry carries its real
timestamp (server receipt time for events, the monitor tick for condition
changes and decisions). This needs Loki 2.4 or newer, which accepts
out-of-order writes within about an hour of a
stream's newest entry. While an event backlog drains, for example on first
enable, the monitor sends only events, oldest first, and holds condition and
decision lines until it catches up. Loki still rejects anything older than its
`reject_old_samples_max_age` (one week by default); that history is skipped. Delivery uses a ten-second timeout,
does not follow redirects, and retries independently from one minute to one
hour. An ambiguous response can cause a duplicate, but does not lose a cursor.
Restoring `events.db` below the Loki rowid resets only the Loki event cursor;
GELF's cursor and progress are unchanged.

Import [`grafana/loki-panel.json`](server/grafana/loki-panel.json) after adding
your external Loki data source. [`grafana/loki-alerts.md`](server/grafana/loki-alerts.md)
uses condition-change lines for its primary error/critical alert, so it works
with ntfy off. Those LogQL rules detect transitions in a recent time window;
the Prometheus rule from Step 11e reads the current active condition state and
the configured no-contact state. Delivered-decision LogQL examples are
separate and apply only when ntfy is enabled.

To pause Loki, comment out all five settings in `monitor.env`:
`LOKI_PUSH_URL`, `LOKI_BEARER_TOKEN`, `LOKI_BASIC_AUTH_USER`,
`LOKI_BASIC_AUTH_PASSWORD` and `LOKI_TENANT_ID`. Reapply with
`docker compose --profile monitor up -d mileage-monitor`. Leaving credentials
or a tenant set without a URL is a configuration error that stops the whole
monitor, including its other outputs. Its cursor and pending decisions stay
in `monitor-state`; when re-enabled, it resumes from those cursors.

## Troubleshooting

**Container is `unhealthy` or restarting**

```bash
docker compose logs mileage-ingest --tail 50
docker compose logs mileage-viewer --tail 50
```

Each service keeps at most three 10 MB Docker log files per container, including
optional monitor and ntfy. The limit applies after the containers are recreated;
the installer update does this when their Compose settings change. Old logs
are not trimmed in place. Recreating a container discards its old logs, so save
any you need first. `bash install.sh --check` warns about older log settings
and shows the command to apply the limit.

**Upload returns 500, logs show a permission error** — the `./data` ownership
problem from Step 3. Fix: `sudo chown -R 1000:1000 ~/mileage-tracker/data`
then `docker compose restart mileage-ingest`.

**Upload returns 401** — token mismatch. Check whether the running container
has the unquoted hex token from Step 4a without printing either value:

```bash
TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
printf '%s' "$TOKEN" | docker compose exec -T mileage-ingest python -c 'import hmac,os,sys; print("Token matches .env" if hmac.compare_digest(os.environ["INGEST_AUTH_TOKEN"], sys.stdin.read()) else "Token differs from .env")'
unset TOKEN
```

If it differs, check `.env` and run `docker compose up -d mileage-ingest`.
If it matches, compare the board's build settings privately: `AUTH_TOKEN` in
`secrets.h` must match it byte for byte, and the board only picks up a change
when you flash it again (Step 8). Regenerating the token after flashing causes
exactly this. On the bench, a small file with a wrong token reported E5004
(HTTP 401), while a roughly 3 MB file instead reported E5003 (`HTTP -3`):
a broken send can hide the rejection. Check ingest's log for 401s too.

**Upload returns 5xx** — the server answered with an error. The 2026-11-01
(UTC) bench check on firmware `v0.11+f6b0b1c96a` and server v0.10 confirmed
that a 503 keeps the trip queued, raises E5008, and uploads successfully after
the server recovers; retried diagnostics batches created no duplicate events.
Nothing needs doing on the board: it retries on its own. Look in
`docker compose logs mileage-ingest` for the cause on the server.

**Trips stop arriving from the board, but the server checks out** — the
board can't reach it. The usual cause is something that changed since it
was flashed: the server's IP (check the DHCP reservation), the token in
`.env`, or the WiFi name or password. The board has all of these compiled
in, so update `secrets.h` and reflash (Step 8). The firmware guide's
[recording and upload checks](firmware/README.md#check-recording-and-uploads) explain the board's Serial Monitor
output. Trips recorded meanwhile wait on the board, but once its storage
fills it stops recording. On the microSD card it can hold far more
than any realistic gap between uploads; without a usable card, internal
flash fallback holds about 5–6 hours of driving, then recording stops.

**Compose says `INGEST_AUTH_TOKEN` is unset** — `.env` must sit next to
`docker-compose.yml` in `~/mileage-tracker/`, not inside `mileage-ingest/`.

**Viewer container exits immediately at startup** — it fail-fasts on bad admin
auth configuration rather than starting with the login broken. The log says
exactly which value is wrong:

```bash
docker compose logs mileage-viewer --tail 20
```

Usual causes: `viewer.env` not created, or the admin password hash lost its `$`
characters to shell expansion (fix by single-quoting it).

**Locked out of the viewer** — 5 bad passwords lock the IP for 15 minutes.
Failures count until there has been a 15-minute pause without one; the next
failure then starts a fresh count. A successful login clears only failures
against its own username. To unlock immediately, clear it:

```bash
docker compose exec mileage-viewer python -c \
  "import sqlite3;c=sqlite3.connect('/db/annotations.db');c.execute('DELETE FROM login_attempts');c.commit()"
```

**Forgot the admin password** — rerun `bash install.sh`. When asked whether to
keep the existing login, answer `n`, then set the admin username and password
again. Alternatively, regenerate an admin password hash using Step 4b,
replace `VIEWER_PASSWORD_HASH` in `viewer.env`, and run
`docker compose up -d mileage-viewer`. The admin's existing sessions end;
everyday logins and their passwords are unchanged.

**Forgot an everyday password** — an admin can reset it on `/admin`. From
`~/mileage-tracker`, the CLI can also reset it; `manage_users.py` reads the new
password from standard input:

```bash
read -rs -p 'New everyday password: ' new_password; printf '\n'
printf '%s\n' "$new_password" | docker compose run --rm --no-deps -T mileage-viewer python manage_users.py reset-password '<everyday-user>'
unset new_password
```

**Nobody can log in** — from `~/mileage-tracker`, list everyday accounts and
check whether they are enabled:

```bash
docker compose run --rm --no-deps -T mileage-viewer python manage_users.py list
```

For a disabled everyday login, enable it:

```bash
docker compose run --rm --no-deps -T mileage-viewer python manage_users.py enable '<everyday-user>'
```

If no everyday login can be used, `add '<everyday-user>'` creates one. `add` and
`reset-password` read standard input; use the shell's hidden prompt and pipe
above, because the CLI itself does not prompt. This
restores everyday access to the trip list and its allowed actions; it does not
restore the admin login. For a lost admin password, use the admin recovery
steps above.

**Containers missing after a reboot, log says `cannot assign requested
address`** — Docker started before the network brought up the `BIND_ADDR` IP,
or the server's IP has changed. Check `ip -4 addr show` still lists the
address in `.env`, fix the reservation if it doesn't, then
`docker compose up -d`. If it happens on every boot, make Docker wait for
the network:

```bash
sudo systemctl enable ifupdown-wait-online.service           # Debian default (/etc/network/interfaces)
sudo systemctl enable NetworkManager-wait-online.service     # if using NetworkManager
sudo systemctl enable systemd-networkd-wait-online.service   # if using systemd-networkd
```

Enable only the one matching your setup.

**Logged out constantly / login doesn't stick** — `VIEWER_SECRET_KEY` is
changing or differs between workers. It must be a fixed value in
`viewer.env`.

**Viewer loads but the map is blank** — Leaflet/GPX are vendored locally, so
the page frame works offline; only OSM *tiles* need internet. Blank tiles =
the server (or your browser) can't reach `tile.openstreetmap.org`.

**The trip list says a trip summary file can't be read** — the `.json` next
to that trip's GPX is damaged or missing (disk trouble, a hand edit, or a
summary write that failed after the GPX was saved), so the viewer can't
count its miles, and the exports list it as unreadable. The GPX is the
record: send it to ingest again, and ingest rebuilds the summary from it.

```bash
cd ~/mileage-tracker
TRIP=2026-10/2026-10-05T16-00-00Z      # the path from the warning, minus .json
TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
printf 'Authorization: Bearer %s\n' "$TOKEN" | sudo curl -X POST http://<host>:8090/upload \
  -H @- \
  -H "Content-Type: application/gpx+xml" \
  -H "X-Device-Id: tbeam-01" \
  --data-binary @data/$TRIP.gpx
```

The trip files are private to UID 1000, so `curl` runs under `sudo` to read the
GPX even when your server login has another numeric user ID (as in Step 6).
`sudo` passes the header on standard input through to `curl` unchanged.

`X-Device-Id` is the firmware's `DEVICE_ID`. Ingest gives a timed trip the
same name again from its timestamps (it ignores `X-Trip-Id` for a GPX that has
them), so nothing else is needed. (For a GPX with no timestamps at all, add
`-H "X-Trip-Id: ${TRIP#*/}"`.) The reply's `stored_as` should be `$TRIP.gpx`.
If it differs, that trip was filed under a custom `X-Trip-Id` by an older
version of ingest; see [server/README.md](server/README.md) before sending it
again. When it matches, the trip is back in the list with its annotations.
Ingest rebuilds a damaged or missing summary only when the GPX it already holds matches the upload byte for byte,
since that's what proves it's the same trip.

If the warning says there's no GPX either, put the GPX back from a backup
snapshot first (Step 10), then send it as above. Without it, ingest answers
409 (`trip ID has a damaged summary and no GPX to match`).

```bash
ls ~/mileage-backups/snapshots/*/data/$TRIP.gpx      # the snapshots that have it
sudo cp -p ~/mileage-backups/snapshots/<time>/data/$TRIP.gpx data/$TRIP.gpx
```

This copy also uses `sudo`, because `data/` belongs to UID 1000 (mode 700).

**Port already in use** — `sudo ss -tlnp | grep -E '8090|8091|8092|8093'` shows
which program has it. Stop or reconfigure that program to free the required
port, then rerun the installer. Its checks use the supplied ports; changing
the Compose port mapping does not fix this check, and release updates replace
the supplied Compose file.

**Rebuild without changing configuration (code-only fix):**

This is a shortcut for rebuilding the containers from source you have just
changed. **It is not the way to update the server.** An update means copying
the source and running `bash ~/mileage-tracker/install.sh` (see
[Updating](#updating)), because only the installer tightens `data/` to mode
700, writes the backup destination marker and refreshes the backup systemd
unit, timer and drop-ins. After this shortcut on an existing install, backups
can fail from the first night.

First copy the new committed source from your computer using Step 2's
`git -c core.autocrlf=false archive HEAD server | ssh ...` command (or copy a fresh source download
with `scp -r server/. ...`). This refreshes `COMMIT` along with the code;
do not use `HEAD:server`. Then run this **on the server**. Rebuilding without
copying new source keeps the old code and stamp.

```bash
cd ~/mileage-tracker
SERVER_VERSION="$(python3 stamp_version.py)" BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose up -d --build
```
