#!/bin/sh
# Run in python:3.12-slim, with the checkout mounted at /work (see README.md).
set -eu
cd -- "$(dirname -- "$0")"
python -c 'import sys; assert sys.platform == "linux" and sys.version_info[:2] == (3, 12), "Use the documented Python 3.12 Linux container"'
python -m pip install --no-cache-dir pip==25.2 pip-tools==7.5.3
for service in mileage-ingest mileage-viewer; do
    (
        cd "$service"
        set -- pip-compile --upgrade --generate-hashes --resolver=backtracking --strip-extras \
            --no-emit-index-url --no-emit-trusted-host
        # Constrain this resolve, not requirements.in: Dependabot regenerates
        # each pair independently and must be able to bump shared packages.
        if [ "$service" = mileage-viewer ]; then
            set -- "$@" --constraint ../mileage-ingest/requirements.txt
        fi
        set -- "$@" --output-file=requirements.txt requirements.in
        CUSTOM_COMPILE_COMMAND="$*" "$@"
    )
done
# Keep pip-compile's standard Python-version header for Dependabot.
python - <<'PY'
from pathlib import Path

comment = (
    "# Full dependency lock for Python 3.12 on Linux (amd64 and arm64).\n"
    "# requirements.in lists direct packages; this file pins their dependencies too.\n"
    "# Image builds and CI check these hashes. Regenerate both locks with\n"
    "# server/update_python_dependencies.sh; see server/README.md.\n"
)
for service in ("mileage-ingest", "mileage-viewer"):
    path = Path(service) / "requirements.txt"
    path.write_text(comment + path.read_text(encoding="utf-8"), encoding="utf-8")
PY
