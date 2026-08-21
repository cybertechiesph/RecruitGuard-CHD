from django.db import migrations, models
from django.db.models import Count


def check_candidate_code_uniqueness(apps, schema_editor):
    RecruitmentApplication = apps.get_model("recruitment", "RecruitmentApplication")
    database_alias = schema_editor.connection.alias
    duplicates = (
        RecruitmentApplication.objects.using(database_alias)
        .exclude(candidate_code="")
        .values("position_id", "candidate_code")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .order_by("position_id", "candidate_code")
    )
    duplicate_count = duplicates.count()
    if not duplicate_count:
        return

    examples = "; ".join(
        (
            f"position_id={row['position_id']}, "
            f"candidate_code={row['candidate_code']!r}, rows={row['row_count']}"
        )
        for row in duplicates[:20]
    )
    if duplicate_count > 20:
        examples = f"{examples}; ... and {duplicate_count - 20} more group(s)"
    raise RuntimeError(
        "Cannot add uniq_position_candidate_code: found "
        f"{duplicate_count} duplicate non-blank candidate-code group(s). "
        "Resolve every duplicate before rerunning this migration. "
        f"Conflicts: {examples}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("recruitment", "0079_sl14_cos_decision_rescind"),
    ]

    operations = [
        migrations.RunPython(
            check_candidate_code_uniqueness,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="recruitmentapplication",
            constraint=models.UniqueConstraint(
                fields=("position", "candidate_code"),
                condition=~models.Q(candidate_code=""),
                name="uniq_position_candidate_code",
            ),
        ),
    ]
