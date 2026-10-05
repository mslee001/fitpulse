"""AI training plans — the form, draft polling, review/swap and create views.
All plan logic lives in training_plans.py; these views only route and render."""
import json
from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import training_plans as tp
from .catalog import DifficultyRanker
from .models import PelotonClass, PlanDraft, Program, ProgramSlot, ProgramWorkout
from .onboarding_views import is_owner


def _drafts(request):
    return PlanDraft.objects.for_user(request.user)


def _form_values(inputs):
    """Turn stored inputs back into form field values (for ?from=<pk> and error re-renders)."""
    v = dict(inputs)
    if inputs.get("target_time"):
        v["target_time"] = tp._fmt_hms(inputs["target_time"])
    v["level"] = inputs.get("level_choice", "auto")
    v["pace_level"] = inputs.get("pace_level_choice", "auto")
    return v


def _post_values(post):
    v = {k: post.get(k, "") for k in post}
    v["days"] = [int(d) for d in post.getlist("days") if d.isdigit()]
    for k in ("long_day", "companion_program_id", "strength_per_week", "weeks"):
        if str(v.get(k, "")).isdigit():
            v[k] = int(v[k])
    v["mobility"] = bool(post.get("mobility"))
    v["allow_doubles"] = bool(post.get("allow_doubles"))
    return v


def program_training_plan_new(request):
    """GET/POST /programs/training-plan/new/ — the goal form; POST starts generation."""
    catalog_empty = not PelotonClass.objects.filter(is_available=True).exists()
    errors = []
    if request.method == "POST" and not catalog_empty:
        inputs, errors = tp.clean_inputs(request.user, request.POST)
        if not errors:
            draft = PlanDraft.objects.create(user=request.user, inputs_json=inputs)
            tp.start_generation(draft)
            return redirect("program_training_plan_draft", pk=draft.pk)
        values = _post_values(request.POST)
    else:
        values = {"goal": "5k", "start_date": tp.default_start().isoformat(), "max_weekday_min": 45,
                  "max_weekend_min": 75, "setting": "tread", "mode": "standalone", "strength_per_week": 2,
                  "level": "auto", "pace_level": "auto", "days": [], "weeks": 8}
        src = request.GET.get("from")
        if src and src.isdigit():
            prior = _drafts(request).filter(pk=src).first()
            if prior:
                values.update(_form_values(prior.inputs_json))
    assessment = tp.assess_running_level(request.user)
    pace_level = tp.latest_pace_level(request.user)
    return render(request, "workouts/program_training_plan_new.html", {
        "pace_level": pace_level, "pace_levels": tp.PACE_LEVELS,
        "pace_zones_display": [(z["name"], tp._mph_to_pace(z["hi"]), tp._mph_to_pace(z["lo"]))
                               for z in tp.chart_zones(pace_level["level"])
                               if z["name"] not in ("Recovery", "Max")] if pace_level else [],
        "pace_json": json.dumps({"chart": tp.PELOTON_PACE_CHART, "zoneOrder": tp.PACE_ZONES,
                                 "level": (pace_level or {}).get("level"), "raceZone": tp.RACE_PACE_ZONE,
                                 "miles": tp.RACE_MILES, "longMultiple": tp.LONG_RUN_RACE_MULTIPLE,
                                 "longFloor": tp.LONG_RUN_FLOOR_MIN,
                                 "runLengths": tp.run_lengths_for_form()}),
        "catalog_empty": catalog_empty, "is_owner": is_owner(request.user),
        "errors": errors, "values": values, "assessment": assessment,
        "goals": tp.GOALS, "levels": tp.LEVELS, "days": tp.DAY_NAMES.items(),
        "companions": tp.companion_choices(request.user),
        "runway_json": json.dumps(tp.RUNWAY_MIN_WEEKS),
    })


# ---------------------------------------------------------------------------
# Draft page
# ---------------------------------------------------------------------------

def _review_weeks(draft):
    """Week cards for the review page: plan slots with their picked class, plus
    the companion program's sessions (alongside mode) as muted rows."""
    inputs, spec, picks = draft.inputs_json, draft.spec_json, draft.picks_json
    ids = set()
    for p in picks.values():
        ids |= {p.get("ride_id")} | set(p.get("alternates", []))
    classes = tp.classes_by_id(ids)
    ranker = DifficultyRanker()
    companion = tp.companion_schedule(draft.user, inputs)
    _, race = tp.plan_dates(inputs)
    weeks = []
    for wk in spec.get("weeks", []):
        dates = tp.week_dates(inputs, wk["number"])
        rows = [_slot_row_context(draft, wk["number"], slot, classes, ranker) | {"kind": "slot"}
                for slot in wk["slots"]]
        for c in companion.get(wk["number"], []):
            rows.append({"kind": "companion", "day": c["day"], "order": -1, "c": c})
        if race and inputs.get("race_week") == wk["number"]:
            rows.append({"kind": "race", "day": inputs["race_weekday"], "order": 99,
                         "label": tp.RACE_LABELS.get(inputs["goal"], "Race")})
        rows.sort(key=lambda r: (r["day"], r["kind"] != "companion", r.get("order", 0)))
        for r in rows:
            r["date"] = dates[r["day"] - 1]
        weeks.append({"number": wk["number"], "phase": wk["phase"], "focus": wk["focus"], "rows": rows,
                      "start": dates[0], "end": dates[-1]})
    return weeks


def _slot_row_context(draft, week, slot, classes=None, ranker=None):
    key = tp.slot_key(week, slot)
    pick = draft.picks_json.get(key) or {"ride_id": None, "alternates": []}
    if classes is None:
        classes = tp.classes_by_id({pick.get("ride_id")} | set(pick.get("alternates", [])))
    from datetime import date
    last_taken = date.fromisoformat(pick["last_taken"]) if pick.get("last_taken") else None
    cls = classes.get(pick.get("ride_id"))
    return {"key": key, "week": week, "slot": slot, "day": slot["day"], "order": slot["order"], "pick": pick,
            "last_taken": last_taken, "cls": cls, "rank": (ranker or DifficultyRanker()).rank(cls) if cls else None,
            "alternates": [classes[r] for r in pick.get("alternates", []) if r in classes],
            "day_name": tp.DAY_NAMES[slot["day"]]}


def program_training_plan_draft(request, pk):
    draft = get_object_or_404(_drafts(request), pk=pk).refreshed()
    PlanDraft.prune(request.user)
    if draft.status == "created" and draft.program_id:
        return redirect("program_detail", slug=draft.program.slug)
    ctx = {"draft": draft, "inputs": draft.inputs_json}
    if draft.status == "ready":
        start, race = tp.plan_dates(draft.inputs_json)
        ctx.update({
            "weeks": _review_weeks(draft), "start": start, "race": race, "pace": _pace_summary(draft),
            "goal_label": tp.GOALS.get(draft.inputs_json.get("goal"), ""),
            "companion": (Program.objects.for_user(request.user)
                          .filter(pk=draft.inputs_json.get("companion_program_id")).first()),
            "other_active": ([p for p in tp.companion_choices(request.user)]
                             if draft.inputs_json.get("mode") == "standalone" else []),
        })
    return render(request, "workouts/program_training_plan_draft.html", ctx)


def _pace_summary(draft):
    return tp.pace_summary(draft.inputs_json, draft.spec_json)


def program_training_plan_status(request, pk):
    draft = get_object_or_404(_drafts(request), pk=pk).refreshed()
    if draft.status != "generating":
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = reverse("program_training_plan_draft", args=[draft.pk])
        return resp
    return render(request, "workouts/partials/training_plan_status.html", {"draft": draft})


@require_POST
def program_training_plan_retry(request, pk):
    draft = get_object_or_404(_drafts(request), pk=pk).refreshed()
    if draft.status not in ("failed", "ready"):
        return redirect("program_training_plan_draft", pk=draft.pk)
    tp.reset_and_regenerate(draft)
    return redirect("program_training_plan_draft", pk=draft.pk)


def _ready_draft(request, pk):
    """(draft, None) when the draft is ready for review actions, else (None, response)."""
    draft = get_object_or_404(_drafts(request), pk=pk).refreshed()
    if draft.status == "created" and draft.program_id:
        return None, redirect("program_detail", slug=draft.program.slug)
    if draft.status != "ready":
        return None, HttpResponseBadRequest("This plan isn't ready for review.")
    return draft, None


def _find_slot(draft, key):
    for week, slot in tp.iter_slots(draft.spec_json):
        if tp.slot_key(week, slot) == key:
            return week, slot
    return None, None


def _render_row(request, draft, key):
    week, slot = _find_slot(draft, key)
    return render(request, "workouts/partials/training_plan_slot_row.html",
                  {"draft": draft, "r": _slot_row_context(draft, week, slot)})


@require_POST
def program_training_plan_swap(request, pk):
    draft, resp = _ready_draft(request, pk)
    if resp:
        return resp
    key = request.POST.get("key", "")
    if _find_slot(draft, key)[1] is None:
        return HttpResponseBadRequest("Unknown slot.")
    tp.swap_pick(draft, key)
    return _render_row(request, draft, key)


@require_POST
def program_training_plan_pick(request, pk):
    draft, resp = _ready_draft(request, pk)
    if resp:
        return resp
    key, ride_id = request.POST.get("key", ""), request.POST.get("ride_id", "")
    if _find_slot(draft, key)[1] is None:
        return HttpResponseBadRequest("Unknown slot.")
    if ride_id in tp.draft_ride_ids(draft) - {draft.picks_json[key].get("ride_id")}:
        return HttpResponseBadRequest("That class is already used elsewhere in this plan.")
    try:
        tp.set_pick(draft, key, ride_id)
    except ValueError:
        return HttpResponseBadRequest("That class isn't one of this slot's alternates.")
    return _render_row(request, draft, key)


@require_POST
def program_training_plan_create(request, pk):
    draft, resp = _ready_draft(request, pk)
    if resp:
        return resp
    name = (request.POST.get("name") or draft.spec_json.get("plan_name") or "Training plan").strip()[:200]
    program = tp.create_program_from_draft(draft, name)
    messages.success(request, f"Created {program.name}. Its cycle starts {program.goal_json['start_date']}.")
    return redirect("program_run", pk=program.active_run.pk)


@require_POST
def program_training_plan_discard(request, pk):
    draft = get_object_or_404(_drafts(request), pk=pk)
    if draft.status == "created" and draft.program_id:
        return redirect("program_detail", slug=draft.program.slug)
    draft.delete()
    messages.success(request, "Discarded the draft plan.")
    return redirect("program_list")


# ---------------------------------------------------------------------------
# Post-creation swap (no AI) — feature "programs"
# ---------------------------------------------------------------------------

def plan_slot_date(slot):
    """The calendar date of an AI-plan slot (Mon–Sun weeks from goal_json's start), or None."""
    goal = slot.week.program.goal_json or {}
    if not goal.get("start_date") or not slot.day:
        return None
    from datetime import date
    start = date.fromisoformat(goal["start_date"])
    return start - timedelta(days=start.weekday()) + timedelta(weeks=slot.week.number - 1, days=slot.day - 1)


def slot_swappable(slot, entry=None):
    d = plan_slot_date(slot)
    return bool(slot.spec_json) and entry is None and d is not None and d >= timezone.localdate()


@require_POST
def program_slot_swap(request, pk):
    slot = get_object_or_404(ProgramSlot.objects.filter(week__program__user=request.user)
                             .select_related("week", "week__program"), pk=pk)
    run = slot.week.program.active_run
    entry = ProgramWorkout.objects.filter(slot=slot, run_week__run=run).first() if run else None
    if not slot_swappable(slot, entry):
        return HttpResponseBadRequest("Only upcoming, not-yet-done training-plan slots can be swapped.")
    new = tp.swap_program_slot(slot)
    d = plan_slot_date(slot)
    return render(request, "workouts/partials/program_plan_cell.html", {
        "cell": {"slot": slot, "entry": None, "swappable": True, "difficulty": DifficultyRanker().rank(new),
                 "day_label": f"{tp.DAY_NAMES.get(slot.day, '')} · {d:%b} {d.day}" if d else ""},
        "swap_note": "" if new else "No other matching class right now.",
    })
