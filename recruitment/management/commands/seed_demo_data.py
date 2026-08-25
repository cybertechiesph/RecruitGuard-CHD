"""Seed a small, curated demo dataset for panel presentations.

This command wipes ALL existing applications and postings and replaces them with:

  * A PLANTILLA vacancy with two real-inbox applicants -- one freshly submitted
    and one advanced to the exam step (screening passed, awaiting the exam).
  * A COS vacancy with two real-inbox applicants -- one freshly submitted and one
    advanced to the exam step.
  * A SEPARATE, self-contained plantilla vacancy carried all the way to a
    FINALIZED Comparative Assessment Report (CAR) parked at the Appointing
    Authority, using realistic DUMMY applicants. This exists only so the panel can
    see the Appointing Authority workflow -- which is impossible to reach in a live
    demo without waiting for a real vacancy to close so the CAR can be generated.

Each case is built by driving the real services (``submit_application``,
``save_screening_review``, ``generate_comparative_assessment_report`` ...), so
every record lands in a genuinely valid state.

Workflow steps need an internal actor for each role. Real staff accounts are used
when present (matched by role); otherwise disposable ``demo_staff_*`` actors are
created just to author the records. The CAR-demo dummy applicants never receive
real email even under ``--send-email``. Real staff logins and the
``PositionReference`` catalog are never deleted.

SAFETY: this command is destructive (it deletes ALL applications/postings and
inserts fakes), so it REFUSES to run when ``settings.DEBUG`` is False unless
``--force`` is passed. Only ever run it against a disposable demo/staging
database, NEVER one with real applicant submissions.
"""

from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from recruitment.models import (
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompetencyRatingTemplate,
    CosSelectionRound,
    CosVacancyDeliberation,
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
    MIN_INTERVIEW_RATINGS_PLANTILLA,
    create_competency_rating_template,
    create_default_position_document_requirements,
    generate_comparative_assessment_report,
    get_competency_rating_template,
    get_current_workflow_section,
    get_published_competency_rating_template,
    save_exam_record,
    save_exam_schedule,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    set_application_ete_rating,
    submit_application,
    upload_evidence_item,
)

LOCMEM_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# --- purge / tagging markers ---------------------------------------------------
DEMO_CREATOR_USERNAME = "demo_seed_bot"
DEMO_APPLICANT_PREFIX = "demo_applicant_"
# Disposable internal actors created only to author workflow records when no real
# staff account exists for a role. Purged and recreated on every run.
DEMO_STAFF_PREFIX = "demo_staff_"


# --- curated demo cases --------------------------------------------------------
# Two real-inbox applicants share one open plantilla vacancy: one fresh, one at exam.
PLANTILLA_VACANCY_TITLE = "Administrative Aide VI"
PLANTILLA_CASES = [
    # (first_name, last_name, email, at_exam)
    ("Christian James", "Able", "christianjamesable@gmail.com", False),
    ("Roeb", "Dela Cruz", "delacruz.roeb.ths@gmail.com", True),
]

# Two real-inbox applicants share one open COS vacancy: one fresh, one at exam.
COS_VACANCY_TITLE = "Administrative Assistant I"
COS_CASES = [
    ("Luis Miguel", "Mina", "luismiguelmina0010@gmail.com", False),
    ("Jeric", "Salvador", "j3r1c02@gmail.com", True),
]

# A separate plantilla vacancy carried to a finalized CAR at the Appointing
# Authority, using realistic DUMMY applicants (no real email delivery).
CAR_DEMO_VACANCY_TITLE = "Administrative Aide IV"
CAR_DEMO_APPLICANTS = [
    # (first_name, last_name, email) -- fake addresses, ranked in listed order
    ("Antonio", "Ramirez", "antonio.ramirez@example.com"),
    ("Beatriz", "Santos", "beatriz.santos@example.com"),
    ("Carlos", "Villanueva", "carlos.villanueva@example.com"),
]

PHONE_PREFIXES = ("0917", "0918", "0905", "0926", "0939", "0998", "0949", "0977")


# --- believable qualification narratives, keyed by position title --------------
_DEFAULT_PROFILE = {
    "education": "a relevant bachelor's degree",
    "eligibility": "CSC Professional",
    "experience": "three years of related government service",
    "skills": "office operations, documentation, and public service",
}
TITLE_PROFILES = {
    "Administrative Aide VI": {
        "education": "completion of two years of college",
        "eligibility": "CSC Sub-Professional",
        "experience": "three years of clerical and administrative support",
        "skills": "records management, document routing, and office administration",
    },
    "Administrative Aide IV": {
        "education": "completion of two years of college",
        "eligibility": "CSC Sub-Professional",
        "experience": "two years of clerical and messengerial work",
        "skills": "document routing, supplies inventory, and basic office software",
    },
    "Administrative Assistant I": {
        "education": "completion of two years of college",
        "eligibility": "CSC Sub-Professional",
        "experience": "two years of records handling and frontline service",
        "skills": "data encoding, filing, and frontline assistance",
    },
}


class Command(BaseCommand):
    help = (
        "Wipe all applications/postings and seed a curated demo set: real-inbox "
        "plantilla and COS applicants at the fresh/exam steps, plus a separate "
        "dummy CAR finalized at the Appointing Authority."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the demo set inside a rolled-back transaction (no changes saved).",
        )
        parser.add_argument(
            "--send-email",
            action="store_true",
            help=(
                "Allow real notification email to the real-inbox applicants while "
                "seeding (off by default; the CAR-demo dummies never send email)."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Required to run when DEBUG is False (production-like). This command "
                "is destructive; never use it on a database with real applicant data."
            ),
        )

    # -- entry point ------------------------------------------------------------
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            target_db = settings.DATABASES.get("default", {}).get("NAME", "?")
            raise CommandError(
                "Refusing to run: DEBUG is False, which looks like a production-like "
                f"environment (target database: {target_db!r}). seed_demo_data is "
                "destructive -- it deletes ALL applications/postings and inserts fake "
                "applications. If you are certain this is a disposable demo/staging "
                "database and NOT one with real applicant submissions, re-run with --force."
            )
        # Real-inbox applicants respect --send-email; everything else is silent.
        email_context = (
            nullcontext()
            if options["send_email"]
            else override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
        )
        with email_context:
            if options["dry_run"]:
                with transaction.atomic():
                    result = self._run()
                    transaction.set_rollback(True)
            else:
                result = self._run()

        verb = "would be seeded" if options["dry_run"] else "seeded"
        self.stdout.write(
            self.style.SUCCESS(
                f"{result['purged_entries']} posting(s) and "
                f"{result['purged_applicants']} applicant(s) removed; "
                f"{result['created']} demo application(s) {verb}."
            )
        )
        for row in result["rows"]:
            self.stdout.write(
                "  {reference}  {title} [{branch}] -> {stage} / {section}: "
                "{name} <{email}>".format(**row)
            )

    def _run(self):
        purged_entries, purged_applicants = self._purge_all()
        creator = self._demo_creator()
        actors = {
            "secretariat": self._resolve_actor(
                RecruitmentUser.Role.SECRETARIAT, "Demo", "Secretariat"
            ),
            "hrm_chief": self._resolve_actor(
                RecruitmentUser.Role.HRM_CHIEF, "Demo", "HRM Chief"
            ),
        }
        # Workflow governance requires a quorum of HRMPSB members to finalize a
        # Plantilla interview, so the panel needs that many distinct raters.
        hrmpsb_panel = self._resolve_hrmpsb_panel(MIN_INTERVIEW_RATINGS_PLANTILLA)
        actors["hrmpsb_panel"] = hrmpsb_panel
        actors["hrmpsb"] = hrmpsb_panel[0]

        rows = []
        index = 1
        rows.extend(
            self._seed_intake_group(
                PLANTILLA_VACANCY_TITLE,
                PositionPosting.Branch.PLANTILLA,
                PLANTILLA_CASES,
                creator,
                actors,
                start_index=index,
            )
        )
        index += len(PLANTILLA_CASES)
        rows.extend(
            self._seed_intake_group(
                COS_VACANCY_TITLE,
                PositionPosting.Branch.COS,
                COS_CASES,
                creator,
                actors,
                start_index=index,
            )
        )
        index += len(COS_CASES)
        # The CAR-demo dummies must never email real mailboxes, even with --send-email.
        with override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND):
            rows.extend(self._seed_car_demo(creator, actors, start_index=index))

        return {
            "purged_entries": purged_entries,
            "purged_applicants": purged_applicants,
            "created": len(rows),
            "rows": rows,
        }

    # -- purge ------------------------------------------------------------------
    def _purge_all(self):
        """Delete every application and posting (children before parents).

        Mirrors the delete order proven by the older scoped purge, applied to all
        rows. Real staff logins and the PositionReference catalog are preserved.
        """
        User = get_user_model()

        # Entry/case-scoped rows that PROTECT applications, cases, or postings.
        FinalSelection.objects.all().delete()
        FinalDecision.objects.all().delete()
        ComparativeAssessmentReportItem.objects.all().delete()
        DeliberationRecord.objects.all().delete()
        CosVacancyDeliberation.objects.all().delete()
        # SL-02: the selection-round membership snapshot PROTECTs its cases.
        CosSelectionRound.objects.all().delete()
        ComparativeAssessmentReport.objects.all().delete()
        EvidenceVaultItem.objects.all().delete()

        purged_entries = PositionPosting.objects.count()
        # Deleting applications cascades screening/exam/interview/case/etc.;
        # deleting postings then cascades templates/weights/requirements.
        RecruitmentApplication.objects.all().delete()
        PositionPosting.objects.all().delete()

        applicants = User.objects.filter(role=RecruitmentUser.Role.APPLICANT)
        purged_applicants = applicants.count()
        applicants.delete()
        User.objects.filter(username__startswith=DEMO_STAFF_PREFIX).delete()

        return purged_entries, purged_applicants

    # -- actor / creator resolution ---------------------------------------------
    def _demo_creator(self):
        User = get_user_model()
        creator, _created = User.objects.get_or_create(
            username=DEMO_CREATOR_USERNAME,
            defaults={
                "role": RecruitmentUser.Role.SYSTEM_ADMIN,
                "first_name": "Demo",
                "last_name": "Data Loader",
                "email": "demo-data-loader@example.invalid",
                "is_active": True,
                "is_staff": False,
            },
        )
        creator.set_unusable_password()
        creator.save(update_fields=["password"])
        return creator

    def _resolve_hrmpsb_panel(self, count):
        """`count` distinct active HRMPSB members for interview rating — real
        accounts first, topped up with disposable demo members so a finalized
        Plantilla interview meets the HRMPSB quorum (workflow governance)."""
        User = get_user_model()
        members = list(
            User.objects.filter(
                role=RecruitmentUser.Role.HRMPSB_MEMBER, is_active=True
            )
            .exclude(username__startswith=DEMO_STAFF_PREFIX)
            .order_by("id")[:count]
        )
        seq = 0
        while len(members) < count:
            seq += 1
            username = f"{DEMO_STAFF_PREFIX}hrmpsb_panel_{seq}"
            member, _created = User.objects.get_or_create(
                username=username,
                defaults={
                    "role": RecruitmentUser.Role.HRMPSB_MEMBER,
                    "first_name": "Demo",
                    "last_name": f"HRMPSB Member {seq}",
                    "email": f"{username}@example.invalid",
                    "is_active": True,
                    "is_staff": False,
                },
            )
            members.append(member)
        return members

    def _resolve_actor(self, role, first_name, last_name):
        """Prefer a real active staff account for the role; else a disposable actor."""
        User = get_user_model()
        real = (
            User.objects.filter(role=role, is_active=True)
            .exclude(username__startswith=DEMO_STAFF_PREFIX)
            .order_by("id")
            .first()
        )
        if real:
            return real
        username = f"{DEMO_STAFF_PREFIX}{role}"
        actor, _created = User.objects.get_or_create(
            username=username,
            defaults={
                "role": role,
                "first_name": first_name,
                "last_name": last_name,
                "email": f"{username}@example.invalid",
                "is_active": True,
                "is_staff": False,
            },
        )
        actor.role = role
        actor.is_active = True
        actor.set_unusable_password()
        actor.save(update_fields=["role", "is_active", "password"])
        return actor

    # -- vacancy / applicant / application builders -----------------------------
    def _resolve_reference(self, preferred_title, *, require_level=None):
        level_to_class = {
            1: PositionReference.LevelClassification.FIRST_LEVEL,
            2: PositionReference.LevelClassification.SECOND_LEVEL,
        }
        # routing_level is a computed property (not a DB field), so filter on the
        # underlying level_classification and let the property map it back to 1/2.
        base = PositionReference.objects.filter(
            is_active=True,
            level_classification__in=list(level_to_class.values()),
        )
        reference = base.filter(position_title=preferred_title).first()
        if reference and (require_level is None or reference.routing_level == require_level):
            return reference
        fallback = base
        if require_level is not None:
            fallback = fallback.filter(level_classification=level_to_class[require_level])
        reference = fallback.order_by("position_title", "id").first()
        if not reference:
            raise CommandError(
                f"No active PositionReference available for '{preferred_title}'. Run "
                "migrations so the starter position references are loaded before seeding."
            )
        return reference

    def _create_entry(self, *, title, branch, creator, require_level=None):
        reference = self._resolve_reference(title, require_level=require_level)
        is_plantilla = branch == PositionPosting.Branch.PLANTILLA
        opening_date = timezone.localdate()
        entry = PositionPosting(
            position_reference=reference,
            branch=branch,
            level=reference.routing_level,
            item_number="",
            intake_mode=(
                PositionPosting.IntakeMode.FIXED_PERIOD
                if is_plantilla
                else PositionPosting.IntakeMode.POOLING
            ),
            status=PositionPosting.EntryStatus.ACTIVE,
            opening_date=opening_date,
            closing_date=(
                PositionPosting.calculate_plantilla_closing_date(opening_date)
                if is_plantilla
                else None
            ),
            description=(
                f"{reference.position_title} vacancy at DOH-CHD CALABARZON. "
                "Interested and qualified applicants are encouraged to apply."
            ),
            requirements=(
                "Submit a fully accomplished Personal Data Sheet (PDS) with recent "
                "photo, authenticated eligibility, Transcript of Records, and relevant "
                "training certificates."
            ),
            created_by=creator,
            updated_by=creator,
        )
        entry.full_clean()
        entry.save()
        create_default_position_document_requirements(entry)
        return entry

    def _create_applicant(self, index, first_name, last_name, email):
        User = get_user_model()
        username = f"{DEMO_APPLICANT_PREFIX}{index:02d}"
        applicant, _created = User.objects.get_or_create(
            username=username,
            defaults={
                "role": RecruitmentUser.Role.APPLICANT,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "is_active": True,
            },
        )
        applicant.role = RecruitmentUser.Role.APPLICANT
        applicant.first_name = first_name
        applicant.last_name = last_name
        applicant.email = email
        applicant.is_active = True
        applicant.set_unusable_password()
        applicant.save(
            update_fields=["role", "first_name", "last_name", "email", "is_active", "password"]
        )
        return applicant

    def _create_application(self, entry, applicant, first_name, last_name):
        is_cos = entry.branch == PositionPosting.Branch.COS
        performance_rating_applicability = (
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            if is_cos
            else RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )
        full_name = f"{first_name} {last_name}"
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=entry,
            applicant_first_name=first_name,
            applicant_last_name=last_name,
            applicant_email=applicant.email,
            applicant_phone=self._applicant_phone(applicant.pk),
            performance_rating_applicability=performance_rating_applicability,
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary=self._qualification_summary(entry.title),
            cover_letter=self._cover_letter(entry.title, full_name),
        )
        self._upload_required_documents(
            application,
            applicant,
            performance_rating_applicability=performance_rating_applicability,
        )
        now = timezone.now()
        application.otp_hash = f"demo-seed-{application.pk}"
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
        return application

    def _upload_required_documents(
        self, application, applicant, *, performance_rating_applicability
    ):
        performance_rating_not_applicable = (
            performance_rating_applicability
            == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
        )
        for requirement in get_required_applicant_document_requirements(
            application,
            performance_rating_not_applicable=performance_rating_not_applicable,
        ):
            upload_evidence_item(
                application=application,
                actor=applicant,
                label=requirement.title,
                uploaded_file=self._document_upload(requirement.code, application.pk),
                document_key=requirement.code,
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type="applicant_document",
            )

    def _document_upload(self, requirement_code, application_pk):
        raw_bytes = (
            b"%PDF-1.4\n"
            + f"demo-seed application {application_pk} :: {requirement_code}\n".encode("utf-8")
            + b"%%EOF\n"
        )
        return SimpleUploadedFile(
            f"{requirement_code}.pdf",
            raw_bytes,
            content_type="application/pdf",
        )

    # -- case builders ----------------------------------------------------------
    def _seed_intake_group(self, title, branch, cases, creator, actors, *, start_index):
        """One open vacancy with real-inbox applicants at the fresh and/or exam step."""
        entry = self._create_entry(
            title=title, branch=branch, creator=creator, require_level=1
        )
        rows = []
        for offset, (first_name, last_name, email, at_exam) in enumerate(cases):
            applicant = self._create_applicant(
                start_index + offset, first_name, last_name, email
            )
            application = self._create_application(entry, applicant, first_name, last_name)
            if at_exam:
                self._advance_to_exam(application, actors["secretariat"])
            application.refresh_from_db()
            application.case.refresh_from_db()
            rows.append(self._summary_row(application))
        return rows

    def _seed_car_demo(self, creator, actors, *, start_index):
        """A separate plantilla vacancy taken to a finalized CAR at the AA (dummy data)."""
        entry = self._create_entry(
            title=CAR_DEMO_VACANCY_TITLE,
            branch=PositionPosting.Branch.PLANTILLA,
            creator=creator,
            require_level=1,
        )
        applications = []
        for offset, (first_name, last_name, email) in enumerate(CAR_DEMO_APPLICANTS):
            applicant = self._create_applicant(
                start_index + offset, first_name, last_name, email
            )
            application = self._create_application(entry, applicant, first_name, last_name)
            score = f"{95 - offset * 4}.00"
            self._advance_to_hrmpsb(application, actors["secretariat"], score)
            self._finalize_interview(
                application,
                session_actor=actors["secretariat"],
                rating_actors=actors["hrmpsb_panel"],
                score=score,
            )
            applications.append(application)

        self._close_entry_pool(entry)
        for offset, application in enumerate(applications):
            application.refresh_from_db()
            application.case.refresh_from_db()
            application.position.refresh_from_db()
            # SL-03: a CAR cannot finalize while a candidate's ETE is blank, so the demo
            # data carries the Secretariat's rating the way a real prepared CAR would.
            set_application_ete_rating(
                application,
                actors["secretariat"],
                Decimal(f"{92 - offset * 3}.00"),
            )

        # Draft then finalize the CAR; recommend the top-ranked (first) applicant.
        generate_comparative_assessment_report(
            application=applications[0],
            actor=actors["secretariat"],
            cleaned_data={"summary_notes": "Demo CAR draft for panel presentation."},
            finalize=False,
        )
        generate_comparative_assessment_report(
            application=applications[0],
            actor=actors["secretariat"],
            cleaned_data={
                "summary_notes": "Demo finalized CAR forwarded to the Appointing Authority.",
                "recommended_case_id": applications[0].case.id,
                "recommendation_notes": "HRMPSB recommends the top-ranked applicant.",
                "members_present": MIN_INTERVIEW_RATINGS_PLANTILLA,
                "deliberated_on": timezone.localdate(),
            },
            finalize=True,
        )
        rows = []
        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
            rows.append(self._summary_row(application))
        return rows

    # -- workflow advancement (mirrors the proven end-to-end service calls) ------
    def _advance_to_exam(self, application, actor):
        """Finalize screening (qualified) so the case sits at the exam step."""
        self._finalize_screening(application, actor)
        application.refresh_from_db()
        application.case.refresh_from_db()

    def _advance_to_hrmpsb(self, application, actor, score):
        self._finalize_screening(application, actor, score=score)
        self._finalize_exam(application, actor, score=score)
        application.refresh_from_db()
        application.case.refresh_from_db()
        if application.case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW:
            raise CommandError(
                f"{application.reference_label} did not advance to HRMPSB review "
                f"(stage: {application.case.current_stage})."
            )

    def _finalize_screening(self, application, actor, score="91.00"):
        return save_screening_review(
            application=application,
            actor=actor,
            cleaned_data={
                "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
                "completeness_notes": "All required demo documents were reviewed.",
                "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
                "education_score": score,
                "training_score": score,
                "experience_score": score,
                "document_review_score": "",
                "screening_notes": "Demo screening finalized as qualified.",
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
                "exam_notes": "Demo examination record finalized.",
            },
            finalize=True,
        )

    def _ensure_published_rating_sheet(self, application, actor):
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

    def _finalize_interview(self, application, *, session_actor, rating_actors, score):
        scheduled_for = timezone.now() + timedelta(days=2)
        save_interview_session(
            application=application,
            actor=session_actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": "Demo Interview Room",
                "session_notes": "Demo interview session scheduled.",
            },
            finalize=False,
        )
        template = self._ensure_published_rating_sheet(application, session_actor)
        level = self._competency_level(score, template.scale_max)
        competency_scores = {
            competency: level for competency in template.competencies.all()
        }
        # Each HRMPSB panel member records their own rating so the interview
        # meets the finalize quorum.
        for rating_actor in rating_actors:
            save_interview_rating(
                application=application,
                actor=rating_actor,
                cleaned_data={
                    "competency_scores": competency_scores,
                    "rating_notes": "Demo interview rating recorded.",
                    "justification": "Consistent competency performance across the panel sheet.",
                },
            )
        return save_interview_session(
            application=application,
            actor=session_actor,
            cleaned_data={
                "scheduled_for": scheduled_for,
                "location": "Demo Interview Room",
                "session_notes": "Demo interview session finalized.",
            },
            finalize=True,
        )

    @staticmethod
    def _competency_level(score, scale_max):
        """Accept a competency level (1..scale_max) or a 0-100 score; clamp to range."""
        try:
            value = float(score)
        except (TypeError, ValueError):
            value = scale_max
        if value > scale_max:
            value = value / 100 * scale_max
        return max(1, min(scale_max, int(round(value))))

    def _close_entry_pool(self, entry):
        entry.status = PositionPosting.EntryStatus.CLOSED
        entry.save(update_fields=["status", "is_active", "updated_at"])
        entry.refresh_from_db()

    # -- realistic field builders ----------------------------------------------
    def _applicant_phone(self, seed):
        prefix = PHONE_PREFIXES[seed % len(PHONE_PREFIXES)]
        suffix = 1000000 + (seed * 813457) % 8999999
        return f"{prefix}{suffix:07d}"

    def _profile(self, title):
        return TITLE_PROFILES.get(title, _DEFAULT_PROFILE)

    def _qualification_summary(self, title):
        profile = self._profile(title)
        experience = profile["experience"]
        experience = experience[:1].upper() + experience[1:]
        return (
            f"Holder of {profile['education']}; {profile['eligibility']} eligible. "
            f"{experience}. Skilled in {profile['skills']}."
        )

    def _cover_letter(self, title, full_name):
        profile = self._profile(title)
        return (
            "Dear Sir/Madam,\n\n"
            f"I respectfully submit my application for the position of {title} at the "
            "Department of Health - Center for Health Development CALABARZON. With "
            f"{profile['experience']} and a background in {profile['skills']}, I am "
            "confident that I can contribute meaningfully to your office. I hold "
            f"{profile['education']} and am {profile['eligibility']} eligible.\n\n"
            "Thank you for considering my application. I look forward to the "
            "opportunity to be of service.\n\n"
            f"Respectfully,\n{full_name}"
        )

    def _summary_row(self, application):
        case = getattr(application, "case", None)
        stage = (
            RecruitmentCase.Stage(case.current_stage).label
            if case and case.current_stage
            else "(submitted)"
        )
        return {
            "reference": application.reference_label,
            "title": application.position.title,
            "branch": application.position.get_branch_display(),
            "stage": stage,
            "section": get_current_workflow_section(application) or "-",
            "name": application.applicant_display_name,
            "email": application.applicant_email,
        }
