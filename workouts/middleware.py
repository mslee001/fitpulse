from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve, reverse

PUBLIC_PATHS = (
    "/healthz/",
    "/accounts/login/",
    "/accounts/logout/",
    "/accounts/welcome/",   # welcome email set-password links (token-checked)
    "/static/",
    "/api/withings/webhook/",
    "/webhooks/google-health/",
)


def _redirect(request, url_name):
    """A redirect HTMX will follow as a full navigation, not swap into a fragment."""
    if request.headers.get("HX-Request"):
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = reverse(url_name)
        return resp
    return redirect(url_name)


def _forbidden(request):
    if request.headers.get("HX-Request"):
        return HttpResponseForbidden(render(request, "workouts/partials/ai_unavailable.html", {
            "ai_unavailable_reason": "This isn't turned on for your account.",
        }).content)
    resp = render(request, "workouts/access_denied.html", status=403)
    return resp


class LoginRequiredMiddleware:
    """Login, then per-user access, deny by default. Order after login:
    password-change gate → onboarding gate → superuser → core → owner/admin → feature."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check public paths first, before touching request.user — accessing
        # request.user forces the lazy session/auth lookup to evaluate, which
        # can hit the DB. /healthz/ must stay DB-free for anonymous requests.
        for public in PUBLIC_PATHS:
            if request.path.startswith(public):
                return self.get_response(request)

        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")

        denied = self._check_access(request)
        if denied is not None:
            return denied
        return self.get_response(request)

    def _check_access(self, request):
        from .access import (
            ADMIN_URL_NAMES, CORE_URL_NAMES, OWNER_URL_NAMES, access_for, feature_for_url_name, has_feature,
        )
        from .onboarding import ONBOARDING_ALLOWED

        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None   # Django's normal 404
        name = match.url_name
        user = request.user
        access = access_for(user)

        # 08: password-change gate — applies to everyone, superusers included.
        if access.must_change_password and name not in ("password_change", "password_change_done", "logout"):
            return _redirect(request, "password_change")

        # 09: onboarding gate. Superusers are created already set up.
        if access.onboarding_completed_at is None and not user.is_superuser and name not in ONBOARDING_ALLOWED:
            return _redirect(request, "get_started")

        if user.is_superuser or name in CORE_URL_NAMES:
            return None
        if name in OWNER_URL_NAMES or name in ADMIN_URL_NAMES:
            return _forbidden(request)
        slug = feature_for_url_name(name)
        if slug is None or not has_feature(user, slug, access):
            return _forbidden(request)
        return None
