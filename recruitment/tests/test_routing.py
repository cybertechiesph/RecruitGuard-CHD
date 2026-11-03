"""Case routing, the case workflow and the routing history."""

import inspect
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..forms import CaseHandoffForm, WorkflowActionForm
from ..models import (
    AuditLog,
    CompletionRequirement,
    ExamRecord,
    FinalDecision,
    Notification,
    NotificationLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningDocumentReview,
    ScreeningRecord,
    StageRecordVersion,
    WorkflowOverride,
)
from ..requirements import get_applicant_document_requirements
from ..services import (
    get_case_handoff_options,
    get_current_workflow_section,
    get_resubmission_request,
    get_screening_record,
    get_exam_record,
    get_stage_record_versions,
    user_can_manage_screening,
    reopen_recruitment_case,
    get_available_actions,
    get_queue_for_user,
    grant_secretariat_override,
    issue_application_otp,
    process_workflow_action,
    repair_auto_advance_workflow_boundaries,
    route_case_between_secretariat_and_hrm_chief,
    remove_applicant_from_batch,
    get_latest_final_decision,
    submit_application,
    submit_document_resubmission,
    cancel_recruitment_entry,
    user_can_view_application,
    verify_application_otp,
)
from .base import BaseRecruitmentTestCase, BatchScreeningHelpers


class WorkflowRoutingTests(BaseRecruitmentTestCase):
    def test_level1_submission_routes_to_secretariat(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(routing_event.branch, PositionPosting.Branch.PLANTILLA)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_1)

    def test_level2_submission_routes_to_hrm_chief(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        routing_event = application.routing_history.get()

        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.route_type, RoutingHistory.RouteType.INITIAL)
        self.assertEqual(routing_event.to_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(routing_event.to_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(routing_event.level, PositionPosting.Level.LEVEL_2)

    def test_secretariat_cannot_process_level2_without_override(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat.",
        ):
            process_workflow_action(
                application,
                self.secretariat,
                "reject",
                "Attempted without override.",
            )

    def test_case_handoff_modal_shows_fixed_destination_for_single_target(self):
        # C4 + F9: cases move as a whole batch, offered on the vacancy page, not per case.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        case_page = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(case_page.status_code, 200)
        self.assertNotIn('name="target_role"', case_page.content.decode())
        batch_page = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertContains(batch_page, "Send the batch to the HRM Chief")

    def test_handoff_options_match_routes_the_service_accepts(self):
        level1 = self.make_application(self.level1_position)
        self.verify_application_for_submission(level1)
        submit_application(level1, self.applicant)
        self.assertEqual(
            get_case_handoff_options(level1, self.secretariat),
            [(RecruitmentUser.Role.HRM_CHIEF, "Send to HRM Chief")],
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("vacancy-batch-handoff", args=[level1.position.pk]),
            {"operation": "send", "remarks": "Routine Level 1 office handoff."},
        )
        self.assertEqual(response.status_code, 302)
        level1.refresh_from_db()
        self.assertEqual(level1.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

        level2 = self.make_application(self.level2_position)
        self.verify_application_for_submission(level2)
        submit_application(level2, self.applicant)
        self.assertEqual(
            get_case_handoff_options(level2, self.hrm_chief),
            [(RecruitmentUser.Role.SECRETARIAT, "Send to Secretariat")],
        )
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("vacancy-batch-handoff", args=[level2.position.pk]),
            {"operation": "send", "remarks": "Authorized Level 2 office handoff."},
        )
        self.assertEqual(response.status_code, 302)
        level2.refresh_from_db()
        self.assertEqual(level2.current_handler_role, RecruitmentUser.Role.SECRETARIAT)

    def test_completion_cases_offer_no_handoff_and_refuse_direct_routing(self):
        refusal = (
            "Completion is handled by the case's designated office and cannot be sent "
            "between offices."
        )
        postings = [
            self.cos_position,
            PositionPosting.objects.create(
                position_reference=self.medical_officer_position,
                branch=PositionPosting.Branch.COS,
                level=PositionPosting.Level.LEVEL_2,
                intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
                status=PositionPosting.EntryStatus.ACTIVE,
            ),
        ]

        for posting in postings:
            with self.subTest(level=posting.level):
                application = self.make_application(posting)
                self.verify_application_for_submission(application)
                submit_application(application, self.applicant)
                handler = (
                    RecruitmentUser.Role.SECRETARIAT
                    if posting.level == PositionPosting.Level.LEVEL_1
                    else RecruitmentUser.Role.HRM_CHIEF
                )
                actor = (
                    self.secretariat
                    if handler == RecruitmentUser.Role.SECRETARIAT
                    else self.hrm_chief
                )
                target = (
                    RecruitmentUser.Role.HRM_CHIEF
                    if handler == RecruitmentUser.Role.SECRETARIAT
                    else RecruitmentUser.Role.SECRETARIAT
                )
                application.status = RecruitmentApplication.Status.APPROVED
                application.current_handler_role = handler
                application.save(
                    update_fields=["status", "current_handler_role", "updated_at"]
                )
                application.case.current_stage = RecruitmentCase.Stage.COMPLETION
                application.case.current_handler_role = handler
                application.case.case_status = RecruitmentCase.CaseStatus.ACTIVE
                application.case.is_stage_locked = False
                application.case.locked_stage = ""
                application.case.save(
                    update_fields=[
                        "current_stage",
                        "current_handler_role",
                        "case_status",
                        "is_stage_locked",
                        "locked_stage",
                        "updated_at",
                    ]
                )

                self.assertEqual(get_case_handoff_options(application, actor), [])
                client = Client()
                self.force_login_with_mfa(client, actor)
                response = client.post(
                    reverse("case-handoff", kwargs={"pk": application.pk}),
                    {
                        "target_role": target,
                        "remarks": "Forced completion-stage handoff.",
                    },
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn(
                    refusal,
                    [
                        message.message
                        for message in get_messages(response.wsgi_request)
                    ],
                )
                with self.assertRaisesMessage(ValueError, refusal):
                    route_case_between_secretariat_and_hrm_chief(
                        application,
                        actor,
                        target,
                        "Forced completion-stage handoff.",
                    )
                application.refresh_from_db()
                application.case.refresh_from_db()
                self.assertEqual(application.current_handler_role, handler)
                self.assertEqual(
                    application.case.current_stage,
                    RecruitmentCase.Stage.COMPLETION,
                )

    def test_handoff_is_hidden_and_refused_while_resubmission_is_pending(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        first_requirement = get_applicant_document_requirements(application)[0]
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                "completeness_notes": "A clearer document is required.",
                "qualification_outcome": "",
                "screening_notes": "Waiting for the corrected file.",
                **self.screening_document_status_payload(
                    application,
                    overrides={
                        first_requirement.code: (
                            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                        ),
                    },
                ),
                f"document_remarks__{first_requirement.code}": "Upload a readable copy.",
                "operation": "save",
            },
        )
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application,
                self.secretariat,
                "return_to_applicant",
                "Please upload the corrected document.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        requirements_before, deadline_before, remarks_before = get_resubmission_request(
            application
        )
        self.assertEqual([item.code for item in requirements_before], [first_requirement.code])
        self.assertIsNotNone(deadline_before)
        self.assertIn(first_requirement.code, remarks_before)
        self.assertEqual(get_case_handoff_options(application, self.secretariat), [])

        refusal = (
            "This case is waiting for the applicant's resubmission. Wait for it, or remove "
            "the applicant from the batch, before sending the case."
        )
        with self.assertRaisesMessage(ValueError, refusal):
            route_case_between_secretariat_and_hrm_chief(
                application,
                self.secretariat,
                RecruitmentUser.Role.HRM_CHIEF,
                "Attempted office handoff while waiting.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.case_status,
            RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        requirements_after, deadline_after, remarks_after = get_resubmission_request(
            application
        )
        self.assertEqual([item.code for item in requirements_after], [first_requirement.code])
        self.assertEqual(deadline_after, deadline_before)
        self.assertEqual(remarks_after, remarks_before)

        otp_code = issue_application_otp(application, actor=self.applicant)
        verify_application_otp(application, otp_code, actor=self.applicant)
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            submit_document_resubmission(
                application,
                self.applicant,
                {
                    first_requirement.code: self.build_valid_applicant_document_upload(
                        first_requirement.code,
                        content_prefix="handoff-guard-resubmission",
                    )
                },
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertEqual(
            application.current_handler_role,
            RecruitmentUser.Role.SECRETARIAT,
        )

    def test_case_handoff_form_collapses_single_authorized_target(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        form = CaseHandoffForm(application=application, user=self.hrm_chief)

        self.assertTrue(form.target_role_is_fixed)
        self.assertEqual(form.target_role_fixed_label, "Send to Secretariat")
        self.assertEqual(form.fields["target_role"].widget.input_type, "hidden")
        self.assertEqual(form["target_role"].value(), RecruitmentUser.Role.SECRETARIAT)

    def test_workflow_action_form_collapses_single_authorized_action(self):
        application = self.make_application(self.level1_position)
        with patch(
            "recruitment.forms.get_available_actions",
            return_value=[
                (
                    "return_car_for_reassessment",
                    "Return CAR for correction (ETE / recommendation)",
                )
            ],
        ):
            form = WorkflowActionForm(application=application, user=self.appointing)

        self.assertTrue(form.action_is_fixed)
        self.assertEqual(
            form.action_fixed_label,
            "Return CAR for correction (ETE / recommendation)",
        )
        self.assertEqual(form.fields["action"].widget.input_type, "hidden")
        self.assertEqual(form["action"].value(), "return_car_for_reassessment")

        html = render_to_string(
            "internal_includes/workflow_action_card.html",
            {"action_form": form, "application": application},
        )
        self.assertIn("Return CAR for correction (ETE / recommendation)", html)
        self.assertIn(
            'type="hidden" name="action" value="return_car_for_reassessment"', html
        )
        self.assertNotIn('<select name="action"', html)

    def test_secretariat_cannot_view_or_queue_level2_without_override_even_if_misassigned(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])

        self.assertFalse(user_can_view_application(self.secretariat, application))
        self.assertFalse(get_queue_for_user(self.secretariat).filter(pk=application.pk).exists())

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_legacy_override_endpoint_rejects_unauthorized_case_before_processing(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("workflow-override", kwargs={"pk": application.pk}),
            {"reason": "Unauthorized probing."},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(WorkflowOverride.objects.filter(application=application).exists())

    def test_level2_plantilla_override_exam_routes_through_chief_to_hrmpsb(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("vacancy-batch-handoff", args=[application.position.pk]),
            {"operation": "send", "remarks": "Controlled screening support."},
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            screening_notes="Override-backed screening completed.",
        )
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertFalse(WorkflowOverride.objects.filter(application=application, is_active=True).exists())
        routing_events = list(
            application.routing_history.values_list(
                "route_type",
                "from_handler_role",
                "to_handler_role",
            )
        )
        self.assertEqual(
            routing_events,
            [
                (RoutingHistory.RouteType.INITIAL, "", RecruitmentUser.Role.HRM_CHIEF),
                (
                    RoutingHistory.RouteType.OVERRIDE,
                    RecruitmentUser.Role.HRM_CHIEF,
                    RecruitmentUser.Role.SECRETARIAT,
                ),
                (
                    RoutingHistory.RouteType.FORWARD,
                    RecruitmentUser.Role.SECRETARIAT,
                    RecruitmentUser.Role.HRM_CHIEF,
                ),
                (
                    RoutingHistory.RouteType.FORWARD,
                    RecruitmentUser.Role.HRM_CHIEF,
                    RecruitmentUser.Role.HRMPSB_MEMBER,
                ),
            ],
        )
        override_audits = AuditLog.objects.filter(
            application=application,
            action=AuditLog.Action.OVERRIDE_USED,
        )
        self.assertEqual(override_audits.count(), 1)
        self.assertEqual(
            override_audits.get().description,
            "Level 2 Secretariat authorization ended after automatic case advancement.",
        )
        automatic_routes = list(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.ROUTED,
                metadata__auto_routed=True,
            )
            .order_by("id")
            .values_list("description", flat=True)
        )
        self.assertEqual(
            automatic_routes,
            [
                "Automatically returned to HRM Chief after Plantilla examination was finalized by Secretariat.",
                "Automatically routed to HRMPSB after Plantilla examination was finalized by Secretariat under HRM Chief authorization.",
            ],
        )

    def test_level1_plantilla_exam_still_routes_directly_to_hrmpsb(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)

        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

    def test_level2_cos_override_exam_still_returns_to_hrm_chief(self):
        cos_level2_position = PositionPosting.objects.create(
            position_reference=self.medical_officer_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_2,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        application = self.make_application(cos_level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        route_case_between_secretariat_and_hrm_chief(
            application,
            self.hrm_chief,
            RecruitmentUser.Role.SECRETARIAT,
            "Secretariat will administer the authorized COS examination.",
        )
        self.finalize_screening_for_current_stage(application, self.secretariat)

        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertFalse(
            WorkflowOverride.objects.filter(application=application, is_active=True).exists()
        )
        self.assertFalse(
            application.routing_history.filter(
                to_handler_role=RecruitmentUser.Role.HRMPSB_MEMBER
            ).exists()
        )

    def test_secretariat_can_return_handed_off_level2_case_to_hrm_chief(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("vacancy-batch-handoff", args=[application.position.pk]),
            {"operation": "send", "remarks": "Controlled screening support."},
        )
        self.assertEqual(response.status_code, 302)

        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("vacancy-batch-handoff", args=[application.position.pk]),
            {"operation": "send", "remarks": "Returning to HRM Chief."},
        )
        self.assertEqual(response.status_code, 302)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(WorkflowOverride.objects.filter(application=application, is_active=True).exists())

    def test_repair_auto_advance_moves_stale_exam_boundary_case(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        case = application.case
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])
        case.current_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        case.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.save(
            update_fields=[
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "updated_at",
            ]
        )

        self.assertEqual(get_available_actions(application, self.secretariat), [])
        repaired = repair_auto_advance_workflow_boundaries(actor=None)

        application.refresh_from_db()
        self.assertEqual(len(repaired), 1)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRMPSB_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

    def test_repair_auto_advance_moves_stale_car_boundary_case(self):
        application = self.make_application(self.level1_position)
        self.move_application_to_appointing_review(application)

        application.refresh_from_db()
        case = application.case
        application.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        application.status = RecruitmentApplication.Status.HRMPSB_REVIEW
        application.save(update_fields=["current_handler_role", "status", "updated_at"])
        case.current_stage = RecruitmentCase.Stage.HRMPSB_REVIEW
        case.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.save(
            update_fields=[
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "updated_at",
            ]
        )

        self.assertEqual(get_available_actions(application, self.hrmpsb), [])
        repaired = repair_auto_advance_workflow_boundaries(actor=None)

        application.refresh_from_db()
        self.assertEqual(len(repaired), 1)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)

    def test_secretariat_cannot_view_selected_level2_case_without_authorized_basis(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_appointing_review(application)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Select Level 2 applicant for HRM Chief completion tracking.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertFalse(user_can_view_application(self.secretariat, application))

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_override_is_limited_to_active_hrm_chief_review_stage(self):
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)

        with self.assertRaisesMessage(
            ValueError,
            "Special authorization is only available while the case is assigned to HRM Chief review.",
        ):
            grant_secretariat_override(
                application=application,
                actor=self.hrm_chief,
                reason="Improper reroute attempt.",
            )

        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRMPSB_MEMBER)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertFalse(WorkflowOverride.objects.filter(application=application).exists())

    def test_cos_skips_hrmpsb_and_appointing_authority_stage(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)

        self.finalize_screening_for_current_stage(application, self.hrm_chief)
        self.finalize_exam_for_current_stage(application, self.hrm_chief)
        self.finalize_interview_for_current_stage(application, self.hrm_chief)
        application.refresh_from_db()
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertEqual(application.status, RecruitmentApplication.Status.HRM_CHIEF_REVIEW)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        # SL-01: finalizing the vacancy deliberation pick IS the COS selection — the case
        # goes straight from HRM Chief Review to completion, never via HRMPSB or the
        # Appointing Authority.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_deliberation_for_current_stage(application, self.hrm_chief)
        decision = get_latest_final_decision(application)
        application.refresh_from_db()
        self.assertEqual(decision.decided_by, self.hrm_chief)
        self.assertEqual(decision.review_stage, RecruitmentCase.Stage.HRM_CHIEF_REVIEW)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)


class RecruitmentCaseWorkflowTests(BaseRecruitmentTestCase):
    def test_submission_creates_one_recruitment_case(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertTrue(hasattr(application, "case"))
        self.assertEqual(RecruitmentCase.objects.filter(application=application).count(), 1)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_CREATED,
            ).exists()
        )

    def test_stage_progression_updates_case_stage_in_defined_order(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)

        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(application, self.hrmpsb, ranking_position=1)
        self.finalize_car_for_current_stage(application, self.hrmpsb)
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW)

        with self.captureOnCommitCallbacks(execute=True):
            self.record_final_decision_for_current_stage(
                application,
                self.appointing,
                decision_outcome=FinalDecision.Outcome.SELECTED,
                decision_notes="Approved for completion.",
            )
        application.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.status, RecruitmentApplication.Status.APPROVED)

    def test_stage_entered_at_updates_when_case_enters_new_stage(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        old_stage_entered_at = timezone.now() - timedelta(days=2)
        application.case.stage_entered_at = old_stage_entered_at
        application.case.save(update_fields=["stage_entered_at", "updated_at"])

        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.HRMPSB_REVIEW)
        self.assertGreater(application.case.stage_entered_at, old_stage_entered_at)
        self.assertGreater(application.case.time_in_current_stage, timedelta(seconds=0))

    def test_stage_sla_state_uses_default_thresholds_and_pauses_awaiting_resubmission(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        case = application.case

        case.stage_entered_at = timezone.now() - timedelta(days=4, minutes=59)
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.save(update_fields=["stage_entered_at", "case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "ok")

        case.stage_entered_at = timezone.now() - timedelta(days=5)
        case.save(update_fields=["stage_entered_at", "updated_at"])
        self.assertEqual(case.stage_sla_state, "warning")
        self.assertTrue(case.stage_sla_context["is_warning"])

        case.stage_entered_at = timezone.now() - timedelta(days=7)
        case.save(update_fields=["stage_entered_at", "updated_at"])
        self.assertEqual(case.stage_sla_state, "overdue")
        self.assertTrue(case.stage_sla_context["is_overdue"])

        case.case_status = RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
        case.save(update_fields=["case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "paused")
        self.assertTrue(case.stage_sla_context["is_paused"])

        case.current_stage = RecruitmentCase.Stage.CLOSED
        case.case_status = RecruitmentCase.CaseStatus.APPROVED
        case.save(update_fields=["current_stage", "case_status", "updated_at"])
        self.assertEqual(case.stage_sla_state, "ok")

    def test_closed_case_is_locked_after_completion_and_can_be_reopened(self):
        application = self.make_selected_application(self.cos_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("completion-tracking", kwargs={"pk": application.pk}),
            {
                "completion_reference": "COS-CONTRACT-001",
                "completion_date": timezone.localdate().isoformat(),
                "deadline": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "remarks": "Contract requirements fully tracked.",
                "completion_requirements-TOTAL_FORMS": "3",
                "completion_requirements-INITIAL_FORMS": "0",
                "completion_requirements-MIN_NUM_FORMS": "0",
                "completion_requirements-MAX_NUM_FORMS": "1000",
                "completion_requirements-0-item_label": "Signed contract",
                "completion_requirements-0-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-0-notes": "Submitted.",
                "completion_requirements-1-item_label": "Government-issued ID",
                "completion_requirements-1-status": CompletionRequirement.RequirementStatus.COMPLETED,
                "completion_requirements-1-notes": "Verified.",
                "completion_requirements-2-item_label": "",
                "completion_requirements-2-status": CompletionRequirement.RequirementStatus.PENDING,
                "completion_requirements-2-notes": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        response = client.post(
            reverse("case-close", kwargs={"pk": application.pk}),
            {"closure_notes": "Completion handling finished."},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()

        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.APPROVED)
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(application.case.locked_stage, RecruitmentCase.Stage.COMPLETION)

        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("workflow-reopen", kwargs={"pk": application.pk}),
            {"reason": "Correcting the completion tracking record."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.COMPLETION)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.ACTIVE)
        self.assertFalse(application.case.is_stage_locked)
        self.assertEqual(application.current_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.CASE_REOPENED,
            ).exists()
        )
        returned_bell = Notification.objects.get(
            application=application,
            recipient=self.secretariat,
            kind=Notification.Kind.CASE_RETURNED,
        )
        self.assertEqual(
            returned_bell.title,
            f"{application.reference_label} reopened for correction",
        )
        self.assertEqual(
            returned_bell.body,
            "Correcting the completion tracking record.",
        )
        self.assertFalse(
            Notification.objects.filter(
                application=application,
                recipient=self.hrm_chief,
                kind=Notification.Kind.CASE_RETURNED,
            ).exists()
        )

    def test_reopen_refused_for_non_selected_case_in_decided_vacancy(self):
        # RD-7: once the vacancy's selection stands, a pool-resolved sibling
        # (already emailed "not selected") must not return to an active stage —
        # pool queries filter on ACTIVE, so a reopened sibling would re-enter
        # batch logic for a decided vacancy. The winner's case stays reopenable
        # (completion corrections — covered by the closed-case reopen test).
        from recruitment.services import reopen_recruitment_case

        winner = self.make_application(self.cos_position)
        self.verify_application_for_submission(winner)
        submit_application(winner, self.applicant)

        # Vacancy decided: the winner holds a SELECTED final decision; the
        # sibling was closed by pool resolution (locked at its review stage).
        # The sibling is ORM-constructed — the guard reads only the vacancy's
        # decision state and the case lock, not how the pool got there.
        FinalDecision.objects.create(
            application=winner,
            recruitment_case=winner.case,
            recruitment_entry=winner.position,
            review_stage=RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            decided_by=self.hrm_chief,
            branch=winner.branch,
            level=winner.level,
            decision_outcome=FinalDecision.Outcome.SELECTED,
            decision_notes="Selected for the COS engagement.",
        )
        sibling_user = RecruitmentUser.objects.create_user(
            username="rd7-sibling",
            password="StrongPass!234",
            role=RecruitmentUser.Role.APPLICANT,
            email="rd7-sibling@example.com",
        )
        sibling = RecruitmentApplication.objects.create(
            applicant=sibling_user,
            position=self.cos_position,
            applicant_first_name="Pool",
            applicant_last_name="Sibling",
            applicant_email="rd7-sibling@example.com",
            status=RecruitmentApplication.Status.REJECTED,
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        RecruitmentCase.objects.create(
            application=sibling,
            branch=sibling.branch,
            current_stage=RecruitmentCase.Stage.CLOSED,
            current_handler_role="",
            case_status=RecruitmentCase.CaseStatus.REJECTED,
            is_stage_locked=True,
            locked_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        )
        sibling.refresh_from_db()

        with self.assertRaisesMessage(
            ValueError, "selection is already recorded"
        ):
            reopen_recruitment_case(
                application=sibling,
                actor=self.hrm_chief,
                reason="Trying to revive a resolved sibling.",
            )

        # An undecided vacancy's locked case reopens exactly as before.
        undecided = self.make_application(self.level1_position)
        self.verify_application_for_submission(undecided)
        submit_application(undecided, self.applicant)
        undecided_case = undecided.case
        undecided_case.case_status = RecruitmentCase.CaseStatus.REJECTED
        undecided_case.is_stage_locked = True
        undecided_case.locked_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
        undecided_case.save(
            update_fields=["case_status", "is_stage_locked", "locked_stage", "updated_at"]
        )
        reopen_recruitment_case(
            application=undecided,
            actor=self.hrm_chief,
            reason="Legitimate correction on an undecided vacancy.",
        )
        undecided_case.refresh_from_db()
        self.assertFalse(undecided_case.is_stage_locked)

    def _close_case_after_not_qualified_screening(
        self,
        application,
        screening_notes="Applicant does not meet the education requirement.",
    ):
        """Drive a case to the exact terminal state the SL-12 probe used: screening
        finalized Not Qualified, which closes the case and locks it at the screening
        stage. This is the construction the finding reproduced."""
        if application.status == RecruitmentApplication.Status.DRAFT:
            self.verify_application_for_submission(application)
            with self.captureOnCommitCallbacks(execute=True):
                submit_application(application, application.applicant)
            application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                screening_notes=screening_notes,
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(
            application.case.locked_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        return application

    def test_reopening_a_finalized_screening_case_leaves_editable_work(self):
        # SL-12 AC#1/#3/#4: the old reopen unlocked the envelope and nothing else — the
        # finalized screening record kept refusing every write, so the handler got active
        # workload with no legal move. Reopening must hand back something they can change.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )
        application.refresh_from_db()
        application.case.refresh_from_db()

        record = get_screening_record(application)
        self.assertFalse(record.is_finalized)
        self.assertIsNone(record.finalized_at)
        self.assertIsNone(record.finalized_by)
        self.assertEqual(record.correction_version, 2)
        self.assertEqual(get_current_workflow_section(application), "screening")
        self.assertTrue(user_can_manage_screening(self.secretariat, application))

        # AC#3: the corrected decision saves and the case follows the normal route.
        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
            screening_notes="Corrected after re-reading the transcript of records.",
        )
        application.refresh_from_db()
        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertEqual(
            record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED
        )
        self.assertEqual(get_current_workflow_section(application), "exam")

    def test_a_reopened_record_keeps_its_previous_finalized_values(self):
        # SL-12 AC#2: correcting in place is only acceptable if the answer being replaced
        # survives, linked to who reopened it and why.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        # The case is closed, so the record has to be fetched by its own stage rather than
        # by the case's current one.
        original = get_screening_record(
            application, stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        original_finalized_at = original.finalized_at
        original_document_count = original.document_reviews.count()

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(
            application,
            self.secretariat,
            qualification_outcome=ScreeningRecord.QualificationOutcome.QUALIFIED,
            screening_notes="Corrected after re-reading the transcript of records.",
        )

        versions = list(get_stage_record_versions(application))
        self.assertEqual(len(versions), 1)
        version = versions[0]
        self.assertEqual(version.record_type, StageRecordVersion.RecordType.SCREENING)
        self.assertEqual(version.record_id, original.id)
        self.assertEqual(version.version_number, 1)
        self.assertEqual(version.reopened_by, self.hrm_chief)
        self.assertEqual(version.reopened_by_role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertIn("Transcript was misread", version.reopen_reason)
        self.assertEqual(
            version.trigger, StageRecordVersion.Trigger.AUTHORIZED_REOPEN
        )
        # The archived values are the ORIGINAL ones, not the corrected ones.
        self.assertEqual(
            version.snapshot["record"]["qualification_outcome"],
            ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
        )
        self.assertTrue(version.snapshot["record"]["is_finalized"])
        self.assertEqual(
            version.snapshot["record"]["finalized_at"], original_finalized_at.isoformat()
        )
        self.assertIn(
            "does not meet the education requirement",
            version.snapshot["record"]["screening_notes"],
        )
        self.assertEqual(
            len(version.snapshot["document_reviews"]), original_document_count
        )
        self.assertTrue(
            AuditLog.objects.filter(
                application=application,
                action=AuditLog.Action.STAGE_RECORD_REOPENED,
            ).exists()
        )

    def test_reopen_corrects_the_last_finalized_record_of_the_stage(self):
        # SL-12: the record that terminalized the case is the one the handler came back to
        # change. Reopening a case closed after the examination must not silently unlock
        # the screening verdict that was already settled and is not in dispute.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, application.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        # An Absent examination keeps the case at the Secretariat stage with both records
        # finalized — the construction where "the stage's last finalized record" matters.
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_status=ExamRecord.ExamStatus.ABSENT,
                exam_score=None,
                technical_score=None,
                general_score=None,
            )
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application=application,
                actor=self.secretariat,
                action="reject",
                remarks="Closed after the applicant missed the examination.",
            )
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertTrue(application.case.is_stage_locked)
        self.assertEqual(
            application.case.locked_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )

        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="The applicant did sit the examination; the sheet was misfiled.",
        )
        application.refresh_from_db()

        exam_record = get_exam_record(application)
        screening_record = get_screening_record(application)
        self.assertFalse(exam_record.is_finalized)
        self.assertEqual(exam_record.correction_version, 2)
        self.assertTrue(screening_record.is_finalized)
        self.assertEqual(screening_record.correction_version, 1)
        self.assertEqual(get_current_workflow_section(application), "exam")
        versions = list(get_stage_record_versions(application))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].record_type, StageRecordVersion.RecordType.EXAM)
        self.assertEqual(
            versions[0].snapshot["record"]["exam_status"], ExamRecord.ExamStatus.ABSENT
        )

    def test_correcting_the_same_record_twice_keeps_both_earlier_versions(self):
        # SL-12: the trail is append-only, so two corrections read as two corrections.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)

        for pass_number in (1, 2):
            reopen_recruitment_case(
                application=application,
                actor=self.hrm_chief,
                reason=f"Correction pass {pass_number}.",
            )
            application.refresh_from_db()
            # Re-finalizing Not Qualified closes and re-locks the case, so the next pass
            # starts from the same terminal state the first one did.
            self._close_case_after_not_qualified_screening(
                application,
                screening_notes=f"Screening verdict after correction {pass_number}.",
            )

        record = get_screening_record(
            application, stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        self.assertEqual(record.correction_version, 3)
        versions = list(
            get_stage_record_versions(application).order_by("version_number")
        )
        self.assertEqual([v.version_number for v in versions], [1, 2])
        self.assertIn(
            "does not meet the education requirement",
            versions[0].snapshot["record"]["screening_notes"],
        )
        self.assertIn(
            "after correction 1", versions[1].snapshot["record"]["screening_notes"]
        )

    def test_the_case_surface_says_the_record_is_being_corrected(self):
        # SL-12: editing a decision that is already on file must not look like ordinary
        # first-time data entry.
        application = self.make_application(self.level1_position)
        self._close_case_after_not_qualified_screening(application)
        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="Transcript was misread; the applicant does meet the requirement.",
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You are correcting a finalized screening record.")
        self.assertContains(response, "Transcript was misread")
        self.assertContains(response, "Corrected records")
        self.assertNotContains(response, "{#")
        # The archive is only half the point — the answer that is being replaced has to be
        # READABLE, or the correction trail names a version nobody can open.
        self.assertContains(response, "Not Qualified")
        self.assertContains(response, "Applicant does not meet the education requirement.")
        self.assertContains(response, "Document review at the time")

    def test_the_archived_values_render_for_each_record_type(self):
        # SL-12: the exam and interview archives must be readable too, not just screening.
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, application.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_exam_for_current_stage(
                application,
                self.secretariat,
                exam_status=ExamRecord.ExamStatus.ABSENT,
                exam_score=None,
                technical_score=None,
                general_score=None,
                exam_notes="Applicant did not appear for the scheduled examination.",
            )
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(
                application=application,
                actor=self.secretariat,
                action="reject",
                remarks="Closed after the applicant missed the examination.",
            )
        application.refresh_from_db()
        reopen_recruitment_case(
            application=application,
            actor=self.hrm_chief,
            reason="The applicant did sit the examination; the sheet was misfiled.",
        )

        version = get_stage_record_versions(application).first()
        rows = {row["label"]: row["value"] for row in version.summary_rows}
        self.assertEqual(rows["Examination"], "Absent")
        self.assertEqual(
            rows["Notes"], "Applicant did not appear for the scheduled examination."
        )
        self.assertIn("Finalized", rows)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Examination Record &mdash; version 1", html=False)
        self.assertContains(
            response, "Applicant did not appear for the scheduled examination."
        )
        self.assertNotContains(response, "{#")

    def test_case_timeline_is_visible_on_application_detail(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertContains(response, "CASE-")
        self.assertContains(response, "Qualification Screening")
        self.assertContains(response, 'rg-cws-layout rg-cws-layout--full')
        self.assertNotContains(response, "Workflow Snapshot")
        self.assertNotContains(response, "Stage Integrity")
        self.assertNotContains(response, "Recent Activity")
        self.assertNotContains(response, "Current task only")
        self.assertNotContains(response, 'data-section="cws-exam"')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, "{#")
        self.assertNotContains(response, "{% include")

    def test_application_detail_uses_compressed_pipeline_and_expanded_applicant_panel(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Step 1 of 6')
        self.assertContains(response, '<details class="rg-applicant-panel" open>')
        self.assertContains(response, "Applicant Profile &amp; Submission")
        for label in ["Screening", "Exam", "Interview", "Comparative Assessment", "Decision", "Completion"]:
            self.assertContains(
                response,
                f'<span class="rg-pipeline__label">{label}</span>',
            )
        for removed_label in ["Publication", "Intake", "Submission", "Appointment", "Archive", "Deliberation/CAR"]:
            self.assertNotContains(
                response,
                f'<span class="rg-pipeline__label">{removed_label}</span>',
            )

    def test_application_detail_shows_only_the_current_task_tab(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Qualification Screening</span>')
        self.assertNotContains(response, 'data-section="cws-exam"')
        self.assertNotContains(response, 'data-section="cws-interview"')
        self.assertNotContains(response, 'data-section="cws-actions"')

    def test_cos_screening_checklist_uses_cos_document_requirements(self):
        application = self.make_application(self.cos_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Signed Application Letter")
        self.assertContains(response, "Transcript of Records (TOR)")
        self.assertContains(response, "Training Certificates")
        self.assertNotContains(response, "Performance Rating in the last rating period")

    def test_application_detail_header_hides_coarse_review_status_pill(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        match = re.search(
            r'(?s)<div class="rg-cws-header__facts"[^>]*>(.*?)</div>\s*<div class="rg-cws-header__actions">',
            content,
        )
        self.assertIsNotNone(match)
        header_facts = match.group(1)
        self.assertIn("Plantilla", header_facts)
        self.assertIn("Level 2", header_facts)
        self.assertIn("Assigned to HRM Chief", header_facts)
        self.assertNotIn("HRM Chief Review", header_facts)
        self.assertNotIn("rg-pill", header_facts)

    def test_pipeline_advances_to_exam_after_screening_is_finalized(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            completeness_notes="",
            screening_notes="",
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rg-cws-stage-titlebar__title">Examination</span>')
        self.assertContains(response, "Step 2 of 6")
        content = response.content.decode()
        self.assertRegex(
            content,
            r'(?s)rg-pipeline__stage is-complete">.*?<span class="rg-pipeline__label">Screening</span>',
        )
        self.assertRegex(
            content,
            r'(?s)rg-pipeline__stage is-current">.*?<span class="rg-pipeline__label">Exam</span>',
        )

    def test_not_qualified_screening_closes_the_case_at_level_two(self):
        # RD-1: Not Qualified IS the definitive cut — a reviewer who wants the
        # applicant to fix documents returns them from step 2 instead. So the
        # old "finalized as failed, but parked at screening awaiting a
        # reject-or-return choice" state no longer exists: the case closes.
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        self.finalize_screening_for_current_stage(
            application,
            self.hrm_chief,
            qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
            screening_notes="Applicant does not meet the Qualification Standards.",
        )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Endorse to HRMPSB")
        self.assertNotContains(response, 'data-section="cws-exam"')

    def test_definitive_not_qualified_screening_auto_rejects_and_notifies(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            self.finalize_screening_for_current_stage(
                application,
                self.secretariat,
                qualification_outcome=ScreeningRecord.QualificationOutcome.NOT_QUALIFIED,
                screening_notes="Does not meet the education requirement under the QS.",
            )

        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(application.status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(application.case.case_status, RecruitmentCase.CaseStatus.REJECTED)
        self.assertEqual(application.case.current_stage, RecruitmentCase.Stage.CLOSED)
        notification = NotificationLog.objects.get(
            application=application,
            notification_type=NotificationLog.NotificationType.NON_SELECTED_APPLICANT,
        )
        self.assertTrue(notification.metadata.get("cut_at_screening"))
        self.assertEqual(notification.delivery_status, NotificationLog.DeliveryStatus.SENT)
        self.assertIn("Qualification Standards", notification.body)
        self.assertNotIn("submitted document(s)", notification.body)

    def test_future_stage_posts_are_blocked_until_the_current_task_is_finalized(self):
        application = self.make_application(self.level2_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        exam_response = client.post(
            reverse("exam-review", kwargs={"pk": application.pk}),
            {
                "exam_type": ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_score": "85.00",
                "exam_result": "",
                "valid_from": timezone.localdate().isoformat(),
                "valid_until": (timezone.localdate() + timedelta(days=365)).isoformat(),
                "exam_notes": "Should be blocked before screening is finalized.",
                "operation": "save",
            },
        )
        interview_response = client.post(
            reverse("interview-session", kwargs={"pk": application.pk}),
            {
                "scheduled_for": timezone.now() + timedelta(days=1),
                "location": "Blocked Interview Room",
                "session_notes": "Should be blocked before screening is finalized.",
                "operation": "save",
            },
        )

        self.assertEqual(exam_response.status_code, 403)
        self.assertEqual(interview_response.status_code, 403)


class RoutingRowForEveryTransitionTests(BatchScreeningHelpers, BaseRecruitmentTestCase):
    """F15 round 2b: every case movement leaves a routing row, including the ones that used
    to leave only an audit entry (rejection, resubmission, removal, cancellation)."""

    def _last_route(self, application):
        return application.routing_history.order_by("-id").first()

    def test_transition_case_requires_a_route_type_and_description(self):
        from recruitment.services import routing

        signature = inspect.signature(routing.transition_case)
        for name in ("route_type", "description"):
            with self.subTest(parameter=name):
                self.assertIs(signature.parameters[name].default, inspect.Parameter.empty)

    def test_rejection_records_a_close_route(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        with self.captureOnCommitCallbacks(execute=True):
            process_workflow_action(application, self.secretariat, "reject", "Not eligible.")
        route = self._last_route(application)
        self.assertEqual(route.route_type, RoutingHistory.RouteType.CLOSE)
        self.assertEqual(route.from_status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)
        self.assertEqual(route.to_status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(route.to_stage, RecruitmentCase.Stage.CLOSED)
        self.assertEqual(route.notes, "Not eligible.")

    def test_resubmission_request_and_resubmission_each_record_a_route(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self._make_awaiting_case(application)
        request = self._last_route(application)
        self.assertEqual(request.to_status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT)
        self.assertEqual(request.from_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(request.to_handler_role, RecruitmentUser.Role.SECRETARIAT)
        self.assertEqual(request.to_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)

        first_requirement = get_applicant_document_requirements(application.branch)[0]
        otp_code = issue_application_otp(application, actor=self.applicant)
        verify_application_otp(application, otp_code, actor=self.applicant)
        application.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            submit_document_resubmission(
                application,
                self.applicant,
                {
                    first_requirement.code: self.build_valid_applicant_document_upload(
                        first_requirement.code, content_prefix="route-row-resubmission"
                    )
                },
            )
        returned = self._last_route(application)
        self.assertEqual(returned.route_type, RoutingHistory.RouteType.REOPEN)
        self.assertEqual(returned.from_status, RecruitmentApplication.Status.RETURNED_TO_APPLICANT)
        self.assertEqual(returned.to_status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

    def test_removal_from_batch_records_a_close_route(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        self._make_awaiting_case(application)
        remove_applicant_from_batch(application, self.secretariat, "Missed the deadline.")
        route = self._last_route(application)
        self.assertEqual(route.route_type, RoutingHistory.RouteType.CLOSE)
        self.assertEqual(route.to_status, RecruitmentApplication.Status.REJECTED)
        self.assertEqual(route.notes, "Missed the deadline.")

    def test_vacancy_cancellation_records_a_close_route(self):
        application = self._submit(self.make_application(self.level1_position), self.applicant)
        with self.captureOnCommitCallbacks(execute=True):
            cancel_recruitment_entry(self.level1_position, self.secretariat, "Publication disapproved.")
        route = self._last_route(application)
        self.assertEqual(route.route_type, RoutingHistory.RouteType.CLOSE)
        self.assertEqual(route.to_status, RecruitmentApplication.Status.VACANCY_CANCELLED)
