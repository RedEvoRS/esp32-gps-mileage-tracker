"""Viewer response headers and login redirect safety (2026-10-23 audit)."""

import builtins
import errno
import io
import os
import json
import sqlite3
from pathlib import Path
import unittest
from contextlib import closing, ExitStack
from unittest.mock import patch
from urllib.parse import quote

from openpyxl import load_workbook
from zoneinfo import ZoneInfo

import test_viewer_roles as roles


ADMIN_REFUSAL = ("This trip&#39;s files can&#39;t be opened: its summary, track or month folder is a link that leads "
                 "outside the data folder or can&#39;t be followed safely, so the viewer won&#39;t read them. "
                 "If it is in the log, the trip list and exports flag it. "
                 "Replace the link with the real file or folder to show the trip again.")

BS = chr(92)


class ViewerHardeningTests(unittest.TestCase):
    setUp = roles.ViewerRolesTests.setUp
    seed_editor = roles.ViewerRolesTests.seed_editor
    signed_in = roles.ViewerRolesTests.signed_in
    login = roles.ViewerRolesTests.login

    def test_invalid_trip_url_components_never_read_or_change_trips(self):
        self.signed_in("admin")
        # Hand-made folders expose the old disagreement with merge-key checks.
        month = "\u0662\u0660\u0662\u0666-\u0660\u0661"
        original = self.viewer.DATA_ROOT / "2026-01"
        folder = self.viewer.DATA_ROOT / month
        folder.mkdir()
        for extension in ("json", "gpx"):
            (folder / f"drive.{extension}").write_bytes((original / f"drive.{extension}").read_bytes())
        routes = [("GET", "/trip/{key}"), ("GET", "/gpx/{key}")]
        routes += [("POST", "/trip/{key}/" + action) for action in
                   ("annotate", "remove", "restore", "lookup-place", "merge", "split")]
        tables = ("annotations", "trip_removals", "trip_history", "trip_merges", "trip_merge_parts")
        with closing(sqlite3.connect(self.db)) as conn:
            before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        files = {p: p.read_bytes() for p in self.viewer.DATA_ROOT.glob("*/*")}
        for key in ("2026-01/drive%0A", f"{quote(month)}/drive", "2026-01%0A/drive",
                    "2026-01/..", "2026-01/C:%5Cprivate", "2026-01/%5C%5Chost%5Cshare",
                    "2026-01/drive%2Fother", "2026-01/drive%5Cother"):
            for method, route in routes:
                with self.subTest(key=key, route=route):
                    with patch.object(self.viewer, "read_summary", side_effect=AssertionError("invalid URL read")), \
                            patch.object(self.viewer.GEOCODER, "lookup", side_effect=AssertionError("lookup")):
                        response = self.client.open(route.format(key=key), method=method, data={
                            "csrf_token": "known-token", "category": "business", "purpose_note": "changed",
                            "reason": "test removal", "confirm": "yes", "endpoint": "start",
                            "next_trip": "2026-01/drive",
                        })
                    self.assertEqual(response.status_code, 404)
                    response.close()
                    with closing(sqlite3.connect(self.db)) as conn:
                        self.assertEqual(before, {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                                                  for table in tables})
                    self.assertEqual(files, {p: p.read_bytes() for p in self.viewer.DATA_ROOT.glob("*/*")})
        self.assertEqual(self.viewer._folder_years(month), set())
        self.assertEqual(self.viewer._folder_years("2026-01\n"), set())

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

    def assert_unreadable_link(self, key, target, folder=False):
        if folder:
            self.assert_unreadable_folder(key, target)
            return
        self.signed_in("admin")
        # Catch application summary stats/opens, including a cache hit after
        # replacing a regular summary with a symlink.
        with patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside summary opened")), \
                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("outside summary stat")):
            trips, unreadable = self.viewer.load_trips()
            self.assertEqual(trips, [])
            self.assertEqual([t["trip_key"] for t in unreadable], [key])
            self.assertEqual(unreadable[0]["problem"], "the file could not be read")
            # A containment failure must reach the unreadable result without
            # probing presence through an escaping month or final-file link.
            with patch.object(self.viewer.os.path, "lexists", side_effect=AssertionError("unsafe presence probe")):
                with self.viewer._trip_read(trip_key=key) as (_, trips, targeted_unreadable):
                    self.assertEqual(trips, [])
                    self.assertEqual(targeted_unreadable, unreadable)
            self.assertEqual(self.viewer._recording_years(key), {2025, 2026})
            for route, status in (("/", 200), (f"/trip/{key}", 404), ("/export.csv", 200),
                                  ("/export.xlsx", 200), ("/admin/year/2026", 200)):
                with self.subTest(route=route):
                    response = self.client.get(route)
                    self.assertEqual(response.status_code, status)
                    if route.endswith(".xlsx"):
                        book = load_workbook(io.BytesIO(response.data))
                        try:
                            self.assertIn("Unreadable trips", book.sheetnames)
                            text = str([list(sheet.values) for sheet in book])
                        finally:
                            book.close()
                    else:
                        text = response.get_data(as_text=True)
                    self.assertNotIn("outside-summary-sentinel", text)
                    self.assertNotIn(str(target), text)
                    if route == "/export.csv":
                        self.assertIn("unreadable", text)
                    elif route == f"/trip/{key}":
                        self.assertIn(ADMIN_REFUSAL, text)
                        self.assertNotIn("the file could not be read", text)
                    elif not route.endswith(".xlsx"):
                        self.assertIn("the file could not be read", text)
                    response.close()

        everyday = self.signed_in("editor", self.viewer.app.test_client())
        unknown = everyday.get("/trip/2026-03/none")
        refused = everyday.get(f"/trip/{key}")
        self.assertEqual((refused.status_code, refused.data, refused.content_type),
                         (unknown.status_code, unknown.data, unknown.content_type))

    def assert_unreadable_folder(self, key, target, reason="outside"):
        problem = self.viewer.FOLDER_PROBLEMS[reason]
        self.signed_in("admin")
        with patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside read")), \
                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("outside stat")):
            trips, unreadable = self.viewer.load_trips()
            self.assertEqual(trips, [])
            self.assertEqual(len(unreadable), 1)
            folder = unreadable[0]
            self.assertTrue(folder["folder"])
            self.assertEqual(folder["trip_key"], "2026-01/")
            self.assertEqual(folder["refused_file"], "folder")
            self.assertEqual(folder["problem_file"], "folder")
            self.assertEqual(folder["problem"], problem)
            self.assertEqual(folder["folder_reason"], reason)
            with patch.object(self.viewer.os.path, "lexists", side_effect=AssertionError("unsafe presence probe")), \
                    self.viewer._trip_read(trip_key=key) as (_, targeted, damaged):
                self.assertEqual(targeted, [])
                self.assertEqual([t["trip_key"] for t in damaged], [key])
                self.assertEqual(damaged[0]["problem"], "the file could not be read")
            self.assertEqual(self.viewer._recording_years(key), {2025, 2026})
        for role in ("admin", "editor"):
            client = self.signed_in(role, self.viewer.app.test_client())
            routes = ["/", "/export.csv", "/export.xlsx", f"/trip/{key}", f"/gpx/{key}"]
            if role == "admin":
                routes += ["/admin/year/2025", "/admin/year/2026"]
            for route in routes:
                with self.subTest(role=role, route=route):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 404 if route.startswith(("/trip/", "/gpx/")) else 200)
                    if route.endswith(".xlsx"):
                        book = load_workbook(io.BytesIO(response.data))
                        text = str([list(sheet.values) for sheet in book])
                        self.assertIn("2026-01/ (whole month folder)", text)
                        book.close()
                    else:
                        text = response.get_data(as_text=True)
                    if route == f"/trip/{key}":
                        if role == "admin":
                            self.assertIn(ADMIN_REFUSAL, text)
                        else:
                            unknown = client.get("/trip/2026-03/none")
                            self.assertEqual((response.status_code, response.data, response.content_type),
                                             (unknown.status_code, unknown.data, unknown.content_type))
                            unknown.close()
                    self.assertNotIn("(no GPX either)", text)
                    self.assertNotIn("distinct-outside-name", text)
                    self.assertNotIn("outside-file-sentinel", text)
                    self.assertNotIn(str(target), text)
                    if route == "/export.csv":
                        import csv
                        rows = list(csv.DictReader(io.StringIO(text)))
                        row = next(r for r in rows if r["row_type"] == "unreadable_folder")
                        self.assertEqual(row["trip_id"], "2026-01/")
                        self.assertTrue(all(not v for k, v in row.items() if k not in ("trip_id", "row_type", "purpose_note")))
                    elif route == "/" or route.startswith("/admin/year/"):
                        self.assertIn("2026-01/", text)
                        self.assertIn(problem, text)
                        self.assertNotIn("2026-01/drive", text)
                        self.assertNotIn('/trip/2026-01/', text)
                        if route == "/":
                            self.assertIn("1 month folder(s)", text)
                            self.assertIn("none of its trips are shown or counted", text)
                            self.assertNotIn("Remove from log", text)
                            self.assertIn("mount the other disk" if role == "admin" else "ask the admin", text)
                    response.close()

    def test_scan_never_lists_or_opens_refused_target(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "outside-month"
        month.rename(target)
        self.symlink(month, target, directory=True)
        def guard(original):
            def checked(path, *args, **kwargs):
                if not isinstance(path, int):
                    candidate = Path(path).absolute()
                    for refused in (month, target):
                        self.assertFalse(candidate == refused or refused in candidate.parents,
                                         f"outside listing/open: {candidate}")
                return original(path, *args, **kwargs)
            return checked

        # Resolution may stat a target. Enumeration and file reads must stop
        # before either the link name or its outside target is used.
        with ExitStack() as stack:
            for owner, names in ((Path, ("iterdir", "glob", "rglob", "open", "read_text", "read_bytes")),
                                 (os, ("scandir", "listdir", "walk")), (builtins, ("open",))):
                for name in names:
                    stack.enter_context(patch.object(owner, name, guard(getattr(owner, name))))
            self.assertEqual(self.viewer.load_trips()[1][0]["trip_key"], "2026-01/")

    def test_nonmonth_directories_are_not_trip_folders(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        for name in ("archive", "diagnostics"):
            folder = self.viewer.DATA_ROOT / name
            folder.mkdir()
            (folder / "drive.json").write_bytes((month / "drive.json").read_bytes())
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertEqual(self.viewer.mileage_totals(trips)["all"], 1)
        self.assertEqual(unreadable, [])

    def test_unavailable_data_root_is_empty(self):
        self.viewer.DATA_ROOT.rename(self.viewer.DATA_ROOT.with_name("vanished"))
        self.assertEqual(self.viewer.load_trips(), ([], []))

    def test_month_link_causes_and_hints(self):
        root = self.viewer.DATA_ROOT
        cases = (("missing-inside", root / "missing", "dangling", "the folder link target is missing"),
                 ("missing-outside", self.db.parent / "missing", "dangling", "the folder link target is missing"),
                 ("loop", root / "2027-02", "loop", "the folder link forms a loop"),
                 ("root", root, "outside", "the folder link points at the data folder itself or above it"),
                 ("parent", root.parent, "outside", "the folder link points at the data folder itself or above it"))
        for label, target, reason, problem in cases:
            with self.subTest(cause=label):
                link = root / "2027-02"
                self.symlink(link, target, directory=True)
                try:
                    entry = self.viewer.load_trips()[1][0]
                    self.assertEqual(entry["folder_reason"], reason)
                    self.assertEqual(entry["problem"], problem)
                    for role in ("admin", "editor"):
                        client = self.signed_in(role, self.viewer.app.test_client())
                        for route in ("/", "/export.csv", "/export.xlsx") + (("/admin/year/2027",) if role == "admin" else ()):
                            response = client.get(route)
                            if route.endswith("xlsx"):
                                book = load_workbook(io.BytesIO(response.data))
                                text = str(list(book["Unreadable trips"].values))
                                book.close()
                            else:
                                text = response.get_data(as_text=True)
                            self.assertIn(problem, text)
                            self.assertIn("mount the other disk" if role == "admin" else "ask the admin", text)
                            response.close()
                finally:
                    link.unlink()

    def test_real_month_metadata_errors_are_unreadable(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        original = Path.stat
        for operation in ("stat", "lstat"):
            def failing(path, *args, operation=operation, **kwargs):
                if path == month and (operation == "lstat" or kwargs.get("follow_symlinks", True)):
                    raise OSError(errno.EIO, "synthetic metadata failure")
                return original(path, *args, **kwargs)

            with self.subTest(operation=operation), patch.object(Path, "stat", failing):
                trips, unreadable = self.viewer.load_trips()
            self.assertEqual(trips, [])
            self.assertEqual(unreadable[0]["folder_reason"], "unreadable")
            self.assertEqual(unreadable[0]["problem"], "the folder could not be read")

    def test_vanished_month_is_reported_and_other_month_loads(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        other = self.viewer.DATA_ROOT / "2026-02"
        other.mkdir()
        (other / "drive.json").write_bytes((month / "drive.json").read_bytes())
        original = self.viewer.trip_merges.checked_path

        def vanish(root, path, **kwargs):
            checked = original(root, path, **kwargs)
            if path == month:
                month.rename(self.viewer.DATA_ROOT / "vanished")
            return checked

        with patch.object(self.viewer.trip_merges, "checked_path", side_effect=vanish):
            trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-02/drive"])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/"])
        self.assertEqual(unreadable[0]["problem"], "the folder could not be read")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0, "requires non-root POSIX permissions")
    def test_unlistable_month_is_reported_and_pages_still_load(self):
        month = self.viewer.DATA_ROOT / "2026-02"
        month.mkdir()
        month.chmod(0)
        self.addCleanup(month.chmod, 0o700)
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-02/"])
        self.assertEqual(unreadable[0]["problem"], "the folder could not be read")
        self.assertEqual(unreadable[0]["folder_reason"], "unreadable")
        self.signed_in("admin")
        for route in ("/", "/export.csv", "/export.xlsx"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            if route.endswith("xlsx"):
                book = load_workbook(io.BytesIO(response.data))
                text = str(list(book["Unreadable trips"].values))
                book.close()
            else:
                text = response.get_data(as_text=True)
            self.assertIn("owner and permissions", text)
            self.assertIn("UID 1000", text)
            response.close()
        review = self.client.get("/admin/year/2026").get_data(as_text=True)
        self.assertIn("owner and permissions", review)

    @unittest.skipUnless(os.scandir in os.supports_fd and hasattr(os, "O_NOFOLLOW"), "requires no-follow directory handles")
    def test_month_swapped_before_listing_never_lists_outside(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "outside-month"
        probe = self.viewer.DATA_ROOT / "probe"
        self.symlink(probe, target, directory=True)
        probe.unlink()
        original_iterdir, original_open, original_scan = Path.iterdir, os.open, os.scandir
        swapped = False

        def swap():
            nonlocal swapped
            if not swapped:
                month.rename(target)
                month.symlink_to(target, target_is_directory=True)
                swapped = True

        def listing(path):
            if path == month:
                swap()
                self.fail("path listing follows the swapped month link")
            return original_iterdir(path)

        def opening(path, *args, **kwargs):
            if path == month:
                swap()
            return original_open(path, *args, **kwargs)

        def scanning(path):
            if isinstance(path, int):
                self.assertNotEqual(os.fstat(path).st_ino, target.stat().st_ino)
            else:
                self.assertNotEqual(Path(path).resolve(), target)
            return original_scan(path)

        with patch.object(Path, "iterdir", listing), patch.object(os, "open", opening), \
                patch.object(os, "scandir", scanning), \
                patch.object(os, "supports_fd", os.supports_fd | {scanning}), \
                patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside read")):
            trips, unreadable = self.viewer.load_trips()
        self.assertTrue(swapped)
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/"])
        self.assertTrue(unreadable[0]["folder"])

    def test_month_swapped_during_file_read_folds_into_folder(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "swapped-month"
        # Establish symlink support before the mocked read.
        probe = self.viewer.DATA_ROOT / "probe"
        self.symlink(probe, target, directory=True)
        probe.unlink()
        cached = self.viewer._cached_summary
        swapped = False

        def swap(path, **kwargs):
            nonlocal swapped
            if not swapped:
                month.rename(target)
                month.symlink_to(target, target_is_directory=True)
                swapped = True
            return cached(path, **kwargs)

        with patch.object(self.viewer, "_cached_summary", side_effect=swap):
            trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/"])
        self.assertTrue(unreadable[0]["folder"])

    def test_internal_month_link_still_scans_and_nonmonth_link_is_ignored(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.viewer.DATA_ROOT / "2026-02"
        month.rename(target)
        self.symlink(month, target, directory=True)
        self.symlink(self.viewer.DATA_ROOT / "other", self.db.parent / "missing", directory=True)
        with self.assertNoLogs(self.viewer.app.logger, "WARNING"):
            trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/"])
        self.assertEqual({t["trip_key"] for t in trips}, {"2026-02/drive"})

    def assert_alias_counted_once(self, key, alias):
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], [key])
        self.assertEqual(self.viewer.mileage_totals(trips)["all"], 1)
        self.assertEqual([t["trip_key"] for t in unreadable], [alias + "/"])
        self.assertTrue(unreadable[0]["folder"])
        self.assertIn("already counted", unreadable[0]["problem"])
        self.assertNotIn(str(self.viewer.DATA_ROOT), unreadable[0]["problem"])
        for role in ("admin", "editor"):
            client = self.signed_in(role, self.viewer.app.test_client())
            page = client.get("/").get_data(as_text=True)
            self.assertIn(key, page)
            self.assertNotIn("totals below may be short", page)
            self.assertNotIn("none of its trips are shown or counted", page)
            self.assertIn("whose trips are counted there", page)
            self.assertIn("Remove the extra link" if role == "admin" else "ask the admin", page)
            self.assertIn("example", page)
            self.assertNotIn(f'/trip/{alias}/drive', page)
            import csv
            rows = list(csv.DictReader(io.StringIO(client.get("/export.csv").get_data(as_text=True))))
            self.assertEqual(len([r for r in rows if r["row_type"] == "trip"]), 1)
            self.assertEqual([float(r["distance_miles"]) for r in rows
                              if r["row_type"] == "total" and r["category"] == "all"], [1])
            self.assertEqual([r["trip_id"] for r in rows if r["row_type"] == "folder_notice"], [alias + "/"])
            book = load_workbook(io.BytesIO(client.get("/export.xlsx").data))
            self.assertEqual(book["Mileage"].max_row, 2)
            self.assertEqual([r[3] for r in book["Totals"].values if r[2] == "all"], [1])
            self.assertEqual(book["Unreadable trips"].max_row, 2)
            self.assertIn("whose trips are counted there", str(list(book["Unreadable trips"].values)))
            notice = next(r for r in rows if r["row_type"] == "folder_notice")
            self.assertIn("whose trips are counted there", notice["purpose_note"])
            self.assertNotIn("missing", notice["purpose_note"])
            self.assertIn(alias + "/", str(list(book["Unreadable trips"].values)))
            book.close()
        self.signed_in("admin")
        for year in (int(alias[:4]) - 1, int(alias[:4])):
            page = self.client.get(f"/admin/year/{year}").get_data(as_text=True)
            self.assertEqual(page.count(f"<code>{alias}/</code> (month folder)"), 1)
            self.assertNotIn("totals above may be short", page)
            self.assertIn("whose trips are counted there", page)
        page = self.client.get("/admin/year/2026").get_data(as_text=True)
        self.assertIn("<td>all</td><td>1</td><td>1.000</td>", page)

    def test_internal_alias_prefers_real_month_and_keeps_annotations(self):
        self.symlink(self.viewer.DATA_ROOT / "2025-01", self.viewer.DATA_ROOT / "2026-01", directory=True)
        self.assert_alias_counted_once("2026-01/drive", "2025-01")
        trips, _ = self.viewer.load_trips()
        self.assertEqual(trips[0]["category"], "business")
        self.assertEqual(trips[0]["purpose_note"], "example")

    def test_two_month_links_to_nonmonth_folder_count_once_in_sorted_order(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.viewer.DATA_ROOT / "archive"
        month.rename(target)
        self.symlink(self.viewer.DATA_ROOT / "2027-01", target, directory=True)
        self.symlink(month, target, directory=True)
        self.assert_alias_counted_once("2026-01/drive", "2027-01")

    def test_single_month_link_to_nonmonth_folder_scans_under_link_name(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.viewer.DATA_ROOT / "archive"
        month.rename(target)
        self.symlink(month, target, directory=True)
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertEqual(unreadable, [])
        self.assertEqual(trips[0]["purpose_note"], "example")
        self.assertEqual(trips[0]["category"], "business")

    def linked_archive(self, name="2026-02"):
        root = self.viewer.DATA_ROOT
        (root / "2026-01").rename(root / "archive")
        self.symlink(root / name, root / "archive", directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE annotations SET trip_key=?", (name + "/drive",))
        return root / "archive"

    def test_earlier_link_keeps_saved_annotations_and_removal(self):
        target = self.linked_archive()
        self.signed_in("admin")
        self.assertEqual(self.client.post("/trip/2026-02/drive/annotate", data={
            "csrf_token": "known-token", "category": "business", "purpose_note": "kept note",
        }).status_code, 302)
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        trips, _ = self.viewer.load_trips()
        self.assertEqual([(t["trip_key"], t["category"], t["purpose_note"]) for t in trips],
                         [("2026-02/drive", "business", "kept note")])
        self.assertEqual(self.client.post("/trip/2026-02/drive/remove", data={
            "csrf_token": "known-token", "reason": "example removal",
        }).status_code, 302)
        self.assertEqual(self.viewer.load_trips()[0], [])
        self.assertEqual(self.viewer.load_trips(include_removed=True)[0][0]["trip_key"], "2026-02/drive")

    def test_link_choice_checks_each_persisted_trip_key_and_closed_snapshot(self):
        target = self.linked_archive()
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        statements = [
            "INSERT INTO annotations (trip_key) VALUES ('2026-02/drive')",
            "INSERT INTO trip_removals VALUES ('2026-02/drive', 'example', 'admin', '')",
            "INSERT INTO trip_history (trip_key,actor,actor_role,at,action,details) VALUES ('2026-02/drive','admin','admin','','annotate','{}')",
            "INSERT INTO trip_merges (trip_key,summary,fingerprint) VALUES ('2026-02/merge-example','{}','[]')",
            "INSERT INTO trip_merge_parts VALUES ('2026-02/drive','2026-03/merge-example',0)",
            "INSERT INTO trip_merge_parts VALUES ('2026-03/drive','2026-02/merge-example',0)",
            "INSERT INTO trip_merge_aliases VALUES ('2026-02/merge-old','2026-03/merge-example')",
            "INSERT INTO tax_years VALUES (2026,'admin','', '{\"trip_keys\":[\"2026-02/drive\"]}')",
        ]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for statement in statements:
                for table in ("annotations", "trip_removals", "trip_history", "trip_merges",
                              "trip_merge_parts", "trip_merge_aliases", "tax_years"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute(statement)
                with self.subTest(statement=statement):
                    counted, problems = self.viewer._month_choices(conn, self.viewer.DATA_ROOT.resolve())
                    self.assertEqual(counted["2026-01"], "2026-02")
                    self.assertFalse(any(p[1] == "saved_keys" for p in problems))

    def test_link_only_conflicts_use_one_query_and_no_rows_use_sorted_order(self):
        target = self.linked_archive()
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM annotations")
            queries = []
            conn.set_trace_callback(queries.append)
            counted, _ = self.viewer._month_choices(conn, self.viewer.DATA_ROOT.resolve())
            self.assertEqual(counted["2026-02"], "2026-01")
            self.assertEqual(len(queries), 1)
            self.assertIn("WITH candidates", queries[0])
        # An ordinary folder plus an alias needs no saved-key query.
        (self.viewer.DATA_ROOT / "2026-02").unlink()
        (self.viewer.DATA_ROOT / "2026-01").unlink()
        target.rename(self.viewer.DATA_ROOT / "2026-02")
        self.symlink(self.viewer.DATA_ROOT / "2026-01", self.viewer.DATA_ROOT / "2026-02", directory=True)
        with patch.object(self.viewer, "_saved_month_names", side_effect=AssertionError("extra DB query")):
            self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], "2026-02/drive")

    def test_conflicting_saved_names_warn_and_refuse_year_close(self):
        target = self.linked_archive()
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key,category) VALUES ('2026-01/drive','personal')")
        trips, entries = self.viewer.load_trips()
        self.assertEqual(trips[0]["trip_key"], "2026-01/drive")
        warning = next(t for t in entries if t["folder_reason"] == "saved_keys")
        self.assertEqual(warning["saved_months"], ["2026-01", "2026-02"])
        self.assertEqual(self.viewer.year_figures(trips, entries)["unreadable"], 0)
        self.signed_in("admin")
        for path in ("/", "/admin/year/2026", "/export.csv"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("Warning: saved trip data exists under 2026-01/, 2026-02/", page)
            self.assertNotIn("totals below may be short", page)
        response = self.client.post("/admin/year/2026/close", data={"csrf_token": "known-token", "confirm": "yes"})
        self.assertEqual(response.status_code, 409)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tax_years").fetchone()[0], 0)

    def test_closed_year_snapshot_keeps_link_key_and_lock_without_annotation(self):
        target = self.linked_archive()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM annotations")
        self.signed_in("admin")
        self.assertEqual(self.client.post("/admin/year/2026/close", data={
            "csrf_token": "known-token", "confirm": "yes"}).status_code, 302)
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], "2026-02/drive")
        self.assertEqual(self.client.post("/trip/2026-02/drive/annotate", data={
            "csrf_token": "known-token", "category": "business"}).status_code, 409)

    def test_earlier_link_keeps_live_merge_parts(self):
        target = self.linked_archive()
        (target / "drive.gpx").write_bytes((roles.SERVER / "sample.gpx").read_bytes())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM annotations")
            conn.execute("INSERT INTO trip_merges (trip_key,summary,fingerprint) VALUES ('2026-02/merge-example','{}','[]')")
            conn.execute("INSERT INTO trip_merge_parts VALUES ('2026-02/drive','2026-02/merge-example',0)")
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        trips, entries = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-02/merge-example"])
        self.assertFalse(any(t.get("parts") for t in entries))

    def test_merged_trip_with_linked_month_part_keeps_undo(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "outside-month"
        month.rename(target)
        self.symlink(month, target, directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO trip_merges (trip_key, summary, fingerprint) VALUES (?, ?, ?)",
                         ("2026-02/merged", "{}", "[]"))
            conn.execute("INSERT INTO trip_merge_parts VALUES (?, ?, ?)",
                         ("2026-01/drive", "2026-02/merged", 0))
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual({t["trip_key"] for t in unreadable}, {"2026-01/", "2026-02/merged"})
        self.signed_in("admin")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Undo merge (split back)", page.data)

    def test_dangling_month_is_reported_in_full_scan(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "missing-month"
        for file in month.iterdir():
            file.unlink()
        month.rmdir()
        self.symlink(month, target, directory=True)
        self.assert_unreadable_folder("2026-01/missing", target, reason="dangling")

    def test_linked_month_does_not_break_other_pages_or_merge_guard(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "outside-month"
        month.rename(target)
        self.symlink(month, target, directory=True)
        other = self.viewer.DATA_ROOT / "2026-02"
        other.mkdir()
        for name in ("first", "second"):
            data = json.loads((target / "drive.json").read_text())
            data["start_time"] = "2026-02-02T12:00:00Z" if name == "first" else "2026-02-02T12:01:00Z"
            data["end_time"] = data["start_time"]
            (other / f"{name}.json").write_text(json.dumps(data))
        self.signed_in("admin")
        for route in ("/admin/history", "/admin/removed", "/trip/2026-02/first", "/admin/year/2026", "/export.csv"):
            self.assertEqual(self.client.get(route).status_code, 200, route)
        trips, unreadable = self.viewer.load_trips()
        first = next(t for t in trips if t["id"] == "first")
        candidate, reason = self.viewer.trip_merges.next_trip(first, trips, unreadable)
        self.assertIsNone(candidate)
        self.assertIn("the folder links outside the data folder", reason)

    def test_refused_trip_links_hide_removal_and_stop_before_trip_reads(self):
        everyday = self.signed_in("editor", self.viewer.app.test_client())
        self.signed_in("admin")
        unknown = everyday.get("/trip/2026-03/none")
        outside = self.db.parent / "outside"
        outside.mkdir()
        for kind in ("summary", "gpx", "month"):
            with self.subTest(kind=kind):
                key = f"2026-02/{kind}"
                month = self.viewer.DATA_ROOT / "2026-02"
                if kind == "month":
                    self.symlink(month, outside, directory=True)
                    link = month
                else:
                    month.mkdir(exist_ok=True)
                    link = month / (kind + (".json" if kind == "summary" else ".gpx"))
                    target = outside / kind
                    target.write_text("outside-file-sentinel", encoding="utf-8")
                    self.symlink(link, target)
                try:
                    for removed in (False, True):
                        if removed:
                            with closing(sqlite3.connect(self.db)) as conn, conn:
                                self.viewer.trip_log.add_removal(conn, key, "test reason", "driver")
                        with patch.object(self.viewer, "read_summary", side_effect=AssertionError("trip read")), \
                                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("trip stat")), \
                                patch.object(self.viewer.trip_log, "removal", side_effect=AssertionError("removal read")):
                            response = everyday.get(f"/trip/{key}")
                            self.assertEqual((response.status_code, response.data, response.content_type),
                                             (unknown.status_code, unknown.data, unknown.content_type))
                            admin = self.client.get(f"/trip/{key}")
                            self.assertEqual(admin.status_code, 404)
                            self.assertIn(ADMIN_REFUSAL, admin.get_data(as_text=True))
                            for result in (response, admin):
                                self.assertNotIn(b"outside-file-sentinel", result.data)
                                self.assertNotIn(str(outside).encode(), result.data)
                            for client in (everyday, self.client):
                                plain = client.get("/no/such/page")
                                for address in ("/trip/2026-x2/month", "/trip/2026-02/bad.key"):
                                    malformed = client.get(address)
                                    self.assertEqual((malformed.status_code, malformed.data, malformed.content_type),
                                                     (plain.status_code, plain.data, plain.content_type))
                finally:
                    link.unlink()
                    if kind != "month":
                        month.rmdir()

    def test_summary_symlink_is_unreadable_in_lists_pages_exports_and_year_review(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        target = self.db.parent / "private-summary.json"
        target.write_text(json.dumps({"start_time": "2026-01-02T12:00:00Z", "distance_miles": 999,
                                      "device_id": "outside-summary-sentinel"}), encoding="utf-8")
        summary.unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_cached_summary_cannot_bypass_replaced_symlink_check(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        self.viewer.load_trips()
        self.assertIn(summary, self.viewer._summary_cache)
        target = self.db.parent / "private-summary.json"
        target.write_bytes(summary.read_bytes())
        summary.unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_symlinked_month_is_unreadable_and_has_no_downloadable_gpx(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "private-month"
        month.rename(target)
        (target / "distinct-outside-name.json").write_text("{}")
        (target / "distinct-outside-name.gpx").write_text("<gpx/>")
        self.symlink(month, target, directory=True)
        self.assert_unreadable_link("2026-01/drive", target, folder=True)
        self.assertFalse(self.viewer.load_trips()[1][0]["has_gpx"])
        self.assertEqual(self.client.get("/gpx/2026-01/drive").status_code, 404)

    def test_dangling_escaping_summary_symlink_is_unreadable_without_gpx(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        target = self.db.parent / "missing-summary.json"
        summary.unlink()
        summary.with_suffix(".gpx").unlink()
        self.symlink(summary, target)
        self.assert_unreadable_link("2026-01/drive", target)

    def test_dangling_escaping_month_symlink_is_unreadable_on_targeted_read(self):
        target = self.db.parent / "missing-month"
        self.symlink(self.viewer.DATA_ROOT / "2026-02", target, directory=True)
        with patch.object(self.viewer.os.path, "lexists", side_effect=AssertionError("unsafe presence probe")), \
                patch.object(self.viewer, "read_summary", side_effect=AssertionError("outside summary opened")), \
                patch.object(self.viewer, "_summary_stamp", side_effect=AssertionError("outside summary stat")):
            with self.viewer._trip_read(trip_key="2026-02/missing") as (_, trips, unreadable):
                self.assertEqual(trips, [])
                self.assertEqual([t["trip_key"] for t in unreadable], ["2026-02/missing"])
                self.assertEqual(unreadable[0]["problem"], "the file could not be read")

    def test_missing_trip_page_does_not_log_unreadable_summary(self):
        self.signed_in("admin")
        for key in ("2026-01/missing", "2026-02/missing"):
            with self.subTest(key=key), self.assertNoLogs(self.viewer.app.logger, "WARNING"):
                response = self.client.get(f"/trip/{key}")
                self.assertEqual(response.status_code, 404)
                response.close()

    def test_looping_gpx_link_is_unreadable_on_every_python(self):
        # #729: Python 3.13's resolve() no longer raises on a link loop, so a
        # looping GPX beside a valid summary was counted on 3.13, not on 3.12.
        self.signed_in("admin")
        gpx = self.viewer.DATA_ROOT / "2026-01/drive.gpx"
        gpx.unlink()
        self.symlink(gpx, gpx)
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([u["trip_key"] for u in unreadable], ["2026-01/drive"])
        self.assertEqual(unreadable[0]["problem"], "the file could not be read")
        self.assertEqual(self.client.get("/trip/2026-01/drive").status_code, 404)

    def test_escaping_gpx_is_not_available_to_pages_or_exports(self):
        self.signed_in("admin")
        gpx = self.viewer.DATA_ROOT / "2026-01/drive.gpx"
        target = self.db.parent / "private-track.gpx"
        gpx.rename(target)
        self.symlink(gpx, target)
        target.write_text("outside-track-sentinel", encoding="utf-8")
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/drive"])
        self.assertEqual(unreadable[0]["problem"], "the file could not be read")
        self.assertEqual(unreadable[0]["device_id"], "example")
        self.assertEqual(unreadable[0]["start_time"], "2026-01-02T12:00:00Z")
        self.assertNotIn("distance_miles", unreadable[0])
        with self.viewer._trip_read(trip_key="2026-01/drive") as (_, targeted, damaged):
            self.assertEqual(targeted, trips)
            self.assertEqual(damaged, unreadable)
        # Mix a readable recording with the refused one so counting either
        # recording incorrectly changes the total.
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        readable = json.loads(summary.read_text())
        readable["distance_miles"] = 2.5
        summary.with_name("readable.json").write_text(json.dumps(readable))
        counted, _ = self.viewer.load_trips()
        self.assertEqual(self.viewer.mileage_totals(counted)["all"], 2.5)
        summary.with_name("readable.json").unlink()
        for route in ("/", "/export.csv", "/export.xlsx", "/admin/year/2026"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                if route.endswith(".xlsx"):
                    book = load_workbook(io.BytesIO(response.data))
                    try:
                        self.assertEqual(book["Mileage"].max_row, 1)
                        self.assertEqual(book["Totals"]["D2"].value, 0)
                        self.assertIn("Unreadable trips", book.sheetnames)
                        rows = list(book["Unreadable trips"].values)
                        self.assertEqual([row[rows[0].index("trip_id")] for row in rows[1:]], ["drive"])
                        text = str([list(sheet.values) for sheet in book])
                    finally:
                        book.close()
                else:
                    text = response.get_data(as_text=True)
                if route.endswith(".csv"):
                    self.assertIn(",unreadable,", text)
                elif not route.endswith(".xlsx"):
                    self.assertIn("the file could not be read", text)
                    self.assertIn("2026-01/drive.gpx", text)
                    self.assertNotIn("2026-01/drive.json", text)
                    self.assertNotIn("(no GPX either)", text)
                    if route == "/":
                        self.assertIn("1 trip file(s) can't be read.", text)
                        self.assertIn("damaged summary from its GPX", text)
                    else:
                        self.assertIn("Trips whose files can't be read", text)
                self.assertNotIn("refused_file", text)
                self.assertNotIn("outside-track-sentinel", text)
                self.assertNotIn(str(target), text)
                if route.endswith(".csv"):
                    self.assertNotIn(",trip,", text)
                response.close()
        response = self.client.get("/gpx/2026-01/drive")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"outside-track-sentinel", response.data)

    def assert_refused_gpx_label(self, suffix, problem):
        self.signed_in("admin")
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(trips, [])
        self.assertEqual(len(unreadable), 1)
        self.assertEqual(unreadable[0]["refused_file"], "gpx")
        self.assertEqual(unreadable[0]["problem"], problem)
        with self.viewer._trip_read(trip_key="2026-01/drive") as (_, targeted, damaged):
            self.assertEqual(targeted, trips)
            self.assertEqual(damaged, unreadable)
        for route in ("/", "/admin/year/2026"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                text = response.get_data(as_text=True)
                self.assertIn(f"2026-01/drive.{suffix}</code>", text)
                self.assertIn(problem, text)
                self.assertNotIn("(no GPX either)", text)
                self.assertNotIn("outside-file-sentinel", text)
                if suffix == "json":
                    self.assertIn("(its GPX links outside the data folder)", text)

    def test_missing_summary_with_refused_gpx_names_track(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        summary.unlink()
        gpx = summary.with_suffix(".gpx")
        gpx.unlink()
        target = self.db.parent / "outside.gpx"
        target.write_text("outside-file-sentinel", encoding="utf-8")
        self.symlink(gpx, target)
        self.assert_refused_gpx_label("gpx", "the file could not be read")

    def test_damaged_summary_with_refused_gpx_keeps_summary_problem(self):
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        summary.write_text("{", encoding="utf-8")
        _, problem = self.viewer.read_summary(summary, root=self.viewer.DATA_ROOT.resolve())
        self.assertTrue(problem)
        gpx = summary.with_suffix(".gpx")
        gpx.unlink()
        target = self.db.parent / "outside.gpx"
        target.write_text("outside-file-sentinel", encoding="utf-8")
        self.symlink(gpx, target)
        self.assert_refused_gpx_label("json", problem)

    def test_refused_month_never_labels_track_missing(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.db.parent / "outside-month"
        month.rename(target)
        (target / "drive.json").write_text("outside-file-sentinel", encoding="utf-8")
        (target / "drive.gpx").write_text("outside-file-sentinel", encoding="utf-8")
        self.symlink(month, target, directory=True)
        self.assert_unreadable_link("2026-01/drive", target, folder=True)

    def test_missing_gpx_keeps_summary_readable_and_miles_counted(self):
        (self.viewer.DATA_ROOT / "2026-01/drive.gpx").unlink()
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual(unreadable, [])
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertFalse(trips[0]["has_gpx"])
        self.assertEqual(self.viewer.mileage_totals(trips)["all"], 1)
        with self.viewer._trip_read(trip_key="2026-01/drive") as (_, targeted, damaged):
            self.assertEqual(targeted, trips)
            self.assertEqual(damaged, [])

    def test_unreadable_summary_hides_os_error_paths_but_logs_details(self):
        self.signed_in("admin")
        summary = self.viewer.DATA_ROOT / "2026-01/drive.json"
        real_open = open
        private_path = "/private-storage/example/drive.json"

        def read_error(path, *args, **kwargs):
            if Path(path) == summary:
                raise PermissionError(13, "Permission denied", private_path)
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=read_error), \
                self.assertLogs(self.viewer.app.logger, "WARNING") as logs:
            for path, status in (("/", 200), ("/trip/2026-01/drive", 404),
                                 ("/export.csv", 200), ("/export.xlsx", 200)):
                response = self.client.get(path)
                self.assertEqual(response.status_code, status)
                if path.endswith(".xlsx"):
                    workbook = load_workbook(io.BytesIO(response.data))
                    try:
                        text = str([list(sheet.values) for sheet in workbook])
                    finally:
                        workbook.close()
                else:
                    text = response.get_data(as_text=True)
                self.assertNotIn(private_path, text)
                self.assertNotIn("Permission denied", text)
                if not path.startswith("/export"):
                    self.assertIn("the file could not be read", text)
                response.close()
        self.assertIn(private_path, "\n".join(logs.output))
        self.assertIn("Permission denied", "\n".join(logs.output))

    def test_final_trip_symlinks_cannot_escape_data_root(self):
        self.signed_in("admin")
        outside = self.db.parent / "outside"
        outside.mkdir()
        for extension in ("gpx", "json"):
            target = outside / ("private." + extension)
            target.write_text("private-file-sentinel", encoding="utf-8")
            link = self.viewer.DATA_ROOT / f"2026-01/linked.{extension}"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            try:
                for route in ("trip", "gpx"):
                    with self.subTest(extension=extension, route=route):
                        response = self.client.get(f"/{route}/2026-01/linked")
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn(b"private-file-sentinel", response.data)
                        if route == "trip":
                            self.assertIn(ADMIN_REFUSAL, response.get_data(as_text=True))
                        else:
                            self.assertIn(b"The requested URL was not found on the server", response.data)
                        self.assertNotIn(b"the file could not be read", response.data)
                        response.close()
            finally:
                link.unlink()

    def test_safe_trip_paths_allow_missing_files_and_resolved_data_root(self):
        with self.viewer.app.test_request_context():
            paths = self.viewer._safe_trip_paths("2026-02", "missing")
        root = self.viewer.DATA_ROOT.resolve()
        self.assertEqual(paths, (root / "2026-02/missing.gpx", root / "2026-02/missing.json"))
        self.assertFalse(any(p.exists() for p in paths))

    def test_trip_path_checks_preserve_ascii_allowlists(self):
        merges = self.viewer.trip_merges
        root = self.viewer.DATA_ROOT.resolve()
        for trip_id in ("a", "AZaz09_-", "2026-01-02T12-00-00Z"):
            with self.subTest(trip_id=trip_id), self.viewer.app.test_request_context():
                paths = self.viewer._safe_trip_paths("2026-01", trip_id)
                self.assertEqual(paths, tuple(root / f"2026-01/{trip_id}{ext}"
                                              for ext in (".gpx", ".json")))
        for key in ("2026-01/../private", "2026-01/drive\n", "2026-01/drive.other",
                    "2026-01/C:" + BS + "private", "2026-01" + BS + "drive",
                    "\u0662\u0660\u0662\u0666-01/drive"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                list(merges.source_file_pairs(root, [key]))

    def test_trip_path_checks_reject_root_and_sibling_prefix(self):
        root = self.viewer.DATA_ROOT.resolve()
        for path in (root, root.parent / (root.name + "-other") / "drive.json",
                     root / ".." / "private.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.viewer.trip_merges.checked_path(root, path)

    def test_linked_data_root_and_internal_trip_links_still_work(self):
        root = self.viewer.DATA_ROOT
        alias = self.db.parent / "linked-data"
        self.symlink(alias, root, directory=True)
        link = root / "2026-01/linked.json"
        self.symlink(link, root / "2026-01/drive.json")
        with patch.object(self.viewer, "DATA_ROOT", alias), self.viewer.app.test_request_context():
            self.assertEqual(self.viewer._safe_trip_paths("2026-01", "linked")[1],
                             (root / "2026-01/drive.json").resolve())
            self.assertTrue(self.viewer._trip_file_exists(root.resolve(), alias / "2026-01/linked.json"))

    def test_targeted_presence_keeps_internal_dangling_summary_link(self):
        root = self.viewer.DATA_ROOT.resolve()
        summary = root / "2026-01/drive.json"
        summary.unlink()
        target = root / "2026-01/missing.json"
        self.symlink(summary, target)
        checked = self.viewer.trip_merges.checked_path(root, summary, preserve_final=True)
        self.assertEqual(checked, summary)
        self.assertTrue(self.viewer.os.path.lexists(checked))
        self.assertEqual(self.viewer.trip_merges.checked_path(root, summary), target)
        with self.assertLogs(self.viewer.app.logger, "WARNING"):
            with self.viewer._trip_read(trip_key="2026-01/drive") as (_, trips, unreadable):
                self.assertEqual(trips, [])
                self.assertEqual([t["trip_key"] for t in unreadable], ["2026-01/drive"])
                self.assertEqual(unreadable[0]["problem"], "the file is missing")

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

    # -- control characters and length in notes (XLSX export) -------------

    ILLEGAL = {"U+0001": chr(1), "U+000B": chr(0x0B), "U+FFFE": chr(0xFFFE), "U+FFFF": chr(0xFFFF)}

    def annotate(self, **fields):
        self.signed_in("admin")
        data = {"csrf_token": "known-token", "category": "business",
                "purpose_note": "", "destination": ""}
        data.update(fields)
        return self.client.post("/trip/2026-01/drive/annotate", data=data)

    def stored(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT purpose_note, destination FROM annotations "
                                "WHERE trip_key='2026-01/drive'").fetchone()

    def test_save_rejects_characters_xml_cannot_hold_and_says_why(self):
        for name, char in self.ILLEGAL.items():
            for field in ("purpose_note", "destination"):
                with self.subTest(char=name, field=field):
                    response = self.annotate(**{field: "Client" + char + "visit"})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(name.encode(), response.data)
        # Nothing was saved: the seeded annotation is untouched.
        self.assertEqual(self.stored()[0], "example")

    def test_save_keeps_ordinary_text_including_tabs_and_line_breaks(self):
        note = "Line one" + chr(13) + chr(10) + chr(9) + "line two " + chr(0xE9) + chr(0x2014)
        response = self.annotate(purpose_note=note, destination="Caf" + chr(0xE9))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(), (note, "Caf" + chr(0xE9)))

    def test_xlsx_export_survives_old_bad_values(self):
        # A value saved before the check, or a device ID from an upload.
        bad = {name: "Client" + char + "visit" for name, char in self.ILLEGAL.items()}
        for name, text in bad.items():
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE annotations SET purpose_note=?, destination=? WHERE trip_key='2026-01/drive'",
                             (text, text))
            with self.subTest(char=name):
                self.signed_in("admin")
                response = self.client.get("/export.xlsx")
                self.assertEqual(response.status_code, 200)
                sheet = load_workbook(io.BytesIO(response.data))["Mileage"]
                values = [cell.value for row in sheet.iter_rows() for cell in row]
                self.assertIn("Clientvisit", values)

    def test_xlsx_cleans_uploaded_text_such_as_device_ids(self):
        summary = self.viewer.Path(self.viewer.DATA_ROOT / "2026-01/drive.json")
        import json
        data = json.loads(summary.read_text(encoding="utf-8"))
        data["device_id"] = "tr" + chr(0x0B) + "acker" + chr(0xFFFE)
        summary.write_text(json.dumps(data), encoding="utf-8")
        self.signed_in("admin")
        response = self.client.get("/export.xlsx")
        self.assertEqual(response.status_code, 200)
        values = [c.value for row in load_workbook(io.BytesIO(response.data))["Mileage"].iter_rows() for c in row]
        self.assertIn("tracker", values)

    # -- length ------------------------------------------------------------

    def test_notes_and_destinations_have_a_server_side_limit_and_field_maxlength(self):
        limit = self.viewer.ANNOTATION_MAX_CHARS
        self.assertLess(limit, self.viewer.XLSX_CELL_MAX_CHARS)
        for field in ("purpose_note", "destination"):
            with self.subTest(field=field):
                over = self.annotate(**{field: "x" * (limit + 1)})
                self.assertEqual(over.status_code, 400)
                self.assertIn(str(limit).encode(), over.data)
                self.assertEqual(self.annotate(**{field: "x" * limit}).status_code, 302)
        page = self.client.get("/trip/2026-01/drive").data.decode()
        self.assertEqual(page.count(f'maxlength="{limit}"'), 2)

    def test_xlsx_flags_a_stored_value_too_long_for_a_cell(self):
        long_text = "a" * 40000
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE annotations SET purpose_note=? WHERE trip_key='2026-01/drive'", (long_text,))
        self.signed_in("admin")
        sheet = load_workbook(io.BytesIO(self.client.get("/export.xlsx").data))["Mileage"]
        cells = [c.value for row in sheet.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("aaa")]
        self.assertEqual(len(cells), 1)
        self.assertLessEqual(len(cells[0]), 32767)
        self.assertIn("TRUNCATED: 40000 characters", cells[0])
        csv_text = self.client.get("/export.csv").data.decode()
        self.assertIn(long_text, csv_text)

    # -- stored times with no local equivalent (year 1, year 9999) --------

    def write_trip(self, trip_id, **summary):
        month = self.viewer.DATA_ROOT / "2026-01"
        base = {"trip_id": trip_id, "device_id": "example", "distance_miles": 3,
                "duration_seconds": 300, "start_time": "2026-01-03T12:00:00Z",
                "end_time": "2026-01-03T12:05:00Z"}
        base.update(summary)
        (month / f"{trip_id}.json").write_text(json.dumps(base), encoding="utf-8")
        (month / f"{trip_id}.gpx").write_text("<gpx><trk><trkseg/></trk></gpx>", encoding="utf-8")

    def test_times_with_no_local_equivalent_are_unknown_not_a_500(self):
        cases = [
            ("America/Chicago", "year1", {"start_time": "0001-01-01T00:00:00Z"}),
            ("Asia/Tokyo", "year9999", {"recorded_until": "9999-12-31T23:59:59Z",
                                          "end_time": "9999-12-31T23:59:59Z"}),
            ("Asia/Tokyo", "start9999", {"start_time": "9999-12-31T23:00:00Z"}),
        ]
        for zone, trip_id, summary in cases:
            with self.subTest(zone=zone, trip=trip_id):
                self.write_trip(trip_id, **summary)
                with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo(zone)):
                    self.assertIsNone(self.viewer._to_local(self.viewer._parse(
                        summary.get("start_time") or summary["recorded_until"])))
                    self.assertEqual(self.viewer.fmt_local_iso(
                        summary.get("start_time") or summary["recorded_until"]), "")
                    self.signed_in("admin")
                    for path in ("/", "/?year=2026", "/export.csv", "/export.xlsx", "/admin",
                                 "/admin/year/2026", f"/trip/2026-01/{trip_id}"):
                        response = self.client.get(path)
                        self.assertEqual(response.status_code, 200, path)
                        response.close()
                    # The admin can still take it out of the log.
                    removed = self.client.post(f"/trip/2026-01/{trip_id}/remove", data={
                        "csrf_token": "known-token", "reason": "bad timestamp"})
                    self.assertEqual(removed.status_code, 302)

    def test_year_1_trip_is_listed_as_undated_and_stays_out_of_every_year(self):
        self.write_trip("year1", start_time="0001-01-01T00:00:00Z")
        with patch.object(self.viewer, "DISPLAY_TZ", ZoneInfo("America/Chicago")):
            trips, _ = self.viewer.load_trips()
            trip = next(t for t in trips if t["id"] == "year1")
            self.assertIsNone(self.viewer._trip_local_year(trip))
            self.assertEqual(self.viewer.trip_years(trip), set())
            # With a year closed, the closed-year check runs on every edit and
            # must cope with the unconvertible date too.
            self.signed_in("admin")
            self.assertEqual(self.client.post("/admin/year/2026/close", data={
                "csrf_token": "known-token", "confirm": "yes"}).status_code, 302)
            self.assertEqual(self.client.get("/trip/2026-01/year1").status_code, 200)
            self.assertEqual(self.client.post("/trip/2026-01/year1/annotate", data={
                "csrf_token": "known-token", "category": "business"}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
