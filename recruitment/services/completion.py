"""Completion: requirements, completion tracking and closing a case.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    CompletionRecord,
    CompletionRequirement,
    CompletionDeadlineChange,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RoutingHistory,
)
from . import access, audit, common, routing


COMPLETION_REVIEW_ROLES = common.SCREENING_REVIEW_ROLES
COMPLETION_STAGES = {
    RecruitmentCase.Stage.COMPLETION,
}


def get_completion_record(application):
    return CompletionRecord.objects.select_related(
        "tracked_by",
        "recruitment_case",
    ).filter(application=application).first()


def get_completion_requirements(application):
    completion_record = get_completion_record(application)
    if not completion_record:
        return CompletionRequirement.objects.none()
    return completion_record.requirements.all()


def user_can_manage_completion(user, application):
    current_stage = routing.get_current_review_stage(application)
    if (
        user.role not in COMPLETION_REVIEW_ROLES
        or current_stage not in COMPLETION_STAGES
        or application.status != RecruitmentApplication.Status.APPROVED
        or routing.get_current_workflow_section(application) != "completion"
    ):
        return False
    return access.user_can_process_application(user, application)


def user_can_close_case(user, application):
    completion_record = get_completion_record(application)
    return bool(
        completion_record
        and not completion_record.is_voided
        and user_can_manage_completion(user, application)
        and completion_record.ready_for_closure
    )


@transaction.atomic
@common.locks_workflow_row("case")
def save_completion_tracking(application, actor, cleaned_data, requirement_formset):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before completion details can be recorded.")
    if not user_can_manage_completion(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for completion details."
        )

    completion_record = get_completion_record(application)
    created = completion_record is None
    if completion_record is not None and completion_record.is_voided:
        raise ValueError(
            "This completion record was voided by rescission. Record a new selection "
            "before starting completion tracking again."
        )
    if completion_record is None:
        completion_record = CompletionRecord(
            application=application,
            recruitment_case=application.case,
            tracked_by=actor,
        )

    # SL-13: capture the deadline as it stands BEFORE this save, so an extension can be
    # recorded as its own audited fact rather than silently replacing the original.
    previous_deadline = None if created else completion_record.deadline
    submitted_deadline = cleaned_data.get("deadline")
    deadline_changed = not created and submitted_deadline != previous_deadline
    deadline_change_reason = (cleaned_data.get("deadline_change_reason") or "").strip()
    if deadline_changed and not deadline_change_reason:
        raise ValueError("Record why the completion deadline is being changed.")

    completion_record.recruitment_case = application.case
    completion_record.tracked_by = actor
    completion_record.branch = application.branch
    completion_record.level = application.level
    completion_record.completion_reference = cleaned_data.get("completion_reference", "")
    completion_record.completion_date = cleaned_data.get("completion_date")
    completion_record.deadline = cleaned_data.get("deadline")
    completion_record.remarks = cleaned_data.get("remarks", "")
    if application.branch == PositionPosting.Branch.PLANTILLA:
        completion_record.announcement_reference = cleaned_data.get("announcement_reference", "")
        completion_record.announcement_date = cleaned_data.get("announcement_date")
    else:
        completion_record.announcement_reference = ""
        completion_record.announcement_date = None
    completion_record.full_clean()
    completion_record.save()

    if deadline_changed:
        # SL-13: an immutable extension trail. The original deadline survives every later
        # edit, which is what makes "this was completed late" provable afterwards.
        change = CompletionDeadlineChange(
            completion_record=completion_record,
            previous_deadline=previous_deadline,
            new_deadline=submitted_deadline,
            changed_by=actor,
            reason=deadline_change_reason,
        )
        change.full_clean()
        change.save()
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.COMPLETION_DEADLINE_CHANGED,
            description=(
                "Extended the completion deadline."
                if change.is_extension
                else "Changed the completion deadline."
            ),
            metadata={
                "completion_record_id": completion_record.id,
                "previous_deadline": (
                    previous_deadline.isoformat() if previous_deadline else ""
                ),
                "new_deadline": (
                    submitted_deadline.isoformat() if submitted_deadline else ""
                ),
                "is_extension": change.is_extension,
                "reason": deadline_change_reason,
            },
        )

    active_requirement_ids = []
    requirement_count = 0
    resolved_requirement_count = 0
    for form in requirement_formset.forms:
        if not hasattr(form, "cleaned_data") or not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            if form.instance.pk:
                form.instance.delete()
            continue
        item_label = (form.cleaned_data.get("item_label") or "").strip()
        if not item_label:
            continue

        requirement = form.save(commit=False)
        requirement.completion_record = completion_record
        requirement.display_order = requirement_count
        requirement.full_clean()
        requirement.save()
        active_requirement_ids.append(requirement.pk)
        requirement_count += 1
        if requirement.status != CompletionRequirement.RequirementStatus.PENDING:
            resolved_requirement_count += 1

    completion_record.requirements.exclude(pk__in=active_requirement_ids).delete()

    if requirement_count == 0:
        raise ValidationError("Add at least one completion requirement item.")

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.COMPLETION_RECORDED,
        description=(
            "Saved appointment completion tracking."
            if application.branch == PositionPosting.Branch.PLANTILLA
            else "Saved contract completion tracking."
        ),
        metadata={
            "completion_record_id": completion_record.id,
            "created": created,
            "case_stage": application.case.current_stage,
            "case_status": application.case.case_status,
            "deadline": completion_record.deadline.isoformat() if completion_record.deadline else "",
            "completion_reference": completion_record.completion_reference,
            "completion_date": (
                completion_record.completion_date.isoformat()
                if completion_record.completion_date
                else ""
            ),
            "announcement_reference": completion_record.announcement_reference,
            "announcement_date": (
                completion_record.announcement_date.isoformat()
                if completion_record.announcement_date
                else ""
            ),
            "requirement_count": requirement_count,
            "resolved_requirement_count": resolved_requirement_count,
        },
    )
    return completion_record


@transaction.atomic
@common.locks_workflow_row("case")
def close_recruitment_case(application, actor, closure_notes):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before it can be closed.")
    if not user_can_manage_completion(actor, application):
        raise ValueError("This case is not currently assigned to you for closure.")

    case = application.case
    if case.current_stage != RecruitmentCase.Stage.COMPLETION:
        raise ValueError("Case closure is available only from the completion step.")

    completion_record = get_completion_record(application)
    if not completion_record:
        raise ValueError("Record completion tracking before closing the recruitment case.")
    if completion_record.is_voided:
        raise ValueError(
            "This completion record was voided by rescission. Record fresh completion "
            "tracking after a new selection."
        )
    if not completion_record.requirements.exists():
        raise ValueError("Add at least one completion requirement item before closing the case.")
    if completion_record.has_pending_requirements:
        raise ValueError(
            "All completion requirements must be marked completed or not applicable before closing the case."
        )
    completion_label = completion_record.completion_label.lower()
    if not completion_record.has_completion_reference_for_closure:
        raise ValueError(f"Record the {completion_label} reference before closing the case.")
    if not completion_record.has_completion_date_for_closure:
        raise ValueError(f"Record the {completion_label} date before closing the case.")

    previous_role = application.current_handler_role
    previous_stage = case.current_stage
    previous_case_status = case.case_status
    closed_at = timezone.now()

    routing._transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
    case.case_status = RecruitmentCase.CaseStatus.APPROVED
    case.current_handler_role = ""
    case.is_stage_locked = True
    case.locked_stage = RecruitmentCase.Stage.COMPLETION
    case.closed_at = closed_at
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "stage_entered_at",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "updated_at",
        ]
    )

    application.current_handler_role = ""
    application.closed_at = closed_at
    application.save(update_fields=["current_handler_role", "closed_at", "updated_at"])

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_CLOSED,
        description="Closed the recruitment case after completion tracking.",
        metadata={
            "remarks": closure_notes,
            "from_stage": previous_stage,
            "to_stage": case.current_stage,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
            "completion_record_id": completion_record.id,
            "requirement_count": completion_record.total_requirement_count,
            "resolved_requirement_count": completion_record.completed_requirement_count,
        },
    )
    routing.record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.CLOSE,
        description="Recruitment case closed after completion tracking.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role="",
        from_status=application.status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=closure_notes,
    )
    return case
