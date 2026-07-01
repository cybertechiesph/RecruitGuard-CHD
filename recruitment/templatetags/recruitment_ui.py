import json

from django import template

from recruitment.models import (
    audit_action_label as resolve_audit_action_label,
    AuditLog,
    CompletionRequirement,
    EvidenceVaultItem,
    ExamRecord,
    Notification,
    NotificationLog,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)
from recruitment.notification_services import (
    get_recent_notifications,
    get_unread_count,
)
from recruitment.services import (
    application_awaiting_car_preparation,
    get_current_workflow_section,
)

register = template.Library()


ROLE_LABELS = dict(RecruitmentUser.Role.choices)
STAGE_LABELS = dict(RecruitmentCase.Stage.choices)
STATUS_LABELS = {
    **dict(RecruitmentApplication.Status.choices),
    **dict(RecruitmentCase.CaseStatus.choices),
    **dict(PositionPosting.EntryStatus.choices),
    **dict(ScreeningRecord.CompletenessStatus.choices),
    **dict(ScreeningRecord.QualificationOutcome.choices),
    **dict(ExamRecord.ExamStatus.choices),
    **dict(CompletionRequirement.RequirementStatus.choices),
    **dict(NotificationLog.DeliveryStatus.choices),
}

STATUS_THEMES = {
    PositionPosting.EntryStatus.ACTIVE: "success",
    PositionPosting.EntryStatus.DRAFT: "neutral",
    PositionPosting.EntryStatus.SUSPENDED: "warning",
    PositionPosting.EntryStatus.CLOSED: "neutral",
    RecruitmentApplication.Status.DRAFT: "neutral",
    RecruitmentApplication.Status.SECRETARIAT_REVIEW: "info",
    RecruitmentApplication.Status.HRM_CHIEF_REVIEW: "info",
    RecruitmentApplication.Status.HRMPSB_REVIEW: "info",
    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: "info",
    RecruitmentApplication.Status.RETURNED_TO_APPLICANT: "warning",
    RecruitmentApplication.Status.APPROVED: "success",
    RecruitmentApplication.Status.REJECTED: "danger",
    RecruitmentApplication.Status.WITHDRAWN: "neutral",
    RecruitmentCase.CaseStatus.ACTIVE: "info",
    RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT: "warning",
    RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION: "warning",
    RecruitmentCase.CaseStatus.APPROVED: "success",
    RecruitmentCase.CaseStatus.REJECTED: "danger",
    ScreeningRecord.CompletenessStatus.COMPLETE: "success",
    ScreeningRecord.CompletenessStatus.INCOMPLETE: "warning",
    ScreeningRecord.QualificationOutcome.QUALIFIED: "success",
    ScreeningRecord.QualificationOutcome.NOT_QUALIFIED: "danger",
    ExamRecord.ExamStatus.COMPLETED: "success",
    ExamRecord.ExamStatus.WAIVED: "warning",
    ExamRecord.ExamStatus.ABSENT: "danger",
    CompletionRequirement.RequirementStatus.PENDING: "warning",
    CompletionRequirement.RequirementStatus.COMPLETED: "success",
    CompletionRequirement.RequirementStatus.NOT_APPLICABLE: "neutral",
    NotificationLog.DeliveryStatus.PENDING: "warning",
    NotificationLog.DeliveryStatus.SENT: "success",
    NotificationLog.DeliveryStatus.FAILED: "danger",
}

ROLE_THEMES = {
    RecruitmentUser.Role.APPLICANT: "applicant",
    RecruitmentUser.Role.SECRETARIAT: "secretariat",
    RecruitmentUser.Role.HRM_CHIEF: "hrm-chief",
    RecruitmentUser.Role.HRMPSB_MEMBER: "hrmpsb-member",
    RecruitmentUser.Role.APPOINTING_AUTHORITY: "appointing-authority",
    RecruitmentUser.Role.SYSTEM_ADMIN: "system-admin",
}


def _slug(value):
    return str(value).replace("_", "-").lower()


@register.filter
def role_label(value):
    if not value:
        return "Unassigned"
    return ROLE_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def stage_label(value):
    if not value:
        return "Not assigned"
    return STAGE_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def status_label(value):
    if not value:
        return "Not recorded"
    return STATUS_LABELS.get(value, str(value).replace("_", " ").title())


@register.filter
def audit_action_label(value):
    return resolve_audit_action_label(value)


# Audit-detail rendering: a human-readable summary instead of a raw JSON dump.
AUDIT_REVIEW_ACTIONS = {
    AuditLog.Action.EVIDENCE_VAULT_VIEWED,
    AuditLog.Action.AUDIT_LOG_VIEWED,
    AuditLog.Action.PROTECTED_RECORD_VIEWED,
}
# Internal identifiers / bookkeeping that mean nothing to a human reviewer.
AUDIT_METADATA_HIDDEN_KEYS = {
    "review_scope",
    "version_family",
    "evidence_id",
    "previous_version_id",
    "requested_application_id",
    "access_source",
    "document_key",
}
AUDIT_METADATA_LABELS = {
    "filename": "File",
    "stage": "Step",
    "artifact_scope": "File scope",
    "artifact_type": "File type",
    "version_number": "Version",
    "is_archived": "Archived",
    "reason": "Reason",
    "target_user_id": "Target user ID",
    "result_count": "Records shown",
    "sha256": "SHA-256",
    "to_stage": "Moved to step",
    "from_stage": "Moved from step",
}
AUDIT_METADATA_STAGE_KEYS = {
    "stage",
    "to_stage",
    "from_stage",
    "workflow_stage",
    "case_stage",
    "review_stage",
}
AUDIT_SCOPE_LABELS = dict(EvidenceVaultItem.OwnerScope.choices)


def _format_audit_metadata_value(key, value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if key in AUDIT_METADATA_STAGE_KEYS:
            return stage_label(text)
        if key == "artifact_scope":
            return AUDIT_SCOPE_LABELS.get(text, text.replace("_", " ").title())
        return text
    if isinstance(value, (list, dict)):
        return json.dumps(value) if value else ""
    return str(value)


def _summarize_audit_review_filters(metadata):
    applied = []
    search = (metadata.get("search_query") or "").strip()
    if search:
        applied.append(f"search “{search}”")
    stage = (metadata.get("stage") or "").strip()
    if stage:
        applied.append(f"step {stage_label(stage)}")
    scope = (metadata.get("artifact_scope") or "").strip()
    if scope:
        applied.append(f"scope {AUDIT_SCOPE_LABELS.get(scope, scope.replace('_', ' ').title())}")
    archival = (metadata.get("archival_status") or "").strip()
    if archival and archival != "active":
        applied.append(f"archive {archival.replace('_', ' ')}")
    action_filter = (metadata.get("action_filter") or "").strip()
    if action_filter:
        applied.append(f"action {resolve_audit_action_label(action_filter)}")
    role_filter = (metadata.get("actor_role_filter") or "").strip()
    if role_filter:
        applied.append(f"role {role_label(role_filter)}")
    if metadata.get("sensitive_only"):
        applied.append("sensitive records only")

    lines = []
    if applied:
        lines.append("Filtered by " + ", ".join(applied) + ".")
    else:
        lines.append("No filters applied.")
    if "result_count" in metadata:
        count = metadata.get("result_count") or 0
        lines.append(f"{count} record{'' if count == 1 else 's'} shown.")
    return lines


@register.filter
def audit_detail_summary(log):
    """Human-readable lines describing an audit record's metadata.

    Replaces the raw JSON dump: empty and purely-technical fields are dropped,
    keys and enum values are humanized, and oversight 'review' events are
    summarized by the filters that were applied.
    """
    metadata = getattr(log, "metadata", None)
    if not isinstance(metadata, dict) or not metadata:
        return []
    if getattr(log, "action", "") in AUDIT_REVIEW_ACTIONS:
        return _summarize_audit_review_filters(metadata)
    lines = []
    for key, value in metadata.items():
        if key in AUDIT_METADATA_HIDDEN_KEYS:
            continue
        text = _format_audit_metadata_value(key, value)
        if not text:
            continue
        label = AUDIT_METADATA_LABELS.get(key, key.replace("_", " ").capitalize())
        lines.append(f"{label}: {text}")
    return lines


@register.filter
def status_theme(value):
    return STATUS_THEMES.get(value, "neutral")


@register.filter
def branch_theme(value):
    if value == PositionPosting.Branch.PLANTILLA:
        return "plantilla"
    if value == PositionPosting.Branch.COS:
        return "cos"
    return "neutral"


@register.filter
def level_theme(value):
    if str(value) == str(PositionPosting.Level.LEVEL_1):
        return "level-1"
    if str(value) == str(PositionPosting.Level.LEVEL_2):
        return "level-2"
    return "neutral"


@register.filter
def role_theme(value):
    return ROLE_THEMES.get(value, _slug(value) if value else "neutral")


@register.filter
def pretty_json(value):
    if not value:
        return ""
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


@register.filter
def dom_id(value, prefix=""):
    return f"{prefix}{value}"


@register.filter
def roman_numeral(value):
    """Render small civil-service level numbers as Roman numerals (1 -> I, 2 -> II)."""
    numerals = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
    try:
        return numerals.get(int(value), str(value))
    except (TypeError, ValueError):
        return value


@register.simple_tag
def workflow_stages(branch):
    stages = [
        {
            "value": RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            "label": RecruitmentCase.Stage.SECRETARIAT_REVIEW.label,
            "short_label": "Secretariat",
        },
        {
            "value": RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
            "label": RecruitmentCase.Stage.HRM_CHIEF_REVIEW.label,
            "short_label": "HRM Chief",
        },
    ]
    if branch == PositionPosting.Branch.PLANTILLA:
        stages.append(
            {
                "value": RecruitmentCase.Stage.HRMPSB_REVIEW,
                "label": RecruitmentCase.Stage.HRMPSB_REVIEW.label,
                "short_label": "HRMPSB",
            }
        )
    if branch == PositionPosting.Branch.PLANTILLA:
        stages.append(
            {
                "value": RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
                "label": RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW.label,
                "short_label": "Authority",
            }
        )
    stages.extend(
        [
            {
                "value": RecruitmentCase.Stage.COMPLETION,
                "label": RecruitmentCase.Stage.COMPLETION.label,
                "short_label": "Completion",
            },
            {
                "value": RecruitmentCase.Stage.CLOSED,
                "label": RecruitmentCase.Stage.CLOSED.label,
                "short_label": "Closed",
            },
        ]
    )
    return stages


@register.simple_tag
def workflow_stage_state(branch, current_stage, step_value, case_status=""):
    stages = [stage["value"] for stage in workflow_stages(branch)]
    if step_value not in stages:
        return "future"
    if step_value == RecruitmentCase.Stage.CLOSED:
        return "current" if current_stage == RecruitmentCase.Stage.CLOSED else "future"
    if current_stage == RecruitmentCase.Stage.CLOSED:
        return "complete"
    try:
        current_index = stages.index(current_stage)
    except ValueError:
        current_index = -1
    step_index = stages.index(step_value)
    if current_stage == step_value:
        return "current"
    if step_index < current_index:
        return "complete"
    if case_status in {
        RecruitmentCase.CaseStatus.APPROVED,
        RecruitmentCase.CaseStatus.REJECTED,
    } and current_stage == RecruitmentCase.Stage.CLOSED:
        return "complete"
    return "future"


PIPELINE_STAGES = [
    {"key": "screening", "label": "Screening"},
    {"key": "exam", "label": "Exam"},
    {"key": "interview", "label": "Interview"},
    {"key": "deliberation", "label": "Deliberation"},
    {"key": "decision", "label": "Decision"},
    {"key": "completion", "label": "Completion"},
]

PIPELINE_STAGE_MAP = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW: "screening",
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: "screening",
    RecruitmentCase.Stage.HRMPSB_REVIEW: "deliberation",
    RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: "decision",
    RecruitmentCase.Stage.COMPLETION: "completion",
    RecruitmentCase.Stage.CLOSED: "completion",
}

PIPELINE_SECTION_MAP = {
    "screening": "screening",
    "exam": "exam",
    "interview": "interview",
    "deliberation": "deliberation",
    "decision": "decision",
    "completion": "completion",
}

QUEUE_TASK_LABELS = {
    "overview": "Overview",
    "screening": "Screening",
    "exam": "Exam",
    "interview": "Interview",
    "car": "CAR",
    "deliberation": "Deliberation",
    "actions": "Disposition",
    "decision": "Decision",
    "completion": "Completion",
}

QUEUE_TASK_THEMES = {
    "overview": "neutral",
    "screening": "info",
    "exam": "info",
    "interview": "info",
    "car": "info",
    "deliberation": "info",
    "actions": "warning",
    "decision": "info",
    "completion": "info",
}


@register.simple_tag
def pipeline_stages():
    return PIPELINE_STAGES


def _pipeline_step_for_actions(application, current_stage):
    if current_stage in {
        RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    }:
        return "interview"
    if current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
        return "decision"
    return PIPELINE_STAGE_MAP.get(current_stage, "screening")


@register.simple_tag
def pipeline_stage_state(application, case_status, step_key):
    recruitment_case = getattr(application, "case", None)
    current_stage = getattr(recruitment_case, "current_stage", "")
    if not current_stage:
        return "future"

    current_section = get_current_workflow_section(application)
    if current_section == "actions":
        mapped = _pipeline_step_for_actions(application, current_stage)
    else:
        mapped = PIPELINE_SECTION_MAP.get(current_section) or PIPELINE_STAGE_MAP.get(
            current_stage,
            "screening",
        )
    order = [s["key"] for s in PIPELINE_STAGES]

    try:
        current_idx = order.index(mapped)
    except ValueError:
        current_idx = 0

    try:
        step_idx = order.index(step_key)
    except ValueError:
        return "future"

    if case_status == RecruitmentCase.CaseStatus.REJECTED:
        terminal_idx = order.index("decision")
        if step_idx < terminal_idx:
            return "complete"
        if step_idx == terminal_idx:
            return "current"
        return "future"
    if case_status == RecruitmentCase.CaseStatus.APPROVED:
        terminal_idx = order.index("completion")
        if step_idx < terminal_idx:
            return "complete"
        if step_idx == terminal_idx:
            return "current"
        return "future"

    if step_idx < current_idx:
        return "complete"
    if step_idx == current_idx:
        return "current"
    return "future"


def _queue_task_display(application):
    recruitment_case = getattr(application, "case", None)
    if recruitment_case:
        if recruitment_case.case_status == RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT:
            return (
                RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT.label,
                status_theme(RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT),
            )
        if recruitment_case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
            return (
                RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION.label,
                status_theme(RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION),
            )
        if recruitment_case.case_status == RecruitmentCase.CaseStatus.REJECTED:
            return (
                RecruitmentCase.CaseStatus.REJECTED.label,
                status_theme(RecruitmentCase.CaseStatus.REJECTED),
            )
        if recruitment_case.current_stage == RecruitmentCase.Stage.CLOSED:
            if recruitment_case.case_status:
                return (
                    status_label(recruitment_case.case_status),
                    status_theme(recruitment_case.case_status),
                )
            return ("Closed", "closed")

        section = get_current_workflow_section(application)
        return (
            QUEUE_TASK_LABELS.get(section, section.replace("_", " ").title()),
            QUEUE_TASK_THEMES.get(section, "info"),
        )

    return (
        status_label(application.status),
        status_theme(application.status),
    )


@register.simple_tag
def queue_task_label(application):
    return _queue_task_display(application)[0]


@register.simple_tag
def queue_task_theme(application):
    return _queue_task_display(application)[1]


@register.simple_tag(takes_context=True)
def queue_waiting_on_car(context, application):
    """True when this row is a Plantilla HRMPSB case whose CAR is still being prepared by
    another role and the current viewer can't prepare it — used only to add a soft
    "Waiting on CAR preparation" hint. The case stays in the queue; access is unchanged."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return application_awaiting_car_preparation(user, application)


@register.simple_tag
def stage_sla_state(recruitment_case):
    if not recruitment_case:
        return "ok"
    return recruitment_case.stage_sla_state


@register.simple_tag
def stage_sla_context(recruitment_case):
    if not recruitment_case:
        return {
            "state": "ok",
            "elapsed": None,
            "elapsed_days": 0,
            "is_paused": False,
            "is_overdue": False,
            "is_warning": False,
            "warning_days": 5,
            "overdue_days": 7,
        }
    return recruitment_case.stage_sla_context


@register.simple_tag
def stage_sla_label(recruitment_case):
    if not recruitment_case:
        return "current step"
    application = getattr(recruitment_case, "application", None)
    if application:
        return _queue_task_display(application)[0]
    return stage_label(recruitment_case.current_stage)


@register.inclusion_tag("internal_includes/notifications_bell.html", takes_context=True)
def notifications_bell(context):
    """
    Renders the topbar notifications bell + dropdown.

    Pulls the 10 most recent notifications and the current unread count for the
    signed-in user. Returns an empty payload when there is no authenticated
    internal user so the topbar still renders cleanly.
    """
    request = context.get("request")
    user = getattr(request, "user", None) if request else None
    if not user or not getattr(user, "is_authenticated", False):
        return {
            "request": request,
            "notifications": [],
            "unread_count": 0,
            "has_unread": False,
            "badge_label": "",
        }

    notifications = list(get_recent_notifications(user, limit=10))
    unread_count = get_unread_count(user)
    badge_label = ""
    if unread_count > 0:
        badge_label = "9+" if unread_count > 9 else str(unread_count)

    return {
        "request": request,
        "notifications": notifications,
        "unread_count": unread_count,
        "has_unread": unread_count > 0,
        "badge_label": badge_label,
    }
