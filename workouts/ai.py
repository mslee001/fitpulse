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
from django.db.models import Avg, Count, Q
from django.db.models.functions import Coalesce, TruncWeek
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.utils import timezone as tz

from . import llm
from .models import CachedWorkout, DailyStats, UserSettings, Intervention
from .prompt_formats import HEADLINE_BULLETS_FORMAT, INTENSITY_ACTIVITY_REASON_FORMAT
from .services.chat_tools import CHAT_TOOLS, build_tool_dispatch

# Stricter headline guidance for the day-analysis prompt only.
# compare_analysis keeps the looser HEADLINE_BULLETS_FORMAT because a comparison
# headline that summarises the key difference is appropriate there.
DAY_HEADLINE_BULLETS_FORMAT = """\
Respond in exactly this format with no extra text before or after:

HEADLINE: <one sentence naming the single most notable thing about this day. \
This is NOT a summary of the day. Pick the most interesting signal — if it's \
in nutrition, intervention timing, or a multi-day pattern, lead with that, \
not with the workout. Avoid filler adjectives ("solid", "good", "great", \
"nice") and avoid generic framings ("a recovery day", "a training day") \
unless they're earned by an explicit comparison. The headline must be consistent with the bullets — do not use it to soften or qualify observations that the bullets state directly.>
• <specific observation referencing actual numbers>
• <specific observation referencing actual numbers>
• <specific observation referencing actual numbers>

Max 3 bullets. Each bullet makes one observation and cites a specific metric. \
Do not open a bullet by restating today's activity before making the point — \
lead with the observation itself. If the point is a multi-day pattern, \
state the pattern directly without prefacing it with what happened today."""

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Raised by llm.guard() before any request goes out; views turn them into a
# short note (partials/ai_unavailable.html) instead of an error.
AI_UNAVAILABLE = (llm.AIBudgetExceeded, llm.AIFeatureDenied)


def ai_unavailable_reason(exc):
    if isinstance(exc, llm.AIBudgetExceeded):
        return "You've reached this month's AI limit. It resets on the 1st."
    return "This AI feature isn't turned on for your account."


def render_ai_unavailable(request, exc, status=200):
    from django.shortcuts import render
    return render(request, "workouts/partials/ai_unavailable.html",
                  {"ai_unavailable_reason": ai_unavailable_reason(exc)}, status=status)


def _render_poll_partial(request, template_name, context, status=200):
    """Render an HTMX batch-poll partial.

    status=286 is HTMX's "stop polling" convention. Pass it for every terminal
    (non-pending) state — no batch, missing API key, no successful result, or
    a resolved success — so a browser tab left open on an unresolved batch
    can't keep re-querying the DB every 30s forever. Genuinely pending states
    keep the default 200 so HTMX continues polling.
    """
    from django.shortcuts import render
    return render(request, template_name, context, status=status)


def _interventions_context(user, start_date, end_date) -> str:
    """Human-readable summary of interventions and dose changes overlapping the given date range."""
    from django.db.models import Q
    overlapping = Intervention.objects.for_user(user).filter(
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
            dose_days = (end_date - d.start_date).days + 1 if d.start_date <= end_date else 0
            dose_weeks = dose_days // 7
            duration_note = f" ({dose_days} days / week {dose_weeks + 1})" if dose_days > 0 else ""
            lines.append(f"    • {d.dose} from {d.start_date} to {d_end}{duration_note}")
        if iv.expected_effects:
            lines.append(f"    Expected: {iv.expected_effects}")
    return "\n".join(lines)


def build_persona_block(user, date_range=None, *, include_interventions=True) -> str:
    """
    One paragraph about the user, built ONLY from data they entered.
    date_range: optional (start_date, end_date) tuple to scope intervention listing.
    Returns "" if no profile data and no interventions — caller's prompt should
    work fine without it.
    """
    from .models import AthleteProfile
    profile = AthleteProfile.for_user(user)

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
        iv_ctx = _interventions_context(user, start, end)
        if iv_ctx and "No tracked interventions" not in iv_ctx:
            parts.append("Active interventions/medications affecting this user:\n" + iv_ctx)

    return "\n\n".join(parts)


def coaching_tone_instruction(user) -> str:
    """Return a short directive matching the user's tone preference."""
    from .models import AthleteProfile
    tone = AthleteProfile.for_user(user).coaching_tone
    return {
        "encouraging": "Be encouraging and constructive while staying honest about the data.",
        "direct":      "Be direct and concise. Skip pleasantries.",
        "data_only":   "Report what the data shows without commentary or recommendations.",
    }.get(tone, "")


def rehab_flag_for(user, title: str) -> str:
    """Return ' [PT/REHAB — not a training session]' if title matches a configured rehab keyword."""
    from .models import AthleteProfile
    keywords = AthleteProfile.for_user(user).rehab_keywords or []
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

def cached_settings_field(user, field_name, ttl_hours, generator, *, force=False, extra_save=None):
    """Read-through cache for UserSettings-backed AI text fields."""
    settings = UserSettings.for_user(user)
    cached = getattr(settings, field_name)
    stamp  = getattr(settings, f"{field_name}_generated_at")
    if not force and cached and stamp:
        if (tz.now() - stamp).total_seconds() / 3600 < ttl_hours:
            return cached
    try:
        new_text = generator()
    except AI_UNAVAILABLE:
        raise
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
    except AI_UNAVAILABLE:
        raise
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


def _build_insights_summary(user):
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

    total_all  = CachedWorkout.objects.for_user(user).count()
    total_365d = CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_365d).count()
    total_90d  = CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_90d).count()

    # Discipline breakdown: count + avg duration over the last 90 days
    disc_rows = list(
        CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_90d)
        .values("discipline")
        .annotate(count=Count("id"), avg_dur=Avg("duration_seconds"))
        .order_by("-count")
    )

    # Running form metrics (populated by Garmin augmentation)
    run_form_qs = list(
        CachedWorkout.objects.for_user(user)
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
        CachedWorkout.objects.for_user(user)
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

    ftp = UserSettings.for_user(user).ftp
    avg_pct_ftp = (
        round(sum(cyc_watts_list) / len(cyc_watts_list) / ftp * 100)
        if cyc_watts_list and ftp else None
    )

    run_avg_incline = round(sum(run_incline_list) / len(run_incline_list), 1) if run_incline_list else None
    run_avg_dist    = round(sum(run_dist_list) / len(run_dist_list), 2) if run_dist_list else None

    # Strength — combines Peloton movement tracker + Garmin exercise sets
    str_qs = list(
        CachedWorkout.objects.for_user(user)
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
        CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_90d).values_list("created_at", flat=True)
    )
    all_timestamps_30d = list(
        CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_30d).values_list("created_at", flat=True)
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
        CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_30d)
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
    ts_7d    = list(CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_7d).values("created_at", "discipline"))
    ts_prior = list(CachedWorkout.objects.for_user(user).filter(created_at__gte=cutoff_14d, created_at__lt=cutoff_7d).values("created_at", "discipline"))
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
        "readiness_score", "body_battery_start", "training_load",
    ]
    # has_wellness_data isn't a DB field (it covers both the Garmin synced_at
    # stamp and Google Health's), so filter on the two underlying timestamps directly.
    _has_wellness = Q(synced_at__isnull=False) | Q(google_health_synced_at__isnull=False)
    recent_stats = list(
        DailyStats.objects.for_user(user).filter(_has_wellness, date__gte=today - datetime.timedelta(days=14))
        .order_by("date")
    )
    prior_stats = list(
        DailyStats.objects.for_user(user).filter(
            _has_wellness,
            date__gte=today - datetime.timedelta(days=28),
            date__lt=today - datetime.timedelta(days=14),
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
        "avg_readiness_score: 0-100, higher is better recovery. Comes from Garmin's own "
        "training-readiness algorithm when available; on days with only Google Health data "
        "it's FitPulse's own estimate from HRV/sleep/resting-HR vs. personal baseline instead "
        "(same 0-100 scale, roughly comparable, but don't treat it as Garmin's proprietary number). "
        "hrv_last_night in ms — higher is better recovery. "
        "resting_hr in bpm — lower is better recovery. "
        "training_load is Garmin acute load — higher means more recent training stress. "
        "Compare last_14_days vs prior_14_days to detect whether recovery is improving or declining."
    )

    # Nutrition adherence: last 30 days
    nutrition_stats = list(
        DailyStats.objects.for_user(user).filter(date__gte=today - datetime.timedelta(days=30))
        .values("date", "cal_total", "protein_g_total", "fiber_g_total")
        .order_by("date")
    )
    from workouts.nutrition import compute_macro_targets
    try:
        targets = compute_macro_targets(user)
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
def build_insights_system(user) -> str:
    """Build the analytics batch system prompt, personalised from AthleteProfile."""
    from .models import AthleteProfile
    profile = AthleteProfile.for_user(user)

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
        "if training_load is rising while hrv_last_night is falling or avg_readiness_score is "
        "declining, flag the imbalance. If recovery metrics are stable or improving alongside "
        "consistent training, call that out as a positive sign. "
        "current_training_status (e.g. 'maintaining', 'productive', 'overreaching') is Garmin's "
        "own assessment — use it as supporting context. "
        "When nutrition_last_30_days is present, connect fueling to training: low protein adherence "
        "on weeks with high strength volume is worth flagging; consistent calorie logging alongside "
        "training momentum is a positive habit worth reinforcing. "
    )

    parts.append(
        "ANALYSIS RULES — apply to every section: "
        "Rule 1 — Filler adjectives are banned; grounded interpretation is encouraged. "
        "Do not use filler adjectives not earned by an explicit comparison or threshold. "
        "Banned by default: 'solid,' 'good,' 'great,' 'encouraging,' 'excellent,' 'favorable,' "
        "'strong,' 'healthy,' 'nice,' 'impressive,' 'exceptional,' 'extremely,' 'genuinely.' "
        "Banned intensifier combinations: 'extremely high,' 'genuinely impressive,' "
        "'exceptional consistency,' 'solidly aerobic,' 'remarkably consistent.' "
        "If a metric is notable, name what makes it notable — what threshold it crossed, "
        "what value it improved from, what comparison anchors the claim. "
        "You may and should offer interpretation that names a cause, mechanism, or cross-section "
        "connection, AS LONG AS the interpretation is supported by the data. "
        "Interpretation like 'improving recovery markers while volume held steady is different from "
        "improvement during a deload' or 'this protein consistency directly supports the strength "
        "volume' is exactly the kind of synthesis this analysis exists for. "
        "Examples — banned: 'extremely high training frequency,' 'genuinely impressive recovery,' "
        "'solidly aerobic runs,' 'exceptional consistency in the rehab protocol.' "
        "Allowed: 'training 5.8 days per week — high relative to typical norms and up from 5.5 "
        "days/week 13 weeks ago'; 'recovery markers improved across all four signals while training "
        "volume held steady'; 'average pace of 15:38/mi at 1.2% incline, below typical lactate "
        "threshold pace, indicating aerobic-zone work'; 'Stretch Pectoral logged 79 times in 60 "
        "days indicates near-daily execution'; 'main risk is that a rigid 6-day pattern leaves "
        "little room to respond to fatigue signals' (interpretation, no filler). "
        "Rule 2 — No clinical or medical-advice framings. "
        "This is fitness data analysis, not a clinical assessment. "
        "Banned framings: 'healthy range,' 'lower end of a healthy range,' "
        "'absorbing this training load well,' 'well-tolerated,' 'is exactly right,' "
        "'progressing appropriately,' 'system is coping well,' 'concerning,' 'needs attention,' "
        "'appropriate level of.' "
        "Describe what the data shows. Comparisons to defined benchmarks are allowed and encouraged "
        "(e.g. 'cadence at 173.7 spm, above the 165 spm running-economy benchmark'). "
        "Comparisons to vague clinical norms ('within healthy range') are not. "
        "Forward-looking suggestions: use 'worth watching,' 'would be worth,' 'could extend,' "
        "'is the exercise with the most room to grow' — not 'should,' 'must,' 'needs to.' "
        "Rule 3 — Do not infer the user's attitudes, motivations, or mental states. "
        "The data shows what the user does, not how they feel about what they do. "
        "Do not write phrases like 'rather than treating them as optional,' 'despite the temptation "
        "to push harder,' 'given your commitment to consistency,' or similar inferred-intent framings. "
        "Describe behavior; do not project attitudes onto it. "
        "Exception: genuinely descriptive labels for observed behavior patterns are fine. "
        "'A well-grooved routine' describes an observed structural pattern — that is data-traceable "
        "and allowed. The test: can you point to the data row that supports the statement? "
        "If yes, allowed. If no, remove it."
    )

    parts.append(
        "Respond using ## section headers with paragraph text — no bullet points, no intro paragraph."
    )

    tone = coaching_tone_instruction(user)
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


def _submit_insights_batch(user):
    """Submit a new Anthropic batch for insights. Returns the batch ID."""
    summary = _build_insights_summary(user)
    prompt = (
        "Here is my workout data from Peloton and Garmin Connect for the past year:\n\n"
        + json.dumps(summary, indent=2)
        + INSIGHTS_PROMPT_SUFFIX
    )
    return llm.submit_batch("peloton-insights", prompt, user=user, feature="ai_training_insights", model=llm.SONNET, max_tokens=2000, system=build_insights_system(user))


def analytics_generate_insights(request):
    user = request.user
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return render_insights_partial(request, {
            "error": "ANTHROPIC_API_KEY is not set. Add it to your .env file and restart the server."
        })
    try:
        batch_id = _submit_insights_batch(user)
    except AI_UNAVAILABLE as e:
        return render_ai_unavailable(request, e)
    except Exception as e:
        return render_insights_partial(request, {"error": f"Failed to submit batch: {e}"})
    settings_obj = UserSettings.for_user(user)
    settings_obj.ai_insights_batch_id = batch_id
    settings_obj.save(update_fields=["ai_insights_batch_id"])
    return render_insights_partial(request, {"pending": True})


def analytics_check_insights(request):
    """Poll the Anthropic Batch API for insight results. Called via HTMX every 30s."""
    user = request.user
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    settings_obj = UserSettings.for_user(user)
    batch_id = settings_obj.ai_insights_batch_id

    if not batch_id:
        return render_insights_partial(request, {
            "insights": settings_obj.ai_insights,
            "generated_at": settings_obj.ai_insights_generated_at,
        }, status=286)

    if not api_key:
        settings_obj.ai_insights_batch_id = None
        settings_obj.save(update_fields=["ai_insights_batch_id"])
        return render_insights_partial(request, {"error": "ANTHROPIC_API_KEY is not set."}, status=286)

    # Check batch status — treat any transient error (including 429) as still-pending
    try:
        batch = llm.get_batch_status(batch_id)
    except Exception:
        return render_insights_partial(request, {"pending": True})

    if batch.get("processing_status") != "ended":
        return render_insights_partial(request, {"pending": True})

    # Batch complete — fetch results; treat 429 as transient (stay pending)
    try:
        insights_text = result_row = None
        for row in llm.get_batch_results(batch_id):
            if row.get("result", {}).get("type") == "succeeded":
                insights_text = llm.extract_text(row["result"]["message"]["content"])
                result_row = row
                break
    except Exception as e:
        logger.warning("batch results fetch failed for %s: %s", batch_id, e)
        return render_insights_partial(request, {"pending": True})

    if not insights_text:
        settings_obj.ai_insights_batch_id = None
        settings_obj.save(update_fields=["ai_insights_batch_id"])
        return render_insights_partial(request, {
            "error": "Batch completed but no successful result found."
        }, status=286)

    settings_obj.ai_insights = insights_text
    settings_obj.ai_insights_generated_at = tz.now()
    settings_obj.ai_insights_batch_id = None
    settings_obj.save(update_fields=["ai_insights", "ai_insights_generated_at", "ai_insights_batch_id"])
    llm.log_batch_result(result_row)

    return render_insights_partial(request, {
        "insights": insights_text,
        "generated_at": settings_obj.ai_insights_generated_at,
    }, status=286)


def render_insights_partial(request, context, status=200):
    """Thin wrapper so the insights endpoints don't need to import render."""
    return _render_poll_partial(request, "workouts/partials/insights.html", context, status=status)


# ---------------------------------------------------------------------------
# Day analysis — synchronous (Claude Haiku, cached 7 days)
# ---------------------------------------------------------------------------

def _get_or_generate_day_analysis(user, day, workouts, stats):
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
        if stats.readiness_score is not None:
            est = " — estimated from HRV/sleep/resting HR, Google Health only" if stats.readiness_is_computed else ""
            wellness_parts.append(f"Training readiness: {stats.readiness_score}/100 ({stats.readiness_label}){est}")
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
                profile = NutritionProfile.objects.filter(user=user).first()
                targets = compute_macro_targets(user, profile) if profile else None
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
                meals = list(FoodEntry.objects.for_user(user).filter(date=day).values_list("meal", "raw_text").order_by("logged_at"))
                if meals:
                    meal_strs = [f"{m or 'log'}: {t[:40]}" for m, t in meals[:4]]
                    nutrition_parts.append("Meals: " + "; ".join(meal_strs))
            except Exception:
                pass

        nutrition_section = ""
        if nutrition_parts:
            nutrition_section = "\n\nNUTRITION FOR THIS DAY\n" + "\n".join(nutrition_parts)

        intervention_context = _interventions_context(user, day, day)
        intervention_section = ""
        if intervention_context and "No tracked interventions" not in intervention_context:
            intervention_section = f"\n\nACTIVE INTERVENTIONS\n{intervention_context}"

        # Prior 7-day context — enables multi-day pattern observations
        prior_start = day - timedelta(days=7)
        prior_end = day - timedelta(days=1)
        prior_stats_qs = list(
            DailyStats.objects.for_user(user).filter(date__gte=prior_start, date__lte=prior_end)
            .order_by("date")
        )
        prior_workout_rows = list(
            CachedWorkout.objects.for_user(user).filter(
                created_at__date__gte=prior_start,
                created_at__date__lte=prior_end,
            ).order_by("created_at")
        )
        prior_workouts_by_date: dict = {}
        for w in prior_workout_rows:
            d = tz.localtime(w.created_at).date()
            prior_workouts_by_date.setdefault(d, []).append(w)

        all_prior_dates = sorted(
            set(s.date for s in prior_stats_qs) | set(prior_workouts_by_date.keys())
        )
        prior_stats_by_date = {s.date: s for s in prior_stats_qs}

        prior_lines = []
        for d in all_prior_dates:
            s = prior_stats_by_date.get(d)
            parts = []
            if s:
                if s.readiness_score is not None:
                    tag = " (est.)" if s.readiness_is_computed else ""
                    parts.append(f"readiness {s.readiness_score}{tag}")
                if s.hrv_last_night:
                    parts.append(f"HRV {s.hrv_last_night:.0f}")
                if s.sleep_score:
                    parts.append(f"sleep score {s.sleep_score}")
                if s.resting_hr:
                    parts.append(f"RHR {s.resting_hr}")
            day_wos = prior_workouts_by_date.get(d, [])
            if day_wos:
                wo_strs = []
                for w in day_wos[:3]:
                    wo_str = f"{w.discipline} {w.duration_minutes}min"
                    if w.title:
                        wo_str += f' "{w.title}"'
                    wo_strs.append(wo_str)
                if len(day_wos) > 3:
                    wo_strs.append("...")
                did_str = ", ".join(wo_strs)
            else:
                did_str = "rest"
            stats_str = ", ".join(parts) if parts else "no wellness data"
            prior_lines.append(f"{d.strftime('%a %Y-%m-%d')}: {stats_str} | did: {did_str}")

        prior_section = ""
        if prior_lines:
            prior_section = "\n\nPRIOR 7 DAYS\n" + "\n".join(prior_lines)

        today_note = " Do not comment on missing body battery end-of-day value — it is only recorded after sleep and is not available for the current day." if is_today else ""
        persona = build_persona_block(user, date_range=(day, day))
        persona_section = f"\n\nABOUT THIS PERSON\n{persona}" if persona else ""
        # Omit the whole section (don't even mention "recovery"/"wellness")
        # rather than a "no data" placeholder — a present-but-empty section,
        # or even a "not available" note, reads to the model as a gap worth
        # flagging, which is exactly the "commenting on absence" failure
        # mode we don't want (recovery data is legitimately unavailable on
        # some days depending on which wellness source synced that day).
        recovery_section = f"RECOVERY & READINESS\n{chr(10).join(wellness_parts)}\n\n" if wellness_parts else ""
        prompt = f"""Date: {day.strftime('%A, %B %-d, %Y')}

{recovery_section}{nutrition_section}{intervention_section}{prior_section}

WORKOUTS PERFORMED TODAY ({day.strftime('%B %-d, %Y')})
{chr(10).join(workout_lines)}
Note: workout titles may contain dates (e.g. "6/5/26") indicating when a routine was created — these are NOT the workout date. All workouts above were performed today.{persona_section}

Analyze how this person performed given their recovery state AND the trajectory of the last 7 days. Call out multi-day patterns when present — e.g. "third moderate-readiness day in a row", "first easy day after four consecutive training days", "HRV recovering from a dip earlier in the week." Single-day observations are still fine, but prefer pattern-level observations when the data supports them.

ANALYSIS RULES
1. Pattern threshold: A multi-day pattern requires at least 3 consecutive days moving in the same direction, OR the same metric staying in the same range (high/low/moderate) for at least 4 of the last 7 days. A single day's change from the previous day is not a pattern — it is a day-over-day change, and should be described as such.
2. Cite values for every pattern claim: When making any trend, pattern, or multi-day observation, you MUST include the actual sequence of values inline. Example: "readiness has been 65, 68, 71 over the last three days (rising)" — not "readiness has been climbing." If you cannot show the values that support the pattern, do not make the pattern claim.
3. No trend-inflation: Do not characterize a single day-over-day change as part of a longer trend unless the longer trend genuinely exists by Rule 1. Do not use phrases like "tracking a pattern of," "continues a trend of," "consistent with declining," or "second consecutive" to describe a single-day change. If the only signal is a one-day change, describe it as a one-day change.
4. Metrics to cite: pace, HR, effort score, HRV, nutrition, and whatever recovery metrics appear above under RECOVERY & READINESS (e.g. body battery, training readiness, training load — some days won't have all of these, depending on which device synced that day; only cite what's actually listed above, and never comment on a metric's absence). If nutrition data is present, note connections like "calories were 300 below target" or "low carb day may have affected energy". If intervention data is present, note relevant context — e.g. if a supplement was just started, acknowledge it's day 1 and effects won't be immediate; if a medication dose changed recently, note that.
5. Meal timing: Do not attribute intentional timing or purpose to logged meals. Do not call a meal a "pre-workout snack," "recovery meal," or similar unless the food name explicitly says so. Describe timing factually instead — e.g. "eaten 90 minutes before the strength session."
6. No inferred mental states: Do not speculate about the user's emotions, motivations, or what they "might have wanted to do." Stick to what the data shows. Do not characterize effort as "appropriate," "earned," "deserved," or similar — describe what happened, not whether it was the right choice.
7. Intervention hedging: When connecting a metric to a medication, supplement, or other intervention, hedge appropriately. Use "may," "could," or "is consistent with" unless the data shows a clear before/after change of ≥20% sustained over multiple days. Do not assert causation from a single day's data.
8. No commenting on missing data: If RECOVERY & READINESS is absent above, that means no wellness device synced data for this day — analyze the workouts on their own terms. Do not write anything like "no wellness data was available," "without recovery context," "readiness is unknown," or similar. Treat the absence as normal, not as a limitation worth mentioning.

{DAY_HEADLINE_BULLETS_FORMAT}{today_note}"""

        # max_tokens bumped to 400: prior-context enables multi-day pattern bullets
        # that tend to run slightly longer than single-day observations.
        # Sonnet, not Haiku: this cites specific workouts/days/metrics causally
        # (fatigue attribution, intervention effects) and is cached 24h/7d, so
        # the extra cost is negligible against the reliability gain.
        return llm.call(prompt, user=user, feature="ai_day_analysis", model=llm.SONNET, max_tokens=400)

    # If a workout was synced after the last analysis, force a refresh
    force_regen = False
    if workouts and stats.ai_day_generated_at:
        latest_synced = max(w.created_at for w in workouts)
        if latest_synced > stats.ai_day_generated_at:
            force_regen = True

    return cached_daily_stats_field(
        stats, "ai_day_analysis", cache_hours, _gen,
        stamp_field="ai_day_generated_at",
        force=force_regen,
    )


# ---------------------------------------------------------------------------
# Next-workout recommendation — synchronous (Claude Haiku, cached 24h)
# ---------------------------------------------------------------------------

def next_workout_refresh(request):
    """Clear the cached next-workout recommendation and regenerate it immediately."""
    user = request.user
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    today_stats, _ = DailyStats.objects.get_or_create(user=user, date=date.today())
    if today_stats.readiness_score is None:
        # No readiness signal yet (Garmin or Google Health) — nothing to base a rec on.
        return redirect("calendar")
    today_stats.ai_next_workout = None
    today_stats.ai_next_workout_generated_at = None
    today_stats.save(update_fields=["ai_next_workout", "ai_next_workout_generated_at"])
    try:
        _get_or_generate_next_workout(user, today_stats)
    except AI_UNAVAILABLE as e:
        from django.contrib import messages
        messages.info(request, ai_unavailable_reason(e))
    return redirect("calendar")


def _get_or_generate_next_workout(user, today_stats):
    """Return a cached next-workout recommendation or generate a fresh one."""
    def _gen():
        today_local = tz.localdate()
        cutoff = today_local - timedelta(days=14)
        recent_workouts = list(
            CachedWorkout.objects.for_user(user).filter(created_at__date__gte=cutoff)
            .order_by("-created_at")
        )

        recent_stats = list(
            DailyStats.objects.for_user(user).filter(date__gte=today_local - timedelta(days=7))
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
            pt_flag = rehab_flag_for(user, title)
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
            # Movement Tracker names when Peloton recorded them; otherwise the exercises
            # the user logged by hand, then the class's programmed exercises.
            names = [m["name"] for m in (w.movements or []) if m.get("name")]
            if not names:
                names = [r["name"] for r in (w.manual_movements_json or []) if r.get("name")]
            if not names:
                names = [e["name"] for seg in (w.class_plan_json or []) for e in seg.get("exercises", [])]
            names = list(dict.fromkeys(names))
            move_str = ""
            if names and not is_pt:
                move_str = "; exercises: " + ", ".join(names)

            workout_lines.append(
                f"- {day_label} {title} ({w.discipline}, {dur}min{hr}{eff}{muscle_str}{move_str}){pt_flag}"
            )

        stat_lines = []
        for s in recent_stats:
            parts = [str(s.date)]
            if s.readiness_score is not None:
                tag = " (est.)" if s.readiness_is_computed else ""
                parts.append(f"readiness {s.readiness_score}{tag}")
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
            if today_rec.readiness_score is not None:
                est = " — estimated from HRV trend + sleep, Google Health only, no Garmin training-readiness algorithm" if today_rec.readiness_is_computed else ""
                parts.append(f"Training readiness today: {today_rec.readiness_score}/100 ({today_rec.readiness_label}){est}")
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

        # Omit sections entirely rather than "No data" placeholders — a
        # present-but-empty section reads as a gap worth flagging, which is
        # exactly the "commenting on absence" failure mode to avoid (some
        # days/weeks legitimately have no wellness data depending on which
        # device synced, and that's normal, not a problem).
        today_signals_section = f"\n\nTODAY'S RECOVERY SIGNALS\n{today_context}" if today_context else ""
        wellness_trend_section = f"\n\nDAILY WELLNESS TREND (last 7 days)\n{chr(10).join(stat_lines)}" if stat_lines else ""

        persona = build_persona_block(user)
        prompt = f"""Today is {date.today().strftime('%A, %B %-d, %Y')}.
{today_workout_note}{today_signals_section}

LAST 14 DAYS OF WORKOUTS
{chr(10).join(workout_lines) if workout_lines else 'No recent workouts.'}{wellness_trend_section}

Based on this data, give a next-workout recommendation for {target_day}. {INTENSITY_ACTIVITY_REASON_FORMAT}

Note: not every day has a readiness score, and some days/weeks have no wellness data at all — some wellness sources don't compute every metric. Base the recommendation on whatever recovery signals and recent training load are actually shown above. Do not write anything like "no wellness data was available," "readiness is unknown," or similar — treat the absence as normal, not as a limitation worth mentioning.

IMPORTANT: Reference workouts only by the day and date exactly as listed in LAST 14 DAYS OF WORKOUTS above. Do not invent a session on a day that isn't listed, and do not restate the same workout under a different day — if only one strength session is listed, attribute fatigue to that single session and its actual date, not to a separate "day before" session that doesn't appear in the data.

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

        # Sonnet, not Haiku: cached 24h, and this attributes fatigue/readiness
        # to specific workouts and days — the exact class of causal claim that
        # was hallucinating a nonexistent session under Haiku.
        return llm.call(prompt, user=user, feature="ai_next_workout", model=llm.SONNET, max_tokens=350)

    return cached_daily_stats_field(today_stats, "ai_next_workout", 24, _gen)


# ---------------------------------------------------------------------------
# Compare page analysis
# ---------------------------------------------------------------------------

def compare_analysis(request):
    """HTMX endpoint — returns an HTML snippet comparing 2–4 workouts."""
    user = request.user
    ids = [i.strip() for i in request.GET.get("ids", "").split(",") if i.strip()][:4]
    workouts = list(CachedWorkout.objects.for_user(user).filter(workout_id__in=ids).order_by("created_at"))
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

        # Hand-entered exercise log (classes Movement Tracker didn't record)
        manual = w.manual_log_summary
        if manual and not w.movements:
            lines.append(
                f"  logged by hand: {manual['total_sets']} sets, {manual['total_reps']} reps, "
                f"{manual['volume_lb']:,} lb volume"
            )
            lines.append("  exercises (hand-logged):")
            for r in w.manual_log_rows:
                unit = "s" if r["timed"] else " reps"
                side = "/side" if r["per_side"] else ""
                pair = "2×" if r["dumbbells"] == 2 else ""
                wt = f" @ {pair}{r['weight_lb']:g} lb" if r.get("weight_lb") else ""
                lines.append(f"    {r.get('name')}: {r.get('sets') or '?'} sets × {r.get('reps') or '?'}{unit}{side}{wt}")

        return "\n".join(lines)

    workout_blocks = "\n\n".join(f"WORKOUT {i+1}:\n{_stat(w)}" for i, w in enumerate(workouts))
    persona = build_persona_block(user)
    persona_rule = f"\n8. {persona}" if persona else ""

    prompt = f"""You are analyzing {len(workouts)} Peloton and/or Garmin workouts being compared side by side.

{workout_blocks}

Provide a brief, insightful comparison. {HEADLINE_BULLETS_FORMAT}

Rules:
1. INCLINE/ELEVATION FIRST — MANDATORY: Before drawing any conclusion about HR, efficiency, or fatigue, check whether avg incline or max incline differs between runs. A higher avg incline directly raises HR and slows pace — this is physics, not fitness decline. If inclines differ, the first bullet MUST address this and quantify the impact. Do NOT attribute HR differences to fatigue or mechanics if incline explains it.
2. No developmental claims: You are comparing {len(workouts)} specific workouts. This is not a fitness assessment, a training-adaptation analysis, or a progress check. Do not infer developmental progress, base-building, improved fitness, stronger aerobic capacity, or training adaptation from comparing these workouts — two or three workouts is not enough data to support those claims. You MAY describe how the workouts differ ("this run had lower HR at higher output") and speculate about likely causes (terrain, effort, recovery state, weather, time of day). Banned phrases: "a hallmark of," "stronger aerobic base," "base-building progress," "endurance gains," "improving fitness," "showing development," "indicates training adaptation."
3. Mechanically-derived metrics are restatements, not independent evidence: Stride length × cadence ≈ speed — at identical cadence, longer stride and faster pace are the same observation in different units, not two separate signals. Output (kJ) scales with intensity × duration — do not cite output and pace/HR as independent signals if duration was the same. Calorie burn tracks HR × duration — do not cite calorie differences as separate evidence when HR and duration have already been cited. At identical duration, output (kJ) and distance are the same observation in different units: more distance in the same time means more work done. Do not cite output and distance as independent evidence for the same claim when the workouts had the same duration — you may note the relationship when it is the point (e.g. "the extra distance translated directly to higher output"), but do not stack them. You MAY comment on any mechanical relationship when it IS the point (e.g. "the speed gain came from longer strides at unchanged cadence, not faster turnover").
4. Calibrate causal language to the magnitude of the confound: When attributing a performance difference to an environmental confound (incline, weather, terrain, elevation), match the strength of your causal language to how large the confound is relative to the observed effect. Use "directly explains" or "fully accounts for" ONLY when the confound's magnitude is large enough to plausibly account for the full observed difference — a 0.2 percentage-point incline drop does NOT directly explain a 6.8% pace gain; a 4 percentage-point drop could plausibly explain a substantial pace change. Use "partially explains" or "contributes to" when the confound is in the right direction but smaller than the observed effect, leaving a residual. Use "is consistent with" or "aligns with" when direction matches but magnitude is unclear. Before using strong causal language, ask: is the confound large enough to plausibly cause the entire observed difference? If not, use a weaker frame and acknowledge the residual.
5. Cite values, ban inflation language: For any comparative claim, include the actual values inline — e.g. "pace improved from 16:05 to 15:02" not "pace improved significantly." If you cannot show the values supporting a claim, do not make the claim. Do not use phrases like "a hallmark of," "clear sign of," "indicates a stronger," or "showing improved fitness" to characterize a two-workout comparison. Describe what happened in the specific workouts being compared — stop there.
6. Keep each bullet to one to two sentences and reference actual numbers.
7. Focus on what's interesting or actionable — effort vs output tradeoffs, HR efficiency, pacing strategy, incline-adjusted performance, cross-discipline comparisons.{persona_rule}"""

    try:
        text = llm.call(prompt, user=user, feature="ai_training_insights", model=llm.HAIKU, max_tokens=450)
        return _render_compare_analysis_html(text, ids_param=request.GET.get("ids", ""))
    except AI_UNAVAILABLE as e:
        return render_ai_unavailable(request, e)
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

def _get_or_generate_body_commentary(user, force=False) -> str:
    """Daily Haiku commentary on body composition trends. Cached 24h in UserSettings."""
    def _gen():
        from datetime import date as date_cls
        today = date_cls.today()
        cutoff_30d = today - timedelta(days=30)
        cutoff_7d  = today - timedelta(days=7)

        stats_30d = list(
            DailyStats.objects.for_user(user).filter(date__gte=cutoff_30d, date__lte=today)
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

        # 7-day recovery averages — only include metrics that actually have
        # data this week (sleep score and body battery have no Google Health
        # equivalent, so a Google-Health-only week legitimately has neither).
        avg_hrv = _avg([s.hrv_last_night for s in stats_7d])
        avg_rhr  = _avg([s.resting_hr for s in stats_7d])
        avg_sleep = _avg([s.sleep_score for s in stats_7d])
        avg_bb    = _avg([s.body_battery_high for s in stats_7d])
        recovery_parts = []
        if avg_hrv is not None:
            recovery_parts.append(f"HRV: {avg_hrv} ms")
        if avg_rhr is not None:
            recovery_parts.append(f"Resting HR: {avg_rhr} bpm")
        if avg_sleep is not None:
            recovery_parts.append(f"Sleep score: {avg_sleep}")
        if avg_bb is not None:
            recovery_parts.append(f"Body battery high: {avg_bb}")
        # Omit the whole section rather than a "no data" placeholder — same
        # reasoning as _get_or_generate_day_analysis's recovery_section: a
        # present-but-empty section reads as a gap worth flagging.
        recovery_section = f"\n\n7-DAY RECOVERY AVERAGES\n{' | '.join(recovery_parts)}" if recovery_parts else ""

        # Interventions context
        iv_ctx = _interventions_context(user, cutoff_30d, today)

        # Nutrition 7-day context
        nutrition_section = ""
        try:
            from .nutrition import compute_macro_targets
            from .models import NutritionProfile
            profile = NutritionProfile.objects.filter(user=user).first()
            targets = compute_macro_targets(user, profile) if profile else None
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
{chr(10).join(weight_series) if weight_series else 'No data.'}{nutrition_section}{recovery_section}

ACTIVE INTERVENTIONS
{iv_ctx}

ANALYSIS RULES
A. No subjective-effect fabrication: The intervention list shows what medications, supplements, or protocols are being taken. It does NOT show how the user is responding subjectively. Do not claim or infer that any intervention has produced mood changes, anxiety changes, energy changes, mental clarity improvements, tolerability signals, or effectiveness ("coping well," "well-tolerated," "appears to be working," "is helping with X") — none of those are in the data. You may note that an intervention's start date or dose change aligns in time with an observed objective change (weight, HRV, sleep score) — hedged as a possible mechanism, never asserted as causation. Banned phrases: "since starting X you've experienced Y," "X is helping with Y," "your system is coping well," "the medication appears to be working," "well-tolerated."
B. Filler adjectives are banned; grounded interpretation is encouraged: Do not use filler adjectives not earned by an explicit comparison or threshold. Banned: "solid," "good," "great," "encouraging," "excellent," "favorable," "strong," "healthy," "nice," "impressive." If a metric is notable, name what makes it notable — the value it changed from, the threshold it crossed, or the target it hit. You may and should offer interpretation that names a likely cause, mechanism, or context for what the data shows, AS LONG AS the interpretation is supported by the data in the prompt. Interpretation that names mechanisms ("likely water retention rather than real fat gain," "consistent with a sustained caloric deficit," "matches the timing of the dose increase") is valuable and should appear when the data supports it. The test: can you point to the specific data in the prompt that supports the interpretation? If yes, include it. If you are reaching for an interpretation to fill space, leave it out and just describe the data. Examples — banned: "solid weight loss," "excellent HRV," "healthy plateau." Allowed: "weight loss of 3.4 lb, consistent with a sustained caloric deficit"; "this 0.8 lb weekly gain is small enough to likely reflect water retention rather than real fat gain"; "HRV at 34 ms, above the recent baseline of 28 ms"; "weight has held in a narrow band for 10 days, suggesting the recent loss has stalled."
C. No clinical framings or directives: Do not use clinical assessments or soft directives. Banned: "monitor closely," "healthy plateau," "your system is coping," "well-tolerated," "appears to be working," "concerning," "needs attention." Reframe directives as observations: "worth watching whether..." instead of "monitor closely." Reframe assessments as data: "weight has held in the X–Y lb range for N days" instead of "healthy plateau."
D. No commenting on missing data: If 7-DAY RECOVERY AVERAGES is absent above, that means no wellness device synced recovery data this week — write the ## Body Composition and ## To Watch sections and omit ## Recovery entirely. Do not write anything like "no recovery data was available," "HRV wasn't tracked this week," or similar. Treat the absence as normal, not as a limitation worth mentioning.

Write the commentary in exactly this structure. Each section: 1-2 sentences of flowing text, no bullet points. Use **bold** for specific numbers only — not for qualitative assessments.

## Body Composition
Weight and fat/lean mass changes over 7 and 30 days, referencing the actual values.

## Recovery
Only include this section if 7-DAY RECOVERY AVERAGES is present above. Discuss whatever metrics are listed there, referencing the actual values — different weeks may have a different subset available depending on which device synced that week.

## To Watch
The single most objective signal worth noting — a continued trend, a stall, or a gap between expected and observed. If an intervention's start date or dose change aligns with an objective change in the data above, note the timing and hedge it (e.g. "weight dropped 1.2 lb in the week after the dose increase — possibly related"). If nutrition data is present, connect calorie or protein intake to the body composition data. No directives."""

        return llm.call(prompt, user=user, feature="ai_body_commentary", model=llm.HAIKU, max_tokens=500)

    return cached_settings_field(user, "ai_body_commentary", 24, _gen, force=force)


def body_commentary_refresh(request):
    """POST /api/body/commentary/refresh/ — force-regenerate body commentary."""
    user = request.user
    from django.http import JsonResponse
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        _get_or_generate_body_commentary(user, force=True)
    except AI_UNAVAILABLE as e:
        return JsonResponse({"ok": False, "error": ai_unavailable_reason(e)})
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
    user,
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
    image_b64: base64-encoded photo of a nutrition label OR of food; the model classifies which (optional)
    serving_note: user's quantity qualifier, e.g. "I had the whole bag" or "half"
    Returns {"ok": True, "model": ..., "items": [...], "meal_guess": ..., "confidence": ..., "note": ...}
    (image parses also carry "image_type": "label" | "meal" | "not_food")
    or {"ok": False, "error": ..., "items": [], "model": ...}
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
        # ── Image path: model classifies label vs. meal vs. not_food ─────
        serving_line = f'\nUSER QUANTITY NOTE: "{serving_note}"' if serving_note else ""
        extra_text = f'\nADDITIONAL CONTEXT FROM USER: "{raw_text}"' if raw_text.strip() else ""

        image_json_schema = """{
  "image_type": "meal",
  "items": [
    {"name": "grilled chicken breast", "quantity": "~5 oz", "calories": 230, "protein_g": 43, "carbs_g": 0, "fat_g": 5, "fiber_g": 0}
  ],
  "meal_guess": "dinner",
  "confidence": "medium",
  "note": "Assumed 1 tsp oil on the chicken; rice portion estimated from plate size."
}"""

        prompt = f"""You are a nutrition parser for a food photo. First decide what the image shows, then follow the matching rules.

STEP 1 — CLASSIFY the image as exactly one of:
- "label": a Nutrition Facts / nutrition information panel is the main subject and its numbers are legible.
- "meal": prepared food, a plate, bowl, snack, or drink, OR packaged food whose nutrition panel is not visible or not legible.
- "not_food": anything else.
If a legible nutrition panel AND food are both visible, use "label" and the panel's values.
If only the front of a package is visible, use "meal" and identify the product by the name printed on it.

STEP 2a — IF "label":
1. COLUMN PRIORITY: If the label has multiple columns (e.g. "as packaged" vs "as prepared", "unpopped" vs "popped", "dry" vs "cooked"), always use the "as prepared" or "ready-to-eat" column.
2. SERVING SIZE LOGIC:
   - Default to the serving size printed on the label (1 serving).
   - "I had all of this" / "the whole thing/bag/package/container" → multiply all values by the number of servings per container.
   - "half" → multiply by 0.5. "two servings" → multiply by 2. Fractional descriptions (e.g. "about a third") → apply that multiplier.
   - If the user specifies a weight or volume that differs from the label serving, scale proportionally.
3. SPECIAL NOTATIONS: Interpret any %, DV, added sugars, trans fat asterisks, and ingredient callouts naturally.
4. AMBIGUITY: If part of the label is cut off or unclear, extract what you can and set confidence to "low" or "medium" with a note explaining what was unclear.
- Use the label values directly — do not substitute estimates from training knowledge when the label is readable.
- Confidence "high" if the panel is clear and fully visible, "medium" if partially visible, "low" if very unclear.

STEP 2b — IF "meal":
1. ITEMS: list each distinct food or drink as its own item. Split visible sides, sauces, and drinks. Keep a dish as one item only when its components can't be separated visually (e.g. a burrito, a casserole, a smoothie).
2. QUANTITY: put your portion assumption in "quantity" using household measures or weight, prefixed with "~" (e.g. "~1 cup", "~6 oz", "1 medium"). Judge scale from visible references: plate or bowl size (assume a 10–11 inch dinner plate unless it is clearly smaller), utensils, hands, cans, cups.
3. HIDDEN FAT: for foods that look fried, sautéed, roasted, buttered, or glossy, include a typical amount of cooking fat in that item's values. List dressing or sauce as its own item only when it is visibly separate. State the fat assumption in "note".
4. USER CONTEXT WINS: a portion, brand, ingredient, or preparation method in the user quantity note or additional context overrides your visual estimate.
5. CONFIDENCE: at most "medium" from a photo alone. "high" only when the user context supplies portions for the main items. "low" when the food is partly hidden, a mixed dish with unknown ingredients, or there is no scale reference.
6. NAMES: generic food names. Use a brand only if it is legible in the photo or given by the user.

STEP 2c — IF "not_food": return "items": [] and a "note" saying what the image appears to show.
{serving_line}{extra_text}
MEAL CONTEXT: {meal_context}

Respond with ONLY valid JSON, no markdown fences:
{image_json_schema}

Rules for every image type:
- Round all numbers to whole integers. Missing nutrients are 0.
- "note" is one sentence stating the main assumption or what was unclear. No adjectives like "healthy", "delicious", "balanced"; no advice."""

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
        # Photo portion estimation is the hardest vision task in the app and
        # classification happens inside the call, so every image parse uses Sonnet.
        model, max_tokens, timeout = llm.SONNET, 900, 45
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
        model, max_tokens, timeout = llm.HAIKU, 600, 30

    try:
        result = llm.call_json(prompt, user=user, feature="ai_food_parse", model=model, max_tokens=max_tokens,
                               message_content=message_content, timeout=timeout)
        result["ok"] = True
        result["model"] = model
        if image_b64 and not result.get("image_type"):
            result["image_type"] = "label"  # pre-classification behavior
        return result
    except AI_UNAVAILABLE:
        raise
    except json.JSONDecodeError as e:
        logger.warning("parse_food_text JSON decode failed: %s", e)
        return {"ok": False, "error": "parse_failed", "items": [], "confidence": "low", "note": "", "model": model}
    except Exception as e:
        logger.warning("parse_food_text failed: %s", e)
        return {"ok": False, "error": str(e), "items": [], "confidence": "low", "note": "", "model": model}


def parse_plan_skeleton(user, raw_text: str = "", image_b64: str | None = None, image_media_type: str = "image/jpeg") -> dict:
    """
    Parse a multi-week workout-plan schedule — pasted text (optionally containing
    class links) and/or a screenshot of a plan tracker page/PDF — into a
    structured week/day/slot skeleton for the "New Plan" builder.

    Returns {"ok": True, "plan_name_guess", "instructor_guess", "items": [
      {"week", "day", "order", "title", "discipline", "duration_min", "optional",
       "source_url"}, ...], "note"} or {"ok": False, "error": ..., "items": []}.
    Ride-id resolution happens separately in workouts/programs.py — this only
    extracts structure and, where present, per-item source links.
    """
    if not raw_text.strip() and not image_b64:
        return {"ok": False, "error": "no_input", "items": []}

    json_schema = """{
  "plan_name_guess": "Glutes and Legs Strength Program",
  "instructor_guess": "Adrian Williams",
  "items": [
    {"week": 1, "day": 1, "order": 0, "title": "20 min Power & Performance Benchmark", "discipline": "strength", "duration_min": 20, "optional": false, "source_url": "", "any_class": false, "class_type": ""},
    {"week": 1, "day": 3, "order": 0, "title": "Pilates (any class)", "discipline": "strength", "duration_min": null, "optional": false, "source_url": "", "any_class": true, "class_type": "pilates"}
  ],
  "note": "optional one-line note about anything ambiguous or skipped"
}"""

    rules = f"""Extract every class/session listed into "items", one row per class, in the order they appear.

RULES:
- "week" and "day" are 1-indexed integers from the schedule structure (e.g. "Week 1", "Day 2"). If the source has no explicit week grouping, use week 1 for everything and infer "day" from the day/session labels.
- "order" is the position of this class within its day (0 for the first class listed under that day, 1 for the second, etc).
- "title" is the class title only — strip the instructor name and date/time (e.g. "20 min Power & Performance Benchmark with Adrian Williams – 2023/12/18 @ 5:00am ET" -> "20 min Power & Performance Benchmark"). Keep a leading duration if present.
- "discipline" is your best guess from: cycling, running, walking, strength, stretching, cardio, yoga, meditation. Stretches/foam rolling/mobility -> "stretching". Warm ups/benchmarks/strength work -> "strength" unless clearly another discipline.
- "duration_min" is the integer minutes from the title if present, else your best guess, else null.
- "optional" is true only if the source explicitly marks it optional/bonus — default false.
- "source_url" is any URL directly associated with this specific class line (e.g. it was hyperlinked, or a plain https://... URL sits adjacent to it in the pasted text). Empty string if none.
- "plan_name_guess" / "instructor_guess": best guess for the overall plan name and lead instructor from context.
- If a title repeats verbatim on a different day, keep both as SEPARATE items — never merge or dedupe.
- Some days are open-ended: "Pilates (any class)", "Yoga", "your choice of stretch", "walk". For those set "any_class": true and "class_type" to one of: pilates, yoga, stretching, walking, running, cycling, strength, cardio, circuit, meditation. Write "title" as the plain label (e.g. "Pilates (any class)"), leave "source_url" empty, and "duration_min" null unless a length is given. For a specific named class (with a duration, a series, or an instructor) set "any_class": false and "class_type": "".
- Exercise details listed UNDER a class are NOT separate items — supersets, sets/reps ("x3", "30 secs each side"), movement names (e.g. "Chest Press", "Bear Crawl 45 sec"). Ignore them entirely; only extract the classes/sessions themselves.

Respond with ONLY valid JSON, no markdown fences, no preamble:
{json_schema}"""

    if image_b64:
        extra_text = f'\n\nADDITIONAL TEXT PROVIDED BY USER (may include class links or corrections):\n"""{raw_text}"""' if raw_text.strip() else ""
        prompt = f"""You are extracting a structured workout-plan schedule from a screenshot of a plan tracker page. Read every visible class entry in the image, in order.{extra_text}

{rules}"""
        message_content = [
            {"type": "image", "source": {"type": "base64", "media_type": image_media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]
    else:
        prompt = f"""You are extracting a structured workout-plan schedule from pasted text (copied from a plan tracker page or PDF, possibly including class links).

SOURCE TEXT:
\"\"\"{raw_text}\"\"\"

{rules}"""
        message_content = prompt

    try:
        result = llm.call_json(prompt, user=user, feature="ai_program_tools", model=llm.HAIKU, max_tokens=4000,
                               message_content=message_content, timeout=45)
        result["ok"] = True
        result.setdefault("items", [])
        return result
    except AI_UNAVAILABLE:
        raise
    except json.JSONDecodeError as e:
        logger.warning("parse_plan_skeleton JSON decode failed: %s", e)
        return {"ok": False, "error": "parse_failed", "items": []}
    except Exception as e:
        logger.warning("parse_plan_skeleton failed: %s", e)
        return {"ok": False, "error": str(e), "items": []}


def suggest_meals(
    user,
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
    _profile = NutritionProfile.objects.filter(user=user).first()
    _targets = compute_macro_targets(user, _profile) if _profile else None
    top_gap = _macro_priority_hint(remaining_protein, remaining_carbs, remaining_fiber, _targets)

    persona = build_persona_block(user)
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
        data = llm.call_json(prompt, user=user, feature="ai_meal_suggest", model=llm.HAIKU, max_tokens=900, timeout=30)
        if gi_note_str:
            data["gi_note"] = gi_note_str
        return data
    except AI_UNAVAILABLE:
        raise
    except Exception as e:
        logger.warning("suggest_meals failed: %s", e)
        return {"suggestions": [], "tip": "", "gi_note": ""}


# ---------------------------------------------------------------------------
# Nutrition analytics insights — synchronous (Claude Sonnet, weekly cache)
# ---------------------------------------------------------------------------

def _build_nutrition_insights_prompt(user, range_days: int = 30) -> str | None:
    """Build the nutrition insights prompt. Returns None if no data logged."""
    from datetime import date as date_cls
    from .nutrition import compute_macro_targets, get_top_foods
    from .models import NutritionProfile

    today = date_cls.today()
    start = today - timedelta(days=range_days - 1)

    profile = NutritionProfile.objects.filter(user=user).first()
    targets = compute_macro_targets(user, profile) if profile else None

    stats_qs = list(
        DailyStats.objects.for_user(user).filter(date__gte=start, date__lte=today, cal_total__isnull=False)
        .order_by("date")
    )
    total_days = range_days
    logged = len(stats_qs)

    if not logged:
        return None

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

    days_hit_cal = _days_hit("cal_total", cal_t, pct=1.0)
    if cal_t:
        days_hit_cal = sum(1 for s in stats_qs if s.cal_total and s.cal_total <= cal_t * 1.1)
    days_hit_prot = _days_hit("protein_g_total", prot_t)
    days_hit_fiber = _days_hit("fiber_g_total", fiber_t, pct=0.85)

    def _wd_we_avg(field):
        weekday_vals = [getattr(s, field) for s in stats_qs
                        if s.date.weekday() < 5 and getattr(s, field)]
        weekend_vals = [getattr(s, field) for s in stats_qs
                        if s.date.weekday() >= 5 and getattr(s, field)]
        wd = round(sum(weekday_vals) / len(weekday_vals)) if weekday_vals else None
        we = round(sum(weekend_vals) / len(weekend_vals)) if weekend_vals else None
        return wd, we

    wd_cal, we_cal = _wd_we_avg("cal_total")
    wd_prot, we_prot = _wd_we_avg("protein_g_total")
    wd_fiber, we_fiber = _wd_we_avg("fiber_g_total")

    weight_stats = list(
        DailyStats.objects.for_user(user).filter(date__gte=start, date__lte=today, weight_lb__isnull=False)
        .order_by("date")
    )
    weight_start = weight_stats[0].weight_lb if weight_stats else None
    weight_end = weight_stats[-1].weight_lb if weight_stats else None
    if weight_start and weight_end:
        weight_delta = round(weight_end - weight_start, 1)
        trend_desc = f"{weight_start:.1f}lb → {weight_end:.1f}lb ({weight_delta:+.1f}lb)"
    else:
        trend_desc = "No weight data"

    top_foods = get_top_foods(user, start, today, top_n=5)
    top_food_lines = "\n".join(
        f"  - {f['name']} (logged {f['count']}x, avg {f['avg_calories']:.0f} kcal, {f['avg_protein_g']:.0f}g P)"
        for f in top_foods
    ) if top_foods else "  No data"

    iv_ctx = _interventions_context(user, start, today)

    targets_section = ""
    if targets:
        targets_section = f"""TARGETS:
- Calories: {cal_t or 'not set'}
- Protein: {f'{prot_t}g' if prot_t else 'not set'}
- Carbs: {f'{carbs_t}g' if carbs_t else 'not set'}
- Fat: {f'{fat_t}g' if fat_t else 'not set'}
- Fiber: {f'{fiber_t}g' if fiber_t else 'not set'}

"""

    persona = build_persona_block(user, date_range=(start, today))
    tone = coaching_tone_instruction(user)
    persona_section = f"\n{persona}" if persona else ""
    tone_section = f"\n{tone}" if tone else ""

    return f"""You are analyzing nutrition tracking data.{persona_section}{tone_section}

{targets_section}LAST {range_days} DAYS (logged {logged}/{total_days} days):
- Avg calories: {avg_cal or 'n/a'}{f' (hit target {days_hit_cal}/{logged} days)' if days_hit_cal is not None else ''}
- Avg protein: {avg_prot or 'n/a'}g{f' (hit target {days_hit_prot}/{logged} days)' if days_hit_prot is not None else ''}
- Avg fiber: {avg_fiber or 'n/a'}g{f' (hit target {days_hit_fiber}/{logged} days)' if days_hit_fiber is not None else ''}

WEEKDAY VS WEEKEND (averages where data exists):
- Calories: weekday {f'{wd_cal} kcal' if wd_cal else 'not enough data'}, weekend {f'{we_cal} kcal' if we_cal else 'not enough data'}
- Protein: weekday {f'{wd_prot}g' if wd_prot else 'not enough data'}, weekend {f'{we_prot}g' if we_prot else 'not enough data'}
- Fiber: weekday {f'{wd_fiber}g' if wd_fiber else 'not enough data'}, weekend {f'{we_fiber}g' if we_fiber else 'not enough data'}

TOP FOODS (most logged):
{top_food_lines}

WEIGHT TREND OVER SAME PERIOD:
{trend_desc}

ACTIVE INTERVENTIONS:
{iv_ctx}

ANALYSIS RULES
1. Filler adjectives are banned; grounded interpretation is encouraged: Do not use filler adjectives not earned by an explicit comparison or threshold. Banned: "solid," "good," "great," "encouraging," "excellent," "favorable," "strong," "healthy," "nice," "impressive." If a metric is notable, name what makes it notable — the value it changed from, the threshold it crossed, or the target it hit. You may and should offer interpretation that names a likely cause, mechanism, or context for what the data shows, AS LONG AS the interpretation is supported by the data in the prompt. Interpretation that names mechanisms ("consistent with a sustained caloric deficit," "matches the timing of the dose increase") is valuable and should appear when the data supports it. Interpretive framings like "essentially a solved problem at 130g average with 23/24 days hitting target" or "the weakest pillar" are allowed — they describe a behavioral pattern, not a filler adjective. The test: can you point to the specific data in the prompt that supports the interpretation? If yes, include it. If you are reaching to fill space, describe the data and stop. Examples — banned: "strong result," "solid calorie adherence." Allowed: "5.7lb loss over 30 days, consistent with sustained adherence"; "21/24 days within calorie target."
2. Cite the data or hedge harder: When making any claim about a pattern (weekend vs weekday, day-of-week, meal-to-meal, food-specific), cite the actual numbers from the data block. If the data block contains the breakdown, use it. If it does NOT contain the breakdown needed to support the claim, convert the assertion into a hypothesis worth checking. Examples — GOOD: "Weekend calorie average of 2,077 vs 1,856 weekdays is a 221-calorie gap." GOOD (acknowledges data gap): "Worth checking whether weekend fiber also runs lower than weekday fiber." BAD: "Weekends likely also see the fiber dip." BAD: "Dinner appears to be the highest-calorie meal" (no meal-level breakdown in this data). Apply this to any claim where the supporting data could exist but isn't shown in the prompt.
3. No subjective intervention effects: The intervention list shows what medications or supplements are taken — not how the user is responding subjectively. Do not claim a medication has produced mood changes, anxiety changes, energy changes, mental clarity, tolerability ("coping well," "well-tolerated"), or effectiveness ("appears to be working"). You MAY note that a dose change aligns in time with an observed objective change (weight, calorie pattern), hedged as a possible mechanism. You MAY use generic population-level framing ("some people experience appetite changes during SSRI dose increases") to frame a future-observation note — this differs from claiming a specific effect on this user.

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


def _submit_nutrition_insights_batch(user, range_days: int = 30) -> str:
    """Submit nutrition insights to Batch API. Saves batch_id to UserSettings. Returns batch_id."""
    prompt = _build_nutrition_insights_prompt(user, range_days)
    if not prompt:
        raise ValueError("No nutrition data logged for the selected period")
    batch_id = llm.submit_batch("nutrition_insights", prompt, user=user, feature="ai_nutrition_insights", model=llm.SONNET, max_tokens=1800)
    settings = UserSettings.for_user(user)
    settings.ai_nutrition_insights_batch_id = batch_id
    settings.ai_nutrition_insights_range = range_days
    settings.save(update_fields=["ai_nutrition_insights_batch_id", "ai_nutrition_insights_range"])
    return batch_id


def render_nutrition_insights_partial(request, context, status=200):
    return _render_poll_partial(request, "workouts/partials/nutrition_insights.html", context, status=status)


def nutrition_insights_check(request):
    """HTMX poll — check nutrition insights batch status, return rendered HTML partial."""
    user = request.user
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    settings = UserSettings.for_user(user)
    batch_id = settings.ai_nutrition_insights_batch_id

    if not batch_id:
        return render_nutrition_insights_partial(request, {
            "insights": settings.ai_nutrition_insights,
            "generated_at": settings.ai_nutrition_insights_generated_at,
        }, status=286)

    if not api_key:
        settings.ai_nutrition_insights_batch_id = None
        settings.save(update_fields=["ai_nutrition_insights_batch_id"])
        return render_nutrition_insights_partial(request, {"error": "ANTHROPIC_API_KEY is not set."}, status=286)

    try:
        batch = llm.get_batch_status(batch_id)
    except Exception:
        return render_nutrition_insights_partial(request, {"pending": True})

    if batch.get("processing_status") != "ended":
        return render_nutrition_insights_partial(request, {"pending": True})

    try:
        insights_text = result_row = None
        for row in llm.get_batch_results(batch_id):
            if row.get("result", {}).get("type") == "succeeded":
                insights_text = llm.extract_text(row["result"]["message"]["content"])
                result_row = row
                break
    except Exception as e:
        logger.warning("nutrition insights batch results failed for %s: %s", batch_id, e)
        return render_nutrition_insights_partial(request, {"pending": True})

    if not insights_text:
        settings.ai_nutrition_insights_batch_id = None
        settings.save(update_fields=["ai_nutrition_insights_batch_id"])
        return render_nutrition_insights_partial(request, {
            "error": "Batch completed but no successful result found."
        }, status=286)

    settings.ai_nutrition_insights = insights_text
    settings.ai_nutrition_insights_generated_at = tz.now()
    settings.ai_nutrition_insights_batch_id = None
    settings.save(update_fields=[
        "ai_nutrition_insights", "ai_nutrition_insights_generated_at", "ai_nutrition_insights_batch_id",
    ])
    llm.log_batch_result(result_row)
    return render_nutrition_insights_partial(request, {
        "insights": insights_text,
        "generated_at": settings.ai_nutrition_insights_generated_at,
    }, status=286)


def nutrition_insights_refresh(request):
    """POST /api/nutrition/insights/refresh/ — submit new batch, return pending partial."""
    user = request.user
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    range_param = request.GET.get("range", "30d")
    range_days = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(range_param, 30)
    try:
        _submit_nutrition_insights_batch(user, range_days=range_days)
    except AI_UNAVAILABLE as e:
        return render_ai_unavailable(request, e)
    except Exception as e:
        return render_nutrition_insights_partial(request, {"error": f"Failed to submit batch: {e}"})
    return render_nutrition_insights_partial(request, {"pending": True})


# ---------------------------------------------------------------------------
# Intervention analysis interpretation — synchronous (Claude Sonnet)
# ---------------------------------------------------------------------------

def _generate_intervention_interpretation(
    user,
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

        return llm.call(prompt, user=user, feature="ai_intervention_interpretation", model=llm.SONNET, max_tokens=1600, timeout=60)
    except Exception as e:
        logger.warning("Intervention interpretation failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Pattern insights — weekly Sonnet deep analysis (Phase 3)
# ---------------------------------------------------------------------------

def _build_pattern_insights_prompt(user) -> str:
    """Build the pattern insights prompt from the last 60 days of data."""
    from datetime import date as date_cls, datetime as datetime_cls
    today = date_cls.today()
    cutoff_60 = today - timedelta(days=60)

    def _as_date(v):
        return v.date() if isinstance(v, datetime_cls) else v

    weight_rows = list(
        DailyStats.objects.for_user(user).filter(date__gte=cutoff_60, date__lte=today, weight_lb__isnull=False)
        .order_by("date").values_list("date", "weight_lb", "fat_ratio_pct", "muscle_mass_lb")
    )
    weight_lines = [
        f"  {d}: {w:.1f} lb"
        + (f", {f:.1f}% fat" if f else "")
        + (f", {m:.1f} lb muscle" if m else "")
        for d, w, f, m in weight_rows
    ]

    recovery_rows = list(
        DailyStats.objects.for_user(user).filter(date__gte=cutoff_60, date__lte=today)
        .annotate(week=TruncWeek("date"))
        .values("week")
        .annotate(
            avg_hrv=Avg("hrv_last_night"),
            avg_rhr=Avg("resting_hr"),
            avg_sleep=Avg("sleep_score"),
            avg_bb=Avg("body_battery_high"),
            avg_stress=Avg("stress_avg"),
            # Garmin's real score when a day has one, else FitPulse's Google
            # Health-derived proxy — see DailyStats.readiness_score.
            avg_readiness=Avg(Coalesce("training_readiness_score", "computed_readiness_score")),
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

    nutr_rows = list(
        DailyStats.objects.for_user(user).filter(
            date__gte=cutoff_60, date__lte=today, cal_total__isnull=False
        ).order_by("date").values_list("date", "cal_total", "protein_g_total", "fiber_g_total")
    )
    nutr_lines = [
        f"  {d}: {cal:.0f} kcal, {prot:.0f}g protein, {fib:.1f}g fiber"
        for d, cal, prot, fib in nutr_rows
        if cal
    ]

    from .models import HungerCheck
    hunger_lines = []
    hunger_rows = list(
        HungerCheck.objects.for_user(user).filter(date__gte=cutoff_60, context="morning")
        .annotate(week=TruncWeek("date"))
        .values("week")
        .annotate(avg_hunger=Avg("hunger_level"))
        .order_by("week")
    )
    for r in hunger_rows:
        hunger_lines.append(f"  Week of {_as_date(r['week'])}: avg morning hunger {r['avg_hunger']:.1f}/10")

    from .models import SideEffectLog
    symptom_lines = []
    symptom_rows = list(
        SideEffectLog.objects.for_user(user).filter(date__gte=cutoff_60)
        .annotate(week=TruncWeek("date"))
        .values("week", "symptom", "other_label")
        .annotate(count=Count("id"), avg_severity=Avg("severity"))
        .order_by("week", "symptom", "other_label")
    )
    if symptom_rows:
        by_week: dict = {}
        for r in symptom_rows:
            w = str(_as_date(r["week"]))
            label = r["other_label"] if r["symptom"] == "other" and r["other_label"] else r["symptom"]
            by_week.setdefault(w, []).append(
                f"{label} ×{r['count']} (avg severity {r['avg_severity']:.1f})"
            )
        for week, symptoms in sorted(by_week.items()):
            symptom_lines.append(f"  Week of {week}: {', '.join(symptoms)}")

    from .models import CachedWorkout
    workout_rows = list(
        CachedWorkout.objects.for_user(user).filter(
            created_at__date__gte=cutoff_60,
            created_at__date__lte=today,
        )
        .annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(count=Count("id"))
        .order_by("week")
    )
    workout_lines = [f"  Week of {_as_date(r['week'])}: {r['count']} workouts" for r in workout_rows]

    iv_context = _interventions_context(user, today - timedelta(days=60), today)

    sections = [
        "WEIGHT & BODY COMPOSITION (last 60 days — daily)",
        "\n".join(weight_lines) if weight_lines else "  No weight data",
        "",
        "RECOVERY METRICS (weekly averages; readiness is Garmin's own score where available, "
        "otherwise FitPulse's Google Health-derived estimate from HRV/sleep/resting HR vs. baseline)",
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
    persona = build_persona_block(user, date_range=(today - timedelta(days=60), today))
    persona_section = f"\n{persona}" if persona else ""

    return f"""You are analyzing up to 60 days of integrated health data. Find non-obvious patterns the user might miss.{persona_section}

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


def _submit_pattern_insights_batch(user) -> str:
    """Submit pattern insights to Batch API. Saves batch_id to UserSettings. Returns batch_id."""
    prompt = _build_pattern_insights_prompt(user)
    batch_id = llm.submit_batch("pattern_insights", prompt, user=user, feature="ai_pattern_insights", model=llm.SONNET, max_tokens=2400)
    settings = UserSettings.for_user(user)
    settings.ai_pattern_insights_batch_id = batch_id
    settings.save(update_fields=["ai_pattern_insights_batch_id"])
    return batch_id


def render_pattern_insights_partial(request, context, status=200):
    return _render_poll_partial(request, "workouts/partials/pattern_insights.html", context, status=status)


def pattern_insights_check(request):
    """HTMX poll — check pattern insights batch status, return rendered HTML partial."""
    user = request.user
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    settings = UserSettings.for_user(user)
    batch_id = settings.ai_pattern_insights_batch_id

    if not batch_id:
        return render_pattern_insights_partial(request, {
            "insights": settings.ai_pattern_insights,
            "generated_at": settings.ai_pattern_insights_generated_at,
        }, status=286)

    if not api_key:
        settings.ai_pattern_insights_batch_id = None
        settings.save(update_fields=["ai_pattern_insights_batch_id"])
        return render_pattern_insights_partial(request, {"error": "ANTHROPIC_API_KEY is not set."}, status=286)

    try:
        batch = llm.get_batch_status(batch_id)
    except Exception:
        return render_pattern_insights_partial(request, {"pending": True})

    if batch.get("processing_status") != "ended":
        return render_pattern_insights_partial(request, {"pending": True})

    try:
        insights_text = result_row = None
        for row in llm.get_batch_results(batch_id):
            if row.get("result", {}).get("type") == "succeeded":
                insights_text = llm.extract_text(row["result"]["message"]["content"])
                result_row = row
                break
    except Exception as e:
        logger.warning("pattern insights batch results failed for %s: %s", batch_id, e)
        return render_pattern_insights_partial(request, {"pending": True})

    if not insights_text:
        settings.ai_pattern_insights_batch_id = None
        settings.save(update_fields=["ai_pattern_insights_batch_id"])
        return render_pattern_insights_partial(request, {
            "error": "Batch completed but no successful result found."
        }, status=286)

    settings.ai_pattern_insights = insights_text
    settings.ai_pattern_insights_generated_at = tz.now()
    settings.ai_pattern_insights_batch_id = None
    settings.save(update_fields=[
        "ai_pattern_insights", "ai_pattern_insights_generated_at", "ai_pattern_insights_batch_id",
    ])
    llm.log_batch_result(result_row)
    return render_pattern_insights_partial(request, {
        "insights": insights_text,
        "generated_at": settings.ai_pattern_insights_generated_at,
    }, status=286)


def pattern_insights_refresh(request):
    """POST — submit new pattern insights batch; returns pending HTML fragment."""
    user = request.user
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        _submit_pattern_insights_batch(user)
    except AI_UNAVAILABLE as e:
        return render_ai_unavailable(request, e)
    except Exception as e:
        return render_pattern_insights_partial(request, {"error": f"Failed to submit batch: {e}"})
    return render_pattern_insights_partial(request, {"pending": True})


# ---------------------------------------------------------------------------
# Weekly review — Claude Sonnet, cached per calendar week
# ---------------------------------------------------------------------------

def _build_weekly_review_prompt(user, week_start) -> str:
    """Build the weekly review prompt for the given Monday week_start."""
    from datetime import timedelta
    from .models import WeeklyReview, CachedWorkout, DailyStats, HungerCheck, SideEffectLog

    week_end = week_start + timedelta(days=6)

    daily_qs = DailyStats.objects.for_user(user).filter(date__gte=week_start, date__lte=week_end)
    weights = [(d.date.isoformat(), round(d.weight_lb, 1)) for d in daily_qs if d.weight_lb]
    weight_lines = "\n".join(f"  {d}: {w} lb" for d, w in weights) if weights else "  (no data)"

    prior_end = week_start - timedelta(days=1)
    prior_start = week_start - timedelta(days=7)
    prior_weights = list(DailyStats.objects.for_user(user).filter(
        date__gte=prior_start, date__lte=prior_end, weight_lb__isnull=False
    ).values_list("weight_lb", flat=True))
    prior_avg = sum(prior_weights) / len(prior_weights) if prior_weights else None
    current_avg = sum(w for _, w in weights) / len(weights) if weights else None
    weight_change_str = ""
    if prior_avg and current_avg:
        diff = current_avg - prior_avg
        weight_change_str = f"  Week avg: {current_avg:.1f} lb vs prior week avg {prior_avg:.1f} lb ({diff:+.1f} lb)"

    nutrition_rows = []
    logged_days_data = []
    for d in daily_qs:
        if d.cal_total:
            nutrition_rows.append(
                f"  {d.date}: {d.cal_total:.0f} kcal, {d.protein_g_total or 0:.0f}g P, "
                f"{d.carbs_g_total or 0:.0f}g C, {d.fat_g_total or 0:.0f}g F, "
                f"{d.fiber_g_total or 0:.0f}g fiber"
            )
            logged_days_data.append(d)
    nutrition_str = "\n".join(nutrition_rows) if nutrition_rows else "  (no nutrition data logged)"

    try:
        from .models import NutritionProfile
        from .nutrition import compute_macro_targets
        profile = NutritionProfile.objects.filter(user=user).first()
        targets = compute_macro_targets(user, profile) if profile else None
        if targets:
            cal_t = targets.get("calories")
            prot_t = targets.get("protein_g")
            fiber_t = targets.get("fiber_g")
            n_logged = len(logged_days_data)
            if n_logged and prot_t:
                days_hit_cal = sum(1 for d in logged_days_data if d.cal_total and d.cal_total <= cal_t * 1.05) if cal_t else None
                days_hit_prot = sum(1 for d in logged_days_data if (d.protein_g_total or 0) >= prot_t * 0.9)
                days_hit_fiber = sum(1 for d in logged_days_data if (d.fiber_g_total or 0) >= (fiber_t or 0) * 0.85) if fiber_t else None
                adherence_str = (
                    f"\n  Adherence ({n_logged} days logged): "
                    + (f"calories ≤target {days_hit_cal}/{n_logged} days, " if days_hit_cal is not None else "")
                    + f"protein ≥90% of target {days_hit_prot}/{n_logged} days"
                    + (f", fiber ≥85% of target {days_hit_fiber}/{n_logged} days" if days_hit_fiber is not None else "")
                )
            else:
                adherence_str = ""
            target_str = (
                f"  Configured targets (use these exact numbers — do not substitute your own): "
                f"{cal_t:.0f} kcal, {prot_t:.0f}g P, {fiber_t:.0f}g fiber"
                + adherence_str
            )
        else:
            target_str = "  (targets not configured)"
    except Exception:
        target_str = "  (targets unavailable)"

    workouts = list(CachedWorkout.objects.for_user(user).filter(
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

    hrv_vals = [d.hrv_last_night for d in daily_qs if d.hrv_last_night]
    sleep_vals = [d.sleep_seconds / 3600 for d in daily_qs if d.sleep_seconds]
    rhr_vals = [d.resting_hr for d in daily_qs if d.resting_hr]
    recovery_bits = []
    if sleep_vals:
        recovery_bits.append(f"Avg sleep: {sum(sleep_vals)/len(sleep_vals):.1f}h")
    if hrv_vals:
        recovery_bits.append(f"avg HRV: {sum(hrv_vals)/len(hrv_vals):.0f} ms")
    if rhr_vals:
        recovery_bits.append(f"avg RHR: {sum(rhr_vals)/len(rhr_vals):.0f} bpm")
    recovery_str = "  " + ", ".join(recovery_bits) if recovery_bits else "  (no recovery data logged this week)"

    hunger_qs = HungerCheck.objects.for_user(user).filter(date__gte=week_start, date__lte=week_end)
    morning_hunger = [h.hunger_level for h in hunger_qs if h.context == "morning"]
    hunger_str = (
        f"  Morning hunger avg: {sum(morning_hunger)/len(morning_hunger):.1f}/10"
        if morning_hunger else "  Morning hunger: (not tracked)"
    )

    symptoms_qs = SideEffectLog.objects.for_user(user).filter(date__gte=week_start, date__lte=week_end)
    symptom_counts: dict = {}
    for s in symptoms_qs:
        key = s.display_name
        symptom_counts[key] = symptom_counts.get(key, 0) + 1
    symptoms_str = (
        "  " + ", ".join(f"{k} ×{v}" for k, v in sorted(symptom_counts.items(), key=lambda x: -x[1]))
        if symptom_counts else "  (none logged)"
    )

    interventions_ctx = _interventions_context(user, week_start, week_end)

    persona = build_persona_block(user, date_range=(week_start, week_end))
    persona_section = f"\n{persona}" if persona else ""

    return f"""You are reviewing someone's health and fitness week ({week_start} to {week_end}).{persona_section}

WEIGHT THIS WEEK:
{weight_lines}
{weight_change_str}

NUTRITION THIS WEEK:
{nutrition_str}
{target_str}

WORKOUTS:
{workouts_str}

RECOVERY:
{recovery_str}

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


def _submit_weekly_review_batch(user, week_start):
    """Submit weekly review to Batch API. Creates/updates WeeklyReview with batch_id. Returns instance."""
    from .models import WeeklyReview
    prompt = _build_weekly_review_prompt(user, week_start)
    batch_id = llm.submit_batch("weekly_review", prompt, user=user, feature="ai_weekly_review", model=llm.SONNET, max_tokens=1600)
    review, _ = WeeklyReview.objects.update_or_create(user=user,
        week_start=week_start,
        defaults={"content": "", "ai_model": llm.SONNET, "batch_id": batch_id},
    )
    return review


def render_weekly_review_partial(request, context, status=200):
    return _render_poll_partial(request, "workouts/partials/weekly_review_content.html", context, status=status)


def weekly_review_check(request):
    """HTMX poll — check weekly review batch status, return rendered HTML."""
    user = request.user
    import datetime as _dt
    from .models import WeeklyReview

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    week_str = request.GET.get("week", "")
    try:
        week_start = _dt.date.fromisoformat(week_str)
        review = WeeklyReview.objects.for_user(user).get(week_start=week_start)
    except (ValueError, WeeklyReview.DoesNotExist):
        # Not in the four listed terminal states, but equally unresolvable —
        # a bad/missing week param can never turn into a live batch, so
        # polling on it would otherwise never stop either.
        return HttpResponse("Week not found.", status=286)

    def _pending():
        return render_weekly_review_partial(request, {
            "pending": True, "week_start": week_start,
        })

    if not review.batch_id:
        return render_weekly_review_partial(request, {"review": review}, status=286)

    if not api_key:
        WeeklyReview.objects.for_user(user).filter(week_start=week_start).update(batch_id=None)
        return render_weekly_review_partial(request, {
            "error": "ANTHROPIC_API_KEY is not set.", "week_start": week_start,
        }, status=286)

    try:
        batch = llm.get_batch_status(review.batch_id)
    except Exception:
        return _pending()

    if batch.get("processing_status") != "ended":
        return _pending()

    try:
        content = result_row = None
        for row in llm.get_batch_results(review.batch_id):
            if row.get("result", {}).get("type") == "succeeded":
                content = llm.extract_text(row["result"]["message"]["content"])
                result_row = row
                break
    except Exception as e:
        logger.warning("weekly review batch results failed for %s: %s", review.batch_id, e)
        return _pending()

    if not content:
        WeeklyReview.objects.for_user(user).filter(week_start=week_start).update(batch_id=None)
        return render_weekly_review_partial(request, {
            "error": "Batch completed but no result found.", "week_start": week_start,
        }, status=286)

    WeeklyReview.objects.for_user(user).filter(week_start=week_start).update(content=content, batch_id=None)
    llm.log_batch_result(result_row)
    review.refresh_from_db()
    return render_weekly_review_partial(request, {"review": review}, status=286)


def _get_or_generate_weekly_review(user, week_start, force: bool = False):
    """
    Return a WeeklyReview for the given Monday week_start.
    If missing (or force), submits a batch and returns a pending WeeklyReview.
    Returns the WeeklyReview instance, or None on failure.
    """
    from .models import WeeklyReview

    if not force:
        try:
            existing = WeeklyReview.objects.for_user(user).get(week_start=week_start)
            if existing.content or existing.batch_id:
                return existing
        except WeeklyReview.DoesNotExist:
            pass

    try:
        return _submit_weekly_review_batch(user, week_start)
    except AI_UNAVAILABLE:
        raise
    except Exception as e:
        logger.warning("Weekly review batch submit failed: %s", e)
        try:
            return WeeklyReview.objects.for_user(user).get(week_start=week_start)
        except WeeklyReview.DoesNotExist:
            return None


# ---------------------------------------------------------------------------
# Program retrospective — Claude Sonnet, cached per ProgramRun
# ---------------------------------------------------------------------------

def _get_or_generate_retrospective(user, run, force: bool = False) -> str:
    """
    Return the retrospective for this ProgramRun, generating (or regenerating) it
    with Sonnet if missing. Synchronous like intervention interpretation — a
    retrospective is generated once per run end (or previewed on demand mid-cycle),
    not on a schedule, so the batch API isn't worth the complexity here.
    """
    if run.retrospective and not force:
        return run.retrospective

    from .programs import build_retrospective_context
    ctx = build_retrospective_context(run)

    kind_line = (
        "This is a structured PLAN with an intended ramp (durations/intensity increase across weeks). "
        "Grade whether the athlete followed that progression."
        if ctx["kind"] == "plan" else
        "This is a SPLIT — the same sessions repeated weekly with no designed progression. "
        "Judge it on consistency and strength gain, not on following a ramp."
    )

    prompt = f"""You are writing a retrospective for a completed training block in a personal fitness app.
{kind_line}

Be specific and cite the numbers you're given; never invent values or infer effort that wasn't
logged. RPE is the athlete's reported experience — use it only where present, note coverage, and
highlight where load trend and RPE diverge (e.g. load flat but RPE rising = possible under-recovery;
load rising but RPE falling = ready to progress harder). If an intervention or dose change overlapped
the window, treat it as a confounder and do NOT attribute body-composition or performance changes to
training alone. Calibrate causal language to the evidence. No filler adjectives.

progression_deltas carries both top-set weight and total reps per exercise where each was tracked —
treat them as two independent progression signals, not one. An exercise with flat or missing weight
but rising reps is still real progress (more work at the same load) and should be named as such, not
read as stalled; the reverse (reps flat or falling while weight rises) is also worth naming. Don't
assume the two always move together.

If present, running_progression_deltas carries pace and distance deltas per run type (intervals /
tempo / steady-race-prep). IMPORTANT sign convention: pace is seconds per mile, so a NEGATIVE
pace_change_sec_per_mi / pace_change_pct means the athlete got FASTER — this is the opposite of
progression_deltas, where negative means a regression. Read pace improvement and distance growth as
two independent signals, same as weight/reps above.

Data:
{json.dumps(ctx, indent=1)}

Respond using these markdown headers exactly:

## What worked
## Where it slipped
## Progression highlights
## Recovery & effort
## Focus for next cycle"""

    try:
        text = llm.call(prompt, user=user, feature="ai_program_tools", model=llm.SONNET, max_tokens=1800, timeout=60)
    except AI_UNAVAILABLE:
        raise
    except Exception as e:
        logger.warning("Program retrospective generation failed: %s", e)
        return run.retrospective or ""

    run.retrospective = text
    run.retrospective_generated_at = tz.now()
    run.retrospective_model = llm.SONNET
    run.save(update_fields=["retrospective", "retrospective_generated_at", "retrospective_model"])
    return text


# ---------------------------------------------------------------------------
# Stats chat (sidebar)
# ---------------------------------------------------------------------------

MAX_CHAT_TOOL_ROUNDS = 5

_CHAT_PAGE_HINTS = {
    "dashboard": "The user is looking at their workout dashboard overview.",
    "body": "The user is looking at the Body page, currently showing the last {range_days} days.",
    "trends": "The user is on the Trends page, currently focused on the intervention: {intervention_name}.",
    "nutrition": "The user is looking at their daily nutrition log.",
    "nutrition_analytics": "The user is looking at nutrition analytics.",
    "history": "The user is looking at their workout history, filtered to: {discipline}.",
    "insights": "The user is looking at their AI pattern insights page.",
    "review": "The user is looking at their weekly review.",
}


def _build_chat_system_prompt(user, context):
    template = _CHAT_PAGE_HINTS.get(context.get("page"))
    if template:
        page_hint = template.format(
            range_days=context.get("range_days", 90),
            intervention_name=context.get("intervention_name") or "none selected",
            discipline=context.get("discipline") or "all disciplines",
        )
    else:
        page_hint = "The user is somewhere in their fitness dashboard."

    return f"""You are the stats assistant embedded in FitPulse, a personal \
health and fitness dashboard. Today's date is {context.get('today')}.

{page_hint}

You answer questions about the user's own workout, recovery, body \
composition, nutrition, and intervention data by calling the provided \
tools. Ground every claim in numbers the tools return — never estimate, \
round dramatically, or state a trend you haven't pulled data for.

Rules:
- Every specific numeric or trend claim must cite a value that came from a \
tool result. If you haven't called a tool for it, don't state it.
- If a tool returns a small sample size (few days of data, few workouts), \
say so and hedge ("worth checking, but only N days of data") rather than \
stating it as settled.
- No filler adjectives ("amazing", "concerning", "impressive"). State the \
number and let it speak.
- No clinical or diagnostic framing — this is a personal tracker, not a \
medical read.
- Don't invent subjective states ("you must have felt tired") the user \
hasn't told you about.
- Calibrate causal language to how strong the evidence actually is. "X \
dropped after Y" is not "X dropped because of Y" unless the tool result \
supports a real before/after comparison.
- If the question is ambiguous about date range, default to what's \
currently in view (see above) rather than asking — but say what range \
you used in your answer.
- Keep answers short: a few sentences or a tight bullet list, not a report."""


def _chat_tools_with_cache():
    """CHAT_TOOLS with a cache_control breakpoint on the last entry, so the
    (static) system prompt + tool schemas are cached together across turns."""
    tools = [dict(t) for t in CHAT_TOOLS]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def run_stats_chat(user, context, history, user_message):
    """
    context: dict describing what page/range/intervention is in view
    history: list of prior {"role": ..., "content": ...} message dicts
             (already in Anthropic message format; empty list for a new
             conversation)
    user_message: str

    Returns: (answer_text: str, updated_history: list)
    """
    system_prompt = _build_chat_system_prompt(user, context)
    messages = history + [{"role": "user", "content": user_message}]
    tool_dispatch = build_tool_dispatch(user)

    for _round in range(MAX_CHAT_TOOL_ROUNDS):
        body = {
            "model": llm.SONNET,
            "max_tokens": 1024,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": _chat_tools_with_cache(),
            "messages": messages,
        }
        data = llm.call_raw(body, user=user, feature="ai_chat", timeout=30)

        messages.append({"role": "assistant", "content": data["content"]})

        if data.get("stop_reason") != "tool_use":
            answer_text = "".join(
                block["text"] for block in data["content"] if block["type"] == "text"
            ).strip()
            return answer_text, messages

        tool_results = []
        for block in data["content"]:
            if block["type"] != "tool_use":
                continue
            fn = tool_dispatch.get(block["name"])
            try:
                if fn is None:
                    raise ValueError(f"unknown tool {block['name']}")
                result = fn(**block["input"])
                content = json.dumps(result, default=str)
            except Exception as e:
                logger.warning("Chat tool %s failed: %s", block["name"], e)
                content = json.dumps({"error": str(e)})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": content,
            })
        messages.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to pull together a complete answer to that in the "
        "allotted number of steps — try narrowing the question (e.g. a "
        "specific date range or metric).",
        messages,
    )


# ---------------------------------------------------------------------------
# AI training plans — Sonnet writes the week-by-week structure as class specs;
# workouts/training_plans.py validates them and picks the real classes.
# ---------------------------------------------------------------------------

def _training_plan_prompt(inputs, context_text, menu_text):
    from .training_plans import DAY_NAMES, GOALS, RACE_LABELS, _fmt_hms, plan_dates

    start, race = plan_dates(inputs)
    goal_line = GOALS[inputs["goal"]]
    if race:
        goal_line += f", race on {race:%A} {race.isoformat()}"
    if inputs.get("target_time"):
        goal_line += f", target time {_fmt_hms(inputs['target_time'])}"
    race_line = (f" Race is on day {inputs['race_weekday']} of week {inputs['race_week']}."
                 if race else "")
    if inputs["mode"] == "standalone":
        mode_line = "STANDALONE: this plan is their main training for these weeks"
        extras = (f"Strength sessions per week: {inputs.get('strength_per_week', 0)}. "
                  f"Mobility sessions: {'yes' if inputs.get('mobility') else 'no'}.")
    else:
        mode_line = ("ALONGSIDE: they keep doing the COMPANION PROGRAM below; you schedule only runs "
                     "(and optional short stretches) around it")
        extras = f"Running on a companion strength day: {'allowed' if inputs.get('allow_doubles') else 'not allowed'}."
    days = ", ".join(DAY_NAMES[d] for d in inputs["days"])
    long_day = DAY_NAMES[inputs["long_day"]] if inputs.get("long_day") else "none"
    weeks = inputs["weeks"]
    race_name = RACE_LABELS.get(inputs["goal"], "")
    example_name = f"{race_name} in {weeks} Weeks" if race_name else f"{weeks}-Week Running Base"
    pace = inputs.get("pace") or {}
    pace_rules = ""
    if pace.get("goal_pace_s"):
        pace_rules += """
11. PACE: the goal is to move them from their current pace (PACE section) to goal
    pace by race day. Easy and long runs stay in the Easy–Moderate zones. Each week's
    quality sessions practice goal pace and faster: tempo/progression running around
    goal pace (usually Challenging–Hard), intervals at and above it (Hard–Very Hard),
    getting longer or more frequent as the plan builds. Use Intervals/Speed class
    types for the fast work once the level gate allows them.
12. If PACE says STRETCH GOAL, keep the ramp limits anyway — don't add hard sessions
    to chase the time — and say in assumptions that the target is ambitious for this
    runway, citing the numbers."""
    if pace.get("long_run_min"):
        pace_rules += f"""
13. LONG RUN: build the weekly long run to at least {pace['long_run_min']} min at easy pace
    (longer than the race builds the endurance to hold pace to the finish), reaching it
    no later than 2–3 weeks before race day, within the ramp limits and the max session
    length. If the max session length or the runway makes that impossible, go as long as
    allowed and say so in assumptions."""

    return f"""You are building a {weeks}-week training plan for one person, made only of Peloton
classes. You write the structure; software picks the actual classes afterwards, so
you describe each session as a class spec, never a class title or id.

GOAL
{goal_line}
Plan weeks run Monday–Sunday. Week 1 starts {start.isoformat()} ({start:%A}); schedule nothing in week 1 before that day.{race_line}
Mode: {mode_line}
Available days: {days} (1=Mon … 7=Sun; allowed day numbers: {inputs['days']}). Preferred long-run day: {long_day}.
Max session length: weekdays {inputs['max_weekday_min']} min, weekends {inputs['max_weekend_min']} min.
{extras}
Their notes: {inputs.get('notes') or 'none'}

FITNESS CONTEXT
{context_text}

CLASS MENU — choose class types and durations ONLY from these lines
{menu_text}

PLANNING RULES
1. Start from STARTING LEVEL and their current running volume in FITNESS CONTEXT,
   not from a generic template. Week 1 total running minutes should be within about
   10% of their recent 4-week weekly average — except for beginner (see rule 8) and
   returning (rule 8b).
2. Increase weekly running minutes by no more than about 10–15% week over week.
   For plans of 8+ weeks, make every 3rd or 4th week a lighter week (~20–30% less).
3. At most 2 quality sessions per week (intervals, speed, hills, tempo/progression).
   Never schedule quality sessions on consecutive days.
4. One long easy run per week on the preferred day when given; grow it gradually.
   Easy/endurance running should be most of the weekly minutes.
5. Taper: the final 7–10 days before the race cut volume ~30–50% while keeping one
   short quality session early in race week. The day before the race is rest or a
   very short easy session. Do not schedule anything on race day — software adds the
   race itself.
6. ALONGSIDE mode: no quality run on the same day as, or the day after, a [lower body]
   companion session. Only put a run on a companion strength day if doubling is allowed.
   Use only running, walking, stretching and (if on the menu) yoga.
7. STANDALONE mode: place strength on non-quality days, not the day before a long run
   or a quality run if avoidable. Stretching/pilates sessions are short (10–20 min)
   and may share a day with a run (order 1 = after the run).
7b. Yoga (when on the menu) is recovery or mobility: gentle types (Slow Flow,
   Restorative, Yin, Recovery, Flow ≤ 30 min) on easy days or rest days, or after a
   long run. No Power/Sculpt yoga the day before a quality run or the long run. In
   ALONGSIDE mode, don't add yoga on a day the companion program already has yoga or
   pilates, and mark added yoga sessions optional.
8. beginner: start with Walk + Run / Beginner Running / Running Basics at 15–20 min,
   2–3 times a week. Progress toward 25–30 minutes of continuous running before any
   quality session; the first quality sessions are short and moderate.
8b. returning: they have a running base from before. Start with short easy continuous
   runs (20–30 min) rather than walk/run unless the evidence shows no runs at all in
   8 weeks; you may ramp faster than for a beginner (up to ~20% a week for the first
   3 weeks) but add no quality session before week 3.
8c. intermediate/advanced: keep their current frequency, build the long run, and use
   up to 2 quality sessions a week from week 1–2.
8d. Only use running class types marked [early OK] during the weeks the menu says;
   types marked [from week N] only from week N onward.
8e. If the runway is short, aim the plan at finishing comfortably rather than the
   target time, keep the ramp within the limits above, and say so in assumptions.
8f. RECENT CLASS DIFFICULTY is member-rated relative to each rater's own fitness —
   use it only to see whether they usually choose easier or harder classes, never as
   an ability score.
9. Respect available days and max session lengths exactly. One running session per
   day at most. duration_min must be one of the lengths listed on that class type's
   CLASS MENU line — Peloton doesn't make in-between lengths like 25, 35 or 40 min, so
   build progression by stepping between listed lengths (e.g. 20 → 30 → 45).
10. If their notes mention pain or an injury, keep intensity lower than you otherwise
    would and say so in assumptions. Don't give medical instructions.{pace_rules}

WRITING RULES
- summary: 2–3 sentences. Name the starting point using numbers from FITNESS CONTEXT
  (e.g. "averaging 3 runs and 74 minutes a week over the last 4 weeks, longest 35 min")
  and how the plan builds from it. Every number you cite must appear in FITNESS
  CONTEXT or be a property of this plan. No filler adjectives ("solid", "great",
  "strong", "excellent", "impressive").
- purpose: one short sentence per session saying what it's for ("easy aerobic
  minutes", "race-pace practice") — no motivation lines.
- assumptions: things you inferred because data was missing, each one sentence.
  If FITNESS CONTEXT lacks something you needed, say so here instead of guessing.
- pace_zone (running/walking sessions only, else null): the Peloton pace zone the
  session mostly targets — Recovery, Easy, Moderate, Challenging, Hard, Very Hard or Max.
- pace_guidance: 1–3 sentences (under 600 characters). If PACE gives a race-pace level
  above their current level, map roughly which weeks to try each step up (when Hard-zone
  efforts start to feel controlled), no faster than the ramp rules allow, and say where
  they can realistically get to if it's out of reach. If PACE says no level change is
  needed, say which zones to run goal-pace work in at their current level instead —
  never suggest moving to a lower level. Empty string if PACE has no Peloton pace level.

Return ONLY JSON, no prose, in exactly this shape:
{{
  "plan_name": "{example_name}",
  "summary": "...",
  "assumptions": ["..."],
  "pace_guidance": "...",
  "weeks": [
    {{"number": 1, "phase": "base", "focus": "one short line",
     "slots": [
       {{"day": 1, "order": 0, "discipline": "running", "class_type": "Endurance",
        "class_type_id": "19efefbcf7394ff8bac0ac89a674c545", "duration_min": 30,
        "intensity": "easy", "setting": "tread", "pace_zone": "Easy", "purpose": "...", "optional": false}}
     ]}}
  ]
}}
phase ∈ base | build | peak | lighter | taper | race. intensity ∈ easy | moderate | hard.
setting ∈ tread | outdoor (only when the menu lists both; otherwise use the one listed).
discipline ∈ the first column of the CLASS MENU (running, walking, stretching, strength, pilates, yoga, cycling)."""


_TRAINING_PLAN_SYSTEM = ("You write training plans as data for software to read. Reply with exactly one JSON "
                         "object matching the requested shape — no introduction, no explanation, no code fence.")


def generate_training_plan_spec(user, inputs, context_text, menu_text) -> dict:
    """Sonnet writes the plan structure as class specs (feature ai_program_tools).
    Input ≈ 6–9k tokens, output ≈ 3–10k (more for long standalone plans) →
    roughly $0.03–0.12 per plan at the MODEL_PRICES Sonnet rate.
    AIFeatureDenied / AIBudgetExceeded propagate; llm.AIBadJSON on an
    unreadable or cut-off reply."""
    prompt = _training_plan_prompt(inputs, context_text, menu_text)
    raw = llm.call_json(prompt, user=user, feature="ai_program_tools", model=llm.SONNET,
                        max_tokens=16000, timeout=300,   # long plans run past 8k tokens; this runs in a thread
                        system=_TRAINING_PLAN_SYSTEM, expect=dict)
    raw["model"] = llm.SONNET
    return raw
