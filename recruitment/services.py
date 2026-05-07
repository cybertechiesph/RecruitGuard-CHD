import csv
import hashlib
import hmac
import json
import os
import secrets
import textwrap
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    InterviewRating,
    InterviewSession,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningRecord,
    WorkflowOverride,
)
from .notification_services import (
    queue_non_selected_applicant_notification,
    queue_selected_applicant_notification,
    queue_submission_acknowledgment_notification,
)
from .permissions import WORKFLOW_PROCESSOR_ROLES
from .requirements import (
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from .upload_validation import validate_applicant_document_upload


EXPORT_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.APPOINTING_AUTHORITY,
}
ENTRY_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.SYSTEM_ADMIN,
}
CASE_REOPEN_ROLES = {
    RecruitmentUser.Role.HRM_CHIEF,
}
SCREENING_REVIEW_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}
SCREENING_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
}
EXAM_REVIEW_ROLES = SCREENING_REVIEW_ROLES
EXAM_STAGES = SCREENING_STAGES
INTERVIEW_SESSION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.HRMPSB_MEMBER,
}
INTERVIEW_SESSION_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    RecruitmentCase.Stage.HRMPSB_REVIEW,
}
INTERVIEW_RATING_ROLES_BY_STAGE = {
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: {RecruitmentUser.Role.HRM_CHIEF},
    RecruitmentCase.Stage.HRMPSB_REVIEW: {RecruitmentUser.Role.HRMPSB_MEMBER},
}
INTERVIEW_FALLBACK_LABEL = "Interview Rating Sheet (Fallback)"
ARTIFACT_TYPE_APPLICANT_DOCUMENT = "applicant_document"
ARTIFACT_TYPE_WORKFLOW_EVIDENCE = "workflow_evidence"
ARTIFACT_TYPE_EXAM_EVIDENCE = "exam_supporting_evidence"
ARTIFACT_TYPE_INTERVIEW_FALLBACK = "interview_fallback_rating_sheet"
ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT = "comparative_assessment_report"
DELIBERATION_STAGES_BY_BRANCH = {
    PositionPosting.Branch.COS: RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    PositionPosting.Branch.PLANTILLA: RecruitmentCase.Stage.HRMPSB_REVIEW,
}
DELIBERATION_ROLES_BY_BRANCH = {
    PositionPosting.Branch.COS: {RecruitmentUser.Role.HRM_CHIEF},
    PositionPosting.Branch.PLANTILLA: {RecruitmentUser.Role.HRMPSB_MEMBER},
}
CAR_REVIEW_STAGE = RecruitmentCase.Stage.HRMPSB_REVIEW
CAR_MANAGER_ROLES = {RecruitmentUser.Role.HRMPSB_MEMBER}
CAR_LABEL = "Comparative Assessment Report"
FINAL_DECISION_OUTCOME_TO_STATUS = {
    FinalDecision.Outcome.SELECTED: RecruitmentApplication.Status.APPROVED,
    FinalDecision.Outcome.NOT_SELECTED: RecruitmentApplication.Status.REJECTED,
}
COMPLETION_REVIEW_ROLES = SCREENING_REVIEW_ROLES
COMPLETION_STAGES = {
    RecruitmentCase.Stage.COMPLETION,
}
EVIDENCE_ARCHIVE_ROLES = WORKFLOW_PROCESSOR_ROLES
WORKFLOW_SECTION_LABELS = {
    "overview": "Overview",
    "screening": "Screening",
    "exam": "Exam",
    "interview": "Interview",
    "deliberation": "Deliberation",
    "decision": "Decision",
    "completion": "Completion",
    "actions": "Actions",
}


def _copy_metadata(metadata):
    return dict(metadata or {})


def record_audit_event(application, actor, action, description, metadata=None):
    metadata = _copy_metadata(metadata)
    if application is not None:
        metadata.setdefault("case_reference", application.reference_number or "")
        case = getattr(application, "case", None)
        if case is not None:
            metadata.setdefault("case_stage", case.current_stage)
            metadata.setdefault("case_status", case.case_status)
            metadata.setdefault("case_handler_role", case.current_handler_role)
        elif application.status:
            metadata.setdefault("application_status", application.status)
    return AuditLog.objects.create(
        application=application,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        action=action,
        description=description,
        metadata=metadata,
    )


def record_system_audit_event(actor, action, description, metadata=None):
    return record_audit_event(
        application=None,
        actor=actor,
        action=action,
        description=description,
        metadata=metadata,
    )


def record_protected_record_access(application, actor, source):
    return record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.PROTECTED_RECORD_VIEWED,
        description="Reviewed a protected recruitment case record.",
        metadata={
            "access_source": source,
        },
    )


def record_evidence_vault_access(
    actor,
    *,
    search_query="",
    stage="",
    artifact_scope="",
    archival_status="",
    current_version_only=True,
):
    return record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
        description="Reviewed evidence vault records.",
        metadata={
            "search_query": search_query,
            "stage": stage,
            "artifact_scope": artifact_scope,
            "archival_status": archival_status,
            "current_version_only": bool(current_version_only),
        },
    )


def record_audit_log_review(
    actor,
    *,
    application=None,
    search_query="",
    action="",
    actor_role="",
    sensitive_only=False,
    result_count=0,
):
    metadata = {
        "review_scope": "application_audit" if application is not None else "system_audit",
        "search_query": search_query,
        "action_filter": action,
        "actor_role_filter": actor_role,
        "sensitive_only": bool(sensitive_only),
        "result_count": result_count,
    }
    if application is not None:
        return record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
            description="Reviewed the application audit trail.",
            metadata=metadata,
        )
    return record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.AUDIT_LOG_VIEWED,
        description="Reviewed system audit logs.",
        metadata=metadata,
    )


def record_routing_history_event(
    application,
    actor,
    route_type,
    description,
    *,
    recruitment_case=None,
    from_handler_role="",
    to_handler_role="",
    from_status="",
    to_status="",
    from_stage="",
    to_stage="",
    notes="",
    is_override=False,
):
    return RoutingHistory.objects.create(
        application=application,
        recruitment_case=recruitment_case,
        actor=actor,
        actor_role=getattr(actor, "role", ""),
        branch=application.branch,
        level=application.level,
        route_type=route_type,
        from_handler_role=from_handler_role,
        to_handler_role=to_handler_role,
        from_status=from_status,
        to_status=to_status,
        from_stage=from_stage,
        to_stage=to_stage,
        description=description,
        notes=notes,
        is_override=is_override,
    )


def get_visible_positions_for_user(user):
    if user.role == RecruitmentUser.Role.APPLICANT:
        entries = PositionPosting.objects.filter(
            status=PositionPosting.EntryStatus.ACTIVE,
        ).select_related("position_reference")
        return [entry for entry in entries if entry.is_open_for_intake]
    return PositionPosting.objects.select_related("position_reference").all()


def get_queue_for_user(user):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return RecruitmentApplication.objects.none()
    queryset = RecruitmentApplication.objects.select_related("applicant", "position", "case")
    queryset = queryset.filter(current_handler_role=user.role)
    if user.role == RecruitmentUser.Role.SECRETARIAT:
        queryset = queryset.filter(
            Q(level=PositionPosting.Level.LEVEL_1)
            | Q(
                overrides__is_active=True,
                overrides__target_role=RecruitmentUser.Role.SECRETARIAT,
            )
        ).distinct()
    return queryset


def _user_has_closed_application_access(user, application):
    case = getattr(application, "case", None)
    if (
        user.role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return False
    if case and case.current_stage == RecruitmentCase.Stage.CLOSED:
        return user.role in WORKFLOW_PROCESSOR_ROLES
    return user.role in EXPORT_ROLES


def get_manageable_positions(user):
    if user.role not in ENTRY_MANAGER_ROLES:
        return PositionReference.objects.none()
    return PositionReference.objects.all().order_by("position_title", "salary_grade", "class_id")


def get_manageable_recruitment_entries(user):
    if user.role not in ENTRY_MANAGER_ROLES:
        return PositionPosting.objects.none()
    return PositionPosting.objects.select_related(
        "position_reference",
        "created_by",
        "updated_by",
    ).exclude(status=PositionPosting.EntryStatus.CLOSED).order_by("-updated_at")


def user_can_view_application(user, application):
    if user.role == RecruitmentUser.Role.APPLICANT:
        return application.applicant_id == user.id
    if application.current_handler_role == user.role:
        if (
            user.role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            return application.active_secretariat_override is not None
        return True
    if application.status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    }:
        return _user_has_closed_application_access(user, application)
    return False


def get_effective_role_for_action(user, application):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return ""
    return user.role


def user_can_process_application(user, application):
    case = getattr(application, "case", None)
    effective_role = get_effective_role_for_action(user, application)
    if not effective_role:
        return False
    expected_role = case.current_handler_role if case else application.current_handler_role
    if effective_role != expected_role:
        return False
    if case and case.is_stage_locked:
        return False
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return application.active_secretariat_override is not None
    return effective_role in WORKFLOW_PROCESSOR_ROLES


def user_can_upload_evidence(user, application):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return True
    if user.role == RecruitmentUser.Role.APPLICANT and application.applicant_id == user.id:
        return application.is_editable_by_applicant
    return user_can_process_application(user, application)


def user_can_manage_evidence_archive(user, application):
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        return True
    return user.role in EVIDENCE_ARCHIVE_ROLES and user_can_view_application(user, application)


def user_can_export_application(user, application):
    return user.role in EXPORT_ROLES and user_can_view_application(user, application)


def generate_submission_hash(application):
    payload = "|".join(
        [
            application.reference_number or "pending-reference",
            str(application.applicant_id),
            str(application.position_id),
            application.status,
            timezone.now().isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate_application_reference():
    return f"RG-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def _review_stage_from_application_status(application_status):
    stage_map = {
        RecruitmentApplication.Status.SECRETARIAT_REVIEW: RecruitmentCase.Stage.SECRETARIAT_REVIEW,
        RecruitmentApplication.Status.HRM_CHIEF_REVIEW: RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        RecruitmentApplication.Status.HRMPSB_REVIEW: RecruitmentCase.Stage.HRMPSB_REVIEW,
        RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
    }
    return stage_map.get(application_status, "")


def _application_status_from_stage(stage):
    status_map = {
        RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentApplication.Status.SECRETARIAT_REVIEW,
        RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
        RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentApplication.Status.HRMPSB_REVIEW,
        RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
        RecruitmentCase.Stage.COMPLETION: RecruitmentApplication.Status.APPROVED,
    }
    return status_map.get(stage, "")


def _completion_handler_role(application):
    if application.level == PositionPosting.Level.LEVEL_1:
        return RecruitmentUser.Role.SECRETARIAT
    return RecruitmentUser.Role.HRM_CHIEF


def _handler_role_from_stage(stage, application=None):
    role_map = {
        RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentUser.Role.SECRETARIAT,
        RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
        RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW: RecruitmentUser.Role.APPOINTING_AUTHORITY,
    }
    if stage == RecruitmentCase.Stage.COMPLETION and application is not None:
        return _completion_handler_role(application)
    return role_map.get(stage, "")


def _case_timeline_metadata(case):
    return {
        "case_stage": case.current_stage,
        "case_status": case.case_status,
        "case_handler_role": case.current_handler_role,
        "case_locked": case.is_stage_locked,
    }


def _ensure_case_stage_alignment(application, case):
    expected_stage = _review_stage_from_application_status(application.status)
    if expected_stage and case.current_stage != expected_stage:
        raise ValueError("The recruitment case is out of sync with the application workflow state.")


def get_case_timeline(application):
    return application.audit_logs.filter(
        action__in=[
            AuditLog.Action.APPLICATION_SUBMITTED,
            AuditLog.Action.CASE_CREATED,
            AuditLog.Action.CASE_REOPENED,
            AuditLog.Action.ROUTED,
            AuditLog.Action.SCREENING_RECORDED,
            AuditLog.Action.SCREENING_FINALIZED,
            AuditLog.Action.EXAM_RECORDED,
            AuditLog.Action.EXAM_FINALIZED,
            AuditLog.Action.INTERVIEW_SCHEDULED,
            AuditLog.Action.INTERVIEW_FINALIZED,
            AuditLog.Action.INTERVIEW_RATING_RECORDED,
            AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
            AuditLog.Action.DELIBERATION_RECORDED,
            AuditLog.Action.DELIBERATION_FINALIZED,
            AuditLog.Action.CAR_GENERATED,
            AuditLog.Action.CAR_FINALIZED,
            AuditLog.Action.DECISION_RECORDED,
            AuditLog.Action.COMPLETION_RECORDED,
            AuditLog.Action.CASE_CLOSED,
            AuditLog.Action.NOTIFICATION_SENT,
            AuditLog.Action.NOTIFICATION_FAILED,
            AuditLog.Action.OVERRIDE_GRANTED,
            AuditLog.Action.OVERRIDE_USED,
            AuditLog.Action.EVIDENCE_UPLOADED,
            AuditLog.Action.EVIDENCE_DOWNLOADED,
            AuditLog.Action.EVIDENCE_ARCHIVED,
            AuditLog.Action.EVIDENCE_RESTORED,
            AuditLog.Action.EXPORT_GENERATED,
        ]
    ).order_by("created_at")


def _filter_audit_logs(queryset, *, search_query="", action="", actor_role="", sensitive_only=False):
    if search_query:
        queryset = queryset.filter(
            Q(case_reference__icontains=search_query)
            | Q(workflow_stage__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(actor__username__icontains=search_query)
            | Q(actor__first_name__icontains=search_query)
            | Q(actor__last_name__icontains=search_query)
        )
    if action:
        queryset = queryset.filter(action=action)
    if actor_role:
        queryset = queryset.filter(actor_role=actor_role)
    if sensitive_only:
        queryset = queryset.filter(is_sensitive_access=True)
    return queryset


def get_application_audit_logs(
    application,
    *,
    search_query="",
    action="",
    actor_role="",
    sensitive_only=False,
):
    queryset = application.audit_logs.select_related(
        "actor",
        "application",
        "application__position",
    ).order_by("-created_at")
    return _filter_audit_logs(
        queryset,
        search_query=search_query,
        action=action,
        actor_role=actor_role,
        sensitive_only=sensitive_only,
    )


def get_system_audit_logs(*, search_query="", action="", actor_role="", sensitive_only=False):
    queryset = AuditLog.objects.filter(application__isnull=True).select_related("actor").order_by("-created_at")
    return _filter_audit_logs(
        queryset,
        search_query=search_query,
        action=action,
        actor_role=actor_role,
        sensitive_only=sensitive_only,
    )


def _evidence_stage_for_application(application):
    case = getattr(application, "case", None)
    if case and case.current_stage:
        return case.current_stage
    derived_stage = _review_stage_from_application_status(application.status)
    if derived_stage:
        return derived_stage
    return EvidenceVaultItem.Stage.APPLICANT_INTAKE


def _accessible_application_ids_for_user(user):
    if user.role not in WORKFLOW_PROCESSOR_ROLES:
        return []
    applications = RecruitmentApplication.objects.select_related(
        "position",
        "case",
        "applicant",
    ).order_by("-updated_at")
    return [application.id for application in applications if user_can_view_application(user, application)]


def _evidence_context_filter_for_application(application):
    filters = Q(
        artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
        application=application,
    ) | Q(
        artifact_scope=EvidenceVaultItem.OwnerScope.ENTRY,
        recruitment_entry=application.position,
    )
    case = getattr(application, "case", None)
    if case is not None:
        filters |= Q(
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            recruitment_case=case,
        )
    return filters


def evidence_belongs_to_application_context(evidence, application):
    if evidence.artifact_scope == EvidenceVaultItem.OwnerScope.APPLICATION:
        return evidence.application_id == application.id
    if evidence.artifact_scope == EvidenceVaultItem.OwnerScope.CASE:
        case = getattr(application, "case", None)
        return case is not None and evidence.recruitment_case_id == case.id
    return evidence.recruitment_entry_id == application.position_id


def get_evidence_context_application_for_user(user, evidence, preferred_application=None):
    candidates = []
    if preferred_application is not None:
        candidates.append(preferred_application)
    if evidence.application_id:
        candidates.append(evidence.application)
    if evidence.recruitment_case_id:
        candidates.append(evidence.recruitment_case.application)
    if evidence.recruitment_entry_id:
        candidates.extend(
            RecruitmentApplication.objects.select_related("position", "case", "applicant")
            .filter(position_id=evidence.recruitment_entry_id)
            .order_by("-submitted_at", "-updated_at", "-created_at")
        )
    seen_ids = set()
    for candidate in candidates:
        if candidate is None or candidate.id in seen_ids:
            continue
        seen_ids.add(candidate.id)
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN and evidence_belongs_to_application_context(
            evidence,
            candidate,
        ):
            return candidate
        if user_can_view_application(user, candidate) and evidence_belongs_to_application_context(
            evidence,
            candidate,
        ):
            return candidate
    return None


def get_evidence_queryset_for_user(
    user,
    *,
    application=None,
    search_query="",
    stage="",
    artifact_scope="",
    archival_status="active",
    current_version_only=False,
):
    queryset = EvidenceVaultItem.objects.select_related(
        "application",
        "application__position",
        "recruitment_case",
        "recruitment_case__application",
        "recruitment_case__application__position",
        "recruitment_entry",
        "uploaded_by",
        "archived_by",
        "previous_version",
    )
    if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        if application is not None:
            queryset = queryset.filter(_evidence_context_filter_for_application(application))
    elif application is not None:
        if not user_can_view_application(user, application):
            return queryset.none()
        queryset = queryset.filter(_evidence_context_filter_for_application(application))
    else:
        accessible_ids = _accessible_application_ids_for_user(user)
        if not accessible_ids:
            return queryset.none()
        case_ids = list(
            RecruitmentCase.objects.filter(application_id__in=accessible_ids).values_list("id", flat=True)
        )
        entry_ids = list(
            PositionPosting.objects.filter(applications__id__in=accessible_ids)
            .distinct()
            .values_list("id", flat=True)
        )
        queryset = queryset.filter(
            Q(
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                application_id__in=accessible_ids,
            )
            | Q(
                artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
                recruitment_case_id__in=case_ids,
            )
            | Q(
                artifact_scope=EvidenceVaultItem.OwnerScope.ENTRY,
                recruitment_entry_id__in=entry_ids,
            )
        )

    if search_query:
        search_query = search_query.strip()
        queryset = queryset.filter(
            Q(label__icontains=search_query)
            | Q(original_filename__icontains=search_query)
            | Q(sha256_digest__icontains=search_query)
            | Q(archive_tag__icontains=search_query)
            | Q(application__reference_number__icontains=search_query)
            | Q(application__position__title__icontains=search_query)
            | Q(recruitment_case__application__reference_number__icontains=search_query)
            | Q(recruitment_case__application__position__title__icontains=search_query)
            | Q(recruitment_entry__job_code__icontains=search_query)
            | Q(recruitment_entry__title__icontains=search_query)
            | Q(uploaded_by__username__icontains=search_query)
            | Q(uploaded_by__first_name__icontains=search_query)
            | Q(uploaded_by__last_name__icontains=search_query)
        )
    if stage:
        queryset = queryset.filter(stage=stage)
    if artifact_scope:
        queryset = queryset.filter(artifact_scope=artifact_scope)
    if archival_status == "archived":
        queryset = queryset.filter(is_archived=True)
    elif archival_status == "active":
        queryset = queryset.filter(is_archived=False)
    if current_version_only:
        queryset = queryset.filter(is_current_version=True)
    return queryset.order_by("artifact_scope", "-created_at", "document_key", "-version_number")


def user_can_reopen_case(user, case):
    return bool(
        case
        and user.role in CASE_REOPEN_ROLES
        and case.is_stage_locked
        and case.locked_stage
    )


def get_current_review_stage(application):
    case = getattr(application, "case", None)
    if case:
        return case.current_stage
    return _review_stage_from_application_status(application.status)


def get_screening_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).filter(review_stage=review_stage).first()


def get_screening_records(application):
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).order_by("created_at")


def user_can_manage_screening(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in SCREENING_REVIEW_ROLES
        or current_stage not in SCREENING_STAGES
        or get_current_workflow_section(application) != "screening"
    ):
        return False
    return user_can_process_application(user, application)


def screening_is_finalized_for_current_stage(application):
    screening_record = get_screening_record(application)
    return bool(screening_record and screening_record.is_finalized)


def screening_requires_disposition_for_current_stage(application):
    screening_record = get_screening_record(application)
    if not screening_record or not screening_record.is_finalized:
        return False
    return (
        screening_record.completeness_status == ScreeningRecord.CompletenessStatus.INCOMPLETE
        or screening_record.qualification_outcome
        == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
    )


def exam_is_finalized_for_current_stage(application):
    exam_record = get_exam_record(application)
    return bool(exam_record and exam_record.is_finalized)


def interview_is_finalized_for_current_stage(application):
    interview_session = get_interview_session(application)
    return bool(interview_session and interview_session.is_finalized)


def deliberation_is_finalized_for_current_stage(application):
    deliberation_record = get_deliberation_record(application)
    return bool(deliberation_record and deliberation_record.is_finalized)


def car_is_finalized_for_current_stage(application):
    report = get_comparative_assessment_report(application)
    return bool(report and report.is_finalized)


def _workflow_detail_sequence(application):
    current_stage = get_current_review_stage(application)
    if current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
        return ["screening", "exam", "actions"]
    if current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
        if application.branch == PositionPosting.Branch.COS:
            return ["screening", "exam", "interview", "deliberation", "decision"]
        return ["screening", "exam", "actions"]
    if current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
        return ["interview", "deliberation", "actions"]
    if current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW:
        return ["decision"]
    if current_stage in {
        RecruitmentCase.Stage.COMPLETION,
        RecruitmentCase.Stage.CLOSED,
    }:
        return ["completion"]
    return ["overview"]


def _workflow_section_is_complete(application, section_key):
    if section_key == "screening":
        return (
            screening_is_finalized_for_current_stage(application)
            and not screening_requires_disposition_for_current_stage(application)
        )
    if section_key == "exam":
        return exam_is_finalized_for_current_stage(application)
    if section_key == "interview":
        return interview_is_finalized_for_current_stage(application)
    if section_key == "deliberation":
        if not deliberation_is_finalized_for_current_stage(application):
            return False
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and get_current_review_stage(application) == RecruitmentCase.Stage.HRMPSB_REVIEW
        ):
            return car_is_finalized_for_current_stage(application)
        return True
    if section_key == "actions":
        return False
    return True


def get_current_workflow_section(application):
    sequence = _workflow_detail_sequence(application)
    for section_key in sequence:
        if not _workflow_section_is_complete(application, section_key):
            return section_key
    if sequence:
        return sequence[-1]
    return "overview"


def get_current_workflow_section_label(application):
    return WORKFLOW_SECTION_LABELS.get(get_current_workflow_section(application), "Overview")


def get_application_detail_tab(application):
    section_key = get_current_workflow_section(application)
    return {
        "key": section_key,
        "label": WORKFLOW_SECTION_LABELS.get(section_key, "Overview"),
        "section_id": f"cws-{section_key}",
    }


def _workflow_progress_block_message(application, section_key=None):
    section_key = section_key or get_current_workflow_section(application)
    if section_key == "screening":
        return "Finalize the screening record before endorsing this application."
    if section_key == "exam":
        return "Finalize the examination record before proceeding to the next workflow task."
    if section_key == "interview":
        return "Finalize the interview task before proceeding to the next workflow task."
    if section_key == "deliberation":
        if (
            application.branch == PositionPosting.Branch.COS
            and get_current_review_stage(application) == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        ):
            return "Finalize the deliberation record before endorsing this COS application."
        if (
            application.branch == PositionPosting.Branch.PLANTILLA
            and get_current_review_stage(application) == RecruitmentCase.Stage.HRMPSB_REVIEW
        ):
            if not deliberation_is_finalized_for_current_stage(application):
                return "Finalize the deliberation record before recommending this Plantilla application."
            if not car_is_finalized_for_current_stage(application):
                return "Finalize the Comparative Assessment Report before recommending this Plantilla application."
        return "Finalize the deliberation record before proceeding to the next workflow task."
    return "Complete the current workflow task before proceeding."


def get_exam_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
        "evidence_item",
    ).filter(review_stage=review_stage).first()


def get_exam_records(application):
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
        "evidence_item",
    ).order_by("created_at")


def user_can_manage_exam(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in EXAM_REVIEW_ROLES
        or current_stage not in EXAM_STAGES
        or get_current_workflow_section(application) != "exam"
    ):
        return False
    return user_can_process_application(user, application)


def get_interview_session(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.interview_sessions.select_related(
        "scheduled_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def get_interview_sessions(application):
    sessions = list(
        application.interview_sessions.select_related(
            "scheduled_by",
            "finalized_by",
            "recruitment_case",
            "recruitment_entry",
        ).prefetch_related("ratings__rated_by").order_by("created_at")
    )
    fallback_items = list(get_interview_fallback_evidence(application))
    fallback_by_stage = {}
    for item in fallback_items:
        fallback_by_stage.setdefault(item.stage, []).append(item)
    for session in sessions:
        session.fallback_evidence_items = fallback_by_stage.get(session.review_stage, [])
    return sessions


def get_interview_ratings(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.interview_ratings.select_related(
        "rated_by",
        "interview_session",
    ).filter(review_stage=review_stage).order_by("created_at")


def get_interview_rating_for_user(application, user, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.interview_ratings.select_related(
        "interview_session",
    ).filter(review_stage=review_stage, rated_by=user).first()


def get_interview_fallback_evidence(application, stage=None):
    case = getattr(application, "case", None)
    if case is None:
        return EvidenceVaultItem.objects.none()
    queryset = EvidenceVaultItem.objects.select_related(
        "uploaded_by",
        "recruitment_case",
    ).filter(
        artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
        artifact_type=ARTIFACT_TYPE_INTERVIEW_FALLBACK,
        recruitment_case=case,
    )
    if stage:
        queryset = queryset.filter(stage=stage)
    return queryset.order_by("created_at")


def user_can_manage_interview_session(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in INTERVIEW_SESSION_MANAGER_ROLES
        or current_stage not in INTERVIEW_SESSION_STAGES
        or get_current_workflow_section(application) != "interview"
    ):
        return False
    if (
        application.branch != PositionPosting.Branch.COS
        and current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return False
    return user_can_process_application(user, application)


def user_can_manage_interview_rating(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in INTERVIEW_RATING_ROLES_BY_STAGE.get(current_stage, set())
        or get_current_workflow_section(application) != "interview"
    ):
        return False
    return user_can_process_application(user, application)


def user_can_upload_interview_fallback(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in INTERVIEW_SESSION_MANAGER_ROLES
        or current_stage not in INTERVIEW_SESSION_STAGES
        or get_current_workflow_section(application) != "interview"
    ):
        return False
    if (
        application.branch != PositionPosting.Branch.COS
        and current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return False
    return user_can_process_application(user, application)


def _decimal_string(value):
    if value is None:
        return ""
    return str(value)


def _average_interview_rating(interview_session):
    ratings = list(interview_session.ratings.all())
    if not ratings:
        return None
    total = sum((rating.rating_score for rating in ratings), Decimal("0"))
    return (total / Decimal(len(ratings))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _deliberation_snapshot_for_screening(screening_record):
    return {
        "id": screening_record.id,
        "review_stage": screening_record.review_stage,
        "completeness_status": screening_record.completeness_status,
        "qualification_outcome": screening_record.qualification_outcome,
        "finalized_at": screening_record.finalized_at.isoformat() if screening_record.finalized_at else "",
    }


def _deliberation_snapshot_for_exam(exam_record):
    return {
        "id": exam_record.id,
        "review_stage": exam_record.review_stage,
        "exam_type": exam_record.exam_type,
        "exam_status": exam_record.exam_status,
        "exam_score": _decimal_string(exam_record.effective_score),
        "exam_result": exam_record.exam_result,
        "technical_score": _decimal_string(exam_record.technical_score),
        "technical_result": exam_record.technical_result,
        "practical_score": _decimal_string(exam_record.practical_score),
        "practical_result": exam_record.practical_result,
        "component_summary": exam_record.component_summary,
        "exam_date": exam_record.exam_date.isoformat() if exam_record.exam_date else "",
        "administered_by": exam_record.administered_by,
        "evidence_id": exam_record.evidence_item_id or "",
        "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
        "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
        "finalized_at": exam_record.finalized_at.isoformat() if exam_record.finalized_at else "",
    }


def _deliberation_snapshot_for_interview(application, interview_session):
    average_score = _average_interview_rating(interview_session)
    fallback_count = get_interview_fallback_evidence(application, stage=interview_session.review_stage).count()
    return {
        "id": interview_session.id,
        "review_stage": interview_session.review_stage,
        "scheduled_for": interview_session.scheduled_for.isoformat(),
        "rating_count": interview_session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": _decimal_string(average_score),
        "finalized_at": interview_session.finalized_at.isoformat() if interview_session.finalized_at else "",
    }


def build_deliberation_consolidation(application):
    screening_records = list(
        application.screening_records.filter(is_finalized=True).select_related(
            "reviewed_by",
            "finalized_by",
        ).order_by("created_at")
    )
    exam_records = list(
        application.exam_records.filter(is_finalized=True).select_related(
            "recorded_by",
            "finalized_by",
            "evidence_item",
        ).order_by("created_at")
    )
    interview_sessions = list(
        application.interview_sessions.filter(is_finalized=True).select_related(
            "scheduled_by",
            "finalized_by",
        ).prefetch_related("ratings").order_by("created_at")
    )
    latest_screening = screening_records[-1] if screening_records else None
    latest_exam = exam_records[-1] if exam_records else None
    latest_interview = interview_sessions[-1] if interview_sessions else None
    latest_interview_average = _average_interview_rating(latest_interview) if latest_interview else None
    return {
        "application_reference": application.reference_number or "",
        "entry_code": application.position.job_code,
        "branch": application.branch,
        "level": application.level,
        "generated_at": timezone.now().isoformat(),
        "screening_records": [_deliberation_snapshot_for_screening(item) for item in screening_records],
        "exam_records": [_deliberation_snapshot_for_exam(item) for item in exam_records],
        "interview_sessions": [
            _deliberation_snapshot_for_interview(application, item) for item in interview_sessions
        ],
        "summary": {
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "latest_qualification_outcome": (
                latest_screening.qualification_outcome if latest_screening else ""
            ),
            "latest_exam_status": latest_exam.exam_status if latest_exam else "",
            "latest_exam_score": _decimal_string(latest_exam.effective_score if latest_exam else None),
            "latest_exam_components": latest_exam.component_summary if latest_exam else "",
            "latest_interview_average": _decimal_string(latest_interview_average),
        },
    }


def get_deliberation_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def get_deliberation_records(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("created_at")


def get_latest_finalized_deliberation_record(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(is_finalized=True).order_by("-finalized_at", "-created_at").first()


def user_can_manage_deliberation(user, application):
    current_stage = get_current_review_stage(application)
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch)
    if (
        current_stage != expected_stage
        or get_current_workflow_section(application) != "deliberation"
    ):
        return False
    if user.role not in DELIBERATION_ROLES_BY_BRANCH.get(application.branch, set()):
        return False
    return user_can_process_application(user, application)


def get_comparative_assessment_report(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    queryset = ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
    ).order_by(
        "-version_number",
        "-is_finalized",
        "-finalized_at",
        "-created_at",
    )
    return queryset.first()


def get_latest_finalized_comparative_assessment_report(application):
    return ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        is_finalized=True,
    ).order_by("-version_number", "-finalized_at", "-created_at").first()


def get_comparative_assessment_report_items_for_report(report):
    if not report:
        return ComparativeAssessmentReportItem.objects.none()
    return report.items.select_related(
        "recruitment_case",
        "recruitment_case__application",
        "deliberation_record",
    ).order_by("rank_order", "created_at")


def user_can_manage_comparative_assessment_report(user, application):
    current_stage = get_current_review_stage(application)
    if application.branch != PositionPosting.Branch.PLANTILLA:
        return False
    if current_stage != CAR_REVIEW_STAGE:
        return False
    if user.role not in CAR_MANAGER_ROLES:
        return False
    if get_current_workflow_section(application) != "deliberation":
        return False
    return user_can_process_application(user, application)


def get_final_decision_history(application):
    return application.final_decisions.select_related(
        "decided_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("-decided_at", "-created_at")


def get_latest_final_decision(application):
    return get_final_decision_history(application).first()


def _final_decision_stage_and_role(application):
    if application.branch == PositionPosting.Branch.COS:
        return RecruitmentCase.Stage.HRM_CHIEF_REVIEW, RecruitmentUser.Role.HRM_CHIEF
    return RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW, RecruitmentUser.Role.APPOINTING_AUTHORITY


def user_can_record_final_decision(user, application):
    current_stage = get_current_review_stage(application)
    expected_stage, expected_role = _final_decision_stage_and_role(application)
    if (
        current_stage != expected_stage
        or get_current_workflow_section(application) != "decision"
    ):
        return False
    if user.role != expected_role:
        return False
    return user_can_process_application(user, application)


def get_completion_record(application):
    return CompletionRecord.objects.select_related(
        "tracked_by",
        "recruitment_case",
    ).filter(application=application).first()


def get_completion_requirements(application):
    completion_record = get_completion_record(application)
    if not completion_record:
        return CompletionRequirement.objects.none()
    return completion_record.requirements.all()


def user_can_manage_completion(user, application):
    current_stage = get_current_review_stage(application)
    if (
        user.role not in COMPLETION_REVIEW_ROLES
        or current_stage not in COMPLETION_STAGES
        or application.status != RecruitmentApplication.Status.APPROVED
        or get_current_workflow_section(application) != "completion"
    ):
        return False
    return user_can_process_application(user, application)


def user_can_close_case(user, application):
    completion_record = get_completion_record(application)
    return bool(
        completion_record
        and user_can_manage_completion(user, application)
        and completion_record.ready_for_closure
    )


def _decision_packet_screening_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "reviewed_by": str(record.reviewed_by) if record.reviewed_by else "",
        "reviewed_by_role": record.reviewed_by_role,
        "completeness_status": record.completeness_status,
        "completeness_status_label": record.get_completeness_status_display(),
        "qualification_outcome": record.qualification_outcome,
        "qualification_outcome_label": record.get_qualification_outcome_display(),
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_exam_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "exam_type": record.exam_type,
        "exam_status": record.exam_status,
        "exam_status_label": record.get_exam_status_display(),
        "exam_score": _decimal_string(record.effective_score),
        "exam_result": record.exam_result,
        "technical_score": _decimal_string(record.technical_score),
        "technical_result": record.technical_result,
        "practical_score": _decimal_string(record.practical_score),
        "practical_result": record.practical_result,
        "component_summary": record.component_summary,
        "exam_date": record.exam_date.isoformat() if record.exam_date else "",
        "administered_by": record.administered_by,
        "evidence_id": record.evidence_item_id or "",
        "evidence_label": record.evidence_item.label if record.evidence_item_id else "",
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_interview_session(application, session):
    average_score = _average_interview_rating(session)
    fallback_count = get_interview_fallback_evidence(application, stage=session.review_stage).count()
    return {
        "id": session.id,
        "review_stage": session.review_stage,
        "review_stage_label": session.get_review_stage_display(),
        "scheduled_by": str(session.scheduled_by) if session.scheduled_by else "",
        "scheduled_by_role": session.scheduled_by_role,
        "scheduled_for": session.scheduled_for.isoformat(),
        "location": session.location,
        "rating_count": session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": _decimal_string(average_score),
        "finalized_at": session.finalized_at.isoformat() if session.finalized_at else "",
        "is_read_only": session.is_finalized,
    }


def _decision_packet_deliberation_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "deliberated_at": record.deliberated_at.isoformat(),
        "decision_support_summary": record.decision_support_summary,
        "ranking_position": record.ranking_position,
        "ranking_notes": record.ranking_notes,
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "finalized_screening_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_screening_count",
            0,
        ),
        "finalized_exam_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_exam_count",
            0,
        ),
        "finalized_interview_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_interview_count",
            0,
        ),
        "latest_interview_average": record.consolidated_snapshot.get("summary", {}).get(
            "latest_interview_average",
            "",
        ),
        "is_read_only": record.is_finalized,
    }


def get_evidence_items_for_application_context(application):
    return (
        EvidenceVaultItem.objects.select_related(
            "application",
            "application__position",
            "recruitment_case",
            "recruitment_case__application",
            "recruitment_case__application__position",
            "recruitment_entry",
            "uploaded_by",
            "previous_version",
        )
        .filter(_evidence_context_filter_for_application(application))
        .order_by("artifact_scope", "stage", "document_key", "version_number", "created_at", "id")
    )


def _evidence_owner_filters(*, application=None, recruitment_case=None, recruitment_entry=None):
    owner_count = sum(bool(owner is not None) for owner in [application, recruitment_case, recruitment_entry])
    if owner_count != 1:
        raise ValueError(
            "Evidence ownership must target exactly one scope: application, recruitment case, or recruitment entry."
        )
    if application is not None:
        return {
            "artifact_scope": EvidenceVaultItem.OwnerScope.APPLICATION,
            "application": application,
            "recruitment_case": None,
            "recruitment_entry": None,
        }
    if recruitment_case is not None:
        return {
            "artifact_scope": EvidenceVaultItem.OwnerScope.CASE,
            "application": None,
            "recruitment_case": recruitment_case,
            "recruitment_entry": None,
        }
    return {
        "artifact_scope": EvidenceVaultItem.OwnerScope.ENTRY,
        "application": None,
        "recruitment_case": None,
        "recruitment_entry": recruitment_entry,
    }


def _decision_packet_car_item(item):
    application = item.application
    return {
        "id": item.id,
        "rank_order": item.rank_order,
        "recruitment_case_id": item.recruitment_case_id,
        "application_id": application.id,
        "application_reference": application.reference_number or "",
        "applicant_name": application.applicant_display_name,
        "qualification_outcome": item.qualification_outcome,
        "exam_status": item.exam_status,
        "exam_score": _decimal_string(item.exam_score),
        "interview_average_score": _decimal_string(item.interview_average_score),
        "decision_support_summary": item.decision_support_summary,
    }


def _decision_packet_car_report(report):
    items = list(get_comparative_assessment_report_items_for_report(report))
    evidence = report.evidence_item
    return {
        "id": report.id,
        "review_stage": report.review_stage,
        "review_stage_label": report.get_review_stage_display(),
        "generated_by": str(report.generated_by) if report.generated_by else "",
        "generated_by_role": report.generated_by_role,
        "summary_notes": report.summary_notes,
        "version_number": report.version_number,
        "candidate_count": len(items),
        "finalized_at": report.finalized_at.isoformat() if report.finalized_at else "",
        "evidence_item": (
            {
                "id": evidence.id,
                "label": evidence.label,
                "artifact_scope": evidence.artifact_scope,
                "original_filename": evidence.original_filename,
                "sha256_digest": evidence.sha256_digest,
            }
            if evidence
            else {}
        ),
        "items": [_decision_packet_car_item(item) for item in items],
        "is_read_only": report.is_finalized,
    }


def _decision_packet_evidence_reference(evidence):
    application = evidence.owning_application
    recruitment_case = evidence.owning_case
    recruitment_entry = evidence.owning_recruitment_entry
    return {
        "id": evidence.id,
        "label": evidence.label,
        "artifact_scope": evidence.artifact_scope,
        "artifact_scope_label": evidence.get_artifact_scope_display(),
        "artifact_type": evidence.artifact_type,
        "application_id": application.id if application else None,
        "recruitment_case_id": recruitment_case.id if recruitment_case else None,
        "recruitment_entry_id": recruitment_entry.id if recruitment_entry else None,
        "stage": evidence.stage,
        "stage_label": evidence.get_stage_display() if evidence.stage else "",
        "document_key": evidence.document_key,
        "version_family": str(evidence.version_family),
        "version_number": evidence.version_number,
        "is_current_version": evidence.is_current_version,
        "is_archived": evidence.is_archived,
        "archive_tag": evidence.archive_tag,
        "original_filename": evidence.original_filename,
        "uploaded_by_role": evidence.uploaded_by_role,
        "uploaded_at": evidence.created_at.isoformat(),
        "digest_algorithm": evidence.digest_algorithm,
        "sha256_digest": evidence.sha256_digest,
    }


def build_submission_packet(application):
    case = getattr(application, "case", None)
    screening_records = list(
        application.screening_records.filter(is_finalized=True).select_related(
            "reviewed_by",
            "finalized_by",
        ).order_by("created_at")
    )
    exam_records = list(
        application.exam_records.filter(is_finalized=True).select_related(
            "recorded_by",
            "finalized_by",
            "evidence_item",
        ).order_by("created_at")
    )
    interview_sessions = list(
        application.interview_sessions.filter(is_finalized=True).select_related(
            "scheduled_by",
            "finalized_by",
        ).prefetch_related("ratings").order_by("created_at")
    )
    deliberation_record = get_latest_finalized_deliberation_record(application)
    comparative_assessment_report = get_latest_finalized_comparative_assessment_report(application)
    evidence_items = list(get_evidence_items_for_application_context(application))

    missing_components = []
    if not deliberation_record:
        missing_components.append("Finalized deliberation record")
    if application.branch == PositionPosting.Branch.PLANTILLA and not comparative_assessment_report:
        missing_components.append("Finalized Comparative Assessment Report")

    preserved_artifact_ids = {
        "screening_record_ids": [record.id for record in screening_records],
        "exam_record_ids": [record.id for record in exam_records],
        "interview_session_ids": [session.id for session in interview_sessions],
        "deliberation_record_ids": [deliberation_record.id] if deliberation_record else [],
        "comparative_assessment_report_ids": (
            [comparative_assessment_report.id] if comparative_assessment_report else []
        ),
        "evidence_item_ids": [item.id for item in evidence_items],
    }

    return {
        "context": {
            "built_at": timezone.now().isoformat(),
            "application_reference": application.reference_number or "",
            "application_id": application.id,
            "branch": application.branch,
            "branch_label": application.get_branch_display(),
            "level": application.level,
            "level_label": application.get_level_display(),
            "applicant_name": application.applicant_display_name,
            "applicant_email": application.applicant_email,
            "recruitment_entry_code": application.position.job_code,
            "recruitment_entry_title": application.position.title,
            "current_stage": getattr(case, "current_stage", ""),
            "current_stage_label": case.get_current_stage_display() if case else "",
            "case_status": getattr(case, "case_status", ""),
            "case_status_label": case.get_case_status_display() if case else "",
            "current_handler_role": getattr(case, "current_handler_role", ""),
            "locked_stage": getattr(case, "locked_stage", ""),
            "is_stage_locked": getattr(case, "is_stage_locked", False),
        },
        "summary": {
            "ready_for_final_decision": not missing_components,
            "missing_components": missing_components,
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "evidence_reference_count": len(evidence_items),
            "has_deliberation_record": bool(deliberation_record),
            "has_comparative_assessment_report": bool(comparative_assessment_report),
        },
        "screening_records": [_decision_packet_screening_record(record) for record in screening_records],
        "exam_records": [_decision_packet_exam_record(record) for record in exam_records],
        "interview_sessions": [
            _decision_packet_interview_session(application, session) for session in interview_sessions
        ],
        "deliberation_record": (
            _decision_packet_deliberation_record(deliberation_record) if deliberation_record else {}
        ),
        "comparative_assessment_report": (
            _decision_packet_car_report(comparative_assessment_report)
            if comparative_assessment_report
            else {}
        ),
        "evidence_references": [
            _decision_packet_evidence_reference(item) for item in evidence_items
        ],
        "preserved_artifact_ids": preserved_artifact_ids,
    }


def save_screening_review(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before screening can be recorded.")
    review_stage = application.case.current_stage
    screening_record = get_screening_record(application, stage=review_stage)
    if screening_record and screening_record.is_finalized:
        raise ValueError("Finalized screening outputs are locked and cannot be modified.")
    if not user_can_manage_screening(actor, application):
        raise ValueError("You cannot record screening for this application at its current workflow stage.")
    if review_stage not in SCREENING_STAGES:
        raise ValueError("Screening is only available during Secretariat or HRM Chief review stages.")

    created = screening_record is None
    if screening_record is None:
        screening_record = ScreeningRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            reviewed_by=actor,
            branch=application.branch,
            level=application.level,
        )

    screening_record.recruitment_case = application.case
    screening_record.reviewed_by = actor
    screening_record.completeness_status = cleaned_data["completeness_status"]
    screening_record.completeness_notes = cleaned_data["completeness_notes"]
    screening_record.qualification_outcome = cleaned_data["qualification_outcome"]
    screening_record.screening_notes = cleaned_data["screening_notes"]
    screening_record.is_finalized = finalize
    if finalize:
        screening_record.finalized_by = actor
        screening_record.finalized_at = timezone.now()
    else:
        screening_record.finalized_by = None
        screening_record.finalized_at = None
    screening_record.full_clean()
    screening_record.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.SCREENING_FINALIZED
            if finalize
            else AuditLog.Action.SCREENING_RECORDED
        ),
        description=(
            "Finalized screening output."
            if finalize
            else "Saved screening review."
        ),
        metadata={
            "screening_record_id": screening_record.id,
            "created": created,
            "review_stage": review_stage,
            "completeness_status": screening_record.completeness_status,
            "qualification_outcome": screening_record.qualification_outcome,
            "is_finalized": screening_record.is_finalized,
        },
    )
    return screening_record


@transaction.atomic
def save_exam_record(application, actor, cleaned_data, finalize=False, evidence_file=None):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before examination data can be recorded.")
    review_stage = application.case.current_stage
    exam_record = get_exam_record(application, stage=review_stage)
    if exam_record and exam_record.is_finalized:
        raise ValueError("Finalized examination outputs are locked and cannot be modified.")
    if not user_can_manage_exam(actor, application):
        raise ValueError(
            "You cannot record examination data for this application at its current workflow stage."
        )
    if review_stage not in EXAM_STAGES:
        raise ValueError("Examination records are only available during Secretariat or HRM Chief review stages.")

    created = exam_record is None
    if exam_record is None:
        exam_record = ExamRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            recorded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    exam_record.recruitment_case = application.case
    exam_record.recorded_by = actor
    exam_record.exam_type = cleaned_data["exam_type"]
    exam_record.exam_status = cleaned_data["exam_status"]
    exam_record.exam_score = cleaned_data.get("exam_score")
    exam_record.exam_result = cleaned_data.get("exam_result", "")
    exam_record.technical_score = cleaned_data.get("technical_score")
    exam_record.technical_result = cleaned_data.get("technical_result", "")
    exam_record.practical_score = cleaned_data.get("practical_score")
    exam_record.practical_result = cleaned_data.get("practical_result", "")
    exam_record.exam_date = cleaned_data.get("exam_date")
    exam_record.administered_by = cleaned_data.get("administered_by", "")
    exam_record.valid_from = cleaned_data.get("valid_from")
    exam_record.valid_until = cleaned_data.get("valid_until")
    exam_record.exam_notes = cleaned_data.get("exam_notes", "")
    exam_record.is_finalized = finalize
    if finalize:
        exam_record.finalized_by = actor
        exam_record.finalized_at = timezone.now()
    else:
        exam_record.finalized_by = None
        exam_record.finalized_at = None
    exam_record.full_clean()
    exam_record.save()

    evidence = None
    if evidence_file:
        evidence = upload_evidence_item(
            application=application,
            actor=actor,
            label=f"Exam Evidence - {exam_record.exam_type}",
            uploaded_file=evidence_file,
            document_key=f"exam-record-{review_stage}",
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type=ARTIFACT_TYPE_EXAM_EVIDENCE,
        )
        exam_record.evidence_item = evidence
        exam_record.full_clean()
        exam_record.save(update_fields=["evidence_item", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.EXAM_FINALIZED
            if finalize
            else AuditLog.Action.EXAM_RECORDED
        ),
        description=(
            "Finalized examination output."
            if finalize
            else "Saved examination record."
        ),
        metadata={
            "exam_record_id": exam_record.id,
            "created": created,
            "review_stage": review_stage,
            "exam_type": exam_record.exam_type,
            "exam_status": exam_record.exam_status,
            "exam_score": _decimal_string(exam_record.effective_score),
            "exam_result": exam_record.exam_result,
            "technical_score": _decimal_string(exam_record.technical_score),
            "technical_result": exam_record.technical_result,
            "practical_score": _decimal_string(exam_record.practical_score),
            "practical_result": exam_record.practical_result,
            "exam_date": exam_record.exam_date.isoformat() if exam_record.exam_date else "",
            "administered_by": exam_record.administered_by,
            "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
            "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
            "evidence_id": evidence.id if evidence else exam_record.evidence_item_id,
            "evidence_uploaded": bool(evidence),
            "is_finalized": exam_record.is_finalized,
        },
    )
    return exam_record


@transaction.atomic
def save_interview_session(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before interview scheduling can be recorded.")
    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot be modified.")
    if not user_can_manage_interview_session(actor, application):
        raise ValueError(
            "You cannot manage interview scheduling for this application at its current workflow stage."
        )
    if review_stage not in INTERVIEW_SESSION_STAGES:
        raise ValueError(
            "Interview sessions are only available during Secretariat, HRM Chief, or HRMPSB review stages."
        )

    created = interview_session is None
    if interview_session is None:
        interview_session = InterviewSession(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            scheduled_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_session.recruitment_case = application.case
    interview_session.recruitment_entry = application.position
    interview_session.scheduled_by = actor
    interview_session.scheduled_for = cleaned_data["scheduled_for"]
    interview_session.location = cleaned_data["location"]
    interview_session.session_notes = cleaned_data["session_notes"]

    existing_rating_count = interview_session.ratings.count() if interview_session.pk else 0
    fallback_count = get_interview_fallback_evidence(application, stage=review_stage).count()
    if finalize and existing_rating_count == 0 and fallback_count == 0:
        raise ValueError(
            "Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session."
        )

    interview_session.is_finalized = finalize
    if finalize:
        interview_session.finalized_by = actor
        interview_session.finalized_at = timezone.now()
    else:
        interview_session.finalized_by = None
        interview_session.finalized_at = None
    interview_session.full_clean()
    interview_session.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.INTERVIEW_FINALIZED
            if finalize
            else AuditLog.Action.INTERVIEW_SCHEDULED
        ),
        description=(
            "Finalized interview session output."
            if finalize
            else "Saved interview session schedule."
        ),
        metadata={
            "interview_session_id": interview_session.id,
            "created": created,
            "review_stage": review_stage,
            "scheduled_for": interview_session.scheduled_for.isoformat(),
            "location": interview_session.location,
            "rating_count": existing_rating_count,
            "fallback_count": fallback_count,
            "is_finalized": interview_session.is_finalized,
        },
    )
    return interview_session


@transaction.atomic
def save_interview_rating(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before interview ratings can be recorded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot accept rating changes.")
    if not user_can_manage_interview_rating(actor, application):
        raise ValueError(
            "You cannot record interview ratings for this application at its current workflow stage."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before recording interview ratings.")

    interview_rating = interview_session.ratings.filter(rated_by=actor).first()
    created = interview_rating is None
    if interview_rating is None:
        interview_rating = InterviewRating(
            interview_session=interview_session,
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            rated_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_rating.rating_score = cleaned_data["rating_score"]
    interview_rating.rating_notes = cleaned_data["rating_notes"]
    interview_rating.justification = cleaned_data["justification"]
    interview_rating.full_clean()
    interview_rating.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
        description="Recorded interview rating.",
        metadata={
            "interview_session_id": interview_session.id,
            "interview_rating_id": interview_rating.id,
            "created": created,
            "review_stage": review_stage,
            "rating_score": str(interview_rating.rating_score),
            "has_justification": bool(interview_rating.justification),
        },
    )
    return interview_rating


@transaction.atomic
def upload_interview_fallback_rating(application, actor, uploaded_file, remarks):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before fallback interview ratings can be uploaded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview sessions are locked and cannot accept fallback rating uploads.")
    if not user_can_upload_interview_fallback(actor, application):
        raise ValueError(
            "You cannot upload a fallback interview rating sheet for this application at its current workflow stage."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before uploading a fallback rating sheet.")

    evidence = upload_evidence_item(
        application=application,
        actor=actor,
        label=f"{INTERVIEW_FALLBACK_LABEL} - {application.case.get_current_stage_display()}",
        uploaded_file=uploaded_file,
        artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
        artifact_type=ARTIFACT_TYPE_INTERVIEW_FALLBACK,
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
        description="Uploaded fallback interview rating sheet.",
        metadata={
            "interview_session_id": interview_session.id,
            "evidence_id": evidence.id,
            "review_stage": review_stage,
            "remarks": remarks,
            "filename": evidence.original_filename,
        },
    )
    return evidence


def _finalized_deliberation_queryset_for_entry(recruitment_entry, review_stage):
    return DeliberationRecord.objects.filter(
        recruitment_entry=recruitment_entry,
        review_stage=review_stage,
        is_finalized=True,
    ).select_related(
        "application",
        "recruitment_case",
        "recruitment_entry",
        "recorded_by",
        "finalized_by",
    ).order_by("ranking_position", "application__reference_number")


@transaction.atomic
def save_deliberation_record(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before deliberation data can be recorded.")
    if not user_can_manage_deliberation(actor, application):
        raise ValueError(
            "You cannot manage deliberation records for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch, "")
    if review_stage != expected_stage:
        raise ValueError(
            "Deliberation is only available during the branch-appropriate decision-support stage."
        )

    deliberation_record = get_deliberation_record(application, stage=review_stage)
    if deliberation_record and deliberation_record.is_finalized:
        raise ValueError("Finalized deliberation records are locked and cannot be modified.")

    created = deliberation_record is None
    if deliberation_record is None:
        deliberation_record = DeliberationRecord(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            recorded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    current_screening_record = get_screening_record(application, stage=review_stage)
    current_exam_record = get_exam_record(application, stage=review_stage)
    current_interview_session = get_interview_session(application, stage=review_stage)
    if (
        finalize
        and review_stage in SCREENING_STAGES
        and (not current_screening_record or not current_screening_record.is_finalized)
    ):
        raise ValueError("Finalize the screening record before finalizing the deliberation record.")
    if finalize and current_exam_record and not current_exam_record.is_finalized:
        raise ValueError("Finalize the examination record before finalizing the deliberation record.")
    if finalize and current_interview_session and not current_interview_session.is_finalized:
        raise ValueError("Finalize the interview session before finalizing the deliberation record.")

    consolidated_snapshot = build_deliberation_consolidation(application)
    consolidated_source_count = (
        len(consolidated_snapshot["screening_records"])
        + len(consolidated_snapshot["exam_records"])
        + len(consolidated_snapshot["interview_sessions"])
    )
    if finalize and consolidated_source_count == 0:
        raise ValueError(
            "Finalize at least one screening, examination, or interview output before finalizing deliberation."
        )
    if (
        finalize
        and application.branch == PositionPosting.Branch.PLANTILLA
        and not cleaned_data.get("ranking_position")
    ):
        raise ValueError(
            "Record the ranking position before finalizing the Plantilla deliberation record."
        )

    deliberation_record.recruitment_case = application.case
    deliberation_record.recruitment_entry = application.position
    deliberation_record.recorded_by = actor
    deliberation_record.deliberated_at = cleaned_data["deliberated_at"]
    deliberation_record.deliberation_minutes = cleaned_data["deliberation_minutes"]
    deliberation_record.decision_support_summary = cleaned_data["decision_support_summary"]
    deliberation_record.ranking_position = cleaned_data["ranking_position"]
    deliberation_record.ranking_notes = cleaned_data["ranking_notes"]
    deliberation_record.consolidated_snapshot = consolidated_snapshot
    deliberation_record.is_finalized = finalize
    if finalize:
        deliberation_record.finalized_by = actor
        deliberation_record.finalized_at = timezone.now()
    else:
        deliberation_record.finalized_by = None
        deliberation_record.finalized_at = None
    deliberation_record.full_clean()
    deliberation_record.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.DELIBERATION_FINALIZED
            if finalize
            else AuditLog.Action.DELIBERATION_RECORDED
        ),
        description=(
            "Finalized deliberation and decision-support record."
            if finalize
            else "Saved deliberation and decision-support record."
        ),
        metadata={
            "deliberation_record_id": deliberation_record.id,
            "created": created,
            "review_stage": review_stage,
            "ranking_position": deliberation_record.ranking_position,
            "finalized_screening_count": len(consolidated_snapshot["screening_records"]),
            "finalized_exam_count": len(consolidated_snapshot["exam_records"]),
            "finalized_interview_count": len(consolidated_snapshot["interview_sessions"]),
            "is_finalized": deliberation_record.is_finalized,
        },
    )
    return deliberation_record


def _car_candidate_rows(recruitment_entry, review_stage):
    rows = []
    deliberation_records = list(_finalized_deliberation_queryset_for_entry(recruitment_entry, review_stage))
    if not deliberation_records:
        raise ValueError(
            "Finalize at least one Plantilla deliberation record before generating the Comparative Assessment Report."
        )
    if any(record.ranking_position is None for record in deliberation_records):
        raise ValueError(
            "All finalized Plantilla deliberation records for this recruitment entry must include a ranking position before CAR generation."
        )
    rank_positions = [record.ranking_position for record in deliberation_records]
    if len(rank_positions) != len(set(rank_positions)):
        raise ValueError(
            "Comparative Assessment Report generation requires unique ranking positions within the same recruitment entry."
        )

    for record in deliberation_records:
        summary = record.consolidated_snapshot.get("summary", {})
        rows.append(
            {
                "application": record.application,
                "recruitment_case": record.recruitment_case,
                "deliberation_record": record,
                "rank_order": record.ranking_position,
                "qualification_outcome": summary.get("latest_qualification_outcome", ""),
                "exam_status": summary.get("latest_exam_status", ""),
                "exam_score": summary.get("latest_exam_score", ""),
                "exam_components": summary.get("latest_exam_components", ""),
                "interview_average_score": summary.get("latest_interview_average", ""),
                "decision_support_summary": record.decision_support_summary,
            }
        )
    return sorted(rows, key=lambda row: (row["rank_order"], row["application"].reference_number or ""))


@transaction.atomic
def generate_comparative_assessment_report(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before a Comparative Assessment Report can be generated.")
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "You cannot generate the Comparative Assessment Report for this application at its current workflow stage."
        )

    review_stage = application.case.current_stage
    if review_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError(
            "Comparative Assessment Report generation is only available for Plantilla cases at the HRMPSB stage."
        )

    deliberation_record = get_deliberation_record(application, stage=review_stage)
    if not deliberation_record or not deliberation_record.is_finalized:
        raise ValueError(
            "Finalize the deliberation record before generating the Comparative Assessment Report."
        )

    candidate_rows = _car_candidate_rows(application.position, review_stage)
    latest_report = get_comparative_assessment_report(application, stage=review_stage)
    version_number = (latest_report.version_number + 1) if latest_report else 1
    consolidated_snapshot = {
        "generated_at": timezone.now().isoformat(),
        "entry_code": application.position.job_code,
        "review_stage": review_stage,
        "candidate_count": len(candidate_rows),
        "ranked_candidates": [
            {
                "rank_order": row["rank_order"],
                "application_reference": row["application"].reference_number or "",
                "applicant_name": row["application"].applicant_display_name,
                "qualification_outcome": row["qualification_outcome"],
                "exam_status": row["exam_status"],
                "exam_score": row["exam_score"],
                "exam_components": row["exam_components"],
                "interview_average_score": row["interview_average_score"],
            }
            for row in candidate_rows
        ],
    }

    pdf_bytes = _build_comparative_assessment_report_pdf(
        application=application,
        actor=actor,
        candidate_rows=candidate_rows,
        generation_number=version_number,
        summary_notes=cleaned_data["summary_notes"],
    )
    evidence = store_generated_evidence_item(
        application=application,
        actor=actor,
        label=f"{CAR_LABEL} - {application.case.get_current_stage_display()}",
        filename=f"{application.position.job_code.lower()}-{review_stage.replace('_', '-')}-car-v{version_number}.pdf",
        raw_bytes=pdf_bytes,
        content_type="application/pdf",
        recruitment_entry=application.position,
        artifact_scope=EvidenceVaultItem.OwnerScope.ENTRY,
        artifact_type=ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
        stage=review_stage,
        document_key=ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
    )

    report = ComparativeAssessmentReport(
        recruitment_entry=application.position,
        review_stage=review_stage,
        generated_by=actor,
        branch=application.branch,
        summary_notes=cleaned_data["summary_notes"],
        consolidated_snapshot=consolidated_snapshot,
        version_number=version_number,
        evidence_item=evidence,
        is_finalized=finalize,
    )
    if finalize:
        report.finalized_by = actor
        report.finalized_at = timezone.now()
    report.full_clean()
    report.save()

    for row in candidate_rows:
        item = ComparativeAssessmentReportItem(
            report=report,
            recruitment_case=row["recruitment_case"],
            deliberation_record=row["deliberation_record"],
            rank_order=row["rank_order"],
            qualification_outcome=row["qualification_outcome"],
            exam_status=row["exam_status"],
            exam_score=Decimal(row["exam_score"]) if row["exam_score"] else None,
            interview_average_score=(
                Decimal(row["interview_average_score"]) if row["interview_average_score"] else None
            ),
            decision_support_summary=row["decision_support_summary"],
        )
        item.full_clean()
        item.save()

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CAR_FINALIZED if finalize else AuditLog.Action.CAR_GENERATED,
        description=(
            "Finalized the Comparative Assessment Report."
            if finalize
            else "Generated or updated the Comparative Assessment Report."
        ),
        metadata={
            "car_report_id": report.id,
            "review_stage": review_stage,
            "version_number": report.version_number,
            "candidate_count": len(candidate_rows),
            "evidence_id": evidence.id,
            "is_finalized": report.is_finalized,
        },
    )
    return report


@transaction.atomic
def save_completion_tracking(application, actor, cleaned_data, requirement_formset):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before completion tracking can be recorded.")
    if not user_can_manage_completion(actor, application):
        raise ValueError(
            "You cannot manage completion tracking for this application at its current workflow stage."
        )

    completion_record = get_completion_record(application)
    created = completion_record is None
    if completion_record is None:
        completion_record = CompletionRecord(
            application=application,
            recruitment_case=application.case,
            tracked_by=actor,
        )

    completion_record.recruitment_case = application.case
    completion_record.tracked_by = actor
    completion_record.branch = application.branch
    completion_record.level = application.level
    completion_record.completion_reference = cleaned_data.get("completion_reference", "")
    completion_record.completion_date = cleaned_data.get("completion_date")
    completion_record.deadline = cleaned_data.get("deadline")
    completion_record.remarks = cleaned_data.get("remarks", "")
    if application.branch == PositionPosting.Branch.PLANTILLA:
        completion_record.announcement_reference = cleaned_data.get("announcement_reference", "")
        completion_record.announcement_date = cleaned_data.get("announcement_date")
    else:
        completion_record.announcement_reference = ""
        completion_record.announcement_date = None
    completion_record.full_clean()
    completion_record.save()

    active_requirement_ids = []
    requirement_count = 0
    resolved_requirement_count = 0
    for form in requirement_formset.forms:
        if not hasattr(form, "cleaned_data") or not form.cleaned_data:
            continue
        if form.cleaned_data.get("DELETE"):
            if form.instance.pk:
                form.instance.delete()
            continue
        item_label = (form.cleaned_data.get("item_label") or "").strip()
        if not item_label:
            continue

        requirement = form.save(commit=False)
        requirement.completion_record = completion_record
        requirement.display_order = requirement_count
        requirement.full_clean()
        requirement.save()
        active_requirement_ids.append(requirement.pk)
        requirement_count += 1
        if requirement.status != CompletionRequirement.RequirementStatus.PENDING:
            resolved_requirement_count += 1

    completion_record.requirements.exclude(pk__in=active_requirement_ids).delete()

    if requirement_count == 0:
        raise ValidationError("Add at least one completion requirement item.")

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.COMPLETION_RECORDED,
        description=(
            "Saved appointment completion tracking."
            if application.branch == PositionPosting.Branch.PLANTILLA
            else "Saved contract completion tracking."
        ),
        metadata={
            "completion_record_id": completion_record.id,
            "created": created,
            "case_stage": application.case.current_stage,
            "case_status": application.case.case_status,
            "deadline": completion_record.deadline.isoformat() if completion_record.deadline else "",
            "completion_reference": completion_record.completion_reference,
            "completion_date": (
                completion_record.completion_date.isoformat()
                if completion_record.completion_date
                else ""
            ),
            "announcement_reference": completion_record.announcement_reference,
            "announcement_date": (
                completion_record.announcement_date.isoformat()
                if completion_record.announcement_date
                else ""
            ),
            "requirement_count": requirement_count,
            "resolved_requirement_count": resolved_requirement_count,
        },
    )
    return completion_record


def _upsert_recruitment_case_for_submission(application, actor, next_role, next_status):
    next_stage = _review_stage_from_application_status(next_status)
    if not next_stage:
        raise ValueError("Submitted applications must route to a valid internal review stage.")

    case, created = RecruitmentCase.objects.get_or_create(
        application=application,
        defaults={
            "branch": application.branch,
            "current_stage": next_stage,
            "current_handler_role": next_role,
            "case_status": RecruitmentCase.CaseStatus.ACTIVE,
            "is_stage_locked": False,
            "locked_stage": "",
        },
    )
    if not created:
        if case.case_status != RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT:
            raise ValueError("A recruitment case already exists for this application.")
        case.current_stage = next_stage
        case.current_handler_role = next_role
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
        case.reopened_at = timezone.now()
        case.save(
            update_fields=[
                "branch",
                "current_stage",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "closed_at",
                "reopened_at",
                "updated_at",
            ]
      )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_CREATED,
        description="Recruitment case created from the finalized application.",
        metadata={"created": created, **_case_timeline_metadata(case)},
    )
    return case


def _sync_case_after_workflow_action(application, actor, next_role, next_status, remarks):
    case = application.case
    _ensure_case_stage_alignment(application, case)
    previous_stage = case.current_stage
    previous_case_status = case.case_status

    next_stage = _review_stage_from_application_status(next_status)
    if next_stage:
        case.current_stage = next_stage
        case.case_status = RecruitmentCase.CaseStatus.ACTIVE
        case.current_handler_role = next_role
        case.is_stage_locked = False
        case.locked_stage = ""
        case.closed_at = None
    elif next_status == RecruitmentApplication.Status.RETURNED_TO_APPLICANT:
        case.case_status = RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT
        case.current_handler_role = RecruitmentUser.Role.APPLICANT
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = None
    elif next_status == RecruitmentApplication.Status.APPROVED:
        if next_role:
            case.current_stage = RecruitmentCase.Stage.COMPLETION
            case.case_status = RecruitmentCase.CaseStatus.ACTIVE
            case.current_handler_role = next_role
            case.is_stage_locked = False
            case.locked_stage = ""
            case.closed_at = None
        else:
            case.current_stage = RecruitmentCase.Stage.CLOSED
            case.case_status = RecruitmentCase.CaseStatus.APPROVED
            case.current_handler_role = ""
            case.is_stage_locked = True
            case.locked_stage = previous_stage
            case.closed_at = timezone.now()
    elif next_status == RecruitmentApplication.Status.REJECTED:
        case.current_stage = RecruitmentCase.Stage.CLOSED
        case.case_status = RecruitmentCase.CaseStatus.REJECTED
        case.current_handler_role = ""
        case.is_stage_locked = True
        case.locked_stage = previous_stage
        case.closed_at = timezone.now()
    else:
        raise ValueError("Unsupported recruitment case transition target.")

    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "updated_at",
        ]
    )
    return {
        "previous_stage": previous_stage,
        "previous_case_status": previous_case_status,
        "case": case,
    }


def get_public_recruitment_entries(branch=None):
    entries = PositionPosting.objects.filter(
        status=PositionPosting.EntryStatus.ACTIVE,
    ).select_related("position_reference")
    if branch:
        entries = entries.filter(branch=branch)
    return [entry for entry in entries.order_by("branch", "title") if entry.is_open_for_intake]


def _build_portal_applicant_username():
    return f"portal-{uuid.uuid4().hex[:12]}"


def normalize_applicant_email(email):
    return (email or "").strip().lower()


def create_portal_applicant_identity(first_name, last_name, email, phone):
    applicant = RecruitmentUser(
        username=_build_portal_applicant_username(),
        first_name=first_name,
        last_name=last_name,
        email=email,
        office_name="Public Applicant",
        employee_id="",
        role=RecruitmentUser.Role.APPLICANT,
        is_active=False,
    )
    applicant.set_unusable_password()
    applicant.save()
    return applicant


def get_reusable_public_application_draft(entry, applicant_email):
    return (
        RecruitmentApplication.objects.select_related("applicant", "position")
        .filter(
            position=entry,
            applicant_email__iexact=applicant_email,
            submitted_at__isnull=True,
            status=RecruitmentApplication.Status.DRAFT,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )


def get_portal_applicant_identity_by_email(applicant_email):
    return (
        RecruitmentUser.objects.filter(
            role=RecruitmentUser.Role.APPLICANT,
            email__iexact=applicant_email,
        )
        .order_by("-is_active", "id")
        .first()
    )


def _sync_portal_applicant_identity(applicant, *, first_name, last_name, email):
    changed_fields = []
    if applicant.first_name != first_name:
        applicant.first_name = first_name
        changed_fields.append("first_name")
    if applicant.last_name != last_name:
        applicant.last_name = last_name
        changed_fields.append("last_name")
    if normalize_applicant_email(applicant.email) != email:
        applicant.email = email
        changed_fields.append("email")
    if not applicant.is_active and applicant.office_name != "Public Applicant":
        applicant.office_name = "Public Applicant"
        changed_fields.append("office_name")
    if not applicant.is_active and applicant.employee_id:
        applicant.employee_id = ""
        changed_fields.append("employee_id")
    if changed_fields:
        applicant.save(update_fields=changed_fields)
    return applicant


def get_current_applicant_document_items(application):
    return EvidenceVaultItem.objects.filter(
        application=application,
        artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
        artifact_type=ARTIFACT_TYPE_APPLICANT_DOCUMENT,
        stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
        is_current_version=True,
        is_archived=False,
    ).order_by("document_key", "-version_number", "-created_at")


def get_current_applicant_document_map(application):
    return {
        evidence.document_key: evidence
        for evidence in get_current_applicant_document_items(application)
    }


def get_applicant_document_review_items(application):
    current_documents = get_current_applicant_document_map(application)
    review_items = []
    for requirement in get_applicant_document_requirements(application.branch):
        evidence = current_documents.get(requirement.code)
        is_not_applicable = (
            requirement.conditional_on_performance_rating
            and application.performance_rating_not_applicable
            and evidence is None
        )
        review_items.append(
            {
                "requirement": requirement,
                "evidence": evidence,
                "is_submitted": evidence is not None,
                "is_not_applicable": is_not_applicable,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
            }
        )
    return review_items


def get_missing_required_applicant_document_requirements(application):
    present_document_codes = set(get_current_applicant_document_map(application))
    return [
        requirement
        for requirement in get_required_applicant_document_requirements(
            branch=application.branch,
            performance_rating_not_applicable=application.performance_rating_not_applicable,
        )
        if requirement.code not in present_document_codes
    ]


def _upsert_public_application_draft(
    entry,
    cleaned_data,
    requirement_uploads,
    *,
    issue_otp=False,
    created_description="Applicant created an accountless application draft.",
    updated_description="Applicant reused and refreshed an existing accountless application draft.",
):
    applicant_email = normalize_applicant_email(cleaned_data["email"])
    if RecruitmentApplication.objects.filter(
        position=entry,
        applicant_email__iexact=applicant_email,
        submitted_at__isnull=False,
    ).exists():
        raise ValueError(
            "An application for this recruitment entry has already been submitted using this email address."
        )

    application = get_reusable_public_application_draft(entry, applicant_email)
    created = application is None
    if application is None:
        applicant_user = get_portal_applicant_identity_by_email(applicant_email)
        if applicant_user is None:
            applicant_user = create_portal_applicant_identity(
                first_name=cleaned_data["first_name"],
                last_name=cleaned_data["last_name"],
                email=applicant_email,
                phone=cleaned_data["phone"],
            )
        else:
            _sync_portal_applicant_identity(
                applicant_user,
                first_name=cleaned_data["first_name"],
                last_name=cleaned_data["last_name"],
                email=applicant_email,
            )
        application = RecruitmentApplication(applicant=applicant_user, position=entry)
    else:
        applicant_user = application.applicant
        _sync_portal_applicant_identity(
            applicant_user,
            first_name=cleaned_data["first_name"],
            last_name=cleaned_data["last_name"],
            email=applicant_email,
        )

    application.applicant = applicant_user
    application.position = entry
    application.status = RecruitmentApplication.Status.DRAFT
    application.current_handler_role = ""
    application.qualification_summary = cleaned_data["qualification_summary"]
    application.cover_letter = cleaned_data.get("cover_letter", "")
    application.applicant_first_name = cleaned_data["first_name"]
    application.applicant_last_name = cleaned_data["last_name"]
    application.applicant_email = applicant_email
    application.applicant_phone = cleaned_data["phone"]
    application.performance_rating_applicability = (
        cleaned_data.get("performance_rating_applicability")
        or application.performance_rating_applicability
    )
    application.checklist_privacy_consent = bool(
        cleaned_data.get("checklist_privacy_consent")
    )
    application.checklist_documents_complete = bool(
        cleaned_data.get("checklist_documents_complete")
    )
    application.checklist_information_certified = bool(
        cleaned_data.get("checklist_information_certified")
    )
    application.submission_hash = ""
    application.submitted_at = None
    application.closed_at = None
    application.save()
    requirement_catalog = {
        requirement.code: requirement
        for requirement in get_applicant_document_requirements(entry.branch)
    }
    for requirement_code, uploaded_file in requirement_uploads.items():
        requirement = requirement_catalog[requirement_code]
        upload_evidence_item(
            application=application,
            actor=applicant_user,
            label=requirement.title,
            uploaded_file=uploaded_file,
            document_key=requirement.code,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type=ARTIFACT_TYPE_APPLICANT_DOCUMENT,
        )
    record_audit_event(
        application=application,
        actor=applicant_user,
        action=AuditLog.Action.APPLICATION_CREATED if created else AuditLog.Action.APPLICATION_UPDATED,
        description=created_description if created else updated_description,
        metadata={
            "public_token": str(application.public_token),
            "reused_draft": not created,
            "applicant_identity_id": applicant_user.id,
            "saved_requirement_codes": sorted(requirement_uploads.keys()),
            "otp_requested": bool(issue_otp),
        },
    )
    if issue_otp:
        issue_application_otp(application, actor=applicant_user)
    return application


@transaction.atomic
def save_public_application_draft_progress(entry, cleaned_data, requirement_uploads):
    return _upsert_public_application_draft(
        entry,
        cleaned_data,
        requirement_uploads,
        issue_otp=False,
        created_description="Applicant saved valid requirement-coded files to a draft while correcting the form.",
        updated_description="Applicant updated a draft with valid requirement-coded files while correcting the form.",
    )


def _hash_application_otp(application, otp_code):
    payload = "|".join(
        [
            str(application.public_token),
            application.applicant_email.lower(),
            otp_code,
            settings.APPLICATION_OTP_HASH_SECRET,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generate_otp_code():
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _deliver_application_otp(application, otp_code, *, actor=None, otp_expires_at=None):
    send_mail(
        subject="RecruitGuard-CHD applicant verification code",
        message=(
            "Your RecruitGuard-CHD one-time password is "
            f"{otp_code}. It expires in {settings.APPLICATION_OTP_VALIDITY_MINUTES} minutes."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[application.applicant_email],
        fail_silently=False,
    )
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_SENT,
        description="Sent applicant OTP for final submission verification.",
        metadata={"otp_expires_at": (otp_expires_at or application.otp_expires_at).isoformat()},
    )


def issue_application_otp(application, actor=None):
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("OTP can only be issued while the application is still in draft.")

    otp_code = _generate_otp_code()
    now = timezone.now()
    application.otp_hash = _hash_application_otp(application, otp_code)
    application.otp_requested_at = now
    application.otp_expires_at = now + timedelta(minutes=settings.APPLICATION_OTP_VALIDITY_MINUTES)
    application.otp_verified_at = None
    application.save(
        update_fields=[
            "otp_hash",
            "otp_requested_at",
            "otp_expires_at",
            "otp_verified_at",
            "updated_at",
        ]
    )
    otp_expires_at = application.otp_expires_at

    def deliver_otp():
        _deliver_application_otp(
            application,
            otp_code,
            actor=actor,
            otp_expires_at=otp_expires_at,
        )

    connection = transaction.get_connection(using=application._state.db or "default")
    if connection.in_atomic_block:
        transaction.on_commit(deliver_otp, using=connection.alias)
    else:
        deliver_otp()
    return otp_code


def verify_application_otp(application, otp_code, actor=None):
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("OTP verification is only available before final submission.")
    if not application.otp_hash or not application.otp_expires_at:
        raise ValueError("Request an OTP first before attempting verification.")
    if application.otp_expires_at < timezone.now():
        raise ValueError("The OTP has expired. Request a new code before final submission.")

    expected_hash = _hash_application_otp(application, otp_code)
    if not hmac.compare_digest(application.otp_hash, expected_hash):
        raise ValueError("The OTP is invalid.")

    application.otp_verified_at = timezone.now()
    application.save(update_fields=["otp_verified_at", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
        description="Applicant OTP verified successfully.",
        metadata={"verified_at": application.otp_verified_at.isoformat()},
    )
    return application


@transaction.atomic
def create_public_application_draft(entry, cleaned_data, requirement_uploads):
    if not entry.is_open_for_intake:
        raise ValueError("The selected recruitment entry is not currently open for intake.")
    return _upsert_public_application_draft(
        entry,
        cleaned_data,
        requirement_uploads,
        issue_otp=True,
    )


def _get_aes_key():
    return hashlib.sha256(settings.EVIDENCE_ENCRYPTION_SECRET.encode("utf-8")).digest()


def encrypt_evidence_bytes(raw_bytes):
    nonce = os.urandom(12)
    cipher = AESGCM(_get_aes_key())
    return nonce, cipher.encrypt(nonce, raw_bytes, None)


def _decrypt_evidence_bytes(evidence):
    cipher = AESGCM(_get_aes_key())
    return cipher.decrypt(bytes(evidence.nonce), bytes(evidence.ciphertext), None)


def decrypt_evidence_bytes(evidence, actor):
    context_application = get_evidence_context_application_for_user(actor, evidence)
    if context_application is None:
        raise ValueError("You cannot access this evidence item.")
    plaintext = _decrypt_evidence_bytes(evidence)
    record_audit_event(
        application=context_application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_DOWNLOADED,
        description=f"Downloaded evidence '{evidence.label}' {evidence.version_label}.",
        metadata={
            "evidence_id": evidence.id,
            "filename": evidence.original_filename,
            "stage": evidence.stage,
            "artifact_scope": evidence.artifact_scope,
            "artifact_type": evidence.artifact_type,
            "version_family": str(evidence.version_family),
            "version_number": evidence.version_number,
            "is_archived": evidence.is_archived,
        },
    )
    return plaintext


def store_generated_evidence_item(
    application,
    actor,
    label,
    filename,
    raw_bytes,
    content_type="",
    artifact_type="",
    artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
    recruitment_case=None,
    recruitment_entry=None,
    stage="",
    document_key="",
):
    if artifact_scope == EvidenceVaultItem.OwnerScope.CASE and recruitment_case is None:
        raise ValueError("Case-owned generated evidence must point to a recruitment case.")
    if artifact_scope == EvidenceVaultItem.OwnerScope.ENTRY and recruitment_entry is None:
        raise ValueError("Entry-owned generated evidence must point to a recruitment entry.")
    owner_kwargs = _evidence_owner_filters(
        application=application if artifact_scope == EvidenceVaultItem.OwnerScope.APPLICATION else None,
        recruitment_case=(
            recruitment_case if artifact_scope == EvidenceVaultItem.OwnerScope.CASE else None
        ),
        recruitment_entry=(
            recruitment_entry if artifact_scope == EvidenceVaultItem.OwnerScope.ENTRY else None
        ),
    )
    if (
        owner_kwargs["artifact_scope"] == EvidenceVaultItem.OwnerScope.CASE
        and owner_kwargs["recruitment_case"].application_id != application.id
    ):
        raise ValueError("Case-owned evidence must stay linked to the recruitment case of the same application.")
    if (
        owner_kwargs["artifact_scope"] == EvidenceVaultItem.OwnerScope.ENTRY
        and owner_kwargs["recruitment_entry"].id != application.position_id
    ):
        raise ValueError("Entry-owned evidence must stay linked to the same recruitment entry as the application.")
    if not stage:
        stage = _evidence_stage_for_application(application)
    sha256_digest = hashlib.sha256(raw_bytes).hexdigest()
    nonce, ciphertext = encrypt_evidence_bytes(raw_bytes)
    document_key = document_key or EvidenceVaultItem.build_document_key(label)
    previous_version = EvidenceVaultItem.objects.filter(
        artifact_scope=owner_kwargs["artifact_scope"],
        application=owner_kwargs["application"],
        recruitment_case=owner_kwargs["recruitment_case"],
        recruitment_entry=owner_kwargs["recruitment_entry"],
        stage=stage,
        document_key=document_key,
        is_current_version=True,
    ).order_by("-version_number", "-created_at").first()
    version_family = previous_version.version_family if previous_version else uuid.uuid4()
    version_number = previous_version.version_number + 1 if previous_version else 1
    evidence = EvidenceVaultItem(
        application=owner_kwargs["application"],
        recruitment_case=owner_kwargs["recruitment_case"],
        recruitment_entry=owner_kwargs["recruitment_entry"],
        artifact_scope=owner_kwargs["artifact_scope"],
        artifact_type=artifact_type or ARTIFACT_TYPE_WORKFLOW_EVIDENCE,
        stage=stage,
        uploaded_by=actor,
        uploaded_by_role=getattr(actor, "role", ""),
        label=label,
        document_key=document_key,
        version_family=version_family,
        version_number=version_number,
        previous_version=previous_version,
        is_current_version=True,
        original_filename=filename,
        content_type=content_type or "",
        size_bytes=len(raw_bytes),
        digest_algorithm="sha256",
        sha256_digest=sha256_digest,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    evidence.full_clean()
    evidence.save()
    if previous_version:
        previous_version.is_current_version = False
        previous_version.save(update_fields=["is_current_version", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_UPLOADED,
        description=f"Uploaded evidence '{label}' {evidence.version_label}.",
        metadata={
            "evidence_id": evidence.id,
            "artifact_scope": evidence.artifact_scope,
            "artifact_type": evidence.artifact_type,
            "case_id": evidence.recruitment_case_id,
            "entry_id": evidence.recruitment_entry_id,
            "stage": evidence.stage,
            "document_key": evidence.document_key,
            "version_family": str(evidence.version_family),
            "version_number": evidence.version_number,
            "previous_version_id": previous_version.id if previous_version else None,
            "sha256": sha256_digest,
        },
    )
    return evidence


@transaction.atomic
def upload_evidence_item(
    application,
    actor,
    label,
    uploaded_file,
    document_key="",
    artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
    artifact_type="",
):
    if not user_can_upload_evidence(actor, application):
        raise ValueError("You cannot upload evidence for this application.")
    if artifact_scope == EvidenceVaultItem.OwnerScope.CASE and not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before case-owned evidence can be uploaded.")
    validated_upload = None
    file_size = getattr(uploaded_file, "size", None)
    if artifact_type == ARTIFACT_TYPE_APPLICANT_DOCUMENT:
        validated_upload = validate_applicant_document_upload(uploaded_file)
    elif file_size is not None and file_size > settings.MAX_EVIDENCE_UPLOAD_BYTES:
        raise ValueError("Uploaded file exceeds the configured Evidence Vault size limit.")
    if validated_upload is not None:
        raw_bytes = validated_upload.raw_bytes
        content_type = validated_upload.canonical_content_type
        file_size = validated_upload.size_bytes
    else:
        raw_bytes = uploaded_file.read()
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        content_type = getattr(uploaded_file, "content_type", "") or ""
        file_size = len(raw_bytes)
    sha256_digest = hashlib.sha256(raw_bytes).hexdigest()
    nonce, ciphertext = encrypt_evidence_bytes(raw_bytes)
    stage = _evidence_stage_for_application(application)
    document_key = document_key or EvidenceVaultItem.build_document_key(label)
    owner_kwargs = _evidence_owner_filters(
        application=application if artifact_scope == EvidenceVaultItem.OwnerScope.APPLICATION else None,
        recruitment_case=(
            getattr(application, "case", None) if artifact_scope == EvidenceVaultItem.OwnerScope.CASE else None
        ),
        recruitment_entry=(
            application.position if artifact_scope == EvidenceVaultItem.OwnerScope.ENTRY else None
        ),
    )
    previous_version = EvidenceVaultItem.objects.filter(
        artifact_scope=owner_kwargs["artifact_scope"],
        application=owner_kwargs["application"],
        recruitment_case=owner_kwargs["recruitment_case"],
        recruitment_entry=owner_kwargs["recruitment_entry"],
        stage=stage,
        document_key=document_key,
        is_current_version=True,
    ).order_by("-version_number", "-created_at").first()
    version_family = previous_version.version_family if previous_version else uuid.uuid4()
    version_number = previous_version.version_number + 1 if previous_version else 1
    evidence = EvidenceVaultItem(
        application=owner_kwargs["application"],
        recruitment_case=owner_kwargs["recruitment_case"],
        recruitment_entry=owner_kwargs["recruitment_entry"],
        artifact_scope=owner_kwargs["artifact_scope"],
        artifact_type=artifact_type or ARTIFACT_TYPE_WORKFLOW_EVIDENCE,
        uploaded_by=actor,
        uploaded_by_role=getattr(actor, "role", ""),
        stage=stage,
        label=label,
        document_key=document_key,
        version_family=version_family,
        version_number=version_number,
        previous_version=previous_version,
        is_current_version=True,
        original_filename=uploaded_file.name,
        content_type=content_type,
        size_bytes=file_size,
        digest_algorithm="sha256",
        sha256_digest=sha256_digest,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    evidence.full_clean()
    evidence.save()
    if previous_version:
        previous_version.is_current_version = False
        previous_version.save(update_fields=["is_current_version", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_UPLOADED,
        description=f"Uploaded evidence '{label}' {evidence.version_label}.",
        metadata={
            "evidence_id": evidence.id,
            "artifact_scope": evidence.artifact_scope,
            "artifact_type": evidence.artifact_type,
            "case_id": evidence.recruitment_case_id,
            "entry_id": evidence.recruitment_entry_id,
            "stage": evidence.stage,
            "document_key": evidence.document_key,
            "version_family": str(evidence.version_family),
            "version_number": evidence.version_number,
            "previous_version_id": previous_version.id if previous_version else None,
            "sha256": sha256_digest,
        },
    )
    return evidence


@transaction.atomic
def update_evidence_archive_status(evidence, actor, action, archive_tag=""):
    archive_tag = (archive_tag or "").strip()
    context_application = get_evidence_context_application_for_user(actor, evidence)
    if context_application is None or not user_can_manage_evidence_archive(actor, context_application):
        raise ValueError("You cannot change the archive state of this evidence item.")

    if action == "archive":
        if not archive_tag:
            raise ValueError("Archive tag is required when archiving an evidence item.")
        evidence.is_archived = True
        evidence.archive_tag = archive_tag
        evidence.archived_at = timezone.now()
        evidence.archived_by = actor
        audit_action = AuditLog.Action.EVIDENCE_ARCHIVED
        description = f"Archived evidence '{evidence.label}' {evidence.version_label}."
    elif action == "restore":
        evidence.is_archived = False
        evidence.archive_tag = ""
        evidence.archived_at = None
        evidence.archived_by = None
        audit_action = AuditLog.Action.EVIDENCE_RESTORED
        description = f"Restored evidence '{evidence.label}' {evidence.version_label} from archive."
    else:
        raise ValueError("Unsupported evidence archive action.")

    evidence.full_clean()
    evidence.save(
        update_fields=[
            "is_archived",
            "archive_tag",
            "archived_at",
            "archived_by",
            "archived_by_role",
            "updated_at",
        ]
    )
    record_audit_event(
        application=context_application,
        actor=actor,
        action=audit_action,
        description=description,
        metadata={
            "evidence_id": evidence.id,
            "stage": evidence.stage,
            "artifact_scope": evidence.artifact_scope,
            "artifact_type": evidence.artifact_type,
            "version_family": str(evidence.version_family),
            "version_number": evidence.version_number,
            "archive_tag": evidence.archive_tag,
            "is_archived": evidence.is_archived,
        },
    )
    return evidence


def _route_for_submission(application):
    if application.level == PositionPosting.Level.LEVEL_1:
        return RecruitmentUser.Role.SECRETARIAT, RecruitmentApplication.Status.SECRETARIAT_REVIEW
    return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW


@transaction.atomic
def submit_application(application, actor):
    if application.applicant_id != actor.id:
        raise ValueError("Only the owning applicant can submit this application.")
    if not application.is_editable_by_applicant:
        raise ValueError("This application can no longer be submitted.")
    if not application.checklist_complete:
        raise ValueError("Complete the submission checklist before final submission.")
    missing_requirements = get_missing_required_applicant_document_requirements(application)
    if missing_requirements:
        missing_labels = "; ".join(requirement.title for requirement in missing_requirements)
        raise ValueError(
            "Upload the required requirement-coded applicant documents before final submission. "
            f"Missing: {missing_labels}."
        )
    if not application.position.is_open_for_intake:
        raise ValueError("This recruitment entry is not currently open for intake.")
    if not application.otp_is_currently_valid:
        if application.otp_expires_at and application.otp_expires_at < timezone.now():
            raise ValueError("Your OTP verification has expired. Request a new code before final submission.")
        raise ValueError("Valid OTP verification is required before final submission.")

    previous_status = application.status
    previous_role = application.current_handler_role
    next_role, next_status = _route_for_submission(application)
    if not application.reference_number:
        application.reference_number = generate_application_reference()
    application.status = next_status
    application.current_handler_role = next_role
    application.submitted_at = timezone.now()
    application.submission_hash = generate_submission_hash(application)
    application.save(
        update_fields=[
            "reference_number",
            "status",
            "current_handler_role",
            "submitted_at",
            "submission_hash",
            "branch",
            "level",
            "updated_at",
        ]
    )
    case = _upsert_recruitment_case_for_submission(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_SUBMITTED,
        description="Applicant submitted the application.",
        metadata={"submission_hash": application.submission_hash, **_case_timeline_metadata(case)},
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Application routed to {next_role}.",
        metadata={
            "status": next_status,
            "current_handler_role": next_role,
            **_case_timeline_metadata(case),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.INITIAL,
        description=f"Application routed to {next_role}.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=next_role,
        from_status=previous_status,
        to_status=next_status,
        from_stage="",
        to_stage=case.current_stage,
    )
    queue_submission_acknowledgment_notification(application, actor=actor)
    return application


def get_available_actions(application, user):
    effective_role = get_effective_role_for_action(user, application)
    case = getattr(application, "case", None)
    if case and case.is_stage_locked:
        return []
    current_section = get_current_workflow_section(application)
    current_stage = case.current_stage if case else _review_stage_from_application_status(application.status)

    if (
        current_section == "screening"
        and screening_requires_disposition_for_current_stage(application)
        and effective_role in SCREENING_REVIEW_ROLES
        and current_stage in SCREENING_STAGES
    ):
        return [
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]

    if current_section != "actions":
        return []

    if effective_role == RecruitmentUser.Role.SECRETARIAT and current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
        return [
            ("endorse", "Endorse to HRM Chief"),
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]
    if effective_role == RecruitmentUser.Role.HRM_CHIEF and current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
        endorse_label = (
            "Endorse to Appointing Authority"
            if application.branch == PositionPosting.Branch.COS
            else "Endorse to HRMPSB"
        )
        return [
            ("endorse", endorse_label),
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]
    if effective_role == RecruitmentUser.Role.HRMPSB_MEMBER and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
        return [
            ("recommend", "Recommend to Appointing Authority"),
            ("return_to_hrm_chief", "Return to HRM Chief"),
            ("reject", "Reject Application"),
        ]
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
    ):
        return [
            ("return_to_hrm_chief", "Return to HRM Chief"),
        ]
    return []


def _transition_target(application, effective_role, action):
    if effective_role == RecruitmentUser.Role.SECRETARIAT:
        if action == "endorse":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Endorsed by Secretariat."
        if action == "return_to_applicant":
            return RecruitmentUser.Role.APPLICANT, RecruitmentApplication.Status.RETURNED_TO_APPLICANT, "Returned by Secretariat."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by Secretariat."
    if effective_role == RecruitmentUser.Role.HRM_CHIEF:
        if action == "endorse":
            if application.branch == PositionPosting.Branch.COS:
                return (
                    RecruitmentUser.Role.APPOINTING_AUTHORITY,
                    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
                    "COS application endorsed by HRM Chief.",
                )
            return RecruitmentUser.Role.HRMPSB_MEMBER, RecruitmentApplication.Status.HRMPSB_REVIEW, "Plantilla application endorsed to HRMPSB."
        if action == "return_to_applicant":
            return RecruitmentUser.Role.APPLICANT, RecruitmentApplication.Status.RETURNED_TO_APPLICANT, "Returned by HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by HRM Chief."
    if effective_role == RecruitmentUser.Role.HRMPSB_MEMBER:
        if action == "recommend":
            return RecruitmentUser.Role.APPOINTING_AUTHORITY, RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW, "Recommended by HRMPSB."
        if action == "return_to_hrm_chief":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Returned by HRMPSB to HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by HRMPSB."
    if effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY:
        if action == "approve":
            completion_role = _completion_handler_role(application)
            completion_role_label = RecruitmentUser.Role(completion_role).label
            return (
                completion_role,
                RecruitmentApplication.Status.APPROVED,
                (
                    "Approved by Appointing Authority and routed to "
                    f"{completion_role_label} for completion tracking."
                ),
            )
        if action == "return_to_hrm_chief":
            return RecruitmentUser.Role.HRM_CHIEF, RecruitmentApplication.Status.HRM_CHIEF_REVIEW, "Returned by Appointing Authority to HRM Chief."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by Appointing Authority."
    raise ValueError("Unsupported workflow action for the current stage.")


@transaction.atomic
def process_workflow_action(application, actor, action, remarks):
    effective_role = get_effective_role_for_action(actor, application)
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before workflow actions can be processed.")
    if application.case.is_stage_locked:
        raise ValueError("This recruitment case is stage-locked. Use controlled reopen before proceeding.")
    if not user_can_process_application(actor, application):
        if (
            effective_role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat cannot process Level 2 applications without an active override."
            )
        raise ValueError("You cannot process this application at its current workflow stage.")
    current_section = get_current_workflow_section(application)
    if action in {"endorse", "recommend"} and current_section != "actions":
        raise ValueError(_workflow_progress_block_message(application, current_section))
    if (
        effective_role in SCREENING_REVIEW_ROLES
        and application.case.current_stage in SCREENING_STAGES
        and action == "endorse"
        and not screening_is_finalized_for_current_stage(application)
    ):
        raise ValueError("Finalize the screening record before endorsing this application.")
    if (
        effective_role == RecruitmentUser.Role.HRM_CHIEF
        and application.branch == PositionPosting.Branch.COS
        and application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        and action == "endorse"
    ):
        deliberation_record = get_deliberation_record(application, stage=application.case.current_stage)
        if not deliberation_record or not deliberation_record.is_finalized:
            raise ValueError("Finalize the deliberation record before endorsing this COS application.")
    if (
        effective_role == RecruitmentUser.Role.HRMPSB_MEMBER
        and application.branch == PositionPosting.Branch.PLANTILLA
        and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and action == "recommend"
    ):
        deliberation_record = get_deliberation_record(application, stage=application.case.current_stage)
        if not deliberation_record or not deliberation_record.is_finalized:
            raise ValueError("Finalize the deliberation record before recommending this Plantilla application.")
        comparative_assessment_report = get_comparative_assessment_report(
            application,
            stage=application.case.current_stage,
        )
        if not comparative_assessment_report or not comparative_assessment_report.is_finalized:
            raise ValueError(
                "Finalize the Comparative Assessment Report before recommending this Plantilla application."
            )

    next_role, next_status, description = _transition_target(application, effective_role, action)
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = _sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
        remarks=remarks,
    )
    application.current_handler_role = next_role
    application.status = next_status
    if application.case.current_stage == RecruitmentCase.Stage.CLOSED and next_status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    }:
        application.closed_at = timezone.now()
    else:
        application.closed_at = None
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=description,
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "action": action,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
        },
    )
    if next_role:
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=f"Application routed to {next_role}.",
            metadata={
                "status": next_status,
                "current_handler_role": next_role,
                **_case_timeline_metadata(application.case),
            },
        )
        record_routing_history_event(
            application=application,
            actor=actor,
            route_type=RoutingHistory.RouteType.FORWARD,
            description=f"Application routed to {next_role}.",
            recruitment_case=application.case,
            from_handler_role=previous_role,
            to_handler_role=next_role,
            from_status=previous_status,
            to_status=next_status,
            from_stage=case_transition["previous_stage"],
            to_stage=application.case.current_stage,
            notes=remarks,
        )
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        override = application.active_secretariat_override
        if override:
            override.mark_used()
            record_audit_event(
                application=application,
                actor=actor,
                action=AuditLog.Action.OVERRIDE_USED,
                description="Secretariat override consumed during Level 2 processing.",
                metadata={"override_id": override.id},
            )
    if next_status == RecruitmentApplication.Status.APPROVED:
        queue_selected_applicant_notification(application, actor=actor)
    elif next_status == RecruitmentApplication.Status.REJECTED:
        queue_non_selected_applicant_notification(application, actor=actor)
    return application


@transaction.atomic
def record_final_decision(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before a final decision can be recorded.")
    if application.case.is_stage_locked:
        raise ValueError("This recruitment case is stage-locked. Use controlled reopen before proceeding.")
    expected_stage, expected_role = _final_decision_stage_and_role(application)
    if not user_can_record_final_decision(actor, application):
        expected_role_label = RecruitmentUser.Role(expected_role).label
        raise ValueError(
            f"Only the {expected_role_label} may record the final decision at the current workflow stage."
        )
    if application.case.current_stage != expected_stage:
        expected_stage_label = RecruitmentCase.Stage(expected_stage).label
        raise ValueError(
            f"Final decisions may only be recorded during the {expected_stage_label} stage."
        )

    submission_packet = build_submission_packet(application)
    missing_components = submission_packet["summary"]["missing_components"]
    if missing_components:
        raise ValueError(
            "Submission packet is incomplete for final decision recording. Missing: "
            + "; ".join(missing_components)
            + "."
        )

    decision = FinalDecision(
        application=application,
        recruitment_case=application.case,
        recruitment_entry=application.position,
        review_stage=expected_stage,
        decided_by=actor,
        branch=application.branch,
        level=application.level,
        decision_outcome=cleaned_data["decision_outcome"],
        decision_notes=cleaned_data["decision_notes"],
        submission_packet_snapshot=submission_packet,
    )
    decision.full_clean()
    decision.save()

    next_status = FINAL_DECISION_OUTCOME_TO_STATUS[decision.decision_outcome]
    next_role = _completion_handler_role(application) if decision.is_selected else ""
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = _sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
        remarks=decision.decision_notes,
    )
    application.current_handler_role = next_role
    application.status = next_status
    application.closed_at = timezone.now() if application.case.current_stage == RecruitmentCase.Stage.CLOSED else None
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=(
            (
                "COS selection recorded as selected by the HRM Chief."
                if application.branch == PositionPosting.Branch.COS
                else "Final decision recorded as selected by the Appointing Authority."
            )
            if decision.is_selected
            else (
                "COS selection recorded as not selected by the HRM Chief."
                if application.branch == PositionPosting.Branch.COS
                else "Final decision recorded as not selected by the Appointing Authority."
            )
        ),
        metadata={
            "final_decision_id": decision.id,
            "decision_outcome": decision.decision_outcome,
            "decision_notes": decision.decision_notes,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
            "preserved_artifact_ids": submission_packet["preserved_artifact_ids"],
        },
    )
    if next_role:
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=f"Application routed to {next_role} for completion handling.",
            metadata={
                "status": next_status,
                "current_handler_role": next_role,
                **_case_timeline_metadata(application.case),
            },
        )
        record_routing_history_event(
            application=application,
            actor=actor,
            route_type=RoutingHistory.RouteType.FORWARD,
            description=f"Application routed to {next_role} for completion handling.",
            recruitment_case=application.case,
            from_handler_role=previous_role,
            to_handler_role=next_role,
            from_status=previous_status,
            to_status=next_status,
            from_stage=case_transition["previous_stage"],
            to_stage=application.case.current_stage,
            notes=decision.decision_notes,
        )
        queue_selected_applicant_notification(application, actor=actor)
    else:
        queue_non_selected_applicant_notification(application, actor=actor)
    return decision


@transaction.atomic
def close_recruitment_case(application, actor, closure_notes):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before it can be closed.")
    if not user_can_manage_completion(actor, application):
        raise ValueError("You cannot close this recruitment case at its current workflow stage.")

    case = application.case
    if case.current_stage != RecruitmentCase.Stage.COMPLETION:
        raise ValueError("Case closure is only available from the completion tracking stage.")

    completion_record = get_completion_record(application)
    if not completion_record:
        raise ValueError("Record completion tracking before closing the recruitment case.")
    if not completion_record.requirements.exists():
        raise ValueError("Add at least one completion requirement item before closing the case.")
    if completion_record.has_pending_requirements:
        raise ValueError(
            "All completion requirements must be marked completed or not applicable before closing the case."
        )
    completion_label = completion_record.completion_label.lower()
    if not completion_record.has_completion_reference_for_closure:
        raise ValueError(f"Record the {completion_label} reference before closing the case.")
    if not completion_record.has_completion_date_for_closure:
        raise ValueError(f"Record the {completion_label} date before closing the case.")

    previous_role = application.current_handler_role
    previous_stage = case.current_stage
    previous_case_status = case.case_status
    closed_at = timezone.now()

    case.current_stage = RecruitmentCase.Stage.CLOSED
    case.case_status = RecruitmentCase.CaseStatus.APPROVED
    case.current_handler_role = ""
    case.is_stage_locked = True
    case.locked_stage = RecruitmentCase.Stage.COMPLETION
    case.closed_at = closed_at
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "updated_at",
        ]
    )

    application.current_handler_role = ""
    application.closed_at = closed_at
    application.save(update_fields=["current_handler_role", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_CLOSED,
        description="Closed the recruitment case after completion tracking.",
        metadata={
            "remarks": closure_notes,
            "from_stage": previous_stage,
            "to_stage": case.current_stage,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
            "completion_record_id": completion_record.id,
            "requirement_count": completion_record.total_requirement_count,
            "resolved_requirement_count": completion_record.completed_requirement_count,
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.CLOSE,
        description="Recruitment case closed after completion tracking.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role="",
        from_status=application.status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=closure_notes,
    )
    return case


@transaction.atomic
def grant_secretariat_override(application, actor, reason):
    if actor.role != RecruitmentUser.Role.SYSTEM_ADMIN:
        raise ValueError("Only the System Administrator can grant a Secretariat override.")
    if application.level != PositionPosting.Level.LEVEL_2:
        raise ValueError("Overrides are only available for Level 2 applications.")
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before an override can be granted.")
    if application.status in {
        RecruitmentApplication.Status.APPROVED,
        RecruitmentApplication.Status.REJECTED,
    }:
        raise ValueError("Closed applications cannot be overridden.")
    case = application.case
    if case.is_stage_locked:
        raise ValueError("Stage-locked recruitment cases cannot be rerouted by override.")
    if (
        application.status != RecruitmentApplication.Status.HRM_CHIEF_REVIEW
        or application.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
        or case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        or case.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
    ):
        raise ValueError(
            "Secretariat overrides are only available while a Level 2 application is actively assigned to the HRM Chief review stage."
        )
    previous_role = application.current_handler_role
    previous_status = application.status
    application.overrides.filter(is_active=True).update(
        is_active=False,
        revoked_at=timezone.now(),
    )
    override = WorkflowOverride.objects.create(
        application=application,
        granted_by=actor,
        target_role=RecruitmentUser.Role.SECRETARIAT,
        reason=reason,
    )
    previous_stage = case.current_stage
    case.current_stage = RecruitmentCase.Stage.SECRETARIAT_REVIEW
    case.current_handler_role = RecruitmentUser.Role.SECRETARIAT
    case.case_status = RecruitmentCase.CaseStatus.ACTIVE
    case.is_stage_locked = False
    case.locked_stage = ""
    case.closed_at = None
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "updated_at",
        ]
    )
    application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
    application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
    application.save(update_fields=["current_handler_role", "status", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.OVERRIDE_GRANTED,
        description="Secretariat override granted for a Level 2 application.",
        metadata={
            "override_id": override.id,
            "reason": reason,
            **(_case_timeline_metadata(case) if case else {}),
        },
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description="Application rerouted to Secretariat under controlled override.",
        metadata={
            "status": application.status,
            "current_handler_role": application.current_handler_role,
            **(_case_timeline_metadata(case) if case else {}),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.OVERRIDE,
        description="Application rerouted to Secretariat under controlled override.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=application.current_handler_role,
        from_status=previous_status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage if case else _review_stage_from_application_status(application.status),
        notes=reason,
        is_override=True,
    )
    return override


@transaction.atomic
def reopen_recruitment_case(application, actor, reason):
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case does not exist for this application.")
    case = application.case
    if actor.role not in CASE_REOPEN_ROLES:
        raise ValueError("Only the HRM Chief can perform a controlled reopen in this prototype.")
    if not case.is_stage_locked or not case.locked_stage:
        raise ValueError("Only stage-locked recruitment cases can be reopened.")

    previous_stage = case.current_stage
    previous_role = application.current_handler_role
    previous_status = application.status
    reopened_stage = case.locked_stage
    reopened_status = _application_status_from_stage(reopened_stage)
    if not reopened_status:
        raise ValueError("The locked recruitment case does not point to a reopenable workflow stage.")

    case.current_stage = reopened_stage
    case.current_handler_role = _handler_role_from_stage(reopened_stage, application=application)
    case.case_status = RecruitmentCase.CaseStatus.ACTIVE
    case.is_stage_locked = False
    case.locked_stage = ""
    case.closed_at = None
    case.reopened_at = timezone.now()
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "current_handler_role",
            "case_status",
            "is_stage_locked",
            "locked_stage",
            "closed_at",
            "reopened_at",
            "updated_at",
        ]
    )

    application.status = reopened_status
    application.current_handler_role = case.current_handler_role
    application.closed_at = None
    application.save(update_fields=["status", "current_handler_role", "closed_at", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_REOPENED,
        description="Controlled reopen applied to a stage-locked recruitment case.",
        metadata={
            "reason": reason,
            "reopened_stage": reopened_stage,
            **_case_timeline_metadata(case),
        },
    )
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Recruitment case reopened to {case.current_handler_role}.",
        metadata={
            "status": application.status,
            "current_handler_role": application.current_handler_role,
            **_case_timeline_metadata(case),
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.REOPEN,
        description=f"Recruitment case reopened to {case.current_handler_role}.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=application.current_handler_role,
        from_status=previous_status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=reason,
    )
    return case


def _build_pdf_document(title, lines, *, document_title="RecruitGuard-CHD Export"):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _page_width, page_height = A4

    def start_page():
        pdf.setTitle(document_title)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(50, page_height - 50, title[:90])
        pdf.setFont("Helvetica", 10)
        return page_height - 80

    y = start_page()
    for raw_line in lines:
        line_text = str(raw_line or "")
        wrapped_lines = (
            textwrap.wrap(
                line_text,
                width=100,
                break_long_words=True,
                break_on_hyphens=False,
            )
            if line_text
            else [""]
        )
        for line in wrapped_lines:
            if y < 60:
                pdf.showPage()
                y = start_page()
            if line:
                pdf.drawString(50, y, line)
            y -= 14

    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _build_application_pdf(application, *, actor=None, generated_at=None):
    case = getattr(application, "case", None)
    completion_record = get_completion_record(application)
    evidence_items = list(get_evidence_items_for_application_context(application))
    generated_at = generated_at or timezone.now()
    actor_label = str(actor) if actor else "System"
    actor_role = actor.get_role_display() if actor else "System"
    lines = [
        f"Reference: {application.reference_number}",
        f"Application ID: {application.id}",
        f"Recruitment Case ID: {case.id if case else 'Not created'}",
        f"Exported At: {generated_at:%Y-%m-%d %H:%M}",
        f"Exported By: {actor_label} ({actor_role})",
        f"Applicant: {application.applicant_display_name}",
        f"Position: {application.position.title} [{application.position.job_code}]",
        f"Branch: {application.position.get_branch_display()}",
        f"Level: {application.position.get_level_display()}",
        f"Application Status: {application.get_status_display()}",
        f"Current Handler: {application.current_handler_role or 'Closed'}",
        f"Case Stage: {case.get_current_stage_display() if case else 'Not created'}",
        f"Case Status: {case.get_case_status_display() if case else 'N/A'}",
        f"Stage Locked: {'Yes' if getattr(case, 'is_stage_locked', False) else 'No'}",
        f"Submission Hash: {application.submission_hash or 'N/A'}",
        "",
        "Qualification Summary:",
        application.qualification_summary or "N/A",
        "",
        "Cover Letter:",
        application.cover_letter or "N/A",
        "",
        f"Evidence Count: {len(evidence_items)}",
        f"Audit Entry Count: {application.audit_logs.count()}",
        f"Routing Event Count: {application.routing_history.count()}",
    ]
    if completion_record:
        lines.extend(
            [
                "",
                "Completion Tracking:",
                f"Reference: {completion_record.completion_reference or 'N/A'}",
                (
                    f"Completion Date: {completion_record.completion_date:%Y-%m-%d}"
                    if completion_record.completion_date
                    else "Completion Date: N/A"
                ),
                (
                    f"Deadline: {completion_record.deadline:%Y-%m-%d}"
                    if completion_record.deadline
                    else "Deadline: N/A"
                ),
                f"Announcement Reference: {completion_record.announcement_reference or 'N/A'}",
                f"Requirements Ready for Closure: {'Yes' if completion_record.requirements_ready_for_closure else 'No'}",
            ]
        )
    return _build_pdf_document(
        "RecruitGuard-CHD Controlled Export Summary",
        lines,
        document_title=application.reference_number or "RecruitGuard-CHD Export",
    )


def _build_comparative_assessment_report_pdf(
    application,
    actor,
    candidate_rows,
    generation_number,
    summary_notes,
):
    lines = [
        "RecruitGuard-CHD Comparative Assessment Report",
        f"Recruitment Entry: {application.position.title} [{application.position.job_code}]",
        f"Branch: {application.position.get_branch_display()}",
        f"Workflow Stage: {application.case.get_current_stage_display()}",
        f"Generated By: {actor}",
        f"Generated At: {timezone.now():%Y-%m-%d %H:%M}",
        f"Generation Version: {generation_number}",
        "",
        "Ranked Candidates",
    ]
    if summary_notes:
        lines.extend(["Summary Notes:", summary_notes, ""])
    for row in candidate_rows:
        lines.extend(
            [
                (
                    f"Rank {row['rank_order']} | {row['application'].reference_number} | "
                    f"{row['application'].applicant_display_name}"
                ),
                (
                    f"Qualification: {row['qualification_outcome'] or 'N/A'} | "
                    f"Exam: {row['exam_status'] or 'N/A'} ({row['exam_score'] or 'N/A'}) | "
                    f"Interview Avg: {row['interview_average_score'] or 'N/A'}"
                ),
                f"Exam Components: {row.get('exam_components') or 'N/A'}",
                row["decision_support_summary"] or "No decision-support summary recorded.",
                "",
            ]
        )
    return _build_pdf_document(
        "RecruitGuard-CHD Comparative Assessment Report",
        lines,
        document_title=f"{application.position.job_code} Comparative Assessment Report",
    )


def _audit_log_csv(application):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "timestamp",
            "case_reference",
            "workflow_stage",
            "actor",
            "actor_role",
            "action",
            "is_sensitive_access",
            "description",
            "metadata",
        ]
    )
    for row in application.audit_logs.select_related("actor").order_by("created_at"):
        writer.writerow(
            [
                row.created_at.isoformat(),
                row.case_reference,
                row.workflow_stage,
                row.actor.username if row.actor else "",
                row.actor_role,
                row.action,
                row.is_sensitive_access,
                row.description,
                json.dumps(row.metadata, sort_keys=True),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _routing_history_csv(application):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "timestamp",
            "route_type",
            "branch",
            "level",
            "actor",
            "actor_role",
            "from_handler_role",
            "to_handler_role",
            "from_status",
            "to_status",
            "from_stage",
            "to_stage",
            "is_override",
            "description",
            "notes",
        ]
    )
    for row in application.routing_history.select_related("actor").order_by("created_at"):
        writer.writerow(
            [
                row.created_at.isoformat(),
                row.route_type,
                row.branch,
                row.level,
                row.actor.username if row.actor else "",
                row.actor_role,
                row.from_handler_role,
                row.to_handler_role,
                row.from_status,
                row.to_status,
                row.from_stage,
                row.to_stage,
                row.is_override,
                row.description,
                row.notes,
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _submission_packet_json(application):
    return json.dumps(build_submission_packet(application), indent=2).encode("utf-8")


def _safe_export_path_component(value, fallback):
    normalized = slugify(value or "")
    return normalized or fallback


def _safe_export_filename(filename, fallback):
    safe_name = os.path.basename(filename or "").strip().replace("\\", "_").replace("/", "_")
    return safe_name or fallback


def _export_bundle_root(application):
    reference = application.reference_number or f"application-{application.id}"
    return f"{reference}/"


def _evidence_export_path(evidence, bundle_root):
    scope_component = _safe_export_path_component(evidence.artifact_scope, "artifact")
    stage_component = _safe_export_path_component(evidence.stage, "unstaged")
    document_component = evidence.document_key or _safe_export_path_component(evidence.label, "artifact")
    fallback_name = f"{document_component}.bin"
    filename = _safe_export_filename(evidence.original_filename, fallback_name)
    return (
        f"{bundle_root}evidence/{scope_component}/{stage_component}/{document_component}/"
        f"{evidence.version_label}_{filename}"
    )


def _collect_export_evidence(application, bundle_root):
    evidence_items = get_evidence_items_for_application_context(application).select_related(
        "uploaded_by",
        "archived_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("stage", "document_key", "version_number", "created_at", "id")
    export_items = []
    for evidence in evidence_items:
        plaintext = _decrypt_evidence_bytes(evidence)
        exported_sha256 = hashlib.sha256(plaintext).hexdigest()
        export_items.append(
            {
                "id": evidence.id,
                "artifact_scope": evidence.artifact_scope,
                "artifact_scope_label": evidence.get_artifact_scope_display(),
                "artifact_type": evidence.artifact_type,
                "application_id": evidence.application_id,
                "recruitment_case_id": evidence.recruitment_case_id,
                "recruitment_entry_id": evidence.recruitment_entry_id,
                "label": evidence.label,
                "stage": evidence.stage,
                "stage_label": evidence.get_stage_display(),
                "document_key": evidence.document_key,
                "version_family": str(evidence.version_family),
                "version_number": evidence.version_number,
                "version_label": evidence.version_label,
                "is_current_version": evidence.is_current_version,
                "is_archived": evidence.is_archived,
                "archive_tag": evidence.archive_tag,
                "original_filename": evidence.original_filename,
                "uploaded_by": str(evidence.uploaded_by),
                "uploaded_by_role": evidence.uploaded_by_role,
                "stored_at": evidence.created_at.isoformat(),
                "digest_algorithm": evidence.digest_algorithm,
                "stored_sha256_digest": evidence.sha256_digest,
                "exported_sha256_digest": exported_sha256,
                "sha256_matches_stored": exported_sha256 == evidence.sha256_digest,
                "size_bytes": evidence.size_bytes,
                "content_type": evidence.content_type,
                "export_path": _evidence_export_path(evidence, bundle_root),
                "file_bytes": plaintext,
            }
        )
    return export_items


def _evidence_inventory_csv(application, actor, generated_at, evidence_exports):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "case_reference",
            "application_id",
            "recruitment_case_id",
            "recruitment_entry_id",
            "export_generated_at",
            "exported_by",
            "exported_by_role",
            "evidence_id",
            "artifact_scope",
            "artifact_scope_label",
            "artifact_type",
            "label",
            "stage",
            "stage_label",
            "document_key",
            "version_family",
            "version_number",
            "is_current_version",
            "is_archived",
            "archive_tag",
            "original_filename",
            "uploaded_by",
            "uploaded_by_role",
            "stored_at",
            "digest_algorithm",
            "stored_sha256_digest",
            "exported_sha256_digest",
            "digest_match",
            "size_bytes",
            "export_path",
        ]
    )
    case = getattr(application, "case", None)
    for item in evidence_exports:
        writer.writerow(
            [
                application.reference_number,
                application.id,
                getattr(case, "id", ""),
                application.position_id,
                generated_at.isoformat(),
                actor.username,
                actor.role,
                item["id"],
                item["artifact_scope"],
                item["artifact_scope_label"],
                item["artifact_type"],
                item["label"],
                item["stage"],
                item["stage_label"],
                item["document_key"],
                item["version_family"],
                item["version_number"],
                item["is_current_version"],
                item["is_archived"],
                item["archive_tag"],
                item["original_filename"],
                item["uploaded_by"],
                item["uploaded_by_role"],
                item["stored_at"],
                item["digest_algorithm"],
                item["stored_sha256_digest"],
                item["exported_sha256_digest"],
                item["sha256_matches_stored"],
                item["size_bytes"],
                item["export_path"],
            ]
        )
    return buffer.getvalue().encode("utf-8")


def _build_evidence_inventory_pdf(application, actor, generated_at, evidence_exports):
    case = getattr(application, "case", None)
    lines = [
        f"Reference: {application.reference_number}",
        f"Application ID: {application.id}",
        f"Recruitment Case ID: {case.id if case else 'Not created'}",
        f"Generated At: {generated_at:%Y-%m-%d %H:%M}",
        f"Exported By: {actor} ({actor.get_role_display()})",
        f"Evidence Count: {len(evidence_exports)}",
    ]
    if not evidence_exports:
        lines.extend(
            [
                "",
                "No evidence files were present in the Evidence Vault for this export.",
            ]
        )
    else:
        for index, item in enumerate(evidence_exports, start=1):
            lines.extend(
                [
                    "",
                    (
                        f"{index}. {item['label']} | {item['artifact_scope_label']} | "
                        f"{item['stage_label']} | {item['version_label']}"
                    ),
                    (
                        f"Filename: {item['original_filename']} | Current Version: "
                        f"{'Yes' if item['is_current_version'] else 'No'} | Archived: "
                        f"{'Yes' if item['is_archived'] else 'No'}"
                    ),
                    (
                        f"Uploader: {item['uploaded_by']} ({item['uploaded_by_role']}) | "
                        f"Stored At: {item['stored_at']}"
                    ),
                    f"SHA-256: {item['stored_sha256_digest']}",
                    f"Export Path: {item['export_path']}",
                ]
            )
    return _build_pdf_document(
        "RecruitGuard-CHD Evidence Inventory",
        lines,
        document_title=f"{application.reference_number} Evidence Inventory",
    )


def _manifest_json(
    application,
    *,
    actor,
    generated_at,
    bundle_root,
    evidence_exports,
    bundle_members,
    audit_log_path,
    routing_history_path,
    inventory_paths,
    verification_paths,
    submission_packet_path,
    export_log,
):
    case = getattr(application, "case", None)
    completion_record = get_completion_record(application)
    submission_packet = build_submission_packet(application)
    payload = {
        "export": {
            "bundle_root": bundle_root,
            "generated_at": generated_at.isoformat(),
            "bundle_member_count": len(bundle_members),
            "evidence_file_count": len(evidence_exports),
            "export_log_id": export_log.id,
            "export_log_created_at": export_log.created_at.isoformat(),
            "exported_by": {
                "id": actor.id,
                "username": actor.username,
                "display_name": str(actor),
                "role": actor.role,
                "role_label": actor.get_role_display(),
            },
        },
        "source_application": {
            "id": application.id,
            "reference_number": application.reference_number,
            "branch": application.branch,
            "branch_label": application.get_branch_display(),
            "level": application.level,
            "level_label": application.get_level_display(),
            "status": application.status,
            "status_label": application.get_status_display(),
            "current_handler_role": application.current_handler_role,
            "position": {
                "entry_id": application.position_id,
                "job_code": application.position.job_code,
                "title": application.position.title,
            },
        },
        "submission_hash": application.submission_hash,
        "source_case": {
            "id": getattr(case, "id", None),
            "current_stage": getattr(case, "current_stage", ""),
            "current_stage_label": case.get_current_stage_display() if case else "",
            "case_status": getattr(case, "case_status", ""),
            "case_status_label": case.get_case_status_display() if case else "",
            "current_handler_role": getattr(case, "current_handler_role", ""),
            "is_stage_locked": getattr(case, "is_stage_locked", False),
        },
        "bundle_contents": {
            "members": bundle_members,
            "audit_log_path": audit_log_path,
            "routing_history_path": routing_history_path,
            "submission_packet_path": submission_packet_path,
            "inventory_paths": inventory_paths,
            "verification_paths": verification_paths,
        },
        "submission_packet_summary": submission_packet.get("summary", {}),
        "evidence": [
            {
                "evidence_id": item["id"],
                "artifact_scope": item["artifact_scope"],
                "artifact_scope_label": item["artifact_scope_label"],
                "artifact_type": item["artifact_type"],
                "application_id": item["application_id"],
                "recruitment_case_id": item["recruitment_case_id"],
                "recruitment_entry_id": item["recruitment_entry_id"],
                "label": item["label"],
                "document_key": item["document_key"],
                "stage": item["stage"],
                "stage_label": item["stage_label"],
                "version_family": item["version_family"],
                "version_number": item["version_number"],
                "version_label": item["version_label"],
                "is_current_version": item["is_current_version"],
                "is_archived": item["is_archived"],
                "archive_tag": item["archive_tag"],
                "original_filename": item["original_filename"],
                "uploaded_by": item["uploaded_by"],
                "uploaded_by_role": item["uploaded_by_role"],
                "stored_at": item["stored_at"],
                "digest_algorithm": item["digest_algorithm"],
                "stored_sha256_digest": item["stored_sha256_digest"],
                "exported_sha256_digest": item["exported_sha256_digest"],
                "sha256_matches_stored": item["sha256_matches_stored"],
                "size_bytes": item["size_bytes"],
                "export_path": item["export_path"],
            }
            for item in evidence_exports
        ],
        "completion": (
            {
                "completion_reference": completion_record.completion_reference,
                "completion_date": (
                    completion_record.completion_date.isoformat()
                    if completion_record.completion_date
                    else ""
                ),
                "deadline": completion_record.deadline.isoformat() if completion_record.deadline else "",
                "announcement_reference": completion_record.announcement_reference,
                "announcement_date": (
                    completion_record.announcement_date.isoformat()
                    if completion_record.announcement_date
                    else ""
                ),
                "remarks": completion_record.remarks,
                "requirements": [
                    {
                        "item_label": requirement.item_label,
                        "status": requirement.status,
                        "notes": requirement.notes,
                    }
                    for requirement in completion_record.requirements.all()
                ],
            }
            if completion_record
            else {}
        ),
        "routing_history": [
            {
                "timestamp": route.created_at.isoformat(),
                "route_type": route.route_type,
                "actor_role": route.actor_role,
                "from_handler_role": route.from_handler_role,
                "to_handler_role": route.to_handler_role,
                "from_status": route.from_status,
                "to_status": route.to_status,
                "from_stage": route.from_stage,
                "to_stage": route.to_stage,
                "is_override": route.is_override,
                "description": route.description,
                "notes": route.notes,
            }
            for route in application.routing_history.order_by("created_at")
        ],
        "final_decisions": [
            {
                "review_stage": decision.review_stage,
                "decision_outcome": decision.decision_outcome,
                "decision_notes": decision.decision_notes,
                "decided_by_role": decision.decided_by_role,
                "decided_at": decision.decided_at.isoformat() if decision.decided_at else "",
            }
            for decision in get_final_decision_history(application)
        ],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def _verification_report_payload(
    application,
    *,
    actor,
    generated_at,
    bundle_root,
    verifiable_entries,
    evidence_exports,
):
    case = getattr(application, "case", None)
    file_hashes = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(verifiable_entries.items())
    }
    evidence_verification = []
    for item in evidence_exports:
        exported_digest = file_hashes[item["export_path"]]
        evidence_verification.append(
            {
                "evidence_id": item["id"],
                "export_path": item["export_path"],
                "stored_sha256_digest": item["stored_sha256_digest"],
                "exported_sha256_digest": exported_digest,
                "digest_match": exported_digest == item["stored_sha256_digest"],
            }
        )
    return {
        "verification_generated_at": generated_at.isoformat(),
        "bundle_root": bundle_root,
        "digest_algorithm": "sha256",
        "verification_scope": "All bundle members except verification outputs.",
        "source_application_id": application.id,
        "source_case_id": getattr(case, "id", None),
        "case_reference": application.reference_number,
        "exported_by": {
            "id": actor.id,
            "username": actor.username,
            "role": actor.role,
        },
        "covered_file_count": len(file_hashes),
        "evidence_file_count": len(evidence_exports),
        "covered_files": [
            {"path": path, "sha256_digest": digest}
            for path, digest in file_hashes.items()
        ],
        "evidence_files": evidence_verification,
    }


def _verification_report_json(
    application,
    *,
    actor,
    generated_at,
    bundle_root,
    verifiable_entries,
    evidence_exports,
):
    payload = _verification_report_payload(
        application,
        actor=actor,
        generated_at=generated_at,
        bundle_root=bundle_root,
        verifiable_entries=verifiable_entries,
        evidence_exports=evidence_exports,
    )
    return json.dumps(payload, indent=2).encode("utf-8"), payload


def _verification_checksums_text(verification_payload):
    lines = [
        f"{item['sha256_digest']}  {item['path']}"
        for item in verification_payload["covered_files"]
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_verification_summary_pdf(application, actor, generated_at, verification_payload):
    matching_count = sum(
        1 for item in verification_payload["evidence_files"] if item["digest_match"]
    )
    lines = [
        f"Reference: {application.reference_number}",
        f"Application ID: {application.id}",
        f"Generated At: {generated_at:%Y-%m-%d %H:%M}",
        f"Exported By: {actor} ({actor.get_role_display()})",
        f"Verification Scope: {verification_payload['verification_scope']}",
        f"Covered File Count: {verification_payload['covered_file_count']}",
        (
            f"Evidence Digest Matches: {matching_count} of "
            f"{verification_payload['evidence_file_count']}"
        ),
        "",
        "Independent Verification Steps:",
        "1. Extract the ZIP bundle without modifying file names or folder structure.",
        "2. Recompute SHA-256 hashes for each path listed in verification/checksums.sha256.",
        "3. Confirm the recomputed digest matches the listed digest for each file.",
        "4. For evidence files, confirm the digest also matches the stored digest in the inventory or JSON report.",
    ]
    mismatches = [
        item for item in verification_payload["evidence_files"] if not item["digest_match"]
    ]
    if mismatches:
        lines.extend(["", "Digest Mismatches Detected:"])
        for item in mismatches:
            lines.append(f"Evidence {item['evidence_id']} mismatch at {item['export_path']}")
    else:
        lines.extend(["", "All exported evidence files matched their stored SHA-256 digests."])
    return _build_pdf_document(
        "RecruitGuard-CHD Verification Summary",
        lines,
        document_title=f"{application.reference_number} Verification Summary",
    )


def build_export_bundle(application, actor):
    if not user_can_export_application(actor, application):
        raise ValueError("You cannot export this application.")
    bundle_root = _export_bundle_root(application)
    generated_at = timezone.now()
    evidence_exports = _collect_export_evidence(application, bundle_root)

    application_summary_path = f"{bundle_root}records/application_summary.pdf"
    submission_packet_path = f"{bundle_root}records/submission_packet.json"
    manifest_path = f"{bundle_root}records/case_manifest.json"
    inventory_csv_path = f"{bundle_root}inventory/evidence_inventory.csv"
    inventory_pdf_path = f"{bundle_root}inventory/evidence_inventory.pdf"
    audit_log_path = f"{bundle_root}logs/audit_log.csv"
    routing_history_path = f"{bundle_root}logs/routing_history.csv"
    verification_report_path = f"{bundle_root}verification/verification_report.json"
    verification_checksums_path = f"{bundle_root}verification/checksums.sha256"
    verification_summary_path = f"{bundle_root}verification/verification_summary.pdf"

    archive_entries = {
        application_summary_path: _build_application_pdf(
            application,
            actor=actor,
            generated_at=generated_at,
        ),
        submission_packet_path: _submission_packet_json(application),
        inventory_csv_path: _evidence_inventory_csv(
            application,
            actor,
            generated_at,
            evidence_exports,
        ),
        inventory_pdf_path: _build_evidence_inventory_pdf(
            application,
            actor,
            generated_at,
            evidence_exports,
        ),
        routing_history_path: _routing_history_csv(application),
    }
    for item in evidence_exports:
        archive_entries[item["export_path"]] = item["file_bytes"]

    planned_bundle_members = sorted(
        [
            *archive_entries.keys(),
            audit_log_path,
            manifest_path,
            verification_report_path,
            verification_checksums_path,
            verification_summary_path,
        ]
    )
    export_log = record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EXPORT_GENERATED,
        description="Generated controlled export package.",
        metadata={
            "generated_at": generated_at.isoformat(),
            "bundle_root": bundle_root,
            "source_application_id": application.id,
            "source_case_id": getattr(getattr(application, "case", None), "id", None),
            "evidence_item_count": len(evidence_exports),
            "bundle_member_count": len(planned_bundle_members),
            "inventory_files": [inventory_csv_path, inventory_pdf_path],
            "verification_files": [
                verification_report_path,
                verification_checksums_path,
                verification_summary_path,
            ],
            "evidence_item_ids": [item["id"] for item in evidence_exports],
        },
    )

    archive_entries[audit_log_path] = _audit_log_csv(application)
    archive_entries[manifest_path] = _manifest_json(
        application,
        actor=actor,
        generated_at=generated_at,
        bundle_root=bundle_root,
        evidence_exports=evidence_exports,
        bundle_members=planned_bundle_members,
        audit_log_path=audit_log_path,
        routing_history_path=routing_history_path,
        inventory_paths=[inventory_csv_path, inventory_pdf_path],
        verification_paths=[
            verification_report_path,
            verification_checksums_path,
            verification_summary_path,
        ],
        submission_packet_path=submission_packet_path,
        export_log=export_log,
    )

    verifiable_entries = dict(archive_entries)
    verification_report_bytes, verification_payload = _verification_report_json(
        application,
        actor=actor,
        generated_at=generated_at,
        bundle_root=bundle_root,
        verifiable_entries=verifiable_entries,
        evidence_exports=evidence_exports,
    )
    archive_entries[verification_report_path] = verification_report_bytes
    archive_entries[verification_checksums_path] = _verification_checksums_text(
        verification_payload
    )
    archive_entries[verification_summary_path] = _build_verification_summary_pdf(
        application,
        actor,
        generated_at,
        verification_payload,
    )

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(archive_entries.items()):
            archive.writestr(path, content)
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def persist_position(position, actor, changed_fields):
    is_create = position.pk is None
    position.full_clean()
    position.save()
    action = AuditLog.Action.POSITION_CREATED if is_create else AuditLog.Action.POSITION_UPDATED
    description = (
        f"Created position reference catalog record '{position.position_title}'."
        if is_create
        else f"Updated position reference catalog record '{position.position_title}'."
    )
    record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "position_id": position.id,
            "position_slug": position.position_slug,
            "position_title": position.position_title,
            "reference_status": position.reference_status,
            "changed_fields": changed_fields,
        },
    )
    return position


def persist_recruitment_entry(entry, actor, changed_fields):
    is_create = entry.pk is None
    entry.updated_by = actor
    if is_create and not entry.created_by_id:
        entry.created_by = actor
    entry.apply_position_reference_metadata()
    entry.full_clean()
    entry.save()
    action = (
        AuditLog.Action.RECRUITMENT_ENTRY_CREATED
        if is_create
        else AuditLog.Action.RECRUITMENT_ENTRY_UPDATED
    )
    description = (
        f"Created recruitment entry '{entry.job_code}'."
        if is_create
        else f"Updated recruitment entry '{entry.job_code}'."
    )
    record_system_audit_event(
        actor=actor,
        action=action,
        description=description,
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "engagement_type": entry.branch,
            "routing_basis": entry.level,
            "status": entry.status,
            "position_reference_id": entry.position_reference_id,
            "changed_fields": changed_fields,
        },
    )
    return entry


def update_recruitment_entry_status(entry, actor, new_status):
    previous_status = entry.status
    previous_closing_date = entry.closing_date
    previous_updated_by = entry.updated_by
    entry.status = new_status
    entry.updated_by = actor
    try:
        entry.full_clean()
    except ValidationError:
        entry.status = previous_status
        entry.closing_date = previous_closing_date
        entry.updated_by = previous_updated_by
        raise
    entry.save(update_fields=["status", "closing_date", "updated_by", "is_active", "updated_at"])
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
        description=f"Changed recruitment entry '{entry.job_code}' status from {previous_status} to {new_status}.",
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "old_status": previous_status,
            "new_status": new_status,
        },
    )
    return entry
