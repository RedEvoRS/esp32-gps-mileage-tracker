"""Management and authentication behavior for a login that now matches admin."""

import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing

from werkzeug.security import check_password_hash

import test_viewer_roles as roles


CLI = roles.SERVER / "mileage-viewer/manage_users.py"


def run_cli(db, *args, password=b""):
    env = os.environ.copy()
    for name in ("DISPLAY_TZ", "VIEWER_SECRET_KEY", "VIEWER_PASSWORD_HASH", "VIEWER_USERNAME"):
        env.pop(name, None)
    env["VIEWER_DB_PATH"] = str(db)
    env["VIEWER_USERNAME"] = "driver"
    return subprocess.run([sys.executable, str(CLI), *args], input=password,
                          capture_output=True, env=env, timeout=30)


class ViewerAdminClashTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    login = roles.ViewerRolesTests.login

    def seed_clashing_user(self, disabled=0):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM users")
            conn.execute(
                "INSERT INTO users (username, password_hash, disabled, session_version, created_at, updated_at) "
                "VALUES ('DRIVER', ?, ?, 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (roles.EDITOR_HASH, disabled),
            )

    def test_change_user_can_manage_a_clashing_login(self):
        for action in ("disable", "enable", "remove", "reset-password"):
            with self.subTest(action=action):
                self.seed_clashing_user(disabled=1 if action == "enable" else 0)
                updated = self.viewer.users.change_user(
                    self.db, action, "DRIVER",
                    password="new-password" if action == "reset-password" else None,
                    admin_username="driver",
                )
                user = self.viewer.users.get_user(self.db, "DRIVER")
                if action == "remove":
                    self.assertIsNone(updated)
                    self.assertIsNone(user)
                elif action == "disable":
                    self.assertEqual(user["disabled"], 1)
                elif action == "enable":
                    self.assertEqual(user["disabled"], 0)
                else:
                    self.assertTrue(check_password_hash(user["password_hash"], "new-password"))
                    self.assertEqual(user["session_version"], 1)

    def test_manage_users_cli_can_manage_a_clashing_login(self):
        for action in ("disable", "enable", "remove", "reset-password"):
            with self.subTest(action=action):
                self.seed_clashing_user(disabled=1 if action == "enable" else 0)
                password = b"new-password\n" if action == "reset-password" else b""
                result = run_cli(self.db, action, "DRIVER", password=password)
                self.assertEqual(result.returncode, 0, result.stderr)
                user = self.viewer.users.get_user(self.db, "DRIVER")
                if action == "remove":
                    self.assertIsNone(user)
                elif action == "disable":
                    self.assertEqual(user["disabled"], 1)
                elif action == "enable":
                    self.assertEqual(user["disabled"], 0)
                else:
                    self.assertTrue(check_password_hash(user["password_hash"], "new-password"))
                    self.assertEqual(user["session_version"], 1)

    def test_add_still_refuses_the_admin_clash(self):
        self.seed_clashing_user()
        with self.assertRaisesRegex(self.viewer.users.UserError, "clashes with the admin"):
            self.viewer.users.change_user(self.db, "add", "DRIVER", "new-password", "driver")
        result = run_cli(self.db, "add", "DRIVER", password=b"new-password\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"clashes with the admin", result.stderr)

    def test_non_add_actions_still_validate_username_format(self):
        for action in ("disable", "enable", "remove", "reset-password"):
            for username in ("bad name", "x" * 65, "replace-me-helper"):
                with self.subTest(action=action, username=username[:12]):
                    password = "new-password" if action == "reset-password" else None
                    with self.assertRaises(self.viewer.users.UserError):
                        self.viewer.users.change_user(
                            self.db, action, username, password=password, admin_username="driver")

    def test_clashing_login_and_existing_session_are_still_refused(self):
        self.seed_clashing_user()
        self.assertEqual(self.login("DRIVER").status_code, 401)

        client = self.viewer.app.test_client()
        user = self.viewer.users.get_user(self.db, "DRIVER")
        with client.session_transaction() as session:
            session.update(
                user="DRIVER", kind="editor",
                pw_tag=self.viewer.users.session_tag(self.viewer.VIEWER_SECRET_KEY, user),
            )
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
