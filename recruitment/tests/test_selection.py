"""Deliberation, the Comparative Assessment Report and the final decision."""

import re
from decimal import ROUND_HALF_UP, Decimal
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from ..forms import FinalSelectionForm
from ..models import (
    ApplicationETERating,
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CosSelectionRound,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    FinalSelection,
    Notification,
    NotificationLog,
    OfficePolicy,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
)
from ..services import (
    build_submission_packet,
    close_cos_selection_round_short,
    generate_comparative_assessment_report,
    get_current_workflow_section,
    get_office_policy,
    reopen_recruitment_case,
    get_latest_finalized_comparative_assessment_report,
    get_available_actions,
    get_queue_for_user,
    issue_application_otp,
    process_workflow_action,
    record_final_decision,
    user_can_record_final_decision,
    record_final_selection,
    record_no_appointment,
    return_car_for_reassessment,
    _final_selection_items_are_ready,
    rescind_final_selection,
    user_can_rescind_final_selection,
    user_can_record_final_selection,
    user_can_record_no_appointment,
    get_final_selection_for_entry,
    get_final_selection_history,
    final_selection_recommended_case_ids,
    save_cos_vacancy_deliberation,
    rescind_cos_vacancy_deliberation,
    user_can_rescind_cos_vacancy_deliberation,
    get_rescinded_cos_picks_for_case,
    get_final_decision_history,
    cos_selection_round_pool,
    get_cos_vacancy_deliberation,
    get_latest_final_decision,
    car_item_is_within_top_five,
    car_top_rank_case_ids,
    get_comparative_assessment_readiness,
    get_cos_pick_for_case,
    get_latest_cos_selection_round,
    get_open_cos_selection_round,
    open_cos_selection_round,
    _open_implicit_cos_selection_round,
    user_can_open_cos_selection_round,
    user_can_close_cos_selection_round_short,
    cos_deliberation_pool,
    cos_eligible_pool,
    get_or_create_vacancy_assessment_weights,
    lock_vacancy_assessment_weights,
    unlock_vacancy_assessment_weights,
    set_application_ete_rating,
    save_interview_rating,
    save_interview_session,
    submit_application,
    user_can_manage_exam,
    vacancy_screening_batch_status,
    record_application_withdrawal,
    user_can_manage_deliberation,
    save_deliberation_record,
    cancel_recruitment_entry,
    user_can_manage_comparative_assessment_report,
    user_can_view_application,
    verify_application_otp,
)
from .base import User, BaseRecruitmentTestCase


class DeliberationDecisionSupportTests(BaseRecruitmentTestCase):
    def session_payload(self, **overrides):
        payload = {
            "scheduled_for": timezone.now() + timedelta(days=1),
            "location": "Virtual Deliberation Room",
            "session_notes": "Decision-support interview session prepared.",
        }
        payload.update(overrides)
        return payload

    def deliberation_payload(self, **overrides):
        payload = {
            "deliberated_at": timezone.now(),
            "deliberation_minutes": "Panel reviewed the finalized applicant pool.",
            "recommendation": "HRMPSB recommends the ranking after reviewing the CAR draft.",
            "decision_support_summary": "Decision-support summary for ranking.",
            "quorum_status": DeliberationRecord.QuorumStatus.MET,
            "attendance_notes": "Quorum and attendance were recorded.",
            "ranking_position": 1,
            "ranking_notes": "Ranking basis recorded.",
        }
        payload.update(overrides)
        return payload

    def rating_payload(self, application, level=3, **overrides):
        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a single end-user / HRMS score, not the competency
            # sheet. Map the legacy level to its normalized 0-100 score (3/4 -> 75.00).
            payload = {
                "rating_score": "{:.2f}".format(level / 4 * 100),
                "rating_notes": "Interview performance supports the recommendation.",
                "justification": "",
            }
            payload.update(overrides)
            return payload
        template = self.publish_competency_rating_sheet(application.position)
        payload = {
            "competency_scores": {
                competency: level for competency in template.competencies.all()
            },
            "rating_notes": "Interview performance supports the recommendation.",
            "justification": "",
        }
        payload.update(overrides)
        return payload

    def test_cos_deliberation_pick_records_the_chosen_applicant(self):
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
        save_interview_session(
            application=application,
            actor=self.hrm_chief,
            cleaned_data=self.session_payload(session_notes="Interview output locked before deliberation."),
            finalize=True,
        )
        application.refresh_from_db()
        # The COS pool is at the deliberation section before the pick.
        self.assertEqual(get_current_workflow_section(application), "deliberation")

        pick = save_cos_vacancy_deliberation(
            self.cos_position,
            self.hrm_chief,
            application.case.id,
            "End-user and HRM Chief selected this applicant.",
            finalize=True,
        )

        self.assertTrue(pick.is_finalized)
        self.assertEqual(pick.chosen_case_id, application.case.id)
        self.assertEqual(pick.recruitment_entry_id, self.cos_position.id)
        # The finalized pick is one per vacancy and gates the decision section.
        self.assertEqual(get_cos_vacancy_deliberation(self.cos_position).pk, pick.pk)
        application.refresh_from_db()
        # SL-01: finalizing the pick IS the selection — the chosen applicant is recorded
        # as selected and routed straight to completion, with no second decision step.
        self.assertEqual(get_current_workflow_section(application), "completion")
        decision = get_latest_final_decision(application)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)
        self.assertEqual(decision.recruitment_case_id, pick.chosen_case_id)
        # The submission packet now reads the per-vacancy pick.
        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_cos_deliberation_pick"])
        self.assertNotIn(
            "Finalized COS deliberation pick", packet["summary"]["missing_components"]
        )

    def _cos_case_at_deliberation(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    def test_default_policy_keeps_implicit_cos_selection_round_single_slot(self):
        self._cos_case_at_deliberation()
        PositionPosting.objects.filter(pk=self.cos_position.pk).update(positions_to_fill=4)
        self.cos_position.refresh_from_db()

        selection_round = _open_implicit_cos_selection_round(
            self.cos_position, self.hrm_chief
        )

        self.assertEqual(selection_round.slot_count, 1)

    def test_policy_can_apply_positions_to_fill_to_implicit_cos_round(self):
        self._cos_case_at_deliberation()
        policy = get_office_policy()
        policy.cos_positions_multi_slot_allowed = True
        policy.save(update_fields=["cos_positions_multi_slot_allowed", "updated_at"])
        self.cos_position.positions_to_fill = 3
        self.cos_position.save(update_fields=["positions_to_fill", "updated_at"])

        selection_round = _open_implicit_cos_selection_round(
            self.cos_position, self.hrm_chief
        )

        self.assertEqual(selection_round.slot_count, 3)

    # ── SL-14 (COS): a recorded COS pick can be corrected ─────────────────────
    def _cos_pick_with_a_closed_sibling(self):
        """A finalized COS pick plus a sibling the pick closed as not selected."""
        winner = self._cos_case_at_deliberation()
        # Its own applicant account — one application per applicant per position — and
        # already submitted, so this drives it from submission rather than re-submitting.
        sibling = self._submitted_cos_application(self.cos_position, "rescind-sib")
        if sibling.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(sibling, self.secretariat)
            self.finalize_exam_for_current_stage(sibling, self.secretariat)
            sibling.refresh_from_db()
        self.finalize_screening_for_current_stage(sibling, self.hrm_chief)
        self.finalize_exam_for_current_stage(sibling, self.hrm_chief)
        self.finalize_interview_for_current_stage(sibling, self.hrm_chief)
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            pick = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "End-user and HRM Chief selected this applicant.",
                finalize=True,
            )
        winner.refresh_from_db()
        sibling.refresh_from_db()
        return pick, winner, sibling

    def test_cos_rescind_cutoff_blocks_contract_then_never_allows_and_voids_tracking(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        completion = CompletionRecord.objects.create(
            application=winner,
            recruitment_case=winner.case,
            tracked_by=self.hrm_chief,
            completion_reference="COS-CONTRACT-2026-001",
            completion_date=timezone.localdate(),
            deadline=timezone.localdate() + timedelta(days=5),
        )
        CompletionRequirement.objects.create(
            completion_record=completion,
            item_label="Signed contract",
            status=CompletionRequirement.RequirementStatus.COMPLETED,
        )
        refusal = (
            "The appointment has been issued. A revocation at this point is handled as a "
            "separation, not a rescission."
        )

        self.assertFalse(user_can_rescind_cos_vacancy_deliberation(self.hrm_chief, pick))
        with self.assertRaisesMessage(ValueError, refusal):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "The engagement will not proceed."
            )

        policy = get_office_policy()
        policy.rescind_cutoff = OfficePolicy.RescindCutoff.NEVER
        policy.save(update_fields=["rescind_cutoff", "updated_at"])
        self.assertTrue(user_can_rescind_cos_vacancy_deliberation(self.hrm_chief, pick))
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "The engagement will not proceed."
            )
        completion.refresh_from_db()
        self.assertTrue(completion.is_voided)
        self.assertIsNotNone(completion.voided_at)
        self.assertEqual(completion.void_reason, "The engagement will not proceed.")
        self.assertFalse(completion.ready_for_closure)
        self.assertIn("Voided by rescission", render_to_string(
            "internal_includes/completion_locked_body.html",
            {"completion_record": completion},
        ))

        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "The engagement is proceeding after correction.",
                finalize=True,
            )
        completion.refresh_from_db()
        self.assertFalse(completion.is_voided)
        self.assertEqual(completion.completion_reference, "")
        self.assertIsNone(completion.completion_date)
        self.assertFalse(completion.requirements.exists())

    def test_a_live_cos_pick_must_be_rescinded_before_the_vacancy_is_cancelled(self):
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        refusal = (
            "A COS selection has already been recorded for this vacancy. Rescind the "
            "deliberation pick first if the engagement is not going ahead."
        )

        # Chief review P3: a decided vacancy's row no longer offers Cancel; the server-side
        # refusal below still stands for a forced POST.
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        listing = client.get(reverse("recruitment-entry-list") + "?status=all")
        self.assertNotContains(
            listing,
            reverse("recruitment-entry-cancel", args=[self.cos_position.pk]),
        )
        refused = client.post(
            reverse("recruitment-entry-cancel", args=[self.cos_position.pk]),
            {"reason": "The engagement is not going ahead."},
            follow=True,
        )
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, refusal)
        self.cos_position.refresh_from_db()
        pick.refresh_from_db()
        winner.refresh_from_db()
        self.assertEqual(self.cos_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertTrue(pick.is_effective)
        self.assertEqual(winner.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertIsNotNone(get_latest_final_decision(winner))

        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick,
                self.hrm_chief,
                "The engagement is not going ahead.",
            )
            entry, affected = cancel_recruitment_entry(
                self.cos_position,
                self.hrm_chief,
                "The requesting office cancelled the engagement.",
            )

        self.assertEqual(entry.status, PositionPosting.EntryStatus.CANCELLED)
        self.assertEqual({application.pk for application in affected}, {winner.pk, sibling.pk})
        for application in (winner, sibling):
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
            self.assertTrue(
                NotificationLog.objects.filter(
                    application=application,
                    notification_type=NotificationLog.NotificationType.VACANCY_CANCELLED,
                ).exists()
            )

    def test_rescinding_a_cos_pick_preserves_it_and_reopens_the_round(self):
        # SL-14 (COS): SL-01 made the finalized pick the COS selection, so voiding it must
        # unwind everything that act did — the winner left the decision step for completion
        # and the rest of the round was closed as not selected.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        self.assertEqual(get_current_workflow_section(winner), "completion")
        self.assertEqual(sibling.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        selection_round = pick.selection_round
        self.assertEqual(selection_round.status, CosSelectionRound.Status.RESOLVED)
        hrm_chief_peer = User.objects.create_user(
            username="hrm-chief-peer",
            password="testpass123",
            email="hrm.chief.peer@example.com",
            role=RecruitmentUser.Role.HRM_CHIEF,
        )
        withdrawn = self._submitted_cos_application(
            self.cos_position,
            "rescind-withdrawn",
        )
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                withdrawn,
                self.secretariat,
                "Applicant withdrew before the COS pick was reconsidered.",
            )

        with self.captureOnCommitCallbacks(execute=True):
            rescinded, restored = rescind_cos_vacancy_deliberation(
                pick,
                self.hrm_chief,
                "Chosen applicant declined before the contract was issued.",
                reference="HRM Memo 2026-14",
            )

        # The pick is preserved, never edited back to a draft or deleted.
        rescinded.refresh_from_db()
        self.assertTrue(rescinded.is_rescinded)
        self.assertTrue(rescinded.is_finalized, "the original finalization must survive")
        self.assertIsNotNone(rescinded.finalized_at)
        self.assertEqual(rescinded.rescinded_by, self.hrm_chief)
        self.assertIn("declined", rescinded.rescind_reason)
        self.assertEqual(rescinded.rescind_reference, "HRM Memo 2026-14")
        self.assertFalse(rescinded.is_effective)

        # The round reopens: its slot is free again for the replacement pick.
        selection_round.refresh_from_db()
        self.assertEqual(selection_round.status, CosSelectionRound.Status.OPEN)
        self.assertIsNone(selection_round.resolved_at)

        # Winner and the sibling it closed both come back to the deliberation step, which
        # is where the replacement pick is actually made (the COS decision IS the pick).
        self.assertEqual({a.pk for a in restored}, {winner.pk, sibling.pk})
        for application in (winner, sibling):
            application.refresh_from_db()
            self.assertEqual(
                application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW
            )
            self.assertEqual(
                application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF
            )
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )
            self.assertIsNone(application.closed_at)
            self.assertEqual(get_current_workflow_section(application), "deliberation")
            review_notice = NotificationLog.objects.get(
                application=application,
                notification_type=(
                    NotificationLog.NotificationType.SELECTION_UNDER_REVIEW
                ),
            )
            self.assertEqual(
                review_notice.subject,
                f"Update on your application for {self.cos_position.title}",
            )
            self.assertIn("no longer final", review_notice.body)
            self.assertNotIn("declined", review_notice.body.lower())
        self.assertFalse(
            NotificationLog.objects.filter(
                application=withdrawn,
                notification_type=(
                    NotificationLog.NotificationType.SELECTION_UNDER_REVIEW
                ),
            ).exists()
        )

        # Auditable: actor, reason and who was restored.
        entry = AuditLog.objects.filter(
            application=winner, action=AuditLog.Action.FINAL_SELECTION_RESCINDED
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.hrm_chief)
        self.assertTrue(entry.metadata["was_selected"])
        self.assertIn("declined", entry.metadata["reason"])
        returned_bells = Notification.objects.filter(
            application=winner,
            kind=Notification.Kind.CASE_RETURNED,
        )
        self.assertEqual(returned_bells.count(), 1)
        returned_bell = returned_bells.get()
        self.assertEqual(returned_bell.recipient, hrm_chief_peer)
        self.assertEqual(
            returned_bell.title,
            f"{winner.reference_label} COS pick rescinded — round reopened",
        )
        self.assertEqual(
            returned_bell.body,
            "Chosen applicant declined before the contract was issued.",
        )
        self.assertNotIn(winner.applicant_display_name, returned_bell.title)
        self.assertNotIn(winner.applicant_display_name, returned_bell.body)
        self.assertFalse(
            returned_bells.filter(recipient=self.hrm_chief).exists()
        )

    def test_a_replacement_cos_pick_can_be_recorded_after_a_rescind(self):
        # The point of the correction: the vacancy must be decidable again. Every gate that
        # reads "is there a finalized pick" has to skip the rescinded one, or the
        # replacement is refused and the rescind achieved nothing.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Recorded against the wrong applicant."
            )
        sibling.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick after the first was rescinded.",
                finalize=True,
            )

        self.assertTrue(replacement.is_finalized)
        self.assertFalse(replacement.is_rescinded)
        self.assertNotEqual(replacement.pk, pick.pk)
        self.assertEqual(replacement.chosen_case_id, sibling.case.id)
        # The correction chain: the replacement points back at the pick it supersedes,
        # so the file reads as one decision correcting another (Plantilla parity).
        self.assertEqual(replacement.supersedes_id, pick.pk)
        # The replacement is the effective pick; the rescinded one is history.
        sibling.refresh_from_db()
        self.assertEqual(get_cos_pick_for_case(sibling.case).pk, replacement.pk)
        self.assertIsNone(get_cos_pick_for_case(winner.case))
        self.assertEqual(
            {p.pk for p in get_rescinded_cos_picks_for_case(winner.case)}, {pick.pk}
        )
        sibling.refresh_from_db()
        self.assertEqual(get_current_workflow_section(sibling), "completion")

    def test_the_same_candidate_can_be_re_picked_after_a_mistaken_rescind(self):
        # The unique constraint is scoped to live picks, so a rescission recorded by
        # mistake is itself correctable — re-picking the same person in the same round.
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Rescinded in error."
            )
        winner.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                winner.case.id,
                "Re-picked: the rescission was a mistake.",
                finalize=True,
            )
        self.assertNotEqual(replacement.pk, pick.pk)
        self.assertEqual(replacement.chosen_case_id, winner.case.id)
        winner.refresh_from_db()
        self.assertEqual(get_current_workflow_section(winner), "completion")

    def test_a_rescinded_cos_pick_stops_asserting_the_case_was_selected(self):
        # A COS selection leaves TWO records — the pick and a FinalDecision row. Marking
        # only the pick left the decision row still saying "Selected", which is not just a
        # display lie: it drives the case-detail banner AND the RD-7 reopen guard.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        decision = get_latest_final_decision(winner)
        self.assertEqual(decision.decision_outcome, FinalDecision.Outcome.SELECTED)

        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Chosen applicant declined."
            )

        winner.refresh_from_db()
        # The case no longer carries a decision in force...
        self.assertIsNone(get_latest_final_decision(winner))
        # ...but the decision remains in the history, marked, never deleted.
        history = list(get_final_decision_history(winner))
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].is_rescinded)
        self.assertIsNotNone(history[0].rescinded_at)

        # RD-7 guard, direction 1: with nobody holding the post, a genuinely non-selected
        # sibling is reopenable again instead of being frozen behind a void decision.
        sibling.refresh_from_db()
        self.assertEqual(
            sibling.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
        )

    def test_a_replaced_cos_winner_cannot_be_reopened_behind_the_new_selection(self):
        # RD-7 guard, direction 2: after a replacement names someone else, the FIRST winner
        # is an ordinary non-selected case. An unmarked decision row would have kept them in
        # winner_application_ids — reopenable while another selection stands, the exact
        # zombie the guard exists to prevent.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Recorded against the wrong applicant."
            )
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick.",
                finalize=True,
            )
        winner.refresh_from_db()
        self.assertEqual(winner.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        with self.assertRaisesMessage(
            ValueError, "This vacancy's selection is already recorded"
        ):
            reopen_recruitment_case(
                application=winner,
                actor=self.hrm_chief,
                reason="Trying to revive the replaced winner.",
            )

    def test_a_reopened_round_picks_only_from_the_candidates_who_competed(self):
        # A rescission reopens the round, which is the one window where a single-batch
        # vacancy's pool could drift. The replacement must come from the members who
        # actually competed — a latecomer would otherwise win a competition they were never
        # entered in, and the pool-closing that follows resolves the frozen membership, so
        # every other latecomer would be left dangling in a round already decided.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        round_member_ids = {a.pk for a in cos_selection_round_pool(pick.selection_round)}
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Declined.")

        # A candidate who only now reaches the deliberation step was never a member.
        latecomer = self._submitted_cos_application(self.cos_position, "latecomer")
        if latecomer.case.current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            self.finalize_screening_for_current_stage(latecomer, self.secretariat)
            self.finalize_exam_for_current_stage(latecomer, self.secretariat)
            latecomer.refresh_from_db()
        self.finalize_screening_for_current_stage(latecomer, self.hrm_chief)
        self.finalize_exam_for_current_stage(latecomer, self.hrm_chief)
        self.finalize_interview_for_current_stage(latecomer, self.hrm_chief)
        latecomer.refresh_from_db()
        self.assertNotIn(latecomer.pk, round_member_ids)

        pool_ids = {a.pk for a in cos_deliberation_pool(self.cos_position)}
        self.assertNotIn(latecomer.pk, pool_ids)
        self.assertTrue(pool_ids <= round_member_ids)
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                latecomer.case.id,
                "Picking someone who never competed.",
                finalize=True,
            )

    def test_a_rescinded_pick_is_not_displayed_as_the_authoritative_selection(self):
        # Review catch on the first implementation: the case surface fell back to "the
        # vacancy's most recent pick" gated on is_finalized — which a rescinded pick
        # deliberately keeps — so a voided pick was presented as "the authoritative
        # selection" during the exact window when the vacancy is undecided.
        pick, winner, sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "Chosen applicant declined."
            )

        response = client.get(reverse("application-detail", args=[winner.pk]))
        self.assertEqual(response.status_code, 200)
        # No authoritative-pick card, and the deliberation section says so honestly.
        self.assertIsNone(response.context.get("cos_authoritative_pick"))
        self.assertContains(response, "Not yet recorded")

        # Once the replacement lands, IT is the authoritative pick everywhere.
        sibling.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            replacement = save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                sibling.case.id,
                "Replacement pick.",
                finalize=True,
            )
        response = client.get(reverse("application-detail", args=[winner.pk]))
        shown = response.context.get("cos_authoritative_pick")
        self.assertIsNotNone(shown)
        self.assertEqual(shown.pk, replacement.pk)

    def test_rescinding_a_cos_pick_requires_the_hrm_chief_and_a_reason(self):
        pick, _winner, _sibling = self._cos_pick_with_a_closed_sibling()
        # The Secretariat handles COS screening/exam but does not decide it.
        self.assertFalse(
            user_can_rescind_cos_vacancy_deliberation(self.secretariat, pick)
        )
        with self.assertRaisesMessage(
            ValueError, "Only the HRM Chief may rescind a recorded COS deliberation pick."
        ):
            rescind_cos_vacancy_deliberation(pick, self.secretariat, "No authority.")
        with self.assertRaisesMessage(
            ValueError, "Record why this COS deliberation pick is being rescinded."
        ):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "   ")
        # And it cannot be rescinded twice.
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Correcting an error.")
        with self.assertRaisesMessage(
            ValueError, "This COS deliberation pick has already been rescinded."
        ):
            rescind_cos_vacancy_deliberation(pick, self.hrm_chief, "Again.")

    def test_the_hrm_chief_can_rescind_a_cos_pick_from_the_case_surface(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        # The card is offered on the winner's case.
        response = client.get(reverse("application-detail", args=[winner.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("cos_pick_rescind_form"))
        self.assertNotContains(response, "{#")

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("cos-pick-rescind", args=[winner.pk]),
                {"reason": "Chosen applicant declined the contract."},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "COS deliberation pick rescinded")
        pick.refresh_from_db()
        self.assertTrue(pick.is_rescinded)

    def test_the_cos_rescind_endpoint_refuses_a_role_that_cannot_decide(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("cos-pick-rescind", args=[winner.pk]),
            {"reason": "Trying to void someone else's decision."},
        )
        self.assertEqual(response.status_code, 403)
        pick.refresh_from_db()
        self.assertFalse(pick.is_rescinded)

    def test_cos_deliberation_pick_requires_the_hrm_chief(self):
        application = self._cos_case_at_deliberation()
        with self.assertRaisesMessage(
            ValueError, "Only the HRM Chief records the COS deliberation pick."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position, self.secretariat, application.case.id, "Attempt.", finalize=True
            )

    def test_cos_deliberation_pick_rejects_a_candidate_outside_the_pool(self):
        application = self._cos_case_at_deliberation()
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_position, self.hrm_chief, 999999, "Bad pick.", finalize=True
            )
        self.assertIsNone(get_cos_vacancy_deliberation(self.cos_position))

    def test_cos_deliberation_view_finalizes_via_post(self):
        application = self._cos_case_at_deliberation()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-cos-deliberation", args=[self.cos_position.pk]),
                {
                    "operation": "finalize",
                    "chosen_case": application.case.id,
                    "notes": "Chosen via the COS deliberation console.",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        pick = get_cos_vacancy_deliberation(self.cos_position)
        self.assertIsNotNone(pick)
        self.assertTrue(pick.is_finalized)
        self.assertEqual(pick.chosen_case_id, application.case.id)
        application.refresh_from_db()
        # SL-01: the console finalize is the selection act.
        self.assertEqual(get_current_workflow_section(application), "completion")
        self.assertEqual(
            get_latest_final_decision(application).decision_outcome,
            FinalDecision.Outcome.SELECTED,
        )

    # ── SL-04: continuous COS is gated on its own screening ───────────────────
    def test_a_continuous_cos_case_is_not_blocked_by_a_sibling_in_screening(self):
        # SL-04, kept under C1: an open pool's batch is whoever is ready; an applicant still
        # in screening joins the next batch instead of holding this one.
        ready = self._continuous_candidate_at_exam("ready")
        pending = self._continuous_candidate_awaiting_screening("pending")
        self.assertTrue(vacancy_screening_batch_status(self.cos_continuous_position).is_ready)
        self.assertEqual(get_current_workflow_section(ready), "exam")
        self.assertTrue(user_can_manage_exam(self.secretariat, ready))
        self.assertEqual(get_current_workflow_section(pending), "screening")

    def test_a_batch_cos_vacancy_keeps_its_vacancy_wide_screening_gate(self):
        # SL-04 AC#3: opening-based and pooling COS still move as one batch.
        first, second = self._two_pooling_cos_candidates()
        self.finalize_screening_for_current_stage(first, self.secretariat)
        first.refresh_from_db()
        self.assertEqual(get_current_workflow_section(first), "exam")
        self.assertFalse(
            vacancy_screening_batch_status(self.cos_position).is_ready
        )
        self.assertFalse(user_can_manage_exam(self.secretariat, first))
        self.assertIsNotNone(second)

    def _submitted_cos_application(self, position, suffix):
        """A submitted COS application from its own applicant account."""
        applicant = RecruitmentUser.objects.create_user(
            username=f"cos-applicant-{suffix}",
            password="testpass123",
            email=f"cos.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Cos{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"cos.{suffix}@example.com",
            applicant_phone="09177770001",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="COS pool candidate.",
            cover_letter="Applying for the COS vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application,
            applicant,
            content_prefix=f"cos-{suffix}",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _continuous_candidate_awaiting_screening(self, suffix):
        return self._submitted_cos_application(self.cos_continuous_position, suffix)

    def _continuous_candidate_at_exam(self, suffix):
        application = self._submitted_cos_application(self.cos_continuous_position, suffix)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        return application

    def _two_pooling_cos_candidates(self):
        first = self._submitted_cos_application(self.cos_position, "pool-a")
        second = self._submitted_cos_application(self.cos_position, "pool-b")
        return first, second

    # ── SL-02: need-based COS selection rounds ────────────────────────────────
    def _continuous_candidate(self, suffix):
        """A continuous-COS candidate driven to the HRM Chief deliberation section."""
        application = self._submitted_cos_application(self.cos_continuous_position, suffix)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        return application

    def test_an_open_pool_pick_opens_the_next_round_when_none_is_open(self):
        application = self._continuous_candidate("a")
        self.assertEqual(get_current_workflow_section(application), "deliberation")
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                application.case.id,
                "Picked without opening a round first.",
                finalize=True,
            )
        rounds = CosSelectionRound.objects.filter(recruitment_entry=self.cos_continuous_position)
        self.assertEqual(rounds.count(), 1)
        self.assertEqual(rounds.get().slot_count, 1)

    def test_opening_a_round_snapshots_the_candidates_eligible_at_that_moment(self):
        first = self._continuous_candidate("a")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position,
            self.hrm_chief,
            slot_count=1,
            notes="One slot opened in Finance.",
        )
        # A candidate who becomes eligible AFTER the round opened is not in it.
        later = self._continuous_candidate("b")

        self.assertEqual(selection_round.round_number, 1)
        member_case_ids = set(
            selection_round.members.values_list("recruitment_case_id", flat=True)
        )
        self.assertEqual(member_case_ids, {first.case.id})
        self.assertNotIn(later.case.id, member_case_ids)
        pool_ids = {a.pk for a in cos_deliberation_pool(self.cos_continuous_position)}
        self.assertEqual(pool_ids, {first.pk})
        # SL-02: the later candidate cannot be picked in a round they never joined.
        with self.assertRaisesMessage(
            ValueError, "The chosen applicant must be one of this vacancy's candidates."
        ):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                later.case.id,
                "Wrong round.",
                finalize=True,
            )

    def test_unpicked_continuous_candidates_stay_in_the_pool_for_the_next_round(self):
        # SL-02: the client keeps unpicked candidates on file and calls them when a slot
        # opens. Losing a round is not a rejection.
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "First hire.",
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(first.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        # The unpicked candidate is untouched — still active, still at deliberation.
        self.assertNotEqual(second.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(second.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(get_current_workflow_section(second), "deliberation")
        self.assertFalse(
            NotificationLog.objects.filter(
                application=second,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )
        self.assertIsNone(get_open_cos_selection_round(self.cos_continuous_position))
        self.assertEqual(
            {a.pk for a in cos_eligible_pool(self.cos_continuous_position)}, {second.pk}
        )

    def test_a_later_candidate_enters_a_new_round_and_can_be_hired(self):
        # SL-02 AC#2: a later continuous candidate joins a NEW cycle rather than
        # inheriting the outcome of one they never took part in.
        first = self._continuous_candidate("a")
        open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "First hire.",
                finalize=True,
            )
        later = self._continuous_candidate("b")
        later.refresh_from_db()
        # The old pick does not push the later candidate to a decision.
        self.assertEqual(get_current_workflow_section(later), "deliberation")
        self.assertFalse(user_can_record_final_decision(self.hrm_chief, later))

        second_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=1
        )
        self.assertEqual(second_round.round_number, 2)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                later.case.id,
                "Second hire.",
                finalize=True,
            )
        later.refresh_from_db()
        self.assertEqual(later.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(
            get_cos_pick_for_case(later.case).selection_round_id, second_round.id
        )

    def test_a_multi_slot_round_fills_every_slot_before_it_resolves(self):
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        third = self._continuous_candidate("c")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "Hire one.",
                finalize=True,
            )
        selection_round.refresh_from_db()
        self.assertTrue(selection_round.is_open)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                second.case.id,
                "Hire two.",
                finalize=True,
            )
        selection_round.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()

        self.assertTrue(selection_round.is_resolved)
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(second.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(get_current_workflow_section(third), "deliberation")

    def test_a_round_cannot_exceed_its_slot_count(self):
        first = self._continuous_candidate("a")
        second = self._continuous_candidate("b")
        round_one = open_cos_selection_round(self.cos_continuous_position, self.hrm_chief, slot_count=1)
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position, self.hrm_chief, first.case.id, "Only hire.", finalize=True
            )
        # The second pick can't join the filled round; it opens round 2 instead.
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position, self.hrm_chief, second.case.id, "Next slot.", finalize=True
            )
        self.assertEqual(round_one.picks.filter(is_finalized=True, is_rescinded=False).count(), 1)
        self.assertEqual(CosSelectionRound.objects.filter(recruitment_entry=self.cos_continuous_position).count(), 2)

    def test_single_batch_cos_still_closes_the_unpicked_pool_once(self):
        # SL-02: opening/pooling COS keeps single-batch behaviour — one round, and the
        # candidates it does not pick are closed as not selected.
        application = self._cos_case_at_deliberation()
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                application.case.id,
                "The single-batch pick.",
                finalize=True,
            )
        selection_round = get_latest_cos_selection_round(self.cos_position)
        self.assertIsNotNone(selection_round)
        self.assertEqual(selection_round.round_number, 1)
        self.assertTrue(selection_round.is_resolved)
        self.assertIsNone(get_open_cos_selection_round(self.cos_position))
        self.assertFalse(
            user_can_open_cos_selection_round(self.hrm_chief, self.cos_position)
        )

    def test_continuous_round_can_close_short_and_return_unpicked_members_to_pool(self):
        first = self._continuous_candidate("short-a")
        second = self._continuous_candidate("short-b")
        third = self._continuous_candidate("short-c")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position,
            self.hrm_chief,
            slot_count=2,
            notes="Two positions were expected.",
        )
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        console_url = reverse(
            "vacancy-batch-cos-deliberation",
            args=[self.cos_continuous_position.pk],
        )
        response = client.get(console_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Close this round short")
        self.assertContains(response, 'name="close_short_reason"')

        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "One appointment was enough.",
                finalize=True,
            )
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                console_url,
                {
                    "operation": "close_round_short",
                    "close_short_reason": "The second funded position was withdrawn.",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "closed with 1 of 2 slots filled")

        selection_round.refresh_from_db()
        self.assertEqual(
            selection_round.status, CosSelectionRound.Status.CLOSED_SHORT
        )
        self.assertEqual(
            selection_round.closed_short_reason,
            "The second funded position was withdrawn.",
        )
        audit = AuditLog.objects.get(
            action=AuditLog.Action.COS_SELECTION_ROUND_CLOSED_SHORT,
            application__isnull=True,
        )
        self.assertEqual(audit.metadata["round_number"], 1)
        self.assertEqual(audit.metadata["slot_count"], 2)
        self.assertEqual(audit.metadata["picks_finalized"], 1)
        self.assertEqual(
            audit.metadata["members"],
            sorted([first.case.id, second.case.id, third.case.id]),
        )
        self.assertEqual(
            audit.metadata["reason"], "The second funded position was withdrawn."
        )

        for application in (second, third):
            application.refresh_from_db()
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )
            self.assertEqual(get_current_workflow_section(application), "deliberation")
        self.assertEqual(
            {application.pk for application in cos_eligible_pool(self.cos_continuous_position)},
            {second.pk, third.pk},
        )
        self.assertTrue(
            user_can_open_cos_selection_round(
                self.hrm_chief, self.cos_continuous_position
            )
        )
        second_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        self.assertEqual(second_round.round_number, 2)
        self.assertEqual(
            set(second_round.members.values_list("recruitment_case_id", flat=True)),
            {second.case.id, third.case.id},
        )

    def test_single_batch_round_can_close_short_and_notify_unpicked_members(self):
        first, second = self._two_pooling_cos_candidates()
        third = self._submitted_cos_application(self.cos_position, "pool-short-c")
        for application in (first, second, third):
            self.finalize_screening_for_current_stage(application, self.secretariat)
        for application in (first, second, third):
            self.finalize_exam_for_current_stage(application, self.secretariat)
            application.refresh_from_db()
        for application in (first, second, third):
            self.finalize_screening_for_current_stage(application, self.hrm_chief)
        for application in (first, second, third):
            self.finalize_exam_for_current_stage(application, self.hrm_chief)
            application.refresh_from_db()
        for application in (first, second, third):
            self.finalize_interview_for_current_stage(application, self.hrm_chief)
            application.refresh_from_db()

        save_cos_vacancy_deliberation(
            self.cos_position,
            self.hrm_chief,
            first.case.id,
            "Draft the first of two expected picks.",
            finalize=False,
        )
        selection_round = get_open_cos_selection_round(self.cos_position)
        selection_round.slot_count = 2
        selection_round.save(update_fields=["slot_count", "updated_at"])
        with self.captureOnCommitCallbacks(execute=True):
            save_cos_vacancy_deliberation(
                self.cos_position,
                self.hrm_chief,
                first.case.id,
                "Finalize the only appointment.",
                finalize=True,
            )
        for application in (second, third):
            application.refresh_from_db()
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )

        with self.captureOnCommitCallbacks(execute=True):
            close_cos_selection_round_short(
                self.cos_position,
                self.hrm_chief,
                "The office will fill only one position from this batch.",
            )
        selection_round.refresh_from_db()
        self.assertEqual(
            selection_round.status, CosSelectionRound.Status.CLOSED_SHORT
        )
        for application in (second, third):
            application.refresh_from_db()
            self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.REJECTED
            )
            self.assertTrue(
                NotificationLog.objects.filter(
                    application=application,
                    notification_type=(
                        NotificationLog.NotificationType.NON_SELECTED_APPLICANT
                    ),
                ).exists()
            )
        first.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.APPROVED)
        self.assertFalse(
            user_can_open_cos_selection_round(self.hrm_chief, self.cos_position)
        )
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        self.assertEqual(
            client.get(
                reverse(
                    "vacancy-batch-cos-deliberation", args=[self.cos_position.pk]
                )
            ).status_code,
            200,
        )

    def test_close_short_guards_and_open_round_candidate_bound(self):
        first = self._continuous_candidate("guard-a")
        self._continuous_candidate("guard-b")
        with self.assertRaisesMessage(
            ValueError,
            "This round cannot fill more positions than there are eligible candidates (2).",
        ):
            open_cos_selection_round(
                self.cos_continuous_position, self.hrm_chief, slot_count=5
            )

        selection_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        self.assertTrue(
            user_can_close_cos_selection_round_short(
                self.hrm_chief, self.cos_continuous_position
            )
        )
        with self.assertRaisesMessage(
            ValueError, "Record why this COS selection round is being closed short."
        ):
            close_cos_selection_round_short(
                self.cos_continuous_position, self.hrm_chief, "   "
            )
        with self.assertRaisesMessage(
            ValueError, "Only the HRM Chief may close a COS selection round short."
        ):
            close_cos_selection_round_short(
                self.cos_continuous_position, self.secretariat, "Office decision."
            )
        close_cos_selection_round_short(
            self.cos_continuous_position, self.hrm_chief, "No appointment this round."
        )
        selection_round.refresh_from_db()
        self.assertEqual(
            selection_round.status, CosSelectionRound.Status.CLOSED_SHORT
        )
        first.refresh_from_db()
        self.assertEqual(first.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        with self.assertRaisesMessage(
            ValueError, "This vacancy has no open COS selection round to close."
        ):
            close_cos_selection_round_short(
                self.cos_continuous_position, self.hrm_chief, "Again."
            )

    def test_only_the_latest_round_pick_can_be_rescinded(self):
        first = self._continuous_candidate("latest-a")
        second = self._continuous_candidate("latest-b")
        third = self._continuous_candidate("latest-c")
        first_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=1
        )
        with self.captureOnCommitCallbacks(execute=True):
            old_pick = save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "Round one appointment.",
                finalize=True,
            )
        second_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        self.assertFalse(
            user_can_rescind_cos_vacancy_deliberation(self.hrm_chief, old_pick)
        )
        refusal = (
            "Only the most recent selection round's pick can be rescinded. Earlier rounds "
            "are history; run a new round instead."
        )
        with self.assertRaisesMessage(ValueError, refusal):
            rescind_cos_vacancy_deliberation(
                old_pick, self.hrm_chief, "Trying to reopen history."
            )
        first_round.refresh_from_db()
        second_round.refresh_from_db()
        self.assertEqual(first_round.status, CosSelectionRound.Status.RESOLVED)
        self.assertEqual(second_round.status, CosSelectionRound.Status.OPEN)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", args=[first.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("cos_pick_rescind_form"))
        self.assertEqual(
            response.context.get("cos_pick_rescind_unavailable_message"), refusal
        )
        self.assertContains(response, "Only the most recent selection round")
        self.assertContains(response, "Earlier rounds are history")
        self.assertIsNotNone(second)
        self.assertIsNotNone(third)

    def test_rescinding_latest_closed_short_pick_reopens_the_same_round(self):
        first = self._continuous_candidate("reopen-short-a")
        self._continuous_candidate("reopen-short-b")
        selection_round = open_cos_selection_round(
            self.cos_continuous_position, self.hrm_chief, slot_count=2
        )
        with self.captureOnCommitCallbacks(execute=True):
            pick = save_cos_vacancy_deliberation(
                self.cos_continuous_position,
                self.hrm_chief,
                first.case.id,
                "One appointment.",
                finalize=True,
            )
        close_cos_selection_round_short(
            self.cos_continuous_position,
            self.hrm_chief,
            "The second position is no longer funded.",
        )
        self.assertTrue(
            user_can_rescind_cos_vacancy_deliberation(self.hrm_chief, pick)
        )
        with self.captureOnCommitCallbacks(execute=True):
            rescind_cos_vacancy_deliberation(
                pick, self.hrm_chief, "The selected applicant declined."
            )
        selection_round.refresh_from_db()
        self.assertEqual(selection_round.status, CosSelectionRound.Status.OPEN)
        self.assertEqual(selection_round.closed_short_reason, "")
        self.assertIsNone(selection_round.resolved_at)
        self.assertIsNone(selection_round.resolved_by)

    def test_cos_rescind_view_turns_a_round_conflict_into_a_clear_message(self):
        pick, winner, _sibling = self._cos_pick_with_a_closed_sibling()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        with patch(
            "recruitment.views.rescind_cos_vacancy_deliberation",
            side_effect=IntegrityError("one open round"),
        ):
            response = client.post(
                reverse("cos-pick-rescind", args=[winner.pk]),
                {"reason": "Correct the pick."},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "This pick cannot be rescinded right now because another selection round is open.",
        )
        pick.refresh_from_db()
        self.assertFalse(pick.is_rescinded)

    def test_user_full_name_falls_back_to_username_for_display(self):
        self.assertEqual(self.applicant.get_full_name(), self.applicant.username)

    def test_hrms_prepares_car_draft_from_finalized_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)

        draft_report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft CAR prepared for HRMPSB review."},
            finalize=False,
        )
        draft_item = draft_report.items.get(recruitment_case=application.case)

        self.assertFalse(draft_report.is_finalized)
        self.assertEqual(draft_report.version_number, 1)
        self.assertIsNone(draft_item.deliberation_record)
        self.assertEqual(str(draft_item.interview_average_score), "100.00")
        # SL-03: no ETE yet, so no computed score — and therefore no rank. A draft may be
        # incomplete; what it may not do is imply a merit ordering it cannot support.
        self.assertIsNone(draft_item.assessment_score)
        self.assertIsNone(draft_item.rank_order)

        set_application_ete_rating(application, self.secretariat, Decimal("90"))
        ranked_report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft CAR with the ETE rating entered."},
            finalize=False,
        )
        ranked_item = ranked_report.items.get(recruitment_case=application.case)
        self.assertIsNotNone(ranked_item.assessment_score)
        self.assertEqual(ranked_item.rank_order, 1)

    def test_car_autosave_does_not_generate_report_versions_or_files(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_applicant_pool_for_test(application.position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "autosave before draft"},
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).exists()
        )
        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).exists()
        )

        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "manual draft generation"},
        )
        self.assertEqual(response.status_code, 302)
        draft_report = ComparativeAssessmentReport.objects.get(
            recruitment_entry=application.position,
            review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
        )
        self.assertEqual(draft_report.version_number, 1)
        self.assertEqual(draft_report.summary_notes, "manual draft generation")
        self.assertEqual(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).count(),
            1,
        )

        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "autosaved draft notes"},
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )

        self.assertEqual(response.status_code, 204)
        draft_report.refresh_from_db()
        self.assertEqual(draft_report.summary_notes, "autosaved draft notes")
        self.assertEqual(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).count(),
            1,
        )
        self.assertEqual(
            EvidenceVaultItem.objects.filter(
                recruitment_entry=application.position,
                artifact_type="comparative_assessment_report",
            ).count(),
            1,
        )

    def test_car_generation_requires_finalized_applicant_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_applicant_pool_for_test(self.level1_position)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        self.level1_position.status = PositionPosting.EntryStatus.ACTIVE
        self.level1_position.closing_date = timezone.localdate() + timedelta(days=3)
        self.level1_position.save(update_fields=["status", "closing_date", "is_active", "updated_at"])
        application.position.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Plantilla deliberation and CAR generation are available only after the vacancy is closed",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={"summary_notes": "Attempted before pool finalization."},
                finalize=True,
            )

    def test_secretariat_prepares_level1_car_from_finalized_hrmpsb_outputs(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb, rating_score="91.25")
        self.finalize_deliberation_for_current_stage(
            application,
            self.hrmpsb,
            ranking_position=1,
            decision_support_summary="HRMPSB recommendation summary for the CAR.",
        )

        self.assertTrue(user_can_view_application(self.secretariat, application))
        self.assertTrue(user_can_manage_comparative_assessment_report(self.secretariat, application))
        self.assertFalse(user_can_manage_comparative_assessment_report(self.hrmpsb, application))
        self.assertTrue(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())
        self.ensure_ete_ratings_for_pool(application)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Prepared by HRMS from finalized HRMPSB records.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )
        item = report.items.get(recruitment_case=application.case)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.recruitment_entry, self.level1_position)
        self.assertEqual(report.generated_by, self.secretariat)
        self.assertEqual(report.finalized_by, self.secretariat)
        self.assertEqual(report.generated_by_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(item.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED)
        self.assertEqual(item.exam_status, ExamRecord.ExamStatus.COMPLETED)
        self.assertEqual(str(item.exam_score), "88.50")
        self.assertEqual(str(item.interview_average_score), "100.00")
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["document_review_outcome"],
            ScreeningRecord.QualificationOutcome.QUALIFIED,
        )
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["finalized_exam_count"],
            1,
        )
        self.assertEqual(
            report.consolidated_snapshot["ranked_candidates"][0]["finalized_interview_count"],
            1,
        )

    def test_secretariat_prepares_level2_car_and_hrm_chief_cannot_take_it(self):
        # WF-04 (client decision): the Secretariat generates the CAR for BOTH levels;
        # the HRM Chief no longer prepares the Level-2 CAR.
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        self.assertTrue(user_can_manage_comparative_assessment_report(self.secretariat, application))
        self.assertFalse(user_can_manage_comparative_assessment_report(self.hrm_chief, application))
        self.assertTrue(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())
        self.assertFalse(get_queue_for_user(self.hrm_chief).filter(pk=application.pk).exists())

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for CAR preparation.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={"summary_notes": "HRM Chief should not prepare this CAR."},
                finalize=False,
            )

        self.ensure_ete_ratings_for_pool(application)
        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Level 2 CAR prepared by the Secretariat (WF-04).",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.generated_by, self.secretariat)
        self.assertEqual(report.finalized_by, self.secretariat)
        self.assertEqual(report.generated_by_role, RecruitmentUser.Role.SECRETARIAT)

    def test_hrmpsb_member_cannot_prepare_car_after_deliberation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for CAR preparation.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.hrmpsb,
                cleaned_data={"summary_notes": "HRMPSB should not prepare this CAR."},
                finalize=True,
            )

    def test_car_preserves_policy_preliminary_ranking_and_hrmpsb_ranking_notes(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="prelim-rank-secondary",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="prelim.rank.secondary@example.com",
            applicant_phone="09179990222",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for preliminary ranking.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="prelim-rank-secondary",
        )

        for application, applicant in (
            (primary_application, self.applicant),
            (secondary_application, secondary_applicant),
        ):
            otp_code = issue_application_otp(application, actor=applicant)
            verify_application_otp(application, otp_code, actor=applicant)
            application.refresh_from_db()
            submit_application(application, applicant)
            application.refresh_from_db()

        # Finalize the whole vacancy's screening before any exam — the batch screening gate
        # holds the exam batch until every submitted applicant has a screening decision.
        self.finalize_screening_for_current_stage(
            primary_application,
            self.secretariat,
            education_score="90.00",
            training_score="90.00",
            experience_score="90.00",
        )
        self.finalize_screening_for_current_stage(
            secondary_application,
            self.secretariat,
            education_score="80.00",
            training_score="80.00",
            experience_score="80.00",
        )
        self.finalize_exam_for_current_stage(
            primary_application,
            self.secretariat,
            exam_score="90.00",
            technical_score="90.00",
            general_score="90.00",
        )
        primary_application.refresh_from_db()
        self.finalize_exam_for_current_stage(
            secondary_application,
            self.secretariat,
            exam_score="80.00",
            technical_score="80.00",
            general_score="80.00",
        )
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb, rating_score="90.00")
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb, rating_score="80.00")
        self.finalize_deliberation_for_current_stage(
            primary_application,
            self.hrmpsb,
            ranking_position=2,
            decision_support_summary="Higher preliminary score noted.",
            ranking_notes="HRMPSB placed this candidate second after considering office-fit concerns.",
        )
        self.finalize_deliberation_for_current_stage(
            secondary_application,
            self.hrmpsb,
            ranking_position=1,
            decision_support_summary="Panel recommended this candidate despite lower preliminary score.",
            ranking_notes="Panel justification recorded for the rank adjustment.",
        )

        report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "CAR includes advisory preliminary ranking.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": secondary_application.case.id,
            },
            finalize=True,
        )
        primary_item = report.items.get(recruitment_case=primary_application.case)
        secondary_item = report.items.get(recruitment_case=secondary_application.case)

        # Ranking is purely computed now (no HRMPSB override) — higher score ranks first.
        self.assertEqual(primary_item.rank_order, 1)
        self.assertEqual(primary_item.preliminary_rank_order, 1)
        # interview 90->level 4->100: 90*0.20 + 90*0.40 + 100*0.40 = 94.00
        self.assertEqual(str(primary_item.assessment_score), "94.00")
        self.assertEqual(str(primary_item.document_review_score), "90.00")
        self.assertEqual(secondary_item.rank_order, 2)
        self.assertEqual(secondary_item.preliminary_rank_order, 2)
        # interview 80->level 3->75: 80*0.20 + 80*0.40 + 75*0.40 = 78.00
        self.assertEqual(str(secondary_item.assessment_score), "78.00")
        self.assertEqual(
            report.consolidated_snapshot["assessment_weight_display"],
            "Document review 40%, exam 20%, interview 40%.",
        )

    def test_car_ete_blank_until_rating_entered_for_new_records(self):
        # RD-3: the screening form no longer collects ETE scores, so a fresh
        # case reaches the CAR with no fallback value — the assessment score
        # stays blank until the Secretariat enters the ETE rating there, and
        # then computes. One source: the CAR grades, screening only verifies.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        screening_record = application.screening_records.latest("created_at")
        self.assertIsNone(screening_record.document_review_score)

        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft before any ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNone(item.assessment_score)

        set_application_ete_rating(application, self.secretariat, Decimal("70"))
        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft after the ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNotNone(item.assessment_score)

    def test_car_uses_manual_ete_rating_when_set(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        # Finalizes the applicant pool and stages an initial CAR draft.
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()
        set_application_ete_rating(application, self.secretariat, Decimal("60"))

        draft = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft with a manual ETE rating."},
            finalize=False,
        )
        item = draft.items.get(recruitment_case=application.case)

        self.assertEqual(item.ete_rating, Decimal("60.00"))
        # Overall uses the manual ETE (40%) instead of the screening score.
        expected = (
            Decimal("60") * Decimal("0.40")
            + item.exam_score * Decimal("0.20")
            + item.interview_average_score * Decimal("0.40")
        ).quantize(Decimal("0.01"))
        self.assertEqual(item.assessment_score, expected)

    def test_car_view_saves_posted_ete_ratings(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {
                "operation": "save",
                "summary_notes": "Draft with ETE entered on screen.",
                f"ete_{application.case.id}": "65",
            },
        )

        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertEqual(application.ete_rating.rating, Decimal("65.00"))
        draft = (
            ComparativeAssessmentReport.objects.filter(recruitment_entry=application.position)
            .order_by("-version_number")
            .first()
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertEqual(item.ete_rating, Decimal("65.00"))

    def test_car_view_saves_component_ete_ratings_with_required_basis(self):
        # WF-05: the CAR draft collects Education/Training/Experience (0–100 each)
        # plus a required one-line basis per component; the stored rating is the
        # Plantilla 40/30/30 composition.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        application.refresh_from_db()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        case_id = application.case.id
        payload = {
            "operation": "save",
            "summary_notes": "Draft with component ETE.",
            f"ete_education_{case_id}": "90",
            f"ete_training_{case_id}": "80",
            f"ete_experience_{case_id}": "70",
            f"ete_education_basis_{case_id}": "Master's degree; only Bachelor's required",
            f"ete_training_basis_{case_id}": "40 hours of relevant training",
            # experience basis intentionally blank first
        }
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            payload,
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "each ETE component needs its basis on record")
        self.assertFalse(ApplicationETERating.objects.filter(application=application).exists())

        payload[f"ete_experience_basis_{case_id}"] = "Three years of directly relevant experience"
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            payload,
        )
        self.assertEqual(response.status_code, 302)
        rating = ApplicationETERating.objects.get(application=application)
        # 90×.40 + 80×.30 + 70×.30 = 81.00 (Plantilla composition).
        self.assertEqual(rating.rating, Decimal("81.00"))
        self.assertEqual(rating.education_score, Decimal("90.00"))
        self.assertEqual(rating.training_score, Decimal("80.00"))
        self.assertEqual(rating.experience_score, Decimal("70.00"))
        self.assertEqual(rating.education_basis, "Master's degree; only Bachelor's required")
        draft = (
            ComparativeAssessmentReport.objects.filter(recruitment_entry=application.position)
            .order_by("-version_number")
            .first()
        )
        item = draft.items.get(recruitment_case=application.case)
        self.assertEqual(item.ete_rating, Decimal("81.00"))

    def test_car_finalize_derives_quorum_from_members_and_records_deliberation_date(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="quorum_met"')
        self.assertContains(response, 'name="members_present"')
        self.assertContains(response, 'name="deliberated_on"')

        with self.assertRaisesMessage(
            ValueError,
            "Record how many HRMPSB members were present (3–5).",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Too few members.",
                    "members_present": 2,
                    "deliberated_on": timezone.localdate(),
                    "recommended_case_id": application.case.id,
                },
                finalize=True,
            )

        deliberated_on = timezone.localdate() - timedelta(days=1)
        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Finalized CAR.",
                "members_present": 3,
                "deliberated_on": deliberated_on,
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertIsNone(report.quorum_met)
        self.assertEqual(report.members_present, 3)
        self.assertEqual(report.deliberated_on, deliberated_on)

    def test_car_finalize_requires_the_deliberation_date(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        with self.assertRaisesMessage(
            ValueError,
            "Record the deliberation date before finalizing the CAR.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Date omitted.",
                    "members_present": 3,
                    "recommended_case_id": application.case.id,
                },
                finalize=True,
            )
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position,
                is_finalized=True,
            ).exists()
        )

    def test_car_cannot_finalize_with_a_blank_formula_component(self):
        # SL-03: the audit's probe — a candidate with no ETE finalized at rank 1 and was
        # recommended to the Appointing Authority with nothing behind 40% of the formula.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        readiness = get_comparative_assessment_readiness(application)
        self.assertFalse(readiness["can_finalize"])
        self.assertIn("#DIV/0!", readiness["finalize_block_message"])
        self.assertIn(application.reference_label, readiness["finalize_block_message"])

        with self.assertRaisesMessage(
            ValueError,
            "Every candidate needs a complete score before the CAR can be finalized",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Finalizing without an ETE rating.",
                    "members_present": 4,
                    "deliberated_on": timezone.localdate(),
                    "recommended_case_id": application.case.id,
                },
                finalize=True,
            )
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position, is_finalized=True
            ).exists()
        )
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW
        )

        # With the ETE entered the same finalize succeeds and the case routes forward.
        self.ensure_ete_ratings_for_pool(application)
        self.assertTrue(
            get_comparative_assessment_readiness(application)["can_finalize"]
        )
        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Complete CAR.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": application.case.id,
            },
            finalize=True,
        )
        item = report.items.get(recruitment_case=application.case)
        self.assertIsNotNone(item.assessment_score)
        self.assertEqual(item.rank_order, 1)

    def test_equal_totals_share_a_rank_and_stay_inside_the_top_five(self):
        # SL-08: the audit's probe — two candidates with identical ETE, exam and interview
        # totals were split 1 and 2 by their case reference, which is not a merit
        # distinction and could push one of them outside the top five.
        first = self.make_application(self.level1_position)
        second_applicant = RecruitmentUser.objects.create_user(
            username="tie-candidate",
            password="testpass123",
            email="tie.candidate@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        second = RecruitmentApplication.objects.create(
            applicant=second_applicant,
            position=self.level1_position,
            applicant_first_name="Tie",
            applicant_last_name="Candidate",
            applicant_email="tie.candidate@example.com",
            applicant_phone="09179990333",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Equal-scoring applicant.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            second, second_applicant, content_prefix="tie-candidate"
        )
        self.verify_application_for_submission(second)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(second, second_applicant)
        self.verify_application_for_submission(first)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(first, self.applicant)
        first.refresh_from_db()
        second.refresh_from_db()
        # The batch screening gate holds the exam until every applicant has a decision.
        self.finalize_screening_for_current_stage(first, self.secretariat)
        self.finalize_screening_for_current_stage(second, self.secretariat)
        self.finalize_exam_for_current_stage(first, self.secretariat)
        self.finalize_exam_for_current_stage(second, self.secretariat)
        first.refresh_from_db()
        second.refresh_from_db()
        self.finalize_interview_for_current_stage(first, self.hrmpsb, rating_score="90.00")
        self.finalize_interview_for_current_stage(second, self.hrmpsb, rating_score="90.00")
        self.finalize_applicant_pool_for_test(self.level1_position)
        first.refresh_from_db()
        second.refresh_from_db()

        for application in (first, second):
            set_application_ete_rating(application, self.secretariat, Decimal("90"))
        report = generate_comparative_assessment_report(
            application=first,
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Draft with two equal candidates."},
            finalize=False,
        )
        items = list(report.items.all())
        self.assertEqual(len(items), 2)
        scores = {item.assessment_score for item in items}
        self.assertEqual(len(scores), 1, "fixture should produce equal totals")
        self.assertEqual({item.rank_order for item in items}, {1})
        for item in items:
            self.assertTrue(item.shares_rank)
            self.assertTrue(car_item_is_within_top_five(item))
        # SL-08 AC#3: either member of a rank-1 tie is the board's ordering, so picking
        # one is not a deviation needing deep-selection paperwork.
        self.assertEqual(
            car_top_rank_case_ids(report),
            {first.case.id, second.case.id},
        )

    def test_the_ete_grid_shows_the_qs_and_the_candidates_own_credentials(self):
        # SL-08A: ETE is 40% of the CAR and the scorer was being asked for that verdict
        # with nothing on screen but a candidate code and three empty boxes.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Minimum qualifications for this position")
        self.assertContains(response, "credentials on file")
        self.assertContains(response, application.qualification_summary)

    def test_an_ete_rating_keeps_the_facts_it_was_judged_against(self):
        # SL-08A AC#3: a later QS edit or resubmission must not rewrite the basis of a
        # rating already given.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        rating = set_application_ete_rating(application, self.secretariat, Decimal("88"))
        snapshot = rating.source_facts_snapshot

        self.assertIn("captured_at", snapshot)
        self.assertEqual(
            snapshot["qualification_summary"], application.qualification_summary
        )
        self.assertTrue(snapshot["documents"])

        # Editing the position's QS afterwards leaves the recorded basis alone.
        application.position.qualification_reference = "Revised QS text."
        application.position.save(update_fields=["qualification_reference", "updated_at"])
        rating.refresh_from_db()
        self.assertNotEqual(
            rating.source_facts_snapshot.get("qualification_reference"),
            "Revised QS text.",
        )

    def test_an_unscored_car_row_cannot_be_the_board_recommendation(self):
        # SL-03 AC#2: no rank, no recommendation, no route forward without a score.
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        draft = ComparativeAssessmentReport.objects.filter(
            recruitment_entry=application.position, is_finalized=False
        ).first()
        item = draft.items.get(recruitment_case=application.case)
        self.assertIsNone(item.assessment_score)
        self.assertIsNone(item.rank_order)

    def test_car_finalize_requires_a_recommended_applicant(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        with self.assertRaisesMessage(
            ValueError,
            "Record the HRMPSB board's recommendation before finalizing the CAR.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "No recommendation provided.",
                    "members_present": 4,
                    "deliberated_on": timezone.localdate(),
                },
                finalize=True,
            )
        # No finalized CAR should have been created.
        self.assertFalse(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=application.position, is_finalized=True
            ).exists()
        )

    def test_car_finalize_records_the_board_recommendation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Finalized CAR with recommendation.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": application.case.id,
                "recommendation_notes": "Strongest competency profile in the pool.",
            },
            finalize=True,
        )

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.recommended_case_id, application.case.id)
        self.assertEqual(report.recommendation_notes, "Strongest competency profile in the pool.")

    def test_board_can_recommend_none_with_notes_and_every_pick_is_a_deviation(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertContains(response, '— Board recommends no candidate —')

        with self.assertRaisesMessage(
            ValueError,
            "Explain why the board recommends no candidate before finalizing the CAR.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Board recommendation pending.",
                    "members_present": 3,
                    "deliberated_on": timezone.localdate(),
                    "recommended_case_id": "none",
                    "recommendation_notes": "",
                },
                finalize=True,
            )

        report = generate_comparative_assessment_report(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Board concluded the search.",
                "members_present": 3,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": "none",
                "recommendation_notes": "No candidate met the board's full expectations.",
            },
            finalize=True,
        )
        self.assertTrue(report.recommends_none)
        self.assertIsNone(report.recommended_case_id)
        self.assertEqual(final_selection_recommended_case_ids(report), set())

        application.refresh_from_db()
        item = report.items.get(recruitment_case=application.case)
        form = FinalSelectionForm(
            {
                "selected_item": item.id,
                "decision_notes": "Attempted without a deviation reason.",
            },
            report=report,
        )
        self.assertFalse(form.is_valid())
        self.assertIn(
            "This selection differs from the board's recorded recommendation.",
            form.errors["deep_selection_justification"][0],
        )
        with self.assertRaisesMessage(
            ValueError,
            "This selection differs from the board's recorded recommendation.",
        ):
            record_final_selection(
                application,
                self.appointing,
                {
                    "selected_item": item,
                    "decision_notes": "Attempted without a deviation reason.",
                },
            )

        self.force_login_with_mfa(client, self.appointing)
        detail_response = client.get(
            reverse("application-detail", kwargs={"pk": application.pk})
        )
        self.assertContains(detail_response, 'data-recommended="0"')
        print_response = client.get(
            reverse("comparative-assessment-print", kwargs={"pk": application.pk})
        )
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "Board recommendation: none")
        self.assertContains(print_response, "No candidate met the board&#x27;s full expectations.")
        self.assertContains(
            print_response,
            report.deliberated_on.strftime("%B %d, %Y"),
        )

        filing_client = Client()
        self.force_login_with_mfa(filing_client, self.secretariat)
        filing_response = filing_client.get(
            reverse("comparative-assessment-filing-copy", kwargs={"pk": application.position_id})
        )
        self.assertEqual(filing_response.status_code, 200)
        self.assertContains(filing_response, "Board recommendation: none")
        self.assertContains(
            filing_response,
            report.deliberated_on.strftime("%B %d, %Y"),
        )

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application,
                self.appointing,
                {
                    "selected_item": item,
                    "decision_notes": "Selected despite no board recommendation.",
                    "deep_selection_justification": (
                        "The appointing authority documented the independent selection basis."
                    ),
                },
            )
        self.assertEqual(selection.selected_item_id, item.id)

    def test_recommend_one_keeps_the_existing_non_deviation_path(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)
        report = get_latest_finalized_comparative_assessment_report(application)
        item = report.items.get(recruitment_case=application.case)

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application,
                self.appointing,
                {
                    "selected_item": item,
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        self.assertEqual(selection.selected_item_id, item.id)
        self.assertFalse(selection.deep_selection_justification)

    def test_car_finalize_rejects_a_recommendation_outside_the_pool(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.ensure_ete_ratings_for_pool(application)
        # A case from a different vacancy is not a valid recommendation.
        other = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(other)

        with self.assertRaisesMessage(
            ValueError,
            "The recommended applicant must be one of this vacancy's candidates.",
        ):
            generate_comparative_assessment_report(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "summary_notes": "Cross-vacancy recommendation.",
                    "members_present": 4,
                    "deliberated_on": timezone.localdate(),
                    "recommended_case_id": other.case.id,
                },
                finalize=True,
            )

    def test_car_generation_creates_versioned_evidence(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)

        report = self.finalize_car_for_current_stage(application, self.hrmpsb)

        self.assertTrue(report.is_finalized)
        self.assertEqual(report.version_number, 2)
        self.assertTrue(report.evidence_item.is_current_version)
        self.assertEqual(report.evidence_item.stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertEqual(report.evidence_item.artifact_scope, EvidenceVaultItem.OwnerScope.ENTRY)
        self.assertEqual(report.evidence_item.artifact_type, "comparative_assessment_report")
        self.assertIsNone(report.evidence_item.application_id)
        self.assertIsNone(report.evidence_item.recruitment_case_id)
        self.assertEqual(report.evidence_item.recruitment_entry_id, application.position_id)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CAR_FINALIZED,
            ).exists()
        )

    def test_finalized_car_and_effective_selection_block_weight_unlock(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)
        report = get_latest_finalized_comparative_assessment_report(application)
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        self.assertTrue(weights.is_locked)

        car_message = (
            "The assessment weights cannot be changed: a Comparative Assessment Report "
            "has been finalized on this split. Return the CAR for correction first."
        )
        with self.assertRaisesMessage(ValidationError, car_message):
            unlock_vacancy_assessment_weights(
                self.level1_position,
                self.secretariat,
                "Correct the recorded split.",
            )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        weights_url = reverse(
            "vacancy-assessment-weights",
            args=[self.level1_position.pk],
        )
        blocked_page = client.get(weights_url)
        self.assertContains(blocked_page, car_message)
        self.assertNotContains(blocked_page, "Unlock weights for correction")

        return_car_for_reassessment(
            application,
            self.appointing,
            "The CAR split needs a documented correction.",
        )
        unlocked = unlock_vacancy_assessment_weights(
            self.level1_position,
            self.secretariat,
            "Correct the split after the CAR return.",
        )
        self.assertTrue(unlocked.is_editable)

        lock_vacancy_assessment_weights(self.level1_position)
        item = report.items.first()
        selection = FinalSelection.objects.create(
            comparative_assessment_report=report,
            recruitment_entry=self.level1_position,
            selected_item=item,
            selected_application=item.application,
            selected_case=item.recruitment_case,
            decided_by=self.appointing,
            decision_notes="Effective selection used to protect the recorded split.",
        )
        selection_message = (
            "The assessment weights cannot be changed after a selection has been recorded."
        )
        with self.assertRaisesMessage(ValidationError, selection_message):
            unlock_vacancy_assessment_weights(
                self.level1_position,
                self.secretariat,
                "Attempt after selection.",
            )
        blocked_page = client.get(weights_url)
        self.assertContains(blocked_page, selection_message)
        self.assertNotContains(blocked_page, "Unlock weights for correction")

        selection.is_rescinded = True
        selection.save(update_fields=["is_rescinded", "updated_at"])
        unlocked = unlock_vacancy_assessment_weights(
            self.level1_position,
            self.secretariat,
            "Selection was rescinded and the CAR remains returned.",
        )
        self.assertTrue(unlocked.is_editable)

    def test_car_prints_and_filing_copy_use_the_report_snapshot_weights(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)
        report = get_latest_finalized_comparative_assessment_report(application)
        self.assertEqual(
            report.consolidated_snapshot["assessment_weights"],
            {
                "ete": "40.00",
                "exam": "20.00",
                "interview": "40.00",
                "exam_general": "60.00",
                "exam_technical": "40.00",
            },
        )

        return_car_for_reassessment(
            application,
            self.appointing,
            "Correct the assessment split for the next CAR version.",
        )
        weights = unlock_vacancy_assessment_weights(
            self.level1_position,
            self.secretariat,
            "The next CAR uses the corrected split.",
        )
        weights.ete_weight = Decimal("50.00")
        weights.exam_weight = Decimal("10.00")
        weights.interview_weight = Decimal("40.00")
        weights.full_clean()
        weights.save()

        report_weights = report.assessment_weights_for_display
        self.assertEqual(report_weights.ete_weight, Decimal("40.00"))
        self.assertEqual(report_weights.exam_weight, Decimal("20.00"))
        self.assertEqual(report_weights.interview_weight, Decimal("40.00"))

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with patch(
            "recruitment.views.get_comparative_assessment_report",
            return_value=report,
        ):
            print_response = client.get(
                reverse("comparative-assessment-print", kwargs={"pk": application.pk})
            )
        self.assertEqual(print_response.status_code, 200)
        self.assertEqual(print_response.context["weights"].ete_weight, Decimal("40.00"))
        self.assertContains(print_response, "ETE (40%)")
        self.assertContains(print_response, "Examination (20%)")
        self.assertContains(print_response, "Interview (40%)")

        with (
            patch(
                "recruitment.views.user_can_generate_entry_car_filing_copy",
                return_value=True,
            ),
            patch("recruitment.views.get_entry_finalized_car", return_value=report),
        ):
            filing_response = client.get(
                reverse(
                    "comparative-assessment-filing-copy",
                    kwargs={"pk": self.level1_position.pk},
                )
            )
        self.assertEqual(filing_response.status_code, 200)
        self.assertEqual(filing_response.context["weights"].exam_weight, Decimal("20.00"))
        self.assertContains(filing_response, "this CAR version was finalized")
        for row in filing_response.context["filing_rows"]:
            expected_eps = (row["item"].ete_rating * Decimal("0.40")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            self.assertEqual(row["eps"], expected_eps)

        pre_snapshot = dict(report.consolidated_snapshot)
        pre_snapshot.pop("assessment_weights")
        report.consolidated_snapshot = pre_snapshot
        report.save(update_fields=["consolidated_snapshot", "updated_at"])
        fallback = report.assessment_weights_for_display
        self.assertEqual(fallback.ete_weight, Decimal("50.00"))
        self.assertEqual(fallback.exam_weight, Decimal("10.00"))
        self.assertEqual(fallback.interview_weight, Decimal("40.00"))

    def test_car_generation_creates_versioned_entry_reports_across_candidates(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="car-secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="car.second.candidate@example.com",
            applicant_phone="09179990001",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for CAR reuse testing.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="car-secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)

        draft_report = ComparativeAssessmentReport.objects.get(
            recruitment_entry=self.level1_position,
            review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            is_finalized=False,
        )
        self.ensure_ete_ratings_for_pool(primary_application)
        finalized_report = generate_comparative_assessment_report(
            application=secondary_application,
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Final entry-level CAR.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": secondary_application.case.id,
            },
            finalize=True,
        )

        self.assertNotEqual(draft_report.pk, finalized_report.pk)
        self.assertEqual(
            ComparativeAssessmentReport.objects.filter(
                recruitment_entry=self.level1_position,
                review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            ).count(),
            2,
        )
        self.assertEqual(draft_report.version_number, 1)
        self.assertEqual(finalized_report.version_number, 2)
        self.assertFalse(draft_report.is_finalized)
        self.assertTrue(finalized_report.is_finalized)
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=finalized_report).count(),
            2,
        )

    def test_finalized_car_is_reused_for_other_candidates_in_same_entry(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="secondary-applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="second.candidate@example.com",
            applicant_phone="09179990000",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for the same Plantilla entry.",
            cover_letter="Applying for the same entry.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)
        shared_report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)

        secondary_packet = build_submission_packet(secondary_application)
        self.assertTrue(secondary_packet["summary"]["has_comparative_assessment_report"])
        self.assertEqual(
            ComparativeAssessmentReportItem.objects.filter(report=shared_report).count(),
            2,
        )

        secondary_application.refresh_from_db()
        self.assertEqual(
            secondary_application.status,
            RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
        )
        self.assertEqual(
            secondary_application.case.current_stage,
            RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
        )


class FinalDecisionHandlingTests(BaseRecruitmentTestCase):
    def prepare_ranked_level1_car(self, candidate_count):
        applications = [self.make_application(self.level1_position)]
        for index in range(2, candidate_count + 1):
            applicant = User.objects.create_user(
                username=f"ranked-car-applicant-{index}",
                password="testpass123",
                role=RecruitmentUser.Role.APPLICANT,
            )
            application = RecruitmentApplication.objects.create(
                applicant=applicant,
                position=self.level1_position,
                applicant_first_name=f"Candidate{index}",
                applicant_last_name="Ranked",
                applicant_email=f"ranked.car.{index}@example.com",
                applicant_phone=f"09179990{index:03d}",
                checklist_privacy_consent=True,
                checklist_documents_complete=True,
                checklist_information_certified=True,
                qualification_summary="Additional applicant for ranked CAR testing.",
                cover_letter="Applying for the same vacancy.",
                performance_rating_applicability=(
                    RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
                ),
            )
            self.upload_required_applicant_documents(
                application,
                applicant,
                content_prefix=f"ranked-car-{index}",
            )
            applications.append(application)

        for index, application in enumerate(applications, start=1):
            self.verify_application_for_submission(application)
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, application.applicant)
            application.refresh_from_db()
            score = f"{96 - index}.00"
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                education_score=score,
                training_score=score,
                experience_score=score,
            )
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_score=score,
                technical_score=score,
                general_score=score,
            )
            application.refresh_from_db()

        for index, application in enumerate(applications, start=1):
            self.finalize_interview_for_current_stage(
                application,
                self.hrmpsb,
                rating_score=f"{96 - index}.00",
            )
        for index, application in enumerate(applications, start=1):
            self.finalize_deliberation_for_current_stage(
                application,
                self.hrmpsb,
                ranking_position=index,
                decision_support_summary=f"HRMPSB support summary for rank {index}.",
                ranking_notes=f"HRMPSB ranking notes for rank {index}.",
            )

        report = self.finalize_car_for_current_stage(applications[0], self.hrmpsb)
        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
        return applications, report

    def test_final_selection_routes_selected_plantilla_case_to_completion_and_preserves_car(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        selection = self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected after reviewing the finalized CAR.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(selection.selected_application_id, application.id)
        self.assertEqual(selection.decided_by, self.appointing)
        self.assertTrue(selection.car_snapshot["comparative_assessment_report"]["items"])
        self.assertFalse(FinalDecision.objects.filter(application=application).exists())
        self.assertTrue(
            NotificationLog.objects.filter(
                application=application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )

    def test_final_selection_marks_other_car_applicants_not_selected(self):
        primary_application = self.make_application(self.level1_position)
        secondary_applicant = User.objects.create_user(
            username="final-selection-secondary",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        secondary_application = RecruitmentApplication.objects.create(
            applicant=secondary_applicant,
            position=self.level1_position,
            applicant_first_name="Second",
            applicant_last_name="Candidate",
            applicant_email="final.selection.secondary@example.com",
            applicant_phone="09179990123",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant for CAR final selection.",
            cover_letter="Applying for the same vacancy.",
        )
        self.upload_required_applicant_documents(
            secondary_application,
            secondary_applicant,
            content_prefix="final-selection-secondary",
        )

        self.move_application_to_hrmpsb_review(primary_application)
        otp_code = issue_application_otp(secondary_application, actor=secondary_applicant)
        verify_application_otp(secondary_application, otp_code, actor=secondary_applicant)
        secondary_application.refresh_from_db()
        submit_application(secondary_application, secondary_applicant)
        self.finalize_screening_for_current_stage(secondary_application, self.secretariat)
        self.finalize_exam_for_current_stage(secondary_application, self.secretariat)
        secondary_application.refresh_from_db()

        self.finalize_interview_for_current_stage(primary_application, self.hrmpsb)
        self.finalize_interview_for_current_stage(secondary_application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(primary_application, self.hrmpsb, ranking_position=1)
        self.finalize_deliberation_for_current_stage(secondary_application, self.hrmpsb, ranking_position=2)
        report = self.finalize_car_for_current_stage(primary_application, self.hrmpsb)
        primary_application.refresh_from_db()
        secondary_application.refresh_from_db()
        selected_item = report.items.get(recruitment_case=secondary_application.case)

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=primary_application,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "decision_notes": "Selected the second-ranked applicant from the CAR.",
                    # WF-03: deviating from the board's recommendation needs a justification.
                    "deep_selection_justification": (
                        "Documented justification for selecting against the board's recommendation."
                    ),
                },
            )

        primary_application.refresh_from_db()
        primary_application.case.refresh_from_db()
        secondary_application.refresh_from_db()
        secondary_application.case.refresh_from_db()

        self.assertEqual(FinalSelection.objects.filter(recruitment_entry=self.level1_position).count(), 1)
        self.assertEqual(selection.selected_application_id, secondary_application.id)
        self.assertEqual(secondary_application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(secondary_application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(secondary_application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(primary_application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(primary_application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertTrue(primary_application.case.is_stage_locked)
        self.assertFalse(FinalDecision.objects.filter(application__position=self.level1_position).exists())
        self.assertTrue(
            NotificationLog.objects.filter(
                application=secondary_application,
                notification_type=NotificationLog.NotificationType.SELECTED_APPLICANT,
            ).exists()
        )
        self.assertTrue(
            NotificationLog.objects.filter(
                application=primary_application,
                notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
            ).exists()
        )

    def test_appointing_authority_can_record_no_appointment_and_cases_remain_reopenable(self):
        applications, report = self.prepare_ranked_level1_car(3)
        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(
            reverse("application-detail", kwargs={"pk": applications[0].pk})
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "No appointment (failure of search)")
        self.assertContains(
            detail,
            "Every remaining candidate is told they were not selected. "
            "The vacancy can then be re-published as a new posting.",
        )
        self.assertContains(
            detail,
            reverse("no-appointment-record", kwargs={"pk": applications[0].pk}),
        )

        blank_response = client.post(
            reverse("no-appointment-record", kwargs={"pk": applications[0].pk}),
            {"reason": ""},
        )
        self.assertEqual(blank_response.status_code, 200)
        self.assertContains(blank_response, "This field is required.")
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.NO_APPOINTMENT_RECORDED).exists()
        )

        reason = "The appointing authority declared a failure of search."
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("no-appointment-record", kwargs={"pk": applications[0].pk}),
                {"reason": reason},
            )
        self.assertEqual(response.status_code, 302)
        redirected = client.get(response["Location"])
        self.assertEqual(redirected.status_code, 200)

        report.refresh_from_db()
        self.assertTrue(report.is_finalized)
        self.assertFalse(report.is_returned)
        self.assertFalse(
            FinalSelection.objects.filter(recruitment_entry=self.level1_position).exists()
        )
        for item, application in zip(report.items.order_by("rank_order", "id"), applications):
            application.refresh_from_db()
            application.case.refresh_from_db()
            self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
            self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
            self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
            decision_audit = AuditLog.objects.get(
                application=application,
                action=AuditLog.Action.DECISION_RECORDED,
                metadata__no_appointment_recorded=True,
            )
            self.assertEqual(decision_audit.metadata["car_report_id"], report.id)
            self.assertEqual(decision_audit.metadata["car_item_id"], item.id)
            self.assertEqual(decision_audit.metadata["decision_notes"], reason)
            self.assertEqual(
                decision_audit.metadata["decision_outcome"],
                FinalDecision.Outcome.NOT_SELECTED,
            )
            self.assertTrue(
                NotificationLog.objects.filter(
                    application=application,
                    notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
                ).exists()
            )

        system_audit = AuditLog.objects.get(
            application__isnull=True,
            action=AuditLog.Action.NO_APPOINTMENT_RECORDED,
        )
        self.assertEqual(system_audit.metadata["report_id"], report.id)
        self.assertEqual(system_audit.metadata["report_version"], report.version_number)
        self.assertEqual(system_audit.metadata["reason"], reason)
        self.assertEqual(
            system_audit.metadata["item_ids"],
            list(report.items.order_by("rank_order", "id").values_list("id", flat=True)),
        )

        # RD-7: no FinalSelection exists, so this is an ordinary rejected case and the
        # correction door remains available.
        reopened = reopen_recruitment_case(
            applications[0],
            self.hrm_chief,
            "Correction needed after the failure-of-search record.",
        )
        reopened.application.refresh_from_db()
        reopened.refresh_from_db()
        self.assertEqual(reopened.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(
            reopened.application.status,
            RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
        )

    def test_no_appointment_refuses_an_unready_car_or_an_existing_selection(self):
        applications, report = self.prepare_ranked_level1_car(2)
        unready = applications[1]
        unready.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        unready.save(update_fields=["current_handler_role", "updated_at"])
        unready.case.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        unready.case.save(update_fields=["current_handler_role", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "All CAR applicants must be ready for Appointing Authority final selection.",
        ):
            record_no_appointment(
                applications[0],
                self.appointing,
                "Attempted while a CAR item was not ready.",
            )

        unready.current_handler_role = RecruitmentUser.Role.APPOINTING_AUTHORITY
        unready.status = RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW
        unready.save(update_fields=["current_handler_role", "status", "updated_at"])
        unready.case.current_handler_role = RecruitmentUser.Role.APPOINTING_AUTHORITY
        unready.case.current_stage = RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        unready.case.save(
            update_fields=["current_handler_role", "current_stage", "updated_at"]
        )
        selected_item = report.items.get(recruitment_case=applications[0].case)
        with self.captureOnCommitCallbacks(execute=True):
            record_final_selection(
                applications[0],
                self.appointing,
                {
                    "selected_item": selected_item,
                    "decision_notes": "A selection now stands.",
                },
            )

        self.assertFalse(user_can_record_no_appointment(self.appointing, applications[0]))
        with self.assertRaisesMessage(
            ValueError,
            "Final selection has already been recorded for this vacancy.",
        ):
            record_no_appointment(
                applications[0],
                self.appointing,
                "Attempted after selection.",
            )

    def test_final_selection_outside_top_five_requires_deep_selection_support(self):
        applications, report = self.prepare_ranked_level1_car(6)
        selected_item = report.items.get(rank_order=6)

        with self.assertRaisesMessage(
            ValueError,
            "Selecting outside the top five requires deep selection documentation.",
        ):
            record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "decision_notes": "Attempted selection below the top five.",
                },
            )

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": selected_item,
                    "is_deep_selection": True,
                    "deep_selection_justification": (
                        "Appointing Authority documented superior qualifications "
                        "for the applicant ranked outside the top five."
                    ),
                    "decision_notes": "Selected through documented deep selection.",
                },
            )

        selected_application = applications[5]
        selected_application.refresh_from_db()
        selected_application.case.refresh_from_db()
        applications[0].refresh_from_db()
        applications[0].case.refresh_from_db()

        self.assertTrue(selection.is_deep_selection)
        self.assertIn("superior qualifications", selection.deep_selection_justification)
        self.assertEqual(selection.selected_application_id, selected_application.id)
        self.assertEqual(selected_application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(selected_application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(applications[0].status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(applications[0].case.current_stage, RecruitmentCase.Stage.CLOSED)

    def test_final_selection_invalid_post_rerenders_bound_form(self):
        applications, report = self.prepare_ranked_level1_car(6)
        selected_item = report.items.get(rank_order=6)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        response = client.post(
            reverse("final-selection-record", kwargs={"pk": applications[0].pk}),
            {
                "selected_item": str(selected_item.pk),
                "decision_notes": "Attempted to record a rank-six candidate without support.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Selecting outside the top five requires deep selection documentation.",
        )
        self.assertContains(response, "Pick the appointee")
        self.assertRegex(
            response.content.decode(),
            rf'name="selected_item"\s+value="{selected_item.pk}"\s+checked',
        )
        self.assertFalse(
            FinalSelection.objects.filter(recruitment_entry=self.level1_position).exists()
        )

    def test_appointing_authority_can_return_car_for_hrmpsb_reassessment(self):
        applications, report = self.prepare_ranked_level1_car(2)

        self.assertIn(
            (
                "return_car_for_reassessment",
                "Return CAR for correction (ETE / recommendation)",
            ),
            get_available_actions(applications[0], self.appointing),
        )
        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(
            reverse("application-detail", kwargs={"pk": applications[0].pk})
        )
        self.assertContains(detail, "Return CAR for correction (ETE / recommendation)")
        self.assertContains(
            detail,
            "A return reopens the ETE ratings and the recommendation. "
            "Finalized exam and interview scores are not re-opened.",
        )
        returned_report = process_workflow_action(
            applications[0],
            self.appointing,
            "return_car_for_reassessment",
            "CAR recommendation does not conform to final assessment.",
        )

        report.refresh_from_db()
        self.assertEqual(returned_report.id, report.id)
        self.assertTrue(report.is_returned)
        self.assertEqual(report.returned_by, self.appointing)
        self.assertIn("does not conform", report.return_reason)
        self.assertIsNone(get_latest_finalized_comparative_assessment_report(applications[0]))

        for application in applications:
            application.refresh_from_db()
            application.case.refresh_from_db()
            self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
            self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
            self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
            self.assertTrue(
                AuditLog.objects.filter(
                    application=application,
                    action=AuditLog.Action.CAR_RETURNED,
                ).exists()
            )
            self.assertTrue(
                application.routing_history.filter(
                    route_type=RoutingHistory.RouteType.REOPEN,
                    to_handler_role=RecruitmentUser.Role.HRMPSB_MEMBER,
                ).exists()
            )

        new_draft = generate_comparative_assessment_report(
            application=applications[0],
            actor=self.secretariat,
            cleaned_data={"summary_notes": "Reassessment CAR draft after AA return."},
            finalize=False,
        )
        self.assertGreater(new_draft.version_number, report.version_number)
        for index, application in enumerate(applications, start=1):
            self.finalize_deliberation_for_current_stage(
                application,
                self.hrmpsb,
                ranking_position=index,
                decision_support_summary=f"Reassessed support summary for rank {index}.",
            )
        new_report = generate_comparative_assessment_report(
            application=applications[0],
            actor=self.secretariat,
            cleaned_data={
                "summary_notes": "Final CAR after HRMPSB reassessment.",
                "members_present": 4,
                "deliberated_on": timezone.localdate(),
                "recommended_case_id": applications[0].case.id,
            },
            finalize=True,
        )
        self.assertTrue(new_report.is_finalized)
        self.assertFalse(new_report.is_returned)

    def test_a_historical_car_without_a_deliberation_date_can_still_be_returned(self):
        applications, report = self.prepare_ranked_level1_car(1)
        ComparativeAssessmentReport.objects.filter(pk=report.pk).update(
            deliberated_on=None
        )
        report.refresh_from_db()

        returned = return_car_for_reassessment(
            applications[0],
            self.appointing,
            "Correct the recommendation on this historical CAR.",
        )

        returned.refresh_from_db()
        self.assertTrue(returned.is_returned)
        self.assertIsNone(returned.deliberated_on)

    def test_withdrawn_car_item_is_greyed_unselectable_and_untouched_by_selection(self):
        applications, report = self.prepare_ranked_level1_car(3)
        winner, withdrawn, sibling = applications
        original_ranks = {
            item.recruitment_case_id: item.rank_order for item in report.items.all()
        }
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                withdrawn,
                self.appointing,
                "Applicant withdrew after the CAR reached the Appointing Authority.",
            )
        withdrawn.refresh_from_db()
        notification_count = NotificationLog.objects.filter(application=withdrawn).count()
        withdrawn_item = report.items.get(recruitment_case=withdrawn.case)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Withdrawn")
        self.assertRegex(
            detail.content.decode(),
            rf'(?s)<div\s+class="rg-del-car-item rg-fs-candidate opacity-50"[^>]*>.*?'
            rf'{re.escape(withdrawn.applicant_display_name)}',
        )
        self.assertNotRegex(
            detail.content.decode(),
            rf'name="selected_item"\s+value="{withdrawn_item.pk}"',
        )

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(recruitment_case=winner.case),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        withdrawn.refresh_from_db()
        withdrawn.case.refresh_from_db()
        self.assertEqual(selection.selected_application_id, winner.id)
        self.assertEqual(withdrawn.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(
            withdrawn.case.case_status, RecruitmentCase.CaseStatus.WITHDRAWN
        )
        self.assertEqual(
            NotificationLog.objects.filter(application=withdrawn).count(),
            notification_count,
        )
        self.assertEqual(
            {
                item.recruitment_case_id: item.rank_order
                for item in report.items.all()
            },
            original_ranks,
        )

        with self.captureOnCommitCallbacks(execute=True):
            _rescinded, restored = rescind_final_selection(
                selection,
                self.appointing,
                "The recorded selection must be reconsidered.",
            )
        withdrawn.refresh_from_db()
        self.assertEqual({item.pk for item in restored}, {winner.pk, sibling.pk})
        self.assertEqual(withdrawn.status, RecruitmentApplication.Status.WITHDRAWN)
        for application in (winner, sibling):
            notice = NotificationLog.objects.get(
                application=application,
                notification_type=(
                    NotificationLog.NotificationType.SELECTION_UNDER_REVIEW
                ),
            )
            self.assertEqual(
                notice.subject,
                f"Update on your application for {self.level1_position.title}",
            )
            self.assertIn("re-deliberating", notice.body)
        self.assertFalse(
            NotificationLog.objects.filter(
                application=withdrawn,
                notification_type=(
                    NotificationLog.NotificationType.SELECTION_UNDER_REVIEW
                ),
            ).exists()
        )

    def test_selection_proceeds_with_a_withdrawn_ranked_item(self):
        applications, report = self.prepare_ranked_level1_car(3)
        recommended, withdrawn, selected = applications
        original_ranks = list(report.items.values_list("recruitment_case_id", "rank_order"))
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                withdrawn,
                self.appointing,
                "Applicant withdrew before the final selection.",
            )

        # Rank #3 remains a deviation from the still-active board recommendation and
        # therefore keeps the existing WF-03 justification requirement.
        without_justification = FinalSelectionForm(
            {
                "selected_item": report.items.get(recruitment_case=selected.case).pk,
                "decision_notes": "Selecting rank three.",
            },
            report=report,
        )
        self.assertFalse(without_justification.is_valid())
        self.assertIn("deep_selection_justification", without_justification.errors)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=recommended,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(recruitment_case=selected.case),
                    "decision_notes": "Selected rank three after reviewing the record.",
                    "deep_selection_justification": (
                        "The Authority documented why rank three better meets the vacancy."
                    ),
                },
            )
        withdrawn.refresh_from_db()
        self.assertEqual(selection.selected_application_id, selected.id)
        self.assertEqual(withdrawn.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(
            list(report.items.values_list("recruitment_case_id", "rank_order")),
            original_ranks,
        )

    def test_car_return_proceeds_with_a_withdrawn_ranked_item(self):
        applications, report = self.prepare_ranked_level1_car(3)
        first, withdrawn, third = applications
        rank_before_return = list(
            report.items.values_list("recruitment_case_id", "rank_order")
        )
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                withdrawn,
                self.appointing,
                "Applicant withdrew before CAR reassessment.",
            )
            returned = return_car_for_reassessment(
                first,
                self.appointing,
                "The remaining candidates require HRMPSB reassessment.",
            )
        withdrawn.refresh_from_db()
        first.refresh_from_db()
        third.refresh_from_db()
        self.assertTrue(returned.is_returned)
        self.assertEqual(withdrawn.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(first.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(third.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(
            list(report.items.values_list("recruitment_case_id", "rank_order")),
            rank_before_return,
        )

    def test_withdrawn_recommendation_needs_no_deviation_justification_and_is_audited(self):
        applications, report = self.prepare_ranked_level1_car(3)
        recommended, selected, third = applications
        self.assertEqual(report.recommended_case_id, recommended.case.id)
        with self.captureOnCommitCallbacks(execute=True):
            record_application_withdrawal(
                recommended,
                self.appointing,
                "The board's recommended candidate withdrew.",
            )

        selected_item = report.items.get(recruitment_case=selected.case)
        form = FinalSelectionForm(
            {
                "selected_item": selected_item.pk,
                "decision_notes": "Selected another ranked candidate after the withdrawal.",
            },
            report=report,
        )
        self.assertTrue(form.is_valid(), form.errors)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=selected,
                actor=self.appointing,
                cleaned_data=form.cleaned_data,
            )
        recommended.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(selection.selected_application_id, selected.id)
        self.assertEqual(recommended.status, RecruitmentApplication.Status.WITHDRAWN)
        self.assertEqual(third.status, RecruitmentApplication.Status.REJECTED)
        decision_log = AuditLog.objects.filter(
            application=selected,
            action=AuditLog.Action.DECISION_RECORDED,
        ).latest("created_at")
        self.assertIs(decision_log.metadata["recommended_candidate_withdrawn"], True)

    def test_no_contending_car_items_raises_and_the_form_offers_no_radio(self):
        applications, report = self.prepare_ranked_level1_car(3)
        for index, application in enumerate(applications, start=1):
            with self.captureOnCommitCallbacks(execute=True):
                record_application_withdrawal(
                    application,
                    self.appointing,
                    f"Applicant {index} withdrew before selection.",
                )

        with self.assertRaisesMessage(
            ValueError,
            "The finalized CAR has no ranked applicants in contention.",
        ):
            _final_selection_items_are_ready(report)
        form = FinalSelectionForm(report=report)
        self.assertEqual(form.fields["selected_item"].queryset.count(), 0)
        self.assertNotIn('type="radio"', str(form["selected_item"]))

    def test_vacancy_cancelled_car_item_is_also_out_of_contention(self):
        applications, report = self.prepare_ranked_level1_car(2)
        cancelled = applications[1]
        RecruitmentApplication.objects.filter(pk=cancelled.pk).update(
            status=RecruitmentApplication.Status.VACANCY_CANCELLED,
            current_handler_role="",
        )
        RecruitmentCase.objects.filter(pk=cancelled.case.pk).update(
            current_stage=RecruitmentCase.Stage.CLOSED,
            current_handler_role="",
            case_status=RecruitmentCase.CaseStatus.VACANCY_CANCELLED,
            is_stage_locked=True,
        )
        cancelled.refresh_from_db()
        contenders = _final_selection_items_are_ready(report)
        self.assertEqual(
            {item.recruitment_case_id for item in contenders},
            {applications[0].case.id},
        )
        form = FinalSelectionForm(report=report)
        self.assertNotIn(
            cancelled.case.id,
            set(
                form.fields["selected_item"].queryset.values_list(
                    "recruitment_case_id", flat=True
                )
            ),
        )

    def test_return_car_invalid_post_rerenders_bound_form(self):
        applications, report = self.prepare_ranked_level1_car(2)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        response = client.post(
            reverse("workflow-action", kwargs={"pk": applications[0].pk}),
            {
                "action": "return_car_for_reassessment",
                "remarks": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Return CAR for correction (ETE / recommendation)")
        self.assertContains(response, "This field is required.")
        self.assertFalse(FinalSelection.objects.filter(recruitment_entry=self.level1_position).exists())
        report.refresh_from_db()
        self.assertFalse(report.is_returned)

    def test_level2_final_selection_routes_selected_applicant_to_hrm_chief_completion(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        self.record_final_decision_for_current_stage(
            application,
            self.appointing,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected Level 2 applicant from the finalized CAR.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

    def test_a_selection_cannot_be_recorded_before_the_pick_is_finalized(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_hrm_chief_review(application)
        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Finalize the COS vacancy deliberation pick before recording the selection.",
        ):
            record_final_decision(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={
                    "decision_outcome": FinalDecision.Outcome.SELECTED,
                    "decision_notes": "Skipping the deliberation.",
                },
            )

    def test_appointing_authority_cannot_record_cos_selection(self):
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)
        application.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError,
            "Only the HRM Chief may record the final decision at the current step.",
        ):
            record_final_decision(
                application=application,
                actor=self.appointing,
                cleaned_data={
                    "decision_outcome": FinalDecision.Outcome.SELECTED,
                    "decision_notes": "COS should not require Appointing Authority signing.",
                },
            )

        # The only decision on record is the one the HRM Chief's finalized pick produced.
        decisions = FinalDecision.objects.filter(application=application)
        self.assertEqual(decisions.count(), 1)
        self.assertEqual(decisions.first().decided_by, self.hrm_chief)

    def test_application_detail_exposes_decision_packet_sections(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Final Selection</span>')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-deliberation"')
        self.assertContains(response, 'rg-cws-layout rg-cws-layout--full')
        self.assertNotContains(response, "Workflow Snapshot")
        self.assertContains(response, "Pick the appointee")
        self.assertNotContains(response, "Review the submission packet")

        packet = build_submission_packet(application)
        self.assertTrue(packet["summary"]["has_comparative_assessment_report"])

    def test_cos_decision_surface_displays_the_authoritative_pick(self):
        # SL-01 AC#2: the case's Decision surface shows the vacancy pick and its notes —
        # it never asks the HRM Chief to choose the person a second time.
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "COS vacancy deliberation pick")
        self.assertContains(response, "recorded once on the")
        self.assertContains(response, "This applicant")
        self.assertNotContains(response, "Record your decision")
        self.assertNotContains(response, "Pick the appointee")

    def test_submission_packet_marks_missing_evaluation_records(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)

        packet = build_submission_packet(application)

        self.assertFalse(packet["summary"]["ready_for_final_decision"])
        self.assertIn("Finalized screening record", packet["summary"]["missing_components"])
        self.assertIn("Finalized examination record", packet["summary"]["missing_components"])
        self.assertIn("Finalized interview session", packet["summary"]["missing_components"])
        self.assertIn("Finalized COS deliberation pick", packet["summary"]["missing_components"])

    def test_the_per_case_cos_decision_endpoint_no_longer_exists(self):
        # SL-01: the second door is gone — the pick on the console is the only one.
        with self.assertRaises(NoReverseMatch):
            reverse("final-decision-record", kwargs={"pk": 1})

    def test_the_legacy_per_case_deliberation_record_cannot_be_written(self):
        # SL-15: the route, view, form and service were all still live behind a section
        # the sequence can no longer produce — one permission change away from a second,
        # competing source of the board's recommendation.
        application = self.make_application(self.cos_position)
        self.move_application_to_appointing_review(application)
        application.refresh_from_db()

        self.assertFalse(user_can_manage_deliberation(self.hrm_chief, application))
        with self.assertRaisesMessage(
            ValueError, "Per-case deliberation records are no longer recorded."
        ):
            save_deliberation_record(
                application=application,
                actor=self.hrm_chief,
                cleaned_data={},
                finalize=True,
            )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("deliberation-record", kwargs={"pk": application.pk}),
            {"operation": "finalize"},
        )
        self.assertEqual(response.status_code, 403)

    def test_rescinding_restores_only_the_cars_own_candidates(self):
        # Live E2E regression: the rescind's restore query swept EVERY rejected case on
        # the posting, so a candidate closed for an unrelated reason — an absent examinee
        # decided terminal after the letter of intent, a not-qualified screening — was
        # resurrected straight into Appointing Authority Review, selectable in a
        # competition they were never in. Restore is scoped to the CAR's ranked
        # candidates: the population the selection actually closed.
        applications, report = self.prepare_ranked_level1_car(2)
        outsider_user = User.objects.create_user(
            username="rescind-outsider",
            password="testpass123",
            email="rescind.outsider@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        outsider = RecruitmentApplication.objects.create(
            applicant=outsider_user,
            position=self.level1_position,
            applicant_first_name="Absent",
            applicant_last_name="Outsider",
            applicant_email="rescind.outsider@example.com",
            status=RecruitmentApplication.Status.REJECTED,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        RecruitmentCase.objects.create(
            application=outsider,
            branch=outsider.branch,
            current_stage=RecruitmentCase.Stage.CLOSED,
            current_handler_role="",
            case_status=RecruitmentCase.CaseStatus.REJECTED,
            is_stage_locked=True,
            locked_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        self.assertFalse(
            report.items.filter(recruitment_case=outsider.case).exists(),
            "fixture: the outsider must not be a CAR candidate",
        )

        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            _rescinded, restored = rescind_final_selection(
                selection, self.appointing, "Appointee declined."
            )

        # The CAR's own candidates come back...
        self.assertEqual(
            {a.pk for a in restored}, {applications[0].pk, applications[1].pk}
        )
        # ...and the unrelated rejection stays exactly where the office left it.
        outsider.refresh_from_db()
        outsider.case.refresh_from_db()
        self.assertEqual(outsider.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(
            outsider.case.case_status, RecruitmentCase.CaseStatus.REJECTED
        )
        self.assertEqual(outsider.case.current_stage, RecruitmentCase.Stage.CLOSED)

    def test_plantilla_rescind_cutoff_blocks_appointment_then_never_allows_and_resets_tracking(self):
        applications, report = self.prepare_ranked_level1_car(1)
        winner = applications[0]
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        completion = CompletionRecord.objects.create(
            application=winner,
            recruitment_case=winner.case,
            tracked_by=self.secretariat,
            completion_reference="APPOINTMENT-2026-001",
            completion_date=timezone.localdate(),
            deadline=timezone.localdate() + timedelta(days=5),
        )
        CompletionRequirement.objects.create(
            completion_record=completion,
            item_label="Signed appointment paper",
            status=CompletionRequirement.RequirementStatus.COMPLETED,
        )
        refusal = (
            "The appointment has been issued. A revocation at this point is handled as a "
            "separation, not a rescission."
        )

        self.assertFalse(user_can_rescind_final_selection(self.appointing, selection))
        with self.assertRaisesMessage(ValueError, refusal):
            rescind_final_selection(
                selection, self.appointing, "The appointment will not proceed."
            )
        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, refusal)
        self.assertNotContains(
            detail, reverse("final-selection-rescind", kwargs={"pk": winner.pk})
        )

        policy = get_office_policy()
        policy.rescind_cutoff = OfficePolicy.RescindCutoff.NEVER
        policy.save(update_fields=["rescind_cutoff", "updated_at"])
        self.assertTrue(user_can_rescind_final_selection(self.appointing, selection))
        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(
                selection, self.appointing, "The appointment will not proceed."
            )
        completion.refresh_from_db()
        self.assertTrue(completion.is_voided)
        self.assertFalse(completion.ready_for_closure)
        self.assertIn("voided by rescission", completion.closure_blockers[0].lower())

        with self.captureOnCommitCallbacks(execute=True):
            replacement = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Re-selected after correcting the rescission.",
                },
            )
        self.assertEqual(replacement.supersedes_id, selection.id)
        completion.refresh_from_db()
        self.assertFalse(completion.is_voided)
        self.assertEqual(completion.completion_reference, "")
        self.assertIsNone(completion.completion_date)
        self.assertFalse(completion.requirements.exists())

    def test_rescinding_a_selection_preserves_it_and_reopens_the_vacancy(self):
        # SL-14: the correction is additive. The original selection keeps every value it
        # was recorded with, the winner comes back out of completion, and the sibling it
        # closed is in contention again — otherwise a mis-click is the official truth.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        sibling = applications[1]
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        original_decided_at = selection.decided_at
        original_snapshot = selection.car_snapshot
        appointing_peer = User.objects.create_user(
            username="appointing-peer",
            password="testpass123",
            email="appointing.peer@example.com",
            role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
        )

        with self.captureOnCommitCallbacks(execute=True):
            rescinded, restored = rescind_final_selection(
                selection,
                self.appointing,
                "The selected applicant declined the appointment before assumption.",
                reference="Letter dated 2026-07-20",
            )

        rescinded.refresh_from_db()
        winner.refresh_from_db()
        winner.case.refresh_from_db()
        sibling.refresh_from_db()
        sibling.case.refresh_from_db()

        self.assertTrue(rescinded.is_rescinded)
        self.assertEqual(rescinded.rescinded_by, self.appointing)
        self.assertEqual(
            rescinded.rescinded_by_role, RecruitmentUser.Role.APPOINTING_AUTHORITY
        )
        self.assertIn("declined the appointment", rescinded.rescind_reason)
        self.assertEqual(rescinded.rescind_reference, "Letter dated 2026-07-20")
        # Preserved, not rewritten.
        self.assertEqual(rescinded.selected_application_id, winner.id)
        self.assertEqual(rescinded.decided_by, self.appointing)
        self.assertEqual(rescinded.decided_at, original_decided_at)
        self.assertEqual(rescinded.car_snapshot, original_snapshot)

        self.assertIn(winner.id, [a.id for a in restored])
        self.assertIn(sibling.id, [a.id for a in restored])
        for application in (winner, sibling):
            self.assertEqual(
                application.status,
                RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
            )
            self.assertEqual(
                application.case.current_stage,
                RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
            )
            self.assertEqual(
                application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE
            )
            self.assertFalse(application.case.is_stage_locked)
            self.assertIsNone(application.closed_at)

        # The vacancy reads as undecided again, and the trail keeps the rescinded row.
        self.assertIsNone(get_final_selection_for_entry(self.level1_position))
        history = list(get_final_selection_history(self.level1_position))
        self.assertEqual([record.id for record in history], [selection.id])
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
                application=winner,
            ).exists()
        )
        returned_bells = Notification.objects.filter(
            application=winner,
            kind=Notification.Kind.CASE_RETURNED,
        )
        self.assertEqual(returned_bells.count(), 1)
        returned_bell = returned_bells.get()
        self.assertEqual(returned_bell.recipient, appointing_peer)
        self.assertEqual(
            returned_bell.title,
            f"{winner.reference_label} selection rescinded — vacancy back for decision",
        )
        self.assertEqual(
            returned_bell.body,
            "The selected applicant declined the appointment before assumption.",
        )
        self.assertNotIn(winner.applicant_display_name, returned_bell.title)
        self.assertNotIn(winner.applicant_display_name, returned_bell.body)
        self.assertFalse(
            returned_bells.filter(recipient=self.appointing).exists()
        )

    def test_a_replacement_selection_can_be_recorded_and_chains_to_the_rescinded_one(self):
        # SL-14: reopening the vacancy is only useful if it can then be decided again, and
        # the two rows have to read as one correction rather than two rival decisions.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        replacement = applications[1]
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(
                selection, self.appointing, "Recorded against the wrong candidate."
            )

        replacement.refresh_from_db()
        self.assertTrue(user_can_record_final_selection(self.appointing, replacement))

        with self.captureOnCommitCallbacks(execute=True):
            second = record_final_selection(
                application=replacement,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=2),
                    "decision_notes": "Recorded the correct appointee.",
                    "deep_selection_justification": (
                        "The originally selected applicant declined; the next ranked "
                        "applicant is appointed."
                    ),
                },
            )

        replacement.refresh_from_db()
        replacement.case.refresh_from_db()
        winner.refresh_from_db()
        winner.case.refresh_from_db()

        self.assertEqual(second.supersedes_id, selection.id)
        self.assertEqual(get_final_selection_for_entry(self.level1_position), second)
        self.assertEqual(replacement.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(
            replacement.case.current_stage, RecruitmentCase.Stage.COMPLETION
        )
        self.assertEqual(winner.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(
            FinalSelection.objects.filter(recruitment_entry=self.level1_position).count(),
            2,
        )

    def test_rescinding_requires_the_appointing_authority_and_a_reason(self):
        # SL-14: voiding the office's own appointment decision is not a routine edit — it
        # is restricted to the role that recorded it and never accepted without a reason.
        applications, report = self.prepare_ranked_level1_car(3)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        for actor in (self.secretariat, self.hrm_chief, self.hrmpsb):
            self.assertFalse(user_can_rescind_final_selection(actor, selection))
        self.assertTrue(user_can_rescind_final_selection(self.appointing, selection))

        with self.assertRaisesMessage(
            ValueError, "Only the Appointing Authority may rescind"
        ):
            rescind_final_selection(selection, self.hrm_chief, "Not my call to make.")
        with self.assertRaisesMessage(
            ValueError, "Record why this selection is being rescinded."
        ):
            rescind_final_selection(selection, self.appointing, "   ")

        selection.refresh_from_db()
        self.assertFalse(selection.is_rescinded)
        self.assertEqual(get_final_selection_for_entry(self.level1_position), selection)

    def test_the_appointing_authority_can_rescind_from_the_case_surface(self):
        # SL-14: the affordance has to exist where the decision was made, and it may not
        # be reachable by anyone else.
        applications, report = self.prepare_ranked_level1_car(3)
        winner = applications[0]
        with self.captureOnCommitCallbacks(execute=True):
            record_final_selection(
                application=winner,
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        client = Client()
        self.force_login_with_mfa(client, self.appointing)
        detail = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Rescind this selection")
        self.assertNotContains(detail, "{#")

        other_client = Client()
        self.force_login_with_mfa(other_client, self.hrm_chief)
        forbidden = other_client.post(
            reverse("final-selection-rescind", kwargs={"pk": winner.pk}),
            {"reason": "Trying to undo someone else's decision."},
        )
        self.assertEqual(forbidden.status_code, 403)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("final-selection-rescind", kwargs={"pk": winner.pk}),
                {
                    "reason": "The appointment is not going ahead.",
                    "reference": "Memo 2026-07-22",
                },
            )
        self.assertEqual(response.status_code, 302)

        self.assertIsNone(get_final_selection_for_entry(self.level1_position))
        after = client.get(reverse("application-detail", kwargs={"pk": winner.pk}))
        self.assertContains(after, "Rescinded selection")
        self.assertContains(after, "The appointment is not going ahead.")
        self.assertContains(after, "Memo 2026-07-22")
        self.assertNotContains(after, "Rescind this selection")

    def test_a_rescinded_selection_no_longer_blocks_cancelling_the_vacancy(self):
        # SL-14 + SL-11: cancellation tells the canceller to rescind first, so a rescinded
        # row must stop refusing them once they have.
        applications, report = self.prepare_ranked_level1_car(3)
        with self.captureOnCommitCallbacks(execute=True):
            selection = record_final_selection(
                application=applications[0],
                actor=self.appointing,
                cleaned_data={
                    "selected_item": report.items.get(rank_order=1),
                    "decision_notes": "Selected the board's recommended applicant.",
                },
            )

        with self.assertRaisesMessage(
            ValidationError, "A selection has already been recorded for this vacancy."
        ):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Item withdrawn by the region."
            )

        with self.captureOnCommitCallbacks(execute=True):
            rescind_final_selection(
                selection, self.appointing, "The appointment is not going ahead."
            )
        with self.captureOnCommitCallbacks(execute=True):
            cancel_recruitment_entry(
                self.level1_position, self.secretariat, "Item withdrawn by the region."
            )

        self.level1_position.refresh_from_db()
        self.assertEqual(
            self.level1_position.status, PositionPosting.EntryStatus.CANCELLED
        )
