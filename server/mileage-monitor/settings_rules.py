"""Stdlib monitor settings rules, also used by the installer's stdin checker.

Keep transport policy here, independent of monitor state, HTTP and databases.
The checker stays alive across prompts so a host without Python needs only
one disposable container. Requests and replies are NUL-delimited, never argv.
"""

import ipaddress
import math
import re
import sys
import urllib.parse


SEVERITIES = ("info", "warning", "error", "critical")
NUMBERS = {
    "NOTIFY_HOLD_SECONDS": (0, 604800, False),
    "NOTIFY_REPEAT_HOURS": (1 / 3600, 8760, False),
    "NOTIFY_MAX_PER_HOUR": (1, 1000, True),
    "MONITOR_STALE_DAYS": (1 / 86400, 36500, False),
}


def ascii_text(value, minimum=1, maximum=None, spaces=False):
    return (len(value) >= minimum and (maximum is None or len(value) <= maximum)
            and all((32 if spaces else 33) <= ord(char) <= 126 for char in value))


def host_valid(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(host and len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.rstrip(".").split(".")))


def http_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme in ("http", "https") and host_valid(parsed.hostname or "")
                and parsed.port != 0 and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
            return parsed
    except ValueError:
        pass
    return None


def tcp_address(value):
    match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([^:\[\]\s]+):(\d{1,5})", value)
    if match:
        host, port = match.group(1) or match.group(3), int(match.group(2) or match.group(4))
        if host_valid(host) and 1 <= port <= 65535:
            return host, port
    return None


def tcp_target(value):
    return tcp_address(value) is not None


def valid(name, value):
    if name in ("MONITOR_ENABLED", "NOTIFY_ENABLED", "METRICS_ENABLED", "GRAYLOG_ENABLED",
                "GRAYLOG_GELF_TLS", "GRAYLOG_GELF_TLS_VERIFY"):
        return value.lower() in ("true", "false")
    if name in NUMBERS:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return False  # A numeric value must stay on one monitor.env line.
        lower, upper, integer = NUMBERS[name]
        try:
            number = int(value) if integer else float(value)
            return math.isfinite(number) and lower <= number <= upper
        except (ValueError, OverflowError):
            return False
    if name in ("NTFY_TOPIC", "NTFY_USER"):
        return re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None
    if name == "NTFY_MODE":
        return value in ("bundled", "existing")
    if name == "LOKI_AUTH":
        return value in ("none", "bearer", "basic")
    if name == "NOTIFY_MIN_SEVERITY":
        return value in SEVERITIES
    if name == "NTFY_TOKEN":
        return value != "<token>" and ascii_text(value)
    if name == "METRICS_TOKEN":
        return ascii_text(value, minimum=32)
    if name == "LOKI_BEARER_TOKEN":
        return value != "<token>" and ascii_text(value, maximum=1024)
    if name in ("LOKI_BASIC_AUTH_USER", "LOKI_BASIC_AUTH_PASSWORD"):
        return ascii_text(value, maximum=1024, spaces=True) and (name.endswith("PASSWORD") or ":" not in value)
    if name == "LOKI_TENANT_ID":
        return not value or ascii_text(value, maximum=128, spaces=True)
    if name in ("NTFY_URL", "LOKI_PUSH_URL", "GRAYLOG_GELF_URL", "HTTP_URL"):
        parsed = http_url(value)
        if parsed is None:
            return False
        if name == "NTFY_URL":
            return valid("NTFY_TOPIC", parsed.path.rsplit("/", 1)[-1])
        if name == "LOKI_PUSH_URL":
            return parsed.path.rstrip("/").endswith("/loki/api/v1/push")
        return True
    if name == "GRAYLOG_GELF_TCP":
        return tcp_target(value)
    if name == "GRAYLOG_TARGET":
        return valid("GRAYLOG_GELF_URL", value) or tcp_target(value)
    raise ValueError("Unknown monitor setting rule")


def boolean(env, name, default):
    value = env.get(name, default)
    if not valid(name, value):
        raise ValueError(name + " must be true or false")
    return value.lower() == "true"


def env_quote(value):
    """Compose 2.26+ dotenv syntax; double quotes handle backslashes.

    Single quotes are literal except for \\'; backslashes can escape a quote.
    In that case double-quote, escaping backslash/quote and
    doubling dollars so Compose interpolation preserves them.
    """
    if "\\" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
    return "'" + value.replace("'", "\\'") + "'"


def env_value(raw):
    """Read one-line Compose env syntax for defaults and retained credentials.

    Read literal values with Compose's quote, comment and escape conventions.
    Reject dollar references rather than guessing Compose's project/shell
    interpolation scope. Installer-written values never contain references.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        chars = []
        escaped = False
        for char in raw[1:]:
            if char == quote and not escaped:
                break
            if char == "\\" and not escaped:
                escaped = True
                continue
            if escaped:
                if char != quote:
                    chars.append("\\")
                escaped = False
            chars.append(char)
        else:
            raise ValueError("Unterminated monitor.env quote")
        value = "".join(chars)
        if quote == "'":
            return value
        value = re.sub(r'\\([\\nrt$])', lambda m: {"\\": "\\", "n": "\n", "r": "\r", "t": "\t", "$": "$$"}[m[1]], value)
    else:
        value = raw.split(" #", 1)[0].rstrip()
    # Installer-written values use $$, never an environment reference.
    # Fail closed on references rather than guessing Compose's project scope.
    if re.search(r"(?<!\$)\$(?:[A-Za-z_]|\{)", value.replace("$$", "")):
        raise ValueError("Use single quotes around literal dollars in monitor.env")
    return value.replace("$$", "$")


def serve():
    sys.stdout.buffer.write(b"+ready\0")
    sys.stdout.buffer.flush()

    def field():
        result = bytearray()
        while True:
            char = sys.stdin.buffer.read(1)
            if not char:
                return None
            if char == b"\0":
                return result.decode("utf-8")
            result.extend(char)

    while (operation := field()) is not None:
        value = field()
        if value is None:
            return
        try:
            if operation == "quote":
                answer = env_quote(value)
            elif operation == "value":
                answer = env_value(value)
            else:
                answer = "yes" if valid(operation, value) else "no"
            # No response may inject a second protocol record.
            if "\0" in answer:
                raise ValueError("NUL in setting")
            sys.stdout.buffer.write(b"+" + answer.encode("utf-8") + b"\0")
            sys.stdout.buffer.flush()
        except ValueError:
            # No value or exception text (which can contain a credential).
            sys.stdout.buffer.write(b"-\0")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        serve()
    except UnicodeError:
        raise SystemExit(1) from None  # Never put credential bytes in a traceback.
