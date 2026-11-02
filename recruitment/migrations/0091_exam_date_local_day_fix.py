from datetime import timezone as dt_timezone

from django.db import migrations
from django.utils import timezone


def correct_exam_dates(apps, schema_editor):
    """Batch exams used to record the UTC calendar day of the sitting, so a sitting before
    08:00 in Manila was dated the day before. Correct only records that carry exactly that
    wrong date for their own stage's sitting; anything else was entered on purpose."""
    ExamRecord = apps.get_model("recruitment", "ExamRecord")
    ExamSchedule = apps.get_model("recruitment", "ExamSchedule")

    for schedule in ExamSchedule.objects.exclude(scheduled_for__isnull=True).iterator():
        utc_day = schedule.scheduled_for.astimezone(dt_timezone.utc).date()
        local_day = timezone.localdate(schedule.scheduled_for)
        if utc_day == local_day:
            continue
        ExamRecord.objects.filter(
            application_id=schedule.application_id,
            review_stage=schedule.review_stage,
            exam_date=utc_day,
        ).update(exam_date=local_day)


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0090_one_open_posting_per_plantilla_item"),
    ]

    operations = [
        migrations.RunPython(correct_exam_dates, migrations.RunPython.noop),
    ]
