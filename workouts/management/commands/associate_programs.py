from datetime import date

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils.text import slugify

from workouts import programs as P
from workouts.models import CachedWorkout, Program, ProgramWorkout, RunWeek


class Command(BaseCommand):
    help = "Associate synced workouts with Programs (retroactive matcher run)."

    def add_arguments(self, parser):
        parser.add_argument("--program", help="Only this program slug's rides/achievement")
        parser.add_argument("--rebuild", action="store_true",
                            help="Delete existing ProgramWorkout rows first (per --program if given)")
        parser.add_argument("--suggest", action="store_true",
                            help="Print title-suffix plans not yet tracked, don't associate")

    def handle(self, *args, **o):
        if o["rebuild"]:
            qs = ProgramWorkout.objects.all()
            if o["program"]:
                qs = qs.filter(run_week__run__program__slug=o["program"])
            n = qs.count(); qs.delete()
            self.stdout.write(f"Deleted {n} ProgramWorkout rows")

            # A plan's base skeleton (one RunWeek per ProgramWeek, sequence == week
            # number) is seeded up front and stays even with zero completions — keep
            # it. Everything else that's now empty (extra repeat passes, and every
            # RunWeek for a split, which are always created dynamically) is stale.
            empty = RunWeek.objects.annotate(n=Count("entries")).filter(n=0).select_related("run__program")
            if o["program"]:
                empty = empty.filter(run__program__slug=o["program"])
            removable = [rw.pk for rw in empty
                         if rw.run.program.kind != "plan" or rw.sequence > rw.run.program.weeks.count()]
            RunWeek.objects.filter(pk__in=removable).delete()
            self.stdout.write(f"Deleted {len(removable)} now-empty passes")

        # date order is essential for fill-or-append to reconstruct weeks correctly
        workouts = sorted(
            CachedWorkout.objects.all(),
            key=lambda w: (P.workout_local_date(w) or date.min),
        )

        if o["suggest"]:
            seen = set()
            for w in workouts:
                m = P.TITLE_SUFFIX_RE.search(w.title or "")
                if m and m.group("plan").strip().lower() not in seen:
                    plan = m.group("plan").strip()
                    if not Program.objects.filter(slug=slugify(plan)).exists():
                        seen.add(plan.lower())
                        self.stdout.write(f"  suggested plan: {plan!r}")
            self.stdout.write(self.style.SUCCESS(f"{len(seen)} untracked plan(s) seen"))
            return

        made = 0
        for w in workouts:
            if P.associate_workout(w):
                made += 1
        self.stdout.write(self.style.SUCCESS(f"Associated {made} workouts"))
