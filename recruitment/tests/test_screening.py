"""Screening records and the vacancy screening batch gate."""

from datetime import timedelta

from django.core import mail
from django.test import Client
from django.template.loader import render_to_string
from django.urls import reverse

from ..forms import ScreeningReviewForm
from ..models import (
    AuditLog,
    NotificationLog,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from ..requirements import get_applicant_document_requirements
from ..services import (
    extend_document_resubmission_deadline,
    get_office_policy,
    get_screening_record,
    get_available_actions,
    remove_applicant_from_batch,
    save_screening_review,
    submit_application,
    user_can_manage_exam,
    vacancy_screening_batch_status,
    user_can_extend_resubmission_deadline,
)
from .base import BaseRecruitmentTestCase, BatchScreeningHelpers


class ScreeningRecordTests(BaseRecruitmentTestCase):
    def screening_payload(self, **overrides):
        # RD-1: completeness is derived from the document rows, never posted.
        payload = {
            "completeness_notes": "All required application documents were reviewed.",
            "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
            "screening_notes": "Applicant satisfies the documented qualification basis.",
        }
        payload.update(overrides)
        return payload

    def test_not_qualified_notice_counts_flagged_document_deficiencies_without_details(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement = get_applicant_document_requirements(application)[0]
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("screening-review", kwargs={"pk": application.pk}),
                {
                    **self.screening_payload(
                        qualification_outcome=(
                            ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
                        ),
                        screening_notes="Does not meet the Qualification Standards.",
                    ),
                    **self.screening_document_status_payload(
                        application,
                        overrides={
                            requirement.code: (
                                ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                            )
                        },
                    ),
                    f"document_remarks__{requirement.code}": "Pages are incomplete.",
                    "operation": "finalize",
                },
            )
        self.assertEqual(response.status_code, 302)
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        )
        self.assertIn(
            "The review also noted deficiencies in 1 submitted document(s).",
            notification.body,
        )
        self.assertNotIn("Pages are incomplete", notification.body)

    def _awaiting_resubmission_application(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application)[0]
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("screening-review", kwargs={"pk": application.pk}),
                {
                    **self.screening_payload(qualification_outcome=""),
                    **self.screening_document_status_payload(
                        application,
                        overrides={
                            first_requirement.code: (
                                ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                            ),
                        },
                    ),
                    f"document_remarks__{first_requirement.code}": "Upload a clearer copy.",
                    "return_remarks": "Please upload the corrected document.",
                    "operation": "return_to_applicant",
                },
            )
        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        return application

    def test_default_policy_allows_audited_resubmission_deadline_extension(self):
        application = self._awaiting_resubmission_application()
        record = get_screening_record(application, stage=application.case.current_stage)
        old_deadline = record.resubmission_deadline
        new_deadline = old_deadline + timedelta(days=3)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        detail = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Extend resubmission deadline")
        before_count = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
        ).count()
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse(
                    "resubmission-deadline-extend", kwargs={"pk": application.pk}
                ),
                {
                    "new_deadline": new_deadline.isoformat(),
                    "reason": "The applicant documented a temporary portal access issue.",
                },
            )

        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.resubmission_deadline, new_deadline)
        notices = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
        ).order_by("id")
        self.assertEqual(notices.count(), before_count + 1)
        self.assertIn("deadline has been extended", notices.last().body)
        audit = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.DECISION_RECORDED,
            description="Extended the pending document resubmission deadline.",
        )
        self.assertEqual(audit.metadata["old_resubmission_deadline"], old_deadline.isoformat())
        self.assertEqual(audit.metadata["new_resubmission_deadline"], new_deadline.isoformat())
        self.assertEqual(audit.metadata["actor_id"], self.secretariat.id)
        self.assertIn("portal access issue", audit.metadata["reason"])

    def test_policy_can_disable_resubmission_deadline_extension(self):
        application = self._awaiting_resubmission_application()
        record = get_screening_record(application, stage=application.case.current_stage)
        policy = get_office_policy()
        policy.allow_resubmission_deadline_extension = False
        policy.save(update_fields=["allow_resubmission_deadline_extension", "updated_at"])

        self.assertFalse(user_can_extend_resubmission_deadline(self.secretariat, application))
        with self.assertRaisesMessage(
            ValueError, "Office Policy does not allow resubmission deadline extensions."
        ):
            extend_document_resubmission_deadline(
                application,
                self.secretariat,
                record.resubmission_deadline + timedelta(days=1),
                "The office granted more time.",
            )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        detail = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "Extend resubmission deadline")
        refused = client.post(
            reverse("resubmission-deadline-extend", kwargs={"pk": application.pk}),
            {
                "new_deadline": (record.resubmission_deadline + timedelta(days=1)).isoformat(),
                "reason": "The office granted more time.",
            },
        )
        self.assertEqual(refused.status_code, 403)

    def test_pending_resubmission_blocks_qualified_finalize_in_service_and_form(self):
        application = self._awaiting_resubmission_application()
        refusal = (
            "The applicant's resubmission is still pending. Wait for the new documents "
            "(or remove the applicant from the batch) before finalizing this screening "
            "as Qualified."
        )

        with self.assertRaisesMessage(ValueError, refusal):
            save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.screening_payload(),
                finalize=True,
            )

        record = ScreeningRecord.objects.get(application=application)
        self.assertFalse(record.is_finalized)
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )

        form = ScreeningReviewForm(
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
            },
            instance=record,
            application=application,
        )
        self.assertFalse(form.is_valid())
        self.assertIn(refusal, form.errors["qualification_outcome"])

    def test_pending_resubmission_still_allows_screening_draft_save(self):
        application = self._awaiting_resubmission_application()

        record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(
                qualification_outcome="",
                screening_notes="Draft re-review notes while the corrected file is pending.",
            ),
            finalize=False,
            allow_partial=True,
        )

        self.assertFalse(record.is_finalized)
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )

    def test_pending_resubmission_still_allows_not_qualified_finalize(self):
        application = self._awaiting_resubmission_application()

        with self.captureOnCommitCallbacks(execute=True):
            record = save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.screening_payload(
                    qualification_outcome=(
                        ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
                    ),
                    screening_notes="The qualification requirement is not met.",
                ),
                finalize=True,
            )

        record.refresh_from_db()
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.REJECTED,
        )

    def test_current_handler_can_save_and_finalize_screening_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        screening_record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(),
            finalize=False,
        )
        self.assertFalse(screening_record.is_finalized)
        self.assertEqual(
            screening_record.qualification_outcome,
            ScreeningRecord.QualificationOutcome.QUALIFIED,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.SCREENING_RECORDED,
            ).exists()
        )

        screening_record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(screening_notes="Screening finalized."),
            finalize=True,
        )
        screening_record.refresh_from_db()
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.finalized_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.SCREENING_FINALIZED,
            ).exists()
        )

    def test_screening_view_can_finalize_when_all_required_fields_are_present(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.finalized_by, self.secretariat)

    def test_screening_view_allows_blank_notes_fields(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    completeness_notes="",
                    screening_notes="",
                ),
                **self.screening_document_status_payload(application),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.completeness_notes, "")
        self.assertEqual(screening_record.screening_notes, "")

    def test_screening_view_rejects_reviewer_selected_not_submitted(self):
        # Not Submitted is system-applied only (a required row with no file);
        # a reviewer can never declare an uploaded document "not submitted" —
        # the editable dropdown offers Complied / Return for Resubmission only.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    completeness_status=ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    completeness_notes="Cover letter requires validation.",
                    qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                    screening_notes="Qualification cannot proceed until the document issue is resolved.",
                ),
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED,
                    },
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice.")
        self.assertFalse(
            ScreeningRecord.objects.filter(application=application, is_finalized=True).exists()
        )

    def test_screening_view_persists_document_resubmission_request_and_remarks(self):
        # RD-1: flagging a document and returning it is one atomic action
        # (operation=return_to_applicant). It persists the flag + remarks and
        # returns the case WITHOUT finalizing the screening.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    qualification_outcome="",
                    screening_notes="Qualification cannot proceed until the corrected document is submitted.",
                ),
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "return_remarks": "Please upload a signed and readable copy.",
                "operation": "return_to_applicant",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Flagged documents returned to the applicant")
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertFalse(screening_record.is_finalized)
        locked_body = render_to_string(
            "internal_includes/screening_locked_body.html",
            {
                "current_screening_record": screening_record,
                "screening_flagged_count": 1,
                "screening_not_submitted_count": 0,
            },
        )
        self.assertIn("flagged for resubmission", locked_body)
        self.assertIn("Returned for resubmission", locked_body)
        document_review = screening_record.document_reviews.get(document_key=first_requirement.code)
        self.assertEqual(
            document_review.status,
            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION,
        )
        self.assertEqual(document_review.remarks, "Upload a signed and readable copy.")

    def test_screening_view_requires_remarks_for_document_resubmission_request(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    completeness_status=ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    completeness_notes="A corrected document is required.",
                    qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                    screening_notes="Qualification cannot proceed until the corrected document is submitted.",
                ),
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add the instruction the applicant should follow")
        self.assertFalse(ScreeningRecord.objects.filter(application=application, is_finalized=True).exists())

    def test_screening_form_has_no_score_inputs_and_stores_null_scores(self):
        # RD-3: screening checks papers, the CAR gives grades — the ETE score
        # boxes are gone from the screening surface, and a finalized screening
        # stores NULL scores (the model fields persist only for history).
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        page = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="document_review_score"')
        self.assertNotContains(page, 'name="education_score"')
        self.assertNotContains(page, "Add detailed scores")
        self.assertNotContains(page, "{#")

        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "finalize",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertTrue(screening_record.is_finalized)
        self.assertIsNone(screening_record.education_score)
        self.assertIsNone(screening_record.training_score)
        self.assertIsNone(screening_record.experience_score)
        self.assertIsNone(screening_record.document_review_score)
        self.assertNotContains(response, "Document review score")
        self.assertNotContains(response, "Score breakdown")
        audit_metadata = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.SCREENING_FINALIZED,
        ).metadata
        for score_key in (
            "education_score",
            "training_score",
            "experience_score",
            "document_review_score",
        ):
            self.assertNotIn(score_key, audit_metadata)

    def test_completeness_is_derived_from_the_document_review(self):
        # RD-1: the reviewer never answers "are the documents complete?" — the
        # value is computed from the rows and stored so the CAR, exports, locked
        # views, and audit keep reading it.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        # A flagged document derives INCOMPLETE, with no completeness input posted.
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(qualification_outcome=""),
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a readable copy.",
                "operation": "save",
            },
        )
        record = ScreeningRecord.objects.get(application=application)
        self.assertEqual(
            record.completeness_status, ScreeningRecord.CompletenessStatus.INCOMPLETE
        )

        # Clearing the flag derives COMPLETE.
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(qualification_outcome=""),
                **self.screening_document_status_payload(application),
                "operation": "save",
            },
        )
        record.refresh_from_db()
        self.assertEqual(
            record.completeness_status, ScreeningRecord.CompletenessStatus.COMPLETE
        )

    def test_return_to_applicant_is_one_atomic_decision(self):
        # RD-1: returning flagged documents saves the review AND sends the case
        # back in a single action — no finalize, no invented verdict.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        mail.outbox.clear()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("screening-review", kwargs={"pk": application.pk}),
                {
                    **self.screening_payload(qualification_outcome=""),
                    **self.screening_document_status_payload(
                        application,
                        overrides={
                            first_requirement.code: (
                                ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                            ),
                        },
                    ),
                    f"document_remarks__{first_requirement.code}": "Upload a signed copy.",
                    "return_remarks": "Please re-upload the signed application letter.",
                    "operation": "return_to_applicant",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        record = ScreeningRecord.objects.get(application=application)
        # The flags persisted, but screening was NOT finalized.
        self.assertFalse(record.is_finalized)
        self.assertEqual(
            record.document_reviews.get(document_key=first_requirement.code).status,
            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION,
        )
        # ...and the case went back to the applicant in the same action.
        self.assertIsNotNone(record.resubmission_deadline)
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        self.assertTrue(mail.outbox)

    def test_not_qualified_with_flags_sends_only_the_rejection(self):
        # RD-1: Not Qualified is available even with flags (they become moot),
        # and the applicant gets exactly one message — no resubmission email.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        mail.outbox.clear()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("screening-review", kwargs={"pk": application.pk}),
                {
                    **self.screening_payload(
                        qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                        screening_notes="Eligibility does not cover this position.",
                    ),
                    **self.screening_document_status_payload(
                        application,
                        overrides={
                            first_requirement.code: (
                                ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                            ),
                        },
                    ),
                    f"document_remarks__{first_requirement.code}": "Blurry scan.",
                    "operation": "finalize",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        screening_record = ScreeningRecord.objects.get(application=application)
        locked_body = render_to_string(
            "internal_includes/screening_locked_body.html",
            {
                "current_screening_record": screening_record,
                "screening_flagged_count": 1,
                "screening_not_submitted_count": 0,
            },
        )
        self.assertIn("document flagged as deficient", locked_body)
        self.assertIn("Flagged as deficient (not returned)", locked_body)
        self.assertNotIn("Returned for resubmission", locked_body)
        bodies = " ".join(message.body for message in mail.outbox)
        self.assertNotIn("resubmission", bodies.lower())

    def test_screening_view_blocks_qualified_while_a_document_is_flagged(self):
        # RD-1 asymmetric gate, server-enforced: an applicant is never passed on
        # evidence that has not been verified.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application.branch)[0]

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a readable copy.",
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "before this applicant can be marked Qualified")
        self.assertFalse(ScreeningRecord.objects.filter(application=application, is_finalized=True).exists())

    def test_document_review_score_uses_policy_component_weights(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        screening_record = save_screening_review(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.screening_payload(
                education_score="90.00",
                training_score="80.00",
                experience_score="70.00",
                document_review_score="10.00",
            ),
            finalize=True,
        )

        self.assertEqual(str(screening_record.document_review_score), "81.00")
        self.assertEqual(str(screening_record.education_score), "90.00")
        self.assertEqual(str(screening_record.training_score), "80.00")
        self.assertEqual(str(screening_record.experience_score), "70.00")

    def test_level2_document_review_score_uses_second_level_component_weights(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        screening_record = save_screening_review(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.screening_payload(
                education_score="90.00",
                training_score="80.00",
                experience_score="70.00",
            ),
            finalize=True,
        )

        self.assertEqual(str(screening_record.document_review_score), "79.00")

    def test_finalized_screening_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        screening_record = self.finalize_screening_for_current_stage(application, self.secretariat)

        with self.assertRaisesMessage(
            ValueError,
            "Finalized screening records cannot be edited.",
        ):
            save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    "completeness_notes": "Changed after finalization.",
                    "qualification_outcome": ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                    "screening_notes": "Should not be saved.",
                },
                finalize=False,
            )

        screening_record.refresh_from_db()
        self.assertEqual(screening_record.completeness_status, ScreeningRecord.CompletenessStatus.COMPLETE)
        self.assertEqual(screening_record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED)

    def test_unauthorized_user_cannot_record_screening(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_record_level2_screening_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class BatchScreeningGateTests(BatchScreeningHelpers, BaseRecruitmentTestCase):
    def test_single_application_vacancy_allows_exam_after_screening(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.assertTrue(vacancy_screening_batch_status(self.level1_position).is_ready)
        self.assertTrue(user_can_manage_exam(self.secretariat, application))

    def test_pending_sibling_blocks_exam_for_a_qualified_application(self):
        qualified = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        # A second submitted applicant whose screening is not finalized yet is "pending".
        self._extra_submitted_application(self.level1_position, "1")
        qualified.refresh_from_db()

        status = vacancy_screening_batch_status(self.level1_position)
        self.assertFalse(status.is_ready)
        self.assertEqual(status.pending, 1)
        self.assertEqual(status.qualified, 1)
        self.assertFalse(user_can_manage_exam(self.secretariat, qualified))
        with self.assertRaises(ValueError):
            self.schedule_exam_for_current_stage(qualified, self.secretariat)

    def test_deciding_the_sibling_unblocks_the_batch(self):
        qualified = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        sibling = self._extra_submitted_application(self.level1_position, "1")
        self.finalize_screening_for_current_stage(sibling, self.secretariat)
        qualified.refresh_from_db()

        status = vacancy_screening_batch_status(self.level1_position)
        self.assertTrue(status.is_ready)
        self.assertEqual(status.pending, 0)
        self.assertEqual(status.qualified, 2)
        self.assertTrue(user_can_manage_exam(self.secretariat, qualified))

    def test_remove_applicant_disqualifies_and_records_reason(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self._make_awaiting_case(application)
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        remove_applicant_from_batch(
            application, self.secretariat, "Did not resubmit before the deadline."
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertTrue(application.case.is_stage_locked)
        audit = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.APPLICANT_DISQUALIFIED,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata["reason"], "Did not resubmit before the deadline.")

    def test_remove_applicant_requires_a_reason(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self._make_awaiting_case(application)
        with self.assertRaises(ValueError):
            remove_applicant_from_batch(application, self.secretariat, "   ")

    def test_awaiting_resubmission_case_offers_remove_from_batch(self):
        # RD-4: the Remove-from-Batch action must actually be offered on an
        # awaiting-resubmission case. It previously sat behind the disposition
        # gate (screening_requires_disposition_for_current_stage), which post-RD-1
        # is never true for a returned case (its screening is not finalized), so
        # the offer was silently dead. It is now keyed on the case status alone.
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self._make_awaiting_case(application)
        actions = {value for value, _label in get_available_actions(application, self.secretariat)}
        self.assertIn("remove_applicant", actions)
        self.assertNotIn("reject", actions)
        self.assertNotIn("return_to_applicant", actions)

    def test_removing_awaiting_sibling_unblocks_the_batch(self):
        qualified = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        sibling = self._extra_submitted_application(self.level1_position, "2")
        self._make_awaiting_case(sibling)
        qualified.refresh_from_db()
        self.assertFalse(vacancy_screening_batch_status(self.level1_position).is_ready)
        self.assertFalse(user_can_manage_exam(self.secretariat, qualified))

        remove_applicant_from_batch(sibling, self.secretariat, "Missed the resubmission deadline.")
        qualified.refresh_from_db()
        self.assertTrue(vacancy_screening_batch_status(self.level1_position).is_ready)
        self.assertTrue(user_can_manage_exam(self.secretariat, qualified))
