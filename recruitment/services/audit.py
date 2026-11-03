"""The audit log: the single writer (`record_audit_event`), protected-record access,
audit archives, and the audit queries and summaries.
"""

import csv
import hashlib
import json
from datetime import timedelta
from io import StringIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from config.client_ip import get_client_ip
from ..request_context import current_session_tag
from ..models import (
    AUDIT_ACTION_CATEGORIES,
    AUDIT_CATEGORY_BY_ACTION,
    audit_actions_for_category,
    AuditLog,
    AuditLogArchive,
    audit_action_codes_matching,
)
from . import evidence_vault


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
        # REV-F: stamp which sign-in produced this entry (empty outside a
        # request, e.g. management commands).
        session_tag=current_session_tag(),
    )


def record_system_audit_event(actor, action, description, metadata=None):
    return record_audit_event(
        application=None,
        actor=actor,
        action=action,
        description=description,
        metadata=metadata,
    )


def _request_ip_address(request):
    return get_client_ip(request) or None


def _request_user_agent(request):
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:1000]


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


# ── REV-D: audit log archival (Option B: export, verify, then delete) ──────

AUDIT_ARCHIVE_MIN_AGE_DAYS = 183  # entries younger than ~6 months can never be archived


def audit_archive_latest_allowed_cutoff():
    return timezone.localdate() - timedelta(days=AUDIT_ARCHIVE_MIN_AGE_DAYS)


def preview_audit_archive(cutoff_date):
    queryset = AuditLog.objects.filter(created_at__date__lt=cutoff_date)
    aggregate = queryset.aggregate(Min("created_at"), Max("created_at"))
    return {
        "count": queryset.count(),
        "range_start": aggregate["created_at__min"],
        "range_end": aggregate["created_at__max"],
    }


def archive_audit_logs(actor, cutoff_date):
    """Export every audit entry older than ``cutoff_date`` to an encrypted CSV
    (AuditLogArchive — the Secured Files system-archive category), verify the
    stored payload decrypts back to the same bytes, and only then delete the
    live rows. The whole operation is atomic: a failed verification rolls
    everything back with nothing deleted. The archival act itself is written
    to the audit log AFTER the delete, so it survives its own cleanup."""
    if cutoff_date > audit_archive_latest_allowed_cutoff():
        raise ValueError(
            "Only entries older than six months can be archived. Choose a date "
            f"on or before {audit_archive_latest_allowed_cutoff():%b %d, %Y}."
        )

    queryset = AuditLog.objects.filter(created_at__date__lt=cutoff_date).order_by("created_at")
    preview = queryset.aggregate(Min("created_at"), Max("created_at"))
    rows = list(
        queryset.values(
            "id",
            "created_at",
            "actor__username",
            "actor_role",
            "action",
            "case_reference",
            "workflow_stage",
            "description",
            "is_sensitive_access",
            "application_id",
            "metadata",
        )
    )
    if not rows:
        raise ValueError("There are no audit entries older than that date to archive.")

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id", "created_at", "actor", "actor_role", "action", "case_reference",
            "workflow_stage", "description", "is_sensitive_access", "application_id",
            "metadata_json",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["created_at"].isoformat(),
                row["actor__username"] or "",
                row["actor_role"],
                row["action"],
                row["case_reference"],
                row["workflow_stage"],
                row["description"],
                row["is_sensitive_access"],
                row["application_id"] or "",
                json.dumps(row["metadata"], default=str, ensure_ascii=False),
            ]
        )
    raw_bytes = buffer.getvalue().encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    nonce, ciphertext = evidence_vault.encrypt_evidence_bytes(raw_bytes)

    with transaction.atomic():
        archive = AuditLogArchive.objects.create(
            cutoff_date=cutoff_date,
            range_start=preview["created_at__min"],
            range_end=preview["created_at__max"],
            entry_count=len(rows),
            file_name=f"audit-archive-{cutoff_date:%Y%m%d}-{len(rows)}-entries.csv",
            file_size=len(raw_bytes),
            sha256_digest=digest,
            nonce=nonce,
            ciphertext=ciphertext,
            created_by=actor,
            created_by_role=getattr(actor, "role", ""),
        )
        # Delete-only-after-verified: read the stored payload back and prove it
        # decrypts to the exact bytes that were hashed. Any mismatch aborts the
        # transaction with the live rows untouched.
        stored = AuditLogArchive.objects.get(pk=archive.pk)
        recovered = AESGCM(evidence_vault._get_aes_key()).decrypt(
            bytes(stored.nonce), bytes(stored.ciphertext), None
        )
        if hashlib.sha256(recovered).hexdigest() != digest:
            raise ValueError("Archive verification failed; no entries were deleted.")
        deleted_count, _ = queryset.delete()
        record_system_audit_event(
            actor=actor,
            action=AuditLog.Action.AUDIT_LOG_ARCHIVED,
            description=(
                f"Archived {deleted_count} audit entries "
                f"({archive.range_start:%b %d, %Y} – {archive.range_end:%b %d, %Y}) "
                f"to {archive.file_name}."
            ),
            metadata={
                "archive_id": archive.pk,
                "file_name": archive.file_name,
                "entry_count": deleted_count,
                "cutoff_date": cutoff_date.isoformat(),
                "range_start": archive.range_start.isoformat(),
                "range_end": archive.range_end.isoformat(),
                "sha256_digest": digest,
            },
        )
    return archive


def download_audit_archive(archive, actor):
    """Decrypt an archive for the system administrator and put the access on
    the record."""
    content = AESGCM(evidence_vault._get_aes_key()).decrypt(
        bytes(archive.nonce), bytes(archive.ciphertext), None
    )
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.AUDIT_ARCHIVE_DOWNLOADED,
        description=f"Downloaded audit archive {archive.file_name}.",
        metadata={
            "archive_id": archive.pk,
            "file_name": archive.file_name,
            "entry_count": archive.entry_count,
        },
    )
    return content


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
        "review_scope": "application_audit" if application is not None else "consolidated_audit",
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
        description="Reviewed the consolidated audit log.",
        metadata=metadata,
    )


def _filter_audit_logs(
    queryset,
    *,
    search_query="",
    action="",
    actor_role="",
    sensitive_only=False,
    category="",
    actor=None,
    date_from=None,
    date_to=None,
    session_tag="",
):
    if search_query:
        # Also match the reviewer-facing action wording (e.g. "Secured Files
        # Viewed"), which is a display label rather than a stored field, so the
        # search covers what the reviewer actually sees on each record.
        search_filter = (
            Q(case_reference__icontains=search_query)
            | Q(workflow_stage__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(actor__username__icontains=search_query)
            | Q(actor__first_name__icontains=search_query)
            | Q(actor__last_name__icontains=search_query)
        )
        matching_action_codes = audit_action_codes_matching(search_query)
        if matching_action_codes:
            search_filter |= Q(action__in=matching_action_codes)
        queryset = queryset.filter(search_filter)
    if category:
        queryset = queryset.filter(action__in=audit_actions_for_category(category))
    if action:
        queryset = queryset.filter(action=action)
    if actor_role:
        queryset = queryset.filter(actor_role=actor_role)
    if actor is not None:
        queryset = queryset.filter(actor=actor)
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)
    if session_tag:
        queryset = queryset.filter(session_tag=session_tag)
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
    category="",
    actor=None,
    date_from=None,
    date_to=None,
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
        category=category,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
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


def get_all_audit_logs(
    *,
    search_query="",
    action="",
    actor_role="",
    sensitive_only=False,
    category="",
    actor=None,
    date_from=None,
    date_to=None,
    session_tag="",
):
    """Consolidated audit trail: system/identity events plus case activity.

    Unlike ``get_system_audit_logs`` this does not exclude application-scoped
    records, so the System Administrator's audit review shows the full trail of
    who acted on a case and when (oversight), while case content remains gated
    behind the per-case views.
    """
    queryset = (
        AuditLog.objects.select_related(
            "actor",
            "application",
            "application__position",
        )
        .order_by("-created_at")
    )
    return _filter_audit_logs(
        queryset,
        search_query=search_query,
        action=action,
        actor_role=actor_role,
        sensitive_only=sensitive_only,
        category=category,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        session_tag=session_tag,
    )


def summarize_audit_logs(queryset):
    """REV-F summary strip: what turns a filtered listing into a report —
    total actions, per-category counts, and the busiest day."""
    from django.db.models.functions import TruncDate

    total = queryset.count()
    category_counts = {key: 0 for key in AUDIT_ACTION_CATEGORIES}
    other = 0
    for row in queryset.values("action").annotate(n=Count("id")):
        key = AUDIT_CATEGORY_BY_ACTION.get(row["action"])
        if key is None:
            other += row["n"]
        else:
            category_counts[key] += row["n"]
    busiest = (
        queryset.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )
    return {
        "total": total,
        "categories": [
            {"key": key, "label": AUDIT_ACTION_CATEGORIES[key]["label"], "count": category_counts[key]}
            for key in AUDIT_ACTION_CATEGORIES
            if category_counts[key]
        ],
        "other_count": other,
        "busiest_day": busiest["day"] if busiest else None,
        "busiest_day_count": busiest["n"] if busiest else 0,
    }


def build_audit_session_summary(session_tag):
    """REV-F session view header: the sign-in that anchors the tag, the device
    facts recorded on it, and an honest ending (logout, or last activity)."""
    entries = AuditLog.objects.filter(session_tag=session_tag)
    if not entries.exists():
        return None
    login_entry = entries.filter(action=AuditLog.Action.INTERNAL_LOGIN).order_by("created_at").first()
    logout_entry = entries.filter(action=AuditLog.Action.INTERNAL_LOGOUT).order_by("-created_at").first()
    first = entries.order_by("created_at").first()
    last = entries.order_by("-created_at").first()
    anchor = login_entry or first
    metadata = anchor.metadata if isinstance(anchor.metadata, dict) else {}
    return {
        "session_tag": session_tag,
        "actor": anchor.actor,
        "actor_role": anchor.actor_role,
        "signed_in_at": login_entry.created_at if login_entry else None,
        "first_activity": first.created_at,
        "last_activity": last.created_at,
        "logout_at": logout_entry.created_at if logout_entry else None,
        "ip_address": metadata.get("ip_address", ""),
        "user_agent": metadata.get("user_agent", ""),
        "entry_count": entries.count(),
    }
