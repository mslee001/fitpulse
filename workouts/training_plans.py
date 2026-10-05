"""AI training plans: a goal → a week-by-week plan of real Peloton classes.

Flow: the form's inputs (clean_inputs) → a PlanDraft → background generation
(build_fitness_context + catalog_menu → Sonnet writes class *specs* →
validate_spec → pick_classes fills each spec with a real class from the
catalog) → review/swap → create_program_from_draft saves a normal Program.

Sonnet never sees or invents ride ids; code picks the classes. Every number in
the fitness context comes from the database so the AI's claims can be checked
against PlanDraft.context_text.
"""
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean, median

from django.db.models import Count
from django.utils import timezone

from .models import (
    AthleteProfile, CachedWorkout, DailyStats, PelotonClass, PelotonClassType, Program, ProgramSlot,
)

logger = logging.getLogger(__name__)

GOALS = {"5k": "5K race", "10k": "10K race", "half": "Half marathon", "full": "Marathon",
         "base": "General running base (no race)"}
RACE_LABELS = {"5k": "5K", "10k": "10K", "half": "Half marathon", "full": "Marathon"}
LEVELS = ["beginner", "returning", "intermediate", "advanced"]
SETTINGS = ["tread", "outdoor", "either"]
MODES = ["standalone", "alongside"]
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}

MIN_WEEKS, MAX_WEEKS = 3, 20

# Minimum sensible weeks to race day, by goal × starting level. Short runways
# get a warning and a "finish comfortably" plan — never a block.
RUNWAY_MIN_WEEKS = {
    "5k":   {"beginner": 8,  "returning": 5,  "intermediate": 4,  "advanced": 3},
    "10k":  {"beginner": 10, "returning": 7,  "intermediate": 6,  "advanced": 4},
    "half": {"beginner": 14, "returning": 11, "intermediate": 10, "advanced": 8},
    "full": {"beginner": 20, "returning": 18, "intermediate": 16, "advanced": 14},
}

# Disciplines CachedWorkout uses for runs (Peloton tread/outdoor, Garmin and
# Google Health all map to "running"; the other two are defensive).
RUN_DISCIPLINES = ("running", "outdoor_running", "outdoor")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _local_date(dt):
    return timezone.localtime(dt).date() if dt else None


def _minutes(w):
    return (w.duration_seconds or 0) / 60


def _monday(d):
    return d - timedelta(days=d.weekday())


def _fmt_hms(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_time(raw):
    """'H:MM:SS' or 'MM:SS' → seconds; None if blank; ValueError if malformed."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(raw)
    nums = [int(p) for p in parts]
    if any(n >= 60 for n in nums[1:]):
        raise ValueError(raw)
    return nums[0] * 3600 + nums[1] * 60 + nums[2] if len(nums) == 3 else nums[0] * 60 + nums[1]


def _getlist(post, key):
    return post.getlist(key) if hasattr(post, "getlist") else list(post.get(key) or [])


def plan_dates(inputs):
    """(start_date, race_date or None) as dates."""
    start = date.fromisoformat(inputs["start_date"])
    race = date.fromisoformat(inputs["race_date"]) if inputs.get("race_date") else None
    return start, race


def week_dates(inputs, week):
    """The seven dates (Mon..Sun) of plan week `week` (1-based)."""
    start, _ = plan_dates(inputs)
    monday = _monday(start) + timedelta(weeks=week - 1)
    return [monday + timedelta(days=i) for i in range(7)]


def early_weeks(weeks):
    """The first third of the plan, rounded up — where the level gate and clamp apply."""
    return math.ceil(weeks / 3)


# ---------------------------------------------------------------------------
# Class-type classification (for the level gate and "continuous" runs)
# ---------------------------------------------------------------------------

def run_type_tag(name):
    """Canonical tag for a running class type name ('Walk + Run' → 'walk_run').
    Substring-based so 'Outdoor Endurance' and 'Endurance Running' both match."""
    n = (name or "").lower().replace("&", " and ")
    if "walk" in n and "run" in n:
        return "walk_run"
    for tag, needles in (("beginner", ("beginner",)), ("basics", ("basic",)),
                         ("pre_post", ("pre and post", "pre-run", "post-run")),
                         ("endurance", ("endurance",)), ("just_run", ("just run",)),
                         ("music", ("music",)), ("theme", ("theme",)),
                         ("intervals", ("interval",)), ("speed", ("speed",)), ("hill", ("hill",))):
        if any(x in n for x in needles):
            return tag
    return "other"


# Running types allowed in the early weeks, by level: tag → max minutes (None = any).
_BEGINNER_GATE = {"walk_run": None, "beginner": None, "basics": None, "endurance": 20, "pre_post": None}
EARLY_GATE = {
    "beginner": _BEGINNER_GATE,
    "returning": {**_BEGINNER_GATE, "endurance": 30, "just_run": 30, "music": 30, "theme": 30},
}


def early_ok(level, type_name, duration_min=None):
    """Whether a running class type (at this duration) is allowed in the early weeks."""
    gate = EARLY_GATE.get(level)
    if gate is None:
        return True
    tag = run_type_tag(type_name)
    if tag not in gate:
        return False
    cap = gate[tag]
    return cap is None or duration_min is None or duration_min <= cap


def _is_walk_title(title):
    t = (title or "").lower()
    return ("walk" in t and "run" in t) or "walk" in t


def _class_type_names(ride_ids):
    """ride_id → class type name, from the catalog (empty for unknown rides)."""
    ride_ids = [r for r in ride_ids if r]
    if not ride_ids:
        return {}
    rows = dict(PelotonClass.objects.filter(ride_id__in=ride_ids).values_list("ride_id", "class_type_id"))
    names = dict(PelotonClassType.objects.filter(id__in=set(rows.values())).values_list("id", "name"))
    return {rid: names.get(tid, "") for rid, tid in rows.items()}


def _runs(user, since):
    return list(CachedWorkout.objects.for_user(user)
                .filter(discipline__in=RUN_DISCIPLINES, created_at__date__gte=since)
                .order_by("created_at"))


def _continuous(w, type_names):
    """A run that isn't Walk + Run / walking — counts toward the longest continuous run."""
    if _is_walk_title(w.title):
        return False
    return run_type_tag(type_names.get(w.ride_id, "")) != "walk_run"


# ---------------------------------------------------------------------------
# Starting level
# ---------------------------------------------------------------------------

def assess_running_level(user, today=None) -> dict:
    """Classify beginner / returning / intermediate / advanced from the user's
    own running history. Deterministic; the form shows it with its evidence and
    lets the user override it."""
    today = today or timezone.localdate()
    runs = _runs(user, today - timedelta(days=730))
    type_names = _class_type_names({w.ride_id for w in runs})
    d56, d28 = today - timedelta(days=56), today - timedelta(days=28)
    last8 = [w for w in runs if _local_date(w.created_at) > d56]
    last4 = [w for w in runs if _local_date(w.created_at) > d28]
    continuous8 = [_minutes(w) for w in last8 if _continuous(w, type_names)]

    # Calendar months 2..23 back (the current and previous month excluded).
    month_counts = Counter((_local_date(w.created_at).year, _local_date(w.created_at).month) for w in runs)
    history_months = 0
    y, m = today.year, today.month
    for back in range(24):
        if back >= 2 and month_counts.get((y, m), 0) >= 4:
            history_months += 1
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)

    measures = {
        "runs_per_week_8w": round(len(last8) / 8, 1),
        "longest_continuous_8w": round(max(continuous8)) if continuous8 else 0,
        "minutes_per_week_4w": round(sum(_minutes(w) for w in last4) / 4),
        "history_months_24m": history_months,
        "days_since_last_run": (today - _local_date(runs[-1].created_at)).days if runs else None,
    }
    rpw, longest, hist = measures["runs_per_week_8w"], measures["longest_continuous_8w"], history_months
    if rpw >= 3 and longest >= 45 and hist >= 12:
        level = "advanced"
    elif rpw >= 1.5 and longest >= 25:
        level = "intermediate"
    elif hist >= 6:
        level = "returning"
    else:
        level = "beginner"

    experience = AthleteProfile.for_user(user).running_experience
    if experience == "experienced" and level == "beginner":
        level = "returning"          # profile breaks ties only; data wins otherwise

    evidence = [
        f"{rpw} runs/week over the last 8 weeks",
        f"longest continuous run {longest} min in 8 weeks" if longest else "no continuous runs in 8 weeks",
        f"{measures['minutes_per_week_4w']} running minutes/week over the last 4 weeks",
        f"regular running (4+ runs) in {hist} of the 22 months before that",
    ]
    if measures["days_since_last_run"] is not None:
        evidence.append(f"last run {measures['days_since_last_run']} days ago")
    return {"level": level, "measures": measures, "evidence": evidence, "profile_experience": experience}


def runway_check(goal, weeks, level):
    if goal not in RUNWAY_MIN_WEEKS:
        return None
    rec = RUNWAY_MIN_WEEKS[goal][level]
    return {"weeks": weeks, "recommended_min": rec, "short": weeks < rec}


def runway_line(inputs):
    r = inputs.get("runway")
    if not r:
        return "No race date (base building) — no runway check."
    status = "SHORT" if r["short"] else "OK"
    return (f"Runway: {r['weeks']} weeks to race day; recommended minimum for a {inputs['level']} "
            f"{RACE_LABELS.get(inputs['goal'], inputs['goal'])} is {r['recommended_min']} weeks — {status}.")


# ---------------------------------------------------------------------------
# Pace: current Peloton pace level, recent paces, race estimate, goal pace
# ---------------------------------------------------------------------------

RACE_MILES = {"5k": 3.10686, "10k": 6.21371, "half": 13.1094, "full": 26.2188}
PACE_ZONES = ["Recovery", "Easy", "Moderate", "Challenging", "Hard", "Very Hard", "Max"]
PACE_LEVELS = list(range(1, 11))   # Peloton Tread pace levels
RIEGEL_EXPONENT = 1.06             # Riegel race-time prediction: T2 = T1 × (D2 / D1) ^ 1.06
LONG_RUN_RACE_MULTIPLE = 1.25      # 5K/10K: peak long run ≥ this × race time…
LONG_RUN_FLOOR_MIN = 45            # …and never less than this
STRETCH_PCT_PER_4_WEEKS = 5        # needing more improvement than this per 4 weeks reads as a stretch goal


# Peloton Tread running pace chart: mph range per zone and level (Recovery … Max).
# Fixed by Peloton; zones are treated as contiguous (each runs up to the next zone's
# lower bound), since the 0.1-mph gaps in the printed chart are just rounding.
PELOTON_PACE_CHART = {
    1:  [(1.0, 3.0), (3.1, 3.3), (3.4, 3.6), (3.7, 4.0), (4.1, 4.4), (4.5, 4.9), (5.0, 12.5)],
    2:  [(1.0, 3.2), (3.3, 3.6), (3.7, 3.9), (4.0, 4.3), (4.4, 4.7), (4.8, 5.2), (5.3, 12.5)],
    3:  [(1.0, 3.5), (3.6, 3.9), (4.0, 4.2), (4.3, 4.6), (4.7, 5.1), (5.2, 5.6), (5.7, 12.5)],
    4:  [(1.0, 3.7), (3.8, 4.1), (4.2, 4.5), (4.6, 5.0), (5.1, 5.4), (5.5, 6.1), (6.2, 12.5)],
    5:  [(1.0, 4.1), (4.2, 4.5), (4.6, 4.9), (5.0, 5.4), (5.5, 6.0), (6.1, 6.6), (6.7, 12.5)],
    6:  [(1.0, 4.5), (4.6, 4.9), (5.0, 5.4), (5.5, 6.0), (6.1, 6.6), (6.7, 7.3), (7.4, 12.5)],
    7:  [(1.0, 5.0), (5.1, 5.5), (5.6, 6.0), (6.1, 6.7), (6.8, 7.3), (7.4, 8.1), (8.2, 12.5)],
    8:  [(1.0, 5.7), (5.8, 6.2), (6.3, 6.8), (6.9, 7.5), (7.6, 8.2), (8.3, 9.1), (9.2, 12.5)],
    9:  [(1.0, 6.5), (6.6, 7.2), (7.3, 7.8), (7.9, 8.6), (8.7, 9.4), (9.5, 10.4), (10.5, 12.5)],
    10: [(1.0, 7.6), (7.7, 8.4), (8.5, 9.0), (9.1, 10.0), (10.1, 10.9), (11.0, 12.2), (12.3, 12.5)],
}
# The zone a race is usually run in — goal pace should sit here at the level you
# reach by race day. A coaching heuristic, not a Peloton rule.
RACE_PACE_ZONE = {"5k": "Hard", "10k": "Challenging", "half": "Challenging", "full": "Moderate"}


def chart_zones(level):
    """[{"name", "lo", "hi"} mph] for a Peloton level, from PELOTON_PACE_CHART."""
    return [{"name": name, "lo": lo, "hi": hi} for name, (lo, hi) in zip(PACE_ZONES, PELOTON_PACE_CHART[level])]


def _mph_to_pace(mph):
    sec = round(3600 / mph)
    return f"{sec // 60}:{sec % 60:02d}"


def zone_for_pace(level, pace_s):
    """The zone a pace (seconds/mile) falls in at a Peloton level: the last zone
    whose lower speed bound it reaches. "faster than Max" above 12.5 mph."""
    mph = 3600 / pace_s
    zones = chart_zones(level)
    if mph > zones[-1]["hi"]:
        return "faster than Max"
    if mph < zones[0]["lo"]:
        return "slower than Recovery"
    return [z for z in zones if mph >= z["lo"]][-1]["name"]


def race_pace_level(goal, pace_s):
    """The lowest Peloton level at which goal pace is no harder than the zone that
    race is usually run in (RACE_PACE_ZONE) — the level to grow into by race day.
    None when even Level 10 isn't enough."""
    zone = RACE_PACE_ZONE.get(goal)
    if not zone:
        return None
    limit = PACE_ZONES.index(zone)
    for level in PACE_LEVELS:
        z = zone_for_pace(level, pace_s)
        if z in PACE_ZONES and PACE_ZONES.index(z) <= limit:
            return level
    return None


def _fmt_min_pace(decimal_min):
    """Peloton zone paces are decimal minutes per mile (14.38 → '14:23')."""
    total = round(decimal_min * 60)
    return f"{total // 60}:{total % 60:02d}"


def latest_pace_level(user):
    """The Peloton pace level and zone paces from the user's most recent Tread run
    that recorded them (the performance graph carries only the user's own level).
    {"level", "zones": [{"name", "fast", "slow"} decimal min/mi], "date", "title"} or None."""
    for w in (CachedWorkout.objects.for_user(user).filter(discipline="running", source="peloton")
              .exclude(performance_graph_json__isnull=True).order_by("-created_at")[:40]):
        pg = w.performance_graph_json or {}
        label, zones = pg.get("pace_level"), pg.get("pace_zones") or []
        m = re.search(r"(\d+)", label or "")
        if m and zones:
            return {"level": int(m.group(1)), "date": _local_date(w.created_at), "title": w.title,
                    "zones": [{"name": z.get("name"), "fast": z.get("fast_pace"), "slow": z.get("slow_pace")}
                              for z in zones if z.get("fast_pace") and z.get("slow_pace")]}
    return None


def race_estimate(user, goal, today=None):
    """Rough current race time from recent training runs (Riegel formula), the
    fastest prediction over runs of 15+ min with a distance in the last 8 weeks.
    Training runs aren't all-out, so this tends to be conservative."""
    miles = RACE_MILES.get(goal)
    if not miles:
        return None
    today = today or timezone.localdate()
    best = None
    for w in _runs(user, today - timedelta(days=56)):
        if not w.distance_miles or w.distance_miles < 1 or _minutes(w) < 15:
            continue
        predicted = (w.duration_seconds or 0) * (miles / w.distance_miles) ** RIEGEL_EXPONENT
        if best is None or predicted < best["seconds"]:
            best = {"seconds": round(predicted), "from_miles": round(w.distance_miles, 2),
                    "from_seconds": w.duration_seconds, "from_date": _local_date(w.created_at).isoformat()}
    return best


def pace_profile(user, inputs, today=None):
    """Everything the plan needs about pace, JSON-ready (stored in inputs)."""
    today = today or timezone.localdate()
    level = latest_pace_level(user)
    runs8 = [w for w in _runs(user, today - timedelta(days=56)) if w.avg_pace_seconds and _minutes(w) >= 20]
    paces = sorted(w.avg_pace_seconds for w in runs8)
    profile = {
        "detected_level": level["level"] if level else None,
        "level": level["level"] if level else None,
        "level_from": f"{level['date']:%b %-d} · {level['title']}" if level else "",
        "race_level": None, "race_zone": RACE_PACE_ZONE.get(inputs.get("goal"), ""),
        "typical_pace_s": round(median(paces)) if paces else None,
        "fastest_pace_s": paces[0] if paces else None,
        "estimate": race_estimate(user, inputs.get("goal"), today),
        "goal_pace_s": None, "goal_zone": "", "gap_pct": None, "stretch": False, "long_run_min": None,
    }
    choice = inputs.get("pace_level_choice")
    if choice and choice != "auto":
        profile["level"] = int(choice)
    miles = RACE_MILES.get(inputs.get("goal"))
    target = inputs.get("target_time")
    if miles and target:
        profile["goal_pace_s"] = round(target / miles)
        if profile["level"]:
            profile["goal_zone"] = zone_for_pace(profile["level"], profile["goal_pace_s"])
        profile["race_level"] = race_pace_level(inputs.get("goal"), profile["goal_pace_s"])
        est = profile["estimate"]
        if est:
            profile["gap_pct"] = round(100 * (est["seconds"] - target) / est["seconds"], 1)
            weeks = inputs.get("weeks") or 1
            profile["stretch"] = profile["gap_pct"] > STRETCH_PCT_PER_4_WEEKS * weeks / 4
    if inputs.get("goal") in ("5k", "10k"):
        race_s = target or (profile["estimate"] or {}).get("seconds")
        if race_s:
            raw = max(LONG_RUN_FLOOR_MIN, LONG_RUN_RACE_MULTIPLE * race_s / 60)
            profile["long_run_min"] = _nearest_run_length(inputs, raw)
    return profile


def run_lengths_for_form():
    """Offered tread running lengths ≥ LONG_RUN_FLOOR_MIN, for the form's live long-run text."""
    return sorted(m for m in {round(sec / 60) for sec in menu_queryset("running", "tread")
                              .values_list("duration_seconds", flat=True).distinct()} if m >= LONG_RUN_FLOOR_MIN)


def _nearest_run_length(inputs, minutes):
    """The offered running class length nearest a target (ties → shorter, never
    under LONG_RUN_FLOOR_MIN) — a 50-min target is a 45-min class, not a 60."""
    lengths = sorted({round(sec / 60) for sec in menu_queryset("running", inputs.get("setting"))
                      .values_list("duration_seconds", flat=True).distinct()})
    lengths = [m for m in lengths if m >= LONG_RUN_FLOOR_MIN]
    if not lengths:
        return math.ceil(minutes / 5) * 5
    return min(lengths, key=lambda m: (abs(m - minutes), m))


def pace_lines(inputs):
    """PACE section of the fitness context (empty when there's nothing to say)."""
    p = inputs.get("pace") or {}
    out = []
    if p.get("level"):
        line = f"- Peloton pace level: Level {p['level']}"
        if p.get("detected_level") and p["level"] != p["detected_level"]:
            line += f" (chosen by the user; Peloton last reported Level {p['detected_level']})"
        elif p.get("level_from"):
            line += f" (from {p['level_from']})"
        out.append(line)
    if p.get("level"):
        out.append(f"- Their zones at Level {p['level']} (Peloton pace chart, min/mi): " + "; ".join(
            f"{z['name']} {_mph_to_pace(z['hi'])}–{_mph_to_pace(z['lo'])}"
            for z in chart_zones(p["level"]) if z["name"] not in ("Recovery", "Max")))
    if p.get("typical_pace_s"):
        out.append(f"- Runs of 20+ min, last 8 weeks: typical average pace {_fmt_pace(p['typical_pace_s'])}, "
                   f"fastest {_fmt_pace(p['fastest_pace_s'])}")
    est = p.get("estimate")
    goal_label = RACE_LABELS.get(inputs.get("goal"), "")
    if est:
        out.append(f"- Estimated current {goal_label} from training runs: {_fmt_hms(est['seconds'])} "
                   f"(Riegel formula from {est['from_miles']} mi in {_fmt_hms(est['from_seconds'])} on "
                   f"{est['from_date']}; training runs aren't all-out, so this is likely conservative)")
    if p.get("goal_pace_s"):
        line = f"- Goal: {goal_label} in {_fmt_hms(inputs['target_time'])} = {_fmt_pace(p['goal_pace_s'])}"
        if p.get("gap_pct") is not None:
            line += (f", {p['gap_pct']}% faster than the estimate" if p["gap_pct"] > 0
                     else f", already within the estimate ({-p['gap_pct']}% slower)")
        if p.get("goal_zone"):
            line += f"; at Level {p['level']} goal pace sits in their {p['goal_zone']} zone"
        out.append(line)
        if p.get("race_level") and p.get("level") and p["race_level"] <= p["level"]:
            out.append(f"- Pace level: their current Level {p['level']} already covers goal pace (it's their "
                       f"{p['goal_zone']} zone — no harder than the {p['race_zone']} zone a {goal_label} is usually "
                       f"run in). No level change is needed; stay at Level {p['level']}.")
        elif p.get("race_level"):
            line = (f"- Race-pace level: Level {p['race_level']} — the lowest level where goal pace is within the "
                    f"{p['race_zone']} zone, where a {goal_label} is usually run")
            if p.get("level"):
                steps = p["race_level"] - p["level"]
                line += f"; from Level {p['level']} that's {steps} level{'s' if steps != 1 else ''} to climb over " \
                        f"{inputs.get('weeks')} weeks"
            out.append(line)
        elif p.get("race_zone"):
            out.append(f"- Race-pace level: beyond Level 10 — goal pace is harder than the {p['race_zone']} zone "
                       "at every Peloton level")
        if p.get("stretch"):
            out.append(f"- STRETCH GOAL: that's more than ~{STRETCH_PCT_PER_4_WEEKS}% improvement per 4 weeks "
                       "of plan.")
    if p.get("long_run_min"):
        out.append(f"- Long run target: build to at least {p['long_run_min']} min at easy pace "
                   f"(longer than the race itself builds the endurance to hold pace to the finish).")
    return out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def default_start(today=None):
    today = today or timezone.localdate()
    return today if today.weekday() == 0 else today + timedelta(days=7 - today.weekday())


def companion_choices(user):
    """The user's programs with an active run (for Alongside mode)."""
    return [p for p in Program.objects.for_user(user).order_by("name") if p.active_run is not None]


def _int_in(raw, lo, hi, default):
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def clean_inputs(user, post, today=None):
    """Validate the form. Returns (inputs, errors). inputs is JSON-ready and
    carries the derived level assessment, runway and week count."""
    today = today or timezone.localdate()
    errors, inputs = [], {}

    goal = post.get("goal", "")
    if goal not in GOALS:
        errors.append("Choose a goal.")
    inputs["goal"] = goal

    try:
        start = date.fromisoformat(post.get("start_date")) if post.get("start_date") else default_start(today)
    except ValueError:
        start = None
        errors.append("Start date isn't a valid date.")
    if start and start < today:
        errors.append("Start date can't be in the past.")
    inputs["start_date"] = start.isoformat() if start else ""

    race = None
    if goal and goal != "base":
        try:
            race = date.fromisoformat(post.get("race_date")) if post.get("race_date") else None
        except ValueError:
            errors.append("Race date isn't a valid date.")
        if race is None and not any("Race date" in e for e in errors):
            errors.append("Race date is required.")
        elif start and race and (race - start).days < 21:
            errors.append("Race date must be at least 3 weeks after the start date.")
    inputs["race_date"] = race.isoformat() if race else ""

    if goal == "base":
        weeks = _int_in(post.get("weeks"), 4, 16, None)
        if weeks is None:
            errors.append("Plan length must be 4–16 weeks.")
    elif start and race:
        weeks = (_monday(race) - _monday(start)).days // 7 + 1
    else:
        weeks = None
    if weeks is not None and not (MIN_WEEKS <= weeks <= MAX_WEEKS):
        errors.append(f"Plans run {MIN_WEEKS}–{MAX_WEEKS} weeks; this one would be {weeks}.")
    inputs["weeks"] = weeks
    inputs["race_week"] = weeks if race else None
    inputs["race_weekday"] = race.isoweekday() if race else None

    try:
        target = _parse_time(post.get("target_time"))
    except ValueError:
        target = None
        errors.append("Target time should look like 28:30 or 1:55:00.")
    inputs["target_time"] = target

    days = sorted({int(d) for d in _getlist(post, "days") if str(d).isdigit() and 1 <= int(d) <= 7})
    if len(days) < 2:
        errors.append("Pick at least 2 training days.")
    inputs["days"] = days
    long_day = _int_in(post.get("long_day"), 1, 7, None) if post.get("long_day") else None
    if long_day is not None and long_day not in days:
        errors.append("The long-run day must be one of your training days.")
    inputs["long_day"] = long_day

    for key, default in (("max_weekday_min", 45), ("max_weekend_min", 75)):
        v = _int_in(post.get(key), 20, 120, default)
        if v is None:
            errors.append("Max session lengths must be 20–120 minutes.")
            v = default
        inputs[key] = v

    setting = post.get("setting") or "tread"
    if setting not in SETTINGS:
        errors.append("Choose tread, outdoor or either.")
    inputs["setting"] = setting

    mode = post.get("mode") or "standalone"
    if mode not in MODES:
        errors.append("Choose standalone or alongside.")
    inputs["mode"] = mode
    inputs["mobility"] = bool(post.get("mobility"))
    inputs["companion_program_id"] = None
    inputs["allow_doubles"] = False
    inputs["strength_per_week"] = 0
    if mode == "alongside":
        raw = post.get("companion_program_id")
        program = (Program.objects.for_user(user).filter(pk=raw).first()
                   if raw and str(raw).isdigit() else None)
        if program is None or program.active_run is None:
            errors.append("Choose one of your programs with an active cycle to plan around.")
        else:
            inputs["companion_program_id"] = program.pk
        inputs["allow_doubles"] = bool(post.get("allow_doubles"))
    else:
        spw = _int_in(post.get("strength_per_week"), 0, 3, 2)
        if spw is None:
            errors.append("Strength sessions per week must be 0–3.")
            spw = 2
        inputs["strength_per_week"] = spw

    assessment = assess_running_level(user, today)
    level_choice = post.get("level") or "auto"
    if level_choice not in ["auto"] + LEVELS:
        errors.append("Choose a starting level.")
        level_choice = "auto"
    inputs["level_choice"] = level_choice
    inputs["level"] = assessment["level"] if level_choice == "auto" else level_choice
    inputs["level_overridden"] = level_choice != "auto"
    inputs["level_assessment"] = assessment
    inputs["runway"] = runway_check(goal, weeks, inputs["level"]) if race and weeks else None

    notes = (post.get("notes") or "").strip()
    if len(notes) > 500:
        errors.append("Notes can be at most 500 characters.")
    inputs["notes"] = notes[:500]

    pace_choice = post.get("pace_level") or "auto"
    if pace_choice != "auto" and not (str(pace_choice).isdigit() and int(pace_choice) in PACE_LEVELS):
        errors.append("Choose a pace level from 1 to 10.")
        pace_choice = "auto"
    inputs["pace_level_choice"] = pace_choice
    if not errors:
        inputs["pace"] = pace_profile(user, inputs, today)
    return inputs, errors


# ---------------------------------------------------------------------------
# Companion program schedule (Alongside mode)
# ---------------------------------------------------------------------------

_STRENGTH_LIKE = {"strength", "circuit", "bootcamp", "bike_bootcamp", "caesar_bootcamp", "caesar"}
_LOWER_TITLE = ("lower body", "legs", "leg day", "glute", "hamstring", "quad", "squat", "lunge", "deadlift",
                "hip thrust", "calf", "booty")
_UPPER_TITLE = ("upper body", "arms", "back", "chest", "shoulder", "bicep", "tricep", "push", "pull",
                "press", "row", "lat")
_FULL_TITLE = ("full body", "total body", "full-body")


def body_flag(title, discipline):
    """'[lower body]' / '[upper body]' / '[full body]' / '' from a class title.
    Word-start matching, so 'Pilates' doesn't read as 'lat'."""
    if discipline not in _STRENGTH_LIKE:
        return ""
    from .programs import _LOWER_BODY_KEYWORDS, _UPPER_BODY_KEYWORDS
    t = (title or "").lower()

    def hit(words):
        return any(re.search(r"\b" + re.escape(w), t) for w in words)
    if hit(_FULL_TITLE):
        return "full body"
    lower = hit(_LOWER_TITLE + _LOWER_BODY_KEYWORDS)
    upper = hit(_UPPER_TITLE + _UPPER_BODY_KEYWORDS)
    if lower and upper:
        return "full body"
    return "lower body" if lower else "upper body" if upper else ""


def _slot_label(slot):
    if slot.match_discipline and not slot.peloton_ride_id:
        kind = slot.match_title_keyword.strip().title() or slot.match_discipline.title()
        return f"{kind} (any class)"
    return slot.title


def companion_schedule(user, inputs):
    """{plan_week: [{"day", "title", "discipline", "duration_min", "body", "optional"}]}
    — what the companion program has on each plan week's dates. Not stored;
    used by the context builder and the review page."""
    pid = inputs.get("companion_program_id")
    if inputs.get("mode") != "alongside" or not pid:
        return {}
    program = Program.objects.for_user(user).filter(pk=pid).first()
    if program is None:
        return {}
    weeks = {w.number: w for w in program.weeks.prefetch_related("slots")}
    if not weeks:
        return {}
    numbers = sorted(weeks)
    run = program.active_run
    out = {}
    for plan_week in range(1, (inputs.get("weeks") or 0) + 1):
        rows = []
        for d in week_dates(inputs, plan_week):
            if program.kind == "split" or len(numbers) == 1 or run is None:
                cw = weeks[numbers[0]]
            else:
                n = min(max((d - run.start_date).days // 7 + 1, numbers[0]), numbers[-1])
                cw = weeks.get(n) or weeks[min((x for x in numbers if x >= n), default=numbers[-1])]
            for slot in cw.slots.all():
                if slot.day == d.isoweekday():
                    rows.append({"day": slot.day, "title": _slot_label(slot),
                                 "discipline": slot.discipline or slot.match_discipline,
                                 "duration_min": slot.duration_min,
                                 "body": body_flag(slot.title, slot.discipline or slot.match_discipline),
                                 "optional": slot.optional})
        out[plan_week] = sorted(rows, key=lambda r: r["day"])
    return out


# ---------------------------------------------------------------------------
# Fitness context
# ---------------------------------------------------------------------------

def _fmt_pace(sec):
    return f"{int(sec) // 60}:{int(sec) % 60:02d}/mi" if sec else ""


def build_fitness_context(user, inputs, today=None) -> str:
    """Plain-text FITNESS CONTEXT for the plan prompt. Every number comes from
    this user's rows, so the AI's claims can be checked against it."""
    from .ai import build_persona_block

    today = today or timezone.localdate()
    out = []
    runs12 = _runs(user, _monday(today) - timedelta(weeks=11))
    type_names = _class_type_names({w.ride_id for w in runs12})
    walks12 = (CachedWorkout.objects.for_user(user)
               .filter(discipline="walking", created_at__date__gte=_monday(today) - timedelta(weeks=11)))
    walk_weeks = Counter(_monday(_local_date(w.created_at)) for w in walks12)

    out.append("RUNNING — LAST 12 WEEKS (week starting Monday: runs · minutes · miles · longest run · walks)")
    by_week = defaultdict(list)
    for w in runs12:
        by_week[_monday(_local_date(w.created_at))].append(w)
    for i in range(11, -1, -1):
        wk = _monday(today) - timedelta(weeks=i)
        ws = by_week.get(wk, [])
        miles = sum(w.distance_miles or 0 for w in ws)
        longest = max((_minutes(w) for w in ws), default=0)
        line = f"- {wk:%b %-d}: {len(ws)} runs · {round(sum(_minutes(w) for w in ws))} min"
        if miles:
            line += f" · {miles:.1f} mi"
        if ws:
            line += f" · longest {round(longest)} min"
        if walk_weeks.get(wk):
            line += f" · {walk_weeks[wk]} walks"
        if i == 0:
            line += " (current week, partial)"
        out.append(line)
    if len(runs12) < 3:
        out.append("Running history is thin — fewer than 3 runs in 12 weeks.")

    recent = list(CachedWorkout.objects.for_user(user).filter(discipline__in=RUN_DISCIPLINES)
                  .order_by("-created_at")[:10])
    if recent:
        out.append("")
        out.append("RECENT RUNS (newest first: date · title · minutes · miles · pace · avg HR)")
        for w in recent:
            bits = [f"{_local_date(w.created_at):%Y-%m-%d}", w.title or "Run", f"{round(_minutes(w))} min"]
            if w.distance_miles:
                bits.append(f"{w.distance_miles:.2f} mi")
            if w.avg_pace_seconds:
                bits.append(_fmt_pace(w.avg_pace_seconds))
            hr = w.heart_rate_avg_best
            if hr:
                bits.append(f"HR {round(hr)}")
            out.append("- " + " · ".join(bits))

    d56, d28 = today - timedelta(days=56), today - timedelta(days=28)
    last8 = [w for w in runs12 if _local_date(w.created_at) > d56]
    cont8 = [_minutes(w) for w in last8 if _continuous(w, type_names)]
    paces = [w.avg_pace_seconds for w in last8 if w.avg_pace_seconds and _minutes(w) >= 20]
    out.append("")
    out.append("BENCHMARKS")
    out.append(f"- Longest continuous run, last 8 weeks: {round(max(cont8))} min" if cont8
               else "- Longest continuous run, last 8 weeks: none")
    if paces:
        out.append(f"- Fastest average pace on a run of 20+ min, last 8 weeks: {_fmt_pace(min(paces))}")
    out.append(f"- Average runs/week, last 4 weeks: "
               f"{round(sum(1 for w in runs12 if _local_date(w.created_at) > d28) / 4, 1)}")

    other = (CachedWorkout.objects.for_user(user).filter(created_at__date__gt=d28)
             .exclude(discipline__in=RUN_DISCIPLINES).values("discipline").annotate(n=Count("id")).order_by("-n"))
    if other:
        out.append("")
        out.append("OTHER TRAINING — LAST 4 WEEKS (sessions/week)")
        for row in other:
            out.append(f"- {row['discipline'] or 'other'}: {row['n'] / 4:.1f}")

    stats14 = list(DailyStats.objects.for_user(user).filter(date__gt=today - timedelta(days=14), date__lte=today))
    stats60 = list(DailyStats.objects.for_user(user).filter(date__gt=today - timedelta(days=60), date__lte=today))
    rec = []
    ready = [s.readiness_score for s in stats14 if s.readiness_score is not None]
    if ready:
        est = any(s.readiness_is_computed for s in stats14 if s.readiness_score is not None)
        rec.append(f"- Readiness, 14-day mean: {round(mean(ready))}/100"
                   + (" (Google Health estimate, not Garmin's score)" if est else ""))
    rhr14 = [s.resting_hr for s in stats14 if s.resting_hr]
    rhr60 = [s.resting_hr for s in stats60 if s.resting_hr]
    if rhr14:
        line = f"- Resting HR: 14-day mean {round(mean(rhr14))} bpm"
        if rhr60:
            line += f" vs 60-day mean {round(mean(rhr60))} bpm"
        rec.append(line)
    hrv = next((s.hrv_weekly_avg for s in sorted(stats14, key=lambda s: s.date, reverse=True) if s.hrv_weekly_avg),
               None)
    if hrv:
        rec.append(f"- HRV weekly average (latest): {round(hrv)} ms")
    status = next((s.training_status for s in sorted(stats14, key=lambda s: s.date, reverse=True)
                   if s.training_status), "")
    if status:
        rec.append(f"- Latest training status: {status}")
    if rec:
        out.append("")
        out.append("RECOVERY — LAST 14 DAYS")
        out.extend(rec)

    persona = build_persona_block(user, include_interventions=False)
    if persona:
        out.append("")
        out.append("ATHLETE PROFILE")
        out.append(persona)

    schedule = companion_schedule(user, inputs)
    if schedule:
        program = Program.objects.for_user(user).get(pk=inputs["companion_program_id"])
        out.append("")
        out.append(f"COMPANION PROGRAM — {program.name} (they keep doing this; context only, don't output it)")
        same = len({tuple((r["day"], r["title"]) for r in rows) for rows in schedule.values()}) == 1
        for week, rows in schedule.items():
            if same and week > 1:
                break
            label = "Every week" if same else f"Week {week}"
            if not rows:
                out.append(f"- {label}: nothing scheduled")
                continue
            parts = []
            for r in rows:
                bit = f"{DAY_NAMES[r['day']]} {r['title']}"
                meta = [x for x in (r["discipline"], f"{r['duration_min']} min" if r["duration_min"] else "") if x]
                if meta:
                    bit += f" ({', '.join(meta)})"
                if r["body"]:
                    bit += f" [{r['body']}]"
                if r["optional"]:
                    bit += " (optional)"
                parts.append(bit)
            out.append(f"- {label}: " + "; ".join(parts))

    pace = pace_lines(inputs)
    if pace:
        out.append("")
        out.append("PACE")
        out.extend(pace)

    a = inputs.get("level_assessment") or {}
    out.append("")
    out.append("STARTING LEVEL")
    out.append(f"- Level: {inputs.get('level')}"
               + (f" (chosen by the user; the data suggested {a.get('level')})" if inputs.get("level_overridden")
                  else " (assessed from their history)"))
    for e in a.get("evidence", []):
        out.append(f"- {e}")
    if a.get("profile_experience"):
        out.append(f"- Profile says running experience: {a['profile_experience']}")
    out.append(f"- {runway_line(inputs)}")

    by_type = defaultdict(list)
    for w in last8:
        if w.ride_id:
            by_type[w.ride_id].append(w)
    diffs = defaultdict(list)
    if by_type:
        for c in PelotonClass.objects.filter(ride_id__in=list(by_type), difficulty_estimate__isnull=False):
            diffs[type_names.get(c.ride_id) or "Unknown type"].extend([c.difficulty_estimate] * len(by_type[c.ride_id]))
    if diffs:
        out.append("")
        out.append("RECENT CLASS DIFFICULTY (member-rated, relative to each rater's own fitness — not an ability score)")
        for name, vals in sorted(diffs.items()):
            out.append(f"- {name}: median {median(vals):.1f}, max {max(vals):.1f} over {len(vals)} classes")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Catalog menu
# ---------------------------------------------------------------------------

_JUNK_TYPE = ("do not use", "don't use", "delete", "not a class type")
MIN_TYPE_CLASSES = 5          # a class type needs this many available classes to be offered
MIN_DURATION_CLASSES = 2      # ...and a duration this many


def menu_disciplines(inputs):
    """Menu discipline keys: catalog disciplines plus the virtual 'pilates'."""
    discs = ["running", "walking", "stretching"]
    if inputs["mode"] == "standalone":
        if inputs.get("strength_per_week"):
            discs.append("strength")
        if inputs.get("mobility"):
            discs += ["pilates", "yoga"]
        discs.append("cycling")
    elif inputs.get("mobility"):
        discs.append("yoga")
    return discs


def menu_queryset(discipline, setting=None):
    """Available English classes for a menu discipline (pilates = strength classes
    seen under the pilates category; strength excludes them)."""
    qs = PelotonClass.objects.filter(is_available=True, language="english")
    if discipline == "pilates":
        qs = qs.filter(discipline="strength", categories__contains=",pilates,")
    elif discipline == "strength":
        qs = qs.filter(discipline="strength").exclude(categories__contains=",pilates,")
    else:
        qs = qs.filter(discipline=discipline)
    if discipline in ("running", "walking") and setting in ("tread", "outdoor"):
        qs = qs.filter(is_outdoor=(setting == "outdoor"))
    return qs


def catalog_menu(inputs):
    """What the AI may choose from. Returns (menu_text, allowed) where allowed =
    {"types": {(discipline, class_type_id): {"name", "durations", "by_setting",
    "settings"}}, "names": {(discipline, lower name): class_type_id},
    "early_weeks": n}."""
    max_len = max(inputs["max_weekday_min"], inputs["max_weekend_min"])
    level, weeks = inputs.get("level"), inputs.get("weeks") or 0
    early = early_weeks(weeks)
    type_names = dict(PelotonClassType.objects.values_list("id", "name"))
    types, names, lines = {}, {}, []
    for disc in menu_disciplines(inputs):
        settings = (["tread", "outdoor"] if inputs["setting"] == "either" else [inputs["setting"]]) \
            if disc in ("running", "walking") else [None]
        for setting in settings:
            rows = (menu_queryset(disc, setting).values("class_type_id", "duration_seconds")
                    .annotate(n=Count("ride_id")))
            per_type = defaultdict(dict)
            for r in rows:
                if r["class_type_id"]:
                    m = round(r["duration_seconds"] / 60)
                    per_type[r["class_type_id"]][m] = per_type[r["class_type_id"]].get(m, 0) + r["n"]
            for tid, durs in sorted(per_type.items(), key=lambda kv: type_names.get(kv[0], "")):
                name = type_names.get(tid, "")
                if not name or any(j in name.lower() for j in _JUNK_TYPE) or sum(durs.values()) < MIN_TYPE_CLASSES:
                    continue
                offered = {m: n for m, n in sorted(durs.items()) if n >= MIN_DURATION_CLASSES and 5 <= m <= max_len}
                if not offered:
                    continue
                entry = types.setdefault((disc, tid), {"name": name, "durations": [], "by_setting": {},
                                                        "settings": []})
                entry["by_setting"][setting or "any"] = sorted(offered)
                entry["durations"] = sorted(set(entry["durations"]) | set(offered))
                if setting:
                    entry["settings"].append(setting)
                names[(disc, name.lower())] = tid
                tag = ""
                if disc == "running" and level in EARLY_GATE:
                    if early_ok(level, name):
                        cap = EARLY_GATE[level].get(run_type_tag(name))
                        tag = f" [early OK{f' ≤ {cap} min' if cap else ''}]"
                    else:
                        tag = f" [from week {early + 1}]"
                setting_txt = f" | {setting}" if setting and inputs["setting"] == "either" else ""
                durs_txt = ", ".join(f"{m} min ×{n}" for m, n in offered.items())
                lines.append(f"{disc}{setting_txt} | {name} ({tid}) | {durs_txt}{tag}")
    header = []
    if level in EARLY_GATE:
        header.append(f"Early weeks for a {level} start: weeks 1–{early}. Running types marked [from week {early + 1}] "
                      f"may only be used from week {early + 1}.")
    return "\n".join(header + lines), {"types": types, "names": names, "early_weeks": early}


# ---------------------------------------------------------------------------
# Validation of the AI's spec (04)
# ---------------------------------------------------------------------------

class PlanSpecInvalid(ValueError):
    """The AI's plan needed too much repair to show."""


INTENSITIES = ("easy", "moderate", "hard")
PHASES = ("base", "build", "peak", "lighter", "taper", "race")
MAX_DROPPED_SHARE = 0.25      # more than this share of slots dropped → the draft fails with Retry
_ALONGSIDE_DISCS = {"running", "walking", "stretching", "yoga"}
# Level-gate replacements, tried in order: a gated type → the closest early-OK type.
_GATE_FALLBACK = {"intervals": ["endurance", "beginner", "walk_run"], "speed": ["endurance", "beginner", "walk_run"],
                  "hill": ["endurance", "beginner", "walk_run"], "endurance": ["beginner", "walk_run", "basics"]}


def _day_max(inputs, day):
    return inputs["max_weekend_min"] if day in (6, 7) else inputs["max_weekday_min"]


def _snap_duration(offered, wanted, day_max, prefer_up=False):
    """An offered class length for a wanted one. In between two lengths: the
    longer when prefer_up (build weeks — keeps the progression moving), else the
    nearer (ties → shorter). Never over the day's max; None when nothing fits."""
    fits = [m for m in offered if m <= day_max]
    if not fits:
        return None
    if wanted in fits:
        return wanted
    longer = [m for m in fits if m > wanted]
    if prefer_up and longer:
        return min(longer)
    return min(fits, key=lambda m: (abs(m - wanted), m))


def _clip(text, limit):
    """Shorten to the last full sentence (or word) within limit — never mid-word."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…" if " " in cut else cut + "…"


def _resolve_type(slot, allowed):
    disc = (slot.get("discipline") or "").strip().lower()
    tid = (slot.get("class_type_id") or "").strip()
    if (disc, tid) in allowed["types"]:
        return disc, tid
    name = (slot.get("class_type") or "").strip().lower()
    if (disc, name) in allowed["names"]:
        return disc, allowed["names"][(disc, name)]
    if name:   # "Endurance" vs "Endurance Running": a unique containment match within the discipline
        hits = {t for (d, n), t in allowed["names"].items() if d == disc and (name in n or n in name)}
        if len(hits) == 1:
            return disc, hits.pop()
    return disc, None


def _gate_replacement(allowed, level, entry_name, duration, setting):
    tag = run_type_tag(entry_name)
    for want in _GATE_FALLBACK.get(tag, ["endurance", "beginner", "walk_run"]):
        for (d, tid), e in sorted(allowed["types"].items(), key=lambda kv: kv[1]["name"]):
            if d != "running" or run_type_tag(e["name"]) != want:
                continue
            durs = e["by_setting"].get(setting or "any") or e["durations"]
            if duration in durs and early_ok(level, e["name"], duration):
                return tid, e
    return None, None


def validate_spec(spec, inputs, allowed, first_week=1, kept_longest=0):
    """Repair what's cheap, drop what isn't, and return (clean_spec, warnings).
    Raises PlanSpecInvalid when too much was dropped or a week has no run.
    A reassessment validates only weeks first_week…; kept_longest is the longest
    run in the weeks it keeps (for the long-run check)."""
    warnings = []
    weeks_wanted = inputs["weeks"]
    start, race = plan_dates(inputs)
    early = allowed.get("early_weeks") or early_weeks(weeks_wanted)
    level = inputs.get("level")
    by_number = {}
    for wk in spec.get("weeks") or []:
        try:
            n = int(wk.get("number"))
        except (TypeError, ValueError):
            continue
        by_number.setdefault(n, wk)
    extra = sorted(n for n in by_number if not first_week <= n <= weeks_wanted)
    if extra:
        warnings.append(f"Dropped week(s) {', '.join(map(str, extra))} outside weeks {first_week}–{weeks_wanted}.")
    missing = [n for n in range(first_week, weeks_wanted + 1) if n not in by_number]
    if missing:
        raise PlanSpecInvalid(f"The AI's plan is missing week(s) {', '.join(map(str, missing))}.")

    total = dropped = 0
    rounded = []          # "Week 3 Sun 35→30" — reported as one line
    clean_weeks = []
    for n in range(first_week, weeks_wanted + 1):
        wk = by_number[n]
        phase = wk.get("phase") if wk.get("phase") in PHASES else "build"
        dates = week_dates(inputs, n)
        slots_out, run_days = [], set()
        raw_slots = sorted(wk.get("slots") or [], key=lambda s: (s.get("day") or 0, s.get("order") or 0))
        for s in raw_slots:
            total += 1
            label = f"Week {n} {DAY_NAMES.get(s.get('day'), s.get('day'))}"
            try:
                day = int(s.get("day"))
            except (TypeError, ValueError):
                day = None
            if day not in inputs["days"]:
                warnings.append(f"{label}: dropped a session on a day you didn't make available.")
                dropped += 1
                continue
            d = dates[day - 1]
            if d < start:
                warnings.append(f"{label}: dropped a session before the plan's start date.")
                dropped += 1
                continue
            if race and d >= race:
                warnings.append(f"{label}: dropped a session on or after race day.")
                dropped += 1
                continue
            disc, tid = _resolve_type(s, allowed)
            if inputs["mode"] == "alongside" and (disc not in _ALONGSIDE_DISCS
                                                  or (disc == "yoga" and not inputs.get("mobility"))):
                warnings.append(f"{label}: dropped a {disc or 'non-running'} session — alongside plans only add runs, "
                                "walks, stretches" + (" and yoga." if inputs.get("mobility") else "."))
                dropped += 1
                continue
            if tid is None:
                warnings.append(f"{label}: dropped '{s.get('class_type') or '?'}' ({disc or 'no discipline'}) — "
                                "not a class type in the catalog.")
                dropped += 1
                continue
            entry = allowed["types"][(disc, tid)]
            setting = None
            if disc in ("running", "walking"):
                setting = s.get("setting") if s.get("setting") in entry["settings"] else (
                    inputs["setting"] if inputs["setting"] in entry["settings"] else entry["settings"][0])
            offered = entry["by_setting"].get(setting or "any") or entry["durations"]
            try:
                wanted = int(s.get("duration_min"))
            except (TypeError, ValueError):
                wanted = offered[0]
            prefer_up = disc == "running" and n > early and phase not in ("lighter", "taper", "race")
            duration = _snap_duration(offered, wanted, _day_max(inputs, day), prefer_up)
            if duration is None:
                warnings.append(f"{label}: dropped {entry['name']} — no length fits the day's max.")
                dropped += 1
                continue
            if duration != wanted:
                rounded.append(f"{label} {wanted}→{duration}")
            if disc == "running" and n <= early and not early_ok(level, entry["name"], duration):
                new_tid, new_entry = _gate_replacement(allowed, level, entry["name"], duration, setting)
                if new_tid is None:
                    warnings.append(f"{label}: dropped {entry['name']} — too advanced for a {level} start in week {n}.")
                    dropped += 1
                    continue
                warnings.append(f"{label}: {entry['name']} → {new_entry['name']} (a {level} start uses gentler "
                                f"types until week {early + 1}).")
                tid, entry = new_tid, new_entry
            if disc == "running":
                if day in run_days:
                    warnings.append(f"{label}: dropped a second run on the same day.")
                    dropped += 1
                    continue
                run_days.add(day)
            intensity = s.get("intensity") if s.get("intensity") in INTENSITIES else "moderate"
            zone = next((z for z in PACE_ZONES if z.lower() == str(s.get("pace_zone") or "").strip().lower()), "")
            if disc not in ("running", "walking"):
                zone = ""
            optional = bool(s.get("optional"))
            if inputs["mode"] == "alongside" and disc == "yoga":
                optional = True
            slots_out.append({
                "day": day, "order": 0, "discipline": disc, "class_type": entry["name"], "class_type_id": tid,
                "duration_min": duration, "intensity": intensity, "setting": setting, "pace_zone": zone,
                "purpose": _clip(s.get("purpose"), 200), "optional": optional,
            })
        per_day = Counter()
        for slot in slots_out:
            slot["order"] = per_day[slot["day"]]
            per_day[slot["day"]] += 1
        if not any(sl["discipline"] == "running" for sl in slots_out) and n != inputs.get("race_week"):
            raise PlanSpecInvalid(f"Week {n} ended up with no runs. " + "; ".join(warnings[:4]))
        clean_weeks.append({"number": n, "phase": phase, "focus": _clip(wk.get("focus"), 160),
                            "slots": slots_out})
    if total and dropped / total > MAX_DROPPED_SHARE:
        raise PlanSpecInvalid(f"The AI's plan needed {dropped} of {total} sessions dropped. "
                              + "; ".join(warnings[:4]))
    if rounded:
        warnings.insert(0, f"Rounded {len(rounded)} session length{'s' if len(rounded) != 1 else ''} to Peloton "
                           f"class lengths (up in build weeks, down early on and in lighter/taper weeks): "
                           + ", ".join(rounded) + " min.")
    long_min = (inputs.get("pace") or {}).get("long_run_min")
    if long_min:
        longest = max([sl["duration_min"] for wk in clean_weeks for sl in wk["slots"]
                       if sl["discipline"] == "running"] + [kept_longest])
        reachable = min(long_min, inputs["max_weekend_min"] if inputs.get("long_day") in (6, 7, None)
                        else inputs["max_weekday_min"])
        if longest < reachable:
            warnings.append(f"The longest run peaks at {longest} min; for this race, building to {long_min}+ min "
                            "at easy pace would help you hold pace to the finish.")
    clean = {"plan_name": str(spec.get("plan_name") or "Training plan")[:120],
             "summary": str(spec.get("summary") or ""),
             "assumptions": [str(a) for a in (spec.get("assumptions") or []) if a][:8],
             "pace_guidance": _clip(spec.get("pace_guidance"), 900),
             "changes": [_clip(c, 300) for c in (spec.get("changes") or []) if c][:6],
             "weeks": clean_weeks}
    return clean, warnings


def slot_key(week, slot):
    return f"{week}-{slot['day']}-{slot['order']}"


def iter_slots(spec):
    for wk in spec.get("weeks", []):
        for slot in wk["slots"]:
            yield wk["number"], slot


# ---------------------------------------------------------------------------
# Picking real classes (05)
# ---------------------------------------------------------------------------

RECENT_REPEAT_DAYS = 60         # classes taken this recently are never picked again
REPEAT_AGE_PENALTY = timedelta(days=180)   # a class you've done before ranks as if aired this much earlier
SAME_INSTRUCTOR_PENALTY = timedelta(days=30)   # per class by the same instructor already picked that week
FAMILIAR_BONUS = timedelta(days=60)        # instructor taught ≥ FAMILIAR_MIN of your workouts in this discipline lately
FAMILIAR_MIN, FAMILIAR_DAYS = 2, 180
MIN_RATING = 0.90               # smoothed rating floor (applied only if ≥ MIN_LEFT candidates survive it)
RATING_PRIOR, RATING_PRIOR_N = 0.95, 20   # Bayesian prior for the smoothed rating
MIN_DIFFICULTY_RATINGS = 10     # difficulty from fewer ratings than this is too noisy
MIN_LEFT = 3
ALTERNATES = 8
# Intensity → difficulty percentile band within one class type + duration (overlapping).
BANDS = {"easy": (0, 40), "moderate": (30, 70), "hard": (60, 100)}
LEVEL_CLAMP_PCT = 50            # beginner/returning, first third of the plan: lowest 50% only
PROGRESSION_START_DROP = 20     # easy/moderate runs: week 1's band top sits this far below normal…
PROGRESSION_STEP = 5            # …and rises this much a week, back up to the normal top


def _smoothed(c):
    avg = c.overall_rating_avg if c.overall_rating_avg is not None else RATING_PRIOR
    n = c.overall_rating_count or 0
    return (avg * n + RATING_PRIOR * RATING_PRIOR_N) / (n + RATING_PRIOR_N)


def _quantile(sorted_vals, pct):
    if not sorted_vals:
        return None
    idx = (len(sorted_vals) - 1) * pct / 100
    lo, hi = math.floor(idx), math.ceil(idx)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


class PickContext:
    """Everything computed once per plan (or per swap): exclusions, history, familiarity."""

    def __init__(self, user, level, weeks, exclude_program=None, today=None):
        from .programs import program_ride_ids
        self.user, self.level, self.weeks = user, level, weeks
        self.early = early_weeks(weeks or 1)
        self.today = today or timezone.localdate()
        self.pins = set()
        for p in Program.objects.for_user(user):
            if exclude_program is None or p.pk != exclude_program.pk:
                self.pins |= program_ride_ids(p)
        self.last_taken = {}
        familiar = defaultdict(Counter)
        cutoff = self.today - timedelta(days=FAMILIAR_DAYS)
        for ride_id, created, instr, disc in (CachedWorkout.objects.for_user(user).exclude(ride_id="")
                                              .values_list("ride_id", "created_at", "instructor_name", "discipline")):
            d = _local_date(created)
            if d and (ride_id not in self.last_taken or d > self.last_taken[ride_id]):
                self.last_taken[ride_id] = d
            if d and d >= cutoff and instr:
                familiar[disc][instr] += 1
        self.familiar = {disc: {i for i, n in c.items() if n >= FAMILIAR_MIN} for disc, c in familiar.items()}
        self.used = set()
        self.week_instructors = defaultdict(Counter)   # plan week → instructor → picks
        self._cache = {}

    def recent(self, ride_id):
        d = self.last_taken.get(ride_id)
        return d is not None and (self.today - d).days < RECENT_REPEAT_DAYS

    def candidates(self, disc, setting, tid, duration):
        key = (disc, setting, tid, duration)
        if key not in self._cache:
            self._cache[key] = list(menu_queryset(disc, setting).filter(
                class_type_id=tid, duration_seconds__gte=duration * 60 - 30, duration_seconds__lte=duration * 60 + 30))
        return self._cache[key]

    def durations(self, disc, setting, tid):
        return sorted({round(s / 60) for s in menu_queryset(disc, setting).filter(class_type_id=tid)
                       .values_list("duration_seconds", flat=True)})


def _band(spec_slot, week, ctx):
    lo, hi = BANDS.get(spec_slot.get("intensity"), BANDS["moderate"])
    if spec_slot.get("discipline") == "running" and spec_slot.get("intensity") in ("easy", "moderate"):
        hi = min(hi, max(lo + 10, hi - PROGRESSION_START_DROP + PROGRESSION_STEP * (week - 1)))
    if ctx.level in ("beginner", "returning") and week <= ctx.early:
        hi = min(hi, LEVEL_CLAMP_PCT)
        lo = min(lo, max(0, hi - 40))
    return lo, hi


def rank_candidates(spec_slot, week, ctx, exclude=()):
    """Ranked candidate classes for one spec (best first), with the duration used."""
    disc, tid, setting = spec_slot["discipline"], spec_slot["class_type_id"], spec_slot.get("setting")
    duration = spec_slot["duration_min"]
    pool = ctx.candidates(disc, setting, tid, duration)
    if not pool:
        for d in sorted(ctx.durations(disc, setting, tid), key=lambda m: (abs(m - duration), m)):
            pool = ctx.candidates(disc, setting, tid, d)
            if pool:
                duration = d
                break
    if not pool:
        return [], duration
    diffs = sorted(c.difficulty_estimate for c in pool if c.difficulty_estimate is not None)
    blocked = ctx.used | set(exclude)
    left = [c for c in pool if c.ride_id not in ctx.pins and c.ride_id not in blocked and not ctx.recent(c.ride_id)]

    lo, hi = _band(spec_slot, week, ctx)

    def in_band(c, lo, hi):
        if c.difficulty_estimate is None or not diffs:
            return lo <= 70 and hi >= 30      # unknown difficulty counts as moderate
        return _quantile(diffs, lo) <= c.difficulty_estimate <= _quantile(diffs, hi)
    chosen = [c for c in left if in_band(c, lo, hi)]
    steps = 0
    while not chosen and steps < 10 and (lo > 0 or hi < 100):
        lo, hi, steps = max(0, lo - 10), min(100, hi + 10), steps + 1
        chosen = [c for c in left if in_band(c, lo, hi)]
    if not chosen:
        chosen = left

    if disc == "running" and setting != "outdoor":
        # Tread classes with pace targets show your zone paces on screen — what pace work needs.
        paced = [c for c in chosen if c.has_tread_pace_target]
        if len(paced) >= MIN_LEFT:
            chosen = paced
    rated = [c for c in chosen if _smoothed(c) >= MIN_RATING]
    if len(rated) >= MIN_LEFT:
        chosen = rated
    counted = [c for c in chosen if (c.difficulty_rating_count or 0) >= MIN_DIFFICULTY_RATINGS]
    if len(counted) >= MIN_LEFT:
        chosen = counted

    familiar = ctx.familiar.get(disc if disc != "pilates" else "strength", set())

    def key(c):
        eff = c.original_air_time
        if c.ride_id in ctx.last_taken:
            eff -= REPEAT_AGE_PENALTY
        eff -= SAME_INSTRUCTOR_PENALTY * ctx.week_instructors[week][c.instructor_name]
        if c.instructor_name and c.instructor_name in familiar:
            eff += FAMILIAR_BONUS
        return (eff, _smoothed(c), c.ride_id)
    ranked = sorted(chosen, key=key, reverse=True)
    # Alternates beyond the band, so Swap/Choose always have options; the top
    # pick still comes from the band.
    chosen_ids = {c.ride_id for c in chosen}
    rest = sorted((c for c in left if c.ride_id not in chosen_ids), key=key, reverse=True)
    return ranked + rest, duration


def _pick_entry(ranked, duration, spec_slot, ctx):
    if not ranked:
        return {"ride_id": None, "alternates": []}
    top = ranked[0]
    entry = {"ride_id": top.ride_id, "alternates": [c.ride_id for c in ranked[1:1 + ALTERNATES]]}
    if top.ride_id in ctx.last_taken:
        entry["repeat"] = True
        entry["last_taken"] = ctx.last_taken[top.ride_id].isoformat()
    if duration != spec_slot["duration_min"]:
        entry["duration_min"] = duration
    return entry


def pick_classes(user, spec, inputs, today=None, exclude_program=None, used=()):
    """{"<week>-<day>-<order>": {"ride_id", "alternates", ["repeat", "last_taken"]}}
    for every slot. Deterministic for the same inputs, catalog and history.
    A reassessment passes its program (whose pins aren't "another program's")
    and the ride ids it keeps, so no class appears twice in the plan."""
    ctx = PickContext(user, inputs.get("level"), inputs.get("weeks"), exclude_program=exclude_program, today=today)
    ctx.used |= set(used)
    picks = {}
    for week, slot in iter_slots(spec):
        ranked, duration = rank_candidates(slot, week, ctx)
        entry = _pick_entry(ranked, duration, slot, ctx)
        picks[slot_key(week, slot)] = entry
        if entry["ride_id"]:
            ctx.used.add(entry["ride_id"])
            ctx.week_instructors[week][ranked[0].instructor_name] += 1
    return picks


def classes_by_id(ride_ids):
    return {c.ride_id: c for c in PelotonClass.objects.filter(ride_id__in=[r for r in ride_ids if r])}


def draft_ride_ids(draft):
    return {p["ride_id"] for p in (draft.picks_json or {}).values() if p.get("ride_id")}


def swap_pick(draft, key):
    """Move a slot to its next alternate not used anywhere in the draft (cycling
    through the list). Returns the new ride id or None."""
    pick = (draft.picks_json or {}).get(key)
    if not pick:
        return None
    options = ([pick["ride_id"]] if pick.get("ride_id") else []) + pick.get("alternates", [])
    used = draft_ride_ids(draft) - {pick.get("ride_id")}
    if not options:
        return None
    start = options.index(pick["ride_id"]) if pick.get("ride_id") in options else -1
    for step in range(1, len(options) + 1):
        candidate = options[(start + step) % len(options)]
        if candidate not in used and candidate != pick.get("ride_id"):
            return set_pick(draft, key, candidate, options)
    return None


def set_pick(draft, key, ride_id, options=None):
    """Point a slot at ride_id (which must be its current pick or an alternate).
    Keeps every option in the alternates list so Swap can cycle back."""
    pick = draft.picks_json[key]
    options = options or ([pick["ride_id"]] if pick.get("ride_id") else []) + pick.get("alternates", [])
    if ride_id not in options:
        raise ValueError("not one of this slot's alternates")
    i = options.index(ride_id)
    pick["alternates"] = options[i + 1:] + options[:i]   # rotate, so the next Swap moves on rather than back
    pick["ride_id"] = ride_id
    ctx_last = (CachedWorkout.objects.for_user(draft.user).filter(ride_id=ride_id)
                .order_by("-created_at").values_list("created_at", flat=True).first())
    pick.pop("repeat", None)
    pick.pop("last_taken", None)
    if ctx_last:
        pick["repeat"], pick["last_taken"] = True, _local_date(ctx_last).isoformat()
    draft.picks_json[key] = pick
    draft.save(update_fields=["picks_json", "updated_at"])
    return ride_id


# ---------------------------------------------------------------------------
# Background generation (04)
# ---------------------------------------------------------------------------

def start_generation(draft):
    import threading
    from .sync import _in_background
    threading.Thread(target=_in_background, args=(_generate, draft.pk), daemon=True).start()


def _generate(draft_pk):
    import traceback
    from . import ai, llm
    from .models import PlanDraft, WebhookError

    draft = PlanDraft.objects.select_related("user", "program").get(pk=draft_pk)
    user, inputs = draft.user, draft.inputs_json
    try:
        fitness = build_fitness_context(user, inputs)
        reassess = None
        if draft.kind == "reassess":
            reassess = {"from_week": draft.from_week,
                        "progress": progress_context(draft.program, draft.from_week, inputs),
                        "current_plan": remaining_plan_text(draft.program, draft.from_week)}
            draft.context_text = "\n\n".join([fitness, reassess["progress"], reassess["current_plan"]])
        else:
            draft.context_text = fitness
        draft.save(update_fields=["context_text", "updated_at"])   # inspectable even if the AI fails
        menu_text, allowed = catalog_menu(inputs)
        if not allowed["types"]:
            raise PlanSpecInvalid("The class catalog has nothing that fits these settings.")
        raw = ai.generate_training_plan_spec(user, inputs, fitness, menu_text, reassess=reassess)
        if reassess:
            kept_ids, kept_longest = kept_plan(draft.program, draft.from_week)
            spec, warnings = validate_spec(raw, inputs, allowed, first_week=draft.from_week,
                                           kept_longest=kept_longest)
            picks = pick_classes(user, spec, inputs, exclude_program=draft.program, used=kept_ids)
        else:
            spec, warnings = validate_spec(raw, inputs, allowed)
            picks = pick_classes(user, spec, inputs)
        draft.spec_json, draft.picks_json, draft.warnings = spec, picks, warnings
        draft.ai_model, draft.status, draft.error = raw.get("model", ""), "ready", ""
    except ai.AI_UNAVAILABLE as e:
        draft.status, draft.error = "failed", ai.ai_unavailable_reason(e)
    except llm.AIBadJSON as e:
        # Keep the reply for debugging (Webhook Errors page); the user gets a plain message.
        cut_off = e.stop_reason == "max_tokens"
        draft.status = "failed"
        draft.error = ("The AI's plan was too long and got cut off. Try again, or shorten the plan."
                       if cut_off else "The AI's reply wasn't a readable plan. Try again.")
        WebhookError.record(source="training_plan", user=user,
                            summary=f"Training plan reply unreadable: {e}"[:300],
                            detail=f"stop_reason={e.stop_reason}\n\n{e.text[:20000]}")
    except ValueError as e:      # PlanSpecInvalid
        draft.status, draft.error = "failed", str(e)[:1000] or "The AI's plan couldn't be read."
    except Exception as e:
        logger.exception("Training plan generation failed for draft %s", draft_pk)
        draft.status, draft.error = "failed", "Something went wrong building the plan. Try again."
        WebhookError.record(source="training_plan", summary=f"Training plan generation failed: {str(e)[:200]}",
                            detail=traceback.format_exc(), user=user)
    draft.save()


def reset_and_regenerate(draft):
    draft.status, draft.error = "generating", ""
    draft.spec_json, draft.picks_json, draft.warnings = {}, {}, []
    draft.save()
    start_generation(draft)


# ---------------------------------------------------------------------------
# Creating the Program (05)
# ---------------------------------------------------------------------------

def pace_summary(inputs, spec):
    """{"lines", "guidance", "stretch"} — the pace card on the review page and,
    saved in goal_json, on the program's run page."""
    p = inputs.get("pace") or {}
    lines = []
    if p.get("level"):
        lines.append(f"Peloton pace level {p['level']}")
    if p.get("goal_pace_s"):
        goal = f"Goal pace {_fmt_pace(p['goal_pace_s'])}"
        if p.get("goal_zone"):
            goal += f" ({p['goal_zone']} zone at Level {p['level']})"
        lines.append(goal)
        if p.get("race_level") and p.get("level") and p["race_level"] <= p["level"]:
            lines.append(f"Level {p['level']} already covers goal pace")
        elif p.get("race_level"):
            lines.append(f"Race-pace level {p['race_level']}")
    est = p.get("estimate")
    if est:
        lines.append(f"Current estimate {_fmt_hms(est['seconds'])} from training runs"
                     + (f" · {p['gap_pct']}% to go" if p.get("gap_pct") and p["gap_pct"] > 0 else ""))
    longest = max((sl["duration_min"] for _, sl in iter_slots(spec) if sl["discipline"] == "running"), default=0)
    if longest:
        lines.append(f"Longest run {longest} min" + (f" (target {p['long_run_min']}+)" if p.get("long_run_min") else ""))
    return {"lines": lines, "guidance": spec.get("pace_guidance", ""), "stretch": bool(p.get("stretch"))}


def plan_overview(program):
    """What the run page shows about an AI training plan: summary, pace card and
    per-week dates/phase/focus. Saved in goal_json at creation; plans created
    before that read the pace card and weeks from their original draft. None for
    other programs."""
    from .models import PlanDraft
    goal = program.goal_json or {}
    if not goal.get("start_date"):
        return None
    pace, weeks_info = goal.get("pace_summary"), goal.get("weeks_info")
    if (pace is None or weeks_info is None) and goal.get("draft_id"):
        draft = PlanDraft.objects.for_user(program.user).filter(pk=goal["draft_id"]).first()
        if draft and draft.spec_json:
            pace = pace if pace is not None else pace_summary(draft.inputs_json, draft.spec_json)
            weeks_info = weeks_info if weeks_info is not None else {
                str(wk["number"]): {"phase": wk.get("phase", ""), "focus": wk.get("focus", "")}
                for wk in draft.spec_json.get("weeks", [])}
    start = date.fromisoformat(goal["start_date"])
    monday = start - timedelta(days=start.weekday())
    weeks = {}
    for pw in program.weeks.all():
        info = (weeks_info or {}).get(str(pw.number), {})
        weeks[pw.number] = {"start": monday + timedelta(weeks=pw.number - 1),
                            "end": monday + timedelta(weeks=pw.number - 1, days=6),
                            "phase": info.get("phase", ""), "focus": info.get("focus", "")}
    return {"summary": program.description, "pace": pace or {}, "weeks": weeks, "monday": monday}


def slot_notes(spec_slot, instructor=""):
    """"easy aerobic minutes · Easy zone · Becs Gentry" — purpose, pace zone, instructor."""
    parts = [spec_slot.get("purpose") or "",
             f"{spec_slot['pace_zone']} zone" if spec_slot.get("pace_zone") else "", instructor or ""]
    return " · ".join(p for p in parts if p)


def week_slot_data(spec_week, picks, classes):
    """create_plan slot dicts for one spec week, each filled with its picked class."""
    slots = []
    for slot in spec_week["slots"]:
        pick = picks.get(slot_key(spec_week["number"], slot)) or {}
        c = classes.get(pick.get("ride_id"))
        duration = c.duration_min if c else (pick.get("duration_min") or slot["duration_min"])
        slots.append({
            "day": slot["day"], "order": slot["order"],
            "title": c.title if c else f"{slot['class_type']} · {duration} min",
            "discipline": "strength" if slot["discipline"] == "pilates" else slot["discipline"],
            "duration_min": duration, "ride_id": c.ride_id if c else "",
            "optional": slot["optional"], "notes": slot_notes(slot, c.instructor_name if c else ""),
            "spec": dict(slot),
        })
    return slots


def race_slot_data(inputs):
    """The title-only, optional race-day slot (deliberately no any-class matcher)."""
    return {"day": inputs["race_weekday"], "order": 9,
            "title": f"Race day: {RACE_LABELS.get(inputs['goal'], 'Race')}",
            "discipline": "running", "optional": True, "ride_id": "",
            "notes": "Informational — not matched automatically (an any-run matcher would claim every "
                     "unplanned run in the plan window)."}


def create_program_from_draft(draft, name):
    """Save a ready draft as a ride-id-pinned plan and start its run on the plan's
    start date. Doesn't touch any other program's run."""
    from django.db import transaction
    from .programs import create_plan, start_run, unique_slug

    inputs, spec, picks = draft.inputs_json, draft.spec_json, draft.picks_json
    user = draft.user
    classes = classes_by_id({p.get("ride_id") for p in picks.values()})
    start, race = plan_dates(inputs)
    weeks_data = []
    for wk in spec["weeks"]:
        slots = week_slot_data(wk, picks, classes)
        if inputs.get("race_week") == wk["number"] and race:
            slots.append(race_slot_data(inputs))
        weeks_data.append({"number": wk["number"], "slots": slots})

    end = race or (week_dates(inputs, inputs["weeks"])[-1])
    with transaction.atomic():
        program = create_plan(user, name, unique_slug(user, name), "", weeks_data, kind="plan")
        program.description = spec.get("summary", "")
        program.goal_json = {
            "goal": inputs["goal"], "race_date": inputs.get("race_date") or "", "end_date": end.isoformat(),
            "target_time": inputs.get("target_time"), "start_date": inputs["start_date"],
            "mode": inputs["mode"], "companion_program_id": inputs.get("companion_program_id"),
            "draft_id": draft.pk, "generated_at": draft.updated_at.isoformat() if draft.updated_at else "",
            "model": draft.ai_model, "level": inputs.get("level"), "weeks": inputs["weeks"],
            "pace_level": (inputs.get("pace") or {}).get("level"),
            "goal_pace_s": (inputs.get("pace") or {}).get("goal_pace_s"),
            "pace_summary": pace_summary(inputs, spec),
            "weeks_info": {str(wk["number"]): {"phase": wk.get("phase", ""), "focus": wk.get("focus", "")}
                           for wk in spec["weeks"]},
        }
        program.save(update_fields=["description", "goal_json"])
        start_run(program, start)
        draft.status, draft.program = "created", program
        draft.save(update_fields=["status", "program", "updated_at"])
    return program


def swap_program_slot(slot):
    """Post-creation swap (no AI): re-run the picker for one training-plan slot,
    excluding its current class, other programs' pins, this program's other
    classes and anything taken in the last RECENT_REPEAT_DAYS. Returns the new
    PelotonClass or None."""
    from .programs import program_ride_ids
    program = slot.week.program
    goal = program.goal_json or {}
    ctx = PickContext(program.user, goal.get("level"), goal.get("weeks") or program.weeks.count(),
                      exclude_program=program)
    ctx.used = program_ride_ids(program)
    ranked, _ = rank_candidates(slot.spec_json, slot.week.number, ctx, exclude={slot.peloton_ride_id})
    if not ranked:
        return None
    c = ranked[0]
    slot.peloton_ride_id, slot.title, slot.alt_ride_ids = c.ride_id, c.title, []
    slot.duration_min = c.duration_min
    slot.notes = slot_notes(slot.spec_json, c.instructor_name)
    slot.save(update_fields=["peloton_ride_id", "title", "alt_ride_ids", "duration_min", "notes"])
    return c


# ---------------------------------------------------------------------------
# Reassessing a plan mid-way: Sonnet rewrites the remaining weeks from how the
# runs so far actually went; the user reviews, then it's applied in place
# (same Program, same run, completed weeks untouched).
# ---------------------------------------------------------------------------

REASSESS_COOLDOWN = timedelta(days=7)   # no nudge within a week of the last reassessment
EASY_RPE, HARD_RPE = 3, 8               # two rated weeks in a row at/below (or at/above) → nudge
MISSED_RUNS_NUDGE = 2                   # planned runs missed over the last two finished weeks → nudge


def plan_week_for(program, day):
    """Which plan week a date falls in (1-based; ≤ 0 before the plan starts)."""
    start = date.fromisoformat(program.goal_json["start_date"])
    return (day - (start - timedelta(days=start.weekday()))).days // 7 + 1


def source_draft(program):
    """The draft a plan was generated from (holds its original inputs)."""
    from .models import PlanDraft
    draft_id = (program.goal_json or {}).get("draft_id")
    return PlanDraft.objects.for_user(program.user).filter(pk=draft_id, kind="new").first() if draft_id else None


def reassess_window(program, today=None):
    """((from_week, last_week), "") a reassessment would rewrite — next Monday's
    week through the end — or (None, reason) when it isn't available."""
    goal = program.goal_json or {}
    if not goal.get("start_date"):
        return None, "Only AI training plans can be reassessed."
    if program.active_run is None:
        return None, "This plan has no active cycle."
    today = today or timezone.localdate()
    weeks = goal.get("weeks") or program.weeks.count()
    from_week = max(plan_week_for(program, today) + 1, 1)
    if from_week > weeks:
        return None, "The plan's last week has already started."
    if source_draft(program) is None:
        return None, "The plan's original inputs are gone, so it can't be reassessed."
    return (from_week, weeks), ""


def _planned_runs(program, weeks):
    return [s for s in ProgramSlot.objects.filter(week__program=program, week__number__in=weeks)
            .select_related("week") if s.spec_json.get("discipline") == "running"]


def reassess_signals(program, today=None):
    """Plain-language reasons the plan may need a refresh (no AI involved), for the
    nudge banner. Empty when nothing stands out, or right after a reassessment."""
    from .models import ProgramWorkout
    window, _ = reassess_window(program, today)
    if not window:
        return []
    today = today or timezone.localdate()
    goal = program.goal_json
    last = (goal.get("reassessments") or [{}])[-1].get("date")
    if last and today - date.fromisoformat(last) < REASSESS_COOLDOWN:
        return []
    run = program.active_run
    reasons = []
    level = latest_pace_level(program.user)
    if (level and goal.get("pace_level") and level["level"] != goal["pace_level"]
            and level["date"] >= date.fromisoformat(goal["start_date"])):
        reasons.append(f"your Peloton pace level is now {level['level']} (the plan was built for "
                       f"Level {goal['pace_level']})")
    rated = list(run.run_weeks.filter(rpe__isnull=False).order_by("-sequence")[:2])
    if len(rated) == 2 and all(r.rpe <= EASY_RPE for r in rated):
        reasons.append(f"you rated your last two weeks {rated[1].rpe}/10 and {rated[0].rpe}/10 — it may be too easy")
    elif len(rated) == 2 and all(r.rpe >= HARD_RPE for r in rated):
        reasons.append(f"you rated your last two weeks {rated[1].rpe}/10 and {rated[0].rpe}/10 — it may be too hard")
    current = plan_week_for(program, today)
    finished = [w for w in (current - 1, current - 2) if w >= 1]
    planned = [s for s in _planned_runs(program, finished) if not s.optional]
    done = set(ProgramWorkout.objects.filter(run_week__run=run, slot__in=planned).values_list("slot_id", flat=True))
    missed = sum(1 for s in planned if s.pk not in done)
    if missed >= MISSED_RUNS_NUDGE:
        reasons.append(f"{missed} planned runs were missed in the last two weeks")
    return reasons


def _done_line(w, level):
    bits = [f'"{w.title}"', f"{round(_minutes(w))} min"]
    if w.avg_pace_seconds:
        pace = _fmt_pace(w.avg_pace_seconds)
        if level:
            pace += f" ({zone_for_pace(level, w.avg_pace_seconds)} at Level {level})"
        bits.append(pace)
    if w.effort_per_min:
        bits.append(f"{w.effort_per_min} effort pts/min")
    if w.heart_rate_avg_best:
        bits.append(f"HR {round(w.heart_rate_avg_best)}")
    return " · ".join(bits)


def progress_context(program, from_week, inputs, today=None):
    """PROGRESS SO FAR: each week before from_week — planned vs done (pace and zone,
    effort per minute, HR), the week's rating, unplanned runs — plus pace-level
    change. Every number comes from the database."""
    from .models import ProgramWorkout
    today = today or timezone.localdate()
    run = program.active_run
    goal = program.goal_json
    level = (inputs.get("pace") or {}).get("level")
    weeks = list(range(1, from_week))
    out = [f"PROGRESS SO FAR (weeks 1–{from_week - 1} of {goal.get('weeks')}; today is {today:%a %b} {today.day})"]
    entries = {e.slot_id: e.workout for e in ProgramWorkout.objects.filter(run_week__run=run)
               .select_related("workout") if e.slot_id}
    on_grid = {w.pk for w in entries.values()}
    ratings = {rw.sequence: rw for rw in run.run_weeks.all()}
    start = date.fromisoformat(goal["start_date"])
    monday = start - timedelta(days=start.weekday())
    slots_by_week = defaultdict(list)
    for s in ProgramSlot.objects.filter(week__program=program, week__number__in=weeks).select_related("week"):
        if s.spec_json:
            slots_by_week[s.week.number].append(s)
    for n in weeks:
        wk_start = monday + timedelta(weeks=n - 1)
        slots = sorted(slots_by_week[n], key=lambda s: (s.day or 0, s.order))
        planned_runs = [s for s in slots if s.spec_json.get("discipline") == "running"]
        done_runs = [entries[s.pk] for s in planned_runs if s.pk in entries]
        head = (f"Week {n} ({wk_start:%b} {wk_start.day}): planned {len(planned_runs)} runs · "
                f"{sum(s.duration_min or 0 for s in planned_runs)} min; done {len(done_runs)} · "
                f"{round(sum(_minutes(w) for w in done_runs))} min")
        rw = ratings.get(n)
        if rw and rw.rpe:
            head += f"; rated {rw.rpe}/10" + (f' ("{rw.note.strip()[:120]}")' if rw.note.strip() else "")
        out.append(head)
        for s in slots:
            sp = s.spec_json
            plan = (f"{DAY_NAMES.get(s.day, '?')} {sp.get('class_type', s.title)} {s.duration_min} min "
                    f"{sp.get('intensity', '')}" + (f" [{sp['pace_zone']}]" if sp.get("pace_zone") else "")
                    + (" (optional)" if s.optional else ""))
            w = entries.get(s.pk)
            out.append(f"  - {plan} → " + (f"done: {_done_line(w, level)}" if w else "missed"))
        extra = (CachedWorkout.objects.for_user(program.user)
                 .filter(discipline__in=RUN_DISCIPLINES, created_at__date__gte=wk_start,
                         created_at__date__lte=wk_start + timedelta(days=6)).exclude(pk__in=on_grid))
        for w in extra:
            out.append(f"  - unplanned run: {_done_line(w, level)}")
    p = inputs.get("pace") or {}
    if goal.get("pace_level") and p.get("level") and p["level"] != goal["pace_level"]:
        out.append(f"Peloton pace level: Level {goal['pace_level']} when the plan was made; now Level {p['level']}.")
    elif p.get("level"):
        out.append(f"Peloton pace level: still Level {p['level']}.")
    return "\n".join(out)


def remaining_plan_text(program, from_week):
    """CURRENT PLAN for the weeks being rewritten, as the specs they were built from."""
    info = (program.goal_json or {}).get("weeks_info") or {}
    out = [f"CURRENT PLAN (weeks {from_week}–{(program.goal_json or {}).get('weeks')}, as scheduled now)"]
    by_week = defaultdict(list)
    for s in ProgramSlot.objects.filter(week__program=program, week__number__gte=from_week).select_related("week"):
        by_week[s.week.number].append(s)
    for n in sorted(by_week):
        phase = info.get(str(n), {}).get("phase", "")
        parts = []
        for s in sorted(by_week[n], key=lambda s: (s.day or 0, s.order)):
            sp = s.spec_json
            if not sp:
                parts.append(f"{DAY_NAMES.get(s.day, '?')} {s.title}")
                continue
            parts.append(f"{DAY_NAMES.get(s.day, '?')} {sp.get('discipline')} {sp.get('class_type')} "
                         f"{s.duration_min} min {sp.get('intensity', '')}"
                         + (f" [{sp['pace_zone']}]" if sp.get("pace_zone") else ""))
        out.append(f"- Week {n}{f' ({phase})' if phase else ''}: " + "; ".join(parts))
    return "\n".join(out)


def kept_plan(program, from_week):
    """(ride ids, longest run minutes) in the weeks a reassessment keeps."""
    slots = ProgramSlot.objects.filter(week__program=program, week__number__lt=from_week)
    ride_ids = {s.peloton_ride_id for s in slots if s.peloton_ride_id}
    longest = max((s.duration_min or 0 for s in slots if s.spec_json.get("discipline") == "running"), default=0)
    return ride_ids, longest


def start_reassessment(program, today=None):
    """Create a "reassess" draft from the plan's original inputs (pace re-profiled
    with today's data) and start generating it. Raises ValueError when unavailable."""
    from .models import PlanDraft
    window, reason = reassess_window(program, today)
    if not window:
        raise ValueError(reason)
    inputs = dict(source_draft(program).inputs_json)
    inputs["pace"] = pace_profile(program.user, inputs, today)
    draft = PlanDraft.objects.create(user=program.user, kind="reassess", program=program,
                                     from_week=window[0], inputs_json=inputs)
    start_generation(draft)
    return draft


def apply_reassessment(draft):
    """Replace the slots of weeks from_week… with the reviewed picks. Slots that
    already have a completion, and the race-day slot, are kept."""
    from django.db import transaction
    from .models import ProgramWorkout
    from .programs import backfill_program

    program, spec, picks, inputs = draft.program, draft.spec_json, draft.picks_json, draft.inputs_json
    classes = classes_by_id({p.get("ride_id") for p in picks.values()})
    today = timezone.localdate()
    with transaction.atomic():
        for wk in spec["weeks"]:
            pw = program.weeks.get(number=wk["number"])
            done = set(ProgramWorkout.objects.filter(slot__week=pw).values_list("slot_id", flat=True))
            for s in pw.slots.all():
                if s.spec_json and s.pk not in done:
                    s.delete()
            for d in week_slot_data(wk, picks, classes):
                ProgramSlot.objects.create(
                    week=pw, day=d["day"], order=d["order"], title=d["title"], discipline=d["discipline"],
                    duration_min=d["duration_min"], peloton_ride_id=d["ride_id"], optional=d["optional"],
                    notes=d["notes"], spec_json=d["spec"])
        goal = dict(program.goal_json)
        weeks_info = dict(goal.get("weeks_info") or {})
        for wk in spec["weeks"]:
            weeks_info[str(wk["number"])] = {"phase": wk.get("phase", ""), "focus": wk.get("focus", "")}
        goal["weeks_info"] = weeks_info
        goal["pace_summary"] = pace_summary(inputs, spec)
        goal["pace_level"] = (inputs.get("pace") or {}).get("level") or goal.get("pace_level")
        goal["reassessments"] = (goal.get("reassessments") or []) + [{
            "date": today.isoformat(), "from_week": draft.from_week, "draft_id": draft.pk,
            "summary": spec.get("summary", ""), "changes": spec.get("changes", [])}]
        program.goal_json = goal
        program.save(update_fields=["goal_json"])
        draft.status = "created"
        draft.save(update_fields=["status", "updated_at"])
    backfill_program(program)
    return program
