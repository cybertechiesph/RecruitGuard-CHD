import functools
import logging
import smtplib
import socket
from .blinding import blind_text, is_blind_viewer
from datetime import timedelta

from django.utils.dateparse import parse_datetime
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext, ngettext

from .email_branding import (
    REPORTING_INSTRUCTIONS,
    email_branding_context,
    email_footer_text_lines,
)
from .models import (
    AuditLog,
    CompletionRequirement,
    Notification,
    NotificationLog,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from .applicant_labels import applicant_branch_label, applicant_status_label


logger = logging.getLogger(__name__)


def summarize_delivery_failure(exc: Exception) -> str:
    """Return a safe, fixed delivery-failure summary for persistent records."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        summary = "Mail server rejected the sender credentials"
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        summary = "Mail server refused the recipient address"
    elif isinstance(
        exc,
        (
            smtplib.SMTPConnectError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPHeloError,
        ),
    ):
        summary = "Could not reach the mail server"
    elif isinstance(exc, smtplib.SMTPException):
        summary = "Mail server returned an error"
    elif isinstance(
        exc,
        (
            ConnectionRefusedError,
            socket.timeout,
            TimeoutError,
            OSError,
        ),
    ):
        summary = "Could not reach the mail server"
    elif (
        isinstance(exc, RuntimeError)
        and exc.args == ("Email backend did not confirm delivery.",)
    ):
        summary = "Email backend did not confirm delivery."
    else:
        summary = "Unexpected error while sending"
    return f"{summary} ({type(exc).__name__})"

# Each notification type maps to its branded HTML template (extends email/base.html).
# Audience (below) drives both the HTML footer and the plain-text footer: applicant-facing
# emails carry the HRM Unit contact + data-privacy notice; internal staff emails carry only
# the office identity + do-not-reply line.
NOTIFICATION_EMAIL_TEMPLATES = {
    NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT: "email/application_received.html",
    NotificationLog.NotificationType.SELECTED_APPLICANT: "email/selected_applicant.html",
    NotificationLog.NotificationType.NON_SELECTED_APPLICANT: "email/non_selected_applicant.html",
    NotificationLog.NotificationType.SELECTION_UNDER_REVIEW: "email/selection_under_review.html",
    NotificationLog.NotificationType.APPLICANT_CONTACT_UPDATED: "email/applicant_contact_updated.html",
    NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST: "email/document_resubmission_request.html",
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
# RD-8: the checklist email reads the completion record's deadline (one source);
# the client-confirmed two-week default lives on the completion tracking form.


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
    if application is not None and is_blind_viewer(recipient):
        # The panel never learns who a candidate is, not even from a notice title.
        title = blind_text(title, application)
        body = blind_text(body, application)
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
    return f"{deadline:%B} {deadline.day}, {deadline.year}"


def _format_schedule(value):
    if not value:
        return ""
    local = timezone.localtime(value)
    return f"{local:%B} {local.day}, {local.year} at {local:%I:%M %p}".replace(" at 0", " at ")


def build_applicant_status_url(application):
    path = reverse("applicant-status-link", kwargs={"token": application.public_token})
    base_url = (getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def build_applicant_resubmit_url(application):
    """Absolute link to the page where the applicant re-uploads returned documents."""
    path = reverse("applicant-resubmit", kwargs={"token": application.public_token})
    base_url = (getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def build_applicant_otp_url(application):
    """Absolute link back to the OTP verification page, so an applicant who closed
    the tab after intake can return, enter the code, and finish submitting."""
    path = reverse("applicant-otp", kwargs={"token": application.public_token})
    base_url = (getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or "").strip().rstrip("/")
    return f"{base_url}{path}" if base_url else path


def applicant_email_language(application):
    """The language for an applicant's emails: the one they used on the portal."""
    return (getattr(application, "preferred_language", "") or settings.LANGUAGE_CODE)


def _in_applicant_language(func):
    """Build (and queue) an applicant email in the applicant's portal language."""

    @functools.wraps(func)
    def wrapper(application, *args, **kwargs):
        with translation.override(applicant_email_language(application)):
            return func(application, *args, **kwargs)

    return wrapper


def _append_status_link(lines, application):
    lines.extend(
        [
            "",
            gettext("Check your application status anytime: %(link)s")
            % {"link": build_applicant_status_url(application)},
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
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("Your application has been received by the DOH–CHD CALABARZON Recruitment Portal."),
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        gettext("Position: %(title)s") % {"title": application.position.title},
        gettext("Position type: %(type)s") % {"type": applicant_branch_label(application)},
        gettext("Current status: %(status)s") % {"status": applicant_status_label(application)},
        "",
        gettext("Your private Tracking Code: %(code)s") % {"code": application.tracking_code},
        gettext(
            "Keep it private. You can also see every application you made on My Applications "
            "with just your email."
        ),
    ]
    _append_status_link(lines, application)
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal application received: %(ref)s")
        % {"ref": application.reference_number},
        "\n".join(lines),
    )


def _build_selected_notification(application):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("You have been selected for %(title)s under %(branch)s recruitment.")
        % {"title": application.position.title, "branch": application.position.get_branch_display()},
        gettext("The next step is %(step)s within the scope of this recruitment process.")
        % {"step": _completion_label(application).lower()},
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        "",
        gettext("Please wait for the requirement checklist or any additional office instructions."),
    ]
    _append_status_link(lines, application)
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal application result: %(title)s")
        % {"title": application.position.title},
        "\n".join(lines),
    )


def _build_non_selected_notification(application, cut_at_screening=False, exam_status=""):
    # exam_status carries ExamRecord.ExamStatus values ("absent" / "waived") when the
    # application was closed at exam finalization because the candidate did not take
    # (or was waived from) the scheduled examination (judgment item A).
    names = {"title": application.position.title, "branch": application.position.get_branch_display()}
    other_positions = gettext(
        "Thank you for your interest. You are welcome to apply for other open positions "
        "that match your qualifications."
    )
    if exam_status == "absent":
        result_line = gettext(
            "Your application for %(title)s under %(branch)s recruitment did not proceed "
            "because you did not take the scheduled examination."
        ) % names
        closing = other_positions
    elif exam_status == "waived":
        result_line = gettext(
            "For your application for %(title)s under %(branch)s recruitment, "
            "your examination was waived and the application did not proceed."
        ) % names
        closing = other_positions
    elif cut_at_screening:
        result_line = gettext(
            "After the initial screening of qualifications for %(title)s under %(branch)s "
            "recruitment, your application did not meet the Qualification Standards (QS) for "
            "this position and will not proceed to the examination."
        ) % names
        closing = other_positions
    else:
        result_line = gettext(
            "Your application for %(title)s under %(branch)s recruitment was not selected."
        ) % names
        closing = gettext("Thank you for your interest in this recruitment opportunity.")
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        result_line,
    ]
    if cut_at_screening:
        screening = (
            ScreeningRecord.objects.filter(
                application=application,
                is_finalized=True,
                qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
            )
            .order_by("-updated_at", "-pk")
            .first()
        )
        deficient_count = (
            ScreeningDocumentReview.objects.filter(
                screening_record=screening,
                status=(
                    ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                ),
            ).count()
            if screening
            else 0
        )
        if deficient_count:
            lines.extend(
                [
                    "",
                    gettext("The review also noted deficiencies in %(count)s submitted document(s).")
                    % {"count": deficient_count},
                ]
            )
    lines.extend(
        [
            gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
            "",
            closing,
        ]
    )
    _append_status_link(lines, application)
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal application result: %(title)s")
        % {"title": application.position.title},
        "\n".join(lines),
    )


def _build_selection_under_review_notification(application):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("The earlier selection notice for your application for %(title)s is no longer final.")
        % {"title": application.position.title},
        gettext(
            "The office is re-deliberating the vacancy. You will be informed when a "
            "new decision is recorded."
        ),
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
    ]
    _append_status_link(lines, application)
    return (
        gettext("Update on your application for %(title)s") % {"title": application.position.title},
        "\n".join(lines),
    )


def _build_applicant_contact_updated_notification(application, *, is_new_address):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("The contact information on your recruitment application was corrected by the office."),
    ]
    if is_new_address:
        lines.append(gettext("Future recruitment notices will be sent using the updated contact details."))
    else:
        lines.append(gettext("Future recruitment notices will no longer be sent to this email address."))
    lines.extend(
        [
            gettext("If you did not request or expect this correction, contact the HRM Unit promptly."),
            gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        ]
    )
    _append_status_link(lines, application)
    return (
        gettext("Contact information updated for %(title)s") % {"title": application.position.title},
        "\n".join(lines),
    )


def _build_interview_session_scheduled_notification(application, interview_session, recipient):
    recipient_name = recipient.get_full_name() or recipient.username
    lines = [
        f"Dear {recipient_name},",
        "",
        "An interview session has been scheduled and is ready for panel rating.",
        f"Candidate: {_panel_candidate_line(application, recipient)}",
        f"Position: {application.position.title}",
        f"Schedule: {_format_schedule(interview_session.scheduled_for)}",
        f"Location / medium: {interview_session.location}",
    ]
    if interview_session.session_notes:
        lines.extend(["", "Session notes:", interview_session.session_notes.strip()])
    lines.extend(
        [
            "",
            "Please sign in to the recruitment portal to review the case and submit your interview rating.",
        ]
    )
    return (
        f"DOH–CHD CALABARZON Recruitment Portal interview scheduled: {application.position.title}",
        "\n".join(lines),
    )


def _build_document_resubmission_fallback_body(
    application,
    requested_documents,
    workflow_remarks="",
    resubmission_deadline=None,
    deadline_was_extended=False,
):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext(
            "Some documents in your application for %(title)s need to be corrected and "
            "uploaded again."
        )
        % {"title": application.position.title},
        "",
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        "",
        ngettext(
            "Please review the document item below:",
            "Please review the document items below:",
            len(requested_documents),
        ),
    ]
    for document in requested_documents:
        lines.append(f"- {document['requirement_title']}")
        lines.append("  " + gettext("Instruction: %(remarks)s") % {"remarks": document["remarks"]})
    if resubmission_deadline:
        lines.extend(
            [
                "",
                (
                    gettext("The resubmission deadline has been extended. ")
                    if deadline_was_extended
                    else ""
                )
                + gettext("Please resubmit on or before %(date)s.")
                % {"date": _format_deadline(resubmission_deadline)},
            ]
        )
    remarks = (workflow_remarks or "").strip()
    if remarks:
        lines.extend(["", gettext("Additional note from the recruitment team:"), remarks])
    lines.extend(
        [
            "",
            gettext("Upload the corrected documents here: %(link)s")
            % {"link": build_applicant_resubmit_url(application)},
        ]
    )
    return "\n".join(lines)


def _build_document_resubmission_request_notification(
    application,
    document_reviews,
    workflow_remarks="",
    resubmission_deadline=None,
    deadline_was_extended=False,
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
                "resubmit_link": build_applicant_resubmit_url(application),
                "resubmission_deadline": resubmission_deadline,
                "deadline_was_extended": deadline_was_extended,
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
            deadline_was_extended=deadline_was_extended,
        )
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal document resubmission needed: %(title)s")
        % {"title": application.position.title},
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
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("You have been selected for %(title)s. Please complete the following %(what)s requirements:")
        % {"title": application.position.title, "what": _completion_label(application).lower()},
        checklist_items.strip(),
    ]
    if deadline:
        lines.extend(["", gettext("Submission deadline: %(date)s") % {"date": _format_deadline(deadline)}])
    if additional_message:
        lines.extend(["", additional_message.strip()])
    lines.extend(
        [
            "",
            gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
            gettext("Check your application status anytime: %(link)s")
            % {"link": build_applicant_status_url(application)},
        ]
    )
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal requirement checklist: %(title)s")
        % {"title": application.position.title},
        "\n".join(lines),
    )


def _build_reminder_notification(application, reminder_subject, reminder_message, deadline=None):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        reminder_message.strip(),
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        gettext("Current status: %(status)s") % {"status": applicant_status_label(application)},
    ]
    if deadline:
        lines.append(gettext("Reminder deadline: %(date)s") % {"date": _format_deadline(deadline)})
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
    # REV-D: failures are anomalies and stay on the audit trail — but without
    # copying the recipient's email address into a second store; the
    # notification record (referenced by id) already holds it.
    _record_notification_audit(
        application=notification.application,
        actor=notification.triggered_by,
        action=AuditLog.Action.NOTIFICATION_FAILED,
        description=f"Failed to send {notification.get_notification_type_display().lower()}.",
        metadata={
            "notification_id": notification.id,
            "notification_type": notification.notification_type,
            "reason": reason,
        },
    )
    # RD-9: surface the failure where work happens — an in-app bell for the
    # current handler. Local import: services imports this module at module
    # level, so the reverse import must stay inside the function.
    try:
        from .services import emit_notification_failed_in_app_alert

        emit_notification_failed_in_app_alert(notification)
    except Exception:
        # The bell is best-effort; a failure to alert must never mask the
        # already-recorded delivery failure.
        logger.exception(
            "Failed to emit the in-app alert for failed notification %s.",
            notification.id,
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
        "resubmit_link": build_applicant_resubmit_url(application),
        "status_label": applicant_status_label(application),
        "position_type_label": applicant_branch_label(application),
        "meta": metadata,
        "reporting_instructions": REPORTING_INSTRUCTIONS,
        **email_branding_context(audience),
    }
    language = (
        applicant_email_language(application) if audience == "applicant" else settings.LANGUAGE_CODE
    )
    try:
        with translation.override(language):
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
    except Exception as exc:
        logger.exception("Failed to deliver notification %s.", notification.id)
        return _mark_notification_failed(
            notification,
            summarize_delivery_failure(exc),
        )

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
    # REV-D: routine sends are NOT double-recorded in the audit log — the
    # notifications table is the source of truth for delivery history.
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


RESEND_REPEAT_WINDOW = timedelta(minutes=1)


@transaction.atomic
def resend_notification(notification_log, actor):
    """RD-9: re-deliver a previously queued applicant email, verbatim.

    A resend is a redelivery, not a re-render — the stored subject/body are
    the content the office believes was sent (the footer is already baked in,
    so this deliberately bypasses queue_notification). The recipient email is
    re-resolved from the application so a corrected address store takes
    effect; it falls back to the original address. A deliberate act, so it is
    audited (REV-D's no-routine-send-rows rule is about automatic sends).
    """
    if actor.role not in NOTIFICATION_MANAGER_ROLES:
        raise ValueError("Only Secretariat or HRM Chief may resend applicant emails.")
    if _email_audience_for(notification_log.notification_type) != "applicant":
        # The recipient below is re-resolved from the application, so resending a staff
        # notice would deliver an internal email to the applicant.
        raise ValueError("Only emails sent to the applicant can be sent again from here.")
    if notification_log.notification_type in {
        NotificationLog.NotificationType.EXAM_INVITATION,
        NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
    }:
        scheduled_for = parse_datetime((notification_log.metadata or {}).get("scheduled_for") or "")
        if scheduled_for and scheduled_for < timezone.now():
            raise ValueError(
                "This schedule has already passed, so its notice can't be sent again. "
                "Schedule a new date if the applicant still needs to attend."
            )
    # A double click used to queue two copies. Lock the email being resent so a second,
    # simultaneous click waits, then sees the copy the first one queued.
    NotificationLog.objects.select_for_update().filter(pk=notification_log.pk).exists()
    if NotificationLog.objects.filter(
        metadata__resent_from=notification_log.pk,
        created_at__gte=timezone.now() - RESEND_REPEAT_WINDOW,
    ).exists():
        raise ValueError(
            f"{notification_log.get_notification_type_display()} was just sent again. "
            "Check its status in the list before sending another copy."
        )
    application = notification_log.application
    recipient_email = (
        (_recipient_email(application) or "").strip().lower()
        or notification_log.recipient_email
    )
    resent = NotificationLog.objects.create(
        application=application,
        recruitment_case=getattr(application, "case", None),
        triggered_by=actor,
        triggered_by_role=getattr(actor, "role", ""),
        notification_type=notification_log.notification_type,
        delivery_channel=NotificationLog.DeliveryChannel.EMAIL,
        delivery_status=NotificationLog.DeliveryStatus.PENDING,
        related_status=application.status,
        recipient_name=notification_log.recipient_name,
        recipient_email=recipient_email or "missing-email@invalid.local",
        subject=notification_log.subject,
        body=notification_log.body,
        metadata={**(notification_log.metadata or {}), "resent_from": notification_log.id},
    )
    _record_notification_audit(
        application=application,
        actor=actor,
        action=AuditLog.Action.NOTIFICATION_RESENT,
        description=(
            f"Resent {notification_log.get_notification_type_display().lower()} "
            "to the applicant."
        ),
        metadata={
            "notification_id": resent.id,
            "resent_from": notification_log.id,
            "notification_type": notification_log.notification_type,
        },
    )
    if not recipient_email:
        return _mark_notification_failed(
            resent,
            "No recipient email address is available for this notification.",
        )
    transaction.on_commit(lambda: _deliver_notification(resent.id))
    return resent


@_in_applicant_language
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


@_in_applicant_language
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


@_in_applicant_language
def queue_non_selected_applicant_notification(
    application, actor=None, cut_at_screening=False, exam_status=""
):
    subject, body = _build_non_selected_notification(
        application, cut_at_screening=cut_at_screening, exam_status=exam_status
    )
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
            "exam_status": exam_status,
            "status_link": build_applicant_status_url(application),
        },
    )


@_in_applicant_language
def queue_selection_under_review_notification(application, actor=None):
    subject, body = _build_selection_under_review_notification(application)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.SELECTION_UNDER_REVIEW,
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


@_in_applicant_language
def queue_applicant_contact_updated_notifications(
    application,
    *,
    actor=None,
    old_email,
    new_email,
):
    notifications = []
    old_email = (old_email or "").strip().lower()
    new_email = (new_email or "").strip().lower()
    if old_email and old_email != new_email:
        subject, body = _build_applicant_contact_updated_notification(
            application,
            is_new_address=False,
        )
        notifications.append(
            queue_notification(
                application,
                notification_type=NotificationLog.NotificationType.APPLICANT_CONTACT_UPDATED,
                actor=actor,
                subject=subject,
                body=body,
                recipient_email=old_email,
                metadata={
                    "reference_number": application.reference_number,
                    "status_link": build_applicant_status_url(application),
                    "copy": "previous_address",
                },
            )
        )
    subject, body = _build_applicant_contact_updated_notification(
        application,
        is_new_address=True,
    )
    notifications.append(
        queue_notification(
            application,
            notification_type=NotificationLog.NotificationType.APPLICANT_CONTACT_UPDATED,
            actor=actor,
            subject=subject,
            body=body,
            recipient_email=new_email,
            metadata={
                "reference_number": application.reference_number,
                "status_link": build_applicant_status_url(application),
                "copy": "updated_address",
                "new_email": new_email,
            },
        )
    )
    return notifications


def _build_withdrawal_acknowledgement(application, reason=""):
    """SL-10: confirm the applicant's own withdrawal.

    This is not a rejection notice and must not read like one — the applicant left of
    their own accord, and the copy says exactly that."""
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext(
            "We have recorded your withdrawal from the recruitment for %(title)s under "
            "%(branch)s recruitment, as you requested."
        )
        % {"title": application.position.title, "branch": application.position.get_branch_display()},
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        "",
        gettext(
            "No further action is needed from you for this application, and your record "
            "is closed as withdrawn — not as a rejection."
        ),
        "",
        gettext(
            "Thank you for your interest. You are welcome to apply for other open "
            "positions that match your qualifications."
        ),
    ]
    _append_status_link(lines, application)
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal: withdrawal recorded for %(title)s")
        % {"title": application.position.title},
        "\n".join(lines),
    )


@_in_applicant_language
def queue_withdrawal_acknowledgement_notification(application, actor=None, reason=""):
    subject, body = _build_withdrawal_acknowledgement(application, reason=reason)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.WITHDRAWAL_ACKNOWLEDGEMENT,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "reason": reason,
            "status_link": build_applicant_status_url(application),
        },
    )


def _build_vacancy_cancellation_notice(application, reason=""):
    """SL-11: the vacancy was cancelled. The applicant was never judged, and the notice
    must not read as though they were."""
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext(
            "The recruitment for %(title)s under %(branch)s recruitment has been cancelled "
            "by the office, so your application will not proceed."
        )
        % {"title": application.position.title, "branch": application.position.get_branch_display()},
        gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
        "",
        gettext(
            "This is not an assessment of your qualifications — no candidate was selected "
            "for this vacancy."
        ),
    ]
    if reason:
        lines.extend(["", gettext("Reason: %(reason)s") % {"reason": reason}])
    lines.extend(
        [
            "",
            gettext(
                "Thank you for your interest. You are welcome to apply for other open "
                "positions, including this one should it be published again."
            ),
        ]
    )
    _append_status_link(lines, application)
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal: %(title)s recruitment cancelled")
        % {"title": application.position.title},
        "\n".join(lines),
    )


@_in_applicant_language
def queue_vacancy_cancellation_notification(application, actor=None, reason=""):
    subject, body = _build_vacancy_cancellation_notice(application, reason=reason)
    return queue_notification(
        application,
        notification_type=NotificationLog.NotificationType.VACANCY_CANCELLED,
        actor=actor,
        subject=subject,
        body=body,
        metadata={
            "reference_number": application.reference_number,
            "status": application.status,
            "branch": application.branch,
            "reason": reason,
            "status_link": build_applicant_status_url(application),
        },
    )


def _panel_candidate_line(application, recipient):
    """How a candidate is named in an email to this recipient. The HRMPSB panel reviews
    blind (REV-B): its pages show only the candidate code, and so must its email."""
    if is_blind_viewer(recipient):
        return application.candidate_code_label
    return (
        f"{application.candidate_code_label} — {application.applicant_display_name}"
        f" ({application.reference_number})"
    )


def _build_interview_batch_panel_digest(posting, applications, interview_session, recipient):
    """WF-14: one digest per panel member per batch scheduling action — the whole pool
    shares the date/venue, so one email lists every candidate instead of N copies."""
    recipient_name = recipient.get_full_name() or recipient.username
    candidate_count = len(applications)
    lines = [
        f"Dear {recipient_name},",
        "",
        (
            f"An interview has been scheduled for {posting.title}: "
            f"{candidate_count} candidate{'' if candidate_count == 1 else 's'} "
            "will be interviewed and are ready for panel rating."
        ),
        f"Schedule: {_format_schedule(interview_session.scheduled_for)}",
        f"Location / medium: {interview_session.location}",
        "",
        "Candidates:",
    ]
    for application in applications:
        lines.append(f"- {_panel_candidate_line(application, recipient)}")
    if interview_session.session_notes:
        lines.extend(["", "Session notes:", interview_session.session_notes.strip()])
    lines.extend(
        [
            "",
            "Please sign in to the recruitment portal to review each case and submit your interview ratings.",
        ]
    )
    return (
        f"DOH–CHD CALABARZON Recruitment Portal interview schedule: {posting.title} ({candidate_count} candidate{'' if candidate_count == 1 else 's'})",
        "\n".join(lines),
    )


def queue_interview_batch_panel_digest_notifications(
    posting,
    applications,
    interview_session,
    recipients,
    actor=None,
):
    """WF-14: one digest email per panel member for a batch scheduling action. Logged
    against the batch's first application (the digest concerns the whole vacancy pool)."""
    notifications = []
    for recipient in recipients:
        subject, body = _build_interview_batch_panel_digest(
            posting,
            applications,
            interview_session,
            recipient,
        )
        notifications.append(
            queue_notification(
                applications[0],
                notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
                actor=actor,
                subject=subject,
                body=body,
                recipient_name=recipient.get_full_name() or recipient.username,
                recipient_email=recipient.email,
                metadata={
                    "recruitment_entry_id": posting.id,
                    "candidate_count": len(applications),
                    "batch_digest": True,
                    "candidate_references": [
                        application.reference_number for application in applications
                    ],
                    "candidate_labels": [
                        _panel_candidate_line(application, recipient)
                        for application in applications
                    ],
                    "position_title": posting.title,
                    "scheduled_for": interview_session.scheduled_for.isoformat(),
                    "schedule_display": _format_schedule(interview_session.scheduled_for),
                    "location": interview_session.location,
                    "session_notes": (interview_session.session_notes or "").strip(),
                    "recipient_user_id": recipient.id,
                    "recipient_role": recipient.role,
                },
            )
        )
    return notifications


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
                    "candidate_labels": [_panel_candidate_line(application, recipient)],
                    "position_title": application.position.title,
                    "recipient_user_id": recipient.id,
                    "recipient_role": recipient.role,
                },
            )
        )
    return notifications


def _build_exam_invitation_notification(application, exam_schedule, changed=False):
    intro = (
        gettext(
            "The examination for %(title)s has a new schedule. This replaces the schedule we "
            "sent you earlier. Please note the new details below:"
        )
        if changed
        else gettext(
            "Good news. You passed the initial screening for %(title)s "
            "and are invited to take the examination. Please note the details below:"
        )
    )
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        intro % {"title": application.position.title},
        "",
        gettext("Schedule: %(when)s") % {"when": _format_schedule(exam_schedule.scheduled_for)},
        gettext("Venue: %(venue)s") % {"venue": exam_schedule.venue},
    ]
    if exam_schedule.instructions:
        lines.extend(["", gettext("Instructions:"), exam_schedule.instructions.strip()])
    lines.append("")
    lines.append(gettext("Reporting instructions:"))
    lines.extend(f"- {item}" for item in REPORTING_INSTRUCTIONS)
    lines.extend(
        [
            "",
            gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
            gettext("Check your application status anytime: %(link)s")
            % {"link": build_applicant_status_url(application)},
        ]
    )
    subject = (
        gettext("DOH–CHD CALABARZON Recruitment Portal exam schedule changed: %(title)s")
        if changed
        else gettext("DOH–CHD CALABARZON Recruitment Portal exam invitation: %(title)s")
    )
    return subject % {"title": application.position.title}, "\n".join(lines)


@_in_applicant_language
def queue_exam_invitation_notification(application, exam_schedule, actor=None, changed=False):
    subject, body = _build_exam_invitation_notification(application, exam_schedule, changed=changed)
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
            "schedule_changed": changed,
        },
    )


def _build_applicant_interview_notice_notification(application, interview_session):
    lines = [
        gettext("Dear %(name)s,") % {"name": application.applicant_display_name},
        "",
        gettext("You are scheduled for an interview for %(title)s. Please note the details below:")
        % {"title": application.position.title},
        "",
        gettext("Schedule: %(when)s") % {"when": _format_schedule(interview_session.scheduled_for)},
        gettext("Location / medium: %(place)s") % {"place": interview_session.location},
    ]
    lines.append("")
    lines.append(gettext("Reporting instructions:"))
    lines.extend(f"- {item}" for item in REPORTING_INSTRUCTIONS)
    lines.extend(
        [
            "",
            gettext("Case No. %(ref)s") % {"ref": application.case_number_short or application.reference_label},
            gettext("Check your application status anytime: %(link)s")
            % {"link": build_applicant_status_url(application)},
        ]
    )
    return (
        gettext("DOH–CHD CALABARZON Recruitment Portal interview schedule: %(title)s")
        % {"title": application.position.title},
        "\n".join(lines),
    )


@_in_applicant_language
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


@_in_applicant_language
def queue_document_resubmission_request_notification(
    application,
    actor=None,
    *,
    document_reviews,
    workflow_remarks="",
    resubmission_deadline=None,
    deadline_was_extended=False,
):
    document_reviews = list(document_reviews or [])
    if not document_reviews:
        raise ValueError("At least one document review row is required for a resubmission request.")
    subject, body, requested_documents = _build_document_resubmission_request_notification(
        application,
        document_reviews,
        workflow_remarks=workflow_remarks,
        resubmission_deadline=resubmission_deadline,
        deadline_was_extended=deadline_was_extended,
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
            "resubmission_deadline_display": (
                _format_deadline(resubmission_deadline)
                if resubmission_deadline
                else ""
            ),
            "deadline_was_extended": deadline_was_extended,
        },
    )


@_in_applicant_language
def send_requirement_checklist_notification(
    application,
    actor,
    *,
    additional_message="",
):
    """RD-8: the emailed checklist is built from the completion record's
    requirement rows and the record's deadline — one source of truth. The
    handler no longer re-types the list (the old free-text path let the email
    and the tracked rows silently disagree) and the applicant is told the same
    deadline the office tracks. Pending rows only: the email is an ask, not an
    inventory of what was already received.
    """
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

    completion_record = getattr(application, "completion_record", None)
    if completion_record is None:
        raise ValueError(
            "Record the completion tracking details (with the requirement checklist) "
            "before sending the checklist email."
        )
    pending_requirements = list(
        completion_record.requirements.filter(
            status=CompletionRequirement.RequirementStatus.PENDING
        ).order_by("display_order", "created_at")
    )
    if not completion_record.requirements.exists():
        raise ValueError(
            "Add at least one completion requirement item before sending the checklist email."
        )
    if not pending_requirements:
        raise ValueError(
            "Every requirement is already resolved — there is nothing to ask the applicant for."
        )
    deadline = completion_record.deadline
    if not deadline:
        raise ValueError(
            "Set the completion deadline on the tracking form before sending the checklist email."
        )

    checklist_items = "\n".join(
        f"• {requirement.item_label}" for requirement in pending_requirements
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
            "completion_record_id": completion_record.id,
            "status_link": build_applicant_status_url(application),
        },
    )


@_in_applicant_language
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
