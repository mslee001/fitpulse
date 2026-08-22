"""One-time Google Health API webhook subscriber registration.

*** NOT RUN OR LIVE-VERIFIED as of writing this (2026-08-17) — see the
warnings below before executing. ***

This is a one-time setup step, deliberately NOT run automatically on every
deploy (per file 06's own instruction) so it's never accidentally
re-triggered. Local development cannot receive real webhook deliveries —
Google requires a public, verified HTTPS endpoint — so this only makes sense
to run against the deployed Render URL, after that deployment is already
serving workouts/sync.py's google_health_webhook view at
/webhooks/google-health/.

IMPORTANT — credentials this command needs are DIFFERENT from the ones
google_health_login sets up:
  - Subscriber management (projects.subscribers.create) requires the
    "https://www.googleapis.com/auth/cloud-platform" OAuth scope — confirmed
    live against the API reference 2026-08-17. The per-user
    activity/health-data scopes GoogleHealthAuth holds do NOT grant this;
    reusing that token here will 403.
  - You'll need a token with that scope some other way — e.g.
    `gcloud auth login` (as a principal with access to the Cloud project)
    followed by `gcloud auth print-access-token`, or a service account key.
    Set GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN to that token before running this.
  - GOOGLE_HEALTH_PROJECT_NUMBER must be the numeric Cloud project number
    (not the string project ID) — the API path uses `projects/{number}`.

The exact CreateSubscriberPayload request body schema below is a best
effort from the REST reference docs, not confirmed against a live 200
response (the docs page didn't render the full schema when checked). If this
400s with a field-validation error, that's the first thing to check:
https://developers.google.com/health/reference/rest/v4/projects.subscribers/create

Prerequisites (set in .env):
    GOOGLE_HEALTH_PROJECT_NUMBER=...
    GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN=...   (cloud-platform scope, short-lived)
    GOOGLE_HEALTH_WEBHOOK_SECRET=...       (already set — same value the
                                             webhook view checks incoming
                                             Authorization headers against)
"""
import os

import requests
from django.core.management.base import BaseCommand

# Data types our sync functions actually consume — see the
# _GH_WELLNESS_WEBHOOK_TYPES / _GH_EXERCISE_WEBHOOK_TYPES sets in sync.py.
# Only subscribe to types we'll do something with.
SUBSCRIBED_DATA_TYPES = [
    "daily-resting-heart-rate", "heart-rate-variability", "daily-heart-rate-variability",
    "run-vo2-max", "daily-respiratory-rate", "respiratory-rate-sleep-summary",
    "daily-oxygen-saturation", "sleep", "steps", "floors", "active-zone-minutes",
    "exercise",
]


class Command(BaseCommand):
    help = (
        "Register a Google Health webhook subscriber against the deployed "
        "Render URL. One-time setup — NOT for local dev (Google requires a "
        "public verified HTTPS endpoint). UNVERIFIED against a live call as "
        "of writing — read the module docstring before running."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--endpoint-uri",
            default="https://fitpulse-jp2p.onrender.com/webhooks/google-health/",
            help="Public HTTPS URL Google will POST notifications to.",
        )

    def handle(self, *args, **options):
        project_number = os.environ.get("GOOGLE_HEALTH_PROJECT_NUMBER", "")
        admin_token = os.environ.get("GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN", "")
        webhook_secret = os.environ.get("GOOGLE_HEALTH_WEBHOOK_SECRET", "")

        if not project_number:
            self.stderr.write(self.style.ERROR("GOOGLE_HEALTH_PROJECT_NUMBER not set. Add it to your .env file."))
            return
        if not admin_token:
            self.stderr.write(self.style.ERROR(
                "GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN not set. This needs the cloud-platform "
                "OAuth scope — see this file's module docstring for how to get one."
            ))
            return
        if not webhook_secret:
            self.stderr.write(self.style.ERROR("GOOGLE_HEALTH_WEBHOOK_SECRET not set. Add it to your .env file."))
            return

        endpoint_uri = options["endpoint_uri"]
        self.stdout.write(f"Registering subscriber for {endpoint_uri}")
        self.stdout.write(f"Data types: {', '.join(SUBSCRIBED_DATA_TYPES)}")

        url = f"https://health.googleapis.com/v4/projects/{project_number}/subscribers"
        params = {"subscriberId": "fitpulse-webhook"}
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        body = {
            "endpointUri": endpoint_uri,
            "endpointAuthorization": {"secret": webhook_secret},
            "subscriberConfigs": [
                {"dataTypes": SUBSCRIBED_DATA_TYPES, "subscriptionCreatePolicy": "PUBLISH_ON_CREATE"}
            ],
        }

        resp = requests.post(url, params=params, headers=headers, json=body)
        if resp.status_code not in (200, 201):
            self.stderr.write(self.style.ERROR(f"Registration failed ({resp.status_code}): {resp.text}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Subscriber registered: {resp.text}"))
        self.stdout.write(
            "Google will now send a verification handshake to your endpoint "
            "(two POST requests, one with the configured secret, one without) "
            "— check the deployed app's logs to confirm both were answered correctly."
        )
