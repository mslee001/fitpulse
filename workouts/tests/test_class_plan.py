from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse

from workouts.models import CachedWorkout
from workouts.services.peloton_client import parse_class_plan
from workouts.views import _manual_movement_context


def _sub(kind, name, movements):
    return {"type": kind, "display_name": name,
            "movements": [{"name": m, "is_rest": m in ("Rest", "Demo")} for m in movements]}


# Trimmed from the real /api/ride/{id}/details payload for "45 min Lower Body + Run"
RIDE_DETAILS = {"segments": {"segment_list": [
    {"name": "Warm Up", "metrics_type": "floor", "length": 179,
     "subsegments_v2": [_sub("movement", "Warm Up", ["Warm Up"])]},
    {"name": "Lower Body", "metrics_type": "floor", "length": 1501, "subsegments_v2": [
        _sub("rest", "Demo", ["Demo"]),
        _sub("generic_group", "Circuit", ["Hip Bridge", "Single Leg Hip Bridge", "Single Leg Hip Bridge"]),
        _sub("rest", "Rest", ["Rest"]),
        _sub("generic_group", "Circuit", ["Hip Bridge", "Single Leg Hip Bridge", "Single Leg Hip Bridge"]),
        _sub("movement", "Cossack Squat", ["Cossack Squat"]),
        _sub("rest", "Transition", ["Transition"]),
        _sub("movement", "Hip Bridge", ["Hip Bridge"]),
    ]},
    {"name": "Running", "metrics_type": "running", "length": 660, "subsegments_v2": [
        _sub("movement", "Easy Pace", ["Easy Pace"]),
        _sub("movement", "Recovery Pace", ["Recovery Pace"]),
    ]},
]}}


class ParseClassPlanTests(SimpleTestCase):
    def test_keeps_only_real_exercises_in_first_appearance_order(self):
        plan = parse_class_plan(RIDE_DETAILS)
        # warm-up-only and pace-only segments have no exercises, so they're dropped
        self.assertEqual([seg["name"] for seg in plan], ["Lower Body"])
        names = [e["name"] for e in plan[0]["exercises"]]
        self.assertEqual(names, ["Hip Bridge", "Single Leg Hip Bridge", "Cossack Squat"])

    def test_appearances_count_blocks_not_sides(self):
        ex = {e["name"]: e["appearances"] for e in parse_class_plan(RIDE_DETAILS)[0]["exercises"]}
        self.assertEqual(ex["Hip Bridge"], 3)              # two circuits + finisher
        self.assertEqual(ex["Single Leg Hip Bridge"], 2)   # listed per side, counted once per block
        self.assertEqual(ex["Cossack Squat"], 1)

    def test_tolerates_missing_or_empty_payloads(self):
        self.assertEqual(parse_class_plan(None), [])
        self.assertEqual(parse_class_plan({}), [])
        self.assertEqual(parse_class_plan({"segments": {"segment_list": None}}), [])


class ManualMovementsTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        self.w = CachedWorkout.objects.create(
            workout_id="testworkout1", ride_id="r1", title="Circuit", discipline="circuit",
            source="peloton", created_at=timezone.now(),
            class_plan_json=parse_class_plan(RIDE_DETAILS),
        )
        user = get_user_model().objects.create_user("t", password="x")
        self.client.force_login(user)
        self.url = reverse("save_manual_movements", args=[self.w.workout_id])

    def _post(self, **kw):
        return self.client.post(self.url, kw, SERVER_NAME="localhost")

    def test_saves_only_rows_with_numbers_or_notes_and_ignores_junk(self):
        resp = self._post(
            name=["Hip Bridge", "Cossack Squat", "Frogger", "Curl", ""],
            sets=["3", "", "", "abc", "2"],
            reps=["12", "", "", "", ""],
            weight_lb=["25.5", "", "", "-5", ""],
            notes=["", "", "felt easy", "bad numbers", ""],
        )
        self.assertEqual(resp.status_code, 302)
        self.w.refresh_from_db()
        saved = {r["name"]: r for r in self.w.manual_movements_json}
        self.assertEqual(set(saved), {"Hip Bridge", "Frogger", "Curl"})   # blank-name and no-data rows dropped
        self.assertEqual(saved["Hip Bridge"], {"name": "Hip Bridge", "sets": 3, "reps": 12, "weight_lb": 25.5, "notes": ""})
        self.assertEqual(saved["Frogger"]["notes"], "felt easy")
        self.assertIsNone(saved["Curl"]["sets"])          # non-numeric -> None
        self.assertIsNone(saved["Curl"]["weight_lb"])     # negative -> None

    def test_empty_submit_clears_log_and_sync_fields_untouched(self):
        self.w.manual_movements_json = [{"name": "X", "sets": 1, "reps": 1, "weight_lb": None, "notes": ""}]
        self.w.save()
        self._post(name=["Hip Bridge"], sets=[""], reps=[""], weight_lb=[""], notes=[""])
        self.w.refresh_from_db()
        self.assertEqual(self.w.manual_movements_json, [])
        self.assertTrue(self.w.class_plan_json)           # plan unaffected

    def test_detail_sync_does_not_clobber_manual_log(self):
        self.w.manual_movements_json = [{"name": "Hip Bridge", "sets": 3, "reps": 10, "weight_lb": 20.0, "notes": ""}]
        self.w.save()
        self.w.apply_detail({"movements": [], "movement_summary": {}})   # what a re-sync does
        self.w.save(update_fields=CachedWorkout.DETAIL_FIELDS)
        self.w.refresh_from_db()
        self.assertEqual(len(self.w.manual_movements_json), 1)
        self.assertTrue(self.w.class_plan_json)           # empty detail["class_plan"] must not wipe the plan

    def test_form_rows_merge_saved_first_then_unlogged_plan_exercises(self):
        self.w.manual_movements_json = [{"name": "Cossack Squat", "sets": 2, "reps": 8, "weight_lb": None, "notes": ""}]
        ctx = _manual_movement_context(self.w)
        self.assertEqual([r["name"] for r in ctx["manual_rows"]],
                         ["Cossack Squat", "Hip Bridge", "Single Leg Hip Bridge"])
        self.assertTrue(ctx["show_manual_card"])

    def test_card_hidden_when_peloton_tracker_has_data(self):
        self.w.movements = [{"name": "Squat"}]
        self.assertFalse(_manual_movement_context(self.w)["show_manual_card"])

    def test_detail_page_renders_card(self):
        # Called directly rather than via the test client: on Django 4.2 + Python 3.14 the
        # client's template-render instrumentation crashes copying the context
        # ("'super' object has no attribute 'dicts'"), unrelated to this feature.
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from workouts.views import workout_detail
        req = RequestFactory().get("/workout/x/")
        req.user = get_user_model().objects.get(username="t")
        req.session = {}
        req._messages = FallbackStorage(req)
        # No real Peloton client/network in tests: _get_perf_dict would try a live
        # perf-graph fetch for an uncached Peloton workout.
        with patch("workouts.views._client") as fake_client:
            fake_client.return_value.get_parsed_performance.side_effect = RuntimeError("no network in tests")
            resp = workout_detail(req, self.w.workout_id)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("CLASS EXERCISES", html)
        self.assertIn("Single Leg Hip Bridge", html)
