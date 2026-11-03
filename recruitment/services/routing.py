"""How a case moves: routing history, level ownership, Secretariat and HRM Chief
handoffs, batch handoff and recall, auto-advance, workflow sections, available
actions and the generic workflow action processor.
"""

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    Notification,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    WorkflowOverride,
)
from ..notification_services import queue_non_selected_applicant_notification
from . import (
    access,
    audit,
    car,
    case_notices,
    common,
    exam,
    intake,
    interview,
    screening,
    selection,
    workflow_map,
)


PENDING_RESUBMISSION_HANDOFF_MESSAGE = (
    "This case is waiting for the applicant's resubmission. Wait for it, or remove the "
    "applicant from the batch, before sending the case."
)
COMPLETION_HANDOFF_MESSAGE = (
    "Completion is handled by the case's designated office and cannot be sent between "
    "offices."
)
WORKFLOW_SECTION_LABELS = workflow_map.SECTION_LABELS


def record_routing_history_event(
    application,
    actor,
    route_type,
    description,
    *,
    recruitment_case=None,
    from_handler_role="",
    to_handler_role="",
    from_status="",
    to_status="",
    from_stage="",
    to_stage="",
    notes="",
    is_override=False,
):
    return RoutingHistory.objects.create(
        application=application,
        recruitment_case=recruitment_case,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        branch=application.branch,
        level=application.level,
        route_type=route_type,
        from_handler_role=from_handler_role,
        to_handler_role=to_handler_role,
        from_status=from_status,
        to_status=to_status,
        from_stage=from_stage,
        to_stage=to_stage,
        description=description,
        notes=notes,
        is_override=is_override,
    )


def level_owner_role(application):
    """The office that owns a case by the routing rule: Level 1 the Secretariat, Level 2 the
    HRM Chief (the owner of the stage the case started in)."""
    return workflow_map.stage_owner(
        workflow_map.intake_stage(application.branch, application.level), application.level
    )


LOANABLE_CASE_STATUSES = {
    RecruitmentCase.CaseStatus.ACTIVE,
    RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
}


def case_is_on_loan(application):
    """C4: the owning office lent this case (with its batch) to the other office, which now
    holds it at the intake stage."""
    case = getattr(application, "case", None)
    if not case or case.is_stage_locked or case.case_status not in LOANABLE_CASE_STATUSES:
        return False
    if case.current_stage != screening._vacancy_screening_stage(application.position):
        return False
    return bool(case.current_handler_role) and case.current_handler_role != level_owner_role(application)


def get_loaned_out_applications(user):
    """The cases this office lent to the other office and may recall (C4)."""
    if user.role == RecruitmentUser.Role.SECRETARIAT:
        level, stage = PositionPosting.Level.LEVEL_1, RecruitmentCase.Stage.SECRETARIAT_REVIEW
    elif user.role == RecruitmentUser.Role.HRM_CHIEF:
        level, stage = PositionPosting.Level.LEVEL_2, RecruitmentCase.Stage.HRM_CHIEF_REVIEW
    else:
        return RecruitmentApplication.objects.none()
    return (
        RecruitmentApplication.objects.filter(
            level=level,
            case__current_stage=stage,
            case__is_stage_locked=False,
            case__case_status__in=LOANABLE_CASE_STATUSES,
        )
        .exclude(case__current_handler_role__in=[user.role, ""])
        .select_related("position", "case")
        .order_by("reference_number")
    )


def _batch_cases_held_by(posting, actor):
    """The cases of this vacancy the office holds at the vacancy's intake stage (screening and
    exam), the only stage a batch is lent at. A Level 1 COS case also sits at the HRM Chief's
    review stage for the Chief's own re-check, interview and selection; those are the Chief's
    steps, not a lendable batch, and sending them would strand the case (it would not count as
    a loan, so it could not be recalled)."""
    intake_stage = screening._vacancy_screening_stage(posting)
    return [
        application
        for application in RecruitmentApplication.objects.filter(
            position=posting, current_handler_role=actor.role
        ).select_related("case", "position")
        if getattr(application, "case", None)
        and not application.case.is_stage_locked
        and application.case.case_status in LOANABLE_CASE_STATUSES
        and application.case.current_stage == intake_stage
    ]


def user_can_send_vacancy_batch(user, posting):
    return user.role in {
        RecruitmentUser.Role.SECRETARIAT,
        RecruitmentUser.Role.HRM_CHIEF,
    } and bool(_batch_cases_held_by(posting, user))


@transaction.atomic
@common.locks_workflow_row("vacancy")
def hand_off_vacancy_batch(posting, actor, remarks):
    """C4 + F9 (office decision 2026-11-02): the Secretariat and the HRM Chief lend each other
    a vacancy's whole batch, never one case of it, so the batch exam and interview are never
    split between two offices. Every case the office holds at screening/exam moves together."""
    remarks = (remarks or "").strip()
    if not remarks:
        raise ValueError("Add a short reason before sending the batch.")
    if actor.role not in {RecruitmentUser.Role.SECRETARIAT, RecruitmentUser.Role.HRM_CHIEF}:
        raise ValueError("Only the Secretariat and the HRM Chief lend batches to each other.")
    held = _batch_cases_held_by(posting, actor)
    if not held:
        raise ValueError("You hold no cases in this vacancy's batch right now.")
    waiting = [
        application.reference_label
        for application in held
        if application.case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
    ]
    if waiting:
        raise ValueError(
            f"{', '.join(waiting)} {'is' if len(waiting) == 1 else 'are'} waiting for corrected "
            "documents. Send the batch once they're back, so it stays together."
        )
    target = (
        RecruitmentUser.Role.HRM_CHIEF
        if actor.role == RecruitmentUser.Role.SECRETARIAT
        else RecruitmentUser.Role.SECRETARIAT
    )
    for application in held:
        route_case_between_secretariat_and_hrm_chief(application, actor, target, remarks)
    return held


@transaction.atomic
@common.locks_workflow_row("vacancy")
def recall_vacancy_batch(posting, actor, remarks):
    """C4: the office that lent a batch takes it back, whatever step its cases reached."""
    remarks = (remarks or "").strip()
    if not remarks:
        raise ValueError("Add a short reason before recalling the batch.")
    loaned = list(get_loaned_out_applications(actor).filter(position=posting))
    if not loaned:
        raise ValueError("No cases of this vacancy are on loan from you.")
    list(
        RecruitmentCase.objects.select_for_update()
        .filter(application__in=loaned)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    for application in loaned:
        application.refresh_from_db()
        case = application.case
        previous_role = case.current_handler_role
        if application.level == PositionPosting.Level.LEVEL_2:
            application.overrides.filter(is_active=True).update(
                is_active=False, revoked_at=timezone.now()
            )
        description = f"Batch recalled by the {RecruitmentUser.Role(actor.role).label}."
        # The case keeps its step (and a pending resubmission stays pending); only the
        # handler changes back to the office that lent it.
        transition_case(
            application,
            actor,
            to_status=application.status,
            to_role=actor.role,
            route_type=RoutingHistory.RouteType.FORWARD,
            description=description,
            notes=remarks,
        )
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=description,
            metadata={"remarks": remarks, "from_role": previous_role, "to_role": actor.role, "recall": True},
        )
        case_notices._emit_case_assignment_notification(
            application,
            actor,
            previous_role,
            kind=Notification.Kind.CASE_RETURNED,
            title=f"{application.reference_label} recalled by {case_notices._actor_display_name(actor)}",
            body=remarks,
        )
    return loaned


def _handoff_target_status(application):
    return application.status


def _is_secretariat_hrm_handoff_role(role):
    return role in {
        RecruitmentUser.Role.SECRETARIAT,
        RecruitmentUser.Role.HRM_CHIEF,
    }


def get_case_handoff_options(application, user):
    if not hasattr(application, "case"):
        return []
    case = application.case
    if case.is_stage_locked or case.current_stage in {
        RecruitmentCase.Stage.COMPLETION,
        RecruitmentCase.Stage.CLOSED,
    }:
        return []
    if not _is_secretariat_hrm_handoff_role(user.role):
        return []
    if application.current_handler_role != user.role or case.current_handler_role != user.role:
        return []
    if case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
        return []
    if (
        application.level == PositionPosting.Level.LEVEL_2
        and case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW
    ):
        return []
    if not access.user_can_process_application(user, application):
        return []

    if user.role == RecruitmentUser.Role.SECRETARIAT:
        return [(RecruitmentUser.Role.HRM_CHIEF, "Send to HRM Chief")]
    if user.role == RecruitmentUser.Role.HRM_CHIEF:
        return [(RecruitmentUser.Role.SECRETARIAT, "Send to Secretariat")]
    return []


def _case_handoff_description(actor_role, target_role, application):
    if (
        actor_role == RecruitmentUser.Role.HRM_CHIEF
        and target_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return "Level 2 case sent to Secretariat by HRM Chief."
    if target_role == RecruitmentUser.Role.SECRETARIAT:
        return "Case sent to Secretariat."
    return "Case sent to HRM Chief."


@transaction.atomic
def route_case_between_secretariat_and_hrm_chief(application, actor, target_role, remarks):
    if target_role not in {
        RecruitmentUser.Role.SECRETARIAT,
        RecruitmentUser.Role.HRM_CHIEF,
    }:
        raise ValueError("Cases can only be sent between Secretariat and HRM Chief.")
    if actor.role == target_role:
        raise ValueError("Select the other office.")
    if not _is_secretariat_hrm_handoff_role(actor.role):
        raise ValueError("Only Secretariat and HRM Chief can send cases to each other.")
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before it can be sent.")
    case = application.case
    if case.current_stage == RecruitmentCase.Stage.COMPLETION:
        raise ValueError(COMPLETION_HANDOFF_MESSAGE)
    if case.is_stage_locked or case.current_stage == RecruitmentCase.Stage.CLOSED:
        raise ValueError("Final or closed cases cannot be sent.")
    if application.current_handler_role != actor.role or case.current_handler_role != actor.role:
        raise ValueError("Only the assigned office can send this case.")
    if case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
        raise ValueError(PENDING_RESUBMISSION_HANDOFF_MESSAGE)
    if not access.user_can_process_application(actor, application):
        if (
            actor.role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat."
            )
        raise ValueError("This case cannot be sent at the current step.")

    active_override = application.active_secretariat_override
    override = None
    is_level2_secretariat_handoff = (
        application.level == PositionPosting.Level.LEVEL_2
        and actor.role == RecruitmentUser.Role.HRM_CHIEF
        and target_role == RecruitmentUser.Role.SECRETARIAT
    )
    is_level2_return_to_hrm_chief = (
        application.level == PositionPosting.Level.LEVEL_2
        and actor.role == RecruitmentUser.Role.SECRETARIAT
        and target_role == RecruitmentUser.Role.HRM_CHIEF
    )

    if application.level == PositionPosting.Level.LEVEL_2 and target_role == RecruitmentUser.Role.SECRETARIAT:
        if actor.role != RecruitmentUser.Role.HRM_CHIEF:
            raise ValueError("Only HRM Chief can send a Level 2 case to Secretariat.")
        if case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
            raise ValueError(
                "A Level 2 case can be sent to Secretariat only while it is assigned to HRM Chief review."
            )
        application.overrides.filter(is_active=True).update(
            is_active=False,
            revoked_at=timezone.now(),
        )
        override = WorkflowOverride.objects.create(
            application=application,
            granted_by=actor,
            target_role=RecruitmentUser.Role.SECRETARIAT,
            reason=remarks,
        )
    elif is_level2_return_to_hrm_chief:
        if not active_override:
            raise ValueError("This Level 2 case can return to HRM Chief only after HRM Chief first sent it to Secretariat.")

    previous_role = application.current_handler_role
    previous_status = application.status
    previous_stage = case.current_stage
    previous_case_status = case.case_status

    description = _case_handoff_description(actor.role, target_role, application)
    transition_case(
        application,
        actor,
        to_status=_handoff_target_status(application),
        to_role=target_role,
        route_type=(
            RoutingHistory.RouteType.OVERRIDE
            if is_level2_secretariat_handoff
            else RoutingHistory.RouteType.FORWARD
        ),
        description=description,
        notes=remarks,
        is_override=is_level2_secretariat_handoff,
    )
    if override:
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.OVERRIDE_GRANTED,
            description="HRM Chief handed off a Level 2 case to Secretariat.",
            metadata={
                "override_id": override.id,
                "reason": remarks,
                **_case_timeline_metadata(case),
            },
        )

    if is_level2_return_to_hrm_chief and active_override:
        active_override.mark_used()
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.OVERRIDE_USED,
            description="Level 2 case returned from Secretariat to HRM Chief.",
            metadata={"override_id": active_override.id},
        )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=description,
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": application.status,
            "from_role": previous_role,
            "to_role": target_role,
            "from_stage": previous_stage,
            "to_stage": case.current_stage,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
            "case_locked": case.is_stage_locked,
        },
    )
    case_notices._emit_case_assignment_notification(
        application,
        actor,
        target_role,
        kind=(
            Notification.Kind.CASE_RETURNED
            if is_level2_return_to_hrm_chief
            else Notification.Kind.CASE_ASSIGNED
        ),
        title=f"{application.reference_label} handed off to you by {case_notices._actor_display_name(actor)}",
        body=(remarks or "").strip(),
    )
    return application


def _consume_active_secretariat_handoff(application, actor, description):
    if application.level != PositionPosting.Level.LEVEL_2:
        return
    override = application.active_secretariat_override
    if not override:
        return
    override.mark_used()
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.OVERRIDE_USED,
        description=description,
        metadata={"override_id": override.id},
    )


def _auto_route_application(application, actor, *, next_role, next_status, description, remarks):
    if not hasattr(application, "case"):
        return False
    if (
        application.current_handler_role == next_role
        and application.status == next_status
        and application.case.current_handler_role == next_role
    ):
        return False

    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = transition_case(
        application,
        actor,
        to_status=next_status,
        to_role=next_role,
        route_type=RoutingHistory.RouteType.FORWARD,
        description=description,
        notes=remarks,
    )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=description,
        metadata={
            "auto_routed": True,
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
        },
    )
    case_notices._emit_case_assignment_notification(
        application,
        actor,
        next_role,
        title=f"{application.reference_label} is now in your queue",
        body=description,
    )
    if previous_role == RecruitmentUser.Role.SECRETARIAT:
        _consume_active_secretariat_handoff(
            application,
            actor,
            "Level 2 Secretariat authorization ended after automatic case advancement.",
        )
    return True


def _auto_advance_after_exam_finalized(application, actor, review_stage):
    if application.branch == PositionPosting.Branch.PLANTILLA and review_stage in common.SCREENING_STAGES:
        if (
            review_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
            and application.current_handler_role == RecruitmentUser.Role.SECRETARIAT
        ):
            returned_to_hrm_chief = _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically returned to HRM Chief after Plantilla examination was finalized by Secretariat.",
                remarks="Plantilla examination finalized by Secretariat; next assigned office is HRM Chief.",
            )
            if not returned_to_hrm_chief:
                return False
            onward_description = (
                "Automatically routed to HRMPSB after Plantilla examination was finalized "
                "by Secretariat under HRM Chief authorization."
            )
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRMPSB_MEMBER,
                next_status=RecruitmentApplication.Status.HRMPSB_REVIEW,
                description=onward_description,
                remarks=onward_description,
            )
        return _auto_route_application(
            application,
            actor,
            next_role=RecruitmentUser.Role.HRMPSB_MEMBER,
            next_status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            description="Automatically routed to HRMPSB after examination finalization.",
            remarks="Examination finalized; next designated handler is HRMPSB.",
        )
    if application.branch == PositionPosting.Branch.COS:
        if review_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically routed to HRM Chief after COS examination finalization.",
                remarks="COS examination finalized; next designated handler is HRM Chief.",
            )
        if (
            review_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
            and application.current_handler_role == RecruitmentUser.Role.SECRETARIAT
        ):
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically returned to HRM Chief after COS examination was finalized by Secretariat.",
                remarks="COS examination finalized by Secretariat; next assigned office is HRM Chief.",
            )
    return False


def _auto_advance_after_car_finalized(report, actor):
    if not report.is_finalized:
        return []
    routed_applications = []
    report_items = report.items.select_related(
        "recruitment_case",
        "recruitment_case__application",
    )
    for item in report_items:
        application = item.recruitment_case.application
        if (
            application.branch != PositionPosting.Branch.PLANTILLA
            or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
            or application.case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
            or application.current_handler_role != RecruitmentUser.Role.HRMPSB_MEMBER
        ):
            continue
        routed = _auto_route_application(
            application,
            actor,
            next_role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
            next_status=RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
            description="Automatically routed to Appointing Authority after CAR finalization.",
            remarks=f"{common.CAR_LABEL} finalized; next designated handler is Appointing Authority.",
        )
        if routed:
            routed_applications.append(application)
    return routed_applications


def _auto_advance_boundary_is_ready(application, current_stage=None):
    current_stage = current_stage or get_current_review_stage(application)
    if current_stage in common.SCREENING_STAGES:
        if not exam.exam_is_finalized_for_current_stage(application):
            return False
        if application.branch == PositionPosting.Branch.PLANTILLA:
            return True
        if application.branch == PositionPosting.Branch.COS:
            return current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        report = car.get_comparative_assessment_report(application, stage=current_stage)
        return bool(report and report.is_finalized)
    return False


@transaction.atomic
def repair_auto_advance_workflow_boundaries(actor=None):
    repaired = []
    applications = RecruitmentApplication.objects.select_related(
        "applicant",
        "position",
        "case",
    ).filter(
        case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
        case__is_stage_locked=False,
    ).order_by("id")

    processed_reports = set()
    for application in applications:
        if not hasattr(application, "case"):
            continue
        current_stage = application.case.current_stage
        if not _auto_advance_boundary_is_ready(application, current_stage):
            continue

        before_role = application.current_handler_role
        before_status = application.status
        before_stage = application.case.current_stage
        if current_stage in common.SCREENING_STAGES:
            routed = _auto_advance_after_exam_finalized(application, actor, current_stage)
            if routed:
                repaired.append(
                    {
                        "application_id": application.id,
                        "reference": application.reference_label,
                        "from_role": before_role,
                        "to_role": application.current_handler_role,
                        "from_status": before_status,
                        "to_status": application.status,
                        "from_stage": before_stage,
                        "to_stage": application.case.current_stage,
                        "reason": "exam_finalized_boundary",
                    }
                )
            continue

        report = car.get_comparative_assessment_report(application, stage=current_stage)
        if not report or not report.is_finalized or report.id in processed_reports:
            continue
        processed_reports.add(report.id)
        routed_applications = _auto_advance_after_car_finalized(report, actor)
        for routed_application in routed_applications:
            repaired.append(
                {
                    "application_id": routed_application.id,
                    "reference": routed_application.reference_label,
                    "from_role": RecruitmentUser.Role.HRMPSB_MEMBER,
                    "to_role": routed_application.current_handler_role,
                    "from_status": RecruitmentApplication.Status.HRMPSB_REVIEW,
                    "to_status": routed_application.status,
                    "from_stage": RecruitmentCase.Stage.HRMPSB_REVIEW,
                    "to_stage": routed_application.case.current_stage,
                    "reason": "car_finalized_boundary",
                }
            )
    return repaired


def _review_stage_from_application_status(application_status):
    return workflow_map.review_stage_for_status(application_status)


def _application_status_from_stage(stage):
    return workflow_map.status_for_stage(stage)


def _completion_handler_role(application):
    return workflow_map.stage_owner(RecruitmentCase.Stage.COMPLETION, application.level)


def _handler_role_from_stage(stage, application=None):
    return workflow_map.stage_owner(stage, application.level if application is not None else None)


def _case_timeline_metadata(case):
    return {
        "case_stage": case.current_stage,
        "stage_entered_at": case.stage_entered_at.isoformat() if case.stage_entered_at else "",
        "case_status": case.case_status,
        "case_handler_role": case.current_handler_role,
        "case_locked": case.is_stage_locked,
    }


def _ensure_case_stage_alignment(application, case):
    expected_stage = _review_stage_from_application_status(application.status)
    if expected_stage and case.current_stage != expected_stage:
        raise ValueError("The recruitment case is out of sync with the application workflow state.")


def _transition_case_stage(case, new_stage, *, force=False):
    if not new_stage:
        return False
    if force or case.current_stage != new_stage:
        case.current_stage = new_stage
        case.stage_entered_at = timezone.now()
        if new_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
            # Entering panel evaluation: assign the blinding alias evaluator
            # surfaces identify the candidate by (REV-B).
            intake.assign_candidate_code(case.application)
        return True
    return False


def get_case_timeline(application):
    return application.audit_logs.filter(
        action__in=[
            AuditLog.Action.APPLICATION_SUBMITTED,
            AuditLog.Action.CASE_CREATED,
            AuditLog.Action.CASE_REOPENED,
            AuditLog.Action.ROUTED,
            AuditLog.Action.SCREENING_RECORDED,
            AuditLog.Action.SCREENING_FINALIZED,
            AuditLog.Action.EXAM_RECORDED,
            AuditLog.Action.EXAM_FINALIZED,
            AuditLog.Action.INTERVIEW_SCHEDULED,
            AuditLog.Action.INTERVIEW_FINALIZED,
            AuditLog.Action.INTERVIEW_RATING_RECORDED,
            AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
            AuditLog.Action.DELIBERATION_RECORDED,
            AuditLog.Action.DELIBERATION_FINALIZED,
            AuditLog.Action.CAR_GENERATED,
            AuditLog.Action.CAR_FINALIZED,
            AuditLog.Action.CAR_RETURNED,
            AuditLog.Action.DECISION_RECORDED,
            AuditLog.Action.COMPLETION_RECORDED,
            AuditLog.Action.CASE_CLOSED,
            AuditLog.Action.NOTIFICATION_SENT,
            AuditLog.Action.NOTIFICATION_FAILED,
            AuditLog.Action.OVERRIDE_GRANTED,
            AuditLog.Action.OVERRIDE_USED,
            AuditLog.Action.EVIDENCE_UPLOADED,
            AuditLog.Action.EVIDENCE_DOWNLOADED,
            AuditLog.Action.EVIDENCE_ARCHIVED,
            AuditLog.Action.EVIDENCE_RESTORED,
            AuditLog.Action.EXPORT_GENERATED,
        ]
    ).order_by("created_at")


def get_current_review_stage(application):
    case = getattr(application, "case", None)
    if case:
        return case.current_stage
    return _review_stage_from_application_status(application.status)


def _workflow_detail_sequence(application):
    return workflow_map.section_sequence(
        application.branch, get_current_review_stage(application)
    ) or ["overview"]


def _workflow_section_is_complete(application, section_key):
    if section_key == "screening":
        return screening.screening_is_finalized_for_current_stage(application)
    if section_key == "exam":
        return exam.exam_is_finalized_for_current_stage(application)
    if section_key == "interview":
        return interview.interview_is_finalized_for_current_stage(application)
    if section_key == "deliberation":
        return selection.deliberation_is_finalized_for_current_stage(application)
    if section_key == "car":
        return car.car_is_finalized_for_current_stage(application)
    return True


def get_current_workflow_section(application):
    sequence = _workflow_detail_sequence(application)
    for section_key in sequence:
        if not _workflow_section_is_complete(application, section_key):
            return section_key
    if sequence:
        return sequence[-1]
    return "overview"


def get_current_workflow_section_label(application):
    return WORKFLOW_SECTION_LABELS.get(get_current_workflow_section(application), "Overview")


def get_application_detail_tab(application):
    section_key = get_current_workflow_section(application)
    return {
        "key": section_key,
        "label": WORKFLOW_SECTION_LABELS.get(section_key, "Overview"),
        "section_id": f"cws-{section_key}",
    }


def _workflow_progress_block_message(application, section_key=None):
    section_key = section_key or get_current_workflow_section(application)
    if section_key == "screening":
        return "Finalize the screening record before endorsing this application."
    if section_key == "exam":
        return "Finalize the examination record before proceeding to the next workflow task."
    if section_key == "interview":
        return "Finalize the interview task before proceeding to the next workflow task."
    if section_key == "car":
        if (
            screening.application_requires_finalized_applicant_pool(application)
            and not screening.application_has_finalized_applicant_pool(application)
        ):
            return screening.get_applicant_pool_finalization_block_message(application)
        return "Finalize the Comparative Assessment Report before recommending this Plantilla application."
    if section_key == "deliberation":
        return "Finalize the deliberation record before endorsing this COS application."
    return "Complete the current workflow task before proceeding."


@common.locks_workflow_row("case", refresh=False)
def transition_case(
    application,
    actor,
    *,
    to_status,
    to_role,
    route_type,
    description,
    notes="",
    is_override=False,
    restart_stage_clock=False,
):
    """The only way a case changes stage, case status or handler (F15 round 2).

    Locks the case row, derives the case's stage and status from the application status
    it is moving to, writes the case and the application's status, handler and closed time,
    and records one RoutingHistory row (``route_type`` and ``description`` are required, so
    every movement shows in the case's routing history and the export). The caller records
    its own audit event (the action and metadata differ by step) from the returned
    before-state. ``restart_stage_clock`` restarts the stage's SLA clock when the stage
    itself does not change (an applicant's resubmission).
    """
    case = application.case
    _ensure_case_stage_alignment(application, case)
    previous_stage = case.current_stage
    previous_case_status = case.case_status
    previous_role = application.current_handler_role
    previous_status = application.status

    next_stage = _review_stage_from_application_status(to_status)
    if next_stage:
        _transition_case_stage(case, next_stage)
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.current_handler_role = to_role
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
    elif to_status == RecruitmentApplication.Status.RETURNED_TO_APPLICANT:
        # A resubmission request: the case keeps its stage and reviewing handler but waits
        # for the applicant, which pauses the stage clock and flags it in the batch console.
        case.case_status = RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        case.current_handler_role = to_role
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
    elif to_status == RecruitmentApplication.Status.APPROVED:
        if to_role:
            _transition_case_stage(case, RecruitmentCase.Stage.COMPLETION)
            case.case_status = RecruitmentCase.CaseStatus.ACTIVE
            case.current_handler_role = to_role
            case.is_stage_locked = False
            case.locked_stage = ""
            case.closed_at = None
        else:
            _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
            case.case_status = RecruitmentCase.CaseStatus.APPROVED
            case.current_handler_role = ""
            case.is_stage_locked = True
            case.locked_stage = previous_stage
            case.closed_at = timezone.now()
    elif to_status == RecruitmentApplication.Status.REJECTED:
        _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
        case.case_status = RecruitmentCase.CaseStatus.REJECTED
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    elif to_status == RecruitmentApplication.Status.VACANCY_CANCELLED:
        # SL-11: terminal, and explicitly not a merit outcome.
        _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
        case.case_status = RecruitmentCase.CaseStatus.VACANCY_CANCELLED
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    elif to_status == RecruitmentApplication.Status.WITHDRAWN:
        # SL-10: terminal like a rejection in mechanics, distinct from it in meaning. The
        # case leaves every active pool but the record never says the office turned this
        # person down.
        _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
        case.case_status = RecruitmentCase.CaseStatus.WITHDRAWN
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    else:
        raise ValueError("Unsupported recruitment case transition target.")
    if restart_stage_clock and case.current_stage == previous_stage:
        case.stage_entered_at = timezone.now()

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
    application.current_handler_role = case.current_handler_role
    application.status = to_status
    application.closed_at = (
        case.closed_at if case.current_stage == RecruitmentCase.Stage.CLOSED else None
    )
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=route_type,
        description=description,
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=application.current_handler_role,
        from_status=previous_status,
        to_status=to_status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=notes,
        is_override=is_override,
    )
    return {
        "previous_stage": previous_stage,
        "previous_case_status": previous_case_status,
        "previous_role": previous_role,
        "previous_status": previous_status,
        "case": case,
    }


def get_available_actions(application, user):
    effective_role = access.get_effective_role_for_action(user, application)
    case = getattr(application, "case", None)
    if case and case.is_stage_locked:
        return []
    current_section = get_current_workflow_section(application)
    current_stage = case.current_stage if case else _review_stage_from_application_status(application.status)

    # RD-4: an applicant awaiting resubmission (typically past the deadline) can be
    # removed from the batch — a disqualification with a reason that unblocks the
    # batch screening gate. Removal is offered on the case status alone because an
    # awaiting-resubmission case's screening is not finalized.
    if (
        current_section == "screening"
        and effective_role in common.SCREENING_REVIEW_ROLES
        and current_stage in common.SCREENING_STAGES
        and case
        and case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
    ):
        return [("remove_applicant", "Remove from Batch")]

    if (
        current_section == "decision"
        and effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        and application.branch == PositionPosting.Branch.PLANTILLA
        and car.get_latest_finalized_comparative_assessment_report(application)
        and not selection.get_final_selection_for_application(application)
    ):
        return [
            (
                "return_car_for_reassessment",
                "Return CAR for correction (ETE / recommendation)",
            ),
        ]

    return []


def _transition_target(application, effective_role, action):
    if action == "reject" and effective_role in common.SCREENING_REVIEW_ROLES:
        role_label = RecruitmentUser.Role(effective_role).label
        return "", RecruitmentApplication.Status.REJECTED, f"Rejected by {role_label}."
    raise ValueError("This action is not allowed at the current step.")


@transaction.atomic
def remove_applicant_from_batch(application, actor, reason):
    """Remove an applicant who is awaiting resubmission from the vacancy's batch — i.e. disqualify
    them with a recorded reason (§4.3). This is a terminal, audit-kept close (NOT a hard delete):
    it reuses the rejected/closed case state so the applicant is excluded from the advancing batch,
    which also unblocks the batch screening gate. Scoped to screening-stage applications that are
    awaiting resubmission (the deadline-misser case)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record a reason before removing the applicant from the batch.")
    case = getattr(application, "case", None)
    if case is None or case.current_stage not in common.SCREENING_STAGES:
        raise ValueError("Applicants can only be removed from the batch during screening.")
    if case.case_status != RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
        raise ValueError("Only an applicant awaiting resubmission can be removed from the batch.")
    if not access.user_can_process_application(actor, application):
        raise ValueError("This case is not currently assigned to you.")

    screening_record = screening.get_screening_record(application, stage=case.current_stage)
    missed_deadline = bool(screening_record and screening_record.resubmission_is_overdue)
    previous_status = application.status
    previous_case_status = case.case_status

    # Reuse the terminal rejected/closed transition so queue-exclusion + lock semantics are shared.
    transition_case(
        application,
        actor,
        to_status=RecruitmentApplication.Status.REJECTED,
        to_role="",
        route_type=RoutingHistory.RouteType.CLOSE,
        description="Removed the applicant from the batch (disqualified).",
        notes=reason,
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICANT_DISQUALIFIED,
        description="Removed the applicant from the batch (disqualified).",
        metadata={
            "reason": reason,
            "missed_resubmission_deadline": missed_deadline,
            "from_status": previous_status,
            "to_status": application.status,
            "from_case_status": previous_case_status,
            "to_case_status": application.case.case_status,
        },
    )
    queue_non_selected_applicant_notification(application, actor=actor, cut_at_screening=True)
    return application


@transaction.atomic
def process_workflow_action(application, actor, action, remarks):
    effective_role = access.get_effective_role_for_action(actor, application)
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before saving the next step.")
    if application.case.is_stage_locked:
        raise ValueError("This case is final and cannot be edited unless the HRM Chief reopens it.")
    if not access.user_can_process_application(actor, application):
        if (
            effective_role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat."
            )
        raise ValueError("This case is not currently assigned to you.")
    current_section = get_current_workflow_section(application)
    # A return that flags specific documents is a RESUBMISSION request: the case stays
    # visible in the reviewing role's batch as "awaiting resubmission" and the applicant
    # re-uploads the flagged files from Track Application (§4.2).
    if action == "remove_applicant":
        return remove_applicant_from_batch(application, actor, remarks)
    if action == "return_to_applicant":
        _screening_record, _flagged = screening._flagged_resubmission_reviews(
            application, stage=application.case.current_stage
        )
        if _flagged:
            return screening.request_document_resubmission(application, actor, remarks)
        # RD-6b (Option A): there is no generic "hand the whole application back"
        # return. It never had a working applicant path (the Track Application
        # resubmission page only exposes flagged documents), it was barely
        # reachable (auto-advance skips the post-exam actions window), and it
        # contradicted RD-1's doctrine — a return is a document-resubmission
        # request; an unsuitable applicant is Not Qualified / Rejected.
        raise ValueError(
            "Returning the whole application is no longer available. Flag the "
            "specific documents to return on the screening review, or record "
            "Not Qualified / Reject the application."
        )
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and action == "return_car_for_reassessment"
    ):
        if current_section != "decision":
            raise ValueError(_workflow_progress_block_message(application, current_section))
        return selection.return_car_for_reassessment(application, actor, remarks)
    next_role, next_status, description = _transition_target(application, effective_role, action)
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = transition_case(
        application,
        actor,
        to_status=next_status,
        to_role=next_role,
        route_type=(
            RoutingHistory.RouteType.CLOSE
            if next_status == RecruitmentApplication.Status.REJECTED
            else RoutingHistory.RouteType.FORWARD
        ),
        description=description,
        notes=remarks,
    )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=description,
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "action": action,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
        },
    )
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        _consume_active_secretariat_handoff(
            application,
            actor,
            "Secretariat special authorization ended after Level 2 processing.",
        )
    if next_status == RecruitmentApplication.Status.REJECTED:
        queue_non_selected_applicant_notification(
            application,
            actor=actor,
            cut_at_screening=case_transition["previous_stage"] in common.SCREENING_STAGES,
        )
    return application


@transaction.atomic
def grant_secretariat_override(application, actor, reason):
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief can send a Level 2 case to Secretariat.")
    if application.level != PositionPosting.Level.LEVEL_2:
        raise ValueError("Special authorization is only required for Level 2 applications.")
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before special authorization can be recorded.")
    case = application.case
    if (
        application.status != RecruitmentApplication.Status.HRM_CHIEF_REVIEW
        or application.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
        or case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        or case.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
    ):
        raise ValueError(
            "Special authorization is only available while the case is assigned to HRM Chief review."
        )
    route_case_between_secretariat_and_hrm_chief(
        application=application,
        actor=actor,
        target_role=RecruitmentUser.Role.SECRETARIAT,
        remarks=reason,
    )
    application.refresh_from_db()
    return application.active_secretariat_override
