from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0038_exam_schedule_applicant_notifications"),
    ]

    operations = [
        migrations.RenameField(
            model_name="examrecord",
            old_name="practical_score",
            new_name="general_score",
        ),
        migrations.RenameField(
            model_name="examrecord",
            old_name="practical_result",
            new_name="general_result",
        ),
    ]
