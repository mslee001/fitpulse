from django.core.management.color import no_style
from django.db import migrations


def reset_sequences(apps, schema_editor):
    """Bring every workouts table's id counter up to its highest id.

    The former singletons were written with an explicit pk=1, which on Postgres
    doesn't advance the table's id sequence — so the first row a second user
    created could be handed id 1 again and fail with a duplicate key (seen in
    production on GoogleHealthAuth). setval to max(id) is idempotent and a
    no-op where the sequence is already ahead. SQLite needs nothing."""
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    models = list(apps.get_app_config("workouts").get_models())
    for sql in connection.ops.sequence_reset_sql(no_style(), models):
        schema_editor.execute(sql)


class Migration(migrations.Migration):

    dependencies = [("workouts", "0036_backfill_athlete_profile_saved_at")]

    operations = [migrations.RunPython(reset_sequences, migrations.RunPython.noop)]
