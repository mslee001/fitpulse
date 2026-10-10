"""
Seed the demo user with realistic, deterministic sample data — the data behind
the read-only demo (workouts/demo.py) — or a local demo database.

Usage (DATABASE_URL on the command line wins over the one in .env):
    DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py migrate
    DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py seed_demo [--user USERNAME]
    DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py changepassword demo

Seeds (and first clears) only the given user's data — by default a user named
"demo", created with an unusable password if missing. To keep a typo from wiping
a real account, it refuses superusers and anyone with Peloton, Withings or Google
Health connected, and asks you to type the username before replacing an existing
user's data (--no-input skips that question, not the refusals).

What it builds, all dated back from today (so re-running keeps the demo current;
sync_daily re-seeds the demo user twice a day): 90 days of wellness and body
data, ~11 weeks of varied workouts, a "Strength & Ride Split" program with five
weeks of passes, an AI 5K plan alongside it with three weeks done, a food log,
hunger checks, symptoms, two interventions with dose history and a saved Trends
analysis. Random numbers come from fixed seeds, so every run tells the same story.

AI text comes from workouts/demo/ai_examples.json — generated once against this
data with `manage.py generate_demo_ai` — falling back to the canned SAMPLE_* text
below for anything missing. The AI training plan exists only when that file has one.
"""

import datetime
import hashlib
import json
import random
import uuid
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

EXAMPLES_PATH = Path(__file__).resolve().parents[2] / "demo" / "ai_examples.json"
SPLIT_WEEKS = 5         # the split's cycle started on the Monday this many weeks back
PLAN_DONE_WEEKS = 3     # the AI plan started on the Monday this many weeks back

# The timeline is anchored to the Monday of the seeding week: each day's data is
# drawn at a fixed offset from it, the same random numbers whatever the weekday,
# and only days before "as of" (today; plus today's wellness and breakfast) are
# saved. So a Thursday is always the same Thursday of the story — weekday
# references in the saved AI text stay true — and a re-seed later in the week
# only reveals more of it.
_AS_OF = {"date": None}


def _today():
    return _AS_OF["date"] or timezone.localdate()


def _monday(day):
    return day - datetime.timedelta(days=day.weekday())


def _anchor():
    return _monday(_today())


def _day(offset):
    """The date `offset` days from this week's Monday (negative = earlier weeks)."""
    return _anchor() + datetime.timedelta(days=offset)


def _done(day):
    """Days before today have happened."""
    return day < _today()


def _dt_on(day, hour=8, minute=0):
    """A local (TIME_ZONE) moment on `day`, so workouts land on the day shown in the app."""
    return timezone.make_aware(datetime.datetime(day.year, day.month, day.day, hour, minute))


def _uid(rng):
    return uuid.UUID(int=rng.getrandbits(128)).hex


def load_ai_examples():
    """The saved AI responses (generate_demo_ai), or {} when there aren't any yet."""
    try:
        return json.loads(EXAMPLES_PATH.read_text())
    except FileNotFoundError:
        return {}


class Command(BaseCommand):
    help = "Seed the demo user (default: demo) with realistic, deterministic sample data."

    def add_arguments(self, parser):
        parser.add_argument("--user", metavar="USERNAME", default="demo",
                            help="User to seed (default: demo, created if missing)")
        parser.add_argument("--no-input", action="store_true", dest="no_input",
                            help="Replace an existing user's data without asking")

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username=options["user"]).first()
        if user is not None:
            self._check_safe_to_seed(user, ask=not options["no_input"])
        with transaction.atomic():   # a demo visitor never sees it half-seeded
            if user is None:
                user = User(username=options["user"])
                user.set_unusable_password()
                user.save()
            seed(user, load_ai_examples(), log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(
            f"\nDemo data seeded for user '{user.username}'.\n"
            f"Set its password with: manage.py changepassword {user.username}, or use "
            f"\"Explore the demo\" on the sign-in page."
        ))

    def _check_safe_to_seed(self, user, ask):
        """Seeding deletes the user's data first, so never let it land on a real account."""
        from workouts.models import (
            PelotonAuth, WithingsAuth, GoogleHealthAuth,
            CachedWorkout, DailyStats, FoodEntry, Intervention, BodyMeasurement,
        )
        name = user.username
        if user.is_superuser:
            raise CommandError(
                f"Refusing to seed '{name}': it's a superuser account. "
                f"Seed a separate test user instead, e.g. --user testuser."
            )
        connected = [label for label, M in (("Peloton", PelotonAuth),
                                            ("Withings", WithingsAuth),
                                            ("Google Health", GoogleHealthAuth))
                     if M.objects.filter(user=user).exists()]
        if connected:
            raise CommandError(
                f"Refusing to seed '{name}': it has {', '.join(connected)} connected, "
                f"so it looks like a real account. Seeding would delete its data."
            )
        counts = [(n, label) for n, label in (
            (CachedWorkout.objects.filter(user=user).count(), "workouts"),
            (DailyStats.objects.filter(user=user).count(), "days of stats"),
            (FoodEntry.objects.filter(user=user).count(), "food entries"),
            (Intervention.objects.filter(user=user).count(), "interventions"),
            (BodyMeasurement.objects.filter(user=user).count(), "weigh-ins"),
        ) if n]
        if not counts or not ask:
            return
        summary = ", ".join(f"{n} {label}" for n, label in counts)
        self.stdout.write(self.style.WARNING(
            f"'{name}' already has {summary}. Seeding deletes them and replaces them with demo data."
        ))
        if input(f"Type '{name}' to continue: ").strip() != name:
            raise CommandError("Cancelled — nothing was changed.")



def seed(user, examples, log=print, as_of=None):
    """Clear the user's data and seed it as of `as_of` (default today).
    `examples` is the saved-AI dict (load_ai_examples()); its training plan,
    if any, becomes the AI plan."""
    _AS_OF["date"] = as_of
    try:
        _seed(user, examples, log)
    finally:
        _AS_OF["date"] = None


def _seed(user, examples, log):
    from workouts.access import FEATURES, access_for
    from workouts.models import (
        AthleteProfile, BodyMeasurement, CachedWorkout, DailyStats, DoseChange, FoodEntry, HungerCheck,
        Integration, Intervention, NutritionProfile, PlanDraft, Program, SavedAnalysis, SavedMeal,
        SideEffectLog, UserSettings, WeeklyReview,
    )
    from workouts.nutrition import recompute_daily_nutrition

    log(f"Clearing existing data for {user.username}...")
    for M in [CachedWorkout, DailyStats, UserSettings, NutritionProfile, FoodEntry, SavedMeal, HungerCheck,
              SideEffectLog, Intervention, WeeklyReview, BodyMeasurement, Program, PlanDraft, SavedAnalysis,
              AthleteProfile]:
        M.objects.filter(user=user).delete()   # children (doses, program weeks/runs) go with them

    rng = random.Random(42)
    now = timezone.now()
    ago = lambda hours: now - datetime.timedelta(hours=hours)   # noqa: E731

    # ── Settings + saved AI text ──────────────────────────────────────────────
    nutrition_ai = examples.get("nutrition_insights") or {}
    UserSettings.objects.create(
        user=user, ftp=220,
        ai_insights=examples.get("insights") or SAMPLE_INSIGHTS, ai_insights_generated_at=ago(3),
        ai_pattern_insights=examples.get("pattern_insights") or SAMPLE_PATTERN_INSIGHTS,
        ai_pattern_insights_generated_at=ago(2),
        ai_body_commentary=examples.get("body_commentary") or SAMPLE_BODY_COMMENTARY,
        ai_body_commentary_generated_at=ago(1),
        ai_nutrition_insights=nutrition_ai.get("text") or SAMPLE_NUTRITION_INSIGHTS,
        ai_nutrition_insights_generated_at=ago(4),
        ai_nutrition_insights_range=nutrition_ai.get("range") or 30,
    )
    AthleteProfile.objects.create(
        user=user, running_experience="new", cycling_experience="experienced",
        strength_experience="intermediate", primary_disciplines=["cycling", "strength", "running"],
        training_focus="Getting back into running for a first 5K while keeping two strength days",
        coaching_tone="encouraging", saved_at=now,
    )
    NutritionProfile.objects.create(
        user=user, height_cm=165.0, age=34, biological_sex="female", activity_level="active", goal="loss",
        deficit_pct=20.0, protein_g_per_kg_lean=2.2, manual_calories=1650, manual_protein_g=140,
        manual_fiber_g=25,
    )

    # ── Interventions + dose history ──────────────────────────────────────────
    iv = Intervention.objects.create(
        user=user, name="Semaglutide", category="medication", start_date=_day(-84),
        expected_effects="Appetite reduction, gradual weight loss, improved blood sugar regulation",
        notes="Weekly injection, dose escalation protocol",
    )
    DoseChange.objects.create(intervention=iv, dose="0.25mg", start_date=_day(-84), end_date=_day(-57))
    DoseChange.objects.create(intervention=iv, dose="0.5mg", start_date=_day(-56), end_date=_day(-29))
    DoseChange.objects.create(intervention=iv, dose="1mg", start_date=_day(-28), end_date=None)
    iv2 = Intervention.objects.create(
        user=user, name="Creatine Monohydrate", category="supplement", start_date=_day(-56),
        expected_effects="Improved strength output, faster recovery, slight lean mass gain",
        notes="5g daily with post-workout shake",
    )
    DoseChange.objects.create(intervention=iv2, dose="5g/day", start_date=_day(-56), end_date=None)

    log("Seeding 90 days of wellness + body data...")
    _seed_daily_stats(user, rng)

    log("Seeding workouts...")
    _seed_workouts(user, rng)
    split = _seed_split(user, random.Random(44))

    log("Seeding food log (30 days)...")
    _seed_nutrition(user, rng)
    for offset in range(-28, 7):
        if _day(offset) <= _today():
            recompute_daily_nutrition(user, _day(offset))   # daily totals match the log
    _seed_saved_meals(user)
    _seed_hunger(user, rng)
    _seed_symptoms(user, rng, iv)

    # Last, with its own random numbers: the data above is the same with or without it
    # (generate_demo_ai seeds once without a plan, to make one).
    if examples.get("training_plan"):
        log("Seeding the AI training plan...")
        _seed_training_plan(user, random.Random(43), examples["training_plan"], split)

    # ── Saved AI text tied to days, weeks and the Trends analysis ────────────
    # Day analyses are keyed by offset from this week's Monday; the next-workout
    # card by today's weekday (it talks about "yesterday" and "today").
    for offset, text in (examples.get("day_analysis") or {}).items():
        if _done(_day(int(offset))):
            DailyStats.objects.filter(user=user, date=_day(int(offset))).update(
                ai_day_analysis=text, ai_day_generated_at=now)
    next_workout = examples.get("next_workout") or {}
    if isinstance(next_workout, dict):
        next_workout = next_workout.get(str(_today().weekday()))
    if next_workout:
        DailyStats.objects.filter(user=user, date=_today()).update(
            ai_next_workout=next_workout, ai_next_workout_generated_at=now)
    reviews = examples.get("weekly_reviews") or [{"weeks_ago": 1, "content": SAMPLE_WEEKLY_REVIEW}]
    for r in reviews:
        WeeklyReview.objects.create(user=user, week_start=_anchor() - datetime.timedelta(weeks=r["weeks_ago"]),
                                    content=r["content"], ai_model=r.get("ai_model") or "claude-sonnet-4-6")
    analysis = demo_analysis(user)
    interpretation = examples.get("intervention_analysis") or {}
    SavedAnalysis.objects.create(
        user=user, label=analysis["label"], intervention=analysis["intervention"],
        before_start=analysis["before_start"], before_end=analysis["before_end"],
        after_start=analysis["after_start"], after_end=analysis["after_end"],
        window_days=analysis["window_days"], weight_goal="loss", metrics_json=analysis["metrics_json"],
        ai_interpretation=interpretation.get("text", ""),
        ai_model=interpretation.get("ai_model") or "claude-sonnet-4-6",
    )

    # Everything but the stats chat (a demo visitor would just be told it's off), set up.
    access = access_for(user)
    access.features, access.ai_enabled = [s for s in FEATURES if s != "ai_chat"], True
    access.must_change_password = False
    access.onboarding_completed_at = now
    access.save()
    Integration.ensure_for_user(user)


def demo_analysis(user):
    """The saved Trends analysis: the Semaglutide step from 0.5 mg to 1 mg, the
    four weeks either side (ending last Sunday). Shared with generate_demo_ai so its AI interpretation
    reads the same numbers."""
    from workouts.analysis import run_intervention_analysis
    from workouts.models import Intervention
    from workouts.nutrition import get_nutrition_gap
    before_start, before_end, after_start, after_end = _day(-56), _day(-29), _day(-28), _day(-1)
    result = run_intervention_analysis(user, before_start=before_start, before_end=before_end,
                                       after_start=after_start, after_end=after_end, weight_goal="loss")
    result["nutrition_gaps"] = {"before": get_nutrition_gap(user, before_start, before_end),
                                "after": get_nutrition_gap(user, after_start, after_end)}
    raw = dict(result)
    for key in ("before_start", "before_end", "after_start", "after_end"):
        if hasattr(result.get(key), "isoformat"):
            result[key] = result[key].isoformat()
    return {"label": "Semaglutide 0.5 mg → 1 mg", "intervention": Intervention.objects.for_user(user)
            .filter(name="Semaglutide").first(), "before_start": before_start, "before_end": before_end,
            "after_start": after_start, "after_end": after_end, "window_days": 28,
            "metrics_json": json.loads(json.dumps(result, default=str)), "result": raw}


def _seed_daily_stats(user, rng):
    from workouts.models import DailyStats
    base_weight = 172.0
    base_hrv = 48.0
    daily_stats = []
    for i, offset in enumerate(range(-91, 7)):   # 13 weeks back through the end of this week
        d = _day(offset)
        # gradual weight loss with noise
        weight = base_weight - i * 0.045 + rng.gauss(0, 0.4)
        fat_pct = 29.5 - i * 0.04 + rng.gauss(0, 0.3)
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

        readiness = max(30, min(99, int(rng.gauss(72, 12))))
        readiness_label = "Ready" if readiness >= 70 else "Moderate" if readiness >= 50 else "Low"

        row = DailyStats(
            user=user, date=d,
            weight_lb=round(weight, 1), fat_mass_lb=round(fat_mass, 1), fat_free_mass_lb=round(lean, 1),
            muscle_mass_lb=round(muscle, 1), fat_ratio_pct=round(fat_pct, 1), weight_count=1,
            weight_synced_at=timezone.now(),
            hrv_last_night=round(hrv, 1), hrv_weekly_avg=round(hrv + rng.gauss(0, 2), 1),
            hrv_status=rng.choice(["BALANCED", "BALANCED", "BALANCED", "UNBALANCED", "POOR"]),
            hrv_min=int(hrv - rng.uniform(5, 15)), hrv_max=int(hrv + rng.uniform(5, 15)),
            resting_hr=rhr,
            sleep_score=int(rng.gauss(74, 9)), sleep_seconds=sleep_s, sleep_deep_seconds=sleep_deep,
            sleep_rem_seconds=sleep_rem, sleep_light_seconds=sleep_light,
            body_battery_high=bb_high, body_battery_low=bb_low, body_battery_start=bb_high,
            body_battery_end=bb_low, body_battery_charge=int(rng.gauss(45, 10)),
            body_battery_drain=int(rng.gauss(52, 12)),
            steps=steps, steps_goal=8000, active_calories=int(rng.gauss(480, 80)),
            total_calories=int(rng.gauss(2050, 120)), bmr_calories=1520,
            floors_climbed=int(rng.gauss(8, 3)), floors_climbed_goal=10,
            stress_avg=stress, stress_max=stress + int(rng.gauss(20, 5)),
            stress_rest_minutes=int(rng.gauss(320, 40)), stress_low_minutes=int(rng.gauss(420, 60)),
            stress_medium_minutes=int(rng.gauss(180, 30)), stress_high_minutes=int(rng.gauss(60, 20)),
            training_readiness_score=readiness, training_readiness_label=readiness_label,
            training_status=rng.choice(["Productive", "Productive", "Maintaining", "Unproductive", "Recovery"]),
            training_load=round(rng.gauss(340, 80), 1),
            vo2_max_running=round(rng.gauss(42.5, 0.5), 1), fitness_age=32,
            respiration_avg=round(rng.gauss(15.2, 0.8), 1), spo2_sleep_avg=round(rng.gauss(96.5, 0.5), 1),
            synced_at=timezone.now(),
        )
        if d <= _today():   # values are drawn for every day; only up to today is saved
            daily_stats.append(row)
    DailyStats.objects.bulk_create(daily_stats)


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



def _get_ride_id(title):
    """A stable class id per title, so class history, Compare and the programs line up across re-seeds."""
    return hashlib.md5(f"fitpulse-demo:{title}".encode()).hexdigest()


def _effort_points(minutes, hr, base_hr, per_min, spread):
    """Peloton-like effort points (heart-rate based): about `per_min` a minute at
    `base_hr`, more or less with a harder or easier session."""
    return round(minutes * max(0.2, per_min + (hr - base_hr) / spread), 1)


def _perf_graph_cycling(avg_watts, rng, dur, hr=155):
    n = max(1, dur // 10)   # every_n=10s
    values = [round(avg_watts + rng.gauss(0, 15)) for _ in range(n)]
    return {
        "source": "peloton", "every_n": 10,
        "metrics_by_slug": {
            "output": {"display_name": "Output", "display_unit": "W",
                       "values": values, "average_value": avg_watts, "max_value": max(values)},
        },
        "effort_zones": {"total_effort_points": _effort_points(dur / 60, hr, 155, 1.4, 25)},
        "summaries": {"output": {"display_name": "Output", "display_unit": "kJ",
                                 "value": round(avg_watts * dur / 1000)}},
        "average_summaries": {}, "segments": [], "splits": [], "muscle_groups": [],
    }


def _perf_graph_running(avg_pace_s, hr, rng, dur):
    n = max(1, dur // 10)
    values = [round(avg_pace_s / 60 + rng.gauss(0, 0.1), 2) for _ in range(n)]
    hr_vals = [round(hr + rng.gauss(0, 4)) for _ in range(n)]
    return {
        "source": "peloton", "every_n": 10,
        "metrics_by_slug": {
            "pace": {"display_name": "Pace", "display_unit": "min/mi",
                     "values": values, "average_value": round(avg_pace_s / 60, 2), "max_value": None},
            "heart_rate": {"display_name": "Heart Rate", "display_unit": "bpm",
                           "values": hr_vals, "average_value": hr, "max_value": max(hr_vals)},
        },
        "effort_zones": {"total_effort_points": _effort_points(dur / 60, hr, 158, 1.6, 20)},
        "summaries": {"distance": {"display_name": "Distance", "display_unit": "mi",
                                   "value": round(dur / avg_pace_s, 2)}},
        "average_summaries": {"avg_pace": {"display_name": "Avg Pace", "display_unit": "min/mi",
                                           "value": round(avg_pace_s / 60, 2)}},
        "segments": [], "splits": [], "muscle_groups": [],
    }


_POOLS = {"cycling": CYCLING_TITLES, "running": RUNNING_TITLES, "strength": STRENGTH_TITLES, "yoga": YOGA_TITLES}
_DISPLAY = {"cycling": "Cycling", "running": "Running", "strength": "Strength", "yoga": "Yoga",
            "stretching": "Stretching", "walking": "Walking", "meditation": "Meditation"}


def _make_workout(user, rng, disc, dt, title=None, instructor=None, dur=None, ride_id=None, pace_s=None):
    """One unsaved CachedWorkout of a discipline. Title/instructor default to one
    from that discipline's pool; ride_id defaults to the title's stable id."""
    from workouts.models import CachedWorkout
    if title is None:
        title, instructor = rng.choice(_POOLS.get(disc, YOGA_TITLES))
    common = dict(user=user, workout_id=_uid(rng), ride_id=ride_id or _get_ride_id(title), title=title,
                  discipline=disc, fitness_discipline_display=_DISPLAY.get(disc, disc.title()),
                  workout_type="class", instructor_name=instructor or "", created_at=dt, source="peloton",
                  detail_synced_at=timezone.now())

    if disc == "cycling":
        dur = dur or rng.choice([20 * 60, 30 * 60, 45 * 60, 60 * 60])
        avg_w = int(rng.gauss(165, 20))
        hr = int(rng.gauss(155, 8))
        return CachedWorkout(
            **common, duration_seconds=dur, calories=int(rng.gauss(420, 60) * dur / 2700),
            heart_rate_avg=hr, heart_rate_max=hr + rng.randint(10, 25),
            output_watts=int(avg_w * dur / 1000), avg_watts=avg_w,
            avg_cadence=int(rng.gauss(82, 5)), avg_resistance=round(rng.gauss(42, 5), 1),
            leaderboard_rank=rng.randint(800, 15000), total_leaderboard_users=rng.randint(20000, 80000),
            ftp=220, performance_graph_json=_perf_graph_cycling(avg_w, rng, dur, hr), is_pr=rng.random() < 0.08,
            hr_z1_seconds=int(dur * 0.05), hr_z2_seconds=int(dur * 0.20), hr_z3_seconds=int(dur * 0.35),
            hr_z4_seconds=int(dur * 0.30), hr_z5_seconds=int(dur * 0.10),
        )
    if disc == "running":
        dur = dur or rng.choice([20 * 60, 30 * 60, 45 * 60])
        pace_s = int(pace_s or rng.gauss(570, 30))  # ~9:30/mi
        hr = int(rng.gauss(158, 7))
        cadence = int(rng.gauss(168, 4))
        return CachedWorkout(
            **common, duration_seconds=dur, calories=int(rng.gauss(340, 40) * dur / 1800),
            heart_rate_avg=hr, heart_rate_max=hr + rng.randint(8, 20),
            avg_pace_seconds=pace_s, distance_miles=round(dur / pace_s, 2),
            avg_speed_mph=round(3600 / pace_s, 1), avg_cadence=cadence, run_cadence_avg=cadence,
            stride_length_avg=round(rng.gauss(108, 5), 1),
            vertical_oscillation_avg=round(rng.gauss(8.2, 0.6), 1),
            vertical_ratio_avg=round(rng.gauss(8.8, 0.5), 1),
            ground_contact_time_avg=round(rng.gauss(262, 12), 1),
            performance_graph_json=_perf_graph_running(pace_s, hr, rng, dur), is_pr=rng.random() < 0.06,
            hr_z1_seconds=int(dur * 0.03), hr_z2_seconds=int(dur * 0.15), hr_z3_seconds=int(dur * 0.30),
            hr_z4_seconds=int(dur * 0.35), hr_z5_seconds=int(dur * 0.17),
        )
    if disc == "strength":
        dur = dur or rng.choice([20 * 60, 30 * 60, 45 * 60])
        hr = int(rng.gauss(142, 8))
        effort = round(rng.gauss(72, 8), 1)
        return CachedWorkout(
            **common, duration_seconds=dur, calories=int(rng.gauss(220, 30) * dur / 1800),
            heart_rate_avg=hr, heart_rate_max=hr + rng.randint(10, 20),
            effort_score=effort, average_effort_score=effort, exercise_sets_json=_make_exercise_sets(rng),
            performance_graph_json={"source": "peloton", "effort_zones": {
                "total_effort_points": _effort_points(dur / 60, hr, 142, 0.9, 40)
            }, "metrics_by_slug": {}, "summaries": {}, "average_summaries": {},
                "segments": [], "splits": [], "muscle_groups": [
                    {"name": "Glutes", "percentage": 0.28},
                    {"name": "Quads", "percentage": 0.22},
                    {"name": "Core", "percentage": 0.18},
                ]},
            is_pr=False,
            hr_z1_seconds=int(dur * 0.10), hr_z2_seconds=int(dur * 0.30), hr_z3_seconds=int(dur * 0.35),
            hr_z4_seconds=int(dur * 0.18), hr_z5_seconds=int(dur * 0.07),
        )
    # yoga, stretching, walking, …: light sessions
    dur = dur or rng.choice([15 * 60, 20 * 60, 30 * 60])
    hr = int(rng.gauss(105 if disc == "walking" else 95, 8))
    return CachedWorkout(
        **common, duration_seconds=dur, calories=int(rng.gauss(85, 15) * dur / 1200), heart_rate_avg=hr,
        performance_graph_json={"source": "peloton", "metrics_by_slug": {}, "summaries": {},
                                "average_summaries": {}, "effort_zones": {}, "segments": [], "splits": [],
                                "muscle_groups": []},
    )


def _seed_workouts(user, rng):
    """2–4 varied workouts a week, from 13 weeks back until the programs take over."""
    from workouts.models import CachedWorkout
    schedule = []
    for week in range(-13, -SPLIT_WEEKS):
        for weekday in rng.sample(range(7), k=rng.randint(2, 4)):
            disc = rng.choices(["cycling", "running", "strength", "yoga"], weights=[40, 25, 25, 10])[0]
            schedule.append((_day(week * 7 + weekday), disc))
    workouts = [_make_workout(user, rng, disc, _dt_on(day, rng.randint(6, 18), rng.randint(0, 59)))
                for day, disc in sorted(schedule)]
    CachedWorkout.objects.bulk_create(workouts)
    print(f"  Created {len(workouts)} workouts")


# ── Programs: a weekly split and an AI training plan ─────────────────────────

SPLIT_SLOTS = [   # (day, title, instructor, discipline, minutes); title None = any class of the discipline
    (1, "30 min Full Body Strength", "Adrian Williams", "strength", 30),
    (3, "45 min Climb Ride", "Robin Arzón", "cycling", 45),
    (5, "30 min Lower Body", "Adrian Williams", "strength", 30),
    (6, None, None, "yoga", None),
]


def _seed_split(user, rng):
    """A weekly split whose cycle started SPLIT_WEEKS Mondays ago: one pass per
    week, sessions sometimes a day late and now and then skipped."""
    from workouts.models import Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek
    program = Program.objects.create(
        user=user, name="Strength & Ride Split", slug="strength-ride-split", kind="split",
        match_strategy="ride_ids", instructor="Adrian Williams",
        description="Two strength days, a climb ride and a yoga flow every week.",
    )
    week = ProgramWeek.objects.create(program=program, number=1)
    slots = []
    for order, (day, title, instructor, disc, minutes) in enumerate(SPLIT_SLOTS):
        slots.append((ProgramSlot.objects.create(
            week=week, day=day, order=order, title=title or "Yoga (any class)", discipline=disc,
            duration_min=minutes, peloton_ride_id=_get_ride_id(title) if title else "",
            match_discipline="" if title else disc,
        ), title, instructor, disc, minutes))
    start = _anchor() - datetime.timedelta(weeks=SPLIT_WEEKS)
    run = ProgramRun.objects.create(program=program, start_date=start, label="Cycle 1")
    for n in range(SPLIT_WEEKS + 1):
        monday = start + datetime.timedelta(weeks=n)
        rw = RunWeek.objects.create(run=run, program_week=week, sequence=n + 1)
        for slot, title, instructor, disc, minutes in slots:
            day = monday + datetime.timedelta(days=slot.day - 1 + (1 if rng.random() < 0.2 else 0))
            skipped = rng.random() < 0.08
            w = _make_workout(user, rng, disc, _dt_on(day, rng.randint(6, 18), rng.randint(0, 59)),
                              title=title, instructor=instructor, dur=minutes * 60 if minutes else None)
            if skipped or not _done(day):   # drawn either way, so later days don't depend on today
                continue
            w.save()
            ProgramWorkout.objects.create(run_week=rw, slot=slot, workout=w,
                                          matched_by="ride_id" if title else "discipline")
    return program


def _ensure_catalog_rows(classes):
    """The plan's classes in the (global) catalog, for difficulty and links. Only
    adds missing rows — real catalog rows are never changed."""
    from workouts.models import PelotonClass
    for ride_id, c in classes.items():
        PelotonClass.objects.get_or_create(ride_id=ride_id, defaults={
            "title": c.get("title", ""), "discipline": c.get("discipline", ""),
            "class_type_id": c.get("class_type_id", ""), "instructor_name": c.get("instructor_name", ""),
            "duration_seconds": c.get("duration_seconds") or 0,
            "difficulty_estimate": c.get("difficulty_estimate"),
            "original_air_time": c.get("original_air_time") or timezone.now(),
            "last_seen_at": timezone.now(), "is_available": True,
        })


def _seed_training_plan(user, rng, plan, companion):
    """The saved AI plan (generate_demo_ai) as a program that started
    PLAN_DONE_WEEKS Mondays ago, alongside the split — so its run grid has
    finished weeks (rated), the current week and what's ahead."""
    from workouts.models import Program, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek
    from workouts.run_targets import zone_pace_seconds
    from workouts.training_plans import PACE_ZONES, pace_summary, race_slot_data, slot_key, slot_notes

    inputs, spec = dict(plan["inputs"]), plan["spec"]
    picks, classes = plan.get("picks") or {}, plan.get("classes") or {}
    start = _anchor() - datetime.timedelta(weeks=PLAN_DONE_WEEKS)
    race = (start + datetime.timedelta(weeks=inputs["weeks"] - 1, days=inputs["race_weekday"] - 1)
            if inputs.get("race_weekday") else None)
    end = race or start + datetime.timedelta(weeks=inputs["weeks"], days=-1)
    inputs.update(start_date=start.isoformat(), race_date=race.isoformat() if race else "",
                  companion_program_id=companion.pk if inputs.get("mode") == "alongside" else None)
    _ensure_catalog_rows(classes)

    name = plan.get("name") or "5K Plan"
    program = Program.objects.create(user=user, name=name, slug=slugify(name), kind="plan",
                                     match_strategy="ride_ids", description=spec.get("summary", ""))
    for wk in spec["weeks"]:
        pw = ProgramWeek.objects.create(program=program, number=wk["number"])
        for s in wk["slots"]:
            c = classes.get((picks.get(slot_key(wk["number"], s)) or {}).get("ride_id")) or {}
            minutes = round(c["duration_seconds"] / 60) if c.get("duration_seconds") else s["duration_min"]
            ProgramSlot.objects.create(
                week=pw, day=s["day"], order=s["order"],
                title=c.get("title") or f"{s.get('class_type', s['discipline'].title())} · {minutes} min",
                discipline="strength" if s["discipline"] == "pilates" else s["discipline"],
                duration_min=minutes, peloton_ride_id=c.get("ride_id", ""), optional=bool(s.get("optional")),
                notes=slot_notes(s, c.get("instructor_name", "")), spec_json=s,
            )
        if race and inputs.get("race_week") == wk["number"]:
            r = race_slot_data(inputs)
            ProgramSlot.objects.create(week=pw, day=r["day"], order=r["order"], title=r["title"],
                                       discipline=r["discipline"], optional=True, notes=r["notes"])
    program.goal_json = {
        "goal": inputs["goal"], "race_date": inputs.get("race_date") or "", "end_date": end.isoformat(),
        "target_time": inputs.get("target_time"), "start_date": inputs["start_date"], "mode": inputs["mode"],
        "companion_program_id": inputs.get("companion_program_id"), "draft_id": None,
        "generated_at": plan.get("generated_at", ""), "model": plan.get("ai_model", ""),
        "level": inputs.get("level"), "weeks": inputs["weeks"],
        "pace_level": (inputs.get("pace") or {}).get("level"),
        "goal_pace_s": (inputs.get("pace") or {}).get("goal_pace_s"),
        "pace_summary": pace_summary(inputs, spec),
        "weeks_info": {str(wk["number"]): {"phase": wk.get("phase", ""), "focus": wk.get("focus", "")}
                       for wk in spec["weeks"]},
    }
    program.save(update_fields=["goal_json"])

    run = ProgramRun.objects.create(program=program, start_date=start)
    level = (inputs.get("pace") or {}).get("level") or 5
    notes = {1: (4, "Easy start. The intervals felt short."), 2: (5, "Legs tired after the climb ride."),
             3: (6, "Long run was the hardest yet, but finished strong.")}
    for pw in program.weeks.all():
        rw = RunWeek.objects.create(run=run, program_week=pw, sequence=pw.number)
        if pw.number in notes and pw.number <= PLAN_DONE_WEEKS:
            rw.rpe, rw.note = notes[pw.number]
            rw.rated_at = timezone.now()
            rw.save(update_fields=["rpe", "note", "rated_at"])
        for slot in pw.slots.all():
            day = start + datetime.timedelta(weeks=pw.number - 1, days=(slot.day or 1) - 1)
            if not slot.peloton_ride_id:
                continue
            skipped = rng.random() < (0.5 if slot.optional else 0.1)
            pace_s = None
            zone = (slot.spec_json or {}).get("pace_zone")
            if slot.discipline == "running" and zone in PACE_ZONES:
                pace_s = (zone_pace_seconds(level, PACE_ZONES.index(zone)) or 570) * rng.uniform(0.98, 1.04)
            instructor = (classes.get(slot.peloton_ride_id) or {}).get("instructor_name", "")
            w = _make_workout(user, rng, slot.discipline or "running", _dt_on(day, rng.randint(6, 9), rng.randint(0, 59)),
                              title=slot.title, instructor=instructor, dur=(slot.duration_min or 20) * 60,
                              ride_id=slot.peloton_ride_id, pace_s=pace_s)
            if skipped or not _done(day):
                continue
            w.save()
            ProgramWorkout.objects.create(run_week=rw, slot=slot, workout=w, matched_by="ride_id")
    return program


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


def _seed_nutrition(user, rng):
    from workouts.models import FoodEntry
    entries = []
    for offset in range(-28, 7):   # four weeks back through the end of this week
        d = _day(offset)
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
    # Drawn for every day; saved for the days before today, plus today's breakfast.
    entries = [e for e in entries if _done(e.date) or (e.date == _today() and e.meal == "breakfast")]
    for e in entries:
        e.user = user
    FoodEntry.objects.bulk_create(entries)
    print(f"  Created {len(entries)} food entries")


def _seed_saved_meals(user):
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
    for m in saved:
        m.user = user
    SavedMeal.objects.bulk_create(saved)


def _seed_hunger(user, rng):
    from workouts.models import HungerCheck
    checks = []
    for offset in range(-14, 7):
        d = _day(offset)
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
    checks = [c for c in checks if _done(c.date) or (c.date == _today() and c.context == "morning")]
    for c in checks:
        c.user = user
    HungerCheck.objects.bulk_create(checks)


def _seed_symptoms(user, rng, intervention):
    from workouts.models import SideEffectLog
    # Mild GI symptoms early in medication, tapering off
    logs = []
    for offset in range(-84, -28):   # the first two doses
        if rng.random() > 0.15:
            continue
        early = offset < -56
        severity = rng.choice([1, 1, 2]) if early else 1
        symptom = rng.choice(["nausea", "nausea", "bloating", "dry_mouth"])
        logs.append(SideEffectLog(
            date=_day(offset),
            symptom=symptom,
            severity=severity,
            related_intervention=intervention,
            notes="Early dose escalation period" if early else "",
        ))
    for log in logs:
        log.user = user
    SideEffectLog.objects.bulk_create(logs)
    print(f"  Created {len(logs)} symptom logs")


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
