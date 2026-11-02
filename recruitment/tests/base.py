"""Shared test setup: BaseRecruitmentTestCase (users, vacancies and the helpers most tests
use) and the helper mixins that several test classes share."""

import io
import re
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import (
    ComparativeAssessmentReport,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    CompetencyRatingTemplate,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from ..permissions import (
    INTERNAL_MFA_USER_SESSION_KEY,
    INTERNAL_MFA_VERIFIED_SESSION_KEY,
)
from ..requirements import (
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from ..services import (
    generate_comparative_assessment_report,
    get_latest_finalized_comparative_assessment_report,
    issue_application_otp,
    process_workflow_action,
    record_final_decision,
    record_final_selection,
    save_cos_vacancy_deliberation,
    get_latest_final_decision,
    save_exam_record,
    save_exam_schedule,
    create_competency_rating_template,
    get_competency_rating_template,
    set_application_ete_rating,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    upload_evidence_item,
    verify_application_otp,
)


User = get_user_model()


@override_settings(
    CAPTCHA_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class BaseRecruitmentTestCase(TestCase):
    # Most tests schedule an exam or interview a little ahead and record its results straight
    # away, standing in for "the office records them afterwards". The rule that results wait
    # for the sitting has its own tests (SecretariatEssentialsTests), which set this to True.
    enforce_event_held = False

    def setUp(self):
        if not self.enforce_event_held:
            for target, value in (
                ("recruitment.services.clock.event_not_held_yet", False),
                ("recruitment.services.clock.event_held_by", True),
            ):
                held = patch(target, return_value=value)
                held.start()
                self.addCleanup(held.stop)
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
        # The shared COS vacancy is one batch (it was "pooling", which meant one batch with
        # no closing date, before the C1 merge); open-pool behaviour has its own fixture.
        self.cos_position = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.OPENING_BASED,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
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

    # -- the step-by-step applicant portal (details -> verify -> documents -> submit) --
    def real_pdf_upload(self, label, filename=None):
        """A small, genuinely parseable PDF (the portal opens every uploaded PDF)."""
        from reportlab.pdfgen import canvas as rl_canvas

        buffer = io.BytesIO()
        pdf = rl_canvas.Canvas(buffer)
        pdf.drawString(72, 720, f"Test document: {label}")
        pdf.showPage()
        pdf.save()
        return SimpleUploadedFile(
            filename or f"{label}.pdf", buffer.getvalue(), content_type="application/pdf"
        )

    def start_portal_draft(self, client, position, email, **details):
        """Step 1: name, email, mobile and consent. Emails a code; returns the response."""
        payload = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "email": email,
            "phone": "09171234567",
            "privacy_consent": "on",
        }
        payload.update(details)
        return self.post_portal_intake(client, position, payload)

    def verify_portal_draft(self, client, application):
        """Step 2: enter the code from the last email (gives this client the draft)."""
        return client.post(
            reverse("applicant-otp", kwargs={"token": application.public_token}),
            {"action": "verify", "otp": self.extract_otp_from_last_email()},
        )

    def portal_answers(self, position, **overrides):
        answers = {
            "first_name": "Pat",
            "last_name": "Applicant",
            "phone": "09171234567",
            "qualification_summary": "Qualified applicant with complete supporting credentials.",
            "cover_letter": "Please consider this application.",
            "performance_rating_applicability": (
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
                if position.branch == PositionPosting.Branch.COS
                else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            ),
        }
        answers.update(overrides)
        return answers

    def save_portal_answers(self, client, application, **overrides):
        return client.post(
            reverse("applicant-application", kwargs={"token": application.public_token}),
            {**self.portal_answers(application.position, **overrides), "action": "autosave"},
        )

    def upload_portal_document(self, client, application, code, uploaded_file=None):
        return client.post(
            reverse(
                "applicant-document-part-upload",
                kwargs={"token": application.public_token, "code": code},
            ),
            {"file": uploaded_file or self.real_pdf_upload(f"{application.pk}-{code}")},
        )

    def upload_required_portal_documents(self, client, application, skip=()):
        from recruitment.applicant_documents import build_document_slot_states

        application.refresh_from_db()
        rating_applies = (
            application.position.branch == PositionPosting.Branch.PLANTILLA
            and application.performance_rating_applicability
            == RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )
        codes = [
            slot["code"]
            for slot in build_document_slot_states(
                application, performance_rating_applicable=rating_applies
            )
            if slot["required_now"] and slot["code"] not in skip
        ]
        for code in codes:
            response = self.upload_portal_document(client, application, code)
            self.assertEqual(response.status_code, 200, response.content)
        return codes

    def submit_portal_draft(self, client, application, **answer_overrides):
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(
                reverse("applicant-application", kwargs={"token": application.public_token}),
                {
                    **self.portal_answers(application.position, **answer_overrides),
                    "submission_confirmation": "on",
                    "action": "submit",
                },
                follow=True,
            )

    def open_portal_draft(self, email, position=None, client=None, **details):
        """Start + verify. Returns (client, draft)."""
        position = position or self.level1_position
        client = client or Client()
        mail.outbox.clear()
        response = self.start_portal_draft(client, position, email, **details)
        self.assertEqual(response.status_code, 302)
        application = (
            RecruitmentApplication.objects.filter(position=position, applicant_email__iexact=email)
            .order_by("-created_at", "-pk")
            .first()
        )
        self.verify_portal_draft(client, application)
        return client, application

    def apply_through_portal(self, email, position=None, **answer_overrides):
        """The whole applicant journey. Returns (client, application, receipt response)."""
        client, application = self.open_portal_draft(email, position)
        self.save_portal_answers(client, application, **answer_overrides)
        self.upload_required_portal_documents(client, application)
        response = self.submit_portal_draft(client, application, **answer_overrides)
        application.refresh_from_db()
        self.assertIsNotNone(application.submitted_at, response.content[:2000])
        return client, application, response

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
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
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
            if recommended_case_id is None and not report.recommends_none:
                rank_one = candidate_items.filter(rank_order=1).first()
                recommended_case_id = rank_one.recruitment_case_id if rank_one else None
            cleaned_data = {
                "selected_item": selected_item,
                "decision_notes": decision_notes,
            }
            if (
                report.recommends_none
                or (
                    recommended_case_id
                    and selected_item.recruitment_case_id != recommended_case_id
                )
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


class BatchScreeningHelpers:
    """Submitted applications and a case awaiting resubmission, for screening-batch tests."""

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


class BatchExamHelpers:
    """Two screened-qualified applications and an exam schedule, for batch-exam tests."""

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


class InterviewSessionHelpers:
    """An interview session payload and a Plantilla session waiting for ratings."""

    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Conference Room A",
            "session_notes": "Structured interview schedule prepared.",
        }
        payload.update(overrides)
        return payload

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
