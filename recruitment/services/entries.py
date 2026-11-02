"""Recruitment entries (vacancies): office policy, posting, status changes,
assessment weights, document requirements and the vacancy panel.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    ComparativeAssessmentReport,
    CosVacancyDeliberation,
    InterviewFallbackRating,
    FinalSelection,
    InterviewRating,
    OfficePolicy,
    PositionDocumentRequirement,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    VacancyAssessmentWeights,
)
from ..notification_services import queue_vacancy_cancellation_notification
from ..requirements import (
    APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH,
    MIN_REQUIRED_DOCUMENT_CODES,
)
from . import audit, routing


def get_office_policy():
    """Return the single office-policy row, creating the shipped defaults if needed."""
    policy, _created = OfficePolicy.objects.get_or_create(pk=OfficePolicy.SINGLETON_PK)
    return policy


@transaction.atomic
def update_office_policy(policy_form, actor):
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief may update Office Policy.")
    policy = policy_form.instance
    changed_fields = list(policy_form.changed_data)
    stored_policy = OfficePolicy.objects.get(pk=policy.pk)

    def _audit_value(holder, field):
        value = getattr(holder, field)
        if hasattr(value, "all"):  # a many-to-many list (the core panel): record names
            return sorted(user.get_full_name() or user.username for user in value.all())
        return value

    previous = {field: _audit_value(stored_policy, field) for field in changed_fields}
    policy = policy_form.save()
    changes = {
        field: {"old": previous[field], "new": _audit_value(policy, field)}
        for field in changed_fields
    }
    if changes:
        audit.record_system_audit_event(
            actor=actor,
            action=AuditLog.Action.OFFICE_POLICY_UPDATED,
            description="Updated Office Policy.",
            metadata={
                "changed_fields": changed_fields,
                "changes": changes,
            },
        )
    return policy, changes


ENTRY_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}


@transaction.atomic
def set_vacancy_panel_end_users(posting, actor, end_user_ids):
    """4a: choose the end-user members who join this vacancy's panel. A member who has
    already rated one of its candidates stays on the panel (their rating counts)."""
    core = set(get_office_policy().core_panel_members.values_list("pk", flat=True))
    chosen = set(
        RecruitmentUser.objects.filter(
            pk__in=end_user_ids, is_active=True, role=RecruitmentUser.Role.HRMPSB_MEMBER
        ).exclude(pk__in=core).values_list("pk", flat=True)
    )
    raters = set(
        InterviewRating.objects.filter(interview_session__application__position=posting)
        .values_list("rated_by_id", flat=True)
    ) | set(
        InterviewFallbackRating.objects.filter(interview_session__application__position=posting)
        .values_list("rated_by_id", flat=True)
    )
    current = set(posting.panel_end_users.values_list("pk", flat=True))
    kept_raters = (current & raters) - chosen
    if kept_raters:
        names = ", ".join(
            user.get_full_name() or user.username
            for user in RecruitmentUser.objects.filter(pk__in=kept_raters)
        )
        raise ValueError(
            f"{names} already rated this vacancy's candidates, so they stay on its panel."
        )
    posting.panel_end_users.set(chosen)
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
        description=f"Set the end-user panel members for '{posting.job_code}'.",
        metadata={"entry_id": posting.id, "panel_end_user_ids": sorted(chosen)},
    )
    return chosen


ENDED_ENTRY_STATUSES = (
    PositionPosting.EntryStatus.CLOSED,
    PositionPosting.EntryStatus.CANCELLED,
)


def get_manageable_recruitment_entries(user, status_filter=None):
    """Vacancies the user manages. ``status_filter``: "open" (not closed or cancelled),
    "closed" (closed or cancelled, the past vacancies), "all", or None for the
    long-standing default of everything except closed."""
    if user.role not in ENTRY_MANAGER_ROLES:
        return PositionPosting.objects.none()
    entries = PositionPosting.objects.select_related(
        "position_reference",
        "created_by",
        "updated_by",
    ).order_by("-updated_at")
    if status_filter == "open":
        return entries.exclude(status__in=ENDED_ENTRY_STATUSES)
    if status_filter == "closed":
        return entries.filter(status__in=ENDED_ENTRY_STATUSES)
    if status_filter == "all":
        return entries
    return entries.exclude(status=PositionPosting.EntryStatus.CLOSED)


def get_public_recruitment_entries(branch=None):
    entries = PositionPosting.objects.filter(
        status=PositionPosting.EntryStatus.ACTIVE,
    ).select_related("position_reference")
    if branch:
        entries = entries.filter(branch=branch)
    return [entry for entry in entries.order_by("branch", "title") if entry.is_open_for_intake]


def persist_recruitment_entry(entry, actor, changed_fields):
    is_create = entry.pk is None
    entry.updated_by = actor
    if is_create and not entry.created_by_id:
        entry.created_by = actor
    entry.apply_position_reference_metadata()
    entry.full_clean()
    entry.save()
    if is_create and entry.branch == PositionPosting.Branch.PLANTILLA:
        # Seed the per-vacancy assessment weights with the office defaults (exam 60/40,
        # CAR 40/20/40) so they are editable from the moment the Plantilla vacancy exists.
        # COS vacancies use a single unweighted end-user exam score and have no CAR, so they
        # get no weights row.
        get_or_create_vacancy_assessment_weights(entry)
    action = (
        AuditLog.Action.RECRUITMENT_ENTRY_CREATED
        if is_create
        else AuditLog.Action.RECRUITMENT_ENTRY_UPDATED
    )
    description = (
        f"Created recruitment entry '{entry.job_code}'."
        if is_create
        else f"Updated recruitment entry '{entry.job_code}'."
    )
    audit.record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "engagement_type": entry.branch,
            "routing_basis": entry.level,
            "status": entry.status,
            "position_reference_id": entry.position_reference_id,
            "changed_fields": changed_fields,
        },
    )
    return entry


def get_or_create_vacancy_assessment_weights(posting):
    """Return the vacancy's assessment-weight row, creating it (with seeded defaults) if
    absent. Use this when a persisted, editable row is needed (seeding, the edit screen,
    locking). Pure reads for computation should use
    ``PositionPosting.assessment_weights_or_default`` instead, which never writes."""
    weights, _created = VacancyAssessmentWeights.objects.get_or_create(recruitment_entry=posting)
    return weights


def lock_vacancy_assessment_weights(posting):
    """Lock the vacancy's weights once scoring starts, so the split a finalized exam or CAR
    was computed against can no longer be edited. Mirrors lock_competency_rating_template."""
    weights = get_or_create_vacancy_assessment_weights(posting)
    if weights.status != VacancyAssessmentWeights.Status.LOCKED:
        weights.status = VacancyAssessmentWeights.Status.LOCKED
        if not weights.locked_at:
            weights.locked_at = timezone.now()
        weights.save(update_fields=["status", "locked_at", "updated_at"])
    return weights


def vacancy_assessment_weights_unlock_block_message(posting):
    if FinalSelection.objects.filter(
        recruitment_entry=posting,
        is_rescinded=False,
    ).exists():
        return "The assessment weights cannot be changed after a selection has been recorded."
    if ComparativeAssessmentReport.objects.filter(
        recruitment_entry=posting,
        is_finalized=True,
        is_returned=False,
    ).exists():
        return (
            "The assessment weights cannot be changed: a Comparative Assessment Report "
            "has been finalized on this split. Return the CAR for correction first."
        )
    return ""


@transaction.atomic
def unlock_vacancy_assessment_weights(posting, actor, reason):
    """SL-00: reopen a locked weight row so an encoding error in the split can be corrected.

    The lock exists so a finalized score cannot silently shift under its weights, and that
    stays true — unlocking is a deliberate, reason-bearing, audited act, not a quiet edit.
    The audit entry preserves the split that was in force at the moment of the unlock, so
    the record shows both what the finalized scores were computed against and why it was
    reopened. Any subsequent edit is separately audited by
    ``update_vacancy_assessment_weights``."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Record why the assessment weights are being unlocked.")
    weights = get_or_create_vacancy_assessment_weights(posting)
    if not weights.is_locked:
        raise ValidationError("These assessment weights are already editable.")
    block_message = vacancy_assessment_weights_unlock_block_message(posting)
    if block_message:
        raise ValidationError(block_message)
    locked_at = weights.locked_at
    weights.status = VacancyAssessmentWeights.Status.DRAFT
    weights.locked_at = None
    weights.updated_by = actor
    weights.save(update_fields=["status", "locked_at", "updated_by", "updated_at"])
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.ASSESSMENT_WEIGHTS_UNLOCKED,
        description=f"Unlocked assessment weights for {posting.job_code} for correction.",
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "reason": reason,
            "locked_at": locked_at.isoformat() if locked_at else "",
            "split_at_unlock": {
                "exam_general_weight": str(weights.exam_general_weight),
                "exam_technical_weight": str(weights.exam_technical_weight),
                "ete_weight": str(weights.ete_weight),
                "exam_weight": str(weights.exam_weight),
                "interview_weight": str(weights.interview_weight),
            },
        },
    )
    return weights


@transaction.atomic
def update_vacancy_assessment_weights(posting, weights_form, actor):
    """Persist edits to a Plantilla vacancy's assessment weights and audit the change.
    Refuses once the weights are locked (scoring has started). The form has already validated
    that each sub-group (exam General/Technical, CAR ETE/Exam/Interview) sums to 100%."""
    instance = weights_form.instance
    if instance.is_locked:
        raise ValidationError(
            "Assessment weights are locked because scoring has started for this vacancy."
        )

    def _snapshot(record):
        return {
            "exam_general_weight": str(record.exam_general_weight),
            "exam_technical_weight": str(record.exam_technical_weight),
            "ete_weight": str(record.ete_weight),
            "exam_weight": str(record.exam_weight),
            "interview_weight": str(record.interview_weight),
        }

    previous = (
        _snapshot(VacancyAssessmentWeights.objects.get(pk=instance.pk))
        if instance.pk
        else {}
    )
    weights = weights_form.save(commit=False)
    weights.recruitment_entry = posting
    weights.updated_by = actor
    weights.full_clean()
    weights.save()
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.ASSESSMENT_WEIGHTS_UPDATED,
        description=f"Updated assessment weights for {posting.job_code}.",
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "previous": previous,
            **_snapshot(weights),
        },
    )
    return weights


def create_default_position_document_requirements(posting):
    """Seed the branch-standard document requirement rows for a brand-new posting."""
    branch_catalog = APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(posting.branch, ())
    PositionDocumentRequirement.objects.bulk_create(
        PositionDocumentRequirement(
            posting=posting,
            document_code=requirement.code,
            is_required=requirement.is_required,
            order=index,
        )
        for index, requirement in enumerate(branch_catalog, start=1)
    )


@transaction.atomic
def set_position_document_requirements(posting, selections, actor):
    """Replace a posting's applicant document configuration.

    ``selections`` is an iterable of dicts ``{"code", "applies", "is_required"}``. Only codes
    valid for the posting's branch are honoured; the minimum required set always applies and
    stays required; conditional documents (performance rating) are stored as optional because
    their requiredness is applicant-driven.
    """
    branch_catalog = APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(posting.branch, ())
    selection_by_code = {selection["code"]: selection for selection in selections}

    desired = {}
    order_by_code = {}
    for index, requirement in enumerate(branch_catalog, start=1):
        code = requirement.code
        order_by_code[code] = index
        forced_min = code in MIN_REQUIRED_DOCUMENT_CODES
        selection = selection_by_code.get(code, {})
        applies = forced_min or bool(selection.get("applies"))
        if not applies:
            continue
        if requirement.conditional_on_performance_rating:
            is_required = False
        elif forced_min:
            is_required = True
        else:
            is_required = bool(selection.get("is_required"))
        desired[code] = is_required

    existing = {row.document_code: row for row in posting.document_requirements.all()}
    if posting.is_live_for_metadata_lock:
        if not existing:
            # Legacy postings without rows still expose the branch catalog as their
            # effective checklist. Preserve an ordinary metadata edit as a no-op; if an
            # existing catalog requirement is relaxed, materialize that effective set.
            effective_codes = {requirement.code for requirement in branch_catalog}
            if set(desired) != effective_codes:
                raise ValidationError(
                    "A live application checklist may only relax an existing required "
                    "document to optional; documents cannot be added or removed."
                )
            relaxed_codes = sorted(
                requirement.code
                for requirement in branch_catalog
                if requirement.is_required
                and requirement.code not in MIN_REQUIRED_DOCUMENT_CODES
                and not desired[requirement.code]
            )
            if not relaxed_codes:
                return
            PositionDocumentRequirement.objects.bulk_create(
                PositionDocumentRequirement(
                    posting=posting,
                    document_code=code,
                    is_required=is_required,
                    order=order_by_code[code],
                )
                for code, is_required in desired.items()
            )
            audit.record_system_audit_event(
                actor=actor,
                action=AuditLog.Action.RECRUITMENT_ENTRY_UPDATED,
                description=f"Relaxed application documents for '{posting.job_code}'.",
                metadata={
                    "entry_id": posting.id,
                    "entry_code": posting.job_code,
                    "relaxed_codes": relaxed_codes,
                    "document_codes": sorted(desired),
                    "required_codes": sorted(
                        code for code, required in desired.items() if required
                    ),
                },
            )
            return
        existing_codes = set(existing)
        desired_codes = set(desired)
        if desired_codes != existing_codes:
            raise ValidationError(
                "A live application checklist may only relax an existing required document "
                "to optional; documents cannot be added or removed."
            )
        strengthened_codes = sorted(
            code
            for code, row in existing.items()
            if not row.is_required and desired[code]
        )
        if strengthened_codes:
            raise ValidationError(
                "A live application checklist may only be relaxed; an optional document "
                "cannot be made required."
            )
        relaxed_codes = sorted(
            code
            for code, row in existing.items()
            if row.is_required and not desired[code]
        )
        if relaxed_codes:
            PositionDocumentRequirement.objects.filter(
                posting=posting,
                document_code__in=relaxed_codes,
            ).update(is_required=False, updated_at=timezone.now())
            audit.record_system_audit_event(
                actor=actor,
                action=AuditLog.Action.RECRUITMENT_ENTRY_UPDATED,
                description=f"Relaxed application documents for '{posting.job_code}'.",
                metadata={
                    "entry_id": posting.id,
                    "entry_code": posting.job_code,
                    "relaxed_codes": relaxed_codes,
                    "document_codes": sorted(desired),
                    "required_codes": sorted(
                        code for code, required in desired.items() if required
                    ),
                },
            )
        return

    for code, row in existing.items():
        if code not in desired:
            row.delete()
    for code, is_required in desired.items():
        row = existing.get(code) or PositionDocumentRequirement(
            posting=posting, document_code=code
        )
        row.is_required = is_required
        row.order = order_by_code[code]
        row.full_clean()
        row.save()

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_UPDATED,
        description=f"Configured application documents for '{posting.job_code}'.",
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "document_codes": sorted(desired),
            "required_codes": sorted(code for code, required in desired.items() if required),
        },
    )


@transaction.atomic
def cancel_recruitment_entry(entry, actor, reason):
    """SL-11: cancel a published vacancy and resolve every in-flight application.

    Neither Suspended nor Closed can say this. Suspension only pauses intake — the exam
    pool and scheduling ignore it entirely, so a cancelled recruitment kept running
    underneath. Closed is the ordinary end of the intake period and unlocks pooled
    evaluation and eventual selection. Staff had to misuse one of them, and the applicants
    were left either active forever or filed as rejected for a competition that never
    happened.

    Cancellation is one audited transition with a required reason: the posting stops
    accepting anything, every active case closes with a non-merit disposition, and each
    applicant is told the vacancy was cancelled rather than that they were not selected.

    The authorizer default is the roles that may already close a posting — flagged in the
    Resolution Log for the client to narrow if they want it held higher."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Record why this vacancy is being cancelled.")
    if entry.status == PositionPosting.EntryStatus.CANCELLED:
        raise ValidationError("This vacancy is already cancelled.")
    # SL-14: only an *effective* selection blocks cancellation — the message tells the
    # canceller to rescind first, so a rescinded row must not keep refusing them.
    if FinalSelection.objects.filter(recruitment_entry=entry, is_rescinded=False).exists():
        raise ValidationError(
            "A selection has already been recorded for this vacancy. Rescind the selection "
            "first if the appointment is not going ahead."
        )
    if CosVacancyDeliberation.objects.filter(
        recruitment_entry=entry,
        is_finalized=True,
        is_rescinded=False,
    ).exists():
        raise ValidationError(
            "A COS selection has already been recorded for this vacancy. Rescind the "
            "deliberation pick first if the engagement is not going ahead."
        )

    previous_status = entry.status
    entry.status = PositionPosting.EntryStatus.CANCELLED
    entry.updated_by = actor
    entry.save()
    entry.refresh_from_db()

    affected = list(
        RecruitmentApplication.objects.select_related("case", "position", "applicant")
        .filter(position=entry, case__isnull=False)
        .exclude(
            case__case_status__in=[
                RecruitmentCase.CaseStatus.APPROVED,
                RecruitmentCase.CaseStatus.REJECTED,
                RecruitmentCase.CaseStatus.WITHDRAWN,
                RecruitmentCase.CaseStatus.VACANCY_CANCELLED,
            ]
        )
    )
    for application in affected:
        previous_case_status = application.case.case_status
        previous_application_status = application.status
        previous_role = application.current_handler_role
        routing._sync_case_after_workflow_action(
            application=application,
            actor=actor,
            next_role="",
            next_status=RecruitmentApplication.Status.VACANCY_CANCELLED,
            remarks=reason,
        )
        application.current_handler_role = ""
        application.status = RecruitmentApplication.Status.VACANCY_CANCELLED
        application.closed_at = timezone.now()
        application.save(
            update_fields=["current_handler_role", "status", "closed_at", "updated_at"]
        )
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.RECRUITMENT_ENTRY_CANCELLED,
            description="Closed because the vacancy was cancelled.",
            metadata={
                "entry_id": entry.id,
                "entry_code": entry.job_code,
                "reason": reason,
                "from_status": previous_application_status,
                "to_status": application.status,
                "from_role": previous_role,
                "to_role": "",
                "from_case_status": previous_case_status,
                "to_case_status": application.case.case_status,
            },
        )
        queue_vacancy_cancellation_notification(application, actor=actor, reason=reason)

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_CANCELLED,
        description=f"Cancelled recruitment entry '{entry.job_code}'.",
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "reason": reason,
            "previous_status": previous_status,
            "affected_application_ids": sorted(a.id for a in affected),
            "affected_count": len(affected),
        },
    )
    return entry, affected


def entry_selection_stands(entry):
    """Chief review C3: whether this vacancy is filled for good, so re-opening its intake
    would invite applications for a decided position. An open pool hires again and again,
    so its standing picks never count here."""
    if entry.branch == PositionPosting.Branch.COS and entry.intake_mode == PositionPosting.IntakeMode.CONTINUOUS:
        return False
    if FinalSelection.objects.filter(recruitment_entry=entry, is_rescinded=False).exists():
        return True
    return CosVacancyDeliberation.objects.filter(
        recruitment_entry=entry, is_finalized=True, is_rescinded=False
    ).exists()


def update_recruitment_entry_status(entry, actor, new_status, reason=""):
    if entry.status == PositionPosting.EntryStatus.CANCELLED:
        raise ValidationError(
            "This vacancy was cancelled and cannot be reactivated, suspended, or "
            "closed. Publish a new vacancy instead."
        )
    if new_status == PositionPosting.EntryStatus.ACTIVE and entry_selection_stands(entry):
        raise ValidationError(
            "A selection has already been recorded for this vacancy, so it can't take new "
            "applications. Rescind the selection first if the appointment is not going ahead."
        )
    if (
        new_status == PositionPosting.EntryStatus.ACTIVE
        and entry.branch == PositionPosting.Branch.PLANTILLA
        and entry.missing_qualification_standards
    ):
        raise ValidationError(
            "Add the Qualification Standards before this vacancy goes Active (missing: "
            f"{', '.join(entry.missing_qualification_standards)}). Edit the entry and write "
            "them as \"Education: …\", \"Training: …\", \"Experience: …\"."
        )
    # WF-07 (HARD): the publication period is a promise to applicants — the vacancy
    # cannot be closed before its closing date. An early close would also auto-finalize
    # the applicant pool and unlock deliberation/CAR for an incomplete pool. Suspend
    # remains available as the emergency valve.
    if (
        new_status == PositionPosting.EntryStatus.CLOSED
        and entry.close_blocked_until is not None
    ):
        raise ValidationError(
            f"This vacancy's publication period runs until "
            f"{entry.close_blocked_until.strftime('%B %d, %Y')}. It can't be closed "
            "before then. Use Suspend if applications must stop for an exceptional reason."
        )
    reason = (reason or "").strip()
    if (
        new_status
        in {
            PositionPosting.EntryStatus.SUSPENDED,
            PositionPosting.EntryStatus.CLOSED,
        }
        and get_office_policy().require_reason_for_suspend_close
        and not reason
    ):
        raise ValidationError("Record why this vacancy is being suspended or closed.")
    previous_status = entry.status
    previous_closing_date = entry.closing_date
    previous_updated_by = entry.updated_by
    entry.status = new_status
    entry.updated_by = actor
    try:
        entry.full_clean()
    except ValidationError:
        entry.status = previous_status
        entry.closing_date = previous_closing_date
        entry.updated_by = previous_updated_by
        raise
    entry.save(update_fields=["status", "closing_date", "updated_by", "is_active", "updated_at"])
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
        description=f"Changed recruitment entry '{entry.job_code}' status from {previous_status} to {new_status}.",
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "old_status": previous_status,
            "new_status": new_status,
            "reason": reason,
        },
    )
    return entry
