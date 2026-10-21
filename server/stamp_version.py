"""Print a Docker build stamp; diagnostics go to stderr, never into the stamp.

Run from any directory: python3 <path-to-server>/stamp_version.py.
Without usable Git metadata, use an archive's COMMIT or VERSION alone.
"""

from pathlib import Path
import re
import subprocess
import sys

from common.version import validate_version


SERVER = Path(__file__).resolve().parent
RELEASE = re.compile(r"v(0|[1-9][0-9]?)\.[0-9]{2}", re.ASCII)
COMMIT_HASH = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII | re.IGNORECASE)


def stamp(server=SERVER, warn=None):
    if warn is None:
        warn = lambda reason: print(f"Server version: {reason}", file=sys.stderr)
    server = Path(server).resolve()
    try:
        release = (server / "VERSION").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        warn("cannot read VERSION; using unversioned")
        return "unversioned"
    if not RELEASE.fullmatch(release):
        warn("VERSION must match v<major>.<two-digit minor> (major 0-99); using unversioned")
        return "unversioned"

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=server, text=True, encoding="utf-8",
            stderr=subprocess.DEVNULL, timeout=10).strip()

    try:
        # A copied server folder inside an unrelated checkout must not borrow
        # that checkout's commit. Linked worktrees are supported as well.
        root = Path(git("rev-parse", "--show-toplevel")).resolve()
        if root != server.parent or server.name != "server":
            raise ValueError
        git("ls-files", "--error-unmatch", "VERSION")
        # Slice the full hash: --short=10 can grow on a prefix collision.
        commit = git("rev-parse", "HEAD")
        if not COMMIT_HASH.fullmatch(commit):
            raise ValueError
        dirty = git("status", "--porcelain", "--untracked-files=no",
                    "--ignore-submodules=untracked")
    except (OSError, subprocess.SubprocessError, ValueError):
        try:
            commit = (server / "COMMIT").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            reason = "COMMIT missing or unreadable"
        else:
            if COMMIT_HASH.fullmatch(commit):
                # Commit/tag archives are clean by construction. Later hand
                # edits to extracted files cannot be detected without Git.
                return validate_version(release + "+" + commit[:10].lower())
            reason = ("COMMIT has an unexpanded archive placeholder" if commit == "$Format:%H$"
                      else "COMMIT is not a 40- or 64-hex hash")
        warn("Git unavailable or not a usable repository checkout; " + reason
             + "; using VERSION alone (no commit/dirty information)")
        return release
    return validate_version(release + "+" + commit[:10] + ("-dirty" if dirty else ""))


if __name__ == "__main__":
    print(stamp())
