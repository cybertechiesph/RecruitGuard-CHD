# RD-9 backfill: sessions whose application already received a SENT applicant
# interview notice get stamped from that notice's sent_at — without this,
# currently-visible schedules would vanish from the applicant status page the
# moment the notified-only filter lands.
from django.db import migrations


def backfill(apps, schema_editor):
    InterviewSession = apps.get_model("recruitment", "InterviewSession")
    NotificationLog = apps.get_model("recruitment", "NotificationLog")
    for session in InterviewSession.objects.filter(applicant_notified_at__isnull=True):
        notice = (
            NotificationLog.objects.filter(
                application_id=session.application_id,
                notification_type="applicant_interview_notice",
                delivery_status="sent",
            )
            .order_by("-sent_at", "-created_at")
            .first()
        )
        if notice:
            session.applicant_notified_at = notice.sent_at or notice.created_at
            session.save(update_fields=["applicant_notified_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("recruitment", "0064_interviewsession_applicant_notified_at_and_more"),
    ]
    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
