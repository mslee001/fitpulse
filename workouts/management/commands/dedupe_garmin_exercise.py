"""
Manual sweep for Garmin CachedWorkout rows that duplicate an existing
Peloton workout.

Background: Garmin sync's dedup (_is_peloton_duplicate) only checks
against Peloton workouts that already exist at the moment Garmin syncs —
a one-time snapshot taken at the start of that run. If Garmin syncs
before the matching Peloton workout does, the two land as separate rows
and nothing re-checks them afterward, since Peloton sync never used to
look at garmin rows at all.

This command (and _reconcile_garmin_duplicates() in sync.py, which
Peloton syncs now call automatically after every run) closes that gap —
useful for a manual sweep of duplicates left over from before this fix,
or from any Garmin sync that ran before its matching Peloton workout did.

Usage:
    python manage.py dedupe_garmin_exercise --dry-run   # report only
    python manage.py dedupe_garmin_exercise             # apply
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Reconcile and delete Garmin CachedWorkout rows that duplicate an existing Peloton workout."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be augmented/deleted without writing anything.",
        )

    def handle(self, *args, **options):
        from workouts.sync import _reconcile_garmin_duplicates

        dry_run = options["dry_run"]
        result = _reconcile_garmin_duplicates(dry_run=dry_run)

        for d in result["details"]:
            if d["discipline"] == "running":
                if d["skipped"]:
                    self.stdout.write(self.style.WARNING(
                        f"{d['garmin_workout_id']} matches {d['peloton_workout_id']} but Garmin is "
                        f"disabled (or the API call failed) — left in place, not deleted"
                    ))
                    continue
                prefix = "[dry-run] would fill" if dry_run else "Augmented"
                if d["filled"]:
                    self.stdout.write(
                        f"{prefix} {d['filled']} on {d['peloton_workout_id']} ({d['peloton_title']}) "
                        f"from {d['garmin_workout_id']}"
                    )
                else:
                    self.stdout.write(
                        f"{d['garmin_workout_id']} matches {d['peloton_workout_id']} "
                        f"({d['peloton_title']}) — nothing new to fill"
                    )
            else:
                self.stdout.write(
                    f"{d['garmin_workout_id']} ({d['discipline']}) duplicates "
                    f"{d['peloton_workout_id']} ({d['peloton_title']}) — Peloton's own data is authoritative"
                )

        to_delete_count = sum(1 for d in result["details"] if not d["skipped"])
        verb = "would be augmented" if dry_run else "augmented"
        self.stdout.write(
            f"\n{result['matched']} of {result['checked']} Garmin workouts duplicate an existing Peloton workout. "
            f"{result['augmented']} Peloton records {verb}."
        )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run — {to_delete_count} rows would be deleted. Re-run without --dry-run to apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Deleted {result['deleted']} duplicate CachedWorkout rows."))
