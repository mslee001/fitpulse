from datetime import date

from django.core.management.base import BaseCommand

from workouts.models import (
    CachedWorkout, Program, ProgramWeek, ProgramSlot, ProgramRun, RunWeek,
)
from workouts.programs import workout_local_date

HILIT_REGEX = r"W(?:eek)?\s*(?P<week>\d+)[,\s]*D(?:ay)?\s*(?P<day>\d+)"

# (week, day, order, base_title, discipline, duration, optional)
# base_title should match the real title minus its ": HiLit W_ D_" suffix.
# Matching is tolerant (substring); reconcile titles as real classes sync, or pin ride_ids.
HILIT_SCHEDULE = [
    # Week 1 (30-min block)
    (1, 1, 0, "10 min Mobility", "stretching", 10, False),
    (1, 1, 1, "30 min Power Walk", "walking", 30, False),
    (1, 2, 0, "10 min Mobility", "stretching", 10, True),
    (1, 2, 1, "30 min Full Body Strength: Pull", "strength", 30, False),
    (1, 2, 2, "10 min Low Impact Cardio", "cardio", 10, False),
    (1, 3, 0, "10 min Mobility", "stretching", 10, True),
    (1, 3, 1, "20 min Pilates", "stretching", 20, False),
    (1, 4, 0, "10 min Mobility", "stretching", 10, True),
    (1, 4, 1, "30 min Full Body Strength: Push", "strength", 30, False),
    (1, 4, 2, "15 min Low Impact Cardio", "cardio", 15, False),
    (1, 5, 0, "10 min Mobility", "stretching", 10, True),
    (1, 5, 1, "20 min Standing Core", "strength", 20, False),
    (1, 5, 2, "10 min Full Body Stretch", "stretching", 10, False),
    # Week 2 (45-min block)
    (2, 1, 0, "15 min Mobility", "stretching", 15, False),
    (2, 1, 1, "30 min Hike", "walking", 30, False),
    (2, 2, 0, "15 min Mobility", "stretching", 15, True),
    (2, 2, 1, "45 min Full Body Strength: Pull", "strength", 45, False),
    (2, 3, 0, "15 min Mobility", "stretching", 15, True),
    (2, 3, 1, "45 min Pilates", "stretching", 45, False),
    (2, 4, 0, "15 min Mobility", "stretching", 15, True),
    (2, 4, 1, "45 min Full Body Strength: Push", "strength", 45, False),
    (2, 5, 0, "15 min Mobility", "stretching", 15, True),
    (2, 5, 1, "15 min Standing Core", "strength", 15, False),
    (2, 5, 2, "30 min Low Impact Cardio", "cardio", 30, False),
    # Week 3
    (3, 1, 0, "10 min Full Body Foam Rolling", "stretching", 10, False),
    (3, 1, 1, "30 min Hike", "walking", 30, False),
    (3, 2, 0, "10 min Full Body Foam Rolling", "stretching", 10, True),
    (3, 2, 1, "45 min Full Body Strength: Pull", "strength", 45, False),
    (3, 3, 0, "10 min Full Body Foam Rolling", "stretching", 10, True),
    (3, 3, 1, "30 min Pilates", "stretching", 30, False),
    (3, 4, 0, "10 min Full Body Foam Rolling", "stretching", 10, True),
    (3, 4, 1, "45 min Full Body Strength: Push", "strength", 45, False),
    (3, 5, 0, "10 min Full Body Foam Rolling", "stretching", 10, True),
    (3, 5, 1, "30 min Hiking Bootcamp", "walking", 30, False),
    # Week 4
    (4, 1, 0, "45 min Hike", "walking", 45, False),
    (4, 2, 0, "45 min Full Body Strength: Pull", "strength", 45, False),
    (4, 2, 1, "10 min Low Impact Cardio", "cardio", 10, False),
    (4, 3, 0, "30 min Pilates", "stretching", 30, False),
    (4, 4, 0, "45 min Full Body Strength: Push", "strength", 45, False),
    (4, 4, 1, "10 min Low Impact Cardio", "cardio", 10, False),
    (4, 5, 0, "45 min Hiking Bootcamp", "walking", 45, False),
    (4, 6, 0, "20 min Full Body Stretch", "stretching", 20, False),
]

# The split's three known classes. series_id_hint groups a much broader family of
# strength classes across multiple past splits (older 30-min Back&Biceps/Legs&Core/
# etc. variants share it too), so we don't trust blind series discovery — only these
# ride-ids (confirmed by the user) are treated as authoritative, with discovery used
# solely to refresh title/discipline. Pull and Push each have a second ride-id because
# Peloton re-shot both classes at some point under a new id but kept the same title —
# alt_ride_ids on the ProgramSlot covers the older completions.
SPLIT_SERIES_ID = "32462012d0de4377b0cd97578b4103ab"
SPLIT_KNOWN = [
    {"ride_ids": ["89316d8ca82d4c53858f8618afe1da82", "6cd388a9f01340ee88b3e38ded56971d"],
     "title": "45 min Upper Body: Pull", "discipline": "strength"},
    {"ride_ids": ["08831cf41e6c4aaf87628a215c503ef5", "e6b84059875b487995f9b2454545dea8"],
     "title": "45 min Upper Body: Push", "discipline": "strength"},
    {"ride_ids": ["bf63df83cfbf43abac717355855a5bb6"],
     "title": "45 min Legs and Core", "discipline": "strength"},
]


class Command(BaseCommand):
    help = "Seed the HiLit plan and the Rebecca 3 Day Split programs (idempotent)."

    def handle(self, *args, **opts):
        self._seed_hilit()
        self._seed_split()
        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_hilit(self):
        p, created = Program.objects.get_or_create(
            slug="hilit",
            defaults=dict(
                name="HiLit", kind="plan", instructor="Rebecca Kennedy",
                match_strategy="achievement", achievement_name="HiLit Training Plan",
                title_week_day_regex=HILIT_REGEX,
                description="Rebecca Kennedy's High Intensity, Low Impact Training plan (4 weeks).",
            ),
        )
        weeks = {}
        for n in range(1, 5):
            weeks[n], _ = ProgramWeek.objects.get_or_create(program=p, number=n)
        for (w, d, order, title, disc, dur, opt) in HILIT_SCHEDULE:
            ProgramSlot.objects.get_or_create(
                week=weeks[w], day=d, order=order, title=title,
                defaults=dict(discipline=disc, duration_min=dur, optional=opt),
            )
        # Seed an empty current run so the 4-week grid shows immediately.
        if p.active_run is None:
            first = CachedWorkout.objects.filter(title__icontains="HiLit").order_by("created_at").first()
            start = workout_local_date(first) if first else date.today()
            run = ProgramRun.objects.create(program=p, start_date=start, label="Cycle 1")
            for n in range(1, 5):
                RunWeek.objects.get_or_create(run=run, program_week=weeks[n], sequence=n)
        self.stdout.write(f"HiLit: {'created' if created else 'exists'}, "
                          f"{p.weeks.count()} weeks, {ProgramSlot.objects.filter(week__program=p).count()} slots")

    def _seed_split(self):
        p, created = Program.objects.get_or_create(
            slug="rebecca-3-day-split",
            defaults=dict(
                name="Rebecca 3 Day Split", kind="split", instructor="Rebecca Kennedy",
                match_strategy="ride_ids", series_id_hint=SPLIT_SERIES_ID,
                description="Same three rides repeated weekly; associated by ride-id.",
            ),
        )
        week, _ = ProgramWeek.objects.get_or_create(program=p, number=1)

        # Refresh title/discipline for each known ride-id from synced history, if present.
        all_ids = {rid for entry in SPLIT_KNOWN for rid in entry["ride_ids"]}
        discovered = {}
        for w in CachedWorkout.objects.filter(ride_id__in=all_ids):
            rid = w.ride_id
            if rid and rid not in discovered:
                discovered[rid] = (w.title or "", w.discipline or "strength")

        for i, entry in enumerate(SPLIT_KNOWN):
            canonical, *alts = entry["ride_ids"]
            title, disc = discovered.get(canonical, (entry["title"], entry["discipline"]))
            ProgramSlot.objects.get_or_create(
                week=week, peloton_ride_id=canonical,
                defaults=dict(title=title, discipline=disc, order=i, day=None, alt_ride_ids=alts),
            )
        self.stdout.write(f"Split: {'created' if created else 'exists'}, "
                          f"{week.slots.count()} slots ({'discovered' if discovered else 'known fallback'})")
