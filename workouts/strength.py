"""Per-exercise history and "move up the weight" recommendations from the
hand-entered exercise log (CachedWorkout.manual_movements_json).

Peloton classes prescribe the reps, so logged reps nearly always match the
class — the signal for progressing is the per-exercise effort rating
("easy" / "right" / "hard" / "fail") logged next to each row.
"""
from .models import DEFAULT_DUMBBELLS_LB, CachedWorkout, UserSettings

EFFORT_CHOICES = [
    ("easy", "Easy"),
    ("right", "Just right"),
    ("hard", "Hard"),
    ("fail", "Couldn't finish"),
]
EFFORT_LABELS = dict(EFFORT_CHOICES)


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
                "timed": r["timed"],
                "per_side": r["per_side"],
                "dumbbells": r["dumbbells"],
                "volume_lb": r["volume_lb"],
            })
    return history


def recommend(sessions, rack):
    """Next-session weight from an exercise's sessions (oldest first).

    Returns None for bodyweight work, else {"action", "weight", "current", "reason"}
    where action is "up" / "hold" / "down" / "max":
      - Couldn't finish → drop one dumbbell.
      - Hard → stay.
      - Easy → next dumbbell up.
      - Just right → up once it's been just right (or easy) two sessions running at
        this weight; otherwise one more session here.
      - No rating → up after two sessions at this weight, but say a rating would help.
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

    def go_up(reason):
        if up is None:
            return {"action": "max", "weight": current, "current": current,
                    "reason": f"{reason} — that's your heaviest dumbbell; add reps or slow the tempo."}
        return {"action": "up", "weight": up, "current": current, "reason": reason}

    def hold(reason):
        return {"action": "hold", "weight": current, "current": current, "reason": reason}

    if effort == "fail":
        down = prev_dumbbell(current, rack)
        if down is None:
            return hold("Couldn't finish — stay here and build up the reps.")
        return {"action": "down", "weight": down, "current": current,
                "reason": "Couldn't finish last time — drop one dumbbell and own the reps."}
    if effort == "hard":
        return hold("Felt hard — repeat this weight until it feels just right.")
    if effort == "easy":
        return go_up("Felt easy")
    if effort == "right":
        if len(at_weight) >= 2 and at_weight[1]["effort"] in ("right", "easy"):
            return go_up("Just right two sessions in a row")
        return hold("Just right — one more session here, then move up if it holds.")
    if len(at_weight) >= 2 and not any(s["effort"] in ("hard", "fail") for s in at_weight):
        return go_up("Two sessions at this weight (rate effort for sharper advice)")
    return hold("Rate how it felt next time to get a recommendation.")


def recommendations(user, until=None):
    """{exercise key: recommendation} for every exercise the user has logged."""
    rack = dumbbells(user)
    return {k: rec for k, e in exercise_history(user, until).items() if (rec := recommend(e["sessions"], rack))}
