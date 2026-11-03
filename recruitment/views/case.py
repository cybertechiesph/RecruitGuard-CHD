"""The case page (ApplicationDetailView) and the case-level actions: workflow action,
handoff, override, reopen, withdrawal, contact update and resubmission deadline.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView

from ..forms import (
    ApplicationWithdrawalForm,
    ApplicantContactUpdateForm,
    ResubmissionDeadlineExtensionForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from ..models import RecruitmentApplication, RecruitmentCase
from ..permissions import (
    InternalUserRequiredMixin,
    WorkflowProcessorRequiredMixin,
)
from ..services import (
    COMPLETION_HANDOFF_MESSAGE,
    record_application_withdrawal,
    extend_document_resubmission_deadline,
    update_applicant_contact,
    user_can_extend_resubmission_deadline,
    user_can_record_withdrawal,
    user_can_update_applicant_contact,
    get_screening_record,
    grant_secretariat_override,
    process_workflow_action,
    record_protected_record_access,
    reopen_recruitment_case,
    user_can_process_application,
    user_can_view_application,
)
from . import (
    case_car,
    case_completion,
    case_decision,
    case_exam,
    case_interview,
    case_screening,
    case_shared,
)
from .common import _format_form_errors


# The case page shows one workflow step at a time; each step's forms and flags come from
# its own module.
SECTION_BUILDERS = {
    "screening": case_screening.build,
    "exam": case_exam.build,
    "interview": case_interview.build,
    "deliberation": case_car.build,
    "car": case_car.build,
    "decision": case_decision.build,
    "completion": case_completion.build,
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
