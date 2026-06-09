"""
AI integration for FitPulse.

Contains all Anthropic API calls, prompt construction, and caching logic for:
  - Analytics insights (batch API, Claude Sonnet, cached 7 days)
  - Day analysis (synchronous, Claude Haiku, cached 7 days)
  - Next-workout recommendation (synchronous, Claude Haiku, cached 24h)

All Anthropic calls use direct HTTP via requests — the anthropic Python package
is not installed.
"""

import json
import logging
import os
from datetime import date, timedelta

import requests
from django.db.models import Avg, Count
from django.db.models.functions import TruncWeek
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.utils import timezone as tz

from . import llm
from .models import CachedWorkout, DailyStats, UserSettings, Intervention
from .prompt_formats import HEADLINE_BULLETS_FORMAT, INTENSITY_ACTIVITY_REASON_FORMAT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _interventions_context(start_date, end_date) -> str:
    """Human-readable summary of interventions and dose changes overlapping the given date range."""
    from django.db.models import Q
    overlapping = Intervention.objects.filter(
        start_date__lte=end_date
    ).filter(
        Q(end_date__gte=start_date) | Q(end_date__isnull=True)
    ).order_by("start_date")
    if not overlapping.exists():
        return "No tracked interventions during this period."
    lines = []
    for iv in overlapping:
        end_str = iv.end_date.isoformat() if iv.end_date else "ongoing"
        lines.append(f"\n- {iv.name} [{iv.category}]: {iv.start_date} to {end_str}")
        doses = iv.dose_changes.filter(
            start_date__lte=end_date
        ).filter(
            Q(end_date__gte=start_date) | Q(end_date__isnull=True)
        ).order_by("start_date")
        for d in doses:
            d_end = d.end_date.isoformat() if d.end_date else "ongoing"
            lines.append(f"    • {d.dose} from {d.start_date} to {d_end}")
        if iv.expected_effects:
            lines.append(f"    Expected: {iv.expected_effects}")
    return "\n".join(lines)


def build_persona_block(date_range=None, *, include_interventions=True) -> str:
    """
    One paragraph about the user, built ONLY from data they entered.
    date_range: optional (start_date, end_date) tuple to scope intervention listing.
    Returns "" if no profile data and no interventions — caller's prompt should
    work fine without it.
    """
    from .models import AthleteProfile
    profile = AthleteProfile.get()

    parts = []

    # Athletic identity
    identity_bits = []
    for disc, level in [
        ("runner",  profile.running_experience),
        ("cyclist", profile.cycling_experience),
        ("lifter",  profile.strength_experience),
    ]:
        if level == "new":           identity_bits.append(f"new {disc}")
        elif level == "intermediate": identity_bits.append(f"intermediate {disc}")
        elif level == "experienced":  identity_bits.append(f"experienced {disc}")
    if identity_bits:
        parts.append("User identifies as: " + ", ".join(identity_bits) + ".")

    if profile.training_focus:
        parts.append(f"Current training focus: {profile.training_focus}")

    # Health context
    if profile.health_context_override:
        parts.append(profile.health_context_override)
    elif include_interventions:
        today = tz.localdate()
        start, end = date_range if date_range else (today, today)
        iv_ctx = _interventions_context(start, end)
        if iv_ctx and "No tracked interventions" not in iv_ctx:
            parts.append("Active interventions/medications affecting this user:\n" + iv_ctx)

    return "\n\n".join(parts)


def coaching_tone_instruction() -> str:
    """Return a short directive matching the user's tone preference."""
    from .models import AthleteProfile
    tone = AthleteProfile.get().coaching_tone
    return {
        "encouraging": "Be encouraging and constructive while staying honest about the data.",
        "direct":      "Be direct and concise. Skip pleasantries.",
        "data_only":   "Report what the data shows without commentary or recommendations.",
    }.get(tone, "")


def rehab_flag_for(title: str) -> str:
    """Return ' [PT/REHAB — not a training session]' if title matches a configured rehab keyword."""
    from .models import AthleteProfile
    keywords = AthleteProfile.get().rehab_keywords or []
    if not keywords:
        return ""
    lower = (title or "").lower()
    return " [PT/REHAB — not a training session]" if any(kw in lower for kw in keywords) else ""


def _macro_priority_hint(remaining_protein, remaining_carbs, remaining_fiber, targets):
    """Return the name of the macro with the largest remaining gap as % of its target, or None."""
    if not targets:
        return None
    candidates = {
        "protein": (remaining_protein, targets.get("protein_g")),
        "fiber":   (remaining_fiber,   targets.get("fiber_g")),
        "carbs":   (remaining_carbs,   targets.get("carbs_g")),
    }
    best_name, best_pct = None, 0.0
    for name, (remaining, target) in candidates.items():
        if target and target > 0 and remaining and remaining > 0:
            pct = remaining / target
            if pct > best_pct:
                best_pct = pct
                best_name = name
    return best_name


def _slug_peloton_avg(perf, slug):
    """Return the pre-computed average_value for a metric slug from a performance graph.
    Falls back to the mean of the sample values if average_value is absent."""
    slug_data = perf.get("metrics_by_slug", {}).get(slug, {})
    av = slug_data.get("average_value")
    if av is not None:
        return av
    vals = slug_data.get("values", [])
    valid = [v for v in vals if v is not None]
    return sum(valid) / len(valid) if valid else None


def _avg(vals, decimals=1):
    """Mean of non-None values, rounded. None if empty."""
    clean = [v for v in vals if v is not None]
    return round(sum(clean) / len(clean), decimals) if clean else None


def _halves(vals):
    """Return (first_half_avg, second_half_avg) for time-ordered values."""
    clean = [v for v in vals if v is not None]
    if not clean:
        return None, None
    h = len(clean) // 2
    first  = round(sum(clean[:h]) / h, 1) if h else None
    second = round(sum(clean[h:]) / (len(clean) - h), 1) if (len(clean) - h) else None
    return first, second


def _pace_fmt(secs):
    """Seconds-per-mile → 'M:SS/mi' string. None if falsy."""
    if not secs:
        return None
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}/mi"


def _delta(new, old, decimals=1):
    """Signed delta string ('+1.2', '-0.5', 'n/a')."""
    if new is None or old is None:
        return "n/a"
    d = new - old
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.{decimals}f}"


# ---------------------------------------------------------------------------
# Generic cache wrappers for AI text fields
# ---------------------------------------------------------------------------

def cached_settings_field(field_name, ttl_hours, generator, *, force=False, extra_save=None):
    """Read-through cache for UserSettings-backed AI text fields."""
    settings = UserSettings.get()
    cached = getattr(settings, field_name)
    stamp  = getattr(settings, f"{field_name}_generated_at")
    if not force and cached and stamp:
        if (tz.now() - stamp).total_seconds() / 3600 < ttl_hours:
            return cached
    try:
        new_text = generator()
    except Exception as e:
        logger.warning("%s generation failed: %s", field_name, e)
        return cached or ""
    setattr(settings, field_name, new_text)
    setattr(settings, f"{field_name}_generated_at", tz.now())
    fields = [field_name, f"{field_name}_generated_at"]
    if extra_save:
        for k, v in extra_save.items():
            setattr(settings, k, v)
            fields.append(k)
    settings.save(update_fields=fields)
    return new_text


def cached_daily_stats_field(stats, field_name, ttl_hours, generator, *, force=False, stamp_field=None):
    """Read-through cache for DailyStats-backed AI text fields."""
    stamp_name = stamp_field or f"{field_name}_generated_at"
    cached = getattr(stats, field_name)
    stamp  = getattr(stats, stamp_name)
    if not force and cached and stamp:
        if (tz.now() - stamp).total_seconds() / 3600 < ttl_hours:
            return cached
    try:
        new_text = generator()
    except Exception as e:
        logger.warning("%s generation failed: %s", field_name, e)
        return cached
    setattr(stats, field_name, new_text)
    setattr(stats, stamp_name, tz.now())
    stats.save(update_fields=[field_name, stamp_name])
    return new_text


# ---------------------------------------------------------------------------
# Analytics insights — batch API (Claude Sonnet)
# ---------------------------------------------------------------------------

def _build_running_section(pace_list, first_p, second_p, avg_incline, avg_dist, form_qs):
    """Build the running sub-dict for the AI insights summary."""
    if not pace_list and not form_qs:
        return None

    form_section = None
    if form_qs:
        cad_list = [w["run_cadence_avg"] for w in form_qs if w["run_cadence_avg"]]
        sl_list  = [w["stride_length_avg"] for w in form_qs if w["stride_length_avg"]]
        vo_list  = [w["vertical_oscillation_avg"] for w in form_qs if w["vertical_oscillation_avg"]]
        vr_list  = [w["vertical_ratio_avg"] for w in form_qs if w["vertical_ratio_avg"]]
        gct_list = [w["ground_contact_time_avg"] for w in form_qs if w["ground_contact_time_avg"]]
        cad_first, cad_second = _halves(cad_list)
        vo_first, vo_second   = _halves(vo_list)
        gct_first, gct_second = _halves(gct_list)
        form_section = {
            "runs_with_garmin_form_data": len(form_qs),
            "avg_cadence_spm": _avg(cad_list),
            "cadence_first_half_spm": cad_first,
            "cadence_second_half_spm": cad_second,
            "avg_stride_length_cm": _avg(sl_list),
            "avg_vertical_oscillation_cm": _avg(vo_list),
            "vertical_oscillation_first_half_cm": vo_first,
            "vertical_oscillation_second_half_cm": vo_second,
            "avg_vertical_ratio_pct": _avg(vr_list),
            "avg_ground_contact_time_ms": _avg(gct_list),
            "ground_contact_time_first_half_ms": gct_first,
            "ground_contact_time_second_half_ms": gct_second,
            "note_on_form": (
                "Running form benchmarks for context: cadence ≥165 spm is efficient; "
                "vertical oscillation ≤7.5 cm is good (less wasted bounce); "
                "vertical ratio ≤8% is efficient (oscil. vs. stride length); "
                "ground contact time ≤260 ms is good (shorter = more elastic energy return)."
            ),
        }

    return {
        "total_last_year": len(pace_list),
        "avg_pace_first_half": _pace_fmt(first_p),
        "avg_pace_second_half": _pace_fmt(second_p),
        "avg_incline_pct_last_year": avg_incline,
        "avg_distance_miles_per_run_last_year": avg_dist,
        "note_on_incline": "1% incline is the standard treadmill setting to simulate outdoor running; higher values indicate deliberate hill work",
        "form": form_section,
    } if (pace_list or form_qs) else None


def _build_insights_summary():
    """Aggregate workout stats into a compact dict for the LLM prompt."""
    import datetime
    from django.db.models import Avg, Count
    from django.utils import timezone

    now = timezone.now()
    today = now.date()
    cutoff_90d  = now - datetime.timedelta(days=90)
    cutoff_365d = now - datetime.timedelta(days=365)
    cutoff_30d  = now - datetime.timedelta(days=30)
    cutoff_7d   = now - datetime.timedelta(days=7)
    cutoff_14d  = now - datetime.timedelta(days=14)
    cutoff_28d  = now - datetime.timedelta(days=28)

    total_all  = CachedWorkout.objects.count()
    total_365d = CachedWorkout.objects.filter(created_at__gte=cutoff_365d).count()
    total_90d  = CachedWorkout.objects.filter(created_at__gte=cutoff_90d).count()

    # Discipline breakdown: count + avg duration over the last 90 days
    disc_rows = list(
        CachedWorkout.objects.filter(created_at__gte=cutoff_90d)
        .values("discipline")
        .annotate(count=Count("id"), avg_dur=Avg("duration_seconds"))
        .order_by("-count")
    )

    # Running form metrics (populated by Garmin augmentation)
    run_form_qs = list(
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_365d, discipline="running")
        .exclude(stride_length_avg__isnull=True)
        .order_by("created_at")
        .values("created_at", "run_cadence_avg", "stride_length_avg",
                "vertical_oscillation_avg", "vertical_ratio_avg", "ground_contact_time_avg")
    )

    # Heart rate and performance from performance_graph_json
    # (model fields for HR/pace/watts are null from the list API — perf graph is authoritative)
    hr_by_disc = {}
    cyc_watts_list   = []
    cyc_cadence_list = []
    run_pace_list    = []
    run_incline_list = []
    run_dist_list    = []
    perf_qs = (
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_365d, performance_graph_json__isnull=False)
        .order_by("created_at")
        .values("discipline", "created_at", "duration_seconds", "performance_graph_json")
    )
    for w in perf_qs:
        disc = w["discipline"]
        perf = w["performance_graph_json"]
        dur_h = (w["duration_seconds"] or 0) / 3600
        hr = _slug_peloton_avg(perf, "heart_rate")
        if hr:
            hr_by_disc.setdefault(disc, []).append(hr)
        if disc in ("cycling", "bike_bootcamp"):
            watts = _slug_peloton_avg(perf, "output")
            if watts:
                cyc_watts_list.append(watts)
            cad = _slug_peloton_avg(perf, "cadence")
            if cad:
                cyc_cadence_list.append(cad)
        elif disc in ("running", "outdoor_running"):
            # Only use pace from Peloton workouts — Garmin directPace is not in decimal min/mi
            if perf.get("source") != "garmin":
                pace_min = _slug_peloton_avg(perf, "pace")
                if pace_min:
                    run_pace_list.append(round(pace_min * 60))  # decimal min/mi → seconds
                incline = _slug_peloton_avg(perf, "incline")
                if incline is not None:
                    run_incline_list.append(incline)
            speed = _slug_peloton_avg(perf, "speed")  # mph (valid for both sources)
            if speed and dur_h:
                run_dist_list.append(speed * dur_h)

    avg_hr_by_disc = {
        disc: round(sum(vals) / len(vals), 1)
        for disc, vals in hr_by_disc.items()
    }

    cyc_first_w, cyc_second_w     = _halves(cyc_watts_list)
    cyc_first_cad, cyc_second_cad = _halves(cyc_cadence_list)
    run_first_p, run_second_p     = _halves(run_pace_list)

    ftp = UserSettings.get().ftp
    avg_pct_ftp = (
        round(sum(cyc_watts_list) / len(cyc_watts_list) / ftp * 100)
        if cyc_watts_list and ftp else None
    )

    run_avg_incline = round(sum(run_incline_list) / len(run_incline_list), 1) if run_incline_list else None
    run_avg_dist    = round(sum(run_dist_list) / len(run_dist_list), 2) if run_dist_list else None

    # Strength — combines Peloton movement tracker + Garmin exercise sets
    str_qs = list(
        CachedWorkout.objects
        .filter(created_at__gte=cutoff_365d, discipline="strength")
        .values("created_at", "source", "exercise_sets_json", "movements", "movement_summary")
        .order_by("created_at")
    )
    strength_data = None
    if str_qs:
        cutoff_60d = now - datetime.timedelta(days=60)
        # name → {reps: [], weight_lbs: [], recent_weight_lbs: [], session_dates: set(), last_date: date}
        ex_stats = {}
        peloton_session_count = 0
        garmin_session_count = 0

        for row in str_qs:
            row_date = row["created_at"].date()
            is_recent = row["created_at"] >= cutoff_60d
            for s in (row["exercise_sets_json"] or []):
                name = s.get("exercise")
                if not name:
                    continue
                ex_stats.setdefault(name, {"reps": [], "weight_lbs": [], "recent_weight_lbs": [], "session_dates": set(), "last_date": row_date})
                if s.get("reps") is not None:
                    ex_stats[name]["reps"].append(s["reps"])
                if s.get("weight_kg"):
                    lbs = s["weight_kg"] * 2.20462
                    ex_stats[name]["weight_lbs"].append(lbs)
                    if is_recent:
                        ex_stats[name]["recent_weight_lbs"].append(lbs)
                ex_stats[name]["session_dates"].add(row_date)
                if row_date > ex_stats[name]["last_date"]:
                    ex_stats[name]["last_date"] = row_date
            for m in (row["movements"] or []):
                name = m.get("name")
                if not name:
                    continue
                ex_stats.setdefault(name, {"reps": [], "weight_lbs": [], "recent_weight_lbs": [], "session_dates": set(), "last_date": row_date})
                if m.get("reps_done") is not None:
                    ex_stats[name]["reps"].append(m["reps_done"])
                if m.get("weight_lbs"):
                    lbs = m["weight_lbs"]
                    ex_stats[name]["weight_lbs"].append(lbs)
                    if is_recent:
                        ex_stats[name]["recent_weight_lbs"].append(lbs)
                ex_stats[name]["session_dates"].add(row_date)
                if row_date > ex_stats[name]["last_date"]:
                    ex_stats[name]["last_date"] = row_date

            if row["source"] == "garmin" and row["exercise_sets_json"]:
                garmin_session_count += 1
            elif row["source"] == "peloton" and row["movements"]:
                peloton_session_count += 1

        # Sort by most recently active, so discontinued exercises fall to the bottom
        top_ex = sorted(ex_stats.items(), key=lambda x: x[1]["last_date"], reverse=True)[:8]
        strength_data = {
            "total_sessions_last_year": len(str_qs),
            "peloton_sessions_with_movement_data": peloton_session_count,
            "garmin_sessions_with_exercise_data": garmin_session_count,
            "top_exercises": {},
        }
        for name, stats in top_ex:
            entry = {
                "sessions": len(stats["session_dates"]),
                "last_seen": stats["last_date"].isoformat(),
            }
            if stats["reps"]:
                entry["avg_reps"] = round(sum(stats["reps"]) / len(stats["reps"]), 1)
            # Prefer recent (60-day) weight average; fall back to all-time if no recent data
            weight_source = stats["recent_weight_lbs"] or stats["weight_lbs"]
            if weight_source:
                entry["avg_weight_lbs"] = round(sum(weight_source) / len(weight_source), 1)
                if stats["recent_weight_lbs"]:
                    entry["weight_based_on"] = "last 60 days"
            strength_data["top_exercises"][name] = entry

        # Weight progression: first vs second half of the year for the top weighted exercise
        weighted_ex = [(n, s) for n, s in top_ex if s["weight_lbs"]]
        if weighted_ex:
            top_wt_name, _ = weighted_ex[0]
            cutoff_half = now - datetime.timedelta(days=182)
            first_wts, second_wts = [], []
            for row in str_qs:
                ts = row["created_at"]
                for s in (row["exercise_sets_json"] or []):
                    if s.get("exercise") == top_wt_name and s.get("weight_kg"):
                        (first_wts if ts < cutoff_half else second_wts).append(s["weight_kg"] * 2.20462)
                for m in (row["movements"] or []):
                    if m.get("name") == top_wt_name and m.get("weight_lbs"):
                        (first_wts if ts < cutoff_half else second_wts).append(m["weight_lbs"])
            if first_wts and second_wts:
                strength_data["weight_progression"] = {
                    "exercise": top_wt_name,
                    "avg_lbs_first_half_year": round(sum(first_wts) / len(first_wts), 1),
                    "avg_lbs_second_half_year": round(sum(second_wts) / len(second_wts), 1),
                }

        peloton_vols = [
            row["movement_summary"].get("total_volume")
            for row in str_qs
            if row["source"] == "peloton" and (row["movement_summary"] or {}).get("total_volume")
        ]
        if peloton_vols:
            strength_data["peloton_avg_volume_lbs_per_session"] = round(
                sum(peloton_vols) / len(peloton_vols)
            )

    # Active training days: multiple workouts on one day = one training session
    all_timestamps_90d = list(
        CachedWorkout.objects.filter(created_at__gte=cutoff_90d).values_list("created_at", flat=True)
    )
    all_timestamps_30d = list(
        CachedWorkout.objects.filter(created_at__gte=cutoff_30d).values_list("created_at", flat=True)
    )
    active_days_90d = len(set(dt.date() for dt in all_timestamps_90d))
    active_days_30d = len(set(dt.date() for dt in all_timestamps_30d))

    sorted_active_days_90d = sorted(set(dt.date() for dt in all_timestamps_90d))
    if len(sorted_active_days_90d) > 1:
        day_gaps = [
            (sorted_active_days_90d[i + 1] - sorted_active_days_90d[i]).days
            for i in range(len(sorted_active_days_90d) - 1)
        ]
        avg_session_gap = round(sum(day_gaps) / len(day_gaps), 1)
    else:
        avg_session_gap = None

    disc_mix_90d = {
        row["discipline"]: {
            "count": row["count"],
            "avg_duration_minutes": round(row["avg_dur"] / 60, 1) if row["avg_dur"] else None,
            "avg_heart_rate_bpm": avg_hr_by_disc.get(row["discipline"]),
        }
        for row in disc_rows
    }

    disc_rows_30d = list(
        CachedWorkout.objects.filter(created_at__gte=cutoff_30d)
        .values("discipline")
        .annotate(count=Count("id"), avg_dur=Avg("duration_seconds"))
        .order_by("-count")
    )
    disc_mix_30d = {
        row["discipline"]: {
            "count": row["count"],
            "avg_duration_minutes": round(row["avg_dur"] / 60, 1) if row["avg_dur"] else None,
        }
        for row in disc_rows_30d
    }

    # Last 7d vs prior 7d workout comparison
    ts_7d    = list(CachedWorkout.objects.filter(created_at__gte=cutoff_7d).values("created_at", "discipline"))
    ts_prior = list(CachedWorkout.objects.filter(created_at__gte=cutoff_14d, created_at__lt=cutoff_7d).values("created_at", "discipline"))
    active_days_7d    = len(set(r["created_at"].date() for r in ts_7d))
    active_days_prior = len(set(r["created_at"].date() for r in ts_prior))

    def _disc_counts(rows):
        counts = {}
        for r in rows:
            counts[r["discipline"]] = counts.get(r["discipline"], 0) + 1
        return counts

    week_comparison = {
        "this_week_training_days": active_days_7d,
        "prior_week_training_days": active_days_prior,
        "this_week_by_discipline": _disc_counts(ts_7d),
        "prior_week_by_discipline": _disc_counts(ts_prior),
    }

    # Wellness: last 14 days vs prior 14 days (DailyStats)
    def _wellness_avgs(stats_rows, fields):
        result = {}
        for f in fields:
            vals = [getattr(r, f) for r in stats_rows if getattr(r, f) is not None]
            if vals:
                result[f"avg_{f}"] = round(sum(vals) / len(vals), 1)
        return result

    wellness_fields = [
        "hrv_last_night", "sleep_score", "resting_hr",
        "training_readiness_score", "body_battery_start", "training_load",
    ]
    recent_stats = list(
        DailyStats.objects.filter(date__gte=today - datetime.timedelta(days=14), synced_at__isnull=False)
        .order_by("date")
    )
    prior_stats = list(
        DailyStats.objects.filter(
            date__gte=today - datetime.timedelta(days=28),
            date__lt=today - datetime.timedelta(days=14),
            synced_at__isnull=False,
        ).order_by("date")
    )

    recent_wellness = _wellness_avgs(recent_stats, wellness_fields)
    prior_wellness  = _wellness_avgs(prior_stats, wellness_fields)

    # Training load trajectory: last 7 days vs prior 7 days
    load_7d    = [r.training_load for r in recent_stats if r.date >= today - datetime.timedelta(days=7) and r.training_load]
    load_prior = [r.training_load for r in prior_stats  if r.date >= today - datetime.timedelta(days=21) and r.date < today - datetime.timedelta(days=7) and r.training_load]
    training_status_recent = next(
        (r.training_status for r in reversed(recent_stats) if r.training_status), None
    )

    wellness_trend = {}
    if recent_wellness:
        wellness_trend["last_14_days"] = recent_wellness
    if prior_wellness:
        wellness_trend["prior_14_days"] = prior_wellness
    if load_7d:
        wellness_trend["avg_training_load_last_7d"] = round(sum(load_7d) / len(load_7d), 1)
    if load_prior:
        wellness_trend["avg_training_load_prior_7d"] = round(sum(load_prior) / len(load_prior), 1)
    if training_status_recent:
        wellness_trend["current_training_status"] = training_status_recent
    wellness_trend["note"] = (
        "training_readiness_score: 0–100 (≥70 = ready, 40–69 = moderate, <40 = poor). "
        "hrv_last_night in ms — higher is better recovery. "
        "resting_hr in bpm — lower is better recovery. "
        "training_load is Garmin acute load — higher means more recent training stress. "
        "Compare last_14_days vs prior_14_days to detect whether recovery is improving or declining."
    )

    # Nutrition adherence: last 30 days
    nutrition_stats = list(
        DailyStats.objects.filter(date__gte=today - datetime.timedelta(days=30))
        .values("date", "cal_total", "protein_g_total", "fiber_g_total")
        .order_by("date")
    )
    from workouts.nutrition import compute_macro_targets
    try:
        targets = compute_macro_targets()
        cal_target     = targets.get("calories")
        protein_target = targets.get("protein_g")
        fiber_target   = targets.get("fiber_g")
    except Exception:
        cal_target = protein_target = fiber_target = None

    logged_days = [r for r in nutrition_stats if r["cal_total"]]
    nutrition_summary = {"days_logged_last_30d": len(logged_days)}
    if logged_days:
        avg_cal     = sum(r["cal_total"] for r in logged_days) / len(logged_days)
        avg_protein = sum(r["protein_g_total"] for r in logged_days) / len(logged_days)
        avg_fiber   = sum(r["fiber_g_total"] or 0 for r in logged_days) / len(logged_days)
        nutrition_summary["avg_calories_per_logged_day"] = round(avg_cal)
        nutrition_summary["avg_protein_g_per_logged_day"] = round(avg_protein, 1)
        nutrition_summary["avg_fiber_g_per_logged_day"] = round(avg_fiber, 1)
        if cal_target:
            nutrition_summary["calorie_target"] = cal_target
            nutrition_summary["days_hit_calorie_target"] = sum(
                1 for r in logged_days if r["cal_total"] and r["cal_total"] >= cal_target * 0.9
            )
        if protein_target:
            nutrition_summary["protein_target_g"] = protein_target
            nutrition_summary["days_hit_protein_target"] = sum(
                1 for r in logged_days if r["protein_g_total"] and r["protein_g_total"] >= protein_target * 0.9
            )
        if fiber_target:
            nutrition_summary["fiber_target_g"] = fiber_target
            nutrition_summary["days_hit_fiber_target"] = sum(
                1 for r in logged_days if r["fiber_g_total"] and r["fiber_g_total"] >= fiber_target * 0.85
            )

    return {
        "total_workouts_ever": total_all,
        "total_workouts_last_year": total_365d,
        "note_on_workout_counts": (
            "Multiple workouts logged on the same calendar day represent a single training "
            "session (e.g. a run + cool-down walk + post-run stretch). Use active_training_days "
            "and training_days_per_week for frequency and rest assessment, not raw workout counts."
        ),
        "active_training_days_last_30d": active_days_30d,
        "active_training_days_last_90d": active_days_90d,
        "training_days_per_week_last_4_weeks": round(active_days_30d / 4.3, 1),
        "training_days_per_week_last_13_weeks": round(active_days_90d / 13, 1),
        "rest_days_per_week_last_13_weeks": round((90 - active_days_90d) / 13, 1),
        "avg_days_between_training_sessions_last_90d": avg_session_gap,
        "avg_activities_per_training_day_last_90d": round(total_90d / active_days_90d, 1) if active_days_90d else None,
        "discipline_mix_last_30_days": disc_mix_30d,
        "discipline_mix_last_90_days": disc_mix_90d,
        "week_over_week": week_comparison,
        "recovery_and_wellness": wellness_trend if wellness_trend else None,
        "nutrition_last_30_days": nutrition_summary if logged_days else None,
        "cycling": {
            "total_last_year": len(cyc_watts_list),
            "ftp_current": ftp,
            "avg_watts_first_half_year": cyc_first_w,
            "avg_watts_second_half_year": cyc_second_w,
            "avg_pct_ftp_last_year": avg_pct_ftp,
            "avg_cadence_first_half_year": round(cyc_first_cad, 1) if cyc_first_cad else None,
            "avg_cadence_second_half_year": round(cyc_second_cad, 1) if cyc_second_cad else None,
            "note_on_pct_ftp": "avg_pct_ftp is average watts as % of FTP; zone 2 endurance is ~56-75%, threshold is ~91-105%",
        } if cyc_watts_list else None,
        "running": _build_running_section(
            run_pace_list, run_first_p, run_second_p,
            run_avg_incline, run_avg_dist, run_form_qs,
        ),
        "strength": strength_data,
    }


# System prompt and user prompt suffix for the insights batch job.
def build_insights_system() -> str:
    """Build the analytics batch system prompt, personalised from AthleteProfile."""
    from .models import AthleteProfile
    profile = AthleteProfile.get()

    parts = [
        "You are a fitness coach analyzing workout data for a Peloton and Garmin Connect user. "
        "Provide specific, data-driven insights. Be encouraging and practical. "
        "KEY CONTEXT: Multiple workouts logged on the same day are stacked activities within one "
        "training session (e.g. run + cool-down walk + post-run stretch). ALWAYS use "
        "active_training_days and training_days_per_week — never raw workout counts or "
        "avg_days_between_training_sessions — to assess frequency, rest, or overtraining risk. "
        "Use avg_duration_minutes to distinguish effort: sessions ≤10 min are cooldowns or recovery. "
        "Use avg_heart_rate_bpm when available to gauge intensity. "
        "For running, use avg_incline_pct alongside pace — a 13:30/mi pace at 3% incline reflects "
        "much harder effort than the same pace on flat ground; 1% is standard treadmill baseline. "
        "Use avg_distance_miles_per_run to assess training load and long-run development. "
        "When running.form is present, analyze the Garmin running form metrics. "
    ]

    if profile.running_experience == "new":
        parts.append(
            "The user is early in their running journey — be encouraging, explain what each metric means simply, "
            "and give one or two specific, actionable tips (e.g. 'focus on quick, light steps' for high GCT; "
            "'think about running tall' for high vertical oscillation; "
            "'try to land with your foot under your hip' for low cadence). "
            "Compare first-half vs second-half trends in cadence, vertical oscillation, and ground contact time "
            "to detect form improvement or fatigue over the season. "
            "Do not overwhelm with all metrics at once — pick the 1-2 most actionable form cues. "
        )
    elif profile.running_experience:
        parts.append(
            "When analyzing running form, compare first-half vs second-half trends in cadence, "
            "vertical oscillation, and ground contact time to detect improvement or fatigue. "
            "Reference the benchmarks in the data and highlight the 1-2 most actionable cues. "
        )

    parts.append(
        "For cycling, use avg_pct_ftp to determine training zone: <75% is endurance, 76-90% is tempo, "
        "91-105% is threshold; comment on whether training intensity matches stated goals. "
        "Cadence trend (first vs second half year) shows pedaling efficiency development. "
        "For strength: data comes from two sources — Peloton's movement tracker "
        "(peloton_sessions_with_movement_data, peloton_avg_volume_lbs_per_session) and Garmin's "
        "exercise tracking (garmin_sessions_with_exercise_data). top_exercises merges both sources "
        "and shows each exercise's session count, avg_reps per set, and avg_weight_lbs. "
        "weight_progression compares the avg weight for the most frequent weighted exercise between "
        "the first and second half of the year — an increase signals strength progression. "
    )

    rehab_kws = profile.rehab_keywords or []
    if rehab_kws:
        kw_str = ", ".join(f"'{k}'" for k in rehab_kws)
        parts.append(
            f"Exercises matching these keywords ({kw_str}) indicate physical therapy or rehab work — "
            "acknowledge the rehab context and focus on consistency and progressive overload rather than "
            "volume maximization. "
        )
    else:
        parts.append(
            "If exercise names suggest physical therapy or rehab work (e.g. rotator cuff movements, "
            "mobility-only sessions), acknowledge the rehab context and focus on consistency rather than volume. "
        )

    parts.append(
        "IMPORTANT: Always compare discipline_mix_last_30_days against discipline_mix_last_90_days "
        "to detect recent behavioral changes before making recommendations. If a discipline has "
        "increased in the last 30 days, acknowledge that momentum instead of suggesting they start. "
        "Use week_over_week to detect the most recent 7-day shift — a jump or drop in training days "
        "or a discipline swap this week is the freshest signal available. "
        "When recovery_and_wellness is present, connect training load to recovery signals: "
        "if training_load is rising while hrv_last_night is falling or training_readiness_score is "
        "declining, flag the imbalance. If recovery metrics are stable or improving alongside "
        "consistent training, call that out as a positive sign. "
        "current_training_status (e.g. 'maintaining', 'productive', 'overreaching') is Garmin's "
        "own assessment — use it as supporting context. "
        "When nutrition_last_30_days is present, connect fueling to training: low protein adherence "
        "on weeks with high strength volume is worth flagging; consistent calorie logging alongside "
        "training momentum is a positive habit worth reinforcing. "
        "Respond using ## section headers with paragraph text — no bullet points, no intro paragraph."
    )

    tone = coaching_tone_instruction()
    if tone:
        parts.append(tone)

    return " ".join(parts)

INSIGHTS_PROMPT_SUFFIX = (
    "\n\nPlease analyze this data and provide specific, actionable insights about "
    "my fitness trends, consistency, and progress. Be encouraging but honest. "
    "Before making any recommendation, check whether discipline_mix_last_30_days already "
    "shows the user doing it — if so, recognize the recent effort rather than suggesting "
    "they start. Focus on what the data actually shows: recent momentum, trends vs. "
    "prior months, recovery signals relative to training load, nutrition fueling relative "
    "to training demands, and areas that are genuinely still underdeveloped.\n\n"
    "Write the analysis using 3-5 ## section headers based on what's most relevant in the data "
    "(e.g. ## Consistency, ## Running, ## Strength, ## Recovery, ## What to Focus On). "
    "Under each header write 2-3 sentences of flowing paragraph text — no bullet points. "
    "Use **bold** for emphasis on specific numbers or key observations. No intro paragraph."
)


def _submit_insights_batch():
    """Submit a new Anthropic batch for insights. Returns the batch ID."""
    summary = _build_insights_summary()
    prompt = (
        "Here is my workout data from Peloton and Garmin Connect for the past year:\n\n"
        + json.dumps(summary, indent=2)
        + INSIGHTS_PROMPT_SUFFIX
    )
    return llm.submit_batch("peloton-insights", prompt, model=llm.SONNET, max_tokens=1024, system=build_insights_system())


def analytics_generate_insights(request):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return render_insights_partial(request, {
            "error": "ANTHROPIC_API_KEY is not set. Add it to your .env file and restart the server."
        })
    try:
        batch_id = _submit_insights_batch()
    except Exception as e:
        return render_insights_partial(request, {"error": f"Failed to submit batch: {e}"})
    settings_obj = UserSettings.get()
    settings_obj.ai_insights_batch_id = batch_id
    settings_obj.save(update_fields=["ai_insights_batch_id"])
    return render_insights_partial(request, {"pending": True})


def analytics_check_insights(request):
    """Poll the Anthropic Batch API for insight results. Called via HTMX every 30s."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    settings_obj = UserSettings.get()
    batch_id = settings_obj.ai_insights_batch_id

    if not batch_id:
        return render_insights_partial(request, {
            "insights": settings_obj.ai_insights,
            "generated_at": settings_obj.ai_insights_generated_at,
        })

    if not api_key:
        settings_obj.ai_insights_batch_id = None
        settings_obj.save(update_fields=["ai_insights_batch_id"])
        return render_insights_partial(request, {"error": "ANTHROPIC_API_KEY is not set."})

    # Check batch status — treat any transient error (including 429) as still-pending
    try:
        batch = llm.get_batch_status(batch_id)
    except Exception:
        return render_insights_partial(request, {"pending": True})

    if batch.get("processing_status") != "ended":
        return render_insights_partial(request, {"pending": True})

    # Batch complete — fetch results; treat 429 as transient (stay pending)
    try:
        insights_text = None
        for row in llm.get_batch_results(batch_id):
            if row.get("result", {}).get("type") == "succeeded":
                insights_text = row["result"]["message"]["content"][0]["text"]
                break
    except Exception as e:
        logger.warning("batch results fetch failed for %s: %s", batch_id, e)
        return render_insights_partial(request, {"pending": True})

    if not insights_text:
        settings_obj.ai_insights_batch_id = None
        settings_obj.save(update_fields=["ai_insights_batch_id"])
        return render_insights_partial(request, {
            "error": "Batch completed but no successful result found."
        })

    settings_obj.ai_insights = insights_text
    settings_obj.ai_insights_generated_at = tz.now()
    settings_obj.ai_insights_batch_id = None
    settings_obj.save(update_fields=["ai_insights", "ai_insights_generated_at", "ai_insights_batch_id"])

    return render_insights_partial(request, {
        "insights": insights_text,
        "generated_at": settings_obj.ai_insights_generated_at,
    })


def render_insights_partial(request, context):
    """Thin wrapper so the insights endpoints don't need to import render."""
    from django.shortcuts import render
    return render(request, "workouts/partials/insights.html", context)


# ---------------------------------------------------------------------------
# Day analysis — synchronous (Claude Haiku, cached 7 days)
# ---------------------------------------------------------------------------

def _get_or_generate_day_analysis(day, workouts, stats):
    """Return a cached day analysis or generate a fresh one synchronously."""
    if not workouts:
        return None

    today_local = tz.localdate()
    is_today = (day == today_local)
    local_now = tz.localtime(tz.now())

    # Don't generate for today before 8 PM — nutrition and workouts are still in flux
    if is_today and local_now.hour < 20:
        return None

    cache_hours = 4 if is_today else 168  # 4h cache for today, 7 days for past

    def _gen():
        workout_lines = []
        for w in workouts:
            parts = [f"- {w.title} ({w.discipline}, {w.duration_minutes} min)"]
            if w.heart_rate_avg_best:
                parts.append(f"avg HR {w.heart_rate_avg_best:.0f} bpm")
            effort = w.effort_points or w.average_effort_score
            if effort:
                parts.append(f"effort {effort:.1f}")
            if w.avg_pace_display:
                parts.append(f"pace {w.avg_pace_display}")
            if w.output_watts:
                parts.append(f"output {w.output_watts:.0f}W")
            workout_lines.append(", ".join(parts))

        wellness_parts = []
        if stats.training_readiness_score:
            wellness_parts.append(f"Training readiness: {stats.training_readiness_score}/100 ({stats.training_readiness_label})")
        if stats.hrv_last_night:
            wellness_parts.append(f"HRV last night: {stats.hrv_last_night:.0f} ms ({stats.hrv_status_display})")
        if stats.resting_hr:
            wellness_parts.append(f"Resting HR: {stats.resting_hr} bpm")
        if stats.sleep_score:
            wellness_parts.append(f"Sleep score: {stats.sleep_score}")
        if stats.sleep_minutes:
            wellness_parts.append(f"Sleep: {stats.sleep_minutes // 60}h {stats.sleep_minutes % 60}m")
        if stats.body_battery_start is not None:
            if stats.body_battery_end is not None:
                computed_drain = stats.body_battery_start - stats.body_battery_end
                drain_str = f", drained {computed_drain}" if computed_drain > 0 else ""
                wellness_parts.append(f"Body battery: {stats.body_battery_start} → {stats.body_battery_end}{drain_str}")
            elif not is_today:
                wellness_parts.append(f"Body battery: started at {stats.body_battery_start}")
            else:
                # Today: end-of-day value recorded after sleep — omit from prompt to avoid
                # the AI commenting on missing data
                wellness_parts.append(f"Body battery: wakeup {stats.body_battery_start}")
        if stats.training_status:
            wellness_parts.append(f"Training status: {stats.training_status}")
        if stats.training_load:
            wellness_parts.append(f"Acute training load: {stats.training_load:.0f}")
        if any([stats.load_focus_anaerobic, stats.load_focus_high_aerobic, stats.load_focus_low_aerobic]):
            wellness_parts.append(
                f"Load focus — anaerobic: {stats.load_focus_anaerobic or 0:.0f}, "
                f"high aerobic: {stats.load_focus_high_aerobic or 0:.0f}, "
                f"low aerobic: {stats.load_focus_low_aerobic or 0:.0f}"
            )

        # Nutrition context for this day
        nutrition_parts = []
        if stats.cal_total is not None:
            from .nutrition import compute_macro_targets
            from .models import NutritionProfile, FoodEntry
            try:
                profile = NutritionProfile.objects.filter(pk=1).first()
                targets = compute_macro_targets(profile) if profile else None
                cal_t = targets.get("calories") if targets else None
                prot_t = targets.get("protein_g") if targets else None
                fiber_t = targets.get("fiber_g") if targets else None
                if stats.cal_total:
                    cal_str = f"{stats.cal_total:.0f}"
                    if cal_t:
                        cal_str += f" (target {cal_t})"
                    nutrition_parts.append(f"Calories: {cal_str}")
                if stats.protein_g_total is not None:
                    prot_str = f"{stats.protein_g_total:.0f}g"
                    if prot_t:
                        prot_str += f" (target {prot_t}g)"
                    nutrition_parts.append(f"Protein: {prot_str}")
                if stats.fiber_g_total is not None:
                    fiber_str = f"{stats.fiber_g_total:.1f}g"
                    if fiber_t:
                        fiber_str += f" (target {fiber_t}g)"
                    nutrition_parts.append(f"Fiber: {fiber_str}")
                # Brief meal summary
                meals = list(FoodEntry.objects.filter(date=day).values_list("meal", "raw_text").order_by("logged_at"))
                if meals:
                    meal_strs = [f"{m or 'log'}: {t[:40]}" for m, t in meals[:4]]
                    nutrition_parts.append("Meals: " + "; ".join(meal_strs))
            except Exception:
                pass

        nutrition_section = ""
        if nutrition_parts:
            nutrition_section = "\n\nNUTRITION FOR THIS DAY\n" + "\n".join(nutrition_parts)

        intervention_context = _interventions_context(day, day)
        intervention_section = ""
        if intervention_context and "No tracked interventions" not in intervention_context:
            intervention_section = f"\n\nACTIVE INTERVENTIONS\n{intervention_context}"

        today_note = " Do not comment on missing body battery end-of-day value — it is only recorded after sleep and is not available for the current day." if is_today else ""
        persona = build_persona_block(date_range=(day, day))
        persona_section = f"\n\nABOUT THIS PERSON\n{persona}" if persona else ""
        prompt = f"""Date: {day.strftime('%A, %B %-d, %Y')}

RECOVERY & READINESS
{chr(10).join(wellness_parts) if wellness_parts else 'No Garmin wellness data available.'}{nutrition_section}{intervention_section}

WORKOUTS
{chr(10).join(workout_lines)}{persona_section}

Analyze how this person performed given their recovery state. {HEADLINE_BULLETS_FORMAT}
Metrics to cite: pace, HR, effort score, body battery, HRV, nutrition. If nutrition data is present, note connections like "pre-workout protein was strong" or "low carb day may have affected energy". If intervention data is present, note relevant context — e.g. if a supplement was just started, acknowledge it's day 1 and effects won't be immediate; if a medication dose changed recently, note that.{today_note}"""

        return llm.call(prompt, model=llm.HAIKU, max_tokens=300)

    return cached_daily_stats_field(
        stats, "ai_day_analysis", cache_hours, _gen,
        stamp_field="ai_day_generated_at",
    )


# ---------------------------------------------------------------------------
# Next-workout recommendation — synchronous (Claude Haiku, cached 24h)
# ---------------------------------------------------------------------------

def next_workout_refresh(request):
    """Clear the cached next-workout recommendation and regenerate it immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    today_stats, _ = DailyStats.objects.get_or_create(date=date.today())
    today_stats.ai_next_workout = None
    today_stats.ai_next_workout_generated_at = None
    today_stats.save(update_fields=["ai_next_workout", "ai_next_workout_generated_at"])
    _get_or_generate_next_workout(today_stats)
    return redirect("calendar")


def _get_or_generate_next_workout(today_stats):
    """Return a cached next-workout recommendation or generate a fresh one."""
    def _gen():
        today_local = tz.localdate()
        cutoff = today_local - timedelta(days=14)
        recent_workouts = list(
            CachedWorkout.objects.filter(created_at__date__gte=cutoff)
            .order_by("-created_at")
        )

        recent_stats = list(
            DailyStats.objects.filter(date__gte=today_local - timedelta(days=7))
            .order_by("-date")
        )

        workout_lines = []
        for w in recent_workouts:
            # Use local date so workouts appear on the day the user actually did them
            local_date = tz.localtime(w.created_at).date()
            day_label = local_date.strftime("%A %Y-%m-%d")
            effort = w.effort_points
            hr = f", HR {w.heart_rate_avg_best:.0f}" if w.heart_rate_avg_best else ""
            eff = f", effort {effort:.0f}" if effort else ""
            dur = (w.duration_seconds or 0) // 60
            title = w.title or w.discipline

            # Flag PT/rehab sessions so the AI doesn't treat them as training load
            pt_flag = rehab_flag_for(title)
            is_pt = bool(pt_flag)

            # Muscles: bucket 3 = high, 2 = moderate; skip bucket 1 (light)
            pg = w.performance_graph_json or {}
            muscles = pg.get("muscle_groups") or []
            high = [m["display_name"] for m in muscles if m.get("bucket") == 3]
            mod  = [m["display_name"] for m in muscles if m.get("bucket") == 2]
            muscle_parts = []
            if high:
                muscle_parts.append("high: " + ", ".join(high))
            if mod:
                muscle_parts.append("mod: " + ", ".join(mod))
            muscle_str = "; muscles " + " | ".join(muscle_parts) if muscle_parts else ""

            # For strength, also list the exercise names
            movements = w.movements or []
            move_str = ""
            if movements and not is_pt:
                move_str = "; exercises: " + ", ".join(m["name"] for m in movements if m.get("name"))

            workout_lines.append(
                f"- {day_label} {title} ({w.discipline}, {dur}min{hr}{eff}{muscle_str}{move_str}){pt_flag}"
            )

        stat_lines = []
        for s in recent_stats:
            parts = [str(s.date)]
            if s.training_readiness_score:
                parts.append(f"readiness {s.training_readiness_score}")
            if s.hrv_status:
                parts.append(f"HRV {s.hrv_status}")
            if s.training_status:
                parts.append(f"status {s.training_status}")
            if s.body_battery_low is not None:
                parts.append(f"BB low {s.body_battery_low}")
            stat_lines.append(" | ".join(parts))

        # Use local date for all today-vs-not comparisons
        today_workouts = [
            w for w in recent_workouts
            if tz.localtime(w.created_at).date() == today_local
        ]
        target_day = "tomorrow" if today_workouts else "today"

        today_rec = next((s for s in recent_stats if s.date == today_local), None)
        today_context = ""
        if today_rec:
            parts = []
            if today_rec.training_readiness_score:
                parts.append(f"Training readiness today: {today_rec.training_readiness_score}/100 ({today_rec.training_readiness_label})")
            if today_rec.hrv_last_night:
                parts.append(f"HRV last night: {today_rec.hrv_last_night:.0f} ms ({today_rec.hrv_status_display})")
            if today_rec.sleep_score:
                parts.append(f"Sleep score: {today_rec.sleep_score}")
            if today_rec.training_load:
                parts.append(f"Acute training load: {today_rec.training_load:.0f}")
            if today_rec.training_status:
                parts.append(f"Training status: {today_rec.training_status}")
            if any([today_rec.load_focus_anaerobic, today_rec.load_focus_high_aerobic, today_rec.load_focus_low_aerobic]):
                parts.append(
                    f"Load focus — anaerobic: {today_rec.load_focus_anaerobic or 0:.0f}, "
                    f"high aerobic: {today_rec.load_focus_high_aerobic or 0:.0f}, "
                    f"low aerobic: {today_rec.load_focus_low_aerobic or 0:.0f}"
                )
            today_context = "\n".join(parts)

        today_workout_note = ""
        if today_workouts:
            descs = []
            for w in today_workouts:
                dur = (w.duration_seconds or 0) // 60
                descs.append(f"{w.title or w.discipline} ({w.discipline}, {dur} min)")
            today_workout_note = (
                f"Already completed today ({today_local.strftime('%A, %B %-d')}): "
                + "; ".join(descs)
                + ". Recommendation is for tomorrow."
            )

        persona = build_persona_block()
        prompt = f"""Today is {date.today().strftime('%A, %B %-d, %Y')}.
{today_workout_note}

TODAY'S RECOVERY SIGNALS
{today_context or 'No data available yet.'}

LAST 14 DAYS OF WORKOUTS
{chr(10).join(workout_lines) if workout_lines else 'No recent workouts.'}

DAILY WELLNESS TREND (last 7 days)
{chr(10).join(stat_lines) if stat_lines else 'No wellness data.'}

Based on this data, give a next-workout recommendation for {target_day}. {INTENSITY_ACTIVITY_REASON_FORMAT}

CARDIO GUIDANCE: When recommending cardio, use these rules:
- Running: good when readiness ≥70 and no heavy posterior chain (glutes/hamstrings/quads) fatigue from recent strength work
- Cycling (Peloton): good when legs are moderately fatigued but cardio fitness is the goal; lower impact than running
- Walking/hiking: best on low-readiness days (readiness <55) or active recovery; still builds aerobic base
- High-intensity intervals (any modality): only when readiness ≥75 and ≥2 days since last hard effort

STRENGTH GUIDANCE: When recommending strength:
- Note which muscle groups have been hit hard recently and steer toward undertrained areas
- Upper body / push / pull: good when lower body is fatigued from running or leg-focused strength
- Lower body / legs: needs ≥48h since last heavy leg session (glutes/hamstrings/quads at bucket 3)
- Core / mobility / PT: always appropriate as a complement
- IMPORTANT: Any session tagged [PT/REHAB] is physical therapy, not a training session. Do NOT count it toward fatigue or recovery time for any muscle group.
{f"{chr(10)}{persona}" if persona else ""}"""

        return llm.call(prompt, model=llm.HAIKU, max_tokens=350)

    return cached_daily_stats_field(today_stats, "ai_next_workout", 24, _gen)


# ---------------------------------------------------------------------------
# Compare page analysis
# ---------------------------------------------------------------------------

def compare_analysis(request):
    """HTMX endpoint — returns an HTML snippet comparing 2–4 workouts."""
    ids = [i.strip() for i in request.GET.get("ids", "").split(",") if i.strip()][:4]
    workouts = list(CachedWorkout.objects.filter(workout_id__in=ids).order_by("created_at"))
    if len(workouts) < 2:
        return _render_compare_analysis_html(None)

    def _stat(w):
        from collections import defaultdict
        source_label = "Garmin" if w.source == "garmin" else "Peloton"
        lines = [f"{w.title} ({w.created_at.strftime('%b %-d, %Y')}, {w.discipline}, {w.duration_minutes} min, source: {source_label})"]

        # General stats
        if w.output_watts:
            lines.append(f"  output: {w.output_watts/1000:.0f} kJ")
        if w.avg_pace_display:
            lines.append(f"  avg pace: {w.avg_pace_display}")
        if w.avg_speed_mph:
            lines.append(f"  avg speed: {w.avg_speed_mph:.1f} mph")
        if w.max_speed_mph:
            lines.append(f"  max speed: {w.max_speed_mph:.1f} mph")
        if w.distance_miles:
            lines.append(f"  distance: {w.distance_miles:.2f} mi")
        elevation = w.elevation_gain
        avg_incline = w.avg_incline
        max_incline = None
        # Fall back to performance graph when flat fields are null
        pg_incline = (w.performance_graph_json or {}).get("metrics_by_slug", {}).get("incline", {})
        if isinstance(pg_incline, dict):
            if avg_incline is None and pg_incline.get("average_value") is not None:
                avg_incline = pg_incline["average_value"]
            if pg_incline.get("max_value") is not None:
                max_incline = pg_incline["max_value"]
        if elevation:
            lines.append(f"  elevation gain: {elevation:.0f} ft")
        if avg_incline is not None:
            max_str = f", max {max_incline:.1f}%" if max_incline is not None else ""
            lines.append(f"  avg incline: {avg_incline:.1f}%{max_str}")
        if w.heart_rate_avg_best:
            lines.append(f"  avg HR: {w.heart_rate_avg_best:.0f} bpm")
        if w.heart_rate_max:
            lines.append(f"  max HR: {w.heart_rate_max:.0f} bpm")
        ep = w.effort_points
        if ep:
            lines.append(f"  effort pts: {ep:.0f}")
        if w.calories:
            lines.append(f"  calories: {w.calories:.0f}")
        if w.avg_cadence:
            lines.append(f"  cadence: {w.avg_cadence:.0f} rpm")
        if w.avg_watts:
            lines.append(f"  avg power: {w.avg_watts:.0f} W")

        # Running form (Garmin-augmented)
        if w.run_cadence_avg:
            lines.append(f"  run cadence: {w.run_cadence_avg:.0f} spm")
        if w.stride_length_avg:
            lines.append(f"  stride length: {w.stride_length_avg:.1f} cm")
        if w.vertical_oscillation_avg:
            lines.append(f"  vert oscillation: {w.vertical_oscillation_avg:.1f} cm")
        if w.vertical_ratio_avg:
            lines.append(f"  vert ratio: {w.vertical_ratio_avg:.1f}%")
        if w.ground_contact_time_avg:
            lines.append(f"  ground contact: {w.ground_contact_time_avg:.0f} ms")

        # Strength-specific
        if w.discipline == "strength":
            if w.movement_tracker_tier:
                lines.append(f"  movement tier: {w.movement_tracker_tier}")
            ms = w.movement_summary or {}
            if ms.get("total_volume"):
                lines.append(f"  total volume: {ms['total_volume']:.0f} lb")
            if ms.get("completion_percentage") is not None:
                lines.append(f"  class completion: {ms['completion_percentage']:.0f}%")
            if ms.get("num_targets_reached") is not None:
                lines.append(f"  targets hit: {ms['num_targets_reached']}")

            sets = w.exercise_sets_json or []
            if sets:
                by_exercise = defaultdict(list)
                for s in sets:
                    name = s.get("exercise") or s.get("exercise_key") or "Unknown"
                    by_exercise[name].append(s)
                lines.append("  exercises:")
                for name, ex_sets in by_exercise.items():
                    rep_sets = [s for s in ex_sets if s.get("reps") is not None]
                    timed_sets = [s for s in ex_sets if s.get("reps") is None and s.get("duration_seconds")]
                    ex_parts = []
                    if rep_sets:
                        avg_reps = round(sum(s["reps"] for s in rep_sets) / len(rep_sets))
                        wt_kg = rep_sets[0].get("weight_kg")
                        wt_str = f" @ {wt_kg * 2.20462:.0f} lb" if wt_kg else ""
                        ex_parts.append(f"{len(rep_sets)} sets × {avg_reps} reps{wt_str}")
                    if timed_sets:
                        avg_secs = round(sum(s["duration_seconds"] for s in timed_sets) / len(timed_sets))
                        ex_parts.append(f"{len(timed_sets)} sets × {avg_secs}s")
                    lines.append(f"    {name}: {', '.join(ex_parts)}")

        return "\n".join(lines)

    workout_blocks = "\n\n".join(f"WORKOUT {i+1}:\n{_stat(w)}" for i, w in enumerate(workouts))
    persona = build_persona_block()
    persona_rule = f"\n4. {persona}" if persona else ""

    prompt = f"""You are analyzing {len(workouts)} Peloton and/or Garmin workouts being compared side by side.

{workout_blocks}

Provide a brief, insightful comparison. {HEADLINE_BULLETS_FORMAT}

Rules:
1. INCLINE/ELEVATION FIRST — MANDATORY: Before drawing any conclusion about HR, efficiency, or fatigue, check whether avg incline or max incline differs between runs. A higher avg incline directly raises HR and slows pace — this is physics, not fitness decline. If inclines differ, the first bullet MUST address this and quantify the impact. Do NOT attribute HR differences to fatigue or mechanics if incline explains it.
2. Keep each bullet to one to two sentences and reference actual numbers.
3. Focus on what's interesting or actionable — effort vs output tradeoffs, HR efficiency, pacing strategy, incline-adjusted performance, cross-discipline comparisons.{persona_rule}"""

    try:
        text = llm.call(prompt, model=llm.HAIKU, max_tokens=450)
        return _render_compare_analysis_html(text, ids_param=request.GET.get("ids", ""))
    except Exception as e:
        logger.warning("Compare analysis failed: %s", e)
        return _render_compare_analysis_html(None, ids_param=request.GET.get("ids", ""))


def _render_compare_analysis_html(text, ids_param=""):
    from django.http import HttpResponse
    regen = (
        f'<button class="btn btn-ghost" style="font-size:0.75rem;padding:0.25rem 0.6rem;margin-top:1rem"'
        f' hx-get="/api/compare/analysis/?ids={ids_param}"'
        f' hx-target="#compare-ai-body" hx-swap="innerHTML"'
        f' hx-indicator="#compare-ai-spinner">Regenerate</button>'
        f'<span id="compare-ai-spinner" class="cai-spinner htmx-indicator" style="margin-left:0.75rem;vertical-align:middle"></span>'
    )
    if not text:
        return HttpResponse(f'<p style="color:var(--text-dim);font-size:0.85rem">Analysis unavailable.</p>{regen}')

    headline = ""
    bullets = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("HEADLINE:"):
            headline = line[len("HEADLINE:"):].strip()
        elif line.startswith("•"):
            bullets.append(line[1:].strip())

    headline_html = f'<div class="cai-headline">{headline}</div>' if headline else ""
    bullets_html = "".join(f'<li class="insights-item">{b}</li>' for b in bullets)
    list_html = f'<ul class="insights-list">{bullets_html}</ul>' if bullets_html else ""
    return HttpResponse(f'{headline_html}{list_html}{regen}')


# ---------------------------------------------------------------------------
# Body commentary — synchronous (Claude Haiku, cached 24h)
# ---------------------------------------------------------------------------

def _get_or_generate_body_commentary(force=False) -> str:
    """Daily Haiku commentary on body composition trends. Cached 24h in UserSettings."""
    def _gen():
        from datetime import date as date_cls
        today = date_cls.today()
        cutoff_30d = today - timedelta(days=30)
        cutoff_7d  = today - timedelta(days=7)

        stats_30d = list(
            DailyStats.objects.filter(date__gte=cutoff_30d, date__lte=today)
            .order_by("date")
        )
        stats_7d = [s for s in stats_30d if s.date >= cutoff_7d]

        # Current (latest) body comp
        latest_weight = next((s.weight_lb for s in reversed(stats_30d) if s.weight_lb), None)
        latest_fat_pct = next((s.fat_ratio_pct for s in reversed(stats_30d) if s.fat_ratio_pct), None)
        latest_fat_lb  = next((s.fat_mass_lb for s in reversed(stats_30d) if s.fat_mass_lb), None)
        latest_lean_lb = next((s.fat_free_mass_lb for s in reversed(stats_30d) if s.fat_free_mass_lb), None)

        # 7-day ago values
        week_ago_stats = [s for s in stats_30d if s.date <= cutoff_7d]
        wk_weight = next((s.weight_lb for s in reversed(week_ago_stats) if s.weight_lb), None)
        wk_fat_lb  = next((s.fat_mass_lb for s in reversed(week_ago_stats) if s.fat_mass_lb), None)
        wk_lean_lb = next((s.fat_free_mass_lb for s in reversed(week_ago_stats) if s.fat_free_mass_lb), None)

        # 30-day ago values (oldest in window)
        month_ago_stats = [s for s in stats_30d if s.weight_lb]
        mo_weight = month_ago_stats[0].weight_lb if month_ago_stats else None
        mo_fat_lb  = next((s.fat_mass_lb for s in stats_30d if s.fat_mass_lb), None)
        mo_lean_lb = next((s.fat_free_mass_lb for s in stats_30d if s.fat_free_mass_lb), None)

        # Weight series
        weight_series = [
            f"{s.date}: {s.weight_lb:.1f}lb"
            for s in stats_30d if s.weight_lb
        ]

        # 7-day recovery averages
        avg_hrv = _avg([s.hrv_last_night for s in stats_7d])
        avg_rhr  = _avg([s.resting_hr for s in stats_7d])
        avg_sleep = _avg([s.sleep_score for s in stats_7d])
        avg_bb    = _avg([s.body_battery_high for s in stats_7d])

        # Interventions context
        iv_ctx = _interventions_context(cutoff_30d, today)

        # Nutrition 7-day context
        nutrition_section = ""
        try:
            from .nutrition import compute_macro_targets
            from .models import NutritionProfile
            profile = NutritionProfile.objects.filter(pk=1).first()
            targets = compute_macro_targets(profile) if profile else None
            nutr_7d = [s for s in stats_7d if s.cal_total is not None]
            if len(nutr_7d) >= 3:
                n_days = len(nutr_7d)
                avg_n_cal = round(sum(s.cal_total for s in nutr_7d) / n_days)
                avg_n_prot = round(sum(s.protein_g_total or 0 for s in nutr_7d) / n_days)
                avg_n_fiber = round(sum(s.fiber_g_total or 0 for s in nutr_7d) / n_days, 1)
                cal_t = targets.get("calories") if targets else None
                prot_t = targets.get("protein_g") if targets else None
                fiber_t = targets.get("fiber_g") if targets else None
                nutrition_section = f"""

NUTRITION (7-day averages, {n_days}/7 days logged)
Calories: {avg_n_cal}{f' (target {cal_t})' if cal_t else ''}
Protein: {avg_n_prot}g{f' (target {prot_t}g)' if prot_t else ''}
Fiber: {avg_n_fiber}g{f' (target {fiber_t}g)' if fiber_t else ''}"""
        except Exception:
            pass

        prompt = f"""Today: {today.strftime('%B %-d, %Y')}

CURRENT BODY COMPOSITION
Weight: {f'{latest_weight:.1f}lb' if latest_weight else 'n/a'}
Fat %: {f'{latest_fat_pct:.1f}%' if latest_fat_pct else 'n/a'}
Fat mass: {f'{latest_fat_lb:.1f}lb' if latest_fat_lb else 'n/a'}
Lean mass: {f'{latest_lean_lb:.1f}lb' if latest_lean_lb else 'n/a'}

7-DAY CHANGES
Weight: {_delta(latest_weight, wk_weight)}lb | Fat mass: {_delta(latest_fat_lb, wk_fat_lb)}lb | Lean mass: {_delta(latest_lean_lb, wk_lean_lb)}lb

30-DAY CHANGES
Weight: {_delta(latest_weight, mo_weight)}lb | Fat mass: {_delta(latest_fat_lb, mo_fat_lb)}lb | Lean mass: {_delta(latest_lean_lb, mo_lean_lb)}lb

LAST 30 DAYS WEIGHT (daily, skip nulls)
{chr(10).join(weight_series) if weight_series else 'No data.'}{nutrition_section}

7-DAY RECOVERY AVERAGES
HRV: {avg_hrv or 'n/a'} ms | Resting HR: {avg_rhr or 'n/a'} bpm | Sleep score: {avg_sleep or 'n/a'} | Body battery high: {avg_bb or 'n/a'}

ACTIVE INTERVENTIONS
{iv_ctx}

Write the commentary in exactly this structure. Write each section as 2-3 sentences of flowing paragraph text — no bullet points. Use **bold** for emphasis on specific numbers or key observations.

## Body Composition
2-3 sentences on weight and fat/lean mass changes over 7 and 30 days. Reference actual numbers.

## Recovery
2-3 sentences on HRV, resting HR, sleep score, and body battery trends. Note any concerning or encouraging signals.

## To Watch
1-2 sentence on the most important thing to keep an eye on. Note if any interventions likely explain observed patterns. If nutrition data is available, connect intake to body composition changes."""

        return llm.call(prompt, model=llm.HAIKU, max_tokens=400)

    return cached_settings_field("ai_body_commentary", 24, _gen, force=force)


def body_commentary_refresh(request):
    """POST /api/body/commentary/refresh/ — force-regenerate body commentary."""
    from django.http import JsonResponse
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    _get_or_generate_body_commentary(force=True)
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Nutrition — food parsing + meal suggestions (Claude Haiku)
# ---------------------------------------------------------------------------

def _lookup_branded_nutrition(query: str) -> list[dict]:
    """
    Search USDA FoodData Central for branded foods matching the query.
    Returns up to 3 results with label-accurate nutrition per serving.
    Uses USDA_API_KEY from env, falls back to DEMO_KEY (30 req/hr).
    """
    api_key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
    try:
        resp = requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"query": query, "api_key": api_key, "dataType": "Branded", "pageSize": 5},
            timeout=5,
        )
        resp.raise_for_status()
        foods = resp.json().get("foods", [])
        results = []
        for food in foods:
            nutrients = {n["nutrientName"]: n["value"] for n in food.get("foodNutrients", [])}
            cal = nutrients.get("Energy") or nutrients.get("Energy (Atwater General Factors)")
            if not cal:
                continue
            serving = food.get("servingSize")
            serving_unit = (food.get("servingSizeUnit") or "").lower()
            serving_str = f"{serving:.0f} {serving_unit}".strip() if serving else "1 serving"
            results.append({
                "name": food.get("description", ""),
                "brand": food.get("brandOwner") or food.get("brandName", ""),
                "serving": serving_str,
                "calories": round(cal),
                "protein_g": round(nutrients.get("Protein", 0)),
                "carbs_g": round(nutrients.get("Carbohydrate, by difference", 0)),
                "fat_g": round(nutrients.get("Total lipid (fat)", 0)),
                "fiber_g": round(nutrients.get("Fiber, total dietary", 0)),
            })
            if len(results) >= 3:
                break
        return results
    except Exception as e:
        logger.debug("USDA branded lookup failed: %s", e)
        return []


_MEAL_KIT_BRANDS = frozenset({
    "home chef", "hellofresh", "hello fresh", "green chef", "everyplate", "every plate",
    "marley spoon", "sunbasket", "sun basket", "purple carrot", "dinnerly", "factor",
    "factor 75", "gobble", "freshly", "blue apron", "plated",
})

def _detect_meal_kit(text: str) -> str | None:
    """Return the matched meal kit brand name (title-cased) or None."""
    lower = text.lower()
    for brand in _MEAL_KIT_BRANDS:
        if brand in lower:
            return brand.title()
    return None


def _homechef_slugs(raw_text: str) -> list[str]:
    """Generate slug candidates from a meal description containing 'Home Chef'."""
    import re
    name = re.sub(r"home\s+chef\s*", "", raw_text, flags=re.IGNORECASE).strip()
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    if not words:
        return []
    candidates = []
    # Natural order
    candidates.append("-".join(words))
    # Try rotating the first word to different positions (handles common reorderings)
    for i in range(1, min(len(words), 3)):
        rotated = words[i:] + words[:i]
        candidates.append("-".join(rotated))
    return list(dict.fromkeys(candidates))  # deduplicate, preserve order


def _fetch_homechef_nutrition(raw_text: str) -> dict | None:
    """
    Fetch live nutrition data from homechef.com for the named meal.
    Tries a few slug variations; returns dict with nutrition fields or None.
    """
    from bs4 import BeautifulSoup
    import re

    for slug in _homechef_slugs(raw_text):
        url = f"https://www.homechef.com/meals/{slug}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            nutrition_div = soup.find("div", class_="meal__nutrition")
            if not nutrition_div:
                continue

            nutrients = {}
            for li in nutrition_div.find_all("li"):
                parts = li.get_text(separator="|", strip=True).split("|")
                if len(parts) >= 2:
                    label = parts[0].strip().lower()
                    num = re.sub(r"[^\d.]", "", parts[1])
                    if num:
                        nutrients[label] = float(num)

            if "calories" not in nutrients:
                continue

            h1 = soup.find("h1")
            meal_title = h1.get_text(strip=True).split(" with ")[0] if h1 else slug.replace("-", " ").title()

            return {
                "name": meal_title,
                "url": url,
                "calories": round(nutrients.get("calories", 0)),
                "protein_g": round(nutrients.get("protein", 0)),
                "carbs_g": round(nutrients.get("carbohydrates", 0)),
                "fat_g": round(nutrients.get("fat", 0)),
                "fiber_g": round(nutrients.get("fiber", 0)),
            }
        except Exception as e:
            logger.debug("Home Chef fetch failed for %s: %s", url, e)
            continue

    return None


def _fetch_meal_kit_nutrition(raw_text: str, brand: str) -> dict | None:
    """Dispatch to brand-specific live-fetch. Returns nutrition dict or None."""
    if "home chef" in brand.lower():
        return _fetch_homechef_nutrition(raw_text)
    return None


def _lookup_open_food_facts(query: str) -> list[dict]:
    """
    Search Open Food Facts for branded/packaged products.
    Free, no auth required. Returns up to 3 results with per-serving nutrition.
    """
    try:
        resp = requests.get(
            "https://world.openfoodfacts.org/cgi/search.pl",
            params={
                "search_terms": query,
                "json": 1,
                "page_size": 5,
                "fields": "product_name,brands,nutriments,serving_size",
            },
            timeout=5,
        )
        resp.raise_for_status()
        results = []
        for p in resp.json().get("products", []):
            n = p.get("nutriments", {})
            # Prefer per-serving values; fall back to per-100g
            cal = n.get("energy-kcal_serving") or n.get("energy-kcal_100g")
            if not cal:
                continue
            results.append({
                "name": p.get("product_name", ""),
                "brand": p.get("brands", ""),
                "serving": p.get("serving_size") or "1 serving",
                "calories": round(cal),
                "protein_g": round(n.get("proteins_serving") or n.get("proteins_100g") or 0),
                "carbs_g": round(n.get("carbohydrates_serving") or n.get("carbohydrates_100g") or 0),
                "fat_g": round(n.get("fat_serving") or n.get("fat_100g") or 0),
                "fiber_g": round(n.get("fiber_serving") or n.get("fiber_100g") or 0),
            })
            if len(results) >= 3:
                break
        return results
    except Exception as e:
        logger.debug("Open Food Facts lookup failed: %s", e)
        return []


def _match_saved_meals(raw_text: str, saved_meals: list) -> list:
    """Return saved meals whose name shares ≥2 significant words with the input."""
    words = {w.lower() for w in raw_text.split() if len(w) > 3}
    matches = []
    for sm in saved_meals:
        sm_words = {w.lower() for w in sm["name"].split() if len(w) > 3}
        if len(words & sm_words) >= 2:
            matches.append(sm)
    return matches[:5]


def parse_food_text(
    raw_text: str,
    meal: str = "",
    saved_meals: list | None = None,
    image_b64: str | None = None,
    image_media_type: str = "image/jpeg",
    serving_note: str = "",
) -> dict:
    """
    Parse freeform food description into structured nutrition data.
    saved_meals: list of dicts with name/calories/protein_g/carbs_g/fat_g/fiber_g
    image_b64: base64-encoded nutrition label image (optional)
    serving_note: user's quantity qualifier, e.g. "I had the whole bag" or "half"
    Returns {"ok": True, "items": [...], "meal_guess": ..., "confidence": ..., "note": ...}
    or {"ok": False, "error": ..., "items": []}
    """
    meal_context = meal or "unspecified"

    json_schema = """{
  "items": [
    {"name": "scrambled eggs", "quantity": "2 large", "calories": 180, "protein_g": 12, "carbs_g": 2, "fat_g": 14, "fiber_g": 0}
  ],
  "meal_guess": "breakfast",
  "confidence": "high",
  "note": "optional one-line note about assumptions made"
}"""

    if image_b64:
        # ── Label image path ──────────────────────────────────────────────
        serving_line = f'\nUSER QUANTITY NOTE: "{serving_note}"' if serving_note else ""
        extra_text = f'\nADDITIONAL CONTEXT FROM USER: "{raw_text}"' if raw_text.strip() else ""

        prompt = f"""You are a nutrition label parser. Extract nutrition data from the label in this image and return it as structured JSON.

LABEL PARSING RULES:
1. COLUMN PRIORITY: If the label has multiple columns (e.g. "as packaged" vs "as prepared", "unpopped" vs "popped", "dry" vs "cooked"), always use the "as prepared" or "ready-to-eat" column.
2. SERVING SIZE LOGIC:
   - Default to the serving size printed on the label (1 serving).
   - "I had all of this" / "the whole thing/bag/package/container" → multiply all values by the number of servings per container.
   - "half" → multiply by 0.5. "two servings" → multiply by 2. Fractional descriptions (e.g. "about a third") → apply that multiplier.
   - If the user specifies a weight or volume that differs from the label serving, scale proportionally.
3. SPECIAL NOTATIONS: Interpret any %, DV, added sugars, trans fat asterisks, and ingredient callouts naturally.
4. AMBIGUITY: If part of the label is cut off or unclear, extract what you can and set confidence to "low" or "medium" with a note explaining what was unclear.{serving_line}{extra_text}
MEAL CONTEXT: {meal_context}

Respond with ONLY valid JSON, no markdown fences:
{json_schema}

Additional rules:
- Use the label values directly — do not substitute estimates from training knowledge when the label is readable.
- Round all numbers to whole integers.
- If a nutrient is not listed on the label, use 0.
- Set confidence "high" if the label is clear and fully visible, "medium" if partially visible, "low" if very unclear."""

        message_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": image_b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
    else:
        # ── Text description path (existing logic) ─────────────────────────
        meal_kit_brand = _detect_meal_kit(raw_text)
        if meal_kit_brand:
            live = _fetch_meal_kit_nutrition(raw_text, meal_kit_brand)
            if live:
                db_block = (
                    f"VERIFIED NUTRITION FROM {meal_kit_brand.upper()} WEBSITE ({live['url']}):\n"
                    f"  • {live['name']}: {live['calories']} kcal, "
                    f"{live['protein_g']}g protein, {live['carbs_g']}g carbs, "
                    f"{live['fat_g']}g fat, {live['fiber_g']}g fiber\n"
                    f"Use these exact values. Set confidence to 'high'.\n\n"
                )
            else:
                db_block = (
                    f"MEAL KIT DELIVERY SERVICE DETECTED ({meal_kit_brand}): "
                    f"Could not find this specific meal on the {meal_kit_brand} website. "
                    f"Estimate from the ingredients in the name, set confidence to 'low', "
                    f"and note that the user should check the {meal_kit_brand} app for exact nutrition.\n\n"
                )
        else:
            sections = []

            personal_matches = _match_saved_meals(raw_text, saved_meals or [])
            if personal_matches:
                lines = ["PERSONAL MEAL HISTORY (user's own logged data — use these values if the name matches):"]
                for sm in personal_matches:
                    lines.append(
                        f"  • {sm['name']}: {sm['calories']} kcal, "
                        f"{sm['protein_g']}g protein, {sm['carbs_g']}g carbs, "
                        f"{sm['fat_g']}g fat, {sm['fiber_g']}g fiber"
                    )
                sections.append("\n".join(lines))

            if not personal_matches:
                hits = _lookup_open_food_facts(raw_text) or _lookup_branded_nutrition(raw_text)
                if hits:
                    lines = [
                        "PRODUCT DATABASE — use these values only if the brand AND product name "
                        "closely match. If the brand differs, discard and estimate:"
                    ]
                    for h in hits:
                        brand_prefix = f"{h['brand']} — " if h["brand"] else ""
                        lines.append(
                            f"  • {brand_prefix}{h['name']} (per {h['serving']}): "
                            f"{h['calories']} kcal, {h['protein_g']}g protein, "
                            f"{h['carbs_g']}g carbs, {h['fat_g']}g fat, {h['fiber_g']}g fiber"
                        )
                    sections.append("\n".join(lines))

            db_block = "\n\n".join(sections) + "\n\n" if sections else ""

        prompt = f"""You are a nutrition estimation assistant. Parse the user's freeform food description into structured nutrition data. Estimate reasonable values for typical portions when quantities aren't specified. Be realistic, not perfectionist — this is for casual tracking.

{db_block}USER INPUT: "{raw_text}"
MEAL CONTEXT: {meal_context}

Respond with ONLY valid JSON, no markdown fences, no preamble:
{json_schema}

Rules:
- BRAND NAMES & MEAL KITS: Check context blocks in priority order: (1) PERSONAL MEAL HISTORY — use exact values, confidence "high"; (2) PRODUCT DATABASE — use only if brand matches, confidence "high"; (3) MEAL KIT DELIVERY SERVICE — estimate from ingredients, follow instructions; (4) your own training knowledge — confidence "medium". Never substitute a different brand's product.
- Estimate standard portions if unspecified (e.g. "toast" = 1 slice, "banana" = 1 medium)
- confidence is "high", "medium", or "low" — use "low" if the input is vague or hard to quantify
- Round all numbers to whole integers
- If input is genuinely not food, return items: [] with a descriptive note
- Separate combo items into individual components when reasonable (e.g. "eggs and toast" → two rows)"""

        message_content = prompt

    try:
        result = llm.call_json(prompt, model=llm.SONNET, max_tokens=600,
                               message_content=message_content, timeout=30)
        result["ok"] = True
        return result
    except json.JSONDecodeError as e:
        logger.warning("parse_food_text JSON decode failed: %s", e)
        return {"ok": False, "error": "parse_failed", "items": [], "confidence": "low", "note": ""}
    except Exception as e:
        logger.warning("parse_food_text failed: %s", e)
        return {"ok": False, "error": str(e), "items": [], "confidence": "low", "note": ""}


def suggest_meals(
    remaining_cal: float,
    remaining_protein: float,
    remaining_carbs: float,
    remaining_fat: float,
    remaining_fiber: float,
    meal_summary: str = "",
    time_of_day: str = "",
    recent_meals: list | None = None,
    top_foods: list | None = None,
    current_hunger: int | None = None,
    gi_symptoms: bool = False,
) -> dict:
    """
    Suggest 3-4 protein-forward meals/snacks that fit the remaining macros.
    Optionally scales suggestion size based on hunger level (1-10) and
    avoids high-fat options when GI symptoms are present.
    Returns {"suggestions": [...], "tip": "...", "gi_note": "..."}
    """
    # Recent meals context (avoid repeats)
    recent_ctx = ""
    if recent_meals:
        names = [m for m in recent_meals if m][:8]
        if names:
            recent_ctx = "\n- Recent meals (avoid repeating these): " + ", ".join(names)

    # Top foods context (lean toward familiar foods)
    top_ctx = ""
    if top_foods:
        names = [f["name"] for f in top_foods[:8]]
        if names:
            top_ctx = "\n- Foods they commonly eat (prefer suggestions using these): " + ", ".join(names)

    # Hunger-based size guidance
    hunger_ctx = ""
    size_guidance = ""
    if current_hunger is not None:
        if current_hunger <= 3:
            size_guidance = "They are NOT very hungry right now (hunger level {}/10). Suggest SMALLER options: 60-200 calories each — protein-dense snacks, not full meals.".format(current_hunger)
        elif current_hunger <= 6:
            size_guidance = "Their hunger level is moderate ({}/10). Suggest standard-sized options: 300-500 calories each.".format(current_hunger)
        else:
            size_guidance = "They are quite hungry right now ({}/10). Suggest more substantial options: 500-700 calories each.".format(current_hunger)
        hunger_ctx = f"\n- Current hunger level: {current_hunger}/10"

    # GI symptom guidance
    gi_ctx = ""
    gi_note_str = ""
    if gi_symptoms:
        gi_ctx = "\n- IMPORTANT: User has logged nausea or bloating in the last 24 hours. Avoid high-fat foods. Favor lower-volume, easily-digested options (e.g. rice, toast, banana, lean protein, broth-based soups). No greasy, fried, or very high-fiber options."
        gi_note_str = "Suggestions adjusted for recent GI symptoms"

    from .nutrition import compute_macro_targets
    from .models import NutritionProfile
    _profile = NutritionProfile.objects.filter(pk=1).first()
    _targets = compute_macro_targets(_profile) if _profile else None
    top_gap = _macro_priority_hint(remaining_protein, remaining_carbs, remaining_fiber, _targets)

    persona = build_persona_block()
    persona_line = f"\n{persona}" if persona else ""

    prompt = f"""You are a meal suggestion assistant.{persona_line}

REMAINING MACROS FOR TODAY:
- Calories: {remaining_cal:.0f}
- Protein: {remaining_protein:.0f}g{' (top gap)' if top_gap == 'protein' else ''}
- Carbs: {remaining_carbs:.0f}g{' (top gap)' if top_gap == 'carbs' else ''}
- Fat: {remaining_fat:.0f}g
- Fiber: {remaining_fiber:.0f}g{' (top gap)' if top_gap == 'fiber' else ''}

CONTEXT:
- Time of day: {time_of_day or "unknown"}
- Meals already logged today: {meal_summary or "none"}{hunger_ctx}{gi_ctx}{recent_ctx}{top_ctx}

{size_guidance}

Suggest 3-4 meal or snack ideas that fit the remaining macros. Requirements:
- Protein-dense above all else
- Include fiber where possible (psyllium, chia, legumes, vegetables)
- Realistic, easy to prepare — no elaborate cooking
- Do NOT suggest meals very similar to recent meals listed above
- Lean toward familiar foods when possible
- For each: name, estimated macros, and one sentence on why it fits

Respond with ONLY valid JSON, no markdown:
{{
  "suggestions": [
    {{"name": "...", "calories": N, "protein_g": N, "carbs_g": N, "fat_g": N, "fiber_g": N, "why": "..."}}
  ],
  "tip": "one-line tip for hitting remaining macros (focus on protein if it's the main gap)"
}}"""

    try:
        data = llm.call_json(prompt, model=llm.HAIKU, max_tokens=900, timeout=30)
        if gi_note_str:
            data["gi_note"] = gi_note_str
        return data
    except Exception as e:
        logger.warning("suggest_meals failed: %s", e)
        return {"suggestions": [], "tip": "", "gi_note": ""}


# ---------------------------------------------------------------------------
# Nutrition analytics insights — synchronous (Claude Sonnet, weekly cache)
# ---------------------------------------------------------------------------

def _get_or_generate_nutrition_insights(range_days: int = 30, force: bool = False) -> str:
    """Sonnet analysis of nutrition patterns. Cached 7 days in UserSettings."""
    def _gen():
        from datetime import date as date_cls
        from .nutrition import compute_macro_targets, get_top_foods
        from .models import NutritionProfile

        today = date_cls.today()
        start = today - timedelta(days=range_days - 1)

        profile = NutritionProfile.objects.filter(pk=1).first()
        targets = compute_macro_targets(profile) if profile else None

        # Pull logged DailyStats
        stats_qs = list(
            DailyStats.objects.filter(date__gte=start, date__lte=today, cal_total__isnull=False)
            .order_by("date")
        )
        total_days = range_days
        logged = len(stats_qs)

        if not logged:
            return ""

        def _avg(field):
            vals = [getattr(s, field) or 0 for s in stats_qs]
            return round(sum(vals) / len(vals)) if vals else None

        avg_cal = _avg("cal_total")
        avg_prot = _avg("protein_g_total")
        avg_fiber = _avg("fiber_g_total")

        cal_t = targets.get("calories") if targets else None
        prot_t = targets.get("protein_g") if targets else None
        fiber_t = targets.get("fiber_g") if targets else None
        carbs_t = targets.get("carbs_g") if targets else None
        fat_t = targets.get("fat_g") if targets else None

        def _days_hit(field, target, pct=0.9):
            if not target:
                return None
            return sum(1 for s in stats_qs if (getattr(s, field) or 0) >= target * pct)

        days_hit_cal = _days_hit("cal_total", cal_t, pct=1.0)  # within target (≤110%)
        # for calories, "hit" means ≤ 110%
        if cal_t:
            days_hit_cal = sum(1 for s in stats_qs if s.cal_total and s.cal_total <= cal_t * 1.1)
        days_hit_prot = _days_hit("protein_g_total", prot_t)
        days_hit_fiber = _days_hit("fiber_g_total", fiber_t, pct=0.85)

        # Weekday vs weekend
        weekday_cals = [s.cal_total for s in stats_qs if s.date.weekday() < 5 and s.cal_total]
        weekend_cals = [s.cal_total for s in stats_qs if s.date.weekday() >= 5 and s.cal_total]
        wd_avg = round(sum(weekday_cals) / len(weekday_cals)) if weekday_cals else None
        we_avg = round(sum(weekend_cals) / len(weekend_cals)) if weekend_cals else None

        # Weight trend
        weight_stats = list(
            DailyStats.objects.filter(date__gte=start, date__lte=today, weight_lb__isnull=False)
            .order_by("date")
        )
        weight_start = weight_stats[0].weight_lb if weight_stats else None
        weight_end = weight_stats[-1].weight_lb if weight_stats else None
        if weight_start and weight_end:
            weight_delta = round(weight_end - weight_start, 1)
            trend_desc = f"{weight_start:.1f}lb → {weight_end:.1f}lb ({weight_delta:+.1f}lb)"
        else:
            trend_desc = "No weight data"

        # Top 5 foods
        top_foods = get_top_foods(start, today, top_n=5)
        top_food_lines = "\n".join(
            f"  - {f['name']} (logged {f['count']}x, avg {f['avg_calories']:.0f} kcal, {f['avg_protein_g']:.0f}g P)"
            for f in top_foods
        ) if top_foods else "  No data"

        # Intervention context
        iv_ctx = _interventions_context(start, today)

        targets_section = ""
        if targets:
            targets_section = f"""TARGETS:
- Calories: {cal_t or 'not set'}
- Protein: {f'{prot_t}g' if prot_t else 'not set'}
- Carbs: {f'{carbs_t}g' if carbs_t else 'not set'}
- Fat: {f'{fat_t}g' if fat_t else 'not set'}
- Fiber: {f'{fiber_t}g' if fiber_t else 'not set'}

"""

        persona = build_persona_block(date_range=(start, today))
        tone = coaching_tone_instruction()
        persona_section = f"\n{persona}" if persona else ""
        tone_section = f"\n{tone}" if tone else ""
        prompt = f"""You are analyzing nutrition tracking data.{persona_section}{tone_section}

{targets_section}LAST {range_days} DAYS (logged {logged}/{total_days} days):
- Avg calories: {avg_cal or 'n/a'}{f' (hit target {days_hit_cal}/{logged} days)' if days_hit_cal is not None else ''}
- Avg protein: {avg_prot or 'n/a'}g{f' (hit target {days_hit_prot}/{logged} days)' if days_hit_prot is not None else ''}
- Avg fiber: {avg_fiber or 'n/a'}g{f' (hit target {days_hit_fiber}/{logged} days)' if days_hit_fiber is not None else ''}

DAY OF WEEK PATTERNS:
- Weekday avg calories: {wd_avg or 'n/a'}
- Weekend avg calories: {we_avg or 'n/a'}

TOP FOODS (most logged):
{top_food_lines}

WEIGHT TREND OVER SAME PERIOD:
{trend_desc}

ACTIVE INTERVENTIONS:
{iv_ctx}

Write the analysis in exactly this structure. Write each section as 2-3 sentences of flowing paragraph text — no bullet points or lists. Use **bold** for emphasis on specific numbers or key points.

## What's working
2-3 sentences on specific patterns going well. Cite actual numbers.

## Where the friction is
2-3 sentences on specific patterns limiting progress. Be honest but not preachy.

## Specific suggestions
2-3 sentences with concrete, low-friction suggestions. Match their personality: no elaborate meal prep or obsessive counting.

## Watch list
1-2 sentence on something to keep an eye on over the next few weeks.

Avoid: generic wellness advice, recommending specific diets, being judgmental, ignoring that they're on medications, any commentary about meal timing or eating windows (food is logged retroactively, so log timestamps do not reflect actual eating times)."""

        return llm.call(prompt, model=llm.SONNET, max_tokens=1400, timeout=60)

    return cached_settings_field(
        "ai_nutrition_insights", 168, _gen,
        force=force, extra_save={"ai_nutrition_insights_range": range_days},
    )


def nutrition_insights_refresh(request):
    """POST /api/nutrition/insights/refresh/ — force-regenerate nutrition insights, return HTML partial."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    range_param = request.GET.get("range", "30d")
    range_days = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(range_param, 30)
    insights = _get_or_generate_nutrition_insights(range_days=range_days, force=True)
    from django.shortcuts import render as _render
    return _render(request, "workouts/partials/nutrition_insights.html", {"insights": insights})


# ---------------------------------------------------------------------------
# Intervention analysis interpretation — synchronous (Claude Sonnet)
# ---------------------------------------------------------------------------

def _generate_intervention_interpretation(
    analysis_result: dict,
    intervention=None,
    interventions_context_str: str = "",
    nutrition_gaps: dict | None = None,
) -> str:
    """Sonnet interpretation of intervention analysis. Returns plain text markdown."""
    try:
        lines = []

        if intervention:
            lines.append(f"INTERVENTION: {intervention.name}")
            cd = intervention.current_dose
            if cd:
                lines.append(f"Dose: {cd.dose}")
            lines.append(f"Category: {intervention.category}")
            if intervention.expected_effects:
                lines.append(f"Expected effects: {intervention.expected_effects}")
            lines.append("")

        before_start = analysis_result.get("before_start")
        before_end   = analysis_result.get("before_end")
        after_start  = analysis_result.get("after_start")
        after_end    = analysis_result.get("after_end")
        before_n = analysis_result.get("before_n", 0)
        after_n  = analysis_result.get("after_n", 0)

        lines.append(f"ANALYSIS WINDOWS")
        lines.append(f"Before: {before_start} → {before_end} ({before_n} days with data)")
        lines.append(f"After:  {after_start} → {after_end} ({after_n} days with data)")
        lines.append("")

        if interventions_context_str:
            lines.append("OTHER CONCURRENT INTERVENTIONS")
            lines.append(interventions_context_str)
            lines.append("")

        lines.append("METRICS (before mean → after mean, % change, improved?)")
        for group in analysis_result.get("groups", []):
            lines.append(f"\n{group['name']}")
            for m in group.get("metrics", []):
                bm = m.get("before_mean")
                am = m.get("after_mean")
                if bm is None and am is None:
                    continue
                pct = m.get("pct_change")
                imp = m.get("improved")
                pct_str = f"{pct:+.1f}%" if pct is not None else "n/a"
                imp_str = "improved" if imp is True else ("worsened" if imp is False else "neutral/n/a")
                bm_str = f"{bm:.2f}" if bm is not None else "—"
                am_str = f"{am:.2f}" if am is not None else "—"
                unit = m.get("unit", "")
                lines.append(
                    f"  {m['display_name']}: {bm_str}{' '+unit if unit else ''} → "
                    f"{am_str}{' '+unit if unit else ''} ({pct_str}, {imp_str})"
                )

        # Nutrition gap context
        if nutrition_gaps:
            before_gap = nutrition_gaps.get("before", {})
            after_gap = nutrition_gaps.get("after", {})
            before_pct = before_gap.get("pct", 0)
            after_pct = after_gap.get("pct", 0)
            lines.append("\nNUTRITION DATA COVERAGE")
            lines.append(
                f"Before window: {before_gap.get('days_logged', 0)}/{before_gap.get('total_days', 0)} days logged ({before_pct}%)"
            )
            lines.append(
                f"After window: {after_gap.get('days_logged', 0)}/{after_gap.get('total_days', 0)} days logged ({after_pct}%)"
            )

            # Pull avg nutrition from analysis result if present
            for group in analysis_result.get("groups", []):
                if group["name"] == "NUTRITION":
                    n_lines = []
                    for m in group.get("metrics", []):
                        bm = m.get("before_mean")
                        am = m.get("after_mean")
                        if bm is not None or am is not None:
                            unit = m.get("unit", "")
                            bm_s = f"{bm:.0f}{unit}" if bm is not None else "—"
                            am_s = f"{am:.0f}{unit}" if am is not None else "—"
                            pct = m.get("pct_change")
                            delta = f" ({pct:+.0f}%)" if pct is not None else ""
                            n_lines.append(f"  {m['display_name']}: {bm_s} → {am_s}{delta}")
                    if n_lines:
                        lines.append("Notable nutrition shifts:\n" + "\n".join(n_lines))
                    break

        prompt = "\n".join(lines) + """

Please provide an interpretation of this before/after intervention analysis.
Respond using these markdown headers exactly:

## Headline
One paragraph summary of the most notable finding.

## What the data suggests
2-3 paragraphs interpreting the metrics. Reference specific numbers. Consider the intervention's mechanism and whether the observed changes align with expected effects. If nutrition data coverage is below 50% in either window, note that nutrition findings have limited reliability.

## Caveats and confounds
1 paragraph on limitations: sample size, other concurrent interventions, natural variation, seasonality, or other factors that could explain the changes.

## What to watch next
- bullet
- bullet
- bullet

Be specific and data-driven. Avoid generic advice."""

        return llm.call(prompt, model=llm.SONNET, max_tokens=1200, timeout=60)
    except Exception as e:
        logger.warning("Intervention interpretation failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Pattern insights — weekly Sonnet deep analysis (Phase 3)
# ---------------------------------------------------------------------------

def _get_or_generate_pattern_insights(force=False) -> str:
    """
    Weekly Sonnet-powered deep pattern analysis across nutrition, weight,
    recovery, workouts, and symptoms. Cached 7 days in UserSettings.
    """
    def _gen():
        from datetime import date as date_cls, datetime as datetime_cls
        today = date_cls.today()
        cutoff_60 = today - timedelta(days=60)

        def _as_date(v):
            """Normalize TruncWeek result — DateField→date, DateTimeField→datetime on SQLite."""
            return v.date() if isinstance(v, datetime_cls) else v

        # ── Weight trend ────────────────────────────────────────────────────
        weight_rows = list(
            DailyStats.objects.filter(date__gte=cutoff_60, date__lte=today, weight_lb__isnull=False)
            .order_by("date").values_list("date", "weight_lb", "fat_ratio_pct", "muscle_mass_lb")
        )
        weight_lines = [
            f"  {d}: {w:.1f} lb"
            + (f", {f:.1f}% fat" if f else "")
            + (f", {m:.1f} lb muscle" if m else "")
            for d, w, f, m in weight_rows
        ]

        # ── Recovery metrics (weekly averages) ──────────────────────────────
        recovery_rows = list(
            DailyStats.objects.filter(date__gte=cutoff_60, date__lte=today)
            .annotate(week=TruncWeek("date"))
            .values("week")
            .annotate(
                avg_hrv=Avg("hrv_last_night"),
                avg_rhr=Avg("resting_hr"),
                avg_sleep=Avg("sleep_score"),
                avg_bb=Avg("body_battery_high"),
                avg_stress=Avg("stress_avg"),
                avg_readiness=Avg("training_readiness_score"),
            )
            .order_by("week")
        )
        recovery_full = []
        for r in recovery_rows:
            parts = [f"  Week of {_as_date(r['week'])}:"]
            if r['avg_hrv']: parts.append(f"HRV {r['avg_hrv']:.0f}ms")
            if r['avg_rhr']: parts.append(f"RHR {r['avg_rhr']:.0f}")
            if r['avg_sleep']: parts.append(f"sleep score {r['avg_sleep']:.0f}")
            if r['avg_bb']: parts.append(f"body battery peak {r['avg_bb']:.0f}")
            if r['avg_stress']: parts.append(f"stress {r['avg_stress']:.0f}")
            if r['avg_readiness']: parts.append(f"readiness {r['avg_readiness']:.0f}")
            recovery_full.append(" ".join(parts))

        # ── Nutrition (daily, last 60 days) ──────────────────────────────────
        nutr_rows = list(
            DailyStats.objects.filter(
                date__gte=cutoff_60, date__lte=today, cal_total__isnull=False
            ).order_by("date").values_list("date", "cal_total", "protein_g_total", "fiber_g_total")
        )
        nutr_lines = [
            f"  {d}: {cal:.0f} kcal, {prot:.0f}g protein, {fib:.1f}g fiber"
            for d, cal, prot, fib in nutr_rows
            if cal
        ]

        # ── Hunger patterns (weekly avg morning hunger) ────────────────────
        from .models import HungerCheck
        hunger_lines = []
        hunger_rows = list(
            HungerCheck.objects.filter(date__gte=cutoff_60, context="morning")
            .annotate(week=TruncWeek("date"))
            .values("week")
            .annotate(avg_hunger=Avg("hunger_level"))
            .order_by("week")
        )
        for r in hunger_rows:
            hunger_lines.append(f"  Week of {_as_date(r['week'])}: avg morning hunger {r['avg_hunger']:.1f}/10")

        # ── Side effects (weekly counts) ─────────────────────────────────────
        from .models import SideEffectLog
        symptom_lines = []
        symptom_rows = list(
            SideEffectLog.objects.filter(date__gte=cutoff_60)
            .annotate(week=TruncWeek("date"))
            .values("week", "symptom")
            .annotate(count=Count("id"), avg_severity=Avg("severity"))
            .order_by("week", "symptom")
        )
        if symptom_rows:
            by_week: dict = {}
            for r in symptom_rows:
                w = str(_as_date(r["week"]))
                by_week.setdefault(w, []).append(
                    f"{r['symptom']} ×{r['count']} (avg severity {r['avg_severity']:.1f})"
                )
            for week, symptoms in sorted(by_week.items()):
                symptom_lines.append(f"  Week of {week}: {', '.join(symptoms)}")

        # ── Workouts (weekly volume) ─────────────────────────────────────────
        from .models import CachedWorkout
        workout_rows = list(
            CachedWorkout.objects.filter(
                created_at__date__gte=cutoff_60,
                created_at__date__lte=today,
            )
            .annotate(week=TruncWeek("created_at"))
            .values("week")
            .annotate(count=Count("id"))
            .order_by("week")
        )
        workout_lines = [f"  Week of {_as_date(r['week'])}: {r['count']} workouts" for r in workout_rows]

        # ── Interventions ────────────────────────────────────────────────────
        iv_context = _interventions_context(today - timedelta(days=60), today)

        # ── Build prompt ──────────────────────────────────────────────────────
        sections = [
            "WEIGHT & BODY COMPOSITION (last 60 days — daily)",
            "\n".join(weight_lines) if weight_lines else "  No weight data",
            "",
            "RECOVERY METRICS (weekly averages)",
            "\n".join(recovery_full) if recovery_full else "  No recovery data",
            "",
            "NUTRITION (daily logged days)",
            "\n".join(nutr_lines[-30:]) if nutr_lines else "  No nutrition data logged",
            "",
        ]
        if hunger_lines:
            sections += ["HUNGER PATTERNS (weekly avg morning hunger)", "\n".join(hunger_lines), ""]
        if symptom_lines:
            sections += ["SIDE EFFECTS (weekly counts)", "\n".join(symptom_lines), ""]
        if workout_lines:
            sections += ["WORKOUT VOLUME (weekly)", "\n".join(workout_lines), ""]
        if iv_context:
            sections += ["INTERVENTIONS & MEDICATIONS", iv_context, ""]

        data_block = "\n".join(sections)
        persona = build_persona_block(date_range=(today - timedelta(days=60), today))
        persona_section = f"\n{persona}" if persona else ""

        prompt = f"""You are analyzing up to 60 days of integrated health data. Find non-obvious patterns the user might miss.{persona_section}

{data_block}

INSTRUCTIONS:
Find 3–5 non-obvious patterns across the data above. Look for:
- Lagged correlations (something on day X affecting outcomes at day X+2 or X+7)
- Threshold effects (e.g. after protein exceeds X, weight trend improves)
- Day-of-week patterns in nutrition, recovery, or symptoms
- Recovery degradation that precedes weight plateau
- Hunger creep that may signal dose tolerance
- Symptoms clustering around dose changes or food patterns
- Workout output or recovery declining without obvious cause

For each pattern use this structure:
## [Pattern name]
**What the data shows:** cite specific numbers and dates
**What it might mean:** physiological or behavioral interpretation
**What to watch:** one concrete thing to track or try (optional — only if clear)

End with:
## Highest-confidence pattern
One sentence naming the pattern with the strongest signal in the data.

## Most worth testing
One hypothesis they could actively test in the next 2 weeks.

Be specific and data-driven. Avoid generic advice. Do not recommend medical decisions. 3–5 patterns only — quality over quantity."""

        return llm.call(prompt, model=llm.SONNET, max_tokens=1800, timeout=90)

    return cached_settings_field("ai_pattern_insights", 168, _gen, force=force)


def pattern_insights_refresh(request):
    """POST — force-refresh pattern insights; returns rendered HTML fragment."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    insights = _get_or_generate_pattern_insights(force=True)
    from django.shortcuts import render as _render
    return _render(request, "workouts/partials/pattern_insights.html", {"insights": insights})


# ---------------------------------------------------------------------------
# Weekly review — Claude Sonnet, cached per calendar week
# ---------------------------------------------------------------------------

def _get_or_generate_weekly_review(week_start, force: bool = False):
    """
    Generate (or return cached) a WeeklyReview for the given Monday week_start.
    Returns the WeeklyReview instance, or None on failure.
    """
    from datetime import timedelta
    from .models import WeeklyReview, CachedWorkout, DailyStats, HungerCheck, SideEffectLog

    if not force:
        try:
            return WeeklyReview.objects.get(week_start=week_start)
        except WeeklyReview.DoesNotExist:
            pass

    week_end = week_start + timedelta(days=6)

    # --- Weight & body comp ---
    daily_qs = DailyStats.objects.filter(date__gte=week_start, date__lte=week_end)
    weights = [(d.date.isoformat(), round(d.weight_lb, 1)) for d in daily_qs if d.weight_lb]
    weight_lines = "\n".join(f"  {d}: {w} lb" for d, w in weights) if weights else "  (no data)"

    # Prior week weight for comparison
    prior_end = week_start - timedelta(days=1)
    prior_start = week_start - timedelta(days=7)
    prior_weights = list(DailyStats.objects.filter(
        date__gte=prior_start, date__lte=prior_end, weight_lb__isnull=False
    ).values_list("weight_lb", flat=True))
    prior_avg = sum(prior_weights) / len(prior_weights) if prior_weights else None
    current_avg = sum(w for _, w in weights) / len(weights) if weights else None
    weight_change_str = ""
    if prior_avg and current_avg:
        diff = current_avg - prior_avg
        weight_change_str = f"  Week avg: {current_avg:.1f} lb vs prior week avg {prior_avg:.1f} lb ({diff:+.1f} lb)"

    # --- Nutrition ---
    nutrition_rows = []
    for d in daily_qs:
        if d.cal_total:
            nutrition_rows.append(
                f"  {d.date}: {d.cal_total:.0f} kcal, {d.protein_g_total or 0:.0f}g P, "
                f"{d.carbs_g_total or 0:.0f}g C, {d.fat_g_total or 0:.0f}g F, "
                f"{d.fiber_g_total or 0:.0f}g fiber"
            )
    nutrition_str = "\n".join(nutrition_rows) if nutrition_rows else "  (no nutrition data logged)"

    # Targets for context
    try:
        from .models import NutritionProfile
        from .nutrition import compute_macro_targets
        profile = NutritionProfile.objects.filter(pk=1).first()
        targets = compute_macro_targets(profile) if profile else None
        if targets:
            target_str = (
                f"  Targets: {targets.get('calories', '?'):.0f} kcal, "
                f"{targets.get('protein_g', '?'):.0f}g P, {targets.get('fiber_g', '?'):.0f}g fiber"
            )
        else:
            target_str = "  (targets not configured)"
    except Exception:
        target_str = "  (targets unavailable)"

    # --- Workouts ---
    workouts = list(CachedWorkout.objects.filter(
        created_at__date__gte=week_start, created_at__date__lte=week_end
    ).order_by("created_at"))
    workout_lines = []
    for w in workouts:
        parts_w = [f"{w.created_at.strftime('%a')} {w.discipline}"]
        if w.title:
            parts_w.append(f'"{w.title}"')
        if w.duration_seconds:
            parts_w.append(f"{w.duration_seconds // 60} min")
        if w.calories:
            parts_w.append(f"{w.calories:.0f} kcal")
        workout_lines.append("  " + " · ".join(parts_w))
    workouts_str = "\n".join(workout_lines) if workout_lines else "  (no workouts)"

    # --- Recovery averages ---
    hrv_vals = [d.hrv_last_night for d in daily_qs if d.hrv_last_night]
    sleep_vals = [d.sleep_seconds / 3600 for d in daily_qs if d.sleep_seconds]
    rhr_vals = [d.resting_hr for d in daily_qs if d.resting_hr]
    sleep_str = f"  Avg sleep: {sum(sleep_vals)/len(sleep_vals):.1f}h" if sleep_vals else "  Avg sleep: n/a"
    hrv_str = f", avg HRV: {sum(hrv_vals)/len(hrv_vals):.0f} ms" if hrv_vals else ""
    rhr_str = f", avg RHR: {sum(rhr_vals)/len(rhr_vals):.0f} bpm" if rhr_vals else ""

    # --- Hunger & symptoms ---
    hunger_qs = HungerCheck.objects.filter(date__gte=week_start, date__lte=week_end)
    morning_hunger = [h.hunger_level for h in hunger_qs if h.context == "morning"]
    hunger_str = f"  Morning hunger avg: {sum(morning_hunger)/len(morning_hunger):.1f}/10" if morning_hunger else "  Morning hunger: (not tracked)"

    symptoms_qs = SideEffectLog.objects.filter(date__gte=week_start, date__lte=week_end)
    symptom_counts: dict = {}
    for s in symptoms_qs:
        symptom_counts[s.symptom] = symptom_counts.get(s.symptom, 0) + 1
    symptoms_str = (
        "  " + ", ".join(f"{k} ×{v}" for k, v in sorted(symptom_counts.items(), key=lambda x: -x[1]))
        if symptom_counts else "  (none logged)"
    )

    # --- Active interventions ---
    interventions_ctx = _interventions_context(week_start, week_end)

    persona = build_persona_block(date_range=(week_start, week_end))
    persona_section = f"\n{persona}" if persona else ""
    prompt = f"""You are reviewing someone's health and fitness week ({week_start} to {week_end}).{persona_section}

WEIGHT THIS WEEK:
{weight_lines}
{weight_change_str}

NUTRITION THIS WEEK:
{nutrition_str}
{target_str}

WORKOUTS:
{workouts_str}

RECOVERY:
{sleep_str}{hrv_str}{rhr_str}

HUNGER TRACKING:
{hunger_str}

SYMPTOMS THIS WEEK:
{symptoms_str}

ACTIVE INTERVENTIONS:
{interventions_ctx}

Write a concise weekly review covering:
## Weight & Body Composition
One paragraph on weight trend vs. goal, notable changes.

## Nutrition
How well they hit targets. Patterns (protein gaps, good days, weekend drift, etc.).

## Training
What they did, whether it aligns with their goals, recovery quality.

## Hunger & Symptoms
Any patterns in hunger or symptoms worth noting.

## One Thing Going Well
A single specific positive.

## One Focus for Next Week
One specific, actionable thing to improve next week.

Use **bold** for emphasis. Be direct, specific, and data-driven. Skip sections where there's no data. Keep the whole review under 500 words."""

    try:
        content = llm.call(prompt, model=llm.SONNET, max_tokens=1200, timeout=60)

        review, _ = WeeklyReview.objects.update_or_create(
            week_start=week_start,
            defaults={"content": content, "ai_model": llm.SONNET},
        )
        return review
    except Exception as e:
        logger.warning("Weekly review generation failed: %s", e)
        try:
            return WeeklyReview.objects.get(week_start=week_start)
        except WeeklyReview.DoesNotExist:
            return None
