from django.conf import settings
from django.db import migrations
from django.utils import timezone


def create_rows(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    UserAccess = apps.get_model("workouts", "UserAccess")
    for user in User.objects.all():
        defaults = {"onboarding_completed_at": timezone.now()} if user.is_superuser else {}
        UserAccess.objects.get_or_create(user=user, defaults=defaults)


class Migration(migrations.Migration):

    dependencies = [
        ("workouts", "0033_ai_usage_and_user_access"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(create_rows, migrations.RunPython.noop),
    ]
