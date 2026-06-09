"""One-time interactive Garmin authentication.

Run this once from the terminal to save tokens to ~/.garminconnect.
After that, the sync views use the cached tokens automatically (no MFA needed).

Usage:
    python manage.py garmin_login
"""
import os
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = "Authenticate with Garmin Connect and save tokens for sync views."

    def handle(self, *args, **options):
        from garminconnect import Garmin

        token_dir = os.path.expanduser("~/.garminconnect")

        self.stdout.write("Logging in to Garmin Connect...")
        self.stdout.write(f"Email: {settings.GARMIN_EMAIL}")

        api = Garmin(
            email=settings.GARMIN_EMAIL,
            password=settings.GARMIN_PASSWORD,
            prompt_mfa=self._prompt_mfa,
        )

        os.makedirs(token_dir, mode=0o700, exist_ok=True)

        try:
            # Passing tokenstore causes the library to auto-save tokens after login
            api.login(tokenstore=token_dir)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Login failed: {e}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Tokens saved to {token_dir}"))
        self.stdout.write("You can now use Garmin → Sync from the nav dropdown.")

    def _prompt_mfa(self):
        return input("Enter Garmin MFA code: ").strip()
