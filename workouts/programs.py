"""Program association: match a CachedWorkout to a Program and place it on the run grid."""
import re
from datetime import date, timedelta
from statistics import mean

from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from .models import (
    CachedWorkout, Program, ProgramWeek, ProgramSlot, ProgramRun, RunWeek, ProgramWorkout,
)

# "<base title>: <Plan> W1 D1"  — suffix on CachedWorkout.title, e.g.
# "10 min Mobility: HiLit W1 D1". Real Peloton titles put the plan tag at the
# end, not the front, so this matches a suffix rather than a prefix.
TITLE_SUFFIX_RE = re.compile(
    r":\s*(?P<plan>[\w'&]+(?:[ \-][\w'&]+)*?)\s+"
    r"W(?:eek)?\s*(?P<week>\d+)[,\s]*D(?:ay)?\s*(?P<day>\d+)\s*$",
    re.IGNORECASE,
)


# ---------- reading the workout ----------

def _ride(workout):
    """Raw ride sub-dict, when raw_data is needed for fields with no flat column (e.g. series_id)."""
    return ((workout.raw_data or {}).get("peloton") or {}).get("ride") or {}

def workout_local_date(workout):
    """Return a date for ordering. CachedWorkout has no `date` field; created_at is authoritative."""
    if workout.created_at:
        return workout.created_at.date()
    ts = (workout.raw_data or {}).get("start_time")
    return date.fromtimestamp(ts) if ts else None

def achievement_names(workout):
    """Peloton's list-sync payload carries no achievement_templates/slug; the detail-synced
    `achievements` field (name/description/count) is the only achievement signal available."""
    return {
        (a or {}).get("name")
        for a in (workout.achievements or [])
        if (a or {}).get("name")
    }

def normalize_title(title):
    """Strip a trailing ': HiLit W1 D1' style suffix and collapse whitespace, lowercased."""
    if not title:
        return ""
    base = re.split(r":\s*\S*\s*W\d+\s*D\d+\s*$", title, flags=re.IGNORECASE)[0]
    base = re.sub(r":\s*.*Week\s*\d+.*Day\s*\d+.*$", "", base, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", base).strip().lower()

PELOTON_CLASS_ID_RE = re.compile(r'(?:classId=|/player/)([0-9a-f]{16,40})', re.IGNORECASE)

def extract_ride_id_from_text(text):
    """Pull a Peloton ride/class id out of a pasted class URL, if present."""
    if not text:
        return None
    m = PELOTON_CLASS_ID_RE.search(text)
    return m.group(1).lower() if m else None


def normalize_ride_id_input(raw):
    """
    Accept either a raw ride_id or a pasted class URL in a manual-entry field
    (the plan review table's ride_id box takes both) and normalize to a bare
    ride_id, or "" if unrecognizable.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    linked = extract_ride_id_from_text(raw)
    if linked:
        return linked
    if re.fullmatch(r"[0-9a-fA-F]{16,40}", raw):
        return raw.lower()
    return ""


def parse_week_day(program, workout):
    """Return (week, day) ints from the workout title using the program's regex, else (None, None)."""
    pattern = program.title_week_day_regex or TITLE_SUFFIX_RE.pattern
    text = workout.title
    if text:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            gd = m.groupdict()
            wk = int(gd["week"]) if gd.get("week") else None
            dy = int(gd["day"]) if gd.get("day") else None
            if wk:
                return wk, dy
    return None, None


# ---------- the confidence ladder ----------

def identify_membership(workout):
    """
    Return (program, canonical_week_number, day, matched_by) or None.
    1) achievement name  2) ride-id membership  3) title suffix (only if a Program matches).
    """
    names = achievement_names(workout)
    ride_id = workout.ride_id

    # 1) achievement-stamped plans
    for p in Program.objects.filter(match_strategy="achievement").exclude(achievement_name=""):
        if p.achievement_name in names:
            wk, dy = parse_week_day(p, workout)
            return p, (wk or 1), dy, "achievement"

    # 2) ride-id membership (splits, and any exact-pinned slot)
    if ride_id:
        slots = list(ProgramSlot.objects
                     .filter(Q(peloton_ride_id=ride_id) | Q(alt_ride_ids__contains=ride_id))
                     .select_related("week", "week__program"))
        if slots:
            program = slots[0].week.program
            week_numbers = {s.week.number for s in slots}
            if len(week_numbers) == 1:
                return program, slots[0].week.number, slots[0].day, "ride_id"
            # This ride_id has a slot in more than one canonical week — a
            # "repeat_all_weeks" plan slot (e.g. a warm-up retakeable in any
            # week). Which week it belongs to can't be decided from the ride_id
            # alone; associate_workout resolves it once it has the active run
            # (week=None is the signal to do that).
            return program, None, slots[0].day, "ride_id"

    # 3) title suffix -> only auto-associate if a Program name matches
    m = TITLE_SUFFIX_RE.search(workout.title or "")
    if m:
        plan_slug = slugify(m.group("plan"))
        p = Program.objects.filter(slug=plan_slug).first() or \
            Program.objects.filter(name__iexact=m.group("plan").strip()).first()
        if p:
            return p, int(m.group("week")), int(m.group("day")), "description"
        # A plan we don't track yet — caller may surface this as a suggestion.
        return None
    return None


# ---------- placing it on the grid ----------

def get_or_create_active_run(program, on_date):
    run = program.active_run
    if run is None:
        run = ProgramRun.objects.create(program=program, start_date=on_date)
    return run

def _slot_for(workout, program_week, matched_by, day=None):
    """
    Return an ordered list of ProgramSlot candidates in this canonical week that
    this workout could fill. Several slots can share an identical title (e.g.
    HiLit's "10 min Mobility" appears once per day, days 1-5) — the parsed day, if
    known, orders the same-day slot first, but every other same-titled slot is
    still offered as a fallback. That fallback matters: it lets a repeat of an
    already-filled slot (e.g. rewatching that same D1 mobility class) land on a
    still-open same-titled slot instead of always spilling into a brand new pass.

    Ride-id matching gets the same multi-candidate treatment as title matching —
    a "repeat_all_days" plan slot (e.g. a stretch retakeable any day of the week)
    is authored as several ProgramSlot rows sharing one ride_id (one per day, all
    but the first optional); returning all of them here (not just the first) lets
    _open_slot_in spread retakes across the still-open day-slots instead of
    always reusing the same one.
    """
    ride_id = workout.ride_id
    if ride_id:
        matches = list(program_week.slots.filter(
            Q(peloton_ride_id=ride_id) | Q(alt_ride_ids__contains=ride_id)
        ))
        if matches:
            if day is not None:
                matches.sort(key=lambda s: s.day != day)
            return matches
    norm = normalize_title(workout.title)
    if not norm:
        return []
    matches = []
    for s in program_week.slots.all():
        st = s.title.strip().lower()
        if st and (st == norm or st in norm or norm in st):
            matches.append(s)
    if day is not None:
        matches.sort(key=lambda s: s.day != day)
    return matches

PASS_GAP_DAYS = 7   # a pass can't span more than this many days start-to-end

def _open_slot_in(run_week, slot_candidates):
    """
    Which slot a completion should land in within this run_week, and whether it's
    actually open. With candidates, the first one not yet filled here wins (falls
    back to the top candidate, marked not-open, if all are taken). With no
    candidates (no specific slot matched — e.g. a title that doesn't match any
    seeded slot), the pass is open as long as it doesn't already hold another
    loose entry — unrelated slotted entries in the same pass don't block it. A
    stricter "any entry at all blocks it" rule would force a whole new pass over
    one unmatched title even when the rest of the week's slots are still filling
    normally.
    """
    if not slot_candidates:
        return None, not run_week.entries.filter(slot__isnull=True).exists()
    taken_ids = set(run_week.entries.exclude(slot=None).values_list("slot_id", flat=True))
    for s in slot_candidates:
        if s.id not in taken_ids:
            return s, True
    return slot_candidates[0], False

def fill_or_append(run, workout, program_week, slot_candidates, matched_by):
    """
    A completion joins the most recent pass for this canonical week if one of its
    candidate slots is still open there AND adding this date wouldn't stretch the
    pass beyond PASS_GAP_DAYS end-to-end; otherwise it starts a new pass.

    Checking only the latest pass (not "earliest with an empty slot") matters:
    workouts are always processed in date order, so once a pass has fallen more
    than PASS_GAP_DAYS behind, treat it as closed — an "earliest open slot" rule
    would happily bridge a two-month gap just because that slot happened to still
    be empty, mashing unrelated calendar weeks into one pass.
    """
    on_date = workout_local_date(workout)
    latest = run.run_weeks.filter(program_week=program_week).order_by("-sequence").first()
    if latest is not None:
        slot, is_open = _open_slot_in(latest, slot_candidates)
        within_gap = True
        if on_date:
            other_dates = [d for d in (workout_local_date(e.workout) for e in latest.entries.all()) if d]
            if other_dates:
                span = max(on_date, *other_dates) - min(on_date, *other_dates)
                within_gap = span.days <= PASS_GAP_DAYS
        if is_open and within_gap:
            return ProgramWorkout.objects.create(
                run_week=latest, slot=slot, workout=workout, matched_by=matched_by)
    # need a new pass
    next_seq = (run.run_weeks.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
    rw = RunWeek.objects.create(run=run, program_week=program_week, sequence=next_seq)
    slot = slot_candidates[0] if slot_candidates else None
    return ProgramWorkout.objects.create(
        run_week=rw, slot=slot, workout=workout, matched_by=matched_by)


def _resolve_recurring_week(run):
    """
    Attribute a repeat_all_weeks completion (its ride_id has a slot in every
    canonical week — see identify_membership) to whichever week the run is
    currently on: the program_week of its most recent pass. There's no
    per-week date range to check the completion's date against, so "current
    week" is defined behaviorally, by wherever the run's progress already is.
    Falls back to week 1 for a brand new run with no passes yet.
    """
    latest = run.run_weeks.order_by("-sequence").first()
    return latest.program_week.number if latest else 1


@transaction.atomic
def associate_workout(workout):
    """Full pipeline for one workout. Idempotent. Returns the ProgramWorkout or None."""
    existing = ProgramWorkout.objects.filter(workout=workout).first()
    if existing:
        return existing

    hit = identify_membership(workout)
    if not hit:
        return None
    program, week_number, day, matched_by = hit

    on_date = workout_local_date(workout) or date.today()
    run = get_or_create_active_run(program, on_date)

    if week_number is None:
        week_number = _resolve_recurring_week(run)

    pw_week = program.weeks.filter(number=week_number).first()
    if pw_week is None:   # week not seeded (e.g. a plan week we didn't define) -> create bare
        pw_week = ProgramWeek.objects.create(program=program, number=week_number)

    slot_candidates = _slot_for(workout, pw_week, matched_by, day=day)
    return fill_or_append(run, workout, pw_week, slot_candidates, matched_by)


# ---------- cross-cycle exercise load progression ----------

KG_TO_LB = 2.20462

MAX_PROGRESSION_SERIES = 8   # matches the app's 8-color validated categorical palette

# Best-effort muscle-group bucket for a free-text exercise name — there's no
# stored per-exercise taxonomy (Garmin/Peloton give names, not tags), so this
# is a keyword heuristic, not a lookup. Checked in priority order since some
# terms are ambiguous alone (e.g. bare "curl"/"extension"/"press" span both
# legs and arms) — the more specific compound terms go first so "leg curl"
# resolves to lower body before the generic "curl" would claim it for upper.
_CORE_KEYWORDS = (
    "twist", "crunch", "plank", "sit-up", "situp", "mountain climber",
    "dead bug", "bird dog", "side bend", "ab wheel", "hollow hold", "v-up",
)
_LOWER_BODY_KEYWORDS = (
    "leg curl", "leg extension", "leg press", "squat", "deadlift", "lunge",
    "glute", "hamstring", "quad", "calf", "hip thrust", "hip hinge", "swing",
    "step up", "step-up", "bridge",
)
_UPPER_BODY_KEYWORDS = (
    "press", "curl", "row", "pull", "push", "fly", "extension", "raise",
    "pulldown", "shoulder", "chest", "tricep", "bicep", "lat", "shrug",
)
CATEGORY_LABELS = {"lower_body": "Lower Body", "upper_body": "Upper Body", "core": "Core", "other": "Other"}
CATEGORY_ORDER = ["lower_body", "upper_body", "core", "other"]

def _muscle_group_for(exercise_name):
    name = (exercise_name or "").lower()
    if any(kw in name for kw in _CORE_KEYWORDS):
        return "core"
    if any(kw in name for kw in _LOWER_BODY_KEYWORDS):
        return "lower_body"
    if any(kw in name for kw in _UPPER_BODY_KEYWORDS):
        return "upper_body"
    return "other"


def progression_categories(run):
    """
    Muscle-group categories present among this run's tracked exercises — only
    the ones with data are offered as chart filter buttons. Grouping by body
    region rather than by ProgramSlot: a plan slot can be retaken across many
    weeks (e.g. a repeat_all_weeks class) with several near-identical-titled
    ProgramSlot rows, which made a per-slot toggle both confusing (indistinguishable
    labels) and thin (any one slot instance rarely holds more than a session or
    two); muscle group is a stable axis regardless of how the slots were authored.
    """
    present = set()
    pws = ProgramWorkout.objects.filter(run_week__run=run).select_related("workout")
    for pw in pws:
        for _key, name, top_lb, _vol, _reps in _workout_exercise_loads(pw.workout):
            if top_lb:
                present.add(_muscle_group_for(name))
    return [{"key": k, "label": CATEGORY_LABELS[k]} for k in CATEGORY_ORDER if k in present]


def run_exercise_progression(run, metric="top_weight", category=None):
    """
    Returns {"labels": [...pass labels...], "series": [{"exercise","key","points":[...]}...],
    "hidden_count": N}. metric: "top_weight" (max weight in the session), "volume"
    (sum reps*weight), or "reps" (total reps in the session, weight-independent — tracks
    endurance progression even when load is flat). Contributes from either data source a
    strength workout may carry: Garmin's per-set `exercise_sets_json` (weight_kg,
    converted to lb), or Peloton's Movement Tracker `movements` summary (already
    aggregated per movement, in lb).

    Bodyweight-only movements (never any recorded weight) are dropped — they carry no
    signal on a load chart. If more than MAX_PROGRESSION_SERIES exercises remain, only
    the most-frequently-tracked ones are kept, since one canvas can't stay legible with
    the ride's full movement roster (a "9th series" folds out rather than getting an
    invented color) — this is normal for slots that span two ride-id variants of a
    class with slightly different movement lineups. Pass `category` (see
    progression_categories) to scope to one muscle-group bucket — filtered per exercise,
    not per slot/workout, so a mixed full-body class still splits correctly across
    categories instead of an all-or-nothing toggle.
    """
    run_weeks = list(run.run_weeks.select_related("program_week").order_by("sequence"))
    labels = [f"W{rw.program_week.number}·p{rw.sequence}" for rw in run_weeks]
    seq_index = {rw.id: i for i, rw in enumerate(run_weeks)}

    names, counts, grid = {}, {}, {}   # names[key] -> display name, grid[key] -> [None]*len(run_weeks)

    pws = (ProgramWorkout.objects
           .filter(run_week__run=run)
           .select_related("workout", "run_week"))
    for pw in pws:
        col = seq_index.get(pw.run_week_id)
        if col is None:
            continue
        for key, name, top_lb, vol_lb, reps in _workout_exercise_loads(pw.workout):
            if not top_lb:   # bodyweight-only movement — never any real load, no signal here
                continue
            if category and _muscle_group_for(name) != category:
                continue
            names.setdefault(key, name)
            counts[key] = counts.get(key, 0) + 1
            row = grid.setdefault(key, [None] * len(run_weeks))
            val = {"top_weight": top_lb, "volume": vol_lb, "reps": reps}[metric]
            # if two sessions in the same pass hit the same exercise, keep the higher
            row[col] = max(row[col], val) if row[col] is not None else val

    keys = sorted(grid, key=lambda k: (-counts[k], names[k].lower()))
    hidden_count = max(0, len(keys) - MAX_PROGRESSION_SERIES)
    keys = keys[:MAX_PROGRESSION_SERIES]
    series = [{"exercise": names[k], "key": k, "points": [round(v, 1) if v is not None else None for v in grid[k]]}
              for k in sorted(keys, key=lambda k: names[k].lower())]
    return {"labels": labels, "series": series, "metric": metric, "hidden_count": hidden_count}


def _workout_exercise_loads(workout):
    """Yield (key, display_name, top_weight_lb, volume_lb, total_reps) per exercise for one workout."""
    sets = getattr(workout, "exercise_sets_json", None) or []
    if sets:
        per_ex = {}
        for s in sets:
            key = s.get("exercise_key") or s.get("exercise")
            if not key:
                continue
            w = s.get("weight_kg")
            reps = s.get("reps") or 0
            bucket = per_ex.setdefault(key, {"name": s.get("exercise") or key, "top": 0.0, "vol": 0.0, "reps": 0})
            bucket["reps"] += reps
            if w is not None:
                w_lb = w * KG_TO_LB
                bucket["top"] = max(bucket["top"], w_lb)
                bucket["vol"] += w_lb * reps
        for key, agg in per_ex.items():
            yield key, agg["name"], agg["top"], agg["vol"], agg["reps"]
        return

    # Peloton's own weight-tracking classes (e.g. "gold" Movement Tracker tier) record
    # one summary row per movement instead of per-set — weight is already in lb, and
    # reps_done is already the session total (not per-set). A movement can appear more
    # than once in a single workout (e.g. two supersets of the same exercise), so
    # aggregate by name before yielding.
    per_ex = {}
    for m in getattr(workout, "movements", None) or []:
        name = m.get("name")
        if not name:
            continue
        weight_lb = m.get("weight_lbs") or 0
        reps_done = m.get("reps_done") or 0
        volume_lb = m.get("volume")
        if volume_lb is None:
            volume_lb = weight_lb * reps_done
        bucket = per_ex.setdefault(name, {"top": 0.0, "vol": 0.0, "reps": 0})
        bucket["top"] = max(bucket["top"], weight_lb)
        bucket["vol"] += volume_lb
        bucket["reps"] += reps_done
    for name, agg in per_ex.items():
        yield name, name, agg["top"], agg["vol"], agg["reps"]


# ---------- building a new split from the UI ----------

def resolve_split_candidates(workout_ids):
    """
    Turn a set of explicitly-selected CachedWorkout ids (picked by the user on the
    History page) into one slot candidate per distinct ride-id among them.

    No title-based grouping: an earlier version grouped candidates by title, which
    silently merged unrelated rides that happen to share a generic name (e.g. "5 min
    Post-Run Stretch" — 115 different ride-ids) and silently failed to merge things
    that should've been merged. Grounding each slot in the ride-id of a workout the
    user actually picked is unambiguous; the user makes the merge/split call by
    which workouts they select, not a title heuristic.
    """
    workouts = (CachedWorkout.objects
                .filter(workout_id__in=workout_ids, source="peloton")
                .exclude(ride_id=""))
    by_ride = {}
    for w in workouts:
        g = by_ride.setdefault(w.ride_id, {
            "title": w.title, "discipline": w.discipline, "count": 0, "instructors": {},
        })
        g["count"] += 1
        if w.instructor_name:
            g["instructors"][w.instructor_name] = g["instructors"].get(w.instructor_name, 0) + 1

    claimed_by = dict(
        ProgramSlot.objects
        .filter(peloton_ride_id__in=by_ride.keys())
        .select_related("week__program")
        .values_list("peloton_ride_id", "week__program__name")
    )
    for slot in ProgramSlot.objects.exclude(alt_ride_ids=[]).select_related("week__program"):
        for rid in slot.alt_ride_ids:
            if rid in by_ride:
                claimed_by[rid] = slot.week.program.name

    candidates = [
        {"ride_id": rid, "title": g["title"], "discipline": g["discipline"], "count": g["count"],
         "claimed_by": claimed_by.get(rid),
         "instructor": max(g["instructors"], key=g["instructors"].get) if g["instructors"] else ""}
        for rid, g in by_ride.items()
    ]
    candidates.sort(key=lambda c: c["title"].lower())
    return candidates


def program_ride_ids(program):
    """Every ride-id (canonical + alt) that can match one of this program's slots."""
    ride_ids = set()
    for slot in ProgramSlot.objects.filter(week__program=program).exclude(peloton_ride_id=""):
        ride_ids.add(slot.peloton_ride_id)
        ride_ids.update(slot.alt_ride_ids)
    return ride_ids


def backfill_program(program):
    """
    Re-run the matcher over every workout that could belong to this program — scoped
    to its ride-ids, so it's fast (not a full associate_programs pass over every
    workout in the app). associate_workout is idempotent, so this is always safe to
    re-run; it's the only thing that picks up workouts a rebuild orphaned (e.g. after
    deleting a pass) or that were never associated in the first place. Returns the
    count of new associations made.
    """
    ride_ids = program_ride_ids(program)
    workouts = CachedWorkout.objects.filter(ride_id__in=ride_ids).order_by("created_at")
    made = 0
    for w in workouts:
        existing = ProgramWorkout.objects.filter(workout=w).exists()
        if not existing and associate_workout(w):
            made += 1
    return made


def recompute_run_dates(run):
    """
    Sync start_date to the earliest completion still in the run and, if the run
    has already ended, end_date to the latest — so the displayed range (and
    ProgramRun.display_name, which is derived from it) never drifts from what's
    actually in the run after a pass gets deleted or a backfill adds history
    back in. A no-op if the run has no completions left. Never sets end_date on
    a still-current run — that's an explicit action (Complete / Start new cycle),
    not an automatic side effect of a date sync.
    """
    dates = sorted(
        d for d in (
            workout_local_date(e.workout)
            for e in ProgramWorkout.objects.filter(run_week__run=run).select_related("workout")
        ) if d
    )
    if not dates:
        return
    update_fields = []
    if dates[0] != run.start_date:
        run.start_date = dates[0]
        update_fields.append("start_date")
    if run.end_date is not None and dates[-1] != run.end_date:
        run.end_date = dates[-1]
        update_fields.append("end_date")
    if update_fields:
        run.save(update_fields=update_fields)


def create_split(name, slug, selected_candidates):
    """
    Create a Program (kind=split, match_strategy=ride_ids) with one ProgramSlot per
    selected candidate (from resolve_split_candidates), then backfill history scoped
    to just those ride-ids — cheap, unlike a full associate_programs run.
    """
    from collections import Counter
    instructor_votes = Counter()
    for c in selected_candidates:
        if c.get("instructor"):
            instructor_votes[c["instructor"]] += c["count"]
    instructor = instructor_votes.most_common(1)[0][0] if instructor_votes else ""

    program = Program.objects.create(
        name=name, slug=slug, kind="split", match_strategy="ride_ids", instructor=instructor,
    )
    week = ProgramWeek.objects.create(program=program, number=1)
    for i, c in enumerate(selected_candidates):
        ProgramSlot.objects.create(
            week=week, peloton_ride_id=c["ride_id"],
            title=c["title"], discipline=c["discipline"], order=i,
        )

    backfill_program(program)
    return program


# ---------- building a new plan from the UI (link / screenshot / text intake) ----------

def verify_ride_id(ride_id):
    """
    Live-confirm a ride_id resolves to a real Peloton class, and return the
    catalog facts about it. Used both to validate a link-extracted id and to
    let the review table show what a manually-pasted id/link actually is
    before the user commits to it.
    """
    from .services.peloton_client import PelotonClient
    try:
        detail = PelotonClient().get_ride_details(ride_id)
    except Exception:
        return None
    ride = detail.get("ride") or {}
    if not ride.get("id"):
        return None
    return {
        "ride_id": ride["id"],
        "title": ride.get("title", ""),
        "duration_min": round((ride.get("duration") or 0) / 60) or None,
        "discipline": ride.get("fitness_discipline", ""),
        "instructor": ((detail.get("ride") or {}).get("instructor") or {}).get("name", "")
                      or (ride.get("instructor") or {}).get("name", ""),
    }


def find_local_ride_ids(title, instructor=""):
    """
    Match a plan-slot title against the user's own already-synced Peloton
    history. A class the user has already taken resolves for free, with no
    external lookup — this is the most common way slots for older content
    (no achievement badge, no title-suffix tagging) get pinned automatically.
    Returns candidates most-recent-first, deduped by ride_id.
    """
    norm = normalize_title(title)
    if not norm:
        return []
    qs = CachedWorkout.objects.filter(source="peloton").exclude(ride_id="")
    if instructor:
        qs = qs.filter(instructor_name__iexact=instructor)
    seen = {}
    for w in qs.order_by("-created_at").iterator():
        wnorm = normalize_title(w.title)
        if wnorm and (wnorm == norm or norm in wnorm or wnorm in norm) and w.ride_id not in seen:
            seen[w.ride_id] = {
                "ride_id": w.ride_id, "title": w.title,
                "instructor": w.instructor_name, "taken_on": workout_local_date(w),
            }
    return sorted(seen.values(), key=lambda c: c["taken_on"] or date.min, reverse=True)


def resolve_slot_ride_id(title, instructor="", source_url=""):
    """
    Best-effort ride_id resolution for one plan slot, cascading:
    1) a Peloton class link pasted alongside this item (ground truth — verified live)
    2) an exact/near title match already in the user's own synced history

    Peloton's on-demand catalog has no reachable title-search endpoint from this
    app's session (verified live — /api/v2/ride/archived 401s, /api/ride/search
    is an unrelated endpoint), so anything not covered by 1) or 2) is left for
    manual entry in the review table rather than guessed.

    Returns {"ride_id": str|None, "matched_via": "link"|"history"|"ambiguous"|"none",
    "info": dict|None, "candidates": [...]}.
    """
    linked = extract_ride_id_from_text(source_url)
    if linked:
        info = verify_ride_id(linked)
        if info:
            return {"ride_id": linked, "matched_via": "link", "info": info, "candidates": []}
    local = find_local_ride_ids(title, instructor=instructor)
    if len(local) == 1:
        return {"ride_id": local[0]["ride_id"], "matched_via": "history", "info": None, "candidates": []}
    if len(local) > 1:
        return {"ride_id": None, "matched_via": "ambiguous", "info": None, "candidates": local}
    return {"ride_id": None, "matched_via": "none", "info": None, "candidates": []}


def _expand_repeats(base, week, weeks_by_number, s):
    """
    A slot marked repeat_all_days / repeat_all_weeks is authored once but
    needs an open slot for every day and/or week it can be retaken into — see
    _slot_for's ride-id branch (day spread within a week) and
    _resolve_recurring_week (week spread) for the matching-side half of this.
    Duplicates are always optional, regardless of the original slot's flag.
    Checking both flags cross-products days x weeks.
    """
    ride_id = base.peloton_ride_id
    if not ride_id or not (s.get("repeat_all_days") or s.get("repeat_all_weeks")):
        return
    weeks = list(weeks_by_number.values()) if s.get("repeat_all_weeks") else [week]
    days = list(range(1, 8)) if s.get("repeat_all_days") else [base.day]
    for wk_obj in weeks:
        for d in days:
            if wk_obj.pk == week.pk and d == base.day:
                continue   # that's `base` itself
            ProgramSlot.objects.create(
                week=wk_obj, day=d, order=base.order,
                title=base.title, discipline=base.discipline,
                duration_min=base.duration_min,
                peloton_ride_id=ride_id, optional=True,
            )


def create_plan(name, slug, instructor, weeks_data):
    """
    Create a Program (kind=plan, match_strategy=ride_ids) from a structured
    skeleton — weeks_data: [{"number": 1, "slots": [{"day", "order", "title",
    "discipline", "duration_min", "optional", "ride_id", "repeat_all_days",
    "repeat_all_weeks"}, ...]}, ...]. Structurally the multi-week counterpart
    of create_split: every slot is ride-id pinned up front rather than
    discovered from History picks, so the grid is fully seeded (including
    weeks not yet started) the moment the plan is created. Slots left without
    a ride_id (unresolved at review time) are still created — they just won't
    auto-match until pinned later via ProgramSlot.peloton_ride_id, e.g. by
    editing the row after taking the class.
    """
    program = Program.objects.create(
        name=name, slug=slug, kind="plan", match_strategy="ride_ids", instructor=instructor,
    )
    weeks_by_number = {wk["number"]: ProgramWeek.objects.create(program=program, number=wk["number"])
                       for wk in weeks_data}
    for wk in weeks_data:
        week = weeks_by_number[wk["number"]]
        for s in wk["slots"]:
            base = ProgramSlot.objects.create(
                week=week, day=s.get("day"), order=s.get("order", 0),
                title=s["title"], discipline=s.get("discipline", ""),
                duration_min=s.get("duration_min"),
                peloton_ride_id=s.get("ride_id") or "",
                optional=bool(s.get("optional")),
            )
            _expand_repeats(base, week, weeks_by_number, s)
    backfill_program(program)
    return program


# ---------- program retrospective — aggregation for the Sonnet prompt ----------

def _run_window(run):
    start = run.start_date
    end = run.end_date or date.today()
    return start, end


def adherence_summary(run):
    """Completion of non-optional slots, per week and overall, plus skip counts by slot title."""
    weeks = []
    skipped = {}
    done_total = planned_total = 0
    for rw in run.run_weeks.select_related("program_week").order_by("sequence"):
        slots = [s for s in rw.program_week.slots.all() if not s.optional]
        planned = len(slots)
        done = rw.entries.filter(slot__optional=False).count()
        # loose (week-matched, no slot) count toward done but not planned
        done += rw.entries.filter(slot__isnull=True).count()
        planned_total += planned
        done_total += min(done, planned) if planned else 0
        for s in slots:
            if not rw.entries.filter(slot=s).exists():
                skipped[s.title] = skipped.get(s.title, 0) + 1
        weeks.append({"seq": rw.sequence, "week": rw.program_week.number,
                      "done": done, "planned": planned,
                      "pct": round(100 * done / planned) if planned else None,
                      "rpe": rw.rpe, "note": rw.note})
    overall = round(100 * done_total / planned_total) if planned_total else None
    return {"overall_pct": overall, "weeks": weeks,
            "most_skipped": sorted(skipped.items(), key=lambda kv: -kv[1])[:5]}


def cadence_summary(run):
    """Gaps between sessions, longest streak of consecutive active days, calendar length vs weeks."""
    dates = sorted({workout_local_date(e.workout)
                    for rw in run.run_weeks.all() for e in rw.entries.all()
                    if workout_local_date(e.workout)})
    if not dates:
        return {"sessions": 0}
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    start, end = _run_window(run)
    return {"sessions": len(dates),
            "span_days": (end - start).days,
            "weeks_of_passes": run.run_weeks.count(),
            "max_gap_days": max(gaps) if gaps else 0,
            "avg_gap_days": round(mean(gaps), 1) if gaps else 0}


def recovery_across_block(run):
    """Week-1 vs final-week averages for HRV / sleep / body battery / readiness (DailyStats)."""
    from .models import DailyStats
    start, end = _run_window(run)

    def block_avg(d0, d1, field):
        vals = [getattr(s, field) for s in
                DailyStats.objects.filter(date__gte=d0, date__lt=d1)
                if getattr(s, field, None) is not None]
        return round(mean(vals), 1) if vals else None

    fields = ["hrv_last_night", "sleep_score", "body_battery_high", "training_readiness_score"]
    first = {f: block_avg(start, start + timedelta(days=7), f) for f in fields}
    last = {f: block_avg(end - timedelta(days=7), end + timedelta(days=1), f) for f in fields}
    return {"first_week": first, "last_week": last}


def intervention_overlap(run):
    """Interventions / dose changes whose dates fall inside the run window (confounders)."""
    from .models import Intervention
    start, end = _run_window(run)
    out = []
    for iv in Intervention.objects.all():
        if iv.start_date <= end and (iv.end_date is None or iv.end_date >= start):
            doses = [f"{dc.dose} from {dc.start_date}"
                     for dc in iv.dose_changes.filter(start_date__gte=start, start_date__lte=end)]
            out.append({"name": iv.name, "category": iv.category,
                        "expected": iv.expected_effects, "dose_changes_in_window": doses})
    return out


def load_vs_rpe(run):
    """Pair per-pass load trend with RPE so the model can flag divergence. Sparse-safe."""
    prog = run_exercise_progression(run, metric="top_weight")
    # collapse to a single 'mean top-set across exercises' per pass for a coarse trend
    per_pass = []
    for i, label in enumerate(prog["labels"]):
        vals = [s["points"][i] for s in prog["series"] if s["points"][i] is not None]
        per_pass.append(round(mean(vals), 1) if vals else None)
    rpes = [rw.rpe for rw in run.run_weeks.order_by("sequence")]
    rated = sum(1 for r in rpes if r is not None)
    return {"labels": prog["labels"], "mean_top_set_lb": per_pass,
            "rpe": rpes, "rpe_coverage": f"{rated}/{len(rpes)}"}


def build_retrospective_context(run):
    prog_weight = run_exercise_progression(run, metric="top_weight")
    prog_reps = run_exercise_progression(run, metric="reps")
    reps_points = {s["key"]: s["points"] for s in prog_reps["series"]}

    # start vs end per exercise, weight and reps together — so a flat-weight/rising-reps
    # exercise (still real progress) is visible in one place rather than two separate lists
    deltas = []
    for s in prog_weight["series"]:
        pts = [p for p in s["points"] if p is not None]
        reps_pts = [p for p in reps_points.get(s["key"], []) if p is not None]
        if len(pts) < 2 and len(reps_pts) < 2:
            continue
        d = {"exercise": s["exercise"]}
        if len(pts) >= 2:
            d.update({"start_top_set_lb": pts[0], "end_top_set_lb": pts[-1],
                      "change_lb": round(pts[-1] - pts[0], 1),
                      "change_pct": round(100 * (pts[-1] - pts[0]) / pts[0], 1) if pts[0] else None})
        if len(reps_pts) >= 2:
            d.update({"start_reps": reps_pts[0], "end_reps": reps_pts[-1],
                      "reps_change": round(reps_pts[-1] - reps_pts[0], 1)})
        deltas.append(d)
    ctx = {
        "program": run.program.name, "kind": run.program.kind,
        "window": [str(x) for x in _run_window(run)],
        "adherence": adherence_summary(run),
        "cadence": cadence_summary(run),
        "progression_deltas": sorted(deltas, key=lambda d: (d.get("change_pct") or 0)),
        "load_vs_rpe": load_vs_rpe(run),
        "recovery": recovery_across_block(run),
        "interventions": intervention_overlap(run),
    }
    # cross-cycle: only when a prior ended run of the same program exists
    prior = (run.program.runs.filter(end_date__isnull=False)
             .exclude(pk=run.pk).order_by("-end_date").first())
    if prior:
        prior_adh = adherence_summary(prior)
        prior_reps = {s["key"]: s["points"] for s in run_exercise_progression(prior, metric="reps")["series"]}
        ctx["prior_cycle"] = {"label": prior.display_name,
                              "overall_pct": prior_adh["overall_pct"],
                              "progression": [
                                  {"exercise": s["exercise"],
                                   "end_top_set_lb": next((p for p in reversed(s["points"]) if p is not None), None),
                                   "end_reps": next((p for p in reversed(prior_reps.get(s["key"], [])) if p is not None), None)}
                                  for s in run_exercise_progression(prior)["series"]]}
    return ctx
