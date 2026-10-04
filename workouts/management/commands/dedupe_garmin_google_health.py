"""
Manual sweep for Google Health CachedWorkout rows that duplicate a
Garmin-sourced workout with no Peloton counterpart at all.

Background: _reconcile_google_health_duplicates only checks Google Health
rows against Peloton workouts. Both _reconcile_garmin_duplicates and that
function are anchored on Peloton — but an activity neither Garmin nor
Google Health routes through Peloton (e.g. an outdoor hike, or any workout
not also logged on the Peloton bike/tread) never goes through either
reconciler, so a Garmin watch and Health Connect independently
auto-detecting the same session land as two permanent separate rows.

This command (and _reconcile_garmin_google_health_duplicates() in sync.py,
which Garmin and Google Health syncs now call automatically) closes that
gap — useful for a manual sweep of duplicates left over from before this
fix. Garmin is treated as authoritative (a dedicated watch's own recording
beats a phone/Health-Connect-detected entry), same reasoning as Peloton
being authoritative over Garmin elsewhere.

Usage:
    python manage.py dedupe_garmin_google_health --dry-run   # report only
    python manage.py dedupe_garmin_google_health             # apply
"""
from django.core.management.base import BaseCommand

from workouts.management.user_arg import add_user_argument, resolve_user


class Command(BaseCommand):
    help = "Reconcile and delete Google Health CachedWorkout rows that duplicate an existing Garmin workout."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be augmented/deleted without writing anything.",
        )
        add_user_argument(parser)

    def handle(self, *args, **options):
        from workouts.sync import _reconcile_garmin_google_health_duplicates

        dry_run = options["dry_run"]
        result = _reconcile_garmin_google_health_duplicates(resolve_user(options), dry_run=dry_run)

        for d in result["details"]:
            if d.get("overlap_only"):
                self.stdout.write(
                    f"{d['google_workout_id']} overlaps {d['garmin_workout_id']} "
                    f"({d['garmin_title']}) — deleting, not merging (its own stats don't describe that one workout)"
                )
            elif not d["had_raw_data"]:
                self.stdout.write(self.style.WARNING(
                    f"{d['google_workout_id']} has no stored raw_data — deleting without reconciling"
                ))
            elif d["filled"]:
                prefix = "[dry-run] would fill" if dry_run else "Augmented"
                self.stdout.write(
                    f"{prefix} {d['filled']} on {d['garmin_workout_id']} ({d['garmin_title']}) "
                    f"from {d['google_workout_id']}"
                )
            else:
                self.stdout.write(
                    f"{d['google_workout_id']} matches {d['garmin_workout_id']} "
                    f"({d['garmin_title']}) — nothing new to fill"
                )

        verb = "would be augmented" if dry_run else "augmented"
        self.stdout.write(
            f"\n{result['matched']} of {result['checked']} Google Health workouts duplicate an existing "
            f"Garmin workout with no Peloton counterpart. {result['augmented']} Garmin records {verb}."
        )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run — {result['matched']} rows would be deleted. Re-run without --dry-run to apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Deleted {result['deleted']} duplicate CachedWorkout rows."))
