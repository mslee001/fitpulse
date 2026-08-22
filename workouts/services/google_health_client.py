"""
GoogleHealthClient — OAuth 2.0 client for the Google Health API.

Modeled on WithingsClient: tokens stored in the GoogleHealthAuth DB singleton
(pk=1), auto-refreshed 5 minutes before expiry, single refresh-and-retry on a
401. Run `google_health_login` to do a fresh OAuth flow.

Credentials from environment variables:
    GOOGLE_HEALTH_CLIENT_ID
    GOOGLE_HEALTH_CLIENT_SECRET
    GOOGLE_HEALTH_REDIRECT_URI

Data-type paths, actions (list vs. dailyRollUp), and response shapes below
were all confirmed against live payloads from a real Pixel Watch 3 account
(2026-08-17) — not guessed from docs. Two things the docs got wrong or left
ambiguous, worth remembering if this needs revisiting:
  - dailyRollUp is POST {base}/dataTypes/{type}/dataPoints:dailyRollUp with a
    JSON body `{"range": {"start": {"date": {"year","month","day"}}, "end": {...}}}`
    — NOT a GET with startTime/endTime query params.
  - The `list` endpoint's `filter` query param uses an undocumented-in-practice
    grammar that rejected every startTime/endTime/date filter expression we
    tried (see git history if you want the failed attempts). We fetch
    unfiltered + paginate + let the caller stop once results are older than
    the requested range, rather than fight that grammar further.
Per-data-type action support (which data types 400 on the "wrong" action) is
itself something Google's error responses tell you directly — e.g. a 400 on
`list` for an unsupported type includes "the following actions are
supported: rollup, dailyRollup" in the message. Trust that over assumption.
"""

import logging
import os
import time
from datetime import date as date_cls, datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://health.googleapis.com/v4/users/me/"
TOKEN_URL = "https://oauth2.googleapis.com/token"

LIST = "list"
ROLLUP = "dailyRollUp"

# (path segment, action) per method below — confirmed against live payloads.
DATA_TYPES = {
    "resting_heart_rate": ("daily-resting-heart-rate", LIST),
    "heart_rate_variability": ("heart-rate-variability", LIST),
    "daily_heart_rate_variability": ("daily-heart-rate-variability", LIST),
    "daily_vo2_max": ("daily-vo2-max", LIST),
    "run_vo2_max": ("run-vo2-max", LIST),
    "daily_respiratory_rate": ("daily-respiratory-rate", LIST),
    "respiratory_rate_sleep_summary": ("respiratory-rate-sleep-summary", LIST),
    "oxygen_saturation": ("oxygen-saturation", LIST),
    "daily_oxygen_saturation": ("daily-oxygen-saturation", LIST),
    "sleep": ("sleep", LIST),
    "exercise": ("exercise", LIST),
    "steps": ("steps", ROLLUP),
    "floors": ("floors", ROLLUP),
    "active_energy_burned": ("active-energy-burned", ROLLUP),
    "total_calories": ("total-calories", ROLLUP),
    "active_minutes": ("active-minutes", ROLLUP),
    "active_zone_minutes": ("active-zone-minutes", ROLLUP),
    "daily_sleep_temperature_derivations": ("daily-sleep-temperature-derivations", LIST),
    "sedentary_period": ("sedentary-period", LIST),
    "time_in_heart_rate_zone": ("time-in-heart-rate-zone", LIST),
    "height": ("height", LIST),
}

# dailyRollUp query-range caps in days, per file 02's spec (confirmed live via
# the INVALID_ROLLUP_QUERY_DURATION error on an out-of-range request).
ROLLUP_MAX_DAYS = {
    "calories-in-heart-rate-zone": 14,
    "heart-rate": 14,
    "active-minutes": 14,
    "total-calories": 14,
}
DEFAULT_ROLLUP_MAX_DAYS = 90

# exercise and sleep are capped at 25 results/page; everything else ~10,000.
LIST_PAGE_SIZE = {
    "exercise": 25,
    "sleep": 25,
}
DEFAULT_LIST_PAGE_SIZE = 1000
# Safety cap on pages fetched per _list() call, so a date-extraction bug that
# fails to trigger the early-stop can't paginate forever.
MAX_LIST_PAGES = 200

# Peloton's registered Fitbit Web API client — confirmed live 2026-08-17 by
# matching a Google Health exercise entry's dataSource.application.webClientId
# against a known CachedWorkout(source="peloton") at the exact same timestamp.
# Corroborated 100% (369/369 sampled) by exercise.displayName starting with
# "Peloton - ". NOT dataSource.application.packageName — that field doesn't
# exist in the real payload.
PELOTON_FITBIT_WEB_CLIENT_ID = "227TGP"

# ExerciseType -> CachedWorkout.discipline. Not exhaustive (the real enum has
# 160+ values, see https://developers.google.com/health/reference/rest/v4/ExerciseType)
# — covers what a Peloton user's watch is plausibly going to log. Anything
# unmapped falls back to the lowercased raw type, same as GarminClient's
# GARMIN_SPORT_TO_DISCIPLINE.get(sport_key, sport_key) — it just routes to the
# generic detail template rather than crashing.
GOOGLE_HEALTH_EXERCISE_TYPE_TO_DISCIPLINE = {
    "RUNNING": "running", "TRAIL_RUN": "running", "TREADMILL": "running",
    "INCLINE_RUN": "running", "TRACK_AND_FIELD": "running",
    "WALKING": "walking", "TREADMILL_WALK": "walking", "POWER_WALKING": "walking",
    "NORDIC_WALKING": "walking", "INCLINE_WALK": "walking", "HIKING": "walking",
    "STROLLER_WALK": "walking", "WALK_WITH_WEIGHTS": "walking", "RUCKING": "walking",
    "BIKING": "cycling", "OUTDOOR_BIKE": "cycling", "STATIONARY_BIKE": "cycling",
    "SPINNING": "cycling", "MOUNTAIN_BIKE": "cycling", "ELECTRIC_BIKE": "cycling",
    "ASSAULT_BIKE": "cycling", "HAND_CYCLING": "cycling",
    "STRENGTH_TRAINING": "strength", "WEIGHTLIFTING": "strength", "WEIGHT_MACHINES": "strength",
    "WEIGHTS": "strength", "FREE_WEIGHTS": "strength", "FUNCTIONAL_STRENGTH_TRAINING": "strength",
    "CIRCUIT_TRAINING": "strength", "CROSSFIT": "strength", "POWERLIFTING": "strength",
    "BODY_WEIGHT": "strength", "CALISTHENICS": "strength", "CORE_TRAINING": "strength",
    "TRX": "strength", "RESISTANCE_BANDS": "strength",
    "YOGA": "yoga", "YOGA_HATHA": "yoga", "YOGA_VINYASA": "yoga",
    "YOGA_POWER": "yoga", "YOGA_BIKRAM": "yoga",
    "MEDITATE": "meditation",
    "STRETCHING": "stretching", "PILATES": "stretching", "BARRE_CLASS": "stretching",
    "ELLIPTICAL": "cardio", "STAIRCLIMBER": "cardio", "ROWING_MACHINE": "cardio",
    "ROWING": "cardio", "HIIT": "cardio", "TABATA_WORKOUT": "cardio",
    "INTERVAL_WORKOUT": "cardio", "CARDIO_WORKOUT": "cardio", "AEROBIC_WORKOUT": "cardio",
    "CARDIO_SCULPT": "cardio", "JUMPING_ROPE": "cardio", "STEP_TRAINING": "cardio",
    "BOOTCAMP": "cardio", "EXERCISE_CLASS": "cardio", "WORKOUT": "cardio",
    "CROSS_TRAINING": "cardio", "OUTDOOR_WORKOUT": "cardio",
    "SWIMMING": "cardio", "SWIMMING_POOL": "cardio", "SWIMMING_OPEN_WATER": "cardio",
    "WATER_AEROBICS": "cardio", "WATER_JOGGING": "cardio",
}


class GoogleHealthReauthRequired(Exception):
    """Raised when the refresh token itself is rejected (e.g. expired after
    7 days under Testing publishing status). Callers should log an
    actionable message telling the developer to re-run google_health_login."""
    pass


class GoogleHealthClient:
    def __init__(self):
        self.client_id = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "")
        self.client_secret = os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "")
        self.redirect_uri = os.environ.get("GOOGLE_HEALTH_REDIRECT_URI", "")
        self._tokens: dict = {}

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _load_tokens(self) -> None:
        """Load tokens from the GoogleHealthAuth DB singleton into self._tokens."""
        from workouts.models import GoogleHealthAuth
        auth = GoogleHealthAuth.get()
        if not auth:
            raise RuntimeError(
                "No Google Health credentials in DB. "
                "Run: venv/bin/python3 manage.py google_health_login"
            )
        self._tokens = {
            "access_token": auth.access_token,
            "refresh_token": auth.refresh_token,
            "expires_at": int(auth.token_expires_at.timestamp()),
            "scopes": auth.scopes,
        }

    def _save_tokens(self) -> None:
        """Persist tokens to the GoogleHealthAuth DB singleton."""
        from workouts.models import GoogleHealthAuth
        expires_at = datetime.fromtimestamp(self._tokens["expires_at"], tz=timezone.utc)
        GoogleHealthAuth.objects.update_or_create(
            pk=1,
            defaults={
                "access_token": self._tokens["access_token"],
                "refresh_token": self._tokens["refresh_token"],
                "token_expires_at": expires_at,
                "scopes": self._tokens.get("scopes", ""),
            },
        )

    def _ensure_token_valid(self) -> None:
        """Load from DB if needed, then auto-refresh if within 5 minutes of expiry."""
        if not self._tokens.get("access_token"):
            self._load_tokens()
        if time.time() >= self._tokens.get("expires_at", 0) - 300:
            self.refresh_tokens()

    # ── OAuth helpers ─────────────────────────────────────────────────────────

    def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for tokens and save to DB."""
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Google Health token exchange failed ({resp.status_code}): {resp.text}")
        token_data = resp.json()
        self._tokens = {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", self._tokens.get("refresh_token")),
            "expires_at": int(time.time()) + int(token_data.get("expires_in", 3600)),
            "scopes": token_data.get("scope", ""),
        }
        if not self._tokens["refresh_token"]:
            raise RuntimeError(
                "Google Health token exchange returned no refresh_token. "
                "Retry with prompt=consent and access_type=offline in the authorization URL."
            )
        self._save_tokens()
        return self._tokens

    def refresh_tokens(self) -> None:
        """Refresh the access token using the stored refresh token."""
        if not self._tokens.get("refresh_token"):
            self._load_tokens()
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._tokens["refresh_token"],
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        if resp.status_code != 200:
            raise GoogleHealthReauthRequired(
                f"Google Health refresh token rejected ({resp.status_code}): {resp.text}. "
                "Re-run: venv/bin/python3 manage.py google_health_login"
            )
        token_data = resp.json()
        self._tokens["access_token"] = token_data["access_token"]
        self._tokens["expires_at"] = int(time.time()) + int(token_data.get("expires_in", 3600))
        # Google usually omits refresh_token on refresh responses — keep the existing one.
        if token_data.get("refresh_token"):
            self._tokens["refresh_token"] = token_data["refresh_token"]
        self._save_tokens()
        logger.info("Google Health tokens refreshed successfully")

    # ── API requests ──────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
        """Make an authenticated request to the Google Health API. Retries once on 401."""
        self._ensure_token_valid()
        url = BASE_URL + path

        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._tokens['access_token']}"}
            resp = requests.request(method, url, headers=headers, params=params, json=json_body)
            if resp.status_code == 401 and attempt == 0:
                logger.info("Google Health 401 — refreshing tokens and retrying")
                self.refresh_tokens()
                continue
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()
        raise RuntimeError("Google Health request failed after token refresh")

    @staticmethod
    def _chunked_date_range(start, end, max_days: int) -> list[tuple]:
        """Split [start, end] (date objects, inclusive) into <= max_days chunks."""
        if start > end:
            raise ValueError(f"start ({start}) is after end ({end})")
        chunks = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=max_days - 1), end)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
        return chunks

    def _rollup(self, data_type_key: str, start, end) -> list[dict]:
        """POST .../dataPoints:dailyRollUp with a CivilDateTime range body,
        chunking per the type's documented query-range cap.

        IMPORTANT: range.end is EXCLUSIVE (confirmed live 2026-08-17 two
        ways: a start==end request 400s with "end time must be strictly
        greater than start time", and a real multi-day request's last
        rollupDataPoint bucket was [end-1, end) — i.e. the calendar day
        equal to `end` itself never got its own bucket). So the request
        body's end date must be chunk_end + 1 day to actually include
        chunk_end in the results.
        """
        path_segment, action = DATA_TYPES[data_type_key]
        assert action == ROLLUP, f"{data_type_key} does not support dailyRollUp"
        max_days = ROLLUP_MAX_DAYS.get(path_segment, DEFAULT_ROLLUP_MAX_DAYS)
        results: list[dict] = []
        for chunk_start, chunk_end in self._chunked_date_range(start, end, max_days):
            body = {
                "range": {
                    "start": {"date": _civil_date(chunk_start)},
                    "end": {"date": _civil_date(chunk_end + timedelta(days=1))},
                }
            }
            resp = self._request(
                "POST", f"dataTypes/{path_segment}/dataPoints:dailyRollUp", json_body=body
            )
            results.extend(resp.get("rollupDataPoints", []))
        return results

    def _list(self, data_type_key: str, start=None, end=None, extract_date=None) -> list[dict]:
        """
        GET .../dataPoints, paginating newest-first until exhausted or until
        `extract_date(point)` returns a date older than `start` (if given).
        The API's `filter` query param grammar rejected every date-range
        expression we tried live, so date-bounding happens client-side here
        instead of via query params.
        """
        path_segment, action = DATA_TYPES[data_type_key]
        assert action == LIST, f"{data_type_key} does not support list"
        page_size = LIST_PAGE_SIZE.get(path_segment, DEFAULT_LIST_PAGE_SIZE)
        results: list[dict] = []
        page_token = None
        for _ in range(MAX_LIST_PAGES):
            params = {"pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            body = self._request("GET", f"dataTypes/{path_segment}/dataPoints", params=params)
            points = body.get("dataPoints", [])
            if not points:
                break
            stop = False
            for point in points:
                d = extract_date(point) if extract_date else None
                if start is not None and d is not None and d < start:
                    stop = True
                    continue
                if end is not None and d is not None and d > end:
                    continue
                results.append(point)
            page_token = body.get("nextPageToken")
            if not page_token or stop:
                break
        else:
            logger.warning(
                "Google Health _list(%s) hit the %d-page safety cap without "
                "exhausting results — date range may be incomplete",
                data_type_key, MAX_LIST_PAGES,
            )
        return results

    # ── Per-data-type methods ────────────────────────────────────────────────
    # Each returns parsed Python data (list of dicts), date-bounded to
    # [start, end] (inclusive, date objects) where the type's shape allows it.

    def get_daily_resting_heart_rate(self, start, end) -> list[dict]:
        return self._list("resting_heart_rate", start, end, _extract_civil_date("dailyRestingHeartRate"))

    def get_heart_rate_variability(self, start, end) -> list[dict]:
        return self._list("heart_rate_variability", start, end, _extract_sample_time_date("heartRateVariability"))

    def get_daily_heart_rate_variability(self, start, end) -> list[dict]:
        return self._list("daily_heart_rate_variability", start, end, _extract_civil_date("dailyHeartRateVariability"))

    def get_daily_vo2_max(self, start, end) -> list[dict]:
        return self._list("daily_vo2_max", start, end, _extract_civil_date("dailyVo2Max"))

    def get_run_vo2_max(self, start, end) -> list[dict]:
        return self._list("run_vo2_max", start, end, _extract_sample_time_date("runVo2Max"))

    def get_daily_respiratory_rate(self, start, end) -> list[dict]:
        return self._list("daily_respiratory_rate", start, end, _extract_civil_date("dailyRespiratoryRate"))

    def get_respiratory_rate_sleep_summary(self, start, end) -> list[dict]:
        """Bonus data type (not in file 04's original mapping table) — gives a
        sleep-specific respiratory rate split (deep/light/REM/full-sleep
        breathsPerMinute) that daily-respiratory-rate alone doesn't provide."""
        return self._list(
            "respiratory_rate_sleep_summary", start, end,
            _extract_sample_time_date("respiratoryRateSleepSummary"),
        )

    def get_oxygen_saturation(self, start, end) -> list[dict]:
        return self._list("oxygen_saturation", start, end, _extract_sample_time_date("oxygenSaturation"))

    def get_daily_oxygen_saturation(self, start, end) -> list[dict]:
        return self._list("daily_oxygen_saturation", start, end, _extract_civil_date("dailyOxygenSaturation"))

    def get_sleep(self, start, end) -> list[dict]:
        return self._list("sleep", start, end, _extract_interval_start_date("sleep"))

    def get_exercise(self, start, end) -> list[dict]:
        return self._list("exercise", start, end, _extract_interval_start_date("exercise"))

    def get_steps_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("steps", start, end)

    def get_floors_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("floors", start, end)

    def get_active_energy_burned_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("active_energy_burned", start, end)

    def get_total_calories_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("total_calories", start, end)

    def get_active_minutes_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("active_minutes", start, end)

    def get_active_zone_minutes_daily_rollup(self, start, end) -> list[dict]:
        return self._rollup("active_zone_minutes", start, end)

    def get_daily_sleep_temperature_derivations(self, start, end) -> list[dict]:
        return self._list(
            "daily_sleep_temperature_derivations", start, end,
            _extract_civil_date("dailySleepTemperatureDerivations"),
        )

    def get_sedentary_period(self, start, end) -> list[dict]:
        return self._list("sedentary_period", start, end, _extract_interval_start_date("sedentaryPeriod"))

    def get_time_in_heart_rate_zone(self, start, end) -> list[dict]:
        return self._list("time_in_heart_rate_zone", start, end, _extract_interval_start_date("timeInHeartRateZone"))

    def get_height(self, start, end) -> list[dict]:
        return self._list("height", start, end, _extract_sample_time_date("height"))


def _civil_date(d: date_cls) -> dict:
    """date -> the {year, month, day} shape the dailyRollUp CivilDateTime body wants."""
    return {"year": d.year, "month": d.month, "day": d.day}


def _extract_civil_date(field_key: str):
    """Data types shaped like dailyRestingHeartRate.date = {year, month, day}."""
    def extract(point: dict):
        d = point.get(field_key, {}).get("date")
        if not d:
            return None
        return date_cls(d["year"], d["month"], d["day"])
    return extract


def _extract_sample_time_date(field_key: str):
    """Data types shaped like heartRateVariability.sampleTime.civilTime.date."""
    def extract(point: dict):
        d = point.get(field_key, {}).get("sampleTime", {}).get("civilTime", {}).get("date")
        if not d:
            return None
        return date_cls(d["year"], d["month"], d["day"])
    return extract


def _extract_interval_start_date(field_key: str):
    """Data types shaped like sleep.interval.startTime = RFC3339 string."""
    def extract(point: dict):
        start = point.get(field_key, {}).get("interval", {}).get("startTime")
        if not start:
            return None
        return datetime.fromisoformat(start.replace("Z", "+00:00")).date()
    return extract
