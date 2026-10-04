"""One-time interactive Google Health API OAuth2 authentication.

Run this once from the terminal to save tokens to the user's GoogleHealthAuth
row. After that, sync functions use the cached tokens automatically
(auto-refresh on expiry).

Usage:
    python manage.py google_health_login [--user USERNAME]

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

from workouts.management.user_arg import add_user_argument, resolve_user


class Command(BaseCommand):
    help = "Authenticate with the Google Health API and save OAuth tokens for sync."

    def add_arguments(self, parser):
        add_user_argument(parser)

    def handle(self, *args, **options):
        from workouts.services.google_health_client import GoogleHealthClient, build_google_health_auth_url
        from workouts.models import Integration

        user = resolve_user(options)
        client = GoogleHealthClient(user)

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
        auth_url = build_google_health_auth_url(client.redirect_uri, state, client.client_id)

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

        Integration.objects.filter(user=user, key="google_health").update(
            is_authenticated=True, last_synced_at=None
        )

        self.stdout.write(self.style.SUCCESS(
            f"Success! Granted scopes: {tokens.get('scopes', '(none reported)')}"
        ))
        self.stdout.write(f"Tokens saved to DB (GoogleHealthAuth for {user.username}).")
        self.stdout.write(
            "Reminder: while your Google Cloud OAuth consent screen is in \"Testing\" "
            "publishing status, the refresh token issued above expires after 7 days — "
            "re-run this command if syncs start failing with a re-auth error. "
            f"(Ran {timezone.now().strftime('%Y-%m-%d %H:%M')}.)"
        )
        self.stdout.write("You can now use Google Health Sync from the nav dropdown once enabled in /settings/integrations/.")
