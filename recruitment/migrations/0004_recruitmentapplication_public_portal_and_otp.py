import uuid

from django.db import migrations, models


def populate_public_tokens(apps, schema_editor):
    RecruitmentApplication = apps.get_model("recruitment", "RecruitmentApplication")
    for application in RecruitmentApplication.objects.filter(public_token__isnull=True).iterator():
        application.public_token = uuid.uuid4()
        application.save(update_fields=["public_token"])


class Migration(migrations.Migration):
    dependencies = [
        (
            "recruitment",
            "0003_position_alter_positionposting_options_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="recruitmentapplication",
            name="applicant_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="applicant_first_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="applicant_last_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="applicant_phone",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="checklist_documents_complete",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="checklist_information_certified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="checklist_privacy_consent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="otp_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="otp_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="otp_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="otp_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recruitmentapplication",
            name="public_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="recruitmentapplication",
            name="reference_number",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=30,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(populate_public_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="recruitmentapplication",
            name="public_token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
