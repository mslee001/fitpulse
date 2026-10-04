"""AI calls run for a named user and feature: usage is logged with cost, budgets
are enforced before any request goes out, and chat tools only see the caller's data."""
import json
from datetime import date, datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import MagicMock, patch

from workouts import llm
from workouts.access import FEATURES
from workouts.models import AIUsage, CachedWorkout, Intervention
from workouts.services.chat_tools import build_tool_dispatch
from workouts.tests.helpers import TwoUserTestCase

USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500,
         "cache_creation_input_tokens": 100}


def response(body):
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def text_response(text="ok", model=llm.HAIKU):
    return response({"model": model, "content": [{"type": "text", "text": text}], "usage": USAGE,
                     "stop_reason": "end_turn"})


class AIUsageTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        from workouts.access import access_for
        access = access_for(self.b)
        access.features, access.ai_enabled = list(FEATURES), True
        access.save()

    def test_call_logs_usage_with_cost(self):
        with patch("workouts.llm.requests.post", return_value=text_response("hello")):
            self.assertEqual(llm.call("hi", user=self.a, feature="ai_day_analysis"), "hello")
        row = AIUsage.objects.get()
        self.assertEqual((row.user, row.feature, row.model), (self.a, "ai_day_analysis", llm.HAIKU))
        self.assertEqual((row.input_tokens, row.output_tokens, row.cache_read_tokens, row.cache_write_tokens),
                         (1000, 200, 500, 100))
        # Haiku: 1000×$1 + 200×$5 + 100×$1.25 + 500×$0.10 per million
        self.assertEqual(row.cost_usd, Decimal("0.002175"))

    def test_user_and_feature_are_required(self):
        with self.assertRaises(TypeError):
            llm.call("hi", feature="ai_day_analysis")
        with self.assertRaises(TypeError):
            llm.call("hi", user=self.a)
        with self.assertRaises(TypeError):
            llm.call_raw({}, user=self.a)
        with self.assertRaises(TypeError):
            llm.submit_batch("x", "hi", user=self.a)

    def test_budget_blocks_before_any_request(self):
        AIUsage.objects.create(user=self.b, feature="ai_chat", model=llm.HAIKU, cost_usd=Decimal("0.02"))
        with patch("workouts.llm._monthly_budget", return_value=Decimal("0.01")), \
                patch("workouts.llm.requests.post") as post:
            with self.assertRaises(llm.AIBudgetExceeded):
                llm.call("hi", user=self.b, feature="ai_day_analysis")
            post.assert_not_called()

    def test_last_months_spend_does_not_count(self):
        old = AIUsage.objects.create(user=self.b, feature="ai_chat", model=llm.HAIKU, cost_usd=Decimal("5"))
        AIUsage.objects.filter(pk=old.pk).update(created_at=datetime(2020, 1, 1, tzinfo=dt_tz.utc))
        with patch("workouts.llm._monthly_budget", return_value=Decimal("1.00")), \
                patch("workouts.llm.requests.post", return_value=text_response()):
            llm.call("hi", user=self.b, feature="ai_day_analysis")

    def test_superuser_is_never_capped(self):
        AIUsage.objects.create(user=self.a, feature="ai_chat", model=llm.HAIKU, cost_usd=Decimal("50"))
        with patch("workouts.llm._monthly_budget", return_value=Decimal("0.01")), \
                patch("workouts.llm.requests.post", return_value=text_response()):
            llm.call("hi", user=self.a, feature="ai_day_analysis")
        self.assertEqual(AIUsage.objects.filter(user=self.a).count(), 2)

    def test_unknown_model_is_priced_at_sonnet_rate(self):
        self.assertEqual(llm.cost_usd("mystery-model", {"input_tokens": 1_000_000}),
                         Decimal(str(llm.MODEL_PRICES[llm.SONNET][0])))

    def test_logging_failure_never_breaks_the_call(self):
        with patch("workouts.llm.requests.post", return_value=text_response("fine")), \
                patch("workouts.models.AIUsage.objects.create", side_effect=RuntimeError("db down")):
            self.assertEqual(llm.call("hi", user=self.a, feature="ai_day_analysis"), "fine")

    def test_batch_custom_id_carries_user_and_feature_and_result_logs_to_them(self):
        with patch("workouts.llm.requests.post", return_value=response({"id": "batch_1"})) as post:
            llm.submit_batch("weekly_review", "prompt", user=self.b, feature="ai_weekly_review")
        custom_id = post.call_args.kwargs["json"]["requests"][0]["custom_id"]
        self.assertEqual(custom_id, f"u{self.b.id}-ai_weekly_review-weekly_review")
        self.assertEqual(AIUsage.objects.count(), 0)    # logged when the result is stored
        llm.log_batch_result({"custom_id": custom_id, "result": {"type": "succeeded", "message": {
            "model": llm.SONNET, "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}})
        row = AIUsage.objects.get()
        self.assertEqual((row.user, row.feature, row.is_batch), (self.b, "ai_weekly_review", True))
        self.assertEqual(row.cost_usd, Decimal(str(llm.MODEL_PRICES[llm.SONNET][0] / 2)))


class ChatToolIsolationTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        CachedWorkout.objects.create(user=self.a, workout_id="a1", title="ALICE RIDE", discipline="cycling",
                                     created_at=datetime(2026, 9, 1, 12, tzinfo=dt_tz.utc), duration_seconds=1800)
        Intervention.objects.create(user=self.a, name="ALICE-MED", category="medication", start_date=date(2026, 8, 1))

    def test_tools_bound_to_b_see_none_of_a(self):
        tools = build_tool_dispatch(self.b)
        summary = tools["get_workout_summary"](start_date="2026-08-01", end_date="2026-09-30")
        self.assertNotIn("ALICE", json.dumps(summary, default=str))
        self.assertIn("cycling", json.dumps(build_tool_dispatch(self.a)["get_workout_summary"](
            start_date="2026-08-01", end_date="2026-09-30"), default=str))
        ctx = tools["get_intervention_context"](run_before_after=True, intervention_name="ALICE")
        self.assertEqual(ctx["error"], "not_found")
        self.assertNotIn("ALICE-MED", ctx["available"])

    def test_model_cannot_pick_the_user(self):
        # Tool input arrives as keyword arguments; a "user" key can't override the bound one.
        with self.assertRaises(TypeError):
            build_tool_dispatch(self.b)["get_workout_summary"](
                user=self.a, start_date="2026-08-01", end_date="2026-09-30")

    def test_run_stats_chat_only_calls_tools_bound_to_its_user(self):
        from workouts.access import access_for
        from workouts.ai import run_stats_chat
        access = access_for(self.b)
        access.features, access.ai_enabled = ["ai_chat"], True
        access.save()
        tool_round = response({"model": llm.SONNET, "stop_reason": "tool_use", "usage": USAGE, "content": [
            {"type": "tool_use", "id": "t1", "name": "get_workout_summary",
             "input": {"start_date": "2026-08-01", "end_date": "2026-09-30"}}]})
        final = text_response("done", model=llm.SONNET)
        with patch("workouts.llm.requests.post", side_effect=[tool_round, final]) as post:
            answer, history = run_stats_chat(self.b, {"page": "today", "today": "2026-10-01"}, [], "how many rides?")
        self.assertEqual(answer, "done")
        tool_result = json.loads(history[2]["content"][0]["content"])
        self.assertNotIn("ALICE", json.dumps(tool_result))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(list(AIUsage.objects.filter(feature="ai_chat").values_list("user", flat=True)),
                         [self.b.id, self.b.id])
