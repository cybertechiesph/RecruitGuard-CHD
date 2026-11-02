"""Completion tracking and closing a case.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..forms import (
    CaseClosureForm,
    CompletionRequirementFormSet,
    CompletionTrackingForm,
)
from ..models import CompletionRecord, RecruitmentApplication
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    close_recruitment_case,
    get_completion_record,
    save_completion_tracking,
    user_can_close_case,
    user_can_manage_completion,
)
from .case import _render_application_detail_with_overrides


class CompletionTrackingView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def _render_with_bound_forms(self, request, application, form, formset, message):
        messages.error(request, message)
        return _render_application_detail_with_overrides(
            request,
            application,
            completion_form=form,
            completion_requirement_formset=formset,
        )

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_completion(request.user, application):
            raise PermissionDenied

        completion_record = get_completion_record(application)
        requirement_instance = completion_record or CompletionRecord(
            application=application,
            recruitment_case=application.case,
            tracked_by=request.user,
        )
        form = CompletionTrackingForm(
            request.POST,
            instance=completion_record,
            application=application,
            actor=request.user,
        )
        formset = CompletionRequirementFormSet(
            request.POST,
            instance=requirement_instance,
            prefix="completion_requirements",
        )
        if form.is_valid() and formset.is_valid():
            try:
                save_completion_tracking(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    requirement_formset=formset,
                )
            except (ValueError, ValidationError) as exc:
                return self._render_with_bound_forms(
                    request,
                    application,
                    form,
                    formset,
                    str(exc),
                )
            else:
                messages.success(request, "Completion details saved.")
        else:
            errors = []
            errors.extend(error for error_list in form.errors.values() for error in error_list)
            errors.extend(error for error in formset.non_form_errors())
            for requirement_form in formset.forms:
                errors.extend(
                    error
                    for error_list in requirement_form.errors.values()
                    for error in error_list
                )
            return self._render_with_bound_forms(
                request,
                application,
                form,
                formset,
                "; ".join(errors) or "Complete the completion tracking fields before saving.",
            )
        return redirect("application-detail", pk=pk)


class CaseClosureView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def _render_with_bound_form(self, request, application, form, message):
        can_close = user_can_close_case(request.user, application)
        if can_close and not form.errors:
            form.add_error("closure_notes", message)
        messages.error(request, message)
        overrides = {}
        if can_close:
            overrides["closure_form"] = form
        return _render_application_detail_with_overrides(
            request,
            application,
            **overrides,
        )

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_completion(request.user, application):
            raise PermissionDenied

        form = CaseClosureForm(request.POST)
        if form.is_valid():
            try:
                close_recruitment_case(
                    application=application,
                    actor=request.user,
                    closure_notes=form.cleaned_data["closure_notes"],
                )
            except ValueError as exc:
                return self._render_with_bound_form(
                    request,
                    application,
                    form,
                    str(exc),
                )
            else:
                messages.success(request, "Case closed after completion.")
        else:
            return self._render_with_bound_form(
                request,
                application,
                form,
                "Closure notes are required.",
            )
        return redirect("application-detail", pk=pk)
