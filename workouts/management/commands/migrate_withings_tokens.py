"""One-shot: migrate ~/.fitpulse/withings_tokens.json to the WithingsAuth DB row."""
import json
from datetime import datetime, timezone
from pathlib import Path
from django.core.management.base import BaseCommand
from workouts.models import WithingsAuth


class Command(BaseCommand):
    help = "One-shot: migrate ~/.fitpulse/withings_tokens.json to WithingsAuth DB row."

    def handle(self, *args, **opts):
        token_path = Path.home() / ".fitpulse" / "withings_tokens.json"
        if not token_path.exists():
            self.stderr.write(
                f"No token file at {token_path}. "
                "Run withings_login first to create a fresh token."
            )
            return

        data = json.loads(token_path.read_text())

        userid = str(data.get("userid") or data.get("user_id") or "")
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
        expires_at_raw = data.get("expires_at") or data.get("token_expires_at")

        if isinstance(expires_at_raw, (int, float)):
            expires_at = datetime.fromtimestamp(expires_at_raw, tz=timezone.utc)
        elif isinstance(expires_at_raw, str):
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        else:
            raise ValueError(f"Could not parse expires_at: {expires_at_raw!r}")

        WithingsAuth.objects.update_or_create(
            pk=1,
            defaults={
                "userid": userid,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expires_at": expires_at,
            },
        )
        self.stdout.write(self.style.SUCCESS(
            f"Seeded WithingsAuth row from {token_path}. userid={userid}"
        ))
