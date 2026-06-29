from contextlib import nullcontext
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from recruitment.models import (
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompetencyRatingTemplate,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    EvidenceVaultItem,
    ExamRecord,
    FinalDecision,
    FinalSelection,
    PositionPosting,
    PositionReference,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)
from recruitment.requirements import get_required_applicant_document_requirements
from recruitment.services import (
    create_competency_rating_template,
    generate_comparative_assessment_report,
    get_competency_rating_template,
    get_current_workflow_section,
    get_published_competency_rating_template,
    record_final_selection,
    save_deliberation_record,
    save_exam_record,
    save_exam_schedule,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    submit_application,
    upload_evidence_item,
)


SEED_DESCRIPTION_PREFIX = "E2E seed:"
SEED_USER_PREFIX = "e2e_seed_applicant_"
PRIMARY_REFERENCES = {
    "cos_screening": "RG-COS-test-screening",
    "exam": "RG-PLT-test-exam",
    "interview": "RG-PLT-test-interview",
    "deliberation": "RG-PLT-test-deliberation",
    "cos_decision": "RG-COS-test-decision",
    "final_selection": "RG-PLT-test-final-selection",
    "aa_decision": "RG-PLT-test-aa-decision",
    "aa_return": "RG-PLT-test-aa-return",
    "completion": "RG-PLT-test-completion",
}


class Command(BaseCommand):
    help = "Seed synthetic E2E recruitment cases for live UI verification and user testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-existing",
            action="store_true",
            help="Do not delete prior E2E seed data before creating the cases.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the seed set inside a rolled-back transaction.",
        )
        parser.add_argument(
            "--send-email",
            action="store_true",
            help="Allow normal notification email delivery while seeding.",
        )
        parser.add_argument(
            "--base-email",
            default="j3r1c02@gmail.com",
            help="Base email used for deterministic synthetic applicant aliases.",
        )

    def handle(self, *args, **options):
        email_context = (
            nullcontext()
            if options["send_email"]
            else override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
        )
        with email_context:
            if options["dry_run"]:
                with transaction.atomic():
                    seeded = self._seed(options)
                    summary_rows = self._summary_rows(seeded)
                    transaction.set_rollback(True)
            else:
                seeded = self._seed(options)
                summary_rows = self._summary_rows(seeded)

        suffix = "would be seeded" if options["dry_run"] else "seeded"
        self.stdout.write(self.style.SUCCESS(f"{len(seeded)} primary E2E case(s) {suffix}."))
        for row in summary_rows:
            self.stdout.write(
                "{label}: {reference} [{stage}/{section}] {url}".format(
                    **row,
                )
            )

    def _summary_rows(self, seeded):
        rows = []
        for label, application in seeded.items():
            application.refresh_from_db()
            application.case.refresh_from_db()
            rows.append(
                {
                    "label": label,
                    "reference": application.reference_label,
                    "stage": application.case.current_stage,
                    "section": get_current_workflow_section(application),
                    "url": reverse("application-detail", kwargs={"pk": application.pk}),
                }
            )
        return rows

    def _seed(self, options):
        with transaction.atomic():
            if not options["keep_existing"]:
                self._reset_seed_data()
            users = self._resolve_users()
            applicants = self._resolve_applicants(options["base_email"], minimum_count=6)

            seeded = {
                "Secretariat COS screening": self._seed_cos_screening_case(users, applicants[0]),
                "Exam wizard": self._seed_exam_case(users, applicants[0]),
                "Interview wizard": self._seed_interview_case(users, applicants[4]),
                "Deliberation wizard": self._seed_deliberation_case(users, applicants[5]),
                "COS decision wizard": self._seed_cos_decision_case(users, applicants[1]),
                "Final selection deep-lock": self._seed_final_selection_case(
                    users,
                    applicants[:6],
                ),
                "Appointing Authority decision": self._seed_aa_decision_case(
                    users,
                    applicants[:2],
                ),
                "Appointing Authority CAR return": self._seed_aa_return_case(
                    users,
                    applicants[2:4],
                ),
                "Completion wizard": self._seed_completion_case(users, applicants[2:4]),
            }
        return seeded

    def _reset_seed_data(self):
        reference_prefixes = tuple(PRIMARY_REFERENCES.values())
        entries = PositionPosting.objects.filter(description__startswith=SEED_DESCRIPTION_PREFIX)
        applications = RecruitmentApplication.objects.filter(position__in=entries)
        for reference in reference_prefixes:
            applications = applications | RecruitmentApplication.objects.filter(
                reference_number__startswith=reference,
            )
        entries = entries | PositionPosting.objects.filter(applications__in=applications)
        entries = entries.distinct()
        applications = applications.distinct()

        FinalSelection.objects.filter(recruitment_entry__in=entries).delete()
        FinalDecision.objects.filter(recruitment_entry__in=entries).delete()
        ComparativeAssessmentReportItem.objects.filter(report__recruitment_entry__in=entries).delete()
        DeliberationRecord.objects.filter(recruitment_entry__in=entries).delete()
        ComparativeAssessmentReport.objects.filter(recruitment_entry__in=entries).delete()
        EvidenceVaultItem.objects.filter(recruitment_entry__in=entries).delete()
        applications.delete()
        entries.delete()

        User = get_user_model()
        User.objects.filter(username__startswith=SEED_USER_PREFIX).delete()

    def _resolve_users(self):
        return {
            "secretariat": self._get_role_user(RecruitmentUser.Role.SECRETARIAT, "secretariat"),
            "hrm_chief": self._get_role_user(RecruitmentUser.Role.HRM_CHIEF, "hrm_chief"),
            "hrmpsb": self._get_role_user(RecruitmentUser.Role.HRMPSB_MEMBER, "hrmpsb_member", "hrmpsb"),
            "appointing": self._get_role_user(
                RecruitmentUser.Role.APPOINTING_AUTHORITY,
                "appointing_authority",
                "appointing",
            ),
        }

    def _get_role_user(self, role, *preferred_usernames):
        User = get_user_model()
        for username in preferred_usernames:
            user = User.objects.filter(username=username, role=role, is_active=True).first()
            if user:
                return user
        user = User.objects.filter(role=role, is_active=True).order_by("id").first()
        if not user:
            role_label = RecruitmentUser.Role(role).label
            raise CommandError(f"Create an active {role_label} user before seeding E2E cases.")
        return user

    def _resolve_applicants(self, base_email, minimum_count):
        User = get_user_model()
        applicants = list(
            User.objects.filter(role=RecruitmentUser.Role.APPLICANT, is_active=True)
            .order_by("id")[:minimum_count]
        )
        next_index = 1
        while len(applicants) < minimum_count:
            username = f"{SEED_USER_PREFIX}{next_index:02d}"
            email = self._email_alias(base_email, next_index)
            user, _created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "role": RecruitmentUser.Role.APPLICANT,
                    "first_name": "E2E",
                    "last_name": f"Applicant {next_index:02d}",
                    "is_active": True,
                },
            )
            if user.role != RecruitmentUser.Role.APPLICANT:
                raise CommandError(f"Seed username {username} exists but is not an applicant.")
            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
            user.set_unusable_password()
            user.email = email
            user.first_name = "E2E"
            user.last_name = f"Applicant {next_index:02d}"
            user.save(update_fields=["password", "email", "first_name", "last_name", "is_active"])
            applicants.append(user)
            next_index += 1
        return applicants

    def _email_alias(self, base_email, index):
        local, at, domain = base_email.partition("@")
        if not at:
            return f"e2e.seed.{index:02d}@example.test"
        return f"{local}+e2e{index:02d}@{domain}"

    def _position_reference(self, title):
        reference = PositionReference.objects.filter(position_title=title, is_active=True).first()
        if not reference:
            raise CommandError(f"Missing active PositionReference: {title}")
        if reference.routing_level is None:
            raise CommandError(f"PositionReference {title} is missing a routing level.")
        return reference

    def _create_entry(self, *, code, branch, level, reference_title, created_by, candidate_count=1):
        reference = self._position_reference(reference_title)
        entry = PositionPosting(
            position_reference=reference,
            branch=branch,
            level=level,
            item_number=(
                f"ITEM-{code}" if branch == PositionPosting.Branch.PLANTILLA else ""
            ),
            intake_mode=(
                PositionPosting.IntakeMode.FIXED_PERIOD
                if branch == PositionPosting.Branch.PLANTILLA
                else PositionPosting.IntakeMode.POOLING
            ),
            status=PositionPosting.EntryStatus.ACTIVE,
            opening_date=timezone.localdate(),
            closing_date=(
                PositionPosting.calculate_plantilla_closing_date(timezone.localdate())
                if branch == PositionPosting.Branch.PLANTILLA
                else None
            ),
            description=f"{SEED_DESCRIPTION_PREFIX} {code} ({candidate_count} candidate(s))",
            requirements="Synthetic E2E verification recruitment entry.",
            created_by=created_by,
            updated_by=created_by,
        )
        entry.full_clean()
        entry.save()
        return entry

    def _create_application(self, *, entry, applicant, reference, first_name, last_name, label):
        performance_rating_applicability = (
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            if entry.branch == PositionPosting.Branch.COS
            else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=entry,
            applicant_first_name=first_name,
            applicant_last_name=last_name,
            applicant_email=applicant.email or self._email_alias("j3r1c02@gmail.com", applicant.pk),
            applicant_phone=f"0917{application_safe_number(applicant.pk)}",
            performance_rating_applicability=performance_rating_applicability,
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary=f"Synthetic qualifications for {label}.",
            cover_letter=f"Synthetic cover letter for {label}.",
        )
        self._upload_required_documents(
            application,
            applicant,
            performance_rating_applicability=performance_rating_applicability,
            content_prefix=reference,
        )
        now = timezone.now()
        application.otp_hash = f"e2e-seed-{reference}"
        application.otp_requested_at = now
        application.otp_verified_at = now
        application.otp_expires_at = now + timedelta(days=1)
        application.save(
            update_fields=[
                "otp_hash",
                "otp_requested_at",
                "otp_verified_at",
                "otp_expires_at",
                "updated_at",
            ]
        )
        submit_application(application, applicant)
        application.refresh_from_db()
        self._set_reference(application, reference)
        return application

    def _upload_required_documents(
        self,
        application,
        applicant,
        *,
        performance_rating_applicability,
        content_prefix,
    ):
        performance_rating_not_applicable = (
            performance_rating_applicability
            == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
        )
        for requirement in get_required_applicant_document_requirements(
            branch=application.branch,
            performance_rating_not_applicable=performance_rating_not_applicable,
        ):
            upload_evidence_item(
                application=application,
                actor=applicant,
                label=requirement.title,
                uploaded_file=self._document_upload(requirement.code, content_prefix),
                document_key=requirement.code,
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

    def _document_upload(self, requirement_code, content_prefix):
        raw_bytes = (
            b"%PDF-1.4\n"
            + f"{content_prefix}:{requirement_code}\n".encode("utf-8")
            + b"%%EOF\n"
        )
        return SimpleUploadedFile(
            f"{requirement_code}.pdf",
            raw_bytes,
            content_type="application/pdf",
        )

    def _set_reference(self, application, reference):
        if (
            RecruitmentApplication.objects.exclude(pk=application.pk)
            .filter(reference_number=reference)
            .exists()
        ):
            raise CommandError(f"Reference number {reference} already exists.")
        application.reference_number = reference
        application.save(update_fields=["reference_number", "updated_at"])
        application.refresh_from_db()

    def _finalize_screening(self, application, actor, score="91.00"):
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
                "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
                "completeness_notes": "All required E2E documents were reviewed.",
                "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
                "education_score": score,
                "training_score": score,
                "experience_score": score,
                "document_review_score": "",
                "screening_notes": "E2E screening finalized as qualified.",
            },
            finalize=True,
        )

    def _finalize_exam(self, application, actor, score="89.00"):
        save_exam_schedule(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": timezone.now() + timedelta(hours=2),
                "venue": "CHD CALABARZON Examination Room",
                "instructions": "Bring a valid government ID.",
            },
        )
        return save_exam_record(
            application=application,
            actor=actor,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": score,
                "exam_result": "",
                "technical_score": score,
                "technical_result": "",
                "general_score": score,
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "E2E examination record finalized.",
            },
            finalize=True,
        )

    @staticmethod
    def _competency_level(score, scale_max):
        """Accept either a competency level (1..scale_max) or a 0-100 score and
        return an in-range competency level for the seeded ratings."""
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = scale_max
        if value > scale_max:
            value = value / 100 * scale_max
        return max(1, min(scale_max, int(round(value))))

    def _ensure_published_rating_sheet(self, application, actor):
        """Make sure the vacancy has a published competency rating sheet so the
        HRMPSB rating below can be scored against it."""
        entry = application.position
        template = get_published_competency_rating_template(entry)
        if template is not None:
            return template
        template = get_competency_rating_template(entry)
        if template is None:
            template = create_competency_rating_template(entry, actor)
        template.status = CompetencyRatingTemplate.Status.PUBLISHED
        template.published_at = timezone.now()
        template.save(update_fields=["status", "published_at", "updated_at"])
        return template

    def _finalize_interview(self, application, *, session_actor, rating_actor, score=3):
        scheduled_for = timezone.now() + timedelta(days=2)
        save_interview_session(
            application=application,
            actor=session_actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": "E2E Verification Room",
                "session_notes": "E2E interview session scheduled.",
            },
            finalize=False,
        )
        template = self._ensure_published_rating_sheet(application, session_actor)
        level = self._competency_level(score, template.scale_max)
        competency_scores = {
            competency: level for competency in template.competencies.all()
        }
        save_interview_rating(
            application=application,
            actor=rating_actor,
            cleaned_data={
                "competency_scores": competency_scores,
                "rating_notes": "E2E interview rating recorded.",
                "justification": "Consistent competency performance across the panel sheet.",
            },
        )
        return save_interview_session(
            application=application,
            actor=session_actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": "E2E Verification Room",
                "session_notes": "E2E interview session finalized.",
            },
            finalize=True,
        )

    def _finalize_deliberation(self, application, actor, rank):
        return save_deliberation_record(
            application=application,
            actor=actor,
            cleaned_data={
                "deliberated_at": timezone.now(),
                "deliberation_minutes": "E2E panel deliberation minutes.",
                "recommendation": f"Recommended for CAR rank {rank}.",
                "decision_support_summary": f"E2E decision-support summary for rank {rank}.",
                "quorum_status": DeliberationRecord.QuorumStatus.MET,
                "attendance_notes": "E2E HRMPSB quorum met.",
                "ranking_position": rank,
                "ranking_notes": f"E2E ranking basis for rank {rank}.",
            },
            finalize=True,
        )

    def _close_entry_pool(self, entry):
        entry.status = PositionPosting.EntryStatus.CLOSED
        entry.save(update_fields=["status", "is_active", "updated_at"])
        entry.refresh_from_db()

    def _advance_to_hrmpsb(self, application, users, *, score="91.00"):
        self._finalize_screening(application, users["secretariat"], score=score)
        self._finalize_exam(application, users["secretariat"], score=score)
        application.refresh_from_db()
        application.case.refresh_from_db()
        if application.case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW:
            raise CommandError(f"{application.reference_label} did not advance to HRMPSB review.")

    def _prepare_plantilla_car_group(
        self,
        *,
        users,
        applicants,
        code,
        candidate_count,
        finalize_car,
    ):
        entry = self._create_entry(
            code=code,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            reference_title="Administrative Aide VI",
            created_by=users["secretariat"],
            candidate_count=candidate_count,
        )
        applications = []
        for index in range(candidate_count):
            reference = code if index == 0 else f"{code}-{index + 1:02d}"
            application = self._create_application(
                entry=entry,
                applicant=applicants[index],
                reference=reference,
                first_name="E2E",
                last_name=(
                    "Deep Lock"
                    if index == candidate_count - 1 and candidate_count > 5
                    else f"CAR {index + 1:02d}"
                ),
                label=reference,
            )
            score = f"{96 - index}.00"
            self._advance_to_hrmpsb(application, users, score=score)
            self._finalize_interview(
                application,
                session_actor=users["secretariat"],
                rating_actor=users["hrmpsb"],
                score=score,
            )
            applications.append(application)

        self._close_entry_pool(entry)
        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
            application.position.refresh_from_db()
        draft = generate_comparative_assessment_report(
            application=applications[0],
            actor=users["secretariat"],
            cleaned_data={"summary_notes": f"E2E CAR draft for {code}."},
            finalize=False,
        )
        for index, application in enumerate(applications, start=1):
            self._finalize_deliberation(application, users["hrmpsb"], rank=index)
        if finalize_car:
            report = generate_comparative_assessment_report(
                application=applications[0],
                actor=users["secretariat"],
                cleaned_data={"summary_notes": f"E2E finalized CAR for {code}."},
                finalize=True,
            )
            for application in applications:
                application.refresh_from_db()
                application.case.refresh_from_db()
            return applications, report
        return applications, draft

    def _seed_exam_case(self, users, applicant):
        entry = self._create_entry(
            code=PRIMARY_REFERENCES["exam"],
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_2,
            reference_title="Medical Officer V",
            created_by=users["hrm_chief"],
        )
        application = self._create_application(
            entry=entry,
            applicant=applicant,
            reference=PRIMARY_REFERENCES["exam"],
            first_name="E2E",
            last_name="Exam",
            label=PRIMARY_REFERENCES["exam"],
        )
        self._finalize_screening(application, users["hrm_chief"])
        application.refresh_from_db()
        application.case.refresh_from_db()
        return application

    def _seed_cos_screening_case(self, users, applicant):
        entry = self._create_entry(
            code=PRIMARY_REFERENCES["cos_screening"],
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            reference_title="Administrative Assistant I",
            created_by=users["secretariat"],
        )
        application = self._create_application(
            entry=entry,
            applicant=applicant,
            reference=PRIMARY_REFERENCES["cos_screening"],
            first_name="E2E",
            last_name="COS Screening",
            label=PRIMARY_REFERENCES["cos_screening"],
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        return application

    def _seed_interview_case(self, users, applicant):
        entry = self._create_entry(
            code=PRIMARY_REFERENCES["interview"],
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            reference_title="Administrative Aide VI",
            created_by=users["secretariat"],
        )
        application = self._create_application(
            entry=entry,
            applicant=applicant,
            reference=PRIMARY_REFERENCES["interview"],
            first_name="E2E",
            last_name="Interview",
            label=PRIMARY_REFERENCES["interview"],
        )
        self._advance_to_hrmpsb(application, users)
        save_interview_session(
            application=application,
            actor=users["secretariat"],
            cleaned_data={
                "scheduled_for": timezone.now() + timedelta(days=2),
                "location": "E2E Verification Room",
                "session_notes": "E2E interview scheduled (awaiting ratings).",
            },
            finalize=False,
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        return application

    def _seed_deliberation_case(self, users, applicant):
        entry = self._create_entry(
            code=PRIMARY_REFERENCES["deliberation"],
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            reference_title="Administrative Aide VI",
            created_by=users["secretariat"],
        )
        application = self._create_application(
            entry=entry,
            applicant=applicant,
            reference=PRIMARY_REFERENCES["deliberation"],
            first_name="E2E",
            last_name="Deliberation",
            label=PRIMARY_REFERENCES["deliberation"],
        )
        self._advance_to_hrmpsb(application, users)
        self._finalize_interview(
            application,
            session_actor=users["secretariat"],
            rating_actor=users["hrmpsb"],
        )
        self._close_entry_pool(entry)
        application.refresh_from_db()
        application.case.refresh_from_db()
        application.position.refresh_from_db()
        generate_comparative_assessment_report(
            application=application,
            actor=users["secretariat"],
            cleaned_data={"summary_notes": "E2E CAR draft for deliberation."},
            finalize=False,
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        return application

    def _seed_cos_decision_case(self, users, applicant):
        entry = self._create_entry(
            code=PRIMARY_REFERENCES["cos_decision"],
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            reference_title="Administrative Assistant I",
            created_by=users["secretariat"],
        )
        application = self._create_application(
            entry=entry,
            applicant=applicant,
            reference=PRIMARY_REFERENCES["cos_decision"],
            first_name="E2E",
            last_name="COS Decision",
            label=PRIMARY_REFERENCES["cos_decision"],
        )
        self._advance_cos_to_hrm_chief_decision(application, users)
        application.refresh_from_db()
        application.case.refresh_from_db()
        return application

    def _advance_cos_to_hrm_chief_decision(self, application, users):
        self._finalize_screening(application, users["secretariat"])
        self._finalize_exam(application, users["secretariat"])
        application.refresh_from_db()
        application.case.refresh_from_db()
        self._finalize_screening(application, users["hrm_chief"], score="93.00")
        self._finalize_exam(application, users["hrm_chief"], score="92.00")
        self._finalize_interview(
            application,
            session_actor=users["hrm_chief"],
            rating_actor=users["hrm_chief"],
            score="91.00",
        )
        save_deliberation_record(
            application=application,
            actor=users["hrm_chief"],
            cleaned_data={
                "deliberated_at": timezone.now(),
                "deliberation_minutes": "E2E COS deliberation minutes.",
                "recommendation": "COS applicant recommended for final decision.",
                "decision_support_summary": "E2E COS decision-support packet is complete.",
                "quorum_status": DeliberationRecord.QuorumStatus.MET,
                "attendance_notes": "HRM Chief completed COS review.",
                "ranking_position": None,
                "ranking_notes": "COS ranking not required.",
            },
            finalize=True,
        )

    def _seed_final_selection_case(self, users, applicants):
        applications, _report = self._prepare_plantilla_car_group(
            users=users,
            applicants=applicants,
            code=PRIMARY_REFERENCES["final_selection"],
            candidate_count=6,
            finalize_car=True,
        )
        return applications[0]

    def _seed_aa_decision_case(self, users, applicants):
        applications, _report = self._prepare_plantilla_car_group(
            users=users,
            applicants=applicants,
            code=PRIMARY_REFERENCES["aa_decision"],
            candidate_count=2,
            finalize_car=True,
        )
        return applications[0]

    def _seed_aa_return_case(self, users, applicants):
        applications, _report = self._prepare_plantilla_car_group(
            users=users,
            applicants=applicants,
            code=PRIMARY_REFERENCES["aa_return"],
            candidate_count=2,
            finalize_car=True,
        )
        return applications[0]

    def _seed_completion_case(self, users, applicants):
        applications, report = self._prepare_plantilla_car_group(
            users=users,
            applicants=applicants,
            code=PRIMARY_REFERENCES["completion"],
            candidate_count=2,
            finalize_car=True,
        )
        selected_item = report.items.order_by("rank_order", "created_at").first()
        record_final_selection(
            application=applications[0],
            actor=users["appointing"],
            cleaned_data={
                "selected_item": selected_item,
                "is_deep_selection": False,
                "deep_selection_justification": "",
                "decision_notes": "E2E final selection recorded for completion testing.",
            },
        )
        selected_application = selected_item.application
        selected_application.refresh_from_db()
        selected_application.case.refresh_from_db()
        completion_record = CompletionRecord(
            application=selected_application,
            recruitment_case=selected_application.case,
            tracked_by=users["secretariat"],
            branch=selected_application.branch,
            level=selected_application.level,
            deadline=timezone.localdate() + timedelta(days=10),
            remarks="E2E completion tracking initialized.",
        )
        completion_record.full_clean()
        completion_record.save()
        requirement = CompletionRequirement(
            completion_record=completion_record,
            item_label="Appointment papers",
            status=CompletionRequirement.RequirementStatus.PENDING,
            notes="E2E pending requirement.",
            display_order=0,
        )
        requirement.full_clean()
        requirement.save()
        return selected_application


def application_safe_number(value):
    return str(value).zfill(7)[-7:]
