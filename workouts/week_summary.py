"""Week summaries: Today's "This week" tiles (Monday → today) and the Weekly Review's
week-over-week chips (a whole past week). Both compare a span with the same span a week
earlier, in neutral words (more workouts isn't always better); only body weight is
colored, and only against the user's goal (goal_tone).
"""

from datetime import timedelta

from django.db.models import Avg, Count, Sum

from .access import has_feature
from .models import CachedWorkout, DailyStats
from .nutrition import PROTEIN_HIT_PCT, compute_macro_targets


def _hm(seconds):
    """3h 05m / 45m."""
    minutes = round((seconds or 0) / 60)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _signed_minutes(seconds):
    minutes = round(seconds / 60)
    return f"+{minutes}m" if minutes > 0 else f"−{-minutes}m" if minutes < 0 else "±0m"


def _signed(n):
    return f"+{n}" if n > 0 else f"−{-n}" if n < 0 else "±0"


def goal_tone(delta, goal, up_good=None):
    """Text class for a body-composition change. Down/up is only good or bad against the
    user's NutritionProfile.goal (loss: down is good; gain: up is good; maintain or no
    goal: neutral). up_good=True forces "up is good" (lean/muscle mass)."""
    if not delta:
        return ""
    if up_good is None:
        if goal == "loss":
            up_good = False
        elif goal == "gain":
            up_good = True
        else:
            return ""
    return "text-success" if (delta > 0) == up_good else "text-error"


def _span(user, start, end, protein_target):
    """Raw numbers for start..end (inclusive). Workouts are bucketed by local start date:
    CachedWorkout.created_at is the start time, and __date converts it to TIME_ZONE."""
    w = CachedWorkout.objects.for_user(user).filter(
        created_at__date__gte=start, created_at__date__lte=end,
    ).aggregate(n=Count("pk"), secs=Sum("duration_seconds"))
    days = DailyStats.objects.for_user(user).filter(date__gte=start, date__lte=end)
    sleep = list(days.filter(sleep_seconds__gt=0).values_list("sleep_seconds", flat=True))
    protein = list(days.filter(protein_g_total__isnull=False).values_list("protein_g_total", flat=True))
    weight = days.filter(weight_lb__isnull=False).aggregate(avg=Avg("weight_lb"))["avg"]
    return {
        "workouts": w["n"], "seconds": w["secs"] or 0,
        "sleep": sum(sleep) / len(sleep) if sleep else None,
        "protein_hit": sum(1 for v in protein if protein_target and v >= protein_target * PROTEIN_HIT_PCT),
        "protein_logged": len(protein),
        "weight": weight,
    }


def _compare(user, start, end, targets):
    """(this span, the same span a week earlier, protein target or None)."""
    target = None
    if has_feature(user, "nutrition"):
        if targets is None:
            targets = compute_macro_targets(user)
        target = (targets or {}).get("protein_g")
    week = timedelta(days=7)
    return _span(user, start, end, target), _span(user, start - week, end - week, target), target


def week_summary(user, start, end=None, targets=None):
    """Up to four tiles for Today's This week section.

    week_summary(user, today) covers Monday → today; week_summary(user, start, end) any
    span. Returns {"start", "days" (D), "tiles": [{key, label, value, sub}]}; a tile is
    left out when the user doesn't have its feature or it has no data. `targets` is
    compute_macro_targets(user) when the caller already has it.
    """
    if end is None:
        end, start = start, start - timedelta(days=start.weekday())
    days = (end - start).days + 1
    now, prev, target = _compare(user, start, end, targets)
    tiles = []

    if has_feature(user, "training") and (now["workouts"] or prev["workouts"]):
        tiles.append({"key": "workouts", "label": "Workouts", "value": str(now["workouts"]),
                      "sub": f"{prev['workouts']} same time last week"})
        tiles.append({"key": "training_time", "label": "Training time", "value": _hm(now["seconds"]),
                      "sub": f"{_hm(prev['seconds'])} same time last week"})

    if now["sleep"]:
        tiles.append({"key": "sleep", "label": "Avg sleep", "value": _hm(now["sleep"]),
                      "sub": f"{_hm(prev['sleep'])} same time last week" if prev["sleep"]
                      else "No sleep data same time last week"})

    if target and (now["protein_logged"] or prev["protein_logged"]):
        tiles.append({"key": "protein", "label": "Protein days hit", "value": f"{now['protein_hit']} / {days}",
                      "sub": f"{prev['protein_hit']} / {days} same time last week"})

    return {"start": start, "days": days, "tiles": tiles}


def review_chips(user, week_start, goal=None, targets=None):
    """Week-over-week chips for a Weekly Review (Mon week_start → Sun) vs the week before:
    [{key, text, tone, sr}]. Weight is the average of the week's daily (earliest weigh-in)
    values, colored by goal_tone; the rest are neutral. Chips without data are left out."""
    end = week_start + timedelta(days=6)
    now, prev, target = _compare(user, week_start, end, targets)
    chips = []
    if now["weight"] is not None and prev["weight"] is not None:
        d = round(now["weight"] - prev["weight"], 1)
        chips.append({"key": "weight", "text": f"Weight {_signed(d)} lb",
                      "tone": goal_tone(d, goal),
                      "sr": f"Weight {'up' if d > 0 else 'down' if d < 0 else 'unchanged'}"
                            f"{f' {abs(d)} pounds' if d else ''} from the week before"})
    if has_feature(user, "training") and (now["workouts"] or prev["workouts"]):
        d = now["workouts"] - prev["workouts"]
        chips.append({"key": "workouts", "text": f"Workouts {now['workouts']} ({_signed(d)})", "tone": "",
                      "sr": f"{now['workouts']} workouts, {prev['workouts']} the week before"})
    if now["sleep"]:
        extra = f" ({_signed_minutes(now['sleep'] - prev['sleep'])})" if prev["sleep"] else ""
        before = f", {_hm(prev['sleep'])} the week before" if prev["sleep"] else ""
        chips.append({"key": "sleep", "text": f"Avg sleep {_hm(now['sleep'])}{extra}", "tone": "",
                      "sr": f"Average sleep {_hm(now['sleep'])}{before}"})
    if target and (now["protein_logged"] or prev["protein_logged"]):
        d = now["protein_hit"] - prev["protein_hit"]
        chips.append({"key": "protein", "text": f"Protein days {now['protein_hit']}/7 ({_signed(d)})", "tone": "",
                      "sr": f"Protein target hit on {now['protein_hit']} of 7 days, {prev['protein_hit']} the week before"})
    return chips
