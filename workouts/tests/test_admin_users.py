"""/settings/users/: owner-only, temp passwords shown once, forced password
change, feature/AI controls, and no health data on admin pages."""
from datetime import date, datetime, timezone as dt_tz
from decimal import Decimal

from django.contrib.auth import authenticate, get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse

from workouts import llm
from workouts.access import ADMIN_URL_NAMES, access_for
from workouts.models import AIUsage, CachedWorkout, FoodEntry, Integration, Intervention
from workouts.tests.helpers import TwoUserTestCase, make_user

User = get_user_model()


def _admin_url(name, pk):
    if name in ("admin_users", "admin_user_new"):
        return reverse(name)
    if name == "admin_user_feature_toggle":
        return reverse(name, args=[pk, "nutrition"])
    return reverse(name, args=[pk])


class AdminUsersTests(TwoUserTestCase):
    def create(self, username="carol", preset="nutrition"):
        return self.client_a.post(reverse("admin_user_new"), {"username": username, "preset": preset})

    def test_non_superuser_gets_403_everywhere(self):
        for name in ADMIN_URL_NAMES:
            url = _admin_url(name, self.a.pk)
            self.assertEqual(self.client_b.get(url).status_code, 403, name)
            self.assertEqual(self.client_b.post(url).status_code, 403, name)

    def test_create_user_with_preset_and_one_time_password(self):
        resp = self.create()
        carol = User.objects.get(username="carol")
        access = access_for(carol)
        self.assertTrue(access.must_change_password)
        self.assertEqual(sorted(access.features), ["ai_food_parse", "nutrition"])
        self.assertTrue(access.ai_enabled)
        self.assertEqual(set(Integration.objects.filter(user=carol).values_list("key", flat=True)),
                         {"peloton", "withings", "google_health"})
        password = resp.context["password"]
        self.assertEqual(len(password), 12)
        self.assertContains(resp, password)
        self.assertEqual(authenticate(username="carol", password=password), carol)
        # never stashed anywhere but this one response
        self.assertNotIn(password, str(dict(self.client_a.session)))
        self.assertFalse([m for m in get_messages(resp.wsgi_request) if password in str(m)])

    def test_duplicate_username_is_rejected(self):
        self.create("dave")
        resp = self.create("Dave")
        self.assertContains(resp, "already taken")
        self.assertEqual(User.objects.filter(username__iexact="dave").count(), 1)

    def test_new_user_must_change_password_first(self):
        carol = make_user("carol", features=["nutrition"], set_up=False)
        client = self.client_class()
        client.force_login(carol)
        for path in ("/", "/nutrition/"):
            resp = client.get(path)
            self.assertRedirects(resp, reverse("password_change"), fetch_redirect_response=False)
        resp = client.get("/nutrition/", HTTP_HX_REQUEST="true")
        self.assertEqual(resp["HX-Redirect"], reverse("password_change"))
        self.assertEqual(client.get(reverse("password_change")).status_code, 200)
        resp = client.post(reverse("password_change"), {
            "old_password": "pw", "new_password1": "lettuce-orbit-42", "new_password2": "lettuce-orbit-42"})
        self.assertRedirects(resp, reverse("password_change_done"), fetch_redirect_response=False)
        self.assertFalse(access_for(carol).must_change_password)
        # next stop is Get Started (onboarding isn't finished), not the password page
        self.assertRedirects(client.get("/"), reverse("get_started"), fetch_redirect_response=False)

    def test_reset_password_forces_a_change(self):
        resp = self.client_a.post(reverse("admin_user_reset_password", args=[self.b.pk]))
        self.assertTrue(access_for(self.b).must_change_password)
        self.assertEqual(authenticate(username="bob", password=resp.context["password"]), self.b)

    def test_feature_toggle_flips_and_returns_the_row(self):
        access = access_for(self.b)
        access.features = []
        access.save()
        resp = self.client_a.post(reverse("admin_user_feature_toggle", args=[self.b.pk, "programs"]))
        self.assertContains(resp, 'class="feature-row"')
        self.assertContains(resp, "Needs: Workouts &amp; calendar")
        self.assertEqual(access_for(self.b).features, ["programs"])
        self.client_a.post(reverse("admin_user_feature_toggle", args=[self.b.pk, "programs"]))
        self.assertEqual(access_for(self.b).features, [])

    def test_budget_saves_as_decimal_and_blank_clears(self):
        self.client_a.post(reverse("admin_user_ai", args=[self.b.pk]), {"ai_enabled": "on", "monthly_ai_budget_usd": "5"})
        access = access_for(self.b)
        self.assertEqual((access.ai_enabled, access.monthly_ai_budget_usd), (True, Decimal("5.00")))
        self.client_a.post(reverse("admin_user_ai", args=[self.b.pk]), {"monthly_ai_budget_usd": ""})
        access = access_for(self.b)
        self.assertEqual((access.ai_enabled, access.monthly_ai_budget_usd), (False, None))
        resp = self.client_a.post(reverse("admin_user_ai", args=[self.b.pk]), {"monthly_ai_budget_usd": "lots"})
        self.assertContains(resp, "Couldn&#x27;t read")

    def test_cannot_deactivate_self_or_last_owner(self):
        self.client_a.post(reverse("admin_user_active", args=[self.a.pk]))
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_active)
        second_owner = make_user("erin", superuser=True)
        client = self.client_class()
        client.force_login(second_owner)
        client.post(reverse("admin_user_active", args=[self.a.pk]))   # allowed: erin is still an active owner
        self.a.refresh_from_db()
        self.assertFalse(self.a.is_active)
        client.post(reverse("admin_user_active", args=[self.b.pk]))
        self.b.refresh_from_db()
        self.assertFalse(self.b.is_active)

    def test_last_active_owner_rule(self):
        from workouts.admin_views import _can_deactivate
        self.assertFalse(_can_deactivate(self.b, self.a))    # alice is the only active owner
        self.assertTrue(_can_deactivate(self.a, self.b))

    def test_admin_pages_show_no_health_data(self):
        CachedWorkout.objects.create(user=self.b, workout_id="b1", title="BOB-SECRET-WORKOUT",
                                     created_at=datetime(2026, 9, 1, tzinfo=dt_tz.utc))
        FoodEntry.objects.create(user=self.b, date=date(2026, 9, 1), raw_text="BOB-SECRET-FOOD")
        Intervention.objects.create(user=self.b, name="BOB-SECRET-MED", category="medication",
                                    start_date=date(2026, 9, 1))
        AIUsage.objects.create(user=self.b, feature="ai_chat", model=llm.HAIKU, cost_usd=Decimal("0.25"))
        for url in (reverse("admin_users"), reverse("admin_user_detail", args=[self.b.pk])):
            html = self.client_a.get(url).content.decode()
            for secret in ("BOB-SECRET-WORKOUT", "BOB-SECRET-FOOD", "BOB-SECRET-MED"):
                self.assertNotIn(secret, html, url)
        self.assertIn("0.25", self.client_a.get(reverse("admin_user_detail", args=[self.b.pk])).content.decode())
