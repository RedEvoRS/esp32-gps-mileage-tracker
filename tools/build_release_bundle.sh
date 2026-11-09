#!/usr/bin/env bash
# Shared by release publication and the installer integration test.
# Usage: bash tools/build_release_bundle.sh TAG EMPTY_OUTPUT_DIR [COMMIT]
set -euo pipefail
[[ $# -ge 2 && $# -le 3 ]] || { echo 'Usage: build_release_bundle.sh TAG OUTPUT_DIR [COMMIT]' >&2; exit 2; }
RELEASE_TAG=$1
[[ $RELEASE_TAG =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]] || { echo 'Invalid release tag' >&2; exit 2; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
commit=$(git rev-parse --verify "${3:-HEAD}^{commit}")
test "$(git rev-parse HEAD)" = "$commit"
assets=$(mkdir -p -- "$2" && cd -- "$2" && pwd)
test -z "$(ls -A "$assets")" || { echo "Asset directory must be empty" >&2; exit 1; }
prefix="mileage-tracker-$RELEASE_TAG"
bundle="$assets/$prefix.tar.gz"
git archive --format=tar.gz --prefix="$prefix/" --output="$bundle" "$commit" -- \
  server/ firmware/04_trip_logger/ firmware/VERSION firmware/COMMIT \
  firmware/secrets.h.example tools/stamp_firmware_version.py LICENSE SECURITY.md
tar -tzf "$bundle"
python3 - "$bundle" "$prefix" "$commit" <<'PY'
import re
import sys
import tarfile

bundle, prefix, commit = sys.argv[1:]
with tarfile.open(bundle, "r:gz") as archive:
    for path in ("server/COMMIT", "firmware/COMMIT"):
        value = archive.extractfile(f"{prefix}/{path}").read().decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", value) or value != commit:
            raise SystemExit(f"{path} is not the expanded tag commit")
        print(f"{path}: {value}")
PY
for script in setup.sh flash.sh flash.ps1; do
  if [ -f "install/$script" ]; then
    python3 - "install/$script" "$assets/$script" "$RELEASE_TAG" <<'PY'
from pathlib import Path
import sys

source, target, tag = sys.argv[1:]
Path(target).write_bytes(Path(source).read_bytes().replace(b"@RELEASE_TAG@", tag.encode("ascii")))
PY
  else
    echo "::notice::install/$script absent at this tag; skipping asset"
  fi
done
(
  cd "$assets"
  sha256sum -- * > SHA256SUMS
  sha256sum --check SHA256SUMS
)
extracted=$(mktemp -d)
trap 'rm -rf -- "$extracted"' EXIT
tar -xzf "$bundle" -C "$extracted"
(
  cd "$extracted/$prefix"
  test ! -e .git
  # Ship the lock inputs and regeneration helper with the compiled locks.
  test -f server/update_python_dependencies.sh
  for service in mileage-ingest mileage-viewer; do
    test -f "server/$service/requirements.in"
    test -f "server/$service/requirements.txt"
  done
  server_stamp=$(python3 server/stamp_version.py)
  firmware_stamp=$(python3 tools/stamp_firmware_version.py)
  echo "Server stamp: $server_stamp"
  echo "Firmware stamp: $firmware_stamp"
  test "$server_stamp" = "$(tr -d '\r\n' < server/VERSION)+${commit:0:10}"
  test "$firmware_stamp" = "$(tr -d '\r\n' < firmware/VERSION)+${commit:0:10}"
  # setup.sh relocates these into firmware-src; check script-relative discovery too.
  mkdir firmware-src
  mv firmware tools firmware-src/
  installed_stamp=$(python3 firmware-src/tools/stamp_firmware_version.py)
  echo "Installed firmware stamp: $installed_stamp"
  test "$installed_stamp" = "$firmware_stamp"
)
