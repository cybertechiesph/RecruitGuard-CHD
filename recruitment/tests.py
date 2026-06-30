import importlib
import io
import json
import re
import urllib.parse
import uuid
import zipfile
from decimal import Decimal
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django import forms
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .captcha import (
    CAPTCHA_ANSWER_SESSION_KEY,
    validate_recaptcha_token,
    validate_turnstile_token,
)
from .forms import (
    APPLICANT_MOBILE_ERROR_MESSAGE,
    APPLICANT_NAME_ERROR_MESSAGE,
    APPLICANT_QUALIFICATION_SUMMARY_LENGTH_ERROR_MESSAGE,
    APPLICANT_QUALIFICATION_SUMMARY_MAX_LENGTH,
    ApplicantOTPForm,
    ApplicantPortalIntakeForm,
    CaseHandoffForm,
    ExamRecordForm,
    InternalMFAOTPForm,
    WorkflowActionForm,
)
from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    FinalSelection,
    InternalEmailChangeRequest,
    InternalLoginAttempt,
    InternalMFAChallenge,
    InternalPasswordHistory,
    CompetencyDefinition,
    CompetencyRatingTemplate,
    CompetencyScore,
    InterviewRating,
    Notification,
    NotificationLog,
    PositionDocumentRequirement,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningDocumentReview,
    ScreeningRecord,
    VacancyAssessmentWeights,
    WorkflowOverride,
)
from .permissions import (
    INTERNAL_MFA_USER_SESSION_KEY,
    INTERNAL_MFA_VERIFIED_SESSION_KEY,
)
from .requirements import (
    APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH,
    DIPLOMA,
    MIN_REQUIRED_DOCUMENT_CODES,
    PERFORMANCE_RATING,
    PERSONAL_DATA_SHEET,
    SIGNED_COVER_LETTER,
    TRAINING_CERTIFICATES,
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from .services import (
    build_export_bundle,
    build_submission_packet,
    emit_deadline_approaching_notifications,
    generate_comparative_assessment_report,
    get_current_workflow_section,
    get_latest_finalized_comparative_assessment_report,
    get_available_actions,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    persist_position,
    process_workflow_action,
    repair_auto_advance_workflow_boundaries,
    record_final_decision,
    record_final_selection,
    record_system_audit_event,
    save_deliberation_record,
    save_exam_record,
    save_exam_schedule,
    create_competency_rating_template,
    create_default_position_document_requirements,
    get_or_create_vacancy_assessment_weights,
    lock_vacancy_assessment_weights,
    persist_recruitment_entry,
    get_competency_rating_template,
    get_missing_required_applicant_document_requirements,
    set_position_document_requirements,
    get_published_competency_rating_template,
    compute_competency_rating_score,
    set_application_ete_rating,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    submit_document_resubmission,
    update_recruitment_entry_status,
    upload_interview_fallback_rating,
    upload_evidence_item,
    user_can_manage_comparative_assessment_report,
    user_can_view_application,
    verify_application_otp,
)
from .upload_validation import validate_applicant_document_upload


User = get_user_model()


@override_settings(
    CAPTCHA_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class BaseRecruitmentTestCase(TestCase):
    def setUp(self):
        self.applicant = User.objects.create_user(
            username="applicant",
            password="testpass123",
            email="applicant@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        self.secretariat = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.hrm_chief = User.objects.create_user(
            username="hrmchief",
            password="testpass123",
            email="hrmchief@example.com",
            role=RecruitmentUser.Role.HRM_CHIEF,
        )
        self.hrmpsb = User.objects.create_user(
            username="hrmpsb",
            password="testpass123",
            email="hrmpsb@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        self.appointing = User.objects.create_user(
            username="appointing",
            password="testpass123",
            email="appointing@example.com",
            role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
        )
        self.sysadmin = User.objects.create_user(
            username="sysadmin",
            password="testpass123",
            email="sysadmin@example.com",
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
            item_number="OSEC-DOH-AA6-1-2026",
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        self.level2_position = PositionPosting.objects.create(
            position_reference=self.medical_officer_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_2,
            item_number="OSEC-DOH-MO5-2-2026",
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

    def force_login_with_mfa(self, client, user):
        client.force_login(user)
        if getattr(user, "is_internal_user", False):
            session = client.session
            session[INTERNAL_MFA_VERIFIED_SESSION_KEY] = True
            session[INTERNAL_MFA_USER_SESSION_KEY] = user.id
            session.save()

    def level1_closing_date(self):
        return PositionPosting.calculate_plantilla_closing_date(timezone.localdate())

    def entry_opening_date(self):
        return timezone.localdate()

    def entry_opening_date_string(self):
        return self.entry_opening_date().isoformat()

    def finalize_applicant_pool_for_test(self, entry):
        if entry.applicant_pool_is_finalized:
            return entry
        entry.status = PositionPosting.EntryStatus.CLOSED
        entry.save(update_fields=["status", "is_active", "updated_at"])
        entry.refresh_from_db()
        return entry

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
            application,
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

    def screening_document_status_payload(self, application, *, default_status=None, overrides=None):
        default_status = default_status or ScreeningDocumentReview.ReviewStatus.MEETS
        overrides = overrides or {}
        payload = {}
        for requirement in get_applicant_document_requirements(application):
            has_evidence = application.evidence_items.filter(
                document_key=requirement.code,
                is_current_version=True,
                is_archived=False,
            ).exists()
            is_not_applicable = (
                requirement.conditional_on_performance_rating
                and application.performance_rating_not_applicable
                and not has_evidence
            )
            is_required = requirement.is_required or (
                requirement.conditional_on_performance_rating
                and not application.performance_rating_not_applicable
            )
            if requirement.code in overrides:
                status = overrides[requirement.code]
            elif is_not_applicable or (not has_evidence and not is_required):
                status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
            elif not has_evidence:
                status = ScreeningDocumentReview.ReviewStatus.ABSENT
            else:
                status = default_status
            payload[f"document_status__{requirement.code}"] = status
        return payload

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
        education_score=None,
        training_score=None,
        experience_score=None,
        document_review_score=None,
    ):
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
                "completeness_status": completeness_status,
                "completeness_notes": completeness_notes,
                "qualification_outcome": qualification_outcome,
                "education_score": education_score,
                "training_score": training_score,
                "experience_score": experience_score,
                "document_review_score": document_review_score,
                "screening_notes": screening_notes,
            },
            finalize=True,
        )

    def schedule_exam_for_current_stage(self, application, actor, scheduled_for=None):
        return save_exam_schedule(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": scheduled_for or (timezone.now() + timedelta(hours=1)),
                "venue": "CHD CALABARZON Examination Room",
                "instructions": "Bring a valid government ID.",
            },
        )

    def finalize_exam_for_current_stage(
        self,
        application,
        actor,
        exam_type=ExamRecord.ExamType.TECHNICAL_PRACTICAL,
        exam_status=ExamRecord.ExamStatus.COMPLETED,
        exam_score="88.50",
        exam_result="",
        technical_score=None,
        technical_result="",
        general_score=None,
        general_result="",
        exam_date=None,
        administered_by=ExamRecord.AdministeredBy.HRMS,
        valid_from=None,
        valid_until=None,
        exam_notes="Formal examination output recorded.",
    ):
        exam_date = exam_date or timezone.localdate()
        # The overall is computed (Gen x0.60 + Tech x0.40); default each component to
        # the requested overall so the computed result equals exam_score.
        if technical_score is None:
            technical_score = exam_score
        if general_score is None:
            general_score = exam_score
        save_exam_schedule(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": timezone.now() + timedelta(hours=1),
                "venue": "CHD CALABARZON Examination Room",
                "instructions": "Bring a valid government ID.",
            },
        )
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
                "general_score": general_score,
                "general_result": general_result,
                "exam_date": exam_date,
                "administered_by": administered_by,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "exam_notes": exam_notes,
            },
            finalize=True,
        )

    def publish_competency_rating_sheet(self, entry, actor=None, scale_max=4):
        """Ensure the vacancy has a published competency rating sheet (standard
        Core + Organizational competencies) the panel can score against."""
        actor = actor or self.secretariat
        template = get_competency_rating_template(entry)
        if template is None:
            template = create_competency_rating_template(entry, actor, scale_max=scale_max)
        if template.status != CompetencyRatingTemplate.Status.PUBLISHED:
            template.status = CompetencyRatingTemplate.Status.PUBLISHED
            if not template.published_at:
                template.published_at = timezone.now()
            template.save(update_fields=["status", "published_at", "updated_at"])
        return template

    def competency_rating_cleaned_data(self, application, level=3, *, actor=None, **overrides):
        """cleaned_data for save_interview_rating: publishes a sheet for the vacancy
        and scores every competency at ``level`` (uniform). On a 1-4 scale a level
        of 4->100, 3->75, 2->50, 1->25 after normalization."""
        template = self.publish_competency_rating_sheet(application.position, actor=actor)
        payload = {
            "competency_scores": {
                competency: level for competency in template.competencies.all()
            },
            "rating_notes": "Interview responses addressed the major competency areas.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _score_to_competency_level(rating_score, scale_max=4):
        """Map a legacy 0-100 rating to a uniform competency level (1..scale_max)."""
        try:
            value = float(rating_score)
        except (TypeError, ValueError):
            return 3
        return max(1, min(scale_max, int(round(value / 100 * scale_max))))

    def finalize_interview_for_current_stage(
        self,
        application,
        actor,
        scheduled_for=None,
        location="Conference Room A",
        session_notes="Structured interview output preserved.",
        level=None,
        rating_score=None,
        rating_notes="Interview responses addressed the competency requirements.",
        justification="Consistent competency performance recorded by the panel.",
    ):
        if level is None:
            level = (
                self._score_to_competency_level(rating_score)
                if rating_score is not None
                else 3
            )
        scheduled_for = scheduled_for or (timezone.now() + timedelta(days=1))
        session_actor = actor
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        ):
            session_actor = (
                self.secretariat
                if application.level == PositionPosting.Level.LEVEL_1
                else self.hrm_chief
            )
        save_interview_session(
            application=application,
            actor=session_actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": location,
                "session_notes": session_notes,
            },
            finalize=False,
        )
        template = self.publish_competency_rating_sheet(application.position)
        save_interview_rating(
            application=application,
            actor=actor,
            cleaned_data={
                "competency_scores": {
                    competency: level for competency in template.competencies.all()
                },
                "rating_notes": rating_notes,
                "justification": justification,
            },
        )
        return save_interview_session(
            application=application,
            actor=session_actor,
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
        if (
            application.branch == PositionPosting.Branch.COS
            and application.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW
        ):
            self.finalize_screening_for_current_stage(application, self.secretariat)
            self.finalize_exam_for_current_stage(application, self.secretariat)
            application.refresh_from_db()
        return application

    def move_application_to_hrmpsb_review(self, application):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        if application.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(application, self.secretariat)
            self.finalize_exam_for_current_stage(application, self.secretariat)
        elif application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
            self.finalize_exam_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    def move_application_to_appointing_review(self, application):
        if application.branch == PositionPosting.Branch.PLANTILLA:
            self.move_application_to_hrmpsb_review(application)
            self.finalize_interview_for_current_stage(application, self.hrmpsb)
            self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
            self.finalize_car_for_current_stage(application, self.hrmpsb)
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
        recommendation="HRMPSB recommends this ranking based on the CAR draft.",
        decision_support_summary="Decision-support summary preserved for routing.",
        quorum_status=DeliberationRecord.QuorumStatus.MET,
        attendance_notes="Chairperson and HRMPSB members attended with quorum met.",
        ranking_notes="Ranking basis recorded for decision support.",
    ):
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        ):
            self.finalize_applicant_pool_for_test(application.position)
            car_actor = (
                self.secretariat
                if application.level == PositionPosting.Level.LEVEL_1
                else self.hrm_chief
            )
            if not ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
                is_finalized=False,
            ).exists():
                generate_comparative_assessment_report(
                    application=application,
                    actor=car_actor,
                    cleaned_data={"summary_notes": "Draft CAR prepared for HRMPSB deliberation."},
                    finalize=False,
                )
            # Plantilla no longer records an in-system deliberation — the CAR is the
            # gate now. Leave the draft staged and return without a deliberation record.
            return None
        return save_deliberation_record(
            application=application,
            actor=actor,
            cleaned_data={
                "deliberated_at": deliberated_at or timezone.now(),
                "deliberation_minutes": deliberation_minutes,
                "recommendation": recommendation,
                "decision_support_summary": decision_support_summary,
                "quorum_status": quorum_status,
                "attendance_notes": attendance_notes,
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
        car_actor = actor
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
            and actor.role == RecruitmentUser.Role.HRMPSB_MEMBER
        ):
            car_actor = (
                self.secretariat
                if application.level == PositionPosting.Level.LEVEL_1
                else self.hrm_chief
            )
        return generate_comparative_assessment_report(
            application=application,
            actor=car_actor,
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
        if application.branch == PositionPosting.Branch.PLANTILLA:
            report = get_latest_finalized_comparative_assessment_report(application)
            if not report:
                raise ValueError("Finalize the CAR before recording the final selection.")
            candidate_items = report.items.select_related(
                "recruitment_case",
                "recruitment_case__application",
            ).order_by("rank_order", "created_at")
            if decision_outcome == FinalDecision.Outcome.NOT_SELECTED:
                selected_item = candidate_items.exclude(
                    recruitment_case__application=application,
                ).first()
            else:
                selected_item = candidate_items.filter(
                    recruitment_case__application=application,
                ).first()
            if not selected_item:
                raise ValueError("Plantilla final selection requires a selected CAR applicant.")
            return record_final_selection(
                application=application,
                actor=actor,
                cleaned_data={
                    "selected_item": selected_item,
                    "decision_notes": decision_notes,
                },
            )
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


@override_settings(
    CAPTCHA_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class FoundationSmokeTests(TestCase):
    def extract_internal_mfa_code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

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

    def test_security_headers_are_applied(self):
        response = self.client.get(reverse("login"))

        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertIn("camera=()", response["Permissions-Policy"])
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_internal_user_can_log_in(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))
        self.assertContains(response, "Check Your Email")
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )
        challenge = InternalMFAChallenge.objects.get(user=user)
        otp_code = self.extract_internal_mfa_code()
        self.assertNotEqual(challenge.otp_hash, otp_code)
        self.assertEqual(len(mail.outbox[-1].alternatives), 1)
        html_body, mime_type = mail.outbox[-1].alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Internal Verification Code", html_body)
        self.assertIn("DOH–CHD CALABARZON", html_body)
        self.assertIn(otp_code, html_body)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": otp_code},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("workflow-queue"))
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_VERIFIED).exists()
        )

    @override_settings(INTERNAL_MFA_ENABLED=False)
    def test_internal_login_can_bypass_mfa_when_disabled(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("workflow-queue"))
        self.assertFalse(InternalMFAChallenge.objects.filter(user=user).exists())
        self.assertEqual(mail.outbox, [])
        self.assertTrue(self.client.session[INTERNAL_MFA_VERIFIED_SESSION_KEY])
        self.assertEqual(self.client.session[INTERNAL_MFA_USER_SESSION_KEY], user.id)

    def test_internal_mfa_email_falls_back_to_text_when_html_render_fails(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        # Simulate a missing/broken "email/internal_mfa_otp.html" template. The
        # MFA code email is sent synchronously inside an atomic block, so a render
        # failure must not roll back the challenge or block staff login; it should
        # degrade to text-only delivery (the code lives in the plain-text body).
        with patch(
            "recruitment.services.render_to_string",
            side_effect=RuntimeError("template render boom"),
        ), self.assertLogs("recruitment.services", level="ERROR") as logs:
            response = self.client.post(
                reverse("login"),
                {"username": "secretariat", "password": "testpass123"},
                follow=True,
            )

        # Login still advances to the MFA verification step.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))

        # The challenge row survived (the atomic block did not roll back)...
        challenge = InternalMFAChallenge.objects.get(user=user)
        # ...and the code email went out, degraded to text-only.
        otp_code = self.extract_internal_mfa_code()
        self.assertNotEqual(challenge.otp_hash, otp_code)
        self.assertEqual(mail.outbox[-1].alternatives, [])
        self.assertTrue(
            any(
                "Failed to render internal MFA HTML email" in message
                for message in logs.output
            )
        )

    def test_internal_mfa_login_rotates_session_key_and_marks_session_verified(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        session = self.client.session
        session["pre_login_marker"] = "before-mfa"
        session.save()
        initial_session_key = session.session_key

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        pending_session_key = self.client.session.session_key
        self.assertNotEqual(initial_session_key, pending_session_key)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": self.extract_internal_mfa_code()},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("workflow-queue"))
        verified_session = self.client.session
        self.assertNotEqual(initial_session_key, verified_session.session_key)
        self.assertTrue(verified_session[INTERNAL_MFA_VERIFIED_SESSION_KEY])
        self.assertEqual(verified_session[INTERNAL_MFA_USER_SESSION_KEY], user.id)

    def test_internal_login_requires_registered_email_for_mfa(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have an email address")
        self.assertFalse(InternalMFAChallenge.objects.exists())
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_FAILED).exists()
        )

    def test_internal_page_requires_completed_mfa(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_session_inactivity_timeout_is_configured(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 30 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_internal_login_challenge_respects_cooldown(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)

        challenge.requested_at = timezone.now() - timedelta(
            seconds=settings.INTERNAL_MFA_RESEND_COOLDOWN_SECONDS + 1
        )
        challenge.save(update_fields=["requested_at", "updated_at"])

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 2)

    def test_internal_mfa_resend_respects_cooldown(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)

        challenge.requested_at = timezone.now() - timedelta(
            seconds=settings.INTERNAL_MFA_RESEND_COOLDOWN_SECONDS + 1
        )
        challenge.save(update_fields=["requested_at", "updated_at"])
        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "A new verification code has been sent.")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 2)
        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_RESENT).exists()
        )

    @override_settings(
        INTERNAL_LOGIN_MAX_ATTEMPTS=2,
        INTERNAL_LOGIN_WINDOW_MINUTES=15,
        INTERNAL_LOGIN_LOCKOUT_MINUTES=15,
    )
    def test_internal_login_locks_after_repeated_bad_passwords(self):
        User.objects.create_user(
            username="sysadmin",
            password="testpass123",
            email="sysadmin@example.com",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        for _attempt in range(2):
            response = self.client.post(
                reverse("login"),
                {"username": "secretariat", "password": "wrongpass"},
            )
            self.assertEqual(response.status_code, 200)

        attempt = InternalLoginAttempt.objects.get(username_normalized="secretariat")
        self.assertTrue(attempt.is_locked)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN_LOCKED).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN_ALERT_SENT).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("internal login lockout alert", mail.outbox[0].subject.lower())

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertContains(response, "Too many failed sign-in attempts")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_successful_password_entry_clears_failed_login_counter(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "wrongpass"},
        )
        self.assertTrue(
            InternalLoginAttempt.objects.filter(username_normalized="secretariat").exists()
        )

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))
        self.assertFalse(
            InternalLoginAttempt.objects.filter(username_normalized="secretariat").exists()
        )

    def test_internal_password_reset_uses_tokenized_email_link(self):
        cache.clear()
        user = User.objects.create_user(
            username="secretariat",
            password="OriginalSecurePass123!",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).exists()
        )
        reset_path = re.search(
            r"http://testserver(?P<path>/internal/password/reset/[^\s]+/[^\s]+/)",
            mail.outbox[0].body,
        ).group("path")

        response = self.client.get(reset_path, follow=True)
        confirm_path = response.request["PATH_INFO"]
        response = self.client.post(
            confirm_path,
            {
                "new_password1": "DifferentSecurePass123!",
                "new_password2": "DifferentSecurePass123!",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-complete"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("DifferentSecurePass123!"))
        self.assertTrue(
            InternalPasswordHistory.objects.filter(user=user).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_COMPLETED).exists()
        )

    def test_password_reset_page_shows_logo_and_rate_limit_notice(self):
        response = self.client.get(reverse("password-reset"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rg-login-card__seal")
        self.assertContains(
            response,
            "For security, repeated reset requests may be temporarily delayed.",
        )

    def test_password_reset_email_cooldown_suppresses_repeat_email_neutrally(self):
        cache.clear()
        User.objects.create_user(
            username="secretariat",
            password="OriginalSecurePass123!",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        first_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )
        second_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )

        self.assertEqual(first_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(second_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertContains(
            second_response,
            "If the email address matches an active internal account, a password reset link will be sent.",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).count(),
            1,
        )

    @override_settings(
        PASSWORD_RESET_EMAIL_COOLDOWN_SECONDS=0,
        PASSWORD_RESET_EMAIL_MAX_PER_WINDOW=0,
        PASSWORD_RESET_IP_MAX_PER_WINDOW=1,
        PASSWORD_RESET_IP_WINDOW_SECONDS=3600,
    )
    def test_password_reset_ip_limit_suppresses_additional_reset_email_neutrally(self):
        cache.clear()
        User.objects.create_user(
            username="secretariat-one",
            password="OriginalSecurePass123!",
            email="secretariat.one@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        User.objects.create_user(
            username="secretariat-two",
            password="OriginalSecurePass123!",
            email="secretariat.two@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        first_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat.one@example.com"},
            follow=True,
            REMOTE_ADDR="203.0.113.10",
        )
        second_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat.two@example.com"},
            follow=True,
            REMOTE_ADDR="203.0.113.10",
        )

        self.assertEqual(first_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(second_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).count(),
            1,
        )

    def test_password_reset_does_not_email_applicant_accounts(self):
        cache.clear()
        User.objects.create_user(
            username="applicant",
            password="ApplicantSecurePass123!",
            email="applicant@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )

        response = self.client.post(
            reverse("password-reset"),
            {"email": "applicant@example.com"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_internal_mfa_code_is_rejected(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        otp_code = self.extract_internal_mfa_code()
        challenge = InternalMFAChallenge.objects.get(user=user)
        challenge.expires_at = timezone.now() - timedelta(minutes=1)
        challenge.save(update_fields=["expires_at", "updated_at"])

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": otp_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid or expired")
        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_EXPIRED).exists()
        )

    @override_settings(INTERNAL_MFA_MAX_ATTEMPTS=2)
    def test_internal_mfa_locks_after_too_many_failed_attempts(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        for _attempt in range(2):
            response = self.client.post(
                reverse("internal-mfa-verify"),
                {"action": "verify", "otp": "000000"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "invalid or expired")

        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertEqual(challenge.attempt_count, 2)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_LOCKED).exists()
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


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="local",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class CaptchaProtectionTests(TestCase):
    def captcha_answer(self, scope):
        return self.client.session[CAPTCHA_ANSWER_SESSION_KEY.format(scope=scope)]

    def test_internal_login_requires_valid_captcha(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.get(reverse("login"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

        response = self.client.post(
            reverse("login"),
            {
                "username": "secretariat",
                "password": "testpass123",
                "captcha_answer": self.captcha_answer("internal_login"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.count(), 1)

    def test_internal_mfa_page_uses_cooldown_without_duplicate_captcha(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.get(reverse("login"))
        self.client.post(
            reverse("login"),
            {
                "username": "secretariat",
                "password": "testpass123",
                "captcha_answer": self.captcha_answer("internal_login"),
            },
        )
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.get(reverse("internal-mfa-verify"))
        self.assertNotContains(response, "Security check")

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_otp_verification_forms_do_not_duplicate_captcha(self):
        self.assertNotIn("captcha_answer", ApplicantOTPForm().fields)
        self.assertNotIn("captcha_answer", InternalMFAOTPForm().fields)

    def test_internal_password_reset_requires_valid_captcha(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.get(reverse("password-reset"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertEqual(len(mail.outbox), 0)

    def test_applicant_status_lookup_requires_valid_captcha(self):
        response = self.client.get(reverse("applicant-status-lookup"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("applicant-status-lookup"),
            {
                "application_id": "RG-20260528-ABCDE1",
                "email": "applicant@example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")

        response = self.client.post(
            reverse("applicant-status-lookup"),
            {
                "application_id": "RG-20260528-ABCDE1",
                "email": "applicant@example.com",
                "captcha_answer": self.captcha_answer("applicant_status_lookup"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "We could not find an application")


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="turnstile",
    TURNSTILE_SITE_KEY="0x4AAAAAADmkw6o9jbb0OdQs",
    TURNSTILE_VERIFY_URL="https://turnstile-siteverify.example.workers.dev",
    TURNSTILE_VERIFY_ALLOWED_HOSTS="turnstile-siteverify.example.workers.dev,challenges.cloudflare.com",
    TURNSTILE_SECRET_KEY="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class TurnstileCaptchaTests(TestCase):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_turnstile_response(self, success):
        payload = json.dumps({"success": success}).encode("utf-8")
        return self.FakeResponse(payload)

    def test_turnstile_widget_renders_on_internal_login(self):
        response = self.client.get(reverse("login"))

        self.assertContains(
            response,
            "https://challenges.cloudflare.com/turnstile/v0/api.js",
        )
        self.assertContains(response, 'class="cf-turnstile"')
        self.assertContains(response, 'data-sitekey="0x4AAAAAADmkw6o9jbb0OdQs"')
        self.assertContains(response, 'data-action="turnstile-spin-v1"')
        self.assertContains(response, 'data-callback="rgCaptchaSuccess"')

    def test_turnstile_worker_success_allows_internal_login_challenge(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(True),
        ) as urlopen:
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "cf-turnstile-response": "valid-turnstile-token",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://turnstile-siteverify.example.workers.dev",
        )
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(
            json.loads(request.data.decode("utf-8"))["token"],
            "valid-turnstile-token",
        )
        self.assertNotIn("remoteip", json.loads(request.data.decode("utf-8")))

    def test_turnstile_direct_secret_takes_priority_over_worker(self):
        with self.settings(TURNSTILE_SECRET_KEY="configured-secret"):
            with patch(
                "recruitment.captcha.urllib.request.urlopen",
                return_value=self.fake_turnstile_response(True),
            ) as urlopen:
                is_valid = validate_turnstile_token(None, "valid-turnstile-token")

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["secret"], ["configured-secret"])
        self.assertEqual(payload["response"], ["valid-turnstile-token"])
        self.assertNotIn("remoteip", payload)

    def test_turnstile_forwards_public_client_ip(self):
        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(True),
        ) as urlopen:
            is_valid = validate_turnstile_token(
                SimpleNamespace(
                    META={
                        "HTTP_X_FORWARDED_FOR": "8.8.8.8, 10.0.0.1",
                        "REMOTE_ADDR": "10.0.0.1",
                    }
                ),
                "valid-turnstile-token",
            )

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["remoteip"], "8.8.8.8")

    def test_turnstile_worker_failure_blocks_internal_login_challenge(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(False),
        ):
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "cf-turnstile-response": "invalid-turnstile-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_turnstile_rejects_unallowed_verify_host_before_network_call(self):
        with self.settings(TURNSTILE_VERIFY_URL="https://evil.example/turnstile"):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_turnstile_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()

    def test_turnstile_rejects_non_https_verify_url_before_network_call(self):
        with self.settings(
            TURNSTILE_VERIFY_URL="http://turnstile-siteverify.example.workers.dev"
        ):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_turnstile_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="recaptcha",
    RECAPTCHA_SITE_KEY="test-recaptcha-site-key",
    RECAPTCHA_SECRET_KEY="test-recaptcha-secret-key",
    RECAPTCHA_TIMEOUT_SECONDS=5,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class RecaptchaCaptchaTests(TestCase):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_recaptcha_response(self, success):
        payload = json.dumps({"success": success}).encode("utf-8")
        return self.FakeResponse(payload)

    def test_recaptcha_widget_renders_on_internal_login(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, "https://www.google.com/recaptcha/api.js")
        self.assertContains(response, 'class="g-recaptcha"')
        self.assertContains(response, 'data-sitekey="test-recaptcha-site-key"')
        self.assertContains(response, 'data-callback="rgCaptchaSuccess"')

    def test_recaptcha_success_allows_internal_login_challenge(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(True),
        ) as urlopen:
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "g-recaptcha-response": "valid-recaptcha-token",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://www.google.com/recaptcha/api/siteverify",
        )
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["secret"], ["test-recaptcha-secret-key"])
        self.assertEqual(payload["response"], ["valid-recaptcha-token"])
        self.assertNotIn("remoteip", payload)

    def test_recaptcha_failure_blocks_internal_login_challenge(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(False),
        ):
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "g-recaptcha-response": "invalid-recaptcha-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_recaptcha_forwards_public_client_ip(self):
        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(True),
        ) as urlopen:
            is_valid = validate_recaptcha_token(
                SimpleNamespace(
                    META={
                        "HTTP_X_FORWARDED_FOR": "8.8.8.8, 10.0.0.1",
                        "REMOTE_ADDR": "10.0.0.1",
                    }
                ),
                "valid-recaptcha-token",
            )

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["remoteip"], ["8.8.8.8"])

    def test_recaptcha_without_secret_fails_closed(self):
        with self.settings(RECAPTCHA_SECRET_KEY=""):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_recaptcha_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()


@override_settings(RATE_LIMIT_ENABLED=False)
class OutputEncodingTests(TestCase):
    def test_shared_form_field_template_escapes_help_text(self):
        class UnsafeHelpTextForm(forms.Form):
            name = forms.CharField(help_text="<script>alert(1)</script>")

        rendered = render_to_string(
            "recruitment/includes/form_field.html",
            {"field": UnsafeHelpTextForm()["name"]},
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)


@override_settings(
    CAPTCHA_ENABLED=False,
    RATE_LIMIT_ENABLED=True,
    RATE_LIMIT_RULES={"default": {"limit": 2, "window": 60}},
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "rate-limit-test-cache",
        }
    },
)
class RateLimitMiddlewareTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_default_rate_limit_returns_429_after_threshold(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-RateLimit-Limit"], "2")
        self.assertEqual(response["X-RateLimit-Remaining"], "0")
        self.assertIn("Retry-After", response)


@override_settings(RATE_LIMIT_ENABLED=False)
class LoggingRedactionTests(TestCase):
    def test_sensitive_values_are_redacted_from_log_text(self):
        from config.logging import redact_sensitive_text

        redacted = redact_sensitive_text(
            "password=hunter2 otp:123456 token='abc123' Authorization: Bearer secret-token"
        )

        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("123456", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret-token", redacted)


class IdentityAdministrationTests(BaseRecruitmentTestCase):
    def test_internal_user_create_form_exposes_password_strength_meter(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("internal-user-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-password-strength="true"')

    def test_django_admin_requires_true_superuser(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get("/admin/")

        self.assertNotEqual(response.status_code, 200)

        superuser = User.objects.create_superuser(
            username="root-admin",
            password="testpass123",
            email="root-admin@example.com",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        client.force_login(superuser)

        response = client.get("/admin/")

        self.assertEqual(response.status_code, 200)

    def test_internal_email_change_requires_new_email_verification(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.post(
            reverse("internal-user-update", kwargs={"pk": self.secretariat.pk}),
            {
                "username": self.secretariat.username,
                "first_name": self.secretariat.first_name,
                "last_name": self.secretariat.last_name,
                "email": "new-secretariat@example.com",
                "employee_id": self.secretariat.employee_id,
                "office_name": self.secretariat.office_name,
                "role": self.secretariat.role,
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-user-list"))
        self.secretariat.refresh_from_db()
        self.assertEqual(self.secretariat.email, "secretariat@example.com")
        change_request = InternalEmailChangeRequest.objects.get(user=self.secretariat)
        self.assertEqual(change_request.new_email, "new-secretariat@example.com")
        self.assertEqual(len(mail.outbox), 1)
        verify_path = re.search(
            r"http://testserver(?P<path>/internal/users/email-change/[^\s]+/verify/)",
            mail.outbox[0].body,
        ).group("path")

        response = client.get(verify_path)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("login"))
        self.secretariat.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(self.secretariat.email, "new-secretariat@example.com")
        self.assertTrue(change_request.is_verified)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED).exists()
        )

    def test_password_history_blocks_recent_password_reuse(self):
        self.secretariat.set_password("OriginalSecurePass123!")
        self.secretariat.save(update_fields=["password"])
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("password-change"),
            {
                "old_password": "OriginalSecurePass123!",
                "new_password1": "DifferentSecurePass123!",
                "new_password2": "DifferentSecurePass123!",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-change-done"))
        self.secretariat.refresh_from_db()
        self.assertTrue(self.secretariat.check_password("DifferentSecurePass123!"))
        self.assertGreaterEqual(
            InternalPasswordHistory.objects.filter(user=self.secretariat).count(),
            2,
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("password-change"),
            {
                "old_password": "DifferentSecurePass123!",
                "new_password1": "OriginalSecurePass123!",
                "new_password2": "OriginalSecurePass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not been used recently")

    def test_system_admin_can_create_internal_account(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.sysadmin)
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
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_system_admin_role_does_not_inherit_django_admin_access(self):
        self.sysadmin.refresh_from_db()
        self.assertFalse(self.sysadmin.is_staff)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
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

    def test_position_posting_salary_grade_display_uses_reference(self):
        self.admin_aide_position.salary_grade = 7
        self.admin_aide_position.save(update_fields=["salary_grade", "updated_at"])
        self.level1_position.refresh_from_db()

        self.assertEqual(self.level1_position.salary_grade_display, "SG 7")

        entry_without_reference = PositionPosting(
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        self.assertEqual(entry_without_reference.salary_grade_display, "")

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
        self.force_login_with_mfa(client, self.sysadmin)
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
        self.force_login_with_mfa(client, self.hrm_chief)

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

    def test_plantilla_entry_uses_fourteen_calendar_day_publication_period(self):
        publication_date = date(2026, 6, 1)
        expected_closing_date = date(2026, 6, 14)
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            item_number="OSEC-DOH-AA6-1-2026",
            publication_date=publication_date,
            opening_date=publication_date,
            closing_date=expected_closing_date,
        )

        entry.full_clean()

        self.assertEqual(entry.expected_plantilla_closing_date, expected_closing_date)

    def test_plantilla_entry_allows_custom_publication_period(self):
        publication_date = date(2026, 6, 1)
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            item_number="OSEC-DOH-AA6-1-2026",
            publication_date=publication_date,
            opening_date=publication_date,
            closing_date=publication_date + timedelta(days=30),
        )

        # The 14-day window is a default, not a cap; a longer period is accepted.
        entry.full_clean()

        self.assertEqual(entry.closing_date, publication_date + timedelta(days=30))

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
        self.force_login_with_mfa(client, self.secretariat)

        create_response = client.get(reverse("recruitment-entry-create"))
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, 'id="id_job_code"')
        self.assertContains(create_response, 'placeholder="Will be generated automatically after first save"')
        self.assertContains(
            create_response,
            "Generated automatically for tracking. This cannot be edited.",
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
        self.force_login_with_mfa(client, self.secretariat)
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

    def test_entry_manager_can_create_plantilla_entry_with_auto_closing_date(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        opening_date = self.entry_opening_date()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "item_number": "OSEC-DOH-AA6-1-2026",
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": opening_date.isoformat(),
                "closing_date": "",
                "qualification_reference": "Fourteen-day Plantilla publication window.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.get(
            qualification_reference="Fourteen-day Plantilla publication window."
        )
        self.assertEqual(
            created_entry.closing_date,
            PositionPosting.calculate_plantilla_closing_date(opening_date),
        )

    def test_entry_manager_can_set_custom_plantilla_publication_period(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        opening_date = self.entry_opening_date()
        closing_date = opening_date + timedelta(days=30)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "item_number": "OSEC-DOH-AA6-1-2026",
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": opening_date.isoformat(),
                "closing_date": closing_date.isoformat(),
                "qualification_reference": "Custom Plantilla publication window.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.get(
            qualification_reference="Custom Plantilla publication window."
        )
        self.assertEqual(created_entry.closing_date, closing_date)

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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.assertContains(response, "Entry Code is generated automatically for tracking and cannot be edited.")
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
        self.force_login_with_mfa(client, self.secretariat)
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

    def test_incomplete_position_reference_does_not_invent_routing_metadata(self):
        incomplete_reference = PositionReference.objects.create(
            position_title="Incomplete Reference",
            reference_status=PositionReference.ReferenceStatus.INCOMPLETE_REFERENCE,
            is_active=True,
            position_code="POS-999",
            notes="Synthetic incomplete record for test coverage.",
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
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
        self.assertContains(
            response,
            "This position reference is missing the level classification needed for assignment.",
        )
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_non_entry_manager_cannot_access_entry_management(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.get(reverse("recruitment-entry-list"))
        self.assertEqual(response.status_code, 403)

    def test_entry_status_change_is_audited(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
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

    def test_closed_entry_is_removed_from_management_list(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.CLOSED},
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.CLOSED)
        self.assertFalse(self.level1_position.is_active)
        self.assertNotContains(response, self.level1_position.job_code)
        self.assertContains(response, self.level2_position.job_code)

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
        self.force_login_with_mfa(client, self.hrm_chief)
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

    def intake_field_validation_form(self, **overrides):
        data = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "email": "portal.validation@example.com",
            "phone": "09171234567",
            "qualification_summary": "Qualified applicant.",
            "performance_rating_applicability": (
                RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
            "checklist_privacy_consent": "on",
            "checklist_documents_complete": "on",
            "checklist_information_certified": "on",
        }
        data.update(overrides)
        form = ApplicantPortalIntakeForm(data=data, entry=self.level1_position)
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
        self.post_portal_intake(
            self.client,
            self.level1_position,
            self.portal_payload(
                email="normalized.mobile@example.com",
                phone="(0917) 123-4567",
            ),
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
        self.assertContains(response, "The verification code is valid for 12 minutes.")

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
        response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["max_upload_bytes"], 5 * 1024 * 1024)
        self.assertEqual(response.context["max_upload_mb"], 5)
        self.assertContains(response, 'data-max-upload-bytes="5242880"')
        self.assertContains(response, "data-file-selection")
        self.assertContains(response, "data-file-clear")
        self.assertContains(response, "Remove selected file")
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
        self.assertContains(detail_response, "Applications are not currently open for this position")

        intake_response = self.client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk})
        )
        self.assertEqual(intake_response.status_code, 404)

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
        self.assertEqual(intake_response.status_code, 404)

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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.assertContains(response, "Email verification is required before final submission.")
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
        self.assertContains(response, f"?token={application.public_token}")
        self.assertContains(response, 'id="rg-otp-submit-confirm"')
        self.assertContains(response, 'form="rg-otp-submit-form"')

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

    def test_verified_otp_summary_counts_only_current_active_applicant_documents(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="document.count@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="document.count@example.com",
        )
        first_document = application.evidence_items.filter(
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type="applicant_document",
            document_key="signed_cover_letter",
            is_current_version=True,
            is_archived=False,
        ).get()
        upload_evidence_item(
            application=application,
            actor=application.applicant,
            label=first_document.label,
            uploaded_file=self.build_valid_applicant_document_upload(
                "signed_cover_letter",
                content_prefix="replacement",
            ),
            document_key="signed_cover_letter",
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type="applicant_document",
        )
        application.evidence_items.filter(
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type="applicant_document",
            document_key="performance_rating",
        ).update(is_archived=True, archive_tag="Marked not applicable by applicant")
        application.performance_rating_applicability = (
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
        )
        application.save(update_fields=["performance_rating_applicability", "updated_at"])

        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        response = client.get(reverse("applicant-otp", kwargs={"token": application.public_token}))

        self.assertContains(response, "8 files uploaded")
        self.assertNotContains(response, "10 files uploaded")

        receipt_response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(receipt_response, "8 files submitted")
        self.assertNotContains(receipt_response, "10 files submitted")

    def test_otp_delivery_failure_keeps_saved_draft_without_server_error(self):
        client = Client()

        with self.assertLogs("recruitment.services", level="ERROR"):
            with patch(
                "recruitment.services.EmailMultiAlternatives.send",
                side_effect=PermissionError("denied"),
            ):
                response = self.post_portal_intake(
                    client,
                    self.level1_position,
                    self.portal_payload(email="otp.delivery.fail@example.com"),
                )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not send the verification code right now")
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="otp.delivery.fail@example.com",
        )
        self.assertTrue(application.otp_hash)
        self.assertIsNone(application.submitted_at)
        form = response.context["form"]
        self.assertTrue(form.saved_draft_notice)
        self.assertIn("signed_cover_letter", form.existing_documents_by_code)

    def test_intake_get_rehydrates_existing_draft_from_token(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(
                email="rehydrate.draft@example.com",
                first_name="Maria",
                last_name="Santos",
                phone="09991234567",
                qualification_summary="Saved qualification summary.",
                cover_letter="Saved cover letter note.",
                performance_rating_applicability=(
                    RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
                ),
            ),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="rehydrate.draft@example.com",
        )

        response = client.get(
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
            {"token": application.public_token},
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["first_name"].value(), "Maria")
        self.assertEqual(form["last_name"].value(), "Santos")
        self.assertEqual(form["email"].value(), "rehydrate.draft@example.com")
        self.assertEqual(form["phone"].value(), "+639991234567")
        self.assertEqual(form["qualification_summary"].value(), "Saved qualification summary.")
        self.assertEqual(form["cover_letter"].value(), "Saved cover letter note.")
        self.assertEqual(
            form["performance_rating_applicability"].value(),
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE,
        )
        self.assertTrue(form["checklist_privacy_consent"].value())
        self.assertTrue(form.saved_draft_notice)
        self.assertIn("signed_cover_letter", form.existing_documents_by_code)

    def test_intake_reuses_identity_draft_when_application_email_drifted(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="identity.old@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="identity.old@example.com",
        )
        original_token = application.public_token
        application.applicant.email = "identity.new@example.com"
        application.applicant.save(update_fields=["email"])

        response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="identity.new@example.com"),
            follow=True,
        )

        application.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            RecruitmentApplication.objects.filter(position=self.level1_position).count(),
            1,
        )
        self.assertEqual(application.public_token, original_token)
        self.assertEqual(application.applicant_email, "identity.new@example.com")
        self.assertContains(response, "Resend the code")

    def test_intake_blocks_submitted_identity_duplicate_without_server_error(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="submitted.old@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="submitted.old@example.com",
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        application.refresh_from_db()
        application.applicant.email = "submitted.new@example.com"
        application.applicant.save(update_fields=["email"])

        response = self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="submitted.new@example.com"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "You have already submitted an application for this position using this email address.",
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(position=self.level1_position).count(),
            1,
        )

    def test_intake_get_ignores_invalid_or_mismatched_draft_token(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="wrong.position.token@example.com"),
            follow=True,
        )
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
        self.assertFalse(form.saved_draft_notice)

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
        self.assertContains(
            receipt_response,
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
        )
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

    def test_applicant_status_magic_link_renders_status_page(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.cos_position,
            self.portal_payload(position=self.cos_position, email="magic.status@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.cos_position,
            applicant_email="magic.status@example.com",
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
            follow=True,
        )
        client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        application.refresh_from_db()

        response = client.get(reverse("applicant-status-link", kwargs={"token": application.public_token}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, application.reference_number)
        self.assertContains(response, application.applicant_display_name)
        self.assertContains(response, "Application details")

    def test_unfinished_draft_status_link_redirects_to_otp(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="draft.status.link@example.com"),
            follow=True,
        )
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

    def test_status_lookup_for_single_unfinished_draft_redirects_to_otp(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="draft.status.lookup@example.com"),
            follow=True,
        )
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email="draft.status.lookup@example.com",
        )
        application.reference_number = "RG-20260528-DRAFT1"
        application.save(update_fields=["reference_number", "updated_at"])

        response = client.post(
            reverse("applicant-status-lookup"),
            {
                "application_id": "RG-20260528-DRAFT1",
                "email": "draft.status.lookup@example.com",
            },
            follow=True,
        )

        self.assertRedirects(
            response,
            reverse("applicant-otp", kwargs={"token": application.public_token}),
        )
        self.assertContains(response, "is not finished yet")

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
        self.assertContains(response, "The verification code is invalid.")
        self.assertIsNone(application.otp_verified_at)

    @override_settings(APPLICATION_OTP_MAX_ATTEMPTS=2)
    def test_applicant_otp_locks_after_too_many_invalid_attempts_until_resend(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="otp.lockout@example.com"),
            follow=True,
        )
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
        self.assertContains(response, "The verification code is invalid.")

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
        self.assertContains(response, "Email verified")

    def test_applicant_otp_resend_respects_server_cooldown(self):
        client = Client()
        self.post_portal_intake(
            client,
            self.level1_position,
            self.portal_payload(email="otp.cooldown@example.com"),
            follow=True,
        )
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
        self.assertEqual(
            vacancy_response.context["document_requirements"][-1].code,
            "performance_rating",
        )

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
        performance_slot = next(
            slot
            for slot in response.context["form"].document_slots
            if slot["requirement"].code == "performance_rating"
        )
        self.assertTrue(performance_slot["is_required_now"])

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

    def test_performance_rating_not_applicable_archives_saved_rating_evidence(self):
        client = Client()
        applicant_email = "saved.rating.not.applicable@example.com"
        duplicate_bytes = self.build_valid_applicant_document_bytes(
            "duplicate-rating",
            content_prefix="same-file",
        )
        first_payload = self.portal_payload(email=applicant_email)
        first_payload["performance_rating"] = SimpleUploadedFile(
            "performance-rating.pdf",
            duplicate_bytes,
            content_type="application/pdf",
        )

        self.post_portal_intake(client, self.level1_position, first_payload, follow=True)
        application = RecruitmentApplication.objects.get(
            position=self.level1_position,
            applicant_email=applicant_email,
        )
        self.assertTrue(
            application.evidence_items.filter(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
                document_key="performance_rating",
                is_current_version=True,
                is_archived=False,
            ).exists()
        )

        second_payload = self.portal_payload(
            email=applicant_email,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        second_payload["diploma"] = SimpleUploadedFile(
            "diploma.pdf",
            duplicate_bytes,
            content_type="application/pdf",
        )

        response = self.post_portal_intake(
            client,
            self.level1_position,
            second_payload,
            follow=True,
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
        self.assertNotContains(response, "Performance Rating in the last rating period")

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
        self.assertContains(response, "Saved to this draft")

    def test_empty_portal_submission_returns_validation_errors_without_crashing(self):
        client = Client()

        response = self.post_portal_intake(
            client,
            self.level1_position,
            {},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please fix the highlighted fields below.")
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

    def test_duplicate_applicant_document_file_blocks_intake(self):
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
            "The same file cannot be used for multiple document slots",
        )
        self.assertContains(response, "rg-pub-upload-slot--error")
        self.assertFalse(mail.outbox)
        application = RecruitmentApplication.objects.filter(
            position=self.level1_position,
            applicant_email="duplicate.file@example.com",
        ).first()
        if application is not None:
            self.assertFalse(application.otp_hash)

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
            "Each document slot must use a different file before final submission.",
        ):
            submit_application(application, self.applicant)

        application.refresh_from_db()
        self.assertIsNone(application.submitted_at)

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
        self.assertContains(response, "The verification code has expired.")

        application.refresh_from_db()
        application.otp_verified_at = timezone.now() - timedelta(minutes=2)
        application.save(update_fields=["otp_verified_at", "updated_at"])
        response = client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "finalize"},
            follow=True,
        )
        self.assertContains(response, "Your email verification has expired.")
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
            "recruitment.services.render_to_string",
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
            "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat.",
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

    def test_case_handoff_modal_shows_fixed_destination_for_single_target(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Send to HRM Chief", content)
        self.assertIn('type="hidden" name="target_role" value="hrm_chief"', content)
        self.assertNotIn('<select name="target_role"', content)

    def test_case_handoff_form_collapses_single_authorized_target(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        form = CaseHandoffForm(application=application, user=self.hrm_chief)

        self.assertTrue(form.target_role_is_fixed)
        self.assertEqual(form.target_role_fixed_label, "Send to Secretariat")
        self.assertEqual(form.fields["target_role"].widget.input_type, "hidden")
        self.assertEqual(form["target_role"].value(), RecruitmentUser.Role.SECRETARIAT)

    def test_workflow_action_form_collapses_single_authorized_action(self):
        application = self.make_application(self.level1_position)
        with patch(
            "recruitment.forms.get_available_actions",
            return_value=[("return_to_hrm_chief", "Return to HRM Chief")],
        ):
            form = WorkflowActionForm(application=application, user=self.appointing)

        self.assertTrue(form.action_is_fixed)
        self.assertEqual(form.action_fixed_label, "Return to HRM Chief")
        self.assertEqual(form.fields["action"].widget.input_type, "hidden")
        self.assertEqual(form["action"].value(), "return_to_hrm_chief")

        html = render_to_string(
            "internal_includes/workflow_action_card.html",
            {"action_form": form, "application": application},
        )
        self.assertIn("Return to HRM Chief", html)
        self.assertIn('type="hidden" name="action" value="return_to_hrm_chief"', html)
        self.assertNotIn('<select name="action"', html)

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
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_legacy_override_endpoint_rejects_unauthorized_case_before_processing(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("workflow-override", kwargs={"pk": application.pk}),
            {"reason": "Unauthorized probing."},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(WorkflowOverride.objects.filter(application=application).exists())

    def test_override_allows_secretariat_processing_of_level2(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("case-handoff", kwargs={"pk": application.pk}),
            {
                "target_role": RecruitmentUser.Role.SECRETARIAT,
                "remarks": "Controlled screening support.",
            },
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            screening_notes="Override-backed screening completed.",
        )
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
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
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.OVERRIDE_USED,
            ).exists()
        )

    def test_secretariat_can_return_handed_off_level2_case_to_hrm_chief(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("case-handoff", kwargs={"pk": application.pk}),
            {
                "target_role": RecruitmentUser.Role.SECRETARIAT,
                "remarks": "Controlled screening support.",
            },
        )
        self.assertEqual(response.status_code, 302)

        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("case-handoff", kwargs={"pk": application.pk}),
            {
                "target_role": RecruitmentUser.Role.HRM_CHIEF,
                "remarks": "Returning to HRM Chief.",
            },
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

    def test_legacy_cos_authority_review_can_return_to_hrm_chief(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)

        case = application.case
        application.current_handler_role = RecruitmentUser.Role.APPOINTING_AUTHORITY
        application.status = RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])
        case.current_stage = RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        case.current_handler_role = RecruitmentUser.Role.APPOINTING_AUTHORITY
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.save(
            update_fields=[
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "updated_at",
            ]
        )

        self.assertEqual(get_current_workflow_section(application), "decision")
        self.assertEqual(
            get_available_actions(application, self.appointing),
            [("return_to_hrm_chief", "Return to HRM Chief")],
        )

        process_workflow_action(
            application,
            self.appointing,
            "return_to_hrm_chief",
            "Return legacy COS case to HRM Chief.",
        )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)

    def test_repair_auto_advance_moves_stale_exam_boundary_case(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        case = application.case
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])
        case.current_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        case.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.save(
            update_fields=[
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "updated_at",
            ]
        )

        self.assertEqual(get_available_actions(application, self.secretariat), [])
        repaired = repair_auto_advance_workflow_boundaries(actor=None)

        application.refresh_from_db()
        self.assertEqual(len(repaired), 1)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

    def test_repair_auto_advance_moves_stale_car_boundary_case(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        application.refresh_from_db()
        case = application.case
        application.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        application.status = RecruitmentApplication.Status.HRMPSB_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])
        case.current_stage = RecruitmentCase.Stage.HRMPSB_REVIEW
        case.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.save(
            update_fields=[
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "updated_at",
            ]
        )

        self.assertEqual(get_available_actions(application, self.hrmpsb), [])
        repaired = repair_auto_advance_workflow_boundaries(actor=None)

        application.refresh_from_db()
        self.assertEqual(len(repaired), 1)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)

    def test_secretariat_cannot_view_selected_level2_case_without_authorized_basis(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Select Level 2 applicant for HRM Chief completion tracking.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(user_can_view_application(self.secretariat, application))

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_is_limited_to_active_hrm_chief_review_stage(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Level 2 Secretariat authorization is only available while the case is assigned to HRM Chief review.",
        ):
            grant_secretariat_override(
                application=application,
                actor=self.hrm_chief,
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
        self.assertNotIn(
            ("endorse", "Endorse to Appointing Authority"),
            get_available_actions(application, self.hrm_chief),
        )

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
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.finalize_car_for_current_stage(application, self.hrmpsb)
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

    def test_stage_entered_at_updates_when_case_enters_new_stage(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        old_stage_entered_at = timezone.now() - timedelta(days=2)
        application.case.stage_entered_at = old_stage_entered_at
        application.case.save(update_fields=["stage_entered_at", "updated_at"])

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertGreater(application.case.stage_entered_at, old_stage_entered_at)
        self.assertGreater(application.case.time_in_current_stage, timedelta(seconds=0))

    def test_stage_sla_state_uses_default_thresholds_and_pauses_returned_cases(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        case = application.case

        case.stage_entered_at = timezone.now() - timedelta(days=4, minutes=59)
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.save(update_fields=["stage_entered_at", "case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "ok")

        case.stage_entered_at = timezone.now() - timedelta(days=5)
        case.save(update_fields=["stage_entered_at", "updated_at"])
        self.assertEqual(case.stage_sla_state, "warning")
        self.assertTrue(case.stage_sla_context["is_warning"])

        case.stage_entered_at = timezone.now() - timedelta(days=7)
        case.save(update_fields=["stage_entered_at", "updated_at"])
        self.assertEqual(case.stage_sla_state, "overdue")
        self.assertTrue(case.stage_sla_context["is_overdue"])

        case.case_status = RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT
        case.save(update_fields=["case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "paused")
        self.assertTrue(case.stage_sla_context["is_paused"])

        case.current_stage = RecruitmentCase.Stage.CLOSED
        case.case_status = RecruitmentCase.CaseStatus.APPROVED
        case.save(update_fields=["current_stage", "case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "ok")

    def test_closed_case_is_locked_after_completion_and_can_be_reopened(self):
        application = self.make_selected_application(self.cos_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
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

        self.force_login_with_mfa(client, self.hrm_chief)
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
        self.force_login_with_mfa(client, self.secretariat)
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

    def test_application_detail_uses_compressed_pipeline_and_expanded_applicant_panel(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Step 1 of 6')
        self.assertContains(response, '<details class="rg-applicant-panel" open>')
        self.assertContains(response, "Applicant Profile &amp; Submission")
        for label in ["Screening", "Exam", "Interview", "Deliberation", "Decision", "Completion"]:
            self.assertContains(
                response,
                f'<span class="rg-pipeline__label">{label}</span>',
            )
        for removed_label in ["Publication", "Intake", "Submission", "Appointment", "Archive", "Deliberation/CAR"]:
            self.assertNotContains(
                response,
                f'<span class="rg-pipeline__label">{removed_label}</span>',
            )

    def test_application_detail_shows_only_the_current_task_tab(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Qualification Screening</span>')
        self.assertNotContains(response, 'data-section="cws-exam"')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-actions"')

    def test_cos_screening_checklist_uses_cos_document_requirements(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        match = re.search(
            r'(?s)<div class="rg-cws-header__facts"[^>]*>(.*?)</div>\s*<div class="rg-cws-header__actions">',
            content,
        )
        self.assertIsNotNone(match)
        header_facts = match.group(1)
        self.assertIn("Plantilla", header_facts)
        self.assertIn("Level 2", header_facts)
        self.assertIn("Assigned to HRM Chief", header_facts)
        self.assertNotIn("HRM Chief Review", header_facts)
        self.assertNotIn("rg-pill", header_facts)

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
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Examination</span>')
        self.assertContains(response, "Step 2 of 6")
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
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Qualification Screening</span>')
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
        self.force_login_with_mfa(client, self.hrm_chief)
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

    def test_definitive_not_qualified_screening_auto_rejects_and_notifies(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                completeness_status=ScreeningRecord.CompletenessStatus.COMPLETE,
                qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                screening_notes="Does not meet the education requirement under the QS.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        )
        self.assertTrue(notification.metadata.get("cut_at_screening"))
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertIn("Qualification Standards", notification.body)

    def test_future_stage_posts_are_blocked_until_the_current_task_is_finalized(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        exam_response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "85.00",
                "exam_result": "",
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

    def test_screening_view_persists_document_review_statuses(self):
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
                        first_requirement.code: ScreeningDocumentReview.ReviewStatus.NEEDS_REVIEW,
                    },
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        document_review = screening_record.document_reviews.get(document_key=first_requirement.code)
        self.assertEqual(document_review.status, ScreeningDocumentReview.ReviewStatus.NEEDS_REVIEW)
        self.assertEqual(
            screening_record.document_reviews.count(),
            len(get_applicant_document_requirements(application)),
        )

    def test_screening_view_persists_document_resubmission_request_and_remarks(self):
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
                            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening finalized and locked.")
        screening_record = ScreeningRecord.objects.get(application=application)
        document_review = screening_record.document_reviews.get(document_key=first_requirement.code)
        self.assertEqual(
            document_review.status,
            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION,
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
                            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
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

    def test_screening_view_blocks_complete_when_required_document_needs_review(self):
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
                        first_requirement.code: ScreeningDocumentReview.ReviewStatus.NEEDS_REVIEW,
                    },
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Required documents must be marked Meets before using Complete")
        self.assertFalse(ScreeningRecord.objects.filter(application=application, is_finalized=True).exists())

    def test_screening_view_blocks_qualified_when_completeness_is_incomplete(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(
                    completeness_status=ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    completeness_notes="Missing or deficient document detected.",
                ),
                **self.screening_document_status_payload(application),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be marked Qualified")
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


class ExamRecordTests(BaseRecruitmentTestCase):
    def exam_payload(self, **overrides):
        payload = {
            "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            "exam_status": ExamRecord.ExamStatus.COMPLETED,
            "exam_result": "",
            "technical_score": "84.50",
            "technical_result": "",
            "general_score": "89.00",
            "general_result": "",
            "exam_date": timezone.localdate().isoformat(),
            "administered_by": ExamRecord.AdministeredBy.HRMS,
            "valid_from": timezone.localdate().isoformat(),
            "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
            "exam_notes": "Validated through the current review stage.",
        }
        payload.update(overrides)
        return payload

    def test_exam_form_displays_single_policy_exam_type_as_fixed_value(self):
        level1_application = self.make_application(self.level1_position)
        level1_form = ExamRecordForm(application=level1_application)

        self.assertTrue(level1_form.exam_type_is_fixed)
        self.assertEqual(
            level1_form.exam_type_fixed_label,
            ExamRecord.ExamType.TECHNICAL_PRACTICAL.label,
        )
        self.assertEqual(
            level1_form["exam_type"].value(),
            ExamRecord.ExamType.TECHNICAL_PRACTICAL,
        )
        self.assertEqual(level1_form.fields["exam_type"].widget.input_type, "hidden")
        self.assertTrue(level1_form.administered_by_is_fixed)
        self.assertEqual(
            level1_form.administered_by_fixed_label,
            ExamRecord.AdministeredBy.HRMS.label,
        )

        cos_application = self.make_application(self.cos_position)
        cos_form = ExamRecordForm(application=cos_application)
        self.assertTrue(cos_form.exam_type_is_fixed)
        self.assertEqual(
            cos_form.exam_type_fixed_label,
            ExamRecord.ExamType.END_USER_ASSESSMENT.label,
        )
        self.assertTrue(cos_form.administered_by_is_fixed)
        self.assertEqual(
            cos_form.administered_by_fixed_label,
            ExamRecord.AdministeredBy.END_USER.label,
        )

    def test_exam_wizard_reveals_conditional_fields_for_outcome_validation(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rg-wizard-validation.js?v=20260624-val2c")
        self.assertContains(
            response,
            'condCompleted.classList.toggle("is-visible", showCompleted);',
        )
        self.assertContains(
            response,
            'condRemarks.classList.toggle("is-visible", showRemarks);',
        )

    def test_exam_form_rejects_scores_outside_allowed_range(self):
        application = self.make_application(self.level1_position)
        form = ExamRecordForm(
            data=self.exam_payload(
                technical_score="-1",
                general_score="125",
            ),
            application=application,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(ExamRecordForm.SCORE_RANGE_MESSAGE, form.errors["technical_score"])
        self.assertIn(ExamRecordForm.SCORE_RANGE_MESSAGE, form.errors["general_score"])

    def test_current_handler_can_create_update_and_finalize_exam_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "82.50",
                "exam_result": "",
                "technical_score": "80.00",
                "technical_result": "",
                "general_score": "85.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
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
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "90.00",
                "exam_result": "",
                "technical_score": "88.00",
                "technical_result": "",
                "general_score": "92.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Updated before finalization.",
            },
            finalize=True,
        )
        exam_record.refresh_from_db()
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "90.40")
        self.assertEqual(str(exam_record.technical_score), "88.00")
        self.assertEqual(str(exam_record.general_score), "92.00")
        self.assertEqual(exam_record.exam_date, timezone.localdate())
        self.assertEqual(exam_record.administered_by, ExamRecord.AdministeredBy.HRMS)
        self.assertEqual(
            exam_record.component_summary,
            "Technical: 88.00 (Recorded for evaluation); General Ability: 92.00 (Recorded for evaluation)",
        )
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
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": "82.00",
                "technical_result": "",
                "general_score": "88.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Structured components recorded without an overall score.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "85.60")
        self.assertEqual(str(exam_record.effective_score), "85.60")

    def test_exam_record_can_attach_optional_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "91.00",
                "exam_result": "",
                "technical_score": "90.00",
                "technical_result": "",
                "general_score": "92.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
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

    def test_exam_evidence_rejects_active_content_upload(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        with self.assertRaisesMessage(
            ValueError,
            "This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "91.00",
                    "exam_result": "",
                    "technical_score": "90.00",
                    "technical_result": "",
                    "general_score": "92.00",
                    "general_result": "",
                    "exam_date": timezone.localdate(),
                    "administered_by": ExamRecord.AdministeredBy.HRMS,
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=365),
                    "exam_notes": "Active content should not be stored.",
                },
                evidence_file=SimpleUploadedFile(
                    "exam-result.html",
                    b"<html><script>alert(1)</script></html>",
                    content_type="text/html",
                ),
                finalize=True,
            )

        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                application=application,
                artifact_type="exam_supporting_evidence",
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

        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for exam details.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "70.00",
                    "exam_result": "",
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=30),
                    "exam_notes": "Should not save.",
                },
                finalize=False,
            )

        exam_record.refresh_from_db()
        self.assertEqual(str(exam_record.exam_score), "88.50")
        self.assertEqual(exam_record.exam_result, ExamRecord.OverallResult.FOR_EVALUATION)

    def test_cos_exam_waiver_can_be_finalized_without_score_or_validity(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": None,
                "technical_result": "",
                "general_score": None,
                "general_result": "",
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
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": None,
                    "exam_result": "",
                    "technical_score": None,
                    "technical_result": "",
                    "general_score": None,
                    "general_result": "",
                    "exam_date": timezone.localdate(),
                    "administered_by": ExamRecord.AdministeredBy.HRMS,
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
        self.force_login_with_mfa(client, self.hrmpsb)
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
        self.force_login_with_mfa(client, self.secretariat)
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

    def test_exam_view_invalid_scores_show_readable_message_and_do_not_finalize(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                **self.exam_payload(
                    technical_score="120",
                    general_score="-1",
                ),
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        message_text = " ".join(message.message for message in get_messages(response.wsgi_request))
        self.assertIn("Technical Score", message_text)
        self.assertIn("General Ability Score", message_text)
        self.assertIn(ExamRecordForm.SCORE_RANGE_MESSAGE, message_text)
        self.assertNotIn("{'exam_score'", message_text)
        self.assertFalse(ExamRecord.objects.filter(application=application).exists())

    def test_exam_autosave_accepts_partial_draft_without_audit_or_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "",
                "exam_result": "",
                "technical_score": "",
                "technical_result": "",
                "general_score": "",
                "general_result": "",
                "exam_date": "",
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": "",
                "valid_until": "",
                "exam_notes": "Autosave captured a partial exam draft.",
                "operation": "save",
                "evidence_file": SimpleUploadedFile(
                    "autosave-exam.pdf",
                    b"%PDF-1.4\nautosave should not upload",
                    content_type="application/pdf",
                ),
            },
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 204)
        exam_record = ExamRecord.objects.get(application=application)
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.COMPLETED)
        self.assertIsNone(exam_record.technical_score)
        self.assertIsNone(exam_record.general_score)
        self.assertEqual(exam_record.exam_result, ExamRecord.OverallResult.INCOMPLETE)
        self.assertIsNone(exam_record.evidence_item)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_RECORDED,
            ).exists()
        )
        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                application=application,
                artifact_type="exam_supporting_evidence",
            ).exists()
        )

    def test_exam_autosave_rejects_invalid_scores_without_creating_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                **self.exam_payload(technical_score="101"),
                "operation": "save",
            },
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ExamRecord.objects.filter(application=application).exists())

    def test_exam_finalize_rerenders_wizard_with_bound_field_errors(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                **self.exam_payload(
                    technical_score="",
                    general_score="",
                ),
                "operation": "finalize",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter this required exam score.")
        self.assertContains(response, "invalid-feedback")
        self.assertContains(response, "exam-form")
        self.assertFalse(ExamRecord.objects.filter(application=application).exists())

    def test_waived_exam_clears_hidden_score_and_validity_values(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                **self.exam_payload(
                    exam_status=ExamRecord.ExamStatus.WAIVED,
                    exam_notes="Waived by authorized office control.",
                ),
                "operation": "finalize",
            },
        )

        self.assertEqual(response.status_code, 302)
        exam_record = ExamRecord.objects.get(application=application)
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.WAIVED)
        self.assertIsNone(exam_record.exam_score)
        self.assertIsNone(exam_record.technical_score)
        self.assertIsNone(exam_record.general_score)
        self.assertIsNone(exam_record.valid_from)
        self.assertIsNone(exam_record.valid_until)

    def test_secretariat_cannot_record_level2_exam_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class VacancyAssessmentWeightsTests(BaseRecruitmentTestCase):
    def test_assessment_weights_or_default_returns_unsaved_defaults(self):
        # A vacancy with no weights row falls back to the office defaults (exam 60/40,
        # CAR 40/20/40) without persisting anything.
        weights = self.level1_position.assessment_weights_or_default
        self.assertIsNone(weights.pk)
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))
        self.assertEqual(weights.exam_technical_weight, Decimal("40.00"))
        self.assertEqual(weights.ete_weight, Decimal("40.00"))
        self.assertEqual(weights.exam_weight, Decimal("20.00"))
        self.assertEqual(weights.interview_weight, Decimal("40.00"))

    def test_get_or_create_persists_a_single_row(self):
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        self.assertIsNotNone(weights.pk)
        self.assertEqual(
            VacancyAssessmentWeights.objects.filter(
                recruitment_entry=self.level1_position
            ).count(),
            1,
        )
        # The reverse accessor now returns the persisted row.
        self.assertEqual(
            self.level1_position.assessment_weights_or_default.pk, weights.pk
        )

    def test_lock_marks_locked_and_is_idempotent(self):
        weights = lock_vacancy_assessment_weights(self.level1_position)
        self.assertTrue(weights.is_locked)
        self.assertIsNotNone(weights.locked_at)
        first_locked_at = weights.locked_at
        again = lock_vacancy_assessment_weights(self.level1_position)
        self.assertEqual(again.locked_at, first_locked_at)

    def test_exam_weights_must_sum_to_100(self):
        weights = VacancyAssessmentWeights(
            recruitment_entry=self.level1_position,
            exam_general_weight=Decimal("70.00"),
            exam_technical_weight=Decimal("40.00"),
        )
        with self.assertRaises(ValidationError):
            weights.full_clean()

    def test_car_weights_must_sum_to_100(self):
        weights = VacancyAssessmentWeights(
            recruitment_entry=self.level1_position,
            ete_weight=Decimal("50.00"),
            exam_weight=Decimal("30.00"),
            interview_weight=Decimal("30.00"),
        )
        with self.assertRaises(ValidationError):
            weights.full_clean()

    def test_new_plantilla_vacancy_seeds_default_weights(self):
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            item_number="OSEC-DOH-AA6-9-2026",
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        persist_recruitment_entry(entry, self.secretariat, [])
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=entry)
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.DRAFT)

    def test_new_cos_vacancy_has_no_weights_row(self):
        entry = PositionPosting(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.POOLING,
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        persist_recruitment_entry(entry, self.secretariat, [])
        self.assertFalse(
            VacancyAssessmentWeights.objects.filter(recruitment_entry=entry).exists()
        )

    def test_exam_score_uses_per_vacancy_default_weights(self):
        application = self.make_application(self.level1_position)
        record = ExamRecord(
            application=application,
            exam_type=ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            general_score=Decimal("90"),
            technical_score=Decimal("80"),
        )
        # 90 * 0.60 + 80 * 0.40 = 86 with the seeded defaults.
        self.assertEqual(record.calculate_policy_score(), Decimal("86.00"))

    def test_exam_score_follows_per_vacancy_weights(self):
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        weights.exam_general_weight = Decimal("50.00")
        weights.exam_technical_weight = Decimal("50.00")
        weights.full_clean()
        weights.save()
        application = self.make_application(self.level1_position)
        record = ExamRecord(
            application=application,
            exam_type=ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            general_score=Decimal("90"),
            technical_score=Decimal("80"),
        )
        # 90 * 0.50 + 80 * 0.50 = 85 — the formula tracks this vacancy's weights.
        self.assertEqual(record.calculate_policy_score(), Decimal("85.00"))

    def test_secretariat_can_view_and_update_vacancy_weights(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        self.assertEqual(client.get(url).status_code, 200)
        response = client.post(
            url,
            {
                "ete_weight": "50",
                "exam_weight": "20",
                "interview_weight": "30",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.ete_weight, Decimal("50.00"))
        self.assertEqual(weights.interview_weight, Decimal("30.00"))
        self.assertEqual(weights.exam_general_weight, Decimal("55.00"))
        self.assertEqual(weights.updated_by_id, self.secretariat.id)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.ASSESSMENT_WEIGHTS_UPDATED,
                actor=self.secretariat,
            ).exists()
        )

    def test_weights_page_requires_entry_manager_role(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        self.assertEqual(client.get(url).status_code, 403)

    def test_weights_view_rejects_sums_that_are_not_100(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {
                "ete_weight": "40",
                "exam_weight": "20",
                "interview_weight": "40",
                "exam_general_weight": "70",
                "exam_technical_weight": "40",
            },
        )
        self.assertEqual(response.status_code, 200)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        # Rejected — the stored weights stay at the seeded defaults.
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))

    def test_cos_vacancy_weights_page_redirects_without_creating_a_row(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.cos_position.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            VacancyAssessmentWeights.objects.filter(
                recruitment_entry=self.cos_position
            ).exists()
        )

    def test_locked_weights_cannot_be_edited_through_the_view(self):
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {
                "ete_weight": "50",
                "exam_weight": "20",
                "interview_weight": "30",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        # Refused — scoring has started, so the weights stay at the defaults.
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))

    def test_finalizing_an_exam_locks_the_vacancy_weights(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertTrue(weights.is_locked)


class VacancyBatchConsoleTests(BaseRecruitmentTestCase):
    def _submit(self, application):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def test_console_groups_secretariat_queue_by_vacancy(self):
        self._submit(self.make_application(self.level1_position))
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batches"))
        self.assertEqual(response.status_code, 200)
        entry_ids = {v["entry"].id for v in response.context["vacancies"]}
        self.assertIn(self.level1_position.id, entry_ids)

    def test_console_excludes_level2_vacancy_from_secretariat(self):
        # A Level-2 application routes to HRM Chief, so it must not appear in the
        # Secretariat's batch console (FRS Level-2 bar).
        self._submit(self.make_application(self.level2_position))
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batches"))
        entry_ids = {v["entry"].id for v in response.context["vacancies"]}
        self.assertNotIn(self.level2_position.id, entry_ids)

    def test_console_shows_level2_vacancy_to_hrm_chief(self):
        self._submit(self.make_application(self.level2_position))
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("vacancy-batches"))
        entry_ids = {v["entry"].id for v in response.context["vacancies"]}
        self.assertIn(self.level2_position.id, entry_ids)

    def test_detail_lists_scoped_applications(self):
        application = self._submit(self.make_application(self.level1_position))
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(application, list(response.context["applications"]))
        self.assertContains(response, application.reference_label)

    def test_detail_404s_for_vacancy_without_scoped_apps(self):
        # The Secretariat has no scoped applications for a Level-2 vacancy.
        self._submit(self.make_application(self.level2_position))
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level2_position.pk]))
        self.assertEqual(response.status_code, 404)

    def test_console_requires_workflow_processor_role(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("vacancy-batches"))
        self.assertEqual(response.status_code, 403)


class ApplicantUploadValidationTests(TestCase):
    def assert_upload_error(self, *, filename, content, content_type, message):
        with self.assertRaisesMessage(ValueError, message):
            validate_applicant_document_upload(
                SimpleUploadedFile(
                    filename,
                    content,
                    content_type=content_type,
                )
            )

    def test_empty_file_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="empty.pdf",
            content=b"",
            content_type="application/pdf",
            message="This file is empty. Pick a file that has something in it.",
        )

    def test_oversized_file_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="oversized.pdf",
            content=b"%PDF-" + (b"A" * (5 * 1024 * 1024)),
            content_type="application/pdf",
            message="This file is too large. Choose a file that is 5 MB or smaller.",
        )

    def test_wrong_extension_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.html",
            content=b"<html></html>",
            content_type="text/html",
            message="This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        )

    def test_unreadable_signature_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"not-a-real-document",
            content_type="application/pdf",
            message=(
                "We couldn't read this as a PDF, JPG, or PNG. "
                "Save it again, then upload it."
            ),
        )

    def test_signature_extension_mismatch_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"\x89PNG\r\n\x1a\nimage-data",
            content_type="application/octet-stream",
            message=(
                "This file's contents don't match a PDF, JPG, or PNG. "
                "Save it again, then upload it."
            ),
        )

    def test_mime_filename_mismatch_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"%PDF-1.4\ndocument",
            content_type="image/png",
            message=(
                "This file format does not match its filename. "
                "Save it again as a PDF, JPG, or PNG, then upload it."
            ),
        )


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
        self.force_login_with_mfa(client, self.sysadmin)
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

    def test_system_admin_can_view_pdf_evidence_inline(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )
        self.assertEqual(evidence.content_type, "application/pdf")

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
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
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        requirement_code = get_required_applicant_document_requirements()[0].code
        evidence = EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=requirement_code,
        )
        evidence.content_type = "application/octet-stream"
        evidence.save(update_fields=["content_type"])

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
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
        self.force_login_with_mfa(client, self.sysadmin)

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
        self.assertContains(response, "No identity events yet")
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
        self.assertContains(response, "No recruitment entries recorded yet")
        self.assertContains(response, reverse("recruitment-entry-create"))
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No recruitment entries recorded yet" copy="Create a Plantilla or COS entry to begin managing applications for a position." action_url=create_entry_url action_label="Create First Entry" %}',
        )
        self.assertNotContains(response, "{# Empty state partial.")

    def test_recruitment_entry_list_renders_modal_partial_without_raw_include_tags(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

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
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Administrator")
        self.assertContains(response, "rg-pill--system-admin")
        self.assertContains(response, f'href="{reverse("evidence-vault-list")}"')
        self.assertContains(response, f'href="{reverse("audit-log-list")}"')
        self.assertContains(response, "User Management")

    def test_non_admin_dashboard_redirects_to_workflow_queue(self):
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
                self.assertEqual(response["Location"], reverse("workflow-queue"))

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
        self.force_login_with_mfa(client, self.secretariat)
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
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Case Audit Log")
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
        self.force_login_with_mfa(client, self.sysadmin)
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
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System Audit Log")
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
            self.force_login_with_mfa(client, user)
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

    def test_resubmission_resets_stage_clock_and_notifies_next_handler(self):
        application = self.make_submitted_application()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please update your application before resubmitting.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        Notification.objects.filter(application=application).delete()
        returned_stage_entered_at = timezone.now() - timedelta(days=4)
        application.case.stage_entered_at = returned_stage_entered_at
        application.case.save(update_fields=["stage_entered_at", "updated_at"])

        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertGreater(application.case.stage_entered_at, returned_stage_entered_at)
        notification = Notification.objects.get(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.RESUBMISSION_RECEIVED,
        )
        self.assertIn("resubmitted", notification.title)

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
        self.assertIn(application.reference_number, mail.outbox[0].subject)
        status_link = reverse("applicant-status-link", kwargs={"token": application.public_token})
        self.assertIn(status_link, mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        html_body, mime_type = mail.outbox[0].alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Application Received", html_body)
        self.assertIn("DOH–CHD CALABARZON", html_body)
        self.assertIn(application.reference_number, html_body)
        self.assertIn(application.position.title, html_body)
        self.assertIn(status_link, html_body)
        self.assertIn(status_link, notification.metadata["status_link"])
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.NOTIFICATION_SENT,
                metadata__notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
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

        # The successful-send audit trail is still recorded.
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
                "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                "completeness_notes": "A corrected document is required.",
                "qualification_outcome": ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                "screening_notes": "Screening cannot continue until the document is corrected.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "operation": "finalize",
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
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
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
                            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{requirement.code}": "Upload a signed and readable copy.",
                "operation": "finalize",
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

        # Verify the applicant (the page's email-OTP step) then re-upload the flagged document.
        otp_code = issue_application_otp(application, actor=application.applicant)
        verify_application_otp(application, otp_code, actor=application.applicant)
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
                            ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a signed and readable copy.",
                "operation": "finalize",
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
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            body,
        )

        # The render failure is logged rather than swallowed silently.
        self.assertTrue(
            any(
                "Failed to render document resubmission email" in message
                for message in logs.output
            )
        )

    def test_return_to_applicant_without_flagged_documents_sends_generic_return_notification(self):
        application = self.make_submitted_application()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please update your submitted information before resubmitting.",
            )

        application.refresh_from_db()
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.APPLICATION_RETURNED_TO_APPLICANT,
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT)
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertEqual(notification.triggered_by, self.secretariat)
        self.assertEqual(
            notification.metadata["workflow_remarks"],
            "Please update your submitted information before resubmitting.",
        )
        self.assertFalse(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.DOCUMENT_RESUBMISSION_REQUEST,
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("application returned", mail.outbox[0].subject.lower())
        self.assertIn(
            "Please update your submitted information before resubmitting.",
            mail.outbox[0].body,
        )
        self.assertIn(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )

    def test_secretariat_can_send_requirement_checklist_notification_for_level1_completion(self):
        application = self.make_approved_cos_application()
        mail.outbox.clear()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

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
        self.assertIn(
            reverse("applicant-status-link", kwargs={"token": application.public_token}),
            mail.outbox[0].body,
        )
        self.assertContains(response, "Send Requirement Checklist")

    def test_requirement_checklist_without_deadline_defaults_to_two_weeks(self):
        application = self.make_approved_cos_application()
        mail.outbox.clear()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("notification-checklist", kwargs={"pk": application.pk}),
                {
                    "checklist_items": "- Signed contract\n- Government-issued ID",
                    "deadline": "",
                    "additional_message": "",
                },
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        notification = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.REQUIREMENT_CHECKLIST,
        ).latest("created_at")
        expected_deadline = timezone.localdate() + timedelta(days=14)
        self.assertEqual(notification.metadata["deadline"], expected_deadline.isoformat())
        self.assertIn("Submission deadline", notification.body)

    def test_secretariat_cannot_send_requirement_checklist_before_selection(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

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
        self.force_login_with_mfa(client, self.secretariat)

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
        self.force_login_with_mfa(client, self.secretariat)

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

    def test_completion_tracking_errors_rerender_bound_wizard_form(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                ],
                deadline=(timezone.localdate() - timedelta(days=1)).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Completion deadline cannot be earlier than today.")
        self.assertContains(response, 'id="completion-form"')
        self.assertContains(response, "Requirement checklist")
        self.assertFalse(CompletionRecord.objects.filter(application=application).exists())

    def test_case_close_missing_notes_rerenders_closure_step_with_bound_errors(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
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
                completion_reference="COS-CONTRACT-CLOSE-ERR",
                completion_date=timezone.localdate().isoformat(),
            ),
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Closure notes are required.")
        self.assertContains(response, "This field is required.")
        self.assertContains(response, 'data-completion-step="close"')
        self.assertContains(response, 'id="closure-form"')
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_requires_completion_reference(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

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
        self.force_login_with_mfa(client, self.secretariat)

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
        self.force_login_with_mfa(client, self.secretariat)

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
        self.assertContains(detail_response, "The case can't be closed yet.")
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
        self.force_login_with_mfa(client, self.secretariat)

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
        self.force_login_with_mfa(client, self.hrm_chief)

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

    def rating_payload(self, application, level=3, **overrides):
        template = self.publish_competency_rating_sheet(application.position)
        payload = {
            "competency_scores": {
                competency: level for competency in template.competencies.all()
            },
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
            "This case is not currently assigned to you for interview scheduling.",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(location="Secretariat Coordination Room"),
                finalize=False,
            )

    def test_secretariat_schedules_plantilla_interview_and_hrmpsb_rates(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)

        interview_session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(location="HRMS Interview Room"),
            finalize=False,
        )
        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(application, level=3),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(
                location="HRMS Interview Room",
                session_notes="HRMS support finalized the Plantilla interview session.",
            ),
            finalize=True,
        )

        self.assertTrue(interview_session.is_finalized)
        self.assertEqual(interview_session.scheduled_by, self.secretariat)
        self.assertEqual(interview_session.finalized_by, self.secretariat)
        self.assertEqual(interview_rating.rated_by, self.hrmpsb)
        self.assertEqual(interview_rating.encoded_by, self.hrmpsb)

    def test_interview_notify_buttons_send_panel_and_applicant_notices(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        second_hrmpsb = User.objects.create_user(
            username="hrmpsb-panel-2",
            password="testpass123",
            email="hrmpsb.panel2@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        scheduled_for = timezone.now() + timedelta(days=2)
        mail.outbox.clear()

        # A plain save is silent — no notifications are sent.
        with self.captureOnCommitCallbacks(execute=True):
            interview_session = save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(
                    scheduled_for=scheduled_for,
                    location="Panel Room 1",
                    session_notes="Panel interview schedule.",
                ),
                finalize=False,
            )
        self.assertEqual(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
            ).count(),
            0,
        )
        self.assertEqual(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
            ).count(),
            0,
        )
        self.assertEqual(
            Notification.objects.filter(
                application=application,
                kind=Notification.Kind.INTERVIEW_SCHEDULED,
            ).count(),
            0,
        )
        self.assertEqual(len(mail.outbox), 0)

        # "Notify HRMPSB panel" emails the panel + posts in-app; the applicant is untouched.
        with self.captureOnCommitCallbacks(execute=True):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(
                    scheduled_for=scheduled_for,
                    location="Panel Room 1",
                    session_notes="Panel interview schedule.",
                ),
                finalize=False,
                notify_panel=True,
            )
        panel_notifications = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
        )
        self.assertEqual(panel_notifications.count(), 2)
        self.assertEqual(
            {notification.recipient_email for notification in panel_notifications},
            {"hrmpsb@example.com", second_hrmpsb.email},
        )
        self.assertEqual(
            Notification.objects.filter(
                application=application,
                kind=Notification.Kind.INTERVIEW_SCHEDULED,
            ).count(),
            2,
        )
        self.assertTrue(
            all(
                notification.metadata["interview_session_id"] == interview_session.id
                for notification in panel_notifications
            )
        )
        self.assertEqual(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
            ).count(),
            0,
        )
        self.assertEqual(len(mail.outbox), 2)

        # "Notify applicant" emails only the applicant; panel notices are unchanged.
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(
                    scheduled_for=scheduled_for,
                    location="Panel Room 1",
                    session_notes="Panel interview schedule.",
                ),
                finalize=False,
                notify_applicant=True,
            )
        self.assertEqual(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
            ).count(),
            1,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(application.applicant_display_name, mail.outbox[0].body)
        self.assertIn(application.position.title, mail.outbox[0].body)
        self.assertIn("Panel Room 1", mail.outbox[0].body)
        self.assertEqual(panel_notifications.count(), 2)

    def test_interview_finalize_notifies_panel_members_without_ratings(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        second_hrmpsb = User.objects.create_user(
            username="hrmpsb-panel-2",
            password="testpass123",
            email="hrmpsb.panel2@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        scheduled_for = timezone.now() + timedelta(days=2)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(
                scheduled_for=scheduled_for,
                location="Panel Room 1",
            ),
            finalize=False,
        )
        save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(application, level=3),
        )

        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(
                scheduled_for=scheduled_for,
                location="Panel Room 1",
            ),
            finalize=True,
        )

        finalized_notifications = Notification.objects.filter(
            application=application,
            kind=Notification.Kind.INTERVIEW_FINALIZED,
        )
        self.assertEqual(finalized_notifications.count(), 1)
        self.assertEqual(finalized_notifications.get().recipient, second_hrmpsb)

    def test_interview_session_rejects_past_schedule(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "The interview can't be scheduled in the past.",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(
                    scheduled_for=timezone.now() - timedelta(hours=1),
                ),
                finalize=False,
            )

    def test_interview_finalize_requires_at_least_one_rating_or_fallback_sheet(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        scheduled_for = timezone.now() + timedelta(days=1)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(scheduled_for=scheduled_for),
            finalize=False,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session.",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(scheduled_for=scheduled_for),
                finalize=True,
            )

    def test_hrmpsb_member_cannot_schedule_plantilla_interview_session(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for interview scheduling.",
        ):
            save_interview_session(
                application=application,
                actor=self.hrmpsb,
                cleaned_data=self.session_payload(location="Panel Room"),
                finalize=False,
            )

    def test_secretariat_can_encode_paper_based_hrmpsb_rating(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(location="Paper Rating Room"),
            finalize=False,
        )

        interview_rating = save_interview_rating(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                **self.rating_payload(application, level=3),
                "rated_by": self.hrmpsb,
            },
        )

        self.assertEqual(interview_rating.rated_by, self.hrmpsb)
        self.assertEqual(interview_rating.rated_by_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(interview_rating.encoded_by, self.secretariat)
        self.assertEqual(interview_rating.encoded_by_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
                metadata__encoded_on_behalf=True,
            ).exists()
        )

    def test_level2_plantilla_interview_support_routes_to_hrm_chief_not_secretariat(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for interview scheduling.",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(location="Level 2 Room"),
                finalize=False,
            )

        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(location="Level 2 HRMS Room"),
            finalize=False,
        )

        self.assertEqual(interview_session.scheduled_by, self.hrm_chief)

    def test_extreme_interview_ratings_require_justification(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        with self.assertRaises(ValidationError) as exc:
            save_interview_rating(
                application=application,
                actor=self.hrmpsb,
                cleaned_data=self.rating_payload(application, level=4, justification=""),
            )
        self.assertIn(
            "Provide a justification when the interview rating is below 75 or above 98.",
            str(exc.exception),
        )

        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(
                application,
                level=4,
                justification="Exceptional technical and behavioral competency responses.",
            ),
        )

        self.assertIsInstance(interview_rating, InterviewRating)
        # All competencies at the top of a 1-4 scale normalize to 100.
        self.assertEqual(str(interview_rating.rating_score), "100.00")

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
            cleaned_data=self.rating_payload(application, level=3),
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
        # Every competency scored at 3 on a 1-4 scale normalizes to 75.
        self.assertEqual(str(interview_rating.rating_score), "75.00")
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
            cleaned_data=self.rating_payload(application),
        )
        interview_session = save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Finalized interview session."),
            finalize=True,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview records cannot accept rating changes.",
        ):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data=self.rating_payload(application, level=3),
            )
        with self.assertRaisesMessage(
            ValueError,
            "Finalized interview records cannot accept fallback rating uploads.",
        ):
            upload_interview_fallback_rating(
                application=application,
                actor=self.hrm_chief,
                uploaded_file=SimpleUploadedFile("fallback.pdf", b"fallback", content_type="application/pdf"),
                remarks="Late upload.",
            )

        interview_session.refresh_from_db()
        self.assertTrue(interview_session.is_finalized)

    def test_interview_fallback_rejects_active_content_upload(self):
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

        with self.assertRaisesMessage(
            ValueError,
            "This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        ):
            upload_interview_fallback_rating(
                application=application,
                actor=self.hrm_chief,
                uploaded_file=SimpleUploadedFile(
                    "fallback.html",
                    b"<html><script>alert(1)</script></html>",
                    content_type="text/html",
                ),
                remarks="Fallback upload.",
            )

        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                application=application,
                artifact_type="interview_fallback_rating_sheet",
            ).exists()
        )


class CompetencyRatingSheetTests(BaseRecruitmentTestCase):
    def test_create_seeds_standard_core_and_org_competencies(self):
        template = create_competency_rating_template(self.level1_position, self.secretariat)
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.DRAFT)
        self.assertEqual(template.scale_min, 1)
        self.assertEqual(template.scale_max, 4)
        self.assertEqual(template.created_by, self.secretariat)
        self.assertEqual(template.created_by_role, RecruitmentUser.Role.SECRETARIAT)
        groups = list(template.competencies.values_list("group", flat=True))
        self.assertEqual(groups.count(CompetencyDefinition.Group.CORE), 3)
        self.assertEqual(groups.count(CompetencyDefinition.Group.ORGANIZATIONAL), 3)
        self.assertEqual(groups.count(CompetencyDefinition.Group.TECHNICAL), 0)
        self.assertEqual(template.competencies.count(), 6)

    def test_create_is_one_per_vacancy(self):
        create_competency_rating_template(self.level1_position, self.secretariat)
        self.assertEqual(
            get_competency_rating_template(self.level1_position).competencies.count(), 6
        )
        with self.assertRaises(ValueError):
            create_competency_rating_template(self.level1_position, self.secretariat)

    def test_builder_create_then_add_technical_and_publish_via_view(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        url = reverse("interview-rating-sheet", kwargs={"pk": application.pk})

        create_response = client.post(url, {"operation": "create"})
        self.assertEqual(create_response.status_code, 302)
        template = get_competency_rating_template(self.level1_position)
        self.assertEqual(template.competencies.count(), 6)
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.DRAFT)

        existing = list(template.competencies.order_by("order"))
        data = {
            "operation": "publish",
            "scale_max": "4",
            "instructions": "Score each competency.",
            "competencies-TOTAL_FORMS": str(len(existing) + 1),
            "competencies-INITIAL_FORMS": str(len(existing)),
            "competencies-MIN_NUM_FORMS": "0",
            "competencies-MAX_NUM_FORMS": "1000",
        }
        for index, competency in enumerate(existing):
            data[f"competencies-{index}-id"] = str(competency.id)
            data[f"competencies-{index}-group"] = competency.group
            data[f"competencies-{index}-name"] = competency.name
            data[f"competencies-{index}-weight"] = "1.00"
        new_index = len(existing)
        data[f"competencies-{new_index}-id"] = ""
        data[f"competencies-{new_index}-group"] = CompetencyDefinition.Group.TECHNICAL
        data[f"competencies-{new_index}-name"] = "Data Recording and Reporting"
        data[f"competencies-{new_index}-weight"] = "2.00"

        publish_response = client.post(url, data)
        self.assertEqual(publish_response.status_code, 302)
        template.refresh_from_db()
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.PUBLISHED)
        self.assertIsNotNone(template.published_at)
        self.assertEqual(template.competencies.count(), 7)
        technical = template.competencies.get(group=CompetencyDefinition.Group.TECHNICAL)
        self.assertEqual(technical.name, "Data Recording and Reporting")
        self.assertEqual(str(technical.weight), "2.00")


class InterviewCompetencyScoringTests(BaseRecruitmentTestCase):
    def _setup_interview(self, application=None):
        application = application or self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "scheduled_for": timezone.now() + timedelta(days=1),
                "location": "Panel Room",
                "session_notes": "Scheduled.",
            },
            finalize=False,
        )
        return application

    def test_normalizes_each_score_over_the_scale_max(self):
        template = self.publish_competency_rating_sheet(self.level1_position)
        comps = list(template.competencies.all())
        self.assertEqual(
            compute_competency_rating_score(template, {c: 3 for c in comps}),
            Decimal("75.00"),
        )
        self.assertEqual(
            compute_competency_rating_score(template, {c: 4 for c in comps}),
            Decimal("100.00"),
        )
        self.assertEqual(
            compute_competency_rating_score(template, {c: 1 for c in comps}),
            Decimal("25.00"),
        )

    def test_weighted_competencies_normalize_proportionally(self):
        template = self.publish_competency_rating_sheet(self.level1_position)
        comps = list(template.competencies.all())
        comps[0].weight = Decimal("5.00")
        comps[0].save(update_fields=["weight", "updated_at"])
        scores = {comps[0]: 4}
        for competency in comps[1:]:
            scores[competency] = 2
        # (5*100 + 5*50) / 10 = 75.00 — the heavy competency pulls the average up.
        self.assertEqual(compute_competency_rating_score(template, scores), Decimal("75.00"))

    def test_draft_sheet_is_not_available_to_raters(self):
        create_competency_rating_template(self.level1_position, self.secretariat)
        self.assertIsNone(get_published_competency_rating_template(self.level1_position))

    def test_rating_requires_a_published_sheet(self):
        application = self._setup_interview()
        with self.assertRaisesMessage(
            ValueError,
            "Publish the interview rating sheet",
        ):
            save_interview_rating(
                application=application,
                actor=self.hrmpsb,
                cleaned_data={"competency_scores": {}, "rating_notes": "", "justification": ""},
            )

    def test_first_score_locks_template_and_persists_scores(self):
        application = self._setup_interview()
        template = self.publish_competency_rating_sheet(self.level1_position)
        comps = list(template.competencies.all())
        rating = save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data={
                "competency_scores": {c: 3 for c in comps},
                "rating_notes": "Recorded.",
                "justification": "",
            },
        )
        template.refresh_from_db()
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.LOCKED)
        self.assertIsNotNone(template.locked_at)
        self.assertEqual(str(rating.rating_score), "75.00")
        self.assertEqual(rating.competency_scores.count(), len(comps))

    def test_revision_replaces_scores_and_recomputes(self):
        application = self._setup_interview()
        template = self.publish_competency_rating_sheet(self.level1_position)
        comps = list(template.competencies.all())
        save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data={
                "competency_scores": {c: 3 for c in comps},
                "rating_notes": "",
                "justification": "",
            },
        )
        rating = save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data={
                "competency_scores": {c: 4 for c in comps},
                "rating_notes": "",
                "justification": "Top marks across the sheet.",
            },
        )
        self.assertEqual(str(rating.rating_score), "100.00")
        self.assertEqual(rating.competency_scores.count(), len(comps))
        self.assertTrue(all(score.score == 4 for score in rating.competency_scores.all()))
        self.assertEqual(CompetencyScore.objects.filter(interview_rating=rating).count(), len(comps))

    def test_missing_competency_score_is_rejected(self):
        application = self._setup_interview()
        template = self.publish_competency_rating_sheet(self.level1_position)
        comps = list(template.competencies.all())
        partial = {c: 3 for c in comps[:-1]}
        with self.assertRaisesMessage(ValueError, "Score every competency"):
            save_interview_rating(
                application=application,
                actor=self.hrmpsb,
                cleaned_data={
                    "competency_scores": partial,
                    "rating_notes": "",
                    "justification": "",
                },
            )

    def test_rating_view_posts_the_competency_grid(self):
        application = self._setup_interview()
        template = self.publish_competency_rating_sheet(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        data = {
            "rated_by": self.hrmpsb.pk,
            "rating_notes": "Submitted via the scoring grid.",
            "justification": "",
        }
        for competency in template.competencies.all():
            data[f"score_{competency.id}"] = "3"
        response = client.post(
            reverse("interview-rating", kwargs={"pk": application.pk}),
            data,
        )
        self.assertEqual(response.status_code, 302)
        rating = InterviewRating.objects.get(application=application, rated_by=self.hrmpsb)
        self.assertEqual(str(rating.rating_score), "75.00")
        self.assertEqual(rating.competency_scores.count(), template.competencies.count())
        template.refresh_from_db()
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.LOCKED)


class DeliberationDecisionSupportTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Virtual Deliberation Room",
            "session_notes": "Decision-support interview session prepared.",
        }
        payload.update(overrides)
        return payload

    def deliberation_payload(self, **overrides):
        payload = {
            "deliberated_at": timezone.now(),
            "deliberation_minutes": "Panel reviewed the finalized applicant pool.",
            "recommendation": "HRMPSB recommends the ranking after reviewing the CAR draft.",
            "decision_support_summary": "Decision-support summary for ranking.",
            "quorum_status": DeliberationRecord.QuorumStatus.MET,
            "attendance_notes": "Quorum and attendance were recorded.",
            "ranking_position": 1,
            "ranking_notes": "Ranking basis recorded.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, application, level=3, **overrides):
        template = self.publish_competency_rating_sheet(application.position)
        payload = {
            "competency_scores": {
                competency: level for competency in template.competencies.all()
            },
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
            cleaned_data=self.rating_payload(application),
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
            "75.00",
        )

    def test_user_full_name_falls_back_to_username_for_display(self):
        self.assertEqual(self.applicant.get_full_name(), self.applicant.username)

    def test_hrms_prepares_car_draft_from_finalized_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)

        draft_report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft CAR prepared for HRMPSB review."},
            finalize=False,
        )
        draft_item = draft_report.items.get(recruitment_case=application.case)

        self.assertFalse(draft_report.is_finalized)
        self.assertEqual(draft_report.version_number, 1)
        self.assertIsNone(draft_item.deliberation_record)
        self.assertEqual(str(draft_item.interview_average_score), "100.00")
        self.assertEqual(draft_item.rank_order, 1)

    def test_car_autosave_does_not_generate_report_versions_or_files(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "autosave before draft"},
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).exists()
        )
        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).exists()
        )

        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "manual draft generation"},
        )
        self.assertEqual(response.status_code, 302)
        draft_report = ComparativeAssessmentReport.objects.get(
            recruitment_entry=application.position,
            review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
        )
        self.assertEqual(draft_report.version_number, 1)
        self.assertEqual(draft_report.summary_notes, "manual draft generation")
        self.assertEqual(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).count(),
            1,
        )

        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "autosaved draft notes"},
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 204)
        draft_report.refresh_from_db()
        self.assertEqual(draft_report.summary_notes, "autosaved draft notes")
        self.assertEqual(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).count(),
            1,
        )
        self.assertEqual(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).count(),
            1,
        )

    def test_car_generation_requires_finalized_applicant_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_applicant_pool_for_test(self.level1_position)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        self.level1_position.status = PositionPosting.EntryStatus.ACTIVE
        self.level1_position.closing_date = timezone.localdate() + timedelta(days=3)
        self.level1_position.save(update_fields=["status", "closing_date", "is_active", "updated_at"])
        application.position.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Plantilla deliberation and CAR generation are available only after the vacancy is closed",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={"summary_notes": "Attempted before pool finalization."},
                finalize=True,
            )

    def test_secretariat_prepares_level1_car_from_finalized_hrmpsb_outputs(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_deliberation_for_current_stage(
            application,
            self.hrmpsb,
            ranking_position=1,
            decision_support_summary="HRMPSB recommendation summary for the CAR.",
        )

        self.assertTrue(user_can_view_application(self.secretariat, application))
        self.assertTrue(user_can_manage_comparative_assessment_report(self.secretariat, application))
        self.assertFalse(user_can_manage_comparative_assessment_report(self.hrmpsb, application))
        self.assertTrue(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Prepared by HRMS from finalized HRMPSB records."},
            finalize=True,
        )
        item = report.items.get(recruitment_case=application.case)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.recruitment_entry, self.level1_position)
        self.assertEqual(report.generated_by, self.secretariat)
        self.assertEqual(report.finalized_by, self.secretariat)
        self.assertEqual(report.generated_by_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(item.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED)
        self.assertEqual(item.exam_status, ExamRecord.ExamStatus.COMPLETED)
        self.assertEqual(str(item.exam_score), "88.50")
        self.assertEqual(str(item.interview_average_score), "100.00")
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["document_review_outcome"],
            ScreeningRecord.QualificationOutcome.QUALIFIED,
        )
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["finalized_exam_count"],
            1,
        )
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["finalized_interview_count"],
            1,
        )

    def test_hrm_chief_prepares_level2_car_and_secretariat_cannot_take_it(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        self.assertFalse(user_can_manage_comparative_assessment_report(self.secretariat, application))
        self.assertFalse(user_can_view_application(self.secretariat, application))
        self.assertTrue(user_can_manage_comparative_assessment_report(self.hrm_chief, application))
        self.assertTrue(get_queue_for_user(self.hrm_chief).filter(pk=application.pk).exists())

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.hrm_chief,
            cleaned_data={"summary_notes": "Level 2 CAR prepared by HRM Chief."},
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.generated_by, self.hrm_chief)
        self.assertEqual(report.finalized_by, self.hrm_chief)
        self.assertEqual(report.generated_by_role, RecruitmentUser.Role.HRM_CHIEF)

    def test_hrmpsb_member_cannot_prepare_car_after_deliberation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for CAR preparation.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.hrmpsb,
                cleaned_data={"summary_notes": "HRMPSB should not prepare this CAR."},
                finalize=True,
            )

    def test_car_preserves_policy_preliminary_ranking_and_hrmpsb_ranking_notes(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="prelim-rank-secondary",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="prelim.rank.secondary@example.com",
            applicant_phone="09179990222",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for preliminary ranking.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="prelim-rank-secondary",
        )

        for application, applicant in (
            (primary_application, self.applicant),
            (secondary_application, secondary_applicant),
        ):
            otp_code = issue_application_otp(application, actor=applicant)
            verify_application_otp(application, otp_code, actor=applicant)
            application.refresh_from_db()
            submit_application(application, applicant)
            application.refresh_from_db()

        self.finalize_screening_for_current_stage(
            primary_application,
            self.secretariat,
            education_score="90.00",
            training_score="90.00",
            experience_score="90.00",
        )
        self.finalize_exam_for_current_stage(
            primary_application,
            self.secretariat,
            exam_score="90.00",
            technical_score="90.00",
            general_score="90.00",
        )
        primary_application.refresh_from_db()
        self.finalize_screening_for_current_stage(
            secondary_application,
            self.secretariat,
            education_score="80.00",
            training_score="80.00",
            experience_score="80.00",
        )
        self.finalize_exam_for_current_stage(
            secondary_application,
            self.secretariat,
            exam_score="80.00",
            technical_score="80.00",
            general_score="80.00",
        )
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb, rating_score="90.00")
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb, rating_score="80.00")
        self.finalize_deliberation_for_current_stage(
            primary_application,
            self.hrmpsb,
            ranking_position=2,
            decision_support_summary="Higher preliminary score noted.",
            ranking_notes="HRMPSB placed this candidate second after considering office-fit concerns.",
        )
        self.finalize_deliberation_for_current_stage(
            secondary_application,
            self.hrmpsb,
            ranking_position=1,
            decision_support_summary="Panel recommended this candidate despite lower preliminary score.",
            ranking_notes="Panel justification recorded for the rank adjustment.",
        )

        report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "CAR includes advisory preliminary ranking."},
            finalize=True,
        )
        primary_item = report.items.get(recruitment_case=primary_application.case)
        secondary_item = report.items.get(recruitment_case=secondary_application.case)

        # Ranking is purely computed now (no HRMPSB override) — higher score ranks first.
        self.assertEqual(primary_item.rank_order, 1)
        self.assertEqual(primary_item.preliminary_rank_order, 1)
        # interview 90->level 4->100: 90*0.20 + 90*0.40 + 100*0.40 = 94.00
        self.assertEqual(str(primary_item.assessment_score), "94.00")
        self.assertEqual(str(primary_item.document_review_score), "90.00")
        self.assertEqual(secondary_item.rank_order, 2)
        self.assertEqual(secondary_item.preliminary_rank_order, 2)
        # interview 80->level 3->75: 80*0.20 + 80*0.40 + 75*0.40 = 78.00
        self.assertEqual(str(secondary_item.assessment_score), "78.00")
        self.assertEqual(
            report.consolidated_snapshot["assessment_weight_display"],
            "Document review 40%, exam 20%, interview 40%.",
        )

    def test_car_uses_manual_ete_rating_when_set(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        # Finalizes the applicant pool and stages an initial CAR draft.
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()
        set_application_ete_rating(application, self.secretariat, Decimal("60"))

        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft with a manual ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)

        self.assertEqual(item.ete_rating, Decimal("60.00"))
        # Overall uses the manual ETE (40%) instead of the screening score.
        expected = (
            Decimal("60") * Decimal("0.40")
            + item.exam_score * Decimal("0.20")
            + item.interview_average_score * Decimal("0.40")
        ).quantize(Decimal("0.01"))
        self.assertEqual(item.assessment_score, expected)

    def test_car_view_saves_posted_ete_ratings(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {
                "operation": "save",
                "summary_notes": "Draft with ETE entered on screen.",
                f"ete_{application.case.id}": "65",
            },
        )

        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertEqual(application.ete_rating.rating, Decimal("65.00"))
        draft = (
            ComparativeAssessmentReport.objects.filter(recruitment_entry=application.position)
            .order_by("-version_number")
            .first()
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertEqual(item.ete_rating, Decimal("65.00"))

    def test_car_finalize_records_quorum_attestation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Finalized CAR.",
                "quorum_met": True,
                "members_present": 5,
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertIs(report.quorum_met, True)
        self.assertEqual(report.members_present, 5)

    def test_plantilla_recommendation_requires_finalized_car(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_applicant_pool_for_test(self.level1_position)
        application.position.refresh_from_db()

        # Plantilla has no in-system deliberation anymore — the finalized CAR is the gate.
        with self.assertRaisesMessage(
            ValueError,
            "Finalize the Comparative Assessment Report before recommending this Plantilla application.",
        ):
            process_workflow_action(application, self.hrmpsb, "recommend", "Attempted without CAR.")

        # Stages the CAR draft, then finalizing it clears the gate.
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        report = self.finalize_car_for_current_stage(application, self.hrmpsb)
        self.assertEqual(ComparativeAssessmentReportItem.objects.filter(report=report).count(), 1)

    def test_car_generation_creates_versioned_evidence(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.version_number, 2)
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
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)

        draft_report = ComparativeAssessmentReport.objects.get(
            recruitment_entry=self.level1_position,
            review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            is_finalized=False,
        )
        finalized_report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.secretariat,
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
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
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
    def prepare_ranked_level1_car(self, candidate_count):
        applications = [self.make_application(self.level1_position)]
        for index in range(2, candidate_count + 1):
            applicant = User.objects.create_user(
                username=f"ranked-car-applicant-{index}",
                password="testpass123",
                role=RecruitmentUser.Role.APPLICANT,
            )
            application = RecruitmentApplication.objects.create(
                applicant=applicant,
                position=self.level1_position,
                applicant_first_name=f"Candidate{index}",
                applicant_last_name="Ranked",
                applicant_email=f"ranked.car.{index}@example.com",
                applicant_phone=f"09179990{index:03d}",
                checklist_privacy_consent=True,
                checklist_documents_complete=True,
                checklist_information_certified=True,
                qualification_summary="Additional applicant for ranked CAR testing.",
                cover_letter="Applying for the same vacancy.",
                performance_rating_applicability=(
                    RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
                ),
            )
            self.upload_required_applicant_documents(
                application,
                applicant,
                content_prefix=f"ranked-car-{index}",
            )
            applications.append(application)

        for index, application in enumerate(applications, start=1):
            self.verify_application_for_submission(application)
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, application.applicant)
            application.refresh_from_db()
            score = f"{96 - index}.00"
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                education_score=score,
                training_score=score,
                experience_score=score,
            )
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_score=score,
                technical_score=score,
                general_score=score,
            )
            application.refresh_from_db()

        for index, application in enumerate(applications, start=1):
            self.finalize_interview_for_current_stage(
                application,
                self.hrmpsb,
                rating_score=f"{96 - index}.00",
            )
        for index, application in enumerate(applications, start=1):
            self.finalize_deliberation_for_current_stage(
                application,
                self.hrmpsb,
                ranking_position=index,
                decision_support_summary=f"HRMPSB support summary for rank {index}.",
                ranking_notes=f"HRMPSB ranking notes for rank {index}.",
            )

        report = self.finalize_car_for_current_stage(applications[0], self.hrmpsb)
        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
        return applications, report

    def test_final_selection_routes_selected_plantilla_case_to_completion_and_preserves_car(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        selection = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after reviewing the finalized CAR.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(selection.selected_application_id, application.id)
        self.assertEqual(selection.decided_by, self.appointing)
        self.assertTrue(selection.car_snapshot["comparative_assessment_report"]["items"])
        self.assertFalse(FinalDecision.objects.filter(application=application).exists())
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )

    def test_final_selection_marks_other_car_applicants_not_selected(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="final-selection-secondary",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="final.selection.secondary@example.com",
            applicant_phone="09179990123",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for CAR final selection.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="final-selection-secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)
        report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)
        primary_application.refresh_from_db()
        secondary_application.refresh_from_db()
        selected_item = report.items.get(recruitment_case=secondary_application.case)

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=primary_application,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "decision_notes": "Selected the second-ranked applicant from the CAR.",
                },
            )

        primary_application.refresh_from_db()
        primary_application.case.refresh_from_db()
        secondary_application.refresh_from_db()
        secondary_application.case.refresh_from_db()

        self.assertEqual(FinalSelection.objects.filter(recruitment_entry=self.level1_position).count(), 1)
        self.assertEqual(selection.selected_application_id, secondary_application.id)
        self.assertEqual(secondary_application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(secondary_application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(secondary_application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(primary_application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(primary_application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(primary_application.case.is_stage_locked)
        self.assertFalse(FinalDecision.objects.filter(application__position=self.level1_position).exists())
        self.assertTrue(
            NotificationLog.objects.filter(
                application=secondary_application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )
        self.assertTrue(
            NotificationLog.objects.filter(
                application=primary_application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_final_selection_outside_top_five_requires_deep_selection_support(self):
        applications, report = self.prepare_ranked_level1_car(6)
        selected_item = report.items.get(rank_order=6)

        with self.assertRaisesMessage(
            ValueError,
            "Selecting outside the top five requires deep selection documentation.",
        ):
            record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "decision_notes": "Attempted selection below the top five.",
                },
            )

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "is_deep_selection": True,
                    "deep_selection_justification": (
                        "Appointing Authority documented superior qualifications "
                        "for the applicant ranked outside the top five."
                    ),
                    "decision_notes": "Selected through documented deep selection.",
                },
            )

        selected_application = applications[5]
        selected_application.refresh_from_db()
        selected_application.case.refresh_from_db()
        applications[0].refresh_from_db()
        applications[0].case.refresh_from_db()

        self.assertTrue(selection.is_deep_selection)
        self.assertIn("superior qualifications", selection.deep_selection_justification)
        self.assertEqual(selection.selected_application_id, selected_application.id)
        self.assertEqual(selected_application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(selected_application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(applications[0].status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(applications[0].case.current_stage, RecruitmentCase.Stage.CLOSED)

    def test_final_selection_invalid_post_rerenders_bound_form(self):
        applications, report = self.prepare_ranked_level1_car(6)
        selected_item = report.items.get(rank_order=6)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        response = client.post(
            reverse("final-selection-record", kwargs={"pk": applications[0].pk}),
            {
                "selected_item": str(selected_item.pk),
                "decision_notes": "Attempted to record a rank-six candidate without support.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Selecting outside the top five requires deep selection documentation.",
        )
        self.assertContains(response, "Pick the appointee")
        self.assertRegex(
            response.content.decode(),
            rf'name="selected_item"\s+value="{selected_item.pk}"\s+checked',
        )
        self.assertFalse(
            FinalSelection.objects.filter(recruitment_entry=self.level1_position).exists()
        )

    def test_appointing_authority_can_return_car_for_hrmpsb_reassessment(self):
        applications, report = self.prepare_ranked_level1_car(2)

        self.assertIn(
            ("return_car_for_reassessment", "Return CAR for HRMPSB Reassessment"),
            get_available_actions(applications[0], self.appointing),
        )
        returned_report = process_workflow_action(
            applications[0],
            self.appointing,
            "return_car_for_reassessment",
            "CAR recommendation does not conform to final assessment.",
        )

        report.refresh_from_db()
        self.assertEqual(returned_report.id, report.id)
        self.assertTrue(report.is_returned)
        self.assertEqual(report.returned_by, self.appointing)
        self.assertIn("does not conform", report.return_reason)
        self.assertIsNone(get_latest_finalized_comparative_assessment_report(applications[0]))

        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
            self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
            self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
            self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
            self.assertTrue(
                AuditLog.objects.filter(
                    application=application,
                    action=AuditLog.Action.CAR_RETURNED,
                ).exists()
            )
            self.assertTrue(
                application.routing_history.filter(
                    route_type=RoutingHistory.RouteType.REOPEN,
                    to_handler_role=RecruitmentUser.Role.HRMPSB_MEMBER,
                ).exists()
            )

        new_draft = generate_comparative_assessment_report(
            application=applications[0],
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Reassessment CAR draft after AA return."},
            finalize=False,
        )
        self.assertGreater(new_draft.version_number, report.version_number)
        for index, application in enumerate(applications, start=1):
            self.finalize_deliberation_for_current_stage(
                application,
                self.hrmpsb,
                ranking_position=index,
                decision_support_summary=f"Reassessed support summary for rank {index}.",
            )
        new_report = generate_comparative_assessment_report(
            application=applications[0],
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Final CAR after HRMPSB reassessment."},
            finalize=True,
        )
        self.assertTrue(new_report.is_finalized)
        self.assertFalse(new_report.is_returned)

    def test_return_car_invalid_post_rerenders_bound_form(self):
        applications, report = self.prepare_ranked_level1_car(2)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        response = client.post(
            reverse("workflow-action", kwargs={"pk": applications[0].pk}),
            {
                "action": "return_car_for_reassessment",
                "remarks": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send the CAR back to HRMPSB")
        self.assertContains(response, "This field is required.")
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=self.level1_position).exists())
        report.refresh_from_db()
        self.assertFalse(report.is_returned)

    def test_appointing_authority_cannot_use_legacy_single_case_approval_actions(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        for action in ("approve", "reject", "return_to_hrm_chief"):
            with self.assertRaisesMessage(
                ValueError,
                "Plantilla Appointing Authority actions must be recorded from the finalized CAR.",
            ):
                process_workflow_action(
                    application,
                    self.appointing,
                    action,
                    "Legacy single-case action should not be available.",
                )
            application.refresh_from_db()

    def test_level2_final_selection_routes_selected_applicant_to_hrm_chief_completion(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected Level 2 applicant from the finalized CAR.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

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
            "Only the HRM Chief may record the final decision at the current step.",
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
        self.force_login_with_mfa(client, self.appointing)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Final Selection</span>')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-deliberation"')
        self.assertContains(response, 'rg-cws-layout rg-cws-layout--full')
        self.assertNotContains(response, "Workflow Snapshot")
        self.assertContains(response, "Pick the appointee")
        self.assertNotContains(response, "Review the submission packet")

        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_comparative_assessment_report"])

    def test_cos_application_detail_exposes_decision_wizard(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">COS Selection</span>')
        self.assertContains(response, "Review the submission packet")
        self.assertContains(response, "Record your decision")
        self.assertContains(response, reverse("final-decision-record", kwargs={"pk": application.pk}))
        self.assertNotContains(response, "Pick the appointee")

    def test_submission_packet_marks_missing_evaluation_records(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)

        packet = build_submission_packet(application)

        self.assertFalse(packet["summary"]["ready_for_final_decision"])
        self.assertIn("Finalized screening record", packet["summary"]["missing_components"])
        self.assertIn("Finalized examination record", packet["summary"]["missing_components"])
        self.assertIn("Finalized interview session", packet["summary"]["missing_components"])
        self.assertIn("Finalized deliberation record", packet["summary"]["missing_components"])

    def test_final_decision_invalid_post_rerenders_bound_wizard_form(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("final-decision-record", kwargs={"pk": application.pk}),
            {
                "decision_outcome": "",
                "decision_notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose the final outcome and provide the decision remarks.")
        self.assertContains(response, "This field is required.")
        self.assertContains(response, 'data-decision-step="decide"')


class E2ESeedCommandTests(BaseRecruitmentTestCase):
    def test_seed_e2e_test_cases_creates_verification_cases(self):
        out = io.StringIO()
        call_command("seed_e2e_test_cases", stdout=out, base_email="e2e@example.test")

        cos_screening = RecruitmentApplication.objects.get(reference_number="RG-COS-test-screening")
        self.assertEqual(cos_screening.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(get_current_workflow_section(cos_screening), "screening")
        self.assertFalse(cos_screening.screening_records.filter(is_finalized=True).exists())

        exam = RecruitmentApplication.objects.get(reference_number="RG-PLT-test-exam")
        self.assertEqual(exam.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(get_current_workflow_section(exam), "exam")
        self.assertTrue(exam.screening_records.filter(is_finalized=True).exists())

        interview = RecruitmentApplication.objects.get(
            reference_number="RG-PLT-test-interview"
        )
        self.assertEqual(
            interview.case.current_stage,
            RecruitmentCase.Stage.HRMPSB_REVIEW,
        )
        self.assertEqual(get_current_workflow_section(interview), "interview")
        self.assertFalse(
            interview.interview_sessions.filter(is_finalized=True).exists()
        )

        deliberation = RecruitmentApplication.objects.get(
            reference_number="RG-PLT-test-deliberation"
        )
        self.assertEqual(
            deliberation.case.current_stage,
            RecruitmentCase.Stage.HRMPSB_REVIEW,
        )
        self.assertEqual(get_current_workflow_section(deliberation), "car")
        self.assertTrue(
            deliberation.interview_sessions.filter(is_finalized=True).exists()
        )
        self.assertTrue(
            deliberation.position.comparative_assessment_reports.filter(
                is_finalized=False
            ).exists()
        )

        cos_decision = RecruitmentApplication.objects.get(reference_number="RG-COS-test-decision")
        self.assertEqual(cos_decision.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(get_current_workflow_section(cos_decision), "decision")

        final_selection = RecruitmentApplication.objects.get(
            reference_number="RG-PLT-test-final-selection"
        )
        final_selection_report = get_latest_finalized_comparative_assessment_report(
            final_selection
        )
        self.assertEqual(final_selection.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(final_selection), "decision")
        self.assertEqual(final_selection_report.items.count(), 6)
        self.assertTrue(final_selection_report.items.filter(rank_order__gt=5).exists())

        aa_decision = RecruitmentApplication.objects.get(reference_number="RG-PLT-test-aa-decision")
        self.assertEqual(aa_decision.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(aa_decision), "decision")
        self.assertIsNotNone(get_latest_finalized_comparative_assessment_report(aa_decision))
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=aa_decision.position).exists())

        aa_return = RecruitmentApplication.objects.get(reference_number="RG-PLT-test-aa-return")
        self.assertEqual(aa_return.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(aa_return), "decision")
        self.assertIsNotNone(get_latest_finalized_comparative_assessment_report(aa_return))
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=aa_return.position).exists())

        completion = RecruitmentApplication.objects.get(reference_number="RG-PLT-test-completion")
        self.assertEqual(completion.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(get_current_workflow_section(completion), "completion")
        self.assertTrue(hasattr(completion, "completion_record"))
        self.assertEqual(completion.completion_record.requirements.count(), 1)

        call_command("seed_e2e_test_cases", stdout=io.StringIO(), base_email="e2e@example.test")
        self.assertEqual(
            RecruitmentApplication.objects.filter(reference_number="RG-PLT-test-exam").count(),
            1,
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                reference_number="RG-PLT-test-interview"
            ).count(),
            1,
        )
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                reference_number="RG-PLT-test-deliberation"
            ).count(),
            1,
        )


class PositionDocumentRequirementTests(BaseRecruitmentTestCase):
    def _branch_selections(self, branch, *, applies_overrides=None, level_overrides=None):
        """Build a full selections list for a branch, defaulting to the standard set."""
        applies_overrides = applies_overrides or {}
        level_overrides = level_overrides or {}
        selections = []
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[branch]:
            code = requirement.code
            selections.append(
                {
                    "code": code,
                    "applies": applies_overrides.get(code, True),
                    "is_required": level_overrides.get(code, requirement.is_required),
                }
            )
        return selections

    def test_set_position_document_requirements_configures_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA,
                applies_overrides={DIPLOMA: False},
                level_overrides={TRAINING_CERTIFICATES: True},
            ),
            self.secretariat,
        )

        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertNotIn(DIPLOMA, codes)
        required_codes = [
            requirement.code for requirement in get_required_applicant_document_requirements(posting)
        ]
        self.assertIn(TRAINING_CERTIFICATES, required_codes)
        self.assertNotIn(DIPLOMA, required_codes)
        self.assertFalse(posting.document_requirements.filter(document_code=DIPLOMA).exists())
        self.assertTrue(
            posting.document_requirements.get(document_code=TRAINING_CERTIFICATES).is_required
        )

    def test_minimum_required_documents_cannot_be_dropped_or_optionalized(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA,
                applies_overrides={SIGNED_COVER_LETTER: False},
                level_overrides={PERSONAL_DATA_SHEET: False},
            ),
            self.secretariat,
        )
        self.assertTrue(
            posting.document_requirements.get(document_code=SIGNED_COVER_LETTER).is_required
        )
        self.assertTrue(
            posting.document_requirements.get(document_code=PERSONAL_DATA_SHEET).is_required
        )

    def test_performance_rating_is_offered_only_and_stays_conditional(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        offered = [
            requirement
            for requirement in get_applicant_document_requirements(posting)
            if requirement.code == PERFORMANCE_RATING
        ]
        self.assertEqual(len(offered), 1)
        self.assertFalse(offered[0].is_required)
        self.assertTrue(offered[0].conditional_on_performance_rating)

        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={PERFORMANCE_RATING: False}
            ),
            self.secretariat,
        )
        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertNotIn(PERFORMANCE_RATING, codes)

    def test_resolver_skips_codes_invalid_for_branch(self):
        posting = self.cos_position
        PositionDocumentRequirement.objects.create(
            posting=posting, document_code=SIGNED_COVER_LETTER, is_required=True, order=1
        )
        PositionDocumentRequirement.objects.create(
            posting=posting, document_code=PERFORMANCE_RATING, is_required=True, order=2
        )
        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertIn(SIGNED_COVER_LETTER, codes)
        self.assertNotIn(PERFORMANCE_RATING, codes)

    def test_cos_posting_never_stores_performance_rating(self):
        posting = self.cos_position
        selections = self._branch_selections(PositionPosting.Branch.COS)
        selections.append({"code": PERFORMANCE_RATING, "applies": True, "is_required": True})
        set_position_document_requirements(posting, selections, self.secretariat)
        self.assertFalse(
            posting.document_requirements.filter(document_code=PERFORMANCE_RATING).exists()
        )

    def test_legacy_posting_without_rows_uses_branch_catalog(self):
        posting = self.level1_position
        self.assertEqual(posting.document_requirements.count(), 0)
        self.assertEqual(
            get_applicant_document_requirements(posting),
            APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA],
        )

    def test_configuration_locked_once_posting_is_live(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        posting.refresh_from_db()
        self.assertTrue(posting.is_live_for_metadata_lock)

        with self.assertRaises(ValidationError):
            set_position_document_requirements(
                posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
            )

        existing_row = posting.document_requirements.first()
        existing_row.is_required = not existing_row.is_required
        with self.assertRaises(ValidationError):
            existing_row.save()

    def test_submit_gate_uses_configured_required_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={DIPLOMA: False}
            ),
            self.secretariat,
        )
        application = self.make_application(posting)
        required_codes = [
            requirement.code
            for requirement in get_required_applicant_document_requirements(application)
        ]
        self.assertNotIn(DIPLOMA, required_codes)
        self.assertEqual(get_missing_required_applicant_document_requirements(application), [])

    def test_screening_review_uses_configured_document_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={DIPLOMA: False}
            ),
            self.secretariat,
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        screening_record = self.finalize_screening_for_current_stage(application, self.secretariat)
        self.assertEqual(
            screening_record.document_reviews.count(),
            len(get_applicant_document_requirements(application)),
        )
        self.assertFalse(screening_record.document_reviews.filter(document_key=DIPLOMA).exists())

    def test_create_view_persists_document_configuration(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        payload = {
            "position_reference": self.admin_aide_position.pk,
            "branch": PositionPosting.Branch.PLANTILLA,
            "item_number": "OSEC-DOH-AA6-9-2026",
            "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
            "status": PositionPosting.EntryStatus.ACTIVE,
            "publication_date": "",
            "opening_date": self.entry_opening_date().isoformat(),
            "closing_date": "",
            "qualification_reference": "Per-vacancy document configuration create test.",
        }
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA]:
            code = requirement.code
            if code == DIPLOMA:
                continue  # leave unchecked so it is dropped
            payload[f"doc_applies__{code}"] = "on"
            if code not in MIN_REQUIRED_DOCUMENT_CODES and not requirement.conditional_on_performance_rating:
                payload[f"doc_level__{code}"] = "required"

        response = client.post(reverse("recruitment-entry-create"), payload)
        self.assertEqual(response.status_code, 302)
        created = PositionPosting.objects.get(
            qualification_reference="Per-vacancy document configuration create test."
        )
        configured_codes = set(
            created.document_requirements.values_list("document_code", flat=True)
        )
        self.assertNotIn(DIPLOMA, configured_codes)
        self.assertIn(SIGNED_COVER_LETTER, configured_codes)
        self.assertIn(PERSONAL_DATA_SHEET, configured_codes)

    def test_update_view_renders_locked_documents_for_live_posting(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("recruitment-entry-update", kwargs={"pk": posting.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "locked and cannot be changed")
        self.assertContains(response, "disabled")
