from django.db import migrations, models


def merge_pooling_into_continuous(apps, schema_editor):
    """Chief review C1: the office's "pooling" is a standing pool hired from round by round,
    which the system calls continuous ("Open pool"). The old pooling mode (one batch, no
    closing date) stranded every later batch, so its vacancies become open pools. Cases
    already waiting at the deliberation become the next round's pool."""
    PositionPosting = apps.get_model("recruitment", "PositionPosting")
    PositionPosting.objects.filter(intake_mode="pooling").update(intake_mode="continuous")


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0091_exam_date_local_day_fix"),
    ]

    operations = [
        migrations.RunPython(merge_pooling_into_continuous, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="positionposting",
            name="intake_mode",
            field=models.CharField(
                choices=[
                    ("fixed_period", "Fixed Period"),
                    ("opening_based", "One Batch"),
                    ("continuous", "Open Pool"),
                ],
                default="fixed_period",
                max_length=30,
            ),
        ),
    ]
