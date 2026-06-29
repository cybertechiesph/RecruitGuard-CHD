from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from .forms import (
    AssessmentWeightConfigForm,
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
    FinalDecisionForm,
    FinalSelectionForm,
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    CompetencyDefinitionFormSet,
    CompetencyRatingTemplateForm,
    InterviewSessionForm,
    ReminderNotificationForm,
    RequirementChecklistNotificationForm,
    ScreeningReviewForm,
    WorkflowActionForm,
    WorkflowOverrideForm,
    WorkflowReopenForm,
)
from .models import (
    AuditLog,
    CompletionRecord,
    EvidenceVaultItem,
    Notification,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
)
from .notification_services import (
    get_recent_notifications,
    get_unread_count,
    mark_all_notifications_read,
    mark_notification_read,
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
)
from .services import (
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    apply_car_ete_ratings,
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
    get_completion_record,
    get_completion_requirements,
    decrypt_evidence_bytes,
    evidence_belongs_to_application_context,
    get_deliberation_record,
    get_deliberation_records,
    get_evidence_context_application_for_user,
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
    get_system_audit_logs,
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
    get_assessment_weight_config,
    update_assessment_weights,
    user_can_manage_interview_session,
    user_can_manage_screening,
    user_can_process_application,
    user_can_record_final_decision,
    user_can_record_final_selection,
    user_can_reopen_case,
    user_can_upload_interview_fallback,
    user_can_upload_evidence,
    user_can_view_application,
    user_is_interview_rating_support_encoder,
)


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
        # Workflow roles land directly on their queue — that is their home.
        # Only System Admin sees the dashboard (a distinct identity overview).
        if request.user.role != RecruitmentUser.Role.SYSTEM_ADMIN:
            return redirect("workflow-queue")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["positions"] = get_visible_positions_for_user(user)[:6]
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

    def get_queryset(self):
        return get_visible_positions_for_user(self.request.user)


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
                if r.status == ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
            )
            context["screening_absent_count"] = sum(
                1 for r in _reviews
                if r.status == ScreeningDocumentReview.ReviewStatus.ABSENT
            )
        context["applicant_document_review_items"] = get_applicant_document_review_items(application)
        context["exam_records"] = get_exam_records(application)
        context["current_exam_record"] = get_exam_record(application)
        context["interview_sessions"] = get_interview_sessions(application)
        context["current_interview_session"] = get_interview_session(application)
        context["current_interview_ratings"] = get_interview_ratings(application)
        context["current_interview_fallback_evidence"] = get_interview_fallback_evidence(application)
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
            item.applicant_display_name = item.application.applicant_display_name
        context["current_comparative_assessment_report_items"] = comparative_assessment_items
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
        if user_can_manage_exam(user, application):
            exam_record = context["current_exam_record"]
            exam_schedule = get_exam_schedule(application)
            context["current_exam_schedule"] = exam_schedule
            if exam_record and exam_record.is_finalized:
                context["exam_locked"] = True
            else:
                context["exam_form"] = ExamRecordForm(
                    instance=exam_record,
                    application=application,
                )
                context["exam_schedule_form"] = ExamScheduleForm(instance=exam_schedule)
        if user_can_manage_interview_session(user, application):
            interview_session = context["current_interview_session"]
            if interview_session and interview_session.is_finalized:
                context["interview_session_locked"] = True
            else:
                context["interview_session_form"] = InterviewSessionForm(instance=interview_session)
        if user_can_manage_interview_rating(user, application):
            interview_session = context["current_interview_session"]
            if not interview_session:
                context["interview_rating_requires_session"] = True
            elif interview_session.is_finalized:
                context["interview_rating_locked"] = True
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
                context["interview_fallback_form"] = InterviewFallbackUploadForm()
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
        if not applicant_pool_is_blocked and user_can_manage_comparative_assessment_report(user, application):
            report = context["current_comparative_assessment_report"]
            if report and report.is_finalized:
                context["car_locked"] = True
            else:
                context["car_form"] = ComparativeAssessmentReportForm(instance=report)
        if user_can_record_final_decision(user, application):
            context["final_decision_form"] = FinalDecisionForm()
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
                            status=ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                        ).order_by("display_order", "created_at")
                    )
        if get_case_handoff_options(application, user):
            context["case_handoff_form"] = CaseHandoffForm(application=application, user=user)
        if context["recruitment_case"] and user_can_reopen_case(user, context["recruitment_case"]):
            context["reopen_form"] = WorkflowReopenForm()
        if user_can_send_requirement_checklist_notification(user, application):
            context["checklist_notification_form"] = RequirementChecklistNotificationForm()
        if user_can_send_reminder_notification(user, application):
            context["reminder_notification_form"] = ReminderNotificationForm()
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
        if self.search_form.is_valid():
            cleaned_data = self.search_form.cleaned_data
        else:
            cleaned_data = {
                "q": "",
                "action": "",
                "actor_role": "",
                "sensitive_only": False,
            }
        audit_logs = list(
            get_application_audit_logs(
                application,
                search_query=cleaned_data["q"],
                action=cleaned_data["action"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
            )
        )
        context["search_form"] = self.search_form
        context["audit_logs"] = audit_logs
        context["result_count"] = len(audit_logs)
        context["review_scope"] = "application"
        context["recruitment_case"] = getattr(application, "case", None)
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
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        if self.search_form.is_valid():
            cleaned_data = self.search_form.cleaned_data
        else:
            cleaned_data = {
                "q": "",
                "action": "",
                "actor_role": "",
                "sensitive_only": False,
            }
        audit_logs = list(
            get_system_audit_logs(
                search_query=cleaned_data["q"],
                action=cleaned_data["action"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
            )
        )
        context["search_form"] = self.search_form
        context["audit_logs"] = audit_logs
        context["result_count"] = len(audit_logs)
        context["review_scope"] = "system"
        record_audit_log_review(
            actor=self.request.user,
            search_query=cleaned_data["q"],
            action=cleaned_data["action"],
            actor_role=cleaned_data["actor_role"],
            sensitive_only=cleaned_data["sensitive_only"],
            result_count=len(audit_logs),
        )
        return context


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
            evidence.context_application = get_evidence_context_application_for_user(
                self.request.user,
                evidence,
            )
        context["evidence_items"] = evidence_items
        context["search_form"] = self.search_form
        context["result_count"] = len(evidence_items)
        # Include recent audit logs for the combined Evidence & Audit view
        context["recent_audit_logs"] = list(
            get_system_audit_logs(
                search_query="",
                action="",
                actor_role="",
                sensitive_only=False,
            )[:50]
        )
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

    def get_queryset(self):
        return get_queue_for_user(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = True
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
        form = ScreeningReviewForm(request.POST, application=application)
        if form.is_valid():
            try:
                screening_record = save_screening_review(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                    finalize=operation == "finalize",
                )
            except ValueError as exc:
                if is_autosave:
                    return _autosave_response(False)
                messages.error(request, str(exc))
            else:
                if is_autosave:
                    return _autosave_response()
                if screening_record.is_finalized:
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


class AssessmentWeightConfigView(LoginRequiredMixin, EntryManagerRequiredMixin, View):
    template_name = "recruitment/assessment_weights.html"

    def get(self, request):
        config = get_assessment_weight_config()
        form = AssessmentWeightConfigForm(instance=config)
        return render(request, self.template_name, {"config": config, "form": form})

    def post(self, request):
        config = get_assessment_weight_config()
        form = AssessmentWeightConfigForm(request.POST, instance=config)
        if form.is_valid():
            try:
                update_assessment_weights(request.user, form)
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Assessment weights updated.")
                return redirect("assessment-weights")
        else:
            messages.error(request, "Fix the highlighted problems before saving.")
        return render(request, self.template_name, {"config": config, "form": form})


class InterviewRatingView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_manage_interview_rating(request.user, application):
            raise PermissionDenied

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


class InterviewFallbackUploadView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_upload_interview_fallback(request.user, application):
            raise PermissionDenied

        form = InterviewFallbackUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                upload_interview_fallback_rating(
                    application=application,
                    actor=request.user,
                    uploaded_file=form.cleaned_data["file"],
                    remarks=form.cleaned_data["remarks"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Fallback rating sheet saved with secured files.")
        else:
            messages.error(request, "Provide the scanned fallback rating sheet and upload remarks.")
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
        if not user_can_manage_comparative_assessment_report(request.user, application):
            raise PermissionDenied

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


class FinalDecisionView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, View):
    def _render_with_bound_form(self, request, application, form, message):
        if not form.errors:
            form.add_error("decision_outcome", message)
        messages.error(request, message)
        return _render_application_detail_with_overrides(
            request,
            application,
            final_decision_form=form,
        )

    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not user_can_record_final_decision(request.user, application):
            raise PermissionDenied

        form = FinalDecisionForm(request.POST)
        if form.is_valid():
            try:
                decision = record_final_decision(
                    application=application,
                    actor=request.user,
                    cleaned_data=form.cleaned_data,
                )
            except (ValueError, ValidationError) as exc:
                return self._render_with_bound_form(
                    request,
                    application,
                    form,
                    str(exc),
                )
            else:
                messages.success(
                    request,
                    "Final decision recorded as "
                    f"{decision.get_decision_outcome_display().lower()}.",
                )
        else:
            return self._render_with_bound_form(
                request,
                application,
                form,
                "Choose the final outcome and provide the decision remarks.",
            )
        return redirect("application-detail", pk=pk)


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
                    checklist_items=form.cleaned_data["checklist_items"],
                    deadline=form.cleaned_data["deadline"],
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
