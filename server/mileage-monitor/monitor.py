"""Optional, stdlib-only diagnostics policy, ntfy alerts and external exports.

Ingest owns events.db. This process only opens it in mode=ro, never immutable.
All mutable policy, queue, retry and rate-limit state belongs to monitor.db.
"""

import json
import logging
import os
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

from common.version import server_version

from common.diagnostics import CatalogError, load_catalog, validate_device_id, validate_request  # noqa: E402

import gelf  # noqa: E402
import loki as loki_module  # noqa: E402
import prometheus  # noqa: E402
import shared  # noqa: E402
from shared import NoRedirect, SNAPSHOT_MAX_AGE_SECONDS  # noqa: E402
import settings_rules  # noqa: E402



LOG = logging.getLogger("mileage-monitor")
SEVERITIES = settings_rules.SEVERITIES
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
    loki: loki_module.LokiConfig = field(default_factory=loki_module.LokiConfig)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def boolean(name):
            value = env.get(name, "false").lower()
            if not settings_rules.valid(name, value):
                raise ValueError(name + " must be true or false")
            return value == "true"

        metrics = prometheus.MetricsConfig.from_env(env)
        loki_config = loki_module.LokiConfig.from_env(env)
        enabled = boolean("MONITOR_ENABLED")
        if not enabled:
            if metrics.enabled:
                raise ValueError("METRICS_ENABLED requires MONITOR_ENABLED=true")
            if loki_config.enabled:
                raise ValueError("LOKI_PUSH_URL requires MONITOR_ENABLED=true")
            return cls()  # Inactive profile can carry unfilled example settings.
        notify = boolean("NOTIFY_ENABLED")

        def number(name, default):
            try:
                raw = env.get(name, str(default))
                value = int(raw) if settings_rules.NUMBERS[name][2] else float(raw)
                if not settings_rules.valid(name, raw):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise ValueError(name + " is outside its supported numeric range") from None
            return value

        severity = env.get("NOTIFY_MIN_SEVERITY", "error")
        if not settings_rules.valid("NOTIFY_MIN_SEVERITY", severity):
            raise ValueError("NOTIFY_MIN_SEVERITY must be info, warning, error or critical")
        hold = number("NOTIFY_HOLD_SECONDS", 300)
        repeat = number("NOTIFY_REPEAT_HOURS", 24) * 3600
        maximum = number("NOTIFY_MAX_PER_HOUR", 6)
        stale = number("MONITOR_STALE_DAYS", 7) * 86400
        url, token = env.get("NTFY_URL", ""), env.get("NTFY_TOKEN", "")
        if notify:
            valid = settings_rules.valid("NTFY_URL", url)
            if not valid:
                raise ValueError("NTFY_URL must be an http(s) topic URL without credentials, query or fragment")
            if not settings_rules.valid("NTFY_TOKEN", token):
                raise ValueError("NTFY_TOKEN must be a nonempty access token without whitespace")
        return cls(enabled=enabled, notify=notify, url=url, token=token,
                   min_severity=severity, hold_seconds=hold, repeat_seconds=repeat,
                   max_per_hour=maximum, stale_seconds=stale,
                   graylog=gelf.GelfConfig.from_env(env), metrics=metrics, loki=loki_config)


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
                 gelf_sender=None, loki_sender=None):
        self.config = config
        try:
            self.catalog = load_catalog() if catalog is None else catalog
        except CatalogError as exc:
            raise ValueError("Cannot start monitor: missing or invalid diagnostics catalog.json") from exc
        self.codes = {row["code"]: row for row in self.catalog["codes"]}
        self.events_path, self.state_path = Path(events_path), Path(state_path)
        self.heartbeat_path = self.state_path.with_name("heartbeat")
        if self.events_path.resolve() == self.state_path.resolve():
            raise ValueError("monitor state must be separate from diagnostics")
        self.clock = clock
        self.sender = NtfySender(config) if sender is None else sender
        self.gelf_sender = (gelf.transport(config.graylog) if gelf_sender is None and config.graylog.enabled
                            else gelf_sender)
        self.loki_sender = (loki_module.transport(config.loki) if loki_sender is None and config.loki.enabled
                            else loki_sender)
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
                # Export tables are additive to schema v1 (CREATE IF NOT EXISTS),
                # so an existing monitor.db gains them without a migration.
                for statement in (SCHEMA + gelf.SCHEMA + loki_module.SCHEMA).split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT OR IGNORE INTO schema_version VALUES (1,1)")
                db.execute("INSERT OR IGNORE INTO retry VALUES (1,0,0)")
                for statement in gelf.SEED + loki_module.SEED:
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
        known = {(device, code) for device, code in db.execute("SELECT device,code FROM episodes")}
        states = shared.condition_states(devices, known, now, self.config.stale_seconds)
        by_device = {device["device_id"]: device for device in devices}
        clear_codes = set()
        for (name, code), (state, episode, contact) in states.items():
            if state == "unknown":
                continue
            if code == shared.SILENCE:
                clear_codes.add((name, code))
                # Newer contact alone can resolve a silence episode; a clock
                # jump with unchanged contact must never manufacture recovery.
                db.execute("UPDATE episodes SET state='clear', eligible_since=NULL "
                           "WHERE device=? AND code=? AND contact<?", (name, code, contact))
                if state == "active":
                    self._active(db, name, code, episode, 0, contact, now, previous)
                continue
            revision = by_device[name]["snapshot"]["status_sequence"]
            if state == "clear":
                clear_codes.add((name, code))
                db.execute("UPDATE episodes SET state='clear', eligible_since=NULL "
                           "WHERE device=? AND code=? AND revision<?", (name, code, revision))
            elif self.eligible(code):
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
            completed = self._tick()
            if completed:
                # Disabled and not-yet-enrolled installs are healthy. Unreadable
                # diagnostics return False; no useful monitoring completed.
                self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                self.heartbeat_path.touch()
            return completed
        except sqlite3.Error:
            if self.metrics_snapshot is not None:
                self.metrics_snapshot.record_read_error()
            raise

    def _tick(self):
        if not self.config.enabled:
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
                    delay = shared.retry_delay(attempts)
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
                        if self.config.loki.enabled:
                            db.execute("INSERT INTO loki_decisions(occurred_at,device,code,decision) "
                                       "VALUES (?,?,?,?)", (now, row[1], row[2], row[4]))
                        db.execute("DELETE FROM work WHERE id=?", (row[0],))
                    db.execute("INSERT INTO deliveries VALUES (?)", (now,))
                    db.execute("DELETE FROM outbox")
                    db.execute("UPDATE retry SET attempts=0,next_attempt=0 WHERE singleton=1")
                LOG.info("ntfy delivered %s condition update(s)", len(rows))
            try:
                self._forward_gelf(db, devices, now)
            except Exception as exc:  # Graylog problems must never affect ntfy or trips.
                if isinstance(exc, sqlite3.Error) and self.metrics_snapshot is not None:
                    self.metrics_snapshot.record_read_error()
                LOG.warning("Graylog export unavailable (%s); retry next tick", type(exc).__name__)
            try:
                self._forward_loki(db, devices, now)
            except Exception as exc:  # Loki outages never affect ntfy or Graylog.
                if isinstance(exc, sqlite3.Error) and self.metrics_snapshot is not None:
                    self.metrics_snapshot.record_read_error()
                LOG.warning("Loki export unavailable (%s); retry next tick", type(exc).__name__)
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
        changes = shared.desired_conditions(devices, known, now, self.config.stale_seconds,
                                          SNAPSHOT_MAX_AGE_SECONDS)
        # Items are (kind, key, payload); payload None marks a damaged event
        # that is skipped but still advances the cursor once reached.
        items = [("condition", (key, value), gelf.condition_message(
                    self.codes, key[0], key[1], value[0], value[1], now, value[2]))
                 for key, value in sorted(changes.items())][:gelf.MAX_PER_TICK]
        cursor = db.execute("SELECT event_rowid FROM gelf_cursor WHERE singleton=1").fetchone()[0]
        highest, rows, anchor, pruned = shared.read_events(self.events_path, cursor, max(0, gelf.MAX_PER_TICK - len(items)))
        if highest < cursor or shared.cursor_changed(db, "gelf", cursor, anchor, pruned):
            # events.db was replaced (e.g. restored from a backup): start over.
            # Graylog may see duplicates; it never silently misses events.
            LOG.warning("Diagnostics event identity changed at the Graylog cursor; resending from the start")
            with db:
                db.execute("UPDATE gelf_cursor SET event_rowid=0 WHERE singleton=1")
                db.execute("DELETE FROM event_cursor_markers WHERE sink='gelf'")
            return
        fingerprints = {row[0]: shared.event_fingerprint(row) for row in rows}
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
                    shared.save_cursor_marker(db, "gelf", key, fingerprints[key])
            if error is None:
                db.execute("UPDATE gelf_retry SET attempts=0,next_attempt=0 WHERE singleton=1")
            else:
                attempts += 1
                delay = shared.retry_delay(attempts)
                db.execute("UPDATE gelf_retry SET attempts=?,next_attempt=? WHERE singleton=1",
                           (attempts, now + delay))
        if error is None:
            LOG.info("Graylog accepted %s message(s)", delivered)
        else:
            LOG.warning("Graylog delivery failed (%s) after %s message(s); retry in %s seconds",
                        error, delivered, delay)

    def _forward_loki(self, db, devices, now):
        """Push condition changes, new events and delivered-alert lines."""
        if not self.config.loki.enabled:
            return
        attempts, next_attempt = db.execute(
            "SELECT attempts,next_attempt FROM loki_retry WHERE singleton=1").fetchone()
        if now < next_attempt:
            return
        event_cursor, decision_cursor = db.execute(
            "SELECT event_rowid,decision_id FROM loki_cursor WHERE singleton=1").fetchone()
        highest, rows, anchor, pruned = shared.read_events(self.events_path, event_cursor, loki_module.MAX_PER_TICK)
        if highest < event_cursor or shared.cursor_changed(db, "loki", event_cursor, anchor, pruned):
            # A restored events.db can be shorter than this sink's cursor.
            # Start its history over without changing GELF progress.
            with db:
                db.execute("UPDATE loki_cursor SET event_rowid=0 WHERE singleton=1")
                db.execute("DELETE FROM event_cursor_markers WHERE sink='loki'")
            LOG.warning("Diagnostics event identity changed at the Loki cursor; resending from the start")
            return

        # While an event backlog drains, send only events so each stream stays
        # in ascending time order; a newer condition or decision line first
        # would push older backlog outside Loki's out-of-order window.
        backlog = len(rows) >= loki_module.MAX_PER_TICK
        condition_items = []
        if not backlog:
            known_conditions = {(device, code): (state, episode) for device, code, state, episode in
                                db.execute("SELECT device,code,state,episode FROM loki_conditions")}
            changes = shared.desired_conditions(devices, known_conditions, now, self.config.stale_seconds,
                                              SNAPSHOT_MAX_AGE_SECONDS)
            for key, value in sorted(changes.items())[:loki_module.MAX_PER_TICK - len(rows)]:
                item = loki_module.condition_log(self.codes, key[0], key[1], value[0], now)
                if item is None:
                    LOG.warning("Skipping a damaged monitor condition for Loki")
                condition_items.append((key, value, item))

        selected_events = []
        push_items = [item for _, _, item in condition_items if item is not None]
        for row in rows:
            selected_events.append(row[0])
            item = loki_module.event_log(self.catalog, row)
            if item is None:
                LOG.warning("Skipping a damaged diagnostics event for Loki")
            else:
                push_items.append(item)
        decision_limit = max(0, loki_module.MAX_PER_TICK - len(condition_items) - len(rows))
        decisions = db.execute(
            "SELECT id,occurred_at,device,code,decision FROM loki_decisions "
            "WHERE id>? ORDER BY id LIMIT ?", (decision_cursor, decision_limit)).fetchall()
        for row in decisions:
            item = loki_module.decision_log(self.codes, row)
            if item is None:
                LOG.warning("Skipping a damaged monitor decision for Loki")
            else:
                push_items.append(item)
        if not condition_items and not rows and not decisions:
            return

        rejection_status = None
        if push_items:
            try:
                self.loki_sender(loki_module.push_payload(push_items))
            except loki_module.PermanentRejection as exc:
                rejection_status = exc.status_code
            except Exception as exc:
                attempts += 1
                delay = shared.retry_delay(attempts)
                with db:
                    db.execute("UPDATE loki_retry SET attempts=?,next_attempt=? WHERE singleton=1",
                               (attempts, now + delay))
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    # Backoff already bounds retries; keep settings failures
                    # visible at the default log level until delivery recovers.
                    LOG.warning("Loki delivery failed (HTTP %s); check token, tenant, push URL or proxy; "
                                "retry in %s seconds without advancing cursors", exc.code, delay)
                else:
                    LOG.warning("Loki delivery failed (%s); retry in %s seconds", type(exc).__name__, delay)
                return

        next_event = max(selected_events, default=event_cursor)
        next_decision = max((row[0] for row in decisions), default=decision_cursor)
        with db:
            db.execute("UPDATE loki_cursor SET event_rowid=?,decision_id=? WHERE singleton=1",
                       (next_event, next_decision))
            if rows:
                shared.save_cursor_marker(db, "loki", next_event, shared.event_fingerprint(rows[-1]))
            for (device, code), (state, episode, _), _ in condition_items:
                db.execute("INSERT INTO loki_conditions VALUES (?,?,?,?) ON CONFLICT(device,code) "
                           "DO UPDATE SET state=excluded.state, episode=excluded.episode",
                           (device, code, state, episode))
            db.execute("DELETE FROM loki_decisions WHERE id<=?", (next_decision,))
            db.execute("UPDATE loki_retry SET attempts=0,next_attempt=0 WHERE singleton=1")
        if rejection_status is not None:
            LOG.warning("Loki permanently rejected a batch with HTTP status %s; skipping it",
                        rejection_status)
        elif push_items:
            LOG.info("Loki accepted %s log line(s)", len(push_items))


def log_unexpected_tick(exc):
    """Log a traceback without exception text, source, locals or private paths."""
    frames = ["Traceback (frames only; exception messages, source and locals omitted):"]
    trace = exc.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        # Only these reviewed source names/functions can enter the log. A
        # library or dynamically compiled filename might itself contain secrets.
        filename = Path(code.co_filename)
        if (filename.parent == Path(__file__).parent and filename.name in
                {"monitor.py", "shared.py", "gelf.py", "loki.py", "prometheus.py"}):
            name, function = filename.name, code.co_name
        else:
            name, function = "<external>", "<external>"
        frames.append(f'  File "{name}", line {trace.tb_lineno}, in {function}')
        trace = trace.tb_next
    LOG.error("Unexpected monitor tick failure (%s); retry next tick\n%s",
              type(exc).__name__, "\n".join(frames))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("Server version: %s", server_version())
    try:
        config = Config.from_env()
    except ValueError as exc:
        LOG.error("Invalid monitor configuration: %s", exc)
        raise SystemExit(2) from None
    monitor = Monitor(config)
    metrics_server = monitor.start_metrics()
    if config.enabled:
        LOG.info("Monitor enabled; ntfy %s, Graylog %s, Prometheus %s, Loki %s",
                 "enabled" if config.notify else "disabled",
                 "enabled" if config.graylog.enabled else "disabled",
                 "enabled" if config.metrics.enabled else "disabled",
                 "enabled" if config.loki.enabled else "disabled")
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
        except Exception as exc:
            log_unexpected_tick(exc)
        stop.wait(TICK_SECONDS)
    if metrics_server is not None:
        metrics_server.shutdown()
        metrics_server.server_close()


if __name__ == "__main__":
    main()
