"""Module-local clocks that leave the process-wide time module untouched."""

import time
from types import SimpleNamespace


def time_stub(**overrides):
    """Keep real clock functions unless a test explicitly replaces them."""
    functions = {name: getattr(time, name) for name in ("time", "monotonic", "sleep")}
    functions.update(overrides)
    return SimpleNamespace(**functions)
