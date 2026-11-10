"""Keep standalone flash safety fences in sync with the sketch partition table."""

import csv
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
# ESP-IDF's ESP32-S3 default (CONFIG_PARTITION_TABLE_OFFSET); the table uses
# one 4 KiB sector. These are platform defaults, not entries in partitions.csv.
# https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32s3/api-guides/partition-tables.html
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0x1000


def partition_regions():
    with (ROOT / "firmware/04_trip_logger/partitions.csv").open(encoding="utf-8") as source:
        rows = csv.reader(line for line in source if line.strip() and not line.lstrip().startswith("#"))
        regions = {}
        for row in rows:
            name, _, _, offset, size = (cell.strip() for cell in row[:5])
            # Explicit offsets only: a new layout using automatic offsets needs
            # a deliberate update to this parser rather than silently skipping it.
            def number(value):
                suffix = value[-1:].upper()
                return int(value[:-1], 0) * {"K": 1024, "M": 1024**2}[suffix] if suffix in ("K", "M") else int(value, 0)
            start = number(offset)
            regions[name] = (start, start + number(size))
    return regions


def expected_limits():
    regions = partition_regions()
    return {
        "bootloader": (PARTITION_TABLE_OFFSET,),
        "partitions": (PARTITION_TABLE_OFFSET, PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE),
        "boot_app0": regions["otadata"],
        "app": regions["factory"],
    }


def script_limits(source, powershell=False):
    """Parse whole fence lines; unknown syntax must fail, never omit a fence."""
    number = r"(0x[0-9a-fA-F]+|[0-9]+)"
    result = {}
    for name in expected_limits():
        if powershell:
            start = r"\$start\s+-ge\s+" + number + r"\s+-and\s+"
            end = r"\$end\s+-le\s+" + number
            extra = r"\s+-and\s+\$part\.offset\s+-ceq\s+\$m\.app_offset" if name == "app" else ""
            pattern = rf"\s*'{name}'\s*\{{\s*" + ("" if name == "bootloader" else start) + end + extra + r"\s*\}\s*"
        else:
            start = r'\[\s*"\$start"\s+-ge\s+\$\(\(' + number + r"\)\)\s*\]\s*&&\s*"
            end = r'\[\s*"\$end"\s+-le\s+\$\(\(' + number + r"\)\)\s*\]"
            pattern = rf"\s*{name}\)\s*" + ("" if name == "bootloader" else start) + end + r"\s*\|\|\s*return\s+1\s*;;\s*"
        matches = [re.fullmatch(pattern, line) for line in source.splitlines()]
        matches = [match for match in matches if match]
        if len(matches) != 1:
            raise AssertionError(f"Expected one recognizable {name} fence; found {len(matches)}. Update the fence parser if syntax changed.")
        result[name] = tuple(int(value, 0) for value in matches[0].groups())
    return result


class FlashPartitionLimitsTests(unittest.TestCase):
    def test_both_scripts_match_partition_table(self):
        regions = partition_regions()
        self.assertLessEqual(PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE, min(start for start, _ in regions.values()))
        for filename in ("flash.sh", "flash.ps1"):
            with self.subTest(script=filename):
                source = (ROOT / "install" / filename).read_text(encoding="utf-8")
                self.assertEqual(script_limits(source, filename.endswith("ps1")), expected_limits())

    def test_every_changed_limit_is_detected(self):
        for filename in ("flash.sh", "flash.ps1"):
            source = (ROOT / "install" / filename).read_text(encoding="utf-8")
            for line in source.splitlines():
                if not re.match(r"\s*(?:'(?:bootloader|partitions|boot_app0|app)'\s*\{|(?:bootloader|partitions|boot_app0|app)\))", line):
                    continue
                for match in re.finditer(r"0x[0-9a-fA-F]+", line):
                    changed = line[:match.start()] + hex(int(match[0], 0) + 4096) + line[match.end():]
                    with self.subTest(script=filename, fence=line.strip(), limit=match[0]):
                        self.assertNotEqual(script_limits(source.replace(line, changed), filename.endswith("ps1")), expected_limits())

    def test_unrecognized_or_missing_fence_fails_loudly(self):
        for filename in ("flash.sh", "flash.ps1"):
            source = (ROOT / "install" / filename).read_text(encoding="utf-8")
            app_line = next(line for line in source.splitlines() if re.match(r"\s*(?:'app'\s*\{|app\))", line))
            changed = re.sub(r"0x[0-9a-fA-F]+", "APP_LIMIT", app_line)
            with self.subTest(script=filename), self.assertRaisesRegex(AssertionError, "recognizable app fence"):
                script_limits(source.replace(app_line, changed), filename.endswith("ps1"))
