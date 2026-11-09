"""Timed trip collisions preserve both devices and immutable upload identity."""

import hashlib
import json
import unittest
from zoneinfo import ZoneInfo

import test_ingest_audit as audit


class IngestDeviceCollisionTests(unittest.TestCase):
    setUp = audit.IngestAuditTests.setUp
    upload = audit.IngestAuditTests.upload

    def suffix_id(self, device):
        return "2026-07-01T02-50-00Z-" + hashlib.sha256(device.encode("utf-8")).hexdigest()[:12]

    def pair(self):
        self.ing.DISPLAY_TZ = ZoneInfo("America/Chicago")
        first = self.upload(audit.gpx("2026-07-01T02:50:00Z"), **{"X-Device-Id": "device-a"})
        second_raw = audit.gpx("2026-07-01T02:50:00Z", speed="2")
        second = self.upload(second_raw, **{"X-Device-Id": "device-b"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        return first, second, second_raw

    def test_different_devices_keep_ids_bytes_receipts_and_month_after_timezone_change(self):
        first, second, raw = self.pair()
        self.assertEqual(first.json["trip_id"], "2026-07-01T02-50-00Z")
        self.assertEqual(second.json["trip_id"], self.suffix_id("device-b"))
        paths = [self.root / r.json["stored_as"] for r in (first, second)]
        before = {p: p.with_suffix(".json").read_bytes() for p in paths}
        self.assertTrue(all(p.parent.name == "2026-06" for p in paths))
        self.ing.DISPLAY_TZ = ZoneInfo("Asia/Tokyo")
        for original, payload, device in ((first, audit.gpx("2026-07-01T02:50:00Z"), "device-a"),
                                          (second, raw, "device-b")):
            retry = self.upload(payload, **{"X-Device-Id": device, "X-Trip-Id": "ignored"})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json, original.json)
        for path in paths:
            record = json.loads(before[path])
            self.assertEqual(path.with_suffix(".json").read_bytes(), before[path])
            self.assertEqual(record["trip_id"], path.stem)
            self.assertEqual(record["gpx_file"], path.name)
        self.assertEqual(paths[1].read_bytes(), raw)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 2)

    def test_creator_fallback_is_used_when_device_header_is_absent(self):
        for creator in ("creator-a", "creator-b"):
            raw = audit.gpx("2026-07-01T02:50:00Z", creator=creator)
            first = self.upload(raw, **{"X-Device-Id": ""})
            self.assertEqual(first.status_code, 200)
            retry = self.upload(raw, **{"X-Device-Id": ""})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json, first.json)
        self.assertEqual(first.json["trip_id"], self.suffix_id("creator-b"))

    def test_same_device_changed_gpx_conflicts_for_both_plain_and_suffixed_ids(self):
        self.pair()
        before = {p: p.read_bytes() for p in self.root.glob("*/*") if p.is_file()}
        for device in ("device-a", "device-b"):
            response = self.upload(audit.gpx("2026-07-01T02:50:00Z", speed="3"),
                                   **{"X-Device-Id": device})
            self.assertEqual(response.status_code, 409)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 2)

    def test_identical_gpx_from_another_device_keeps_existing_conflict(self):
        raw = audit.gpx()
        self.assertEqual(self.upload(raw, **{"X-Device-Id": "device-a"}).status_code, 200)
        self.assertEqual(self.upload(raw, **{"X-Device-Id": "device-b"}).status_code, 409)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)

    def test_unknown_stored_device_does_not_authorize_a_suffix(self):
        first = self.upload(audit.gpx(), **{"X-Device-Id": "device-a"})
        path = (self.root / first.json["stored_as"]).with_suffix(".json")
        for damaged in (b"not-json", b"{}", None):
            if damaged is None:
                path.unlink()
            else:
                path.write_bytes(damaged)
            response = self.upload(audit.gpx(speed="2"), **{"X-Device-Id": "device-b"})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 1)

    def test_suffixed_partial_pairs_are_repaired_in_the_original_month(self):
        _, second, raw = self.pair()
        saved = self.root / second.json["stored_as"]
        self.ing.DISPLAY_TZ = ZoneInfo("Asia/Tokyo")
        for path in (saved.with_suffix(".json"), saved):
            path.unlink()
            retry = self.upload(raw, **{"X-Device-Id": "device-b"})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json, second.json)
            self.assertTrue(saved.exists())
            self.assertTrue(saved.with_suffix(".json").exists())
        self.assertEqual(len(list(self.root.glob("*/*.gpx"))), 2)

    def test_multiple_months_for_suffixed_id_are_a_conflict(self):
        _, second, raw = self.pair()
        saved = self.root / second.json["stored_as"]
        duplicate = self.root / "2026-07" / saved.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(raw)
        retry = self.upload(raw, **{"X-Device-Id": "device-b"})
        self.assertEqual(retry.status_code, 409)
        self.assertIn("multiple month", retry.json["error"])

    def test_untimed_custom_ids_cannot_claim_either_timed_namespace(self):
        for trip_id in ("2026-07-01T02-50-00Z", self.suffix_id("device-b")):
            response = self.upload(audit.gpx(None), **{"X-Trip-Id": trip_id})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.root.glob("*/*.gpx")), [])
