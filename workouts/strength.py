"""Per-exercise history and "move up the weight" recommendations from the
hand-entered exercise log (CachedWorkout.manual_movements_json).

The signal for progressing is the per-exercise effort rating ("easy" /
"right" / "hard" / "fail") logged next to each row. A row can also carry a
rep range (`rep_min`/`rep_max`, e.g. 6–8): then it's double progression —
add reps at the same weight until the top of the range, then move up.
"""
import re

from .models import DEFAULT_DUMBBELLS_LB, CachedWorkout, UserSettings

EFFORT_CHOICES = [
    ("easy", "Easy"),
    ("right", "Just right"),
    ("hard", "Hard"),
    ("fail", "Couldn't finish"),
]
EFFORT_LABELS = dict(EFFORT_CHOICES)


_RANGE_RE = re.compile(r"^\s*(\d{1,3})\s*(?:(?:-|–|—|to)\s*(\d{1,3}))?\s*(?:reps?|s|sec|seconds)?\s*$", re.I)


def parse_rep_range(text):
    """"6-8" / "6–8" / "6 to 8" / "8" → (6, 8) / (8, 8); "" → None; unreadable → ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    m = _RANGE_RE.match(text)
    if not m:
        raise ValueError(text)
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    if lo < 1:
        raise ValueError(text)
    return (min(lo, hi), max(lo, hi))


def format_rep_range(lo, hi):
    if not lo:
        return ""
    return str(lo) if lo == hi else f"{lo}–{hi}"


def rep_range(sessions):
    """The exercise's rep range: the latest session that has one, else None."""
    for s in reversed(sessions):
        if s.get("rep_min"):
            return (s["rep_min"], s["rep_max"])
    return None


def exercise_key(name):
    return " ".join((name or "").lower().split())


def dumbbells(user):
    """The dumbbells on the user's rack (Settings → Dumbbells) — "next weight up" steps through these."""
    return sorted(UserSettings.for_user(user).dumbbells_lb or DEFAULT_DUMBBELLS_LB)


def next_dumbbell(weight, rack):
    return next((d for d in rack if d > weight), None)


def prev_dumbbell(weight, rack):
    return next((d for d in reversed(rack) if d < weight), None)


def exercise_history(user, until=None):
    """{key: {"name", "sessions": [...]}} over every one of the user's workouts with a manual log,
    oldest session first. `until` (a datetime) limits it to workouts up to then.
    Timed rows are flagged; they get recommendations when weighted (e.g. carries)."""
    qs = CachedWorkout.objects.for_user(user).exclude(manual_movements_json=[]).order_by("created_at")
    if until is not None:
        qs = qs.filter(created_at__lte=until)
    history = {}
    for w in qs.only("workout_id", "title", "created_at", "manual_movements_json"):
        for r in w.manual_log_rows:
            key = exercise_key(r.get("name"))
            if not key:
                continue
            entry = history.setdefault(key, {"name": r["name"].strip(), "sessions": []})
            entry["name"] = r["name"].strip()   # latest spelling wins
            entry["sessions"].append({
                "workout_id": w.workout_id,
                "title": w.title,
                "date": w.created_at,
                "sets": r.get("sets"),
                "reps": r.get("reps"),
                "weight_lb": r.get("weight_lb") or 0,
                "effort": r.get("effort") or "",
                "rep_min": r.get("rep_min"),
                "rep_max": r.get("rep_max"),
                "timed": r["timed"],
                "per_side": r["per_side"],
                "dumbbells": r["dumbbells"],
                "volume_lb": r["volume_lb"],
            })
    return history


def recommend(sessions, rack):
    """Next-session weight from an exercise's sessions (oldest first).

    Returns None for bodyweight work, else {"action", "weight", "current", "reps", "timed", "reason"}
    where action is "up" / "hold" / "reps" / "down" / "max":
      - Couldn't finish → drop one dumbbell.
      - Hard → stay.
      - Easy → next dumbbell up.
      - Just right → up once it's been just right (or easy) two sessions running at
        this weight; otherwise one more session here.
      - No rating → up after two sessions at this weight, but say a rating would help.
    With a rep range (latest one logged for the exercise) and reps on the last session,
    the weight only goes up from the top of the range:
      - below the top: "reps" — same weight, one more rep (Easy: straight to the top;
        timed rows step 5 s);
      - at or above the top, Easy / Just right / unrated → up, starting back at the bottom.
    `reps` is the target for next time (None without a range; seconds when `timed`); a one-number range
    (lo == hi) is a fixed target, so the plain rules above apply.
    """
    loaded = [s for s in sessions if s["weight_lb"]]   # timed work with a weight (carries) counts too
    if not loaded:
        return None
    last = loaded[-1]
    current = last["weight_lb"]
    at_weight = []
    for s in reversed(loaded):            # the current streak at this weight
        if s["weight_lb"] != current:
            break
        at_weight.append(s)
    up = next_dumbbell(current, rack)
    effort = last["effort"]
    rng = rep_range(sessions)
    lo, hi = rng or (None, None)
    reps = last.get("reps")
    timed = bool(last.get("timed"))
    unit = " s" if timed else " reps"

    def rec(action, weight, reps, reason):
        return {"action": action, "weight": weight, "current": current, "reps": reps, "timed": timed,
                "reason": reason}

    def go_up(reason, reps=None):
        if up is None:
            return rec("max", current, None, f"{reason} — that's your heaviest dumbbell; add reps or slow the tempo.")
        return rec("up", up, reps, reason)

    def hold(reason, reps=None, action="hold"):
        return rec(action, current, reps, reason)

    if effort == "fail":
        down = prev_dumbbell(current, rack)
        if down is None:
            return hold("Couldn't finish — stay here and build up the reps.", lo)
        return rec("down", down, lo, "Couldn't finish last time — drop one dumbbell and own the reps.")
    if effort == "hard":
        return hold("Felt hard — repeat this weight until it feels just right.",
                    min(max(reps, lo), hi) if rng and reps else None)

    if rng and reps and lo < hi:
        range_text = f"{format_rep_range(lo, hi)}{unit}"
        if reps >= hi:
            how = {"easy": "Felt easy", "right": "Just right"}.get(effort, "Reached the top")
            note = "" if effort else " (rate effort for sharper advice)"
            return go_up(f"{how} at {reps}{unit}, the top of your {format_rep_range(lo, hi)} range — "
                         f"move up and start back at {lo}{note}", lo)
        step = 5 if timed else 1
        target = hi if effort == "easy" else max(lo, min(hi, reps + step))
        how = {"easy": "Felt easy", "right": "Just right"}.get(effort, "Logged")
        note = "" if effort else " (rate effort for sharper advice)"
        then = ("the top of your range — then move up" if target == hi
                else f"then move up once you reach {hi} ({range_text})")
        return hold(f"{how} at {reps}{unit} — aim for {target} at this weight, {then}{note}.",
                    target, action="reps")

    if effort == "easy":
        return go_up("Felt easy", lo if rng else None)
    if effort == "right":
        if len(at_weight) >= 2 and at_weight[1]["effort"] in ("right", "easy"):
            return go_up("Just right two sessions in a row", lo if rng else None)
        return hold("Just right — one more session here, then move up if it holds.", lo if rng else None)
    if len(at_weight) >= 2 and not any(s["effort"] in ("hard", "fail") for s in at_weight):
        return go_up("Two sessions at this weight (rate effort for sharper advice)", lo if rng else None)
    return hold("Rate how it felt next time to get a recommendation.", lo if rng else None)


def recommendations(user, until=None):
    """{exercise key: recommendation} for every exercise the user has logged."""
    rack = dumbbells(user)
    return {k: rec for k, e in exercise_history(user, until).items() if (rec := recommend(e["sessions"], rack))}
