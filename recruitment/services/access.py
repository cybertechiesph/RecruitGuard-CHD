"""Who may see what: visible positions, the work queue, closed cases, Level 2
following, vacancy panel membership and the generic view/process checks.
"""

from django.db.models import Q

from ..models import (
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..permissions import WORKFLOW_PROCESSOR_ROLES
from . import car, common, entries, interview, routing, workflow_map


def get_visible_positions_for_user(user):
    if user.role == RecruitmentUser.Role.APPLICANT:
        postings = PositionPosting.objects.filter(
            status=PositionPosting.EntryStatus.ACTIVE,
        ).select_related("position_reference")
        return [entry for entry in postings if entry.is_open_for_intake]
    return PositionPosting.objects.select_related("position_reference").all()


def get_queue_for_user(user):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return RecruitmentApplication.objects.none()
    queryset = RecruitmentApplication.objects.select_related("applicant", "position", "case")
    assigned_queryset = queryset.filter(current_handler_role=user.role)
    support_queryset = RecruitmentApplication.objects.none()
    interview_support_levels = [
        level
        for level, roles in common.PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL.items()
        if user.role in roles
    ]
    car_support_levels = [
        level
        for level, roles in common.PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.items()
        if user.role in roles
    ]
    if interview_support_levels:
        support_queryset = support_queryset | queryset.filter(
            branch=PositionPosting.Branch.PLANTILLA,
            level__in=interview_support_levels,
            status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            case__current_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
            case__is_stage_locked=False,
        ).exclude(
            interview_sessions__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            interview_sessions__is_finalized=True,
        )
    if car_support_levels:
        support_queryset = support_queryset | queryset.filter(
            branch=PositionPosting.Branch.PLANTILLA,
            level__in=car_support_levels,
            status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            case__current_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
            case__is_stage_locked=False,
            interview_sessions__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            interview_sessions__is_finalized=True,
        ).exclude(
            position__comparative_assessment_reports__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            position__comparative_assessment_reports__is_finalized=True,
        )
    if user.role == RecruitmentUser.Role.SECRETARIAT:
        # The Level-1/override restriction applies to cases ASSIGNED to the Secretariat.
        # Support carve-outs stay untouched: WF-04 makes the Secretariat the CAR preparer
        # for BOTH levels, so a Level-2 case at the CAR section must reach its queue.
        # No .distinct() here — it would make the queryset non-combinable with the
        # support union below, which applies distinct() over the whole result anyway.
        assigned_queryset = assigned_queryset.filter(
            Q(level=PositionPosting.Level.LEVEL_1)
            | Q(
                overrides__is_active=True,
                overrides__target_role=RecruitmentUser.Role.SECRETARIAT,
            )
        )
    queryset = (assigned_queryset | support_queryset).distinct()
    if user.role == RecruitmentUser.Role.HRMPSB_MEMBER:
        queryset = queryset.filter(_panel_position_q(user)).distinct()
    # Urgency ordering: oldest stage-entry first, so the most overdue cases (the
    # longest in their current stage) lead the queue instead of being buried in
    # insertion order. pk breaks ties deterministically.
    return queryset.order_by("case__stage_entered_at", "pk")


def get_closed_cases_for_user(user):
    """Closed/decided cases the user retains view access to — a lookup surface for records
    requests. Strictly mirrors _user_has_closed_application_access as queryset filters so it
    never widens the "see only what you need" access model: the list only ever contains cases
    the user could already open by URL."""
    is_processor = user.role in WORKFLOW_PROCESSOR_ROLES
    is_exporter = user.role in common.EXPORT_ROLES
    if not (is_processor or is_exporter):
        return RecruitmentApplication.objects.none()
    queryset = RecruitmentApplication.objects.select_related(
        "applicant", "position", "case"
    ).filter(
        status__in=[
            RecruitmentApplication.Status.APPROVED,
            RecruitmentApplication.Status.REJECTED,
            # SL-10: a withdrawal is a closed case too — records requests about a withdrawn
            # applicant must find it, and the recorder must not lose sight of the record
            # they just created.
            RecruitmentApplication.Status.WITHDRAWN,
            RecruitmentApplication.Status.VACANCY_CANCELLED,
        ]
    )
    # Secretariat never sees Level 2 closed cases (helper's first guard).
    if user.role == RecruitmentUser.Role.SECRETARIAT:
        queryset = queryset.exclude(level=PositionPosting.Level.LEVEL_2)
    # Helper split: closed-stage cases need a processor role; approved/rejected
    # cases not yet at the closed stage need an export role.
    if is_processor and is_exporter:
        pass  # sees both (secretariat / HRM chief / appointing authority)
    elif is_processor:
        queryset = queryset.filter(case__current_stage=RecruitmentCase.Stage.CLOSED)
    else:  # is_exporter only — no role currently, kept for completeness
        queryset = queryset.exclude(case__current_stage=RecruitmentCase.Stage.CLOSED)
    return queryset.order_by("-closed_at", "-updated_at", "pk").distinct()


def _user_has_closed_application_access(user, application):
    case = getattr(application, "case", None)
    if (
        user.role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return False
    if case and case.current_stage == RecruitmentCase.Stage.CLOSED:
        return user.role in WORKFLOW_PROCESSOR_ROLES
    return user.role in common.EXPORT_ROLES


LEVEL_TWO_FOLLOW_STAGES = workflow_map.stages_followed_by_intake_office(PositionPosting.Level.LEVEL_2)


def user_can_follow_level_two_case(user, application):
    """Chief review C2: the HRM Chief owns Level 2 by the routing rule, but the Secretariat
    prepares the CAR (WF-04) and the Appointing Authority decides. The Chief follows those two
    steps read-only: view, print and export, never an action (every action keeps its own
    permission check, none of which this grants)."""
    case = getattr(application, "case", None)
    return bool(
        case
        and user.role == RecruitmentUser.Role.HRM_CHIEF
        and application.branch == PositionPosting.Branch.PLANTILLA
        and application.level == PositionPosting.Level.LEVEL_2
        and case.case_status == RecruitmentCase.CaseStatus.ACTIVE
        and case.current_stage in LEVEL_TWO_FOLLOW_STAGES
    )


def get_followed_applications(user):
    """The Level 2 Plantilla cases the HRM Chief follows read-only (C2)."""
    if user.role != RecruitmentUser.Role.HRM_CHIEF:
        return RecruitmentApplication.objects.none()
    return (
        RecruitmentApplication.objects.filter(
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_2,
            case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
            case__current_stage__in=LEVEL_TWO_FOLLOW_STAGES,
        )
        .select_related("position", "case")
        .order_by("reference_number")
    )


def vacancy_panel_member_ids(posting):
    """4a: the HRMPSB members on this vacancy's panel: the standing core plus the vacancy's
    end users. None when neither is set, meaning every active member (as before 4a)."""
    core = set(
        entries.get_office_policy()
        .core_panel_members.filter(is_active=True, role=RecruitmentUser.Role.HRMPSB_MEMBER)
        .values_list("pk", flat=True)
    )
    end_users = set(
        posting.panel_end_users.filter(is_active=True, role=RecruitmentUser.Role.HRMPSB_MEMBER)
        .values_list("pk", flat=True)
    )
    if not core and not end_users:
        return None
    return core | end_users


def user_is_on_vacancy_panel(user, posting):
    """Only restricts HRMPSB members; every other role is unaffected."""
    if user.role != RecruitmentUser.Role.HRMPSB_MEMBER:
        return True
    member_ids = vacancy_panel_member_ids(posting)
    return member_ids is None or user.pk in member_ids


def _panel_position_q(user):
    """Queryset filter for the vacancies an HRMPSB member sits on (mirrors the rule above)."""
    core = set(entries.get_office_policy().core_panel_members.values_list("pk", flat=True))
    if user.pk in core:
        return Q()
    if core:
        return Q(position__panel_end_users=user)
    return Q(position__panel_end_users=user) | Q(position__panel_end_users__isnull=True)


def user_can_view_application(user, application):
    if user.role == RecruitmentUser.Role.APPLICANT:
        return application.applicant_id == user.id
    if not user_is_on_vacancy_panel(user, application.position):
        # 4a: an end-user member sees only the vacancies they sit on.
        return False
    if interview.user_can_support_plantilla_interview(user, application):
        return True
    if car.user_can_prepare_plantilla_car(user, application):
        return True
    if user_can_follow_level_two_case(user, application):
        return True
    if user.role == routing.level_owner_role(application) and routing.case_is_on_loan(application):
        # C4: the office that lent the batch keeps read access to it.
        return True
    if application.current_handler_role == user.role:
        if (
            user.role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            return application.active_secretariat_override is not None
        return True
    if application.status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
        # SL-10: withdrawn follows the same closed-case access as rejected — without this,
        # a withdrawn case was viewable by NOBODY (the status fell outside both branches),
        # which broke the post-withdrawal redirect and hid the record from audit retrieval.
        RecruitmentApplication.Status.WITHDRAWN,
        RecruitmentApplication.Status.VACANCY_CANCELLED,
    }:
        return _user_has_closed_application_access(user, application)
    return False


def get_effective_role_for_action(user, application):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return ""
    return user.role


def user_can_process_application(user, application):
    case = getattr(application, "case", None)
    effective_role = get_effective_role_for_action(user, application)
    if not effective_role:
        return False
    expected_role = case.current_handler_role if case else application.current_handler_role
    if effective_role != expected_role:
        return False
    if case and case.is_stage_locked:
        return False
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return application.active_secretariat_override is not None
    return effective_role in WORKFLOW_PROCESSOR_ROLES
