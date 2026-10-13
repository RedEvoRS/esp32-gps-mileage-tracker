#!/usr/bin/env python3
"""Fail if anything personal is about to be published.

This repo is meant to go public at some point, so everything committed --
file contents, file names, commit messages, author emails -- has to be safe
for strangers to read. This catches the leaks that have actually happened
here (a Windows profile path, a first name, a personal email in commit
metadata) and a few that easily could on a GPS project (real trip files,
photos with location metadata, a router's MAC address).

Names and other personal terms can't be listed in the repo without leaking
them, so they come from a denylist that is never committed: the
PII_DENYLIST environment variable (a GitHub Actions secret in CI) and/or a
gitignored .pii-denylist file at the repo root, one term per line.

Findings print as location + rule, never the matched text, so a CI log
can't become the leak.

    python tools/check_pii.py                  # tracked files
    python tools/check_pii.py --commits A..B   # plus those commits' emails and messages
    python tools/check_pii.py --history        # plus every file version ever committed
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Third-party files kept verbatim, upstream license headers and all.
VENDORED = {
    "server/mileage-viewer/static/leaflet.js",
    "server/mileage-viewer/static/leaflet.css",
    "server/mileage-viewer/static/gpx.js",
}

# GPX files that are made up. Real trips start and end at home and live in
# server/data/ (gitignored), so a new fixture has to be added here on purpose.
SYNTHETIC_GPX = {"server/sample.gpx"}

FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)(secrets\.h|\.env|viewer\.env|backup\.conf|\.pii-denylist)$"),
     "secrets file"),
    (re.compile(r"(^|/)settings\.local\.json$"), "per-machine Claude Code settings"),
    (re.compile(r"\.(db|sqlite3?|pem|key|p12|pfx)$|(^|/)id_(rsa|ecdsa|ed25519)"),
     "database or private key"),
    (re.compile(r"^server/data/"), "trip data"),
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff")

# A home directory with a real account name after it: C:\Users\<name> (also
# with forward slashes or JSON-escaped), /c/Users/<name>, /mnt/c/Users/<name>,
# /home/<name>, /Users/<name>. Placeholders start with a non-word character
# (<user>, %i, $USER), so they never match.
HOME_PATH = re.compile(
    r"(?i)(?:\b[a-z]:[\\/]+users"
    r"|(?<![\w.~])/(?:mnt/)?[a-z]/users"
    r"|(?<![\w.~])[\\/](?:home|users))"
    r"[\\/]+(?!(?:public|shared|user|username|runner)\b)\w"
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
# A 6-byte array on a line about a BSSID/MAC, e.g. secrets.h's WIFI_BSSID.
MAC_ARRAY = re.compile(r"(?i)0x[0-9a-f]{2}(?:\s*,\s*0x[0-9a-f]{2}){5}")


def email_allowed(addr):
    """noreply, example and LAN-only addresses (ssh targets like backup@nas.lan)."""
    local, _, domain = addr.lower().rpartition("@")
    return (local in ("noreply", "git")
            or domain.endswith("users.noreply.github.com")
            or domain in ("example.com", "example.org", "example.net")
            or domain.rsplit(".", 1)[-1] in ("lan", "local", "test", "invalid", "example"))


def _real_mac(text):
    return any(set(re.findall(r"[0-9a-f]{2}", m.lower())) - {"00"} for m in text)


LINE_RULES = [
    ("home directory path with an account name", lambda l: HOME_PATH.search(l)),
    ("personal email address",
     lambda l: any(not email_allowed(m) for m in EMAIL.findall(l))),
    ("MAC address", lambda l: _real_mac(MAC.findall(l))),
    ("MAC address", lambda l: re.search(r"(?i)bssid|mac", l)
     and _real_mac(MAC_ARRAY.findall(l))),
]


def term_pattern(term):
    return re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")


def load_denylist():
    raw = os.environ.get("PII_DENYLIST", "")
    local = ROOT / ".pii-denylist"
    if local.is_file():
        raw += "\n" + local.read_text(encoding="utf-8")
    terms = {t.strip() for t in raw.splitlines()}
    return [term_pattern(t) for t in sorted(terms) if t and not t.startswith("#")]


def scan_text(label, text, deny):
    """Yield 'label:line: rule' for each problem line in text."""
    for n, line in enumerate(text.splitlines(), 1):
        rules = {name for name, hit in LINE_RULES if hit(line)}
        if any(p.search(line) for p in deny):
            rules.add("personal term from the denylist")
        for rule in sorted(rules):
            yield f"{label}:{n}: {rule}"


def check_path(path, deny=()):
    """Problems with a file's name alone, whatever is in it."""
    for pattern, rule in FORBIDDEN_PATHS:
        if pattern.search(path):
            yield f"{path}: {rule} must not be committed"
    if path.lower().endswith(".gpx") and path not in SYNTHETIC_GPX:
        yield (f"{path}: GPX that isn't a known synthetic fixture "
               "(real trips reveal home; add made-up ones to SYNTHETIC_GPX)")
    for problem in scan_text(path, path, deny):
        yield f"{path}: file name has a {problem.rsplit(': ', 1)[1]}"


def has_image_metadata(data):
    # EXIF (JPEG/HEIC/WebP/TIFF) or a PNG eXIf chunk; phone photos put GPS here.
    return b"Exif\0\0" in data[:65536] or b"eXIf" in data[:65536]


def check_blob(path, data, deny):
    yield from check_path(path, deny)
    if path.lower().endswith(IMAGE_EXTS) and has_image_metadata(data):
        yield f"{path}: image has EXIF metadata (can include GPS); strip it"
    if path in VENDORED or b"\0" in data[:8192]:
        return
    yield from scan_text(path, data.decode("utf-8", "replace"), deny)


def git(*args, data=None):
    return subprocess.run(["git", *args], cwd=ROOT, input=data, capture_output=True,
                          check=True).stdout


def scan_tree(deny):
    for path in git("ls-files", "-z").decode().split("\0"):
        if path and (ROOT / path).is_file():
            yield from check_blob(path, (ROOT / path).read_bytes(), deny)


def scan_history(deny):
    """Every file version reachable from a branch, tag or remote."""
    paths = {}
    for line in git("rev-list", "--objects", "--exclude=refs/stash", "--all").decode().splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths.setdefault(sha, path)
    types = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                data="\n".join(paths).encode()).decode().split()
    blobs = [sha for sha, kind in zip(types[::2], types[1::2]) if kind == "blob"]
    out = git("cat-file", "--batch", data=("\n".join(blobs) + "\n").encode())
    i = 0
    while i < len(out):
        nl = out.index(b"\n", i)
        sha, _, size = out[i:nl].decode().split()
        body = out[nl + 1:nl + 1 + int(size)]
        i = nl + 2 + int(size)
        for problem in check_blob(paths[sha], body, deny):
            yield f"(history {sha[:8]}) {problem}"


def scan_commits(rev_range, deny):
    log = git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", rev_range)
    for rec in log.decode("utf-8", "replace").split("\x1e"):
        if not rec.strip():
            continue
        sha, an, ae, cn, ce, body = rec.strip("\n").split("\0", 5)
        where = f"commit {sha[:12]}"
        for role, addr in (("author", ae), ("committer", ce)):
            if not email_allowed(addr):
                yield f"{where}: {role} email isn't a noreply address"
        yield from scan_text(f"{where} names", f"{an}\n{cn}", deny)
        yield from scan_text(f"{where} message", body, deny)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commits", metavar="RANGE",
                    help="also check these commits' emails and messages, e.g. origin/main..HEAD")
    ap.add_argument("--history", action="store_true",
                    help="also check every file version ever committed")
    args = ap.parse_args(argv)

    deny = load_denylist()
    findings = list(scan_tree(deny))
    if args.history:
        findings += scan_history(deny)
    if args.commits:
        findings += scan_commits(args.commits, deny)

    for finding in findings:
        print(finding)
    if not deny:
        note = ("No denylist (PII_DENYLIST or .pii-denylist), so names and other "
                "personal terms weren't checked.")
        print(f"::warning::{note}" if os.environ.get("GITHUB_ACTIONS") else f"note: {note}")
    if findings:
        print(f"\n{len(findings)} finding(s). Use placeholders like <user>, <host> and "
              "<username>; commit with your GitHub noreply email.", file=sys.stderr)
        return 1
    print("No personal info found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
