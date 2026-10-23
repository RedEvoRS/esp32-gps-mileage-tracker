"""Explicit, compare-and-set diagnostics recovery; run inside the ingest image."""

import argparse
import os
from pathlib import Path

from common.diagnostics import load_catalog, validate_device_id
from diag_store import DiagnosticsStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--high-water", required=True, type=int)
    parser.add_argument("--expect-high-water", required=True, type=int)
    args = parser.parse_args()
    try:
        device = validate_device_id(args.device_id)
        store = DiagnosticsStore(Path(os.environ.get("INGEST_DATA_ROOT", "/data")), load_catalog())
        store.reset_high_water(device, args.high_water, args.expect_high_water)
    except ValueError as exc:
        parser.error(str(exc))
    print("High-water reset; current health is unknown until a newer snapshot arrives.")


if __name__ == "__main__":
    main()
