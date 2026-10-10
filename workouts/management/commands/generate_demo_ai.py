"""
Generate the demo's saved AI examples, once, into workouts/demo/ai_examples.json.

Runs the app's real prompts against the seeded demo user — the AI training plan
(Sonnet, needs the class catalog, so run it against the production database),
then, with that plan seeded, analytics/pattern/nutrition insights, body
commentary, three weekly reviews and the saved Trends analysis's
interpretation, and — once per weekday, with "today" set to that day — the
next-workout card and the day analyses for the past week — and writes every
reply to the JSON file. seed_demo loads that file on every re-seed; the demo
itself never calls the AI. Commit the file. Roughly $0.50 of API usage.

The demo's timeline is anchored to the seeding week's Monday (seed_demo), so
weekday names in this text stay true; calendar dates don't (the weeks move).
Every prompt is asked to avoid them, and a Haiku pass rewrites any that slip
through into weekday or relative wording.

Usage:
    venv/bin/python3 manage.py generate_demo_ai              # plan + everything else
    venv/bin/python3 manage.py generate_demo_ai --skip-plan  # keep the saved plan, redo the text
"""
import datetime
import json
import re
from contextlib import contextmanager
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workouts.management.commands.seed_demo import EXAMPLES_PATH, _monday, demo_analysis, load_ai_examples, seed

DATE_NEUTRAL = ("\n\nThis text will be shown as a saved example in a demo whose weeks move forward. Refer to days "
                "by weekday or relatively (\"Tuesday\", \"yesterday\", \"last week\"); never write calendar dates "
                "such as \"Oct 1\" or \"10/08\".")
PLAN_NAME = "5K in 8 Weeks"
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
# "Oct 1", "Thu 10/08" / "(Sat 9/26)", "2026-10-01" — a bare "7/7" is a count ("7/7 days"), not a date.
CALENDAR_DATE = re.compile(rf"\b(?:{_MONTHS})\.? ?\d{{1,2}}\b"
                           r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,? \(?\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
                           r"|\b\d{4}-\d{2}-\d{2}\b")


@contextmanager
def _as_of(day):
    """Make the app's "today" `day` (prompts, caches and recent-workout windows)."""
    with mock.patch("django.utils.timezone.localdate", return_value=day):
        yield


@contextmanager
def _date_neutral_prompts():
    """Append DATE_NEUTRAL to the system prompt of every AI request made inside."""
    from workouts import llm
    real = llm._send

    def send(prompt, *, system=None, **kw):
        return real(prompt, system=(system or "") + DATE_NEUTRAL, **kw)
    with mock.patch.object(llm, "_send", send):
        yield


class Command(BaseCommand):
    help = "Generate the read-only demo's saved AI examples (real API calls) into workouts/demo/ai_examples.json."

    def add_arguments(self, parser):
        parser.add_argument("--skip-plan", action="store_true", help="Keep the saved training plan; redo the rest.")

    def handle(self, *args, **opts):
        from workouts.demo import allow_ai_generation
        user = get_user_model().objects.filter(username=settings.DEMO_USERNAME, is_superuser=False).first()
        if user is None:
            raise CommandError(f"No demo user '{settings.DEMO_USERNAME}' yet. Run `manage.py seed_demo` first.")
        examples = load_ai_examples()

        with allow_ai_generation(), _date_neutral_prompts():
            if not opts["skip_plan"]:
                examples["training_plan"] = self._plan(user, examples)
            if examples.get("training_plan"):
                self._undate_plan(user, examples["training_plan"])
                self._save(examples)
            with transaction.atomic():
                seed(user, examples, log=lambda *_: None)   # plan + its completed weeks in place
            examples.update(self._texts(user))
            examples.update(self._by_weekday(user, examples))
        examples["generated_at"] = timezone.now().isoformat(timespec="seconds")
        self._save(examples)

        with transaction.atomic():
            seed(user, examples, log=lambda *_: None)
        self.stdout.write(self.style.SUCCESS(f"Saved {EXAMPLES_PATH} and re-seeded '{user.username}'. Commit the file."))

    def _save(self, examples):
        EXAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXAMPLES_PATH.write_text(json.dumps(examples, indent=1, ensure_ascii=False, sort_keys=True) + "\n")

    def _plan(self, user, examples):
        """A 5K plan alongside the demo's split, made the way the form makes one."""
        from django.http import QueryDict
        from workouts.models import PelotonClass, PlanDraft, Program
        from workouts.training_plans import _generate, clean_inputs

        self.stdout.write("Training plan (Sonnet)…")
        with transaction.atomic():
            seed(user, {k: v for k, v in examples.items() if k != "training_plan"}, log=lambda *_: None)
        split = Program.objects.for_user(user).get(kind="split")
        start = _monday(timezone.localdate()) + datetime.timedelta(weeks=1)
        post = QueryDict(mutable=True)
        post.update({"goal": "5k", "start_date": start.isoformat(),
                     "race_date": (start + datetime.timedelta(weeks=7, days=5)).isoformat(),
                     "target_time": "29:30", "long_day": "7", "max_weekday_min": "45", "max_weekend_min": "60",
                     "setting": "tread", "mode": "alongside", "companion_program_id": str(split.pk),
                     "level": "auto", "pace_level": "5", "mobility": "1",
                     "notes": "First 5K. Keep the split's strength days; runs on the other days."})
        post.setlist("days", ["2", "4", "7"])
        inputs, errors = clean_inputs(user, post)
        if errors:
            raise CommandError(f"Plan inputs were rejected: {errors}")
        draft = PlanDraft.objects.create(user=user, inputs_json=inputs, status="generating")
        _generate(draft.pk)
        draft.refresh_from_db()
        if draft.status != "ready":
            raise CommandError(f"The plan didn't generate: {draft.error}")
        picks = draft.picks_json or {}
        ids = {p.get("ride_id") for p in picks.values() if p.get("ride_id")}
        classes = {c.ride_id: {"ride_id": c.ride_id, "title": c.title, "instructor_name": c.instructor_name,
                               "duration_seconds": c.duration_seconds, "discipline": c.discipline,
                               "class_type_id": c.class_type_id, "difficulty_estimate": c.difficulty_estimate,
                               "original_air_time": c.original_air_time.isoformat()}
                   for c in PelotonClass.objects.filter(ride_id__in=ids)}
        # Only the picked classes; alternates point at catalog rows a demo database may not have.
        picks = {k: {"ride_id": p.get("ride_id")} for k, p in picks.items()}
        plan = {"name": PLAN_NAME, "inputs": inputs, "spec": draft.spec_json, "picks": picks, "classes": classes,
                "warnings": draft.warnings, "ai_model": draft.ai_model,
                "generated_at": timezone.now().isoformat(timespec="seconds")}
        draft.delete()
        return plan

    def _undate_plan(self, user, plan):
        """The plan's own words (summary, pace guidance, week focus, session purposes)
        without calendar dates — the seeded plan is re-dated, its race day moves."""
        today = timezone.localdate()
        spec = plan["spec"]
        for key in ("summary", "pace_guidance"):
            spec[key] = self._undate(user, spec.get(key, ""), today)
        for wk in spec.get("weeks", []):
            wk["focus"] = self._undate(user, wk.get("focus", ""), today)
            for slot in wk.get("slots", []):
                slot["purpose"] = self._undate(user, slot.get("purpose", ""), today)

    def _texts(self, user):
        from workouts import ai, llm
        from workouts.models import UserSettings

        out = {}
        say = self.stdout.write

        say("Analytics insights…")
        prompt = ("Here is my workout data from Peloton and Garmin Connect for the past year:\n\n"
                  + json.dumps(ai._build_insights_summary(user), indent=2) + ai.INSIGHTS_PROMPT_SUFFIX)
        out["insights"] = llm.call(prompt, user=user, feature="ai_training_insights", model=llm.SONNET,
                                   max_tokens=2000, system=ai.build_insights_system(user), timeout=120)

        say("Pattern insights…")
        out["pattern_insights"] = llm.call(ai._build_pattern_insights_prompt(user), user=user,
                                           feature="ai_pattern_insights", model=llm.SONNET, max_tokens=2400,
                                           timeout=120)

        say("Nutrition insights…")
        out["nutrition_insights"] = {"range": 30, "text": llm.call(
            ai._build_nutrition_insights_prompt(user, 30), user=user, feature="ai_nutrition_insights",
            model=llm.SONNET, max_tokens=1800, timeout=120)}

        say("Body commentary…")
        UserSettings.objects.filter(user=user).update(ai_body_commentary=None, ai_body_commentary_generated_at=None)
        out["body_commentary"] = ai._get_or_generate_body_commentary(user, force=True)

        say("Weekly reviews…")
        out["weekly_reviews"] = []
        for weeks_ago in (1, 2, 3):
            week_start = _monday(timezone.localdate()) - datetime.timedelta(weeks=weeks_ago)
            out["weekly_reviews"].append({"weeks_ago": weeks_ago, "ai_model": llm.SONNET, "content": llm.call(
                ai._build_weekly_review_prompt(user, week_start), user=user, feature="ai_weekly_review",
                model=llm.SONNET, max_tokens=1600, timeout=120)})

        say("Trends interpretation…")
        a = demo_analysis(user)
        result = dict(a["result"], before_start=a["before_start"], before_end=a["before_end"],
                      after_start=a["after_start"], after_end=a["after_end"])
        out["intervention_analysis"] = {"ai_model": llm.SONNET, "text": ai._generate_intervention_interpretation(
            user, result, intervention=a["intervention"],
            interventions_context_str=ai._interventions_context(user, a["before_start"], a["after_end"]),
            nutrition_gaps=a["metrics_json"].get("nutrition_gaps"))}
        today = timezone.localdate()
        for key in ("insights", "pattern_insights", "body_commentary"):
            out[key] = self._undate(user, out[key], today)
        out["nutrition_insights"]["text"] = self._undate(user, out["nutrition_insights"]["text"], today)
        out["intervention_analysis"]["text"] = self._undate(user, out["intervention_analysis"]["text"], today)
        for r in out["weekly_reviews"]:
            r["content"] = self._undate(user, r["content"], today)
        return out

    def _by_weekday(self, user, examples):
        """The texts that speak from "today": for each weekday of this week, seed the
        demo as of that day and generate the next-workout card and yesterday's
        analysis (on Monday also the rest of last week's). Keys: next_workout by
        weekday (0 = Monday), day_analysis by offset from this week's Monday."""
        from workouts import ai
        from workouts.models import CachedWorkout, DailyStats

        monday = _monday(timezone.localdate())
        next_workout, day_analysis = {}, {}
        for k in range(7):
            as_of = monday + datetime.timedelta(days=k)
            self.stdout.write(f"As of {as_of:%A}: next-workout card and day analyses…")
            with transaction.atomic():
                seed(user, examples, log=lambda *_: None, as_of=as_of)
            with _as_of(as_of):
                today_stats = DailyStats.objects.for_user(user).get(date=as_of)
                DailyStats.objects.filter(pk=today_stats.pk).update(ai_next_workout=None,
                                                                     ai_next_workout_generated_at=None)
                today_stats.refresh_from_db()
                next_workout[str(k)] = self._undate(user, ai._get_or_generate_next_workout(user, today_stats), as_of)
                for offset in (range(-7, 0) if k == 0 else [k - 1]):
                    day = monday + datetime.timedelta(days=offset)
                    workouts = [w for w in CachedWorkout.objects.for_user(user).order_by("created_at")
                                if timezone.localtime(w.created_at).date() == day]
                    stats = DailyStats.objects.for_user(user).filter(date=day).first()
                    if not workouts or not stats:
                        continue
                    DailyStats.objects.filter(pk=stats.pk).update(ai_day_analysis=None, ai_day_generated_at=None)
                    stats.refresh_from_db()
                    text = ai._get_or_generate_day_analysis(user, day, workouts, stats)
                    if text:
                        day_analysis[str(offset)] = self._undate(user, text, as_of)
        return {"next_workout": next_workout, "day_analysis": day_analysis}

    def _undate(self, user, text, as_of):
        """Rewrite calendar dates (they drift as the demo's weeks move) into weekday or
        relative wording with a cheap Haiku pass; unchanged when there are none."""
        from workouts import llm
        if not text or not CALENDAR_DATE.search(text):
            return text
        def when(d):
            n = (d - as_of).days
            return "today" if n == 0 else f"{-n} days before today" if n < 0 else f"{n} days after today"
        table = "\n".join(f"{d:%b} {d.day} ({d:%m}/{d:%d}) = {d:%A}, {when(d)}"
                          for d in (as_of + datetime.timedelta(days=i) for i in range(-63, 85)))
        system = ("You edit text. Rewrite it so it contains no calendar dates: replace every date (like \"Oct 1\", "
                  "\"October 3–4\", \"10/08\", \"9/26\", \"2026-10-01\") with its weekday or a relative phrase "
                  "(\"last Thursday\", \"earlier this week\", \"two weeks ago\"; a future race date becomes \"race day\") using the table. Keep everything "
                  "else exactly: wording, numbers, markdown, line breaks and labels (HEADLINE:, INTENSITY:, ACTIVITY:, "
                  "REASON:, ## headers, bullets). Reply with the rewritten text only.\n\n"
                  f"Today is {as_of:%A}.\n{table}")
        fixed = llm.call(text, user=user, feature="ai_weekly_review", model=llm.HAIKU,
                         max_tokens=4000, system=system, timeout=120)
        if CALENDAR_DATE.search(fixed):
            self.stderr.write(f"  ! a calendar date is still there: {CALENDAR_DATE.search(fixed).group(0)!r}")
        return fixed
