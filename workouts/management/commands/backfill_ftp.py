"""
Backfill the ftp field on CachedWorkout based on known historical FTP values.

Usage:
    python manage.py backfill_ftp

Edit FTP_HISTORY below with your own FTP test dates and values, newest first.
Workouts before the earliest date will have FTP set to null.
"""
from datetime import date, datetime, timezone
from django.core.management.base import BaseCommand
from workouts.models import CachedWorkout


FTP_HISTORY = [
    # (effective_date, ftp_value)  — sorted newest first so we can use "first match"
    # Add your FTP history here, e.g.:
    # (date(2024, 12, 1), 200),
    # (date(2024,  6, 1), 185),
]


def ftp_for_date(d: date):
    for effective, ftp in FTP_HISTORY:
        if d >= effective:
            return ftp
    return None


class Command(BaseCommand):
    help = "Backfill the per-workout FTP field from historical FTP values"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be set without writing to the DB",
        )
        parser.add_argument(
            "--discipline", default="",
            help="Only update workouts with this discipline (default: all cycling)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        discipline = options["discipline"] or None

        qs = CachedWorkout.objects.filter(
            discipline__in=["cycling", "bike_bootcamp"]
        )
        if discipline:
            qs = CachedWorkout.objects.filter(discipline=discipline)

        updated = skipped = 0
        for w in qs.iterator():
            if not w.created_at:
                skipped += 1
                continue
            d = w.created_at.astimezone(timezone.utc).date()
            ftp = ftp_for_date(d)
            if not dry_run:
                w.ftp = ftp
                w.save(update_fields=["ftp"])
            updated += 1
            if dry_run:
                self.stdout.write(f"  {w.created_at:%Y-%m-%d}  {w.title[:40]:<40}  FTP→{ftp}")

        if dry_run:
            self.stdout.write(self.style.WARNING(f"\nDry run — {updated} workouts would be updated, {skipped} skipped (no date)"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done — {updated} workouts updated, {skipped} skipped (no date)"))
