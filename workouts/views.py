"""
Page views for Cadence.

Each function here corresponds to a URL and renders an HTML response.
Sync endpoints live in sync.py; AI helpers live in ai.py.
"""

import calendar as _cal
import datetime
import json
import logging
import urllib.parse

from django.db.models import Avg, Count, Max, Min, Q
from django.db.models.functions import TruncWeek
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ai import (
    _get_or_generate_day_analysis, _slug_peloton_avg, _get_or_generate_body_commentary,
    _get_or_generate_nutrition_insights,
)
from .models import (
    CachedWorkout, DailyStats, Intervention, SavedAnalysis, UserSettings,
    NutritionProfile, FoodEntry, SavedMeal, HungerCheck, SideEffectLog, TargetAdjustment,
    AthleteProfile,
)
from .sync import _client, _garmin_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discipline display constants
# ---------------------------------------------------------------------------

DISCIPLINE_COLORS = {
    # cycling family — purple
    "cycling":          "#A78BFA",
    "bike_bootcamp":    "#7C3AED",
    # running / walking family — amber/orange/warm
    "running":          "#FB923C",
    "outdoor_running":  "#FBBF24",
    "walking":          "#FCA5A5",
    # mind-body family — teal/cyan
    "yoga":             "#34D399",
    "stretching":       "#2DD4BF",
    "meditation":       "#67E8F9",
    # other cardio — slate + rose
    "strength":         "#94A3B8",
    "cardio":           "#FB7185",
}

DISCIPLINE_LABELS = {
    "cycling":          "Cycling",
    "bike_bootcamp":    "Bike Bootcamp",
    "running":          "Running",
    "outdoor_running":  "Outdoor Run",
    "strength":         "Strength",
    "yoga":             "Yoga",
    "stretching":       "Stretching",
    "walking":          "Walking",
    "cardio":           "Cardio",
    "meditation":       "Meditation",
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard(request):
    try:
        client = _client()
        overview = client.get_overview()
    except Exception:
        overview = {}

    total_cached = CachedWorkout.objects.count()
    discipline_counts = [
        (slug, DISCIPLINE_LABELS.get(slug, slug.replace("_", " ").title()), count, DISCIPLINE_COLORS.get(slug, "#888888"))
        for slug, count in (
            CachedWorkout.objects
            .values_list("discipline")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
    ]
    return render(request, "workouts/dashboard.html", {
        "overview": overview,
        "discipline_counts": discipline_counts,
        "total_cached": total_cached,
    })


# ---------------------------------------------------------------------------
# Workout history
# ---------------------------------------------------------------------------

def history(request):
    qs = CachedWorkout.objects.all()

    discipline  = request.GET.get("discipline", "")
    instructor  = request.GET.get("instructor", "")
    duration    = request.GET.get("duration", "")
    workout_type = request.GET.get("workout_type", "")
    sort        = request.GET.get("sort", "-created_at")
    date_from   = request.GET.get("date_from", "")
    date_to     = request.GET.get("date_to", "")
    q           = request.GET.get("q", "").strip()

    if q:
        qs = qs.filter(title__icontains=q)
    if discipline:
        qs = qs.filter(discipline=discipline)
    if instructor:
        qs = qs.filter(instructor_name=instructor)
    if duration:
        mins = int(duration)
        qs = qs.filter(duration_seconds__gte=mins * 60, duration_seconds__lt=(mins + 1) * 60)
    if workout_type:
        qs = qs.filter(workout_type=workout_type)
    if date_from:
        try:
            df = datetime.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            qs = qs.filter(created_at__gte=df)
        except ValueError:
            date_from = ""
    if date_to:
        try:
            dt_end = (
                datetime.datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
                + datetime.timedelta(days=1)
            )
            qs = qs.filter(created_at__lt=dt_end)
        except ValueError:
            date_to = ""

    allowed_sorts = {
        "-created_at", "created_at",
        "-output_watts", "-avg_watts", "-avg_cadence",
        "-calories", "-avg_pace_seconds", "avg_pace_seconds",
        "-effort_score", "-distance_miles",
    }
    if sort in allowed_sorts:
        qs = qs.order_by(sort)

    page     = int(request.GET.get("page", 1))
    per_page = 20
    offset   = (page - 1) * per_page
    workouts = qs[offset: offset + per_page]
    total    = qs.count()
    has_next = (offset + per_page) < total

    disc_slugs = (
        CachedWorkout.objects
        .values_list("discipline", flat=True)
        .distinct().order_by("discipline")
    )
    disciplines = [
        (slug, DISCIPLINE_LABELS.get(slug) or slug.replace("_", " ").title())
        for slug in disc_slugs
    ]
    discipline_pills = [
        (slug, display, DISCIPLINE_COLORS.get(slug, "#888888"))
        for slug, display in disciplines
    ]
    durations = sorted(set(
        d // 60
        for d in CachedWorkout.objects
        .exclude(duration_seconds__isnull=True)
        .values_list("duration_seconds", flat=True)
    ))

    instructors = list(
        CachedWorkout.objects
        .exclude(instructor_name__isnull=True).exclude(instructor_name="")
        .values_list("instructor_name", flat=True)
        .distinct().order_by("instructor_name")
    )

    context = {
        "workouts": workouts,
        "disciplines": disciplines,
        "discipline_pills": discipline_pills,
        "discipline_colors": DISCIPLINE_COLORS,
        "durations": durations,
        "instructors": instructors,
        "has_next": has_next,
        "page": page,
        "total": total,
        "filters": {
            "discipline": discipline,
            "instructor": instructor,
            "duration": duration,
            "workout_type": workout_type,
            "sort": sort,
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        },
    }

    if request.htmx:
        return render(request, "workouts/partials/workout_list.html", context)
    return render(request, "workouts/history.html", context)


# ---------------------------------------------------------------------------
# Workout detail — routes to discipline-specific view
# ---------------------------------------------------------------------------

def workout_detail(request, workout_id):
    workout = get_object_or_404(CachedWorkout, workout_id=workout_id)
    if workout.is_run:
        return _run_detail(request, workout)
    elif workout.is_walking:
        return _walking_detail(request, workout)
    elif workout.is_cycling:
        return _cycling_detail(request, workout)
    elif workout.is_strength:
        return _strength_detail(request, workout)
    else:
        return _generic_detail(request, workout)


def _workout_detail_fields(workout):
    """Common detail fields shared by all discipline detail views."""
    return {
        "is_pr": workout.is_pr,
        "average_effort_score": workout.average_effort_score,
        "leaderboard_rank": workout.leaderboard_rank,
        "total_leaderboard_users": workout.total_leaderboard_users,
        "leaderboard_distance_rank": getattr(workout, "leaderboard_distance_rank", None),
        "total_leaderboard_distance_users": getattr(workout, "total_leaderboard_distance_users", None),
        "achievements": workout.achievements,
        "class_description": workout.class_description,
        "difficulty_estimate": workout.difficulty_estimate,
        "strava_id": workout.strava_id,
        "detail_synced": workout.detail_synced_at is not None,
    }


def _run_detail(request, workout):
    client = _client()
    if workout.performance_graph_json:
        perf = workout.performance_graph_json
    else:
        try:
            perf = client.get_parsed_performance(workout.workout_id, every_n=5)
        except Exception:
            perf = {}

    # Inject cached pace targets if the perf graph doesn't include them
    if not perf.get("target_pace") and workout.pace_targets_json:
        cached = workout.pace_targets_json
        valid = [v for v in cached if v is not None]
        if valid:
            perf.setdefault("metrics_by_slug", {})["target_pace"] = {
                "display_name": "Target Pace",
                "display_unit": "min/mi",
                "values": cached,
                "average_value": sum(valid) / len(valid),
                "max_value": None,
                "zones": None,
            }

    splits = perf.get("splits", [])
    split_col_labels = {
        "pace": "Pace", "speed": "Speed", "heart_rate": "Heart Rate",
        "cadence": "Cadence", "resistance": "Resistance",
        "incline": "Incline", "elevation": "Elevation",
    }
    split_keys = [key for key in split_col_labels if any(s.get(key) is not None for s in splits)]

    sibling_qs = (
        CachedWorkout.objects
        .filter(discipline=workout.discipline)
        .exclude(workout_id=workout.workout_id)
    )
    type_stats = sibling_qs.aggregate(
        avg_pace=Avg("avg_pace_seconds"), best_pace=Min("avg_pace_seconds"),
        avg_hr=Avg("heart_rate_avg"), avg_distance=Avg("distance_miles"),
        max_distance=Max("distance_miles"), avg_calories=Avg("calories"),
        avg_cadence=Avg("run_cadence_avg"),
        avg_stride=Avg("stride_length_avg"),
        avg_gct=Avg("ground_contact_time_avg"),
        avg_vo=Avg("vertical_oscillation_avg"),
        avg_vr=Avg("vertical_ratio_avg"),
        count=Count("id"),
    )
    recent_runs_qs = list(
        sibling_qs.order_by("-created_at")
        .values("workout_id", "created_at", "avg_pace_seconds", "distance_miles",
                "heart_rate_avg", "calories", "effort_score", "title",
                "avg_cadence", "stride_length_avg", "vertical_oscillation_avg",
                "vertical_ratio_avg", "ground_contact_time_avg")[:10]
    )
    recent_runs = [
        {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in recent_runs_qs
    ]

    perf["_typeStats"] = {
        "avgPace": type_stats.get("avg_pace"), "bestPace": type_stats.get("best_pace"),
        "avgHR": type_stats.get("avg_hr"), "avgDistance": type_stats.get("avg_distance"),
        "maxDistance": type_stats.get("max_distance"), "avgCalories": type_stats.get("avg_calories"),
        "avgCadence": type_stats.get("avg_cadence"),
        "avgStride": type_stats.get("avg_stride"),
        "avgGCT": type_stats.get("avg_gct"),
        "avgVO": type_stats.get("avg_vo"),
        "avgVR": type_stats.get("avg_vr"),
        "count": type_stats.get("count", 0),
    }

    run_form = {
        "cadence": workout.run_cadence_avg,
        "stride_length": workout.stride_length_avg,
        "vertical_oscillation": workout.vertical_oscillation_avg,
        "vertical_ratio": workout.vertical_ratio_avg,
        "ground_contact_time": workout.ground_contact_time_avg,
    }

    return render(request, "workouts/run_detail.html", {
        "workout": workout,
        "pace_level": perf.get("pace_level"),
        "perf": json.dumps(perf),
        "splits": splits,
        "segments": perf.get("segments", []),
        "split_keys": split_keys,
        "split_col_labels": split_col_labels,
        "muscle_groups": perf.get("muscle_groups", []),
        "effort_zones": perf.get("effort_zones", {}),
        "perf_summaries": perf.get("summaries", {}),
        "perf_avg_summaries": perf.get("average_summaries", {}),
        "class_history": _class_history_sidebar(workout),
        "garmin_activity_url": _garmin_activity_url(workout),
        "type_stats": type_stats,
        "recent_runs": json.dumps(recent_runs),
        "workout_detail": _workout_detail_fields(workout),
        "run_form": run_form,
    })


def _walking_detail(request, workout):
    client = _client()
    if workout.performance_graph_json:
        perf = workout.performance_graph_json
    else:
        try:
            perf = client.get_parsed_performance(workout.workout_id, every_n=5)
        except Exception:
            perf = {}

    splits = perf.get("splits", [])
    split_col_labels = {
        "pace": "Pace", "speed": "Speed", "heart_rate": "Heart Rate",
        "cadence": "Cadence", "incline": "Incline", "elevation": "Elevation",
    }
    split_keys = [key for key in split_col_labels if any(s.get(key) is not None for s in splits)]

    sibling_qs = (
        CachedWorkout.objects
        .filter(discipline=workout.discipline)
        .exclude(workout_id=workout.workout_id)
    )
    type_stats = sibling_qs.aggregate(
        avg_pace=Avg("avg_pace_seconds"), best_pace=Min("avg_pace_seconds"),
        avg_hr=Avg("heart_rate_avg"), avg_distance=Avg("distance_miles"),
        max_distance=Max("distance_miles"), avg_calories=Avg("calories"),
        count=Count("id"),
    )
    recent_walks_qs = list(
        sibling_qs.order_by("-created_at")
        .values("workout_id", "created_at", "avg_pace_seconds", "distance_miles",
                "heart_rate_avg", "calories", "effort_score", "title")[:10]
    )
    recent_walks = [
        {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in recent_walks_qs
    ]

    perf["_typeStats"] = {
        "avgPace": type_stats.get("avg_pace"), "bestPace": type_stats.get("best_pace"),
        "avgHR": type_stats.get("avg_hr"), "avgDistance": type_stats.get("avg_distance"),
        "maxDistance": type_stats.get("max_distance"), "avgCalories": type_stats.get("avg_calories"),
        "count": type_stats.get("count", 0),
    }

    return render(request, "workouts/walking_detail.html", {
        "workout": workout,
        "pace_level": perf.get("pace_level"),
        "perf": json.dumps(perf),
        "splits": splits,
        "segments": perf.get("segments", []),
        "split_keys": split_keys,
        "split_col_labels": split_col_labels,
        "muscle_groups": perf.get("muscle_groups", []),
        "effort_zones": perf.get("effort_zones", {}),
        "perf_summaries": perf.get("summaries", {}),
        "perf_avg_summaries": perf.get("average_summaries", {}),
        "class_history": _class_history_sidebar(workout),
        "garmin_activity_url": _garmin_activity_url(workout),
        "type_stats": type_stats,
        "recent_walks": json.dumps(recent_walks),
        "workout_detail": _workout_detail_fields(workout),
    })


def _cycling_detail(request, workout):
    client = _client()
    if workout.performance_graph_json:
        perf = workout.performance_graph_json
    else:
        try:
            perf = client.get_parsed_performance(workout.workout_id, every_n=5)
        except Exception:
            perf = {}

    sibling_qs = (
        CachedWorkout.objects
        .filter(discipline=workout.discipline)
        .exclude(workout_id=workout.workout_id)
    )
    type_stats = sibling_qs.aggregate(
        avg_output=Avg("output_watts"), max_output=Max("output_watts"),
        avg_power=Avg("avg_watts"), max_power=Max("avg_watts"),
        avg_cadence=Avg("avg_cadence"), avg_resistance=Avg("avg_resistance"),
        avg_hr=Avg("heart_rate_avg"), avg_calories=Avg("calories"),
        count=Count("id"),
    )
    recent_rides_qs = list(
        sibling_qs.order_by("-created_at")
        .values("workout_id", "created_at", "output_watts", "avg_watts",
                "avg_cadence", "avg_resistance", "heart_rate_avg", "calories", "title")[:10]
    )
    recent_rides = [
        {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in recent_rides_qs
    ]

    perf["_typeStats"] = {
        "avgOutput": type_stats.get("avg_output"),
        "maxOutput": type_stats.get("max_output"),
        "avgWatts": type_stats.get("avg_power"),
        "maxWatts": type_stats.get("max_power"),
        "avgCadence": type_stats.get("avg_cadence"),
        "avgResistance": type_stats.get("avg_resistance"),
        "avgHR": type_stats.get("avg_hr"),
        "avgCalories": type_stats.get("avg_calories"),
        "count": type_stats.get("count", 0),
    }

    detail_fields = _workout_detail_fields(workout)
    detail_fields["total_work"] = workout.output_watts

    return render(request, "workouts/cycling_detail.html", {
        "workout": workout,
        "perf": json.dumps(perf),
        "muscle_groups": perf.get("muscle_groups", []),
        "effort_zones": perf.get("effort_zones", {}),
        "perf_summaries": perf.get("summaries", {}),
        "perf_avg_summaries": perf.get("average_summaries", {}),
        "class_history": _class_history_sidebar(workout),
        "type_stats": type_stats,
        "recent_rides": json.dumps(recent_rides),
        "workout_detail": detail_fields,
        "ftp": workout.ftp,
    })


def _strength_detail(request, workout):
    if workout.performance_graph_json:
        perf = workout.performance_graph_json
    elif workout.source != "garmin":
        try:
            perf = _client().get_parsed_performance(workout.workout_id, every_n=5)
        except Exception:
            perf = {}
    else:
        perf = {}

    total_hr_zone_secs = sum(filter(None, [
        workout.hr_z1_seconds, workout.hr_z2_seconds, workout.hr_z3_seconds,
        workout.hr_z4_seconds, workout.hr_z5_seconds,
    ]))
    hr_zones_direct = {
        "z1": workout.hr_z1_seconds or 0,
        "z2": workout.hr_z2_seconds or 0,
        "z3": workout.hr_z3_seconds or 0,
        "z4": workout.hr_z4_seconds or 0,
        "z5": workout.hr_z5_seconds or 0,
    } if total_hr_zone_secs > 0 else None

    sibling_qs = (
        CachedWorkout.objects
        .filter(discipline="strength")
        .exclude(workout_id=workout.workout_id)
    )
    type_stats = sibling_qs.aggregate(
        avg_effort=Avg("effort_score"), max_effort=Max("effort_score"),
        avg_hr=Avg("heart_rate_avg"), avg_calories=Avg("calories"),
        count=Count("id"),
    )
    recent_strength_qs = list(
        sibling_qs.order_by("-created_at")
        .values("workout_id", "created_at", "effort_score", "calories",
                "heart_rate_avg", "title", "duration_seconds")[:10]
    )
    recent_strength = [
        {**r, "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in recent_strength_qs
    ]

    detail_fields = _workout_detail_fields(workout)
    detail_fields.update({
        "movement_tracker_tier": workout.movement_tracker_tier,
        "movements": workout.movements,
        "movement_summary": workout.movement_summary,
    })

    return render(request, "workouts/strength_detail.html", {
        "workout": workout,
        "perf": json.dumps(perf),
        "segments": perf.get("segments", []),
        "muscle_groups": perf.get("muscle_groups", []),
        "effort_zones": perf.get("effort_zones", {}),
        "perf_summaries": perf.get("summaries", {}),
        "class_history": _class_history_sidebar(workout),
        "garmin_activity_url": _garmin_activity_url(workout),
        "type_stats": type_stats,
        "recent_strength": json.dumps(recent_strength),
        "workout_detail": detail_fields,
        "exercise_sets": json.dumps(workout.exercise_sets_json or []),
        "hr_zones_direct": json.dumps(hr_zones_direct),
    })


def _generic_detail(request, workout):
    if workout.performance_graph_json:
        perf = workout.performance_graph_json
    else:
        try:
            perf = _client().get_parsed_performance(workout.workout_id, every_n=5)
        except Exception:
            perf = {}

    total_hr_zone_secs = sum(filter(None, [
        workout.hr_z1_seconds, workout.hr_z2_seconds, workout.hr_z3_seconds,
        workout.hr_z4_seconds, workout.hr_z5_seconds,
    ]))
    hr_zones_direct = {
        "z1": workout.hr_z1_seconds or 0,
        "z2": workout.hr_z2_seconds or 0,
        "z3": workout.hr_z3_seconds or 0,
        "z4": workout.hr_z4_seconds or 0,
        "z5": workout.hr_z5_seconds or 0,
    } if total_hr_zone_secs > 0 else None

    return render(request, "workouts/detail.html", {
        "workout": workout,
        "perf": json.dumps(perf),
        "muscle_groups": perf.get("muscle_groups", []),
        "effort_zones": perf.get("effort_zones", {}),
        "perf_summaries": perf.get("summaries", {}),
        "segments": perf.get("segments", []),
        "class_history": _class_history_sidebar(workout),
        "garmin_activity_url": _garmin_activity_url(workout),
        "workout_detail": _workout_detail_fields(workout),
        "hr_zones_direct": json.dumps(hr_zones_direct),
    })


def _class_history_sidebar(workout):
    """Return the last 5 prior instances of this class/activity (for the detail page sidebar)."""
    if workout.ride_id:
        return (
            CachedWorkout.objects
            .filter(ride_id=workout.ride_id)
            .exclude(workout_id=workout.workout_id)
            .order_by("-created_at")[:5]
        )
    if workout.source == "garmin" and workout.discipline:
        qs = (
            CachedWorkout.objects
            .filter(source="garmin", discipline=workout.discipline)
            .exclude(workout_id=workout.workout_id)
        )
        if workout.title:
            title_qs = qs.filter(title=workout.title).order_by("-created_at")
            if title_qs.exists():
                return title_qs[:5]
        return qs.order_by("-created_at")[:5]
    return []


def _garmin_activity_url(workout):
    """Return the garmin_activity_history URL for a Garmin workout, or None."""
    if workout.source != "garmin" or not workout.discipline:
        return None
    from django.urls import reverse
    url = reverse("garmin_activity_history", args=[workout.discipline])
    if workout.title:
        url += "?" + urllib.parse.urlencode({"title": workout.title})
    return url


# ---------------------------------------------------------------------------
# Class history
# ---------------------------------------------------------------------------

def class_history(request, ride_id):
    workouts = list(CachedWorkout.objects.filter(ride_id=ride_id).order_by("created_at"))

    if not workouts:
        return render(request, "workouts/class_history.html", {
            "workouts": [], "stats": {}, "trend_data": "[]", "ride_id": ride_id,
            "class_title": "Class", "discipline": "", "has_form_data": False,
            "enriched_rows": [], "workout_ids_json": "[]",
        })

    discipline = workouts[0].discipline

    def _pg_summary(pg, slug):
        s = (pg.get("summaries") or {}).get(slug)
        return s.get("value") if isinstance(s, dict) else None

    def _pg_avg_summary(pg, slug):
        s = (pg.get("average_summaries") or {}).get(slug)
        return s.get("value") if isinstance(s, dict) else None

    def _pg_metric_avg(pg, slug):
        m = (pg.get("metrics_by_slug") or {}).get(slug)
        return m.get("average_value") if isinstance(m, dict) else None

    enriched_rows = []
    for w in workouts:
        pg = w.performance_graph_json or {}
        pace_min = _pg_avg_summary(pg, "avg_pace")
        pace_sec = w.avg_pace_seconds or (round(pace_min * 60) if pace_min else None)
        dist = w.distance_miles or _pg_summary(pg, "distance")
        calories = w.calories or _pg_summary(pg, "calories")
        hr = w.heart_rate_avg or _pg_metric_avg(pg, "heart_rate")
        enriched_rows.append({
            "workout": w,
            "avg_pace_seconds": pace_sec,
            "avg_pace_display": f"{pace_sec // 60}:{pace_sec % 60:02d}/mi" if pace_sec else None,
            "distance_miles": dist,
            "calories": calories,
            "heart_rate_avg": hr,
            "leaderboard_pct": w.leaderboard_pct,
            "leaderboard_rank": w.leaderboard_rank,
            "total_leaderboard_users": w.total_leaderboard_users,
            "run_cadence_avg": w.run_cadence_avg,
            "vertical_oscillation_avg": w.vertical_oscillation_avg,
            "ground_contact_time_avg": w.ground_contact_time_avg,
        })

    def _avg(vals): return sum(vals) / len(vals) if vals else None
    def _min(vals): return min(vals) if vals else None
    def _max(vals): return max(vals) if vals else None

    has_form_data = any(r["run_cadence_avg"] for r in enriched_rows)
    has_leaderboard = any(r["leaderboard_pct"] for r in enriched_rows)

    if discipline == "running":
        paces = [r["avg_pace_seconds"] for r in enriched_rows if r["avg_pace_seconds"]]
        dists = [r["distance_miles"] for r in enriched_rows if r["distance_miles"]]
        hrs   = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        cads  = [r["run_cadence_avg"] for r in enriched_rows if r["run_cadence_avg"]]
        vos   = [r["vertical_oscillation_avg"] for r in enriched_rows if r["vertical_oscillation_avg"]]
        gcts  = [r["ground_contact_time_avg"] for r in enriched_rows if r["ground_contact_time_avg"]]
        stats = {
            "best_pace": _min(paces), "avg_pace": _avg(paces),
            "best_distance": _max(dists), "avg_distance": _avg(dists),
            "avg_hr": _avg(hrs), "avg_cadence": _avg(cads),
            "avg_vo": _avg(vos), "avg_gct": _avg(gcts),
            "times_taken": len(workouts),
        }
    elif discipline == "strength":
        efforts      = [r["workout"].average_effort_score for r in enriched_rows if r["workout"].average_effort_score]
        calories_vals = [r["calories"] for r in enriched_rows if r["calories"]]
        hrs          = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        stats = {
            "avg_effort": _avg(efforts), "max_effort": _max(efforts),
            "avg_calories": _avg(calories_vals), "avg_hr": _avg(hrs),
            "times_taken": len(workouts),
        }
    else:
        outputs    = [r["workout"].output_watts for r in enriched_rows if r["workout"].output_watts]
        cads       = [r["workout"].avg_cadence for r in enriched_rows if r["workout"].avg_cadence]
        resistances = [r["workout"].avg_resistance for r in enriched_rows if r["workout"].avg_resistance]
        hrs        = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        stats = {
            "avg_output": _avg(outputs), "max_output": _max(outputs),
            "avg_cadence": _avg(cads), "avg_resistance": _avg(resistances),
            "avg_hr": _avg(hrs), "times_taken": len(workouts),
        }

    trend_data = []
    for r in enriched_rows:
        w = r["workout"]
        lb_pct = (
            round((1 - w.leaderboard_rank / w.total_leaderboard_users) * 100)
            if w.leaderboard_rank and w.total_leaderboard_users else None
        )
        trend_data.append({
            "created_at": w.created_at.isoformat(),
            "workout_id": w.workout_id,
            "output_watts": w.output_watts,
            "avg_cadence": w.avg_cadence,
            "avg_pace_seconds": r["avg_pace_seconds"],
            "distance_miles": r["distance_miles"],
            "effort_score": w.average_effort_score or w.effort_score,
            "leaderboard_pct": lb_pct,
            "heart_rate_avg": r["heart_rate_avg"],
            "calories": r["calories"],
            "movement_tracker_tier": w.movement_tracker_tier,
            "run_cadence_avg": w.run_cadence_avg,
            "vertical_oscillation_avg": w.vertical_oscillation_avg,
            "ground_contact_time_avg": w.ground_contact_time_avg,
        })

    return render(request, "workouts/class_history.html", {
        "enriched_rows": enriched_rows,
        "stats": stats,
        "trend_data": json.dumps(trend_data),
        "ride_id": ride_id,
        "class_title": workouts[0].title,
        "discipline": discipline,
        "has_form_data": has_form_data,
        "has_leaderboard": has_leaderboard,
        "workout_ids_json": json.dumps([w.workout_id for w in workouts]),
    })


def garmin_activity_history(request, discipline):
    title = request.GET.get("title", "").strip()
    from_date = request.GET.get("from_date", "").strip()
    to_date = request.GET.get("to_date", "").strip()

    base_qs = CachedWorkout.objects.filter(source="garmin", discipline=discipline)

    grouped_by_title = False
    if title and base_qs.filter(title=title).count() >= 2:
        base_qs = base_qs.filter(title=title)
        grouped_by_title = True

    if from_date:
        try:
            base_qs = base_qs.filter(created_at__date__gte=from_date)
        except (ValueError, TypeError):
            pass
    if to_date:
        try:
            base_qs = base_qs.filter(created_at__date__lte=to_date)
        except (ValueError, TypeError):
            pass

    workouts = list(base_qs.order_by("created_at"))

    page_title = title if grouped_by_title else discipline.replace("_", " ").title()
    # Walking uses the same column layout as running (pace/distance/HR)
    template_discipline = "running" if discipline == "walking" else discipline

    empty_ctx = {
        "workouts": [], "stats": {}, "trend_data": "[]", "ride_id": "",
        "class_title": page_title, "discipline": template_discipline,
        "has_form_data": False, "has_leaderboard": False,
        "enriched_rows": [], "workout_ids_json": "[]",
        "show_date_filter": True, "from_date": from_date, "to_date": to_date,
        "garmin_discipline": discipline, "grouped_by_title": grouped_by_title, "filter_title": title,
    }
    if not workouts:
        return render(request, "workouts/class_history.html", empty_ctx)

    def _pg_summary(pg, slug):
        s = (pg.get("summaries") or {}).get(slug)
        return s.get("value") if isinstance(s, dict) else None

    def _pg_avg_summary(pg, slug):
        s = (pg.get("average_summaries") or {}).get(slug)
        return s.get("value") if isinstance(s, dict) else None

    def _pg_metric_avg(pg, slug):
        m = (pg.get("metrics_by_slug") or {}).get(slug)
        return m.get("average_value") if isinstance(m, dict) else None

    enriched_rows = []
    for w in workouts:
        pg = w.performance_graph_json or {}
        pace_min = _pg_avg_summary(pg, "avg_pace")
        pace_sec = w.avg_pace_seconds or (round(pace_min * 60) if pace_min else None)
        dist = w.distance_miles or _pg_summary(pg, "distance")
        calories = w.calories or _pg_summary(pg, "calories")
        hr = w.heart_rate_avg or _pg_metric_avg(pg, "heart_rate")
        enriched_rows.append({
            "workout": w,
            "avg_pace_seconds": pace_sec,
            "avg_pace_display": f"{pace_sec // 60}:{pace_sec % 60:02d}/mi" if pace_sec else None,
            "distance_miles": dist,
            "calories": calories,
            "heart_rate_avg": hr,
            "leaderboard_pct": None,
            "leaderboard_rank": None,
            "total_leaderboard_users": None,
            "run_cadence_avg": w.run_cadence_avg,
            "vertical_oscillation_avg": w.vertical_oscillation_avg,
            "ground_contact_time_avg": w.ground_contact_time_avg,
        })

    def _avg(vals): return sum(vals) / len(vals) if vals else None
    def _min(vals): return min(vals) if vals else None
    def _max(vals): return max(vals) if vals else None

    has_form_data = any(r["run_cadence_avg"] for r in enriched_rows)

    if discipline in ("running", "walking"):
        paces = [r["avg_pace_seconds"] for r in enriched_rows if r["avg_pace_seconds"]]
        dists = [r["distance_miles"] for r in enriched_rows if r["distance_miles"]]
        hrs   = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        cads  = [r["run_cadence_avg"] for r in enriched_rows if r["run_cadence_avg"]]
        vos   = [r["vertical_oscillation_avg"] for r in enriched_rows if r["vertical_oscillation_avg"]]
        gcts  = [r["ground_contact_time_avg"] for r in enriched_rows if r["ground_contact_time_avg"]]
        stats = {
            "best_pace": _min(paces), "avg_pace": _avg(paces),
            "best_distance": _max(dists), "avg_distance": _avg(dists),
            "avg_hr": _avg(hrs), "avg_cadence": _avg(cads),
            "avg_vo": _avg(vos), "avg_gct": _avg(gcts),
            "times_taken": len(workouts),
        }
    elif discipline == "strength":
        efforts       = [r["workout"].average_effort_score for r in enriched_rows if r["workout"].average_effort_score]
        calories_vals = [r["calories"] for r in enriched_rows if r["calories"]]
        hrs           = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        stats = {
            "avg_effort": _avg(efforts), "max_effort": _max(efforts),
            "avg_calories": _avg(calories_vals), "avg_hr": _avg(hrs),
            "times_taken": len(workouts),
        }
    else:
        cals = [r["calories"] for r in enriched_rows if r["calories"]]
        hrs  = [r["heart_rate_avg"] for r in enriched_rows if r["heart_rate_avg"]]
        stats = {"avg_calories": _avg(cals), "avg_hr": _avg(hrs), "times_taken": len(workouts)}

    trend_data = []
    for r in enriched_rows:
        w = r["workout"]
        trend_data.append({
            "created_at": w.created_at.isoformat(),
            "workout_id": w.workout_id,
            "output_watts": None,
            "avg_cadence": w.avg_cadence,
            "avg_pace_seconds": r["avg_pace_seconds"],
            "distance_miles": r["distance_miles"],
            "effort_score": w.average_effort_score or w.effort_score,
            "leaderboard_pct": None,
            "heart_rate_avg": r["heart_rate_avg"],
            "calories": r["calories"],
            "movement_tracker_tier": w.movement_tracker_tier,
            "run_cadence_avg": w.run_cadence_avg,
            "vertical_oscillation_avg": w.vertical_oscillation_avg,
            "ground_contact_time_avg": w.ground_contact_time_avg,
        })

    return render(request, "workouts/class_history.html", {
        "enriched_rows": enriched_rows,
        "stats": stats,
        "trend_data": json.dumps(trend_data),
        "ride_id": "",
        "class_title": page_title,
        "discipline": template_discipline,
        "has_form_data": has_form_data,
        "has_leaderboard": False,
        "workout_ids_json": json.dumps([w.workout_id for w in workouts]),
        "show_date_filter": True,
        "from_date": from_date,
        "to_date": to_date,
        "garmin_discipline": discipline,
        "grouped_by_title": grouped_by_title,
        "filter_title": title,
    })


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def compare(request):
    ids_param  = request.GET.get("ids", "")
    workout_ids = [i.strip() for i in ids_param.split(",") if i.strip()][:4]
    workouts   = list(CachedWorkout.objects.filter(workout_id__in=workout_ids))

    perf_data = {}
    client = _client()
    for w in workouts:
        if w.performance_graph_json:
            perf_data[w.workout_id] = w.performance_graph_json
        elif w.source == "garmin":
            perf_data[w.workout_id] = {}
        else:
            try:
                perf_data[w.workout_id] = client.get_parsed_performance(w.workout_id, every_n=5)
            except Exception:
                perf_data[w.workout_id] = {}

    disciplines = {w.discipline for w in workouts}
    if disciplines == {"running"}:
        compare_mode = "run"
    elif disciplines == {"strength"}:
        compare_mode = "strength"
    elif disciplines <= {"cycling", "bike_bootcamp"}:
        compare_mode = "cycling"
    else:
        compare_mode = "mixed"

    splits_comparison = []
    if compare_mode == "run":
        max_miles = max(
            (len(perf_data[w.workout_id].get("splits", [])) for w in workouts),
            default=0,
        )
        for mile in range(1, max_miles + 1):
            row = {"mile": mile}
            for w in workouts:
                splits = perf_data[w.workout_id].get("splits", [])
                row[w.workout_id] = splits[mile - 1] if mile <= len(splits) else None
            splits_comparison.append(row)

    workout_detail_data = {
        w.workout_id: {
            "is_pr": w.is_pr,
            "movement_tracker_tier": w.movement_tracker_tier,
            "movements": w.movements,
            "movement_summary": w.movement_summary,
            "achievements": w.achievements,
            "exercise_sets": w.exercise_sets_json or [],
            "source": w.source,
        }
        for w in workouts
    }

    workout_stats = {
        w.workout_id: {
            "calories": w.calories,
            "effort_score": w.effort_score,
            "heart_rate_avg": w.heart_rate_avg,
            "heart_rate_max": w.heart_rate_max,
            "avg_pace_seconds": w.avg_pace_seconds,
            "distance_miles": w.distance_miles,
            "elevation_gain": w.elevation_gain,
            "avg_incline": w.avg_incline,
            "max_speed_mph": w.max_speed_mph,
            "run_cadence_avg": w.run_cadence_avg,
            "stride_length_avg": w.stride_length_avg,
            "vertical_oscillation_avg": w.vertical_oscillation_avg,
            "vertical_ratio_avg": w.vertical_ratio_avg,
            "ground_contact_time_avg": w.ground_contact_time_avg,
            "output_watts": w.output_watts,
            "avg_watts": w.avg_watts,
            "avg_cadence": w.avg_cadence,
            "avg_resistance": w.avg_resistance,
            "distance": w.distance,
            "leaderboard_rank": w.leaderboard_rank,
            "total_leaderboard_users": w.total_leaderboard_users,
            "leaderboard_pct": w.leaderboard_pct,
            "is_pr": w.is_pr,
            "movement_tracker_tier": w.movement_tracker_tier,
            "movement_summary": w.movement_summary,
            "total_reps": sum(s.get("reps") or 0 for s in (w.exercise_sets_json or [])),
            "total_sets": len([s for s in (w.exercise_sets_json or []) if s.get("reps") is not None or s.get("duration_seconds")]),
            "unique_exercises": len({s.get("exercise") for s in (w.exercise_sets_json or []) if s.get("exercise")}),
        }
        for w in workouts
    }

    return render(request, "workouts/compare.html", {
        "workouts": workouts,
        "perf_data": json.dumps(perf_data),
        "compare_mode": compare_mode,
        "splits_comparison": json.dumps(splits_comparison),
        "workout_ids": [w.workout_id for w in workouts],
        "workout_detail_data": json.dumps(workout_detail_data),
        "workout_stats": json.dumps(workout_stats),
    })


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def analytics_page(request):
    now = timezone.now()
    cutoff_16w  = now - datetime.timedelta(weeks=16)
    cutoff_90d  = now - datetime.timedelta(days=90)
    cutoff_365d = now - datetime.timedelta(days=365)

    # Weekly volume — last 16 weeks, stacked by discipline
    weekly_qs = (
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_16w)
        .annotate(week=TruncWeek("created_at"))
        .values("week", "discipline")
        .annotate(count=Count("id"))
        .order_by("week")
    )

    def _mondays(start, end):
        d = start.date() - datetime.timedelta(days=start.weekday())
        out = []
        while d <= end.date():
            out.append(d)
            d += datetime.timedelta(weeks=1)
        return out

    weeks = _mondays(cutoff_16w, now)
    weekly_labels = [w.isoformat() for w in weeks]

    disc_week_counts: dict[str, dict[str, int]] = {}
    seen_disciplines: set[str] = set()
    for row in weekly_qs:
        d = row["discipline"]
        w = row["week"].date().isoformat()
        seen_disciplines.add(d)
        disc_week_counts.setdefault(d, {})[w] = row["count"]

    ORDERED = ["cycling", "bike_bootcamp", "running", "outdoor_running",
               "strength", "yoga", "stretching", "walking", "cardio", "meditation"]
    known = [d for d in ORDERED if d in seen_disciplines]
    other_discs = seen_disciplines - set(ORDERED)

    weekly_datasets = []
    for disc in known:
        data = [disc_week_counts.get(disc, {}).get(w.isoformat(), 0) for w in weeks]
        weekly_datasets.append({
            "label": DISCIPLINE_LABELS.get(disc, disc.replace("_", " ").title()),
            "discipline": disc,
            "color": DISCIPLINE_COLORS.get(disc, "#888"),
            "data": data,
        })
    if other_discs:
        other_data = [
            sum(disc_week_counts.get(d, {}).get(w.isoformat(), 0) for d in other_discs)
            for w in weeks
        ]
        if any(v > 0 for v in other_data):
            weekly_datasets.append({"label": "Other", "discipline": "", "color": "#555566", "data": other_data})

    # Discipline mix — last 90 days
    mix_qs = (
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_90d)
        .values("discipline")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    discipline_mix = [
        {
            "discipline": row["discipline"],
            "label": DISCIPLINE_LABELS.get(row["discipline"]) or row["discipline"].replace("_", " ").title(),
            "count": row["count"],
            "color": DISCIPLINE_COLORS.get(row["discipline"], "#888888"),
        }
        for row in mix_qs
    ]

    # Performance trends — last 365 days, extracted from performance_graph_json
    perf_qs = (
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_365d, performance_graph_json__isnull=False)
        .order_by("created_at")
        .values("created_at", "discipline", "title", "workout_id", "performance_graph_json")
    )

    cycling_trend, running_trend, strength_trend = [], [], []
    for w in perf_qs:
        perf = w["performance_graph_json"]
        x = int(w["created_at"].timestamp() * 1000)  # ms timestamp for Chart.js
        disc = w["discipline"]
        if disc in ("cycling", "bike_bootcamp"):
            y = _slug_peloton_avg(perf, "output")
            if y is not None:
                cycling_trend.append({"x": x, "y": round(y), "title": w["title"], "id": w["workout_id"]})
        elif disc in ("running", "outdoor_running"):
            if perf.get("source") == "garmin":
                continue  # Garmin directPace is not in decimal min/mi — skip to avoid unit mismatch
            y = _slug_peloton_avg(perf, "pace")
            if y is not None:
                running_trend.append({"x": x, "y": round(y * 60), "title": w["title"], "id": w["workout_id"]})
        elif disc == "strength":
            y = _slug_peloton_avg(perf, "heart_rate")
            if y is not None:
                strength_trend.append({"x": x, "y": round(y), "title": w["title"], "id": w["workout_id"]})

    performance_trends = {}
    if cycling_trend:
        performance_trends["cycling"] = {
            "label": "Avg Power", "color": DISCIPLINE_COLORS["cycling"], "unit": "W", "reverse": False,
            "data": cycling_trend,
        }
    if running_trend:
        performance_trends["running"] = {
            "label": "Avg Pace", "color": DISCIPLINE_COLORS["running"], "unit": "sec/mi", "reverse": True,
            "data": running_trend,
        }
    if strength_trend:
        performance_trends["strength"] = {
            "label": "Avg HR", "color": DISCIPLINE_COLORS["strength"], "unit": "bpm", "reverse": False,
            "data": strength_trend,
        }

    # Cached AI insights — auto-submit a fresh batch if stale
    import os
    settings_obj = UserSettings.get()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    INSIGHTS_AUTO_REFRESH_DAYS = 7
    if (
        api_key
        and not settings_obj.ai_insights_batch_id
        and (
            not settings_obj.ai_insights_generated_at
            or (now - settings_obj.ai_insights_generated_at).days >= INSIGHTS_AUTO_REFRESH_DAYS
        )
    ):
        try:
            from .ai import _submit_insights_batch
            batch_id = _submit_insights_batch(api_key)
            settings_obj.ai_insights_batch_id = batch_id
            settings_obj.save(update_fields=["ai_insights_batch_id"])
        except Exception as e:
            logger.warning("Auto insights generation failed: %s", e)

    return render(request, "workouts/analytics.html", {
        "weekly_labels": json.dumps(weekly_labels),
        "weekly_datasets": json.dumps(weekly_datasets),
        "discipline_mix": json.dumps(discipline_mix),
        "performance_trends": json.dumps(performance_trends),
        "cutoff_90d_iso": cutoff_90d.date().isoformat(),
        "last_insights": settings_obj.ai_insights,
        "insights_generated_at": settings_obj.ai_insights_generated_at,
        "insights_pending": bool(settings_obj.ai_insights_batch_id),
    })


# ---------------------------------------------------------------------------
# Settings & FTP
# ---------------------------------------------------------------------------

def settings_page(request):
    settings_obj = UserSettings.get()
    athlete = AthleteProfile.get()
    return render(request, "workouts/settings.html", {
        "current_ftp": settings_obj.ftp,
        "ftp_updated_at": settings_obj.updated_at,
        "athlete": athlete,
        "experience_choices": AthleteProfile.EXPERIENCE_CHOICES,
        "tone_choices": AthleteProfile.TONE_CHOICES,
    })


@require_POST
def set_athlete_profile(request):
    athlete = AthleteProfile.get()
    athlete.running_experience  = request.POST.get("running_experience", "")
    athlete.cycling_experience  = request.POST.get("cycling_experience", "")
    athlete.strength_experience = request.POST.get("strength_experience", "")
    athlete.training_focus      = request.POST.get("training_focus", "").strip()
    athlete.coaching_tone       = request.POST.get("coaching_tone", "encouraging")
    athlete.health_context_override = request.POST.get("health_context_override", "").strip()
    raw_keywords = request.POST.get("rehab_keywords", "")
    athlete.rehab_keywords = [k.strip().lower() for k in raw_keywords.split(",") if k.strip()]
    athlete.save()
    return redirect("settings")


@require_POST
def set_ftp(request):
    ftp_val = request.POST.get("ftp", "").strip()
    settings_obj = UserSettings.get()
    if ftp_val:
        try:
            settings_obj.ftp = max(1, int(ftp_val))
        except ValueError:
            from django.http import JsonResponse
            return JsonResponse({"error": "invalid ftp"}, status=400)
    else:
        settings_obj.ftp = None
    settings_obj.save()
    return redirect(request.POST.get("next") or "settings")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def calendar_view(request, year=None, month=None):
    today = datetime.date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month
    year, month = int(year), int(month)

    first_of_month  = datetime.date(year, month, 1)
    prev_month_last = first_of_month - datetime.timedelta(days=1)
    month_end       = datetime.date(year, month, _cal.monthrange(year, month)[1])
    next_month_first = month_end + datetime.timedelta(days=1)

    # Query with a one-day buffer on each side to catch workouts that straddle midnight UTC.
    workouts_qs = CachedWorkout.objects.filter(
        created_at__date__gte=first_of_month - datetime.timedelta(days=1),
        created_at__date__lte=month_end + datetime.timedelta(days=1),
    ).values("workout_id", "discipline", "created_at", "duration_seconds", "title")

    workouts_by_date: dict[datetime.date, list] = {}
    for w in workouts_qs:
        d = timezone.localtime(w["created_at"]).date()
        if first_of_month <= d <= month_end:
            workouts_by_date.setdefault(d, []).append(w)

    stats_by_date = {
        s.date: s
        for s in DailyStats.objects.filter(date__gte=first_of_month, date__lte=month_end)
    }

    # Nutrition status per day (for calendar dots)
    from .nutrition import compute_macro_targets
    nutrition_profile = NutritionProfile.objects.filter(pk=1).first()
    nutrition_targets = compute_macro_targets(nutrition_profile) if nutrition_profile else None
    cal_t = nutrition_targets.get("calories") if nutrition_targets else None
    prot_t = nutrition_targets.get("protein_g") if nutrition_targets else None
    fiber_t = nutrition_targets.get("fiber_g") if nutrition_targets else None

    def _nutrition_status(stats):
        if not stats or stats.cal_total is None:
            return None
        if not nutrition_targets:
            return "logged"
        all_ok = True
        if cal_t and stats.cal_total > cal_t * 1.1:
            all_ok = False
        if prot_t and (stats.protein_g_total or 0) < prot_t * 0.9:
            all_ok = False
        if fiber_t and (stats.fiber_g_total or 0) < fiber_t * 0.85:
            all_ok = False
        return "green" if all_ok else "yellow"

    grid = []
    for week in _cal.monthcalendar(year, month):
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(None)
            else:
                d = datetime.date(year, month, day_num)
                s = stats_by_date.get(d)
                row.append({
                    "date": d,
                    "is_today": d == today,
                    "is_future": d > today,
                    "workouts": workouts_by_date.get(d, []),
                    "stats": s,
                    "nutrition_status": _nutrition_status(s),
                })
        grid.append(row)

    used_disciplines = {w["discipline"] for ws in workouts_by_date.values() for w in ws}
    legend_colors = {d: c for d, c in DISCIPLINE_COLORS.items() if d in used_disciplines}

    today_stats, _ = DailyStats.objects.get_or_create(date=today)
    next_workout_rec = today_stats.ai_next_workout or None

    return render(request, "workouts/calendar.html", {
        "grid": grid,
        "year": year,
        "month": month,
        "month_name": first_of_month.strftime("%B %Y"),
        "prev_year": prev_month_last.year,
        "prev_month": prev_month_last.month,
        "next_year": next_month_first.year if next_month_first.year == year or next_month_first <= today else None,
        "next_month": next_month_first.month,
        "today": today,
        "next_workout_rec": next_workout_rec,
        "today_stats": today_stats,
        "discipline_colors": DISCIPLINE_COLORS,
        "legend_colors": legend_colors,
    })


# ---------------------------------------------------------------------------
# Day view
# ---------------------------------------------------------------------------

def day_view(request, date_str):
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        raise Http404

    workouts = list(
        CachedWorkout.objects.filter(created_at__date=day).order_by("created_at")
    )

    stats, created = DailyStats.objects.get_or_create(date=day)
    is_today = (day == datetime.date.today())
    stale = (
        stats.synced_at is None or
        (is_today and (timezone.now() - stats.synced_at).total_seconds() > 7200)
    )
    if created or stale:
        try:
            client = _garmin_client()
            data = client.get_wellness_data(date_str)
            for field, value in data.items():
                setattr(stats, field, value)
            stats.synced_at = timezone.now()
            stats.save()
        except Exception as e:
            logger.warning("Day view wellness sync failed for %s: %s", date_str, e)

    ai_analysis = _get_or_generate_day_analysis(day, workouts, stats)

    today = datetime.date.today()
    prev_day = day - datetime.timedelta(days=1)
    next_day = day + datetime.timedelta(days=1)

    # Nutrition for this day
    from .nutrition import compute_macro_targets
    nutrition_entries = list(FoodEntry.objects.filter(date=day).order_by("logged_at"))
    nutrition_profile = NutritionProfile.objects.filter(pk=1).first()
    nutrition_targets = compute_macro_targets(nutrition_profile) if nutrition_profile else None
    nutrition_totals = None
    if nutrition_entries:
        nutrition_totals = {
            "cal": round(sum(e.calories for e in nutrition_entries)),
            "prot": round(sum(e.protein_g for e in nutrition_entries)),
            "carbs": round(sum(e.carbs_g for e in nutrition_entries)),
            "fat": round(sum(e.fat_g for e in nutrition_entries)),
            "fiber": round(sum(e.fiber_g for e in nutrition_entries), 1),
        }

    return render(request, "workouts/day_view.html", {
        "day": day,
        "is_today": day == today,
        "workouts": workouts,
        "stats": stats,
        "ai_analysis": ai_analysis,
        "prev_day": prev_day,
        "next_day": next_day if next_day <= today else None,
        "discipline_colors": DISCIPLINE_COLORS,
        "nutrition_entries": nutrition_entries,
        "nutrition_targets": nutrition_targets,
        "nutrition_totals": nutrition_totals,
    })


# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------

def interventions_list(request):
    if request.method == "POST":
        from .models import DoseChange
        name         = request.POST.get("name", "").strip()
        category     = request.POST.get("category", "other")
        initial_dose = request.POST.get("initial_dose", "").strip()
        start_str    = request.POST.get("start_date", "").strip()
        end_str      = request.POST.get("end_date", "").strip()
        notes        = request.POST.get("notes", "").strip()
        effects      = request.POST.get("expected_effects", "").strip()
        if name and start_str:
            try:
                start = datetime.date.fromisoformat(start_str)
                end   = datetime.date.fromisoformat(end_str) if end_str else None
                iv = Intervention.objects.create(
                    name=name, category=category,
                    start_date=start, end_date=end,
                    notes=notes, expected_effects=effects,
                )
                if initial_dose:
                    DoseChange.objects.create(
                        intervention=iv,
                        dose=initial_dose,
                        start_date=start,
                        end_date=end,
                    )
            except ValueError:
                pass
        return redirect("interventions")

    all_ivs  = Intervention.objects.all()
    active   = [iv for iv in all_ivs if iv.is_active]
    ended    = [iv for iv in all_ivs if not iv.is_active]
    return render(request, "workouts/interventions.html", {
        "active_interventions": active,
        "ended_interventions": ended,
        "category_choices": Intervention.CATEGORY_CHOICES,
    })


def intervention_edit(request, pk):
    iv = get_object_or_404(Intervention, pk=pk)
    if request.method == "POST":
        iv.name     = request.POST.get("name", iv.name).strip()
        iv.category = request.POST.get("category", iv.category)
        end_str     = request.POST.get("end_date", "").strip()
        start_str   = request.POST.get("start_date", "").strip()
        iv.notes    = request.POST.get("notes", "").strip()
        iv.expected_effects = request.POST.get("expected_effects", "").strip()
        try:
            if start_str:
                iv.start_date = datetime.date.fromisoformat(start_str)
            iv.end_date = datetime.date.fromisoformat(end_str) if end_str else None
        except ValueError:
            pass
        iv.save()
        return redirect("interventions")
    return render(request, "workouts/intervention_edit.html", {
        "iv": iv,
        "category_choices": Intervention.CATEGORY_CHOICES,
    })


def intervention_end(request, pk):
    if request.method != "POST":
        return redirect("interventions")
    iv = get_object_or_404(Intervention, pk=pk)
    if iv.end_date is None:
        iv.end_date = datetime.date.today()
    else:
        iv.end_date = None  # toggle: re-open
    iv.save()
    return redirect("interventions")


def intervention_delete(request, pk):
    if request.method != "POST":
        return redirect("interventions")
    iv = get_object_or_404(Intervention, pk=pk)
    iv.delete()
    return redirect("interventions")


def intervention_detail(request, pk):
    from .models import DoseChange
    intervention = get_object_or_404(Intervention, pk=pk)
    dose_changes = intervention.dose_changes.order_by("-start_date")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_dose":
            dose = request.POST.get("dose", "").strip()
            start_str = request.POST.get("start_date", "")
            end_str = request.POST.get("end_date", "").strip()
            auto_end = request.POST.get("auto_end_previous") == "1"

            if dose and start_str:
                new_start = datetime.date.fromisoformat(start_str)
                new_end = datetime.date.fromisoformat(end_str) if end_str else None

                if auto_end:
                    active = intervention.dose_changes.filter(end_date__isnull=True).first()
                    if active and active.start_date < new_start:
                        active.end_date = new_start - datetime.timedelta(days=1)
                        active.save()

                DoseChange.objects.create(
                    intervention=intervention,
                    dose=dose,
                    start_date=new_start,
                    end_date=new_end,
                    notes=request.POST.get("notes", ""),
                )

        elif action == "edit_dose":
            dc_id = request.POST.get("dose_change_id")
            dc = get_object_or_404(DoseChange, pk=dc_id, intervention=intervention)
            dc.dose = request.POST.get("dose", dc.dose).strip()
            dc.start_date = datetime.date.fromisoformat(
                request.POST.get("start_date", str(dc.start_date))
            )
            end_str = request.POST.get("end_date", "").strip()
            dc.end_date = datetime.date.fromisoformat(end_str) if end_str else None
            dc.notes = request.POST.get("notes", "")
            dc.save()

        elif action == "end_dose":
            dc_id = request.POST.get("dose_change_id")
            dc = get_object_or_404(DoseChange, pk=dc_id, intervention=intervention)
            dc.end_date = datetime.date.today()
            dc.save()

        elif action == "delete_dose":
            dc_id = request.POST.get("dose_change_id")
            DoseChange.objects.filter(pk=dc_id, intervention=intervention).delete()

        elif action == "edit_intervention":
            intervention.name = request.POST.get("name", intervention.name)
            intervention.category = request.POST.get("category", intervention.category)
            intervention.notes = request.POST.get("notes", intervention.notes)
            intervention.expected_effects = request.POST.get("expected_effects", intervention.expected_effects)
            start_str = request.POST.get("start_date")
            if start_str:
                intervention.start_date = datetime.date.fromisoformat(start_str)
            end_str = request.POST.get("end_date", "").strip()
            intervention.end_date = datetime.date.fromisoformat(end_str) if end_str else None
            intervention.save()

        return redirect("intervention_detail", pk=pk)

    active_dose = (
        intervention.dose_changes.filter(end_date__isnull=True).order_by("-start_date").first()
    )

    return render(request, "workouts/intervention_detail.html", {
        "intervention": intervention,
        "dose_changes": dose_changes,
        "active_dose": active_dose,
        "category_choices": Intervention.CATEGORY_CHOICES,
    })


def intervention_quick_dose(request, pk):
    """POST: add a new dose change, auto-end previous. Returns redirect to list."""
    from .models import DoseChange
    intervention = get_object_or_404(Intervention, pk=pk)
    if request.method == "POST":
        dose = request.POST.get("dose", "").strip()
        start_str = request.POST.get("start_date", str(datetime.date.today()))
        if dose:
            new_start = datetime.date.fromisoformat(start_str)
            active = intervention.dose_changes.filter(end_date__isnull=True).first()
            if active and active.start_date < new_start:
                active.end_date = new_start - datetime.timedelta(days=1)
                active.save()
            DoseChange.objects.create(
                intervention=intervention,
                dose=dose,
                start_date=new_start,
                end_date=None,
            )
    return redirect("interventions")


# ---------------------------------------------------------------------------
# Body page
# ---------------------------------------------------------------------------

def _rolling_avg(values, window):
    """values: list of (date, float|None). Returns list of (date, float|None)."""
    result = []
    for i, (d, v) in enumerate(values):
        window_vals = [vv for _, vv in values[max(0, i - window + 1):i + 1] if vv is not None]
        result.append((d, sum(window_vals) / len(window_vals) if window_vals else None))
    return result


def body_view(request):
    range_param = request.GET.get("range", "30d")
    range_days  = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": 3650}.get(range_param, 30)

    today   = datetime.date.today()
    cutoff  = today - datetime.timedelta(days=range_days)
    stats_qs = list(
        DailyStats.objects.filter(date__gte=cutoff, date__lte=today)
        .order_by("date")
    )

    # Build weight data series
    weight_pairs  = [(s.date, s.weight_lb) for s in stats_qs]
    fat_pct_pairs = [(s.date, s.fat_ratio_pct) for s in stats_qs]
    fat_lb_pairs  = [(s.date, s.fat_mass_lb) for s in stats_qs]
    lean_pairs    = [(s.date, s.fat_free_mass_lb) for s in stats_qs]
    hydration_pairs = [(s.date, s.hydration_lb) for s in stats_qs]

    rolling7  = _rolling_avg(weight_pairs, 7)
    rolling30 = _rolling_avg(weight_pairs, 30)

    weight_data_json = json.dumps([
        {
            "date":       d.isoformat(),
            "weight":     w,
            "fat_ratio":  fat_pct_pairs[i][1],
            "lean":       lean_pairs[i][1],
            "fat_mass":   fat_lb_pairs[i][1],
            "hydration":  hydration_pairs[i][1],
            "roll7":      rolling7[i][1],
            "roll30":     rolling30[i][1],
        }
        for i, (d, w) in enumerate(weight_pairs)
    ])

    # Recovery sparkline data
    recovery_data_json = json.dumps([
        {
            "date":        s.date.isoformat(),
            "hrv":         s.hrv_last_night or s.hrv_weekly_avg,
            "rhr":         s.resting_hr,
            "sleep_score": s.sleep_score,
            "bb_high":     s.body_battery_high,
            "stress":      s.stress_avg,
        }
        for s in stats_qs
    ])

    # Intervention annotations within range
    from django.db.models import Q as DQ
    ivs_in_range = Intervention.objects.filter(
        start_date__gte=cutoff,
        start_date__lte=today,
    ).order_by("start_date")

    CAT_COLORS = {
        "medication": "#ff35da",
        "supplement": "#B4FF39",
        "hormone":    "#A78BFA",
        "lifestyle":  "#00D1FF",
        "surgery":    "#FF8A65",
        "other":      "#888899",
    }
    interventions_json = json.dumps([
        {
            "date":  iv.start_date.isoformat(),
            "label": iv.name,
            "color": CAT_COLORS.get(iv.category, "#888"),
        }
        for iv in ivs_in_range
    ])

    # Dose-change annotations within range
    from .models import DoseChange
    dose_annotations = []
    for iv in Intervention.objects.filter(
        start_date__lte=today
    ).filter(
        DQ(end_date__gte=cutoff) | DQ(end_date__isnull=True)
    ):
        for dc in iv.dose_changes.filter(
            start_date__gte=cutoff,
            start_date__lte=today,
        ).order_by("start_date"):
            dose_annotations.append({
                "date":  dc.start_date.isoformat(),
                "label": dc.dose,
                "color": CAT_COLORS.get(iv.category, "#888"),
            })
    dose_annotations_json = json.dumps(dose_annotations)

    # Active interventions for sidebar
    active_ivs = [iv for iv in Intervention.objects.all() if iv.is_active]

    # Current stats (latest available)
    current_weight  = next((s.weight_lb for s in reversed(stats_qs) if s.weight_lb), None)
    current_fat_pct = next((s.fat_ratio_pct for s in reversed(stats_qs) if s.fat_ratio_pct), None)
    current_lean    = next((s.fat_free_mass_lb for s in reversed(stats_qs) if s.fat_free_mass_lb), None)

    # 7-day change
    cutoff_7d    = today - datetime.timedelta(days=7)
    week_ago_qs  = [s for s in stats_qs if s.date <= cutoff_7d]
    prev_weight  = next((s.weight_lb for s in reversed(week_ago_qs) if s.weight_lb), None)
    delta_7d     = round(current_weight - prev_weight, 1) if (current_weight and prev_weight) else None

    # 30-day change
    cutoff_30d   = today - datetime.timedelta(days=30)
    month_ago_qs = [s for s in stats_qs if s.date <= cutoff_30d]
    prev_weight_30 = next((s.weight_lb for s in reversed(month_ago_qs) if s.weight_lb), None) if month_ago_qs else None
    delta_30d = round(current_weight - prev_weight_30, 1) if (current_weight and prev_weight_30) else None

    # Recent symptoms (last 3 days)
    three_days_ago = today - datetime.timedelta(days=2)
    recent_symptoms = list(SideEffectLog.objects.filter(date__gte=three_days_ago).order_by("-timestamp")[:10])

    # Pattern insight headline — extract the "Highest-confidence pattern" from cached insights
    pattern_insight_headline = None
    _ps = UserSettings.get()
    if _ps.ai_pattern_insights:
        for line in _ps.ai_pattern_insights.splitlines():
            line = line.strip()
            if line.startswith("## Highest") and not pattern_insight_headline:
                continue
            if pattern_insight_headline is None and line and not line.startswith("#"):
                pattern_insight_headline = line
                break

    # Body commentary
    commentary = _get_or_generate_body_commentary()

    # Nutrition 7-day summary for body page card
    from .nutrition import compute_macro_targets
    nutrition_profile = NutritionProfile.objects.filter(pk=1).first()
    nutrition_targets = compute_macro_targets(nutrition_profile) if nutrition_profile else None
    week_ago_date = today - datetime.timedelta(days=6)
    nutrition_week = list(
        DailyStats.objects.filter(date__gte=week_ago_date, date__lte=today)
        .exclude(cal_total__isnull=True)
        .values("date", "cal_total", "protein_g_total", "fiber_g_total")
    )
    if nutrition_week:
        nutrition_avg_cal = round(sum(r["cal_total"] for r in nutrition_week) / len(nutrition_week))
        nutrition_avg_prot = round(sum(r["protein_g_total"] or 0 for r in nutrition_week) / len(nutrition_week))
        nutrition_avg_fiber = round(sum(r["fiber_g_total"] or 0 for r in nutrition_week) / len(nutrition_week), 1)
    else:
        nutrition_avg_cal = nutrition_avg_prot = nutrition_avg_fiber = None

    return render(request, "workouts/body.html", {
        "range_param": range_param,
        "ranges": [("7d", "7D"), ("30d", "30D"), ("90d", "90D"), ("1y", "1Y"), ("all", "All")],
        "weight_data_json": weight_data_json,
        "recovery_data_json": recovery_data_json,
        "interventions_json": interventions_json,
        "dose_annotations_json": dose_annotations_json,
        "active_ivs": active_ivs,
        "current_weight": current_weight,
        "current_fat_pct": current_fat_pct,
        "current_lean": current_lean,
        "delta_7d": delta_7d,
        "delta_30d": delta_30d,
        "commentary": commentary,
        "nutrition_targets": nutrition_targets,
        "nutrition_avg_cal": nutrition_avg_cal,
        "nutrition_avg_prot": nutrition_avg_prot,
        "nutrition_avg_fiber": nutrition_avg_fiber,
        "nutrition_days_tracked": len(nutrition_week),
        "recent_symptoms": recent_symptoms,
        "pattern_insight_headline": pattern_insight_headline,
    })


def intervention_analysis_view(request):
    all_interventions = Intervention.objects.all().order_by("-start_date")
    interventions_data = []
    for iv in all_interventions:
        doses = list(iv.dose_changes.order_by("start_date").values("id", "dose", "start_date", "end_date"))
        interventions_data.append({
            "id": iv.id,
            "name": iv.name,
            "category": iv.category,
            "start_date": iv.start_date.isoformat(),
            "end_date": iv.end_date.isoformat() if iv.end_date else None,
            "doses": [
                {
                    **{k: v for k, v in d.items() if k not in ("start_date", "end_date")},
                    "start_date": d["start_date"].isoformat(),
                    "end_date": d["end_date"].isoformat() if d["end_date"] else None,
                }
                for d in doses
            ],
        })
    saved_analyses = SavedAnalysis.objects.all().order_by("-created_at")[:20]
    return render(request, "workouts/intervention_analysis.html", {
        "interventions": all_interventions,
        "interventions_data": json.dumps(interventions_data),
        "saved_analyses": saved_analyses,
    })


def run_analysis_api(request):
    """POST /api/trends/run/ — run a before/after analysis, return JSON."""
    from django.http import JsonResponse
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])

    import json as _json
    try:
        body = _json.loads(request.body)
    except Exception:
        body = {}

    def _parse_date(val):
        if not val:
            return None
        try:
            return datetime.date.fromisoformat(str(val))
        except ValueError:
            return None

    before_start = _parse_date(body.get("before_start"))
    before_end   = _parse_date(body.get("before_end"))
    after_start  = _parse_date(body.get("after_start"))
    after_end    = _parse_date(body.get("after_end"))
    weight_goal  = body.get("weight_goal", "loss")
    washout_days = int(body.get("washout_days", 0) or 0)

    if not all([before_start, before_end, after_start, after_end]):
        return JsonResponse({"error": "Missing required date parameters."}, status=400)

    from .analysis import run_intervention_analysis
    from .nutrition import get_nutrition_gap
    result = run_intervention_analysis(
        before_start=before_start,
        before_end=before_end,
        after_start=after_start,
        after_end=after_end,
        weight_goal=weight_goal,
        washout_days=washout_days,
    )

    # Nutrition data gap warning
    result["nutrition_gaps"] = {
        "before": get_nutrition_gap(before_start, before_end),
        "after": get_nutrition_gap(after_start, after_end),
    }

    # Convert dates to strings for JSON serialisation
    result["before_start"] = result["before_start"].isoformat()
    result["before_end"]   = result["before_end"].isoformat()
    result["after_start"]  = result["after_start"].isoformat()
    result["after_end"]    = result["after_end"].isoformat()

    return JsonResponse(result)


def save_analysis_api(request):
    """POST /api/trends/save/ — generate AI interpretation, save SavedAnalysis."""
    from django.http import JsonResponse
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])

    import json as _json
    try:
        body = _json.loads(request.body)
    except Exception:
        body = {}

    def _parse_date(val):
        if not val:
            return None
        try:
            return datetime.date.fromisoformat(str(val))
        except ValueError:
            return None

    label       = body.get("label", "").strip() or "Untitled Analysis"
    iv_id       = body.get("intervention_id")
    before_start = _parse_date(body.get("before_start"))
    before_end   = _parse_date(body.get("before_end"))
    after_start  = _parse_date(body.get("after_start"))
    after_end    = _parse_date(body.get("after_end"))
    window_days  = int(body.get("window_days", 28) or 28)
    washout_days = int(body.get("washout_days", 0) or 0)
    weight_goal  = body.get("weight_goal", "loss")
    metrics_json = body.get("metrics_json") or {}

    intervention = None
    if iv_id:
        try:
            intervention = Intervention.objects.get(pk=int(iv_id))
        except (Intervention.DoesNotExist, ValueError):
            pass

    # Build analysis result for AI context
    from .analysis import run_intervention_analysis
    from .ai import _generate_intervention_interpretation, _interventions_context

    if before_start and before_end and after_start and after_end:
        analysis_result = run_intervention_analysis(
            before_start=before_start,
            before_end=before_end,
            after_start=after_start,
            after_end=after_end,
            weight_goal=weight_goal,
        )
        analysis_result["before_start"] = before_start
        analysis_result["before_end"]   = before_end
        analysis_result["after_start"]  = after_start
        analysis_result["after_end"]    = after_end

        iv_ctx = _interventions_context(before_start, after_end) if before_start and after_end else ""
        from .nutrition import get_nutrition_gap
        n_gaps = {
            "before": get_nutrition_gap(before_start, before_end),
            "after": get_nutrition_gap(after_start, after_end),
        }
        ai_text = _generate_intervention_interpretation(
            analysis_result,
            intervention=intervention,
            interventions_context_str=iv_ctx,
            nutrition_gaps=n_gaps,
        )
    else:
        ai_text = ""

    sa = SavedAnalysis.objects.create(
        label=label,
        intervention=intervention,
        before_start=before_start or datetime.date.today(),
        before_end=before_end or datetime.date.today(),
        after_start=after_start or datetime.date.today(),
        after_end=after_end or datetime.date.today(),
        window_days=window_days,
        washout_days=washout_days,
        weight_goal=weight_goal,
        metrics_json=metrics_json,
        ai_interpretation=ai_text,
    )
    return JsonResponse({"ok": True, "id": sa.pk, "ai_text": ai_text})


def saved_analysis_detail(request, pk):
    sa = get_object_or_404(SavedAnalysis, pk=pk)
    return render(request, "workouts/saved_analysis_detail.html", {"sa": sa})


def saved_analysis_delete(request, pk):
    sa = get_object_or_404(SavedAnalysis, pk=pk)
    if request.method == "POST":
        sa.delete()
    return redirect("body")


# ---------------------------------------------------------------------------
# Nutrition views
# ---------------------------------------------------------------------------

def nutrition_page(request):
    from django.db.models import Sum
    from .nutrition import compute_macro_targets, compute_streaks, get_weekly_stats, get_yesterday_recap, get_satisfying_meals

    date_str = request.GET.get("date", "")
    try:
        page_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        page_date = datetime.date.today()

    today = datetime.date.today()
    profile = NutritionProfile.objects.filter(pk=1).first()
    targets = compute_macro_targets(profile) if profile else None

    entries = list(FoodEntry.objects.filter(date=page_date))
    totals = FoodEntry.objects.filter(date=page_date).aggregate(
        cal=Sum("calories"),
        prot=Sum("protein_g"),
        carbs=Sum("carbs_g"),
        fat=Sum("fat_g"),
        fiber=Sum("fiber_g"),
    )

    saved_meals = list(SavedMeal.objects.all()[:12])
    satisfying_meals = get_satisfying_meals(min_occurrences=3, top_n=5)

    # Streaks (today-relative only)
    streaks = compute_streaks(reference_date=today) if page_date == today else None

    # Weekly summary
    weekly = get_weekly_stats(page_date, targets=targets)

    # Yesterday recap — shown only when today has no entries yet
    yesterday_recap = None
    if page_date == today and not entries:
        yesterday_recap = get_yesterday_recap(today, targets=targets)

    def _rem(target_key, total_key):
        t = targets.get(target_key) if targets else None
        v = totals.get(total_key) or 0
        if t is None:
            return None
        return max(0, t - v)

    remaining = {
        "cal":    _rem("calories",  "cal"),
        "prot":   _rem("protein_g", "prot"),
        "carbs":  _rem("carbs_g",   "carbs"),
        "fat":    _rem("fat_g",     "fat"),
        "fiber":  _rem("fiber_g",   "fiber"),
    }

    return render(request, "workouts/nutrition.html", {
        "page_date": page_date,
        "prev_date": (page_date - datetime.timedelta(days=1)).isoformat(),
        "next_date": (page_date + datetime.timedelta(days=1)).isoformat(),
        "is_today": page_date == today,
        "targets": targets,
        "profile": profile,
        "entries": entries,
        "totals": totals,
        "remaining": remaining,
        "saved_meals": saved_meals,
        "satisfying_meals": satisfying_meals,
        "streaks": streaks,
        "weekly": weekly,
        "yesterday_recap": yesterday_recap,
        "meal_choices": FoodEntry.MEAL_CHOICES,
    })


def nutrition_targets_page(request):
    from .nutrition import compute_macro_targets

    profile = NutritionProfile.get()

    if request.method == "POST":
        def _float(key, default=None):
            v = request.POST.get(key, "").strip()
            try:
                return float(v) if v else default
            except ValueError:
                return default

        def _int(key, default=None):
            v = request.POST.get(key, "").strip()
            try:
                return int(v) if v else default
            except ValueError:
                return default

        profile.height_cm = _float("height_cm")
        profile.age = _int("age")
        profile.biological_sex = request.POST.get("biological_sex", "female")
        profile.activity_level = request.POST.get("activity_level", "active")
        profile.goal = request.POST.get("goal", "loss")
        profile.deficit_pct = _float("deficit_pct", 20.0)
        profile.protein_g_per_kg_lean = _float("protein_g_per_kg_lean", 2.2)
        profile.manual_calories = _int("manual_calories")
        profile.manual_protein_g = _int("manual_protein_g")
        profile.manual_carbs_g = _int("manual_carbs_g")
        profile.manual_fat_g = _int("manual_fat_g")
        profile.manual_fiber_g = _int("manual_fiber_g")
        profile.save()
        return redirect("nutrition_targets")

    targets = compute_macro_targets(profile)

    from .nutrition import evaluate_target_fit
    try:
        fit = evaluate_target_fit()
    except Exception:
        fit = None

    adjustments = TargetAdjustment.objects.order_by("-timestamp")[:10]

    activ_choices = [
        ("sedentary",   "Sedentary (desk job, little exercise)"),
        ("light",       "Light (1–3 days/wk)"),
        ("moderate",    "Moderate (3–5 days/wk)"),
        ("active",      "Active (6–7 days/wk)"),
        ("very_active", "Very Active (2× training/day)"),
    ]

    return render(request, "workouts/nutrition_targets.html", {
        "profile": profile,
        "targets": targets,
        "activ_choices": activ_choices,
        "fit": fit,
        "adjustments": adjustments,
    })


def nutrition_parse_api(request):
    """POST (HTMX) — parse food text or label image and return editable preview partial."""
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])

    import base64
    from .ai import parse_food_text

    raw_text = request.POST.get("raw_text", "").strip()
    meal = request.POST.get("meal", "").strip()
    page_date = request.POST.get("date", datetime.date.today().isoformat())
    serving_note = request.POST.get("serving_note", "").strip()

    label_image = request.FILES.get("label_image")
    image_b64 = None
    image_media_type = "image/jpeg"

    if label_image:
        content_type = label_image.content_type or "image/jpeg"
        if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return render(request, "workouts/partials/nutrition_parse_result.html", {
                "error": "Unsupported image type. Please upload a JPEG, PNG, or WebP.",
                "raw_text": raw_text,
                "meal": meal,
                "page_date": page_date,
            })
        image_b64 = base64.b64encode(label_image.read()).decode("utf-8")
        image_media_type = content_type

    if not raw_text and not image_b64:
        return render(request, "workouts/partials/nutrition_parse_result.html", {
            "error": "Please describe what you ate or upload a nutrition label.",
            "raw_text": raw_text,
            "meal": meal,
            "page_date": page_date,
        })

    saved_meals_data = list(
        SavedMeal.objects.values("name", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g")
    )
    result = parse_food_text(
        raw_text, meal,
        saved_meals=saved_meals_data,
        image_b64=image_b64,
        image_media_type=image_media_type,
        serving_note=serving_note,
    )

    return render(request, "workouts/partials/nutrition_parse_result.html", {
        "result": result,
        "raw_text": raw_text,
        "meal": meal or result.get("meal_guess", ""),
        "page_date": page_date,
        "meal_choices": FoodEntry.MEAL_CHOICES,
    })


def nutrition_log_api(request):
    """POST — save FoodEntry from editable parse form."""
    from django.http import HttpResponseNotAllowed
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from .nutrition import recompute_daily_nutrition

    date_str = request.POST.get("date", "")
    try:
        entry_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        entry_date = datetime.date.today()

    # Collect indexed items from form: items-0-name, items-0-calories, …
    items = []
    i = 0
    while i < 60:
        name = request.POST.get(f"items-{i}-name", "").strip()
        if not name and i > 0:
            break
        if name:
            items.append({
                "name": name,
                "quantity": request.POST.get(f"items-{i}-quantity", ""),
                "calories":  _safe_float(request.POST.get(f"items-{i}-calories",  "0")),
                "protein_g": _safe_float(request.POST.get(f"items-{i}-protein_g", "0")),
                "carbs_g":   _safe_float(request.POST.get(f"items-{i}-carbs_g",   "0")),
                "fat_g":     _safe_float(request.POST.get(f"items-{i}-fat_g",     "0")),
                "fiber_g":   _safe_float(request.POST.get(f"items-{i}-fiber_g",   "0")),
            })
        i += 1

    if not items:
        return redirect(f"/nutrition/?date={entry_date.isoformat()}")

    FoodEntry.objects.create(
        date=entry_date,
        meal=request.POST.get("meal", ""),
        raw_text=request.POST.get("raw_text", ""),
        items_json=items,
        calories=sum(it["calories"]  for it in items),
        protein_g=sum(it["protein_g"] for it in items),
        carbs_g=sum(it["carbs_g"]   for it in items),
        fat_g=sum(it["fat_g"]       for it in items),
        fiber_g=sum(it["fiber_g"]   for it in items),
        ai_model=request.POST.get("ai_model", ""),
        ai_confidence=request.POST.get("ai_confidence", ""),
        edited_by_user=request.POST.get("edited_by_user", "") == "1",
    )
    recompute_daily_nutrition(entry_date)

    return redirect(f"/nutrition/?date={entry_date.isoformat()}")


def nutrition_delete_api(request, pk):
    """POST — delete a FoodEntry and recompute daily rollup."""
    from django.http import HttpResponseNotAllowed
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from .nutrition import recompute_daily_nutrition

    entry = get_object_or_404(FoodEntry, pk=pk)
    entry_date = entry.date
    entry.delete()
    recompute_daily_nutrition(entry_date)

    return redirect(f"/nutrition/?date={entry_date.isoformat()}")


def nutrition_suggest_api(request):
    """GET (HTMX) — return meal suggestion cards partial."""
    from django.db.models import Sum
    from .nutrition import compute_macro_targets
    from .ai import suggest_meals
    import datetime as dt_mod

    date_str = request.GET.get("date", "")
    try:
        page_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        page_date = datetime.date.today()

    profile = NutritionProfile.objects.filter(pk=1).first()
    targets = compute_macro_targets(profile) if profile else None

    if not targets:
        return render(request, "workouts/partials/nutrition_suggestions.html", {
            "error": "Set up your macro targets first to get meal suggestions.",
            "page_date": page_date,
        })

    totals = FoodEntry.objects.filter(date=page_date).aggregate(
        cal=Sum("calories"),
        prot=Sum("protein_g"),
        carbs=Sum("carbs_g"),
        fat=Sum("fat_g"),
        fiber=Sum("fiber_g"),
    )

    def _rem(target_key, total_key):
        return max(0, (targets.get(target_key) or 0) - (totals[total_key] or 0))

    remaining_cal    = _rem("calories",  "cal")
    remaining_prot   = _rem("protein_g", "prot")
    remaining_carbs  = _rem("carbs_g",   "carbs")
    remaining_fat    = _rem("fat_g",     "fat")
    remaining_fiber  = _rem("fiber_g",   "fiber")

    meals_today = list(FoodEntry.objects.filter(date=page_date).values_list("meal", flat=True))
    meal_summary = ", ".join(m or "log" for m in meals_today) if meals_today else "none"

    now_hour = dt_mod.datetime.now().hour
    if now_hour < 10:
        time_of_day = "morning"
    elif now_hour < 13:
        time_of_day = "late morning / lunch"
    elif now_hour < 17:
        time_of_day = "afternoon"
    elif now_hour < 20:
        time_of_day = "dinner time"
    else:
        time_of_day = "evening"

    # Recent meals (last 3 days) to avoid repeat suggestions
    recent_3d = page_date - datetime.timedelta(days=3)
    recent_meal_names = list(
        FoodEntry.objects.filter(date__gte=recent_3d, date__lte=page_date)
        .exclude(raw_text="")
        .order_by("-logged_at")
        .values_list("raw_text", flat=True)[:10]
    )

    # Top foods from last 30 days for familiarity context
    from .nutrition import get_top_foods as _get_top_foods
    top_foods_data = _get_top_foods(
        page_date - datetime.timedelta(days=30), page_date, top_n=8
    )

    # Most recent hunger check within 4 hours
    from .models import HungerCheck, SideEffectLog
    hunger_cutoff = dt_mod.datetime.now() - dt_mod.timedelta(hours=4)
    recent_hunger = HungerCheck.objects.filter(timestamp__gte=hunger_cutoff).order_by("-timestamp").first()
    current_hunger = recent_hunger.hunger_level if recent_hunger else None

    # GI symptoms (nausea or bloating) in last 24 hours
    gi_cutoff = dt_mod.datetime.now() - dt_mod.timedelta(hours=24)
    gi_symptoms = SideEffectLog.objects.filter(
        timestamp__gte=gi_cutoff,
        symptom__in=["nausea", "bloating"],
    ).exists()

    result = suggest_meals(
        remaining_cal, remaining_prot, remaining_carbs, remaining_fat, remaining_fiber,
        meal_summary, time_of_day,
        recent_meals=recent_meal_names,
        top_foods=top_foods_data,
        current_hunger=current_hunger,
        gi_symptoms=gi_symptoms,
    )

    return render(request, "workouts/partials/nutrition_suggestions.html", {
        "suggestions": result.get("suggestions", []),
        "tip": result.get("tip", ""),
        "gi_note": result.get("gi_note", ""),
        "page_date": page_date,
    })


def nutrition_save_meal_api(request, pk):
    """POST — save a FoodEntry as a SavedMeal for re-logging."""
    from django.http import HttpResponseNotAllowed, JsonResponse
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    entry = get_object_or_404(FoodEntry, pk=pk)
    name = request.POST.get("name", "").strip() or entry.raw_text[:100]

    sm, _ = SavedMeal.objects.get_or_create(
        name=name,
        defaults=dict(
            meal=entry.meal,
            items_json=entry.items_json,
            calories=entry.calories,
            protein_g=entry.protein_g,
            carbs_g=entry.carbs_g,
            fat_g=entry.fat_g,
            fiber_g=entry.fiber_g,
        ),
    )
    from django.db.models import Q
    FoodEntry.objects.filter(
        Q(pk=entry.pk) | Q(source_saved_meal=sm) | Q(raw_text=sm.name)
    ).update(is_favorite=True, source_saved_meal=sm)
    return redirect(f"/nutrition/?date={entry.date.isoformat()}")


def nutrition_relog_api(request, pk):
    """POST — create a FoodEntry from a SavedMeal."""
    from django.http import HttpResponseNotAllowed
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from .nutrition import recompute_daily_nutrition

    saved = get_object_or_404(SavedMeal, pk=pk)
    date_str = request.POST.get("date", "")
    try:
        entry_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        entry_date = datetime.date.today()

    FoodEntry.objects.create(
        date=entry_date,
        meal=saved.meal,
        raw_text=saved.name,
        items_json=saved.items_json,
        calories=saved.calories,
        protein_g=saved.protein_g,
        carbs_g=saved.carbs_g,
        fat_g=saved.fat_g,
        fiber_g=saved.fiber_g,
        is_favorite=True,
        source_saved_meal=saved,
    )
    saved.times_logged = (saved.times_logged or 0) + 1
    saved.save(update_fields=["times_logged"])
    recompute_daily_nutrition(entry_date)

    return redirect(f"/nutrition/?date={entry_date.isoformat()}")


def nutrition_delete_meal_api(request, pk):
    """POST — delete a SavedMeal and unstar any entries that came from it."""
    from django.http import HttpResponseNotAllowed
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    meal = get_object_or_404(SavedMeal, pk=pk)
    FoodEntry.objects.filter(source_saved_meal=meal).update(is_favorite=False, source_saved_meal=None)
    meal.delete()
    return redirect("nutrition")


def nutrition_save_suggestion_api(request):
    """POST — create a SavedMeal directly from a meal suggestion (no FoodEntry required)."""
    from django.http import HttpResponseNotAllowed
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "name required"}, status=400)

    def _f(key):
        try:
            return float(request.POST.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    SavedMeal.objects.get_or_create(
        name=name,
        defaults={
            "calories": _f("calories"),
            "protein_g": _f("protein_g"),
            "carbs_g": _f("carbs_g"),
            "fat_g": _f("fat_g"),
            "fiber_g": _f("fiber_g"),
        },
    )
    return JsonResponse({"ok": True})


def nutrition_entry_row_api(request, pk):
    """GET — return the read-only table row partial for a FoodEntry (used by edit cancel)."""
    entry = get_object_or_404(FoodEntry, pk=pk)
    return render(request, "workouts/partials/nutrition_entry_row.html", {"entry": entry})


def nutrition_edit_api(request, pk):
    """GET — return edit row partial; POST — save edits and redirect."""
    from .nutrition import recompute_daily_nutrition
    entry = get_object_or_404(FoodEntry, pk=pk)

    if request.method == "POST":
        entry.meal = request.POST.get("meal", "")
        raw = request.POST.get("raw_text", "").strip()
        if raw:
            entry.raw_text = raw
        entry.calories = _safe_float(request.POST.get("calories"))
        entry.protein_g = _safe_float(request.POST.get("protein_g"))
        entry.carbs_g = _safe_float(request.POST.get("carbs_g"))
        entry.fat_g = _safe_float(request.POST.get("fat_g"))
        entry.fiber_g = _safe_float(request.POST.get("fiber_g"))
        entry.edited_by_user = True
        entry.save()
        recompute_daily_nutrition(entry.date)
        return redirect(f"/nutrition/?date={entry.date.isoformat()}")

    return render(request, "workouts/partials/nutrition_edit_row.html", {
        "entry": entry,
        "meal_choices": FoodEntry.MEAL_CHOICES,
    })


def _safe_float(val, default=0.0):
    try:
        return float(val) if val else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Nutrition analytics page
# ---------------------------------------------------------------------------

def nutrition_analytics_page(request):
    from django.db.models import Avg, Sum
    from .nutrition import (
        compute_macro_targets, get_top_foods, get_day_of_week_stats,
    )

    range_param = request.GET.get("range", "30d")
    range_days = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(range_param, 30)

    today = datetime.date.today()
    start = today - datetime.timedelta(days=range_days - 1)

    profile = NutritionProfile.objects.filter(pk=1).first()
    targets = compute_macro_targets(profile) if profile else None

    cal_t = targets.get("calories") if targets else None
    prot_t = targets.get("protein_g") if targets else None
    fiber_t = targets.get("fiber_g") if targets else None

    # All logged DailyStats in range
    stats_qs = list(
        DailyStats.objects.filter(date__gte=start, date__lte=today, cal_total__isnull=False)
        .order_by("date")
    )
    total_days = range_days
    logged_days = len(stats_qs)

    # Adherence
    def _avg_n(field):
        vals = [getattr(s, field) or 0 for s in stats_qs]
        return round(sum(vals) / len(vals)) if vals else None

    avg_cal   = _avg_n("cal_total")
    avg_prot  = _avg_n("protein_g_total")
    avg_fiber = _avg_n("fiber_g_total")

    days_hit_cal = None
    if cal_t:
        days_hit_cal = sum(1 for s in stats_qs if s.cal_total and s.cal_total <= cal_t * 1.1)
    days_hit_prot = None
    if prot_t:
        days_hit_prot = sum(1 for s in stats_qs if (s.protein_g_total or 0) >= prot_t * 0.9)
    days_hit_fiber = None
    if fiber_t:
        days_hit_fiber = sum(1 for s in stats_qs if (s.fiber_g_total or 0) >= fiber_t * 0.85)

    avg_prot_gap = None
    if prot_t and logged_days:
        under = [prot_t - (s.protein_g_total or 0) for s in stats_qs if (s.protein_g_total or 0) < prot_t * 0.9]
        avg_prot_gap = round(sum(under) / len(under)) if under else 0

    # Chart data: daily series
    chart_data = [
        {
            "date": s.date.isoformat(),
            "cal": s.cal_total,
            "protein": s.protein_g_total,
            "carbs": s.carbs_g_total,
            "fat": s.fat_g_total,
            "fiber": s.fiber_g_total,
        }
        for s in stats_qs
    ]

    # Day of week patterns
    dow_stats = get_day_of_week_stats(start, today)

    # Top foods
    top_foods = get_top_foods(start, today, top_n=15)

    # Hunger trend data
    hunger_qs = (
        HungerCheck.objects.filter(date__gte=start, date__lte=today)
        .values("date", "context", "hunger_level", "fullness_level")
        .order_by("date")
    )
    hunger_by_date: dict = {}
    for row in hunger_qs:
        d = row["date"].isoformat()
        hunger_by_date.setdefault(d, {"all": [], "morning": [], "fullness": []})
        hunger_by_date[d]["all"].append(row["hunger_level"])
        if row["context"] == "morning":
            hunger_by_date[d]["morning"].append(row["hunger_level"])
        if row["context"] == "post_meal" and row["fullness_level"]:
            hunger_by_date[d]["fullness"].append(row["fullness_level"])

    hunger_chart = [
        {
            "date": d,
            "avg": round(sum(v["all"]) / len(v["all"]), 1) if v["all"] else None,
            "morning_avg": round(sum(v["morning"]) / len(v["morning"]), 1) if v["morning"] else None,
        }
        for d, v in sorted(hunger_by_date.items())
        if v["all"]
    ]
    all_morning = [h["morning_avg"] for h in hunger_chart if h["morning_avg"] is not None]
    hunger_avg_morning = round(sum(all_morning) / len(all_morning), 1) if all_morning else None
    all_fullness = [r["fullness_level"] for r in hunger_qs if r.get("fullness_level")]
    hunger_avg_fullness = round(sum(all_fullness) / len(all_fullness), 1) if all_fullness else None

    # Symptom summary
    symptom_summary = list(
        SideEffectLog.objects.filter(date__gte=start, date__lte=today)
        .values("symptom")
        .annotate(count=Count("id"), avg_sev=Avg("severity"))
        .order_by("-count")
    )

    # AI insights — generate on load if cache is missing or expired (>7 days)
    insights = _get_or_generate_nutrition_insights(range_days=range_days)
    from django.utils import timezone as _tz
    _s = UserSettings.get()
    insights_generated_at = _s.ai_nutrition_insights_generated_at if insights else None

    return render(request, "workouts/nutrition_analytics.html", {
        "range_param": range_param,
        "range_days": range_days,
        "ranges": [("7d", "7D"), ("30d", "30D"), ("90d", "90D"), ("1y", "1Y")],
        "start": start,
        "today": today,
        "targets": targets,
        "total_days": total_days,
        "logged_days": logged_days,
        "avg_cal": avg_cal,
        "avg_prot": avg_prot,
        "avg_fiber": avg_fiber,
        "cal_t": cal_t,
        "prot_t": prot_t,
        "fiber_t": fiber_t,
        "days_hit_cal": days_hit_cal,
        "days_hit_prot": days_hit_prot,
        "days_hit_fiber": days_hit_fiber,
        "avg_prot_gap": avg_prot_gap,
        "chart_data_json": json.dumps(chart_data),
        "dow_stats_json": json.dumps(dow_stats),
        "top_foods": top_foods,
        "hunger_chart_json": json.dumps(hunger_chart),
        "hunger_avg_morning": hunger_avg_morning,
        "hunger_avg_fullness": hunger_avg_fullness,
        "symptom_summary": symptom_summary,
        "insights": insights,
        "insights_generated_at": insights_generated_at,
    })


# ---------------------------------------------------------------------------
# Hunger logging (Part 1)
# ---------------------------------------------------------------------------

def hunger_log_api(request):
    """POST — log a HungerCheck. Returns JSON {ok, id, avg_morning}."""
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
    from django.http import JsonResponse
    try:
        level = int(request.POST.get("hunger_level", 0))
        if not (1 <= level <= 10):
            return JsonResponse({"ok": False, "error": "hunger_level must be 1–10"}, status=400)
        context = request.POST.get("context", "random")
        fullness = request.POST.get("fullness_level", "")
        notes = request.POST.get("notes", "").strip()
        meal_pk = request.POST.get("related_meal_id", "")

        today = datetime.date.today()
        hc = HungerCheck.objects.create(
            date=today,
            context=context,
            hunger_level=level,
            fullness_level=int(fullness) if fullness else None,
            related_meal_id=int(meal_pk) if meal_pk else None,
            notes=notes,
        )

        # 7-day avg morning hunger for the widget summary line
        week_ago = today - datetime.timedelta(days=6)
        morning_checks = HungerCheck.objects.filter(
            date__gte=week_ago, date__lte=today, context="morning"
        )
        avg_morning = None
        if morning_checks.exists():
            avg_morning = round(
                sum(c.hunger_level for c in morning_checks) / morning_checks.count(), 1
            )

        return JsonResponse({"ok": True, "id": hc.pk, "avg_morning": avg_morning})
    except Exception as e:
        logger.warning("hunger_log_api error: %s", e)
        return JsonResponse({"ok": False, "error": str(e)}, status=400)


# ---------------------------------------------------------------------------
# Symptoms page (Part 2)
# ---------------------------------------------------------------------------

def symptoms_page(request):
    """GET — symptom log form + recent entries. POST — log a new symptom."""
    from django.http import JsonResponse

    if request.method == "POST":
        try:
            symptom = request.POST.get("symptom", "").strip()
            severity = int(request.POST.get("severity", 0))
            notes = request.POST.get("notes", "").strip()
            meal_pk = request.POST.get("related_meal_id", "")
            iv_pk = request.POST.get("related_intervention_id", "")

            if not symptom or severity not in (1, 2, 3):
                return JsonResponse({"ok": False, "error": "symptom and severity required"}, status=400)

            today = datetime.date.today()
            SideEffectLog.objects.create(
                date=today,
                symptom=symptom,
                severity=severity,
                notes=notes,
                related_meal_id=int(meal_pk) if meal_pk else None,
                related_intervention_id=int(iv_pk) if iv_pk else None,
            )
            return JsonResponse({"ok": True})
        except Exception as e:
            logger.warning("symptoms_page POST error: %s", e)
            return JsonResponse({"ok": False, "error": str(e)}, status=400)

    # GET — show page
    cutoff_30 = datetime.date.today() - datetime.timedelta(days=29)
    recent = list(SideEffectLog.objects.filter(date__gte=cutoff_30).order_by("-timestamp")[:50])

    # Summary: counts by symptom last 30 days
    from django.db.models import Count as _Count
    summary = (
        SideEffectLog.objects.filter(date__gte=cutoff_30)
        .values("symptom")
        .annotate(count=_Count("id"), avg_sev=Avg("severity"))
        .order_by("-count")
    )

    # Recent meals for the dropdown (last 24 hours)
    day_ago = datetime.datetime.now() - datetime.timedelta(hours=24)
    recent_meals = FoodEntry.objects.filter(logged_at__gte=day_ago).order_by("-logged_at")[:10]

    active_interventions = Intervention.objects.filter(
        Q(end_date__isnull=True) | Q(end_date__gte=datetime.date.today())
    ).order_by("name")

    return render(request, "workouts/symptoms.html", {
        "recent": recent,
        "summary": summary,
        "recent_meals": recent_meals,
        "active_interventions": active_interventions,
        "symptom_choices": SideEffectLog.SYMPTOM_CHOICES,
    })


# ---------------------------------------------------------------------------
# Pattern insights page (Part 3)
# ---------------------------------------------------------------------------

def insights_page(request):
    """GET — display Sonnet pattern insights. On load, generate if cache expired."""
    from .ai import _get_or_generate_pattern_insights
    settings = UserSettings.get()

    # Auto-generate on page load if missing or >7 days old
    insights = None
    generated_at = None
    try:
        insights = _get_or_generate_pattern_insights(force=False)
        generated_at = settings.ai_pattern_insights_generated_at
    except Exception as e:
        logger.warning("insights_page: pattern generation failed: %s", e)

    return render(request, "workouts/insights.html", {
        "insights": insights,
        "generated_at": generated_at,
    })


# ---------------------------------------------------------------------------
# Target accept/decline (Part 4)
# ---------------------------------------------------------------------------

def target_accept_api(request):
    """POST — accept a suggested calorie adjustment and record it."""
    if request.method != "POST":
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["POST"])
    from django.http import JsonResponse
    try:
        new_cal = int(request.POST.get("new_calories", 0))
        reason = request.POST.get("reason", "").strip()
        if not new_cal:
            return JsonResponse({"ok": False, "error": "new_calories required"}, status=400)

        profile = NutritionProfile.get()
        previous = profile.manual_calories or 0

        profile.manual_calories = new_cal
        profile.save(update_fields=["manual_calories", "updated_at"])

        TargetAdjustment.objects.create(
            previous_calories=previous,
            new_calories=new_cal,
            reason=reason,
            auto_suggested=True,
            accepted_by_user=True,
        )
        return JsonResponse({"ok": True, "new_calories": new_cal})
    except Exception as e:
        logger.warning("target_accept_api error: %s", e)
        return JsonResponse({"ok": False, "error": str(e)}, status=400)


def today_page(request):
    """Today page — single-glance morning check-in."""
    from django.db.models import Sum
    from .nutrition import compute_macro_targets
    from .ai import _get_or_generate_day_analysis

    today_date = datetime.date.today()
    yesterday = today_date - datetime.timedelta(days=1)

    # Wellness — try today, fall back to yesterday if today hasn't synced yet
    daily = DailyStats.objects.filter(date=today_date).first()
    wellness_is_yesterday = False
    if not daily or not (daily.hrv_last_night or daily.body_battery_start):
        fallback = DailyStats.objects.filter(date=yesterday).first()
        if fallback and (fallback.hrv_last_night or fallback.body_battery_start):
            daily = fallback
            wellness_is_yesterday = True

    # Today's workouts
    todays_workouts = list(
        CachedWorkout.objects.filter(created_at__date=today_date).order_by("-created_at")
    )

    # Nutrition progress
    nutrition_targets = None
    nutrition_progress = None
    try:
        nutrition_targets = compute_macro_targets()
    except Exception:
        pass

    today_food = FoodEntry.objects.filter(date=today_date).aggregate(
        cal=Sum("calories"),
        protein=Sum("protein_g"),
        carbs=Sum("carbs_g"),
        fat=Sum("fat_g"),
        fiber=Sum("fiber_g"),
    )
    if nutrition_targets:
        nutrition_progress = {
            "cal":     {"now": today_food["cal"] or 0,     "target": nutrition_targets.get("calories")},
            "protein": {"now": today_food["protein"] or 0, "target": nutrition_targets.get("protein_g")},
            "carbs":   {"now": today_food["carbs"] or 0,   "target": nutrition_targets.get("carbs_g")},
            "fat":     {"now": today_food["fat"] or 0,     "target": nutrition_targets.get("fat_g")},
            "fiber":   {"now": today_food["fiber"] or 0,   "target": nutrition_targets.get("fiber_g")},
        }
        for v in nutrition_progress.values():
            t = v["target"]
            v["pct"] = min(round((v["now"] / t) * 100), 100) if t else 0

    # Active interventions
    active_interventions = [i for i in Intervention.objects.all() if i.is_active]

    # AI: day analysis (requires DailyStats + workouts) + next workout rec
    day_analysis_text = None
    next_workout_text = None
    today_stats = DailyStats.objects.filter(date=today_date).first()
    if today_stats:
        try:
            day_analysis_text = _get_or_generate_day_analysis(today_date, todays_workouts, today_stats)
        except Exception:
            pass
        next_workout_text = today_stats.ai_next_workout or None

    return render(request, "workouts/today.html", {
        "today_date": today_date,
        "daily": daily,
        "wellness_is_yesterday": wellness_is_yesterday,
        "todays_workouts": todays_workouts,
        "nutrition_progress": nutrition_progress,
        "nutrition_targets": nutrition_targets,
        "active_interventions": active_interventions,
        "next_workout_text": next_workout_text,
        "day_analysis_text": day_analysis_text,
    })


def weekly_review_page(request):
    """GET — show the weekly review for the most recently completed week, plus archive."""
    from .models import WeeklyReview
    from .ai import _get_or_generate_weekly_review

    # Most recently completed Mon–Sun week
    today = datetime.date.today()
    days_since_monday = today.weekday()  # Mon=0
    if days_since_monday == 0:
        # Today is Monday — show the week that just ended (last week)
        last_monday = today - datetime.timedelta(days=7)
    else:
        last_monday = today - datetime.timedelta(days=days_since_monday + 7)

    force = request.GET.get("refresh") == "1"
    current_review = _get_or_generate_weekly_review(last_monday, force=force)

    archive = list(WeeklyReview.objects.exclude(week_start=last_monday).order_by("-week_start")[:12])

    return render(request, "workouts/review.html", {
        "current_review": current_review,
        "archive": archive,
        "last_monday": last_monday,
    })
