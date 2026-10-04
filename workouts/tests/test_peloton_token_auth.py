"""Peloton Auth0 refresh tokens: connect spends the pasted token, PelotonClient
refreshes near expiry and saves every rotated token, 401 retries once, and a
rejected token marks the connection for reconnecting. No real network."""
import logging
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from workouts.models import Integration, PelotonAuth, WebhookError
from workouts.services.peloton_client import PelotonAuthError, PelotonClient, PelotonNetworkError
from workouts.tests.helpers import TwoUserTestCase

import requests


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.ok = status < 400

    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"{status}")
    r.raise_for_status.side_effect = raise_for_status
    return r


AUTH0_OK = {"access_token": "A1", "refresh_token": "R2", "expires_in": 172800, "id_token": "x",
            "scope": "openid profile email offline_access", "token_type": "Bearer"}


class ConnectTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        patch("workouts.background.start_backfill").start()
        self.addCleanup(patch.stopall)

    def _post(self, client, token="R1"):
        return client.post(reverse("set_peloton_auth"), {"refresh_token": token})

    @patch("workouts.services.peloton_client.requests.get", return_value=_resp(200, {"id": "pid", "username": "me"}))
    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_connect_spends_pasted_token_and_stores_rotated_one(self, post, get):
        self._post(self.client_b)
        auth = PelotonAuth.objects.get(user=self.b)
        self.assertEqual(auth.refresh_token, "R2")
        self.assertEqual(auth.access_token, "A1")
        self.assertIsNotNone(auth.connected_at)
        self.assertIsNotNone(auth.access_expires_at)
        self.assertEqual(post.call_args.kwargs["json"]["refresh_token"], "R1")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer A1")

    @patch("workouts.services.peloton_client.requests.get", return_value=_resp(200, {"id": "pid", "username": "me"}))
    @patch("workouts.services.peloton_client.requests.post")
    def test_no_rotation_keeps_pasted_token(self, post, get):
        post.return_value = _resp(200, {k: v for k, v in AUTH0_OK.items() if k != "refresh_token"})
        self._post(self.client_b, '"R1"')   # JSON-quoted paste is cleaned up
        self.assertEqual(PelotonAuth.objects.get(user=self.b).refresh_token, "R1")

    @patch("workouts.services.peloton_client.requests.get", return_value=_resp(200, {"id": "p-owner", "username": "owner"}))
    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_taken_account_names_it_and_saves_nothing(self, post, get):
        PelotonAuth.objects.create(user=self.a, refresh_token="x", peloton_user_id="p-owner")
        resp = self._post(self.client_b)
        self.assertFalse(PelotonAuth.objects.filter(user=self.b).exists())
        msg = " ".join(str(m) for m in get_messages(resp.wsgi_request))
        self.assertIn("@owner", msg)

    @patch("workouts.services.peloton_client.requests.get", return_value=_resp(401))
    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_failure_after_exchange_says_token_is_spent(self, post, get):
        resp = self._post(self.client_b)
        msg = " ".join(str(m) for m in get_messages(resp.wsgi_request))
        self.assertIn("copy a fresh one", msg)
        self.assertFalse(PelotonAuth.objects.filter(user=self.b).exists())

    @patch("workouts.services.peloton_client.requests.get", return_value=_resp(200, {"id": "pid", "username": "me"}))
    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_no_token_values_in_messages_or_logs(self, post, get):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("workouts").debug("marker")
            resp = self._post(self.client_b)
        text = " ".join(str(m) for m in get_messages(resp.wsgi_request)) + " ".join(logs.output)
        for secret in ("R1", "R2", "A1"):
            self.assertNotIn(secret, text)
        page = self.client_b.get(reverse("integrations_settings")).content.decode()
        for secret in ("R2", "A1"):
            self.assertNotIn(f">{secret}<", page)
            self.assertNotIn(f'"{secret}"', page)


class ClientRefreshTests(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        self.auth = PelotonAuth.objects.create(
            user=self.b, peloton_user_id="pid", refresh_token="R1",
            access_token="OLD", access_expires_at=timezone.now() + timedelta(hours=10))

    def _stale(self):
        PelotonAuth.objects.filter(pk=self.auth.pk).update(access_expires_at=timezone.now() + timedelta(minutes=5))

    def test_no_row_or_no_token_raises(self):
        PelotonAuth.objects.filter(pk=self.auth.pk).update(refresh_token="")
        with self.assertRaises(PelotonAuthError):
            PelotonClient(self.b)

    @patch("workouts.services.peloton_client.requests.post")
    @patch("workouts.services.peloton_client.requests.Session.get", return_value=_resp(200, {"ok": 1}))
    def test_fresh_token_makes_no_refresh_call(self, get, post):
        PelotonClient(self.b)._get("/api/me")
        post.assert_not_called()
        get.assert_called_once()

    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_bearer_header_sent(self, post):
        c = PelotonClient(self.b)
        with patch.object(c.session, "get", return_value=_resp(200, {})) as get:
            c._get("/api/me")
        self.assertEqual(c.session.headers["Authorization"], "Bearer OLD")
        self.assertEqual(c.session.headers["peloton-platform"], "web")
        self.assertNotIn("peloton_session_id", c.session.cookies)
        post.assert_not_called()
        self.assertEqual(get.call_args.kwargs["timeout"], 30)

    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_near_expiry_refreshes_once_and_saves_rotated_token(self, post):
        self._stale()
        c = PelotonClient(self.b)
        with patch.object(c.session, "get", return_value=_resp(200, {})):
            c._get("/api/me")
            c._get("/api/me")
        self.assertEqual(post.call_count, 1)
        self.auth.refresh_from_db()
        self.assertEqual((self.auth.refresh_token, self.auth.access_token), ("R2", "A1"))
        self.assertIsNotNone(self.auth.refresh_rotated_at)
        self.assertEqual(c.session.headers["Authorization"], "Bearer A1")

    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_401_forces_one_refresh_and_retries(self, post):
        c = PelotonClient(self.b)
        with patch.object(c.session, "get", side_effect=[_resp(401), _resp(200, {"ok": 1})]):
            self.assertEqual(c._get("/api/me"), {"ok": 1})
        self.assertEqual(post.call_count, 1)

    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK))
    def test_two_401s_raise_auth_error(self, post):
        c = PelotonClient(self.b)
        with patch.object(c.session, "get", side_effect=[_resp(401), _resp(401)]):
            with self.assertRaises(PelotonAuthError):
                c._get("/api/me")

    @patch("workouts.services.peloton_client.requests.post",
           return_value=_resp(400, {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."}))
    def test_invalid_grant_marks_reconnect(self, post):
        self._stale()
        c = PelotonClient(self.b)
        with patch.object(c.session, "get") as get:
            with self.assertRaises(PelotonAuthError):
                c._get("/api/me")
            get.assert_not_called()
        self.auth.refresh_from_db()
        self.assertIsNotNone(self.auth.auth_failed_at)
        self.assertTrue(self.auth.needs_reconnect)
        self.assertEqual(self.auth.refresh_token, "R1")     # not blanked
        self.assertTrue(WebhookError.objects.filter(source="peloton_auth", user=self.b).exists())
        # …and the banner shows while Peloton is enabled
        Integration.ensure_for_user(self.b)
        self.assertIn("Peloton needs reconnecting", self.client_b.get("/").content.decode())

    @patch("workouts.services.peloton_client.requests.post", side_effect=requests.ConnectionError("down"))
    def test_network_error_does_not_mark_failed(self, post):
        self._stale()
        c = PelotonClient(self.b)
        with self.assertRaises(PelotonNetworkError):
            c._get("/api/me")
        self.auth.refresh_from_db()
        self.assertIsNone(self.auth.auth_failed_at)
        self.assertFalse(WebhookError.objects.filter(source="peloton_auth").exists())

    @patch("workouts.services.peloton_client.requests.post", return_value=_resp(503))
    def test_auth0_5xx_is_network_error(self, post):
        with self.assertRaises(PelotonNetworkError):
            PelotonClient.exchange_refresh_token("R1")

    def test_double_checked_locking_rereads_the_row(self):
        self._stale()
        c = PelotonClient(self.b)                 # holds the stale in-memory copy
        PelotonAuth.objects.filter(pk=self.auth.pk).update(       # another thread refreshed meanwhile
            access_token="NEW", access_expires_at=timezone.now() + timedelta(hours=40))
        with patch.object(PelotonClient, "exchange_refresh_token",
                          side_effect=AssertionError("must not spend the refresh token")):
            c._ensure_token()
        self.assertEqual(c.session.headers["Authorization"], "Bearer NEW")

    def test_reconnect_clears_failure(self):
        PelotonAuth.objects.filter(pk=self.auth.pk).update(auth_failed_at=timezone.now(), auth_error="x")
        with patch("workouts.services.peloton_client.requests.post", return_value=_resp(200, AUTH0_OK)), \
             patch("workouts.services.peloton_client.requests.get",
                   return_value=_resp(200, {"id": "pid", "username": "me"})), \
             patch("workouts.background.start_backfill"):
            self.client_b.post(reverse("set_peloton_auth"), {"refresh_token": "R9"})
        self.auth.refresh_from_db()
        self.assertIsNone(self.auth.auth_failed_at)
        self.assertFalse(self.auth.needs_reconnect)
