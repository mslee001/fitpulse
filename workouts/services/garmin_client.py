import logging
import os
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# Maps Garmin sport type keys to the discipline strings used in CachedWorkout
GARMIN_SPORT_TO_DISCIPLINE = {
    "running": "running",
    "treadmill_running": "running",
    "indoor_running": "running",
    "trail_running": "running",
    "virtual_run": "running",
    "cycling": "cycling",
    "indoor_cycling": "cycling",
    "road_biking": "cycling",
    "mountain_biking": "cycling",
    "virtual_ride": "cycling",
    "strength_training": "strength",
    "walking": "walking",
    "indoor_walking": "walking",
    "yoga": "yoga",
    "pilates": "stretching",
    "hiit": "cardio",
    "cardio": "cardio",
    "elliptical": "cardio",
    "stair_climbing": "cardio",
    "meditation": "meditation",
    "stretching": "stretching",
    "swimming": "cardio",
}


class GarminClient:
    TOKEN_DIR = os.path.expanduser("~/.garminconnect")

    def __init__(self):
        from garminconnect import Garmin
        if not os.path.isdir(self.TOKEN_DIR):
            raise RuntimeError(
                "Garmin tokens not found. Run: python manage.py garmin_login"
            )
        self.api = Garmin(email=settings.GARMIN_EMAIL, password=settings.GARMIN_PASSWORD)
        self.api.login(self.TOKEN_DIR)

    def get_activities(self, limit=20, start=0, activity_type=None):
        return self.api.get_activities(start=start, limit=limit, activitytype=activity_type)

    def get_activity_details(self, garmin_activity_id: int):
        return self.api.get_activity_details(garmin_activity_id)

    def get_hr_zones(self, garmin_activity_id: int):
        return self.api.get_activity_hr_in_timezones(garmin_activity_id)

    def get_splits(self, garmin_activity_id: int):
        return self.api.get_activity_splits(garmin_activity_id)

    def get_exercise_sets(self, garmin_activity_id: int):
        return self.api.get_activity_exercise_sets(garmin_activity_id)

    # ── Wellness / daily stats ────────────────────────────────────────────────

    def get_wellness_data(self, date_str: str) -> dict:
        """
        Fetch all daily wellness signals for date_str (YYYY-MM-DD) and return
        a normalized dict ready to be applied to a DailyStats instance.
        Each sub-call is wrapped individually so a missing endpoint never
        aborts the whole fetch.
        """
        result = {}

        def _safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs) or {}
            except Exception as exc:
                logger.debug("Garmin wellness call failed (%s): %s", fn.__name__, exc)
                return {}

        def _num(v):
            """Extract a number from either a plain value or a {'value': N, ...} dict."""
            if isinstance(v, dict):
                return v.get("value")
            return v

        # Body battery
        bb_raw = _safe(self.api.get_body_battery, date_str, date_str)
        bb_entries = bb_raw if isinstance(bb_raw, list) else []
        if bb_entries:
            series = []
            for entry in bb_entries:
                for reading in entry.get("bodyBatteryValuesArray", []):
                    if len(reading) >= 3:
                        series.append({"ts": reading[0], "status": reading[1], "value": reading[2]})
                    elif len(reading) == 2:
                        series.append({"ts": reading[0], "value": reading[1]})
            values = [s["value"] for s in series if s.get("value") is not None]
            result["body_battery_json"] = series
            result["body_battery_high"] = max(values) if values else None
            result["body_battery_low"] = min(values) if values else None

            # charge/drain totals from entry-level fields
            if bb_entries:
                first_entry = bb_entries[0]
                result["body_battery_charge"] = _num(first_entry.get("charged"))
                result["body_battery_drain"] = _num(first_entry.get("drained"))

            # start/end are set later from get_stats (bodyBatteryAtWakeTime /
            # bodyBatteryMostRecentValue), which are the accurate wakeup and
            # most-recent values. Store time-series endpoints as a last-resort
            # fallback only if get_stats didn't provide them.
            if values:
                result["_bb_series_first"] = values[0]
                # For end-of-day, use the minimum after the daily peak — this is the
                # bedtime drain value. bodyBatteryMostRecentValue is the midnight
                # reading (overnight charging has already started) and overstates
                # the battery at sleep time.
                peak_idx = values.index(max(values))
                post_peak = values[peak_idx:]
                if len(post_peak) > 1:
                    result["_bb_series_end_of_day"] = min(post_peak)
                else:
                    result["_bb_series_end_of_day"] = values[-1]

        # Sleep
        sleep_raw = _safe(self.api.get_sleep_data, date_str)
        daily_sleep = sleep_raw.get("dailySleepDTO") or {}
        if daily_sleep:
            overall = (sleep_raw.get("sleepScores") or {}).get("overall") or \
                      (daily_sleep.get("sleepScores") or {}).get("overall")
            result["sleep_score"] = _num(overall)
            result["sleep_seconds"] = daily_sleep.get("sleepTimeSeconds")
            result["sleep_deep_seconds"] = daily_sleep.get("deepSleepSeconds")
            result["sleep_light_seconds"] = daily_sleep.get("lightSleepSeconds")
            result["sleep_rem_seconds"] = daily_sleep.get("remSleepSeconds")

        # HRV — summary fields + min/max from 5-min readings
        hrv_raw = _safe(self.api.get_hrv_data, date_str)
        hrv_summary = hrv_raw.get("hrvSummary") or {}
        if hrv_summary:
            result["hrv_weekly_avg"] = hrv_summary.get("weeklyAvg")
            result["hrv_last_night"] = hrv_summary.get("lastNightAvg")
            result["hrv_status"] = hrv_summary.get("status", "")
        hrv_readings = hrv_raw.get("hrvReadings") or []
        if hrv_readings:
            hrv_values = [r["hrvValue"] for r in hrv_readings if r.get("hrvValue") is not None]
            if hrv_values:
                result["hrv_min"] = min(hrv_values)
                result["hrv_max"] = max(hrv_values)

        # Resting HR
        rhr_raw = _safe(self.api.get_rhr_day, date_str)
        rhr_list = rhr_raw.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [])
        if rhr_list:
            result["resting_hr"] = rhr_list[0].get("value")

        # Stress (avg from get_all_day_stress)
        stress_raw = _safe(self.api.get_all_day_stress, date_str)
        result["stress_avg"] = stress_raw.get("avgStressLevel")

        # Activity volume + stress breakdown + body battery start/end + SpO2/respiration
        # get_stats is the richest single endpoint — mine it for many fields at once.
        try:
            stats_raw = self.api.get_stats(date_str) or {}
            if stats_raw:
                result["steps"] = _num(stats_raw.get("totalSteps"))
                # floorsAscended is a float (e.g. 6.67 floors); round to int
                floors = _num(stats_raw.get("floorsAscended"))
                result["floors_climbed"] = int(round(floors)) if floors is not None else None
                result["active_calories"] = _num(stats_raw.get("activeKilocalories"))
                result["total_calories"] = _num(stats_raw.get("totalKilocalories"))
                result["bmr_calories"] = _num(stats_raw.get("bmrKilocalories"))
                result["moderate_intensity_minutes"] = _num(stats_raw.get("moderateIntensityMinutes"))
                result["vigorous_intensity_minutes"] = _num(stats_raw.get("vigorousIntensityMinutes"))
                result["steps_goal"] = _num(stats_raw.get("dailyStepGoal"))
                result["floors_climbed_goal"] = _num(stats_raw.get("userFloorsAscendedGoal"))
                result["intensity_minutes_goal"] = _num(stats_raw.get("intensityMinutesGoal"))
                # Stress refinement — durations in get_stats are in seconds; convert to minutes
                max_stress = _num(stats_raw.get("maxStressLevel"))
                result["stress_max"] = int(max_stress) if max_stress is not None else None
                rest_secs = _num(stats_raw.get("restStressDuration"))
                low_secs = _num(stats_raw.get("lowStressDuration"))
                med_secs = _num(stats_raw.get("mediumStressDuration"))
                high_secs = _num(stats_raw.get("highStressDuration"))
                result["stress_rest_minutes"] = int(rest_secs // 60) if rest_secs is not None else None
                result["stress_low_minutes"] = int(low_secs // 60) if low_secs is not None else None
                result["stress_medium_minutes"] = int(med_secs // 60) if med_secs is not None else None
                result["stress_high_minutes"] = int(high_secs // 60) if high_secs is not None else None
                # Body battery start = wakeup value (authoritative from get_stats).
                # body_battery_end is derived from the time series (min after daily
                # peak) — bodyBatteryMostRecentValue is NOT used because for past
                # days it reflects the midnight reading after overnight charging has
                # started, which overstates the battery at bedtime.
                wake_val = _num(stats_raw.get("bodyBatteryAtWakeTime"))
                if wake_val is not None:
                    result["body_battery_start"] = int(wake_val)
                if "body_battery_charge" not in result:
                    charged = _num(stats_raw.get("bodyBatteryChargedValue"))
                    result["body_battery_charge"] = int(charged) if charged is not None else None
                if "body_battery_drain" not in result:
                    drained = _num(stats_raw.get("bodyBatteryDrainedValue"))
                    result["body_battery_drain"] = int(drained) if drained is not None else None
        except Exception as exc:
            logger.warning("Garmin wellness call failed (get_stats): %s", exc)

        # Resolve body battery start/end from time series
        if "body_battery_start" not in result and "_bb_series_first" in result:
            result["body_battery_start"] = result["_bb_series_first"]
        # end-of-day = min after daily peak (bedtime drain value), not midnight recharge
        if "_bb_series_end_of_day" in result:
            result["body_battery_end"] = result["_bb_series_end_of_day"]
        result.pop("_bb_series_first", None)
        result.pop("_bb_series_end_of_day", None)

        # For today the day isn't over yet — strip end so we don't show a mid-day value
        from datetime import date as _date
        if date_str == _date.today().isoformat():
            result.pop("body_battery_end", None)

        # Training status (includes load + load focus)
        ts_raw = _safe(self.api.get_training_status, date_str)
        ts_detail = ts_raw.get("mostRecentTrainingStatus") or ts_raw
        if ts_detail:
            result["training_status"] = ts_detail.get("trainingStatusType") or ts_detail.get("trainingStatus", "")
            result["training_load"] = ts_detail.get("acuteLoad") or ts_detail.get("trainingLoad")
            focus = ts_detail.get("loadFocus") or {}
            result["load_focus_anaerobic"] = focus.get("anaerobicEffect")
            result["load_focus_high_aerobic"] = focus.get("highAerobicEffect")
            result["load_focus_low_aerobic"] = focus.get("lowAerobicEffect")

        # Training readiness
        tr_raw = _safe(self.api.get_training_readiness, date_str)
        tr_list = tr_raw if isinstance(tr_raw, list) else tr_raw.get("items", [])
        if tr_list:
            latest = tr_list[-1] if isinstance(tr_list, list) else tr_raw
            result["training_readiness_score"] = latest.get("score") or latest.get("trainingReadinessScore")
            result["training_readiness_label"] = latest.get("level") or latest.get("trainingReadinessLabel", "")

        # Respiration — avg (waking + sleep)
        try:
            resp_raw = self.api.get_respiration_data(date_str) or {}
            if resp_raw:
                # highestRespirationValue is the daily high, use as overall avg proxy
                # or use avgWakingRespirationValue as the "avg" field
                result["respiration_avg"] = _num(resp_raw.get("highestRespirationValue"))
                result["respiration_waking_avg"] = _num(resp_raw.get("avgWakingRespirationValue"))
                result["respiration_sleep_avg"] = _num(resp_raw.get("avgSleepRespirationValue"))
        except Exception as exc:
            logger.warning("Garmin wellness call failed (get_respiration_data): %s", exc)

        # SpO2 — sleep average and low
        try:
            spo2_raw = self.api.get_spo2_data(date_str) or {}
            if spo2_raw:
                spo2_sleep = _num(spo2_raw.get("avgSleepSpO2"))
                result["spo2_sleep_avg"] = spo2_sleep
                spo2_low = _num(spo2_raw.get("lowestSpO2"))
                result["spo2_sleep_low"] = int(spo2_low) if spo2_low is not None else None
        except Exception as exc:
            logger.warning("Garmin wellness call failed (get_spo2_data): %s", exc)

        # VO2 max + fitness age (get_max_metrics returns a list of metric entries)
        try:
            max_metrics = self.api.get_max_metrics(date_str) or []
            if isinstance(max_metrics, list):
                for entry in max_metrics:
                    sport = (entry.get("sport") or "").lower()
                    vo2 = _num(entry.get("vo2MaxPreciseValue") or entry.get("vo2MaxValue"))
                    if vo2 is not None:
                        if "cycl" in sport:
                            result["vo2_max_cycling"] = vo2
                        else:
                            result["vo2_max_running"] = vo2
                    age = _num(entry.get("fitnessAge") or entry.get("fitnessAgeDescriptorAge"))
                    if age is not None and "fitness_age" not in result:
                        result["fitness_age"] = int(age)
        except Exception as exc:
            logger.warning("Garmin wellness call failed (get_max_metrics): %s", exc)

        return result

    def parse_activity(self, activity: dict) -> dict:
        """Normalize a Garmin activity list entry into CachedWorkout field values."""
        activity_type = activity.get("activityType") or {}
        sport_key = activity_type.get("typeKey", "")
        discipline = GARMIN_SPORT_TO_DISCIPLINE.get(sport_key, sport_key)

        start_str = activity.get("startTimeGMT") or activity.get("startTimeLocal") or ""
        created_at = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                created_at = datetime.strptime(start_str, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue

        dist_m = activity.get("distance") or 0
        dist_miles = dist_m / 1609.344 if dist_m else None

        avg_speed_ms = activity.get("averageSpeed") or 0
        avg_speed_mph = avg_speed_ms * 2.23694 if avg_speed_ms else None
        avg_pace_secs = (1609.344 / avg_speed_ms) if avg_speed_ms else None

        cadence = (
            activity.get("averageRunningCadenceInStepsPerMinute")
            or activity.get("averageBikingCadenceInRPM")
        )

        return {
            "workout_id": f"garmin_{activity['activityId']}",
            "title": activity.get("activityName") or "Garmin Workout",
            "discipline": discipline,
            "workout_type": sport_key,
            "duration_seconds": int(activity.get("duration") or 0),
            "calories": activity.get("calories"),
            "heart_rate_avg": activity.get("averageHR"),
            "heart_rate_max": activity.get("maxHR"),
            "distance_miles": dist_miles,
            "distance": dist_miles,
            "avg_speed_mph": avg_speed_mph,
            "avg_speed": avg_speed_mph,
            "avg_pace_seconds": avg_pace_secs,
            "avg_watts": activity.get("avgPower"),
            "avg_cadence": cadence,
            "elevation_gain": activity.get("elevationGain"),
            "created_at": created_at,
            "source": "garmin",
            "raw_data": activity,
            # Running form metrics (Garmin only; None for non-running disciplines)
            "stride_length_avg": activity.get("avgStrideLength"),
            "vertical_oscillation_avg": activity.get("avgVerticalOscillation"),
            "vertical_ratio_avg": activity.get("avgVerticalRatio"),
            "ground_contact_time_avg": activity.get("avgGroundContactTime"),
        }

    def parse_hr_zones(self, hr_data) -> dict | None:
        """
        Convert get_activity_hr_in_timezones response to {z1: secs, z2: secs, ...}.
        Garmin returns a list of zone objects ordered Z1→Z5 (lowest to highest intensity),
        with zone numbers starting at 0 or 1 depending on the account configuration.
        """
        zones = hr_data if isinstance(hr_data, list) else (hr_data or {}).get("heartRateZones", [])
        if not zones:
            return None
        result = {}
        for i, zone in enumerate(zones[:5], start=1):
            secs = zone.get("secsInZone") or zone.get("secondsInZone") or 0
            result[f"z{i}"] = int(secs)
        return result if result else None

    def parse_splits(self, splits_data: dict) -> list | None:
        """
        Convert get_activity_splits response into the normalized list format used
        by performance_graph_json.splits (same shape as Peloton splits).
        Each lap is normalized to miles/seconds; elevation converted from meters to feet.
        """
        laps = (splits_data or {}).get("lapDTOs") or []
        if not laps:
            return None
        result = []
        for lap in laps:
            dist_m = lap.get("distance") or 0
            dist_miles = dist_m / 1609.344 if dist_m else None
            duration = lap.get("duration") or 0
            # pace in seconds/mile
            pace_secs = (duration / dist_miles) if dist_miles else None
            elev_gain_m = lap.get("elevationGain") or 0
            result.append({
                "lap": lap.get("lapIndex", len(result)),
                "distance_miles": round(dist_miles, 3) if dist_miles else None,
                "duration_seconds": round(duration),
                "pace_seconds": round(pace_secs) if pace_secs else None,
                "heart_rate_avg": lap.get("averageHR"),
                "elevation_gain_ft": round(elev_gain_m * 3.28084, 1) if elev_gain_m else None,
            })
        return result or None

    def parse_exercise_sets(self, sets_data: dict) -> list | None:
        """
        Convert get_activity_exercise_sets response into a clean list of sets.
        Skips REST sets. Exercise name comes from exercises[0].name (snake_case → Title Case).
        """
        raw_sets = (sets_data or {}).get("exerciseSets") or []
        if not raw_sets:
            return None
        result = []
        order = 1
        for s in raw_sets:
            if s.get("setType") == "REST":
                continue
            exercises = s.get("exercises") or []
            raw_name = exercises[0].get("name") if exercises else None
            category = exercises[0].get("category") if exercises else None
            name = raw_name.replace("_", " ").title() if raw_name else None
            weight_g = s.get("weight") or 0
            result.append({
                "order": order,
                "exercise": name,
                "exercise_key": category,
                "reps": s.get("repetitionCount"),
                "weight_kg": round(weight_g / 1000, 2) if weight_g else 0,
                "duration_seconds": round(s.get("duration") or 0),
            })
            order += 1
        return result or None

    def parse_performance(self, details: dict) -> dict | None:
        """
        Convert Garmin activity details into the metrics_by_slug format used
        by CachedWorkout.performance_graph_json so existing chart code works.

        Garmin's format: top-level `metricDescriptors` maps metricsIndex → key/unit,
        and each entry in `activityDetailMetrics` has a `metrics` array indexed by metricsIndex.
        """
        descriptors = details.get("metricDescriptors") or []
        metrics_raw = details.get("activityDetailMetrics") or []
        if not descriptors or not metrics_raw:
            return None

        SLUG_MAP = {
            "directHeartRate": "heart_rate",
            "directSpeed": "speed",
            "directPace": "pace",
            "directPower": "output",
            "directBikeCadence": "cadence",
            "directDoubleCadence": "cadence",   # steps/min (both feet) — matches activity summary
            "directElevation": "incline",
            "directResistance": "resistance",
            "directStrideLength": "stride_length",          # cm
            "directVerticalOscillation": "vertical_oscillation",  # cm
            "directVerticalRatio": "vertical_ratio",        # %
            "directGroundContactTime": "ground_contact_time",  # ms
        }

        # Build metricsIndex → (slug, factor) from top-level descriptor list.
        # factor=0 means the field is a timestamp — skip it.
        index_to_slug: dict[int, tuple[str, float]] = {}
        for desc in descriptors:
            key = desc.get("key", "")
            slug = SLUG_MAP.get(key)
            if not slug:
                continue
            idx = desc.get("metricsIndex")
            factor = (desc.get("unit") or {}).get("factor") or 1.0
            if idx is not None and factor != 0.0:
                index_to_slug[idx] = (slug, factor)

        if not index_to_slug:
            return None

        SAMPLE = 5  # store one point per 5 seconds to match Peloton's resolution
        slug_values: dict[str, list[float]] = {}
        for i, point in enumerate(metrics_raw):
            if i % SAMPLE != 0:
                continue
            vals = point.get("metrics") or []
            for idx, (slug, factor) in index_to_slug.items():
                if idx < len(vals) and vals[idx] is not None:
                    v = vals[idx] / factor if factor != 1.0 else vals[idx]
                    slug_values.setdefault(slug, []).append(v)

        metrics_by_slug: dict[str, dict] = {}
        for slug, values in slug_values.items():
            if not values:
                continue
            avg = sum(values) / len(values)
            metrics_by_slug[slug] = {
                "values": values,
                "average_value": round(avg, 2),
                "max_value": max(values),
            }

        if not metrics_by_slug:
            return None

        return {
            "metrics_by_slug": metrics_by_slug,
            "duration": details.get("metricsCount"),
            "every_n": SAMPLE,
            "source": "garmin",
        }

    @staticmethod
    def parse_performance_raw(details: dict) -> dict | None:
        """
        Same as parse_performance but keeps every sample (every_n=1).
        Used when caching full-resolution form metrics for later re-alignment.
        """
        from workouts.services.garmin_client import GarminClient
        client = GarminClient.__new__(GarminClient)
        descriptors = details.get("metricDescriptors") or []
        metrics_raw = details.get("activityDetailMetrics") or []
        if not descriptors or not metrics_raw:
            return None

        SLUG_MAP = {
            "directHeartRate": "heart_rate",
            "directSpeed": "speed",
            "directPace": "pace",
            "directPower": "output",
            "directBikeCadence": "cadence",
            "directDoubleCadence": "cadence",
            "directElevation": "incline",
            "directResistance": "resistance",
            "directStrideLength": "stride_length",
            "directVerticalOscillation": "vertical_oscillation",
            "directVerticalRatio": "vertical_ratio",
            "directGroundContactTime": "ground_contact_time",
        }
        index_to_slug: dict[int, tuple[str, float]] = {}
        for desc in descriptors:
            key = desc.get("key", "")
            slug = SLUG_MAP.get(key)
            if not slug:
                continue
            idx = desc.get("metricsIndex")
            factor = (desc.get("unit") or {}).get("factor") or 1.0
            if idx is not None and factor != 0.0:
                index_to_slug[idx] = (slug, factor)
        if not index_to_slug:
            return None

        slug_values: dict[str, list] = {}
        for point in metrics_raw:
            vals = point.get("metrics") or []
            for idx, (slug, factor) in index_to_slug.items():
                if idx < len(vals) and vals[idx] is not None:
                    v = vals[idx] / factor if factor != 1.0 else vals[idx]
                    slug_values.setdefault(slug, []).append(v)
                else:
                    slug_values.setdefault(slug, []).append(None)

        metrics_by_slug: dict[str, dict] = {}
        for slug, values in slug_values.items():
            non_null = [v for v in values if v is not None]
            if not non_null:
                continue
            metrics_by_slug[slug] = {
                "values": values,
                "average_value": round(sum(non_null) / len(non_null), 2),
                "max_value": max(non_null),
            }
        return {"metrics_by_slug": metrics_by_slug, "every_n": 1} if metrics_by_slug else None
