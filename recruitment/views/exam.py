"""The exam record and schedule forms of a case, and attendance exceptions.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..forms import (
    ExamRecordForm,
    ExamScheduleForm,
    AttendanceAbsenceDecisionForm,
    AttendanceLetterOfIntentForm,
)
from ..models import AttendanceException, RecruitmentApplication
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    record_attendance_letter_of_intent,
    resolve_attendance_absence,
    user_can_manage_attendance_exception,
    get_exam_schedule,
    save_exam_record,
    save_exam_schedule,
    vacancy_uses_batch_exam,
    user_can_manage_exam,
    user_can_view_application,
)
from .common import (
    EXAM_FIELD_LABELS,
    _add_validation_errors_to_form,
    _autosave_response,
    _format_form_errors,
    _format_validation_error,
    _is_autosave_request,
)
from .case import _render_application_detail_with_overrides


class ExaminationRecordView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_exam(request.user, application):
            raise PermissionDenied
        # RD-2: one door. A pooled vacancy is scored only in the batch exam
        # console, so this per-case endpoint refuses writes server-side — a
        # forced POST cannot create a second, conflicting score.
        if vacancy_uses_batch_exam(application.position):
            messages.error(
                request,
                "Exam scores for this vacancy are recorded in the batch exam console, "
                "where the whole qualified pool is scored together.",
            )
            return redirect("vacancy-batch-exam", pk=application.position.pk)

        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        is_finalize = operation == "finalize"
        form = ExamRecordForm(
            request.POST,
            request.FILES,
            application=application,
            draft=not is_finalize,
        )
        if form.is_valid():
            try:
                exam_record = save_exam_record(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=is_finalize,
                    evidence_file=None if is_autosave else form.cleaned_data.get("evidence_file"),
                    allow_partial=not is_finalize,
                    record_audit=not is_autosave,
                )
            except (ValueError, ValidationError) as exc:
                if is_autosave:
                    return _autosave_response(False)
                _add_validation_errors_to_form(form, exc)
                messages.error(
                    request,
                    _format_validation_error(
                        exc,
                        "Review the examination fields before saving or finalizing.",
                        EXAM_FIELD_LABELS,
                    ),
                )
                return _render_application_detail_with_overrides(
                    request,
                    application,
                    exam_form=form,
                    exam_locked=False,
                )
            else:
                if is_autosave:
                    return _autosave_response()
                if exam_record.is_finalized:
                    messages.success(request, "Exam finalized and locked.")
                else:
                    messages.success(request, "Exam draft saved.")
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(
                request,
                _format_form_errors(
                    form,
                    "Review the examination fields before saving or finalizing.",
                ),
            )
            return _render_application_detail_with_overrides(
                request,
                application,
                exam_form=form,
                exam_locked=False,
            )
        application.refresh_from_db()
        if not user_can_view_application(request.user, application):
            return redirect("workflow-queue")
        return redirect("application-detail", pk=pk)


class ExamScheduleView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_exam(request.user, application):
            raise PermissionDenied
        # RD-2: the whole pool is scheduled together in the batch console.
        if vacancy_uses_batch_exam(application.position):
            messages.error(
                request,
                "This vacancy's exam is scheduled for the whole pool in the batch exam console.",
            )
            return redirect("vacancy-batch-exam", pk=application.position.pk)

        form = ExamScheduleForm(
            request.POST,
            instance=get_exam_schedule(application),
        )
        if form.is_valid():
            try:
                save_exam_schedule(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Exam schedule saved and the applicant was notified.",
                )
        else:
            messages.error(
                request,
                _format_form_errors(
                    form,
                    "Complete the required exam scheduling fields before saving.",
                ),
            )
        return redirect("application-detail", pk=pk)


class AttendanceExceptionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-06: file the letter of intent against a recorded absence, or decide it."""

    def post(self, request, pk, exception_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        exception = get_object_or_404(
            AttendanceException, pk=exception_pk, application=application
        )
        if not user_can_manage_attendance_exception(request.user, exception):
            raise PermissionDenied
        operation = request.POST.get("operation", "")
        if operation == "letter":
            form = AttendanceLetterOfIntentForm(request.POST, request.FILES)
            if not form.is_valid():
                messages.error(request, "Attach the applicant's letter of intent.")
                return redirect("application-detail", pk=pk)
            service = record_attendance_letter_of_intent
            kwargs = {
                "uploaded_file": form.cleaned_data["file"],
                "notes": form.cleaned_data.get("notes", ""),
            }
            success = "Letter of intent filed against the recorded absence."
        elif operation == "decide":
            form = AttendanceAbsenceDecisionForm(request.POST)
            if not form.is_valid():
                messages.error(
                    request, "Choose an outcome and record why it was decided that way."
                )
                return redirect("application-detail", pk=pk)
            service = resolve_attendance_absence
            kwargs = {
                "outcome": form.cleaned_data["outcome"],
                "reason": form.cleaned_data["reason"],
            }
            success = (
                "Candidate rescheduled — the missed sitting is open again."
                if form.cleaned_data["outcome"] == AttendanceException.Outcome.RESCHEDULED
                else "Recorded that the application does not proceed. The applicant was notified."
            )
        else:
            messages.error(request, "This action is not available.")
            return redirect("application-detail", pk=pk)
        try:
            service(exception, request.user, **kwargs)
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, success)
        return redirect("application-detail", pk=pk)
