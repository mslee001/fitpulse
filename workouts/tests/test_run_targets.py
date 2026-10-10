"""Pace zone units (minutes.seconds) and the planned/measured run of "+ Run" classes."""
import importlib
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from workouts.models import CachedWorkout
from workouts.run_targets import pace_level_at, planned_run, run_summary, zone_pace_seconds
from workouts.services.peloton_client import (
    PelotonClient, _pace_zone_table, parse_run_targets, zone_pace_minutes,
)
from workouts.tests.helpers import TwoUserTestCase, make_user

# Level 4 Tread zones as Peloton sends them (minutes.seconds)
RUN_TMC = {"workout_pace_level": "level_4", "pace_intensities_mapping": [
    {"value": v, "display_name": n, "pace_levels": [{"slug": "level_4", "display_name": "Level 4",
                                                     "fast_pace": f, "slow_pace": s}]}
    for v, n, f, s in [(0, "Recovery", 16.13, 60.0), (1, "Easy", 14.38, 15.47), (2, "Moderate", 13.2, 14.17),
                       (3, "Challenging", 12.0, 13.03), (4, "Hard", 11.07, 11.46), (5, "Very Hard", 9.5, 10.55),
                       (6, "Max", 4.48, 9.41)]]}
WALK_TMC = {"workout_pace_level": "level_4", "pace_intensities_mapping": [
    {"value": v, "display_name": n, "pace_levels": [{"slug": "level_4", "display_name": "Level 4",
                                                     "fast_pace": f, "slow_pace": s}]}
    for v, n, f, s in [(0, "Recovery", 21.26, 60.0), (1, "Easy", 18.45, 20.41), (2, "Brisk", 16.4, 18.11),
                       (3, "Power", 15.0, 16.13), (4, "Max", 8.27, 14.37)]]}


def _seg(start, end, zone, kind="running"):
    metrics = [{"name": "incline", "upper": 1, "lower": 0}]
    if kind == "running":
        metrics.append({"name": "pace_intensity", "upper": zone, "lower": zone})
    return {"offsets": {"start": start, "end": end}, "segment_type": kind, "metrics": metrics}


RIDE = {"target_metrics_data": {"target_metrics": [
    _seg(60, 1807, None, "floor"),
    _seg(1808, 1923, 1),      # 116 s Easy
    _seg(1924, 1939, 5),      # 16 s Very Hard
    _seg(1940, 2059, 0),      # 120 s Recovery
]}}


class ZoneUnitsTests(SimpleTestCase):
    def test_zone_paces_are_minutes_seconds(self):
        self.assertAlmostEqual(zone_pace_minutes(15.47), 15 + 47 / 60)
        self.assertAlmostEqual(zone_pace_minutes(9.5), 9 + 50 / 60)
        self.assertAlmostEqual(zone_pace_minutes(20.0), 20.0)
        self.assertIsNone(zone_pace_minutes(None))

    def test_running_zones_match_the_pace_chart(self):
        # Easy at Level 4 is 3.8–4.1 mph on Peloton's chart = 15:47–14:38/mi
        label, zones = _pace_zone_table(RUN_TMC)
        self.assertEqual(label, "Level 4")
        self.assertAlmostEqual(60 / zones[1]["slow"], 3.8, places=2)
        self.assertAlmostEqual(60 / zones[1]["fast"], 4.1, places=2)
        self.assertEqual(zones[0]["slow"], 20.0)           # Recovery capped for running

    def test_walking_recovery_gets_the_walking_cap(self):
        _, zones = _pace_zone_table(WALK_TMC)
        self.assertEqual(zones[0]["slow"], 35.0)
        self.assertGreater(zones[0]["slow"], zones[0]["fast"])

    def test_target_pace_series_uses_zone_midpoints(self):
        tmpd = {"target_metrics": [_seg(60, 200, 1)]}
        series = PelotonClient._parse_target_pace(RUN_TMC, tmpd, [{"slug": "pace", "values": [1, 1]}], [0, 5])
        self.assertAlmostEqual(series[0], (zone_pace_minutes(14.38) + zone_pace_minutes(15.47)) / 2)


class PlannedRunTests(SimpleTestCase):
    def test_parse_keeps_only_running_segments(self):
        targets = parse_run_targets(RIDE)
        self.assertEqual([t["upper"] for t in targets], [1, 5, 0])
        self.assertEqual(targets[0], {"start": 1808, "end": 1923, "lower": 1, "upper": 1})
        self.assertEqual(parse_run_targets({}), [])

    def test_zone_paces_from_the_chart(self):
        self.assertAlmostEqual(zone_pace_seconds(4, 1), (3600 / 3.8 + 3600 / 4.1) / 2)
        self.assertAlmostEqual(zone_pace_seconds(4, 0), 3600 / 3.7)     # Recovery: fast edge
        self.assertAlmostEqual(zone_pace_seconds(4, 6), 3600 / 6.2)     # Max: slow edge
        self.assertIsNone(zone_pace_seconds(11, 1))

    def test_planned_run_totals(self):
        plan = planned_run(parse_run_targets(RIDE), 4)
        self.assertEqual(plan["seconds"], 116 + 16 + 120)
        miles = 116 / zone_pace_seconds(4, 1) + 16 / zone_pace_seconds(4, 5) + 120 / zone_pace_seconds(4, 0)
        self.assertEqual(plan["miles"], round(miles, 2))
        self.assertEqual(plan["pace_s"], round(252 / miles))
        self.assertEqual([z["name"] for z in plan["zones"]], ["Recovery", "Easy", "Very Hard"])
        self.assertEqual(plan["segments"][0]["start"], 1808 - 60)   # pre-show removed

    def test_no_level_means_time_only(self):
        plan = planned_run(parse_run_targets(RIDE), None)
        self.assertEqual(plan["seconds"], 252)
        self.assertIsNone(plan["miles"])
        self.assertIsNone(plan["pace_s"])


class RunSummaryTests(TestCase):
    def setUp(self):
        self.user = make_user("t", features=["training"])
        self.now = timezone.now()

    def _workout(self, wid, **kw):
        defaults = dict(user=self.user, workout_id=wid, ride_id="r1", title="45 min Lower Body + Run",
                        discipline="circuit", source="peloton", created_at=self.now)
        defaults.update(kw)
        return CachedWorkout.objects.create(**defaults)

    def test_estimated_without_a_measured_distance(self):
        w = self._workout("w1", run_targets_json={"segments": parse_run_targets(RIDE), "level": 4})
        run = w.run_summary
        self.assertTrue(run["estimated"])
        self.assertEqual(run["miles"], planned_run(parse_run_targets(RIDE), 4)["miles"])

    def test_measured_distance_and_pelotons_average_pace_win(self):
        w = self._workout("w2", distance_miles=1.1677,
                          run_targets_json={"segments": parse_run_targets(RIDE), "level": 3},
                          performance_graph_json={"pace_level": "Level 4",
                                                  "average_summaries": {"avg_pace": {"value": 13.67}}})
        run = run_summary(w)
        self.assertFalse(run["estimated"])
        self.assertEqual(run["miles"], 1.17)
        self.assertEqual(run["pace_s"], round(13.67 * 60))
        self.assertEqual(run["level"], 4)            # the workout's own level beats the stored one

    def test_no_targets_no_summary(self):
        self.assertIsNone(self._workout("w3").run_summary)
        self.assertIsNone(self._workout("w4", run_targets_json={"segments": []}).run_summary)

    def test_pace_level_from_nearest_tread_run(self):
        self._workout("run1", discipline="running", created_at=self.now - timedelta(days=3),
                      performance_graph_json={"pace_level": "Level 3"})
        self._workout("run2", discipline="running", created_at=self.now - timedelta(days=1),
                      performance_graph_json={"pace_level": "Level 4"})
        self._workout("run3", discipline="running", created_at=self.now + timedelta(days=1),
                      performance_graph_json={"pace_level": "Level 5"})
        self.assertEqual(pace_level_at(self.user, self.now), 4)
        self.assertEqual(pace_level_at(self.user, self.now - timedelta(days=5)), 3)   # none before → after
        other = make_user("u")
        self.assertIsNone(pace_level_at(other, self.now))

    def test_detail_sync_stores_targets_once_per_class(self):
        from workouts.sync import _fetch_and_store_details
        self._workout("run1", discipline="running", created_at=self.now - timedelta(days=1),
                      performance_graph_json={"pace_level": "Level 4"})
        a, b = self._workout("w1"), self._workout("w2", created_at=self.now + timedelta(hours=1))
        client = MagicMock()
        client.get_parsed_workout_detail.return_value = {}
        client.get_ride_details.return_value = RIDE
        _fetch_and_store_details(self.user, [a.workout_id, b.workout_id], client)
        self.assertEqual(client.get_ride_details.call_count, 1)
        a.refresh_from_db()
        self.assertEqual(a.run_targets_json["level"], 4)
        self.assertEqual(len(a.run_targets_json["segments"]), 3)
        self.assertTrue(a.run_summary["estimated"])


class MigrationConvertTests(SimpleTestCase):
    def setUp(self):
        self.m = importlib.import_module("workouts.migrations.0042_run_targets_and_pace_zone_units")

    def test_converts_zones_and_targets_once(self):
        old_zones = [{"name": "Recovery", "fast_pace": 16.13, "slow_pace": 20.0},
                     {"name": "Easy", "fast_pace": 14.38, "slow_pace": 15.47}]
        easy_old = (14.38 + 15.47) / 2
        perf = {"pace_zones": old_zones,
                "metrics_by_slug": {"target_pace": {"values": [None, easy_old, 20.0], "average_value": 1}}}
        new, legacy = self.m.convert(perf, [easy_old])
        easy_new = (zone_pace_minutes(14.38) + zone_pace_minutes(15.47)) / 2
        recovery_new = (zone_pace_minutes(16.13) + 20.0) / 2
        self.assertAlmostEqual(new["pace_zones"][1]["fast_pace"], zone_pace_minutes(14.38))
        self.assertEqual(new["metrics_by_slug"]["target_pace"]["values"], [None, easy_new, recovery_new])
        self.assertEqual(legacy, [easy_new])
        self.assertIsNone(self.m.convert(new, legacy))       # marked: never converted twice

    def test_walking_recovery_recapped(self):
        perf = {"pace_zones": [{"name": "Recovery", "fast_pace": 21.26, "slow_pace": 20.0},
                               {"name": "Brisk", "fast_pace": 16.4, "slow_pace": 18.11}]}
        new, _ = self.m.convert(perf, [])
        self.assertEqual(new["pace_zones"][0]["slow_pace"], 35.0)


class RunPagesTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        targets = {"segments": parse_run_targets(RIDE), "level": 4}
        common = dict(user=self.a, ride_id="r1", title="45 min Upper Body Pull + Run", discipline="circuit",
                      source="peloton", run_targets_json=targets)
        self.est = CachedWorkout.objects.create(workout_id="est", created_at=now, **common)
        self.real = CachedWorkout.objects.create(workout_id="real", created_at=now - timedelta(days=7),
                                                 distance_miles=1.09, **common)
        p = patch("workouts.views._peloton_client_or_none", return_value=None)
        p.start(); self.addCleanup(p.stop)

    def test_detail_page_shows_estimated_run(self):
        html = self.client_a.get(reverse("workout_detail", args=["est"])).content.decode()
        self.assertIn("Estimated from the class's pace targets", html)
        self.assertIn("runChart", html)
        self.assertIn("Very Hard", html)

    def test_compare_carries_run_numbers(self):
        resp = self.client_a.get(reverse("compare") + "?ids=est,real")
        stats = json.loads(resp.context["workout_stats"])
        self.assertTrue(stats["est"]["run"]["estimated"])
        self.assertFalse(stats["real"]["run"]["estimated"])
        self.assertEqual(stats["real"]["run"]["miles"], 1.09)

    def test_circuit_next_to_a_tread_run_compares_run_numbers(self):
        CachedWorkout.objects.create(user=self.a, workout_id="tread", ride_id="r2", title="20 min Run",
                                     discipline="running", source="peloton", created_at=timezone.now(),
                                     duration_seconds=1200, distance_miles=1.194, avg_pace_seconds=1005)
        resp = self.client_a.get(reverse("compare") + "?ids=est,tread")
        self.assertEqual(resp.context["compare_mode"], "strength")    # not the bike-stats "mixed" table
        stats = json.loads(resp.context["workout_stats"])
        self.assertEqual(stats["tread"]["run"], {"seconds": 1200, "miles": 1.19, "pace_s": 1005,
                                                 "estimated": False, "level": None})
        self.assertTrue(stats["est"]["run"]["estimated"])

    def test_class_history_uses_running_layout_and_skips_estimates_in_stats(self):
        resp = self.client_a.get(reverse("class_history", args=["r1"]))
        self.assertEqual(resp.context["discipline"], "running")
        self.assertEqual(resp.context["stats"]["best_distance"], 1.09)
        self.assertIn("est.", resp.content.decode())

    def test_other_users_run_is_not_visible(self):
        self.assertEqual(self.client_b.get(reverse("workout_detail", args=["est"])).status_code, 404)
