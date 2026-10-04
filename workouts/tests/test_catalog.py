"""Peloton class catalog sync: paging rules, unavailable classes, category
merging, incremental stop, removal marking, and the owner-only refresh."""
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from workouts.catalog import sync_catalog
from workouts.models import PelotonAuth, PelotonClass, PelotonClassType, SyncJob
from workouts.services.peloton_client import PelotonNetworkError
from workouts.tests.helpers import TwoUserTestCase

AIR = 1791033007


def _item(rid, available=True, instructor="i1", air=AIR):
    return {"id": rid, "title": f"Class {rid}", "description": "", "duration": 1800, "length": 1900,
            "fitness_discipline": "running", "class_type_ids": ["t1"], "ride_type_id": "t1",
            "instructor_id": instructor, "difficulty_estimate": 7.1, "difficulty_level": None,
            "difficulty_rating_count": 40, "overall_rating_avg": 0.98, "overall_rating_count": 50,
            "original_air_time": air, "is_outdoor": False, "has_tread_pace_target": True,
            "equipment_tags": [], "is_explicit": False, "language": "english", "image_url": "",
            "availability": {"is_available": available, "reason": None}}


def _page(items, total, page_count=999):
    return {"data": items, "total": total, "count": len(items), "page_count": page_count,
            "class_types": [{"id": "t1", "name": "Endurance", "display_name": "Endurance",
                             "fitness_discipline": "running", "is_active": True}],
            "instructors": [{"id": "i1", "name": "Becs Gentry"}]}


class FakeClient:
    """Serves pages per category: pages[category] = [page0, page1, ...]."""
    def __init__(self, pages, fail=None):
        self.pages, self.fail, self.calls = pages, fail or {}, []

    def get_archived_classes(self, category, page):
        self.calls.append((category, page))
        if (category, page) in self.fail:
            raise PelotonNetworkError("boom")
        return self.pages[category][page]


class CatalogSyncTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        PelotonAuth.objects.create(user=self.a, peloton_user_id="pa", refresh_token="r")

    def _sync(self, pages, fail=None, **kw):
        client = FakeClient(pages, fail)
        with patch("workouts.catalog.PelotonClient", return_value=client):
            result = sync_catalog(self.a, pause=0, **kw)
        return client, result

    def test_paging_stops_on_short_page_with_limit_100(self):
        pages = {"running": [_page([_item(f"a{i}") for i in range(100)], 250),
                             _page([_item(f"b{i}") for i in range(100)], 250),
                             _page([_item(f"c{i}") for i in range(50)], 250)]}
        client, result = self._sync(pages, categories=["running"], full=True)
        self.assertEqual(client.calls, [("running", 0), ("running", 1), ("running", 2)])
        self.assertEqual(result["categories"]["running"]["created"], 250)
        self.assertEqual(PelotonClass.objects.count(), 250)

    def test_page_size_param_is_100(self):
        from workouts.services.peloton_client import PelotonClient
        c = PelotonClient.__new__(PelotonClient)
        with patch.object(PelotonClient, "_get", return_value={}) as get:
            c.get_archived_classes("Running", 2)
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["limit"], params["page"], params["browse_category"]), (100, 2, "running"))

    def test_stops_when_page_reaches_total_even_if_full_page(self):
        pages = {"running": [_page([_item(f"a{i}") for i in range(100)], 100)]}
        client, _ = self._sync(pages, categories=["running"], full=True)
        self.assertEqual(client.calls, [("running", 0)])

    def test_fields_mapped(self):
        self._sync({"running": [_page([_item("x")], 1)]}, categories=["running"])
        c = PelotonClass.objects.get(pk="x")
        self.assertEqual((c.title, c.duration_seconds, c.class_type_id, c.instructor_name, c.categories),
                         ("Class x", 1800, "t1", "Becs Gentry", ",running,"))
        self.assertEqual(int(c.original_air_time.timestamp()), AIR)
        self.assertEqual(PelotonClassType.objects.get(pk="t1").name, "Endurance")

    def test_unavailable_items_are_skipped_and_counted(self):
        _, result = self._sync({"running": [_page([_item("x"), _item("y", available=False)], 2)]},
                               categories=["running"])
        self.assertEqual(result["categories"]["running"]["skipped_unavailable"], 1)
        self.assertFalse(PelotonClass.objects.filter(pk="y").exists())

    def test_pilates_seen_under_strength_and_pilates_is_one_row(self):
        item = dict(_item("p1"), fitness_discipline="strength")
        self._sync({"strength": [_page([item], 1)], "pilates": [_page([item], 1)]},
                   categories=["strength", "pilates"])
        self.assertEqual(PelotonClass.objects.count(), 1)
        self.assertEqual(PelotonClass.objects.get(pk="p1").categories, ",strength,pilates,")

    def test_incremental_stops_after_first_all_known_page(self):
        full = {"running": [_page([_item(f"a{i}") for i in range(100)], 300),
                            _page([_item(f"b{i}") for i in range(100)], 300),
                            _page([_item(f"c{i}") for i in range(100)], 300)]}
        self._sync(full, categories=["running"], full=True)
        PelotonClass.objects.update(first_seen_at=timezone.now() - timedelta(days=1))
        client, result = self._sync(full, categories=["running"])
        self.assertEqual(client.calls, [("running", 0)])
        self.assertEqual(result["categories"]["running"]["created"], 0)

    def test_full_sync_marks_vanished_classes_unavailable(self):
        self._sync({"running": [_page([_item("keep"), _item("gone")], 2)]}, categories=["running"], full=True)
        PelotonClass.objects.update(last_seen_at=timezone.now() - timedelta(days=1))
        self._sync({"running": [_page([_item("keep")], 1)]}, categories=["running"], full=True)
        self.assertTrue(PelotonClass.objects.get(pk="keep").is_available)
        self.assertFalse(PelotonClass.objects.get(pk="gone").is_available)

    def test_category_that_errors_midway_marks_nothing(self):
        self._sync({"running": [_page([_item("old")], 1)]}, categories=["running"], full=True)
        PelotonClass.objects.update(last_seen_at=timezone.now() - timedelta(days=1))
        pages = {"running": [_page([_item(f"a{i}") for i in range(100)], 200)],
                 "walking": [_page([dict(_item("w1"), fitness_discipline="walking")], 1)]}
        _, result = self._sync(pages, fail={("running", 1)}, categories=["running", "walking"], full=True)
        self.assertIn("boom", result["categories"]["running"]["error"])
        self.assertTrue(PelotonClass.objects.get(pk="old").is_available)
        self.assertTrue(PelotonClass.objects.filter(pk="w1").exists())    # next category still ran


class CatalogAccessTests(TwoUserTestCase):
    def test_owner_can_start_refresh(self):
        with patch("workouts.onboarding_views.start_backfill") as start:
            resp = self.client_a.post(reverse("catalog_sync_start"))
        self.assertEqual(resp.status_code, 200)
        start.assert_called_once_with(self.a, "catalog")

    def test_regular_user_gets_403(self):
        with patch("workouts.onboarding_views.start_backfill") as start:
            self.assertEqual(self.client_b.post(reverse("catalog_sync_start")).status_code, 403)
            self.assertEqual(self.client_b.get(reverse("gs_sync_status", args=["catalog"])).status_code, 403)
            self.assertEqual(self.client_b.post(reverse("gs_retry", args=["catalog"])).status_code, 403)
        start.assert_not_called()

    def test_owner_status_partial(self):
        SyncJob.objects.create(user=self.a, source="catalog", status="done", summary={"total_classes": 42000},
                               finished_at=timezone.now())
        resp = self.client_a.get(reverse("gs_sync_status", args=["catalog"]))
        self.assertContains(resp, "42000 classes")

    def test_integrations_card_owner_only(self):
        self.assertContains(self.client_a.get(reverse("integrations_settings")), "Refresh catalog")
        self.assertNotContains(self.client_b.get(reverse("integrations_settings")), "Refresh catalog")


class DifficultyInContextTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        from workouts.models import PelotonClassType
        PelotonClassType.objects.create(id="t1", name="Endurance", discipline="running")
        now = timezone.now()
        for i, d in enumerate([5.0, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0]):
            PelotonClass.objects.create(ride_id=f"r{i}", title=f"30 min Endurance {i}", discipline="running",
                                        class_type_id="t1", duration_seconds=1800, difficulty_estimate=d,
                                        original_air_time=now, last_seen_at=now)

    def test_phrase_harder_and_easier(self):
        from workouts.catalog import DifficultyRanker
        r = DifficultyRanker()
        hard = r.rank(PelotonClass.objects.get(pk="r8"))       # 9.5: 8 of 10 below
        self.assertEqual(hard["phrase"], "harder than 80% of 30-min Endurance classes")
        easy = r.rank(PelotonClass.objects.get(pk="r1"))       # 6.0: 8 of 10 above
        self.assertEqual(easy["phrase"], "easier than 80% of 30-min Endurance classes")

    def test_too_few_comparable_classes_shows_number_only(self):
        from workouts.catalog import DifficultyRanker
        PelotonClass.objects.filter(pk__in=["r0", "r1", "r2", "r3", "r4", "r5"]).update(is_available=False)
        info = DifficultyRanker().rank(PelotonClass.objects.get(pk="r8"))
        self.assertEqual((info["difficulty"], info["phrase"]), (9.5, ""))

    def test_workout_not_in_catalog_uses_its_own_number(self):
        from workouts.catalog import DifficultyRanker
        from workouts.models import CachedWorkout
        w = CachedWorkout(user=self.a, workout_id="w", ride_id="unknown", difficulty_estimate=6.2)
        DifficultyRanker().annotate_workouts([w])
        self.assertEqual((w.difficulty_info["difficulty"], w.difficulty_info["phrase"]), (6.2, ""))

    def test_effort_per_min(self):
        from workouts.models import CachedWorkout
        w = CachedWorkout(duration_seconds=1800, performance_graph_json={"effort_zones": {"total_effort_points": 42}})
        self.assertEqual(w.effort_per_min, 1.4)
        self.assertIsNone(CachedWorkout(duration_seconds=1800).effort_per_min)
        w.duration_seconds = 120
        self.assertIsNone(w.effort_per_min)

    def test_detail_and_history_show_it(self):
        from workouts.models import CachedWorkout
        CachedWorkout.objects.create(
            user=self.a, workout_id="wk1", ride_id="r8", title="30 min Endurance 8", discipline="running",
            source="peloton", created_at=timezone.now() - timedelta(days=1), duration_seconds=1800,
            difficulty_estimate=9.5, performance_graph_json={"effort_zones": {"total_effort_points": 42}})
        detail = self.client_a.get(reverse("workout_detail", args=["wk1"]))
        self.assertContains(detail, "harder than 80% of 30-min Endurance classes")
        self.assertContains(detail, "1.4 pts/min")
        history = self.client_a.get(reverse("history"))
        self.assertContains(history, "harder than 80%")
        self.assertContains(history, "Effort/min")


class WeeklyFullSyncTests(TwoUserTestCase):
    """sync_daily runs a full catalog sync for the owner when the last good one is
    a week old (or there isn't one), otherwise incremental."""

    def setUp(self):
        super().setUp()
        from workouts.models import Integration
        PelotonAuth.objects.create(user=self.a, peloton_user_id="pa", refresh_token="r")
        Integration.ensure_for_user(self.a)
        Integration.objects.filter(user=self.a).exclude(key="peloton").update(is_enabled=False)

    def run_daily(self, *args, catalog_result=None):
        from io import StringIO
        from django.core.management import call_command
        result = catalog_result or {"categories": {"running": {"pages": 1, "created": 2, "updated": 0,
                                                               "skipped_unavailable": 0, "error": ""}},
                                    "class_types": 1, "total_classes": 100}
        out = StringIO()
        with patch("workouts.management.commands.sync_daily._run_peloton_sync_new",
                   return_value={"done": True, "created": 0, "updated": 0}), \
             patch("workouts.catalog.sync_catalog", return_value=result) as sync:
            try:
                call_command("sync_daily", "--user", "alice", *args, stdout=out)
            except SystemExit:
                pass
        return sync, out.getvalue()

    def done_job(self, days_ago):
        return SyncJob.objects.create(user=self.a, source="catalog", status="done",
                                      finished_at=timezone.now() - timedelta(days=days_ago))

    def test_no_full_sync_yet_runs_full_and_records_a_job(self):
        sync, out = self.run_daily()
        self.assertTrue(sync.call_args.kwargs["full"])
        self.assertIn("weekly full sync", out)
        self.assertEqual(SyncJob.objects.get(user=self.a, source="catalog").status, "done")

    def test_recent_full_sync_runs_incremental(self):
        self.done_job(days_ago=3)
        sync, out = self.run_daily()
        self.assertFalse(sync.call_args.kwargs["full"])
        self.assertEqual(SyncJob.objects.filter(source="catalog").count(), 1)

    def test_week_old_full_sync_runs_full(self):
        self.done_job(days_ago=7)
        sync, _ = self.run_daily()
        self.assertTrue(sync.call_args.kwargs["full"])

    def test_flag_forces_full(self):
        self.done_job(days_ago=1)
        sync, _ = self.run_daily("--catalog-full")
        self.assertTrue(sync.call_args.kwargs["full"])

    def test_partial_failure_marks_job_failed_so_next_run_retries(self):
        bad = {"categories": {"running": {"pages": 3, "created": 0, "updated": 0, "skipped_unavailable": 0,
                                          "error": "boom"}},
               "class_types": 1, "total_classes": 100, "error": "running: boom"}
        self.run_daily(catalog_result=bad)
        self.assertEqual(SyncJob.objects.get(user=self.a, source="catalog").status, "failed")
        from workouts.catalog import full_sync_due
        self.assertTrue(full_sync_due(self.a))

    def test_skipped_while_the_button_job_is_running(self):
        SyncJob.objects.create(user=self.a, source="catalog", status="running")
        sync, out = self.run_daily()
        sync.assert_not_called()
        self.assertIn("already running", out)

    def test_sync_catalog_reports_top_level_error(self):
        client = FakeClient({"running": [_page([_item(f"a{i}") for i in range(100)], 200)]}, fail={("running", 1)})
        with patch("workouts.catalog.PelotonClient", return_value=client):
            result = sync_catalog(self.a, categories=["running"], full=True, pause=0)
        self.assertIn("running: boom", result["error"])
