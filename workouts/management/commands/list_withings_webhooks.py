"""List active Withings webhook subscriptions."""
from django.core.management.base import BaseCommand

from workouts.management.user_arg import add_user_argument, resolve_user
from workouts.models import WithingsAuth
from workouts.services.withings_client import WithingsClient


class Command(BaseCommand):
    help = "List active Withings webhook subscriptions."

    def add_arguments(self, parser):
        parser.add_argument("--appli", type=int, default=1)
        add_user_argument(parser)

    def handle(self, *args, **opts):
        user = resolve_user(opts)
        if not WithingsAuth.for_user(user):
            self.stderr.write(f"No WithingsAuth row for {user.username}.")
            return
        self.stdout.write(str(WithingsClient(user).list_webhooks(appli=opts["appli"])))
