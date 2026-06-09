"""
Sync logic for Peloton and Garmin Connect data.

Contains all data-fetching helpers, upsert logic, and the URL-registered
sync endpoints. Nothing in here renders HTML — every public function returns
a JsonResponse.
"""

import bisect
import logging
from datetime import date, datetime, timedelta, timezone

from django.http import JsonResponse
from django.utils import timezone as tz

from .models import BodyMeasurement, CachedWorkout, DailyStats, UserSettings
from .services.garmin_client import GarminClient
from .services.peloton_client import PelotonClient
from .services.withings_client import WithingsClient

logger = logging.getLogger(__name__)

# Disciplines that have a Peloton performance graph worth fetching.
_PERF_DISCS = {"cycling", "bike_bootcamp", "running", "outdoor_running", "strength", "walking"}


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _client():
    return PelotonClient()


def _garmin_client():
    return GarminClient()


def _withings_client():
    return WithingsClient()


# ---------------------------------------------------------------------------
# Peloton helpers
# ---------------------------------------------------------------------------

def _fetch_and_store_details(workout_ids, client):
    """Fetch /api/workout/:id for each ID and persist detail fields to the DB."""
    for wid in workout_ids:
        try:
            detail = client.get_parsed_workout_detail(wid)
            w = CachedWorkout.objects.get(workout_id=wid)
            w.apply_detail(detail)
            w.save(update_fields=CachedWorkout.DETAIL_FIELDS)
        except CachedWorkout.DoesNotExist:
            pass
        except Exception as e:
            logger.warning("detail fetch failed for %s: %s", wid, e)


def _extract_perf_fields(perf: dict) -> dict:
    """Extract flat model fields (calories, distance, HR, pace) from a parsed perf graph dict."""
    avg_summaries = perf.get("average_summaries") or {}
    summaries = perf.get("summaries") or {}
    metrics = perf.get("metrics_by_slug") or {}

    calories = (summaries.get("calories") or {}).get("value")
    distance_miles = (summaries.get("distance") or {}).get("value")
    hr_avg = (metrics.get("heart_rate") or {}).get("average_value")

    # avg_pace from perf graph is decimal min/mi; store as integer seconds/mi
    avg_pace_raw = (avg_summaries.get("avg_pace") or {}).get("value")
    avg_pace_seconds = round(avg_pace_raw * 60) if avg_pace_raw else None

    return {
        "calories": calories,
        "distance_miles": distance_miles,
        "heart_rate_avg": hr_avg,
        "avg_pace_seconds": avg_pace_seconds,
    }


def _fetch_and_store_performance(workout_ids, client):
    """Fetch performance_graph for each ID and persist. Skips already-synced workouts."""
    eligible = list(
        CachedWorkout.objects
        .filter(workout_id__in=workout_ids, discipline__in=_PERF_DISCS,
                performance_graph_json__isnull=True)
        .values_list("workout_id", flat=True)
    )
    for wid in eligible:
        try:
            perf = client.get_parsed_performance(wid, every_n=5)
            if perf:
                update_fields = {"performance_graph_json": perf, **_extract_perf_fields(perf)}
                CachedWorkout.objects.filter(workout_id=wid).update(**update_fields)
        except Exception as e:
            logger.warning("perf sync failed for %s: %s", wid, e)


def _upsert_page(raw_data):
    """Write one page of Peloton API workout objects to the DB. Returns (created, updated)."""
    created_count = 0
    updated_count = 0
    current_ftp = UserSettings.get().ftp
    fields = [
        "ride_id", "title", "discipline", "fitness_discipline_display",
        "workout_type", "instructor_name", "instructor_image_url",
        "class_image_url", "duration_seconds",
        "calories", "heart_rate_avg", "heart_rate_max", "effort_score",
        "hr_z1_seconds", "hr_z2_seconds", "hr_z3_seconds", "hr_z4_seconds", "hr_z5_seconds",
        "output_watts", "avg_watts", "avg_cadence", "avg_resistance",
        "avg_speed", "distance", "leaderboard_rank", "total_leaderboard_users",
        "avg_pace_seconds", "distance_miles", "avg_speed_mph",
        "avg_incline", "max_speed_mph", "max_incline", "elevation_gain",
        "created_at", "raw_data",
    ]
    for workout_data in raw_data:
        obj = CachedWorkout.from_api(workout_data)
        defaults = {field: getattr(obj, field) for field in fields}
        _, created = CachedWorkout.objects.update_or_create(
            workout_id=obj.workout_id,
            defaults=defaults,
        )
        # Stamp FTP only on new records — don't overwrite a manually-corrected historical value.
        if created:
            CachedWorkout.objects.filter(workout_id=obj.workout_id).update(ftp=current_ftp)
            created_count += 1
        else:
            updated_count += 1
    return created_count, updated_count


# ---------------------------------------------------------------------------
# Peloton sync runners
# ---------------------------------------------------------------------------

def _run_peloton_sync_all():
    limit = 100
    page = 0
    total_created = total_updated = total_on_peloton = 0
    try:
        client = _client()
        while True:
            raw = client.get_workouts(limit=limit, page=page)
            data = raw.get("data", [])
            total_on_peloton = raw.get("total", 0)
            if not data:
                break
            c, u = _upsert_page(data)
            total_created += c
            total_updated += u
            page_ids = [w.get("id") for w in data if w.get("id")]
            unsynced = list(
                CachedWorkout.objects
                .filter(workout_id__in=page_ids, detail_synced_at__isnull=True)
                .values_list("workout_id", flat=True)
            )
            if unsynced:
                _fetch_and_store_details(unsynced, client)
            _fetch_and_store_performance(page_ids, client)
            page += 1
            if page * limit >= total_on_peloton:
                break
        return {
            "done": True,
            "total_on_peloton": total_on_peloton,
            "created": total_created,
            "updated": total_updated,
            "pages_fetched": page,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_peloton_sync_new(days=None):
    cutoff_dt = None
    if days:
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=int(days))

    existing_ids = set(CachedWorkout.objects.values_list("workout_id", flat=True))
    limit = 100
    page = 0
    total_created = total_updated = 0
    try:
        client = _client()
        while True:
            raw = client.get_workouts(limit=limit, page=page, sort_by="-created")
            data = raw.get("data", [])
            if not data:
                break
            page_workouts = []
            stop = False
            for workout in data:
                wid = workout.get("id")
                created_ts = workout.get("created_at") or workout.get("start_time")
                workout_dt = datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None
                if cutoff_dt and workout_dt and workout_dt < cutoff_dt:
                    stop = True
                    break
                if not days and wid in existing_ids:
                    stop = True
                    break
                page_workouts.append(workout)
            if page_workouts:
                c, u = _upsert_page(page_workouts)
                total_created += c
                total_updated += u
                page_workout_ids = [w.get("id") for w in page_workouts if w.get("id")]
                _fetch_and_store_details(page_workout_ids, client)
                _fetch_and_store_performance(page_workout_ids, client)
            if stop or len(data) < limit:
                break
            page += 1
        return {"done": True, "created": total_created, "updated": total_updated, "pages_fetched": page + 1}
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


# ---------------------------------------------------------------------------
# Garmin deduplication helpers
# ---------------------------------------------------------------------------

def _peloton_timestamp_index():
    """Sorted list of Unix timestamps for all Peloton-sourced workouts.
    Used to detect Garmin activities that duplicate Peloton workouts."""
    return sorted(
        int(dt.timestamp())
        for dt in CachedWorkout.objects
        .filter(source="peloton")
        .values_list("created_at", flat=True)
        if dt is not None
    )


def _is_peloton_duplicate(garmin_start_dt, peloton_timestamps, window_seconds=120):
    """True if garmin_start_dt is within window_seconds of any Peloton workout."""
    if garmin_start_dt is None or not peloton_timestamps:
        return False
    ts = int(garmin_start_dt.timestamp())
    pos = bisect.bisect_left(peloton_timestamps, ts - window_seconds)
    return pos < len(peloton_timestamps) and peloton_timestamps[pos] <= ts + window_seconds


# ---------------------------------------------------------------------------
# Garmin form augmentation
# ---------------------------------------------------------------------------

def _augment_peloton_run(parsed: dict, garmin_activity_id: int, client) -> None:
    """
    When a Garmin running activity duplicates an existing Peloton workout, stamp
    the Peloton record with walking-filtered Garmin running form metrics so they
    appear on the run detail page. Also merges the Garmin form metric time-series
    into performance_graph_json.
    """
    garmin_start = parsed.get("created_at")
    if not garmin_start:
        return
    window = timedelta(seconds=120)
    match = CachedWorkout.objects.filter(
        source="peloton",
        discipline="running",
        created_at__range=(garmin_start - window, garmin_start + window),
    ).first()
    if not match:
        return

    # Cadence threshold for filtering walking from running averages.
    RUN_CADENCE_MIN = 140
    FORM_METRIC_KEYS = {
        "directStrideLength":         ("stride_length",         "stride_length_avg"),
        "directVerticalOscillation":  ("vertical_oscillation",  "vertical_oscillation_avg"),
        "directVerticalRatio":        ("vertical_ratio",        "vertical_ratio_avg"),
        "directGroundContactTime":    ("ground_contact_time",   "ground_contact_time_avg"),
    }

    try:
        details = client.get_activity_details(garmin_activity_id)
        descs = details.get("metricDescriptors") or []
        pts = details.get("activityDetailMetrics") or []

        cad_idx = next((d["metricsIndex"] for d in descs if d["key"] == "directDoubleCadence"), None)
        form_idx: dict[str, tuple[int, float, str]] = {}  # slug → (index, factor, field_name)
        for d in descs:
            entry = FORM_METRIC_KEYS.get(d["key"])
            if entry:
                slug, field = entry
                factor = (d.get("unit") or {}).get("factor") or 1.0
                form_idx[slug] = (d["metricsIndex"], factor, field)

        run_cads: list[float] = []
        form_running_vals: dict[str, list[float]] = {s: [] for s in form_idx}

        if cad_idx is not None:
            for pt in pts:
                metrics = pt.get("metrics") or []
                if cad_idx >= len(metrics) or metrics[cad_idx] is None:
                    continue
                cad = metrics[cad_idx]
                if cad < RUN_CADENCE_MIN:
                    continue
                run_cads.append(cad)
                for slug, (idx, factor, _) in form_idx.items():
                    if idx < len(metrics) and metrics[idx] is not None:
                        v = metrics[idx] / factor if factor != 1.0 else metrics[idx]
                        form_running_vals[slug].append(v)

        if run_cads:
            match.run_cadence_avg = round(sum(run_cads) / len(run_cads), 1)
        for slug, (_, _, field) in form_idx.items():
            vals = form_running_vals[slug]
            if vals:
                setattr(match, field, round(sum(vals) / len(vals), 2))

        # Parse full-resolution time-series for garmin_form_json.
        # Override average_value with the walking-filtered value so chart tooltips are accurate.
        garmin_perf_raw = GarminClient.parse_performance_raw(details)

        FORM_SLUGS = {"stride_length", "vertical_oscillation", "vertical_ratio", "ground_contact_time", "heart_rate"}
        garmin_form = {
            slug: garmin_perf_raw["metrics_by_slug"][slug]
            for slug in FORM_SLUGS
            if slug in garmin_perf_raw.get("metrics_by_slug", {})
        } if garmin_perf_raw else {}

        for slug, (_, _, field) in form_idx.items():
            if slug in garmin_form and getattr(match, field, None) is not None:
                garmin_form[slug]["average_value"] = getattr(match, field)

        # Store the sumElapsedDuration array so _apply_garmin_form can map
        # each point to its actual timestamp rather than assuming 1s resolution.
        elapsed_idx = next(
            (d["metricsIndex"] for d in descs if d["key"] == "sumElapsedDuration"), None
        )
        if elapsed_idx is not None:
            elapsed_times = [
                (pt.get("metrics") or [])[elapsed_idx]
                if elapsed_idx < len(pt.get("metrics") or []) else None
                for pt in pts
            ]
            for slug in garmin_form:
                garmin_form[slug]["elapsed"] = elapsed_times

        CachedWorkout.objects.filter(pk=match.pk).update(
            run_cadence_avg=match.run_cadence_avg,
            stride_length_avg=match.stride_length_avg,
            vertical_oscillation_avg=match.vertical_oscillation_avg,
            vertical_ratio_avg=match.vertical_ratio_avg,
            ground_contact_time_avg=match.ground_contact_time_avg,
            garmin_activity_id=garmin_activity_id,
            garmin_activity_start=garmin_start,
            garmin_form_json=garmin_form,
        )
        match.garmin_activity_start = garmin_start
        match.garmin_form_json = garmin_form

        _apply_garmin_form(match)
    except Exception as e:
        # Fallback: use Garmin summary averages if the time-series fetch fails.
        for f in ("stride_length_avg", "vertical_oscillation_avg", "vertical_ratio_avg", "ground_contact_time_avg"):
            v = parsed.get(f)
            if v is not None:
                setattr(match, f, v)
        match.save(update_fields=["stride_length_avg", "vertical_oscillation_avg",
                                   "vertical_ratio_avg", "ground_contact_time_avg"])
        logger.warning("Garmin form perf fetch failed for %s: %s", match.workout_id, e)


def _find_hr_offset(garmin_form: dict, peloton_perf: dict, max_offset: int = 300) -> int | None:
    """
    Cross-correlate Garmin and Peloton HR time-series to find how many seconds into
    the Garmin recording Peloton's t=0 occurs.

    Services like syncmyworkout.com modify a Garmin activity's startTimeGMT to match
    the Peloton class start, so the stored garmin_activity_start is unreliable.  Both
    devices record HR for the same physical session, so their HR traces are strongly
    correlated and can pinpoint the true alignment.

    Returns the best-fit offset in whole seconds (≥ 0), or None if HR data is absent
    or peak correlation is below 0.7 (inconclusive).
    """
    garmin_hr = garmin_form.get("heart_rate") or {}
    g_vals = garmin_hr.get("values") or []
    g_elapsed = garmin_hr.get("elapsed") or []
    p_vals = ((peloton_perf.get("metrics_by_slug") or {}).get("heart_rate") or {}).get("values") or []

    if not g_vals or not p_vals:
        return None

    every_n = peloton_perf.get("every_n", 5)

    # Expand Peloton HR to 1s resolution.
    peloton_1s: list = []
    for v in p_vals:
        peloton_1s.extend([v] * every_n)
    n = len(peloton_1s)

    # Resample Garmin HR to 1s using sumElapsedDuration if available.
    if g_elapsed and len(g_elapsed) == len(g_vals):
        max_t = int(g_elapsed[-1]) + 2 if g_elapsed[-1] is not None else len(g_vals)
        garmin_1s: list = []
        for t in range(max_t):
            gi = bisect.bisect_left(g_elapsed, t)
            if gi >= len(g_elapsed):
                gi = len(g_elapsed) - 1
            elif gi > 0 and abs(g_elapsed[gi - 1] - t) < abs(g_elapsed[gi] - t):
                gi -= 1
            garmin_1s.append(g_vals[gi])
    else:
        garmin_1s = list(g_vals)

    best_corr = -2.0
    best_offset = 0
    upper = min(max_offset + 1, len(garmin_1s) - n + 1)
    if upper <= 0:
        return None

    for offset in range(upper):
        segment = garmin_1s[offset : offset + n]
        pairs = [(p, g) for p, g in zip(peloton_1s, segment) if p is not None and g is not None]
        if len(pairs) < 120:  # need ≥ 2 min of paired data
            continue
        p_list = [p for p, _ in pairs]
        g_list = [g for _, g in pairs]
        m = len(p_list)
        p_mean = sum(p_list) / m
        g_mean = sum(g_list) / m
        num = sum((p - p_mean) * (g - g_mean) for p, g in zip(p_list, g_list))
        p_var = sum((p - p_mean) ** 2 for p in p_list)
        g_var = sum((g - g_mean) ** 2 for g in g_list)
        if p_var < 1.0 or g_var < 1.0:
            continue
        corr = num / (p_var * g_var) ** 0.5
        if corr > best_corr:
            best_corr = corr
            best_offset = offset

    return best_offset if best_corr >= 0.7 else None


def _apply_garmin_form(match: "CachedWorkout") -> None:
    """
    Resample and align cached Garmin form metrics into the Peloton performance graph.
    Reads from match.garmin_form_json and match.garmin_activity_start — no API calls.

    Offset detection priority:
    1. HR cross-correlation via _find_hr_offset (robust against syncmyworkout.com
       overwriting startTimeGMT with the Peloton class start time).
    2. Timestamp difference: garmin_activity_start vs created_at.
    3. Zero.

    The detected offset is stored in garmin_offset_seconds on the model.
    """
    if not match.garmin_form_json or not match.performance_graph_json:
        return

    existing_perf = match.performance_graph_json
    peloton_every_n = existing_perf.get("every_n", 5)

    peloton_len = 0
    for m in existing_perf.get("metrics_by_slug", {}).values():
        v = m.get("values") or []
        if v:
            peloton_len = len(v)
            break
    if not peloton_len:
        return

    # Detect offset via HR cross-correlation; fall back to timestamp difference.
    detected = _find_hr_offset(match.garmin_form_json, existing_perf)
    if detected is not None:
        offset_secs = float(detected)
    elif match.garmin_activity_start and match.created_at:
        offset_secs = max(0.0, (match.created_at - match.garmin_activity_start).total_seconds())
    else:
        offset_secs = 0.0

    # heart_rate lives in garmin_form_json for offset detection only;
    # Peloton's own HR is authoritative for the chart.
    SKIP_IN_PERF = {"heart_rate"}

    merged = False
    for slug, garmin_metric in match.garmin_form_json.items():
        if slug in SKIP_IN_PERF:
            continue
        garmin_values = garmin_metric.get("values") or []
        elapsed = garmin_metric.get("elapsed")  # sumElapsedDuration per point, or None
        if not garmin_values:
            continue

        resampled = []
        if elapsed and len(elapsed) == len(garmin_values):
            # Map each Peloton sample to the Garmin point whose elapsed time is
            # closest to (peloton_t + offset_secs), using binary search.
            for i in range(peloton_len):
                target = offset_secs + i * peloton_every_n
                gi = bisect.bisect_left(elapsed, target)
                if gi >= len(elapsed):
                    resampled.append(None)  # Garmin data ran out
                else:
                    if gi > 0 and abs(elapsed[gi - 1] - target) < abs(elapsed[gi] - target):
                        gi -= 1
                    resampled.append(garmin_values[gi])
        else:
            # Legacy fallback: assume 1s resolution
            step = peloton_every_n
            offset_pts = round(offset_secs)
            for i in range(peloton_len):
                gi = offset_pts + round(i * step)
                resampled.append(garmin_values[gi] if gi < len(garmin_values) else None)

        existing_perf.setdefault("metrics_by_slug", {})[slug] = {**garmin_metric, "values": resampled}
        merged = True

    update_fields: dict = {"garmin_offset_seconds": round(offset_secs)}
    if merged:
        update_fields["performance_graph_json"] = existing_perf
    CachedWorkout.objects.filter(pk=match.pk).update(**update_fields)


# ---------------------------------------------------------------------------
# Garmin activity upsert + extras
# ---------------------------------------------------------------------------

def _upsert_garmin_activity(parsed: dict) -> tuple[bool, bool]:
    """Insert or update a CachedWorkout from a parsed Garmin activity dict.
    Returns (created, updated)."""
    wid = parsed["workout_id"]
    existing = CachedWorkout.objects.filter(workout_id=wid).first()
    if existing:
        for field, value in parsed.items():
            if field != "raw_data" and value is not None:
                setattr(existing, field, value)
        existing.source = "garmin"
        existing.save()
        return False, True
    obj = CachedWorkout.from_garmin(parsed)
    obj.save()
    return True, False


def _fetch_garmin_extra(wid: str, garmin_id: int, client, discipline: str) -> None:
    """Fetch performance graph, HR zones, splits, and exercise sets for one Garmin activity."""
    update_perf: dict = {}
    update_direct: dict = {}

    # Performance graph (time-series metrics)
    try:
        details = client.get_activity_details(garmin_id)
        perf = client.parse_performance(details)
        if perf:
            update_perf = perf
    except Exception as e:
        logger.warning("Garmin perf fetch failed for %s: %s", wid, e)

    # HR zones → hr_z1_seconds–hr_z5_seconds
    try:
        hr_data = client.get_hr_zones(garmin_id)
        zones = client.parse_hr_zones(hr_data)
        if zones:
            update_direct.update({
                "hr_z1_seconds": zones.get("z1"),
                "hr_z2_seconds": zones.get("z2"),
                "hr_z3_seconds": zones.get("z3"),
                "hr_z4_seconds": zones.get("z4"),
                "hr_z5_seconds": zones.get("z5"),
            })
    except Exception as e:
        logger.warning("Garmin HR zones fetch failed for %s: %s", wid, e)

    # Splits → stored inside performance_graph_json
    try:
        splits_data = client.get_splits(garmin_id)
        splits = client.parse_splits(splits_data)
        if splits:
            update_perf["splits"] = splits
    except Exception as e:
        logger.warning("Garmin splits fetch failed for %s: %s", wid, e)

    # Exercise sets (strength only)
    if discipline == "strength":
        try:
            sets_data = client.get_exercise_sets(garmin_id)
            sets = client.parse_exercise_sets(sets_data)
            if sets:
                update_direct["exercise_sets_json"] = sets
        except Exception as e:
            logger.warning("Garmin exercise sets fetch failed for %s: %s", wid, e)

    if update_perf:
        CachedWorkout.objects.filter(workout_id=wid).update(performance_graph_json=update_perf)
    if update_direct:
        CachedWorkout.objects.filter(workout_id=wid).update(**update_direct)


# ---------------------------------------------------------------------------
# Garmin sync runners
# ---------------------------------------------------------------------------

def _run_garmin_sync_new():
    existing_ids = set(
        CachedWorkout.objects.filter(source="garmin").values_list("workout_id", flat=True)
    )
    peloton_timestamps = _peloton_timestamp_index()
    limit = 100
    start = 0
    total_created = total_updated = total_skipped = 0
    try:
        client = _garmin_client()
        while True:
            activities = client.get_activities(limit=limit, start=start)
            if not activities:
                break
            stop = False
            for activity in activities:
                wid = f"garmin_{activity['activityId']}"
                if wid in existing_ids:
                    stop = True
                    break
                parsed = client.parse_activity(activity)
                if _is_peloton_duplicate(parsed.get("created_at"), peloton_timestamps):
                    if parsed.get("discipline") == "running":
                        _augment_peloton_run(parsed, activity["activityId"], client)
                    total_skipped += 1
                    continue
                created, updated = _upsert_garmin_activity(parsed)
                total_created += created
                total_updated += updated
                _fetch_garmin_extra(wid, activity["activityId"], client, parsed.get("discipline", ""))
            if stop or len(activities) < limit:
                break
            start += limit
        return {
            "done": True,
            "created": total_created,
            "updated": total_updated,
            "skipped_peloton_duplicates": total_skipped,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_garmin_sync_all():
    peloton_timestamps = _peloton_timestamp_index()
    limit = 100
    start = 0
    total_created = total_updated = total_skipped = 0
    try:
        client = _garmin_client()
        while True:
            activities = client.get_activities(limit=limit, start=start)
            if not activities:
                break
            for activity in activities:
                wid = f"garmin_{activity['activityId']}"
                parsed = client.parse_activity(activity)
                if _is_peloton_duplicate(parsed.get("created_at"), peloton_timestamps):
                    if parsed.get("discipline") == "running":
                        _augment_peloton_run(parsed, activity["activityId"], client)
                    total_skipped += 1
                    continue
                created, updated = _upsert_garmin_activity(parsed)
                total_created += created
                total_updated += updated
                _fetch_garmin_extra(wid, activity["activityId"], client, parsed.get("discipline", ""))
            if len(activities) < limit:
                break
            start += limit
        return {
            "done": True,
            "created": total_created,
            "updated": total_updated,
            "skipped_peloton_duplicates": total_skipped,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_wellness_sync(dates):
    synced = errors = 0
    try:
        client = _garmin_client()
    except Exception as e:
        return {"error": str(e), "synced": 0, "errors": len(dates)}
    for d in dates:
        date_str = d.isoformat()
        try:
            data = client.get_wellness_data(date_str)
            stats, _ = DailyStats.objects.get_or_create(date=d)
            for field, value in data.items():
                setattr(stats, field, value)
            stats.synced_at = tz.now()
            stats.save()
            synced += 1
        except Exception as e:
            logger.warning("Wellness sync failed for %s: %s", date_str, e)
            errors += 1

    # When syncing today, backfill yesterday's body_battery_end if it's missing.
    # Yesterday is a completed day so bodyBatteryMostRecentValue = end-of-night value.
    today = date.today()
    if today in dates:
        yesterday = today - timedelta(days=1)
        yesterday_stats = DailyStats.objects.filter(date=yesterday, body_battery_end__isnull=True).first()
        if yesterday_stats:
            try:
                data = client.get_wellness_data(yesterday.isoformat())
                for field, value in data.items():
                    setattr(yesterday_stats, field, value)
                yesterday_stats.synced_at = tz.now()
                yesterday_stats.save()
                synced += 1
            except Exception as e:
                logger.warning("Yesterday body battery backfill failed for %s: %s", yesterday, e)

    return {"done": True, "synced": synced, "errors": errors}


# ---------------------------------------------------------------------------
# Sync API endpoints
# ---------------------------------------------------------------------------

def sync_new(request):
    """Combined sync: new Peloton workouts + new Garmin activities + today's wellness."""
    peloton = _run_peloton_sync_new()
    garmin = _run_garmin_sync_new()
    wellness = _run_wellness_sync([date.today()])
    return JsonResponse({"peloton": peloton, "garmin": garmin, "wellness": wellness})


def sync_all(request):
    """Combined sync: all Peloton workouts + all Garmin activities + last 30 days of wellness."""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(30)]
    peloton = _run_peloton_sync_all()
    garmin = _run_garmin_sync_all()
    wellness = _run_wellness_sync(dates)
    return JsonResponse({"peloton": peloton, "garmin": garmin, "wellness": wellness})


def sync_all_workouts(request):
    return JsonResponse(_run_peloton_sync_all())


def sync_new_workouts(request):
    days = request.GET.get("days")
    return JsonResponse(_run_peloton_sync_new(days=days))


def sync_garmin_new(request):
    return JsonResponse(_run_garmin_sync_new())


def sync_garmin_all(request):
    return JsonResponse(_run_garmin_sync_all())


def sync_garmin_wellness(request):
    days_param = request.GET.get("days")
    date_param = request.GET.get("date")
    today = date.today()
    if days_param:
        n = min(int(days_param), 90)
        dates = [today - timedelta(days=i) for i in range(n)]
    elif date_param:
        try:
            dates = [date.fromisoformat(date_param)]
        except ValueError:
            return JsonResponse({"error": "invalid date format, use YYYY-MM-DD"}, status=400)
    else:
        dates = [today]
    return JsonResponse(_run_wellness_sync(dates))


# ---------------------------------------------------------------------------
# Withings body composition helpers
# ---------------------------------------------------------------------------

def _upsert_measurements(measurements: list[dict]) -> tuple[int, int]:
    """Upsert normalized measurement dicts into BodyMeasurement. Returns (created, updated)."""
    from django.db.models import Max
    created_count = 0
    updated_count = 0
    for m in measurements:
        grpid = m.get("grpid", "")
        if not grpid:
            logger.warning("Withings measurement missing grpid — skipping: %s", m)
            continue
        measured_at = m["measured_at"]
        local_date = measured_at.astimezone().date()
        defaults = {
            "measured_at": measured_at,
            "date": local_date,
            "source": "withings",
            "weight_lb": m.get("weight_lb"),
            "fat_mass_lb": m.get("fat_mass_lb"),
            "fat_free_mass_lb": m.get("fat_free_mass_lb"),
            "muscle_mass_lb": m.get("muscle_mass_lb"),
            "bone_mass_lb": m.get("bone_mass_lb"),
            "hydration_lb": m.get("hydration_lb"),
            "fat_ratio_pct": m.get("fat_ratio_pct"),
            "raw_data": m.get("raw", {}),
        }
        _, created = BodyMeasurement.objects.update_or_create(
            source="withings",
            withings_grpid=grpid,
            defaults=defaults,
        )
        if created:
            created_count += 1
        else:
            updated_count += 1
    return created_count, updated_count


def _update_daily_stats_for_dates(dates: list) -> None:
    """
    For each date, recompute DailyStats body composition fields from BodyMeasurement rows.
    Uses the earliest weigh-in of the day (first measurement by measured_at) for each metric.
    Sets weight_synced_at = now().
    """
    now = tz.now()
    for d in dates:
        first = BodyMeasurement.objects.filter(date=d).order_by("measured_at").first()
        count = BodyMeasurement.objects.filter(date=d).count()
        stats, _ = DailyStats.objects.get_or_create(date=d)
        if first:
            stats.weight_lb = first.weight_lb
            stats.fat_mass_lb = first.fat_mass_lb
            stats.fat_free_mass_lb = first.fat_free_mass_lb
            stats.muscle_mass_lb = first.muscle_mass_lb
            stats.hydration_lb = first.hydration_lb
            stats.bone_mass_lb = first.bone_mass_lb
            stats.fat_ratio_pct = first.fat_ratio_pct
        stats.weight_count = count
        stats.weight_synced_at = now
        stats.save(update_fields=[
            "weight_lb", "fat_mass_lb", "fat_free_mass_lb", "muscle_mass_lb",
            "hydration_lb", "bone_mass_lb", "fat_ratio_pct",
            "weight_count", "weight_synced_at",
        ])


def _run_withings_sync_new() -> dict:
    """
    Pull measurements since last sync (lastupdate from max measured_at in DB,
    or 30 days ago if no rows exist). Returns summary dict.
    """
    from django.db.models import Max
    result = BodyMeasurement.objects.aggregate(max_date=Max("measured_at"))
    if result["max_date"]:
        lastupdate = int(result["max_date"].timestamp())
    else:
        lastupdate = int((datetime.now(tz=timezone.utc) - timedelta(days=30)).timestamp())

    total_created = total_updated = 0
    try:
        client = _withings_client()
        measurements = client.get_measurements(lastupdate=lastupdate)
        if measurements:
            total_created, total_updated = _upsert_measurements(measurements)
            dates = list({m["measured_at"].astimezone().date() for m in measurements})
            _update_daily_stats_for_dates(dates)
        return {
            "done": True,
            "fetched": len(measurements),
            "created": total_created,
            "updated": total_updated,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_withings_sync_all() -> dict:
    """
    Pull ALL historical measurements via pagination. For first-time setup.
    Returns summary dict.
    """
    total_created = total_updated = 0
    try:
        client = _withings_client()
        measurements = client.get_measurements()
        if measurements:
            total_created, total_updated = _upsert_measurements(measurements)
            dates = list({m["measured_at"].astimezone().date() for m in measurements})
            _update_daily_stats_for_dates(dates)
        return {
            "done": True,
            "fetched": len(measurements),
            "created": total_created,
            "updated": total_updated,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_withings_sync_range(start: date, end: date) -> dict:
    """Pull measurements for a specific date range (inclusive). Returns summary dict."""
    total_created = total_updated = 0
    try:
        client = _withings_client()
        start_epoch = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        end_epoch   = int(datetime(end.year,   end.month,   end.day,   23, 59, 59, tzinfo=timezone.utc).timestamp())
        measurements = client.get_measurements(start_date=start_epoch, end_date=end_epoch)
        if measurements:
            total_created, total_updated = _upsert_measurements(measurements)
            dates = list({m["measured_at"].astimezone().date() for m in measurements})
            _update_daily_stats_for_dates(dates)
        return {
            "done": True,
            "start": str(start),
            "end": str(end),
            "fetched": len(measurements),
            "created": total_created,
            "updated": total_updated,
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def sync_withings_new(request):
    """POST /api/sync/withings/new/"""
    return JsonResponse(_run_withings_sync_new())


def sync_withings_all(request):
    """POST /api/sync/withings/all/"""
    return JsonResponse(_run_withings_sync_all())
