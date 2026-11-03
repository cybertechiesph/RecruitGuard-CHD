"""The evidence vault, views and exports."""

import io
import json
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AuditLog,
    ComparativeAssessmentReport,
    EvidenceVaultItem,
    Notification,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentUser,
)
from ..requirements import get_required_applicant_document_requirements
from ..services import (
    ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
    build_export_bundle,
    decrypt_evidence_bytes,
    submit_application,
    upload_evidence_item,
)
from .base import User, BaseRecruitmentTestCase


class EvidenceVaultTests(BaseRecruitmentTestCase):
    def test_evidence_is_encrypted_and_digest_is_stored_with_stage_metadata(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        resume_bytes = self.build_valid_applicant_document_bytes(
            "resume",
            content_prefix="plain-text resume",
        )
        upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume.pdf",
                resume_bytes,
                content_type="application/pdf",
            ),
        )

        evidence = EvidenceVaultItem.objects.get(application=application)
        self.assertNotEqual(bytes(evidence.ciphertext), resume_bytes)
        self.assertEqual(len(evidence.sha256_digest), 64)
        self.assertEqual(evidence.digest_algorithm, "sha256")
        self.assertEqual(evidence.stage, EvidenceVaultItem.Stage.APPLICANT_INTAKE)
        self.assertEqual(evidence.artifact_scope, EvidenceVaultItem.OwnerScope.APPLICATION)
        self.assertEqual(evidence.application_id, application.id)
        self.assertIsNone(evidence.recruitment_case_id)
        self.assertIsNone(evidence.recruitment_entry_id)
        self.assertEqual(evidence.version_number, 1)
        self.assertTrue(evidence.is_current_version)
        self.assertEqual(evidence.uploaded_by_role, RecruitmentUser.Role.APPLICANT)

    def test_reuploading_same_label_preserves_version_history(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        first_version = upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume-v1.pdf",
                self.build_valid_applicant_document_bytes(
                    "resume",
                    content_prefix="resume version one",
                ),
                content_type="application/pdf",
            ),
        )
        second_version = upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume-v2.pdf",
                self.build_valid_applicant_document_bytes(
                    "resume",
                    content_prefix="resume version two",
                ),
                content_type="application/pdf",
            ),
        )

        first_version.refresh_from_db()
        second_version.refresh_from_db()
        self.assertEqual(first_version.version_number, 1)
        self.assertEqual(second_version.version_number, 2)
        self.assertEqual(second_version.artifact_scope, EvidenceVaultItem.OwnerScope.APPLICATION)
        self.assertEqual(first_version.version_family, second_version.version_family)
        self.assertEqual(second_version.previous_version, first_version)
        self.assertFalse(first_version.is_current_version)
        self.assertTrue(second_version.is_current_version)
        self.assertEqual(EvidenceVaultItem.objects.filter(application=application).count(), 2)

    def test_applicant_document_upload_rejects_invalid_signature_even_with_pdf_extension(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )

        with self.assertRaisesMessage(
            ValueError,
            "We couldn't read this as a PDF, JPG, or PNG. Save it again, then upload it.",
        ):
            upload_evidence_item(
                application=application,
                actor=self.applicant,
                label="Signed Cover Letter",
                uploaded_file=SimpleUploadedFile(
                    "signed-cover-letter.pdf",
                    b"not-a-real-pdf",
                    content_type="application/pdf",
                ),
                document_key="signed_cover_letter",
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

    def test_generic_evidence_upload_rejects_active_content(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )

        with self.assertRaisesMessage(
            ValueError,
            "This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        ):
            upload_evidence_item(
                application=application,
                actor=self.applicant,
                label="Active Content",
                uploaded_file=SimpleUploadedFile(
                    "workflow-note.html",
                    b"<html><script>alert(1)</script></html>",
                    content_type="text/html",
                ),
                artifact_type="workflow_evidence",
            )

        self.assertFalse(EvidenceVaultItem.objects.filter(application=application).exists())

    def test_applicant_document_upload_rejects_files_larger_than_five_mb(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        oversized_bytes = b"%PDF-1.4\n" + (b"A" * (5 * 1024 * 1024)) + b"\n%%EOF\n"

        with self.assertRaisesMessage(
            ValueError,
            "This file is too large. Choose a file that is 5 MB or smaller.",
        ):
            upload_evidence_item(
                application=application,
                actor=self.applicant,
                label="Signed Cover Letter",
                uploaded_file=SimpleUploadedFile(
                    "signed-cover-letter.pdf",
                    oversized_bytes,
                    content_type="application/pdf",
                ),
                document_key="signed_cover_letter",
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

    def test_system_admin_cannot_download_evidence_content(self):
        # Oversight is metadata-only: the System Administrator must not be able
        # to decrypt and read an applicant's document content.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            )
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                actor=self.sysadmin,
                action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_car_artifact_is_blinded_and_hrmpsb_decrypt_is_denied_and_audited(self):
        from pypdf import PdfReader

        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(
            application,
            self.hrmpsb,
            ranking_position=1,
        )
        draft = ComparativeAssessmentReport.objects.get(
            recruitment_entry=self.level1_position,
            is_finalized=False,
        )
        secretariat_bytes = decrypt_evidence_bytes(
            draft.evidence_item,
            self.secretariat,
            application=application,
        )
        self.assertTrue(secretariat_bytes.startswith(b"%PDF"))

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)
        application.refresh_from_db()
        evidence = report.evidence_item

        self.assertEqual(
            evidence.artifact_type,
            ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
        )
        with self.assertRaisesMessage(
            ValueError,
            "HRMPSB members cannot download the Comparative Assessment Report file.",
        ):
            decrypt_evidence_bytes(evidence, self.hrmpsb, application=application)

        denial = AuditLog.objects.get(
            actor=self.hrmpsb,
            action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
            metadata__evidence_id=evidence.id,
        )
        self.assertEqual(
            denial.metadata["reason"],
            "car_artifact_restricted_for_hrmpsb",
        )
        self.assertFalse(
            AuditLog.objects.filter(
                actor=self.hrmpsb,
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

        appointing_bytes = decrypt_evidence_bytes(
            evidence,
            self.appointing,
            application=application,
        )
        pdf_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(io.BytesIO(appointing_bytes)).pages
        )
        self.assertIn(application.candidate_code, pdf_text)
        self.assertNotIn(application.reference_number, pdf_text)
        self.assertNotIn(application.applicant_display_name, pdf_text)

    def test_system_admin_cannot_inline_view_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 403)

    def test_assigned_handler_can_view_pdf_evidence_inline(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )
        self.assertEqual(evidence.content_type, "application/pdf")

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            f'inline; filename="{evidence.original_filename}"',
        )
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'none'", response["Content-Security-Policy"])
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_inline_request_forced_to_attachment_for_disallowed_content_type(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )
        evidence.content_type = "application/octet-stream"
        evidence.save(update_fields=["content_type"])

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="{evidence.original_filename}"',
        )
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        # The attachment response keeps the global CSP; it must not pick up the
        # hardened inline policy that disables scripts entirely.
        self.assertNotIn("script-src 'none'", response["Content-Security-Policy"])

    def test_inline_view_request_still_enforces_permission(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_application_detail_hides_evidence_vault_and_audit_links_for_non_admin_users(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        evidence_url = reverse(
            "evidence-download",
            kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
        )
        audit_url = reverse("application-audit-log", kwargs={"pk": application.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, evidence.label)
        self.assertContains(response, evidence.original_filename)
        # Reviewers get an inline "View" link for viewable documents alongside
        # the download link, but the Evidence Vault and audit links stay hidden.
        self.assertContains(response, f"{evidence_url}?disposition=inline")
        self.assertContains(response, evidence_url)
        self.assertNotContains(response, audit_url)
        self.assertContains(response, "Download")
        self.assertNotContains(response, "Evidence Vault")

    def test_evidence_service_rejects_unauthorized_upload_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot upload files for this application.",
        ):
            upload_evidence_item(
                application=application,
                actor=self.hrmpsb,
                label="Late Submission",
                uploaded_file=SimpleUploadedFile(
                    "late.txt",
                    b"late evidence",
                    content_type="text/plain",
                ),
            )

    def test_case_owned_workflow_evidence_uses_recruitment_case_owner(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        evidence = upload_evidence_item(
            application=application,
            actor=self.secretariat,
            label="Secretariat Routing Notes",
            uploaded_file=SimpleUploadedFile(
                "routing-notes.pdf",
                self.build_valid_applicant_document_bytes(
                    "routing-notes",
                    content_prefix="internal routing notes",
                ),
                content_type="application/pdf",
            ),
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type="workflow_evidence",
        )

        self.assertEqual(evidence.artifact_scope, EvidenceVaultItem.OwnerScope.CASE)
        self.assertIsNone(evidence.application_id)
        self.assertEqual(evidence.recruitment_case_id, application.case.id)
        self.assertIsNone(evidence.recruitment_entry_id)

    def test_system_admin_can_search_and_review_but_cannot_archive(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=get_required_applicant_document_requirements()[0].code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        # Metadata oversight: the vault lists the file and its owning case, but
        # exposes no download link and no content.
        review_response = client.get(
            reverse("evidence-vault-list"),
            {
                "q": application.reference_label,
                "archival_status": "all",
            },
        )
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, application.reference_label)
        self.assertContains(review_response, evidence.original_filename)
        download_url = reverse(
            "evidence-download",
            kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
        )
        self.assertNotContains(review_response, download_url)

        # Archiving mutates the secured-files store and is denied for the
        # oversight role.
        archive_response = client.post(
            reverse(
                "evidence-archive-toggle",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            ),
            {
                "action": "archive",
                "archive_tag": "Closed case retention batch",
                "next": reverse("evidence-vault-list"),
            },
        )
        self.assertEqual(archive_response.status_code, 403)
        evidence.refresh_from_db()
        self.assertFalse(evidence.is_archived)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.EVIDENCE_ARCHIVED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_system_admin_cannot_upload_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.post(
            reverse("evidence-upload", kwargs={"pk": application.pk}),
            {"label": "Admin Upload"},
        )

        self.assertEqual(response.status_code, 403)

    def test_assigned_workflow_handler_can_download_screening_document(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content,
            self.build_valid_applicant_document_bytes(requirement_code),
        )

    def test_unassigned_internal_roles_cannot_download_application_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )

        roles = {
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            self.force_login_with_mfa(client, user)
            with self.subTest(role=label):
                response = client.get(
                    reverse(
                        "evidence-download",
                        kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
                    )
                )
                self.assertEqual(response.status_code, 403)

        denied_logs = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
            metadata__evidence_id=evidence.id,
        )
        self.assertEqual(denied_logs.count(), len(roles))
        self.assertTrue(all(log.is_sensitive_access for log in denied_logs))
        self.assertEqual(
            set(denied_logs.values_list("metadata__reason", flat=True)),
            {"unauthorized"},
        )

    def test_non_admin_roles_cannot_access_evidence_vault_routes(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=get_required_applicant_document_requirements()[0].code,
        )

        roles = {
            "secretariat": self.secretariat,
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            self.force_login_with_mfa(client, user)
            with self.subTest(role=label, endpoint="evidence_vault_list"):
                response = client.get(reverse("evidence-vault-list"))
                self.assertEqual(response.status_code, 403)
            with self.subTest(role=label, endpoint="evidence_upload"):
                response = client.post(
                    reverse("evidence-upload", kwargs={"pk": application.pk}),
                    {"label": "Restricted Upload"},
                )
                self.assertEqual(response.status_code, 403)
            with self.subTest(role=label, endpoint="evidence_archive_toggle"):
                response = client.post(
                    reverse(
                        "evidence-archive-toggle",
                        kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
                    ),
                    {
                        "action": "archive",
                        "archive_tag": "Restricted archive attempt",
                    },
                )
                self.assertEqual(response.status_code, 403)


class ViewAndExportTests(BaseRecruitmentTestCase):
    def test_dashboard_empty_state_renders_without_raw_include_tags(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No account or security events yet.")
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No identity events yet" copy="Account creation, role changes, and activation events will appear here." %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")
        self.assertNotContains(
            response,
            '{% include "internal_includes/banner.html" with variant="info" copy="No notices right now. Notices from the HRM Chief or administrators will appear here." %}',
        )
        self.assertNotContains(response, "{# Contextual banner partial.")

    def test_hrmpsb_queue_leads_with_candidate_code_not_case_number(self):
        # RD-5: REV-B's case-header rule applied to the queue — evaluators never
        # see the sequential case number (it weakly leaks submission order);
        # their rows lead with the vacancy code + candidate code.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        application.refresh_from_db()
        self.assertTrue(application.candidate_code)

        # The notification bell also renders reference labels (separate, pre-
        # existing surface) — clear it so the assertion targets the queue rows.
        Notification.objects.filter(recipient=self.hrmpsb).delete()

        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.get(reverse("workflow-queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Candidate {application.candidate_code}")
        self.assertContains(response, application.position.job_code)
        self.assertNotContains(response, application.reference_number)
        self.assertNotContains(response, application.applicant_display_name)

    def test_workflow_queue_empty_state_renders_without_raw_include_tags(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You're all caught up")
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="You\'re all caught up" copy="No cases need your action right now. New assignments will appear here." %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")

    def test_recruitment_entry_empty_state_renders_without_raw_include_tags(self):
        PositionPosting.objects.all().delete()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.get(reverse("recruitment-entry-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No vacancies yet")
        self.assertContains(response, reverse("recruitment-entry-create"))
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No vacancies yet" copy="Create a Plantilla or COS vacancy to start taking applications for a position." action_url=create_entry_url action_label="Create First Entry" %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")

    def test_recruitment_entry_list_renders_modal_partial_without_raw_include_tags(self):
        # WF-07: the close modal only renders once the entry is closable — move the
        # closing date to today so the modal (the subject of this test) appears.
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=timezone.localdate(),
            updated_at=timezone.now(),
        )
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.get(reverse("recruitment-entry-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Close this vacancy?")
        self.assertContains(response, f'data-bs-target="#closeModal{self.level1_position.pk}"')
        self.assertContains(response, f'id="closeModal{self.level1_position.pk}"')
        self.assertContains(response, f'id="closeForm{self.level1_position.pk}"')
        self.assertContains(response, f'form="closeForm{self.level1_position.pk}"')
        self.assertContains(response, "Reason for closing")
        self.assertContains(response, f'id="close-reason-{self.level1_position.pk}"')
        self.assertContains(response, 'name="reason"')
        self.assertContains(
            response,
            f'form="closeForm{self.level1_position.pk}" required',
        )
        self.assertContains(response, 'class="mb-0 rg-modal-confirm__body-copy"')
        self.assertNotContains(
            response,
            '{% include "internal_includes/modal_confirm.html" with modal_id="closeModal"|add:entry.pk|stringformat:"s" variant="destructive" title="Close this vacancy?" body="Closing will stop accepting new applications for this entry. Existing cases in the workflow will not be affected." confirm_label="Close Entry" form_id="closeForm"|add:entry.pk|stringformat:"s" %}',
        )
        self.assertNotContains(response, "{# Confirmation modal partial.")

    def test_system_admin_dashboard_shows_role_label_and_admin_only_nav(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Administrator")
        self.assertContains(response, "rg-pill--system-admin")
        self.assertContains(response, f'href="{reverse("evidence-vault-list")}"')
        self.assertContains(response, f'href="{reverse("audit-log-list")}"')
        self.assertContains(response, f'href="{reverse("internal-user-list")}"')
        self.assertContains(response, "Accounts")

    def test_non_admin_dashboard_redirects_to_vacancy_console(self):
        roles = {
            "secretariat": self.secretariat,
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            self.force_login_with_mfa(client, user)
            response = client.get(reverse("dashboard"))

            with self.subTest(role=label):
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("vacancy-batches"))

    def test_non_admin_sidebar_keeps_only_my_queue_link_for_case_navigation(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("workflow-queue")}"')
        self.assertContains(response, "My Queue")
        self.assertNotContains(response, f'href="{reverse("application-list")}"')
        self.assertNotContains(response, ">Applications<")

    def test_application_list_redirects_to_workflow_queue_for_internal_users(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.get(reverse("application-list"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("workflow-queue"))

    def test_workflow_queue_shows_current_task_for_active_case(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Needed Step")
        self.assertContains(response, "Screening")
        self.assertNotContains(response, "HRM Chief Review")
        # Guard against multi-line {# #} template comments leaking to the page
        # (Django only strips single-line comments) — the branch-filter toolbar
        # is where one previously rendered verbatim.
        self.assertNotContains(response, "{#")

    def test_workflow_queue_updates_current_task_after_screening_finalization(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Needed Step")
        self.assertContains(response, "Exam")
        self.assertNotContains(response, "HRM Chief Review")

    def test_applicant_user_cannot_access_internal_dashboard(self):
        client = Client()
        self.force_login_with_mfa(client, self.applicant)
        response = client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_applicant_user_cannot_access_internal_application_detail(self):
        other_applicant = User.objects.create_user(
            username="otherapplicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=other_applicant,
            position=self.level1_position,
            qualification_summary="Another applicant.",
        )

        client = Client()
        self.force_login_with_mfa(client, self.applicant)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 403)

    def test_export_bundle_returns_structured_zip_with_inventory_and_verification_outputs(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        root = f"{application.reference_number}/"
        self.assertIn(f"{root}records/application_summary.pdf", names)
        self.assertIn(f"{root}records/submission_packet.json", names)
        self.assertIn(f"{root}records/case_manifest.json", names)
        self.assertIn(f"{root}inventory/evidence_inventory.csv", names)
        self.assertIn(f"{root}inventory/evidence_inventory.pdf", names)
        self.assertIn(f"{root}logs/audit_log.csv", names)
        self.assertIn(f"{root}logs/routing_history.csv", names)
        self.assertIn(f"{root}verification/verification_report.json", names)
        self.assertIn(f"{root}verification/checksums.sha256", names)
        self.assertIn(f"{root}verification/verification_summary.pdf", names)
        evidence_paths = [name for name in names if name.startswith(f"{root}evidence/")]
        self.assertTrue(evidence_paths)

        manifest = json.loads(archive.read(f"{root}records/case_manifest.json").decode("utf-8"))
        verification_report = json.loads(
            archive.read(f"{root}verification/verification_report.json").decode("utf-8")
        )
        audit_log_csv = archive.read(f"{root}logs/audit_log.csv").decode("utf-8")
        inventory_csv = archive.read(f"{root}inventory/evidence_inventory.csv").decode("utf-8")
        checksums = archive.read(f"{root}verification/checksums.sha256").decode("utf-8")
        required_document_count = len(get_required_applicant_document_requirements())

        self.assertEqual(manifest["source_application"]["id"], application.id)
        self.assertEqual(manifest["source_case"]["id"], application.case.id)
        self.assertEqual(manifest["export"]["bundle_root"], root)
        self.assertEqual(manifest["export"]["evidence_file_count"], required_document_count)
        self.assertEqual(
            manifest["bundle_contents"]["verification_paths"],
            [
                f"{root}verification/verification_report.json",
                f"{root}verification/checksums.sha256",
                f"{root}verification/verification_summary.pdf",
            ],
        )
        self.assertEqual(verification_report["case_reference"], application.reference_number)
        self.assertEqual(verification_report["source_case_id"], application.case.id)
        self.assertEqual(verification_report["evidence_file_count"], required_document_count)
        self.assertTrue(all(item["digest_match"] for item in verification_report["evidence_files"]))
        self.assertTrue(
            any(
                covered["path"] == evidence_paths[0]
                for covered in verification_report["covered_files"]
            )
        )
        self.assertIn("case_reference,workflow_stage,actor,actor_role,action,is_sensitive_access", audit_log_csv)
        self.assertIn("stored_sha256_digest,exported_sha256_digest,digest_match", inventory_csv)
        self.assertIn(evidence_paths[0], inventory_csv)
        self.assertIn(evidence_paths[0], checksums)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXPORT_GENERATED,
            ).exists()
        )
        export_log = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.EXPORT_GENERATED,
        )
        self.assertEqual(export_log.metadata["bundle_root"], root)
        self.assertEqual(export_log.metadata["source_case_id"], application.case.id)
        self.assertEqual(export_log.metadata["evidence_item_count"], required_document_count)

    def test_export_bundle_preserves_application_case_and_entry_scoped_artifacts(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        upload_evidence_item(
            application=application,
            actor=self.appointing,
            label="Appointing Review Notes",
            uploaded_file=SimpleUploadedFile(
                "appointing-notes.pdf",
                self.build_valid_applicant_document_bytes(
                    "appointing-notes",
                    content_prefix="appointing review notes",
                ),
                content_type="application/pdf",
            ),
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type="workflow_evidence",
        )

        bundle_bytes = build_export_bundle(application, self.appointing)
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes))
        root = f"{application.reference_number}/"
        manifest = json.loads(archive.read(f"{root}records/case_manifest.json").decode("utf-8"))

        scopes = {item["artifact_scope"] for item in manifest["evidence"]}
        artifact_types = {item["artifact_type"] for item in manifest["evidence"]}

        self.assertEqual(scopes, {"application", "case", "entry"})
        self.assertIn("applicant_document", artifact_types)
        self.assertIn("workflow_evidence", artifact_types)
        self.assertIn("comparative_assessment_report", artifact_types)
        self.assertTrue(any(item["artifact_scope"] == "entry" for item in manifest["evidence"]))
        self.assertTrue(any(item["artifact_scope"] == "case" for item in manifest["evidence"]))
        self.assertTrue(any(item["artifact_scope"] == "application" for item in manifest["evidence"]))

    def test_secretariat_can_export_level1_case_when_they_can_view_it(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")

    def test_non_export_role_cannot_access_controlled_export(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 403)
        denied_log = AuditLog.objects.get(
            application=application,
            actor=self.hrmpsb,
            action=AuditLog.Action.EXPORT_DENIED,
        )
        self.assertTrue(denied_log.is_sensitive_access)
        self.assertEqual(denied_log.metadata["reason"], "unauthorized")

    def test_export_service_rejects_unauthorized_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot export this application.",
        ):
            build_export_bundle(application, self.hrmpsb)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                actor=self.hrmpsb,
                action=AuditLog.Action.EXPORT_DENIED,
            ).exists()
        )
