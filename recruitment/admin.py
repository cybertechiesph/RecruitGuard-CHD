from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    InterviewRating,
    InterviewSession,
    NotificationLog,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
    WorkflowOverride,
)


def _superuser_only_admin_site(request):
    return bool(request.user.is_active and request.user.is_superuser)


admin.site.has_permission = _superuser_only_admin_site


@admin.register(RecruitmentUser)
class RecruitmentUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            "RecruitGuard",
            {"fields": ("role", "office_name", "employee_id")},
        ),
    )
    list_display = ("username", "email", "role", "office_name", "is_active")
    list_filter = ("role", "is_active", "is_staff")


@admin.register(PositionReference)
class PositionReferenceAdmin(admin.ModelAdmin):
    list_display = (
        "position_title",
        "salary_grade",
        "level_classification",
        "class_id",
        "reference_status",
        "is_active",
    )
    list_filter = ("level_classification", "reference_status", "is_active")
    search_fields = (
        "position_title",
        "position_slug",
        "position_code",
        "class_id",
        "os_code",
        "occupational_service",
        "occupational_group",
    )


@admin.register(PositionPosting)
class PositionPostingAdmin(admin.ModelAdmin):
    list_display = (
        "job_code",
        "title",
        "branch",
        "intake_mode",
        "level",
        "position_reference",
        "status",
        "opening_date",
        "closing_date",
    )
    list_filter = ("branch", "intake_mode", "level", "status")
    search_fields = (
        "job_code",
        "title",
        "unit",
        "position_reference__position_title",
        "position_reference__class_id",
        "position_reference__os_code",
    )
    readonly_fields = ("job_code",)
    inlines = []


class EvidenceVaultItemInline(admin.TabularInline):
    model = EvidenceVaultItem
    extra = 0
    readonly_fields = (
        "label",
        "stage",
        "version_family",
        "version_number",
        "is_current_version",
        "archive_tag",
        "is_archived",
        "original_filename",
        "digest_algorithm",
        "sha256_digest",
        "size_bytes",
        "uploaded_by",
        "uploaded_by_role",
        "archived_by",
        "archived_by_role",
        "archived_at",
        "created_at",
    )
    can_delete = False


class AuditLogInline(admin.TabularInline):
    model = AuditLog
    extra = 0
    readonly_fields = (
        "created_at",
        "actor",
        "actor_role",
        "case_reference",
        "workflow_stage",
        "action",
        "is_sensitive_access",
        "description",
        "metadata",
    )
    can_delete = False


class RoutingHistoryInline(admin.TabularInline):
    model = RoutingHistory
    extra = 0
    readonly_fields = (
        "created_at",
        "route_type",
        "from_handler_role",
        "to_handler_role",
        "from_stage",
        "to_stage",
        "description",
        "notes",
        "is_override",
    )
    can_delete = False


class ScreeningRecordInline(admin.TabularInline):
    model = ScreeningRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "reviewed_by",
        "reviewed_by_role",
        "completeness_status",
        "completeness_notes",
        "qualification_outcome",
        "screening_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class ExamRecordInline(admin.TabularInline):
    model = ExamRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "recorded_by",
        "recorded_by_role",
        "exam_type",
        "exam_status",
        "exam_score",
        "exam_result",
        "valid_from",
        "valid_until",
        "exam_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class InterviewSessionInline(admin.TabularInline):
    model = InterviewSession
    extra = 0
    readonly_fields = (
        "review_stage",
        "scheduled_by",
        "scheduled_by_role",
        "scheduled_for",
        "location",
        "session_notes",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class InterviewRatingInline(admin.TabularInline):
    model = InterviewRating
    extra = 0
    readonly_fields = (
        "review_stage",
        "rated_by",
        "rated_by_role",
        "rating_score",
        "rating_notes",
        "justification",
        "created_at",
    )
    can_delete = False


class DeliberationRecordInline(admin.TabularInline):
    model = DeliberationRecord
    extra = 0
    readonly_fields = (
        "review_stage",
        "recorded_by",
        "recorded_by_role",
        "deliberated_at",
        "deliberation_minutes",
        "decision_support_summary",
        "ranking_position",
        "ranking_notes",
        "consolidated_snapshot",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class ComparativeAssessmentReportInline(admin.TabularInline):
    model = ComparativeAssessmentReport
    extra = 0
    readonly_fields = (
        "review_stage",
        "recruitment_entry",
        "generated_by",
        "generated_by_role",
        "summary_notes",
        "version_number",
        "evidence_item",
        "is_finalized",
        "finalized_by",
        "finalized_at",
        "created_at",
    )
    can_delete = False


class FinalDecisionInline(admin.TabularInline):
    model = FinalDecision
    extra = 0
    readonly_fields = (
        "review_stage",
        "decided_by",
        "decided_by_role",
        "decision_outcome",
        "decision_notes",
        "submission_packet_snapshot",
        "decided_at",
        "created_at",
    )
    can_delete = False


PositionPostingAdmin.inlines = [ComparativeAssessmentReportInline]


class NotificationLogInline(admin.TabularInline):
    model = NotificationLog
    extra = 0
    readonly_fields = (
        "created_at",
        "notification_type",
        "delivery_channel",
        "delivery_status",
        "recipient_email",
        "subject",
        "triggered_by",
        "triggered_by_role",
        "sent_at",
        "failure_details",
        "metadata",
    )
    can_delete = False


class CompletionRecordInline(admin.TabularInline):
    model = CompletionRecord
    extra = 0
    readonly_fields = (
        "tracked_by",
        "tracked_by_role",
        "completion_reference",
        "completion_date",
        "deadline",
        "announcement_reference",
        "announcement_date",
        "remarks",
        "created_at",
        "updated_at",
    )
    can_delete = False


@admin.register(RecruitmentApplication)
class RecruitmentApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "reference_number",
        "applicant_display_name",
        "applicant_email",
        "position",
        "branch",
        "level",
        "status",
        "current_handler_role",
    )
    list_filter = ("branch", "level", "status", "current_handler_role")
    search_fields = (
        "reference_number",
        "applicant__username",
        "applicant_first_name",
        "applicant_last_name",
        "applicant_email",
        "position__title",
    )
    inlines = [
        EvidenceVaultItemInline,
        ScreeningRecordInline,
        ExamRecordInline,
        InterviewSessionInline,
        InterviewRatingInline,
        DeliberationRecordInline,
        FinalDecisionInline,
        CompletionRecordInline,
        NotificationLogInline,
        RoutingHistoryInline,
        AuditLogInline,
    ]


@admin.register(RecruitmentCase)
class RecruitmentCaseAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "branch",
        "current_stage",
        "case_status",
        "current_handler_role",
        "is_stage_locked",
    )
    list_filter = ("branch", "current_stage", "case_status", "is_stage_locked")
    search_fields = ("application__reference_number", "application__position__title")


@admin.register(WorkflowOverride)
class WorkflowOverrideAdmin(admin.ModelAdmin):
    list_display = ("application", "target_role", "granted_by", "is_active", "created_at", "used_at")
    list_filter = ("target_role", "is_active")
    search_fields = ("application__reference_number", "granted_by__username")


@admin.register(EvidenceVaultItem)
class EvidenceVaultItemAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "artifact_scope",
        "artifact_type",
        "owner_reference",
        "stage",
        "version_number",
        "is_current_version",
        "is_archived",
        "original_filename",
        "size_bytes",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("artifact_scope", "artifact_type", "stage", "is_current_version", "is_archived", "uploaded_by_role")
    search_fields = (
        "application__reference_number",
        "recruitment_case__application__reference_number",
        "recruitment_entry__job_code",
        "recruitment_entry__title",
        "label",
        "original_filename",
        "sha256_digest",
        "archive_tag",
        "uploaded_by__username",
    )
    readonly_fields = (
        "application",
        "recruitment_case",
        "recruitment_entry",
        "artifact_scope",
        "artifact_type",
        "stage",
        "document_key",
        "version_family",
        "version_number",
        "previous_version",
        "is_current_version",
        "digest_algorithm",
        "sha256_digest",
        "uploaded_by_role",
        "archived_by_role",
        "nonce",
        "ciphertext",
    )

    @admin.display(description="Owner")
    def owner_reference(self, obj):
        if obj.application_id:
            return obj.application.reference_number
        if obj.recruitment_case_id:
            return f"{obj.recruitment_case.application.reference_number} / case #{obj.recruitment_case_id}"
        if obj.recruitment_entry_id:
            return f"{obj.recruitment_entry.job_code} / {obj.recruitment_entry.title}"
        return "-"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "case_reference",
        "workflow_stage",
        "action",
        "actor",
        "actor_role",
        "is_sensitive_access",
        "created_at",
    )
    list_filter = ("action", "actor_role", "workflow_stage", "is_sensitive_access")
    search_fields = ("case_reference", "description", "actor__username", "workflow_stage")

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "notification_type",
        "delivery_channel",
        "delivery_status",
        "recipient_email",
        "triggered_by",
        "sent_at",
        "created_at",
    )
    list_filter = ("notification_type", "delivery_channel", "delivery_status")
    search_fields = (
        "application__reference_number",
        "recipient_email",
        "subject",
        "body",
        "triggered_by__username",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "triggered_by_role",
        "sent_at",
        "failure_details",
        "metadata",
    )


@admin.register(RoutingHistory)
class RoutingHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "route_type",
        "from_handler_role",
        "to_handler_role",
        "branch",
        "level",
        "created_at",
    )
    list_filter = ("route_type", "branch", "level", "is_override", "to_handler_role")
    search_fields = ("application__reference_number", "description", "notes", "actor__username")
    readonly_fields = ("created_at",)


@admin.register(ScreeningRecord)
class ScreeningRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "reviewed_by",
        "completeness_status",
        "qualification_outcome",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "completeness_status", "qualification_outcome", "is_finalized")
    search_fields = ("application__reference_number", "completeness_notes", "screening_notes", "reviewed_by__username")
    readonly_fields = ("created_at", "updated_at", "reviewed_by_role", "finalized_by_role")


@admin.register(ExamRecord)
class ExamRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "review_stage",
        "recorded_by",
        "exam_type",
        "exam_status",
        "exam_score",
        "is_finalized",
        "finalized_at",
    )
    list_filter = ("review_stage", "branch", "level", "exam_status", "is_finalized")
    search_fields = ("application__reference_number", "exam_type", "exam_result", "exam_notes", "recorded_by__username")
    readonly_fields = ("created_at", "updated_at", "recorded_by_role", "finalized_by_role")


class CompletionRequirementInline(admin.TabularInline):
    model = CompletionRequirement
    extra = 0


@admin.register(CompletionRecord)
class CompletionRecordAdmin(admin.ModelAdmin):
    list_display = (
        "application",
        "branch",
        "tracked_by",
        "completion_reference",
        "completion_date",
        "deadline",
        "updated_at",
    )
    list_filter = ("branch", "level", "tracked_by_role")
    search_fields = (
        "application__reference_number",
        "completion_reference",
        "announcement_reference",
        "remarks",
        "tracked_by__username",
    )
    readonly_fields = ("created_at", "updated_at", "tracked_by_role")
    inlines = [CompletionRequirementInline]


@admin.register(CompletionRequirement)
class CompletionRequirementAdmin(admin.ModelAdmin):
    list_display = (
        "completion_record",
        "item_label",
        "status",
        "display_order",
        "updated_at",
    )
    list_filter = ("status",)
    search_fields = ("completion_record__application__reference_number", "item_label", "notes")
