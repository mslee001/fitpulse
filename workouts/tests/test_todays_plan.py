"""Today's plan: each current program's sessions for today, dated as on its run page."""
from datetime import date, datetime, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase

from workouts.models import UserAccess, CachedWorkout, Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek
from workouts.program_views import todays_plan
from workouts.programs import start_run
from workouts.tests.helpers import make_user


def _workout(user, wid, title, discipline, when):
    return CachedWorkout.objects.create(user=user, workout_id=wid, title=title, discipline=discipline,
                                        source="peloton", created_at=when, duration_seconds=1800)


class TodaysPlanTests(TestCase):
    def setUp(self):
        self.user = make_user("owner", superuser=True)

    def plan(self, today, user=None):
        with patch("workouts.program_views.timezone.localdate", return_value=today):
            return todays_plan(user or self.user, today)

    def titles(self, today, user=None):
        return {p["program"].name: [(i["title"], i["done"]) for i in p["items"]]
                for p in self.plan(today, user)}

    def make_split(self):
        program = Program.objects.create(user=self.user, name="Split", slug="split", kind="split")
        week = ProgramWeek.objects.create(program=program, number=1)
        pilates = ProgramSlot.objects.create(week=week, title="Pilates (any class)", day=3, order=0,
                                             match_discipline="strength", match_title_keyword="pilates")
        yoga = ProgramSlot.objects.create(week=week, title="Yoga (any class)", day=6, order=1,
                                          match_discipline="yoga")
        run = ProgramRun.objects.create(program=program, start_date=date(2026, 9, 14))
        rw = RunWeek.objects.create(run=run, program_week=week, sequence=1)
        # Thu Sep 17, 10:35 Pacific
        w = _workout(self.user, "p1", "30 min Pilates", "strength", datetime(2026, 9, 17, 17, 35, tzinfo=dt_tz.utc))
        ProgramWorkout.objects.create(run_week=rw, slot=pilates, workout=w)
        return run, rw, pilates, yoga

    # ----- splits: open sessions follow on from the last one done -----

    def test_split_done_today_shows_done_and_nothing_more(self):
        self.make_split()
        self.assertEqual(self.titles(date(2026, 9, 17)), {"Split": [("Pilates (any class)", True)]})

    def test_split_next_session_is_today_the_day_after(self):
        self.make_split()
        self.assertEqual(self.titles(date(2026, 9, 18)), {"Split": [("Yoga (any class)", False)]})

    def test_split_with_a_finished_pass_offers_its_first_session(self):
        run, rw, pilates, yoga = self.make_split()
        w = _workout(self.user, "y1", "20 min Yoga", "yoga", datetime(2026, 9, 18, 17, 0, tzinfo=dt_tz.utc))
        ProgramWorkout.objects.create(run_week=rw, slot=yoga, workout=w)
        self.assertEqual(self.titles(date(2026, 9, 19)), {"Split": [("Pilates (any class)", False)]})
        # ...but not on the day the pass was finished
        self.assertEqual(self.titles(date(2026, 9, 18)), {"Split": [("Yoga (any class)", True)]})

    # ----- dated AI plans: the plan's own calendar -----

    def make_dated_plan(self):
        program = Program.objects.create(user=self.user, name="10K plan", slug="10k", kind="plan",
                                         goal_json={"start_date": "2026-10-05", "weeks_info": {}, "pace_summary": {}})
        weeks = [ProgramWeek.objects.create(program=program, number=n) for n in (1, 2)]
        slots = {}
        for wk in weeks:
            slots[(wk.number, "tue")] = ProgramSlot.objects.create(
                week=wk, title=f"30 min Endurance Run W{wk.number}", day=2, discipline="running",
                duration_min=30, spec_json={"pace_zone": "Moderate"})
            slots[(wk.number, "thu")] = ProgramSlot.objects.create(
                week=wk, title=f"20 min Yoga W{wk.number}", day=4, discipline="yoga", optional=True)
        run = start_run(program, date(2026, 10, 5))
        return run, slots

    def test_dated_plan_lists_the_sessions_dated_today(self):
        self.make_dated_plan()
        plan = self.plan(date(2026, 10, 13))   # Tue of week 2
        self.assertEqual([(i["title"], i["done"]) for i in plan[0]["items"]],
                         [("30 min Endurance Run W2", False)])
        self.assertEqual(plan[0]["items"][0]["pace_zone"], "Moderate")
        self.assertEqual(plan[0]["progress"]["current"], 2)

    def test_dated_plan_marks_optional_sessions(self):
        self.make_dated_plan()
        item = self.plan(date(2026, 10, 15))[0]["items"][0]   # Thu of week 2
        self.assertTrue(item["optional"])

    def test_dated_plan_session_done_today_is_done(self):
        run, slots = self.make_dated_plan()
        w = _workout(self.user, "r1", "30 min Endurance Run", "running", datetime(2026, 10, 13, 15, 0, tzinfo=dt_tz.utc))
        rw = run.run_weeks.get(sequence=2)
        ProgramWorkout.objects.create(run_week=rw, slot=slots[(2, "tue")], workout=w)
        items = self.plan(date(2026, 10, 13))[0]["items"]
        self.assertEqual([(i["title"], i["done"]) for i in items], [("30 min Endurance Run W2", True)])
        self.assertEqual(items[0]["workout"], w)

    def test_rest_day_keeps_the_program_with_no_items(self):
        self.make_dated_plan()
        self.assertEqual(self.titles(date(2026, 10, 14)), {"10K plan": []})   # Wed

    def test_nothing_before_the_plan_starts_or_after_it_ends(self):
        self.make_dated_plan()
        self.assertEqual(self.titles(date(2026, 9, 29)), {"10K plan": []})
        self.assertEqual(self.titles(date(2026, 10, 27)), {"10K plan": []})

    # ----- undated plans: only the current week is projected -----

    def test_undated_plan_projects_only_the_current_week(self):
        program = Program.objects.create(user=self.user, name="HiLit", slug="hilit", kind="plan")
        for n in (1, 2, 3):
            wk = ProgramWeek.objects.create(program=program, number=n)
            ProgramSlot.objects.create(week=wk, title=f"Mobility W{n}", day=1)
        start_run(program, date(2026, 10, 12))
        self.assertEqual(self.titles(date(2026, 10, 14)), {"HiLit": [("Mobility W1", False)]})

    # ----- whose programs -----

    def test_ended_runs_and_other_users_programs_are_left_out(self):
        run, *_ = self.make_split()
        other = make_user("bob", features=["training", "programs"])
        self.assertEqual(self.plan(date(2026, 9, 18), other), [])
        run.end_date = date(2026, 9, 17)
        run.save()
        self.assertEqual(self.plan(date(2026, 9, 18)), [])


class TodayPageTests(TestCase):
    def setUp(self):
        self.user = make_user("bob", features=["training", "programs"])
        program = Program.objects.create(user=self.user, name="Bob's Split", slug="split", kind="split")
        week = ProgramWeek.objects.create(program=program, number=1)
        ProgramSlot.objects.create(week=week, title="45 min Upper Body", day=1, discipline="strength", duration_min=45)
        start_run(program, date(2026, 10, 1))
        RunWeek.objects.create(run=program.active_run, program_week=week, sequence=1)
        self.client.force_login(self.user)

    def test_today_page_shows_the_plan(self):
        r = self.client.get("/")
        self.assertContains(r, ">Today's plan</h2>")
        self.assertContains(r, "45 min Upper Body")
        self.assertContains(r, "Bob&#x27;s Split")

    def test_hidden_without_the_programs_feature(self):
        UserAccess.objects.filter(user=self.user).update(features=["training"])
        r = self.client.get("/")
        self.assertNotContains(r, ">Today's plan</h2>")
