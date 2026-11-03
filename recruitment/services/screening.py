"""Screening: screening records, document review, resubmission requests and the
vacancy screening batch gate.
"""

from collections import namedtuple
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RoutingHistory,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from ..notification_services import queue_document_resubmission_request_notification
from ..requirements import get_applicant_document_requirements
from . import (
    access,
    audit,
    case_notices,
    common,
    cos_selection,
    entries,
    intake,
    routing,
)


PENDING_RESUBMISSION_SCREENING_FINALIZE_MESSAGE = (
    "The applicant's resubmission is still pending. Wait for the new documents (or remove "
    "the applicant from the batch) before finalizing this screening as Qualified."
)
# Default window the applicant has to re-upload flagged documents before the secretariat
# may remove them from the batch (the "Incomplete shall not be entertained" final outcome).
DOCUMENT_RESUBMISSION_DEADLINE_DAYS = 14
PLANTILLA_POOL_NOT_FINAL_MESSAGE = (
    "Plantilla deliberation and CAR generation are available only after the vacancy "
    "is closed or its closing date has passed."
)


def get_screening_record(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).prefetch_related(
        "document_reviews__evidence_item",
    ).filter(review_stage=review_stage).first()


def get_screening_records(application):
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).prefetch_related(
        "document_reviews__evidence_item",
    ).order_by("created_at")


def user_can_manage_screening(user, application):
    current_stage = routing.get_current_review_stage(application)
    if (
        user.role not in common.SCREENING_REVIEW_ROLES
        or current_stage not in common.SCREENING_STAGES
        or routing.get_current_workflow_section(application) != "screening"
    ):
        return False
    return access.user_can_process_application(user, application)


def screening_is_finalized_for_current_stage(application):
    screening_record = get_screening_record(application)
    return bool(screening_record and screening_record.is_finalized)


VacancyScreeningBatchStatus = namedtuple(
    "VacancyScreeningBatchStatus",
    ["total", "qualified", "terminal", "pending", "pending_references", "is_ready"],
)


def _vacancy_screening_stage(posting):
    """The screening stage a vacancy's applications enter, derived from its level
    (Level 1 -> Secretariat, Level 2 -> HRM Chief). A vacancy has a single level, so all
    its submitted applications share one screening stage. Mirrors _route_for_submission."""
    if posting.level == PositionPosting.Level.LEVEL_1:
        return RecruitmentCase.Stage.SECRETARIAT_REVIEW
    return RecruitmentCase.Stage.HRM_CHIEF_REVIEW


def _vacancy_batch_stage(posting):
    """The review stage the vacancy's pooled batch currently occupies.

    The level derives the INTAKE stage, but a Level-1 COS batch takes a second,
    independent pass at the HRM Chief stage (SL-05) — and the batch consoles have to
    follow the pool there. Pinning them to the intake stage left the second-pass exam
    with NO door at all: the console's pool was empty (wrong stage) while the per-case
    exam form stays one-door-refused for a pooled vacancy (live E2E finding).

    The pooled batch moves between stages in lockstep, so the active, unlocked review
    cases sit at one stage at a time; if a transition ever left them split, the earlier
    stage wins so the gate stays conservative. Falls back to the intake stage when no
    active pool exists (nothing submitted yet, or everything terminal)."""
    stages = set(
        RecruitmentCase.objects.filter(
            application__position=posting,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
            is_stage_locked=False,
            current_stage__in=[
                RecruitmentCase.Stage.SECRETARIAT_REVIEW,
                RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            ],
        ).values_list("current_stage", flat=True)
    )
    if RecruitmentCase.Stage.SECRETARIAT_REVIEW in stages:
        return RecruitmentCase.Stage.SECRETARIAT_REVIEW
    if RecruitmentCase.Stage.HRM_CHIEF_REVIEW in stages:
        return RecruitmentCase.Stage.HRM_CHIEF_REVIEW
    return _vacancy_screening_stage(posting)


def vacancy_screening_batch_status(posting):
    """Classify the screening state of every submitted application in a vacancy so the batch
    can move to exam only when no application is still pending. Screening is INDIVIDUAL but the
    qualified pool moves to exam TOGETHER (§4.3): a single pending member (awaiting resubmission,
    or finalized-but-undecided, or not yet finalized) makes the whole batch wait. Removed
    (disqualified/rejected) and withdrawn applications are terminal and do NOT block.

    DRAFT applications are excluded, so screening an early submitter before others have submitted
    does not deadlock the batch."""
    stage = _vacancy_batch_stage(posting)
    applications = list(
        RecruitmentApplication.objects.filter(position=posting)
        .exclude(status=RecruitmentApplication.Status.DRAFT)
        .select_related("case")
        .prefetch_related("screening_records")
    )
    qualified = terminal = pending = 0
    pending_references = []
    for application in applications:
        case = getattr(application, "case", None)
        if application.status in {
            RecruitmentApplication.Status.REJECTED,
            RecruitmentApplication.Status.WITHDRAWN,
            RecruitmentApplication.Status.VACANCY_CANCELLED,
        } or (case and case.case_status == RecruitmentCase.CaseStatus.REJECTED):
            terminal += 1
            continue
        if case is None or case.current_stage != stage:
            # Already advanced past screening (exam/interview/decision/completion) — decided.
            qualified += 1
            continue
        if case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
            pending += 1
            pending_references.append(application.reference_label)
            continue
        record = next(
            (r for r in application.screening_records.all() if r.review_stage == stage),
            None,
        )
        if (
            record
            and record.is_finalized
            and record.completeness_status == ScreeningRecord.CompletenessStatus.COMPLETE
            and record.qualification_outcome == ScreeningRecord.QualificationOutcome.QUALIFIED
        ):
            qualified += 1
        else:
            pending += 1
            pending_references.append(application.reference_label)
    return VacancyScreeningBatchStatus(
        total=len(applications),
        qualified=qualified,
        terminal=terminal,
        pending=pending,
        pending_references=pending_references,
        # An open pool never has "everyone screened": people keep applying. Its batch is
        # whoever is screened and ready; later applicants join the next batch (C1).
        is_ready=(pending == 0) or cos_selection.vacancy_uses_cos_selection_rounds(posting),
    )


def vacancy_screening_batch_block_message(posting):
    status = vacancy_screening_batch_status(posting)
    if status.is_ready:
        return ""
    count = status.pending
    return (
        f"The exam opens once every applicant has a screening decision: {count} "
        f"{'is' if count == 1 else 'are'} still in screening or waiting for corrected documents."
    )


def vacancy_batch_pool_is_final(posting):
    """SL-09: whether a Plantilla vacancy's applicant pool is settled enough to run its
    single vacancy-wide batch.

    For a fixed-period recruitment "everyone has been screened" cannot be known until the
    last timely applicant can no longer apply. Starting the batch earlier silently creates
    a second cohort the model has no name for, and a candidate who applied on time misses
    the sitting they were entitled to.

    Scoped to Plantilla deliberately. The finding is written for the fixed-period Plantilla
    batch, and open-pool COS vacancies have no closing date at all — applying the
    same rule to them would lock their consoles permanently rather than temporarily."""
    if posting.branch != PositionPosting.Branch.PLANTILLA:
        return True
    return posting.applicant_pool_is_finalized


def vacancy_batch_pool_block_message(posting):
    """Why the batch is locked and when it unlocks — a date, not just a refusal."""
    if vacancy_batch_pool_is_final(posting):
        return ""
    closing_date = getattr(posting, "closing_date", None)
    if closing_date:
        return (
            f"Publication is open until {closing_date:%B %d, %Y} — batch examination and "
            "interview unlock when the applicant pool is final. Screening each applicant "
            "can continue in the meantime."
        )
    return (
        "Batch examination and interview unlock when the applicant pool is final. Close "
        "the vacancy once its application period is over. Screening each applicant "
        "can continue in the meantime."
    )


def _require_final_batch_pool(posting):
    message = vacancy_batch_pool_block_message(posting)
    if message:
        raise ValueError(message)


def application_requires_finalized_applicant_pool(application):
    return (
        application.branch == PositionPosting.Branch.PLANTILLA
        and routing.get_current_review_stage(application) == RecruitmentCase.Stage.HRMPSB_REVIEW
    )


def application_has_finalized_applicant_pool(application):
    if not application_requires_finalized_applicant_pool(application):
        return True
    return application.position.applicant_pool_is_finalized


def get_applicant_pool_finalization_block_message(application):
    closing_date = getattr(application.position, "closing_date", None)
    if closing_date:
        return f"{PLANTILLA_POOL_NOT_FINAL_MESSAGE} Current closing date: {closing_date:%Y-%m-%d}."
    return f"{PLANTILLA_POOL_NOT_FINAL_MESSAGE} Close the vacancy before starting HRMPSB deliberation."


SCREENING_COMPLETENESS_BLOCKING_DOCUMENT_STATUSES = {
    ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED,
    ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION,
    ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED,
}


def _screening_document_review_rows(
    application,
    screening_record=None,
    document_reviews=None,
    *,
    default_submitted_status=ScreeningDocumentReview.ReviewStatus.COMPLIED,
):
    current_documents = intake.get_current_applicant_document_map(application)
    requirements = get_applicant_document_requirements(application)
    provided_by_key = {
        (row.get("document_key") or ""): row
        for row in (document_reviews or [])
        if row.get("document_key")
    }
    existing_by_key = {}
    if screening_record and screening_record.pk:
        existing_by_key = {
            review.document_key: review
            for review in screening_record.document_reviews.select_related("evidence_item")
        }

    rows = []
    for display_order, requirement in enumerate(requirements, start=1):
        evidence = current_documents.get(requirement.code)
        provided = provided_by_key.get(requirement.code, {})
        existing_review = existing_by_key.get(requirement.code)
        is_not_applicable = (
            requirement.conditional_on_performance_rating
            and application.performance_rating_not_applicable
            and evidence is None
        )
        is_required = requirement.is_required or (
            requirement.conditional_on_performance_rating
            and not application.performance_rating_not_applicable
        )

        if is_not_applicable:
            status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
        elif evidence is None and is_required:
            status = ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED
        elif evidence is None:
            status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
        elif provided.get("status"):
            status = provided["status"]
        elif existing_review is not None:
            status = existing_review.status
        else:
            status = default_submitted_status
        remarks = (provided.get("remarks") or "").strip()
        if not remarks and existing_review is not None:
            remarks = existing_review.remarks

        rows.append(
            {
                "document_key": requirement.code,
                "requirement_title": requirement.title,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
                "status": status,
                "remarks": remarks,
                "is_required": is_required,
                "is_not_applicable": is_not_applicable,
                "evidence_item": evidence,
                "display_order": display_order,
            }
        )
    return rows


def _validate_screening_review_consistency(
    *,
    qualification_outcome,
    screening_notes,
    document_reviews,
):
    if (
        qualification_outcome == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
        and not (screening_notes or "").strip()
    ):
        raise ValueError("Record screening notes before marking this applicant not qualified.")

    blocking_reviews = [
        row
        for row in document_reviews
        if row["is_required"]
        and row["status"] in SCREENING_COMPLETENESS_BLOCKING_DOCUMENT_STATUSES
    ]
    # RD-1 asymmetric gate (service-layer mirror of the form rule): Qualified
    # requires every required document Complied; Not Qualified is always allowed.
    if (
        qualification_outcome == ScreeningRecord.QualificationOutcome.QUALIFIED
        and blocking_reviews
    ):
        blocking_labels = "; ".join(row["requirement_title"] for row in blocking_reviews)
        raise ValueError(
            "Every required document must be marked Complied before this applicant can be "
            f"marked Qualified. Still pending: {blocking_labels}."
        )
    for row in document_reviews:
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
            and row["is_required"]
            and not row["is_not_applicable"]
        ):
            raise ValueError(f"{row['requirement_title']} cannot be marked not applicable.")
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.COMPLIED
            and row["evidence_item"] is None
        ):
            raise ValueError(
                f"{row['requirement_title']} cannot be marked Complied without an uploaded file."
            )
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
            and not (row.get("remarks") or "").strip()
        ):
            raise ValueError(
                f"Record resubmission instructions for {row['requirement_title']}."
            )


def _sync_screening_document_reviews(screening_record, document_reviews):
    active_keys = []
    for row in document_reviews:
        active_keys.append(row["document_key"])
        review, _created = ScreeningDocumentReview.objects.update_or_create(
            screening_record=screening_record,
            document_key=row["document_key"],
            defaults={
                "evidence_item": row["evidence_item"],
                "requirement_title": row["requirement_title"],
                "requirement_label": row["requirement_label"],
                "status": row["status"],
                "remarks": row["remarks"],
                "is_required": row["is_required"],
                "is_not_applicable": row["is_not_applicable"],
                "display_order": row["display_order"],
            },
        )
        review.full_clean()
        review.save()
    screening_record.document_reviews.exclude(document_key__in=active_keys).delete()


def _document_review_status_counts(document_reviews):
    counts = {}
    for row in document_reviews:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


@transaction.atomic
@common.locks_workflow_row("case")
def save_screening_review(
    application, actor, cleaned_data, finalize=False, allow_partial=False, record_audit=True
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before screening can be recorded.")
    review_stage = application.case.current_stage
    screening_record = get_screening_record(application, stage=review_stage)
    if screening_record and screening_record.is_finalized:
        raise ValueError("Finalized screening records cannot be edited.")
    if not user_can_manage_screening(actor, application):
        raise ValueError("This case is not currently assigned to you for screening.")
    if review_stage not in common.SCREENING_STAGES:
        raise ValueError("Screening can only be edited while the case is assigned to Secretariat or HRM Chief review.")
    if (
        finalize
        and application.case.case_status
        == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        and cleaned_data.get("qualification_outcome")
        != ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
    ):
        raise ValueError(PENDING_RESUBMISSION_SCREENING_FINALIZE_MESSAGE)

    created = screening_record is None
    if screening_record is None:
        screening_record = ScreeningRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            reviewed_by=actor,
            branch=application.branch,
            level=application.level,
        )

    allow_partial = bool(allow_partial and not finalize)

    document_reviews = _screening_document_review_rows(
        application,
        screening_record,
        cleaned_data.get("document_reviews"),
    )
    if not allow_partial:
        _validate_screening_review_consistency(
            qualification_outcome=cleaned_data["qualification_outcome"],
            screening_notes=cleaned_data["screening_notes"],
            document_reviews=document_reviews,
        )

    screening_record.recruitment_case = application.case
    screening_record.reviewed_by = actor
    # RD-1: completeness is an output of the document review, not a reviewer's
    # answer. Stored so the CAR, exports, locked views, and audit keep reading it.
    screening_record.completeness_status = ScreeningRecord.derive_completeness_status(
        document_reviews
    )
    screening_record.completeness_notes = cleaned_data["completeness_notes"]
    screening_record.qualification_outcome = cleaned_data["qualification_outcome"]
    screening_record.education_score = common._optional_decimal(cleaned_data.get("education_score"))
    screening_record.training_score = common._optional_decimal(cleaned_data.get("training_score"))
    screening_record.experience_score = common._optional_decimal(cleaned_data.get("experience_score"))
    screening_record.document_review_score = common._optional_decimal(cleaned_data.get("document_review_score"))
    screening_record.screening_notes = cleaned_data["screening_notes"]
    screening_record.is_finalized = False
    screening_record.finalized_by = None
    screening_record.finalized_at = None
    if allow_partial:
        # Draft: skip full_clean's required-field enforcement (blank
        # completeness/qualification are allowed mid-review) but still guard the
        # unique constraint. Finalize always runs full_clean below.
        screening_record.validate_unique()
    else:
        screening_record.full_clean()
    screening_record.save()
    _sync_screening_document_reviews(screening_record, document_reviews)
    if finalize:
        screening_record.is_finalized = True
        screening_record.finalized_by = actor
        screening_record.finalized_at = timezone.now()
        screening_record.full_clean()
        screening_record.save()

    # REV-D: background autosaves pass record_audit=False (matching the exam /
    # interview / deliberation services) so one screening session logs its
    # deliberate saves and finalize — not dozens of identical draft rows.
    if record_audit or finalize:
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=(
                AuditLog.Action.SCREENING_FINALIZED
                if finalize
                else AuditLog.Action.SCREENING_RECORDED
            ),
            description=(
                "Finalized screening output."
                if finalize
                else "Saved screening review."
            ),
            metadata={
                "screening_record_id": screening_record.id,
                "created": created,
                "review_stage": review_stage,
                "completeness_status": screening_record.completeness_status,
                "qualification_outcome": screening_record.qualification_outcome,
                "document_review_status_counts": _document_review_status_counts(document_reviews),
                "is_finalized": screening_record.is_finalized,
            },
        )
    if finalize:
        case_notices._emit_screening_finalized_notification(application, actor, review_stage)
        # Coupling (Gap B): a *definitive* Not-Qualified screening cut rejects the
        # applicant and notifies them in the same action, so a screened-out applicant
        # can never be silently stranded. A cut is "definitive" only when the documents
        # are complete and nothing is flagged for resubmission — i.e. a true QS failure
        # with nothing left to fix. When documents are incomplete or a resubmission was
        # requested, the handler keeps the reject-or-return choice (both of which notify
        # the applicant), so we do not auto-reject. The rejection notice uses
        # screening-specific wording because the reject fires from a screening stage.
        # RD-1: Not Qualified IS the definitive cut. A reviewer who wants the
        # applicant to fix documents chooses "Return to applicant" instead, so
        # reaching finalize with Not Qualified means the QS failure stands
        # regardless of any flags — those become moot. The applicant therefore
        # receives exactly one message (the rejection): no resubmission email is
        # sent, because request_document_resubmission is never called on this path.
        is_definitive_screening_cut = (
            screening_record.qualification_outcome
            == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
        )
        if is_definitive_screening_cut:
            routing.process_workflow_action(
                application,
                actor,
                "reject",
                screening_record.screening_notes
                or "Did not meet the Qualification Standards at screening.",
            )
    return screening_record


@transaction.atomic
@common.locks_workflow_row("case")
def return_screening_documents_to_applicant(application, actor, cleaned_data, remarks=""):
    """RD-1: the reviewer's "Return to applicant" decision — one atomic action.

    Screening no longer has to be finalized (with a verdict invented on evidence
    the reviewer just flagged) before documents can go back. This saves the
    in-progress screening — flags and remarks, record NOT finalized — and fires
    the existing resubmission request (deadline, applicant email, Track
    Application re-upload page) inside a single transaction, so a failure in
    either half leaves no in-between state.

    The case stays with the reviewing role as "awaiting resubmission" (the
    vacancy's exam batch stays parked) exactly as before; only the entry point
    changed.
    """
    screening_record = save_screening_review(
        application=application,
        actor=actor,
        cleaned_data=cleaned_data,
        finalize=False,
        allow_partial=True,
    )
    request_document_resubmission(
        application,
        actor,
        (remarks or "").strip()
        or "Return the flagged documents through the Track Application page.",
    )
    screening_record.refresh_from_db()
    return screening_record


def _flagged_resubmission_reviews(application, *, stage):
    """The flagged (Return for Resubmission) document-review rows on a stage's screening record."""
    screening_record = get_screening_record(application, stage=stage)
    if screening_record is None:
        return screening_record, []
    flagged = list(
        screening_record.document_reviews.filter(
            status=ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
        ).order_by("display_order", "created_at")
    )
    return screening_record, flagged


@transaction.atomic
def request_document_resubmission(application, actor, remarks):
    """Request resubmission of the flagged documents while keeping the case VISIBLE in the
    reviewing role's queue/console as "awaiting resubmission" (it is NOT handed off to the
    applicant and dropped from the workflow, which is the old return-to-applicant behavior).

    Records a per-resubmission deadline (default 2 weeks), keeps the reviewing role as handler,
    pauses the stage SLA, and emails the applicant a notify-only Track Application link. The
    applicant then re-uploads only the flagged documents through that page.
    """
    case = application.case
    review_stage = case.current_stage
    screening_record, flagged = _flagged_resubmission_reviews(application, stage=review_stage)
    if not flagged:
        raise ValueError("Flag at least one document for resubmission before requesting it.")

    previous_status = application.status
    previous_case_status = case.case_status
    reviewing_role = application.current_handler_role

    deadline = timezone.localdate() + timedelta(days=DOCUMENT_RESUBMISSION_DEADLINE_DAYS)
    screening_record.resubmission_requested_at = timezone.now()
    screening_record.resubmission_deadline = deadline
    screening_record.resubmission_reminder_sent_at = None
    screening_record.save(
        update_fields=[
            "resubmission_requested_at",
            "resubmission_deadline",
            "resubmission_reminder_sent_at",
            "updated_at",
        ]
    )

    # The case stays at its reviewing stage with the reviewing handler, unlocked, but flagged as
    # awaiting the applicant's resubmission so the SLA pauses and the batch console surfaces it.
    # The application becomes applicant-editable (RETURNED_TO_APPLICANT) so the scoped
    # re-upload + OTP work, but it KEEPS the reviewing role as handler so it stays in that
    # role's queue.
    routing.transition_case(
        application,
        actor,
        to_status=RecruitmentApplication.Status.RETURNED_TO_APPLICANT,
        to_role=reviewing_role,
        route_type=RoutingHistory.RouteType.FORWARD,
        description="Waiting for the applicant to resubmit flagged documents; the reviewer keeps the case.",
        notes=remarks,
    )
    # Invalidate any leftover OTP (e.g. from the original submission) so the applicant must
    # verify a fresh code before resubmitting.
    application.otp_hash = ""
    application.otp_requested_at = None
    application.otp_expires_at = None
    application.otp_verified_at = None
    application.otp_attempt_count = 0
    application.save(
        update_fields=[
            "otp_hash",
            "otp_requested_at",
            "otp_expires_at",
            "otp_verified_at",
            "otp_attempt_count",
            "updated_at",
        ]
    )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description="Requested resubmission of flagged documents.",
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": application.status,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
            "resubmission_deadline": deadline.isoformat(),
            "flagged_document_keys": [row.document_key for row in flagged],
        },
    )
    queue_document_resubmission_request_notification(
        application,
        actor,
        document_reviews=flagged,
        workflow_remarks=remarks,
        resubmission_deadline=deadline,
    )
    return case


def user_can_extend_resubmission_deadline(user, application):
    case = getattr(application, "case", None)
    if (
        not entries.get_office_policy().allow_resubmission_deadline_extension
        or case is None
        or case.case_status != RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        or not access.user_can_process_application(user, application)
    ):
        return False
    screening_record = get_screening_record(application, stage=case.current_stage)
    return bool(screening_record and screening_record.resubmission_deadline)


@transaction.atomic
def extend_document_resubmission_deadline(application, actor, new_deadline, reason):
    if not entries.get_office_policy().allow_resubmission_deadline_extension:
        raise ValueError("Office Policy does not allow resubmission deadline extensions.")
    if not user_can_extend_resubmission_deadline(actor, application):
        raise ValueError(
            "This pending resubmission is not currently assigned to you for review."
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why the resubmission deadline is being extended.")
    case = application.case
    screening_record, flagged = _flagged_resubmission_reviews(
        application, stage=case.current_stage
    )
    old_deadline = screening_record.resubmission_deadline
    if new_deadline is None:
        raise ValueError("Choose the new resubmission deadline.")
    if new_deadline < timezone.localdate():
        raise ValueError("The new resubmission deadline cannot be in the past.")
    if new_deadline <= old_deadline:
        raise ValueError("The new deadline must be later than the current deadline.")

    screening_record.resubmission_deadline = new_deadline
    screening_record.resubmission_reminder_sent_at = None  # the new deadline gets its own reminder
    screening_record.save(
        update_fields=["resubmission_deadline", "resubmission_reminder_sent_at", "updated_at"]
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description="Extended the pending document resubmission deadline.",
        metadata={
            "old_resubmission_deadline": old_deadline.isoformat(),
            "new_resubmission_deadline": new_deadline.isoformat(),
            "actor_id": actor.id,
            "actor_role": actor.role,
            "reason": reason,
        },
    )
    queue_document_resubmission_request_notification(
        application,
        actor,
        document_reviews=flagged,
        workflow_remarks=reason,
        resubmission_deadline=new_deadline,
        deadline_was_extended=True,
    )
    return screening_record


def get_resubmission_request(application):
    """For an application whose case is awaiting resubmission, return the document requirements
    the applicant must re-upload (the flagged ones), the resubmission deadline, and a
    {document_code: reviewer remarks} map explaining why each was returned. Returns
    ([], None, {}) when the application is not awaiting resubmission."""
    case = getattr(application, "case", None)
    if case is None or case.case_status != RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
        return [], None, {}
    screening_record, flagged = _flagged_resubmission_reviews(application, stage=case.current_stage)
    flagged_codes = {row.document_key for row in flagged}
    remarks_by_code = {row.document_key: (row.remarks or "") for row in flagged}
    requirements = [
        requirement
        for requirement in get_applicant_document_requirements(application)
        if requirement.code in flagged_codes
    ]
    deadline = screening_record.resubmission_deadline if screening_record else None
    return requirements, deadline, remarks_by_code
