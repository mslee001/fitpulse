import time
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from workouts.services.google_health_client import (
    GoogleHealthClient,
    _civil_date,
    _extract_civil_date,
    _extract_interval_start_date,
    _extract_sample_time_date,
)


def _resp(status_code, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}" if json_body is not None else b""
    resp.json.return_value = json_body or {}
    resp.raise_for_status.side_effect = (
        None if status_code < 400 else Exception(f"HTTP {status_code}")
    )
    return resp


class RefreshOnceOn401Tests(SimpleTestCase):
    def setUp(self):
        self.client_obj = GoogleHealthClient(user=None)
        self.client_obj._tokens = {
            "access_token": "stale-token",
            "refresh_token": "refresh-token",
            "expires_at": int(time.time()) + 3600,  # not stale, so _ensure_token_valid won't preemptively refresh
            "scopes": "",
        }

    @patch.object(GoogleHealthClient, "_save_tokens", lambda self: None)
    @patch("workouts.services.google_health_client.requests.request")
    @patch("workouts.services.google_health_client.requests.post")
    def test_refreshes_and_retries_exactly_once(self, mock_post, mock_request):
        # First API call: 401, second (retry after refresh): 200
        mock_request.side_effect = [_resp(401), _resp(200, {"dataPoints": []})]
        # Token refresh call
        mock_post.return_value = _resp(200, {"access_token": "new-token", "expires_in": 3600})

        result = self.client_obj._request("GET", "dataTypes/steps/dataPoints", params={})

        self.assertEqual(result, {"dataPoints": []})
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(self.client_obj._tokens["access_token"], "new-token")

    @patch.object(GoogleHealthClient, "_save_tokens", lambda self: None)
    @patch("workouts.services.google_health_client.requests.request")
    @patch("workouts.services.google_health_client.requests.post")
    def test_does_not_loop_on_repeated_401(self, mock_post, mock_request):
        # Even after refresh, a second 401 should not trigger a second refresh attempt.
        mock_request.side_effect = [_resp(401), _resp(401)]
        mock_post.return_value = _resp(200, {"access_token": "new-token", "expires_in": 3600})

        with self.assertRaises(Exception):
            self.client_obj._request("GET", "dataTypes/steps/dataPoints", params={})

        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(mock_post.call_count, 1)

    @patch("workouts.services.google_health_client.requests.post")
    def test_rejected_refresh_raises_reauth_required(self, mock_post):
        from workouts.services.google_health_client import GoogleHealthReauthRequired
        mock_post.return_value = _resp(400, {"error": "invalid_grant"})

        with self.assertRaises(GoogleHealthReauthRequired):
            self.client_obj.refresh_tokens()


class ChunkedDateRangeTests(SimpleTestCase):
    def test_single_chunk_within_cap(self):
        chunks = GoogleHealthClient._chunked_date_range(
            date(2026, 1, 1), date(2026, 1, 10), max_days=14
        )
        self.assertEqual(chunks, [(date(2026, 1, 1), date(2026, 1, 10))])

    def test_exact_boundary_is_one_chunk(self):
        # 14-day cap, exactly 14 days inclusive
        chunks = GoogleHealthClient._chunked_date_range(
            date(2026, 1, 1), date(2026, 1, 14), max_days=14
        )
        self.assertEqual(chunks, [(date(2026, 1, 1), date(2026, 1, 14))])

    def test_one_day_over_boundary_splits(self):
        chunks = GoogleHealthClient._chunked_date_range(
            date(2026, 1, 1), date(2026, 1, 15), max_days=14
        )
        self.assertEqual(
            chunks,
            [(date(2026, 1, 1), date(2026, 1, 14)), (date(2026, 1, 15), date(2026, 1, 15))],
        )

    def test_90_day_boundary(self):
        start = date(2026, 1, 1)
        end = date(2026, 4, 15)  # 105 days inclusive
        chunks = GoogleHealthClient._chunked_date_range(start, end, max_days=90)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], start)
        # First chunk is exactly 90 days inclusive
        self.assertEqual((chunks[0][1] - chunks[0][0]).days, 89)
        self.assertEqual(chunks[1][0] - chunks[0][1], __import__("datetime").timedelta(days=1))
        self.assertEqual(chunks[-1][1], end)

    def test_span_covers_full_range_no_gaps_or_overlaps(self):
        start = date(2026, 1, 1)
        end = date(2026, 6, 30)
        chunks = GoogleHealthClient._chunked_date_range(start, end, max_days=14)
        # Every day in [start, end] covered exactly once
        covered = set()
        for c_start, c_end in chunks:
            d = c_start
            while d <= c_end:
                self.assertNotIn(d, covered)
                covered.add(d)
                d += __import__("datetime").timedelta(days=1)
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)


class DateExtractionTests(SimpleTestCase):
    """Real response shapes confirmed live against a Pixel Watch 3 account
    on 2026-08-17 — see the module docstring in google_health_client.py."""

    def test_civil_date_shape(self):
        self.assertEqual(_civil_date(date(2026, 8, 16)), {"year": 2026, "month": 8, "day": 16})

    def test_extract_civil_date(self):
        extract = _extract_civil_date("dailyRestingHeartRate")
        point = {"dailyRestingHeartRate": {"date": {"year": 2025, "month": 11, "day": 12}, "beatsPerMinute": "78"}}
        self.assertEqual(extract(point), date(2025, 11, 12))

    def test_extract_civil_date_missing_field_returns_none(self):
        extract = _extract_civil_date("dailyRestingHeartRate")
        self.assertIsNone(extract({}))

    def test_extract_sample_time_date(self):
        extract = _extract_sample_time_date("heartRateVariability")
        point = {
            "heartRateVariability": {
                "sampleTime": {
                    "physicalTime": "2025-11-12T15:15:00Z",
                    "civilTime": {"date": {"year": 2025, "month": 11, "day": 12}, "time": {"hours": 7, "minutes": 15}},
                },
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 14.1,
            }
        }
        self.assertEqual(extract(point), date(2025, 11, 12))

    def test_extract_interval_start_date(self):
        extract = _extract_interval_start_date("exercise")
        point = {"exercise": {"interval": {"startTime": "2026-04-21T22:22:54Z", "endTime": "2026-04-21T22:42:53Z"}}}
        self.assertEqual(extract(point), date(2026, 4, 21))


class ListDateBoundingTests(SimpleTestCase):
    """_list() paginates newest-first and stops once results fall before
    `start` — confirmed necessary live, since the API's `filter` query
    param rejected every date-range expression tried against it."""

    def setUp(self):
        self.client_obj = GoogleHealthClient(user=None)
        self.client_obj._tokens = {
            "access_token": "token", "refresh_token": "refresh",
            "expires_at": int(time.time()) + 3600, "scopes": "",
        }

    @staticmethod
    def _point(day):
        return {"dailyRestingHeartRate": {"date": {"year": 2026, "month": 8, "day": day}, "beatsPerMinute": "70"}}

    @patch("workouts.services.google_health_client.requests.request")
    def test_stops_paginating_once_past_start(self, mock_request):
        # Page 1: days 16 down to 10 (newest first) already runs past start=13,
        # so _list should stop after this one page and never fetch page 2.
        page1 = _resp(200, {"dataPoints": [self._point(d) for d in range(16, 9, -1)], "nextPageToken": "tok2"})
        page2 = _resp(200, {"dataPoints": [self._point(d) for d in range(9, 5, -1)]})
        mock_request.side_effect = [page1, page2]

        results = self.client_obj._list(
            "resting_heart_rate", date(2026, 8, 13), date(2026, 8, 16),
            extract_date=lambda p: date(2026, 8, p["dailyRestingHeartRate"]["date"]["day"]),
        )

        days = sorted(p["dailyRestingHeartRate"]["date"]["day"] for p in results)
        self.assertEqual(days, [13, 14, 15, 16])
        self.assertEqual(mock_request.call_count, 1)
