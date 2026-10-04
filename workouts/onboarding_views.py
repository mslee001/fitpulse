"""Get Started onboarding page, its HTMX backfill status, and the Withings web
OAuth flow (the Google Health one lives in views.py; this mirrors it)."""
import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .access import access_for
from .background import ALL_SYNC, latest_job, start_backfill
from .models import AthleteProfile, Integration, NutritionProfile, UserSettings
from .onboarding import DATA_SOURCES, can_finish, missing_steps, steps_for

logger = logging.getLogger(__name__)


def safe_next(request, default):
    """The posted/queried `next` URL if it stays on this site, else `default`
    (a URL name or path)."""
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                                require_https=request.is_secure()):
        return nxt
    return default


def _gs_url(step):
    return f"{reverse('get_started')}#{step}"


def get_started(request):
    user = request.user
    Integration.ensure_for_user(user)
    access = access_for(user)
    steps = steps_for(user)
    from .models import PelotonAuth, WithingsAuth, GoogleHealthAuth
    return render(request, "workouts/get_started.html", {
        "steps": {s.key: s for s in steps},
        "step_list": steps,
        "complete": access.onboarding_completed_at is not None or user.is_superuser,
        "can_finish": can_finish(user, steps),
        "missing": missing_steps(user, steps),
        "access": access,
        "peloton_auth": PelotonAuth.for_user(user),
        "withings_auth": WithingsAuth.for_user(user),
        "google_health_auth": GoogleHealthAuth.for_user(user),
        "jobs": {source: latest_job(user, source) for source in DATA_SOURCES},
        "nutrition_profile": NutritionProfile.objects.filter(user=user).first(),
        "athlete": AthleteProfile.for_user(user),
        "experience_choices": AthleteProfile.EXPERIENCE_CHOICES,
        "tone_choices": AthleteProfile.TONE_CHOICES,
        "settings_row": UserSettings.for_user(user),
        "activ_choices": [
            ("sedentary", "Sedentary (desk job, little exercise)"),
            ("light", "Light (1–3 days/wk)"),
            ("moderate", "Moderate (3–5 days/wk)"),
            ("active", "Active (6–7 days/wk)"),
            ("very_active", "Very Active (2× training/day)"),
        ],
    })


def _set_source_enabled(request, source, enabled):
    if source not in DATA_SOURCES:
        return HttpResponseNotFound()
    Integration.ensure_for_user(request.user)
    Integration.objects.for_user(request.user).filter(key=source).update(is_enabled=enabled)
    return redirect(_gs_url(source))


@require_POST
def gs_skip(request, source):
    return _set_source_enabled(request, source, False)


@require_POST
def gs_unskip(request, source):
    return _set_source_enabled(request, source, True)


@require_POST
def gs_nutrition_profile(request):
    """Save just the five fields the calorie target needs. The full targets page
    stays behind the nutrition feature."""
    profile = NutritionProfile.for_user(request.user)
    post = request.POST
    try:
        height = float(post.get("height_cm") or 0) or None
        age = int(post.get("age") or 0) or None
    except ValueError:
        messages.error(request, "Height and age need to be numbers.")
        return redirect(_gs_url("nutrition_profile"))
    profile.height_cm, profile.age = height, age
    if post.get("biological_sex") in ("female", "male"):
        profile.biological_sex = post["biological_sex"]
    if post.get("activity_level") in ("sedentary", "light", "moderate", "active", "very_active"):
        profile.activity_level = post["activity_level"]
    if post.get("goal") in ("loss", "gain", "maintain"):
        profile.goal = post["goal"]
    profile.save()
    messages.success(request, "Nutrition profile saved.")
    return redirect(_gs_url("nutrition_profile"))


@require_POST
def gs_finish(request):
    user = request.user
    steps = steps_for(user)
    if not can_finish(user, steps):
        messages.error(request, "Not quite done: " + "; ".join(missing_steps(user, steps)) + ".")
        return redirect("get_started")
    access = access_for(user)
    access.onboarding_completed_at = timezone.now()
    access.save(update_fields=["onboarding_completed_at", "updated_at"])
    messages.success(request, "You're all set.")
    return redirect("today")


def _status_partial(request, source):
    job = latest_job(request.user, source)
    return render(request, "workouts/partials/gs_sync_status.html", {"job": job, "source": source})


def gs_sync_status(request, source):
    if source not in ALL_SYNC:
        return HttpResponseNotFound()
    return _status_partial(request, source)


@require_POST
def gs_retry(request, source):
    if source not in ALL_SYNC:
        return HttpResponseNotFound()
    start_backfill(request.user, source)
    return _status_partial(request, source)


# ---------------------------------------------------------------------------
# Withings web OAuth
# ---------------------------------------------------------------------------

def withings_oauth_connect(request):
    """GET or POST /auth/withings/connect/ — send the browser to Withings'
    consent page. A full-page redirect (OAuth needs a real navigation)."""
    from .services.withings_client import WithingsClient

    state = secrets.token_urlsafe(32)
    request.session["withings_oauth_state"] = state
    request.session["withings_oauth_next"] = safe_next(request, reverse("integrations_settings"))
    # Explicit save before redirecting off-site — see google_health_oauth_connect.
    request.session.save()
    redirect_uri = request.build_absolute_uri(reverse("withings_oauth_callback"))
    return redirect(WithingsClient(request.user).get_authorization_url(state, redirect_uri=redirect_uri))


def withings_oauth_callback(request):
    """GET /auth/withings/callback/ — Withings sends the browser back here.
    Always redirects on with a message; never renders an error page."""
    from .models import WithingsAuth
    from .services.withings_client import WithingsClient

    nxt = request.session.pop("withings_oauth_next", None) or reverse("integrations_settings")
    try:
        expected = request.session.pop("withings_oauth_state", None)
        if not expected or request.GET.get("state") != expected:
            logger.warning("Withings OAuth state mismatch for user %s", request.user.pk)
            messages.error(request, "Withings connection failed: the security check didn't match. Try Connect again.")
            return redirect(nxt)
        if request.GET.get("error"):
            messages.error(request, f"Withings connection failed: Withings returned an error ({request.GET['error']}).")
            return redirect(nxt)
        code = request.GET.get("code")
        if not code:
            messages.error(request, "Withings connection failed: no authorization code was returned.")
            return redirect(nxt)

        client = WithingsClient(request.user)
        redirect_uri = request.build_absolute_uri(reverse("withings_oauth_callback"))
        tokens = client.request_tokens(code, redirect_uri=redirect_uri)
        userid = tokens.get("userid", "")
        if userid and WithingsAuth.objects.filter(userid=userid).exclude(user=request.user).exists():
            messages.error(request, "That Withings account is already connected to another FitPulse user.")
            return redirect(nxt)
        try:
            with transaction.atomic():
                client._save_tokens()
        except IntegrityError:
            if userid and WithingsAuth.objects.filter(userid=userid).exclude(user=request.user).exists():
                messages.error(request, "That Withings account is already connected to another FitPulse user.")
            else:
                logger.exception("Saving WithingsAuth failed for user %s", request.user.pk)
                messages.error(request, "Withings connection failed because of a database error. "
                                        "Try again, and tell Megan if it keeps happening.")
            return redirect(nxt)

        Integration.ensure_for_user(request.user)
        Integration.objects.for_user(request.user).filter(key="withings").update(is_enabled=True, is_authenticated=True)
        callback_url = getattr(settings, "WITHINGS_CALLBACK_URL", "")
        try:
            if not callback_url:
                raise RuntimeError("WITHINGS_CALLBACK_URL isn't set")
            client.subscribe_webhook(callback_url)
        except Exception as e:
            logger.warning("Withings webhook subscribe failed for user %s: %s", request.user.pk, e)
            messages.warning(request, f"Withings connected, but automatic weigh-in updates couldn't be "
                                      f"turned on ({e}). Ask Megan to check it.")
        start_backfill(request.user, "withings")
        messages.success(request, "Withings connected. Importing your weigh-ins…")
    except Exception as e:
        logger.exception("Withings OAuth callback failed")
        messages.error(request, f"Withings connection failed: {e}")
    return redirect(nxt)
