"""The read-only demo: a public "Explore the demo" sign-in as the seeded demo
user (seed_demo), where nothing is saved and no AI runs live.

- Who: `is_demo(user)` — the non-superuser named settings.DEMO_USERNAME.
- Read-only: LoginRequiredMiddleware refuses anything but page views (and
  sign-out) and BLOCKED_URL_NAMES with `readonly_response`, and runs every other
  demo request inside a transaction that's always rolled back, so even the
  small writes a page view makes never stick.
- AI: llm.guard raises AIDemoOff for the demo user; the AI text the demo shows
  was generated once by `manage.py generate_demo_ai` and is loaded by seed_demo.
"""
import json
from contextlib import contextmanager

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

READ_ONLY_MESSAGE = "This is a read-only demo, so nothing was saved."

# Page views that would reach outside the demo (syncs, account connections) or
# change the account, refused even as GETs. Everything else that isn't a GET is
# refused by method.
BLOCKED_URL_NAMES = {
    "sync_new_workouts", "sync_all_workouts", "sync_withings_new", "sync_withings_all",
    "sync_google_health_new", "sync_google_health_all",
    "sync_garmin_new", "sync_garmin_all", "sync_garmin_wellness",
    "withings_oauth_connect", "withings_oauth_callback",
    "google_health_oauth_connect", "google_health_oauth_callback",
    "password_change", "password_change_done",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_ai_generation_allowed = False


def is_demo(user):
    return bool(getattr(user, "is_authenticated", False) and not user.is_superuser
                and user.username == settings.DEMO_USERNAME)


def demo_user():
    """The demo account when the demo is on and seeded, else None."""
    if not settings.DEMO_ENABLED:
        return None
    return (get_user_model().objects
            .filter(username=settings.DEMO_USERNAME, is_active=True, is_superuser=False).first())


def demo_login(request):
    """POST /demo/ (the sign-in page's "Explore the demo" button): sign in as
    the demo user. POST-only so a link can't silently swap someone's session."""
    if request.method != "POST":
        return redirect("login")
    user = demo_user()
    if user is None:
        messages.info(request, "The demo isn't available right now.")
        return redirect("login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("today")


def blocked(request, url_name):
    """True when the demo user may not do this request at all."""
    if url_name == "logout":
        return False
    return request.method not in SAFE_METHODS or url_name in BLOCKED_URL_NAMES


def readonly_response(request):
    """What a refused demo request gets: HTMX → no swap + an `fp-demo-readonly`
    event (base.html shows a toast); JSON callers → a 403 with an error;
    full-page forms → back where they came from with a message."""
    if request.headers.get("HX-Request"):
        resp = HttpResponse(status=204)
        resp["HX-Trigger"] = json.dumps({"fp-demo-readonly": READ_ONLY_MESSAGE})
        return resp
    accept = request.headers.get("Accept", "")
    if request.path.startswith("/api/") or "application/json" in accept or request.content_type == "application/json":
        return JsonResponse({"error": READ_ONLY_MESSAGE, "demo": True}, status=403)
    messages.info(request, READ_ONLY_MESSAGE)
    back = request.headers.get("Referer")
    if back and url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure()):
        return redirect(back)
    return redirect("today")


def ai_generation_allowed():
    return _ai_generation_allowed


@contextmanager
def allow_ai_generation():
    """Let the demo user call the AI — only for `generate_demo_ai`, which writes
    the saved examples. Process-wide, so never use it inside a web request."""
    global _ai_generation_allowed
    previous, _ai_generation_allowed = _ai_generation_allowed, True
    try:
        yield
    finally:
        _ai_generation_allowed = previous
