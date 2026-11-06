"""Management commands."""

import io

from django.core.management import call_command

from ..models import (
    FinalSelection,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
)
from ..services import (
    get_current_workflow_section,
    get_latest_finalized_comparative_assessment_report,
    get_cos_vacancy_deliberation,
)
from .base import BaseRecruitmentTestCase


class E2ESeedCommandTests(BaseRecruitmentTestCase):
    def seeded_application(self, scenario_code):
        application = (
            RecruitmentApplication.objects.filter(
                position__description__startswith=f"E2E seed: {scenario_code} "
            )
            .order_by("id")
            .first()
        )
        self.assertIsNotNone(application)
        return application

    def test_seed_e2e_test_cases_creates_verification_cases(self):
        out = io.StringIO()
        call_command("seed_e2e_test_cases", stdout=out, base_email="e2e@example.test")

        cos_screening = self.seeded_application("cos-screening")
        self.assertEqual(cos_screening.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(get_current_workflow_section(cos_screening), "screening")
        self.assertFalse(cos_screening.screening_records.filter(is_finalized=True).exists())

        exam = self.seeded_application("plantilla-exam")
        self.assertEqual(exam.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(get_current_workflow_section(exam), "exam")
        self.assertTrue(exam.screening_records.filter(is_finalized=True).exists())

        interview = self.seeded_application("plantilla-interview")
        self.assertEqual(
            interview.case.current_stage,
            RecruitmentCase.Stage.HRMPSB_REVIEW,
        )
        self.assertEqual(get_current_workflow_section(interview), "interview")
        self.assertFalse(
            interview.interview_sessions.filter(is_finalized=True).exists()
        )

        deliberation = self.seeded_application("plantilla-deliberation")
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

        cos_decision = self.seeded_application("cos-decision")
        self.assertEqual(cos_decision.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        # SL-01: the COS decision is made by finalizing the vacancy pick, so the case that
        # still awaits a decision is one whose pick is saved but not yet finalized.
        self.assertEqual(get_current_workflow_section(cos_decision), "deliberation")
        pick = get_cos_vacancy_deliberation(cos_decision.position)
        self.assertIsNotNone(pick)
        self.assertFalse(pick.is_finalized)

        final_selection = self.seeded_application("plantilla-final-selection")
        final_selection_report = get_latest_finalized_comparative_assessment_report(
            final_selection
        )
        self.assertEqual(final_selection.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(final_selection), "decision")
        self.assertEqual(final_selection_report.items.count(), 6)
        self.assertTrue(final_selection_report.items.filter(rank_order__gt=5).exists())

        aa_decision = self.seeded_application("plantilla-aa-decision")
        self.assertEqual(aa_decision.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(aa_decision), "decision")
        self.assertIsNotNone(get_latest_finalized_comparative_assessment_report(aa_decision))
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=aa_decision.position).exists())

        aa_return = self.seeded_application("plantilla-aa-return")
        self.assertEqual(aa_return.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(get_current_workflow_section(aa_return), "decision")
        self.assertIsNotNone(get_latest_finalized_comparative_assessment_report(aa_return))
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=aa_return.position).exists())

        completion = self.seeded_application("plantilla-completion")
        self.assertEqual(completion.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(get_current_workflow_section(completion), "completion")
        self.assertTrue(hasattr(completion, "completion_record"))
        self.assertEqual(completion.completion_record.requirements.count(), 1)

        seeded_applications = RecruitmentApplication.objects.filter(
            position__description__startswith="E2E seed:"
        )
        self.assertTrue(seeded_applications.exists())
        for application in seeded_applications:
            self.assertRegex(application.reference_number, r"^CASE-\d{4}-\d{4,}$")
        self.assertNotIn("RG-", out.getvalue())

        call_command("seed_e2e_test_cases", stdout=io.StringIO(), base_email="e2e@example.test")
        self.assertEqual(
            PositionPosting.objects.filter(
                description__startswith="E2E seed: plantilla-exam "
            ).count(),
            1,
        )
        self.assertEqual(
            PositionPosting.objects.filter(
                description__startswith="E2E seed: plantilla-interview "
            ).count(),
            1,
        )
        self.assertEqual(
            PositionPosting.objects.filter(
                description__startswith="E2E seed: plantilla-deliberation "
            ).count(),
            1,
        )


class SeedDemoRealClockTests(BaseRecruitmentTestCase):
    """The suite normally switches off the "has this exam happened yet" rule. A real server
    does not, and the seed crashed there on its first run."""

    enforce_event_held = True

    def test_seed_runs_with_the_real_clock_and_leaves_held_events_in_the_past(self):
        from django.utils import timezone

        from recruitment.models import ExamRecord, PositionPosting

        call_command("seed_demo_data", force=True, stdout=io.StringIO())

        self.assertTrue(PositionPosting.objects.exists())
        finalized = ExamRecord.objects.filter(finalized_at__isnull=False)
        self.assertTrue(finalized.exists())
        for record in finalized.select_related("application"):
            for sitting in record.application.exam_schedules.all():
                self.assertLess(sitting.scheduled_for, timezone.now())


class SeedDemoPanelTests(BaseRecruitmentTestCase):
    """Round G (G4): the demo seed models the office's panel rule."""

    def test_seed_sets_a_standing_panel_and_one_end_user_per_plantilla_vacancy(self):
        from recruitment.models import OfficePolicy, PositionPosting
        from recruitment.services import get_office_policy

        call_command("seed_demo_data", force=True, stdout=io.StringIO())
        core = set(get_office_policy().core_panel_members.values_list("pk", flat=True))
        self.assertEqual(len(core), 3)
        self.assertIn(self.hrmpsb.pk, core)
        plantilla = PositionPosting.objects.filter(branch=PositionPosting.Branch.PLANTILLA)
        self.assertTrue(plantilla.exists())
        for posting in plantilla:
            end_users = set(posting.panel_end_users.values_list("pk", flat=True))
            self.assertEqual(len(end_users), 1, posting.job_code)
            self.assertFalse(end_users & core)

        # A second run leaves the same shape (the reseed on every deploy).
        call_command("seed_demo_data", force=True, stdout=io.StringIO())
        self.assertEqual(get_office_policy().core_panel_members.count(), 3)
        self.assertEqual(OfficePolicy.objects.count(), 1)
