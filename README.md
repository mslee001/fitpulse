# FitPulse

A fitness dashboard for a small household, built with Django. Syncs workout and wellness data from Peloton, Garmin Connect, Withings, and Google Health to a database and surfaces detailed stats, charts, and AI-powered insights that the default apps don't provide.

Each person has their own account and their own data. The first superuser is the owner: they create accounts for the rest of the household, choose which features each person can use, and set a monthly AI budget per person.

> **Private use only.** This tool uses Peloton's unofficial internal API with your own Peloton sign-in. It is not affiliated with Peloton, Garmin, Withings, or Google and is intended for one household running its own copy.

---

## Features

- **Household accounts** — every workout, weigh-in, food log, intervention and AI summary belongs to one person; nobody sees anyone else's data
- **Users page** (owner only, `/settings/users/`) — create accounts with a one-time temporary password, turn individual features on or off per person, switch AI on or off and set a monthly AI spend cap, and see each person's setup and connection status. It never shows anyone's health data
- **Get Started** — new users change their temporary password, then connect Peloton, Withings and Google Health, fill in a nutrition and coaching profile, and finish setup. Their history imports in the background. Afterwards the page stays available as a setup guide
- **Dashboard** — overview of total workouts and discipline breakdown
- **Workout history** — filterable, sortable list of all cached workouts, each with the class's difficulty in context ("harder than 68% of 30-min Endurance classes") and your effort per minute
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
- **AI training plans** — enter a goal (5K, 10K, half, marathon or a running base) and get a week-by-week plan of real Peloton classes built from your own running history and pace, standalone or alongside another program; review and swap classes before saving, follow it on a run page with days, dates and pace advice, and reassess it mid-way from how your runs have actually gone (see [AI Training Plans](#ai-training-plans))
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

Peloton's web app signs in through Auth0. FitPulse uses a refresh token copied from the web app once, then renews it by itself:

1. On a computer, open a **private/incognito window** and sign in at [members.onepeloton.com](https://members.onepeloton.com)
2. Open developer tools → **Console** and paste the one-line snippet shown on the **Integrations** page (`/settings/integrations/`) or Get Started. It copies your sign-in token to the clipboard.
3. Paste it under "Peloton Connection" and click **Connect**, then close the private window (a private window keeps FitPulse's sign-in separate from your everyday browser's, so the two don't log each other out)

FitPulse trades the pasted token for its own (the pasted one stops working right away), checks it with Peloton and shows "Connected as @username". Access tokens last 48 hours and renew automatically. The first time you connect, your workout history imports in the background. Tokens are stored in the app's database, not in `.env`.

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
| **Refresh catalog** (owner, Peloton card) | Full sync of the shared Peloton class catalog that training plans pick from (~42k classes, a few minutes) |

Withings and Google Health also push updates automatically via webhooks. Withings notifications are routed to the right person by their Withings account; Google Health notifications currently re-sync every connected person for the notified dates.

Peloton sign-ins renew automatically. If Peloton ends one, FitPulse shows a "Peloton needs reconnecting" banner — repeat the connect steps from the **Integrations** page. Garmin and Withings tokens auto-refresh. Google Health refresh tokens expire every 7 days while the Google Cloud project is in "Testing" status — reconnect from the Integrations page when that happens.

---

## AI Training Plans

Programs → **+ New Training Plan** (needs the *Plan import, training plans & retrospectives* AI feature).

### The class catalog

Plans are built from FitPulse's copy of Peloton's on-demand library — running, walking, strength, stretching, pilates, cycling and yoga, about 42,000 classes. It's shared by everyone in the household and synced with the owner's Peloton connection:

- once by the owner: **Settings → Data Sources → Peloton Connection → Refresh catalog** (or `venv/bin/python3 manage.py sync_peloton_catalog --full`)
- automatically after that: the daily sync checks for new classes every run and does a full refresh once a week

### Building a plan

1. **Goal** — race distance and date (or a 4–16 week running base), optional target time, start date.
2. **Pace** — FitPulse reads your Peloton pace level from your latest Tread run (you can override it) and shows your zone paces from Peloton's pace chart. With a target time it shows the goal pace, the zone it falls in at your level, and the level whose race-day zone matches it.
3. **Starting level** — suggested from your history (beginner, returning, intermediate, advanced) with the evidence, and a warning if the time to race day is short for that level.
4. **Schedule** — training days, a long-run day, max session lengths, tread or outdoor.
5. **How it fits** — *standalone* (your main training, with optional strength, pilates and yoga) or *alongside* a program you're already doing (runs and short stretches scheduled around its strength days).

Claude Sonnet writes the week-by-week structure from your actual running history; FitPulse then picks a real class for every session (newest classes first, a rating floor, nothing you've taken in the last 60 days, difficulty matched to each session within its class type). A few rules it follows:

- weekly running minutes rise gradually, with lighter weeks in longer plans and a taper before race day
- easy and long runs stay in your Easy–Moderate zones; quality sessions practise goal pace and faster
- for a 5K or 10K, the long run builds to longer than the race itself (at least 45 min, about 1.25× race time, at easy pace)
- session lengths are ones Peloton actually makes (15, 20, 30, 45, 60 …)

The **review page** shows every session with the picked class (difficulty, rating, air date, "taken before" tag, a link that opens the class on Peloton so you can add it to your Stack), any adjustments FitPulse made, and a pace card with advice on when to try the next pace level. **Swap** or **Choose…** any class, then **Create plan**. It becomes a normal program, so your classes are matched automatically as you take them.

### Following it

The plan's run page shows a PLAN card (summary, pace numbers and pace advice), each week's dates and focus, and every session in day order with its day and date ("Wed · Oct 7"). Upcoming sessions have a **Swap** button if you want a different class. Rate each week's effort (1–10) on the run page — it's the clearest signal for reassessing.

### Reassessing mid-way

**Reassess weeks N–end** (in the PLAN card) rewrites the rest of the plan from how it's actually going: sessions done vs planned, your pace and zone on each run, effort per minute and heart rate, missed runs, your weekly ratings, and any change in your Peloton pace level. If it's been too easy, the plan progresses a bit faster (within safe limits); if it's been hard or you've missed runs, it holds steady or eases off — missed sessions are never crammed into later weeks. You review the revised weeks, with a "What changed" list that cites the evidence, and nothing changes until you **Apply**. Completed sessions, earlier weeks and the race day stay as they are.

FitPulse also suggests a reassessment (a banner on every page, no AI cost) when your Peloton pace level changed since the plan was made, you rated two weeks in a row 3/10 or lower (or 8/10 or higher), or you missed two or more planned runs in the last two weeks.

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

`sync_daily` syncs every active user in turn. For each person it runs Peloton, Garmin activities and wellness (owner only), and Google Health — each only if that source is enabled and connected. For the owner it also syncs the shared Peloton class catalog: incremental on most runs, a full sync once a week. People with nothing connected are skipped. One person's failure (an ended Peloton sign-in, say) doesn't stop the others; output lines are prefixed with the username. Withings isn't included because its webhook pushes new weigh-ins.

```bash
venv/bin/python3 manage.py sync_daily
```

Flags:
- `--user USERNAME` — sync just one person
- `--skip-peloton` — skip Peloton sync
- `--skip-garmin` — skip Garmin (the scheduled job always passes this)
- `--catalog-full` — run the full class catalog sync now instead of waiting for the weekly one
- `--wellness-days N` — days of Garmin wellness to sync (default: 2, catches today + yesterday)
- `--if-stale HOURS` — skip anyone whose last daily sync was less than HOURS hours ago

### Scheduled on Render

A Render **Cron Job** runs it twice a day, in its own container, separate from the web service:

| Setting | Value |
|---|---|
| Repository / branch | same as the web service (`main`) |
| Runtime | Python (picks up `runtime.txt`) |
| Build command | `pip install -r requirements.txt` |
| Command | `python manage.py sync_daily --skip-garmin` |
| Schedule | `30 2,15 * * *` — UTC, so 8:30 AM and 7:30 PM Pacific during daylight time (7:30 AM / 6:30 PM in winter) |
| Instance type | the smallest (0.5 CPU, 512 MB) |
| Environment | the same variables as the web service — at least `DATABASE_URL`, `DJANGO_SECRET_KEY`, `WITHINGS_CLIENT_ID`/`SECRET`, `GOOGLE_HEALTH_CLIENT_ID`/`SECRET` (token refreshes happen here too). An Environment Group shared by both services keeps them in sync. |

A run that exits non-zero (any user's source failed) shows as failed in Render and triggers its failure notification. Each person's last successful sync time appears in the nav Sync dropdown and the Settings page footer.

Garmin isn't scheduled: its tokens live in `~/.garminconnect` on the machine where `garmin_login` ran, which the cron container doesn't have. Garmin syncs run only from the owner's Sync buttons on a local server.

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
| Training plan generation and reassessment | Programs → New Training Plan, plan run page | claude-sonnet-5 | Saved with the plan (about $0.03–0.12 per plan) |
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
| Peloton data | Unofficial internal API via the web app's Auth0 sign-in |
| Garmin data | `garminconnect` library |
| Withings data | Withings OAuth 2.0 API + push webhooks |
| Google Health data | Google Health API (OAuth 2.0 + push webhooks) |

No npm, no build step, no bundler.

---

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Peloton Interactive, Garmin, Withings, or Google. It uses Peloton's unofficial internal API, which may change or break without notice. Use at your own risk and for private use only.
