"""Program & Collection Tracker — list/detail/run views and the completion grid builder."""
import base64
from datetime import date

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .models import Program, ProgramRun, RunWeek, ProgramWorkout
from .programs import (
    backfill_program, create_plan, create_split, extract_ride_id_from_text,
    normalize_ride_id_input, progression_categories, recompute_run_dates,
    resolve_slot_ride_id, resolve_split_candidates, run_exercise_progression,
    run_running_progression,
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

    # Only the run's single most-recent pass overall (by sequence, across
    # every program_week) can still receive a "repeat" completion —
    # _resolve_recurring_week always targets the run's global latest pass,
    # not the latest pass of that specific canonical week — so once a later
    # pass exists anywhere in the run (e.g. week 2 has started), every
    # earlier pass is done. A never-filled optional slot there is just noise.
    latest_run_week_id = (run.run_weeks.order_by("-sequence")
                          .values_list("id", flat=True).first())

    rows = []
    total_workouts = 0
    total_effort = 0.0
    # Grouped by canonical week number, chronological (sequence) within each
    # group — so a repeated pass (e.g. a week resumed after a gap) sits right
    # under its earlier attempt instead of trailing at the end of the run.
    for rw in run.run_weeks.select_related("program_week").order_by("program_week__number", "sequence"):
        is_open_pass = run.end_date is None and rw.id == latest_run_week_id
        slots = list(rw.program_week.slots.all())
        cells = []
        for slot in slots:
            e = by_cell.get((rw.id, slot.id))
            if e:
                total_workouts += 1
                total_effort += (getattr(e.workout, "effort_points", 0) or 0)
            elif slot.optional and not is_open_pass:
                continue   # never-filled optional slot in a closed pass — hide it
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
    current = [p for p in programs if p.active_run]
    past = [p for p in programs if not p.active_run]
    return render(request, "workouts/program_list.html", {"current_programs": current, "past_programs": past})


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


def _plan_rows_from_post(post):
    """Rebuild the review-table row list from a submitted review/create form —
    used both to redisplay the table on a validation error and to build the
    final skeleton on success."""
    weeks = post.getlist("week")
    days = post.getlist("day")
    orders = post.getlist("order")
    titles = post.getlist("title")
    disciplines = post.getlist("discipline")
    durations = post.getlist("duration")
    ride_ids = post.getlist("ride_id")
    optional_idx = set(post.getlist("optional"))
    included_idx = set(post.getlist("include"))
    repeat_days_idx = set(post.getlist("repeat_days"))
    repeat_weeks_idx = set(post.getlist("repeat_weeks"))

    def _int(lst, i, default=None):
        try:
            v = lst[i].strip()
            return int(v) if v else default
        except (ValueError, IndexError, AttributeError):
            return default

    rows = []
    for i, title in enumerate(titles):
        rows.append({
            "i": i,
            "included": str(i) in included_idx,
            "week": _int(weeks, i, 1) or 1,
            "day": _int(days, i, None),
            "order": _int(orders, i, 0) or 0,
            "title": title.strip(),
            "discipline": disciplines[i].strip() if i < len(disciplines) else "",
            "duration_min": _int(durations, i, None),
            "optional": str(i) in optional_idx,
            "ride_id": ride_ids[i].strip() if i < len(ride_ids) else "",
            "repeat_all_days": str(i) in repeat_days_idx,
            "repeat_all_weeks": str(i) in repeat_weeks_idx,
            "matched_via": "", "candidates": [],
        })
    return rows


def program_new_plan(request):
    """
    Build a multi-week Plan from pasted text/links and/or a screenshot, instead
    of hand-writing HILIT_SCHEDULE-style Python. Single URL, three stages:
      GET / no stage        -> intake form (name, instructor, paste text/links,
                                optional screenshot)
      POST stage="review"   -> AI-extract the skeleton (workouts.ai.parse_plan_skeleton),
                                resolve a ride_id per slot where possible (a pasted
                                class link first, then a match in the user's own
                                synced history — see resolve_slot_ride_id for why
                                catalog search isn't in the cascade), render an
                                editable review table
      POST stage="create"   -> build the Program from whatever rows are still
                                checked, using the (possibly hand-corrected)
                                submitted field values
    """
    from .ai import parse_plan_skeleton

    stage = request.POST.get("stage") if request.method == "POST" else None

    if stage == "create":
        name = (request.POST.get("name") or "").strip()
        instructor = (request.POST.get("instructor") or "").strip()
        rows = _plan_rows_from_post(request.POST)

        errors = []
        if not name:
            errors.append("Plan name is required.")
        slug = slugify(name)
        if not errors and Program.objects.filter(slug=slug).exists():
            errors.append(f'A program named "{name}" already exists.')
        included = [r for r in rows if r["included"] and r["title"]]
        if not included:
            errors.append("Select at least one class to include.")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "workouts/program_new_plan.html", {
                "rows": rows, "name": name, "instructor": instructor,
            })

        rows_by_week = {}
        for r in included:
            rows_by_week.setdefault(r["week"], []).append({
                "day": r["day"], "order": r["order"], "title": r["title"],
                "discipline": r["discipline"], "duration_min": r["duration_min"],
                "optional": r["optional"], "ride_id": normalize_ride_id_input(r["ride_id"]),
                "repeat_all_days": r["repeat_all_days"], "repeat_all_weeks": r["repeat_all_weeks"],
            })
        weeks_data = [{"number": n, "slots": slots} for n, slots in sorted(rows_by_week.items())]
        program = create_plan(name, slug, instructor, weeks_data)
        total = sum(len(w["slots"]) for w in weeks_data)
        messages.success(
            request,
            f'"{name}" created with {total} class{"es" if total != 1 else ""} '
            f'across {len(weeks_data)} week{"s" if len(weeks_data) != 1 else ""}.')
        return redirect("program_detail", slug=program.slug)

    if stage == "review":
        raw_text = (request.POST.get("raw_text") or "").strip()
        name = (request.POST.get("name") or "").strip()
        instructor = (request.POST.get("instructor") or "").strip()

        image_b64 = None
        image_media_type = "image/jpeg"
        screenshot = request.FILES.get("screenshot")
        if screenshot:
            content_type = screenshot.content_type or "image/jpeg"
            if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                messages.error(request, "Unsupported image type. Please upload a JPEG, PNG, or WebP.")
                return render(request, "workouts/program_new_plan.html",
                              {"raw_text": raw_text, "name": name, "instructor": instructor})
            image_b64 = base64.b64encode(screenshot.read()).decode("utf-8")
            image_media_type = content_type

        if not raw_text and not image_b64:
            messages.error(request, "Paste the plan's schedule text (with links, if you have them) or upload a screenshot.")
            return render(request, "workouts/program_new_plan.html",
                          {"raw_text": raw_text, "name": name, "instructor": instructor})

        result = parse_plan_skeleton(raw_text, image_b64=image_b64, image_media_type=image_media_type)
        if not result.get("ok") or not result.get("items"):
            messages.error(
                request,
                f"Couldn't extract a schedule from that ({result.get('error', 'no items found')}). "
                f"Try adding more text context or a clearer screenshot.")
            return render(request, "workouts/program_new_plan.html",
                          {"raw_text": raw_text, "name": name, "instructor": instructor})

        instructor = instructor or result.get("instructor_guess", "")
        name = name or result.get("plan_name_guess", "")

        rows = []
        for item in result["items"]:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            resolved = resolve_slot_ride_id(
                title, instructor=instructor, source_url=item.get("source_url", ""))
            rows.append({
                "week": item.get("week") or 1,
                "day": item.get("day"),
                "order": item.get("order") or 0,
                "title": title,
                "discipline": item.get("discipline", ""),
                "duration_min": item.get("duration_min"),
                "optional": bool(item.get("optional")),
                "included": True,
                "repeat_all_days": False, "repeat_all_weeks": False,
                "ride_id": resolved["ride_id"] or "",
                "matched_via": resolved["matched_via"],
                "candidates": resolved["candidates"],
            })
        rows.sort(key=lambda r: (r["week"], r["day"] if r["day"] is not None else 99, r["order"]))
        for i, r in enumerate(rows):
            r["i"] = i

        return render(request, "workouts/program_new_plan.html", {
            "rows": rows, "name": name, "instructor": instructor,
            "note": result.get("note", ""),
        })

    return render(request, "workouts/program_new_plan.html", {})


@require_POST
def program_delete(request, slug):
    """Delete an entire Program — every week/slot/run/pass/completion it owns
    (cascades via FK on_delete=CASCADE). Does not touch the underlying workout
    history, same as deleting a single run."""
    program = get_object_or_404(Program, slug=slug)
    program.delete()
    return redirect("program_list")


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

    # Retrospective only loads for a completed run — _end_run() already
    # generates and caches one automatically when a run ends, so this is
    # normally an instant cache read; it just avoids spending a Sonnet call
    # (and showing a necessarily-partial analysis) on every view of a run
    # that's still in progress.
    retro_text = None
    if not run.is_current:
        from .ai import _get_or_generate_retrospective
        if request.GET.get("refresh_retro") == "1":
            _get_or_generate_retrospective(run, force=True)
            return redirect("program_run", pk=run.pk)
        retro_text = _get_or_generate_retrospective(run)

    return render(request, "workouts/program_run.html", {
        "program": run.program, "run": run,
        "rows": rows, "totals": totals,
        "other_runs": run.program.runs.exclude(pk=run.pk),
        "retro_text": retro_text,
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
    categories = progression_categories(run)
    category = request.GET.get("category") or None
    if category not in {c["key"] for c in categories}:
        category = None   # "All" (or an unrecognized value) -> no filter
    data = run_exercise_progression(run, metric=metric, category=category)
    if request.headers.get("HX-Request") or request.GET.get("format") == "json":
        return JsonResponse(data)
    return render(request, "workouts/program_progression.html", {
        "program": run.program, "run": run, "data": data, "metric": metric,
        "categories": categories, "selected_category": category,
    })


def program_running_progression(request, pk):
    run = get_object_or_404(ProgramRun.objects.select_related("program"), pk=pk)
    metric = request.GET.get("metric", "pace")
    if metric not in ("pace", "distance", "hr"):
        metric = "pace"
    data = run_running_progression(run, metric=metric)
    if request.headers.get("HX-Request") or request.GET.get("format") == "json":
        return JsonResponse(data)
    return render(request, "workouts/program_running_progression.html", {
        "program": run.program, "run": run, "data": data, "metric": metric,
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
