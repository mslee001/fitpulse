from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from workouts.models import CachedWorkout, UserSettings
from workouts.programs import _workout_exercise_loads
from workouts.strength import (
    dumbbells, exercise_history, next_dumbbell, prev_dumbbell, recommend, recommendations,
)

BASE = datetime(2026, 9, 1, 17, 0, tzinfo=dt_tz.utc)
_n = 0


def session(weight, effort="", timed=False):
    return {"weight_lb": weight, "effort": effort, "timed": timed}


def logged(day, *rows, discipline="circuit"):
    global _n
    _n += 1
    return CachedWorkout.objects.create(
        workout_id=f"s{_n}", title="45 min Upper Body Pull + Run", discipline=discipline,
        created_at=BASE + timedelta(days=day), duration_seconds=2700, manual_movements_json=list(rows),
    )


def row(name, weight, effort="", sets=3, reps=8, notes=""):
    r = {"name": name, "sets": sets, "reps": reps, "weight_lb": weight, "notes": notes}
    if effort:
        r["effort"] = effort
    return r


class DumbbellStepTests(TestCase):
    def test_steps_through_the_rack(self):
        rack = dumbbells()   # default rack
        self.assertEqual(next_dumbbell(5, rack), 7)
        self.assertEqual(next_dumbbell(12, rack), 15)
        self.assertEqual(next_dumbbell(22, rack), 25)    # off-rack weight rounds to the next real one
        self.assertIsNone(next_dumbbell(45, rack))
        self.assertEqual(prev_dumbbell(10, rack), 7)
        self.assertIsNone(prev_dumbbell(2, rack))

    def test_recommendations_follow_the_rack_in_settings(self):
        UserSettings.objects.update_or_create(pk=1, defaults={"dumbbells_lb": [10, 12.5, 15, 50]})
        self.assertEqual(recommend([session(12.5, "easy")])["weight"], 15)
        self.assertEqual(recommend([session(15, "easy")])["weight"], 50)


class SetDumbbellsTests(TestCase):
    def post(self, value):
        from workouts.views import set_dumbbells
        req = RequestFactory().post("/settings/dumbbells/", {"dumbbells": value})
        req.session, req._messages = {}, _NoMessages()
        set_dumbbells(req)
        return UserSettings.get().dumbbells_lb

    def test_parses_sorts_and_dedupes(self):
        self.assertEqual(self.post("15, 5 10lb 12.5, 5"), [5, 10, 12.5, 15])

    def test_bad_input_leaves_the_rack_alone(self):
        before = self.post("5, 10")
        self.assertEqual(self.post("5, ten"), before)
        self.assertEqual(self.post(""), before)
        self.assertEqual(self.post("-5"), before)


class RecommendTests(TestCase):
    def test_easy_moves_up_one_dumbbell(self):
        self.assertEqual(recommend([session(12, "easy")]), {
            "action": "up", "weight": 15, "current": 12, "reason": "Felt easy"})

    def test_just_right_needs_two_in_a_row_at_the_same_weight(self):
        self.assertEqual(recommend([session(30, "right")])["action"], "hold")
        self.assertEqual(recommend([session(30, "right"), session(30, "right")])["weight"], 35)
        # a lighter session before doesn't count toward the streak
        self.assertEqual(recommend([session(25, "right"), session(30, "right")])["action"], "hold")

    def test_hard_holds_and_fail_drops(self):
        self.assertEqual(recommend([session(30, "hard")])["weight"], 30)
        rec = recommend([session(30, "fail")])
        self.assertEqual((rec["action"], rec["weight"]), ("down", 25))

    def test_unrated_moves_up_after_two_sessions_unless_one_was_hard(self):
        self.assertEqual(recommend([session(20)])["action"], "hold")
        self.assertEqual(recommend([session(20), session(20)])["weight"], 25)
        self.assertEqual(recommend([session(20, "hard"), session(20)])["action"], "hold")

    def test_heaviest_dumbbell_is_maxed(self):
        self.assertEqual(recommend([session(45, "easy")])["action"], "max")

    def test_bodyweight_gets_nothing_but_weighted_timed_work_does(self):
        self.assertIsNone(recommend([session(0, "easy")]))
        self.assertEqual(recommend([session(35, "easy", timed=True)])["weight"], 40)


class HistoryTests(TestCase):
    def test_history_groups_by_name_and_respects_until(self):
        logged(0, row("Bent Over Row", 25, "right"))
        later = logged(7, row("bent over  row", 25, "right"))
        hist = exercise_history()
        self.assertEqual([s["weight_lb"] for s in hist["bent over row"]["sessions"]], [25, 25])
        self.assertEqual(recommendations()["bent over row"]["weight"], 30)
        self.assertEqual(recommendations(until=later.created_at - timedelta(days=1))["bent over row"]["action"], "hold")


class CardAndSaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")

    def test_save_keeps_effort_and_a_row_with_only_effort(self):
        from workouts.views import save_manual_movements
        w = logged(0)
        req = RequestFactory().post(reverse("save_manual_movements", args=[w.workout_id]), {
            "name": ["Reverse Fly", "Bear Crawl", "Striders"], "sets": ["3", "", ""], "reps": ["8", "", ""],
            "weight_lb": ["12", "", ""], "notes": ["", "", ""], "effort": ["easy", "hard", "bogus"],
        })
        req.user, req.session, req._messages = self.user, {}, _NoMessages()
        save_manual_movements(req, w.workout_id)
        w.refresh_from_db()
        self.assertEqual([(r["name"], r.get("effort")) for r in w.manual_movements_json],
                         [("Reverse Fly", "easy"), ("Bear Crawl", "hard")])

    def test_card_shows_next_for_logged_rows_and_try_for_unlogged_plan_rows(self):
        from workouts.views import _manual_movement_context
        logged(0, row("Reverse Fly", 12, "easy"))
        w = logged(7, row("Bent Over Row", 30, "hard"))
        w.class_plan_json = [{"name": "Strength", "exercises": [{"name": "Reverse Fly", "appearances": 3}]}]
        rows = {r["name"]: r for r in _manual_movement_context(w)["manual_rows"]}
        self.assertEqual(rows["Bent Over Row"]["rec"]["action"], "hold")
        self.assertEqual(rows["Reverse Fly"]["rec"]["weight"], 15)    # unlogged today → suggestion from history
        self.assertIsNone(rows["Reverse Fly"]["weight_lb"])

    def test_trends_page_lists_weighted_and_bodyweight_exercises(self):
        from workouts.views import strength_trends
        logged(0, row("Reverse Fly", 12, "easy"), row("Bear Crawl", 0, sets=3, reps=45, notes="seconds"))
        with patch("workouts.views.render", return_value=HttpResponse()) as render:
            strength_trends(RequestFactory().get("/strength/"))
        ctx = render.call_args.args[2]
        self.assertEqual([x["name"] for x in ctx["weighted"]], ["Reverse Fly"])
        self.assertEqual([x["name"] for x in ctx["unweighted"]], ["Bear Crawl"])
        self.assertEqual(ctx["move_up_count"], 1)


class ProgramProgressionFallbackTests(TestCase):
    def test_manual_log_feeds_load_progression_when_tracker_is_empty(self):
        w = logged(0, row("Bent Over Row", 30, notes="per side", sets=3, reps=6),
                   row("Dead Bug", 20, sets=3, reps=90, notes="seconds"))
        loads = list(_workout_exercise_loads(w))
        self.assertEqual(loads, [("bent over row", "Bent Over Row", 30, 1080, 36)])


class _NoMessages:
    def add(self, *args, **kwargs):
        pass
