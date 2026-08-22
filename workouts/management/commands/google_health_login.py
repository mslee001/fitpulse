"""One-time interactive Google Health API OAuth2 authentication.

Run this once from the terminal to save tokens to the GoogleHealthAuth DB
singleton. After that, sync functions use the cached tokens automatically
(auto-refresh on expiry).

Usage:
    python manage.py google_health_login

Prerequisites (set in .env):
    GOOGLE_HEALTH_CLIENT_ID=...
    GOOGLE_HEALTH_CLIENT_SECRET=...
    GOOGLE_HEALTH_REDIRECT_URI=...   (must match the redirect URI registered
                                       on the OAuth client in Google Cloud Console)

This command does not spin up a local callback server — same pattern as
withings_login.py. The configured redirect URI does not need to resolve to
a real page; after authorizing, copy the full URL your browser lands on
(even if it 404s) and paste it back here.
"""
import secrets
import urllib.parse
from django.core.management.base import BaseCommand
from django.utils import timezone

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# https://developers.google.com/health/scopes
# activity_and_fitness.writeonly added 2026-08-18 to support writing Peloton
# data back into Google Health for workouts where Google's own copy is
# missing fields Peloton has (see _push_peloton_to_google_health in sync.py).
# nutrition.writeonly added 2026-08-19 to support exporting FitPulse FoodEntry
# logs to Google Health (see _push_food_entry_to_google_health in sync.py).
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.writeonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.writeonly",
]


class Command(BaseCommand):
    help = "Authenticate with the Google Health API and save OAuth tokens for sync."

    def handle(self, *args, **options):
        from workouts.services.google_health_client import GoogleHealthClient
        from workouts.models import Integration

        client = GoogleHealthClient()

        if not client.client_id:
            self.stderr.write(self.style.ERROR(
                "GOOGLE_HEALTH_CLIENT_ID not set. Add it to your .env file."
            ))
            return
        if not client.client_secret:
            self.stderr.write(self.style.ERROR(
                "GOOGLE_HEALTH_CLIENT_SECRET not set. Add it to your .env file."
            ))
            return
        if not client.redirect_uri:
            self.stderr.write(self.style.ERROR(
                "GOOGLE_HEALTH_REDIRECT_URI not set. Add it to your .env file."
            ))
            return

        state = secrets.token_urlsafe(16)
        params = {
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "response_type": "code",
            "access_type": "offline",
            "scope": " ".join(SCOPES),
            "prompt": "consent",
            "state": state,
        }
        auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)

        self.stdout.write("\nOpen this URL in your browser, authorize, then paste the full callback URL here:")
        self.stdout.write(f"\n  {auth_url}\n")

        callback_url = input("Callback URL: ").strip()
        if not callback_url:
            self.stderr.write(self.style.ERROR("No callback URL entered. Aborting."))
            return

        parsed = urllib.parse.urlparse(callback_url)
        callback_params = urllib.parse.parse_qs(parsed.query)

        returned_state = callback_params.get("state", [None])[0]
        if returned_state != state:
            self.stderr.write(self.style.ERROR(
                f"State mismatch! Expected '{state}', got '{returned_state}'. Possible CSRF. Aborting."
            ))
            return

        error = callback_params.get("error", [None])[0]
        if error:
            self.stderr.write(self.style.ERROR(f"Google returned an error: {error}"))
            return

        code = callback_params.get("code", [None])[0]
        if not code:
            self.stderr.write(self.style.ERROR("No 'code' parameter found in callback URL. Aborting."))
            return

        self.stdout.write("Exchanging authorization code for tokens...")
        try:
            tokens = client.exchange_code(code)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Token exchange failed: {e}"))
            return

        Integration.objects.filter(key="google_health").update(
            is_authenticated=True, last_synced_at=None
        )

        self.stdout.write(self.style.SUCCESS(
            f"Success! Granted scopes: {tokens.get('scopes', '(none reported)')}"
        ))
        self.stdout.write("Tokens saved to DB (GoogleHealthAuth singleton).")
        self.stdout.write(
            "Reminder: while your Google Cloud OAuth consent screen is in \"Testing\" "
            "publishing status, the refresh token issued above expires after 7 days — "
            "re-run this command if syncs start failing with a re-auth error. "
            f"(Ran {timezone.now().strftime('%Y-%m-%d %H:%M')}.)"
        )
        self.stdout.write("You can now use Google Health Sync from the nav dropdown once enabled in /settings/integrations/.")
