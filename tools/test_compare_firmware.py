import json
from pathlib import Path
import tempfile
import unittest

from compare_firmware import compare


class FirmwareParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "firmware-src/firmware"
        self.current = self.root / "firmware-out/current"
        self.reference = self.root / "reference"
        for path in (self.source, self.current, self.reference):
            path.mkdir(parents=True)
        self.commit = "a" * 40
        self.version = "v0.10+" + self.commit[:10]
        (self.source / "COMMIT").write_text(self.commit)
        (self.source / "VERSION").write_text("v0.10\n")
        self.provenance = dict(commit=self.commit, version=self.version, fqbn="test-fqbn")
        self.manifest = dict(self.provenance, kind="normal")
        self.save()
        self.binary = self.version.encode() + b"\0" * 500000
        (self.current / "app.bin").write_bytes(self.binary)
        (self.reference / "04_trip_logger.ino.bin").write_bytes(self.binary)

    def save(self):
        (self.current / "manifest.json").write_text(json.dumps(self.manifest))
        (self.reference / "provenance.json").write_text(json.dumps(self.provenance))

    def test_allows_small_settings_size_difference(self):
        (self.current / "app.bin").write_bytes(self.binary + b"\0" * 200)
        compare(self.root, self.reference)

    def test_rejects_wrong_kind_and_provenance(self):
        for key, value in (("kind", "enroll"), ("commit", "b" * 40),
                           ("version", "unversioned"), ("fqbn", "other")):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    compare(self.root, self.reference)
                self.manifest[key] = original

    def test_rejects_missing_embedded_version_in_either_image(self):
        for image in (self.current / "app.bin", self.reference / "04_trip_logger.ino.bin"):
            with self.subTest(image=image.name):
                image.write_bytes(b"\0" * len(self.binary))
                with self.assertRaises(ValueError):
                    compare(self.root, self.reference)
                image.write_bytes(self.binary)

    def test_rejects_large_size_difference(self):
        (self.current / "app.bin").write_bytes(self.binary + b"\0" * 20000)
        with self.assertRaises(ValueError):
            compare(self.root, self.reference)
