"""
Daily sync for every active user: Peloton, Garmin activities + wellness (owner
only), and Google Health.
Run every morning via launchd (see scripts/sync_daily.sh).
Withings is push-based (webhook) and doesn't need scheduling.
"""
import logging
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from workouts.management.user_arg import add_user_argument, resolve_user
from workouts.models import GoogleHealthAuth, PelotonAuth, UserSettings
from workouts.sync import (
    _integration_enabled,
    _run_garmin_sync_new,
    _run_google_health_sync_new,
    _run_peloton_sync_new,
    _run_wellness_sync,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Daily sync for each active user: Peloton, Google Health, and (owner) Garmin."

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
            help="Only sync a user if their last sync was more than HOURS hours ago (used by fallback plist)",
        )
        add_user_argument(parser)

    def handle(self, *args, **opts):
        if opts.get("user"):
            users = [resolve_user(opts)]
        else:
            users = list(get_user_model().objects.filter(is_active=True).order_by("pk"))

        any_failed = False
        for user in users:
            try:
                if not self._sync_user(user, opts):
                    any_failed = True
            except Exception as e:
                # One user's problem (expired cookie, revoked token, bad row)
                # must never stop the rest of the household from syncing.
                any_failed = True
                self._out(user, self.style.ERROR(f"✗ unexpected failure: {e}"))
                logger.exception("sync_daily failed for user %s", user.pk)

        if any_failed:
            raise SystemExit(1)

    def _out(self, user, msg):
        self.stdout.write(f"[sync_daily] [{user.username}] {msg}")

    def _step(self, user, results, name, label, fn, summarize):
        self._out(user, f"{label}…")
        try:
            r = fn()
            if "error" in r:
                raise RuntimeError(r["error"])
            results.append((name, "ok"))
            self._out(user, self.style.SUCCESS(f"  ✓ {summarize(r)}"))
        except Exception as e:
            results.append((name, "fail"))
            self._out(user, self.style.ERROR(f"  ✗ {e}"))
            logger.exception("%s sync failed for user %s", label, user.pk)

    def _sync_user(self, user, opts):
        """Run every enabled source for one user. Returns False if any failed."""
        settings_row = UserSettings.for_user(user)
        stale_hours = opts.get("if_stale")
        if stale_hours and settings_row.last_daily_sync_at and \
                (timezone.now() - settings_row.last_daily_sync_at).total_seconds() < stale_hours * 3600:
            self._out(user, f"Skipped — last sync was less than {stale_hours}h ago.")
            return True

        do_peloton = (not opts["skip_peloton"] and _integration_enabled(user, "peloton")
                      and PelotonAuth.for_user(user) is not None)
        do_garmin = user.is_superuser and _integration_enabled(user, "garmin")
        do_google = (_integration_enabled(user, "google_health")
                     and GoogleHealthAuth.for_user(user) is not None)
        if not (do_peloton or do_garmin or do_google):
            self._out(user, "Nothing connected — skipped.")
            return True

        results = []
        counts = lambda r: f"{r.get('created', 0)} new, {r.get('updated', 0)} updated"

        # Order no longer matters for correctness: Peloton sync runs
        # _reconcile_garmin_duplicates() (and the Google Health equivalent)
        # after every run, which cleans up any Garmin/Google Health rows that
        # duplicate a Peloton workout regardless of which source synced
        # first. Peloton still runs first here just to avoid the wasted
        # work of creating a Garmin row only to delete it moments later.
        if do_peloton:
            self._step(user, results, "peloton", "Peloton",
                       lambda: _run_peloton_sync_new(user), counts)

        if do_garmin:
            # Garmin activities — new since last sync
            self._step(user, results, "garmin_activities", "Garmin activities",
                       lambda: _run_garmin_sync_new(user), counts)
            # Garmin wellness — today and yesterday by default
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(opts["wellness_days"])]
            self._step(user, results, "garmin_wellness", "Garmin wellness",
                       lambda: _run_wellness_sync(user, dates),
                       lambda r: f"{r.get('synced', len(dates))} day(s) of wellness data")

        if do_google:
            def google():
                r = _run_google_health_sync_new(user)
                for part in ("wellness", "exercise"):
                    if "error" in r.get(part, {}):
                        return {"error": f"{part}: {r[part]['error']}"}
                return r
            self._step(user, results, "google_health", "Google Health", google,
                       lambda r: (f"{r['wellness'].get('synced', 0)} day(s) of wellness, "
                                  f"{counts(r['exercise'])} workouts"))

        failed = [name for name, status in results if status == "fail"]
        if failed:
            self._out(user, self.style.WARNING(f"Done with {len(failed)} failure(s): {', '.join(failed)}"))
            return False
        UserSettings.objects.filter(pk=settings_row.pk).update(last_daily_sync_at=timezone.now())
        self._out(user, self.style.SUCCESS("All sources synced ✓"))
        return True
