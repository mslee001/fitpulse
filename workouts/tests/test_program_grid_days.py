"""Run-grid cells show the day a session was actually done, not just its planned day."""
from datetime import date, datetime, timezone as dt_tz
from unittest.mock import patch

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

    def labels(self, today):
        with patch("workouts.program_views.timezone.localdate", return_value=today):
            rows, _ = _run_grid(self.run)
        return {c["slot"].pk: c["day_label"] for c in rows[0]["cells"]}

    def test_done_cell_shows_actual_weekday(self):
        self.assertEqual(self.labels(date(2026, 9, 17))[self.pilates.pk], "Thu")

    def test_open_sessions_follow_the_last_one_done(self):
        # Pilates done Thu 17 → Yoga projected to Fri 18 (planned Sat)
        self.assertEqual(self.labels(date(2026, 9, 17))[self.yoga.pk], "Fri")

    def test_projection_never_lands_in_the_past(self):
        self.assertEqual(self.labels(date(2026, 9, 20))[self.yoga.pk], "Sun")

    def test_each_open_session_takes_the_next_day(self):
        extra = ProgramSlot.objects.create(week=self.pilates.week, title="Run", day=7)
        labels = self.labels(date(2026, 9, 17))
        self.assertEqual((labels[self.yoga.pk], labels[extra.pk]), ("Fri", "Sat"))

    def test_closed_pass_keeps_planned_days(self):
        self.run.end_date = date(2026, 9, 20)
        self.run.save()
        self.assertEqual(self.labels(date(2026, 9, 25))[self.yoga.pk], "Sat")

    def test_actual_day_uses_local_time(self):
        # 03:00 UTC Sep 21 is still Sunday evening Sep 20 in Los Angeles
        CachedWorkout.objects.filter(workout_id="p1").update(created_at=datetime(2026, 9, 21, 3, 0, tzinfo=dt_tz.utc))
        self.assertEqual(self.labels(date(2026, 9, 21))[self.pilates.pk], "Sun")
