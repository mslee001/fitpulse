from django.conf import settings
from django.db import migrations

SINGLETONS = ["UserSettings", "NutritionProfile", "AthleteProfile",
              "WithingsAuth", "PelotonAuth", "GoogleHealthAuth"]


def assign_to_owner(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    if owner is None:
        if any(apps.get_model("workouts", n).objects.exists() for n in SINGLETONS):
            raise RuntimeError(
                "Existing data but no superuser to own it. "
                "Run `manage.py createsuperuser`, then migrate again."
            )
        return  # fresh/test database — nothing to assign
    for name in SINGLETONS:
        Model = apps.get_model("workouts", name)
        Model.objects.filter(pk=1).update(user=owner)
        extra = Model.objects.exclude(pk=1)
        if extra.exists():
            print(f"\n  {name}: deleting {extra.count()} non-singleton row(s)")
            extra.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workouts", "0029_per_user_singletons"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_to_owner, migrations.RunPython.noop),
    ]
