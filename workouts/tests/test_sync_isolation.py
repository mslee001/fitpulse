"""Sync runs per user: dedup never crosses users, locks are per user, Garmin is
owner-only, and sync_daily isolates one user's failure from the rest."""
from datetime import date, datetime, timedelta, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from workouts.models import CachedWorkout, DailyStats, GoogleHealthAuth, Integration, PelotonAuth
from workouts.sync import (
    _reconcile_garmin_google_health_duplicates, _reconcile_google_health_duplicates,
    _run_garmin_sync_new, _run_google_health_exercise_sync, _run_google_health_wellness_sync, user_lock,
)
from workouts.tests.helpers import TwoUserTestCase

START = datetime.now(dt_tz.utc).replace(hour=17, minute=0, second=0, microsecond=0) - timedelta(days=1)


def gh_point(name, start, minutes, title="Free weights", calories=150):
    end = start + timedelta(minutes=minutes)
    return {
        "name": f"exercises/{name}",
        "dataSource": {"application": {"packageName": "com.google.android.apps.fitness"}},
        "exercise": {
            "displayName": title, "exerciseType": "STRENGTH_TRAINING",
            "interval": {"startTime": start.isoformat().replace("+00:00", "Z"),
                         "endTime": end.isoformat().replace("+00:00", "Z")},
            "activeDuration": f"{minutes * 60}s",
            "metricsSummary": {"caloriesKcal": calories},
        },
    }


class SyncIsolationTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        for u in (self.a, self.b):
            Integration.ensure_for_user(u)
        Integration.objects.filter(key="google_health").update(is_enabled=True)
        # Alice's Peloton class at 17:00, 45 min
        self.alice_class = CachedWorkout.objects.create(
            user=self.a, workout_id="alice-peloton", source="peloton", discipline="strength",
            title="45 min Strength", created_at=START, duration_seconds=45 * 60)

    def sync_b(self, points):
        with patch("workouts.services.google_health_client.GoogleHealthClient") as client:
            client.return_value.get_exercise.return_value = points
            return _run_google_health_exercise_sync(self.b, START.date(), START.date())

    def assert_alice_untouched(self):
        self.alice_class.refresh_from_db()
        self.assertIsNone(self.alice_class.calories)
        self.assertIsNone(self.alice_class.google_health_activity_id)

    def test_same_time_workouts_for_two_users_are_never_merged(self):
        result = self.sync_b([gh_point("bob1", START, 45)])
        self.assertEqual(result["created"], 1)
        _reconcile_google_health_duplicates(self.a)
        _reconcile_google_health_duplicates(self.b)
        _reconcile_garmin_google_health_duplicates(self.a)
        _reconcile_garmin_google_health_duplicates(self.b)
        self.assertTrue(CachedWorkout.objects.filter(user=self.b, source="google_health").exists())
        self.assertTrue(CachedWorkout.objects.filter(pk=self.alice_class.pk).exists())
        self.assert_alice_untouched()

    def test_contained_sub_segment_of_another_users_workout_is_not_skipped(self):
        # 14 min into Alice's class — would be an overlap duplicate if it were Alice's
        result = self.sync_b([gh_point("bob2", START + timedelta(minutes=14), 15)])
        self.assertEqual((result["created"], result["skipped_overlapping_workouts"]), (1, 0))
        _reconcile_google_health_duplicates(self.b)
        self.assertEqual(CachedWorkout.objects.filter(user=self.b).count(), 1)
        self.assert_alice_untouched()

    def test_wellness_sync_writes_only_that_users_row(self):
        d = date.today() - timedelta(days=1)
        DailyStats.objects.create(user=self.a, date=d, resting_hr=50)
        rhr =[{"dailyRestingHeartRate": {"date": {"year": d.year, "month": d.month, "day": d.day},
                                          "beatsPerMinute": "61"}}]
        with patch("workouts.services.google_health_client.GoogleHealthClient") as cls:
            inst = cls.return_value
            for name in dir(inst):
                if name.startswith("get_"):
                    getattr(inst, name).return_value = []
            inst.get_daily_resting_heart_rate.return_value = rhr
            inst.get_height.return_value = []
            result = _run_google_health_wellness_sync(self.b, [d])
        self.assertEqual(result.get("synced"), 1, result)
        self.assertEqual(DailyStats.objects.get(user=self.b, date=d).resting_hr, 61)
        self.assertEqual(DailyStats.objects.get(user=self.a, date=d).resting_hr, 50)

    def test_one_users_lock_does_not_block_another(self):
        d = date.today()
        lock = user_lock("gh_wellness", self.a.id)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with patch("workouts.sync._run_google_health_wellness_sync_locked", return_value={"done": True}) as body:
                self.assertEqual(_run_google_health_wellness_sync(self.b, [d]), {"done": True})
                self.assertEqual(_run_google_health_wellness_sync(self.a, [d])["skipped"], "already_in_progress")
            body.assert_called_once()
        finally:
            lock.release()

    def test_garmin_is_owner_only(self):
        with patch("workouts.sync._garmin_client") as garmin:
            self.assertEqual(_run_garmin_sync_new(self.b), {"error": "Garmin is owner-only"})
            garmin.assert_not_called()
        self.assertEqual(self.client_b.get("/api/sync/garmin/new/").status_code, 403)

    def test_sync_daily_keeps_going_after_one_users_failure(self):
        for u in (self.a, self.b):
            PelotonAuth.objects.create(user=u, session_id=f"cookie-{u.username}", peloton_user_id=f"p-{u.username}")
        Integration.objects.filter(user=self.a, key="garmin").update(is_enabled=False)
        Integration.objects.filter(key="google_health").update(is_enabled=False)
        calls = []

        def peloton(user, days=None):
            calls.append(user.username)
            if user == self.a:
                raise RuntimeError("cookie expired")
            return {"done": True, "created": 1, "updated": 0}

        out = StringIO()
        with patch("workouts.management.commands.sync_daily._run_peloton_sync_new", side_effect=peloton):
            with self.assertRaises(SystemExit):
                call_command("sync_daily", stdout=out)
        self.assertEqual(calls, ["alice", "bob"])
        self.assertIn("[sync_daily] [bob] ", out.getvalue())
        from workouts.models import UserSettings
        self.assertIsNotNone(UserSettings.for_user(self.b).last_daily_sync_at)
        self.assertIsNone(UserSettings.for_user(self.a).last_daily_sync_at)

    def test_sync_daily_user_option_and_skips_unconnected(self):
        out = StringIO()
        with patch("workouts.management.commands.sync_daily._run_peloton_sync_new") as peloton:
            call_command("sync_daily", "--user", "bob", stdout=out)
        peloton.assert_not_called()
        self.assertIn("[sync_daily] [bob] Nothing connected", out.getvalue())
        self.assertNotIn("alice", out.getvalue())

    def test_food_export_uses_the_entry_owners_google_account(self):
        from workouts.models import FoodEntry
        from workouts.sync import _push_food_entry_to_google_health
        entry = FoodEntry.objects.create(user=self.b, date=date.today(), raw_text="toast", calories=100)
        with patch("workouts.services.google_health_client.GoogleHealthClient") as cls:
            self.assertFalse(_push_food_entry_to_google_health(entry))   # Bob hasn't connected Google Health
            cls.assert_not_called()
            GoogleHealthAuth.objects.create(user=self.b, access_token="t", refresh_token="r",
                                            token_expires_at=datetime.now(dt_tz.utc))
            cls.return_value._request.return_value = {"response": {"name": "logs/1"}}
            self.assertTrue(_push_food_entry_to_google_health(entry))
            cls.assert_called_once_with(self.b)
