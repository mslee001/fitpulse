from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from workouts.models import DEFAULT_DUMBBELLS_LB, CachedWorkout, UserSettings
from workouts.programs import _workout_exercise_loads
from workouts.strength import (
    dumbbells, exercise_history, next_dumbbell, parse_rep_range, prev_dumbbell, recommend, recommendations,
)

BASE = datetime(2026, 9, 1, 17, 0, tzinfo=dt_tz.utc)
RACK = list(DEFAULT_DUMBBELLS_LB)
_n = 0


def owner():
    return get_user_model().objects.get_or_create(
        username="owner", defaults={"is_superuser": True, "is_staff": True})[0]


def session(weight, effort="", timed=False, reps=None, rng=None):
    lo, hi = rng or (None, None)
    return {"weight_lb": weight, "effort": effort, "timed": timed, "reps": reps, "rep_min": lo, "rep_max": hi}


def logged(day, *rows, discipline="circuit"):
    global _n
    _n += 1
    return CachedWorkout.objects.create(
        user=owner(), workout_id=f"s{_n}", title="45 min Upper Body Pull + Run", discipline=discipline,
        created_at=BASE + timedelta(days=day), duration_seconds=2700, manual_movements_json=list(rows),
    )


def row(name, weight, effort="", sets=3, reps=8, notes="", rng=None):
    r = {"name": name, "sets": sets, "reps": reps, "weight_lb": weight, "notes": notes}
    if effort:
        r["effort"] = effort
    if rng:
        r["rep_min"], r["rep_max"] = rng
    return r


class DumbbellStepTests(TestCase):
    def test_steps_through_the_rack(self):
        rack = dumbbells(owner())   # default rack
        self.assertEqual(next_dumbbell(5, rack), 7)
        self.assertEqual(next_dumbbell(12, rack), 15)
        self.assertEqual(next_dumbbell(22, rack), 25)    # off-rack weight rounds to the next real one
        self.assertIsNone(next_dumbbell(45, rack))
        self.assertEqual(prev_dumbbell(10, rack), 7)
        self.assertIsNone(prev_dumbbell(2, rack))

    def test_recommendations_follow_the_rack_in_settings(self):
        UserSettings.objects.update_or_create(user=owner(), defaults={"dumbbells_lb": [10, 12.5, 15, 50]})
        rack = dumbbells(owner())
        self.assertEqual(recommend([session(12.5, "easy")], rack)["weight"], 15)
        self.assertEqual(recommend([session(15, "easy")], rack)["weight"], 50)


class SetDumbbellsTests(TestCase):
    def post(self, value):
        from workouts.views import set_dumbbells
        req = RequestFactory().post("/settings/dumbbells/", {"dumbbells": value})
        req.user, req.session, req._messages = owner(), {}, _NoMessages()
        set_dumbbells(req)
        return UserSettings.for_user(owner()).dumbbells_lb

    def test_parses_sorts_and_dedupes(self):
        self.assertEqual(self.post("15, 5 10lb 12.5, 5"), [5, 10, 12.5, 15])

    def test_bad_input_leaves_the_rack_alone(self):
        before = self.post("5, 10")
        self.assertEqual(self.post("5, ten"), before)
        self.assertEqual(self.post(""), before)
        self.assertEqual(self.post("-5"), before)


class RecommendTests(TestCase):
    def test_easy_moves_up_one_dumbbell(self):
        self.assertEqual(recommend([session(12, "easy")], RACK), {
            "action": "up", "weight": 15, "current": 12, "reps": None, "timed": False, "reason": "Felt easy"})

    def test_just_right_needs_two_in_a_row_at_the_same_weight(self):
        self.assertEqual(recommend([session(30, "right")], RACK)["action"], "hold")
        self.assertEqual(recommend([session(30, "right"), session(30, "right")], RACK)["weight"], 35)
        # a lighter session before doesn't count toward the streak
        self.assertEqual(recommend([session(25, "right"), session(30, "right")], RACK)["action"], "hold")

    def test_hard_holds_and_fail_drops(self):
        self.assertEqual(recommend([session(30, "hard")], RACK)["weight"], 30)
        rec = recommend([session(30, "fail")], RACK)
        self.assertEqual((rec["action"], rec["weight"]), ("down", 25))

    def test_unrated_moves_up_after_two_sessions_unless_one_was_hard(self):
        self.assertEqual(recommend([session(20)], RACK)["action"], "hold")
        self.assertEqual(recommend([session(20), session(20)], RACK)["weight"], 25)
        self.assertEqual(recommend([session(20, "hard"), session(20)], RACK)["action"], "hold")

    def test_heaviest_dumbbell_is_maxed(self):
        self.assertEqual(recommend([session(45, "easy")], RACK)["action"], "max")

    def test_bodyweight_gets_nothing_but_weighted_timed_work_does(self):
        self.assertIsNone(recommend([session(0, "easy")], RACK))
        self.assertEqual(recommend([session(35, "easy", timed=True)], RACK)["weight"], 40)


class RepRangeTests(TestCase):
    def test_parse(self):
        self.assertEqual(parse_rep_range("6-8"), (6, 8))
        self.assertEqual(parse_rep_range(" 6 – 8 reps"), (6, 8))
        self.assertEqual(parse_rep_range("8 to 6"), (6, 8))
        self.assertEqual(parse_rep_range("10"), (10, 10))
        self.assertEqual(parse_rep_range("30-45s"), (30, 45))
        self.assertIsNone(parse_rep_range(""))
        for bad in ("six", "0", "6-8-10"):
            with self.assertRaises(ValueError):
                parse_rep_range(bad)

    def test_just_right_below_the_top_adds_a_rep_instead_of_weight(self):
        # 6 reps of a 6–8 range, just right two weeks running → stay at 20, go for 7
        rec = recommend([session(20, "right", reps=6, rng=(6, 8)), session(20, "right", reps=6, rng=(6, 8))], RACK)
        self.assertEqual((rec["action"], rec["weight"], rec["reps"]), ("reps", 20, 7))

    def test_top_of_the_range_moves_up_and_restarts_at_the_bottom(self):
        rec = recommend([session(20, "right", reps=8, rng=(6, 8))], RACK)
        self.assertEqual((rec["action"], rec["weight"], rec["reps"]), ("up", 25, 6))
        self.assertEqual(recommend([session(20, reps=9, rng=(6, 8))], RACK)["action"], "up")   # unrated, past the top

    def test_easy_below_the_top_jumps_to_the_top_reps(self):
        rec = recommend([session(20, "easy", reps=6, rng=(6, 8))], RACK)
        self.assertEqual((rec["action"], rec["weight"], rec["reps"]), ("reps", 20, 8))

    def test_below_the_bottom_aims_for_the_bottom(self):
        self.assertEqual(recommend([session(20, "right", reps=4, rng=(6, 8))], RACK)["reps"], 6)

    def test_hard_and_fail_with_a_range(self):
        rec = recommend([session(20, "hard", reps=7, rng=(6, 8))], RACK)
        self.assertEqual((rec["action"], rec["weight"], rec["reps"]), ("hold", 20, 7))
        rec = recommend([session(20, "fail", reps=5, rng=(6, 8))], RACK)
        self.assertEqual((rec["action"], rec["weight"], rec["reps"]), ("down", 15, 6))

    def test_one_number_range_keeps_the_two_session_rule(self):
        self.assertEqual(recommend([session(20, "right", reps=8, rng=(8, 8))], RACK)["action"], "hold")
        rec = recommend([session(20, "right", reps=8, rng=(8, 8))] * 2, RACK)
        self.assertEqual((rec["action"], rec["weight"]), ("up", 25))

    def test_timed_rows_step_five_seconds(self):
        rec = recommend([session(35, "right", timed=True, reps=30, rng=(30, 45))], RACK)
        self.assertEqual((rec["action"], rec["reps"]), ("reps", 35))

    def test_range_carries_from_the_latest_session_that_has_one(self):
        rec = recommend([session(20, "right", reps=6, rng=(6, 8)), session(20, "right", reps=6)], RACK)
        self.assertEqual((rec["action"], rec["reps"]), ("reps", 7))


class HistoryTests(TestCase):
    def test_history_groups_by_name_and_respects_until(self):
        logged(0, row("Bent Over Row", 25, "right"))
        later = logged(7, row("bent over  row", 25, "right"))
        hist = exercise_history(owner())
        self.assertEqual([s["weight_lb"] for s in hist["bent over row"]["sessions"]], [25, 25])
        self.assertEqual(recommendations(owner())["bent over row"]["weight"], 30)
        self.assertEqual(recommendations(owner(), until=later.created_at - timedelta(days=1))["bent over row"]["action"], "hold")


class CardAndSaveTests(TestCase):
    def setUp(self):
        self.user = owner()

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

    def test_save_stores_the_rep_range_but_a_range_alone_is_not_a_log(self):
        from workouts.views import save_manual_movements
        w = logged(0)
        req = RequestFactory().post(reverse("save_manual_movements", args=[w.workout_id]), {
            "name": ["Split Squat", "Woodchop", "Farmer Carry"], "sets": ["3", "", "2"], "reps": ["6", "", "60"],
            "weight_lb": ["20", "", "35"], "notes": ["", "", "seconds"], "effort": ["right", "", "hard"],
            "rep_range": ["6–8", "8-10", "nope"],
        })
        req.user, req.session, req._messages = self.user, {}, _NoMessages()
        save_manual_movements(req, w.workout_id)
        w.refresh_from_db()
        self.assertEqual([(r["name"], r.get("rep_min"), r.get("rep_max")) for r in w.manual_movements_json],
                         [("Split Squat", 6, 8), ("Farmer Carry", None, None)])

    def test_card_carries_the_rep_range_onto_unlogged_plan_rows(self):
        from workouts.views import _manual_movement_context
        logged(0, row("Split Squat", 20, "right", reps=6, rng=(6, 8)))
        w = logged(7)
        w.class_plan_json = [{"name": "Lower", "exercises": [{"name": "Split Squat", "appearances": 3}]}]
        r = _manual_movement_context(w)["manual_rows"][0]
        self.assertEqual(r["rep_range"], "6–8")
        self.assertEqual((r["rec"]["action"], r["rec"]["weight"], r["rec"]["reps"]), ("reps", 20, 7))

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
            req = RequestFactory().get("/strength/")
            req.user = owner()
            strength_trends(req)
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
