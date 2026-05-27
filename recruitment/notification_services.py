from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import (
    AuditLog,
    Notification,
    NotificationLog,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)


NOTIFICATION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}
IN_APP_NOTIFICATION_RETENTION_DAYS = 90


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
    return (
        f"RecruitGuard-CHD application received: {application.reference_number}",
        "\n".join(
            [
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
        ),
    )


def _build_selected_notification(application):
    return (
        f"RecruitGuard-CHD application result: {application.position.title}",
        "\n".join(
            [
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
        ),
    )


def _build_non_selected_notification(application):
    return (
        f"RecruitGuard-CHD application result: {application.position.title}",
        "\n".join(
            [
                f"Dear {application.applicant_display_name},",
                "",
                (
                    f"Your application for {application.position.title} "
                    f"under {application.position.get_branch_display()} recruitment was not selected."
                ),
                f"Application ID: {application.reference_number}",
                "",
                "Thank you for your interest in this recruitment opportunity.",
            ]
        ),
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
    lines.extend(
        [
            "",
            "Please check your application status and follow the instructions from the recruitment office.",
            "This notice was sent through RecruitGuard-CHD.",
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


def _build_document_resubmission_request_notification(
    application,
    document_reviews,
    workflow_remarks="",
):
    requested_documents = [
        {
            "document_key": review.document_key,
            "requirement_title": review.requirement_title,
            "remarks": review.remarks,
        }
        for review in document_reviews
    ]
    body = render_to_string(
        "email/document_resubmission_request.txt",
        {
            "application": application,
            "requested_documents": requested_documents,
            "workflow_remarks": (workflow_remarks or "").strip(),
        },
    ).strip()
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
            "This requirements checklist was sent through RecruitGuard-CHD.",
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
    lines.extend(["", "This reminder was sent through RecruitGuard-CHD."])
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


def _deliver_notification(notification_id):
    notification = NotificationLog.objects.select_related(
        "application",
        "triggered_by",
    ).get(pk=notification_id)
    if notification.delivery_status != NotificationLog.DeliveryStatus.PENDING:
        return notification

    try:
        sent_count = send_mail(
            subject=notification.subject,
            message=notification.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[notification.recipient_email],
            fail_silently=False,
        )
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
        body=body,
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
        },
    )


def queue_non_selected_applicant_notification(application, actor=None):
    subject, body = _build_non_selected_notification(application)
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
                    "location": interview_session.location,
                    "recipient_user_id": recipient.id,
                    "recipient_role": recipient.role,
                },
            )
        )
    return notifications


def queue_document_resubmission_request_notification(
    application,
    actor=None,
    *,
    document_reviews,
    workflow_remarks="",
):
    document_reviews = list(document_reviews or [])
    if not document_reviews:
        raise ValueError("At least one document review row is required for a resubmission request.")
    subject, body, requested_documents = _build_document_resubmission_request_notification(
        application,
        document_reviews,
        workflow_remarks=workflow_remarks,
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
            "screening_document_review_ids": [review.id for review in document_reviews],
            "workflow_remarks": (workflow_remarks or "").strip(),
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
        },
    )
