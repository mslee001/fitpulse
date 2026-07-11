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
        slot = (ProgramSlot.objects
                .filter(Q(peloton_ride_id=ride_id) | Q(alt_ride_ids__contains=ride_id))
                .select_related("week", "week__program").first())
        if slot:
            return slot.week.program, slot.week.number, slot.day, "ride_id"

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
    """
    ride_id = workout.ride_id
    if ride_id:
        s = program_week.slots.filter(
            Q(peloton_ride_id=ride_id) | Q(alt_ride_ids__contains=ride_id)
        ).first()
        if s:
            return [s]
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

    pw_week = program.weeks.filter(number=week_number).first()
    if pw_week is None:   # week not seeded (e.g. a plan week we didn't define) -> create bare
        pw_week = ProgramWeek.objects.create(program=program, number=week_number)

    on_date = workout_local_date(workout) or date.today()
    run = get_or_create_active_run(program, on_date)
    slot_candidates = _slot_for(workout, pw_week, matched_by, day=day)
    return fill_or_append(run, workout, pw_week, slot_candidates, matched_by)


# ---------- cross-cycle exercise load progression ----------

KG_TO_LB = 2.20462

MAX_PROGRESSION_SERIES = 8   # matches the app's 8-color validated categorical palette

def progression_slots(run):
    """Slots in this run with at least one completion carrying exercise load data —
    the set worth offering as a chart filter. Ordered by (week, slot order)."""
    slot_ids = set()
    pws = (ProgramWorkout.objects
           .filter(run_week__run=run, slot__isnull=False)
           .select_related("workout", "slot"))
    for pw in pws:
        if pw.slot_id in slot_ids:
            continue
        if any(True for _ in _workout_exercise_loads(pw.workout)):
            slot_ids.add(pw.slot_id)
    return list(
        ProgramSlot.objects.filter(pk__in=slot_ids)
        .select_related("week").order_by("week__number", "order")
    )


def run_exercise_progression(run, metric="top_weight", slot_id=None):
    """
    Returns {"labels": [...pass labels...], "series": [{"exercise","key","points":[...]}...],
    "hidden_count": N}. metric: "top_weight" (max weight in the session) or "volume"
    (sum reps*weight). Contributes from either data source a strength workout may carry:
    Garmin's per-set `exercise_sets_json` (weight_kg, converted to lb), or Peloton's
    Movement Tracker `movements` summary (already aggregated per movement, in lb).

    Bodyweight-only movements (never any recorded weight) are dropped — they carry no
    signal on a load chart. If more than MAX_PROGRESSION_SERIES exercises remain, only
    the most-frequently-tracked ones are kept, since one canvas can't stay legible with
    the ride's full movement roster (a "9th series" folds out rather than getting an
    invented color) — this is normal for slots that span two ride-id variants of a
    class with slightly different movement lineups. Pass `slot_id` to scope to a single
    class instead of every slot in the run.
    """
    run_weeks = list(run.run_weeks.select_related("program_week").order_by("sequence"))
    labels = [f"W{rw.program_week.number}·p{rw.sequence}" for rw in run_weeks]
    seq_index = {rw.id: i for i, rw in enumerate(run_weeks)}

    names, counts, grid = {}, {}, {}   # names[key] -> display name, grid[key] -> [None]*len(run_weeks)

    pws = (ProgramWorkout.objects
           .filter(run_week__run=run)
           .select_related("workout", "run_week"))
    if slot_id:
        pws = pws.filter(slot_id=slot_id)
    for pw in pws:
        col = seq_index.get(pw.run_week_id)
        if col is None:
            continue
        for key, name, top_lb, vol_lb in _workout_exercise_loads(pw.workout):
            if not top_lb:   # bodyweight-only movement — never any real load, no signal here
                continue
            names.setdefault(key, name)
            counts[key] = counts.get(key, 0) + 1
            row = grid.setdefault(key, [None] * len(run_weeks))
            val = top_lb if metric == "top_weight" else vol_lb
            # if two sessions in the same pass hit the same exercise, keep the higher
            row[col] = max(row[col], val) if row[col] is not None else val

    keys = sorted(grid, key=lambda k: (-counts[k], names[k].lower()))
    hidden_count = max(0, len(keys) - MAX_PROGRESSION_SERIES)
    keys = keys[:MAX_PROGRESSION_SERIES]
    series = [{"exercise": names[k], "key": k, "points": [round(v, 1) if v is not None else None for v in grid[k]]}
              for k in sorted(keys, key=lambda k: names[k].lower())]
    return {"labels": labels, "series": series, "metric": metric, "hidden_count": hidden_count}


def _workout_exercise_loads(workout):
    """Yield (key, display_name, top_weight_lb, volume_lb) per exercise for one workout."""
    sets = getattr(workout, "exercise_sets_json", None) or []
    if sets:
        per_ex = {}
        for s in sets:
            key = s.get("exercise_key") or s.get("exercise")
            if not key:
                continue
            w = s.get("weight_kg")
            reps = s.get("reps") or 0
            bucket = per_ex.setdefault(key, {"name": s.get("exercise") or key, "top": 0.0, "vol": 0.0})
            if w is not None:
                w_lb = w * KG_TO_LB
                bucket["top"] = max(bucket["top"], w_lb)
                bucket["vol"] += w_lb * reps
        for key, agg in per_ex.items():
            yield key, agg["name"], agg["top"], agg["vol"]
        return

    # Peloton's own weight-tracking classes (e.g. "gold" Movement Tracker tier) record
    # one summary row per movement instead of per-set — weight is already in lb. A
    # movement can appear more than once in a single workout (e.g. two supersets of
    # the same exercise), so aggregate by name before yielding.
    per_ex = {}
    for m in getattr(workout, "movements", None) or []:
        name = m.get("name")
        if not name:
            continue
        weight_lb = m.get("weight_lbs") or 0
        volume_lb = m.get("volume")
        if volume_lb is None:
            volume_lb = weight_lb * (m.get("reps_done") or 0)
        bucket = per_ex.setdefault(name, {"top": 0.0, "vol": 0.0})
        bucket["top"] = max(bucket["top"], weight_lb)
        bucket["vol"] += volume_lb
    for name, agg in per_ex.items():
        yield name, name, agg["top"], agg["vol"]


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
    prog = run_exercise_progression(run, metric="top_weight")
    # start vs end per exercise
    deltas = []
    for s in prog["series"]:
        pts = [p for p in s["points"] if p is not None]
        if len(pts) >= 2:
            deltas.append({"exercise": s["exercise"], "start": pts[0], "end": pts[-1],
                           "change_lb": round(pts[-1] - pts[0], 1),
                           "change_pct": round(100 * (pts[-1] - pts[0]) / pts[0], 1) if pts[0] else None})
    ctx = {
        "program": run.program.name, "kind": run.program.kind,
        "window": [str(x) for x in _run_window(run)],
        "adherence": adherence_summary(run),
        "cadence": cadence_summary(run),
        "progression_deltas": sorted(deltas, key=lambda d: (d["change_pct"] or 0)),
        "load_vs_rpe": load_vs_rpe(run),
        "recovery": recovery_across_block(run),
        "interventions": intervention_overlap(run),
    }
    # cross-cycle: only when a prior ended run of the same program exists
    prior = (run.program.runs.filter(end_date__isnull=False)
             .exclude(pk=run.pk).order_by("-end_date").first())
    if prior:
        prior_adh = adherence_summary(prior)
        ctx["prior_cycle"] = {"label": prior.label or str(prior.start_date),
                              "overall_pct": prior_adh["overall_pct"],
                              "progression": [
                                  {"exercise": s["exercise"],
                                   "end": next((p for p in reversed(s["points"]) if p is not None), None)}
                                  for s in run_exercise_progression(prior)["series"]]}
    return ctx
