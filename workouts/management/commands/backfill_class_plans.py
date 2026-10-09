"""
Backfill CachedWorkout.class_plan_json (the class's programmed exercises) and
run_targets_json (its Tread running targets) for existing Peloton
strength/circuit workouts, and the performance graph of circuit workouts that
don't have one (their measured run pace and the pace level they were taken at).

New workouts get all of this automatically during sync
(_fetch_and_store_details / _fetch_and_store_performance); this covers ones
synced before that existed. Class details cost one Peloton request per distinct
class (ride_id), not per workout — repeat takes of a class share it; a
performance graph is one request per workout. Read-only against Peloton.

Usage:
    python manage.py backfill_class_plans --dry-run   # report only
    python manage.py backfill_class_plans             # apply
    python manage.py backfill_class_plans --force     # also refresh workouts that already have a plan
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from workouts.models import CachedWorkout
from workouts.run_targets import pace_level_at
from workouts.services.peloton_client import parse_class_plan, parse_run_targets
from workouts.sync import _CLASS_PLAN_DISCS, _extract_perf_fields
from workouts.management.user_arg import add_user_argument, resolve_user


class Command(BaseCommand):
    help = "Fetch and store class exercise plans, running targets and circuit performance graphs for existing Peloton workouts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be fetched without calling Peloton or writing.")
        parser.add_argument("--force", action="store_true", help="Refresh workouts that already have a stored plan.")
        add_user_argument(parser)

    def handle(self, *args, **opts):
        user = resolve_user(opts)
        base = CachedWorkout.objects.for_user(user).filter(source="peloton")
        qs = base.filter(discipline__in=_CLASS_PLAN_DISCS).exclude(ride_id="")
        if not opts["force"]:
            qs = qs.filter(Q(class_plan_json=[]) | Q(run_targets_json__isnull=True))
        by_ride = {}
        for w in qs.only("id", "ride_id", "title", "created_at", "user"):
            by_ride.setdefault(w.ride_id, []).append(w)
        perf_ids = list(base.filter(discipline="circuit")
                        .filter(Q(performance_graph_json__isnull=True) | Q(performance_graph_json={}))
                        .values_list("workout_id", flat=True))

        self.stdout.write(f"{sum(len(v) for v in by_ride.values())} workouts across {len(by_ride)} distinct classes "
                          f"need class details; {len(perf_ids)} circuit workouts need a performance graph.")
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing fetched or written."))
            return

        from workouts.services.peloton_client import PelotonClient
        client = PelotonClient(user)

        # Performance graphs first: they carry the pace level each run was taken at.
        graphs = 0
        for wid in perf_ids:
            try:
                perf = client.get_parsed_performance(wid, every_n=5)
            except Exception as e:
                self.stderr.write(f"performance graph {wid}: failed ({e})")
                continue
            if perf:
                row = base.filter(workout_id=wid)
                fields = {"performance_graph_json": perf}
                if not row.filter(user_corrected=True).exists():
                    fields.update({k: v for k, v in _extract_perf_fields(perf).items() if v is not None})
                row.update(**fields)
                graphs += 1
            time.sleep(0.3)

        stored = empty = runs = failed = 0
        for i, (ride_id, workouts) in enumerate(by_ride.items(), 1):
            try:
                ride = client.get_ride_details(ride_id)
            except Exception as e:
                failed += 1
                self.stderr.write(f"[{i}/{len(by_ride)}] {workouts[0].title}: failed ({e})")
                continue
            plan, targets = parse_class_plan(ride), parse_run_targets(ride)
            if plan:
                CachedWorkout.objects.filter(pk__in=[w.pk for w in workouts]).update(class_plan_json=plan)
                stored += len(workouts)
            else:
                empty += 1  # class has no programmed exercises — leave the plan unset
            for w in workouts:
                CachedWorkout.objects.filter(pk=w.pk).update(run_targets_json={
                    "segments": targets, "level": pace_level_at(user, w.created_at) if targets else None})
            if targets:
                runs += len(workouts)
            time.sleep(0.3)  # be polite to Peloton's API
        self.stdout.write(self.style.SUCCESS(
            f"Stored plans on {stored} workouts; {empty} classes had no exercises; running targets on {runs} "
            f"workouts; {graphs} performance graphs; {failed} fetches failed."
        ))
