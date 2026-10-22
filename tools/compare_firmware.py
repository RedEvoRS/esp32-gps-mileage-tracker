"""Compare a normal installed builder image with CI's Stage 4 image.

Settings differ, so require the same provenance and at most 1% (or 4 KiB)
size difference rather than byte equality. No firmware/settings are printed.
"""

import json
from pathlib import Path
import sys


def compare(install, reference):
    install, reference = Path(install), Path(reference)
    source = install / "firmware-src/firmware"
    commit = (source / "COMMIT").read_text().strip()
    version = (source / "VERSION").read_text().strip() + "+" + commit[:10]
    current = install / "firmware-out/current"
    manifest = json.loads((current / "manifest.json").read_text())
    if manifest["kind"] != "normal" or manifest["commit"] != commit or manifest["version"] != version:
        raise ValueError("Builder manifest provenance/kind differs from bundle")
    provenance = json.loads((reference / "provenance.json").read_text())
    if provenance != {"commit": commit, "version": version, "fqbn": manifest["fqbn"]}:
        raise ValueError("Stage 4 provenance differs from builder")
    built = (current / "app.bin").read_bytes()
    baseline = (reference / "04_trip_logger.ino.bin").read_bytes()
    if version.encode("ascii") not in built or version.encode("ascii") not in baseline:
        raise ValueError("Both app images must contain the exact firmware version")
    tolerance = max(4096, len(baseline) // 100)
    if not baseline or abs(len(built) - len(baseline)) > tolerance:
        raise ValueError("App sizes differ by more than 1% / 4 KiB")
    print(f"Parity: {version}; builder {len(built)} bytes, Stage 4 {len(baseline)} bytes; tolerance {tolerance} bytes")


if __name__ == "__main__":
    compare(*sys.argv[1:])
