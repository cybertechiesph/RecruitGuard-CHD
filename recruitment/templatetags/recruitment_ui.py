import json
import re

from django import template
from django.conf import settings
from django.utils.timesince import timesince as django_timesince

from recruitment.blinding import case_ref_for as _case_ref_for, identity_for as _identity_for
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
    get_exam_record,
    get_interview_session,
)
from recruitment.services import workflow_map

register = template.Library()


@register.filter
def identity_for(application, user):
    """Applicant name for staff; "Candidate C-07" for HRMPSB members (blind review)."""
    return _identity_for(application, user)


@register.filter
def case_ref_for(application, user):
    """Case reference for staff; the candidate code for HRMPSB members (blind review)."""
    return _case_ref_for(application, user)

# Apps that open links in their own built-in browser (Facebook, Messenger, Instagram, LINE,
# WeChat, TikTok). Vacancies are shared on Facebook, so many applicants arrive this way, and
# these browsers are known to mishandle file uploads, the camera and cookies.
IN_APP_BROWSER_PATTERN = re.compile(
    r"FBAN|FBAV|FB_IAB|FBIOS|Instagram|Messenger|\bLine/|MicroMessenger|TikTok|musical_ly|BytedanceWebview",
    re.IGNORECASE,
)


@register.simple_tag
def office_contact():
    """The office contact details for applicant pages, from the one place they are kept
    (recruitment/email_branding.py, shared with the emails). ``hrmu`` is for anything about
    an application; ``desk`` is the general Public Assistance Desk."""
    from recruitment import email_branding as branding

    return {
        "hrmu": {
            "name": "Human Resource Management Unit (HRMU)",
            "phone": branding.CONTACT_PHONE,
            "tel": branding.CONTACT_PHONE_TEL,
            "email": branding.CONTACT_EMAIL,
        },
        "desk": {
            "name": branding.PUBLIC_DESK_NAME,
            "phone": branding.PUBLIC_DESK_PHONE,
            "tel": branding.PUBLIC_DESK_PHONE_TEL,
            "email": branding.PUBLIC_DESK_EMAIL,
        },
    }


@register.simple_tag(takes_context=True)
def in_app_browser(context):
    """"facebook", "other" or "" for the visitor's browser, from its user agent."""
    request = context.get("request")
    user_agent = request.META.get("HTTP_USER_AGENT", "") if request is not None else ""
    match = IN_APP_BROWSER_PATTERN.search(user_agent)
    if not match:
        return ""
    return "facebook" if match.group(0).upper().startswith(("FB", "MESSENGER")) else "other"


@register.filter
def timesince_short(value):
    """Largest-unit-only 'time since' (e.g. '2 days', '1 month'). Django's timesince can
    return two comma-joined units ('2 days, 3 hours'); list rows only need the leading unit,
    so this trims the rest cleanly (no trailing ellipsis)."""
    if not value:
        return ""
    return django_timesince(value).split(",")[0]


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
    RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION: "warning",
    RecruitmentCase.CaseStatus.APPROVED: "success",
    RecruitmentCase.CaseStatus.REJECTED: "danger",
    # SL-10/SL-11: neutral exits, so they must not wear the rejection colour.
    RecruitmentCase.CaseStatus.WITHDRAWN: "neutral",
    RecruitmentCase.CaseStatus.VACANCY_CANCELLED: "neutral",
    RecruitmentApplication.Status.VACANCY_CANCELLED: "neutral",
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
def dict_get(mapping, key):
    """Look a value up by a non-literal key. Django templates can only do
    ``mapping.literal``, and the CAR's per-candidate facts are keyed by case id."""
    if not hasattr(mapping, "get"):
        return None
    return mapping.get(key)


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


QUEUE_TASK_LABELS = {
    "overview": "Overview",
    "screening": "Screening",
    "exam": "Exam",
    "interview": "Interview",
    "car": "CAR",
    "deliberation": "Deliberation",
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
    "decision": "info",
    "completion": "info",
}


ENDED_CASE_STATUS_PHRASES = {
    RecruitmentCase.CaseStatus.WITHDRAWN: "Withdrawn at",
    RecruitmentCase.CaseStatus.VACANCY_CANCELLED: "Vacancy cancelled at",
    RecruitmentCase.CaseStatus.REJECTED: "Ended at",
}


def _case_step_keys(application):
    """The progress steps of this case's branch and level (services/workflow_map.py)."""
    return [
        (step.key, step.label)
        for step in workflow_map.steps(application.branch, application.position.level)
    ]


def _case_step_for_stage(application, stage, section):
    """Which progress step a (stage, section) pair belongs to, per branch and level."""
    return workflow_map.step_key(application.branch, application.position.level, stage, section)


def _ended_case_step(application, case):
    """Where a withdrawn, cancelled or not-selected case stopped, from the stage it was
    closed in (``locked_stage``) and the records it had reached there."""
    stage = case.locked_stage or RecruitmentCase.Stage.SECRETARIAT_REVIEW
    Stage = RecruitmentCase.Stage
    if stage == Stage.HRMPSB_REVIEW:
        session = get_interview_session(application, stage=stage)
        section = "car" if session and session.is_finalized else "interview"
    elif application.branch == PositionPosting.Branch.COS and stage == Stage.HRM_CHIEF_REVIEW:
        session = get_interview_session(application, stage=stage)
        if session and session.is_finalized:
            section = "deliberation"
        elif session:
            section = "interview"
        else:
            section = "exam" if get_exam_record(application, stage=stage) else "screening"
    elif stage in {Stage.SECRETARIAT_REVIEW, Stage.HRM_CHIEF_REVIEW}:
        section = "exam" if get_exam_record(application, stage=stage) else "screening"
    else:
        section = ""
    return _case_step_for_stage(application, stage, section)


@register.simple_tag
def case_progress(application):
    """The case's progress bar: this branch's own steps, each marked complete, current,
    ended or future, plus the one-line summary shown when the bar is collapsed."""
    steps = _case_step_keys(application)
    keys = [key for key, _ in steps]
    case = getattr(application, "case", None)
    ended_phrase = ENDED_CASE_STATUS_PHRASES.get(getattr(case, "case_status", ""))
    finished = (
        case is not None
        and case.current_stage == RecruitmentCase.Stage.CLOSED
        and case.case_status == RecruitmentCase.CaseStatus.APPROVED
    )
    if case is None:
        current_key = None
    elif ended_phrase:
        current_key = _ended_case_step(application, case)
    else:
        current_key = _case_step_for_stage(
            application, case.current_stage, get_current_workflow_section(application)
        )
    current_idx = keys.index(current_key) if current_key in keys else -1

    rows = []
    for idx, (key, label) in enumerate(steps):
        if finished or (current_idx >= 0 and idx < current_idx):
            state = "complete"
        elif idx == current_idx:
            state = "ended" if ended_phrase else "current"
        else:
            state = "future"
        rows.append({"key": key, "label": label, "state": state, "number": idx + 1})

    if finished:
        summary_step, summary_label = "Done", "All steps complete"
    elif ended_phrase and current_idx >= 0:
        if (
            case.case_status == RecruitmentCase.CaseStatus.REJECTED
            and current_key in {"decision", "selection", "car"}
        ):
            ended_phrase = "Not selected at"
        summary_step, summary_label = "Closed", f"{ended_phrase} {steps[current_idx][1]}"
    elif current_idx >= 0:
        summary_step = f"Step {current_idx + 1} of {len(steps)}"
        summary_label = steps[current_idx][1]
    else:
        summary_step, summary_label = "", ""
    return {"steps": rows, "summary_step": summary_step, "summary_label": summary_label}


def _queue_task_display(application):
    recruitment_case = getattr(application, "case", None)
    if recruitment_case:
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


STAGE_SLA_OWNER_PHRASES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW: "With the Secretariat",
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: "With the HRM Chief",
    RecruitmentCase.Stage.HRMPSB_REVIEW: "At the HRMPSB stage",
    RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: "With the Appointing Authority",
    RecruitmentCase.Stage.COMPLETION: "At Completion",
}


@register.simple_tag
def stage_sla_label(recruitment_case):
    """What the stage clock measures: time with the office that holds the case. It covers
    every step inside that stage (screening and exam together), so naming one step would
    understate it."""
    if not recruitment_case:
        return "At the current step"
    return STAGE_SLA_OWNER_PHRASES.get(recruitment_case.current_stage, "At the current step")


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


@register.simple_tag
def session_inactivity_timeout_seconds():
    """TI-1: the server-side inactivity timeout, so the idle-warning script mirrors it exactly."""
    return int(getattr(settings, "SESSION_INACTIVITY_TIMEOUT_SECONDS", 30 * 60))
