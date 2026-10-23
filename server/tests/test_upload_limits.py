"""The ingest size cap, what an early-rejected upload gets back, and the
limits a device trip has to fit through (#10, #29, #30, #114).

    python -m unittest server/tests/test_upload_limits.py
"""

import importlib.util
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1]
APP_PATH = SERVER / "mileage-ingest" / "app.py"
DOCKERFILE = SERVER / "mileage-ingest" / "Dockerfile"
PARTITIONS = SERVER.parent / "firmware" / "04_trip_logger" / "partitions.csv"
SKETCH = PARTITIONS.with_name("04_trip_logger.ino")

TOKEN = "test-token"
CAP = 1024  # small, so an over-cap body is cheap to build

# A weak signal at the edge of the driveway. A full split piece has to
# upload at this speed before gunicorn's worker timeout.
SLOW_LINK_BYTES_PER_S = 10_000


def load_ingest(root, **env):
    env = {"INGEST_AUTH_TOKEN": TOKEN, "INGEST_DATA_ROOT": str(root),
           "DISPLAY_TZ": "America/Los_Angeles", **env}
    with mock.patch.dict(os.environ, env):
        if "INGEST_MAX_UPLOAD_BYTES" not in env:
            os.environ.pop("INGEST_MAX_UPLOAD_BYTES", None)
        spec = importlib.util.spec_from_file_location("ingest_limits", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def gpx(points):
    track = "".join(
        f'<trkpt lat="37.{i:06d}" lon="-122.0"><time>2026-04-14T14:30:{i % 60:02d}Z</time>'
        f"<speed>10.00</speed><hdop>0.9</hdop></trkpt>\n" for i in range(points)
    )
    return f'<gpx version="1.0"><trk><trkseg>\n{track}</trkseg></trk></gpx>\n'.encode()


def trip_partition_bytes():
    """Size of the LittleFS trip partition (LittleFS mounts "spiffs")."""
    for line in PARTITIONS.read_text().splitlines():
        fields = [f.strip() for f in line.split(",")]
        if fields[0] == "spiffs":
            return int(fields[4], 0)
    raise AssertionError(f"no spiffs partition in {PARTITIONS}")


def trip_split_bytes():
    """Read the sketch's limit, accepting an integer product only."""
    match = re.search(r"\bTRIP_SPLIT_BYTES\s*=\s*([\d\s*]+);", SKETCH.read_text())
    if not match:
        raise AssertionError("no numeric TRIP_SPLIT_BYTES constant in sketch")
    size = 1
    for factor in match.group(1).split("*"):
        size *= int(factor.strip())
    return size


class EarlyReplies(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ing = load_ingest(self.root, INGEST_MAX_UPLOAD_BYTES=str(CAP))
        self.client = self.ing.app.test_client()

    def post(self, payload, token=TOKEN):
        """POST from a stream, so the test can see how much was read."""
        stream = io.BytesIO(payload)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.post("/upload", input_stream=stream,
                                    content_length=len(payload), headers=headers)
        return response, stream.tell()

    def post_chunked(self, payload):
        """Gunicorn exposes dechunked bytes and marks the input terminated."""
        stream = io.BytesIO(payload)
        response = self.client.post(
            "/upload", headers={"Authorization": f"Bearer {TOKEN}",
                                "Transfer-Encoding": "chunked",
                                "X-Trip-Id": f"chunked-{len(payload)}"},
            environ_overrides={"wsgi.input": stream, "CONTENT_LENGTH": None,
                               "wsgi.input_terminated": True})
        return response, stream.tell()

    def test_chunked_over_cap_gets_json_413(self):  # #116
        payload = gpx(25)
        self.assertGreater(len(payload), CAP)
        response, read = self.post_chunked(payload)
        self.assertEqual(response.status_code, 413, response.get_json())
        self.assertEqual(response.get_json(), {
            "error": f"payload too large (limit {CAP} bytes)"})
        self.assertEqual(read, CAP + 1)
        self.assertEqual(list(self.root.glob("*/*")), [])

    def test_chunked_over_cap_whitespace_is_not_accepted(self):
        # Truncating at the cap used to accept this valid GPX prefix.
        payload = gpx(2).ljust(CAP + 1, b" ")
        response, read = self.post_chunked(payload)
        self.assertEqual(response.status_code, 413, response.get_json())
        self.assertEqual(read, CAP + 1)
        self.assertEqual(list(self.root.glob("*/*")), [])

    def test_chunked_at_or_just_under_cap_is_accepted(self):
        for size in (CAP - 1, CAP):
            with self.subTest(size=size):
                # Timed identity comes from GPX, so give each padded upload
                # its own start minute rather than relying on X-Trip-Id.
                payload = gpx(2).replace(b"14:30:", f"14:{size % 60:02d}:".encode()).ljust(size, b" ")
                response, read = self.post_chunked(payload)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(read, size)
                stored = self.root / response.get_json()["stored_as"]
                self.assertEqual(stored.read_bytes(), payload)

    def test_over_cap_gets_json_413(self):  # #10
        response, _ = self.post(gpx(20))
        self.assertEqual(response.status_code, 413)
        self.assertTrue(response.is_json, response.content_type)
        self.assertIn(f"limit {CAP} bytes", response.get_json()["error"])
        self.assertEqual(list(self.root.glob("*/*")), [])

    def test_413_reads_the_body_before_replying(self):  # #29
        payload = gpx(20)
        _, read = self.post(payload)
        self.assertEqual(read, len(payload))

    def test_401_reads_the_body_before_replying(self):  # #29
        for payload in (gpx(2), gpx(20)):
            for token in ("wrong-token", None):
                with self.subTest(token=token, size=len(payload)):
                    response, read = self.post(payload, token=token)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(read, len(payload))

    def test_body_past_the_discard_limit_is_left_unread(self):  # #29
        with mock.patch.object(self.ing, "MAX_DISCARD_BYTES", CAP + 1):
            response, read = self.post(gpx(20))
        self.assertEqual(response.status_code, 413)
        self.assertTrue(response.is_json)
        self.assertEqual(read, 0)

    def test_under_cap_is_still_accepted(self):
        payload = gpx(2)
        self.assertLess(len(payload), CAP)
        response, read = self.post(payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(read, len(payload))

    def test_content_length_exactly_at_cap_is_accepted(self):
        payload = gpx(2).ljust(CAP, b" ")
        response, read = self.post(payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(read, CAP)


class DefaultCap(unittest.TestCase):
    """The real default cap, not the small one EarlyReplies uses (#114)."""

    def test_default_cap_is_16_mib(self):
        # ~39 h of nonstop driving. Sized for the server's memory, not the
        # V1.2's partition: an upload peaks at ~9x its size while parsing.
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(load_ingest(root).MAX_UPLOAD_BYTES, 16 * 1024 * 1024)

    def test_discard_limit_stays_above_the_cap(self):
        # At or under the cap, every over-cap upload would be past the
        # discard limit too, and get a reset the device retries forever.
        for override in (None, str(64 * 1024 * 1024)):
            env = {} if override is None else {"INGEST_MAX_UPLOAD_BYTES": override}
            with self.subTest(cap=override or "default"),                     tempfile.TemporaryDirectory() as root:
                ing = load_ingest(root, **env)
                self.assertGreater(ing.MAX_DISCARD_BYTES, ing.MAX_UPLOAD_BYTES)

    def test_just_over_the_default_cap_gets_a_413_after_the_body(self):
        with tempfile.TemporaryDirectory() as root:
            ing = load_ingest(root)
            payload = b"x" * (ing.MAX_UPLOAD_BYTES + 1)
            stream = io.BytesIO(payload)
            response = ing.app.test_client().post(
                "/upload", input_stream=stream, content_length=len(payload),
                headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(response.status_code, 413)
            self.assertTrue(response.is_json, response.content_type)
            self.assertEqual(stream.tell(), len(payload))
            self.assertEqual(list(Path(root).glob("*/*")), [])


class DeviceTripsFit(unittest.TestCase):
    """Cross-checks between the firmware's storage and the server's limits."""

    def test_trip_split_is_at_most_half_the_default_cap(self):
        with tempfile.TemporaryDirectory() as root:
            cap = load_ingest(root).MAX_UPLOAD_BYTES
        self.assertGreater(trip_split_bytes(), 0)
        self.assertLessEqual(trip_split_bytes(), cap // 2,
                             "a trip piece must fit within half the upload cap")

    def test_fallback_partition_is_under_the_default_cap(self):
        with tempfile.TemporaryDirectory() as root:
            cap = load_ingest(root).MAX_UPLOAD_BYTES
        # Flash fallback still bounds a trip below the server cap.
        self.assertLess(trip_partition_bytes(), cap,
                        "a full trip partition would be rejected with a 413")

    def test_worker_timeout_lets_a_split_piece_through_a_slow_link(self):  # #30
        match = re.search(r'"--timeout",\s*"(\d+)"', DOCKERFILE.read_text())
        self.assertIsNotNone(match, "gunicorn's default 30 s timeout is too short")
        self.assertGreater(int(match.group(1)) * SLOW_LINK_BYTES_PER_S,
                           trip_split_bytes())


if __name__ == "__main__":
    unittest.main()
