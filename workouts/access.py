"""Per-user feature access. Deny by default: every named route is public, core
(any logged-in user), owner-only, admin-only, or belongs to exactly one feature
in FEATURES — test_access enforces that, so a new route must be classified."""

FEATURES = {
    # slug: {"label", "group", "ai", "url_names", "requires", "note"}
    "training": {
        "label": "Workouts & calendar", "group": "Training", "ai": False,
        "url_names": ["dashboard", "history", "workout_detail", "save_manual_movements", "class_history",
                      "compare", "calendar", "calendar_month", "analytics"],
        "requires": [], "note": None,
    },
    "programs": {
        "label": "Programs", "group": "Training", "ai": False,
        "url_names": ["program_list", "program_new", "program_new_blank", "program_detail", "program_delete",
                      "program_edit", "program_duplicate", "program_start_cycle", "program_backfill",
                      "program_run", "program_progression", "program_running_progression",
                      "program_complete_run", "program_delete_week", "run_week_rate", "program_delete_run",
                      "program_slot_swap"],
        "requires": ["training"], "note": None,
    },
    "strength": {
        "label": "Strength trends", "group": "Training", "ai": False,
        "url_names": ["strength_trends", "set_dumbbells"],
        "requires": ["training"], "note": None,
    },
    "body": {
        "label": "Body composition", "group": "Body", "ai": False,
        "url_names": ["body"], "requires": [], "note": None,
    },
    "interventions": {
        "label": "Interventions & trends", "group": "Body", "ai": False,
        "url_names": ["interventions", "intervention_detail", "intervention_edit", "intervention_end",
                      "intervention_delete", "intervention_quick_dose", "intervention_analysis", "run_analysis",
                      "save_analysis", "saved_analysis_detail", "saved_analysis_delete"],
        "requires": [], "note": None,
    },
    "symptoms": {
        "label": "Symptom log", "group": "Body", "ai": False,
        "url_names": ["symptoms"], "requires": [], "note": None,
    },
    "nutrition": {
        "label": "Nutrition log", "group": "Nutrition", "ai": False,
        "url_names": ["nutrition", "nutrition_targets", "nutrition_log", "nutrition_delete", "nutrition_save_meal",
                      "nutrition_relog", "nutrition_delete_meal", "nutrition_entry_row", "nutrition_edit",
                      "nutrition_analytics", "nutrition_save_suggestion", "hunger_log", "target_accept"],
        "requires": [],
        "note": "Logging food requires Food parsing. Without it, this user can view targets but can't log.",
    },
    "ai_next_workout": {
        "label": "Next-workout recommendation", "group": "AI", "ai": True,
        "url_names": ["next_workout_refresh"], "requires": ["training"], "note": None,
    },
    "ai_day_analysis": {
        "label": "Day analysis", "group": "AI", "ai": True,
        "url_names": [], "requires": [], "note": None,   # inline on day_view / today
    },
    "ai_training_insights": {
        "label": "Training insights & compare narrative", "group": "AI", "ai": True,
        "url_names": ["analytics_generate_insights", "analytics_check_insights", "compare_analysis"],
        "requires": ["training"], "note": None,
    },
    "ai_body_commentary": {
        "label": "Body commentary", "group": "AI", "ai": True,
        "url_names": ["body_commentary_refresh"], "requires": ["body"], "note": None,
    },
    "ai_food_parse": {
        "label": "Food parsing (text & photo)", "group": "AI", "ai": True,
        "url_names": ["nutrition_parse"], "requires": ["nutrition"], "note": None,
    },
    "ai_meal_suggest": {
        "label": "Meal suggestions", "group": "AI", "ai": True,
        "url_names": ["nutrition_suggest"], "requires": ["nutrition"], "note": None,
    },
    "ai_nutrition_insights": {
        "label": "Nutrition insights", "group": "AI", "ai": True,
        "url_names": ["nutrition_insights_refresh", "nutrition_insights_check"],
        "requires": ["nutrition"], "note": None,
    },
    "ai_pattern_insights": {
        "label": "Pattern insights", "group": "AI", "ai": True,
        "url_names": ["insights", "pattern_insights_refresh", "pattern_insights_check"],
        "requires": [], "note": None,
    },
    "ai_weekly_review": {
        "label": "Weekly review", "group": "AI", "ai": True,
        "url_names": ["weekly_review", "weekly_review_check"], "requires": [], "note": None,
    },
    "ai_intervention_interpretation": {
        "label": "Intervention interpretation", "group": "AI", "ai": True,
        "url_names": [], "requires": ["interventions"], "note": None,   # inline in save_analysis
    },
    "ai_program_tools": {
        "label": "Plan import, training plans & retrospectives", "group": "AI", "ai": True,
        "url_names": ["program_new_plan", "program_retrospective", "program_training_plan_new",
                      "program_training_plan_draft", "program_training_plan_status", "program_training_plan_retry",
                      "program_training_plan_swap", "program_training_plan_pick", "program_training_plan_create",
                      "program_training_plan_discard", "program_reassess"],
        "requires": ["programs"], "note": None,
    },
    "ai_chat": {
        "label": "Stats chat", "group": "AI", "ai": True,
        "url_names": ["chat_message_api", "chat_clear_api"], "requires": [],
        "note": "Chat can read all of this user's data, regardless of other feature settings.",
    },
}

GROUPS = ["Training", "Body", "Nutrition", "AI"]
AI_FEATURES = [slug for slug, f in FEATURES.items() if f["ai"]]

CORE_URL_NAMES = {   # any logged-in, active user
    "today", "day_view", "settings", "set_ftp", "set_athlete_profile",
    "integrations_settings", "integration_toggle", "webhook_errors",
    "google_health_oauth_connect", "google_health_oauth_callback",
    "set_peloton_auth",
    "sync_new_workouts", "sync_all_workouts",
    "sync_withings_new", "sync_withings_all",
    "sync_google_health_new", "sync_google_health_all",
    "logout",
    "password_change", "password_change_done",
    # Get Started (09) — also reachable after setup, as a setup guide
    "get_started", "gs_skip", "gs_unskip", "gs_nutrition_profile", "gs_finish", "gs_sync_status", "gs_retry",
    "withings_oauth_connect", "withings_oauth_callback",
}
OWNER_URL_NAMES = {  # superusers only
    "sync_garmin_new", "sync_garmin_all", "sync_garmin_wellness", "garmin_activity_history",
    "catalog_sync_start",
}
ADMIN_URL_NAMES = {      # superusers only: /settings/users/
    "admin_users", "admin_user_new", "admin_user_detail", "admin_user_feature_toggle", "admin_user_ai",
    "admin_user_reset_password", "admin_user_active", "admin_user_reset_onboarding", "admin_user_gh_test_user",
    "admin_user_welcome",
}
PUBLIC_URL_NAMES = {"health", "login", "withings_webhook", "google_health_webhook",
                    "welcome_set_password"}   # the welcome email's link — the user isn't logged in yet

_URL_TO_FEATURE = {name: slug for slug, f in FEATURES.items() for name in f["url_names"]}


def access_for(user):
    from .models import UserAccess
    return UserAccess.objects.get_or_create(user=user)[0]


def has_feature(user, slug, access=None):
    """Whether this user may use this feature right now. Superusers always may."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser:
        return True
    feature = FEATURES.get(slug)
    if feature is None:
        return False
    access = access or access_for(user)
    granted = set(access.features or [])
    if slug not in granted:
        return False
    if feature["ai"] and not access.ai_enabled:
        return False
    # Transitive: ai_program_tools needs programs, which itself needs training.
    return all(has_feature(user, req, access) for req in feature["requires"])


def feature_for_url_name(name):
    return _URL_TO_FEATURE.get(name)
