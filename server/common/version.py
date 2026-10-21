"""Bounded, public server build identity (stdlib only)."""

import os
import re


TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", re.ASCII)


def validate_version(value):
    """Never publish malformed build arguments in HTML, JSON or logs."""
    if isinstance(value, str) and TOKEN.fullmatch(value):
        return value
    return "unversioned"


def server_version():
    return validate_version(os.environ.get("SERVER_VERSION"))
