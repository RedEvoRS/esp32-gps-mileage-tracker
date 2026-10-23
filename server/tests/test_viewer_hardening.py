"""Viewer response headers and login redirect safety (2026-10-23 audit)."""

import unittest
from urllib.parse import quote

import test_viewer_roles as roles


BS = chr(92)


class ViewerHardeningTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in
    login = roles.ViewerRolesTests.login

    # -- framing and sniffing headers -------------------------------------

    def assert_hardened(self, response):
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_every_kind_of_response_refuses_framing(self):
        anonymous = self.client.get("/login")
        self.assert_hardened(anonymous)
        self.assert_hardened(self.client.get("/health"))
        self.assert_hardened(self.client.get("/"))  # login redirect
        self.assert_hardened(self.client.post("/login", data={}))  # CSRF failure page
        self.signed_in("admin")
        for path in ("/", "/trip/2026-01/drive", "/static/gpx.js", "/export.csv", "/export.xlsx",
                     "/admin", "/admin/year/2026", "/gpx/2026-01/drive", "/no/such/page"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assert_hardened(response)
                response.close()
        self.assert_hardened(self.client.post("/logout", data={"csrf_token": "known-token"}))

    def test_existing_csp_is_merged_not_replaced(self):
        @self.viewer.app.route("/csp-test")
        def csp_test():
            response = self.viewer.app.response_class("ok")
            response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors *"
            return response

        self.viewer.PUBLIC_ENDPOINTS = self.viewer.PUBLIC_ENDPOINTS | {"csp_test"}
        policy = self.client.get("/csp-test").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertEqual(policy.count("frame-ancestors"), 1)
        self.assertIn("frame-ancestors 'none'", policy)

    # -- open redirect after login ----------------------------------------

    def next_after_login(self, target, raw=False):
        query = target if raw else quote(target, safe="")
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            token = sess["csrf"]
        return self.client.post(f"/login?next={query}", data={
            "username": "driver", "password": "test-password", "csrf_token": token})

    def test_next_rejects_control_characters_and_backslashes(self):
        bad = ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/" + BS + "evil.example",
               "/" + BS + "/evil.example", "/ok\x0b/evil.example", "//evil.example", "https://evil.example",
               "evil.example", "/%5Cevil.example", "/%09/evil.example"]
        for target in bad:
            for raw in (False, True):
                with self.subTest(target=target, raw=raw):
                    response = self.next_after_login(target, raw)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], "/")
                    self.client.post("/logout", data={"csrf_token": "x"})
                    with self.client.session_transaction() as sess:
                        sess.clear()

    def test_next_accepts_plain_local_paths(self):
        for target in ("/export.csv", "/trip/2026-01/drive", "/?year=2026", "/admin/year/2026"):
            with self.subTest(target=target):
                response = self.next_after_login(target)
                self.assertEqual(response.headers["Location"], target)
                with self.client.session_transaction() as sess:
                    sess.clear()

    def test_unit_safe_next(self):
        safe = self.viewer._safe_next
        self.assertIsNone(safe(None))
        self.assertIsNone(safe(""))
        self.assertIsNone(safe("/\t/evil.example"))
        self.assertIsNone(safe("/%0A/evil.example"))
        self.assertIsNone(safe("/%5Cevil.example"))
        self.assertEqual(safe("/trip/a b"), "/trip/a b")

    def test_logged_out_request_for_a_tab_path_does_not_become_an_open_redirect(self):
        for path in ("/%09/evil.example", "/%0A/evil.example", "/%0D/evil.example"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].startswith("/login?next="))
                follow = self.next_after_login_from(response.headers["Location"])
                self.assertEqual(follow.status_code, 302)
                self.assertEqual(follow.headers["Location"], "/")
                with self.client.session_transaction() as sess:
                    sess.clear()

    def next_after_login_from(self, location):
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            token = sess["csrf"]
        return self.client.post(location, data={
            "username": "driver", "password": "test-password", "csrf_token": token})


if __name__ == "__main__":
    unittest.main()
