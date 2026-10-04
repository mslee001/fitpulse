"""Webhooks reach the right user: Withings by its userid, Google Health by fanning
out to every connected user (until a payload shows a per-user identifier)."""
import json
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import override_settings

from workouts.models import BodyMeasurement, GoogleHealthAuth, Integration, WebhookError, WithingsAuth
from workouts.sync import _process_google_health_notification
from workouts.tests.helpers import TwoUserTestCase

SECRET = "test-webhook-secret"
NOW = datetime.now(dt_tz.utc)


def measurement(grpid):
    return {"grpid": grpid, "measured_at": NOW - timedelta(hours=1), "weight_lb": 150.0, "raw": {}}


class WithingsWebhookTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        for u, userid in ((self.a, "111"), (self.b, "222")):
            Integration.ensure_for_user(u)
            WithingsAuth.objects.create(user=u, userid=userid, access_token="t", refresh_token="r",
                                        token_expires_at=NOW + timedelta(hours=3))

    def post(self, userid):
        return self.client.post("/api/withings/webhook/", {
            "userid": userid, "appli": "1",
            "startdate": int((NOW - timedelta(days=1)).timestamp()), "enddate": int(NOW.timestamp())})

    @patch("workouts.sync.WithingsClient")
    def test_routes_to_the_user_with_that_userid(self, client_cls):
        client_cls.return_value.get_measurements.return_value = [measurement("g-bob")]
        self.assertEqual(self.post("222").status_code, 200)
        client_cls.assert_called_once_with(self.b)
        self.assertEqual(list(BodyMeasurement.objects.values_list("user__username", flat=True)), ["bob"])
        self.assertTrue(WithingsAuth.objects.get(user=self.b).webhook_subscription_active)
        self.assertIsNone(WithingsAuth.objects.get(user=self.a).last_webhook_received_at)

    @patch("workouts.sync.WithingsClient")
    def test_unknown_userid_is_acknowledged_and_ignored(self, client_cls):
        self.assertEqual(self.post("999").status_code, 200)
        client_cls.assert_not_called()
        self.assertFalse(BodyMeasurement.objects.exists())

    @patch("workouts.sync.WithingsClient")
    def test_disabled_integration_is_acknowledged_and_ignored(self, client_cls):
        Integration.objects.filter(user=self.b, key="withings").update(is_enabled=False)
        self.assertEqual(self.post("222").status_code, 200)
        client_cls.assert_not_called()
        self.assertFalse(BodyMeasurement.objects.exists())

    def test_head_check_gets_200_without_touching_anything(self):
        # Withings HEADs the callback URL before registering a subscription.
        resp = self.client.head("/api/withings/webhook/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(BodyMeasurement.objects.exists())
        self.assertEqual(self.client.get("/api/withings/webhook/").status_code, 405)

    def test_two_users_cannot_share_a_withings_account(self):
        WithingsAuth.objects.filter(user=self.b).delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            WithingsAuth.objects.create(user=self.b, userid="111", access_token="t", refresh_token="r",
                                        token_expires_at=NOW)


@override_settings(GOOGLE_HEALTH_CAPTURE_PAYLOADS=True)
class GoogleHealthWebhookTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        for u in (self.a, self.b):
            Integration.ensure_for_user(u)
            GoogleHealthAuth.objects.create(user=u, access_token="t", refresh_token="r",
                                            token_expires_at=NOW + timedelta(hours=1))
        p = patch.dict("os.environ", {"GOOGLE_HEALTH_WEBHOOK_SECRET": SECRET})
        p.start()
        self.addCleanup(p.stop)
        # Run the background thread's body inline so assertions see its effects. The
        # thread target is _in_background(fn, ...); call fn directly — the wrapper
        # only closes the thread's own DB connection, which here is the test's.
        t = patch("workouts.sync.threading.Thread")
        self.thread = t.start()
        self.addCleanup(t.stop)
        self.thread.side_effect = lambda target, args, kwargs, daemon: type(
            "T", (), {"start": lambda _self: args[0](*args[1:], **kwargs)})()

    def notify(self, data_type="daily-resting-heart-rate"):
        payload = [{"data": {"dataType": data_type, "operation": "UPSERT", "intervals": [
            {"civilIso8601TimeInterval": {"startTime": "2026-10-01T00:00:00", "endTime": "2026-10-01T23:59:59"}}]}}]
        return self.client.post("/webhooks/google-health/", json.dumps(payload),
                                content_type="application/json", HTTP_AUTHORIZATION=SECRET)

    @patch("workouts.sync._run_google_health_wellness_sync", return_value={"done": True})
    def test_wellness_notification_syncs_every_connected_user(self, sync):
        self.assertEqual(self.notify().status_code, 204)
        self.assertEqual({c.args[0] for c in sync.call_args_list}, {self.a, self.b})
        self.assertEqual(self.thread.call_count, 1)    # one thread per kind, not per user

    @patch("workouts.sync._run_google_health_wellness_sync")
    def test_one_users_failure_does_not_stop_the_others(self, sync):
        def run(user, dates):
            if user == self.b:
                raise RuntimeError("refresh token expired")
            return {"done": True}
        sync.side_effect = run
        self.notify()
        self.assertEqual(sync.call_count, 2)
        errors = WebhookError.objects.filter(source="google_health")
        self.assertEqual([e.user for e in errors], [self.b])

    @patch("workouts.sync._run_google_health_exercise_sync", return_value={"done": True})
    def test_disabled_users_are_skipped(self, sync):
        Integration.objects.filter(user=self.b, key="google_health").update(is_enabled=False)
        self.notify("exercise")
        self.assertEqual([c.args[0] for c in sync.call_args_list], [self.a])

    @patch("workouts.sync._run_google_health_wellness_sync", return_value={"done": True})
    def test_payload_capture_stops_after_three(self, sync):
        for _ in range(5):
            self.notify()
        captured = WebhookError.objects.filter(source="google_health_payload")
        self.assertEqual(captured.count(), 3)
        self.assertIn("daily-resting-heart-rate", captured.first().detail)

    def test_unauthorized_notification_is_rejected_and_not_captured(self):
        resp = self.client.post("/webhooks/google-health/", "[]", content_type="application/json",
                                HTTP_AUTHORIZATION="wrong")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(WebhookError.objects.filter(source="google_health_payload").exists())

    @patch("workouts.sync._run_google_health_wellness_sync", return_value={"done": True})
    def test_process_function_directly(self, sync):
        _process_google_health_notification("wellness", [NOW.date()], data_types={"sleep"})
        self.assertEqual(sync.call_count, 2)
