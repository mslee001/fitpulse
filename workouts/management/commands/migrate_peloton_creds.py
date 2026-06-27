"""One-shot: seed PelotonAuth from PELOTON_SESSION_ID and PELOTON_USER_ID env vars."""
import os
from django.core.management.base import BaseCommand
from workouts.models import PelotonAuth


class Command(BaseCommand):
    help = "One-shot: seed PelotonAuth from PELOTON_SESSION_ID and PELOTON_USER_ID env vars."

    def handle(self, *args, **opts):
        session_id = os.environ.get("PELOTON_SESSION_ID", "").strip()
        user_id = os.environ.get("PELOTON_USER_ID", "").strip()

        if not session_id or not user_id:
            self.stderr.write(
                "PELOTON_SESSION_ID and/or PELOTON_USER_ID not set in environment."
            )
            return

        PelotonAuth.objects.update_or_create(
            pk=1,
            defaults={
                "session_id": session_id,
                "user_id": user_id,
                "notes": "Seeded from env vars",
            },
        )
        self.stdout.write(self.style.SUCCESS(
            f"Seeded PelotonAuth row: user_id={user_id}, session_id ends in …{session_id[-4:]}"
        ))
