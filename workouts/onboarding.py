"""Get Started onboarding: which setup steps a user still has to do.

Step status is derived from the data each time, never stored — connecting a
source anywhere in the app marks its step done."""
from dataclasses import dataclass

from .access import AI_FEATURES, access_for, has_feature

# Routes a user who hasn't finished setup may still reach (everything else
# redirects to Get Started).
ONBOARDING_ALLOWED = {
    "get_started", "gs_skip", "gs_unskip", "gs_nutrition_profile", "gs_finish", "gs_sync_status", "gs_retry",
    "password_change", "password_change_done", "logout",
    "set_peloton_auth", "set_athlete_profile", "set_ftp", "set_dumbbells",
    "withings_oauth_connect", "withings_oauth_callback",
    "google_health_oauth_connect", "google_health_oauth_callback",
}

# The sign-in page's short tour of the app (icon names from partials/icon.html)
LOGIN_TOUR = [
    {"icon": "dumbbell", "title": "Training",
     "text": "Every Peloton class and watch workout, side-by-side compares and training plans that pick your classes."},
    {"icon": "heart", "title": "Recovery",
     "text": "Sleep, HRV and resting heart rate, rolled into a daily readiness score."},
    {"icon": "scale", "title": "Body",
     "text": "Weigh-ins and body composition, with meds and supplements right on the timeline."},
    {"icon": "utensils", "title": "Nutrition",
     "text": "Log a meal by typing it or snapping a photo. The macros add up for you."},
    {"icon": "sparkles", "title": "Insights",
     "text": "A weekly review and the patterns you'd miss, written by AI."},
    {"icon": "link", "title": "Hands-off syncing",
     "text": "Peloton, Withings and Google Health come in on their own, twice a day."},
]

DATA_SOURCES = ("peloton", "withings", "google_health")
NUTRITION_PROFILE_FIELDS = ("height_cm", "age", "biological_sex", "activity_level", "goal")


@dataclass
class Step:
    key: str            # "password", "peloton", "withings", "google_health", "nutrition_profile", "athlete_profile", "equipment"
    title: str
    status: str         # "done" | "todo" | "skipped" | "blocked"
    required: bool
    blocked_reason: str = ""


def _source_enabled(user, key):
    from .models import Integration
    row = Integration.objects.for_user(user).filter(key=key).first()
    return row is None or row.is_enabled


def steps_for(user):
    from .models import (
        DEFAULT_DUMBBELLS_LB, AthleteProfile, GoogleHealthAuth, NutritionProfile, PelotonAuth, UserSettings,
        WithingsAuth,
    )
    access = access_for(user)
    steps = [Step("password", "Choose your password", "todo" if access.must_change_password else "done", True)]

    peloton = PelotonAuth.for_user(user)
    withings = WithingsAuth.for_user(user)
    google = GoogleHealthAuth.for_user(user)
    sources = [
        ("peloton", "Peloton", bool(peloton and peloton.peloton_user_id and peloton.has_tokens), ""),
        ("withings", "Withings scale", bool(withings and withings.webhook_subscription_active), ""),
        ("google_health", "Google Health", google is not None,
         "" if access.google_test_user_added or google else "Waiting on Megan"),
    ]
    for key, title, done, blocked in sources:
        if done:
            status = "done"
        elif not _source_enabled(user, key):
            status = "skipped"
        elif blocked:
            status = "blocked"
        else:
            status = "todo"
        steps.append(Step(key, title, status, True, blocked if status == "blocked" else ""))

    if has_feature(user, "nutrition", access):
        profile = NutritionProfile.objects.filter(user=user).first()
        done = profile is not None and all(getattr(profile, f) not in (None, "") for f in NUTRITION_PROFILE_FIELDS)
        steps.append(Step("nutrition_profile", "Nutrition profile", "done" if done else "todo", True))

    if any(has_feature(user, slug, access) for slug in AI_FEATURES):
        athlete = AthleteProfile.objects.filter(user=user).first()
        steps.append(Step("athlete_profile", "AI coaching profile",
                          "done" if athlete and athlete.saved_at else "todo", True))

    if has_feature(user, "training", access) or has_feature(user, "strength", access):
        settings_row = UserSettings.for_user(user)
        changed_rack = sorted(settings_row.dumbbells_lb or []) != sorted(DEFAULT_DUMBBELLS_LB)
        steps.append(Step("equipment", "Equipment", "done" if settings_row.ftp or changed_rack else "todo", False))
    return steps


def can_finish(user, steps=None):
    """Every required step done; every data source done or skipped, with at
    least one actually connected."""
    steps = steps if steps is not None else steps_for(user)
    sources = [s for s in steps if s.key in DATA_SOURCES]
    if not any(s.status == "done" for s in sources):
        return False
    if any(s.status not in ("done", "skipped") for s in sources):
        return False
    return all(s.status == "done" for s in steps if s.required and s.key not in DATA_SOURCES)


def missing_steps(user, steps=None):
    """Human-readable list of what's left before Finish is allowed."""
    steps = steps if steps is not None else steps_for(user)
    left = []
    sources = [s for s in steps if s.key in DATA_SOURCES]
    if not any(s.status == "done" for s in sources):
        left.append("Connect at least one data source")
    for s in steps:
        if s.key in DATA_SOURCES:
            if s.status in ("todo", "blocked"):
                left.append(f"{s.title}: connect it or skip it")
        elif s.required and s.status != "done":
            left.append(s.title)
    return left


def pending_after_setup(user):
    """Required non-source steps that became to-do after setup was finished —
    e.g. Megan granted Nutrition later, so the nutrition profile is now missing."""
    access = access_for(user)
    if access.onboarding_completed_at is None or user.is_superuser:
        return []
    return [s for s in steps_for(user)
            if s.required and s.key not in DATA_SOURCES and s.key != "password" and s.status == "todo"]
