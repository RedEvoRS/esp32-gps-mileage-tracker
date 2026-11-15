"""Keep test clocks local to the module under test, including in threaded tests."""

import ast
from pathlib import Path
import unittest


class TimePatchingTests(unittest.TestCase):
    def test_shared_clock_functions_are_not_patched(self):
        violations = []
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = ast.unparse(node.func)
                target = node.args[0]
                if function.split(".")[-1] == "patch" and isinstance(target, ast.Constant):
                    # Covers both time.sleep and a service's imported time.sleep.
                    parts = str(target.value).split(".")
                elif function.endswith("patch.object") and len(node.args) >= 2:
                    attribute = node.args[1]
                    if not isinstance(attribute, ast.Constant):
                        continue
                    parts = [*ast.unparse(target).split("."), str(attribute.value)]
                else:
                    continue
                if len(parts) >= 2 and parts[-2] == "time" and parts[-1] in {"time", "monotonic", "sleep"}:
                    violations.append(f"{path.name}:{node.lineno}")
        self.assertFalse(
            violations,
            "Patching shared time functions changes the clock for every thread: "
            + ", ".join(violations)
            + ". Patch the module's time name with time_helpers.time_stub(...) instead.",
        )
