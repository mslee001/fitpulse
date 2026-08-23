"""One-time Google Health API webhook subscriber registration.

*** UPDATED 2026-08-22 against the live webhooks guide
(https://developers.google.com/health/webhooks, last updated 2026-08-18).
Two bugs from the original draft are fixed below — see inline comments at
SUBSCRIBED_DATA_TYPES and subscriptionCreatePolicy. ***

*** PREREQUISITE — do this before running this command, or registration
will fail with FAILED_PRECONDITION: ***
Google performs a synchronous two-step verification handshake against your
endpoint when the subscriber is created. It sends two POSTs with body
{"type": "verification"}:
  1. WITH your configured Authorization header -> your view MUST respond
     200 OK or 201 Created.
  2. WITHOUT any Authorization header -> your view MUST respond
     401 Unauthorized or 403 Forbidden.
Separately, for REAL data notifications (not verification), your view must
respond 204 No Content — not 200. Confirm workouts/sync.py's
google_health_webhook view implements all three of these branches before
running this script.

This is a one-time setup step, deliberately NOT run automatically on every
deploy so it's never accidentally re-triggered. Local development cannot
receive real webhook deliveries — Google requires a public, verified HTTPS
endpoint — so this only makes sense to run against the deployed Render URL,
after that deployment is already serving the (fixed) webhook view at
/webhooks/google-health/.

IMPORTANT — credentials this command needs are DIFFERENT from the ones
google_health_login sets up:
  - Subscriber management (projects.subscribers.create) needs a token with
    permission to manage Google Health API subscribers on the Cloud project
    — Google's own docs recommend a dedicated service account granted the
    "Google Health API Admin" IAM role. For a one-time command run by the
    project's Owner, the simpler path also works: `gcloud auth login` as
    yourself, then `gcloud auth print-access-token`. The per-user
    activity/health-data scopes GoogleHealthAuth holds do NOT grant this;
    reusing that token here will 403.
  - Set GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN to whichever token you use.
  - GOOGLE_HEALTH_PROJECT_NUMBER must be the numeric Cloud project number,
    not the string project ID — using the ID here is a documented common
    error (400 "Invalid project number in resource name" / 403).

Request body schema below is now confirmed against the live guide's
documented example request. Two fields in the original draft were wrong:
  - subscriptionCreatePolicy: only "AUTOMATIC" or "MANUAL" are valid values
    — "PUBLISH_ON_CREATE" is not a real value. Fixed to "AUTOMATIC" below,
    which is what you want (no per-user manual subscription management).
  - Data type strings are camelCase, confirmed via the API release notes
    (e.g. "dailyRestingHeartRate", not "daily-resting-heart-rate"). Fixed
    below.

Prerequisites (set in .env):
    GOOGLE_HEALTH_PROJECT_NUMBER=...
    GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN=...   (subscriber-admin permission,
                                             short-lived if using gcloud)
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
#
# FIXED 2026-08-22: these were kebab-case in the original draft
# ("daily-resting-heart-rate" etc). Confirmed camelCase via the API release
# notes at https://developers.google.com/health/release-notes, which lists
# exactly this style (e.g. "dailyRestingHeartRate", "activeZoneMinutes").
SUBSCRIBED_DATA_TYPES = [
    "dailyRestingHeartRate", "heartRateVariability", "dailyHeartRateVariability",
    "runVo2Max", "dailyRespiratoryRate", "respiratoryRateSleepSummary",
    "dailyOxygenSaturation", "sleep", "steps", "floors", "activeZoneMinutes",
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
        self.stdout.write(self.style.WARNING(
            "Before continuing: confirm google_health_webhook responds 200/201 "
            "to an authorized {\"type\": \"verification\"} POST, 401/403 to an "
            "unauthorized one, and 204 to real notifications. Registration "
            "will fail with FAILED_PRECONDITION otherwise."
        ))
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
                # FIXED 2026-08-22: "PUBLISH_ON_CREATE" is not a valid value —
                # the API only accepts "AUTOMATIC" or "MANUAL". AUTOMATIC is
                # what we want: data flows as soon as a user is both
                # authenticated and covered by this subscriber, with no
                # separate per-user subscription calls needed.
                {"dataTypes": SUBSCRIBED_DATA_TYPES, "subscriptionCreatePolicy": "AUTOMATIC"}
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