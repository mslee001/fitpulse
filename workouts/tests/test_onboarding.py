"""Get Started: the onboarding gate, derived step status, Peloton/Withings
connect flows, and background backfill jobs."""
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from workouts.access import access_for
from workouts.middleware import PUBLIC_PATHS
from workouts.models import (
    AthleteProfile, GoogleHealthAuth, Integration, NutritionProfile, PelotonAuth, SyncJob, WithingsAuth,
)
from workouts.onboarding import can_finish, steps_for
from workouts.services.peloton_client import PelotonAuthError
from workouts.tests.helpers import TwoUserTestCase, make_user

NOW = timezone.now()


class OnboardingTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        self.carol = make_user("carol", features=["nutrition", "ai_food_parse"], ai=True)
        access = access_for(self.carol)
        access.onboarding_completed_at = None
        access.save()
        Integration.ensure_for_user(self.carol)
        self.client_c = self.client_class()
        self.client_c.force_login(self.carol)
        # No real backfill threads: views import start_backfill both lazily and at module load.
        self.start_backfill = patch("workouts.background.start_backfill").start()
        patch("workouts.onboarding_views.start_backfill", self.start_backfill).start()
        self.addCleanup(patch.stopall)

    def step(self, key, user=None):
        return {s.key: s for s in steps_for(user or self.carol)}.get(key)

    def finish_profiles(self):
        NutritionProfile.objects.update_or_create(user=self.carol, defaults=dict(
            height_cm=165, age=40, biological_sex="female", activity_level="active", goal="loss"))
        AthleteProfile.objects.update_or_create(user=self.carol, defaults={"saved_at": NOW})

    # ── gate ──────────────────────────────────────────────────────────────────

    def test_unfinished_user_is_sent_to_get_started(self):
        for path in ("/", "/nutrition/", "/settings/"):
            self.assertRedirects(self.client_c.get(path), reverse("get_started"), fetch_redirect_response=False)
        resp = self.client_c.get("/nutrition/", HTTP_HX_REQUEST="true")
        self.assertEqual(resp["HX-Redirect"], reverse("get_started"))
        self.assertEqual(self.client_c.get(reverse("get_started")).status_code, 200)
        self.assertEqual(self.client_c.get(reverse("password_change")).status_code, 200)

    def test_superuser_is_never_gated(self):
        access = access_for(self.a)
        access.onboarding_completed_at = None
        access.save()
        self.assertEqual(self.client_a.get("/").status_code, 200)

    # ── steps ─────────────────────────────────────────────────────────────────

    def test_steps_follow_features(self):
        self.assertIsNotNone(self.step("nutrition_profile"))
        self.assertIsNotNone(self.step("athlete_profile"))      # has an AI feature
        self.assertIsNone(self.step("equipment"))               # no training/strength
        dave = make_user("dave", features=["training"])
        self.assertIsNone(self.step("nutrition_profile", dave))
        self.assertIsNone(self.step("athlete_profile", dave))
        self.assertEqual(self.step("equipment", dave).status, "todo")
        self.assertFalse(self.step("equipment", dave).required)

    def test_google_health_is_blocked_until_megan_adds_the_test_user(self):
        self.assertEqual(self.step("google_health").status, "blocked")
        access = access_for(self.carol)
        access.google_test_user_added = True
        access.save()
        self.assertEqual(self.step("google_health").status, "todo")
        resp = self.client_c.get(reverse("google_health_oauth_connect"))   # still guarded server-side when blocked?
        self.assertEqual(resp.status_code, 302)

    def test_can_finish_rules(self):
        self.finish_profiles()
        self.assertFalse(can_finish(self.carol))                 # nothing connected
        Integration.objects.filter(user=self.carol).update(is_enabled=False)
        self.assertFalse(can_finish(self.carol))                 # everything skipped
        Integration.objects.filter(user=self.carol, key="peloton").update(is_enabled=True)
        PelotonAuth.objects.create(user=self.carol, refresh_token="rt", peloton_user_id="p-carol")
        self.assertTrue(can_finish(self.carol))
        NutritionProfile.objects.filter(user=self.carol).update(age=None)
        self.assertFalse(can_finish(self.carol))                 # required profile incomplete

    def test_finish_only_when_ready_then_setup_guide_mode(self):
        self.client_c.post(reverse("gs_finish"))
        self.assertIsNone(access_for(self.carol).onboarding_completed_at)
        self.finish_profiles()
        PelotonAuth.objects.create(user=self.carol, refresh_token="rt", peloton_user_id="p-carol")
        for key in ("withings", "google_health"):
            self.client_c.post(reverse("gs_skip", args=[key]))
        resp = self.client_c.post(reverse("gs_finish"))
        self.assertRedirects(resp, reverse("today"), fetch_redirect_response=False)
        self.assertIsNotNone(access_for(self.carol).onboarding_completed_at)
        self.assertEqual(self.client_c.get("/nutrition/").status_code, 200)
        page = self.client_c.get(reverse("get_started")).content.decode()
        self.assertIn("Setup complete", page)
        self.assertNotIn("Finish setup", page)

    def test_nutrition_profile_step_saves_only_its_fields(self):
        self.client_c.post(reverse("gs_nutrition_profile"), {
            "height_cm": "170", "age": "33", "biological_sex": "male", "activity_level": "light", "goal": "gain"})
        p = NutritionProfile.objects.get(user=self.carol)
        self.assertEqual((p.height_cm, p.age, p.biological_sex, p.activity_level, p.goal),
                         (170.0, 33, "male", "light", "gain"))
        self.assertEqual(self.step("nutrition_profile").status, "done")

    def test_newly_granted_feature_shows_the_banner(self):
        access = access_for(self.carol)
        access.onboarding_completed_at = NOW
        access.features = ["training"]
        access.save()
        self.assertNotIn("Finish setting up", self.client_c.get("/").content.decode())
        access.features = ["training", "nutrition"]
        access.save()
        self.assertIn("Finish setting up Nutrition profile", self.client_c.get("/").content.decode())

    # ── Peloton connect ───────────────────────────────────────────────────────

    def patch_exchange(self, **kwargs):
        tokens = {"access_token": "A1", "refresh_token": "R2", "expires_at": NOW + timedelta(hours=48)}
        p = patch("workouts.services.peloton_client.PelotonClient.exchange_refresh_token",
                  return_value=tokens, **kwargs)
        return p.start()

    @patch("workouts.services.peloton_client.PelotonClient.fetch_me")
    def test_peloton_token_connect(self, fetch_me):
        self.patch_exchange()
        fetch_me.return_value = {"id": "p-carol", "username": "carolrides"}
        resp = self.client_c.post(reverse("set_peloton_auth"), {"refresh_token": "R1", "next": "/get-started/#peloton"})
        self.assertRedirects(resp, "/get-started/#peloton", fetch_redirect_response=False)
        auth = PelotonAuth.objects.get(user=self.carol)
        self.assertEqual((auth.peloton_user_id, auth.peloton_username), ("p-carol", "carolrides"))
        self.start_backfill.assert_called_once_with(self.carol, "peloton")
        self.assertEqual(self.step("peloton").status, "done")

    def test_peloton_row_without_token_is_not_done(self):
        PelotonAuth.objects.create(user=self.carol, peloton_user_id="p-carol")   # pre-token row
        self.assertNotEqual(self.step("peloton").status, "done")

    def test_peloton_bad_token(self):
        self.patch_exchange(side_effect=PelotonAuthError("That Peloton sign-in token expired or was already used."))
        resp = self.client_c.post(reverse("set_peloton_auth"), {"refresh_token": "stale"}, follow=False)
        self.assertFalse(PelotonAuth.objects.filter(user=self.carol).exists())
        self.start_backfill.assert_not_called()
        from django.contrib.messages import get_messages
        self.assertIn("expired or was already used", " ".join(str(m) for m in get_messages(resp.wsgi_request)))

    @patch("workouts.services.peloton_client.PelotonClient.fetch_me",
           return_value={"id": "p-bob", "username": "bob"})
    def test_peloton_account_already_used_by_someone_else(self, fetch_me):
        from django.contrib.messages import get_messages
        self.patch_exchange()
        PelotonAuth.objects.create(user=self.b, refresh_token="b", peloton_user_id="p-bob")
        resp = self.client_c.post(reverse("set_peloton_auth"), {"refresh_token": "R1"})
        self.assertFalse(PelotonAuth.objects.filter(user=self.carol).exists())
        msg = " ".join(str(m) for m in get_messages(resp.wsgi_request))
        self.assertIn("That sign-in is for Peloton account @bob", msg)   # names whose account the token is
        self.assertIn("Sign in to Peloton as yourself", msg)

    @patch("workouts.services.peloton_client.PelotonClient.fetch_me",
           return_value={"id": "p-carol", "username": "carol"})
    def test_database_error_is_not_reported_as_a_duplicate_account(self, fetch_me):
        from django.contrib.messages import get_messages
        from django.db import IntegrityError
        self.patch_exchange()
        with patch("workouts.models.PelotonAuth.objects.update_or_create", side_effect=IntegrityError("dup pk")):
            resp = self.client_c.post(reverse("set_peloton_auth"), {"refresh_token": "R1"})
        msg = " ".join(str(m) for m in get_messages(resp.wsgi_request))
        self.assertIn("database error", msg)
        self.assertIn("copy a fresh one", msg)       # the pasted token is spent
        self.assertNotIn("already connected", msg)

    def test_open_redirects_are_refused(self):
        self.patch_exchange()
        with patch("workouts.services.peloton_client.PelotonClient.fetch_me",
                   return_value={"id": "p-carol", "username": "c"}):
            resp = self.client_c.post(reverse("set_peloton_auth"),
                                      {"refresh_token": "x", "next": "https://evil.example/"})
        self.assertEqual(resp["Location"], reverse("integrations_settings"))

    # ── Withings connect ──────────────────────────────────────────────────────

    def start_withings(self):
        resp = self.client_c.post(reverse("withings_oauth_connect"), {"next": "/get-started/#withings"})
        self.assertEqual(resp.status_code, 302)
        return self.client_c.session["withings_oauth_state"]

    def test_withings_bad_state(self):
        self.start_withings()
        resp = self.client_c.get(reverse("withings_oauth_callback"), {"state": "wrong", "code": "c"})
        self.assertRedirects(resp, "/get-started/#withings", fetch_redirect_response=False)
        self.assertFalse(WithingsAuth.objects.filter(user=self.carol).exists())

    def _token(self, userid):
        def request_tokens(client, code, redirect_uri=None):
            client._tokens = {"access_token": "a", "refresh_token": "r",
                              "expires_at": int(NOW.timestamp()) + 3600, "userid": userid}
            return client._tokens
        return request_tokens

    def test_withings_duplicate_userid_is_refused(self):
        WithingsAuth.objects.create(user=self.b, userid="999", access_token="a", refresh_token="r",
                                    token_expires_at=NOW)
        state = self.start_withings()
        with patch("workouts.services.withings_client.WithingsClient.request_tokens", self._token("999")):
            self.client_c.get(reverse("withings_oauth_callback"), {"state": state, "code": "c"})
        self.assertFalse(WithingsAuth.objects.filter(user=self.carol).exists())

    def _inline_threads(self):
        """Run background-thread targets inline (and leave the test's DB connection open)."""
        fake = lambda target, args, daemon: type("T", (), {"start": lambda _s: target(*args)})()
        return (patch("workouts.background.threading.Thread", side_effect=fake),
                patch("workouts.background.close_old_connections"))

    def test_withings_success(self):
        state = self.start_withings()
        threads, conns = self._inline_threads()
        with patch("workouts.services.withings_client.WithingsClient.request_tokens", self._token("777")), \
                patch("workouts.services.withings_client.WithingsClient.subscribe_webhook") as subscribe, \
                patch("workouts.onboarding_views.settings.WITHINGS_CALLBACK_URL", "https://x/api/withings/webhook/", create=True), \
                threads, conns:
            subscribe.side_effect = lambda url, appli=1: WithingsAuth.objects.filter(user=self.carol).update(
                webhook_subscription_active=True)
            resp = self.client_c.get(reverse("withings_oauth_callback"), {"state": state, "code": "c"})
        self.assertRedirects(resp, "/get-started/#withings", fetch_redirect_response=False)
        subscribe.assert_called_once_with("https://x/api/withings/webhook/")
        self.assertEqual(WithingsAuth.objects.get(user=self.carol).userid, "777")
        self.assertTrue(Integration.objects.get(user=self.carol, key="withings").is_authenticated)
        self.assertEqual(self.step("withings").status, "done")

    def test_withings_subscribe_runs_outside_the_callback_request(self):
        # Withings HEAD-checks the callback URL before answering the subscribe; doing it
        # inside this request deadlocks a single-worker server (production 293s).
        state = self.start_withings()
        with patch("workouts.services.withings_client.WithingsClient.request_tokens", self._token("778")), \
                patch("workouts.services.withings_client.WithingsClient.subscribe_webhook") as subscribe, \
                patch("workouts.onboarding_views.settings.WITHINGS_CALLBACK_URL", "https://x/api/withings/webhook/", create=True), \
                patch("workouts.background.threading.Thread") as thread:
            self.client_c.get(reverse("withings_oauth_callback"), {"state": state, "code": "c"})
        subscribe.assert_not_called()                 # not inline
        from workouts.background import _subscribe_withings
        self.assertIs(thread.call_args.kwargs["target"], _subscribe_withings)
        thread.return_value.start.assert_called_once()

    def test_withings_subscribe_failure_is_recorded(self):
        from workouts.background import _subscribe_withings
        from workouts.models import WebhookError
        WithingsAuth.objects.create(user=self.carol, userid="779", access_token="a", refresh_token="r",
                                    token_expires_at=NOW + timedelta(hours=1))
        with patch("workouts.services.withings_client.WithingsClient.subscribe_webhook",
                   side_effect=RuntimeError("Withings subscribe failed: 293")), \
                patch("workouts.background.close_old_connections"):
            _subscribe_withings(self.carol.pk, "https://x/api/withings/webhook/")
        err = WebhookError.objects.get(source="withings_subscribe")
        self.assertEqual(err.user, self.carol)
        self.assertIn("293", err.summary)
        self.assertEqual(self.step("withings").status, "todo")

    def test_withings_callback_is_not_public(self):
        self.assertNotIn("/auth/withings/callback/", PUBLIC_PATHS)
        resp = self.client_class().get(reverse("withings_oauth_callback"))
        self.assertIn(reverse("login"), resp["Location"])


class BackfillJobTests(TwoUserTestCase):
    def test_second_start_returns_none_while_running(self):
        from workouts.background import start_backfill
        with patch("workouts.background.threading.Thread") as thread:
            first = start_backfill(self.b, "peloton")
            self.assertIsNotNone(first)
            self.assertIsNone(start_backfill(self.b, "peloton"))
            self.assertEqual(thread.call_count, 1)

    def test_stale_job_reads_as_failed_and_retry_starts_a_new_one(self):
        job = SyncJob.objects.create(user=self.b, source="withings")
        SyncJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(hours=3))
        with patch("workouts.background.threading.Thread"):
            resp = self.client_b.post(reverse("gs_retry", args=["withings"]))
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("Interrupted", job.error)
        self.assertEqual(SyncJob.objects.filter(user=self.b, source="withings").count(), 2)
        self.assertContains(resp, 'hx-trigger="every 5s"')

    def test_status_partial_polls_only_while_running(self):
        job = SyncJob.objects.create(user=self.b, source="peloton")
        self.assertContains(self.client_b.get(reverse("gs_sync_status", args=["peloton"])), 'hx-trigger="every 5s"')
        SyncJob.objects.filter(pk=job.pk).update(status="done", summary={"created": 12}, finished_at=timezone.now())
        resp = self.client_b.get(reverse("gs_sync_status", args=["peloton"]))
        self.assertNotContains(resp, "hx-trigger")
        self.assertContains(resp, "12 workouts")

    def test_job_body_records_success_and_failure(self):
        from workouts.background import _run_job
        ok = SyncJob.objects.create(user=self.b, source="peloton")
        with patch.dict("workouts.background.ALL_SYNC", {"peloton": lambda u: {"done": True, "created": 3}}), \
                patch("workouts.background.close_old_connections"):
            _run_job(ok.pk)
        ok.refresh_from_db()
        self.assertEqual((ok.status, ok.summary["created"]), ("done", 3))
        bad = SyncJob.objects.create(user=self.b, source="withings")
        with patch.dict("workouts.background.ALL_SYNC", {"withings": lambda u: {"error": "token revoked"}}), \
                patch("workouts.background.close_old_connections"):
            _run_job(bad.pk)
        bad.refresh_from_db()
        self.assertEqual((bad.status, bad.error), ("failed", "token revoked"))
        from workouts.models import WebhookError
        self.assertTrue(WebhookError.objects.filter(source="backfill_withings", user=self.b).exists())
