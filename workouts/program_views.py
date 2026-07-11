"""Program & Collection Tracker — list/detail/run views and the completion grid builder."""
from datetime import date

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .models import Program, ProgramRun, RunWeek, ProgramWorkout
from .programs import (
    backfill_program, create_split, progression_slots, recompute_run_dates,
    resolve_split_candidates, run_exercise_progression,
)


def _run_grid(run):
    """
    Build a grid: one row per RunWeek (sequence), cells keyed by ProgramSlot.
    Returns (rows, totals).
    """
    entries = (ProgramWorkout.objects
               .filter(run_week__run=run)
               .select_related("slot", "workout", "run_week", "run_week__program_week"))
    by_cell = {}
    for e in entries:
        by_cell[(e.run_week_id, e.slot_id)] = e

    rows = []
    total_workouts = 0
    total_effort = 0.0
    for rw in run.run_weeks.select_related("program_week").order_by("sequence"):
        slots = list(rw.program_week.slots.all())
        cells = []
        for slot in slots:
            e = by_cell.get((rw.id, slot.id))
            if e:
                total_workouts += 1
                total_effort += (getattr(e.workout, "effort_points", 0) or 0)
            cells.append({"slot": slot, "entry": e})
        # Completed classes in the order actually taken; still-empty slots trail at
        # the end (they have no date to sort by) in their defined slot order.
        cells.sort(key=lambda c: (c["entry"] is None, c["entry"] and c["entry"].workout.created_at))
        # entries in this run-week with no slot (matched week, not a specific class)
        loose = sorted(
            (e for e in entries if e.run_week_id == rw.id and e.slot_id is None),
            key=lambda e: e.workout.created_at,
        )
        total_workouts += len(loose)
        has_done = any(c["entry"] for c in cells) or bool(loose)
        rows.append({"run_week": rw, "cells": cells, "loose": loose, "has_done": has_done})

    totals = {"workouts": total_workouts, "effort_points": round(total_effort)}
    return rows, totals


def program_list(request):
    programs = Program.objects.prefetch_related("runs").all()
    return render(request, "workouts/program_list.html", {"programs": programs})


def program_new(request):
    """
    GET ?ids=<workout_id,...>: review the distinct classes among workouts picked on
    the History page, one slot candidate per ride-id. POST: create the split from
    whichever slots are still checked, and backfill history for those ride-ids.
    """
    if request.method == "POST":
        ids_param = request.POST.get("ids", "")
        name = (request.POST.get("name") or "").strip()
        checked_ride_ids = set(request.POST.getlist("slot_ride_id"))

        workout_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
        candidates = resolve_split_candidates(workout_ids)

        errors = []
        if not name:
            errors.append("Name is required.")
        if not checked_ride_ids:
            errors.append("Select at least one class.")
        slug = slugify(name)
        if not errors and Program.objects.filter(slug=slug).exists():
            errors.append(f'A program named "{name}" already exists.')
        claimed = [c for c in candidates if c["ride_id"] in checked_ride_ids and c["claimed_by"]]
        for c in claimed:
            errors.append(f'"{c["title"]}" is already used by {c["claimed_by"]} — deselect it to continue.')

        if not errors:
            selected = [c for c in candidates if c["ride_id"] in checked_ride_ids]
            program = create_split(name, slug, selected)
            return redirect("program_detail", slug=program.slug)
        return render(request, "workouts/program_new.html", {
            "ids_param": ids_param, "candidates": candidates, "errors": errors,
            "name": name, "checked_ride_ids": checked_ride_ids,
        })

    ids_param = request.GET.get("ids", "")
    workout_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    candidates = resolve_split_candidates(workout_ids) if workout_ids else None
    # pre-check everything except slots already claimed by another program
    checked_ride_ids = {c["ride_id"] for c in candidates if not c["claimed_by"]} if candidates else set()
    return render(request, "workouts/program_new.html", {
        "ids_param": ids_param, "candidates": candidates, "checked_ride_ids": checked_ride_ids,
    })


def program_detail(request, slug):
    program = get_object_or_404(Program, slug=slug)
    runs = program.runs.all()
    # If there's a current run, jump straight into it.
    if program.active_run:
        return redirect("program_run", pk=program.active_run.pk)
    return render(request, "workouts/program_detail.html",
                  {"program": program, "runs": runs})


def program_run(request, pk):
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    rows, totals = _run_grid(run)
    return render(request, "workouts/program_run.html", {
        "program": run.program, "run": run,
        "rows": rows, "totals": totals,
        "other_runs": run.program.runs.exclude(pk=run.pk),
    })


@require_POST
def program_backfill(request, slug):
    """
    Re-run the matcher for this program. Nothing re-associates a workout on its own
    after a pass is deleted or a slot is added later — this is the manual trigger for
    that, e.g. to pick up workouts a deleted pass orphaned into whatever run is
    currently active.
    """
    program = get_object_or_404(Program, slug=slug)
    made = backfill_program(program)
    for run in program.runs.all():
        recompute_run_dates(run)
    messages.success(request, f"Backfill complete — {made} workout{'s' if made != 1 else ''} associated.")
    return redirect("program_detail", slug=program.slug)


@require_POST
def run_week_rate(request, pk):
    """Save RPE + note for one pass via HTMX; swaps just that row's rating widget."""
    rw = get_object_or_404(RunWeek.objects.select_related("run", "program_week"), pk=pk)
    raw = (request.POST.get("rpe") or "").strip()
    if raw == "":
        rw.rpe = None
    else:
        try:
            v = int(raw)
        except ValueError:
            v = None
        rw.rpe = v if v and 1 <= v <= 10 else rw.rpe
    rw.note = (request.POST.get("note") or "").strip()
    rw.rated_at = timezone.now()
    rw.save(update_fields=["rpe", "note", "rated_at"])

    rows, _ = _run_grid(rw.run)
    row = next(r for r in rows if r["run_week"].pk == rw.pk)
    return render(request, "workouts/partials/run_week_rating.html", {"row": row})


@require_POST
def program_delete_week(request, pk):
    """Delete one pass (RunWeek) and its completions, then compact remaining sequences
    so passes stay numbered contiguously (e.g. deleting pass 13 of 14 renumbers 14 -> 13)."""
    week = get_object_or_404(RunWeek.objects.select_related("run", "program_week"), pk=pk)
    run, program_week = week.run, week.program_week
    week.delete()

    remaining = list(run.run_weeks.filter(program_week=program_week).order_by("sequence"))
    for i, rw in enumerate(remaining, start=1):
        if rw.sequence != i:
            rw.sequence = i
            rw.save(update_fields=["sequence"])

    recompute_run_dates(run)
    return redirect("program_run", pk=run.pk)


@require_POST
def program_delete_run(request, pk):
    """Delete an entire cycle — all its passes and completions (cascades via
    RunWeek -> ProgramWorkout). Does not touch the underlying workout history."""
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    slug = run.program.slug
    run.delete()
    return redirect("program_detail", slug=slug)


def _generate_retrospective_safe(run):
    """Best-effort retrospective generation — a Sonnet/aggregation failure must
    never block ending a run; the page can always regenerate on demand."""
    try:
        from .ai import _get_or_generate_retrospective
        _get_or_generate_retrospective(run)
    except Exception:
        pass


def _end_run(run):
    """
    Mark a run as ended. end_date is backdated to the run's last completion
    rather than today, so a run that quietly stopped months ago doesn't show a
    misleading just-now end date — used by both Complete and Start New Cycle so
    the two ending paths behave identically. Once end_date is set,
    Program.active_run no longer returns this run, so any future matching
    workout starts a fresh run instead of attaching here.
    """
    last_date = (ProgramWorkout.objects
                 .filter(run_week__run=run)
                 .order_by("-workout__created_at")
                 .values_list("workout__created_at", flat=True)
                 .first())
    run.end_date = last_date.date() if last_date else date.today()
    run.save(update_fields=["end_date"])
    recompute_run_dates(run)
    _generate_retrospective_safe(run)


@require_POST
def program_complete_run(request, pk):
    """Mark a run as ended, without starting a new one (unlike 'Start new cycle', which does both)."""
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    _end_run(run)
    return redirect("program_run", pk=run.pk)


def program_start_cycle(request, slug):
    """POST: end the current run (if any) and open a fresh one. Explicit 'new cycle'."""
    program = get_object_or_404(Program, slug=slug)
    if request.method == "POST":
        cur = program.active_run
        if cur:
            _end_run(cur)
        # No label — ProgramRun.display_name derives the name from start_date/end_date
        # directly, so it can't drift out of sync the way a static "Cycle N" string would.
        run = ProgramRun.objects.create(program=program, start_date=date.today())
        # seed empty weeks for a plan so the grid shows
        if program.kind == "plan":
            for pw in program.weeks.all():
                RunWeek.objects.get_or_create(run=run, program_week=pw, sequence=pw.number)
        return redirect("program_run", pk=run.pk)
    return redirect("program_detail", slug=slug)


def program_progression(request, pk):
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    metric = request.GET.get("metric", "top_weight")
    slots = progression_slots(run)
    slot_id = request.GET.get("slot")
    selected_slot = None
    if slots:
        if slot_id:
            selected_slot = next((s for s in slots if str(s.pk) == slot_id), slots[0])
        else:
            selected_slot = slots[0]
    data = run_exercise_progression(run, metric=metric, slot_id=selected_slot.pk if selected_slot else None)
    if request.headers.get("HX-Request") or request.GET.get("format") == "json":
        return JsonResponse(data)
    return render(request, "workouts/program_progression.html", {
        "program": run.program, "run": run, "data": data, "metric": metric,
        "slots": slots, "selected_slot": selected_slot,
    })


def program_retrospective(request, pk):
    """Sonnet retrospective for a run — cached, regenerable via ?refresh=1. Works
    mid-cycle too (a partial read), not just after the run is marked ended."""
    from .ai import _get_or_generate_retrospective
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    force = request.GET.get("refresh") == "1"
    text = _get_or_generate_retrospective(run, force=force)
    return render(request, "workouts/program_retrospective.html",
                  {"program": run.program, "run": run, "text": text})
