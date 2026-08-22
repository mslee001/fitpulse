from django.db import migrations


def backfill_wellness_source(apps, schema_editor):
    """
    wellness_source didn't exist before this feature — every pre-existing
    Garmin-synced DailyStats row has it NULL. Without this backfill, the
    Google Health sync's "don't clobber Garmin-exclusive fields" guard
    (which checks wellness_source == 'garmin') silently fails to protect
    real historical data, since NULL != 'garmin'. synced_at is Garmin's own
    pre-existing sync stamp — its presence is what actually indicates a real
    Garmin wellness sync happened for that day.
    """
    DailyStats = apps.get_model("workouts", "DailyStats")
    DailyStats.objects.filter(
        wellness_source__isnull=True, synced_at__isnull=False
    ).update(wellness_source="garmin")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('workouts', '0012_seed_integrations'),
    ]

    operations = [
        migrations.RunPython(backfill_wellness_source, noop_reverse),
    ]
