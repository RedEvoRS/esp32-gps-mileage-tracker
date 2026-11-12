"""Strict, stdlib-only environment settings shared by services and installer."""

import ipaddress
import math
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


def geocoder_interval(value, default=0.1):
    try:
        interval = float(value.strip() or default)
        if not math.isfinite(interval) or not 0.1 <= interval <= 60:
            raise ValueError
    except ValueError:
        raise ValueError(
            "Invalid VIEWER_GEOCODER_INTERVAL. Use a finite number from 0.1 to 60 seconds."
        ) from None
    return interval


def geocoder_endpoint(url, user_agent):
    if url:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
            valid = (parts.scheme in {"http", "https"} and host
                     and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment
                     and url.isascii() and len(url) <= 2048
                     and not any(ord(c) <= 32 or ord(c) == 127 for c in url)
                     and parts.port != 0
                     and host != "openstreetmap.org"
                     and not host.endswith(".openstreetmap.org")
                     and user_agent and user_agent.isascii() and len(user_agent) <= 256
                     and not any(ord(c) < 32 or ord(c) == 127 for c in user_agent))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Invalid VIEWER_GEOCODER_URL or VIEWER_GEOCODER_USER_AGENT. "
                "Use a self-hosted or approved Nominatim reverse endpoint, "
                "without URL credentials, query or fragment. The public "
                "OpenStreetMap service does not permit vehicle tracking."
            ) from None


def geocoder_public_host(url):
    """Whether the host looks public; advisory only, never resolves DNS."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    shared = ipaddress.ip_network("100.64.0.0/10")
    return not (address.is_private or address.is_loopback or address.is_link_local
                or (address.version == 4 and address in shared))
