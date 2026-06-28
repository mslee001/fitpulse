"""
Daily sync: Garmin activities + wellness + Peloton.
Run every morning via launchd (see scripts/sync_daily.sh).
Withings is push-based (webhook) and doesn't need scheduling.
"""
import logging
from datetime import date, timedelta
from django.core.management.base import BaseCommand

from workouts.sync import (
    _run_garmin_sync_new,
    _run_wellness_sync,
    _run_peloton_sync_new,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Daily sync: Garmin activities + wellness, optionally Peloton."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-peloton",
            action="store_true",
            help="Skip Peloton sync (Garmin only)",
        )
        parser.add_argument(
            "--wellness-days",
            type=int,
            default=2,
            help="Days back to sync wellness (default: 2, catches today + yesterday)",
        )
        parser.add_argument(
            "--if-stale",
            type=int,
            metavar="HOURS",
            help="Only sync if last sync was more than HOURS hours ago (used by fallback plist)",
        )

    def handle(self, *args, **opts):
        stale_hours = opts.get("if_stale")
        if stale_hours:
            from django.utils import timezone
            from workouts.models import UserSettings
            last = UserSettings.objects.filter(pk=1).values_list(
                "last_daily_sync_at", flat=True
            ).first()
            if last and (timezone.now() - last).total_seconds() < stale_hours * 3600:
                self.stdout.write(
                    f"[sync_daily] Skipped — last sync was less than {stale_hours}h ago."
                )
                return

        results = []

        # Peloton first — its timestamps must be in the DB before Garmin dedup runs,
        # otherwise a workout done today appears in both (Garmin doesn't see it as a duplicate).
        if not opts["skip_peloton"]:
            self.stdout.write("[sync_daily] Peloton…")
            try:
                r = _run_peloton_sync_new()
                if "error" in r:
                    raise RuntimeError(r["error"])
                summary = f"{r.get('created', 0)} new, {r.get('updated', 0)} updated"
                results.append(("peloton", "ok"))
                self.stdout.write(self.style.SUCCESS(f"  ✓ {summary}"))
            except Exception as e:
                results.append(("peloton", "fail"))
                self.stdout.write(self.style.ERROR(f"  ✗ {e}"))
                logger.exception("Peloton sync failed")

        # Garmin activities — new since last sync
        self.stdout.write("[sync_daily] Garmin activities…")
        try:
            r = _run_garmin_sync_new()
            if "error" in r:
                raise RuntimeError(r["error"])
            summary = f"{r.get('created', 0)} new, {r.get('updated', 0)} updated"
            results.append(("garmin_activities", "ok"))
            self.stdout.write(self.style.SUCCESS(f"  ✓ {summary}"))
        except Exception as e:
            results.append(("garmin_activities", "fail"))
            self.stdout.write(self.style.ERROR(f"  ✗ {e}"))
            logger.exception("Garmin activities sync failed")

        # Garmin wellness — today and yesterday by default
        self.stdout.write("[sync_daily] Garmin wellness…")
        try:
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(opts["wellness_days"])]
            r = _run_wellness_sync(dates)
            if "error" in r:
                raise RuntimeError(r["error"])
            results.append(("garmin_wellness", "ok"))
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ {r.get('synced', len(dates))} day(s) of wellness data"
            ))
        except Exception as e:
            results.append(("garmin_wellness", "fail"))
            self.stdout.write(self.style.ERROR(f"  ✗ {e}"))
            logger.exception("Garmin wellness sync failed")

        # Summary
        failed = [r[0] for r in results if r[1] == "fail"]
        if failed:
            self.stdout.write(self.style.WARNING(
                f"\n[sync_daily] Done with {len(failed)} failure(s): {', '.join(failed)}"
            ))
            raise SystemExit(1)
        else:
            from django.utils import timezone
            from workouts.models import UserSettings
            UserSettings.objects.filter(pk=1).update(last_daily_sync_at=timezone.now())
            self.stdout.write(self.style.SUCCESS("\n[sync_daily] All sources synced ✓"))
