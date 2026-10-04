"""Revoke a Withings webhook subscription."""
import os

from django.core.management.base import BaseCommand

from workouts.management.user_arg import add_user_argument, resolve_user
from workouts.models import WithingsAuth
from workouts.services.withings_client import WithingsClient


class Command(BaseCommand):
    help = "Revoke a Withings webhook subscription."

    def add_arguments(self, parser):
        parser.add_argument("--appli", type=int, default=1)
        add_user_argument(parser)

    def handle(self, *args, **opts):
        callback_url = os.environ.get("WITHINGS_CALLBACK_URL")
        if not callback_url:
            self.stderr.write("WITHINGS_CALLBACK_URL env var not set.")
            return

        user = resolve_user(opts)
        if not WithingsAuth.for_user(user):
            self.stderr.write(f"No WithingsAuth row for {user.username}.")
            return
        self.stdout.write(str(WithingsClient(user).revoke_webhook(callback_url, appli=opts["appli"])))
