from django.urls import path, include

urlpatterns = [
    path("", include("workouts.urls")),
]
