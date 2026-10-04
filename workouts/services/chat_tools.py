"""
Aggregation tools for the stats chat sidebar.

Every function here takes plain arguments (dates, strings, ints) and returns a
JSON-serializable dict of pre-aggregated numbers — never raw querysets or model
instances. The chat agent (workouts/ai.py: run_stats_chat) calls these as
Anthropic tools; keeping the aggregation in Python (not left to the model)
bounds both token cost and hallucination risk on multi-day data.
"""

import datetime
from difflib import get_close_matches
from functools import partial

from django.db.models import Avg, Count, Min, Max

from workouts.analysis import run_intervention_analysis
from workouts.models import CachedWorkout, DailyStats, Intervention, SideEffectLog, HungerCheck
from workouts.nutrition import compute_macro_targets

DETAIL_MAX_DAYS = 14

ALLOWED_METRICS = {
    "sleep_score", "sleep_seconds", "hrv_last_night", "hrv_weekly_avg",
    "hrv_status", "resting_hr", "stress_avg", "training_readiness_score",
    "body_battery_charge", "body_battery_high", "body_battery_low",
    "steps", "active_calories", "vo2_max_running", "vo2_max_cycling",
    "weight_lb", "fat_ratio_pct", "muscle_mass_lb",
    "cal_total", "protein_g_total", "carbs_g_total", "fat_g_total",
}

STRING_METRICS = {"hrv_status"}


def _parse_date(d):
    if isinstance(d, datetime.date):
        return d
    return datetime.date.fromisoformat(str(d))


def get_daily_stats_summary(user, start_date, end_date, metrics, detail=False):
    """Aggregate DailyStats fields over a date range."""
    start_date = _parse_date(start_date)
    end_date = _parse_date(end_date)

    known = [m for m in metrics if m in ALLOWED_METRICS]
    skipped = [m for m in metrics if m not in ALLOWED_METRICS]

    qs = DailyStats.objects.for_user(user).filter(date__gte=start_date, date__lte=end_date).order_by("date")
    days_with_data = qs.exclude(**{f"{known[0]}__isnull": True}).count() if known else 0

    metrics_out = {}
    for field in known:
        if field in STRING_METRICS:
            values = list(qs.exclude(**{f"{field}__isnull": True}).values_list(field, flat=True))
            counts = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            metrics_out[field] = counts
            continue

        vals = list(qs.exclude(**{f"{field}__isnull": True}).values_list(field, flat=True))
        if not vals:
            metrics_out[field] = {"avg": None, "min": None, "max": None, "n": 0}
            continue
        metrics_out[field] = {
            "avg": round(sum(vals) / len(vals), 2),
            "min": round(min(vals), 2) if isinstance(min(vals), float) else min(vals),
            "max": round(max(vals), 2) if isinstance(max(vals), float) else max(vals),
            "n": len(vals),
        }

    result = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days_with_data": days_with_data,
        "metrics": metrics_out,
    }
    if skipped:
        result["skipped_metrics"] = skipped

    range_days = (end_date - start_date).days + 1
    if detail and range_days <= DETAIL_MAX_DAYS and known:
        daily = []
        for row in qs.values("date", *known):
            row["date"] = row["date"].isoformat()
            daily.append(row)
        result["daily"] = daily
    elif detail:
        result["detail_available"] = False
        result["detail_unavailable_reason"] = (
            f"range is {range_days} days, detail is capped at {DETAIL_MAX_DAYS} days"
            if range_days > DETAIL_MAX_DAYS else "no valid metrics requested"
        )

    return result


def get_workout_summary(user, start_date, end_date, discipline="all"):
    """Aggregate CachedWorkout over a date range, grouped by discipline."""
    start_date = _parse_date(start_date)
    end_date = _parse_date(end_date)

    qs = CachedWorkout.objects.for_user(user).filter(
        created_at__date__gte=start_date, created_at__date__lte=end_date
    )
    if discipline != "all":
        qs = qs.filter(discipline=discipline)

    disciplines = qs.values_list("discipline", flat=True).distinct()

    by_discipline = {}
    for disc in disciplines:
        if not disc:
            continue
        disc_workouts = list(qs.filter(discipline=disc))
        efforts = [w.effort_points for w in disc_workouts if w.effort_points is not None]
        hrs = [w.heart_rate_avg_best for w in disc_workouts if w.heart_rate_avg_best is not None]
        calories = [w.calories for w in disc_workouts if w.calories is not None]
        by_discipline[disc] = {
            "count": len(disc_workouts),
            "avg_effort_points": round(sum(efforts) / len(efforts), 1) if efforts else None,
            "avg_heart_rate": round(sum(hrs) / len(hrs), 1) if hrs else None,
            "total_calories": round(sum(calories)) if calories else None,
        }

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_workouts": qs.count(),
        "by_discipline": by_discipline,
    }


def get_nutrition_summary(user, start_date, end_date):
    """Nutrition adherence summary over an arbitrary date range."""
    start_date = _parse_date(start_date)
    end_date = _parse_date(end_date)

    targets = compute_macro_targets(user)

    cal_t = targets.get("calories") if targets else None
    prot_t = targets.get("protein_g") if targets else None
    fiber_t = targets.get("fiber_g") if targets else None

    qs = DailyStats.objects.for_user(user).filter(
        date__gte=start_date, date__lte=end_date, cal_total__isnull=False
    )
    today = datetime.date.today()
    complete_qs = qs.filter(date__lt=today)

    total_days = (end_date - start_date).days + 1
    days_logged = qs.count()

    def _avg(field):
        vals = list(complete_qs.exclude(**{f"{field}__isnull": True}).values_list(field, flat=True))
        return round(sum(vals) / len(vals), 1) if vals else None

    def _days_hit(field, target, pct=0.9):
        if not target:
            return None
        return complete_qs.filter(**{f"{field}__gte": target * pct}).count()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days_logged": days_logged,
        "total_days": total_days,
        "avg_cal": _avg("cal_total"),
        "avg_protein_g": _avg("protein_g_total"),
        "avg_fiber_g": _avg("fiber_g_total"),
        "days_hit_cal": _days_hit("cal_total", cal_t),
        "days_hit_protein": _days_hit("protein_g_total", prot_t),
        "days_hit_fiber": _days_hit("fiber_g_total", fiber_t, pct=0.85),
        "current_targets": {
            "calories": cal_t,
            "protein_g": prot_t,
            "carbs_g": targets.get("carbs_g") if targets else None,
            "fat_g": targets.get("fat_g") if targets else None,
            "fiber_g": fiber_t,
        } if targets else None,
    }


def get_intervention_context(user, as_of_date=None, run_before_after=False,
                              intervention_name=None, window_days=28):
    """List active interventions, or run a before/after analysis for one."""
    as_of = _parse_date(as_of_date) if as_of_date else datetime.date.today()

    if run_before_after and intervention_name:
        matches = Intervention.objects.for_user(user).filter(name__icontains=intervention_name)
        intervention = matches.first()
        if intervention is None:
            return {
                "error": "not_found",
                "available": list(Intervention.objects.for_user(user).values_list("name", flat=True)),
            }

        split_date = as_of
        if not as_of_date:
            latest_dose = intervention.dose_changes.order_by("-start_date").first()
            split_date = latest_dose.start_date if latest_dose else intervention.start_date

        before_end = split_date - datetime.timedelta(days=1)
        before_start = before_end - datetime.timedelta(days=window_days - 1)
        after_start = split_date
        after_end = split_date + datetime.timedelta(days=window_days - 1)

        analysis = run_intervention_analysis(
            user,
            before_start, before_end, after_start, after_end,
        )

        groups_out = []
        for group in analysis["groups"]:
            metrics_out = [
                {
                    "display_name": m["display_name"],
                    "unit": m["unit"],
                    "before_mean": round(m["before_mean"], 2) if m["before_mean"] is not None else None,
                    "after_mean": round(m["after_mean"], 2) if m["after_mean"] is not None else None,
                    "pct_change": round(m["pct_change"], 1) if m["pct_change"] is not None else None,
                    "improved": m["improved"],
                }
                for m in group["metrics"]
                if m["before_mean"] is not None or m["after_mean"] is not None
            ]
            if metrics_out:
                groups_out.append({"name": group["name"], "metrics": metrics_out})

        return {
            "intervention_name": intervention.name,
            "split_date": split_date.isoformat(),
            "before_start": analysis["before_start"].isoformat(),
            "before_end": analysis["before_end"].isoformat(),
            "after_start": analysis["after_start"].isoformat(),
            "after_end": analysis["after_end"].isoformat(),
            "before_n": analysis["before_n"],
            "after_n": analysis["after_n"],
            "groups": groups_out,
        }

    if run_before_after and not intervention_name:
        return {"error": "intervention_name is required when run_before_after=true"}

    active = []
    for iv in Intervention.objects.for_user(user).all():
        if iv.end_date is not None and iv.end_date < as_of:
            continue
        if iv.start_date > as_of:
            continue
        active.append({
            "name": iv.name,
            "category": iv.category,
            "current_dose": iv.current_dose.dose if iv.current_dose else None,
            "dose_summary": iv.dose_summary,
            "duration_days": (as_of - iv.start_date).days,
        })

    return {"as_of_date": as_of.isoformat(), "active_interventions": active}


def get_symptoms_and_hunger_summary(user, start_date, end_date):
    """Symptom frequency/severity and hunger-level summary over a date range."""
    start_date = _parse_date(start_date)
    end_date = _parse_date(end_date)

    symptom_qs = SideEffectLog.objects.for_user(user).filter(date__gte=start_date, date__lte=end_date)
    symptoms = {}
    for row in symptom_qs.values("symptom").annotate(count=Count("id"), avg_severity=Avg("severity")):
        symptoms[row["symptom"]] = {
            "count": row["count"],
            "avg_severity": round(row["avg_severity"], 1),
        }

    hunger_qs = HungerCheck.objects.for_user(user).filter(date__gte=start_date, date__lte=end_date)
    avg_by_context = {}
    for row in hunger_qs.values("context").annotate(avg=Avg("hunger_level")):
        avg_by_context[row["context"]] = round(row["avg"], 1)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "symptoms": symptoms,
        "hunger": {
            "avg_by_context": avg_by_context,
            "n": hunger_qs.count(),
        },
    }


CHAT_TOOLS = [
    {
        "name": "get_daily_stats_summary",
        "description": (
            "Get aggregated recovery/sleep/activity/body-comp metrics from "
            "DailyStats over a date range. Returns averages and ranges, not "
            "individual daily rows, unless detail=true on a range of 14 days "
            "or fewer. Use this for any question about trends, averages, or "
            "comparisons over time in sleep, HRV, resting HR, stress, "
            "training readiness, body battery, steps, VO2 max, weight, or "
            "body composition."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Field names, e.g. sleep_score, hrv_last_night, "
                        "resting_hr, steps, weight_lb"
                    ),
                },
                "detail": {"type": "boolean", "default": False},
            },
            "required": ["start_date", "end_date", "metrics"],
        },
    },
    {
        "name": "get_workout_summary",
        "description": (
            "Get aggregated workout counts, effort points, heart rate, and "
            "calories from CachedWorkout over a date range, grouped by "
            "discipline. Use this for questions about training volume or "
            "workout performance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "discipline": {
                    "type": "string",
                    "description": "e.g. cycling, running, strength, walking, yoga, meditation, or 'all'",
                    "default": "all",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_nutrition_summary",
        "description": (
            "Get nutrition logging adherence over a date range: average "
            "calories/protein/fiber, days hitting each target, and the "
            "currently configured targets. Use for questions about diet "
            "adherence or macro trends."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_intervention_context",
        "description": (
            "Without run_before_after: list interventions (medications, "
            "supplements, etc.) active as of a date, with current dose and "
            "duration. With run_before_after=true and intervention_name set: "
            "run a before/after comparison of recovery/sleep/stress/activity/"
            "body-comp/nutrition metrics split at the intervention's most "
            "recent dose-change date. Use this for 'did X help' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {"type": "string", "format": "date"},
                "run_before_after": {"type": "boolean", "default": False},
                "intervention_name": {"type": "string"},
                "window_days": {"type": "integer", "default": 28},
            },
        },
    },
    {
        "name": "get_symptoms_and_hunger_summary",
        "description": (
            "Get symptom frequency/severity and hunger-level averages by "
            "time-of-day context over a date range. Use for questions about "
            "side effects, GI symptoms, or hunger/satiety patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
            },
            "required": ["start_date", "end_date"],
        },
    },
]

def build_tool_dispatch(user):
    """Tool name → callable with the requesting user already bound. The model
    picks tools and arguments, never whose data they read."""
    return {
        "get_daily_stats_summary": partial(get_daily_stats_summary, user),
        "get_workout_summary": partial(get_workout_summary, user),
        "get_nutrition_summary": partial(get_nutrition_summary, user),
        "get_intervention_context": partial(get_intervention_context, user),
        "get_symptoms_and_hunger_summary": partial(get_symptoms_and_hunger_summary, user),
    }
