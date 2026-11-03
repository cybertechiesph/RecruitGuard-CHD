"""Changes from the November 2026 office reviews (Secretariat walkthrough, Round A,
Round B, Round C from the HRMPSB Member walkthrough and Round D from the Appointing
Authority walkthrough)."""

from decimal import Decimal
from datetime import datetime, time, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from ..forms import RecruitmentEntryForm, OfficePolicyForm
from ..models import (
    AuditLog,
    ComparativeAssessmentReport,
    CosSelectionRound,
    ExamRecord,
    ExamSchedule,
    FinalSelection,
    CompetencyRatingTemplate,
    InterviewRating,
    Notification,
    NotificationLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..services import (
    generate_comparative_assessment_report,
    get_current_workflow_section,
    get_office_policy,
    get_queue_for_user,
    save_cos_vacancy_deliberation,
    get_competency_rating_template,
    save_interview_session,
    submit_application,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    save_vacancy_interview_schedule,
    save_vacancy_cos_interview_ratings,
    finalize_vacancy_interview_batch,
    upload_interview_fallback_rating,
    record_application_withdrawal,
    get_interview_session,
    hand_off_vacancy_batch,
    process_workflow_action,
    record_no_appointment,
    rescind_final_selection,
    save_interview_rating,
    user_can_record_withdrawal,
    user_can_send_vacancy_batch,
)
from ..notification_services import resend_notification
from ..schedule_services import EventKind, get_schedule_events_for_user
from .base import BaseRecruitmentTestCase, BatchExamHelpers, InterviewSessionHelpers


class SecretariatEssentialsTests(BatchExamHelpers, BaseRecruitmentTestCase):
    """The six essential fixes from docs/secretariat-walkthrough-2026-11.md."""

    enforce_event_held = True

    def _not_yet(self):
        # Setup steps that record results "afterwards" use the suite-wide stand-in.
        return patch("recruitment.services.clock.event_not_held_yet", return_value=False)

    def _plantilla_at_interview(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self._not_yet(), self.captureOnCommitCallbacks(execute=True):
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

    # P1-2: results wait for the sitting
    def test_exam_results_cannot_be_finalized_before_the_exam(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        scores = {
            first.case.id: {"general_score": "90", "technical_score": "80"},
            second.case.id: {"general_score": "85", "technical_score": "75"},
        }
        with self.assertRaisesMessage(ValueError, "The exam is scheduled for"):
            save_vacancy_exam_scores(self.level1_position, self.secretariat, scores, finalize=True)
        # Saving them as a draft is fine; once the sitting has passed, finalize works.
        save_vacancy_exam_scores(self.level1_position, self.secretariat, scores, finalize=False)
        ExamSchedule.objects.filter(application__in=[first, second]).update(
            scheduled_for=timezone.now() - timedelta(hours=1)
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(self.level1_position, self.secretariat, scores, finalize=True)
        first.refresh_from_db()
        self.assertEqual(first.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

    def test_interview_cannot_be_finalized_before_it_takes_place(self):
        self._plantilla_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.level1_position,
                self.secretariat,
                {"scheduled_for": timezone.now() + timedelta(days=2), "location": "Board Room", "session_notes": ""},
            )
        with self.assertRaisesMessage(ValueError, "The interview is scheduled for"):
            finalize_vacancy_interview_batch(self.level1_position, self.secretariat)

    # P1-3: the sent schedule is visible; a second send is a marked change
    def test_a_changed_exam_schedule_is_shown_and_marked_as_a_change(self):
        first, _second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        page = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertContains(page, "data-current-sitting")
        self.assertContains(page, "Change the schedule")
        moved = dict(self._schedule_payload(), scheduled_for=timezone.now() + timedelta(hours=5), venue="Room 2")
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, moved)
        subjects = list(
            NotificationLog.objects.filter(
                application=first, notification_type=NotificationLog.NotificationType.EXAM_INVITATION
            ).order_by("created_at").values_list("subject", flat=True)
        )
        self.assertEqual(len(subjects), 2)
        self.assertIn("exam invitation", subjects[0])
        self.assertIn("exam schedule changed", subjects[1])
        self.assertEqual(ExamSchedule.objects.filter(application=first).count(), 1)

    def test_staff_notices_and_past_invitations_cannot_be_resent(self):
        first, _second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        invitation = NotificationLog.objects.get(
            application=first, notification_type=NotificationLog.NotificationType.EXAM_INVITATION
        )
        staff_notice = NotificationLog.objects.create(
            application=first,
            notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
            recipient_email="panel@example.com",
            subject="Interview scheduled",
            body="Internal notice.",
        )
        with self.assertRaisesMessage(ValueError, "Only emails sent to the applicant"):
            resend_notification(staff_notice, self.secretariat)
        invitation.metadata = dict(
            invitation.metadata, scheduled_for=(timezone.now() - timedelta(days=1)).isoformat()
        )
        invitation.save(update_fields=["metadata"])
        with self.assertRaisesMessage(ValueError, "already passed"):
            resend_notification(invitation, self.secretariat)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        page = client.get(reverse("application-detail", args=[first.pk]))
        self.assertNotIn(staff_notice, page.context["notification_log_entries"])

    # P1-1: the panel never sees who a candidate is
    def test_the_panel_never_sees_the_applicants_identity(self):
        first, _second = self._plantilla_at_interview()
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.level1_position,
                self.secretariat,
                {"scheduled_for": timezone.now() + timedelta(hours=3), "location": "Board Room", "session_notes": ""},
            )
        first.refresh_from_db()
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        secrets = [first.applicant_display_name, first.applicant_email, first.reference_number]
        for url in (
            reverse("vacancy-batch-detail", args=[self.level1_position.pk]),
            reverse("vacancy-batch-interview", args=[self.level1_position.pk]),
            reverse("application-detail", args=[first.pk]),
            reverse("notification-list"),
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 200, url)
            for secret in secrets:
                self.assertNotContains(response, secret, msg_prefix=url)
        for title, body in Notification.objects.filter(recipient=self.hrmpsb).values_list("title", "body"):
            for secret in secrets:
                self.assertNotIn(secret, title + body)

    # P1-4: the HRM Chief's COS re-check reuses the Secretariat's sitting
    def test_cos_chief_recheck_reuses_the_sitting_and_offers_the_scores(self):
        first, second = self._two_qualified(self.cos_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._schedule_payload())
        scores = {
            first.case.id: {"general_score": "82", "technical_score": "80"},
            second.case.id: {"general_score": "78", "technical_score": "74"},
        }
        with self._not_yet(), self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(self.cos_position, self.secretariat, scores, finalize=True)
        for application in (first, second):
            application.refresh_from_db()
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
        invitations = NotificationLog.objects.filter(
            notification_type=NotificationLog.NotificationType.EXAM_INVITATION
        )
        sent_before = invitations.count()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        page = client.get(reverse("vacancy-batch-exam", args=[self.cos_position.pk]))
        self.assertContains(page, "no new schedule is needed")
        self.assertContains(page, "data-copy-prior-scores")
        self.assertContains(page, "Earlier review: 82")
        self.assertNotContains(page, "Send the exam schedule to the batch (section 1) before finalizing")
        with self._not_yet(), self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(self.cos_position, self.hrm_chief, scores, finalize=True)
        self.assertEqual(invitations.count(), sent_before)

    # P1-5: one open posting per plantilla item
    def test_a_plantilla_item_can_only_be_open_once(self):
        from django.db import IntegrityError, transaction as db_transaction

        with self.assertRaises(IntegrityError), db_transaction.atomic():
            PositionPosting.objects.create(
                position_reference=self.admin_aide_position,
                title="Administrative Aide VI",
                branch=PositionPosting.Branch.PLANTILLA,
                level=PositionPosting.Level.LEVEL_1,
                item_number=self.level1_position.item_number,
                status=PositionPosting.EntryStatus.ACTIVE,
                intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
                opening_date=timezone.localdate(),
                closing_date=timezone.localdate() + timedelta(days=14),
                job_code="VAC-PLT-2026-990",
            )
        form = RecruitmentEntryForm(
            data={
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "item_number": self.level1_position.item_number.lower(),
                "opening_date": timezone.localdate().isoformat(),
                "closing_date": (timezone.localdate() + timedelta(days=14)).isoformat(),
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("already being filled", str(form.errors["item_number"]))

    # P1-6: a portal withdrawal request is visible where the office works
    def test_a_withdrawal_request_is_flagged_on_queue_and_batch_lists(self):
        first, _second = self._two_qualified(self.level1_position)
        RecruitmentApplication.objects.filter(pk=first.pk).update(
            withdrawal_requested_at=timezone.now(), withdrawal_request_reason="Took another job."
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        for url in (
            reverse("workflow-queue"),
            reverse("vacancy-batch-detail", args=[self.level1_position.pk]),
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
        ):
            response = client.get(url)
            self.assertContains(response, "data-flag-withdrawal-requested", msg_prefix=url)
        page = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertContains(page, "asked to withdraw")


class SecretariatImprovementsTests(InterviewSessionHelpers, BatchExamHelpers, BaseRecruitmentTestCase):
    """The P2/P3 round from docs/secretariat-walkthrough-2026-11.md."""

    def _sheet(self, name="sheet.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 sheet", content_type="application/pdf")

    def _staff_client(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _submitted(self, position):
        application = self.make_application(position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    # Archive: past records are one click away
    def test_past_records_is_in_the_secretariat_sidebar(self):
        client = self._staff_client(self.secretariat)
        page = client.get(reverse("workflow-queue"))
        self.assertContains(page, f'href="{reverse("workflow-closed")}"')
        self.assertContains(page, "Past Records")
        closed = client.get(reverse("workflow-closed"))
        self.assertContains(closed, "<title>Past Records | Internal Portal</title>")
        self.assertContains(closed, "?status=closed")

    # P2-3: a paper sheet replaces an earlier one only on purpose
    def test_a_second_paper_sheet_for_the_same_member_needs_the_replace_box(self):
        application = self._plantilla_session_awaiting_ratings()
        upload_interview_fallback_rating(
            application=application, actor=self.secretariat, uploaded_file=self._sheet(),
            remarks="", rated_by=self.hrmpsb, rating_score=Decimal("80.00"),
        )
        with self.assertRaisesMessage(ValueError, "already has a paper sheet on record (rating 80.00)"):
            upload_interview_fallback_rating(
                application=application, actor=self.secretariat, uploaded_file=self._sheet("b.pdf"),
                remarks="", rated_by=self.hrmpsb, rating_score=Decimal("85.00"),
            )
        upload_interview_fallback_rating(
            application=application, actor=self.secretariat, uploaded_file=self._sheet("c.pdf"),
            remarks="", rated_by=self.hrmpsb, rating_score=Decimal("85.00"), replace_existing=True,
        )
        session = get_interview_session(application)
        self.assertEqual(session.fallback_ratings.count(), 1)
        self.assertEqual(session.fallback_ratings.get().rating_score, Decimal("85.00"))

    def test_an_invalid_paper_sheet_says_what_is_wrong(self):
        application = self._plantilla_session_awaiting_ratings()
        client = self._staff_client(self.secretariat)
        response = client.post(
            reverse("interview-fallback-upload", kwargs={"pk": application.pk}),
            {"rated_by": self.hrmpsb.pk, "rating_score": "150", "uploaded_file": self._sheet()},
            follow=True,
        )
        self.assertContains(response, "The rating sheet wasn&#x27;t saved:")
        self.assertFalse(get_interview_session(application).fallback_ratings.exists())

    # P2-5: no publishing a rating sheet without the position's own competencies
    def test_a_rating_sheet_without_technical_competencies_cannot_be_published(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        client = self._staff_client(self.secretariat)
        url = reverse("interview-rating-sheet", kwargs={"pk": application.pk})
        client.post(url, {"operation": "create"})
        template = get_competency_rating_template(self.level1_position)
        existing = list(template.competencies.order_by("order"))
        data = {
            "operation": "publish",
            "scale_max": "4",
            "instructions": "Score each competency.",
            "competencies-TOTAL_FORMS": str(len(existing)),
            "competencies-INITIAL_FORMS": str(len(existing)),
            "competencies-MIN_NUM_FORMS": "0",
            "competencies-MAX_NUM_FORMS": "1000",
        }
        for index, competency in enumerate(existing):
            data[f"competencies-{index}-id"] = str(competency.id)
            data[f"competencies-{index}-group"] = competency.group
            data[f"competencies-{index}-name"] = competency.name
            data[f"competencies-{index}-weight"] = "1.00"
        response = client.post(url, data, follow=True)
        self.assertContains(response, "Add at least one Technical competency")
        template.refresh_from_db()
        self.assertEqual(template.status, CompetencyRatingTemplate.Status.DRAFT)
        page = client.get(url)
        self.assertContains(page, "<title>Interview Rating Sheet | Internal Portal</title>")

    # P2-4: the version people see counts finalized reports, not drafts
    def test_car_drafts_do_not_raise_the_official_version(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)
        for note in ("First draft.", "Second draft."):
            report = generate_comparative_assessment_report(
                application=application, actor=self.secretariat,
                cleaned_data={"summary_notes": note}, finalize=False,
            )
        self.assertEqual(report.version_number, 2)
        self.assertIsNone(report.official_version)
        ComparativeAssessmentReport.objects.filter(pk=report.pk).update(is_finalized=True)
        report.refresh_from_db()
        self.assertEqual(report.official_version, 1)

    def test_ete_scores_can_be_entered_before_the_first_car_draft(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)
        client = self._staff_client(self.secretariat)
        page = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertContains(page, f'name="ete_education_{application.case.id}"')
        payload = {"operation": "save", "summary_notes": ""}
        for part in ("education", "training", "experience"):
            payload[f"ete_{part}_{application.case.id}"] = "80"
            payload[f"ete_{part}_basis_{application.case.id}"] = "Meets the standard."
        client.post(reverse("comparative-assessment-report", kwargs={"pk": application.pk}), payload)
        report = ComparativeAssessmentReport.objects.get(recruitment_entry=self.level1_position)
        item = report.items.get(recruitment_case=application.case)
        self.assertIsNotNone(item.assessment_score)
        self.assertEqual(item.rank_order, 1)

    # Only the office running the interview schedules it
    def test_interview_schedule_form_is_only_for_the_scheduling_office(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_applicant_pool_for_test(self.level1_position)
        url = reverse("vacancy-batch-interview", args=[self.level1_position.pk])
        sec_page = self._staff_client(self.secretariat).get(url)
        self.assertContains(sec_page, 'id="interviewScheduleForm"')
        psb_page = self._staff_client(self.hrmpsb).get(url)
        if psb_page.status_code == 200:
            self.assertNotContains(psb_page, 'id="interviewScheduleForm"')

    # P2-7: each branch shows its own steps
    def test_progress_bar_follows_the_branch(self):
        plantilla = self._submitted(self.level1_position)
        cos = self._submitted(self.cos_position)
        client = self._staff_client(self.secretariat)
        cos_page = client.get(reverse("application-detail", kwargs={"pk": cos.pk}))
        self.assertContains(cos_page, '<span class="rg-pipeline__label">HRM Chief re-check</span>')
        self.assertContains(cos_page, '<span class="rg-pipeline__label">Selection</span>')
        self.assertNotContains(cos_page, '<span class="rg-pipeline__label">Decision</span>')
        plantilla_page = client.get(reverse("application-detail", kwargs={"pk": plantilla.pk}))
        self.assertContains(plantilla_page, '<span class="rg-pipeline__label">Decision</span>')
        self.assertNotContains(plantilla_page, "HRM Chief re-check")

    def test_a_withdrawn_case_shows_where_it_ended(self):
        application = self._submitted(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(application, self.secretariat, "The applicant took another job.")
        page = self._staff_client(self.secretariat).get(
            reverse("application-detail", kwargs={"pk": application.pk})
        )
        self.assertContains(page, "Withdrawn at Screening")
        self.assertContains(page, "rg-pipeline__stage is-ended")
        self.assertNotContains(page, "Step 6 of 6")

    # P2-8: Completion has its own two-week target; the deadline does not drift
    def test_completion_uses_the_two_week_target(self):
        case = self._submitted(self.level1_position).case
        case.current_stage = RecruitmentCase.Stage.COMPLETION
        for days, expected in ((8, "ok"), (11, "warning"), (15, "overdue")):
            case.stage_entered_at = timezone.now() - timedelta(days=days)
            self.assertEqual(case.stage_sla_state, expected, days)
        case.current_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        case.stage_entered_at = timezone.now() - timedelta(days=8)
        self.assertEqual(case.stage_sla_state, "overdue")
        self.assertEqual(case.stage_sla_context["overdue_days"], 7)

    def test_completion_deadline_counts_from_the_selection(self):
        from ..forms import CompletionTrackingForm

        application = self._submitted(self.level1_position)
        selected_on = timezone.now() - timedelta(days=20)
        RecruitmentCase.objects.filter(pk=application.case.pk).update(
            current_stage=RecruitmentCase.Stage.COMPLETION, stage_entered_at=selected_on
        )
        application = RecruitmentApplication.objects.get(pk=application.pk)
        form = CompletionTrackingForm(application=application, actor=self.secretariat)
        self.assertEqual(
            form.fields["deadline"].initial,
            timezone.localdate(selected_on) + timedelta(days=14),
        )

    # P2-9: closed vacancies are listed on request; lapsed ones are flagged
    def test_entries_list_filters_and_flags_a_lapsed_deadline(self):
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=timezone.localdate() - timedelta(days=1)
        )
        PositionPosting.objects.filter(pk=self.cos_position.pk).update(
            status=PositionPosting.EntryStatus.CLOSED
        )
        client = self._staff_client(self.secretariat)
        open_list = client.get(reverse("recruitment-entry-list"))
        self.assertContains(open_list, "data-intake-deadline-passed")
        self.assertContains(open_list, "Close intake")
        self.assertNotContains(open_list, self.cos_position.job_code)
        closed_list = client.get(reverse("recruitment-entry-list") + "?status=closed")
        self.assertContains(closed_list, self.cos_position.job_code)
        self.assertNotContains(closed_list, self.level1_position.job_code)
        all_list = client.get(reverse("recruitment-entry-list") + "?status=all")
        self.assertContains(all_list, self.cos_position.job_code)
        self.assertContains(all_list, self.level1_position.job_code)

    # P3: one problem, one message on the entry form
    def test_entry_form_reports_each_problem_once(self):
        form = RecruitmentEntryForm(
            data={
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.COS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "opening_date": timezone.localdate().isoformat(),
                "closing_date": (timezone.localdate() + timedelta(days=14)).isoformat(),
            }
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.errors["intake_mode"]), 1)


class SecretariatRoundATests(BatchExamHelpers, BaseRecruitmentTestCase):
    """Round A from docs/secretariat-final-review-2026-11.md (F1-F6)."""

    def _staff_client(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _batch_exam_scores(self, applications, general, technical, operation="save_scores"):
        data = {"operation": operation}
        for application in applications:
            data[f"general_score_{application.case.id}"] = general
            data[f"technical_score_{application.case.id}"] = technical
        return data

    # F2: a draft score is held to the same 0-100 rule as a final one
    def test_batch_exam_draft_scores_outside_0_to_100_are_refused(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = self._staff_client(self.secretariat)
        url = reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        for bad in ("101", "-1"):
            response = client.post(url, self._batch_exam_scores([first, second], bad, "80"), follow=True)
            self.assertContains(response, "General Ability score must be between 0 and 100", msg_prefix=bad)
        self.assertFalse(ExamRecord.objects.filter(application__in=[first, second]).exists())

    def test_batch_exam_non_numeric_score_is_a_message_not_a_crash(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = self._staff_client(self.secretariat)
        url = reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        response = client.post(url, self._batch_exam_scores([first, second], "abc", "80"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "General Ability score must be a number from 0 to 100")
        self.assertFalse(ExamRecord.objects.filter(application__in=[first, second]).exists())

    # F3: the exam date is the Manila calendar day of the sitting
    def test_batch_exam_date_is_the_local_day_of_an_early_sitting(self):
        first, second = self._two_qualified(self.level1_position)
        tomorrow = timezone.localdate() + timedelta(days=1)
        # 07:00 in Manila is 23:00 the previous day in UTC.
        early = timezone.make_aware(datetime.combine(tomorrow, time(7, 0)))
        self.assertNotEqual(early.astimezone(dt_timezone.utc).date(), tomorrow)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position,
                self.secretariat,
                dict(self._schedule_payload(), scheduled_for=early),
            )
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
        for application in (first, second):
            record = ExamRecord.objects.get(application=application)
            self.assertEqual(record.exam_date, tomorrow)

    # F4: a simultaneous second save is a plain message, not an error page
    def test_a_conflicting_save_is_reported_not_crashed(self):
        from django.db import IntegrityError

        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        client = self._staff_client(self.secretariat)
        case_url = reverse("application-detail", args=[application.pk])
        data = {"operation": "finalize", "qualification_outcome": "qualified", "screening_notes": "", "completeness_notes": ""}
        data.update(self.screening_document_status_payload(application))
        with patch(
            "recruitment.views.screening.save_screening_review",
            side_effect=IntegrityError("duplicate key value violates unique constraint"),
        ):
            response = client.post(
                reverse("screening-review", args=[application.pk]),
                data,
                HTTP_REFERER=f"http://testserver{case_url}",
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], case_url)
        page = client.get(case_url)
        self.assertContains(page, "Someone else saved this step at the same moment")

    def test_workflow_transitions_lock_their_rows_first(self):
        from recruitment import services

        for name in (
            "save_screening_review",
            "return_screening_documents_to_applicant",
            "save_exam_record",
            "save_vacancy_exam_schedule",
            "save_vacancy_exam_scores",
            "save_interview_session",
            "save_vacancy_interview_schedule",
            "finalize_vacancy_interview_batch",
            "generate_comparative_assessment_report",
            "record_final_selection",
            "record_no_appointment",
            "save_completion_tracking",
            "close_recruitment_case",
            "transition_case",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    getattr(getattr(services, name), "locks_workflow_row", False),
                    f"{name} must take its row lock before reading state",
                )

    # F5: a double click on "Send again" sends one email
    def test_send_again_twice_in_a_row_queues_one_email(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        original = NotificationLog.objects.filter(
            application=application,
            notification_type=NotificationLog.NotificationType.SUBMISSION_ACKNOWLEDGMENT,
        ).first()
        client = self._staff_client(self.secretariat)
        url = reverse("notification-resend", args=[application.pk, original.pk])
        with self.captureOnCommitCallbacks(execute=True):
            client.post(url)
        with self.captureOnCommitCallbacks(execute=True):
            second = client.post(url, follow=True)
        resent = NotificationLog.objects.filter(
            application=application, metadata__resent_from=original.pk
        )
        self.assertEqual(resent.count(), 1)
        self.assertContains(second, "was just sent again")

    # F6: the panel's interview email is blind like its pages
    def test_panel_interview_email_shows_candidate_codes_not_names(self):
        from recruitment.notification_services import _render_notification_html

        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
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
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                self.level1_position,
                self.secretariat,
                {
                    "scheduled_for": timezone.now() + timedelta(days=2),
                    "location": "HRMPSB Board Room",
                    "session_notes": "",
                },
            )
        panel_logs = NotificationLog.objects.filter(
            notification_type=NotificationLog.NotificationType.INTERVIEW_SESSION_SCHEDULED,
            application__position=self.level1_position,
        )
        self.assertTrue(panel_logs.exists())
        for log in panel_logs:
            html = _render_notification_html(log)
            for text in (log.subject, log.body, html):
                for application in (first, second):
                    self.assertNotIn(application.applicant_display_name, text)
                    self.assertNotIn(application.applicant_first_name, text)
                    self.assertNotIn(application.reference_number, text)
            for application in (first, second):
                self.assertIn(application.candidate_code_label, log.body)
                self.assertIn(application.candidate_code_label, html)
            self.assertIn("HRMPSB Board Room", html)

    # F1: the panel reviews blind, so no file that carries the applicant's name
    def test_hrmpsb_member_cannot_download_applicant_documents(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        document = application.evidence_items.filter(
            artifact_type="applicant_document", is_current_version=True
        ).first()
        url = reverse("evidence-download", args=[application.pk, document.pk])

        response = self._staff_client(self.hrmpsb).get(url)

        self.assertEqual(response.status_code, 403)
        denial = AuditLog.objects.get(
            actor=self.hrmpsb,
            action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
            metadata__evidence_id=document.id,
        )
        self.assertEqual(denial.metadata["reason"], "applicant_file_restricted_for_hrmpsb")
        self.assertFalse(
            AuditLog.objects.filter(
                actor=self.hrmpsb,
                action=AuditLog.Action.EVIDENCE_DOWNLOADED,
                metadata__evidence_id=document.id,
            ).exists()
        )
        # The Secretariat, who screened it, still can.
        self.assertEqual(self._staff_client(self.secretariat).get(url).status_code, 200)


class RoundBTests(BatchExamHelpers, BaseRecruitmentTestCase):
    """Round B from docs/hrm-chief-review-2026-11.md section 8."""

    def _staff_client(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _open_pool(self):
        # Medical Officer V is a Level 2 reference, so the pool routes to the HRM Chief.
        return PositionPosting.objects.create(
            position_reference=self.medical_officer_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_2,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.ACTIVE,
        )

    def _cos_l2_to_deliberation(self, posting, applications):
        """Drive a Level 2 open-pool batch to the HRM Chief's deliberation, the batch way."""
        for application in applications:
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(posting, self.hrm_chief, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                posting,
                self.hrm_chief,
                {a.case.id: {"general_score": "85", "technical_score": "80"} for a in applications},
                finalize=True,
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_interview_schedule(
                posting,
                self.hrm_chief,
                {"scheduled_for": timezone.now() + timedelta(days=2), "location": "Chief's office", "session_notes": ""},
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_cos_interview_ratings(
                posting,
                self.hrm_chief,
                {a.case.id: {"rating_score": "88"} for a in applications},
                finalize=True,
            )
        for application in applications:
            application.refresh_from_db()

    # C1: "pooling" is gone; COS chooses between an open pool and one batch
    def test_cos_intake_choices_are_open_pool_and_one_batch(self):
        values = dict(PositionPosting.IntakeMode.choices)
        self.assertNotIn("pooling", values)
        self.assertEqual(values["continuous"], "Open Pool")

    def test_a_one_batch_cos_vacancy_needs_a_closing_date(self):
        form = RecruitmentEntryForm(
            data={
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "intake_mode": PositionPosting.IntakeMode.OPENING_BASED,
                "opening_date": timezone.localdate().isoformat(),
                "closing_date": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("closing date", str(form.errors).lower())

    def test_open_pool_batch_exam_does_not_wait_for_a_newcomer(self):
        posting = self._open_pool()
        first = self._extra_submitted_application(posting, "1")
        second = self._extra_submitted_application(posting, "2")
        for application in (first, second):
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
        newcomer = self._extra_submitted_application(posting, "3")
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(posting, self.hrm_chief, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                posting,
                self.hrm_chief,
                {a.case.id: {"general_score": "85", "technical_score": "80"} for a in (first, second)},
                finalize=True,
            )
        for application in (first, second):
            self.assertTrue(ExamRecord.objects.get(application=application).is_finalized)
        newcomer.refresh_from_db()
        self.assertEqual(get_current_workflow_section(newcomer), "screening")

    def test_open_pool_keeps_unpicked_candidates_and_picks_a_later_batch_in_round_two(self):
        posting = self._open_pool()
        first = self._extra_submitted_application(posting, "1")
        second = self._extra_submitted_application(posting, "2")
        self._cos_l2_to_deliberation(posting, [first, second])
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(posting, self.hrm_chief, first.case.id, "Best scores.", finalize=True)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        # The pool stays: the candidate not picked waits for the next slot.
        self.assertEqual(second.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        later = self._extra_submitted_application(posting, "3")
        self._cos_l2_to_deliberation(posting, [later])
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(posting, self.hrm_chief, later.case.id, "Second slot.", finalize=True)
        later.refresh_from_db()
        self.assertEqual(later.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(CosSelectionRound.objects.filter(recruitment_entry=posting).count(), 2)

    def test_pooling_vacancies_migrate_to_open_pool(self):
        import importlib
        from django.apps import apps as global_apps

        migration = importlib.import_module("recruitment.migrations.0092_merge_pooling_into_open_pool")
        posting = self._open_pool()
        PositionPosting.objects.filter(pk=posting.pk).update(intake_mode="pooling")
        migration.merge_pooling_into_continuous(global_apps, None)
        posting.refresh_from_db()
        self.assertEqual(posting.intake_mode, PositionPosting.IntakeMode.CONTINUOUS)

    # C2: the HRM Chief owns Level 2 and follows it, read-only, through the CAR and decision
    def _level_two_at_car(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_applicant_pool_for_test(self.level2_position)
        application.refresh_from_db()
        return application

    def test_chief_follows_a_level_two_case_at_the_car_read_only(self):
        application = self._level_two_at_car()
        self.assertEqual(get_current_workflow_section(application), "car")
        chief = self._staff_client(self.hrm_chief)
        case_url = reverse("application-detail", args=[application.pk])
        self.assertEqual(chief.get(case_url).status_code, 200)
        self.assertEqual(chief.get(reverse("application-export", args=[application.pk])).status_code, 200)
        self.assertEqual(
            chief.get(reverse("vacancy-batch-detail", args=[self.level2_position.pk])).status_code, 200
        )
        response = chief.post(
            reverse("comparative-assessment-report", args=[application.pk]),
            {"operation": "save", "summary_notes": "Chief tries to prepare."},
        )
        self.assertEqual(response.status_code, 403)

    def test_chief_follows_a_level_two_case_at_the_decision_read_only(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)
        chief = self._staff_client(self.hrm_chief)
        self.assertEqual(chief.get(reverse("application-detail", args=[application.pk])).status_code, 200)
        response = chief.post(
            reverse("final-selection-record", args=[application.pk]),
            {"decision_notes": "Chief tries to decide."},
        )
        self.assertEqual(response.status_code, 403)

    def test_level_two_completion_shows_the_car_summary(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(application, self.appointing)
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        page = self._staff_client(self.hrm_chief).get(reverse("application-detail", args=[application.pk]))
        self.assertContains(page, "data-car-summary")
        self.assertContains(page, application.applicant_display_name)

    def test_level_two_interview_finalize_message_names_the_secretariat(self):
        from recruitment.views import _interview_finalize_next_step

        self.assertIn("Secretariat", _interview_finalize_next_step(self.level2_position, self.hrm_chief))
        self.assertIn("prepare the Comparative Assessment Report", _interview_finalize_next_step(self.level1_position, self.secretariat))

    # C3: a filled vacancy can't be re-opened for applications by accident
    def test_a_decided_vacancy_cannot_be_reactivated(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(application, self.appointing)
        self.level2_position.refresh_from_db()
        PositionPosting.objects.filter(pk=self.level2_position.pk).update(status=PositionPosting.EntryStatus.CLOSED)
        chief = self._staff_client(self.hrm_chief)
        response = chief.post(
            reverse("recruitment-entry-status", args=[self.level2_position.pk, "active"]), follow=True
        )
        self.assertContains(response, "selection has already been recorded")
        self.level2_position.refresh_from_db()
        self.assertEqual(self.level2_position.status, PositionPosting.EntryStatus.CLOSED)
        closed_list = chief.get(reverse("recruitment-entry-list") + "?status=closed")
        self.assertNotContains(
            closed_list, reverse("recruitment-entry-status", args=[self.level2_position.pk, "active"])
        )

    def test_a_suspended_open_pool_with_a_pick_can_be_reactivated(self):
        posting = self._open_pool()
        first = self._extra_submitted_application(posting, "1")
        self._cos_l2_to_deliberation(posting, [first])
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(posting, self.hrm_chief, first.case.id, "Filled one slot.", finalize=True)
        PositionPosting.objects.filter(pk=posting.pk).update(status=PositionPosting.EntryStatus.SUSPENDED)
        self._staff_client(self.hrm_chief).post(reverse("recruitment-entry-status", args=[posting.pk, "active"]))
        posting.refresh_from_db()
        self.assertEqual(posting.status, PositionPosting.EntryStatus.ACTIVE)

    # C4 + F9: the Secretariat and the HRM Chief lend each other whole batches
    def _two_screened(self, position, reviewer):
        first = self._extra_submitted_application(position, "h1")
        second = self._extra_submitted_application(position, "h2")
        for application in (first, second):
            self.finalize_screening_for_current_stage(application, reviewer)
            application.refresh_from_db()
        return first, second

    def _batch_handoff(self, client, position, operation, remarks="Covering this batch while I'm away."):
        return client.post(
            reverse("vacancy-batch-handoff", args=[position.pk]),
            {"operation": operation, "remarks": remarks},
            follow=True,
        )

    def test_secretariat_lends_the_whole_batch_and_keeps_sight_of_it(self):
        first, second = self._two_screened(self.level1_position, self.secretariat)
        sec = self._staff_client(self.secretariat)
        response = self._batch_handoff(sec, self.level1_position, "send")
        self.assertContains(response, "2 cases")
        for application in (first, second):
            application.refresh_from_db()
            self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        page = sec.get(reverse("application-detail", args=[first.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "data-case-on-loan")
        batch = sec.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertContains(batch, "Recall the batch")

    def test_the_lending_office_can_recall_the_batch(self):
        first, second = self._two_screened(self.level1_position, self.secretariat)
        sec = self._staff_client(self.secretariat)
        self._batch_handoff(sec, self.level1_position, "send")
        self._batch_handoff(sec, self.level1_position, "recall", remarks="Back from leave.")
        for application in (first, second):
            application.refresh_from_db()
            self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    def test_a_batch_waiting_on_corrected_documents_is_not_split(self):
        first, _second = self._two_screened(self.level1_position, self.secretariat)
        returned = self._extra_submitted_application(self.level1_position, "h3")
        data = {"operation": "return_to_applicant", "qualification_outcome": "", "screening_notes": "", "completeness_notes": "", "return_remarks": "Fix the PDS."}
        data.update(self.screening_document_status_payload(returned))
        code = next(k for k in data if k.startswith("document_status__") and "personal_data_sheet" in k)
        data[code] = "return_for_resubmission"
        data[code.replace("document_status__", "document_remarks__")] = "Unsigned PDS."
        with self.captureOnCommitCallbacks(execute=True):
            self._staff_client(self.secretariat).post(reverse("screening-review", args=[returned.pk]), data)
        response = self._batch_handoff(self._staff_client(self.secretariat), self.level1_position, "send")
        self.assertContains(response, "waiting for corrected documents")
        first.refresh_from_db()
        self.assertEqual(first.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    def test_the_chief_lends_a_level_two_batch_and_recalls_it(self):
        first, second = self._two_screened(self.level2_position, self.hrm_chief)
        chief = self._staff_client(self.hrm_chief)
        self._batch_handoff(chief, self.level2_position, "send")
        first.refresh_from_db()
        self.assertEqual(first.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(chief.get(reverse("application-detail", args=[first.pk])).status_code, 200)
        self._batch_handoff(chief, self.level2_position, "recall", remarks="Taking it back.")
        first.refresh_from_db()
        self.assertEqual(first.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertIsNone(first.active_secretariat_override)

    def test_single_case_handoff_is_retired(self):
        first, _second = self._two_screened(self.level1_position, self.secretariat)
        response = self._staff_client(self.secretariat).post(
            reverse("case-handoff", args=[first.pk]), {"target_role": "hrm_chief", "remarks": "x"}, follow=True
        )
        self.assertContains(response, "whole batch")
        first.refresh_from_db()
        self.assertEqual(first.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    # 4a: the HRMPSB panel is a standing core plus end-user members per vacancy
    def test_an_unconfigured_panel_is_every_member(self):
        from recruitment.services import user_is_on_vacancy_panel

        end_user, _outsider = self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        self.assertTrue(user_is_on_vacancy_panel(end_user, self.level1_position))

    def test_an_end_user_member_sees_only_the_vacancies_they_sit_on(self):
        from recruitment.services import (
            get_interview_schedule_notification_recipients,
            user_can_manage_interview_rating,
        )

        end_user, outsider = self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        get_office_policy().core_panel_members.set([self.hrmpsb])
        self.level1_position.panel_end_users.set([end_user])
        level1_case = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(level1_case)
        level2_case = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(level2_case)
        url = lambda application: reverse("application-detail", args=[application.pk])

        end_user_client = self._staff_client(end_user)
        self.assertEqual(end_user_client.get(url(level1_case)).status_code, 200)
        self.assertEqual(end_user_client.get(url(level2_case)).status_code, 404)
        self.assertEqual(self._staff_client(outsider).get(url(level1_case)).status_code, 404)
        core_client = self._staff_client(self.hrmpsb)
        self.assertEqual(core_client.get(url(level1_case)).status_code, 200)
        self.assertEqual(core_client.get(url(level2_case)).status_code, 200)

        queue = set(get_queue_for_user(end_user).values_list("pk", flat=True))
        self.assertIn(level1_case.pk, queue)
        self.assertNotIn(level2_case.pk, queue)
        self.assertFalse(user_can_manage_interview_rating(outsider, level1_case))
        self.assertEqual(
            {user.pk for user in get_interview_schedule_notification_recipients(level1_case)},
            {self.hrmpsb.pk, end_user.pk},
        )

    def test_the_rating_sheet_page_sets_the_vacancy_end_users(self):
        end_user, _outsider = self.extra_plantilla_panel_raters(exclude=self.hrmpsb)
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        response = self._staff_client(self.secretariat).post(
            reverse("interview-rating-sheet", args=[application.pk]),
            {"operation": "save_panel", "panel_end_users": [end_user.pk]},
            follow=True,
        )
        self.assertContains(response, "Panel saved")
        self.assertEqual(list(self.level1_position.panel_end_users.all()), [end_user])

    def test_office_policy_sets_the_core_panel(self):
        policy = get_office_policy()
        form = OfficePolicyForm(instance=policy)
        self.assertIn("core_panel_members", form.fields)
        data = {name: form.initial.get(name) for name in form.fields if name != "core_panel_members"}
        data = {key: value for key, value in data.items() if value not in (None, False)}
        data["core_panel_members"] = [self.hrmpsb.pk]
        bound = OfficePolicyForm(data=data, instance=policy)
        self.assertTrue(bound.is_valid(), bound.errors)
        bound.save()
        self.assertEqual(list(get_office_policy().core_panel_members.all()), [self.hrmpsb])

    # C5: a case page shows a pick only from a round this case was in
    def test_a_later_candidate_does_not_see_an_earlier_rounds_pick_as_theirs(self):
        posting = self._open_pool()
        first = self._extra_submitted_application(posting, "1")
        second = self._extra_submitted_application(posting, "2")
        self._cos_l2_to_deliberation(posting, [first, second])
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(posting, self.hrm_chief, first.case.id, "Best scores.", finalize=True)
        later = self._extra_submitted_application(posting, "3")
        self._cos_l2_to_deliberation(posting, [later])
        chief = self._staff_client(self.hrm_chief)
        later_page = chief.get(reverse("application-detail", args=[later.pk]))
        self.assertNotContains(later_page, first.applicant_display_name)
        member_page = chief.get(reverse("application-detail", args=[second.pk]))
        self.assertContains(member_page, first.applicant_display_name)

    # F10: a Plantilla vacancy goes Active only with its Qualification Standards on file
    def _plantilla_entry_data(self, **overrides):
        data = {
            "position_reference": self.admin_aide_position.pk,
            "branch": PositionPosting.Branch.PLANTILLA,
            "status": PositionPosting.EntryStatus.ACTIVE,
            "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
            "item_number": "OSEC-DOH-QS-9-2026",
            "opening_date": timezone.localdate().isoformat(),
            "closing_date": (timezone.localdate() + timedelta(days=14)).isoformat(),
            "qualification_reference": "",
        }
        data.update(overrides)
        return data

    def test_an_active_plantilla_entry_needs_its_qualification_standards(self):
        form = RecruitmentEntryForm(data=self._plantilla_entry_data())
        self.assertFalse(form.is_valid())
        self.assertIn("Education", str(form.errors["qualification_reference"]))
        complete = RecruitmentEntryForm(
            data=self._plantilla_entry_data(
                qualification_reference="Education: Completion of two years of college\nTraining: None required\nExperience: None required"
            )
        )
        self.assertTrue(complete.is_valid(), complete.errors)
        draft = RecruitmentEntryForm(data=self._plantilla_entry_data(status=PositionPosting.EntryStatus.DRAFT))
        self.assertNotIn("qualification_reference", draft.errors)

    def test_activating_a_plantilla_entry_without_qualification_standards_is_refused(self):
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            status=PositionPosting.EntryStatus.DRAFT, qualification_reference=""
        )
        response = self._staff_client(self.secretariat).post(
            reverse("recruitment-entry-status", args=[self.level1_position.pk, "active"]), follow=True
        )
        self.assertContains(response, "Qualification Standards")
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.DRAFT)

    # F7: the panel's interview card shows the earlier stage's results, not "Not recorded"
    def test_the_interview_card_shows_the_earlier_screening_and_exam(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        page = self._staff_client(self.hrmpsb).get(reverse("application-detail", args=[application.pk]))
        self.assertContains(page, "rg-int-prev-step")
        self.assertNotContains(page, "rg-int-prev-step__empty")

    # F8: a refused online rating says what is missing
    def test_a_rating_that_needs_a_justification_says_so(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        save_interview_session(
            application=application,
            actor=self.secretariat,
            cleaned_data={"scheduled_for": timezone.now() + timedelta(days=1), "location": "Board Room", "session_notes": ""},
            finalize=False,
        )
        payload = self.competency_rating_cleaned_data(application, level=4)
        data = {"rating_notes": "", "justification": ""}
        for competency in payload["competency_scores"]:
            data[f"score_{competency.id}"] = "4"
        response = self._staff_client(self.hrmpsb).post(
            reverse("interview-rating", args=[application.pk]), data, follow=True
        )
        self.assertContains(response, "justification")

    # F11: after screening one case, the next case that still needs screening is offered
    def test_after_a_screening_the_next_case_to_screen_is_offered(self):
        first = self._extra_submitted_application(self.level1_position, "n1")
        second = self._extra_submitted_application(self.level1_position, "n2")
        self.finalize_screening_for_current_stage(first, self.secretariat)
        page = self._staff_client(self.secretariat).get(
            reverse("application-detail", args=[first.pk]) + "?from=batch"
        )
        self.assertContains(page, "Next in this batch")
        self.assertContains(page, second.case_number_short)
        self.assertContains(page, "still in screening")

    # F12: the export keeps draft CARs apart from the official one
    def test_export_files_draft_cars_as_working_copies(self):
        import io as _io
        import zipfile as _zipfile

        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_applicant_pool_for_test(self.level1_position)
        self.ensure_ete_ratings_for_pool(application)
        generate_comparative_assessment_report(
            application=application, actor=self.secretariat, cleaned_data={"summary_notes": "Draft."}, finalize=False
        )
        self.finalize_car_for_current_stage(application, self.hrmpsb)
        response = self._staff_client(self.appointing).get(reverse("application-export", args=[application.pk]))
        self.assertEqual(response.status_code, 200)
        names = _zipfile.ZipFile(_io.BytesIO(response.content)).namelist()
        car_files = [name for name in names if "comparative_assessment_report" in name or "comparative-assessment-report" in name]
        self.assertTrue(any("working-copies/draft-" in name for name in car_files), car_files)
        self.assertTrue(any(name.endswith("car-official-version-1.pdf") for name in car_files), car_files)

    # F13: the database itself refuses an impossible score
    def test_the_database_refuses_scores_outside_0_to_100(self):
        from django.db import IntegrityError, transaction as db_transaction

        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        record = ExamRecord.objects.get(application=application)
        for field in ("general_score", "technical_score", "exam_score"):
            with self.subTest(field=field), self.assertRaises(IntegrityError), db_transaction.atomic():
                ExamRecord.objects.filter(pk=record.pk).update(**{field: 101})

    # F14: file logging with rotation and error mail, switched on by environment
    def test_logging_config_adds_file_and_error_mail_handlers_when_configured(self):
        from config.logging import build_logging_config

        plain = build_logging_config()
        self.assertEqual(set(plain["handlers"]), {"console"})
        full = build_logging_config(log_file="/var/log/recruitment/app.log", mail_admins=True)
        self.assertEqual(full["handlers"]["file"]["class"], "logging.handlers.RotatingFileHandler")
        self.assertIn("file", full["loggers"]["django.request"]["handlers"])
        self.assertIn("mail_admins", full["loggers"]["django.request"]["handlers"])
        self.assertEqual(full["handlers"]["mail_admins"]["level"], "ERROR")

    # P3: the rating sheet has a vacancy-level address
    def test_the_rating_sheet_opens_from_the_vacancy(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        response = self._staff_client(self.secretariat).get(
            reverse("vacancy-rating-sheet", args=[self.level1_position.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("interview-rating-sheet", args=[application.pk]))
        self.assertEqual(
            self._staff_client(self.hrmpsb)
            .get(reverse("vacancy-rating-sheet", args=[self.level1_position.pk]))
            .status_code,
            404,
        )

    # C1, through the page: a pick on an open pool with no open round
    def test_the_deliberation_page_picks_from_an_open_pool_without_an_open_round(self):
        posting = self._open_pool()
        first = self._extra_submitted_application(posting, "1")
        self._cos_l2_to_deliberation(posting, [first])
        chief = self._staff_client(self.hrm_chief)
        url = reverse("vacancy-batch-cos-deliberation", args=[posting.pk])
        page = chief.get(url)
        self.assertContains(page, 'name="chosen_case"')
        # Round G (G1): the page uses the intake mode's name.
        self.assertContains(page, "an open pool")
        self.assertNotContains(page, "continuous intake")
        with self.captureOnCommitCallbacks(execute=True):
            chief.post(url, {"operation": "finalize", "chosen_case": str(first.case.id), "notes": "Fills the slot."})
        first.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)

    def test_cos_exam_finalize_lands_on_the_batch_page_while_cases_remain(self):
        # Round B P3: both branches use one landing rule after the exam finalize. A COS
        # Secretariat who still holds an absent candidate lands on this vacancy's batch page,
        # not the batches list.
        first, second = self._two_qualified(self.cos_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._schedule_payload())
        client = self._staff_client(self.secretariat)
        response = client.post(
            reverse("vacancy-batch-exam", args=[self.cos_position.pk]),
            {
                "operation": "finalize",
                f"general_score_{first.case.id}": "80",
                f"technical_score_{first.case.id}": "90",
                f"outcome_{second.case.id}": ExamRecord.ExamStatus.ABSENT,
                f"notes_{second.case.id}": "Did not arrive on the exam day.",
            },
        )
        self.assertRedirects(
            response, reverse("vacancy-batch-detail", args=[self.cos_position.pk]),
            fetch_redirect_response=False,
        )


class HrmpsbRoundCTests(InterviewSessionHelpers, BaseRecruitmentTestCase):
    """Round C: fixes from docs/hrmpsb-member-review-2026-11.md (the panel member's view)."""

    def _client_for(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _named(self):
        """An application whose applicant has a name no page label contains."""
        self.applicant.first_name, self.applicant.last_name = "Zenaida", "Quiambao"
        self.applicant.save(update_fields=["first_name", "last_name"])
        application = self.make_application(self.level1_position)
        application.applicant_first_name, application.applicant_last_name = "Zenaida", "Quiambao"
        application.save(update_fields=["applicant_first_name", "applicant_last_name"])
        return application

    def _submitted(self):
        application = self._named()
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def _at_panel(self):
        application = self._named()
        self.move_application_to_hrmpsb_review(application)
        application.refresh_from_db()
        return application

    # P1-1: a closed case's timeline must not name the applicant to the panel.
    def test_closed_case_timeline_does_not_name_the_applicant_to_the_panel(self):
        application = self._submitted()
        # Round G (G3): a member's past records are their own vacancies.
        application.position.panel_end_users.add(self.hrmpsb)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(application, self.secretariat, "reject", "Not eligible.")
        url = reverse("application-detail", args=[application.pk])
        member_page = self._client_for(self.hrmpsb).get(url)
        self.assertEqual(member_page.status_code, 200)
        self.assertNotContains(member_page, application.applicant_last_name)
        self.assertNotContains(member_page, application.applicant_email)
        staff_page = self._client_for(self.secretariat).get(url)
        self.assertContains(staff_page, application.applicant_last_name)

    def test_selected_appointee_is_blinded_in_the_locked_case_summary(self):
        winner = self._submitted()
        selection = SimpleNamespace(
            selected_application=winner,
            selected_application_id=winner.pk,
            selected_item=SimpleNamespace(rank_order=1),
            comparative_assessment_report=SimpleNamespace(official_version=1),
            decided_by="Appointing Authority",
            decided_at=None,
            is_deep_selection=False,
        )
        request = RequestFactory().get("/")
        request.user = self.hrmpsb
        html = render_to_string(
            "internal_includes/completion_locked_body.html",
            {"latest_final_selection": selection, "application": winner},
            request=request,
        )
        self.assertNotIn(winner.applicant_last_name, html)
        self.assertIn("Candidate", html)

    # P1-2: the panel never records an applicant's withdrawal.
    def test_panel_member_cannot_record_a_withdrawal(self):
        application = self._at_panel()
        self.assertFalse(user_can_record_withdrawal(self.hrmpsb, application))
        response = self._client_for(self.hrmpsb).post(
            reverse("application-withdraw", args=[application.pk]),
            {"reason": "Told the board they are no longer interested."},
        )
        application.refresh_from_db()
        self.assertNotEqual(application.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertIn(response.status_code, (302, 403, 404))
        page = self._client_for(self.hrmpsb).get(reverse("application-detail", args=[application.pk]))
        self.assertNotContains(page, "Record an applicant withdrawal")

    def test_owning_office_records_a_withdrawal_at_the_panel_stage_whatever_the_policy(self):
        policy = get_office_policy()
        policy.withdrawal_recorded_by_owning_office = False
        policy.save(update_fields=["withdrawal_recorded_by_owning_office", "updated_at"])
        application = self._at_panel()
        self.assertTrue(user_can_record_withdrawal(self.secretariat, application))

    # P2-2: the member is not offered a Finalize it cannot use.
    def test_batch_interview_page_hides_finalize_from_the_panel(self):
        application = self._plantilla_session_awaiting_ratings()
        self.finalize_applicant_pool_for_test(application.position)
        url = reverse("vacancy-batch-interview", args=[application.position.pk])
        member_page = self._client_for(self.hrmpsb).get(url)
        self.assertEqual(member_page.status_code, 200)
        self.assertNotContains(member_page, 'id="interviewFinalizeForm"')
        staff_page = self._client_for(self.secretariat).get(url)
        self.assertContains(staff_page, 'id="interviewFinalizeForm"')

    # P2-3: once the interview is finalized the panel waits for the CAR; nothing is theirs.
    def test_cases_waiting_for_the_car_are_not_the_panels_work(self):
        application = self._at_panel()
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        application.refresh_from_db()
        self.assertEqual(get_current_workflow_section(application), "car")
        member = self._client_for(self.hrmpsb)
        batch = member.get(reverse("vacancy-batch-detail", args=[application.position.pk]))
        self.assertEqual(batch.status_code, 200)
        self.assertContains(batch, "Waiting for the Secretariat")
        self.assertEqual(batch.context["awaiting_count"], 0)
        console = member.get(reverse("vacancy-batches"))
        self.assertEqual(console.context["total_applications"], 0)

    # P2-5: the panel is told when its CAR is finalized.
    def test_panel_is_notified_when_the_car_is_finalized(self):
        application = self._named()
        with self.captureOnCommitCallbacks(execute=True):
            self.move_application_to_appointing_review(application)
        application.refresh_from_db()
        notice = Notification.objects.filter(
            recipient=self.hrmpsb, kind=Notification.Kind.CAR_FINALIZED
        ).first()
        self.assertIsNotNone(notice)
        self.assertIn(application.position.job_code, notice.title)
        self.assertIn(application.candidate_code_label, notice.body)
        self.assertNotIn(application.applicant_last_name, notice.title + notice.body)

    # P3-1: one line for a sheet with gaps, not one per competency.
    def test_rating_errors_are_said_once(self):
        application = self._plantilla_session_awaiting_ratings()
        self.publish_competency_rating_sheet(application.position)
        response = self._client_for(self.hrmpsb).post(
            reverse("interview-rating", args=[application.pk]),
            {"rating_notes": "", "justification": ""},
            follow=True,
        )
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Score every competency", text)
        self.assertNotIn("This field is required.", text)

    # P3-2: the batch finalize names the case that is short of ratings.
    def test_batch_finalize_names_the_case_short_of_ratings(self):
        application = self._plantilla_session_awaiting_ratings()
        self.finalize_applicant_pool_for_test(application.position)
        with self.assertRaisesMessage(ValueError, application.reference_label):
            finalize_vacancy_interview_batch(application.position, self.secretariat)

    # P3-3: the panel's calendar holds its interviews only.
    def test_panel_calendar_shows_interviews_only(self):
        self._plantilla_session_awaiting_ratings()
        today = timezone.localdate()
        events = get_schedule_events_for_user(
            self.hrmpsb, today - timedelta(days=30), today + timedelta(days=30)
        )
        self.assertTrue(events)
        self.assertEqual({event.kind for event in events}, {EventKind.INTERVIEW})


class HrmpsbRoundCTimingTests(InterviewSessionHelpers, BaseRecruitmentTestCase):
    """Round C, the rules that depend on the interview time (P2-1, P2-4)."""

    enforce_event_held = True

    def _client_for(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _awaiting_ratings(self):
        """A Plantilla case with its interview tomorrow; the earlier steps are recorded with
        the usual stand-in for "after the sitting"."""
        with patch("recruitment.services.clock.event_not_held_yet", return_value=False):
            application = self._plantilla_session_awaiting_ratings()
        application.applicant_first_name, application.applicant_last_name = "Zenaida", "Quiambao"
        application.save(update_fields=["applicant_first_name", "applicant_last_name"])
        return application

    def _hold_interview(self, application):
        session = get_interview_session(application)
        session.scheduled_for = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["scheduled_for", "updated_at"])

    # P2-1: no scoring before the interview.
    def test_rating_waits_for_the_interview(self):
        application = self._awaiting_ratings()
        cleaned = self.competency_rating_cleaned_data(application, level=3)
        with self.assertRaisesMessage(ValueError, "Scoring opens once it has taken place"):
            save_interview_rating(application=application, actor=self.hrmpsb, cleaned_data=cleaned)
        page = self._client_for(self.hrmpsb).get(reverse("application-detail", args=[application.pk]))
        self.assertContains(page, "Scoring opens")
        self.assertNotContains(page, "Submit your rating")
        self._hold_interview(application)
        save_interview_rating(application=application, actor=self.hrmpsb, cleaned_data=cleaned)
        self.assertTrue(
            get_interview_session(application).ratings.filter(rated_by=self.hrmpsb).exists()
        )

    def test_paper_sheet_waits_for_the_interview(self):
        application = self._awaiting_ratings()
        with self.assertRaisesMessage(ValueError, "Scoring opens once it has taken place"):
            upload_interview_fallback_rating(
                application=application,
                actor=self.secretariat,
                uploaded_file=SimpleUploadedFile(
                    "sheet.pdf", b"%PDF-1.4 sheet", content_type="application/pdf"
                ),
                remarks="",
                rated_by=self.hrmpsb,
                rating_score=Decimal("80"),
            )

    # P2-4: the panel's printout names the applicant only from the interview time.
    def test_printed_sheet_names_the_applicant_to_the_panel_only_once_the_interview_starts(self):
        application = self._awaiting_ratings()
        url = reverse("interview-rating-sheet-print", args=[application.pk])
        before = self._client_for(self.hrmpsb).get(url)
        self.assertEqual(before.status_code, 200)
        self.assertNotContains(before, application.applicant_last_name)
        self.assertContains(before, application.candidate_code_label)
        office = self._client_for(self.secretariat).get(url)
        self.assertContains(office, application.applicant_last_name)
        self._hold_interview(application)
        after = self._client_for(self.hrmpsb).get(url)
        self.assertContains(after, application.applicant_last_name)


class AppointingAuthorityRoundDTests(BaseRecruitmentTestCase):
    """Round D: fixes from docs/appointing-authority-review-2026-11.md."""

    def _client_for(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _named(self):
        self.applicant.first_name, self.applicant.last_name = "Zenaida", "Quiambao"
        self.applicant.save(update_fields=["first_name", "last_name"])
        application = self.make_application(self.level1_position)
        application.applicant_first_name, application.applicant_last_name = "Zenaida", "Quiambao"
        application.save(update_fields=["applicant_first_name", "applicant_last_name"])
        return application

    def _at_decision(self):
        application = self._named()
        with self.captureOnCommitCallbacks(execute=True):
            self.move_application_to_appointing_review(application)
        application.refresh_from_db()
        return application

    def _selected(self):
        application = self._at_decision()
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(application, self.appointing)
        application.refresh_from_db()
        return application

    # P2-1: after selecting, the appointee's page shows the decision.
    def test_appointee_page_shows_the_decision_after_selecting(self):
        application = self._selected()
        page = self._client_for(self.appointing).get(
            reverse("application-detail", args=[application.pk])
        )
        self.assertContains(page, "Final selection record")
        self.assertContains(page, "Completion tracking is with the Secretariat")
        self.assertNotContains(page, "This step will open after a final decision is recorded")

    # P2-2: rescinding asks for confirmation first.
    def test_rescission_asks_for_confirmation(self):
        application = self._selected()
        page = self._client_for(self.appointing).get(
            reverse("application-detail", args=[application.pk])
        )
        self.assertContains(page, 'id="finalSelectionRescindForm"')
        self.assertContains(page, 'data-bs-target="#confirmRescindSelection"')
        self.assertContains(page, 'id="confirmRescindSelection"')

    # P2-3: the Director never records a withdrawal; the owning office does.
    def test_appointing_authority_cannot_record_a_withdrawal(self):
        policy = get_office_policy()
        policy.withdrawal_recorded_by_owning_office = False
        policy.save(update_fields=["withdrawal_recorded_by_owning_office", "updated_at"])
        application = self._at_decision()
        self.assertFalse(user_can_record_withdrawal(self.appointing, application))
        self.assertTrue(user_can_record_withdrawal(self.secretariat, application))
        page = self._client_for(self.appointing).get(
            reverse("application-detail", args=[application.pk])
        )
        self.assertNotContains(page, "Record an applicant withdrawal")

    # P2-4: the panel is told when its vacancy is decided.
    def test_panel_is_told_the_vacancy_was_decided(self):
        application = self._selected()
        notice = Notification.objects.filter(
            recipient=self.hrmpsb, kind=Notification.Kind.VACANCY_DECIDED
        ).first()
        self.assertIsNotNone(notice)
        self.assertIn(application.position.job_code, notice.title)
        self.assertIn(application.candidate_code_label, notice.body)
        self.assertNotIn("Quiambao", notice.title + notice.body)

    def test_panel_is_told_when_no_appointment_is_made(self):
        application = self._at_decision()
        with self.captureOnCommitCallbacks(execute=True):
            record_no_appointment(application, self.appointing, "No candidate meets the need.")
        notice = Notification.objects.filter(
            recipient=self.hrmpsb, kind=Notification.Kind.VACANCY_DECIDED
        ).first()
        self.assertIsNotNone(notice)
        self.assertIn("No appointment", notice.body)

    # P3-1: the CAR return says what happened.
    def test_car_return_says_what_happened(self):
        application = self._at_decision()
        response = self._client_for(self.appointing).post(
            reverse("workflow-action", args=[application.pk]),
            {"action": "return_car_for_reassessment", "remarks": "ETE needs correction."},
            follow=True,
        )
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("CAR returned to the HRMPSB", text)

    # P3-3: a replacement selection says what it replaced.
    def test_replacement_selection_names_the_one_it_replaced(self):
        application = self._selected()
        selection = FinalSelection.objects.get(selected_application=application)
        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(selection, self.appointing, "Recorded in error.")
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(application, self.appointing)
        page = self._client_for(self.appointing).get(
            reverse("application-detail", args=[application.pk])
        )
        self.assertContains(page, "Replaces the selection rescinded on")


class WithdrawalOwningOfficeSwitchTests(BaseRecruitmentTestCase):
    """Rounds C and D leave the Office Policy switch one job: whether the owning office may
    record a withdrawal while the other office holds the case on loan."""

    def _lent(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        with self.captureOnCommitCallbacks(execute=True):
            hand_off_vacancy_batch(self.level1_position, self.secretariat, "Lending the batch.")
        application.refresh_from_db()
        return application

    def test_switch_on_lets_the_owning_office_record_while_the_case_is_lent(self):
        application = self._lent()
        self.assertTrue(user_can_record_withdrawal(self.secretariat, application))
        self.assertTrue(user_can_record_withdrawal(self.hrm_chief, application))

    def test_switch_off_leaves_it_to_the_office_holding_the_case(self):
        policy = get_office_policy()
        policy.withdrawal_recorded_by_owning_office = False
        policy.save(update_fields=["withdrawal_recorded_by_owning_office", "updated_at"])
        application = self._lent()
        self.assertFalse(user_can_record_withdrawal(self.secretariat, application))
        self.assertTrue(user_can_record_withdrawal(self.hrm_chief, application))


class RoundBLeftoverCheckTests(BaseRecruitmentTestCase):
    """Fixes from the Round B browser check (2026-11-03)."""

    def _client_for(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    def _cos_case_at_chief_step(self):
        application = self.make_application(self.cos_position)
        application = self.move_application_to_hrm_chief_review(application)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        return application

    # A Level 1 COS case at the Chief's own steps is not a lendable batch.
    def test_chief_cannot_send_a_level1_cos_batch_from_the_chiefs_own_steps(self):
        application = self._cos_case_at_chief_step()
        self.assertFalse(user_can_send_vacancy_batch(self.hrm_chief, self.cos_position))
        with self.assertRaisesMessage(ValueError, "You hold no cases in this vacancy's batch"):
            hand_off_vacancy_batch(self.cos_position, self.hrm_chief, "Lending it.")
        page = self._client_for(self.hrm_chief).get(
            reverse("vacancy-batch-detail", args=[self.cos_position.pk])
        )
        self.assertNotContains(page, "Send the batch to the Secretariat")
        self._client_for(self.hrm_chief).post(
            reverse("vacancy-batch-handoff", args=[self.cos_position.pk]),
            {"operation": "send", "remarks": "Lending it."},
        )
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

    # Lending at screening still works both ways: send, and the borrower sends it back.
    def test_level1_batch_at_screening_can_still_be_lent_and_sent_back(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        self.assertTrue(user_can_send_vacancy_batch(self.secretariat, self.cos_position))
        with self.captureOnCommitCallbacks(execute=True):
            hand_off_vacancy_batch(self.cos_position, self.secretariat, "Lending it.")
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertTrue(user_can_send_vacancy_batch(self.hrm_chief, self.cos_position))
        with self.captureOnCommitCallbacks(execute=True):
            hand_off_vacancy_batch(self.cos_position, self.hrm_chief, "Sending it back.")
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    # The standing-panel field renders as a list of real checkboxes.
    def test_office_policy_panel_field_renders_as_checkboxes(self):
        policy = get_office_policy()
        policy.core_panel_members.set([self.hrmpsb])
        page = self._client_for(self.hrm_chief).get(reverse("office-policy"))
        html = page.content.decode()
        self.assertContains(page, "data-checkbox-list")
        self.assertContains(page, "<legend class=\"form-label\">Standing HRMPSB panel")
        self.assertRegex(
            html,
            r'<input type="checkbox" name="core_panel_members" value="%d" class="form-check-input"[^>]*checked'
            % self.hrmpsb.pk,
        )
        self.assertNotRegex(html, r'name="core_panel_members"[^>]*class="[^"]*form-control')


class RoundGTests(BaseRecruitmentTestCase):
    """Round G (docs/polish-round-g-2026-11.md): the Director's calendar and a panel
    member's past records."""

    def _client_for(self, user):
        client = Client()
        self.force_login_with_mfa(client, user)
        return client

    # G2: the Director has no calendar of their own.
    def test_the_director_has_no_schedule_and_no_coming_up(self):
        director = self._client_for(self.appointing)
        self.assertEqual(director.get(reverse("schedule-calendar")).status_code, 403)
        landing = director.get(reverse("vacancy-batches"))
        self.assertEqual(landing.status_code, 200)
        self.assertNotContains(landing, "data-coming-up")
        self.assertNotContains(landing, f'href="{reverse("schedule-calendar")}"')

    def test_the_offices_keep_their_schedule_and_coming_up(self):
        secretariat = self._client_for(self.secretariat)
        self.assertEqual(secretariat.get(reverse("schedule-calendar")).status_code, 200)
        landing = secretariat.get(reverse("vacancy-batches"))
        self.assertContains(landing, "data-coming-up")
        self.assertContains(landing, f'href="{reverse("schedule-calendar")}"')

    # G3: a panel member's past records are their own vacancies.
    def _rated_and_closed(self):
        application = self.make_application(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            self.move_application_to_appointing_review(application)
        self.assertTrue(InterviewRating.objects.filter(rated_by=self.hrmpsb, application=application).exists())
        RecruitmentApplication.objects.filter(pk=application.pk).update(status=RecruitmentApplication.Status.REJECTED)
        RecruitmentCase.objects.filter(pk=application.case.pk).update(current_stage=RecruitmentCase.Stage.CLOSED)
        application.refresh_from_db()
        return application

    def _closed_without_the_member(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(application, self.hrm_chief, "reject", "Not eligible.")
        application.refresh_from_db()
        return application

    def test_a_member_sees_closed_cases_of_vacancies_they_rated_on_only(self):
        from recruitment.services import get_closed_cases_for_user, user_can_view_application

        rated = self._rated_and_closed()
        other = self._closed_without_the_member()
        closed_ids = set(get_closed_cases_for_user(self.hrmpsb).values_list("pk", flat=True))
        self.assertIn(rated.pk, closed_ids)
        self.assertNotIn(other.pk, closed_ids)
        self.assertTrue(user_can_view_application(self.hrmpsb, rated))
        self.assertFalse(user_can_view_application(self.hrmpsb, other))
        self.assertEqual(
            self._client_for(self.hrmpsb).get(reverse("application-detail", args=[other.pk])).status_code,
            404,
        )

    def test_a_named_end_user_sees_that_vacancys_closed_cases(self):
        from recruitment.services import get_closed_cases_for_user, user_can_view_application

        other = self._closed_without_the_member()
        self.level2_position.panel_end_users.add(self.hrmpsb)
        self.assertIn(other.pk, set(get_closed_cases_for_user(self.hrmpsb).values_list("pk", flat=True)))
        self.assertTrue(user_can_view_application(self.hrmpsb, other))

    def test_a_standing_member_does_not_get_every_closed_case(self):
        from recruitment.services import get_closed_cases_for_user

        other = self._closed_without_the_member()
        get_office_policy().core_panel_members.set([self.hrmpsb])
        self.assertNotIn(other.pk, set(get_closed_cases_for_user(self.hrmpsb).values_list("pk", flat=True)))

    def test_the_offices_closed_lists_are_unchanged(self):
        from recruitment.services import get_closed_cases_for_user

        other = self._closed_without_the_member()
        self.assertIn(other.pk, set(get_closed_cases_for_user(self.hrm_chief).values_list("pk", flat=True)))
