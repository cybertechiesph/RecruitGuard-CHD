"""The case page (ApplicationDetailView) and the case-level actions: workflow action,
handoff, override, reopen, withdrawal, contact update and resubmission deadline.
"""

from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from ..forms import (
    CaseClosureForm,
    ComparativeAssessmentReportForm,
    CompletionRequirementFormSet,
    CompletionTrackingForm,
    DeliberationRecordForm,
    ExamRecordForm,
    ExamScheduleForm,
    EvidenceUploadForm,
    FinalSelectionForm,
    NoAppointmentForm,
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    InterviewSessionForm,
    ReminderNotificationForm,
    SimpleInterviewRatingForm,
    RequirementChecklistNotificationForm,
    ScreeningReviewForm,
    ApplicationWithdrawalForm,
    ApplicantContactUpdateForm,
    CosVacancyDeliberationRescindForm,
    FinalSelectionRescindForm,
    AttendanceAbsenceDecisionForm,
    AttendanceLetterOfIntentForm,
    InterviewAbsenceForm,
    ResubmissionDeadlineExtensionForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from ..models import (
    CompletionRecord,
    CompletionRequirement,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
    StageRecordVersion,
)
from ..notification_services import (
    INTERNAL_AUDIENCE_NOTIFICATION_TYPES,
    NOTIFICATION_MANAGER_ROLES,
    user_can_send_reminder_notification,
    user_can_send_requirement_checklist_notification,
)
from ..permissions import (
    InternalUserRequiredMixin,
    WorkflowProcessorRequiredMixin,
    WORKFLOW_PROCESSOR_ROLES,
)
from ..services import (
    COMPLETION_HANDOFF_MESSAGE,
    COS_EARLIER_ROUND_RESCIND_MESSAGE,
    FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES,
    application_awaiting_car_preparation,
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    plantilla_car_preparer_role_label,
    build_submission_packet,
    cos_pick_is_from_latest_selection_round,
    get_application_detail_tab,
    get_applicant_document_review_items,
    get_comparative_assessment_report,
    get_comparative_assessment_report_items_for_report,
    get_comparative_assessment_readiness,
    get_current_workflow_section,
    get_completion_record,
    get_completion_requirements,
    ETE_COMPONENT_WEIGHTS_BY_BRANCH,
    user_can_generate_entry_car_filing_copy,
    get_attendance_exceptions,
    get_car_candidate_fact_rows,
    get_cos_pick_for_case,
    get_rescinded_cos_picks_for_case,
    get_interview_fallback_ratings,
    get_stage_record_correction,
    get_stage_record_versions,
    get_pending_attendance_exception,
    interview_fallback_panel_queryset,
    record_application_withdrawal,
    extend_document_resubmission_deadline,
    rescind_cutoff_block_message,
    update_applicant_contact,
    user_can_extend_resubmission_deadline,
    user_can_manage_attendance_exception,
    user_can_record_withdrawal,
    user_can_update_applicant_contact,
    user_can_rescind_cos_vacancy_deliberation,
    user_can_rescind_final_selection,
    get_final_selection_history,
    final_selection_recommended_case_ids,
    user_can_follow_level_two_case,
    get_cos_round_pick_for_case,
    case_is_on_loan,
    get_deliberation_record,
    get_deliberation_records,
    get_exam_record,
    get_exam_schedule,
    get_notified_exam_sitting,
    get_exam_records,
    get_final_decision_history,
    get_final_selection_for_application,
    get_applicant_pool_finalization_block_message,
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
    grant_secretariat_override,
    process_workflow_action,
    record_protected_record_access,
    reopen_recruitment_case,
    vacancy_uses_batch_exam,
    user_can_close_case,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
    user_can_manage_completion,
    user_can_export_application,
    user_can_manage_exam,
    user_can_manage_interview_rating,
    interview_scoring_is_open,
    scoring_opens_message,
    get_published_competency_rating_template,
    user_can_manage_interview_session,
    user_can_manage_screening,
    user_can_process_application,
    vacancy_screening_batch_block_message,
    user_can_record_final_selection,
    user_can_record_no_appointment,
    user_can_reopen_case,
    user_can_upload_interview_fallback,
    user_can_upload_evidence,
    user_can_view_application,
    user_is_interview_rating_support_encoder,
)
from .common import _format_form_errors


def _render_application_detail_with_overrides(request, application, **overrides):
    view = ApplicationDetailView()
    view.request = request
    view.args = ()
    view.kwargs = {"pk": application.pk}
    view.object = application
    context = view.get_context_data(object=application)
    context.update(overrides)
    return render(request, view.template_name, context)


class ApplicationDetailView(LoginRequiredMixin, InternalUserRequiredMixin, DetailView):
    model = RecruitmentApplication
    template_name = "recruitment/application_detail.html"
    context_object_name = "application"

    def get_object(self, queryset=None):
        application = super().get_object(queryset)
        if not user_can_view_application(self.request.user, application):
            raise Http404
        return application

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context["application"]
        user = self.request.user
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
        context["applicant_document_review_items"] = get_applicant_document_review_items(application)
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
        if user_can_extend_resubmission_deadline(user, application):
            screening_record = context["current_screening_record"]
            context["resubmission_deadline_extension_form"] = (
                ResubmissionDeadlineExtensionForm(
                    current_deadline=screening_record.resubmission_deadline
                )
            )
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
        context["interview_rating_is_support_encoding"] = user_is_interview_rating_support_encoder(
            user,
            application,
        )
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
        context["car_readiness"] = get_comparative_assessment_readiness(application)
        # The ETE grid lists the draft's rows; before the first draft it lists the pool,
        # so the scores can be typed first and one "Prepare draft CAR" saves and ranks.
        car_ete_rows = comparative_assessment_items or [
            SimpleNamespace(
                recruitment_case_id=pool_case.id,
                application=pool_case.application,
                candidate_label=pool_case.application.candidate_code_label,
            )
            for pool_case in context["car_readiness"].get("draft_candidate_cases", [])
        ]
        context["car_ete_rows"] = car_ete_rows
        # SL-08A: the QS and each candidate's screened credentials, so the ETE verdict is
        # made against the record rather than from memory.
        context["car_candidate_facts"] = get_car_candidate_fact_rows(car_ete_rows)
        context["car_requires_deliberation"] = False
        context["car_requires_finalized_deliberation"] = False
        context["car_finalize_block_message"] = context["car_readiness"].get(
            "finalize_block_message",
            "",
        )
        context["car_prepare_block_message"] = context["car_readiness"].get(
            "prepare_block_message",
            "",
        )
        context["evidence_items"] = []
        context["can_archive_evidence"] = False
        context["evidence_vault_url"] = ""
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
        if user_can_upload_evidence(user, application):
            context["evidence_form"] = EvidenceUploadForm()
        if user_can_manage_screening(user, application):
            screening_record = context["current_screening_record"]
            if screening_record and screening_record.is_finalized:
                context["screening_locked"] = True
            else:
                context["screening_form"] = ScreeningReviewForm(
                    instance=screening_record,
                    application=application,
                )
        # RD-1: completeness is computed for the banner (never asked), and the
        # flag count drives which decision the reviewer is offered.
        if context.get("screening_form") is not None:
            review_items = context["screening_form"].document_review_fields
            required_items = [i for i in review_items if i["is_required_for_completeness"]]
            flagged = [
                i
                for i in review_items
                if i["field"].value()
                == ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
            ]
            pending = [
                i
                for i in required_items
                if i["field"].value()
                in ScreeningRecord.COMPLETENESS_BLOCKING_DOCUMENT_STATUSES
                or not i["field"].value()
            ]
            context["screening_required_count"] = len(required_items)
            context["screening_flagged_now_count"] = len(flagged)
            context["screening_pending_count"] = len(pending)
            context["screening_documents_complete"] = not pending
            # The QS the verdict is judged against, so step 2 reads as a
            # comparison rather than a floating question.
            context["screening_qualification_reference"] = (
                application.position.qualification_reference or ""
            )
            context["screening_position_reference"] = application.position.position_reference
        if user_can_manage_exam(user, application):
            exam_record = context["current_exam_record"]
            # The sitting the applicant was told about, even when it was scheduled at an
            # earlier review stage (the HRM Chief re-checking the Secretariat's exam).
            exam_schedule = get_notified_exam_sitting(application) or get_exam_schedule(application)
            context["current_exam_schedule"] = exam_schedule
            if exam_record and exam_record.is_finalized:
                context["exam_locked"] = True
            elif vacancy_uses_batch_exam(application.position):
                # RD-2: one door. This vacancy's pool is scheduled, scored, and
                # finalized together in the batch console, so the per-case screen
                # is a read-only summary — no exam_form, no schedule form.
                context["exam_batch_managed"] = True
                context["exam_batch_url"] = reverse(
                    "vacancy-batch-exam", kwargs={"pk": application.position.pk}
                )
            else:
                context["exam_form"] = ExamRecordForm(
                    instance=exam_record,
                    application=application,
                )
                context["exam_schedule_form"] = ExamScheduleForm(instance=exam_schedule)
        elif (
            context.get("current_detail_tab", {}).get("key") == "exam"
            and user.role in {RecruitmentUser.Role.SECRETARIAT, RecruitmentUser.Role.HRM_CHIEF}
            # SL-04: only a batch vacancy can be waiting on its batch. A continuous COS case
            # is never on hold for a sibling, so it must not be told that it is.
            and vacancy_uses_batch_exam(application.position)
        ):
            # The case's own screening is done (section is exam) but the vacancy's exam batch is
            # on hold because a sibling still needs a screening decision (§4.3).
            batch_block = vacancy_screening_batch_block_message(application.position)
            if batch_block:
                context["exam_batch_block_message"] = batch_block
        interview_is_batch_managed = vacancy_uses_batch_exam(application.position)
        if interview_is_batch_managed:
            context["interview_batch_url"] = reverse(
                "vacancy-batch-interview", kwargs={"pk": application.position.pk}
            )
        if user_can_manage_interview_session(user, application):
            interview_session = context["current_interview_session"]
            if interview_session and interview_session.is_finalized:
                context["interview_session_locked"] = True
            elif interview_is_batch_managed:
                # RD-4: one door. This vacancy's pool is scheduled and finalized
                # together in the batch interview console, so the per-case screen
                # is a read-only summary — no editable session form.
                context["interview_session_batch_managed"] = True
            else:
                context["interview_session_form"] = InterviewSessionForm(instance=interview_session)
        if user_can_manage_interview_rating(user, application):
            interview_session = context["current_interview_session"]
            if not interview_session:
                context["interview_rating_requires_session"] = True
            elif interview_session.is_finalized:
                context["interview_rating_locked"] = True
            elif not interview_scoring_is_open(interview_session):
                context["interview_rating_opens_message"] = scoring_opens_message(
                    interview_session.scheduled_for
                )
            elif application.branch == PositionPosting.Branch.COS:
                # COS interviews are scored with a single end-user / HRMS number,
                # not the competency rating sheet (spec §6.2).
                if interview_is_batch_managed:
                    # RD-4: a pooled COS single score is recorded in the console.
                    context["interview_rating_batch_managed"] = True
                else:
                    existing_rating = context["current_user_interview_rating"]
                    context["interview_rating_is_simple"] = True
                    context["interview_rating_form"] = SimpleInterviewRatingForm(
                        initial={
                            "rating_score": existing_rating.rating_score if existing_rating else None,
                            "rating_notes": existing_rating.rating_notes if existing_rating else "",
                            "justification": existing_rating.justification if existing_rating else "",
                        }
                    )
            else:
                rating_template = get_published_competency_rating_template(application.position)
                if rating_template is None:
                    context["interview_rating_requires_sheet"] = True
                else:
                    context["interview_rating_template"] = rating_template
                    context["interview_rating_form"] = InterviewRatingForm(
                        instance=context["current_user_interview_rating"],
                        application=application,
                        actor=user,
                        template=rating_template,
                    )
        if user_can_upload_interview_fallback(user, application):
            interview_session = context["current_interview_session"]
            if not interview_session:
                context["interview_fallback_requires_session"] = True
            elif interview_session.is_finalized:
                context["interview_fallback_locked"] = True
            else:
                # SL-03: the encoder picks the member whose sheet they are encoding, from
                # the panel that was actually invited to this interview.
                context["interview_fallback_form"] = InterviewFallbackUploadForm(
                    panel_members=interview_fallback_panel_queryset(application),
                )
            if (
                interview_session
                and not interview_session.is_finalized
                # A pooled interview records absence on the batch console, so the per-case
                # screen stays the read-only summary the one-door rule intends.
                and not vacancy_uses_batch_exam(application.position)
                and user_can_manage_interview_session(user, application)
            ):
                # SL-06: recording a no-show must be as available as recording a score.
                context["interview_absence_form"] = InterviewAbsenceForm()
        applicant_pool_is_blocked = (
            application_requires_finalized_applicant_pool(application)
            and not application_has_finalized_applicant_pool(application)
        )
        if applicant_pool_is_blocked:
            context["deliberation_requires_vacancy_closure"] = True
            context["deliberation_vacancy_closure_message"] = (
                get_applicant_pool_finalization_block_message(application)
            )
        elif user_can_manage_deliberation(user, application):
            deliberation_record = context["current_deliberation_record"]
            report = context["current_comparative_assessment_report"]
            if deliberation_record and deliberation_record.is_finalized:
                context["deliberation_locked"] = True
            elif (
                application.branch == PositionPosting.Branch.PLANTILLA
                and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
                and not report
            ):
                context["deliberation_requires_car_draft"] = True
            else:
                context["deliberation_form"] = DeliberationRecordForm(
                    instance=deliberation_record,
                    application=application,
                )
                if (
                    application.branch == PositionPosting.Branch.PLANTILLA
                    and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
                    and report
                    and not report.is_finalized
                ):
                    if deliberation_record is None:
                        context["car_requires_deliberation"] = True
                    elif not deliberation_record.is_finalized:
                        context["car_requires_finalized_deliberation"] = True
        elif (
            application.branch == PositionPosting.Branch.COS
            and application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
            and get_current_workflow_section(application) == "deliberation"
        ):
            # COS deliberation is one per-vacancy pick recorded on the console (spec §6.3).
            context["cos_deliberation_section"] = True
            context["cos_deliberation_pick"] = get_cos_round_pick_for_case(application.case)
        if not applicant_pool_is_blocked and user_can_manage_comparative_assessment_report(user, application):
            report = context["current_comparative_assessment_report"]
            if report and report.is_finalized:
                context["car_locked"] = True
            else:
                context["car_form"] = ComparativeAssessmentReportForm(instance=report)
        elif (
            user.role == RecruitmentUser.Role.HRMPSB_MEMBER
            and application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
            and get_current_workflow_section(application) == "car"
            and context["current_comparative_assessment_report"]
        ):
            # The HRMPSB board gets a read-only ranked CAR to deliberate off-system (spec §5.4).
            context["car_readonly"] = True
        elif (
            user_can_follow_level_two_case(user, application)
            and get_current_workflow_section(application) == "car"
            and context["current_comparative_assessment_report"]
        ):
            # C2: the HRM Chief follows the Level 2 CAR the Secretariat prepares.
            context["car_readonly"] = True
            context["car_readonly_following"] = True
        # When the CAR card would otherwise be blank for this viewer (e.g. an HRMPSB member whose
        # board is still waiting on the Secretariat/HRM Chief to prepare the CAR), surface a
        # "CAR in preparation" state instead of an empty titlebar. Does not change access.
        context["car_in_preparation"] = (
            not context.get("car_form")
            and not context.get("car_locked")
            and not context.get("car_readonly")
            and application_awaiting_car_preparation(user, application)
        )
        if context["car_in_preparation"]:
            label = plantilla_car_preparer_role_label(application)
            context["car_preparer_role_label"] = label
            context["car_in_preparation_copy"] = (
                "The Comparative Assessment Report for this vacancy is being prepared by the "
                f"{label}. You will be able to record the deliberation once the CAR is finalized."
            )
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
        if user_can_record_final_selection(user, application):
            report = context["current_comparative_assessment_report"]
            selection_items = list(context["current_comparative_assessment_report_items"])
            recommended_case_ids = final_selection_recommended_case_ids(report)
            for item in selection_items:
                item.is_out_of_contention = (
                    item.application.status
                    in FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES
                )
                item.is_selection_recommended = (
                    not report.recommends_none
                    and (
                        not recommended_case_ids
                        or item.recruitment_case_id in recommended_case_ids
                    )
                )
            context["final_selection_report"] = report
            context["final_selection_items"] = selection_items
            context["final_selection_form"] = FinalSelectionForm(report=report)
            if user_can_record_no_appointment(user, application):
                context["no_appointment_form"] = NoAppointmentForm()
        if user_can_manage_completion(user, application):
            completion_record = context["completion_record"]
            requirement_instance = completion_record or CompletionRecord(
                application=application,
                recruitment_case=application.case,
                tracked_by=user,
            )
            context["completion_form"] = CompletionTrackingForm(
                instance=completion_record,
                application=application,
                actor=user,
            )
            context["completion_requirement_formset"] = CompletionRequirementFormSet(
                instance=requirement_instance,
                prefix="completion_requirements",
            )
        if user_can_close_case(user, application):
            context["closure_form"] = CaseClosureForm()
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
        record_protected_record_access(
            application=application,
            actor=user,
            source="application_detail",
        )
        return context


class WorkflowActionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def _should_rerender_bound_action_form(self, form, request):
        action = ""
        if getattr(form, "is_bound", False) and "action" in getattr(form, "cleaned_data", {}):
            action = form.cleaned_data["action"]
        action = action or request.POST.get("action", "")
        return action == "return_car_for_reassessment"

    def _render_with_bound_action_form(self, request, application, form, message):
        if not form.errors and "remarks" in form.fields:
            form.add_error("remarks", message)
        messages.error(request, message)
        return _render_application_detail_with_overrides(
            request,
            application,
            action_form=form,
        )

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_process_application(request.user, application):
            raise PermissionDenied
        form = WorkflowActionForm(request.POST, application=application, user=request.user)
        if form.is_valid():
            try:
                process_workflow_action(
                    application=application,
                    actor=request.user,
                    action=form.cleaned_data["action"],
                    remarks=form.cleaned_data["remarks"],
                )
            except ValueError as exc:
                if self._should_rerender_bound_action_form(form, request):
                    return self._render_with_bound_action_form(
                        request,
                        application,
                        form,
                        str(exc),
                    )
                messages.error(request, str(exc))
            else:
                if form.cleaned_data["action"] == "return_car_for_reassessment":
                    # AA review P3-1: say what the return did.
                    messages.success(
                        request,
                        "CAR returned to the HRMPSB for correction. The candidates' cases are "
                        "back at the HRMPSB step.",
                    )
                else:
                    messages.success(request, "Next step saved.")
        else:
            message = _format_form_errors(form, "Choose an allowed next step.")
            if self._should_rerender_bound_action_form(form, request):
                return self._render_with_bound_action_form(
                    request,
                    application,
                    form,
                    message,
                )
            messages.error(request, message)
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class CaseHandoffView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        if application.case.current_stage == RecruitmentCase.Stage.COMPLETION:
            messages.error(request, COMPLETION_HANDOFF_MESSAGE)
            return redirect("application-detail", pk=pk)

        # C4 + F9 (office decision): cases move between the Secretariat and the HRM Chief as a
        # whole batch, so one case never splits a batch exam or interview between offices.
        messages.error(
            request,
            "Cases are sent as a whole batch now. Use “Send the batch” on the vacancy's page.",
        )
        return redirect("application-detail", pk=pk)


class ResubmissionDeadlineExtensionView(
    LoginRequiredMixin, WorkflowProcessorRequiredMixin, View
):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_extend_resubmission_deadline(request.user, application):
            raise PermissionDenied
        screening_record = get_screening_record(
            application, stage=application.case.current_stage
        )
        form = ResubmissionDeadlineExtensionForm(
            request.POST,
            current_deadline=screening_record.resubmission_deadline,
        )
        if not form.is_valid():
            messages.error(
                request,
                "; ".join(
                    str(error)
                    for errors in form.errors.values()
                    for error in errors
                ),
            )
            return redirect("application-detail", pk=pk)
        try:
            extend_document_resubmission_deadline(
                application,
                request.user,
                form.cleaned_data["new_deadline"],
                form.cleaned_data["reason"],
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                "Resubmission deadline extended and one reminder email queued for the applicant.",
            )
        return redirect("application-detail", pk=pk)


class ApplicationWithdrawalView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-10: record that the applicant has withdrawn from the competition."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_record_withdrawal(request.user, application):
            raise PermissionDenied
        form = ApplicationWithdrawalForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(
                request, "Record the reason and reference for this withdrawal."
            )
            return redirect("application-detail", pk=pk)
        try:
            record_application_withdrawal(
                application=application,
                actor=request.user,
                reason=form.cleaned_data["reason"],
                uploaded_file=form.cleaned_data.get("file"),
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                "Withdrawal recorded. The case is closed as withdrawn — not as a "
                "rejection — and the applicant has been sent a confirmation.",
            )
        # The case just left the active queue, so check retained (closed-case) access
        # before sending the recorder back to it — same pattern as WorkflowActionView.
        # Without this, an actor who loses view access on withdrawal (e.g. a Secretariat
        # acting on a Level-2 override) would land their success message on a 404.
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class ApplicantContactUpdateView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_update_applicant_contact(request.user, application):
            raise PermissionDenied
        form = ApplicantContactUpdateForm(request.POST, application=application)
        if not form.is_valid():
            messages.error(request, "Correct the highlighted contact details.")
            return _render_application_detail_with_overrides(
                request,
                application,
                applicant_contact_form=form,
            )
        try:
            update_applicant_contact(
                application,
                request.user,
                form.cleaned_data["new_email"],
                form.cleaned_data["new_phone"],
                form.cleaned_data["reason"],
            )
        except (ValueError, ValidationError) as exc:
            form.add_error(None, str(exc))
            messages.error(request, str(exc))
            return _render_application_detail_with_overrides(
                request,
                application,
                applicant_contact_form=form,
            )
        messages.success(
            request,
            "Applicant contact updated. Notices were queued for the affected email addresses.",
        )
        return redirect("application-detail", pk=pk)


class WorkflowOverrideView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise Http404
        if not user_can_process_application(request.user, application):
            raise PermissionDenied
        form = WorkflowOverrideForm(request.POST)
        if form.is_valid():
            try:
                grant_secretariat_override(
                    application=application,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Level 2 authorization recorded.")
        else:
            messages.error(request, "Add a reason for this authorization.")
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class WorkflowReopenView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        form = WorkflowReopenForm(request.POST)
        if form.is_valid():
            try:
                reopen_recruitment_case(
                    application=application,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Case reopened with authorization.")
        else:
            messages.error(request, "Add a reason before reopening this case.")
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")
