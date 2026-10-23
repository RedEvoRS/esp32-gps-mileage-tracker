"""install.sh makes data/ private to its owner (#473).

Trip file names are drive start times, so another account on the server
shouldn't be able to list the folder. Sources the real setup_data_dir into
Bash and runs it in a scratch folder, with the container UID set to the
test user's own so no sudo is needed.
"""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest


INSTALL = Path(__file__).resolve().parents[1] / "install.sh"

HARNESS = textwrap.dedent("""\
    source <(sed '$d' "$INSTALL")
    MODE=$TEST_MODE
    CONTAINER_UID=$(id -u)
    cd "$WORK"
    setup_data_dir
    printf 'FAILURES=%s\n' "$FAILURES"
    """)


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "needs a POSIX shell")
class InstallerDataDir(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.data = self.work / "data"

    def mode(self):
        return stat.S_IMODE(self.data.stat().st_mode)

    def run_setup(self, mode):
        env = dict(os.environ, INSTALL=str(INSTALL), WORK=str(self.work), TEST_MODE=mode)
        proc = subprocess.run(["bash", "-c", HARNESS], env=env, text=True,
                              capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def test_new_data_dir_is_private(self):
        self.run_setup("install")
        self.assertEqual(self.mode(), 0o700)

    def test_existing_world_listable_data_dir_is_fixed_on_a_rerun(self):
        self.data.mkdir()
        self.data.chmod(0o755)
        out = self.run_setup("install")
        self.assertEqual(self.mode(), 0o700)
        self.assertIn("private", out)

    def test_check_warns_but_does_not_change_it(self):
        self.data.mkdir()
        self.data.chmod(0o755)
        out = self.run_setup("check")
        self.assertEqual(self.mode(), 0o755)
        self.assertIn("can be listed by other accounts", out)
        self.assertIn("FAILURES=0", out)


if __name__ == "__main__":
    unittest.main()
