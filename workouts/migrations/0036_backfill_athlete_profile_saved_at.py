from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    # Existing profiles (the owner's) were filled in before saved_at existed.
    apps.get_model("workouts", "AthleteProfile").objects.filter(saved_at__isnull=True).update(saved_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [("workouts", "0035_onboarding_fields")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
