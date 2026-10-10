from django.contrib.auth import views as auth_views
from django.urls import path, include

from workouts.admin_views import PasswordChangeDoneView, PasswordChangeView, WelcomeSetPasswordView
from workouts.demo import demo_login
from workouts.onboarding import LOGIN_TOUR
from workouts.views import health

urlpatterns = [
    path("healthz/", health, name="health"),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="registration/login.html", extra_context={"tour": LOGIN_TOUR},
    ), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(
        next_page="/accounts/login/",
    ), name="logout"),
    path("accounts/welcome/<uidb64>/<token>/", WelcomeSetPasswordView.as_view(), name="welcome_set_password"),
    path("demo/", demo_login, name="demo_login"),
    path("accounts/password/", PasswordChangeView.as_view(), name="password_change"),
    path("accounts/password/done/", PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("", include("workouts.urls")),
]
