# FitPulse

A fitness dashboard for a small household, built with Django. Syncs workout and wellness data from Peloton, Garmin Connect, Withings, and Google Health to a database and surfaces detailed stats, charts, and AI-powered insights that the default apps don't provide.

Each person has their own account and their own data. The first superuser is the owner: they create accounts for the rest of the household, choose which features each person can use, and set a monthly AI budget per person.

> **Private use only.** This tool uses Peloton's unofficial internal API via session cookie. It is not affiliated with Peloton, Garmin, Withings, or Google and is intended for one household running its own copy.

---

## Features

- **Household accounts** — every workout, weigh-in, food log, intervention and AI summary belongs to one person; nobody sees anyone else's data
- **Users page** (owner only, `/settings/users/`) — create accounts with a one-time temporary password, turn individual features on or off per person, switch AI on or off and set a monthly AI spend cap, and see each person's setup and connection status. It never shows anyone's health data
- **Get Started** — new users change their temporary password, then connect Peloton, Withings and Google Health, fill in a nutrition and coaching profile, and finish setup. Their history imports in the background. Afterwards the page stays available as a setup guide
- **Dashboard** — overview of total workouts and discipline breakdown
- **Workout history** — filterable, sortable list of all cached workouts
- **Discipline-specific detail pages** — power zone charts for cycling, pace/splits/HR/running form for runs, muscle groups and exercise sets for strength
- **Running form** — Garmin foot pod metrics (cadence, stride length, vertical oscillation, vertical ratio, ground contact time) overlaid on run charts; walking filtered from averages
- **Calendar** — monthly grid with per-day workout dots, training readiness scores, and a next-workout AI recommendation
- **Day view** — per-day wellness signals (HRV, sleep, body battery, readiness) alongside workouts and an AI day analysis
- **Analytics** — weekly volume, discipline mix, performance trends, and AI-generated training insights
- **Garmin wellness** (owner only) — daily body battery, HRV, sleep score, resting HR, training load, and training readiness synced from Garmin Connect
- **Google Health** — wellness and exercise sync (resting HR, HRV, sleep, steps, floors, calories, respiratory rate, SpO2, Active Zone Minutes, skin temperature, HR zone minutes) via OAuth, plus real-time push-webhook sync. When Garmin's own readiness score isn't available, a computed readiness score (Low/Moderate/High) is derived from Google Health's HRV, sleep, and resting HR against your personal baseline, so the readiness ring, calendar, and AI features keep working either way
- **Integrations page** — enable/disable each data source independently, connect or reconnect Peloton, Withings and Google Health from the browser, see connection status and last-synced time, and run a full backfill ("Sync All") per source
- **Webhook error log** — failed background syncs are recorded and viewable in the UI (self-pruning after 2 weeks) instead of only living in server logs
- **Compare** — side-by-side comparison of 2–4 workouts with AI narrative analysis
- **Programs** — track structured training plans and splits, with progression charts and AI retrospectives
- **Strength trends** — per-exercise weight trends and "move up a dumbbell" recommendations from your exercise log
- **Body composition** — weight trend chart with rolling averages, body composition stacked chart, and recovery sparklines (HRV, sleep, resting HR, body battery) from Withings scale data
- **Interventions & Trends** — track health interventions (medications, supplements, habits) with dose history; before/after statistical analysis across 20+ wellness metrics with AI interpretation; save and revisit analyses
- **Nutrition** — freeform food logging with AI macro parsing (text or a photo), daily macro targets (Mifflin-St Jeor BMR/TDEE), saved meals for one-click re-logging, and AI meal suggestions that adapt to your current hunger level and any GI symptoms logged
- **Hunger & satiety tracking** — log hunger level (1–10) before/after meals; morning hunger trends surfaced on the nutrition analytics page
- **Symptom log** — track GI and other side effects with severity; symptoms inform meal suggestions and appear as a summary on the body trends page
- **Pattern insights** — Claude Sonnet deep-analysis of 60 days of integrated data (weight, recovery, nutrition, hunger, symptoms, workouts, interventions) to surface non-obvious correlations
- **Weekly review** — AI-generated summary of each completed Mon–Sun week covering weight trend, nutrition adherence, training, and one focus for the next week; archived for all past weeks
- **Stats chat** — ask questions about your own data from a sidebar on any page
- **Settings** — FTP, dumbbells, AI coaching profile, password, and links to the setup guide and data sources

---

## Requirements

- Python 3.11+
- A Peloton account
- A Garmin Connect account (optional, owner only — needed for running form, Garmin wellness data, and Garmin-tracked workouts)
- A Withings developer app (optional — needed for body composition tracking)
- A Google Health API project (optional — wellness and exercise data; also enables the computed readiness score when Garmin isn't connected)
- An Anthropic API key (optional — needed for every AI feature)

---

## Setup

**1. Clone and create a virtual environment**

```bash
git clone <repo-url>
cd peloton_dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Create a `.env` file**

```bash
cp .env.example .env
```

Then fill in your values. Generate a Django secret key with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

`DATABASE_URL` points at your Postgres database; without it the app uses a local `db.sqlite3`. Peloton credentials are not set here — each person connects Peloton in the app.

**3. Create the owner account and run migrations**

```bash
venv/bin/python3 manage.py migrate
venv/bin/python3 manage.py createsuperuser
```

The first superuser is the owner. If you're upgrading a database that already has data from the single-user version, create the superuser **before** migrating (and back the database up first): the migrations assign every existing row to the first superuser and stop with an error if there isn't one.

**4. Start the server and log in**

```bash
venv/bin/python3 manage.py runserver
```

Open [http://localhost:8000](http://localhost:8000) and log in as the owner.

**5. Connect Peloton**

Peloton's login endpoint is no longer publicly accessible, so authentication is done via session cookie:

1. On a computer, log into [onepeloton.com](https://www.onepeloton.com)
2. Open developer tools → **Application** (Safari: **Storage**) → **Cookies** → `https://www.onepeloton.com`
3. Copy the value of `peloton_session_id`
4. Paste it into the **Integrations** page (`/settings/integrations/`) under "Peloton Session Cookie"

FitPulse checks the cookie with Peloton and shows "Connected as @username". The first time you connect, your workout history imports in the background. The cookie is stored in the app's database, not in `.env`.

**6. Connect Garmin (owner only, optional)**

Garmin requires an interactive login to obtain and cache auth tokens:

```bash
venv/bin/python3 manage.py garmin_login
```

Tokens are saved to `~/.garminconnect/` and auto-refresh on subsequent syncs. You should only need to do this once per machine. Garmin is available to the owner account only.

**7. Set up Withings (optional)**

1. Create a Withings developer account at [developer.withings.com](https://developer.withings.com) and register an app
2. In the app settings, add **both** of these to the Callback URLs: `https://<your-host>/auth/withings/callback/` (the sign-in redirect) and `https://<your-host>/api/withings/webhook/` (where weigh-in notifications are sent — Withings refuses to subscribe a URL that isn't listed, with error 293). Withings no longer accepts `localhost` callback URLs.
3. Add `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` and `WITHINGS_CALLBACK_URL` (your `https://<your-host>/api/withings/webhook/` endpoint) to `.env`
4. On the **Integrations** page, click **Connect Withings**, sign in and approve access

FitPulse saves the tokens, subscribes to weigh-in notifications, and imports your history. There's also a CLI flow: `venv/bin/python3 manage.py withings_login [--user USERNAME]` (uses `WITHINGS_REDIRECT_URI`).

**8. Set up welcome emails (optional)**

New household members can get a welcome email with a link to choose their password. FitPulse sends it from a Gmail account:

1. Turn on 2-Step Verification for the Gmail account, then create an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Add `EMAIL_HOST_USER` (the Gmail address) and `EMAIL_HOST_PASSWORD` (the 16-character app password) to `.env`, and to Render's environment for production

Without these, emails print to the server console instead of being sent, and you hand over the temporary password yourself.

**9. Set up Google Health (optional)**

1. Create a Google Cloud project, enable the Google Health API, and create OAuth 2.0 credentials
2. Add `GOOGLE_HEALTH_CLIENT_ID` and `GOOGLE_HEALTH_CLIENT_SECRET` to `.env`
3. While the project's OAuth consent screen is in **Testing** status, add every household member's Google account under **APIs & Services → OAuth consent screen → Test users**
4. On the **Integrations** page, click **Connect** next to Google Health

Refresh tokens expire after 7 days while the project is in Testing status, so **Reconnect** from the Integrations page when it reminds you. The CLI alternative is `venv/bin/python3 manage.py google_health_login [--user USERNAME]` (uses `GOOGLE_HEALTH_REDIRECT_URI`).

---

## Adding a Household Member

1. As the owner, go to **Settings → Users** (`/settings/users/`) → **New user**. Enter their username and email, and pick a starting set of features (for example "Nutrition only"); you can change individual features later.
2. They get a welcome email with a link to choose their password (it works once and expires after 3 days) and an outline of what Get Started will ask for. No password is ever sent by email.
3. FitPulse also shows a temporary password once, as a fallback if the email doesn't arrive. It's never stored, so if you need it, send it to them yourself; they'll choose a new one when they log in.
4. If they'll use Google Health: add their Google account as a test user in the Google Cloud console (see setup step 9), then tick **Google test user added** on their page in Users.
5. They land on **Get Started**, which walks them through connecting their accounts and filling in their profiles. Their history imports in the background.
6. On their page in Users you can change their email and resend the welcome link, turn AI on or off, set a monthly AI budget, change features, reset their password, or deactivate the account.

---

## Demo Mode

A seed command generates a populated demo account with 90 days of realistic fake data — no Peloton/Garmin/Withings account needed.

**What's included:**
- 44 workouts across cycling, running, strength, and yoga with realistic metrics, HR zones, and running form data
- 91 days of wellness data (weight trending down, HRV, sleep, body battery, stress, training readiness)
- 2 interventions: Semaglutide with a 3-step dose escalation history, and Creatine
- 30 days of food log entries with macro breakdowns, 5 saved meals, hunger checks, and symptom logs
- Pre-loaded AI insights, body commentary, nutrition insights, pattern insights, and a weekly review

**Create a separate demo database and seed it:**

```bash
DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py migrate
DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py seed_demo
DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py changepassword demo
```

**Start the server in demo mode** and log in as `demo`:

```bash
DATABASE_URL=sqlite:///demo.sqlite3 venv/bin/python3 manage.py runserver
```

A `DATABASE_URL` given on the command line overrides the one in `.env`, so always pass it for every demo command — otherwise the command runs against your real database. `seed_demo` only ever clears and seeds the `demo` user's data (or the user named with `--user`), never anyone else's. Re-run it to reset the demo.

---

## Syncing Data

All data is stored in the database. Nothing is fetched in real time for page views — sync first, then browse. Every sync runs for the logged-in user and only touches their data.

Each source is independent — no combined "sync everything" button, and syncing them in any order (or combination) is safe. Cross-source duplicates (e.g. the same run recorded by both Peloton and Garmin, or Peloton and Google Health) are automatically reconciled after every Peloton sync, regardless of which order you synced in. Matching is per person, so two people taking the same class at the same time are never merged.

**Nav Sync dropdown** — fast, incremental syncs, safe to run often:

| Option | What it does |
|---|---|
| **Peloton Sync New** | New Peloton workouts |
| **Garmin Sync New** | New Garmin activities (owner) |
| **Garmin Wellness Today** | Today's wellness data — body battery, HRV, sleep, readiness (owner) |
| **Withings Sync New** | New Withings body composition measurements |
| **Google Health Sync New** | New Google Health wellness (trailing 7 days) + exercise data |

**Integrations page** (`/settings/integrations/`) — full historical backfills, plus enable/disable per source:

| Option | What it does |
|---|---|
| **Peloton Sync All** | Full backfill of all Peloton workouts |
| **Garmin Sync All** | Full backfill of all Garmin activities (owner) |
| **Withings Sync All** | Full Withings history backfill |
| **Google Health Sync All** | Full Google Health history backfill (wellness + exercise, ~3 years back) |

Withings and Google Health also push updates automatically via webhooks. Withings notifications are routed to the right person by their Withings account; Google Health notifications currently re-sync every connected person for the notified dates.

Session cookies expire periodically. When Peloton syncing stops working, grab a fresh `peloton_session_id` from your browser and update it on the **Integrations** page. Garmin and Withings tokens auto-refresh. Google Health refresh tokens expire every 7 days while the Google Cloud project is in "Testing" status — reconnect from the Integrations page when that happens.

---

## FTP (Cycling Power Zones)

Your current FTP is set in the Settings page (`/settings/`). Each workout is stamped with your FTP at the time of sync, keeping historical power zone charts accurate as your FTP changes.

To retroactively correct past workouts after an FTP update, edit `FTP_HISTORY` in `workouts/management/commands/backfill_ftp.py` and run:

```bash
venv/bin/python3 manage.py backfill_ftp            # apply (owner's workouts; --user USERNAME for someone else)
venv/bin/python3 manage.py backfill_ftp --dry-run  # preview without writing
```

---

## Automated Daily Sync

`sync_daily` syncs every active user in turn. For each person it runs Peloton, Garmin activities and wellness (owner only), and Google Health — each only if that source is enabled and connected. People with nothing connected are skipped. One person's failure (an expired cookie, say) doesn't stop the others; output lines are prefixed with the username. Withings isn't included because its webhook pushes new weigh-ins.

```bash
venv/bin/python3 manage.py sync_daily
```

Flags:
- `--user USERNAME` — sync just one person
- `--skip-peloton` — skip Peloton sync
- `--wellness-days N` — days of Garmin wellness to sync (default: 2, catches today + yesterday)
- `--if-stale HOURS` — skip anyone whose last daily sync was less than HOURS hours ago

On macOS, two launchd plists automate this (stored in `~/Library/LaunchAgents/`, not the repo):
- `com.fitpulse.sync-daily.plist` — fires at 8:30 AM and 7:00 PM; if the Mac is asleep, launchd catches up on the next wake
- `com.fitpulse.sync-fallback.plist` — fires every 5 minutes when awake, runs with `--if-stale 8` as a fallback after hibernation

The wrapper script is `scripts/sync_daily.sh`. Django loads `.env` itself, so the script doesn't source it. After a successful sync it schedules the next one-shot wake via `sudo pmset schedule wake` (requires a sudoers entry for passwordless `pmset`). Each person's last sync time appears in the nav Sync dropdown and the Settings page footer.

---

## AI Features

Requires `ANTHROPIC_API_KEY` in `.env`. All AI calls use the Anthropic API directly (no `anthropic` Python package needed).

| Feature | Where | Model | Cache |
|---|---|---|---|
| Training insights | Analytics page | claude-sonnet-5 (Batch API) | 7 days |
| Day analysis | Day view | claude-sonnet-5 | 7 days |
| Next-workout recommendation | Calendar sidebar | claude-sonnet-5 | 24h / manual refresh |
| Body commentary | Body page | claude-haiku-4-5 | 24h / manual refresh |
| Workout comparison | Compare page | claude-haiku-4-5 | On-demand |
| Intervention interpretation | Trends page | claude-sonnet-5 | Saved with analysis |
| Food parsing | Nutrition log | claude-haiku-4-5 (text), claude-sonnet-5 (photo) | On-demand |
| Meal suggestions | Nutrition log | claude-haiku-4-5 | On-demand |
| Nutrition analytics insights | Nutrition analytics | claude-sonnet-5 (Batch API) | 7 days |
| Pattern insights | Insights page | claude-sonnet-5 (Batch API) | 7 days |
| Weekly review | Weekly Review page | claude-sonnet-5 (Batch API) | Per week |
| Plan import, program retrospectives | Programs | claude-haiku-4-5, claude-sonnet-5 | Saved with program |
| Stats chat | Sidebar | claude-sonnet-5 | Per conversation |

Each AI feature can be turned on per person on the Users page, along with a master AI switch and an optional monthly budget in US dollars. Every call's token usage and cost is logged per person and per feature; the Users page shows this month's totals (counts and cost only, never what was asked or answered). When someone reaches their budget, AI cards show "You've reached this month's AI limit. It resets on the 1st." The owner is never capped.

The next-workout recommendation and day analysis need at least one day's wellness data to work from — either Garmin's or Google Health's. If Garmin's own readiness score isn't available, they fall back to the Google Health-derived computed readiness score automatically. Use the **Get Rec** / **Refresh** button in the calendar sidebar to generate or update the next-workout recommendation (useful before and after a workout to see how it changes).

---

## Tech Stack

| Layer | What |
|---|---|
| Backend | Django 4.2 |
| Database | Neon Postgres (cloud) via dj-database-url; SQLite for local and demo use |
| Frontend | Vanilla JS + HTMX |
| Charts | Chart.js (CDN) |
| Styles | Single hand-written CSS file, no framework |
| AI | Anthropic API (Claude Sonnet + Haiku) |
| Peloton data | Unofficial internal API via session cookie |
| Garmin data | `garminconnect` library |
| Withings data | Withings OAuth 2.0 API + push webhooks |
| Google Health data | Google Health API (OAuth 2.0 + push webhooks) |

No npm, no build step, no bundler.

---

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Peloton Interactive, Garmin, Withings, or Google. It uses Peloton's unofficial internal API, which may change or break without notice. Use at your own risk and for private use only.
