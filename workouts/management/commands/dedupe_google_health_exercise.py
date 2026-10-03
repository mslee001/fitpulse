"""
One-time cleanup for Google Health exercise workouts synced before the
2026-08-18 dedup fix.

Background: the original Peloton-dedup filter (_is_peloton_sourced) only
caught exercise entries synced through Peloton's own Fitbit Web API
integration. It missed a second source of duplicates — the watch's own
independent auto-workout-detection, which logs a separate generic-titled
entry ("Walk", "Run", "Stretching") for the same physical session. A first
full "Sync All" run created 471 such CachedWorkout rows, 446 of which
duplicate an existing Peloton workout (confirmed by a timestamp-window
match — see _find_peloton_match in sync.py).

This command re-processes every existing source="google_health" CachedWorkout
row via _reconcile_google_health_duplicates() in sync.py (the same function
Peloton syncs now call automatically after every run) — useful for a manual
sweep, or to pick up matches after widening the matching window.

Usage:
    python manage.py dedupe_google_health_exercise --dry-run   # report only
    python manage.py dedupe_google_health_exercise             # apply
"""
from django.core.management.base import BaseCommand

from workouts.management.user_arg import add_user_argument, resolve_user


class Command(BaseCommand):
    help = "Reconcile and delete Google Health CachedWorkout rows that duplicate an existing Peloton workout."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be augmented/deleted without writing anything.",
        )
        add_user_argument(parser)

    def handle(self, *args, **options):
        from workouts.sync import _reconcile_google_health_duplicates

        dry_run = options["dry_run"]
        result = _reconcile_google_health_duplicates(resolve_user(options), dry_run=dry_run)

        for d in result["details"]:
            if d.get("overlap_only"):
                self.stdout.write(
                    f"{d['google_workout_id']} overlaps {d['peloton_workout_id']} "
                    f"({d['peloton_title']}) — deleting, not merging (its own stats don't describe that one workout)"
                )
            elif not d["had_raw_data"]:
                self.stdout.write(self.style.WARNING(
                    f"{d['google_workout_id']} has no stored raw_data — deleting without reconciling"
                ))
            elif d["filled"]:
                prefix = "[dry-run] would fill" if dry_run else "Augmented"
                self.stdout.write(
                    f"{prefix} {d['filled']} on {d['peloton_workout_id']} ({d['peloton_title']}) "
                    f"from {d['google_workout_id']}"
                )

        verb = "would be augmented" if dry_run else "augmented"
        self.stdout.write(
            f"\n{result['matched']} of {result['checked']} Google Health workouts duplicate an existing Peloton workout. "
            f"{result['augmented']} Peloton records {verb}."
        )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run — {result['matched']} rows would be deleted. Re-run without --dry-run to apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Deleted {result['deleted']} duplicate CachedWorkout rows."))
