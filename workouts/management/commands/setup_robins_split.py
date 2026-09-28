"""
Configure the "Robin's Split + Run" program for the 6-day weekly plan:

    Day 1 Upper Body Push + Run   Day 2 Lower Body + Run   Day 3 Pilates (any class)
    Day 4 Upper Body Pull + Run   Day 5 Lower Body + Run   Day 6 Yoga (any class)

- turns on recovery tracking (cool-down walk / stretch after each completion),
- gives the four ride slots their plan day numbers,
- adds "any class" slots for Pilates (stored by Peloton as discipline "strength", so it
  also requires "pilates" in the title) and Yoga (discipline "yoga"),
- then catches the current run up: matches any Pilates/Yoga classes since it started and
  attaches walks/stretches taken right after workouts.

Idempotent. Usage:
    python manage.py setup_robins_split --dry-run   # show the plan, change nothing
    python manage.py setup_robins_split
"""
from django.core.management.base import BaseCommand, CommandError

from workouts.models import Program, ProgramSlot
from workouts.programs import reconcile_program_extras

SLUG = "robins-split-run"

# Ride-id prefix -> (plan day, label). The two "Lower Body + Run" classes have identical
# titles; the class descriptions say which day of Robin's series each one is (D2 squats/lunges,
# D4 hip thrusts + RDLs), which the plan numbers as Day 2 and Day 5.
RIDE_DAYS = {
    "f7771d36": (1, "Upper Body Push + Run"),
    "7a4a6ffa": (2, "Lower Body + Run (squats/lunges)"),
    "995a556a": (4, "Upper Body Pull + Run"),
    "28b36359": (5, "Lower Body + Run (hip thrusts/RDLs)"),
}
ANY_CLASS_SLOTS = [
    dict(day=3, order=0, title="Pilates (any class)", discipline="strength",
         match_discipline="strength", match_title_keyword="pilates"),
    dict(day=6, order=0, title="Yoga (any class)", discipline="yoga",
         match_discipline="yoga", match_title_keyword=""),
]


class Command(BaseCommand):
    help = "Enable recovery tracking and Pilates/Yoga slots on the Robin's Split + Run program."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        program = Program.objects.filter(slug=SLUG).first()
        if program is None:
            raise CommandError(f"Program {SLUG!r} not found.")
        week = program.weeks.order_by("number").first()
        if week is None:
            raise CommandError("Program has no week defined.")
        tag = "[dry-run] would" if dry else "Did:"

        if not program.track_recovery:
            self.stdout.write(f"{tag} enable track_recovery on {program.name}")
            if not dry:
                program.track_recovery = True
                program.save(update_fields=["track_recovery"])

        for slot in week.slots.exclude(peloton_ride_id=""):
            for prefix, (day, label) in RIDE_DAYS.items():
                if slot.peloton_ride_id.startswith(prefix) and slot.day != day:
                    self.stdout.write(f"{tag} set day {day} on slot {slot.pk} ({label})")
                    if not dry:
                        slot.day = day
                        slot.save(update_fields=["day"])

        for spec in ANY_CLASS_SLOTS:
            existing = week.slots.filter(match_discipline=spec["match_discipline"],
                                         match_title_keyword=spec["match_title_keyword"]).first()
            if existing:
                continue
            self.stdout.write(f"{tag} add slot {spec['title']!r} (day {spec['day']}, "
                              f"discipline={spec['match_discipline']!r}, keyword={spec['match_title_keyword']!r})")
            if not dry:
                ProgramSlot.objects.create(week=week, **spec)

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing written; the catch-up match runs on a real run."))
            return
        result = reconcile_program_extras()
        self.stdout.write(self.style.SUCCESS(
            f"Caught up: {result['associated']} Pilates/Yoga classes matched, "
            f"{result['recoveries']} recovery sessions attached."
        ))
