"""Internal-portal views, one module per area.

Every name the old recruitment/views.py defined is re-exported here, so
`from recruitment.views import ScreeningReviewView` (internal_urls.py) keeps working.
The modules depend only on `common` (shared helpers) and `case` (the case page), so they
import those directly; public_names.txt freezes the names re-exported here.
"""
from .common import (
    logger,
    EXAM_FIELD_LABELS,
    _join_labels,
    _format_grouped_validation_messages,
    _format_form_errors,
    _format_validation_error,
    _safe_next_url,
    _is_autosave_request,
    AUDIT_REVIEW_LOG_WINDOW_SECONDS,
    _should_record_audit_review,
    _autosave_response,
    _add_validation_errors_to_form,
    _safe_internal_redirect,
)
from .home import DashboardView, ForbiddenView, SessionKeepAliveView, PositionListView
from .maintenance import MaintenanceView
from .references import (
    PositionReferenceListView,
    PositionReferenceCreateView,
    PositionReferenceUpdateView,
)
from .notifications import (
    NotificationListView,
    NotificationReadView,
    NotificationReadAllView,
    NotificationUnreadCountView,
    RequirementChecklistNotificationView,
    ReminderNotificationView,
    NotificationResendView,
)
from .lists import (
    WORKFLOW_LIST_STEP_CHOICES,
    _apply_workflow_list_search,
    _workflow_list_object_list,
    _add_workflow_list_search_context,
    ApplicationListView,
    WorkflowQueueView,
    ClosedCaseListView,
    _apply_closed_case_filters,
    ScheduleCalendarView,
)
from .case import (
    _render_application_detail_with_overrides,
    ApplicationDetailView,
    WorkflowActionView,
    CaseHandoffView,
    ResubmissionDeadlineExtensionView,
    ApplicationWithdrawalView,
    ApplicantContactUpdateView,
    WorkflowOverrideView,
    WorkflowReopenView,
)
from .audit import (
    ApplicationAuditLogView,
    AuditLogListView,
    _audit_search_cleaned_data,
    _filtered_audit_logs_from_request,
    _describe_audit_filters,
    AuditLogExportCsvView,
    AuditLogReportView,
    AuditLogArchiveView,
    AuditArchiveDownloadView,
)
from .evidence import (
    EvidenceUploadView,
    EvidenceDownloadView,
    EvidenceArchiveToggleView,
    EvidenceVaultListView,
    ExportApplicationBundleView,
)
from .batches import (
    VacancyBatchConsoleView,
    VacancyBatchDetailView,
    _batch_pool_empty_redirect,
    VacancyBatchExamView,
    VacancyBatchInterviewView,
    _interview_finalize_next_step,
    VacancyBatchCosDeliberationView,
    VacancyRatingSheetView,
    VacancyBatchHandoffView,
)
from .screening import ScreeningReviewView
from .exam import ExaminationRecordView, ExamScheduleView, AttendanceExceptionView
from .interview import (
    InterviewSessionView,
    InterviewRatingSheetView,
    InterviewRatingSheetPrintView,
    InterviewRatingView,
    InterviewAbsenceView,
    InterviewFallbackUploadView,
)
from .car import (
    ComparativeAssessmentPrintView,
    ComparativeAssessmentFilingCopyView,
    DeliberationRecordView,
    ComparativeAssessmentReportView,
)
from .selection import (
    FinalSelectionRescindView,
    CosVacancyDeliberationRescindView,
    FinalSelectionView,
    NoAppointmentView,
)
from .completion import CompletionTrackingView, CaseClosureView
from .policy import VacancyAssessmentWeightsView, OfficePolicyView
