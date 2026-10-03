import json
from datetime import datetime, timedelta, timezone as dt_tz

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from workouts.models import CachedWorkout

BASE = datetime(2026, 9, 26, 17, 0, tzinfo=dt_tz.utc)


def owner():
    return get_user_model().objects.get_or_create(username="owner", defaults={"is_superuser": True})[0]


def mk(n, discipline, log):
    return CachedWorkout.objects.create(
        user=owner(), workout_id=f"w{n}", title="45 min Lower Body + Run", discipline=discipline,
        created_at=BASE + timedelta(days=n), duration_seconds=2700,
        performance_graph_json={"metrics_by_slug": {}}, manual_movements_json=log,
    )


class ManualLogSummaryTests(TestCase):
    def test_per_side_counts_twice_and_timed_rows_are_time(self):
        w = mk(1, "circuit", [
            {"name": "Reverse Lunge", "sets": 3, "reps": 6, "weight_lb": 30, "notes": "per side"},
            {"name": "Dumbbell Squat", "sets": 3, "reps": 6, "weight_lb": 30, "notes": ""},
            {"name": "Farmer Carry", "sets": 2, "reps": 60, "weight_lb": 35, "notes": "seconds"},
        ])
        self.assertEqual(w.manual_log_summary, {
            "exercises": 3, "total_sets": 8, "total_reps": 54, "timed_seconds": 120,
            # lunge: 36 reps × 1 dumbbell; squat: 18 reps × 2 dumbbells
            "volume_lb": 36 * 30 + 18 * 30 * 2, "heaviest_lb": 35,
        })

    def test_dumbbell_count_from_notes(self):
        w = mk(1, "circuit", [
            {"name": "Woodchop", "sets": 3, "reps": 6, "weight_lb": 20, "notes": "single dumbbell"},
            {"name": "Split Squat", "sets": 1, "reps": 5, "weight_lb": 20, "notes": "per side, two dumbbells"},
            {"name": "Lateral Lunge", "sets": 1, "reps": 5, "weight_lb": 20, "notes": "single leg"},
        ])
        self.assertEqual([r["dumbbells"] for r in w.manual_log_rows], [1, 2, 2])
        self.assertEqual(w.manual_log_summary["volume_lb"], 18 * 20 + 10 * 20 * 2 + 5 * 20 * 2)

    def test_no_log_is_none(self):
        self.assertIsNone(mk(1, "circuit", []).manual_log_summary)


class CompareManualLogTests(TestCase):
    # Calls the view directly and captures render()'s context — Django 4.2's test
    # client can't copy template contexts on Python 3.14 (see test_food_photo).
    def test_circuits_compare_in_strength_mode_with_logged_stats(self):
        from workouts.views import compare
        a = mk(1, "circuit", [{"name": "Hip Bridge", "sets": 3, "reps": 8, "weight_lb": 25, "notes": ""}])
        b = mk(2, "strength", [{"name": "Hip Bridge", "sets": 3, "reps": 8, "weight_lb": 30, "notes": ""}])
        req = RequestFactory().get("/compare/", {"ids": f"{a.workout_id},{b.workout_id}"})
        req.user = owner()
        with patch("workouts.views._client"), \
                patch("workouts.views.render", return_value=HttpResponse()) as render:
            compare(req)
        ctx = render.call_args.args[2]
        self.assertEqual(ctx["compare_mode"], "strength")
        stats = json.loads(ctx["workout_stats"])
        self.assertEqual(stats[b.workout_id]["manual_log"]["volume_lb"], 3 * 8 * 30 * 2)
        detail = json.loads(ctx["workout_detail_data"])
        self.assertEqual(detail[a.workout_id]["manual_log"][0]["name"], "Hip Bridge")
