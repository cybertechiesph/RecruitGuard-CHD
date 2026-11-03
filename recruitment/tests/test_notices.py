"""Email and in-app notifications."""

import io
import json
import smtplib
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from ..models import (
    AuditLog,
    CompletionRecord,
    CompletionRequirement,
    EvidenceVaultItem,
    Notification,
    NotificationLog,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from ..requirements import get_applicant_document_requirements
from ..services import (
    get_current_applicant_document_map,
    emit_deadline_approaching_notifications,
    emit_notification_failed_in_app_alert,
    extend_document_resubmission_deadline,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    process_workflow_action,
    save_interview_session,
    submit_application,
    submit_document_resubmission,
    update_applicant_contact,
    user_can_update_applicant_contact,
    user_can_view_application,
    verify_application_otp,
)
from .base import User, BaseRecruitmentTestCase


class NotificationManagementTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def make_approved_cos_application(self):
        return self.make_selected_application(self.cos_position)

    def make_approved_level2_plantilla_application(self):
        return self.make_selected_application(self.level2_position)

    def test_owning_office_corrects_contact_with_audit_dual_notice_and_fresh_otp(self):
        application = self.make_submitted_application()
        old_email = application.applicant_email
        new_email = "corrected.applicant@example.com"
        application.otp_requested_at = timezone.now() - timedelta(minutes=30)
        application.save(update_fields=["otp_requested_at", "updated_at"])
        old_otp = issue_application_otp(
            application,
            actor=application.applicant,
            purpose="status_lookup",
        )

        self.assertTrue(user_can_update_applicant_contact(self.secretariat, application))
        self.assertFalse(user_can_update_applicant_contact(self.hrm_chief, application))
        with self.assertRaisesMessage(
            ValueError,
            "Only the case's owning office may correct applicant contact details.",
        ):
            update_applicant_contact(
                application,
                self.hrm_chief,
                new_email,
                "09181234567",
                "Applicant supplied a corrected address.",
            )

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            updated, notices = update_applicant_contact(
                application,
                self.secretariat,
                new_email,
                "0918 123 4567",
                "Applicant supplied a corrected address.",
            )
        updated.refresh_from_db()
        self.assertEqual(updated.applicant_email, new_email)
        self.assertEqual(updated.applicant_phone, "+639181234567")
        self.assertEqual(len(notices), 2)
        logs = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.APPLICANT_CONTACT_UPDATED,
        )
        self.assertEqual(set(logs.values_list("recipient_email", flat=True)), {old_email, new_email})
        old_copy = logs.get(recipient_email=old_email)
        self.assertNotIn(new_email, old_copy.body)
        self.assertNotIn(new_email, json.dumps(old_copy.metadata))
        old_message = next(message for message in mail.outbox if old_email in message.to)
        self.assertNotIn(new_email, old_message.body)
        self.assertTrue(
            all(new_email not in alternative[0] for alternative in old_message.alternatives)
        )
        audit = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.APPLICANT_CONTACT_UPDATED,
        )
        self.assertEqual(audit.metadata["old_email"], old_email)
        self.assertEqual(audit.metadata["new_email"], new_email)
        self.assertIn("corrected address", audit.metadata["reason"])

        with self.assertRaises(ValueError):
            verify_application_otp(application, old_otp, purpose="status_lookup")
        application.otp_requested_at = timezone.now() - timedelta(minutes=30)
        application.save(update_fields=["otp_requested_at", "updated_at"])
        fresh_otp = issue_application_otp(
            application,
            actor=application.applicant,
            purpose="status_lookup",
        )
        verify_application_otp(application, fresh_otp, purpose="status_lookup")

        status_client = Client()
        old_lookup = status_client.post(
            reverse("applicant-status-lookup"),
            {"tracking_code": application.tracking_code, "email": old_email},
        )
        self.assertContains(old_lookup, "We could not find an application")
        application.otp_requested_at = timezone.now() - timedelta(minutes=30)
        application.save(update_fields=["otp_requested_at", "updated_at"])
        new_lookup = status_client.post(
            reverse("applicant-status-lookup"),
            {"tracking_code": application.tracking_code, "email": new_email},
        )
        self.assertContains(new_lookup, "Check Your Email")

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        detail = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertContains(detail, "Update contact details")
        self.assertContains(detail, reverse("applicant-contact-update", args=[application.pk]))

    def test_contact_correction_permission_follows_level_and_closes_with_the_case(self):
        level2 = self.make_submitted_application(self.level2_position)
        self.assertTrue(user_can_update_applicant_contact(self.hrm_chief, level2))
        self.assertFalse(user_can_update_applicant_contact(self.secretariat, level2))
        level2.case.current_stage = RecruitmentCase.Stage.CLOSED
        level2.case.save(update_fields=["current_stage", "updated_at"])
        self.assertFalse(user_can_update_applicant_contact(self.hrm_chief, level2))

    def test_contact_correction_refuses_unchanged_and_duplicate_active_email(self):
        application = self.make_submitted_application()
        application.applicant_phone = "+639171234567"
        application.save(update_fields=["applicant_phone", "updated_at"])
        with self.assertRaisesMessage(ValueError, "contact details are unchanged"):
            update_applicant_contact(
                application,
                self.secretariat,
                application.applicant_email,
                application.applicant_phone,
                "No effective change.",
            )

        other_applicant = User.objects.create_user(
            username="contact-duplicate-applicant",
            email="contact.duplicate@example.com",
            role=RecruitmentUser.Role.APPLICANT,
            is_active=False,
        )
        RecruitmentApplication.objects.create(
            applicant=other_applicant,
            position=self.level1_position,
            status=RecruitmentApplication.Status.DRAFT,
            applicant_first_name="Other",
            applicant_last_name="Applicant",
            applicant_email="contact.duplicate@example.com",
            applicant_phone="+639171110000",
            qualification_summary="Other application.",
        )
        with self.assertRaisesMessage(ValueError, "already uses that email address"):
            update_applicant_contact(
                application,
                self.secretariat,
                "contact.duplicate@example.com",
                application.applicant_phone,
                "Applicant supplied a duplicate address.",
            )

    def test_initial_submission_creates_in_app_case_assignment(self):
        application = self.make_submitted_application()

        notification = Notification.objects.get(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.CASE_ASSIGNED,
        )
        self.assertEqual(notification.read_at, None)
        self.assertIn(application.reference_label, notification.title)
        self.assertIn("?tab=screening", notification.related_url)
        self.assertFalse(
            Notification.objects.filter(application=application, recipient=self.hrm_chief).exists()
        )

    def test_notification_endpoints_are_recipient_scoped(self):
        application = self.make_submitted_application()
        notification = Notification.objects.get(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.CASE_ASSIGNED,
        )
        other_notification = Notification.objects.create(
            application=application,
            recipient=self.hrm_chief,
            kind=Notification.Kind.CASE_ASSIGNED,
            title="Other user's notification",
            related_url=reverse("application-detail", kwargs={"pk": application.pk}),
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("notification-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, notification.title)
        response = client.get(reverse("notification-unread-count"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"count": 1})

        response = client.post(reverse("notification-read", kwargs={"pk": notification.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("application-detail", kwargs={"pk": application.pk}), response["Location"])
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

        next_url = reverse("workflow-queue")
        notification.read_at = None
        notification.save(update_fields=["read_at", "updated_at"])
        response = client.post(
            reverse("notification-read", kwargs={"pk": notification.pk}),
            {"next": next_url},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], next_url)

        response = client.post(reverse("notification-read", kwargs={"pk": other_notification.pk}))
        self.assertEqual(response.status_code, 404)
        other_notification.refresh_from_db()
        self.assertIsNone(other_notification.read_at)

    def test_mark_all_notifications_marks_only_current_user(self):
        application = self.make_submitted_application()
        Notification.objects.create(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.SCREENING_FINALIZED,
            title="Screening finalized",
        )
        other_notification = Notification.objects.create(
            application=application,
            recipient=self.hrm_chief,
            kind=Notification.Kind.CASE_ASSIGNED,
            title="Other user's notification",
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        next_url = reverse("notification-list")
        response = client.post(reverse("notification-read-all"), {"next": next_url})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], next_url)

        self.assertEqual(
            Notification.objects.filter(recipient=self.secretariat, read_at__isnull=True).count(),
            0,
        )
        other_notification.refresh_from_db()
        self.assertIsNone(other_notification.read_at)

    def test_level2_handoff_notifies_target_office_without_resetting_stage_clock(self):
        application = self.make_submitted_application(self.level2_position)
        Notification.objects.filter(application=application).delete()
        old_stage_entered_at = timezone.now() - timedelta(days=3)
        application.case.stage_entered_at = old_stage_entered_at
        application.case.save(update_fields=["stage_entered_at", "updated_at"])

        grant_secretariat_override(
            application=application,
            actor=self.hrm_chief,
            reason="Secretariat document verification needed.",
        )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.stage_entered_at, old_stage_entered_at)
        notification = Notification.objects.get(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.CASE_ASSIGNED,
        )
        self.assertIn("handed off", notification.title)

    def test_deadline_approaching_notifications_emit_once_per_day(self):
        application = self.make_submitted_application()
        application.position.closing_date = timezone.localdate() + timedelta(days=1)
        application.position.save(update_fields=["closing_date", "updated_at"])
        Notification.objects.filter(application=application).delete()

        emitted = emit_deadline_approaching_notifications()
        emitted_again = emit_deadline_approaching_notifications()

        self.assertEqual(len(emitted), 1)
        self.assertEqual(len(emitted_again), 0)
        notification = emitted[0]
        self.assertEqual(notification.recipient, self.secretariat)
        self.assertEqual(notification.kind, Notification.Kind.DEADLINE_APPROACHING)
        self.assertIn("closing in 24 hours", notification.title)

    def test_submission_acknowledgment_notification_is_sent_and_stored(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        )
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.recipient_email, "applicant@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"Case No. {application.case_number_short}", mail.outbox[0].subject)
        status_link = reverse("applicant-status-link", kwargs={"token": application.public_token})
        self.assertIn(status_link, mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        html_body, mime_type = mail.outbox[0].alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Application Received", html_body)
        self.assertIn("DOH–CHD CALABARZON", html_body)
        # REV-G dual-ID email: labeled short case number + private tracking code.
        self.assertIn(f"Case No. {application.case_number_short}", html_body)
        self.assertIn(application.tracking_code, html_body)
        self.assertIn(application.position.title, html_body)
        self.assertIn(status_link, html_body)
        self.assertIn(status_link, notification.metadata["status_link"])
        # REV-D: routine sends are no longer double-recorded in the audit log;
        # the notifications table is the delivery record.
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.NOTIFICATION_SENT,
            ).exists()
        )

    def test_submission_acknowledgment_falls_back_to_text_when_html_render_fails(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        mail.outbox.clear()

        # Simulate a missing/broken "email/application_received.html" template.
        # The only render_to_string call in the submission delivery path lives in
        # _render_notification_html, so this isolates the HTML render failure.
        with patch(
            "recruitment.notification_services.render_to_string",
            side_effect=RuntimeError("template render boom"),
        ), self.assertLogs("recruitment.notification_services", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, self.applicant)

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        )
        # Delivery still succeeds on the already-prepared plain-text body.
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(notification.failure_details, "")

        # The email went out, but degraded to text-only (no HTML alternative).
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].alternatives, [])
        status_link = reverse("applicant-status-link", kwargs={"token": application.public_token})
        self.assertIn(status_link, mail.outbox[0].body)

        # The render failure is logged rather than swallowed silently.
        self.assertTrue(
            any("Failed to render HTML email" in message for message in logs.output)
        )

        # REV-D: no audit row for a routine successful send — the notification
        # record above is the delivery record.
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.NOTIFICATION_SENT,
            ).exists()
        )

    def test_delivery_failure_redacts_raw_smtp_exception_text(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        mail.outbox.clear()

        failure = smtplib.SMTPConnectError(
            421,
            b"secret-host.example refused applicant@example.com",
        )
        with patch(
            "recruitment.notification_services.EmailMultiAlternatives.send",
            side_effect=failure,
        ), self.assertLogs("recruitment.notification_services", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, self.applicant)

        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        )
        self.assertEqual(
            notification.failure_details,
            "Could not reach the mail server (SMTPConnectError)",
        )
        self.assertEqual(
            notification.delivery_status,
            NotificationLog.DeliveryStatus.FAILED,
        )
        self.assertNotIn("secret-host", notification.failure_details)

        failed_audit = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.NOTIFICATION_FAILED,
        )
        self.assertNotIn("secret-host", str(failed_audit.metadata))
        self.assertEqual(failed_audit.metadata["notification_id"], notification.id)

    def test_delivery_failure_summaries_follow_exception_hierarchy(self):
        from recruitment.notification_services import summarize_delivery_failure

        cases = [
            (
                smtplib.SMTPAuthenticationError(535, b"x"),
                "Mail server rejected the sender credentials (SMTPAuthenticationError)",
            ),
            (
                smtplib.SMTPRecipientsRefused({"a@b": (550, b"x")}),
                "Mail server refused the recipient address (SMTPRecipientsRefused)",
            ),
            (
                smtplib.SMTPConnectError(421, b"x"),
                "Could not reach the mail server (SMTPConnectError)",
            ),
            (
                smtplib.SMTPServerDisconnected("x"),
                "Could not reach the mail server (SMTPServerDisconnected)",
            ),
            (
                smtplib.SMTPDataError(554, b"x"),
                "Mail server returned an error (SMTPDataError)",
            ),
            (
                smtplib.SMTPSenderRefused(550, b"x", "a@b"),
                "Mail server returned an error (SMTPSenderRefused)",
            ),
            (
                ConnectionRefusedError(),
                "Could not reach the mail server (ConnectionRefusedError)",
            ),
            (
                RuntimeError("Email backend did not confirm delivery."),
                "Email backend did not confirm delivery. (RuntimeError)",
            ),
            (
                ValueError("x"),
                "Unexpected error while sending (ValueError)",
            ),
        ]

        outputs = []
        for exception, expected in cases:
            with self.subTest(exception=type(exception).__name__):
                output = summarize_delivery_failure(exception)
                outputs.append(output)
                self.assertEqual(output, expected)

        self.assertTrue(all("a@b" not in output for output in outputs))

    def test_approval_sends_selected_applicant_notification(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        mail.outbox.clear()

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)

        # SL-01: the selection (and its applicant notice) happens when the vacancy
        # deliberation pick is finalized.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_deliberation_for_current_stage(application, self.hrm_chief)

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("application result", mail.outbox[0].subject.lower())
        self.assertIn("COS", mail.outbox[0].body)
        self.assertIn(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )

    def test_rejection_sends_non_selected_applicant_notification(self):
        application = self.make_submitted_application()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(application, self.secretariat, "reject", "Rejected at Secretariat.")

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("application result", mail.outbox[0].subject.lower())
        self.assertIn(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )

    def test_return_to_applicant_sends_document_resubmission_request_notification(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                "completeness_notes": "A corrected document is required.",
                "qualification_outcome": "",
                "screening_notes": "Screening cannot continue until the document is corrected.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "operation": "save",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please resubmit the corrected document.",
            )

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.secretariat)
        self.assertEqual(notification.metadata["document_keys"], [first_requirement.code])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("document resubmission needed", mail.outbox[0].subject.lower())
        self.assertIn(first_requirement.title, mail.outbox[0].body)
        self.assertIn("Upload a signed and readable copy.", mail.outbox[0].body)
        self.assertIn("Please resubmit the corrected document.", mail.outbox[0].body)
        self.assertIn(
            reverse("applicant-resubmit", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )

    def _finalize_screening_with_flagged_document(self, application, requirement):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                "completeness_notes": "A corrected document is required.",
                "qualification_outcome": ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                "screening_notes": "Screening cannot continue until the document is corrected.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{requirement.code}": "Upload a signed and readable copy.",
                "operation": "save",
            },
            follow=True,
        )

    def test_resubmission_request_keeps_case_visible_as_awaiting(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please resubmit the corrected document.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()
        # The case stays with the reviewing role (so it stays in the queue/console), unlocked,
        # awaiting the applicant — it is NOT handed off to the applicant and dropped.
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertFalse(application.case.is_stage_locked)
        self.assertEqual(application.case.stage_sla_state, "paused")
        self.assertIn(application, list(get_queue_for_user(self.secretariat)))
        # The applicant can still re-upload while it is returned.
        self.assertTrue(application.is_editable_by_applicant)
        # A resubmission deadline (default 2 weeks) is recorded and emailed.
        screening_record = ScreeningRecord.objects.get(
            application=application,
            review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        self.assertEqual(
            screening_record.resubmission_deadline,
            timezone.localdate() + timedelta(days=14),
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Please resubmit on or before", mail.outbox[0].body)

    def test_resubmission_succeeds_after_an_rd1_return_with_no_verdict(self):
        # Live E2E regression: the RD-1 "Return to applicant" decision deliberately saves
        # the screening WITHOUT a qualification verdict (forcing one on flagged documents is
        # the fake-verdict pattern RD-1 removed). The resubmission service then ran
        # full_clean() on that record, and the blank outcome 500'd the applicant's
        # resubmission POST. The old fixtures never caught it because they built the
        # returned state with a verdict already filled in.
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("screening-review", kwargs={"pk": application.pk}),
                {
                    "completeness_notes": "",
                    # RD-1: no verdict — the reviewer is returning, not deciding.
                    "qualification_outcome": "",
                    "screening_notes": "",
                    **self.screening_document_status_payload(
                        application,
                        overrides={
                            first_requirement.code: (
                                ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                            ),
                        },
                    ),
                    f"document_remarks__{first_requirement.code}": "Incomplete pages.",
                    "return_remarks": "Please upload the complete document.",
                    "operation": "return_to_applicant",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        )
        screening_record = ScreeningRecord.objects.get(
            application=application,
            review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        self.assertEqual(screening_record.qualification_outcome, "")

        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        uploaded_files = {
            first_requirement.code: self.build_valid_applicant_document_upload(
                first_requirement.code, content_prefix="rd1-resubmitted"
            )
        }
        with self.captureOnCommitCallbacks(execute=True):
            submit_document_resubmission(application, self.applicant, uploaded_files)

        application.refresh_from_db()
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(
            application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW
        )
        # Still verdict-less — the reviewer decides on the corrected evidence, and the
        # finalize path (which full_cleans) keeps requiring the verdict where it matters.
        screening_record.refresh_from_db()
        self.assertEqual(screening_record.qualification_outcome, "")
        self.assertFalse(screening_record.is_finalized)

    def test_otp_can_be_issued_for_awaiting_resubmission_application(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        # The applicant can now obtain + verify an OTP even though the application is submitted.
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        self.assertTrue(application.otp_is_currently_valid)

    def test_resubmission_emails_the_applicant_a_receipt(self):
        """Round E, P2-3: corrected documents get a confirmation in the inbox."""
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        mail.outbox.clear()
        uploaded_files = {
            first_requirement.code: self.build_valid_applicant_document_upload(
                first_requirement.code, content_prefix="resubmitted-receipt"
            )
        }
        with self.captureOnCommitCallbacks(execute=True):
            submit_document_resubmission(application, self.applicant, uploaded_files)

        notice = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_RECEIVED,
        )
        self.assertEqual(notice.recipient_email, application.applicant_email)
        sent = [m for m in mail.outbox if "corrected documents received" in m.subject]
        self.assertEqual(len(sent), 1)
        self.assertIn(first_requirement.title, sent[0].body)
        self.assertIn(f"Case No. {application.case_number_short}", sent[0].body)
        html_body, _mime = sent[0].alternatives[0]
        self.assertIn("Corrected documents received", html_body)

    def test_submit_document_resubmission_returns_case_to_reviewer(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        Notification.objects.filter(application=application).delete()

        uploaded_files = {
            first_requirement.code: self.build_valid_applicant_document_upload(
                first_requirement.code, content_prefix="resubmitted"
            )
        }
        submit_document_resubmission(application, self.applicant, uploaded_files)

        application.refresh_from_db()
        application.case.refresh_from_db()
        # Case returns to the reviewing role as ACTIVE for re-review.
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        # The flagged screening record reopened and the flagged row is back to NOT_REVIEWED.
        screening_record = ScreeningRecord.objects.get(
            application=application,
            review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        self.assertFalse(screening_record.is_finalized)
        flagged_row = screening_record.document_reviews.get(document_key=first_requirement.code)
        self.assertEqual(
            flagged_row.status, ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
        )
        # The re-upload landed on the applicant-intake chain as a new current version.
        intake_versions = EvidenceVaultItem.objects.filter(
            application=application,
            document_key=first_requirement.code,
            stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
        )
        self.assertTrue(intake_versions.filter(is_current_version=True).exists())
        self.assertGreaterEqual(intake_versions.count(), 2)
        # The reviewer is notified of the resubmission.
        self.assertTrue(
            Notification.objects.filter(
                application=application,
                recipient=self.secretariat,
                kind=Notification.Kind.RESUBMISSION_RECEIVED,
            ).exists()
        )

    def test_resubmission_view_renders_and_accepts_scoped_reupload(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()

        client = Client()
        url = reverse("applicant-resubmit", kwargs={"token": application.public_token})
        # The page renders the OTP step for an awaiting-resubmission application.
        self.assertEqual(client.get(url).status_code, 200)

        # Verify the applicant on the page (email-OTP step) then re-upload the flagged document.
        otp_code = issue_application_otp(application, actor=application.applicant)
        verified = client.post(url, {"action": "verify", "otp": otp_code})
        self.assertEqual(verified.status_code, 302)
        upload = self.build_valid_applicant_document_upload(
            first_requirement.code, content_prefix="reuploaded"
        )
        response = client.post(
            url,
            {"action": "resubmit", first_requirement.file_field_name: upload},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    def test_resubmission_needs_this_browser_to_be_verified(self):
        application = self._awaiting_resubmission_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        url = reverse("applicant-resubmit", kwargs={"token": application.public_token})
        # The applicant verifies in their own browser...
        applicant_browser = Client()
        otp_code = issue_application_otp(application, actor=application.applicant)
        applicant_browser.post(url, {"action": "verify", "otp": otp_code})
        # ...but someone else holding the emailed link still cannot upload.
        other_browser = Client()
        self.assertContains(other_browser.get(url), "Step 1")
        response = other_browser.post(
            url,
            {
                "action": "resubmit",
                first_requirement.file_field_name: self.build_valid_applicant_document_upload(
                    first_requirement.code
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        )

    def test_resubmission_cards_stage_files_until_resubmit(self):
        from recruitment.applicant_documents import get_document_parts

        application = self._awaiting_resubmission_application()
        code = get_applicant_document_requirements(application.branch)[0].code
        client = self._verified_status_client(application)
        page = client.get(reverse("applicant-resubmit", kwargs={"token": application.public_token}))
        self.assertContains(page, "data-doc-card")
        self.assertContains(page, "Why it was returned:")
        before = get_current_applicant_document_map(application)[code].sha256_digest

        upload_url = reverse(
            "applicant-resubmit-part-upload", kwargs={"token": application.public_token, "code": code}
        )
        first = client.post(upload_url, {"file": self._real_pdf_upload("page1.pdf", "Page one")})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.json()["is_ready"])
        second = client.post(upload_url, {"file": self._real_pdf_upload("page2.pdf", "Page two")})
        part_id = second.json()["parts"][-1]["id"]
        preview = client.get(
            reverse(
                "applicant-resubmit-part-preview",
                kwargs={"token": application.public_token, "part_id": part_id},
            )
        )
        self.assertEqual(preview["Content-Type"], "application/pdf")
        # A document that was not returned cannot be staged.
        other_code = get_applicant_document_requirements(application.branch)[1].code
        refused = client.post(
            reverse(
                "applicant-resubmit-part-upload",
                kwargs={"token": application.public_token, "code": other_code},
            ),
            {"file": self._real_pdf_upload("other.pdf", "Other")},
        )
        self.assertEqual(refused.status_code, 400)
        # Nothing reaches the office until the applicant presses Resubmit.
        application.refresh_from_db()
        self.assertEqual(get_current_applicant_document_map(application)[code].sha256_digest, before)
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        )

        response = client.post(
            reverse("applicant-resubmit", kwargs={"token": application.public_token}),
            {"action": "resubmit"},
        )
        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        current = get_current_applicant_document_map(application)[code]
        self.assertNotEqual(current.sha256_digest, before)
        from pypdf import PdfReader

        from recruitment.services import _decrypt_evidence_bytes

        self.assertEqual(len(PdfReader(io.BytesIO(_decrypt_evidence_bytes(current))).pages), 2)
        self.assertFalse(get_document_parts(application).exists())

    def test_resubmission_cards_refuse_an_unverified_browser(self):
        application = self._awaiting_resubmission_application()
        code = get_applicant_document_requirements(application.branch)[0].code
        response = Client().post(
            reverse(
                "applicant-resubmit-part-upload",
                kwargs={"token": application.public_token, "code": code},
            ),
            {"file": self._real_pdf_upload("page1.pdf", "Page one")},
        )
        self.assertEqual(response.status_code, 403)

    def test_submit_document_resubmission_requires_a_valid_otp(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        uploaded_files = {
            first_requirement.code: self.build_valid_applicant_document_upload(
                first_requirement.code
            )
        }
        # No OTP verified -> refused.
        with self.assertRaises(ValueError):
            submit_document_resubmission(application, self.applicant, uploaded_files)

    def _real_pdf_upload(self, name, label):
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        pdf.drawString(72, 720, label)
        pdf.showPage()
        pdf.save()
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="application/pdf")

    def _verified_status_client(self, application):
        from recruitment.portal_views import STATUS_OTP_VERIFIED_SESSION_KEY

        client = Client()
        session = client.session
        session[STATUS_OTP_VERIFIED_SESSION_KEY] = {str(application.pk): timezone.now().isoformat()}
        session.save()
        return client

    def _awaiting_resubmission_application(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        return application

    def test_resubmission_reminder_goes_once_per_deadline(self):
        from django.core.management import call_command

        application = self._awaiting_resubmission_application()
        record = ScreeningRecord.objects.get(
            application=application, review_stage=application.case.current_stage
        )
        record.resubmission_deadline = timezone.localdate() + timedelta(days=1)
        record.save(update_fields=["resubmission_deadline", "updated_at"])
        mail.outbox.clear()

        call_command("send_resubmission_reminders", stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("corrected documents", mail.outbox[0].subject)
        self.assertIn(
            reverse("applicant-resubmit", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.RESUBMISSION_REMINDER_SENT
            ).exists()
        )
        call_command("send_resubmission_reminders", stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)

        # A new (extended) deadline gets its own reminder.
        with self.captureOnCommitCallbacks(execute=True):
            extend_document_resubmission_deadline(
                application, self.secretariat, timezone.localdate() + timedelta(days=2), "Applicant asked."
            )
        mail.outbox.clear()
        call_command("send_resubmission_reminders", stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_resubmission_reminder_skips_deadlines_far_away(self):
        from recruitment.services import find_resubmissions_due_for_reminder

        application = self._awaiting_resubmission_application()
        ScreeningRecord.objects.filter(application=application).update(
            resubmission_deadline=timezone.localdate() + timedelta(days=10)
        )
        self.assertEqual(find_resubmissions_due_for_reminder(), [])

    def test_applicant_can_ask_to_withdraw_and_the_office_confirms_it(self):
        application = self.make_submitted_application()
        status_url = reverse("applicant-status-link", kwargs={"token": application.public_token})
        withdraw_url = reverse("applicant-withdrawal-request", kwargs={"token": application.public_token})

        # Without the emailed status code, nothing happens.
        response = Client().post(withdraw_url, {"confirm": "on"})
        self.assertRedirects(response, status_url, fetch_redirect_response=False)
        application.refresh_from_db()
        self.assertIsNone(application.withdrawal_requested_at)

        client = self._verified_status_client(application)
        page = client.get(status_url)
        self.assertContains(page, "Withdraw my application")
        no_confirm = client.post(withdraw_url, {"reason": "Found another job."}, follow=True)
        self.assertContains(no_confirm, "Tick the box to confirm")
        application.refresh_from_db()
        self.assertIsNone(application.withdrawal_requested_at)

        response = client.post(withdraw_url, {"reason": "Found another job.", "confirm": "on"}, follow=True)
        self.assertContains(response, "Your request to withdraw was sent")
        self.assertContains(response, "data-withdrawal-requested")
        application.refresh_from_db()
        self.assertIsNotNone(application.withdrawal_requested_at)
        self.assertEqual(application.withdrawal_request_reason, "Found another job.")
        # The case itself is untouched until staff record it.
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertTrue(
            Notification.objects.filter(
                application=application,
                recipient=self.secretariat,
                kind=Notification.Kind.WITHDRAWAL_REQUESTED,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.APPLICATION_WITHDRAWAL_REQUESTED
            ).exists()
        )
        # Asking twice is refused politely.
        again = client.post(withdraw_url, {"confirm": "on"}, follow=True)
        self.assertNotContains(again, "Withdraw my application")

        staff = Client()
        self.force_login_with_mfa(staff, self.secretariat)
        case_page = staff.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertContains(case_page, "The applicant asked to withdraw")
        self.assertContains(case_page, "Found another job.")
        self.assertEqual(
            case_page.context["withdrawal_form"].initial["reason"].startswith(
                "The applicant asked to withdraw through the applicant portal"
            ),
            True,
        )

    def test_withdrawal_is_not_offered_after_a_decision(self):
        application = self.make_approved_cos_application()
        page = self._verified_status_client(application).get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )
        self.assertNotContains(page, "Withdraw my application")

    def test_resubmission_page_combines_several_files_into_one_pdf(self):
        from pypdf import PdfReader

        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        client = self._verified_status_client(application)

        url = reverse("applicant-resubmit", kwargs={"token": application.public_token})
        page = client.get(url)
        self.assertContains(page, "multiple")
        response = client.post(
            url,
            {
                "action": "resubmit",
                first_requirement.file_field_name: [
                    self._real_pdf_upload("page1.pdf", "Page one"),
                    self._real_pdf_upload("page2.pdf", "Page two"),
                ],
            },
        )
        self.assertEqual(response.status_code, 302)
        application.case.refresh_from_db()
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        current = EvidenceVaultItem.objects.get(
            application=application,
            document_key=first_requirement.code,
            stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
            is_current_version=True,
        )
        self.assertEqual(current.content_type, "application/pdf")
        from recruitment.services import _decrypt_evidence_bytes

        combined = PdfReader(io.BytesIO(_decrypt_evidence_bytes(current)))
        self.assertEqual(len(combined.pages), 2)

    def test_resubmission_rejects_the_same_file_twice_for_one_document(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        self._finalize_screening_with_flagged_document(application, first_requirement)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        upload = self._real_pdf_upload("same.pdf", "Same")
        upload_again = SimpleUploadedFile("same-copy.pdf", upload.read(), content_type="application/pdf")
        upload.seek(0)
        with self.assertRaisesMessage(ValueError, "You already added this file"):
            submit_document_resubmission(
                application, self.applicant, {first_requirement.code: [upload, upload_again]}
            )

    def test_status_page_shows_a_step_by_step_timeline(self):
        application = self.make_submitted_application()
        response = self._verified_status_client(application).get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )
        self.assertContains(response, "data-status-timeline")
        self.assertContains(response, "Application submitted")
        self.assertContains(response, "Document check")
        self.assertContains(response, "Final decision")
        steps = response.context["timeline"]
        self.assertEqual([step["state"] for step in steps], ["done", "current", "upcoming"])

    def test_status_timeline_marks_an_ended_application(self):
        application = self.make_submitted_application()
        application.status = RecruitmentApplication.Status.REJECTED
        application.save(update_fields=["status", "updated_at"])
        response = self._verified_status_client(application).get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )
        last = response.context["timeline"][-1]
        self.assertEqual(last["state"], "ended")
        self.assertEqual(last["label"], "Result: Not Selected")

    def test_status_page_shows_completion_checklist_only_after_it_was_emailed(self):
        application = self.make_approved_cos_application()
        deadline = timezone.localdate() + timedelta(days=10)
        self._record_completion_tracking(
            application,
            self.secretariat,
            deadline=deadline,
            items=[
                ("Signed contract", CompletionRequirement.RequirementStatus.PENDING),
                ("Medical certificate", CompletionRequirement.RequirementStatus.COMPLETED),
                ("Waived item", CompletionRequirement.RequirementStatus.NOT_APPLICABLE),
            ],
        )
        status_url = reverse("applicant-status-link", kwargs={"token": application.public_token})
        before = self._verified_status_client(application).get(status_url)
        self.assertNotContains(before, "data-completion-checklist")

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            client.post(reverse("notification-checklist", kwargs={"pk": application.pk}), {})
        after = self._verified_status_client(application).get(status_url)
        self.assertContains(after, "data-completion-checklist")
        self.assertContains(after, "Signed contract")
        self.assertContains(after, "Medical certificate")
        self.assertContains(after, "Received")
        self.assertNotContains(after, "Waived item")
        self.assertContains(after, date_format(deadline, "F d, Y"))

    def test_return_to_applicant_falls_back_to_text_when_resubmission_template_fails(self):
        application = self.make_submitted_application()
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                "completeness_notes": "A corrected document is required.",
                "qualification_outcome": ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                "screening_notes": "Screening cannot continue until the document is corrected.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "operation": "save",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        mail.outbox.clear()

        # Simulate a missing/broken "email/document_resubmission_request.txt"
        # template. This body is rendered synchronously inside the atomic
        # process_workflow_action, so the fallback must keep the reviewer's
        # "return to applicant" action from rolling back.
        with patch(
            "recruitment.notification_services.render_to_string",
            side_effect=RuntimeError("template render boom"),
        ), self.assertLogs("recruitment.notification_services", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                process_workflow_action(
                    application,
                    self.secretariat,
                    "return_to_applicant",
                    "Please resubmit the corrected document.",
                )

        # The workflow action still commits despite the template failure.
        application.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT)

        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
        )
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.failure_details, "")
        self.assertEqual(notification.metadata["document_keys"], [first_requirement.code])

        # The applicant still receives a complete, actionable plain-text body.
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(first_requirement.title, body)
        self.assertIn("Upload a signed and readable copy.", body)
        self.assertIn("Please resubmit the corrected document.", body)
        self.assertIn(
            reverse("applicant-resubmit", kwargs={"token": application.public_token}),
            body,
        )

        # The render failure is logged rather than swallowed silently.
        self.assertTrue(
            any(
                "Failed to render document resubmission email" in message
                for message in logs.output
            )
        )

    def test_generic_whole_application_return_is_refused(self):
        # RD-6b (Option A): the generic whole-application return is removed. A
        # return with no flagged documents is refused server-side — it never had
        # a working applicant path and contradicted RD-1's doctrine. Returns
        # flag documents (the resubmission request path); unsuitable applicants
        # are Rejected.
        application = self.make_submitted_application()
        mail.outbox.clear()
        notification_count = NotificationLog.objects.filter(application=application).count()

        with self.assertRaisesMessage(
            ValueError, "Returning the whole application is no longer available"
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please update your submitted information before resubmitting.",
            )

        application.refresh_from_db()
        self.assertNotEqual(
            application.status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT
        )
        self.assertEqual(
            NotificationLog.objects.filter(application=application).count(),
            notification_count,
        )
        self.assertEqual(len(mail.outbox), 0)

    def _record_completion_tracking(self, application, actor, *, deadline=None, items=None):
        # RD-8 test helper: give the completion-stage case a tracked record with
        # requirement rows — the single source the checklist email reads.
        record = CompletionRecord.objects.create(
            application=application,
            recruitment_case=application.case,
            tracked_by=actor,
            branch=application.branch,
            level=application.level,
            deadline=deadline,
        )
        for index, (label, status) in enumerate(
            items or [("Signed contract", CompletionRequirement.RequirementStatus.PENDING)]
        ):
            CompletionRequirement.objects.create(
                completion_record=record,
                item_label=label,
                status=status,
                display_order=index,
            )
        return record

    def test_secretariat_can_send_requirement_checklist_notification_for_level1_completion(self):
        application = self.make_approved_cos_application()
        deadline = timezone.localdate() + timedelta(days=7)
        self._record_completion_tracking(
            application,
            self.secretariat,
            deadline=deadline,
            items=[
                ("Signed contract", CompletionRequirement.RequirementStatus.PENDING),
                ("Government-issued ID", CompletionRequirement.RequirementStatus.PENDING),
                ("Medical certificate", CompletionRequirement.RequirementStatus.COMPLETED),
            ],
        )
        mail.outbox.clear()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-checklist", kwargs={"pk": application.pk}),
                {"additional_message": "Bring original copies during submission."},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        notification = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.REQUIREMENT_CHECKLIST,
        ).latest("created_at")
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.secretariat)
        self.assertEqual(len(mail.outbox), 1)
        # RD-8: the email carries the record's PENDING rows and the record's
        # deadline — never a re-typed list or an independently computed date.
        body = mail.outbox[0].body
        self.assertIn("Signed contract", body)
        self.assertIn("Government-issued ID", body)
        self.assertNotIn("Medical certificate", body)
        self.assertEqual(notification.metadata["deadline"], deadline.isoformat())
        self.assertContains(response, "Send Requirement Checklist")
        # The dialog previews exactly what the email carries (record rows + deadline).
        self.assertContains(response, "Will be emailed")
        self.assertNotContains(response, "{#")

    def test_requirement_checklist_requires_record_deadline_and_rows(self):
        # RD-8: the send is refused with a readable message when the record,
        # its rows, or its deadline are missing — the email never invents them.
        application = self.make_approved_cos_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        # No completion record at all.
        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {"additional_message": ""},
            follow=True,
        )
        self.assertContains(response, "Record the completion tracking details")

        # Record with rows but no deadline.
        record = self._record_completion_tracking(application, self.secretariat, deadline=None)
        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {"additional_message": ""},
            follow=True,
        )
        self.assertContains(response, "Set the completion deadline")

        # Deadline set but every row already resolved.
        record.deadline = timezone.localdate() + timedelta(days=7)
        record.save(update_fields=["deadline", "updated_at"])
        record.requirements.update(
            status=CompletionRequirement.RequirementStatus.COMPLETED
        )
        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {"additional_message": ""},
            follow=True,
        )
        self.assertContains(response, "Every requirement is already resolved")
        self.assertFalse(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.REQUIREMENT_CHECKLIST,
            ).exists()
        )

    def test_completion_overdue_property_truth_table(self):
        application = self.make_approved_cos_application()
        record = self._record_completion_tracking(application, self.secretariat, deadline=None)
        # No deadline -> not overdue.
        self.assertFalse(record.is_overdue)
        # Future deadline -> not overdue.
        record.deadline = timezone.localdate() + timedelta(days=3)
        record.save(update_fields=["deadline", "updated_at"])
        self.assertFalse(record.is_overdue)
        # Past deadline with a pending row -> overdue.
        record.deadline = timezone.localdate() - timedelta(days=1)
        record.save(update_fields=["deadline", "updated_at"])
        self.assertTrue(record.is_overdue)
        # Past deadline, everything resolved -> not overdue.
        record.requirements.update(
            status=CompletionRequirement.RequirementStatus.COMPLETED
        )
        self.assertFalse(record.is_overdue)

    def test_reminder_form_defaults_deadline_from_completion_record(self):
        application = self.make_approved_cos_application()
        deadline = timezone.localdate() + timedelta(days=9)
        self._record_completion_tracking(application, self.secretariat, deadline=deadline)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        # RD-8: the reminder dialog defaults to the deadline the office tracks.
        self.assertContains(response, f'value="{deadline.isoformat()}"')

    def test_secretariat_cannot_send_requirement_checklist_before_selection(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {"additional_message": ""},
        )

        self.assertEqual(response.status_code, 403)

    def test_notifications_panel_renders_for_secretariat_not_hrmpsb(self):
        # RD-9: delivery state is visible where work happens — but never to
        # HRMPSB, whose blinded view must not meet subjects carrying names.
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notifications sent")
        self.assertNotContains(response, "{#")

        hrmpsb_case = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(hrmpsb_case)
        hrmpsb_client = Client()
        self.force_login_with_mfa(hrmpsb_client, self.hrmpsb)
        response = hrmpsb_client.get(
            reverse("application-detail", kwargs={"pk": hrmpsb_case.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Notifications sent")

    def test_resend_redelivers_the_stored_email_verbatim(self):
        application = self.make_submitted_application()
        original = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        ).latest("created_at")
        mail.outbox.clear()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse(
                    "notification-resend",
                    kwargs={"pk": application.pk, "log_id": original.pk},
                ),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)

        resent = NotificationLog.objects.filter(
            application=application, metadata__resent_from=original.pk
        ).latest("created_at")
        # Verbatim redelivery: the stored subject/body, not a re-render.
        self.assertEqual(resent.subject, original.subject)
        self.assertEqual(resent.body, original.body)
        self.assertEqual(resent.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.NOTIFICATION_RESENT
            ).exists()
        )

    def test_resend_refused_for_hrmpsb_member(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        original = NotificationLog.objects.filter(application=application).latest("created_at")
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.post(
            reverse(
                "notification-resend",
                kwargs={"pk": application.pk, "log_id": original.pk},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_failed_send_raises_in_app_bell_for_current_handler(self):
        # RD-9: a failed applicant email must reach the handler's bell —
        # otherwise the office believes the applicant was told.
        application = self.make_submitted_application()
        Notification.objects.filter(application=application).delete()
        # Blank the applicant email so the queue path fails deterministically.
        application.applicant_email = ""
        application.save(update_fields=["applicant_email", "updated_at"])
        application.applicant.email = ""
        application.applicant.save(update_fields=["email"])

        from recruitment.notification_services import queue_submission_acknowledgment_notification

        log = queue_submission_acknowledgment_notification(application, actor=self.secretariat)
        self.assertEqual(log.delivery_status, NotificationLog.DeliveryStatus.FAILED)
        bell = Notification.objects.filter(
            application=application,
            kind=Notification.Kind.NOTIFICATION_FAILED,
            recipient=self.secretariat,
        ).first()
        self.assertIsNotNone(bell)
        self.assertIn("did NOT receive", bell.body)

    def test_failed_terminal_email_alerts_the_closed_cases_owning_office(self):
        for position, expected_recipient, excluded_recipient in [
            (self.level1_position, self.secretariat, self.hrm_chief),
            (self.level2_position, self.hrm_chief, self.secretariat),
        ]:
            with self.subTest(level=position.level):
                application = self.make_submitted_application(position)
                Notification.objects.filter(application=application).delete()
                application.status = RecruitmentApplication.Status.REJECTED
                application.current_handler_role = ""
                application.closed_at = timezone.now()
                application.save(
                    update_fields=[
                        "status",
                        "current_handler_role",
                        "closed_at",
                        "updated_at",
                    ]
                )
                case = application.case
                case.current_stage = RecruitmentCase.Stage.CLOSED
                case.current_handler_role = ""
                case.case_status = RecruitmentCase.CaseStatus.REJECTED
                case.is_stage_locked = True
                case.locked_stage = RecruitmentCase.Stage.HRM_CHIEF_REVIEW
                case.closed_at = timezone.now()
                case.save(
                    update_fields=[
                        "current_stage",
                        "current_handler_role",
                        "case_status",
                        "is_stage_locked",
                        "locked_stage",
                        "closed_at",
                        "updated_at",
                    ]
                )
                inactive_owner = User.objects.create_user(
                    username=f"inactive-owner-{position.level}",
                    password="testpass123",
                    email=f"inactive-{position.level}@example.com",
                    role=expected_recipient.role,
                    is_active=False,
                )
                log = NotificationLog.objects.create(
                    application=application,
                    recruitment_case=case,
                    notification_type=(
                        NotificationLog.NotificationType.NON_SELECTED_APPLICANT
                    ),
                    delivery_status=NotificationLog.DeliveryStatus.FAILED,
                    related_status=RecruitmentApplication.Status.REJECTED,
                    recipient_email=application.applicant_email,
                    subject="Application result",
                    body="Final outcome notice.",
                    failure_details="Mail server returned an error.",
                )

                alerts = emit_notification_failed_in_app_alert(log)

                self.assertEqual(
                    {alert.recipient_id for alert in alerts},
                    {expected_recipient.id},
                )
                self.assertTrue(
                    all(
                        user_can_view_application(alert.recipient, application)
                        for alert in alerts
                    )
                )
                self.assertNotIn(
                    excluded_recipient.id,
                    {alert.recipient_id for alert in alerts},
                )
                self.assertNotIn(
                    inactive_owner.id,
                    {alert.recipient_id for alert in alerts},
                )
                self.assertTrue(
                    all(
                        alert.kind == Notification.Kind.NOTIFICATION_FAILED
                        for alert in alerts
                    )
                )

    def test_status_page_hides_unannounced_interview_sessions(self):
        # RD-9: only announced interviews reach the applicant status page
        # (parity with the exam rule).
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "scheduled_for": timezone.now() + timedelta(days=2),
                "location": "Panel Room A",
                "session_notes": "Drafted, not yet announced.",
            },
            finalize=False,
        )
        client = Client()
        application.otp_requested_at = timezone.now() - timedelta(
            seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1
        )
        application.save(update_fields=["otp_requested_at", "updated_at"])
        client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": application.applicant_email,
            },
        )
        client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        response = client.get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Panel Room A")

        # Announcing it puts it on the page.
        with self.captureOnCommitCallbacks(execute=True):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "scheduled_for": timezone.now() + timedelta(days=2),
                    "location": "Panel Room A",
                    "session_notes": "Announced.",
                },
                finalize=False,
                notify_applicant=True,
            )
        response = client.get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )
        self.assertContains(response, "Panel Room A")

    def test_exam_finalize_emits_in_app_bell(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        Notification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.assertTrue(
            Notification.objects.filter(
                application=application, kind=Notification.Kind.EXAM_FINALIZED
            ).exists()
        )

    def test_hrm_chief_can_send_reminder_notification_for_level2_completion(self):
        application = self.make_approved_level2_plantilla_application()
        mail.outbox.clear()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-reminder", kwargs={"pk": application.pk}),
                {
                    "reminder_subject": "Follow-up reminder for completion documents",
                    "reminder_message": "Please submit the remaining completion documents this week.",
                    "deadline": (timezone.localdate() + timedelta(days=3)).isoformat(),
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        notification = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.REMINDER,
        ).latest("created_at")
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.hrm_chief)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )
        self.assertContains(response, "Send Reminder")
