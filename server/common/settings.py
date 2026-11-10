"""Strict, stdlib-only environment settings shared by services and installer."""

import ipaddress
import html
import re
import unicodedata
from urllib.parse import urlsplit


DEFAULT_MAP_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_MAP_TILE_ATTRIBUTION = "© OpenStreetMap contributors"


def map_tiles(url, attribution):
    """Validate a Leaflet URL template and return HTML-safe attribution text.

    Compose supplies empty strings for absent keys in older .env files, so
    both empty and unset select the defaults. Only the explicit sentinel
    'none' disables the base layer.
    """
    url = url or DEFAULT_MAP_TILE_URL
    attribution = attribution or DEFAULT_MAP_TILE_ATTRIBUTION
    if url == "none":
        url = None
    else:
        problem = "MAP_TILE_URL must be http(s) with {z}, {x}, {y} (optional {s}, {r}), or none"
        if (len(url) > 2048 or any(c.isspace() or unicodedata.category(c) == "Cc"
                                   or c in "\"'`\\<>" for c in url)):
            raise ValueError(problem + "; at most 2048 characters, no whitespace, controls or quotes")
        placeholders = re.findall(r"\{([^{}]*)\}", url)
        plain = re.sub(r"\{[zxysr]\}", "0", url)
        if (not {"z", "x", "y"}.issubset(placeholders)
                or set(placeholders) - {"z", "x", "y", "s", "r"}
                or "{" in plain or "}" in plain):
            raise ValueError(problem)
        try:
            parsed = urlsplit(plain)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or parsed.port == 0):
                raise ValueError(problem)
        except ValueError as exc:
            raise ValueError(problem) from exc
    if len(attribution) > 2000 or any(unicodedata.category(c) == "Cc" for c in attribution):
        raise ValueError("MAP_TILE_ATTRIBUTION must be text of at most 2000 characters without controls")
    # Leaflet inserts attribution with innerHTML. The setting is always text.
    return url, html.escape(attribution, quote=True)


def boolean(env, name, default):
    """Parse the monitor's existing true/false convention (case insensitive)."""
    value = env.get(name, default).lower()
    if value not in ("true", "false"):
        raise ValueError(name + " must be true or false")
    return value == "true"


def trusted_proxies(value):
    """Return IP networks; empty disables forwarding, malformed entries fail."""
    if not value.strip():
        return ()
    try:
        entries = value.split(",")
        if any(not entry.strip() or "%" in entry for entry in entries):
            raise ValueError("empty entry or scoped address")
        return tuple(ipaddress.ip_network(entry.strip(), strict=False) for entry in entries)
    except ValueError as exc:
        raise ValueError("VIEWER_TRUSTED_PROXIES must be comma-separated IPv4/IPv6 addresses or CIDRs") from exc


def viewer_cookie_secure(value):
    # Compose passes an empty example setting through; it means default false.
    return boolean({"VIEWER_COOKIE_SECURE": value or "false"}, "VIEWER_COOKIE_SECURE", "false")
