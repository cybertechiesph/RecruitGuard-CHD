from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import AuditLog, NotificationLog, RecruitmentApplication, RecruitmentCase, RecruitmentUser


NOTIFICATION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}


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
        f"RecruitGuard-CHD submission acknowledged: {application.reference_number}",
        "\n".join(
            [
                f"Dear {application.applicant_display_name},",
                "",
                "Your application has been submitted successfully to RecruitGuard-CHD.",
                f"Application ID: {application.reference_number}",
                f"Position: {application.position.title}",
                f"Recruitment branch: {application.position.get_branch_display()}",
                f"Current status: {application.get_status_display()}",
                "",
                "Keep your Application ID for future status checks in the applicant portal.",
            ]
        ),
    )


def _build_selected_notification(application):
    return (
        f"RecruitGuard-CHD selection result: {application.position.title}",
        "\n".join(
            [
                f"Dear {application.applicant_display_name},",
                "",
                (
                    f"You have been selected for the {application.position.get_branch_display()} "
                    f"recruitment process for {application.position.title}."
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
        f"RecruitGuard-CHD non-selection notice: {application.position.title}",
        "\n".join(
            [
                f"Dear {application.applicant_display_name},",
                "",
                (
                    f"Your application for the {application.position.get_branch_display()} "
                    f"recruitment process for {application.position.title} was not selected."
                ),
                f"Application ID: {application.reference_number}",
                "",
                "Thank you for your interest in this recruitment opportunity.",
            ]
        ),
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
            "This requirement checklist was sent through RecruitGuard-CHD.",
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
):
    recipient_email = _recipient_email(application)
    notification = NotificationLog.objects.create(
        application=application,
        recruitment_case=getattr(application, "case", None),
        triggered_by=actor,
        triggered_by_role=getattr(actor, "role", ""),
        notification_type=notification_type,
        delivery_channel=NotificationLog.DeliveryChannel.EMAIL,
        delivery_status=NotificationLog.DeliveryStatus.PENDING,
        related_status=application.status,
        recipient_name=_recipient_name(application),
        recipient_email=recipient_email or "missing-email@invalid.local",
        subject=subject,
        body=body,
        metadata=metadata or {},
    )

    if not recipient_email:
        return _mark_notification_failed(
            notification,
            "No applicant email address is available for this application.",
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
        raise ValueError("Only Secretariat or HRM Chief may send requirement checklist notifications.")
    if application.status != RecruitmentApplication.Status.APPROVED:
        raise ValueError("Requirement checklist notifications are only available for approved applications.")
    if (
        not case
        or case.current_stage != RecruitmentCase.Stage.COMPLETION
        or case.is_stage_locked
    ):
        raise ValueError("Requirement checklist notifications are only available during active completion tracking.")
    if case.current_handler_role != actor.role:
        raise ValueError("Only the assigned completion handler may send the requirement checklist.")

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
        raise ValueError("Only Secretariat or HRM Chief may send reminder notifications.")
    if not application.submitted_at:
        raise ValueError("Reminders are only available after an application has been submitted.")
    if application.status in {
        RecruitmentApplication.Status.REJECTED,
        RecruitmentApplication.Status.WITHDRAWN,
    }:
        raise ValueError("Reminders are not available for closed non-selected applications.")
    if case and (case.is_stage_locked or case.current_handler_role != actor.role):
        raise ValueError("Only the assigned active handler may send reminders for this application.")

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
