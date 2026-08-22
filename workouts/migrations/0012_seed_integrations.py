from django.db import migrations

SEED_ROWS = [
    {"key": "peloton", "display_name": "Peloton", "is_enabled": True, "is_authenticated": True},
    {"key": "garmin", "display_name": "Garmin", "is_enabled": True, "is_authenticated": True},
    {"key": "withings", "display_name": "Withings", "is_enabled": True, "is_authenticated": True},
    {"key": "google_health", "display_name": "Google Health", "is_enabled": False, "is_authenticated": False},
]


def seed_integrations(apps, schema_editor):
    Integration = apps.get_model("workouts", "Integration")
    for row in SEED_ROWS:
        Integration.objects.update_or_create(key=row["key"], defaults=row)


def unseed_integrations(apps, schema_editor):
    Integration = apps.get_model("workouts", "Integration")
    Integration.objects.filter(key__in=[row["key"] for row in SEED_ROWS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('workouts', '0011_googlehealthauth_integration_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_integrations, unseed_integrations),
    ]
