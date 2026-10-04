"""Program & Collection Tracker — list/detail/run views and the completion grid builder."""
import base64
import re
from datetime import date

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .access import has_feature
from .training_plan_views import slot_swappable
from .models import Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek
from .programs import (
    ANY_CLASS_PRESETS, backfill_program, create_plan, create_split, duplicate_program,
    extract_ride_id_from_text, normalize_ride_id_input, preset_for, progression_categories,
    recompute_run_dates, resolve_slot_match, resolve_slot_ride_id, resolve_split_candidates,
    run_exercise_progression, run_running_progression, start_run, unique_slug,
)


def _run_grid(run):
    """
    Build a grid: one row per RunWeek (sequence), cells keyed by ProgramSlot.
    Returns (rows, totals).
    """
    entries = (ProgramWorkout.objects
               .filter(run_week__run=run)
               .select_related("slot", "workout", "run_week", "run_week__program_week")
               .prefetch_related("recoveries__workout"))
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

    from .catalog import DifficultyRanker
    ranker = DifficultyRanker()
    planned = ranker.for_rides(ProgramSlot.objects.filter(week__program=run.program)
                               .exclude(peloton_ride_id="").values_list("peloton_ride_id", flat=True))
    rows = []
    total_workouts = 0
    total_effort = 0.0
    recovery_sessions = 0
    recovery_seconds = 0
    completions_with_recovery = 0
    # Grouped by canonical week number, chronological (sequence) within each
    # group — so a repeated pass (e.g. a week resumed after a gap) sits right
    # under its earlier attempt instead of trailing at the end of the run.
    for rw in run.run_weeks.select_related("program_week").order_by("program_week__number", "sequence"):
        is_open_pass = run.end_date is None and rw.id == latest_run_week_id
        slots = list(rw.program_week.slots.all())
        cells = []
        for slot in slots:
            e = by_cell.get((rw.id, slot.id))
            recoveries = []
            if e:
                total_workouts += 1
                total_effort += (getattr(e.workout, "effort_points", 0) or 0)
                recoveries = list(e.recoveries.all())
                if recoveries:
                    completions_with_recovery += 1
                    recovery_sessions += len(recoveries)
                    recovery_seconds += sum(r.workout.duration_seconds or 0 for r in recoveries)
            elif slot.optional and not is_open_pass:
                continue   # never-filled optional slot in a closed pass — hide it
            cells.append({"slot": slot, "entry": e, "recoveries": recoveries,
                          "swappable": run.end_date is None and slot_swappable(slot, e),
                          "difficulty": None if e else ranker.rank(planned.get(slot.peloton_ride_id))})
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

    totals = {
        "workouts": total_workouts, "effort_points": round(total_effort),
        "recovery_sessions": recovery_sessions,
        "recovery_minutes": round(recovery_seconds / 60),
        "completions_with_recovery": completions_with_recovery,
    }
    return rows, totals


def program_list(request):
    programs = Program.objects.for_user(request.user).prefetch_related("runs").all()
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
        candidates = resolve_split_candidates(request.user, workout_ids)

        errors = []
        if not name:
            errors.append("Name is required.")
        if not checked_ride_ids:
            errors.append("Select at least one class.")
        slug = slugify(name)
        if not errors and Program.objects.for_user(request.user).filter(slug=slug).exists():
            errors.append(f'A program named "{name}" already exists.')
        claimed = [c for c in candidates if c["ride_id"] in checked_ride_ids and c["claimed_by"]]
        for c in claimed:
            errors.append(f'"{c["title"]}" is already used by {c["claimed_by"]} — deselect it to continue.')

        if not errors:
            selected = [c for c in candidates if c["ride_id"] in checked_ride_ids]
            program = create_split(request.user, name, slug, selected)
            return redirect("program_detail", slug=program.slug)
        return render(request, "workouts/program_new.html", {
            "ids_param": ids_param, "candidates": candidates, "errors": errors,
            "name": name, "checked_ride_ids": checked_ride_ids,
        })

    ids_param = request.GET.get("ids", "")
    workout_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    candidates = resolve_split_candidates(request.user, workout_ids) if workout_ids else None
    # pre-check everything except slots already claimed by another program
    checked_ride_ids = {c["ride_id"] for c in candidates if not c["claimed_by"]} if candidates else set()
    return render(request, "workouts/program_new.html", {
        "ids_param": ids_param, "candidates": candidates, "checked_ride_ids": checked_ride_ids,
    })


def _preset_choices():
    return [(k, v[0]) for k, v in ANY_CLASS_PRESETS.items()]


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
    matches = post.getlist("match")
    presets = post.getlist("preset")

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
            "match": matches[i] if i < len(matches) and matches[i] in ("ride", "any") else "ride",
            "preset": presets[i] if i < len(presets) else "",
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
        if not errors and Program.objects.for_user(request.user).filter(slug=slug).exists():
            errors.append(f'A program named "{name}" already exists.')
        included = [r for r in rows if r["included"] and r["title"]]
        if not included:
            errors.append("Select at least one class to include.")

        if errors:
            for e in errors:
                messages.error(request, e)
            return render(request, "workouts/program_new_plan.html", {
                "rows": rows, "name": name, "instructor": instructor, "presets": _preset_choices(),
            })

        rows_by_week = {}
        for r in included:
            # strict=False: an unusable ride link / typeless any-class row degrades to an
            # empty match here and can be fixed on the Edit page, rather than blocking creation.
            m = resolve_slot_match(r["match"], r["ride_id"], r["preset"], r["discipline"], strict=False)
            rows_by_week.setdefault(r["week"], []).append({
                "day": r["day"], "order": r["order"], "title": r["title"],
                "discipline": r["discipline"], "duration_min": r["duration_min"],
                "optional": r["optional"], "ride_id": m["peloton_ride_id"],
                "match_discipline": m["match_discipline"], "match_title_keyword": m["match_title_keyword"],
                "repeat_all_days": r["repeat_all_days"], "repeat_all_weeks": r["repeat_all_weeks"],
            })
        weeks_data = [{"number": n, "slots": slots} for n, slots in sorted(rows_by_week.items())]
        kind = request.POST.get("kind") if request.POST.get("kind") in ("plan", "split") else None
        program = create_plan(request.user, name, slug, instructor, weeks_data, kind=kind)
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

        from .ai import AI_UNAVAILABLE, ai_unavailable_reason
        try:
            result = parse_plan_skeleton(request.user, raw_text, image_b64=image_b64, image_media_type=image_media_type)
        except AI_UNAVAILABLE as e:
            messages.error(request, ai_unavailable_reason(e))
            return render(request, "workouts/program_new_plan.html",
                          {"raw_text": raw_text, "name": name, "instructor": instructor})
        if not result.get("ok") or not result.get("items"):
            messages.error(
                request,
                f"Couldn't extract a schedule from that ({result.get('error', 'no items found')}). "
                f"Try adding more text context or a clearer screenshot.")
            return render(request, "workouts/program_new_plan.html",
                          {"raw_text": raw_text, "name": name, "instructor": instructor})

        instructor = instructor or (result.get("instructor_guess") or "")
        name = name or (result.get("plan_name_guess") or "")

        rows = []
        for item in result["items"]:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            is_any = bool(item.get("any_class"))
            class_type = (item.get("class_type") or "").strip().lower()
            # Open-ended days ("Pilates (any class)") have no single ride to resolve.
            resolved = ({"ride_id": "", "matched_via": "", "candidates": []} if is_any else
                        resolve_slot_ride_id(request.user, title, instructor=instructor, source_url=item.get("source_url", "")))
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
                "match": "any" if is_any else "ride",
                "preset": (class_type if class_type in ANY_CLASS_PRESETS else "custom") if is_any else "",
                "matched_via": resolved["matched_via"],
                "candidates": resolved["candidates"],
            })
        rows.sort(key=lambda r: (r["week"], r["day"] if r["day"] is not None else 99, r["order"]))
        for i, r in enumerate(rows):
            r["i"] = i

        return render(request, "workouts/program_new_plan.html", {
            "rows": rows, "name": name, "instructor": instructor,
            "note": result.get("note", ""), "presets": _preset_choices(),
        })

    return render(request, "workouts/program_new_plan.html", {})


# ---------- editing, duplicating, and blank programs ----------

MAX_DAY = 31


def _opt_int(raw, lo=0, hi=None, default=None):
    """int from a form string; `default` if blank/garbage/out of range."""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if v < lo or (hi is not None and v > hi):
        return default
    return v


def _slot_row(pfx, *, slot=None, week_id=None, is_new=False):
    """Everything the slot-row partial needs, from a saved slot or blank."""
    if slot is None:
        return {"pfx": pfx, "is_new": True, "title": "", "day": "", "order": 0, "duration_min": "",
                "optional": False, "match": "ride", "ride_id": "", "preset": "", "discipline": "",
                "keyword": "", "completions": 0}
    match = "any" if slot.match_discipline else ("ride" if slot.peloton_ride_id else "title")
    return {
        "pfx": pfx, "is_new": is_new, "slot_id": slot.pk, "title": slot.title,
        "day": slot.day if slot.day is not None else "", "order": slot.order,
        "duration_min": slot.duration_min or "", "optional": slot.optional, "match": match,
        "ride_id": slot.peloton_ride_id, "preset": preset_for(slot.match_discipline, slot.match_title_keyword),
        "discipline": slot.match_discipline, "keyword": slot.match_title_keyword,
        "completions": slot.programworkout_set.count(),
    }


def _slot_fields_from_post(post, pfx):
    """Validated ProgramSlot field values for one row of the edit form; ValueError with
    a user-facing message if the row can't be saved as entered."""
    title = (post.get(pfx + "title") or "").strip()
    if not title:
        raise ValueError("a slot needs a title")
    match = post.get(pfx + "match", "ride")
    m = resolve_slot_match(
        match, post.get(pfx + "ride_id", ""), post.get(pfx + "preset", ""),
        post.get(pfx + "discipline", ""), post.get(pfx + "keyword", ""))
    fields = {
        "title": title[:200],
        "day": _opt_int(post.get(pfx + "day"), lo=1, hi=MAX_DAY),
        "order": _opt_int(post.get(pfx + "order"), lo=0, hi=99, default=0),
        "duration_min": _opt_int(post.get(pfx + "duration"), lo=1, hi=600),
        "optional": (pfx + "optional") in post,
        **m,
    }
    if match == "any":
        fields["discipline"] = m["match_discipline"]
    return fields


def _apply_program_edit(program, post):
    """Apply the edit form. Returns (summary_parts, warnings). Valid changes are applied even
    if another row is skipped — a skipped row is reported and left as it was."""
    parts, warnings = [], []

    name = (post.get("name") or "").strip()
    if name:
        program.name = name[:200]
    program.instructor = (post.get("instructor") or "").strip()[:120]
    program.description = (post.get("description") or "").strip()
    program.track_recovery = "track_recovery" in post
    program.recovery_walks = "recovery_walks" in post
    program.recovery_stretches = "recovery_stretches" in post
    program.recovery_window_min = _opt_int(post.get("recovery_window_min"), lo=0, hi=120,
                                           default=program.recovery_window_min)
    program.recovery_max_min = _opt_int(post.get("recovery_max_min"), lo=1, hi=240,
                                        default=program.recovery_max_min)
    program.save()

    weeks = list(program.weeks.prefetch_related("slots"))
    updated = removed = added = 0
    for wk in weeks:
        label = (post.get(f"week_{wk.pk}_label") or "").strip()[:120]
        if label != wk.label:
            wk.label = label
            wk.save(update_fields=["label"])
        for slot in wk.slots.all():
            pfx = f"slot_{slot.pk}_"
            if pfx + "present" not in post:
                continue
            if pfx + "delete" in post:
                slot.delete()
                removed += 1
                continue
            try:
                fields = _slot_fields_from_post(post, pfx)
            except ValueError as e:
                warnings.append(f'Week {wk.number} "{slot.title}": {e} — left unchanged.')
                continue
            if fields["peloton_ride_id"] != slot.peloton_ride_id or fields["match_discipline"]:
                slot.alt_ride_ids = []   # alternates belong to the old ride id
            for k, v in fields.items():
                setattr(slot, k, v)
            slot.save()
            updated += 1

    # blank rows the user filled in: new_<weekid>_<k>_title
    week_by_id = {w.pk: w for w in weeks}
    new_keys = sorted(
        (int(m.group(1)), int(m.group(2))) for k in post
        if (m := re.fullmatch(r"new_(\d+)_(\d+)_title", k)))
    for wid, k in new_keys:
        pfx = f"new_{wid}_{k}_"
        if not (post.get(pfx + "title") or "").strip() or wid not in week_by_id:
            continue
        try:
            fields = _slot_fields_from_post(post, pfx)
        except ValueError as e:
            warnings.append(f'New slot "{post.get(pfx + "title")}": {e} — not added.')
            continue
        ProgramSlot.objects.create(week=week_by_id[wid], **fields)
        added += 1

    del_week = _opt_int(post.get("delete_week"), lo=1)
    if del_week:
        wk = week_by_id.get(del_week)
        if wk is None:
            pass
        elif RunWeek.objects.filter(program_week=wk).exists():
            warnings.append(f"Week {wk.number} has completions in a cycle — delete those passes first.")
        elif len(weeks) == 1:
            warnings.append("A program needs at least one week.")
        else:
            wk.delete()
            parts.append(f"removed week {wk.number}")

    if post.get("action") == "add_week":
        next_no = max([w.number for w in weeks] or [0]) + 1
        ProgramWeek.objects.create(program=program, number=next_no)
        parts.append(f"added week {next_no}")

    for n, word in ((updated, "updated"), (added, "added"), (removed, "removed")):
        if n:
            parts.insert(0, f"{n} slot{'s' if n != 1 else ''} {word}")
    return parts, warnings


def program_edit(request, slug):
    """Edit a program's definition in the app: name, recovery-tracking rules, and every slot
    (day/order/optional, and how a workout matches it — a specific class, any class of a
    type, or by title). Replaces the one-off setup commands and shell edits."""
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    if request.method == "POST":
        parts, warnings = _apply_program_edit(program, request.POST)
        # New/changed slots may now match history you already have (and recovery rules may
        # have changed) — re-run matching; idempotent, only adds.
        made = backfill_program(program)
        summary = "Saved" + (": " + ", ".join(parts) if parts else "")
        if made:
            summary += f". Matched {made} earlier workout{'s' if made != 1 else ''}"
        messages.success(request, summary + ".")
        for w in warnings:
            messages.warning(request, w)
        return redirect("program_edit", slug=program.slug)

    weeks = []
    for wk in program.weeks.order_by("number"):
        rows = [_slot_row(f"slot_{s.pk}_", slot=s) for s in wk.slots.order_by("day", "order", "id")]
        blanks = [_slot_row(f"new_{wk.pk}_{k}_") for k in range(2)]
        weeks.append({"week": wk, "rows": rows, "blanks": blanks,
                      "has_passes": RunWeek.objects.filter(program_week=wk).exists()})
    return render(request, "workouts/program_edit.html", {
        "program": program, "weeks": weeks,
        "presets": _preset_choices(),
        "blank_row": _slot_row("__PFX__"),
    })


@require_POST
def program_duplicate(request, slug):
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    copy = duplicate_program(
        program, name=request.POST.get("name"), keep_ride_ids="keep_ride_ids" in request.POST)
    messages.success(
        request,
        f'Created "{copy.name}" from "{program.name}".'
        + ("" if "keep_ride_ids" in request.POST else " Class links were cleared so it doesn't compete with the original — set each slot's class below."))
    return redirect("program_edit", slug=copy.slug)


@require_POST
def program_new_blank(request):
    """Start an empty program (one empty week) and go straight to the editor."""
    name = (request.POST.get("name") or "").strip()
    kind = request.POST.get("kind") if request.POST.get("kind") in ("plan", "split") else "split"
    if not name:
        messages.error(request, "Give the program a name.")
        return redirect("program_new_plan")
    program = Program.objects.create(user=request.user, name=name[:200], slug=unique_slug(request.user, name),
                                     kind=kind, match_strategy="ride_ids")
    ProgramWeek.objects.create(program=program, number=1)
    messages.success(request, f'"{program.name}" created — add its classes below.')
    return redirect("program_edit", slug=program.slug)


@require_POST
def program_delete(request, slug):
    """Delete an entire Program — every week/slot/run/pass/completion it owns
    (cascades via FK on_delete=CASCADE). Does not touch the underlying workout
    history, same as deleting a single run."""
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    program.delete()
    return redirect("program_list")


def program_detail(request, slug):
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    runs = program.runs.all()
    # If there's a current run, jump straight into it.
    if program.active_run:
        return redirect("program_run", pk=program.active_run.pk)
    return render(request, "workouts/program_detail.html",
                  {"program": program, "runs": runs})


def program_run(request, pk):
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
    rows, totals = _run_grid(run)

    # Retrospective only loads for a completed run — _end_run() already
    # generates and caches one automatically when a run ends, so this is
    # normally an instant cache read; it just avoids spending a Sonnet call
    # (and showing a necessarily-partial analysis) on every view of a run
    # that's still in progress.
    retro_text = None
    if not run.is_current and has_feature(request.user, "ai_program_tools"):
        from .ai import AI_UNAVAILABLE, _get_or_generate_retrospective
        try:
            if request.GET.get("refresh_retro") == "1":
                _get_or_generate_retrospective(request.user, run, force=True)
                return redirect("program_run", pk=run.pk)
            retro_text = _get_or_generate_retrospective(request.user, run)
        except AI_UNAVAILABLE:
            retro_text = run.retrospective or None

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
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    made = backfill_program(program)
    for run in program.runs.all():
        recompute_run_dates(run)
    messages.success(request, f"Backfill complete — {made} workout{'s' if made != 1 else ''} associated.")
    return redirect("program_detail", slug=program.slug)


@require_POST
def run_week_rate(request, pk):
    """Save RPE + note for one pass via HTMX; swaps just that row's rating widget."""
    rw = get_object_or_404(RunWeek.objects.filter(run__program__user=request.user).select_related("run", "program_week"), pk=pk)
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
    week = get_object_or_404(RunWeek.objects.filter(run__program__user=request.user).select_related("run", "program_week"), pk=pk)
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
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
    slug = run.program.slug
    run.delete()
    return redirect("program_detail", slug=slug)


def _generate_retrospective_safe(run):
    """Best-effort retrospective generation — a Sonnet/aggregation failure must
    never block ending a run; the page can always regenerate on demand."""
    try:
        from .ai import _get_or_generate_retrospective
        _get_or_generate_retrospective(run.program.user, run)
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
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
    _end_run(run)
    return redirect("program_run", pk=run.pk)


def program_start_cycle(request, slug):
    """POST: end the current run (if any) and open a fresh one. Explicit 'new cycle'."""
    program = get_object_or_404(Program.objects.for_user(request.user), slug=slug)
    if request.method == "POST":
        cur = program.active_run
        if cur:
            _end_run(cur)
        # No label — ProgramRun.display_name derives the name from start_date/end_date
        # directly, so it can't drift out of sync the way a static "Cycle N" string would.
        run = start_run(program, date.today())
        return redirect("program_run", pk=run.pk)
    return redirect("program_detail", slug=slug)


def program_progression(request, pk):
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
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
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
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
    from .ai import AI_UNAVAILABLE, _get_or_generate_retrospective, ai_unavailable_reason
    run = get_object_or_404(ProgramRun.objects.filter(program__user=request.user).select_related("program"), pk=pk)
    force = request.GET.get("refresh") == "1"
    try:
        text = _get_or_generate_retrospective(request.user, run, force=force)
    except AI_UNAVAILABLE as e:
        text = run.retrospective or ai_unavailable_reason(e)
    return render(request, "workouts/program_retrospective.html",
                  {"program": run.program, "run": run, "text": text})
