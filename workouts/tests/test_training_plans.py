"""AI training plans: inputs, level assessment, context isolation, spec
validation, class picking, generation, creation and swaps. Anthropic is never
called — llm.call_json is patched."""
import unittest
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.db import connection
from django.urls import reverse
from django.utils import timezone

from workouts import llm
from workouts import training_plans as tp
from workouts.access import access_for
from workouts.models import (
    CachedWorkout, PelotonClass, PelotonClassType, PlanDraft, Program, ProgramRun, ProgramSlot, ProgramWeek,
    ProgramWorkout,
)
from workouts.programs import _in_ai_plan_window, identify_membership
from workouts.tests.helpers import TwoUserTestCase

TODAY = date(2026, 10, 4)          # a Sunday
WED_START = date(2026, 10, 7)
SAT_RACE = date(2026, 11, 21)      # 7 Mon–Sun weeks from WED_START, race week included
NOW = timezone.now()

TYPES = {
    "t_end": ("Endurance", "running"), "t_int": ("Intervals", "running"),
    "t_beg": ("Beginner Running", "running"), "t_wr": ("Walk + Run", "running"),
    "t_str": ("Full Body Stretch", "stretching"), "t_yoga": ("Slow Flow", "yoga"),
    "t_pil": ("Pilates", "strength"), "t_lift": ("Upper Body Strength", "strength"),
}


def make_class(rid, tid, minutes=30, diff=7.0, aired_days_ago=10, rating=0.98, n=100, dcount=50,
               instructor="Becs Gentry", outdoor=False):
    name, disc = TYPES[tid]
    cats = ",strength,pilates," if tid == "t_pil" else f",{disc},"
    return PelotonClass.objects.create(
        ride_id=rid, title=f"{minutes} min {name} {rid}", discipline=disc, categories=cats, class_type_id=tid,
        class_type_ids=[tid], instructor_name=instructor, duration_seconds=minutes * 60, difficulty_estimate=diff,
        difficulty_rating_count=dcount, overall_rating_avg=rating, overall_rating_count=n,
        original_air_time=NOW - timedelta(days=aired_days_ago), is_outdoor=outdoor, last_seen_at=NOW)


def at(d, hour=17):
    return datetime.combine(d, time(hour), tzinfo=dt_tz.utc)


class PlanTestCase(TwoUserTestCase):
    def setUp(self):
        super().setUp()
        for tid, (name, disc) in TYPES.items():
            PelotonClassType.objects.create(id=tid, name=name, display_name=name, discipline=disc)
        n = 0
        for tid in TYPES:
            for minutes in (20, 30, 45):
                for i in range(8):
                    n += 1
                    make_class(f"{tid}-{minutes}-{i}", tid, minutes, diff=5.0 + i * 0.5, aired_days_ago=10 + i * 7 + n % 3,
                               instructor=["Becs Gentry", "Matt Wilpers", "Jess Sims"][i % 3])

    def post(self, **over):
        base = {"goal": "10k", "race_date": SAT_RACE.isoformat(), "start_date": WED_START.isoformat(),
                "days": ["1", "3", "5", "6"], "long_day": "6", "max_weekday_min": "45", "max_weekend_min": "75",
                "setting": "tread", "mode": "standalone", "strength_per_week": "2", "level": "intermediate",
                "target_time": "55:00", "notes": ""}
        base.update(over)
        return base

    def inputs(self, user=None, **over):
        inputs, errors = tp.clean_inputs(user or self.a, self.post(**over), today=TODAY)
        self.assertEqual(errors, [])
        return inputs

    def run_workout(self, user, d, minutes=30, title="30 min Endurance Run", ride_id="", wid=None, disc="running"):
        return CachedWorkout.objects.create(
            user=user, workout_id=wid or f"{user.username}-{d}-{minutes}-{title[:8]}", ride_id=ride_id, title=title,
            discipline=disc, source="peloton", created_at=at(d), duration_seconds=minutes * 60)


class InputTests(PlanTestCase):
    def test_week_count_from_wednesday_start_to_saturday_race(self):
        inputs = self.inputs()
        self.assertEqual((inputs["weeks"], inputs["race_week"], inputs["race_weekday"]), (7, 7, 6))

    def test_race_less_than_three_weeks_out_is_an_error(self):
        _, errors = tp.clean_inputs(self.a, self.post(race_date=(WED_START + timedelta(days=14)).isoformat()),
                                    today=TODAY)
        self.assertTrue(any("3 weeks" in e for e in errors))

    def test_needs_two_days(self):
        _, errors = tp.clean_inputs(self.a, self.post(days=["1"]), today=TODAY)
        self.assertTrue(any("2 training days" in e for e in errors))

    def test_companion_of_another_user_is_invalid(self):
        other = Program.objects.create(user=self.b, name="B split", slug="b", kind="split")
        ProgramRun.objects.create(program=other, start_date=TODAY)
        _, errors = tp.clean_inputs(self.a, self.post(mode="alongside", companion_program_id=str(other.pk)),
                                    today=TODAY)
        self.assertTrue(any("active cycle" in e for e in errors))

    def test_alongside_needs_an_active_run(self):
        mine = Program.objects.create(user=self.a, name="A split", slug="a", kind="split")
        _, errors = tp.clean_inputs(self.a, self.post(mode="alongside", companion_program_id=str(mine.pk)),
                                    today=TODAY)
        self.assertTrue(any("active cycle" in e for e in errors))
        ProgramRun.objects.create(program=mine, start_date=TODAY)
        _, errors = tp.clean_inputs(self.a, self.post(mode="alongside", companion_program_id=str(mine.pk)),
                                    today=TODAY)
        self.assertEqual(errors, [])

    def test_target_time_parsing(self):
        self.assertEqual(self.inputs(target_time="1:55:00")["target_time"], 6900)
        _, errors = tp.clean_inputs(self.a, self.post(target_time="abc"), today=TODAY)
        self.assertTrue(errors)


class LevelTests(PlanTestCase):
    def test_advanced(self):
        for back in range(1, 57, 2):                       # ~3.5 runs/week for 8 weeks
            self.run_workout(self.a, TODAY - timedelta(days=back), minutes=50 if back < 8 else 30)
        for m in range(3, 17):                             # 14 regular months before that
            for k in range(4):
                self.run_workout(self.a, TODAY - timedelta(days=30 * m + k), wid=f"h{m}-{k}")
        self.assertEqual(tp.assess_running_level(self.a, TODAY)["level"], "advanced")

    def test_intermediate(self):
        for wk in range(8):
            for k in (1, 4):
                self.run_workout(self.a, TODAY - timedelta(days=7 * wk + k), minutes=30)
        self.assertEqual(tp.assess_running_level(self.a, TODAY)["level"], "intermediate")

    def test_returning(self):
        self.run_workout(self.a, TODAY - timedelta(days=20))
        for m in range(10, 20):
            for k in range(4):
                self.run_workout(self.a, TODAY - timedelta(days=30 * m + k), wid=f"old{m}-{k}")
        result = tp.assess_running_level(self.a, TODAY)
        self.assertEqual(result["level"], "returning")
        self.assertTrue(any("of the 22 months" in e for e in result["evidence"]))

    def test_beginner(self):
        self.assertEqual(tp.assess_running_level(self.a, TODAY)["level"], "beginner")

    def test_walk_run_minutes_not_continuous(self):
        self.run_workout(self.a, TODAY - timedelta(days=3), minutes=45, title="45 min Walk + Run")
        m = tp.assess_running_level(self.a, TODAY)["measures"]
        self.assertEqual(m["longest_continuous_8w"], 0)
        self.assertGreater(m["minutes_per_week_4w"], 0)

    def test_runway_short_for_beginner_10k_in_7_weeks(self):
        self.assertTrue(tp.runway_check("10k", 7, "beginner")["short"])
        self.assertFalse(tp.runway_check("10k", 7, "intermediate")["short"])


class ContextTests(PlanTestCase):
    def test_context_uses_only_the_requesting_users_workouts(self):
        self.run_workout(self.a, TODAY - timedelta(days=2), title="ALICE RUN")
        self.run_workout(self.b, TODAY - timedelta(days=2), title="BOB-ONLY RUN")
        text = tp.build_fitness_context(self.a, self.inputs(), today=TODAY)
        self.assertIn("ALICE RUN", text)
        self.assertNotIn("BOB-ONLY", text)

    def test_thin_history_line_and_level(self):
        text = tp.build_fitness_context(self.a, self.inputs(), today=TODAY)
        self.assertIn("Running history is thin", text)
        self.assertIn("STARTING LEVEL", text)

    def test_companion_schedule_flags_lower_body(self):
        comp = Program.objects.create(user=self.a, name="Robin's Split", slug="rs", kind="split")
        wk = ProgramWeek.objects.create(program=comp, number=1)
        ProgramSlot.objects.create(week=wk, day=2, title="45 min Lower Body: Glutes", discipline="strength",
                                   duration_min=45)
        ProgramSlot.objects.create(week=wk, day=4, title="Pilates", discipline="strength", duration_min=30,
                                   match_discipline="strength", match_title_keyword="pilates")
        ProgramRun.objects.create(program=comp, start_date=TODAY)
        inputs = self.inputs(mode="alongside", companion_program_id=str(comp.pk))
        text = tp.build_fitness_context(self.a, inputs, today=TODAY)
        self.assertIn("[lower body]", text)
        self.assertIn("Pilates (any class)", text)
        self.assertNotIn("[upper body]", text)     # "Pilates" doesn't read as "lat"

    def test_menu_marks_early_gate_for_returning(self):
        text, allowed = tp.catalog_menu(self.inputs(level="returning"))
        self.assertIn("Intervals (t_int)", text)
        line = next(l for l in text.splitlines() if "(t_int)" in l)
        self.assertIn("[from week 4]", line)
        self.assertIn("[early OK", next(l for l in text.splitlines() if "(t_wr)" in l))
        self.assertIn(("running", "t_end"), allowed["types"])


def spec_slot(day, tid="t_end", minutes=30, intensity="easy", disc="running", **kw):
    name = TYPES[tid][0]
    return {"day": day, "order": 0, "discipline": disc, "class_type": name, "class_type_id": tid,
            "duration_min": minutes, "intensity": intensity, "setting": "tread", "purpose": "easy minutes", **kw}


def full_spec(weeks=7, extra=None):
    out = {"plan_name": "10K in 7 Weeks", "summary": "s", "assumptions": [], "weeks": []}
    for n in range(1, weeks + 1):
        slots = [spec_slot(3), spec_slot(6, minutes=45)]
        out["weeks"].append({"number": n, "phase": "build", "focus": "f", "slots": slots + (extra or {}).get(n, [])})
    return out


class ValidateTests(PlanTestCase):
    def validate(self, spec, **over):
        inputs = self.inputs(**over)
        _, allowed = tp.catalog_menu(inputs)
        return tp.validate_spec(spec, inputs, allowed)

    def test_off_day_slot_dropped_with_warning(self):
        clean, warnings = self.validate(full_spec(extra={2: [spec_slot(2)]}))
        self.assertEqual(len(clean["weeks"][1]["slots"]), 2)
        self.assertTrue(any("didn't make available" in w for w in warnings))

    def test_unknown_id_resolved_by_name(self):
        clean, _ = self.validate(full_spec(extra={2: [spec_slot(1, class_type_id="bogus")]}))
        self.assertEqual(clean["weeks"][1]["slots"][0]["class_type_id"], "t_end")

    def test_bad_duration_snapped(self):
        clean, warnings = self.validate(full_spec(extra={2: [spec_slot(1, minutes=33)]}))
        self.assertEqual(clean["weeks"][1]["slots"][0]["duration_min"], 30)
        self.assertTrue(any("33 min → 30 min" in w for w in warnings))

    def test_weekday_max_caps_duration(self):
        clean, _ = self.validate(full_spec(extra={2: [spec_slot(1, minutes=60)]}))
        self.assertEqual(clean["weeks"][1]["slots"][0]["duration_min"], 45)

    def test_alongside_strength_slot_dropped(self):
        comp = Program.objects.create(user=self.a, name="Split", slug="s", kind="split")
        ProgramRun.objects.create(program=comp, start_date=TODAY)
        clean, warnings = self.validate(
            full_spec(extra={2: [spec_slot(1, tid="t_lift", disc="strength")]}),
            mode="alongside", companion_program_id=str(comp.pk))
        self.assertFalse(any(s["discipline"] == "strength" for s in clean["weeks"][1]["slots"]))
        self.assertTrue(any("alongside" in w for w in warnings))

    def test_slot_on_race_day_dropped(self):
        clean, warnings = self.validate(full_spec())   # week 7 day 6 is race day
        self.assertEqual([s["day"] for s in clean["weeks"][6]["slots"]], [3])
        self.assertTrue(any("race day" in w for w in warnings))

    def test_too_many_drops_is_invalid(self):
        spec = full_spec()
        for wk in spec["weeks"]:
            wk["slots"] += [spec_slot(2), spec_slot(4), spec_slot(7)]
        with self.assertRaises(tp.PlanSpecInvalid):
            self.validate(spec)

    def test_missing_week_is_invalid(self):
        spec = full_spec(weeks=6)
        with self.assertRaises(tp.PlanSpecInvalid):
            self.validate(spec)

    def test_level_gate_replaces_intervals_for_returning(self):
        clean, warnings = self.validate(full_spec(extra={1: [spec_slot(5, tid="t_int", intensity="hard")]}),
                                        level="returning")
        friday = next(s for s in clean["weeks"][0]["slots"] if s["day"] == 5)
        self.assertEqual(friday["class_type"], "Endurance")
        self.assertTrue(any("Intervals → Endurance" in w for w in warnings))

    def test_second_run_same_day_dropped_and_orders_renumbered(self):
        clean, _ = self.validate(full_spec(extra={2: [spec_slot(3), spec_slot(3, tid="t_str", disc="stretching",
                                                                              minutes=20)]}))
        wed = [s for s in clean["weeks"][1]["slots"] if s["day"] == 3]
        self.assertEqual([(s["discipline"], s["order"]) for s in wed], [("running", 0), ("stretching", 1)])


class PickTests(PlanTestCase):
    def setUp(self):
        super().setUp()
        p = patch.object(tp, "MIN_TYPE_CLASSES", 1)   # tiny hand-made pools still reach the menu
        p.start()
        self.addCleanup(p.stop)

    def picks(self, spec, **over):
        inputs = self.inputs(**over)
        _, allowed = tp.catalog_menu(inputs)
        clean, _ = tp.validate_spec(spec, inputs, allowed)
        return clean, tp.pick_classes(self.a, clean, inputs, today=TODAY), inputs

    def one_slot_spec(self, slot):
        spec = full_spec()
        spec["weeks"][1]["slots"] = [spec_slot(3), slot]
        return spec

    def test_every_slot_picked_no_ride_twice_and_deterministic(self):
        clean, picks, inputs = self.picks(full_spec())
        ids = [p["ride_id"] for p in picks.values()]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(picks, tp.pick_classes(self.a, clean, inputs, today=TODAY))

    def test_excludes_other_programs_pins_and_recent_takes(self):
        newest = "t_lift-30-0"
        other = Program.objects.create(user=self.a, name="Other", slug="o", kind="split")
        ProgramSlot.objects.create(week=ProgramWeek.objects.create(program=other, number=1), title="x",
                                   peloton_ride_id=newest)
        second = "t_lift-30-1"
        self.run_workout(self.a, TODAY - timedelta(days=10), ride_id=second, disc="strength", wid="recent")
        _, picks, _ = self.picks(self.one_slot_spec(spec_slot(1, tid="t_lift", disc="strength", intensity="moderate")))
        all_ids = {p["ride_id"] for p in picks.values()} | {a for p in picks.values() for a in p["alternates"]}
        self.assertNotIn(newest, all_ids)
        self.assertNotIn(second, all_ids)

    def test_old_take_allowed_and_labeled_repeat(self):
        PelotonClass.objects.filter(class_type_id="t_lift").exclude(ride_id="t_lift-30-0").update(is_available=False)
        for i in range(3):
            make_class(f"lift-old-{i}", "t_lift", 30, diff=5.0, aired_days_ago=900 + i)
        self.run_workout(self.a, TODAY - timedelta(days=200), ride_id="t_lift-30-0", disc="strength", wid="old")
        _, picks, _ = self.picks(self.one_slot_spec(spec_slot(1, tid="t_lift", disc="strength", intensity="moderate")))
        pick = picks["2-1-0"]
        self.assertEqual(pick["ride_id"], "t_lift-30-0")
        self.assertTrue(pick["repeat"])

    def test_repeat_loses_to_comparable_class_aired_up_to_180_days_earlier(self):
        PelotonClass.objects.filter(class_type_id="t_lift").update(is_available=False)
        make_class("rep", "t_lift", 30, aired_days_ago=10)
        make_class("fresh", "t_lift", 30, aired_days_ago=110)
        make_class("older", "t_lift", 30, aired_days_ago=400)
        self.run_workout(self.a, TODAY - timedelta(days=200), ride_id="rep", disc="strength", wid="rep")
        _, picks, _ = self.picks(self.one_slot_spec(spec_slot(1, tid="t_lift", disc="strength", intensity="moderate")))
        self.assertEqual(picks["2-1-0"]["ride_id"], "fresh")

    def test_newer_wins_between_equals(self):
        PelotonClass.objects.filter(class_type_id="t_lift").update(is_available=False)
        make_class("new", "t_lift", 30, aired_days_ago=5, instructor="A")
        make_class("old", "t_lift", 30, aired_days_ago=50, instructor="B")
        make_class("older", "t_lift", 30, aired_days_ago=80, instructor="C")
        _, picks, _ = self.picks(self.one_slot_spec(spec_slot(1, tid="t_lift", disc="strength", intensity="moderate")))
        self.assertEqual(picks["2-1-0"]["ride_id"], "new")

    def test_low_rating_filtered_only_when_three_remain(self):
        PelotonClass.objects.filter(class_type_id="t_lift").update(is_available=False)
        make_class("bad", "t_lift", 30, aired_days_ago=1, rating=0.5, n=500)
        make_class("ok1", "t_lift", 30, aired_days_ago=20)
        make_class("ok2", "t_lift", 30, aired_days_ago=30)
        spec = self.one_slot_spec(spec_slot(1, tid="t_lift", disc="strength", intensity="moderate"))
        _, picks, _ = self.picks(spec)
        self.assertEqual(picks["2-1-0"]["ride_id"], "bad")         # only 2 would remain — floor skipped
        make_class("ok3", "t_lift", 30, aired_days_ago=40)
        _, picks, _ = self.picks(spec)
        self.assertEqual(picks["2-1-0"]["ride_id"], "ok1")

    def test_hard_picks_come_from_the_top_band(self):
        PelotonClass.objects.filter(class_type_id="t_int").update(is_available=False)
        for i in range(10):
            make_class(f"int{i}", "t_int", 30, diff=1.0 + i, aired_days_ago=5 + (9 - i) * 10)  # easiest is newest
        _, picks, _ = self.picks(self.one_slot_spec(spec_slot(1, tid="t_int", intensity="hard")), level="advanced")
        self.assertGreaterEqual(PelotonClass.objects.get(pk=picks["2-1-0"]["ride_id"]).difficulty_estimate, 6.4)

    def test_beginner_week_one_pick_from_lowest_half(self):
        PelotonClass.objects.filter(class_type_id="t_beg").update(is_available=False)
        for i in range(10):
            make_class(f"beg{i}", "t_beg", 20, diff=1.0 + i, aired_days_ago=5 + i * 10)   # hardest is oldest…
        PelotonClass.objects.filter(pk="beg9").update(original_air_time=NOW)               # …except the newest
        spec = full_spec()
        spec["weeks"][0]["slots"] = [spec_slot(3, tid="t_beg", minutes=20, intensity="moderate")]
        _, picks, _ = self.picks(spec, level="beginner")
        self.assertLessEqual(PelotonClass.objects.get(pk=picks["1-3-0"]["ride_id"]).difficulty_estimate, 5.5)


class GenerateAndCreateTests(PlanTestCase):
    def make_draft(self, **over):
        return PlanDraft.objects.create(user=self.a, inputs_json=self.inputs(**over))

    def test_generate_ready_with_picks(self):
        draft = self.make_draft()
        with patch.object(llm, "call_json", return_value=full_spec()) as call:
            tp._generate(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ready", draft.error)
        self.assertTrue(draft.picks_json)
        self.assertIn("RUNNING — LAST 12 WEEKS", draft.context_text)
        self.assertEqual(call.call_args.kwargs["feature"], "ai_program_tools")
        self.assertEqual(call.call_args.kwargs["model"], llm.SONNET)

    def test_generate_budget_exceeded(self):
        draft = self.make_draft()
        with patch.object(llm, "call_json", side_effect=llm.AIBudgetExceeded()):
            tp._generate(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "failed")
        self.assertIn("month's AI limit", draft.error)

    def test_generate_bad_json_gives_a_plain_message_and_logs_the_reply(self):
        from workouts.models import WebhookError
        draft = self.make_draft()
        bad = llm.AIBadJSON("The AI's reply wasn't valid JSON (Expecting value).", "Sorry, I can't", "end_turn")
        with patch.object(llm, "call_json", side_effect=bad):
            tp._generate(draft.pk)
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.error), ("failed", "The AI's reply wasn't a readable plan. Try again."))
        self.assertIn("Sorry, I can't", WebhookError.objects.get(source="training_plan", user=self.a).detail)

    def test_generate_cut_off_reply(self):
        draft = self.make_draft()
        with patch.object(llm, "call_json", side_effect=llm.AIBadJSON("cut off", '{"weeks": [', "max_tokens")):
            tp._generate(draft.pk)
        draft.refresh_from_db()
        self.assertIn("too long and got cut off", draft.error)

    def test_stale_generating_draft_reads_failed(self):
        draft = self.make_draft()
        PlanDraft.objects.filter(pk=draft.pk).update(updated_at=timezone.now() - timedelta(minutes=11))
        draft.refresh_from_db()
        self.assertEqual(draft.refreshed().status, "failed")

    def ready_draft(self, **over):
        draft = self.make_draft(**over)
        with patch.object(llm, "call_json", return_value=full_spec()):
            tp._generate(draft.pk)
        draft.refresh_from_db()
        return draft

    def test_create_program(self):
        comp = Program.objects.create(user=self.a, name="Split", slug="s", kind="split")
        ProgramSlot.objects.create(week=ProgramWeek.objects.create(program=comp, number=1), day=2, title="Legs",
                                   discipline="strength")
        comp_run = ProgramRun.objects.create(program=comp, start_date=TODAY - timedelta(days=30))
        draft = self.ready_draft(mode="alongside", companion_program_id=str(comp.pk))
        program = tp.create_program_from_draft(draft, "My 10K")
        self.assertEqual(program.weeks.count(), 7)
        self.assertEqual(program.goal_json["race_date"], SAT_RACE.isoformat())
        slots = ProgramSlot.objects.filter(week__program=program)
        self.assertTrue(all(s.spec_json and s.peloton_ride_id for s in slots if not s.title.startswith("Race day")))
        race = slots.get(title="Race day: 10K")
        self.assertTrue(race.optional)
        self.assertEqual((race.peloton_ride_id, race.match_discipline, race.day), ("", "", 6))
        run = program.active_run
        self.assertEqual(run.start_date, WED_START)
        self.assertEqual(run.run_weeks.count(), 7)
        comp_run.refresh_from_db()
        self.assertIsNone(comp_run.end_date)
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.program_id), ("created", program.pk))

    def test_date_guard(self):
        draft = self.ready_draft()
        program = tp.create_program_from_draft(draft, "Plan")
        ride = ProgramSlot.objects.filter(week__program=program).exclude(peloton_ride_id="").first().peloton_ride_id
        before = self.run_workout(self.a, WED_START - timedelta(days=100), ride_id=ride, wid="before")
        during = self.run_workout(self.a, WED_START + timedelta(days=8), ride_id=ride, wid="during")
        self.assertFalse(_in_ai_plan_window(program, before))
        self.assertTrue(_in_ai_plan_window(program, during))
        other = Program(user=self.a, goal_json={})
        self.assertTrue(_in_ai_plan_window(other, before))     # non-AI programs keep date-free matching

    @unittest.skipUnless(connection.vendor == "postgresql", "identify_membership uses a JSON contains lookup")
    def test_date_guard_in_identify_membership(self):
        draft = self.ready_draft()
        program = tp.create_program_from_draft(draft, "Plan")
        ride = ProgramSlot.objects.filter(week__program=program).exclude(peloton_ride_id="").first().peloton_ride_id
        before = self.run_workout(self.a, WED_START - timedelta(days=100), ride_id=ride, wid="before")
        during = self.run_workout(self.a, WED_START + timedelta(days=8), ride_id=ride, wid="during")
        self.assertIsNone(identify_membership(before))
        self.assertEqual(identify_membership(during)[0], program)

    def test_swap_cycles_without_reusing_and_pick_validates(self):
        for i in range(12):   # the plan's seven Wednesdays use most of the fixture's 8 Endurance-30 classes
            make_class(f"extra-end-{i}", "t_end", 30, diff=5.0 + (i % 8) * 0.5, aired_days_ago=200 + i)
        draft = self.ready_draft()
        key = next(iter(draft.picks_json))
        seen = {draft.picks_json[key]["ride_id"]}
        for _ in range(3):
            resp = self.client_a.post(reverse("program_training_plan_swap", args=[draft.pk]), {"key": key})
            self.assertEqual(resp.status_code, 200)
            draft.refresh_from_db()
            rid = draft.picks_json[key]["ride_id"]
            others = {p["ride_id"] for k, p in draft.picks_json.items() if k != key}
            self.assertNotIn(rid, others)
            seen.add(rid)
        self.assertEqual(len(seen), 4)
        resp = self.client_a.post(reverse("program_training_plan_pick", args=[draft.pk]),
                                  {"key": key, "ride_id": "not-an-alternate"})
        self.assertEqual(resp.status_code, 400)
        alt = draft.picks_json[key]["alternates"][0]
        self.client_a.post(reverse("program_training_plan_pick", args=[draft.pk]), {"key": key, "ride_id": alt})
        draft.refresh_from_db()
        self.assertEqual(draft.picks_json[key]["ride_id"], alt)

    def test_review_page_and_create_view(self):
        draft = self.ready_draft()
        page = self.client_a.get(reverse("program_training_plan_draft", args=[draft.pk]))
        self.assertContains(page, "Create plan")
        self.assertContains(page, "WEEK 7")
        resp = self.client_a.post(reverse("program_training_plan_create", args=[draft.pk]), {"name": "Fall 10K"})
        program = Program.objects.get(user=self.a, name="Fall 10K")
        self.assertRedirects(resp, reverse("program_run", args=[program.active_run.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client_a.post(reverse("program_training_plan_swap", args=[draft.pk]),
                                            {"key": "1-3-0"}).status_code, 302)

    def test_post_creation_swap(self):
        draft = self.ready_draft()
        program = tp.create_program_from_draft(draft, "Plan")
        slot = ProgramSlot.objects.filter(week__program=program, week__number=3).exclude(peloton_ride_id="").first()
        slot.alt_ride_ids = ["x"]
        slot.save()
        old = slot.peloton_ride_id
        resp = self.client_a.post(reverse("program_slot_swap", args=[slot.pk]))
        self.assertEqual(resp.status_code, 200)
        slot.refresh_from_db()
        self.assertNotEqual(slot.peloton_ride_id, old)
        self.assertEqual(slot.alt_ride_ids, [])
        used = set(ProgramSlot.objects.filter(week__program=program).exclude(pk=slot.pk)
                   .values_list("peloton_ride_id", flat=True))
        self.assertNotIn(slot.peloton_ride_id, used)

    def test_completed_or_past_slot_cannot_be_swapped(self):
        draft = self.ready_draft()
        program = tp.create_program_from_draft(draft, "Plan")
        slot = ProgramSlot.objects.filter(week__program=program, week__number=2).exclude(peloton_ride_id="").first()
        w = self.run_workout(self.a, WED_START + timedelta(days=7), ride_id=slot.peloton_ride_id, wid="done")
        rw = program.active_run.run_weeks.get(sequence=2)
        ProgramWorkout.objects.create(run_week=rw, slot=slot, workout=w)
        self.assertEqual(self.client_a.post(reverse("program_slot_swap", args=[slot.pk])).status_code, 400)


class TrainingPlanAccessTests(PlanTestCase):
    def test_without_feature_form_is_denied_and_button_hidden(self):
        access = access_for(self.b)
        access.features = ["training", "programs"]
        access.save()
        resp = self.client_b.get(reverse("program_training_plan_new"))
        self.assertEqual(resp.status_code, 403)
        self.assertNotContains(self.client_b.get(reverse("program_list")), "New Training Plan")

    def test_form_renders_with_level(self):
        page = self.client_a.get(reverse("program_training_plan_new"))
        self.assertContains(page, "Suggested from your history")
        self.assertContains(page, "Build my plan")

    def test_empty_catalog_message(self):
        PelotonClass.objects.all().delete()
        self.assertContains(self.client_a.get(reverse("program_training_plan_new")), "Refresh catalog")
        access = access_for(self.b)
        access.features, access.ai_enabled = ["training", "programs", "ai_program_tools"], True
        access.save()
        self.assertContains(self.client_b.get(reverse("program_training_plan_new")), "ask Megan")

    def test_form_post_creates_draft_and_starts_generation(self):
        with patch("workouts.training_plans.start_generation") as start:
            resp = self.client_a.post(reverse("program_training_plan_new"),
                                      self.post(start_date=tp.default_start().isoformat(),
                                                race_date=(tp.default_start() + timedelta(weeks=7)).isoformat()))
        draft = PlanDraft.objects.get(user=self.a)
        self.assertRedirects(resp, reverse("program_training_plan_draft", args=[draft.pk]),
                             fetch_redirect_response=False)
        start.assert_called_once()

    def test_status_polls_while_generating_then_redirects(self):
        draft = PlanDraft.objects.create(user=self.a, inputs_json={})
        self.assertContains(self.client_a.get(reverse("program_training_plan_status", args=[draft.pk])), "every 3s")
        PlanDraft.objects.filter(pk=draft.pk).update(status="failed", error="x")
        resp = self.client_a.get(reverse("program_training_plan_status", args=[draft.pk]))
        self.assertEqual(resp["HX-Redirect"], reverse("program_training_plan_draft", args=[draft.pk]))


class JsonReplyTests(TwoUserTestCase):
    def test_parse_tolerates_prose_and_fences(self):
        self.assertEqual(llm.parse_json_text('{"a": 1}'), {"a": 1})
        self.assertEqual(llm.parse_json_text('Here is your plan:\n```json\n{"a": 1}\n```\nEnjoy!'), {"a": 1})
        self.assertEqual(llm.parse_json_text('Here is your plan:\n{"a": [1, 2]}\nGood luck.'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            llm.parse_json_text("I can't help with that.")

    def test_expect_dict_skips_list_fragments_in_prose(self):
        text = 'Using your days [1, 3, 5, 6] as given:\n{"plan_name": "10K", "weeks": []}'
        self.assertEqual(llm.parse_json_text(text), [1, 3, 5, 6])          # what broke plan generation
        self.assertEqual(llm.parse_json_text(text, expect=dict), {"plan_name": "10K", "weeks": []})
        with self.assertRaises(ValueError):
            llm.parse_json_text("[1, 2, 3]", expect=dict)

    def test_plan_spec_requires_an_object_and_sends_a_system_prompt(self):
        with patch("workouts.llm.requests.post",
                   return_value=self._reply('Days [1, 3]:\n{"weeks": [], "plan_name": "x"}')) as post:
            from workouts.ai import generate_training_plan_spec
            with patch("workouts.ai._training_plan_prompt", return_value="p"):
                raw = generate_training_plan_spec(self.a, {}, "", "")
        self.assertEqual(raw["plan_name"], "x")
        self.assertIn("exactly one JSON object", post.call_args.kwargs["json"]["system"])
        with patch("workouts.llm.requests.post", return_value=self._reply("[1, 2, 3]")), \
             patch("workouts.ai._training_plan_prompt", return_value="p"):
            with self.assertRaises(llm.AIBadJSON) as ctx:
                generate_training_plan_spec(self.a, {}, "", "")
        self.assertEqual(ctx.exception.text, "[1, 2, 3]")    # the reply is kept for Webhook Errors

    def _reply(self, text, stop="end_turn"):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.json.return_value = {"model": llm.SONNET, "stop_reason": stop,
                               "usage": {"input_tokens": 10, "output_tokens": 10},
                               "content": [{"type": "text", "text": text}]}
        return r

    def test_call_json_reports_cut_off_and_unreadable(self):
        with patch("workouts.llm.requests.post", return_value=self._reply('{"weeks": [{"number": 1', "max_tokens")):
            with self.assertRaises(llm.AIBadJSON) as ctx:
                llm.call_json("p", user=self.a, feature="ai_program_tools", model=llm.SONNET)
        self.assertEqual(ctx.exception.stop_reason, "max_tokens")
        with patch("workouts.llm.requests.post", return_value=self._reply("Sure! Here it is:\n{\"ok\": true}")):
            self.assertEqual(llm.call_json("p", user=self.a, feature="ai_program_tools", model=llm.SONNET),
                             {"ok": True})
