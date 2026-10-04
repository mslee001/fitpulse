"""First-time backfills, run in a background thread.

A full history import takes minutes, well past gunicorn's 120s request
timeout, so it can't run inside the request. There's no task queue: a daemon
thread runs it and a SyncJob row records progress for the Get Started page to
poll. Threads die on Render deploys, so a job still "running" after
SyncJob.STALE_AFTER is reported as failed and can be retried."""
import json
import logging
import threading
import traceback

from django.db import close_old_connections
from django.utils import timezone

from .models import SyncJob, WebhookError
from .sync import _run_google_health_sync_all, _run_peloton_sync_all, _run_withings_sync_all

logger = logging.getLogger(__name__)

ALL_SYNC = {
    "peloton": _run_peloton_sync_all,
    "withings": _run_withings_sync_all,
    "google_health": _run_google_health_sync_all,
}


def latest_job(user, source):
    job = SyncJob.objects.for_user(user).filter(source=source).order_by("-started_at").first()
    return job.refreshed() if job else None


def start_backfill(user, source):
    """Start a daemon thread running ALL_SYNC[source](user). Returns the new
    SyncJob, or None (no new job) if a non-stale running job exists."""
    if source not in ALL_SYNC:
        raise ValueError(f"unknown source {source!r}")
    current = latest_job(user, source)
    if current is not None and current.status == "running":
        return None
    job = SyncJob.objects.create(user=user, source=source)
    threading.Thread(target=_run_job, args=(job.pk,), daemon=True).start()
    return job


def start_withings_subscribe(user, callback_url):
    """Subscribe the user's Withings account to weigh-in notifications in a
    background thread.

    It can't run inside the OAuth callback request: Withings checks the
    callback URL with a HEAD request *before* answering the subscribe call,
    and production's single gunicorn worker is busy with that very request, so
    the check queues until Withings gives up and answers 293 ("callback URL is
    either absent or incorrect"). Seen in production 2026-10-03. From a thread,
    the worker is free to answer the check. Success marks the user's
    WithingsAuth subscribed; failure is recorded at /settings/integrations/errors/."""
    thread = threading.Thread(target=_subscribe_withings, args=(user.pk, callback_url), daemon=True)
    thread.start()
    return thread


def _subscribe_withings(user_pk, callback_url):
    from django.contrib.auth import get_user_model
    from .services.withings_client import WithingsClient

    close_old_connections()
    user = get_user_model().objects.get(pk=user_pk)
    try:
        WithingsClient(user).subscribe_webhook(callback_url)
    except Exception as exc:
        logger.warning("Withings webhook subscribe failed for user %s: %s", user_pk, exc)
        WebhookError.record(source="withings_subscribe", user=user,
                            summary=f"Couldn't turn on Withings weigh-in updates: {str(exc)[:200]}",
                            detail=traceback.format_exc())
    finally:
        close_old_connections()


def _error_from(result):
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return str(result["error"])
    for part in ("wellness", "exercise"):   # Google Health returns both halves
        if isinstance(result.get(part), dict) and result[part].get("error"):
            return f"{part}: {result[part]['error']}"
    return ""


def _run_job(job_pk):
    close_old_connections()   # this thread gets its own DB connection
    job = SyncJob.objects.select_related("user").get(pk=job_pk)
    try:
        result = ALL_SYNC[job.source](job.user)
        error = _error_from(result)
        if error:
            raise RuntimeError(error)
        job.status, job.summary = "done", json.loads(json.dumps(result, default=str))
    except Exception as exc:
        logger.exception("Backfill %s failed for user %s", job.source, job.user_id)
        job.status, job.error = "failed", str(exc)[:500]
        WebhookError.record(source=f"backfill_{job.source}", user=job.user,
                            summary=f"First-time {job.source} import failed", detail=traceback.format_exc())
    finally:
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "summary", "error", "finished_at"])
        close_old_connections()
