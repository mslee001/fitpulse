"""seed_demo deletes a user's data before seeding, so it must never land on a real account."""
import datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from workouts.models import CachedWorkout, GoogleHealthAuth, FoodEntry
from workouts.tests.helpers import make_user


def seed(*args):
    call_command("seed_demo", *args, stdout=StringIO())


def add_food(user):
    return FoodEntry.objects.create(user=user, date=datetime.date.today(), meal="lunch",
                                    raw_text="real lunch", items_json=[], calories=500)


class SeedDemoGuardTests(TestCase):
    def test_new_user_is_created_and_seeded(self):
        seed("--user", "testuser")
        u = get_user_model().objects.get(username="testuser")
        self.assertFalse(u.has_usable_password())
        self.assertTrue(CachedWorkout.objects.filter(user=u).exists())
        self.assertIsNotNone(u.access.onboarding_completed_at)

    def test_refuses_superuser(self):
        owner = make_user("megan", superuser=True)
        entry = add_food(owner)
        with self.assertRaisesMessage(CommandError, "superuser"):
            seed("--user", "megan", "--no-input")
        self.assertTrue(FoodEntry.objects.filter(pk=entry.pk).exists())

    def test_refuses_user_with_a_connected_source(self):
        member = make_user("sam")
        GoogleHealthAuth.objects.create(user=member, access_token="a", refresh_token="r",
                                        token_expires_at=timezone.now())
        entry = add_food(member)
        with self.assertRaisesMessage(CommandError, "Google Health connected"):
            seed("--user", "sam", "--no-input")
        self.assertTrue(FoodEntry.objects.filter(pk=entry.pk).exists())

    def test_existing_data_needs_the_username_typed(self):
        member = make_user("testuser")
        entry = add_food(member)
        with patch("builtins.input", return_value="nope"):
            with self.assertRaisesMessage(CommandError, "Cancelled"):
                seed("--user", "testuser")
        self.assertTrue(FoodEntry.objects.filter(pk=entry.pk).exists())

        with patch("builtins.input", return_value="testuser"):
            seed("--user", "testuser")
        self.assertFalse(FoodEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(CachedWorkout.objects.filter(user=member).exists())

    def test_no_input_reseeds_without_asking(self):
        member = make_user("testuser")
        add_food(member)
        with patch("builtins.input", side_effect=AssertionError("asked")):
            seed("--user", "testuser", "--no-input")
        self.assertTrue(CachedWorkout.objects.filter(user=member).exists())

    def test_user_without_data_is_not_asked(self):
        make_user("testuser")
        with patch("builtins.input", side_effect=AssertionError("asked")):
            seed("--user", "testuser")
