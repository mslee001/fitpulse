from django.contrib.auth import views as auth_views
from django.urls import path, include

from workouts.views import health

urlpatterns = [
    path("healthz/", health, name="health"),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="registration/login.html",
    ), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(
        next_page="/accounts/login/",
    ), name="logout"),
    path("", include("workouts.urls")),
]
