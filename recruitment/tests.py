import importlib
import io
import json
import re
import zipfile
from datetime import date, timedelta

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import ApplicantPortalIntakeForm
from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CompletionRequirement,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    NotificationLog,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
    WorkflowOverride,
)
from .requirements import (
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from .services import (
    build_export_bundle,
    build_submission_packet,
    generate_comparative_assessment_report,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    persist_position,
    process_workflow_action,
    record_final_decision,
    record_system_audit_event,
    save_deliberation_record,
    save_exam_record,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    update_recruitment_entry_status,
    upload_interview_fallback_rating,
    upload_evidence_item,
    user_can_view_application,
    verify_application_otp,
)


User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class BaseRecruitmentTestCase(TestCase):
    def setUp(self):
        self.applicant = User.objects.create_user(
            username="applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        self.secretariat = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.hrm_chief = User.objects.create_user(
            username="hrmchief",
            password="testpass123",
            role=RecruitmentUser.Role.HRM_CHIEF,
        )
        self.hrmpsb = User.objects.create_user(
            username="hrmpsb",
            password="testpass123",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        self.appointing = User.objects.create_user(
            username="appointing",
            password="testpass123",
            role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
        )
        self.sysadmin = User.objects.create_user(
            username="sysadmin",
            password="testpass123",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        self.admin_aide_position = PositionReference.objects.get(position_title="Administrative Aide VI")
        self.admin_aide_position.office_division_default = "HR Unit"
        self.admin_aide_position.notes = "Administrative support role."
        self.admin_aide_position.save(update_fields=["office_division_default", "notes", "updated_at"])

        self.medical_officer_position = PositionReference.objects.get(position_title="Medical Officer V")
        self.medical_officer_position.office_division_default = "Regional Office"
        self.medical_officer_position.notes = "Clinical leadership role."
        self.medical_officer_position.save(update_fields=["office_division_default", "notes", "updated_at"])

        self.project_assistant_position = PositionReference.objects.get(position_title="Administrative Assistant I")
        self.project_assistant_position.office_division_default = "Special Projects"
        self.project_assistant_position.notes = "COS support role."
        self.project_assistant_position.save(update_fields=["office_division_default", "notes", "updated_at"])
        self.level1_position = PositionPosting.objects.create(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        self.level2_position = PositionPosting.objects.create(
            position_reference=self.medical_officer_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_2,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        self.cos_position = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
        )

    def level1_closing_date(self):
        return timezone.localdate() + timedelta(days=15)

    def entry_opening_date(self):
        return timezone.localdate()

    def entry_opening_date_string(self):
        return self.entry_opening_date().isoformat()

    def build_valid_applicant_document_upload(
        self,
        requirement_code,
        *,
        content_prefix="sample",
        filename=None,
    ):
        filename = filename or f"{requirement_code}.pdf"
        file_bytes = self.build_valid_applicant_document_bytes(
            requirement_code,
            content_prefix=content_prefix,
        )
        return SimpleUploadedFile(
            filename,
            file_bytes,
            content_type="application/pdf",
        )

    def build_valid_applicant_document_bytes(
        self,
        requirement_code,
        *,
        content_prefix="sample",
    ):
        return (
            b"%PDF-1.4\n"
            + f"{content_prefix}:{requirement_code}\n".encode("utf-8")
            + b"%%EOF\n"
        )

    def upload_required_applicant_documents(
        self,
        application,
        actor,
        *,
        content_prefix="sample",
        performance_rating_applicability=RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE,
    ):
        uploaded_evidence = []
        for requirement in get_required_applicant_document_requirements(
            branch=application.branch,
            performance_rating_not_applicable=(
                performance_rating_applicability
                == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            )
        ):
            uploaded_evidence.append(
                upload_evidence_item(
                    application=application,
                    actor=actor,
                    label=requirement.title,
                    uploaded_file=self.build_valid_applicant_document_upload(
                        requirement.code,
                        content_prefix=content_prefix,
                    ),
                    document_key=requirement.code,
                    artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                    artifact_type="applicant_document",
                )
            )
        return uploaded_evidence

    def post_portal_intake(self, client, position, payload, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(
                reverse("applicant-intake", kwargs={"pk": position.pk}),
                payload,
                **kwargs,
            )

    def make_application(self, position):
        performance_rating_applicability = (
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            if position.branch == PositionPosting.Branch.COS
            else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=position,
            applicant_first_name="Test",
            applicant_last_name="Applicant",
            applicant_email="applicant@example.com",
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
            performance_rating_applicability=performance_rating_applicability,
        )
        self.upload_required_applicant_documents(
            application,
            self.applicant,
            performance_rating_applicability=performance_rating_applicability,
        )
        return application

    def verify_application_for_submission(self, application):
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
        application.refresh_from_db()
        return otp_code

    def finalize_screening_for_current_stage(
        self,
        application,
        actor,
        completeness_status=ScreeningRecord.CompletenessStatus.COMPLETE,
        qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
        completeness_notes="All required screening documents were reviewed.",
        screening_notes="Qualification screening completed.",
    ):
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
                "completeness_status": completeness_status,
                "completeness_notes": completeness_notes,
                "qualification_outcome": qualification_outcome,
                "screening_notes": screening_notes,
            },
            finalize=True,
        )

    def finalize_exam_for_current_stage(
        self,
        application,
        actor,
        exam_type="Technical Examination",
        exam_status=ExamRecord.ExamStatus.COMPLETED,
        exam_score="88.50",
        exam_result="Passed",
        technical_score="87.00",
        technical_result="Technical passed",
        practical_score="90.00",
        practical_result="Practical passed",
        exam_date=None,
        administered_by="HRMS Exam Administrator",
        valid_from=None,
        valid_until=None,
        exam_notes="Formal examination output recorded.",
    ):
        exam_date = exam_date or timezone.localdate()
        return save_exam_record(
            application=application,
            actor=actor,
            cleaned_data={
                "exam_type": exam_type,
                "exam_status": exam_status,
                "exam_score": exam_score,
                "exam_result": exam_result,
                "technical_score": technical_score,
                "technical_result": technical_result,
                "practical_score": practical_score,
                "practical_result": practical_result,
                "exam_date": exam_date,
                "administered_by": administered_by,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "exam_notes": exam_notes,
            },
            finalize=True,
        )

    def finalize_interview_for_current_stage(
        self,
        application,
        actor,
        scheduled_for=None,
        location="Conference Room A",
        session_notes="Structured interview output preserved.",
        rating_score="89.50",
        rating_notes="Interview responses addressed the competency requirements.",
        justification="",
    ):
        scheduled_for = scheduled_for or (timezone.now() + timedelta(days=1))
        save_interview_session(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": location,
                "session_notes": session_notes,
            },
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=actor,
            cleaned_data={
                "rating_score": rating_score,
                "rating_notes": rating_notes,
                "justification": justification,
            },
        )
        return save_interview_session(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": location,
                "session_notes": session_notes,
            },
            finalize=True,
        )

    def move_application_to_hrm_chief_review(self, application):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        if application.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(application, self.secretariat)
            self.finalize_exam_for_current_stage(application, self.secretariat)
            process_workflow_action(application, self.secretariat, "endorse", "Forward to HRM Chief.")
            application.refresh_from_db()
        return application

    def move_application_to_hrmpsb_review(self, application):
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "Forward to HRMPSB.")
        application.refresh_from_db()
        return application

    def move_application_to_appointing_review(self, application):
        if application.branch == PositionPosting.Branch.PLANTILLA:
            self.move_application_to_hrmpsb_review(application)
            self.finalize_interview_for_current_stage(application, self.hrmpsb)
            self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
            self.finalize_car_for_current_stage(application, self.hrmpsb)
            process_workflow_action(application, self.hrmpsb, "recommend", "Forward to Appointing Authority.")
        else:
            self.move_application_to_hrm_chief_review(application)
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
            self.finalize_exam_for_current_stage(application, self.hrm_chief)
            self.finalize_interview_for_current_stage(application, self.hrm_chief)
            self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    def finalize_deliberation_for_current_stage(
        self,
        application,
        actor,
        ranking_position=None,
        deliberated_at=None,
        deliberation_minutes="Recorded structured deliberation minutes.",
        decision_support_summary="Decision-support summary preserved for routing.",
        ranking_notes="Ranking basis recorded for decision support.",
    ):
        return save_deliberation_record(
            application=application,
            actor=actor,
            cleaned_data={
                "deliberated_at": deliberated_at or timezone.now(),
                "deliberation_minutes": deliberation_minutes,
                "decision_support_summary": decision_support_summary,
                "ranking_position": ranking_position,
                "ranking_notes": ranking_notes,
            },
            finalize=True,
        )

    def finalize_car_for_current_stage(
        self,
        application,
        actor,
        summary_notes="Comparative ranking sheet generated for Plantilla decision support.",
    ):
        return generate_comparative_assessment_report(
            application=application,
            actor=actor,
            cleaned_data={"summary_notes": summary_notes},
            finalize=True,
        )

    def record_final_decision_for_current_stage(
        self,
        application,
        actor,
        decision_outcome=FinalDecision.Outcome.SELECTED,
        decision_notes="Final decision recorded after packet review.",
    ):
        return record_final_decision(
            application=application,
            actor=actor,
            cleaned_data={
                "decision_outcome": decision_outcome,
                "decision_notes": decision_notes,
            },
        )

    def extract_otp_from_last_email(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def make_selected_application(self, position):
        application = self.make_application(position)
        self.move_application_to_appointing_review(application)
        decision_actor = (
            self.hrm_chief
            if application.branch == PositionPosting.Branch.COS
            else self.appointing
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                decision_actor,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved for completion tracking.",
            )
        application.refresh_from_db()
        return application


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FoundationSmokeTests(TestCase):
    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal Access")
        self.assertContains(response, "RecruitGuard-CHD")
        self.assertTrue(reverse("login").startswith("/internal/"))

    def test_dashboard_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_root_redirects_to_public_applicant_portal(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("applicant-portal"))

    def test_public_status_lookup_page_loads_without_login(self):
        response = self.client.get(reverse("applicant-status-lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(reverse("applicant-status-lookup").startswith("/apply/"))

    def test_workflow_queue_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("workflow-queue"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_internal_user_can_log_in(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("dashboard"))
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )

    def test_applicant_cannot_use_internal_login(self):
        User.objects.create_user(
            username="applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "applicant", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "restricted to internal users")

    def test_inactive_internal_user_cannot_log_in(self):
        User.objects.create_user(
            username="inactive-chief",
            password="testpass123",
            role=RecruitmentUser.Role.HRM_CHIEF,
            is_active=False,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "inactive-chief", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")


class IdentityAdministrationTests(BaseRecruitmentTestCase):
    def test_system_admin_can_create_internal_account(self):
        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("internal-user-create"),
            {
                "username": "chief-two",
                "first_name": "Chief",
                "last_name": "Two",
                "email": "chief.two@example.com",
                "employee_id": "EMP-002",
                "office_name": "HR Office",
                "role": RecruitmentUser.Role.HRM_CHIEF,
                "is_active": "on",
                "password1": "VeryStrongPass123",
                "password2": "VeryStrongPass123",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(username="chief-two")
        self.assertEqual(created_user.role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                metadata__target_username="chief-two",
            ).exists()
        )

    def test_non_admin_cannot_access_internal_user_directory(self):
        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("internal-user-list"))
        self.assertEqual(response.status_code, 403)

    def test_system_admin_can_update_role_and_activation_with_audit(self):
        managed_user = User.objects.create_user(
            username="member-one",
            password="testpass123",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
            email="member.one@example.com",
        )
        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("internal-user-update", kwargs={"pk": managed_user.pk}),
            {
                "username": "member-one",
                "first_name": "Member",
                "last_name": "One",
                "email": "member.one@example.com",
                "employee_id": "EMP-019",
                "office_name": "Board Office",
                "role": RecruitmentUser.Role.APPOINTING_AUTHORITY,
                "is_active": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        managed_user.refresh_from_db()
        self.assertEqual(managed_user.role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertFalse(managed_user.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ROLE_CHANGED,
                metadata__target_username="member-one",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                metadata__target_username="member-one",
            ).exists()
        )

    def test_system_admin_cannot_view_case_content_by_default(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_system_admin_role_does_not_inherit_django_admin_access(self):
        self.sysadmin.refresh_from_db()
        self.assertFalse(self.sysadmin.is_staff)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)


class RecruitmentEntryManagementTests(BaseRecruitmentTestCase):
    def mark_application_as_submitted_without_case(self, application):
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.submitted_at = timezone.now()
        application.save(
            update_fields=[
                "status",
                "current_handler_role",
                "submitted_at",
                "branch",
                "level",
                "updated_at",
            ]
        )
        return application

    def test_starter_position_reference_catalog_is_seeded(self):
        self.assertTrue(
            PositionReference.objects.filter(
                position_title="Accountant II",
                reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
            ).exists()
        )

    def test_position_reference_slug_generation_is_collision_safe(self):
        first = PositionReference.objects.create(
            position_title="Budget Officer",
            salary_grade=18,
            level_classification=PositionReference.LevelClassification.SECOND_LEVEL,
            class_id="BO1",
            os_code="02-FS",
            occupational_service="Financial Service",
            occupational_group="Budgeting",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
        )
        second = PositionReference.objects.create(
            position_title="Budget Officer",
            salary_grade=19,
            level_classification=PositionReference.LevelClassification.SECOND_LEVEL,
            class_id="BO2",
            os_code="02-FS",
            occupational_service="Financial Service",
            occupational_group="Budgeting",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
        )

        self.assertEqual(first.position_slug, "budget-officer")
        self.assertEqual(second.position_slug, "budget-officer-2")

    def test_persist_position_rejects_verified_reference_with_missing_official_metadata(self):
        with self.assertRaises(ValidationError):
            persist_position(
                PositionReference(
                    position_title="Half-Encoded Record",
                    reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
                    is_active=True,
                ),
                actor=self.sysadmin,
                changed_fields=["position_title", "reference_status"],
            )

    def test_system_admin_can_create_position_reference_catalog_record(self):
        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("position-catalog-create"),
            {
                "position_title": "Nurse II",
                "position_slug": "",
                "salary_grade": "15",
                "level_classification": PositionReference.LevelClassification.SECOND_LEVEL,
                "class_id": "NURS2",
                "os_code": "09-MH",
                "occupational_service": "Medicine and Health Service",
                "occupational_group": "Nursing",
                "reference_status": PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
                "notes": "Synthetic test record.",
                "position_code": "POS-010",
                "agency_item_number": "",
                "office_division_default": "Clinical Services",
                "qs_education": "Bachelor of Science in Nursing",
                "qs_training": "",
                "qs_experience": "",
                "qs_eligibility": "RA 1080",
                "employment_track_applicability": "plantilla",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(PositionReference.objects.filter(position_title="Nurse II").exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.POSITION_CREATED,
                metadata__position_title="Nurse II",
            ).exists()
        )

    def test_entry_manager_cannot_create_position_reference_catalog_record(self):
        client = Client()
        client.force_login(self.hrm_chief)

        response = client.get(reverse("position-catalog-create"))

        self.assertEqual(response.status_code, 403)

    def test_plantilla_entry_requires_fixed_period_and_closing_date(self):
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_cos_pooling_entry_cannot_set_closing_date(self):
        entry = PositionPosting(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_entry_code_is_generated_on_first_save_and_matches_pattern(self):
        entry = PositionPosting.objects.create(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            closing_date=self.level1_closing_date(),
        )

        expected_code = f"RG-PLT-{timezone.localdate().year:04d}-0003"
        self.assertEqual(entry.job_code, expected_code)
        self.assertRegex(entry.job_code, r"^RG-(PLT|COS)-\d{4}-\d{4}$")

    def test_entry_code_generation_uses_year_priority_and_next_sequence_within_branch(self):
        current_year = timezone.localdate().year
        publication_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            publication_date=date(current_year + 1, 2, 10),
            opening_date=date(current_year, 5, 1),
        )
        opening_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            opening_date=date(current_year + 2, 3, 15),
        )
        fallback_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        self.assertEqual(publication_entry.job_code, f"RG-COS-{current_year + 1:04d}-0001")
        self.assertEqual(opening_entry.job_code, f"RG-COS-{current_year + 2:04d}-0001")
        self.assertEqual(fallback_entry.job_code, f"RG-COS-{current_year:04d}-0002")

    def test_entry_code_validation_rejects_invalid_pattern_and_missing_saved_code(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        entry.job_code = "INVALID"
        with self.assertRaises(ValidationError) as invalid_pattern_error:
            entry.full_clean()
        self.assertIn(
            "Entry Code must match the format",
            invalid_pattern_error.exception.message_dict["job_code"][0],
        )

        entry.job_code = ""
        with self.assertRaises(ValidationError) as missing_code_error:
            entry.full_clean()
        self.assertIn(
            "Entry Code is required after the recruitment entry is first saved.",
            missing_code_error.exception.message_dict["job_code"],
        )

    def test_entry_code_validation_rejects_branch_and_year_mismatch(self):
        current_year = timezone.localdate().year
        branch_mismatch_entry = PositionPosting(
            position_reference=self.project_assistant_position,
            job_code=f"RG-PLT-{current_year:04d}-0001",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        with self.assertRaises(ValidationError) as branch_error:
            branch_mismatch_entry.full_clean()
        self.assertIn(
            "Entry Code branch segment must match the selected engagement type.",
            branch_error.exception.message_dict["job_code"],
        )

        year_mismatch_entry = PositionPosting(
            position_reference=self.project_assistant_position,
            job_code=f"RG-COS-{current_year - 1:04d}-0001",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        with self.assertRaises(ValidationError) as year_error:
            year_mismatch_entry.full_clean()
        self.assertIn(
            "Entry Code year segment must match the publication date, opening date, or current year fallback.",
            year_error.exception.message_dict["job_code"],
        )

    def test_entry_code_field_is_read_only_in_create_and_edit_forms(self):
        client = Client()
        client.force_login(self.secretariat)

        create_response = client.get(reverse("recruitment-entry-create"))
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, 'id="id_job_code"')
        self.assertContains(create_response, 'placeholder="Will be generated automatically after first save"')
        self.assertContains(
            create_response,
            "This code is generated automatically for tracking and cannot be edited manually.",
        )

        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        update_response = client.get(reverse("recruitment-entry-update", kwargs={"pk": entry.pk}))

        self.assertEqual(update_response.status_code, 200)
        self.assertContains(update_response, entry.job_code)

    def test_entry_manager_can_create_recruitment_entry(self):
        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Continuous talent pool for technical support.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.exclude(pk=self.cos_position.pk).get(
            position_reference=self.project_assistant_position,
            qualification_reference="Continuous talent pool for technical support.",
        )
        self.assertEqual(created_entry.created_by, self.secretariat)
        self.assertEqual(created_entry.updated_by, self.secretariat)
        self.assertEqual(created_entry.position_reference, self.project_assistant_position)
        self.assertEqual(created_entry.title, self.project_assistant_position.position_title)
        self.assertEqual(created_entry.level, PositionPosting.Level.LEVEL_1)
        self.assertEqual(created_entry.job_code, f"RG-COS-{self.entry_opening_date().year:04d}-0002")
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_CREATED,
                metadata__entry_code=created_entry.job_code,
            ).exists()
        )

    def test_generated_entry_code_is_preserved_on_edit(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            qualification_reference="Initial notes.",
        )
        original_job_code = entry.job_code

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Updated notes.",
            },
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.job_code, original_job_code)
        self.assertEqual(entry.qualification_reference, "Updated notes.")

    def test_entry_update_rejects_year_drift_when_generated_code_would_change(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.DRAFT,
                "publication_date": "",
                "opening_date": date(self.entry_opening_date().year + 1, 1, 15).isoformat(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Entry Code year segment must match the publication date, opening date, or current year fallback.",
        )

    def test_live_entry_update_blocks_position_reference_change_after_first_submitted_application(self):
        application = self.make_application(self.level1_position)
        self.mark_application_as_submitted_without_case(application)

        self.assertFalse(RecruitmentCase.objects.filter(application=application).exists())

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            {
                "position_reference": self.medical_officer_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.level1_position.opening_date.isoformat(),
                "closing_date": self.level1_position.closing_date.isoformat(),
                "qualification_reference": "Attempted live metadata drift.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Position reference cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.position_reference, self.admin_aide_position)
        self.assertEqual(self.level1_position.title, self.admin_aide_position.position_title)
        self.assertEqual(self.level1_position.level, PositionPosting.Level.LEVEL_1)

    def test_live_entry_save_blocks_opening_date_change_after_linked_case_exists(self):
        application = self.make_application(self.cos_position)
        RecruitmentCase.objects.create(
            application=application,
            current_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            current_handler_role=RecruitmentUser.Role.SECRETARIAT,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
        )
        original_opening_date = self.cos_position.opening_date

        self.assertIsNone(application.submitted_at)

        self.cos_position.opening_date = original_opening_date + timedelta(days=1)
        with self.assertRaises(ValidationError) as exc:
            self.cos_position.save()

        self.assertIn(
            "Opening date cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
            exc.exception.message_dict["opening_date"],
        )
        self.cos_position.refresh_from_db()
        self.assertEqual(self.cos_position.opening_date, original_opening_date)

    def test_live_entry_allows_non_critical_qualification_reference_update(self):
        application = self.make_application(self.cos_position)
        self.mark_application_as_submitted_without_case(application)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.POOLING,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.cos_position.opening_date.isoformat(),
                "closing_date": "",
                "qualification_reference": "Live entry note update is still allowed.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.cos_position.refresh_from_db()
        self.assertEqual(
            self.cos_position.qualification_reference,
            "Live entry note update is still allowed.",
        )

    def test_non_live_entry_save_resyncs_office_metadata_when_position_reference_changes(self):
        self.level1_position.position_reference = self.medical_officer_position
        self.level1_position.save()

        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.title, self.medical_officer_position.position_title)
        self.assertEqual(self.level1_position.level, PositionPosting.Level.LEVEL_2)
        self.assertEqual(self.level1_position.unit, "Regional Office")

    def test_recruitment_entry_creation_requires_position_reference(self):
        client = Client()
        client.force_login(self.secretariat)
        initial_count = PositionPosting.objects.count()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": "",
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Missing reference should fail.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a position reference before creating the recruitment entry.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_recruitment_entry_create_rejects_manual_entry_code_tampering(self):
        client = Client()
        client.force_login(self.secretariat)
        initial_count = PositionPosting.objects.count()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "job_code": "RG-COS-2099-9999",
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Tamper attempt should fail.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Entry Code is generated automatically and cannot be edited manually.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_inactive_position_reference_cannot_be_used_for_entry_creation(self):
        inactive_reference = PositionReference.objects.create(
            position_title="Inactive Reference",
            salary_grade=11,
            level_classification=PositionReference.LevelClassification.FIRST_LEVEL,
            class_id="INACT1",
            os_code="01-GA",
            occupational_service="General Administrative Service",
            occupational_group="Administrative",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
            is_active=False,
        )
        client = Client()
        client.force_login(self.secretariat)
        initial_count = PositionPosting.objects.count()

        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": inactive_reference.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice. That choice is not one of the available choices.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_incomplete_position_reference_warns_and_does_not_invent_routing_metadata(self):
        incomplete_reference = PositionReference.objects.create(
            position_title="Incomplete Reference",
            reference_status=PositionReference.ReferenceStatus.INCOMPLETE_REFERENCE,
            is_active=True,
            position_code="POS-999",
            notes="Synthetic incomplete record for test coverage.",
        )
        client = Client()
        client.force_login(self.secretariat)
        initial_count = PositionPosting.objects.count()

        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": incomplete_reference.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.DRAFT,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reference metadata is incomplete")
        self.assertContains(
            response,
            "This position reference does not contain the level classification required for routing.",
        )
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_non_entry_manager_cannot_access_entry_management(self):
        client = Client()
        client.force_login(self.hrmpsb)
        response = client.get(reverse("recruitment-entry-list"))
        self.assertEqual(response.status_code, 403)

    def test_entry_status_change_is_audited(self):
        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.SUSPENDED},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)
        self.assertFalse(self.level1_position.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
            ).exists()
        )

    def test_status_service_blocks_transition_when_entry_fails_full_validation(self):
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=self.level1_position.opening_date - timedelta(days=1),
            updated_at=timezone.now(),
        )
        self.level1_position.refresh_from_db()

        with self.assertRaises(ValidationError) as exc:
            update_recruitment_entry_status(
                self.level1_position,
                self.hrm_chief,
                PositionPosting.EntryStatus.SUSPENDED,
            )

        self.assertIn(
            "Closing date cannot be earlier than opening date.",
            exc.exception.message_dict["closing_date"],
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
                metadata__new_status=PositionPosting.EntryStatus.SUSPENDED,
            ).exists()
        )

    def test_status_view_blocks_transition_when_linked_position_reference_is_inactive(self):
        self.admin_aide_position.is_active = False
        self.admin_aide_position.save(update_fields=["is_active", "updated_at"])

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.SUSPENDED},
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Inactive position references cannot be used for recruitment entries.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertTrue(self.level1_position.is_active)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
                metadata__new_status=PositionPosting.EntryStatus.SUSPENDED,
            ).exists()
        )


class ApplicantPortalFlowTests(BaseRecruitmentTestCase):
    def portal_payload(self, *, position=None, **overrides):
        position = position or self.level1_position
        performance_rating_applicability = overrides.pop(
            "performance_rating_applicability",
            (
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
                if position.branch == PositionPosting.Branch.COS
                else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
        )
        payload = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "email": "portal.applicant@example.com",
            "phone": "09171234567",
            "qualification_summary": "Qualified applicant with complete supporting credentials.",
            "cover_letter": "Please consider this application.",
            "performance_rating_applicability": performance_rating_applicability,
            "checklist_privacy_consent": "on",
            "checklist_documents_complete": "on",
            "checklist_information_certified": "on",
        }
        for requirement in get_required_applicant_document_requirements(
            branch=position.branch,
            performance_rating_not_applicable=(
                performance_rating_applicability
                == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            )
        ):
            payload[requirement.code] = self.build_valid_applicant_document_upload(
                requirement.code,
                content_prefix="portal",
            )
        payload.update(overrides)
        return payload

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

    def test_internal_position_list_uses_reference_metadata_over_legacy_entry_text(self):
        self.level1_position.description = "Legacy internal description should stay hidden."
        self.level1_position.requirements = "Legacy internal requirements should stay hidden."
        self.level1_position.qualification_reference = "Internal entry note."
        self.level1_position.save(update_fields=["description", "requirements", "qualification_reference", "updated_at"])
        self.admin_aide_position.qs_training = "Eight hours of records management training"
        self.admin_aide_position.save(update_fields=["qs_training", "updated_at"])

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("position-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal entry note.")
        self.assertContains(response, "Training: Eight hours of records management training")
        self.assertNotContains(response, "Legacy internal description should stay hidden.")
        self.assertNotContains(response, "Legacy internal requirements should stay hidden.")

    def test_plantilla_public_submission_requires_valid_otp_before_finalization(self):
        client = Client()
        response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="plantilla.portal@example.com"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="plantilla.portal@example.com",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(application.otp_hash)
        self.assertIsNone(application.submitted_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "Valid OTP verification is required before final submission.")
        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        self.assertIsNotNone(application.otp_verified_at)

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        application.refresh_from_db()
        self.assertContains(response, application.reference_number)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
            ).exists()
        )

    def test_cos_public_submission_completes_and_status_lookup_works(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.cos_position,
            self.portal_payload(position=self.cos_position, email="cos.portal@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email="cos.portal@example.com",
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        receipt_response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertContains(receipt_response, application.reference_number)
        self.assertEqual(application.branch, PositionPosting.Branch.COS)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

        status_response = client.post(
            reverse("applicant-status-lookup"),
            {
                "application_id": application.reference_number,
                "email": "cos.portal@example.com",
            },
        )
        self.assertContains(status_response, "Under Review")
        self.assertContains(status_response, application.reference_number)

    def test_invalid_otp_is_rejected(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="invalid.otp@example.com"),
            follow=True,
        )
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
        self.assertContains(response, "The OTP is invalid.")
        self.assertIsNone(application.otp_verified_at)

    def test_portal_intake_requires_requirement_coded_documents(self):
        client = Client()
        missing_requirement = get_required_applicant_document_requirements(
            branch=self.level1_position.branch
        )[0]
        payload = self.portal_payload(email="missing.requirement@example.com")
        payload.pop(missing_requirement.code)

        response = self.post_portal_intake(
            client,
            self.level1_position,
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"Upload the required document for {missing_requirement.title}.",
        )
        draft = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="missing.requirement@example.com",
        )
        saved_codes = set(
            draft.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                is_current_version=True,
                is_archived=False,
            ).values_list("document_key", flat=True)
        )
        self.assertNotIn(missing_requirement.code, saved_codes)
        self.assertTrue(saved_codes)
        self.assertContains(response, "Your valid files were saved to this draft.")

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

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        slot_labels = {
            slot["requirement"].code: slot["label_tag"]
            for slot in intake_response.context["form"].document_slots
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
        slot_labels = {
            slot["requirement"].code: slot["label_tag"]
            for slot in intake_response.context["form"].document_slots
        }
        self.assertNotIn("performance_rating", slot_labels)
        self.assertEqual(slot_labels["training_certificates"], "If applicable")

    def test_cos_submission_does_not_require_performance_rating_or_training_certificates(self):
        client = Client()
        response = self.post_portal_intake(
            client,
            self.cos_position,
            self.portal_payload(position=self.cos_position, email="cos.no.rating@example.com"),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email="cos.no.rating@example.com",
        )
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
        self.assertFalse(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="training_certificates",
                is_current_version=True,
                is_archived=False,
            ).exists()
        )

    def test_performance_rating_requires_upload_when_marked_applicable(self):
        client = Client()
        payload = self.portal_payload(email="rating.required@example.com")
        payload.pop("performance_rating")

        response = self.post_portal_intake(
            client,
            self.level1_position,
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Upload the required document for Performance Rating in the last rating period.",
        )

    def test_performance_rating_not_applicable_allows_submission_without_file(self):
        client = Client()
        response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(
                email="no.rating@example.com",
                performance_rating_applicability=(
                    RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
                ),
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="no.rating@example.com",
        )
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

        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        receipt_response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )

        application.refresh_from_db()
        self.assertIsNotNone(application.submitted_at)
        self.assertContains(receipt_response, application.reference_number)

    def test_invalid_submission_preserves_non_file_inputs_and_saved_valid_uploads(self):
        client = Client()
        payload = self.portal_payload(
            email="preserved.inputs@example.com",
            qualification_summary="Persistent qualifications for validation retry.",
        )
        missing_requirement = get_required_applicant_document_requirements(
            branch=self.level1_position.branch
        )[-1]
        payload.pop(missing_requirement.code)

        response = self.post_portal_intake(
            client,
            self.level1_position,
            payload,
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["email"].value(), "preserved.inputs@example.com")
        self.assertEqual(
            form["qualification_summary"].value(),
            "Persistent qualifications for validation retry.",
        )
        self.assertEqual(
            form["performance_rating_applicability"].value(),
            RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE,
        )
        self.assertTrue(form.saved_draft_notice)
        self.assertIn("signed_cover_letter", form.existing_documents_by_code)
        self.assertNotIn(missing_requirement.code, form.existing_documents_by_code)
        self.assertContains(response, "Already saved:")

    def test_empty_portal_submission_returns_validation_errors_without_crashing(self):
        client = Client()

        response = self.post_portal_intake(
            client,
            self.level1_position,
            {},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please review the highlighted fields below.")
        self.assertContains(response, "This field is required.")
        self.assertFalse(
            RecruitmentApplication.objects.filter(position=self.level1_position).exists()
        )

    def test_blank_browser_file_input_is_ignored_by_duplicate_warning_builder(self):
        class BrowserEmptyUpload:
            name = ""
            size = 1
            content_type = "application/octet-stream"

            def __bool__(self):
                return True

            def read(self):
                return b"browser-empty-file-input"

            def seek(self, position):
                return None

        form = ApplicantPortalIntakeForm(data={}, entry=self.level1_position)
        form.cleaned_data = {
            requirement.file_field_name: None
            for requirement in form.document_requirements
        }
        form.cleaned_data["signed_cover_letter"] = BrowserEmptyUpload()

        self.assertIsNone(form._normalize_requirement_upload(BrowserEmptyUpload()))
        self.assertEqual(form._build_duplicate_document_warnings(), [])

    def test_duplicate_applicant_document_file_warning_is_shown(self):
        client = Client()
        duplicate_bytes = b"%PDF-1.4\nshared-applicant-document\n%%EOF\n"
        payload = self.portal_payload(email="duplicate.file@example.com")
        payload["signed_cover_letter"] = SimpleUploadedFile(
            "shared-document.pdf",
            duplicate_bytes,
            content_type="application/pdf",
        )
        payload["personal_data_sheet"] = SimpleUploadedFile(
            "shared-document-again.pdf",
            duplicate_bytes,
            content_type="application/pdf",
        )

        response = self.post_portal_intake(
            client,
            self.level1_position,
            payload,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "The same file appears to be attached to multiple document slots",
        )

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
            "Upload the required requirement-coded applicant documents before final submission.",
        ) as exc:
            submit_application(application, self.applicant)

        self.assertIn("Signed Cover Letter", str(exc.exception))

    def test_portal_reuses_existing_draft_and_requirement_documents_for_same_entry_email(self):
        client = Client()
        initial_response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="reused.draft@example.com"),
        )
        self.assertEqual(initial_response.status_code, 302)
        original_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="reused.draft@example.com",
        )
        applicant_id = original_application.applicant_id

        payload = self.portal_payload(
            email="reused.draft@example.com",
            qualification_summary="Updated qualifications for the same draft.",
        )
        for requirement in get_required_applicant_document_requirements(
            branch=self.level1_position.branch
        ):
            payload.pop(requirement.code)

        followup_response = self.post_portal_intake(
            client,
            self.level1_position,
            payload,
        )

        self.assertEqual(followup_response.status_code, 302)
        refreshed_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="reused.draft@example.com",
        )
        self.assertEqual(RecruitmentApplication.objects.filter(position=self.level1_position).count(), 1)
        self.assertEqual(refreshed_application.pk, original_application.pk)
        self.assertEqual(refreshed_application.applicant_id, applicant_id)
        self.assertEqual(
            refreshed_application.qualification_summary,
            "Updated qualifications for the same draft.",
        )
        self.assertEqual(
            RecruitmentUser.objects.filter(
                role=RecruitmentUser.Role.APPLICANT,
                email__iexact="reused.draft@example.com",
            ).count(),
            1,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=refreshed_application,
                action=AuditLog.Action.APPLICATION_UPDATED,
            ).exists()
        )

    def test_portal_reuses_existing_applicant_identity_by_email_across_entries(self):
        client = Client()
        shared_email = "shared.identity@example.com"
        first_response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email=shared_email),
        )
        self.assertEqual(first_response.status_code, 302)
        first_application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email=shared_email,
        )

        second_response = self.post_portal_intake(
            client,
            self.cos_position,
            self.portal_payload(position=self.cos_position, email=shared_email),
        )
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
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="expired.otp@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="expired.otp@example.com",
        )
        otp_code = self.extract_otp_from_last_email()
        application.otp_expires_at = timezone.now() - timedelta(minutes=1)
        application.save(update_fields=["otp_expires_at", "updated_at"])

        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": otp_code},
            follow=True,
        )
        self.assertContains(response, "The OTP has expired.")

        application.refresh_from_db()
        application.otp_verified_at = timezone.now() - timedelta(minutes=2)
        application.save(update_fields=["otp_verified_at", "updated_at"])
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "Your OTP verification has expired.")
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


class WorkflowRoutingTests(BaseRecruitmentTestCase):
    def test_level1_submission_routes_to_secretariat(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(routing_event.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_1)

    def test_level2_submission_routes_to_hrm_chief(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_2)

    def test_secretariat_cannot_process_level2_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "Secretariat cannot process Level 2 applications without an active override.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "endorse",
                "Attempted without override.",
            )

    def test_endorsement_requires_finalized_screening_for_screening_stages(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the screening record before endorsing this application.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "endorse",
                "Attempted before screening finalization.",
            )

    def test_secretariat_cannot_view_or_queue_level2_without_override_even_if_misassigned(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        self.assertFalse(user_can_view_application(self.secretariat, application))
        self.assertFalse(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_allows_secretariat_processing_of_level2(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.sysadmin)
        response = client.post(
            reverse("workflow-override", kwargs={"pk": application.pk}),
            {"reason": "Controlled screening support."},
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            screening_notes="Override-backed screening completed.",
        )
        self.finalize_exam_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "Pre-screen completed.")
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(WorkflowOverride.objects.filter(application=application, is_active=True).exists())
        routing_events = list(application.routing_history.values_list("route_type", "to_handler_role"))
        self.assertEqual(
            routing_events,
            [
                (RoutingHistory.RouteType.INITIAL, RecruitmentUser.Role.HRM_CHIEF),
                (RoutingHistory.RouteType.OVERRIDE, RecruitmentUser.Role.SECRETARIAT),
                (RoutingHistory.RouteType.FORWARD, RecruitmentUser.Role.HRM_CHIEF),
            ],
        )

    def test_secretariat_cannot_view_finalized_level2_case_without_authorized_basis(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.NOT_SELECTED,
                decision_notes="Finalize Level 2 case without Secretariat visibility.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertFalse(user_can_view_application(self.secretariat, application))

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_is_limited_to_active_hrm_chief_review_stage(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Secretariat overrides are only available while a Level 2 application is actively assigned to the HRM Chief review stage.",
        ):
            grant_secretariat_override(
                application=application,
                actor=self.sysadmin,
                reason="Improper reroute attempt.",
            )

        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertFalse(WorkflowOverride.objects.filter(application=application).exists())

    def test_cos_skips_hrmpsb_and_appointing_authority_stage(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "COS screening done.")
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)

        with self.assertRaisesMessage(ValueError, "Complete the current workflow task before proceeding."):
            process_workflow_action(application, self.hrm_chief, "endorse", "COS endorsed.")

        with self.captureOnCommitCallbacks(execute=True):
            decision = self.record_final_decision_for_current_stage(
                application,
                self.hrm_chief,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="COS selected after HRM Chief deliberation.",
            )
        application.refresh_from_db()
        self.assertEqual(decision.decided_by, self.hrm_chief)
        self.assertEqual(decision.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)


class RecruitmentCaseWorkflowTests(BaseRecruitmentTestCase):
    def test_submission_creates_one_recruitment_case(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertTrue(hasattr(application, "case"))
        self.assertEqual(RecruitmentCase.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CREATED,
            ).exists()
        )

    def test_stage_progression_updates_case_stage_in_defined_order(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "Forward to HRM Chief.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)

        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        process_workflow_action(application, self.hrm_chief, "endorse", "Forward to HRMPSB.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.finalize_car_for_current_stage(application, self.hrmpsb)
        process_workflow_action(application, self.hrmpsb, "recommend", "Forward to appointing authority.")
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved for completion.",
            )
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)

    def test_closed_case_is_locked_after_completion_and_can_be_reopened(self):
        application = self.make_selected_application(self.cos_position)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            {
                "completion_reference": "COS-CONTRACT-001",
                "completion_date": timezone.localdate().isoformat(),
                "deadline": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "remarks": "Contract requirements fully tracked.",
                "completion_requirements-TOTAL_FORMS": "3",
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
                "completion_requirements-0-item_label": "Signed contract",
                "completion_requirements-0-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-0-notes": "Submitted.",
                "completion_requirements-1-item_label": "Government-issued ID",
                "completion_requirements-1-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-1-notes": "Verified.",
                "completion_requirements-2-item_label": "",
                "completion_requirements-2-status": CompletionRequirement.RequirementStatus.PENDING,
                "completion_requirements-2-notes": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished."},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)

        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("workflow-reopen", kwargs={"pk": application.pk}),
            {"reason": "Correcting the completion tracking record."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertFalse(application.case.is_stage_locked)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_REOPENED,
            ).exists()
        )

    def test_case_timeline_is_visible_on_application_detail(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertContains(response, "RG-")
        self.assertContains(response, "Qualification Screening")
        self.assertContains(response, 'rg-cws-layout rg-cws-layout--full')
        self.assertNotContains(response, "Workflow Snapshot")
        self.assertNotContains(response, "Stage Integrity")
        self.assertNotContains(response, "Recent Activity")
        self.assertNotContains(response, "Current task only")
        self.assertNotContains(response, 'data-section="cws-exam"')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, "{#")
        self.assertNotContains(response, "{% include")

    def test_application_detail_shows_only_the_current_task_tab(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-tab is-active">Screening</span>')
        self.assertNotContains(response, 'data-section="cws-exam"')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-actions"')

    def test_cos_screening_checklist_uses_cos_document_requirements(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signed Application Letter")
        self.assertContains(response, "Transcript of Records (TOR)")
        self.assertContains(response, "Training Certificates")
        self.assertNotContains(response, "Performance Rating in the last rating period")

    def test_application_detail_header_hides_coarse_review_status_pill(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        match = re.search(
            r'(?s)<div class="rg-cws-header__pills">(.*?)</div>\s*<div class="rg-cws-header__actions">',
            content,
        )
        self.assertIsNotNone(match)
        header_pills = match.group(1)
        self.assertIn("Plantilla", header_pills)
        self.assertIn("Level 2", header_pills)
        self.assertIn("HRM Chief", header_pills)
        self.assertNotIn("HRM Chief Review", header_pills)

    def test_pipeline_advances_to_exam_after_screening_is_finalized(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            completeness_notes="",
            screening_notes="",
        )

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-tab is-active">Exam</span>')
        content = response.content.decode()
        self.assertRegex(
            content,
            r'(?s)rg-pipeline__stage is-complete">.*?<span class="rg-pipeline__label">Screening</span>',
        )
        self.assertRegex(
            content,
            r'(?s)rg-pipeline__stage is-current">.*?<span class="rg-pipeline__label">Exam</span>',
        )

    def test_failed_screening_keeps_disposition_inside_screening_stage(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            completeness_status=ScreeningRecord.CompletenessStatus.INCOMPLETE,
            qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
            completeness_notes="Missing required documents.",
            screening_notes="Applicant does not meet the screening requirements.",
        )

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-tab is-active">Screening</span>')
        self.assertContains(response, "Return to Applicant")
        self.assertContains(response, "Reject Application")
        self.assertNotContains(response, "Endorse to HRMPSB")
        self.assertNotContains(response, 'data-section="cws-exam"')
        content = response.content.decode()
        self.assertRegex(
            content,
            r'(?s)rg-pipeline__stage is-current">.*?<span class="rg-pipeline__label">Screening</span>',
        )

    def test_failed_screening_can_be_rejected_from_screening_stage(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            completeness_status=ScreeningRecord.CompletenessStatus.INCOMPLETE,
            qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
            completeness_notes="Missing required documents.",
            screening_notes="Applicant does not meet the screening requirements.",
        )

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.post(
            reverse("workflow-action", kwargs={"pk": application.pk}),
            {"action": "reject", "remarks": "Rejected directly from screening."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)

    def test_future_stage_posts_are_blocked_until_the_current_task_is_finalized(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrm_chief)

        exam_response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": "Blocked Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "85.00",
                "exam_result": "Passed",
                "valid_from": timezone.localdate().isoformat(),
                "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
                "exam_notes": "Should be blocked before screening is finalized.",
                "operation": "save",
            },
        )
        interview_response = client.post(
            reverse("interview-session", kwargs={"pk": application.pk}),
            {
                "scheduled_for": timezone.now() + timedelta(days=1),
                "location": "Blocked Interview Room",
                "session_notes": "Should be blocked before screening is finalized.",
                "operation": "save",
            },
        )

        self.assertEqual(exam_response.status_code, 403)
        self.assertEqual(interview_response.status_code, 403)


class ScreeningRecordTests(BaseRecruitmentTestCase):
    def screening_payload(self, **overrides):
        payload = {
            "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
            "completeness_notes": "All required application documents were reviewed.",
            "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
            "screening_notes": "Applicant satisfies the documented qualification basis.",
        }
        payload.update(overrides)
        return payload

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
        client.force_login(self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "finalize"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening output finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.finalized_by, self.secretariat)

    def test_screening_view_allows_blank_notes_fields(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    completeness_notes="",
                    screening_notes="",
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening output finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.completeness_notes, "")
        self.assertEqual(screening_record.screening_notes, "")

    def test_finalized_screening_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        screening_record = self.finalize_screening_for_current_stage(application, self.secretariat)

        with self.assertRaisesMessage(
            ValueError,
            "Finalized screening outputs are locked and cannot be modified.",
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
        client.force_login(self.hrmpsb)
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
        client.force_login(self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {**self.screening_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class ExamRecordTests(BaseRecruitmentTestCase):
    def exam_payload(self, **overrides):
        payload = {
            "exam_type": "Technical Examination",
            "exam_status": ExamRecord.ExamStatus.COMPLETED,
            "exam_score": "86.75",
            "exam_result": "Passed",
            "technical_score": "84.50",
            "technical_result": "Passed technical component",
            "practical_score": "89.00",
            "practical_result": "Passed practical component",
            "exam_date": timezone.localdate().isoformat(),
            "administered_by": "HRMS Exam Administrator",
            "valid_from": timezone.localdate().isoformat(),
            "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
            "exam_notes": "Validated through the current review stage.",
        }
        payload.update(overrides)
        return payload

    def test_current_handler_can_create_update_and_finalize_exam_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "82.50",
                "exam_result": "Initial pass",
                "technical_score": "80.00",
                "technical_result": "Initial technical pass",
                "practical_score": "85.00",
                "practical_result": "Initial practical pass",
                "exam_date": timezone.localdate(),
                "administered_by": "HRMS Exam Administrator",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=180),
                "exam_notes": "Initial exam draft.",
            },
            finalize=False,
        )
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.recruitment_case, application.case)
        self.assertEqual(ExamRecord.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_RECORDED,
            ).exists()
        )

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "90.00",
                "exam_result": "Passed with updated score",
                "technical_score": "88.00",
                "technical_result": "Technical passed",
                "practical_score": "92.00",
                "practical_result": "Practical passed",
                "exam_date": timezone.localdate(),
                "administered_by": "HRMS Exam Administrator",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Updated before finalization.",
            },
            finalize=True,
        )
        exam_record.refresh_from_db()
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "90.00")
        self.assertEqual(str(exam_record.technical_score), "88.00")
        self.assertEqual(str(exam_record.practical_score), "92.00")
        self.assertEqual(exam_record.exam_date, timezone.localdate())
        self.assertEqual(exam_record.administered_by, "HRMS Exam Administrator")
        self.assertEqual(exam_record.component_summary, "Technical: 88.00 (Technical passed); Practical: 92.00 (Practical passed)")
        self.assertEqual(exam_record.finalized_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_FINALIZED,
            ).exists()
        )

    def test_completed_exam_can_use_structured_components_without_overall_score(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical and Practical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": "82.00",
                "technical_result": "Technical passed",
                "practical_score": "88.00",
                "practical_result": "Practical passed",
                "exam_date": timezone.localdate(),
                "administered_by": "End-user and HRMS",
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Structured components recorded without an overall score.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertIsNone(exam_record.exam_score)
        self.assertEqual(str(exam_record.effective_score), "85.00")

    def test_exam_record_can_attach_optional_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Technical and Practical Examination",
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "91.00",
                "exam_result": "Passed",
                "technical_score": "90.00",
                "technical_result": "Technical passed",
                "practical_score": "92.00",
                "practical_result": "Practical passed",
                "exam_date": timezone.localdate(),
                "administered_by": "End-user and HRMS",
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Evidence file attached.",
            },
            evidence_file=SimpleUploadedFile(
                "exam-result.pdf",
                b"%PDF-1.4\nexam evidence",
                content_type="application/pdf",
            ),
            finalize=True,
        )

        exam_record.refresh_from_db()
        self.assertIsNotNone(exam_record.evidence_item)
        self.assertEqual(exam_record.evidence_item.artifact_scope, EvidenceVaultItem.OwnerScope.CASE)
        self.assertEqual(exam_record.evidence_item.artifact_type, "exam_supporting_evidence")
        self.assertEqual(exam_record.evidence_item.stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_UPLOADED,
                metadata__artifact_type="exam_supporting_evidence",
            ).exists()
        )

    def test_finalized_exam_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        exam_record = self.finalize_exam_for_current_stage(
            application,
            self.secretariat,
            valid_from=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=90),
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized examination outputs are locked and cannot be modified.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": "Technical Examination",
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "70.00",
                    "exam_result": "Changed after finalization",
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=30),
                    "exam_notes": "Should not save.",
                },
                finalize=False,
            )

        exam_record.refresh_from_db()
        self.assertEqual(str(exam_record.exam_score), "88.50")
        self.assertEqual(exam_record.exam_result, "Passed")

    def test_cos_exam_waiver_can_be_finalized_without_score_or_validity(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": "Internal COS Assessment",
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": None,
                "technical_result": "",
                "practical_score": None,
                "practical_result": "",
                "exam_date": None,
                "administered_by": "",
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Waived under COS office control.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(exam_record.branch, PositionPosting.Branch.COS)
        self.assertIsNone(exam_record.exam_score)
        self.assertIsNone(exam_record.valid_from)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.WAIVED)

    def test_completed_exam_requires_score_or_result(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        with self.assertRaises(ValidationError):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": "Technical Examination",
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": None,
                    "exam_result": "",
                    "technical_score": None,
                    "technical_result": "",
                    "practical_score": None,
                    "practical_result": "",
                    "exam_date": timezone.localdate(),
                    "administered_by": "HRMS Exam Administrator",
                    "valid_from": None,
                    "valid_until": None,
                    "exam_notes": "Missing score and result.",
                },
                finalize=False,
            )

    def test_unauthorized_user_cannot_record_exam(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrmpsb)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)

    def test_exam_view_invalid_payload_does_not_raise_server_error(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": "314",
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": "1231243",
                "exam_result": "214124",
                "valid_from": "0214-02-04",
                "valid_until": "124124-12-04",
                "exam_notes": "",
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExamRecord.objects.filter(application=application).exists())

    def test_secretariat_cannot_record_level2_exam_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        client.force_login(self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class EvidenceVaultTests(BaseRecruitmentTestCase):
    def test_evidence_is_encrypted_and_digest_is_stored_with_stage_metadata(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume.txt",
                b"plain-text resume",
                content_type="text/plain",
            ),
        )

        evidence = EvidenceVaultItem.objects.get(application=application)
        self.assertNotEqual(bytes(evidence.ciphertext), b"plain-text resume")
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
                "resume-v1.txt",
                b"resume version one",
                content_type="text/plain",
            ),
        )
        second_version = upload_evidence_item(
            application=application,
            actor=self.applicant,
            label="Resume",
            uploaded_file=SimpleUploadedFile(
                "resume-v2.txt",
                b"resume version two",
                content_type="text/plain",
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
            "could not be verified as a valid PDF, JPG, JPEG, or PNG document",
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

    def test_applicant_document_upload_rejects_files_larger_than_five_mb(self):
        application = RecruitmentApplication.objects.create(
            applicant=self.applicant,
            position=self.level1_position,
            qualification_summary="Qualified applicant.",
        )
        oversized_bytes = b"%PDF-1.4\n" + (b"A" * (5 * 1024 * 1024)) + b"\n%%EOF\n"

        with self.assertRaisesMessage(
            ValueError,
            "Each applicant document must be 5 MB or smaller.",
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

    def test_system_admin_can_download_uploaded_evidence(self):
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
        client.force_login(self.sysadmin)
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
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

    def test_system_admin_can_open_uploaded_evidence_inline(self):
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
        client.force_login(self.sysadmin)
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
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        evidence_url = reverse(
            "evidence-download",
            kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
        )
        audit_url = reverse("application-audit-log", kwargs={"pk": application.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, evidence.label)
        self.assertContains(response, evidence.original_filename)
        self.assertContains(response, f"{evidence_url}?disposition=inline")
        self.assertContains(response, evidence_url)
        self.assertNotContains(response, audit_url)
        self.assertContains(response, "View File")
        self.assertNotContains(response, "Evidence Vault")

    def test_evidence_service_rejects_unauthorized_upload_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot upload evidence for this application.",
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
                "routing-notes.txt",
                b"internal routing notes",
                content_type="text/plain",
            ),
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type="workflow_evidence",
        )

        self.assertEqual(evidence.artifact_scope, EvidenceVaultItem.OwnerScope.CASE)
        self.assertIsNone(evidence.application_id)
        self.assertEqual(evidence.recruitment_case_id, application.case.id)
        self.assertIsNone(evidence.recruitment_entry_id)

    def test_system_admin_can_search_archive_and_review_evidence_vault(self):
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
        client.force_login(self.sysadmin)

        initial_response = client.get(
            reverse("evidence-vault-list"),
            {
                "q": "Personal Data Sheet",
                "archival_status": "all",
            },
        )
        self.assertEqual(initial_response.status_code, 200)
        self.assertContains(initial_response, application.reference_label)

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
            follow=True,
        )
        self.assertEqual(archive_response.status_code, 200)
        evidence.refresh_from_db()
        self.assertTrue(evidence.is_archived)
        self.assertEqual(evidence.archive_tag, "Closed case retention batch")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_ARCHIVED,
                metadata__evidence_id=evidence.id,
            ).exists()
        )

        archived_response = client.get(
            reverse("evidence-vault-list"),
            {
                "q": application.reference_label,
                "archival_status": "archived",
                "current_version_only": "",
            },
        )
        self.assertEqual(archived_response.status_code, 200)
        self.assertContains(archived_response, "Closed case retention batch")
        self.assertContains(archived_response, application.reference_label)

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
        client.force_login(self.hrm_chief)
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
            client.force_login(user)
            with self.subTest(role=label):
                response = client.get(
                    reverse(
                        "evidence-download",
                        kwargs={"pk": application.pk, "evidence_pk": evidence.pk},
                    )
                )
                self.assertEqual(response.status_code, 403)

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
            client.force_login(user)
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
        client.force_login(self.secretariat)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Queue is clear")
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="Queue is clear" copy="No applications are currently routed to your role." %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")
        self.assertNotContains(
            response,
            '{% include "internal_includes/banner.html" with variant="info" copy="No active announcements. Notices from the HRM Chief or administrators will appear here." %}',
        )
        self.assertNotContains(response, "{# Contextual banner partial.")

    def test_workflow_queue_empty_state_renders_without_raw_include_tags(self):
        client = Client()
        client.force_login(self.secretariat)

        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No applications found")
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No applications found" copy="No applications are currently routed to your role, or no records match the current filter." %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")

    def test_recruitment_entry_empty_state_renders_without_raw_include_tags(self):
        PositionPosting.objects.all().delete()
        client = Client()
        client.force_login(self.hrm_chief)

        response = client.get(reverse("recruitment-entry-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No recruitment entries recorded yet")
        self.assertContains(response, reverse("recruitment-entry-create"))
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No recruitment entries recorded yet" copy="Create a Plantilla or COS entry to begin managing applications for a position." action_url=create_entry_url action_label="Create First Entry" %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")

    def test_recruitment_entry_list_renders_modal_partial_without_raw_include_tags(self):
        client = Client()
        client.force_login(self.hrm_chief)

        response = client.get(reverse("recruitment-entry-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Close this recruitment entry?")
        self.assertContains(response, f'data-bs-target="#closeModal{self.level1_position.pk}"')
        self.assertContains(response, f'id="closeModal{self.level1_position.pk}"')
        self.assertContains(response, f'id="closeForm{self.level1_position.pk}"')
        self.assertContains(response, f'form="closeForm{self.level1_position.pk}"')
        self.assertContains(response, 'class="mb-0 rg-modal-confirm__body-copy"')
        self.assertNotContains(
            response,
            '{% include "internal_includes/modal_confirm.html" with modal_id="closeModal"|add:entry.pk|stringformat:"s" variant="destructive" title="Close this recruitment entry?" body="Closing will stop accepting new applications for this entry. Existing cases in the workflow will not be affected." confirm_label="Close Entry" form_id="closeForm"|add:entry.pk|stringformat:"s" %}',
        )
        self.assertNotContains(response, "{# Confirmation modal partial.")

    def test_system_admin_dashboard_shows_role_label_and_admin_only_nav(self):
        client = Client()
        client.force_login(self.sysadmin)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Administrator")
        self.assertContains(response, "rg-pill--system-admin")
        self.assertContains(response, f'href="{reverse("evidence-vault-list")}"')
        self.assertContains(response, f'href="{reverse("audit-log-list")}"')
        self.assertContains(response, "User Management")

    def test_non_admin_dashboard_hides_evidence_and_audit_nav_items(self):
        roles = {
            "secretariat": self.secretariat,
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            client.force_login(user)
            response = client.get(reverse("dashboard"))

            with self.subTest(role=label):
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, f'href="{reverse("evidence-vault-list")}"')
                self.assertNotContains(response, f'href="{reverse("audit-log-list")}"')

    def test_non_admin_sidebar_keeps_only_my_queue_link_for_case_navigation(self):
        client = Client()
        client.force_login(self.hrm_chief)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("workflow-queue")}"')
        self.assertContains(response, "My Queue")
        self.assertNotContains(response, f'href="{reverse("application-list")}"')
        self.assertNotContains(response, ">Applications<")

    def test_application_list_redirects_to_workflow_queue_for_internal_users(self):
        client = Client()
        client.force_login(self.hrm_chief)

        response = client.get(reverse("application-list"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("workflow-queue"))

    def test_workflow_queue_shows_current_task_for_active_case(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current Task")
        self.assertContains(response, "Screening")
        self.assertNotContains(response, "HRM Chief Review")

    def test_workflow_queue_updates_current_task_after_screening_finalization(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)

        client = Client()
        client.force_login(self.hrm_chief)
        response = client.get(reverse("workflow-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current Task")
        self.assertContains(response, "Exam")
        self.assertNotContains(response, "HRM Chief Review")

    def test_applicant_user_cannot_access_internal_dashboard(self):
        client = Client()
        client.force_login(self.applicant)
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
        client.force_login(self.applicant)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 403)

    def test_export_bundle_returns_structured_zip_with_inventory_and_verification_outputs(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

        client = Client()
        client.force_login(self.hrm_chief)
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
                "appointing-notes.txt",
                b"appointing review notes",
                content_type="text/plain",
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
        client.force_login(self.secretariat)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")

    def test_non_export_role_cannot_access_controlled_export(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        client.force_login(self.hrmpsb)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 403)

    def test_export_service_rejects_unauthorized_actor(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot export this application.",
        ):
            build_export_bundle(application, self.hrmpsb)


class AuditLoggingTraceabilityTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def test_submission_audit_log_stores_traceability_fields(self):
        application = self.make_submitted_application()

        log = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.APPLICATION_SUBMITTED,
        )

        self.assertEqual(log.actor, self.applicant)
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, application.reference_number)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)

    def test_application_detail_view_logs_sensitive_record_access(self):
        application = self.make_submitted_application()

        client = Client()
        client.force_login(self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application=application,
            actor=self.secretariat,
            action=AuditLog.Action.PROTECTED_RECORD_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(log.metadata["access_source"], "application_detail")

    def test_system_admin_can_review_application_audit_log_and_it_is_logged(self):
        application = self.make_submitted_application()

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Application Audit Trail")
        log = AuditLog.objects.filter(
            application=application,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "application_audit")

    def test_evidence_vault_review_logs_sensitive_access(self):
        self.make_submitted_application()

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("evidence-vault-list"))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)

    def test_system_admin_can_review_system_audit_logs_only(self):
        record_system_audit_event(
            actor=self.sysadmin,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="System administrator changed a password.",
            metadata={"target_user_id": self.secretariat.id},
        )

        client = Client()
        client.force_login(self.sysadmin)
        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Audit Logs")
        self.assertContains(response, "Password Changed")
        self.assertFalse(response.context["audit_logs"][0].application_id if response.context["audit_logs"] else False)
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "system_audit")

    def test_non_admin_roles_cannot_review_audit_logs(self):
        application = self.make_submitted_application()
        record_system_audit_event(
            actor=self.sysadmin,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="System administrator changed a password.",
            metadata={"target_user_id": self.secretariat.id},
        )

        roles = {
            "secretariat": self.secretariat,
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            client.force_login(user)
            with self.subTest(role=label, endpoint="audit_log_list"):
                response = client.get(reverse("audit-log-list"))
                self.assertEqual(response.status_code, 403)
            with self.subTest(role=label, endpoint="application_audit_log"):
                response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))
                self.assertEqual(response.status_code, 403)

    def test_traceability_backfill_handles_draft_logs_without_reference_numbers(self):
        migration_module = importlib.import_module(
            "recruitment.migrations.0014_auditlog_traceability_fields"
        )
        application = self.make_application(self.level1_position)
        log = AuditLog.objects.create(
            application=application,
            actor=self.applicant,
            actor_role="",
            case_reference="",
            workflow_stage="",
            action=AuditLog.Action.APPLICATION_CREATED,
            description="Applicant created a draft application.",
            metadata={"review_stage": RecruitmentCase.Stage.SECRETARIAT_REVIEW},
            is_sensitive_access=False,
        )

        migration_module.backfill_audit_log_traceability(django_apps, None)

        log.refresh_from_db()
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, "")
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)


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
        self.assertIn(application.reference_number, mail.outbox[0].subject)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.NOTIFICATION_SENT,
                metadata__notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
            ).exists()
        )

    def test_approval_sends_selected_applicant_notification(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        mail.outbox.clear()

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        process_workflow_action(application, self.secretariat, "endorse", "COS screening done.")
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        self.finalize_deliberation_for_current_stage(application, self.hrm_chief)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.hrm_chief,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved.",
            )

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
        self.assertIn("selection result", mail.outbox[0].subject.lower())
        self.assertIn("COS", mail.outbox[0].body)

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
        self.assertIn("non-selection notice", mail.outbox[0].subject.lower())

    def test_secretariat_can_send_requirement_checklist_notification_for_level1_completion(self):
        application = self.make_approved_cos_application()
        mail.outbox.clear()
        client = Client()
        client.force_login(self.secretariat)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-checklist", kwargs={"pk": application.pk}),
                {
                    "checklist_items": "- Signed contract\n- Government-issued ID",
                    "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
                    "additional_message": "Bring original copies during submission.",
                },
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
        self.assertEqual(mail.outbox[0].subject, notification.subject)
        self.assertContains(response, "Requirement Checklist Notification")

    def test_secretariat_cannot_send_requirement_checklist_before_selection(self):
        application = self.make_submitted_application()
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("notification-checklist", kwargs={"pk": application.pk}),
            {
                "checklist_items": "- Any requirement",
                "deadline": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "additional_message": "",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_hrm_chief_can_send_reminder_notification_for_level2_completion(self):
        application = self.make_approved_level2_plantilla_application()
        mail.outbox.clear()
        client = Client()
        client.force_login(self.hrm_chief)

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
        self.assertContains(response, "Reminder Notification")


class CompletionTrackingTests(BaseRecruitmentTestCase):
    def completion_payload(self, items, **overrides):
        payload = {
            "completion_reference": "COMP-001",
            "completion_date": timezone.localdate().isoformat(),
            "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "remarks": "Completion tracking updated.",
        }
        payload.update(overrides)
        total_forms = len(items) + 1
        payload.update(
            {
                "completion_requirements-TOTAL_FORMS": str(total_forms),
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
            }
        )
        for index, item in enumerate(items):
            payload[f"completion_requirements-{index}-item_label"] = item["item_label"]
            payload[f"completion_requirements-{index}-status"] = item["status"]
            payload[f"completion_requirements-{index}-notes"] = item.get("notes", "")
        payload[f"completion_requirements-{len(items)}-item_label"] = ""
        payload[f"completion_requirements-{len(items)}-status"] = CompletionRequirement.RequirementStatus.PENDING
        payload[f"completion_requirements-{len(items)}-notes"] = ""
        return payload

    def test_plantilla_completion_tracking_stores_announcement_and_requirement_statuses(self):
        application = self.make_selected_application(self.level1_position)
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Validated by Secretariat.",
                    },
                    {
                        "item_label": "Medical certificate",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting submission.",
                    },
                ],
                completion_reference="PLANTILLA-APPT-001",
                announcement_reference="ANN-PL-2026-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Appointment completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(record.completion_reference, "PLANTILLA-APPT-001")
        self.assertEqual(record.announcement_reference, "ANN-PL-2026-001")
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(
            record.requirements.filter(
                item_label="Signed appointment paper",
                status=CompletionRequirement.RequirementStatus.COMPLETED,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.COMPLETION_RECORDED,
                metadata__completion_record_id=record.id,
            ).exists()
        )

    def test_cos_completion_tracking_ignores_announcement_fields_and_preserves_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Contract received.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing verified copy reused.",
                    },
                ],
                completion_reference="COS-CONTRACT-2026-001",
                announcement_reference="SHOULD-NOT-SAVE",
                announcement_date=timezone.localdate().isoformat(),
                remarks="COS contract completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.COS)
        self.assertEqual(record.completion_reference, "COS-CONTRACT-2026-001")
        self.assertEqual(record.announcement_reference, "")
        self.assertIsNone(record.announcement_date)
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(record.requirements_ready_for_closure)
        self.assertTrue(record.ready_for_closure)

    def test_case_close_requires_completion_reference(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing record reused.",
                    },
                ],
                completion_reference="",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Attempting closure without a contract reference."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Record the contract reference before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_requires_completion_date(self):
        application = self.make_selected_application(self.level1_position)
        client = Client()
        client.force_login(self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Filed.",
                    },
                    {
                        "item_label": "Medical clearance",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Waived.",
                    },
                ],
                completion_reference="PL-APPT-REF-002",
                completion_date="",
                announcement_reference="ANN-PL-REQ-002",
                announcement_date=timezone.localdate().isoformat(),
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Attempting closure without an appointment date."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Record the appointment date before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_resolved_checklist_alone_does_not_make_case_ready_for_closure(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing record reused.",
                    },
                ],
                completion_reference="",
                completion_date="",
            ),
            follow=True,
        )

        record = CompletionRecord.objects.get(application=application)
        self.assertTrue(record.requirements_ready_for_closure)
        self.assertFalse(record.ready_for_closure)

        detail_response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Case cannot be closed yet.")
        self.assertContains(
            detail_response,
            "Record the contract reference before closing the case.",
        )
        self.assertContains(
            detail_response,
            "Record the contract date before closing the case.",
        )
        self.assertNotContains(detail_response, "Ready to Close")

    def test_case_close_requires_resolved_completion_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        client.force_login(self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Tax form",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Still pending.",
                    },
                ],
                completion_reference="COS-CONTRACT-LOCK",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Trying to close too early."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "All completion requirements must be marked completed or not applicable before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_locks_case_and_closed_case_remains_retrievable(self):
        application = self.make_selected_application(self.level2_position)
        client = Client()
        client.force_login(self.hrm_chief)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Filed.",
                    },
                    {
                        "item_label": "Medical clearance",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Waived under recorded office rule.",
                    },
                ],
                completion_reference="PL2-APPT-001",
                announcement_reference="ANN-PL2-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Ready for case closure.",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished and archived."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, "")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )
        self.assertTrue(
            RoutingHistory.objects.filter(
                application=application,
                route_type=RoutingHistory.RouteType.CLOSE,
            ).exists()
        )

        detail_response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Completion Tracking")


class InterviewManagementTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Conference Room A",
            "session_notes": "Structured interview schedule prepared.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, **overrides):
        payload = {
            "rating_score": "89.50",
            "rating_notes": "Interview responses addressed the major competency areas.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def test_secretariat_cannot_manage_interview_before_the_case_reaches_that_task(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(
            ValueError,
            "You cannot manage interview scheduling for this application at its current workflow stage.",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(location="Secretariat Coordination Room"),
                finalize=False,
            )

    def test_hrm_chief_can_record_direct_interview_rating_for_cos_case(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)

        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(location="Virtual Interview Room"),
            finalize=False,
        )
        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(rating_score="91.25"),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(
                location="Virtual Interview Room",
                session_notes="Direct HRM Chief interview rating finalized.",
            ),
            finalize=True,
        )

        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_rating.rated_by, self.hrm_chief)
        self.assertEqual(str(interview_rating.rating_score), "91.25")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
            ).exists()
        )

    def test_finalized_interview_session_blocks_session_rating_and_fallback_changes(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)

        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Finalized interview session."),
            finalize=True,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept rating changes.",
        ):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.rating_payload(rating_score="90.00"),
            )
        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview sessions are locked and cannot accept fallback rating uploads.",
        ):
            upload_interview_fallback_rating(
                application=application,
                actor=self.hrm_chief,
                uploaded_file=SimpleUploadedFile("fallback.pdf", b"fallback", content_type="application/pdf"),
                remarks="Late upload.",
            )

        interview_session.refresh_from_db()
        self.assertTrue(interview_session.is_finalized)


class DeliberationDecisionSupportTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Virtual Deliberation Room",
            "session_notes": "Decision-support interview session prepared.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, **overrides):
        payload = {
            "rating_score": "92.00",
            "rating_notes": "Interview performance supports the recommendation.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def test_cos_deliberation_consolidates_finalized_outputs(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(),
        )
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Interview output locked before deliberation."),
            finalize=True,
        )

        deliberation_record = self.finalize_deliberation_for_current_stage(application, self.hrm_chief)

        self.assertTrue(deliberation_record.is_finalized)
        self.assertEqual(deliberation_record.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["finalized_screening_count"],
            2,
        )
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["finalized_interview_count"],
            1,
        )
        self.assertEqual(
            deliberation_record.consolidated_snapshot["summary"]["latest_interview_average"],
            "92.00",
        )

    def test_plantilla_recommendation_requires_deliberation_and_car(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the deliberation record before recommending this Plantilla application.",
        ):
            process_workflow_action(application, self.hrmpsb, "recommend", "Attempted without deliberation.")

        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the Comparative Assessment Report before recommending this Plantilla application.",
        ):
            process_workflow_action(application, self.hrmpsb, "recommend", "Attempted without CAR.")

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)
        self.assertEqual(ComparativeAssessmentReportItem.objects.filter(report=report).count(), 1)

    def test_car_generation_creates_versioned_evidence(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.version_number, 1)
        self.assertTrue(report.evidence_item.is_current_version)
        self.assertEqual(report.evidence_item.stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertEqual(report.evidence_item.artifact_scope, EvidenceVaultItem.OwnerScope.ENTRY)
        self.assertEqual(report.evidence_item.artifact_type, "comparative_assessment_report")
        self.assertIsNone(report.evidence_item.application_id)
        self.assertIsNone(report.evidence_item.recruitment_case_id)
        self.assertEqual(report.evidence_item.recruitment_entry_id, application.position_id)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CAR_FINALIZED,
            ).exists()
        )

    def test_car_generation_creates_versioned_entry_reports_across_candidates(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="car-secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="car.second.candidate@example.com",
            applicant_phone="09179990001",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for CAR reuse testing.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="car-secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        process_workflow_action(
            secondary_application,
            self.secretariat,
            "endorse",
            "Forward second candidate to HRM Chief.",
        )
        secondary_application.refresh_from_db()
        self.finalize_screening_for_current_stage(secondary_application, self.hrm_chief)
        self.finalize_exam_for_current_stage(secondary_application, self.hrm_chief)
        process_workflow_action(
            secondary_application,
            self.hrm_chief,
            "endorse",
            "Forward second candidate to HRMPSB.",
        )
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)

        draft_report = generate_comparative_assessment_report(
            application=primary_application,
            actor=self.hrmpsb,
            cleaned_data={"summary_notes": "Draft entry-level CAR."},
            finalize=False,
        )
        finalized_report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.hrmpsb,
            cleaned_data={"summary_notes": "Final entry-level CAR."},
            finalize=True,
        )

        self.assertNotEqual(draft_report.pk, finalized_report.pk)
        self.assertEqual(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=self.level1_position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).count(),
            2,
        )
        self.assertEqual(draft_report.version_number, 1)
        self.assertEqual(finalized_report.version_number, 2)
        self.assertFalse(draft_report.is_finalized)
        self.assertTrue(finalized_report.is_finalized)
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=finalized_report).count(),
            2,
        )

    def test_finalized_car_is_reused_for_other_candidates_in_same_entry(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="second.candidate@example.com",
            applicant_phone="09179990000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for the same Plantilla entry.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        process_workflow_action(
            secondary_application,
            self.secretariat,
            "endorse",
            "Forward second candidate to HRM Chief.",
        )
        secondary_application.refresh_from_db()
        self.finalize_screening_for_current_stage(secondary_application, self.hrm_chief)
        self.finalize_exam_for_current_stage(secondary_application, self.hrm_chief)
        process_workflow_action(
            secondary_application,
            self.hrm_chief,
            "endorse",
            "Forward second candidate to HRMPSB.",
        )
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)
        shared_report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)

        secondary_packet = build_submission_packet(secondary_application)
        self.assertTrue(secondary_packet["summary"]["has_comparative_assessment_report"])
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=shared_report).count(),
            2,
        )

        process_workflow_action(
            secondary_application,
            self.hrmpsb,
            "recommend",
            "Shared CAR covers all ranked candidates in the entry.",
        )
        secondary_application.refresh_from_db()
        self.assertEqual(
            secondary_application.status,
            RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
        )
        self.assertEqual(
            secondary_application.case.current_stage,
            RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
        )


class FinalDecisionHandlingTests(BaseRecruitmentTestCase):
    def test_selected_final_decision_routes_case_to_completion_and_preserves_packet(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        decision = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after reviewing the final submission packet.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertTrue(decision.submission_packet_snapshot["summary"]["has_deliberation_record"])
        self.assertTrue(decision.submission_packet_snapshot["summary"]["has_comparative_assessment_report"])
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )

    def test_not_selected_final_decision_closes_case_and_locks_it(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        decision = self.record_final_decision_for_current_stage(
            application,
            self.hrm_chief,
            decision_outcome=FinalDecision.Outcome.NOT_SELECTED,
            decision_notes="Not selected after reviewing final records.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.NOT_SELECTED)
        self.assertEqual(decision.decided_by, self.hrm_chief)
        self.assertEqual(decision.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_appointing_authority_cannot_record_cos_selection(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Only the HRM Chief may record the final decision at the current workflow stage.",
        ):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="COS should not require Appointing Authority signing.",
            )

        self.assertFalse(FinalDecision.objects.filter(application=application).exists())
        application.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

    def test_application_detail_exposes_decision_packet_sections(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        client.force_login(self.appointing)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-tab is-active">Decision</span>')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-deliberation"')
        self.assertContains(response, 'rg-cws-layout rg-cws-layout--full')
        self.assertNotContains(response, "Workflow Snapshot")
        self.assertContains(response, "Final Decision")

        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_deliberation_record"])
