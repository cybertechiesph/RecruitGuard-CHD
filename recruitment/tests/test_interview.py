"""Interviews: batch schedule, sessions, ratings and competency sheets."""

from decimal import ROUND_HALF_UP, Decimal
from datetime import timedelta

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AttendanceException,
    AttendanceOutcome,
    AuditLog,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    CompetencyDefinition,
    CompetencyRatingTemplate,
    CompetencyScore,
    InterviewRating,
    Notification,
    NotificationLog,
    OfficePolicy,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..services import (
    get_current_workflow_section,
    get_office_policy,
    interview_is_finalized_for_current_stage,
    record_final_decision,
    user_can_record_final_decision,
    save_cos_vacancy_deliberation,
    get_cos_vacancy_deliberation,
    get_latest_final_decision,
    cos_deliberation_pool,
    save_exam_record,
    save_exam_schedule,
    create_competency_rating_template,
    get_competency_rating_template,
    save_competency_rating_sheet,
    get_published_competency_rating_template,
    compute_competency_rating_score,
    save_interview_rating,
    save_interview_session,
    submit_application,
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
    record_attendance_absence,
    record_application_withdrawal,
    user_can_record_withdrawal,
    user_can_manage_attendance_exception,
    cancel_recruitment_entry,
    get_pending_attendance_exception,
    record_attendance_letter_of_intent,
    resolve_attendance_absence,
    get_interview_fallback_evidence,
    get_interview_session,
    interview_session_rater_ids,
    _average_interview_rating,
    user_can_view_application,
)
from .base import User, BaseRecruitmentTestCase, InterviewSessionHelpers


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

    def _three_plantilla_at_interview(self):
        applications = [
            self._submit(self.make_application(self.level1_position), self.applicant),
            self._extra_submitted_application(self.level1_position, "x"),
            self._extra_submitted_application(self.level1_position, "y"),
        ]
        for application in applications:
            self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_applicant_pool_for_test(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._exam_schedule_payload()
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    application.case.id: {
                        "general_score": str(92 - index),
                        "technical_score": str(86 - index),
                    }
                    for index, application in enumerate(applications)
                },
                finalize=True,
            )
        for application in applications:
            application.refresh_from_db()
        return applications

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
        # Screen BOTH before examining either: the batch gate (§4.3) follows the pool to
        # the chief stage now, so the second-pass exam waits for every second-pass
        # screening — exactly as the console enforces it.
        for application in (first, second):
            application.refresh_from_db()
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
        for application in (first, second):
            application.refresh_from_db()
            self.finalize_exam_for_current_stage(application, self.hrm_chief)
            application.refresh_from_db()
        return first, second

    def test_a_cos_interview_no_show_is_recorded_on_the_console_and_held_from_the_pick(self):
        # Live COS E2E regression, two halves:
        # (1) The per-case absence form is hidden for pooled vacancies on the promise that
        #     "a pooled interview records absence on the batch console" — but the console
        #     control was never built, so interview absence had NO door. The console now
        #     posts int_outcome_<case> through to the service's attendance_outcome row.
        # (2) A no-show whose absence decision is still PENDING sat in the deliberation
        #     pool, pickable — and pool-closable — before the office decided their letter.
        #     cos_eligible_pool now holds them out until the decision lands.
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
                    f"int_outcome_{second.case.id}": "absent",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        # The scored candidate reached deliberation; the no-show holds with a PENDING
        # exception and no fabricated score.
        self.assertEqual(get_current_workflow_section(first), "deliberation")
        absence = get_pending_attendance_exception(second)
        self.assertIsNotNone(absence)
        self.assertEqual(absence.assessment_stage, AttendanceException.Stage.INTERVIEW)
        # And the pick pool holds the undecided no-show out.
        self.assertEqual(
            {a.pk for a in cos_deliberation_pool(self.cos_position)}, {first.pk}
        )
        # Once the office decides the absence, the case resolves and stays out.
        with self.captureOnCommitCallbacks(execute=True):
            resolve_attendance_absence(
                absence,
                self.hrm_chief,
                AttendanceException.Outcome.TERMINAL,
                "Letter reviewed; the office will not reschedule.",
            )
        second.refresh_from_db()
        self.assertEqual(second.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(
            {a.pk for a in cos_deliberation_pool(self.cos_position)}, {first.pk}
        )

    def test_a_cos_draft_absence_clears_when_the_console_later_posts_a_score(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        console_url = reverse("vacancy-batch-interview", args=[self.cos_position.pk])

        draft = client.post(
            console_url,
            {
                "operation": "save_scores",
                f"int_outcome_{second.case.id}": AttendanceOutcome.ABSENT,
            },
        )
        self.assertEqual(draft.status_code, 302)
        second_session = get_interview_session(second)
        self.assertEqual(second_session.attendance_outcome, AttendanceOutcome.ABSENT)
        self.assertFalse(second_session.is_finalized)
        self.assertIsNone(
            get_pending_attendance_exception(
                second, AttendanceException.Stage.INTERVIEW
            )
        )

        with self.captureOnCommitCallbacks(execute=True):
            finalized = client.post(
                console_url,
                {
                    "operation": "finalize",
                    f"rating_score_{first.case.id}": "88",
                    f"rating_score_{second.case.id}": "79",
                },
            )
        self.assertEqual(finalized.status_code, 302)
        second_session.refresh_from_db()
        self.assertEqual(second_session.attendance_outcome, AttendanceOutcome.ATTENDED)
        self.assertTrue(second_session.is_finalized)
        self.assertEqual(
            str(second.interview_ratings.get().rating_score),
            "79.00",
        )
        for application in (first, second):
            application.refresh_from_db()
            self.assertEqual(get_current_workflow_section(application), "deliberation")

    def test_a_cos_score_refuses_an_absence_that_has_entered_the_decision_process(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
        save_vacancy_cos_interview_ratings(
            self.cos_position,
            self.hrm_chief,
            {
                second.case.id: {
                    "attendance_outcome": AttendanceOutcome.ABSENT,
                }
            },
            finalize=False,
        )
        absence = record_attendance_absence(
            second,
            self.hrm_chief,
            AttendanceException.Stage.INTERVIEW,
            notes="Absence entered into the letter-of-intent process.",
        )

        refusal = (
            "This candidate has a recorded absence awaiting a letter-of-intent decision. "
            "Resolve the absence (reschedule) before recording a score."
        )
        with self.assertRaisesMessage(ValueError, refusal):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {second.case.id: {"rating_score": "79"}},
                finalize=False,
            )
        self.assertFalse(second.interview_ratings.exists())
        self.assertEqual(
            get_interview_session(second).attendance_outcome,
            AttendanceOutcome.ABSENT,
        )

        resolve_attendance_absence(
            absence,
            self.hrm_chief,
            AttendanceException.Outcome.RESCHEDULED,
            "The candidate will attend the rescheduled sitting.",
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {
                    first.case.id: {"rating_score": "88"},
                    second.case.id: {"rating_score": "79"},
                },
                finalize=True,
            )
        self.assertTrue(interview_is_finalized_for_current_stage(second))

    def test_a_decided_terminal_cos_absence_cannot_be_scored_from_the_console(self):
        first, second = self._two_cos_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.cos_position, self.hrm_chief, self._interview_schedule_payload()
            )
            for application in (first, second):
                mark_interview_absent(
                    application,
                    self.hrm_chief,
                    notes="Did not attend.",
                )
                resolve_attendance_absence(
                    get_pending_attendance_exception(
                        application, AttendanceException.Stage.INTERVIEW
                    ),
                    self.hrm_chief,
                    AttendanceException.Outcome.TERMINAL,
                    "The absence was decided as terminal.",
                )

        with self.assertRaisesMessage(
            ValueError, "No candidates are ready for the interview in this vacancy."
        ):
            save_vacancy_cos_interview_ratings(
                self.cos_position,
                self.hrm_chief,
                {second.case.id: {"rating_score": "79"}},
                finalize=False,
            )
        second.refresh_from_db()
        second.case.refresh_from_db()
        self.assertEqual(second.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertFalse(second.interview_ratings.exists())

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
            # Blind review (REV-B): the panel's email lists candidate codes, never names.
            self.assertIn(first.candidate_code_label, log.body)
            self.assertIn(second.candidate_code_label, log.body)
            self.assertNotIn(first.applicant_display_name, log.body)

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

    def test_plantilla_console_records_a_no_show_without_blocking_the_pool(self):
        first, absentee, third = self._three_plantilla_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.level1_position,
                self.secretariat,
                self._interview_schedule_payload(),
            )

        support_client = Client()
        self.force_login_with_mfa(support_client, self.secretariat)
        console_url = reverse(
            "vacancy-batch-interview", args=[self.level1_position.pk]
        )
        console = support_client.get(console_url)
        self.assertEqual(console.status_code, 200)
        self.assertContains(console, 'value="mark_absent"', count=3)

        # A panel member can rate but is not one of the level-aware scheduling support
        # roles, so a forced console operation still reaches the service guard and fails.
        panel_client = Client()
        self.force_login_with_mfa(panel_client, self.hrmpsb)
        refused = panel_client.post(
            console_url,
            {
                "operation": "mark_absent",
                "case_id": third.case.id,
                "notes": "Forced panel-member attempt.",
            },
        )
        self.assertEqual(refused.status_code, 302)
        self.assertFalse(get_interview_session(third).is_finalized)
        self.assertIsNone(get_pending_attendance_exception(third))

        with self.captureOnCommitCallbacks(execute=True):
            recorded = support_client.post(
                console_url,
                {
                    "operation": "mark_absent",
                    "case_id": absentee.case.id,
                    "notes": "Did not appear at the scheduled panel interview.",
                },
                follow=True,
            )
        self.assertEqual(recorded.status_code, 200)
        self.assertContains(recorded, "Absent &mdash; letter of intent pending", html=True)
        self.assertContains(recorded, 'value="mark_absent"', count=2)

        absence = get_pending_attendance_exception(
            absentee, AttendanceException.Stage.INTERVIEW
        )
        self.assertIsNotNone(absence)
        self.assertTrue(get_interview_session(absentee).candidate_was_absent)
        self.assertEqual(
            absentee.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW
        )

        # The batch console remains the only no-show door for pooled vacancies.
        case_detail = support_client.get(
            reverse("application-detail", kwargs={"pk": absentee.pk}),
            {"tab": "interview"},
        )
        self.assertEqual(case_detail.status_code, 200)
        self.assertNotContains(
            case_detail,
            reverse("interview-absence-record", kwargs={"pk": absentee.pk}),
        )

        panel_raters = [self.hrmpsb] + self.extra_plantilla_panel_raters(
            exclude=self.hrmpsb
        )
        for application in (first, third):
            for panel_rater in panel_raters:
                save_interview_rating(
                    application=application,
                    actor=panel_rater,
                    cleaned_data=self.competency_rating_cleaned_data(
                        application, level=3
                    ),
                )

        finalized = finalize_vacancy_interview_batch(
            self.level1_position, self.secretariat
        )
        self.assertEqual({application.pk for application in finalized}, {first.pk, third.pk})
        for application in (first, third):
            application.refresh_from_db()
            self.assertEqual(get_current_workflow_section(application), "car")
        absentee.refresh_from_db()
        absentee.case.refresh_from_db()
        absence.refresh_from_db()
        self.assertEqual(
            absentee.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW
        )
        self.assertTrue(absence.is_pending)

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


class InterviewManagementTests(InterviewSessionHelpers, BaseRecruitmentTestCase):
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

    def test_cancelled_vacancy_is_terminal_and_list_offers_no_lifecycle_actions(self):
        cancel_recruitment_entry(
            self.level1_position,
            self.secretariat,
            "The vacancy was withdrawn by the requesting office.",
        )
        self.level1_position.refresh_from_db()
        refusal = (
            "This vacancy was cancelled and cannot be reactivated, suspended, or closed. "
            "Publish a new vacancy instead."
        )

        for new_status in (
            PositionPosting.EntryStatus.ACTIVE,
            PositionPosting.EntryStatus.SUSPENDED,
            PositionPosting.EntryStatus.CLOSED,
        ):
            with self.subTest(new_status=new_status):
                with self.assertRaisesMessage(ValidationError, refusal):
                    update_recruitment_entry_status(
                        self.level1_position,
                        self.hrm_chief,
                        new_status,
                    )
                self.level1_position.refresh_from_db()
                self.assertEqual(
                    self.level1_position.status,
                    PositionPosting.EntryStatus.CANCELLED,
                )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("recruitment-entry-list") + "?status=closed")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.level1_position.job_code)
        self.assertContains(response, "Cancelled")
        for new_status in (
            PositionPosting.EntryStatus.ACTIVE,
            PositionPosting.EntryStatus.SUSPENDED,
            PositionPosting.EntryStatus.CLOSED,
        ):
            self.assertNotContains(
                response,
                reverse(
                    "recruitment-entry-status",
                    kwargs={"pk": self.level1_position.pk, "status": new_status},
                ),
            )
        self.assertNotContains(
            response,
            reverse("recruitment-entry-cancel", kwargs={"pk": self.level1_position.pk}),
        )

    def test_vacancy_cancelled_cases_remain_visible_listed_and_terminal_for_batches(self):
        from recruitment.services import get_closed_cases_for_user

        level1_application = self.make_application(self.level1_position)
        level2_application = self.make_application(self.level2_position)
        for application in (level1_application, level2_application):
            self.verify_application_for_submission(application)
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, self.applicant)
            application.refresh_from_db()

        viewers_before_cancellation = {
            level1_application.pk: {
                user.pk
                for user in (
                    self.applicant,
                    self.secretariat,
                    self.hrm_chief,
                    self.hrmpsb,
                    self.appointing,
                    self.sysadmin,
                )
                if user_can_view_application(user, level1_application)
            },
            level2_application.pk: {
                user.pk
                for user in (
                    self.applicant,
                    self.secretariat,
                    self.hrm_chief,
                    self.hrmpsb,
                    self.appointing,
                    self.sysadmin,
                )
                if user_can_view_application(user, level2_application)
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            cancel_recruitment_entry(
                self.level1_position,
                self.secretariat,
                "The requesting office cancelled the Level 1 vacancy.",
            )
            cancel_recruitment_entry(
                self.level2_position,
                self.hrm_chief,
                "The requesting office cancelled the Level 2 vacancy.",
            )

        for application in (level1_application, level2_application):
            application.refresh_from_db()
            application.case.refresh_from_db()
            self.assertEqual(
                application.status,
                RecruitmentApplication.Status.VACANCY_CANCELLED,
            )
            self.assertEqual(
                application.case.case_status,
                RecruitmentCase.CaseStatus.VACANCY_CANCELLED,
            )
            self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
            for user_pk in viewers_before_cancellation[application.pk]:
                user = RecruitmentUser.objects.get(pk=user_pk)
                self.assertTrue(user_can_view_application(user, application))

        for user in (
            self.hrm_chief,
            self.hrmpsb,
            self.appointing,
        ):
            self.assertTrue(user_can_view_application(user, level1_application))
            self.assertTrue(user_can_view_application(user, level2_application))
            closed_ids = {case.pk for case in get_closed_cases_for_user(user)}
            self.assertIn(level1_application.pk, closed_ids)
            self.assertIn(level2_application.pk, closed_ids)

        self.assertTrue(user_can_view_application(self.secretariat, level1_application))
        self.assertFalse(user_can_view_application(self.secretariat, level2_application))
        secretariat_closed_ids = {
            case.pk for case in get_closed_cases_for_user(self.secretariat)
        }
        self.assertIn(level1_application.pk, secretariat_closed_ids)
        self.assertNotIn(level2_application.pk, secretariat_closed_ids)
        self.assertFalse(user_can_view_application(self.sysadmin, level1_application))
        self.assertFalse(user_can_view_application(self.sysadmin, level2_application))
        self.assertFalse(get_closed_cases_for_user(self.sysadmin).exists())

        for posting in (self.level1_position, self.level2_position):
            batch_status = vacancy_screening_batch_status(posting)
            self.assertEqual(batch_status.total, 1)
            self.assertEqual(batch_status.terminal, 1)
            self.assertEqual(batch_status.pending, 0)
            self.assertTrue(batch_status.is_ready)

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

    def test_the_absence_endpoint_files_the_letter_and_decides_over_http(self):
        # Live E2E regression: AttendanceExceptionView referenced the AttendanceException
        # model without importing it, so EVERY letter/decide POST raised NameError — the
        # whole SL-06 surface was dead in the browser while the service-level tests below
        # stayed green. This drives both operations through the real URL.
        application = self._cos_session_awaiting_ratings()
        with self.captureOnCommitCallbacks(execute=True):
            mark_interview_absent(
                application=application,
                actor=self.hrm_chief,
                notes="Did not attend.",
            )
        absence = get_pending_attendance_exception(application)
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "attendance-exception-manage",
                kwargs={"pk": application.pk, "exception_pk": absence.pk},
            ),
            {
                "operation": "letter",
                "file": SimpleUploadedFile(
                    "loi.pdf", b"%PDF-1.4 letter", content_type="application/pdf"
                ),
                "notes": "Received by email.",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Letter of intent filed")
        absence.refresh_from_db()
        self.assertIsNotNone(absence.letter_received_at)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse(
                    "attendance-exception-manage",
                    kwargs={"pk": application.pk, "exception_pk": absence.pk},
                ),
                {
                    "operation": "decide",
                    "outcome": AttendanceException.Outcome.TERMINAL,
                    "reason": "Letter reviewed; the office will not reschedule.",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        absence.refresh_from_db()
        self.assertEqual(absence.outcome, AttendanceException.Outcome.TERMINAL)

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

    def test_default_policy_assigns_plantilla_interview_absence_to_support_office(self):
        application = self._plantilla_session_awaiting_ratings()
        mark_interview_absent(
            application,
            self.secretariat,
            notes="Candidate did not attend the scheduled sitting.",
        )
        exception = get_pending_attendance_exception(application)

        self.assertTrue(user_can_manage_attendance_exception(self.secretariat, exception))
        self.assertFalse(user_can_manage_attendance_exception(self.hrmpsb, exception))
        with self.assertRaisesMessage(ValueError, "not currently assigned to you"):
            record_attendance_letter_of_intent(
                exception,
                self.hrmpsb,
                SimpleUploadedFile(
                    "unauthorized-letter.pdf",
                    b"%PDF-1.4 unauthorized",
                    content_type="application/pdf",
                ),
            )

        filed = record_attendance_letter_of_intent(
            exception,
            self.secretariat,
            SimpleUploadedFile(
                "letter-of-intent.pdf",
                b"%PDF-1.4 letter",
                content_type="application/pdf",
            ),
            notes="Received by HR support.",
        )
        self.assertIsNotNone(filed.letter_of_intent_id)
        resolved = resolve_attendance_absence(
            exception,
            self.secretariat,
            AttendanceException.Outcome.RESCHEDULED,
            "The submitted letter supports another sitting.",
        )
        self.assertEqual(resolved.outcome, AttendanceException.Outcome.RESCHEDULED)

    def test_policy_can_return_plantilla_interview_absence_to_stage_owner(self):
        policy = get_office_policy()
        policy.interview_absence_decided_by = (
            OfficePolicy.InterviewAbsenceDecisionOwner.PANEL
        )
        policy.save(update_fields=["interview_absence_decided_by", "updated_at"])
        application = self._plantilla_session_awaiting_ratings()
        mark_interview_absent(
            application,
            self.secretariat,
            notes="Candidate did not attend the scheduled sitting.",
        )
        exception = get_pending_attendance_exception(application)

        self.assertFalse(user_can_manage_attendance_exception(self.secretariat, exception))
        self.assertTrue(user_can_manage_attendance_exception(self.hrmpsb, exception))
        with self.assertRaisesMessage(ValueError, "not currently assigned to you"):
            resolve_attendance_absence(
                exception,
                self.secretariat,
                AttendanceException.Outcome.RESCHEDULED,
                "Support office should not decide under panel ownership.",
            )
        filed = record_attendance_letter_of_intent(
            exception,
            self.hrmpsb,
            SimpleUploadedFile(
                "panel-letter.pdf", b"%PDF-1.4 panel", content_type="application/pdf"
            ),
        )
        self.assertIsNotNone(filed.letter_of_intent_id)

    def test_default_policy_lets_owning_office_record_pre_completion_withdrawal(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        application.refresh_from_db()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertTrue(user_can_record_withdrawal(self.secretariat, application))
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                application,
                self.secretariat,
                "The applicant sent the withdrawal request to the owning office.",
            )
        application.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.WITHDRAWN)

    def test_policy_can_limit_withdrawal_recording_to_the_current_stage_owner(self):
        policy = get_office_policy()
        policy.withdrawal_recorded_by_owning_office = False
        policy.save(update_fields=["withdrawal_recorded_by_owning_office", "updated_at"])
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        application.refresh_from_db()

        self.assertFalse(user_can_record_withdrawal(self.secretariat, application))
        self.assertTrue(user_can_record_withdrawal(self.hrmpsb, application))
        with self.assertRaisesMessage(ValueError, "not currently assigned to you"):
            record_application_withdrawal(
                application,
                self.secretariat,
                "The applicant wrote to the owning office.",
            )

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
                # Re-encoding the same member is a deliberate replacement.
                replace_existing=index > 0,
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
