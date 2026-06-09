"""
Macro target calculator, daily nutrition rollup, and analytics helpers for Cadence.
"""

import statistics
from datetime import date, timedelta
from difflib import SequenceMatcher

from .models import DailyStats, NutritionProfile


ACTIVITY_MULTIPLIERS = {
    "sedentary":   1.2,
    "light":       1.375,
    "moderate":    1.55,
    "active":      1.725,
    "very_active": 1.9,
}

CALORIE_FLOOR = {"female": 1200, "male": 1500}


def compute_macro_targets(profile=None):
    """
    Compute daily macro targets from NutritionProfile + latest Withings body comp data.

    Returns a dict with all computed targets, BMR/TDEE intermediates, and warning flags.
    If manual_* overrides are set, those replace the computed values (computed still shown).
    Returns None if no profile exists.
    """
    if profile is None:
        try:
            profile = NutritionProfile.objects.get(pk=1)
        except NutritionProfile.DoesNotExist:
            return None

    # Latest weight and lean mass from DailyStats (Withings data)
    latest = (
        DailyStats.objects.filter(weight_lb__isnull=False)
        .order_by("-date")
        .first()
    )

    weight_lb = latest.weight_lb if latest else None
    lean_mass_lb = latest.fat_free_mass_lb if latest else None
    weight_date = latest.date.isoformat() if latest else None

    weight_kg = weight_lb / 2.20462 if weight_lb else None
    lean_mass_kg = lean_mass_lb / 2.20462 if lean_mass_lb else None

    warnings = []

    # ── BMR (Mifflin-St Jeor) ───────────────────────────────────────────────
    bmr = None
    if weight_kg and profile.height_cm and profile.age:
        base = 10 * weight_kg + 6.25 * profile.height_cm - 5 * profile.age
        bmr = base + 5 if profile.biological_sex == "male" else base - 161

    # ── TDEE ────────────────────────────────────────────────────────────────
    tdee = None
    if bmr:
        multiplier = ACTIVITY_MULTIPLIERS.get(profile.activity_level, 1.55)
        tdee = bmr * multiplier

    # ── Calorie target ───────────────────────────────────────────────────────
    floor = CALORIE_FLOOR.get(profile.biological_sex, 1200)
    computed_calories = None
    if tdee:
        if profile.goal == "loss":
            computed_calories = tdee * (1 - profile.deficit_pct / 100)
        elif profile.goal == "gain":
            computed_calories = tdee * (1 + profile.deficit_pct / 100)
        else:
            computed_calories = tdee

        if computed_calories < floor:
            computed_calories = floor
            warnings.append("deficit_too_aggressive")

    calories = profile.manual_calories if profile.manual_calories else computed_calories

    # ── Protein ──────────────────────────────────────────────────────────────
    if lean_mass_kg:
        computed_protein = profile.protein_g_per_kg_lean * lean_mass_kg
    elif weight_kg:
        computed_protein = 1.6 * weight_kg
        warnings.append("no_lean_mass_data")
    else:
        computed_protein = 120
        warnings.append("no_body_data")

    computed_protein = max(computed_protein, 100)
    protein_g = profile.manual_protein_g if profile.manual_protein_g else computed_protein

    # ── Fat ──────────────────────────────────────────────────────────────────
    ref_cal = calories or computed_calories or 1500
    computed_fat = ref_cal * 0.25 / 9
    if weight_kg:
        computed_fat = max(computed_fat, 0.5 * weight_kg)
    fat_g = profile.manual_fat_g if profile.manual_fat_g else computed_fat

    # ── Carbs: remainder ────────────────────────────────────────────────────
    used_protein = protein_g
    carbs_remainder = (ref_cal - used_protein * 4 - fat_g * 9) / 4
    if carbs_remainder < 50:
        # Trim protein toward floor to free up carbs
        trim = (50 - carbs_remainder)
        used_protein = max(used_protein - trim, 100)
        carbs_remainder = (ref_cal - used_protein * 4 - fat_g * 9) / 4
        if not profile.manual_carbs_g:
            warnings.append("low_carb_check")
    computed_carbs = max(carbs_remainder, 20)
    carbs_g = profile.manual_carbs_g if profile.manual_carbs_g else computed_carbs

    # ── Fiber: 14g per 1000 kcal ────────────────────────────────────────────
    computed_fiber = (ref_cal / 1000 * 14)
    fiber_g = profile.manual_fiber_g if profile.manual_fiber_g else computed_fiber

    return {
        # Final targets (manual or computed)
        "calories":  round(calories)      if calories       else None,
        "protein_g": round(protein_g)     if protein_g      else None,
        "carbs_g":   round(carbs_g)       if carbs_g        else None,
        "fat_g":     round(fat_g)         if fat_g          else None,
        "fiber_g":   round(fiber_g, 1)    if fiber_g        else None,
        # Computed (for display alongside manual overrides)
        "computed_calories":  round(computed_calories) if computed_calories else None,
        "computed_protein_g": round(computed_protein)  if computed_protein  else None,
        "computed_carbs_g":   round(computed_carbs)    if computed_carbs    else None,
        "computed_fat_g":     round(computed_fat)      if computed_fat      else None,
        "computed_fiber_g":   round(computed_fiber, 1) if computed_fiber    else None,
        # Intermediates
        "bmr":          round(bmr)         if bmr          else None,
        "tdee":         round(tdee)        if tdee         else None,
        "weight_kg":    round(weight_kg, 1)    if weight_kg    else None,
        "weight_lb":    round(weight_lb, 1)    if weight_lb    else None,
        "lean_mass_kg": round(lean_mass_kg, 1) if lean_mass_kg else None,
        "lean_mass_lb": round(lean_mass_lb, 1) if lean_mass_lb else None,
        "weight_date":  weight_date,
        "calorie_floor": floor,
        "warnings": warnings,
        # Profile echo
        "activity_level":        profile.activity_level,
        "goal":                  profile.goal,
        "deficit_pct":           profile.deficit_pct,
        "protein_g_per_kg_lean": profile.protein_g_per_kg_lean,
        "has_manual_overrides":  any([
            profile.manual_calories, profile.manual_protein_g,
            profile.manual_carbs_g, profile.manual_fat_g, profile.manual_fiber_g,
        ]),
    }


def recompute_daily_nutrition(date_obj):
    """Recompute DailyStats nutrition rollup for a date from all FoodEntry rows."""
    from .models import FoodEntry
    from django.db.models import Sum

    agg = FoodEntry.objects.filter(date=date_obj).aggregate(
        cal=Sum("calories"),
        prot=Sum("protein_g"),
        carbs=Sum("carbs_g"),
        fat=Sum("fat_g"),
        fiber=Sum("fiber_g"),
    )

    stats, _ = DailyStats.objects.get_or_create(date=date_obj)
    stats.cal_total = agg["cal"]
    stats.protein_g_total = agg["prot"]
    stats.carbs_g_total = agg["carbs"]
    stats.fat_g_total = agg["fat"]
    stats.fiber_g_total = agg["fiber"]
    stats.save(update_fields=[
        "cal_total", "protein_g_total", "carbs_g_total", "fat_g_total", "fiber_g_total"
    ])


# ---------------------------------------------------------------------------
# Streak computation
# ---------------------------------------------------------------------------

def compute_streaks(reference_date=None):
    """
    Compute food-logging streak and protein-target streak.

    Returns:
      {
        "logging_days": int,     # consecutive logged days ending today or yesterday
        "protein_days": int,     # consecutive days hitting >=90% protein target
        "protein_target": float | None,
      }
    """
    from .models import FoodEntry

    today = reference_date or date.today()

    logged_dates = set(
        FoodEntry.objects.filter(date__lte=today).values_list("date", flat=True).distinct()
    )

    # Start from today if logged; fall back to yesterday
    if today in logged_dates:
        start = today
    elif (today - timedelta(days=1)) in logged_dates:
        start = today - timedelta(days=1)
    else:
        return {"logging_days": 0, "protein_days": 0, "protein_target": None}

    logging_streak = 0
    d = start
    while d in logged_dates:
        logging_streak += 1
        d = d - timedelta(days=1)

    # Protein streak
    protein_target = None
    protein_streak = 0
    try:
        profile = NutritionProfile.objects.filter(pk=1).first()
        if profile:
            t = compute_macro_targets(profile)
            protein_target = t.get("protein_g") if t else None
    except Exception:
        pass

    if protein_target:
        protein_map = {
            row[0]: row[1]
            for row in DailyStats.objects.filter(
                date__lte=today,
                protein_g_total__isnull=False,
            ).values_list("date", "protein_g_total")
        }
        d = start
        while d in logged_dates:
            prot = protein_map.get(d)
            if prot is not None and prot >= protein_target * 0.9:
                protein_streak += 1
                d = d - timedelta(days=1)
            else:
                break

    return {
        "logging_days": logging_streak,
        "protein_days": protein_streak,
        "protein_target": protein_target,
    }


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def get_weekly_stats(end_date, targets=None):
    """
    Per-day nutrition for the 7 days ending on end_date (inclusive).

    Returns a dict with:
      days       — list of 7 day-dicts (oldest first)
      days_logged
      avg_cal, avg_protein_g, avg_fiber_g
      days_hit_cal, days_hit_protein, days_hit_fiber (None if no target)
    """
    days_list = [end_date - timedelta(days=i) for i in range(6, -1, -1)]

    stats_map = {
        s.date: s
        for s in DailyStats.objects.filter(date__gte=days_list[0], date__lte=days_list[-1])
    }

    cal_t = targets.get("calories") if targets else None
    prot_t = targets.get("protein_g") if targets else None
    fiber_t = targets.get("fiber_g") if targets else None

    rows = []
    for d in days_list:
        s = stats_map.get(d)
        logged = s is not None and s.cal_total is not None
        cal_v = round(s.cal_total) if logged and s.cal_total else None
        prot_v = round(s.protein_g_total) if logged and s.protein_g_total else None
        fiber_v = round(s.fiber_g_total, 1) if logged and s.fiber_g_total else None
        rows.append({
            "date": d,
            "weekday": d.strftime("%a"),
            "logged": logged,
            "cal": cal_v,
            "protein_g": prot_v,
            "carbs_g": round(s.carbs_g_total) if logged and s.carbs_g_total else None,
            "fat_g": round(s.fat_g_total) if logged and s.fat_g_total else None,
            "fiber_g": fiber_v,
            "cal_ok": (cal_v >= cal_t * 0.9) if (cal_v and cal_t) else None,
            "prot_ok": (prot_v >= prot_t * 0.9) if (prot_v and prot_t) else None,
            "fiber_ok": (fiber_v >= fiber_t * 0.85) if (fiber_v and fiber_t) else None,
        })

    logged_rows = [r for r in rows if r["logged"]]
    n = len(logged_rows)

    def _avg(key):
        vals = [r[key] for r in logged_rows if r[key] is not None]
        return round(sum(vals) / len(vals)) if vals else None

    def _days_hit(key, target, pct=0.9):
        if not target:
            return None
        return sum(1 for r in logged_rows if r[key] and r[key] >= target * pct)

    return {
        "days": rows,
        "days_logged": n,
        "avg_cal": _avg("cal"),
        "avg_protein_g": _avg("protein_g"),
        "avg_fiber_g": _avg("fiber_g"),
        "days_hit_cal": _days_hit("cal", cal_t),
        "days_hit_protein": _days_hit("protein_g", prot_t),
        "days_hit_fiber": _days_hit("fiber_g", fiber_t, pct=0.85),
    }


# ---------------------------------------------------------------------------
# Yesterday recap
# ---------------------------------------------------------------------------

def get_yesterday_recap(today, targets=None):
    """
    Return a summary of yesterday's nutrition for the 'no entries today' nudge card.
    Returns None if yesterday has no logged data.
    """
    from .models import FoodEntry

    yesterday = today - timedelta(days=1)
    stats = DailyStats.objects.filter(date=yesterday).first()
    if not stats or stats.cal_total is None:
        return None

    entries = list(
        FoodEntry.objects.filter(date=yesterday)
        .order_by("-protein_g")
        .values("raw_text", "protein_g", "meal")
    )
    top_protein_source = entries[0]["raw_text"][:60] if entries else None

    cal_t = targets.get("calories") if targets else None
    prot_t = targets.get("protein_g") if targets else None
    fiber_t = targets.get("fiber_g") if targets else None

    return {
        "date": yesterday,
        "cal": round(stats.cal_total),
        "protein_g": round(stats.protein_g_total or 0),
        "fiber_g": round(stats.fiber_g_total or 0, 1),
        "cal_target": cal_t,
        "prot_target": prot_t,
        "fiber_target": fiber_t,
        "cal_ok": cal_t and stats.cal_total <= cal_t * 1.1,
        "prot_ok": prot_t and (stats.protein_g_total or 0) >= prot_t * 0.9,
        "fiber_ok": fiber_t and (stats.fiber_g_total or 0) >= fiber_t * 0.85,
        "top_protein_source": top_protein_source,
        "entry_count": len(entries),
    }


# ---------------------------------------------------------------------------
# Top foods aggregation
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    import re
    name = name.lower().strip()
    # Strip leading quantity (e.g. "2x ", "1 cup ", "3oz ")
    name = re.sub(r'^\d+(\.\d+)?\s*(x|oz|g|ml|cup|cups|tbsp|tsp|serving|servings|piece|pieces|slice|slices)?\s*', '', name)
    return name.strip()


def get_top_foods(start, end, top_n=15):
    """
    Aggregate items from FoodEntry.items_json for the date range.
    Fuzzy-groups similar names (≥0.80 similarity).

    Returns list of dicts sorted by count desc:
      {name, count, avg_calories, avg_protein_g, avg_carbs_g, avg_fat_g, avg_fiber_g}
    """
    from .models import FoodEntry

    entries = FoodEntry.objects.filter(date__gte=start, date__lte=end).values_list("items_json", flat=True)

    all_items = []
    for items_json in entries:
        if isinstance(items_json, list):
            for item in items_json:
                if isinstance(item, dict) and item.get("name"):
                    all_items.append(item)

    if not all_items:
        return []

    groups = {}  # canonical → [items]

    for item in all_items:
        raw = _normalize_name(str(item.get("name", "")))
        if not raw:
            continue
        matched = None
        for canonical in groups:
            if SequenceMatcher(None, raw, canonical).ratio() >= 0.80:
                matched = canonical
                break
        if matched:
            groups[matched].append(item)
        else:
            groups[raw] = [item]

    results = []
    for canonical, items in groups.items():
        def _f(field, _items=items):
            vals = [float(it.get(field) or 0) for it in _items]
            return round(sum(vals) / len(vals), 1) if vals else 0.0

        results.append({
            "name": canonical,
            "count": len(items),
            "avg_calories": _f("calories"),
            "avg_protein_g": _f("protein_g"),
            "avg_carbs_g": _f("carbs_g"),
            "avg_fat_g": _f("fat_g"),
            "avg_fiber_g": _f("fiber_g"),
        })

    results.sort(key=lambda x: x["count"], reverse=True)
    return results[:top_n]


# ---------------------------------------------------------------------------
# Meal timing analysis
# ---------------------------------------------------------------------------

def get_meal_timing_stats(days=30):
    """
    Analyze meal timing across FoodEntry records for the past N days.

    Returns:
      {
        avg_breakfast_time, avg_lunch_time, avg_dinner_time,  # "8:15 AM" or None
        avg_last_meal_time,
        avg_gap_to_sleep,   # hours (uses 10:30 PM as default bedtime proxy)
        eating_window_hours,
        meal_type_counts: {meal: count, ...}
      }
    """
    from .models import FoodEntry
    from django.utils import timezone

    cutoff = date.today() - timedelta(days=days)
    entries = list(
        FoodEntry.objects.filter(date__gte=cutoff).values("meal", "logged_at", "date")
    )

    def _to_local_minutes(logged_at):
        if timezone.is_aware(logged_at):
            dt_local = timezone.localtime(logged_at)
        else:
            dt_local = logged_at
        return dt_local.hour * 60 + dt_local.minute

    def _avg_mins_to_str(mins_list):
        if not mins_list:
            return None
        avg_m = round(sum(mins_list) / len(mins_list))
        h, m = divmod(avg_m, 60)
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ampm}"

    by_meal: dict = {}
    for e in entries:
        m = e.get("meal") or "snack"
        by_meal.setdefault(m, []).append(e)

    meal_type_counts = {m: len(v) for m, v in by_meal.items()}

    def _extract_mins(elist):
        return [_to_local_minutes(e["logged_at"]) for e in elist]

    # Per-day first and last meal times
    daily_first: dict = {}
    daily_last: dict = {}
    for e in entries:
        d = e["date"]
        m = _to_local_minutes(e["logged_at"])
        if d not in daily_first or m < daily_first[d]:
            daily_first[d] = m
        if d not in daily_last or m > daily_last[d]:
            daily_last[d] = m

    last_meal_mins = list(daily_last.values())

    # Eating window per day
    windows = [
        daily_last[d] - daily_first[d]
        for d in daily_first if d in daily_last and daily_last[d] > daily_first[d]
    ]
    avg_window = round(sum(windows) / len(windows) / 60, 1) if windows else None

    # Gap to sleep — default bedtime 10:30 PM (1350 min)
    avg_gap = None
    if last_meal_mins:
        avg_last = sum(last_meal_mins) / len(last_meal_mins)
        bedtime = 22 * 60 + 30  # 10:30 PM
        gap = bedtime - avg_last
        if gap < 0:
            gap += 24 * 60
        avg_gap = round(gap / 60, 1)

    return {
        "avg_breakfast_time": _avg_mins_to_str(_extract_mins(by_meal.get("breakfast", []))),
        "avg_lunch_time": _avg_mins_to_str(_extract_mins(by_meal.get("lunch", []))),
        "avg_dinner_time": _avg_mins_to_str(_extract_mins(by_meal.get("dinner", []))),
        "avg_last_meal_time": _avg_mins_to_str(last_meal_mins),
        "avg_gap_to_sleep": avg_gap,
        "eating_window_hours": avg_window,
        "meal_type_counts": meal_type_counts,
    }


# ---------------------------------------------------------------------------
# Day-of-week patterns
# ---------------------------------------------------------------------------

def get_day_of_week_stats(start, end):
    """
    Average calories by day of week for logged days in the range.

    Returns list of 7 dicts: {day_name, avg_cal, count}
    """
    stats_qs = DailyStats.objects.filter(
        date__gte=start, date__lte=end, cal_total__isnull=False
    ).values_list("date", "cal_total")

    by_dow = {i: [] for i in range(7)}  # 0=Mon … 6=Sun
    for d, cal in stats_qs:
        by_dow[d.weekday()].append(cal)

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    results = []
    for i, name in enumerate(day_names):
        vals = by_dow[i]
        results.append({
            "day_name": name,
            "avg_cal": round(sum(vals) / len(vals)) if vals else None,
            "count": len(vals),
        })
    return results


# ---------------------------------------------------------------------------
# Nutrition data gap helper (for Trends page warning)
# ---------------------------------------------------------------------------

def get_nutrition_gap(start, end):
    """
    Return logged vs. total days in the window.

    Returns {days_logged: int, total_days: int, pct: float}
    """
    total = (end - start).days + 1
    logged = DailyStats.objects.filter(
        date__gte=start, date__lte=end, cal_total__isnull=False
    ).count()
    pct = round(logged / total * 100) if total else 0
    return {"days_logged": logged, "total_days": total, "pct": pct}


# ---------------------------------------------------------------------------
# Target fit evaluator (Part 4 — auto-tune)
# ---------------------------------------------------------------------------

def evaluate_target_fit():
    """
    Compare actual 14-day weight trend to expected trend given current calorie target.

    Returns a dict:
      status: on_track | under_responding | over_responding | insufficient_data | no_target | no_weight
      actual_trend_lb_per_week: float | None
      expected_trend_lb_per_week: float | None
      suggested_calorie_adjustment: int (positive = increase, negative = decrease, 0 = no change)
      new_suggested_calories: int | None
      reasoning: str
      confidence: high | medium | low
      logging_days: int
      current_calories: int | None
    """
    profile = NutritionProfile.objects.filter(pk=1).first()
    targets = compute_macro_targets(profile) if profile else None

    current_calories = targets.get("calories") if targets else None
    goal = profile.goal if profile else "loss"
    sex = profile.biological_sex if profile else "female"
    floor = CALORIE_FLOOR.get(sex, 1200)
    tdee = targets.get("tdee") if targets else None

    if not current_calories:
        return {
            "status": "no_target",
            "actual_trend_lb_per_week": None,
            "expected_trend_lb_per_week": None,
            "suggested_calorie_adjustment": 0,
            "new_suggested_calories": None,
            "reasoning": "No calorie target set. Fill in your profile on the Targets page.",
            "confidence": "low",
            "logging_days": 0,
            "current_calories": None,
        }

    today = date.today()
    start_14 = today - timedelta(days=13)

    # Require at least 10 logged days in the 14-day window
    logging_days = DailyStats.objects.filter(
        date__gte=start_14, date__lte=today, cal_total__isnull=False
    ).count()

    if logging_days < 10:
        return {
            "status": "insufficient_data",
            "actual_trend_lb_per_week": None,
            "expected_trend_lb_per_week": None,
            "suggested_calorie_adjustment": 0,
            "new_suggested_calories": None,
            "reasoning": f"Only {logging_days}/14 days logged in the last 2 weeks. Log at least 10 days for a reliable fit check.",
            "confidence": "low",
            "logging_days": logging_days,
            "current_calories": current_calories,
        }

    # Get daily weights for rolling averages
    weights = list(
        DailyStats.objects.filter(date__gte=start_14, date__lte=today, weight_lb__isnull=False)
        .order_by("date")
        .values_list("date", "weight_lb")
    )

    if len(weights) < 6:
        return {
            "status": "no_weight",
            "actual_trend_lb_per_week": None,
            "expected_trend_lb_per_week": None,
            "suggested_calorie_adjustment": 0,
            "new_suggested_calories": None,
            "reasoning": "Not enough weight data in the last 14 days. Sync Withings for a trend comparison.",
            "confidence": "low",
            "logging_days": logging_days,
            "current_calories": current_calories,
        }

    # Split into first-half and second-half rolling averages
    mid = len(weights) // 2
    first_half = [w for _, w in weights[:mid]]
    second_half = [w for _, w in weights[mid:]]
    avg_early = sum(first_half) / len(first_half)
    avg_recent = sum(second_half) / len(second_half)

    days_span = (weights[-1][0] - weights[0][0]).days or 1
    actual_trend = (avg_recent - avg_early) / days_span * 7  # lb/week

    # Expected trend from deficit
    expected_trend = 0.0
    if tdee and goal == "loss":
        daily_deficit = tdee - current_calories
        expected_trend = -(daily_deficit * 7) / 3500  # lb/week (negative = weight loss)
    elif tdee and goal == "gain":
        daily_surplus = current_calories - tdee
        expected_trend = (daily_surplus * 7) / 3500
    # maintain: expected 0

    diff = actual_trend - expected_trend  # positive = losing less than expected (under-responding for loss)
    confidence = "high" if logging_days >= 12 and len(weights) >= 10 else "medium"

    if goal == "loss":
        # For loss: actual_trend should be ≤ expected_trend (both negative)
        # under_responding = losing less than expected (diff > 0.5)
        # over_responding = losing more than expected (diff < -0.5)
        if diff > 0.5:
            adj = -200 if diff > 1.0 else -100
            new_cal = max(current_calories + adj, floor)
            actual_adj = new_cal - current_calories
            return {
                "status": "under_responding",
                "actual_trend_lb_per_week": round(actual_trend, 2),
                "expected_trend_lb_per_week": round(expected_trend, 2),
                "suggested_calorie_adjustment": actual_adj,
                "new_suggested_calories": new_cal,
                "reasoning": (
                    f"Your weight is trending {abs(actual_trend):.1f} lb/week "
                    f"(expected {abs(expected_trend):.1f} lb/week loss). "
                    f"Possible causes: logging inaccuracy, metabolic adaptation, "
                    f"or water retention from training."
                ),
                "confidence": confidence,
                "logging_days": logging_days,
                "current_calories": current_calories,
            }
        elif diff < -0.5:
            adj = 100
            new_cal = current_calories + adj
            return {
                "status": "over_responding",
                "actual_trend_lb_per_week": round(actual_trend, 2),
                "expected_trend_lb_per_week": round(expected_trend, 2),
                "suggested_calorie_adjustment": adj,
                "new_suggested_calories": new_cal,
                "reasoning": (
                    f"You're losing {abs(actual_trend):.1f} lb/week — faster than the "
                    f"{abs(expected_trend):.1f} lb/week your target predicts. "
                    f"A small increase prevents metabolic adaptation and protects lean mass."
                ),
                "confidence": confidence,
                "logging_days": logging_days,
                "current_calories": current_calories,
            }
    else:
        # maintain or gain: use ±0.5 threshold on absolute diff
        if abs(diff) > 0.5:
            adj = -100 if diff > 0 else 100
            new_cal = max(current_calories + adj, floor)
            return {
                "status": "under_responding" if diff > 0 else "over_responding",
                "actual_trend_lb_per_week": round(actual_trend, 2),
                "expected_trend_lb_per_week": round(expected_trend, 2),
                "suggested_calorie_adjustment": new_cal - current_calories,
                "new_suggested_calories": new_cal,
                "reasoning": f"Weight trend ({actual_trend:+.1f} lb/week) diverges from expected ({expected_trend:+.1f} lb/week).",
                "confidence": confidence,
                "logging_days": logging_days,
                "current_calories": current_calories,
            }

    return {
        "status": "on_track",
        "actual_trend_lb_per_week": round(actual_trend, 2),
        "expected_trend_lb_per_week": round(expected_trend, 2),
        "suggested_calorie_adjustment": 0,
        "new_suggested_calories": None,
        "reasoning": (
            f"Your actual trend ({actual_trend:+.1f} lb/week) is within range of "
            f"the expected trend ({expected_trend:+.1f} lb/week). Keep going."
        ),
        "confidence": confidence,
        "logging_days": logging_days,
        "current_calories": current_calories,
    }


def get_satisfying_meals(min_occurrences: int = 3, top_n: int = 5) -> list[dict]:
    """
    Return SavedMeals that have been rated as satisfying (post-meal fullness >= 7)
    at least min_occurrences times via linked HungerCheck records.

    Each result dict: {meal, name, calories, protein_g, carbs_g, fat_g, fiber_g,
                       times_logged, satisfying_count}
    Sorted by satisfying_count desc, then times_logged desc.
    """
    from django.db.models import Count
    from .models import HungerCheck, SavedMeal

    # HungerChecks with high fullness linked to a saved meal via FoodEntry
    qs = (
        HungerCheck.objects
        .filter(context="post_meal", fullness_level__gte=7, related_meal__source_saved_meal__isnull=False)
        .values("related_meal__source_saved_meal")
        .annotate(satisfying_count=Count("pk"))
        .filter(satisfying_count__gte=min_occurrences)
        .order_by("-satisfying_count")[:top_n]
    )

    results = []
    for row in qs:
        meal_pk = row["related_meal__source_saved_meal"]
        try:
            sm = SavedMeal.objects.get(pk=meal_pk)
        except SavedMeal.DoesNotExist:
            continue
        results.append({
            "meal": sm,
            "name": sm.name,
            "calories": sm.calories,
            "protein_g": sm.protein_g,
            "carbs_g": sm.carbs_g,
            "fat_g": sm.fat_g,
            "fiber_g": sm.fiber_g,
            "times_logged": sm.times_logged,
            "satisfying_count": row["satisfying_count"],
        })
    return results
