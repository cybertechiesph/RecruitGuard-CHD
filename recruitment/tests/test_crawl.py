"""The link crawl: as every role, open every page the role can reach and follow every link,
with a case sitting at each step of each path. A link a user is shown must lead somewhere: no
crash, no dead page, no refusal, and no bounce back to where it started.
"""

import collections
import re
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from django.test import Client
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from ..forms import PositionReferenceForm
from ..models import AuditLog, PositionPosting, PositionReference
from ..schedule_services import get_schedule_events_for_user
from ..services import record_application_withdrawal, save_interview_session, submit_application
from .base import BaseRecruitmentTestCase, BatchScreeningHelpers

SKIP_ROUTES = {"logout"}
PAGES_PER_ROUTE = 30
QUERIES_PER_ROUTE = 10


class StagedCasesMixin(BatchScreeningHelpers):
    """One vacancy per step, so each case stands alone at the step it is meant to show."""

    def staged_vacancy(self, reference, branch, level, code):
        return PositionPosting.objects.create(
            position_reference=reference,
            branch=branch,
            level=level,
            item_number=code if branch == PositionPosting.Branch.PLANTILLA else "",
            intake_mode=(
                PositionPosting.IntakeMode.FIXED_PERIOD
                if branch == PositionPosting.Branch.PLANTILLA
                else PositionPosting.IntakeMode.OPENING_BASED
            ),
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )

    def plantilla(self, level, code):
        reference = (
            self.admin_aide_position if level == PositionPosting.Level.LEVEL_1 else self.medical_officer_position
        )
        return self.staged_vacancy(reference, PositionPosting.Branch.PLANTILLA, level, code)

    def cos(self):
        return self.staged_vacancy(
            self.project_assistant_position, PositionPosting.Branch.COS, PositionPosting.Level.LEVEL_1, ""
        )

    def build_staged_cases(self):
        one, two = PositionPosting.Level.LEVEL_1, PositionPosting.Level.LEVEL_2
        cases = {}

        for level, code, screener in ((one, "L1", self.secretariat), (two, "L2", self.hrm_chief)):
            # Screening
            application = self.make_application(self.plantilla(level, f"{code}-SCREEN"))
            self.submit_for_crawl(application)
            cases[f"plantilla {code} screening"] = application
            # Exam
            application = self.make_application(self.plantilla(level, f"{code}-EXAM"))
            self.submit_for_crawl(application)
            self.finalize_screening_for_current_stage(application, screener)
            cases[f"plantilla {code} exam"] = application
            # Interview
            application = self.make_application(self.plantilla(level, f"{code}-INT"))
            self.move_application_to_hrmpsb_review(application)
            cases[f"plantilla {code} interview"] = application
            # Decision, then completion
            application = self.make_application(self.plantilla(level, f"{code}-DEC"))
            self.move_application_to_appointing_review(application)
            cases[f"plantilla {code} decision"] = application
            cases[f"plantilla {code} completion"] = self.make_selected_application(
                self.plantilla(level, f"{code}-DONE")
            )

        # COS: Secretariat first, then the HRM Chief's check, interview and pick.
        application = self.make_application(self.cos())
        self.submit_for_crawl(application)
        cases["cos screening"] = application
        application = self.make_application(self.cos())
        self.move_application_to_hrm_chief_review(application)
        cases["cos chief check"] = application
        application = self.make_application(self.cos())
        self.move_application_to_appointing_review(application)
        cases["cos deliberation"] = application
        cases["cos completion"] = self.make_selected_application(self.cos())

        # A case that ended with an interview still on the calendar: the "Coming up" trap.
        application = self.make_application(self.plantilla(one, "L1-LEFT"))
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        application.interview_sessions.update(scheduled_for=timezone.now() + timedelta(days=2))
        application.refresh_from_db()
        record_application_withdrawal(application, self.secretariat, "Left the competition.")
        cases["closed with a date ahead"] = application

        cases.update(self.build_work_in_progress())
        return cases

    def build_work_in_progress(self):
        """Cases paused where someone has work to do, so the consoles and forms are on screen.
        A vacancy's batch screens only open once its applications have closed."""
        one, two = PositionPosting.Level.LEVEL_1, PositionPosting.Level.LEVEL_2
        cases = {}
        for level, code, office in ((one, "L1", self.secretariat), (two, "L2", self.hrm_chief)):
            # Batch exam: screened, applications closed, waiting for a schedule and scores.
            vacancy = self.plantilla(level, f"{code}-BEXAM")
            application = self.make_application(vacancy)
            self.submit_for_crawl(application)
            self.finalize_screening_for_current_stage(application, office)
            self.finalize_applicant_pool_for_test(vacancy)
            cases[f"plantilla {code} batch exam"] = application

            # Interview with scoring open: the rating sheet is out and the time has come.
            vacancy = self.plantilla(level, f"{code}-RATE")
            application = self.make_application(vacancy)
            self.move_application_to_hrmpsb_review(application)
            self.finalize_applicant_pool_for_test(vacancy)
            self.publish_competency_rating_sheet(vacancy)
            save_interview_session(
                application=application,
                actor=office,
                cleaned_data={
                    "scheduled_for": timezone.now() + timedelta(days=1),
                    "location": "Conference Room A",
                    "session_notes": "Crawl.",
                },
                finalize=False,
            )
            application.interview_sessions.update(scheduled_for=timezone.now() - timedelta(hours=1))
            cases[f"plantilla {code} scoring open"] = application

            # CAR being prepared: the interview is finished, the report is not started.
            vacancy = self.plantilla(level, f"{code}-CAR")
            application = self.make_application(vacancy)
            self.move_application_to_hrmpsb_review(application)
            self.finalize_applicant_pool_for_test(vacancy)
            self.finalize_interview_for_current_stage(application, self.hrmpsb)
            cases[f"plantilla {code} car"] = application

        # COS waiting for the HRM Chief's pick.
        vacancy = self.cos()
        application = self.make_application(vacancy)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        self.finalize_applicant_pool_for_test(vacancy)
        cases["cos pick waiting"] = application

        # COS batch exam at the Secretariat.
        vacancy = self.cos()
        application = self.make_application(vacancy)
        self.submit_for_crawl(application)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_applicant_pool_for_test(vacancy)
        cases["cos batch exam"] = application

        # An open pool, which runs selection rounds.
        application = self.make_application(self.cos_continuous_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        cases["cos open pool"] = application

        # Documents out for correction.
        application = self.make_application(self.plantilla(one, "L1-FIX"))
        self.submit_for_crawl(application)
        self._make_awaiting_case(application)
        cases["documents returned"] = application
        return cases

    def submit_for_crawl(self, application):
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        return application


def route_of(path):
    try:
        return resolve(urlsplit(path).path).url_name or "?"
    except Resolver404:
        return "no route"


def links_on(html, base):
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    found = set()
    for href in re.findall(r'<a\b[^>]*\bhref="([^"]+)"', body):
        href = href.replace("&amp;", "&").strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        parts = urlsplit(urljoin(base, href))
        if parts.netloc and parts.netloc not in ("testserver", "localhost"):
            continue
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if not path.startswith(("/static/", "/media/")):
            found.add(path)
    return found


def crawl(client, starts):
    """Every problem found walking from `starts`, as (kind, url, the page that linked it)."""
    problems, seen = [], set()
    pages, queries = collections.Counter(), collections.Counter()
    queue = collections.deque((start, "") for start in starts)
    while queue:
        path, source = queue.popleft()
        route = route_of(path)
        if path in seen or route in SKIP_ROUTES:
            continue
        counter, limit = (queries, QUERIES_PER_ROUTE) if "?" in path else (pages, PAGES_PER_ROUTE)
        counter[route] += 1
        if counter[route] > limit:
            continue
        seen.add(path)
        response = client.get(path)
        status = response.status_code
        if status >= 500:
            problems.append(("crash", path, source))
        elif status == 404:
            problems.append(("dead link", path, source))
        elif status == 403:
            problems.append(("forbidden link", path, source))
        elif status in (301, 302):
            target = urlsplit(response["Location"]).path
            if source and target == urlsplit(source).path:
                problems.append(("bounce", path, source))
            elif route.startswith("vacancy-batch") and target.rstrip("/").endswith("/vacancies"):
                problems.append(("bounce", path, source))
            else:
                queue.append((target, path))
        elif status == 200 and "text/html" in response.get("Content-Type", ""):
            for link in links_on(response.content.decode("utf-8", "replace"), path):
                if link not in seen:
                    queue.append((link, path))
    return problems, seen


class LinkCrawlTests(StagedCasesMixin, BaseRecruitmentTestCase):
    def setUp(self):
        super().setUp()
        self.cases = self.build_staged_cases()
        # Sign-ins with no person attached, the kind a mistyped username leaves in the log.
        AuditLog.objects.create(
            action=AuditLog.Action.INTERNAL_LOGIN_FAILED,
            description="Internal login failed.",
        )

    def staff_client(self, user):
        client = Client(raise_request_exception=False)
        self.force_login_with_mfa(client, user)
        return client

    def assertCrawlClean(self, user):
        problems, seen = crawl(self.staff_client(user), ["/internal/"])
        self.assertGreater(len(seen), 5, "the crawl never got past the first page")
        self.assertEqual(problems, [])

    def test_the_secretariat_has_no_broken_links(self):
        self.assertCrawlClean(self.secretariat)

    def test_the_hrm_chief_has_no_broken_links(self):
        self.assertCrawlClean(self.hrm_chief)

    def test_a_panel_member_has_no_broken_links(self):
        self.assertCrawlClean(self.hrmpsb)

    def test_the_appointing_authority_has_no_broken_links(self):
        self.assertCrawlClean(self.appointing)

    def test_the_system_administrator_has_no_broken_links(self):
        self.assertCrawlClean(self.sysadmin)

    def test_the_public_pages_have_no_broken_links(self):
        client = Client(raise_request_exception=False)
        problems, seen = crawl(client, ["/apply/", "/apply/help/", "/apply/status/", "/apply/my/"])
        self.assertGreater(len(seen), 3)
        self.assertEqual(problems, [])


class SweepFindingTests(StagedCasesMixin, BaseRecruitmentTestCase):
    """What the mechanical sweep found, kept fixed."""

    def staff_client(self, user):
        client = Client(raise_request_exception=False)
        self.force_login_with_mfa(client, user)
        return client

    def closed_case_with_a_date_ahead(self):
        vacancy = self.plantilla(PositionPosting.Level.LEVEL_1, "L1-GONE")
        application = self.make_application(vacancy)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        application.interview_sessions.update(scheduled_for=timezone.now() + timedelta(days=2))
        application.refresh_from_db()
        record_application_withdrawal(application, self.secretariat, "Left the competition.")
        return vacancy, application

    def test_a_finished_case_is_not_coming_up(self):
        vacancy, _ = self.closed_case_with_a_date_ahead()
        today = timezone.localdate()
        events = get_schedule_events_for_user(self.hrm_chief, today, today + timedelta(days=6))
        self.assertEqual([e for e in events if e.vacancy == vacancy and e.kind == "interview"], [])

    def test_a_past_event_of_a_finished_vacancy_links_to_the_case(self):
        vacancy, application = self.closed_case_with_a_date_ahead()
        when = timezone.now() - timedelta(days=2)
        application.interview_sessions.update(scheduled_for=when)
        day = timezone.localdate(when)
        events = get_schedule_events_for_user(self.hrm_chief, day, day)
        event = next(e for e in events if e.vacancy == vacancy and e.kind == "interview")
        self.assertEqual(event.url, reverse("application-detail", args=[application.pk]))

    def test_the_audit_report_shows_system_for_an_entry_with_no_person(self):
        AuditLog.objects.create(action=AuditLog.Action.INTERNAL_LOGIN_FAILED, description="Internal login failed.")
        response = self.staff_client(self.sysadmin).get(reverse("audit-log-report") + "?category=security")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<td>System</td>", html=True)

    def test_clearing_a_detail_of_a_verified_reference_saves_it_as_incomplete(self):
        reference = self.admin_aide_position
        reference.reference_status = PositionReference.ReferenceStatus.VERIFIED_REFERENCE
        reference.save()
        url = reverse("position-reference-update", kwargs={"pk": reference.pk})
        client = self.staff_client(self.sysadmin)

        blank = client.post(url, {})
        self.assertEqual(blank.status_code, 200)
        self.assertContains(blank, "This field is required")

        data = {
            name: getattr(reference, name) or ""
            for name in PositionReferenceForm.Meta.fields
            if name != "is_active"
        }
        data.update({"is_active": "on", "class_id": ""})
        response = client.post(url, data)
        self.assertEqual(response.status_code, 302, response.content[:600])
        reference.refresh_from_db()
        self.assertEqual(reference.reference_status, PositionReference.ReferenceStatus.INCOMPLETE_REFERENCE)
