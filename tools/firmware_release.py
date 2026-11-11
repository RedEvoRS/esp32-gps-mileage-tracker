"""Validate reviewed firmware release notes and assemble the public release body."""

import argparse
from pathlib import Path
import subprocess

from stamp_firmware_version import RELEASE


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = "firmware/VERSION"
INSTALL_PATH = "release-notes/install.md"


def git(root, *args):
    try:
        return subprocess.check_output(
            ["git", *args], cwd=root, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("cannot read Git revision or file; fetch the base and head commits") from None


def resolve(root, ref):
    return git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode("ascii").strip()


def read(root, path, ref=None):
    try:
        return git(root, "show", f"{ref}:{path}") if ref else (Path(root) / path).read_bytes()
    except (OSError, ValueError):
        raise ValueError(f"cannot read {path}") from None


def version(raw):
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        raise ValueError("firmware/VERSION must be ASCII") from None
    if not RELEASE.fullmatch(value):
        raise ValueError("firmware/VERSION must match v<major>.<two-digit minor> (major 0-99, no leading zeros)")
    return value


def number(value):
    return tuple(int(part) for part in value[1:].split("."))


def body(root, value, ref=None):
    notes_path = f"release-notes/fw-{value}.md"
    try:
        summary = read(root, notes_path, ref).decode("utf-8").replace("\r\n", "\n").strip()
        install = read(root, INSTALL_PATH, ref).decode("utf-8").replace("\r\n", "\n")
    except UnicodeError:
        raise ValueError("release notes and Install template must be UTF-8") from None
    if not summary:
        raise ValueError(f"{notes_path} must contain a non-empty summary paragraph")
    # Keep the shared instructions out of version notes, so there is one source.
    if "\n\n" in summary or "**Install**" in summary:
        raise ValueError(f"{notes_path} must contain only one version-specific summary paragraph")
    if not install.startswith("**Install**\n") or not install.strip():
        raise ValueError(f"{INSTALL_PATH} must start with **Install**")
    return summary + "\n\n" + install


def validate(root, base=None, ref=None, *, pull_request=False):
    """Compare pushes with base; PR changes must exceed merge base and base tip."""
    ref = resolve(root, ref) if ref else None
    raw = read(root, VERSION_PATH, ref)
    value = version(raw)
    if base:
        base = resolve(root, base)
        comparison_base = base
        if pull_request:
            head = ref or resolve(root, "HEAD")
            try:
                comparison_base = git(root, "merge-base", base, head).decode("ascii").strip()
            except ValueError:
                raise ValueError("cannot compute PR merge base; fetch complete base and head history") from None
        previous_raw = read(root, VERSION_PATH, comparison_base)
        previous = version(previous_raw)
        if raw == previous_raw:
            return value, False
        if pull_request:
            base_tip = version(read(root, VERSION_PATH, base))
            if number(value) <= number(previous) or number(value) <= number(base_tip):
                raise ValueError(
                    f"firmware/VERSION must increase above both merge base {previous} "
                    f"and base tip {base_tip}; proposed {value}")
        elif number(value) <= number(previous):
            raise ValueError(f"firmware/VERSION must increase: base {previous}, proposed {value}")
    body(root, value, ref)
    return value, True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "render"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", help="base revision to compare firmware/VERSION against (required for check)")
    parser.add_argument("--ref", help="read VERSION, notes and template from this commit instead of working files")
    parser.add_argument("--body-file", type=Path, help="render to this UTF-8 file instead of stdout")
    parser.add_argument("--github-output", type=Path, help="append validated version and tag as Actions outputs")
    args = parser.parse_args(argv)
    if args.command == "check" and not args.base:
        parser.error("check requires --base")
    try:
        value, changed = validate(args.root, args.base, args.ref, pull_request=args.command == "check")
        if args.command == "render":
            if args.base and not changed:
                raise ValueError("firmware/VERSION did not change; there is no release to publish")
            ref = resolve(args.root, args.ref) if args.ref else None
            content = body(args.root, value, ref)
            if args.body_file:
                args.body_file.write_text(content, encoding="utf-8", newline="\n")
            else:
                print(content, end="")
        else:
            print(f"Firmware release check: {value} ({'increase with reviewed notes' if changed else 'unchanged'})")
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
                output.write(f"version={value}\ntag=fw-{value}\n")
    except (OSError, ValueError) as error:
        raise SystemExit(f"Firmware release validation failed: {error}") from None


if __name__ == "__main__":
    main()
