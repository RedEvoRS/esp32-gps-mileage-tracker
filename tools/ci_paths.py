"""Cheap, fail-closed path gates; no third-party action or PR code execution."""

import os
import subprocess


def relevant(paths):
    paths = list(paths)
    install = any(p.startswith("install/") for p in paths)
    builder = install or any(
        p.startswith(("server/firmware-builder/", "firmware/", ".github/workflows/", "tools/ci-install/"))
        or p in ("tools/stamp_firmware_version.py", "tools/build_release_bundle.sh",
                 "tools/compare_firmware.py", "tools/ci_paths.py") for p in paths)
    return builder, install


if __name__ == "__main__":
    if os.environ["EVENT_NAME"] == "workflow_dispatch":
        builder, windows = True, False
    elif set(os.environ.get("BASE_SHA", "")) <= {"0"}:
        builder, windows = True, True
    else:
        changed = subprocess.check_output([
            "git", "diff", "--no-renames", "--name-only", "-z", os.environ["BASE_SHA"], os.environ["HEAD_SHA"],
        ]).decode().split("\0")
        builder, windows = relevant(changed)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"builder={str(builder).lower()}\nwindows={str(windows).lower()}\n")
