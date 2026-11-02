"""Helpers shared by the internal views: form-error messages, safe redirects, autosave
responses and the audit-review throttle.
"""

import logging

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme


logger = logging.getLogger("recruitment.views")
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


# REV-D: "audit log viewed" records one entry per review visit, not one per
# filter change or page load. A visit = the same session touching the same
# scope within this window.
AUDIT_REVIEW_LOG_WINDOW_SECONDS = 30 * 60


def _should_record_audit_review(request, scope_key):
    from datetime import datetime as _datetime

    stamps = request.session.get("audit_review_logged") or {}
    raw = stamps.get(scope_key)
    if raw:
        try:
            last = _datetime.fromisoformat(raw)
        except ValueError:
            last = None
        if last is not None and (timezone.now() - last).total_seconds() < AUDIT_REVIEW_LOG_WINDOW_SECONDS:
            return False
    stamps[scope_key] = timezone.now().isoformat()
    request.session["audit_review_logged"] = stamps
    return True


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


def _safe_internal_redirect(request, target_url, fallback_url):
    if target_url and url_has_allowed_host_and_scheme(
        target_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target_url
    return fallback_url
