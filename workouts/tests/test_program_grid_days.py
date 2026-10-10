"""Run-grid cells show the day a session was actually done, not just its planned day."""
from datetime import date, datetime, timezone as dt_tz

from django.test import TestCase

from workouts.models import CachedWorkout, Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek
from workouts.program_views import _run_grid
from workouts.tests.helpers import make_user


class GridDayLabelTests(TestCase):
    def setUp(self):
        user = make_user("owner", superuser=True)
        program = Program.objects.create(user=user, name="Split", slug="split", kind="split")
        week = ProgramWeek.objects.create(program=program, number=1)
        self.pilates = ProgramSlot.objects.create(week=week, title="Pilates (any class)", day=3)   # planned Wed
        self.yoga = ProgramSlot.objects.create(week=week, title="Yoga (any class)", day=6)         # planned Sat
        self.run = ProgramRun.objects.create(program=program, start_date=date(2026, 9, 14))
        rw = RunWeek.objects.create(run=self.run, program_week=week, sequence=1)
        # Thu Sep 17, 10:35 Pacific
        w = CachedWorkout.objects.create(user=user, workout_id="p1", title="30 min Pilates", discipline="strength",
                                         source="peloton", created_at=datetime(2026, 9, 17, 17, 35, tzinfo=dt_tz.utc))
        ProgramWorkout.objects.create(run_week=rw, slot=self.pilates, workout=w)

    def test_done_cell_shows_actual_weekday_and_open_cell_its_planned_day(self):
        rows, _ = _run_grid(self.run)
        labels = {c["slot"].pk: c["day_label"] for c in rows[0]["cells"]}
        self.assertEqual(labels[self.pilates.pk], "Thu")
        self.assertEqual(labels[self.yoga.pk], "Sat")

    def test_actual_day_uses_local_time(self):
        # 03:00 UTC Sep 21 is still Sunday evening Sep 20 in Los Angeles
        CachedWorkout.objects.filter(workout_id="p1").update(created_at=datetime(2026, 9, 21, 3, 0, tzinfo=dt_tz.utc))
        rows, _ = _run_grid(self.run)
        self.assertEqual({c["slot"].pk: c["day_label"] for c in rows[0]["cells"]}[self.pilates.pk], "Sun")
