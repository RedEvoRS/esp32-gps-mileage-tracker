#!/usr/bin/env bash
# Run only in the disposable systemd/Docker Debian container, never on a host.
set -euo pipefail
test "$(cat /etc/hostname)" = two-piece-ci
# shellcheck source=/dev/null
test "$(. /etc/os-release; echo "$ID:$VERSION_ID")" = debian:13
trap 'rm -f /cache/builder.tar.gz.partial' EXIT
for _ in {1..60}; do
    if systemctl is-active --quiet docker && docker info >/dev/null 2>&1; then break; fi
    sleep 1
done
docker info --format 'Nested Docker: {{.ServerVersion}} / {{.Driver}}'
install_dir=$(getent passwd tester | cut -d: -f6)/mileage-tracker
bundle=$(find /assets -maxdepth 1 -name 'mileage-tracker-*.tar.gz' -print -quit)
test -n "$bundle"
lan_addr=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
export INSTALL_BIND_ADDR=$lan_addr INSTALL_DISPLAY_TZ=America/Chicago
export INSTALL_VIEWER_USER=tester INSTALL_VIEWER_PASSWORD=correct-horse-battery
export INSTALL_ADD_EDITOR=y INSTALL_EDITOR_USER=helper INSTALL_EDITOR_PASSWORD=staple-battery-horse
export INSTALL_WAIT_ONLINE=y INSTALL_BACKUPS=y INSTALL_BACKUP_REMOTE=/srv/offhost
export INSTALL_SAME_DISK=y INSTALL_PING_URL='' INSTALL_OUTPUTS=none
as_user() { sudo -E -u tester -H "$@"; }
setup() { as_user bash /assets/setup.sh --bundle "$bundle" --dir "$install_dir" --yes < /dev/null; }
check() { as_user bash "$install_dir/install.sh" --check < /dev/null; }
# The everyday login exists exactly once: made by the first install, kept by
# the rerun (whose preset asks for it again and gets "already exists").
everyday_login_kept() {
    local list
    list=$(docker exec mileage-viewer python manage_users.py list)
    test "$(grep -cFx $'helper\tenabled' <<< "$list")" = 1
}
test ! -e "$install_dir"
setup
# install.sh already tests upload, viewer login/exports, and scheduled backup.
test -n "$(ls /srv/offhost/snapshots)"
systemctl is-enabled "mileage-backup@$(id -un tester).timer"
test -z "$(find "$install_dir" -name .git -print -quit)"
test -f "$install_dir/firmware-src/firmware/COMMIT"
everyday_login_kept

# Exercise all private exclusions with real files. Monitor stays disabled.
as_user cp "$install_dir/monitor.env.example" "$install_dir/monitor.env"
as_user mkdir -p "$install_dir/firmware-out"
as_user touch "$install_dir/firmware-out/ci-sentinel" "$install_dir/data/ci-sentinel"
private=(.env viewer.env monitor.env backup/backup.conf data/ci-sentinel firmware-out/ci-sentinel)
private_hashes() { (cd "$install_dir"; sha256sum "${private[@]}"); }
before=$(private_hashes)
started=$(docker inspect -f '{{.State.StartedAt}}' mileage-ingest mileage-viewer)
export INSTALL_KEEP_LOGIN=y INSTALL_KEEP_OUTPUTS=y
# A changed public file must be replaced by the bundle's original source.
# The child shell expands its positional parameter.
# shellcheck disable=SC2016
as_user bash -c 'echo "# CI update marker" >> "$1/stamp_version.py"' test "$install_dir"
setup
if grep -q 'CI update marker' "$install_dir/stamp_version.py"; then exit 1; fi
test "$before" = "$(private_hashes)"
test "$started" = "$(docker inspect -f '{{.State.StartedAt}}' mileage-ingest mileage-viewer)"
everyday_login_kept
check
expected="$(tr -d '\r\n' < "$install_dir/VERSION")+$(cut -c1-10 "$install_dir/COMMIT")"
for port in 8090 8091; do
    test "$(curl -fsS "http://$lan_addr:$port/health" | jq -r .version)" = "$expected"
done

# Back up an accepted synthetic trip, damage it, then restore both files.
token=$(sed -n 's/^INGEST_AUTH_TOKEN=//p' "$install_dir/.env" | tail -n 1)
curl -fsS -o /dev/null -H @<(printf 'Authorization: Bearer %s\n' "$token") \
    -H 'Content-Type: application/gpx+xml' -H 'X-Device-Id: ci-device' \
    -H 'X-Trip-Id: ci-backup' --data-binary @"$install_dir/sample.gpx" "http://$lan_addr:8090/upload"
unset token
trip=$(find "$install_dir/data" -name ci-backup.gpx -print -quit)
test -n "$trip"
trip=${trip%.gpx}
before=$(sha256sum "$trip.gpx" "$trip.json")
as_user bash "$install_dir/backup/backup.sh"
printf 'damaged\n' > "$trip.gpx"
rm "$trip.json"
as_user bash "$install_dir/backup/restore.sh" check latest
as_user bash "$install_dir/backup/restore.sh" restore latest --yes
test "$before" = "$(sha256sum "$trip.gpx" "$trip.json")"
check
echo 'Bundle install/update, private preservation, versions and populated restore passed.'

if [[ ${1:-} == builder ]]; then
    # All server acceptance checks are complete. Release their containers,
    # volumes, images and download/build cache before the toolchain build.
    systemctl disable --now "mileage-backup@$(id -un tester).timer"
    systemctl stop "mileage-backup@$(id -un tester).service"
    as_user bash /tests/tools/ci-install/clean-project.sh "$install_dir"
    docker builder prune --all --force
    bash /tests/tools/ci-install/disk.sh before-builder
    # Preservation is already proven. Publishing reserves this directory for
    # current/cleanup only, so remove the synthetic marker and its empty dir.
    as_user rm "$install_dir/firmware-out/ci-sentinel"
    as_user rmdir "$install_dir/firmware-out"
    # The cached image contains public toolchain files only, never firmware.
    export TRACKER_BUILDER_IMAGE=two-piece-ci-builder:cache
    if [[ -f /cache/builder.tar.gz ]]; then
        docker load -i /cache/builder.tar.gz
        rm /cache/builder.tar.gz
        bash /tests/tools/ci-install/disk.sh after-cache-load
    fi
    export TRACKER_ACCEPT_DOWNLOAD=yes TRACKER_WIFI_SSID=ci-network
    export TRACKER_WIFI_PASSWORD=ci-password TRACKER_DEVICE_ID=ci-device
    export TRACKER_SERVER_HOST=tracker.invalid TRACKER_SERVER_PORT=8090
    export TRACKER_NEW_BOARD=no TRACKER_FRESH_SD=no
    as_user bash "$install_dir/firmware-builder/build.sh" --non-interactive --install-dir "$install_dir"
    # BuildKit's download mount and intermediate records are no longer
    # needed; retain only the tagged public image before archiving it.
    docker builder prune --all --force
    bash /tests/tools/ci-install/disk.sh after-builder
    python3 /tests/tools/compare_firmware.py "$install_dir" /reference
    # Run the flash script's actual validator and offset selector, no esptool.
    sed -n '/# BEGIN PURE HELPERS/,/# END PURE HELPERS/p' /assets/flash.sh > /tmp/flash-helpers.sh
    # shellcheck source=/dev/null
    source /tmp/flash-helpers.sh
    flash_validate "$install_dir/firmware-out/current" true
    test "$(flash_parts "$install_dir/firmware-out/current" false | cut -f1)" = app
    echo 'Builder normal manifest and app-only flash selection passed.'
    if [[ ${SAVE_BUILDER_CACHE:-true} == true ]]; then
        # Stream compression: never hold an uncompressed tar beside the
        # image. pipefail prevents publishing a partial failed save.
        docker save "$TRACKER_BUILDER_IMAGE" | gzip -1 > /cache/builder.tar.gz.partial
        mv /cache/builder.tar.gz.partial /cache/builder.tar.gz
        bash /tests/tools/ci-install/disk.sh after-cache-save
    fi
    docker image rm "$TRACKER_BUILDER_IMAGE"
    docker builder prune --all --force
fi
