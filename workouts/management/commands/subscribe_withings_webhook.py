"""Subscribe to Withings webhook notifications for weight/body composition."""
import os
import requests
from django.core.management.base import BaseCommand
from django.utils import timezone
from workouts.models import WithingsAuth
from workouts.services.withings_client import WithingsClient


class Command(BaseCommand):
    help = "Subscribe to Withings webhook notifications for weight (appli=1)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--appli",
            type=int,
            default=1,
            help="Withings data category (1=weight/body comp). Default: 1.",
        )

    def handle(self, *args, **opts):
        callback_url = os.environ.get("WITHINGS_CALLBACK_URL")
        if not callback_url:
            self.stderr.write(
                "WITHINGS_CALLBACK_URL env var not set. "
                "Set it to https://fitpulse-jp2p.onrender.com/api/withings/webhook/"
            )
            return

        auth = WithingsAuth.get()
        if not auth:
            self.stderr.write(
                "No WithingsAuth row. Run migrate_withings_tokens or withings_login first."
            )
            return

        client = WithingsClient()
        client._ensure_token_valid()
        auth.refresh_from_db()

        resp = requests.post(
            "https://wbsapi.withings.net/notify",
            headers={"Authorization": f"Bearer {auth.access_token}"},
            data={
                "action": "subscribe",
                "callbackurl": callback_url,
                "appli": opts["appli"],
                "comment": "FitPulse",
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == 0:
            WithingsAuth.objects.filter(pk=1).update(
                last_subscribed_at=timezone.now(),
                webhook_subscription_active=True,
            )
            self.stdout.write(self.style.SUCCESS(
                f"Subscribed to appli={opts['appli']} at {callback_url}"
            ))
        else:
            self.stderr.write(f"Subscribe failed: {body}")
