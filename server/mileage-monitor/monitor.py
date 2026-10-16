"""Optional, stdlib-only diagnostics policy, ntfy alerts and external exports.

Ingest owns events.db. This process only opens it in mode=ro, never immutable.
All mutable policy, queue, retry and rate-limit state belongs to monitor.db.
"""

import json
import ipaddress
import logging
import math
import os
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sys

# Support the source tree (tests load this file directly) and the container's
# flat /app layout, as the ingest and viewer do.
for import_root in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from common.diagnostics import CatalogError, load_catalog, validate_device_id, validate_request  # noqa: E402

import gelf  # noqa: E402
import prometheus  # noqa: E402


LOG = logging.getLogger("mileage-monitor")
SEVERITIES = ("info", "warning", "error", "critical")
SNAPSHOT_MAX_AGE_SECONDS = 86400
TICK_SECONDS = 60
MAX_BODY_BYTES = 3500  # Leave room below ntfy's default 4096-byte message limit.


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    notify: bool = False
    url: str = ""
    token: str = ""
    min_severity: str = "error"
    hold_seconds: float = 300
    repeat_seconds: float = 86400
    max_per_hour: int = 6
    stale_seconds: float = 7 * 86400
    graylog: gelf.GelfConfig = gelf.GelfConfig()
    metrics: prometheus.MetricsConfig = field(default_factory=prometheus.MetricsConfig)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def boolean(name):
            value = env.get(name, "false").lower()
            if value not in ("true", "false"):
                raise ValueError(name + " must be true or false")
            return value == "true"

        metrics = prometheus.MetricsConfig.from_env(env)
        enabled = boolean("MONITOR_ENABLED")
        if not enabled:
            if metrics.enabled:
                raise ValueError("METRICS_ENABLED requires MONITOR_ENABLED=true")
            return cls()  # Inactive profile can carry unfilled example settings.
        notify = boolean("NOTIFY_ENABLED")

        def number(name, default, lower, upper, *, integer=False):
            try:
                raw = env.get(name, str(default))
                value = int(raw) if integer else float(raw)
                if not math.isfinite(value) or not lower <= value <= upper:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ValueError(name + " is outside its supported numeric range") from None
            return value

        severity = env.get("NOTIFY_MIN_SEVERITY", "error")
        if severity not in SEVERITIES:
            raise ValueError("NOTIFY_MIN_SEVERITY must be info, warning, error or critical")
        hold = number("NOTIFY_HOLD_SECONDS", 300, 0, 604800)
        repeat = number("NOTIFY_REPEAT_HOURS", 24, 1 / 3600, 8760) * 3600
        maximum = number("NOTIFY_MAX_PER_HOUR", 6, 1, 1000, integer=True)
        stale = number("MONITOR_STALE_DAYS", 7, 1 / 86400, 36500) * 86400
        url, token = env.get("NTFY_URL", ""), env.get("NTFY_TOKEN", "")
        if notify:
            try:
                parsed = urllib.parse.urlsplit(url)
                host = parsed.hostname or ""
                try:
                    ipaddress.ip_address(host)
                    valid_host = True
                except ValueError:
                    valid_host = bool(host and len(host) <= 253 and all(
                        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                        for label in host.rstrip(".").split(".")))
                valid = (parsed.scheme in ("http", "https") and parsed.hostname
                         and valid_host
                         and parsed.port != 0 and parsed.username is None
                         and parsed.password is None and not parsed.query and not parsed.fragment
                         and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", parsed.path.rsplit("/", 1)[-1])
                         and not any(c.isspace() for c in url))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("NTFY_URL must be an http(s) topic URL without credentials, query or fragment")
            if not token or token == "<token>" or any(not 33 <= ord(c) <= 126 for c in token):
                raise ValueError("NTFY_TOKEN must be a nonempty access token without whitespace")
        return cls(enabled=enabled, notify=notify, url=url, token=token,
                   min_severity=severity, hold_seconds=hold, repeat_seconds=repeat,
                   max_per_hour=maximum, stale_seconds=stale,
                   graylog=gelf.GelfConfig.from_env(env), metrics=metrics)


def receipt(value):
    """Only timezone-aware server receipt timestamps are policy inputs."""
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("invalid diagnostics receipt timestamp")
    return stamp.timestamp()


def read_devices(path, catalog):
    path = Path(path)
    catalog_codes = {row["code"] for row in catalog["codes"]}
    try:
        path.stat()
    except FileNotFoundError:
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        versions = db.execute("SELECT singleton, version FROM schema_version").fetchall()
        if [tuple(row) for row in versions] != [(1, 1)]:
            raise ValueError("unsupported diagnostics database schema version")
        devices = []
        for row in db.execute("SELECT device_id,last_contact_at,status_sequence,snapshot_json,"
                              "snapshot_received_at FROM devices ORDER BY device_id"):
            device = dict(row)
            device["contact"] = None
            device["snapshot"] = None
            device["conditions"] = {}
            device["device_id_valid"] = False
            try:
                validate_device_id(device["device_id"])
                device["device_id_valid"] = True
                device["contact"] = receipt(device["last_contact_at"])
                if device["snapshot_received_at"] is not None:
                    device["snapshot_at"] = receipt(device["snapshot_received_at"])
                if device["snapshot_json"] is not None:
                    snapshot = json.loads(device["snapshot_json"])
                    validate_request({"schema_version": 1, "checkin": snapshot, "events": []}, catalog=catalog)
                    if snapshot["status_sequence"] != device["status_sequence"]:
                        raise ValueError("invalid diagnostics snapshot revision")
                    device["snapshot"] = snapshot
            except (ValueError, TypeError, OverflowError, RecursionError, OSError) as exc:
                # A stored snapshot can predate the current catalog or be
                # damaged. Its faults are unknown, but valid contact remains
                # usable for silence. Never carry stored text into a log.
                device["snapshot_error"] = type(exc).__name__
            devices.append(device)
        by_device = {device["device_id"]: device for device in devices}
        for device_id, code, state in db.execute(
                "SELECT device_id,code,state FROM device_conditions ORDER BY device_id,code"):
            device = by_device.get(device_id)
            if (device is not None and type(code) is int and code in catalog_codes
                    and state in ("active", "clear", "unknown")):
                device["conditions"][code] = state
        return devices


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token through a redirect.
        return None


class NtfySender:
    def __init__(self, config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, body):
        request = urllib.request.Request(
            self.config.url, body.encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self.config.token,
                     "Content-Type": "text/plain; charset=utf-8"})
        try:
            with self.opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise OSError("ntfy rejected delivery")
                response.read(1024)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS episodes (
    device TEXT NOT NULL, code INTEGER NOT NULL, episode TEXT NOT NULL,
    state TEXT NOT NULL, eligible_since REAL, revision INTEGER NOT NULL,
    contact REAL, alerted_at REAL, last_sent_at REAL, resolved_at REAL,
    PRIMARY KEY (device, code, episode)
);
CREATE UNIQUE INDEX IF NOT EXISTS open_fault ON episodes(device, code)
    WHERE code != 7001 AND alerted_at IS NOT NULL AND resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS work (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL, code INTEGER NOT NULL, episode TEXT NOT NULL,
    kind TEXT NOT NULL, queued_at REAL NOT NULL, overflow INTEGER NOT NULL DEFAULT 0,
    UNIQUE (device, code, episode, kind)
);
CREATE TABLE IF NOT EXISTS outbox (singleton INTEGER PRIMARY KEY, ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (sent_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS retry (
    singleton INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_warnings (
    device TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY (device, revision)
);
"""


class Monitor:
    def __init__(self, config, events_path="/data/diagnostics/events.db",
                 state_path="/state/monitor.db", *, clock=time.time, sender=None, catalog=None,
                 gelf_sender=None):
        self.config = config
        try:
            self.catalog = load_catalog() if catalog is None else catalog
        except CatalogError as exc:
            raise ValueError("Cannot start monitor: missing or invalid diagnostics catalog.json") from exc
        self.codes = {row["code"]: row for row in self.catalog["codes"]}
        self.events_path, self.state_path = Path(events_path), Path(state_path)
        if self.events_path.resolve() == self.state_path.resolve():
            raise ValueError("monitor state must be separate from diagnostics")
        self.clock = clock
        self.sender = NtfySender(config) if sender is None else sender
        self.gelf_sender = (gelf.transport(config.graylog) if gelf_sender is None and config.graylog.enabled
                            else gelf_sender)
        self.metrics_snapshot = (prometheus.MetricsSnapshot(self.codes, config.stale_seconds)
                                 if config.metrics.enabled else None)
        if config.enabled:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:
                # Initialize/version in one transaction: interrupted first use
                # must not leave a partial schema that cannot restart.
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone():
                    if db.execute("SELECT singleton,version FROM schema_version").fetchall() != [(1, 1)]:
                        raise ValueError("unsupported monitor state schema version")
                # GELF tables are additive to schema v1 (CREATE IF NOT EXISTS),
                # so an existing monitor.db gains them without a migration.
                for statement in (SCHEMA + gelf.SCHEMA).split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT OR IGNORE INTO schema_version VALUES (1,1)")
                db.execute("INSERT OR IGNORE INTO retry VALUES (1,0,0)")
                for statement in gelf.SEED:
                    db.execute(statement)

    def _connect(self):
        db = sqlite3.connect(self.state_path, timeout=2)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
        except Exception:
            db.close()
            raise
        return db

    def start_metrics(self):
        if self.metrics_snapshot is None:
            return None
        return prometheus.start_server(self.metrics_snapshot, self.config.metrics.token)

    def _publish_metrics(self, devices, now, db=None):
        if self.metrics_snapshot is None:
            return
        episodes = ()
        if db is not None:
            try:
                episodes = db.execute("SELECT device,code,state,alerted_at,resolved_at FROM episodes").fetchall()
            except sqlite3.Error:
                self.metrics_snapshot.record_read_error()
                LOG.warning("Monitor state unavailable (database read error); retaining previous metrics")
                return
        self.metrics_snapshot.publish(devices, episodes, now)

    def eligible(self, code):
        definition = self.codes.get(code)
        return bool(definition and definition["notify"] and
                    (code == 7001 or SEVERITIES.index(definition["severity"]) >=
                     SEVERITIES.index(self.config.min_severity)))

    @staticmethod
    def _warn_snapshots(db, devices):
        for device in devices:
            error = device.get("snapshot_error")
            if error and db.execute("INSERT OR IGNORE INTO snapshot_warnings VALUES (?,?)",
                                    (str(device["device_id"]), str(device["status_sequence"]))).rowcount:
                LOG.warning("Device snapshot unavailable (%s); conditions unknown", error)

    def _reconcile(self, db, devices, now):
        # Reset eligibility on unknown/stale input, but preserve sent history.
        previous = {tuple(row[:3]): row[3] for row in db.execute(
            "SELECT device,code,episode,eligible_since FROM episodes WHERE state='active'")}
        db.execute("UPDATE episodes SET state='unknown', eligible_since=NULL WHERE state!='clear'")
        clear_codes = set()
        for device in devices:
            name, contact = device["device_id"], device["contact"]
            if contact is None:  # Damaged identity/contact cannot drive any policy.
                continue
            clear_codes.add((name, 7001))  # Per-episode contact comparison below.
            # A contact is recovery only when it is genuinely newer than the
            # contact on which this silence episode was based (not a clock jump).
            db.execute("UPDATE episodes SET state='clear', eligible_since=NULL "
                       "WHERE device=? AND code=7001 AND contact<?", (name, contact))
            if now - contact >= self.config.stale_seconds:
                episode = "silence:" + device["last_contact_at"]
                self._active(db, name, 7001, episode, 0, contact, now, previous)
            snapshot = device["snapshot"]
            if snapshot is None or not 0 <= now - device["snapshot_at"] <= SNAPSHOT_MAX_AGE_SECONDS:
                continue
            revision = snapshot["status_sequence"]
            active = {item["code"]: item["episode_id"] for item in snapshot["active"]}
            for code in set(snapshot.get("observed", [])) - set(active):
                clear_codes.add((name, code))
                db.execute("UPDATE episodes SET state='clear', eligible_since=NULL "
                           "WHERE device=? AND code=? AND revision<?", (name, code, revision))
            for code, episode in active.items():
                if self.eligible(code):
                    self._active(db, name, code, episode, revision, None, now, previous)
        return clear_codes

    @staticmethod
    def _active(db, device, code, episode, revision, contact, now, previous):
        # Firmware creates a new boot/episode on every deep-sleep wake. Once
        # sent, a fault stays anchored to its original monitor row until an
        # explicit clear AND successful resolution close it. This also cancels
        # a deferred resolution if the fault returns before it is delivered.
        if code != 7001:
            opened = db.execute("SELECT episode FROM episodes WHERE device=? AND code=? "
                                "AND alerted_at IS NOT NULL AND resolved_at IS NULL",
                                (device, code)).fetchone()
            if opened:
                episode = opened[0]
        since = previous.get((device, code, episode))
        db.execute("INSERT INTO episodes(device,code,episode,state,eligible_since,revision,contact) "
                   "VALUES (?,?,?,'active',?,?,?) ON CONFLICT(device,code,episode) DO UPDATE SET "
                   "state='active', eligible_since=excluded.eligible_since, "
                   "revision=MAX(episodes.revision,excluded.revision) WHERE episodes.state!='clear' "
                   "OR (episodes.alerted_at IS NOT NULL AND episodes.resolved_at IS NULL)",
                   (device, code, episode, since if since is not None else now, revision, contact))

    def _queue(self, db, now, clear_codes):
        desired = set()
        for row in db.execute("SELECT device,code,episode,state,eligible_since,alerted_at,"
                              "last_sent_at,resolved_at FROM episodes"):
            device, code, episode, state, since, alerted, last_sent, resolved = row
            if (state == "clear" and code in self.codes and (device, code) in clear_codes
                    and alerted is not None and resolved is None):
                desired.add((device, code, episode, "resolution"))
            elif state == "active" and self.eligible(code):
                hold = 0 if code == 7001 or self.codes[code]["severity"] == "critical" else self.config.hold_seconds
                if alerted is None and now - since >= hold:
                    desired.add((device, code, episode, "alert"))
                elif alerted is not None and now - last_sent >= self.config.repeat_seconds:
                    desired.add((device, code, episode, "reminder"))
        if not self.config.notify:
            desired.clear()
        for row in db.execute("SELECT id,device,code,episode,kind FROM work").fetchall():
            if tuple(row[1:]) not in desired:
                db.execute("DELETE FROM work WHERE id=?", (row[0],))
        for key in sorted(desired):
            db.execute("INSERT OR IGNORE INTO work(device,code,episode,kind,queued_at) VALUES (?,?,?,?,?)",
                       (*key, now))

    def _message(self, row, now):
        _, device, code, episode, kind, *_ = row
        definition = self.codes[code]
        title = definition["title"]
        if code == 7001 and kind != "resolution":
            contact = receipt(episode.removeprefix("silence:"))
            title = f"No contact for {max(0, int((now - contact) // 86400))} days"
        label = {"alert": "Alert", "reminder": "Reminder", "resolution": "Resolved"}[kind]
        return f"{label} E{code}: {title}\nDevice: {device}\nRecovery: {definition['recovery']}"

    def _prepare(self, db, now, remaining):
        work = db.execute("SELECT * FROM work ORDER BY id").fetchall()
        if not work:
            db.execute("DELETE FROM outbox")
            return None
        if remaining <= 0:
            db.execute("UPDATE work SET overflow=1")
            return None
        pending = db.execute("SELECT ids_json FROM outbox WHERE singleton=1").fetchone()
        if pending:
            ids = set(json.loads(pending[0]))
            chosen = [row for row in work if row[0] in ids]
            if chosen:
                body = "\n\n".join(self._message(row, now) for row in chosen)
                if len(chosen) > 1 or any(row[6] for row in chosen):
                    body = "Diagnostics digest\n\n" + body
                return chosen, body
            db.execute("DELETE FROM outbox")
        # Coalesce a burst that cannot fit in the rolling-hour budget. Marked
        # overflow stays coalesced when a slot opens in a later hour.
        overflow = len(work) > remaining or any(row[6] for row in work)
        if len(work) > remaining:
            db.execute("UPDATE work SET overflow=1")
        digest = overflow and (remaining == 1 or any(row[6] for row in work))
        chosen, parts = [], []
        body = "Diagnostics digest\n\n" if digest else ""
        for row in work:
            part = self._message(row, now)
            candidate = body + "\n\n".join(parts + [part])
            if len(candidate.encode("utf-8")) > MAX_BODY_BYTES:
                break
            chosen.append(row)
            parts.append(part)
            if not digest:
                break
        body += "\n\n".join(parts)
        db.execute("INSERT OR REPLACE INTO outbox VALUES (1,?)",
                   (json.dumps([row[0] for row in chosen]),))
        return chosen, body

    def tick(self):
        try:
            return self._tick()
        except sqlite3.Error:
            if self.metrics_snapshot is not None:
                self.metrics_snapshot.record_read_error()
            raise

    def _tick(self):
        if not self.config.enabled and self.metrics_snapshot is None:
            return True
        try:
            devices = read_devices(self.events_path, self.catalog)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            # Exceptions may contain a path or endpoint. Do not log their text.
            if self.metrics_snapshot is not None:
                self.metrics_snapshot.record_read_error()
            LOG.warning("Diagnostics unavailable (%s); retry next tick", type(exc).__name__)
            return False
        now = self.clock()
        if devices is None:
            LOG.info("No diagnostics database yet; nothing to monitor")
            self._publish_metrics([], now)
            return True
        if not self.config.enabled:
            self._publish_metrics(devices, now)
            return True
        with closing(self._connect()) as db:
            with db:
                self._warn_snapshots(db, devices)
                clear_codes = self._reconcile(db, devices, now)
                self._queue(db, now, clear_codes)
                db.execute("DELETE FROM deliveries WHERE sent_at<=?", (now - 3600,))
            while self.config.notify:
                attempts, next_attempt = db.execute("SELECT attempts,next_attempt FROM retry WHERE singleton=1").fetchone()
                if now < next_attempt:
                    break
                count = db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
                with db:
                    prepared = self._prepare(db, now, self.config.max_per_hour - count)
                if prepared is None:
                    break
                rows, body = prepared  # Intent is committed before any HTTP POST.
                try:
                    self.sender(body)
                except Exception as exc:
                    attempts += 1
                    delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
                    with db:
                        db.execute("UPDATE retry SET attempts=?,next_attempt=? WHERE singleton=1",
                                   (attempts, now + delay))
                    LOG.warning("ntfy delivery failed (%s); retry in %s seconds", type(exc).__name__, delay)
                    break
                with db:
                    for row in rows:
                        key = (row[1], row[2], row[3])
                        if row[4] == "resolution":
                            db.execute("UPDATE episodes SET resolved_at=? WHERE device=? AND code=? AND episode=?",
                                       (now, *key))
                        else:
                            db.execute("UPDATE episodes SET alerted_at=COALESCE(alerted_at,?),last_sent_at=? "
                                       "WHERE device=? AND code=? AND episode=?", (now, now, *key))
                        db.execute("DELETE FROM work WHERE id=?", (row[0],))
                    db.execute("INSERT INTO deliveries VALUES (?)", (now,))
                    db.execute("DELETE FROM outbox")
                    db.execute("UPDATE retry SET attempts=0,next_attempt=0 WHERE singleton=1")
                LOG.info("ntfy delivered %s condition update(s)", len(rows))
            try:
                self._forward_gelf(db, devices, now)
            except Exception as exc:  # Graylog problems must never affect ntfy or trips.
                LOG.warning("Graylog export unavailable (%s); retry next tick", type(exc).__name__)
            self._publish_metrics(devices, now, db)
        return True

    def _forward_gelf(self, db, devices, now):
        """Forward condition changes, then new events, to Graylog (at least once)."""
        if not self.config.graylog.enabled:
            return
        attempts, next_attempt = db.execute(
            "SELECT attempts,next_attempt FROM gelf_retry WHERE singleton=1").fetchone()
        if now < next_attempt:
            return
        known = {(device, code): (state, episode) for device, code, state, episode in
                 db.execute("SELECT device,code,state,episode FROM gelf_conditions")}
        changes = gelf.desired_conditions(devices, known, now, self.config.stale_seconds,
                                          SNAPSHOT_MAX_AGE_SECONDS)
        # Items are (kind, key, payload); payload None marks a damaged event
        # that is skipped but still advances the cursor once reached.
        items = [("condition", (key, value), gelf.condition_message(
                    self.codes, key[0], key[1], value[0], value[1], now, value[2]))
                 for key, value in sorted(changes.items())][:gelf.MAX_PER_TICK]
        cursor = db.execute("SELECT event_rowid FROM gelf_cursor WHERE singleton=1").fetchone()[0]
        highest, rows = gelf.read_events(self.events_path, cursor, max(0, gelf.MAX_PER_TICK - len(items)))
        if highest < cursor:
            # events.db was replaced (e.g. restored from a backup): start over.
            # Graylog may see duplicates; it never silently misses events.
            LOG.warning("Diagnostics events restarted below the Graylog cursor; resending from the start")
            with db:
                db.execute("UPDATE gelf_cursor SET event_rowid=0 WHERE singleton=1")
            return
        for row in rows:
            payload = gelf.event_message(self.codes, row)
            if payload is None:
                LOG.warning("Skipping a damaged diagnostics event for Graylog")
            items.append(("event", row[0], payload))
        payloads = [payload for _, _, payload in items if payload is not None]
        if not items:
            return
        error = None
        try:
            delivered = self.gelf_sender(payloads) if payloads else 0
        except gelf.DeliveryError as exc:
            delivered, error = exc.delivered, exc
        with db:
            sent = 0
            for kind, key, payload in items:
                if payload is not None:
                    if sent == delivered:
                        break
                    sent += 1
                if kind == "condition":
                    (device, code), (state, episode, _) = key
                    db.execute("INSERT INTO gelf_conditions VALUES (?,?,?,?) ON CONFLICT(device,code) "
                               "DO UPDATE SET state=excluded.state, episode=excluded.episode",
                               (device, code, state, episode))
                else:
                    db.execute("UPDATE gelf_cursor SET event_rowid=? WHERE singleton=1", (key,))
            if error is None:
                db.execute("UPDATE gelf_retry SET attempts=0,next_attempt=0 WHERE singleton=1")
            else:
                attempts += 1
                delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
                db.execute("UPDATE gelf_retry SET attempts=?,next_attempt=? WHERE singleton=1",
                           (attempts, now + delay))
        if error is None:
            LOG.info("Graylog accepted %s message(s)", delivered)
        else:
            LOG.warning("Graylog delivery failed (%s) after %s message(s); retry in %s seconds",
                        error, delivered, delay)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except ValueError as exc:
        LOG.error("Invalid monitor configuration: %s", exc)
        raise SystemExit(2) from None
    monitor = Monitor(config)
    metrics_server = monitor.start_metrics()
    if config.enabled:
        LOG.info("Monitor enabled; ntfy %s, Graylog %s, Prometheus %s",
                 "enabled" if config.notify else "disabled",
                 "enabled" if config.graylog.enabled else "disabled",
                 "enabled" if config.metrics.enabled else "disabled")
        if config.graylog.enabled and not config.graylog.tls_verify:
            LOG.warning("Graylog TLS certificate verification is off (GRAYLOG_GELF_TLS_VERIFY=false)")
    else:
        LOG.info("Monitor disabled; idling (no database reads or notifications)")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            monitor.tick()
        except (sqlite3.Error, OSError, ValueError) as exc:
            LOG.error("Monitor state unavailable (%s); retry next tick", type(exc).__name__)
        stop.wait(TICK_SECONDS)
    if metrics_server is not None:
        metrics_server.shutdown()
        metrics_server.server_close()


if __name__ == "__main__":
    main()
