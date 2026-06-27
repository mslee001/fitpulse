# CLAUDE.md — FitPulse

A Django app (renamed from "Peloton Dashboard") that pulls workout data from Peloton, Garmin Connect, and Withings and displays it in a personal dashboard.

---

## Project Structure

```
peloton_dashboard/       # Django project root (settings.py, urls.py)
workouts/                # Main app
  models.py              # CachedWorkout + UserSettings + DailyStats + BodyMeasurement + Intervention + DoseChange + SavedAnalysis + NutritionProfile + FoodEntry + SavedMeal + HungerCheck + SideEffectLog + TargetAdjustment + WeeklyReview + WithingsAuth + PelotonAuth; SQLite-backed
  views.py               # All HTML-rendering views only
  sync.py                # All sync logic + sync API endpoints (returns JsonResponse)
  ai.py                  # Anthropic API calls, insights generation, day analysis, next-workout rec, body commentary, intervention interpretation, nutrition parsing/suggestions, compare analysis, pattern insights, weekly review
  analysis.py            # run_intervention_analysis() — shared logic for analyze_intervention command + Trends page
  nutrition.py           # compute_macro_targets() (Mifflin-St Jeor BMR/TDEE) + recompute_daily_nutrition() rollup + evaluate_target_fit() + get_satisfying_meals()
  services/
    peloton_client.py    # PelotonClient — all Peloton API calls
    garmin_client.py     # GarminClient — Garmin Connect API calls + parsers
    withings_client.py   # WithingsClient — Withings OAuth2 + body measurement API
  templatetags/workout_filters.py
  management/commands/
    backfill_ftp.py              # Stamp per-workout FTP from historical values
    garmin_login.py              # One-time interactive Garmin auth
    withings_login.py            # One-time interactive Withings OAuth flow
    analyze_intervention.py      # Before/after analysis across wellness + body composition
    sync_daily.py                # Automated daily sync (Peloton + Garmin activities + wellness)
    migrate_withings_tokens.py   # One-time migration of tokens from file to DB
    migrate_peloton_creds.py     # One-time migration of creds from .env to DB
    subscribe_withings_webhook.py  # Subscribe Withings push webhook
    list_withings_webhooks.py    # List active Withings webhook subscriptions
    revoke_withings_webhook.py   # Revoke a Withings webhook subscription
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
  review.html            # Weekly Review: AI Sonnet review of most recently completed Mon–Sun week; archive of past weeks in collapsible details
  settings.html          # FTP setting + Peloton credentials card (collapsible)
  partials/
    insights.html              # Analytics AI insights partial (HTMX polling target)
    workout_list.html          # Workout list rows partial
    nutrition_parse_result.html      # Food parse preview (editable items before confirming)
    nutrition_entry_row.html         # Read-only food entry table row
    nutrition_edit_row.html          # Inline edit form for a food entry
    nutrition_suggestions.html       # AI meal suggestion cards with "Log this" + "★ Save" buttons (HTMX)
    nutrition_insights.html          # Nutrition analytics AI insights partial (HTMX)
    pattern_insights.html            # Pattern insights partial (HTMX target for /insights/ page)
static/css/main.css      # All styles — single flat file, CSS variables
```

---

## Key Architecture

### Auth
- **Peloton**: session cookie (`peloton_session_id`) from browser DevTools. `/auth/login` is dead (403). Stored in `PelotonAuth` DB model (singleton pk=1). Rotate via `/settings/`. `PelotonAuthError` raised on 403 with link to `/settings/`.
- **Garmin**: `garminconnect` lib from `zpython-garminconnect-master/`. First-time: `venv/bin/python3 manage.py garmin_login`. Tokens saved to `~/.garminconnect/` and auto-refresh. Never attempt password login from a web request.
- **Withings**: OAuth 2.0. First-time: `venv/bin/python3 manage.py withings_login`. Tokens saved to DB (`WithingsAuth` singleton pk=1). Auto-refreshes 5 min before expiry. Credentials in `.env`: `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `WITHINGS_REDIRECT_URI`.
- **Python**: venv uses Python 3.14 (Homebrew). Always `venv/bin/python3 manage.py ...`.

### Local Cache (`CachedWorkout`)
SQLite-backed. Never query either API in real time for list views — sync first, then read from DB.

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

**Leaderboard sync tracking:**
- `leaderboard_synced_at` — stamped after every leaderboard detail sync attempt (success or null-result). Used to prevent infinite re-syncing of workouts that Peloton returns null rank for. `raw_data__has_leaderboard_metrics=True` is the reliable sentinel for whether a discipline can have leaderboard data (cycling/running/walking = True; strength/yoga/meditation = False).

### DailyStats Model
One row per calendar day. Stores Garmin wellness data synced via `GarminClient.get_wellness_data()`, plus Withings body composition aggregates.

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

### Intervention Model
Tracks health/lifestyle interventions (medications, supplements, protocols, habits).

**Fields:** `name`, `category` (CATEGORY_CHOICES: medication/supplement/training/nutrition/habit/other), `start_date`, `end_date` (null = ongoing), `expected_effects`, `notes`, `is_active` property

**Key properties/methods:**
- `is_active` — True if `end_date` is null or in the future
- `current_dose` — latest `DoseChange` ordered by `start_date` (or None)
- `dose_summary` — e.g. `"2.5mg → 5mg → 7.5mg"` (all dose changes joined with →)
- `dose_at(target_date)` — returns the `DoseChange` active on a given date (binary search by `start_date`), or None
- `duration_days` — days since `start_date`

### DoseChange Model
Tracks dose history within an intervention. One row per dose period.

**Fields:** `intervention` (FK), `dose` (CharField, e.g. `"7.5mg"`), `start_date`, `end_date` (null = current dose), `notes`
**Constraints:** unique on `["intervention", "start_date"]`
**Properties:** `duration_days` — days from `start_date` to `end_date` (or today)

**Dose management flow:** When adding a new dose via `intervention_detail` or `intervention_quick_dose`, the previous active dose's `end_date` is automatically set to `new_start_date - 1 day`.

### SavedAnalysis Model
Stores saved Trends page analyses for later retrieval.

**Fields:** `intervention` (FK, nullable), `label`, `before_start`, `before_end`, `after_start`, `after_end`, `weight_goal`, `result_json` (full analysis dict), `ai_interpretation` (Sonnet text), `created_at`

### NutritionProfile Model
Singleton (pk=1). Stores inputs for the macro target calculator.

**Fields:** `height_cm`, `age`, `biological_sex` (male/female), `activity_level` (sedentary/light/moderate/active/very_active), `goal` (loss/gain/maintain), `deficit_pct` (default 20.0), `protein_g_per_kg_lean` (default 2.2), `manual_calories`, `manual_protein_g`, `manual_carbs_g`, `manual_fat_g`, `manual_fiber_g`

**Access:** `NutritionProfile.get()` — creates if missing.

### FoodEntry Model
One logged food/meal event per day.

**Fields:** `date`, `logged_at`, `meal` (breakfast/lunch/dinner/snack), `raw_text`, `items_json` (list of parsed item dicts), `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `ai_model`, `ai_confidence`, `edited_by_user`, `is_favorite`, `source_saved_meal` (FK to SavedMeal, nullable)

### SavedMeal Model
Saved meal template for one-click re-logging.

**Fields:** `name`, `meal`, `items_json`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `times_logged` (auto-incremented on relog), `created_at`

**Ordering:** by `-times_logged`, then `name` (most-used first).

### HungerCheck Model
One hunger/satiety reading. Contexts auto-detected from time of day on the nutrition page.

**Fields:** `timestamp`, `date`, `context` (morning/pre_meal/post_meal/evening/random), `hunger_level` (1–10), `fullness_level` (1–10, post_meal only, nullable), `related_meal` (FK to FoodEntry, nullable), `notes`

### SideEffectLog Model
One logged symptom event.

**Fields:** `timestamp`, `date`, `symptom` (nausea/bloating/constipation/diarrhea/reflux/fatigue/headache/injection_site/dizziness/dry_mouth/other), `severity` (1=Mild/2=Moderate/3=Severe), `related_meal` (FK to FoodEntry, nullable), `related_intervention` (FK to Intervention, nullable), `notes`

### TargetAdjustment Model
Records every accepted calorie target change for history tracking.

**Fields:** `timestamp`, `previous_calories`, `new_calories`, `reason`, `auto_suggested` (bool), `accepted_by_user` (bool)

**Ordering:** by `-timestamp`.

### WeeklyReview Model
Stores one AI-generated review per completed Mon–Sun calendar week.

**Fields:** `week_start` (DateField, unique), `content` (TextField — Sonnet markdown text), `generated_at` (DateTimeField, auto_now_add), `ai_model` (CharField)

**Property:** `week_end` — `week_start + 6 days`

**Ordering:** by `-week_start`. Displayed at `/review/` (current week) and in a collapsible archive.

### WithingsAuth Model
Singleton (pk=1). Stores Withings OAuth tokens in DB instead of a file on disk.

**Fields:** `userid`, `access_token`, `refresh_token`, `token_expires_at` (DateTimeField), `last_subscribed_at`, `last_webhook_received_at`, `webhook_subscription_active` (bool), `updated_at`

**Access:** `WithingsAuth.get()` — returns the singleton or None.

### PelotonAuth Model
Singleton (pk=1). Stores Peloton session cookie in DB.

**Fields:** `session_id`, `user_id`, `last_updated` (auto_now), `notes`

**Property:** `masked_session_id` — shows `…{last4}` of the session ID.

**Access:** `PelotonAuth.get()` — returns the singleton or None. `PelotonClient` reads from this model; raises `PelotonAuthError` on 403.

### UserSettings (additional field)
- `last_daily_sync_at` — `DateTimeField(null=True)`. Stamped by the `sync_daily` management command on full success. Used in the nav sync dropdown and settings page footer.

### BodyMeasurement Model
Raw per-weigh-in records from Withings. Multiple per day is normal. Deduped by `withings_grpid` (Withings assigns one grpid per step-on session).

**Fields:** `measured_at`, `date` (local), `source` (default `"withings"`), `weight_lb`, `fat_mass_lb`, `fat_free_mass_lb`, `muscle_mass_lb`, `bone_mass_lb`, `hydration_lb`, `fat_ratio_pct`, `withings_grpid`, `raw_data`

**Sync flow:** Withings API → upsert `BodyMeasurement` → `_update_daily_stats_for_dates()` recomputes `DailyStats` body composition fields using the earliest weigh-in of each day.

### Garmin ↔ Peloton Deduplication
`_is_peloton_duplicate()` binary-searches a sorted timestamp index with ±120s window. For **running** duplicates, instead of skipping entirely, `_augment_peloton_run()` stamps the matching Peloton workout with the Garmin form metrics and merges form-metric time-series into `performance_graph_json`. Non-running duplicates are skipped.

### Garmin `performance_graph_json` Format
Uses the top-level `metricDescriptors` list (with `metricsIndex` positions) — **not** per-entry `metricDescriptor`. `parse_performance()` builds an index map then extracts values per point. Normalized to `metrics_by_slug`:
```
directHeartRate→heart_rate  directSpeed→speed  directPower→output
directBikeCadence/directDoubleCadence→cadence  directElevation→incline
directStrideLength→stride_length  directVerticalOscillation→vertical_oscillation
directVerticalRatio→vertical_ratio  directGroundContactTime→ground_contact_time
```
`directRunCadence` is strides/min (half steps); use `directDoubleCadence` for steps/min.

### Sync Endpoints
- Peloton: `Sync New` (`/api/sync/new/`), `Sync All` (`/api/sync/all/`)
- Garmin activities: `Garmin Sync New` (`/api/sync/garmin/new/`), `Garmin Sync All` (`/api/sync/garmin/all/`)
- Garmin wellness: `Garmin Wellness Today` (`/api/sync/garmin/wellness/`), `?date=YYYY-MM-DD`, `?days=N` (max 90)
- Withings: `Withings Sync New` (`/api/sync/withings/new/`), `Withings Sync All` (`/api/sync/withings/all/`)
- `POST /api/withings/webhook/` — Withings push webhook. `@csrf_exempt`. Listed in `PUBLIC_PATHS` (no auth required). Called by Withings when body measurements change. Fetches measurements for the notified time window and upserts them. Always returns HTTP 200.
- All sync types are accessible from the Sync dropdown in the nav.

### Detail Page Templates
All five discipline-specific detail pages extend `detail_base.html`, which owns:
- Page header, PR banner, class info card
- Full sidebar: leaderboard, previous attempts list (with compare checkboxes), "View all times" link, external links (Peloton / Garmin Connect)
- External links appear for all workouts: `workout.external_url` for the primary source, plus a secondary Garmin Connect link when `workout.garmin_activity_id` is set (Peloton run augmented with Garmin data)

Child templates override these blocks: `discipline_tag`, `page_title`, `pr_sub`, `detail_main`, `history_item_stats`, `recent_section`, `detail_scripts`.

### Calendar & Day View
- **Calendar** (`/calendar/`, `/calendar/<year>/<month>/`): monthly grid, discipline dots per day, training readiness score overlaid on each cell, next-workout AI recommendation in sidebar.
- **Day view** (`/day/YYYY-MM-DD/`): lazy-syncs Garmin wellness on first load if missing, shows all workouts for the day with correct HR/effort, plus AI day analysis.

### AI (Direct HTTP, not anthropic package)
All Anthropic calls live in `workouts/ai.py`. They use `requests.post` to `https://api.anthropic.com/v1/messages` with `_anthropic_headers(os.environ.get("ANTHROPIC_API_KEY", ""))`. The `anthropic` Python package is **not installed**.

- **Analytics insights**: Batch API (`/api/analytics/insights/`). Polls via `/api/analytics/check-insights/` (HTMX). Long-form multi-section analysis. Cached in `UserSettings.ai_insights`.
- **Day analysis** (`_get_or_generate_day_analysis`): Claude Haiku, synchronous, cached 7 days in `DailyStats.ai_day_analysis`. Structured output: `HEADLINE:` + bullet points.
- **Next workout rec** (`_get_or_generate_next_workout`): Claude Haiku, synchronous, cached 24h in `DailyStats.ai_next_workout`. Force-refresh via `POST /api/next-workout/refresh/`. Structured output: `INTENSITY:` / `ACTIVITY:` / `REASON:`. Prompt includes: workout titles (not just discipline), muscle groups worked (high/moderate buckets from perf graph), exercise names for strength sessions, and explicit cardio vs. strength guidance rules. Detects if user already trained today and frames as "tomorrow" if so.
- **Body commentary** (`_get_or_generate_body_commentary`): Claude Haiku, synchronous, cached 24h in `UserSettings.ai_body_commentary`. Interprets recent body composition and recovery trends. Refreshed via `POST /api/body/commentary/refresh/`.
- **Intervention interpretation** (`_generate_intervention_interpretation`): Claude Sonnet, called on-demand from Trends page run-analysis flow. Returns free-form text interpreting the before/after metrics in context of the intervention and its dose history.
- **Compare analysis** (`compare_analysis`): Claude Haiku, on-demand HTMX endpoint (`POST /api/compare/analysis/`). Generates a short narrative comparing 2–4 workouts side-by-side using extracted stats.
- **Food parsing** (`parse_food_text`): Claude Haiku, synchronous. Called from `POST /nutrition/api/parse/`. Converts freeform food description into structured items list with per-item macros. Also calls OpenFoodFacts for branded foods, detects meal kit services (Home Chef, Factor, etc.) and fetches their nutrition data from their APIs.
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

### Withings Client
`WithingsClient` in `workouts/services/withings_client.py`:
- Tokens read from and written to `WithingsAuth` DB singleton (not `~/.fitpulse/withings_tokens.json`)
- Measurement types fetched: weight (1), fat free mass (5), fat ratio (6), fat mass (8), muscle mass (76), hydration (77), bone mass (88), pulse wave velocity (91), vascular age (155)
- Value decoding: `raw_value * 10^unit` — always apply the unit exponent
- kg → lb conversion: multiply by 2.20462
- 503 rate-limit: logs warning and returns empty (doesn't crash sync)
- Status 100–105: raises with "re-run withings_login" message
- 401: refreshes token and retries once

### Nutrition Module (`workouts/nutrition.py`)
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
| `POST /nutrition/api/parse/` | `nutrition_parse_api` | Parse raw food text → `nutrition_parse_result.html` partial (HTMX) |
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
`venv/bin/python3 manage.py analyze_intervention --start YYYY-MM-DD --label "..." --window 28 --weight-goal loss|gain|maintain`

Compares DailyStats metrics before vs. after an intervention date. Prints before/after means with ✓/✗/→ direction indicators across six sections: RECOVERY, SLEEP, STRESS, ACTIVITY, BODY COMPOSITION, NUTRITION. `--weight-goal` controls whether weight going down is ✓ or ✗ (default: `loss`). Thin wrapper around `run_intervention_analysis()` in `workouts/analysis.py`.

### Body & Trends Pages
- **Body** (`/body/`): 90/180/365-day range toggle. Stat cards (current weight, fat%, muscle mass, avg sleep, avg HRV). Weight chart with 7-day rolling avg, full-height solid intervention start lines, half-height dashed dose-change lines. Body composition stacked area chart. Recovery sparklines (HRV, sleep, resting HR, body battery). Active interventions card with `current_dose`. 7-day nutrition summary card (avg calories, protein, fiber; shows "Log more days" prompt if <3 days logged). **Pattern insight teaser** (cyan card showing headline of latest Sonnet pattern insight, links to `/insights/`; only shown when insights exist). **Recent symptoms card** (last 3 days of `SideEffectLog`, links to `/symptoms/`; only shown when symptoms exist). AI body commentary (Haiku, 24h cache, includes 7-day nutrition context when ≥3 days logged).
- **Trends** (`/trends/`): Select an intervention → see dose timeline → pick before/after window (auto-fills from intervention dates, or custom) → Run Analysis → metrics table (6 sections including NUTRITION, ✓/✗/→) → Sonnet AI interpretation → Save. Saved analyses listed below with load/delete.
- **Saved analysis detail** (`/trends/analysis/<pk>/`): Full view of a single saved analysis. Delete via `POST /trends/analysis/<pk>/delete/`.
- **Interventions** (`/interventions/`): List with `dose_summary`, inline Change Dose form (3-click: select new dose + date → confirm), "Manage Doses" button.
- **Intervention detail** (`/interventions/<pk>/`): Full dose timeline table with per-row edit/end/delete. Add Dose form auto-ends previous active dose.
- **Intervention edit** (`/interventions/<pk>/edit/`): Edit intervention name, category, dates, expected_effects, notes.

### Other Key Patterns
- **FTP**: `workout.ftp` per-workout (stamped at sync); `backfill_ftp.py` for history. Use `workout.ftp` in templates, not `UserSettings.get().ftp`.
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
# Peloton credentials are stored in the DB and managed via /settings/
ANTHROPIC_API_KEY=...
GARMIN_EMAIL=...
GARMIN_PASSWORD=...
WITHINGS_CLIENT_ID=...
WITHINGS_CLIENT_SECRET=...
WITHINGS_REDIRECT_URI=http://localhost:8000/auth/withings/callback/
WITHINGS_CALLBACK_URL=https://fitpulse-jp2p.onrender.com/api/withings/webhook/  # used by subscribe_withings_webhook
DJANGO_DEBUG=True
```

---

## Common Tasks

- **Add a model field**: `models.py` → `makemigrations && migrate` → populate in `from_api()`, `from_garmin()`, or `apply_detail()` → add to `DETAIL_FIELDS` if from detail endpoint
- **Add a Garmin metric**: add to `parse_activity()` return dict, add slug to `SLUG_MAP` in `parse_performance()`, add field to `from_garmin()`
- **Update FTP**: `/settings/` → update `FTP_HISTORY` in `backfill_ftp.py` → `venv/bin/python3 manage.py backfill_ftp`
- **First-time Garmin**: `venv/bin/python3 manage.py garmin_login`
- **First-time Withings**: `venv/bin/python3 manage.py withings_login`
- **Clear stale AI insights**: `UserSettings.objects.filter(pk=1).update(ai_insights=None, ai_insights_batch_id=None)` in Django shell
- **Clear day analysis cache**: `DailyStats.objects.filter(date=d).update(ai_day_analysis=None, ai_day_generated_at=None)`
- **Clear next-workout rec**: `DailyStats.objects.filter(date=date.today()).update(ai_next_workout=None, ai_next_workout_generated_at=None)`
- **Backfill wellness data**: hit `/api/sync/garmin/wellness/?days=30` or use the Sync dropdown
- **Backfill perf graph fields** (calories/distance/HR/pace from cached perf graphs):
  ```python
  from workouts.models import CachedWorkout
  from workouts.sync import _extract_perf_fields
  for w in CachedWorkout.objects.filter(source='peloton', performance_graph_json__isnull=False, calories__isnull=True).iterator():
      fields = _extract_perf_fields(w.performance_graph_json)
      if any(v is not None for v in fields.values()):
          CachedWorkout.objects.filter(pk=w.pk).update(**fields)
  ```
- **Add a template filter**: `workout_filters.py` with `@register.filter`; loaded via `{% load workout_filters %}`
- **Add an intervention**: `/interventions/` → "New Intervention" form. Add dose changes from `/interventions/<pk>/`.
- **Change a dose**: `/interventions/` → "Change Dose" button → enter new dose + start date → confirm. Previous dose `end_date` is auto-set.
- **Clear body commentary cache**: `UserSettings.objects.filter(pk=1).update(ai_body_commentary=None, ai_body_commentary_generated_at=None)` in Django shell, or use the Refresh button on `/body/`.
- **Run intervention analysis**:
  ```bash
  venv/bin/python3 manage.py analyze_intervention --start 2026-01-15 --label "My Supplement 5mg" --window 28 --weight-goal loss
  ```
- **Set up nutrition targets**: `/nutrition/targets/` → fill in height, age, sex, activity level, goal, deficit %. Macro targets auto-compute from latest Withings body comp data.
- **Clear nutrition AI insights cache**: `UserSettings.objects.filter(pk=1).update(ai_nutrition_insights=None, ai_nutrition_insights_generated_at=None, ai_nutrition_insights_range=None)` in Django shell, or use the Refresh button on `/nutrition/analytics/`.
- **Recompute daily nutrition totals** (e.g. after backfill): `from workouts.nutrition import recompute_daily_nutrition; from datetime import date, timedelta; [recompute_daily_nutrition(date.today()-timedelta(d)) for d in range(30)]`
- **Override macros manually**: `/nutrition/targets/` → fill in `manual_calories` / `manual_protein_g` etc. Computed values still shown for reference.
- **Recompute nutrition totals** (if DailyStats is out of sync):
  ```python
  from workouts.nutrition import recompute_daily_nutrition
  from datetime import date
  recompute_daily_nutrition(date.today())
  ```
- **Log a hunger check (shell)**: `HungerCheck.objects.create(date=date.today(), context='morning', hunger_level=3)`
- **Clear pattern insights cache**: `UserSettings.objects.filter(pk=1).update(ai_pattern_insights=None, ai_pattern_insights_generated_at=None)` in Django shell, or use the Regenerate button on `/insights/`.
- **Check target fit**: `from workouts.nutrition import evaluate_target_fit; print(evaluate_target_fit())`
- **Accept a target adjustment (shell)**: update `NutritionProfile.objects.filter(pk=1).update(manual_calories=NEW)` and create a `TargetAdjustment` record manually if bypassing the UI.
- **See most satisfying meals**: `from workouts.nutrition import get_satisfying_meals; print(get_satisfying_meals())` — requires post-meal HungerCheck records linked to FoodEntries that have a `source_saved_meal`.
- **Force-regenerate weekly review**: visit `/review/?refresh=1` in the browser, or in the shell: `from workouts.ai import _get_or_generate_weekly_review; from datetime import date, timedelta; _get_or_generate_weekly_review(date(2026,5,25), force=True)`.
- **Delete a weekly review to re-generate**: `WeeklyReview.objects.filter(week_start='2026-05-25').delete()` then reload `/review/`.
- **Daily sync (shell)**: import from `workouts.sync`, not `workouts.views`:
  ```python
  from workouts.sync import _run_peloton_sync_new, _run_garmin_sync_new, _run_wellness_sync, _run_withings_sync_new
  from datetime import date
  _run_peloton_sync_new()
  _run_garmin_sync_new()
  _run_wellness_sync([date.today()])
  _run_withings_sync_new()
  ```
- **Rotate Peloton cookie**: `/settings/` → expand "Update Cookie" → paste new `peloton_session_id` value
- **Migrate Peloton creds from .env to DB (one-time)**: `venv/bin/python3 manage.py migrate_peloton_creds`
- **Migrate Withings tokens from file to DB (one-time)**: `venv/bin/python3 manage.py migrate_withings_tokens`
- **Subscribe Withings webhook**: `venv/bin/python3 manage.py subscribe_withings_webhook`
- **Run daily sync manually**: `venv/bin/python3 manage.py sync_daily`
- **Run daily sync only if stale**: `venv/bin/python3 manage.py sync_daily --if-stale 8`
