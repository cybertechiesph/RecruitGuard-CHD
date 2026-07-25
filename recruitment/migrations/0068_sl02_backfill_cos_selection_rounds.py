from django.db import migrations


def backfill_rounds(apps, schema_editor):
    """SL-02 — give every existing COS deliberation pick a round to belong to.

    Before this change a vacancy held exactly one pick and the deliberated membership was
    never persisted (the finalize audit event recorded only a pool size). So the historical
    round is reconstructed rather than recovered: round 1, one slot, its members being the
    cases that actually reached the HRM Chief stage or beyond in that vacancy. That is the
    population the old code would have resolved, and it is marked resolved when the pick
    was finalized. Approximate by necessity, and stated as such — no new outcome is
    inferred for anybody, and no case state is touched.
    """
    CosVacancyDeliberation = apps.get_model("recruitment", "CosVacancyDeliberation")
    CosSelectionRound = apps.get_model("recruitment", "CosSelectionRound")
    CosSelectionRoundMember = apps.get_model("recruitment", "CosSelectionRoundMember")
    RecruitmentCase = apps.get_model("recruitment", "RecruitmentCase")

    considered_stages = {
        "hrm_chief_review",
        "completion",
        "closed",
    }
    for pick in CosVacancyDeliberation.objects.filter(selection_round__isnull=True):
        selection_round = CosSelectionRound.objects.create(
            recruitment_entry_id=pick.recruitment_entry_id,
            round_number=1,
            slot_count=1,
            status="resolved" if pick.is_finalized else "open",
            opened_by_id=pick.recorded_by_id,
            opened_by_role=pick.recorded_by_role,
            resolved_at=pick.finalized_at,
            resolved_by_id=pick.finalized_by_id,
            notes="Historical round reconstructed when selection rounds were introduced.",
        )
        pick.selection_round = selection_round
        pick.save(update_fields=["selection_round"])
        member_case_ids = set(
            RecruitmentCase.objects.filter(
                application__position_id=pick.recruitment_entry_id,
                current_stage__in=considered_stages,
            ).values_list("id", flat=True)
        )
        member_case_ids.add(pick.chosen_case_id)
        for case_id in sorted(member_case_ids):
            CosSelectionRoundMember.objects.get_or_create(
                selection_round=selection_round,
                recruitment_case_id=case_id,
                defaults={"snapshot": {"reconstructed": True}},
            )


def unbackfill(apps, schema_editor):
    CosVacancyDeliberation = apps.get_model("recruitment", "CosVacancyDeliberation")
    CosSelectionRound = apps.get_model("recruitment", "CosSelectionRound")
    CosVacancyDeliberation.objects.update(selection_round=None)
    CosSelectionRound.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("recruitment", "0067_sl02_cos_selection_rounds"),
    ]

    operations = [
        migrations.RunPython(backfill_rounds, unbackfill),
    ]
