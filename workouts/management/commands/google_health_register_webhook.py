"""One-time Google Health API webhook subscriber registration.

*** STATUS 2026-08-23: subscriber "fitpulse-webhook" is registered and live
against the deployed Render endpoint, with all 12 data types below. Running
this command again will hit a 409 (subscriber already exists) — to change
the config later, PATCH the existing subscriber instead (ad hoc, not a
built-in flag of this command): `PATCH
https://health.googleapis.com/v4/projects/{project}/subscribers/fitpulse-webhook
?updateMask=subscriberConfigs`, same auth/headers as the create call below,
body `{"endpointUri": ..., "endpointAuthorization": {...}, "subscriberConfigs": [...]}`
— see the CASING HISTORY comment at SUBSCRIBED_DATA_TYPES for exactly how
this was done on 2026-08-23. This file is kept mainly as a record of what
was set up and why, and in case the subscriber ever needs to be recreated
from scratch (e.g. after a delete, or for a fresh Cloud project). ***

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
running this script. (Confirmed live 2026-08-23 with curl against the
deployed endpoint — all three passed.)

This is a one-time setup step, deliberately NOT run automatically on every
deploy so it's never accidentally re-triggered. Local development cannot
receive real webhook deliveries — Google requires a public, verified HTTPS
endpoint — so this only makes sense to run against the deployed Render URL,
after that deployment is already serving the (fixed) webhook view at
/webhooks/google-health/.

IMPORTANT — credentials this command needs are DIFFERENT from the ones
google_health_login sets up:
  - Subscriber management (projects.subscribers.create) needs a token with
    permission to manage Google Health API subscribers on the Cloud
    project. CONFIRMED LIVE 2026-08-23: a personal `gcloud auth login` +
    `gcloud auth print-access-token` is NOT sufficient — it produced a 403
    PERMISSION_DENIED ("The caller does not have permission") on
    subscribers.create even for a project Owner, even with a correctly
    shaped request. A dedicated service account granted the "Google Health
    API Admin" IAM role, with a token minted for that service account, is
    what actually worked.
  - Separately: a personal-credential token from `gcloud auth
    print-access-token` also needs an explicit `X-Goog-User-Project`
    header, or the API rejects it with a *different* 403 complaining about
    "local Application Default Credentials" needing a quota project —
    confusing, since this script never touches ADC, it's a plain bearer
    token over `requests`. Google's backend flags personal-credential
    tokens this way regardless. The header is set below unconditionally;
    it's harmless for a service-account token too.
  - The per-user activity/health-data scopes GoogleHealthAuth holds do NOT
    grant subscriber-management permission; reusing that token here will
    also 403.
  - Set GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN to whichever token you use.
  - GOOGLE_HEALTH_PROJECT_NUMBER must be the numeric Cloud project number,
    not the string project ID — using the ID here is a documented common
    error (400 "Invalid project number in resource name" / 403).

Request body schema matches the live guide's documented example request,
with subscriptionCreatePolicy: "AUTOMATIC" (only "AUTOMATIC" or "MANUAL"
are valid — "PUBLISH_ON_CREATE" from the original draft was never real).
Data type string casing took two attempts to get right — see the comment
at SUBSCRIBED_DATA_TYPES for the full history; short version: kebab-case,
not camelCase, despite the release notes suggesting otherwise.

Prerequisites (set in .env):
    GOOGLE_HEALTH_PROJECT_NUMBER=...
    GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN=...   (subscriber-admin permission —
                                             a service account with the
                                             "Google Health API Admin" IAM
                                             role; a personal gcloud token
                                             is confirmed NOT sufficient)
    GOOGLE_HEALTH_WEBHOOK_SECRET=...       (already set — same value the
                                             webhook view checks incoming
                                             Authorization headers against)
"""
import os

import requests
from django.core.management.base import BaseCommand

# Data types our sync functions actually consume — see the
# _GH_WELLNESS_WEBHOOK_TYPES / _GH_EXERCISE_WEBHOOK_TYPES sets in sync.py.
# Only subscribe to types we'll do something with, AND that the live API
# actually accepts for subscription creation.
#
# CASING HISTORY, both corrections confirmed live against real
# subscribers.create calls (not docs, which disagreed with each other and,
# it turns out, with reality):
#   1. First pass (2026-08-17) used kebab-case, matching the webhooks guide.
#   2. "FIXED" 2026-08-22 to camelCase, matching the API release notes'
#      phrasing (e.g. "dailyRestingHeartRate") — reasonable given the
#      source, but wrong for this endpoint.
#   3. CORRECTED BACK 2026-08-23 after camelCase got 400 INVALID_ARGUMENT
#      for 8 of 12 types. Bisected one data type at a time (each tried
#      alone, both AUTOMATIC and MANUAL policy): every kebab-case string
#      below was individually confirmed valid — created as a real test
#      subscriber, verified via GET, then deleted. The full list was then
#      applied to the actual "fitpulse-webhook" subscriber via PATCH
#      (?updateMask=subscriberConfigs) and reconfirmed via GET. camelCase
#      is wrong for subscribers.create, full stop, despite the release
#      notes; kebab-case (the original webhooks guide's convention) is
#      correct — this resolves the original design prompt's flagged doc
#      disagreement in the guide's favor.
# Whether real NOTIFICATION payloads echo dataType back in kebab-case or
# camelCase is still unconfirmed (no live notification has arrived yet) —
# sync.py's _GH_WELLNESS_WEBHOOK_TYPES matches both defensively.
SUBSCRIBED_DATA_TYPES = [
    "daily-resting-heart-rate", "heart-rate-variability", "daily-heart-rate-variability",
    "run-vo2-max", "daily-respiratory-rate", "respiratory-rate-sleep-summary",
    "daily-oxygen-saturation", "active-zone-minutes", "sleep", "steps", "floors", "exercise",
]


class Command(BaseCommand):
    help = (
        "Register a Google Health webhook subscriber against the deployed "
        "Render URL. One-time setup — NOT for local dev (Google requires a "
        "public verified HTTPS endpoint). Already run successfully as of "
        "2026-08-23 (subscriber 'fitpulse-webhook' exists) — re-running "
        "will 409. Read the module docstring before re-running or adapting."
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
            # Required when admin_token comes from `gcloud auth print-access-token`
            # (user credentials, not a service account) — without it, the API
            # returns 403 PERMISSION_DENIED/SERVICE_DISABLED complaining about
            # "local Application Default Credentials" needing a quota project,
            # even though this request doesn't use ADC at all. This header tells
            # Google which project to bill/quota the call against.
            "X-Goog-User-Project": project_number,
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