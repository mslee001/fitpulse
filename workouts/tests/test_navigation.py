import re

from workouts.navigation import NAV_GROUPS, group_for_url_name, nav_for, section_tabs
from workouts.tests.helpers import TwoUserTestCase, make_user


def menu_links(html, group):
    """(href, label, muted) for one group's desktop dropdown."""
    block = re.search(r'data-nav-group="%s">.*?<ul[^>]*>(.*?)</ul>' % group, html, re.S)
    if not block:
        return None
    return re.findall(r'<li><a (?:class="(text-muted)" )?href="([^"]+)">([^<]+)</a></li>|<li aria-hidden="true"></li>', block.group(1))


def tabs(html):
    m = re.search(r'<nav aria-label="([^"]+) pages" id="section-tabs".*?</nav>', html, re.S)
    if not m:
        return None
    return m.group(1), re.findall(r'<a href="([^"]+)" class="tab[^"]*"( aria-current="page")?>([^<]+)</a>', m.group(0))


class NavigationTests(TwoUserTestCase):
    def test_limited_user_sees_only_permitted_tabs(self):
        c = make_user("carol", features=["training", "nutrition"])
        self.client.force_login(c)
        label, items = tabs(self.client.get("/history/").content.decode())
        self.assertEqual(label, "Training")
        names = [t[2] for t in items]
        self.assertEqual(names, ["Workouts", "Dashboard", "Compare", "Calendar", "Analytics"])
        self.assertEqual([t[2] for t in items if t[1]], ["Workouts"])

    def test_owner_sees_users_in_the_menu_never_as_a_tab(self):
        html = self.client_a.get("/history/").content.decode()
        _, items = tabs(html)
        names = [t[2] for t in items]
        self.assertIn("Programs", names)
        self.assertIn("Strength", names)
        self.assertNotIn("Users", names)
        self.assertNotIn("Settings", names)
        menu = [m[2] for m in menu_links(html, "training") if m[2]]
        self.assertIn("Users", menu)
        self.assertIn("Settings", menu)

    def test_group_with_one_visible_item_renders_no_tabs(self):
        c = make_user("dave", features=["body"])
        self.client.force_login(c)
        self.assertIsNone(tabs(self.client.get("/body/").content.decode()))
        self.assertIsNone(section_tabs(c, "body"))

    def test_pages_outside_groups_have_no_tabs(self):
        for url in ("/", "/settings/", "/settings/integrations/"):
            self.assertIsNone(tabs(self.client_a.get(url).content.decode()), url)
        self.assertIsNone(group_for_url_name("settings"))        # muted: menu only
        self.assertIsNone(group_for_url_name("workout_detail"))
        self.assertIsNone(group_for_url_name("day_view"))
        self.assertEqual(group_for_url_name("nutrition_analytics"), "nutrition")

    def test_menus_match_the_old_hand_written_lists(self):
        """The data-driven menus render the same items, order and muting as the
        partials/nav_items.html they replaced, for an owner and a limited user."""
        old = {
            "training": [("", "/history/", "Workouts"), ("", "/dashboard/", "Dashboard"),
                         ("", "/compare/", "Compare"), ("", "/calendar/", "Calendar"),
                         ("", "/analytics/", "Analytics"), ("", "/programs/", "Programs"),
                         ("", "/strength/", "Strength"), ("", "", ""),
                         ("text-muted", "/settings/", "Settings"), ("text-muted", "/settings/users/", "Users")],
            "nutrition": [("", "/nutrition/", "Today&#x27;s Log"), ("", "/nutrition/analytics/", "Analytics"),
                          ("", "", ""), ("text-muted", "/nutrition/targets/", "Macro Settings")],
        }
        html = self.client_a.get("/settings/").content.decode()
        for group, expected in old.items():
            got = [(m[0], m[1], m[2].replace("'", "&#x27;")) for m in menu_links(html, group)]
            self.assertEqual(got, expected, group)
        limited = make_user("erin", features=["training", "interventions"])
        self.client.force_login(limited)
        html = self.client.get("/settings/").content.decode()
        training = [m[2] for m in menu_links(html, "training") if m[2]]
        self.assertEqual(training, ["Workouts", "Dashboard", "Compare", "Calendar", "Analytics", "Settings"])
        self.assertEqual([m[2] for m in menu_links(html, "body")], ["Interventions"])
        self.assertEqual([m[2] for m in menu_links(html, "insights")], ["Intervention Analysis"])
        self.assertIsNone(menu_links(html, "nutrition"))

    def test_dividers_never_lead_trail_or_double(self):
        c = make_user("fay", features=["nutrition"])
        for group in NAV_GROUPS:
            items = nav_for(c, group)
            if items:
                self.assertIsNotNone(items[0])
                self.assertIsNotNone(items[-1])
            self.assertFalse(any(a is None and b is None for a, b in zip(items, items[1:])))
