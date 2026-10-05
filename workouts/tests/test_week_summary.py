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


class PastWeekTests(TwoUserTestCase):
    """week_summary(user, start, end) / review_chips for a whole past week."""
    WEEK = date(2026, 9, 14)          # Mon 14 → Sun 20; the week before is Sep 7–13

    def setUp(self):
        super().setUp()
        from datetime import timedelta
        self.d = lambda n: self.WEEK + timedelta(days=n)

    def test_arbitrary_week_range(self):
        from datetime import timedelta
        for i in range(4):
            CachedWorkout.objects.create(user=self.b, workout_id=f"now{i}", created_at=local(self.d(i), 8),
                                         duration_seconds=1800)
        CachedWorkout.objects.create(user=self.b, workout_id="prev", created_at=local(self.d(-3), 8),
                                     duration_seconds=1800)
        CachedWorkout.objects.create(user=self.b, workout_id="after", created_at=local(self.d(7), 8),
                                     duration_seconds=1800)
        s = week_summary(self.b, self.WEEK, self.WEEK + timedelta(days=6), targets=TARGETS)
        self.assertEqual(s["days"], 7)
        t = tiles(s)
        self.assertEqual(t["workouts"]["value"], "4")
        self.assertEqual(t["workouts"]["sub"], "1 same time last week")

    def test_review_chips(self):
        from workouts.week_summary import review_chips
        for i in range(3):
            DailyStats.objects.create(user=self.b, date=self.d(i), weight_lb=180, sleep_seconds=7 * 3600,
                                      protein_g_total=100)
            DailyStats.objects.create(user=self.b, date=self.d(i - 7), weight_lb=181, sleep_seconds=6 * 3600 + 48 * 60)
        CachedWorkout.objects.create(user=self.b, workout_id="w", created_at=local(self.d(1), 8))
        chips = {c["key"]: c for c in review_chips(self.b, self.WEEK, goal="loss", targets=TARGETS)}
        self.assertEqual(chips["weight"]["text"], "Weight −1.0 lb")
        self.assertEqual(chips["weight"]["tone"], "text-success")       # down is good for a loss goal
        self.assertEqual(chips["workouts"]["text"], "Workouts 1 (+1)")
        self.assertEqual(chips["sleep"]["text"], "Avg sleep 7h 00m (+12m)")
        self.assertEqual(chips["protein"]["text"], "Protein days 3/7 (+3)")
        neutral = {c["key"]: c for c in review_chips(self.b, self.WEEK, goal=None, targets=TARGETS)}
        self.assertEqual(neutral["weight"]["tone"], "")

    def test_review_chips_ignore_other_users(self):
        from workouts.week_summary import review_chips
        DailyStats.objects.create(user=self.a, date=self.d(0), weight_lb=150, sleep_seconds=8 * 3600)
        DailyStats.objects.create(user=self.a, date=self.d(-7), weight_lb=151, sleep_seconds=8 * 3600)
        self.assertEqual(review_chips(self.b, self.WEEK, targets=TARGETS), [])


class BodyAndMealTests(TwoUserTestCase):
    def test_body_change_chips_need_three_values_and_follow_goal(self):
        from datetime import timedelta
        from workouts.views import _body_change_chips
        today = date(2026, 10, 4)
        for i in range(3):
            DailyStats.objects.create(user=self.b, date=today - timedelta(days=i), weight_lb=178, fat_free_mass_lb=130)
            DailyStats.objects.create(user=self.b, date=today - timedelta(days=30 + i), weight_lb=180, fat_free_mass_lb=129)
        chips = _body_change_chips(self.b, today, "loss")
        self.assertEqual(chips["weight"]["text"], "−2.0 lb · 30d")
        self.assertEqual(chips["weight"]["tone"], "text-success")
        self.assertEqual(chips["lean"]["tone"], "text-success")           # muscle up is good regardless
        self.assertNotIn("fat_pct", chips)                                # no fat data
        self.assertEqual(_body_change_chips(self.b, today, None)["weight"]["tone"], "")
        self.assertEqual(_body_change_chips(self.a, today, "loss"), {})

    def test_meal_groups_order_and_subtotals(self):
        from workouts.models import FoodEntry
        from workouts.views import _meal_groups
        d = date(2026, 10, 4)
        for meal, cal in (("dinner", 600), ("breakfast", 300), ("breakfast", 100), ("", 50)):
            FoodEntry.objects.create(user=self.b, date=d, meal=meal, raw_text=meal, calories=cal, protein_g=10)
        groups = _meal_groups(list(FoodEntry.objects.for_user(self.b).filter(date=d).order_by("logged_at")))
        self.assertEqual([g["label"] for g in groups], ["Breakfast", "Dinner", "Other"])
        self.assertEqual(groups[0]["kcal"], 400)
        self.assertEqual(groups[0]["protein"], 20)
