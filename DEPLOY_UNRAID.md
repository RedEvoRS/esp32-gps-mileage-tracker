# Unraid Deployment Runbook — Mileage Tracker Server

Runs the same Docker services as the [Debian deployment](DEPLOY_DEBIAN.md).
Ingest receives the tracker's trips. The viewer lets you review, annotate
and export them. Optional services provide notifications and metrics.
The [top-level README](README.md) explains the board and the full setup.

**Supported route: Unraid 7.3+ within the 7.x series. Unraid 8.x is not
supported yet.** Unraid support has been tested only with fakes so far.
Installation, array stop/start, reboot recovery, nightly backups, the
firmware build and a Debian migration still need checks on a real Unraid host.
A successful installer check does not establish those results.

Replace placeholders before running commands: `<host>` is the server's
address, `<pool>` is your SSD pool name, `<name>` is a disk or share name,
and `<user>` is a login on another machine. Commands run in the Unraid
root SSH session unless a step says otherwise. Use your actual absolute
install path wherever an example shows `/mnt/<pool>/appdata/mileage-tracker`.

## Step 0 — Prepare storage, Docker and SSH

1. Start the array in the Unraid web UI. Enable Docker in **Settings →
   Docker**. Unraid manages Docker itself; this installer does not install
   or start the Docker daemon.
2. Install the **Compose Manager Plus** plugin. The installer needs
   `docker compose` version 2 or newer, including newer major versions.
3. Use an SSD pool for appdata. In **Shares → appdata**, set **Primary
   storage** to that pool and **Secondary storage** to **None**. This
   keeps new application data on the pool, without an array overflow or
   Mover destination. Changing the settings does not move old files already
   on other storage. See [Unraid's share settings](https://docs.unraid.net/go/shares/).
4. Use a direct path such as `/mnt/<pool>/appdata/mileage-tracker`, rather
   than `/mnt/user/appdata/mileage-tracker`. A direct path avoids the user
   share layer for SQLite database access and makes the backing filesystem
   explicit. The installer warns about `/mnt/user`; it does not reject it.
   Keep the pool mounted. Do not put the install under `/root`, `/tmp` or
   `/boot`: the first two are volatile, and a USB boot filesystem does not
   provide the private Unix permissions these files need.
5. Enable SSH in **Settings → Management Access**. Use the `root` login.
   For the later PC flash step, arrange root SSH key access from that PC
   and confirm it works. Keep the private key on the PC.

From your PC's PowerShell or terminal:

```bash
ssh root@<host>
```

In that SSH session:

```bash
docker info
docker compose version
```

Both commands must work before setup. There is no `sudo`, apt or systemd
setup on this route. Ingest, viewer and monitor run as the non-root application
user, with user/group IDs (UID/GID) **1000:1000**. The optional bundled ntfy
runs as root inside its container and keeps its state in the `ntfy-state`
volume. Root on the host can maintain their private files.
The installer creates `data/` with that ownership and mode 700. On an existing
folder, it checks the owner UID and permissions.

Allow space on both the install pool and Docker's storage. Setup checks
them separately: a Docker image disk can fill while the pool still has
space. Allow at least 10 GiB free for the first firmware build.

## Step 1 — Install with `setup.sh`

Read the [download and trust note](README.md#setting-it-up). In the root
SSH session, download the release helper, inspect it, then run it:

```bash
curl -fsSL https://github.com/RedEvoRS/esp32-gps-mileage-tracker/releases/latest/download/setup.sh -o setup.sh
less setup.sh
bash setup.sh --dir /mnt/<pool>/appdata/mileage-tracker
```

Without `--dir`, Unraid setup uses `/mnt/cache/appdata/mileage-tracker`
only when `/mnt/cache` is mounted. A pool with another name needs `--dir`.
Use a simple path with letters, digits, underscores, dots, hyphens and
slashes; spaces are refused. Setup extracts on the target disk, verifies
the release checksum, and copies the server files directly into that folder.
It preserves private configuration, trip data, backup keys, backup logs and
state on updates. Hand edits to shipped files such as `ntfy/server.yml` are
replaced on update; use the `.env` settings for ntfy's proxy configuration
described in [Debian Step 7](DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager).
Rerun the release helper with the same `--dir` to update.

Setup runs `install.sh`. Answer its questions about the LAN bind address,
display timezone, viewer admin, optional everyday login, backups and optional
outputs. The viewer login is separate from the root SSH login. Choose the
IANA timezone for where you drive; `DISPLAY_TZ` controls displayed trip times
and the month folders, independently of the host's cron timezone.

The installer builds and starts the services, tests an upload and the viewer,
then removes its test trip. Setup offers a personal firmware build afterward.
A failure stops the process; fix the reported problem and rerun.
`setup.sh --yes` skips the firmware builder and wrapper prompts. It still
needs the inner installer's answers through `INSTALL_*` presets. The
[shared installer overview](DEPLOY_DEBIAN.md#the-quick-way-setupsh) explains
those presets. On Unraid, skip its Debian sudo, UID 1000 login and network
wait instructions; root can run backups. `INSTALL_BACKUP_DIR` is described
in Step 4 below.

**No host Python installation is needed.** The installer and builder use
host `python3` when present. Otherwise they run disposable Python containers
using the first `FROM` image in `mileage-ingest/Dockerfile`. The first use
pulls that image if it is not cached, so Docker needs download access.
The Python process itself runs with no network and only the mounts needed
for that step. The firmware toolchain is a separate, larger download.

## Step 2 — Check the install and its network exposure

```bash
cd /mnt/<pool>/appdata/mileage-tracker
bash install.sh --check
```

This leaves configuration unchanged, but it uploads and removes a temporary
trip and can test the admin login. It also performs a restore test when
backups are configured. It is not a read-only command.

Unraid-specific results mean:

- **Docker is answering / Compose version:** the daemon is usable now.
  The installer also checks free space at Docker's reported storage path.
- **Docker is enabled in Settings → Docker:** `DOCKER_ENABLED="yes"` is
  present in `/boot/config/docker.cfg`. This checks the setting, not a boot.
- **Containers start again with Docker:** the existing containers have
  restart policy `always`. Docker starts after networking and the array;
  no Debian wait-online service is used.
- **Persistent backup cron entries are loaded:** the generated file matches
  this install, and both commands appear in `crontab -l`. Backup checks also
  examine recorded mounts, the latest attempt and the last successful run.
  An older success does not hide a failed latest attempt. A success more
  than two days old is reported as a problem.
- **“boot recovery remains unverified”:** even if the preceding checks pass,
  no actual array stop/start or reboot has been tested by this command.
  At a suitable time, test both from the Unraid UI, then check the services,
  loaded cron entries, backup key and persistent log again.

Unraid's own web UI normally uses **80/443**. This stack uses:

- **8090:** ingest, authenticated uploads from the tracker.
- **8091:** viewer, with a session login.
- **8092:** optional self-hosted ntfy notifications.
- **8093:** optional authenticated monitor metrics.

These app ports do not conflict with 80/443. Unraid has no UFW-style host
firewall for this setup. Choose the server's LAN IP for `BIND_ADDR` in
`.env`; an empty `BIND_ADDR=` publishes on every interface. `all` is only an
answer to the installer's prompt, which writes that empty value. Do not put
`BIND_ADDR=all` in `.env`: Compose rejects it as an invalid IP address.
Binding is the LAN-only control here. It does not replace router rules between
networks. Do not forward these ports from the internet.

For HTTPS, follow [Debian Step 7's reverse-proxy section](DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager).
The viewer proxy settings are the same. If the proxy takes host ports
80/443, move the Unraid web UI to other ports in Management Access first.
Keep the tracker's ingest address reachable; a viewer proxy does not change
the firmware's plain HTTP upload connection.

For viewer use and account management, follow the
[viewer guide](server/README.md#viewer--export-ui). For notifications and
external dashboards, rerun `bash install.sh` from this install directory as
root. At **Notifications**, answer **no** to **Keep them as they are?** if
outputs are already configured, then choose the outputs you want. Keep the
other existing settings when asked. The installer stamps and builds the monitor
using a disposable Python container when host `python3` is absent.
[Debian Step 11](DEPLOY_DEBIAN.md#step-11--optional-device-notifications)
explains the outputs and dashboard setup, but its manual build commands use
host `python3`: that fallback works only inside `install.sh`, not in commands
pasted into the shell. Its systemd, sudo and Debian firewall instructions do
not apply on Unraid.

## Step 3 — Understand restarts

The Unraid installer writes `RESTART_POLICY=always` into `.env`. All four
Compose services use it. Without the setting, Compose defaults to
`unless-stopped`, which remains the Debian default.

Stopping the Unraid array stops the containers and Docker. With `always`,
Docker should bring existing containers back when the array and daemon
start again. With `unless-stopped`, the stopped containers stay stopped.
This is why the policy matters. See [Docker's restart policy rules](https://docs.docker.com/engine/containers/start-containers-automatically/).
Actual recovery on Unraid still needs the host checks above.

`docker compose stop` leaves the containers in place. It stops them now,
but `always` allows them to return after the next Docker restart, including
an array start. To keep this stack down, run `docker compose down` from its
install directory, including any enabled profiles. That removes containers;
it keeps trip data and named volumes unless you explicitly request volume
removal. Restart with `docker compose up -d` and the profiles you use.
Never add `-v` to a routine shutdown: it removes named volumes, including
the viewer database. Disabling an optional profile also requires removing
its existing container; removing a profile setting alone does not do that.

## Step 4 — Set up and check backups

Backups need two locations: `BACKUP_DIR` holds local snapshots, and `REMOTE`
receives their verified copy. Both are required on this route. `REMOTE`
can be an SSH target such as `<user>@<host>:/backups/mileage-tracker`, or
a mounted disk/share folder. See the [shared backup explanation](DEPLOY_DEBIAN.md#step-10--backups)
for snapshot contents, retention, destination markers and restore details.
Use this section for Unraid paths and scheduling.

Mount the destinations first. Unassigned Devices commonly mounts local
disks at `/mnt/disks/<name>` and network shares at `/mnt/remotes/<name>`.
Enable the mount's automatic mounting if it should be available after boot.
Choose a filesystem that supports the snapshot hard links and private file
permissions. A separate share on this same Unraid box is **not off-host**.
It cannot protect against losing the whole machine. The installer warns
about a local `REMOTE`, and about destinations on the same filesystem as
`data/`; it does not refuse them for that reason.

The installer asks for an absolute local snapshot path. There is no default
under `/root`. To supply it in advance, run setup with, for example:

```bash
INSTALL_BACKUP_DIR=/mnt/disks/<name>/mileage-backups bash setup.sh --dir /mnt/<pool>/appdata/mileage-tracker
```

This answers only the local folder question. You must still choose the
`REMOTE` destination and other installer answers. Do not set the two paths
to the same folder. The installer records settings in `backup/backup.conf`,
including `BACKUP_MOUNT` and, for a local `REMOTE`, `REMOTE_MOUNT`.
Before backup or restore, each recorded mount must be mounted, and its
folder must resolve onto that mount. If a disk or share is missing, the
backup refuses before creating snapshot folders or copying files. This
prevents writing into an empty mount folder on the RAM root filesystem.
Do not remove the guards to get past a missing disk.

`backup.conf` is Bash code sourced by root. Keep it root-owned and not
writable by group or others; mode **600** is recommended, and 644 is also
accepted. After copying a config from Debian, use:

```bash
chown root:root backup/backup.conf
chmod 600 backup/backup.conf
```

For an SSH destination, the installer creates a dedicated key and
`known_hosts` in the persistent `backup/ssh/` directory (mode 700).
`RSYNC_SSH` selects that key and known-hosts file. Confirm the destination's
host key and enter its password privately if asked. Do not rely on
`/root/.ssh` for this backup key: `/root` is rebuilt at boot. Never put
private keys or snapshots on the USB boot disk.

### Schedule, log and status

The installer runs one backup through `backup/scheduled.sh`, tests a
restore, then installs the persistent cron file:
`/boot/config/plugins/dynamix/mileage-tracker-backup.cron`.
For an install at the example pool path, its contents are:

```cron
# Mileage tracker backup. Written by install.sh; rerun it to change.
30 3 * * * /bin/bash /mnt/<pool>/appdata/mileage-tracker/backup/scheduled.sh --delay 900
15 * * * * /bin/bash /mnt/<pool>/appdata/mileage-tracker/backup/scheduled.sh --if-overdue 26
```

There are five time fields and then the command, with **no user field**.
Unraid runs them as root. The nightly run starts at **03:30 in the Unraid
host's timezone**, then waits a random zero to 15 minutes. At 15 minutes
past each hour, the catch-up command runs only if the last success is
missing, unreadable, invalid or more than **26 hours** old. It does not
replay every missed night. Overlapping backups are locked out.

The wrapper gives `backup.sh` a **six-hour timeout**, with `nice -n 10`
and `ionice -c3` when available. Timeout status is 124. This limit applies
to the wrapper, not a direct manual `backup.sh` run.

The installer loads the file with `/usr/local/sbin/update_cron`. Inspect
the saved and loaded entries with:

```bash
cat /boot/config/plugins/dynamix/mileage-tracker-backup.cron
crontab -l
```

Rerun `install.sh` to repair the file or update its install path. If you
deliberately edit the times, reload with `/usr/local/sbin/update_cron`;
`install.sh --check` will report that the file differs, and a later install
run rewrites the standard schedule. Editing only `crontab` is not persistent.

Run a backup and inspect its status from the install directory:

```bash
bash backup/scheduled.sh
bash backup/scheduled.sh --status
tail -n 30 backup/logs/backup.log
```

The private persistent log is `backup/logs/backup.log` (mode 600). It rotates
after exceeding 1 MiB and keeps `.1` through `.3`. State lives in
`backup/state/last-attempt`, with UTC start, end and exit status. These
directories are mode 700. `--status` prints `last_attempt_start`,
`last_attempt_end`, `last_attempt_status`, `last_success` and the absolute
log path. Empty values can mean it has never run. Status 0 means success;
`running` has no final result yet. Reading status itself exits 0, so inspect
the fields. `last_success` comes from `BACKUP_DIR/last-success` and may be
empty when its disk is absent. A direct `backup.sh` run does not update
the wrapper's last-attempt record or persistent log.

## Step 5 — Build on Unraid, flash from the PC

The Windows helper's first-install and `-Cleanup` flows, and the macOS/Linux
`flash.sh` helper, still need checks on a real board. Uploads from internal
LittleFS flash without the SD card also remain untested on the Supreme;
use the FAT32 microSD card for this route.

Accept the build offered by setup, or run it later:

```bash
bash /mnt/<pool>/appdata/mileage-tracker/firmware-builder/build.sh --install-dir /mnt/<pool>/appdata/mileage-tracker
```

The array and Docker must be running. The builder checks Buildx/BuildKit
and `linux/amd64` support. Its high-water query and compile helpers run as
**1000:1000**, even though you invoke it as root. Finished firmware is private
because it contains WiFi credentials and the upload token. See the
[builder's Unraid notes](server/firmware-builder/README.md#on-unraid).

Follow [README Step 2](README.md#step-2--flash-the-tracker) for the PC flash
helper, factory backup, first-install erase, and cleanup after the first
diagnostics snapshot. Use setup's printed commands for the matching release.
The Unraid difference is the SSH target and the **absolute install path**.
After downloading the helper, run on the USB-connected PC:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\flash.ps1 -Server 'root@<host>' -InstallDir '/mnt/<pool>/appdata/mileage-tracker'
```

Or in macOS/Linux Terminal:

```bash
bash flash.sh --server 'root@<host>' --install-dir /mnt/<pool>/appdata/mileage-tracker
```

Do not use the helpers' default `~/mileage-tracker`; root's home is not the
Unraid install. For a previously used tracker, follow the update flow and
preserve its saved settings and trips. Follow [README Step 3](README.md#step-3--first-drive-and-device-health)
to confirm uploads and a fresh Device health snapshot.

## Moving an existing Debian install to Unraid

1. **Plan the address and preserve settings.** Record the current trip totals
   and check annotations and Device health. Keep the old server's LAN IP
   for Unraid, or plan a firmware rebuild/reflash with the new address
   (`secrets.h` for a source build). Never run both hosts at the same IP.
   Keep the existing ingest token too. Privately save `.env`, `viewer.env`,
   optional `monitor.env`, backup configuration and required keys. Snapshots
   exclude those credentials and the ntfy volume; preserve ntfy separately
   if used. The [Debian restore notes](DEPLOY_DEBIAN.md#restoring-for-real)
   explain the consequences of losing them.
2. **Freeze Debian writes and take the final backup.** Pick a quiet time and
   stop ingest, viewer and any running monitor with `docker compose stop`
   from the old install directory. This prevents new uploads or annotations
   on Debian while you move. Keep the tracker from uploading (unplug it from
   the car's USB port, or don't drive), and do not annotate on Unraid, until
   the restore in step 5 finishes. Setup starts the new services before the restore; any diagnostics
   or annotations received there would be replaced by the saved databases.
   As the Debian UID 1000 login, run:

   ```bash
   bash ~/mileage-tracker/backup/backup.sh
   bash ~/mileage-tracker/backup/restore.sh check
   ```

   Backup helpers can read the stopped services' volumes. Confirm success
   and note the chosen snapshot. Keep Debian stopped until the move is
   accepted; the tracker can retain queued trips during the outage.
3. **Install on Unraid using Steps 0–2.** Before the first setup, create the
   chosen install directory and privately transfer the old `.env` and
   `viewer.env` into it. Keep them root-owned and mode 600. Edit the old
   `BIND_ADDR` and any `VIEWER_BIND_ADDR` for the new host if needed:
   `VIEWER_BIND_ADDR` wins over `BIND_ADDR` for the viewer's port, so an old
   LAN IP there prevents the viewer from starting. Keep loopback only if
   the new proxy runs on the host or uses host networking. For a proxy,
   change its forwarding destination to the new host, rediscover its peer
   for `VIEWER_TRUSTED_PROXIES` using [Debian Step 7](DEPLOY_DEBIAN.md#behind-a-reverse-proxy-caddy-or-nginx-proxy-manager),
   and test an HTTPS login before accepting the move. Setup preserves
   these files and writes `RESTART_POLICY=always`. Keep the old ingest token
   and `DISPLAY_TZ` so existing firmware and month grouping remain consistent.
   Restore optional output settings deliberately; avoid enabling the monitor
   until its old state is restored. Skip the firmware build if no firmware
   settings need to change.
4. **Prepare restore storage and copy the snapshot.** Configure Unraid
   backups as in Step 4, with mounted persistent destinations. If reusing
   a Debian `backup.conf`, change its paths, ownership, mount guards and
   SSH settings for Unraid. Do not copy a `/home/<user>` path unchanged.
   The new install's first backup may be empty; name the final Debian
   snapshot explicitly rather than relying on `latest`.

   You can restore directly from its off-host path. Alternatively, copy
   the complete final snapshot into a separate mounted folder. For example,
   run this **on Debian**, as its UID 1000 login, while it remains stopped:

   ```bash
   rsync -a ~/mileage-backups/latest/ root@<host>:/mnt/disks/<name>/debian-snapshot/
   ```

   Here `<host>` is Unraid. Replace the source if Debian used another
   `BACKUP_DIR`. Confirm `/mnt/disks/<name>` is mounted on Unraid before
   copying; a manual rsync does not use the backup mount guards. Keep the
   entire snapshot, including `SHA256SUMS`, databases and `data/`.
5. **Check, then restore on Unraid.** From the new install directory:

   ```bash
   bash backup/restore.sh check /mnt/disks/<name>/debian-snapshot
   bash backup/restore.sh restore /mnt/disks/<name>/debian-snapshot
   stat -c '%u:%g %a' data
   ```

   The restore checks every file and each saved database before asking you
   to type `RESTORE`. It stops database writers, saves current databases and
   any replaced trip files under `BACKUP_DIR/pre-restore-...`, restores, then
   starts services again. It leaves trips absent from the snapshot alone.
   Missing optional databases are reported and left unchanged. On an install
   prepared by the installer, `data/` must report **1000:1000 700**; root
   restore uses that directory's owner for restored trip files, with
   directories 700 and files 600. If ownership differs, stop and resolve it
   before accepting the migration. Do not give trip ownership to root.
6. **Verify before retiring Debian.** Compare trip counts, mileage totals,
   annotations, trip removals and closed-year state with the old server.
   Everyday accounts return to their snapshot state; redo later account
   changes if needed. After the restore, re-enable any optional outputs using
   Step 2 above. Check Device health history, then plug the tracker back in
   and check for a fresh contact after its next drive. If the address or
   token changed, rebuild firmware and flash it from the PC using the update
   flow, with no first-install erase. Run
   `bash install.sh --check`, a scheduled backup and a restore test. Arrange
   the real array restart/reboot checks separately. Once satisfied, disable
   the old timer **on Debian**, replacing `<user>` with its login:

   ```bash
   sudo systemctl disable --now mileage-backup@<user>.timer
   ```

   Keep the final snapshot and old server recovery material until the new
   backups and restore have been verified. Do not delete the old data as
   part of testing the migration.

## Troubleshooting

- **`docker compose` is missing:** install Compose Manager Plus and rerun
  `docker compose version`. Version 1 is unsupported. Do not install Debian
  packages on Unraid.
- **Docker is not answering:** start the array, enable Docker in Settings →
  Docker, then retry `docker info`. Check Docker storage space separately
  from the appdata pool.
- **Cron file exists but the jobs are not loaded:** run
  `/usr/local/sbin/update_cron`, inspect `crontab -l`, then rerun
  `bash install.sh --check`. Rerun `install.sh` if the generated path or
  file contents are wrong. Do not add a `root` field to the cron lines.
- **Backup refuses a destination:** confirm the disk or network share is
  mounted at the recorded path. Check `BACKUP_MOUNT` / `REMOTE_MOUNT` in
  `backup.conf` and the persistent backup log. A directory existing is not
  proof of a mount. Also check root ownership and permissions on the config.
- **Containers stay down after a reboot or array start:** confirm the array
  and Docker started, and check `RESTART_POLICY=always` in `.env`. Rerun
  `install.sh`, then `docker compose up -d` with any enabled profiles to
  apply it to the containers. Run `install.sh --check` to inspect the actual
  policies. Editing `.env` alone does not recreate existing containers.
- **Python container cannot start:** the first use may need an image pull.
  Check Docker download access and space. Installing host Python is optional.
