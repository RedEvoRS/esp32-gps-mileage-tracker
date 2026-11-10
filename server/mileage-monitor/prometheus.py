"""Small, stdlib-only Prometheus endpoint for the optional monitor (#134)."""

import hmac
import logging
import math
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common.diagnostics import validate_device_id
from shared import condition_states


PORT = 8093
MIN_TOKEN_LENGTH = 32
LOG = logging.getLogger("mileage-monitor")


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool = False
    token: str = ""

    @classmethod
    def from_env(cls, env):
        value = env.get("METRICS_ENABLED", "false").lower()
        if value not in ("true", "false"):
            raise ValueError("METRICS_ENABLED must be true or false")
        if value == "false":
            return cls()
        token = env.get("METRICS_TOKEN", "")
        if (len(token) < MIN_TOKEN_LENGTH or token == "<token>"
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError("METRICS_TOKEN must be at least 32 printable ASCII characters")
        return cls(True, token)


def escape_label(value):
    """Escape a Prometheus label value per the text exposition format."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name, labels, value):
    encoded_labels = ",".join(f'{key}="{escape_label(value)}"' for key, value in labels)
    return f"{name}{{{encoded_labels}}} {value}"


def _number(value):
    if not math.isfinite(value):
        return "0"
    return format(value, ".6g")


class MetricsSnapshot:
    """Immutable scrape data published by the monitor thread.

    The HTTP worker only takes a reference to this snapshot. It never opens a
    SQLite connection, so a scrape cannot hold a database lock or slow a tick.
    """

    def __init__(self, codes, stale_seconds):
        self.codes = codes
        self.stale_seconds = stale_seconds
        self._lock = threading.Lock()
        self._read_errors = 0
        self._last_success = 0.0
        self._devices = ()
        self._episodes = ()

    def record_read_error(self):
        with self._lock:
            self._read_errors += 1

    def publish(self, devices, episodes, successful_read_at):
        device_snapshot = tuple(dict(device) for device in (devices or ()))
        episode_snapshot = tuple(tuple(row) for row in (episodes or ()))
        with self._lock:
            self._devices = device_snapshot
            self._episodes = episode_snapshot
            self._last_success = successful_read_at

    def render(self, now):
        with self._lock:
            devices, episodes = self._devices, self._episodes
            last_success, read_errors = self._last_success, self._read_errors

        lines = []

        def family(name, help_text, kind, samples):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(samples)

        contacts, snapshots, pending, dropped, counters = [], [], [], [], []
        known_conditions = {}
        decisions = {}
        for device, code, state, alerted_at, resolved_at in episodes:
            if code not in self.codes or not isinstance(device, str):
                continue
            try:
                validate_device_id(device)
            except ValueError:
                continue
            key = (device, code)
            known_conditions.setdefault(device, set()).add(code)
            counts = decisions.setdefault(key, {"active": 0, "sent": 0, "resolved": 0})
            if alerted_at is not None:
                counts["sent"] += 1
                if resolved_at is None and state == "active":
                    counts["active"] += 1
            if resolved_at is not None:
                counts["resolved"] += 1

        for device in devices:
            device_id = device.get("device_id")
            if not isinstance(device_id, str) or device.get("device_id_valid") is False:
                continue
            try:
                validate_device_id(device_id)
            except ValueError:
                continue
            contact = device.get("contact")
            if isinstance(contact, (int, float)):
                contacts.append(_sample("mileage_tracker_last_contact_age_seconds",
                                        (("device_id", device_id),), _number(max(0.0, now - contact))))
            snapshot_at = device.get("snapshot_at")
            if isinstance(snapshot_at, (int, float)):
                snapshots.append(_sample("mileage_tracker_snapshot_age_seconds",
                                         (("device_id", device_id),),
                                         _number(max(0.0, now - snapshot_at))))
            snapshot = device.get("snapshot")
            if isinstance(snapshot, dict):
                for field, output in (("pending_events", pending), ("dropped_events", dropped)):
                    value = snapshot.get(field)
                    if type(value) is int and value >= 0:
                        output.append(_sample("mileage_tracker_" + field,
                                              (("device_id", device_id),), str(value)))
                for code_text, value in snapshot.get("counters", {}).items():
                    try:
                        code = int(code_text)
                    except (TypeError, ValueError):
                        continue
                    definition = self.codes.get(code)
                    if definition and definition.get("journal") == "counter" and type(value) is int and value >= 0:
                        counters.append(_sample("mileage_tracker_checkin_counter",
                                                (("device_id", device_id), ("code", code),
                                                 ("symbol", definition["symbol"]),
                                                 ("severity", definition["severity"])), str(value)))

        known = {(device, code) for device, codes in known_conditions.items() for code in codes}
        conditions = condition_states(devices, known, now, self.stale_seconds)

        family("mileage_tracker_last_contact_age_seconds", "Seconds since the last accepted server contact.",
               "gauge", contacts)
        family("mileage_tracker_snapshot_age_seconds", "Seconds since the last accepted diagnostics snapshot.",
               "gauge", snapshots)
        family("mileage_tracker_condition_state", "Current catalog condition state; exactly one state is 1.",
               "gauge", [_sample("mileage_tracker_condition_state",
                                 (("device_id", device), ("code", code),
                                  ("symbol", self.codes[code]["symbol"]),
                                  ("severity", self.codes[code]["severity"]), ("state", state)), "1")
                         for (device, code), (state, _, _) in sorted(conditions.items()) if code in self.codes])
        family("mileage_tracker_pending_events", "Pending diagnostic events reported by the latest check-in.",
               "gauge", pending)
        family("mileage_tracker_dropped_events", "Dropped diagnostic events reported by the latest check-in.",
               "gauge", dropped)
        family("mileage_tracker_checkin_counter", "Latest check-in counter for a catalog counter condition.",
               "gauge", counters)

        alert_samples = []
        for (device, code), counts in sorted(decisions.items()):
            definition = self.codes[code]
            for decision in ("active", "sent", "resolved"):
                alert_samples.append(_sample("mileage_tracker_alert_decisions",
                                             (("device_id", device), ("code", code),
                                              ("symbol", definition["symbol"]),
                                              ("severity", definition["severity"]),
                                              ("decision", decision)), str(counts[decision])))
        family("mileage_tracker_alert_decisions",
               "ntfy delivery state: episodes active after delivery, sent at least once, or resolved after delivery.",
               "gauge", alert_samples)
        family("mileage_tracker_db_read_errors_total", "Monitor diagnostics or state database read failures.",
               "counter", [f"mileage_tracker_db_read_errors_total {read_errors}"])
        family("mileage_tracker_last_successful_read_timestamp_seconds",
               "Unix timestamp of the last successful diagnostics database read.", "gauge",
               [f"mileage_tracker_last_successful_read_timestamp_seconds {_number(last_success)}"])
        family("mileage_tracker_monitor_stale_after_seconds",
               "Configured server-contact silence threshold used by monitor alert E7001.", "gauge",
               [f"mileage_tracker_monitor_stale_after_seconds {_number(self.stale_seconds)}"])
        return "\n".join(lines) + "\n"


def make_server(snapshot, token, host="0.0.0.0", port=PORT):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            authorization = self.headers.get("Authorization", "")
            scheme, separator, supplied = authorization.partition(" ")
            candidate = supplied if separator and scheme.lower() == "bearer" else ""
            try:
                authorized = hmac.compare_digest(candidate.encode("utf-8"), token.encode("ascii"))
            except (UnicodeEncodeError, TypeError):
                authorized = False
            if not authorized:
                body = b"Unauthorized\n"
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = snapshot.render(time.time()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            # Do not write request headers, tokens, or scrape metadata to logs.
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def start_server(snapshot, token):
    server = make_server(snapshot, token)
    threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True).start()
    LOG.info("Prometheus metrics endpoint listening on port %s", PORT)
    return server
