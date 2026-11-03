"""The final selection, no appointment, and rescinding a selection or a COS pick.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..forms import (
    FinalSelectionForm,
    NoAppointmentForm,
    CosVacancyDeliberationRescindForm,
    FinalSelectionRescindForm,
)
from ..models import RecruitmentApplication
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    get_cos_pick_for_case,
    rescind_cos_vacancy_deliberation,
    rescind_final_selection,
    user_can_rescind_cos_vacancy_deliberation,
    user_can_rescind_final_selection,
    get_final_selection_for_application,
    get_latest_finalized_comparative_assessment_report,
    record_final_selection,
    record_no_appointment,
    user_can_record_final_selection,
    user_can_record_no_appointment,
    user_can_view_application,
)
from .common import _add_validation_errors_to_form, _format_form_errors, logger
from .case import _render_application_detail_with_overrides


class FinalSelectionRescindView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-14: void a recorded final selection so the vacancy can be decided again."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        selection = get_final_selection_for_application(application)
        if not user_can_rescind_final_selection(request.user, selection):
            raise PermissionDenied
        form = FinalSelectionRescindForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record why this selection is being taken back.")
            return redirect("application-detail", pk=pk)
        try:
            _selection, restored = rescind_final_selection(
                selection,
                request.user,
                form.cleaned_data["reason"],
                reference=form.cleaned_data.get("reference", ""),
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                f"Selection taken back. The original decision stays on file, and "
                f"{len(restored)} case(s) are back at the Appointing Authority step so a "
                "replacement decision can be recorded.",
            )
        return redirect("application-detail", pk=pk)


class CosVacancyDeliberationRescindView(
    LoginRequiredMixin, WorkflowProcessorRequiredMixin, View
):
    """SL-14 (COS): void a recorded COS deliberation pick so the round can be decided again."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        pick = get_cos_pick_for_case(getattr(application, "case", None))
        if not user_can_rescind_cos_vacancy_deliberation(request.user, pick):
            raise PermissionDenied
        form = CosVacancyDeliberationRescindForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record why this COS deliberation pick is being taken back.")
            return redirect("application-detail", pk=pk)
        try:
            _pick, restored = rescind_cos_vacancy_deliberation(
                pick,
                request.user,
                form.cleaned_data["reason"],
                reference=form.cleaned_data.get("reference", ""),
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        except IntegrityError:
            logger.exception(
                "The COS pick could not be taken back because another selection round is open."
            )
            messages.error(
                request,
                "This pick cannot be taken back right now because another selection round is open.",
            )
        else:
            messages.success(
                request,
                f"COS deliberation pick taken back. The original pick stays on file, and "
                f"{len(restored)} case(s) are back at the HRM Chief's deliberation step so "
                "a replacement pick can be recorded.",
            )
        # The case may have left this user's view on the way in or out, so check retained
        # access before sending them back to it (the WorkflowActionView pattern).
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


class FinalSelectionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def _render_with_bound_form(self, request, application, form, message):
        if not any(form.errors.get(field_name) for field_name in form.fields):
            form.add_error("selected_item", message)
        messages.error(request, message)
        return _render_application_detail_with_overrides(
            request,
            application,
            final_selection_form=form,
        )

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_record_final_selection(request.user, application):
            raise PermissionDenied

        report = get_latest_finalized_comparative_assessment_report(application)
        form = FinalSelectionForm(request.POST, report=report)
        if form.is_valid():
            try:
                selection = record_final_selection(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                _add_validation_errors_to_form(form, exc)
                return self._render_with_bound_form(
                    request,
                    application,
                    form,
                    str(exc),
                )
            else:
                messages.success(
                    request,
                    "Final selection recorded from the CAR. Applicant cases were updated.",
                )
                return redirect("application-detail", pk=selection.selected_application_id)
        else:
            return self._render_with_bound_form(
                request,
                application,
                form,
                _format_form_errors(
                    form,
                    "Choose the appointee and write the decision notes.",
                ),
            )


class NoAppointmentView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_record_no_appointment(request.user, application):
            raise PermissionDenied

        form = NoAppointmentForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record why no appointment will be made.")
            return _render_application_detail_with_overrides(
                request,
                application,
                no_appointment_form=form,
            )
        try:
            report = record_no_appointment(
                application=application,
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except (ValueError, ValidationError) as exc:
            form.add_error("reason", str(exc))
            messages.error(request, str(exc))
            return _render_application_detail_with_overrides(
                request,
                application,
                no_appointment_form=form,
            )
        messages.success(
            request,
            "No appointment recorded. The remaining candidates were closed as not selected.",
        )
        first_item = report.items.order_by("rank_order", "id").first()
        return redirect(
            "application-detail",
            pk=first_item.application.pk if first_item else application.pk,
        )
