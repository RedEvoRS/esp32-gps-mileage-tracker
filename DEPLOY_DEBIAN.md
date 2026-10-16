# Debian Deployment Runbook — Mileage Tracker Server

Deploys both services from `server/`:

| Service | Port | Auth | Purpose |
|---|---|---|---|
| `mileage-ingest` | 8090 | Bearer token | Accepts GPX uploads from the T-Beam |
| `mileage-viewer` | 8091 | Session login | Trip list, map audit, annotate, CSV/XLSX export |

This is the server half of the project. The top-level `README.md` gives
the whole setup in order (server, then the board, then the car); come
back to it after Step 8.

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
`sample.gpx`, `backup/`, `mileage-ingest/`, and `mileage-viewer/` (with `static/` holding
`leaflet.js`, `leaflet.css`, `gpx.js` — those are vendored, so the viewer
works with no internet except OSM map tiles).

> **Watch for:** `scp -r` on Windows can silently skip dotfiles in some
> OpenSSH builds. Confirm `.env.example` made it: `ls -a ~/mileage-tracker`.
> If missing, copy it explicitly:
> `scp server/.env.example <user>@<host>:~/mileage-tracker/`
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
ever sees it. A `ufw allow from 192.168.1.0/24 ...` rule for 8090/8091 has
no effect, and neither does a `ufw deny`: the ports stay open on every
interface the box has (a VPN, a second NIC). UFW still governs
services running directly on the host, such as SSH; it just has no say
over these two ports.

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
`192.168.1.10:8091->8091/tcp`. If it still says `0.0.0.0`, Compose didn't
pick up `BIND_ADDR`; check the spelling and that `.env` is next to
`docker-compose.yml`.

Confirm it from the server:

```bash
curl http://192.168.1.10:8090/health    # expect {"service":"mileage-ingest","status":"ok"}
curl http://localhost:8090/health       # expect "Connection refused"
```

`localhost` stops answering once the ports are bound to the LAN address.
That's expected. Use the LAN IP for any curl tests from here on.

---

## Step 8 — Point the firmware at it

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

Then flash `firmware/04_trip_logger`, check it works and put it in the car.
The top-level README covers all three under "Setting it up", steps 3 to 5:
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

### 10e — Schedule it

```bash
sudo cp ~/mileage-tracker/backup/mileage-backup@.service \
        ~/mileage-tracker/backup/mileage-backup@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mileage-backup@$USER.timer
```

The unit runs `/home/<user>/mileage-tracker/backup/backup.sh` as `<user>`.
If the server files live somewhere else, edit `ExecStart` in the
`.service` file first.

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
fills (about 5 to 6 hours of driving) it stops recording.

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

**Port already in use** — `sudo ss -tlnp | grep -E '8090|8091'`, then change
the host-side port mapping in `docker-compose.yml`.

**Rebuild after code changes:**

```bash
cd ~/mileage-tracker
docker compose up -d --build
```
