"""Completion tracking and closing a case."""

from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AuditLog,
    CompletionRecord,
    CompletionRequirement,
    PositionPosting,
    RecruitmentCase,
    RoutingHistory,
)
from ..services import get_completion_record
from .base import BaseRecruitmentTestCase


class CompletionTrackingTests(BaseRecruitmentTestCase):
    # ── SL-13: a historical deadline is evidence, not invalid data ────────────
    def test_a_past_deadline_can_be_left_alone_while_a_late_item_is_completed(self):
        # SL-13: the audit's probe — the only way to mark a late-but-valid requirement
        # complete was to move the deadline forward, erasing the evidence of lateness.
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting the applicant.",
                    },
                ]
            ),
        )
        record = get_completion_record(application)
        # Age the deadline the way the calendar would.
        past = timezone.localdate() - timedelta(days=3)
        CompletionRecord.objects.filter(pk=record.pk).update(deadline=past)
        record.refresh_from_db()
        self.assertTrue(record.is_overdue)
        self.assertEqual(record.days_overdue, 3)

        # Resubmitting the SAME historical deadline is accepted, and the late item can be
        # marked complete without rewriting history.
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Received late but valid.",
                    },
                ],
                deadline=past.isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        requirement = record.requirements.get()

        self.assertEqual(record.deadline, past, "the original deadline must survive")
        self.assertEqual(
            requirement.status, CompletionRequirement.RequirementStatus.COMPLETED
        )
        self.assertEqual(requirement.completed_on, timezone.localdate())
        self.assertTrue(requirement.was_completed_late)
        self.assertFalse(record.deadline_changes.exists())

    def test_changing_the_deadline_is_an_audited_extension(self):
        # SL-13 AC#2: old value, new value, actor, reason and time, on an immutable trail.
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ]
            ),
        )
        record = get_completion_record(application)
        original = record.deadline
        extended = original + timedelta(days=7)

        # Without a reason the change is refused.
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ],
                deadline=extended.isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Record why the completion deadline is being changed.")
        record.refresh_from_db()
        self.assertEqual(record.deadline, original)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting.",
                    },
                ],
                deadline=extended.isoformat(),
                deadline_change_reason="Applicant's PSA copy is delayed at the registry.",
            ),
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        change = record.deadline_changes.get()

        self.assertEqual(record.deadline, extended)
        self.assertEqual(change.previous_deadline, original)
        self.assertEqual(change.new_deadline, extended)
        self.assertEqual(change.changed_by, self.secretariat)
        self.assertTrue(change.is_extension)
        self.assertIn("PSA copy", change.reason)
        entry = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.COMPLETION_DEADLINE_CHANGED,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["previous_deadline"], original.isoformat())
        self.assertEqual(entry.metadata["new_deadline"], extended.isoformat())
        self.assertTrue(entry.metadata["is_extension"])

    def completion_payload(self, items, **overrides):
        payload = {
            "completion_reference": "COMP-001",
            "completion_date": timezone.localdate().isoformat(),
            "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "remarks": "Completion tracking updated.",
        }
        payload.update(overrides)
        total_forms = len(items) + 1
        payload.update(
            {
                "completion_requirements-TOTAL_FORMS": str(total_forms),
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
            }
        )
        for index, item in enumerate(items):
            payload[f"completion_requirements-{index}-item_label"] = item["item_label"]
            payload[f"completion_requirements-{index}-status"] = item["status"]
            payload[f"completion_requirements-{index}-completed_on"] = item.get(
                "completed_on", ""
            )
            payload[f"completion_requirements-{index}-notes"] = item.get("notes", "")
        payload[f"completion_requirements-{len(items)}-item_label"] = ""
        payload[f"completion_requirements-{len(items)}-status"] = CompletionRequirement.RequirementStatus.PENDING
        payload[f"completion_requirements-{len(items)}-completed_on"] = ""
        payload[f"completion_requirements-{len(items)}-notes"] = ""
        return payload

    def test_completion_requirement_honors_received_date_and_rejects_future_date(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        received_on = timezone.localdate() - timedelta(days=2)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "completed_on": received_on.isoformat(),
                    }
                ]
            ),
        )

        self.assertEqual(response.status_code, 302)
        requirement = get_completion_record(application).requirements.get()
        self.assertEqual(requirement.completed_on, received_on)

        future = timezone.localdate() + timedelta(days=1)
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "completed_on": future.isoformat(),
                    }
                ]
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The received date cannot be in the future.")
        requirement.refresh_from_db()
        self.assertEqual(requirement.completed_on, received_on)

    def test_plantilla_completion_tracking_stores_announcement_and_requirement_statuses(self):
        application = self.make_selected_application(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Validated by Secretariat.",
                    },
                    {
                        "item_label": "Medical certificate",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Awaiting submission.",
                    },
                ],
                completion_reference="PLANTILLA-APPT-001",
                announcement_reference="ANN-PL-2026-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Appointment completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(record.completion_reference, "PLANTILLA-APPT-001")
        self.assertEqual(record.announcement_reference, "ANN-PL-2026-001")
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(
            record.requirements.filter(
                item_label="Signed appointment paper",
                status=CompletionRequirement.RequirementStatus.COMPLETED,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.COMPLETION_RECORDED,
                metadata__completion_record_id=record.id,
            ).exists()
        )

    def test_cos_completion_tracking_ignores_announcement_fields_and_preserves_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Contract received.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing verified copy reused.",
                    },
                ],
                completion_reference="COS-CONTRACT-2026-001",
                announcement_reference="SHOULD-NOT-SAVE",
                announcement_date=timezone.localdate().isoformat(),
                remarks="COS contract completion tracking started.",
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        record = CompletionRecord.objects.get(application=application)
        self.assertEqual(record.branch, PositionPosting.Branch.COS)
        self.assertEqual(record.completion_reference, "COS-CONTRACT-2026-001")
        self.assertEqual(record.announcement_reference, "")
        self.assertIsNone(record.announcement_date)
        self.assertEqual(record.total_requirement_count, 2)
        self.assertTrue(record.requirements_ready_for_closure)
        self.assertTrue(record.ready_for_closure)

    def test_completion_tracking_errors_rerender_bound_wizard_form(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                ],
                deadline=(timezone.localdate() - timedelta(days=1)).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 200)
        # SL-13: a brand-new deadline still cannot be back-dated.
        self.assertContains(
            response, "A new completion deadline cannot be earlier than today."
        )
        self.assertContains(response, 'id="completion-form"')
        self.assertContains(response, "Requirement checklist")
        self.assertFalse(CompletionRecord.objects.filter(application=application).exists())

    def test_case_close_missing_notes_rerenders_closure_step_with_bound_errors(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing record reused.",
                    },
                ],
                completion_reference="COS-CONTRACT-CLOSE-ERR",
                completion_date=timezone.localdate().isoformat(),
            ),
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Closure notes are required.")
        self.assertContains(response, "This field is required.")
        self.assertContains(response, 'data-completion-step="close"')
        self.assertContains(response, 'id="closure-form"')
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_requires_completion_reference(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing record reused.",
                    },
                ],
                completion_reference="",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Attempting closure without a contract reference."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Record the contract reference before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_requires_completion_date(self):
        application = self.make_selected_application(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Filed.",
                    },
                    {
                        "item_label": "Medical clearance",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Waived.",
                    },
                ],
                completion_reference="PL-APPT-REF-002",
                completion_date="",
                announcement_reference="ANN-PL-REQ-002",
                announcement_date=timezone.localdate().isoformat(),
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Attempting closure without an appointment date."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Record the appointment date before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_resolved_checklist_alone_does_not_make_case_ready_for_closure(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Government-issued ID",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Existing record reused.",
                    },
                ],
                completion_reference="",
                completion_date="",
            ),
            follow=True,
        )

        record = CompletionRecord.objects.get(application=application)
        self.assertTrue(record.requirements_ready_for_closure)
        self.assertFalse(record.ready_for_closure)

        detail_response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "The case can't be closed yet.")
        self.assertContains(
            detail_response,
            "Record the contract reference before closing the case.",
        )
        self.assertContains(
            detail_response,
            "Record the contract date before closing the case.",
        )
        self.assertNotContains(detail_response, "Ready to Close")

    def test_case_close_requires_resolved_completion_requirements(self):
        application = self.make_selected_application(self.cos_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed contract",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Submitted.",
                    },
                    {
                        "item_label": "Tax form",
                        "status": CompletionRequirement.RequirementStatus.PENDING,
                        "notes": "Still pending.",
                    },
                ],
                completion_reference="COS-CONTRACT-LOCK",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Trying to close too early."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "All completion requirements must be marked completed or not applicable before closing the case.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertFalse(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )

    def test_case_close_locks_case_and_closed_case_remains_retrievable(self):
        application = self.make_selected_application(self.level2_position)
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            self.completion_payload(
                [
                    {
                        "item_label": "Signed appointment paper",
                        "status": CompletionRequirement.RequirementStatus.COMPLETED,
                        "notes": "Filed.",
                    },
                    {
                        "item_label": "Medical clearance",
                        "status": CompletionRequirement.RequirementStatus.NOT_APPLICABLE,
                        "notes": "Waived under recorded office rule.",
                    },
                ],
                completion_reference="PL2-APPT-001",
                announcement_reference="ANN-PL2-001",
                announcement_date=timezone.localdate().isoformat(),
                remarks="Ready for case closure.",
            ),
            follow=True,
        )

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished and archived."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, "")
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CLOSED,
            ).exists()
        )
        self.assertTrue(
            RoutingHistory.objects.filter(
                application=application,
                route_type=RoutingHistory.RouteType.CLOSE,
            ).exists()
        )

        detail_response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Completion Tracking")
