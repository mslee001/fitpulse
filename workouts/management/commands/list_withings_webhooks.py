"""List active Withings webhook subscriptions."""
import requests
from django.core.management.base import BaseCommand
from workouts.models import WithingsAuth
from workouts.services.withings_client import WithingsClient


class Command(BaseCommand):
    help = "List active Withings webhook subscriptions."

    def add_arguments(self, parser):
        parser.add_argument("--appli", type=int, default=1)

    def handle(self, *args, **opts):
        auth = WithingsAuth.get()
        if not auth:
            self.stderr.write("No WithingsAuth row.")
            return

        client = WithingsClient()
        client._ensure_token_valid()
        auth.refresh_from_db()

        resp = requests.post(
            "https://wbsapi.withings.net/notify",
            headers={"Authorization": f"Bearer {auth.access_token}"},
            data={"action": "list", "appli": opts["appli"]},
            timeout=30,
        )
        resp.raise_for_status()
        self.stdout.write(str(resp.json()))
