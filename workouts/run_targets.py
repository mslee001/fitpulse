"""
The Tread run inside a class like "45 min Lower Body + Run".

The class's running targets (CachedWorkout.run_targets_json, from the ride's
target_metrics_data — see peloton_client.parse_run_targets) give the run's
length and the pace zone for every stretch of it. With a pace level, each zone
becomes a target pace from PELOTON_PACE_CHART: the average of the zone's fast
and slow edge. Recovery and Max are open-ended (1 mph / 12.5 mph), so they use
their inner edge — Recovery its fast edge, Max its slow edge.

run_summary() then gives one comparable set of numbers for every such workout:
measured distance and pace when Peloton recorded the run, otherwise an estimate
from the targets (flagged `estimated`). Estimates stay out of distance_miles /
avg_pace_seconds so they never mix into running averages.
"""
import re

PRESHOW_SECONDS = 60   # target offsets are on the class clock, which includes the pre-show


def _chart():
    from .training_plans import PACE_ZONES, PELOTON_PACE_CHART
    return PACE_ZONES, PELOTON_PACE_CHART


def zone_pace_seconds(level, zone):
    """Target pace (seconds/mile) for a Tread zone (0 = Recovery … 6 = Max) at a
    Peloton pace level, from PELOTON_PACE_CHART. None for an unknown level/zone."""
    names, chart = _chart()
    zones = chart.get(level)
    if not zones or not 0 <= zone < len(zones):
        return None
    lo, hi = zones[zone]                # mph
    if zone == 0:                       # Recovery: open-ended below
        return 3600 / hi
    if zone == len(zones) - 1:          # Max: open-ended above
        return 3600 / lo
    return (3600 / lo + 3600 / hi) / 2


def zone_name(zone):
    names, _ = _chart()
    return names[zone] if 0 <= zone < len(names) else f"Zone {zone}"


def level_from_label(label):
    """"Level 4" → 4 (the perf graph's pace_level display name), else None."""
    m = re.search(r"(\d+)", label or "")
    return int(m.group(1)) if m else None


def pace_level_at(user, when):
    """The user's Peloton pace level at a moment: from the latest Tread run (or
    + Run class) on or before it whose performance graph recorded one, else the
    earliest after it. None when no performance graph has a level."""
    from .models import CachedWorkout
    qs = (CachedWorkout.objects.for_user(user)
          .filter(source="peloton", discipline__in=("running", "circuit"),
                  performance_graph_json__has_key="pace_level")
          .only("performance_graph_json", "created_at"))
    for w in qs.filter(created_at__lte=when).order_by("-created_at")[:20]:
        level = level_from_label((w.performance_graph_json or {}).get("pace_level"))
        if level:
            return level
    for w in qs.filter(created_at__gt=when).order_by("created_at")[:20]:
        level = level_from_label((w.performance_graph_json or {}).get("pace_level"))
        if level:
            return level
    return None


def planned_run(targets, level):
    """Planned run from class targets at a pace level.

    {"seconds", "level", "miles", "pace_s", "zones": [{"zone", "name", "seconds",
    "pace_s"}] in zone order, "segments": [{"start", "end", "zone", "pace_s"}]
    in workout seconds}. miles/pace_s (and per-zone paces) are None without a
    level. None when there are no targets."""
    if not targets:
        return None
    seconds = 0
    miles = 0.0 if level else None
    by_zone, segments = {}, []
    for t in targets:
        length = t["end"] - t["start"] + 1        # offsets are inclusive
        zones = sorted({t["lower"], t["upper"]})
        paces = [zone_pace_seconds(level, z) for z in zones] if level else []
        pace = sum(paces) / len(paces) if paces and None not in paces else None
        seconds += length
        if miles is not None:
            miles = miles + length / pace if pace else None
        z = t["upper"]
        row = by_zone.setdefault(z, {"zone": z, "name": zone_name(z), "seconds": 0,
                                     "pace_s": round(pace) if pace else None})
        row["seconds"] += length
        segments.append({"start": t["start"] - PRESHOW_SECONDS, "end": t["end"] - PRESHOW_SECONDS + 1,
                         "zone": z, "pace_s": round(pace) if pace else None})
    return {
        "seconds": seconds,
        "level": level,
        "miles": round(miles, 2) if miles else None,
        "pace_s": round(seconds / miles) if miles else None,
        "zones": [by_zone[z] for z in sorted(by_zone)],
        "segments": segments,
    }


def run_summary(workout):
    """Run time, distance and pace for a class with a Tread portion, comparable
    across takes: {"seconds", "miles", "pace_s", "estimated", "level", "plan"}.
    Measured distance (and Peloton's average pace, else time ÷ distance) when
    Peloton recorded the run; otherwise the plan's estimate. None for workouts
    without running targets."""
    data = workout.run_targets_json or {}
    targets = data.get("segments") or []
    if not targets:
        return None
    pg = workout.performance_graph_json or {}
    level = level_from_label(pg.get("pace_level")) or data.get("level")
    plan = planned_run(targets, level)
    measured = workout.distance_miles
    if measured:
        avg = ((pg.get("average_summaries") or {}).get("avg_pace") or {}).get("value")
        pace_s = round(avg * 60) if avg else round(plan["seconds"] / measured)
        return {"seconds": plan["seconds"], "miles": round(measured, 2), "pace_s": pace_s,
                "estimated": False, "level": level, "plan": plan}
    return {"seconds": plan["seconds"], "miles": plan["miles"], "pace_s": plan["pace_s"],
            "estimated": True, "level": level, "plan": plan}
