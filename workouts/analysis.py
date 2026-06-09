"""
Core before/after intervention analysis logic.

Used by both the analyze_intervention management command (CLI output)
and the Trends page (web API).
"""

import statistics
from datetime import date, timedelta

from workouts.models import DailyStats


# ---------------------------------------------------------------------------
# Metric group definitions
# (display_name, field, direction, unit, decimals)
# ---------------------------------------------------------------------------

METRIC_GROUPS = [
    ("RECOVERY", [
        ("Training Readiness", "training_readiness_score", "higher", "",    0),
        ("Body Battery High",  "body_battery_high",        "higher", "",    0),
        ("Body Battery Charge","body_battery_charge",      "higher", "",    0),
        ("HRV Last Night",     "hrv_last_night",           "higher", "ms",  1),
        ("HRV Weekly Avg",     "hrv_weekly_avg",           "higher", "ms",  1),
        ("Resting HR",         "resting_hr",               "lower",  "bpm", 0),
    ]),
    ("SLEEP", [
        ("Sleep Score",        "sleep_score",              "higher", "",         0),
        ("Sleep Duration",     "sleep_seconds",            "higher", "min",      0),
        ("Deep Sleep",         "sleep_deep_seconds",       "higher", "min",      0),
        ("REM Sleep",          "sleep_rem_seconds",        "higher", "min",      0),
        ("SpO2 Sleep Avg",     "spo2_sleep_avg",           "higher", "%",        1),
        ("SpO2 Sleep Low",     "spo2_sleep_low",           "higher", "%",        0),
        ("Respiration (sleep)","respiration_sleep_avg",    "lower",  "br/min",   1),
    ]),
    ("STRESS", [
        ("Stress Avg",         "stress_avg",               "lower",  "",    0),
        ("Stress Max",         "stress_max",               "lower",  "",    0),
        ("High-Stress Min",    "stress_high_minutes",      "lower",  "min", 0),
        ("Rest-State Min",     "stress_rest_minutes",      "higher", "min", 0),
    ]),
    ("ACTIVITY", [
        ("Steps",              "steps",                    "higher", "",     0),
        ("Active Calories",    "active_calories",          "higher", "kcal", 0),
        ("Moderate Int. Min",  "moderate_intensity_minutes","higher","min",  0),
        ("Vigorous Int. Min",  "vigorous_intensity_minutes","higher","min",  0),
    ]),
    ("BODY COMPOSITION", [
        ("Weight",             "weight_lb",                "WEIGHT_GOAL", "lb", 1),
        ("Fat %",              "fat_ratio_pct",            "lower",       "%",  2),
        ("Fat Mass",           "fat_mass_lb",              "lower",       "lb", 1),
        ("Muscle Mass",        "muscle_mass_lb",           "higher",      "lb", 1),
        ("Hydration",          "hydration_lb",             "neutral",     "lb", 1),
        ("Bone Mass",          "bone_mass_lb",             "neutral",     "lb", 1),
    ]),
    ("NUTRITION", [
        ("Daily Calories",  "cal_total",       "neutral", "kcal", 0),
        ("Daily Protein",   "protein_g_total", "higher",  "g",    0),
        ("Daily Carbs",     "carbs_g_total",   "neutral", "g",    0),
        ("Daily Fat",       "fat_g_total",     "neutral", "g",    0),
        ("Daily Fiber",     "fiber_g_total",   "higher",  "g",    1),
    ]),
]

SECONDS_FIELDS = {"sleep_seconds", "sleep_deep_seconds", "sleep_rem_seconds", "sleep_light_seconds"}


def _vals(qs, field):
    return [v for v in qs.values_list(field, flat=True) if v is not None]


def _stats(vals):
    if not vals:
        return None, None
    mean = statistics.mean(vals)
    std  = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return mean, std


def _pct_change(before, after):
    if before is None or after is None or before == 0:
        return None
    return (after - before) / abs(before) * 100


def run_intervention_analysis(
    before_start,
    before_end,
    after_start,
    after_end,
    weight_goal="loss",
    washout_days=0,
) -> dict:
    """
    Returns a structured analysis result dict.

    {
        "before_start": date, "before_end": date,
        "after_start": date,  "after_end": date,
        "before_n": int, "after_n": int,
        "groups": [
            {
                "name": str,
                "metrics": [
                    {
                        "display_name": str,
                        "field": str,
                        "direction": str,   # "higher" / "lower" / "neutral"
                        "unit": str,
                        "before_mean": float|None,
                        "before_std": float|None,
                        "after_mean": float|None,
                        "after_std": float|None,
                        "before_n": int,
                        "after_n": int,
                        "pct_change": float|None,
                        "improved": bool|None,
                    }
                ]
            }
        ]
    }
    """
    weight_direction = {"loss": "lower", "gain": "higher", "maintain": "neutral"}.get(weight_goal, "lower")

    today = date.today()
    # Clamp after_end to today
    if after_end > today:
        after_end = today

    before_qs = DailyStats.objects.filter(date__gte=before_start, date__lte=before_end)
    after_qs  = DailyStats.objects.filter(date__gte=after_start,  date__lte=after_end)

    before_n = before_qs.count()
    after_n  = after_qs.count()

    groups_out = []
    for group_name, metrics in METRIC_GROUPS:
        metrics_out = []
        for display_name, field, direction, unit, decimals in metrics:
            resolved_direction = weight_direction if direction == "WEIGHT_GOAL" else direction

            before_raw = _vals(before_qs, field)
            after_raw  = _vals(after_qs,  field)

            # Convert seconds → minutes for sleep duration fields
            if field in SECONDS_FIELDS:
                before_raw = [v // 60 for v in before_raw]
                after_raw  = [v // 60 for v in after_raw]

            bm, bs = _stats(before_raw)
            am, as_ = _stats(after_raw)

            pct = _pct_change(bm, am)

            improved = None
            if bm is not None and am is not None and resolved_direction != "neutral":
                diff = am - bm
                if abs(diff) < 0.01:
                    improved = None  # no meaningful change
                elif resolved_direction == "higher":
                    improved = diff > 0
                elif resolved_direction == "lower":
                    improved = diff < 0

            metrics_out.append({
                "display_name": display_name,
                "field": field,
                "direction": resolved_direction,
                "unit": unit,
                "before_mean": bm,
                "before_std": bs,
                "after_mean": am,
                "after_std": as_,
                "before_n": len(before_raw),
                "after_n": len(after_raw),
                "pct_change": pct,
                "improved": improved,
            })
        groups_out.append({"name": group_name, "metrics": metrics_out})

    return {
        "before_start": before_start,
        "before_end":   before_end,
        "after_start":  after_start,
        "after_end":    after_end,
        "before_n":     before_n,
        "after_n":      after_n,
        "weight_goal":  weight_goal,
        "groups":       groups_out,
    }
