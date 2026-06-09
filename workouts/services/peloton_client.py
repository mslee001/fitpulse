"""
Peloton API client.

Auth note: The old /auth/login endpoint is dead (403). This client uses the
session cookie approach — grab `peloton_session_id` from your browser's
DevTools (Application → Cookies) while logged into members.onepeloton.com,
and set it in your .env file. Sessions last several days before expiring.
"""

import requests
from django.conf import settings


class PelotonClient:
    BASE_URL = settings.PELOTON_API_BASE

    def __init__(self):
        self.session = requests.Session()
        self.session.cookies.set("peloton_session_id", settings.PELOTON_SESSION_ID)
        self.session.headers.update({"peloton-platform": "web"})
        self.user_id = settings.PELOTON_USER_ID

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        response = self.session.get(url, params=params)
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

    def get_browse_categories(self) -> list:
        data = self._get("/api/browse_categories", params={"library_type": "on_demand"})
        return data.get("browse_categories", [])
