"""Identical hand-made folder layouts for viewer and ingest regressions."""


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


def make_layout(root, layout, symlink):
    for name in layout["directories"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name, target in layout["links"]:
        symlink(root / name, root / target, directory=True)
