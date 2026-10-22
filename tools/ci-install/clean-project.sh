#!/usr/bin/env bash
# Remove only this install's Compose project, including optional profiles.
set -euo pipefail
[[ $# == 1 ]] || { echo 'Usage: clean-project.sh INSTALL_DIR' >&2; exit 2; }
dir=$(cd -- "$1" && pwd)
test -f "$dir/docker-compose.yml"
# Compose scopes containers/networks/volumes by project; --rmi local removes
# only its uncustomized service images, never an unrelated project's images.
docker compose --project-directory "$dir" --profile '*' down \
    --volumes --rmi local --remove-orphans
