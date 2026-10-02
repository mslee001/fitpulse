"""
Sync logic for Peloton and Garmin Connect data.

Contains all data-fetching helpers, upsert logic, and the URL-registered
sync endpoints. Nothing in here renders HTML — every public function returns
a JsonResponse.
"""

import bisect
import hmac
import json
import logging
import os
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone as tz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import programs as _programs
from .models import BodyMeasurement, CachedWorkout, DailyStats, Integration, UserSettings, WebhookError
from .services.garmin_client import GarminClient
from .services.peloton_client import PelotonClient
from .services.withings_client import WithingsClient

logger = logging.getLogger(__name__)

# Disciplines that have a Peloton performance graph worth fetching.
_PERF_DISCS = {"cycling", "bike_bootcamp", "running", "outdoor_running", "strength", "walking"}


def _associate_program_safe(workout):
    """Best-effort Program association — a matcher bug must never break a sync."""
    try:
        _programs.associate_workout(workout)
    except Exception:
        logger.exception("program association failed for workout %s", getattr(workout, "pk", "?"))


def _reconcile_programs_safe():
    """Best-effort catch-up of program "any class" matches and recovery attachments
    (cool-down walks/stretches) — run after syncs, since a recovery session usually
    syncs after its workout. Never raises: must not break a sync."""
    try:
        return _programs.reconcile_program_extras()
    except Exception:
        logger.exception("program reconcile (any-class slots / recoveries) failed")
        return None


def _integration_enabled(key: str) -> bool:
    """
    True if Integration(key=key).is_enabled, defaulting to True if the row
    is somehow missing (fail open rather than silently blocking sync for an
    integration that predates the Integration model, e.g. if a migration
    hasn't run yet in some environment).
    """
    from .models import Integration
    return Integration.objects.filter(key=key).values_list("is_enabled", flat=True).first() is not False


def _integration_disabled_result(key: str) -> dict:
    return {"done": True, "skipped": True, "reason": f"{key} integration is disabled in /settings/integrations/"}


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

# Disciplines whose class exercise plan is worth fetching (one extra API call
# per distinct class) — the ones where "which exercises" is the interesting part.
_CLASS_PLAN_DISCS = {"strength", "circuit"}


def _fetch_and_store_details(workout_ids, client):
    """Fetch /api/workout/:id for each ID and persist detail fields to the DB.
    Also fetches the class exercise plan for strength/circuit workouts, cached
    per ride_id within this call so repeat takes of a class cost one request."""
    plan_cache = {}
    for wid in workout_ids:
        try:
            detail = client.get_parsed_workout_detail(wid)
            w = CachedWorkout.objects.get(workout_id=wid)
            if w.discipline in _CLASS_PLAN_DISCS and w.ride_id:
                try:
                    if w.ride_id not in plan_cache:
                        plan_cache[w.ride_id] = client.get_class_plan(w.ride_id)
                    detail["class_plan"] = plan_cache[w.ride_id]
                except Exception as e:
                    logger.warning("class plan fetch failed for %s: %s", wid, e)
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
                qs = CachedWorkout.objects.filter(workout_id=wid)
                update_fields = {"performance_graph_json": perf}
                if not qs.filter(user_corrected=True).exists():
                    update_fields.update(_extract_perf_fields(perf))
                qs.update(**update_fields)
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
    corrected = set(
        CachedWorkout.objects
        .filter(workout_id__in=[w.get("id") for w in raw_data], user_corrected=True)
        .values_list("workout_id", flat=True)
    )
    for workout_data in raw_data:
        obj = CachedWorkout.from_api(workout_data)
        defaults = {field: getattr(obj, field) for field in fields}
        if obj.workout_id in corrected:
            for field in CachedWorkout.CORRECTABLE_FIELDS:
                defaults.pop(field, None)
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
    if not _integration_enabled("peloton"):
        return _integration_disabled_result("peloton")
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
            # oldest-first: fill_or_append's pass-placement logic assumes strict
            # date order, but CachedWorkout's default ordering is newest-first
            for w in CachedWorkout.objects.filter(workout_id__in=page_ids).order_by("created_at"):
                _associate_program_safe(w)
            page += 1
            if page * limit >= total_on_peloton:
                break
        reconciled = _reconcile_google_health_duplicates()
        garmin_reconciled = _reconcile_garmin_duplicates()
        _reconcile_programs_safe()
        Integration.objects.filter(key="peloton").update(last_synced_at=tz.now())
        return {
            "done": True,
            "total_on_peloton": total_on_peloton,
            "created": total_created,
            "updated": total_updated,
            "pages_fetched": page,
            "google_health_merged": reconciled["deleted"],
            "garmin_merged": garmin_reconciled["deleted"],
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_peloton_sync_new(days=None):
    if not _integration_enabled("peloton"):
        return _integration_disabled_result("peloton")
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
                # oldest-first: fill_or_append's pass-placement logic assumes strict
                # date order, but CachedWorkout's default ordering is newest-first
                for w in CachedWorkout.objects.filter(workout_id__in=page_workout_ids).order_by("created_at"):
                    _associate_program_safe(w)
            if stop or len(data) < limit:
                break
            page += 1
        reconciled = _reconcile_google_health_duplicates()
        garmin_reconciled = _reconcile_garmin_duplicates()
        _reconcile_programs_safe()
        Integration.objects.filter(key="peloton").update(last_synced_at=tz.now())
        return {
            "done": True,
            "created": total_created,
            "updated": total_updated,
            "pages_fetched": page + 1,
            "google_health_merged": reconciled["deleted"],
            "garmin_merged": garmin_reconciled["deleted"],
        }
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
        _associate_program_safe(match)
    except Exception as e:
        # Fallback: use Garmin summary averages if the time-series fetch fails.
        for f in ("stride_length_avg", "vertical_oscillation_avg", "vertical_ratio_avg", "ground_contact_time_avg"):
            v = parsed.get(f)
            if v is not None:
                setattr(match, f, v)
        match.save(update_fields=["stride_length_avg", "vertical_oscillation_avg",
                                   "vertical_ratio_avg", "ground_contact_time_avg"])
        logger.warning("Garmin form perf fetch failed for %s: %s", match.workout_id, e)
        _associate_program_safe(match)


_GARMIN_FORM_FIELDS = ["run_cadence_avg", "stride_length_avg", "vertical_oscillation_avg",
                       "vertical_ratio_avg", "ground_contact_time_avg"]

# Fields from _parse_google_health_exercise() that map 1:1 onto an identically
# named CachedWorkout attribute, so "fill if Peloton's copy is missing it" can
# be one loop instead of a repeated if-block per field. heart_rate_avg is
# handled separately in _augment_peloton_from_google_health because the
# missing-ness check has to go through the heart_rate_avg_best property, not
# the raw field, to match how the rest of the app reads HR.
_GOOGLE_HEALTH_FILLABLE_FIELDS = [
    "calories", "distance_miles", "avg_pace_seconds", "avg_speed_mph", "elevation_gain",
] + _GARMIN_FORM_FIELDS


def _reconcile_garmin_duplicates(dry_run=False) -> dict:
    """
    Re-check every existing source="garmin" CachedWorkout row against the
    current set of Peloton workouts and merge/delete any new match.

    Needed because Garmin sync's dedup (_is_peloton_duplicate) only checks
    against Peloton workouts that exist *at that moment* — a one-time
    snapshot taken at the start of each Garmin sync run (_peloton_timestamp_index()).
    If Garmin syncs before the matching Peloton workout does, the two land as
    separate rows and nothing re-checks them afterward — Peloton sync never
    looked at garmin rows at all. Calling this after every Peloton sync
    closes that gap, the same way _reconcile_google_health_duplicates does
    for Google Health, so sync_daily no longer has to run Peloton before
    Garmin for correctness.

    Running duplicates: re-run the live-sync augmentation (_augment_peloton_run)
    against the already-known garmin_activity_id, so fidelity (HR
    cross-correlation offset detection, full-resolution form metrics) matches
    what a same-order sync would have produced. Requires one live Garmin API
    call per match — the already-stored performance_graph_json on the garmin
    row is downsampled (parse_performance, every 5th sample, no per-point
    elapsed timestamps) and isn't precise enough for offset alignment. If the
    Garmin integration is disabled (or the API call fails), that row is left
    alone rather than deleted, so its form data isn't lost — it'll be picked
    up on a later reconciliation once Garmin is available again.
    Non-running duplicates: Peloton's own data is authoritative (same policy
    as the live sync path) — just delete the redundant Garmin row. No API
    call needed, so this runs even with the Garmin integration disabled.

    Returns {"checked", "matched", "augmented", "deleted", "details"}, where
    each entry in "details" is {"garmin_workout_id", "peloton_workout_id",
    "peloton_title", "discipline", "filled", "skipped"}.
    """
    peloton_index = _peloton_workout_index()
    garmin_workouts = list(CachedWorkout.objects.filter(source="garmin").order_by("created_at"))

    matches = []
    for gw in garmin_workouts:
        match = _find_workout_match(gw.created_at, peloton_index)
        if match is not None:
            matches.append((gw, match))

    garmin_enabled = _integration_enabled("garmin")
    client = None
    if not dry_run and garmin_enabled and any(gw.discipline == "running" for gw, _ in matches):
        try:
            client = _garmin_client()
        except Exception as e:
            logger.warning("Garmin reconciliation: couldn't create client for augmentation: %s", e)

    augmented = 0
    to_delete = []
    details = []
    for gw, match in matches:
        filled = []
        skipped = False
        if gw.discipline == "running":
            if not garmin_enabled:
                skipped = True
            elif dry_run:
                before = {f: getattr(match, f) for f in _GARMIN_FORM_FIELDS}
                filled = [f for f in _GARMIN_FORM_FIELDS if before[f] is None]
            elif client is not None:
                try:
                    garmin_activity_id = int(gw.workout_id.removeprefix("garmin_"))
                    before = {f: getattr(match, f) for f in _GARMIN_FORM_FIELDS}
                    _augment_peloton_run({"created_at": gw.created_at}, garmin_activity_id, client)
                    match.refresh_from_db()
                    filled = [f for f in _GARMIN_FORM_FIELDS
                              if before[f] is None and getattr(match, f) is not None]
                    if filled:
                        augmented += 1
                except Exception as e:
                    logger.warning("Garmin reconciliation augment failed for %s: %s", gw.workout_id, e)
                    skipped = True
            else:
                skipped = True

        details.append({
            "garmin_workout_id": gw.workout_id,
            "peloton_workout_id": match.workout_id,
            "peloton_title": match.title,
            "discipline": gw.discipline,
            "filled": filled,
            "skipped": skipped,
        })
        if not skipped:
            to_delete.append(gw.pk)

    deleted = 0
    if not dry_run and to_delete:
        deleted = CachedWorkout.objects.filter(pk__in=to_delete).delete()[0]

    return {
        "checked": len(garmin_workouts),
        "matched": len(matches),
        "augmented": augmented,
        "deleted": deleted,
        "details": details,
    }


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
    if not _integration_enabled("garmin"):
        return _integration_disabled_result("garmin")
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
        gh_reconciled = _reconcile_garmin_google_health_duplicates()
        Integration.objects.filter(key="garmin").update(last_synced_at=tz.now())
        return {
            "done": True,
            "created": total_created,
            "updated": total_updated,
            "skipped_peloton_duplicates": total_skipped,
            "google_health_merged": gh_reconciled["deleted"],
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_garmin_sync_all():
    if not _integration_enabled("garmin"):
        return _integration_disabled_result("garmin")
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
        gh_reconciled = _reconcile_garmin_google_health_duplicates()
        Integration.objects.filter(key="garmin").update(last_synced_at=tz.now())
        return {
            "done": True,
            "created": total_created,
            "updated": total_updated,
            "skipped_peloton_duplicates": total_skipped,
            "google_health_merged": gh_reconciled["deleted"],
        }
    except Exception as e:
        return {"error": str(e), "created": total_created, "updated": total_updated}


def _run_wellness_sync(dates):
    if not _integration_enabled("garmin"):
        return {**_integration_disabled_result("garmin"), "synced": 0, "errors": 0}
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
            stats.wellness_source = "garmin"
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
                yesterday_stats.wellness_source = "garmin"
                yesterday_stats.synced_at = tz.now()
                yesterday_stats.save()
                synced += 1
            except Exception as e:
                logger.warning("Yesterday body battery backfill failed for %s: %s", yesterday, e)

    Integration.objects.filter(key="garmin").update(last_synced_at=tz.now())
    return {"done": True, "synced": synced, "errors": errors}


# ---------------------------------------------------------------------------
# Google Health wellness sync
#
# Field shapes below were confirmed live against a real Pixel Watch 3 account
# (see workouts/services/google_health_client.py's module docstring). A few
# fields are best-effort because the account had no live data to confirm the
# value-field name against (run VO2 max, active energy burned) — these log a
# warning if the sub-object is ever present-but-unrecognized, rather than
# silently dropping real data once the watch starts reporting it.
# ---------------------------------------------------------------------------

def _gh_civil_date(date_dict: dict):
    """{'year','month','day'} -> date."""
    return date(date_dict["year"], date_dict["month"], date_dict["day"])


def _gh_local_date(iso_utc: str, offset_str: str | None):
    """RFC3339 UTC timestamp + Google's '-28800s'-style offset -> local date."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    offset_seconds = int(offset_str.rstrip("s")) if offset_str else 0
    return (dt + timedelta(seconds=offset_seconds)).date()


def _gh_duration_seconds(start_iso: str, end_iso: str) -> int:
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return int((end - start).total_seconds())


def _gh_apply_resting_hr(point, daily):
    rhr = point.get("dailyRestingHeartRate", {})
    if not rhr.get("date"):
        return
    bpm = rhr.get("beatsPerMinute")
    if bpm is not None:
        daily[_gh_civil_date(rhr["date"])]["resting_hr"] = int(bpm)


def _gh_apply_daily_hrv(point, daily):
    dhrv = point.get("dailyHeartRateVariability", {})
    if not dhrv.get("date"):
        return
    avg = dhrv.get("averageHeartRateVariabilityMilliseconds")
    if avg is not None:
        daily[_gh_civil_date(dhrv["date"])]["hrv_last_night"] = round(float(avg), 1)


def _gh_apply_run_vo2_max(point, daily):
    rv = point.get("runVo2Max", {})
    sample_date = rv.get("sampleTime", {}).get("civilTime", {}).get("date")
    if not sample_date:
        return
    value = rv.get("vo2Max") or rv.get("vo2MaxValue") or rv.get("value")
    if value is None:
        if rv:
            logger.warning("Google Health run VO2 max: no recognized value field, keys=%s", list(rv.keys()))
        return
    daily[_gh_civil_date(sample_date)]["vo2_max_running"] = round(float(value), 1)


def _gh_apply_respiratory_rate(point, daily):
    drr = point.get("dailyRespiratoryRate", {})
    if not drr.get("date"):
        return
    bpm = drr.get("breathsPerMinute")
    if bpm is not None:
        daily[_gh_civil_date(drr["date"])]["respiration_avg"] = round(float(bpm), 1)


def _gh_apply_respiratory_sleep(point, daily):
    summary = point.get("respiratoryRateSleepSummary", {})
    sample_date = summary.get("sampleTime", {}).get("civilTime", {}).get("date")
    if not sample_date:
        return
    bpm = summary.get("fullSleepStats", {}).get("breathsPerMinute")
    if bpm is not None:
        daily[_gh_civil_date(sample_date)]["respiration_sleep_avg"] = round(float(bpm), 1)


def _gh_apply_oxygen_saturation(point, daily):
    dos = point.get("dailyOxygenSaturation", {})
    if not dos.get("date"):
        return
    d = _gh_civil_date(dos["date"])
    avg = dos.get("averagePercentage")
    low = dos.get("lowerBoundPercentage")
    if avg is not None:
        daily[d]["spo2_sleep_avg"] = round(float(avg), 1)
    if low is not None:
        daily[d]["spo2_sleep_low"] = int(round(float(low)))


def _gh_apply_sleep(point, daily):
    s = point.get("sleep", {})
    interval = s.get("interval", {})
    end_time = interval.get("endTime")
    start_time = interval.get("startTime")
    if not end_time or not start_time:
        return
    # Attributed to the wake-up day, matching DailyStats' "night leading into
    # this date" convention already used for Garmin sleep data.
    d = _gh_local_date(end_time, interval.get("endUtcOffset"))
    stages = s.get("stages", [])
    if stages:
        totals = {"AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0}
        for stage in stages:
            stage_type = stage.get("type")
            if stage_type in totals and stage.get("startTime") and stage.get("endTime"):
                totals[stage_type] += _gh_duration_seconds(stage["startTime"], stage["endTime"])
        daily[d]["sleep_deep_seconds"] = totals["DEEP"]
        daily[d]["sleep_light_seconds"] = totals["LIGHT"]
        daily[d]["sleep_rem_seconds"] = totals["REM"]
        daily[d]["sleep_seconds"] = totals["LIGHT"] + totals["DEEP"] + totals["REM"]
    else:
        daily[d]["sleep_seconds"] = _gh_duration_seconds(start_time, end_time)


def _gh_apply_steps(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    count = point.get("steps", {}).get("countSum")
    if count is not None:
        daily[_gh_civil_date(d_dict)]["steps"] = int(count)


def _gh_apply_floors(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    count = point.get("floors", {}).get("countSum")
    if count is not None:
        daily[_gh_civil_date(d_dict)]["floors_climbed"] = int(count)


def _gh_apply_active_calories(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    energy = point.get("activeEnergyBurned", {})
    kcal = energy.get("kcalSum")
    if kcal is not None:
        daily[_gh_civil_date(d_dict)]["active_calories"] = int(round(float(kcal)))
    elif energy:
        logger.warning("Google Health active energy burned: no kcalSum field, keys=%s", list(energy.keys()))


def _gh_apply_total_calories(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    kcal = point.get("totalCalories", {}).get("kcalSum")
    if kcal is not None:
        daily[_gh_civil_date(d_dict)]["total_calories"] = int(round(float(kcal)))


def _gh_apply_active_minutes(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    buckets = point.get("activeMinutes", {}).get("activeMinutesRollupByActivityLevel", [])
    moderate = vigorous = 0
    found = False
    for bucket in buckets:
        mins = bucket.get("activeMinutesSum")
        if mins is None:
            continue
        found = True
        if bucket.get("activityLevel") == "MODERATE":
            moderate += int(mins)
        elif bucket.get("activityLevel") == "VIGOROUS":
            vigorous += int(mins)
    if found:
        d = _gh_civil_date(d_dict)
        daily[d]["moderate_intensity_minutes"] = moderate
        daily[d]["vigorous_intensity_minutes"] = vigorous


def _gh_apply_intraday_hrv(point, daily):
    """HRV min/max for the day, accumulated across intraday
    (~5-min-granularity) readings — mirrors GarminClient.get_wellness_data's
    hrv_min/hrv_max derivation from its own intraday readings."""
    hrv = point.get("heartRateVariability", {})
    sample_date = hrv.get("sampleTime", {}).get("civilTime", {}).get("date")
    if not sample_date:
        return
    value = hrv.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds")
    if value is None:
        return
    d = _gh_civil_date(sample_date)
    value = int(round(value))
    existing_min = daily[d].get("hrv_min")
    existing_max = daily[d].get("hrv_max")
    daily[d]["hrv_min"] = value if existing_min is None else min(existing_min, value)
    daily[d]["hrv_max"] = value if existing_max is None else max(existing_max, value)


def _gh_apply_active_zone_minutes(point, daily):
    d_dict = point.get("civilStartTime", {}).get("date")
    if not d_dict:
        return
    azm = point.get("activeZoneMinutes", {})
    fat_burn = azm.get("sumInFatBurnHeartZone")
    cardio = azm.get("sumInCardioHeartZone")
    peak = azm.get("sumInPeakHeartZone")
    if fat_burn is None and cardio is None and peak is None:
        return
    # Fitbit's published AZM formula: fat-burn minutes count once, cardio and
    # peak minutes count double.
    total = int(fat_burn or 0) + 2 * int(cardio or 0) + 2 * int(peak or 0)
    daily[_gh_civil_date(d_dict)]["active_zone_minutes"] = total


def _gh_apply_sleep_temp(point, daily):
    st = point.get("dailySleepTemperatureDerivations", {})
    if not st.get("date"):
        return
    d = _gh_civil_date(st["date"])
    nightly = st.get("nightlyTemperatureCelsius")
    baseline = st.get("baselineTemperatureCelsius")
    if nightly is not None:
        daily[d]["skin_temp_c"] = round(float(nightly), 2)
    if nightly is not None and baseline is not None:
        daily[d]["skin_temp_deviation_c"] = round(float(nightly) - float(baseline), 2)


def _gh_interval_local_date(interval: dict):
    """civilStartTime.date if present, else derive from startTime + startUtcOffset."""
    d_dict = interval.get("civilStartTime", {}).get("date")
    if d_dict:
        return _gh_civil_date(d_dict)
    start_time = interval.get("startTime")
    if not start_time:
        return None
    return _gh_local_date(start_time, interval.get("startUtcOffset"))


def _gh_apply_sedentary(point, daily):
    interval = point.get("sedentaryPeriod", {}).get("interval", {})
    start_time, end_time = interval.get("startTime"), interval.get("endTime")
    d = _gh_interval_local_date(interval)
    if not d or not start_time or not end_time:
        return
    minutes = _gh_duration_seconds(start_time, end_time) // 60
    daily[d]["sedentary_minutes"] = daily[d].get("sedentary_minutes", 0) + minutes


_HR_ZONE_FIELD_MAP = {
    "LIGHT": "hr_zone_light_minutes",
    "MODERATE": "hr_zone_moderate_minutes",
    "VIGOROUS": "hr_zone_vigorous_minutes",
    "PEAK": "hr_zone_peak_minutes",
}


def _gh_apply_hr_zone_minutes(point, daily):
    tz_point = point.get("timeInHeartRateZone", {})
    interval = tz_point.get("interval", {})
    start_time, end_time = interval.get("startTime"), interval.get("endTime")
    field = _HR_ZONE_FIELD_MAP.get(tz_point.get("heartRateZoneType"))
    d = _gh_interval_local_date(interval)
    if not d or not start_time or not end_time or not field:
        return
    minutes = _gh_duration_seconds(start_time, end_time) // 60
    daily[d][field] = daily[d].get(field, 0) + minutes


def _gh_sync_height():
    """One-time convenience: auto-fill NutritionProfile.height_cm from Google
    Health's height data type if it's not already set. Never overwrites a
    value the user (or Withings, if that's ever wired up) already entered."""
    from .models import NutritionProfile
    from .services.google_health_client import GoogleHealthClient

    profile = NutritionProfile.get()
    if profile.height_cm is not None:
        return
    try:
        client = GoogleHealthClient()
        points = client.get_height(date.today() - timedelta(days=5 * 365), date.today())
    except Exception as e:
        logger.warning("Google Health height backfill failed: %s", e)
        return
    if not points:
        return
    latest = max(points, key=lambda p: p.get("height", {}).get("sampleTime", {}).get("physicalTime", ""))
    mm = latest.get("height", {}).get("heightMillimeters")
    if mm is None:
        return
    profile.height_cm = round(float(mm) / 10, 1)
    profile.save(update_fields=["height_cm"])
    logger.info("Auto-filled NutritionProfile.height_cm=%.1f from Google Health", profile.height_cm)


_gh_wellness_sync_lock = threading.Lock()


def _run_google_health_wellness_sync(dates: list) -> dict:
    """
    Sync Google Health wellness data into DailyStats for the given dates.
    Fetches each data type once across the full [min(dates), max(dates)]
    span (cheaper than one call per day), buckets results per calendar day,
    then upserts. Only ever writes the fields Google Health can plausibly
    supply (see the mapping table in workouts/services/google_health_client.py's
    per-type methods) — Garmin-exclusive fields (body battery, stress,
    training load/readiness, fitness age, daily goals) are never touched.

    Guarded by _gh_wellness_sync_lock (non-blocking) so overlapping callers
    — a webhook-triggered background thread racing another one, or a
    webhook racing a manually-triggered "Sync New"/"Sync All" — skip
    instead of piling up concurrent full 17-endpoint fetches against
    Google's API. 2026-08-23 incident: a single webhook batch with several
    changed wellness metrics spawned one thread per metric (see
    google_health_webhook), each redundantly re-running this entire
    function concurrently — flooded Google with duplicate calls (429s) and
    the unbounded concurrent threads spiked memory enough to trigger a
    Render alert. A skipped run is harmless: the next webhook or the
    regular polling sync covers the same dates shortly after.
    """
    if not dates:
        return {"done": True, "synced": 0, "errors": 0}
    if not _integration_enabled("google_health"):
        return {**_integration_disabled_result("google_health"), "synced": 0, "errors": 0}

    if not _gh_wellness_sync_lock.acquire(blocking=False):
        logger.info("Google Health wellness sync: already in progress elsewhere, skipping this call")
        return {"done": True, "skipped": "already_in_progress", "synced": 0, "errors": 0}

    try:
        return _run_google_health_wellness_sync_locked(dates)
    finally:
        _gh_wellness_sync_lock.release()


def _run_google_health_wellness_sync_locked(dates: list) -> dict:
    """The actual sync body, always called with _gh_wellness_sync_lock held —
    split out so the wrapper's try/finally guarantees the lock releases on
    every exit path (normal return, the early returns below, or any
    unexpected exception) without needing to duplicate release calls at
    each one."""
    from collections import defaultdict
    from .services.google_health_client import GoogleHealthClient, GoogleHealthReauthRequired
    from .models import Integration

    try:
        client = GoogleHealthClient()
    except Exception as e:
        return {"error": str(e), "synced": 0, "errors": len(dates)}

    start, end = min(dates), max(dates)
    daily: dict = defaultdict(dict)
    errors = 0

    fetchers = [
        (client.get_daily_resting_heart_rate, _gh_apply_resting_hr),
        (client.get_daily_heart_rate_variability, _gh_apply_daily_hrv),
        (client.get_run_vo2_max, _gh_apply_run_vo2_max),
        (client.get_daily_respiratory_rate, _gh_apply_respiratory_rate),
        (client.get_respiratory_rate_sleep_summary, _gh_apply_respiratory_sleep),
        (client.get_daily_oxygen_saturation, _gh_apply_oxygen_saturation),
        (client.get_sleep, _gh_apply_sleep),
        (client.get_steps_daily_rollup, _gh_apply_steps),
        (client.get_floors_daily_rollup, _gh_apply_floors),
        (client.get_active_energy_burned_daily_rollup, _gh_apply_active_calories),
        (client.get_total_calories_daily_rollup, _gh_apply_total_calories),
        (client.get_active_minutes_daily_rollup, _gh_apply_active_minutes),
        (client.get_heart_rate_variability, _gh_apply_intraday_hrv),
        (client.get_active_zone_minutes_daily_rollup, _gh_apply_active_zone_minutes),
        (client.get_daily_sleep_temperature_derivations, _gh_apply_sleep_temp),
        (client.get_sedentary_period, _gh_apply_sedentary),
        (client.get_time_in_heart_rate_zone, _gh_apply_hr_zone_minutes),
    ]
    for getter, apply_fn in fetchers:
        try:
            points = getter(start, end)
        except GoogleHealthReauthRequired as e:
            logger.error("Google Health wellness sync aborted: %s", e)
            return {"error": str(e), "synced": 0, "errors": len(dates)}
        except Exception as e:
            logger.warning("Google Health wellness sync: %s failed: %s", getattr(getter, "__name__", getter), e)
            errors += 1
            continue
        for point in points:
            apply_fn(point, daily)

    synced = 0
    for d in dates:
        fields = daily.get(d, {})
        if not fields:
            # No Google Health data at all for this date — don't create a
            # pointless empty row or mislabel wellness_source for a day we
            # have nothing to say about. Matters most for wide "Sync All"
            # ranges where most days in a multi-year span may be empty.
            continue
        stats, _ = DailyStats.objects.get_or_create(date=d)
        if stats.wellness_source == "garmin":
            logger.warning(
                "Google Health wellness sync: %s already has Garmin wellness data — "
                "updating shared fields only, preserving Garmin-exclusive fields", d
            )
        else:
            stats.wellness_source = "google_health"
        for field, value in fields.items():
            setattr(stats, field, value)
        stats.google_health_synced_at = tz.now()
        stats.save()
        synced += 1

    # hrv_weekly_avg has no direct Google Health equivalent — compute it
    # client-side as a trailing 7-day average of hrv_last_night, for any date
    # this sync actually populated hrv_last_night on.
    for d in dates:
        if daily.get(d, {}).get("hrv_last_night") is None:
            continue
        window = DailyStats.objects.filter(
            date__gte=d - timedelta(days=6), date__lte=d, hrv_last_night__isnull=False
        ).values_list("hrv_last_night", flat=True)
        if window:
            DailyStats.objects.filter(date=d).update(hrv_weekly_avg=round(sum(window) / len(window), 1))

    # resting_hr_baseline: trailing 30-day avg of resting_hr, same pattern as
    # hrv_weekly_avg above — used by DailyStats.readiness_score to judge
    # today's resting HR against your own recent norm rather than a fixed number.
    for d in dates:
        if daily.get(d, {}).get("resting_hr") is None:
            continue
        window = DailyStats.objects.filter(
            date__gte=d - timedelta(days=29), date__lte=d, resting_hr__isnull=False
        ).values_list("resting_hr", flat=True)
        if window:
            DailyStats.objects.filter(date=d).update(resting_hr_baseline=round(sum(window) / len(window), 1))

    # sleep_baseline_seconds: trailing 30-day avg of sleep_seconds, same
    # pattern as resting_hr_baseline/hrv_weekly_avg above.
    for d in dates:
        if daily.get(d, {}).get("sleep_seconds") is None:
            continue
        window = DailyStats.objects.filter(
            date__gte=d - timedelta(days=29), date__lte=d, sleep_seconds__isnull=False
        ).values_list("sleep_seconds", flat=True)
        if window:
            DailyStats.objects.filter(date=d).update(sleep_baseline_seconds=round(sum(window) / len(window), 1))

    # wellness_days_synced: trailing 30-day count of days with at least one
    # recovery signal (HRV, resting HR, or sleep) — compute_readiness_proxy()
    # requires >=7 before it trusts the baselines above enough to score against.
    for d in dates:
        if d not in daily:
            continue
        count = DailyStats.objects.filter(
            date__gte=d - timedelta(days=29), date__lte=d
        ).filter(
            Q(hrv_last_night__isnull=False) | Q(resting_hr__isnull=False) | Q(sleep_seconds__isnull=False)
        ).count()
        DailyStats.objects.filter(date=d).update(wellness_days_synced=count)

    # computed_readiness_score/label: only meaningful once the baselines and
    # wellness_days_synced above are current for this sync run, so it runs last.
    for d in dates:
        if d not in daily:
            continue
        stats_row = DailyStats.objects.filter(date=d).first()
        if not stats_row:
            continue
        score, label = stats_row.compute_readiness_proxy()
        DailyStats.objects.filter(date=d).update(computed_readiness_score=score, computed_readiness_label=label or "")

    _gh_sync_height()

    Integration.objects.filter(key="google_health").update(last_synced_at=tz.now())
    return {"done": True, "synced": synced, "errors": errors}


# ---------------------------------------------------------------------------
# Google Health nutrition export (FoodEntry -> nutrition-log data type)
#
# Schema confirmed live 2026-08-19 via the API's $discovery/rest?version=v4
# document (NutritionLog/NutrientQuantity/EnergyQuantity/WeightQuantity/
# SessionTimeInterval schemas) — guessing field names against the live API
# gave "Cannot find field" errors that never converged, so this is the one
# Google Health write path in the codebase that was NOT reverse-engineered
# from error messages alone. One real bug found and worked around: a
# far-future test date (2099) 500s ("An internal error occurred") on this
# data type specifically — unlike exercise writes, which accepted 2099 fine
# during earlier testing. Near-dates work. All live test writes during
# development used today's date and were immediately cleaned up via
# batchDelete (POST dataTypes/nutrition-log/dataPoints:batchDelete with a
# `names` array of full resource names — NOT `dataPointIds`, which 400s).
# ---------------------------------------------------------------------------

_FOOD_ENTRY_NUTRIENT_FIELDS = [
    ("protein_g", "PROTEIN"),
    ("fiber_g", "DIETARY_FIBER"),
]


def _push_food_entry_to_google_health(entry) -> bool:
    """Best-effort export of a newly-logged FoodEntry to Google Health.
    Never raises — a failure here must never block food logging. Stores the
    created data point's resource name on the entry so it can be cleaned up
    if the entry is later deleted. No-op if the integration is disabled/not
    authenticated, or if the nutrition.writeonly scope wasn't granted."""
    from .services.google_health_client import GoogleHealthClient, GoogleHealthReauthRequired

    if not _integration_enabled("google_health"):
        return False

    start = entry.logged_at
    end = start + timedelta(minutes=1)
    # entry.logged_at comes back from the ORM as a UTC-aware datetime (Django
    # stores everything in UTC regardless of TIME_ZONE) — start.utcoffset()
    # is always 0 on that, not the app's real America/Los_Angeles offset. The
    # startTime/endTime below are correct either way (converting to UTC is a
    # no-op on an already-UTC value), but startUtcOffset was being sent as
    # "0s" instead of the real local offset, so Google computed the wrong
    # civil date/time for the entry — a dinner logged ~8pm Pacific showed up
    # at ~3am the next day, since 0s offset makes Google treat the UTC
    # instant as if it were already local. tz.localtime() converts to the
    # Django-configured local timezone first so the offset is correct.
    offset = tz.localtime(start).utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset else 0

    display_name = (entry.raw_text or "").strip()
    if not display_name and entry.items_json:
        display_name = ", ".join(i.get("name", "") for i in entry.items_json if i.get("name"))
    display_name = display_name[:200] or "FitPulse food log"

    nutrients = [
        {"nutrient": nutrient, "quantity": {"grams": getattr(entry, field)}}
        for field, nutrient in _FOOD_ENTRY_NUTRIENT_FIELDS
        if getattr(entry, field)
    ]

    body = {
        "nutritionLog": {
            "interval": {
                "startTime": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "endTime": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "startUtcOffset": f"{offset_seconds}s",
                "endUtcOffset": f"{offset_seconds}s",
            },
            "foodDisplayName": display_name,
            "mealType": entry.meal.upper() if entry.meal else "ANYTIME",
            "energy": {"kcal": entry.calories},
            "totalFat": {"grams": entry.fat_g},
            "totalCarbohydrate": {"grams": entry.carbs_g},
            "nutrients": nutrients,
        }
    }

    try:
        client = GoogleHealthClient()
        resp = client._request("POST", "dataTypes/nutrition-log/dataPoints", json_body=body)
    except GoogleHealthReauthRequired as e:
        logger.warning("Google Health food export skipped (reauth required): %s", e)
        return False
    except Exception as e:
        logger.warning("Google Health food export failed for FoodEntry %s: %s", entry.pk, e)
        return False

    name = resp.get("response", {}).get("name")
    if name:
        entry.google_health_nutrition_log_name = name
        entry.save(update_fields=["google_health_nutrition_log_name"])
    return True


def _delete_food_entry_from_google_health(resource_name: str) -> bool:
    """Best-effort cleanup of a previously-exported nutrition-log entry when
    the source FoodEntry is deleted in FitPulse."""
    from .services.google_health_client import GoogleHealthClient

    if not resource_name:
        return False
    try:
        client = GoogleHealthClient()
        client._request(
            "POST", "dataTypes/nutrition-log/dataPoints:batchDelete",
            json_body={"names": [resource_name]},
        )
        return True
    except Exception as e:
        logger.warning("Google Health food export cleanup failed for %s: %s", resource_name, e)
        return False


# ---------------------------------------------------------------------------
# Google Health exercise sync
# ---------------------------------------------------------------------------

def _is_peloton_sourced(data_source: dict, display_name: str = "") -> bool:
    """
    True if a Google Health exercise data point originated from Peloton's own
    Fitbit Web API integration — these are filtered out entirely since
    PelotonClient already ingests the same workout with richer data (effort
    points, leaderboard). See PELOTON_FITBIT_WEB_CLIENT_ID's docstring in
    google_health_client.py for how this was confirmed.
    """
    from .services.google_health_client import PELOTON_FITBIT_WEB_CLIENT_ID

    web_client_id = data_source.get("application", {}).get("webClientId")
    by_client_id = web_client_id == PELOTON_FITBIT_WEB_CLIENT_ID
    by_display_name = display_name.startswith("Peloton -") or display_name.startswith("Peloton –")

    if by_client_id != by_display_name:
        logger.warning(
            "Google Health exercise dedup signals disagree: webClientId=%r "
            "(match=%s) vs displayName=%r (match=%s) — trusting webClientId. "
            "dataSource format may have changed.",
            web_client_id, by_client_id, display_name, by_display_name,
        )
    elif not web_client_id and not display_name:
        logger.warning("Google Health exercise data point has neither dataSource.application "
                        "nor displayName — cannot confirm Peloton origin either way; treating as non-Peloton.")

    return by_client_id


def _gh_parse_duration_str_seconds(s) -> float | None:
    """Parse a protobuf-style Duration string (e.g. '0.212s') to float
    seconds. Distinct from _gh_duration_seconds (which diffs two ISO
    timestamps) — this parses a single pre-formatted duration value.
    Returns None on anything unparseable rather than raising, since these
    values come from a third-party payload."""
    if not s:
        return None
    try:
        return float(str(s).rstrip("sS"))
    except (TypeError, ValueError):
        return None


def _gh_duration_str_to_int_seconds(s) -> int | None:
    """Like _gh_parse_duration_str_seconds but rounds to a whole second —
    for HR zone durations, where sub-second precision doesn't matter and an
    IntegerField is the natural storage type."""
    seconds = _gh_parse_duration_str_seconds(s)
    return round(seconds) if seconds is not None else None


def _gh_parse_float_str(v) -> float | None:
    """Parse a numeric value that Google Health's API may send as a JSON
    string rather than a number (its usual encoding for values that could
    exceed safe JSON-number precision). Returns None on anything
    unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_google_health_exercise(point: dict) -> dict | None:
    """Extract the fields both _upsert_google_health_exercise and
    _augment_peloton_from_google_health need from one exercise data point.
    Returns None if the point is missing required fields (id, start time).

    metricsSummary.mobilityMetrics (confirmed live 2026-08-22 against a real
    response — running-form data IS available from Google Health after all,
    just as a session-level average rather than Garmin's every-5-second time
    series, so no performance_graph_json overlay/chart toggle is possible
    from this source, only the flat RUNNING FORM card values). Values arrive
    in millimeters/protobuf-Duration-string form and are converted to match
    the units _augment_peloton_run (Garmin path) already uses: cadence in
    steps/min, stride/VO in cm, VR in %, ground contact time in ms."""
    from .services.google_health_client import GOOGLE_HEALTH_EXERCISE_TYPE_TO_DISCIPLINE

    ex = point.get("exercise", {})
    name = point.get("name", "")
    point_id = name.rsplit("/", 1)[-1] if name else None
    if not point_id:
        logger.warning("Google Health exercise data point missing a resource name/ID, skipping: %r", point)
        return None

    interval = ex.get("interval", {})
    start_time = interval.get("startTime")
    if not start_time:
        logger.warning("Google Health exercise data point %s missing interval.startTime, skipping", point_id)
        return None
    created_at = datetime.fromisoformat(start_time.replace("Z", "+00:00"))

    raw_type = ex.get("exerciseType", "OTHER")
    discipline = GOOGLE_HEALTH_EXERCISE_TYPE_TO_DISCIPLINE.get(raw_type)
    if discipline is None:
        discipline = raw_type.lower()
        logger.info("Google Health exercise type %r has no explicit discipline mapping, using %r", raw_type, discipline)

    metrics = ex.get("metricsSummary", {})
    calories = metrics.get("caloriesKcal")
    distance_mm = metrics.get("distanceMillimeters")
    distance_miles = round(distance_mm / 1_609_344, 3) if distance_mm else None
    hr_avg_raw = metrics.get("averageHeartRateBeatsPerMinute")
    heart_rate_avg = int(hr_avg_raw) if hr_avg_raw is not None else None

    active_duration_str = ex.get("activeDuration", "")
    duration_seconds = int(active_duration_str.rstrip("s")) if active_duration_str.rstrip("s").isdigit() else \
        _gh_duration_seconds(start_time, interval.get("endTime", start_time))

    # Prefer the API's own per-meter pace (sensor/GPS-derived, so more
    # precise than deriving it from total duration/distance) when present;
    # fall back to the duration/distance calc otherwise.
    avg_pace_seconds = None
    pace_per_meter = metrics.get("averagePaceSecondsPerMeter")
    if discipline in ("running", "walking"):
        if pace_per_meter:
            avg_pace_seconds = round(pace_per_meter * 1609.344)
        elif distance_miles:
            avg_pace_seconds = round(duration_seconds / distance_miles)

    avg_speed_mph = None
    speed_mmps = metrics.get("averageSpeedMillimetersPerSecond")
    if speed_mmps:
        avg_speed_mph = round((speed_mmps / 1000) * 2.23694, 2)

    elevation_gain = None
    elevation_mm = metrics.get("elevationGainMillimeters")
    if elevation_mm:
        elevation_gain = round(elevation_mm / 304.8, 1)

    mobility = metrics.get("mobilityMetrics", {})
    gct_seconds = _gh_parse_duration_str_seconds(mobility.get("avgGroundContactTimeDuration"))
    ground_contact_time_avg = round(gct_seconds * 1000, 1) if gct_seconds is not None else None

    cadence = mobility.get("avgCadenceStepsPerMinute")
    run_cadence_avg = round(cadence, 1) if cadence is not None else None

    stride_mm = _gh_parse_float_str(mobility.get("avgStrideLengthMillimeters"))
    stride_length_avg = round(stride_mm / 10, 1) if stride_mm is not None else None

    vo_mm = _gh_parse_float_str(mobility.get("avgVerticalOscillationMillimeters"))
    vertical_oscillation_avg = round(vo_mm / 10, 2) if vo_mm is not None else None

    vertical_ratio = mobility.get("avgVerticalRatio")
    vertical_ratio_avg = round(vertical_ratio, 2) if vertical_ratio is not None else None

    # Google's own 4-zone HR-time breakdown for the session — a coarser,
    # differently-defined model than the Peloton/Garmin 5-zone breakdown
    # computed client-side from a real HR time series, so this is only ever
    # used standalone (see _GOOGLE_HEALTH_FILLABLE_FIELDS below), not merged
    # onto a Peloton/Garmin row that already has its own zone chart.
    hr_zones = metrics.get("heartRateZoneDurations", {})
    hr_zone_light_seconds = _gh_duration_str_to_int_seconds(hr_zones.get("lightTime"))
    hr_zone_moderate_seconds = _gh_duration_str_to_int_seconds(hr_zones.get("moderateTime"))
    hr_zone_vigorous_seconds = _gh_duration_str_to_int_seconds(hr_zones.get("vigorousTime"))
    hr_zone_peak_seconds = _gh_duration_str_to_int_seconds(hr_zones.get("peakTime"))

    return dict(
        point_id=point_id,
        title=ex.get("displayName", "") or raw_type.replace("_", " ").title(),
        discipline=discipline,
        duration_seconds=duration_seconds or 0,
        calories=int(round(calories)) if calories is not None else None,
        distance_miles=distance_miles,
        heart_rate_avg=heart_rate_avg,
        avg_pace_seconds=avg_pace_seconds,
        avg_speed_mph=avg_speed_mph,
        elevation_gain=elevation_gain,
        run_cadence_avg=run_cadence_avg,
        stride_length_avg=stride_length_avg,
        vertical_oscillation_avg=vertical_oscillation_avg,
        vertical_ratio_avg=vertical_ratio_avg,
        ground_contact_time_avg=ground_contact_time_avg,
        hr_zone_light_seconds=hr_zone_light_seconds,
        hr_zone_moderate_seconds=hr_zone_moderate_seconds,
        hr_zone_vigorous_seconds=hr_zone_vigorous_seconds,
        hr_zone_peak_seconds=hr_zone_peak_seconds,
        created_at=created_at,
    )


def _upsert_google_health_exercise(point: dict) -> tuple:
    """Insert or update a CachedWorkout from one Google Health exercise data
    point (already filtered to exclude Peloton-sourced/duplicate entries).
    Returns (created, updated). Google Health's exercise type is a
    session-level summary (metricsSummary), not a time-series — no
    performance_graph_json gets populated here (so no chart overlay/toggle),
    but the same session-level running-form averages Garmin provides
    (cadence, stride length, vertical oscillation, vertical ratio, ground
    contact time) are, when present, plus Google's own 4-zone HR-time
    breakdown (light/moderate/vigorous/peak) — see
    _parse_google_health_exercise. run_detail.html renders that breakdown
    server-side in place of the PERFORMANCE OVER TIME chart when there's no
    performance_graph_json to drive it."""
    parsed = _parse_google_health_exercise(point)
    if parsed is None:
        return False, False

    point_id = parsed["point_id"]
    wid = f"google_health_{point_id}"
    fields = dict(
        title=parsed["title"],
        discipline=parsed["discipline"],
        fitness_discipline_display=parsed["discipline"].replace("_", " ").title(),
        duration_seconds=parsed["duration_seconds"],
        calories=parsed["calories"],
        distance_miles=parsed["distance_miles"],
        heart_rate_avg=parsed["heart_rate_avg"],
        avg_pace_seconds=parsed["avg_pace_seconds"],
        avg_speed_mph=parsed["avg_speed_mph"],
        elevation_gain=parsed["elevation_gain"],
        run_cadence_avg=parsed["run_cadence_avg"],
        stride_length_avg=parsed["stride_length_avg"],
        vertical_oscillation_avg=parsed["vertical_oscillation_avg"],
        vertical_ratio_avg=parsed["vertical_ratio_avg"],
        ground_contact_time_avg=parsed["ground_contact_time_avg"],
        hr_zone_light_seconds=parsed["hr_zone_light_seconds"],
        hr_zone_moderate_seconds=parsed["hr_zone_moderate_seconds"],
        hr_zone_vigorous_seconds=parsed["hr_zone_vigorous_seconds"],
        hr_zone_peak_seconds=parsed["hr_zone_peak_seconds"],
        created_at=parsed["created_at"],
        google_health_activity_id=point_id,
        source="google_health",
        raw_data=point,
    )

    def _apply_to_existing(row):
        for field, value in fields.items():
            if field != "raw_data" and value is not None:
                setattr(row, field, value)
        row.raw_data = point
        row.save()

    existing = CachedWorkout.objects.filter(workout_id=wid).first()
    if existing:
        _apply_to_existing(existing)
        return False, True

    try:
        obj = CachedWorkout(workout_id=wid, ride_id="", workout_type="",
                             instructor_name="", instructor_image_url="", class_image_url="",
                             **fields)
        obj.save()
        return True, False
    except IntegrityError:
        # Lost a race on workout_id's unique constraint — another call for
        # this same point (e.g. two webhook deliveries for the same UPSERT
        # notification, processed in overlapping background threads; Google
        # explicitly warns retries can duplicate notifications) already
        # created the row between our filter() and save(). Fall back to
        # updating what it created instead of erroring out.
        existing = CachedWorkout.objects.get(workout_id=wid)
        _apply_to_existing(existing)
        return False, True


def _peloton_workout_index():
    """Sorted [(timestamp, CachedWorkout)] for all Peloton-sourced workouts —
    like _peloton_timestamp_index() but keeps the object so callers can
    augment it, not just detect the overlap."""
    return sorted(
        (
            (int(w.created_at.timestamp()), w)
            for w in CachedWorkout.objects.filter(source="peloton")
            if w.created_at is not None
        ),
        key=lambda pair: pair[0],
    )


def _garmin_workout_index():
    """Sorted [(timestamp, CachedWorkout)] for all Garmin-sourced workouts —
    same shape as _peloton_workout_index(), used to catch Garmin↔Google
    Health duplicates that have no Peloton workout at all (e.g. an outdoor
    hike neither service routes through Peloton), which the Peloton-anchored
    matching above never looks at."""
    return sorted(
        (
            (int(w.created_at.timestamp()), w)
            for w in CachedWorkout.objects.filter(source="garmin")
            if w.created_at is not None
        ),
        key=lambda pair: pair[0],
    )


def _find_workout_match(created_at, workout_index, window_seconds=300):
    """Closest Peloton CachedWorkout within window_seconds of created_at, or None.
    Originally 120s (same as Garmin's _is_peloton_duplicate), calibrated against
    446 real Google Health/Peloton duplicate pairs, 445 of which were within
    120s (median offset: 0s, i.e. identical timestamps). Widened to 300s after
    a confirmed real pair (google_health_7934183891199634496 / Peloton strength
    workout 7ac396af733a45e5ac7b5f4182e95ac9) landed 187s apart — the watch's
    auto-detected start lags Peloton's own timestamp more for strength sessions
    than for cardio."""
    if created_at is None or not workout_index:
        return None
    ts = int(created_at.timestamp())
    timestamps = [t for t, _ in workout_index]
    pos = bisect.bisect_left(timestamps, ts - window_seconds)
    best = None
    while pos < len(workout_index) and workout_index[pos][0] <= ts + window_seconds:
        candidate_ts, candidate = workout_index[pos]
        if best is None or abs(candidate_ts - ts) < abs(best[0] - ts):
            best = (candidate_ts, candidate)
        pos += 1
    return best[1] if best else None


# Below this many seconds of overlap, treat it as clock/reporting noise between
# two genuinely separate, merely adjacent sessions rather than a duplicate.
_OVERLAP_TOLERANCE_SECONDS = 180
# No single Peloton/Garmin class (or unbroken run of them — see below) realistically
# runs longer than this — bounds how far back _find_overlapping_workout looks, so it
# stays a cheap slice of the index rather than a scan of the whole thing.
_MAX_OVERLAP_LOOKBACK_MINUTES = 120


def _find_overlapping_workout(created_at, duration_seconds, workout_index):
    """
    Find an existing workout (Peloton or Garmin — whichever index is passed) whose
    time span overlaps this Google Health point's span by at least
    _OVERLAP_TOLERANCE_SECONDS, even though the point's own start isn't near the
    workout's start (which is what _find_workout_match looks for). Returns the
    first overlap found — there may be more than one (see below); any one of them
    is enough to know this point is redundant.

    Needed because Google Health's own on-device auto-detection doesn't
    necessarily draw the same boundaries around a session that Peloton/Garmin do,
    in either direction:
    - **Nested**: one Peloton/Garmin session gets split into separate sub-segment
      entries — confirmed live 2026-09-28, a 45-minute circuit class's strength
      portion auto-detected as a standalone "Free weights" entry starting ~14
      minutes in, its running portion as "Treadmill run" starting ~30 minutes in.
    - **Spanning**: several separate back-to-back Peloton sessions get folded into
      ONE longer auto-detected entry — confirmed live 2026-09-28, a single 71-minute
      "Bootcamp" entry covering the same real time as three separate Peloton
      classes taken back-to-back (a 45-min circuit class, a 5-min cool-down walk,
      and a 15-min stretch).
    Both land as permanent duplicate rows under _find_workout_match alone, since
    its window is centered on the POINT's own start — it never looks minutes
    earlier for a workout that contains or overlaps it.

    An overlapping point's own stats (calories, HR, pace) cover only its own slice
    of the real activity, or a blend across several unrelated sessions — never the
    same thing as any single existing workout's own stats. Callers must NOT
    augment/merge fields from an overlap match the way a same-session duplicate's
    fields get merged from _find_workout_match; only skip creating the duplicate
    row.
    """
    if created_at is None or not workout_index:
        return None
    point_start = int(created_at.timestamp())
    point_end = point_start + (duration_seconds or 0)
    timestamps = [t for t, _ in workout_index]
    lo = bisect.bisect_left(timestamps, point_start - _MAX_OVERLAP_LOOKBACK_MINUTES * 60)
    hi = bisect.bisect_right(timestamps, point_end + _OVERLAP_TOLERANCE_SECONDS)
    for i in range(lo, hi):
        ts, candidate = workout_index[i]
        c_end = ts + (candidate.duration_seconds or 0)
        overlap = min(point_end, c_end) - max(point_start, ts)
        if overlap >= _OVERLAP_TOLERANCE_SECONDS:
            return candidate
    return None


def _augment_peloton_from_google_health(peloton_workout, point: dict) -> dict:
    """
    When a Google Health exercise entry duplicates a Peloton workout (either
    via Peloton's own Fitbit integration, or the watch's independent
    auto-detection — see _is_peloton_sourced vs _find_workout_match), don't
    create a second CachedWorkout. Instead reconcile: fill any field the
    Peloton record is missing using Google's data (Peloton's own value
    always wins when both sides have one — never overwrites), and report
    which fields Peloton has that Google's copy lacks, for the write-back
    path (_push_peloton_to_google_health).

    Returns {"filled": [...], "google_missing": [...], "point_id": str}.
    """
    parsed = _parse_google_health_exercise(point)
    if parsed is None:
        return {"filled": [], "google_missing": [], "point_id": None}

    filled = []
    for f in _GOOGLE_HEALTH_FILLABLE_FIELDS:
        if parsed[f] is not None and getattr(peloton_workout, f) is None:
            setattr(peloton_workout, f, parsed[f])
            filled.append(f)
    if parsed["heart_rate_avg"] is not None and peloton_workout.heart_rate_avg_best is None:
        peloton_workout.heart_rate_avg = parsed["heart_rate_avg"]
        filled.append("heart_rate_avg")

    if filled:
        peloton_workout.google_health_activity_id = parsed["point_id"]
        peloton_workout.save(update_fields=filled + ["google_health_activity_id"])

    google_missing = []
    if peloton_workout.calories is not None and parsed["calories"] is None:
        google_missing.append("calories")
    if peloton_workout.heart_rate_avg_best is not None and parsed["heart_rate_avg"] is None:
        google_missing.append("heart_rate_avg")
    if peloton_workout.distance_miles is not None and parsed["distance_miles"] is None:
        google_missing.append("distance_miles")

    return {"filled": filled, "google_missing": google_missing, "point_id": parsed["point_id"]}


def _augment_garmin_from_google_health(garmin_workout, point: dict) -> dict:
    """
    Like _augment_peloton_from_google_health, but for the case that function
    doesn't cover: a Google Health exercise entry that duplicates a
    Garmin-sourced workout with no Peloton counterpart at all (e.g. an
    outdoor hike neither service routes through Peloton — both a Garmin
    watch and Health Connect can independently auto-detect the same
    session). Garmin is treated as authoritative here, same reasoning as
    Peloton being authoritative over Garmin elsewhere: a dedicated watch's
    own recording beats a phone/Health-Connect-detected entry — but still
    backfill anything Garmin's copy is missing that Google's has.

    Returns {"filled": [...], "point_id": str}.
    """
    parsed = _parse_google_health_exercise(point)
    if parsed is None:
        return {"filled": [], "point_id": None}

    filled = []
    for f in _GOOGLE_HEALTH_FILLABLE_FIELDS:
        if parsed[f] is not None and getattr(garmin_workout, f) is None:
            setattr(garmin_workout, f, parsed[f])
            filled.append(f)
    if parsed["heart_rate_avg"] is not None and garmin_workout.heart_rate_avg_best is None:
        garmin_workout.heart_rate_avg = parsed["heart_rate_avg"]
        filled.append("heart_rate_avg")

    if filled:
        garmin_workout.google_health_activity_id = parsed["point_id"]
        garmin_workout.save(update_fields=filled + ["google_health_activity_id"])

    return {"filled": filled, "point_id": parsed["point_id"]}


def _reconcile_google_health_duplicates(dry_run=False) -> dict:
    """
    Re-check every existing source="google_health" CachedWorkout row against
    the current set of Peloton workouts and merge any new match.

    Needed because matching is otherwise one-directional: a Google Health
    sync only matches against Peloton workouts that already exist *at that
    moment*. If Google Health syncs before the corresponding Peloton workout
    does, the two land as separate rows and nothing re-checks them — Peloton
    sync never looked at google_health rows at all. Calling this after every
    Peloton sync closes that gap. Same logic as the one-time
    dedupe_google_health_exercise command, factored out so both can share it.

    Returns {"checked", "matched", "augmented", "deleted", "details"}, where
    each entry in "details" is {"google_workout_id", "peloton_workout_id",
    "peloton_title", "filled", "had_raw_data"}.
    """
    if not _integration_enabled("google_health"):
        return {"checked": 0, "matched": 0, "augmented": 0, "deleted": 0, "details": []}

    peloton_index = _peloton_workout_index()
    gh_workouts = list(CachedWorkout.objects.filter(source="google_health").order_by("created_at"))

    augmented = 0
    to_delete = []
    details = []

    for w in gh_workouts:
        match = _find_workout_match(w.created_at, peloton_index)
        overlap_only = False
        if match is None:
            match = _find_overlapping_workout(w.created_at, w.duration_seconds, peloton_index)
            overlap_only = match is not None
        if match is None:
            continue

        filled = []
        had_raw_data = bool(w.raw_data)
        # An overlap-only match (Google's own auto-detection drawing different session
        # boundaries than Peloton did — nested inside it, or spanning across it and
        # others) is never augmented — its stats don't describe the same thing as this
        # one workout's own stats. See _find_overlapping_workout.
        if had_raw_data and not overlap_only:
            if dry_run:
                parsed = _parse_google_health_exercise(w.raw_data)
                if parsed:
                    filled = [f for f in _GOOGLE_HEALTH_FILLABLE_FIELDS
                              if parsed[f] is not None and getattr(match, f) is None]
                    if parsed["heart_rate_avg"] is not None and match.heart_rate_avg_best is None:
                        filled.append("heart_rate_avg")
            else:
                filled = _augment_peloton_from_google_health(match, w.raw_data)["filled"]

        if filled:
            augmented += 1

        details.append({
            "google_workout_id": w.workout_id,
            "peloton_workout_id": match.workout_id,
            "peloton_title": match.title,
            "filled": filled,
            "had_raw_data": had_raw_data,
            "overlap_only": overlap_only,
        })
        to_delete.append(w.pk)

    deleted = 0
    if not dry_run and to_delete:
        deleted = CachedWorkout.objects.filter(pk__in=to_delete).delete()[0]

    return {
        "checked": len(gh_workouts),
        "matched": len(details),
        "augmented": augmented,
        "deleted": deleted,
        "details": details,
    }


def _reconcile_garmin_google_health_duplicates(dry_run=False) -> dict:
    """
    Re-check every existing source="google_health" CachedWorkout row against
    Garmin-sourced workouts and merge any match that has no Peloton
    counterpart — the case _reconcile_google_health_duplicates doesn't
    cover: an outdoor activity (e.g. a hike) that neither Garmin nor Google
    Health routes through Peloton, so both sync independently as separate
    rows with nothing to reconcile them against each other. Called after
    both Garmin and Google Health syncs so order never matters, same as the
    Peloton-anchored reconcilers.

    Rows that duplicate a Peloton workout are skipped here (left for
    _reconcile_google_health_duplicates to handle) rather than risking a
    false Garmin match on a row that actually belongs to Peloton.

    Only gates on Google Health being enabled, not Garmin — unlike
    _reconcile_garmin_duplicates' running-augmentation path, nothing here
    needs a live Garmin API call (_augment_garmin_from_google_health only
    reads already-stored Garmin rows and already-parsed Google Health
    payload data), so there's no reason this can't run while Garmin syncing
    happens to be toggled off.

    Returns {"checked", "matched", "augmented", "deleted", "details"}, where
    each entry in "details" is {"google_workout_id", "garmin_workout_id",
    "garmin_title", "filled", "had_raw_data"}.
    """
    if not _integration_enabled("google_health"):
        return {"checked": 0, "matched": 0, "augmented": 0, "deleted": 0, "details": []}

    peloton_index = _peloton_workout_index()
    garmin_index = _garmin_workout_index()
    gh_workouts = list(CachedWorkout.objects.filter(source="google_health").order_by("created_at"))

    augmented = 0
    to_delete = []
    details = []

    for w in gh_workouts:
        if (_find_workout_match(w.created_at, peloton_index) is not None
                or _find_overlapping_workout(w.created_at, w.duration_seconds, peloton_index) is not None):
            continue  # belongs to the Peloton-anchored reconciler instead

        match = _find_workout_match(w.created_at, garmin_index)
        overlap_only = False
        if match is None:
            match = _find_overlapping_workout(w.created_at, w.duration_seconds, garmin_index)
            overlap_only = match is not None
        if match is None:
            continue

        filled = []
        had_raw_data = bool(w.raw_data)
        # See _find_overlapping_workout — an overlap-only match is never augmented.
        if had_raw_data and not overlap_only:
            if dry_run:
                parsed = _parse_google_health_exercise(w.raw_data)
                if parsed:
                    filled = [f for f in _GOOGLE_HEALTH_FILLABLE_FIELDS
                              if parsed[f] is not None and getattr(match, f) is None]
                    if parsed["heart_rate_avg"] is not None and match.heart_rate_avg_best is None:
                        filled.append("heart_rate_avg")
            else:
                filled = _augment_garmin_from_google_health(match, w.raw_data)["filled"]

        if filled:
            augmented += 1

        details.append({
            "google_workout_id": w.workout_id,
            "garmin_workout_id": match.workout_id,
            "garmin_title": match.title,
            "filled": filled,
            "had_raw_data": had_raw_data,
            "overlap_only": overlap_only,
        })
        to_delete.append(w.pk)

    deleted = 0
    if not dry_run and to_delete:
        deleted = CachedWorkout.objects.filter(pk__in=to_delete).delete()[0]

    return {
        "checked": len(gh_workouts),
        "matched": len(details),
        "augmented": augmented,
        "deleted": deleted,
        "details": details,
    }


_gh_exercise_sync_lock = threading.Lock()


def _run_google_health_exercise_sync(start, end) -> dict:
    """
    Guarded by _gh_exercise_sync_lock (non-blocking), same pattern and same
    reason as _gh_wellness_sync_lock on _run_google_health_wellness_sync:
    overlapping callers — a burst of separate webhook deliveries, or a
    webhook racing a manually-triggered "Sync New"/"Sync All" — skip instead
    of piling up concurrent exercise fetches. 2026-10-02 incident: five
    separate webhook POSTs arrived within ~2 seconds (confirmed in Render's
    logs), each spawning its own thread here with no lock to stop them —
    unlike wellness sync, which the 2026-08-23 fix already covered. Each
    thread independently builds a full in-memory index of every Peloton and
    Garmin workout (_peloton_workout_index/_garmin_workout_index fetch whole
    rows, including large JSON fields like performance_graph_json/raw_data,
    not just IDs); concurrent copies of that is what spiked memory enough to
    trigger Render's limit and restart the instance. A skipped run is
    harmless: the next webhook or the regular polling sync covers the same
    dates shortly after.
    """
    if not _gh_exercise_sync_lock.acquire(blocking=False):
        logger.info("Google Health exercise sync: already in progress elsewhere, skipping this call")
        return {"done": True, "skipped": "already_in_progress", "created": 0, "updated": 0}
    try:
        return _run_google_health_exercise_sync_locked(start, end)
    finally:
        _gh_exercise_sync_lock.release()


def _run_google_health_exercise_sync_locked(start, end) -> dict:
    """The actual sync body, always called with _gh_exercise_sync_lock held —
    split out so the wrapper's try/finally guarantees the lock releases on
    every exit path (normal return, an early return below, or any
    unexpected exception) without needing to duplicate release calls."""
    if not _integration_enabled("google_health"):
        return {**_integration_disabled_result("google_health"), "created": 0, "updated": 0}

    from .services.google_health_client import GoogleHealthClient, GoogleHealthReauthRequired

    try:
        client = GoogleHealthClient()
        points = client.get_exercise(start, end)
    except GoogleHealthReauthRequired as e:
        logger.error("Google Health exercise sync aborted: %s", e)
        return {"error": str(e), "created": 0, "updated": 0}
    except Exception as e:
        return {"error": str(e), "created": 0, "updated": 0}

    peloton_index = _peloton_workout_index()
    garmin_index = _garmin_workout_index()
    created = updated = skipped_peloton = skipped_garmin = augmented = garmin_augmented = 0
    skipped_overlapping = 0
    google_missing_fields: list = []  # candidates for the write-back path

    for point in points:
        ex = point.get("exercise", {})
        data_source = point.get("dataSource", {})
        display_name = ex.get("displayName", "")
        start_time = ex.get("interval", {}).get("startTime")
        created_at = datetime.fromisoformat(start_time.replace("Z", "+00:00")) if start_time else None

        is_official = _is_peloton_sourced(data_source, display_name)
        # Even non-official entries can duplicate a Peloton workout — the
        # watch's own auto-detection creates a separate generic-titled entry
        # ("Walk", "Run") for the same session, independent of Peloton's own
        # Fitbit-integration sync. Confirmed 2026-08-18: 446/471 previously-
        # synced Google Health workouts landed within 120s of an existing
        # Peloton workout despite not matching the official dataSource check.
        match = _find_workout_match(created_at, peloton_index) if (is_official or created_at) else None

        if is_official or match is not None:
            skipped_peloton += 1
            if match is not None:
                result = _augment_peloton_from_google_health(match, point)
                if result["filled"]:
                    augmented += 1
                if result["google_missing"]:
                    google_missing_fields.append({
                        "peloton_workout_id": match.workout_id,
                        "google_point_id": result["point_id"],
                        "fields": result["google_missing"],
                    })
            elif is_official:
                logger.info(
                    "Google Health exercise %s is Peloton-sourced but no matching local "
                    "Peloton workout found within the window — skipped, not augmented",
                    point.get("name"),
                )
            continue

        # No Peloton workout at all for this session — still check Garmin
        # before creating a standalone row. Covers activities neither
        # service routes through Peloton (e.g. an outdoor hike), which both
        # a Garmin watch and Health Connect can independently auto-detect.
        garmin_match = _find_workout_match(created_at, garmin_index) if created_at else None
        if garmin_match is not None:
            skipped_garmin += 1
            if _augment_garmin_from_google_health(garmin_match, point)["filled"]:
                garmin_augmented += 1
            continue

        # Neither a same-session match — but this point could still overlap a
        # Peloton/Garmin session Google drew different boundaries around: nested
        # inside one, or spanning across one or more (see _find_overlapping_workout).
        # Never augment an overlap-only match: its stats don't describe the same
        # thing as any single existing workout's own stats.
        if created_at is not None:
            duration_str = ex.get("activeDuration", "")
            point_duration = (int(duration_str.rstrip("s")) if duration_str.rstrip("s").isdigit()
                              else _gh_duration_seconds(start_time, ex.get("interval", {}).get("endTime", start_time)))
            if (_find_overlapping_workout(created_at, point_duration, peloton_index) is not None
                    or _find_overlapping_workout(created_at, point_duration, garmin_index) is not None):
                skipped_overlapping += 1
                continue

        try:
            was_created, was_updated = _upsert_google_health_exercise(point)
            created += was_created
            updated += was_updated
        except Exception as e:
            logger.warning("Google Health exercise upsert failed for %s: %s", point.get("name"), e)

    from .models import Integration
    Integration.objects.filter(key="google_health").update(last_synced_at=tz.now())
    return {
        "done": True,
        "created": created,
        "updated": updated,
        "skipped_peloton_duplicates": skipped_peloton,
        "peloton_augmented": augmented,
        "skipped_garmin_duplicates": skipped_garmin,
        "garmin_augmented": garmin_augmented,
        "skipped_overlapping_workouts": skipped_overlapping,
        "google_missing_fields": google_missing_fields,
    }


def _run_google_health_exercise_sync_new(since=None) -> dict:
    """Syncs since `since` (pass the previous Integration.last_synced_at,
    captured by the caller before this run bumps it — see
    _run_google_health_sync_new), or the last 30 days if never synced.

    Deliberately NOT based on the most recent standalone source="google_health"
    CachedWorkout row (the original approach) — a new exercise point can
    legitimately turn out to be a Peloton/Garmin duplicate every single time
    (the normal case when those are the primary recording devices and Google
    Health is a secondary aggregator), which never creates a new standalone
    row. That made the window grow forever: confirmed 2026-08-26, the most
    recent standalone row was from June 12 — over two months stale — despite
    dozens of fully successful syncs in between, because every new point in
    that entire window turned out to be a duplicate. Every "Sync New" click
    was silently re-scanning and re-matching the same ~280 points from that
    whole two-month span instead of just what was actually new."""
    start = (since.date() - timedelta(days=1)) if since else (date.today() - timedelta(days=30))
    return _run_google_health_exercise_sync(start, date.today())


def _run_google_health_exercise_sync_all() -> dict:
    """Full historical backfill — 3 years back is generous for a Pixel Watch
    account; the client's date-bounded pagination stops naturally once it
    runs out of real data well before that."""
    start = date.today() - timedelta(days=365 * 3)
    return _run_google_health_exercise_sync(start, date.today())


def _run_google_health_sync_new() -> dict:
    from .models import Integration

    # Captured before wellness sync runs, since that call bumps
    # Integration.last_synced_at itself — exercise sync needs the timestamp
    # from the *previous* run, not the one this run is about to set.
    integration = Integration.objects.filter(key="google_health").first()
    since = integration.last_synced_at if integration else None

    dates = [date.today() - timedelta(days=i) for i in range(7)]
    wellness = _run_google_health_wellness_sync(dates)
    exercise = _run_google_health_exercise_sync_new(since=since)
    # Catches existing google_health rows whose Garmin match arrived after
    # they synced — the live per-point check in _run_google_health_exercise_sync
    # only sees the Garmin rows that already existed at that moment.
    gh_reconciled = _reconcile_garmin_google_health_duplicates()
    exercise["garmin_merged"] = gh_reconciled["deleted"]
    _reconcile_programs_safe()
    return {"wellness": wellness, "exercise": exercise}


def _run_google_health_sync_all() -> dict:
    dates = [date.today() - timedelta(days=i) for i in range(365 * 3)]
    wellness = _run_google_health_wellness_sync(dates)
    exercise = _run_google_health_exercise_sync_all()
    gh_reconciled = _reconcile_garmin_google_health_duplicates()
    exercise["garmin_merged"] = gh_reconciled["deleted"]
    _reconcile_programs_safe()
    return {"wellness": wellness, "exercise": exercise}


# ---------------------------------------------------------------------------
# Google Health webhook endpoint
#
# Auth model (per developers.google.com/health/webhooks, confirmed live via
# WebFetch 2026-08-17 and re-confirmed 2026-08-23 — see google_health_client.py's
# docstring for the broader pattern of trusting live-confirmed docs over
# guesses): the `endpointAuthorization.secret` we set at subscriber-creation
# time is sent as the literal `Authorization` header on EVERY notification,
# including the two-step verification handshake Google performs when the
# subscriber is created (first POST carries the secret and expects 200/201,
# second POST carries no credentials and expects 401/403).
#
# Response codes differ by request type — this tripped up the original
# implementation, which returned 200 for everything: verification gets
# 200/201 (authorized) or 401/403 (unauthorized), but a REAL data
# notification must get 204 No Content, sent immediately before any
# processing (see google_health_webhook's docstring for why).
#
# Batching: CORRECTED 2026-08-23 — an earlier WebFetch summary of this same
# guide claimed each POST carries exactly one bare notification object, no
# array. That was wrong: a real captured notification (via WebhookError,
# after it 500'd) showed the top-level payload is a JSON ARRAY of envelopes
# even for a single notification. google_health_webhook normalizes to a
# list either way and processes each item independently. Whether Google's
# "up to 99 messages per batch" ever actually puts >1 item in that array
# (vs. separate sequential POSTs each wrapping one item) is still
# unconfirmed — the per-item loop handles either case the same way.
#
# We deliberately do NOT verify the per-message GOOGLE-HEALTH-API-SIGNATURE
# (Base64-encoded Tink/ECDSA-P256 signature over the raw payload, verifiable
# against a keyset Google publishes at
# https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json,
# rotated every 30 days) — the shared secret is judged sufficient for this
# single-user app; see the 2026-08-17 conversation for the explicit scope
# decision. Worth adding later if this app ever has other subscribers/users,
# since the shared secret alone doesn't prove the payload wasn't tampered
# with in transit — but real complexity (a Tink dependency or hand-rolled
# ECDSA verification) that isn't warranted for personal use today.
# ---------------------------------------------------------------------------

# Only data types our sync functions actually know how to handle — a subset
# of the ~24 types Google Health supports webhooks for. Types we don't
# subscribe to (weight, nutrition-log, hydration-log, etc.) are irrelevant
# here since a notification for them should never arrive.
#
# CORRECTED 2026-08-23, live: kebab-case is right, not camelCase. The prior
# fix (same day) went camelCase off the release notes' phrasing — reasonable
# at the time, but wrong. Confirmed instead by directly bisecting real
# subscribers.create calls: every kebab-case data type from the webhooks
# guide (daily-resting-heart-rate, heart-rate-variability, etc.) validates
# and was actually registered; the equivalent camelCase strings all got
# 400 INVALID_ARGUMENT. See google_health_register_webhook.py's
# SUBSCRIBED_DATA_TYPES, now updated to match — that's what's actually
# registered against the live subscriber as of this fix.
#
# This only directly confirms the CREATE-time dataTypes format, not the
# dataType field's casing inside a real notification payload — no live
# notification has arrived yet to check that independently. Both casings
# are included below so dispatch works regardless of which one Google
# actually sends; narrow this once real traffic confirms one or the other.
_GH_WELLNESS_WEBHOOK_TYPES = {
    "daily-resting-heart-rate", "heart-rate-variability", "daily-heart-rate-variability",
    "run-vo2-max", "daily-respiratory-rate", "respiratory-rate-sleep-summary",
    "daily-oxygen-saturation", "active-zone-minutes",
    "dailyRestingHeartRate", "heartRateVariability", "dailyHeartRateVariability",
    "runVo2Max", "dailyRespiratoryRate", "respiratoryRateSleepSummary",
    "dailyOxygenSaturation", "activeZoneMinutes",
    "sleep", "steps", "floors",
}
_GH_EXERCISE_WEBHOOK_TYPES = {"exercise"}


def _gh_webhook_authorized(request) -> bool:
    secret = os.environ.get("GOOGLE_HEALTH_WEBHOOK_SECRET", "")
    if not secret:
        return False
    # Constant-time comparison — the actual security boundary here is the
    # shared secret's secrecy, not this comparison, but avoiding a
    # timing side-channel on a wrong guess is zero-cost to get right.
    return hmac.compare_digest(request.headers.get("Authorization", ""), secret)


def _gh_webhook_interval_dates(interval: dict) -> set:
    """Extract calendar date(s) from one notification interval.

    CONFIRMED LIVE 2026-08-23 from a real notification (captured via
    WebhookError after it 500'd): the actual interval shape is
    civilIso8601TimeInterval ({startTime, endTime} as ISO strings with no
    'Z' suffix) alongside civilDateTimeInterval (structured
    {startDateTime: {date: {year,month,day}, time: {...}}, endDateTime: ...}).
    physicalTimeInterval — the only shape shown in the webhooks guide's
    documented example, which the original implementation assumed — never
    actually appeared; kept as a fallback in case some other data type uses
    it. Tries all three; returns whatever dates could be extracted (0, 1, or
    2), never raises."""
    found = set()

    iso = interval.get("civilIso8601TimeInterval") or interval.get("physicalTimeInterval") or {}
    for key in ("startTime", "endTime"):
        val = iso.get(key)
        if val:
            try:
                found.add(datetime.fromisoformat(val.replace("Z", "+00:00")).date())
            except ValueError:
                pass

    if not found:
        civil = interval.get("civilDateTimeInterval", {})
        for key in ("startDateTime", "endDateTime"):
            d = civil.get(key, {}).get("date", {})
            if d.get("year") and d.get("month") and d.get("day"):
                try:
                    found.add(date(d["year"], d["month"], d["day"]))
                except ValueError:
                    pass

    return found


def _process_google_health_notification(kind, date_list, data_types=()):
    """Background-thread body for google_health_webhook — runs after the
    204 response has already been sent, so an exception here can't affect
    what Google sees on the wire: the notification is already acknowledged
    as delivered and Google won't retry it. WebhookError.record() logs the
    failure to the DB (visible at /settings/integrations/errors/) so it
    isn't only a line in the server log — see that model's docstring.

    kind is "wellness" or "exercise" — already resolved by the caller,
    which coalesces every item of that kind in one webhook delivery into a
    single call here (see google_health_webhook). data_types is the
    originating dataType string(s), kept only for logging/WebhookError
    context since this function no longer branches on it."""
    label = ",".join(sorted(data_types)) if data_types else kind
    try:
        if kind == "exercise":
            _run_google_health_exercise_sync(date_list[0], date_list[-1])
        elif kind == "wellness":
            _run_google_health_wellness_sync(date_list)
        else:
            logger.info("Google Health webhook: kind=%s not handled by any sync path, ignoring", kind)
    except Exception:
        logger.exception("Google Health webhook: sync failed for kind=%s dataTypes=%s dates=%s-%s",
                          kind, label, date_list[0], date_list[-1])
        WebhookError.record(
            source="google_health",
            summary=f"kind={kind} dataTypes={label} dates={date_list[0]}–{date_list[-1]}",
            detail=traceback.format_exc(),
        )


@csrf_exempt
@require_POST
def google_health_webhook(request):
    """
    POST /webhooks/google-health/

    Notify-then-fetch: the payload identifies the affected data type + time
    range, not the values themselves — we fetch via the same sync functions
    files 04/05 already built, scoped to just that range.

    Real notifications must get a 204 sent *before* any processing — "Your
    server must respond to notifications with an HTTP 204 No Content status
    code immediately. To avoid timeouts, process the notification payload
    asynchronously after sending the response" (confirmed live via WebFetch,
    2026-08-23). This app has no task queue (no Celery/RQ), so a plain
    background thread is the lightweight equivalent at this app's scale —
    good enough here, would need revisiting if this ever needs to survive a
    mid-request process restart or run across multiple workers reliably.

    Google explicitly warns retries can send duplicate UPSERT notifications
    for the same interval, so the sync functions this dispatches to must be
    idempotent: DailyStats is get_or_create'd by its unique `date`, and
    _upsert_google_health_exercise upserts by workout_id (unique) with an
    IntegrityError fallback for the case where two overlapping background
    threads race on the same point — see that function's docstring.

    CONFIRMED LIVE 2026-08-23 from a real notification: the top-level
    payload is a JSON ARRAY of notification envelopes (e.g. [{"data": ...}]),
    not a bare object — contradicting the earlier assumption (sourced from a
    WebFetch summary, not directly verified) that each POST carries exactly
    one bare object. That mismatch is what caused every real delivery to
    500: payload.get(...) on a list raises AttributeError. Normalized below
    to a list either way, and each item is processed (and error-isolated)
    independently, so one malformed item in a batch doesn't drop the rest.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)

    items = payload if isinstance(payload, list) else [payload]

    # Everything past this point is wrapped in one broad safety net.
    # Verification/auth are simple enough to have already been proven
    # reliable (curl-verified against production), but ANY unexpected
    # payload shape or runtime condition below must never surface as a
    # 500. A non-204/200/401 response here just makes Google retry a
    # request that will fail the same way every time, burning its 7-day
    # retry budget for nothing, so on any unexpected exception we log the
    # raw payload + traceback to WebhookError (visible at
    # /settings/integrations/errors/, unlike Render's own log stream
    # which has no retrievable history for this) and acknowledge anyway.
    try:
        authorized = _gh_webhook_authorized(request)

        # A verification probe, if present, is the entire point of this
        # delivery — handle it and return immediately rather than also
        # trying to process it as a notification.
        for item in items:
            if isinstance(item, dict) and item.get("type") == "verification":
                return HttpResponse(status=200 if authorized else 401)

        if not authorized:
            logger.warning("Google Health webhook: missing/incorrect Authorization header, rejecting")
            return HttpResponse(status=401)

        # Coalesce every item in this delivery by kind (wellness/exercise)
        # instead of spawning one thread per item. _run_google_health_wellness_sync
        # always fetches all 17 wellness endpoints regardless of which single
        # dataType triggered it, so a batch where several wellness metrics
        # changed at once used to spawn that many redundant concurrent
        # threads, each re-running the full fetch — flooding Google with
        # duplicate calls (429s) and piling up unbounded concurrent threads.
        # 2026-08-23 incident: exactly this pattern spiked memory enough to
        # trigger a Render alert. One thread per kind, covering the union of
        # dates, fixes both.
        wellness_dates, wellness_types = set(), set()
        exercise_dates, exercise_types = set(), set()

        for item in items:
            try:
                data = item.get("data", {}) if isinstance(item, dict) else {}
                data_type = data.get("dataType", "")
                operation = data.get("operation", "UPSERT")
                intervals = data.get("intervals", [])

                dates = set()
                for interval in intervals:
                    dates |= _gh_webhook_interval_dates(interval)

                if not dates:
                    logger.warning("Google Health webhook: no usable interval for dataType=%s, item=%r", data_type, item)
                    continue

                logger.info("Google Health webhook: dataType=%s operation=%s dates=%s-%s",
                            data_type, operation, min(dates), max(dates))

                if data_type in _GH_EXERCISE_WEBHOOK_TYPES:
                    exercise_dates |= dates
                    exercise_types.add(data_type)
                elif data_type in _GH_WELLNESS_WEBHOOK_TYPES:
                    wellness_dates |= dates
                    wellness_types.add(data_type)
                else:
                    logger.info("Google Health webhook: dataType=%s not handled by any sync path, ignoring", data_type)
            except Exception:
                logger.exception("Google Health webhook: failed to process notification item=%r", item)
                WebhookError.record(
                    source="google_health",
                    summary="Failed to process one notification in a batch — see detail for raw item",
                    detail=f"item={item!r}\n\n{traceback.format_exc()}",
                )
                # Keep going — the rest of the batch may still be processable.

        if wellness_dates:
            threading.Thread(
                target=_process_google_health_notification,
                args=("wellness", sorted(wellness_dates)),
                kwargs={"data_types": wellness_types},
                daemon=True,
            ).start()
        if exercise_dates:
            threading.Thread(
                target=_process_google_health_notification,
                args=("exercise", sorted(exercise_dates)),
                kwargs={"data_types": exercise_types},
                daemon=True,
            ).start()

        return HttpResponse(status=204)
    except Exception:
        logger.exception("Google Health webhook: unexpected failure, payload=%r", payload)
        WebhookError.record(
            source="google_health",
            summary="Unexpected failure handling notification — see detail for raw payload",
            detail=f"payload={payload!r}\n\n{traceback.format_exc()}",
        )
        return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Sync API endpoints
# ---------------------------------------------------------------------------

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
        else:
            stats.weight_lb = None
            stats.fat_mass_lb = None
            stats.fat_free_mass_lb = None
            stats.muscle_mass_lb = None
            stats.hydration_lb = None
            stats.bone_mass_lb = None
            stats.fat_ratio_pct = None
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
    if not _integration_enabled("withings"):
        return {**_integration_disabled_result("withings"), "created": 0, "updated": 0}
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
        Integration.objects.filter(key="withings").update(last_synced_at=tz.now())
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
    if not _integration_enabled("withings"):
        return {**_integration_disabled_result("withings"), "created": 0, "updated": 0}
    total_created = total_updated = 0
    try:
        client = _withings_client()
        measurements = client.get_measurements()
        if measurements:
            total_created, total_updated = _upsert_measurements(measurements)
            dates = list({m["measured_at"].astimezone().date() for m in measurements})
            _update_daily_stats_for_dates(dates)
        Integration.objects.filter(key="withings").update(last_synced_at=tz.now())
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


def sync_google_health_new(request):
    """POST /api/sync/google-health/new/"""
    return JsonResponse(_run_google_health_sync_new())


def sync_google_health_all(request):
    """POST /api/sync/google-health/all/"""
    return JsonResponse(_run_google_health_sync_all())


# ---------------------------------------------------------------------------
# Withings webhook endpoint
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
def withings_webhook(request):
    """
    POST /api/withings/webhook/

    Withings notify-then-fetch: the payload carries userid + time window,
    not measurement values. We fetch the measurements inline and run the
    normal upsert pipeline.

    Always returns 200 — Withings auto-cancels subscriptions after 20 days
    of consecutive non-200 responses, so we swallow internal errors here.
    """
    from workouts.models import WithingsAuth

    auth = WithingsAuth.get()
    if not auth:
        logger.error("Withings webhook received but no WithingsAuth row exists")
        return HttpResponse(status=200)

    userid = request.POST.get("userid", "")
    appli = request.POST.get("appli", "")
    startdate = request.POST.get("startdate")
    enddate = request.POST.get("enddate")

    if userid != auth.userid:
        logger.warning(
            "Withings webhook userid mismatch: got %s, expected %s", userid, auth.userid
        )
        return HttpResponse(status=200)

    WithingsAuth.objects.filter(pk=1).update(
        last_webhook_received_at=tz.now(),
        webhook_subscription_active=True,
    )

    try:
        if appli == "1":  # weight / body composition
            start = int(startdate) if startdate else None
            end = int(enddate) if enddate else None
            client = WithingsClient()
            measurements = client.get_measurements(start_date=start, end_date=end)
            if measurements:
                _upsert_measurements(measurements)
                dates = list({m["measured_at"].astimezone().date() for m in measurements})
                _update_daily_stats_for_dates(dates)
            logger.info(
                "Withings webhook synced %d measurements for appli=%s",
                len(measurements), appli,
            )
            Integration.objects.filter(key="withings").update(last_synced_at=tz.now())
        else:
            logger.info("Withings webhook for appli=%s — not handled, ignoring", appli)
    except Exception:
        logger.exception("Withings webhook sync failed (will retry on next webhook)")

    return HttpResponse(status=200)
