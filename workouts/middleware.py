from django.shortcuts import redirect
from django.urls import reverse

PUBLIC_PATHS = (
    "/accounts/login/",
    "/accounts/logout/",
    "/static/",
    "/api/mobile/",
    "/auth/withings/callback/",
)


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            return self.get_response(request)

        for public in PUBLIC_PATHS:
            if request.path.startswith(public):
                return self.get_response(request)

        return redirect(f"{reverse('login')}?next={request.path}")
