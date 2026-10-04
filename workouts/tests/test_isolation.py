"""Two-user isolation: nothing of one user's is reachable by another — not via
an id in a URL, a list page, a posted id, or a module helper."""
import json
from datetime import date, datetime, timedelta, timezone as dt_tz

from django.urls import reverse

from workouts.analysis import run_intervention_analysis
from workouts.models import (
    BodyMeasurement, CachedWorkout, DailyStats, DoseChange, FoodEntry, HungerCheck, Intervention,
    Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek, SavedAnalysis, SavedMeal,
    PlanDraft, SideEffectLog, WeeklyReview,
)
from workouts.nutrition import compute_streaks, get_top_foods
from workouts.programs import associate_workout, identify_membership
from workouts.strength import exercise_history
from workouts.tests.helpers import TwoUserTestCase

RIDE = "f" * 32
DAY = date.today() - timedelta(days=1)
WHEN = datetime.combine(DAY, datetime.min.time(), tzinfo=dt_tz.utc) + timedelta(hours=17)


class IsolationTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        # Bob can use everything, so every 404 below is about ownership, not access.
        from workouts.access import FEATURES, access_for
        access = access_for(self.b)
        access.features, access.ai_enabled = list(FEATURES), True
        access.save()
        a = self.a
        self.workout = CachedWorkout.objects.create(
            user=a, workout_id="alice-w1", ride_id=RIDE, title="ALICE-ONLY-WORKOUT", discipline="strength",
            source="peloton", created_at=WHEN, duration_seconds=1800,
            manual_movements_json=[{"name": "ALICE-ONLY-EXERCISE", "sets": 3, "reps": 8, "weight_lb": 20, "notes": ""}],
        )
        DailyStats.objects.create(user=a, date=DAY, weight_lb=150.0, cal_total=1800, protein_g_total=120)
        BodyMeasurement.objects.create(user=a, measured_at=WHEN, date=DAY, weight_lb=150.0, withings_grpid="g1")
        self.iv = Intervention.objects.create(user=a, name="ALICE-ONLY-IV", category="supplement",
                                              start_date=DAY - timedelta(days=30))
        DoseChange.objects.create(intervention=self.iv, dose="5mg", start_date=DAY - timedelta(days=30))
        self.analysis = SavedAnalysis.objects.create(
            user=a, label="ALICE-ONLY-ANALYSIS", intervention=self.iv, before_start=DAY, before_end=DAY,
            after_start=DAY, after_end=DAY, window_days=28, metrics_json={}, ai_interpretation="x")
        self.food = FoodEntry.objects.create(
            user=a, date=DAY, meal="lunch", raw_text="ALICE-ONLY-FOOD", calories=500, protein_g=40,
            items_json=[{"name": "alice-only-item", "calories": 500, "protein_g": 40}])
        self.meal = SavedMeal.objects.create(user=a, name="ALICE-ONLY-MEAL", calories=500)
        HungerCheck.objects.create(user=a, date=DAY, context="morning", hunger_level=4, notes="ALICE-ONLY-HUNGER")
        SideEffectLog.objects.create(user=a, date=DAY, symptom="other", other_label="ALICE-ONLY-SYMPTOM", severity=1)
        WeeklyReview.objects.create(user=a, week_start=DAY - timedelta(days=DAY.weekday() + 7),
                                    content="ALICE-ONLY-REVIEW")
        self.program = Program.objects.create(user=a, name="ALICE-ONLY-PROGRAM", slug="alice-split",
                                              kind="split", match_strategy="ride_ids")
        week = ProgramWeek.objects.create(program=self.program, number=1)
        self.slot = ProgramSlot.objects.create(week=week, title="Pull", peloton_ride_id=RIDE,
                                               spec_json={"discipline": "running", "class_type_id": "t"})
        from workouts.models import PlanDraft
        self.draft = PlanDraft.objects.create(user=a, status="ready", inputs_json={}, spec_json={"weeks": []})
        self.run = ProgramRun.objects.create(program=self.program, start_date=DAY - timedelta(days=7))
        self.run_week = RunWeek.objects.create(run=self.run, program_week=week, sequence=1)
        ProgramWorkout.objects.create(run_week=self.run_week, workout=self.workout)

    def counts(self):
        return {M.__name__: M.objects.count() for M in (
            CachedWorkout, Intervention, DoseChange, SavedAnalysis, FoodEntry, SavedMeal,
            Program, ProgramRun, RunWeek, ProgramWorkout, PlanDraft)}

    # ── ids in URLs ───────────────────────────────────────────────────────────

    def test_id_in_url_routes_404_for_another_user(self):
        wid, iv, run, rw = self.workout.workout_id, self.iv.pk, self.run.pk, self.run_week.pk
        gets = [
            ("workout_detail", [wid]), ("intervention_detail", [iv]), ("intervention_edit", [iv]),
            ("saved_analysis_detail", [self.analysis.pk]), ("nutrition_entry_row", [self.food.pk]),
            ("nutrition_edit", [self.food.pk]), ("program_detail", ["alice-split"]),
            ("program_edit", ["alice-split"]), ("program_run", [run]), ("program_progression", [run]),
            ("program_running_progression", [run]),
            ("program_training_plan_draft", [self.draft.pk]), ("program_training_plan_status", [self.draft.pk]),
        ]
        posts = [
            ("save_manual_movements", [wid]), ("intervention_end", [iv]), ("intervention_delete", [iv]),
            ("intervention_quick_dose", [iv]), ("intervention_edit", [iv]), ("intervention_detail", [iv]),
            ("saved_analysis_delete", [self.analysis.pk]),
            ("nutrition_delete", [self.food.pk]), ("nutrition_edit", [self.food.pk]),
            ("nutrition_save_meal", [self.food.pk]), ("nutrition_relog", [self.meal.pk]),
            ("nutrition_delete_meal", [self.meal.pk]),
            ("program_delete", ["alice-split"]), ("program_edit", ["alice-split"]),
            ("program_duplicate", ["alice-split"]), ("program_start_cycle", ["alice-split"]),
            ("program_backfill", ["alice-split"]), ("program_complete_run", [run]),
            ("program_retrospective", [run]), ("program_delete_run", [run]),
            ("program_delete_week", [rw]), ("run_week_rate", [rw]),
            ("program_training_plan_retry", [self.draft.pk]), ("program_training_plan_swap", [self.draft.pk]),
            ("program_training_plan_pick", [self.draft.pk]), ("program_training_plan_create", [self.draft.pk]),
            ("program_training_plan_discard", [self.draft.pk]), ("program_slot_swap", [self.slot.pk]),
        ]
        before = self.counts()
        for name, args in gets:
            resp = self.client_b.get(reverse(name, args=args))
            self.assertEqual(resp.status_code, 404, f"GET {name}")
        for name, args in posts:
            resp = self.client_b.post(reverse(name, args=args), {"name": "x", "dose": "9mg", "action": "add_dose",
                                                                 "start_date": DAY.isoformat()})
            self.assertEqual(resp.status_code, 404, f"POST {name}")
        self.assertEqual(self.counts(), before)
        self.workout.refresh_from_db()
        self.assertEqual(self.workout.manual_movements_json[0]["name"], "ALICE-ONLY-EXERCISE")

    def test_owner_only_routes_are_forbidden(self):
        self.assertEqual(self.client_b.get(reverse("garmin_activity_history", args=["running"])).status_code, 403)
        self.assertEqual(self.client_b.post(reverse("integration_toggle", args=["garmin"])).status_code, 403)

    # ── list pages ────────────────────────────────────────────────────────────

    LIST_PAGES = [
        ("history", []), ("interventions", []), ("nutrition", []), ("symptoms", []), ("program_list", []),
        ("body", []), ("calendar", []), ("strength_trends", []), ("today", []), ("dashboard", []),
        ("analytics", []), ("intervention_analysis", []), ("nutrition_analytics", []), ("weekly_review", []),
    ]

    def _page_text(self, client, name, args=(), query=None):
        resp = client.get(reverse(name, args=args), query or {})
        self.assertEqual(resp.status_code, 200, name)
        return resp.content.decode()

    def test_list_pages_show_nothing_of_another_users(self):
        pages = self.LIST_PAGES + [("day_view", [DAY.isoformat()]), ("class_history", [RIDE])]
        for name, args in pages:
            html = self._page_text(self.client_b, name, args, {"date": DAY.isoformat()})
            self.assertNotIn("ALICE-ONLY", html, name)

    def test_compare_ignores_another_users_ids(self):
        html = self._page_text(self.client_b, "compare", query={"ids": self.workout.workout_id})
        self.assertNotIn("ALICE-ONLY", html)

    def test_owner_still_sees_their_own_data(self):
        self.assertIn("ALICE-ONLY-WORKOUT", self._page_text(self.client_a, "history"))
        self.assertIn("ALICE-ONLY-IV", self._page_text(self.client_a, "interventions"))
        self.assertIn("ALICE-ONLY-FOOD", self._page_text(self.client_a, "nutrition", query={"date": DAY.isoformat()}))
        self.assertIn("ALICE-ONLY-PROGRAM", self._page_text(self.client_a, "program_list"))
        self.assertIn("ALICE-ONLY-WORKOUT",
                      self._page_text(self.client_a, "workout_detail", [self.workout.workout_id]))

    # ── ids in POST bodies ────────────────────────────────────────────────────

    def test_posted_ids_from_another_user_are_rejected(self):
        resp = self.client_b.post(reverse("hunger_log"), {"hunger_level": 5, "related_meal_id": self.food.pk})
        self.assertEqual(resp.status_code, 400)
        resp = self.client_b.post(reverse("symptoms"), {"symptom": "nausea", "severity": 1,
                                                        "related_intervention_id": self.iv.pk})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(HungerCheck.objects.filter(user=self.b).exists())
        self.assertFalse(SideEffectLog.objects.filter(user=self.b).exists())
        # save_analysis: someone else's intervention id is ignored, not attached
        resp = self.client_b.post(reverse("save_analysis"), json.dumps({"label": "b", "intervention_id": self.iv.pk}),
                                  content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(SavedAnalysis.objects.get(user=self.b).intervention)

    def test_writes_land_on_the_requesting_user(self):
        self.client_b.post(reverse("nutrition_save_suggestion"), {"name": "BOB-MEAL", "calories": "300"})
        self.client_b.post(reverse("set_ftp"), {"ftp": "180"})
        self.assertEqual(SavedMeal.objects.get(name="BOB-MEAL").user, self.b)
        from workouts.models import UserSettings
        self.assertEqual(UserSettings.for_user(self.b).ftp, 180)
        self.assertIsNone(UserSettings.for_user(self.a).ftp)

    # ── module helpers ────────────────────────────────────────────────────────

    def test_module_functions_return_nothing_for_another_user(self):
        self.assertEqual(exercise_history(self.b), {})
        self.assertTrue(exercise_history(self.a))
        self.assertEqual(get_top_foods(self.b, DAY - timedelta(days=7), DAY), [])
        self.assertTrue(get_top_foods(self.a, DAY - timedelta(days=7), DAY))
        self.assertEqual(compute_streaks(self.b, reference_date=DAY)["logging_days"], 0)
        self.assertEqual(compute_streaks(self.a, reference_date=DAY)["logging_days"], 1)
        analysis = run_intervention_analysis(self.b, DAY - timedelta(days=3), DAY - timedelta(days=1),
                                             DAY, DAY)
        self.assertEqual((analysis["before_n"], analysis["after_n"]), (0, 0))

    def test_another_users_workout_never_lands_on_my_program(self):
        bobs = CachedWorkout.objects.create(
            user=self.b, workout_id="bob-w1", ride_id=RIDE, title="Same class, Bob", discipline="strength",
            source="peloton", created_at=WHEN, duration_seconds=1800)
        self.assertIsNone(identify_membership(bobs))
        self.assertIsNone(associate_workout(bobs))
        self.assertEqual(ProgramWorkout.objects.filter(workout=bobs).count(), 0)
