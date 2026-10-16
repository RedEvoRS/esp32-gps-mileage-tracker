# Debian Deployment Runbook — Mileage Tracker Server

Deploys both services from `server/`:

| Service | Port | Auth | Purpose |
|---|---|---|---|
| `mileage-ingest` | 8090 | Bearer token | Accepts GPX uploads from the T-Beam |
| `mileage-viewer` | 8091 | Session login | Trip list, map audit, annotate, CSV/XLSX export |
| `ntfy` | 8092 | Topic token | Optional self-hosted phone notifications |
| `mileage-monitor` | 8093 | Bearer token when enabled | Optional Prometheus `/metrics` endpoint in the `monitor` profile |

This is the server half of the project. The top-level `README.md` gives
the server setup and the current board target. The target is now a T-Beam
Supreme with MAX-M10S GPS and a SanDisk 32 GB Max Endurance microSD card,
but the firmware port is pending. Step 8 below is retained for the retired
V1.2 only; do not flash those instructions to a Supreme.

Assumes: Debian 12/13, a non-root user with sudo, SSH reachable.
Placeholders: `<user>` = your Debian login, `<host>` = server IP or hostname.
Replace the angle-bracket placeholders before running commands. The server
SSH login is separate from the viewer login you create below and from the
upload token the board uses.

For the supplied automatic backups, the server login currently needs
numeric user ID **1000** (`id -u`). The installer skips backups for other
IDs; changing ownership of `data/` alone does not give that login access
to the private trip files. See Steps 3 and 10 before choosing the account.

## The quick way: `install.sh`

Copy `server/` over (Step 2), then on the server, as your normal login
(not root):

```bash
bash ~/mileage-tracker/install.sh
```

It works through Steps 1 and 3 to 10 below, checking each one, and asks
for only four things: the LAN address to publish the ports on, the
timezone trips are shown in, a username and password for the viewer, and
where backups should go. It
uploads `sample.gpx`, logs in to the viewer, finds the trip in the list,
on its map and in both exports, then deletes it again. At the end it
prints the lines for the firmware's `secrets.h` (Step 8).

If something fails, it says what and stops. Fix that and run it again:
anything already set up is checked and kept, never redone.

To check an install later, after a reboot say, without changing its
configuration (the check uploads and removes a temporary test trip and
can test the viewer login):

```bash
bash ~/mileage-tracker/install.sh --check
```

The steps below are what the script does, one at a time. Follow them if
you'd rather do it by hand, or to understand a step it stopped on.

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

Debian's repos ship an old/renamed Docker. Use Docker's official repo so you
get `docker compose` (v2 plugin), not the deprecated `docker-compose`.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl gnupg git rsync jq

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
group change takes effect:

```bash
sudo usermod -aG docker $USER
exit
```

Reconnect and verify:

```bash
ssh <user>@<host>
docker run --rm hello-world
docker compose version      # expect v2.x
```

---

## Step 2 — Copy the `server/` directory over

Run the copy **on your computer**, where you cloned or unzipped the repo.
If you are still in the SSH session from Step 1, type `exit` first to
return to your computer's terminal. Then:

```bash
cd "<path to your copy of the repo>"
scp -r server <user>@<host>:~/mileage-tracker
```

`scp` comes with Windows 10 and later, macOS and Linux, and works the same
in each. Use this for the first copy only: if `~/mileage-tracker` already
exists, `scp -r` puts the new copy inside it as `server/`.

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

## Step 3 — Create the data directory with the right ownership

Both containers run as a non-root user (`app`, UID 1000 inside the image).
The `./data` bind mount takes its ownership from the **host** directory — if
Docker auto-creates it as root, ingest can't write and every upload 500s.
Create it yourself first:

```bash
cd ~/mileage-tracker
mkdir -p data
ls -ld data          # should be owned by your user
```

If your `id -u` from Step 0 was **not** 1000:

```bash
sudo chown -R 1000:1000 ~/mileage-tracker/data
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

Set the token, and the timezone you drive in:

```
INGEST_AUTH_TOKEN=<paste the hex string>
DISPLAY_TZ=America/Chicago
```

Lock it down and confirm it's not world-readable:

```bash
chmod 600 .env
```

**Save this token somewhere you can get at it** — the same value goes into
`firmware/04_trip_logger/secrets.h` in Step 8. It is an upload credential,
not the password used for SSH or for the viewer web page.

**`DISPLAY_TZ` has no default.** It's the timezone every trip time is
shown and exported in, and ingest uses it to pick the month folder a trip
is filed in. Use your zone's IANA name, `America/Chicago` or
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

The viewer requires a login. Its credentials live in a **separate** file from
`.env` on purpose: the viewer has no business holding the ingest token, and
single-quoting the hash in `viewer.env` preserves its `$` separators.
Compose interpolates unquoted and double-quoted values, including values
in an `env_file`; follow the quoting in the example below.

```bash
cd ~/mileage-tracker
cp viewer.env.example viewer.env
chmod 600 viewer.env
```

Generate the password hash. You'll be prompted; the password isn't echoed and
the plaintext is never written to disk or into the container environment:

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
VIEWER_USERNAME=<username>
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

---

## Step 5 — Build and start

```bash
cd ~/mileage-tracker
docker compose up -d --build
```

First build pulls `python:3.12-slim` and compiles wheels; a minute or two is
normal. Then:

```bash
docker compose ps
```

Wait for both containers to show `healthy` (the healthchecks have a 10s start
period and 30s interval, so give it ~45 seconds). If one says `unhealthy` or
`restarting`, jump to Troubleshooting.

---

## Step 6 — Verify end to end

**Health checks:**

```bash
curl http://localhost:8090/health
curl http://localhost:8091/health
```

Expect `{"service":"mileage-ingest","status":"ok"}` and the viewer equivalent.

**Reject an unauthenticated upload** (this must fail — it's the test that
matters):

```bash
curl -i -X POST http://localhost:8090/upload --data-binary @sample.gpx
```

Expect `401`.

**Real authenticated upload:**

```bash
cd ~/mileage-tracker
TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | cut -d= -f2-)
curl -X POST http://localhost:8090/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/gpx+xml" \
  -H "X-Device-Id: tbeam-01" \
  -H "X-Trip-Id: 2026-04-14T13-45-22" \
  --data-binary @sample.gpx
```

Expect a JSON success body with `distance_miles` ≈ **0.30** over 45 seconds at
~24 mph (that's what `sample.gpx` encodes — if the number is wildly different,
the parse is wrong, not the network).

**Confirm both files landed:**

```bash
ls -l data/2026-04/
cat data/2026-04/2026-04-14T13-45-22.json | jq .
```

You want a `.gpx` (byte-for-byte raw) *and* a derived `.json` summary.

**Confirm the viewer requires a login** (do this before opening a browser —
it's the check that actually matters):

```bash
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://localhost:8091/
```

Expect `302 .../login`. If you get `200`, auth is not engaged — stop and
check `docker compose logs mileage-viewer`.

**Verify the hash survived into the container intact:**

```bash
docker compose exec mileage-viewer env | grep VIEWER_PASSWORD_HASH
```

The value must still contain its `$` separators. If they're gone, re-quote it
in `viewer.env` and `docker compose up -d mileage-viewer`.

**Viewer, from a browser on your computer:**

```
http://<host>:8091/
```

You should land on a login page. Sign in with the username and password from
Step 4b. The trip should then be listed. Click **Map / Edit** → the track
draws on an OSM map. Tag it business/personal and add a note → reload → the
annotation persists. Then hit **Export CSV** and **Export XLSX** and confirm
both download with the annotation joined in. Finally click **Log out** and
confirm you're bounced back to the login page.

> Deliberately fail the password five times and you'll be locked out of that
> browser's IP for 15 minutes — including with the *correct* password. If you
> trip this during setup, clear it with:
> `docker compose exec mileage-viewer python -c "import sqlite3;c=sqlite3.connect('/db/annotations.db');c.execute('DELETE FROM login_attempts');c.commit()"`

**Clean up the test trip** so it doesn't pollute your real mileage log:

```bash
rm ~/mileage-tracker/data/2026-04/2026-04-14T13-45-22.{gpx,json}
rmdir ~/mileage-tracker/data/2026-04   # only goes if nothing else is in it
```

(The annotation row stays in the viewer DB but is harmless — it has no
matching trip and won't appear in exports.)

---

## Step 7 — Firewall and network

Both services are **LAN-only by design**. Ingest has a bearer token; the
viewer has a session login. Neither is safe to port-forward: both the session
cookie and the ingest token travel over plain HTTP, so anyone who can observe
the traffic can replay them. The login is defence in depth against other
devices on your network, not a substitute for TLS.

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

What works is telling Docker which address to listen on. Set `BIND_ADDR`
in `.env` to the server's LAN IP:

```bash
cd ~/mileage-tracker
ip -4 addr show          # find the LAN address, e.g. 192.168.1.10
nano .env
```

```
BIND_ADDR=192.168.1.10
```

Then recreate the containers so the new port mapping takes effect:

```bash
docker compose up -d
docker compose ps
```

The `PORTS` column should read `192.168.1.10:8090->8090/tcp` and
`192.168.1.10:8091->8091/tcp`. When the monitor profile is running, port 8093
should also be bound to the LAN IP. If a published port says `0.0.0.0`, Compose didn't
pick up `BIND_ADDR`; check the spelling and that `.env` is next to
`docker-compose.yml`.

Confirm the mappings and health from the server:

```bash
docker compose port mileage-ingest 8090   # expect 192.168.1.10:8090
docker compose port mileage-viewer 8091   # expect 192.168.1.10:8091
docker compose port mileage-monitor 8093 # when the monitor profile is started
curl http://192.168.1.10:8090/health    # expect {"service":"mileage-ingest","status":"ok"}
```

Both mappings must show only the LAN address; a wildcard (`0.0.0.0` or
`[::]`) or an extra mapping means the ports aren't restricted to it.
Another application may listen on `localhost:8090` while the tracker is
bound only to the LAN address, so a response there doesn't tell you where
the tracker is published. Use the LAN IP for any curl tests from here on.

---

## Step 8 — Point the legacy V1.2 firmware at it (reference only)

> The board target is the T-Beam Supreme with MAX-M10S GPS and microSD
> storage, but its firmware port is not available yet. The `secrets.h`,
> flashing, and storage instructions in this step apply only to the retired
> T-Beam V1.2. Do not use them for the Supreme.

**On your computer**, create the firmware's settings file from the
example. If you are still in the server's SSH session, type `exit` first:

```bash
cd "<path to your copy of the repo>/firmware"
cp secrets.h.example 04_trip_logger/secrets.h
```

(In Windows `cmd`, use `copy secrets.h.example 04_trip_logger\secrets.h`;
PowerShell accepts `cp`.) It has to sit in the sketch folder,
`04_trip_logger/`, next to the `.ino`; that's the only place the build
looks for it. Fill in WiFi SSID/password, the server host/IP, port
`8090`, and the `INGEST_AUTH_TOKEN` from Step 4a — it must match
byte-for-byte. Put only the IP address or hostname in `SERVER_HOST`, with
no `http://`, port or `/upload`. `install.sh` prints the server lines ready
to paste. The WiFi has to be 2.4 GHz, and hidden networks won't work.

> **These settings are compiled into the firmware.** If the server's IP or
> the token changes later, or your WiFi name or password does, the board
> stops uploading until you update `secrets.h` and flash it again.

`secrets.h` is gitignored. Keep it that way.

For a retired V1.2 board only, flash `firmware/04_trip_logger`, check it
works and put it in the car. The top-level README covers those legacy steps
under "Setting it up", steps 3 to 5:
the Arduino IDE setup (ESP32 core 3.3.11, XPowersLib 0.3.3, TinyGPSPlus
1.0.3), the rest of the WiFi requirements, and the **one-time storage setup
a new, empty board may need on its first flash**. For a board that has
already recorded trips, use the firmware guide's storage recovery steps
instead of formatting after a mount error.

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

Two things need backing up:

- **Trip files** in `~/mileage-tracker/data`, the raw GPX and its summary.
  The GPX cannot be regenerated; a summary can be rebuilt from its GPX.
- **The annotations database** (business/personal, purpose, destination)
  in the `viewer-db` Docker volume. It's easy to forget, and it's the
  record the IRS cares about. It can't be rebuilt from the trips.

`backup/backup.sh` does both. It runs daily from a systemd timer, keeps
dated snapshots in `~/mileage-backups`, and copies them to another machine.
Tax records need keeping for years, and a backup on the same disk as the
data won't survive the disk, so the script refuses to run without an
off-host destination. Each run:

- snapshots the database with SQLite's backup API, which gives a
  consistent copy while the viewer is running, then checks its integrity.
  It never copies the live `annotations.db` file, which can catch a write
  half done.
- copies `data/` while holding ingest's upload lock, so it never catches
  a trip with its GPX written and its summary not.
- writes a `SHA256SUMS` manifest. Unchanged files are hard links to the
  previous snapshot, and a run where nothing changed doesn't add a
  snapshot at all, so daily runs cost almost nothing.
- keeps the newest 14 snapshots plus the first of each month for 7 years.
- rsyncs to the off-host copy, then reads the newest snapshot back from
  there and checks it matches byte for byte. The off-host copy is never
  pruned unless you ask for it, so nothing on this box can delete its
  history.

The timer runs the script as your login user, which has to be able to read
the trip files. Ingest writes them as UID 1000 with mode 600, so that user
needs UID 1000 (Step 0).

### 10a — Pick the off-host destination

Anything you can rsync to: a NAS, another always-on machine, or a mounted
share. Over SSH, the far end needs `rsync` installed and a login the backup
can use without a password. On this server:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mileage_backup -N '' -C mileage-backup
ssh-copy-id -i ~/.ssh/mileage_backup.pub backup@nas.lan
ssh -i ~/.ssh/mileage_backup backup@nas.lan 'mkdir -p /volume1/backups/mileage-tracker && rsync --version | head -1'
```

That last command must work without a password prompt, or the timer will
fail at 3:30 am with nobody watching. It also creates the directory, which
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
REMOTE=backup@nas.lan:/volume1/backups/mileage-tracker
RSYNC_SSH="ssh -i /home/<user>/.ssh/mileage_backup"
```

The rest of the file explains the retention settings and an optional
`PING_URL`.

### 10c — Run it once by hand

```bash
bash ~/mileage-tracker/backup/backup.sh
```

It should finish with `Backup complete: <snapshot> is on <REMOTE>`. Running
it a second time straight away should say `Nothing changed`.

### 10d — Test a restore from the off-host copy

A backup you haven't restored from is a hope, not a backup:

```bash
bash ~/mileage-tracker/backup/restore.sh check
```

This pulls the newest snapshot back **from the off-host copy** into a
scratch directory, checks every file against `SHA256SUMS`, opens the
database and checks its integrity, and prints what it holds:

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

Run it once through systemd to prove the unattended path works (the SSH key,
the paths, the user):

```bash
sudo systemctl start mileage-backup@$USER.service
systemctl status mileage-backup@$USER.service      # expect "status=0/SUCCESS"
journalctl -u mileage-backup@$USER.service -n 20
systemctl list-timers 'mileage-backup*'            # next run ~03:30
```

`Persistent=true` in the timer means a run missed while the box was off
happens at the next boot.

### 10f — Know when it stops working

A failed run shows up as a failed unit (`systemctl --failed`) and in the
journal, but nobody looks there. Two options:

- Set `PING_URL` in `backup.conf` to a push monitor (Uptime Kuma, or
  healthchecks.io). The script calls it only after a verified off-host copy,
  so if the pings stop, the backups have stopped.
- Or check by hand now and then: `cat ~/mileage-backups/last-success`.

### Restoring for real

After losing the disk, rebuild the server with Steps 1–9, put
`backup.conf` back, then:

```bash
bash ~/mileage-tracker/backup/restore.sh restore latest
```

`latest` means the newest snapshot on the off-host copy. You can also name
one (`2026-10-06T103000Z`) or give a path. It runs the same checks as
`restore.sh check`, asks you to type `RESTORE`, stops both services, saves
the current database and any trip files it would overwrite to
`~/mileage-backups/pre-restore-<time>/`, restores, and starts the services
again. It never deletes trip files that aren't in the snapshot.

If your UID isn't 1000, re-run the `chown` from Step 3 afterwards so ingest
can write to `data/` again.

---

## Step 11 — Optional device notifications

Skip this step to keep notifications off. The installer adds no prompts for it.
Two optional services provide alerts: a monitor checks health every minute;
**ntfy** delivers notifications from your own server. No subscription or cloud
account is required. Its local user is a login on your server. If you already
host ntfy, skip 11a and use that server's topic URL and token.

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

### 11b — Enable the monitor

```bash
install -m 600 monitor.env.example monitor.env
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
docker compose --profile monitor up -d --build mileage-monitor
docker compose logs --tail 30 mileage-monitor
```

Look for `Monitor enabled; ntfy enabled, Graylog disabled, Prometheus disabled, Loki disabled`. Bad enabled settings fail startup
with a message naming the setting. A missing diagnostics DB means no device
has checked in yet; an unreadable DB logs unavailable and retries next minute.
The monitor mounts `/data` read-only and persists policy and delivery state in
`monitor-state`. Port 8093 is published on `BIND_ADDR` for the optional
token-protected metrics endpoint; the listener stays off unless enabled. The
`monitor` and `ntfy` profiles keep
both optional services out of ordinary installs and `install.sh --check`.
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
The opportunistic firmware heartbeat is #125; this service adds no heartbeat
and does not wake the tracker. Retiring an obsolete identity needs a future
explicit policy; all devices remain watched.

Faults need a snapshot at most **24 hours** old; GPX contact alone does not
refresh it. Old events, unknown conditions and a reboot do not imply recovery.
A resolution needs a successfully sent alert and a newer snapshot explicitly
observing that code clear. Overflow coalesces into digests of at most 3500
UTF-8 bytes; unsent work persists and is rechecked before delivery. Failed
posts retry after 60, 120, 240 seconds, and so on, capped at an hour. Delivery
is **at least once**: a crash after ntfy accepts a message but before success
is recorded can produce a duplicate.

### 11c — Check delivery and turn it off

Enter the topic token privately for an authenticated manual test:

```bash
read -rs -p 'ntfy token: ' NTFY_TEST_TOKEN; echo
curl -fsS -H "Authorization: Bearer $NTFY_TEST_TOKEN" \
  --data-binary 'Mileage tracker test notification' 'http://<host>:8092/<topic>'
curl -fsS -H "Authorization: Bearer $NTFY_TEST_TOKEN" \
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
Consistent SQLite backup/restore remains #129; the existing trip backup does
not cover these named volumes.

### 11d — Optional: send diagnostics to your own Graylog

Skip this unless you run [Graylog](https://graylog.org/) somewhere else on
your network. Nothing here installs Graylog; the monitor only sends to it.
It works with or without ntfy: Graylog gets the facts and does its own alerting.

**What it sends**, as [GELF](https://go2docs.graylog.org/current/getting_in_log_data/gelf.html)
messages with source `mileage-tracker`. Field names below are as Graylog shows
and searches them; on the wire GELF prefixes each with `_` (`kind` is `_kind`),
and Graylog removes it:

- `kind:event`: every diagnostic event the tracker uploaded, each once, at the
  time the server received it. On first enable, the whole stored history is
  sent, up to 200 messages a minute.
- `kind:condition`: a change in a fault's state on a device: `state` is
  `active`, `clear` or `unknown` (no fresh health report, so its state isn't
  known). State is per device and code, so a fault that persists across the
  tracker's deep-sleep wakes is one `active` message, not one per wake.
  E7001 `active` means no contact for `MONITOR_STALE_DAYS`
  (`days_without_contact` says how long); it turns `clear` on the next contact.
  On first enable, each device's current states are sent once as a baseline.

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
`GRAYLOG_GELF_TLS=true` for TCP. For a private CA, copy its certificate into
the monitor's state volume, e.g.
`docker compose cp graylog-ca.pem mileage-monitor:/state/graylog-ca.pem`, and
set `GRAYLOG_GELF_CA_FILE=/state/graylog-ca.pem`.

**3. Configure the monitor** in `monitor.env` (`MONITOR_ENABLED=true` is required):

```dotenv
GRAYLOG_ENABLED=true
GRAYLOG_GELF_URL=http://<graylog-host>:12201/gelf
# or: GRAYLOG_GELF_TCP=<graylog-host>:12201
```

`GRAYLOG_GELF_HEADER=<Header-Name>: <value>` adds one HTTP header, for a proxy
in front of the input. Apply with `docker compose --profile monitor up -d
mileage-monitor`, then check the log for `Graylog enabled` and, once there is
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
install -m 600 monitor.env.example monitor.env
openssl rand -hex 32
nano monitor.env
```

Set `MONITOR_ENABLED=true`, `METRICS_ENABLED=true` and put the random output in
`METRICS_TOKEN` in the private `monitor.env`. Keep the token out of shell
history, screenshots and Git. In the existing `.env`, select that file with
`MONITOR_ENV_FILE=./monitor.env`. Apply the settings:

```bash
docker compose --profile monitor up -d --build mileage-monitor
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
curl -i -H "Authorization: Bearer $METRICS_TEST_TOKEN" \
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
docker compose --profile monitor up -d --build mileage-monitor
docker compose logs --tail 30 mileage-monitor
```

The startup line should say `Loki enabled`; once it has new records, the log
reports how many lines Loki accepted. On first enable, the separate persisted
event cursor starts at rowid zero, so stored history is sent in batches of at
most 200 lines per tick without an age filter. Loki may reject history older
than its configured retention window. A permanent HTTP 4xx rejection other
than 408 or 429 skips the entire rejected batch and advances its cursors; that
history is not retried, and later batches continue. Network failures, 408, 429
and 5xx responses back off and retry without advancing the cursors.

Loki also receives one `condition_change` line when a device/code condition
changes to active, clear or unknown, including E7001 silence and recovery.
This state-change export is independent of `NOTIFY_ENABLED` and is deduplicated
across repeated check-ins and deep-sleep wakes. Successful ntfy alerts,
reminders and resolutions remain separate `alert_decision` lines; those lines
exist only for notifications ntfy accepted while Loki was enabled. With ntfy
off, diagnostic events and condition changes still flow to Loki.

Each line is JSON. Loki stream labels are only `job=mileage-tracker`, validated
`device_id`, and current-catalog `severity`; context and other fields stay in
the line. Each entry carries its real timestamp (server receipt time for
events, the monitor tick for condition changes and decisions). This needs Loki
2.4 or newer, which accepts out-of-order writes within about an hour of a
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

To pause the sink, blank `LOKI_PUSH_URL` and reapply the monitor profile. Its
cursor and pending decisions stay in `monitor-state`; when re-enabled, it
resumes from those cursors.

## Troubleshooting

**Container is `unhealthy` or restarting**

```bash
docker compose logs mileage-ingest --tail 50
docker compose logs mileage-viewer --tail 50
```

**Upload returns 500, logs show a permission error** — the `./data` ownership
problem from Step 3. Fix: `sudo chown -R 1000:1000 ~/mileage-tracker/data`
then `docker compose restart mileage-ingest`.

**Upload returns 401** — token mismatch. Verify what the container actually
sees: `docker compose exec mileage-ingest env | grep INGEST`. A trailing space
or quotes in `.env` will break it. If the server's token looks right, the
board has a different one: `AUTH_TOKEN` in `secrets.h` must match it byte
for byte, and the board only picks up a change when you flash it again
(Step 8). Regenerating the token after flashing causes exactly this.

**Trips stop arriving from the board, but the server checks out** — the
board can't reach it. The usual cause is something that changed since it
was flashed: the server's IP (check the DHCP reservation), the token in
`.env`, or the WiFi name or password. The board has all of these compiled
in, so update `secrets.h` and reflash (Step 8). The top-level README's
"Check it works" section says how to read the board's Serial Monitor
output. Trips recorded meanwhile wait on the board, but once its storage
fills (about 5 to 6 hours of driving on the legacy V1.2 LittleFS partition)
it stops recording. That estimate does not describe the Supreme microSD
target.

**Compose says `INGEST_AUTH_TOKEN` is unset** — `.env` must sit next to
`docker-compose.yml` in `~/mileage-tracker/`, not inside `mileage-ingest/`.

**Viewer container exits immediately at startup** — it fail-fasts on bad auth
config rather than starting with the login broken. The log says exactly which
value is wrong:

```bash
docker compose logs mileage-viewer --tail 20
```

Usual causes: `viewer.env` not created, or the password hash lost its `$`
characters to shell expansion (fix by single-quoting it).

**Locked out of the viewer** — 5 bad passwords locks the IP for 15 minutes.
Clear it:

```bash
docker compose exec mileage-viewer python -c \
  "import sqlite3;c=sqlite3.connect('/db/annotations.db');c.execute('DELETE FROM login_attempts');c.commit()"
```

**Forgot the viewer password** — regenerate a hash (Step 4b), replace it in
`viewer.env`, then `docker compose up -d mileage-viewer`. Every browser that
was logged in has to sign in again; your annotations are untouched.

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
curl -X POST http://<host>:8090/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/gpx+xml" \
  -H "X-Device-Id: tbeam-01" \
  -H "X-Trip-Id: ${TRIP#*/}" \
  --data-binary @data/$TRIP.gpx
```

`X-Device-Id` is the firmware's `DEVICE_ID`, and `X-Trip-Id` keeps the
file's name. The reply's `stored_as` should be `$TRIP.gpx`; the trip is then
back in the list with its annotations. Ingest rebuilds a damaged or missing
summary only when the GPX it already holds matches the upload byte for byte,
since that's what proves it's the same trip.

If the warning says there's no GPX either, put the GPX back from a backup
snapshot first (Step 10), then send it as above. Without it, ingest answers
409 (`trip ID has a damaged summary and no GPX to match`).

```bash
ls ~/mileage-backups/snapshots/*/data/$TRIP.gpx      # the snapshots that have it
cp -p ~/mileage-backups/snapshots/<time>/data/$TRIP.gpx data/$TRIP.gpx
```

**Port already in use** — `sudo ss -tlnp | grep -E '8090|8091|8092|8093'`, then change
the host-side port mapping in `docker-compose.yml`.

**Rebuild after code changes:**

```bash
cd ~/mileage-tracker
docker compose up -d --build
```
