from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
import csv
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO

from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from .forms import (
    AuditLogSearchForm,
    CaseHandoffForm,
    CaseClosureForm,
    ComparativeAssessmentReportForm,
    CompletionRequirementFormSet,
    CompletionTrackingForm,
    DeliberationRecordForm,
    EvidenceArchiveForm,
    EvidenceVaultSearchForm,
    ExamRecordForm,
    ExamScheduleForm,
    EvidenceUploadForm,
    FinalSelectionForm,
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    CompetencyDefinitionFormSet,
    CompetencyRatingTemplateForm,
    InterviewSessionForm,
    ReminderNotificationForm,
    SimpleInterviewRatingForm,
    RequirementChecklistNotificationForm,
    ScreeningReviewForm,
    ApplicationWithdrawalForm,
    CosVacancyDeliberationRescindForm,
    FinalSelectionRescindForm,
    AttendanceAbsenceDecisionForm,
    AttendanceLetterOfIntentForm,
    InterviewAbsenceForm,
    VacancyAssessmentWeightsForm,
    VacancyAssessmentWeightsUnlockForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from .models import (
    AUDIT_ACTION_CATEGORIES,
    STAGE_SLA_OVERDUE_THRESHOLD,
    AUDIT_CATEGORY_BY_ACTION,
    AttendanceException,
    AuditLog,
    AuditLogArchive,
    CompetencyDefinition,
    CompletionRecord,
    CompletionRequirement,
    EvidenceVaultItem,
    ExamRecord,
    Notification,
    NotificationLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
    StageRecordVersion,
)
from .notification_services import (
    NOTIFICATION_MANAGER_ROLES,
    get_recent_notifications,
    get_unread_count,
    mark_all_notifications_read,
    mark_notification_read,
    resend_notification,
    send_reminder_notification,
    send_requirement_checklist_notification,
    user_can_send_reminder_notification,
    user_can_send_requirement_checklist_notification,
)
from .permissions import (
    EntryManagerRequiredMixin,
    InternalUserRequiredMixin,
    SystemAdministratorRequiredMixin,
    WorkflowProcessorRequiredMixin,
    WORKFLOW_PROCESSOR_ROLES,
)
from .services import (
    build_audit_session_summary,
    record_system_audit_event,
    summarize_audit_logs,
    archive_audit_logs,
    audit_archive_latest_allowed_cutoff,
    download_audit_archive,
    preview_audit_archive,
    application_awaiting_car_preparation,
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    apply_car_ete_ratings,
    plantilla_car_preparer_role_label,
    autosave_comparative_assessment_report_notes,
    build_submission_packet,
    build_export_bundle,
    close_recruitment_case,
    generate_comparative_assessment_report,
    get_application_detail_tab,
    get_applicant_document_review_items,
    get_application_audit_logs,
    get_comparative_assessment_report,
    get_comparative_assessment_report_items_for_report,
    get_comparative_assessment_readiness,
    get_current_workflow_section,
    get_completion_record,
    get_completion_requirements,
    decrypt_evidence_bytes,
    evidence_belongs_to_application_context,
    ETE_COMPONENT_WEIGHTS_BY_BRANCH,
    record_system_audit_event,
    get_entry_finalized_car,
    user_can_generate_entry_car_filing_copy,
    vacancy_batch_pool_block_message,
    vacancy_batch_pool_is_final,
    get_attendance_exceptions,
    get_car_candidate_fact_rows,
    get_cos_pick_for_case,
    get_rescinded_cos_picks_for_case,
    get_cos_vacancy_deliberation,
    get_interview_fallback_ratings,
    get_stage_record_correction,
    get_stage_record_versions,
    get_pending_attendance_exception,
    interview_fallback_panel_queryset,
    mark_interview_absent,
    record_application_withdrawal,
    record_attendance_letter_of_intent,
    rescind_cos_vacancy_deliberation,
    rescind_final_selection,
    resolve_attendance_absence,
    user_can_record_withdrawal,
    user_can_rescind_cos_vacancy_deliberation,
    user_can_rescind_final_selection,
    get_final_selection_history,
    get_latest_cos_selection_round,
    get_open_cos_selection_round,
    open_cos_selection_round,
    user_can_open_cos_selection_round,
    vacancy_uses_cos_selection_rounds,
    cos_eligible_pool,
    cos_deliberation_pool,
    save_cos_vacancy_deliberation,
    user_can_manage_cos_deliberation,
    get_deliberation_record,
    get_deliberation_records,
    get_evidence_owner_application,
    get_evidence_queryset_for_user,
    get_exam_record,
    get_exam_schedule,
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
    get_latest_finalized_comparative_assessment_report,
    get_available_actions,
    get_case_handoff_options,
    get_case_timeline,
    get_screening_record,
    get_screening_records,
    get_all_audit_logs,
    get_closed_cases_for_user,
    get_queue_for_user,
    get_visible_positions_for_user,
    grant_secretariat_override,
    process_workflow_action,
    record_audit_log_review,
    record_evidence_access_denied,
    record_export_denied,
    record_final_decision,
    record_final_selection,
    record_evidence_vault_access,
    record_protected_record_access,
    reopen_recruitment_case,
    route_case_between_secretariat_and_hrm_chief,
    save_completion_tracking,
    save_deliberation_record,
    save_exam_record,
    save_exam_schedule,
    save_interview_rating,
    save_interview_session,
    save_screening_review,
    return_screening_documents_to_applicant,
    vacancy_uses_batch_exam,
    screening_requires_disposition_for_current_stage,
    user_can_close_case,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
    user_can_manage_evidence_archive,
    user_can_manage_completion,
    upload_evidence_item,
    upload_interview_fallback_rating,
    update_evidence_archive_status,
    user_can_export_application,
    user_can_manage_exam,
    user_can_manage_interview_rating,
    create_competency_rating_template,
    get_competency_rating_template,
    get_published_competency_rating_template,
    save_competency_rating_sheet,
    get_or_create_vacancy_assessment_weights,
    unlock_vacancy_assessment_weights,
    update_vacancy_assessment_weights,
    user_can_manage_interview_session,
    user_can_manage_screening,
    user_can_process_application,
    vacancy_exam_pool,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    vacancy_interview_pool,
    save_vacancy_interview_schedule,
    save_vacancy_cos_interview_ratings,
    finalize_vacancy_interview_batch,
    vacancy_screening_batch_status,
    vacancy_screening_batch_block_message,
    user_can_record_final_decision,
    user_can_record_final_selection,
    user_can_reopen_case,
    user_can_upload_interview_fallback,
    user_can_upload_evidence,
    user_can_view_application,
    user_is_interview_rating_support_encoder,
)
from .templatetags.recruitment_ui import QUEUE_TASK_LABELS
from django.db.models import Count, Q


EXAM_FIELD_LABELS = {
    "exam_type": "Exam Type",
    "exam_status": "Exam Status",
    "exam_score": "Overall / Single Exam Score",
    "technical_score": "Technical Score",
    "general_score": "General Ability Score",
    "exam_date": "Exam Date",
    "administered_by": "Administered By",
    "valid_from": "Validity Start",
    "valid_until": "Validity End",
    "exam_notes": "Exam Notes / Remarks",
}


def _join_labels(labels):
    labels = [label for label in labels if label]
    if len(labels) <= 1:
        return labels[0] if labels else "Field"
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _format_grouped_validation_messages(error_map, field_labels=None):
    grouped = {}
    field_labels = field_labels or {}
    for field_name, errors in error_map.items():
        label = field_labels.get(field_name, field_name.replace("_", " ").title())
        for error in errors:
            error_messages = getattr(error, "messages", None) or [str(error)]
            for message in error_messages:
                grouped.setdefault(message, []).append(label)
    return "; ".join(
        f"{_join_labels(labels)}: {message}"
        for message, labels in grouped.items()
    )


def _format_form_errors(form, fallback):
    message = _format_grouped_validation_messages(
        form.errors.as_data(),
        {field_name: field.label for field_name, field in form.fields.items()},
    )
    return message or fallback


def _format_validation_error(exc, fallback, field_labels=None):
    message_dict = getattr(exc, "message_dict", None)
    if message_dict:
        return _format_grouped_validation_messages(message_dict, field_labels) or fallback
    error_messages = getattr(exc, "messages", None)
    if error_messages:
        return " ".join(str(message) for message in error_messages)
    return str(exc) or fallback


def _safe_next_url(request, fallback_url):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback_url


def _is_autosave_request(request):
    return request.headers.get("X-Requested-With") == "RG-Autosave"


# REV-D: "audit log viewed" records one entry per review visit, not one per
# filter change or page load. A visit = the same session touching the same
# scope within this window.
AUDIT_REVIEW_LOG_WINDOW_SECONDS = 30 * 60


def _should_record_audit_review(request, scope_key):
    from datetime import datetime as _datetime

    stamps = request.session.get("audit_review_logged") or {}
    raw = stamps.get(scope_key)
    if raw:
        try:
            last = _datetime.fromisoformat(raw)
        except ValueError:
            last = None
        if last is not None and (timezone.now() - last).total_seconds() < AUDIT_REVIEW_LOG_WINDOW_SECONDS:
            return False
    stamps[scope_key] = timezone.now().isoformat()
    request.session["audit_review_logged"] = stamps
    return True


def _autosave_response(success=True):
    return HttpResponse(status=204 if success else 400)


def _add_validation_errors_to_form(form, exc):
    if isinstance(exc, ValidationError):
        error_dict = getattr(exc, "error_dict", None)
        if error_dict:
            for field_name, errors in error_dict.items():
                target = field_name if field_name in form.fields else None
                for error in errors:
                    form.add_error(target, error)
            return
        for message in getattr(exc, "messages", None) or [str(exc)]:
            form.add_error(None, message)
        return
    form.add_error(None, str(exc))


def _render_application_detail_with_overrides(request, application, **overrides):
    view = ApplicationDetailView()
    view.request = request
    view.args = ()
    view.kwargs = {"pk": application.pk}
    view.object = application
    context = view.get_context_data(object=application)
    context.update(overrides)
    return render(request, view.template_name, context)


class DashboardView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    template_name = "recruitment/dashboard.html"

    def get(self, request, *args, **kwargs):
        # Workflow roles land on the vacancy batch console — that is their home; the flat
        # My Queue is a secondary view. Only System Admin sees the dashboard overview.
        if request.user.role != RecruitmentUser.Role.SYSTEM_ADMIN:
            return redirect("vacancy-batches")
        return super().get(request, *args, **kwargs)

    # Failed/locked authentication events surfaced on the security card.
    SECURITY_WATCH_ACTIONS = [
        AuditLog.Action.INTERNAL_LOGIN_FAILED,
        AuditLog.Action.INTERNAL_LOGIN_LOCKED,
        AuditLog.Action.INTERNAL_MFA_FAILED,
        AuditLog.Action.INTERNAL_MFA_LOCKED,
        AuditLog.Action.APPLICATION_OTP_FAILED,
        AuditLog.Action.APPLICATION_OTP_LOCKED,
        AuditLog.Action.EVIDENCE_ACCESS_DENIED,
    ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["open_entry_count"] = get_visible_positions_for_user(user).count()
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            internal_users = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())
            context["internal_user_count"] = internal_users.count()
            context["active_internal_user_count"] = internal_users.filter(is_active=True).count()
            context["recent_identity_logs"] = AuditLog.objects.filter(
                action__in=[
                    AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_UPDATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                    AuditLog.Action.INTERNAL_ROLE_CHANGED,
                ]
            )[:5]

            # Panel revision (Chavez, "improve dashboard"): the admin home is an
            # oversight console, assembled from data the system already tracks.
            today = timezone.localdate()
            week_ago = today - timedelta(days=6)

            active_postings = PositionPosting.objects.filter(
                status=PositionPosting.EntryStatus.ACTIVE
            )
            context["active_vacancy_count"] = active_postings.count()
            context["closing_soon_count"] = active_postings.filter(
                closing_date__gte=today, closing_date__lte=today + timedelta(days=7)
            ).count()

            open_cases = RecruitmentCase.objects.exclude(
                current_stage=RecruitmentCase.Stage.CLOSED
            )
            context["open_case_count"] = open_cases.count()
            stage_counts = {
                row["current_stage"]: row["n"]
                for row in open_cases.values("current_stage").annotate(n=Count("id"))
            }
            context["case_stage_counts"] = [
                {"label": label, "count": stage_counts.get(value, 0)}
                for value, label in RecruitmentCase.Stage.choices
                if value != RecruitmentCase.Stage.CLOSED
            ]
            context["overdue_case_count"] = open_cases.filter(
                stage_entered_at__lt=timezone.now() - STAGE_SLA_OVERDUE_THRESHOLD
            ).count()

            context["audit_week_summary"] = summarize_audit_logs(
                get_all_audit_logs(date_from=week_ago)
            )
            context["audit_week_start"] = week_ago
            week_events = AuditLog.objects.filter(created_at__date__gte=week_ago)
            context["security_event_count"] = week_events.filter(
                action__in=self.SECURITY_WATCH_ACTIONS
            ).count()
            context["sensitive_event_count"] = week_events.filter(
                is_sensitive_access=True
            ).count()
            context["audit_archive_count"] = AuditLogArchive.objects.count()
            context["latest_audit_archive"] = AuditLogArchive.objects.first()
        else:
            context["queue"] = get_queue_for_user(user)
        return context


class ForbiddenView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    template_name = "forbidden.html"


def _safe_internal_redirect(request, target_url, fallback_url):
    if target_url and url_has_allowed_host_and_scheme(
        target_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target_url
    return fallback_url


class NotificationListView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request):
        notifications = list(get_recent_notifications(request.user, limit=100))
        unread_count = sum(1 for n in notifications if n.read_at is None)
        return render(
            request,
            "recruitment/notification_list.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
                "has_unread": unread_count > 0,
            },
        )


class NotificationReadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        try:
            notification = mark_notification_read(pk, request.user)
        except Notification.DoesNotExist as exc:
            raise Http404 from exc
        fallback_url = reverse("notification-list")
        redirect_url = _safe_internal_redirect(
            request,
            request.POST.get("next")
            or notification.related_url
            or request.META.get("HTTP_REFERER"),
            fallback_url,
        )
        return redirect(redirect_url)


class NotificationReadAllView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request):
        mark_all_notifications_read(request.user)
        fallback_url = reverse("notification-list")
        redirect_url = _safe_internal_redirect(
            request,
            request.POST.get("next") or request.META.get("HTTP_REFERER"),
            fallback_url,
        )
        return redirect(redirect_url)


class NotificationUnreadCountView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request):
        return JsonResponse({"count": get_unread_count(request.user)})


class PositionListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/position_list.html"
    context_object_name = "positions"

    def get(self, request, *args, **kwargs):
        if request.user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return get_visible_positions_for_user(self.request.user)


# Section keys offered in the workflow list "Needed step" filter (built from the
# canonical queue labels; "overview" is a non-actionable placeholder, so it is dropped).
WORKFLOW_LIST_STEP_CHOICES = [
    (key, label) for key, label in QUEUE_TASK_LABELS.items() if key != "overview"
]


def _apply_workflow_list_search(queryset, q):
    """Filter a role-scoped application queryset by applicant name / reference / position.
    Stays inside the caller's already-scoped queryset — it only narrows, never widens."""
    q = (q or "").strip()
    if not q:
        return queryset
    return queryset.filter(
        Q(applicant__first_name__icontains=q)
        | Q(applicant__last_name__icontains=q)
        | Q(reference_number__icontains=q)
        | Q(position__title__icontains=q)
    )


def _workflow_list_object_list(base_queryset, params):
    """Apply the shared search + step filter to a scoped queryset. Returns a queryset when only
    search is applied, or a Python list when the step filter is applied (the current workflow
    section is computed per-application, not stored, so it can't be filtered in the database).
    Either type is paginatable by ListView."""
    queryset = _apply_workflow_list_search(base_queryset, params.get("q", ""))
    branch = (params.get("branch", "") or "").strip()
    if branch in {PositionPosting.Branch.PLANTILLA, PositionPosting.Branch.COS}:
        queryset = queryset.filter(branch=branch)
    step = (params.get("step", "") or "").strip()
    if step:
        return [
            application
            for application in queryset
            if get_current_workflow_section(application) == step
        ]
    return queryset


def _add_workflow_list_search_context(context, request):
    """Populate the shared search/filter/pagination context used by application_list.html."""
    params = request.GET
    context["q"] = (params.get("q", "") or "").strip()
    context["step"] = (params.get("step", "") or "").strip()
    context["branch"] = (params.get("branch", "") or "").strip()
    context["step_choices"] = WORKFLOW_LIST_STEP_CHOICES
    context["show_search"] = True
    querystring = request.GET.copy()
    querystring.pop("page", None)
    context["querystring"] = querystring.urlencode()
    paginator = context.get("paginator")
    context["total_count"] = paginator.count if paginator else len(context.get("applications") or [])


class ApplicationListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"

    def get(self, request, *args, **kwargs):
        user = request.user
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return redirect("workflow-queue")

    def get_queryset(self):
        user = self.request.user
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return get_queue_for_user(user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = self.request.user.role != RecruitmentUser.Role.APPLICANT
        return context


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
        if pending_absence and user_can_process_application(user, application):
            context["attendance_letter_form"] = AttendanceLetterOfIntentForm()
            context["attendance_decision_form"] = AttendanceAbsenceDecisionForm()
        # SL-10: a neutral exit from an active competition, available at every pre-selection
        # stage to whoever the case is assigned to.
        if user_can_record_withdrawal(user, application):
            context["withdrawal_form"] = ApplicationWithdrawalForm()
        # SL-14: a recorded selection is strongly controlled but no longer a one-way door.
        if application.branch == PositionPosting.Branch.PLANTILLA:
            context["final_selection_history"] = get_final_selection_history(
                application.position
            )
            if user_can_rescind_final_selection(
                user, get_final_selection_for_application(application)
            ):
                context["final_selection_rescind_form"] = FinalSelectionRescindForm()
        else:
            # SL-14 (COS): the same correction, on the record that carries the COS decision
            # — SL-01 made the finalized deliberation pick the selection itself.
            context["cos_rescinded_picks"] = get_rescinded_cos_picks_for_case(
                getattr(application, "case", None)
            )
            if user_can_rescind_cos_vacancy_deliberation(
                user, get_cos_pick_for_case(getattr(application, "case", None))
            ):
                context["cos_pick_rescind_form"] = CosVacancyDeliberationRescindForm()
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
        context["car_weights"] = application.position.assessment_weights_or_default
        # SL-08A: the QS and each candidate's screened credentials, so the ETE verdict is
        # made against the record rather than from memory.
        context["car_candidate_facts"] = get_car_candidate_fact_rows(
            comparative_assessment_items
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
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            context["audit_log_url"] = reverse("application-audit-log", kwargs={"pk": application.pk})
            context["evidence_items"] = list(
                get_evidence_queryset_for_user(
                    user,
                    application=application,
                    archival_status="all",
                )
            )
            context["can_archive_evidence"] = user_can_manage_evidence_archive(user, application)
            context["evidence_vault_url"] = (
                f"{reverse('evidence-vault-list')}?q={application.reference_label}"
            )
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
        context["screening_disposition_required"] = (
            screening_requires_disposition_for_current_stage(application)
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
            exam_schedule = get_exam_schedule(application)
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
            context["cos_deliberation_pick"] = get_cos_vacancy_deliberation(application.position)
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
                if not user_can_process_application(user, application):
                    context["next_batch_case"] = next(
                        (c for c in scoped_batch if c.pk != application.pk), None
                    )
        if application.branch == PositionPosting.Branch.COS:
            # SL-01: the COS selection is made once, on the deliberation console. The case's
            # Decision surface shows that authoritative pick — it never offers a second,
            # independent choice.
            # SL-02: show the pick from the round THIS case was actually considered in —
            # a candidate the pool is still holding has not been decided by anyone's round.
            pick = get_cos_pick_for_case(application.case) or get_cos_vacancy_deliberation(
                application.position
            )
            # SL-14 (COS): is_effective, not is_finalized — a rescinded pick keeps its
            # finalization (that history is the point), so testing is_finalized here would
            # present a voided pick as "the authoritative selection" during the exact
            # window when the vacancy is undecided and awaiting its replacement.
            if pick and pick.is_effective:
                context["cos_authoritative_pick"] = pick
                context["cos_pick_is_this_case"] = pick.chosen_case_id == application.case.id
        if user_can_record_final_selection(user, application):
            report = context["current_comparative_assessment_report"]
            context["final_selection_report"] = report
            context["final_selection_items"] = context["current_comparative_assessment_report_items"]
            context["final_selection_form"] = FinalSelectionForm(report=report)
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
        if get_case_handoff_options(application, user):
            context["case_handoff_form"] = CaseHandoffForm(application=application, user=user)
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
            context["notification_log_entries"] = list(
                application.notifications.select_related("triggered_by").order_by(
                    "-created_at"
                )[:25]
            )
        context["can_export"] = user_can_export_application(user, application)
        record_protected_record_access(
            application=application,
            actor=user,
            source="application_detail",
        )
        return context


class ApplicationAuditLogView(
    LoginRequiredMixin,
    SystemAdministratorRequiredMixin,
    DetailView,
):
    model = RecruitmentApplication
    template_name = "recruitment/audit_log_list.html"
    context_object_name = "application"

    def get_object(self, queryset=None):
        return super().get_object(queryset)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context["application"]
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        cleaned_data = _audit_search_cleaned_data(self.search_form)
        audit_logs = list(
            get_application_audit_logs(
                application,
                search_query=cleaned_data["q"],
                action=cleaned_data["action"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                category=cleaned_data["category"],
                actor=cleaned_data["actor"],
                date_from=cleaned_data["date_from"],
                date_to=cleaned_data["date_to"],
            )
        )
        context["search_form"] = self.search_form
        context["audit_logs"] = audit_logs
        context["result_count"] = len(audit_logs)
        context["review_scope"] = "application"
        context["recruitment_case"] = getattr(application, "case", None)
        if _should_record_audit_review(self.request, f"application:{application.pk}"):
            record_audit_log_review(
                actor=self.request.user,
                application=application,
                search_query=cleaned_data["q"],
                action=cleaned_data["action"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                result_count=len(audit_logs),
            )
        return context


class AuditLogListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, TemplateView):
    template_name = "recruitment/audit_log_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # REV-D archival panel: existing archives, the age guard, and — when a
        # cutoff was chosen — the confirmation preview (count + date range).
        context["audit_archives"] = AuditLogArchive.objects.select_related("created_by")[:20]
        context["archive_latest_cutoff"] = audit_archive_latest_allowed_cutoff()
        cutoff_raw = (self.request.GET.get("archive_cutoff") or "").strip()
        if cutoff_raw:
            try:
                cutoff = date.fromisoformat(cutoff_raw)
            except ValueError:
                cutoff = None
            if cutoff and cutoff <= audit_archive_latest_allowed_cutoff():
                context["archive_preview"] = preview_audit_archive(cutoff)
                context["archive_preview_cutoff"] = cutoff
            elif cutoff:
                context["archive_preview_error"] = (
                    "Only entries older than six months can be archived. Choose a cutoff "
                    f"on or before {audit_archive_latest_allowed_cutoff():%b %d, %Y}."
                )
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        cleaned_data = _audit_search_cleaned_data(self.search_form)
        session_tag = (self.request.GET.get("session") or "").strip()
        audit_logs = get_all_audit_logs(
            search_query=cleaned_data["q"],
            action=cleaned_data["action"],
            actor_role=cleaned_data["actor_role"],
            sensitive_only=cleaned_data["sensitive_only"],
            category=cleaned_data["category"],
            actor=cleaned_data["actor"],
            date_from=cleaned_data["date_from"],
            date_to=cleaned_data["date_to"],
            session_tag=session_tag,
        )
        # REV-F: the session view reads chronologically — a visit replayed
        # top to bottom.
        if session_tag:
            audit_logs = audit_logs.order_by("created_at")
            context["session_summary"] = build_audit_session_summary(session_tag)
            context["session_tag"] = session_tag
        context["audit_summary"] = summarize_audit_logs(audit_logs)
        today = timezone.localdate()
        context["date_presets"] = [
            {"label": "Today", "date_from": today, "date_to": today},
            {"label": "Last 7 days", "date_from": today - timedelta(days=6), "date_to": today},
            {"label": "This month", "date_from": today.replace(day=1), "date_to": today},
        ]
        paginator = Paginator(audit_logs, 50)
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["search_form"] = self.search_form
        context["audit_logs"] = page_obj.object_list
        context["page_obj"] = page_obj
        context["result_count"] = paginator.count
        context["querystring"] = params.urlencode()
        context["review_scope"] = "system"
        # One review entry per visit: the first-page guard stops paging spam,
        # and the session window (REV-D) stops filter-change spam.
        if page_number in (None, "", "1") and _should_record_audit_review(
            self.request, "consolidated"
        ):
            record_audit_log_review(
                actor=self.request.user,
                search_query=cleaned_data["q"],
                action=cleaned_data["action"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                result_count=paginator.count,
            )
        return context


def _audit_search_cleaned_data(search_form):
    if search_form.is_valid():
        return search_form.cleaned_data
    return {
        "q": "",
        "category": "",
        "action": "",
        "actor_role": "",
        "actor": None,
        "date_from": None,
        "date_to": None,
        "sensitive_only": False,
    }


def _filtered_audit_logs_from_request(request):
    """Shared by the list view and both exports so a report always contains
    exactly what the filtered screen shows."""
    form = AuditLogSearchForm(request.GET or None)
    cleaned = _audit_search_cleaned_data(form)
    session_tag = (request.GET.get("session") or "").strip()
    queryset = get_all_audit_logs(
        search_query=cleaned["q"],
        action=cleaned["action"],
        actor_role=cleaned["actor_role"],
        sensitive_only=cleaned["sensitive_only"],
        category=cleaned["category"],
        actor=cleaned["actor"],
        date_from=cleaned["date_from"],
        date_to=cleaned["date_to"],
        session_tag=session_tag,
    )
    if session_tag:
        queryset = queryset.order_by("created_at")
    return queryset, cleaned, session_tag


def _describe_audit_filters(cleaned, session_tag):
    parts = []
    if cleaned["q"]:
        parts.append(f'search "{cleaned["q"]}"')
    if cleaned["category"]:
        parts.append(f"category {AUDIT_ACTION_CATEGORIES[cleaned['category']]['label']}")
    if cleaned["action"]:
        parts.append(f"action {cleaned['action']}")
    if cleaned["actor"]:
        parts.append(f"user {cleaned['actor'].get_full_name() or cleaned['actor'].username}")
    if cleaned["actor_role"]:
        parts.append(f"role {cleaned['actor_role']}")
    if cleaned["date_from"]:
        parts.append(f"from {cleaned['date_from']:%b %d, %Y}")
    if cleaned["date_to"]:
        parts.append(f"to {cleaned['date_to']:%b %d, %Y}")
    if cleaned["sensitive_only"]:
        parts.append("sensitive only")
    if session_tag:
        parts.append("single session")
    return "; ".join(parts) if parts else "no filters (full trail)"


class AuditLogExportCsvView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-F: CSV export of the filtered audit view. The export itself is
    audit-logged."""

    def get(self, request):
        queryset, cleaned, session_tag = _filtered_audit_logs_from_request(request)
        filters_label = _describe_audit_filters(cleaned, session_tag)
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["created_at", "actor", "actor_role", "action", "category",
             "case_reference", "workflow_stage", "description", "sensitive"]
        )
        count = 0
        for entry in queryset.iterator():
            writer.writerow(
                [
                    entry.created_at.isoformat(),
                    entry.actor.username if entry.actor else "",
                    entry.actor_role,
                    entry.action,
                    AUDIT_CATEGORY_BY_ACTION.get(entry.action, "other"),
                    entry.case_reference,
                    entry.workflow_stage,
                    entry.description,
                    entry.is_sensitive_access,
                ]
            )
            count += 1
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=f"Exported {count} audit entries to CSV ({filters_label}).",
            metadata={"export": "audit_csv", "entry_count": count, "filters": filters_label},
        )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="audit-report-{timezone.localdate():%Y%m%d}.csv"'
        )
        return response


class AuditLogReportView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-F: print-ready audit report — the filtered view as a document a
    director can hold: header with the filters applied, the summary strip,
    then the entries."""

    def get(self, request):
        queryset, cleaned, session_tag = _filtered_audit_logs_from_request(request)
        summary = summarize_audit_logs(queryset)
        entries = list(queryset[:1000])
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=(
                f"Generated printable audit report ({summary['total']} entries; "
                f"{_describe_audit_filters(cleaned, session_tag)})."
            ),
            metadata={
                "export": "audit_report",
                "entry_count": summary["total"],
                "filters": _describe_audit_filters(cleaned, session_tag),
            },
        )
        return render(
            request,
            "recruitment/audit_report_print.html",
            {
                "entries": entries,
                "entry_cap": 1000,
                "summary": summary,
                "filters_label": _describe_audit_filters(cleaned, session_tag),
                "session_summary": build_audit_session_summary(session_tag) if session_tag else None,
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class AuditLogArchiveView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-D: execute an archive run (export, verify, delete). The GET-side
    preview/confirmation lives on AuditLogListView; this POST only fires from
    the confirmation form."""

    def post(self, request):
        cutoff_raw = (request.POST.get("archive_cutoff") or "").strip()
        try:
            cutoff = date.fromisoformat(cutoff_raw)
        except ValueError:
            messages.error(request, "Choose a valid cutoff date for the archive.")
            return redirect("audit-log-list")
        try:
            archive = archive_audit_logs(request.user, cutoff)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("audit-log-list")
        messages.success(
            request,
            f"Archived {archive.entry_count} audit entries to {archive.file_name}. "
            "The live log now starts after the archived range; the archival "
            "action itself is on the record.",
        )
        return redirect("audit-log-list")


class AuditArchiveDownloadView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def get(self, request, pk):
        archive = get_object_or_404(AuditLogArchive, pk=pk)
        content = download_audit_archive(archive, request.user)
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{archive.file_name}"'
        return response


class EvidenceUploadView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_evidence(request.user, application):
            raise PermissionDenied
        form = EvidenceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            evidence = upload_evidence_item(
                application=application,
                actor=request.user,
                label=form.cleaned_data["label"],
                uploaded_file=form.cleaned_data["file"],
                artifact_scope=(
                    EvidenceVaultItem.OwnerScope.CASE
                    if hasattr(application, "case")
                    else EvidenceVaultItem.OwnerScope.APPLICATION
                ),
                artifact_type="workflow_evidence",
            )
            messages.success(
                request,
                f"File saved as {evidence.version_label}.",
            )
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("evidence-vault-list")


class EvidenceDownloadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk, evidence_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        evidence = get_object_or_404(
            EvidenceVaultItem.objects.select_related(
                "application",
                "recruitment_case",
                "recruitment_case__application",
                "recruitment_entry",
            ),
            pk=evidence_pk,
        )
        if not evidence_belongs_to_application_context(evidence, application):
            record_evidence_access_denied(
                evidence,
                request.user,
                application=application,
                reason="context_mismatch",
            )
            raise Http404
        try:
            content = decrypt_evidence_bytes(evidence, request.user, application=application)
        except ValueError as exc:
            raise PermissionDenied(str(exc))
        response = HttpResponse(
            content,
            content_type=evidence.content_type or "application/octet-stream",
        )
        # Inline viewing is only honoured for a safe allowlist of static media
        # types (images and PDF). Every other type stays an attachment so an
        # uploaded file cannot execute active content in the RecruitGuard origin.
        serve_inline = (
            request.GET.get("disposition") == "inline" and evidence.is_inline_viewable
        )
        disposition = "inline" if serve_inline else "attachment"
        response["Content-Disposition"] = (
            f'{disposition}; filename="{evidence.original_filename}"'
        )
        response["X-Content-Type-Options"] = "nosniff"
        if serve_inline:
            # Defense in depth: tighten the global policy so the inline file
            # cannot run scripts, submit forms, or be framed even if a file
            # slipped past the allowlist. Same-origin loads stay allowed so the
            # browser can still render the image/PDF.
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'none'; object-src 'none'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        return response


class EvidenceArchiveToggleView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk, evidence_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_evidence_archive(request.user, application):
            raise PermissionDenied

        evidence = get_object_or_404(
            EvidenceVaultItem.objects.select_related(
                "application",
                "recruitment_case",
                "recruitment_case__application",
                "recruitment_entry",
            ),
            pk=evidence_pk,
        )
        if not evidence_belongs_to_application_context(evidence, application):
            raise Http404
        form = EvidenceArchiveForm(request.POST)
        if form.is_valid():
            try:
                update_evidence_archive_status(
                    evidence=evidence,
                    actor=request.user,
                    action=form.cleaned_data["action"],
                    archive_tag=form.cleaned_data["archive_tag"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                if form.cleaned_data["action"] == "archive":
                    messages.success(request, "File archived with its archive label.")
                else:
                    messages.success(request, "File restored from archive.")
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect(_safe_next_url(request, reverse("evidence-vault-list")))


class EvidenceVaultListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, ListView):
    template_name = "recruitment/evidence_vault_list.html"
    context_object_name = "evidence_items"
    paginate_by = 50

    def get_queryset(self):
        self.search_form = EvidenceVaultSearchForm(self.request.GET or None)
        if self.search_form.is_valid():
            cleaned_data = self.search_form.cleaned_data
        else:
            cleaned_data = {
                "q": "",
                "stage": "",
                "artifact_scope": "",
                "archival_status": "active",
                "current_version_only": True,
            }
        self.search_filters = cleaned_data
        return get_evidence_queryset_for_user(
            self.request.user,
            search_query=cleaned_data["q"],
            stage=cleaned_data["stage"],
            artifact_scope=cleaned_data["artifact_scope"],
            archival_status=cleaned_data["archival_status"],
            current_version_only=cleaned_data["current_version_only"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        evidence_items = list(context["evidence_items"])
        for evidence in evidence_items:
            # Oversight is metadata-only: resolve the owning case for labelling,
            # but never authorize the admin to download the file content.
            evidence.owner_application = get_evidence_owner_application(evidence)
        context["evidence_items"] = evidence_items
        context["search_form"] = self.search_form
        context["result_count"] = context["paginator"].count if context.get("paginator") else len(evidence_items)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        record_evidence_vault_access(
            self.request.user,
            search_query=self.search_filters["q"],
            stage=self.search_filters["stage"],
            artifact_scope=self.search_filters["artifact_scope"],
            archival_status=self.search_filters["archival_status"],
            current_version_only=self.search_filters["current_version_only"],
        )
        return context


class WorkflowQueueView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"
    paginate_by = 25

    def get_queryset(self):
        return _workflow_list_object_list(
            get_queue_for_user(self.request.user), self.request.GET
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = True
        _add_workflow_list_search_context(context, self.request)
        return context


class ClosedCaseListView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, ListView):
    """Closed / decided cases the user retains view access to — the records-request lookup that
    the active queue can't provide. Scoped by get_closed_cases_for_user, which mirrors the
    existing closed-case access rules, so it never shows a case the user couldn't already open."""

    template_name = "recruitment/application_list.html"
    context_object_name = "applications"
    paginate_by = 25

    def get_queryset(self):
        return _workflow_list_object_list(
            get_closed_cases_for_user(self.request.user), self.request.GET
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = False
        context["is_closed_list"] = True
        _add_workflow_list_search_context(context, self.request)
        return context


class VacancyBatchConsoleView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, TemplateView):
    """Read-only vacancy-grouped view of the user's own queue. Groups the same
    FRS-scoped applications get_queue_for_user returns (so the Level-1 -> Secretariat /
    Level-2 -> HRM Chief routing and the support-role/override carve-outs are preserved)
    by vacancy, with a per-vacancy "awaiting your action" count for triage."""

    template_name = "recruitment/vacancy_batches.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        groups = {}
        for application in get_queue_for_user(self.request.user):
            group = groups.setdefault(
                application.position_id,
                {"entry": application.position, "applications": []},
            )
            group["applications"].append(application)
        vacancies = sorted(
            groups.values(),
            key=lambda g: (g["entry"].get_branch_display(), (g["entry"].title or "").lower()),
        )
        for group in vacancies:
            group["count"] = len(group["applications"])
        context["vacancies"] = vacancies
        context["total_applications"] = sum(group["count"] for group in vacancies)
        return context


class VacancyBatchDetailView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, TemplateView):
    """The applicants of a single vacancy that are in the current user's scoped queue,
    each linking to the existing per-case detail. 404s when the user has no scoped
    applications for the vacancy AND no closed-case access to it, so a Secretariat never
    sees a Level-2 batch; a user whose own cases simply closed gets a note instead."""

    template_name = "recruitment/vacancy_batch_detail.html"

    def get(self, request, *args, **kwargs):
        pk = self.kwargs["pk"]
        if get_queue_for_user(request.user).filter(position_id=pk).exists():
            return super().get(request, *args, **kwargs)
        # Nothing queued here, for one of two very different reasons: the vacancy's cases are
        # all finished (this user worked them through to closure), or the vacancy was never
        # theirs (FRS scoping — a Secretariat must not see a Level-2 batch). Only the first
        # earns a friendly landing; the second keeps the 404 so a stale or guessed URL can
        # never confirm that another role's batch exists. get_closed_cases_for_user mirrors
        # the closed-access model exactly, so this cannot widen what the user may see.
        if get_closed_cases_for_user(request.user).filter(position_id=pk).exists():
            messages.info(
                request,
                "Every case you handled for this vacancy is closed, so its batch page is no "
                "longer active. You can still look them up under Closed cases.",
            )
            return redirect("vacancy-batches")
        raise Http404("No batch is available for this vacancy.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = self.kwargs["pk"]
        applications = [
            application
            for application in get_queue_for_user(self.request.user)
            if application.position_id == pk
        ]
        if not applications:
            # Defensive net — get() already resolved the empty-queue case above.
            raise Http404("No batch is available for this vacancy.")
        entry = applications[0].position
        context["entry"] = entry
        context["applications"] = applications
        context["screening_batch"] = vacancy_screening_batch_status(entry)
        # SL-09: the batch waits for a final applicant pool — say when it unlocks rather
        # than offering a link that bounces straight back.
        context["batch_pool_block_message"] = vacancy_batch_pool_block_message(entry)
        context["exam_batch_ready"] = any(
            user_can_view_application(self.request.user, application)
            for application in vacancy_exam_pool(entry)
        )
        context["interview_batch_ready"] = any(
            user_can_view_application(self.request.user, application)
            for application in vacancy_interview_pool(entry)
        )
        context["cos_deliberation_ready"] = entry.branch == PositionPosting.Branch.COS and any(
            user_can_view_application(self.request.user, application)
            for application in cos_deliberation_pool(entry)
        )
        return context


def _batch_pool_empty_redirect(request, entry, message):
    """A batch exam/interview console whose scoped pool is empty used to ``raise Http404``,
    which surfaced as a bare "Page Not Found" right after finalizing: the finalized cases
    leave that stage's pool, so a refresh, Back-button, or re-run of the same URL hit an
    empty console. Send the reviewer back to the batch overview with a friendly note instead
    of crashing.

    An empty scoped pool has two causes, and only one is friendly: (a) the stage finished and
    its cases moved on, or (b) the user has no business on this vacancy's batch at all (wrong
    role — FRS scoping). Redirect only when the user still holds batch access for the vacancy
    (the same queue check ``VacancyBatchDetailView`` enforces); otherwise keep the 404 so a
    non-reviewing role can never probe another role's batch."""
    if not get_queue_for_user(request.user).filter(position=entry).exists():
        raise Http404("No batch is available for this vacancy.")
    messages.info(request, message)
    return redirect("vacancy-batch-detail", pk=entry.pk)


class VacancyBatchExamView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """Vacancy-level batch exam: one schedule + one score grid + one finalize over the
    qualified pool, looping the per-case exam services. FRS-scoped (only the reviewing role for
    the vacancy sees its pool); the schedule/score actions refuse until the screening batch is
    ready (Phase-4 gate)."""

    template_name = "recruitment/vacancy_batch_exam.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in vacancy_exam_pool(entry)
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool, schedule_form=None):
        candidates = []
        for application in pool:
            exam_record = get_exam_record(application)
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "exam_record": exam_record,
                    "exam_status": exam_record.exam_status if exam_record else "",
                    "general_score": exam_record.general_score if exam_record else None,
                    "technical_score": exam_record.technical_score if exam_record else None,
                    "schedule": get_exam_schedule(application),
                }
            )
        weights = entry.assessment_weights_or_default
        candidate_count = len(candidates)
        schedule_sent = bool(candidates) and all(c["schedule"] for c in candidates)
        finalize_exam_body = (
            f"This locks the recorded exam scores for all {candidate_count} "
            f"candidate{'' if candidate_count == 1 else 's'} and moves the whole pool to the "
            "next review step. Scores can no longer be edited afterwards."
        )
        # Judgment item A: candidates marked Absent/Waived lock without advancing — say so
        # in the finalize confirmation.
        absent_waived_count = sum(
            1
            for c in candidates
            if c["exam_record"]
            and c["exam_record"].exam_status
            in {ExamRecord.ExamStatus.ABSENT, ExamRecord.ExamStatus.WAIVED}
        )
        finalize_exam_summary_items = []
        if absent_waived_count:
            finalize_exam_summary_items.append(
                f"{absent_waived_count} candidate{'' if absent_waived_count == 1 else 's'} "
                "marked Absent/Waived will not proceed"
            )
        return {
            "finalize_exam_summary_items": finalize_exam_summary_items,
            "entry": entry,
            "candidates": candidates,
            "candidate_count": candidate_count,
            "schedule_sent": schedule_sent,
            "finalize_exam_body": finalize_exam_body,
            "is_plantilla": entry.branch == PositionPosting.Branch.PLANTILLA,
            "screening_batch": vacancy_screening_batch_status(entry),
            "weights": weights,
            "schedule_form": schedule_form or ExamScheduleForm(),
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: one door in BOTH directions. A continuous-intake vacancy is
        # examined per case, so its console is refused outright — otherwise the
        # console URL reopens the two-door bug RD-2 closed, just mirrored.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's exam is recorded per case — open the candidate's "
                "case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the exam step for this vacancy right now — they may "
                "already have been examined and moved to the interview stage, or none are "
                "ready yet.",
            )
        # Batch gate (Phase-4): while any applicant is still pending screening or awaiting
        # resubmission, the exam screen must not be exposed at all — not even read-only. Send
        # the reviewer back to the batch detail so a forced URL cannot preview the exam stage.
        if not vacancy_screening_batch_status(entry).is_ready:
            messages.error(request, vacancy_screening_batch_block_message(entry))
            return redirect("vacancy-batch-detail", pk=entry.pk)
        # SL-09: and the batch itself cannot open until the pool it covers is final —
        # otherwise a timely later applicant misses the sitting they were entitled to.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: same refusal as GET — a forced POST must not write through the
        # console for a per-case (continuous) vacancy.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's exam is recorded per case — open the candidate's "
                "case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the exam step for this vacancy right now — they may "
                "already have been examined and moved to the interview stage, or none are "
                "ready yet.",
            )
        # Same batch gate as GET: refuse every exam operation (schedule/score/finalize) while
        # the screening batch is on hold. The exam services re-check this too, but blocking here
        # keeps a forced POST from re-rendering the read-only exam screen on a form error.
        if not vacancy_screening_batch_status(entry).is_ready:
            messages.error(request, vacancy_screening_batch_block_message(entry))
            return redirect("vacancy-batch-detail", pk=entry.pk)
        # SL-09: and the batch itself cannot open until the pool it covers is final —
        # otherwise a timely later applicant misses the sitting they were entitled to.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        operation = request.POST.get("operation", "")

        if operation == "save_schedule":
            form = ExamScheduleForm(request.POST)
            if not form.is_valid():
                messages.error(
                    request,
                    _format_form_errors(form, "Complete the exam schedule fields before saving."),
                )
                return render(
                    request, self.template_name, self._context(request, entry, pool, schedule_form=form)
                )
            try:
                save_vacancy_exam_schedule(entry, request.user, form.cleaned_data)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Exam schedule sent to every candidate in the batch.")
            return redirect("vacancy-batch-exam", pk=entry.pk)

        if operation in {"save_scores", "finalize"}:
            finalize = operation == "finalize"
            rows = {}
            for application in pool:
                case_id = application.case.id
                general = (request.POST.get(f"general_score_{case_id}", "") or "").strip()
                technical = (request.POST.get(f"technical_score_{case_id}", "") or "").strip()
                row = {}
                if general:
                    row["general_score"] = general
                if technical:
                    row["technical_score"] = technical
                # RD-2: Absent / Waived is chosen here — the per-case exam screen
                # is read-only for a pooled vacancy, so this is the only door. That
                # makes it the only door for the remarks an absence/waiver REQUIRES,
                # too — without this field a batch holding one absent candidate could
                # never be finalized (E2E finding).
                outcome = (request.POST.get(f"outcome_{case_id}", "") or "").strip()
                if outcome:
                    row["exam_status"] = outcome
                notes = (request.POST.get(f"notes_{case_id}", "") or "").strip()
                if notes:
                    row["exam_notes"] = notes
                if row:
                    rows[case_id] = row
            # The pool sat one exam, so one validity window covers the batch.
            valid_from = (request.POST.get("valid_from", "") or "").strip() or None
            valid_until = (request.POST.get("valid_until", "") or "").strip() or None
            # Capture the pre-finalize handler role so the toast can tell "routed to a new
            # office" apart from "same office, next step". The pool moves in lockstep, so one
            # representative role is enough.
            handler_before_finalize = pool[0].current_handler_role if pool else ""
            try:
                saved = save_vacancy_exam_scores(
                    entry,
                    request.user,
                    rows,
                    finalize=finalize,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
            except (ValueError, ValidationError) as exc:
                # A ValidationError's str() is a raw Python dict — join its messages so the
                # reviewer reads "Provide remarks explaining the waiver or absence." rather
                # than "{'exam_notes': [...]}" (E2E finding).
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
                return redirect("vacancy-batch-exam", pk=entry.pk)
            if finalize:
                # An Absent candidate did NOT advance — they hold at the exam awaiting the
                # letter-of-intent decision (SL-06), and a Waived one closed terminally.
                # Counting them as "advanced" told the reviewer something false (E2E finding).
                held = [
                    application
                    for application in saved
                    if (request.POST.get(f"outcome_{application.case.id}", "") or "").strip()
                    in {ExamRecord.ExamStatus.ABSENT, ExamRecord.ExamStatus.WAIVED}
                ]
                count = len(saved) - len(held)
                held_note = (
                    f" {len(held)} candidate{'' if len(held) == 1 else 's'} did not sit the "
                    "exam and awaits the absence decision instead."
                    if held
                    else ""
                )
                # save_exam_record mutates each routed application in place, so a changed
                # handler role means the pool advanced to a new office. When it is unchanged
                # (e.g. a COS batch the HRM Chief finalizes at their own review stage — the
                # case only moves from the exam section to the interview section), don't claim
                # a handoff that didn't happen.
                advanced = [a for a in saved if a not in held]
                next_role = advanced[0].current_handler_role if advanced else ""
                if next_role and next_role != handler_before_finalize:
                    next_role_label = RecruitmentUser.Role(next_role).label
                    messages.success(
                        request,
                        f"Exam batch finalized — {count} "
                        f"candidate{'' if count == 1 else 's'} advanced to "
                        f"{next_role_label} review.{held_note}",
                    )
                else:
                    messages.success(
                        request,
                        f"Exam batch finalized — {count} "
                        f"candidate{'' if count == 1 else 's'} moved to the next review "
                        f"step.{held_note}",
                    )
                # Continue straight to the interview console when this user is the one who
                # runs the next step (Plantilla: the Secretariat / HRM Chief support the
                # interview scheduling; COS interview belongs to the HRM Chief). Checked against
                # the freshly-routed pool, which now sits in the interview section. If they are
                # not the interview handler, fall back to the batch overview. Either way, a
                # forced-empty interview console now shows a friendly note (see
                # _batch_pool_empty_redirect) rather than a 404.
                if saved and user_can_manage_interview_session(request.user, saved[0]):
                    return redirect("vacancy-batch-interview", pk=entry.pk)
                return redirect("vacancy-batches")
            messages.success(request, "Exam scores saved.")
            return redirect("vacancy-batch-exam", pk=entry.pk)

        messages.error(request, "This action is not available.")
        return redirect("vacancy-batch-exam", pk=entry.pk)


class VacancyBatchInterviewView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """Vacancy-level batch interview: one schedule over the whole pool, then a finalize.
    COS records a single end-user score per candidate inline (spec §6.2); Plantilla scores
    each candidate's competency sheet on the per-case detail and finalizes the pool together.
    Loops the per-case interview services so FRS routing + lock-step hold."""

    template_name = "recruitment/vacancy_batch_interview.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in vacancy_interview_pool(entry)
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool, schedule_form=None):
        is_plantilla = entry.branch == PositionPosting.Branch.PLANTILLA
        candidates = []
        for application in pool:
            rating = get_interview_rating_for_user(application, request.user)
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "session": get_interview_session(application),
                    "rating": rating,
                    "rating_count": get_interview_ratings(application).count(),
                }
            )
        # WF-08: the HRMPSB panel can't rate a Plantilla pool until the rating sheet is
        # published — surface that prerequisite on the console instead of letting the
        # panel discover it on their case pages.
        rating_sheet_published = True
        rating_sheet_url = ""
        if is_plantilla:
            rating_sheet_published = get_published_competency_rating_template(entry) is not None
            if not rating_sheet_published and pool:
                # The builder edits the per-position template — any case's URL reaches it.
                rating_sheet_url = reverse(
                    "interview-rating-sheet", kwargs={"pk": pool[0].pk}
                )
        return {
            "entry": entry,
            "candidates": candidates,
            "is_plantilla": is_plantilla,
            "schedule_form": schedule_form or InterviewSessionForm(),
            "rating_sheet_published": rating_sheet_published,
            "rating_sheet_url": rating_sheet_url,
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: a continuous-intake vacancy is interviewed per case — its
        # console is refused so the two-door bug can't reopen in reverse.
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's interview is recorded per case — open the "
                "candidate's case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the interview step for this vacancy right now — they "
                "may already have been interviewed and moved to the next stage, or none are "
                "ready yet.",
            )
        # SL-09: the interview batch is the same single cohort as the exam batch, so it
        # waits for the same final pool.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        # RD-5: same refusal as GET (server-side, PT-03).
        if not vacancy_uses_batch_exam(entry):
            messages.error(
                request,
                "This vacancy's interview is recorded per case — open the "
                "candidate's case to schedule or score it.",
            )
            return redirect("vacancy-batch-detail", pk=entry.pk)
        pool = self._scoped_pool(request, entry)
        if not pool:
            return _batch_pool_empty_redirect(
                request,
                entry,
                "No candidates are at the interview step for this vacancy right now — they "
                "may already have been interviewed and moved to the next stage, or none are "
                "ready yet.",
            )
        # SL-09: the interview batch is the same single cohort as the exam batch, so it
        # waits for the same final pool.
        pool_block = vacancy_batch_pool_block_message(entry)
        if pool_block:
            messages.error(request, pool_block)
            return redirect("vacancy-batch-detail", pk=entry.pk)
        operation = request.POST.get("operation", "")

        if operation == "save_schedule":
            form = InterviewSessionForm(request.POST)
            if not form.is_valid():
                messages.error(
                    request,
                    _format_form_errors(form, "Complete the interview schedule fields before saving."),
                )
                return render(
                    request, self.template_name, self._context(request, entry, pool, schedule_form=form)
                )
            try:
                save_vacancy_interview_schedule(entry, request.user, form.cleaned_data)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Interview schedule sent to every candidate in the batch.")
            return redirect("vacancy-batch-interview", pk=entry.pk)

        if operation in {"save_scores", "finalize"} and entry.branch == PositionPosting.Branch.COS:
            finalize = operation == "finalize"
            rows = {}
            for application in pool:
                case_id = application.case.id
                score = (request.POST.get(f"rating_score_{case_id}", "") or "").strip()
                notes = (request.POST.get(f"rating_notes_{case_id}", "") or "").strip()
                justification = (request.POST.get(f"justification_{case_id}", "") or "").strip()
                row = {}
                if score:
                    row["rating_score"] = score
                if notes:
                    row["rating_notes"] = notes
                if justification:
                    row["justification"] = justification
                if row:
                    rows[case_id] = row
            try:
                save_vacancy_cos_interview_ratings(entry, request.user, rows, finalize=finalize)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
                return redirect("vacancy-batch-interview", pk=entry.pk)
            if finalize:
                messages.success(request, "Interview batch finalized — the pool moved on together.")
                # Same forward as the exam console: for COS the next step is the deliberation
                # pick, run by the same HRM Chief who just finalized the interview. Plantilla
                # deliberately keeps the overview — its next step is the CAR, which belongs to
                # the Secretariat, so there is no single next screen for this finalizer.
                if user_can_manage_cos_deliberation(request.user, entry):
                    return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
                return redirect("vacancy-batches")
            messages.success(request, "Interview scores saved.")
            return redirect("vacancy-batch-interview", pk=entry.pk)

        if operation == "finalize" and entry.branch == PositionPosting.Branch.PLANTILLA:
            # WF-16(c): optional light record of the board's first (pre-CAR) deliberation.
            note = ""
            delib_date = (request.POST.get("first_deliberation_date", "") or "").strip()
            delib_members = (request.POST.get("first_deliberation_members", "") or "").strip()
            if delib_date or delib_members:
                note = (
                    f"First deliberation held on {delib_date}" if delib_date else "First deliberation held"
                )
                if delib_members:
                    note += f", members present {delib_members}"
                note += "."
            try:
                finalize_vacancy_interview_batch(
                    entry, request.user, first_deliberation_note=note
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
                return redirect("vacancy-batch-interview", pk=entry.pk)
            messages.success(request, "Interview batch finalized — the pool moved on together.")
            return redirect("vacancy-batches")

        messages.error(request, "This action is not available.")
        return redirect("vacancy-batch-interview", pk=entry.pk)


class VacancyBatchCosDeliberationView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """The COS decision: one per-vacancy deliberation pick (spec §6.3). The HRM Chief records
    the chosen applicant + notes for the whole pool.

    SL-01: finalizing the pick IS the COS selection — it records the chosen applicant as
    selected, routes them to completion, and closes the rest of the deliberated pool. There
    is no second per-case decision screen that could name someone else."""

    template_name = "recruitment/vacancy_batch_cos_deliberation.html"

    def _scoped_pool(self, request, entry):
        return [
            application
            for application in cos_deliberation_pool(entry)
            if user_can_view_application(request.user, application)
        ]

    def _context(self, request, entry, pool):
        pick = get_cos_vacancy_deliberation(entry)
        # SL-14 (COS): never present a rescinded pick as the working state — the template
        # pre-checks its candidate's radio and shows its notes, which would nudge the
        # re-decision toward the person the office just voided.
        if pick is not None and pick.is_rescinded:
            pick = None
        selection_round = get_open_cos_selection_round(entry)
        latest_round = selection_round or get_latest_cos_selection_round(entry)
        # WF-09: the pick asks the chief to name "the most qualified applicant" — show the
        # finalized comparative data (exam, interview, screening) next to each radio so the
        # decision doesn't rely on recall. Strongest interview scores first; choice stays free.
        candidates = []
        for application in pool:
            exam_record = get_exam_record(application)
            interview_rating = get_interview_ratings(application).first()
            screening_record = get_screening_record(application)
            candidates.append(
                {
                    "application": application,
                    "case_id": application.case.id,
                    "exam_score": exam_record.exam_score if exam_record else None,
                    "interview_score": (
                        interview_rating.rating_score if interview_rating else None
                    ),
                    "screening_outcome": (
                        screening_record.qualification_outcome if screening_record else ""
                    ),
                }
            )
        candidates.sort(
            key=lambda c: (c["interview_score"] is None, -(c["interview_score"] or 0))
        )
        # SL-02: a continuous COS vacancy hires in need-based rounds, so the console has to
        # say which round is open, how many slots it fills, and who is waiting in the pool.
        eligible_pool = cos_eligible_pool(entry)
        return {
            "entry": entry,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "pick": pick,
            "can_manage": user_can_manage_cos_deliberation(request.user, entry),
            "uses_selection_rounds": vacancy_uses_cos_selection_rounds(entry),
            "selection_round": selection_round,
            "latest_round": latest_round,
            "round_picks": (
                # SL-14 (COS): the round's LIVE picks — a rescinded one is not a filled slot.
                list(latest_round.picks.filter(
                    is_finalized=True, is_rescinded=False
                ).select_related("chosen_case", "chosen_case__application"))
                if latest_round
                else []
            ),
            "can_open_round": user_can_open_cos_selection_round(request.user, entry),
            "eligible_pool_count": len(eligible_pool),
        }

    def get(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        if entry.branch != PositionPosting.Branch.COS:
            raise Http404("The COS deliberation pick is only for COS vacancies.")
        pool = self._scoped_pool(request, entry)
        pick = get_cos_vacancy_deliberation(entry)
        if (
            not pool
            and not (pick and pick.is_finalized)
            and not user_can_open_cos_selection_round(request.user, entry)
        ):
            raise Http404("No COS deliberation is available for this vacancy.")
        return render(request, self.template_name, self._context(request, entry, pool))

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        operation = request.POST.get("operation", "")
        if operation == "open_round":
            # SL-02: the office decides when it is hiring; opening the round freezes the
            # candidates it will consider.
            if not user_can_open_cos_selection_round(request.user, entry):
                raise PermissionDenied
            try:
                selection_round = open_cos_selection_round(
                    entry,
                    request.user,
                    slot_count=request.POST.get("slot_count", "1"),
                    notes=request.POST.get("round_notes", ""),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    f"Selection round {selection_round.round_number} opened with "
                    f"{selection_round.members.count()} candidate(s) and "
                    f"{selection_round.slot_count} slot(s).",
                )
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        if not user_can_manage_cos_deliberation(request.user, entry):
            raise PermissionDenied
        finalize = operation == "finalize"
        if operation not in {"save", "finalize"}:
            messages.error(request, "This action is not available.")
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        try:
            save_cos_vacancy_deliberation(
                entry,
                request.user,
                request.POST.get("chosen_case"),
                request.POST.get("notes", ""),
                finalize=finalize,
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, str(exc))
            return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)
        if finalize:
            # SL-01: finalizing IS the selection — say so, because it is the last
            # reversible moment. SL-02: what happens to the rest depends on how the
            # vacancy hires, so do not promise the wrong one.
            if vacancy_uses_cos_selection_rounds(entry):
                messages.success(
                    request,
                    "COS deliberation pick finalized. The chosen applicant is recorded as "
                    "selected and routed to completion; the candidates this round did not "
                    "pick stay in the pool for the next round.",
                )
            else:
                messages.success(
                    request,
                    "COS deliberation pick finalized. The chosen applicant is recorded as "
                    "selected and routed to completion; the rest of the deliberated pool "
                    "is closed as not selected and notified.",
                )
            return redirect("vacancy-batches")
        messages.success(request, "COS deliberation pick saved.")
        return redirect("vacancy-batch-cos-deliberation", pk=entry.pk)


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

        form = CaseHandoffForm(request.POST, application=application, user=request.user)
        if form.is_valid():
            target_role = form.cleaned_data["target_role"]
            try:
                route_case_between_secretariat_and_hrm_chief(
                    application=application,
                    actor=request.user,
                    target_role=target_role,
                    remarks=form.cleaned_data["remarks"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                recipient = (
                    "HRM Chief"
                    if target_role == RecruitmentUser.Role.HRM_CHIEF
                    else "Secretariat"
                )
                messages.success(request, f"Case sent to {recipient}.")
        else:
            messages.error(request, "Add remarks before sending the case.")

        application.refresh_from_db()
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


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


class InterviewSessionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        # RD-4: one door. A pooled vacancy's interview session is scheduled and
        # finalized once for the whole pool in the batch interview console, so
        # this per-case endpoint refuses writes server-side (PT-03) — a forced
        # POST cannot break the pool's lock-step or send a conflicting notice.
        if vacancy_uses_batch_exam(application.position):
            messages.error(
                request,
                "This vacancy's interview is scheduled and finalized for the whole "
                "pool in the batch interview console.",
            )
            return redirect("vacancy-batch-interview", pk=application.position.pk)

        operation = request.POST.get("operation", "save")
        is_autosave = _is_autosave_request(request) and operation != "finalize"
        notify_applicant = operation == "notify_applicant"
        notify_panel = operation == "notify_panel"
        form = InterviewSessionForm(
            request.POST,
            instance=get_interview_session(application),
        )
        if form.is_valid():
            try:
                interview_session = save_interview_session(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                    notify_applicant=notify_applicant,
                    notify_panel=notify_panel,
                )
            except (ValueError, ValidationError) as exc:
                if is_autosave:
                    return _autosave_response(False)
                messages.error(request, str(exc))
            else:
                if is_autosave:
                    return _autosave_response()
                if interview_session.is_finalized:
                    messages.success(request, "Interview session finalized and locked.")
                elif notify_applicant:
                    messages.success(
                        request, "Interview schedule saved and the applicant was notified."
                    )
                elif notify_panel:
                    messages.success(
                        request, "Interview schedule saved and the HRMPSB panel was notified."
                    )
                else:
                    messages.success(request, "Interview schedule saved.")
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(
                request,
                _format_form_errors(
                    form,
                    "Complete the required interview scheduling fields before saving.",
                ),
            )
        return redirect("application-detail", pk=pk)


class InterviewRatingSheetView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    template_name = "recruitment/interview_rating_sheet.html"

    def _load_application(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        return application

    def _context(self, application, template, template_form=None, formset=None):
        return {
            "application": application,
            "entry": application.position,
            "rating_sheet": template,
            "template_form": template_form,
            "competency_formset": formset,
        }

    def get(self, request, pk):
        application = self._load_application(request, pk)
        template = get_competency_rating_template(application.position)
        template_form = formset = None
        if template:
            template_form = CompetencyRatingTemplateForm(instance=template)
            formset = CompetencyDefinitionFormSet(instance=template)
        return render(
            request,
            self.template_name,
            self._context(application, template, template_form, formset),
        )

    def post(self, request, pk):
        application = self._load_application(request, pk)
        entry = application.position
        template = get_competency_rating_template(entry)
        operation = request.POST.get("operation", "save")

        if operation == "create":
            if template:
                messages.info(request, "This vacancy already has an interview rating sheet.")
            else:
                try:
                    create_competency_rating_template(entry, request.user)
                    messages.success(
                        request,
                        "Rating sheet created from the standard template. "
                        "Add the Technical competencies below.",
                    )
                except (ValueError, ValidationError) as exc:
                    messages.error(request, str(exc))
            return redirect("interview-rating-sheet", pk=pk)

        if not template:
            messages.error(request, "Create the interview rating sheet first.")
            return redirect("interview-rating-sheet", pk=pk)
        if template.is_locked:
            messages.error(request, "This rating sheet is locked because scoring has started.")
            return redirect("interview-rating-sheet", pk=pk)

        template_form = CompetencyRatingTemplateForm(request.POST, instance=template)
        formset = CompetencyDefinitionFormSet(request.POST, instance=template)
        if template_form.is_valid() and formset.is_valid():
            try:
                save_competency_rating_sheet(
                    template, template_form, formset, publish=operation == "publish"
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                if operation == "publish":
                    messages.success(
                        request,
                        "Rating sheet saved and made available to the HRMPSB panel.",
                    )
                else:
                    messages.success(request, "Rating sheet saved.")
                return redirect("interview-rating-sheet", pk=pk)
        else:
            messages.error(request, "Fix the highlighted problems on the rating sheet.")
        return render(
            request,
            self.template_name,
            self._context(application, template, template_form, formset),
        )


class InterviewRatingSheetPrintView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """REV-E: print-tailored competency rating sheet for the live interview.
    Shows candidate code AND name — the REV-B face-to-face exception: correct
    attribution outranks symbolic blinding in the room."""

    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_view_application(request.user, application):
            raise PermissionDenied
        rating_template = get_published_competency_rating_template(application.position)
        competencies = list(rating_template.competencies.all()) if rating_template else []
        # Mirror the office form (DOH-RO4A-MPERIF): competencies presented in
        # CORE / ORGANIZATIONAL / TECHNICAL sections with a total-score row.
        grouped = []
        for group_value, group_label in CompetencyDefinition.Group.choices:
            members = [c for c in competencies if c.group == group_value]
            if members:
                grouped.append({"label": group_label, "competencies": members})
        total_weight = sum((c.weight or 0) for c in competencies)
        return render(
            request,
            "recruitment/interview_rating_sheet_print.html",
            {
                "application": application,
                "entry": application.position,
                "rating_template": rating_template,
                "competency_groups": grouped,
                "competency_total_weight": total_weight,
                "interview_session": get_interview_session(application),
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


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
                "weights": application.position.assessment_weights_or_default,
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
        weights = entry.assessment_weights_or_default
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


class VacancyAssessmentWeightsView(LoginRequiredMixin, EntryManagerRequiredMixin, View):
    template_name = "recruitment/vacancy_assessment_weights.html"

    def _get_entry(self, pk):
        # RD-5: both branches use the weights now — post-RD-2 the COS exam
        # overall is computed from General/Technical exactly like Plantilla,
        # so COS entries edit their weights here too.
        return get_object_or_404(PositionPosting, pk=pk)

    def _context(self, entry, weights, form, unlock_form=None):
        return {
            "entry": entry,
            "weights": weights,
            "form": form,
            "is_locked": weights.is_locked,
            "unlock_form": unlock_form or VacancyAssessmentWeightsUnlockForm(),
        }

    def get(self, request, pk):
        entry = self._get_entry(pk)
        weights = get_or_create_vacancy_assessment_weights(entry)
        form = VacancyAssessmentWeightsForm(instance=weights)
        return render(request, self.template_name, self._context(entry, weights, form))

    def _unlock(self, request, entry, weights):
        """SL-00: reopen a locked split for correction, with a mandatory audited reason."""
        unlock_form = VacancyAssessmentWeightsUnlockForm(request.POST)
        if unlock_form.is_valid():
            try:
                unlock_vacancy_assessment_weights(
                    entry,
                    request.user,
                    unlock_form.cleaned_data["reason"],
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    "Assessment weights unlocked for correction. The reason is recorded in "
                    "the audit trail.",
                )
                return redirect("vacancy-assessment-weights", pk=entry.pk)
        else:
            messages.error(request, "Record why the assessment weights are being unlocked.")
        form = VacancyAssessmentWeightsForm(instance=weights)
        return render(
            request,
            self.template_name,
            self._context(entry, weights, form, unlock_form=unlock_form),
        )

    def post(self, request, pk):
        entry = self._get_entry(pk)
        weights = get_or_create_vacancy_assessment_weights(entry)
        if request.POST.get("operation") == "unlock":
            return self._unlock(request, entry, weights)
        if weights.is_locked:
            messages.error(
                request,
                "Assessment weights are locked because scoring has started for this vacancy. "
                "Unlock them for correction if the split was encoded wrongly.",
            )
            return redirect("vacancy-assessment-weights", pk=entry.pk)
        form = VacancyAssessmentWeightsForm(request.POST, instance=weights)
        if form.is_valid():
            try:
                update_vacancy_assessment_weights(entry, form, request.user)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Assessment weights updated.")
                return redirect("vacancy-assessment-weights", pk=entry.pk)
        else:
            messages.error(request, "Fix the highlighted problems before saving.")
        return render(request, self.template_name, self._context(entry, weights, form))


class InterviewRatingView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_rating(request.user, application):
            raise PermissionDenied

        # RD-4: a pooled COS vacancy's single interview score is recorded in the
        # batch console, so this per-case endpoint refuses it server-side. The
        # Plantilla per-rater competency sheet stays per-case (each rater's own
        # sheet — the console cannot host N raters' parallel entries), and a
        # continuous-COS vacancy keeps its per-case simple score.
        if (
            application.branch == PositionPosting.Branch.COS
            and vacancy_uses_batch_exam(application.position)
        ):
            messages.error(
                request,
                "This vacancy's interview scores are recorded in the batch "
                "interview console, where the whole pool is scored together.",
            )
            return redirect("vacancy-batch-interview", pk=application.position.pk)

        if application.branch == PositionPosting.Branch.COS:
            # COS interviews use a simple end-user / HRMS score, not the sheet (spec §6.2).
            form = SimpleInterviewRatingForm(request.POST)
        else:
            rating_template = get_published_competency_rating_template(application.position)
            if rating_template is None:
                messages.error(
                    request,
                    "Publish the interview rating sheet for this vacancy before recording ratings.",
                )
                return redirect("application-detail", pk=pk)

            form = InterviewRatingForm(
                request.POST,
                application=application,
                actor=request.user,
                template=rating_template,
            )
        if form.is_valid():
            try:
                save_interview_rating(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Interview rating saved.")
        else:
            messages.error(request, "Complete the required interview rating fields before saving.")
        return redirect("application-detail", pk=pk)


class InterviewAbsenceView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-06: record that the candidate did not attend their scheduled interview."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_session(request.user, application):
            raise PermissionDenied
        form = InterviewAbsenceForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record what happened at the scheduled interview.")
            return redirect("application-detail", pk=pk)
        try:
            mark_interview_absent(
                application=application,
                actor=request.user,
                notes=form.cleaned_data["notes"],
            )
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                "Absence recorded. No score was written — file the applicant's letter of "
                "intent if one arrives, then decide whether to reschedule.",
            )
        return redirect("application-detail", pk=pk)


class FinalSelectionRescindView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-14: void a recorded final selection so the vacancy can be decided again."""

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        selection = get_final_selection_for_application(application)
        if not user_can_rescind_final_selection(request.user, selection):
            raise PermissionDenied
        form = FinalSelectionRescindForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record why this selection is being rescinded.")
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
                f"Selection rescinded. The original record is preserved, and "
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
            messages.error(request, "Record why this COS deliberation pick is being rescinded.")
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
        else:
            messages.success(
                request,
                f"COS deliberation pick rescinded. The original pick is preserved, and "
                f"{len(restored)} case(s) are back at the HRM Chief's deliberation step so "
                "a replacement pick can be recorded.",
            )
        # The case may have left this user's view on the way in or out, so check retained
        # access before sending them back to it (the WorkflowActionView pattern).
        if user_can_view_application(request.user, application):
            return redirect("application-detail", pk=pk)
        return redirect("workflow-queue")


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


class AttendanceExceptionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    """SL-06: file the letter of intent against a recorded absence, or decide it."""

    def post(self, request, pk, exception_pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        exception = get_object_or_404(
            AttendanceException, pk=exception_pk, application=application
        )
        if not user_can_process_application(request.user, application):
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


class InterviewFallbackUploadView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_interview_fallback(request.user, application):
            raise PermissionDenied

        form = InterviewFallbackUploadForm(
            request.POST,
            request.FILES,
            panel_members=interview_fallback_panel_queryset(application),
        )
        if form.is_valid():
            try:
                upload_interview_fallback_rating(
                    application=application,
                    actor=request.user,
                    uploaded_file=form.cleaned_data["file"],
                    remarks=form.cleaned_data["remarks"],
                    rated_by=form.cleaned_data["rated_by"],
                    rating_score=form.cleaned_data["rating_score"],
                    justification=form.cleaned_data.get("justification", ""),
                )
            except (ValueError, ValidationError) as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(
                    request,
                    "Paper rating sheet encoded — the panel member and their score are on "
                    "record, and the scan is retained with the secured files.",
                )
        else:
            messages.error(
                request,
                "Name the panel member, record their score, and attach the scanned sheet.",
            )
        return redirect("application-detail", pk=pk)


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
                    messages.success(request, "Comparative Assessment Report finalized and locked.")
                else:
                    messages.success(request, "CAR draft prepared with the current ratings.")
        else:
            if is_autosave:
                return _autosave_response(False)
            messages.error(request, "Provide the CAR notes before generating the report.")
        application.refresh_from_db()
        if not user_can_view_application(request.user, application):
            return redirect("workflow-queue")
        return redirect("application-detail", pk=pk)


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
                    "Choose the selected appointee and provide decision remarks.",
                ),
            )


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


class RequirementChecklistNotificationView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and user_can_send_requirement_checklist_notification(request.user, application)
        ):
            raise PermissionDenied

        form = RequirementChecklistNotificationForm(request.POST)
        if form.is_valid():
            try:
                send_requirement_checklist_notification(
                    application=application,
                    actor=request.user,
                    additional_message=form.cleaned_data["additional_message"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Requirement checklist email queued for delivery.",
                )
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("application-detail", pk=pk)


class ReminderNotificationView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and user_can_send_reminder_notification(request.user, application)
        ):
            raise PermissionDenied

        form = ReminderNotificationForm(request.POST)
        if form.is_valid():
            try:
                send_reminder_notification(
                    application=application,
                    actor=request.user,
                    reminder_subject=form.cleaned_data["reminder_subject"],
                    reminder_message=form.cleaned_data["reminder_message"],
                    deadline=form.cleaned_data["deadline"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Reminder email queued for delivery.")
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("application-detail", pk=pk)


class NotificationResendView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    """RD-9: re-deliver a previously queued applicant email, verbatim. The one
    recovery path for a FAILED send (and for a lost email) — nothing retries
    automatically, and re-saving a schedule deliberately doesn't re-notify."""

    def post(self, request, pk, log_id):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and request.user.role in NOTIFICATION_MANAGER_ROLES
        ):
            raise PermissionDenied
        notification_log = get_object_or_404(
            NotificationLog, pk=log_id, application=application
        )
        try:
            resend_notification(notification_log, request.user)
        except (ValueError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{notification_log.get_notification_type_display()} queued for re-delivery.",
            )
        return redirect("application-detail", pk=pk)


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


class ExportApplicationBundleView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_export_application(request.user, application):
            record_export_denied(application, request.user, reason="unauthorized")
            raise PermissionDenied
        bundle = build_export_bundle(application, request.user)
        response = HttpResponse(bundle, content_type="application/zip")
        response["Content-Disposition"] = (
            f'attachment; filename="{application.reference_number}-export.zip"'
        )
        return response
