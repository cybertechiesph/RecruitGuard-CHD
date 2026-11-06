"""Seed the exhibit demo: an office in the middle of hiring, with work waiting for every role.

Everything is synthetic. Applicant addresses use plus-addressing on one inbox the team owns, so
no notification can ever reach a stranger, and item numbers are marked as samples.

The story is built by driving the real services with the clock moved back, so screening, exam
and interview dates, case history and audit entries all carry believable dates instead of today.

Destructive like ``seed_demo_data``: it deletes all applications and vacancies, and refuses to
run when DEBUG is False unless ``--force`` is passed. Never point it at real applicant data.
"""

import io
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from recruitment.management.commands import seed_demo_data
from recruitment.models import PositionPosting, RecruitmentApplication, RecruitmentUser
from recruitment.services import (
    MIN_INTERVIEW_RATINGS_PLANTILLA,
    generate_comparative_assessment_report,
    get_office_policy,
    save_exam_schedule,
    save_interview_rating,
    save_interview_session,
    set_application_ete_rating,
)

INBOX = "cybertechiesph"
PLANTILLA = PositionPosting.Branch.PLANTILLA
COS = PositionPosting.Branch.COS
UNIT = "Management Support Division"

STAFF_NAMES = {
    RecruitmentUser.Role.SECRETARIAT: ("Liza", "Marasigan"),
    RecruitmentUser.Role.HRM_CHIEF: ("Rowena", "Castillo"),
    RecruitmentUser.Role.HRMPSB_MEMBER: ("Ramon", "Bautista"),
    RecruitmentUser.Role.APPOINTING_AUTHORITY: ("Eduardo", "Manalo"),
    RecruitmentUser.Role.SYSTEM_ADMIN: ("Paolo", "Rivera"),
}
PANEL_NAMES = [("Gloria", "Evangelista"), ("Noel", "Dimaano"), ("Cecilia", "Mercado")]

# (title, branch, level, sample item number, first day the vacancy was open, in days ago)
OPEN_PLANTILLA = ("Administrative Assistant II", PLANTILLA, 1, "SAMPLE-AA2-001", 6)
OPEN_COS = ("Administrative Assistant I", COS, 1, "", 12)
AT_SCREENING = ("Administrative Aide VI", PLANTILLA, 1, "SAMPLE-ADA6-002", 24)
AT_EXAM = ("Administrative Assistant III", PLANTILLA, 1, "SAMPLE-AA3-001", 30)
AT_INTERVIEW = ("Administrative Aide IV", PLANTILLA, 1, "SAMPLE-ADA4-003", 42)
AT_CHIEF = ("Nurse III", PLANTILLA, 2, "SAMPLE-N3-004", 26)
AT_DECISION = ("Laboratory Aide II", PLANTILLA, 1, "SAMPLE-LA2-001", 48)

PEOPLE = {
    OPEN_PLANTILLA: [("Kristine Joy", "Alcantara"), ("Mark Anthony", "Villareal")],
    OPEN_COS: [("Jessa Mae", "Pangilinan"), ("Ronald", "Magsino")],
    AT_SCREENING: [("Angelica", "Dimaunahan"), ("John Paul", "Hernandez"), ("Rhea", "Sarmiento")],
    AT_EXAM: [("Michelle", "Javier"), ("Dennis", "Ilagan")],
    AT_INTERVIEW: [("Maricel", "Aguilar"), ("Jerome", "Malabanan"), ("Cherry Ann", "Lontok")],
    AT_CHIEF: [("Patricia Anne", "Laurel"), ("Emmanuel", "Cabrera"), ("Sheila Marie", "Katigbak")],
    AT_DECISION: [("Roselle", "Macaraig"), ("Arnel", "Tolentino"), ("Lovely", "Barrion")],
}
PROFILES = {
    "Administrative Assistant II": ("a bachelor's degree in office administration", "CSC Professional",
                                    "two years of administrative support", "records, reports and office coordination"),
    "Administrative Assistant III": ("a bachelor's degree in public administration", "CSC Professional",
                                     "three years of administrative work", "correspondence, procurement papers and reports"),
    "Nurse III": ("a Bachelor of Science in Nursing", "RA 1080 (Nurse)",
                  "four years of public health nursing", "program coordination and health service delivery"),
    "Laboratory Aide II": ("two years of college in a health-related course", "CSC Sub-Professional",
                           "one year of laboratory support", "specimen handling and laboratory records"),
}
RATING_NOTES = [
    "Answered clearly and gave concrete examples from past work.",
    "Good understanding of the role; could say more about teamwork.",
    "Calm and organized; showed a strong sense of public service.",
]


def manila(day_offset, hour=9, minute=0):
    """An aware date-time `day_offset` days from today, at this hour in the office's time zone."""
    day = timezone.localdate() + timedelta(days=day_offset)
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def next_working_day(after_days):
    when = manila(after_days)
    while when.weekday() >= 5:
        when += timedelta(days=1)
    return when


@contextmanager
def clock_at(when):
    with patch("django.utils.timezone.now", return_value=when):
        yield


class Command(seed_demo_data.Command):
    help = "Wipe applications and vacancies and load the exhibit demo: work waiting for every role."

    # -- the story --------------------------------------------------------------
    def _run(self):
        purged_entries, purged_applicants = self._purge_all()
        self.title_profiles = {**seed_demo_data.TITLE_PROFILES, **self._profiles()}
        self.applicant_index = 0
        self.office = self._resolve_actor(RecruitmentUser.Role.SECRETARIAT, "Liza", "Marasigan")
        self.chief = self._resolve_actor(RecruitmentUser.Role.HRM_CHIEF, "Rowena", "Castillo")
        members = self._resolve_hrmpsb_panel(MIN_INTERVIEW_RATINGS_PLANTILLA + 1)
        self.panel = members[:MIN_INTERVIEW_RATINGS_PLANTILLA]
        self.end_user = members[MIN_INTERVIEW_RATINGS_PLANTILLA]
        get_office_policy().core_panel_members.set(self.panel)
        self._name_staff(members)

        applications = []
        for build in (
            self._open_plantilla,
            self._open_cos,
            self._at_screening,
            self._at_exam,
            self._at_interview,
            self._at_chief,
            self._at_decision,
        ):
            applications.extend(build())

        rows = []
        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
            rows.append(self._summary_row(application))
        return {
            "purged_entries": purged_entries,
            "purged_applicants": purged_applicants,
            "created": len(rows),
            "rows": rows,
        }

    def _open_plantilla(self):
        entry, applications = self._vacancy_with_applicants(OPEN_PLANTILLA)
        return applications

    def _open_cos(self):
        entry, applications = self._vacancy_with_applicants(OPEN_COS)
        return applications

    def _at_screening(self):
        """Applications closed last week; the Secretariat has the batch to screen."""
        entry, applications = self._vacancy_with_applicants(AT_SCREENING)
        self._close(entry, days_ago=4)
        return applications

    def _at_exam(self):
        """Screened, with the exam coming up on a working day."""
        entry, applications = self._vacancy_with_applicants(AT_EXAM)
        self._close(entry, days_ago=10)
        with clock_at(manila(-7, 10)):
            for application in applications:
                self._finalize_screening(application, self.office)
        exam_day = next_working_day(3)
        with clock_at(manila(-2, 14)):
            for application in applications:
                application.refresh_from_db()
                save_exam_schedule(
                    application=application,
                    actor=self.office,
                    cleaned_data={
                        "scheduled_for": exam_day,
                        "venue": "CHD CALABARZON Training Room, 2nd Floor",
                        "instructions": "Bring a valid government ID and a black pen.",
                    },
                )
        return applications

    def _at_interview(self):
        """The interview was held yesterday; the rest of the panel has rated, one member has not."""
        entry, applications = self._vacancy_with_applicants(AT_INTERVIEW)
        self._screen_and_examine(entry, applications, close=-24, screened=-20, examined=-13)
        held_at = manila(-1, 9)
        with clock_at(manila(-6, 11)):
            template = self._ensure_published_rating_sheet(applications[0], self.office)
            for application in applications:
                application.refresh_from_db()
                save_interview_session(
                    application=application,
                    actor=self.office,
                    cleaned_data={
                        "scheduled_for": held_at,
                        "location": "Conference Room A, 3rd Floor",
                        "session_notes": "Panel interview for the Administrative Aide IV batch.",
                    },
                    finalize=False,
                )
        raters = [*self.panel[1:], self.end_user]
        with clock_at(manila(-1, 15)):
            for offset, application in enumerate(applications):
                for turn, rater in enumerate(raters):
                    level = self._competency_level(92 - offset * 6 - turn * 2, template.scale_max)
                    save_interview_rating(
                        application=application,
                        actor=rater,
                        cleaned_data={
                            "competency_scores": {
                                competency: level for competency in template.competencies.all()
                            },
                            "rating_notes": RATING_NOTES[(offset + turn) % len(RATING_NOTES)],
                            "justification": RATING_NOTES[(offset + turn + 1) % len(RATING_NOTES)],
                        },
                    )
        return applications

    def _at_chief(self):
        """A Level 2 vacancy: the HRM Chief has screened one and has two left."""
        entry, applications = self._vacancy_with_applicants(AT_CHIEF)
        self._close(entry, days_ago=6)
        with clock_at(manila(-2, 10)):
            self._finalize_screening(applications[0], self.chief, score="93.00")
        return applications

    def _at_decision(self):
        """A finished CAR, ranked and recommended, waiting for the Appointing Authority."""
        entry, applications = self._vacancy_with_applicants(AT_DECISION)
        self._screen_and_examine(entry, applications, close=-30, screened=-26, examined=-19)
        scores = ("94.00", "90.00", "86.00")
        with clock_at(manila(-11, 15)):
            for application, score in zip(applications, scores):
                application.refresh_from_db()
                self._finalize_interview(
                    application, session_actor=self.office, rating_actors=self.panel, score=score
                )
        with clock_at(manila(-5, 10)):
            for offset, application in enumerate(applications):
                application.refresh_from_db()
                application.case.refresh_from_db()
                application.position.refresh_from_db()
                set_application_ete_rating(application, self.office, Decimal(f"{91 - offset * 3}.00"))
            top = applications[0]
            generate_comparative_assessment_report(
                application=top,
                actor=self.office,
                cleaned_data={"summary_notes": "Draft for the panel's deliberation."},
                finalize=False,
            )
        with clock_at(manila(-3, 14)):
            generate_comparative_assessment_report(
                application=top,
                actor=self.office,
                cleaned_data={
                    "summary_notes": "Deliberated by the panel with quorum present.",
                    "recommended_case_id": top.case.id,
                    "recommendation_notes": "The panel recommends the top-ranked applicant.",
                    "members_present": MIN_INTERVIEW_RATINGS_PLANTILLA,
                    "deliberated_on": timezone.localdate(),
                },
                finalize=True,
            )
        return applications

    # -- shared steps -----------------------------------------------------------
    def _vacancy_with_applicants(self, vacancy):
        title, branch, level, item_number, opened_days_ago = vacancy
        with clock_at(manila(-opened_days_ago, 8, 30)):
            entry = self._create_entry(title=title, branch=branch, creator=self.office, require_level=level)
            PositionPosting.objects.filter(pk=entry.pk).update(item_number=item_number, unit=UNIT)
            entry.refresh_from_db()
            if branch == PLANTILLA:
                entry.panel_end_users.set([self.end_user])
        applications = []
        for offset, (first_name, last_name) in enumerate(PEOPLE[vacancy]):
            self.applicant_index += 1
            applied_on = max(opened_days_ago - 2 - offset * 2, 0)
            with clock_at(manila(-applied_on, 10 + offset, 15 * offset)):
                email = f"{INBOX}+{first_name[0]}{last_name}@gmail.com".lower().replace(" ", "")
                applicant = self._create_applicant(self.applicant_index, first_name, last_name, email)
                applications.append(self._create_application(entry, applicant, first_name, last_name))
        return entry, applications

    def _close(self, entry, days_ago):
        with clock_at(manila(-days_ago, 17)):
            self._close_entry_pool(entry)

    def _screen_and_examine(self, entry, applications, *, close, screened, examined):
        self._close(entry, days_ago=-close)
        scores = ("93.00", "89.00", "85.00")
        with clock_at(manila(screened, 10)):
            for application, score in zip(applications, scores):
                application.refresh_from_db()
                self._finalize_screening(application, self.office, score=score)
        with clock_at(manila(examined, 15)):
            for application, score in zip(applications, scores):
                application.refresh_from_db()
                self._finalize_exam(application, self.office, score=score)

    def _name_staff(self, members):
        for user in RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()):
            if user.role in STAFF_NAMES:
                user.first_name, user.last_name = STAFF_NAMES[user.role]
                user.save(update_fields=["first_name", "last_name"])
        for user, (first_name, last_name) in zip(members[1:], PANEL_NAMES):
            user.first_name, user.last_name = first_name, last_name
            user.save(update_fields=["first_name", "last_name"])

    @staticmethod
    def _profiles():
        return {
            title: {"education": education, "eligibility": eligibility, "experience": experience, "skills": skills}
            for title, (education, eligibility, experience, skills) in PROFILES.items()
        }

    def _profile(self, title):
        return self.title_profiles.get(title, seed_demo_data._DEFAULT_PROFILE)

    def _document_upload(self, requirement_code, application_pk):
        """A real one-page PDF, so opening a document during the demo shows something sensible."""
        application = RecruitmentApplication.objects.get(pk=application_pk)
        buffer = io.BytesIO()
        page = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        page.setFont("Helvetica-Bold", 16)
        page.drawCentredString(width / 2, height - 120, requirement_code.replace("_", " ").title())
        page.setFont("Helvetica", 12)
        page.drawCentredString(width / 2, height - 150, application.applicant_display_name)
        page.drawCentredString(width / 2, height - 170, f"Application for {application.position.title}")
        page.setFont("Helvetica-Bold", 11)
        page.setFillColorRGB(0.63, 0.11, 0.11)
        page.drawCentredString(width / 2, 60, "SAMPLE - FOR DEMONSTRATION ONLY - ALL DETAILS ARE MADE UP")
        page.saveState()
        page.setFillColorRGB(0.75, 0.1, 0.1, alpha=0.13)
        page.setFont("Helvetica-Bold", 74)
        page.translate(width / 2, height / 2)
        page.rotate(45)
        page.drawCentredString(0, 0, "SAMPLE")
        page.restoreState()
        page.save()
        return SimpleUploadedFile(f"{requirement_code}.pdf", buffer.getvalue(), content_type="application/pdf")
