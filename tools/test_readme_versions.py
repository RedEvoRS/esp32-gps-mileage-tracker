"""The README's "Current versions" line must match firmware/VERSION and server/VERSION.

A PR that bumps either VERSION file fails this test until the README line is
updated in the same PR, so the line on main always shows the current numbers.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^\*\*Current versions:\*\* firmware \*\*(\S+)\*\* · server \*\*(\S+)\*\*$",
                  re.MULTILINE)


class ReadmeVersionsTests(unittest.TestCase):
    def test_readme_shows_current_versions(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        matches = LINE.findall(readme)
        self.assertEqual(len(matches), 1, "README.md needs exactly one 'Current versions' line")
        firmware = (ROOT / "firmware" / "VERSION").read_text(encoding="utf-8").strip()
        server = (ROOT / "server" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(matches[0], (firmware, server),
                         "Update the 'Current versions' line in README.md to "
                         f"firmware **{firmware}** · server **{server}**")


if __name__ == "__main__":
    unittest.main()
