"""The sign-in page: a short tour of the app, and no nav that only works signed in."""
from django.test import TestCase
from django.urls import reverse

from workouts.onboarding import LOGIN_TOUR
from workouts.tests.helpers import make_user


class LoginPageTests(TestCase):
    def test_signed_out_page_has_the_tour_and_form_but_no_app_nav(self):
        html = self.client.get(reverse("login")).content.decode()
        for tile in LOGIN_TOUR:
            self.assertIn(tile["title"], html)
        self.assertIn('name="username"', html)
        self.assertIn('id="theme-toggle"', html)
        for app_only in ('id="nav-pills"', 'dropdown-end sync-menu', 'id="nav-burger"', 'id="nav-mobile"', ">Today<"):
            self.assertNotIn(app_only, html)

    def test_signed_in_pages_keep_the_nav(self):
        self.client.force_login(make_user("a", features=["training"]))
        html = self.client.get(reverse("today")).content.decode()
        for app_nav in ('id="nav-pills"', 'dropdown-end sync-menu', 'id="nav-burger"', ">Today<"):
            self.assertIn(app_nav, html)

    def test_bad_password_still_shows_the_error(self):
        make_user("a")
        resp = self.client.post(reverse("login"), {"username": "a", "password": "wrong"})
        self.assertContains(resp, "Invalid username or password.")
