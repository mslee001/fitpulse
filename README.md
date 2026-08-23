# FitPulse

A personal fitness dashboard built with Django. Syncs workout and wellness data from Peloton, Garmin Connect, Withings, and Google Health to a local cache and surfaces detailed stats, charts, and AI-powered insights that the default apps don't provide.

> **Personal use only.** This tool uses Peloton's unofficial internal API via session cookie. It is not affiliated with Peloton, Garmin, Withings, or Google and is intended for a single user running it locally.

---

## Features

- **Dashboard** — overview of total workouts and discipline breakdown
- **Workout history** — filterable, sortable list of all cached workouts
- **Discipline-specific detail pages** — power zone charts for cycling, pace/splits/HR/running form for runs, muscle groups and exercise sets for strength
- **Running form** — Garmin foot pod metrics (cadence, stride length, vertical oscillation, vertical ratio, ground contact time) overlaid on run charts; walking filtered from averages
- **Calendar** — monthly grid with per-day workout dots, training readiness scores, and a next-workout AI recommendation
- **Day view** — per-day wellness signals (HRV, sleep, body battery, readiness) alongside workouts and an AI day analysis
- **Analytics** — weekly volume, discipline mix, performance trends, and AI-generated training insights
- **Garmin wellness** — daily body battery, HRV, sleep score, resting HR, training load, and training readiness synced from Garmin Connect
- **Google Health** — alternate/supplemental wellness and exercise sync (resting HR, HRV, sleep, steps, floors, calories, respiratory rate, SpO2, Active Zone Minutes, skin temperature, HR zone minutes) via OAuth, plus real-time push-webhook sync in addition to manual sync. When Garmin's own readiness score isn't available, a computed readiness score (Low/Moderate/High) is derived from Google Health's HRV, sleep, and resting HR against your personal baseline, so the readiness ring, calendar, and AI features keep working either way
- **Integrations page** — enable/disable each data source independently, see auth/connection status and last-synced time, run a full backfill ("Sync All") per source, and reconnect Google Health from the browser when its token expires
- **Webhook error log** — failed webhook-triggered background syncs are recorded and viewable in the UI (self-pruning after 2 weeks) instead of only living in server logs
- **Compare** — side-by-side comparison of 2–4 workouts with AI narrative analysis
- **Body composition** — weight trend chart with rolling averages, body composition stacked chart, and recovery sparklines (HRV, sleep, resting HR, body battery) from Withings scale data
- **Interventions & Trends** — track health interventions (medications, supplements, habits) with dose history; before/after statistical analysis across 20+ wellness metrics with AI interpretation; save and revisit analyses
- **Nutrition** — freeform food logging with AI macro parsing, daily macro targets (Mifflin-St Jeor BMR/TDEE), saved meals for one-click re-logging, and AI meal suggestions that adapt to your current hunger level and any GI symptoms logged
- **Hunger & satiety tracking** — log hunger level (1–10) before/after meals; morning hunger trends surfaced on the nutrition analytics page
- **Symptom log** — track GI and other side effects with severity; symptoms inform meal suggestions and appear as a summary on the body trends page
- **Pattern insights** — Claude Sonnet deep-analysis of 60 days of integrated data (weight, recovery, nutrition, hunger, symptoms, workouts, interventions) to surface non-obvious correlations
- **Weekly review** — AI-generated summary of each completed Mon–Sun week covering weight trend, nutrition adherence, training, and one focus for the next week; archived for all past weeks
- **Settings** — manage your current FTP and Peloton session cookie; historical FTP tracked per workout for accurate power zone charts

---

## Requirements

- Python 3.11+
- A Peloton account
- A Garmin Connect account (optional — needed for running form, wellness data, and Garmin-tracked workouts)
- A Withings account (optional — needed for body composition tracking)
- A Google Health API project (optional — alternate/supplemental wellness and exercise data source; also enables the computed readiness score fallback when Garmin isn't connected)
- An Anthropic API key (optional — needed for AI insights, day analysis, next-workout recommendations, and nutrition parsing)

---

## Setup

**1. Clone and create a virtual environment**

```bash
git clone <repo-url>
cd peloton_dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Get your Peloton credentials**

Peloton's login endpoint is no longer publicly accessible, so authentication is done via session cookie:

1. Log into [members.onepeloton.com](https://members.onepeloton.com) in your browser
2. Open DevTools → Application → Cookies → `members.onepeloton.com`
3. Copy the value of `peloton_session_id`
4. Find your user ID: it appears in the URL when you visit your profile page (`/members/<user_id>/overview`)

After the app is running, enter these values in the **Settings** page (`/settings/`) under "Peloton Credentials". They are stored in the app's database — not in `.env`.

**3. Create a `.env` file**

```bash
cp .env.example .env
```

Then fill in your values. Generate a Django secret key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Note: Peloton credentials (`peloton_session_id` and user ID) are stored in the app's database and managed via the Settings page — they do not go in `.env`.

**4. Run migrations**

```bash
venv/bin/python3 manage.py migrate
```

**5. Authenticate with Garmin (first time only)**

Garmin requires an interactive login to obtain and cache auth tokens:

```bash
venv/bin/python3 manage.py garmin_login
```

Tokens are saved to `~/.garminconnect/` and auto-refresh on subsequent syncs. You should only need to do this once per machine.

**6. Authenticate with Withings (first time only, optional)**

If you have a Withings scale and want body composition data:

1. Create a Withings developer account at [developer.withings.com](https://developer.withings.com) and register an app
2. Add your `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, and `WITHINGS_REDIRECT_URI` to `.env`
3. Run the OAuth flow:

```bash
venv/bin/python3 manage.py withings_login
```

Tokens are saved to the app's database and auto-refresh.

**7. Authenticate with Google Health (first time only, optional)**

If you want Google Health as a wellness/exercise data source:

1. Create a Google Cloud project and enable the Google Health API, then create OAuth 2.0 credentials
2. Add `GOOGLE_HEALTH_CLIENT_ID`, `GOOGLE_HEALTH_CLIENT_SECRET`, and `GOOGLE_HEALTH_REDIRECT_URI` to `.env`
3. Run the OAuth flow:

```bash
venv/bin/python3 manage.py google_health_login
```

Tokens are saved to the app's database. Refresh tokens expire after 7 days while the Google Cloud project is in "Testing" status — when that happens, reconnect from the browser instead of the CLI: go to **Integrations** (`/settings/integrations/`) and click **Reconnect** next to Google Health.

**8. Start the server**

```bash
venv/bin/python3 manage.py runserver
```

Open [http://localhost:8000](http://localhost:8000).

---

## Setting Up on a New Machine

After cloning the repo:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Then:
1. Copy `.env.example` to `.env` and fill in your values
2. Run `venv/bin/python3 manage.py migrate`
3. Run `venv/bin/python3 manage.py garmin_login` to authenticate Garmin
4. If using Withings, run `venv/bin/python3 manage.py withings_login` (or `migrate_withings_tokens` if migrating tokens from a previous file-based setup)
5. If using Google Health, run `venv/bin/python3 manage.py google_health_login`
6. Start the server and enter your Peloton credentials via the **Settings** page (`/settings/`)
7. Use **Sync All** for each source from the **Integrations** page (`/settings/integrations/`) to pull your full history

Note: `db.sqlite3` is not in the repo — each machine starts with an empty database and needs to sync data fresh.

---

## Demo Mode

A seed command generates a fully populated demo database with 90 days of realistic fake data — no Peloton/Garmin/Withings account needed.

**What's included:**
- 44 workouts across cycling, running, strength, and yoga with realistic metrics, HR zones, and running form data
- 91 days of Garmin wellness data (weight trending down, HRV, sleep, body battery, stress, training readiness)
- 2 interventions: Semaglutide with a 3-step dose escalation history, and Creatine
- 30 days of food log entries with macro breakdowns, 5 saved meals, hunger checks, and symptom logs
- Pre-loaded AI insights, body commentary, nutrition insights, pattern insights, and a weekly review

**Create and seed the demo database:**

```bash
DB_FILE=demo.sqlite3 venv/bin/python3 manage.py migrate
DB_FILE=demo.sqlite3 venv/bin/python3 manage.py seed_demo
```

**Start the server in demo mode:**

```bash
DB_FILE=demo.sqlite3 venv/bin/python3 manage.py runserver
```

**Switch back to your live data:**

```bash
venv/bin/python3 manage.py runserver
```

The `DB_FILE` environment variable controls which SQLite file is used. When omitted, it defaults to `db.sqlite3` (your real data). The two databases are completely independent — switching between them is instant and non-destructive.

To reset the demo database at any time, just re-run `seed_demo` against it (it clears all data first).

---

## Syncing Data

All data is stored in a local cache. Nothing is fetched in real time for page views — sync first, then browse.

Each source is independent — no combined "sync everything" button, and syncing them in any order (or combination) is safe. Cross-source duplicates (e.g. the same run recorded by both Peloton and Garmin, or Peloton and Google Health) are automatically reconciled after every Peloton sync, regardless of which order you synced in.

**Nav Sync dropdown** — fast, incremental syncs, safe to run often:

| Option | What it does |
|---|---|
| **Peloton Sync New** | New Peloton workouts |
| **Garmin Sync New** | New Garmin activities |
| **Garmin Wellness Today** | Today's wellness data (body battery, HRV, sleep, readiness) |
| **Withings Sync New** | New Withings body composition measurements |
| **Google Health Sync New** | New Google Health wellness (trailing 7 days) + exercise data |

**Integrations page** (`/settings/integrations/`) — full historical backfills, plus enable/disable per source:

| Option | What it does |
|---|---|
| **Peloton Sync All** | Full backfill of all Peloton workouts |
| **Garmin Sync All** | Full backfill of all Garmin activities |
| **Withings Sync All** | Full Withings history backfill |
| **Google Health Sync All** | Full Google Health history backfill (wellness + exercise, ~3 years back) |

Google Health can also push updates automatically via a webhook, in addition to manual syncing.

**Recommended first-time setup:**
1. From the Integrations page, run **Sync All** for each source you've connected
2. Browse — charts, running form, and wellness data will all be populated

Session cookies expire periodically. When Peloton syncing stops working, grab a fresh `peloton_session_id` from your browser and update it via the **Settings** page (`/settings/`). Garmin and Withings tokens auto-refresh. Google Health refresh tokens expire every 7 days while the Google Cloud project is in "Testing" status — reconnect from the Integrations page when that happens.

---

## FTP (Cycling Power Zones)

Your current FTP is set in the Settings page (`/settings/`). Each workout is stamped with your FTP at the time of sync, keeping historical power zone charts accurate as your FTP changes.

To retroactively correct past workouts after an FTP update, edit `FTP_HISTORY` in `workouts/management/commands/backfill_ftp.py` and run:

```bash
venv/bin/python3 manage.py backfill_ftp            # apply
venv/bin/python3 manage.py backfill_ftp --dry-run  # preview without writing
```

---

## Automated Daily Sync

`sync_daily` is a management command that runs Garmin activities, Garmin wellness, and Peloton sync in sequence. It does not include Withings or Google Health — Withings has its own push webhook, and Google Health is covered by its own push webhook plus manual sync from the nav/Integrations page.

```bash
venv/bin/python3 manage.py sync_daily
```

Flags:
- `--skip-peloton` — skip Peloton sync (Garmin only)
- `--wellness-days N` — days of wellness to sync (default: 2, catches today + yesterday)
- `--if-stale HOURS` — only sync if last sync was more than N hours ago

On macOS, two launchd plists automate this (stored in `~/Library/LaunchAgents/`, not the repo):
- `com.fitpulse.sync-daily.plist` — fires at 8:30 AM and 7:00 PM; if the Mac is asleep, launchd catches up on the next wake
- `com.fitpulse.sync-fallback.plist` — fires every 5 minutes when awake, runs with `--if-stale 8` as a fallback after hibernation

The wrapper script is `scripts/sync_daily.sh`. It sources `.env` and after a successful sync schedules the next one-shot wake via `sudo pmset schedule wake` (requires a sudoers entry for passwordless `pmset`). The last sync time appears in the nav Sync dropdown and the Settings page footer.

---

## AI Features

Requires `ANTHROPIC_API_KEY` in `.env`. All AI calls use the Anthropic API directly (no `anthropic` Python package needed).

| Feature | Where | Model | Cache |
|---|---|---|---|
| Training insights | Analytics page | claude-sonnet-4-6 (Batch API) | 7 days |
| Day analysis | Day view | claude-sonnet-4-6 | 7 days |
| Next-workout recommendation | Calendar sidebar | claude-sonnet-4-6 | 24h / manual refresh |
| Body commentary | Body page | claude-haiku-4-5 | 24h / manual refresh |
| Workout comparison | Compare page | claude-haiku-4-5 | On-demand |
| Intervention interpretation | Trends page | claude-sonnet-4-6 | Saved with analysis |
| Food parsing | Nutrition log | claude-haiku-4-5 | On-demand |
| Meal suggestions | Nutrition log | claude-haiku-4-5 | On-demand |
| Nutrition analytics insights | Nutrition analytics | claude-sonnet-4-6 (Batch API) | 7 days |
| Pattern insights | Insights page | claude-sonnet-4-6 (Batch API) | 7 days |
| Weekly review | Weekly Review page | claude-sonnet-4-6 (Batch API) | Per week |

The next-workout recommendation and day analysis need at least one day's wellness data to work from — either Garmin's or Google Health's. If Garmin's own readiness score isn't available, they fall back to the Google Health-derived computed readiness score automatically. Use the **Get Rec** / **Refresh** button in the calendar sidebar to generate or update the next-workout recommendation (useful before and after a workout to see how it changes).

---

## Tech Stack

| Layer | What |
|---|---|
| Backend | Django 4.2 |
| Database | Neon Postgres (cloud) via dj-database-url; SQLite for demo mode |
| Frontend | Vanilla JS + HTMX |
| Charts | Chart.js (CDN) |
| Styles | Single hand-written CSS file, no framework |
| AI | Anthropic API (Claude Sonnet + Haiku) |
| Peloton data | Unofficial internal API via session cookie |
| Garmin data | `garminconnect` library |
| Withings data | Withings OAuth 2.0 API |
| Google Health data | Google Health API (OAuth 2.0 + push webhooks) |

No npm, no build step, no bundler.

---

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Peloton Interactive or Garmin. It uses Peloton's unofficial internal API, which may change or break without notice. Use at your own risk and for personal use only.
