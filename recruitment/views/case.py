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
from django.views import View
from django.views.generic import DetailView

from ..forms import (
    CaseClosureForm,
    ComparativeAssessmentReportForm,
    CompletionRequirementFormSet,
    CompletionTrackingForm,
    DeliberationRecordForm,
    EvidenceUploadForm,
    FinalSelectionForm,
    NoAppointmentForm,
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    InterviewSessionForm,
    SimpleInterviewRatingForm,
    ApplicationWithdrawalForm,
    ApplicantContactUpdateForm,
    InterviewAbsenceForm,
    ResubmissionDeadlineExtensionForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from ..models import (
    CompletionRecord,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..permissions import (
    InternalUserRequiredMixin,
    WorkflowProcessorRequiredMixin,
)
from ..services import (
    COMPLETION_HANDOFF_MESSAGE,
    FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES,
    application_awaiting_car_preparation,
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    plantilla_car_preparer_role_label,
    get_comparative_assessment_readiness,
    get_current_workflow_section,
    get_car_candidate_fact_rows,
    interview_fallback_panel_queryset,
    record_application_withdrawal,
    extend_document_resubmission_deadline,
    update_applicant_contact,
    user_can_extend_resubmission_deadline,
    user_can_record_withdrawal,
    user_can_update_applicant_contact,
    final_selection_recommended_case_ids,
    user_can_follow_level_two_case,
    get_cos_round_pick_for_case,
    get_applicant_pool_finalization_block_message,
    get_screening_record,
    grant_secretariat_override,
    process_workflow_action,
    record_protected_record_access,
    reopen_recruitment_case,
    vacancy_uses_batch_exam,
    user_can_close_case,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
    user_can_manage_completion,
    user_can_manage_interview_rating,
    interview_scoring_is_open,
    scoring_opens_message,
    get_published_competency_rating_template,
    user_can_manage_interview_session,
    user_can_process_application,
    user_can_record_final_selection,
    user_can_record_no_appointment,
    user_can_upload_interview_fallback,
    user_can_upload_evidence,
    user_can_view_application,
    user_is_interview_rating_support_encoder,
)
from . import case_exam, case_screening, case_shared
from .common import _format_form_errors


# The case page shows one workflow step at a time; each step's forms and flags come from
# its own module.
SECTION_BUILDERS = {
    "screening": case_screening.build,
    "exam": case_exam.build,
}


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
        context.update(case_shared.build(application, user))
        # Round H: the step on screen adds its own keys; the other steps' keys are never
        # computed (the page shows one step at a time).
        builder = SECTION_BUILDERS.get(context["current_detail_tab"]["key"])
        if builder:
            context.update(builder(context, self.request, application))
        # Round H1: interview, CAR/deliberation, decision and completion are still built here
        # for every step; rounds H2 and H3 move them into their own builders.
        context["interview_rating_is_support_encoding"] = user_is_interview_rating_support_encoder(
            user,
            application,
        )
        context["car_readiness"] = get_comparative_assessment_readiness(application)
        # The ETE grid lists the draft's rows; before the first draft it lists the pool,
        # so the scores can be typed first and one "Prepare draft CAR" saves and ranks.
        car_ete_rows = context["current_comparative_assessment_report_items"] or [
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
        if user_can_upload_evidence(user, application):
            context["evidence_form"] = EvidenceUploadForm()
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
