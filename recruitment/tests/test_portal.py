"""The public applicant portal: intake, drafts, OTP, tracking and language."""

import hashlib
import io
import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from ..forms import (
    APPLICANT_COVER_LETTER_LENGTH_ERROR_MESSAGE,
    APPLICANT_COVER_LETTER_MAX_LENGTH,
    APPLICANT_MOBILE_ERROR_MESSAGE,
    APPLICANT_NAME_ERROR_MESSAGE,
    APPLICANT_NAME_LENGTH_ERROR_MESSAGE,
    APPLICANT_NAME_MAX_LENGTH,
    APPLICANT_QUALIFICATION_SUMMARY_LENGTH_ERROR_MESSAGE,
    APPLICANT_QUALIFICATION_SUMMARY_MAX_LENGTH,
    ApplicantApplicationForm,
    ApplicantStartForm,
)
from ..models import (
    AuditLog,
    EvidenceVaultItem,
    PositionDocumentRequirement,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..requirements import (
    TRAINING_CERTIFICATES,
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from ..services import (
    get_current_applicant_document_map,
    IDENTIFIER_GENERATION_RETRIES,
    assign_candidate_code,
    issue_application_otp,
    submit_application,
    record_application_withdrawal,
    save_vacancy_exam_schedule,
    upload_evidence_item,
)
from .base import User, BaseRecruitmentTestCase, BatchExamHelpers


class ApplicantPortalFlowTests(BaseRecruitmentTestCase):
    def open_verified_application(self, email, position=None):
        """Start (details only) -> verify the emailed code. Returns the client (now holding a
        verified session for the draft) and the draft."""
        return self.open_portal_draft(email, position)

    def submit_portal_application(self, email):
        _client, application, response = self.apply_through_portal(email)
        self.assertEqual(response.status_code, 200)
        return application

    def add_receipt_documents_until(self, application, target_count):
        from recruitment.services import get_current_applicant_document_items

        current_count = get_current_applicant_document_items(application).count()
        for index in range(current_count + 1, target_count + 1):
            document_key = f"zz_receipt_extra_{index:02d}"
            upload_evidence_item(
                application=application,
                actor=application.applicant,
                label=f"Supplemental Requirement {index:02d}",
                uploaded_file=self.build_valid_applicant_document_upload(
                    document_key,
                    content_prefix=f"receipt-extra-{index:02d}",
                    filename=f"receipt-extra-{index:02d}.pdf",
                ),
                document_key=document_key,
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )
        return list(get_current_applicant_document_items(application))

    def intake_field_validation_form(self, **overrides):
        """Identity fields are checked on the start form; the qualification summary and note
        on the application form. Returns whichever form owns the overridden field."""
        start_fields = {"first_name", "last_name", "email", "phone"}
        if not overrides or set(overrides) & start_fields:
            data = {
                "first_name": "Pat",
                "last_name": "Applicant",
                "email": "portal.validation@example.com",
                "phone": "09171234567",
                "privacy_consent": "on",
            }
            data.update(overrides)
            form = ApplicantStartForm(data=data, entry=self.level1_position)
        else:
            data = {
                "first_name": "Pat",
                "last_name": "Applicant",
                "phone": "09171234567",
                "qualification_summary": "Qualified applicant.",
                "performance_rating_applicability": (
                    RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
                ),
            }
            data.update(overrides)
            form = ApplicantApplicationForm(data=data, entry=self.level1_position)
        form.is_valid()
        return form

    def test_intake_rejects_names_without_letters(self):
        for field_name in ("first_name", "last_name"):
            for value in ("2121323", "---"):
                with self.subTest(field_name=field_name, value=value):
                    form = self.intake_field_validation_form(**{field_name: value})
                    self.assertEqual(
                        form.errors[field_name],
                        [APPLICANT_NAME_ERROR_MESSAGE],
                    )

    def test_intake_accepts_permissive_unicode_names(self):
        valid_names = [
            "Ma. Cristina",
            "de la Cruz",
            "dela Cruz",
            "Ni\u00f1o",
            "Jos\u00e9",
            "Dela Cruz-Santos",
            "Jr.",
        ]
        for value in valid_names:
            with self.subTest(value=value):
                form = self.intake_field_validation_form(first_name=value, last_name=value)
                self.assertNotIn("first_name", form.errors)
                self.assertNotIn("last_name", form.errors)
                self.assertEqual(form.cleaned_data["first_name"], value)
                self.assertEqual(form.cleaned_data["last_name"], value)

    def test_intake_normalizes_name_whitespace(self):
        form = self.intake_field_validation_form(
            first_name="  Ma.   Cristina  ",
            last_name="  de   la   Cruz  ",
        )

        self.assertEqual(form.cleaned_data["first_name"], "Ma. Cristina")
        self.assertEqual(form.cleaned_data["last_name"], "de la Cruz")

    def test_intake_rejects_malformed_mobile_numbers(self):
        invalid_numbers = ["3123123", "0917123456", "+638171234567", "0917ABC4567"]
        for value in invalid_numbers:
            with self.subTest(value=value):
                form = self.intake_field_validation_form(phone=value)
                self.assertEqual(form.errors["phone"], [APPLICANT_MOBILE_ERROR_MESSAGE])

    def test_intake_accepts_and_normalizes_supported_mobile_shapes(self):
        valid_numbers = [
            "0917 123 4567",
            "+63 (917) 123-4567",
            "639171234567",
        ]
        for value in valid_numbers:
            with self.subTest(value=value):
                form = self.intake_field_validation_form(phone=value)
                self.assertNotIn("phone", form.errors)
                self.assertEqual(form.cleaned_data["phone"], "+639171234567")

    def test_intake_stores_normalized_mobile_number(self):
        self.start_portal_draft(
            self.client, self.level1_position, "normalized.mobile@example.com", phone="(0917) 123-4567"
        )

        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="normalized.mobile@example.com",
        )
        self.assertEqual(application.applicant_phone, "+639171234567")

    def test_intake_qualification_summary_allows_one_character(self):
        form = self.intake_field_validation_form(qualification_summary=" X ")

        self.assertNotIn("qualification_summary", form.errors)
        self.assertEqual(form.cleaned_data["qualification_summary"], "X")

    def test_intake_rejects_qualification_summary_over_limit(self):
        form = self.intake_field_validation_form(
            qualification_summary="X"
            * (APPLICANT_QUALIFICATION_SUMMARY_MAX_LENGTH + 1)
        )

        self.assertEqual(
            form.errors["qualification_summary"],
            [APPLICANT_QUALIFICATION_SUMMARY_LENGTH_ERROR_MESSAGE],
        )

    def test_intake_rejects_overlong_names(self):
        overlong = "A" * (APPLICANT_NAME_MAX_LENGTH + 1)
        for field_name in ("first_name", "last_name"):
            with self.subTest(field_name=field_name):
                form = self.intake_field_validation_form(**{field_name: overlong})
                self.assertEqual(
                    form.errors[field_name],
                    [APPLICANT_NAME_LENGTH_ERROR_MESSAGE],
                )

    def test_intake_renders_name_maxlength_attribute(self):
        form = ApplicantStartForm(entry=self.level1_position)
        for field_name in ("first_name", "last_name"):
            with self.subTest(field_name=field_name):
                self.assertEqual(
                    form.fields[field_name].widget.attrs.get("maxlength"),
                    str(APPLICANT_NAME_MAX_LENGTH),
                )

    def test_intake_rejects_overlong_cover_letter(self):
        form = self.intake_field_validation_form(
            cover_letter="X" * (APPLICANT_COVER_LETTER_MAX_LENGTH + 1)
        )

        self.assertEqual(
            form.errors["cover_letter"],
            [APPLICANT_COVER_LETTER_LENGTH_ERROR_MESSAGE],
        )

    def test_intake_accepts_cover_letter_at_limit(self):
        form = self.intake_field_validation_form(
            cover_letter="X" * APPLICANT_COVER_LETTER_MAX_LENGTH
        )

        self.assertNotIn("cover_letter", form.errors)

    def test_intake_email_validation_behavior_is_unchanged(self):
        invalid_form = self.intake_field_validation_form(email="not-an-email")
        valid_form = self.intake_field_validation_form(
            email="  Portal.Validation@Example.COM  "
        )

        self.assertIn("email", invalid_form.errors)
        self.assertNotIn("email", valid_form.errors)
        self.assertEqual(
            valid_form.cleaned_data["email"],
            "portal.validation@example.com",
        )

    def test_shared_portal_lists_plantilla_and_cos_paths(self):
        response = self.client.get(reverse("applicant-portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Job Openings")
        self.assertContains(response, self.level1_position.title)
        self.assertContains(response, self.cos_position.title)
        self.assertNotContains(response, "Internal Login")
        self.assertNotContains(response, "My Queue")
        self.assertNotContains(response, "Manage Entries")
        self.assertNotContains(response, "Internal Users")

    @override_settings(APPLICATION_OTP_VALIDITY_MINUTES=12)
    def test_applicant_help_displays_configured_otp_duration(self):
        response = self.client.get(reverse("applicant-help"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Each code works for 12 minutes.")

    def test_applicant_home_shows_trust_notes_at_bottom_only(self):
        response = self.client.get(reverse("applicant-portal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rg-pub-home-footer-notes")
        self.assertNotContains(response, "rg-pub-trust-strip")
        self.assertContains(response, "Official DOH&ndash;CHD CALABARZON portal")
        self.assertContains(response, "RA 10173 Data Privacy compliant")
        self.assertContains(response, "ro4a.doh.gov.ph")

    def test_vacancy_detail_uses_single_bottom_apply_button_without_clock_icon(self):
        response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content.decode("utf-8").count(
                reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
            ),
            1,
        )
        self.assertNotContains(response, "&#9200;")

    @override_settings(MAX_EVIDENCE_UPLOAD_BYTES=10 * 1024 * 1024)
    def test_intake_document_controls_use_effective_server_upload_limit(self):
        client, application = self.open_verified_application("limits@example.com")
        response = client.get(reverse("applicant-application", kwargs={"token": application.public_token}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["max_upload_bytes"], 5 * 1024 * 1024)
        self.assertEqual(response.context["max_upload_mb"], 5)
        self.assertContains(response, 'data-max-upload-bytes="5242880"')
        self.assertContains(response, "data-part-input")
        self.assertContains(response, 'name="submission_confirmation"')
        self.assertNotContains(response, 'name="checklist_privacy_consent"')
        self.assertNotContains(response, 'name="checklist_documents_complete"')
        self.assertNotContains(response, 'name="checklist_information_certified"')

    def test_deadline_passed_vacancy_is_not_open_for_applicant_intake(self):
        self.level1_position.closing_date = timezone.localdate() - timedelta(days=1)
        self.level1_position.save(update_fields=["closing_date", "updated_at"])

        self.level1_position.refresh_from_db()
        self.assertFalse(self.level1_position.is_open_for_intake)
        self.assertTrue(self.level1_position.applicant_pool_is_finalized)

        portal_response = self.client.get(reverse("applicant-portal"))
        self.assertNotContains(portal_response, self.level1_position.title)

        detail_response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk})
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertFalse(detail_response.context["can_apply"])
        self.assertContains(detail_response, "Applications for this position are closed.")
        self.assertNotContains(detail_response, "How to Apply")
        self.assertNotContains(detail_response, "Apply for This Position")

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        # A closed-but-existing vacancy now redirects applicants back to the openings
        # list with an explanation instead of dead-ending on a staff-flavored 404.
        self.assertEqual(intake_response.status_code, 302)
        self.assertEqual(intake_response.url, reverse("applicant-portal"))

    def test_plantilla_fourteenth_publication_day_still_accepts_intake(self):
        publication_date = timezone.localdate() - timedelta(days=13)
        self.level1_position.publication_date = publication_date
        self.level1_position.opening_date = publication_date
        self.level1_position.closing_date = PositionPosting.calculate_plantilla_closing_date(
            publication_date
        )
        self.level1_position.save(
            update_fields=["publication_date", "opening_date", "closing_date", "updated_at"]
        )

        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.closing_date, timezone.localdate())
        self.assertTrue(self.level1_position.is_open_for_intake)

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        self.assertEqual(intake_response.status_code, 200)

    def test_plantilla_fifteenth_publication_day_blocks_intake(self):
        publication_date = timezone.localdate() - timedelta(days=14)
        self.level1_position.publication_date = publication_date
        self.level1_position.opening_date = publication_date
        self.level1_position.closing_date = PositionPosting.calculate_plantilla_closing_date(
            publication_date
        )
        self.level1_position.save(
            update_fields=["publication_date", "opening_date", "closing_date", "updated_at"]
        )

        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.closing_date, timezone.localdate() - timedelta(days=1))
        self.assertFalse(self.level1_position.is_open_for_intake)

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        self.assertEqual(intake_response.status_code, 302)
        self.assertEqual(intake_response.url, reverse("applicant-portal"))

    def test_public_vacancy_detail_uses_reference_metadata_over_legacy_entry_text(self):
        self.level1_position.description = "Legacy description should stay hidden."
        self.level1_position.requirements = "Legacy requirements should stay hidden."
        self.level1_position.qualification_reference = "Entry-specific note for screening."
        self.level1_position.save(update_fields=["description", "requirements", "qualification_reference", "updated_at"])
        self.admin_aide_position.qs_education = "Bachelor's degree relevant to the role"
        self.admin_aide_position.save(update_fields=["qs_education", "updated_at"])

        response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Entry-specific note for screening.")
        self.assertContains(response, "Education: Bachelor&#x27;s degree relevant to the role")
        self.assertNotContains(response, "Legacy description should stay hidden.")
        self.assertNotContains(response, "Legacy requirements should stay hidden.")

    def test_plantilla_public_submission_requires_valid_otp_before_finalization(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "plantilla.portal@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="plantilla.portal@example.com",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(application.otp_hash)
        self.assertIsNone(application.submitted_at)

        # Without the code, neither the page nor a submit gets through.
        otp_url = reverse("applicant-otp", kwargs={"token": application.public_token})
        page_url = reverse("applicant-application", kwargs={"token": application.public_token})
        self.assertRedirects(client.get(page_url), otp_url, fetch_redirect_response=False)
        response = client.post(
            page_url,
            {**self.portal_answers(self.level1_position), "submission_confirmation": "on", "action": "submit"},
        )
        self.assertRedirects(response, otp_url, fetch_redirect_response=False)
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

        response = client.post(
            otp_url,
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertIsNotNone(application.otp_verified_at)
        # Verifying opens the application page with its single Submit.
        self.assertContains(response, "data-submit-application")

        self.save_portal_answers(client, application)
        self.upload_required_portal_documents(client, application)
        response = self.submit_portal_draft(client, application)
        application.refresh_from_db()
        self.assertContains(response, application.tracking_code)
        self.assertContains(response, application.case_number_short)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
            ).exists()
        )

    def test_otp_resend_is_a_secondary_button_not_a_link_styled_action(self):
        application = self.make_application(self.level1_position)

        response = self.client.get(
            reverse("applicant-otp", kwargs={"token": application.public_token})
        )

        self.assertEqual(response.status_code, 200)
        resend_button = re.search(
            r'<button\b[^>]*\bid="resend-btn"[^>]*>',
            response.content.decode("utf-8"),
        )
        self.assertIsNotNone(resend_button)
        self.assertRegex(resend_button.group(0), r'class="[^"]*\bbtn\b[^"]*"')
        self.assertNotContains(response, "rg-pub-resend-link")

    def test_verified_otp_summary_counts_only_current_active_applicant_documents(self):
        client, application = self.open_portal_draft("document.count@example.com")
        self.save_portal_answers(client, application)
        self.upload_required_portal_documents(client, application)
        # A newer version of one document replaces it (it still counts once).
        upload_evidence_item(
            application=application,
            actor=application.applicant,
            label="Signed cover letter",
            uploaded_file=self.real_pdf_upload("replacement-cover-letter"),
            document_key="signed_cover_letter",
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type="applicant_document",
        )
        # "No rating" archives the rating document, which then doesn't count at all.
        self.save_portal_answers(
            client,
            application,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        response = client.get(
            reverse("applicant-application", kwargs={"token": application.public_token})
        )
        self.assertEqual(response.status_code, 200)
        ready_slots = [slot for slot in response.context["slots"] if slot["is_ready"]]
        self.assertEqual(len(ready_slots), 8)
        self.assertNotIn("performance_rating", {slot["code"] for slot in ready_slots})

        receipt_response = self.submit_portal_draft(
            client,
            application,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.assertContains(receipt_response, "8 files submitted")
        self.assertNotContains(receipt_response, "10 files submitted")

    def test_otp_delivery_failure_keeps_saved_draft_without_server_error(self):
        client = Client()

        with self.assertLogs("recruitment.services", level="ERROR"):
            with patch(
                "recruitment.services.intake.EmailMultiAlternatives.send",
                side_effect=PermissionError("denied"),
            ):
                response = self.start_portal_draft(
                    client, self.level1_position, "otp.delivery.fail@example.com"
                )

        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="otp.delivery.fail@example.com",
        )
        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            fetch_redirect_response=False,
        )
        page = client.get(response["Location"])
        self.assertContains(page, "could not send the verification code right now")
        self.assertTrue(application.otp_hash)
        self.assertIsNone(application.submitted_at)

    def test_intake_get_rehydrates_existing_draft_from_token(self):
        client, application = self.open_portal_draft(
            "rehydrate.draft@example.com", first_name="Maria", last_name="Santos", phone="09991234567"
        )
        self.save_portal_answers(
            client,
            application,
            first_name="Maria",
            last_name="Santos",
            phone="09991234567",
            qualification_summary="Saved qualification summary.",
            cover_letter="Saved cover letter note.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_portal_document(client, application, "signed_cover_letter")

        # A bare draft link no longer opens the draft: it goes to e-mail verification first.
        fresh_client = Client()
        response = fresh_client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            {"token": application.public_token},
        )
        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            fetch_redirect_response=False,
        )
        response = fresh_client.get(
            reverse("applicant-application", kwargs={"token": application.public_token})
        )
        self.assertEqual(response.status_code, 302)

        # Once verified, the application page comes back prefilled with the saved draft.
        RecruitmentApplication.objects.filter(pk=application.pk).update(
            otp_requested_at=timezone.now()
            - timedelta(seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1)
        )
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            fresh_client.post(
                reverse("applicant-otp", kwargs={"token": application.public_token}),
                {"action": "resend"},
            )
        self.verify_portal_draft(fresh_client, application)
        response = fresh_client.get(
            reverse("applicant-application", kwargs={"token": application.public_token})
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["first_name"].value(), "Maria")
        self.assertEqual(form["last_name"].value(), "Santos")
        self.assertEqual(form["phone"].value(), "0999 123 4567")  # shown the way applicants write it
        self.assertEqual(form["qualification_summary"].value(), "Saved qualification summary.")
        self.assertEqual(form["cover_letter"].value(), "Saved cover letter note.")
        self.assertEqual(
            form["performance_rating_applicability"].value(),
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE,
        )
        slots = {slot["code"]: slot for slot in response.context["slots"]}
        self.assertTrue(slots["signed_cover_letter"]["is_ready"])

    def test_intake_reuses_identity_draft_when_application_email_drifted(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "identity.old@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="identity.old@example.com",
        )
        original_token = application.public_token
        application.applicant.email = "identity.new@example.com"
        application.applicant.save(update_fields=["email"])

        response = self.start_portal_draft(client, self.level1_position, "identity.new@example.com")

        # The identity's draft is found (no second draft, no server error) and is NOT
        # changed before its email is verified.
        application.refresh_from_db()
        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": original_token}),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(position=self.level1_position).count(),
            1,
        )
        self.assertEqual(application.public_token, original_token)
        self.assertEqual(application.applicant_email, "identity.old@example.com")

    def test_intake_blocks_submitted_identity_duplicate_without_server_error(self):
        _client, application, _response = self.apply_through_portal("submitted.old@example.com")
        application.applicant.email = "submitted.new@example.com"
        application.applicant.save(update_fields=["email"])

        response = self.start_portal_draft(Client(), self.level1_position, "submitted.new@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "You have already submitted an application for this position using this email address.",
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(position=self.level1_position).count(),
            1,
        )

    def test_non_merit_terminal_applications_may_reapply_but_rejection_stays_barred(self):
        withdrawn = self.submit_portal_application("reapply.withdrawn@example.com")
        old_withdrawn_case = withdrawn.case
        old_withdrawn_tracking = withdrawn.tracking_code
        old_withdrawn_reference = withdrawn.reference_number
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                withdrawn,
                self.secretariat,
                "Applicant withdrew and later chose to apply again.",
            )

        reapplied_after_withdrawal = self.submit_portal_application(
            "reapply.withdrawn@example.com"
        )
        withdrawn.refresh_from_db()
        old_withdrawn_case.refresh_from_db()
        self.assertNotEqual(reapplied_after_withdrawal.pk, withdrawn.pk)
        self.assertNotEqual(reapplied_after_withdrawal.case.pk, old_withdrawn_case.pk)
        self.assertNotEqual(reapplied_after_withdrawal.tracking_code, old_withdrawn_tracking)
        self.assertNotEqual(reapplied_after_withdrawal.reference_number, old_withdrawn_reference)
        self.assertEqual(withdrawn.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(old_withdrawn_case.case_status, RecruitmentCase.CaseStatus.WITHDRAWN)

        cancelled = self.submit_portal_application("reapply.cancelled@example.com")
        cancelled.status = RecruitmentApplication.Status.VACANCY_CANCELLED
        cancelled.closed_at = timezone.now()
        cancelled.save(update_fields=["status", "closed_at", "updated_at"])
        cancelled.case.case_status = RecruitmentCase.CaseStatus.VACANCY_CANCELLED
        cancelled.case.current_stage = RecruitmentCase.Stage.CLOSED
        cancelled.case.current_handler_role = ""
        cancelled.case.is_stage_locked = True
        cancelled.case.save(
            update_fields=[
                "case_status",
                "current_stage",
                "current_handler_role",
                "is_stage_locked",
                "updated_at",
            ]
        )
        reapplied_after_cancellation = self.submit_portal_application(
            "reapply.cancelled@example.com"
        )
        self.assertNotEqual(reapplied_after_cancellation.pk, cancelled.pk)
        self.assertNotEqual(reapplied_after_cancellation.case.pk, cancelled.case.pk)
        self.assertNotEqual(reapplied_after_cancellation.tracking_code, cancelled.tracking_code)
        self.assertNotEqual(
            reapplied_after_cancellation.reference_number,
            cancelled.reference_number,
        )

        rejected = self.submit_portal_application("reapply.rejected@example.com")
        rejected.status = RecruitmentApplication.Status.REJECTED
        rejected.closed_at = timezone.now()
        rejected.save(update_fields=["status", "closed_at", "updated_at"])
        rejected.case.case_status = RecruitmentCase.CaseStatus.REJECTED
        rejected.case.current_stage = RecruitmentCase.Stage.CLOSED
        rejected.case.current_handler_role = ""
        rejected.case.is_stage_locked = True
        rejected.case.save(
            update_fields=[
                "case_status",
                "current_stage",
                "current_handler_role",
                "is_stage_locked",
                "updated_at",
            ]
        )
        response = self.start_portal_draft(
            Client(), self.level1_position, "reapply.rejected@example.com"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "You have already submitted an application for this position using this email address.",
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                position=self.level1_position,
                applicant_email="reapply.rejected@example.com",
            ).count(),
            1,
        )

    def test_intake_get_ignores_invalid_or_mismatched_draft_token(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "wrong.position.token@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="wrong.position.token@example.com",
        )

        response = client.get(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
            {"token": application.public_token},
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIsNone(form["first_name"].value())
        self.assertNotIn("qualification_summary", form.fields)

    def test_cos_public_submission_completes_and_status_lookup_works(self):
        _client, application, receipt_response = self.apply_through_portal(
            "cos.portal@example.com", self.cos_position
        )

        self.assertContains(receipt_response, application.tracking_code)
        self.assertContains(receipt_response, application.case_number_short)
        self.assertContains(
            receipt_response,
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
        )
        self.assertEqual(application.branch, PositionPosting.Branch.COS)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

        # REV-I: the lookup no longer shows status directly — it emails a
        # one-time code first. Clear the submission-time OTP cooldown so the
        # lookup can send a fresh code immediately.
        application.otp_requested_at = timezone.now() - timedelta(minutes=5)
        application.save(update_fields=["otp_requested_at", "updated_at"])
        status_client = Client()
        status_response = status_client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": "cos.portal@example.com",
            },
        )
        self.assertContains(status_response, "Check Your Email")
        self.assertNotContains(status_response, "Under Review")

        verify_response = status_client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        self.assertContains(verify_response, "Under Review")
        self.assertContains(verify_response, application.case_number_short)

        # Option A session: within the verified window, a repeat lookup skips
        # the code step entirely.
        repeat_response = status_client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": "cos.portal@example.com",
            },
        )
        self.assertContains(repeat_response, "Under Review")

        # A wrong code never unlocks the status.
        fresh_client = Client()
        application.otp_requested_at = timezone.now() - timedelta(minutes=5)
        application.save(update_fields=["otp_requested_at", "updated_at"])
        fresh_client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": "cos.portal@example.com",
            },
        )
        bad_verify = fresh_client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": "000000"},
        )
        self.assertNotContains(bad_verify, "Under Review")
        self.assertContains(bad_verify, "invalid")

    def test_applicant_status_magic_link_renders_status_page(self):
        _client, application, _response = self.apply_through_portal(
            "magic.status@example.com", self.cos_position
        )
        application.otp_requested_at = timezone.now() - timedelta(
            seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1
        )
        application.save(update_fields=["otp_requested_at", "updated_at"])
        mail.outbox.clear()

        link_client = Client()
        status_link = reverse(
            "applicant-status-link", kwargs={"token": application.public_token}
        )
        response = link_client.get(status_link)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["otp_step"])
        self.assertContains(response, "Check Your Email")
        self.assertNotContains(response, "Application details")
        self.assertNotContains(response, application.tracking_code)
        self.assertEqual(len(mail.outbox), 1)

        verify_response = link_client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        self.assertContains(verify_response, application.case_number_short)
        self.assertContains(verify_response, application.applicant_display_name)
        self.assertContains(verify_response, "Application details")

        mail_count = len(mail.outbox)
        repeat_response = link_client.get(status_link)
        self.assertEqual(repeat_response.status_code, 200)
        self.assertContains(repeat_response, "Application details")
        self.assertNotContains(repeat_response, "Check Your Email")
        self.assertEqual(len(mail.outbox), mail_count)

    def test_applicant_status_link_uses_existing_lookup_verification_window(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        application.otp_requested_at = timezone.now() - timedelta(
            seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1
        )
        application.save(update_fields=["otp_requested_at", "updated_at"])

        client = Client()
        client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": application.applicant_email,
            },
        )
        verify_response = client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        self.assertContains(verify_response, "Application details")

        mail_count = len(mail.outbox)
        response = client.get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, application.case_number_short)
        self.assertContains(response, "Application details")
        self.assertNotContains(response, "Check Your Email")
        self.assertEqual(len(mail.outbox), mail_count)

    def test_submission_finalize_stamps_status_lookup_session(self):
        client, application, finalize_response = self.apply_through_portal(
            "finalize.status.session@example.com", self.cos_position
        )
        self.assertContains(finalize_response, "Application submitted successfully")

        mail_count = len(mail.outbox)
        response = client.get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, application.case_number_short)
        self.assertContains(response, "Application details")
        self.assertNotContains(response, "Check Your Email")
        self.assertEqual(len(mail.outbox), mail_count)

    def test_unfinished_draft_status_link_redirects_to_otp(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "draft.status.link@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="draft.status.link@example.com",
        )

        response = client.get(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            follow=True,
        )

        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
        )
        self.assertContains(response, "is not finished yet")
        self.assertContains(response, "Resend the code")

    def test_invalid_otp_is_rejected(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "invalid.otp@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="invalid.otp@example.com",
        )

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": "000000"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(response, "That code doesn&#x27;t match.")
        self.assertIsNone(application.otp_verified_at)

    @override_settings(APPLICATION_OTP_MAX_ATTEMPTS=2)
    def test_applicant_otp_locks_after_too_many_invalid_attempts_until_resend(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "otp.lockout@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="otp.lockout@example.com",
        )
        original_code = self.extract_otp_from_last_email()

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": "000000"},
            follow=True,
        )
        self.assertContains(response, "That code doesn&#x27;t match.")

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": "111111"},
            follow=True,
        )
        self.assertContains(response, "Too many invalid verification attempts")

        application.refresh_from_db()
        self.assertEqual(application.otp_attempt_count, 2)
        self.assertIsNone(application.otp_verified_at)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.APPLICATION_OTP_FAILED,
                metadata__reason="invalid_code",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.APPLICATION_OTP_LOCKED,
                metadata__attempt_count=2,
            ).exists()
        )

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": original_code},
            follow=True,
        )
        self.assertContains(response, "Too many invalid verification attempts")
        application.refresh_from_db()
        self.assertIsNone(application.otp_verified_at)

        application.otp_requested_at = timezone.now() - timedelta(
            seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1
        )
        application.save(update_fields=["otp_requested_at", "updated_at"])
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "resend"},
            follow=True,
        )
        self.assertContains(response, "A new verification code has been sent")

        application.refresh_from_db()
        self.assertEqual(application.otp_attempt_count, 0)
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        # Verified: the applicant is taken to the application page.
        self.assertContains(response, "data-submit-application")

    def test_applicant_otp_resend_respects_server_cooldown(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "otp.cooldown@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="otp.cooldown@example.com",
        )
        original_hash = application.otp_hash
        original_mail_count = len(mail.outbox)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "resend"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(response, "Please wait")
        self.assertEqual(application.otp_hash, original_hash)
        self.assertEqual(len(mail.outbox), original_mail_count)

        application.otp_requested_at = timezone.now() - timedelta(
            seconds=settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS + 1
        )
        application.save(update_fields=["otp_requested_at", "updated_at"])

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "resend"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(response, "A new verification code has been sent")
        self.assertNotEqual(application.otp_hash, original_hash)
        self.assertEqual(len(mail.outbox), original_mail_count + 1)

    def test_stale_applicant_otp_link_redirects_to_portal(self):
        client = Client()
        stale_token = uuid.uuid4()

        response = client.post(
            reverse("applicant-otp", kwargs={"token": stale_token}),
            {"action": "verify", "otp": "123456"},
            follow=True,
        )

        self.assertRedirects(response, reverse("applicant-portal"))
        self.assertContains(response, "This verification link is no longer available.")

    def test_stale_applicant_receipt_link_redirects_to_portal(self):
        client = Client()
        stale_token = uuid.uuid4()

        response = client.get(
            reverse("applicant-receipt", kwargs={"token": stale_token}),
            follow=True,
        )

        self.assertRedirects(response, reverse("applicant-portal"))
        self.assertContains(response, "This receipt link is no longer available.")

    def test_submitted_application_receipt_itemizes_current_requirements(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        from recruitment.services import get_current_applicant_document_items

        documents = list(get_current_applicant_document_items(application))
        self.assertGreaterEqual(len(documents), 2)

        response = self.applicant_status_verified_client(application).get(
            reverse("applicant-receipt", kwargs={"token": application.public_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"{len(documents)} file{'s' if len(documents) != 1 else ''} submitted",
        )
        for document in documents:
            with self.subTest(document_key=document.document_key):
                self.assertContains(response, document.label)
                self.assertContains(response, document.original_filename)

    def test_receipt_pdf_with_one_requirement_stays_on_one_page(self):
        from pypdf import PdfReader
        from recruitment.services import (
            _build_application_receipt_pdf,
            get_current_applicant_document_items,
        )

        application = self.make_application(self.level1_position)
        documents = list(get_current_applicant_document_items(application))
        self.assertTrue(documents)
        EvidenceVaultItem.objects.filter(
            pk__in=[document.pk for document in documents[1:]]
        ).update(is_archived=True, archive_tag="Single-item receipt fixture")
        application.submitted_at = timezone.now()
        application.save(update_fields=["submitted_at", "updated_at"])

        pdf_bytes = _build_application_receipt_pdf(application)
        reader = PdfReader(io.BytesIO(pdf_bytes))

        self.assertEqual(len(reader.pages), 1)
        self.assertIn(documents[0].label, reader.pages[0].extract_text())

    def test_receipt_pdf_starts_standard_eight_requirements_on_page_one(self):
        from pypdf import PdfReader
        from recruitment.services import (
            _build_application_receipt_pdf,
            get_current_applicant_document_items,
        )

        application = self.make_application(self.level1_position)
        documents = self.add_receipt_documents_until(application, 8)
        if len(documents) > 8:
            EvidenceVaultItem.objects.filter(
                pk__in=[document.pk for document in documents[8:]]
            ).update(is_archived=True, archive_tag="Eight-item receipt fixture")
        documents = list(get_current_applicant_document_items(application))
        self.assertEqual(len(documents), 8)
        application.submitted_at = timezone.now()
        application.save(update_fields=["submitted_at", "updated_at"])

        pdf_bytes = _build_application_receipt_pdf(application)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_texts = [
            " ".join((page.extract_text() or "").split()) for page in reader.pages
        ]

        self.assertIn(len(reader.pages), (1, 2))
        self.assertIn("Requirements Submitted", page_texts[0])
        self.assertIn(documents[0].label, page_texts[0])
        if len(reader.pages) == 2:
            self.assertIn("Requirements Submitted (continued)", page_texts[1])

    def test_receipt_pdf_paginates_twelve_requirements(self):
        from pypdf import PdfReader

        application = self.make_application(self.level1_position)
        documents = self.add_receipt_documents_until(application, 12)
        self.assertEqual(len(documents), 12)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        response = self.applicant_status_verified_client(application).get(
            reverse("applicant-receipt-pdf", kwargs={"token": application.public_token})
        )
        pdf_bytes = response.getvalue()
        reader = PdfReader(io.BytesIO(pdf_bytes))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(reader.pages), 2)
        page_texts = [
            " ".join((page.extract_text() or "").split()) for page in reader.pages
        ]
        self.assertIn("Requirements Submitted", page_texts[0])
        self.assertIn(documents[0].label, page_texts[0])
        self.assertIn("Requirements Submitted (continued)", page_texts[1])
        self.assertIn(documents[-1].label, page_texts[1])
        self.assertIn("What Happens Next", page_texts[1])
        for document in documents:
            with self.subTest(document_key=document.document_key):
                self.assertFalse(
                    document.label in page_texts[0]
                    and document.label in page_texts[1]
                )

    def test_submitted_application_receipt_downloads_as_pdf(self):
        # Panel revision: the application receipt must download as a real PDF
        # file (not only browser-print). Same gate as the receipt page.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        client = self.applicant_status_verified_client(application)

        response = client.get(
            reverse("applicant-receipt-pdf", kwargs={"token": application.public_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".pdf", response["Content-Disposition"])
        self.assertTrue(response.getvalue().startswith(b"%PDF-"))

    def test_stale_applicant_receipt_pdf_link_redirects_to_portal(self):
        client = Client()
        stale_token = uuid.uuid4()

        response = client.get(
            reverse("applicant-receipt-pdf", kwargs={"token": stale_token}),
            follow=True,
        )

        self.assertRedirects(response, reverse("applicant-portal"))
        self.assertContains(response, "This receipt link is no longer available.")

    def test_portal_intake_requires_requirement_coded_documents(self):
        client, application = self.open_portal_draft("missing.requirement@example.com")
        self.save_portal_answers(client, application)
        missing_code = "personal_data_sheet"
        uploaded = self.upload_required_portal_documents(client, application, skip={missing_code})

        response = self.submit_portal_draft(client, application)

        self.assertContains(response, "Missing document:")
        self.assertContains(response, f'href="#doc-{missing_code}"')
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)
        saved_codes = set(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                is_current_version=True,
                is_archived=False,
            ).values_list("document_key", flat=True)
        )
        self.assertNotIn(missing_code, saved_codes)
        self.assertEqual(saved_codes, set(uploaded))

    def test_requirement_catalog_drives_required_and_if_applicable_labels_in_public_pages(self):
        requirements = {
            requirement.code: requirement
            for requirement in get_applicant_document_requirements(self.level1_position.branch)
        }

        self.assertFalse(requirements["performance_rating"].is_required)
        self.assertTrue(requirements["performance_rating"].conditional_on_performance_rating)
        self.assertEqual(requirements["performance_rating"].applicant_label, "If applicable")
        self.assertTrue(
            all(
                requirement.applicant_label == "Required"
                for requirement in requirements.values()
                if requirement.code != "performance_rating"
            )
        )

        vacancy_response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk})
        )
        self.assertEqual(vacancy_response.status_code, 200)
        vacancy_labels = {
            requirement.code: requirement.applicant_label
            for requirement in vacancy_response.context["document_requirements"]
        }
        self.assertEqual(vacancy_labels["performance_rating"], "If applicable")
        self.assertEqual(vacancy_labels["signed_cover_letter"], "Required")
        self.assertEqual(
            vacancy_response.context["document_requirements"][-1].code,
            "performance_rating",
        )

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        # The start page lists the same catalog applicants will upload against.
        slot_labels = {
            requirement.code: requirement.applicant_label
            for requirement in intake_response.context["document_requirements"]
        }
        self.assertEqual(slot_labels["performance_rating"], "If applicable")
        self.assertEqual(slot_labels["personal_data_sheet"], "Required")

    def test_cos_requirement_catalog_drives_branch_specific_public_pages(self):
        requirements = {
            requirement.code: requirement
            for requirement in get_applicant_document_requirements(self.cos_position.branch)
        }

        self.assertNotIn("performance_rating", requirements)
        self.assertEqual(requirements["training_certificates"].applicant_label, "If applicable")
        self.assertIn("Signed Application Letter", requirements["signed_cover_letter"].title)
        self.assertEqual(requirements["transcript_of_records"].title, "Transcript of Records (TOR)")

        vacancy_response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.cos_position.pk})
        )
        self.assertEqual(vacancy_response.status_code, 200)
        self.assertContains(vacancy_response, "Signed Application Letter")
        self.assertContains(vacancy_response, "Transcript of Records (TOR)")
        self.assertContains(vacancy_response, "Training Certificates")
        self.assertContains(vacancy_response, "If applicable")
        self.assertNotContains(vacancy_response, "Performance Rating in the last rating period")

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk})
        )
        self.assertEqual(intake_response.status_code, 200)
        # The start page lists the same catalog applicants will upload against.
        slot_labels = {
            requirement.code: requirement.applicant_label
            for requirement in intake_response.context["document_requirements"]
        }
        self.assertNotIn("performance_rating", slot_labels)
        self.assertEqual(slot_labels["training_certificates"], "If applicable")

    def test_public_detail_and_intake_use_the_same_posting_requirements(self):
        PositionDocumentRequirement.objects.create(
            posting=self.cos_position,
            document_code=TRAINING_CERTIFICATES,
            is_required=True,
            order=1,
        )

        vacancy_response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.cos_position.pk})
        )
        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk})
        )
        self.assertEqual(vacancy_response.status_code, 200)
        self.assertEqual(intake_response.status_code, 200)

        detail_requirements = {
            (requirement.code, requirement.applicant_label)
            for requirement in vacancy_response.context["document_requirements"]
        }
        intake_requirements = {
            (requirement.code, requirement.applicant_label)
            for requirement in intake_response.context["document_requirements"]
        }
        self.assertEqual(detail_requirements, intake_requirements)
        self.assertEqual(
            detail_requirements,
            {(TRAINING_CERTIFICATES, "Required")},
        )

    def test_cos_submission_does_not_require_performance_rating_or_training_certificates(self):
        _client, application, response = self.apply_through_portal(
            "cos.no.rating@example.com", self.cos_position
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            application.performance_rating_applicability,
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE,
        )
        for code in ("performance_rating", "training_certificates"):
            self.assertFalse(
                application.evidence_items.filter(
                    artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                    artifact_type="applicant_document",
                    document_key=code,
                    is_current_version=True,
                    is_archived=False,
                ).exists()
            )

    def test_performance_rating_requires_upload_when_marked_applicable(self):
        client, application = self.open_portal_draft("rating.required@example.com")
        self.save_portal_answers(client, application)
        self.upload_required_portal_documents(client, application, skip={"performance_rating"})

        response = self.submit_portal_draft(client, application)

        self.assertContains(response, "Missing document:")
        self.assertContains(response, 'href="#doc-performance_rating"')
        performance_slot = next(
            slot for slot in response.context["slots"] if slot["code"] == "performance_rating"
        )
        self.assertTrue(performance_slot["required_now"])
        self.assertFalse(performance_slot["is_ready"])

    def test_performance_rating_not_applicable_allows_submission_without_file(self):
        not_applicable = RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
        _client, application, receipt_response = self.apply_through_portal(
            "no.rating@example.com", performance_rating_applicability=not_applicable
        )

        self.assertEqual(application.performance_rating_applicability, not_applicable)
        self.assertFalse(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="performance_rating",
                is_current_version=True,
                is_archived=False,
            ).exists()
        )
        self.assertContains(receipt_response, application.tracking_code)
        self.assertContains(receipt_response, application.case_number_short)

    def test_performance_rating_not_applicable_archives_saved_rating_evidence(self):
        client, application = self.open_portal_draft("saved.rating.not.applicable@example.com")
        self.save_portal_answers(client, application)
        rating_file = self.real_pdf_upload("same-file")
        self.upload_portal_document(client, application, "performance_rating", rating_file)
        self.assertTrue(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="performance_rating",
                is_current_version=True,
                is_archived=False,
            ).exists()
        )

        self.save_portal_answers(
            client,
            application,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )

        application.refresh_from_db()
        self.assertEqual(
            application.performance_rating_applicability,
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE,
        )
        self.assertFalse(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="performance_rating",
                is_current_version=True,
                is_archived=False,
            ).exists()
        )
        self.assertTrue(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="performance_rating",
                is_archived=True,
                archive_tag="Marked not applicable by applicant",
            ).exists()
        )
        # The dropped rating file no longer blocks reusing that file elsewhere.
        rating_file.seek(0)
        reused = self.upload_portal_document(
            client,
            application,
            "diploma",
            SimpleUploadedFile("diploma.pdf", rating_file.read(), content_type="application/pdf"),
        )
        self.assertEqual(reused.status_code, 200)

    def test_invalid_submission_preserves_non_file_inputs_and_saved_valid_uploads(self):
        client, application = self.open_portal_draft("preserved.inputs@example.com")
        self.save_portal_answers(
            client, application, qualification_summary="Persistent qualifications for validation retry."
        )
        self.upload_portal_document(client, application, "signed_cover_letter")

        response = self.submit_portal_draft(
            client,
            application,
            qualification_summary="Persistent qualifications for validation retry.",
        )

        self.assertContains(response, "Your application was not submitted yet.")
        form = response.context["form"]
        self.assertEqual(
            form["qualification_summary"].value(),
            "Persistent qualifications for validation retry.",
        )
        self.assertEqual(
            form["performance_rating_applicability"].value(),
            RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE,
        )
        slots = {slot["code"]: slot for slot in response.context["slots"]}
        self.assertTrue(slots["signed_cover_letter"]["is_ready"])
        self.assertFalse(slots["diploma"]["is_ready"])

    def test_empty_portal_submission_returns_validation_errors_without_crashing(self):
        client = Client()

        response = self.post_portal_intake(
            client,
            self.level1_position,
            {},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please fix the highlighted fields below.")
        self.assertContains(response, "Enter your first name.")
        self.assertFalse(
            RecruitmentApplication.objects.filter(position=self.level1_position).exists()
        )

    def test_duplicate_applicant_document_file_blocks_intake(self):
        client, application = self.open_portal_draft("duplicate.file@example.com")
        shared = self.real_pdf_upload("shared-document")
        first = self.upload_portal_document(client, application, "signed_cover_letter", shared)
        self.assertEqual(first.status_code, 200)
        shared.seek(0)
        second = self.upload_portal_document(
            client,
            application,
            "personal_data_sheet",
            SimpleUploadedFile("shared-document-again.pdf", shared.read(), content_type="application/pdf"),
        )

        self.assertEqual(second.status_code, 400)
        self.assertIn("already used for another document", second.json()["error"])
        self.assertIsNone(
            get_current_applicant_document_map(application).get("personal_data_sheet")
        )

    def test_duplicate_document_messages_are_concise_and_titleless(self):
        # Regression for the applicant feedback that the duplicate-file error
        # ballooned by listing every conflicting document's long formal title.
        from recruitment.applicant_documents import ERROR_SAME_FILE_ELSEWHERE

        client, application = self.open_portal_draft("concise.duplicate@example.com")
        shared = self.real_pdf_upload("shared")
        self.upload_portal_document(client, application, "signed_cover_letter", shared)
        shared.seek(0)
        response = self.upload_portal_document(
            client,
            application,
            "personal_data_sheet",
            SimpleUploadedFile("shared-again.pdf", shared.read(), content_type="application/pdf"),
        )
        error = response.json()["error"]
        self.assertEqual(error, ERROR_SAME_FILE_ELSEWHERE)
        long_titles = [
            requirement.title
            for requirement in get_applicant_document_requirements(self.level1_position)
            if len(requirement.title) > 40
        ]
        self.assertTrue(long_titles)  # sanity: the fixtures do have long titles
        for title in long_titles:
            self.assertNotIn(title, error)

    def test_final_submission_uses_requirement_codes_not_arbitrary_file_count(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            applicant_first_name="Generic",
            applicant_last_name="Counter",
            applicant_email="generic.counter@example.com",
            applicant_phone="09171230000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Application with incorrect requirement codes only.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
        )
        required_document_count = len(
            get_required_applicant_document_requirements(branch=self.level1_position.branch)
        )

        for index in range(required_document_count):
            upload_evidence_item(
                application=application,
                actor=self.applicant,
                label=f"Extra Applicant Document {index}",
                uploaded_file=self.build_valid_applicant_document_upload(
                    f"extra-{index}",
                    content_prefix=f"extra-{index}",
                ),
                document_key=f"extra_document_{index}",
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

        self.verify_application_for_submission(application)
        with self.assertRaisesMessage(
            ValueError,
            "Add all required documents before you submit.",
        ) as exc:
            submit_application(application, self.applicant)

        # Concise + title-free: no long formal document titles are enumerated.
        message = str(exc.exception)
        self.assertIn("still missing", message)
        self.assertNotIn("Signed Cover Letter", message)

    def test_final_submission_rejects_duplicate_document_content(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            applicant_first_name="Duplicate",
            applicant_last_name="Content",
            applicant_email="duplicate.content@example.com",
            applicant_phone="09171230000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Application with duplicate document content.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
        )
        duplicate_bytes = b"%PDF-1.4\nsame-file-for-two-document-slots\n%%EOF\n"

        for index, requirement in enumerate(
            get_required_applicant_document_requirements(branch=self.level1_position.branch)
        ):
            file_bytes = (
                duplicate_bytes
                if index < 2
                else self.build_valid_applicant_document_bytes(
                    requirement.code,
                    content_prefix=f"unique-{index}",
                )
            )
            upload_evidence_item(
                application=application,
                actor=self.applicant,
                label=requirement.title,
                uploaded_file=SimpleUploadedFile(
                    f"{requirement.code}.pdf",
                    file_bytes,
                    content_type="application/pdf",
                ),
                document_key=requirement.code,
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

        self.verify_application_for_submission(application)
        with self.assertRaisesMessage(
            ValueError,
            "Some documents use the same file. Use a different file for each document, then submit.",
        ):
            submit_application(application, self.applicant)

        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

    def test_portal_reuses_existing_draft_and_requirement_documents_for_same_entry_email(self):
        client, original_application = self.open_portal_draft("reused.draft@example.com")
        self.upload_portal_document(client, original_application, "signed_cover_letter")
        applicant_id = original_application.applicant_id

        # Starting again with the same email finds the same draft (and keeps its files).
        followup_response = self.start_portal_draft(
            Client(), self.level1_position, "reused.draft@example.com", first_name="Someone"
        )
        self.assertRedirects(
            followup_response,
            reverse("applicant-otp", kwargs={"token": original_application.public_token}),
            fetch_redirect_response=False,
        )
        self.save_portal_answers(
            client, original_application, qualification_summary="Updated qualifications for the same draft."
        )

        refreshed_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="reused.draft@example.com",
        )
        self.assertEqual(RecruitmentApplication.objects.filter(position=self.level1_position).count(), 1)
        self.assertEqual(refreshed_application.pk, original_application.pk)
        self.assertEqual(refreshed_application.applicant_id, applicant_id)
        self.assertEqual(refreshed_application.applicant_first_name, "Pat")
        self.assertEqual(
            refreshed_application.qualification_summary,
            "Updated qualifications for the same draft.",
        )
        self.assertIsNotNone(
            get_current_applicant_document_map(refreshed_application).get("signed_cover_letter")
        )
        self.assertEqual(
            RecruitmentUser.objects.filter(
                role=RecruitmentUser.Role.APPLICANT,
                email__iexact="reused.draft@example.com",
            ).count(),
            1,
        )

    def test_portal_reuses_existing_applicant_identity_by_email_across_entries(self):
        client = Client()
        shared_email = "shared.identity@example.com"
        first_response = self.start_portal_draft(client, self.level1_position, shared_email)
        self.assertEqual(first_response.status_code, 302)
        first_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email=shared_email,
        )

        second_response = self.start_portal_draft(client, self.cos_position, shared_email)
        self.assertEqual(second_response.status_code, 302)
        second_application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email=shared_email,
        )

        self.assertEqual(first_application.applicant_id, second_application.applicant_id)
        self.assertEqual(
            RecruitmentUser.objects.filter(
                role=RecruitmentUser.Role.APPLICANT,
                email__iexact=shared_email,
            ).count(),
            1,
        )

    def test_expired_otp_cannot_be_used_to_verify_or_finalize(self):
        client = Client()
        self.start_portal_draft(client, self.level1_position, "expired.otp@example.com")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="expired.otp@example.com",
        )
        otp_code = self.extract_otp_from_last_email()
        application.otp_expires_at = timezone.now() - timedelta(minutes=1)
        application.save(update_fields=["otp_expires_at", "updated_at"])

        otp_url = reverse("applicant-otp", kwargs={"token": application.public_token})
        response = client.post(otp_url, {"action": "verify", "otp": otp_code}, follow=True)
        self.assertContains(response, "The verification code has expired.")

        # An old verification on record is not a way in: submitting needs THIS browser to
        # have verified, so the submit is sent back to the code page.
        application.refresh_from_db()
        application.otp_verified_at = timezone.now() - timedelta(minutes=2)
        application.save(update_fields=["otp_verified_at", "updated_at"])
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.portal_answers(self.level1_position), "submission_confirmation": "on", "action": "submit"},
        )
        self.assertRedirects(response, otp_url, fetch_redirect_response=False)
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

    def test_issue_application_otp_defers_email_until_commit_inside_atomic_block(self):
        application = self.make_application(self.level1_position)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with transaction.atomic():
                otp_code = issue_application_otp(application, actor=application.applicant)
                application.refresh_from_db()
                self.assertTrue(application.otp_hash)
                self.assertEqual(len(mail.outbox), 0)
            self.assertEqual(len(mail.outbox), 0)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(len(mail.outbox), 0)

        callbacks[0]()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(otp_code, mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        html_body, mime_type = mail.outbox[0].alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Applicant Verification Code", html_body)
        self.assertIn(otp_code, html_body)

    def test_applicant_otp_falls_back_to_text_when_html_render_fails(self):
        application = self.make_application(self.level1_position)
        mail.outbox.clear()

        # Simulate a missing/broken "email/applicant_otp.html" template. The HTML
        # is only an alternative to the plain-text body, which already carries the
        # verification code, so delivery must degrade to text-only rather than
        # blocking the applicant's submission verification.
        with patch(
            "recruitment.services.intake.render_to_string",
            side_effect=RuntimeError("template render boom"),
        ), self.assertLogs("recruitment.services", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                otp_code = issue_application_otp(application, actor=application.applicant)

        # The code email is still delivered, degraded to text-only.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(otp_code, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].alternatives, [])
        self.assertTrue(
            any(
                "Failed to render applicant OTP HTML email" in message
                for message in logs.output
            )
        )


class CandidateCodeAllocationTests(BaseRecruitmentTestCase):
    def make_candidate_application(self, applicant, position, *, candidate_code=""):
        return RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_email=applicant.email,
            qualification_summary="Candidate-code constraint test.",
            candidate_code=candidate_code,
        )

    def make_applicant_user(self, suffix):
        return User.objects.create_user(
            username=f"candidate-code-{suffix}",
            email=f"candidate-code-{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )

    def test_candidate_code_is_unique_per_vacancy_when_non_blank(self):
        self.make_candidate_application(
            self.applicant,
            self.level1_position,
            candidate_code="C-07",
        )
        second_applicant = self.make_applicant_user("second")
        duplicate = self.make_candidate_application(
            second_applicant,
            self.level1_position,
        )

        duplicate.candidate_code = "C-07"
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                duplicate.save(update_fields=["candidate_code", "updated_at"])

        duplicate.refresh_from_db()
        self.assertEqual(duplicate.candidate_code, "")
        same_code_other_vacancy = self.make_candidate_application(
            second_applicant,
            self.level2_position,
            candidate_code="C-07",
        )
        self.assertEqual(same_code_other_vacancy.candidate_code, "C-07")

        third_applicant = self.make_applicant_user("third")
        another_blank = self.make_candidate_application(
            third_applicant,
            self.level1_position,
        )
        self.assertEqual(another_blank.candidate_code, "")

    def test_assign_candidate_code_retries_after_concurrent_collision(self):
        target = self.make_candidate_application(self.applicant, self.level1_position)
        contender = self.make_candidate_application(
            self.make_applicant_user("contender"),
            self.level1_position,
        )
        selected_codes = []

        def collide_on_first_choice(candidates):
            selected_code = candidates[0]
            if not selected_codes:
                RecruitmentApplication.objects.filter(pk=contender.pk).update(
                    candidate_code=selected_code
                )
            selected_codes.append(selected_code)
            return selected_code

        with patch(
            "recruitment.services.intake.secrets.choice",
            side_effect=collide_on_first_choice,
        ):
            assigned_code = assign_candidate_code(target)

        target.refresh_from_db()
        contender.refresh_from_db()
        self.assertEqual(len(selected_codes), 2)
        self.assertEqual(assigned_code, target.candidate_code)
        self.assertNotEqual(target.candidate_code, contender.candidate_code)

    def test_assign_candidate_code_stops_after_bounded_retries(self):
        target = self.make_candidate_application(self.applicant, self.level1_position)
        collision = IntegrityError("forced candidate-code collision")

        with patch.object(target, "save", side_effect=collision) as save_mock:
            with self.assertRaises(IntegrityError):
                assign_candidate_code(target)

        self.assertEqual(save_mock.call_count, IDENTIFIER_GENERATION_RETRIES)
        self.assertEqual(target.candidate_code, "")


class ApplicantPortalTurnoverTests(BaseRecruitmentTestCase):
    """TI-2: file preview before submitting, and the shared Plantilla/COS explainer shown where
    applicants decide (not only in the Help FAQ)."""

    def test_job_openings_list_shows_the_branch_legend(self):
        response = self.client.get(reverse("applicant-portal"))
        self.assertContains(response, 'data-branch-explainer="legend"')

    def test_vacancy_detail_explains_its_own_branch(self):
        response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk})
        )
        self.assertContains(response, 'data-branch-explainer="detail"')
        self.assertContains(response, "regular government post")

        response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.cos_position.pk})
        )
        self.assertContains(response, "time-bound engagement")

    def test_intake_form_names_the_branch_and_renders_the_preview_dialog(self):
        response = self.client.get(reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}))
        self.assertContains(response, 'data-branch-explainer="note"')
        self.assertContains(response, "You are applying for a <strong>Contract of Service</strong>")
        # The file preview now lives on the application page (after e-mail verification).
        self.assertContains(response, "data-start-form")

    def test_help_faq_uses_the_shared_explainer(self):
        response = self.client.get(reverse("applicant-help"))
        self.assertContains(response, 'data-branch-explainer="faq"')
        self.assertContains(response, "Contract of Service (COS)</strong> positions are time-bound")

    def test_csp_allows_local_blob_previews_only_for_images_and_frames(self):
        policy = self.client.get(reverse("applicant-portal"))["Content-Security-Policy"]
        directives = {
            part.strip().split(" ")[0]: part.strip().split(" ")[1:]
            for part in policy.split(";")
            if part.strip()
        }
        self.assertIn("blob:", directives["img-src"])
        self.assertIn("blob:", directives["frame-src"])
        self.assertNotIn("blob:", directives["default-src"])
        self.assertNotIn("blob:", directives.get("script-src", []))
        self.assertEqual(directives["object-src"], ["'none'"])


class ApplicantFormTemplatesTests(BaseRecruitmentTestCase):
    """Blank official CSC forms (PDS Revised 2026 + WES) and the plain-language PDS guide are
    offered wherever applicants are asked for those documents."""

    def test_pds_and_wes_requirements_carry_their_downloads(self):
        from django.contrib.staticfiles import finders

        from ..requirements import (
            PERSONAL_DATA_SHEET,
            WORK_EXPERIENCE_SHEET,
            get_applicant_document_requirements,
        )

        for position in (self.level1_position, self.cos_position):
            by_code = {r.code: r for r in get_applicant_document_requirements(position)}
            pds = by_code[PERSONAL_DATA_SHEET]
            self.assertIn("Revised 2026", pds.title)
            self.assertTrue(any(d.static_path.endswith("revised-2026.xlsx") for d in pds.downloads))
            self.assertTrue(by_code[WORK_EXPERIENCE_SHEET].downloads)
            for download in pds.downloads + by_code[WORK_EXPERIENCE_SHEET].downloads:
                if download.static_path:
                    self.assertIsNotNone(finders.find(download.static_path), download.static_path)

    def test_intake_form_offers_the_blank_forms_under_their_upload_slots(self):
        response = self.client.get(reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}))
        self.assertContains(response, "forms/cs-form-212-pds-revised-2026.xlsx")
        self.assertContains(response, "forms/cs-form-212-work-experience-sheet.docx")
        self.assertContains(response, reverse("applicant-pds-guide"))
        self.assertNotContains(response, "Revised 2025")

    def test_vacancy_page_lists_the_downloads_with_documents_needed(self):
        response = self.client.get(
            reverse("applicant-vacancy-detail", kwargs={"pk": self.cos_position.pk})
        )
        self.assertContains(response, "forms/cs-form-212-pds-revised-2026.xlsx")
        self.assertContains(response, "data-requirement-downloads")

    def test_help_page_has_a_forms_entry(self):
        response = self.client.get(reverse("applicant-help"))
        self.assertContains(response, "Where do I get the blank PDS and Work Experience Sheet?")
        self.assertContains(response, "forms/csc-pds-guide-revised-2026.pdf")
        self.assertContains(response, "CS Form No. 212, Revised 2026")
        self.assertNotContains(response, "Revised 2025")

    def test_pds_guide_page_renders_with_downloads_and_both_finishing_options(self):
        response = self.client.get(reverse("applicant-pds-guide"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "How to fill up your Personal Data Sheet (PDS)")
        self.assertContains(response, "forms/cs-form-212-pds-revised-2026.xlsx")
        self.assertContains(response, "Option A")
        self.assertContains(response, "Option B")
        self.assertContains(response, "Question 42")

    def test_resubmission_form_offers_the_blank_form_for_a_returned_pds(self):
        from ..forms import DocumentResubmissionForm
        from ..requirements import PERSONAL_DATA_SHEET, get_applicant_document_requirements

        pds = next(
            r for r in get_applicant_document_requirements(self.level1_position)
            if r.code == PERSONAL_DATA_SHEET
        )
        form = DocumentResubmissionForm(requirements=[pds], remarks_by_code={})
        self.assertEqual(form.fields[pds.file_field_name].requirement_downloads, pds.downloads)


class StepByStepApplicantIntakeTests(BaseRecruitmentTestCase):
    """Redesigned applicant intake: details -> verify e-mail -> application page where each
    file uploads on its own (several per document, combined into one PDF) -> one Submit."""

    def setUp(self):
        super().setUp()
        cache.clear()

    # -- helpers --------------------------------------------------------
    def real_pdf(self, label, pages=1):
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        for page in range(pages):
            pdf.drawString(72, 720, f"{label} page {page + 1}")
            pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    def pdf_upload(self, label, pages=1, filename=None):
        return SimpleUploadedFile(
            filename or f"{label}.pdf", self.real_pdf(label, pages), content_type="application/pdf"
        )

    def photo_upload(self, label, size=(3000, 2000), with_gps=True):
        from PIL import Image as PILImage

        image = PILImage.new("RGB", size, (200, 220, 240))
        buffer = io.BytesIO()
        exif = PILImage.Exif()
        exif[0x010F] = "PhoneMaker"  # camera make
        if with_gps:
            exif[0x8825] = {1: "N", 2: (14.0, 38.0, 0.0)}  # GPS IFD
        image.save(buffer, format="JPEG", exif=exif)
        return SimpleUploadedFile(f"{label}.jpg", buffer.getvalue(), content_type="image/jpeg")

    def start(self, client, position, email, first_name="Pat", last_name="Applicant"):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(
                reverse("applicant-intake", kwargs={"pk": position.pk}),
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": "09171234567",
                    "privacy_consent": "on",
                },
            )

    def verified_client(self, email, position=None):
        position = position or self.cos_position
        client = Client()
        self.start(client, position, email)
        application = RecruitmentApplication.objects.get(position=position, applicant_email=email)
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        return client, application

    def upload(self, client, application, code, uploaded_file):
        return client.post(
            reverse(
                "applicant-document-part-upload",
                kwargs={"token": application.public_token, "code": code},
            ),
            {"file": uploaded_file},
        )

    def current_document(self, application, code):
        from recruitment.services import get_current_applicant_document_map

        return get_current_applicant_document_map(application).get(code)

    def answers(self, **overrides):
        data = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "phone": "09171234567",
            "qualification_summary": "Two years of records work.",
            "cover_letter": "",
            "performance_rating_applicability": "not_applicable",
        }
        data.update(overrides)
        return data

    # -- start + verify ---------------------------------------------------
    def test_start_creates_a_details_only_draft_and_sends_a_code(self):
        response = self.start(Client(), self.cos_position, "new.start@example.com")
        application = RecruitmentApplication.objects.get(applicant_email="new.start@example.com")
        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            fetch_redirect_response=False,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.DRAFT)
        self.assertTrue(application.checklist_privacy_consent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Keep this email", mail.outbox[0].body)

    def test_start_requires_consent(self):
        response = Client().post(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
            {"first_name": "Pat", "last_name": "A", "email": "c@example.com", "phone": "09171234567"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Give your consent to continue.")
        self.assertFalse(RecruitmentApplication.objects.filter(applicant_email="c@example.com").exists())

    def test_starting_again_with_someone_elses_email_does_not_change_their_draft(self):
        self.start(Client(), self.cos_position, "owner@example.com", first_name="Maria")
        self.start(Client(), self.cos_position, "owner@example.com", first_name="Intruder")
        drafts = RecruitmentApplication.objects.filter(applicant_email="owner@example.com")
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().applicant_first_name, "Maria")

    def test_verifying_opens_the_application_page_for_this_browser_only(self):
        client, application = self.verified_client("verify.me@example.com")
        page = reverse("applicant-application", kwargs={"token": application.public_token})
        self.assertEqual(client.get(page).status_code, 200)
        # The draft link on its own (another browser) is sent back to verification.
        other = Client().get(page)
        self.assertRedirects(
            other,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            fetch_redirect_response=False,
        )
        denied = self.upload(Client(), application, "personal_data_sheet", self.pdf_upload("pds"))
        self.assertEqual(denied.status_code, 403)

    # -- documents --------------------------------------------------------
    def test_a_single_pdf_is_kept_byte_for_byte(self):
        client, application = self.verified_client("single@example.com")
        upload = self.pdf_upload("signed-pds", pages=4)
        original = upload.read()
        upload.seek(0)
        response = self.upload(client, application, "personal_data_sheet", upload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_ready"])
        evidence = self.current_document(application, "personal_data_sheet")
        self.assertEqual(evidence.sha256_digest, hashlib.sha256(original).hexdigest())

    def test_several_files_are_combined_into_one_pdf_in_order(self):
        from pypdf import PdfReader
        from recruitment.services import _decrypt_evidence_bytes

        client, application = self.verified_client("multi@example.com")
        self.upload(client, application, "training_certificates", self.pdf_upload("cert-a", pages=2))
        self.upload(client, application, "training_certificates", self.photo_upload("cert-b"))
        response = self.upload(client, application, "training_certificates", self.pdf_upload("cert-c"))
        self.assertEqual(len(response.json()["parts"]), 3)
        evidence = self.current_document(application, "training_certificates")
        combined = PdfReader(io.BytesIO(_decrypt_evidence_bytes(evidence)))
        self.assertEqual(len(combined.pages), 4)
        self.assertIn("cert-a page 1", combined.pages[0].extract_text())
        self.assertIn("cert-c page 1", combined.pages[3].extract_text())

    def test_photos_are_shrunk_and_stripped_of_location_data(self):
        from PIL import Image as PILImage
        from recruitment.applicant_documents import get_document_parts, read_applicant_document_part

        client, application = self.verified_client("photo@example.com")
        self.upload(client, application, "diploma", self.photo_upload("diploma", size=(4000, 3000)))
        part = get_document_parts(application, "diploma").get()
        with PILImage.open(io.BytesIO(read_applicant_document_part(part))) as stored:
            self.assertLessEqual(max(stored.size), 2400)
            self.assertNotIn(0x8825, stored.getexif())
            self.assertNotIn(0x010F, stored.getexif())

    def test_the_same_file_is_refused_twice_and_across_documents(self):
        client, application = self.verified_client("dupes@example.com")
        data = self.real_pdf("same")
        first = self.upload(client, application, "diploma", SimpleUploadedFile("a.pdf", data, "application/pdf"))
        self.assertEqual(first.status_code, 200)
        again = self.upload(client, application, "diploma", SimpleUploadedFile("b.pdf", data, "application/pdf"))
        self.assertEqual(again.status_code, 400)
        self.assertIn("already added", again.json()["error"])
        elsewhere = self.upload(
            client, application, "transcript_of_records", SimpleUploadedFile("c.pdf", data, "application/pdf")
        )
        self.assertEqual(elsewhere.status_code, 400)
        self.assertIn("another document", elsewhere.json()["error"])

    def test_bad_files_get_plain_errors(self):
        client, application = self.verified_client("bad@example.com")
        wrong_type = self.upload(
            client, application, "diploma", SimpleUploadedFile("notes.txt", b"hello", "text/plain")
        )
        self.assertEqual(wrong_type.status_code, 400)
        self.assertIn("PDF, JPG, or PNG", wrong_type.json()["error"])
        broken = self.upload(
            client, application, "diploma", SimpleUploadedFile("x.pdf", b"%PDF-1.4 broken", "application/pdf")
        )
        self.assertEqual(broken.status_code, 400)
        self.assertIn("open this PDF", broken.json()["error"])
        unknown = self.upload(client, application, "not_a_requirement", self.pdf_upload("x"))
        self.assertEqual(unknown.status_code, 400)

    def test_removing_the_last_file_clears_the_document(self):
        client, application = self.verified_client("remove@example.com")
        part_id = self.upload(client, application, "diploma", self.pdf_upload("dip")).json()["parts"][0]["id"]
        response = client.post(
            reverse(
                "applicant-document-part-delete",
                kwargs={"token": application.public_token, "part_id": part_id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_ready"])
        self.assertIsNone(self.current_document(application, "diploma"))

    def test_preview_returns_only_the_applicants_own_file(self):
        client, application = self.verified_client("preview@example.com")
        part_id = self.upload(client, application, "diploma", self.pdf_upload("mine")).json()["parts"][0]["id"]
        url = reverse(
            "applicant-document-part-preview",
            kwargs={"token": application.public_token, "part_id": part_id},
        )
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        other_client, other_application = self.verified_client("other.preview@example.com")
        stolen = other_client.get(
            reverse(
                "applicant-document-part-preview",
                kwargs={"token": other_application.public_token, "part_id": part_id},
            )
        )
        self.assertEqual(stolen.status_code, 404)

    # -- answers + submit -------------------------------------------------
    def test_answers_autosave(self):
        client, application = self.verified_client("autosave@example.com")
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(qualification_summary="Saved while typing."), "action": "autosave"},
        )
        self.assertEqual(response.json(), {"ok": True})
        application.refresh_from_db()
        self.assertEqual(application.qualification_summary, "Saved while typing.")

    def test_submit_is_blocked_until_required_documents_are_in(self):
        client, application = self.verified_client("missing@example.com")
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(), "submission_confirmation": "on", "action": "submit"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "required documents")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

    def test_full_submission_in_one_click_after_documents(self):
        from recruitment.applicant_documents import get_document_parts

        client, application = self.verified_client("complete@example.com")
        for requirement in get_required_applicant_document_requirements(application):
            self.assertEqual(
                self.upload(client, application, requirement.code, self.pdf_upload(requirement.code)).status_code,
                200,
            )
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(), "submission_confirmation": "on", "action": "submit"},
        )
        self.assertRedirects(
            response,
            reverse("applicant-receipt", kwargs={"token": application.public_token}),
            fetch_redirect_response=False,
        )
        application.refresh_from_db()
        self.assertIsNotNone(application.submitted_at)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        # Staging copies are gone; the Evidence Vault holds the record.
        self.assertFalse(get_document_parts(application).exists())
        # The draft page is closed for good.
        self.assertEqual(
            client.get(reverse("applicant-application", kwargs={"token": application.public_token})).status_code,
            302,
        )

    def test_submit_needs_the_certification_box(self):
        client, application = self.verified_client("cert@example.com")
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(), "action": "submit"},
        )
        self.assertContains(response, "Tick this box to submit your application.")

    def test_plantilla_no_rating_answer_drops_rating_files(self):
        client, application = self.verified_client("rating@example.com", position=self.level1_position)
        self.upload(client, application, "performance_rating", self.pdf_upload("ipcr"))
        self.assertIsNotNone(self.current_document(application, "performance_rating"))
        client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(performance_rating_applicability="not_applicable"), "action": "autosave"},
        )
        self.assertIsNone(self.current_document(application, "performance_rating"))

    # -- walkthrough improvements (Nov 2026) ------------------------------
    def test_plantilla_rating_question_starts_unanswered_and_is_asked_at_submit(self):
        client, application = self.verified_client("pr.blank@example.com", position=self.level1_position)
        self.assertEqual(application.performance_rating_applicability, "")
        page = client.get(reverse("applicant-application", kwargs={"token": application.public_token}))
        self.assertNotContains(page, 'value="applicable" checked')
        self.assertNotContains(page, 'value="not_applicable" checked')
        url = reverse("applicant-application", kwargs={"token": application.public_token})
        autosave = client.post(
            url, {**self.answers(performance_rating_applicability=""), "action": "autosave"}
        )
        self.assertEqual(autosave.json(), {"ok": True})
        submit = client.post(
            url,
            {
                **self.answers(performance_rating_applicability=""),
                "submission_confirmation": "on",
                "action": "submit",
            },
        )
        self.assertContains(submit, "Choose whether you have a performance rating.")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

    def test_rating_answer_is_kept_when_another_field_has_an_error(self):
        client, application = self.verified_client("pr.keep@example.com", position=self.level1_position)
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(first_name="", performance_rating_applicability="applicable"), "action": "submit"},
        )
        self.assertContains(response, "Enter your first name.")
        application.refresh_from_db()
        self.assertEqual(application.performance_rating_applicability, "applicable")
        rating_slot = next(s for s in response.context["slots"] if s["code"] == "performance_rating")
        self.assertTrue(rating_slot["applies"])

    def test_saved_message_is_shown_once(self):
        client, application = self.verified_client("once@example.com")
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(), "action": "save"},
            follow=True,
        )
        self.assertEqual(response.content.decode().count("Your answers were saved."), 1)

    def test_application_page_has_counters_jump_link_and_phone_progress_bar(self):
        client, application = self.verified_client("counter@example.com")
        page = client.get(reverse("applicant-application", kwargs={"token": application.public_token}))
        self.assertContains(page, 'data-char-limit="1000"')
        self.assertContains(page, "data-char-count-for")
        self.assertContains(page, 'href="#sec-docs"')
        self.assertContains(page, "data-doc-sticky")

    def test_error_summary_links_to_the_field(self):
        client, application = self.verified_client("links@example.com")
        response = client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.answers(first_name=""), "action": "submit"},
        )
        self.assertContains(response, 'href="#id_first_name"')

    def test_save_keeps_valid_answers_when_another_is_blank_or_wrong(self):
        client, application = self.verified_client("partial.save@example.com")
        url = reverse("applicant-application", kwargs={"token": application.public_token})
        # A blank summary is fine for a draft: "Save and finish later" just saves.
        response = client.post(url, {**self.answers(qualification_summary=""), "action": "save"})
        self.assertEqual(response.status_code, 302)
        original_phone = RecruitmentApplication.objects.get(pk=application.pk).applicant_phone
        self.assertEqual(original_phone, "+639171234567")
        # A mistyped number is refused, but the rest of what was typed is kept.
        response = client.post(
            url,
            {**self.answers(phone="12345", cover_letter="Kept even though the number is wrong."), "action": "save"},
        )
        self.assertContains(response, "Some answers could not be saved. Everything else was saved.")
        self.assertNotContains(response, "Your application was not submitted yet.")
        application.refresh_from_db()
        self.assertEqual(application.cover_letter, "Kept even though the number is wrong.")
        self.assertEqual(application.applicant_phone, original_phone)

    def test_wrong_code_says_how_many_tries_are_left(self):
        client = Client()
        self.start(client, self.cos_position, "tries@example.com")
        application = RecruitmentApplication.objects.get(applicant_email="tries@example.com")
        code = self.extract_otp_from_last_email()
        wrong = "000000" if code != "000000" else "111111"
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": wrong},
        )
        self.assertContains(response, "That code doesn&#x27;t match. Check the latest email from us and try again. You have")
        self.assertContains(response, "left.")
        self.assertNotContains(response, "before final submission")

    def test_closed_vacancy_page_says_it_is_closed(self):
        self.level1_position.closing_date = timezone.localdate() - timedelta(days=1)
        self.level1_position.save(update_fields=["closing_date", "updated_at"])
        response = Client().get(reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk}))
        self.assertContains(response, "Applications for this position are closed.")
        self.assertNotContains(response, "Click <strong>&ldquo;Apply for This Position&rdquo;</strong>")

    def test_job_cards_show_the_vacancy_code(self):
        response = Client().get(reverse("applicant-portal"))
        self.assertContains(response, self.cos_position.job_code)
        self.assertContains(response, "Posted ")

    def test_help_page_explains_saving_and_finishing_later(self):
        response = Client().get(reverse("applicant-help"))
        self.assertContains(response, "Can I save my application and finish later?")
        self.assertNotContains(response, "Go back to edit your information")
        self.assertNotContains(response, "TinyPNG")

    def test_receipt_shows_the_finished_journey(self):
        from recruitment.applicant_documents import build_document_slot_states

        client, application = self.verified_client("receipt.journey@example.com")
        for slot in build_document_slot_states(application, performance_rating_applicable=False):
            if slot["required_now"]:
                self.upload(client, application, slot["code"], self.pdf_upload(slot["code"]))
        with self.captureOnCommitCallbacks(execute=True):
            client.post(
                reverse("applicant-application", kwargs={"token": application.public_token}),
                {**self.answers(), "submission_confirmation": "on", "action": "submit"},
            )
        receipt = client.get(reverse("applicant-receipt", kwargs={"token": application.public_token}))
        self.assertContains(receipt, "rg-journey")
        self.assertContains(receipt, 'aria-current="step"')

    def test_draft_reminders_go_once_to_verified_drafts_closing_soon(self):
        from django.core.management import call_command

        self.level1_position.closing_date = timezone.localdate() + timedelta(days=2)
        self.level1_position.save(update_fields=["closing_date", "updated_at"])
        _client, verified = self.verified_client("remind.me@example.com", position=self.level1_position)
        self.start(Client(), self.level1_position, "never.verified@example.com")
        mail.outbox.clear()

        call_command("send_draft_reminders", stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["remind.me@example.com"])
        self.assertIn("not submitted yet", mail.outbox[0].subject)
        self.assertIn(
            reverse("applicant-otp", kwargs={"token": verified.public_token}), mail.outbox[0].body
        )
        verified.refresh_from_db()
        self.assertIsNotNone(verified.draft_reminder_sent_at)

        call_command("send_draft_reminders", stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_draft_reminders_skip_vacancies_closing_later(self):
        from recruitment.services import find_drafts_due_for_reminder

        self.level1_position.closing_date = timezone.localdate() + timedelta(days=20)
        self.level1_position.save(update_fields=["closing_date", "updated_at"])
        self.verified_client("later@example.com", position=self.level1_position)
        self.assertEqual(find_drafts_due_for_reminder(), [])

    # -- the old one-step path is gone -------------------------------------
    def test_old_one_step_post_cannot_change_someone_elses_draft(self):
        self.start(Client(), self.cos_position, "victim@example.com", first_name="Maria")
        draft = RecruitmentApplication.objects.get(applicant_email="victim@example.com")
        with self.captureOnCommitCallbacks(execute=True):
            Client().post(
                reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
                {
                    "first_name": "Intruder",
                    "last_name": "Person",
                    "email": "victim@example.com",
                    "phone": "09171234567",
                    "privacy_consent": "on",
                    "qualification_summary": "Overwritten summary.",
                    "checklist_privacy_consent": "on",
                    "personal_data_sheet": self.pdf_upload("intruder-pds"),
                },
            )
        draft.refresh_from_db()
        self.assertEqual(draft.applicant_first_name, "Maria")
        self.assertEqual(draft.qualification_summary, "")
        self.assertIsNone(self.current_document(draft, "personal_data_sheet"))
        self.assertEqual(RecruitmentApplication.objects.filter(applicant_email="victim@example.com").count(), 1)

    def test_code_page_no_longer_has_a_finalize_action(self):
        client, application = self.verified_client("no.finalize@example.com")
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "This applicant action is not available.")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)


class ApplicantPortalPolishTests(BaseRecruitmentTestCase):
    """In-app-browser tip, qualification self-check, deadline wording, abandoned-draft
    clean-up, document reuse and the vacancy funnel counts."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_facebook_in_app_browser_gets_an_open_in_browser_tip(self):
        url = reverse("applicant-intake", kwargs={"pk": self.cos_position.pk})
        facebook = self.client.get(
            url,
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/119.0 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/440.0.0.0;]",
        )
        self.assertContains(facebook, 'data-in-app-browser="facebook"')
        self.assertContains(facebook, "Open in browser")
        chrome = self.client.get(
            url, HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/119.0 Mobile Safari/537.36"
        )
        self.assertNotContains(chrome, "data-in-app-browser")

    def test_vacancy_page_offers_a_qualification_self_check(self):
        self.level1_position.qualification_reference = (
            "Education: Bachelor's degree. Training: 4 hours of relevant training. "
            "Experience: 1 year of relevant experience. Eligibility: Career Service (Subprofessional)."
        )
        self.level1_position.save(update_fields=["qualification_reference", "updated_at"])
        self.assertEqual(
            [label for label, _text in self.level1_position.qualification_items],
            ["Education", "Training", "Experience", "Eligibility"],
        )
        page = self.client.get(reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk}))
        self.assertContains(page, '<fieldset class="rg-qs-check"')
        self.assertContains(page, "Career Service (Subprofessional).")
        self.assertContains(page, "data-apply-link")

    def test_plain_qualification_text_stays_plain(self):
        self.level1_position.qualification_reference = "See the CSC qualification standards."
        self.level1_position.save(update_fields=["qualification_reference", "updated_at"])
        self.assertEqual(self.level1_position.qualification_items, [])
        page = self.client.get(reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk}))
        self.assertNotContains(page, '<fieldset class="rg-qs-check"')
        self.assertContains(page, "See the CSC qualification standards.")

    def test_deadline_says_the_exact_time_and_counts_down_on_the_last_day(self):
        self.level1_position.closing_date = timezone.localdate()
        self.level1_position.save(update_fields=["closing_date", "updated_at"])
        page = self.client.get(reverse("applicant-vacancy-detail", kwargs={"pk": self.level1_position.pk}))
        self.assertContains(page, "11:59 PM")
        self.assertContains(page, "(Philippine time)")
        self.assertContains(page, "data-closes-at")
        self.assertContains(page, "recruitguard-deadline.js")

        client, application = self.open_portal_draft("deadline.banner@example.com")
        app_page = client.get(reverse("applicant-application", kwargs={"token": application.public_token}))
        self.assertContains(app_page, "data-deadline-banner")
        self.assertContains(app_page, "Submit by 11:59 PM")

    def test_abandoned_draft_files_are_removed_but_the_record_stays(self):
        from django.core.management import call_command
        from recruitment.applicant_documents import get_document_parts

        client, draft = self.open_portal_draft("abandoned@example.com")
        self.upload_portal_document(client, draft, "personal_data_sheet")
        _client2, active_draft = self.open_portal_draft("still.open@example.com", position=self.cos_position)
        self.upload_portal_document(_client2, active_draft, "personal_data_sheet")
        RecruitmentApplication.objects.filter(pk__in=[draft.pk, active_draft.pk]).update(
            updated_at=timezone.now() - timedelta(days=45)
        )
        # Level 1 vacancy has closed; the COS pooling vacancy is still open.
        self.level1_position.closing_date = timezone.localdate() - timedelta(days=40)
        self.level1_position.save(update_fields=["closing_date", "updated_at"])

        call_command("purge_abandoned_drafts", stdout=io.StringIO())

        self.assertTrue(RecruitmentApplication.objects.filter(pk=draft.pk).exists())
        self.assertFalse(get_document_parts(draft).exists())
        item = EvidenceVaultItem.objects.get(application=draft, document_key="personal_data_sheet")
        self.assertEqual(bytes(item.ciphertext), b"")
        self.assertTrue(item.is_archived)
        self.assertTrue(item.sha256_digest)  # the fingerprint is kept
        self.assertTrue(
            AuditLog.objects.filter(application=draft, action=AuditLog.Action.EVIDENCE_ARCHIVED).exists()
        )
        # A draft on a vacancy that is still open is left alone.
        self.assertTrue(get_document_parts(active_draft).exists())

    def test_documents_can_be_reused_from_an_earlier_application(self):
        self.apply_through_portal("reuse.me@example.com")
        client, cos_draft = self.open_portal_draft("reuse.me@example.com", position=self.cos_position)
        page = client.get(reverse("applicant-application", kwargs={"token": cos_draft.public_token}))
        slots = {slot["code"]: slot for slot in page.context["slots"]}
        reusable = slots["personal_data_sheet"]["reusable"]
        self.assertIsNotNone(reusable)
        self.assertContains(page, "Use this file")
        # The signed letter names the earlier position, so it is never offered.
        self.assertIsNone(slots["signed_cover_letter"]["reusable"])

        reuse_url = reverse(
            "applicant-document-reuse",
            kwargs={"token": cos_draft.public_token, "code": "personal_data_sheet"},
        )
        wrong = client.post(reuse_url, {"evidence_id": reusable.pk + 999})
        self.assertEqual(wrong.status_code, 400)
        response = client.post(reuse_url, {"evidence_id": reusable.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["is_ready"])
        self.assertIsNotNone(get_current_applicant_document_map(cos_draft).get("personal_data_sheet"))

    def test_someone_elses_documents_are_never_offered(self):
        self.apply_through_portal("first.person@example.com")
        client, other_draft = self.open_portal_draft("second.person@example.com", position=self.cos_position)
        page = client.get(reverse("applicant-application", kwargs={"token": other_draft.public_token}))
        self.assertTrue(all(slot["reusable"] is None for slot in page.context["slots"]))
        first_pds = EvidenceVaultItem.objects.get(
            application__applicant_email="first.person@example.com",
            document_key="personal_data_sheet",
            is_current_version=True,
        )
        response = client.post(
            reverse(
                "applicant-document-reuse",
                kwargs={"token": other_draft.public_token, "code": "personal_data_sheet"},
            ),
            {"evidence_id": first_pds.pk},
        )
        self.assertEqual(response.status_code, 400)

    def test_vacancy_list_shows_how_far_applicants_got(self):
        self.apply_through_portal("funnel.done@example.com")
        self.open_portal_draft("funnel.verified@example.com")
        self.start_portal_draft(Client(), self.level1_position, "funnel.started@example.com")
        staff = Client()
        self.force_login_with_mfa(staff, self.secretariat)
        page = staff.get(reverse("recruitment-entry-list"))
        self.assertContains(page, "data-portal-funnel")
        funnel = next(e for e in page.context["entries"] if e.pk == self.level1_position.pk).portal_funnel
        self.assertEqual(
            (funnel["started"], funnel["verified"], funnel["with_documents"], funnel["submitted"]),
            (3, 2, 1, 1),
        )


class ApplicantPortalLanguageTests(BaseRecruitmentTestCase):
    """English / Filipino on the applicant portal; the staff portal stays English."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def filipino_client(self):
        client = Client()
        response = client.post(
            reverse("set_language"), {"language": "fil", "next": reverse("applicant-portal")}
        )
        self.assertEqual(response.status_code, 302)
        return client

    def test_language_switch_shows_the_portal_in_filipino(self):
        page = self.filipino_client().get(reverse("applicant-portal"))
        self.assertContains(page, '<html lang="fil">')
        self.assertContains(page, "Tingnan ang Job Openings")
        self.assertContains(page, "Paano mag-apply")
        self.assertContains(page, 'value="fil" lang="fil" class="rg-langswitch__btn is-current"')

    def test_english_is_the_default(self):
        page = Client().get(reverse("applicant-portal"))
        self.assertContains(page, '<html lang="en">')
        self.assertContains(page, "How it works")
        self.assertNotContains(page, "Paano mag-apply")

    def test_a_filipino_phone_gets_filipino_automatically(self):
        page = Client().get(reverse("applicant-portal"), HTTP_ACCEPT_LANGUAGE="fil-PH,fil;q=0.9")
        self.assertContains(page, "Tingnan ang Job Openings")

    def test_staff_pages_stay_in_english(self):
        client = self.filipino_client()
        page = client.get(reverse("login"))
        self.assertContains(page, '<html lang="en">')
        self.assertNotContains(page, "Tingnan ang Job Openings")

    def test_application_flow_messages_follow_the_language(self):
        client = self.filipino_client()
        response = client.post(
            reverse("applicant-intake", kwargs={"pk": self.cos_position.pk}),
            {"first_name": "", "last_name": "Cruz", "email": "x@example.com", "phone": "09171234567"},
        )
        self.assertContains(response, "Ilagay ang first name mo.")
        self.assertContains(response, "Pumayag muna para makapagpatuloy.")

        client, application = self.open_portal_draft("tagalog@example.com", client=client)
        page = client.get(reverse("applicant-application", kwargs={"token": application.public_token}))
        self.assertContains(page, "Ang documents mo")
        self.assertEqual(page.context["script_strings"]["saving"], "Nagse-save…")
        upload = self.upload_portal_document(
            client,
            application,
            "personal_data_sheet",
            SimpleUploadedFile("notes.txt", b"plain text", content_type="text/plain"),
        )
        self.assertEqual(upload.status_code, 400)
        self.assertIn("PDF, JPG, o PNG", upload.json()["error"])

    def test_every_applicant_string_has_a_filipino_translation(self):
        from django.core.management import call_command

        call_command("update_translations", "--check", stdout=io.StringIO())

    def test_contact_details_come_from_one_place(self):
        from recruitment import email_branding

        home = Client().get(reverse("applicant-portal"))
        self.assertContains(home, email_branding.CONTACT_EMAIL)
        self.assertContains(home, email_branding.CONTACT_PHONE)
        self.assertContains(home, email_branding.PUBLIC_DESK_PHONE)
        self.assertNotContains(home, "recruitment@chd4a.doh.gov.ph")
        help_page = Client().get(reverse("applicant-help"))
        self.assertContains(help_page, email_branding.CONTACT_EMAIL)
        self.assertContains(help_page, "Human Resource Management Unit (HRMU)")


class MyApplicationsAndLanguageEmailTests(BaseRecruitmentTestCase):
    """My Applications (one page per email), emails in the applicant's language, the old-PDS
    warning, and the translated Help page."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def hub_url(self):
        return reverse("applicant-my-applications")

    def sign_in_to_hub(self, email, client=None):
        client = client or Client()
        mail.outbox.clear()
        client.post(self.hub_url(), {"action": "request", "email": email})
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1) if mail.outbox else "000000"
        client.post(self.hub_url(), {"action": "verify", "otp": code})
        return client

    def test_my_applications_lists_every_application_for_the_email(self):
        _client, submitted, _response = self.apply_through_portal("hub.person@example.com")
        _c2, draft = self.open_portal_draft("hub.person@example.com", position=self.cos_position)
        client = self.sign_in_to_hub("hub.person@example.com")
        page = client.get(self.hub_url())
        self.assertContains(page, "data-hub-list")
        self.assertContains(page, self.level1_position.title)
        self.assertContains(page, self.cos_position.title)
        self.assertContains(page, "Continue application")
        self.assertContains(page, "View status")

        # Continue the draft without another code.
        response = client.post(reverse("applicant-my-application-open", kwargs={"token": draft.public_token}))
        self.assertRedirects(
            response,
            reverse("applicant-application", kwargs={"token": draft.public_token}),
            fetch_redirect_response=False,
        )
        self.assertEqual(client.get(response["Location"]).status_code, 200)
        # Open the submitted one's status without the tracking code.
        response = client.post(reverse("applicant-my-application-open", kwargs={"token": submitted.public_token}))
        status_page = client.get(response["Location"])
        self.assertContains(status_page, "Application details")

    def test_unknown_email_gets_the_same_answer_and_no_email(self):
        mail.outbox.clear()
        response = Client().post(self.hub_url(), {"action": "request", "email": "nobody@example.com"}, follow=True)
        self.assertContains(response, "If this email has applications with us")
        self.assertEqual(mail.outbox, [])

    def test_wrong_code_does_not_open_the_list(self):
        self.apply_through_portal("hub.guard@example.com")
        client = Client()
        client.post(self.hub_url(), {"action": "request", "email": "hub.guard@example.com"})
        response = client.post(self.hub_url(), {"action": "verify", "otp": "000000"})
        self.assertContains(response, "That code doesn&#x27;t match.")
        self.assertNotContains(response, "data-hub-list")

    def test_someone_elses_application_cannot_be_opened_from_the_hub(self):
        _c, other, _r = self.apply_through_portal("hub.other@example.com")
        self.apply_through_portal("hub.mine@example.com", position=self.cos_position)
        client = self.sign_in_to_hub("hub.mine@example.com")
        response = client.post(
            reverse("applicant-my-application-open", kwargs={"token": other.public_token}), follow=True
        )
        self.assertContains(response, "This application could not be found.")

    def test_emails_follow_the_language_the_applicant_used(self):
        client = Client()
        client.post(reverse("set_language"), {"language": "fil", "next": reverse("applicant-portal")})
        client, application = self.open_portal_draft("tagalog.mail@example.com", client=client)
        application.refresh_from_db()
        self.assertEqual(application.preferred_language, "fil")
        self.save_portal_answers(client, application)
        self.upload_required_portal_documents(client, application)
        mail.outbox.clear()
        self.submit_portal_draft(client, application)
        received = mail.outbox[-1]
        self.assertIn("natanggap ang application", received.subject)
        self.assertIn("Natanggap na ng DOH–CHD CALABARZON Recruitment Portal ang application mo.", received.body)
        html = received.alternatives[0][0]
        self.assertIn("Natanggap ang Application", html)
        self.assertIn('lang="fil"', html)

        # A staff action (English request) still emails this applicant in Filipino.
        from recruitment.notification_services import queue_selection_under_review_notification

        application.refresh_from_db()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            queue_selection_under_review_notification(application, actor=self.secretariat)
        self.assertIn("Update sa application mo", mail.outbox[-1].subject)

    def test_english_applicants_still_get_english_emails(self):
        _client, application, _response = self.apply_through_portal("english.mail@example.com")
        self.assertIn(application.preferred_language, ("", "en"))
        from recruitment.notification_services import queue_selection_under_review_notification

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            queue_selection_under_review_notification(application, actor=self.secretariat)
        self.assertIn("Update on your application for", mail.outbox[-1].subject)

    def test_old_2017_pds_gets_a_warning(self):
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        pdf.drawString(72, 720, "PERSONAL DATA SHEET")
        pdf.drawString(72, 60, "CS FORM 212 (Revised 2017), Page 1 of 4")
        pdf.showPage()
        pdf.save()
        old_pds = SimpleUploadedFile("pds-2017.pdf", buffer.getvalue(), content_type="application/pdf")

        client, application = self.open_portal_draft("old.pds@example.com")
        response = self.upload_portal_document(client, application, "personal_data_sheet", old_pds)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Revised 2017", response.json()["warning"])
        fresh = self.upload_portal_document(client, application, "diploma")
        self.assertNotIn("warning", fresh.json())

    def test_help_page_is_translated(self):
        client = Client()
        client.post(reverse("set_language"), {"language": "fil", "next": reverse("applicant-help")})
        page = client.get(reverse("applicant-help"))
        self.assertContains(page, "Mga Madalas na Tanong")
        self.assertNotContains(page, "data-english-only")
        guide = client.get(reverse("applicant-pds-guide"))
        self.assertContains(guide, "Paano mag-fill up ng Personal Data Sheet (PDS)")

    def test_one_menu_entry_for_everything_after_applying(self):
        home = Client().get(reverse("applicant-portal"))
        self.assertContains(home, reverse("applicant-my-applications"))
        self.assertNotContains(home, ">\n                Track Application\n")
        hub = Client().get(reverse("applicant-my-applications"))
        self.assertContains(hub, "Have a tracking code instead?")
        self.assertContains(hub, reverse("applicant-status-lookup"))
        lookup = Client().get(reverse("applicant-status-lookup"))
        self.assertContains(lookup, "See all your applications with just your email")


class ApplicantRoundETests(BatchExamHelpers, BaseRecruitmentTestCase):
    """Round E: fixes from docs/applicant-review-2026-11.md."""

    def _submitted(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        # The submission's own code is past its resend cooldown, as on any later visit.
        application.otp_requested_at = None
        application.save(update_fields=["otp_requested_at", "updated_at"])
        return application

    def _status_page(self, application):
        return self.applicant_status_verified_client(application).get(
            reverse("applicant-status-link", kwargs={"token": application.public_token})
        )

    # P2-1: the receipt needs a browser that proved the inbox.
    def test_a_stranger_with_the_receipt_link_gets_the_code_step(self):
        application = self._submitted()
        status_url = reverse("applicant-status-link", kwargs={"token": application.public_token})
        for name in ("applicant-receipt", "applicant-receipt-pdf"):
            with self.subTest(url=name):
                response = Client().get(reverse(name, kwargs={"token": application.public_token}))
                self.assertRedirects(response, status_url, fetch_redirect_response=False)
                self.assertNotIn(application.tracking_code.encode(), response.content)

    def test_after_the_code_the_stranger_lands_on_the_receipt(self):
        application = self._submitted()
        client = Client()
        receipt_url = reverse("applicant-receipt", kwargs={"token": application.public_token})
        client.get(receipt_url)
        mail.outbox.clear()
        page = client.get(reverse("applicant-status-link", kwargs={"token": application.public_token}))
        self.assertContains(page, 'name="otp"')
        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        response = client.post(reverse("applicant-status-lookup"), {"action": "verify", "otp": code})
        self.assertRedirects(response, receipt_url, fetch_redirect_response=False)
        self.assertContains(client.get(receipt_url), application.tracking_code)

    def test_a_my_applications_browser_gets_the_receipt_pdf(self):
        from ..portal_views import APPLICANT_HUB_SESSION_KEY

        application = self._submitted()
        client = Client()
        session = client.session
        session[APPLICANT_HUB_SESSION_KEY] = {
            "email": application.applicant_email,
            "verified_at": timezone.now().isoformat(),
        }
        session.save()
        response = client.get(reverse("applicant-receipt-pdf", kwargs={"token": application.public_token}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_the_submitting_browser_sees_the_receipt_at_once(self):
        _client, application, receipt = self.apply_through_portal(
            "round.e.submitter@example.com", self.level1_position
        )
        self.assertEqual(receipt.status_code, 200)
        self.assertContains(receipt, application.tracking_code)

    # P2-2: a scheduled exam ahead is the current step, never "done".
    def test_a_scheduled_exam_ahead_is_the_current_step(self):
        first, _second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        first.refresh_from_db()
        self.assertIn(first.status, {RecruitmentApplication.Status.SECRETARIAT_REVIEW, RecruitmentApplication.Status.HRM_CHIEF_REVIEW})
        html = self._status_page(first).content.decode()
        self.assertIn("Exam scheduled", html)
        self.assertRegex(html, r'Document check\s*<span class="visually-hidden">\(done\)')
        self.assertRegex(html, r'Examination\s*<span class="visually-hidden">\(in progress\)')
        self.assertNotRegex(html, r'Examination\s*<span class="visually-hidden">\(done\)')

    # P3-1: the request card goes once the office records the withdrawal.
    def test_withdrawal_request_card_goes_once_recorded(self):
        application = self._submitted()
        client = self.applicant_status_verified_client(application)
        withdraw_url = reverse("applicant-withdrawal-request", kwargs={"token": application.public_token})
        client.post(withdraw_url, {"confirm": "on", "reason": "Another job."})
        status_url = reverse("applicant-status-link", kwargs={"token": application.public_token})
        self.assertContains(client.get(status_url), "data-withdrawal-requested")
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(application, self.secretariat, "Another job.")
        page = client.get(status_url)
        self.assertContains(page, "Withdrawn")
        self.assertNotContains(page, "data-withdrawal-requested")

    # P3-3: no "your draft was saved" for a submitted application.
    def test_status_code_delivery_failure_is_worded_for_a_submitted_application(self):
        import smtplib

        application = self._submitted()
        with patch(
            "recruitment.services.intake.EmailMultiAlternatives.send",
            side_effect=smtplib.SMTPException("down"),
        ):
            page = Client().get(reverse("applicant-status-link", kwargs={"token": application.public_token}))
        self.assertContains(page, "We could not send a verification code right now.")
        self.assertNotContains(page, "draft and uploaded files were saved")
