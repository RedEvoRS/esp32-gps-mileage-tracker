"""Strict, stdlib-only environment settings shared by services and installer."""

import ipaddress


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
