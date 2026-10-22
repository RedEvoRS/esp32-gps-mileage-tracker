"""Compare builder pins with the CI job, using only the Python standard library."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def check(workflow, pins, dockerfile, builder):
    errors = []
    job = workflow.split("  firmware-compile:", 1)[1].split("\n  installer:", 1)[0]
    values = dict(line.split("=", 1) for line in pins.splitlines() if line and not line.startswith("#"))
    expected = {"ARDUINO_CLI_VERSION", "ARDUINO_CLI_SHA256", "ESP32_CORE_VERSION", "XPOWERS_VERSION",
                "TINYGPS_VERSION", "U8G2_VERSION", "ESP32_INDEX_URL", "FQBN"}
    if set(values) != expected:
        errors.append("Builder pin keys are incomplete or unexpected")
    for key, value in values.items():
        match = re.search(r"^      " + re.escape(key) + r":\s*['\"]?([^'\"\n]+)", job, re.MULTILINE)
        if not match or match[1].strip() != value:
            errors.append(key + " differs from firmware-compile")
    # Verify pins are actually consumed, including the checksum verification.
    for key in values:
        consumer = builder if key == "FQBN" else dockerfile
        if key not in consumer:
            errors.append(key + " is not consumed by builder")
    for token in ("sha256sum -c -", "Linux_64bit.tar.gz", "USER builder"):
        if token not in dockerfile:
            errors.append("Dockerfile missing " + token)
    for token in ("--warnings", '"default"', "--jobs", '"2"',
                  "compiler.cpp.extra_flags=-DFIRMWARE_VERSION_REQUIRE_STAMP", "upload.maximum_size=",
                  "tools/stamp_firmware_version.py"):
        if token not in builder:
            errors.append("Builder missing CI compile setting " + token)
    # Guard CI changes to those same settings, rather than just the pins.
    for token in ("--warnings default --jobs 2", "-DFIRMWARE_VERSION_REQUIRE_STAMP", "upload.maximum_size="):
        if token not in job:
            errors.append("CI compile settings changed: " + token)
    return errors


def check_repository():
    return check((ROOT / ".github/workflows/audit-tests.yml").read_text(encoding="utf-8"),
                 (HERE / "pins.env").read_text(), (HERE / "Dockerfile").read_text(),
                 (HERE / "builder.py").read_text())


if __name__ == "__main__":
    errors = check_repository()
    if errors:
        raise SystemExit("\n".join(errors))
    print("Firmware builder pins and CI compile settings match.")
