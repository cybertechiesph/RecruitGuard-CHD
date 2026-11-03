"""The evidence vault: encryption, evidence visibility, uploads, archive status,
generated PDFs and receipts, and the evidence export bundle with its verification
report.
"""

import csv
import hashlib
import json
import logging
import os
import textwrap
import uuid
import zipfile
from io import BytesIO, StringIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone, translation
from django.utils.text import slugify
from django.utils.translation import gettext, ngettext
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from ..models import (
    AuditLog,
    ComparativeAssessmentReport,
    EvidenceVaultItem,
    FinalDecision,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..permissions import WORKFLOW_PROCESSOR_ROLES
from ..upload_validation import (
    clamp_original_filename,
    validate_applicant_document_upload,
)
from . import access, audit, car, common, completion, intake, routing, selection


logger = logging.getLogger("recruitment.services")


ARTIFACT_TYPE_WORKFLOW_EVIDENCE = "workflow_evidence"
EVIDENCE_ARCHIVE_ROLES = WORKFLOW_PROCESSOR_ROLES


def user_can_upload_evidence(user, application):
    # The System Administrator is an oversight-only role and never adds file
    # content to a case; uploads belong to the applicant or the assigned
    # workflow processors.
    if user.role == RecruitmentUser.Role.APPLICANT and application.applicant_id == user.id:
        return application.is_editable_by_applicant
    return access.user_can_process_application(user, application)


def user_can_manage_evidence_archive(user, application):
    # Archiving mutates the secured-files store, so it stays with the workflow
    # custodians. The System Administrator oversees the trail but never mutates
    # evidence.
    return user.role in EVIDENCE_ARCHIVE_ROLES and access.user_can_view_application(user, application)


def user_can_export_application(user, application):
    return user.role in common.EXPORT_ROLES and access.user_can_view_application(user, application)


# Files that can carry the applicant's name, which the blind-reviewing panel never opens.
HRMPSB_RESTRICTED_ARTIFACT_TYPES = {
    common.ARTIFACT_TYPE_APPLICANT_DOCUMENT,
    common.ARTIFACT_TYPE_EXAM_EVIDENCE,
    common.ARTIFACT_TYPE_LETTER_OF_INTENT,
    common.ARTIFACT_TYPE_WITHDRAWAL_REQUEST,
}


def _evidence_stage_for_application(application):
    case = getattr(application, "case", None)
    if case and case.current_stage:
        return case.current_stage
    derived_stage = routing._review_stage_from_application_status(application.status)
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
    return [application.id for application in applications if access.user_can_view_application(user, application)]


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
        # Note: the System Administrator is intentionally NOT granted here.
        # Reading evidence content (download / decrypt) requires the ability to
        # view the owning case, which the oversight role does not have; the
        # admin sees the secured-files inventory as metadata only.
        if access.user_can_view_application(user, candidate) and evidence_belongs_to_application_context(
            evidence,
            candidate,
        ):
            return candidate
    return None


def get_evidence_owner_application(evidence):
    """Owning application for an evidence item, for display only.

    Resolves which application a secured file belongs to so its case reference
    can be shown in the oversight inventory. This grants no access — it does not
    authorize download or decryption, only labelling.
    """
    if evidence.application_id:
        return evidence.application
    if evidence.recruitment_case_id:
        return evidence.recruitment_case.application
    if evidence.recruitment_entry_id:
        return (
            RecruitmentApplication.objects.select_related("position", "case", "applicant")
            .filter(position_id=evidence.recruitment_entry_id)
            .order_by("-submitted_at", "-updated_at", "-created_at")
            .first()
        )
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
        if not access.user_can_view_application(user, application):
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
            "Evidence ownership must target exactly one scope: application, recruitment case, or vacancy."
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


def _get_aes_key():
    return hashlib.sha256(settings.EVIDENCE_ENCRYPTION_SECRET.encode("utf-8")).digest()


def encrypt_evidence_bytes(raw_bytes):
    nonce = os.urandom(12)
    cipher = AESGCM(_get_aes_key())
    return nonce, cipher.encrypt(nonce, raw_bytes, None)


def _decrypt_evidence_bytes(evidence):
    cipher = AESGCM(_get_aes_key())
    return cipher.decrypt(bytes(evidence.nonce), bytes(evidence.ciphertext), None)


def record_evidence_access_denied(evidence, actor, *, application=None, reason="unauthorized"):
    metadata = {
        "reason": reason,
        "evidence_id": evidence.id,
        "filename": evidence.original_filename,
        "stage": evidence.stage,
        "artifact_scope": evidence.artifact_scope,
        "artifact_type": evidence.artifact_type,
        "version_family": str(evidence.version_family),
        "version_number": evidence.version_number,
        "requested_application_id": application.id if application is not None else None,
    }
    return audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
        description="Denied evidence download request.",
        metadata=metadata,
    )


def decrypt_evidence_bytes(evidence, actor, *, application=None):
    if (
        actor.role == RecruitmentUser.Role.HRMPSB_MEMBER
        and evidence.artifact_type == common.ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT
    ):
        record_evidence_access_denied(
            evidence,
            actor,
            application=application,
            reason="car_artifact_restricted_for_hrmpsb",
        )
        raise ValueError(
            "HRMPSB members cannot download the Comparative Assessment Report file."
        )
    context_application = get_evidence_context_application_for_user(
        actor,
        evidence,
        preferred_application=application,
    )
    if context_application is None:
        record_evidence_access_denied(evidence, actor, application=application, reason="unauthorized")
        raise ValueError("You cannot access this evidence item.")
    if (
        actor.role == RecruitmentUser.Role.HRMPSB_MEMBER
        and evidence.artifact_type in HRMPSB_RESTRICTED_ARTIFACT_TYPES
    ):
        # Blind review: the panel's pages show candidate codes only, but these files
        # are the applicant's own papers (or exam sheets) and carry the name. No panel
        # page links them; this closes the guessed-URL route as well.
        record_evidence_access_denied(
            evidence,
            actor,
            application=application,
            reason="applicant_file_restricted_for_hrmpsb",
        )
        raise ValueError("HRMPSB members review candidates blind and cannot open this file.")
    plaintext = _decrypt_evidence_bytes(evidence)
    audit.record_audit_event(
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
        raise ValueError("Vacancy-owned generated evidence must point to a vacancy.")
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
        raise ValueError("Vacancy-owned evidence must stay linked to the same vacancy as the application.")
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
        original_filename=clamp_original_filename(filename),
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
    audit.record_audit_event(
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
    stage=None,
    permission_checked=False,
):
    # SL-07: ``permission_checked`` is for the few writes whose authority is a narrower,
    # action-specific predicate rather than "you are the assigned handler" — the Plantilla
    # paper-rating fallback, where the support office is deliberately allowed to file for a
    # board that owns the case. Every other caller leaves it alone and keeps the generic
    # check. Widening user_can_upload_evidence itself would silently widen every uploader.
    if not permission_checked and not user_can_upload_evidence(actor, application):
        raise ValueError("You cannot upload files for this application.")
    if artifact_scope == EvidenceVaultItem.OwnerScope.CASE and not hasattr(application, "case"):
        raise ValueError("A case must exist before case files can be uploaded.")
    validated_upload = validate_applicant_document_upload(uploaded_file)
    raw_bytes = validated_upload.raw_bytes
    content_type = validated_upload.canonical_content_type
    file_size = validated_upload.size_bytes
    sha256_digest = hashlib.sha256(raw_bytes).hexdigest()
    nonce, ciphertext = encrypt_evidence_bytes(raw_bytes)
    # ``stage`` is overridable so a scoped document resubmission keeps re-uploads on the
    # applicant-intake version chain even though the case is parked at a review stage.
    # Without this, _evidence_stage_for_application would stamp them with the review stage,
    # starting a new chain and hiding them from the applicant-document map.
    stage = stage or _evidence_stage_for_application(application)
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
        original_filename=clamp_original_filename(uploaded_file.name),
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
    audit.record_audit_event(
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
    audit.record_audit_event(
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


def _build_pdf_document(title, lines, *, document_title="DOH-CHD CALABARZON Export"):
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
    completion_record = completion.get_completion_record(application)
    evidence_items = list(get_evidence_items_for_application_context(application))
    generated_at = generated_at or timezone.now()
    actor_label = str(actor) if actor else "System"
    actor_role = actor.get_role_display() if actor else "System"
    assigned_role = (
        RecruitmentUser.Role(application.current_handler_role).label
        if application.current_handler_role
        else "Closed"
    )
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
        f"Assigned To: {assigned_role}",
        f"Current Step: {case.get_current_stage_display() if case else 'Not created'}",
        f"Case Status: {case.get_case_status_display() if case else 'N/A'}",
        f"Finalized: {'Yes' if getattr(case, 'is_stage_locked', False) else 'No'}",
        f"Submission Hash: {application.submission_hash or 'N/A'}",
        "",
        "Qualification Summary:",
        application.qualification_summary or "N/A",
        "",
        "Cover Letter:",
        application.cover_letter or "N/A",
        "",
        f"File Count: {len(evidence_items)}",
        f"Audit Entry Count: {application.audit_logs.count()}",
        f"Assignment Event Count: {application.routing_history.count()}",
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
        "DOH-CHD CALABARZON Controlled Export Summary",
        lines,
        document_title=application.reference_number or "DOH-CHD CALABARZON Export",
    )


# Applicant-facing receipt PDF (panel revision, Sir Joshua Napila: the
# application ID must be downloadable as a real file, not only browser-print).
# Brand palette mirrors static/css/recruitguard-doh-brand.css so the paper copy
# matches the portal. Unlike the plain controlled-export summary above, this is
# a designed document an applicant keeps.
_RECEIPT_GREEN = colors.HexColor("#1e5c2d")
_RECEIPT_GREEN_INK = colors.HexColor("#0e2f14")
_RECEIPT_GREEN_SOFT = colors.HexColor("#e6f0dc")
_RECEIPT_GOLD = colors.HexColor("#fdb813")
_RECEIPT_INK = colors.HexColor("#1f2933")
_RECEIPT_MUTE = colors.HexColor("#5f6c78")
_RECEIPT_HAIRLINE = colors.HexColor("#d7dee4")


def _brand_seal_reader():
    """The CHD CALABARZON seal as a reportlab ImageReader, or None if the asset
    is missing (the PDF then renders without it rather than erroring)."""
    seal_path = settings.BASE_DIR / "static" / "img" / "brand" / "chd-calabarzon-seal.png"
    if not seal_path.exists():
        return None
    try:
        return ImageReader(str(seal_path))
    except Exception:  # pragma: no cover - corrupt/unreadable asset
        return None


def _draw_application_receipt_header(pdf, *, left, right, page_height):
    """Draw the office identity shared by every receipt PDF page."""
    content_width = right - left
    top = page_height - 54
    seal = _brand_seal_reader()
    text_x = left
    if seal is not None:
        seal_size = 56
        pdf.drawImage(
            seal,
            left,
            top - seal_size + 6,
            width=seal_size,
            height=seal_size,
            preserveAspectRatio=True,
            mask="auto",
        )
        text_x = left + seal_size + 16

    pdf.setFillColor(_RECEIPT_MUTE)
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(text_x, top - 6, gettext("REPUBLIC OF THE PHILIPPINES"))
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(text_x, top - 22, "Department of Health")
    pdf.setFont("Helvetica", 9.5)
    pdf.setFillColor(_RECEIPT_INK)
    pdf.drawString(text_x, top - 36, "Center for Health Development — CALABARZON")
    pdf.setFont("Helvetica", 8.5)
    pdf.setFillColor(_RECEIPT_MUTE)
    pdf.drawString(text_x, top - 49, "Human Resource Management Unit")

    rule_y = top - 62
    pdf.setStrokeColor(_RECEIPT_GOLD)
    pdf.setLineWidth(2.4)
    pdf.line(left, rule_y, right, rule_y)
    pdf.setStrokeColor(_RECEIPT_GREEN)
    pdf.setLineWidth(0.8)
    pdf.line(left, rule_y - 3, right, rule_y - 3)
    return rule_y


def _application_receipt_requirement_groups(documents):
    """Build wrapped label/filename lines before drawing for page-height checks."""
    groups = []
    for index, document in enumerate(documents, start=1):
        label_text = f"{index}. {document.label or gettext('Submitted requirement')}"
        filename_text = f"— {document.original_filename or gettext('File')}"
        label_lines = textwrap.wrap(label_text, width=68) or [label_text]
        filename_lines = textwrap.wrap(filename_text, width=88) or [filename_text]
        lines = [(line, "") for line in label_lines]
        if filename_lines and len(label_lines[-1]) + len(filename_lines[0]) + 1 <= 88:
            lines[-1] = (label_lines[-1], filename_lines.pop(0))
        lines.extend(("", line) for line in filename_lines)
        groups.append(lines)
    return groups


_RECEIPT_REQUIREMENT_HEADING_HEIGHT = 18
_RECEIPT_REQUIREMENT_LINE_HEIGHT = 13
_RECEIPT_REQUIREMENT_ITEM_SPACING = 4
_RECEIPT_FOOTER_Y = 64
_RECEIPT_CONTENT_BOTTOM = _RECEIPT_FOOTER_Y + 60


def _application_receipt_requirement_item_height(lines):
    return (
        len(lines) * _RECEIPT_REQUIREMENT_LINE_HEIGHT
        + _RECEIPT_REQUIREMENT_ITEM_SPACING
    )


def _draw_application_receipt_requirements_heading(pdf, *, left, top, continued=False):
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    heading = gettext("Requirements Submitted (continued)") if continued else gettext("Requirements Submitted")
    pdf.drawString(left, top, heading)
    return top - _RECEIPT_REQUIREMENT_HEADING_HEIGHT


def _draw_application_receipt_requirement_item(pdf, *, left, y, lines):
    for label_text, filename_text in lines:
        text_x = left + 4
        if label_text:
            pdf.setFont("Helvetica", 9.5)
            pdf.setFillColor(_RECEIPT_INK)
            pdf.drawString(text_x, y, label_text)
            text_x += pdf.stringWidth(label_text, "Helvetica", 9.5) + 5
        if filename_text:
            pdf.setFont("Helvetica", 8.5)
            pdf.setFillColor(_RECEIPT_MUTE)
            pdf.drawString(text_x, y, filename_text)
        y -= _RECEIPT_REQUIREMENT_LINE_HEIGHT
    y -= _RECEIPT_REQUIREMENT_ITEM_SPACING
    return y


def _application_receipt_follow_up_height(steps):
    step_lines_height = sum(
        (len(textwrap.wrap(step, width=92)) * 13) + 4 for step in steps
    )
    return 16 + step_lines_height + 16 + 62


def _draw_application_receipt_footer(pdf, *, left, right, generated_at):
    pdf.setStrokeColor(_RECEIPT_HAIRLINE)
    pdf.setLineWidth(0.8)
    pdf.line(left, _RECEIPT_FOOTER_Y, right, _RECEIPT_FOOTER_Y)
    pdf.setFont("Helvetica", 7.5)
    pdf.setFillColor(_RECEIPT_MUTE)
    pdf.drawString(
        left,
        _RECEIPT_FOOTER_Y - 14,
        gettext("Generated %(when)s. This is a system-generated receipt and needs no signature.")
        % {"when": _receipt_datetime(generated_at)},
    )
    pdf.drawString(
        left,
        _RECEIPT_FOOTER_Y - 25,
        gettext("Keep your Tracking Code private — treat it like a password."),
    )


def _receipt_datetime(value):
    local = timezone.localtime(value)
    return f"{local:%B} {local.day}, {local.year}, {local:%I:%M %p}".replace(", 0", ", ")


def _build_application_receipt_pdf(application, *, generated_at=None):
    # The receipt is written in the language the applicant used on the portal.
    language = getattr(application, "preferred_language", "") or settings.LANGUAGE_CODE
    if language not in dict(settings.LANGUAGES):
        language = settings.LANGUAGE_CODE
    with translation.override(language):
        return _render_application_receipt_pdf(application, generated_at=generated_at)


def _render_application_receipt_pdf(application, *, generated_at=None):
    generated_at = generated_at or timezone.now()
    branch_value = getattr(application, "branch", "")
    engagement = (
        gettext("Permanent (Plantilla)")
        if branch_value == PositionPosting.Branch.PLANTILLA
        else gettext("Contract of Service")
    )
    submitted_at = application.submitted_at
    documents = list(intake.get_current_applicant_document_items(application))
    document_count = len(documents)

    buffer = BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Application Receipt — {application.case_number_short or application.reference_number}")

    left = 52
    right = page_width - 52
    content_width = right - left

    # ── Official header: seal + office identity, gold rule beneath ──────
    rule_y = _draw_application_receipt_header(
        pdf,
        left=left,
        right=right,
        page_height=page_height,
    )

    # ── Document title ─────────────────────────────────────────────────
    title_y = rule_y - 32
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawCentredString(page_width / 2, title_y, gettext("Application Submission Receipt"))
    pdf.setFont("Helvetica", 9.5)
    pdf.setFillColor(_RECEIPT_MUTE)
    pdf.drawCentredString(
        page_width / 2,
        title_y - 15,
        gettext("Official proof of a received application. Please keep this copy."),
    )

    # ── Identifier panel: tracking code + case reference ───────────────
    panel_top = title_y - 34
    panel_height = 96
    panel_bottom = panel_top - panel_height
    pdf.setFillColor(_RECEIPT_GREEN_SOFT)
    pdf.setStrokeColor(_RECEIPT_GREEN)
    pdf.setLineWidth(1.1)
    pdf.roundRect(left, panel_bottom, content_width, panel_height, 8, stroke=1, fill=1)

    mid_x = left + content_width / 2
    pdf.setStrokeColor(colors.HexColor("#bcd3a8"))
    pdf.setLineWidth(0.8)
    pdf.line(mid_x, panel_bottom + 14, mid_x, panel_top - 14)

    col_l = left + 22
    col_r = mid_x + 22
    label_y = panel_top - 22
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.drawString(col_l, label_y, gettext("TRACKING CODE  (keep private)"))
    pdf.drawString(col_r, label_y, gettext("CASE REFERENCE"))

    pdf.setFont("Helvetica-Bold", 20)
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.drawString(col_l, label_y - 27, application.tracking_code or "—")
    pdf.drawString(col_r, label_y - 27, f"No. {application.case_number_short or '—'}")

    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(_RECEIPT_MUTE)
    pdf.drawString(col_l, label_y - 44, gettext("Never share it. Staff will never ask for it."))
    pdf.drawString(col_r, label_y - 44, gettext("Quote this when you contact the office."))

    # ── Details table ──────────────────────────────────────────────────
    rows = [
        (gettext("Applicant"), application.applicant_display_name or "—"),
        (gettext("Position"), application.position.title),
        (gettext("Vacancy code"), application.position.job_code),
        (gettext("Position type"), engagement),
        (
            gettext("Date Submitted"),
            _receipt_datetime(submitted_at) if submitted_at else "—",
        ),
        (gettext("Registered Email"), application.applicant_email or "—"),
        (
            gettext("Documents Submitted"),
            ngettext("%(count)s file", "%(count)s files", document_count) % {"count": document_count},
        ),
    ]
    row_height = 23
    table_top = panel_bottom - 26
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.drawString(left, table_top, gettext("Application Details"))
    table_top -= 8

    y = table_top
    for index, (label, value) in enumerate(rows):
        row_bottom = y - row_height
        if index % 2 == 0:
            pdf.setFillColor(colors.HexColor("#f5f8f2"))
            pdf.rect(left, row_bottom, content_width, row_height, stroke=0, fill=1)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColor(_RECEIPT_MUTE)
        pdf.drawString(left + 10, row_bottom + 8, label)
        pdf.setFont("Helvetica", 9.5)
        pdf.setFillColor(_RECEIPT_INK)
        value_lines = textwrap.wrap(str(value), width=52) or [""]
        pdf.drawString(left + 190, row_bottom + 8, value_lines[0])
        y = row_bottom
    pdf.setStrokeColor(_RECEIPT_HAIRLINE)
    pdf.setLineWidth(0.8)
    pdf.rect(left, y, content_width, table_top - y, stroke=1, fill=0)

    # ── Itemized requirements ─────────────────────────────────────────
    requirement_groups = _application_receipt_requirement_groups(documents)
    requirements_top = y - 30
    steps = [
        gettext("Your application will be reviewed by the recruitment team."),
        gettext("You may be contacted by email or phone for updates or additional requirements."),
        gettext("Check your status anytime on My Applications with just your email."),
    ]

    first_item_height = (
        _application_receipt_requirement_item_height(requirement_groups[0])
        if requirement_groups
        else 0
    )
    if (
        requirements_top
        - _RECEIPT_REQUIREMENT_HEADING_HEIGHT
        - first_item_height
        < _RECEIPT_CONTENT_BOTTOM
    ):
        _draw_application_receipt_footer(
            pdf,
            left=left,
            right=right,
            generated_at=generated_at,
        )
        pdf.showPage()
        continued_rule_y = _draw_application_receipt_header(
            pdf,
            left=left,
            right=right,
            page_height=page_height,
        )
        requirements_top = continued_rule_y - 30

    y = _draw_application_receipt_requirements_heading(
        pdf, left=left, top=requirements_top
    )
    first_page_item_count = 0
    projected_y = y
    for lines in requirement_groups:
        item_height = _application_receipt_requirement_item_height(lines)
        if projected_y - item_height < _RECEIPT_CONTENT_BOTTOM:
            break
        projected_y -= item_height
        first_page_item_count += 1

    # Do not leave a continuation page containing only the follow-up unit.
    # Carry the final requirement with it so the repeated heading introduces
    # real list content and the second page reads as a deliberate continuation.
    projected_next_top = projected_y - 30
    if (
        first_page_item_count == len(requirement_groups)
        and len(requirement_groups) > 1
        and projected_next_top - _application_receipt_follow_up_height(steps)
        < _RECEIPT_CONTENT_BOTTOM
    ):
        first_page_item_count -= 1

    for index, lines in enumerate(requirement_groups):
        item_height = _application_receipt_requirement_item_height(lines)
        if (
            index == first_page_item_count
            or y - item_height < _RECEIPT_CONTENT_BOTTOM
        ):
            _draw_application_receipt_footer(
                pdf,
                left=left,
                right=right,
                generated_at=generated_at,
            )
            pdf.showPage()
            continued_rule_y = _draw_application_receipt_header(
                pdf,
                left=left,
                right=right,
                page_height=page_height,
            )
            y = _draw_application_receipt_requirements_heading(
                pdf,
                left=left,
                top=continued_rule_y - 30,
                continued=True,
            )
        y = _draw_application_receipt_requirement_item(
            pdf,
            left=left,
            y=y,
            lines=lines,
        )

    # ── What happens next ──────────────────────────────────────────────
    next_top = y - 30
    if (
        next_top - _application_receipt_follow_up_height(steps)
        < _RECEIPT_CONTENT_BOTTOM
    ):
        _draw_application_receipt_footer(
            pdf,
            left=left,
            right=right,
            generated_at=generated_at,
        )
        pdf.showPage()
        continued_rule_y = _draw_application_receipt_header(
            pdf,
            left=left,
            right=right,
            page_height=page_height,
        )
        next_top = continued_rule_y - 30
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.setFillColor(_RECEIPT_GREEN_INK)
    pdf.drawString(left, next_top, gettext("What Happens Next"))
    pdf.setFont("Helvetica", 9.5)
    step_y = next_top - 16
    for number, step in enumerate(steps, start=1):
        pdf.setFillColor(_RECEIPT_GREEN)
        pdf.setFont("Helvetica-Bold", 9.5)
        pdf.drawString(left + 4, step_y, f"{number}.")
        pdf.setFillColor(_RECEIPT_INK)
        pdf.setFont("Helvetica", 9.5)
        for wrapped in textwrap.wrap(step, width=92):
            pdf.drawString(left + 20, step_y, wrapped)
            step_y -= 13
        step_y -= 4

    # ── Reminder callout (carries the receipt page's privacy warning) ──
    reminder_top = step_y - 16
    reminder_height = 62
    reminder_bottom = reminder_top - reminder_height
    pdf.setFillColor(colors.HexColor("#fff4d6"))
    pdf.setStrokeColor(_RECEIPT_GOLD)
    pdf.setLineWidth(1)
    pdf.roundRect(left, reminder_bottom, content_width, reminder_height, 6, stroke=1, fill=1)
    pdf.setStrokeColor(_RECEIPT_GOLD)
    pdf.setLineWidth(3)
    pdf.line(left + 1.5, reminder_bottom + 5, left + 1.5, reminder_top - 5)
    pdf.setFillColor(colors.HexColor("#8a5a00"))
    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawString(left + 16, reminder_top - 18, gettext("Keep this receipt and your Tracking Code safe."))
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(_RECEIPT_INK)
    reminder_text = gettext(
        "Staff will never ask for your Tracking Code; give your case number instead. A "
        "confirmation was also sent to %(email)s."
    ) % {"email": application.applicant_email or gettext("your registered email")}
    text_y = reminder_top - 34
    for wrapped in textwrap.wrap(reminder_text, width=98):
        pdf.drawString(left + 16, text_y, wrapped)
        text_y -= 12

    # ── Footer ─────────────────────────────────────────────────────────
    _draw_application_receipt_footer(
        pdf,
        left=left,
        right=right,
        generated_at=generated_at,
    )

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


def _build_comparative_assessment_report_pdf(
    application,
    actor,
    candidate_rows,
    generation_number,
    summary_notes,
    assessment_weights,
):
    lines = [
        "DOH-CHD CALABARZON Comparative Assessment Report",
        f"Vacancy: {application.position.title} [{application.position.job_code}]",
        f"Branch: {application.position.get_branch_display()}",
        f"Workflow Stage: {application.case.get_current_stage_display()}",
        f"Prepared By: {actor}",
        f"Prepared At: {timezone.now():%Y-%m-%d %H:%M}",
        f"Generation Version: {generation_number}",
        f"Preliminary Ranking Basis: {car._assessment_weight_display(application.position, assessment_weights) or 'Not available'}",
        "",
        "Ranked Candidates",
    ]
    if summary_notes:
        lines.extend(["Summary Notes:", summary_notes, ""])
    for row in candidate_rows:
        candidate_code = row["application"].candidate_code
        if not candidate_code:
            logger.warning(
                "CAR artifact candidate application_id=%s has no candidate code; using fallback.",
                row["application"].pk,
            )
            candidate_code = "C-??"
        lines.extend(
            [
                (
                    # SL-08: the archived sheet says "tied" in words, so the equal score and
                    # the equal rank both survive in the filed copy.
                    f"Final Rank {row['rank_order'] or 'Unranked'}"
                    f"{' (tied)' if row.get('is_tied') else ''} | "
                    f"Preliminary Rank {row['preliminary_rank_order'] or 'N/A'} | "
                    f"{candidate_code}"
                ),
                (
                    f"Document Review: {row['qualification_outcome'] or 'N/A'} "
                    f"({row['document_review_score'] or 'N/A'}) | "
                    f"Exam: {row['exam_status'] or 'N/A'} ({row['exam_score'] or 'N/A'}) | "
                    f"Interview Avg: {row['interview_average_score'] or 'N/A'}"
                ),
                f"Preliminary Assessment Score: {common._decimal_string(row['assessment_score']) or 'N/A'}",
                f"Exam Components: {row.get('exam_components') or 'N/A'}",
                f"HRMPSB Recommendation: {row.get('recommendation') or 'N/A'}",
                row["decision_support_summary"] or "No decision-support summary recorded.",
                f"Ranking Notes: {row['ranking_notes'] or 'N/A'}",
                "",
            ]
        )
    return _build_pdf_document(
        "DOH-CHD CALABARZON Comparative Assessment Report",
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
    return json.dumps(intake.build_submission_packet(application), indent=2).encode("utf-8")


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
    scope_component = _safe_export_path_component(evidence.artifact_scope, "file")
    stage_component = _safe_export_path_component(evidence.stage, "unstaged")
    document_component = evidence.document_key or _safe_export_path_component(evidence.label, "file")
    fallback_name = f"{document_component}.bin"
    filename = _safe_export_filename(evidence.original_filename, fallback_name)
    folder = f"{bundle_root}evidence/{scope_component}/{stage_component}/{document_component}/"
    if evidence.artifact_type == common.ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT:
        # F12: every "Update draft" stores a PDF for the audit trail, but only a finalized
        # CAR is the record. Drafts go to a working-copies folder; the official one is named
        # by its official version, matching what the screens show.
        report = ComparativeAssessmentReport.objects.filter(evidence_item=evidence).first()
        if report is not None and not report.is_finalized:
            return f"{folder}working-copies/draft-{evidence.version_label}_{filename}"
        if report is not None:
            return f"{folder}car-official-version-{report.official_version}.pdf"
    return f"{folder}{evidence.version_label}_{filename}"


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
            "file_scope",
            "file_scope_label",
            "file_type",
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
        f"File Count: {len(evidence_exports)}",
    ]
    if not evidence_exports:
        lines.extend(
            [
                "",
                "No saved files were present for this export.",
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
        "DOH-CHD CALABARZON Evidence Inventory",
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
    completion_record = completion.get_completion_record(application)
    submission_packet = intake.build_submission_packet(application)
    final_selection = selection.get_final_selection_for_application(application)
    final_selection_outcome = ""
    if final_selection:
        final_selection_outcome = (
            FinalDecision.Outcome.SELECTED
            if application.id == final_selection.selected_application_id
            else FinalDecision.Outcome.NOT_SELECTED
        )
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
            for decision in selection.get_final_decision_history(application)
        ],
        "final_selection": (
            {
                "id": final_selection.id,
                "comparative_assessment_report_id": final_selection.comparative_assessment_report_id,
                "selected_item_id": final_selection.selected_item_id,
                "selected_rank_order": final_selection.selected_item.rank_order,
                "selected_application_id": final_selection.selected_application_id,
                "selected_case_id": final_selection.selected_case_id,
                "selected_applicant_name": final_selection.selected_application.applicant_display_name,
                "decision_outcome_for_this_application": final_selection_outcome,
                "is_deep_selection": final_selection.is_deep_selection,
                "deep_selection_justification": final_selection.deep_selection_justification,
                "decision_notes": final_selection.decision_notes,
                "decided_by_role": final_selection.decided_by_role,
                "decided_at": final_selection.decided_at.isoformat() if final_selection.decided_at else "",
                "car_snapshot": final_selection.car_snapshot,
            }
            if final_selection
            else {}
        ),
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
        "DOH-CHD CALABARZON Verification Summary",
        lines,
        document_title=f"{application.reference_number} Verification Summary",
    )


def record_export_denied(application, actor, *, reason="unauthorized"):
    return audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EXPORT_DENIED,
        description="Denied controlled export request.",
        metadata={
            "reason": reason,
            "requested_application_id": application.id,
        },
    )


def build_export_bundle(application, actor):
    if not user_can_export_application(actor, application):
        record_export_denied(application, actor, reason="unauthorized")
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
    export_log = audit.record_audit_event(
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
