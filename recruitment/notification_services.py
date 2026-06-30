import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .email_branding import (
    REPORTING_INSTRUCTIONS,
    email_branding_context,
    email_footer_text_lines,
)
from .models import (
    AuditLog,
    Notification,
    NotificationLog,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)


logger = logging.getLogger(__name__)

# Each notification type maps to its branded HTML template (extends email/base.html).
# Audience (below) drives both the HTML footer and the plain-text footer: applicant-facing
# emails carry the HRM Unit contact + data-privacy notice; internal staff emails carry only
# the office identity + do-not-reply line.
NOTIFICATION_EMAIL_TEMPLATES = {
    NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT: "email/application_received.html",
    NotificationLog.NotificationType.SELECTED_APPLICANT: "email/selected_applicant.html",
    NotificationLog.NotificationType.NON_SELECTED_APPLICANT: "email/non_selected_applicant.html",
    NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST: "email/document_resubmission_request.html",
    NotificationLog.NotificationType.APPLICATION_RETURNED_TO_APPLICANT: "email/application_returned.html",
    NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED: "email/interview_scheduled_internal.html",
    NotificationLog.NotificationType.EXAM_INVITATION: "email/exam_invitation.html",
    NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE: "email/applicant_interview_notice.html",
    NotificationLog.NotificationType.REQUIREMENT_CHECKLIST: "email/requirement_checklist.html",
    NotificationLog.NotificationType.REMINDER: "email/reminder.html",
}

# Internal staff recipients (interview panel raters); every other type is applicant-facing.
INTERNAL_AUDIENCE_NOTIFICATION_TYPES = {
    NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
}


def _email_audience_for(notification_type):
    if notification_type in INTERNAL_AUDIENCE_NOTIFICATION_TYPES:
        return "internal"
    return "applicant"


def _with_email_footer(body, audience):
    """Append the canonical institutional footer to a plain-text email body."""
    footer_lines = email_footer_text_lines(audience)
    if not footer_lines:
        return body
    return body.rstrip() + "\n\n" + "\n".join(footer_lines)

NOTIFICATION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}
IN_APP_NOTIFICATION_RETENTION_DAYS = 90
# Post-selection "memo" submission window: the successful applicant has two weeks to
# submit the hard-copy requirements (per HRMPSB Secretariat interviews). Used as the
# default deadline when the handler does not set one, so the memo always carries a date.
REQUIREMENT_CHECKLIST_DEFAULT_DEADLINE_DAYS = 14


def _truncate(value, limit):
    return (value or "").strip()[:limit]


def create_in_app_notification(
    *,
    recipient,
    kind,
    title,
    body="",
    related_url="",
    application=None,
):
    if not recipient or not getattr(recipient, "is_active", False):
        return None
    return Notification.objects.create(
        recipient=recipient,
        kind=kind,
        title=_truncate(title, 200),
        body=_truncate(body, 400),
        related_url=_truncate(related_url, 400),
        application=application,
    )


def create_in_app_notifications(
    recipients,
    *,
    kind,
    title,
    body="",
    related_url="",
    application=None,
):
    notifications = []
    seen_recipient_ids = set()
    for recipient in recipients:
        if not recipient or recipient.id in seen_recipient_ids:
            continue
        seen_recipient_ids.add(recipient.id)
        notification = create_in_app_notification(
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            related_url=related_url,
            application=application,
        )
        if notification is not None:
            notifications.append(notification)
    return notifications


def get_recent_notifications(user, limit=10):
    return Notification.objects.select_related("application").filter(
        recipient=user,
    ).order_by("-created_at")[:limit]


def get_unread_count(user):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_notification_read(notification_id, user):
    notification = Notification.objects.select_related("application").get(
        pk=notification_id,
        recipient=user,
    )
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])
    return notification


def mark_all_notifications_read(user):
    now = timezone.now()
    return Notification.objects.filter(
        recipient=user,
        read_at__isnull=True,
    ).update(read_at=now, updated_at=now)


def purge_old_notifications(days=IN_APP_NOTIFICATION_RETENTION_DAYS):
    cutoff = timezone.now() - timedelta(days=days)
    return Notification.objects.filter(created_at__lt=cutoff).delete()


def user_can_send_requirement_checklist_notification(user, application):
    case = getattr(application, "case", None)
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.role in NOTIFICATION_MANAGER_ROLES
        and application.status == RecruitmentApplication.Status.APPROVED
        and case
        and case.current_stage == RecruitmentCase.Stage.COMPLETION
        and not case.is_stage_locked
        and case.current_handler_role == user.role
    )


def user_can_send_reminder_notification(user, application):
    case = getattr(application, "case", None)
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.role in NOTIFICATION_MANAGER_ROLES
        and application.submitted_at
        and application.status
        not in {
            RecruitmentApplication.Status.REJECTED,
            RecruitmentApplication.Status.WITHDRAWN,
        }
        and (
            (case and not case.is_stage_locked and case.current_handler_role == user.role)
            or application.current_handler_role == user.role
        )
    )


def _recipient_name(application):
    return application.applicant_display_name


def _recipient_email(application):
    direct_email = (application.applicant_email or "").strip().lower()
    if direct_email:
        return direct_email
    return (getattr(application.applicant, "email", "") or "").strip().lower()


def _completion_label(application):
    if application.branch == "plantilla":
        return "Plantilla appointment completion"
    return "COS contract completion"


def _format_deadline(deadline):
    if not deadline:
        return ""
    return deadline.strftime("%B %d, %Y")


def _format_schedule(value):
    if not value:
        return ""
    return timezone.localtime(value).strftime("%B %d, %Y at %I:%M %p")


def build_applicant_status_url(application):
    path = reverse("applicant-status-link", kwargs={"token": application.public_token})
    base_url = (getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def _append_status_link(lines, application):
    lines.extend(
        [
            "",
            f"Check your application status anytime: {build_applicant_status_url(application)}",
        ]
    )


def _record_notification_audit(application, actor, action, description, metadata):
    AuditLog.objects.create(
        application=application,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        action=action,
        description=description,
        metadata=metadata,
    )


def _build_submission_acknowledgment(application):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        "Your application has been received by RecruitGuard-CHD.",
        f"Application ID: {application.reference_number}",
        f"Position: {application.position.title}",
        f"Recruitment type: {application.position.get_branch_display()}",
        f"Current status: {application.get_status_display()}",
        "",
        "Keep your Application ID so you can check your application status.",
    ]
    _append_status_link(lines, application)
    return (
        f"RecruitGuard-CHD application received: {application.reference_number}",
        "\n".join(lines),
    )


def _build_selected_notification(application):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"You have been selected for {application.position.title} "
            f"under {application.position.get_branch_display()} recruitment."
        ),
        (
            f"The next step is { _completion_label(application).lower() } "
            "within the scope of this recruitment process."
        ),
        f"Application ID: {application.reference_number}",
        "",
        "Please wait for the requirement checklist or any additional office instructions.",
    ]
    _append_status_link(lines, application)
    return (
        f"RecruitGuard-CHD application result: {application.position.title}",
        "\n".join(lines),
    )


def _build_non_selected_notification(application, cut_at_screening=False):
    if cut_at_screening:
        result_line = (
            f"After the initial screening of qualifications for {application.position.title} "
            f"under {application.position.get_branch_display()} recruitment, your application "
            "did not meet the Qualification Standards (QS) for this position and will not "
            "proceed to the examination."
        )
        closing = (
            "Thank you for your interest. You are welcome to apply for other open positions "
            "that match your qualifications."
        )
    else:
        result_line = (
            f"Your application for {application.position.title} "
            f"under {application.position.get_branch_display()} recruitment was not selected."
        )
        closing = "Thank you for your interest in this recruitment opportunity."
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        result_line,
        f"Application ID: {application.reference_number}",
        "",
        closing,
    ]
    _append_status_link(lines, application)
    return (
        f"RecruitGuard-CHD application result: {application.position.title}",
        "\n".join(lines),
    )


def _build_application_returned_to_applicant_notification(application, workflow_remarks=""):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"Your application for {application.position.title} "
            f"under {application.position.get_branch_display()} recruitment was returned for your action."
        ),
        f"Application ID: {application.reference_number}",
        f"Current status: {application.get_status_display()}",
    ]
    remarks = (workflow_remarks or "").strip()
    if remarks:
        lines.extend(["", "Reason or remarks:", remarks])
    _append_status_link(lines, application)
    lines.extend(
        [
            "",
            "Please check your application status and follow the instructions from the recruitment office.",
        ]
    )
    return (
        f"RecruitGuard-CHD application returned: {application.position.title}",
        "\n".join(lines),
    )


def _build_interview_session_scheduled_notification(application, interview_session, recipient):
    recipient_name = recipient.get_full_name() or recipient.username
    lines = [
        f"Dear {recipient_name},",
        "",
        "An interview session has been scheduled and is ready for panel rating.",
        f"Applicant: {application.applicant_display_name}",
        f"Position: {application.position.title}",
        f"Application ID: {application.reference_number}",
        f"Schedule: {_format_schedule(interview_session.scheduled_for)}",
        f"Location / medium: {interview_session.location}",
    ]
    if interview_session.session_notes:
        lines.extend(["", "Session notes:", interview_session.session_notes.strip()])
    lines.extend(
        [
            "",
            "Please sign in to RecruitGuard-CHD to review the case and submit your interview rating.",
        ]
    )
    return (
        f"RecruitGuard-CHD interview scheduled: {application.position.title}",
        "\n".join(lines),
    )


def _build_document_resubmission_fallback_body(
    application,
    requested_documents,
    workflow_remarks="",
    resubmission_deadline=None,
):
    item_label = "item" if len(requested_documents) == 1 else "items"
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"Your application for {application.position.title} "
            "needs corrected or updated document submission."
        ),
        "",
        f"Application ID: {application.reference_label}",
        "",
        f"Please review the document {item_label} below:",
    ]
    for document in requested_documents:
        lines.append(f"- {document['requirement_title']}")
        lines.append(f"  Instruction: {document['remarks']}")
    if resubmission_deadline:
        lines.extend(
            ["", f"Please resubmit on or before {resubmission_deadline:%B %d, %Y}."]
        )
    remarks = (workflow_remarks or "").strip()
    if remarks:
        lines.extend(["", "Additional note from the recruitment team:", remarks])
    lines.extend(
        [
            "",
            (
                "Please follow the instructions from the recruitment team and "
                "keep your Application ID for status checking."
            ),
            "",
            f"Check your application status anytime: {build_applicant_status_url(application)}",
        ]
    )
    return "\n".join(lines)


def _build_document_resubmission_request_notification(
    application,
    document_reviews,
    workflow_remarks="",
    resubmission_deadline=None,
):
    requested_documents = [
        {
            "document_key": review.document_key,
            "requirement_title": review.requirement_title,
            "remarks": review.remarks,
        }
        for review in document_reviews
    ]
    try:
        body = render_to_string(
            "email/document_resubmission_request.txt",
            {
                "application": application,
                "requested_documents": requested_documents,
                "workflow_remarks": (workflow_remarks or "").strip(),
                "status_link": build_applicant_status_url(application),
                "resubmission_deadline": resubmission_deadline,
            },
        ).strip()
    except Exception:
        # This body is rendered synchronously inside the atomic workflow action
        # (process_workflow_action), so an unguarded template failure would roll
        # back the reviewer's entire "return to applicant" transaction and surface
        # as a 500. Fall back to an equivalent plain-text body built in Python.
        logger.exception(
            "Failed to render document resubmission email for application %s; "
            "falling back to a plain-text body.",
            application.pk,
        )
        body = _build_document_resubmission_fallback_body(
            application,
            requested_documents,
            workflow_remarks=workflow_remarks,
            resubmission_deadline=resubmission_deadline,
        )
    return (
        f"RecruitGuard-CHD document resubmission needed: {application.position.title}",
        body,
        requested_documents,
    )


def _build_requirement_checklist_notification(
    application,
    checklist_items,
    deadline=None,
    additional_message="",
):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"You have been selected for {application.position.title}. "
            f"Please complete the following {_completion_label(application).lower()} requirements:"
        ),
        checklist_items.strip(),
    ]
    if deadline:
        lines.extend(["", f"Submission deadline: {_format_deadline(deadline)}"])
    if additional_message:
        lines.extend(["", additional_message.strip()])
    lines.extend(
        [
            "",
            f"Application ID: {application.reference_number}",
            f"Check your application status anytime: {build_applicant_status_url(application)}",
        ]
    )
    return (
        f"RecruitGuard-CHD requirement checklist: {application.position.title}",
        "\n".join(lines),
    )


def _build_reminder_notification(application, reminder_subject, reminder_message, deadline=None):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        reminder_message.strip(),
        f"Application ID: {application.reference_number}",
        f"Current status: {application.get_status_display()}",
    ]
    if deadline:
        lines.append(f"Reminder deadline: {_format_deadline(deadline)}")
    _append_status_link(lines, application)
    return reminder_subject.strip(), "\n".join(lines)


def _mark_notification_failed(notification, reason):
    notification.delivery_status = NotificationLog.DeliveryStatus.FAILED
    notification.failure_details = reason
    notification.sent_at = None
    notification.save(
        update_fields=[
            "delivery_status",
            "failure_details",
            "sent_at",
            "updated_at",
        ]
    )
    _record_notification_audit(
        application=notification.application,
        actor=notification.triggered_by,
        action=AuditLog.Action.NOTIFICATION_FAILED,
        description=f"Failed to send {notification.get_notification_type_display().lower()}.",
        metadata={
            "notification_id": notification.id,
            "notification_type": notification.notification_type,
            "recipient_email": notification.recipient_email,
            "reason": reason,
        },
    )
    return notification


def _render_notification_html(notification):
    template_name = NOTIFICATION_EMAIL_TEMPLATES.get(notification.notification_type)
    if not template_name:
        return ""

    application = notification.application
    metadata = notification.metadata or {}
    audience = _email_audience_for(notification.notification_type)
    context = {
        "application": application,
        "recipient_name": notification.recipient_name,
        "status_link": (
            metadata.get("status_link") or build_applicant_status_url(application)
        ),
        "meta": metadata,
        "reporting_instructions": REPORTING_INSTRUCTIONS,
        **email_branding_context(audience),
    }
    try:
        return render_to_string(template_name, context).strip()
    except Exception:
        # The HTML body is only a richer alternative to the plain-text message that is
        # already set as the email body. If a template is missing or raises while
        # rendering, degrade gracefully to text-only delivery rather than failing the
        # send (which, for workflow-triggered emails, runs inside the atomic action).
        logger.exception(
            "Failed to render HTML email for notification %s (%s); falling back to "
            "plain-text body.",
            notification.id,
            notification.notification_type,
        )
        return ""


def _deliver_notification(notification_id):
    notification = NotificationLog.objects.select_related(
        "application",
        "application__position",
        "triggered_by",
    ).get(pk=notification_id)
    if notification.delivery_status != NotificationLog.DeliveryStatus.PENDING:
        return notification

    try:
        email = EmailMultiAlternatives(
            subject=notification.subject,
            body=notification.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[notification.recipient_email],
        )
        html_body = _render_notification_html(notification)
        if html_body:
            email.attach_alternative(html_body, "text/html")
        sent_count = email.send(fail_silently=False)
        if sent_count != 1:
            raise RuntimeError("Email backend did not confirm delivery.")
    except Exception as exc:  # pragma: no cover - exercised via status update path
        return _mark_notification_failed(notification, str(exc)[:1000])

    notification.delivery_status = NotificationLog.DeliveryStatus.SENT
    notification.sent_at = timezone.now()
    notification.failure_details = ""
    notification.save(
        update_fields=[
            "delivery_status",
            "sent_at",
            "failure_details",
            "updated_at",
        ]
    )
    _record_notification_audit(
        application=notification.application,
        actor=notification.triggered_by,
        action=AuditLog.Action.NOTIFICATION_SENT,
        description=f"Sent {notification.get_notification_type_display().lower()}.",
        metadata={
            "notification_id": notification.id,
            "notification_type": notification.notification_type,
            "recipient_email": notification.recipient_email,
            "sent_at": notification.sent_at.isoformat(),
        },
    )
    return notification


def queue_notification(
    application,
    *,
    notification_type,
    actor=None,
    subject,
    body,
    metadata=None,
    recipient_name=None,
    recipient_email=None,
):
    resolved_recipient_email = (recipient_email or _recipient_email(application) or "").strip().lower()
    body_with_footer = _with_email_footer(body, _email_audience_for(notification_type))
    notification = NotificationLog.objects.create(
        application=application,
        recruitment_case=getattr(application, "case", None),
        triggered_by=actor,
        triggered_by_role=getattr(actor, "role", ""),
        notification_type=notification_type,
        delivery_channel=NotificationLog.DeliveryChannel.EMAIL,
        delivery_status=NotificationLog.DeliveryStatus.PENDING,
        related_status=application.status,
        recipient_name=recipient_name or _recipient_name(application),
        recipient_email=resolved_recipient_email or "missing-email@invalid.local",
        subject=subject,
        body=body_with_footer,
        metadata=metadata or {},
    )

    if not resolved_recipient_email:
        return _mark_notification_failed(
            notification,
            "No recipient email address is available for this notification.",
        )

    transaction.on_commit(lambda: _deliver_notification(notification.id))
    return notification


def queue_submission_acknowledgment_notification(application, actor=None):
    subject, body = _build_submission_acknowledgment(application)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "status_link": build_applicant_status_url(application),
        },
    )


def queue_selected_applicant_notification(application, actor=None):
    subject, body = _build_selected_notification(application)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "completion_label": _completion_label(application),
            "status_link": build_applicant_status_url(application),
        },
    )


def queue_non_selected_applicant_notification(application, actor=None, cut_at_screening=False):
    subject, body = _build_non_selected_notification(application, cut_at_screening=cut_at_screening)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "cut_at_screening": cut_at_screening,
            "status_link": build_applicant_status_url(application),
        },
    )


def queue_application_returned_to_applicant_notification(
    application,
    actor=None,
    *,
    workflow_remarks="",
):
    subject, body = _build_application_returned_to_applicant_notification(
        application,
        workflow_remarks=workflow_remarks,
    )
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.APPLICATION_RETURNED_TO_APPLICANT,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "workflow_remarks": (workflow_remarks or "").strip(),
            "status_link": build_applicant_status_url(application),
        },
    )


def queue_interview_session_scheduled_notifications(
    application,
    interview_session,
    recipients,
    actor=None,
):
    notifications = []
    for recipient in recipients:
        subject, body = _build_interview_session_scheduled_notification(
            application,
            interview_session,
            recipient,
        )
        notifications.append(
            queue_notification(
                application,
                notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
                actor=actor,
                subject=subject,
                body=body,
                recipient_name=recipient.get_full_name() or recipient.username,
                recipient_email=recipient.email,
                metadata={
                    "reference_number": application.reference_number,
                    "status": application.status,
                    "branch": application.branch,
                    "interview_session_id": interview_session.id,
                    "scheduled_for": interview_session.scheduled_for.isoformat(),
                    "schedule_display": _format_schedule(interview_session.scheduled_for),
                    "location": interview_session.location,
                    "session_notes": (interview_session.session_notes or "").strip(),
                    "applicant_name": application.applicant_display_name,
                    "position_title": application.position.title,
                    "recipient_user_id": recipient.id,
                    "recipient_role": recipient.role,
                },
            )
        )
    return notifications


def _build_exam_invitation_notification(application, exam_schedule):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"Good news. You passed the initial screening for {application.position.title} "
            "and are invited to take the examination. Please note the details below:"
        ),
        "",
        f"Schedule: {_format_schedule(exam_schedule.scheduled_for)}",
        f"Venue: {exam_schedule.venue}",
    ]
    if exam_schedule.instructions:
        lines.extend(["", "Instructions:", exam_schedule.instructions.strip()])
    lines.append("")
    lines.append("Reporting instructions:")
    lines.extend(f"- {item}" for item in REPORTING_INSTRUCTIONS)
    lines.extend(
        [
            "",
            f"Application ID: {application.reference_number}",
            f"Check your application status anytime: {build_applicant_status_url(application)}",
        ]
    )
    return (
        f"RecruitGuard-CHD exam invitation: {application.position.title}",
        "\n".join(lines),
    )


def queue_exam_invitation_notification(application, exam_schedule, actor=None):
    subject, body = _build_exam_invitation_notification(application, exam_schedule)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.EXAM_INVITATION,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "exam_schedule_id": exam_schedule.id,
            "review_stage": exam_schedule.review_stage,
            "scheduled_for": exam_schedule.scheduled_for.isoformat(),
            "schedule_display": _format_schedule(exam_schedule.scheduled_for),
            "venue": exam_schedule.venue,
            "instructions": (exam_schedule.instructions or "").strip(),
            "status_link": build_applicant_status_url(application),
        },
    )


def _build_applicant_interview_notice_notification(application, interview_session):
    lines = [
        f"Dear {application.applicant_display_name},",
        "",
        (
            f"You are scheduled for an interview for {application.position.title}. "
            "Please note the details below:"
        ),
        "",
        f"Schedule: {_format_schedule(interview_session.scheduled_for)}",
        f"Location / medium: {interview_session.location}",
    ]
    lines.append("")
    lines.append("Reporting instructions:")
    lines.extend(f"- {item}" for item in REPORTING_INSTRUCTIONS)
    lines.extend(
        [
            "",
            f"Application ID: {application.reference_number}",
            f"Check your application status anytime: {build_applicant_status_url(application)}",
        ]
    )
    return (
        f"RecruitGuard-CHD interview schedule: {application.position.title}",
        "\n".join(lines),
    )


def queue_applicant_interview_notice_notification(application, interview_session, actor=None):
    subject, body = _build_applicant_interview_notice_notification(application, interview_session)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "interview_session_id": interview_session.id,
            "review_stage": interview_session.review_stage,
            "scheduled_for": interview_session.scheduled_for.isoformat(),
            "schedule_display": _format_schedule(interview_session.scheduled_for),
            "location": interview_session.location,
            "status_link": build_applicant_status_url(application),
        },
    )


def queue_document_resubmission_request_notification(
    application,
    actor=None,
    *,
    document_reviews,
    workflow_remarks="",
    resubmission_deadline=None,
):
    document_reviews = list(document_reviews or [])
    if not document_reviews:
        raise ValueError("At least one document review row is required for a resubmission request.")
    subject, body, requested_documents = _build_document_resubmission_request_notification(
        application,
        document_reviews,
        workflow_remarks=workflow_remarks,
        resubmission_deadline=resubmission_deadline,
    )
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "document_keys": [item["document_key"] for item in requested_documents],
            "requested_documents": requested_documents,
            "screening_document_review_ids": [review.id for review in document_reviews],
            "workflow_remarks": (workflow_remarks or "").strip(),
            "status_link": build_applicant_status_url(application),
            "resubmission_deadline": (
                resubmission_deadline.isoformat() if resubmission_deadline else ""
            ),
        },
    )


def send_requirement_checklist_notification(
    application,
    actor,
    *,
    checklist_items,
    deadline=None,
    additional_message="",
):
    case = getattr(application, "case", None)
    if actor.role not in NOTIFICATION_MANAGER_ROLES:
        raise ValueError("Only Secretariat or HRM Chief may send the requirement checklist.")
    if application.status != RecruitmentApplication.Status.APPROVED:
        raise ValueError("Requirement checklists can only be sent for approved applications.")
    if (
        not case
        or case.current_stage != RecruitmentCase.Stage.COMPLETION
        or case.is_stage_locked
    ):
        raise ValueError("Requirement checklists can only be sent during active completion.")
    if case.current_handler_role != actor.role:
        raise ValueError("Only the office assigned to completion may send the requirement checklist.")

    if deadline is None:
        deadline = timezone.localdate() + timedelta(
            days=REQUIREMENT_CHECKLIST_DEFAULT_DEADLINE_DAYS
        )

    subject, body = _build_requirement_checklist_notification(
        application=application,
        checklist_items=checklist_items,
        deadline=deadline,
        additional_message=additional_message,
    )
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.REQUIREMENT_CHECKLIST,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "deadline": deadline.isoformat() if deadline else "",
            "deadline_display": _format_deadline(deadline),
            "checklist_items": checklist_items.strip(),
            "additional_message": (additional_message or "").strip(),
            "completion_label": _completion_label(application),
            "status_link": build_applicant_status_url(application),
        },
    )


def send_reminder_notification(
    application,
    actor,
    *,
    reminder_subject,
    reminder_message,
    deadline=None,
):
    case = getattr(application, "case", None)
    if actor.role not in NOTIFICATION_MANAGER_ROLES:
        raise ValueError("Only Secretariat or HRM Chief may send reminders.")
    if not application.submitted_at:
        raise ValueError("Reminders can only be sent after an application has been submitted.")
    if application.status in {
        RecruitmentApplication.Status.REJECTED,
        RecruitmentApplication.Status.WITHDRAWN,
    }:
        raise ValueError("Reminders are not available for closed or non-selected applications.")
    if case and (case.is_stage_locked or case.current_handler_role != actor.role):
        raise ValueError("Only the assigned office may send reminders for this application.")

    subject, body = _build_reminder_notification(
        application=application,
        reminder_subject=reminder_subject,
        reminder_message=reminder_message,
        deadline=deadline,
    )
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.REMINDER,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "deadline": deadline.isoformat() if deadline else "",
            "deadline_display": _format_deadline(deadline),
            "reminder_message": reminder_message.strip(),
            "status_link": build_applicant_status_url(application),
        },
    )
