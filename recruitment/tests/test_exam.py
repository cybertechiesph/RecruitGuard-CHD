"""Exam records and the batch exam."""

from decimal import Decimal
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..forms import ExamRecordForm
from ..models import (
    AuditLog,
    ExamRecord,
    EvidenceVaultItem,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..services import (
    get_current_workflow_section,
    vacancy_batch_pool_block_message,
    vacancy_batch_pool_is_final,
    save_exam_record,
    submit_application,
    vacancy_exam_pool,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    vacancy_screening_batch_status,
)
from .base import BaseRecruitmentTestCase, BatchExamHelpers


class ExamRecordTests(BaseRecruitmentTestCase):
    def exam_payload(self, **overrides):
        payload = {
            "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            "exam_status": ExamRecord.ExamStatus.COMPLETED,
            "exam_result": "",
            "technical_score": "84.50",
            "technical_result": "",
            "general_score": "89.00",
            "general_result": "",
            "exam_date": timezone.localdate().isoformat(),
            "administered_by": ExamRecord.AdministeredBy.HRMS,
            "valid_from": timezone.localdate().isoformat(),
            "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
            "exam_notes": "Validated through the current review stage.",
        }
        payload.update(overrides)
        return payload

    def test_exam_form_displays_single_policy_exam_type_as_fixed_value(self):
        level1_application = self.make_application(self.level1_position)
        level1_form = ExamRecordForm(application=level1_application)

        self.assertTrue(level1_form.exam_type_is_fixed)
        self.assertEqual(
            level1_form.exam_type_fixed_label,
            ExamRecord.ExamType.TECHNICAL_PRACTICAL.label,
        )
        self.assertEqual(
            level1_form["exam_type"].value(),
            ExamRecord.ExamType.TECHNICAL_PRACTICAL,
        )
        self.assertEqual(level1_form.fields["exam_type"].widget.input_type, "hidden")
        self.assertTrue(level1_form.administered_by_is_fixed)
        self.assertEqual(
            level1_form.administered_by_fixed_label,
            ExamRecord.AdministeredBy.HRMS.label,
        )

        cos_application = self.make_application(self.cos_position)
        cos_form = ExamRecordForm(application=cos_application)
        self.assertTrue(cos_form.exam_type_is_fixed)
        self.assertEqual(
            cos_form.exam_type_fixed_label,
            ExamRecord.ExamType.END_USER_ASSESSMENT.label,
        )
        self.assertTrue(cos_form.administered_by_is_fixed)
        self.assertEqual(
            cos_form.administered_by_fixed_label,
            ExamRecord.AdministeredBy.END_USER.label,
        )

    def test_exam_form_rejects_scores_outside_allowed_range(self):
        application = self.make_application(self.level1_position)
        form = ExamRecordForm(
            data=self.exam_payload(
                technical_score="-1",
                general_score="125",
            ),
            application=application,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(ExamRecordForm.SCORE_RANGE_MESSAGE, form.errors["technical_score"])
        self.assertIn(ExamRecordForm.SCORE_RANGE_MESSAGE, form.errors["general_score"])

    def test_current_handler_can_create_update_and_finalize_exam_record(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "82.50",
                "exam_result": "",
                "technical_score": "80.00",
                "technical_result": "",
                "general_score": "85.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=180),
                "exam_notes": "Initial exam draft.",
            },
            finalize=False,
        )
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.recruitment_case, application.case)
        self.assertEqual(ExamRecord.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_RECORDED,
            ).exists()
        )

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "90.00",
                "exam_result": "",
                "technical_score": "88.00",
                "technical_result": "",
                "general_score": "92.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Updated before finalization.",
            },
            finalize=True,
        )
        exam_record.refresh_from_db()
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "90.40")
        self.assertEqual(str(exam_record.technical_score), "88.00")
        self.assertEqual(str(exam_record.general_score), "92.00")
        self.assertEqual(exam_record.exam_date, timezone.localdate())
        self.assertEqual(exam_record.administered_by, ExamRecord.AdministeredBy.HRMS)
        self.assertEqual(
            exam_record.component_summary,
            "Technical: 88.00 (Recorded for evaluation); General Ability: 92.00 (Recorded for evaluation)",
        )
        self.assertEqual(exam_record.finalized_by, self.secretariat)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EXAM_FINALIZED,
            ).exists()
        )

    def test_completed_exam_can_use_structured_components_without_overall_score(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": "82.00",
                "technical_result": "",
                "general_score": "88.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Structured components recorded without an overall score.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(str(exam_record.exam_score), "85.60")
        self.assertEqual(str(exam_record.effective_score), "85.60")

    def test_exam_record_can_attach_optional_evidence(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "91.00",
                "exam_result": "",
                "technical_score": "90.00",
                "technical_result": "",
                "general_score": "92.00",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Evidence file attached.",
            },
            evidence_file=SimpleUploadedFile(
                "exam-result.pdf",
                b"%PDF-1.4\nexam evidence",
                content_type="application/pdf",
            ),
            finalize=True,
        )

        exam_record.refresh_from_db()
        self.assertIsNotNone(exam_record.evidence_item)
        self.assertEqual(exam_record.evidence_item.artifact_scope, EvidenceVaultItem.OwnerScope.CASE)
        self.assertEqual(exam_record.evidence_item.artifact_type, "exam_supporting_evidence")
        self.assertEqual(exam_record.evidence_item.stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.EVIDENCE_UPLOADED,
                metadata__artifact_type="exam_supporting_evidence",
            ).exists()
        )

    def test_exam_evidence_rejects_active_content_upload(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        with self.assertRaisesMessage(
            ValueError,
            "This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "91.00",
                    "exam_result": "",
                    "technical_score": "90.00",
                    "technical_result": "",
                    "general_score": "92.00",
                    "general_result": "",
                    "exam_date": timezone.localdate(),
                    "administered_by": ExamRecord.AdministeredBy.HRMS,
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=365),
                    "exam_notes": "Active content should not be stored.",
                },
                evidence_file=SimpleUploadedFile(
                    "exam-result.html",
                    b"<html><script>alert(1)</script></html>",
                    content_type="text/html",
                ),
                finalize=True,
            )

        self.assertFalse(
            EvidenceVaultItem.objects.filter(
                application=application,
                artifact_type="exam_supporting_evidence",
            ).exists()
        )

    def test_finalized_exam_output_is_locked(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        exam_record = self.finalize_exam_for_current_stage(
            application,
            self.secretariat,
            valid_from=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=90),
        )

        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        with self.assertRaisesMessage(
            ValueError,
            "This case is not currently assigned to you for exam details.",
        ):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": "70.00",
                    "exam_result": "",
                    "valid_from": timezone.localdate(),
                    "valid_until": timezone.localdate() + timedelta(days=30),
                    "exam_notes": "Should not save.",
                },
                finalize=False,
            )

        exam_record.refresh_from_db()
        self.assertEqual(str(exam_record.exam_score), "88.50")
        self.assertEqual(exam_record.exam_result, ExamRecord.OverallResult.FOR_EVALUATION)

    def test_cos_exam_waiver_can_be_finalized_without_score_or_validity(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)

        exam_record = save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": None,
                "exam_result": "",
                "technical_score": None,
                "technical_result": "",
                "general_score": None,
                "general_result": "",
                "exam_date": None,
                "administered_by": "",
                "valid_from": None,
                "valid_until": None,
                "exam_notes": "Waived under COS office control.",
            },
            finalize=True,
        )

        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(exam_record.branch, PositionPosting.Branch.COS)
        self.assertIsNone(exam_record.exam_score)
        self.assertIsNone(exam_record.valid_from)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.WAIVED)

    def test_completed_exam_requires_score_or_result(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        with self.assertRaises(ValidationError):
            save_exam_record(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                    "exam_status": ExamRecord.ExamStatus.COMPLETED,
                    "exam_score": None,
                    "exam_result": "",
                    "technical_score": None,
                    "technical_result": "",
                    "general_score": None,
                    "general_result": "",
                    "exam_date": timezone.localdate(),
                    "administered_by": ExamRecord.AdministeredBy.HRMS,
                    "valid_from": None,
                    "valid_until": None,
                    "exam_notes": "Missing score and result.",
                },
                finalize=False,
            )

    def test_unauthorized_user_cannot_record_exam(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)

    def test_exam_view_invalid_payload_does_not_raise_server_error(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": "314",
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_score": "1231243",
                "exam_result": "214124",
                "valid_from": "0214-02-04",
                "valid_until": "124124-12-04",
                "exam_notes": "",
                "operation": "finalize",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExamRecord.objects.filter(application=application).exists())

    def test_waived_exam_clears_hidden_score_and_validity_values(self):
        # A waived exam carries no score and no validity window, whatever was typed before.
        application = self.make_application(self.cos_continuous_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.schedule_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        save_exam_record(
            application=application,
            actor=self.secretariat,
            cleaned_data={
                "exam_type": ExamRecord.ExamType.END_USER_ASSESSMENT,
                "exam_status": ExamRecord.ExamStatus.WAIVED,
                "exam_result": "",
                "technical_score": "80",
                "technical_result": "",
                "general_score": "80",
                "general_result": "",
                "exam_date": timezone.localdate(),
                "administered_by": ExamRecord.AdministeredBy.END_USER,
                "valid_from": timezone.localdate(),
                "valid_until": timezone.localdate() + timedelta(days=365),
                "exam_notes": "Waived by authorized office control.",
            },
            finalize=True,
        )
        exam_record = ExamRecord.objects.get(application=application)
        self.assertTrue(exam_record.is_finalized)
        self.assertEqual(exam_record.exam_status, ExamRecord.ExamStatus.WAIVED)
        self.assertIsNone(exam_record.exam_score)
        self.assertIsNone(exam_record.technical_score)
        self.assertIsNone(exam_record.general_score)
        self.assertIsNone(exam_record.valid_from)
        self.assertIsNone(exam_record.valid_until)

    def test_secretariat_cannot_record_level2_exam_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {**self.exam_payload(), "operation": "save"},
        )
        self.assertEqual(response.status_code, 403)


class BatchExamTests(BatchExamHelpers, BaseRecruitmentTestCase):
    # ── SL-09: the batch waits for a final applicant pool ─────────────────────
    def _two_qualified_pool_still_open(self, position):
        first = self._submit(self.make_application(position), self.applicant)
        self.finalize_screening_for_current_stage(first, self.secretariat)
        second = self._extra_submitted_application(position, "x")
        self.finalize_screening_for_current_stage(second, self.secretariat)
        first.refresh_from_db()
        second.refresh_from_db()
        return first, second

    def test_the_batch_exam_is_blocked_while_publication_is_still_open(self):
        # SL-09: the audit's probe — every submitted case was screened, so the batch could
        # be scheduled, but a timely later applicant could still submit afterwards and
        # would miss the sitting.
        self._two_qualified_pool_still_open(self.level1_position)
        self.assertFalse(self.level1_position.applicant_pool_is_finalized)

        with self.assertRaisesMessage(ValueError, "Publication is open until"):
            save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        with self.assertRaisesMessage(ValueError, "Publication is open until"):
            save_vacancy_exam_scores(
                self.level1_position, self.secretariat, {}, finalize=False
            )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        )
        self.assertEqual(response.status_code, 302)
        response = client.post(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            {"operation": "save_schedule"},
        )
        self.assertEqual(response.status_code, 302)

        # Screening each applicant continues during the window — only the batch waits.
        self.assertTrue(
            vacancy_screening_batch_status(self.level1_position).is_ready
        )

        # Once the pool is final the batch opens exactly as before.
        self.finalize_applicant_pool_for_test(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            pool = save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        self.assertEqual(len(pool), 2)

    def test_a_pooling_cos_vacancy_is_not_locked_by_the_pool_rule(self):
        # SL-09 AC#4 and the scope trap: pooling/continuous COS entries have no closing
        # date, so a branch-agnostic rule would lock their consoles permanently.
        self.assertFalse(self.cos_position.applicant_pool_is_finalized)
        self.assertTrue(vacancy_batch_pool_is_final(self.cos_position))
        self.assertEqual(vacancy_batch_pool_block_message(self.cos_position), "")

    def test_pool_lists_the_qualified_candidates(self):
        first, second = self._two_qualified(self.level1_position)
        pool = vacancy_exam_pool(self.level1_position)
        self.assertEqual({a.pk for a in pool}, {first.pk, second.pk})

    def test_batch_schedule_applies_to_every_candidate(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        for application in (first, second):
            schedule = application.exam_schedules.first()
            self.assertIsNotNone(schedule)
            self.assertIsNotNone(schedule.applicant_notified_at)

    def test_batch_finalize_records_scores_and_advances_the_pool(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "70", "technical_score": "60"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        first_exam = first.exam_records.get(review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(first_exam.is_finalized)
        # 90 * 0.60 + 80 * 0.40 = 86 with the seeded per-vacancy weights.
        self.assertEqual(first_exam.exam_score, Decimal("86.00"))
        # Both passers advance together to the HRMPSB stage.
        self.assertEqual(first.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(second.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        # The vacancy's assessment weights lock once scoring is committed.
        self.assertTrue(self.level1_position.assessment_weights_or_default.is_locked)

    def test_batch_finalize_requires_every_candidate_scored(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.assertRaises(ValueError):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {first.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )
        # All-or-nothing: the first candidate's exam was not finalized either.
        first.refresh_from_db()
        self.assertFalse(
            first.exam_records.filter(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW, is_finalized=True
            ).exists()
        )

    def test_cos_batch_exam_uses_two_weighted_components(self):
        # RD-2: COS scores the same way Plantilla does — General Ability and
        # Technical, combined by the vacancy's weights (default 60/40). The
        # batch requires both components before it can finalize.
        first, second = self._two_qualified(self.cos_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._schedule_payload())

        # One component is not enough to finalize.
        with self.assertRaisesMessage(ValueError, "General Ability and Technical"):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {first.case.id: {"general_score": "82"}},
                finalize=True,
            )

        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "80", "technical_score": "90"},
                    second.case.id: {"general_score": "70", "technical_score": "80"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        first_exam = first.exam_records.get(review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertTrue(first_exam.is_finalized)
        self.assertEqual(first_exam.exam_type, ExamRecord.ExamType.END_USER_ASSESSMENT)
        self.assertEqual(first_exam.general_score, Decimal("80.00"))
        self.assertEqual(first_exam.technical_score, Decimal("90.00"))
        # 80*0.60 + 90*0.40 = 84.00
        self.assertEqual(first_exam.exam_score, Decimal("84.00"))

    def test_consoles_serve_an_open_pool(self):
        # C1: an open pool examines and interviews in batches, so its consoles open.
        application = self.make_application(self.cos_continuous_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-exam", args=[self.cos_continuous_position.pk]))
        self.assertEqual(response.status_code, 200)

    def test_per_case_exam_post_refused_for_pooled_vacancy(self):
        # RD-2 "one door", server-enforced (PT-03 lesson): a pooled vacancy's
        # per-case exam endpoint refuses writes so a forced POST can't create a
        # second, conflicting score. level1_position is Plantilla (fixed-period).
        first, _second = self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("exam-review", kwargs={"pk": first.pk}),
            {
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "general_score": "88",
                "technical_score": "82",
                "administered_by": ExamRecord.AdministeredBy.HRMS,
                "operation": "finalize",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            response.headers["Location"],
        )
        # No exam record was written by the refused POST.
        self.assertFalse(first.exam_records.exists())

    def test_batch_exam_blocked_when_screening_batch_not_ready(self):
        first = self._submit(self.make_application(self.level1_position), self.applicant)
        self.finalize_screening_for_current_stage(first, self.secretariat)
        # A second submitted applicant whose screening is not finalized keeps the batch pending.
        self._extra_submitted_application(self.level1_position, "y")
        with self.assertRaises(ValueError):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {first.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )

    def test_batch_exam_view_renders_for_the_reviewing_role(self):
        self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Batch Exam")
        # Guard: multi-line {# #} Django comments render literally (browser
        # verification caught one on this very page).
        self.assertNotContains(response, "{#")

    def test_batch_exam_view_404s_for_a_non_reviewing_role(self):
        # The HRM Chief does not handle a Level-1 vacancy's screening/exam (FRS scoping).
        self._two_qualified(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 404)

    def test_batch_exam_view_finalizes_via_post(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
                {
                    "operation": "finalize",
                    f"general_score_{first.case.id}": "90",
                    f"technical_score_{first.case.id}": "80",
                    f"general_score_{second.case.id}": "70",
                    f"technical_score_{second.case.id}": "60",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.status, RecruitmentApplication.Status.HRMPSB_REVIEW)

    def test_batch_exam_preserves_and_renders_its_validity_window_on_finalize(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        console_url = reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        valid_from = timezone.localdate()
        valid_until = valid_from + timedelta(days=180)
        score_payload = {
            f"general_score_{first.case.id}": "90",
            f"technical_score_{first.case.id}": "80",
            f"general_score_{second.case.id}": "70",
            f"technical_score_{second.case.id}": "60",
        }

        saved = client.post(
            console_url,
            {
                "operation": "save_scores",
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
                **score_payload,
            },
        )
        self.assertEqual(saved.status_code, 302)
        rendered = client.get(console_url)
        self.assertEqual(rendered.status_code, 200)
        self.assertEqual(rendered.context["batch_valid_from"], valid_from)
        self.assertEqual(rendered.context["batch_valid_until"], valid_until)
        self.assertContains(rendered, f'value="{valid_from.isoformat()}"')
        self.assertContains(rendered, f'value="{valid_until.isoformat()}"')

        with self.captureOnCommitCallbacks(execute=True):
            finalized = client.post(
                console_url,
                {
                    "operation": "finalize",
                    "valid_from": "",
                    "valid_until": "",
                    **score_payload,
                },
            )
        self.assertEqual(finalized.status_code, 302)
        for application in (first, second):
            record = application.exam_records.get(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
            )
            self.assertEqual(record.valid_from, valid_from)
            self.assertEqual(record.valid_until, valid_until)

    def test_batch_exam_validity_window_can_be_explicitly_cleared(self):
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position, self.secretariat, self._schedule_payload()
            )
        valid_from = timezone.localdate()
        valid_until = valid_from + timedelta(days=180)
        scores = {
            first.case.id: {"general_score": "90", "technical_score": "80"},
            second.case.id: {"general_score": "70", "technical_score": "60"},
        }
        save_vacancy_exam_scores(
            self.level1_position,
            self.secretariat,
            scores,
            valid_from=valid_from,
            valid_until=valid_until,
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            {
                "operation": "save_scores",
                "clear_validity_window": "1",
                f"general_score_{first.case.id}": "90",
                f"technical_score_{first.case.id}": "80",
                f"general_score_{second.case.id}": "70",
                f"technical_score_{second.case.id}": "60",
            },
        )
        self.assertEqual(response.status_code, 302)
        for application in (first, second):
            record = application.exam_records.get(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
            )
            self.assertIsNone(record.valid_from)
            self.assertIsNone(record.valid_until)

    def test_the_batch_console_serves_the_chief_stage_second_pass(self):
        # Live COS E2E regression: the console's pool was pinned to the vacancy's INTAKE
        # stage (Secretariat for Level 1), so after the batch handed off to the HRM Chief
        # for the second-pass validation (SL-05), the chief-stage exam had NO door at all —
        # the console showed "no candidates" while the per-case exam form stays
        # one-door-refused for a pooled vacancy. The pool now follows the batch's stage.
        first, second = self._two_qualified(self.cos_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.secretariat, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.cos_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "70", "technical_score": "60"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        # Chief's second-pass screening for both.
        self.finalize_screening_for_current_stage(first, self.hrm_chief)
        self.finalize_screening_for_current_stage(second, self.hrm_chief)
        first.refresh_from_db()
        second.refresh_from_db()

        # The pool follows the batch to the chief stage...
        self.assertEqual(
            {a.pk for a in vacancy_exam_pool(self.cos_position)},
            {first.pk, second.pk},
        )
        # ...and the console serves it over HTTP instead of bouncing on an empty pool.
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("vacancy-batch-exam", args=[self.cos_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, first.reference_label)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.cos_position, self.hrm_chief, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.cos_position,
                self.hrm_chief,
                {
                    first.case.id: {"general_score": "91", "technical_score": "82"},
                    second.case.id: {"general_score": "72", "technical_score": "63"},
                },
                finalize=True,
            )
        first.refresh_from_db()
        self.assertEqual(get_current_workflow_section(first), "interview")

    def test_batch_exam_view_redirects_when_pool_empty_after_finalize(self):
        # Regression: finalizing the exam batch empties the pool (cases move to the
        # interview section), so a refresh / Back-button / re-run of the exam URL used to
        # raise a bare Http404 ("Page Not Found") — the very error seen after finalizing.
        # It must now redirect to the batch overview with a friendly note instead.
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {
                    first.case.id: {"general_score": "90", "technical_score": "80"},
                    second.case.id: {"general_score": "70", "technical_score": "60"},
                },
                finalize=True,
            )
        # The exam pool is now empty (the passers advanced to the interview section).
        self.assertEqual(list(vacancy_exam_pool(self.level1_position)), [])
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-exam", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-detail", args=[self.level1_position.pk]),
            response.headers["Location"],
        )

    def test_batch_exam_finalize_forwards_to_the_interview_console(self):
        # After finalizing the exam, the Secretariat (the Level-1 interview-support role)
        # is carried straight to the interview console for the same pool instead of being
        # dropped back on the batch overview to hunt for the next step.
        first, second = self._two_qualified(self.level1_position)
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(self.level1_position, self.secretariat, self._schedule_payload())
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
                {
                    "operation": "finalize",
                    f"general_score_{first.case.id}": "90",
                    f"technical_score_{first.case.id}": "80",
                    f"general_score_{second.case.id}": "70",
                    f"technical_score_{second.case.id}": "60",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse("vacancy-batch-interview", args=[self.level1_position.pk]),
            response.headers["Location"],
        )
