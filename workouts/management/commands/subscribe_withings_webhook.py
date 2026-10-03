"""Subscribe to Withings webhook notifications for weight/body composition."""
import os

from django.core.management.base import BaseCommand

from workouts.management.user_arg import add_user_argument, resolve_user
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
        add_user_argument(parser)

    def handle(self, *args, **opts):
        callback_url = os.environ.get("WITHINGS_CALLBACK_URL")
        if not callback_url:
            self.stderr.write(
                "WITHINGS_CALLBACK_URL env var not set. "
                "Set it to https://fitpulse-jp2p.onrender.com/api/withings/webhook/"
            )
            return

        user = resolve_user(opts)
        if not WithingsAuth.for_user(user):
            self.stderr.write(
                f"No WithingsAuth row for {user.username}. Run withings_login first."
            )
            return

        try:
            WithingsClient(user).subscribe_webhook(callback_url, appli=opts["appli"])
        except RuntimeError as e:
            self.stderr.write(str(e))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Subscribed {user.username} to appli={opts['appli']} at {callback_url}"
        ))
