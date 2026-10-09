"""Add CachedWorkout.run_targets_json, and convert stored pace zones to decimal minutes.

Peloton's pace zone tables (pace_intensities_mapping) are minutes.seconds —
15.47 is 15:47/mi — but get_parsed_performance used to store them as if they
were decimal minutes (15.47 → 15:28), so pace_zones, the target_pace series
built from their midpoints and the legacy pace_targets_json series were all off
by up to ~25 s/mi. This rewrites them from each row's own stored zones:
fast/slow edges converted, Recovery's slow edge re-capped (20 min/mi running,
35 walking — walking zones used to get the running cap), and every target value
that was an old zone midpoint replaced with the new one. Rows parsed by the
fixed code carry "pace_zone_unit" and are skipped, so this never converts twice.
"""
from django.db import migrations, models

RECOVERY_CAP = {"running": 20.0, "walking": 35.0}


def _minutes(value):
    whole = int(value)
    return whole + round((value - whole) * 100) / 60


def _key(v):
    return round(v, 6)


def convert(perf, legacy):
    """(new perf dict, new legacy list) or None when there's nothing to convert."""
    zones = perf.get("pace_zones") or []
    if perf.get("pace_zone_unit") or not zones:
        return None
    walking = any(z.get("name") in ("Brisk", "Power") for z in zones)
    cap = RECOVERY_CAP["walking" if walking else "running"]
    new_zones, mids = [], {}
    for z in zones:
        fast, slow = z.get("fast_pace"), z.get("slow_pace")
        if not fast or not slow:
            continue
        new_fast = _minutes(fast)
        new_slow = max(cap, new_fast) if z.get("name") == "Recovery" else _minutes(slow)
        new_zones.append({**z, "fast_pace": new_fast, "slow_pace": new_slow})
        mids[_key((fast + slow) / 2)] = (new_fast + new_slow) / 2
        if z.get("name") == "Recovery":
            # older parses plotted Recovery at the cap itself, not a midpoint
            mids.setdefault(_key(slow), (new_fast + new_slow) / 2)

    def remap(values):
        return [mids.get(_key(v), v) if isinstance(v, (int, float)) else v for v in values]

    perf = {**perf, "pace_zones": new_zones, "pace_zone_unit": "decimal_min"}
    target = (perf.get("metrics_by_slug") or {}).get("target_pace")
    if target and target.get("values"):
        values = remap(target["values"])
        valid = [v for v in values if v is not None]
        perf["metrics_by_slug"] = {**perf["metrics_by_slug"], "target_pace": {
            **target, "values": values, "average_value": sum(valid) / len(valid) if valid else None}}
    if isinstance(perf.get("target_pace"), list):
        perf["target_pace"] = remap(perf["target_pace"])
    return perf, remap(legacy or [])


def forwards(apps, schema_editor):
    CachedWorkout = apps.get_model("workouts", "CachedWorkout")
    qs = (CachedWorkout.objects.filter(performance_graph_json__has_key="pace_zones")
          .only("id", "performance_graph_json", "pace_targets_json"))
    for w in qs.iterator():
        result = convert(w.performance_graph_json or {}, w.pace_targets_json)
        if result:
            CachedWorkout.objects.filter(pk=w.pk).update(
                performance_graph_json=result[0], pace_targets_json=result[1])


class Migration(migrations.Migration):

    dependencies = [
        ("workouts", "0041_plan_draft_reassess"),
    ]

    operations = [
        migrations.AddField(
            model_name="cachedworkout",
            name="run_targets_json",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
