# FitPulse

A personal fitness dashboard built with Django. Syncs workout data from Peloton and Garmin Connect to a local SQLite cache and surfaces detailed stats, charts, and AI-powered insights that the default apps don't provide.

> **Personal use only.** This tool uses Peloton's unofficial internal API via session cookie. It is not affiliated with Peloton or Garmin and is intended for a single user running it locally.

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
- **Compare** — side-by-side comparison of 2–4 workouts with AI narrative analysis
- **Body composition** — weight trend chart with rolling averages, body composition stacked chart, and recovery sparklines (HRV, sleep, resting HR, body battery) from Withings scale data
- **Interventions & Trends** — track health interventions (medications, supplements, habits) with dose history; before/after statistical analysis across 20+ wellness metrics with AI interpretation; save and revisit analyses
- **Nutrition** — freeform food logging with AI macro parsing, daily macro targets (Mifflin-St Jeor BMR/TDEE), saved meals for one-click re-logging, and AI meal suggestions that adapt to your current hunger level and any GI symptoms logged
- **Hunger & satiety tracking** — log hunger level (1–10) before/after meals; morning hunger trends surfaced on the nutrition analytics page
- **Symptom log** — track GI and other side effects with severity; symptoms inform meal suggestions and appear as a summary on the body trends page
- **Pattern insights** — Claude Sonnet deep-analysis of 60 days of integrated data (weight, recovery, nutrition, hunger, symptoms, workouts, interventions) to surface non-obvious correlations
- **Weekly review** — AI-generated summary of each completed Mon–Sun week covering weight trend, nutrition adherence, training, and one focus for the next week; archived for all past weeks
- **Settings** — manage your current FTP; historical FTP tracked per workout for accurate power zone charts

---

## Requirements

- Python 3.11+
- A Peloton account
- A Garmin Connect account (optional — needed for running form, wellness data, and Garmin-tracked workouts)
- A Withings account (optional — needed for body composition tracking)
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

**3. Create a `.env` file**

```bash
cp .env.example .env
```

Then fill in your values. Generate a Django secret key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

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

Tokens are saved to `~/.fitpulse/withings_tokens.json` and auto-refresh.

**7. Start the server**

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
4. Run `venv/bin/python3 manage.py withings_login` if using Withings (optional)
5. Start the server and use **Sync All** from the nav to pull your full history

Note: `db.sqlite3` is not in the repo — each machine starts with an empty database and needs to sync data fresh.

---

## Syncing Data

All data is stored in a local SQLite cache. Nothing is fetched in real time for page views — sync first, then browse.

Use the **Sync** dropdown in the nav:

| Option | What it does |
|---|---|
| **Sync New** | New Peloton workouts + new Garmin activities + today's Garmin wellness data |
| **Sync All** | Full backfill of all Peloton + Garmin workouts + last 30 days of wellness |
| **Garmin Wellness Today** | Today's wellness data only (body battery, HRV, sleep, readiness) |
| **Withings Sync New** | New Withings body composition measurements |
| **Withings Sync All** | Full Withings history backfill |

**Recommended first-time setup:**
1. Run **Sync All** to pull your full history from both Peloton and Garmin
2. Browse — charts, running form, and wellness data will all be populated

Session cookies expire periodically. When Peloton syncing stops working, grab a fresh `peloton_session_id` from your browser and update `.env`. Garmin tokens auto-refresh.

---

## FTP (Cycling Power Zones)

Your current FTP is set in the Settings page (`/settings/`). Each workout is stamped with your FTP at the time of sync, keeping historical power zone charts accurate as your FTP changes.

To retroactively correct past workouts after an FTP update, edit `FTP_HISTORY` in `workouts/management/commands/backfill_ftp.py` and run:

```bash
venv/bin/python3 manage.py backfill_ftp            # apply
venv/bin/python3 manage.py backfill_ftp --dry-run  # preview without writing
```

---

## AI Features

Requires `ANTHROPIC_API_KEY` in `.env`. All AI calls use the Anthropic API directly (no `anthropic` Python package needed).

| Feature | Where | Model | Cache |
|---|---|---|---|
| Training insights | Analytics page | claude-sonnet-4-6 (Batch API) | 7 days |
| Day analysis | Day view | claude-haiku-4-5 | 7 days |
| Next-workout recommendation | Calendar sidebar | claude-haiku-4-5 | 24h / manual refresh |
| Body commentary | Body page | claude-haiku-4-5 | 24h / manual refresh |
| Workout comparison | Compare page | claude-haiku-4-5 | On-demand |
| Intervention interpretation | Trends page | claude-sonnet-4-6 | Saved with analysis |
| Food parsing | Nutrition log | claude-haiku-4-5 | On-demand |
| Meal suggestions | Nutrition log | claude-haiku-4-5 | On-demand |
| Nutrition analytics insights | Nutrition analytics | claude-sonnet-4-6 (Batch API) | 7 days |
| Pattern insights | Insights page | claude-sonnet-4-6 (Batch API) | 7 days |
| Weekly review | Weekly Review page | claude-sonnet-4-6 (Batch API) | Per week |

The next-workout recommendation only generates after Garmin wellness has synced for the day. Use the **Get Rec** / **Refresh** button in the calendar sidebar to generate or update it (useful before and after a workout to see how the recommendation changes).

---

## Tech Stack

| Layer | What |
|---|---|
| Backend | Django 4.2 |
| Database | SQLite (local cache) |
| Frontend | Vanilla JS + HTMX |
| Charts | Chart.js (CDN) |
| Styles | Single hand-written CSS file, no framework |
| AI | Anthropic API (Claude Sonnet + Haiku) |
| Peloton data | Unofficial internal API via session cookie |
| Garmin data | `garminconnect` library |
| Withings data | Withings OAuth 2.0 API |

No npm, no build step, no bundler.

---

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Peloton Interactive or Garmin. It uses Peloton's unofficial internal API, which may change or break without notice. Use at your own risk and for personal use only.
