"""Validate the data source inputs of every importable Grafana dashboard."""

import json
import re
import unittest
from pathlib import Path


GRAFANA = Path(__file__).resolve().parents[1] / "grafana"


class DashboardInputs(unittest.TestCase):
    def setUp(self):
        self.dashboards = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(GRAFANA.glob("*.json"))
        }
        self.assertTrue(self.dashboards, "No Grafana dashboards found")

    def test_input_types(self):
        for name, dashboard in self.dashboards.items():
            for entry in dashboard.get("__inputs", []):
                with self.subTest(dashboard=name, input=entry.get("name")):
                    self.assertEqual(entry.get("type"), "datasource")

    def test_datasource_placeholders_have_inputs(self):
        for name, dashboard in self.dashboards.items():
            with self.subTest(dashboard=name):
                placeholders = set(re.findall(r"\$\{(DS_[^}]+)\}",
                                              json.dumps(dashboard)))
                inputs = {entry["name"] for entry in dashboard.get("__inputs", [])}
                self.assertFalse(placeholders - inputs,
                                 f"Undeclared data source inputs: {sorted(placeholders - inputs)}")


if __name__ == "__main__":
    unittest.main()
