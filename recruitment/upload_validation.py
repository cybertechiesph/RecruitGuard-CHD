import hashlib
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES = 5 * 1024 * 1024
GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
VALIDATED_UPLOAD_CACHE_ATTR = "_recruitguard_validated_applicant_document"

# Matches EvidenceVaultItem.original_filename's max_length. A name longer than
# this would otherwise blow up EvidenceVaultItem.full_clean() and 500 the whole
# upload, so we clamp it (keeping the extension) before persisting.
ORIGINAL_FILENAME_MAX_LENGTH = 255


def clamp_original_filename(filename, max_length=ORIGINAL_FILENAME_MAX_LENGTH):
    """Trim an uploaded file's display name to the storage limit, preserving its
    extension. The bytes are kept intact — only the stored label is shortened."""
    name = (filename or "").strip()
    if len(name) <= max_length:
        return name
    suffix = Path(name).suffix
    if len(suffix) >= max_length:
        return name[:max_length]
    return f"{name[: max_length - len(suffix)]}{suffix}"

UPLOAD_ERROR_TOO_LARGE = "This file is too large. Choose a file that is 5 MB or smaller."
UPLOAD_ERROR_EMPTY = "This file is empty. Pick a file that has something in it."
UPLOAD_ERROR_EXTENSION = (
    "This needs to be a PDF, JPG, or PNG file. Upload one of those."
)
UPLOAD_ERROR_SIGNATURE_UNREADABLE = (
    "We couldn't read this as a PDF, JPG, or PNG. Save it again, then upload it."
)
UPLOAD_ERROR_SIGNATURE_MISMATCH = (
    "This file's contents don't match a PDF, JPG, or PNG. Save it again, then upload it."
)
UPLOAD_ERROR_MIME_MISMATCH = (
    "This file format does not match its filename. Save it again as a PDF, JPG, or PNG, then upload it."
)


@dataclass(frozen=True)
class ValidatedApplicantDocumentUpload:
    canonical_content_type: str
    detected_format: str
    extension: str
    raw_bytes: bytes
    sha256_digest: str
    size_bytes: int


FILE_SIGNATURES = {
    "pdf": {
        "canonical_content_type": "application/pdf",
        "content_types": {"application/pdf", "application/x-pdf"},
        "extensions": {".pdf"},
        "signature": b"%PDF-",
    },
    "jpeg": {
        "canonical_content_type": "image/jpeg",
        "content_types": {"image/jpeg", "image/jpg", "image/pjpeg"},
        "extensions": {".jpg", ".jpeg"},
        "signature": b"\xff\xd8\xff",
    },
    "png": {
        "canonical_content_type": "image/png",
        "content_types": {"image/png", "image/x-png"},
        "extensions": {".png"},
        "signature": b"\x89PNG\r\n\x1a\n",
    },
}


def _normalized_content_type(uploaded_file):
    content_type = getattr(uploaded_file, "content_type", "") or ""
    return content_type.split(";", 1)[0].strip().lower()


def _detect_file_format(raw_bytes):
    if raw_bytes.startswith(FILE_SIGNATURES["pdf"]["signature"]):
        return "pdf"
    if raw_bytes.startswith(FILE_SIGNATURES["jpeg"]["signature"]):
        return "jpeg"
    if raw_bytes.startswith(FILE_SIGNATURES["png"]["signature"]):
        return "png"
    return None


def validate_applicant_document_upload(uploaded_file):
    cached_validation = getattr(uploaded_file, VALIDATED_UPLOAD_CACHE_ATTR, None)
    if cached_validation is not None:
        return cached_validation

    file_size = getattr(uploaded_file, "size", None)
    max_bytes = min(
        getattr(settings, "MAX_EVIDENCE_UPLOAD_BYTES", MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES),
        MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES,
    )
    if file_size is not None and file_size > max_bytes:
        raise ValueError(UPLOAD_ERROR_TOO_LARGE)
    if file_size == 0:
        raise ValueError(UPLOAD_ERROR_EMPTY)

    filename = getattr(uploaded_file, "name", "") or ""
    extension = Path(filename).suffix.lower()
    allowed_extensions = {
        allowed_extension
        for signature in FILE_SIGNATURES.values()
        for allowed_extension in signature["extensions"]
    }
    if extension not in allowed_extensions:
        raise ValueError(UPLOAD_ERROR_EXTENSION)

    raw_bytes = uploaded_file.read()
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    if not raw_bytes:
        raise ValueError(UPLOAD_ERROR_EMPTY)
    if len(raw_bytes) > max_bytes:
        raise ValueError(UPLOAD_ERROR_TOO_LARGE)

    detected_format = _detect_file_format(raw_bytes)
    if not detected_format:
        raise ValueError(UPLOAD_ERROR_SIGNATURE_UNREADABLE)

    detected_signature = FILE_SIGNATURES[detected_format]
    if extension not in detected_signature["extensions"]:
        raise ValueError(UPLOAD_ERROR_SIGNATURE_MISMATCH)

    content_type = _normalized_content_type(uploaded_file)
    if (
        content_type not in GENERIC_CONTENT_TYPES
        and content_type not in detected_signature["content_types"]
    ):
        raise ValueError(UPLOAD_ERROR_MIME_MISMATCH)

    validated_upload = ValidatedApplicantDocumentUpload(
        canonical_content_type=detected_signature["canonical_content_type"],
        detected_format=detected_format,
        extension=extension,
        raw_bytes=raw_bytes,
        sha256_digest=hashlib.sha256(raw_bytes).hexdigest(),
        size_bytes=len(raw_bytes),
    )
    setattr(uploaded_file, VALIDATED_UPLOAD_CACHE_ATTR, validated_upload)
    return validated_upload
