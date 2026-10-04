"""
Daily sync for every active user: Peloton, Garmin activities + wellness (owner
only), and Google Health.
Scheduled as a Render Cron Job (`python manage.py sync_daily --skip-garmin`,
twice a day); see README "Daily sync". Garmin runs only from its Sync buttons.
Withings is push-based (webhook) and doesn't need scheduling.
"""
import logging
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from workouts.management.user_arg import add_user_argument, resolve_user
from workouts.models import GoogleHealthAuth, PelotonAuth, UserSettings
from workouts.users import get_owner
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
            "--catalog-full", action="store_true",
            help="Run a full Peloton class catalog sync now (owner only) instead of waiting for the weekly one",
        )
        parser.add_argument(
            "--skip-peloton",
            action="store_true",
            help="Skip Peloton sync (Garmin only)",
        )
        parser.add_argument(
            "--skip-garmin",
            action="store_true",
            help="Skip Garmin (its tokens live only on the machine where garmin_login ran — the Render cron "
                 "job always passes this; Garmin syncs run from the Sync buttons instead)",
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
            help="Only sync a user if their last sync was more than HOURS hours ago",
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

        peloton_auth = PelotonAuth.for_user(user)
        do_peloton = (not opts["skip_peloton"] and _integration_enabled(user, "peloton")
                      and peloton_auth is not None and not peloton_auth.needs_reconnect)
        if peloton_auth and peloton_auth.needs_reconnect and not opts["skip_peloton"]:
            # A rejected refresh token never comes back; retrying would only log errors.
            self._out(user, "Peloton needs reconnecting at /settings/integrations/ — skipped.")
        do_garmin = not opts.get("skip_garmin") and user.is_superuser and _integration_enabled(user, "garmin")
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
            if user == get_owner() and results[-1] == ("peloton", "ok"):
                # Shared class catalog: a full sync once a week (refreshes ratings on older
                # classes and retires removed ones), otherwise incremental — usually one
                # request per category.
                from workouts.catalog import catalog_job_running, full_sync_due
                if catalog_job_running(user):
                    self._out(user, "Peloton class catalog… skipped — a catalog sync is already running.")
                elif opts.get("catalog_full") or full_sync_due(user):
                    self._step(user, results, "catalog", "Peloton class catalog (weekly full sync)",
                               lambda: _catalog_sync(user, full=True), _catalog_summary)
                else:
                    self._step(user, results, "catalog", "Peloton class catalog",
                               lambda: _catalog_sync(user, full=False), _catalog_summary)

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


def _catalog_sync(user, full):
    """sync_catalog puts a top-level "error" in its result when any category failed."""
    from workouts.catalog import run_recorded_full_sync, sync_catalog
    return run_recorded_full_sync(user) if full else sync_catalog(user, full=False)


def _catalog_summary(r):
    cats = r["categories"].values()
    line = f"{sum(c['created'] for c in cats)} new classes"
    gone = sum(c.get("marked_unavailable", 0) for c in cats)
    if any("marked_unavailable" in c for c in cats):
        line += f", {gone} retired · {r['total_classes']} available"
    return line
