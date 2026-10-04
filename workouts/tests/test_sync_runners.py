"""Run the real sync functions end to end with only the external APIs faked —
the multi-user deploy shipped calls that broke at runtime because tests
stubbed these functions out instead of calling them."""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from workouts.models import BodyMeasurement, CachedWorkout, DailyStats, Integration
from workouts.sync import (
    _reconcile_programs_safe, _run_peloton_sync_all, _run_peloton_sync_new, _run_withings_sync_all,
    _run_withings_sync_new, _run_withings_sync_range,
)
from workouts.tests.helpers import TwoUserTestCase

NOW = datetime.now(dt_tz.utc).replace(microsecond=0)


def peloton_workout(wid, hours_ago=2):
    return {"id": wid, "start_time": int((NOW - timedelta(hours=hours_ago)).timestamp()),
            "fitness_discipline": "cycling", "status": "COMPLETE",
            "peloton": {"ride": {"id": f"ride-{wid}", "title": f"Ride {wid}", "duration": 1800}}}


def measurement(grpid):
    return {"grpid": grpid, "measured_at": NOW - timedelta(hours=3), "weight_lb": 150.0, "raw": {}}


class SyncRunnerTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        Integration.ensure_for_user(self.b)

    @patch("workouts.sync.PelotonClient")
    def test_peloton_sync_new_and_all(self, client_cls):
        client = client_cls.return_value
        client.get_workouts.return_value = {"data": [peloton_workout("w1")], "total": 1}
        client.get_parsed_workout_detail.return_value = {}
        client.get_parsed_performance.return_value = {}
        result = _run_peloton_sync_new(self.b)
        self.assertTrue(result.get("done"), result)
        client_cls.assert_called_with(self.b)
        self.assertTrue(CachedWorkout.objects.filter(user=self.b, workout_id="w1").exists())
        result = _run_peloton_sync_all(self.b)
        self.assertTrue(result.get("done"), result)

    @patch("workouts.sync.WithingsClient")
    def test_withings_syncs(self, client_cls):
        client_cls.return_value.get_measurements.return_value = [measurement("g1")]
        for run in (_run_withings_sync_new, _run_withings_sync_all):
            result = run(self.b)
            self.assertTrue(result.get("done"), result)
        result = _run_withings_sync_range(self.b, NOW.date() - timedelta(days=1), NOW.date())
        self.assertTrue(result.get("done"), result)
        client_cls.assert_called_with(self.b)
        self.assertEqual(BodyMeasurement.objects.filter(user=self.b).count(), 1)
        self.assertTrue(DailyStats.objects.filter(user=self.b, weight_lb=150.0).exists())

    def test_program_reconcile_runs(self):
        self.assertEqual(_reconcile_programs_safe(self.b), {"associated": 0, "recoveries": 0})
