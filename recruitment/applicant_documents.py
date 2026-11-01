"""Multi-file applicant documents (the redesigned applicant intake).

An applicant can add several files or photos to one document requirement — page 1, page 2,
... of a PDS, or a stack of training certificates — and each file is uploaded on its own as
soon as it is picked. Each one is stored as an encrypted ApplicantDocumentPart.

Whenever a requirement's parts change, its applicant document in the Evidence Vault is
rebuilt so the rest of the system (missing-document checks, duplicate checks, screening,
the receipt) keeps seeing exactly one current file per requirement:

  * no parts       -> the requirement's current document is archived;
  * one part       -> that file is stored as-is (an e-signed PDF keeps its signature);
  * several parts  -> they are combined, in order, into one PDF.

Photos are cleaned on the way in: rotated upright, shrunk to a sensible size, and re-saved
without their metadata (phone photos carry GPS location in EXIF).

Parts are draft-only staging data; purge_applicant_document_parts() deletes them after the
application is submitted.
"""

import hashlib
import io
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .models import ApplicantDocumentPart, AuditLog, EvidenceVaultItem, RecruitmentCase
from .requirements import SIGNED_COVER_LETTER, get_applicant_document_requirements
from .services import (
    ARTIFACT_TYPE_APPLICANT_DOCUMENT,
    _archive_applicant_document_key,
    _decrypt_evidence_bytes,
    _get_aes_key,
    encrypt_evidence_bytes,
    get_current_applicant_document_map,
    record_audit_event,
    store_generated_evidence_item,
)
from .upload_validation import clamp_original_filename, validate_applicant_document_upload

MAX_PARTS_PER_DOCUMENT = 10
MAX_COMBINED_DOCUMENT_BYTES = 20 * 1024 * 1024
# Long edge for stored photos: sharp enough to read a transcript, small enough to upload
# on a phone connection. The browser shrinks to the same size before uploading.
MAX_PHOTO_EDGE_PX = 2400
PHOTO_JPEG_QUALITY = 85
# Refuse decompression bombs (a tiny file that expands to an enormous image).
Image.MAX_IMAGE_PIXELS = 60_000_000

ERROR_TOO_MANY_PARTS = format_lazy(
    _(
        "You can add up to {max} files to one document. "
        "Combine some pages into one PDF, or remove a file first."
    ),
    max=MAX_PARTS_PER_DOCUMENT,
)
ERROR_COMBINED_TOO_LARGE = _(
    "These files are too large together. Remove a file or use smaller photos."
)
ERROR_SAME_FILE_HERE = _("You already added this file to this document.")
ERROR_SAME_FILE_ELSEWHERE = _(
    "This file is already used for another document. Each document needs its own file."
)
ERROR_PDF_UNREADABLE = _(
    "We couldn't open this PDF. Save it again as a PDF (or take photos of the pages), then upload it."
)
ERROR_PDF_PROTECTED = _(
    "This PDF is password-protected. Save a copy without a password, then upload it."
)
ERROR_IMAGE_UNREADABLE = _("We couldn't open this photo. Take it again or save it as JPG or PNG.")


@dataclass(frozen=True)
class PreparedPart:
    filename: str
    content_type: str
    raw_bytes: bytes


def _decrypt_part(part):
    return AESGCM(_get_aes_key()).decrypt(bytes(part.nonce), bytes(part.ciphertext), None)


def read_applicant_document_part(part):
    """Decrypted bytes of one part (for the applicant's own preview)."""
    return _decrypt_part(part)


def _clean_photo(raw_bytes, detected_format, filename):
    """Rotate upright, shrink, and strip metadata. Returns (bytes, content_type, filename)."""
    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((MAX_PHOTO_EDGE_PX, MAX_PHOTO_EDGE_PX))
            output = io.BytesIO()
            if detected_format == "png":
                # PNGs are usually screenshots or scans with text; keep them lossless.
                image.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png", filename
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=PHOTO_JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ValueError(ERROR_IMAGE_UNREADABLE) from exc
    return output.getvalue(), "image/jpeg", filename


def _check_pdf(raw_bytes):
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        if reader.is_encrypted:
            raise ValueError(ERROR_PDF_PROTECTED)
        if len(reader.pages) == 0:
            raise ValueError(ERROR_PDF_UNREADABLE)
    except (PdfReadError, OSError, KeyError, TypeError) as exc:
        raise ValueError(ERROR_PDF_UNREADABLE) from exc


OLD_PDS_PATTERN = re.compile(r"revised\s*2017", re.IGNORECASE)
CURRENT_PDS_PATTERN = re.compile(r"revised\s*2026", re.IGNORECASE)


def looks_like_old_pds(part):
    """True when an uploaded PDS is a typed PDF of the superseded 2017 form (its footer reads
    "CS FORM 212 (Revised 2017)"). Scans and photos have no text, so they are not judged."""
    if part.content_type != "application/pdf":
        return False
    try:
        reader = PdfReader(io.BytesIO(read_applicant_document_part(part)))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception:  # unreadable text layer: say nothing rather than guess
        return False
    return bool(OLD_PDS_PATTERN.search(text)) and not CURRENT_PDS_PATTERN.search(text)


def prepare_part_upload(uploaded_file):
    """Validate one uploaded file (same rules as every applicant upload) and clean photos."""
    validated = validate_applicant_document_upload(uploaded_file)
    filename = clamp_original_filename(getattr(uploaded_file, "name", "") or "document")
    if validated.detected_format == "pdf":
        _check_pdf(validated.raw_bytes)
        return PreparedPart(filename, validated.canonical_content_type, validated.raw_bytes)
    raw_bytes, content_type, filename = _clean_photo(
        validated.raw_bytes, validated.detected_format, filename
    )
    return PreparedPart(filename, content_type, raw_bytes)


def _image_page_pdf(raw_bytes):
    """One A4 page with the photo fitted inside a 1 cm margin, in its own orientation."""
    with Image.open(io.BytesIO(raw_bytes)) as image:
        width_px, height_px = image.size
    page_w, page_h = A4
    if width_px > height_px:
        page_w, page_h = page_h, page_w
    margin = 28  # ~1 cm
    scale = min((page_w - 2 * margin) / width_px, (page_h - 2 * margin) / height_px)
    draw_w, draw_h = width_px * scale, height_px * scale
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(page_w, page_h))
    pdf.drawImage(
        ImageReader(io.BytesIO(raw_bytes)),
        (page_w - draw_w) / 2,
        (page_h - draw_h) / 2,
        width=draw_w,
        height=draw_h,
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def combine_parts_to_pdf(parts_bytes_and_types):
    """[(bytes, content_type), ...] in order -> one PDF (bytes)."""
    writer = PdfWriter()
    for raw_bytes, content_type in parts_bytes_and_types:
        source = raw_bytes if content_type == "application/pdf" else _image_page_pdf(raw_bytes)
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def combine_uploaded_files(requirement, uploaded_files):
    """Several files picked at once for one document (the resubmission page) -> one stored
    document, under the same rules as intake parts. Returns (filename, content_type, bytes,
    part_digests). Errors name the file they are about."""
    if len(uploaded_files) > MAX_PARTS_PER_DOCUMENT:
        raise ValueError(f"{requirement.title}: {ERROR_TOO_MANY_PARTS}")
    prepared, digests = [], []
    for uploaded_file in uploaded_files:
        try:
            part = prepare_part_upload(uploaded_file)
        except ValueError as exc:
            raise ValueError(f"{getattr(uploaded_file, 'name', 'A file')}: {exc}") from exc
        digest = hashlib.sha256(part.raw_bytes).hexdigest()
        if digest in digests:
            raise ValueError(f"{part.filename}: {ERROR_SAME_FILE_HERE}")
        prepared.append(part)
        digests.append(digest)
    if sum(len(part.raw_bytes) for part in prepared) > MAX_COMBINED_DOCUMENT_BYTES:
        raise ValueError(f"{requirement.title}: {ERROR_COMBINED_TOO_LARGE}")
    raw_bytes = combine_parts_to_pdf([(part.raw_bytes, part.content_type) for part in prepared])
    return f"{requirement.code.replace('_', '-')}.pdf", "application/pdf", raw_bytes, digests


def get_document_parts(application, document_key=None):
    parts = ApplicantDocumentPart.objects.filter(application=application)
    if document_key:
        parts = parts.filter(document_key=document_key)
    return parts.order_by("document_key", "sort_order", "created_at", "pk")


def _requirement_or_error(application, document_key):
    for requirement in get_applicant_document_requirements(application.position):
        if requirement.code == document_key:
            return requirement
    raise ValueError(_("This document is not part of this application."))


def _ensure_editable(application):
    if not application.is_editable_by_applicant or application.submitted_at:
        raise ValueError(_("This application has already been submitted and can no longer be changed."))


@transaction.atomic
def add_applicant_document_part(application, document_key, uploaded_file):
    """Validate, clean and store one file for a requirement, then rebuild that document."""
    _ensure_editable(application)
    requirement = _requirement_or_error(application, document_key)
    part = _store_part(application, document_key, uploaded_file)
    rebuild_applicant_document(application, requirement)
    return part


def _store_part(application, document_key, uploaded_file):
    """Validate, clean and encrypt one file as a part of ``document_key`` (shared by drafts
    and by returned-document resubmissions)."""
    prepared = prepare_part_upload(uploaded_file)
    digest = hashlib.sha256(prepared.raw_bytes).hexdigest()

    existing = list(get_document_parts(application, document_key))
    if len(existing) >= MAX_PARTS_PER_DOCUMENT:
        raise ValueError(ERROR_TOO_MANY_PARTS)
    if any(part.sha256_digest == digest for part in existing):
        raise ValueError(ERROR_SAME_FILE_HERE)
    if get_document_parts(application).exclude(document_key=document_key).filter(
        sha256_digest=digest
    ).exists():
        raise ValueError(ERROR_SAME_FILE_ELSEWHERE)
    if sum(part.size_bytes for part in existing) + len(prepared.raw_bytes) > MAX_COMBINED_DOCUMENT_BYTES:
        raise ValueError(ERROR_COMBINED_TOO_LARGE)

    nonce, ciphertext = encrypt_evidence_bytes(prepared.raw_bytes)
    part = ApplicantDocumentPart.objects.create(
        application=application,
        document_key=document_key,
        sort_order=(max((p.sort_order for p in existing), default=0) + 1),
        original_filename=prepared.filename,
        content_type=prepared.content_type,
        size_bytes=len(prepared.raw_bytes),
        sha256_digest=digest,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    return part


@transaction.atomic
def remove_applicant_document_part(application, part_id):
    _ensure_editable(application)
    part = get_document_parts(application).filter(pk=part_id).first()
    if part is None:
        raise ValueError(_("That file is no longer part of this application."))
    requirement = _requirement_or_error(application, part.document_key)
    part.delete()
    rebuild_applicant_document(application, requirement)


@transaction.atomic
def clear_applicant_document_parts(application, document_key, archive_tag):
    """Drop every part of a requirement that no longer applies (e.g. performance rating
    marked not applicable) and archive its document."""
    _ensure_editable(application)
    get_document_parts(application, document_key).delete()
    _archive_applicant_document_key(application, document_key, application.applicant, archive_tag)


def rebuild_applicant_document(application, requirement):
    """Make the requirement's current Evidence Vault document match its parts."""
    parts = list(get_document_parts(application, requirement.code))
    if not parts:
        _archive_applicant_document_key(
            application,
            requirement.code,
            application.applicant,
            "Removed by applicant before submission",
        )
        return None

    if len(parts) == 1:
        part = parts[0]
        raw_bytes = _decrypt_part(part)
        filename, content_type = part.original_filename, part.content_type
    else:
        raw_bytes = combine_parts_to_pdf([(_decrypt_part(p), p.content_type) for p in parts])
        filename, content_type = f"{requirement.code.replace('_', '-')}.pdf", "application/pdf"

    current = get_current_applicant_document_map(application).get(requirement.code)
    if current is not None and current.sha256_digest == hashlib.sha256(raw_bytes).hexdigest():
        return current
    evidence = store_generated_evidence_item(
        application=application,
        actor=application.applicant,
        label=requirement.title,
        filename=filename,
        raw_bytes=raw_bytes,
        content_type=content_type,
        artifact_type=ARTIFACT_TYPE_APPLICANT_DOCUMENT,
        artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
        stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
        document_key=requirement.code,
    )
    if len(parts) > 1:
        record_audit_event(
            application=application,
            actor=application.applicant,
            action=AuditLog.Action.EVIDENCE_UPLOADED,
            description=(
                f"Combined {len(parts)} applicant files into one PDF for '{requirement.title}'."
            ),
            metadata={
                "document_key": requirement.code,
                "evidence_id": evidence.id,
                "part_digests": [p.sha256_digest for p in parts],
            },
        )
    return evidence


NON_REUSABLE_DOCUMENT_KEYS = frozenset({SIGNED_COVER_LETTER})


def get_reusable_documents(application):
    """{document_key: EvidenceVaultItem}: for each of this draft's requirements, the newest
    current document the SAME applicant (same verified email identity) already submitted in
    another application. Lets someone applying to several vacancies skip re-uploading their
    PDS, diploma, etc. Documents still out for correction elsewhere are not offered.

    The signed cover/application letter is never offered: it names the position it was
    written for, so reusing it would send a letter for the wrong job."""
    codes = {
        requirement.code
        for requirement in get_applicant_document_requirements(application.position)
        if requirement.code not in NON_REUSABLE_DOCUMENT_KEYS
    }
    items = (
        EvidenceVaultItem.objects.filter(
            application__applicant=application.applicant,
            application__submitted_at__isnull=False,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            artifact_type=ARTIFACT_TYPE_APPLICANT_DOCUMENT,
            is_current_version=True,
            is_archived=False,
            size_bytes__gt=0,
            document_key__in=codes,
        )
        .exclude(application=application)
        .exclude(application__case__case_status=RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION)
        .select_related("application__position")
        .order_by("-created_at", "-pk")
    )
    reusable = {}
    for item in items:
        reusable.setdefault(item.document_key, item)
    return reusable


def reuse_applicant_document(application, document_key, evidence_id):
    """Copy a document from the applicant's earlier application into this draft, as a part
    (same validation, duplicate checks and audit as a fresh upload)."""
    item = get_reusable_documents(application).get(document_key)
    if item is None or item.pk != evidence_id:
        raise ValueError(_("That document can no longer be reused. Add the file instead."))
    upload = SimpleUploadedFile(
        item.original_filename,
        _decrypt_evidence_bytes(item),
        content_type=item.content_type or "application/octet-stream",
    )
    part = add_applicant_document_part(application, document_key, upload)
    record_audit_event(
        application=application,
        actor=application.applicant,
        action=AuditLog.Action.EVIDENCE_UPLOADED,
        description=f"Reused a document from the applicant's application {item.application.reference_label}.",
        metadata={"document_key": document_key, "source_evidence_id": item.pk, "part_id": part.pk},
    )
    return part


def purge_applicant_document_parts(application):
    """Delete staging parts once the application is submitted (the Evidence Vault copy is
    the record from then on)."""
    deleted, _ = ApplicantDocumentPart.objects.filter(application=application).delete()
    return deleted


def build_document_slot_states(application, requirements=None, *, performance_rating_applicable=True):
    """Per-requirement state for the application page and its JSON responses."""
    requirements = requirements or get_applicant_document_requirements(application.position)
    parts_by_key = {}
    for part in get_document_parts(application):
        parts_by_key.setdefault(part.document_key, []).append(part)
    current_documents = get_current_applicant_document_map(application)
    slots = []
    for requirement in requirements:
        applies = not (requirement.conditional_on_performance_rating and not performance_rating_applicable)
        required_now = applies and (
            requirement.is_required or requirement.conditional_on_performance_rating
        )
        parts = parts_by_key.get(requirement.code, [])
        evidence = current_documents.get(requirement.code)
        slots.append(
            {
                "requirement": requirement,
                "code": requirement.code,
                "applies": applies,
                "required_now": required_now,
                "parts": parts,
                # Drafts saved by the earlier one-file form have a document but no parts.
                "legacy_document": evidence if (evidence is not None and not parts) else None,
                "is_ready": bool(parts) or evidence is not None,
            }
        )
    return slots


def slot_state_payload(slot):
    return {
        "code": slot["code"],
        "is_ready": slot["is_ready"],
        "parts": [
            {
                "id": part.pk,
                "filename": part.original_filename,
                "size_bytes": part.size_bytes,
                "content_type": part.content_type,
            }
            for part in slot["parts"]
        ],
    }


# ── Returned documents (resubmission) ───────────────────────────────────────────────
# The same staging parts, but nothing in the submitted application changes until the
# applicant presses "Resubmit documents": the office keeps seeing the current copy, and the
# staged files are handed to submit_document_resubmission() in one go.


def add_resubmission_part(application, document_key, uploaded_file, allowed_codes):
    if document_key not in allowed_codes:
        raise ValueError(_("This document was not returned to you."))
    return _store_part(application, document_key, uploaded_file)


def remove_resubmission_part(application, part_id):
    part = get_document_parts(application).filter(pk=part_id).first()
    if part is None:
        raise ValueError(_("That file is no longer part of this application."))
    document_key = part.document_key
    part.delete()
    return document_key


def resubmission_slot_states(application, requirements):
    """Card state for each returned document (same shape as build_document_slot_states)."""
    parts_by_key = {}
    for part in get_document_parts(application):
        parts_by_key.setdefault(part.document_key, []).append(part)
    return [
        {
            "requirement": requirement,
            "code": requirement.code,
            "applies": True,
            "required_now": True,
            "parts": parts_by_key.get(requirement.code, []),
            "legacy_document": None,
            "is_ready": bool(parts_by_key.get(requirement.code)),
        }
        for requirement in requirements
    ]


def staged_resubmission_uploads(application, codes):
    """{code: [uploaded file, ...]} from the staged parts, in the order they were added."""
    uploads = {}
    for part in get_document_parts(application):
        if part.document_key in codes:
            uploads.setdefault(part.document_key, []).append(
                SimpleUploadedFile(part.original_filename, _decrypt_part(part), content_type=part.content_type)
            )
    return uploads
