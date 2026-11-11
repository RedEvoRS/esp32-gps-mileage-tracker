# Firmware release notes

Every PR that increases `firmware/VERSION` includes `fw-<VERSION>.md` here
(for example `fw-v1.06.md`). Write only one version-specific summary paragraph,
in the style of the previous firmware release. Review it with the PR; the
workflow publishes that text. The shared `install.md` is the exact Install
section from the `fw-v1.05` release and is appended to every summary.

From the repository root, validate against main and preview the complete body:

```sh
python tools/firmware_release.py check --base origin/main
python tools/firmware_release.py render
```

CI uses `--ref <head-commit>` to read committed files. The PR check compares
VERSION with the merge base of `--base <base-commit>` and the head commit
(or HEAD when `--ref` is omitted). An unchanged VERSION needs no notes, even
when the base tip has a newer version. A changed VERSION must be greater than
both the merge base's version and the base tip's version and include non-empty
notes. The check requires complete base and head history to find their merge
base. The release workflow still compares the before and after push commits
directly and reads all release inputs from the exact push commit.

Merging the bump to main publishes a Latest firmware release immediately,
before the server is deployed or the board is flashed, then uploads assets.
Server `srv-` tags remain manual after a verified deploy. Re-run all Firmware
release jobs to recover a failed run, or manually run Release assets
(`release.yml`) with the published tag if only assets failed. Details:
[Firmware versions and releases](../firmware/README.md#firmware-versions-and-releases).
