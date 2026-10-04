"""Deny-by-default feature access: route classification, middleware, llm.guard,
inline AI checks and the trimmed nav."""
from unittest.mock import patch

from django.urls import URLPattern, URLResolver, get_resolver, reverse

from workouts import llm
from workouts.access import (
    ADMIN_URL_NAMES, CORE_URL_NAMES, FEATURES, OWNER_URL_NAMES, PUBLIC_URL_NAMES, access_for, has_feature,
)
from workouts.tests.helpers import TwoUserTestCase


def _all_url_names(patterns=None):
    names = []
    for p in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(p, URLResolver):
            names += _all_url_names(p.url_patterns)
        elif isinstance(p, URLPattern) and p.name:
            names.append(p.name)
    return names


class RouteClassificationTests(TwoUserTestCase):
    def test_every_named_route_is_classified_exactly_once(self):
        buckets = [PUBLIC_URL_NAMES, CORE_URL_NAMES, OWNER_URL_NAMES, ADMIN_URL_NAMES] + \
                  [set(f["url_names"]) for f in FEATURES.values()]
        problems = []
        for name in sorted(set(_all_url_names())):
            hits = sum(name in bucket for bucket in buckets)
            if hits != 1:
                problems.append(f"{name}: in {hits} access lists")
        self.assertEqual(problems, [], "Every route must be public, core, owner, admin or one feature's:\n"
                                       + "\n".join(problems))


class AccessTests(TwoUserTestCase):
    def grant(self, *features, ai=False):
        access = access_for(self.b)
        access.features, access.ai_enabled = list(features), ai
        access.save()

    def status(self, client, name, *args, **kw):
        return client.get(reverse(name, args=args), **kw).status_code

    def test_user_with_no_features_gets_403_except_core_pages(self):
        self.grant()
        for name in ("nutrition", "history", "insights", "body", "program_list"):
            self.assertEqual(self.status(self.client_b, name), 403, name)
        for name in ("today", "settings", "integrations_settings"):
            self.assertEqual(self.status(self.client_b, name), 200, name)
        self.assertContains(self.client_b.get(reverse("history")), "isn't turned on for your account", status_code=403)

    def test_ai_feature_needs_the_master_switch(self):
        self.grant("nutrition", "ai_food_parse", ai=False)
        self.assertEqual(self.status(self.client_b, "nutrition"), 200)
        self.assertEqual(self.client_b.post(reverse("nutrition_parse"), {"raw_text": "eggs"}).status_code, 403)
        self.grant("nutrition", "ai_food_parse", ai=True)
        with patch("workouts.ai.llm.call_json", return_value={"items": [{"name": "eggs"}], "confidence": "high"}), \
                patch("workouts.ai._lookup_open_food_facts", return_value=[]), \
                patch("workouts.ai._lookup_branded_nutrition", return_value=[]):
            self.assertEqual(self.client_b.post(reverse("nutrition_parse"), {"raw_text": "eggs"}).status_code, 200)

    def test_requires_are_enforced(self):
        self.grant("programs")
        self.assertFalse(has_feature(self.b, "programs"))
        self.assertEqual(self.status(self.client_b, "program_list"), 403)
        self.grant("programs", "training")
        self.assertEqual(self.status(self.client_b, "program_list"), 200)
        self.grant("training", "programs", "ai_program_tools", ai=True)
        self.assertTrue(has_feature(self.b, "ai_program_tools"))
        self.grant("ai_program_tools", "programs", ai=True)       # training missing two levels down
        self.assertFalse(has_feature(self.b, "ai_program_tools"))

    def test_garmin_routes_are_owner_only(self):
        self.assertEqual(self.status(self.client_b, "sync_garmin_new"), 403)
        with patch("workouts.sync._run_garmin_sync_new", return_value={"done": True}):
            self.assertEqual(self.status(self.client_a, "sync_garmin_new"), 200)

    def test_htmx_403_is_a_partial(self):
        self.grant()
        resp = self.client_b.get(reverse("history"), HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(b"<html", resp.content.lower())
        self.assertIn(b"isn", resp.content)

    def test_guard_denies_ungranted_ai_feature(self):
        self.grant("nutrition")
        with self.assertRaises(llm.AIFeatureDenied):
            llm.guard(self.b, "ai_chat")
        llm.guard(self.a, "ai_chat")   # superuser: fine

    def test_day_view_skips_ai_without_the_feature(self):
        self.grant("training")
        with patch("workouts.views._get_or_generate_day_analysis") as gen:
            self.assertEqual(self.status(self.client_b, "day_view", "2026-09-01"), 200)
            gen.assert_not_called()

    def test_nav_shows_only_granted_groups(self):
        self.grant("nutrition")
        html = self.client_b.get(reverse("today")).content.decode()
        self.assertIn('href="/nutrition/" class="nav-link nav-group-toggle"', html)
        for group in ("Training <span", "Body <span", "Insights <span"):
            self.assertNotIn(group, html)
        self.assertNotIn("Garmin Sync New", html)
        self.assertNotIn('id="chat-sidebar"', html)
        owner_html = self.client_a.get(reverse("today")).content.decode()
        self.assertIn("Training <span", owner_html)

    def test_unknown_route_still_404s(self):
        self.assertEqual(self.client_b.get("/no-such-page/").status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        resp = self.client_class().get(reverse("today"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])
