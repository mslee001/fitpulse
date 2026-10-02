"""Google Health / Peloton / Garmin duplicate detection — time-span overlap.

Covers two 2026-09-28 cases where Google Health's own on-device auto-detection
draws different session boundaries than Peloton/Garmin did, in either direction,
landing as permanent duplicates because _find_workout_match's window is centered
on the point's own start (which isn't near the real workout's start in either
case):
- nested: a circuit class's strength portion detected as a standalone "Free
  weights" entry starting ~14 min in, its running portion as "Treadmill run"
  starting ~30 min in.
- spanning: three separate back-to-back Peloton classes (a 45-min circuit class,
  a 5-min cool-down walk, a 15-min stretch) folded into one 71-minute "Bootcamp"
  entry.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone as tz

from workouts.models import CachedWorkout, Integration
from workouts.sync import (
    _find_overlapping_workout, _find_workout_match, _reconcile_garmin_google_health_duplicates,
    _reconcile_google_health_duplicates, _run_google_health_exercise_sync,
)

_n = 0


def mk(source, offset_min, minutes, discipline, title, workout_id=None, raw_data=None, **extra):
    global _n
    _n += 1
    return CachedWorkout.objects.create(
        workout_id=workout_id or f"w{_n}", source=source, title=title, discipline=discipline,
        created_at=BASE + timedelta(minutes=offset_min), duration_seconds=minutes * 60,
        raw_data=raw_data or {}, **extra,
    )


BASE = tz.now().replace(hour=10, minute=55, second=0, microsecond=0)


def index_of(*workouts):
    return sorted((int(w.created_at.timestamp()), w) for w in workouts)


class FindOverlappingWorkoutTests(TestCase):
    def setUp(self):
        # "45 min Lower Body + Run": 10:55 -> 11:40
        self.main = mk("peloton", 0, 45, "circuit", "45 min Lower Body + Run")
        self.index = index_of(self.main)

    def test_finds_a_sub_segment_starting_well_into_the_main_workout(self):
        # "Free weights": 11:09 -> 11:24 (14 min in)
        point_start = self.main.created_at + timedelta(minutes=14)
        found = _find_overlapping_workout(point_start, 15 * 60, self.index)
        self.assertEqual(found, self.main)

    def test_finds_a_second_sub_segment_near_the_main_workouts_own_end(self):
        # "Treadmill run": 11:25 -> 11:39, ends 1 min before the main workout does
        point_start = self.main.created_at + timedelta(minutes=30)
        found = _find_overlapping_workout(point_start, 14 * 60, self.index)
        self.assertEqual(found, self.main)

    def test_find_workout_match_alone_misses_these(self):
        # confirms the bug this fixes: the existing near-simultaneous-start matcher
        # doesn't find the main workout for a point starting 14+ minutes into it
        point_start = self.main.created_at + timedelta(minutes=14)
        self.assertIsNone(_find_workout_match(point_start, self.index))

    def test_a_point_running_well_past_the_main_workouts_end_still_matches_on_its_overlap(self):
        # 5 min of real overlap (40-45) with a workout that's otherwise unrelated to the
        # rest of the point's span — this is deliberately broader than strict containment,
        # same as the real "Bootcamp" case, which runs well past every workout it covers.
        point_start = self.main.created_at + timedelta(minutes=40)
        self.assertEqual(_find_overlapping_workout(point_start, 20 * 60, self.index), self.main)

    def test_only_a_brief_overlap_past_the_main_workouts_end_does_not_match(self):
        # 2 min of overlap (43-45) — under the 180s tolerance, treated as two genuinely
        # separate, merely adjacent sessions rather than a duplicate.
        point_start = self.main.created_at + timedelta(minutes=43)
        self.assertIsNone(_find_overlapping_workout(point_start, 20 * 60, self.index))

    def test_a_point_starting_well_before_the_main_workout_still_matches_on_its_overlap(self):
        # starts 5 min before the main workout, 10 min of real overlap (0-10) with it
        point_start = self.main.created_at - timedelta(minutes=5)
        self.assertEqual(_find_overlapping_workout(point_start, 15 * 60, self.index), self.main)

    def test_only_a_brief_overlap_before_the_main_workout_does_not_match(self):
        # starts 8 min before, ends 1 min after the main workout starts — only 60s overlap
        point_start = self.main.created_at - timedelta(minutes=8)
        self.assertIsNone(_find_overlapping_workout(point_start, 9 * 60, self.index))

    def test_an_unrelated_separate_workout_does_not_match(self):
        point_start = self.main.created_at + timedelta(hours=3)
        self.assertIsNone(_find_overlapping_workout(point_start, 15 * 60, self.index))

    def test_tolerance_covers_a_slight_overrun_past_the_edges(self):
        # starts 30s before the recorded start, ends 30s after the recorded end
        point_start = self.main.created_at - timedelta(seconds=30)
        found = _find_overlapping_workout(point_start, 45 * 60 + 60, self.index)
        self.assertEqual(found, self.main)

    def test_none_input_is_safe(self):
        self.assertIsNone(_find_overlapping_workout(None, 60, self.index))
        self.assertIsNone(_find_overlapping_workout(self.main.created_at, 60, []))

    def test_finds_a_workout_when_a_point_spans_several_of_them(self):
        # Real 2026-09-22 pattern: a single "Bootcamp" entry (7-79 min) overlaps three
        # separate back-to-back Peloton classes, none of which start near the point's
        # own start (the push+run class is 7 min before it, well outside the 5 min
        # near-simultaneous window; the other two start well after).
        push_run = mk("peloton", 0, 45, "circuit", "45 min Upper Body Push + Run")
        walk = mk("peloton", 46, 5, "walking", "5 min Cool Down Walk")
        stretch = mk("peloton", 56, 15, "stretching", "15 min Full Body Stretch")
        index = index_of(push_run, walk, stretch)
        point_start = push_run.created_at + timedelta(minutes=7)
        found = _find_overlapping_workout(point_start, 72 * 60, index)
        self.assertIn(found, (push_run, walk, stretch))

    def test_brief_overlap_at_a_boundary_does_not_match(self):
        # two back-to-back real workouts whose timestamps overlap by a few seconds of
        # clock noise must not be treated as duplicates of each other
        point_start = self.main.created_at + timedelta(minutes=44, seconds=55)
        self.assertIsNone(_find_overlapping_workout(point_start, 20 * 60, self.index))


class ReconcileOverlapTests(TestCase):
    def setUp(self):
        # google_health is seeded disabled by default (migration 0012) — these
        # reconcilers gate on it being on.
        Integration.objects.update_or_create(key="google_health", defaults={"is_enabled": True})
        self.main = mk("peloton", 0, 45, "circuit", "45 min Lower Body + Run")
        # Real values from the 2026-09-26 case, so an accidental merge would be obvious.
        self.free_weights = mk("google_health", 14, 15, "strength", "Free weights",
                               workout_id="google_health_free_weights", calories=102, heart_rate_avg=136,
                               raw_data={"exercise": {"interval": {"startTime": "x"}, "metricsSummary": {"caloriesKcal": 102}}})
        self.treadmill = mk("google_health", 30, 14, "running", "Treadmill run",
                            workout_id="google_health_treadmill", calories=150, heart_rate_avg=161,
                            avg_pace_seconds=713,
                            raw_data={"exercise": {"interval": {"startTime": "x"}, "metricsSummary": {"caloriesKcal": 150}}})

    def test_both_sub_segments_are_deleted_and_main_workout_is_never_touched(self):
        result = _reconcile_google_health_duplicates()
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["augmented"], 0)          # never augmented — see module docstring
        self.assertFalse(CachedWorkout.objects.filter(source="google_health").exists())
        self.main.refresh_from_db()
        self.assertEqual(self.main.calories, None)         # unchanged — no partial-segment stats leaked in
        self.assertIsNone(self.main.avg_pace_seconds)       # would have been wrongly set to the treadmill segment's own pace

    def test_details_flag_overlap_only_matches(self):
        result = _reconcile_google_health_duplicates()
        by_id = {d["google_workout_id"]: d for d in result["details"]}
        self.assertTrue(by_id["google_health_free_weights"]["overlap_only"])
        self.assertEqual(by_id["google_health_free_weights"]["filled"], [])

    def test_dry_run_reports_without_deleting_or_writing(self):
        result = _reconcile_google_health_duplicates(dry_run=True)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(CachedWorkout.objects.filter(source="google_health").count(), 2)

    def test_a_genuine_same_session_duplicate_still_augments_normally(self):
        # sanity check: containment logic must not disturb the existing merge path
        real_dup = mk("google_health", 0, 45, "circuit", "Lower Body + Run",
                      workout_id="google_health_real_dup", distance_miles=3.1,
                      raw_data={"name": "exercises/real_dup", "exercise": {"interval": {"startTime": "2026-01-01T00:00:00Z"}, "metricsSummary": {"distanceMillimeters": 4988000}}})
        result = _reconcile_google_health_duplicates()
        by_id = {d["google_workout_id"]: d for d in result["details"]}
        self.assertFalse(by_id["google_health_real_dup"]["overlap_only"])
        self.main.refresh_from_db()
        self.assertIsNotNone(self.main.distance_miles)     # this one *should* fill in — it's a real same-session match

    def test_garmin_reconciler_deletes_overlapping_segment_without_augmenting(self):
        CachedWorkout.objects.filter(source="google_health").delete()
        garmin_main = mk("garmin", 60, 30, "strength", "Strength", workout_id="garmin_main")
        segment = mk("google_health", 60 + 10, 10, "strength", "Free weights",   # 10 min in: outside _find_workout_match's 5 min window
                     workout_id="google_health_garmin_segment", calories=80,
                     raw_data={"name": "exercises/garmin_segment", "exercise": {"interval": {"startTime": "2026-01-01T00:00:00Z"}, "metricsSummary": {"caloriesKcal": 80}}})
        result = _reconcile_garmin_google_health_duplicates()
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(result["details"][0]["overlap_only"])
        garmin_main.refresh_from_db()
        self.assertIsNone(garmin_main.calories)

    def test_row_with_no_raw_data_still_deletes_cleanly_when_overlapping(self):
        bare = mk("google_health", 20, 10, "strength", "Free weights", workout_id="google_health_bare", raw_data={})
        result = _reconcile_google_health_duplicates()
        by_id = {d["google_workout_id"]: d for d in result["details"]}
        self.assertTrue(by_id["google_health_bare"]["overlap_only"])
        self.assertFalse(CachedWorkout.objects.filter(pk=bare.pk).exists())

    def test_a_point_spanning_three_separate_workouts_is_deleted_and_none_are_touched(self):
        CachedWorkout.objects.filter(source="google_health").delete()
        push_run = mk("peloton", 0, 45, "circuit", "45 min Upper Body Push + Run", workout_id="peloton_push_run")
        walk = mk("peloton", 46, 5, "walking", "5 min Cool Down Walk", workout_id="peloton_walk")
        stretch = mk("peloton", 56, 15, "stretching", "15 min Full Body Stretch", workout_id="peloton_stretch")
        bootcamp = mk("google_health", 7, 72, "cardio", "Bootcamp", workout_id="google_health_bootcamp",
                      calories=544,
                      raw_data={"name": "exercises/bootcamp",
                                "exercise": {"interval": {"startTime": "2026-01-01T00:00:00Z"},
                                            "metricsSummary": {"caloriesKcal": 544}}})
        result = _reconcile_google_health_duplicates()
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(CachedWorkout.objects.filter(workout_id="google_health_bootcamp").exists())
        for w in (push_run, walk, stretch):
            w.refresh_from_db()
            self.assertIsNone(w.calories)   # none received the Bootcamp's blended, cross-session stats


class LiveSyncOverlapTests(TestCase):
    """_run_google_health_exercise_sync's own overlap check, before a row is ever created."""

    def setUp(self):
        Integration.objects.update_or_create(key="google_health", defaults={"is_enabled": True})
        self.main = mk("peloton", 0, 45, "circuit", "45 min Lower Body + Run")

    def _point(self, offset_min, minutes, discipline_title, calories=100):
        start = self.main.created_at + timedelta(minutes=offset_min)
        end = start + timedelta(minutes=minutes)
        return {
            "name": f"exercises/{offset_min}_{minutes}",
            "dataSource": {"application": {"packageName": "com.google.android.apps.fitness"}},
            "exercise": {
                "displayName": discipline_title,
                "exerciseType": "STRENGTH_TRAINING",
                "interval": {"startTime": start.isoformat().replace("+00:00", "Z"),
                            "endTime": end.isoformat().replace("+00:00", "Z")},
                "metricsSummary": {"caloriesKcal": calories},
            },
        }

    def test_nested_sub_segment_is_skipped_and_never_created(self):
        point = self._point(14, 15, "Free weights")
        with patch("workouts.services.google_health_client.GoogleHealthClient") as MockClient:
            MockClient.return_value.get_exercise.return_value = [point]
            result = _run_google_health_exercise_sync(self.main.created_at.date(), self.main.created_at.date())
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_overlapping_workouts"], 1)
        self.assertFalse(CachedWorkout.objects.filter(source="google_health").exists())
        self.main.refresh_from_db()
        self.assertIsNone(self.main.calories)

    def test_a_real_standalone_workout_is_still_created_normally(self):
        point = self._point(300, 30, "Evening Yoga")   # 5 hours later — unrelated
        with patch("workouts.services.google_health_client.GoogleHealthClient") as MockClient:
            MockClient.return_value.get_exercise.return_value = [point]
            result = _run_google_health_exercise_sync(self.main.created_at.date(), self.main.created_at.date())
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped_overlapping_workouts"], 0)

    def test_point_spanning_three_workouts_is_skipped_and_never_created(self):
        mk("peloton", 46, 5, "walking", "5 min Cool Down Walk", workout_id="peloton_walk2")
        mk("peloton", 56, 15, "stretching", "15 min Full Body Stretch", workout_id="peloton_stretch2")
        point = self._point(7, 72, "Bootcamp", calories=544)
        with patch("workouts.services.google_health_client.GoogleHealthClient") as MockClient:
            MockClient.return_value.get_exercise.return_value = [point]
            result = _run_google_health_exercise_sync(self.main.created_at.date(), self.main.created_at.date())
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_overlapping_workouts"], 1)
        self.assertFalse(CachedWorkout.objects.filter(source="google_health").exists())
