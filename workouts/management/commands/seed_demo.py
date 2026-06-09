"""
Seed a fresh database with realistic demo data.

Usage:
    DB_FILE=demo.sqlite3 venv/bin/python3 manage.py migrate
    DB_FILE=demo.sqlite3 venv/bin/python3 manage.py seed_demo

Then run the server:
    DB_FILE=demo.sqlite3 venv/bin/python3 manage.py runserver
"""

import datetime
import random
import uuid
from django.core.management.base import BaseCommand
from django.utils import timezone


def _d(days_ago):
    return datetime.date.today() - datetime.timedelta(days=days_ago)


def _dt(days_ago, hour=8, minute=0):
    return datetime.datetime(
        *_d(days_ago).timetuple()[:3], hour, minute,
        tzinfo=datetime.timezone.utc,
    )


def _uid():
    return uuid.uuid4().hex


class Command(BaseCommand):
    help = "Populate demo.sqlite3 with realistic fake data for demos."

    def handle(self, *args, **options):
        from workouts.models import (
            CachedWorkout, DailyStats, UserSettings, NutritionProfile,
            FoodEntry, SavedMeal, HungerCheck, SideEffectLog,
            Intervention, DoseChange, WeeklyReview, BodyMeasurement,
        )

        self.stdout.write("Clearing existing data...")
        for M in [CachedWorkout, DailyStats, UserSettings, NutritionProfile,
                  FoodEntry, SavedMeal, HungerCheck, SideEffectLog,
                  Intervention, DoseChange, WeeklyReview, BodyMeasurement]:
            M.objects.all().delete()

        rng = random.Random(42)

        # ── UserSettings ────────────────────────────────────────────────────
        settings = UserSettings.objects.create(
            pk=1, ftp=220,
            ai_insights=SAMPLE_INSIGHTS,
            ai_insights_generated_at=timezone.now() - datetime.timedelta(hours=3),
            ai_pattern_insights=SAMPLE_PATTERN_INSIGHTS,
            ai_pattern_insights_generated_at=timezone.now() - datetime.timedelta(hours=2),
            ai_body_commentary=SAMPLE_BODY_COMMENTARY,
            ai_body_commentary_generated_at=timezone.now() - datetime.timedelta(hours=1),
            ai_nutrition_insights=SAMPLE_NUTRITION_INSIGHTS,
            ai_nutrition_insights_generated_at=timezone.now() - datetime.timedelta(hours=4),
            ai_nutrition_insights_range=30,
        )

        # ── NutritionProfile ─────────────────────────────────────────────────
        NutritionProfile.objects.create(
            pk=1,
            height_cm=165.0,
            age=34,
            biological_sex="female",
            activity_level="active",
            goal="loss",
            deficit_pct=20.0,
            protein_g_per_kg_lean=2.2,
            manual_calories=1650,
            manual_protein_g=140,
            manual_fiber_g=25,
        )

        # ── Intervention + DoseChange ─────────────────────────────────────────
        iv = Intervention.objects.create(
            name="Semaglutide",
            category="medication",
            start_date=_d(90),
            expected_effects="Appetite reduction, gradual weight loss, improved blood sugar regulation",
            notes="Weekly injection, dose escalation protocol",
        )
        DoseChange.objects.create(intervention=iv, dose="0.25mg", start_date=_d(90), end_date=_d(62))
        DoseChange.objects.create(intervention=iv, dose="0.5mg", start_date=_d(62), end_date=_d(34))
        DoseChange.objects.create(intervention=iv, dose="1mg", start_date=_d(34), end_date=None)

        iv2 = Intervention.objects.create(
            name="Creatine Monohydrate",
            category="supplement",
            start_date=_d(60),
            expected_effects="Improved strength output, faster recovery, slight lean mass gain",
            notes="5g daily with post-workout shake",
        )
        DoseChange.objects.create(intervention=iv2, dose="5g/day", start_date=_d(60), end_date=None)

        # ── DailyStats (90 days) ──────────────────────────────────────────────
        self.stdout.write("Seeding 90 days of wellness + body data...")
        base_weight = 172.0
        base_hrv = 48.0
        daily_stats = []
        for i in range(90, -1, -1):
            d = _d(i)
            # gradual weight loss with noise
            weight = base_weight - (90 - i) * 0.045 + rng.gauss(0, 0.4)
            fat_pct = 29.5 - (90 - i) * 0.04 + rng.gauss(0, 0.3)
            fat_mass = weight * fat_pct / 100
            lean = weight - fat_mass
            muscle = lean * 0.87

            hrv = base_hrv + rng.gauss(0, 5)
            sleep_s = int(rng.gauss(27000, 3600))  # ~7.5h ± 1h
            sleep_deep = int(sleep_s * rng.uniform(0.15, 0.25))
            sleep_rem = int(sleep_s * rng.uniform(0.20, 0.30))
            sleep_light = sleep_s - sleep_deep - sleep_rem

            rhr = int(rng.gauss(56, 4))
            bb_high = int(rng.gauss(82, 10))
            bb_low = int(rng.gauss(28, 8))
            steps = int(rng.gauss(9200, 1800))
            stress = int(rng.gauss(32, 10))

            readiness = int(rng.gauss(72, 12))
            readiness = max(30, min(99, readiness))
            readiness_label = (
                "Ready" if readiness >= 70
                else "Moderate" if readiness >= 50
                else "Low"
            )

            # Nutrition totals (skip ~15% of days)
            logged = rng.random() > 0.15
            cal_total = rng.gauss(1680, 120) if logged else None
            prot_total = rng.gauss(138, 12) if logged else None
            carbs_total = rng.gauss(160, 20) if logged else None
            fat_total = rng.gauss(58, 8) if logged else None
            fiber_total = rng.gauss(24, 4) if logged else None

            ds = DailyStats(
                date=d,
                weight_lb=round(weight, 1),
                fat_mass_lb=round(fat_mass, 1),
                fat_free_mass_lb=round(lean, 1),
                muscle_mass_lb=round(muscle, 1),
                fat_ratio_pct=round(fat_pct, 1),
                weight_count=1,
                weight_synced_at=timezone.now(),
                hrv_last_night=round(hrv, 1),
                hrv_weekly_avg=round(hrv + rng.gauss(0, 2), 1),
                hrv_status=rng.choice(["BALANCED", "BALANCED", "BALANCED", "UNBALANCED", "POOR"]),
                hrv_min=int(hrv - rng.uniform(5, 15)),
                hrv_max=int(hrv + rng.uniform(5, 15)),
                resting_hr=rhr,
                sleep_score=int(rng.gauss(74, 9)),
                sleep_seconds=sleep_s,
                sleep_deep_seconds=sleep_deep,
                sleep_rem_seconds=sleep_rem,
                sleep_light_seconds=sleep_light,
                body_battery_high=bb_high,
                body_battery_low=bb_low,
                body_battery_start=bb_high,
                body_battery_end=bb_low,
                body_battery_charge=int(rng.gauss(45, 10)),
                body_battery_drain=int(rng.gauss(52, 12)),
                steps=steps,
                steps_goal=8000,
                active_calories=int(rng.gauss(480, 80)),
                total_calories=int(rng.gauss(2050, 120)),
                bmr_calories=1520,
                floors_climbed=int(rng.gauss(8, 3)),
                floors_climbed_goal=10,
                stress_avg=stress,
                stress_max=stress + int(rng.gauss(20, 5)),
                stress_rest_minutes=int(rng.gauss(320, 40)),
                stress_low_minutes=int(rng.gauss(420, 60)),
                stress_medium_minutes=int(rng.gauss(180, 30)),
                stress_high_minutes=int(rng.gauss(60, 20)),
                training_readiness_score=readiness,
                training_readiness_label=readiness_label,
                training_status=rng.choice(["Productive", "Productive", "Maintaining", "Unproductive", "Recovery"]),
                training_load=round(rng.gauss(340, 80), 1),
                vo2_max_running=round(rng.gauss(42.5, 0.5), 1),
                fitness_age=32,
                respiration_avg=round(rng.gauss(15.2, 0.8), 1),
                spo2_sleep_avg=round(rng.gauss(96.5, 0.5), 1),
                synced_at=timezone.now(),
                cal_total=round(cal_total, 0) if cal_total else None,
                protein_g_total=round(prot_total, 1) if prot_total else None,
                carbs_g_total=round(carbs_total, 1) if carbs_total else None,
                fat_g_total=round(fat_total, 1) if fat_total else None,
                fiber_g_total=round(fiber_total, 1) if fiber_total else None,
            )
            daily_stats.append(ds)
        DailyStats.objects.bulk_create(daily_stats)

        # ── Workouts ──────────────────────────────────────────────────────────
        self.stdout.write("Seeding workouts...")
        _seed_workouts(rng)

        # ── FoodEntries ───────────────────────────────────────────────────────
        self.stdout.write("Seeding food log (30 days)...")
        _seed_nutrition(rng)

        # ── SavedMeals ────────────────────────────────────────────────────────
        _seed_saved_meals()

        # ── HungerChecks ──────────────────────────────────────────────────────
        _seed_hunger(rng)

        # ── SideEffectLogs ────────────────────────────────────────────────────
        _seed_symptoms(rng, iv)

        # ── WeeklyReview ──────────────────────────────────────────────────────
        _seed_weekly_review()

        self.stdout.write(self.style.SUCCESS(
            "\nDemo database seeded successfully.\n"
            "Run with: DB_FILE=demo.sqlite3 venv/bin/python3 manage.py runserver"
        ))


# ── Workout seeding ──────────────────────────────────────────────────────────

CYCLING_TITLES = [
    ("Power Zone Endurance Ride", "Matt Wilpers"),
    ("HIIT Cycling", "Alex Toussaint"),
    ("45 min Climb Ride", "Robin Arzón"),
    ("30 min Pop Ride", "Cody Rigsby"),
    ("60 min Power Zone Max", "Matt Wilpers"),
    ("20 min Express Ride", "Denis Morton"),
    ("45 min Tabata Ride", "Alex Toussaint"),
]

RUNNING_TITLES = [
    ("30 min Fun Run", "Becs Gentry"),
    ("45 min Endurance Run", "Matty Maggiacomo"),
    ("20 min Interval Run", "Robin Arzón"),
    ("30 min HIIT Run", "Becs Gentry"),
    ("60 min Long Run", "Matty Maggiacomo"),
]

STRENGTH_TITLES = [
    ("30 min Full Body Strength", "Adrian Williams"),
    ("20 min Upper Body", "Andy Speer"),
    ("30 min Lower Body", "Adrian Williams"),
    ("20 min Core Strength", "Andy Speer"),
    ("45 min Total Strength", "Adrian Williams"),
]

YOGA_TITLES = [
    ("20 min Morning Yoga", "Anna Greenberg"),
    ("30 min Power Yoga", "Denis Morton"),
    ("15 min Restorative Yoga", "Anna Greenberg"),
]

RIDE_ID_MAP = {}  # title → fixed ride_id for class history


def _get_ride_id(title):
    if title not in RIDE_ID_MAP:
        RIDE_ID_MAP[title] = uuid.uuid4().hex
    return RIDE_ID_MAP[title]


def _perf_graph_cycling(avg_watts, ftp):
    n = 180  # 30 min at every_n=10s
    values = [avg_watts + random.gauss(0, 15) for _ in range(n)]
    return {
        "source": "peloton",
        "metrics_by_slug": {
            "output": {
                "display_name": "Output", "display_unit": "W",
                "values": values, "average_value": avg_watts, "max_value": max(values),
            },
        },
        "effort_zones": {"total_effort_points": int(avg_watts * 1.8)},
        "summaries": {
            "output": {"display_name": "Output", "display_unit": "kJ",
                       "value": round(avg_watts * 0.036)},
        },
        "average_summaries": {},
        "segments": [], "splits": [], "muscle_groups": [],
    }


def _perf_graph_running(avg_pace_s, hr):
    n = 180
    values = [avg_pace_s / 60 + random.gauss(0, 0.1) for _ in range(n)]
    hr_vals = [hr + random.gauss(0, 4) for _ in range(n)]
    return {
        "source": "peloton",
        "metrics_by_slug": {
            "pace": {
                "display_name": "Pace", "display_unit": "min/mi",
                "values": values, "average_value": avg_pace_s / 60, "max_value": None,
            },
            "heart_rate": {
                "display_name": "Heart Rate", "display_unit": "bpm",
                "values": hr_vals, "average_value": hr, "max_value": max(hr_vals),
            },
        },
        "effort_zones": {"total_effort_points": int(hr * 0.9)},
        "summaries": {
            "distance": {"display_name": "Distance", "display_unit": "mi",
                         "value": round(30 / (avg_pace_s / 60), 2)},
        },
        "average_summaries": {
            "avg_pace": {"display_name": "Avg Pace", "display_unit": "min/mi",
                         "value": round(avg_pace_s / 60, 2)},
        },
        "segments": [], "splits": [], "muscle_groups": [],
    }


def _seed_workouts(rng):
    from workouts.models import CachedWorkout

    workouts = []
    ftp = 220

    # ~3 workouts/week for 90 days, varied disciplines
    schedule = []
    for week in range(13):
        days_in_week = rng.sample(range(7), k=rng.randint(2, 4))
        for day_offset in days_in_week:
            days_ago = 90 - week * 7 - day_offset
            if days_ago < 0:
                continue
            disc = rng.choices(
                ["cycling", "running", "strength", "yoga"],
                weights=[40, 25, 25, 10],
            )[0]
            schedule.append((days_ago, disc))

    for days_ago, disc in sorted(schedule, reverse=True):
        hour = rng.randint(6, 18)
        dt = _dt(days_ago, hour, rng.randint(0, 59))

        if disc == "cycling":
            title, instructor = rng.choice(CYCLING_TITLES)
            dur = rng.choice([20 * 60, 30 * 60, 45 * 60, 60 * 60])
            avg_w = int(rng.gauss(165, 20))
            total_out = int(avg_w * dur / 1000)
            hr = int(rng.gauss(155, 8))
            cal = int(rng.gauss(420, 60))
            cadence = int(rng.gauss(82, 5))
            resistance = round(rng.gauss(42, 5), 1)
            rank = rng.randint(800, 15000)
            total_lb = rng.randint(20000, 80000)
            pg = _perf_graph_cycling(avg_w, ftp)
            w = CachedWorkout(
                workout_id=_uid(), ride_id=_get_ride_id(title),
                title=title, discipline="cycling",
                fitness_discipline_display="Cycling",
                workout_type="class", instructor_name=instructor,
                duration_seconds=dur, calories=cal,
                heart_rate_avg=hr, heart_rate_max=hr + rng.randint(10, 25),
                output_watts=total_out, avg_watts=avg_w,
                avg_cadence=cadence, avg_resistance=resistance,
                leaderboard_rank=rank, total_leaderboard_users=total_lb,
                ftp=ftp, performance_graph_json=pg,
                created_at=dt, source="peloton",
                is_pr=rng.random() < 0.08,
                hr_z1_seconds=int(dur * 0.05), hr_z2_seconds=int(dur * 0.20),
                hr_z3_seconds=int(dur * 0.35), hr_z4_seconds=int(dur * 0.30),
                hr_z5_seconds=int(dur * 0.10),
                detail_synced_at=timezone.now(),
            )

        elif disc == "running":
            title, instructor = rng.choice(RUNNING_TITLES)
            dur = rng.choice([20 * 60, 30 * 60, 45 * 60])
            pace_s = int(rng.gauss(570, 30))  # ~9:30/mi
            dist = round(dur / pace_s, 2)
            hr = int(rng.gauss(158, 7))
            cal = int(rng.gauss(340, 40))
            cadence = int(rng.gauss(168, 4))
            pg = _perf_graph_running(pace_s, hr)
            w = CachedWorkout(
                workout_id=_uid(), ride_id=_get_ride_id(title),
                title=title, discipline="running",
                fitness_discipline_display="Running",
                workout_type="class", instructor_name=instructor,
                duration_seconds=dur, calories=cal,
                heart_rate_avg=hr, heart_rate_max=hr + rng.randint(8, 20),
                avg_pace_seconds=pace_s, distance_miles=dist,
                avg_speed_mph=round(3600 / pace_s, 1),
                avg_cadence=cadence, run_cadence_avg=cadence,
                stride_length_avg=round(rng.gauss(108, 5), 1),
                vertical_oscillation_avg=round(rng.gauss(8.2, 0.6), 1),
                vertical_ratio_avg=round(rng.gauss(8.8, 0.5), 1),
                ground_contact_time_avg=round(rng.gauss(262, 12), 1),
                performance_graph_json=pg,
                created_at=dt, source="peloton",
                is_pr=rng.random() < 0.06,
                hr_z1_seconds=int(dur * 0.03), hr_z2_seconds=int(dur * 0.15),
                hr_z3_seconds=int(dur * 0.30), hr_z4_seconds=int(dur * 0.35),
                hr_z5_seconds=int(dur * 0.17),
                detail_synced_at=timezone.now(),
            )

        elif disc == "strength":
            title, instructor = rng.choice(STRENGTH_TITLES)
            dur = rng.choice([20 * 60, 30 * 60, 45 * 60])
            hr = int(rng.gauss(142, 8))
            cal = int(rng.gauss(220, 30))
            effort = round(rng.gauss(72, 8), 1)
            sets = _make_exercise_sets(rng)
            w = CachedWorkout(
                workout_id=_uid(), ride_id=_get_ride_id(title),
                title=title, discipline="strength",
                fitness_discipline_display="Strength",
                workout_type="class", instructor_name=instructor,
                duration_seconds=dur, calories=cal,
                heart_rate_avg=hr, heart_rate_max=hr + rng.randint(10, 20),
                effort_score=effort, average_effort_score=effort,
                exercise_sets_json=sets,
                performance_graph_json={"source": "peloton", "effort_zones": {
                    "total_effort_points": int(effort * 2.5)
                }, "metrics_by_slug": {}, "summaries": {}, "average_summaries": {},
                    "segments": [], "splits": [], "muscle_groups": [
                        {"name": "Glutes", "percentage": 0.28},
                        {"name": "Quads", "percentage": 0.22},
                        {"name": "Core", "percentage": 0.18},
                    ]},
                created_at=dt, source="peloton",
                is_pr=False,
                hr_z1_seconds=int(dur * 0.10), hr_z2_seconds=int(dur * 0.30),
                hr_z3_seconds=int(dur * 0.35), hr_z4_seconds=int(dur * 0.18),
                hr_z5_seconds=int(dur * 0.07),
                detail_synced_at=timezone.now(),
            )

        else:  # yoga
            title, instructor = rng.choice(YOGA_TITLES)
            dur = rng.choice([15 * 60, 20 * 60, 30 * 60])
            hr = int(rng.gauss(95, 8))
            cal = int(rng.gauss(85, 15))
            w = CachedWorkout(
                workout_id=_uid(), ride_id=_get_ride_id(title),
                title=title, discipline="yoga",
                fitness_discipline_display="Yoga",
                workout_type="class", instructor_name=instructor,
                duration_seconds=dur, calories=cal,
                heart_rate_avg=hr,
                performance_graph_json={"source": "peloton", "metrics_by_slug": {},
                    "summaries": {}, "average_summaries": {}, "effort_zones": {},
                    "segments": [], "splits": [], "muscle_groups": []},
                created_at=dt, source="peloton",
                detail_synced_at=timezone.now(),
            )

        workouts.append(w)

    CachedWorkout.objects.bulk_create(workouts)
    count = len(workouts)
    print(f"  Created {count} workouts")


def _make_exercise_sets(rng):
    exercises = [
        ("Squat", "squat"), ("Deadlift", "deadlift"), ("Lunge", "lunge"),
        ("Push-Up", "push_up"), ("Row", "row"), ("Shoulder Press", "shoulder_press"),
        ("Bicep Curl", "bicep_curl"), ("Tricep Extension", "tricep_extension"),
        ("Plank", "plank"), ("Hip Thrust", "hip_thrust"),
    ]
    chosen = rng.sample(exercises, k=rng.randint(4, 7))
    sets = []
    for order, (name, key) in enumerate(chosen):
        for _ in range(rng.randint(2, 4)):
            sets.append({
                "order": order,
                "exercise": name,
                "exercise_key": key,
                "reps": rng.randint(8, 15),
                "weight_kg": round(rng.choice([5, 7.5, 10, 12.5, 15, 17.5, 20, 22.5]), 1),
                "duration_seconds": None,
            })
    return sets


# ── Nutrition seeding ────────────────────────────────────────────────────────

MEAL_TEMPLATES = {
    "breakfast": [
        {"items": [{"name": "Greek yogurt", "qty": "200g", "calories": 130, "protein_g": 18, "carbs_g": 9, "fat_g": 0, "fiber_g": 0},
                   {"name": "Blueberries", "qty": "80g", "calories": 45, "protein_g": 0.5, "carbs_g": 11, "fat_g": 0, "fiber_g": 2},
                   {"name": "Granola", "qty": "30g", "calories": 130, "protein_g": 3, "carbs_g": 20, "fat_g": 4, "fiber_g": 2}],
         "total": (305, 21.5, 40, 4, 4), "text": "Greek yogurt with blueberries and granola"},
        {"items": [{"name": "Eggs scrambled", "qty": "3 eggs", "calories": 210, "protein_g": 18, "carbs_g": 2, "fat_g": 14, "fiber_g": 0},
                   {"name": "Whole wheat toast", "qty": "2 slices", "calories": 140, "protein_g": 5, "carbs_g": 26, "fat_g": 2, "fiber_g": 4},
                   {"name": "Avocado", "qty": "1/2", "calories": 120, "protein_g": 1.5, "carbs_g": 6, "fat_g": 11, "fiber_g": 5}],
         "total": (470, 24.5, 34, 27, 9), "text": "3 scrambled eggs, 2 slices whole wheat toast, half avocado"},
        {"items": [{"name": "Oatmeal", "qty": "80g dry", "calories": 300, "protein_g": 10, "carbs_g": 54, "fat_g": 5, "fiber_g": 8},
                   {"name": "Protein powder", "qty": "1 scoop", "calories": 120, "protein_g": 25, "carbs_g": 3, "fat_g": 1, "fiber_g": 1},
                   {"name": "Banana", "qty": "1 medium", "calories": 105, "protein_g": 1.3, "carbs_g": 27, "fat_g": 0, "fiber_g": 3}],
         "total": (525, 36.3, 84, 6, 12), "text": "Oatmeal with protein powder and banana"},
    ],
    "lunch": [
        {"items": [{"name": "Grilled chicken breast", "qty": "150g", "calories": 248, "protein_g": 46, "carbs_g": 0, "fat_g": 5, "fiber_g": 0},
                   {"name": "Brown rice", "qty": "150g cooked", "calories": 195, "protein_g": 4, "carbs_g": 41, "fat_g": 1, "fiber_g": 2},
                   {"name": "Broccoli", "qty": "150g", "calories": 51, "protein_g": 4.3, "carbs_g": 10, "fat_g": 0.5, "fiber_g": 4}],
         "total": (494, 54.3, 51, 6.5, 6), "text": "Grilled chicken breast, brown rice, steamed broccoli"},
        {"items": [{"name": "Turkey and veggie wrap", "qty": "1 wrap", "calories": 420, "protein_g": 35, "carbs_g": 38, "fat_g": 12, "fiber_g": 6}],
         "total": (420, 35, 38, 12, 6), "text": "Turkey and veggie wrap"},
        {"items": [{"name": "Salmon fillet", "qty": "150g", "calories": 280, "protein_g": 40, "carbs_g": 0, "fat_g": 13, "fiber_g": 0},
                   {"name": "Quinoa", "qty": "120g cooked", "calories": 148, "protein_g": 5.5, "carbs_g": 26, "fat_g": 2.5, "fiber_g": 3},
                   {"name": "Mixed greens salad", "qty": "100g", "calories": 25, "protein_g": 2, "carbs_g": 4, "fat_g": 0, "fiber_g": 2}],
         "total": (453, 47.5, 30, 15.5, 5), "text": "Salmon with quinoa and mixed greens"},
    ],
    "dinner": [
        {"items": [{"name": "Lean ground beef", "qty": "150g", "calories": 300, "protein_g": 33, "carbs_g": 0, "fat_g": 18, "fiber_g": 0},
                   {"name": "Sweet potato", "qty": "200g", "calories": 172, "protein_g": 3.1, "carbs_g": 40, "fat_g": 0, "fiber_g": 6},
                   {"name": "Asparagus", "qty": "150g", "calories": 33, "protein_g": 3.6, "carbs_g": 6, "fat_g": 0, "fiber_g": 3}],
         "total": (505, 39.7, 46, 18, 9), "text": "Ground beef bowl with sweet potato and asparagus"},
        {"items": [{"name": "Shrimp stir fry", "qty": "1 serving", "calories": 380, "protein_g": 38, "carbs_g": 28, "fat_g": 10, "fiber_g": 4}],
         "total": (380, 38, 28, 10, 4), "text": "Shrimp stir fry with vegetables"},
        {"items": [{"name": "Baked chicken thighs", "qty": "200g", "calories": 340, "protein_g": 42, "carbs_g": 0, "fat_g": 18, "fiber_g": 0},
                   {"name": "Roasted vegetables", "qty": "200g", "calories": 110, "protein_g": 3, "carbs_g": 22, "fat_g": 3, "fiber_g": 6},
                   {"name": "Cottage cheese", "qty": "100g", "calories": 98, "protein_g": 11, "carbs_g": 4, "fat_g": 4, "fiber_g": 0}],
         "total": (548, 56, 26, 25, 6), "text": "Baked chicken thighs with roasted vegetables and cottage cheese"},
    ],
    "snack": [
        {"items": [{"name": "Protein shake", "qty": "1 scoop in water", "calories": 120, "protein_g": 25, "carbs_g": 3, "fat_g": 1, "fiber_g": 0}],
         "total": (120, 25, 3, 1, 0), "text": "Protein shake"},
        {"items": [{"name": "Apple", "qty": "1 medium", "calories": 95, "protein_g": 0.5, "carbs_g": 25, "fat_g": 0, "fiber_g": 4},
                   {"name": "Almond butter", "qty": "2 tbsp", "calories": 190, "protein_g": 7, "carbs_g": 6, "fat_g": 17, "fiber_g": 3}],
         "total": (285, 7.5, 31, 17, 7), "text": "Apple with almond butter"},
        {"items": [{"name": "Cottage cheese", "qty": "150g", "calories": 148, "protein_g": 16.5, "carbs_g": 6, "fat_g": 6, "fiber_g": 0}],
         "total": (148, 16.5, 6, 6, 0), "text": "Cottage cheese"},
    ],
}


def _seed_nutrition(rng):
    from workouts.models import FoodEntry
    entries = []
    for days_ago in range(30):
        d = _d(days_ago)
        if rng.random() < 0.12:
            continue  # skip ~12% of days
        meals = ["breakfast", "lunch", "dinner"]
        if rng.random() > 0.4:
            meals.append("snack")
        for meal in meals:
            template = rng.choice(MEAL_TEMPLATES[meal])
            cal, prot, carbs, fat, fiber = template["total"]
            entries.append(FoodEntry(
                date=d,
                meal=meal,
                raw_text=template["text"],
                items_json=template["items"],
                calories=cal + rng.gauss(0, 15),
                protein_g=prot + rng.gauss(0, 3),
                carbs_g=carbs + rng.gauss(0, 5),
                fat_g=fat + rng.gauss(0, 3),
                fiber_g=fiber + rng.gauss(0, 1),
                ai_model="claude-haiku-4-5",
                ai_confidence="high",
            ))
    FoodEntry.objects.bulk_create(entries)
    print(f"  Created {len(entries)} food entries")


def _seed_saved_meals():
    from workouts.models import SavedMeal
    saved = [
        SavedMeal(name="Post-workout protein shake", meal="snack",
                  calories=120, protein_g=25, carbs_g=3, fat_g=1, fiber_g=0, times_logged=18,
                  items_json=[{"name": "Protein shake", "qty": "1 scoop", "calories": 120,
                                "protein_g": 25, "carbs_g": 3, "fat_g": 1, "fiber_g": 0}]),
        SavedMeal(name="Chicken rice bowl", meal="lunch",
                  calories=494, protein_g=54, carbs_g=51, fat_g=6.5, fiber_g=6, times_logged=12,
                  items_json=MEAL_TEMPLATES["lunch"][0]["items"]),
        SavedMeal(name="Overnight oats", meal="breakfast",
                  calories=525, protein_g=36, carbs_g=84, fat_g=6, fiber_g=12, times_logged=9,
                  items_json=MEAL_TEMPLATES["breakfast"][2]["items"]),
        SavedMeal(name="Greek yogurt bowl", meal="breakfast",
                  calories=305, protein_g=21.5, carbs_g=40, fat_g=4, fiber_g=4, times_logged=7,
                  items_json=MEAL_TEMPLATES["breakfast"][0]["items"]),
        SavedMeal(name="Salmon quinoa bowl", meal="lunch",
                  calories=453, protein_g=47.5, carbs_g=30, fat_g=15.5, fiber_g=5, times_logged=5,
                  items_json=MEAL_TEMPLATES["lunch"][2]["items"]),
    ]
    SavedMeal.objects.bulk_create(saved)


def _seed_hunger(rng):
    from workouts.models import HungerCheck
    checks = []
    for days_ago in range(21):
        d = _d(days_ago)
        if rng.random() < 0.2:
            continue
        checks.append(HungerCheck(
            date=d, context="morning",
            hunger_level=rng.randint(3, 6),
        ))
        if rng.random() > 0.3:
            checks.append(HungerCheck(
                date=d, context="post_meal",
                hunger_level=rng.randint(1, 4),
                fullness_level=rng.randint(6, 9),
            ))
        if rng.random() > 0.5:
            checks.append(HungerCheck(
                date=d, context="evening",
                hunger_level=rng.randint(2, 7),
            ))
    HungerCheck.objects.bulk_create(checks)


def _seed_symptoms(rng, intervention):
    from workouts.models import SideEffectLog
    # Mild GI symptoms early in medication, tapering off
    logs = []
    for days_ago in range(85, 30, -1):
        if rng.random() > 0.15:
            continue
        severity = 1 if days_ago < 60 else rng.choice([1, 1, 2])
        symptom = rng.choice(["nausea", "nausea", "bloating", "dry_mouth"])
        logs.append(SideEffectLog(
            date=_d(days_ago),
            symptom=symptom,
            severity=severity,
            related_intervention=intervention,
            notes="Early dose escalation period" if days_ago > 60 else "",
        ))
    SideEffectLog.objects.bulk_create(logs)
    print(f"  Created {len(logs)} symptom logs")


def _seed_weekly_review():
    from workouts.models import WeeklyReview
    today = datetime.date.today()
    # Most recently completed Mon–Sun week
    days_since_monday = today.weekday()
    last_monday = today - datetime.timedelta(days=days_since_monday + 7)
    WeeklyReview.objects.create(
        week_start=last_monday,
        content=SAMPLE_WEEKLY_REVIEW,
        ai_model="claude-sonnet-4-6",
    )


# ── Canned AI text ────────────────────────────────────────────────────────────

SAMPLE_INSIGHTS = """**Training Volume**
Your weekly workout count has been consistent at 3–4 sessions, which is solid. Cycling dominates at 42% of volume, followed by strength at 28% and running at 22%.

**Performance Trends**
Cycling power output has trended up ~8% over 8 weeks — your Power Zone sessions are paying off. Running pace has improved by ~12 seconds/mile over the same window.

**Recovery Quality**
HRV averages 48ms with a weekly average of 50ms — both in a healthy range. Sleep score averages 74, with deep sleep consistently around 20% of total.

**Discipline Mix**
You're balancing cardio and strength well. Consider adding a second strength session per week to accelerate lean mass retention during your current calorie deficit."""

SAMPLE_PATTERN_INSIGHTS = """## Highest-confidence pattern
Higher cycling output on days following 7+ hours of sleep (avg +14W vs sleep-deprived days)

## Sleep → Performance correlation
When sleep exceeds 7h, next-day cycling power is 14W higher on average (n=31 pairs). The effect appears within 24h — same-day sleep quality matters more than 48h-prior sleep.

## Weight plateau breaker
Weight loss stalls ~3 weeks after each dose increase, then resumes. This matches a typical GLP-1 adaptation window. Current 1mg dose was started 34 days ago — plateau may be ending.

## Stress & hunger coupling
On high-stress days (avg stress >45), evening hunger checks average 1.8 points higher. Pre-logging dinner earlier on high-stress days correlates with staying within calorie targets.

## Strength training & body composition
Weeks with 2+ strength sessions show 0.3 lb/week better lean mass retention vs. single-session weeks, despite similar calorie intake."""

SAMPLE_BODY_COMMENTARY = """HEADLINE: Steady progress — body comp trending in the right direction
• Weight down ~4 lbs over 30 days, lean mass holding steady — this is the ratio you want
• Fat % trending from 29.5% → 27.8%, consistent with the 1mg dose and current deficit
• HRV has been stable this week (48–52ms range), suggesting recovery is keeping pace with training load
• Resting HR edging down slightly — a positive sign for aerobic adaptation"""

SAMPLE_NUTRITION_INSIGHTS = """## What's working
Your protein consistency is genuinely impressive — hitting 130–145g on 85% of logged days. This is protecting lean mass during the deficit.

Fiber intake averages 23g, just under the 25g target but close enough that it's not a concern most days.

## Where the friction is
Weekend calories run ~180 kcal higher than weekdays on average. This isn't a problem in itself, but it's erasing about half of the weekday deficit each week.

Post-workout meals on strength days tend to run lower on carbs — worth bumping these up to 30–40g to support recovery.

## Specific suggestions
- Add 100–150 kcal of carbs on strength training days (e.g., a banana + rice cake post-workout)
- On weekends, front-load protein at breakfast to naturally moderate lunch and dinner intake
- Cottage cheese as an evening snack is showing up in your saved meals — this is a great GLP-1-friendly protein source

## Watch list
Fiber dips below 15g on ~20% of days — these tend to correlate with days you skip vegetables at dinner. A simple rule: one fist-sized portion of non-starchy vegetables with lunch and dinner covers it."""

SAMPLE_WEEKLY_REVIEW = """## Weight & Body Composition
Down 0.8 lbs this week (172.4 → 171.6 lbs), which puts the 4-week trend at −3.2 lbs. Lean mass held steady at approximately 125 lbs — the combination of high protein and consistent strength training is working.

Fat percentage continues to trend down slowly (now 27.9%), which is the right direction.

## Nutrition
Logged 6 out of 7 days. Average intake: 1,672 kcal, 141g protein, 23g fiber. Protein target hit on 5/6 logged days. The one miss was Saturday — dinner out.

Calorie target (1,650) was respected on weekdays. Weekend overage was about 200 kcal — reasonable and within expected variance.

## Training
4 workouts this week: 2 cycling, 1 strength, 1 run.
- Best cycling session: 45 min Power Zone Endurance at 171W avg (above recent average of 165W)
- Run: 30 min at 9:24/mi, consistent with recent pacing
- Strength: Full Body with Adrian — completed all sets, felt strong

## Hunger & Symptoms
Morning hunger averaged 4.2/10 — lower than the prior 2 weeks, suggesting the 1mg dose is holding appetite suppression well. No GI symptoms logged this week (improvement from 2 weeks ago).

## One Thing Going Well
Sleep quality improved this week — 4 nights above 7.5h. HRV responded: averaged 51ms vs. 46ms the prior week.

## One Focus for Next Week
Add a second strength session. You have the recovery capacity (readiness scores averaging 74), and the data shows 2+ strength days/week supports better lean mass retention during your current deficit."""
