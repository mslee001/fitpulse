# FitPulse — Console Command Reference

All Python commands use the venv. Prefix every `manage.py` or `-c` invocation with:
```
DJANGO_SETTINGS_MODULE=peloton_dashboard.settings venv/bin/python3
```
Shortened below as **`py`** for readability.

---

## Start the Dev Server

```bash
venv/bin/python3 manage.py runserver
```

---

## Syncing Data

### Wellness only (last N days)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_wellness_sync
from datetime import date, timedelta
dates = [date.today() - timedelta(days=i) for i in range(30)]
print(_run_wellness_sync(dates))
"
```
Change `range(30)` to any number up to 90.

### Garmin activities only (new)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_garmin_sync_new
print(_run_garmin_sync_new())
"
```

### Garmin activities (all)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_garmin_sync_all
print(_run_garmin_sync_all())
"
```

### Peloton (new workouts only)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_peloton_sync_new
print(_run_peloton_sync_new())
"
```

### Withings (new measurements only)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_withings_sync_new
print(_run_withings_sync_new())
"
```

### Everything new (Peloton + Garmin + today's wellness)
```bash
py -c "
import django; django.setup()
from workouts.sync import _run_peloton_sync_new, _run_garmin_sync_new, _run_wellness_sync, _run_withings_sync_new
from datetime import date
print(_run_peloton_sync_new())
print(_run_garmin_sync_new())
print(_run_wellness_sync([date.today()]))
print(_run_withings_sync_new())
"
```

---

## Clear AI Caches

### Day analysis (today)
```bash
py -c "
import django; django.setup()
from workouts.models import DailyStats
from datetime import date
DailyStats.objects.filter(date=date.today()).update(ai_day_analysis=None, ai_day_generated_at=None)
print('Done')
"
```

### Day analysis (specific date)
```bash
py -c "
import django; django.setup()
from workouts.models import DailyStats
DailyStats.objects.filter(date='2026-06-02').update(ai_day_analysis=None, ai_day_generated_at=None)
print('Done')
"
```

### Next workout recommendation
```bash
py -c "
import django; django.setup()
from workouts.models import DailyStats
from datetime import date
DailyStats.objects.filter(date=date.today()).update(ai_next_workout=None, ai_next_workout_generated_at=None)
print('Done')
"
```

### Analytics insights (training page)
```bash
py -c "
import django; django.setup()
from workouts.models import UserSettings
UserSettings.objects.filter(pk=1).update(ai_insights=None, ai_insights_batch_id=None)
print('Done')
"
```

### Body commentary
```bash
py -c "
import django; django.setup()
from workouts.models import UserSettings
UserSettings.objects.filter(pk=1).update(ai_body_commentary=None, ai_body_commentary_generated_at=None)
print('Done')
"
```

### Nutrition analytics insights
```bash
py -c "
import django; django.setup()
from workouts.models import UserSettings
UserSettings.objects.filter(pk=1).update(ai_nutrition_insights=None, ai_nutrition_insights_generated_at=None, ai_nutrition_insights_range=None)
print('Done')
"
```

### Pattern insights (/insights/)
```bash
py -c "
import django; django.setup()
from workouts.models import UserSettings
UserSettings.objects.filter(pk=1).update(ai_pattern_insights=None, ai_pattern_insights_generated_at=None)
print('Done')
"
```

### Weekly review (specific week)
```bash
py -c "
import django; django.setup()
from workouts.models import WeeklyReview
WeeklyReview.objects.filter(week_start='2026-05-25').delete()
print('Done')
"
```

### Clear all AI caches at once
```bash
py -c "
import django; django.setup()
from workouts.models import UserSettings, DailyStats
from datetime import date
UserSettings.objects.filter(pk=1).update(
    ai_insights=None, ai_insights_batch_id=None,
    ai_body_commentary=None, ai_body_commentary_generated_at=None,
    ai_nutrition_insights=None, ai_nutrition_insights_generated_at=None, ai_nutrition_insights_range=None,
    ai_pattern_insights=None, ai_pattern_insights_generated_at=None,
)
DailyStats.objects.filter(date=date.today()).update(ai_day_analysis=None, ai_day_generated_at=None, ai_next_workout=None, ai_next_workout_generated_at=None)
print('Done')
"
```

---

## Useful Database Queries

### Today's wellness snapshot
```bash
py -c "
import django; django.setup()
from workouts.models import DailyStats
from datetime import date
s = DailyStats.objects.get(date=date.today())
print(f'Readiness: {s.training_readiness_score}')
print(f'HRV: {s.hrv_last_night} ms ({s.hrv_status})')
print(f'Sleep: {s.sleep_score}')
print(f'Body battery: start={s.body_battery_start} end={s.body_battery_end}')
print(f'Resting HR: {s.resting_hr}')
print(f'Training status: {s.training_status}')
print(f'Synced at: {s.synced_at}')
"
```

### Total miles run
```bash
py -c "
import django; django.setup()
from workouts.models import CachedWorkout
total = sum(w.distance_miles or 0 for w in CachedWorkout.objects.filter(discipline='running'))
print(f'Total miles run: {total:.1f}')
"
```

### Total miles walked
```bash
py -c "
import django; django.setup()
from workouts.models import CachedWorkout
total = sum(w.distance_miles or 0 for w in CachedWorkout.objects.filter(discipline='walking'))
print(f'Total miles walked: {total:.1f}')
"
```

### Recompute nutrition totals (e.g. after backfill)
```bash
py -c "
import django; django.setup()
from workouts.nutrition import recompute_daily_nutrition
from datetime import date, timedelta
for i in range(30):
    recompute_daily_nutrition(date.today() - timedelta(days=i))
print('Done')
"
```

### Check target fit (calories on track?)
```bash
py -c "
import django; django.setup()
from workouts.nutrition import evaluate_target_fit
import json
print(json.dumps(evaluate_target_fit(), indent=2, default=str))
"
```

### Intervention analysis (before/after)
```bash
venv/bin/python3 manage.py analyze_intervention \
  --start 2026-01-15 \
  --label "My Supplement 5mg" \
  --window 28 \
  --weight-goal loss
```

---

## Migrations

```bash
venv/bin/python3 manage.py makemigrations
venv/bin/python3 manage.py migrate
```

---

## First-Time Auth Setup

```bash
# Garmin (one-time interactive login)
venv/bin/python3 manage.py garmin_login

# Withings (one-time OAuth flow)
venv/bin/python3 manage.py withings_login
```

---

## Django Shell

```bash
venv/bin/python3 manage.py shell
```
