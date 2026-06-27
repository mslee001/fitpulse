"""
WithingsClient — OAuth 2.0 client for the Withings Health API.

Tokens are stored in the WithingsAuth DB singleton (pk=1), shared between
the laptop and the hosted Render app. Run `migrate_withings_tokens` once to
seed from the old JSON file, or `withings_login` to do a fresh OAuth flow.

Credentials from environment variables:
    WITHINGS_CLIENT_ID
    WITHINGS_CLIENT_SECRET
    WITHINGS_REDIRECT_URI
"""

import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Measurement type codes
MEAS_WEIGHT = 1             # kg
MEAS_FAT_FREE = 5           # kg
MEAS_FAT_RATIO = 6          # % (no unit conversion needed)
MEAS_FAT_MASS = 8           # kg
MEAS_MUSCLE_MASS = 76       # kg
MEAS_HYDRATION = 77         # kg
MEAS_BONE_MASS = 88         # kg
MEAS_PULSE_WAVE_VEL = 91    # m/s — vascular stiffness
MEAS_VASCULAR_AGE = 155     # years

# Withings status codes indicating auth failure (re-run withings_login)
_AUTH_ERROR_STATUSES = {100, 101, 102, 103, 104, 105}

KG_TO_LB = 2.20462

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"


class WithingsClient:
    def __init__(self):
        self.client_id = os.environ.get("WITHINGS_CLIENT_ID", "")
        self.client_secret = os.environ.get("WITHINGS_CLIENT_SECRET", "")
        self.redirect_uri = os.environ.get("WITHINGS_REDIRECT_URI", "")
        self._tokens: dict = {}

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _load_tokens(self) -> None:
        """Load tokens from the WithingsAuth DB singleton into self._tokens."""
        from workouts.models import WithingsAuth
        auth = WithingsAuth.get()
        if not auth:
            raise RuntimeError(
                "No Withings credentials in DB. "
                "Run: venv/bin/python3 manage.py migrate_withings_tokens  "
                "(or withings_login for a fresh OAuth flow)"
            )
        self._tokens = {
            "access_token": auth.access_token,
            "refresh_token": auth.refresh_token,
            "expires_at": int(auth.token_expires_at.timestamp()),
            "userid": auth.userid,
        }

    def _save_tokens(self) -> None:
        """Persist tokens to the WithingsAuth DB singleton."""
        from workouts.models import WithingsAuth
        expires_at = datetime.fromtimestamp(self._tokens["expires_at"], tz=timezone.utc)
        WithingsAuth.objects.update_or_create(
            pk=1,
            defaults={
                "userid": str(self._tokens.get("userid", "")),
                "access_token": self._tokens["access_token"],
                "refresh_token": self._tokens["refresh_token"],
                "token_expires_at": expires_at,
            },
        )

    def _ensure_token_valid(self) -> None:
        """Load from DB if needed, then auto-refresh if within 5 minutes of expiry."""
        if not self._tokens.get("access_token"):
            self._load_tokens()
        if time.time() >= self._tokens.get("expires_at", 0) - 300:
            self.refresh_tokens()

    # ── OAuth helpers ─────────────────────────────────────────────────────────

    def get_authorization_url(self, state: str) -> str:
        """Build the OAuth2 authorization URL the user must open in their browser."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "user.metrics",
            "state": state,
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(params)

    def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for tokens and save to DB."""
        resp = requests.post(TOKEN_URL, data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 0:
            raise RuntimeError(f"Withings token exchange failed: {body}")
        token_data = body["body"]
        self._tokens = {
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": int(time.time()) + int(token_data.get("expires_in", 10800)),
            "userid": str(token_data.get("userid", "")),
        }
        self._save_tokens()
        return self._tokens

    def refresh_tokens(self) -> None:
        """Refresh the access token using the stored refresh token."""
        if not self._tokens.get("refresh_token"):
            self._load_tokens()
        resp = requests.post(TOKEN_URL, data={
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._tokens["refresh_token"],
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 0:
            raise RuntimeError(f"Withings token refresh failed: {body}")
        token_data = body["body"]
        self._tokens["access_token"] = token_data["access_token"]
        self._tokens["refresh_token"] = token_data.get("refresh_token", self._tokens["refresh_token"])
        self._tokens["expires_at"] = int(time.time()) + int(token_data.get("expires_in", 10800))
        self._save_tokens()
        logger.info("Withings tokens refreshed successfully")

    # ── API requests ──────────────────────────────────────────────────────────

    def _request(self, endpoint: str, params: dict) -> dict:
        """Make an authenticated POST to the Withings API. Retries once on 401."""
        self._ensure_token_valid()

        headers = {"Authorization": f"Bearer {self._tokens['access_token']}"}
        for attempt in range(2):
            resp = requests.post(endpoint, headers=headers, data=params)
            if resp.status_code == 401 and attempt == 0:
                logger.info("Withings 401 — refreshing tokens and retrying")
                self.refresh_tokens()
                headers["Authorization"] = f"Bearer {self._tokens['access_token']}"
                continue
            if resp.status_code == 503:
                logger.warning("Withings 503 rate limit — skipping this request")
                return {}
            resp.raise_for_status()
            body = resp.json()
            status = body.get("status")
            if status != 0:
                if status in _AUTH_ERROR_STATUSES:
                    raise RuntimeError(
                        f"Withings auth error {status}: {body.get('error')}. "
                        "Re-run: venv/bin/python3 manage.py withings_login"
                    )
                raise RuntimeError(f"Withings API error {status}: {body.get('error', body)}")
            return body
        raise RuntimeError("Withings request failed after token refresh")

    # ── Measurement parsing ───────────────────────────────────────────────────

    @staticmethod
    def _decode_value(value: int, unit: int) -> float:
        """Decode Withings measurement: raw_value * 10^unit."""
        return value * (10 ** unit)

    @staticmethod
    def _to_lb(kg: float) -> float:
        return round(kg * KG_TO_LB, 3)

    def _normalize_grp(self, grp: dict) -> dict:
        """Normalize one measuregrp dict into a flat measurement record."""
        ts = grp.get("date", 0)
        measured_at = datetime.fromtimestamp(ts, tz=timezone.utc)

        result: dict = {
            "measured_at": measured_at,
            "grpid": str(grp.get("grpid", "")),
            "weight_lb": None,
            "fat_mass_lb": None,
            "fat_free_mass_lb": None,
            "fat_ratio_pct": None,
            "muscle_mass_lb": None,
            "hydration_lb": None,
            "bone_mass_lb": None,
            "pulse_wave_velocity": None,
            "vascular_age": None,
            "raw": grp,
        }

        for m in grp.get("measures", []):
            raw_val = m.get("value")
            unit = m.get("unit", 0)
            mtype = m.get("type")
            if raw_val is None:
                continue
            decoded = self._decode_value(raw_val, unit)

            if mtype == MEAS_WEIGHT:
                result["weight_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_FAT_MASS:
                result["fat_mass_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_FAT_FREE:
                result["fat_free_mass_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_FAT_RATIO:
                result["fat_ratio_pct"] = round(decoded, 2)
            elif mtype == MEAS_MUSCLE_MASS:
                result["muscle_mass_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_HYDRATION:
                result["hydration_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_BONE_MASS:
                result["bone_mass_lb"] = self._to_lb(decoded)
            elif mtype == MEAS_PULSE_WAVE_VEL:
                result["pulse_wave_velocity"] = round(decoded, 2)
            elif mtype == MEAS_VASCULAR_AGE:
                result["vascular_age"] = int(decoded)

        return result

    # ── Main data fetch ───────────────────────────────────────────────────────

    def get_measurements(
        self,
        start_date=None,
        end_date=None,
        lastupdate=None,
    ) -> list[dict]:
        """
        Fetch all body composition measurements, paginating until more==0.

        Args:
            start_date: Unix epoch int (optional)
            end_date:   Unix epoch int (optional)
            lastupdate: Unix epoch int (optional) — use instead of start/end for incremental

        Returns:
            List of normalized measurement dicts.
        """
        meastypes = ",".join(str(t) for t in [
            MEAS_WEIGHT, MEAS_FAT_FREE, MEAS_FAT_RATIO,
            MEAS_FAT_MASS, MEAS_MUSCLE_MASS, MEAS_HYDRATION, MEAS_BONE_MASS,
            MEAS_PULSE_WAVE_VEL, MEAS_VASCULAR_AGE,
        ])

        base_params: dict = {
            "action": "getmeas",
            "meastypes": meastypes,
        }
        if lastupdate is not None:
            base_params["lastupdate"] = lastupdate
        else:
            if start_date is not None:
                base_params["startdate"] = start_date
            if end_date is not None:
                base_params["enddate"] = end_date

        all_measurements: list[dict] = []
        offset = 0

        while True:
            params = {**base_params, "offset": offset}
            body = self._request(MEASURE_URL, params)
            data = body.get("body", {})
            grps = data.get("measuregrps", [])

            for grp in grps:
                all_measurements.append(self._normalize_grp(grp))

            more = data.get("more", 0)
            if not more:
                break
            offset = data.get("offset", offset + len(grps))
            logger.debug("Withings pagination: fetched %d so far, more=%s, next offset=%d",
                         len(all_measurements), more, offset)

        return all_measurements
