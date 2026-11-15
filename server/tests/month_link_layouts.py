"""Identical hand-made folder layouts for viewer and ingest regressions."""

from contextlib import contextmanager


PERMISSION_MONTH_LAYOUTS = {
    "list-only-empty": {"directories": ["2026-02"], "links": [], "modes": {"2026-02": 0o600}},
    "no-access": {"directories": ["2026-02"], "links": [], "modes": {"2026-02": 0o000}},
    "search-only": {"directories": ["2026-02"], "links": [], "modes": {"2026-02": 0o100}},
    "locked-parent": {"directories": ["locked/2026-02"], "links": [("2026-02", "locked/2026-02")],
                      "modes": {"locked": 0o000}},
}


@contextmanager
def restricted_layout(root, layout, symlink):
    make_layout(root, layout, symlink)
    try:
        for name, mode in layout["modes"].items():
            (root / name).chmod(mode)
        yield
    finally:
        for name in layout["modes"]:
            (root / name).chmod(0o700)


MONTH_LINK_LAYOUTS = {
    "other-month": {
        "directories": ["2026-11"],
        "links": [("2026-12", "2026-11")],
        "counted": "2026-11",
        "duplicates": ["2026-12"],
        "uploads": {"2026-12": 503, "2026-11": 200, "2026-09": 200},
    },
    "chain": {
        "directories": ["archive"],
        "links": [("2026-01", "2026-02"), ("2026-02", "archive")],
        "counted": "2026-01",
        "duplicates": ["2026-02"],
        "uploads": {"2026-01": 503, "2026-02": 503, "2026-09": 200},
    },
    "two-links": {
        "directories": ["archive"],
        "links": [("2026-01", "archive"), ("2026-02", "archive")],
        "counted": "2026-01",
        "duplicates": ["2026-02"],
        "uploads": {"2026-01": 503, "2026-02": 503, "2026-09": 200},
    },
    "same-named-duplicate": {
        "directories": ["archive/2026-02"],
        "links": [("2026-01", "archive/2026-02"), ("2026-02", "archive/2026-02")],
        "counted": "2026-01",
        "duplicates": ["2026-02"],
        "uploads": {"2026-01": 503, "2026-02": 200, "2026-09": 200},
    },
}

GLOBAL_UPLOAD_LAYOUTS = {
    "outside": {"directories": ["../outside"], "links": [("2027-02", "../outside")], "blocked": True},
    "outside-chain": {"directories": ["../outside/leaf"], "links": [("bridge", "../outside"), ("2027-02", "bridge/leaf")], "blocked": True},
    "dangling-outside": {"directories": [], "links": [("2027-02", "../missing")], "blocked": True},
    "loop": {"directories": [], "links": [("2027-02", "2027-02")], "blocked": True},
    "loop-chain": {"directories": [], "links": [("2027-02", "2027-03"), ("2027-03", "2027-02")], "blocked": True},
    "data-root": {"directories": [], "links": [("2027-02", ".")], "blocked": True},
    "ancestor": {"directories": [], "links": [("2027-02", "..")], "blocked": True},
    "dangling-inside": {"directories": [], "links": [("2027-02", "missing")], "blocked": False},
    "nonmonth-outside": {"directories": ["../outside"], "links": [("archive", "../outside")], "blocked": False},
}


def make_layout(root, layout, symlink):
    root.mkdir(parents=True, exist_ok=True)
    for name in layout["directories"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name, target in layout["links"]:
        symlink(root / name, root / target, directory=True)
