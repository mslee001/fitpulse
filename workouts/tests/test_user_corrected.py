from datetime import datetime, timezone as dt_tz
from unittest.mock import MagicMock

from django.test import TestCase

from workouts.models import CachedWorkout
from workouts.sync import _fetch_and_store_performance, _upsert_page

GHOST_START = 1790963468  # Peloton's start for the session rejoined after a Tread reboot
REAL_START = datetime(2026, 10, 2, 17, 14, 42, tzinfo=dt_tz.utc)


def payload(**overrides):
    data = {
        "id": "w1", "start_time": GHOST_START, "fitness_discipline": "strength",
        "calories": 2, "avg_heart_rate": 85, "status": "COMPLETE",
        "peloton": {"ride": {"id": "r1", "title": "45 min Upper Body Pull + Run", "duration": 2700}},
    }
    data.update(overrides)
    return data


class UserCorrectedTests(TestCase):
    def setUp(self):
        _upsert_page([payload()])
        CachedWorkout.objects.filter(workout_id="w1").update(
            created_at=REAL_START, duration_seconds=1721, calories=217, heart_rate_avg=127,
            manual_movements_json=[{"name": "Bent Over Row", "sets": 3, "reps": 6}],
            user_corrected=True,
        )

    def test_resync_keeps_corrected_stats(self):
        _upsert_page([payload(name="renamed")])
        w = CachedWorkout.objects.get(workout_id="w1")
        self.assertEqual(w.created_at, REAL_START)
        self.assertEqual((w.duration_seconds, w.calories, w.heart_rate_avg), (1721, 217, 127))
        self.assertEqual(len(w.manual_movements_json), 1)
        # non-stat fields still refresh from Peloton
        self.assertEqual(w.title, "45 min Upper Body Pull + Run")
        self.assertEqual(w.raw_data["name"], "renamed")

    def test_uncorrected_workout_still_overwritten(self):
        CachedWorkout.objects.filter(workout_id="w1").update(user_corrected=False)
        _upsert_page([payload()])
        w = CachedWorkout.objects.get(workout_id="w1")
        self.assertEqual((w.calories, w.duration_seconds), (2, 2700))

    def test_perf_graph_stored_without_overwriting_stats(self):
        client = MagicMock()
        client.get_parsed_performance.return_value = {
            "summaries": {"calories": {"value": 2}},
            "metrics_by_slug": {"heart_rate": {"average_value": 85}},
        }
        _fetch_and_store_performance(["w1"], client)
        w = CachedWorkout.objects.get(workout_id="w1")
        self.assertIsNotNone(w.performance_graph_json)
        self.assertEqual((w.calories, w.heart_rate_avg), (217, 127))

    def test_detail_sync_keeps_corrected_hr_zones(self):
        w = CachedWorkout.objects.get(workout_id="w1")
        w.apply_detail({"hr_zones": {"z1": 4056, "z2": 6809}})
        self.assertIsNone(w.hr_z1_seconds)
