"""Distance model tests for mileage-ingest (audit N1, N2, N8).

Stdlib only:  python -m unittest discover -s server/tests -v
(needs flask + tzdata installed, as in mileage-ingest/requirements.txt)
"""

import importlib.util
import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
_DATA = tempfile.mkdtemp()
os.environ.update(INGEST_AUTH_TOKEN="test-token", INGEST_DATA_ROOT=_DATA)
_spec = importlib.util.spec_from_file_location("ingest", SERVER / "mileage-ingest" / "app.py")
ing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ing)

LAT0, LON0 = 37.0, -122.0
# Degrees per metre on the same 6371 km sphere the haversine uses, so a
# synthetic "900 m" measures as 900 m.
M_LAT = 180.0 / (math.pi * 6371000.0)
M_LON = M_LAT / math.cos(math.radians(LAT0))
T0 = datetime(2026, 4, 14, 14, 30, 0, tzinfo=timezone.utc)


def pt(north_m, east_m, t, speed=None, hdop=0.9):
    return {"n": north_m, "e": east_m, "t": t, "speed": speed, "hdop": hdop}


def gpx(segments, version="1.0"):
    """Build a GPX document. version "1.0" mirrors the firmware's format."""
    ns = "http://www.topografix.com/GPX/1/0" if version == "1.0" else "http://www.topografix.com/GPX/1/1"
    out = [f'<?xml version="1.0"?><gpx version="{version}" xmlns="{ns}"><trk>']
    for seg in segments:
        out.append("<trkseg>")
        for p in seg:
            lat, lon = LAT0 + p["n"] * M_LAT, LON0 + p["e"] * M_LON
            s = f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
            if p["t"] is not None:
                s += f"<time>{(T0 + timedelta(seconds=p['t'])).strftime('%Y-%m-%dT%H:%M:%SZ')}</time>"
            if p["speed"] is not None:
                s += f"<speed>{p['speed']:.2f}</speed>"
            if p["hdop"] is not None:
                s += f"<hdop>{p['hdop']:.1f}</hdop>"
            out.append(s + "</trkpt>")
        out.append("</trkseg>")
    out.append("</trk></gpx>")
    return "".join(out).encode()


def summary(segments):
    return ing.summarize(ing.parse_gpx(gpx(segments)))


def meters(s):
    return s["distance_miles"] * ing.METERS_PER_MILE


def drive(n0, t0, seconds, mps):
    """Straight north at mps, one fix per second, starting after (n0, t0)."""
    return [pt(n0 + mps * (i + 1), 0, t0 + i + 1, mps) for i in range(seconds)]


def parked(n, t0, seconds, wander=0.3, seed=1):
    """Stationary fixes with a random-walk wander (m per fix), speed 0."""
    rnd = random.Random(seed)
    x = y = 0.0
    out = []
    for i in range(seconds):
        x += rnd.gauss(0, wander)
        y += rnd.gauss(0, wander)
        out.append(pt(n + y, x, t0 + i + 1, 0.0))
    return out


class DistanceModel(unittest.TestCase):
    def test_sample_gpx_unchanged(self):
        s = ing.summarize(ing.parse_gpx((SERVER / "sample.gpx").read_bytes()))
        self.assertEqual(s["distance_miles"], 0.295)
        self.assertEqual(s["num_points"], 4)
        self.assertEqual(s["summary_version"], ing.SUMMARY_VERSION)

    def test_parked_jitter_adds_nothing(self):  # N1
        s = summary([[pt(0, 0, 0, 0.0)] + parked(0, 0, 180)])
        self.assertEqual(meters(s), 0.0)

    def test_drive_stop_drive(self):  # N1: red light mid-trip
        a = [pt(0, 0, 0, 15.0)] + drive(0, 0, 60, 15.0)          # 900 m
        light = parked(900, 60, 60)                               # 60 s at a light
        b = drive(900, 120, 60, 15.0)                             # 900 m more
        s = summary([a + light + b])
        # Both drives, plus the step into and out of the light (|wander| small).
        self.assertAlmostEqual(meters(s), 1800, delta=8)

    def test_end_of_trip_cooldown_trimmed(self):  # N1 + N8
        a = [pt(0, 0, 0, 15.0)] + drive(0, 0, 60, 15.0)
        cooldown = parked(900, 60, 181)
        s = summary([a + cooldown])
        self.assertAlmostEqual(meters(s), 900, delta=4)
        self.assertEqual(s["end_time"], (T0 + timedelta(seconds=60)).isoformat())
        self.assertEqual(s["recorded_until"], (T0 + timedelta(seconds=241)).isoformat())
        self.assertEqual(s["duration_seconds"], 60)
        self.assertAlmostEqual(s["avg_speed_mph"], 15 * ing.MPS_TO_MPH, delta=0.2)

    def test_tunnel_gap_is_bridged(self):  # N2
        a = [pt(0, 0, 0, 25.0)] + drive(0, 0, 40, 25.0)          # 0..1000 m
        # 40 s in a tunnel at 25 m/s: no fixes, firmware starts a new trkseg
        b = [pt(2000 + 25 * i, 0, 80 + i, 25.0) for i in range(41)]  # 2000..3000 m
        s = summary([a, b])
        self.assertAlmostEqual(meters(s), 3000, delta=3)

    def test_gap_while_parked_adds_nothing(self):  # N2: garage, GPS lost
        a = [pt(0, 0, 0, 0.0), pt(0.5, 0, 1, 0.0)]
        b = [pt(4, 3, 600, 0.0), pt(4.2, 3, 601, 0.0)]           # reacquired 5 m off
        self.assertEqual(meters(summary([a, b])), 0.0)

    def test_implausible_gap_jump_not_counted(self):  # N2 guard
        a = [pt(0, 0, 0, 20.0), pt(20, 0, 1, 20.0)]
        b = [pt(5020, 0, 6, 20.0), pt(5040, 0, 7, 20.0)]         # 5 km in 5 s
        self.assertAlmostEqual(meters(summary([a, b])), 40, delta=1)

    def test_gap_without_timestamps_not_bridged(self):  # N2 guard
        a = [pt(0, 0, None, 20.0), pt(20, 0, None, 20.0)]
        b = [pt(1020, 0, None, 20.0), pt(1040, 0, None, 20.0)]
        self.assertAlmostEqual(meters(summary([a, b])), 40, delta=1)

    def test_single_glitch_fix_not_counted(self):  # plausibility
        seg = [pt(0, 0, 0, 15.0)] + drive(0, 0, 20, 15.0)
        seg[10] = pt(seg[10]["n"], 1500, seg[10]["t"], 15.0)      # 1.5 km sideways
        s = summary([seg])
        self.assertLess(meters(s), 300 + 1)                       # never the 3 km loop
        self.assertGreater(meters(s), 300 - 31)                   # loses ~2 steps at most

    def test_high_hdop_fix_skipped(self):  # N1: multipath
        seg = [pt(0, 0, 0, 15.0)] + drive(0, 0, 20, 15.0)
        seg[10] = pt(seg[10]["n"], 200, seg[10]["t"], 15.0, hdop=12.0)  # 200 m off, bad
        self.assertAlmostEqual(meters(summary([seg])), 300, delta=1)

    def test_no_device_speed_falls_back_to_distance_over_time(self):
        # Third-party GPX without <speed>: distance/time is the only signal,
        # and independent ~0.3 m scatter exceeds 2 mph on ~11% of 1 s steps.
        # So the fallback removes most jitter, not all of it; firmware files
        # carry Doppler speed and aren't affected (test_parked_jitter...).
        moving = [pt(15 * i, 0, i, None) for i in range(61)]     # 900 m
        rnd = random.Random(2)
        still = [pt(900 + rnd.gauss(0, 0.3), rnd.gauss(0, 0.3), 61 + i, None) for i in range(120)]
        unfiltered = sum(ing._haversine_m(LAT0 + a["n"] * M_LAT, LON0 + a["e"] * M_LON,
                                          LAT0 + b["n"] * M_LAT, LON0 + b["e"] * M_LON)
                         for a, b in zip(still, still[1:]))
        excess = meters(summary([moving + still])) - 900
        self.assertGreaterEqual(excess, -2)
        self.assertLess(excess, unfiltered * 0.35, f"{excess:.1f} m of {unfiltered:.1f} m jitter kept")

    def test_zero_second_trip_has_zero_duration(self):  # audit Low item
        s = summary([[pt(0, 0, 0, 5.0), pt(1, 0, 0, 5.0)]])
        self.assertEqual(s["duration_seconds"], 0)

    def test_gpx_11_with_garmin_extension_still_read(self):
        doc = (SERVER / "sample.gpx").read_bytes()
        self.assertIn(b"gpxtpx:speed", doc)
        s = ing.summarize(ing.parse_gpx(doc))
        self.assertGreater(s["max_speed_mph"], 23)


class UploadEndToEnd(unittest.TestCase):
    def test_firmware_style_trip(self):
        a = [pt(0, 0, 0, 15.0)] + drive(0, 0, 60, 15.0)
        body = gpx([a + parked(900, 60, 181)])
        c = ing.app.test_client()
        r = c.post("/upload", data=body, headers={"Authorization": "Bearer test-token"})
        self.assertEqual(r.status_code, 200, r.get_json())
        stored = Path(_DATA) / r.get_json()["stored_as"]
        rec = json.loads(stored.with_suffix(".json").read_text())
        # No X-Trip-Id, like the firmware: the id is the UTC start time,
        # and the summary, the response and the file name all agree (#11).
        self.assertEqual(stored.stem, "2026-04-14T14-30-00Z")
        self.assertEqual(rec["trip_id"], stored.stem)
        self.assertEqual(r.get_json()["trip_id"], stored.stem)
        self.assertAlmostEqual(rec["distance_miles"] * ing.METERS_PER_MILE, 900, delta=4)
        self.assertEqual(rec["summary_version"], ing.SUMMARY_VERSION)
        self.assertEqual(rec["duration_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
