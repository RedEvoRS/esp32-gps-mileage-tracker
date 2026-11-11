"""Bounds shared by the diagnostics setting's owner and consumer."""

from pathlib import Path


DEFAULT_DAYS = 365
MIN_DAYS = 30
MAX_DAYS = 3650
SETTINGS_PATH = Path("/settings/diagnostics.json")


def valid_days(value):
    return type(value) is int and MIN_DAYS <= value <= MAX_DAYS
