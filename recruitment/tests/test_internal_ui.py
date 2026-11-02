"""Internal portal pages: batch console, closed cases, schedule and display."""

from datetime import timedelta

from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from ..models import (
    ExamSchedule,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..services import submit_application, user_can_view_application
from .base import User, BaseRecruitmentTestCase


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


class ClosedCaseLookupTests(BaseRecruitmentTestCase):
    """TI-4: Closed Cases can be narrowed by outcome, closed date, year and vacancy, searched
    by e-mail, and sorted, without ever widening what a role may see."""

    def _closed(self, position, status, closed_at, email):
        application = self.make_application(position)
        RecruitmentApplication.objects.filter(pk=application.pk).update(
            status=status, closed_at=closed_at, applicant_email=email
        )
        application.refresh_from_db()
        return application

    def setUp(self):
        super().setUp()
        tz = timezone.get_current_timezone()
        self.approved_2025 = self._closed(
            self.level1_position,
            RecruitmentApplication.Status.APPROVED,
            timezone.datetime(2025, 3, 10, 10, 0, tzinfo=tz),
            "ana@example.com",
        )
        self.rejected_2026 = self._closed(
            self.cos_position,
            RecruitmentApplication.Status.REJECTED,
            timezone.datetime(2026, 2, 5, 10, 0, tzinfo=tz),
            "ben@example.com",
        )
        self.withdrawn_2026 = self._closed(
            self.cos_continuous_position,
            RecruitmentApplication.Status.WITHDRAWN,
            timezone.datetime(2026, 6, 1, 10, 0, tzinfo=tz),
            "cara@example.com",
        )
        self.level2_closed = self._closed(
            self.level2_position,
            RecruitmentApplication.Status.APPROVED,
            timezone.datetime(2026, 1, 20, 10, 0, tzinfo=tz),
            "dan@example.com",
        )
        self.client_sec = Client()
        self.force_login_with_mfa(self.client_sec, self.secretariat)

    def _ids(self, response):
        return [application.pk for application in response.context["applications"]]

    def test_outcome_filter(self):
        response = self.client_sec.get(reverse("workflow-closed"), {"outcome": "withdrawn"})
        self.assertEqual(self._ids(response), [self.withdrawn_2026.pk])

    def test_year_and_date_range_filters(self):
        response = self.client_sec.get(reverse("workflow-closed"), {"year": "2025"})
        self.assertEqual(self._ids(response), [self.approved_2025.pk])

        response = self.client_sec.get(
            reverse("workflow-closed"), {"closed_from": "2026-01-01", "closed_to": "2026-03-31"}
        )
        self.assertEqual(self._ids(response), [self.rejected_2026.pk])

    def test_reversed_date_range_is_treated_as_the_same_range(self):
        response = self.client_sec.get(
            reverse("workflow-closed"), {"closed_from": "2026-03-31", "closed_to": "2026-01-01"}
        )
        self.assertEqual(self._ids(response), [self.rejected_2026.pk])

    def test_vacancy_filter_and_combined_filters(self):
        response = self.client_sec.get(
            reverse("workflow-closed"),
            {"vacancy": str(self.level1_position.pk), "outcome": "approved"},
        )
        self.assertEqual(self._ids(response), [self.approved_2025.pk])

    def test_search_matches_applicant_email(self):
        response = self.client_sec.get(reverse("workflow-closed"), {"q": "ben@example"})
        self.assertEqual(self._ids(response), [self.rejected_2026.pk])

    def test_sorting(self):
        newest = self._ids(self.client_sec.get(reverse("workflow-closed")))
        self.assertEqual(newest, [self.withdrawn_2026.pk, self.rejected_2026.pk, self.approved_2025.pk])
        oldest = self._ids(self.client_sec.get(reverse("workflow-closed"), {"sort": "oldest"}))
        self.assertEqual(oldest, list(reversed(newest)))

    def test_secretariat_cannot_reach_level2_cases_through_any_filter(self):
        for params in (
            {"vacancy": str(self.level2_position.pk)},
            {"q": "dan@example"},
            {"year": "2026", "outcome": "approved"},
        ):
            response = self.client_sec.get(reverse("workflow-closed"), params)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.level2_closed.pk, self._ids(response), params)
        # The level-2 vacancy is not even offered as a choice.
        response = self.client_sec.get(reverse("workflow-closed"))
        vacancy_choices = dict(response.context["closed_filter_form"].fields["vacancy"].choices)
        self.assertNotIn(str(self.level2_position.pk), vacancy_choices)

    def test_hrm_chief_sees_level2_closed_cases(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("workflow-closed"), {"vacancy": str(self.level2_position.pk)})
        self.assertEqual(self._ids(response), [self.level2_closed.pk])

    def test_invalid_filter_values_are_ignored(self):
        response = self.client_sec.get(
            reverse("workflow-closed"),
            {"closed_from": "not-a-date", "outcome": "bogus", "year": "1999", "vacancy": "abc"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._ids(response)), 3)

    def test_pagination_and_branch_chips_keep_the_filters(self):
        response = self.client_sec.get(
            reverse("workflow-closed"), {"outcome": "approved", "year": "2025"}
        )
        self.assertContains(response, "outcome=approved")
        self.assertContains(response, "?branch=plantilla&amp;outcome=approved&amp;year=2025")
        self.assertContains(response, "Clear filters")

    def test_page_is_titled_closed_cases(self):
        response = self.client_sec.get(reverse("workflow-closed"))
        self.assertContains(response, "<title>Past Records | Internal Portal</title>", html=False)
        self.assertContains(response, "data-closed-filters")


class ScheduleCalendarTests(BaseRecruitmentTestCase):
    """TI-5: the read-only schedule only shows events from cases the user can already open."""

    def _submitted(self, position, applicant=None):
        # make_application always uses self.applicant (one application per applicant per
        # vacancy), so swap in the second applicant for the batch-grouping case.
        original = self.applicant
        if applicant is not None:
            self.applicant = applicant
        try:
            application = self.make_application(position)
        finally:
            self.applicant = original
        self.verify_application_for_submission(application)
        submit_application(application, application.applicant)
        application.refresh_from_db()
        return application

    def _exam(self, application, when, venue="Regional Office Testing Room"):
        return ExamSchedule.objects.create(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=application.case.current_stage,
            scheduled_by=self.secretariat,
            scheduled_for=when,
            venue=venue,
        )

    def setUp(self):
        super().setUp()
        self.when = timezone.localtime(timezone.now() + timedelta(days=2)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        self.l1_app = self._submitted(self.level1_position)
        self.second_applicant = User.objects.create_user(
            username="applicant2",
            password="testpass123",
            email="applicant2@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        self.l1_app_two = self._submitted(self.level1_position, applicant=self.second_applicant)
        self.l2_app = self._submitted(self.level2_position)
        self._exam(self.l1_app, self.when)
        self._exam(self.l1_app_two, self.when)
        self._exam(self.l2_app, self.when, venue="Level 2 Room")

    def _events(self, user):
        from ..schedule_services import get_schedule_events_for_user

        today = timezone.localdate()
        return get_schedule_events_for_user(user, today, today + timedelta(days=30))

    def test_batch_exam_is_one_event_with_an_applicant_count(self):
        exams = [e for e in self._events(self.secretariat) if e.kind == "exam"]
        self.assertEqual(len(exams), 1)
        self.assertEqual(exams[0].count, 2)
        self.assertEqual(exams[0].vacancy, self.level1_position)
        self.assertIn("(2 applicants)", exams[0].title)

    def test_secretariat_never_sees_level2_events(self):
        events = self._events(self.secretariat)
        # Case events (exam, interview, deadlines on a case) must never leak a Level 2 case.
        # A vacancy's public application deadline is different: the Secretariat already
        # manages every posting on Recruitment Entries and the portal publishes it.
        level2_case_events = [
            e for e in events if e.vacancy == self.level2_position and e.kind != "closing"
        ]
        self.assertEqual(level2_case_events, [])
        self.assertFalse(
            any(self.l2_app.pk in e.application_ids for e in events)
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("schedule-calendar"), {"view": "agenda"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Regional Office Testing Room")
        self.assertNotContains(response, "Level 2 Room")

    def test_hrm_chief_sees_the_level2_exam(self):
        exams = [e for e in self._events(self.hrm_chief) if e.kind == "exam"]
        self.assertIn(self.level2_position, [e.vacancy for e in exams])

    def test_hrmpsb_member_sees_nothing_outside_their_access(self):
        for event in self._events(self.hrmpsb):
            for application_id in event.application_ids:
                application = RecruitmentApplication.objects.get(pk=application_id)
                self.assertTrue(user_can_view_application(self.hrmpsb, application))

    def test_events_never_name_the_applicant(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("schedule-calendar"), {"view": "agenda"})
        self.assertNotContains(response, self.l2_app.applicant_last_name)
        self.assertNotContains(response, self.l2_app.applicant_email)

    def test_access_is_limited_to_workflow_users(self):
        response = Client().get(reverse("schedule-calendar"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        self.assertEqual(client.get(reverse("schedule-calendar")).status_code, 403)

    def test_invalid_month_falls_back_to_the_current_month(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("schedule-calendar"), {"month": "2026-13"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["month_start"], timezone.localdate().replace(day=1))
        response = client.get(reverse("schedule-calendar"), {"month": "garbage"})
        self.assertEqual(response.status_code, 200)

    def test_month_view_places_the_exam_on_its_day(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("schedule-calendar"), {"month": self.when.strftime("%Y-%m")}
        )
        cells = [cell for week in response.context["weeks"] for cell in week]
        exam_days = [cell["day"] for cell in cells if any(e.kind == "exam" for e in cell["events"])]
        self.assertEqual(exam_days, [self.when.date()])

    def test_landing_page_shows_the_coming_up_panel(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batches"))
        self.assertContains(response, "data-coming-up")
        self.assertContains(response, 'data-schedule-event="exam"')

    def test_sidebar_links_to_the_schedule_for_workflow_users(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        self.assertContains(client.get(reverse("vacancy-batches")), reverse("schedule-calendar"))


class DisplayComfortTests(BaseRecruitmentTestCase):
    """TI-3: user-adjustable text size on both portals and a compact density option for
    internal users; print outputs are left untouched."""

    def test_applicant_pages_load_the_display_preferences_and_text_size_control(self):
        for url in (
            reverse("applicant-portal"),
            reverse("applicant-help"),
            reverse("applicant-intake", kwargs={"pk": self.level1_position.pk}),
        ):
            response = self.client.get(url)
            self.assertContains(response, 'localStorage.getItem(key)', msg_prefix=url)
            self.assertContains(response, 'data-rg-text-size="larger"', msg_prefix=url)
            self.assertNotContains(response, "data-rg-density=", msg_prefix=url)

    def test_internal_top_bar_offers_text_size_and_density(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batches"))
        self.assertContains(response, "rg-display-menu")
        self.assertContains(response, 'data-rg-text-size="large"')
        self.assertContains(response, 'data-rg-density="compact"')

    def test_sign_in_page_honours_the_saved_text_size(self):
        self.assertContains(self.client.get(reverse("login")), '"rg-text-size"')

    def test_print_templates_do_not_pick_up_screen_display_preferences(self):
        from django.template.loader import get_template

        for name in (
            "recruitment/car_print.html",
            "recruitment/car_filing_copy.html",
            "recruitment/interview_rating_sheet_print.html",
            "recruitment/audit_report_print.html",
        ):
            source = get_template(name).template.source
            self.assertNotIn("display_prefs_head", source, name)
            self.assertNotIn('{% extends "base.html" %}', source, name)


class InterviewFinalizeButtonScriptTests(SimpleTestCase):
    """Walkthrough regression: the Finalize session handler read `maxRequiredOutputs` without
    declaring it, so every click threw a ReferenceError and the interview could not be finalized
    from the UI. Every variable the handler reads must be declared in the template script."""

    def test_finalize_handler_variables_are_declared(self):
        from django.template.loader import get_template

        source = get_template("internal_includes/interview_session_manager.html").template.source
        for name in ("minRequiredOutputs", "maxRequiredOutputs", "totalRatings", "fallbackCount"):
            self.assertIn(f"var {name} =", source, name)
