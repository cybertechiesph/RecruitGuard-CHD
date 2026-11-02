"""In-app notices about cases: assignment, screening, resubmission, exam and
interview notices, deadline reminders and failed-notification alerts.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ..models import (
    Notification,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..notification_services import create_in_app_notifications
from . import access, routing


def _application_detail_url(application, tab=None):
    url = reverse("application-detail", kwargs={"pk": application.pk})
    if tab:
        return f"{url}?tab={tab}"
    return url


def _notification_tab_for_application(application):
    section_key = routing.get_current_workflow_section(application)
    if section_key == "overview":
        return "screening"
    return section_key


def _active_users_with_application_access(application, *, role=None, exclude_user_ids=None):
    exclude_user_ids = set(exclude_user_ids or [])
    queryset = RecruitmentUser.objects.filter(is_active=True).exclude(
        role=RecruitmentUser.Role.APPLICANT,
    )
    if role:
        queryset = queryset.filter(role=role)
    queryset = queryset.order_by("last_name", "first_name", "username")
    return [
        user
        for user in queryset
        if user.id not in exclude_user_ids and access.user_can_view_application(user, application)
    ]


def _notify_application_users(
    application,
    recipients,
    *,
    kind,
    title,
    body="",
    tab=None,
):
    return create_in_app_notifications(
        recipients,
        kind=kind,
        title=title,
        body=body,
        related_url=_application_detail_url(
            application,
            tab=tab or _notification_tab_for_application(application),
        ),
        application=application,
    )


def _notify_role_for_application(
    application,
    role,
    *,
    kind,
    title,
    body="",
    tab=None,
    exclude_user_ids=None,
):
    return _notify_application_users(
        application,
        _active_users_with_application_access(
            application,
            role=role,
            exclude_user_ids=exclude_user_ids,
        ),
        kind=kind,
        title=title,
        body=body,
        tab=tab,
    )


def _actor_display_name(actor):
    if not actor:
        return "the office"
    return actor.get_full_name() or actor.username or actor.get_role_display()


def _emit_case_assignment_notification(
    application,
    actor,
    target_role,
    *,
    kind=Notification.Kind.CASE_ASSIGNED,
    title=None,
    body="",
    tab=None,
    exclude_user_ids=None,
):
    if not target_role or target_role == RecruitmentUser.Role.APPLICANT:
        return []
    title = title or f"{application.reference_label} handed off to you by {_actor_display_name(actor)}"
    return _notify_role_for_application(
        application,
        target_role,
        kind=kind,
        title=title,
        body=body,
        tab=tab,
        exclude_user_ids=exclude_user_ids,
    )


def _emit_screening_finalized_notification(application, actor, review_stage):
    return _notify_role_for_application(
        application,
        application.current_handler_role,
        kind=Notification.Kind.SCREENING_FINALIZED,
        title=f"{application.reference_label} screening finalized",
        body=f"{_actor_display_name(actor)} finalized the screening review.",
        tab="screening",
        exclude_user_ids={actor.id} if actor else None,
    )


def _emit_resubmission_received_notification(
    application,
    actor,
    target_role,
    *,
    document_count=0,
):
    document_word = "document" if document_count == 1 else "documents"
    return _emit_case_assignment_notification(
        application,
        actor,
        target_role,
        kind=Notification.Kind.RESUBMISSION_RECEIVED,
        title=f"{application.reference_label} applicant resubmitted {document_count} {document_word}",
        body="The returned application is ready for review.",
        tab="screening",
    )


def _emit_interview_scheduled_in_app_notifications(application, interview_session, recipients):
    scheduled_for = timezone.localtime(interview_session.scheduled_for).strftime("%B %d, %Y")
    return _notify_application_users(
        application,
        [recipient for recipient in recipients if access.user_can_view_application(recipient, application)],
        kind=Notification.Kind.INTERVIEW_SCHEDULED,
        title=f"Interview scheduled for {application.reference_label} on {scheduled_for}",
        body=f"Location / medium: {interview_session.location}",
        tab="interview",
    )


def _emit_interview_finalized_in_app_notifications(application, recipients):
    return _notify_application_users(
        application,
        [recipient for recipient in recipients if access.user_can_view_application(recipient, application)],
        kind=Notification.Kind.INTERVIEW_FINALIZED,
        title=f"{application.reference_label} interview session finalized",
        body="No further interview ratings can be submitted.",
        tab="interview",
    )


def _emit_exam_finalized_in_app_notification(application, actor):
    """RD-9: exam finalize previously advanced the pool silently in-app —
    mirror the interview-finalized bell for the current handler role."""
    case = getattr(application, "case", None)
    if not case or not case.current_handler_role:
        return []
    recipients = [
        recipient
        for recipient in _active_users_with_application_access(
            application, role=case.current_handler_role
        )
        if recipient != actor
    ]
    return _notify_application_users(
        application,
        recipients,
        kind=Notification.Kind.EXAM_FINALIZED,
        title=f"{application.reference_label} exam finalized",
        body="The examination record is locked; the case moves to its next step.",
        tab="exam",
    )


def emit_notification_failed_in_app_alert(notification_log):
    """RD-9: an applicant email failed to send. Raise an in-app bell to the
    case's current-handler users, or the level's owning office after closure —
    otherwise the office believes the applicant was told (an exam invite, a
    resubmission request, or a terminal outcome) when they never were.
    Called from notification_services via a local import (that module cannot
    import services at module level)."""
    application = notification_log.application
    case = getattr(application, "case", None)
    if not case:
        return []
    target_role = case.current_handler_role or routing._completion_handler_role(application)
    recipients = _active_users_with_application_access(
        application, role=target_role
    )
    return _notify_application_users(
        application,
        recipients,
        kind=Notification.Kind.NOTIFICATION_FAILED,
        title=(
            f"{application.reference_label} applicant email failed: "
            f"{notification_log.get_notification_type_display()}"
        ),
        body="The applicant did NOT receive this email. Open the case to resend it.",
        tab="overview",
    )


def emit_deadline_approaching_notifications(now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    closing_cutoff = today + timedelta(days=1)
    applications = RecruitmentApplication.objects.select_related(
        "position",
        "case",
    ).filter(
        position__closing_date__gte=today,
        position__closing_date__lte=closing_cutoff,
        case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
        case__is_stage_locked=False,
    ).exclude(
        current_handler_role__in=["", RecruitmentUser.Role.APPLICANT],
    )

    emitted = []
    for application in applications:
        case = application.case
        recipients = _active_users_with_application_access(
            application,
            role=case.current_handler_role,
        )
        if not recipients:
            continue
        title = f"{application.reference_label} is closing in 24 hours"
        body = f"Posting closes on {application.position.closing_date:%Y-%m-%d}."
        for recipient in recipients:
            already_sent_today = Notification.objects.filter(
                application=application,
                recipient=recipient,
                kind=Notification.Kind.DEADLINE_APPROACHING,
                created_at__date=today,
            ).exists()
            if already_sent_today:
                continue
            emitted.extend(
                _notify_application_users(
                    application,
                    [recipient],
                    kind=Notification.Kind.DEADLINE_APPROACHING,
                    title=title,
                    body=body,
                    tab="screening",
                )
            )
    return emitted


def emit_car_finalized_panel_notifications(report):
    """HRMPSB review P2-5: tell the vacancy's panel that its CAR is final and the cases are
    with the Appointing Authority. Candidate codes only; the notice links nowhere, since the
    cases leave the panel's view at this point."""
    entry = report.recruitment_entry
    panel = [
        member
        for member in RecruitmentUser.objects.filter(
            is_active=True, role=RecruitmentUser.Role.HRMPSB_MEMBER
        ).order_by("pk")
        if access.user_is_on_vacancy_panel(member, entry)
    ]
    if report.recommends_none:
        recommendation = "The board recommended none of the candidates."
    elif report.recommended_case_id:
        recommendation = (
            f"Recommended: Candidate {report.recommended_case.application.candidate_code_label}."
        )
    else:
        recommendation = ""
    return create_in_app_notifications(
        panel,
        kind=Notification.Kind.CAR_FINALIZED,
        title=f"The CAR for {entry.title} ({entry.job_code}) is finalized",
        body=(
            f"{recommendation} The cases are now with the Appointing Authority for the decision."
        ).strip(),
    )
