"""Opt-in proxy trust and Secure cookies, including real login lockout keys."""

import importlib.util
import os
import sqlite3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_viewer_roles as roles
sys.path.insert(0, str(roles.SERVER))
from common.settings import trusted_proxies, viewer_cookie_secure


class ProxySettingsTests(unittest.TestCase):
    def test_empty_and_whitespace_disable_trust(self):
        for value in ("", "  \t"):
            self.assertEqual(trusted_proxies(value), ())

    def test_ipv4_ipv6_networks_and_whitespace(self):
        networks = trusted_proxies(" 192.0.2.1 , 192.0.2.8/24, ::1 , 2001:db8::1/64 ")
        self.assertEqual(tuple(map(str, networks)),
                         ("192.0.2.1/32", "192.0.2.0/24", "::1/128", "2001:db8::/64"))

    def test_invalid_entries_fail_instead_of_disabling_trust(self):
        for value in (",", "192.0.2.1,", "hostname", "192.0.2.1:80", "[::1]",
                      "192.0.2.1/33", "2001:db8::/129", "fe80::1%eth0", "bad,::1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "VIEWER_TRUSTED_PROXIES"):
                trusted_proxies(value)

    def test_strict_boolean(self):
        for value in ("", "false", "FALSE"):
            self.assertFalse(viewer_cookie_secure(value))
        for value in ("true", "TRUE", "True"):
            self.assertTrue(viewer_cookie_secure(value))
        for value in ("yes", "1", "0", " true ", "no"):
            with self.assertRaisesRegex(ValueError, "VIEWER_COOKIE_SECURE"):
                viewer_cookie_secure(value)

    def test_compose_and_example_include_all_settings(self):
        server = Path(__file__).resolve().parents[1]
        compose = (server / "docker-compose.yml").read_text()
        example = (server / ".env.example").read_text()
        for key in ("VIEWER_BIND_ADDR", "VIEWER_COOKIE_SECURE", "VIEWER_TRUSTED_PROXIES"):
            self.assertIn(key + "=", example)
            self.assertIn("${" + key + ":-", compose)


class ViewerProxyTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor

    def resolve(self, peer, header="", networks="192.0.2.0/24,2001:db8::/64"):
        headers = [("X-Forwarded-For", value) for value in header] if isinstance(header, list) else {
            "X-Forwarded-For": header}
        with patch.object(self.viewer, "VIEWER_TRUSTED_PROXIES", trusted_proxies(networks)):
            with self.viewer.app.test_request_context("/", headers=headers,
                    environ_base={"REMOTE_ADDR": peer}):
                return self.viewer._client_ip()

    def test_untrusted_peer_ignores_spoofed_chain_and_x_real_ip(self):
        self.assertEqual(self.resolve("198.51.100.1", "203.0.113.1"), "198.51.100.1")
        self.assertEqual(self.resolve("192.0.2.1", "203.0.113.1", networks=""), "192.0.2.1")
        with self.viewer.app.test_request_context("/", headers={"X-Real-IP": "203.0.113.1"},
                environ_base={"REMOTE_ADDR": "198.51.100.1"}):
            self.assertEqual(self.viewer._client_ip(), "198.51.100.1")

    def test_rightmost_untrusted_ignores_leftmost_spoof(self):
        self.assertEqual(self.resolve("192.0.2.1", "203.0.113.99, 198.51.100.1"), "198.51.100.1")

    def test_multiple_trusted_hops_and_header_instances(self):
        self.assertEqual(self.resolve("192.0.2.1", ["203.0.113.99, 198.51.100.1",
                                                  "2001:db8::2, 192.0.2.3"]), "198.51.100.1")

    def test_ipv6_client_is_canonicalized(self):
        self.assertEqual(self.resolve("2001:db8::2", "2001:db8:1:0::1, ::1",
                                      networks="2001:db8::/64,::1"), "2001:db8:1::1")

    def test_garbage_entries_are_skipped(self):
        self.assertEqual(self.resolve("192.0.2.1", "198.51.100.1, garbage, [::1], ,192.0.2.2"),
                         "198.51.100.1")

    def test_missing_invalid_and_all_trusted_chains_fall_back_to_peer(self):
        for header in ("", ",bad,[::1],192.0.2.3:80", "192.0.2.2,2001:db8::2"):
            self.assertEqual(self.resolve("192.0.2.1", header), "192.0.2.1")
        self.assertEqual(self.resolve("unknown", "198.51.100.1"), "unknown")

    def test_warning_once_per_peer_under_concurrency_and_bounded(self):
        with patch.object(self.viewer, "_PROXY_WARNING_CAP", 2):
            with patch.object(self.viewer.app.logger, "warning") as warning:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: self.resolve("198.51.100.1", "spoof"), range(16)))
                self.resolve("198.51.100.2", "spoof")
                self.resolve("198.51.100.3", "spoof")
                self.assertEqual(warning.call_count, 2)
                self.assertEqual(len(self.viewer._proxy_warning_peers), 2)
                self.assertIn("VIEWER_TRUSTED_PROXIES", warning.call_args.args[0])
                self.assertEqual(warning.call_args.args[1], "198.51.100.2")

    def test_public_get_warns_but_absent_header_and_trusted_peer_do_not(self):
        with patch.object(self.viewer.app.logger, "warning") as warning:
            self.client.get("/health")
            self.assertEqual(warning.call_count, 0)
            self.client.get("/health", headers={"X-Forwarded-For": "198.51.100.1"})
            self.client.get("/login", headers={"X-Forwarded-For": "198.51.100.2"})
            self.assertEqual(warning.call_count, 1)
            self.resolve("192.0.2.1", "198.51.100.1")
            self.assertEqual(warning.call_count, 1)

    def test_lockout_and_failed_login_logging_use_resolved_ip(self):
        def login(ip):
            client = self.viewer.app.test_client()
            client.get("/login")
            with client.session_transaction() as sess:
                token = sess["csrf"]
            return client.post("/login", data={"username": "driver", "password": "wrong",
                        "csrf_token": token}, headers={"X-Forwarded-For": ip},
                        environ_base={"REMOTE_ADDR": "192.0.2.1"})

        with patch.object(self.viewer, "VIEWER_TRUSTED_PROXIES", trusted_proxies("192.0.2.1")):
            with patch.object(self.viewer.app.logger, "warning") as warning:
                for _ in range(5):
                    self.assertEqual(login("198.51.100.1").status_code, 401)
                self.assertEqual(login("198.51.100.1").status_code, 429)
                self.assertEqual(login("198.51.100.2").status_code, 401)
                self.assertTrue(any("198.51.100.1" in str(call) for call in warning.call_args_list))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT ip, fails FROM login_attempts ORDER BY ip").fetchall(),
                             [("198.51.100.1", 0), ("198.51.100.2", 1)])

    def load(self, **settings):
        spec = importlib.util.spec_from_file_location("viewer_proxy_config_test", roles.SERVER / "mileage-viewer/app.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "INGEST_DATA_ROOT": str(self.db.parent / "data"), "VIEWER_DB_PATH": str(self.db),
            "VIEWER_USERNAME": "driver", "VIEWER_PASSWORD_HASH": roles.ADMIN_HASH,
            "VIEWER_SECRET_KEY": "test-session-key-" * 4, "DISPLAY_TZ": "UTC",
            "VIEWER_TRUSTED_PROXIES": "", "VIEWER_COOKIE_SECURE": "", **settings,
        }):
            spec.loader.exec_module(module)
        return module

    def test_startup_rejects_invalid_settings(self):
        for key, value in (("VIEWER_TRUSTED_PROXIES", "hostname"), ("VIEWER_COOKIE_SECURE", "yes")):
            with self.assertRaisesRegex(RuntimeError, "refusing to start: " + key):
                self.load(**{key: value})

    def test_cookie_config_and_actual_set_cookie_on_and_off(self):
        for value, expected in (("", False), ("false", False), ("true", True)):
            viewer = self.load(VIEWER_COOKIE_SECURE=value)
            self.assertEqual(viewer.app.config["SESSION_COOKIE_SECURE"], expected)
            client = viewer.app.test_client()
            response = client.get("/login", base_url="https://viewer.example.com")
            self.assertEqual("; Secure" in response.headers["Set-Cookie"], expected)
            with client.session_transaction(base_url="https://viewer.example.com") as sess:
                token = sess["csrf"]
            response = client.post("/login", base_url="https://viewer.example.com", data={
                "username": "driver", "password": "test-password", "csrf_token": token})
            self.assertEqual(response.status_code, 302)
            self.assertEqual("; Secure" in response.headers["Set-Cookie"], expected)
