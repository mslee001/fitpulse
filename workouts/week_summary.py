"""Today's "This week" tiles: Monday → today, each compared with the same span last week.

The week is the calendar week the Weekly Review uses (Mon–Sun). The comparison is
neutral wording only: more workouts isn't always better.
"""

from datetime import timedelta

from django.db.models import Count, Sum

from .access import has_feature
from .models import CachedWorkout, DailyStats
from .nutrition import PROTEIN_HIT_PCT, compute_macro_targets


def _hm(seconds):
    """3h 05m / 45m."""
    minutes = round((seconds or 0) / 60)
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _workout_totals(user, start, end):
    # created_at is the workout's start time; __date converts it to the local date
    # (TIME_ZONE), the same as the day view and weekly review.
    return CachedWorkout.objects.for_user(user).filter(
        created_at__date__gte=start, created_at__date__lte=end,
    ).aggregate(n=Count("pk"), secs=Sum("duration_seconds"))


def _avg_sleep(user, start, end):
    vals = list(DailyStats.objects.for_user(user).filter(
        date__gte=start, date__lte=end, sleep_seconds__gt=0,
    ).values_list("sleep_seconds", flat=True))
    return sum(vals) / len(vals) if vals else None


def _protein_days(user, start, end, target):
    """(days hit, days with a protein total) between start and end."""
    vals = list(DailyStats.objects.for_user(user).filter(
        date__gte=start, date__lte=end, protein_g_total__isnull=False,
    ).values_list("protein_g_total", flat=True))
    return sum(1 for v in vals if v >= target * PROTEIN_HIT_PCT), len(vals)


def week_summary(user, today, targets=None):
    """Up to four tiles for Today's This week section.

    Returns {"start": monday, "days": D, "tiles": [{key, label, value, sub}]}; a tile
    is left out when the user doesn't have its feature or it has no data. `targets` is
    compute_macro_targets(user) when the caller already has it.
    """
    start = today - timedelta(days=today.weekday())
    days = (today - start).days + 1
    prev_start, prev_end = start - timedelta(days=7), today - timedelta(days=7)
    tiles = []

    if has_feature(user, "training"):
        now, prev = _workout_totals(user, start, today), _workout_totals(user, prev_start, prev_end)
        if now["n"] or prev["n"]:
            tiles.append({"key": "workouts", "label": "Workouts", "value": str(now["n"]),
                          "sub": f"{prev['n']} same time last week"})
            tiles.append({"key": "training_time", "label": "Training time", "value": _hm(now["secs"]),
                          "sub": f"{_hm(prev['secs'])} same time last week"})

    sleep = _avg_sleep(user, start, today)
    if sleep:
        prev = _avg_sleep(user, prev_start, prev_end)
        tiles.append({"key": "sleep", "label": "Avg sleep", "value": _hm(sleep),
                      "sub": f"{_hm(prev)} same time last week" if prev else "No sleep data same time last week"})

    if has_feature(user, "nutrition"):
        if targets is None:
            targets = compute_macro_targets(user)
        target = (targets or {}).get("protein_g")
        if target:
            hit, logged = _protein_days(user, start, today, target)
            prev_hit, prev_logged = _protein_days(user, prev_start, prev_end, target)
            if logged or prev_logged:
                tiles.append({"key": "protein", "label": "Protein days hit", "value": f"{hit} / {days}",
                              "sub": f"{prev_hit} / {days} same time last week"})

    return {"start": start, "days": days, "tiles": tiles}
