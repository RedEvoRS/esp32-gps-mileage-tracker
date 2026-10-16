"""DISPLAY_TZ has no default: both services refuse to start without it,
and whatever zone is set decides the month folder and the times shown.

install.sh asks for the zone and writes it to .env; Compose hands the
same value to both services.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from test_round_trip import SERVER, load_service


def service_env(root, zone):
    env = {
        "INGEST_DATA_ROOT": str(root / "data"),
        "INGEST_AUTH_TOKEN": "display-tz-test-token",
        "VIEWER_DB_PATH": str(root / "annotations.db"),
        "VIEWER_USERNAME": "driver",
        "VIEWER_PASSWORD_HASH": generate_password_hash("pw", "pbkdf2:sha256:1000"),
        "VIEWER_SECRET_KEY": "display-tz-session-key-" * 3,
    }
    if zone is not None:
        env["DISPLAY_TZ"] = zone
    return env


class DisplayTimezone(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def load(self, name, zone):
        with patch.dict(os.environ, service_env(self.root, zone)):
            if zone is None:
                os.environ.pop("DISPLAY_TZ", None)
            return load_service(name)

    def test_both_services_refuse_to_start_without_a_zone(self):
        for name in ("mileage-ingest", "mileage-viewer"):
            for zone in (None, "", "   "):
                with self.subTest(service=name, zone=zone):
                    with self.assertRaisesRegex(RuntimeError, "DISPLAY_TZ is not set"):
                        self.load(name, zone)

    def test_both_services_reject_an_unknown_zone(self):
        for name in ("mileage-ingest", "mileage-viewer"):
            with self.subTest(service=name):
                with self.assertRaisesRegex(RuntimeError, "not a valid"):
                    self.load(name, "Mars/Olympus_Mons")

    def test_the_chosen_zone_sets_the_month_folder_and_the_page(self):
        # 22:30 UTC on April 30 is already May 1 in Berlin. In Los Angeles
        # it would still be April, so this only passes if the setting is used.
        ingest = self.load("mileage-ingest", "Europe/Berlin")
        viewer = self.load("mileage-viewer", "Europe/Berlin")
        raw = re.sub(rb"<time>[^<]*</time>", b"", (SERVER / "sample.gpx").read_bytes())
        headers = {"Authorization": "Bearer display-tz-test-token",
                   "X-Device-Id": "test-device"}
        with patch.object(ingest, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 4, 30, 22, 30, tzinfo=timezone.utc)
            uploaded = ingest.app.test_client().post("/upload", data=raw, headers=headers)
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(Path(uploaded.json["stored_as"]).parent.name, "2026-05")

        client = viewer.app.test_client()
        with client.session_transaction() as session:
            session["user"] = "driver"
            session["pw_tag"] = viewer._password_tag()
        page = client.get("/").get_data(as_text=True)
        self.assertIn("Europe/Berlin", page)
        self.assertNotIn("America/Los_Angeles", page)


if __name__ == "__main__":
    unittest.main()
