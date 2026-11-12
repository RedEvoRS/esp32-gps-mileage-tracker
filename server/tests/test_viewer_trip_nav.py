"""Trip navigation follows the list's openable rows and local-year selection."""

import re
from unittest.mock import patch

from test_viewer_years import YearTestBase


class TripNavigationTests(YearTestBase):
    def nav(self, key, query="", client=None):
        response = (client or self.admin).get(f"/trip/{key}{query}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        nav = re.search(r'<nav class="trip-nav" aria-label="Trip navigation">.*?</nav>', page, re.S).group()
        self.assertRegex(page, r'<div class="wrap">\s*' + re.escape(nav) + r'\s*<h2>')
        return nav

    def assert_neighbours(self, key, older=None, newer=None, query="", client=None):
        nav = self.nav(key, query, client)
        for target, label in ((older, "← Previous"), (newer, "Next →")):
            if target:
                self.assertIn(f'<a class="btn" href="/trip/{target}{query or "?year="}">{label}</a>', nav)
            else:
                self.assertIn(f'<span class="btn" aria-disabled="true">{label}</span>', nav)
        return nav

    def test_middle_and_ends_for_both_roles_reuse_full_snapshot(self):
        old = self.add_trip("old", "2026-03-01T12:00:00Z")
        middle = self.add_trip("middle", "2026-03-02T12:00:00Z")
        new = self.add_trip("new", "2026-03-03T12:00:00Z")
        for client in (self.admin, self.editor):
            with patch.object(self.viewer, "load_trips", wraps=self.viewer.load_trips) as load:
                self.assert_neighbours(middle, old, new, client=client)
                load.assert_called_once_with(include_removed=True)
            self.assert_neighbours(new, middle, client=client)
            self.assert_neighbours(old, newer=middle, client=client)

    def test_year_filter_uses_local_year_and_preserves_query(self):
        old = self.add_trip("old", "2026-12-31T12:00:00Z")
        # January UTC folder, but still December locally.
        middle = self.add_trip("middle", "2027-01-01T03:00:00Z")
        new = self.add_trip("new", "2027-01-01T12:00:00Z")
        self.assert_neighbours(middle, old, query="?year=2026")
        self.assert_neighbours(old, newer=middle, query="?year=2026")
        self.assert_neighbours(new, query="?year=2027")
        self.assert_neighbours(middle, old, new)
        self.assert_neighbours(middle, query="?year=2025")

    def test_removed_and_unreadable_are_skipped_and_removed_page_has_no_nav(self):
        old = self.add_trip("old", "2026-03-01T12:00:00Z")
        removed = self.add_trip("removed", "2026-03-02T12:00:00Z")
        self.add_broken("broken", "2026-03")
        new = self.add_trip("new", "2026-03-03T12:00:00Z")
        self.assertEqual(self.post(f"/trip/{removed}/remove", reason="Duplicate").status_code, 302)
        self.assert_neighbours(new, old)
        self.assert_neighbours(old, newer=new)
        with patch.object(self.viewer, "load_trips", wraps=self.viewer.load_trips) as load:
            response = self.admin.get(f"/trip/{removed}")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('<nav class="trip-nav"', response.get_data(as_text=True))
            load.assert_not_called()

    def test_unknown_device_and_single_trip_load_once(self):
        old = self.add_trip("old", "2026-03-01T12:00:00Z", device="unknown")
        with patch.object(self.viewer, "load_trips", wraps=self.viewer.load_trips) as load:
            self.assert_neighbours(old)
            load.assert_called_once_with(include_removed=True)
        new = self.add_trip("new", "2026-03-03T12:00:00Z")
        self.assert_neighbours(old, newer=new)

    def test_merged_parts_redirect_and_inside_merge_row_remains_openable(self):
        first = self.add_trip("first", "2026-03-01T12:00:00Z")
        last = self.add_trip("last", "2026-03-01T12:03:00Z")
        response = self.post(f"/trip/{first}/merge", next_trip=last)
        self.assertEqual(response.status_code, 302)
        merged = response.headers["Location"].split("/trip/")[1].split("?")[0]
        late = self.add_trip("late", "2026-03-01T12:01:30Z")
        after = self.add_trip("after", "2026-03-02T12:00:00Z")
        page = self.admin.get("/?year=2026").get_data(as_text=True)
        rows = re.findall(r'<a class="btn" href="/trip/([^?]+)\?year=2026">Map / Edit</a>', page)
        self.assertEqual(rows, [after, late, merged])
        self.assertIn("Inside a merged trip", page)
        self.assert_neighbours(late, merged, after, query="?year=2026")
        self.assert_neighbours(merged, newer=late, query="?year=2026")
        with patch.object(self.viewer, "load_trips", wraps=self.viewer.load_trips) as load:
            response = self.admin.get(f"/trip/{first}?year=2026")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith(f"/trip/{merged}?year=2026"))
            load.assert_not_called()

    def test_known_device_snapshot_retry_remains_bounded(self):
        key = self.add_trip("sample", "2026-03-01T12:00:00Z")
        snapshot = self.viewer.load_trips(include_removed=True)
        with patch.object(self.viewer, "load_trips", side_effect=[([], []), snapshot]) as load:
            self.assert_neighbours(key)
            self.assertEqual(load.call_count, 2)
        with patch.object(self.viewer, "load_trips", return_value=([], [])) as load:
            self.assertEqual(self.admin.get(f"/trip/{key}").status_code, 404)
            self.assertEqual(load.call_count, 2)
