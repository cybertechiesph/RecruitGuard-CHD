"""The part of the case page that does not depend on the step (Round H): the case header,
the pipeline, the cards above the step, the notifications and history panels below it, and
the stage records every step reads. case.py adds the current step's keys on top.
"""

from django.urls import reverse
from django.utils import timezone

from ..forms import (
    ReminderNotificationForm,
    RequirementChecklistNotificationForm,
    ApplicationWithdrawalForm,
    ApplicantContactUpdateForm,
    CosVacancyDeliberationRescindForm,
    FinalSelectionRescindForm,
    AttendanceAbsenceDecisionForm,
    AttendanceLetterOfIntentForm,
    WorkflowActionForm,
    WorkflowReopenForm,
)
from ..models import (
    CompletionRequirement,
    PositionPosting,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    StageRecordVersion,
)
from ..notification_services import (
    INTERNAL_AUDIENCE_NOTIFICATION_TYPES,
    NOTIFICATION_MANAGER_ROLES,
    user_can_send_reminder_notification,
    user_can_send_requirement_checklist_notification,
)
from ..permissions import WORKFLOW_PROCESSOR_ROLES
from ..services import (
    COS_EARLIER_ROUND_RESCIND_MESSAGE,
    build_submission_packet,
    cos_pick_is_from_latest_selection_round,
    get_application_detail_tab,
    get_comparative_assessment_report,
    get_comparative_assessment_report_items_for_report,
    get_current_workflow_section,
    get_completion_record,
    get_completion_requirements,
    ETE_COMPONENT_WEIGHTS_BY_BRANCH,
    user_can_generate_entry_car_filing_copy,
    get_attendance_exceptions,
    get_cos_pick_for_case,
    get_rescinded_cos_picks_for_case,
    get_interview_fallback_ratings,
    get_stage_record_correction,
    get_stage_record_versions,
    get_pending_attendance_exception,
    rescind_cutoff_block_message,
    user_can_manage_attendance_exception,
    user_can_record_withdrawal,
    user_can_update_applicant_contact,
    user_can_rescind_cos_vacancy_deliberation,
    user_can_rescind_final_selection,
    get_final_selection_history,
    get_cos_round_pick_for_case,
    case_is_on_loan,
    get_deliberation_record,
    get_deliberation_records,
    get_exam_record,
    get_exam_records,
    get_final_decision_history,
    get_final_selection_for_application,
    get_interview_fallback_evidence,
    get_interview_rating_for_user,
    get_interview_ratings,
    get_interview_session,
    get_interview_sessions,
    get_latest_final_decision,
    _completion_handler_role,
    get_latest_finalized_comparative_assessment_report,
    get_available_actions,
    get_case_timeline,
    get_screening_record,
    get_screening_records,
    get_queue_for_user,
    user_can_export_application,
    user_can_process_application,
    user_can_reopen_case,
)


def build(application, user):
    """Everything the case page needs whichever step it shows: the header, pipeline and
    cards, the bottom panels, and the stage records the step builders read."""
    context = {}
    context["audit_log_url"] = ""
    context["recruitment_case"] = getattr(application, "case", None)
    context["current_detail_tab"] = get_application_detail_tab(application)
    context["case_timeline"] = get_case_timeline(application) if context["recruitment_case"] else []
    context["routing_history"] = application.routing_history.select_related("actor", "recruitment_case")
    context["notification_history"] = application.notifications.select_related(
        "triggered_by",
        "recruitment_case",
    )
    context["completion_record"] = get_completion_record(application)
    context["completion_requirements"] = get_completion_requirements(application)
    context["screening_records"] = get_screening_records(application)
    context["current_screening_record"] = get_screening_record(application)
    _screening_record = context["current_screening_record"]
    if _screening_record is not None:
        _reviews = list(_screening_record.document_reviews.all())
        context["screening_flagged_count"] = sum(
            1 for r in _reviews
            if r.status == ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
        )
        context["screening_not_submitted_count"] = sum(
            1 for r in _reviews
            if r.status == ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED
        )
    context["exam_records"] = get_exam_records(application)
    context["current_exam_record"] = get_exam_record(application)
    # F7: at the interview the current stage has no screening/exam of its own; the card
    # shows the latest finalized ones from the earlier stage.
    context["earlier_screening_record"] = (
        application.screening_records.filter(is_finalized=True).order_by("-finalized_at", "-id").first()
    )
    context["earlier_exam_record"] = (
        application.exam_records.filter(is_finalized=True).order_by("-finalized_at", "-id").first()
    )
    context["interview_sessions"] = get_interview_sessions(application)
    context["current_interview_session"] = get_interview_session(application)
    context["current_interview_ratings"] = get_interview_ratings(application)
    context["current_interview_fallback_evidence"] = get_interview_fallback_evidence(application)
    # SL-03: the encoded paper ratings — the rows that actually count toward the quorum
    # and the interview average. The evidence list above is the retained scans.
    context["current_interview_fallback_ratings"] = (
        get_interview_fallback_ratings(context["current_interview_session"])
        if context["current_interview_session"]
        else []
    )
    # SL-12: when a reopen unlocked a finalized record for correction, say so on the
    # section being corrected. Editing a decision that is already on file without any
    # sign that it is a correction is how a second answer quietly becomes the first.
    context["screening_correction"] = get_stage_record_correction(
        context["current_screening_record"], StageRecordVersion.RecordType.SCREENING
    )
    context["exam_correction"] = get_stage_record_correction(
        context["current_exam_record"], StageRecordVersion.RecordType.EXAM
    )
    context["interview_correction"] = get_stage_record_correction(
        context["current_interview_session"], StageRecordVersion.RecordType.INTERVIEW
    )
    context["stage_record_versions"] = list(get_stage_record_versions(application))
    # SL-06: a recorded absence waiting on the office's reschedule-or-terminal decision.
    pending_absence = get_pending_attendance_exception(application)
    context["attendance_exceptions"] = get_attendance_exceptions(application)
    context["pending_attendance_exception"] = pending_absence
    if pending_absence and user_can_manage_attendance_exception(user, pending_absence):
        context["attendance_letter_form"] = AttendanceLetterOfIntentForm()
        context["attendance_decision_form"] = AttendanceAbsenceDecisionForm()
    # SL-10: a neutral exit from an active competition, available at every pre-selection
    # stage to whoever the case is assigned to.
    if user_can_record_withdrawal(user, application):
        initial = {}
        if application.withdrawal_requested_at:
            requested_on = timezone.localtime(application.withdrawal_requested_at)
            initial["reason"] = (
                f"The applicant asked to withdraw through the applicant portal on "
                f"{requested_on:%B %d, %Y %I:%M %p}."
                + (f" Reason given: {application.withdrawal_request_reason}" if application.withdrawal_request_reason else "")
            )
        context["withdrawal_form"] = ApplicationWithdrawalForm(initial=initial)
    if user_can_update_applicant_contact(user, application):
        context["applicant_contact_form"] = ApplicantContactUpdateForm(
            application=application
        )
    # SL-14: a recorded selection is strongly controlled but no longer a one-way door.
    if application.branch == PositionPosting.Branch.PLANTILLA:
        context["final_selection_history"] = get_final_selection_history(
            application.position
        )
        final_selection = get_final_selection_for_application(application)
        if user_can_rescind_final_selection(user, final_selection):
            context["final_selection_rescind_form"] = FinalSelectionRescindForm()
        elif (
            final_selection is not None
            and user.role == RecruitmentUser.Role.APPOINTING_AUTHORITY
            and rescind_cutoff_block_message(final_selection.selected_application)
        ):
            context["final_selection_rescind_unavailable_message"] = (
                rescind_cutoff_block_message(final_selection.selected_application)
            )
    else:
        # SL-14 (COS): the same correction, on the record that carries the COS decision
        # — SL-01 made the finalized deliberation pick the selection itself.
        cos_pick = get_cos_pick_for_case(getattr(application, "case", None))
        context["cos_rescinded_picks"] = get_rescinded_cos_picks_for_case(
            getattr(application, "case", None)
        )
        if user_can_rescind_cos_vacancy_deliberation(
            user, cos_pick
        ):
            context["cos_pick_rescind_form"] = CosVacancyDeliberationRescindForm()
        elif (
            cos_pick is not None
            and user.role == RecruitmentUser.Role.HRM_CHIEF
            and rescind_cutoff_block_message(cos_pick.chosen_case.application)
        ):
            context["cos_pick_rescind_unavailable_message"] = (
                rescind_cutoff_block_message(cos_pick.chosen_case.application)
            )
        elif (
            cos_pick is not None
            and user.role == RecruitmentUser.Role.HRM_CHIEF
            and not cos_pick_is_from_latest_selection_round(cos_pick)
        ):
            context["cos_pick_rescind_unavailable_message"] = (
                COS_EARLIER_ROUND_RESCIND_MESSAGE
            )
    context["current_user_interview_rating"] = get_interview_rating_for_user(application, user)
    context["deliberation_records"] = get_deliberation_records(application)
    context["current_deliberation_record"] = get_deliberation_record(application)
    context["current_comparative_assessment_report"] = get_comparative_assessment_report(application)
    if not context["current_comparative_assessment_report"]:
        context["current_comparative_assessment_report"] = (
            get_latest_finalized_comparative_assessment_report(application)
        )
    comparative_assessment_items = list(
        get_comparative_assessment_report_items_for_report(
            context["current_comparative_assessment_report"]
        )
    )
    for item in comparative_assessment_items:
        # REV-B: CAR surfaces are blinded — rows are identified by the
        # per-vacancy candidate code, never by name or case number.
        item.candidate_label = item.application.candidate_code_label
    context["current_comparative_assessment_report_items"] = comparative_assessment_items
    # WF-11: with ETE blank the sort fallback produces an alphabetical pseudo-ranking —
    # suppress ordinals on the draft until every row carries a real assessment score.
    context["car_all_rows_scored"] = bool(comparative_assessment_items) and all(
        item.assessment_score is not None for item in comparative_assessment_items
    )
    # SL-00: the CAR split is per-vacancy, so every CAR surface states the split its
    # scores were actually computed against instead of implying the office default.
    current_car = context["current_comparative_assessment_report"]
    context["car_weights"] = (
        current_car.assessment_weights_for_display
        if current_car
        else application.position.assessment_weights_or_default
    )
    ete_weights = ETE_COMPONENT_WEIGHTS_BY_BRANCH.get(
        application.branch,
        ETE_COMPONENT_WEIGHTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA],
    )
    context["ete_component_weight_display"] = {
        "education": int(ete_weights[0] * 100),
        "training": int(ete_weights[1] * 100),
        "experience": int(ete_weights[2] * 100),
    }
    # N1: the named filing copy, offered wherever the preparer can still reach the case
    # — in practice at Completion, where the appointment paperwork is assembled.
    context["car_filing_copy_available"] = user_can_generate_entry_car_filing_copy(
        user, application.position
    )
    context["submission_packet"] = (
        build_submission_packet(application) if context["recruitment_case"] else {}
    )
    context["final_decision_history"] = get_final_decision_history(application)
    context["latest_final_decision"] = get_latest_final_decision(application)
    context["latest_final_selection"] = get_final_selection_for_application(application)
    context["decision_locked_record"] = (
        context["latest_final_decision"] or context["latest_final_selection"]
    )
    if application.branch == PositionPosting.Branch.COS:
        context["decision_record_label"] = "COS Selection"
        context["decision_actor_label"] = "HRM Chief"
        context["decision_completion_label"] = "contract"
    else:
        context["decision_record_label"] = "Final Selection"
        context["decision_actor_label"] = "Appointing Authority"
        context["decision_completion_label"] = "appointment"
    # AA review P2-1: who tracks completion, for the viewers who do not.
    context["completion_handler_label"] = RecruitmentUser.Role(
        _completion_handler_role(application)
    ).label
    # UX-03 — surface the earlier review stage's finalized screening + exam results so the
    # HRM Chief / HRMPSB re-validate against the work being checked instead of re-keying blind.
    current_review_stage = getattr(context["recruitment_case"], "current_stage", None)
    if (
        current_review_stage
        in {RecruitmentCase.Stage.HRM_CHIEF_REVIEW, RecruitmentCase.Stage.HRMPSB_REVIEW}
        and user.role in {RecruitmentUser.Role.HRM_CHIEF, RecruitmentUser.Role.HRMPSB_MEMBER}
    ):
        context["prior_screening_record"] = next(
            (
                record
                for record in reversed(list(context["screening_records"]))
                if record.is_finalized and record.review_stage != current_review_stage
            ),
            None,
        )
        context["prior_exam_record"] = next(
            (
                record
                for record in reversed(list(context["exam_records"]))
                if record.is_finalized and record.review_stage != current_review_stage
            ),
            None,
        )
    # UX-07 — batch-aware navigation: the user's next scoped case in the same vacancy and a
    # link back to that vacancy's batch. Stays entirely inside get_queue_for_user scoping.
    if user.role in WORKFLOW_PROCESSOR_ROLES and getattr(application, "position_id", None):
        scoped_batch = [
            candidate
            for candidate in get_queue_for_user(user)
            if candidate.position_id == application.position_id
        ]
        if scoped_batch:
            context["batch_url"] = reverse(
                "vacancy-batch-detail", kwargs={"pk": application.position_id}
            )
            others = [c for c in scoped_batch if c.pk != application.pk]
            if not user_can_process_application(user, application):
                context["next_batch_case"] = others[0] if others else None
            elif get_current_workflow_section(application) != "screening":
                # F11: this case's own (per-case) work is done and the rest happens as a
                # batch, so point at the next case that still needs screening.
                context["next_batch_case"] = next(
                    (c for c in others if get_current_workflow_section(c) == "screening"),
                    None,
                )
                context["next_batch_case_in_screening"] = context["next_batch_case"] is not None
    if application.branch == PositionPosting.Branch.COS:
        # SL-01: the COS selection is made once, on the deliberation console. The case's
        # Decision surface shows that authoritative pick — it never offers a second,
        # independent choice.
        # SL-02: show the pick from the round THIS case was actually considered in —
        # a candidate the pool is still holding has not been decided by anyone's round.
        pick = get_cos_round_pick_for_case(application.case)
        # SL-14 (COS): is_effective, not is_finalized — a rescinded pick keeps its
        # finalization (that history is the point), so testing is_finalized here would
        # present a voided pick as "the authoritative selection" during the exact
        # window when the vacancy is undecided and awaiting its replacement.
        if pick and pick.is_effective:
            context["cos_authoritative_pick"] = pick
            context["cos_pick_is_this_case"] = pick.chosen_case_id == application.case.id
    if user_can_process_application(user, application):
        available_actions = get_available_actions(application, user)
        if available_actions:
            context["action_form"] = WorkflowActionForm(application=application, user=user)
            screening_record = context.get("current_screening_record")
            if screening_record is not None:
                context["resubmission_document_reviews"] = list(
                    screening_record.document_reviews.filter(
                        status=ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                    ).order_by("display_order", "created_at")
                )
    # C4: cases are lent as a whole batch from the vacancy page; the header only says so.
    context["case_on_loan"] = case_is_on_loan(application)
    if context["recruitment_case"] and user_can_reopen_case(user, context["recruitment_case"]):
        context["reopen_form"] = WorkflowReopenForm()
    if user_can_send_requirement_checklist_notification(user, application):
        context["checklist_notification_form"] = RequirementChecklistNotificationForm()
        # RD-8: the dialog previews what will actually be emailed — the
        # record's pending rows and the record's deadline (one source).
        completion_record = context.get("completion_record")
        context["checklist_preview_requirements"] = (
            list(
                completion_record.requirements.filter(
                    status=CompletionRequirement.RequirementStatus.PENDING
                ).order_by("display_order", "created_at")
            )
            if completion_record
            else []
        )
    if user_can_send_reminder_notification(user, application):
        reminder_initial = {}
        completion_record = context.get("completion_record")
        if completion_record and completion_record.deadline:
            # RD-8: reminders default to the deadline the office tracks.
            reminder_initial["deadline"] = completion_record.deadline
        context["reminder_notification_form"] = ReminderNotificationForm(
            initial=reminder_initial
        )
    if user.role in NOTIFICATION_MANAGER_ROLES:
        # RD-9: delivery state visible where work happens. NOT shown to
        # HRMPSB — the log's subjects carry applicant names (blinding).
        # Only the emails that went to the applicant: staff notices (e.g. the panel's
        # interview notice) belong to the case history, not to a box that offers to
        # "Send again" to the applicant.
        context["notification_log_entries"] = list(
            application.notifications.select_related("triggered_by")
            .exclude(notification_type__in=INTERNAL_AUDIENCE_NOTIFICATION_TYPES)
            .order_by("-created_at")[:25]
        )
    context["can_export"] = user_can_export_application(user, application)
    return context
