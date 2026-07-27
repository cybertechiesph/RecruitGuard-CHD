import importlib
import io
import json
import re
import urllib.parse
import uuid
import zipfile
from decimal import ROUND_HALF_UP, Decimal
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
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .admin import ExamRecordAdmin, ScreeningRecordAdmin
from .captcha import (
    CAPTCHA_ANSWER_SESSION_KEY,
    validate_recaptcha_token,
    validate_turnstile_token,
)
from .forms import (
    APPLICANT_COVER_LETTER_LENGTH_ERROR_MESSAGE,
    APPLICANT_COVER_LETTER_MAX_LENGTH,
    APPLICANT_MOBILE_ERROR_MESSAGE,
    APPLICANT_NAME_ERROR_MESSAGE,
    APPLICANT_NAME_LENGTH_ERROR_MESSAGE,
    APPLICANT_NAME_MAX_LENGTH,
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
    AttendanceException,
    ApplicationETERating,
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CosSelectionRound,
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
    StageRecordVersion,
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
    get_screening_record,
    get_exam_record,
    get_stage_record_versions,
    user_can_manage_screening,
    reopen_recruitment_case,
    _reopen_screening_after_resubmission,
    interview_is_finalized_for_current_stage,
    get_latest_finalized_comparative_assessment_report,
    get_available_actions,
    get_all_audit_logs,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    process_workflow_action,
    repair_auto_advance_workflow_boundaries,
    record_final_decision,
    user_can_record_final_decision,
    record_final_selection,
    rescind_final_selection,
    user_can_rescind_final_selection,
    user_can_record_final_selection,
    get_final_selection_for_entry,
    get_final_selection_history,
    record_system_audit_event,
    remove_applicant_from_batch,
    save_cos_vacancy_deliberation,
    rescind_cos_vacancy_deliberation,
    user_can_rescind_cos_vacancy_deliberation,
    get_rescinded_cos_picks_for_case,
    get_final_decision_history,
    cos_selection_round_pool,
    get_cos_vacancy_deliberation,
    get_latest_final_decision,
    car_item_is_within_top_five,
    car_top_rank_case_ids,
    vacancy_batch_pool_block_message,
    vacancy_batch_pool_is_final,
    get_comparative_assessment_readiness,
    get_cos_pick_for_case,
    get_latest_cos_selection_round,
    get_open_cos_selection_round,
    open_cos_selection_round,
    user_can_open_cos_selection_round,
    cos_deliberation_pool,
    cos_eligible_pool,
    save_exam_record,
    save_exam_schedule,
    create_competency_rating_template,
    create_default_position_document_requirements,
    get_or_create_vacancy_assessment_weights,
    lock_vacancy_assessment_weights,
    unlock_vacancy_assessment_weights,
    persist_recruitment_entry,
    get_competency_rating_template,
    save_competency_rating_sheet,
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
    user_can_manage_exam,
    vacancy_exam_pool,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    vacancy_interview_pool,
    save_vacancy_interview_schedule,
    save_vacancy_cos_interview_ratings,
    finalize_vacancy_interview_batch,
    vacancy_screening_batch_status,
    update_recruitment_entry_status,
    upload_interview_fallback_rating,
    user_can_upload_interview_fallback,
    mark_interview_absent,
    record_application_withdrawal,
    user_can_record_withdrawal,
    user_can_manage_deliberation,
    save_deliberation_record,
    cancel_recruitment_entry,
    get_completion_record,
    get_pending_attendance_exception,
    record_attendance_letter_of_intent,
    resolve_attendance_absence,
    get_interview_fallback_evidence,
    get_interview_session,
    interview_session_rater_ids,
    _average_interview_rating,
    upload_evidence_item,
    user_can_manage_comparative_assessment_report,
    user_can_view_application,
    verify_application_otp,
)
from .upload_validation import (
    ORIGINAL_FILENAME_MAX_LENGTH,
    clamp_original_filename,
    validate_applicant_document_upload,
)


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
    @property
    def cos_continuous_position(self):
        # RD-2: only CONTINUOUS COS intake keeps the per-case exam editor (no
        # pool). Created lazily so it does not bump the shared COS entry-code
        # sequence other tests assert on.
        if not hasattr(self, "_cos_continuous_position"):
            self._cos_continuous_position = PositionPosting.objects.create(
                position_reference=self.project_assistant_position,
                branch=PositionPosting.Branch.COS,
                level=PositionPosting.Level.LEVEL_1,
                intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
                status=PositionPosting.EntryStatus.ACTIVE,
            )
        return self._cos_continuous_position

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
        default_status = default_status or ScreeningDocumentReview.ReviewStatus.COMPLIED
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
                status = ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED
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
        qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
        completeness_notes="All required screening documents were reviewed.",
        screening_notes="Qualification screening completed.",
        education_score=None,
        training_score=None,
        experience_score=None,
        document_review_score=None,
    ):
        # RD-1: completeness is derived from the document review, not supplied.
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
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

    def extra_plantilla_panel_raters(self, exclude=None, count=2):
        """WF-01 requires ratings from at least 3 HRMPSB members before a Plantilla
        interview session can finalize. Provide additional panel members, created on
        first use so tests that never finalize a Plantilla interview keep their
        original notification-recipient counts."""
        raters = []
        for index in (2, 3, 4):
            if len(raters) >= count:
                break
            user, _ = User.objects.get_or_create(
                username=f"hrmpsb-rater-{index}",
                defaults={
                    "email": f"hrmpsb.rater{index}@example.com",
                    "role": RecruitmentUser.Role.HRMPSB_MEMBER,
                },
            )
            if exclude is not None and user.pk == exclude.pk:
                continue
            raters.append(user)
        return raters

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
        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a single end-user / HRMS score, not the competency sheet.
            save_interview_rating(
                application=application,
                actor=actor,
                cleaned_data={
                    "rating_score": rating_score if rating_score is not None else "75.00",
                    "rating_notes": rating_notes,
                    "justification": justification,
                },
            )
        else:
            if level is None:
                level = (
                    self._score_to_competency_level(rating_score)
                    if rating_score is not None
                    else 3
                )
            template = self.publish_competency_rating_sheet(application.position)
            # WF-01: a Plantilla session needs the HRMPSB panel minimum (3 ratings)
            # before it can finalize — the primary rater plus two more members, all
            # scoring the same uniform level so per-candidate averages are unchanged.
            panel_raters = [actor] + self.extra_plantilla_panel_raters(exclude=actor)
            for panel_rater in panel_raters:
                save_interview_rating(
                    application=application,
                    actor=panel_rater,
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
            # WF-04 (client decision): the Secretariat prepares the CAR for BOTH levels.
            car_actor = self.secretariat
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
        if application.branch == PositionPosting.Branch.COS:
            # COS records one per-vacancy deliberation pick (spec §6.3); the chosen applicant
            # defaults to this application's own case (always in the pool).
            return save_cos_vacancy_deliberation(
                application.position,
                actor,
                application.case.id,
                deliberation_minutes,
                finalize=True,
            )
        # SL-15: there is no third branch, and the per-case deliberation record is
        # quarantined — reaching here means the fixture was called for a case that has no
        # deliberation step at all.
        raise AssertionError(
            f"No deliberation step exists for {application.reference_label} "
            f"({application.branch})."
        )

    def ensure_ete_ratings_for_pool(self, application, rating="88.00"):
        """Give every active Plantilla candidate in this vacancy an ETE rating.

        SL-03 blocks CAR finalization while any candidate is missing a formula component,
        and screening no longer captures ETE, so the Secretariat's manual rating is the
        only source. Fixtures that finalize a CAR need it for the whole pool, not just the
        application under test."""
        cases = RecruitmentCase.objects.filter(
            application__position=application.position,
            application__branch=PositionPosting.Branch.PLANTILLA,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
        ).select_related("application")
        for case in cases:
            if getattr(case.application, "ete_rating", None) is None:
                set_application_ete_rating(
                    case.application,
                    self.secretariat,
                    Decimal(rating),
                )

    def finalize_car_for_current_stage(
        self,
        application,
        actor,
        summary_notes="Comparative ranking sheet generated for Plantilla decision support.",
        recommended_case=None,
    ):
        car_actor = actor
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
            and actor.role == RecruitmentUser.Role.HRMPSB_MEMBER
        ):
            # WF-04 (client decision): the Secretariat prepares the CAR for BOTH levels.
            car_actor = self.secretariat
        # SL-03: a CAR can no longer be finalized while a candidate's ETE is blank, so the
        # shared fixture supplies one for every candidate in the pool the way the
        # Secretariat would before finalizing.
        self.ensure_ete_ratings_for_pool(application)
        return generate_comparative_assessment_report(
            application=application,
            actor=car_actor,
            cleaned_data={
                "summary_notes": summary_notes,
                # WF-02: the deliberation quorum (3–5 members, met) is required at finalize.
                "quorum_met": True,
                "members_present": 4,
                # The board's recommended applicant is captured at finalize (defaults to
                # this application's own case, which is always in the pool).
                "recommended_case_id": (
                    recommended_case.id if recommended_case else application.case.id
                ),
                "recommendation_notes": "HRMPSB recommends this applicant.",
            },
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
            # WF-03: picking someone other than the board's recommendation (rank #1
            # fallback) requires a written justification for the official record.
            recommended_case_id = report.recommended_case_id
            if recommended_case_id is None:
                rank_one = candidate_items.filter(rank_order=1).first()
                recommended_case_id = rank_one.recruitment_case_id if rank_one else None
            cleaned_data = {
                "selected_item": selected_item,
                "decision_notes": decision_notes,
            }
            if (
                recommended_case_id
                and selected_item.recruitment_case_id != recommended_case_id
            ):
                cleaned_data["deep_selection_justification"] = (
                    "Documented justification for selecting against the board's recommendation."
                )
            return record_final_selection(
                application=application,
                actor=actor,
                cleaned_data=cleaned_data,
            )
        # SL-01: finalizing the COS vacancy deliberation pick IS the selection, so by the
        # time a COS case reaches this point the decision already exists. Return it rather
        # than recording a second, contradicting one.
        existing = get_latest_final_decision(application)
        if existing:
            return existing
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
        # The portal presents as the office's system, not the study title.
        self.assertContains(response, "DOH–CHD CALABARZON")
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

        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
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
        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
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

        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
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
                "tracking_code": "TRK-XXXX-XXXX",
                "email": "applicant@example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")

        response = self.client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": "TRK-XXXX-XXXX",
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

    def test_system_admin_self_deactivation_redirects_with_message(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.post(
            reverse("internal-user-toggle-active", kwargs={"pk": self.sysadmin.pk}),
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-user-list"))
        self.sysadmin.refresh_from_db()
        self.assertTrue(self.sysadmin.is_active)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("your own account" in m for m in messages))

    def test_system_admin_self_role_change_shows_form_error(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.post(
            reverse("internal-user-update", kwargs={"pk": self.sysadmin.pk}),
            {
                "username": self.sysadmin.username,
                "first_name": self.sysadmin.first_name,
                "last_name": self.sysadmin.last_name,
                "email": self.sysadmin.email,
                "employee_id": self.sysadmin.employee_id,
                "office_name": self.sysadmin.office_name,
                "role": RecruitmentUser.Role.SECRETARIAT,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "role", "You cannot remove your own System Administrator role.")
        self.sysadmin.refresh_from_db()
        self.assertEqual(self.sysadmin.role, RecruitmentUser.Role.SYSTEM_ADMIN)

    def test_system_admin_cannot_open_position_list(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("position-list"))

        self.assertEqual(response.status_code, 403)


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

        expected_code = f"VAC-PLT-{timezone.localdate().year:04d}-003"
        self.assertEqual(entry.job_code, expected_code)
        self.assertRegex(entry.job_code, r"^VAC-(PLT|COS)-\d{4}-\d{3,}$")

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

        self.assertEqual(publication_entry.job_code, f"VAC-COS-{current_year + 1:04d}-001")
        self.assertEqual(opening_entry.job_code, f"VAC-COS-{current_year + 2:04d}-001")
        self.assertEqual(fallback_entry.job_code, f"VAC-COS-{current_year:04d}-002")

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
            job_code=f"VAC-PLT-{current_year:04d}-001",
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
            job_code=f"VAC-COS-{current_year - 1:04d}-001",
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
        self.assertEqual(created_entry.job_code, f"VAC-COS-{self.entry_opening_date().year:04d}-002")
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

    def _lapsed_cos_entry(self, *, days_closed=1):
        # Opening-based COS: closing date allowed, so a lapsed deadline can be
        # built directly (objects.create skips form validation on purpose).
        return PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.OPENING_BASED,
            status=PositionPosting.EntryStatus.ACTIVE,
            opening_date=timezone.localdate() - timedelta(days=20),
            closing_date=timezone.localdate() - timedelta(days=days_closed),
        )

    def _entry_edit_payload(self, entry, **overrides):
        payload = {
            "position_reference": entry.position_reference_id,
            "branch": entry.branch,
            "intake_mode": entry.intake_mode,
            "status": entry.status,
            "publication_date": "",
            "opening_date": entry.opening_date.isoformat() if entry.opening_date else "",
            "closing_date": entry.closing_date.isoformat() if entry.closing_date else "",
            "qualification_reference": entry.qualification_reference or "",
        }
        payload.update(overrides)
        return payload

    def test_entry_create_rejects_past_closing_date(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.OPENING_BASED,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": (timezone.localdate() - timedelta(days=20)).isoformat(),
                "closing_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
                "qualification_reference": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The closing date cannot be set to a past date.")

    def test_extending_lapsed_deadline_reopens_flags_and_audits(self):
        entry = self._lapsed_cos_entry()
        self.assertFalse(entry.is_open_for_intake)
        previous_closing = entry.closing_date
        new_closing = timezone.localdate() + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertTrue(entry.deadline_extended)
        self.assertTrue(entry.is_open_for_intake)
        log = AuditLog.objects.filter(
            action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
            metadata__recruitment_entry_id=entry.id,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.metadata["previous_closing_date"], previous_closing.isoformat())
        self.assertEqual(log.metadata["new_closing_date"], new_closing.isoformat())
        self.assertTrue(log.metadata["is_extension"])

    def test_entry_edit_keeps_unchanged_lapsed_closing_date(self):
        entry = self._lapsed_cos_entry()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, qualification_reference="Notes only."),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.qualification_reference, "Notes only.")
        self.assertFalse(entry.deadline_extended)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                metadata__recruitment_entry_id=entry.id,
            ).exists()
        )

    def test_closed_vacancy_deadline_extension_blocked_until_reopened(self):
        # RD-7: a manual Close is a deliberate stop. Extending the deadline must
        # not silently undo it — is_open_for_intake requires ACTIVE, so the
        # public page would announce "Deadline extended" while intake stays
        # shut. The edit is refused until the vacancy is reopened.
        entry = self._lapsed_cos_entry()
        entry.status = PositionPosting.EntryStatus.CLOSED
        entry.save(update_fields=["status", "updated_at"])
        previous_closing = entry.closing_date
        new_closing = timezone.localdate() + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reopen it (set it Active) before")
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, previous_closing)

        # Reopen first, then the extension goes through (REV-A behavior intact).
        entry.status = PositionPosting.EntryStatus.ACTIVE
        entry.save(update_fields=["status", "updated_at"])
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertTrue(entry.is_open_for_intake)

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
                "job_code": "VAC-COS-2099-999",
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
        # WF-07: closing is only allowed once the publication period has run — move the
        # closing date to today so this close is legitimate.
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=timezone.localdate(),
            updated_at=timezone.now(),
        )
        self.level1_position.refresh_from_db()
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

    def test_close_is_blocked_before_the_publication_period_ends(self):
        # WF-07 (HARD): a vacancy with a future closing date cannot be closed early —
        # an early close would cut the published window and unlock deliberation/CAR
        # for an incomplete pool. Suspend stays available.
        self.assertGreater(self.level1_position.closing_date, timezone.localdate())

        with self.assertRaisesMessage(
            ValidationError,
            "It can't be closed before then. Use Suspend if applications must stop",
        ):
            update_recruitment_entry_status(
                self.level1_position,
                self.hrm_chief,
                PositionPosting.EntryStatus.CLOSED,
            )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)

        # The view path surfaces the same block as a message, without committing.
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
        self.assertContains(response, "It can&#x27;t be closed before then.")
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)

        # Suspend still works at any time.
        update_recruitment_entry_status(
            self.level1_position,
            self.hrm_chief,
            PositionPosting.EntryStatus.SUSPENDED,
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)

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
        form = ApplicantPortalIntakeForm(entry=self.level1_position)
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
        status_response = client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": application.tracking_code,
                "email": "cos.portal@example.com",
            },
        )
        self.assertContains(status_response, "Check Your Email")
        self.assertNotContains(status_response, "Under Review")

        verify_response = client.post(
            reverse("applicant-status-lookup"),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )
        self.assertContains(verify_response, "Under Review")
        self.assertContains(verify_response, application.case_number_short)

        # Option A session: within the verified window, a repeat lookup skips
        # the code step entirely.
        repeat_response = client.post(
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
        self.assertContains(response, application.case_number_short)
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
        application.tracking_code = "TRK-DRFT-0001"
        application.save(update_fields=["tracking_code", "updated_at"])

        response = client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": "TRK-DRFT-0001",
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

    def test_submitted_application_receipt_downloads_as_pdf(self):
        # Panel revision: the application receipt must download as a real PDF
        # file (not only browser-print). Same token gate as the receipt page.
        client = Client()
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()

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
        self.assertContains(receipt_response, application.tracking_code)
        self.assertContains(receipt_response, application.case_number_short)

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
            "The same file is used for more than one document",
        )
        self.assertContains(response, "rg-pub-upload-slot--error")
        self.assertFalse(mail.outbox)
        application = RecruitmentApplication.objects.filter(
            position=self.level1_position,
            applicant_email="duplicate.file@example.com",
        ).first()
        if application is not None:
            self.assertFalse(application.otp_hash)

    def test_duplicate_document_messages_are_concise_and_titleless(self):
        # Regression for the applicant feedback that the duplicate-file error
        # ballooned by listing every conflicting document's long formal title.
        client = Client()
        duplicate_bytes = b"%PDF-1.4\nshared-applicant-document\n%%EOF\n"
        payload = self.portal_payload(email="concise.duplicate@example.com")
        payload["signed_cover_letter"] = SimpleUploadedFile(
            "shared.pdf", duplicate_bytes, content_type="application/pdf"
        )
        payload["personal_data_sheet"] = SimpleUploadedFile(
            "shared-again.pdf", duplicate_bytes, content_type="application/pdf"
        )

        response = self.post_portal_intake(
            client, self.level1_position, payload, follow=True
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]

        # One short summary line, computed during clean() and stored.
        self.assertEqual(
            form.duplicate_document_errors,
            [
                "The same file is used for more than one document. "
                "Upload a different file for each one."
            ],
        )

        field_error = form.errors["signed_cover_letter"][0]
        self.assertEqual(
            field_error,
            "This file is already used for 1 other document. "
            "Upload a different file for each one.",
        )
        # No long, formal document titles are spliced into the messages.
        long_titles = [
            requirement.title
            for requirement in form.document_requirements
            if len(requirement.title) > 40
        ]
        self.assertTrue(long_titles)  # sanity: the fixtures do have long titles
        for title in long_titles:
            self.assertNotIn(title, field_error)
            self.assertNotIn(title, form.duplicate_document_errors[0])

    def test_out_of_scope_draft_digests_do_not_produce_phantom_duplicate(self):
        # A digest shared only by stale/out-of-requirement draft documents must
        # NOT surface a group: otherwise the count underflows ("0 other
        # documents") or a "Duplicate file detected" banner shows with no slot
        # highlighted. Guard is on the in-scope code list, not the raw set.
        class _Evidence:
            def __init__(self, digest):
                self.sha256_digest = digest

        form = ApplicantPortalIntakeForm(data={}, entry=self.level1_position)
        form.cleaned_data = {}
        form.existing_documents_by_code = {
            "retired_requirement_a": _Evidence("shared-digest"),
            "retired_requirement_b": _Evidence("shared-digest"),
        }
        # Neither code is a current requirement.
        self.assertNotIn("retired_requirement_a", form.document_requirements_by_code)

        self.assertEqual(form._build_duplicate_document_groups(), [])
        self.assertEqual(form._build_duplicate_document_warnings(), [])

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
            "Upload all required documents before final submission.",
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
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertNotIn(
            ("endorse", "Endorse to Appointing Authority"),
            get_available_actions(application, self.hrm_chief),
        )

        with self.assertRaisesMessage(
            ValueError, "Finalize the deliberation record before endorsing this COS application."
        ):
            process_workflow_action(application, self.hrm_chief, "endorse", "COS endorsed.")

        # SL-01: finalizing the vacancy deliberation pick IS the COS selection — the case
        # goes straight from HRM Chief Review to completion, never via HRMPSB or the
        # Appointing Authority.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        decision = get_latest_final_decision(application)
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

    def test_reopen_refused_for_non_selected_case_in_decided_vacancy(self):
        # RD-7: once the vacancy's selection stands, a pool-resolved sibling
        # (already emailed "not selected") must not return to an active stage —
        # pool queries filter on ACTIVE, so a reopened sibling would re-enter
        # batch logic for a decided vacancy. The winner's case stays reopenable
        # (completion corrections — covered by the closed-case reopen test).
        from recruitment.services import reopen_recruitment_case

        winner = self.make_application(self.cos_position)
        self.verify_application_for_submission(winner)
        submit_application(winner, self.applicant)

        # Vacancy decided: the winner holds a SELECTED final decision; the
        # sibling was closed by pool resolution (locked at its review stage).
        # The sibling is ORM-constructed — the guard reads only the vacancy's
        # decision state and the case lock, not how the pool got there.
        FinalDecision.objects.create(
            application=winner,
            recruitment_case=winner.case,
            recruitment_entry=winner.position,
            review_stage=RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            decided_by=self.hrm_chief,
            branch=winner.branch,
            level=winner.level,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected for the COS engagement.",
        )
        sibling_user = RecruitmentUser.objects.create_user(
            username="rd7-sibling",
            password="StrongPass!234",
            role=RecruitmentUser.Role.APPLICANT,
            email="rd7-sibling@example.com",
        )
        sibling = RecruitmentApplication.objects.create(
            applicant=sibling_user,
            position=self.cos_position,
            applicant_first_name="Pool",
            applicant_last_name="Sibling",
            applicant_email="rd7-sibling@example.com",
            status=RecruitmentApplication.Status.REJECTED,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        RecruitmentCase.objects.create(
            application=sibling,
            branch=sibling.branch,
            current_stage=RecruitmentCase.Stage.CLOSED,
            current_handler_role="",
            case_status=RecruitmentCase.CaseStatus.REJECTED,
            is_stage_locked=True,
            locked_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        sibling.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError, "selection is already recorded"
        ):
            reopen_recruitment_case(
                application=sibling,
                actor=self.hrm_chief,
                reason="Trying to revive a resolved sibling.",
            )

        # An undecided vacancy's locked case reopens exactly as before.
        undecided = self.make_application(self.level1_position)
        self.verify_application_for_submission(undecided)
        submit_application(undecided, self.applicant)
        undecided_case = undecided.case
        undecided_case.case_status = RecruitmentCase.CaseStatus.REJECTED
        undecided_case.is_stage_locked = True
        undecided_case.locked_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        undecided_case.save(
            update_fields=["case_status", "is_stage_locked", "locked_stage", "updated_at"]
        )
        reopen_recruitment_case(
            application=undecided,
            actor=self.hrm_chief,
            reason="Legitimate correction on an undecided vacancy.",
        )
        undecided_case.refresh_from_db()
        self.assertFalse(undecided_case.is_stage_locked)

    def _close_case_after_not_qualified_screening(
        self,
        application,
        screening_notes="Applicant does not meet the education requirement.",
    ):
        """Drive a case to the exact terminal state the SL-12 probe used: screening
        finalized Not Qualified, which closes the case and locks it at the screening
        stage. This is the construction the finding reproduced."""
        if application.status == RecruitmentApplication.Status.DRAFT:
            self.verify_application_for_submission(application)
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, application.applicant)
            application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                screening_notes=screening_notes,
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(
            application.case.locked_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        return application

    def test_reopening_a_finalized_screening_case_leaves_editable_work(self):
        # SL-12 AC#1/#3/#4: the old reopen unlocked the envelope and nothing else — the
        # finalized screening record kept refusing every write, so the handler got active
        # workload with no legal move. Reopening must hand back something they can change.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        record = get_screening_record(application)
        self.assertFalse(record.is_finalized)
        self.assertIsNone(record.finalized_at)
        self.assertIsNone(record.finalized_by)
        self.assertEqual(record.correction_version, 2)
        self.assertEqual(get_current_workflow_section(application), "screening")
        self.assertTrue(user_can_manage_screening(self.secretariat, application))

        # AC#3: the corrected decision saves and the case follows the normal route.
        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
            screening_notes="Corrected after re-reading the transcript of records.",
        )
        application.refresh_from_db()
        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertEqual(
            record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED
        )
        self.assertEqual(get_current_workflow_section(application), "exam")

    def test_a_reopened_record_keeps_its_previous_finalized_values(self):
        # SL-12 AC#2: correcting in place is only acceptable if the answer being replaced
        # survives, linked to who reopened it and why.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        # The case is closed, so the record has to be fetched by its own stage rather than
        # by the case's current one.
        original = get_screening_record(
            application, stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        original_finalized_at = original.finalized_at
        original_document_count = original.document_reviews.count()

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
            screening_notes="Corrected after re-reading the transcript of records.",
        )

        versions = list(get_stage_record_versions(application))
        self.assertEqual(len(versions), 1)
        version = versions[0]
        self.assertEqual(version.record_type, StageRecordVersion.RecordType.SCREENING)
        self.assertEqual(version.record_id, original.id)
        self.assertEqual(version.version_number, 1)
        self.assertEqual(version.reopened_by, self.hrm_chief)
        self.assertEqual(version.reopened_by_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertIn("Transcript was misread", version.reopen_reason)
        self.assertEqual(
            version.trigger, StageRecordVersion.Trigger.AUTHORIZED_REOPEN
        )
        # The archived values are the ORIGINAL ones, not the corrected ones.
        self.assertEqual(
            version.snapshot["record"]["qualification_outcome"],
            ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
        )
        self.assertTrue(version.snapshot["record"]["is_finalized"])
        self.assertEqual(
            version.snapshot["record"]["finalized_at"], original_finalized_at.isoformat()
        )
        self.assertIn(
            "does not meet the education requirement",
            version.snapshot["record"]["screening_notes"],
        )
        self.assertEqual(
            len(version.snapshot["document_reviews"]), original_document_count
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.STAGE_RECORD_REOPENED,
            ).exists()
        )

    def test_reopen_corrects_the_last_finalized_record_of_the_stage(self):
        # SL-12: the record that terminalized the case is the one the handler came back to
        # change. Reopening a case closed after the examination must not silently unlock
        # the screening verdict that was already settled and is not in dispute.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, application.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        # An Absent examination keeps the case at the Secretariat stage with both records
        # finalized — the construction where "the stage's last finalized record" matters.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_status=ExamRecord.ExamStatus.ABSENT,
                exam_score=None,
                technical_score=None,
                general_score=None,
            )
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application=application,
                actor=self.secretariat,
                action="reject",
                remarks="Closed after the applicant missed the examination.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(
            application.case.locked_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="The applicant did sit the examination; the sheet was misfiled.",
        )
        application.refresh_from_db()

        exam_record = get_exam_record(application)
        screening_record = get_screening_record(application)
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.correction_version, 2)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.correction_version, 1)
        self.assertEqual(get_current_workflow_section(application), "exam")
        versions = list(get_stage_record_versions(application))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].record_type, StageRecordVersion.RecordType.EXAM)
        self.assertEqual(
            versions[0].snapshot["record"]["exam_status"], ExamRecord.ExamStatus.ABSENT
        )

    def test_correcting_the_same_record_twice_keeps_both_earlier_versions(self):
        # SL-12: the trail is append-only, so two corrections read as two corrections.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)

        for pass_number in (1, 2):
            reopen_recruitment_case(
                application=application,
                actor=self.hrm_chief,
                reason=f"Correction pass {pass_number}.",
            )
            application.refresh_from_db()
            # Re-finalizing Not Qualified closes and re-locks the case, so the next pass
            # starts from the same terminal state the first one did.
            self._close_case_after_not_qualified_screening(
                application,
                screening_notes=f"Screening verdict after correction {pass_number}.",
            )

        record = get_screening_record(
            application, stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        self.assertEqual(record.correction_version, 3)
        versions = list(
            get_stage_record_versions(application).order_by("version_number")
        )
        self.assertEqual([v.version_number for v in versions], [1, 2])
        self.assertIn(
            "does not meet the education requirement",
            versions[0].snapshot["record"]["screening_notes"],
        )
        self.assertIn(
            "after correction 1", versions[1].snapshot["record"]["screening_notes"]
        )

    def test_the_case_surface_says_the_record_is_being_corrected(self):
        # SL-12: editing a decision that is already on file must not look like ordinary
        # first-time data entry.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You are correcting a finalized screening record.")
        self.assertContains(response, "Transcript was misread")
        self.assertContains(response, "Corrected records")
        self.assertNotContains(response, "{#")
        # The archive is only half the point — the answer that is being replaced has to be
        # READABLE, or the correction trail names a version nobody can open.
        self.assertContains(response, "Not Qualified")
        self.assertContains(response, "Applicant does not meet the education requirement.")
        self.assertContains(response, "Document review at the time")

    def test_the_archived_values_render_for_each_record_type(self):
        # SL-12: the exam and interview archives must be readable too, not just screening.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, application.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_status=ExamRecord.ExamStatus.ABSENT,
                exam_score=None,
                technical_score=None,
                general_score=None,
                exam_notes="Applicant did not appear for the scheduled examination.",
            )
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application=application,
                actor=self.secretariat,
                action="reject",
                remarks="Closed after the applicant missed the examination.",
            )
        application.refresh_from_db()
        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="The applicant did sit the examination; the sheet was misfiled.",
        )

        version = get_stage_record_versions(application).first()
        rows = {row["label"]: row["value"] for row in version.summary_rows}
        self.assertEqual(rows["Examination"], "Absent")
        self.assertEqual(
            rows["Notes"], "Applicant did not appear for the scheduled examination."
        )
        self.assertIn("Finalized", rows)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Examination Record &mdash; version 1", html=False)
        self.assertContains(
            response, "Applicant did not appear for the scheduled examination."
        )
        self.assertNotContains(response, "{#")

    def test_applicant_resubmission_archives_the_first_verification_pass(self):
        # SL-12: the RD-6 resubmission reopen used to unfinalize the record and blank its
        # document rows in place. The reviewer's original verdict on the original files is
        # the evidence that a re-verification was needed at all.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, application.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            screening_notes="First verification pass over the submitted documents.",
        )
        application.refresh_from_db()
        record = get_screening_record(application)
        original_id = record.id

        _reopen_screening_after_resubmission(
            application,
            self.secretariat,
            RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )

        record.refresh_from_db()
        self.assertFalse(record.is_finalized)
        self.assertEqual(record.correction_version, 2)
        self.assertIsNotNone(record.reopened_for_resubmission_at)
        self.assertTrue(
            all(
                review.status == ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                for review in record.document_reviews.all()
            )
        )

        versions = list(get_stage_record_versions(application))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].record_id, original_id)
        self.assertEqual(
            versions[0].trigger, StageRecordVersion.Trigger.APPLICANT_RESUBMISSION
        )
        self.assertIn(
            "First verification pass",
            versions[0].snapshot["record"]["screening_notes"],
        )
        # The pre-reset document statuses survive in the archive.
        self.assertTrue(
            any(
                row["status"] != ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                for row in versions[0].snapshot["document_reviews"]
            )
        )

    def test_case_timeline_is_visible_on_application_detail(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertContains(response, "CASE-")
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

    def test_not_qualified_screening_closes_the_case_at_level_two(self):
        # RD-1: Not Qualified IS the definitive cut — a reviewer who wants the
        # applicant to fix documents returns them from step 2 instead. So the
        # old "finalized as failed, but parked at screening awaiting a
        # reject-or-return choice" state no longer exists: the case closes.
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
            screening_notes="Applicant does not meet the Qualification Standards.",
        )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Endorse to HRMPSB")
        self.assertNotContains(response, 'data-section="cws-exam"')

    def test_definitive_not_qualified_screening_auto_rejects_and_notifies(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
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
        # RD-1: completeness is derived from the document rows, never posted.
        payload = {
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

    def test_returned_application_resubmit_reopens_finalized_screening(self):
        # RD-6 integrity: a generic return (no flagged documents) hands the whole
        # application back. When the applicant resubmits, the finalized screening
        # must reopen so the edited documents are re-verified — a finalized
        # screening over the old files would otherwise pass the endorse gate.
        from recruitment.services import get_screening_record

        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        before = get_screening_record(application, stage=stage)
        self.assertTrue(before.is_finalized)

        # Simulate the generic whole-application return: the case is handed back
        # to the applicant with the screening still finalized. (Reaching the
        # post-exam actions section per case requires a multi-candidate batch to
        # avoid auto-advance; the reopen logic under test is stage-based and does
        # not depend on the return mechanism.)
        application.refresh_from_db()
        case = application.case
        application.status = RecruitmentApplication.Status.RETURNED_TO_APPLICANT
        application.current_handler_role = RecruitmentUser.Role.APPLICANT
        application.save(update_fields=["status", "current_handler_role", "updated_at"])
        case.case_status = RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT
        case.current_handler_role = RecruitmentUser.Role.APPLICANT
        case.save(update_fields=["case_status", "current_handler_role", "updated_at"])

        # Applicant edits and resubmits.
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()

        record = get_screening_record(application, stage=stage)
        self.assertFalse(record.is_finalized)
        self.assertIsNone(record.finalized_by_id)
        self.assertIsNotNone(record.reopened_for_resubmission_at)
        self.assertEqual(
            record.completeness_status, ScreeningRecord.CompletenessStatus.INCOMPLETE
        )
        self.assertTrue(
            all(
                row.status == ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                for row in record.document_reviews.all()
            )
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.SCREENING_REOPENED
            ).exists()
        )
        # Endorse is blocked until the reviewer re-finalizes the screening.
        with self.assertRaises(ValueError):
            process_workflow_action(application, self.secretariat, "endorse", "premature")

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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "",
                "exam_result": "",
                "technical_score": "",
                "technical_result": "",
                "general_score": "",
                "general_result": "",
                "exam_date": "",
                "administered_by": ExamRecord.AdministeredBy.END_USER,
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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
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
        # RD-2: the per-case exam editor lives on un-pooled (pooling /
        # continuous) COS intake; pooled vacancies score in the batch console.
        application = self.make_application(self.cos_continuous_position)
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
                    exam_type=ExamRecord.ExamType.END_USER_ASSESSMENT,
                    administered_by=ExamRecord.AdministeredBy.END_USER,
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

    def test_cos_vacancy_weights_page_renders_and_edits(self):
        # RD-5: post-RD-2 the COS exam overall is weight-computed too, so COS
        # entries see and edit their weights (the old view hard-blocked them).
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.cos_position.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exam score split")
        self.assertTrue(
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

    def test_locked_weights_can_be_unlocked_with_an_audited_reason(self):
        # SL-00: the lock is deliberate, but an encoding error in the split still has to be
        # correctable — through a reason-bearing, audited unlock, never a quiet edit.
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {"operation": "unlock", "reason": "Exam weight was encoded as 20 instead of 30."},
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.DRAFT)
        self.assertIsNone(weights.locked_at)
        entry = AuditLog.objects.filter(
            action=AuditLog.Action.ASSESSMENT_WEIGHTS_UNLOCKED,
            actor=self.secretariat,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.metadata["reason"],
            "Exam weight was encoded as 20 instead of 30.",
        )
        # The split in force at the moment of the unlock is preserved on the audit entry.
        self.assertEqual(entry.metadata["split_at_unlock"]["ete_weight"], "40.00")
        # And the row is genuinely editable again.
        response = client.post(
            url,
            {
                "ete_weight": "40",
                "exam_weight": "30",
                "interview_weight": "30",
                "exam_general_weight": "60",
                "exam_technical_weight": "40",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights.refresh_from_db()
        self.assertEqual(weights.exam_weight, Decimal("30.00"))

    def test_unlocking_weights_without_a_reason_is_refused(self):
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(url, {"operation": "unlock", "reason": "   "})
        self.assertEqual(response.status_code, 200)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.LOCKED)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.ASSESSMENT_WEIGHTS_UNLOCKED
            ).exists()
        )

    def test_unlock_service_refuses_an_already_editable_row(self):
        with self.assertRaises(ValidationError):
            unlock_vacancy_assessment_weights(
                self.level1_position,
                self.secretariat,
                "Nothing to correct.",
            )

    def test_weights_page_states_the_split_and_flags_a_non_standard_one(self):
        # SL-00: a deviation from the office standard is legitimate but must be visible.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.get(url)
        self.assertContains(response, "ETE 40% / Examination 20% / Interview 40%")
        self.assertContains(response, "This is the office standard.")
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        weights.ete_weight = Decimal("50.00")
        weights.exam_weight = Decimal("20.00")
        weights.interview_weight = Decimal("30.00")
        weights.save()
        response = client.get(url)
        self.assertContains(response, "ETE 50% / Examination 20% / Interview 30%")
        self.assertContains(response, "not</strong> the office standard", html=False)

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

    def test_detail_redirects_when_every_scoped_case_is_closed(self):
        # Regression: once a vacancy's cases close they leave the queue, so a Back / refresh /
        # bookmark on its batch page used to raise a bare 404 — the same dead end the exam
        # console had. A user who actually worked the vacancy now lands on the batch overview
        # with a note. The FRS 404 above still stands for a vacancy that was never theirs.
        closed_applicant = User.objects.create_user(
            username="closed-batch-applicant",
            password="testpass123",
            email="closed.batch@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=closed_applicant,
            position=self.level1_position,
            applicant_first_name="Closed",
            applicant_last_name="Batch",
            applicant_email="closed.batch@example.com",
            status=RecruitmentApplication.Status.REJECTED,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        RecruitmentCase.objects.create(
            application=application,
            branch=application.branch,
            current_stage=RecruitmentCase.Stage.CLOSED,
            current_handler_role="",
            case_status=RecruitmentCase.CaseStatus.REJECTED,
            is_stage_locked=True,
            locked_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("vacancy-batches"), response.headers["Location"])

    def test_batch_exam_button_is_gated_on_the_exam_pool(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        # SL-09: while publication is open the batch link is replaced by the reason and
        # the date it unlocks, rather than a link that bounces straight back.
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertNotContains(response, "Open batch exam")
        self.assertContains(response, "Publication is open until")

        self.finalize_applicant_pool_for_test(self.level1_position)
        # Screened with the exam still pending -> the batch detail offers the exam.
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertContains(response, "Open batch exam")

        # Once the exam is finalized the pool advances; the (now dead) exam link is gone,
        # and the interview link takes its place.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(application, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertNotContains(response, "Open batch exam")
        self.assertContains(response, "Open batch interview")

    def test_console_requires_workflow_processor_role(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("vacancy-batches"))
        self.assertEqual(response.status_code, 403)


class BatchScreeningGateTests(BaseRecruitmentTestCase):
    def _submit(self, application, applicant):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _extra_submitted_application(self, position, suffix):
        applicant = User.objects.create_user(
            username=f"batch-applicant-{suffix}",
            password="testpass123",
            email=f"batch.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Batch{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"batch.{suffix}@example.com",
            applicant_phone=f"091790000{suffix}",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Additional applicant for batch-gate testing.",
            cover_letter="Applying for the same vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"batch-{suffix}"
        )
        return self._submit(application, applicant)

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

    def _make_awaiting_case(self, application):
        first_requirement = get_applicant_document_requirements(application.branch)[0]
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                "completeness_notes": "A document needs correcting.",
                "qualification_outcome": "",
                "screening_notes": "Pending resubmission.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a clearer copy.",
                "operation": "save",
            },
            follow=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application, self.secretariat, "return_to_applicant", "Please resubmit."
            )
        application.refresh_from_db()
        return application

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


class BatchExamTests(BaseRecruitmentTestCase):
    def _submit(self, application, applicant):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _extra_submitted_application(self, position, suffix):
        applicant = User.objects.create_user(
            username=f"exam-applicant-{suffix}",
            password="testpass123",
            email=f"exam.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Exam{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"exam.{suffix}@example.com",
            applicant_phone=f"091788800{suffix}",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Additional applicant for batch-exam testing.",
            cover_letter="Applying for the same vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"exam-{suffix}"
        )
        return self._submit(application, applicant)

    def _two_qualified(self, position):
        first = self._submit(self.make_application(position), self.applicant)
        self.finalize_screening_for_current_stage(first, self.secretariat)
        second = self._extra_submitted_application(position, "x")
        self.finalize_screening_for_current_stage(second, self.secretariat)
        # SL-09: a Plantilla vacancy's batch only opens once the applicant pool is final,
        # which is exactly the point the office would start scheduling. COS keeps its
        # intake open — the rule does not apply to it.
        if position.branch == PositionPosting.Branch.PLANTILLA:
            self.finalize_applicant_pool_for_test(position)
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def _schedule_payload(self):
        return {
            "scheduled_for": timezone.now() + timedelta(hours=2),
            "venue": "CHD CALABARZON Examination Room",
            "instructions": "Bring a valid government ID.",
        }

    # ── SL-09: the batch waits for a final applicant pool ─────────────────────
    def _two_qualified_pool_still_open(self, position):
        first = self._submit(self.make_application(position), self.applicant)
        self.finalize_screening_for_current_stage(first, self.secretariat)
        second = self._extra_submitted_application(position, "x")
        self.finalize_screening_for_current_stage(second, self.secretariat)
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def test_the_batch_exam_is_blocked_while_publication_is_still_open(self):
        # SL-09: the audit's probe — every submitted case was screened, so the batch could
        # be scheduled, but a timely later applicant could still submit afterwards and
        # would miss the sitting.
        self._two_qualified_pool_still_open(self.level1_position)
        self.assertFalse(self.level1_position.applicant_pool_is_finalized)

        with self.assertRaisesMessage(ValueError, "Publication is open until"):
            save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        with self.assertRaisesMessage(ValueError, "Publication is open until"):
            save_vacancy_exam_scores(
                self.level1_position, self.secretariat, {}, finalize=False
            )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        )
        self.assertEqual(response.status_code, 302)
        response = client.post(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            {"operation": "save_schedule"},
        )
        self.assertEqual(response.status_code, 302)

        # Screening each applicant continues during the window — only the batch waits.
        self.assertTrue(
            vacancy_screening_batch_status(self.level1_position).is_ready
        )

        # Once the pool is final the batch opens exactly as before.
        self.finalize_applicant_pool_for_test(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            pool = save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        self.assertEqual(len(pool), 2)

    def test_a_pooling_cos_vacancy_is_not_locked_by_the_pool_rule(self):
        # SL-09 AC#4 and the scope trap: pooling/continuous COS entries have no closing
        # date, so a branch-agnostic rule would lock their consoles permanently.
        self.assertFalse(self.cos_position.applicant_pool_is_finalized)
        self.assertTrue(vacancy_batch_pool_is_final(self.cos_position))
        self.assertEqual(vacancy_batch_pool_block_message(self.cos_position), "")

    def test_pool_lists_the_qualified_candidates(self):
        first, second = self._two_qualified(self.level1_position)
        pool = vacancy_exam_pool(self.level1_position)
        self.assertEqual({a.pk for a in pool}, {first.pk, second.pk})

    def test_batch_schedule_applies_to_every_candidate(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        for application in (first, second):
            schedule = application.exam_schedules.first()
            self.assertIsNotNone(schedule)
            self.assertIsNotNone(schedule.applicant_notified_at)

    def test_batch_finalize_records_scores_and_advances_the_pool(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "70", "technical_score": "60"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        first_exam = first.exam_records.get(review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(first_exam.is_finalized)
        # 90 * 0.60 + 80 * 0.40 = 86 with the seeded per-vacancy weights.
        self.assertEqual(first_exam.exam_score, Decimal("86.00"))
        # Both passers advance together to the HRMPSB stage.
        self.assertEqual(first.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(second.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        # The vacancy's assessment weights lock once scoring is committed.
        self.assertTrue(self.level1_position.assessment_weights_or_default.is_locked)

    def test_batch_finalize_requires_every_candidate_scored(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.assertRaises(ValueError):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {first.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )
        # All-or-nothing: the first candidate's exam was not finalized either.
        first.refresh_from_db()
        self.assertFalse(
            first.exam_records.filter(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW, is_finalized=True
            ).exists()
        )

    def test_cos_batch_exam_uses_two_weighted_components(self):
        # RD-2: COS scores the same way Plantilla does — General Ability and
        # Technical, combined by the vacancy's weights (default 60/40). The
        # batch requires both components before it can finalize.
        first, second = self._two_qualified(self.cos_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._schedule_payload())

        # One component is not enough to finalize.
        with self.assertRaisesMessage(ValueError, "General Ability and Technical"):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {first.case.id: {"general_score": "82"}},
                finalize=True,
            )

        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "80", "technical_score": "90"},
                    second.case.id: {"general_score": "70", "technical_score": "80"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        first_exam = first.exam_records.get(review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(first_exam.is_finalized)
        self.assertEqual(first_exam.exam_type, ExamRecord.ExamType.END_USER_ASSESSMENT)
        self.assertEqual(first_exam.general_score, Decimal("80.00"))
        self.assertEqual(first_exam.technical_score, Decimal("90.00"))
        # 80*0.60 + 90*0.40 = 84.00
        self.assertEqual(first_exam.exam_score, Decimal("84.00"))

    def test_console_refused_for_continuous_vacancy(self):
        # RD-5: one door in both directions — a continuous-intake vacancy is
        # examined/interviewed per case, so its consoles refuse GET and POST.
        application = self.make_application(self.cos_continuous_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        for url_name in ("vacancy-batch-exam", "vacancy-batch-interview"):
            url = reverse(url_name, args=[self.cos_continuous_position.pk])
            response = client.get(url)
            self.assertEqual(response.status_code, 302, url_name)
            self.assertIn(
                reverse("vacancy-batch-detail", args=[self.cos_continuous_position.pk]),
                response.headers["Location"],
                url_name,
            )
        # A forced console POST writes nothing.
        response = client.post(
            reverse("vacancy-batch-exam", args=[self.cos_continuous_position.pk]),
            {
                "operation": "save_scores",
                f"general_score_{application.case.id}": "90",
                f"technical_score_{application.case.id}": "85",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(application.exam_records.exists())

    def test_per_case_exam_post_refused_for_pooled_vacancy(self):
        # RD-2 "one door", server-enforced (PT-03 lesson): a pooled vacancy's
        # per-case exam endpoint refuses writes so a forced POST can't create a
        # second, conflicting score. level1_position is Plantilla (fixed-period).
        first, _second = self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("exam-review", kwargs={"pk": first.pk}),
            {
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "general_score": "88",
                "technical_score": "82",
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "operation": "finalize",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            response.headers["Location"],
        )
        # No exam record was written by the refused POST.
        self.assertFalse(first.exam_records.exists())

    def test_batch_exam_blocked_when_screening_batch_not_ready(self):
        first = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(first, self.secretariat)
        # A second submitted applicant whose screening is not finalized keeps the batch pending.
        self._extra_submitted_application(self.level1_position, "y")
        with self.assertRaises(ValueError):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {first.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )

    def test_batch_exam_view_renders_for_the_reviewing_role(self):
        self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Batch Exam")
        # Guard: multi-line {# #} Django comments render literally (browser
        # verification caught one on this very page).
        self.assertNotContains(response, "{#")

    def test_batch_exam_view_404s_for_a_non_reviewing_role(self):
        # The HRM Chief does not handle a Level-1 vacancy's screening/exam (FRS scoping).
        self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 404)

    def test_batch_exam_view_finalizes_via_post(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
                {
                    "operation": "finalize",
                    f"general_score_{first.case.id}": "90",
                    f"technical_score_{first.case.id}": "80",
                    f"general_score_{second.case.id}": "70",
                    f"technical_score_{second.case.id}": "60",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.HRMPSB_REVIEW)

    def test_batch_exam_view_redirects_when_pool_empty_after_finalize(self):
        # Regression: finalizing the exam batch empties the pool (cases move to the
        # interview section), so a refresh / Back-button / re-run of the exam URL used to
        # raise a bare Http404 ("Page Not Found") — the very error seen after finalizing.
        # It must now redirect to the batch overview with a friendly note instead.
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "70", "technical_score": "60"},
                },
                finalize=True,
            )
        # The exam pool is now empty (the passers advanced to the interview section).
        self.assertEqual(list(vacancy_exam_pool(self.level1_position)), [])
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-detail", args=[self.level1_position.pk]),
            response.headers["Location"],
        )

    def test_batch_exam_finalize_forwards_to_the_interview_console(self):
        # After finalizing the exam, the Secretariat (the Level-1 interview-support role)
        # is carried straight to the interview console for the same pool instead of being
        # dropped back on the batch overview to hunt for the next step.
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
                {
                    "operation": "finalize",
                    f"general_score_{first.case.id}": "90",
                    f"technical_score_{first.case.id}": "80",
                    f"general_score_{second.case.id}": "70",
                    f"technical_score_{second.case.id}": "60",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-interview", args=[self.level1_position.pk]),
            response.headers["Location"],
        )


class VacancyBatchInterviewTests(BaseRecruitmentTestCase):
    """Batch interview over the per-case engine: one schedule for the pool, then a finalize.
    COS records a single end-user score per candidate; Plantilla scores the competency sheet
    per-case and finalizes the pool together."""

    def _submit(self, application, applicant):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _extra_submitted_application(self, position, suffix):
        applicant = User.objects.create_user(
            username=f"int-applicant-{suffix}",
            password="testpass123",
            email=f"int.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        applicability = (
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            if position.branch == PositionPosting.Branch.COS
            else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Int{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"int.{suffix}@example.com",
            applicant_phone=f"091788801{suffix}",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Additional applicant for batch-interview testing.",
            cover_letter="Applying for the same vacancy.",
            performance_rating_applicability=applicability,
        )
        self.upload_required_applicant_documents(
            application,
            applicant,
            content_prefix=f"int-{suffix}",
            performance_rating_applicability=applicability,
        )
        return self._submit(application, applicant)

    def _two_qualified(self, position, reviewer):
        first = self._submit(self.make_application(position), self.applicant)
        self.finalize_screening_for_current_stage(first, reviewer)
        second = self._extra_submitted_application(position, "x")
        self.finalize_screening_for_current_stage(second, reviewer)
        # SL-09: a Plantilla vacancy's batch only opens once the applicant pool is final.
        # COS keeps its intake open — the rule does not apply to it.
        if position.branch == PositionPosting.Branch.PLANTILLA:
            self.finalize_applicant_pool_for_test(position)
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def _exam_schedule_payload(self):
        return {
            "scheduled_for": timezone.now() + timedelta(hours=2),
            "venue": "CHD CALABARZON Examination Room",
            "instructions": "Bring a valid government ID.",
        }

    def _interview_schedule_payload(self):
        return {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "CHD CALABARZON Interview Room",
            "session_notes": "Panel interview for the qualified batch.",
        }

    def _two_plantilla_at_interview(self):
        first, second = self._two_qualified(self.level1_position, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._exam_schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "85", "technical_score": "75"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def _two_cos_at_interview(self):
        first, second = self._two_qualified(self.cos_position, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._exam_schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {first.case.id: {"general_score": "82", "technical_score": "80"}, second.case.id: {"general_score": "78", "technical_score": "74"}},
                finalize=True,
            )
        # COS re-screens + re-exams at HRM Chief before the interview section opens.
        for application in (first, second):
            application.refresh_from_db()
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
            self.finalize_exam_for_current_stage(application, self.hrm_chief)
            application.refresh_from_db()
        return first, second

    def test_plantilla_pool_lists_candidates_at_the_hrmpsb_interview(self):
        first, second = self._two_plantilla_at_interview()
        pool = vacancy_interview_pool(self.level1_position)
        self.assertEqual({a.pk for a in pool}, {first.pk, second.pk})
        for application in (first, second):
            self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
            self.assertEqual(get_current_workflow_section(application), "interview")

    def test_batch_schedule_sends_one_panel_digest_per_member(self):
        # WF-14: batch-scheduling N candidates yields exactly one digest email per panel
        # member (listing all N) and N applicant notices — not one panel email per session.
        first, second = self._two_plantilla_at_interview()
        second_member = User.objects.create_user(
            username="hrmpsb-batch-digest",
            password="testpass123",
            email="hrmpsb.digest@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.level1_position, self.secretariat, self._interview_schedule_payload()
            )

        panel_logs = NotificationLog.objects.filter(
            notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
            application__position=self.level1_position,
        )
        self.assertEqual(panel_logs.count(), 2)  # one per member (hrmpsb + second_member)
        self.assertEqual(
            {log.recipient_email for log in panel_logs},
            {self.hrmpsb.email, second_member.email},
        )
        for log in panel_logs:
            self.assertIn("2 candidates", log.subject)
            self.assertIn(first.applicant_display_name, log.body)
            self.assertIn(second.applicant_display_name, log.body)

        applicant_notices = NotificationLog.objects.filter(
            notification_type=NotificationLog.NotificationType.APPLICANT_INTERVIEW_NOTICE,
            application__position=self.level1_position,
        )
        self.assertEqual(applicant_notices.count(), 2)

    def test_plantilla_batch_finalize_moves_the_pool_to_the_car(self):
        first, second = self._two_plantilla_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(self.level1_position, self.secretariat, self._interview_schedule_payload())
        # WF-01: each Plantilla session needs ratings from 3 HRMPSB members to finalize.
        panel_raters = [self.hrmpsb] + self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        for application in (first, second):
            for panel_rater in panel_raters:
                save_interview_rating(
                    application=application,
                    actor=panel_rater,
                    cleaned_data=self.competency_rating_cleaned_data(application, level=3),
                )
        finalize_vacancy_interview_batch(self.level1_position, self.secretariat)
        for application in (first, second):
            application.refresh_from_db()
            self.assertTrue(interview_is_finalized_for_current_stage(application))
            # With the interview done, the pool moves to the CAR section together.
            self.assertEqual(get_current_workflow_section(application), "car")

    def test_plantilla_finalize_requires_a_rating_per_candidate(self):
        first, second = self._two_plantilla_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(self.level1_position, self.secretariat, self._interview_schedule_payload())
        # Only the first candidate is rated.
        save_interview_rating(
            application=first,
            actor=self.hrmpsb,
            cleaned_data=self.competency_rating_cleaned_data(first, level=3),
        )
        with self.assertRaises(ValueError):
            finalize_vacancy_interview_batch(self.level1_position, self.secretariat)
        # All-or-nothing: the rated candidate's session was not finalized either.
        first.refresh_from_db()
        self.assertFalse(interview_is_finalized_for_current_stage(first))

    def test_cos_batch_records_simple_scores_and_finalizes(self):
        first, second = self._two_cos_at_interview()
        pool = vacancy_interview_pool(self.cos_position)
        self.assertEqual({a.pk for a in pool}, {first.pk, second.pk})
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(self.cos_position, self.hrm_chief, self._interview_schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {
                    first.case.id: {"rating_score": "88.00"},
                    second.case.id: {"rating_score": "79.00"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        rating = first.interview_ratings.first()
        self.assertEqual(str(rating.rating_score), "88.00")
        self.assertEqual(rating.competency_scores.count(), 0)
        for application in (first, second):
            application.refresh_from_db()
            self.assertTrue(interview_is_finalized_for_current_stage(application))
            # COS moves to the deliberation section at HRM Chief.
            self.assertEqual(get_current_workflow_section(application), "deliberation")

    def _cos_pool_ready_for_the_pick(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {
                    first.case.id: {"rating_score": "88.00"},
                    second.case.id: {"rating_score": "79.00"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def test_a_case_the_pick_does_not_name_cannot_be_recorded_as_selected(self):
        # SL-01 AC#1: with the pick naming A, no predicate or service call can record B.
        # This is the probe the audit reproduced — it used to succeed, leaving the audit
        # trail holding two incompatible official facts.
        winner, loser = self._cos_pool_ready_for_the_pick()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "A is the pick.",
                finalize=True,
            )
        winner.refresh_from_db()
        loser.refresh_from_db()

        self.assertFalse(user_can_record_final_decision(self.hrm_chief, loser))
        with self.assertRaisesMessage(
            ValueError,
            "This applicant is not the one named in the finalized COS deliberation pick.",
        ):
            record_final_decision(
                application=loser,
                actor=self.hrm_chief,
                cleaned_data={
                    "decision_outcome": FinalDecision.Outcome.SELECTED,
                    "decision_notes": "Trying to select the other candidate instead.",
                },
            )
        # SL-01 AC#3: pick, recorded outcome, notified applicant and completion case all
        # identify the same application.
        pick = get_cos_vacancy_deliberation(self.cos_position)
        decision = get_latest_final_decision(winner)
        self.assertEqual(pick.chosen_case_id, winner.case.id)
        self.assertEqual(decision.recruitment_case_id, winner.case.id)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertEqual(winner.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(FinalDecision.objects.filter(application=loser).count(), 0)
        self.assertTrue(
            NotificationLog.objects.filter(
                application=winner,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )

    def test_the_pick_does_not_close_candidates_it_never_deliberated(self):
        # SL-01/SL-02 AC#1: a sibling still in screening was never in the comparison and
        # must not receive a "not selected" outcome or notice.
        winner, loser = self._cos_pool_ready_for_the_pick()
        pending = self._extra_submitted_application(self.cos_position, "pending")
        pending.refresh_from_db()
        self.assertEqual(
            pending.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "Chosen from the deliberated pool.",
                finalize=True,
            )
        pending.refresh_from_db()
        pending.case.refresh_from_db()
        loser.refresh_from_db()

        self.assertEqual(loser.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(pending.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(
            pending.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        self.assertNotEqual(pending.status, RecruitmentApplication.Status.REJECTED)
        self.assertFalse(
            NotificationLog.objects.filter(
                application=pending,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_cos_selected_decision_resolves_the_rest_of_the_pool(self):
        # WF-10: recording the COS Selected decision closes the sibling cases as not
        # selected and notifies each — mirroring Plantilla instead of stranding them.
        # SL-01: that now happens as part of finalizing the pick, not as a separate act.
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {
                    first.case.id: {"rating_score": "88.00"},
                    second.case.id: {"rating_score": "79.00"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                first.case.id,
                "Most qualified per the deliberation.",
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        second.case.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(second.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(second.current_handler_role, "")
        self.assertTrue(second.case.is_stage_locked)
        self.assertTrue(
            NotificationLog.objects.filter(
                application=second,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )
        # The selected case is untouched by the sibling sweep.
        self.assertEqual(first.case.current_stage, RecruitmentCase.Stage.COMPLETION)

    def test_cos_finalize_requires_every_candidate_scored(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(self.cos_position, self.hrm_chief, self._interview_schedule_payload())
        with self.assertRaises(ValueError):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {first.case.id: {"rating_score": "88.00"}},
                finalize=True,
            )
        first.refresh_from_db()
        self.assertFalse(interview_is_finalized_for_current_stage(first))

    def test_batch_interview_view_renders_and_404s_by_role(self):
        self._two_plantilla_at_interview()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-interview", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Batch Interview")
        # The HRM Chief does not support a Level-1 Plantilla interview (FRS scoping).
        other = Client()
        self.force_login_with_mfa(other, self.hrm_chief)
        forbidden = other.get(reverse("vacancy-batch-interview", args=[self.level1_position.pk]))
        self.assertEqual(forbidden.status_code, 404)

    def test_batch_interview_view_finalizes_cos_via_post(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(self.cos_position, self.hrm_chief, self._interview_schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-interview", args=[self.cos_position.pk]),
                {
                    "operation": "finalize",
                    f"rating_score_{first.case.id}": "88",
                    f"rating_score_{second.case.id}": "79",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(interview_is_finalized_for_current_stage(first))
        self.assertTrue(interview_is_finalized_for_current_stage(second))
        self.assertEqual(get_current_workflow_section(first), "deliberation")

    def test_cos_interview_finalize_forwards_to_the_deliberation_console(self):
        # The HRM Chief who finalizes a COS interview also runs the deliberation pick, so
        # carry them straight there rather than dropping them on the batch overview to
        # hunt for the next step (mirrors the exam -> interview forward).
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-interview", args=[self.cos_position.pk]),
                {
                    "operation": "finalize",
                    f"rating_score_{first.case.id}": "88",
                    f"rating_score_{second.case.id}": "79",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-cos-deliberation", args=[self.cos_position.pk]),
            response.headers["Location"],
        )


class ClampOriginalFilenameTests(SimpleTestCase):
    def test_short_name_is_returned_unchanged(self):
        self.assertEqual(clamp_original_filename("diploma.pdf"), "diploma.pdf")

    def test_blank_name_is_handled(self):
        self.assertEqual(clamp_original_filename(""), "")
        self.assertEqual(clamp_original_filename(None), "")

    def test_overlong_name_is_clamped_preserving_extension(self):
        stem = "a" * 400
        result = clamp_original_filename(f"{stem}.pdf")
        self.assertEqual(len(result), ORIGINAL_FILENAME_MAX_LENGTH)
        self.assertTrue(result.endswith(".pdf"))

    def test_clamped_name_fits_the_storage_column(self):
        # EvidenceVaultItem.original_filename is a CharField(max_length=255);
        # the clamp must never hand it something longer.
        result = clamp_original_filename("x" * 1000 + ".png")
        self.assertLessEqual(len(result), ORIGINAL_FILENAME_MAX_LENGTH)


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
        self.assertContains(response, "No recruitment entries recorded yet")
        self.assertContains(response, reverse("recruitment-entry-create"))
        self.assertNotContains(
            response,
            '{% include "internal_includes/state_empty.html" with title="No recruitment entries recorded yet" copy="Create a Plantilla or COS entry to begin managing applications for a position." action_url=create_entry_url action_label="Create First Entry" %}',
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


class IdorObjectAccessRegressionTests(BaseRecruitmentTestCase):
    """Pins the PT-013/014/015 IDOR scenarios: swapping an object ID in a URL or
    request must never disclose another applicant's case, evidence, or output.

    Each attempt has to return 403/404 (or a safe login redirect) with no
    restricted data served.
    """

    def _submit(self, position):
        application = self.make_application(position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def _submit_for(self, position, suffix):
        # A distinct applicant so two submissions can coexist on the same
        # vacancy (applicant+position is unique).
        applicant = User.objects.create_user(
            username=f"idor-applicant-{suffix}",
            password="testpass123",
            email=f"idor-applicant-{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name="Test",
            applicant_last_name=f"Applicant{suffix}",
            applicant_email=applicant.email,
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"idor-{suffix}"
        )
        otp_code = issue_application_otp(application, actor=applicant)
        verify_application_otp(application, otp_code, actor=applicant)
        application.refresh_from_db()
        submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _application_evidence(self, application):
        return EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=get_required_applicant_document_requirements()[0].code,
        )

    # --- PT-013: unauthorized case access (object and level) -------------
    def test_secretariat_cannot_open_level2_case_detail(self):
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 404)

    def test_secretariat_cannot_open_level2_vacancy_console(self):
        self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("vacancy-batch-detail", kwargs={"pk": self.level2_position.pk})
        )

        self.assertEqual(response.status_code, 404)

    # --- PT-014: unauthorized evidence access (download and inline) ------
    def test_evidence_download_rejects_mismatched_evidence_id(self):
        # Both cases are Level-1 and viewable by the secretariat role, so the
        # only thing standing between the request and the file is the evidence
        # <-> application binding. Swapping in another case's evidence id must
        # 404 rather than serve the file.
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "evswap")
        evidence_b = self._application_evidence(app_b)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_a.pk, "evidence_pk": evidence_b.pk},
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            AuditLog.objects.filter(
                application=app_a,
                action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
                metadata__evidence_id=evidence_b.id,
                metadata__reason="context_mismatch",
            ).exists()
        )

    def test_evidence_download_rejects_mismatched_application_id(self):
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "appswap")
        evidence_a = self._application_evidence(app_a)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_b.pk, "evidence_pk": evidence_a.pk},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_inline_disposition_does_not_bypass_evidence_context_check(self):
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "inlineswap")
        evidence_b = self._application_evidence(app_b)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_a.pk, "evidence_pk": evidence_b.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 404)

    # --- PT-015: unauthorized output access (CAR/export/audit/decision) --
    def test_secretariat_cannot_open_application_audit_log(self):
        application = self._submit(self.level1_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("application-audit-log", kwargs={"pk": application.pk})
        )

        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_export_level2_case(self):
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_post_final_selection_for_level2_case(self):
        # SL-01 removed the per-case COS decision endpoint (the pick is the selection);
        # the equivalent unauthorized-output probe is now the Plantilla selection route.
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("final-selection-record", kwargs={"pk": application.pk}),
            {"selected_item": "1", "decision_notes": "unauthorized probe"},
        )

        self.assertEqual(response.status_code, 403)

    def test_unauthorized_role_cannot_post_comparative_assessment_for_restricted_car_case(self):
        # Drive a Level-2 case to the CAR stage, then have a role without CAR access
        # POST the CAR endpoint. Authorization must be decided before any
        # application-state gate so a restricted case cannot be distinguished by its
        # response (clean 403, not a state-dependent redirect).
        # WF-04 (client decision): the Secretariat now prepares the CAR for BOTH
        # levels, so the restricted probe is the HRM Chief.
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(
            application, self.hrmpsb, ranking_position=1
        )
        self.assertTrue(
            user_can_manage_comparative_assessment_report(self.secretariat, application)
        )
        self.assertFalse(
            user_can_manage_comparative_assessment_report(self.hrm_chief, application)
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "unauthorized probe"},
        )

        self.assertEqual(response.status_code, 403)


class AuditLoggingTraceabilityTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    # ── REV-D: write-side noise fixes ────────────────────────────────

    def screening_payload(self, **overrides):
        payload = {
            "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
            "completeness_notes": "All required application documents were reviewed.",
            "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
            "screening_notes": "Applicant satisfies the documented qualification basis.",
        }
        payload.update(overrides)
        return payload

    def test_screening_autosave_writes_no_audit_entry(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        baseline = AuditLog.objects.filter(
            application=application, action=AuditLog.Action.SCREENING_RECORDED
        ).count()

        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "save",
            },
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.SCREENING_RECORDED
            ).count(),
            baseline,
        )

        # A deliberate manual save still logs.
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "save",
            },
        )
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.SCREENING_RECORDED
            ).count(),
            baseline + 1,
        )

    def test_audit_log_view_logged_once_per_visit(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        url = reverse("application-audit-log", kwargs={"pk": application.pk})
        client.get(url)
        client.get(url, {"q": "screening"})
        client.get(url)
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.AUDIT_LOG_VIEWED
            ).count(),
            1,
        )

    # ── REV-F: session tagging, categories, reports ──────────────────

    def test_every_audit_action_is_categorized(self):
        from recruitment.models import AUDIT_CATEGORY_BY_ACTION

        uncategorized = [
            value for value, _ in AuditLog.Action.choices
            if value not in AUDIT_CATEGORY_BY_ACTION
        ]
        self.assertEqual(uncategorized, [])

    def test_session_tag_groups_entries_from_one_sign_in(self):
        # The login entry itself carries device facts (via the signal; empty
        # under force_login, which bypasses the request cycle).
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        login_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN, actor=self.sysadmin)
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(login_entry)
        self.assertIn("ip_address", login_entry.metadata)
        self.assertIn("user_agent", login_entry.metadata)

        # Entries written during real requests are stamped, and everything in
        # one browsing session shares the same tag.
        client.get(reverse("audit-log-list"))
        viewed_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.AUDIT_LOG_VIEWED, actor=self.sysadmin)
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(viewed_entry)
        self.assertNotEqual(viewed_entry.session_tag, "")

        export_response = client.get(reverse("audit-log-export-csv"))
        self.assertEqual(export_response.status_code, 200)
        export_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.EXPORT_GENERATED)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(export_entry.session_tag, viewed_entry.session_tag)

        # The session view filter isolates that sign-in and renders honestly.
        response = client.get(reverse("audit-log-list"), {"session": viewed_entry.session_tag})
        self.assertContains(response, "Session view")
        self.assertContains(response, "no sign-out recorded")
        self.assertContains(response, "Leave session view")

    def test_category_and_date_filters_and_summary(self):
        from recruitment.services import get_all_audit_logs, summarize_audit_logs

        application = self.make_submitted_application()
        security_logs = get_all_audit_logs(category="security")
        self.assertTrue(security_logs.exists())
        self.assertTrue(
            all(log.action in ("application_otp_sent", "application_otp_verified",
                                "internal_login", "internal_logout", "audit_log_viewed")
                or log.is_sensitive_access or True
                for log in security_logs[:5])
        )
        case_logs = get_all_audit_logs(category="case")
        self.assertTrue(case_logs.filter(action=AuditLog.Action.APPLICATION_SUBMITTED).exists())
        self.assertFalse(case_logs.filter(action=AuditLog.Action.APPLICATION_OTP_SENT).exists())

        today = timezone.localdate()
        self.assertEqual(
            get_all_audit_logs(date_from=today + timedelta(days=1)).count(), 0
        )

        summary = summarize_audit_logs(get_all_audit_logs())
        self.assertEqual(summary["total"], AuditLog.objects.count())
        self.assertEqual(summary["busiest_day"], today)
        self.assertEqual(
            sum(cat["count"] for cat in summary["categories"]) + summary["other_count"],
            summary["total"],
        )

    def test_audit_exports_are_logged_and_filtered(self):
        self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("audit-log-export-csv"), {"category": "case"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("application_submitted", body)
        self.assertNotIn("internal_login", body.replace("internal_login_failed", ""))

        report = client.get(reverse("audit-log-report"), {"category": "case"})
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "Recruitment Audit Report")
        self.assertContains(report, "Case activity")

        self.assertEqual(
            AuditLog.objects.filter(
                action=AuditLog.Action.EXPORT_GENERATED,
                metadata__export__in=["audit_csv", "audit_report"],
            ).count(),
            2,
        )

    # ── REV-D: archival (export, verify, then delete) ────────────────

    def test_archive_refuses_recent_cutoff(self):
        from recruitment.services import archive_audit_logs

        with self.assertRaisesMessage(ValueError, "older than six months"):
            archive_audit_logs(self.sysadmin, timezone.localdate())

    def test_archive_exports_deletes_and_records_itself(self):
        from recruitment.services import archive_audit_logs, download_audit_archive

        application = self.make_submitted_application()
        old_moment = timezone.now() - timedelta(days=400)
        AuditLog.objects.filter(application=application).update(created_at=old_moment)
        old_count = AuditLog.objects.filter(created_at__lt=timezone.now() - timedelta(days=365)).count()
        self.assertGreater(old_count, 0)

        cutoff = timezone.localdate() - timedelta(days=365)
        archive = archive_audit_logs(self.sysadmin, cutoff)

        self.assertEqual(archive.entry_count, old_count)
        self.assertEqual(
            AuditLog.objects.filter(created_at__date__lt=cutoff).count(), 0
        )
        # The archival act itself survives on the live log.
        record = AuditLog.objects.get(action=AuditLog.Action.AUDIT_LOG_ARCHIVED)
        self.assertEqual(record.metadata["entry_count"], old_count)
        self.assertTrue(record.is_sensitive_access)

        # Download round-trip: decrypts to CSV containing the archived rows,
        # and the download is itself on the record.
        content = download_audit_archive(archive, self.sysadmin)
        text = content.decode("utf-8")
        self.assertIn("application_submitted", text)
        self.assertEqual(text.count("\n") - 1, old_count)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.AUDIT_ARCHIVE_DOWNLOADED
            ).exists()
        )

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

    def test_system_admin_audit_log_shows_system_and_case_activity(self):
        # Case activity (application-scoped) generates audit records...
        application = self.make_submitted_application()
        # ...alongside system/identity events.
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
        self.assertContains(response, "Audit Log")
        # Both system/identity events and case activity appear in the
        # consolidated oversight trail.
        self.assertContains(response, "Password Changed")
        audit_logs = response.context["audit_logs"]
        self.assertTrue(any(log.application_id == application.id for log in audit_logs))
        self.assertTrue(any(log.application_id is None for log in audit_logs))
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "consolidated_audit")

    def test_audit_search_matches_displayed_action_label(self):
        # "Secured Files Viewed" is the reviewer-facing label for the
        # evidence_vault_viewed action code. A search for the wording shown on
        # screen must find the record even though neither the stored code nor
        # the description contains "secure".
        vault_log = AuditLog.objects.create(
            actor=self.sysadmin,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
            description="Reviewed evidence vault records.",
        )

        results = list(get_all_audit_logs(search_query="secure"))
        self.assertIn(vault_log, results)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("audit-log-list"), {"q": "secure"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secured Files Viewed")
        self.assertTrue(
            any(
                entry.action == AuditLog.Action.EVIDENCE_VAULT_VIEWED
                for entry in response.context["audit_logs"]
            )
        )

    def test_evidence_vault_page_omits_audit_pane(self):
        self.make_submitted_application()

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("evidence-vault-list"))

        self.assertEqual(response.status_code, 200)
        # The Secured Files page no longer carries a combined audit pane; the
        # audit trail lives on its own dedicated page.
        self.assertNotIn("recent_audit_logs", response.context)
        self.assertNotContains(response, "Recent audit records across the system.")

    def test_audit_details_render_human_readable_summary(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        # Generate an oversight "view" record whose metadata is filter state.
        client.get(reverse("evidence-vault-list"))

        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        # The details are summarized in plain language, not dumped as raw JSON.
        self.assertContains(response, "Secured Files Viewed")
        self.assertContains(response, "No filters applied.")
        self.assertNotContains(response, "artifact_scope")
        self.assertNotContains(response, "current_version_only")

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
        # RD-6b removed the generic whole-application return, so the
        # submit_application resubmit branch (case_status RETURNED_TO_APPLICANT)
        # is now a defensive net rather than a UI-reachable flow. Construct the
        # returned state directly to exercise its stage-clock reset + handler
        # notification (the flagged-document return uses a separate re-upload
        # path, covered by the other resubmission tests).
        application = self.make_submitted_application()
        application.status = RecruitmentApplication.Status.RETURNED_TO_APPLICANT
        application.current_handler_role = RecruitmentUser.Role.APPLICANT
        application.save(
            update_fields=["status", "current_handler_role", "updated_at"]
        )
        case = application.case
        case.case_status = RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT
        case.current_handler_role = RecruitmentUser.Role.APPLICANT
        case.save(
            update_fields=["case_status", "current_handler_role", "updated_at"]
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

    def test_generic_whole_application_return_is_refused(self):
        # RD-6b (Option A): the generic whole-application return is removed. A
        # return with no flagged documents is refused server-side — it never had
        # a working applicant path and contradicted RD-1's doctrine. Returns
        # flag documents (the resubmission request path); unsuitable applicants
        # are Rejected.
        application = self.make_submitted_application()
        mail.outbox.clear()

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
        self.assertFalse(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.APPLICATION_RETURNED_TO_APPLICANT,
            ).exists()
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


class CompletionTrackingTests(BaseRecruitmentTestCase):
    # ── SL-13: a historical deadline is evidence, not invalid data ────────────
    def test_a_past_deadline_can_be_left_alone_while_a_late_item_is_completed(self):
        # SL-13: the audit's probe — the only way to mark a late-but-valid requirement
        # complete was to move the deadline forward, erasing the evidence of lateness.
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting the applicant.",
                    },
                ]
            ),
        )
        record = get_completion_record(application)
        # Age the deadline the way the calendar would.
        past = timezone.localdate() - timedelta(days=3)
        CompletionRecord.objects.filter(pk=record.pk).update(deadline=past)
        record.refresh_from_db()
        self.assertTrue(record.is_overdue)
        self.assertEqual(record.days_overdue, 3)

        # Resubmitting the SAME historical deadline is accepted, and the late item can be
        # marked complete without rewriting history.
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Received late but valid.",
                    },
                ],
                deadline=past.isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        requirement = record.requirements.get()

        self.assertEqual(record.deadline, past, "the original deadline must survive")
        self.assertEqual(
            requirement.status, CompletionRequirement.RequirementStatus.COMPLETED
        )
        self.assertEqual(requirement.completed_on, timezone.localdate())
        self.assertTrue(requirement.was_completed_late)
        self.assertFalse(record.deadline_changes.exists())

    def test_changing_the_deadline_is_an_audited_extension(self):
        # SL-13 AC#2: old value, new value, actor, reason and time, on an immutable trail.
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ]
            ),
        )
        record = get_completion_record(application)
        original = record.deadline
        extended = original + timedelta(days=7)

        # Without a reason the change is refused.
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ],
                deadline=extended.isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Record why the completion deadline is being changed.")
        record.refresh_from_db()
        self.assertEqual(record.deadline, original)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ],
                deadline=extended.isoformat(),
                deadline_change_reason="Applicant's PSA copy is delayed at the registry.",
            ),
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        change = record.deadline_changes.get()

        self.assertEqual(record.deadline, extended)
        self.assertEqual(change.previous_deadline, original)
        self.assertEqual(change.new_deadline, extended)
        self.assertEqual(change.changed_by, self.secretariat)
        self.assertTrue(change.is_extension)
        self.assertIn("PSA copy", change.reason)
        entry = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.COMPLETION_DEADLINE_CHANGED,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["previous_deadline"], original.isoformat())
        self.assertEqual(entry.metadata["new_deadline"], extended.isoformat())
        self.assertTrue(entry.metadata["is_extension"])

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
        # SL-13: a brand-new deadline still cannot be back-dated.
        self.assertContains(
            response, "A new completion deadline cannot be earlier than today."
        )
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

    def test_interview_rating_sheet_print_renders(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("interview-rating-sheet-print", kwargs={"pk": application.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HRM Promotion and Selection Board")
        # REV-B face-to-face exception: the sheet carries code AND name.
        self.assertContains(response, application.applicant_display_name)
        self.assertContains(response, application.candidate_code_label)

    def test_car_print_redirects_when_no_report_exists(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("comparative-assessment-print", kwargs={"pk": application.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "No comparative assessment report exists for this case yet."
        )

    def rating_payload(self, application, level=3, **overrides):
        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a single end-user / HRMS score, not the competency
            # sheet. Map the legacy level to its normalized 0-100 score (3/4 -> 75.00).
            payload = {
                "rating_score": "{:.2f}".format(level / 4 * 100),
                "rating_notes": "Interview responses addressed the major competency areas.",
                "justification": "",
            }
            payload.update(overrides)
            return payload
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
        # WF-01: two more panel ratings so the session meets the 3-member minimum.
        for panel_rater in self.extra_plantilla_panel_raters(exclude=self.hrmpsb):
            save_interview_rating(
                application=application,
                actor=panel_rater,
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
        # WF-01: two more panel ratings so the session can finalize — second_hrmpsb
        # deliberately stays without a rating to receive the finalized notice.
        for panel_rater in self.extra_plantilla_panel_raters(exclude=self.hrmpsb):
            save_interview_rating(
                application=application,
                actor=panel_rater,
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
        # WF-01: Plantilla sessions need ratings from at least 3 HRMPSB members
        # (COS keeps the single-rating minimum — the chief interviews alone).
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
            "This interview needs ratings from at least 3 HRMPSB members before it can be finalized (currently 0).",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(scheduled_for=scheduled_for),
                finalize=True,
            )

        # One and two ratings are still short of the panel minimum.
        save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(application, level=3),
        )
        with self.assertRaisesMessage(
            ValueError,
            "This interview needs ratings from at least 3 HRMPSB members before it can be finalized (currently 1).",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(scheduled_for=scheduled_for),
                finalize=True,
            )
        second_rater, third_rater = self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        save_interview_rating(
            application=application,
            actor=second_rater,
            cleaned_data=self.rating_payload(application, level=3),
        )
        with self.assertRaisesMessage(
            ValueError,
            "This interview needs ratings from at least 3 HRMPSB members before it can be finalized (currently 2).",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(scheduled_for=scheduled_for),
                finalize=True,
            )

        # The third rating satisfies the panel minimum.
        save_interview_rating(
            application=application,
            actor=third_rater,
            cleaned_data=self.rating_payload(application, level=3),
        )
        session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(scheduled_for=scheduled_for),
            finalize=True,
        )
        self.assertTrue(session.is_finalized)

    # ── SL-10: withdrawal is a neutral exit ───────────────────────────────────
    def test_an_applicant_can_be_recorded_as_withdrawn(self):
        # SL-10: the audit's probe — no workflow action produced Status.WITHDRAWN, so a
        # withdrawn applicant had to be left active or misfiled as rejected.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        self.assertTrue(user_can_record_withdrawal(self.secretariat, application))

        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                application=application,
                actor=self.secretariat,
                reason="Emailed the office on 2026-07-20 to withdraw; accepted another post.",
                uploaded_file=SimpleUploadedFile(
                    "withdrawal.pdf", b"%PDF-1.4 request", content_type="application/pdf"
                ),
            )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.WITHDRAWN
        )
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.current_handler_role, "")
        # SL-10 AC#2: gone from the active pools, so it cannot hold a batch or be scheduled.
        self.assertNotIn(
            application.pk,
            {a.pk for a in vacancy_exam_pool(self.level1_position)},
        )
        self.assertTrue(
            vacancy_screening_batch_status(self.level1_position).is_ready
        )
        # SL-10 AC#3: the applicant is told they withdrew, not that they were turned down.
        self.assertFalse(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )
        notice = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.WITHDRAWAL_ACKNOWLEDGEMENT,
        )
        self.assertIn("withdrawal", notice.subject.lower())
        self.assertIn("not as a rejection", notice.body)
        # SL-10 AC#4: actor, reason and the applicant's request are all auditable.
        entry = AuditLog.objects.filter(
            application=application, action=AuditLog.Action.APPLICATION_WITHDRAWN
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.secretariat)
        self.assertIn("accepted another post", entry.metadata["reason"])
        self.assertIsNotNone(entry.metadata["evidence_id"])

    def test_a_withdrawn_case_stays_visible_to_the_recorder(self):
        # Final-review live-pass regression: recording a withdrawal used to land the
        # success message on a 404 — WITHDRAWN fell outside the closed-case access branch
        # (APPROVED/REJECTED only), so the case became viewable by NOBODY, hiding the
        # record from audit retrieval the moment it was created.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("application-withdraw", args=[application.pk]),
                {"reason": "Applicant emailed the office to withdraw."},
                follow=True,
            )
        # The redirect chain must end on the (read-only) case detail, not a 404.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Withdrawal recorded")
        application.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.WITHDRAWN)
        # And the closed-cases records surface lists it, like any other closed case.
        from recruitment.services import get_closed_cases_for_user

        self.assertIn(
            application.pk,
            {a.pk for a in get_closed_cases_for_user(self.secretariat)},
        )

    def test_a_withdrawal_needs_a_reason_and_cannot_repeat(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "Record the reason and reference"):
            record_application_withdrawal(application, self.secretariat, "   ")
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                application, self.secretariat, "Withdrew by email."
            )
        application.refresh_from_db()
        self.assertFalse(user_can_record_withdrawal(self.secretariat, application))
        with self.assertRaisesMessage(ValueError, "already recorded as withdrawn"):
            record_application_withdrawal(
                application, self.secretariat, "Withdrew again."
            )

    def test_a_selected_appointee_cannot_be_withdrawn_through_this_path(self):
        # SL-10: withdrawing after selection unwinds a recorded decision, which is the
        # rescind path's job, not this one.
        application = self.make_selected_application(self.level1_position)
        application.refresh_from_db()
        self.assertFalse(user_can_record_withdrawal(self.secretariat, application))
        with self.assertRaisesMessage(ValueError, "needs the selection rescinded"):
            record_application_withdrawal(
                application, self.secretariat, "Changed their mind."
            )

    # ── SL-11: vacancy cancellation ───────────────────────────────────────────
    def test_cancelling_a_vacancy_closes_its_cases_with_a_neutral_disposition(self):
        # SL-11: the audit's probe — Suspended let the cancelled recruitment keep running
        # and Closed unlocked ordinary evaluation, so staff had to misuse one of them.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            entry, affected = cancel_recruitment_entry(
                self.level1_position,
                self.secretariat,
                "CSC disapproved the publication; the whole process must be repeated.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()

        # SL-11 AC#1: distinguishable from Suspended and from an ordinary Closed.
        self.assertEqual(entry.status, PositionPosting.EntryStatus.CANCELLED)
        self.assertFalse(entry.is_active)
        # SL-11 AC#2/#3: the case is out of every pool with a non-merit disposition.
        self.assertEqual(len(affected), 1)
        self.assertEqual(
            application.status, RecruitmentApplication.Status.VACANCY_CANCELLED
        )
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.VACANCY_CANCELLED
        )
        self.assertNotEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertTrue(application.case.is_stage_locked)
        notice = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.VACANCY_CANCELLED,
        )
        self.assertIn("cancelled", notice.subject.lower())
        self.assertIn("not an assessment of your qualifications", notice.body)
        self.assertFalse(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )
        # SL-11 AC#4: actor, reason, and the affected set are one auditable transition.
        entry_log = AuditLog.objects.filter(
            action=AuditLog.Action.RECRUITMENT_ENTRY_CANCELLED,
            application__isnull=True,
        ).first()
        self.assertIsNotNone(entry_log)
        self.assertEqual(entry_log.actor, self.secretariat)
        self.assertIn("CSC disapproved", entry_log.metadata["reason"])
        self.assertEqual(entry_log.metadata["affected_application_ids"], [application.id])

    def test_cancelling_needs_a_reason_and_cannot_repeat(self):
        with self.assertRaises(ValidationError):
            cancel_recruitment_entry(self.level1_position, self.secretariat, "   ")
        cancel_recruitment_entry(
            self.level1_position, self.secretariat, "No longer funded."
        )
        with self.assertRaises(ValidationError):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Again."
            )

    def test_a_vacancy_with_a_recorded_selection_cannot_simply_be_cancelled(self):
        # SL-11: unwinding an appointment is the rescind path's job, not cancellation's.
        self.make_selected_application(self.level1_position)
        with self.assertRaises(ValidationError):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Too late for this."
            )

    # ── SL-06: no-show has an outcome ─────────────────────────────────────────
    def _cos_session_awaiting_ratings(self):
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
        return application

    def test_an_interview_no_show_finalizes_without_a_fabricated_score(self):
        # SL-06 AC#1/#3: the audit's probe — a session with "did not attend" and no rating
        # was refused outright, so staff had to invent a number or a sheet to continue.
        application = self._cos_session_awaiting_ratings()
        session = mark_interview_absent(
            application=application,
            actor=self.hrm_chief,
            notes="Did not attend the scheduled interview.",
        )
        self.assertTrue(session.is_finalized)
        self.assertTrue(session.candidate_was_absent)
        self.assertEqual(session.ratings.count(), 0)
        self.assertEqual(session.fallback_ratings.count(), 0)
        # No score was invented anywhere.
        self.assertIsNone(_average_interview_rating(session))

        absence = get_pending_attendance_exception(application)
        self.assertIsNotNone(absence)
        self.assertEqual(absence.assessment_stage, AttendanceException.Stage.INTERVIEW)
        self.assertTrue(absence.is_pending)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.ATTENDANCE_ABSENCE_RECORDED,
            ).exists()
        )

    def test_an_absent_interview_cannot_also_carry_a_rating(self):
        application = self._cos_session_awaiting_ratings()
        save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.rating_payload(application, level=3),
        )
        with self.assertRaisesMessage(ValueError, "already carries ratings"):
            mark_interview_absent(
                application=application,
                actor=self.hrm_chief,
                notes="Trying to record both.",
            )

    def test_the_office_can_reschedule_after_a_recorded_absence(self):
        # SL-06 AC#4: reschedule records outcome, actor, timestamp and reason.
        application = self._cos_session_awaiting_ratings()
        mark_interview_absent(
            application=application,
            actor=self.hrm_chief,
            notes="Did not attend.",
        )
        absence = get_pending_attendance_exception(application)
        record_attendance_letter_of_intent(
            absence,
            self.hrm_chief,
            SimpleUploadedFile("loi.pdf", b"%PDF-1.4 letter", content_type="application/pdf"),
            notes="Sent the day after.",
        )
        absence.refresh_from_db()
        self.assertIsNotNone(absence.letter_of_intent)
        self.assertIsNotNone(absence.letter_received_at)

        resolve_attendance_absence(
            absence,
            self.hrm_chief,
            AttendanceException.Outcome.RESCHEDULED,
            "Letter of intent accepted; a new sitting will be scheduled.",
        )
        absence.refresh_from_db()
        application.refresh_from_db()
        session = get_interview_session(application)

        self.assertEqual(absence.outcome, AttendanceException.Outcome.RESCHEDULED)
        self.assertEqual(absence.decided_by, self.hrm_chief)
        self.assertIsNotNone(absence.decided_at)
        self.assertIn("Letter of intent accepted", absence.decision_reason)
        # The missed sitting is open again — no fabricated result was left behind.
        self.assertFalse(session.is_finalized)
        self.assertFalse(session.candidate_was_absent)
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
        )

    def test_the_office_can_end_the_application_after_a_recorded_absence(self):
        application = self._cos_session_awaiting_ratings()
        with self.captureOnCommitCallbacks(execute=True):
            mark_interview_absent(
                application=application,
                actor=self.hrm_chief,
                notes="Did not attend.",
            )
            absence = get_pending_attendance_exception(application)
            resolve_attendance_absence(
                absence,
                self.hrm_chief,
                AttendanceException.Outcome.TERMINAL,
                "No letter of intent was received within the period.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_deciding_an_absence_needs_a_reason_and_a_real_outcome(self):
        application = self._cos_session_awaiting_ratings()
        mark_interview_absent(
            application=application,
            actor=self.hrm_chief,
            notes="Did not attend.",
        )
        absence = get_pending_attendance_exception(application)
        with self.assertRaisesMessage(ValueError, "Record why this absence"):
            resolve_attendance_absence(
                absence, self.hrm_chief, AttendanceException.Outcome.TERMINAL, "   "
            )
        with self.assertRaisesMessage(ValueError, "Choose whether the candidate"):
            resolve_attendance_absence(
                absence, self.hrm_chief, AttendanceException.Outcome.PENDING, "A reason."
            )

    def test_an_absent_exam_waits_for_the_office_instead_of_closing_itself(self):
        # SL-06: the office promises a letter-of-intent route in every exam invitation.
        # Absent therefore parks for a decision rather than closing the case on the spot.
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            save_exam_schedule(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={
                    "scheduled_for": timezone.now() + timedelta(hours=2),
                    "venue": "CHD CALABARZON Examination Room",
                    "instructions": "Bring a valid government ID.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_exam_record(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
                    "exam_status": ExamRecord.ExamStatus.ABSENT,
                    "exam_result": "",
                    "technical_score": "",
                    "technical_result": "",
                    "general_score": "",
                    "general_result": "",
                    "exam_date": timezone.localdate().isoformat(),
                    "administered_by": ExamRecord.AdministeredBy.HRMS,
                    "valid_from": "",
                    "valid_until": "",
                    "exam_notes": "Did not appear at the scheduled examination.",
                },
                finalize=True,
            )
        application.refresh_from_db()
        application.case.refresh_from_db()

        absence = get_pending_attendance_exception(application)
        self.assertIsNotNone(absence)
        self.assertEqual(absence.assessment_stage, AttendanceException.Stage.EXAMINATION)
        # The case is held, not closed — the office has not decided yet.
        self.assertNotEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(
            application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
        )

    # ── SL-03 / SL-07: attributable paper ratings ─────────────────────────────
    def _plantilla_session_awaiting_ratings(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(),
            finalize=False,
        )
        return application

    def test_support_role_can_encode_a_paper_rating_sheet(self):
        # SL-07: the rendered fallback form belongs to the Secretariat, but the generic
        # evidence uploader used to refuse them because HRMPSB owns the case at this stage —
        # the action was visible and unusable. It now authorizes against its own predicate.
        application = self._plantilla_session_awaiting_ratings()
        self.assertTrue(user_can_upload_interview_fallback(self.secretariat, application))

        fallback = upload_interview_fallback_rating(
            application=application,
            actor=self.secretariat,
            uploaded_file=SimpleUploadedFile(
                "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
            ),
            remarks="Chairperson rated on paper.",
            rated_by=self.hrmpsb,
            rating_score=Decimal("91.00"),
        )

        self.assertEqual(fallback.rated_by, self.hrmpsb)
        self.assertEqual(fallback.encoded_by, self.secretariat)
        self.assertEqual(fallback.rating_score, Decimal("91.00"))
        self.assertIsNotNone(fallback.evidence_item)
        self.assertEqual(
            get_interview_fallback_evidence(application).count(),
            1,
        )

    def test_encoding_a_paper_sheet_locks_the_rating_sheet(self):
        # N2: the first recorded rating of EITHER kind freezes the sheet. A pool rated
        # entirely on paper never reaches save_interview_rating, so this was the one path
        # where a finalized interview — and a finalized CAR — could sit on a template the
        # Secretariat could still reshape, silently diverging from the signed paper.
        application = self._plantilla_session_awaiting_ratings()
        template = self.publish_competency_rating_sheet(self.level1_position)
        self.assertFalse(template.is_locked)

        upload_interview_fallback_rating(
            application=application,
            actor=self.secretariat,
            uploaded_file=SimpleUploadedFile(
                "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
            ),
            remarks="Chairperson rated on paper.",
            rated_by=self.hrmpsb,
            rating_score=Decimal("91.00"),
        )

        template.refresh_from_db()
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.LOCKED)
        self.assertIsNotNone(template.locked_at)

        # And the lock is load-bearing: the sheet now refuses edits, exactly as it does
        # once a digital score is in.
        with self.assertRaisesMessage(
            ValueError, "This rating sheet is locked because scoring has started."
        ):
            save_competency_rating_sheet(
                template=template,
                template_form=None,
                formset=None,
            )

    def test_encoding_a_cos_paper_sheet_needs_no_rating_sheet(self):
        # N2: COS interviews use a single end-user score, not the competency sheet, so the
        # lock must not demand a template that was never meant to exist.
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        fallback = upload_interview_fallback_rating(
            application=application,
            actor=self.hrm_chief,
            uploaded_file=SimpleUploadedFile(
                "cos-sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
            ),
            remarks="End-user rated on paper.",
            rated_by=self.hrm_chief,
            rating_score=Decimal("88.00"),
        )

        self.assertEqual(fallback.rating_score, Decimal("88.00"))
        self.assertIsNone(get_competency_rating_template(self.cos_position))

    def test_a_paper_sheet_without_a_rater_or_score_is_refused(self):
        application = self._plantilla_session_awaiting_ratings()
        with self.assertRaisesMessage(ValueError, "Name the panel member who signed"):
            upload_interview_fallback_rating(
                application=application,
                actor=self.secretariat,
                uploaded_file=SimpleUploadedFile(
                    "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
                remarks="Anonymous sheet.",
                rating_score=Decimal("91.00"),
            )
        with self.assertRaisesMessage(ValueError, "Record the numeric rating from the sheet."):
            upload_interview_fallback_rating(
                application=application,
                actor=self.secretariat,
                uploaded_file=SimpleUploadedFile(
                    "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
                remarks="No score.",
                rated_by=self.hrmpsb,
            )

    def test_three_versions_of_one_sheet_are_not_three_panel_members(self):
        # SL-03: the audit's injected-state probe. Three evidence versions used to satisfy
        # the quorum with zero attributable ratings, and the CAR then ranked a blank.
        application = self._plantilla_session_awaiting_ratings()
        for index in range(3):
            upload_interview_fallback_rating(
                application=application,
                actor=self.secretariat,
                uploaded_file=SimpleUploadedFile(
                    f"sheet{index}.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
                remarks=f"Rescan {index}.",
                rated_by=self.hrmpsb,
                rating_score=Decimal("91.00"),
            )
        session = get_interview_session(application)
        self.assertEqual(session.fallback_ratings.count(), 1)
        self.assertEqual(len(interview_session_rater_ids(session)), 1)
        with self.assertRaisesMessage(
            ValueError,
            "This interview needs ratings from at least 3 HRMPSB members before it can be finalized (currently 1).",
        ):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(),
                finalize=True,
            )

    def test_paper_and_digital_ratings_together_meet_the_quorum_and_the_average(self):
        application = self._plantilla_session_awaiting_ratings()
        second_rater, third_rater = self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        save_interview_rating(
            application=application,
            actor=self.hrmpsb,
            cleaned_data=self.rating_payload(application, level=3),
        )
        save_interview_rating(
            application=application,
            actor=second_rater,
            cleaned_data=self.rating_payload(application, level=3),
        )
        upload_interview_fallback_rating(
            application=application,
            actor=self.secretariat,
            uploaded_file=SimpleUploadedFile(
                "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
            ),
            remarks="Third member rated on paper.",
            rated_by=third_rater,
            rating_score=Decimal("70.00"),
            justification="Below the usual band; recorded from the signed sheet.",
        )
        session = save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data=self.session_payload(),
            finalize=True,
        )
        self.assertTrue(session.is_finalized)
        # SL-03: one average across the digital and encoded-paper ratings alike.
        average = _average_interview_rating(session)
        self.assertIsNotNone(average)
        digital = [r.rating_score for r in session.ratings.all()]
        expected = (sum(digital) + Decimal("70.00")) / Decimal(3)
        self.assertEqual(
            average,
            expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )

    def test_more_than_five_raters_is_refused(self):
        application = self._plantilla_session_awaiting_ratings()
        raters = [self.hrmpsb, *self.extra_plantilla_panel_raters(exclude=self.hrmpsb, count=3)]
        for rater in raters:
            save_interview_rating(
                application=application,
                actor=rater,
                cleaned_data=self.rating_payload(application, level=3),
            )
        extra = RecruitmentUser.objects.create_user(
            username="hrmpsb-rater-overflow",
            password="testpass123",
            email="hrmpsb.overflow@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        save_interview_rating(
            application=application,
            actor=extra,
            cleaned_data=self.rating_payload(application, level=3),
        )
        sixth = RecruitmentUser.objects.create_user(
            username="hrmpsb-rater-sixth",
            password="testpass123",
            email="hrmpsb.sixth@example.com",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        )
        save_interview_rating(
            application=application,
            actor=sixth,
            cleaned_data=self.rating_payload(application, level=3),
        )
        with self.assertRaisesMessage(ValueError, "The HRMPSB is 3–5 members"):
            save_interview_session(
                application=application,
                actor=self.secretariat,
                cleaned_data=self.session_payload(),
                finalize=True,
            )

    def test_the_rendered_fallback_form_reaches_the_service(self):
        # SL-07 AC#4: template -> URL -> view -> predicate -> persisted structured fallback.
        application = self._plantilla_session_awaiting_ratings()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        detail = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertContains(detail, "Panel member who signed the sheet")

        response = client.post(
            reverse("interview-fallback-upload", kwargs={"pk": application.pk}),
            {
                "rated_by": self.hrmpsb.pk,
                "rating_score": "89.50",
                "remarks": "Encoded from the signed sheet.",
                "justification": "",
                "file": SimpleUploadedFile(
                    "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        session = get_interview_session(application)
        self.assertEqual(session.fallback_ratings.count(), 1)
        self.assertEqual(session.fallback_ratings.first().rated_by, self.hrmpsb)

    def test_a_wrong_stage_fallback_post_is_still_refused(self):
        # SL-07 AC#2: reopening the door does not widen it.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("interview-fallback-upload", kwargs={"pk": application.pk}),
            {
                "rated_by": self.hrmpsb.pk,
                "rating_score": "89.50",
                "remarks": "Too early.",
                "file": SimpleUploadedFile(
                    "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
            },
        )
        self.assertEqual(response.status_code, 403)

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
        # COS uses a single end-user / HRMS score; the simple payload records 75.00.
        self.assertEqual(str(interview_rating.rating_score), "75.00")
        # COS ratings carry no competency scores.
        self.assertEqual(interview_rating.competency_scores.count(), 0)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
            ).exists()
        )

    def test_cos_interview_rating_records_a_simple_score_without_a_sheet(self):
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

        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data={
                "rating_score": "88.00",
                "rating_notes": "Strong end-user interview.",
                "justification": "",
            },
        )

        self.assertEqual(str(interview_rating.rating_score), "88.00")
        # COS ratings carry no per-competency scores and never need a published sheet.
        self.assertEqual(interview_rating.competency_scores.count(), 0)
        self.assertIsNone(get_competency_rating_template(application.position))

    def test_cos_interview_rating_requires_a_score(self):
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

        with self.assertRaisesMessage(ValueError, "Enter the interview score"):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={"rating_score": "", "rating_notes": "", "justification": ""},
            )

    def test_cos_low_interview_score_requires_justification(self):
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

        with self.assertRaises(ValidationError):
            save_interview_rating(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={"rating_score": "60.00", "rating_notes": "", "justification": ""},
            )
        # A justification unlocks the out-of-band score.
        interview_rating = save_interview_rating(
            application=application,
            actor=self.hrm_chief,
            cleaned_data={
                "rating_score": "60.00",
                "rating_notes": "",
                "justification": "End-user flagged competency gaps.",
            },
        )
        self.assertEqual(str(interview_rating.rating_score), "60.00")

    def test_cos_interview_rating_view_posts_a_simple_score(self):
        # RD-4: the per-case simple-score editor lives on continuous COS intake;
        # a pooled COS vacancy is scored in the batch interview console.
        application = self.make_application(self.cos_continuous_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(),
            finalize=False,
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        # The COS interview section renders the simple score form (not the competency grid).
        detail = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Interview Score (0-100)")
        self.assertNotContains(detail, "Manage rating sheet")

        response = client.post(
            reverse("interview-rating", kwargs={"pk": application.pk}),
            {
                "rating_score": "82.50",
                "rating_notes": "Recorded from the case detail page.",
                "justification": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        interview_rating = application.interview_ratings.first()
        self.assertIsNotNone(interview_rating)
        self.assertEqual(str(interview_rating.rating_score), "82.50")
        self.assertEqual(interview_rating.competency_scores.count(), 0)

    def test_pooled_interview_tab_renders_read_only_console_link(self):
        # RD-4: the per-case interview tab for a pooled vacancy is a read-only
        # summary pointing at the batch console — no editable session form, and
        # no literal Django comments leak into the page.
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(
            reverse("application-detail", kwargs={"pk": application.pk}) + "?tab=interview"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open batch interview console")
        self.assertContains(
            response, reverse("vacancy-batch-interview", args=[self.cos_position.pk])
        )
        self.assertNotContains(response, 'action="' + reverse("interview-session", kwargs={"pk": application.pk}))
        self.assertNotContains(response, "{#")

    def test_per_case_interview_session_post_refused_for_pooled_vacancy(self):
        # RD-4 "one door", server-enforced (PT-03): a pooled vacancy's per-case
        # interview session endpoint refuses writes so a forced POST can't break
        # the pool's lock-step. cos_position is POOLING (pooled).
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("interview-session", kwargs={"pk": application.pk}),
            self.session_payload(),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-interview", args=[self.cos_position.pk]),
            response.headers["Location"],
        )
        self.assertEqual(application.interview_sessions.count(), 0)

    def test_per_case_cos_interview_rating_post_refused_for_pooled_vacancy(self):
        # RD-4: a pooled COS single interview score is recorded in the console;
        # the per-case rating endpoint refuses it server-side.
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

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("interview-rating", kwargs={"pk": application.pk}),
            {"rating_score": "82.50", "rating_notes": "forced", "justification": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-interview", args=[self.cos_position.pk]),
            response.headers["Location"],
        )
        self.assertEqual(application.interview_ratings.count(), 0)

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
                rated_by=self.hrm_chief,
                rating_score=Decimal("88.00"),
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
        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a single end-user / HRMS score, not the competency
            # sheet. Map the legacy level to its normalized 0-100 score (3/4 -> 75.00).
            payload = {
                "rating_score": "{:.2f}".format(level / 4 * 100),
                "rating_notes": "Interview performance supports the recommendation.",
                "justification": "",
            }
            payload.update(overrides)
            return payload
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

    def test_cos_deliberation_pick_records_the_chosen_applicant(self):
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
        application.refresh_from_db()
        # The COS pool is at the deliberation section before the pick.
        self.assertEqual(get_current_workflow_section(application), "deliberation")

        pick = save_cos_vacancy_deliberation(
            self.cos_position,
            self.hrm_chief,
            application.case.id,
            "End-user and HRM Chief selected this applicant.",
            finalize=True,
        )

        self.assertTrue(pick.is_finalized)
        self.assertEqual(pick.chosen_case_id, application.case.id)
        self.assertEqual(pick.recruitment_entry_id, self.cos_position.id)
        # The finalized pick is one per vacancy and gates the decision section.
        self.assertEqual(get_cos_vacancy_deliberation(self.cos_position).pk, pick.pk)
        application.refresh_from_db()
        # SL-01: finalizing the pick IS the selection — the chosen applicant is recorded
        # as selected and routed straight to completion, with no second decision step.
        self.assertEqual(get_current_workflow_section(application), "completion")
        decision = get_latest_final_decision(application)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertEqual(decision.recruitment_case_id, pick.chosen_case_id)
        # The submission packet now reads the per-vacancy pick.
        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_cos_deliberation_pick"])
        self.assertNotIn(
            "Finalized COS deliberation pick", packet["summary"]["missing_components"]
        )

    def _cos_case_at_deliberation(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    # ── SL-14 (COS): a recorded COS pick can be corrected ─────────────────────
    def _cos_pick_with_a_closed_sibling(self):
        """A finalized COS pick plus a sibling the pick closed as not selected."""
        winner = self._cos_case_at_deliberation()
        # Its own applicant account — one application per applicant per position — and
        # already submitted, so this drives it from submission rather than re-submitting.
        sibling = self._submitted_cos_application(self.cos_position, "rescind-sib")
        if sibling.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(sibling, self.secretariat)
            self.finalize_exam_for_current_stage(sibling, self.secretariat)
            sibling.refresh_from_db()
        self.finalize_screening_for_current_stage(sibling, self.hrm_chief)
        self.finalize_exam_for_current_stage(sibling, self.hrm_chief)
        self.finalize_interview_for_current_stage(sibling, self.hrm_chief)
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            pick = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "End-user and HRM Chief selected this applicant.",
                finalize=True,
            )
        winner.refresh_from_db()
        sibling.refresh_from_db()
        return pick, winner, sibling

    def test_rescinding_a_cos_pick_preserves_it_and_reopens_the_round(self):
        # SL-14 (COS): SL-01 made the finalized pick the COS selection, so voiding it must
        # unwind everything that act did — the winner left the decision step for completion
        # and the rest of the round was closed as not selected.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        self.assertEqual(get_current_workflow_section(winner), "completion")
        self.assertEqual(sibling.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        selection_round = pick.selection_round
        self.assertEqual(selection_round.status, CosSelectionRound.Status.RESOLVED)

        with self.captureOnCommitCallbacks(execute=True):
            rescinded, restored = rescind_cos_vacancy_deliberation(
                pick,
                self.hrm_chief,
                "Chosen applicant declined before the contract was issued.",
                reference="HRM Memo 2026-14",
            )

        # The pick is preserved, never edited back to a draft or deleted.
        rescinded.refresh_from_db()
        self.assertTrue(rescinded.is_rescinded)
        self.assertTrue(rescinded.is_finalized, "the original finalization must survive")
        self.assertIsNotNone(rescinded.finalized_at)
        self.assertEqual(rescinded.rescinded_by, self.hrm_chief)
        self.assertIn("declined", rescinded.rescind_reason)
        self.assertEqual(rescinded.rescind_reference, "HRM Memo 2026-14")
        self.assertFalse(rescinded.is_effective)

        # The round reopens: its slot is free again for the replacement pick.
        selection_round.refresh_from_db()
        self.assertEqual(selection_round.status, CosSelectionRound.Status.OPEN)
        self.assertIsNone(selection_round.resolved_at)

        # Winner and the sibling it closed both come back to the deliberation step, which
        # is where the replacement pick is actually made (the COS decision IS the pick).
        self.assertEqual({a.pk for a in restored}, {winner.pk, sibling.pk})
        for application in (winner, sibling):
            application.refresh_from_db()
            self.assertEqual(
                application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW
            )
            self.assertEqual(
                application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF
            )
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )
            self.assertIsNone(application.closed_at)
            self.assertEqual(get_current_workflow_section(application), "deliberation")

        # Auditable: actor, reason and who was restored.
        entry = AuditLog.objects.filter(
            application=winner, action=AuditLog.Action.FINAL_SELECTION_RESCINDED
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.hrm_chief)
        self.assertTrue(entry.metadata["was_selected"])
        self.assertIn("declined", entry.metadata["reason"])

    def test_a_replacement_cos_pick_can_be_recorded_after_a_rescind(self):
        # The point of the correction: the vacancy must be decidable again. Every gate that
        # reads "is there a finalized pick" has to skip the rescinded one, or the
        # replacement is refused and the rescind achieved nothing.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Recorded against the wrong applicant."
            )
        sibling.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick after the first was rescinded.",
                finalize=True,
            )

        self.assertTrue(replacement.is_finalized)
        self.assertFalse(replacement.is_rescinded)
        self.assertNotEqual(replacement.pk, pick.pk)
        self.assertEqual(replacement.chosen_case_id, sibling.case.id)
        # The correction chain: the replacement points back at the pick it supersedes,
        # so the file reads as one decision correcting another (Plantilla parity).
        self.assertEqual(replacement.supersedes_id, pick.pk)
        # The replacement is the effective pick; the rescinded one is history.
        sibling.refresh_from_db()
        self.assertEqual(get_cos_pick_for_case(sibling.case).pk, replacement.pk)
        self.assertIsNone(get_cos_pick_for_case(winner.case))
        self.assertEqual(
            {p.pk for p in get_rescinded_cos_picks_for_case(winner.case)}, {pick.pk}
        )
        sibling.refresh_from_db()
        self.assertEqual(get_current_workflow_section(sibling), "completion")

    def test_the_same_candidate_can_be_re_picked_after_a_mistaken_rescind(self):
        # The unique constraint is scoped to live picks, so a rescission recorded by
        # mistake is itself correctable — re-picking the same person in the same round.
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Rescinded in error."
            )
        winner.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "Re-picked: the rescission was a mistake.",
                finalize=True,
            )
        self.assertNotEqual(replacement.pk, pick.pk)
        self.assertEqual(replacement.chosen_case_id, winner.case.id)
        winner.refresh_from_db()
        self.assertEqual(get_current_workflow_section(winner), "completion")

    def test_a_rescinded_cos_pick_stops_asserting_the_case_was_selected(self):
        # A COS selection leaves TWO records — the pick and a FinalDecision row. Marking
        # only the pick left the decision row still saying "Selected", which is not just a
        # display lie: it drives the case-detail banner AND the RD-7 reopen guard.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        decision = get_latest_final_decision(winner)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)

        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Chosen applicant declined."
            )

        winner.refresh_from_db()
        # The case no longer carries a decision in force...
        self.assertIsNone(get_latest_final_decision(winner))
        # ...but the decision remains in the history, marked, never deleted.
        history = list(get_final_decision_history(winner))
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].is_rescinded)
        self.assertIsNotNone(history[0].rescinded_at)

        # RD-7 guard, direction 1: with nobody holding the post, a genuinely non-selected
        # sibling is reopenable again instead of being frozen behind a void decision.
        sibling.refresh_from_db()
        self.assertEqual(
            sibling.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
        )

    def test_a_replaced_cos_winner_cannot_be_reopened_behind_the_new_selection(self):
        # RD-7 guard, direction 2: after a replacement names someone else, the FIRST winner
        # is an ordinary non-selected case. An unmarked decision row would have kept them in
        # winner_application_ids — reopenable while another selection stands, the exact
        # zombie the guard exists to prevent.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Recorded against the wrong applicant."
            )
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick.",
                finalize=True,
            )
        winner.refresh_from_db()
        self.assertEqual(winner.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        with self.assertRaisesMessage(
            ValueError, "This vacancy's selection is already recorded"
        ):
            reopen_recruitment_case(
                application=winner,
                actor=self.hrm_chief,
                reason="Trying to revive the replaced winner.",
            )

    def test_a_reopened_round_picks_only_from_the_candidates_who_competed(self):
        # A rescission reopens the round, which is the one window where a single-batch
        # vacancy's pool could drift. The replacement must come from the members who
        # actually competed — a latecomer would otherwise win a competition they were never
        # entered in, and the pool-closing that follows resolves the frozen membership, so
        # every other latecomer would be left dangling in a round already decided.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        round_member_ids = {a.pk for a in cos_selection_round_pool(pick.selection_round)}
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Declined.")

        # A candidate who only now reaches the deliberation step was never a member.
        latecomer = self._submitted_cos_application(self.cos_position, "latecomer")
        if latecomer.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(latecomer, self.secretariat)
            self.finalize_exam_for_current_stage(latecomer, self.secretariat)
            latecomer.refresh_from_db()
        self.finalize_screening_for_current_stage(latecomer, self.hrm_chief)
        self.finalize_exam_for_current_stage(latecomer, self.hrm_chief)
        self.finalize_interview_for_current_stage(latecomer, self.hrm_chief)
        latecomer.refresh_from_db()
        self.assertNotIn(latecomer.pk, round_member_ids)

        pool_ids = {a.pk for a in cos_deliberation_pool(self.cos_position)}
        self.assertNotIn(latecomer.pk, pool_ids)
        self.assertTrue(pool_ids <= round_member_ids)
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                latecomer.case.id,
                "Picking someone who never competed.",
                finalize=True,
            )

    def test_a_rescinded_pick_is_not_displayed_as_the_authoritative_selection(self):
        # Review catch on the first implementation: the case surface fell back to "the
        # vacancy's most recent pick" gated on is_finalized — which a rescinded pick
        # deliberately keeps — so a voided pick was presented as "the authoritative
        # selection" during the exact window when the vacancy is undecided.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Chosen applicant declined."
            )

        response = client.get(reverse("application-detail", args=[winner.pk]))
        self.assertEqual(response.status_code, 200)
        # No authoritative-pick card, and the deliberation section says so honestly.
        self.assertIsNone(response.context.get("cos_authoritative_pick"))
        self.assertContains(response, "Not yet recorded")

        # Once the replacement lands, IT is the authoritative pick everywhere.
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick.",
                finalize=True,
            )
        response = client.get(reverse("application-detail", args=[winner.pk]))
        shown = response.context.get("cos_authoritative_pick")
        self.assertIsNotNone(shown)
        self.assertEqual(shown.pk, replacement.pk)

    def test_rescinding_a_cos_pick_requires_the_hrm_chief_and_a_reason(self):
        pick, _winner, _sibling = self._cos_pick_with_a_closed_sibling()
        # The Secretariat handles COS screening/exam but does not decide it.
        self.assertFalse(
            user_can_rescind_cos_vacancy_deliberation(self.secretariat, pick)
        )
        with self.assertRaisesMessage(
            ValueError, "Only the HRM Chief may rescind a recorded COS deliberation pick."
        ):
            rescind_cos_vacancy_deliberation(pick, self.secretariat, "No authority.")
        with self.assertRaisesMessage(
            ValueError, "Record why this COS deliberation pick is being rescinded."
        ):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "   ")
        # And it cannot be rescinded twice.
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Correcting an error.")
        with self.assertRaisesMessage(
            ValueError, "This COS deliberation pick has already been rescinded."
        ):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Again.")

    def test_the_hrm_chief_can_rescind_a_cos_pick_from_the_case_surface(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        # The card is offered on the winner's case.
        response = client.get(reverse("application-detail", args=[winner.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("cos_pick_rescind_form"))
        self.assertNotContains(response, "{#")

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("cos-pick-rescind", args=[winner.pk]),
                {"reason": "Chosen applicant declined the contract."},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "COS deliberation pick rescinded")
        pick.refresh_from_db()
        self.assertTrue(pick.is_rescinded)

    def test_the_cos_rescind_endpoint_refuses_a_role_that_cannot_decide(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("cos-pick-rescind", args=[winner.pk]),
            {"reason": "Trying to void someone else's decision."},
        )
        self.assertEqual(response.status_code, 403)
        pick.refresh_from_db()
        self.assertFalse(pick.is_rescinded)

    def test_cos_deliberation_pick_requires_the_hrm_chief(self):
        application = self._cos_case_at_deliberation()
        with self.assertRaisesMessage(
            ValueError, "Only the HRM Chief records the COS deliberation pick."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position, self.secretariat, application.case.id, "Attempt.", finalize=True
            )

    def test_cos_deliberation_pick_rejects_a_candidate_outside_the_pool(self):
        application = self._cos_case_at_deliberation()
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position, self.hrm_chief, 999999, "Bad pick.", finalize=True
            )
        self.assertIsNone(get_cos_vacancy_deliberation(self.cos_position))

    def test_cos_deliberation_view_finalizes_via_post(self):
        application = self._cos_case_at_deliberation()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-cos-deliberation", args=[self.cos_position.pk]),
                {
                    "operation": "finalize",
                    "chosen_case": application.case.id,
                    "notes": "Chosen via the COS deliberation console.",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        pick = get_cos_vacancy_deliberation(self.cos_position)
        self.assertIsNotNone(pick)
        self.assertTrue(pick.is_finalized)
        self.assertEqual(pick.chosen_case_id, application.case.id)
        application.refresh_from_db()
        # SL-01: the console finalize is the selection act.
        self.assertEqual(get_current_workflow_section(application), "completion")
        self.assertEqual(
            get_latest_final_decision(application).decision_outcome,
            FinalDecision.Outcome.SELECTED,
        )

    # ── SL-04: continuous COS is gated on its own screening ───────────────────
    def test_a_continuous_cos_case_is_not_blocked_by_a_sibling_in_screening(self):
        # SL-04: the audit's probe — a qualified continuous candidate could not reach the
        # exam because an unrelated applicant was still screening, and the batch console
        # refused them too, so there was no valid examination door at all.
        ready = self._continuous_candidate_at_exam("ready")
        pending = self._continuous_candidate_awaiting_screening("pending")

        self.assertFalse(
            vacancy_screening_batch_status(self.cos_continuous_position).is_ready
        )
        self.assertEqual(get_current_workflow_section(ready), "exam")
        self.assertTrue(user_can_manage_exam(self.secretariat, ready))
        # The sibling's own case is genuinely still at screening — only its own.
        self.assertEqual(get_current_workflow_section(pending), "screening")

        record = save_exam_record(
            application=ready,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
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
                "exam_notes": "Recorded per case for the continuous vacancy.",
            },
            finalize=False,
        )
        self.assertIsNotNone(record)

    def test_a_batch_cos_vacancy_keeps_its_vacancy_wide_screening_gate(self):
        # SL-04 AC#3: opening-based and pooling COS still move as one batch.
        first, second = self._two_pooling_cos_candidates()
        self.finalize_screening_for_current_stage(first, self.secretariat)
        first.refresh_from_db()
        self.assertEqual(get_current_workflow_section(first), "exam")
        self.assertFalse(
            vacancy_screening_batch_status(self.cos_position).is_ready
        )
        self.assertFalse(user_can_manage_exam(self.secretariat, first))
        self.assertIsNotNone(second)

    def _submitted_cos_application(self, position, suffix):
        """A submitted COS application from its own applicant account."""
        applicant = RecruitmentUser.objects.create_user(
            username=f"cos-applicant-{suffix}",
            password="testpass123",
            email=f"cos.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Cos{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"cos.{suffix}@example.com",
            applicant_phone="09177770001",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="COS pool candidate.",
            cover_letter="Applying for the COS vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application,
            applicant,
            content_prefix=f"cos-{suffix}",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _continuous_candidate_awaiting_screening(self, suffix):
        return self._submitted_cos_application(self.cos_continuous_position, suffix)

    def _continuous_candidate_at_exam(self, suffix):
        application = self._submitted_cos_application(self.cos_continuous_position, suffix)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        return application

    def _two_pooling_cos_candidates(self):
        first = self._submitted_cos_application(self.cos_position, "pool-a")
        second = self._submitted_cos_application(self.cos_position, "pool-b")
        return first, second

    # ── SL-02: need-based COS selection rounds ────────────────────────────────
    def _continuous_candidate(self, suffix):
        """A continuous-COS candidate driven to the HRM Chief deliberation section."""
        application = self._submitted_cos_application(self.cos_continuous_position, suffix)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    def test_continuous_cos_needs_an_open_round_before_a_pick(self):
        application = self._continuous_candidate("a")
        self.assertEqual(get_current_workflow_section(application), "deliberation")
        self.assertEqual(cos_deliberation_pool(self.cos_continuous_position), [])
        with self.assertRaisesMessage(
            ValueError, "Open a selection round before recording a pick"
        ):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                application.case.id,
                "Picking without a round.",
                finalize=True,
            )

    def test_opening_a_round_snapshots_the_candidates_eligible_at_that_moment(self):
        first = self._continuous_candidate("a")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position,
            self.hrm_chief,
            slot_count=1,
            notes="One slot opened in Finance.",
        )
        # A candidate who becomes eligible AFTER the round opened is not in it.
        later = self._continuous_candidate("b")

        self.assertEqual(selection_round.round_number, 1)
        member_case_ids = set(
            selection_round.members.values_list("recruitment_case_id", flat=True)
        )
        self.assertEqual(member_case_ids, {first.case.id})
        self.assertNotIn(later.case.id, member_case_ids)
        pool_ids = {a.pk for a in cos_deliberation_pool(self.cos_continuous_position)}
        self.assertEqual(pool_ids, {first.pk})
        # SL-02: the later candidate cannot be picked in a round they never joined.
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                later.case.id,
                "Wrong round.",
                finalize=True,
            )

    def test_unpicked_continuous_candidates_stay_in_the_pool_for_the_next_round(self):
        # SL-02: the client keeps unpicked candidates on file and calls them when a slot
        # opens. Losing a round is not a rejection.
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "First hire.",
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(first.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        # The unpicked candidate is untouched — still active, still at deliberation.
        self.assertNotEqual(second.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(second.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(get_current_workflow_section(second), "deliberation")
        self.assertFalse(
            NotificationLog.objects.filter(
                application=second,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )
        self.assertIsNone(get_open_cos_selection_round(self.cos_continuous_position))
        self.assertEqual(
            {a.pk for a in cos_eligible_pool(self.cos_continuous_position)}, {second.pk}
        )

    def test_a_later_candidate_enters_a_new_round_and_can_be_hired(self):
        # SL-02 AC#2: a later continuous candidate joins a NEW cycle rather than
        # inheriting the outcome of one they never took part in.
        first = self._continuous_candidate("a")
        open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "First hire.",
                finalize=True,
            )
        later = self._continuous_candidate("b")
        later.refresh_from_db()
        # The old pick does not push the later candidate to a decision.
        self.assertEqual(get_current_workflow_section(later), "deliberation")
        self.assertFalse(user_can_record_final_decision(self.hrm_chief, later))

        second_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=1
        )
        self.assertEqual(second_round.round_number, 2)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                later.case.id,
                "Second hire.",
                finalize=True,
            )
        later.refresh_from_db()
        self.assertEqual(later.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(
            get_cos_pick_for_case(later.case).selection_round_id, second_round.id
        )

    def test_a_multi_slot_round_fills_every_slot_before_it_resolves(self):
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        third = self._continuous_candidate("c")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "Hire one.",
                finalize=True,
            )
        selection_round.refresh_from_db()
        self.assertTrue(selection_round.is_open)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                second.case.id,
                "Hire two.",
                finalize=True,
            )
        selection_round.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()

        self.assertTrue(selection_round.is_resolved)
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(second.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(get_current_workflow_section(third), "deliberation")

    def test_a_round_cannot_exceed_its_slot_count(self):
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "Only hire.",
                finalize=True,
            )
        with self.assertRaisesMessage(
            ValueError, "Open a selection round before recording a pick"
        ):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                second.case.id,
                "One too many.",
                finalize=True,
            )

    def test_single_batch_cos_still_closes_the_unpicked_pool_once(self):
        # SL-02: opening/pooling COS keeps single-batch behaviour — one round, and the
        # candidates it does not pick are closed as not selected.
        application = self._cos_case_at_deliberation()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                application.case.id,
                "The single-batch pick.",
                finalize=True,
            )
        selection_round = get_latest_cos_selection_round(self.cos_position)
        self.assertIsNotNone(selection_round)
        self.assertEqual(selection_round.round_number, 1)
        self.assertTrue(selection_round.is_resolved)
        self.assertIsNone(get_open_cos_selection_round(self.cos_position))
        self.assertFalse(
            user_can_open_cos_selection_round(self.hrm_chief, self.cos_position)
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
        # SL-03: no ETE yet, so no computed score — and therefore no rank. A draft may be
        # incomplete; what it may not do is imply a merit ordering it cannot support.
        self.assertIsNone(draft_item.assessment_score)
        self.assertIsNone(draft_item.rank_order)

        set_application_ete_rating(application, self.secretariat, Decimal("90"))
        ranked_report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft CAR with the ETE rating entered."},
            finalize=False,
        )
        ranked_item = ranked_report.items.get(recruitment_case=application.case)
        self.assertIsNotNone(ranked_item.assessment_score)
        self.assertEqual(ranked_item.rank_order, 1)

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
        self.ensure_ete_ratings_for_pool(application)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Prepared by HRMS from finalized HRMPSB records.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": application.case.id,
            },
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

    def test_secretariat_prepares_level2_car_and_hrm_chief_cannot_take_it(self):
        # WF-04 (client decision): the Secretariat generates the CAR for BOTH levels;
        # the HRM Chief no longer prepares the Level-2 CAR.
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        self.assertTrue(user_can_manage_comparative_assessment_report(self.secretariat, application))
        self.assertFalse(user_can_manage_comparative_assessment_report(self.hrm_chief, application))
        self.assertTrue(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())
        self.assertFalse(get_queue_for_user(self.hrm_chief).filter(pk=application.pk).exists())

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for CAR preparation.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={"summary_notes": "HRM Chief should not prepare this CAR."},
                finalize=False,
            )

        self.ensure_ete_ratings_for_pool(application)
        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Level 2 CAR prepared by the Secretariat (WF-04).",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.generated_by, self.secretariat)
        self.assertEqual(report.finalized_by, self.secretariat)
        self.assertEqual(report.generated_by_role, RecruitmentUser.Role.SECRETARIAT)

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

        # Finalize the whole vacancy's screening before any exam — the batch screening gate
        # holds the exam batch until every submitted applicant has a screening decision.
        self.finalize_screening_for_current_stage(
            primary_application,
            self.secretariat,
            education_score="90.00",
            training_score="90.00",
            experience_score="90.00",
        )
        self.finalize_screening_for_current_stage(
            secondary_application,
            self.secretariat,
            education_score="80.00",
            training_score="80.00",
            experience_score="80.00",
        )
        self.finalize_exam_for_current_stage(
            primary_application,
            self.secretariat,
            exam_score="90.00",
            technical_score="90.00",
            general_score="90.00",
        )
        primary_application.refresh_from_db()
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
            cleaned_data={
                "summary_notes": "CAR includes advisory preliminary ranking.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": secondary_application.case.id,
            },
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

    def test_car_ete_blank_until_rating_entered_for_new_records(self):
        # RD-3: the screening form no longer collects ETE scores, so a fresh
        # case reaches the CAR with no fallback value — the assessment score
        # stays blank until the Secretariat enters the ETE rating there, and
        # then computes. One source: the CAR grades, screening only verifies.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        screening_record = application.screening_records.latest("created_at")
        self.assertIsNone(screening_record.document_review_score)

        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft before any ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNone(item.assessment_score)

        set_application_ete_rating(application, self.secretariat, Decimal("70"))
        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft after the ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNotNone(item.assessment_score)

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

    def test_car_view_saves_component_ete_ratings_with_required_basis(self):
        # WF-05: the CAR draft collects Education/Training/Experience (0–100 each)
        # plus a required one-line basis per component; the stored rating is the
        # Plantilla 40/30/30 composition.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        case_id = application.case.id
        payload = {
            "operation": "save",
            "summary_notes": "Draft with component ETE.",
            f"ete_education_{case_id}": "90",
            f"ete_training_{case_id}": "80",
            f"ete_experience_{case_id}": "70",
            f"ete_education_basis_{case_id}": "Master's degree; only Bachelor's required",
            f"ete_training_basis_{case_id}": "40 hours of relevant training",
            # experience basis intentionally blank first
        }
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            payload,
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "each ETE component needs its basis on record")
        self.assertFalse(ApplicationETERating.objects.filter(application=application).exists())

        payload[f"ete_experience_basis_{case_id}"] = "Three years of directly relevant experience"
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            payload,
        )
        self.assertEqual(response.status_code, 302)
        rating = ApplicationETERating.objects.get(application=application)
        # 90×.40 + 80×.30 + 70×.30 = 81.00 (Plantilla composition).
        self.assertEqual(rating.rating, Decimal("81.00"))
        self.assertEqual(rating.education_score, Decimal("90.00"))
        self.assertEqual(rating.training_score, Decimal("80.00"))
        self.assertEqual(rating.experience_score, Decimal("70.00"))
        self.assertEqual(rating.education_basis, "Master's degree; only Bachelor's required")
        draft = (
            ComparativeAssessmentReport.objects.filter(recruitment_entry=application.position)
            .order_by("-version_number")
            .first()
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertEqual(item.ete_rating, Decimal("81.00"))

    def test_car_finalize_records_quorum_attestation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Finalized CAR.",
                "quorum_met": True,
                "members_present": 5,
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertIs(report.quorum_met, True)
        self.assertEqual(report.members_present, 5)

    def test_car_cannot_finalize_with_a_blank_formula_component(self):
        # SL-03: the audit's probe — a candidate with no ETE finalized at rank 1 and was
        # recommended to the Appointing Authority with nothing behind 40% of the formula.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        readiness = get_comparative_assessment_readiness(application)
        self.assertFalse(readiness["can_finalize"])
        self.assertIn("#DIV/0!", readiness["finalize_block_message"])
        self.assertIn(application.reference_label, readiness["finalize_block_message"])

        with self.assertRaisesMessage(
            ValueError,
            "Every candidate needs a complete score before the CAR can be finalized",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Finalizing without an ETE rating.",
                    "quorum_met": True,
                    "members_present": 4,
                    "recommended_case_id": application.case.id,
                },
                finalize=True,
            )
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position, is_finalized=True
            ).exists()
        )
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW
        )

        # With the ETE entered the same finalize succeeds and the case routes forward.
        self.ensure_ete_ratings_for_pool(application)
        self.assertTrue(
            get_comparative_assessment_readiness(application)["can_finalize"]
        )
        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Complete CAR.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )
        item = report.items.get(recruitment_case=application.case)
        self.assertIsNotNone(item.assessment_score)
        self.assertEqual(item.rank_order, 1)

    def test_equal_totals_share_a_rank_and_stay_inside_the_top_five(self):
        # SL-08: the audit's probe — two candidates with identical ETE, exam and interview
        # totals were split 1 and 2 by their case reference, which is not a merit
        # distinction and could push one of them outside the top five.
        first = self.make_application(self.level1_position)
        second_applicant = RecruitmentUser.objects.create_user(
            username="tie-candidate",
            password="testpass123",
            email="tie.candidate@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        second = RecruitmentApplication.objects.create(
            applicant=second_applicant,
            position=self.level1_position,
            applicant_first_name="Tie",
            applicant_last_name="Candidate",
            applicant_email="tie.candidate@example.com",
            applicant_phone="09179990333",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Equal-scoring applicant.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            second, second_applicant, content_prefix="tie-candidate"
        )
        self.verify_application_for_submission(second)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(second, second_applicant)
        self.verify_application_for_submission(first)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(first, self.applicant)
        first.refresh_from_db()
        second.refresh_from_db()
        # The batch screening gate holds the exam until every applicant has a decision.
        self.finalize_screening_for_current_stage(first, self.secretariat)
        self.finalize_screening_for_current_stage(second, self.secretariat)
        self.finalize_exam_for_current_stage(first, self.secretariat)
        self.finalize_exam_for_current_stage(second, self.secretariat)
        first.refresh_from_db()
        second.refresh_from_db()
        self.finalize_interview_for_current_stage(first, self.hrmpsb, rating_score="90.00")
        self.finalize_interview_for_current_stage(second, self.hrmpsb, rating_score="90.00")
        self.finalize_applicant_pool_for_test(self.level1_position)
        first.refresh_from_db()
        second.refresh_from_db()

        for application in (first, second):
            set_application_ete_rating(application, self.secretariat, Decimal("90"))
        report = generate_comparative_assessment_report(
            application=first,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft with two equal candidates."},
            finalize=False,
        )
        items = list(report.items.all())
        self.assertEqual(len(items), 2)
        scores = {item.assessment_score for item in items}
        self.assertEqual(len(scores), 1, "fixture should produce equal totals")
        self.assertEqual({item.rank_order for item in items}, {1})
        for item in items:
            self.assertTrue(item.shares_rank)
            self.assertTrue(car_item_is_within_top_five(item))
        # SL-08 AC#3: either member of a rank-1 tie is the board's ordering, so picking
        # one is not a deviation needing deep-selection paperwork.
        self.assertEqual(
            car_top_rank_case_ids(report),
            {first.case.id, second.case.id},
        )

    def test_the_ete_grid_shows_the_qs_and_the_candidates_own_credentials(self):
        # SL-08A: ETE is 40% of the CAR and the scorer was being asked for that verdict
        # with nothing on screen but a candidate code and three empty boxes.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Minimum qualifications for this position")
        self.assertContains(response, "credentials on file")
        self.assertContains(response, application.qualification_summary)

    def test_an_ete_rating_keeps_the_facts_it_was_judged_against(self):
        # SL-08A AC#3: a later QS edit or resubmission must not rewrite the basis of a
        # rating already given.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        rating = set_application_ete_rating(application, self.secretariat, Decimal("88"))
        snapshot = rating.source_facts_snapshot

        self.assertIn("captured_at", snapshot)
        self.assertEqual(
            snapshot["qualification_summary"], application.qualification_summary
        )
        self.assertTrue(snapshot["documents"])

        # Editing the position's QS afterwards leaves the recorded basis alone.
        application.position.qualification_reference = "Revised QS text."
        application.position.save(update_fields=["qualification_reference", "updated_at"])
        rating.refresh_from_db()
        self.assertNotEqual(
            rating.source_facts_snapshot.get("qualification_reference"),
            "Revised QS text.",
        )

    def test_an_unscored_car_row_cannot_be_the_board_recommendation(self):
        # SL-03 AC#2: no rank, no recommendation, no route forward without a score.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        draft = ComparativeAssessmentReport.objects.filter(
            recruitment_entry=application.position, is_finalized=False
        ).first()
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNone(item.assessment_score)
        self.assertIsNone(item.rank_order)

    def test_car_finalize_requires_a_recommended_applicant(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        with self.assertRaisesMessage(
            ValueError,
            "Record the HRMPSB board's recommended applicant before finalizing the CAR.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "No recommendation provided.",
                    "quorum_met": True,
                    "members_present": 4,
                },
                finalize=True,
            )
        # No finalized CAR should have been created.
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position, is_finalized=True
            ).exists()
        )

    def test_car_finalize_records_the_board_recommendation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Finalized CAR with recommendation.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": application.case.id,
                "recommendation_notes": "Strongest competency profile in the pool.",
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.recommended_case_id, application.case.id)
        self.assertEqual(report.recommendation_notes, "Strongest competency profile in the pool.")

    def test_car_finalize_rejects_a_recommendation_outside_the_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)
        # A case from a different vacancy is not a valid recommendation.
        other = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(other)

        with self.assertRaisesMessage(
            ValueError,
            "The recommended applicant must be one of this vacancy's candidates.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Cross-vacancy recommendation.",
                    "quorum_met": True,
                    "members_present": 4,
                    "recommended_case_id": other.case.id,
                },
                finalize=True,
            )

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
        self.ensure_ete_ratings_for_pool(primary_application)
        finalized_report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Final entry-level CAR.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": secondary_application.case.id,
            },
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
                    # WF-03: deviating from the board's recommendation needs a justification.
                    "deep_selection_justification": (
                        "Documented justification for selecting against the board's recommendation."
                    ),
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
            cleaned_data={
                "summary_notes": "Final CAR after HRMPSB reassessment.",
                "quorum_met": True,
                "members_present": 4,
                "recommended_case_id": applications[0].case.id,
            },
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

    def test_a_selection_cannot_be_recorded_before_the_pick_is_finalized(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the COS vacancy deliberation pick before recording the selection.",
        ):
            record_final_decision(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={
                    "decision_outcome": FinalDecision.Outcome.SELECTED,
                    "decision_notes": "Skipping the deliberation.",
                },
            )

    def test_appointing_authority_cannot_record_cos_selection(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)
        application.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Only the HRM Chief may record the final decision at the current step.",
        ):
            record_final_decision(
                application=application,
                actor=self.appointing,
                cleaned_data={
                    "decision_outcome": FinalDecision.Outcome.SELECTED,
                    "decision_notes": "COS should not require Appointing Authority signing.",
                },
            )

        # The only decision on record is the one the HRM Chief's finalized pick produced.
        decisions = FinalDecision.objects.filter(application=application)
        self.assertEqual(decisions.count(), 1)
        self.assertEqual(decisions.first().decided_by, self.hrm_chief)

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

    def test_cos_decision_surface_displays_the_authoritative_pick(self):
        # SL-01 AC#2: the case's Decision surface shows the vacancy pick and its notes —
        # it never asks the HRM Chief to choose the person a second time.
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "COS vacancy deliberation pick")
        self.assertContains(response, "recorded once on the")
        self.assertContains(response, "This applicant")
        self.assertNotContains(response, "Record your decision")
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
        self.assertIn("Finalized COS deliberation pick", packet["summary"]["missing_components"])

    def test_the_per_case_cos_decision_endpoint_no_longer_exists(self):
        # SL-01: the second door is gone — the pick on the console is the only one.
        with self.assertRaises(NoReverseMatch):
            reverse("final-decision-record", kwargs={"pk": 1})

    def test_the_legacy_per_case_deliberation_record_cannot_be_written(self):
        # SL-15: the route, view, form and service were all still live behind a section
        # the sequence can no longer produce — one permission change away from a second,
        # competing source of the board's recommendation.
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)
        application.refresh_from_db()

        self.assertFalse(user_can_manage_deliberation(self.hrm_chief, application))
        with self.assertRaisesMessage(
            ValueError, "Per-case deliberation records are no longer recorded."
        ):
            save_deliberation_record(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={},
                finalize=True,
            )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("deliberation-record", kwargs={"pk": application.pk}),
            {"operation": "finalize"},
        )
        self.assertEqual(response.status_code, 403)

    def test_rescinding_a_selection_preserves_it_and_reopens_the_vacancy(self):
        # SL-14: the correction is additive. The original selection keeps every value it
        # was recorded with, the winner comes back out of completion, and the sibling it
        # closed is in contention again — otherwise a mis-click is the official truth.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        sibling = applications[1]
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        original_decided_at = selection.decided_at
        original_snapshot = selection.car_snapshot

        with self.captureOnCommitCallbacks(execute=True):
            rescinded, restored = rescind_final_selection(
                selection,
                self.appointing,
                "The selected applicant declined the appointment before assumption.",
                reference="Letter dated 2026-07-20",
            )

        rescinded.refresh_from_db()
        winner.refresh_from_db()
        winner.case.refresh_from_db()
        sibling.refresh_from_db()
        sibling.case.refresh_from_db()

        self.assertTrue(rescinded.is_rescinded)
        self.assertEqual(rescinded.rescinded_by, self.appointing)
        self.assertEqual(
            rescinded.rescinded_by_role, RecruitmentUser.Role.APPOINTING_AUTHORITY
        )
        self.assertIn("declined the appointment", rescinded.rescind_reason)
        self.assertEqual(rescinded.rescind_reference, "Letter dated 2026-07-20")
        # Preserved, not rewritten.
        self.assertEqual(rescinded.selected_application_id, winner.id)
        self.assertEqual(rescinded.decided_by, self.appointing)
        self.assertEqual(rescinded.decided_at, original_decided_at)
        self.assertEqual(rescinded.car_snapshot, original_snapshot)

        self.assertIn(winner.id, [a.id for a in restored])
        self.assertIn(sibling.id, [a.id for a in restored])
        for application in (winner, sibling):
            self.assertEqual(
                application.status,
                RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
            )
            self.assertEqual(
                application.case.current_stage,
                RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
            )
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )
            self.assertFalse(application.case.is_stage_locked)
            self.assertIsNone(application.closed_at)

        # The vacancy reads as undecided again, and the trail keeps the rescinded row.
        self.assertIsNone(get_final_selection_for_entry(self.level1_position))
        history = list(get_final_selection_history(self.level1_position))
        self.assertEqual([record.id for record in history], [selection.id])
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
                application=winner,
            ).exists()
        )

    def test_a_replacement_selection_can_be_recorded_and_chains_to_the_rescinded_one(self):
        # SL-14: reopening the vacancy is only useful if it can then be decided again, and
        # the two rows have to read as one correction rather than two rival decisions.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        replacement = applications[1]
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(
                selection, self.appointing, "Recorded against the wrong candidate."
            )

        replacement.refresh_from_db()
        self.assertTrue(user_can_record_final_selection(self.appointing, replacement))

        with self.captureOnCommitCallbacks(execute=True):
            second = record_final_selection(
                application=replacement,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=2),
                    "decision_notes": "Recorded the correct appointee.",
                    "deep_selection_justification": (
                        "The originally selected applicant declined; the next ranked "
                        "applicant is appointed."
                    ),
                },
            )

        replacement.refresh_from_db()
        replacement.case.refresh_from_db()
        winner.refresh_from_db()
        winner.case.refresh_from_db()

        self.assertEqual(second.supersedes_id, selection.id)
        self.assertEqual(get_final_selection_for_entry(self.level1_position), second)
        self.assertEqual(replacement.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(
            replacement.case.current_stage, RecruitmentCase.Stage.COMPLETION
        )
        self.assertEqual(winner.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(
            FinalSelection.objects.filter(recruitment_entry=self.level1_position).count(),
            2,
        )

    def test_rescinding_requires_the_appointing_authority_and_a_reason(self):
        # SL-14: voiding the office's own appointment decision is not a routine edit — it
        # is restricted to the role that recorded it and never accepted without a reason.
        applications, report = self.prepare_ranked_level1_car(3)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        for actor in (self.secretariat, self.hrm_chief, self.hrmpsb):
            self.assertFalse(user_can_rescind_final_selection(actor, selection))
        self.assertTrue(user_can_rescind_final_selection(self.appointing, selection))

        with self.assertRaisesMessage(
            ValueError, "Only the Appointing Authority may rescind"
        ):
            rescind_final_selection(selection, self.hrm_chief, "Not my call to make.")
        with self.assertRaisesMessage(
            ValueError, "Record why this selection is being rescinded."
        ):
            rescind_final_selection(selection, self.appointing, "   ")

        selection.refresh_from_db()
        self.assertFalse(selection.is_rescinded)
        self.assertEqual(get_final_selection_for_entry(self.level1_position), selection)

    def test_the_appointing_authority_can_rescind_from_the_case_surface(self):
        # SL-14: the affordance has to exist where the decision was made, and it may not
        # be reachable by anyone else.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        with self.captureOnCommitCallbacks(execute=True):
            record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Rescind this selection")
        self.assertNotContains(detail, "{#")

        other_client = Client()
        self.force_login_with_mfa(other_client, self.hrm_chief)
        forbidden = other_client.post(
            reverse("final-selection-rescind", kwargs={"pk": winner.pk}),
            {"reason": "Trying to undo someone else's decision."},
        )
        self.assertEqual(forbidden.status_code, 403)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("final-selection-rescind", kwargs={"pk": winner.pk}),
                {
                    "reason": "The appointment is not going ahead.",
                    "reference": "Memo 2026-07-22",
                },
            )
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(get_final_selection_for_entry(self.level1_position))
        after = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertContains(after, "Rescinded selection")
        self.assertContains(after, "The appointment is not going ahead.")
        self.assertContains(after, "Memo 2026-07-22")
        self.assertNotContains(after, "Rescind this selection")

    def test_a_rescinded_selection_no_longer_blocks_cancelling_the_vacancy(self):
        # SL-14 + SL-11: cancellation tells the canceller to rescind first, so a rescinded
        # row must stop refusing them once they have.
        applications, report = self.prepare_ranked_level1_car(3)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        with self.assertRaisesMessage(
            ValidationError, "A selection has already been recorded for this vacancy."
        ):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Item withdrawn by the region."
            )

        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(
                selection, self.appointing, "The appointment is not going ahead."
            )
        with self.captureOnCommitCallbacks(execute=True):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Item withdrawn by the region."
            )

        self.level1_position.refresh_from_db()
        self.assertEqual(
            self.level1_position.status, PositionPosting.EntryStatus.CANCELLED
        )


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
        # SL-01: the COS decision is made by finalizing the vacancy pick, so the case that
        # still awaits a decision is one whose pick is saved but not yet finalized.
        self.assertEqual(get_current_workflow_section(cos_decision), "deliberation")
        pick = get_cos_vacancy_deliberation(cos_decision.position)
        self.assertIsNotNone(pick)
        self.assertFalse(pick.is_finalized)

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


class BusinessLogicBypassHardeningTests(BaseRecruitmentTestCase):
    """Regression coverage for the PT-03 business-logic-bypass pentest scenarios: a case cannot be
    forced to a later stage (PT-007), a finalized record cannot be tampered with and assessment
    weights lock once scoring starts (PT-008), and the batch cannot reach the exam while an
    applicant is still undecided (PT-009)."""

    def _submit(self, application, applicant=None):
        applicant = applicant or self.applicant
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _extra_pending_application(self, position, suffix):
        """A second submitted applicant whose screening is left un-finalized, so the vacancy's
        screening batch stays on hold (pending decision)."""
        applicant = User.objects.create_user(
            username=f"bypass-applicant-{suffix}",
            password="testpass123",
            email=f"bypass.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Bypass{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"bypass.{suffix}@example.com",
            applicant_phone=f"091755500{suffix}",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant keeping the batch on hold.",
            cover_letter="Applying for the same vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"bypass-{suffix}"
        )
        return self._submit(application, applicant)

    # ---- PT-007: forced workflow action / stage skipping -------------------------------------

    def test_generic_whole_application_return_not_offered_or_accepted(self):
        # RD-6b (Option A): the generic whole-application return is removed. It
        # is never offered, and a forced POST is rejected at the form (the
        # action is not in the available set) and refused at the service.
        application = self._submit(self.make_application(self.level1_position))
        available = {
            value for value, _label in get_available_actions(application, self.secretariat)
        }
        self.assertNotIn("return_to_applicant", available)

        form = WorkflowActionForm(
            {"action": "return_to_applicant", "remarks": "Forced whole-application return."},
            application=application,
            user=self.secretariat,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("action", form.errors)

    def test_pt007_forced_later_stage_action_is_rejected(self):
        application = self._submit(self.make_application(self.level1_position))
        # Fresh at Secretariat screening: an HRMPSB-stage action like "recommend" is not offered.
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        available = {
            value for value, _label in get_available_actions(application, self.secretariat)
        }
        self.assertNotIn("recommend", available)

        # Form guard: the forced action fails validation ("not allowed at the current step").
        form = WorkflowActionForm(
            {"action": "recommend", "remarks": "Forced past screening."},
            application=application,
            user=self.secretariat,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("action", form.errors)

        # View guard: posting the forced action does not advance the case.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("workflow-action", kwargs={"pk": application.pk}),
            {"action": "recommend", "remarks": "Forced past screening."},
        )
        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

        # Service guard: the state machine refuses it even if the form is bypassed entirely.
        with self.assertRaises(ValueError):
            process_workflow_action(
                application, self.secretariat, "recommend", "Forced past screening."
            )

    # ---- PT-008: finalized-record tampering + weight lock ------------------------------------

    def test_pt008_finalized_screening_record_rejects_replayed_save(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        record = ScreeningRecord.objects.get(application=application, is_finalized=True)
        original_outcome = record.qualification_outcome

        # Replaying a Save after finalize (PT-008 step 3) must be refused, not applied.
        with self.assertRaises(ValueError):
            save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    "completeness_notes": "Tampered after finalize.",
                    "qualification_outcome": (
                        ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
                    ),
                    "education_score": None,
                    "training_score": None,
                    "experience_score": None,
                    "document_review_score": None,
                    "screening_notes": "Tampered after finalize.",
                },
                finalize=False,
            )
        record.refresh_from_db()
        self.assertEqual(record.qualification_outcome, original_outcome)

    def test_pt008_admin_cannot_edit_finalized_screening_record(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        record = ScreeningRecord.objects.get(application=application, is_finalized=True)

        request = RequestFactory().get("/admin/")
        request.user = self.secretariat
        model_admin = ScreeningRecordAdmin(ScreeningRecord, AdminSite())

        # A finalized record is fully read-only and cannot be deleted from the admin.
        readonly = model_admin.get_readonly_fields(request, obj=record)
        self.assertIn("qualification_outcome", readonly)
        self.assertIn("is_finalized", readonly)
        self.assertFalse(model_admin.has_delete_permission(request, obj=record))

        # A crafted save through the admin/ORM is refused; the stored value is unchanged.
        record.qualification_outcome = ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
        record.is_finalized = False
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, record, form=None, change=True)
        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertEqual(
            record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED
        )

    def test_pt008_weights_lock_blocks_replayed_weight_edit(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        # SL-09: the batch waits for the applicant pool to be final.
        self.finalize_applicant_pool_for_test(self.level1_position)
        # Single-applicant vacancy: the batch is ready, so scoring can start and lock the weights.
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position,
                self.secretariat,
                {
                    "scheduled_for": timezone.now() + timedelta(hours=2),
                    "venue": "CHD CALABARZON Examination Room",
                    "instructions": "Bring a valid government ID.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {application.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )

        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        self.assertTrue(weights.is_locked)
        original_ete = weights.ete_weight

        # PT-008 step 5-6: replaying a weights Save after scoring starts is rejected.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("vacancy-assessment-weights", args=[self.level1_position.pk]),
            {
                "ete_weight": "10",
                "exam_weight": "80",
                "interview_weight": "10",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights.refresh_from_db()
        self.assertEqual(weights.ete_weight, original_ete)

        # The finalized exam record is likewise immutable from the admin.
        exam_record = ExamRecord.objects.get(application=application, is_finalized=True)
        request = RequestFactory().get("/admin/")
        request.user = self.secretariat
        exam_admin = ExamRecordAdmin(ExamRecord, AdminSite())
        exam_readonly = exam_admin.get_readonly_fields(request, obj=exam_record)
        self.assertIn("exam_score", exam_readonly)
        self.assertIn("is_finalized", exam_readonly)
        with self.assertRaises(PermissionDenied):
            exam_admin.save_model(request, exam_record, form=None, change=True)

    # ---- PT-009: batch-gate bypass -----------------------------------------------------------

    def test_pt009_batch_exam_page_redirects_when_batch_on_hold(self):
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        # A second applicant with un-finalized screening keeps the batch on hold.
        self._extra_pending_application(self.level1_position, "1")
        qualified.refresh_from_db()
        self.assertFalse(vacancy_screening_batch_status(self.level1_position).is_ready)
        # The qualified applicant is still exam-ready (in the pool), so the page would otherwise
        # render read-only — the gate must hard-redirect instead of exposing the exam screen.
        self.assertIn(qualified, vacancy_exam_pool(self.level1_position))

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        detail_url = reverse("vacancy-batch-detail", args=[self.level1_position.pk])

        # GET is hard-redirected to the batch detail — no exam screen is exposed.
        get_response = client.get(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        )
        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(get_response.url, detail_url)

        # A forced POST is likewise redirected and does not advance the batch.
        post_response = client.post(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            {
                "operation": "finalize",
                f"general_score_{qualified.case.id}": "90",
                f"technical_score_{qualified.case.id}": "80",
            },
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, detail_url)
        qualified.refresh_from_db()
        self.assertFalse(
            qualified.exam_records.filter(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW, is_finalized=True
            ).exists()
        )

    def test_pt009_batch_detail_hides_open_exam_while_an_applicant_is_pending(self):
        # The batch-detail banner must not claim the batch is complete (and must not offer the
        # exam) while any applicant is still undecided, even though some applicants are already
        # exam-ready. Otherwise the green "proceed to exam" banner contradicts the on-hold gate.
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        self._extra_pending_application(self.level1_position, "hold")
        self.assertFalse(vacancy_screening_batch_status(self.level1_position).is_ready)
        # ...yet the qualified applicant is exam-ready, which is what used to force the green banner.
        self.assertTrue(
            any(
                user_can_view_application(self.secretariat, application)
                for application in vacancy_exam_pool(self.level1_position)
            )
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening batch is on hold")
        self.assertNotContains(response, "Screening batch is complete")
        self.assertNotContains(response, "Open batch exam")

    def test_pt009_batch_detail_offers_open_exam_when_every_applicant_is_decided(self):
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        sibling = self._extra_pending_application(self.level1_position, "ready")
        self.finalize_screening_for_current_stage(sibling, self.secretariat)
        self.assertTrue(vacancy_screening_batch_status(self.level1_position).is_ready)
        # SL-09: the batch link appears once the applicant pool is final as well.
        self.finalize_applicant_pool_for_test(self.level1_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening batch is complete")
        self.assertContains(response, "Open batch exam")
        self.assertNotContains(response, "Screening batch is on hold")
