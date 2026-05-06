from collections import defaultdict

import django.db.models.deletion
from django.db import migrations, models


APPLICANT_DOCUMENT_KEYS = {
    "signed_cover_letter",
    "personal_data_sheet",
    "work_experience_sheet",
    "performance_rating",
    "eligibility_or_license",
    "transcript_of_records",
    "diploma",
    "certificate_of_employment",
    "training_certificates",
}
CAR_DOCUMENT_KEY = "comparative_assessment_report"
INTERVIEW_FALLBACK_PREFIX = "interview-rating-sheet-fallback"


def normalize_car_and_evidence(apps, schema_editor):
    RecruitmentApplication = apps.get_model("recruitment", "RecruitmentApplication")
    RecruitmentCase = apps.get_model("recruitment", "RecruitmentCase")
    ComparativeAssessmentReport = apps.get_model("recruitment", "ComparativeAssessmentReport")
    ComparativeAssessmentReportItem = apps.get_model("recruitment", "ComparativeAssessmentReportItem")
    EvidenceVaultItem = apps.get_model("recruitment", "EvidenceVaultItem")

    applications = {
        application.id: application
        for application in RecruitmentApplication.objects.all().only("id", "position_id")
    }
    cases_by_application = {
        recruitment_case.application_id: recruitment_case
        for recruitment_case in RecruitmentCase.objects.all().only("id", "application_id")
    }

    # Backfill CAR item ownership through recruitment cases before the direct application FK is removed.
    for item in ComparativeAssessmentReportItem.objects.filter(recruitment_case__isnull=True).only(
        "id",
        "application_id",
        "recruitment_case_id",
    ):
        case = cases_by_application.get(item.application_id)
        if case is not None:
            item.recruitment_case_id = case.id
            item.save(update_fields=["recruitment_case"])

    # Preserve legacy CAR anchors and convert any duplicate entry-stage rows into explicit versions.
    grouped_reports = defaultdict(list)
    for report in ComparativeAssessmentReport.objects.all().order_by(
        "recruitment_entry_id",
        "review_stage",
        "version_number",
        "created_at",
        "id",
    ):
        grouped_reports[(report.recruitment_entry_id, report.review_stage)].append(report)

    for reports in grouped_reports.values():
        ordered_reports = sorted(
            reports,
            key=lambda report: (
                report.version_number or 0,
                report.finalized_at or report.updated_at or report.created_at,
                report.id,
            ),
        )
        for normalized_version, report in enumerate(ordered_reports, start=1):
            snapshot = dict(report.consolidated_snapshot or {})
            snapshot["normalization_migration"] = {
                "migration": "0015_entry_scoped_car_alignment",
                "legacy_application_id": report.application_id,
                "legacy_recruitment_case_id": report.recruitment_case_id,
                "legacy_version_number": report.version_number,
                "normalized_version_number": normalized_version,
                "note": (
                    "Entry-stage CAR rows were preserved as explicit versions while removing "
                    "legacy application and recruitment-case anchors."
                ),
            }
            report.version_number = normalized_version
            report.consolidated_snapshot = snapshot
            report.save(update_fields=["version_number", "consolidated_snapshot"])

    # Reclassify evidence ownership to application/case/entry according to artifact type.
    for item in EvidenceVaultItem.objects.all().order_by("created_at", "id"):
        application = applications.get(item.application_id)
        recruitment_case = item.recruitment_case or cases_by_application.get(item.application_id)
        entry_id = (
            application.position_id
            if application is not None
            else (
                recruitment_case.application.position_id
                if recruitment_case is not None and recruitment_case.application_id in applications
                else None
            )
        )

        document_key = (item.document_key or "").strip()
        label = (item.label or "").strip().lower()

        if (document_key == CAR_DOCUMENT_KEY or "comparative assessment report" in label) and entry_id is not None:
            item.artifact_scope = "entry"
            item.artifact_type = CAR_DOCUMENT_KEY
            item.recruitment_entry_id = entry_id
            item.application_id = None
            item.recruitment_case_id = None
        elif document_key.startswith(INTERVIEW_FALLBACK_PREFIX) and recruitment_case is not None:
            item.artifact_scope = "case"
            item.artifact_type = "interview_fallback_rating_sheet"
            item.recruitment_entry_id = None
            item.application_id = None
            item.recruitment_case_id = recruitment_case.id
        elif document_key in APPLICANT_DOCUMENT_KEYS or item.stage == "applicant_intake":
            item.artifact_scope = "application"
            item.artifact_type = "applicant_document"
            item.recruitment_entry_id = None
            item.recruitment_case_id = None
        elif recruitment_case is not None:
            item.artifact_scope = "case"
            item.artifact_type = "workflow_evidence"
            item.recruitment_entry_id = None
            item.application_id = None
            item.recruitment_case_id = recruitment_case.id
        else:
            item.artifact_scope = "application"
            item.artifact_type = "workflow_evidence"
            item.recruitment_entry_id = None
            item.recruitment_case_id = None

        item.save(
            update_fields=[
                "application",
                "recruitment_case",
                "recruitment_entry",
                "artifact_scope",
                "artifact_type",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0014_auditlog_traceability_fields"),
    ]

    operations = [
        migrations.RenameField(
            model_name="comparativeassessmentreport",
            old_name="generation_count",
            new_name="version_number",
        ),
        migrations.AddField(
            model_name="evidencevaultitem",
            name="artifact_scope",
            field=models.CharField(
                choices=[
                    ("application", "Application"),
                    ("case", "Recruitment Case"),
                    ("entry", "Recruitment Entry"),
                ],
                default="application",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="evidencevaultitem",
            name="artifact_type",
            field=models.CharField(blank=True, default="supporting_document", max_length=80),
        ),
        migrations.AddField(
            model_name="evidencevaultitem",
            name="recruitment_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evidence_items",
                to="recruitment.positionposting",
            ),
        ),
        migrations.AlterField(
            model_name="evidencevaultitem",
            name="application",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="evidence_items",
                to="recruitment.recruitmentapplication",
            ),
        ),
        migrations.RunPython(normalize_car_and_evidence, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="comparativeassessmentreport",
            options={"ordering": ["review_stage", "-version_number", "-created_at"]},
        ),
        migrations.RemoveConstraint(
            model_name="comparativeassessmentreport",
            name="unique_car_per_application_stage",
        ),
        migrations.RemoveConstraint(
            model_name="comparativeassessmentreportitem",
            name="unique_car_item_per_report_application",
        ),
        migrations.RemoveIndex(
            model_name="evidencevaultitem",
            name="recruitment_applica_1d3765_idx",
        ),
        migrations.RemoveField(
            model_name="comparativeassessmentreport",
            name="application",
        ),
        migrations.RemoveField(
            model_name="comparativeassessmentreport",
            name="recruitment_case",
        ),
        migrations.RemoveField(
            model_name="comparativeassessmentreportitem",
            name="application",
        ),
        migrations.AlterField(
            model_name="comparativeassessmentreportitem",
            name="recruitment_case",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="comparative_assessment_report_items",
                to="recruitment.recruitmentcase",
            ),
        ),
        migrations.AddIndex(
            model_name="evidencevaultitem",
            index=models.Index(
                fields=["artifact_scope", "application", "stage", "document_key"],
                name="recruitment_artifac_25b961_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="evidencevaultitem",
            index=models.Index(
                fields=["artifact_scope", "recruitment_case", "stage", "document_key"],
                name="recruitment_artifac_548295_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="evidencevaultitem",
            index=models.Index(
                fields=["artifact_scope", "recruitment_entry", "stage", "document_key"],
                name="recruitment_artifac_9f4f7c_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="comparativeassessmentreport",
            constraint=models.UniqueConstraint(
                fields=("recruitment_entry", "review_stage", "version_number"),
                name="unique_car_version_per_entry_stage",
            ),
        ),
        migrations.AddConstraint(
            model_name="comparativeassessmentreportitem",
            constraint=models.UniqueConstraint(
                fields=("report", "recruitment_case"),
                name="unique_car_item_per_report_case",
            ),
        ),
        migrations.AddConstraint(
            model_name="evidencevaultitem",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(artifact_scope="application")
                        & models.Q(application__isnull=False)
                        & models.Q(recruitment_case__isnull=True)
                        & models.Q(recruitment_entry__isnull=True)
                    )
                    | (
                        models.Q(artifact_scope="case")
                        & models.Q(application__isnull=True)
                        & models.Q(recruitment_case__isnull=False)
                        & models.Q(recruitment_entry__isnull=True)
                    )
                    | (
                        models.Q(artifact_scope="entry")
                        & models.Q(application__isnull=True)
                        & models.Q(recruitment_case__isnull=True)
                        & models.Q(recruitment_entry__isnull=False)
                    )
                ),
                name="evidence_owner_matches_scope",
            ),
        ),
    ]
