from django.conf import settings
from django.db import migrations

MODEL_NAMES = [
    "CachedWorkout", "DailyStats", "BodyMeasurement", "Intervention", "SavedAnalysis",
    "FoodEntry", "SavedMeal", "HungerCheck", "SideEffectLog", "TargetAdjustment",
    "WeeklyReview", "Program", "Integration", "WebhookError",
]
# Rows that can exist without any real user data: 0012 seeds Integration rows
# on every database (including fresh test ones), and WebhookError stays
# nullable. Neither should force a superuser to exist.
NO_OWNER_OK = {"Integration", "WebhookError"}


def assign_to_owner(apps, schema_editor):
    # Historical models only — get_owner() would import the live User model.
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    if owner is None:
        has_rows = any(
            apps.get_model("workouts", n).objects.exists()
            for n in MODEL_NAMES if n not in NO_OWNER_OK
        )
        if has_rows:
            raise RuntimeError(
                "Existing data but no superuser to own it. "
                "Run `manage.py createsuperuser`, then migrate again."
            )
        # Fresh/test database: the seeded Integration rows have no one to
        # belong to. Integration.ensure_for_user() recreates them per user.
        apps.get_model("workouts", "Integration").objects.filter(user__isnull=True).delete()
        return
    for name in MODEL_NAMES:
        apps.get_model("workouts", name).objects.filter(user__isnull=True).update(user=owner)


class Migration(migrations.Migration):

    dependencies = [
        ("workouts", "0026_user_ownership_fks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_to_owner, migrations.RunPython.noop),
    ]
