"""The read-only demo: sign-in, nothing saved, no live AI, and the seeded programs."""
import datetime
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from workouts import llm
from workouts.demo import allow_ai_generation, is_demo
from workouts.management.commands.seed_demo import seed
from workouts.models import (
    CachedWorkout, DailyStats, FoodEntry, Program, ProgramWorkout, SavedAnalysis, WeeklyReview,
)
from workouts.tests.helpers import make_user

PLAN = {
    "name": "5K in 8 Weeks", "ai_model": "claude-sonnet-5", "generated_at": "2026-10-09T12:00:00",
    "inputs": {"goal": "5k", "weeks": 2, "race_week": 2, "race_weekday": 6, "mode": "alongside",
               "target_time": 1770, "level": "intermediate", "pace": {"level": 5, "goal_pace_s": 570}},
    "spec": {"summary": "Two weeks to race day.", "pace_guidance": "Stay at Level 5.", "weeks": [
        {"number": 1, "phase": "base", "focus": "Easy miles", "slots": [
            {"day": 2, "order": 0, "discipline": "running", "class_type": "Endurance", "duration_min": 20,
             "optional": False, "purpose": "easy aerobic minutes", "pace_zone": "Easy"},
            {"day": 4, "order": 0, "discipline": "running", "class_type": "Intervals", "duration_min": 30,
             "optional": False, "purpose": "speed", "pace_zone": "Hard"}]},
        {"number": 2, "phase": "race", "focus": "Sharpen", "slots": [
            {"day": 2, "order": 0, "discipline": "running", "class_type": "Endurance", "duration_min": 20,
             "optional": False, "purpose": "shakeout", "pace_zone": "Easy"}]},
    ]},
    "picks": {"1-2-0": {"ride_id": "ride-easy"}, "1-4-0": {"ride_id": "ride-hard"}, "2-2-0": {"ride_id": "ride-easy2"}},
    "classes": {
        "ride-easy": {"ride_id": "ride-easy", "title": "20 min Endurance Run", "instructor_name": "Becs Gentry",
                      "duration_seconds": 1200, "discipline": "running", "class_type_id": "t1",
                      "difficulty_estimate": 5.1, "original_air_time": "2025-02-14T05:00:00+00:00"},
        "ride-hard": {"ride_id": "ride-hard", "title": "30 min Intervals Run", "instructor_name": "Matt Wilpers",
                      "duration_seconds": 1800, "discipline": "running", "class_type_id": "t2",
                      "difficulty_estimate": 7.4, "original_air_time": "2025-03-01T05:00:00+00:00"},
        "ride-easy2": {"ride_id": "ride-easy2", "title": "20 min Endurance Run", "instructor_name": "Jess Sims",
                       "duration_seconds": 1200, "discipline": "running", "class_type_id": "t1",
                       "difficulty_estimate": 4.9, "original_air_time": "2025-04-01T05:00:00+00:00"},
    },
}
EXAMPLES = {
    "insights": "## Consistency\nSaved insight.", "pattern_insights": "## Highest-confidence pattern\nSaved pattern.",
    "body_commentary": "HEADLINE: Saved body note", "nutrition_insights": {"range": 30, "text": "## What's working\nSaved."},
    "weekly_reviews": [{"weeks_ago": 1, "content": "## Training\nLast week."},
                       {"weeks_ago": 2, "content": "## Training\nTwo weeks ago."}],
    "next_workout": {str(d): f"INTENSITY: Easy\nACTIVITY: Walk\nREASON: Saved for weekday {d}." for d in range(7)},
    "intervention_analysis": {"text": "Saved interpretation."}, "training_plan": PLAN,
}


@override_settings(DEMO_ENABLED=True, DEMO_USERNAME="demo")
class DemoTestCase(TestCase):
    def setUp(self):
        self.demo = get_user_model().objects.create_user("demo")
        today = timezone.localdate()
        yesterday_offset = (today - datetime.timedelta(days=1) - (today - datetime.timedelta(days=today.weekday()))).days
        self.examples = dict(EXAMPLES, day_analysis={str(yesterday_offset): "HEADLINE: Yesterday, saved"})
        seed(self.demo, self.examples, log=lambda *_: None)
        self.client.force_login(self.demo)


class SeedTests(DemoTestCase):
    def test_programs_and_saved_ai_are_seeded(self):
        split = Program.objects.for_user(self.demo).get(kind="split")
        plan = Program.objects.for_user(self.demo).get(kind="plan")
        self.assertGreater(ProgramWorkout.objects.filter(run_week__run__program=split).count(), 10)
        self.assertEqual(plan.goal_json["companion_program_id"], split.pk)
        self.assertEqual(plan.weeks.count(), 2)
        self.assertTrue(plan.weeks.get(number=2).slots.filter(title__startswith="Race day").exists())
        self.assertEqual(DailyStats.objects.for_user(self.demo).exclude(ai_day_analysis=None).get().ai_day_analysis,
                         "HEADLINE: Yesterday, saved")
        today = DailyStats.objects.for_user(self.demo).get(date=timezone.localdate())
        self.assertIn(f"weekday {timezone.localdate().weekday()}", today.ai_next_workout)
        self.assertEqual(WeeklyReview.objects.for_user(self.demo).count(), 2)
        self.assertEqual(SavedAnalysis.objects.for_user(self.demo).get().ai_interpretation, "Saved interpretation.")
        self.assertFalse(self.demo.access.features.count("ai_chat"))

    def test_reseeding_tells_the_same_story(self):
        first = sorted(CachedWorkout.objects.for_user(self.demo).values_list("workout_id", "calories"))
        seed(self.demo, self.examples, log=lambda *_: None)
        self.assertEqual(sorted(CachedWorkout.objects.for_user(self.demo).values_list("workout_id", "calories")), first)

    def test_a_later_weekday_only_reveals_more_of_the_same_week(self):
        monday = timezone.localdate() - datetime.timedelta(days=timezone.localdate().weekday())

        def snapshot(as_of):
            seed(self.demo, self.examples, log=lambda *_: None, as_of=as_of)
            return {
                "workouts": sorted((w.workout_id, w.calories) for w in CachedWorkout.objects.for_user(self.demo)
                                   if timezone.localtime(w.created_at).date() < monday + datetime.timedelta(days=1)),
                "stats": sorted(DailyStats.objects.for_user(self.demo).filter(date__lte=monday)
                                .values_list("date", "resting_hr", "weight_lb")),
                "food": sorted(FoodEntry.objects.for_user(self.demo).filter(date__lt=monday)
                               .values_list("date", "meal", "calories")),
            }
        on_tuesday = snapshot(monday + datetime.timedelta(days=1))
        on_friday = snapshot(monday + datetime.timedelta(days=4))
        self.assertEqual(on_tuesday, on_friday)

    def test_pages_render_with_the_banner(self):
        plan_run = Program.objects.for_user(self.demo).get(kind="plan").active_run
        for url in (reverse("today"), reverse("history"), reverse("program_run", args=[plan_run.pk]),
                    reverse("insights"), reverse("weekly_review"), reverse("body")):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertContains(resp, "you're exploring sample data", msg_prefix=url)


class ReadOnlyTests(DemoTestCase):
    def test_htmx_change_is_refused_with_a_toast_event(self):
        before = FoodEntry.objects.for_user(self.demo).count()
        resp = self.client.post(reverse("nutrition_log"), {"raw_text": "x"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.assertIn("fp-demo-readonly", json.loads(resp["HX-Trigger"]))
        self.assertEqual(FoodEntry.objects.for_user(self.demo).count(), before)

    def test_json_change_is_refused(self):
        resp = self.client.post(reverse("save_analysis"), "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()["demo"])

    def test_full_page_change_goes_back_with_a_message(self):
        from workouts.models import Intervention
        iv = Intervention.objects.for_user(self.demo).first()
        url = reverse("intervention_edit", args=[iv.pk])
        resp = self.client.post(url, {"name": "Renamed"}, HTTP_REFERER=f"http://testserver{url}")
        self.assertRedirects(resp, f"http://testserver{url}", fetch_redirect_response=False)
        iv.refresh_from_db()
        self.assertNotEqual(iv.name, "Renamed")

    def test_syncs_are_refused_even_as_page_views(self):
        resp = self.client.get(reverse("sync_new_workouts"), HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_whatever_a_page_view_writes_is_rolled_back(self):
        from workouts.middleware import LoginRequiredMiddleware

        def view_that_writes(request):
            CachedWorkout.objects.create(user=self.demo, workout_id="leak", created_at="2026-01-01T00:00:00Z")
            return HttpResponse("ok")
        request = RequestFactory().get(reverse("today"))
        request.user = self.demo
        LoginRequiredMiddleware(view_that_writes)(request)
        self.assertFalse(CachedWorkout.objects.filter(workout_id="leak").exists())

    def test_sign_out_works(self):
        resp = self.client.post(reverse("logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class AITests(DemoTestCase):
    def test_guard_refuses_the_demo_user(self):
        with self.assertRaises(llm.AIDemoOff):
            llm.guard(self.demo, "ai_weekly_review")
        from workouts.ai import ai_unavailable_reason
        self.assertIn("turned off in the demo", ai_unavailable_reason(llm.AIDemoOff("x")))

    def test_generation_switch_lets_the_generator_through(self):
        with allow_ai_generation():
            llm.guard(self.demo, "ai_weekly_review")   # no exception

    def test_other_users_are_unaffected(self):
        owner = make_user("owner", superuser=True)
        self.assertFalse(is_demo(owner))
        llm.guard(owner, "ai_weekly_review")


@override_settings(DEMO_ENABLED=True, DEMO_USERNAME="demo")
class DemoSignInTests(TestCase):
    def test_button_signs_in_as_the_demo_user(self):
        demo = get_user_model().objects.create_user("demo")
        self.assertContains(self.client.get(reverse("login")), "Explore the demo")
        resp = self.client.post(reverse("demo_login"))
        self.assertRedirects(resp, reverse("today"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), demo.pk)

    def test_a_link_alone_does_not_sign_in(self):
        get_user_model().objects.create_user("demo")
        self.assertRedirects(self.client.get(reverse("demo_login")), reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_demo_user_no_button(self):
        self.assertNotContains(self.client.get(reverse("login")), "Explore the demo")
        resp = self.client.post(reverse("demo_login"))
        self.assertRedirects(resp, reverse("login"), fetch_redirect_response=False)

    @override_settings(DEMO_ENABLED=False)
    def test_turned_off(self):
        get_user_model().objects.create_user("demo")
        self.assertNotContains(self.client.get(reverse("login")), "Explore the demo")


@override_settings(DEMO_ENABLED=True, DEMO_USERNAME="demo")
class SyncDailyReseedTests(TestCase):
    def test_full_daily_sync_reseeds_the_demo(self):
        from workouts.management.commands.sync_daily import Command
        get_user_model().objects.create_user("demo")
        with patch("django.core.management.call_command") as call:
            Command()._reseed_demo()
        self.assertEqual(call.call_args.kwargs["user"], "demo")
