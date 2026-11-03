"""Deliberation and the Comparative Assessment Report: the form, print view and filing copy.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from ..forms import ComparativeAssessmentReportForm, DeliberationRecordForm
from ..models import AuditLog, PositionPosting, RecruitmentApplication
from ..permissions import WorkflowProcessorRequiredMixin
from ..services import (
    record_system_audit_event,
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    apply_car_ete_ratings,
    autosave_comparative_assessment_report_notes,
    generate_comparative_assessment_report,
    get_comparative_assessment_report,
    get_comparative_assessment_report_items_for_report,
    get_entry_finalized_car,
    user_can_generate_entry_car_filing_copy,
    get_applicant_pool_finalization_block_message,
    get_latest_finalized_comparative_assessment_report,
    save_deliberation_record,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
    user_can_view_application,
)
from .common import _autosave_response, _is_autosave_request


class ComparativeAssessmentPrintView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """REV-E: print-tailored Comparative Assessment Report. Blinded per REV-B —
    rows carry candidate codes only, never names or case numbers."""

    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        report = get_comparative_assessment_report(application)
        if report is None:
            report = get_latest_finalized_comparative_assessment_report(application)
        if report is None:
            messages.error(request, "No comparative assessment report exists for this case yet.")
            return redirect("application-detail", pk=application.pk)
        items = list(get_comparative_assessment_report_items_for_report(report))
        for item in items:
            item.candidate_label = item.application.candidate_code_label
        return render(
            request,
            "recruitment/car_print.html",
            {
                "application": application,
                "entry": application.position,
                "report": report,
                "items": items,
                "weights": report.assessment_weights_for_display,
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class ComparativeAssessmentFilingCopyView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """N1: the office's official filing copy of the finalized CAR.

    The signed hard copy is still required, and the Secretariat has been re-typing this
    exact sheet by hand. Generated here in the office's own one-page layout — QS block on
    its face, candidates by name, per-component columns, Remarks, wet-ink signature blocks,
    Date of Interview / Date of Deliberation.

    Deliberately named. The on-screen CAR and the deliberation print stay blinded (REV-B)
    because that is what the board deliberates against; this is the copy that gets signed
    and filed, so it must identify people. Secretariat-only, and the generation is recorded
    as an export in the case audit trail."""

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        if not user_can_generate_entry_car_filing_copy(request.user, entry):
            raise PermissionDenied
        report = get_entry_finalized_car(entry)
        if report is None:
            messages.error(
                request,
                "The filing copy is generated from the finalized CAR — finalize it first.",
            )
            return redirect("recruitment-entry-list")
        weights = report.assessment_weights_for_display
        rows = []
        for item in get_comparative_assessment_report_items_for_report(report):
            candidate = item.application
            eps = None
            if item.ete_rating is not None:
                eps = (item.ete_rating * weights.ete_fraction).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            rows.append(
                {
                    "item": item,
                    # The office writes candidates as "Surname, First Name MI".
                    "filing_name": (
                        f"{candidate.applicant_last_name}, "
                        f"{candidate.applicant_first_name}".strip(", ")
                    ),
                    "eps": eps,
                    "is_recommended": item.recruitment_case_id == report.recommended_case_id,
                }
            )
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=(
                f"Generated the official CAR filing copy (named) for {entry.job_code}."
            ),
            metadata={
                "report_id": report.id,
                "report_version": report.version_number,
                "entry_id": entry.id,
                "entry_code": entry.job_code,
                "candidate_count": len(rows),
                "blinded": False,
            },
        )
        return render(
            request,
            "recruitment/car_filing_copy.html",
            {
                "entry": entry,
                "reference": entry.position_reference,
                "report": report,
                "items": [row["item"] for row in rows],
                "filing_rows": rows,
                "weights": weights,
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class DeliberationRecordView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        if (
            application_requires_finalized_applicant_pool(application)
            and not application_has_finalized_applicant_pool(application)
        ):
            if is_autosave:
                return _autosave_response(False)
            messages.error(request, get_applicant_pool_finalization_block_message(application))
            return redirect("application-detail", pk=pk)
        if not user_can_manage_deliberation(request.user, application):
            raise PermissionDenied

        is_finalize = operation == "finalize"
        form = DeliberationRecordForm(
            request.POST,
            application=application,
            draft=not is_finalize,
        )
        if form.is_valid():
            try:
                deliberation_record = save_deliberation_record(
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
                if deliberation_record.is_finalized:
                    if application.branch == PositionPosting.Branch.PLANTILLA:
                        messages.success(request, "HRMPSB recommendation endorsed and locked.")
                    else:
                        messages.success(request, "Deliberation record finalized and locked.")
                else:
                    messages.success(request, "Deliberation record saved.")
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(request, "Complete the required deliberation fields before saving.")
        return redirect("application-detail", pk=pk)


class ComparativeAssessmentReportView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        # Authorize before touching any application-specific state so an
        # unauthorized actor cannot distinguish restricted cases by the
        # response they get (403 vs. a state-dependent redirect).
        if not user_can_manage_comparative_assessment_report(request.user, application):
            raise PermissionDenied
        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        if (
            application_requires_finalized_applicant_pool(application)
            and not application_has_finalized_applicant_pool(application)
        ):
            if is_autosave:
                return _autosave_response(False)
            messages.error(request, get_applicant_pool_finalization_block_message(application))
            return redirect("application-detail", pk=pk)

        form = ComparativeAssessmentReportForm(request.POST)
        if form.is_valid():
            try:
                if is_autosave:
                    autosave_comparative_assessment_report_notes(
                        application=application,
                        actor=request.user,
                        cleaned_data=form.cleaned_data,
                    )
                else:
                    # Persist any per-candidate ETE ratings typed on the draft, then
                    # regenerate so the ranking reflects them.
                    apply_car_ete_ratings(application, request.user, request.POST)
                    if operation == "finalize":
                        # The board's recommended applicant is chosen in the finalize modal.
                        form.cleaned_data["recommended_case_id"] = request.POST.get("recommended_case")
                    report = generate_comparative_assessment_report(
                        application=application,
                        actor=request.user,
                        cleaned_data=form.cleaned_data,
                        finalize=operation == "finalize",
                    )
            except (ValueError, ValidationError) as exc:
                if is_autosave:
                    return _autosave_response(False)
                messages.error(request, str(exc))
            else:
                if is_autosave:
                    return _autosave_response()
                if report.is_finalized:
                    messages.success(
                        request,
                        "Comparative Assessment Report finalized. The candidates' cases are now "
                        "with the Appointing Authority for the decision. The CAR filing copy "
                        "is on the Vacancies page.",
                    )
                else:
                    messages.success(
                        request,
                        "Draft CAR ready with the current scores. Check the ranking, then "
                        "finalize it after the board's deliberation.",
                    )
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(request, "Provide the CAR notes before generating the report.")
        application.refresh_from_db()
        if not user_can_view_application(request.user, application):
            # Finalizing hands the pool to the Appointing Authority; the vacancy's batch
            # list is the preparer's next place to look, as after the other batch steps.
            return redirect("vacancy-batches")
        return redirect("application-detail", pk=pk)
