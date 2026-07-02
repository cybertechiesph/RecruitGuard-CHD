"""Seed realistic-looking demo recruitment applications.

This command wipes synthetic test data (the legacy ``seed_e2e_test_cases``
``E2E seed:`` fixtures and any prior run of this command) and replaces it with a
moderate set of believable, brand-new applications that sit at the *start* of the
pipeline -- freshly submitted and awaiting the first review. Each application is
built by driving the real ``submit_application`` service, so every case lands in a
genuinely valid state (Level 1 -> Secretariat queue, Level 2 -> HRM Chief queue).

Nothing here is advanced past submission: the demo operator walks the rest of the
workflow live. Re-running the command is safe -- it purges its own data first.

Demo data is identified for purge by two invisible markers:
  * recruitment entries created by the ``demo_seed_bot`` account
  * applicant users whose username starts with ``demo_applicant_``
Real ``PositionReference`` rows (loaded by migration 0016) and real staff accounts
are never touched.
"""

from contextlib import nullcontext
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from recruitment.models import (
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CosVacancyDeliberation,
    DeliberationRecord,
    EvidenceVaultItem,
    FinalDecision,
    FinalSelection,
    PositionPosting,
    PositionReference,
    RecruitmentApplication,
    RecruitmentUser,
)
from recruitment.requirements import get_required_applicant_document_requirements
from recruitment.services import (
    create_default_position_document_requirements,
    submit_application,
    upload_evidence_item,
)


# --- purge markers -------------------------------------------------------------
DEMO_CREATOR_USERNAME = "demo_seed_bot"
DEMO_APPLICANT_PREFIX = "demo_applicant_"
# Legacy synthetic markers left behind by seed_e2e_test_cases.
LEGACY_E2E_DESCRIPTION_PREFIX = "E2E seed:"
LEGACY_E2E_USER_PREFIX = "e2e_seed_applicant_"
LEGACY_E2E_REFERENCE_PREFIXES = ("RG-COS-test-", "RG-PLT-test-")


# --- realistic applicant pool (distinct names; one application each) -----------
APPLICANT_NAMES = [
    ("Maria Clara", "Santos"),
    ("Juan", "Dela Cruz"),
    ("Jose", "Reyes"),
    ("Andrea", "Mendoza"),
    ("Mark Anthony", "Garcia"),
    ("Angelica", "Bautista"),
    ("Ramon", "Villanueva"),
    ("Christine Joy", "Aquino"),
    ("Rodel", "Castillo"),
    ("Liza", "Fernandez"),
    ("Emmanuel", "Torres"),
    ("Grace", "Ramos"),
    ("Noel", "Domingo"),
    ("Katrina", "Salazar"),
    ("Joseph", "Pascual"),
    ("Aileen", "Navarro"),
    ("Dennis", "Aguilar"),
    ("Michelle", "Cruz"),
    ("Roberto", "Gonzales"),
    ("Jennifer", "Lim"),
    ("Carlo", "Magno"),
    ("Patricia", "Flores"),
    ("Allan", "Rivera"),
    ("Rowena", "Mercado"),
    ("Edgardo", "Valdez"),
    ("Sheila Marie", "Ocampo"),
    ("Benjamin", "Soriano"),
    ("Cristina", "Galang"),
    ("Arnel", "Padilla"),
    ("Maricel", "Tolentino"),
]

PHONE_PREFIXES = ("0917", "0918", "0905", "0926", "0939", "0998", "0949", "0977")


# --- vacancies to post (title must exist as an active PositionReference) --------
# branch picks the engagement type; level is taken from the reference itself.
DEMO_VACANCIES = [
    {"title": "Accountant II", "branch": PositionPosting.Branch.PLANTILLA, "count": 5},
    {"title": "Administrative Officer III", "branch": PositionPosting.Branch.PLANTILLA, "count": 6},
    {"title": "Nurse III", "branch": PositionPosting.Branch.PLANTILLA, "count": 5},
    {"title": "Administrative Aide IV", "branch": PositionPosting.Branch.PLANTILLA, "count": 4},
    {"title": "Administrative Assistant I", "branch": PositionPosting.Branch.COS, "count": 4},
    {"title": "Security Guard I", "branch": PositionPosting.Branch.COS, "count": 3},
]


# --- believable qualification narratives, keyed by position title --------------
_DEFAULT_PROFILE = {
    "education": "a relevant bachelor's degree",
    "eligibility": "CSC Professional",
    "experience": "three years of related government service",
    "skills": "office operations, documentation, and public service",
}
TITLE_PROFILES = {
    "Accountant II": {
        "education": "a Bachelor's degree in Accountancy",
        "eligibility": "CPA (RA 1080)",
        "experience": "four years of general accounting and government auditing",
        "skills": "eNGAS, financial statement preparation, and budget monitoring",
    },
    "Administrative Officer III": {
        "education": "a Bachelor's degree in Public Administration",
        "eligibility": "CSC Professional",
        "experience": "three years of human resource and records management",
        "skills": "201-file management, recruitment support, and CSC/DBM issuances",
    },
    "Nurse III": {
        "education": "a Bachelor of Science in Nursing",
        "eligibility": "PRC Registered Nurse (RA 1080)",
        "experience": "five years of public health and hospital nursing",
        "skills": "community health programs, immunization, and patient care",
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
    "Security Guard I": {
        "education": "high school graduation with security guard training",
        "eligibility": "License to Exercise Security Profession",
        "experience": "three years of premises and access security",
        "skills": "access control, incident reporting, and visitor management",
    },
}


class Command(BaseCommand):
    help = (
        "Remove synthetic test data and seed realistic-looking, freshly submitted "
        "demo applications across a moderate set of real positions."
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
            help="Allow real notification email delivery while seeding (off by default).",
        )
        parser.add_argument(
            "--keep-synthetic",
            action="store_true",
            help="Do not purge legacy E2E / prior demo data before seeding.",
        )

    # -- entry point ------------------------------------------------------------
    def handle(self, *args, **options):
        email_context = (
            nullcontext()
            if options["send_email"]
            else override_settings(
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
            )
        )
        with email_context:
            if options["dry_run"]:
                with transaction.atomic():
                    result = self._run(options)
                    transaction.set_rollback(True)
            else:
                result = self._run(options)

        verb = "would be seeded" if options["dry_run"] else "seeded"
        self.stdout.write(
            self.style.SUCCESS(
                f"{result['purged_entries']} synthetic entr(y/ies) and "
                f"{result['purged_applicants']} synthetic applicant(s) removed; "
                f"{result['created']} realistic application(s) {verb}."
            )
        )
        for row in result["rows"]:
            self.stdout.write(
                "  {reference}  {title} [{branch}/L{level}] -> {queue}: "
                "{name}".format(**row)
            )

    def _run(self, options):
        purged_entries = purged_applicants = 0
        if not options["keep_synthetic"]:
            purged_entries, purged_applicants = self._purge()

        creator = self._demo_creator()
        rows = []
        applicant_index = 0
        for vacancy in DEMO_VACANCIES:
            entry = self._create_entry(vacancy, creator)
            for _ in range(vacancy["count"]):
                if applicant_index >= len(APPLICANT_NAMES):
                    raise CommandError(
                        "Not enough applicant names in the pool for the requested "
                        f"vacancy counts (need more than {len(APPLICANT_NAMES)})."
                    )
                first_name, last_name = APPLICANT_NAMES[applicant_index]
                applicant = self._create_applicant(applicant_index, first_name, last_name)
                application = self._create_application(entry, applicant, first_name, last_name)
                rows.append(self._summary_row(application))
                applicant_index += 1

        return {
            "purged_entries": purged_entries,
            "purged_applicants": purged_applicants,
            "created": len(rows),
            "rows": rows,
        }

    # -- purge ------------------------------------------------------------------
    def _purge(self):
        """Delete legacy E2E fixtures and any prior demo run, children before parents."""
        User = get_user_model()

        # Identify synthetic recruitment entries: legacy E2E + this command's own.
        legacy_entries = PositionPosting.objects.filter(
            description__startswith=LEGACY_E2E_DESCRIPTION_PREFIX
        )
        demo_entries = PositionPosting.objects.filter(
            created_by__username=DEMO_CREATOR_USERNAME
        )

        # Identify synthetic applications (covers entries we can't tag directly).
        applications = RecruitmentApplication.objects.filter(
            applicant__username__startswith=DEMO_APPLICANT_PREFIX
        ) | RecruitmentApplication.objects.filter(
            applicant__username__startswith=LEGACY_E2E_USER_PREFIX
        )
        for reference_prefix in LEGACY_E2E_REFERENCE_PREFIXES:
            applications = applications | RecruitmentApplication.objects.filter(
                reference_number__startswith=reference_prefix
            )

        entries = legacy_entries | demo_entries | PositionPosting.objects.filter(
            applications__in=applications
        )
        applications = applications | RecruitmentApplication.objects.filter(
            position__in=entries
        )

        # Materialise PKs so .delete() is not run on distinct/joined querysets.
        entry_ids = list(entries.values_list("pk", flat=True))
        application_ids = list(applications.values_list("pk", flat=True))
        entries = PositionPosting.objects.filter(pk__in=entry_ids)
        applications = RecruitmentApplication.objects.filter(pk__in=application_ids)

        # Entry-scoped rows that PROTECT cases/applications must go first.
        FinalSelection.objects.filter(recruitment_entry__in=entries).delete()
        FinalDecision.objects.filter(recruitment_entry__in=entries).delete()
        ComparativeAssessmentReportItem.objects.filter(
            report__recruitment_entry__in=entries
        ).delete()
        DeliberationRecord.objects.filter(recruitment_entry__in=entries).delete()
        CosVacancyDeliberation.objects.filter(recruitment_entry__in=entries).delete()
        ComparativeAssessmentReport.objects.filter(recruitment_entry__in=entries).delete()
        EvidenceVaultItem.objects.filter(recruitment_entry__in=entries).delete()

        purged_entries = entries.count()
        # Deleting applications cascades screening/exam/interview/case/etc.
        applications.delete()
        entries.delete()

        purged_applicants = User.objects.filter(
            username__startswith=DEMO_APPLICANT_PREFIX
        ).count()
        purged_applicants += User.objects.filter(
            username__startswith=LEGACY_E2E_USER_PREFIX
        ).count()
        User.objects.filter(username__startswith=DEMO_APPLICANT_PREFIX).delete()
        User.objects.filter(username__startswith=LEGACY_E2E_USER_PREFIX).delete()

        return purged_entries, purged_applicants

    # -- creation helpers -------------------------------------------------------
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

    def _position_reference(self, title):
        reference = PositionReference.objects.filter(
            position_title=title, is_active=True
        ).first()
        if not reference:
            raise CommandError(
                f"Missing active PositionReference '{title}'. Run migrations so the "
                "starter position references are loaded before seeding demo data."
            )
        if reference.routing_level is None:
            raise CommandError(
                f"PositionReference '{title}' has no level classification / routing level."
            )
        return reference

    def _create_entry(self, vacancy, creator):
        reference = self._position_reference(vacancy["title"])
        branch = vacancy["branch"]
        is_plantilla = branch == PositionPosting.Branch.PLANTILLA
        opening_date = timezone.localdate()
        entry = PositionPosting(
            position_reference=reference,
            branch=branch,
            level=reference.routing_level,
            item_number=(
                f"OSEC-DOHB-{reference.class_id}-{opening_date.year}-{vacancy['count']:03d}"
                if is_plantilla
                else ""
            ),
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

    def _create_applicant(self, index, first_name, last_name):
        User = get_user_model()
        username = f"{DEMO_APPLICANT_PREFIX}{index + 1:02d}"
        email = self._applicant_email(first_name, last_name, index)
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

    # -- realistic field builders ----------------------------------------------
    def _applicant_email(self, first_name, last_name, index):
        local = (
            f"{first_name}.{last_name}".lower().replace(" ", "").replace("'", "")
        )
        return f"{local}{index + 1:02d}@example.com"

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
        queue = (
            RecruitmentUser.Role(application.current_handler_role).label
            if application.current_handler_role
            else "(unrouted)"
        )
        return {
            "reference": application.reference_label,
            "title": application.position.title,
            "branch": application.position.get_branch_display(),
            "level": application.level,
            "queue": queue,
            "name": application.applicant_display_name,
            "stage": getattr(case, "current_stage", ""),
        }
