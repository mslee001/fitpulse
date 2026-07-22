from django.shortcuts import redirect
from django.urls import reverse

PUBLIC_PATHS = (
    "/healthz/",
    "/accounts/login/",
    "/accounts/logout/",
    "/static/",
    "/api/mobile/",
    "/auth/withings/callback/",
    "/api/withings/webhook/",
)


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Check public paths first, before touching request.user — accessing
        # request.user forces the lazy session/auth lookup to evaluate, which
        # can hit the DB. /healthz/ must stay DB-free for anonymous requests.
        for public in PUBLIC_PATHS:
            if request.path.startswith(public):
                return self.get_response(request)

        if request.user.is_authenticated:
            return self.get_response(request)

        return redirect(f"{reverse('login')}?next={request.path}")
