"""Keep test clocks local to the module under test, including in threaded tests."""

import ast
from pathlib import Path
import unittest

CLOCK_FUNCTIONS = {"time", "monotonic", "sleep"}


def shared_clock_patches(tree):
    """Yield the line of each statement that replaces a shared time function."""
    for node in ast.walk(tree):
        candidates = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            # time.sleep = fake, or geocoding.time.sleep = fake
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            candidates = [ast.unparse(t).split(".") for t in targets if isinstance(t, ast.Attribute)]
        elif isinstance(node, ast.Call):
            function = ast.unparse(node.func)
            keywords = {k.arg: k.value for k in node.keywords if k.arg}
            first = node.args[0] if node.args else keywords.get("target")
            second = node.args[1] if len(node.args) >= 2 else keywords.get("attribute")
            if first is None:
                continue
            # A string target names the module path; anything else is an expression.
            base = str(first.value) if isinstance(first, ast.Constant) else ast.unparse(first)
            if function.split(".")[-1] == "patch" and isinstance(first, ast.Constant):
                # Covers both time.sleep and a service's imported time.sleep.
                candidates = [base.split(".")]
            elif function.endswith("patch.object") or function == "setattr":
                if isinstance(second, ast.Constant):
                    candidates = [[*base.split("."), str(second.value)]]
            elif function.endswith("patch.multiple"):
                candidates = [[*base.split("."), name] for name in keywords]
        for parts in candidates:
            if len(parts) >= 2 and parts[-2] == "time" and parts[-1] in CLOCK_FUNCTIONS:
                yield node.lineno


class TimePatchingTests(unittest.TestCase):
    def test_shared_clock_functions_are_not_patched(self):
        violations = []
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(f"{path.name}:{line}" for line in shared_clock_patches(tree))
        self.assertFalse(
            violations,
            "Patching shared time functions changes the clock for every thread: "
            + ", ".join(violations)
            + ". Patch the module's time name with time_helpers.time_stub(...) instead.",
        )

    def test_guard_recognises_each_way_of_replacing_a_shared_clock(self):
        caught = [
            'patch("time.sleep")',
            'patch("geocoding.time.monotonic")',
            'mock.patch(target="time.time")',
            'patch.object(time, "sleep")',
            'patch.object(target=geocoding.time, attribute="time")',
            'patch.multiple(time, sleep=fake)',
            'patch.multiple("geocoding.time", monotonic=fake)',
            'setattr(time, "sleep", fake)',
            "time.sleep = fake",
            "geocoding.time.monotonic = fake",
        ]
        allowed = [
            'patch("geocoding.time", stub)',
            'patch.object(geocoding, "time", stub)',
            'patch.multiple(geocoding, time=stub)',
            'patch("monitor.time")',
            "stub.sleep = fake",
            'note = "time.sleep = fake"',
        ]
        for source in caught:
            self.assertTrue(list(shared_clock_patches(ast.parse(source))), source)
        for source in allowed:
            self.assertFalse(list(shared_clock_patches(ast.parse(source))), source)
