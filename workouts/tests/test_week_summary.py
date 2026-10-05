from datetime import date, datetime

from django.utils import timezone

from workouts.models import CachedWorkout, DailyStats
from workouts.tests.helpers import TwoUserTestCase
from workouts.week_summary import week_summary

MON = date(2026, 9, 28)   # Mon 2026-09-28 … Sun 2026-10-04
SUN = date(2026, 10, 4)
TARGETS = {"protein_g": 100}


def local(d, hour, minute=0):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour, minute))


def tiles(summary):
    return {t["key"]: t for t in summary["tiles"]}


class WeekSummaryTests(TwoUserTestCase):
    def workout(self, user, wid, when, minutes=30):
        return CachedWorkout.objects.create(user=user, workout_id=wid, title=wid,
                                            created_at=when, duration_seconds=minutes * 60)

    def stats(self, user, d, **fields):
        return DailyStats.objects.create(user=user, date=d, **fields)

    def test_monday_only_week(self):
        self.workout(self.b, "mon", local(MON, 7), minutes=45)
        self.stats(self.b, MON, protein_g_total=95, sleep_seconds=7 * 3600 + 4 * 60)
        s = week_summary(self.b, MON, targets=TARGETS)
        self.assertEqual(s["start"], MON)
        self.assertEqual(s["days"], 1)
        t = tiles(s)
        self.assertEqual(t["workouts"]["value"], "1")
        self.assertEqual(t["training_time"]["value"], "45m")
        self.assertEqual(t["sleep"]["value"], "7h 04m")
        self.assertEqual(t["protein"]["value"], "1 / 1")   # 95 ≥ 90% of 100

    def test_sunday_covers_whole_week(self):
        for i, d in enumerate([MON, date(2026, 10, 1), SUN]):
            self.workout(self.b, f"w{i}", local(d, 9), minutes=60)
        self.stats(self.b, MON, protein_g_total=120)
        self.stats(self.b, SUN, protein_g_total=50)
        s = week_summary(self.b, SUN, targets=TARGETS)
        self.assertEqual(s["start"], MON)
        self.assertEqual(s["days"], 7)
        t = tiles(s)
        self.assertEqual(t["workouts"]["value"], "3")
        self.assertEqual(t["training_time"]["value"], "3h 00m")
        self.assertEqual(t["protein"]["value"], "1 / 7")

    def test_last_week_spans_same_weekdays(self):
        wed = date(2026, 9, 30)
        # Last week: Mon and Wed are inside the span; Thu is after "same time" and doesn't count.
        self.workout(self.b, "lw_mon", local(date(2026, 9, 21), 8))
        self.workout(self.b, "lw_wed", local(date(2026, 9, 23), 8))
        self.workout(self.b, "lw_thu", local(date(2026, 9, 24), 8))
        # Late Sunday local is Monday in UTC: still last week, outside this week.
        self.workout(self.b, "sun_late", local(date(2026, 9, 27), 23, 30))
        self.workout(self.b, "this_tue", local(date(2026, 9, 29), 8))
        t = tiles(week_summary(self.b, wed, targets=TARGETS))
        self.assertEqual(t["workouts"]["value"], "1")
        self.assertEqual(t["workouts"]["sub"], "2 same time last week")
        self.assertEqual(t["training_time"]["sub"], "1h 00m same time last week")

    def test_other_users_rows_never_counted(self):
        self.workout(self.a, "a1", local(MON, 7))
        self.stats(self.a, MON, sleep_seconds=8 * 3600, protein_g_total=200)
        s = week_summary(self.b, SUN, targets=TARGETS)
        self.assertEqual(s["tiles"], [])

    def test_no_sleep_data_hides_tile(self):
        self.workout(self.b, "w", local(MON, 7))
        self.stats(self.b, MON, protein_g_total=100)
        t = tiles(week_summary(self.b, SUN, targets=TARGETS))
        self.assertNotIn("sleep", t)
        self.assertIn("workouts", t)

    def test_features_gate_tiles(self):
        from workouts.tests.helpers import make_user
        c = make_user("carol", features=[])
        self.workout(c, "w", local(MON, 7))
        self.stats(c, MON, protein_g_total=100, sleep_seconds=6 * 3600)
        t = tiles(week_summary(c, SUN, targets=TARGETS))
        self.assertEqual(set(t), {"sleep"})

    def test_today_page_renders_this_week(self):
        today = timezone.localdate()
        self.workout(self.b, "today", local(today, 6))
        r = self.client_b.get("/")
        self.assertContains(r, "This week")
        self.assertContains(r, "same time last week")
