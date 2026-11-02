"""The screening review form of a case.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..forms import ScreeningReviewForm
from ..models import RecruitmentApplication
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    save_screening_review,
    return_screening_documents_to_applicant,
    user_can_manage_screening,
)
from .common import _autosave_response, _is_autosave_request


class ScreeningReviewView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_screening(request.user, application):
            raise PermissionDenied

        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        is_finalize = operation == "finalize"
        # RD-1: returning the flagged documents is a decision on this screen, not
        # something bolted on after a finalize. It never finalizes the record and
        # never asks for a verdict on evidence the reviewer just flagged.
        is_return = operation == "return_to_applicant"
        form = ScreeningReviewForm(
            request.POST, application=application, draft=not is_finalize
        )
        if form.is_valid():
            try:
                if is_return:
                    screening_record = return_screening_documents_to_applicant(
                        application=application,
                        actor=request.user,
                        cleaned_data=form.cleaned_data,
                        remarks=request.POST.get("return_remarks", ""),
                    )
                else:
                    screening_record = save_screening_review(
                        application=application,
                        actor=request.user,
                        cleaned_data=form.cleaned_data,
                        finalize=is_finalize,
                        allow_partial=not is_finalize,
                        record_audit=not is_autosave,
                    )
            except (ValueError, ValidationError) as exc:
                if is_autosave:
                    return _autosave_response(False)
                messages.error(request, str(exc))
            else:
                if is_autosave:
                    return _autosave_response()
                if is_return:
                    messages.success(
                        request,
                        "Flagged documents returned to the applicant. The case stays in "
                        "your queue as awaiting resubmission.",
                    )
                elif screening_record.is_finalized:
                    messages.success(request, "Screening finalized and locked.")
                else:
                    messages.success(request, "Screening draft saved.")
        else:
            if is_autosave:
                return _autosave_response(False)
            error_messages = []
            for field_name, errors in form.errors.items():
                label = form.fields.get(field_name).label if field_name in form.fields else ""
                for error in errors:
                    if label:
                        error_messages.append(f"{label}: {error}")
                    else:
                        error_messages.append(str(error))
            messages.error(
                request,
                "; ".join(error_messages) or "Complete all screening fields before saving.",
            )
        return redirect("application-detail", pk=pk)
