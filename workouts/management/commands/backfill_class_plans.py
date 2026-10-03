"""
Backfill CachedWorkout.class_plan_json (the class's programmed exercises) for
existing Peloton strength/circuit workouts.

New workouts get their plan automatically during detail sync
(_fetch_and_store_details); this covers ones synced before that existed. One
Peloton request per distinct class (ride_id), not per workout — repeat takes of
a class share it. Read-only against Peloton; only writes class_plan_json.

Usage:
    python manage.py backfill_class_plans --dry-run   # report only
    python manage.py backfill_class_plans             # apply
    python manage.py backfill_class_plans --force     # also refresh workouts that already have a plan
"""
import time

from django.core.management.base import BaseCommand

from workouts.models import CachedWorkout
from workouts.sync import _CLASS_PLAN_DISCS
from workouts.management.user_arg import add_user_argument, resolve_user


class Command(BaseCommand):
    help = "Fetch and store the class exercise plan for existing Peloton strength/circuit workouts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be fetched without calling Peloton or writing.")
        parser.add_argument("--force", action="store_true", help="Refresh workouts that already have a stored plan.")
        add_user_argument(parser)

    def handle(self, *args, **opts):
        user = resolve_user(opts)
        qs = CachedWorkout.objects.for_user(user).filter(
            source="peloton", discipline__in=_CLASS_PLAN_DISCS,
        ).exclude(ride_id="")
        if not opts["force"]:
            qs = qs.filter(class_plan_json=[])
        by_ride = {}
        for w in qs.only("id", "ride_id", "title"):
            by_ride.setdefault(w.ride_id, []).append(w)

        self.stdout.write(f"{sum(len(v) for v in by_ride.values())} workouts across {len(by_ride)} distinct classes need a plan.")
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing fetched or written."))
            return

        from workouts.services.peloton_client import PelotonClient
        client = PelotonClient(user)
        stored = empty = failed = 0
        for i, (ride_id, workouts) in enumerate(by_ride.items(), 1):
            try:
                plan = client.get_class_plan(ride_id)
            except Exception as e:
                failed += 1
                self.stderr.write(f"[{i}/{len(by_ride)}] {workouts[0].title}: failed ({e})")
                continue
            if not plan:
                empty += 1  # class has no programmed exercises (e.g. plain run) — leave unset
                continue
            CachedWorkout.objects.filter(pk__in=[w.pk for w in workouts]).update(class_plan_json=plan)
            stored += len(workouts)
            time.sleep(0.3)  # be polite to Peloton's API
        self.stdout.write(self.style.SUCCESS(
            f"Stored plans on {stored} workouts; {empty} classes had no exercises; {failed} fetches failed."
        ))
