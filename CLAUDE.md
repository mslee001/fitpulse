# CLAUDE.md — FitPulse

A Django app (renamed from "Peloton Dashboard") that pulls workout and wellness data from Peloton, Garmin Connect, Withings, and Google Health, and displays it in a dashboard for a small household. Each user has their own data; Megan's account (the first superuser) is the owner/admin. See **Users & Ownership** and **Access Control** below before touching any query.

---

## Project Structure

```
peloton_dashboard/       # Django project root (settings.py, urls.py)
workouts/                # Main app
  models.py              # CachedWorkout + UserSettings + DailyStats + BodyMeasurement + Intervention + DoseChange + SavedAnalysis + NutritionProfile + FoodEntry + SavedMeal + HungerCheck + SideEffectLog + TargetAdjustment + WeeklyReview + WithingsAuth + PelotonAuth + GoogleHealthAuth + Integration + WebhookError + Program* + UserAccess + AIUsage + SyncJob; SQLite locally, Postgres (Neon) in production
  users.py               # get_owner() + UserOwnedQuerySet.for_user() / UserOwnedManager — every user-owned model's manager
  access.py              # FEATURES table, CORE/OWNER/ADMIN/PUBLIC_URL_NAMES, access_for(), has_feature(), feature_for_url_name()
  middleware.py          # LoginRequiredMiddleware: login → password gate → onboarding gate → deny-by-default access check
  context_processors.py  # access(): `can.<feature>`, `is_owner`, `setup_pending` for templates
  onboarding.py          # Get Started: steps_for(), can_finish(), missing_steps(), pending_after_setup(), ONBOARDING_ALLOWED
  onboarding_views.py    # Get Started page + gs_* endpoints, Withings web OAuth, safe_next()
  background.py          # start_backfill(user, source) — first-time history import in a daemon thread, tracked by SyncJob
  admin_views.py         # /settings/users/ (owner only) + the PasswordChangeView that clears the forced-change flag
  llm.py                 # The only path to Anthropic: guard() (feature + budget), MODEL_PRICES, AIUsage logging, batches
  views.py               # All HTML-rendering views only
  sync.py                # All sync logic + sync API endpoints (returns JsonResponse)
  ai.py                  # Anthropic API calls, insights generation, day analysis, next-workout rec, body commentary, intervention interpretation, nutrition parsing/suggestions, compare analysis, pattern insights, weekly review
  analysis.py            # run_intervention_analysis() — shared logic for analyze_intervention command + Trends page
  strength.py            # exercise_history() + recommend() — per-exercise weight trends and next-dumbbell recommendations from the manual exercise log
  nutrition.py           # compute_macro_targets() (Mifflin-St Jeor BMR/TDEE) + recompute_daily_nutrition() rollup + evaluate_target_fit() + get_satisfying_meals()
  services/
    peloton_client.py    # PelotonClient — all Peloton API calls
    garmin_client.py     # GarminClient — Garmin Connect API calls + parsers
    withings_client.py   # WithingsClient — Withings OAuth2 + body measurement API
    google_health_client.py  # GoogleHealthClient — OAuth2 + wellness/exercise data API; shared build_google_health_auth_url()/exchange_google_health_code() helpers
    chat_tools.py         # Tool implementations for the stats chat sidebar; build_tool_dispatch(user) binds them to the requesting user
  management/user_arg.py   # add_user_argument(parser) / resolve_user(opts) — the shared --user USERNAME option (default: owner)
  templatetags/workout_filters.py
  management/commands/
    backfill_ftp.py              # Stamp per-workout FTP from historical values
    garmin_login.py              # One-time interactive Garmin auth
    withings_login.py            # One-time interactive Withings OAuth flow
    google_health_login.py       # One-time interactive Google Health OAuth flow (CLI alternative to the web Reconnect button)
    google_health_register_webhook.py  # Create/patch the Google Health push-notification subscriber
    analyze_intervention.py      # Before/after analysis across wellness + body composition
    sync_daily.py                # Automated daily sync (Peloton + Garmin activities + wellness)
    migrate_withings_tokens.py   # One-time migration of tokens from file to DB
    migrate_peloton_creds.py     # One-time migration of creds from .env to DB
    subscribe_withings_webhook.py  # Subscribe Withings push webhook
    list_withings_webhooks.py    # List active Withings webhook subscriptions
    revoke_withings_webhook.py   # Revoke a Withings webhook subscription
    dedupe_garmin_exercise.py    # Manual sweep to reconcile Garmin/Peloton duplicates
    dedupe_google_health_exercise.py  # Manual sweep to reconcile Google Health/Peloton duplicates
    seed_demo.py                  # Populate demo data (see README Demo Mode)
    seed_programs.py              # Seed built-in structured training programs
    associate_programs.py         # Backfill CachedWorkout → Program associations
    backfill_class_plans.py       # Fetch class exercise plans (class_plan_json) for existing Peloton strength/circuit workouts
templates/workouts/
  base.html              # Shared layout — nav brand is "FITPULSE"
  dashboard.html         # Overview: total workouts, discipline breakdown
  history.html           # Filterable/sortable workout list
  detail_base.html       # Shared detail page layout — sidebar, PR banner, class info; all detail pages extend this
  run_detail.html        # Pace, splits, HR, RUNNING FORM card (Garmin form metrics)
  cycling_detail.html    # Power zones, FTP, cadence, resistance
  strength_detail.html   # HR over time, muscles, exercise sets
  walking_detail.html    # Pace, splits, HR, leaderboard
  detail.html            # Generic fallback (yoga, meditation, etc.)
  class_history.html     # All-time history for a specific class
  analytics.html         # Weekly volume, discipline mix, AI insights
  compare.html           # Side-by-side workout comparison with AI narrative
  calendar.html          # Monthly calendar with workout dots + next-workout AI rec
  day_view.html          # Single-day view: wellness signals + workouts + AI analysis
  body.html              # Body composition + recovery trends; weight chart with intervention/dose annotations + rolling avgs
  trends.html            # Intervention analysis: before/after metrics table, AI interpretation, save/load analyses
  saved_analysis_detail.html  # Full view of a saved SavedAnalysis with AI interpretation
  interventions.html     # Intervention list: dose_summary, Change Dose inline form, Manage Doses link
  intervention_detail.html    # Full dose timeline management: add/edit/end/delete DoseChanges per intervention
  intervention_edit.html      # Edit intervention name/category/dates/notes
  nutrition.html         # Daily food log: macro bars, THIS WEEK table, streak badges, yesterday recap, hunger widget, AI suggestions, saved meals
  nutrition_analytics.html  # Analytics: adherence stats, macro trend chart, day-of-week, top foods, hunger trend chart, symptom summary, AI insights
  nutrition_targets.html # Configure NutritionProfile: height/age/activity/goal/deficit + manual overrides + Target Fit check + adjustment history
  symptoms.html          # Symptom log: chip-select symptom + severity, recent entries, 30-day summary
  insights.html          # Pattern Insights: Sonnet deep-analysis page with weekly cache + HTMX regenerate
  strength_trends.html   # /strength/: volume per logged workout + per-exercise weight trend cards with move-up/stay/drop recommendations
  review.html            # Weekly Review: AI Sonnet review of most recently completed Mon–Sun week; archive of past weeks in collapsible details
  today.html             # Landing page ("/"): today's wellness grid + Activity section + workouts
  settings.html          # FTP setting + dumbbells (strength feature) + AI coaching profile + Account links (password, setup guide, Users for the owner); the hardcoded FTP history table is owner-only
  integrations_settings.html  # Data Sources: enable/disable + Sync All + auth status per source; right column stacks Peloton Session Cookie card + Webhook Errors card
  webhook_errors.html    # Failed webhook-triggered background syncs, newest first, collapsible tracebacks
  program_*.html         # Structured training program pages (list/detail/run/edit/new/new-plan/progression/retrospective) — see "Program Recovery Tracking & Any-Class Slots" and "Configuring Programs" below
  partials/
    insights.html              # Analytics AI insights partial (HTMX polling target)
    workout_list.html          # Workout list rows partial
    nutrition_parse_result.html      # Food parse preview (editable items before confirming); source badge for photo parses ("Read from nutrition label" / "Estimated from photo"); hidden ai_model comes from the view context, not hardcoded
    nutrition_entry_row.html         # Read-only food entry table row
    nutrition_edit_row.html          # Inline edit form for a food entry
    nutrition_suggestions.html       # AI meal suggestion cards with "Log this" + "★ Save" buttons (HTMX)
    nutrition_insights.html          # Nutrition analytics AI insights partial (HTMX)
    pattern_insights.html            # Pattern insights partial (HTMX target for /insights/ page)
    weekly_review_content.html       # Weekly review body partial (HTMX target for check/regenerate)
    integration_row.html             # One data-source row on the Integrations page (HTMX target for toggle)
    chat_message_pair.html, chat_error.html, chat_cleared.html  # Stats chat sidebar partials (HTMX)
    run_week_rating.html             # Program run-week rating widget partial
    program_slot_row.html            # One slot row on the program edit page (existing, blank, and JS-template rows)
    manual_movements.html            # Class exercise plan + hand-entered exercise log card (strength_detail.html, detail.html)
    ai_unavailable.html              # "This month's AI limit" / "not turned on" note shown in place of an AI card
    athlete_profile_form.html        # AI coaching profile form (settings.html and get_started.html)
    admin_feature_row.html, admin_ai_form.html, admin_gh_test_user.html  # HTMX rows on the admin user detail page
    gs_sync_status.html              # Backfill status; polls every 5s only while a SyncJob is running
    gs_pill.html, gs_peloton_form.html, gs_withings_button.html  # Get Started card pieces
    messages.html                    # Django messages block
  get_started.html       # Get Started onboarding / setup guide (one card per step)
  admin_users.html, admin_user_new.html, admin_user_detail.html, admin_user_password.html  # Owner's Users pages; the password page is the one-time temp-password display
  access_denied.html     # 403 page for routes not turned on for the user
templates/registration/password_change.html, password_change_done.html  # Forced/voluntary password change
static/css/main.css      # All styles — single flat file, CSS variables
```

---

## Key Architecture

### Users & Ownership
- **Owner**: `workouts.users.get_owner()` — the first superuser by pk (Megan). All pre-multi-user data was assigned to her by migrations 0027/0030. Superusers bypass feature checks and AI budgets.
- **Every user-facing row has an owner.** Models with a direct `user` FK use `objects = UserOwnedManager()`, which adds `.for_user(user)` (`for_user(None)` / anonymous → empty queryset, never "everything"): `CachedWorkout`, `DailyStats`, `BodyMeasurement`, `Intervention`, `SavedAnalysis`, `FoodEntry`, `SavedMeal`, `HungerCheck`, `SideEffectLog`, `TargetAdjustment`, `WeeklyReview`, `Program`, `Integration`, `AIUsage`, `SyncJob`, `WebhookError` (nullable `user`). Children are owned through their parent and have no FK: `DoseChange` (→ Intervention), `ProgramWeek`/`ProgramSlot`/`ProgramRun` (→ Program), `RunWeek` (→ ProgramRun), `ProgramWorkout`/`ProgramRecovery` (→ RunWeek). Per-user settings/auth rows are OneToOne: `UserSettings`, `NutritionProfile`, `AthleteProfile` (`Model.for_user(user)` creates), `WithingsAuth`, `PelotonAuth`, `GoogleHealthAuth` (`Model.for_user(user)` returns None if not connected). There are no `.get()` singletons and no `pk=1` — `for_user` raises `ValueError` without a real user rather than falling back to the owner.
- **Rules**: every read goes through `.for_user(user)` (or a parent filter like `RunWeek.objects.filter(run__program__user=user)`); every write sets `user=` explicitly (`Model.objects.create(user=…)`, `get_or_create(user=…, date=…)` — `.for_user(u).create()` does NOT set the user); every ID in a URL is looked up through the user's queryset (`get_object_or_404(Model.objects.for_user(request.user), pk=pk)`), so someone else's object is a 404, not a 403; ids posted in a body (`related_meal_id`, `intervention_id`, compare `ids`) resolve through the user's queryset and a miss is invalid input. Helpers that query take `user` first; never read the user from a global or thread-local. Functions handed a workout/program/run use its owner (`workout.user`, `run.program.user`).
- **Tests**: `workouts/tests/helpers.py` has `make_user(username, superuser=False, features=None, ai=False, set_up=True)` and `TwoUserTestCase` (`self.a` owner, `self.b` regular with every non-AI feature; logged-in `client_a`/`client_b`; Anthropic and Garmin network calls blocked). `test_isolation.py` (every ID route 404s for another user, list pages leak nothing), `test_sync_isolation.py` (same-time workouts of two users never merge, per-user locks, sync_daily isolation), `test_webhook_routing.py`, and `test_no_singleton_pk.py` (fails on any `pk=1` or `<Singleton>.get()` outside migrations/tests). `workouts/tests/__init__.py` patches Django 4.2's `BaseContext.__copy__` so the test client can render templates on Python 3.14.

### Access Control
- **`UserAccess`** (OneToOne, `related_name="access"`, created for every new user by a `post_save` signal with everything off): `features` (list of slugs), `ai_enabled` (master AI switch), `monthly_ai_budget_usd` (null = no cap), `must_change_password`, `onboarding_completed_at`, `google_test_user_added`. `access.access_for(user)` gets or creates it.
- **`access.FEATURES`** (`slug: {label, group, ai, url_names, requires, note}`):

  | Slug | Group | AI | Requires |
  |---|---|---|---|
  | `training` (workouts, compare, calendar, analytics) | Training | | |
  | `programs` | Training | | training |
  | `strength` (strength trends, dumbbells) | Training | | training |
  | `body` | Body | | |
  | `interventions` (incl. Trends analysis) | Body | | |
  | `symptoms` | Body | | |
  | `nutrition` (log, targets, analytics, hunger) | Nutrition | | |
  | `ai_next_workout` | AI | ✓ | training |
  | `ai_day_analysis` (inline on day view / Today) | AI | ✓ | |
  | `ai_training_insights` (analytics + compare narrative) | AI | ✓ | training |
  | `ai_body_commentary` | AI | ✓ | body |
  | `ai_food_parse` | AI | ✓ | nutrition |
  | `ai_meal_suggest` | AI | ✓ | nutrition |
  | `ai_nutrition_insights` | AI | ✓ | nutrition |
  | `ai_pattern_insights` (/insights/) | AI | ✓ | |
  | `ai_weekly_review` (/review/) | AI | ✓ | |
  | `ai_intervention_interpretation` (inline in save_analysis) | AI | ✓ | interventions |
  | `ai_program_tools` (plan import, retrospectives) | AI | ✓ | programs |
  | `ai_chat` (stats chat sidebar) | AI | ✓ | |

  `has_feature(user, slug)`: superuser → True; inactive/anonymous → False; slug not granted → False; AI slug with `ai_enabled` off → False; `requires` are checked transitively.
- **URL sets**: `PUBLIC_URL_NAMES` (healthz, login, the two webhooks, the welcome set-password link), `CORE_URL_NAMES` (any logged-in user: Today, day view, settings, integrations, sync endpoints, OAuth connect/callbacks, password change, Get Started), `OWNER_URL_NAMES` (Garmin syncs + Garmin history), `ADMIN_URL_NAMES` (`/settings/users/` routes). Every other route belongs to exactly one feature's `url_names`. **Adding a route requires classifying it** — `test_access.RouteClassificationTests` fails on any named route that's in zero or several lists.
- **Middleware** (`LoginRequiredMiddleware`), in order: public path (before touching `request.user`, so `/healthz/` stays DB-free) → login → password-change gate (`must_change_password` → `password_change`; applies to everyone) → onboarding gate (no `onboarding_completed_at`, non-superuser → `get_started` unless the route is in `ONBOARDING_ALLOWED`) → superuser passes → core passes → owner/admin routes 403 → feature check (unclassified = 403). 403s render `access_denied.html`; HTMX requests get a short partial; HTMX redirects use `HX-Redirect`.
- **Templates**: the `workouts.context_processors.access` processor provides `can` (`{% if can.nutrition %}`), `is_owner`, and `setup_pending`. Nav groups hide when none of their items are visible; Garmin sync items are owner-only; the chat sidebar needs `can.ai_chat`; every AI card/button is wrapped in its feature.
- **Inline AI** (generated inside a page view, so the middleware can't see it): `day_view`/`today_page` (`ai_day_analysis`), calendar/Today next-workout card (`ai_next_workout`), `body_view` (`ai_body_commentary`, pattern teaser needs `ai_pattern_insights`), `save_analysis_api` (`ai_intervention_interpretation`), analytics/nutrition-analytics auto batches, the program run retrospective. Each checks `has_feature` first; `llm.guard` is the backstop.

### Admin Users Page
- Routes (owner only, also checked in each view): `/settings/users/` (`admin_users` — username, active, last login, setup, feature count, AI this month, connection dots with Google Health amber at 6 days / red at 7), `/settings/users/new/` (`admin_user_new`), `/settings/users/<pk>/` (`admin_user_detail`: account, features grid, AI switch + budget + this month's per-feature usage, setup steps + "Google test user added", connections + latest SyncJob + error count), and POST endpoints `…/feature/<slug>/`, `…/ai/`, `…/reset-password/`, `…/active/`, `…/reset-onboarding/`, `…/gh-test-user/`. Linked from `/settings/` and the Training ▸ Settings nav section (owner only).
- **Welcome email**: an optional email on New user (or the email form on the detail page, `admin_user_welcome`, which saves the address and sends/resends) triggers `admin_views.send_welcome_email(request, user)`: a one-time set-password link built from Django's password-reset tokens (`PASSWORD_RESET_TIMEOUT` = 3 days; dead once the password changes or they log in), rendered from `registration/welcome_email.txt` / `_subject.txt`. Never a password in the email. The link (`accounts/welcome/<uidb64>/<token>/`, `WelcomeSetPasswordView`, public URL `welcome_set_password` and `PUBLIC_PATHS` entry) lets them choose a password, clears `must_change_password`, logs them in (`post_reset_login`) and the onboarding gate takes them to Get Started. A failed send never blocks account creation — the page says so and the temp password is the fallback. Sent via Gmail SMTP when `EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` are set; otherwise the console backend prints it.
- **Temporary passwords**: creating a user (or Reset password) generates `secrets.token_urlsafe(9)` (12 characters), sets `must_change_password=True`, and shows it once on `admin_user_password.html` with a Copy button. It's never stored — not in the session, messages or logs. New users get presets (Nothing, Nutrition only, Training only, Everything except AI, Everything), `ai_enabled` if the preset has an AI slug, and `Integration.ensure_for_user`.
- **Forced password change**: `accounts/password/` (`PasswordChangeView` in admin_views.py clears the flag) and `accounts/password/done/`. Django's four standard `AUTH_PASSWORD_VALIDATORS` are on.
- You can't deactivate yourself or the last active superuser.
- **Privacy rule**: admin pages show account, feature, AI-spend (counts and cost only, never prompt/response text), onboarding and connection status — never anyone's workouts, daily stats, body, nutrition, intervention or symptom data. `test_admin_users.test_admin_pages_show_no_health_data` enforces it.

### Onboarding (Get Started)
- `/get-started/` (`get_started`, onboarding_views.py). Steps are derived each time by `onboarding.steps_for(user)`, never stored:

  | Step | Shown when | Done when | Required |
  |---|---|---|---|
  | `password` | always | `must_change_password` is off | yes |
  | `peloton` | always | `PelotonAuth` with `peloton_user_id` | one source |
  | `withings` | always | `WithingsAuth` and `webhook_subscription_active` | one source |
  | `google_health` | always | `GoogleHealthAuth` exists; **blocked** ("Waiting on Megan") until `google_test_user_added` | one source |
  | `nutrition_profile` | has `nutrition` | height, age, sex, activity, goal set | yes |
  | `athlete_profile` | has any AI feature | `AthleteProfile.saved_at` set (stamped by `set_athlete_profile`) | yes |
  | `equipment` | has `training` or `strength` | FTP set or dumbbells changed from default | no |

  A data source is `skipped` when its `Integration.is_enabled` is off (`gs_skip`/`gs_unskip`). `can_finish(user)`: every required step done, every source done or skipped, at least one source done. `gs_finish` stamps `onboarding_completed_at` only then.
- **Gate**: until `onboarding_completed_at` is set, non-superusers are redirected to Get Started except for `ONBOARDING_ALLOWED` (the gs_* routes, password change, logout, the profile/FTP/dumbbell/Peloton endpoints and both OAuth flows). Those endpoints honor a hidden `next` (validated by `onboarding_views.safe_next` with `url_has_allowed_host_and_scheme`) so they return to `/get-started/#<step>`. After setup the page stays reachable as a **setup guide** ("Setup complete", no Finish button), linked from `/settings/` and Integrations.
- **Newly granted features**: `pending_after_setup(user)` lists required steps that became to-do after setup; base.html shows a dismissible "Finish setting up …" banner per step (dismissal in `localStorage`, a convenience only).
- **Backfills**: `background.start_backfill(user, source)` (peloton/withings/google_health → `_run_*_sync_all(user)`) runs in a daemon thread with its own DB connection and records a `SyncJob` (`running`/`done`/`failed`, `summary`, `error`). Returns None if a non-stale job is already running. A job still running after `SyncJob.STALE_AFTER` (90 min — threads die on Render deploys) reads as failed ("Interrupted…"). Failures are also `WebhookError.record(source="backfill_<source>", user=…)`. Started by the first Peloton connection, the Withings callback and a first Google Health connection (not by reconnects). `partials/gs_sync_status.html` polls `gs_sync_status` every 5s **only while running** (keeps Neon able to scale to zero); failed jobs get a Retry (`gs_retry`).

### Adding a Household Member (runbook)
1. `/settings/users/` → **New user** → enter their email and pick a preset. They get a welcome email with a set-password link; the temporary password shown once is only the fallback if the email doesn't arrive.
2. Ask for the Google account they use for Google Health / Fitbit; add it in Google Cloud console → APIs & Services → OAuth consent screen → **Test users**; then tick **Google test user added** on their admin page.
3. They log in, choose a password, and land on **Get Started**: paste their Peloton cookie, Connect Withings, Connect Google Health (expect Google's "unverified app" warning — Advanced → Go to FitPulse), fill in the profiles, **Finish setup**.
4. Tune their features and AI budget on their admin page any time.

### Auth
- **Peloton**: cookie-only. The user pastes `peloton_session_id`; `set_peloton_auth` validates it against `/api/me` (`PelotonClient.fetch_me`, reads only `id` + `username`) and stores `peloton_user_id` + `peloton_username` on the user's `PelotonAuth` row (`peloton_user_id` was renamed from `user_id` so it can't clash with the Django `user` FK). `/auth/login` is dead (403). Rotate via `/settings/integrations/` ("Peloton Session Cookie" card) or Get Started; the first connection starts a background history import, a rotation doesn't. `PelotonAuthError` raised on 403 with link to `/settings/integrations/`. A unique constraint (`uniq_peloton_user_id`) stops two users sharing a Peloton account.
- **Garmin**: owner-only (hidden for everyone else; its sync endpoints return 403 and every Garmin `_run_*` returns `{"error": "Garmin is owner-only"}` for non-superusers). `garminconnect` lib from `zpython-garminconnect-master/`. First-time: `venv/bin/python3 manage.py garmin_login`. Tokens saved to `~/.garminconnect/` and auto-refresh. Never attempt password login from a web request.
- **Withings**: OAuth 2.0, per user. Web flow: `/auth/withings/connect/` → Withings → `/auth/withings/callback/` (`withings_oauth_connect`/`_callback` in onboarding_views.py; redirect URI built from the request; callback is behind login, not public). On success it saves the user's tokens (refusing a Withings account already linked to another user — `uniq_withings_userid`), subscribes the webhook (`WithingsClient.subscribe_webhook(settings.WITHINGS_CALLBACK_URL)`), and starts a backfill. CLI alternative: `venv/bin/python3 manage.py withings_login [--user USERNAME]`. Tokens on the user's `WithingsAuth` row. Auto-refreshes 5 min before expiry. Credentials in `.env`: `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `WITHINGS_REDIRECT_URI` (CLI only), `WITHINGS_CALLBACK_URL`.
- **Google Health**: OAuth 2.0. First-time (CLI): `venv/bin/python3 manage.py google_health_login`. Also reconnectable from the UI at `/settings/integrations/` → "Reconnect" (see Google Health OAuth Reconnect section below) — same underlying flow either way, via shared helpers in `services/google_health_client.py`. Tokens saved on the user's `GoogleHealthAuth` row. A non-owner can't start the flow until the owner ticks "Google test user added" for them on the admin page (Google only lets listed test users through the consent screen). Refresh tokens on Google's consent screen expire after 7 days while the app is in "Testing" status, so reconnecting periodically is expected, not a bug. Credentials in `.env`: `GOOGLE_HEALTH_CLIENT_ID`, `GOOGLE_HEALTH_CLIENT_SECRET`, `GOOGLE_HEALTH_REDIRECT_URI` (used by the CLI login command; the web flow builds its redirect URI dynamically from the current request instead).
- **Python**: venv uses Python 3.14 (Homebrew). Always `venv/bin/python3 manage.py ...`.

### Local Cache (`CachedWorkout`)
Owned by `user` (FK); unique on (`user`, `workout_id`) — `uniq_workout_user_workout_id`. Never query either API in real time for list views — sync first, then read from DB. Detail pages fall back to a live Peloton perf-graph fetch only when the viewing user has a Peloton connection (`_peloton_client_or_none`).

**Key computed properties (use these in templates, not raw fields):**
- `effort_points` — from `performance_graph_json.effort_zones.total_effort_points`. Matches the value shown on detail pages. Prefer over `effort_score` or `average_effort_score`.
- `heart_rate_avg_best` — model `heart_rate_avg` if set, else `performance_graph_json.metrics_by_slug.heart_rate.average_value`. Many Peloton runs have null `heart_rate_avg` in the list API; the perf graph is authoritative.
- `leaderboard_pct` — computed from rank/total.
- `avg_pace_display` — formatted MM:SS/mi from `avg_pace_seconds`.
- `external_url` — link to the workout on Peloton.com or Garmin Connect. Garmin workouts use the numeric ID (stripped of `"garmin_"` prefix). Safe to use in any template.

**Flat model fields populated from `performance_graph_json`** (backfilled via `_extract_perf_fields()` in sync.py, also written on every new perf graph fetch):
- `calories`, `distance_miles`, `heart_rate_avg`, `avg_pace_seconds` — populated from `summaries`, `average_summaries`, and `metrics_by_slug` in the perf graph. The Peloton list API does NOT return these for running/walking workouts; the perf graph is the source of truth. These fields are what make VS. YOUR AVERAGES aggregations work.

**Garmin-specific fields:**
- `source` — `"peloton"` or `"garmin"`. Garmin IDs prefixed `"garmin_"`.
- `exercise_sets_json` — strength sets: `{order, exercise, exercise_key, reps, weight_kg, duration_seconds}`
- `performance_graph_json` — time-series + splits (Garmin) or Peloton perf graph

**Running form fields** (populated by `_augment_peloton_run` during Garmin sync):
- `run_cadence_avg` — steps/min, **walking filtered out** (≥140 spm threshold from `directDoubleCadence` time-series). `avg_cadence` retains the raw Garmin summary value (includes walking).
- `stride_length_avg` — cm, from `avgStrideLength`
- `vertical_oscillation_avg` — cm, from `avgVerticalOscillation`
- `vertical_ratio_avg` — %, from `avgVerticalRatio`
- `ground_contact_time_avg` — ms, from `avgGroundContactTime`

**Class exercise plan + manual exercise log** (`class_plan_json`, `manual_movements_json`): Peloton's Movement Tracker only records some classes (184/317 strength workouts; 0/22 "circuit"), so `movements` is often empty. `class_plan_json` is the class's programmed exercises — `[{"name": segment, "metrics_type", "length", "exercises": [{"name", "appearances"}]}]`, flattened by `parse_class_plan()` in `peloton_client.py` from `/api/ride/{ride_id}/details` (`segments → segment_list → subsegments_v2 → movements`; rests, demos, transitions and pace cues dropped; `appearances` = number of blocks the exercise is in, a rough sets proxy). It's class-level, not what you did. Fetched for `strength`/`circuit` workouts in `_fetch_and_store_details` (one request per distinct `ride_id` per call); backfill existing ones with `manage.py backfill_class_plans [--dry-run] [--force]`. `manual_movements_json` is `[{"name", "sets", "reps", "weight_lb", "notes", "effort"?}]` (`effort` ∈ easy/right/hard/fail, optional), entered on the "CLASS EXERCISES" card via `POST /workout/<id>/movements/` (`save_manual_movements`; rows with no numbers/notes are dropped, empty submit clears). It is deliberately **not** in `DETAIL_FIELDS`, so detail re-syncs (which overwrite `movements`) never touch it. The card shows only when `movements` is empty (`_manual_movement_context()` in views.py), with a per-row Volume column and Total Volume footer for saved rows (from `CachedWorkout.manual_log_rows` — same dumbbell/per-side rules as the Compare page; the save message also reports the total); the AI next-workout prompt falls back tracker names → manual log → class plan. Manual numbers feed program Load progression via `_workout_exercise_loads()` when `movements` is empty (timed rows skipped).

**Strength trends & weight recommendations** (`workouts/strength.py`, `/strength/` → `strength_trends`, Training ▸ Strength): `exercise_history()` groups manual-log rows across all workouts by normalized exercise name; `recommend()` steps through the user's dumbbell rack (`UserSettings.dumbbells_lb`, edited on `/settings/` → Dumbbells card via `set_dumbbells`; defaults to `DEFAULT_DUMBBELLS_LB` = 2, 5, 7, 10, 12, 15, 20, 25, 30, 35, 40, 45) — Easy → next dumbbell up, Just right twice in a row at the same weight → up, Hard → stay, Couldn't finish → one down, unrated → up after two sessions at a weight with no Hard/Fail; the heaviest dumbbell is "max" (add reps/tempo). Bodyweight rows get none; weighted timed rows (carries) do. The Class Exercises card shows "Next ↑ 35 lb" on logged rows (history up to that workout) and "Try …" + a weight placeholder on unlogged plan rows. Weight is per dumbbell throughout.

**Hand-corrected workouts** (`user_corrected`): when a Peloton session is wrong at the source (e.g. a Tread reboot left a ghost session with ~0 cal and a start time after the real work), correct the row in the shell and set `user_corrected=True`. Peloton syncs (`_upsert_page`, `_fetch_and_store_performance`, `apply_detail`'s HR zones) then skip `CachedWorkout.CORRECTABLE_FIELDS` (start time, duration, calories, HR, HR zones, distance, pace) for that row; everything else still refreshes. Once the corrected time span covers the watch's entries, `_reconcile_google_health_duplicates()` removes them as duplicates. There's no UI for this yet.

**Leaderboard sync tracking:**
- `leaderboard_synced_at` — stamped after every leaderboard detail sync attempt (success or null-result). Used to prevent infinite re-syncing of workouts that Peloton returns null rank for. `raw_data__has_leaderboard_metrics=True` is the reliable sentinel for whether a discipline can have leaderboard data (cycling/running/walking = True; strength/yoga/meditation = False).

### DailyStats Model
One row per user per calendar day (`user` FK; unique on (`user`, `date`) — `uniq_dailystats_user_date`). Stores Garmin wellness data synced via `GarminClient.get_wellness_data()`, Google Health wellness data synced via `_run_google_health_wellness_sync()`, plus Withings body composition aggregates.

**Garmin wellness fields:**
- Body battery: `body_battery_json` (time series), `body_battery_high`, `body_battery_low`, `body_battery_start`, `body_battery_end`, `body_battery_charge`, `body_battery_drain`
- Sleep: `sleep_score`, `sleep_seconds`, `sleep_deep_seconds`, `sleep_light_seconds`, `sleep_rem_seconds`
- HRV: `hrv_weekly_avg`, `hrv_last_night`, `hrv_status` (BALANCED/UNBALANCED/POOR), `hrv_min`, `hrv_max`
- Recovery: `resting_hr`, `stress_avg`, `stress_max`, `stress_rest_minutes`, `stress_low_minutes`, `stress_medium_minutes`, `stress_high_minutes`
- Training load: `training_status`, `training_load` (acute), `load_focus_anaerobic`, `load_focus_high_aerobic`, `load_focus_low_aerobic`
- Readiness: `training_readiness_score` (0–100), `training_readiness_label`
- Activity volume: `steps`, `floors_climbed`, `active_calories`, `total_calories`, `bmr_calories`, `moderate_intensity_minutes`, `vigorous_intensity_minutes`
- Goals: `steps_goal`, `floors_climbed_goal`, `intensity_minutes_goal`
- Fitness markers: `vo2_max_running`, `vo2_max_cycling`, `fitness_age`
- Respiratory: `respiration_avg`, `respiration_waking_avg`, `respiration_sleep_avg`, `spo2_sleep_avg`, `spo2_sleep_low`
- AI: `ai_day_analysis`, `ai_day_generated_at` (cached 7 days), `ai_next_workout`, `ai_next_workout_generated_at` (cached 24h)
- `synced_at` — last wellness sync timestamp

**Withings body composition fields** (daily aggregate — earliest weigh-in of the day):
- `weight_lb`, `fat_mass_lb`, `fat_free_mass_lb`, `muscle_mass_lb`, `hydration_lb`, `bone_mass_lb`, `fat_ratio_pct`
- `weight_count` — number of weigh-ins that day
- `weight_synced_at` — last Withings sync timestamp

**Nutrition rollup fields** (recomputed by `recompute_daily_nutrition()` in nutrition.py whenever a FoodEntry is added/edited/deleted):
- `cal_total`, `protein_g_total`, `carbs_g_total`, `fat_g_total`, `fiber_g_total`

**Sleep score quirk**: Garmin API returns sleep score as `{'value': 82, 'qualifierKey': 'GOOD'}` not a plain int. The `_num()` helper in `get_wellness_data` unwraps both forms.

**Google Health wellness fields** (synced via `_run_google_health_wellness_sync()` in sync.py; several overlap with the Garmin fields above by design — Google Health can populate the same column when Garmin is disabled/unavailable):
- `wellness_source` — which source last wrote this row's wellness fields (`"garmin"` / `"google_health"`)
- `google_health_synced_at` — last Google Health wellness sync timestamp. Not the same signal as `synced_at` (Garmin-only) — use `has_wellness_data` to check either.
- `active_zone_minutes` — Google's Active Zone Minutes metric
- `skin_temp_c`, `skin_temp_deviation_c` — overnight skin temperature + deviation from personal baseline
- `hr_zone_light_minutes`, `hr_zone_moderate_minutes`, `hr_zone_vigorous_minutes`, `hr_zone_peak_minutes` — time-in-zone minutes, rendered as the HR Zone bars on Day view/Today
- `sedentary_minutes`
- `resting_hr_baseline`, `sleep_baseline_seconds` — trailing 30-day personal averages, inputs to `compute_readiness_proxy()` below (not shown directly in the UI)
- `wellness_days_synced` — rolling count of recent synced wellness days; gates `compute_readiness_proxy()`'s 7-day minimum
- `computed_readiness_score`, `computed_readiness_label` — Google-Health-derived readiness fallback, written by `compute_readiness_proxy()`

**Readiness score** (`readiness_score` / `readiness_label` / `readiness_is_computed` / `readiness_color` properties): a single readiness number that works regardless of which wellness source is active. Prefer these properties over the raw `training_readiness_score` field everywhere — templates, AI prompts, and `analysis.py`/`programs.py` aggregations all use them via `Coalesce("training_readiness_score", "computed_readiness_score")`.
- `readiness_score` — Garmin's real `training_readiness_score` (0–100) when present; otherwise falls back to `computed_readiness_score`. `None` if neither source has a value.
- `compute_readiness_proxy()` — the fallback algorithm, run by the Google Health wellness sync once `resting_hr_baseline`/`sleep_baseline_seconds`/`hrv_weekly_avg` are current. Compares last-night HRV, sleep duration, and resting HR each against their own trailing personal baseline (not an absolute target) and blends them at weights HRV 50% / sleep 30% / resting HR 20% (higher-than-baseline resting HR scores worse — it's a fatigue/illness signal, so it runs the opposite direction of the HRV/sleep ratios). Any missing input's weight redistributes across the ones present. Requires ≥7 days of recent wellness history (`wellness_days_synced`) before returning anything — returns `(None, None)` otherwise, since a baseline built from a day or two isn't reliable.
- Label bands differ by source: Garmin's own score uses Low <40 / Moderate 40–69 / High ≥70 for `readiness_color`; the computed proxy uses Low 1–29 / Moderate 30–64 / High 65–100 for both the label and the color, so the color band always matches whichever bands actually produced the label.
- `readiness_is_computed` — True when the displayed score is the Google Health estimate rather than Garmin's own; templates and AI prompts use this to caption the number as an estimate.

### Intervention Model
Tracks health/lifestyle interventions (medications, supplements, protocols, habits). Owned by `user`.

**Fields:** `name`, `category` (CATEGORY_CHOICES: medication/supplement/training/nutrition/habit/other), `start_date`, `end_date` (null = ongoing), `expected_effects`, `notes`, `is_active` property

**Key properties/methods:**
- `is_active` — True if `end_date` is null or in the future
- `current_dose` — latest `DoseChange` ordered by `start_date` (or None)
- `dose_summary` — e.g. `"2.5mg → 5mg → 7.5mg"` (all dose changes joined with →)
- `dose_at(target_date)` — returns the `DoseChange` active on a given date (binary search by `start_date`), or None
- `duration_days` — days since `start_date`

### DoseChange Model
Tracks dose history within an intervention. One row per dose period. No `user` FK — owned through its `Intervention` (scope with `intervention__user=`).

**Fields:** `intervention` (FK), `dose` (CharField, e.g. `"7.5mg"`), `start_date`, `end_date` (null = current dose), `notes`
**Constraints:** unique on `["intervention", "start_date"]`
**Properties:** `duration_days` — days from `start_date` to `end_date` (or today)

**Dose management flow:** When adding a new dose via `intervention_detail` or `intervention_quick_dose`, the previous active dose's `end_date` is automatically set to `new_start_date - 1 day`.

### SavedAnalysis Model
Stores saved Trends page analyses for later retrieval. Owned by `user`.

**Fields:** `intervention` (FK, nullable), `label`, `before_start`, `before_end`, `after_start`, `after_end`, `weight_goal`, `result_json` (full analysis dict), `ai_interpretation` (Sonnet text), `created_at`

### NutritionProfile Model
One row per user (`user` OneToOne). Stores inputs for the macro target calculator.

**Fields:** `height_cm`, `age`, `biological_sex` (male/female), `activity_level` (sedentary/light/moderate/active/very_active), `goal` (loss/gain/maintain), `deficit_pct` (default 20.0), `protein_g_per_kg_lean` (default 2.2), `manual_calories`, `manual_protein_g`, `manual_carbs_g`, `manual_fat_g`, `manual_fiber_g`

**Access:** `NutritionProfile.for_user(user)` — creates if missing. Read-only paths use `NutritionProfile.objects.filter(user=user).first()` (None if never set up).

### FoodEntry Model
One logged food/meal event per day. Owned by `user`.

**Fields:** `date`, `logged_at`, `meal` (breakfast/lunch/dinner/snack), `raw_text`, `items_json` (list of parsed item dicts), `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `ai_model`, `ai_confidence`, `edited_by_user`, `is_favorite`, `source_saved_meal` (FK to SavedMeal, nullable)

Photo-only entries (no typed text) store `raw_text` as `"Photo: <item names>"` so the logged row isn't blank. The photo itself is never stored.

### SavedMeal Model
Saved meal template for one-click re-logging. Owned by `user`.

**Fields:** `name`, `meal`, `items_json`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `times_logged` (auto-incremented on relog), `created_at`

**Ordering:** by `-times_logged`, then `name` (most-used first).

### HungerCheck Model
One hunger/satiety reading, owned by `user`. Contexts auto-detected from time of day on the nutrition page. A posted `related_meal_id` must be one of the user's own entries (400 otherwise).

**Fields:** `timestamp`, `date`, `context` (morning/pre_meal/post_meal/evening/random), `hunger_level` (1–10), `fullness_level` (1–10, post_meal only, nullable), `related_meal` (FK to FoodEntry, nullable), `notes`

### SideEffectLog Model
One logged symptom event, owned by `user`; posted `related_meal_id`/`related_intervention_id` must be the user's own.

**Fields:** `timestamp`, `date`, `symptom` (nausea/bloating/constipation/diarrhea/reflux/fatigue/headache/injection_site/dizziness/dry_mouth/other), `severity` (1=Mild/2=Moderate/3=Severe), `related_meal` (FK to FoodEntry, nullable), `related_intervention` (FK to Intervention, nullable), `notes`

### TargetAdjustment Model
Records every accepted calorie target change for history tracking. Owned by `user`.

**Fields:** `timestamp`, `previous_calories`, `new_calories`, `reason`, `auto_suggested` (bool), `accepted_by_user` (bool)

**Ordering:** by `-timestamp`.

### WeeklyReview Model
Stores one AI-generated review per completed Mon–Sun calendar week, per user (unique on (`user`, `week_start`) — `uniq_weeklyreview_user_week`).

**Fields:** `user`, `week_start` (DateField), `content` (TextField — Sonnet markdown text), `generated_at` (DateTimeField, auto_now_add), `ai_model` (CharField)

**Property:** `week_end` — `week_start + 6 days`

**Ordering:** by `-week_start`. Displayed at `/review/` (current week) and in a collapsible archive.

### WithingsAuth Model
One row per user (`user` OneToOne). Stores Withings OAuth tokens in DB instead of a file on disk. `userid` (the Withings account id) is unique when non-empty (`uniq_withings_userid`) — the webhook routes on it.

**Fields:** `userid`, `access_token`, `refresh_token`, `token_expires_at` (DateTimeField), `last_subscribed_at`, `last_webhook_received_at`, `webhook_subscription_active` (bool), `updated_at`

**Access:** `WithingsAuth.for_user(user)` — the user's row or None.

### PelotonAuth Model
One row per user (`user` OneToOne). Stores Peloton session cookie in DB.

**Fields:** `user`, `session_id`, `peloton_user_id` (Peloton account id, unique when non-empty), `peloton_username`, `last_updated` (auto_now), `notes`

**Property:** `masked_session_id` — shows `…{last4}` of the session ID.

**Access:** `PelotonAuth.for_user(user)` — the user's row or None. `PelotonClient(user)` reads from it; raises `PelotonAuthError` if missing or on 403. `PelotonClient.fetch_me(session_id)` validates a cookie.

### GoogleHealthAuth Model
One row per user (`user` OneToOne). Stores Google Health API OAuth2 tokens in Postgres, same pattern as `WithingsAuth`. Populated by the `google_health_login` command or the web reconnect flow at `/auth/google-health/connect/`.

**Fields:** `access_token`, `refresh_token`, `token_expires_at`, `scopes` (space-separated granted scopes), `connected_at`, `updated_at`

**`connected_at`** is deliberately not `auto_now_add` — it needs to advance every time the user actually reconnects (not just be frozen at row creation), since it drives the "how close to the 7-day refresh-token expiry are we" freshness badge on the Integrations page. Only stamped on a real reconnect (`GoogleHealthClient._save_tokens(mark_reconnected=True)`, called from `exchange_code`) — never on routine background token refreshes.

**Access:** `GoogleHealthAuth.for_user(user)` — the user's row or None.
**Property:** `days_since_connected`

### Integration Model
One row per user per data source (`peloton` / `garmin` / `withings` / `google_health`; unique on (`user`, `key`) — `uniq_integration_user_key`). `Integration.ensure_for_user(user)` creates missing rows (Garmin only for superusers); the Integrations page and Get Started call it. Lets the Integrations page toggle a source on/off and show connection status without touching that source's sync logic.

**Fields:** `key` (choices above), `display_name`, `is_enabled` (bool), `is_authenticated` (bool), `last_synced_at`

**Property:** `sync_all_url_name` — looks up the URL name for that source's full-backfill sync from `SYNC_ALL_URL_NAMES`; used to render the "Sync All" button on the Integrations page (see Sync Endpoints — Sync All buttons no longer live in the nav).

**Enable/disable gating**: sync functions and lazy-sync call sites (e.g. `day_view`'s lazy Garmin wellness sync) check `_integration_enabled(user, key)` before calling out to a source's API — disabling a source in the UI actually stops calls to it, not just hides its data.

### UserSettings
One row per user (`user` OneToOne, `related_name="fp_settings"`); `UserSettings.for_user(user)` creates if missing. Holds FTP, dumbbells, AI caches and batch ids.

- `last_daily_sync_at` — `DateTimeField(null=True)`. Stamped per user by the `sync_daily` management command when all of that user's sources succeed. Used in the nav sync dropdown and settings page footer.

### WebhookError Model
Log of failed webhook-triggered background syncs — currently only Google Health notification processing (`_process_google_health_notification` in sync.py), which responds `204` *before* processing, so a failure there has no other visible trace: the sender already got a success response and won't retry.

**Fields:** `user` (nullable — some failures happen before a user is known), `source` (e.g. `"google_health"`, `"google_health_payload"`, `"backfill_<source>"`), `summary`, `detail` (traceback), `created_at`. `WebhookError.record(source, summary, detail, user=None)`.

**Self-pruning:** `RETENTION_DAYS = 14`. `WebhookError.record(source, summary, detail)` creates a row and prunes anything past the window in the same call; `WebhookError.prune()` also runs on every load of the errors page — no separate scheduled cleanup job needed.

**UI:** `/settings/integrations/errors/`, linked from the Integrations page with an error-count badge. Newest first. Superusers see every row; everyone else sees only their own.

### BodyMeasurement Model
Raw per-weigh-in records from Withings, owned by `user`. Multiple per day is normal. Deduped by (`user`, `source`, `withings_grpid`) — `unique_withings_measurement_user` (Withings assigns one grpid per step-on session).

**Fields:** `measured_at`, `date` (local), `source` (default `"withings"`), `weight_lb`, `fat_mass_lb`, `fat_free_mass_lb`, `muscle_mass_lb`, `bone_mass_lb`, `hydration_lb`, `fat_ratio_pct`, `withings_grpid`, `raw_data`

**Sync flow:** Withings API → upsert `BodyMeasurement` (`_upsert_measurements(user, …)`) → `_update_daily_stats_for_dates(user, dates)` recomputes `DailyStats` body composition fields using the earliest weigh-in of each day.

### Garmin ↔ Peloton Deduplication
All matching is per user: every index (`_peloton_timestamp_index(user)`, `_peloton_workout_index(user)`, `_garmin_workout_index(user)`) holds only that user's workouts, and the augment/apply helpers (`_augment_peloton_run`, `_apply_garmin_form`, `_augment_peloton_from_google_health`, `_augment_garmin_from_google_health`) raise `ValueError` via `_require_same_user` if handed someone else's workout — two people in the same class at the same time are never merged. `_is_peloton_duplicate()` binary-searches a sorted timestamp index with ±120s window, checked once at the start of each Garmin sync run. For **running** duplicates, instead of skipping entirely, `_augment_peloton_run()` stamps the matching Peloton workout with the Garmin form metrics and merges form-metric time-series into `performance_graph_json`. Non-running duplicates are skipped (not stored, not merged — Peloton's own data is authoritative).

Sync order no longer matters: `_reconcile_garmin_duplicates()` re-checks every existing `source="garmin"` row against current Peloton workouts and runs after every Peloton sync (`_run_peloton_sync_new`/`_run_peloton_sync_all`), so a Garmin activity synced *before* its matching Peloton workout existed still gets merged/cleaned up once Peloton catches up — no need to run Peloton first. Running matches get the same live-API augmentation as the real-time path (one `get_activity_details` call per match, needed for HR-cross-correlation offset precision — the already-stored `performance_graph_json` on a Garmin row is downsampled and isn't precise enough); if Garmin is disabled or the call fails, that row is left in place rather than deleted, so its form data isn't lost. Non-running matches are just deleted, no API call needed. Manual sweep: `venv/bin/python3 manage.py dedupe_garmin_exercise --dry-run [--user USERNAME]` (drop `--dry-run` to apply) — same pattern as `dedupe_google_health_exercise` for Google Health ↔ Peloton.

### Garmin `performance_graph_json` Format
Uses the top-level `metricDescriptors` list (with `metricsIndex` positions) — **not** per-entry `metricDescriptor`. `parse_performance()` builds an index map then extracts values per point. Normalized to `metrics_by_slug`:
```
directHeartRate→heart_rate  directSpeed→speed  directPower→output
directBikeCadence/directDoubleCadence→cadence  directElevation→incline
directStrideLength→stride_length  directVerticalOscillation→vertical_oscillation
directVerticalRatio→vertical_ratio  directGroundContactTime→ground_contact_time
```
`directRunCadence` is strides/min (half steps); use `directDoubleCadence` for steps/min.

### Google Health Sync & Webhook
Mirrors the Garmin/Withings shape (OAuth2 + sync functions in `sync.py`, client in `services/google_health_client.py`) but splits into two independent halves that both feed `DailyStats`/`CachedWorkout`:

- Every `_run_*` function in sync.py takes `user` first (e.g. `_run_peloton_sync_new(user, days=None)`, `_run_google_health_wellness_sync(user, dates)`, `_run_withings_sync_all(user)`), and so does every helper that touches user data (`_upsert_page`, `_fetch_and_store_details`, `_reconcile_*`, `_integration_enabled`, `_client`, `_withings_client`). Sync API endpoints pass `request.user`.
- **Wellness sync** (`_run_google_health_wellness_sync(user, dates)`): fetches per-day recovery/activity metrics (resting HR, HRV, sleep, steps, floors, calories, respiratory rate, SpO2, AZM, skin temp, HR zone minutes, sedentary minutes) via per-metric `_gh_apply_*()` helpers, `get_or_create`s the `DailyStats` row per date, and (once enough history exists) calls `compute_readiness_proxy()` to populate `computed_readiness_score`/`computed_readiness_label`.
- **Locks** are per user: `user_lock("gh_wellness" | "gh_exercise", user.id)` (non-blocking acquire; a busy lock skips the call). One user's sync never blocks or skips another's.
- **Exercise sync** (`_run_google_health_exercise_sync(user, start, end)`): fetches workout sessions, parses each via `_parse_google_health_exercise()`, and upserts via `_upsert_google_health_exercise()` (has an `IntegrityError` fallback for concurrent webhook-triggered background threads racing on the same point). Does **not** sync running-form metrics (cadence/stride length/ground contact time) — Google models those as separate standalone data types, not part of the Exercise summary; deferred until there's a live payload to verify the real shape against.
- `_run_google_health_sync_new(user)`/`_run_google_health_sync_all(user)` run both halves together — "new" wellness covers the trailing 7 days, "new" exercise covers since the last Google-Health-sourced workout (or 30 days); "all" covers 3 years back for both.
- **Dedup with Peloton**: `_reconcile_google_health_duplicates()` — same pattern as `_reconcile_garmin_duplicates()` (see above); for running duplicates, `_augment_peloton_from_google_health()` merges the Google Health workout's data into the matching Peloton row instead of skipping it. Manual sweep: `venv/bin/python3 manage.py dedupe_google_health_exercise --dry-run`.
- **Sub-segment duplicates** (`_find_containing_workout()`): Google Health's own on-device auto-detection can split ONE Peloton/Garmin session into separate entries — confirmed live 2026-09-28, a 45-min circuit class's strength portion auto-detected as a standalone "Free weights" entry starting ~14 min in, its running portion as "Treadmill run" starting ~30 min in. These start well after the main workout's own start, so `_find_workout_match` (window centered on the point's own start) never finds it. `_find_containing_workout()` instead checks whether the point's whole span falls inside an existing Peloton/Garmin workout's span (±90s tolerance, bounded to workouts starting within the last 120 min). Checked as a fallback — after the existing near-simultaneous check fails — in the live sync loop (`_run_google_health_exercise_sync`, `skipped_contained_in_workout` in its result) and in both reconcile functions (`details[]` entries carry `"contained": True`). A contained match is **never** augmented/merged (unlike a same-session duplicate) — its own stats (calories, HR, pace) cover only its slice of the session, not the whole one; merging would corrupt the main workout's totals. Just skipped/deleted.

**Webhook** (`google_health_webhook`, `POST /webhooks/google-health/`, `@csrf_exempt`): notify-then-fetch — the payload identifies affected `dataType` + time range, not values, so the handler just re-runs the scoped sync functions above.
- **Routing (fan-out, for now)**: it isn't known yet whether a notification identifies its user, so `_process_google_health_notification` re-syncs the notified dates for every user in `_gh_connected_users()` (active, has a refresh token, integration enabled), sequentially inside the one thread per kind — never a thread per user. Each user is isolated: a failure is recorded with `WebhookError.record(..., user=user)` and the loop continues. `_gh_user_from_notification(item)` is a placeholder (always `None`) to wire direct routing once a payload shows a user identifier.
- **Payload capture (temporary)**: with `GOOGLE_HEALTH_CAPTURE_PAYLOADS` on (default `"1"`), the first 3 authorized real payloads are stored as `WebhookError` rows with source `google_health_payload` (they self-prune after `RETENTION_DAYS`). Check them at `/settings/integrations/errors/` to decide on direct routing.
- Threads run through `_in_background(fn, …)`, which closes the thread's DB connection when done.
- **Auth**: `_gh_webhook_authorized()` checks the `Authorization` header against `GOOGLE_HEALTH_WEBHOOK_SECRET` via `hmac.compare_digest` (the secret set as `endpointAuthorization.secret` at subscriber-creation time).
- **Response codes**: verification-challenge requests get 200/201; unauthorized requests get 401/403; real notifications get 204 **before** processing starts — Google's docs require this to avoid timeouts, and since there's no task queue (no Celery/RQ) in this app, processing is dispatched to a `threading.Thread(daemon=True)` per item after the response is sent. A failure in that background thread has no other visible trace (the sender already got its 204 and won't retry), so `_process_google_health_notification()` logs failures to `WebhookError.record(...)`.
- **Payload shape** (confirmed live 2026-08-23 against a real captured notification, not from docs — Google's docs example didn't match): top-level JSON array of `{"data": {...}}` items, not a single dict. Each item's interval is `civilIso8601TimeInterval`/`civilDateTimeInterval`, not the `physicalTimeInterval` shown in the webhooks guide's example; `_gh_webhook_interval_dates()` tries all three shapes and never raises. Per-item processing is individually try/excepted so one bad item can't drop the rest of the batch.
- **`dataType` casing**: subscriber-creation (`google_health_register_webhook.py`'s `SUBSCRIBED_DATA_TYPES`) requires kebab-case (e.g. `"daily-resting-heart-rate"`) — camelCase 400s on 8 of 12 types. `_GH_WELLNESS_WEBHOOK_TYPES` in sync.py includes both casings defensively since only the create-time format is confirmed, not necessarily the casing inside a real notification's `dataType` field.
- Idempotent by design: Google warns retries can send duplicate UPSERT notifications for the same interval — `DailyStats` is `get_or_create`d by (`user`, `date`), `CachedWorkout` upserts by (`user`, `workout_id`).

### Sync Endpoints
- Peloton: `Peloton Sync New` (`/api/sync/peloton/new/`), `Peloton Sync All` (`/api/sync/peloton/all/`)
- Garmin activities (owner only — 403 for everyone else): `Garmin Sync New` (`/api/sync/garmin/new/`), `Garmin Sync All` (`/api/sync/garmin/all/`)
- Garmin wellness: `Garmin Wellness Today` (`/api/sync/garmin/wellness/`), `?date=YYYY-MM-DD`, `?days=N` (max 90)
- Withings: `Withings Sync New` (`/api/sync/withings/new/`), `Withings Sync All` (`/api/sync/withings/all/`)
- `POST /api/withings/webhook/` — Withings push webhook. `@csrf_exempt`. Listed in `PUBLIC_PATHS` (no auth required). Called by Withings when body measurements change. Routed by the posted `userid` → `WithingsAuth.objects.filter(userid=…)` → that user; unknown userids, inactive users and users with Withings disabled are acknowledged and ignored. Fetches measurements for the notified time window with `WithingsClient(user)` and upserts them for that user. Always returns HTTP 200. Subscription management lives on the client: `WithingsClient.subscribe_webhook(callback_url)` (marks the user's row `webhook_subscription_active`), `list_webhooks()`, `revoke_webhook(callback_url)`; the subscribe/list/revoke commands are thin wrappers with `--user`.
- Google Health: `Google Health Sync New` (`/api/sync/google-health/new/`), `Google Health Sync All` (`/api/sync/google-health/all/`) — both run wellness + exercise sync together, see above.
- `POST /webhooks/google-health/` — Google Health push webhook (see above). `@csrf_exempt`. Listed in `PUBLIC_PATHS`.
- **Nav vs. Integrations page**: the nav Sync dropdown only offers each source's "Sync New" (a fast incremental sync safe to run often). "Sync All" (slow full backfill) for every source lives on `/settings/integrations/` instead, next to that source's enable/disable toggle — not in the nav. Peloton and Garmin used to be bundled into one nav action (`sync_new`/`sync_all`, since removed and split per-source); `_reconcile_garmin_duplicates()`/`_reconcile_google_health_duplicates()` (see Deduplication sections above) are what actually keep sources consistent regardless of which order or combination you sync in — no source depends on another running first.

### Program Recovery Tracking & "Any Class" Slots
Extensions to the Programs tracker (`workouts/programs.py`, `program_views.py`, `program_run.html`), built for the 6-day Robin's Split + Run plan (4 fixed-ride circuit classes + Pilates and Yoga off-days + cool-downs).
- **Any-class slots**: `ProgramSlot.match_discipline` (+ optional `match_title_keyword`) matches a workout by discipline instead of a ride-id — step 2b of `identify_membership` (`matched_by="discipline"`). Peloton stores **Pilates as discipline `strength`**, so the Pilates slot is `strength` + keyword `pilates`; Yoga is `yoga`. Only counts once the program has an active run, only for workouts on/after `run.start_date`, and a workout whose time span overlaps one already on a grid is skipped (a watch's duplicate "Yoga" entry for a Peloton class). Placement uses `place_by_date()` (the pass whose date range is closest, ties → earlier pass) rather than `fill_or_append`'s newest-pass-only rule, since off-day classes land between the fixed rides and backfills replay after later passes exist.
- **Recovery tracking**: `Program.track_recovery` (off by default) → `attach_recoveries(run)` links cool-down walks/stretches to a completion via `ProgramRecovery` (`entry` FK to `ProgramWorkout`, `workout` OneToOne to `CachedWorkout`, `kind` walk/stretch, `order`). Rule: from a completion's end, a walk or stretch may start within the program's `recovery_window_min` (default 10) — or up to `RECOVERY_SLACK_MIN` (2, constant) before, for inexact end times; after a walk a stretch may follow in the same window; after a stretch the chain ends. Only `walking` / `stretching` workouts ≤ the program's `recovery_max_min` (default 30), each kind switchable via `recovery_walks` / `recovery_stretches`, excluding stretching-category classes titled "pilates". All four settings are editable per program on the Edit page. Any source counts (Peloton or watch), each workout attaches once, and workouts already on a grid are never recoveries. Idempotent and resumable (a stretch syncing after its walk extends the chain). Recoveries cascade-delete with their pass.
- **Ownership**: programs are per user (`Program.user`; unique (`user`, `slug`) — `uniq_program_user_slug`). Weeks/slots/runs/passes/completions/recoveries have no FK — scope through the program (`ProgramRun.objects.filter(program__user=user)`, `RunWeek.objects.filter(run__program__user=user)`). `identify_membership(workout)` only considers `workout.user`'s programs. `seed_programs --user USERNAME`.
- **When it runs**: `reconcile_program_extras(user)` (backfills any-class slots + attaches recoveries for programs using either) is called at the end of Peloton and Google Health syncs via `_reconcile_programs_safe()` in sync.py, and `backfill_program()` (the run page's "Backfill History" button) attaches too.
- **UI**: completed cells on the run page list `+ Cool-down walk · 5 min` / `+ Stretch · 15 min`; a "Recovery" stat card (completions with recovery / total, sessions, minutes) shows when tracking is on.
- **Setup**: nothing to run — enable tracking and add any-class slots on the program's Edit page (see below).

### Configuring Programs (no code changes)
Everything about a program's definition is editable in the app; the old pattern of one-off `seed_*` / `setup_*` commands and shell edits is retired (`seed_programs` remains only as the record of how HiLit and the early splits were first created).
- **Edit page** (`/programs/<slug>/edit/`, `program_edit`; "Edit program" button on the cycle page and program page): name/instructor/description, the recovery settings above, and every slot — day, order, title, minutes, optional, delete — plus **how a workout matches the slot** (`resolve_slot_match()` in programs.py): *Specific class* (ride id or pasted class link → `peloton_ride_id`), *Any class of a type* (a preset from `ANY_CLASS_PRESETS` — Pilates, Yoga, Stretching, Walking, Running, Cycling, Strength, Cardio, Circuit, Meditation — or a custom discipline + title keyword → `match_discipline`/`match_title_keyword`), or *By title only*. Blank rows add slots; "Save & add a week" and "Remove week" manage weeks (a week with completions can't be removed; a program keeps at least one week). Invalid rows are reported and left unchanged while the rest saves; deleting a slot leaves its completions in the cycle as unslotted entries. Saving re-runs `backfill_program()`, so new/changed slots pick up history you already have. Changing a slot's ride id clears its `alt_ride_ids`.
- **Create**: `/programs/new-plan/` — paste schedule text and/or a screenshot; `parse_plan_skeleton()` (Haiku) extracts weeks/days/classes, flags open-ended days ("Pilates (any class)", "Yoga") as `any_class` with a `class_type`, and ignores exercise lists (supersets, sets/reps) under a class. The review table has a "Match by" column (specific class vs. any class of a type) and a Plan/Split selector (auto: one week → split, otherwise plan). Or **start blank** (`program_new_blank`, same page) and build it in the editor.
- **Duplicate** (`duplicate_program()`, form at the bottom of the Edit page): copies weeks, slots and recovery settings, not cycles/completions. Ride ids are cleared by default — two programs pinning the same classes compete for every workout and the original always wins — with a "keep class links" option; any-class slots are always kept.

### Detail Page Templates
All five discipline-specific detail pages extend `detail_base.html`, which owns:
- Page header, PR banner, class info card
- Full sidebar: leaderboard, previous attempts list (with compare checkboxes), "View all times" link, external links (Peloton / Garmin Connect)
- External links appear for all workouts: `workout.external_url` for the primary source, plus a secondary Garmin Connect link when `workout.garmin_activity_id` is set (Peloton run augmented with Garmin data)

Child templates override these blocks: `discipline_tag`, `page_title`, `pr_sub`, `detail_main`, `history_item_stats`, `recent_section`, `detail_scripts`.

### Calendar & Day View
- **Calendar** (`/calendar/`, `/calendar/<year>/<month>/`): monthly grid, discipline dots per day, `readiness_score`/`readiness_color` overlaid on each cell, next-workout AI recommendation in sidebar.
- **Day view** (`/day/YYYY-MM-DD/`): lazy-syncs Garmin wellness on first load if missing (owner only, and only when their `garmin` integration is enabled — see `_integration_enabled` gating under the Integration model above), shows all workouts for the day with correct HR/effort, Recovery Signals card (HRV, sleep, resting HR, respiration, SpO2, skin temp, VO2 max), Activity card (steps, floors, active/total calories, intensity minutes, AZM, sedentary time, HR zone bars), plus AI day analysis.
- **Today** (`/`, `today_page`): landing page — today's wellness grid (incl. Steps) + compact Activity section + today's workouts.

### Integrations Page
- **Settings** (`/settings/integrations/`, `integrations_settings_page`): one row per `Integration` (Peloton/Garmin/Withings/Google Health) — enable/disable toggle (HTMX, swaps `partials/integration_row.html`), auth status, last-synced timestamp, "Sync All" button (via `Integration.sync_all_url_name`), and for Google Health specifically a "Reconnect" link + freshness badge (`GoogleHealthAuth.days_since_connected` vs. the 7-day refresh-token expiry). Also shows a Peloton Session Cookie card (masked cookie, "Connected as @username", last updated + a cookie-only update form posting to `set_peloton_auth`, which validates via `/api/me` and redirects back here), a Withings "Connect Withings"/"Reconnect" button, and a "Setup guide" link to Get Started. Garmin's row is owner-only stacked above a Webhook Errors card with an error-count badge, in a right column laid out side-by-side with the Data Sources card on wide viewports (`.integrations-row` CSS grid, `repeat(auto-fit, minmax(420px,1fr))`, centered up to `max-width:1400px`).
- **Webhook Errors** (`/settings/integrations/errors/`, `webhook_errors_page`): newest-first list of `WebhookError` rows with collapsible `<details>` tracebacks; prunes rows past `WebhookError.RETENTION_DAYS` on every load.
- **Google Health OAuth Reconnect** (`google_health_oauth_connect` / `google_health_oauth_callback`, `/auth/google-health/connect/` → Google consent screen → `/auth/google-health/callback/`): plain full-page redirects (not HTMX — OAuth needs a genuine browser navigation). The connect view builds the redirect URI dynamically from the current request (works on both `localhost` and the deployed domain without env-specific config), stashes a random `state` value in the session with an explicit `request.session.save()` (don't rely solely on `SESSION_SAVE_EVERY_REQUEST` before an external-domain redirect), and the callback validates the returned `state` matches (CSRF protection) before exchanging the code via `GoogleHealthClient`/`exchange_google_health_code()`. Always redirects back to `/settings/integrations/` with a Django `messages` success/error, even on failure — never renders an error page directly, since the browser lands on this exact URL straight from Google.

### AI (Direct HTTP, not anthropic package)
All Anthropic calls live in `workouts/ai.py` and go through `workouts/llm.py` (`requests.post` to `https://api.anthropic.com/v1/messages`). The `anthropic` Python package is **not installed**.

- **Per user, per feature**: `llm.call`/`call_raw`/`call_json`/`submit_batch` require `user=` and `feature=` keyword arguments (a missing one is a `TypeError`). Every generator and prompt builder in ai.py takes `user` first (`_get_or_generate_day_analysis(user, day, workouts, stats)`, `build_persona_block(user, …)`, `cached_settings_field(user, …)`, `run_stats_chat(user, context, history, msg)`, …) and scopes every query to that user.
- **Guard**: `llm.guard(user, feature)` runs before any request: `AIFeatureDenied` unless `has_feature(user, feature)`, then `AIBudgetExceeded` if the user (not a superuser) has spent their `UserAccess.monthly_ai_budget_usd` since the 1st of the month (America/Los_Angeles). Superusers are logged but never capped. Views catch both (`ai.AI_UNAVAILABLE`) and show `partials/ai_unavailable.html` / `ai_unavailable_reason` instead of an error; inline generators also check `has_feature` before building a prompt.
- **Usage log**: each response's tokens and cost go to `AIUsage` (`user`, `feature`, `model`, input/output/cache-read/cache-write tokens, `is_batch`, `cost_usd`). Logging never raises into the caller. Prices: `llm.MODEL_PRICES` (USD per 1M tokens: input, output, cache write, cache read; batch = 50%) — re-check against Anthropic's pricing when prices change; an unknown model is costed at the Sonnet rate with a warning.
- **Batches**: `submit_batch` prefixes `custom_id` with `u{user.id}-{feature}-`; each check endpoint calls `llm.log_batch_result(row)` where it stores the result (once, then clears the batch id). Check endpoints read the batch id from the requesting user's own `UserSettings`/`WeeklyReview` row.
- **Chat tools**: `build_tool_dispatch(user)` (services/chat_tools.py) returns the tool functions with the user bound via `functools.partial`; `run_stats_chat` builds it per call. The JSON schemas sent to the model (`CHAT_TOOLS`) are unchanged — the model can't choose whose data a tool reads.
- **Feature slugs** (same as access.FEATURES): `ai_next_workout`, `ai_day_analysis`, `ai_training_insights` (analytics batch + compare narrative), `ai_body_commentary`, `ai_food_parse`, `ai_meal_suggest`, `ai_nutrition_insights`, `ai_pattern_insights`, `ai_weekly_review`, `ai_intervention_interpretation`, `ai_program_tools` (plan import + retrospectives), `ai_chat`.

- **Analytics insights**: Batch API (`/api/analytics/insights/`). Polls via `/api/analytics/check-insights/` (HTMX). Long-form multi-section analysis. Cached in `UserSettings.ai_insights`.
- **Day analysis** (`_get_or_generate_day_analysis`): Claude Sonnet (upgraded from Haiku), synchronous, cached 7 days in `DailyStats.ai_day_analysis`. Structured output: `HEADLINE:` + bullet points. Uses `readiness_score`/`readiness_is_computed` (not the raw `training_readiness_score` field) so the prompt still gets a readiness signal on Google-Health-only days.
- **Next workout rec** (`_get_or_generate_next_workout`): Claude Sonnet (upgraded from Haiku), synchronous, cached 24h in `DailyStats.ai_next_workout`. Force-refresh via `POST /api/next-workout/refresh/`. Structured output: `INTENSITY:` / `ACTIVITY:` / `REASON:`. Prompt includes: workout titles (not just discipline), muscle groups worked (high/moderate buckets from perf graph), exercise names for strength sessions, explicit cardio vs. strength guidance rules, and an explicit anti-hallucination instruction (added after the model once referenced a specific day the user hadn't actually trained on — ground every claim in the provided workout list, don't infer/invent). Detects if user already trained today and frames as "tomorrow" if so. Also uses `readiness_score`/`readiness_is_computed` like day analysis.
- **Body commentary** (`_get_or_generate_body_commentary`): Claude Haiku, synchronous, cached 24h in `UserSettings.ai_body_commentary`. Interprets recent body composition and recovery trends. Refreshed via `POST /api/body/commentary/refresh/`.
- **Intervention interpretation** (`_generate_intervention_interpretation`): Claude Sonnet, called on-demand from Trends page run-analysis flow. Returns free-form text interpreting the before/after metrics in context of the intervention and its dose history.
- **Compare analysis** (`compare_analysis`): Claude Haiku, on-demand HTMX endpoint (`POST /api/compare/analysis/`). Generates a short narrative comparing 2–4 workouts side-by-side using extracted stats.
- **Food parsing** (`parse_food_text`): synchronous. Called from `POST /nutrition/api/parse/`. Text parses use Claude Haiku: converts freeform food description into structured items list with per-item macros, also calls OpenFoodFacts for branded foods, detects meal kit services (Home Chef, Factor, etc.) and fetches their nutrition data from their APIs. Image parses use Claude Sonnet and a single prompt that classifies `image_type` (`label` / `meal` / `not_food`) and then applies label-reading rules or portion-estimation rules (assumed portion in `quantity` with a `~` prefix, cooking fat folded in and named in `note`, confidence capped at medium for photo-only meals, user portion text overrides the visual estimate). The result includes `model` and, for images, `image_type` (missing → `"label"`).
- **Meal suggestions** (`suggest_meals`): Claude Haiku, synchronous. Called from `GET /nutrition/api/suggest/` (HTMX). Suggests 3 meals based on remaining daily macros and time of day. Context-aware: receives `recent_meals` (last 3 days) and `top_foods` (top 8 foods last 30 days) to avoid repeats and lean toward familiar foods. **Hunger-aware**: `current_hunger` (int 1-10, from most recent `HungerCheck` within 4h) scales suggestion size: 1-3→60-200 kcal snacks, 4-6→300-500 kcal standard, 7-10→500-700 kcal substantial. **Symptom-aware**: `gi_symptoms=True` (nausea or bloating in last 24h via `SideEffectLog`) avoids high-fat, prefers easily-digested options, adds a `gi_note` in the response. Returns `{suggestions, tip, gi_note}` rendered by `nutrition_suggestions.html`. Cards include "+ Save" button to add suggestion directly to SavedMeals.
- **Nutrition insights** (`_get_or_generate_nutrition_insights`): Claude Sonnet, 7-day cache in `UserSettings.ai_nutrition_insights` / `ai_nutrition_insights_generated_at` / `ai_nutrition_insights_range`. Comprehensive prompt covering targets, adherence, weekday vs weekend patterns, meal timing, top 5 foods, weight trend, interventions context. Structured `## What's working / ## Where the friction is / ## Specific suggestions / ## Watch list`. Range-aware: separate caches for different range_days values.
- **Weekly review** (`_get_or_generate_weekly_review`): Claude Sonnet. Generates a review of the most recently completed Mon–Sun week. Covers weight change vs. prior week, nutrition adherence vs. targets, workout summary, recovery averages, hunger patterns (morning avg), and logged symptoms. Structured sections: Weight & Body Composition / Nutrition / Training / Hunger & Symptoms / One Thing Going Well / One Focus for Next Week. Cached per `week_start` in the `WeeklyReview` model (one row per week). Force-refresh via `GET /review/?refresh=1`. Called from `weekly_review_page` view.
- **Pattern insights** (`_get_or_generate_pattern_insights`): Claude Sonnet, 7-day cache in `UserSettings.ai_pattern_insights`. Pulls 60 days of integrated data — weight, body composition, recovery, nutrition, hunger checks, side effects, workouts, interventions. Finds 3–5 non-obvious patterns (lagged correlations, threshold effects, hunger creep, symptom clustering, etc.). Displayed at `/insights/` with HTMX regenerate. A one-line headline from the "Highest-confidence pattern" section appears as a teaser card on `/body/`. Force-refresh via `POST /api/insights/refresh/`.
- **Intervention context** (`_interventions_context`): helper used by day analysis and next-workout prompts. Includes overlapping interventions with per-`DoseChange` bullet lines (dose + date range) and `expected_effects`.

**Rendering filters** (in `workout_filters.py`):
- `format_next_workout` — parses INTENSITY/ACTIVITY/REASON into coloured label + subtitle + body
- `format_day_analysis` — parses HEADLINE + bullets into `.insights-list` / `.insights-item` styled cards
- `format_body_commentary` — same as `format_day_analysis` (HEADLINE + bullet cards)
- `format_insights` — used by analytics page for bullet-list insights
- `format_nutrition_insights` — parses `## Header` sections + body/bullets into `.ni-section` / `.ni-header` styled HTML; bullets render as `.insights-item` cards; supports `**bold**` markdown within text; used for nutrition insights, pattern insights, and saved analysis interpretation

**Simple tags** (in `workout_filters.py`):
- `last_daily_sync` — returns `UserSettings.last_daily_sync_at` (the timestamp of the last successful `sync_daily` run). Used in the nav sync dropdown and settings page footer.

### Run Detail Page
`run_detail.html` shows a **RUNNING FORM** card (cadence, stride length, vertical oscillation, vertical ratio, ground contact time) when Garmin data is present. Color-coded against benchmarks (e.g. VO ≤7.5 cm green, >9 cm orange). Form metrics also appear in the VS. YOUR AVERAGES table and as chart toggle overlays.

### Compare Page
`compare.html` adds running form rows (cadence, stride, VO, VR, GCT) to the stats table when at least one compared workout has Garmin form data. `dir: "lower"` on VO/VR/GCT so best values are highlighted; stride length has no direction.

Strength and circuit workouts compare together in `"strength"` mode (circuit used to fall into `"mixed"` and show bike stats). When any compared workout has a manual exercise log, the stats table adds Volume / Reps / Sets / Timed Work / Heaviest "(logged)" rows from `CachedWorkout.manual_log_summary`, and a LOGGED EXERCISES table (`buildManualLogTable()`) lines up each exercise by name across workouts, highlighting the heaviest weight. Row notes are read for meaning: "sec" means reps are seconds (counted as timed work, not reps or volume); "per side"/"each side" doubles the rep and volume count. `compare_analysis` (AI) gets the same log.

### Withings Client
`WithingsClient` in `workouts/services/withings_client.py`:
- `WithingsClient(user)`: tokens read from and written to that user's `WithingsAuth` row (not `~/.fitpulse/withings_tokens.json`)
- `get_authorization_url(state, redirect_uri=None)` / `request_tokens(code, redirect_uri=None)` (exchange without saving) / `exchange_code(code, redirect_uri=None)`; `redirect_uri` defaults to `WITHINGS_REDIRECT_URI` for the CLI
- Measurement types fetched: weight (1), fat free mass (5), fat ratio (6), fat mass (8), muscle mass (76), hydration (77), bone mass (88), pulse wave velocity (91), vascular age (155)
- Value decoding: `raw_value * 10^unit` — always apply the unit exponent
- kg → lb conversion: multiply by 2.20462
- 503 rate-limit: logs warning and returns empty (doesn't crash sync)
- Status 100–105: raises with "re-run withings_login" message
- 401: refreshes token and retries once

### Nutrition Module (`workouts/nutrition.py`)
Every function takes `user` first (e.g. `compute_macro_targets(user, profile=None)`, `recompute_daily_nutrition(user, date_obj)`, `get_top_foods(user, start, end)`, `evaluate_target_fit(user)`) and reads only that user's rows.
- `compute_macro_targets(profile=None)` — Mifflin-St Jeor BMR → TDEE (with activity multiplier) → calorie target (deficit/surplus %) → protein (g/kg lean mass from Withings) → fat (25% of calories, floored at 0.5g/kg) → carbs (remainder). Respects `manual_*` overrides on `NutritionProfile`. Returns full dict including `bmr`, `tdee`, `computed_*` (shown alongside manual values), `warnings` list, and latest `weight_lb`/`lean_mass_lb` from `DailyStats`.
- `recompute_daily_nutrition(date_obj)` — sums all `FoodEntry` rows for a date and writes the totals to `DailyStats` (`cal_total`, `protein_g_total`, `carbs_g_total`, `fat_g_total`, `fiber_g_total`). Called after every FoodEntry add/edit/delete.
- `compute_streaks(reference_date=None)` — returns `{logging_days, protein_days, protein_target}`; anchors on today or yesterday if today has no entries. Used for streak badges on the nutrition page.
- `get_weekly_stats(end_date, targets=None)` — 7-day per-day rows (oldest first) plus aggregate `days_logged`, `avg_cal`, `avg_protein_g`, `avg_fiber_g`, `days_hit_cal`, `days_hit_protein`, `days_hit_fiber`. Each day row includes `cal_ok`, `prot_ok`, `fiber_ok` booleans for color-coding (90%/90%/85% thresholds). Used for THIS WEEK table on the nutrition page.
- `get_yesterday_recap(today, targets=None)` — summary dict for yesterday's log; `None` if no data. Used for the yesterday nudge card.
- `get_top_foods(start, end, top_n=15)` — aggregates `items_json` from `FoodEntry`, fuzzy-groups similar names (≥0.80 `SequenceMatcher` similarity), returns sorted by count with avg macros per item.
- `get_meal_timing_stats(days=30)` — avg breakfast/lunch/dinner/last-meal times, eating window hours, gap to 10:30 PM bedtime proxy. Used on the analytics page.
- `get_day_of_week_stats(start, end)` — list of 7 `{day_name, avg_cal, count}` dicts (Mon–Sun). Used for the day-of-week bar chart on the analytics page.
- `get_nutrition_gap(start, end)` — `{days_logged, total_days, pct}` for Trends page warning when nutrition data is sparse.
- `get_satisfying_meals(min_occurrences=3, top_n=5)` — returns `SavedMeal`s linked to `HungerCheck` records where `context=post_meal` and `fullness_level≥7`, occurring at least `min_occurrences` times. Result dicts include `{meal, name, calories, protein_g, carbs_g, fat_g, fiber_g, times_logged, satisfying_count}`. Used to render "Most Satisfying Meals" section on the nutrition page.
- `evaluate_target_fit()` — compares actual 14-day weight trend (7-day rolling avg split) to expected trend given current calorie target. Requires ≥10 logged nutrition days and ≥6 weigh-ins. Returns `{status, actual_trend_lb_per_week, expected_trend_lb_per_week, suggested_calorie_adjustment, new_suggested_calories, reasoning, confidence, logging_days, current_calories}`. Status values: `on_track` / `under_responding` / `over_responding` / `insufficient_data` / `no_weight` / `no_target`. Under-responding by 0.5–1 lb/week → suggest −100 kcal; by 1+ lb/week → −200 kcal. Over-responding by 0.5+ lb/week → +100 kcal. Never suggests below the sex-specific calorie floor.

### Nutrition Routes
| URL | View | Notes |
|---|---|---|
| `/nutrition/` | `nutrition_page` | Daily log: macro bars, THIS WEEK table, streak badges, yesterday recap, hunger widget (today only), AI suggestions, saved meals |
| `/nutrition/targets/` | `nutrition_targets_page` | Edit NutritionProfile; computed vs. manual; Target Fit check (14-day trend vs expected); adjustment history |
| `/nutrition/analytics/` | `nutrition_analytics_page` | Adherence stats, macro trend chart, day-of-week, top foods, hunger trend, symptom summary, AI insights |
| `POST /nutrition/api/parse/` | `nutrition_parse_api` | Parse food text and/or a photo (nutrition label or meal, auto-detected) → `nutrition_parse_result.html` partial (HTMX). Images are downscaled in the browser to ≤1568 px JPEG; server rejects >3.75 MB. Upload field is still named `label_image`; "Take photo" (`capture="environment"`) shows on touch devices only |
| `POST /nutrition/api/log/` | `nutrition_log_api` | Save confirmed FoodEntry + recompute daily totals |
| `POST /nutrition/api/delete/<pk>/` | `nutrition_delete_api` | Delete FoodEntry + recompute |
| `GET /nutrition/api/suggest/` | `nutrition_suggest_api` | AI meal suggestions (context-aware: passes recent meals + top foods) → `nutrition_suggestions.html` (HTMX) |
| `POST /nutrition/api/save-meal/<pk>/` | `nutrition_save_meal_api` | Save FoodEntry as SavedMeal |
| `POST /nutrition/api/save-suggestion/` | `nutrition_save_suggestion_api` | Create SavedMeal from suggestion data (name + macros) without a FoodEntry |
| `POST /nutrition/api/relog/<pk>/` | `nutrition_relog_api` | Create new FoodEntry from SavedMeal; increments `times_logged` |
| `POST /nutrition/api/delete-meal/<pk>/` | `nutrition_delete_meal_api` | Delete SavedMeal; clears `source_saved_meal` FK on logged entries |
| `GET /nutrition/api/entry-row/<pk>/` | `nutrition_entry_row_api` | Return read-only `nutrition_entry_row.html` partial |
| `GET/POST /nutrition/api/edit/<pk>/` | `nutrition_edit_api` | Inline edit form or save; sets `edited_by_user=True`; recomputes totals |
| `POST /api/nutrition/insights/refresh/` | `nutrition_insights_refresh` | Force-refresh AI nutrition insights; returns rendered HTML fragment |
| `POST /nutrition/api/hunger/log/` | `hunger_log_api` | Log a HungerCheck; returns JSON `{ok, id, avg_morning}` |
| `POST /nutrition/targets/accept/` | `target_accept_api` | Accept a suggested calorie adjustment; updates `NutritionProfile.manual_calories` + creates `TargetAdjustment` |

### Smart Tracking Routes (Phase 3)
| URL | View | Notes |
|---|---|---|
| `/symptoms/` | `symptoms_page` | GET: symptom log form + recent entries + 30-day summary. POST: create SideEffectLog (JSON response) |
| `/insights/` | `insights_page` | Sonnet pattern insights page; auto-generates on load if cache expired; HTMX regenerate button |
| `POST /api/insights/refresh/` | `pattern_insights_refresh` | Force-regenerate pattern insights; returns rendered `pattern_insights.html` partial |
| `/review/` | `weekly_review_page` | Sonnet weekly review of most recently completed Mon–Sun week; archive of past weeks. `?refresh=1` force-regenerates. |

### analyze_intervention Command
`venv/bin/python3 manage.py analyze_intervention --start YYYY-MM-DD --label "..." --window 28 --weight-goal loss|gain|maintain [--user USERNAME]`

Compares DailyStats metrics before vs. after an intervention date. Prints before/after means with ✓/✗/→ direction indicators across six sections: RECOVERY, SLEEP, STRESS, ACTIVITY, BODY COMPOSITION, NUTRITION. `--weight-goal` controls whether weight going down is ✓ or ✗ (default: `loss`). Thin wrapper around `run_intervention_analysis(user, …)` in `workouts/analysis.py`.

### Body & Trends Pages
- **Body** (`/body/`): 90/180/365-day range toggle. Stat cards (current weight, fat%, muscle mass, avg sleep, avg HRV). Weight chart with 7-day rolling avg, full-height solid intervention start lines, half-height dashed dose-change lines. Body composition stacked area chart. Recovery sparklines (HRV, sleep, resting HR, body battery). Active interventions card with `current_dose`. 7-day nutrition summary card (avg calories, protein, fiber; shows "Log more days" prompt if <3 days logged). **Pattern insight teaser** (cyan card showing headline of latest Sonnet pattern insight, links to `/insights/`; only shown when insights exist). **Recent symptoms card** (last 3 days of `SideEffectLog`, links to `/symptoms/`; only shown when symptoms exist). AI body commentary (Haiku, 24h cache, includes 7-day nutrition context when ≥3 days logged).
- **Trends** (`/trends/`): Select an intervention → see dose timeline → pick before/after window (auto-fills from intervention dates, or custom) → Run Analysis → metrics table (6 sections including NUTRITION, ✓/✗/→) → Sonnet AI interpretation → Save. Saved analyses listed below with load/delete.
- **Saved analysis detail** (`/trends/analysis/<pk>/`): Full view of a single saved analysis. Delete via `POST /trends/analysis/<pk>/delete/`.
- **Interventions** (`/interventions/`): List with `dose_summary`, inline Change Dose form (3-click: select new dose + date → confirm), "Manage Doses" button.
- **Intervention detail** (`/interventions/<pk>/`): Full dose timeline table with per-row edit/end/delete. Add Dose form auto-ends previous active dose.
- **Intervention edit** (`/interventions/<pk>/edit/`): Edit intervention name, category, dates, expected_effects, notes.

### Other Key Patterns
- **FTP**: `workout.ftp` per-workout (stamped at sync); `backfill_ftp.py [--user USERNAME]` for history. Use `workout.ftp` in templates, not `UserSettings.for_user(user).ftp`.
- **Pace**: stored as seconds/mile; `pace` slug in perf graph is decimal min/mile — multiply by 60 before passing to JS `fmtPace`.
- **Chart init**: always wrap `new Chart(...)` in `DOMContentLoaded` (Chart.js loaded `defer`).
- **Frontend**: vanilla JS + HTMX only. No npm/webpack/Tailwind. Single `main.css`.
- **Effort display**: always use `workout.effort_points` (from perf graph) not `effort_score` or `average_effort_score`. Falls back gracefully if perf graph not cached.
- **HR display**: always use `workout.heart_rate_avg_best` — model field if set, perf graph otherwise. Many Peloton workouts have null `heart_rate_avg` from the list API.

---

## Environment Variables (`.env`)

```
DJANGO_SECRET_KEY=...
# PELOTON_SESSION_ID and PELOTON_USER_ID are no longer needed here —
# Peloton credentials are stored in the DB and managed via /settings/integrations/
ANTHROPIC_API_KEY=...
GARMIN_EMAIL=...
GARMIN_PASSWORD=...
WITHINGS_CLIENT_ID=...
WITHINGS_CLIENT_SECRET=...
WITHINGS_REDIRECT_URI=http://localhost:8000/auth/withings/callback/
WITHINGS_CALLBACK_URL=https://fitpulse-jp2p.onrender.com/api/withings/webhook/  # used by subscribe_withings_webhook
GOOGLE_HEALTH_CLIENT_ID=...
GOOGLE_HEALTH_CLIENT_SECRET=...
GOOGLE_HEALTH_REDIRECT_URI=http://localhost:8000/auth/google-health/callback/  # used by the CLI google_health_login command; the web reconnect flow builds this dynamically instead
GOOGLE_HEALTH_WEBHOOK_SECRET=...            # shared secret Google echoes in the webhook's Authorization header
GOOGLE_HEALTH_PROJECT_NUMBER=...            # used by google_health_register_webhook
GOOGLE_HEALTH_ADMIN_ACCESS_TOKEN=...        # service-account token, used by google_health_register_webhook
GOOGLE_HEALTH_CAPTURE_PAYLOADS=1            # temporary: store the first 3 real webhook payloads as WebhookError rows (set 0 to stop)
EMAIL_HOST_USER=...                         # Gmail address that sends welcome emails (blank → console backend)
EMAIL_HOST_PASSWORD=...                     # Google app password for it
DJANGO_DEBUG=True
```

`WITHINGS_CALLBACK_URL` is also read by the web connect flow (it subscribes the webhook right after OAuth). The Withings web flow's callback, `https://fitpulse-jp2p.onrender.com/auth/withings/callback/` (plus `http://localhost:8000/auth/withings/callback/` if the dashboard allows a second one), must be registered in the Withings developer dashboard's app settings — Withings rejects unregistered callbacks.

Must be set on Render separately from local `.env` — a missing `GOOGLE_HEALTH_CLIENT_ID`/`SECRET` in production surfaces as token-refresh failures ("Could not determine client ID from request") only once real webhook traffic actually reaches the sync code, not at deploy time.

---

## Common Tasks

Shell snippets below assume `from django.contrib.auth.models import User; u = User.objects.get(username='<username>')` (or `from workouts.users import get_owner; u = get_owner()`).

- **Add a model field**: `models.py` → `makemigrations && migrate` → populate in `from_api()`, `from_garmin()`, or `apply_detail()` → add to `DETAIL_FIELDS` if from detail endpoint
- **Add a Garmin metric**: add to `parse_activity()` return dict, add slug to `SLUG_MAP` in `parse_performance()`, add field to `from_garmin()`
- **Update FTP**: `/settings/` → update `FTP_HISTORY` in `backfill_ftp.py` → `venv/bin/python3 manage.py backfill_ftp`
- **First-time Garmin**: `venv/bin/python3 manage.py garmin_login`
- **First-time Withings**: `/settings/integrations/` → "Connect Withings" (or `venv/bin/python3 manage.py withings_login [--user USERNAME]`)
- **Clear stale AI insights**: `UserSettings.objects.filter(user__username='<username>').update(ai_insights=None, ai_insights_batch_id=None)` in Django shell
- **Clear day analysis cache**: `DailyStats.objects.filter(user=u, date=d).update(ai_day_analysis=None, ai_day_generated_at=None)`
- **Clear next-workout rec**: `DailyStats.objects.filter(user=u, date=date.today()).update(ai_next_workout=None, ai_next_workout_generated_at=None)`
- **Backfill wellness data**: hit `/api/sync/garmin/wellness/?days=30` or use the Sync dropdown
- **Backfill perf graph fields** (calories/distance/HR/pace from cached perf graphs):
  ```python
  from workouts.models import CachedWorkout
  from workouts.sync import _extract_perf_fields
  for w in CachedWorkout.objects.filter(source='peloton', performance_graph_json__isnull=False, calories__isnull=True).iterator():  # every user's rows
      fields = _extract_perf_fields(w.performance_graph_json)
      if any(v is not None for v in fields.values()):
          CachedWorkout.objects.filter(pk=w.pk).update(**fields)
  ```
- **Add a template filter**: `workout_filters.py` with `@register.filter`; loaded via `{% load workout_filters %}`
- **Add an intervention**: `/interventions/` → "New Intervention" form. Add dose changes from `/interventions/<pk>/`.
- **Change a dose**: `/interventions/` → "Change Dose" button → enter new dose + start date → confirm. Previous dose `end_date` is auto-set.
- **Clear body commentary cache**: `UserSettings.objects.filter(user__username='<username>').update(ai_body_commentary=None, ai_body_commentary_generated_at=None)` in Django shell, or use the Refresh button on `/body/`.
- **Run intervention analysis**:
  ```bash
  venv/bin/python3 manage.py analyze_intervention --start 2026-01-15 --label "My Supplement 5mg" --window 28 --weight-goal loss [--user USERNAME]
  ```
- **Set up nutrition targets**: `/nutrition/targets/` → fill in height, age, sex, activity level, goal, deficit %. Macro targets auto-compute from latest Withings body comp data.
- **Clear nutrition AI insights cache**: `UserSettings.objects.filter(user__username='<username>').update(ai_nutrition_insights=None, ai_nutrition_insights_generated_at=None, ai_nutrition_insights_range=None)` in Django shell, or use the Refresh button on `/nutrition/analytics/`.
- **Recompute daily nutrition totals** (e.g. after backfill): `from workouts.nutrition import recompute_daily_nutrition; from datetime import date, timedelta; [recompute_daily_nutrition(u, date.today()-timedelta(d)) for d in range(30)]`
- **Override macros manually**: `/nutrition/targets/` → fill in `manual_calories` / `manual_protein_g` etc. Computed values still shown for reference.
- **Recompute nutrition totals** (if DailyStats is out of sync):
  ```python
  from workouts.nutrition import recompute_daily_nutrition
  from datetime import date
  recompute_daily_nutrition(u, date.today())
  ```
- **Log a hunger check (shell)**: `HungerCheck.objects.create(user=u, date=date.today(), context='morning', hunger_level=3)`
- **Clear pattern insights cache**: `UserSettings.objects.filter(user__username='<username>').update(ai_pattern_insights=None, ai_pattern_insights_generated_at=None)` in Django shell, or use the Regenerate button on `/insights/`.
- **Check target fit**: `from workouts.nutrition import evaluate_target_fit; print(evaluate_target_fit(u))`
- **Accept a target adjustment (shell)**: `NutritionProfile.objects.filter(user=u).update(manual_calories=NEW)` and create a `TargetAdjustment(user=u, …)` record manually if bypassing the UI.
- **See most satisfying meals**: `from workouts.nutrition import get_satisfying_meals; print(get_satisfying_meals(u))` — requires post-meal HungerCheck records linked to FoodEntries that have a `source_saved_meal`.
- **Force-regenerate weekly review**: visit `/review/?refresh=1` in the browser, or in the shell: `from workouts.ai import _get_or_generate_weekly_review; from datetime import date, timedelta; _get_or_generate_weekly_review(u, date(2026,5,25), force=True)`.
- **Delete a weekly review to re-generate**: `WeeklyReview.objects.filter(user=u, week_start='2026-05-25').delete()` then reload `/review/`.
- **Daily sync (shell)**: import from `workouts.sync`, not `workouts.views`:
  ```python
  from workouts.sync import _run_peloton_sync_new, _run_garmin_sync_new, _run_wellness_sync, _run_withings_sync_new
  from workouts.users import get_owner
  from datetime import date
  u = get_owner()            # or User.objects.get(username='<username>')
  _run_peloton_sync_new(u)
  _run_garmin_sync_new(u)    # owner only
  _run_wellness_sync(u, [date.today()])
  _run_withings_sync_new(u)
  ```
- **Rotate Peloton cookie**: `/settings/integrations/` → Peloton Session Cookie card → expand "Update Cookie" → paste new `peloton_session_id` value (validated against `/api/me`; no user id needed)
- **Migrate Peloton creds from .env to DB (one-time)**: `venv/bin/python3 manage.py migrate_peloton_creds [--user USERNAME]`
- **Migrate Withings tokens from file to DB (one-time)**: `venv/bin/python3 manage.py migrate_withings_tokens [--user USERNAME]`
- **Subscribe Withings webhook**: `venv/bin/python3 manage.py subscribe_withings_webhook [--user USERNAME]` (also `list_withings_webhooks`, `revoke_withings_webhook`; the web connect flow subscribes automatically)
- **Run daily sync manually**: `venv/bin/python3 manage.py sync_daily` — every active user in turn, each isolated (one user's failure doesn't stop the rest; output lines are prefixed `[sync_daily] [username]`); `--user USERNAME` for one user. Per user: Peloton, Garmin (owner), Google Health — each only if enabled and connected; users with nothing connected are skipped.
- **Run daily sync only if stale**: `venv/bin/python3 manage.py sync_daily --if-stale 8`
- **Management commands**: every command that reads or writes user data takes `--user USERNAME` (default: the owner) — `withings_login`, `google_health_login`, `analyze_intervention`, `migrate_*`, `*_withings_webhook(s)`, `dedupe_*`, `seed_programs`, `associate_programs`, `backfill_class_plans`, `backfill_ftp`, `sync_daily`. `seed_demo` defaults to a `demo` user (created if missing) and only clears/seeds that user. `garmin_login` and `google_health_register_webhook` are project-level.
- **Check/clear webhook errors**: visit `/settings/integrations/errors/`, or in shell: `from workouts.models import WebhookError; WebhookError.objects.all().delete()`
- **First-time Google Health (CLI)**: `venv/bin/python3 manage.py google_health_login [--user USERNAME]`
- **Reconnect Google Health (web)**: `/settings/integrations/` → "Reconnect" next to Google Health. Needed roughly every 7 days while the Google Cloud project is in "Testing" status (refresh tokens expire).
- **Register/update the Google Health webhook subscriber**: `venv/bin/python3 manage.py google_health_register_webhook`
- **Enable/disable a data source**: `/settings/integrations/` → toggle. Disabling stops sync calls to that source entirely (not just hides its data) — see `_integration_enabled` gating.
- **Run Google Health sync (shell)**: `from workouts.sync import _run_google_health_sync_new; _run_google_health_sync_new(u)`
- **Backfill computed readiness for existing days**: re-run the Google Health wellness sync over the range — `compute_readiness_proxy()` only writes `computed_readiness_score`/`computed_readiness_label` as a side effect of `_run_google_health_wellness_sync()`, there's no standalone backfill command.
