"""Viewer response headers and login redirect safety (2026-10-23 audit)."""

import builtins
import csv
import errno
import html
import io
import os
import json
import sqlite3
import stat
from types import SimpleNamespace
from pathlib import Path
import unittest
from contextlib import closing, ExitStack
from unittest.mock import patch
from urllib.parse import quote

from openpyxl import load_workbook
from zoneinfo import ZoneInfo

import test_viewer_roles as roles
from month_link_layouts import (GLOBAL_UPLOAD_LAYOUTS, MONTH_LINK_LAYOUTS, MONTH_PROBLEM_LAYOUTS, PERMISSION_MONTH_LAYOUTS,
                                WRITABLE_MONTH_LAYOUTS, make_layout, restricted_layout)


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
                        self.assertIn(problem, text.lower())
                        self.assertNotIn("2026-01/drive", text)
                        self.assertNotIn('/trip/2026-01/', text)
                        if route == "/":
                            self.assertIn("1 month folder(s)", text)
                            self.assertIn("None of its trips are shown or counted.", text)
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
                            self.assertIn(problem, text.lower())
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
            self.assertEqual(unreadable[0]["problem"], "the folder could not be read or entered")

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
        self.assertEqual(unreadable[0]["problem"], "the folder could not be read or entered")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() != 0, "requires non-root POSIX permissions")
    def test_unlistable_month_is_reported_and_pages_still_load(self):
        month = self.viewer.DATA_ROOT / "2026-02"
        month.mkdir()
        month.chmod(0)
        self.addCleanup(month.chmod, 0o700)
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertEqual([t["trip_key"] for t in unreadable], ["2026-02/"])
        self.assertEqual(unreadable[0]["problem"], "the folder could not be read or entered")
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

    def assert_alias_counted_once(self, key, alias, ingest_notice=False):
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], [key])
        self.assertEqual(self.viewer.mileage_totals(trips)["all"], 1)
        duplicates = [t for t in unreadable if t["folder_reason"] == "duplicate"]
        self.assertEqual([t["trip_key"] for t in duplicates], [alias + "/"])
        self.assertEqual(len(unreadable), 2 if ingest_notice else 1)
        self.assertTrue(duplicates[0]["folder"])
        self.assertIn("already counted", duplicates[0]["problem"])
        self.assertNotIn(str(self.viewer.DATA_ROOT), duplicates[0]["problem"])
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
            notices = {r["trip_id"] for r in rows if r["row_type"] == "folder_notice"}
            self.assertEqual(notices, {alias + "/", key.split("/")[0] + "/"} if ingest_notice else {alias + "/"})
            book = load_workbook(io.BytesIO(client.get("/export.xlsx").data))
            self.assertEqual(book["Mileage"].max_row, 2)
            self.assertEqual([r[3] for r in book["Totals"].values if r[2] == "all"], [1])
            self.assertEqual(book["Unreadable trips"].max_row, 3 if ingest_notice else 2)
            self.assertIn("whose trips are counted there", str(list(book["Unreadable trips"].values)))
            notice = next(r for r in rows if r["trip_id"] == alias + "/")
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
        self.assert_alias_counted_once("2026-01/drive", "2027-01", ingest_notice=True)

    def test_single_month_link_to_nonmonth_folder_scans_under_link_name(self):
        month = self.viewer.DATA_ROOT / "2026-01"
        target = self.viewer.DATA_ROOT / "archive"
        month.rename(target)
        self.symlink(month, target, directory=True)
        trips, unreadable = self.viewer.load_trips()
        self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
        self.assertEqual([t["folder_reason"] for t in unreadable], ["ingest_refused"])
        self.assertEqual(trips[0]["purpose_note"], "example")
        self.assertEqual(trips[0]["category"], "business")

    def test_nonmonth_link_notice_explains_queued_uploads_without_missing_miles(self):
        self.linked_archive("2026-01")
        trips, notices = self.viewer.load_trips()
        self.assertEqual(self.viewer.mileage_totals(trips)["all"], 1)
        self.assertEqual(self.viewer.year_figures(trips, notices)["unreadable"], 0)
        self.signed_in("admin")
        for path in ("/", "/admin/year/2026", "/export.csv"):
            text = self.client.get(path).get_data(as_text=True)
            self.assertIn("Trips are shown and counted", text)
            self.assertIn("cannot upload new trips for this month", text)
            self.assertIn("the tracker keeps them queued", text)
            self.assertNotIn("totals below may be short", text)
            self.assertNotIn("totals above may be short", text)
            self.assertNotIn("none of its trips are shown or counted", text)
        self.assertEqual(self.client.post("/admin/year/2026/close", data={
            "csrf_token": "known-token", "confirm": "yes"}).status_code, 302)

    def linked_archive(self, name="2026-02"):
        root = self.viewer.DATA_ROOT
        (root / "2026-01").rename(root / "archive")
        self.symlink(root / name, root / "archive", directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE annotations SET trip_key=?", (name + "/drive",))
        return root / "archive"

    def folder_surface_messages(self, client, year=2026, include_review=True):
        """Read actual HTML and export cells, without matching CSV quoting."""
        result = {}
        paths = ["/", "/export.csv", "/export.xlsx"]
        if include_review:
            paths.append(f"/admin/year/{year}")
        for path in paths:
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            try:
                if path.endswith(".xlsx"):
                    book = load_workbook(io.BytesIO(response.data))
                    try:
                        if "Unreadable trips" in book:
                            rows = list(book["Unreadable trips"].values)
                            result[path] = [r[rows[0].index("purpose_note")] for r in rows[1:]]
                        else:
                            result[path] = []
                    finally:
                        book.close()
                elif path.endswith(".csv"):
                    result[path] = [r["purpose_note"] for r in csv.DictReader(io.StringIO(response.get_data(as_text=True)))
                                    if r["row_type"] in ('unreadable', 'unreadable_folder', 'folder_notice', 'folder_warning')]
                else:
                    result[path] = [html.unescape(response.get_data(as_text=True))]
            finally:
                response.close()
        return result

    def test_every_folder_reason_has_complete_sentences_on_every_surface(self):
        entries, expected = [], []
        descriptions = {
            "outside": "The folder links outside the data folder.",
            "dangling": "The folder link target is missing.",
            "loop": "The folder link forms a loop.",
            "unreadable": "The folder could not be read or entered.",
            "duplicate": "This is a link to 2026-12/, whose trips are counted there.",
            "ingest_refused": "Trips are shown and counted. The tracker cannot upload new trips for this month until the link is replaced by a real folder; the tracker keeps them queued.",
            "saved_keys": "Warning: saved trip data exists under 2026-01/, 2026-12/. Trips are shown and counted under 2026-12/; saved data under the other names is not applied. Years cannot be closed until this conflict is resolved. Healthy trips can still be merged under the name in use.",
            "link_not_folder": "The month name is a link to something other than a folder.",
            "not_folder": "The month name is a file, not a folder.",
            "unused_merge": "This merge is saved under a month name no longer in use (2026-01/, 2026-12/). Saved miles: unknown; purpose: unclassified; purpose note: none. These saved miles and this purpose are not in the totals shown. The trips it was made from are counted separately. Its tax year cannot be closed until this saved merge is resolved.",
            "unwritable": "Trips are shown and counted, but the folder's owner or mode prevents the server writing to it. The tracker cannot upload new trips for this month until the folder's owner or mode is fixed; the tracker keeps them queued.",
        }
        hints = {
            "duplicate": "Remove the extra link.",
            "ingest_refused": "Replace the link with a real folder containing these trips, or mount the disk there, so queued trips can upload.",
            "saved_keys": "Choose which name's saved data to use and keep only that link. Removing a link leaves its saved data unapplied; removing the name in use quietly orphans that data. A new merge is saved under the name in use; choosing another name leaves that merge unapplied too. Nothing is deleted from the database. Recreate the same link to get back; competing saved names bring the conflict back. See DEPLOY_DEBIAN.md, Troubleshooting, for steps.",
            "unreadable": "Check the folder's owner and permissions; the viewer container runs as UID 1000 and reads data/ read-only.",
            "link_not_folder": "Remove only the link, keeping its target; ingest can then create a real month folder for queued trips.",
            "not_folder": "Move the file to a safe place outside the month names; ingest can then create a real month folder for queued trips.",
            "unused_merge": "To use this merge again, restore the original month name, as a link or by renaming the folder back, and leave that name in use. Or use Undo saved merge on the trip list; this keeps the separate trips and their own annotations.",
            "unwritable": "Give the ingest service's user (UID 1000) write and search permission on the real month folder; check its owner and permissions.",
        }
        cases = [(reason, False, False) for reason in descriptions]
        cases += [("saved_keys", True, False), ("duplicate", True, False), ("dangling", True, False)]
        cases += [("link_not_folder", True, False), ("not_folder", True, False)]
        cases += [(reason, False, True) for reason in ("outside", "dangling", "loop", "unreadable")]
        for number, (reason, refused, all_refused) in enumerate(cases, 1):
            entries.append({"folder": True, "ym": f"2026-{number:02}", "trip_key": f"2026-{number:02}/",
                            "id": "", "folder_reason": reason, "problem": self.viewer.FOLDER_PROBLEMS[reason],
                            "counted_as": "2026-12", "saved_months": [] if reason == "duplicate" else ["2026-01", "2026-12"],
                            "uploads_refused": refused, "all_uploads_refused": all_refused, "has_gpx": False})
            text = descriptions[reason]
            if refused:
                if reason == "not_folder":
                    text += " The tracker cannot upload new trips for this month until this name is a folder; the tracker keeps them queued."
                else:
                    repair = "removed" if reason in ("duplicate", "link_not_folder") else "replaced by a real folder"
                    text += f" The tracker cannot upload new trips for this month until the link is {repair}; the tracker keeps them queued."
            if all_refused:
                text += " All of the tracker's trip uploads are refused until the month folder problems below are fixed; the tracker keeps them queued."
            if reason in ("outside", "dangling", "loop", "unreadable"):
                text += " None of its trips are shown or counted."
            text += " " + hints.get(reason, "Replace the link with the real folder, or mount the other disk at that path.")
            expected.append(text)
        nouns = (("annotations", "annotation"), ("removals", "removal"), ("merges", "merge"),
                 ("merge parts", "merge part"), ("old merge addresses", "old merge address"),
                 ("history entries", "history entry"), ("closed-year trip entries", "closed-year trip entry"))
        for count in (0, 1, 2):
            entries.append({**entries[6], "saved_counts": {"2026-12": dict.fromkeys(dict(nouns), count)}})
            amounts = ", ".join(f"{count} {singular if count == 1 else plural}" for plural, singular in nouns)
            expected.append(f"2026-12/ (in use): {amounts}.")
        self.signed_in("admin")
        with patch.object(self.viewer, "_load_trips", return_value=([], entries)):
            for path, texts in self.folder_surface_messages(self.client).items():
                for message in expected:
                    with self.subTest(surface=path, message=message):
                        self.assertTrue(any(message in text for text in texts))
                for text in texts:
                    self.assertNotIn(". trips are shown", text)
                    self.assertNotIn("queued Replace", text)
                    self.assertNotIn("there Remove", text)
        self.signed_in("editor")
        with patch.object(self.viewer, "_load_trips", return_value=([], entries)):
            for path, texts in self.folder_surface_messages(self.client, include_review=False).items():
                for entry in entries:
                    message = self.viewer.folder_message(entry, "editor")
                    self.assertIn("Please ask the admin", message)
                    if entry['folder_reason'] == 'unused_merge':
                        self.assertIn('restore the original month name, as a link or by renaming the folder back', message)
                        self.assertIn('or use Undo saved merge to keep the separate trips and their own annotations.', message)
                    self.assertTrue(any(message in text for text in texts), path)

    def test_damaged_merge_has_same_recovery_message_on_every_surface_for_both_roles(self):
        entry = {'trip_key': '2026-01/merge-example', 'ym': '2026-01', 'id': 'merge-example',
                 'parts': ['2026-01/a', '2026-01/b'], 'gpx_file': '2026-01/a.gpx; 2026-01/b.gpx',
                 'has_gpx': False, 'damaged_merge': True, 'category': 'business',
                 'purpose_note': 'example note', 'destination': '',
                 'saved_summary': {'start_time': '2026-01-02T12:00:00Z', 'distance_miles': .732},
                 'problem': self.viewer.trip_merges.DAMAGED_MERGE_PROBLEM}
        for role in ('admin', 'editor'):
            self.signed_in(role)
            message = self.viewer.folder_message(entry, role)
            self.assertIn('Saved merge: 0.732 mi; purpose: business.', message)
            self.assertIn('The original trips cannot be restored from the viewer; restore the files from backups.', message)
            self.assertIn('Or use Discard damaged merge:' if role == 'admin' else 'Please ask the admin', message)
            with patch.object(self.viewer, '_load_trips', return_value=([], [entry])):
                for path, texts in self.folder_surface_messages(self.client, include_review=role == 'admin').items():
                    self.assertTrue(any(message in text for text in texts), (role, path))

    def test_month_upload_notices_match_ingest_shared_link_layouts_on_every_surface(self):
        summary = (self.viewer.DATA_ROOT / "2026-01/drive.json").read_bytes()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM annotations")
        self.signed_in("admin")
        for label, layout in MONTH_LINK_LAYOUTS.items():
            root = self.viewer.DATA_ROOT / label
            make_layout(root, layout, self.symlink)
            (root / layout["counted"] / "drive.json").write_bytes(summary)
            with patch.object(self.viewer, "DATA_ROOT", root):
                trips, entries = self.viewer.load_trips()
                self.assertEqual([t["trip_key"] for t in trips], [layout["counted"] + "/drive"])
                self.assertEqual(self.viewer.year_figures(trips, entries)["unreadable"], 0)
                self.assertEqual([e["ym"] for e in entries if e["folder_reason"] == "duplicate"], layout["duplicates"])
                for entry in entries:
                    expected = layout["uploads"][entry["ym"]] == 503
                    self.assertEqual(entry["uploads_refused"], expected)
                    message = self.viewer.folder_message(entry, "admin")
                    if expected:
                        repair = "removed" if entry["folder_reason"] == "duplicate" else "replaced by a real folder"
                        self.assertIn(f"The tracker cannot upload new trips for this month until the link is {repair}; "
                                      "the tracker keeps them queued.", message)
                    else:
                        self.assertNotIn("cannot upload", message)
                    for path, texts in self.folder_surface_messages(self.client).items():
                        with self.subTest(layout=label, month=entry["ym"], surface=path):
                            self.assertTrue(any(message in text for text in texts))

    def test_global_upload_warning_matches_ingest_layouts_on_every_surface_and_year(self):
        summary = (self.viewer.DATA_ROOT / "2026-01/drive.json").read_bytes()
        self.signed_in("admin")
        expected = ("All of the tracker's trip uploads are refused until the month folder problems below are fixed; "
                    "the tracker keeps them queued.")
        for label, layout in GLOBAL_UPLOAD_LAYOUTS.items():
            root = self.viewer.DATA_ROOT / label
            make_layout(root, layout, self.symlink)
            (root / "2026-01").mkdir()
            (root / "2026-01/drive.json").write_bytes(summary)
            with patch.object(self.viewer, "DATA_ROOT", root):
                trips, entries = self.viewer.load_trips()
                self.assertEqual([t["trip_key"] for t in trips], ["2026-01/drive"])
                blocked = [e for e in entries if e["all_uploads_refused"]]
                self.assertEqual(bool(blocked), layout["blocked"], label)
                for path, texts in self.folder_surface_messages(self.client, year=2027).items():
                    with self.subTest(layout=label, surface=path):
                        self.assertEqual(any(expected in text for text in texts), layout["blocked"])
                        for entry in blocked:
                            self.assertTrue(any(self.viewer.folder_message(entry, "admin") in text for text in texts))
                for path in ("/", "/?year=2020", "/admin/year/2020"):
                    text = html.unescape(self.client.get(path).get_data(as_text=True))
                    self.assertEqual(expected in text, layout["blocked"], (label, path))
                    if layout["blocked"]:
                        self.assertIn('role="alert"><b>Uploads are blocked.</b>', text)
                        self.assertIn("2027-02/", text)
                # Unrelated years do not gain missing mileage in their snapshot.
                self.assertEqual(self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2020))["unreadable"], 0)

    def test_month_problems_match_ingest_layouts_and_every_surface(self):
        self.signed_in("admin")
        for label, layout in MONTH_PROBLEM_LAYOUTS.items():
            root = self.viewer.DATA_ROOT / label
            make_layout(root, layout, self.symlink)
            with patch.object(self.viewer, "DATA_ROOT", root):
                trips, entries = self.viewer.load_trips()
                self.assertEqual(trips, [])
                self.assertEqual(len(entries), 1)
                entry = entries[0]
                self.assertEqual(entry["folder_reason"], layout["reason"])
                if layout['reason'] in ('not_folder', 'link_not_folder'):
                    self.assertFalse(self.viewer.missing_mileage(entry))
                    self.assertEqual(self.viewer.year_figures(*self.viewer.year_members(trips, entries, 2026))['unreadable'], 0)
                    self.assertNotIn('None of its trips are shown or counted.', self.viewer.folder_message(entry, 'admin'))
                    rows = list(csv.DictReader(io.StringIO(self.client.get('/export.csv').get_data(as_text=True))))
                    self.assertEqual(rows[0]['row_type'], 'folder_notice')
                    book = load_workbook(io.BytesIO(self.client.get('/export.xlsx').data))
                    try:
                        self.assertIn('notice:', str(list(book['Unreadable trips'].values)))
                    finally:
                        book.close()
                self.assertFalse(entry["all_uploads_refused"])
                self.assertEqual(entry["uploads_refused"], layout["uploads"]["2026-02"] == 503)
                for role in ("admin", "editor"):
                    message = self.viewer.folder_message(entry, role)
                    self.assertEqual("cannot upload new trips for this month" in message,
                                     layout["uploads"]["2026-02"] == 503)
                    if role == "admin":
                        for path, texts in self.folder_surface_messages(self.client).items():
                            self.assertTrue(any(message in text for text in texts), (label, path))
                self.assertNotIn("Uploads are blocked.", self.client.get("/?year=2020").get_data(as_text=True))

    def test_month_resolution_permission_error_is_a_global_503(self):
        month = self.viewer.DATA_ROOT / "2026-03"
        month.mkdir()
        original = Path.resolve

        def denied(path, *args, **kwargs):
            if path == month:
                raise PermissionError("synthetic resolution denial")
            return original(path, *args, **kwargs)

        with patch.object(Path, "resolve", denied):
            entries = self.viewer.load_trips()[1]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["folder_reason"], "unreadable")
        self.assertTrue(entries[0]["all_uploads_refused"])

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0, "requires non-root POSIX permissions")
    def test_permission_layouts_show_folder_warning_and_global_alert(self):
        for label, layout in PERMISSION_MONTH_LAYOUTS.items():
            root = self.viewer.DATA_ROOT / label
            with restricted_layout(root, layout, self.symlink), patch.object(self.viewer, "DATA_ROOT", root):
                with patch.object(self.viewer.os, "access", wraps=self.viewer.os.access) as access:
                    trips, entries = self.viewer.load_trips()
                self.assertLessEqual(access.call_count, 1, label)
                self.assertEqual(trips, [])
                self.assertEqual([(e["ym"], e["folder_reason"], e["all_uploads_refused"]) for e in entries],
                                 [("2026-02", "unreadable", True)], label)
                for role in ("admin", "editor"):
                    self.signed_in(role)
                    text = html.unescape(self.client.get("/?year=2020").get_data(as_text=True))
                    self.assertIn("Uploads are blocked.", text)
                    self.assertIn("2026-02/", text)
                self.signed_in("admin")
                message = self.viewer.folder_message(entries[0], "admin")
                for path, texts in self.folder_surface_messages(self.client).items():
                    self.assertTrue(any(message in text for text in texts), (label, path))
            with patch.object(self.viewer, "DATA_ROOT", root):
                self.assertEqual(self.viewer.load_trips(), ([], []))
                self.assertNotIn("Uploads are blocked.", self.client.get("/").get_data(as_text=True))

    def test_upload_alert_gives_role_specific_advice(self):
        entry = {"folder": True, "ym": "2027-02", "trip_key": "2027-02/", "id": "",
                 "folder_reason": "outside", "problem": self.viewer.FOLDER_PROBLEMS["outside"],
                 "all_uploads_refused": True}
        for role in ("admin", "editor"):
            self.signed_in(role)
            with patch.object(self.viewer, "_load_trips", return_value=([], [entry])):
                page = html.unescape(self.client.get("/?year=2020").get_data(as_text=True))
            alert = page.split('<b>Uploads are blocked.</b>', 1)[1].split('</div>', 1)[0]
            expected = ("Review the folder warnings in All years and follow their repair steps." if role == "admin"
                        else "Please tell the admin about these folders.")
            self.assertIn(expected, alert)

    def test_loop_blocks_all_uploads_when_resolve_leaves_it_unresolved(self):
        month = self.viewer.DATA_ROOT / "2026-03"
        self.symlink(month, month, directory=True)
        original = Path.resolve

        def unresolved(path, *args, **kwargs):
            return path if path == month else original(path, *args, **kwargs)

        with patch.object(Path, "resolve", unresolved):
            entry = self.viewer.load_trips()[1][0]
        self.assertEqual(entry["folder_reason"], "loop")
        self.assertTrue(entry["all_uploads_refused"])

    def test_top_upload_warning_uses_only_one_trip_scan(self):
        self.signed_in("admin")
        with patch.object(self.viewer, "load_trips", wraps=self.viewer.load_trips) as load, \
                patch.object(self.viewer, "_month_blocks_all_uploads", side_effect=AssertionError("extra link check")):
            self.assertEqual(self.client.get("/?year=2026").status_code, 200)
            self.assertEqual(load.call_count, 1)

    def test_healthy_month_checks_access_once_without_extra_upload_block_probe(self):
        original = self.viewer.os.access
        with patch.object(self.viewer.os, "access", wraps=original) as access, \
                patch.object(self.viewer, "_month_blocks_all_uploads", side_effect=AssertionError("extra block probe")):
            self.viewer.load_trips()
        self.assertEqual(access.call_count, 1)
        self.assertEqual(access.call_args.args, (self.viewer.DATA_ROOT / "2026-01", os.R_OK | os.X_OK))

    @unittest.skipIf(os.name == 'nt' or getattr(os, 'geteuid', lambda: 0)() == 0, 'requires non-root POSIX permissions')
    def test_unwritable_month_notice_matches_ingest_layout_and_every_surface(self):
        summary = (self.viewer.DATA_ROOT / '2026-01/drive.json').read_bytes()
        for label, layout in WRITABLE_MONTH_LAYOUTS.items():
            root = self.viewer.DATA_ROOT / label
            make_layout(root, layout, self.symlink)
            (root / '2026-02/drive.json').write_bytes(summary)
            with restricted_layout(root, layout, self.symlink), patch.object(self.viewer, 'DATA_ROOT', root):
                trips, entries = self.viewer.load_trips()
                self.assertEqual([t['trip_key'] for t in trips], ['2026-02/drive'])
                self.assertEqual([e['folder_reason'] for e in entries], ['unwritable'])
                self.assertFalse(entries[0]['all_uploads_refused'])
                self.assertTrue(entries[0]['uploads_refused'])
                self.assertEqual(self.viewer.year_figures(trips, entries)['unreadable'], 0)
                for role in ('admin', 'editor'):
                    self.signed_in(role)
                    message = self.viewer.folder_message(entries[0], role)
                    for path, texts in self.folder_surface_messages(self.client, include_review=role == 'admin').items():
                        self.assertTrue(any(message in text for text in texts), (role, path))
                    self.assertNotIn('Uploads are blocked.', self.client.get('/').get_data(as_text=True))
                self.signed_in('admin')
                rows = list(csv.DictReader(io.StringIO(self.client.get('/export.csv').get_data(as_text=True))))
                self.assertEqual(next(row for row in rows if row['trip_id'] == '2026-02/')['row_type'], 'folder_notice')
            with patch.object(self.viewer, 'DATA_ROOT', root):
                self.assertEqual(self.viewer.load_trips()[1], [])

    def test_month_write_modes_select_owner_group_or_other_without_filesystem_calls(self):
        cases = ((1000, 1000, 0o700, True), (1000, 1000, 0o500, False), (1000, 1000, 0o577, False),
                 (0, 0, 0o700, False), (0, 0, 0o755, False), (0, 1000, 0o770, True),
                 (0, 2000, 0o770, True), (0, 1000, 0o757, False), (0, 3000, 0o777, True),
                 (0, 3000, 0o775, False))
        with patch.object(self.viewer, '_MONTH_SERVICE_UID', 1000), \
                patch.object(self.viewer, '_MONTH_SERVICE_GROUPS', {1000, 2000}), \
                patch.object(Path, 'stat', side_effect=AssertionError('extra metadata call')):
            for uid, gid, mode, writable in cases:
                metadata = SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=stat.S_IFDIR | mode)
                self.assertEqual(self.viewer._month_mode_writable(metadata), writable, (uid, gid, mode))

    def test_read_only_viewer_mount_with_healthy_owner_and_mode_has_no_write_notice(self):
        month = self.viewer.DATA_ROOT / '2026-01'
        original_stat, original_access = Path.stat, self.viewer.os.access
        def metadata(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == month:
                fields = list(result)
                fields[0:3] = [stat.S_IFDIR | 0o700, result.st_ino, result.st_dev]
                fields[4:6] = [1000, 1000]
                return os.stat_result(fields)
            return result
        def read_only_access(path, mode, *args, **kwargs):
            if mode & os.W_OK:
                return False  # Model the mount separately from its healthy owner/mode.
            return original_access(path, mode, *args, **kwargs)
        with patch.object(self.viewer, '_MONTH_SERVICE_UID', 1000), \
                patch.object(self.viewer, '_MONTH_SERVICE_GROUPS', {1000}), \
                patch.object(Path, 'stat', metadata), patch.object(self.viewer.os, 'access', side_effect=read_only_access) as access:
            trips, entries = self.viewer.load_trips()
        self.assertEqual(len(trips), 1)
        self.assertEqual(entries, [])
        self.assertFalse(any(call.args[1] & os.W_OK for call in access.call_args_list))

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
        duplicate = next(t for t in entries if t["folder_reason"] == "duplicate")
        message = self.viewer.folder_message(duplicate, "admin")
        self.assertIn("Follow the saved-name conflict steps before removing any link", message)
        self.assertNotIn("Remove the extra link.", message)
        self.signed_in("admin")
        for path, texts in self.folder_surface_messages(self.client).items():
            self.assertTrue(any(message in text for text in texts), path)
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

    def test_saved_conflict_warns_before_close_even_for_unrelated_and_closed_years(self):
        target = self.linked_archive()
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key) VALUES ('2026-01/drive')")
            conn.execute("INSERT INTO tax_years VALUES (2020, 'admin', '', '{}')")
        self.signed_in("admin")
        for path, texts in self.folder_surface_messages(self.client).items():
            for name, status in (("2026-01", "in use"), ("2026-02", "not applied")):
                self.assertTrue(any(f"{name}/ ({status}): 1 annotation, 0 removals, 0 merges, "
                                    "0 merge parts, 0 old merge addresses, 0 history entries, "
                                    "0 closed-year trip entries." in text for text in texts), path)
        for year in (2020, 2026, 2030):
            text = html.unescape(self.client.get(f"/admin/year/{year}").get_data(as_text=True))
            self.assertIn("Years cannot be closed until this conflict is resolved.", text)
            self.assertIn("2026-01/ (in use): 1 annotation, 0 removals, 0 merges", text)
            self.assertIn("2026-02/ (not applied): 1 annotation, 0 removals, 0 merges", text)
            self.assertIn("Nothing is deleted from the database.", text)
            if year != 2020:
                self.assertIn(f'<button class="btn" disabled>Close {year}</button>', text)
                self.assertNotIn(f'action="/admin/year/{year}/close"', text)
                self.assertEqual(self.client.post(f"/admin/year/{year}/close", data={
                    "csrf_token": "known-token", "confirm": "yes"}).status_code, 409)
            else:
                self.assertIn('action="/admin/year/2020/reopen"', text)

    def test_saved_conflict_counts_all_sources_and_only_exact_month_prefixes(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key) VALUES ('2026-010/not-a-month')")
            conn.execute("INSERT INTO trip_removals VALUES ('2026-02/gone', 'example', 'admin', '')")
            conn.execute("INSERT INTO trip_merges (trip_key,summary,fingerprint) VALUES ('2026-02/merge-example','{}','[]')")
            conn.execute("INSERT INTO trip_merge_parts VALUES ('2026-01/drive','2026-02/merge-example',0)")
            conn.execute("INSERT INTO trip_merge_aliases VALUES ('2026-01/merge-old','2026-02/merge-example')")
            conn.execute("INSERT INTO trip_history (trip_key,actor,actor_role,at,action,details) VALUES ('2026-02/gone','admin','admin','','remove','{}')")
            conn.execute("INSERT INTO tax_years VALUES (2020,'admin','', ?)",
                         (json.dumps({"trip_keys": ["2026-01/drive", "2026-02/gone", "2026-02/other"]}),))
            conn.execute("INSERT INTO tax_years VALUES (2021,'admin','', 'broken')")
            counts = self.viewer._saved_month_counts(conn, ["2026-01", "2026-02"])
        self.assertEqual(counts, {
            "2026-01": {"annotations": 1, "removals": 0, "merges": 0, "merge parts": 1,
                        "old merge addresses": 1, "history entries": 0, "closed-year trip entries": 1},
            "2026-02": {"annotations": 0, "removals": 1, "merges": 1, "merge parts": 1,
                        "old merge addresses": 0, "history entries": 1, "closed-year trip entries": 2},
        })

    def test_removing_either_saved_link_and_recreating_it_preserves_database(self):
        target = self.linked_archive()
        first = self.viewer.DATA_ROOT / "2026-01"
        second = self.viewer.DATA_ROOT / "2026-02"
        self.symlink(first, target, directory=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key,category,purpose_note) VALUES ('2026-01/drive','personal','first name')")
            before = list(conn.iterdump())
        for removed, remaining in ((second, first), (first, second)):
            removed.unlink()
            trips, entries = self.viewer.load_trips()
            self.assertEqual(trips[0]["trip_key"], remaining.name + "/drive")
            self.assertEqual(trips[0]["purpose_note"], "first name" if remaining == first else "example")
            self.assertFalse(any(e["folder_reason"] == "saved_keys" for e in entries))
            self.symlink(removed, target, directory=True)
            trips, entries = self.viewer.load_trips()
            self.assertEqual(trips[0]["trip_key"], "2026-01/drive")
            self.assertTrue(any(e["folder_reason"] == "saved_keys" for e in entries))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_no_link_install_does_no_saved_name_queries(self):
        with patch.object(self.viewer, "_saved_month_names", side_effect=AssertionError("extra saved-name query")), \
                patch.object(self.viewer, "_saved_month_counts", side_effect=AssertionError("extra inventory query")):
            self.assertEqual(self.viewer.load_trips()[0][0]["trip_key"], "2026-01/drive")

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

    def assert_alias_routes(self, alias, counted):
        self.signed_in("admin")
        rules = [rule for rule in self.viewer.app.url_map.iter_rules()
                 if {"ym", "trip_id"} <= rule.arguments]
        self.assertEqual(len(rules), 8)  # Two reads and six state-changing routes.
        with closing(sqlite3.connect(self.db)) as conn:
            before = list(conn.iterdump())
        for rule in rules:
            path = rule.rule.replace("<ym>", alias).replace("<trip_id>", "drive")
            with self.subTest(endpoint=rule.endpoint):
                with patch.object(self.viewer.GEOCODER, "lookup", side_effect=AssertionError("lookup")):
                    if "GET" in rule.methods:
                        for method in ("GET", "HEAD"):
                            response = self.client.open(path + "?year=2026", method=method)
                            self.assertEqual(response.status_code, 302)
                            self.assertEqual(response.headers["Location"], path.replace(alias, counted) + "?year=2026")
                            response.close()
                    else:
                        response = self.client.post(path, data={
                            "csrf_token": "known-token", "category": "business", "purpose_note": "changed",
                            "reason": "example", "confirm": "yes", "endpoint": "start",
                            "next_trip": counted + "/drive",
                        })
                        self.assertEqual(response.status_code, 409)
                        message = "This month address is an extra link. Reopen the trip from the trip list before making changes."
                        if rule.endpoint == "lookup_place":
                            self.assertEqual(response.mimetype, "application/json")
                            self.assertEqual(response.get_json(), {"error": message})
                        else:
                            self.assertEqual(response.mimetype, "text/html")
                            self.assertIn(message, response.get_data(as_text=True))
                        response.close()
                with closing(sqlite3.connect(self.db)) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def test_all_alias_month_routes_redirect_reads_and_refuse_writes(self):
        self.symlink(self.viewer.DATA_ROOT / "2026-12", self.viewer.DATA_ROOT / "2026-01", directory=True)
        self.assert_alias_routes("2026-12", "2026-01")
        self.assertEqual(self.client.get("/trip/2026-12/drive", follow_redirects=True).status_code, 200)
        self.assertEqual(self.client.get("/gpx/2026-12/drive", follow_redirects=True).status_code, 200)

    def test_link_only_alias_routes_use_saved_name(self):
        target = self.linked_archive()
        self.symlink(self.viewer.DATA_ROOT / "2026-01", target, directory=True)
        self.assert_alias_routes("2026-01", "2026-02")

    def test_alias_read_redirect_preserves_query_bytes_and_existing_escapes(self):
        self.symlink(self.viewer.DATA_ROOT / "2026-12", self.viewer.DATA_ROOT / "2026-01", directory=True)
        self.signed_in("admin")
        cases = [(b"x=\xe9", "x=%E9"), (b"x=\xc3\xa9", "x=%C3%A9"),
                 (b"x=%c3%a9&year=2026&x=one+two&flag&value=a/b?c=d%26e",
                  "x=%c3%a9&year=2026&x=one+two&flag&value=a/b?c=d%26e"),
                 (b"x=\r\n", "x=%0D%0A")]
        for route in ("trip", "gpx"):
            for method in ("GET", "HEAD"):
                for raw, expected in cases:
                    with self.subTest(route=route, method=method, raw=raw):
                        response = self.client.open(f"/{route}/2026-12/drive", method=method,
                                                    environ_overrides={"QUERY_STRING": raw.decode("latin-1")})
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response.headers["Location"], f"/{route}/2026-01/drive?{expected}")
                        response.close()

    def test_ordinary_trip_route_needs_no_month_resolution_scan(self):
        self.signed_in("admin")
        with patch.object(self.viewer, "_month_choices", side_effect=AssertionError("extra month scan")):
            self.assertEqual(self.client.get("/gpx/2026-01/drive").status_code, 200)

    def test_folder_export_labels_separate_unreadable_notices_and_saved_conflicts(self):
        import csv
        target = self.linked_archive("2026-01")
        self.symlink(self.viewer.DATA_ROOT / "2026-02", target, directory=True)
        self.symlink(self.viewer.DATA_ROOT / "2026-03", self.viewer.DATA_ROOT / "missing", directory=True)
        self.signed_in("admin")

        def exported():
            response = self.client.get("/export.xlsx")
            book = load_workbook(io.BytesIO(response.data))
            try:
                rows = list(book["Unreadable trips"].values)
                labels = [row[rows[0].index("trip_id")] for row in rows[1:]]
            finally:
                book.close()
                response.close()
            csv_rows = list(csv.DictReader(io.StringIO(self.client.get("/export.csv").get_data(as_text=True))))
            return labels, {r["trip_id"]: r["row_type"] for r in csv_rows if r["trip_id"].endswith("/")}

        labels, types = exported()
        self.assertEqual(labels, ["2026-01/ (link blocks new uploads; existing trips counted)",
                                  "2026-02/ (extra link, trips counted elsewhere)",
                                  "2026-03/ (whole month folder)"])
        self.assertEqual(types, {"2026-01/": "folder_notice", "2026-02/": "folder_notice",
                                 "2026-03/": "unreadable_folder"})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO annotations (trip_key) VALUES ('2026-02/drive')")
        labels, types = exported()
        self.assertEqual(labels[0], "2026-01/ (conflicting saved data; trips counted)")
        self.assertEqual(types["2026-01/"], "folder_warning")

    def warning_page(self, *, folder=False, file=False):
        root = self.viewer.DATA_ROOT
        if file:
            (root / "2026-01/drive.json").write_text("{broken", encoding="utf-8")
        if folder:
            (root / "2026-02").mkdir()
        original = self.viewer._month_trip_names

        def listing(path):
            if path.name == "2026-02":
                raise PermissionError("synthetic folder denial")
            return original(path)

        self.signed_in("admin")
        with patch.object(self.viewer, "_month_trip_names", side_effect=listing):
            return self.client.get("/").get_data(as_text=True)

    def test_folder_only_warning_omits_zero_files_and_summary_rebuild_advice(self):
        text = self.warning_page(folder=True)
        self.assertIn("1 month folder(s) can't be read", text)
        self.assertNotIn("trip file(s) can't be read", text)
        self.assertNotIn("To rebuild a damaged summary", text)
        self.assertIn("owner and permissions", text)

    def test_file_only_warning_omits_zero_folders_and_keeps_rebuild_advice(self):
        text = self.warning_page(file=True)
        self.assertIn("1 trip file(s) can't be read", text)
        self.assertNotIn("month folder(s) can't be read", text)
        self.assertIn("To rebuild a damaged summary", text)

    def test_mixed_warning_shows_both_counts_and_summary_rebuild_advice(self):
        text = self.warning_page(folder=True, file=True)
        self.assertIn("1 trip file(s) can't be read", text)
        self.assertIn("1 month folder(s) can't be read", text)
        self.assertIn("To rebuild a damaged summary", text)

    def test_year_heading_omits_folder_notices_without_folder_entries(self):
        self.signed_in("admin")
        page = self.client.get("/admin/year/2026").get_data(as_text=True)
        self.assertIn('id="unreadable-heading">Trips whose files can\'t be read (0)</h2>', page)
        self.assertNotIn("and folder notices", page)
        (self.viewer.DATA_ROOT / "2026-01/drive.json").write_text("{broken", encoding="utf-8")
        page = self.client.get("/admin/year/2026").get_data(as_text=True)
        self.assertIn('id="unreadable-heading">Trips whose files can\'t be read (1)</h2>', page)
        self.assertNotIn("and folder notices", page)

    def test_year_heading_mentions_folders_only_for_years_with_folder_entries(self):
        (self.viewer.DATA_ROOT / "2027-02").mkdir()
        self.signed_in("admin")
        original = self.viewer._month_trip_names

        def listing(path):
            if path.name == "2027-02":
                raise PermissionError("synthetic folder denial")
            return original(path)

        with patch.object(self.viewer, "_month_trip_names", side_effect=listing):
            self.assertIn("and folder notices</h2>", self.client.get("/admin/year/2027").get_data(as_text=True))
            self.assertNotIn("and folder notices", self.client.get("/admin/year/2026").get_data(as_text=True))

    def test_damaged_merge_with_linked_month_part_requires_admin_discard(self):
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
        self.assertNotIn(b'>Undo merge (split back)</button>', page.data)
        self.assertIn(b'>Discard damaged merge</button>', page.data)
        self.assertEqual(self.client.post('/trip/2026-02/merged/split', data={
            'csrf_token': 'known-token', 'confirm': 'yes'}).status_code, 409)

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
