"""COS selection: selection rounds, the eligible pool, picks, closing a round
short, and rescinding a COS pick.
"""

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    CosSelectionRound,
    CosSelectionRoundMember,
    CosVacancyDeliberation,
    FinalDecision,
    Notification,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
)
from ..notification_services import (
    queue_non_selected_applicant_notification,
    queue_selection_under_review_notification,
)
from . import audit, case_notices, common, entries, exam, interview, routing, selection


def vacancy_uses_cos_selection_rounds(posting):
    """SL-02: whether this COS vacancy hires in repeated, need-based rounds.

    Only continuous intake does. A continuous vacancy accumulates candidates and the office
    opens a round when it actually needs to hire. One-batch and fixed-period
    COS vacancies are a single batch: one implicit round, resolved once."""
    return (
        posting.branch == PositionPosting.Branch.COS
        and posting.intake_mode == PositionPosting.IntakeMode.CONTINUOUS
    )


def get_open_cos_selection_round(posting):
    """The vacancy's currently open selection round, or None."""
    return (
        CosSelectionRound.objects.select_related("opened_by")
        .filter(recruitment_entry=posting, status=CosSelectionRound.Status.OPEN)
        .first()
    )


def get_latest_cos_selection_round(posting):
    return (
        CosSelectionRound.objects.select_related("opened_by", "resolved_by")
        .filter(recruitment_entry=posting)
        .order_by("-round_number")
        .first()
    )


def get_cos_vacancy_deliberation(posting):
    """The vacancy's most recent COS deliberation pick, or None. SL-02: a vacancy can hold
    several picks over several rounds; callers that need a specific case's pick should use
    ``get_cos_pick_for_case``."""
    return (
        CosVacancyDeliberation.objects.select_related(
            "chosen_case", "chosen_case__application", "recorded_by", "finalized_by",
            "selection_round",
        )
        .filter(recruitment_entry=posting)
        .order_by("-created_at", "-id")
        .first()
    )


def get_cos_round_pick_for_case(case):
    """C5: the effective pick a case page may show: the one naming this case, else the pick of a
    round this case was a member of. A candidate who arrived after a round never sees that
    round's winner as their own decision."""
    pick = get_cos_pick_for_case(case)
    if pick is not None or case is None:
        return pick
    return (
        CosVacancyDeliberation.objects.select_related("selection_round", "chosen_case__application")
        .filter(
            selection_round__members__recruitment_case=case,
            is_finalized=True,
            is_rescinded=False,
        )
        .order_by("-id")
        .first()
    )


def get_cos_pick_for_case(case):
    """The EFFECTIVE COS pick that names this case, or None. SL-01/SL-02: this — not "does
    the vacancy have any finalized pick" — is what decides whether a given candidate has
    been selected.

    SL-14 (COS): a rescinded pick is history, not a decision. Skipping it here is what makes
    the vacancy decidable again — every downstream gate (whether deliberation is finished for
    the case, whether the selection may be recorded) reads through this."""
    if case is None:
        return None
    return (
        CosVacancyDeliberation.objects.select_related("selection_round")
        .filter(chosen_case=case, is_finalized=True, is_rescinded=False)
        .first()
    )


def get_rescinded_cos_picks_for_case(case):
    """SL-14 (COS): the voided picks that once named this case — preserved history, shown
    beside the live state so a corrected decision reads as a correction."""
    if case is None:
        return CosVacancyDeliberation.objects.none()
    return (
        CosVacancyDeliberation.objects.select_related("rescinded_by", "selection_round")
        .filter(chosen_case=case, is_rescinded=True)
        .order_by("-rescinded_at", "-id")
    )


def cos_eligible_pool(posting):
    """The COS vacancy's candidates who could join a selection round: everyone standing at
    the HRM Chief deliberation section with an active, unlocked case.

    SL-02: for a continuous vacancy this is the *accumulating* pool — candidates a round did
    not pick stay here for the next one (the client keeps them on file for about a year),
    which is why a resolved round must not close them."""
    cases = (
        RecruitmentCase.objects.filter(
            application__position=posting,
            current_stage=RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
            is_stage_locked=False,
        )
        .select_related("application", "application__applicant", "application__position")
        .order_by("application__reference_number", "application_id")
    )
    return [
        case.application
        for case in cases
        if routing.get_current_workflow_section(case.application) == "deliberation"
        # SL-06: a no-show whose absence decision is still PENDING is not a candidate —
        # the case is waiting for the reschedule-or-terminal call, and letting it into
        # the pool would let the Chief pick (or the pool-close reject) someone the
        # office has not finished deciding on (live E2E finding). Plantilla is guarded
        # downstream by SL-03's blank-component CAR block; the COS pick has no numeric
        # gate, so the pool boundary has to hold the case itself.
        and exam.get_pending_attendance_exception(case.application) is None
    ]


def cos_selection_round_pool(selection_round):
    """The frozen membership of an open round — the only candidates its pick may resolve."""
    applications = [
        member.recruitment_case.application
        for member in selection_round.members.select_related(
            "recruitment_case",
            "recruitment_case__application",
            "recruitment_case__application__applicant",
            "recruitment_case__application__position",
        )
    ]
    return sorted(
        applications,
        key=lambda application: (application.reference_number or "", application.id),
    )


def cos_deliberation_pool(posting):
    """The candidates the vacancy's next COS pick is chosen from.

    SL-02: for a continuous vacancy that is the frozen membership of the open round (nothing
    until one is opened — the office decides when it is hiring). For a single-batch COS
    vacancy it is the live eligible pool.

    SL-14 (COS): whenever a round is actually OPEN, its frozen membership is the pool for
    both kinds of vacancy. This only bites on a single-batch round reopened by a rescission:
    its members competed, so the replacement must come from them. Reading the live pool
    there would let someone who reached the deliberation step afterwards win a competition
    they were never entered in, and the pool-closing that follows a pick resolves the frozen
    membership — so any newcomer would be left dangling in a round already decided."""
    open_round = get_open_cos_selection_round(posting)
    if open_round is not None:
        return [
            application
            for application in cos_selection_round_pool(open_round)
            if application.case.case_status == RecruitmentCase.CaseStatus.ACTIVE
            and not application.case.is_stage_locked
        ]
    # An open pool with no open round picks from everyone waiting; the pick opens the round
    # (C1). A single-batch vacancy's first pick does the same.
    return cos_eligible_pool(posting)


@transaction.atomic
def open_cos_selection_round(posting, actor, *, slot_count=1, notes=""):
    """SL-02: open a need-based selection round on a continuous COS vacancy.

    Snapshots the candidates eligible right now as the round's members. Only they can be
    picked, and only they are resolved when the round closes — a candidate who applies
    afterwards belongs to a later round, and one who is not picked stays in the pool."""
    if not vacancy_uses_cos_selection_rounds(posting):
        raise ValueError(
            "Selection rounds are for continuous COS vacancies. This vacancy hires as a "
            "single batch."
        )
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief opens a COS selection round.")
    if get_open_cos_selection_round(posting):
        raise ValueError("This vacancy already has an open selection round.")
    try:
        slot_count = int(slot_count)
    except (TypeError, ValueError):
        slot_count = 0
    if slot_count < 1:
        raise ValueError("Record how many positions this round is filling (at least one).")
    eligible = cos_eligible_pool(posting)
    if not eligible:
        raise ValueError(
            "No candidates are ready for a selection round in this vacancy yet."
        )
    if slot_count > len(eligible):
        raise ValueError(
            "This round cannot fill more positions than there are eligible candidates "
            f"({len(eligible)})."
        )
    latest = get_latest_cos_selection_round(posting)
    selection_round = CosSelectionRound(
        recruitment_entry=posting,
        round_number=(latest.round_number + 1) if latest else 1,
        slot_count=slot_count,
        opened_by=actor,
        notes=notes or "",
    )
    selection_round.full_clean()
    selection_round.save()
    for application in eligible:
        exam_record = exam.get_exam_record(application)
        interview_rating = interview.get_interview_ratings(application).first()
        CosSelectionRoundMember.objects.create(
            selection_round=selection_round,
            recruitment_case=application.case,
            snapshot={
                "application_id": application.id,
                "reference_number": application.reference_number,
                "exam_score": common._decimal_string(exam_record.exam_score) if exam_record else "",
                "interview_score": (
                    common._decimal_string(interview_rating.rating_score) if interview_rating else ""
                ),
            },
        )
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.COS_SELECTION_ROUND_OPENED,
        description=(
            f"Opened COS selection round {selection_round.round_number} for "
            f"{posting.job_code} ({slot_count} slot{'s' if slot_count != 1 else ''})."
        ),
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "round_number": selection_round.round_number,
            "slot_count": slot_count,
            "member_case_ids": sorted(
                application.case.id for application in eligible
            ),
        },
    )
    return selection_round


@transaction.atomic
def save_cos_vacancy_deliberation(posting, actor, chosen_case_id, notes, *, finalize=False):
    """Record (and optionally finalize) the one per-vacancy COS deliberation pick — the chosen
    applicant + notes the end-user and HRM Chief agreed on off-system (spec §6.3).

    SL-01: finalizing IS the COS selection. There is no second, independent per-case choice
    that could name a different person — the moment the pick is finalized the chosen
    applicant is recorded as Selected and routed to completion, and the rest of the
    deliberated pool is closed as not selected and notified. One authoritative act, one
    official fact."""
    if posting.branch != PositionPosting.Branch.COS:
        raise ValueError("COS deliberation picks are only for COS vacancies.")
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief records the COS deliberation pick.")

    selection_round = get_open_cos_selection_round(posting)
    if selection_round is None:
        if vacancy_uses_cos_selection_rounds(posting):
            # An open pool hires round by round. The Chief may open a multi-slot round
            # first; otherwise the pick opens the next round for one slot (C1), so a later
            # batch is never stranded behind a resolved round.
            selection_round = open_cos_selection_round(
                posting, actor, slot_count=1, notes="Opened with the pick."
            )
        else:
            # SL-02: a single-batch COS vacancy still gets a round, opened implicitly here, so
            # its membership is recorded rather than re-derived later against a moved pool.
            selection_round = _open_implicit_cos_selection_round(posting, actor)

    pool = cos_deliberation_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready for the COS deliberation in this vacancy.")
    pool_case_ids = {application.case.id for application in pool}
    try:
        chosen_case_id = int(chosen_case_id)
    except (TypeError, ValueError):
        raise ValueError("Choose the selected applicant before saving the COS deliberation pick.")
    if chosen_case_id not in pool_case_ids:
        raise ValueError("The chosen applicant must be one of this vacancy's candidates.")

    # SL-14 (COS): rescinded picks free their slot again — otherwise a voided selection
    # would keep the round "full" and there would be no way to record the replacement.
    finalized_picks = list(
        selection_round.picks.filter(is_finalized=True, is_rescinded=False).order_by(
            "created_at"
        )
    )
    if len(finalized_picks) >= selection_round.slot_count:
        raise ValueError(
            f"Round {selection_round.round_number} has already filled all "
            f"{selection_round.slot_count} of its slots."
        )
    if any(pick.chosen_case_id == chosen_case_id for pick in finalized_picks):
        raise ValueError("This applicant has already been picked in this round.")

    pick = selection_round.picks.filter(is_finalized=False).first()
    if pick is None:
        pick = CosVacancyDeliberation(
            recruitment_entry=posting,
            selection_round=selection_round,
        )
    if pick.supersedes_id is None:
        # SL-14 (COS): if this round had a pick voided, the new pick is its correction —
        # link them so the file reads as one decision superseding another, mirroring the
        # Plantilla FinalSelection chain. Chained to the latest rescinded pick that no
        # other pick has claimed yet, which keeps multi-slot rounds one-to-one.
        pick.supersedes = (
            selection_round.picks.filter(
                is_rescinded=True, superseded_by__isnull=True
            )
            .order_by("-rescinded_at", "-id")
            .first()
        )
    pick.chosen_case_id = chosen_case_id
    pick.notes = notes or ""
    pick.recorded_by = actor
    pick.is_finalized = finalize
    if finalize:
        pick.finalized_by = actor
        pick.finalized_at = timezone.now()
    else:
        pick.finalized_by = None
        pick.finalized_at = None
    pick.full_clean()
    pick.save()

    audit.record_audit_event(
        application=pick.chosen_case.application,
        actor=actor,
        action=(
            AuditLog.Action.DELIBERATION_FINALIZED
            if finalize
            else AuditLog.Action.DELIBERATION_RECORDED
        ),
        description=(
            f"Finalized the COS deliberation pick for round {selection_round.round_number}."
            if finalize
            else f"Saved the COS deliberation pick for round {selection_round.round_number}."
        ),
        metadata={
            "recruitment_entry_id": posting.id,
            "chosen_case_id": pick.chosen_case_id,
            "is_finalized": pick.is_finalized,
            "selection_round_id": selection_round.id,
            "round_number": selection_round.round_number,
            "slot_count": selection_round.slot_count,
            "pool_size": len(pool),
            # SL-01/SL-02: name the candidates actually deliberated in this round, so the
            # record shows the population this pick resolved.
            "deliberated_case_ids": sorted(pool_case_ids),
        },
    )

    if finalize:
        # SL-01: the pick is the selection. Record it now, against the applicant the pick
        # names, while the case is still at HRM Chief Review.
        selection.record_final_decision(
            pick.chosen_case.application,
            actor,
            {
                "decision_outcome": FinalDecision.Outcome.SELECTED,
                "decision_notes": pick.notes,
            },
        )
        if len(finalized_picks) + 1 >= selection_round.slot_count:
            _resolve_cos_selection_round(selection_round, actor)
    return pick


def _open_implicit_cos_selection_round(posting, actor):
    """The single round a non-continuous COS vacancy runs, created at the moment its first
    pick is recorded so the deliberated membership is captured as a fact."""
    eligible = cos_eligible_pool(posting)
    latest = get_latest_cos_selection_round(posting)
    if latest is not None:
        raise ValueError(
            "This vacancy's selection round is already complete. It hires as a single "
            "batch, so there is no second round."
        )
    selection_round = CosSelectionRound(
        recruitment_entry=posting,
        round_number=1,
        slot_count=(
            posting.positions_to_fill
            if entries.get_office_policy().cos_positions_multi_slot_allowed
            else 1
        ),
        opened_by=actor,
    )
    selection_round.full_clean()
    selection_round.save()
    for application in eligible:
        CosSelectionRoundMember.objects.create(
            selection_round=selection_round,
            recruitment_case=application.case,
            snapshot={
                "application_id": application.id,
                "reference_number": application.reference_number,
            },
        )
    return selection_round


def _resolve_cos_selection_round(selection_round, actor):
    """Close a round once its slots are filled.

    SL-02: what happens to the members this round did not pick depends on how the vacancy
    hires. A single-batch vacancy has finished competing, so they are closed as not selected
    and notified. A continuous vacancy has not — they go back into the pool and remain
    available for the next round, which is exactly what the office does with them today."""
    posting = selection_round.recruitment_entry
    selection_round.status = CosSelectionRound.Status.RESOLVED
    selection_round.resolved_at = timezone.now()
    selection_round.resolved_by = actor
    selection_round.save(
        update_fields=["status", "resolved_at", "resolved_by", "updated_at"]
    )
    chosen_case_ids = set(
        selection_round.picks.filter(is_finalized=True, is_rescinded=False).values_list(
            "chosen_case_id", flat=True
        )
    )
    unpicked = [
        member.recruitment_case.application
        for member in selection_round.members.select_related(
            "recruitment_case", "recruitment_case__application"
        )
        if member.recruitment_case_id not in chosen_case_ids
    ]
    retained = []
    if vacancy_uses_cos_selection_rounds(posting):
        retained = [
            application
            for application in unpicked
            if application.case.case_status == RecruitmentCase.CaseStatus.ACTIVE
            and not application.case.is_stage_locked
        ]
    else:
        _close_cos_round_unpicked(selection_round, actor)
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.COS_SELECTION_ROUND_RESOLVED,
        description=(
            f"Resolved COS selection round {selection_round.round_number} for "
            f"{posting.job_code}."
        ),
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "round_number": selection_round.round_number,
            "chosen_case_ids": sorted(chosen_case_ids),
            "unpicked_case_ids": sorted(
                application.case.id for application in unpicked
            ),
            "retained_in_pool": sorted(
                application.case.id for application in retained
            ),
            "closes_unpicked": not vacancy_uses_cos_selection_rounds(posting),
        },
    )
    return selection_round


def user_can_close_cos_selection_round_short(user, posting):
    """The HRM Chief may finish an open COS round before every slot is filled."""
    if posting.branch != PositionPosting.Branch.COS:
        return False
    if user.role != RecruitmentUser.Role.HRM_CHIEF:
        return False
    selection_round = get_open_cos_selection_round(posting)
    if selection_round is None:
        return False
    picks_finalized = selection_round.picks.filter(
        is_finalized=True, is_rescinded=False
    ).count()
    return picks_finalized < selection_round.slot_count


@transaction.atomic
def close_cos_selection_round_short(posting, actor, reason):
    """End an open COS selection round without filling all of its recorded slots."""
    if posting.branch != PositionPosting.Branch.COS:
        raise ValueError("Selection rounds are only for COS vacancies.")
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief may close a COS selection round short.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why this COS selection round is being closed short.")
    selection_round = (
        CosSelectionRound.objects.select_for_update()
        .filter(
            recruitment_entry=posting,
            status=CosSelectionRound.Status.OPEN,
        )
        .first()
    )
    if selection_round is None:
        raise ValueError("This vacancy has no open COS selection round to close.")
    finalized_picks = list(
        selection_round.picks.filter(is_finalized=True, is_rescinded=False)
        .select_related("chosen_case")
        .order_by("created_at", "id")
    )
    if len(finalized_picks) >= selection_round.slot_count:
        raise ValueError("This COS selection round has already filled all of its slots.")

    member_case_ids = sorted(
        selection_round.members.values_list("recruitment_case_id", flat=True)
    )
    selection_round.status = CosSelectionRound.Status.CLOSED_SHORT
    selection_round.resolved_at = timezone.now()
    selection_round.resolved_by = actor
    selection_round.closed_short_reason = reason
    selection_round.save(
        update_fields=[
            "status",
            "resolved_at",
            "resolved_by",
            "closed_short_reason",
            "updated_at",
        ]
    )

    if not vacancy_uses_cos_selection_rounds(posting):
        _close_cos_round_unpicked(selection_round, actor, notes=reason)

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.COS_SELECTION_ROUND_CLOSED_SHORT,
        description=(
            f"Closed COS selection round {selection_round.round_number} short for "
            f"{posting.job_code}: {len(finalized_picks)} of "
            f"{selection_round.slot_count} slots filled."
        ),
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "round_number": selection_round.round_number,
            "slot_count": selection_round.slot_count,
            "picks_finalized": len(finalized_picks),
            "members": member_case_ids,
            "reason": reason,
        },
    )
    return selection_round


def user_can_manage_cos_deliberation(user, posting):
    """Whether the user may record a COS deliberation pick: the HRM Chief, while the
    vacancy's current selection round still has an unfilled slot and candidates in it.

    SL-02: a continuous vacancy can run round after round, so "a pick already exists" no
    longer ends the chief's ability to hire again — an unfilled slot in an open round does.
    A single-batch vacancy still gets exactly one round."""
    if posting.branch != PositionPosting.Branch.COS:
        return False
    if user.role != RecruitmentUser.Role.HRM_CHIEF:
        return False
    selection_round = get_open_cos_selection_round(posting)
    if selection_round is None:
        if vacancy_uses_cos_selection_rounds(posting):
            # C1: the pick opens the next round, so an open pool is pickable whenever
            # someone is waiting at the deliberation.
            return bool(cos_eligible_pool(posting))
        if get_latest_cos_selection_round(posting) is not None:
            return False
        return bool(cos_eligible_pool(posting))
    if (
        selection_round.picks.filter(is_finalized=True, is_rescinded=False).count()
        >= selection_round.slot_count
    ):
        return False
    return bool(cos_deliberation_pool(posting))


def user_can_open_cos_selection_round(user, posting):
    """SL-02: the HRM Chief may open the next need-based round on a continuous COS vacancy
    when none is open and someone is waiting in the pool."""
    if not vacancy_uses_cos_selection_rounds(posting):
        return False
    if user.role != RecruitmentUser.Role.HRM_CHIEF:
        return False
    if get_open_cos_selection_round(posting) is not None:
        return False
    return bool(cos_eligible_pool(posting))


COS_EARLIER_ROUND_RESCIND_MESSAGE = (
    "Only the most recent selection round's pick can be rescinded. Earlier rounds are "
    "history; run a new round instead."
)


def cos_pick_is_from_latest_selection_round(pick):
    """Pre-round historical picks keep the correction behavior they had before SL2-13."""
    if pick is None or pick.selection_round_id is None:
        return True
    latest_round = get_latest_cos_selection_round(pick.recruitment_entry)
    return latest_round is not None and pick.selection_round_id == latest_round.id


def user_can_rescind_cos_vacancy_deliberation(user, pick):
    """SL-14 (COS): who may void a recorded COS selection.

    Same principle as the Plantilla rescind: the role that records the decision. SL-01 made
    the finalized deliberation pick the COS selection itself, and the HRM Chief records it at
    both levels (see ``_final_decision_stage_and_role``), so the HRM Chief holds the
    correction too. Office Policy supplies the cutoff."""
    if pick is None or pick.is_rescinded or not pick.is_finalized:
        return False
    if pick.recruitment_entry.branch != PositionPosting.Branch.COS:
        return False
    if not cos_pick_is_from_latest_selection_round(pick):
        return False
    if selection.rescind_cutoff_block_message(pick.chosen_case.application):
        return False
    return user.role == RecruitmentUser.Role.HRM_CHIEF


@transaction.atomic
def rescind_cos_vacancy_deliberation(pick, actor, reason, *, reference=""):
    """SL-14 (COS): void a finalized COS deliberation pick so the round can be decided again.

    The COS twin of ``rescind_final_selection``. SL-01 collapsed the COS pick and the COS
    selection into one act, so voiding the pick has to unwind everything that act did:
    the chosen applicant left the decision step for completion, and — on a single-batch
    vacancy — the rest of the round was closed as not selected. Both come back.

    Nothing is deleted or edited back to a draft. The pick keeps every value it was recorded
    with and gains the rescission; the FinalDecision row it produced stays as the historical
    fact that the decision was made, with the audit trail recording that it was later voided.

    Where the pick's round is restored to differs from Plantilla by necessity: a Plantilla
    rescind returns cases to the Appointing Authority holding a finalized CAR to re-pick
    from, whereas the COS decision IS the pick, so its cases return to the HRM Chief at the
    deliberation step — where the replacement pick is actually made."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why this COS deliberation pick is being rescinded.")
    if pick.is_rescinded:
        raise ValueError("This COS deliberation pick has already been rescinded.")
    if not pick.is_finalized:
        raise ValueError("Only a finalized COS deliberation pick can be rescinded.")
    if not cos_pick_is_from_latest_selection_round(pick):
        raise ValueError(COS_EARLIER_ROUND_RESCIND_MESSAGE)
    if selection.rescind_cutoff_block_message(pick.chosen_case.application):
        raise ValueError(common.RESCIND_CUTOFF_MESSAGE)
    if not user_can_rescind_cos_vacancy_deliberation(actor, pick):
        raise ValueError(
            "Only the HRM Chief may rescind a recorded COS deliberation pick."
        )

    entry = pick.recruitment_entry
    selection_round = (
        CosSelectionRound.objects.select_for_update().get(pk=pick.selection_round_id)
        if pick.selection_round_id
        else None
    )
    winner = pick.chosen_case.application

    # The population this pick resolved: its own round's members (SL-02), never the whole
    # posting. Anyone the round never entered was not closed by this pick and must not be
    # disturbed by voiding it.
    round_siblings = []
    if selection_round is not None:
        round_siblings = [
            application
            for application in cos_selection_round_pool(selection_round)
            if application.pk != winner.pk
            and application.case.case_status == RecruitmentCase.CaseStatus.REJECTED
        ]

    rescinded_at = timezone.now()
    pick.is_rescinded = True
    pick.rescinded_at = rescinded_at
    pick.rescinded_by = actor
    pick.rescind_reason = reason
    pick.rescind_reference = reference or ""
    pick.full_clean()
    pick.save()
    voided_completion_record = selection._void_completion_record_for_rescission(winner, reason)

    # The pick is only half the record. Finalizing it recorded a FinalDecision(SELECTED)
    # for the winner, and that row is what reopen_recruitment_case reads to decide whether
    # the vacancy is decided and who won — leaving it unmarked would keep a rescinded
    # winner reopenable and keep the real losers frozen. Marked, never deleted: the history
    # still shows the decision was made, and get_final_decision_history still returns it.
    rescinded_decisions = list(
        FinalDecision.objects.filter(
            application=winner,
            recruitment_entry=entry,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            is_rescinded=False,
        )
    )
    for decision in rescinded_decisions:
        decision.is_rescinded = True
        decision.rescinded_at = rescinded_at
        decision.save(update_fields=["is_rescinded", "rescinded_at", "updated_at"])

    # A finished round reopens: its slot is free again and the replacement pick belongs to
    # the same round, not to a new one — nobody ran a second competition.
    round_reopened = False
    if (
        selection_round is not None
        and selection_round.status
        in {
            CosSelectionRound.Status.RESOLVED,
            CosSelectionRound.Status.CLOSED_SHORT,
        }
    ):
        selection_round.status = CosSelectionRound.Status.OPEN
        selection_round.resolved_at = None
        selection_round.resolved_by = None
        selection_round.closed_short_reason = ""
        selection_round.save(
            update_fields=[
                "status",
                "resolved_at",
                "resolved_by",
                "closed_short_reason",
                "updated_at",
            ]
        )
        round_reopened = True
        # The round's own "resolved" event would otherwise stand unqualified in the trail.
        audit.record_system_audit_event(
            actor=actor,
            action=AuditLog.Action.COS_SELECTION_ROUND_OPENED,
            description=(
                f"Reopened COS selection round {selection_round.round_number} for "
                f"{entry.job_code} after its pick was rescinded."
            ),
            metadata={
                "entry_id": entry.id,
                "entry_code": entry.job_code,
                "round_number": selection_round.round_number,
                "selection_round_id": selection_round.id,
                "reopened_after_rescission": True,
                "cos_deliberation_pick_id": pick.id,
                "reason": reason,
            },
        )

    restored = []
    for application in [winner, *round_siblings]:
        case = getattr(application, "case", None)
        if case is None:
            continue
        previous_status = application.status
        previous_case_status = case.case_status
        routing.transition_case(
            application,
            actor,
            to_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
            to_role=RecruitmentUser.Role.HRM_CHIEF,
        )
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
            description=(
                "Returned to the HRM Chief's deliberation step after the COS pick was "
                "rescinded."
            ),
            metadata={
                "cos_deliberation_pick_id": pick.id,
                "selection_round_id": selection_round.id if selection_round else None,
                "round_number": selection_round.round_number if selection_round else None,
                "reason": reason,
                "reference": pick.rescind_reference,
                "was_selected": application.pk == winner.pk,
                "completion_record_voided": bool(
                    application.pk == winner.pk and voided_completion_record
                ),
                "from_status": previous_status,
                "to_status": application.status,
                "from_case_status": previous_case_status,
                "to_case_status": case.case_status,
            },
        )
        restored.append(application)

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
        description=(
            f"Rescinded the COS deliberation pick for {entry.job_code} "
            f"({winner.reference_label})."
        ),
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "branch": entry.branch,
            "cos_deliberation_pick_id": pick.id,
            "selection_round_id": selection_round.id if selection_round else None,
            "round_number": selection_round.round_number if selection_round else None,
            "previous_selected_application_id": winner.id,
            "reason": reason,
            "reference": pick.rescind_reference,
            "restored_application_ids": sorted(a.id for a in restored),
            "rescinded_final_decision_ids": sorted(d.id for d in rescinded_decisions),
            "selection_round_reopened": round_reopened,
            "voided_completion_record_id": (
                voided_completion_record.id if voided_completion_record else None
            ),
        },
    )
    # Notify only on the winner's restored case. The other restored round members are one
    # receiving-office workload event, not separate rescission events.
    case_notices._emit_case_assignment_notification(
        winner,
        actor,
        RecruitmentUser.Role.HRM_CHIEF,
        kind=Notification.Kind.CASE_RETURNED,
        title=f"{winner.reference_label} COS pick rescinded — round reopened",
        body=reason,
        exclude_user_ids={actor.id},
    )
    for application in restored:
        queue_selection_under_review_notification(application, actor=actor)
    return pick, restored


def _close_cos_round_unpicked(selection_round, actor, *, notes=""):
    """Close the unpicked members of a finished single-batch COS round.

    Full resolution and a short close deliberately share this path. Continuous-vacancy
    members remain retained candidates, and finalized picks are never touched.
    """
    posting = selection_round.recruitment_entry
    if vacancy_uses_cos_selection_rounds(posting):
        return []
    finalized_picks = list(
        selection_round.picks.filter(is_finalized=True, is_rescinded=False)
        .select_related("chosen_case", "chosen_case__application")
        .order_by("created_at", "id")
    )
    chosen_case_ids = {pick.chosen_case_id for pick in finalized_picks}
    selected_application_ids = sorted(
        pick.chosen_case.application_id for pick in finalized_picks
    )
    representative_application = (
        finalized_picks[0].chosen_case.application if finalized_picks else None
    )
    representative_decision = (
        selection.get_latest_final_decision(representative_application)
        if representative_application
        else None
    )
    decision_notes = (
        representative_decision.decision_notes if representative_decision else notes
    )
    siblings = [
        application
        for application in cos_selection_round_pool(selection_round)
        if application.case.id not in chosen_case_ids
        and application.case.case_status == RecruitmentCase.CaseStatus.ACTIVE
        and not application.case.is_stage_locked
    ]
    closed = []
    for sibling in siblings:
        previous_role = sibling.current_handler_role
        previous_status = sibling.status
        case_transition = routing.transition_case(
            sibling,
            actor,
            to_status=RecruitmentApplication.Status.REJECTED,
            to_role="",
            route_type=RoutingHistory.RouteType.CLOSE,
            description="Closed as not selected after the COS deliberation pick's selection was recorded.",
            notes=decision_notes,
        )
        audit.record_audit_event(
            application=sibling,
            actor=actor,
            action=AuditLog.Action.DECISION_RECORDED,
            description=(
                "Closed as not selected after the COS selection was recorded for another "
                f"applicant in round {selection_round.round_number}."
            ),
            metadata={
                "final_decision_id": (
                    representative_decision.id if representative_decision else None
                ),
                "selected_application_id": (
                    representative_application.id if representative_application else None
                ),
                "selected_application_ids": selected_application_ids,
                # SL-02 AC#3: name the cycle this application was actually considered in.
                "selection_round_id": selection_round.id,
                "round_number": selection_round.round_number,
                "decision_outcome": FinalDecision.Outcome.NOT_SELECTED,
                "from_status": previous_status,
                "to_status": sibling.status,
                "from_role": previous_role,
                "to_role": "",
                "from_stage": case_transition["previous_stage"],
                "to_stage": sibling.case.current_stage,
                "from_case_status": case_transition["previous_case_status"],
                "to_case_status": sibling.case.case_status,
                "case_locked": sibling.case.is_stage_locked,
            },
        )
        queue_non_selected_applicant_notification(sibling, actor=actor)
        closed.append(sibling)
    return closed
