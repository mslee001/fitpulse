"""
Peloton API client.

Auth: Peloton's web app signs in through Auth0 (auth.onepeloton.com). Each user
pastes a refresh token once (see partials/peloton_token_help.html); this client
trades it for 48-hour Bearer access tokens and saves every rotated refresh
token on the user's PelotonAuth row. The old session cookie and /auth/login
no longer work against the API.
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class PelotonAuthError(Exception):
    """The user's Peloton sign-in is missing, expired or was rejected."""


class PelotonNetworkError(Exception):
    """Peloton couldn't be reached (retryable), as opposed to a rejected sign-in."""


# Refresh when the access token has less than this left — a long sync can't
# outlive it, and it keeps refreshes to about one a day.
_REFRESH_MARGIN = timedelta(minutes=60)

_EXPIRED_TOKEN_MSG = ("That Peloton sign-in token expired or was already used. "
                      "Copy a new one and paste it again.")


# Movement names in a class's segment data that aren't exercises — pacing cues,
# rests, and transitions Peloton models as "movements" alongside real ones.
_NON_EXERCISE_MOVEMENTS = {
    "rest", "demo", "transition", "warm up", "cool down",
    "recovery pace", "easy pace", "moderate pace", "hard pace", "max pace",
}


def parse_class_plan(ride_details: dict) -> list:
    """Flatten /api/ride/{id}/details into the class's exercise plan.

    Returns [{"name": "Lower Body", "metrics_type": "floor", "length": 1501,
    "exercises": [{"name": "Hip Bridge", "appearances": 5}, ...]}, ...] —
    one entry per segment that contains at least one real exercise, exercises
    in order of first appearance. "appearances" counts the sub-blocks
    (circuit rounds, finisher moves) an exercise shows up in, a rough
    proxy for how many sets the class programs. Class-level only: says what
    was programmed, not what any one member did.
    """
    plan = []
    segments = ((ride_details or {}).get("segments") or {}).get("segment_list") or []
    for seg in segments:
        counts = {}
        for sub in seg.get("subsegments_v2") or []:
            if sub.get("type") == "rest":
                continue
            seen_in_sub = set()
            for m in sub.get("movements") or []:
                name = (m.get("name") or "").strip()
                if not name or m.get("is_rest") or name.lower() in _NON_EXERCISE_MOVEMENTS:
                    continue
                if name in seen_in_sub:
                    continue  # e.g. "Single Leg Hip Bridge" listed once per side
                seen_in_sub.add(name)
                counts[name] = counts.get(name, 0) + 1
        if counts:
            plan.append({
                "name": seg.get("name", ""),
                "metrics_type": seg.get("metrics_type", ""),
                "length": seg.get("length"),
                "exercises": [{"name": n, "appearances": c} for n, c in counts.items()],
            })
    return plan


class PelotonClient:
    BASE_URL = settings.PELOTON_API_BASE

    def __init__(self, user):
        from workouts.models import PelotonAuth
        self.user = user
        auth = PelotonAuth.for_user(user)
        if not auth or not auth.refresh_token:
            raise PelotonAuthError("Peloton isn't connected. Connect Peloton at /settings/integrations/")
        self._auth_pk = auth.pk
        self._access_token = auth.access_token
        self._access_expires_at = auth.access_expires_at
        self.session = requests.Session()
        self.session.headers.update({"peloton-platform": "web"})
        self.user_id = auth.peloton_user_id

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

    @staticmethod
    def exchange_refresh_token(refresh_token: str) -> dict:
        """Trade a refresh token for an access token at Peloton's Auth0.

        Returns {"access_token", "refresh_token", "expires_at"}; "refresh_token"
        is None when Auth0 didn't rotate (keep the old one). The token passed in
        is spent either way. Never logs token values. Raises PelotonAuthError
        when Auth0 rejects it, PelotonNetworkError on connection errors,
        timeouts or 5xx."""
        try:
            resp = requests.post(
                settings.PELOTON_AUTH_TOKEN_URL,
                json={"grant_type": "refresh_token", "client_id": settings.PELOTON_WEB_CLIENT_ID,
                      "refresh_token": refresh_token},
                timeout=20,
            )
        except requests.RequestException as e:
            raise PelotonNetworkError("Couldn't reach Peloton just now. Try again in a minute.") from e
        if resp.status_code >= 500:
            raise PelotonNetworkError(f"Peloton's sign-in service returned an error ({resp.status_code}). "
                                      "Try again in a minute.")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("access_token"):
            logger.warning("Peloton token refresh rejected: %s %s", resp.status_code, body.get("error", ""))
            raise PelotonAuthError(_EXPIRED_TOKEN_MSG)
        expires_in = int(body.get("expires_in") or 0)
        return {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or None,
            "expires_at": timezone.now() + timedelta(seconds=max(expires_in - 60, 0)),
        }

    @staticmethod
    def fetch_me(access_token: str) -> dict:
        """Check an access token against /api/me. Returns {"id", "username"} —
        the only keys read from the response. Raises PelotonAuthError for a
        rejected token, PelotonNetworkError otherwise."""
        try:
            resp = requests.get(
                f"{PelotonClient.BASE_URL}/api/me",
                headers={"Authorization": f"Bearer {access_token}", "peloton-platform": "web"},
                timeout=15,
            )
        except requests.RequestException as e:
            raise PelotonNetworkError("Couldn't reach Peloton just now. Try again in a minute.") from e
        if resp.status_code in (401, 403):
            raise PelotonAuthError("Peloton didn't accept that sign-in. Copy a new token and paste it again.")
        if resp.status_code >= 400:
            raise PelotonNetworkError(f"Peloton returned an error ({resp.status_code}). Try again in a minute.")
        data = resp.json()
        if not data.get("id"):
            raise PelotonAuthError("Peloton didn't accept that sign-in. Copy a new token and paste it again.")
        return {"id": data["id"], "username": data.get("username") or ""}

    def _token_fresh(self, token, expires_at):
        return bool(token) and expires_at is not None and expires_at - timezone.now() > _REFRESH_MARGIN

    def _use_token(self, token, expires_at):
        self._access_token, self._access_expires_at = token, expires_at
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _ensure_token(self, force=False):
        """Make sure the session carries a usable access token, refreshing it
        (and saving the rotated refresh token) when it's close to expiry.

        Double-checked: the per-user lock serializes threads in this process,
        and select_for_update re-reads the row so a thread that waited uses the
        token the first one just saved instead of spending a refresh token
        twice (a reused refresh token can make Auth0 revoke the whole family).

        Known, accepted gap: if the process dies after Auth0 answers but before
        the commit, the rotated token is lost and the user has to reconnect.
        The save happens straight after the response to keep that window tiny.
        """
        from workouts.locks import user_lock
        from workouts.models import PelotonAuth, WebhookError

        if not force and self._token_fresh(self._access_token, self._access_expires_at):
            self.session.headers["Authorization"] = f"Bearer {self._access_token}"
            return
        failure = None
        with user_lock("peloton_token", self.user.id):
            try:
                with transaction.atomic():
                    auth = PelotonAuth.objects.select_for_update().get(pk=self._auth_pk)
                    if not force and self._token_fresh(auth.access_token, auth.access_expires_at):
                        self._use_token(auth.access_token, auth.access_expires_at)
                        return
                    if force and auth.access_token and auth.access_token != self._access_token:
                        # Someone refreshed since this client's token was rejected — try theirs first.
                        self._use_token(auth.access_token, auth.access_expires_at)
                        return
                    if not auth.refresh_token:
                        raise PelotonAuthError("no refresh token saved")
                    tokens = self.exchange_refresh_token(auth.refresh_token)
                    now = timezone.now()
                    auth.access_token = tokens["access_token"]
                    auth.access_expires_at = tokens["expires_at"]
                    if tokens["refresh_token"]:
                        auth.refresh_token = tokens["refresh_token"]
                    auth.refresh_rotated_at = now
                    auth.auth_failed_at = None
                    auth.auth_error = ""
                    auth.save(update_fields=["access_token", "access_expires_at", "refresh_token",
                                             "refresh_rotated_at", "auth_failed_at", "auth_error",
                                             "last_updated"])
                    self._use_token(auth.access_token, auth.access_expires_at)
            except PelotonAuthError as e:
                failure = e
        if failure is not None:
            PelotonAuth.objects.filter(pk=self._auth_pk).update(
                auth_failed_at=timezone.now(), auth_error=str(failure)[:300])
            WebhookError.record(source="peloton_auth", summary=f"Peloton sign-in refresh failed: {failure}"[:300],
                                detail="", user=self.user)
            raise PelotonAuthError("Peloton sign-in expired. Reconnect at /settings/integrations/") from failure

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        self._ensure_token()
        response = self.session.get(url, params=params, timeout=30)
        if response.status_code == 401:
            self._ensure_token(force=True)
            response = self.session.get(url, params=params, timeout=30)
        if response.status_code in (401, 403):
            raise PelotonAuthError(
                f"Peloton returned {response.status_code} — the sign-in may have ended. "
                "Reconnect at /settings/integrations/"
            )
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------------
    # Workouts
    # -------------------------------------------------------------------------

    def get_workouts(self, limit=20, page=0, sort_by="-created", ride_id=None):
        params = {
            "joins": "peloton.ride,peloton.ride.instructor",
            "limit": limit,
            "page": page,
            "sort_by": sort_by,
        }
        if ride_id:
            params["ride_id"] = ride_id
        return self._get(f"/api/user/{self.user_id}/workouts", params=params)

    def get_workout_detail(self, workout_id: str) -> dict:
        return self._get(
            f"/api/workout/{workout_id}",
            # params={"joins": "peloton,peloton.ride,peloton.ride.instructor,user"},
        )

    def get_parsed_workout_detail(self, workout_id: str) -> dict:
        """
        Fetches /api/workout/:workoutId and returns a template-ready dict:

        {
          "is_pr": bool,
          "total_work": float,
          "average_effort_score": float,
          "leaderboard_rank": int,
          "total_leaderboard_users": int,
          "leaderboard_distance_rank": int,
          "total_leaderboard_distance_users": int,
          "achievements": [{"name", "description", "image_url", "count"}, ...],
          "class_description": str,
          "difficulty_estimate": float,
          "movement_tracker_tier": str,         # "Gold", "Silver", etc.
          "movement_summary": {...},             # totals across all exercises
          "movements": [{"name", "reps_done", "reps_target", "sets_done", ...}, ...],
          "strava_id": str,
        }
        """
        raw = self.get_workout_detail(workout_id)
        ride = raw.get("ride") or {}

        achievements = [
            {
                "name": a.get("name", ""),
                "description": a.get("description", ""),
                "image_url": a.get("image_url", ""),
                "count": a.get("achievement_count"),
            }
            for a in raw.get("achievement_templates", [])
        ]

        movements = []
        movement_summary = {}
        mtd = raw.get("movement_tracker_data") or {}
        summary_data = mtd.get("completed_movements_summary_data") or {}
        if summary_data:
            movement_summary = {
                "total_volume": summary_data.get("total_volume"),
                "weight_unit": summary_data.get("weight_unit", "lb"),
                "total_repetitions": summary_data.get("total_repetitions"),
                "num_movements": summary_data.get("num_movements"),
                "num_targets_reached": summary_data.get("num_targets_reached"),
                "completion_percentage": summary_data.get("completion_percentage"),
            }
            for m in summary_data.get("movement_aggregate_data", []):
                stats = {s["slug"]: s for s in m.get("stats", []) if s.get("slug")}
                # Prefer heaviest weight category first
                weight_lbs = None
                weight_cat = None
                wi = m.get("weight_info_summary_data") or {}
                for cat in ("heavy_weights", "medium_weights", "light_weights", "other_weights"):
                    lst = wi.get(cat)
                    if lst:
                        weight_lbs = lst[0].get("weight_value")
                        weight_cat = cat.replace("_weights", "")
                        break

                is_target_reached = False
                mvmts = m.get("movements", [])
                if mvmts:
                    is_target_reached = mvmts[0].get("is_target_reached", False)

                movements.append({
                    "name": m.get("movement_name", ""),
                    "tracking_type": m.get("tracking_type", ""),
                    "reps_done": stats.get("total_reps", {}).get("completed_number"),
                    "reps_target": stats.get("total_reps", {}).get("target_number"),
                    "sets_done": stats.get("targets_hit", {}).get("completed_number"),
                    "sets_target": stats.get("targets_hit", {}).get("target_number"),
                    "volume": stats.get("total_volume", {}).get("completed_number"),
                    "weight_lbs": weight_lbs,
                    "weight_cat": weight_cat,
                    "is_target_reached": is_target_reached,
                    "tags": m.get("tags", []),
                })

        # HR zone durations — cycling/running use total_heart_rate_zone_durations;
        # yoga/other disciplines store them inside effort_zones.heart_rate_zone_durations
        hr_zone_raw = raw.get("total_heart_rate_zone_durations") or {}
        if not hr_zone_raw:
            hr_zone_raw = (raw.get("effort_zones") or {}).get("heart_rate_zone_durations") or {}
        hr_zones = {
            "z1": hr_zone_raw.get("heart_rate_z1_duration"),
            "z2": hr_zone_raw.get("heart_rate_z2_duration"),
            "z3": hr_zone_raw.get("heart_rate_z3_duration"),
            "z4": hr_zone_raw.get("heart_rate_z4_duration"),
            "z5": hr_zone_raw.get("heart_rate_z5_duration"),
        } if hr_zone_raw else {}

        return {
            "is_pr": raw.get("is_total_work_personal_record", False),
            "total_work": raw.get("total_work"),
            "average_effort_score": raw.get("average_effort_score"),
            "leaderboard_rank": raw.get("leaderboard_rank"),
            "total_leaderboard_users": raw.get("total_leaderboard_users"),
            "leaderboard_distance_rank": raw.get("leaderboard_distance_rank"),
            "total_leaderboard_distance_users": raw.get("total_leaderboard_distance_users"),
            "achievements": achievements,
            "class_description": ride.get("description", ""),
            "difficulty_estimate": ride.get("difficulty_estimate"),
            "movement_tracker_tier": raw.get("movement_tracker_tier_display_name", ""),
            "movements": movements,
            "movement_summary": movement_summary,
            "strava_id": raw.get("strava_id"),
            "hr_zones": hr_zones,
        }

    def get_performance_graph(self, workout_id: str, every_n: int = 5) -> dict:
        """
        Raw performance graph. every_n controls resolution:
          1  = every second (max detail)
          5  = every 5 seconds (good default for charts)
          10 = every 10 seconds (lighter, good for long workouts)

        Response shape:
          metrics[]        - time-series arrays per metric slug
          splits_metrics   - mile-by-mile splits (runs only)
          segment_list[]   - class blocks/segments (strength, cycling)
          average_summaries- pre-computed averages per metric
        """
        return self._get(
            f"/api/workout/{workout_id}/performance_graph",
            params={"every_n": every_n},
        )

    @staticmethod
    def _parse_target_pace(tmc: dict, tmpd: dict, metrics_list: list, seconds_array: list) -> list:
        if not tmc or not tmpd:
            return []
        user_level = tmc.get("workout_pace_level")
        if not user_level:
            return []

        # Detect walking by checking if recovery zone's fast_pace is > 25 min/mi
        # (running recovery never goes that slow, so this is a reliable heuristic)
        recovery_fast = next(
            (pl.get("fast_pace", 0)
             for entry in tmc.get("pace_intensities_mapping", []) if entry.get("value") == 0
             for pl in entry.get("pace_levels", []) if pl.get("slug") == user_level),
            0
        )
        recovery_cap = 35.0 if recovery_fast > 25.0 else 20.0

        # Build intensity-value → target pace
        intensity_map: dict = {}
        for entry in tmc.get("pace_intensities_mapping", []):
            intensity = entry.get("value")
            if intensity is None:
                continue
            for pl in entry.get("pace_levels", []):
                if pl.get("slug") == user_level:
                    fast = pl.get("fast_pace")
                    slow = pl.get("slow_pace")
                    if fast and slow:
                        capped_slow = min(slow, recovery_cap) if intensity == 0 else slow
                        intensity_map[intensity] = (fast + capped_slow) / 2
                    break

        if not intensity_map:
            return []

        num_points = 0
        for m in metrics_list:
            if m.get("slug") == "pace":
                num_points = len(m.get("values", []))
                break
        if not num_points:
            return []

        segments = tmpd.get("target_metrics", [])
        result = []

        # Peloton's target_metrics include the 60s class pre-show. 
        # We must offset the pedaling time to match the API's class clock.
        preshow_offset = 60 

        for i in range(num_points):
            if seconds_array and i < len(seconds_array):
                t = seconds_array[i]
            else:
                t = i * 5

            # Shift the lookup forward to skip the pre-show gap
            t_lookup = t + preshow_offset

            pace_intensity = None
            for seg in segments:
                offsets = seg.get("offsets", {})
                start = offsets.get("start", 0)
                end = offsets.get("end", 0)
                
                # Look up the shifted time against the API's bounds
                if start <= t_lookup <= end:
                    for m in seg.get("metrics", []):
                        if m.get("name") == "pace_intensity":
                            pace_intensity = m.get("upper")
                            break
                    if pace_intensity is not None:
                        break
            
            if pace_intensity is not None and pace_intensity in intensity_map:
                result.append(intensity_map[pace_intensity])
            else:
                result.append(None)
                
        return result
    
    def get_parsed_performance(self, workout_id: str, every_n: int = 5) -> dict:
        """
        Fetches and parses the performance graph into a friendlier structure:

        {
          "metrics_by_slug": { "pace": {...}, "speed": {...}, "heart_rate": {...}, ... },
          "splits": [ {"mile": 1, "pace": 945, "elevation": 55, "is_best": True, ...} ],
          "segments": [ {"name": "Warm Up", "length_seconds": 300, "subsegments": [...], ...} ],
          "average_summaries": { "avg_pace": {...}, ... },
          "summaries": { "distance": {...}, "total_output": {...}, "elevation": {...}, ... },
          "muscle_groups": [ {"muscle_group": "glutes", "percentage": 16, "bucket": 3, ...} ],
          "effort_zones": { "total_effort_points": 64.7, "heart_rate_zone_durations": {...} },
          "every_n": 5,
          "duration": 1800,
        }
        """
        raw = self.get_performance_graph(workout_id, every_n)

        # Index metrics by slug; also surface alternatives (e.g. speed nested under pace)
        metrics_by_slug = {}
        for m in raw.get("metrics", []):
            slug = m.get("slug") or m.get("display_name", "").lower().replace(" ", "_")
            metrics_by_slug[slug] = {
                "display_name": m.get("display_name", ""),
                "display_unit": m.get("display_unit", ""),
                "values": m.get("values", []),
                "average_value": m.get("average_value"),
                "max_value": m.get("max_value"),
                "zones": m.get("zones"),  # HR zone objects with range strings and durations
            }
            for alt in m.get("alternatives", []):
                alt_slug = alt.get("slug") or alt.get("display_name", "").lower().replace(" ", "_")
                if alt_slug not in metrics_by_slug:
                    metrics_by_slug[alt_slug] = {
                        "display_name": alt.get("display_name", ""),
                        "display_unit": alt.get("display_unit", ""),
                        "values": alt.get("values", []),
                        "average_value": alt.get("average_value"),
                        "max_value": alt.get("max_value"),
                        "zones": None,
                    }

        # Parse mile splits — actual API shape: splits_metrics.metrics[].data[]
        # (pace comes as min/mi float; convert to sec/mi for the format_pace filter)
        splits = []
        splits_raw = raw.get("splits_metrics") or {}
        for i, row in enumerate(splits_raw.get("metrics", []), start=1):
            split = {"mile": i, "is_best": row.get("is_best", False)}
            for item in row.get("data", []):
                slug = item.get("slug")
                val = item.get("value")
                if slug == "mi":
                    split["mile_distance"] = val
                elif slug == "pace":
                    split["pace"] = round(val * 60) if val is not None else None
                elif slug == "total_time":
                    split["total_time_minutes"] = val
                elif slug == "elevation":
                    split["elevation"] = val
                elif slug:
                    split[slug] = val
            splits.append(split)

        # Parse segment list with subsegments
        segments = []
        for seg in raw.get("segment_list", []):
            subsegments = [
                {
                    "name": sub.get("name", ""),
                    "length_seconds": sub.get("length", 0),
                    "type": sub.get("type", ""),
                    "metrics_type": sub.get("metrics_type", ""),
                    "icon_url": sub.get("icon_url", ""),
                }
                for sub in seg.get("subsegments", [])
            ]
            segments.append({
                "name": seg.get("name", ""),
                "length_seconds": seg.get("length", 0),
                "icon_url": seg.get("icon_url", ""),
                "metrics_type": seg.get("metrics_type", ""),
                "subsegments": subsegments,
            })

        # Index average summaries (e.g. avg_pace, avg_speed) by slug
        average_summaries = {}
        for s in raw.get("average_summaries", []):
            slug = s.get("slug") or s.get("display_name", "").lower()
            average_summaries[slug] = s

        # Index workout totals (distance, output, elevation, calories) by slug
        summaries = {}
        for s in raw.get("summaries", []):
            slug = s.get("slug") or s.get("display_name", "").lower()
            summaries[slug] = s

        # Build target pace time-series from compliance metadata (running only)
        target_pace = self._parse_target_pace(
            raw.get("target_metrics_compliance") or {},
            raw.get("target_metrics_performance_data") or {},
            raw.get("metrics", []),
            raw.get("seconds_since_pedaling_start", [])  # Pass the array here
        )
        if target_pace and any(v is not None for v in target_pace):
            valid = [v for v in target_pace if v is not None]
            metrics_by_slug["target_pace"] = {
                "display_name": "Target Pace",
                "display_unit": "min/mi",
                "values": target_pace,
                "average_value": sum(valid) / len(valid) if valid else None,
                "max_value": None,
                "zones": None,
            }

            # --- EXTRACT PACE ZONES FOR GRAPH BACKGROUND ---
        pace_zones = []
        pace_level_display = None
        target_compliance = raw.get("target_metrics_compliance") or {}
        user_level = target_compliance.get("workout_pace_level")
        recovery_fast = next(
            (pl.get("fast_pace", 0)
             for entry in target_compliance.get("pace_intensities_mapping", []) if entry.get("value") == 0
             for pl in entry.get("pace_levels", []) if pl.get("slug") == user_level),
            0
        )
        recovery_cap = 35.0 if recovery_fast > 25.0 else 20.0

        if user_level:
            for entry in target_compliance.get("pace_intensities_mapping", []):
                name = entry.get("display_name")
                intensity_val = entry.get("value")
                for pl in entry.get("pace_levels", []):
                    if pl.get("slug") == user_level:
                        if pace_level_display is None:
                            pace_level_display = pl.get("display_name")
                        fast = pl.get("fast_pace")
                        slow = recovery_cap if intensity_val == 0 else pl.get("slow_pace")
                        if fast and slow:
                            pace_zones.append({
                                "name": name,
                                "fast_pace": fast,
                                "slow_pace": slow
                            })
                        break
        # -----------------------------------------------

        # Extract power zone segments and time distribution (cycling)
        power_zones = []
        power_zone_distribution = {}
        tmpd = raw.get("target_metrics_performance_data") or {}
        for seg in tmpd.get("target_metrics", []):
            if seg.get("segment_type") != "power_zone":
                continue
            for m in seg.get("metrics", []):
                if m.get("name") == "power_zone":
                    power_zones.append({
                        "zone": m.get("upper"),
                        "start": seg.get("offsets", {}).get("start", 0),
                        "end": seg.get("offsets", {}).get("end", 0),
                    })
                    break
        for tim in tmpd.get("time_in_metric", []):
            if tim.get("name") == "power_zone":
                power_zone_distribution = tim.get("distribution", {})
                break

        return {
            "metrics_by_slug": metrics_by_slug,
            "splits": splits,
            "segments": segments,
            "average_summaries": average_summaries,
            "summaries": summaries,
            "muscle_groups": raw.get("muscle_group_score", []),
            "effort_zones": raw.get("effort_zones", {}),
            "every_n": every_n,
            "duration": raw.get("duration"),
            "target_pace": target_pace,
            "pace_zones": pace_zones,
            "pace_level": pace_level_display,
            "power_zones": power_zones,
            "power_zone_distribution": power_zone_distribution,
        }

    # -------------------------------------------------------------------------
    # User overview
    # -------------------------------------------------------------------------

    def get_overview(self) -> dict:
        return self._get(f"/api/user/{self.user_id}/overview", params={"version": 2})

    def get_calendar(self) -> dict:
        return self._get(f"/api/user/{self.user_id}/calendar")

    # -------------------------------------------------------------------------
    # Rides / classes
    # -------------------------------------------------------------------------

    def get_ride_details(self, ride_id: str) -> dict:
        return self._get(f"/api/ride/{ride_id}/details")

    def get_class_plan(self, ride_id: str) -> list:
        """Exercise plan for a class — see parse_class_plan."""
        return parse_class_plan(self.get_ride_details(ride_id))

    def get_browse_categories(self) -> list:
        data = self._get("/api/browse_categories", params={"library_type": "on_demand"})
        return data.get("browse_categories", [])
