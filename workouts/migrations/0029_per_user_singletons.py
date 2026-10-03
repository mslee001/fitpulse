from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _user_o2o(related_name):
    return models.OneToOneField(
        null=True, on_delete=django.db.models.deletion.CASCADE,
        related_name=related_name, to=settings.AUTH_USER_MODEL,
    )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("workouts", "0028_user_ownership_required"),
    ]

    operations = [
        # Frees the user_id column name for the Django `user` FK below.
        migrations.RenameField("pelotonauth", "user_id", "peloton_user_id"),
        migrations.AddField("usersettings", "user", _user_o2o("fp_settings")),
        migrations.AddField("nutritionprofile", "user", _user_o2o("nutrition_profile")),
        migrations.AddField("athleteprofile", "user", _user_o2o("athlete_profile")),
        migrations.AddField("withingsauth", "user", _user_o2o("withings_auth")),
        migrations.AddField("pelotonauth", "user", _user_o2o("peloton_auth")),
        migrations.AddField("googlehealthauth", "user", _user_o2o("google_health_auth")),
    ]
