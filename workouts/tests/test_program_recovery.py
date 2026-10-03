from datetime import date, datetime, timedelta, timezone as dt_tz

from django.contrib.auth import get_user_model
from django.test import TestCase

from workouts.models import (
    CachedWorkout, Program, ProgramRecovery, ProgramRun, ProgramSlot, ProgramWeek,
    ProgramWorkout, RunWeek,
)
from workouts.programs import (
    RECOVERY_MAX_MIN, RECOVERY_WINDOW_MIN, associate_workout, attach_recoveries,
    reconcile_program_extras, recovery_kind,
)
from workouts.program_views import _run_grid

BASE = datetime(2026, 9, 22, 19, 0, tzinfo=dt_tz.utc)   # 12:00 Pacific
_n = 0


def owner():
    return get_user_model().objects.get_or_create(username="owner", defaults={"is_superuser": True})[0]


def mk(offset_min, minutes, discipline, title, ride_id="", source="peloton"):
    """A CachedWorkout starting offset_min after BASE, lasting `minutes`."""
    global _n
    _n += 1
    return CachedWorkout.objects.create(
        user=owner(), workout_id=f"w{_n}", ride_id=ride_id, title=title, discipline=discipline,
        source=source, created_at=BASE + timedelta(minutes=offset_min),
        duration_seconds=minutes * 60,
    )


class Base(TestCase):
    def setUp(self):
        self.program = Program.objects.create(user=owner(), name="Split", slug="split", kind="split",
                                              match_strategy="ride_ids", track_recovery=True)
        self.week = ProgramWeek.objects.create(program=self.program, number=1)
        self.push = ProgramSlot.objects.create(week=self.week, title="Push", peloton_ride_id="ridePush", day=1)
        self.pilates = ProgramSlot.objects.create(
            week=self.week, title="Pilates (any class)", day=3, match_discipline="strength",
            match_title_keyword="pilates")
        self.yoga = ProgramSlot.objects.create(
            week=self.week, title="Yoga (any class)", day=6, match_discipline="yoga")
        self.run = ProgramRun.objects.create(program=self.program, start_date=date(2026, 9, 14))

    def main(self, offset_min=0, minutes=45, ride_id="ridePush", title="45 min Push + Run"):
        w = mk(offset_min, minutes, "circuit", title, ride_id)
        return associate_workout(w)

    def chain(self, entry):
        return [(r.kind, r.workout.title) for r in entry.recoveries.all()]


class RecoveryKindTests(TestCase):
    def test_classification(self):
        self.assertEqual(recovery_kind(mk(0, 5, "walking", "5 min Cool Down Walk")), "walk")
        self.assertEqual(recovery_kind(mk(0, 15, "stretching", "15 min Full Body Stretch")), "stretch")
        self.assertIsNone(recovery_kind(mk(0, 45, "walking", "45 min Hike")))            # too long
        self.assertIsNone(recovery_kind(mk(0, 30, "stretching", "30 min Pilates")))      # a workout, not a cool-down
        self.assertIsNone(recovery_kind(mk(0, 10, "yoga", "10 min Yoga")))
        self.assertEqual(recovery_kind(mk(0, RECOVERY_MAX_MIN, "walking", "edge")), "walk")


class AttachRecoveryTests(Base):
    def test_walk_then_stretch_chain(self):
        e = self.main()                                            # ends +45
        mk(46, 5, "walking", "5 min Cool Down Walk")               # gap 1  -> ends +51
        mk(56, 15, "stretching", "15 min Full Body Stretch")       # gap 5
        self.assertEqual(attach_recoveries(self.run), 2)
        self.assertEqual(self.chain(e), [("walk", "5 min Cool Down Walk"), ("stretch", "15 min Full Body Stretch")])

    def test_stretch_only_after_main(self):
        e = self.main()
        mk(48, 5, "stretching", "5 min Post-Run Stretch")
        attach_recoveries(self.run)
        self.assertEqual(self.chain(e), [("stretch", "5 min Post-Run Stretch")])

    def test_chain_ends_after_a_stretch(self):
        e = self.main()
        mk(46, 5, "stretching", "stretch A")
        mk(52, 5, "walking", "walk after stretch")     # a walk after a stretch isn't part of the chain
        attach_recoveries(self.run)
        self.assertEqual(self.chain(e), [("stretch", "stretch A")])

    def test_window_boundaries(self):
        e = self.main()                                # ends +45
        mk(45 + RECOVERY_WINDOW_MIN, 5, "walking", "on the edge")       # exactly 10 min gap: counts
        attach_recoveries(self.run)
        self.assertEqual(len(self.chain(e)), 1)
        e2 = self.main(offset_min=200, ride_id="ridePush", title="second push")   # second pass
        mk(200 + 45 + RECOVERY_WINDOW_MIN + 1, 5, "walking", "too late")          # 11 min gap: doesn't
        attach_recoveries(self.run)
        self.assertEqual(self.chain(e2), [])

    def test_walk_that_overlaps_the_workout_is_not_a_cooldown(self):
        e = self.main()
        mk(30, 15, "walking", "walked during class")   # starts 15 min before the workout ends
        attach_recoveries(self.run)
        self.assertEqual(self.chain(e), [])

    def test_each_session_attaches_to_only_one_workout(self):
        e1 = self.main()                                            # ends +45
        e2 = self.main(offset_min=47, title="push again")           # starts right after; ends +92
        mk(46, 5, "walking", "the only walk")                       # fits after e1 (gap 1)
        attach_recoveries(self.run)
        self.assertEqual(len(self.chain(e1)) + len(self.chain(e2)), 1)
        self.assertEqual(ProgramRecovery.objects.count(), 1)

    def test_idempotent_and_late_stretch_extends_existing_chain(self):
        e = self.main()
        mk(46, 5, "walking", "walk")
        self.assertEqual(attach_recoveries(self.run), 1)
        self.assertEqual(attach_recoveries(self.run), 0)            # nothing new
        mk(56, 15, "stretching", "stretch that synced later")
        self.assertEqual(attach_recoveries(self.run), 1)
        self.assertEqual([k for k, _ in self.chain(e)], ["walk", "stretch"])

    def test_workouts_already_on_a_grid_are_never_recoveries(self):
        e = self.main()
        yoga = mk(50, 5, "walking", "Cool Down Walk")
        ProgramWorkout.objects.create(run_week=e.run_week, slot=None, workout=yoga)   # tracked as its own completion
        attach_recoveries(self.run)
        self.assertEqual(self.chain(e), [])

    def test_disabled_program_attaches_nothing(self):
        self.program.track_recovery = False
        self.program.save()
        e = self.main()
        mk(46, 5, "walking", "walk")
        self.assertEqual(attach_recoveries(self.run), 0)
        self.assertEqual(self.chain(e), [])

    def test_recoveries_go_with_their_pass(self):
        e = self.main()
        mk(46, 5, "walking", "walk")
        attach_recoveries(self.run)
        e.run_week.delete()
        self.assertEqual(ProgramRecovery.objects.count(), 0)

    def test_grid_shows_recoveries_and_totals(self):
        self.main()
        mk(46, 5, "walking", "walk")
        mk(56, 15, "stretching", "stretch")
        attach_recoveries(self.run)
        rows, totals = _run_grid(self.run)
        cell = [c for r in rows for c in r["cells"] if c["entry"]][0]
        self.assertEqual([r.kind for r in cell["recoveries"]], ["walk", "stretch"])
        self.assertEqual(totals["recovery_sessions"], 2)
        self.assertEqual(totals["recovery_minutes"], 20)
        self.assertEqual(totals["completions_with_recovery"], 1)


class AnyClassSlotTests(Base):
    def test_pilates_stored_as_strength_matches_by_title_keyword(self):
        w = mk(0, 30, "strength", "30 min Pilates")
        entry = associate_workout(w)
        self.assertEqual(entry.slot, self.pilates)
        self.assertEqual(entry.matched_by, "discipline")

    def test_plain_strength_class_does_not_match_the_pilates_slot(self):
        self.assertIsNone(associate_workout(mk(0, 30, "strength", "30 min Full Body Strength")))

    def test_yoga_matches_by_discipline(self):
        self.assertEqual(associate_workout(mk(0, 30, "yoga", "30 min Yoga Flow")).slot, self.yoga)

    def test_classes_before_the_run_started_are_ignored(self):
        old = mk(-60 * 24 * 30, 30, "yoga", "old yoga")            # a month before BASE < run start
        self.assertIsNone(associate_workout(old))

    def test_second_devices_recording_of_the_same_session_is_skipped(self):
        first = associate_workout(mk(0, 30, "yoga", "30 min Yoga Flow"))
        dup = mk(8, 25, "yoga", "Yoga", source="google_health")    # overlaps the Peloton class
        self.assertIsNotNone(first)
        self.assertIsNone(associate_workout(dup))
        self.assertEqual(ProgramWorkout.objects.count(), 1)

    def test_lands_in_the_pass_whose_dates_it_belongs_to(self):
        # pass 1: a lone push on day 0; pass 2: the real week, days 1-6
        rw1 = RunWeek.objects.create(run=self.run, program_week=self.week, sequence=1)
        ProgramWorkout.objects.create(run_week=rw1, slot=self.push, workout=mk(0, 45, "circuit", "p1", "ridePush"))
        rw2 = RunWeek.objects.create(run=self.run, program_week=self.week, sequence=2)
        ProgramWorkout.objects.create(run_week=rw2, slot=self.push, workout=mk(60 * 24, 45, "circuit", "p2", "ridePush"))
        ProgramWorkout.objects.create(run_week=rw2, slot=None, workout=mk(60 * 24 * 6, 45, "circuit", "p2b", "ridePush"))
        pil = mk(60 * 24 * 3, 30, "strength", "30 min Pilates")     # day 3: inside pass 2's range, near pass 1
        self.assertEqual(associate_workout(pil).run_week, rw2)

    def test_reconcile_backfills_and_attaches_in_one_pass(self):
        e = self.main()
        mk(46, 5, "walking", "walk")
        mk(60 * 24 * 2, 30, "strength", "30 min Pilates")
        result = reconcile_program_extras(owner())
        self.assertEqual(result, {"associated": 1, "recoveries": 1})
        self.assertEqual(reconcile_program_extras(owner()), {"associated": 0, "recoveries": 0})   # idempotent
