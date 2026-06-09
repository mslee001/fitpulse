"""One-time interactive Withings OAuth2 authentication.

Run this once from the terminal to save tokens to ~/.cadence/withings_tokens.json.
After that, the sync views use the cached tokens automatically (auto-refresh on expiry).

Usage:
    python manage.py withings_login

Prerequisites (set in .env):
    WITHINGS_CLIENT_ID=...
    WITHINGS_CLIENT_SECRET=...
    WITHINGS_REDIRECT_URI=...    (must match what you registered in the Withings developer portal)
"""
import secrets
import urllib.parse
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Authenticate with Withings API and save OAuth tokens for sync views."

    def handle(self, *args, **options):
        from workouts.services.withings_client import WithingsClient

        client = WithingsClient()

        if not client.client_id:
            self.stderr.write(self.style.ERROR(
                "WITHINGS_CLIENT_ID not set. Add it to your .env file."
            ))
            return
        if not client.client_secret:
            self.stderr.write(self.style.ERROR(
                "WITHINGS_CLIENT_SECRET not set. Add it to your .env file."
            ))
            return
        if not client.redirect_uri:
            self.stderr.write(self.style.ERROR(
                "WITHINGS_REDIRECT_URI not set. Add it to your .env file."
            ))
            return

        state = secrets.token_urlsafe(16)
        auth_url = client.get_authorization_url(state)

        self.stdout.write("\nOpen this URL in your browser, authorize, then paste the full callback URL here:")
        self.stdout.write(f"\n  {auth_url}\n")

        callback_url = input("Callback URL: ").strip()
        if not callback_url:
            self.stderr.write(self.style.ERROR("No callback URL entered. Aborting."))
            return

        parsed = urllib.parse.urlparse(callback_url)
        params = urllib.parse.parse_qs(parsed.query)

        returned_state = params.get("state", [None])[0]
        if returned_state != state:
            self.stderr.write(self.style.ERROR(
                f"State mismatch! Expected '{state}', got '{returned_state}'. Possible CSRF. Aborting."
            ))
            return

        code = params.get("code", [None])[0]
        if not code:
            self.stderr.write(self.style.ERROR("No 'code' parameter found in callback URL. Aborting."))
            return

        self.stdout.write("Exchanging authorization code for tokens...")
        try:
            tokens = client.exchange_code(code)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Token exchange failed: {e}"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Success! Withings user ID: {tokens.get('userid', 'unknown')}"
        ))
        self.stdout.write(f"Tokens saved to: {client.token_path}")
        self.stdout.write("You can now use Withings Sync from the nav dropdown.")
