import csv
import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import textwrap
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO, StringIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .email_branding import email_branding_context
from .models import (
    ApplicationETERating,
    AssessmentWeightConfig,
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompetencyDefinition,
    CompetencyRatingTemplate,
    CompetencyScore,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    ExamSchedule,
    EvidenceVaultItem,
    FinalDecision,
    FinalSelection,
    format_weight_percentage,
    InternalEmailChangeRequest,
    InternalLoginAttempt,
    InternalMFAChallenge,
    InternalPasswordHistory,
    InterviewRating,
    InterviewSession,
    Notification,
    PositionDocumentRequirement,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningDocumentReview,
    ScreeningRecord,
    VacancyAssessmentWeights,
    WorkflowOverride,
)
from .notification_services import (
    create_in_app_notifications,
    queue_applicant_interview_notice_notification,
    queue_application_returned_to_applicant_notification,
    queue_document_resubmission_request_notification,
    queue_exam_invitation_notification,
    queue_interview_session_scheduled_notifications,
    queue_non_selected_applicant_notification,
    queue_selected_applicant_notification,
    queue_submission_acknowledgment_notification,
)
from .permissions import WORKFLOW_PROCESSOR_ROLES
from .requirements import (
    APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH,
    MIN_REQUIRED_DOCUMENT_CODES,
    PERFORMANCE_RATING,
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from .upload_validation import validate_applicant_document_upload


logger = logging.getLogger(__name__)


class ApplicationOTPDeliveryError(RuntimeError):
    pass


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
PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL = {
    PositionPosting.Level.LEVEL_1: {RecruitmentUser.Role.SECRETARIAT},
    PositionPosting.Level.LEVEL_2: {RecruitmentUser.Role.HRM_CHIEF},
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
MIN_INTERVIEW_OUTPUTS_TO_FINALIZE = 1
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
TOP_FIVE_SELECTION_LIMIT = 5
PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL = {
    PositionPosting.Level.LEVEL_1: {RecruitmentUser.Role.SECRETARIAT},
    PositionPosting.Level.LEVEL_2: {RecruitmentUser.Role.HRM_CHIEF},
}
CAR_LABEL = "Comparative Assessment Report"
PLANTILLA_POOL_NOT_FINAL_MESSAGE = (
    "Plantilla deliberation and CAR generation are available only after the vacancy "
    "is closed or its closing date has passed."
)
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
    "car": "Comparative Assessment",
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


def get_assessment_weight_config():
    """Return the single shared assessment-weight configuration row."""
    return AssessmentWeightConfig.load()


@transaction.atomic
def update_assessment_weights(actor, config_form):
    """Persist the recruitment team's edits to the global assessment weights and
    record a system-level audit entry. The form has already validated that the
    exam sub-weights add up to 100%."""
    config = config_form.save(commit=False)
    before = AssessmentWeightConfig.load()

    def _snapshot(record):
        return {
            "ete_weight": str(record.ete_weight),
            "exam_weight": str(record.exam_weight),
            "interview_weight": str(record.interview_weight),
            "exam_general_weight": str(record.exam_general_weight),
            "exam_technical_weight": str(record.exam_technical_weight),
        }

    previous = _snapshot(before)
    config.updated_by = actor
    config.full_clean()
    config.save()
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.ASSESSMENT_WEIGHTS_UPDATED,
        description="Updated assessment weight configuration.",
        metadata={"previous": previous, **_snapshot(config)},
    )
    return config


def _request_ip_address(request):
    if request is None:
        return None
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _request_user_agent(request):
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:1000]


def normalize_internal_login_username(username):
    return (username or "").strip().lower()


def _internal_login_attempt_key(username, request):
    return normalize_internal_login_username(username), _request_ip_address(request) or ""


def _internal_login_attempt_actor(username_normalized):
    if not username_normalized:
        return None
    return RecruitmentUser.objects.filter(username__iexact=username_normalized).first()


def _security_alert_recipients():
    configured_recipients = [
        email.strip().lower()
        for email in getattr(settings, "INTERNAL_LOGIN_ALERT_EMAILS", [])
        if email.strip()
    ]
    if configured_recipients:
        return sorted(set(configured_recipients))
    return sorted(
        {
            email
            for email in RecruitmentUser.objects.filter(
                is_active=True,
                role=RecruitmentUser.Role.SYSTEM_ADMIN,
            )
            .exclude(email="")
            .values_list("email", flat=True)
        }
    )


def _send_internal_login_lockout_alert(attempt):
    recipients = _security_alert_recipients()
    if not recipients:
        return False

    subject = "RecruitGuard-CHD internal login lockout alert"
    body = (
        "RecruitGuard-CHD detected repeated failed internal login attempts.\n\n"
        f"Username: {attempt.username_normalized}\n"
        f"Source IP: {attempt.ip_address or 'unknown'}\n"
        f"Failed attempts: {attempt.failure_count}\n"
        f"Locked until: {attempt.locked_until:%Y-%m-%d %H:%M:%S %Z}\n\n"
        "Review the audit log and account activity if this was not expected."
    )
    email = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    email.send(fail_silently=False)
    return True


def get_internal_login_lock(username, request=None):
    username_normalized, ip_address = _internal_login_attempt_key(username, request)
    if not username_normalized:
        return None

    attempt = InternalLoginAttempt.objects.filter(
        username_normalized=username_normalized,
        ip_address=ip_address,
    ).first()
    if attempt is None:
        return None

    now = timezone.now()
    if attempt.locked_until and attempt.locked_until > now:
        return attempt

    window_started = attempt.first_failed_at or attempt.created_at
    window_expires_at = window_started + timedelta(
        minutes=settings.INTERNAL_LOGIN_WINDOW_MINUTES
    )
    if attempt.locked_until or window_expires_at <= now:
        attempt.delete()
        return None
    return None


def record_internal_login_locked_attempt(username, request=None):
    attempt = get_internal_login_lock(username, request)
    if attempt is None:
        return None

    actor = _internal_login_attempt_actor(attempt.username_normalized)
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.INTERNAL_LOGIN_LOCKED,
        description="Blocked internal login attempt during lockout window.",
        metadata={
            "username": attempt.username_normalized,
            "ip_address": attempt.ip_address,
            "failure_count": attempt.failure_count,
            "locked_until": attempt.locked_until.isoformat(),
            "user_agent": _request_user_agent(request),
            "reason": "active_lockout",
        },
    )
    return attempt


@transaction.atomic
def record_internal_login_failure(username, request=None):
    username_display = (username or "").strip()
    username_normalized, ip_address = _internal_login_attempt_key(username, request)
    if not username_normalized:
        return None

    now = timezone.now()
    attempt, _created = InternalLoginAttempt.objects.select_for_update().get_or_create(
        username_normalized=username_normalized,
        ip_address=ip_address,
        defaults={
            "username": username_display[:150],
            "user_agent": _request_user_agent(request),
            "first_failed_at": now,
        },
    )

    if attempt.locked_until and attempt.locked_until > now:
        return attempt

    window_started = attempt.first_failed_at or attempt.created_at
    window_expires_at = window_started + timedelta(
        minutes=settings.INTERNAL_LOGIN_WINDOW_MINUTES
    )
    if attempt.locked_until or window_expires_at <= now:
        attempt.failure_count = 0
        attempt.first_failed_at = now
        attempt.locked_until = None

    attempt.username = username_display[:150]
    attempt.user_agent = _request_user_agent(request)
    attempt.failure_count += 1
    attempt.last_failed_at = now
    if attempt.failure_count >= settings.INTERNAL_LOGIN_MAX_ATTEMPTS:
        attempt.locked_until = now + timedelta(
            minutes=settings.INTERNAL_LOGIN_LOCKOUT_MINUTES
        )
    attempt.save(
        update_fields=[
            "username",
            "user_agent",
            "failure_count",
            "first_failed_at",
            "last_failed_at",
            "locked_until",
            "updated_at",
        ]
    )

    actor = _internal_login_attempt_actor(username_normalized)
    metadata = {
        "username": username_normalized,
        "ip_address": ip_address,
        "failure_count": attempt.failure_count,
        "user_agent": attempt.user_agent,
    }
    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.INTERNAL_LOGIN_FAILED,
        description="Internal login failed.",
        metadata=metadata,
    )
    if attempt.locked_until:
        record_system_audit_event(
            actor=actor,
            action=AuditLog.Action.INTERNAL_LOGIN_LOCKED,
            description="Internal login locked after too many failed attempts.",
            metadata={
                **metadata,
                "locked_until": attempt.locked_until.isoformat(),
            },
        )
        try:
            alert_sent = _send_internal_login_lockout_alert(attempt)
        except Exception as exc:
            record_system_audit_event(
                actor=actor,
                action=AuditLog.Action.INTERNAL_LOGIN_ALERT_FAILED,
                description="Internal login lockout alert could not be sent.",
                metadata={
                    **metadata,
                    "locked_until": attempt.locked_until.isoformat(),
                    "error": str(exc),
                },
            )
        else:
            if alert_sent:
                record_system_audit_event(
                    actor=actor,
                    action=AuditLog.Action.INTERNAL_LOGIN_ALERT_SENT,
                    description="Internal login lockout alert sent.",
                    metadata={
                        **metadata,
                        "locked_until": attempt.locked_until.isoformat(),
                    },
                )
    return attempt


def clear_internal_login_failures(username, request=None):
    username_normalized, ip_address = _internal_login_attempt_key(username, request)
    if not username_normalized:
        return 0
    deleted_count, _ = InternalLoginAttempt.objects.filter(
        username_normalized=username_normalized,
        ip_address=ip_address,
    ).delete()
    return deleted_count


def remember_internal_password_hash(user, password_hash=None):
    password_hash = password_hash or user.password
    if not getattr(user, "is_internal_user", False) or not password_hash:
        return None
    if InternalPasswordHistory.objects.filter(
        user=user,
        password_hash=password_hash,
    ).exists():
        return None

    history = InternalPasswordHistory.objects.create(
        user=user,
        password_hash=password_hash,
    )
    stale_ids = list(
        InternalPasswordHistory.objects.filter(user=user)
        .order_by("-created_at")
        .values_list("id", flat=True)[settings.PASSWORD_HISTORY_LIMIT :]
    )
    if stale_ids:
        InternalPasswordHistory.objects.filter(id__in=stale_ids).delete()
    return history


def normalize_internal_password_reset_email(email):
    return (email or "").strip().lower()


def _password_reset_cache_key(kind, value):
    digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()
    return f"recruitguard:internal-password-reset:{kind}:{digest}"


def _increment_cache_counter(cache_key, timeout_seconds):
    if timeout_seconds <= 0:
        return 0
    try:
        if cache.add(cache_key, 1, timeout=timeout_seconds):
            return 1
        return cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=timeout_seconds)
        return 1


def reserve_internal_password_reset_request(email, request=None):
    normalized_email = normalize_internal_password_reset_email(email)
    if not normalized_email:
        return False

    ip_address = _request_ip_address(request) or "unknown"
    allowed = True

    email_cooldown_seconds = int(
        getattr(settings, "PASSWORD_RESET_EMAIL_COOLDOWN_SECONDS", 300)
    )
    if email_cooldown_seconds > 0:
        cooldown_key = _password_reset_cache_key("email-cooldown", normalized_email)
        if cache.get(cooldown_key):
            allowed = False

    email_window_seconds = int(
        getattr(settings, "PASSWORD_RESET_EMAIL_WINDOW_SECONDS", 3600)
    )
    email_limit = int(getattr(settings, "PASSWORD_RESET_EMAIL_MAX_PER_WINDOW", 3))
    if email_limit > 0 and email_window_seconds > 0:
        email_counter_key = _password_reset_cache_key("email-window", normalized_email)
        email_count = _increment_cache_counter(email_counter_key, email_window_seconds)
        if email_count > email_limit:
            allowed = False

    ip_window_seconds = int(getattr(settings, "PASSWORD_RESET_IP_WINDOW_SECONDS", 3600))
    ip_limit = int(getattr(settings, "PASSWORD_RESET_IP_MAX_PER_WINDOW", 10))
    if ip_limit > 0 and ip_window_seconds > 0:
        ip_counter_key = _password_reset_cache_key("ip-window", ip_address)
        ip_count = _increment_cache_counter(ip_counter_key, ip_window_seconds)
        if ip_count > ip_limit:
            allowed = False

    if allowed and email_cooldown_seconds > 0:
        cache.set(cooldown_key, True, timeout=email_cooldown_seconds)
    return allowed


def _send_internal_email_change_verification(change_request, request=None):
    verification_url = reverse(
        "internal-email-change-verify",
        kwargs={"token": change_request.verification_token},
    )
    if request is not None:
        verification_url = request.build_absolute_uri(verification_url)

    subject = "Confirm your RecruitGuard-CHD internal email address"
    body = (
        "RecruitGuard-CHD received a request to use this email address for an internal account.\n\n"
        f"Internal username: {change_request.user.username}\n"
        f"Requested email: {change_request.new_email}\n\n"
        "Confirm the change using this link:\n"
        f"{verification_url}\n\n"
        f"This link expires in {settings.INTERNAL_EMAIL_CHANGE_TOKEN_VALIDITY_HOURS} hours. "
        "If you did not expect this request, contact the System Administrator."
    )
    email = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[change_request.new_email],
    )
    email.send(fail_silently=False)


@transaction.atomic
def issue_internal_email_change_request(user, requested_by, new_email, request=None):
    new_email = (new_email or "").strip().lower()
    if not getattr(user, "is_internal_user", False):
        raise ValueError("Email changes are only supported for internal users.")
    if not getattr(requested_by, "is_internal_user", False):
        raise ValueError("Only internal users may request internal email changes.")
    if not new_email:
        raise ValueError("A new email address is required.")
    if user.email.lower() == new_email:
        raise ValueError("The requested email address is already assigned to this account.")
    if RecruitmentUser.objects.filter(
        email__iexact=new_email,
        role__in=RecruitmentUser.internal_roles(),
    ).exclude(pk=user.pk).exists():
        raise ValueError("This email address is already assigned to an internal account.")

    now = timezone.now()
    InternalEmailChangeRequest.objects.filter(
        user=user,
        is_used=False,
        verified_at__isnull=True,
    ).update(is_used=True, updated_at=now)
    change_request = InternalEmailChangeRequest(
        user=user,
        requested_by=requested_by,
        old_email=user.email,
        new_email=new_email,
        requested_at=now,
        expires_at=now + timedelta(hours=settings.INTERNAL_EMAIL_CHANGE_TOKEN_VALIDITY_HOURS),
        ip_address=_request_ip_address(request),
        user_agent=_request_user_agent(request),
    )
    change_request.full_clean()
    change_request.save()
    _send_internal_email_change_verification(change_request, request)
    record_system_audit_event(
        actor=requested_by,
        action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_REQUESTED,
        description="Requested internal account email change verification.",
        metadata={
            "target_user_id": user.id,
            "target_username": user.username,
            "old_email": user.email,
            "new_email": new_email,
            "expires_at": change_request.expires_at.isoformat(),
            "ip_address": change_request.ip_address,
            "user_agent": change_request.user_agent,
        },
    )
    return change_request


@transaction.atomic
def verify_internal_email_change_request(token, request=None):
    generic_error = "The email verification link is invalid or expired."
    try:
        change_request = InternalEmailChangeRequest.objects.select_for_update().select_related(
            "user",
            "requested_by",
        ).get(verification_token=token)
    except InternalEmailChangeRequest.DoesNotExist as exc:
        record_system_audit_event(
            actor=None,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed.",
            metadata={
                "reason": "token_not_found",
                "ip_address": _request_ip_address(request),
                "user_agent": _request_user_agent(request),
            },
        )
        raise ValueError(generic_error) from exc

    if change_request.is_used or change_request.verified_at or change_request.is_expired:
        change_request.is_used = True
        change_request.save(update_fields=["is_used", "updated_at"])
        record_system_audit_event(
            actor=change_request.requested_by,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed.",
            metadata={
                "target_user_id": change_request.user_id,
                "target_username": change_request.user.username,
                "reason": "used_or_expired",
                "ip_address": _request_ip_address(request),
                "user_agent": _request_user_agent(request),
            },
        )
        raise ValueError(generic_error)

    if RecruitmentUser.objects.filter(
        email__iexact=change_request.new_email,
        role__in=RecruitmentUser.internal_roles(),
    ).exclude(pk=change_request.user_id).exists():
        change_request.is_used = True
        change_request.save(update_fields=["is_used", "updated_at"])
        record_system_audit_event(
            actor=change_request.requested_by,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed because the email is no longer unique.",
            metadata={
                "target_user_id": change_request.user_id,
                "target_username": change_request.user.username,
                "reason": "email_conflict",
                "ip_address": _request_ip_address(request),
                "user_agent": _request_user_agent(request),
            },
        )
        raise ValueError(generic_error)

    user = change_request.user
    previous_email = user.email
    user.email = change_request.new_email
    user.save(update_fields=["email"])
    change_request.verified_at = timezone.now()
    change_request.is_used = True
    change_request.save(update_fields=["verified_at", "is_used", "updated_at"])
    record_system_audit_event(
        actor=change_request.requested_by,
        action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED,
        description="Verified and applied internal account email change.",
        metadata={
            "target_user_id": user.id,
            "target_username": user.username,
            "old_email": previous_email,
            "new_email": user.email,
            "ip_address": _request_ip_address(request),
            "user_agent": _request_user_agent(request),
        },
    )
    return change_request


def _generate_internal_mfa_code():
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _hash_internal_mfa_otp(challenge, otp_code):
    payload = "|".join(
        [
            str(challenge.user_id),
            str(challenge.challenge_token),
            otp_code,
        ]
    )
    return hmac.new(
        settings.INTERNAL_MFA_OTP_HASH_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _send_internal_mfa_email(challenge, otp_code):
    subject = "RecruitGuard-CHD internal verification code"
    text_body = (
        "RecruitGuard-CHD internal verification code\n\n"
        f"Your verification code is {otp_code}.\n"
        f"It expires in {settings.INTERNAL_MFA_OTP_VALIDITY_MINUTES} minutes.\n\n"
        "Do not share this code. RecruitGuard-CHD staff will never ask for it."
    )
    try:
        html_body = render_to_string(
            "email/internal_mfa_otp.html",
            {
                "challenge": challenge,
                "otp_code": otp_code,
                "otp_validity_minutes": settings.INTERNAL_MFA_OTP_VALIDITY_MINUTES,
                **email_branding_context("internal"),
            },
        )
    except Exception:
        # The HTML body is only a richer alternative to the plain-text message,
        # which already carries the verification code. Degrade to text-only
        # delivery rather than blocking the code email (and rolling back the MFA
        # challenge) on a template failure.
        logger.exception(
            "Failed to render internal MFA HTML email for challenge %s; "
            "falling back to plain-text body.",
            challenge.pk,
        )
        html_body = ""
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[challenge.sent_to_email],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def _latest_internal_mfa_challenge(user):
    return user.mfa_challenges.order_by("-requested_at", "-created_at").first()


def issue_internal_mfa_challenge(user, request=None, *, is_resend=False, enforce_cooldown=False):
    if not getattr(user, "is_active", False) or not getattr(user, "is_internal_user", False):
        raise ValueError("Internal MFA is only available for active internal users.")

    sent_to_email = (user.email or "").strip().lower()
    if not sent_to_email:
        record_system_audit_event(
            actor=user,
            action=AuditLog.Action.INTERNAL_MFA_FAILED,
            description="Internal MFA challenge could not be issued because the account has no email address.",
            metadata={
                "reason": "missing_email",
                "user_id": user.id,
                "user_role": user.role,
            },
        )
        raise ValueError("Your internal account does not have an email address. Contact the System Administrator.")

    if enforce_cooldown:
        latest_challenge = _latest_internal_mfa_challenge(user)
        if latest_challenge is not None:
            elapsed = timezone.now() - latest_challenge.requested_at
            cooldown_seconds = settings.INTERNAL_MFA_RESEND_COOLDOWN_SECONDS
            if elapsed.total_seconds() < cooldown_seconds:
                remaining = max(1, int(cooldown_seconds - elapsed.total_seconds()))
                raise ValueError(f"Please wait {remaining} seconds before requesting another code.")

    with transaction.atomic():
        otp_code = _generate_internal_mfa_code()
        now = timezone.now()
        InternalMFAChallenge.objects.filter(
            user=user,
            is_used=False,
            verified_at__isnull=True,
        ).update(is_used=True, updated_at=now)
        challenge = InternalMFAChallenge(
            user=user,
            sent_to_email=sent_to_email,
            requested_at=now,
            expires_at=now + timedelta(minutes=settings.INTERNAL_MFA_OTP_VALIDITY_MINUTES),
            ip_address=_request_ip_address(request),
            user_agent=_request_user_agent(request),
        )
        challenge.otp_hash = _hash_internal_mfa_otp(challenge, otp_code)
        challenge.full_clean()
        challenge.save()
        _send_internal_mfa_email(challenge, otp_code)

        record_system_audit_event(
            actor=user,
            action=(
                AuditLog.Action.INTERNAL_MFA_RESENT
                if is_resend
                else AuditLog.Action.INTERNAL_MFA_SENT
            ),
            description=(
                "Resent internal MFA verification code."
                if is_resend
                else "Sent internal MFA verification code."
            ),
            metadata={
                "challenge_id": challenge.id,
                "sent_to_email": sent_to_email,
                "expires_at": challenge.expires_at.isoformat(),
                "ip_address": challenge.ip_address,
                "user_agent": challenge.user_agent,
            },
        )
    return challenge


def verify_internal_mfa_challenge(user, challenge_token, otp_code, request=None):
    generic_error = "The verification code is invalid or expired."
    challenge = None
    error_to_raise = None

    with transaction.atomic():
        try:
            challenge = InternalMFAChallenge.objects.select_for_update().get(
                user=user,
                challenge_token=challenge_token,
            )
        except InternalMFAChallenge.DoesNotExist:
            record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_FAILED,
                description="Internal MFA verification failed.",
                metadata={
                    "reason": "challenge_not_found",
                    "ip_address": _request_ip_address(request),
                    "user_agent": _request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and (
            not getattr(user, "is_active", False) or not getattr(user, "is_internal_user", False)
        ):
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None and (
            challenge.is_used or challenge.verified_at
        ):
            record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_FAILED,
                description="Internal MFA verification failed.",
                metadata={
                    "challenge_id": challenge.id,
                    "reason": "challenge_used",
                    "ip_address": _request_ip_address(request),
                    "user_agent": _request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None and challenge.is_expired:
            challenge.is_used = True
            challenge.save(update_fields=["is_used", "updated_at"])
            record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_EXPIRED,
                description="Internal MFA verification code expired before successful verification.",
                metadata={
                    "challenge_id": challenge.id,
                    "expires_at": challenge.expires_at.isoformat(),
                    "ip_address": _request_ip_address(request),
                    "user_agent": _request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None and (
            challenge.attempt_count >= settings.INTERNAL_MFA_MAX_ATTEMPTS
        ):
            challenge.is_used = True
            challenge.save(update_fields=["is_used", "updated_at"])
            record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_LOCKED,
                description="Internal MFA challenge locked after too many failed attempts.",
                metadata={
                    "challenge_id": challenge.id,
                    "attempt_count": challenge.attempt_count,
                    "ip_address": _request_ip_address(request),
                    "user_agent": _request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None:
            expected_hash = _hash_internal_mfa_otp(challenge, otp_code)
            if not hmac.compare_digest(challenge.otp_hash, expected_hash):
                challenge.attempt_count += 1
                update_fields = ["attempt_count", "updated_at"]
                locked = challenge.attempt_count >= settings.INTERNAL_MFA_MAX_ATTEMPTS
                if locked:
                    challenge.is_used = True
                    update_fields.append("is_used")
                challenge.save(update_fields=update_fields)
                record_system_audit_event(
                    actor=user,
                    action=AuditLog.Action.INTERNAL_MFA_FAILED,
                    description="Internal MFA verification failed.",
                    metadata={
                        "challenge_id": challenge.id,
                        "reason": "invalid_code",
                        "attempt_count": challenge.attempt_count,
                        "ip_address": _request_ip_address(request),
                        "user_agent": _request_user_agent(request),
                    },
                )
                if locked:
                    record_system_audit_event(
                        actor=user,
                        action=AuditLog.Action.INTERNAL_MFA_LOCKED,
                        description="Internal MFA challenge locked after too many failed attempts.",
                        metadata={
                            "challenge_id": challenge.id,
                            "attempt_count": challenge.attempt_count,
                            "ip_address": _request_ip_address(request),
                            "user_agent": _request_user_agent(request),
                        },
                    )
                error_to_raise = generic_error
            else:
                challenge.verified_at = timezone.now()
                challenge.is_used = True
                challenge.save(update_fields=["verified_at", "is_used", "updated_at"])
                record_system_audit_event(
                    actor=user,
                    action=AuditLog.Action.INTERNAL_MFA_VERIFIED,
                    description="Internal MFA verification completed successfully.",
                    metadata={
                        "challenge_id": challenge.id,
                        "attempt_count": challenge.attempt_count,
                        "verified_at": challenge.verified_at.isoformat(),
                        "ip_address": _request_ip_address(request),
                        "user_agent": _request_user_agent(request),
                    },
                )

    if error_to_raise is not None:
        raise ValueError(error_to_raise)
    return challenge


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
    assigned_queryset = queryset.filter(current_handler_role=user.role)
    support_queryset = RecruitmentApplication.objects.none()
    interview_support_levels = [
        level
        for level, roles in PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL.items()
        if user.role in roles
    ]
    car_support_levels = [
        level
        for level, roles in PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.items()
        if user.role in roles
    ]
    if interview_support_levels:
        support_queryset = support_queryset | queryset.filter(
            branch=PositionPosting.Branch.PLANTILLA,
            level__in=interview_support_levels,
            status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            case__current_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
            case__is_stage_locked=False,
        ).exclude(
            interview_sessions__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            interview_sessions__is_finalized=True,
        )
    if car_support_levels:
        support_queryset = support_queryset | queryset.filter(
            branch=PositionPosting.Branch.PLANTILLA,
            level__in=car_support_levels,
            status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            case__current_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
            case__is_stage_locked=False,
            interview_sessions__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            interview_sessions__is_finalized=True,
        ).exclude(
            position__comparative_assessment_reports__review_stage=RecruitmentCase.Stage.HRMPSB_REVIEW,
            position__comparative_assessment_reports__is_finalized=True,
        )
    queryset = (assigned_queryset | support_queryset).distinct()
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
    if user_can_support_plantilla_interview(user, application):
        return True
    if user_can_prepare_plantilla_car(user, application):
        return True
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


def user_can_support_plantilla_interview(user, application):
    case = getattr(application, "case", None)
    if not case or case.is_stage_locked:
        return False
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
        or case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or get_current_workflow_section(application) != "interview"
    ):
        return False
    return user.role in PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL.get(application.level, set())


def user_is_interview_rating_support_encoder(user, application):
    return user_can_support_plantilla_interview(user, application)


def user_can_prepare_plantilla_car(user, application):
    case = getattr(application, "case", None)
    if not case or case.is_stage_locked:
        return False
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
        or case.current_stage != CAR_REVIEW_STAGE
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or get_current_workflow_section(application) != "car"
    ):
        return False
    return user.role in PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.get(application.level, set())


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


def _handoff_target_status(application):
    return application.status


def _is_secretariat_hrm_handoff_role(role):
    return role in {
        RecruitmentUser.Role.SECRETARIAT,
        RecruitmentUser.Role.HRM_CHIEF,
    }


def get_case_handoff_options(application, user):
    if not hasattr(application, "case"):
        return []
    case = application.case
    if case.is_stage_locked or case.current_stage == RecruitmentCase.Stage.CLOSED:
        return []
    if not _is_secretariat_hrm_handoff_role(user.role):
        return []
    if application.current_handler_role != user.role or case.current_handler_role != user.role:
        return []
    if not user_can_process_application(user, application):
        return []

    if user.role == RecruitmentUser.Role.SECRETARIAT:
        return [(RecruitmentUser.Role.HRM_CHIEF, "Send to HRM Chief")]
    if user.role == RecruitmentUser.Role.HRM_CHIEF:
        return [(RecruitmentUser.Role.SECRETARIAT, "Send to Secretariat")]
    return []


def _case_handoff_description(actor_role, target_role, application):
    if (
        actor_role == RecruitmentUser.Role.HRM_CHIEF
        and target_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        return "Level 2 case sent to Secretariat by HRM Chief."
    if target_role == RecruitmentUser.Role.SECRETARIAT:
        return "Case sent to Secretariat."
    return "Case sent to HRM Chief."


@transaction.atomic
def route_case_between_secretariat_and_hrm_chief(application, actor, target_role, remarks):
    if target_role not in {
        RecruitmentUser.Role.SECRETARIAT,
        RecruitmentUser.Role.HRM_CHIEF,
    }:
        raise ValueError("Cases can only be sent between Secretariat and HRM Chief.")
    if actor.role == target_role:
        raise ValueError("Select the other office.")
    if not _is_secretariat_hrm_handoff_role(actor.role):
        raise ValueError("Only Secretariat and HRM Chief can send cases to each other.")
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before it can be sent.")
    case = application.case
    if case.is_stage_locked or case.current_stage == RecruitmentCase.Stage.CLOSED:
        raise ValueError("Final or closed cases cannot be sent.")
    if application.current_handler_role != actor.role or case.current_handler_role != actor.role:
        raise ValueError("Only the assigned office can send this case.")
    if not user_can_process_application(actor, application):
        if (
            actor.role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat."
            )
        raise ValueError("This case cannot be sent at the current step.")

    active_override = application.active_secretariat_override
    override = None
    is_level2_secretariat_handoff = (
        application.level == PositionPosting.Level.LEVEL_2
        and actor.role == RecruitmentUser.Role.HRM_CHIEF
        and target_role == RecruitmentUser.Role.SECRETARIAT
    )
    is_level2_return_to_hrm_chief = (
        application.level == PositionPosting.Level.LEVEL_2
        and actor.role == RecruitmentUser.Role.SECRETARIAT
        and target_role == RecruitmentUser.Role.HRM_CHIEF
    )

    if application.level == PositionPosting.Level.LEVEL_2 and target_role == RecruitmentUser.Role.SECRETARIAT:
        if actor.role != RecruitmentUser.Role.HRM_CHIEF:
            raise ValueError("Only HRM Chief can send a Level 2 case to Secretariat.")
        if case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
            raise ValueError(
                "A Level 2 case can be sent to Secretariat only while it is assigned to HRM Chief review."
            )
        application.overrides.filter(is_active=True).update(
            is_active=False,
            revoked_at=timezone.now(),
        )
        override = WorkflowOverride.objects.create(
            application=application,
            granted_by=actor,
            target_role=RecruitmentUser.Role.SECRETARIAT,
            reason=remarks,
        )
    elif is_level2_return_to_hrm_chief:
        if not active_override:
            raise ValueError("This Level 2 case can return to HRM Chief only after HRM Chief first sent it to Secretariat.")

    previous_role = application.current_handler_role
    previous_status = application.status
    previous_stage = case.current_stage
    previous_case_status = case.case_status

    case.current_handler_role = target_role
    case.case_status = RecruitmentCase.CaseStatus.ACTIVE
    case.save(update_fields=["current_handler_role", "case_status", "updated_at"])

    application.current_handler_role = target_role
    application.status = _handoff_target_status(application)
    application.save(update_fields=["current_handler_role", "status", "updated_at"])

    description = _case_handoff_description(actor.role, target_role, application)
    if override:
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.OVERRIDE_GRANTED,
            description="HRM Chief handed off a Level 2 case to Secretariat.",
            metadata={
                "override_id": override.id,
                "reason": remarks,
                **_case_timeline_metadata(case),
            },
        )

    if is_level2_return_to_hrm_chief and active_override:
        active_override.mark_used()
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.OVERRIDE_USED,
            description="Level 2 case returned from Secretariat to HRM Chief.",
            metadata={"override_id": active_override.id},
        )

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=description,
        metadata={
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": application.status,
            "from_role": previous_role,
            "to_role": target_role,
            "from_stage": previous_stage,
            "to_stage": case.current_stage,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
            "case_locked": case.is_stage_locked,
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=(
            RoutingHistory.RouteType.OVERRIDE
            if is_level2_secretariat_handoff
            else RoutingHistory.RouteType.FORWARD
        ),
        description=description,
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=target_role,
        from_status=previous_status,
        to_status=application.status,
        from_stage=previous_stage,
        to_stage=case.current_stage,
        notes=remarks,
        is_override=is_level2_secretariat_handoff,
    )
    _emit_case_assignment_notification(
        application,
        actor,
        target_role,
        kind=(
            Notification.Kind.CASE_RETURNED
            if is_level2_return_to_hrm_chief
            else Notification.Kind.CASE_ASSIGNED
        ),
        title=f"{application.reference_label} handed off to you by {_actor_display_name(actor)}",
        body=(remarks or "").strip(),
    )
    return application


def _consume_active_secretariat_handoff(application, actor, description):
    if application.level != PositionPosting.Level.LEVEL_2:
        return
    override = application.active_secretariat_override
    if not override:
        return
    override.mark_used()
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.OVERRIDE_USED,
        description=description,
        metadata={"override_id": override.id},
    )


def _auto_route_application(application, actor, *, next_role, next_status, description, remarks):
    if not hasattr(application, "case"):
        return False
    if (
        application.current_handler_role == next_role
        and application.status == next_status
        and application.case.current_handler_role == next_role
    ):
        return False

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
    application.closed_at = None
    application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=description,
        metadata={
            "auto_routed": True,
            "remarks": remarks,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
        },
    )
    record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.FORWARD,
        description=description,
        recruitment_case=application.case,
        from_handler_role=previous_role,
        to_handler_role=next_role,
        from_status=previous_status,
        to_status=next_status,
        from_stage=case_transition["previous_stage"],
        to_stage=application.case.current_stage,
        notes=remarks,
    )
    _emit_case_assignment_notification(
        application,
        actor,
        next_role,
        title=f"{application.reference_label} assigned to you",
        body=description,
    )
    if previous_role == RecruitmentUser.Role.SECRETARIAT:
        _consume_active_secretariat_handoff(
            application,
            actor,
            "Level 2 Secretariat authorization ended after automatic case advancement.",
        )
    return True


def _auto_advance_after_exam_finalized(application, actor, review_stage):
    if application.branch == PositionPosting.Branch.PLANTILLA and review_stage in SCREENING_STAGES:
        if (
            review_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
            and application.current_handler_role == RecruitmentUser.Role.SECRETARIAT
        ):
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically returned to HRM Chief after Plantilla examination was finalized by Secretariat.",
                remarks="Plantilla examination finalized by Secretariat; next assigned office is HRM Chief.",
            )
        return _auto_route_application(
            application,
            actor,
            next_role=RecruitmentUser.Role.HRMPSB_MEMBER,
            next_status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            description="Automatically routed to HRMPSB after examination finalization.",
            remarks="Examination finalized; next designated handler is HRMPSB.",
        )
    if application.branch == PositionPosting.Branch.COS:
        if review_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically routed to HRM Chief after COS examination finalization.",
                remarks="COS examination finalized; next designated handler is HRM Chief.",
            )
        if (
            review_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
            and application.current_handler_role == RecruitmentUser.Role.SECRETARIAT
        ):
            return _auto_route_application(
                application,
                actor,
                next_role=RecruitmentUser.Role.HRM_CHIEF,
                next_status=RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                description="Automatically returned to HRM Chief after COS examination was finalized by Secretariat.",
                remarks="COS examination finalized by Secretariat; next assigned office is HRM Chief.",
            )
    return False


def _auto_advance_after_car_finalized(report, actor):
    if not report.is_finalized:
        return []
    routed_applications = []
    report_items = report.items.select_related(
        "recruitment_case",
        "recruitment_case__application",
    )
    for item in report_items:
        application = item.recruitment_case.application
        if (
            application.branch != PositionPosting.Branch.PLANTILLA
            or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
            or application.case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
            or application.current_handler_role != RecruitmentUser.Role.HRMPSB_MEMBER
        ):
            continue
        routed = _auto_route_application(
            application,
            actor,
            next_role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
            next_status=RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
            description="Automatically routed to Appointing Authority after CAR finalization.",
            remarks=f"{CAR_LABEL} finalized; next designated handler is Appointing Authority.",
        )
        if routed:
            routed_applications.append(application)
    return routed_applications


def _auto_advance_boundary_is_ready(application, current_stage=None, current_section=None):
    current_stage = current_stage or get_current_review_stage(application)
    current_section = current_section or get_current_workflow_section(application)
    if current_section != "actions":
        return False
    if current_stage in SCREENING_STAGES:
        if not exam_is_finalized_for_current_stage(application):
            return False
        if application.branch == PositionPosting.Branch.PLANTILLA:
            return True
        if application.branch == PositionPosting.Branch.COS:
            return current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        report = get_comparative_assessment_report(application, stage=current_stage)
        return bool(report and report.is_finalized)
    return False


@transaction.atomic
def repair_auto_advance_workflow_boundaries(actor=None):
    repaired = []
    applications = RecruitmentApplication.objects.select_related(
        "applicant",
        "position",
        "case",
    ).filter(
        case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
        case__is_stage_locked=False,
    ).order_by("id")

    processed_reports = set()
    for application in applications:
        if not hasattr(application, "case"):
            continue
        current_stage = application.case.current_stage
        current_section = get_current_workflow_section(application)
        if not _auto_advance_boundary_is_ready(application, current_stage, current_section):
            continue

        before_role = application.current_handler_role
        before_status = application.status
        before_stage = application.case.current_stage
        if current_stage in SCREENING_STAGES:
            routed = _auto_advance_after_exam_finalized(application, actor, current_stage)
            if routed:
                repaired.append(
                    {
                        "application_id": application.id,
                        "reference": application.reference_label,
                        "from_role": before_role,
                        "to_role": application.current_handler_role,
                        "from_status": before_status,
                        "to_status": application.status,
                        "from_stage": before_stage,
                        "to_stage": application.case.current_stage,
                        "reason": "exam_finalized_boundary",
                    }
                )
            continue

        report = get_comparative_assessment_report(application, stage=current_stage)
        if not report or not report.is_finalized or report.id in processed_reports:
            continue
        processed_reports.add(report.id)
        routed_applications = _auto_advance_after_car_finalized(report, actor)
        for routed_application in routed_applications:
            repaired.append(
                {
                    "application_id": routed_application.id,
                    "reference": routed_application.reference_label,
                    "from_role": RecruitmentUser.Role.HRMPSB_MEMBER,
                    "to_role": routed_application.current_handler_role,
                    "from_status": RecruitmentApplication.Status.HRMPSB_REVIEW,
                    "to_status": routed_application.status,
                    "from_stage": RecruitmentCase.Stage.HRMPSB_REVIEW,
                    "to_stage": routed_application.case.current_stage,
                    "reason": "car_finalized_boundary",
                }
            )
    return repaired


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
        "stage_entered_at": case.stage_entered_at.isoformat() if case.stage_entered_at else "",
        "case_status": case.case_status,
        "case_handler_role": case.current_handler_role,
        "case_locked": case.is_stage_locked,
    }


def _ensure_case_stage_alignment(application, case):
    expected_stage = _review_stage_from_application_status(application.status)
    if expected_stage and case.current_stage != expected_stage:
        raise ValueError("The recruitment case is out of sync with the application workflow state.")


def _transition_case_stage(case, new_stage, *, force=False):
    if not new_stage:
        return False
    if force or case.current_stage != new_stage:
        case.current_stage = new_stage
        case.stage_entered_at = timezone.now()
        return True
    return False


def _application_detail_url(application, tab=None):
    url = reverse("application-detail", kwargs={"pk": application.pk})
    if tab:
        return f"{url}?tab={tab}"
    return url


def _notification_tab_for_application(application):
    section_key = get_current_workflow_section(application)
    if section_key == "overview":
        return "screening"
    return section_key


def _active_users_with_application_access(application, *, role=None, exclude_user_ids=None):
    exclude_user_ids = set(exclude_user_ids or [])
    queryset = RecruitmentUser.objects.filter(is_active=True).exclude(
        role=RecruitmentUser.Role.APPLICANT,
    )
    if role:
        queryset = queryset.filter(role=role)
    queryset = queryset.order_by("last_name", "first_name", "username")
    return [
        user
        for user in queryset
        if user.id not in exclude_user_ids and user_can_view_application(user, application)
    ]


def _notify_application_users(
    application,
    recipients,
    *,
    kind,
    title,
    body="",
    tab=None,
):
    return create_in_app_notifications(
        recipients,
        kind=kind,
        title=title,
        body=body,
        related_url=_application_detail_url(
            application,
            tab=tab or _notification_tab_for_application(application),
        ),
        application=application,
    )


def _notify_role_for_application(
    application,
    role,
    *,
    kind,
    title,
    body="",
    tab=None,
    exclude_user_ids=None,
):
    return _notify_application_users(
        application,
        _active_users_with_application_access(
            application,
            role=role,
            exclude_user_ids=exclude_user_ids,
        ),
        kind=kind,
        title=title,
        body=body,
        tab=tab,
    )


def _actor_display_name(actor):
    if not actor:
        return "the office"
    return actor.get_full_name() or actor.username or actor.get_role_display()


def _emit_case_assignment_notification(
    application,
    actor,
    target_role,
    *,
    kind=Notification.Kind.CASE_ASSIGNED,
    title=None,
    body="",
    tab=None,
):
    if not target_role or target_role == RecruitmentUser.Role.APPLICANT:
        return []
    title = title or f"{application.reference_label} handed off to you by {_actor_display_name(actor)}"
    return _notify_role_for_application(
        application,
        target_role,
        kind=kind,
        title=title,
        body=body,
        tab=tab,
    )


def _emit_screening_finalized_notification(application, actor, review_stage):
    return _notify_role_for_application(
        application,
        application.current_handler_role,
        kind=Notification.Kind.SCREENING_FINALIZED,
        title=f"{application.reference_label} screening finalized",
        body=f"{_actor_display_name(actor)} finalized the screening review.",
        tab="screening",
        exclude_user_ids={actor.id} if actor else None,
    )


def _emit_resubmission_received_notification(
    application,
    actor,
    target_role,
    *,
    document_count=0,
):
    document_word = "document" if document_count == 1 else "documents"
    return _emit_case_assignment_notification(
        application,
        actor,
        target_role,
        kind=Notification.Kind.RESUBMISSION_RECEIVED,
        title=f"{application.reference_label} applicant resubmitted {document_count} {document_word}",
        body="The returned application is ready for review.",
        tab="screening",
    )


def _emit_interview_scheduled_in_app_notifications(application, interview_session, recipients):
    scheduled_for = timezone.localtime(interview_session.scheduled_for).strftime("%B %d, %Y")
    return _notify_application_users(
        application,
        [recipient for recipient in recipients if user_can_view_application(recipient, application)],
        kind=Notification.Kind.INTERVIEW_SCHEDULED,
        title=f"Interview scheduled for {application.reference_label} on {scheduled_for}",
        body=f"Location / medium: {interview_session.location}",
        tab="interview",
    )


def _emit_interview_finalized_in_app_notifications(application, recipients):
    return _notify_application_users(
        application,
        [recipient for recipient in recipients if user_can_view_application(recipient, application)],
        kind=Notification.Kind.INTERVIEW_FINALIZED,
        title=f"{application.reference_label} interview session finalized",
        body="No further interview ratings can be submitted.",
        tab="interview",
    )


def emit_deadline_approaching_notifications(now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    closing_cutoff = today + timedelta(days=1)
    applications = RecruitmentApplication.objects.select_related(
        "position",
        "case",
    ).filter(
        position__closing_date__gte=today,
        position__closing_date__lte=closing_cutoff,
        case__case_status=RecruitmentCase.CaseStatus.ACTIVE,
        case__is_stage_locked=False,
    ).exclude(
        current_handler_role__in=["", RecruitmentUser.Role.APPLICANT],
    )

    emitted = []
    for application in applications:
        case = application.case
        recipients = _active_users_with_application_access(
            application,
            role=case.current_handler_role,
        )
        if not recipients:
            continue
        title = f"{application.reference_label} is closing in 24 hours"
        body = f"Posting closes on {application.position.closing_date:%Y-%m-%d}."
        for recipient in recipients:
            already_sent_today = Notification.objects.filter(
                application=application,
                recipient=recipient,
                kind=Notification.Kind.DEADLINE_APPROACHING,
                created_at__date=today,
            ).exists()
            if already_sent_today:
                continue
            emitted.extend(
                _notify_application_users(
                    application,
                    [recipient],
                    kind=Notification.Kind.DEADLINE_APPROACHING,
                    title=title,
                    body=body,
                    tab="screening",
                )
            )
    return emitted


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
            AuditLog.Action.CAR_RETURNED,
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
    ).prefetch_related(
        "document_reviews__evidence_item",
    ).filter(review_stage=review_stage).first()


def get_screening_records(application):
    return application.screening_records.select_related(
        "reviewed_by",
        "finalized_by",
    ).prefetch_related(
        "document_reviews__evidence_item",
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
        return ["interview", "car", "actions"]
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
        return deliberation_is_finalized_for_current_stage(application)
    if section_key == "car":
        return car_is_finalized_for_current_stage(application)
    if section_key == "actions":
        return False
    return True


def application_requires_finalized_applicant_pool(application):
    return (
        application.branch == PositionPosting.Branch.PLANTILLA
        and get_current_review_stage(application) == RecruitmentCase.Stage.HRMPSB_REVIEW
    )


def application_has_finalized_applicant_pool(application):
    if not application_requires_finalized_applicant_pool(application):
        return True
    return application.position.applicant_pool_is_finalized


def get_applicant_pool_finalization_block_message(application):
    closing_date = getattr(application.position, "closing_date", None)
    if closing_date:
        return f"{PLANTILLA_POOL_NOT_FINAL_MESSAGE} Current closing date: {closing_date:%Y-%m-%d}."
    return f"{PLANTILLA_POOL_NOT_FINAL_MESSAGE} Close the recruitment entry before starting HRMPSB deliberation."


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
    if section_key == "car":
        if (
            application_requires_finalized_applicant_pool(application)
            and not application_has_finalized_applicant_pool(application)
        ):
            return get_applicant_pool_finalization_block_message(application)
        return "Finalize the Comparative Assessment Report before recommending this Plantilla application."
    if section_key == "deliberation":
        return "Finalize the deliberation record before endorsing this COS application."
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


def get_exam_schedule(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.exam_schedules.select_related(
        "scheduled_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


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
        ).prefetch_related(
            "ratings__rated_by",
            "ratings__encoded_by",
        ).order_by("created_at")
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
        "encoded_by",
        "interview_session",
    ).filter(review_stage=review_stage).order_by("created_at")


def get_interview_rating_for_user(application, user, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.interview_ratings.select_related(
        "interview_session",
        "encoded_by",
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
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return user_can_support_plantilla_interview(user, application)
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
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and user_can_support_plantilla_interview(user, application)
    ):
        return True
    if (
        user.role not in INTERVIEW_RATING_ROLES_BY_STAGE.get(current_stage, set())
        or get_current_workflow_section(application) != "interview"
    ):
        return False
    return user_can_process_application(user, application)


def user_can_upload_interview_fallback(user, application):
    current_stage = get_current_review_stage(application)
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return user_can_support_plantilla_interview(user, application)
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


def get_interview_schedule_notification_recipients(application):
    return [
        user
        for user in RecruitmentUser.objects.filter(
            is_active=True,
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        ).order_by("last_name", "first_name", "username")
        if user_can_manage_interview_rating(user, application)
    ]


def _interview_scheduled_for_is_past(scheduled_for):
    return scheduled_for < timezone.now() - timedelta(minutes=5)


def _interview_schedule_change_requires_notification(
    *,
    created,
    previous_scheduled_for,
    previous_location,
    new_scheduled_for,
    new_location,
):
    return (
        created
        or previous_scheduled_for != new_scheduled_for
        or (previous_location or "").strip() != (new_location or "").strip()
    )


def _decimal_string(value):
    if value is None:
        return ""
    return str(value)


def _optional_decimal(value):
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _quantize_score(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _calculate_preliminary_assessment_score(posting, document_review_score, exam_score, interview_score):
    # The CAR component weights are now per-vacancy (VacancyAssessmentWeights), resolved from
    # the posting; a vacancy without a row falls back to the office defaults (40/20/40).
    if any(value in (None, "") for value in (document_review_score, exam_score, interview_score)):
        return None
    weights = posting.assessment_weights_or_default
    total = (
        (Decimal(str(document_review_score)) * weights.ete_fraction)
        + (Decimal(str(exam_score)) * weights.exam_component_fraction)
        + (Decimal(str(interview_score)) * weights.interview_fraction)
    )
    return _quantize_score(total)


def _assessment_weight_display(posting):
    weights = posting.assessment_weights_or_default
    return (
        f"Document review {format_weight_percentage(weights.ete_weight)}%, "
        f"exam {format_weight_percentage(weights.exam_weight)}%, "
        f"interview {format_weight_percentage(weights.interview_weight)}%."
    )


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
        "education_score": _decimal_string(screening_record.education_score),
        "training_score": _decimal_string(screening_record.training_score),
        "experience_score": _decimal_string(screening_record.experience_score),
        "document_review_score": _decimal_string(screening_record.document_review_score),
        "document_review_weight_display": screening_record.document_review_weight_display,
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
        "general_score": _decimal_string(exam_record.general_score),
        "general_result": exam_record.general_result,
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
    latest_document_review_score = latest_screening.document_review_score if latest_screening else None
    latest_exam_score = latest_exam.effective_score if latest_exam else None
    preliminary_assessment_score = None
    assessment_weight_display = ""
    car_draft = None
    if application.branch == PositionPosting.Branch.PLANTILLA:
        car_draft = get_latest_draft_comparative_assessment_report(
            application,
            stage=get_current_review_stage(application),
        )
        preliminary_assessment_score = _calculate_preliminary_assessment_score(
            application.position,
            latest_document_review_score,
            latest_exam_score,
            latest_interview_average,
        )
        assessment_weight_display = _assessment_weight_display(application.position)
    return {
        "application_reference": application.reference_number or "",
        "entry_code": application.position.job_code,
        "branch": application.branch,
        "level": application.level,
        "generated_at": timezone.now().isoformat(),
        "assessment_weight_display": assessment_weight_display,
        "car_draft": (
            {
                "id": car_draft.id,
                "version_number": car_draft.version_number,
                "prepared_by": str(car_draft.generated_by) if car_draft.generated_by else "",
                "prepared_by_role": car_draft.generated_by_role,
                "candidate_count": car_draft.items.count(),
                "summary_notes": car_draft.summary_notes,
            }
            if car_draft
            else {}
        ),
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
            "latest_document_review_score": _decimal_string(latest_document_review_score),
            "latest_exam_status": latest_exam.exam_status if latest_exam else "",
            "latest_exam_score": _decimal_string(latest_exam_score),
            "latest_exam_components": latest_exam.component_summary if latest_exam else "",
            "latest_interview_average": _decimal_string(latest_interview_average),
            "preliminary_assessment_score": _decimal_string(preliminary_assessment_score),
        },
    }


def get_deliberation_record(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
    ).filter(review_stage=review_stage).first()


def get_deliberation_records(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
    ).order_by("created_at")


def get_latest_finalized_deliberation_record(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
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


def get_comparative_assessment_report(application, stage=None, include_returned=False):
    review_stage = stage or get_current_review_stage(application)
    queryset = ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
    )
    if not include_returned:
        queryset = queryset.filter(is_returned=False)
    return queryset.order_by(
        "-version_number",
        "-is_finalized",
        "-finalized_at",
        "-created_at",
    ).first()


def get_latest_draft_comparative_assessment_report(application, stage=None):
    review_stage = stage or get_current_review_stage(application)
    return ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
        is_finalized=False,
        is_returned=False,
    ).order_by(
        "-version_number",
        "-created_at",
    ).first()


def get_latest_finalized_comparative_assessment_report(application):
    return ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        is_finalized=True,
        is_returned=False,
    ).order_by("-version_number", "-finalized_at", "-created_at").first()


def get_final_selection_for_entry(recruitment_entry):
    return (
        FinalSelection.objects.select_related(
            "comparative_assessment_report",
            "selected_item",
            "selected_item__recruitment_case",
            "selected_item__recruitment_case__application",
            "selected_application",
            "selected_case",
            "decided_by",
            "recruitment_entry",
        )
        .filter(recruitment_entry=recruitment_entry)
        .order_by("-decided_at", "-created_at")
        .first()
    )


def get_final_selection_for_application(application):
    return get_final_selection_for_entry(application.position)


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
    if get_current_workflow_section(application) != "car":
        return False
    return user_can_prepare_plantilla_car(user, application)


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
    if application.branch == PositionPosting.Branch.PLANTILLA:
        return False
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


def user_can_record_final_selection(user, application):
    if application.branch != PositionPosting.Branch.PLANTILLA:
        return False
    current_stage = get_current_review_stage(application)
    if (
        current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        or get_current_workflow_section(application) != "decision"
    ):
        return False
    if user.role != RecruitmentUser.Role.APPOINTING_AUTHORITY:
        return False
    if get_final_selection_for_application(application):
        return False
    report = get_latest_finalized_comparative_assessment_report(application)
    if not report:
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
        "education_score": _decimal_string(record.education_score),
        "training_score": _decimal_string(record.training_score),
        "experience_score": _decimal_string(record.experience_score),
        "document_review_score": _decimal_string(record.document_review_score),
        "document_reviews": [
            {
                "document_key": review.document_key,
                "requirement_title": review.requirement_title,
                "status": review.status,
                "status_label": review.get_status_display(),
                "remarks": review.remarks,
                "is_required": review.is_required,
                "evidence_item_id": review.evidence_item_id,
            }
            for review in record.document_reviews.all()
        ],
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
        "general_score": _decimal_string(record.general_score),
        "general_result": record.general_result,
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
        "comparative_assessment_report_id": record.comparative_assessment_report_id or "",
        "deliberated_at": record.deliberated_at.isoformat(),
        "recommendation": record.recommendation,
        "decision_support_summary": record.decision_support_summary,
        "quorum_status": record.quorum_status,
        "quorum_status_label": record.get_quorum_status_display(),
        "attendance_notes": record.attendance_notes,
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
        "latest_document_review_score": record.consolidated_snapshot.get("summary", {}).get(
            "latest_document_review_score",
            "",
        ),
        "preliminary_assessment_score": record.consolidated_snapshot.get("summary", {}).get(
            "preliminary_assessment_score",
            "",
        ),
        "assessment_weight_display": record.consolidated_snapshot.get("assessment_weight_display", ""),
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
        "document_review_score": _decimal_string(item.document_review_score),
        "exam_status": item.exam_status,
        "exam_score": _decimal_string(item.exam_score),
        "interview_average_score": _decimal_string(item.interview_average_score),
        "assessment_score": _decimal_string(item.assessment_score),
        "preliminary_rank_order": item.preliminary_rank_order,
        "recommendation": item.recommendation,
        "decision_support_summary": item.decision_support_summary,
        "ranking_notes": item.ranking_notes,
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
        "prepared_by": str(report.generated_by) if report.generated_by else "",
        "prepared_by_role": report.generated_by_role,
        "summary_notes": report.summary_notes,
        "version_number": report.version_number,
        "candidate_count": len(items),
        "assessment_weight_display": report.consolidated_snapshot.get("assessment_weight_display", ""),
        "finalized_at": report.finalized_at.isoformat() if report.finalized_at else "",
        "is_returned": report.is_returned,
        "returned_at": report.returned_at.isoformat() if report.returned_at else "",
        "returned_by": str(report.returned_by) if report.returned_by else "",
        "returned_by_role": report.returned_by_role,
        "return_reason": report.return_reason,
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


def build_car_selection_packet(report):
    return {
        "context": {
            "built_at": timezone.now().isoformat(),
            "recruitment_entry_id": report.recruitment_entry_id,
            "recruitment_entry_code": report.recruitment_entry.job_code,
            "recruitment_entry_title": report.recruitment_entry.title,
            "branch": report.recruitment_entry.branch,
            "branch_label": report.recruitment_entry.get_branch_display(),
            "level": report.recruitment_entry.level,
            "level_label": report.recruitment_entry.get_level_display(),
            "review_stage": report.review_stage,
            "review_stage_label": report.get_review_stage_display(),
        },
        "comparative_assessment_report": _decision_packet_car_report(report),
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
    if not screening_records:
        missing_components.append("Finalized screening record")
    if not exam_records:
        missing_components.append("Finalized examination record")
    if not interview_sessions:
        missing_components.append("Finalized interview session")
    if application.branch == PositionPosting.Branch.COS and not deliberation_record:
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


SCREENING_COMPLETENESS_BLOCKING_DOCUMENT_STATUSES = {
    ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED,
    ScreeningDocumentReview.ReviewStatus.NEEDS_REVIEW,
    ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION,
    ScreeningDocumentReview.ReviewStatus.ABSENT,
}


def _screening_document_review_rows(
    application,
    screening_record=None,
    document_reviews=None,
    *,
    default_submitted_status=ScreeningDocumentReview.ReviewStatus.MEETS,
):
    current_documents = get_current_applicant_document_map(application)
    requirements = get_applicant_document_requirements(application)
    provided_by_key = {
        (row.get("document_key") or ""): row
        for row in (document_reviews or [])
        if row.get("document_key")
    }
    existing_by_key = {}
    if screening_record and screening_record.pk:
        existing_by_key = {
            review.document_key: review
            for review in screening_record.document_reviews.select_related("evidence_item")
        }

    rows = []
    for display_order, requirement in enumerate(requirements, start=1):
        evidence = current_documents.get(requirement.code)
        provided = provided_by_key.get(requirement.code, {})
        existing_review = existing_by_key.get(requirement.code)
        is_not_applicable = (
            requirement.conditional_on_performance_rating
            and application.performance_rating_not_applicable
            and evidence is None
        )
        is_required = requirement.is_required or (
            requirement.conditional_on_performance_rating
            and not application.performance_rating_not_applicable
        )

        if is_not_applicable:
            status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
        elif evidence is None and is_required:
            status = ScreeningDocumentReview.ReviewStatus.ABSENT
        elif evidence is None:
            status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
        elif provided.get("status"):
            status = provided["status"]
        elif existing_review is not None:
            status = existing_review.status
        else:
            status = default_submitted_status
        remarks = (provided.get("remarks") or "").strip()
        if not remarks and existing_review is not None:
            remarks = existing_review.remarks

        rows.append(
            {
                "document_key": requirement.code,
                "requirement_title": requirement.title,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
                "status": status,
                "remarks": remarks,
                "is_required": is_required,
                "is_not_applicable": is_not_applicable,
                "evidence_item": evidence,
                "display_order": display_order,
            }
        )
    return rows


def _validate_screening_review_consistency(
    *,
    completeness_status,
    completeness_notes,
    qualification_outcome,
    screening_notes,
    document_reviews,
):
    if (
        completeness_status == ScreeningRecord.CompletenessStatus.INCOMPLETE
        and not (completeness_notes or "").strip()
    ):
        raise ValueError("Record completeness observations before marking this application incomplete.")
    if (
        completeness_status == ScreeningRecord.CompletenessStatus.INCOMPLETE
        and qualification_outcome == ScreeningRecord.QualificationOutcome.QUALIFIED
    ):
        raise ValueError("Applicants with incomplete documents cannot be marked qualified.")
    if (
        qualification_outcome == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
        and not (screening_notes or "").strip()
    ):
        raise ValueError("Record screening notes before marking this applicant not qualified.")

    blocking_reviews = [
        row
        for row in document_reviews
        if row["is_required"]
        and row["status"] in SCREENING_COMPLETENESS_BLOCKING_DOCUMENT_STATUSES
    ]
    if (
        completeness_status == ScreeningRecord.CompletenessStatus.COMPLETE
        and blocking_reviews
    ):
        blocking_labels = "; ".join(row["requirement_title"] for row in blocking_reviews)
        raise ValueError(
            "Required documents must be marked Meets before using Complete: "
            f"{blocking_labels}."
        )
    for row in document_reviews:
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
            and row["is_required"]
            and not row["is_not_applicable"]
        ):
            raise ValueError(f"{row['requirement_title']} cannot be marked not applicable.")
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.MEETS
            and row["evidence_item"] is None
        ):
            raise ValueError(
                f"{row['requirement_title']} cannot meet the requirement without an uploaded file."
            )
        if (
            row["status"] == ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
            and not (row.get("remarks") or "").strip()
        ):
            raise ValueError(
                f"Record resubmission instructions for {row['requirement_title']}."
            )


def _sync_screening_document_reviews(screening_record, document_reviews):
    active_keys = []
    for row in document_reviews:
        active_keys.append(row["document_key"])
        review, _created = ScreeningDocumentReview.objects.update_or_create(
            screening_record=screening_record,
            document_key=row["document_key"],
            defaults={
                "evidence_item": row["evidence_item"],
                "requirement_title": row["requirement_title"],
                "requirement_label": row["requirement_label"],
                "status": row["status"],
                "remarks": row["remarks"],
                "is_required": row["is_required"],
                "is_not_applicable": row["is_not_applicable"],
                "display_order": row["display_order"],
            },
        )
        review.full_clean()
        review.save()
    screening_record.document_reviews.exclude(document_key__in=active_keys).delete()


def _document_review_status_counts(document_reviews):
    counts = {}
    for row in document_reviews:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


@transaction.atomic
def save_screening_review(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before screening can be recorded.")
    review_stage = application.case.current_stage
    screening_record = get_screening_record(application, stage=review_stage)
    if screening_record and screening_record.is_finalized:
        raise ValueError("Finalized screening records cannot be edited.")
    if not user_can_manage_screening(actor, application):
        raise ValueError("This case is not currently assigned to you for screening.")
    if review_stage not in SCREENING_STAGES:
        raise ValueError("Screening can only be edited while the case is assigned to Secretariat or HRM Chief review.")

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

    document_reviews = _screening_document_review_rows(
        application,
        screening_record,
        cleaned_data.get("document_reviews"),
    )
    _validate_screening_review_consistency(
        completeness_status=cleaned_data["completeness_status"],
        completeness_notes=cleaned_data["completeness_notes"],
        qualification_outcome=cleaned_data["qualification_outcome"],
        screening_notes=cleaned_data["screening_notes"],
        document_reviews=document_reviews,
    )

    screening_record.recruitment_case = application.case
    screening_record.reviewed_by = actor
    screening_record.completeness_status = cleaned_data["completeness_status"]
    screening_record.completeness_notes = cleaned_data["completeness_notes"]
    screening_record.qualification_outcome = cleaned_data["qualification_outcome"]
    screening_record.education_score = _optional_decimal(cleaned_data.get("education_score"))
    screening_record.training_score = _optional_decimal(cleaned_data.get("training_score"))
    screening_record.experience_score = _optional_decimal(cleaned_data.get("experience_score"))
    screening_record.document_review_score = _optional_decimal(cleaned_data.get("document_review_score"))
    screening_record.screening_notes = cleaned_data["screening_notes"]
    screening_record.is_finalized = False
    screening_record.finalized_by = None
    screening_record.finalized_at = None
    screening_record.full_clean()
    screening_record.save()
    _sync_screening_document_reviews(screening_record, document_reviews)
    if finalize:
        screening_record.is_finalized = True
        screening_record.finalized_by = actor
        screening_record.finalized_at = timezone.now()
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
            "education_score": _decimal_string(screening_record.education_score),
            "training_score": _decimal_string(screening_record.training_score),
            "experience_score": _decimal_string(screening_record.experience_score),
            "document_review_score": _decimal_string(screening_record.document_review_score),
            "document_review_status_counts": _document_review_status_counts(document_reviews),
            "is_finalized": screening_record.is_finalized,
        },
    )
    if finalize:
        _emit_screening_finalized_notification(application, actor, review_stage)
        # Coupling (Gap B): a *definitive* Not-Qualified screening cut rejects the
        # applicant and notifies them in the same action, so a screened-out applicant
        # can never be silently stranded. A cut is "definitive" only when the documents
        # are complete and nothing is flagged for resubmission — i.e. a true QS failure
        # with nothing left to fix. When documents are incomplete or a resubmission was
        # requested, the handler keeps the reject-or-return choice (both of which notify
        # the applicant), so we do not auto-reject. The rejection notice uses
        # screening-specific wording because the reject fires from a screening stage.
        is_definitive_screening_cut = (
            screening_record.qualification_outcome
            == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
            and screening_record.completeness_status
            == ScreeningRecord.CompletenessStatus.COMPLETE
            and not screening_record.document_reviews.filter(
                status=ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
            ).exists()
        )
        if is_definitive_screening_cut:
            process_workflow_action(
                application,
                actor,
                "reject",
                screening_record.screening_notes
                or "Did not meet the Qualification Standards at screening.",
            )
    return screening_record


@transaction.atomic
def save_exam_schedule(application, actor, cleaned_data, record_audit=True):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before the exam can be scheduled.")
    review_stage = application.case.current_stage
    if review_stage not in EXAM_STAGES:
        raise ValueError(
            "Exam scheduling is available only while the case is assigned to Secretariat or HRM Chief review."
        )
    if not user_can_manage_exam(actor, application):
        raise ValueError("This case is not currently assigned to you for exam scheduling.")

    exam_schedule = get_exam_schedule(application, stage=review_stage)
    created = exam_schedule is None
    previous_scheduled_for = exam_schedule.scheduled_for if exam_schedule else None
    previous_venue = exam_schedule.venue if exam_schedule else ""
    if exam_schedule is None:
        exam_schedule = ExamSchedule(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            scheduled_by=actor,
            branch=application.branch,
            level=application.level,
        )

    scheduled_for = cleaned_data["scheduled_for"]
    if _interview_scheduled_for_is_past(scheduled_for):
        existing_past_schedule_unchanged = (
            not created
            and previous_scheduled_for == scheduled_for
            and _interview_scheduled_for_is_past(previous_scheduled_for)
        )
        if not existing_past_schedule_unchanged:
            raise ValueError("The exam can't be scheduled in the past.")

    notice_delivery = (
        cleaned_data.get("notice_delivery") or ExamSchedule.NoticeDelivery.SYSTEM_EMAIL
    )
    exam_schedule.recruitment_case = application.case
    exam_schedule.recruitment_entry = application.position
    exam_schedule.scheduled_by = actor
    exam_schedule.scheduled_for = scheduled_for
    exam_schedule.venue = cleaned_data["venue"]
    exam_schedule.instructions = cleaned_data.get("instructions", "")
    exam_schedule.notice_delivery = notice_delivery

    should_notify = _interview_schedule_change_requires_notification(
        created=created,
        previous_scheduled_for=previous_scheduled_for,
        previous_location=previous_venue,
        new_scheduled_for=exam_schedule.scheduled_for,
        new_location=exam_schedule.venue,
    )
    if should_notify:
        # Saving a new or rescheduled exam invitation is the applicant touchpoint:
        # record that the notice was issued (emailed, or hand-delivered for
        # applicants with no email on file).
        exam_schedule.applicant_notified_at = timezone.now()

    exam_schedule.full_clean()
    exam_schedule.save()

    if should_notify and notice_delivery == ExamSchedule.NoticeDelivery.SYSTEM_EMAIL:
        queue_exam_invitation_notification(application, exam_schedule, actor=actor)

    if record_audit:
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.EXAM_SCHEDULED,
            description=(
                "Saved examination schedule and notified the applicant."
                if should_notify
                else "Saved examination schedule."
            ),
            metadata={
                "exam_schedule_id": exam_schedule.id,
                "created": created,
                "review_stage": review_stage,
                "scheduled_for": exam_schedule.scheduled_for.isoformat(),
                "venue": exam_schedule.venue,
                "notice_delivery": exam_schedule.notice_delivery,
                "applicant_notified": should_notify,
            },
        )
    return exam_schedule


@transaction.atomic
def save_exam_record(
    application,
    actor,
    cleaned_data,
    finalize=False,
    evidence_file=None,
    allow_partial=False,
    record_audit=True,
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before exam details can be recorded.")
    review_stage = application.case.current_stage
    exam_record = get_exam_record(application, stage=review_stage)
    if exam_record and exam_record.is_finalized:
        raise ValueError("Finalized exam records cannot be edited.")
    if not user_can_manage_exam(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for exam details."
        )
    if review_stage not in EXAM_STAGES:
        raise ValueError("Exam details can only be edited while the case is assigned to Secretariat or HRM Chief review.")
    if finalize:
        exam_schedule = get_exam_schedule(application, stage=review_stage)
        if exam_schedule is None or not exam_schedule.applicant_was_notified:
            raise ValueError(
                "Schedule the examination and notify the applicant before recording final results."
            )

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

    allow_partial = bool(allow_partial and not finalize)
    exam_status = cleaned_data.get("exam_status", "") or ""

    exam_record.recruitment_case = application.case
    exam_record.recorded_by = actor
    exam_record.exam_type = cleaned_data.get("exam_type", "") or ""
    exam_record.exam_status = exam_status
    # The overall exam score is computed from the components in
    # ExamRecord.apply_policy_outputs(); it is never taken from form input.
    exam_record.exam_result = cleaned_data.get("exam_result", "")
    exam_record.technical_score = _optional_decimal(cleaned_data.get("technical_score"))
    exam_record.technical_result = cleaned_data.get("technical_result", "")
    exam_record.general_score = _optional_decimal(cleaned_data.get("general_score"))
    exam_record.general_result = cleaned_data.get("general_result", "")
    exam_record.exam_date = cleaned_data.get("exam_date")
    exam_record.administered_by = cleaned_data.get("administered_by", "")
    exam_record.valid_from = cleaned_data.get("valid_from")
    exam_record.valid_until = cleaned_data.get("valid_until")
    exam_record.exam_notes = cleaned_data.get("exam_notes", "")
    if exam_status in {ExamRecord.ExamStatus.WAIVED, ExamRecord.ExamStatus.ABSENT}:
        exam_record.exam_score = None
        exam_record.technical_score = None
        exam_record.general_score = None
        exam_record.valid_from = None
        exam_record.valid_until = None
    exam_record.is_finalized = finalize
    if finalize:
        exam_record.finalized_by = actor
        exam_record.finalized_at = timezone.now()
    else:
        exam_record.finalized_by = None
        exam_record.finalized_at = None
    exam_record.apply_policy_outputs()
    if allow_partial:
        exam_record.validate_unique()
    else:
        exam_record.full_clean()
    exam_record.save()

    evidence = None
    if evidence_file:
        evidence = upload_evidence_item(
            application=application,
            actor=actor,
            label=f"Exam Evidence - {exam_record.get_exam_type_display()}",
            uploaded_file=evidence_file,
            document_key=f"exam-record-{review_stage}",
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type=ARTIFACT_TYPE_EXAM_EVIDENCE,
        )
        exam_record.evidence_item = evidence
        if allow_partial:
            exam_record.validate_unique()
        else:
            exam_record.full_clean()
        exam_record.save(update_fields=["evidence_item", "updated_at"])

    if record_audit:
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
                "general_score": _decimal_string(exam_record.general_score),
                "general_result": exam_record.general_result,
                "exam_date": exam_record.exam_date.isoformat() if exam_record.exam_date else "",
                "administered_by": exam_record.administered_by,
                "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
                "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
                "evidence_id": evidence.id if evidence else exam_record.evidence_item_id,
                "evidence_uploaded": bool(evidence),
                "is_finalized": exam_record.is_finalized,
            },
        )
    if finalize:
        _auto_advance_after_exam_finalized(application, actor, review_stage)
    return exam_record


@transaction.atomic
def save_interview_session(
    application, actor, cleaned_data, finalize=False, notify_applicant=False, notify_panel=False
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before interview scheduling can be recorded.")
    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot be edited.")
    if not user_can_manage_interview_session(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for interview scheduling."
        )
    if review_stage not in INTERVIEW_SESSION_STAGES:
        raise ValueError(
            "Interview scheduling is available only while the case is assigned to Secretariat, HRM Chief, or HRMPSB review."
        )

    created = interview_session is None
    previous_scheduled_for = interview_session.scheduled_for if interview_session else None
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

    scheduled_for = cleaned_data["scheduled_for"]
    if _interview_scheduled_for_is_past(scheduled_for):
        existing_past_schedule_unchanged = (
            not created
            and previous_scheduled_for == scheduled_for
            and _interview_scheduled_for_is_past(previous_scheduled_for)
        )
        if not existing_past_schedule_unchanged:
            raise ValueError("The interview can't be scheduled in the past.")

    interview_session.recruitment_case = application.case
    interview_session.recruitment_entry = application.position
    interview_session.scheduled_by = actor
    interview_session.scheduled_for = scheduled_for
    interview_session.location = cleaned_data["location"]
    interview_session.session_notes = cleaned_data["session_notes"]

    existing_rating_count = interview_session.ratings.count() if interview_session.pk else 0
    fallback_count = get_interview_fallback_evidence(application, stage=review_stage).count()
    if (
        finalize
        and existing_rating_count + fallback_count < MIN_INTERVIEW_OUTPUTS_TO_FINALIZE
    ):
        raise ValueError(
            "Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session."
        )
    schedule_notification_recipients = (
        get_interview_schedule_notification_recipients(application)
        if notify_panel
        else []
    )
    unsubmitted_panel_recipients = []
    if finalize:
        submitted_panel_ids = set(
            interview_session.ratings.values_list("rated_by_id", flat=True)
        )
        unsubmitted_panel_recipients = [
            recipient
            for recipient in get_interview_schedule_notification_recipients(application)
            if recipient.id not in submitted_panel_ids
        ]

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
            "scheduled_by_role": actor.role,
            "support_action": user_can_support_plantilla_interview(actor, application),
            "scheduled_for": interview_session.scheduled_for.isoformat(),
            "location": interview_session.location,
            "rating_count": existing_rating_count,
            "fallback_count": fallback_count,
            "is_finalized": interview_session.is_finalized,
        },
    )
    # Notifications are sent only on the explicit "Notify" operations, never on a
    # plain save. Each notify button saves the current schedule first, then sends.
    if notify_panel:
        queue_interview_session_scheduled_notifications(
            application,
            interview_session,
            schedule_notification_recipients,
            actor=actor,
        )
        _emit_interview_scheduled_in_app_notifications(
            application,
            interview_session,
            schedule_notification_recipients,
        )
    if notify_applicant:
        queue_applicant_interview_notice_notification(
            application,
            interview_session,
            actor=actor,
        )
    if finalize and unsubmitted_panel_recipients:
        _emit_interview_finalized_in_app_notifications(
            application,
            unsubmitted_panel_recipients,
        )
    return interview_session


@transaction.atomic
def get_published_competency_rating_template(entry):
    """Return the rating sheet the HRMPSB panel scores on — only once it has been
    made available (published) or scoring has started (locked). A draft sheet is
    not yet visible to raters."""
    return CompetencyRatingTemplate.objects.filter(
        recruitment_entry=entry,
        status__in=[
            CompetencyRatingTemplate.Status.PUBLISHED,
            CompetencyRatingTemplate.Status.LOCKED,
        ],
    ).first()


def lock_competency_rating_template(template):
    """Lock the sheet once the first score is in, so its competencies/weights can no
    longer be re-shaped under ratings that already exist."""
    if template.status != CompetencyRatingTemplate.Status.LOCKED:
        template.status = CompetencyRatingTemplate.Status.LOCKED
        if not template.locked_at:
            template.locked_at = timezone.now()
        template.save(update_fields=["status", "locked_at", "updated_at"])


def _coerce_competency_scores(template, raw_scores):
    """Validate that every competency on the sheet has an in-range score and return
    an ordered ``{competency: int}`` mapping. ``raw_scores`` may be keyed by either
    the competency instance or its id."""
    by_id = {}
    for key, value in (raw_scores or {}).items():
        competency_id = getattr(key, "pk", key)
        by_id[competency_id] = value

    scores = {}
    for competency in template.competencies.all():
        if competency.id not in by_id or by_id[competency.id] in (None, ""):
            raise ValueError(f"Score every competency before saving (missing: {competency.name}).")
        try:
            score = int(by_id[competency.id])
        except (TypeError, ValueError):
            raise ValueError(f"Enter a whole-number score for {competency.name}.")
        if score < template.scale_min or score > template.scale_max:
            raise ValueError(
                f"Scores must be between {template.scale_min} and {template.scale_max} "
                f"(check {competency.name})."
            )
        scores[competency] = score
    return scores


def compute_competency_rating_score(template, scores_by_competency):
    """Normalize the raw per-competency scores to the 0-100 the CAR consumes.

    Each competency is scored out of the scale maximum (score / scale_max), so a
    top mark is 100% and a mark of 1 on a 1-4 scale is 25%. Competencies are then
    combined by their relative weights. Returns a Decimal quantized to 0.01.
    """
    total_weight = Decimal("0")
    weighted_total = Decimal("0")
    scale_max = Decimal(str(template.scale_max))
    for competency, score in scores_by_competency.items():
        weight = competency.weight or Decimal("0")
        component_percent = (Decimal(str(score)) / scale_max) * Decimal("100")
        weighted_total += weight * component_percent
        total_weight += weight
    if total_weight <= 0:
        return Decimal("0.00")
    return (weighted_total / total_weight).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def save_interview_rating(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before interview ratings can be recorded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot accept rating changes.")
    if not user_can_manage_interview_rating(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for interview rating."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before recording interview ratings.")

    template = get_published_competency_rating_template(application.position)
    if template is None:
        raise ValueError(
            "Publish the interview rating sheet for this vacancy before recording interview ratings."
        )

    competency_scores = cleaned_data.get("competency_scores") or {}
    scores_by_competency = _coerce_competency_scores(template, competency_scores)
    rating_value = compute_competency_rating_score(template, scores_by_competency)

    rated_by = cleaned_data.get("rated_by") or actor
    interview_rating = interview_session.ratings.filter(rated_by=rated_by).first()
    created = interview_rating is None
    if interview_rating is None:
        interview_rating = InterviewRating(
            interview_session=interview_session,
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            rated_by=rated_by,
            encoded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_rating.rated_by = rated_by
    interview_rating.encoded_by = actor
    interview_rating.rating_score = rating_value
    interview_rating.rating_notes = cleaned_data.get("rating_notes", "")
    interview_rating.justification = cleaned_data.get("justification", "")
    interview_rating.full_clean()
    interview_rating.save()

    # Persist the per-competency scores (replace any prior set on revision).
    interview_rating.competency_scores.all().delete()
    CompetencyScore.objects.bulk_create(
        [
            CompetencyScore(
                interview_rating=interview_rating,
                competency=competency,
                score=score,
            )
            for competency, score in scores_by_competency.items()
        ]
    )

    # First submitted score locks the template so it can no longer be re-shaped.
    lock_competency_rating_template(template)

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
            "rated_by_id": rated_by.id,
            "rated_by_role": rated_by.role,
            "encoded_by_id": actor.id,
            "encoded_by_role": actor.role,
            "encoded_on_behalf": actor.id != rated_by.id,
            "rating_score": str(interview_rating.rating_score),
            "competency_scores": {
                competency.name: score
                for competency, score in scores_by_competency.items()
            },
            "has_justification": bool(interview_rating.justification),
        },
    )
    return interview_rating


# Standard CSC competencies seeded into every new interview rating sheet. The
# Technical group is left for the Secretariat to fill in per position.
STANDARD_INTERVIEW_COMPETENCIES = (
    (CompetencyDefinition.Group.CORE, "Exemplifying Integrity"),
    (CompetencyDefinition.Group.CORE, "Professionalism"),
    (CompetencyDefinition.Group.CORE, "Service Excellence"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Effective Communication"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Effective Interpersonal Relations"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Organizational Awareness and Commitment"),
)


def get_competency_rating_template(entry):
    return CompetencyRatingTemplate.objects.filter(recruitment_entry=entry).first()


@transaction.atomic
def create_competency_rating_template(entry, actor, scale_min=1, scale_max=4):
    """Create the per-vacancy interview rating sheet, seeded with the standard Core
    and Organizational competencies. The Secretariat then adds the Technical ones."""
    if CompetencyRatingTemplate.objects.filter(recruitment_entry=entry).exists():
        raise ValueError("This vacancy already has an interview rating sheet.")
    template = CompetencyRatingTemplate(
        recruitment_entry=entry,
        created_by=actor,
        scale_min=scale_min,
        scale_max=scale_max,
    )
    template.full_clean()
    template.save()
    CompetencyDefinition.objects.bulk_create(
        [
            CompetencyDefinition(
                template=template,
                group=group,
                name=name,
                weight=Decimal("1.00"),
                order=order,
            )
            for order, (group, name) in enumerate(STANDARD_INTERVIEW_COMPETENCIES, start=1)
        ]
    )
    return template


@transaction.atomic
def save_competency_rating_sheet(template, template_form, formset, publish=False):
    """Persist the Secretariat's edits to the rating sheet: scale + instructions plus
    the competency rows (named rows are saved, blank/removed rows are dropped). When
    ``publish`` is set the sheet is opened to the HRMPSB panel."""
    if template.is_locked:
        raise ValueError("This rating sheet is locked because scoring has started.")

    template = template_form.save(commit=False)
    order = 0
    for form in formset.forms:
        cleaned = getattr(form, "cleaned_data", None) or {}
        name = (cleaned.get("name") or "").strip()
        if cleaned.get("DELETE") or not name:
            if form.instance.pk:
                form.instance.delete()
            continue
        order += 1
        competency = form.instance
        competency.template = template
        competency.group = cleaned["group"]
        competency.name = name
        competency.weight = cleaned["weight"]
        competency.order = order
        competency.full_clean()
        competency.save()

    if publish:
        template.status = CompetencyRatingTemplate.Status.PUBLISHED
        if not template.published_at:
            template.published_at = timezone.now()
    template.full_clean()
    template.save()
    return template


@transaction.atomic
def upload_interview_fallback_rating(application, actor, uploaded_file, remarks):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before fallback interview ratings can be uploaded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot accept fallback rating uploads.")
    if not user_can_upload_interview_fallback(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for fallback rating upload."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before uploading a fallback rating sheet.")

    validate_applicant_document_upload(uploaded_file)

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
        "comparative_assessment_report",
    ).order_by("ranking_position", "application__reference_number")


@transaction.atomic
def save_deliberation_record(
    application,
    actor,
    cleaned_data,
    finalize=False,
    allow_partial=False,
    record_audit=True,
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before deliberation details can be recorded.")
    if (
        application_requires_finalized_applicant_pool(application)
        and not application_has_finalized_applicant_pool(application)
    ):
        raise ValueError(get_applicant_pool_finalization_block_message(application))
    if not user_can_manage_deliberation(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for deliberation."
        )

    review_stage = application.case.current_stage
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch, "")
    if review_stage != expected_stage:
        raise ValueError(
            "Deliberation is available only during the proper decision-support step for this branch."
        )
    car_draft = None
    if application.branch == PositionPosting.Branch.PLANTILLA:
        car_draft = get_latest_draft_comparative_assessment_report(application, stage=review_stage)
        if not car_draft:
            raise ValueError("Prepare the CAR draft before recording HRMPSB deliberation.")

    deliberation_record = get_deliberation_record(application, stage=review_stage)
    if deliberation_record and deliberation_record.is_finalized:
        raise ValueError("Finalized deliberation records cannot be edited.")

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

    allow_partial = bool(allow_partial and not finalize)
    deliberated_at = cleaned_data.get("deliberated_at")
    if allow_partial and deliberated_at is None:
        deliberated_at = getattr(deliberation_record, "deliberated_at", None) or timezone.now()

    deliberation_record.recruitment_case = application.case
    deliberation_record.recruitment_entry = application.position
    deliberation_record.comparative_assessment_report = car_draft
    deliberation_record.recorded_by = actor
    deliberation_record.deliberated_at = deliberated_at
    deliberation_record.deliberation_minutes = cleaned_data.get("deliberation_minutes") or ""
    deliberation_record.recommendation = cleaned_data.get("recommendation", "") or ""
    deliberation_record.decision_support_summary = cleaned_data.get("decision_support_summary") or ""
    deliberation_record.quorum_status = cleaned_data.get(
        "quorum_status",
        DeliberationRecord.QuorumStatus.NOT_RECORDED,
    ) or DeliberationRecord.QuorumStatus.NOT_RECORDED
    deliberation_record.attendance_notes = cleaned_data.get("attendance_notes", "") or ""
    deliberation_record.ranking_position = cleaned_data.get("ranking_position")
    deliberation_record.ranking_notes = cleaned_data.get("ranking_notes", "") or ""
    deliberation_record.consolidated_snapshot = consolidated_snapshot
    deliberation_record.is_finalized = finalize
    if finalize:
        deliberation_record.finalized_by = actor
        deliberation_record.finalized_at = timezone.now()
    else:
        deliberation_record.finalized_by = None
        deliberation_record.finalized_at = None
    validation_exclude = []
    if allow_partial:
        validation_exclude = [
            "deliberation_minutes",
            "decision_support_summary",
        ]
    deliberation_record.full_clean(exclude=validation_exclude)
    deliberation_record.save()

    if record_audit:
        record_audit_event(
            application=application,
            actor=actor,
            action=(
                AuditLog.Action.DELIBERATION_FINALIZED
                if finalize
                else AuditLog.Action.DELIBERATION_RECORDED
            ),
            description=(
                (
                    "Finalized HRMPSB recommendation endorsement."
                    if application.branch == PositionPosting.Branch.PLANTILLA
                    else "Finalized deliberation and decision-support record."
                )
                if finalize
                else (
                    "Saved HRMPSB deliberation on the CAR draft."
                    if application.branch == PositionPosting.Branch.PLANTILLA
                    else "Saved deliberation and decision-support record."
                )
            ),
            metadata={
                "deliberation_record_id": deliberation_record.id,
                "car_draft_id": car_draft.id if car_draft else "",
                "created": created,
                "review_stage": review_stage,
                "recommendation_recorded": bool(deliberation_record.recommendation),
                "quorum_status": deliberation_record.quorum_status,
                "ranking_position": deliberation_record.ranking_position,
                "finalized_screening_count": len(consolidated_snapshot["screening_records"]),
                "finalized_exam_count": len(consolidated_snapshot["exam_records"]),
                "finalized_interview_count": len(consolidated_snapshot["interview_sessions"]),
                "is_finalized": deliberation_record.is_finalized,
            },
        )
    return deliberation_record


def get_application_ete_rating(application):
    return getattr(application, "ete_rating", None)


def set_application_ete_rating(application, actor, rating):
    """Persist the Secretariat's manual ETE rating for a candidate. The CAR's ETE
    (40%) component uses this value once it is set; until then the CAR falls back to
    the screening score."""
    record = getattr(application, "ete_rating", None)
    if record is None:
        record = ApplicationETERating(application=application)
    record.rating = rating
    record.recorded_by = actor
    record.full_clean()
    record.save()
    return record


@transaction.atomic
def apply_car_ete_ratings(application, actor, posted):
    """Persist per-candidate ETE ratings posted from the CAR draft screen. Inputs are
    named ``ete_<recruitment_case_id>``. Blank or out-of-range values are skipped."""
    if not user_can_manage_comparative_assessment_report(actor, application):
        return
    for key in list(posted.keys()):
        if not key.startswith("ete_"):
            continue
        raw = (posted.get(key) or "").strip()
        if raw == "":
            continue
        try:
            case_id = int(key[len("ete_"):])
            rating = Decimal(raw)
        except (ValueError, ArithmeticError):
            continue
        if rating < 0 or rating > 100:
            continue
        case = (
            RecruitmentCase.objects.select_related("application")
            .filter(pk=case_id, application__position=application.position)
            .first()
        )
        if case:
            set_application_ete_rating(case.application, actor, rating)


def _car_candidate_rows(recruitment_entry, review_stage, required_draft=None):
    # The finalized CAR is computed directly from each active candidate's finalized
    # screening/exam/interview outputs and ranked by the assessment score. There is no
    # separate HRMPSB deliberation/ranking step for Plantilla anymore, so the final
    # rows are the same computed-and-ranked rows as the draft. ``required_draft`` is
    # accepted for call-site compatibility; the draft's existence is enforced by the
    # caller before finalizing.
    return _car_draft_candidate_rows(recruitment_entry, review_stage)


def _active_plantilla_cases_for_entry(recruitment_entry, review_stage):
    return list(
        RecruitmentCase.objects.select_related("application", "application__position")
        .filter(
            application__position=recruitment_entry,
            application__branch=PositionPosting.Branch.PLANTILLA,
            application__status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            current_stage=review_stage,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
            is_stage_locked=False,
        )
        .order_by("application__reference_number", "application__created_at", "id")
    )


def _car_draft_candidate_rows(recruitment_entry, review_stage):
    cases = _active_plantilla_cases_for_entry(recruitment_entry, review_stage)
    if not cases:
        raise ValueError("No active Plantilla candidates are available for CAR draft preparation.")

    rows = []
    incomplete_references = []
    for case in cases:
        candidate_application = case.application
        summary = build_deliberation_consolidation(candidate_application)["summary"]
        has_required_outputs = (
            summary.get("finalized_screening_count", 0) > 0
            and summary.get("finalized_exam_count", 0) > 0
            and summary.get("finalized_interview_count", 0) > 0
        )
        if not has_required_outputs:
            incomplete_references.append(candidate_application.reference_label)
            continue

        document_review_score = summary.get("latest_document_review_score", "")
        exam_score = summary.get("latest_exam_score", "")
        interview_average_score = summary.get("latest_interview_average", "")
        # The CAR's ETE (40%) component uses the Secretariat's manual ETE rating when
        # one has been entered; until then it falls back to the screening score.
        manual_ete = getattr(candidate_application, "ete_rating", None)
        ete_rating = str(manual_ete.rating) if manual_ete else ""
        ete_component = ete_rating or document_review_score
        assessment_score = _calculate_preliminary_assessment_score(
            recruitment_entry,
            ete_component,
            exam_score,
            interview_average_score,
        )
        rows.append(
            {
                "application": candidate_application,
                "recruitment_case": case,
                "deliberation_record": None,
                "rank_order": None,
                "preliminary_rank_order": None,
                "qualification_outcome": summary.get("latest_qualification_outcome", ""),
                "document_review_score": document_review_score,
                "ete_rating": ete_rating,
                "finalized_document_review_count": summary.get("finalized_screening_count", 0),
                "exam_status": summary.get("latest_exam_status", ""),
                "exam_score": exam_score,
                "exam_components": summary.get("latest_exam_components", ""),
                "finalized_exam_count": summary.get("finalized_exam_count", 0),
                "interview_average_score": interview_average_score,
                "finalized_interview_count": summary.get("finalized_interview_count", 0),
                "assessment_score": assessment_score,
                "recommendation": "",
                "decision_support_summary": "",
                "ranking_notes": "",
            }
        )

    if incomplete_references:
        pending_references = "; ".join(incomplete_references)
        raise ValueError(
            "Finalize screening, exam, and interview outputs for all active applicants before preparing the CAR draft. "
            f"Pending: {pending_references}."
        )

    scored_rows = sorted(
        [row for row in rows if row["assessment_score"] is not None],
        key=lambda row: (
            -row["assessment_score"],
            row["application"].reference_number or row["application"].reference_label,
        ),
    )
    for index, row in enumerate(scored_rows, start=1):
        row["preliminary_rank_order"] = index

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row["preliminary_rank_order"] is None,
            row["preliminary_rank_order"] or 0,
            row["application"].reference_number or row["application"].reference_label,
        ),
    )
    for index, row in enumerate(ordered_rows, start=1):
        row["rank_order"] = row["preliminary_rank_order"] or index
    return ordered_rows


def get_comparative_assessment_readiness(application):
    readiness = {
        "is_applicable": False,
        "has_draft": False,
        "can_prepare_draft": False,
        "can_finalize": False,
        "draft_candidate_count": 0,
        "final_candidate_count": 0,
        "prepare_block_message": "",
        "finalize_block_message": "",
        "latest_draft_id": None,
    }
    case = getattr(application, "case", None)
    if (
        not case
        or application.branch != PositionPosting.Branch.PLANTILLA
        or case.current_stage != CAR_REVIEW_STAGE
    ):
        return readiness

    readiness["is_applicable"] = True
    if (
        application_requires_finalized_applicant_pool(application)
        and not application_has_finalized_applicant_pool(application)
    ):
        message = get_applicant_pool_finalization_block_message(application)
        readiness["prepare_block_message"] = message
        readiness["finalize_block_message"] = message
        return readiness

    try:
        draft_rows = _car_draft_candidate_rows(application.position, case.current_stage)
    except ValueError as exc:
        readiness["prepare_block_message"] = str(exc)
    else:
        readiness["can_prepare_draft"] = True
        readiness["draft_candidate_count"] = len(draft_rows)

    latest_draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=case.current_stage,
    )
    readiness["has_draft"] = latest_draft_report is not None
    readiness["latest_draft_id"] = latest_draft_report.id if latest_draft_report else None
    if not latest_draft_report:
        readiness["finalize_block_message"] = (
            "Prepare the CAR draft before finalizing the Comparative Assessment Report."
        )
        return readiness

    try:
        final_rows = _car_candidate_rows(
            application.position,
            case.current_stage,
            required_draft=latest_draft_report,
        )
    except ValueError as exc:
        readiness["finalize_block_message"] = str(exc)
    else:
        readiness["can_finalize"] = True
        readiness["final_candidate_count"] = len(final_rows)
    return readiness


@transaction.atomic
def autosave_comparative_assessment_report_notes(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a Comparative Assessment Report can be autosaved.")
    if (
        application_requires_finalized_applicant_pool(application)
        and not application_has_finalized_applicant_pool(application)
    ):
        raise ValueError(get_applicant_pool_finalization_block_message(application))
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for CAR preparation."
        )
    if application.case.current_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError(
            "The Comparative Assessment Report is available only for Plantilla cases at the HRMPSB step."
        )

    draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=application.case.current_stage,
    )
    if draft_report is None:
        return None

    summary_notes = cleaned_data.get("summary_notes") or ""
    if draft_report.summary_notes != summary_notes:
        draft_report.summary_notes = summary_notes
        draft_report.full_clean()
        draft_report.save(update_fields=["summary_notes", "updated_at"])
    return draft_report


@transaction.atomic
def generate_comparative_assessment_report(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a Comparative Assessment Report can be generated.")
    if (
        application_requires_finalized_applicant_pool(application)
        and not application_has_finalized_applicant_pool(application)
    ):
        raise ValueError(get_applicant_pool_finalization_block_message(application))
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for CAR preparation."
        )

    review_stage = application.case.current_stage
    if review_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError(
            "The Comparative Assessment Report is available only for Plantilla cases at the HRMPSB step."
        )
    if ComparativeAssessmentReport.objects.filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
        is_finalized=True,
        is_returned=False,
    ).exists():
        raise ValueError("A finalized CAR already exists for this vacancy.")

    latest_draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=review_stage,
    )
    if finalize:
        if not latest_draft_report:
            raise ValueError(
                "Prepare the CAR draft before finalizing the Comparative Assessment Report."
            )
        candidate_rows = _car_candidate_rows(
            application.position,
            review_stage,
            required_draft=latest_draft_report,
        )
    else:
        candidate_rows = _car_draft_candidate_rows(application.position, review_stage)
    latest_report = get_comparative_assessment_report(
        application,
        stage=review_stage,
        include_returned=True,
    )
    version_number = (latest_report.version_number + 1) if latest_report else 1
    consolidated_snapshot = {
        "generated_at": timezone.now().isoformat(),
        "prepared_by_role": actor.role,
        "entry_code": application.position.job_code,
        "review_stage": review_stage,
        "assessment_weight_display": _assessment_weight_display(application.position),
        "source_car_draft_id": latest_draft_report.id if finalize and latest_draft_report else "",
        "source_car_draft_version": latest_draft_report.version_number if finalize and latest_draft_report else "",
        "candidate_count": len(candidate_rows),
        "ranked_candidates": [
            {
                "rank_order": row["rank_order"],
                "preliminary_rank_order": row["preliminary_rank_order"],
                "application_reference": row["application"].reference_number or "",
                "applicant_name": row["application"].applicant_display_name,
                "document_review_outcome": row["qualification_outcome"],
                "document_review_score": row["document_review_score"],
                "finalized_document_review_count": row["finalized_document_review_count"],
                "qualification_outcome": row["qualification_outcome"],
                "exam_status": row["exam_status"],
                "exam_score": row["exam_score"],
                "exam_components": row["exam_components"],
                "finalized_exam_count": row["finalized_exam_count"],
                "interview_average_score": row["interview_average_score"],
                "finalized_interview_count": row["finalized_interview_count"],
                "assessment_score": _decimal_string(row["assessment_score"]),
                "recommendation": row["recommendation"],
                "decision_support_summary": row["decision_support_summary"],
                "ranking_notes": row["ranking_notes"],
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
        report.quorum_met = cleaned_data.get("quorum_met")
        report.members_present = cleaned_data.get("members_present")
    report.full_clean()
    report.save()

    for row in candidate_rows:
        item = ComparativeAssessmentReportItem(
            report=report,
            recruitment_case=row["recruitment_case"],
            deliberation_record=row["deliberation_record"],
            rank_order=row["rank_order"],
            preliminary_rank_order=row["preliminary_rank_order"],
            qualification_outcome=row["qualification_outcome"],
            document_review_score=(
                Decimal(row["document_review_score"]) if row["document_review_score"] else None
            ),
            ete_rating=Decimal(row["ete_rating"]) if row["ete_rating"] else None,
            exam_status=row["exam_status"],
            exam_score=Decimal(row["exam_score"]) if row["exam_score"] else None,
            interview_average_score=(
                Decimal(row["interview_average_score"]) if row["interview_average_score"] else None
            ),
            assessment_score=row["assessment_score"],
            recommendation=row["recommendation"],
            decision_support_summary=row["decision_support_summary"],
            ranking_notes=row["ranking_notes"],
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
            else "Prepared or updated the CAR draft for HRMPSB deliberation."
        ),
        metadata={
            "car_report_id": report.id,
            "review_stage": review_stage,
            "version_number": report.version_number,
            "candidate_count": len(candidate_rows),
            "evidence_id": evidence.id,
            "is_finalized": report.is_finalized,
            "prepared_by_role": actor.role,
        },
    )
    if finalize:
        _auto_advance_after_car_finalized(report, actor)
    return report


@transaction.atomic
def save_completion_tracking(application, actor, cleaned_data, requirement_formset):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before completion details can be recorded.")
    if not user_can_manage_completion(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for completion details."
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

    returned_at = None
    case, created = RecruitmentCase.objects.get_or_create(
        application=application,
        defaults={
            "branch": application.branch,
            "current_stage": next_stage,
            "stage_entered_at": timezone.now(),
            "current_handler_role": next_role,
            "case_status": RecruitmentCase.CaseStatus.ACTIVE,
            "is_stage_locked": False,
            "locked_stage": "",
        },
    )
    if not created:
        if case.case_status != RecruitmentCase.CaseStatus.RETURNED_TO_APPLICANT:
            raise ValueError("A recruitment case already exists for this application.")
        returned_at = case.updated_at
        _transition_case_stage(case, next_stage, force=True)
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
                "stage_entered_at",
                "current_handler_role",
                "case_status",
                "is_stage_locked",
                "locked_stage",
                "closed_at",
                "reopened_at",
                "updated_at",
            ]
        )
    if created:
        _emit_case_assignment_notification(
            application,
            actor,
            next_role,
            title=f"{application.reference_label} assigned to you",
            body="A new application is ready for review.",
            tab="screening",
        )
    else:
        document_count = application.evidence_items.filter(
            artifact_type=ARTIFACT_TYPE_APPLICANT_DOCUMENT,
            created_at__gte=returned_at,
        ).count()
        _emit_resubmission_received_notification(
            application,
            actor,
            next_role,
            document_count=document_count,
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
        _transition_case_stage(case, next_stage)
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
            _transition_case_stage(case, RecruitmentCase.Stage.COMPLETION)
            case.case_status = RecruitmentCase.CaseStatus.ACTIVE
            case.current_handler_role = next_role
            case.is_stage_locked = False
            case.locked_stage = ""
            case.closed_at = None
        else:
            _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
            case.case_status = RecruitmentCase.CaseStatus.APPROVED
            case.current_handler_role = ""
            case.is_stage_locked = True
            case.locked_stage = previous_stage
            case.closed_at = timezone.now()
    elif next_status == RecruitmentApplication.Status.REJECTED:
        _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
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
            "stage_entered_at",
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


def get_reusable_public_application_for_applicant(entry, applicant):
    return (
        RecruitmentApplication.objects.select_related("applicant", "position")
        .filter(
            position=entry,
            applicant=applicant,
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
    screening_record = get_screening_record(application)
    document_reviews_by_key = {}
    if screening_record is not None:
        document_reviews_by_key = {
            review.document_key: review
            for review in screening_record.document_reviews.select_related("evidence_item")
        }
    review_items = []
    for requirement in get_applicant_document_requirements(application):
        evidence = current_documents.get(requirement.code)
        document_review = document_reviews_by_key.get(requirement.code)
        is_not_applicable = (
            requirement.conditional_on_performance_rating
            and application.performance_rating_not_applicable
            and evidence is None
        )
        review_items.append(
            {
                "requirement": requirement,
                "evidence": evidence,
                "document_review": document_review,
                "is_submitted": evidence is not None,
                "is_not_applicable": is_not_applicable,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
                "review_status": (
                    document_review.status
                    if document_review is not None
                    else (
                        ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
                        if is_not_applicable
                        else ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                    )
                ),
                "review_remarks": document_review.remarks if document_review is not None else "",
            }
        )
    return review_items


def get_missing_required_applicant_document_requirements(application):
    present_document_codes = set(get_current_applicant_document_map(application))
    return [
        requirement
        for requirement in get_required_applicant_document_requirements(
            application,
            performance_rating_not_applicable=application.performance_rating_not_applicable,
        )
        if requirement.code not in present_document_codes
    ]


def get_duplicate_applicant_document_groups(application):
    requirement_titles = {
        requirement.code: requirement.title
        for requirement in get_applicant_document_requirements(application)
    }
    digest_to_codes = {}
    for evidence in get_current_applicant_document_items(application):
        if evidence.document_key not in requirement_titles:
            continue
        digest_to_codes.setdefault(evidence.sha256_digest, set()).add(evidence.document_key)
    return [
        [
            {
                "code": document_key,
                "title": requirement_titles[document_key],
            }
            for document_key in sorted(document_keys)
        ]
        for document_keys in digest_to_codes.values()
        if len(document_keys) > 1
    ]


def _archive_applicant_document_key(application, document_key, actor, archive_tag):
    evidence_items = list(
        get_current_applicant_document_items(application).filter(document_key=document_key)
    )
    if not evidence_items:
        return []

    now = timezone.now()
    archived_ids = []
    for evidence in evidence_items:
        evidence.is_archived = True
        evidence.archive_tag = archive_tag
        evidence.archived_at = now
        evidence.archived_by = actor
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
        archived_ids.append(evidence.id)

    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_ARCHIVED,
        description=f"Archived applicant document '{document_key}' because it no longer applies.",
        metadata={
            "document_key": document_key,
            "evidence_ids": archived_ids,
            "archive_tag": archive_tag,
        },
    )
    return evidence_items


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
    existing_submitted_application = RecruitmentApplication.objects.filter(
        position=entry,
        applicant_email__iexact=applicant_email,
        submitted_at__isnull=False,
    ).exists()
    if existing_submitted_application:
        raise ValueError(
            "You have already submitted an application for this position using this email address."
        )

    applicant_user = get_portal_applicant_identity_by_email(applicant_email)
    application = None
    if applicant_user is not None:
        application = get_reusable_public_application_for_applicant(entry, applicant_user)
        if application and application.submitted_at:
            raise ValueError(
                "You have already submitted an application for this position using this email address."
            )
    if application is None:
        application = get_reusable_public_application_draft(entry, applicant_email)
    created = application is None
    if application is None:
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
    try:
        application.save()
    except IntegrityError as exc:
        if "unique_application_per_applicant_position" not in str(exc):
            raise
        raise ValueError(
            "An application for this recruitment entry already exists for this applicant."
        ) from exc
    if (
        application.performance_rating_applicability
        == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
    ):
        requirement_uploads.pop(PERFORMANCE_RATING, None)
        _archive_applicant_document_key(
            application,
            PERFORMANCE_RATING,
            applicant_user,
            "Marked not applicable by applicant",
        )
    requirement_catalog = {
        requirement.code: requirement
        for requirement in get_applicant_document_requirements(entry)
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
    otp_expires_at = otp_expires_at or application.otp_expires_at
    subject = "RecruitGuard-CHD applicant verification code"
    text_body = (
        "RecruitGuard-CHD applicant verification code\n\n"
        f"Your verification code is {otp_code}.\n"
        f"It expires in {settings.APPLICATION_OTP_VALIDITY_MINUTES} minutes.\n\n"
        "Do not share this code. RecruitGuard-CHD staff will never ask for it."
    )
    try:
        html_body = render_to_string(
            "email/applicant_otp.html",
            {
                "application": application,
                "otp_code": otp_code,
                "otp_expires_at": otp_expires_at,
                "otp_validity_minutes": settings.APPLICATION_OTP_VALIDITY_MINUTES,
                **email_branding_context("applicant"),
            },
        )
    except Exception:
        # The HTML body is only a richer alternative to the plain-text message,
        # which already carries the verification code. Degrade to text-only
        # delivery rather than blocking the code email on a template failure.
        logger.exception(
            "Failed to render applicant OTP HTML email for application %s; "
            "falling back to plain-text body.",
            application.pk,
        )
        html_body = ""
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[application.applicant_email],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    try:
        email.send(fail_silently=False)
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception(
            "Applicant OTP email delivery failed for application %s.",
            application.pk,
        )
        raise ApplicationOTPDeliveryError(
            "Your application draft and uploaded files were saved, but we could not send "
            "the verification code right now. Please try again in a few moments."
        ) from exc
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_SENT,
        description="Sent applicant verification code for final submission.",
        metadata={"otp_expires_at": otp_expires_at.isoformat()},
    )


def issue_application_otp(application, actor=None, *, defer_delivery=True, enforce_cooldown=False):
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("A verification code can only be issued while the application is still in draft.")
    if enforce_cooldown and application.otp_requested_at:
        elapsed = timezone.now() - application.otp_requested_at
        cooldown_seconds = settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS
        if elapsed.total_seconds() < cooldown_seconds:
            remaining = max(1, int(cooldown_seconds - elapsed.total_seconds()))
            raise ValueError(f"Please wait {remaining} seconds before requesting another code.")

    otp_code = _generate_otp_code()
    now = timezone.now()
    application.otp_hash = _hash_application_otp(application, otp_code)
    application.otp_requested_at = now
    application.otp_expires_at = now + timedelta(minutes=settings.APPLICATION_OTP_VALIDITY_MINUTES)
    application.otp_verified_at = None
    application.otp_attempt_count = 0
    application.save(
        update_fields=[
            "otp_hash",
            "otp_requested_at",
            "otp_expires_at",
            "otp_verified_at",
            "otp_attempt_count",
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
    if defer_delivery and connection.in_atomic_block:
        transaction.on_commit(deliver_otp, using=connection.alias)
    else:
        deliver_otp()
    return otp_code


def _application_otp_max_attempts():
    return max(1, int(getattr(settings, "APPLICATION_OTP_MAX_ATTEMPTS", 5)))


def _record_application_otp_failure(application, actor, *, reason, locked=False):
    max_attempts = _application_otp_max_attempts()
    record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=(
            AuditLog.Action.APPLICATION_OTP_LOCKED
            if locked
            else AuditLog.Action.APPLICATION_OTP_FAILED
        ),
        description=(
            "Applicant verification code locked after too many invalid attempts."
            if locked
            else "Applicant verification code verification failed."
        ),
        metadata={
            "reason": reason,
            "attempt_count": application.otp_attempt_count,
            "max_attempts": max_attempts,
        },
    )


def verify_application_otp(application, otp_code, actor=None):
    application = RecruitmentApplication.objects.get(pk=application.pk)
    actor = actor or application.applicant
    locked_error = "Too many invalid verification attempts. Request a new code before final submission."
    if application.status != RecruitmentApplication.Status.DRAFT:
        raise ValueError("Email verification is only available before final submission.")
    if not application.otp_hash or not application.otp_expires_at:
        _record_application_otp_failure(application, actor, reason="missing_code")
        raise ValueError("Request a verification code first.")
    if application.otp_expires_at < timezone.now():
        _record_application_otp_failure(application, actor, reason="expired")
        raise ValueError("The verification code has expired. Request a new code before final submission.")
    if application.otp_attempt_count >= _application_otp_max_attempts():
        _record_application_otp_failure(application, actor, reason="attempt_limit", locked=True)
        raise ValueError(locked_error)

    expected_hash = _hash_application_otp(application, otp_code)
    if not hmac.compare_digest(application.otp_hash, expected_hash):
        application.otp_attempt_count += 1
        application.save(update_fields=["otp_attempt_count", "updated_at"])
        locked = application.otp_attempt_count >= _application_otp_max_attempts()
        _record_application_otp_failure(application, actor, reason="invalid_code", locked=locked)
        if locked:
            raise ValueError(locked_error)
        raise ValueError("The verification code is invalid.")

    application.otp_verified_at = timezone.now()
    application.otp_attempt_count = 0
    application.save(update_fields=["otp_verified_at", "otp_attempt_count", "updated_at"])
    record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
        description="Applicant verification code verified successfully.",
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
        issue_otp=False,
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
    return record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
        description="Denied evidence download request.",
        metadata=metadata,
    )


def decrypt_evidence_bytes(evidence, actor, *, application=None):
    context_application = get_evidence_context_application_for_user(
        actor,
        evidence,
        preferred_application=application,
    )
    if context_application is None:
        record_evidence_access_denied(evidence, actor, application=application, reason="unauthorized")
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
        raise ValueError("You cannot upload files for this application.")
    if artifact_scope == EvidenceVaultItem.OwnerScope.CASE and not hasattr(application, "case"):
        raise ValueError("A case must exist before case files can be uploaded.")
    validated_upload = validate_applicant_document_upload(uploaded_file)
    raw_bytes = validated_upload.raw_bytes
    content_type = validated_upload.canonical_content_type
    file_size = validated_upload.size_bytes
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
    duplicate_document_groups = get_duplicate_applicant_document_groups(application)
    if duplicate_document_groups:
        duplicate_labels = "; ".join(
            ", ".join(item["title"] for item in group)
            for group in duplicate_document_groups
        )
        raise ValueError(
            "Each document slot must use a different file before final submission. "
            f"Duplicates: {duplicate_labels}."
        )
    if not application.position.is_open_for_intake:
        raise ValueError("This recruitment entry is not currently open for intake.")
    if not application.otp_is_currently_valid:
        if application.otp_expires_at and application.otp_expires_at < timezone.now():
            raise ValueError("Your email verification has expired. Request a new code before final submission.")
        raise ValueError("Email verification is required before final submission.")

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

    if (
        current_section == "decision"
        and effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        and application.branch == PositionPosting.Branch.PLANTILLA
        and get_latest_finalized_comparative_assessment_report(application)
        and not get_final_selection_for_application(application)
    ):
        return [
            ("return_car_for_reassessment", "Return CAR for HRMPSB Reassessment"),
        ]

    if (
        current_section == "decision"
        and effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and current_stage == RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        and application.branch == PositionPosting.Branch.COS
    ):
        return [
            ("return_to_hrm_chief", "Return to HRM Chief"),
        ]

    if current_section != "actions":
        return []

    if _auto_advance_boundary_is_ready(application, current_stage, current_section):
        return []

    if effective_role == RecruitmentUser.Role.SECRETARIAT and current_stage == RecruitmentCase.Stage.SECRETARIAT_REVIEW:
        endorse_label = (
            "Endorse to HRMPSB"
            if application.branch == PositionPosting.Branch.PLANTILLA
            else "Endorse to HRM Chief"
        )
        return [
            ("endorse", endorse_label),
            ("return_to_applicant", "Return to Applicant"),
            ("reject", "Reject Application"),
        ]
    if effective_role == RecruitmentUser.Role.HRM_CHIEF and current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
        if application.branch == PositionPosting.Branch.COS:
            return []
        endorse_label = "Endorse to HRMPSB"
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
        return []
    return []


def _transition_target(application, effective_role, action):
    current_stage = get_current_review_stage(application)
    if effective_role == RecruitmentUser.Role.SECRETARIAT:
        if action == "endorse":
            if current_stage != RecruitmentCase.Stage.SECRETARIAT_REVIEW:
                raise ValueError("Unsupported workflow action for the current stage.")
            if application.branch == PositionPosting.Branch.PLANTILLA:
                return (
                    RecruitmentUser.Role.HRMPSB_MEMBER,
                    RecruitmentApplication.Status.HRMPSB_REVIEW,
                    "Plantilla application endorsed to HRMPSB by Secretariat.",
                )
            return (
                RecruitmentUser.Role.HRM_CHIEF,
                RecruitmentApplication.Status.HRM_CHIEF_REVIEW,
                "COS application endorsed to HRM Chief by Secretariat.",
            )
        if action == "return_to_applicant":
            return RecruitmentUser.Role.APPLICANT, RecruitmentApplication.Status.RETURNED_TO_APPLICANT, "Returned by Secretariat."
        if action == "reject":
            return "", RecruitmentApplication.Status.REJECTED, "Rejected by Secretariat."
    if effective_role == RecruitmentUser.Role.HRM_CHIEF:
        if action == "endorse":
            if current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW:
                raise ValueError("Unsupported workflow action for the current stage.")
            if application.branch == PositionPosting.Branch.COS:
                raise ValueError("COS selection is recorded by HRM Chief from the decision step.")
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
    raise ValueError("This action is not allowed at the current step.")


@transaction.atomic
def process_workflow_action(application, actor, action, remarks):
    effective_role = get_effective_role_for_action(actor, application)
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before saving the next step.")
    if application.case.is_stage_locked:
        raise ValueError("This case is final and cannot be edited unless it is reopened with authorization.")
    if not user_can_process_application(actor, application):
        if (
            effective_role == RecruitmentUser.Role.SECRETARIAT
            and application.level == PositionPosting.Level.LEVEL_2
        ):
            raise ValueError(
                "Secretariat can handle a Level 2 case only after HRM Chief sends it to Secretariat."
            )
        raise ValueError("This case is not currently assigned to you.")
    current_section = get_current_workflow_section(application)
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and action == "return_car_for_reassessment"
    ):
        if current_section != "decision":
            raise ValueError(_workflow_progress_block_message(application, current_section))
        return return_car_for_reassessment(application, actor, remarks)
    if (
        effective_role == RecruitmentUser.Role.APPOINTING_AUTHORITY
        and application.branch == PositionPosting.Branch.PLANTILLA
        and action in {"approve", "reject", "return_to_hrm_chief"}
    ):
        raise ValueError("Plantilla Appointing Authority actions must be recorded from the finalized CAR.")
    if action in {"endorse", "recommend"} and current_section != "actions":
        raise ValueError(_workflow_progress_block_message(application, current_section))
    if (
        effective_role in SCREENING_REVIEW_ROLES
        and application.case.current_stage in SCREENING_STAGES
        and action == "endorse"
        and not screening_is_finalized_for_current_stage(application)
    ):
        raise ValueError("Finalize the screening record before moving this case forward.")
    if (
        effective_role == RecruitmentUser.Role.HRM_CHIEF
        and application.branch == PositionPosting.Branch.COS
        and application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        and action == "endorse"
    ):
        deliberation_record = get_deliberation_record(application, stage=application.case.current_stage)
        if not deliberation_record or not deliberation_record.is_finalized:
            raise ValueError("Finalize the deliberation record before moving this COS case forward.")
    if (
        effective_role == RecruitmentUser.Role.HRMPSB_MEMBER
        and application.branch == PositionPosting.Branch.PLANTILLA
        and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and action == "recommend"
    ):
        comparative_assessment_report = get_comparative_assessment_report(
            application,
            stage=application.case.current_stage,
        )
        if not comparative_assessment_report or not comparative_assessment_report.is_finalized:
            raise ValueError(
                "Finalize the Comparative Assessment Report before recommending this Plantilla case."
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
        next_role_label = RecruitmentUser.Role(next_role).label
        record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=f"Case assigned to {next_role_label}.",
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
            description=f"Case assigned to {next_role_label}.",
            recruitment_case=application.case,
            from_handler_role=previous_role,
            to_handler_role=next_role,
            from_status=previous_status,
            to_status=next_status,
            from_stage=case_transition["previous_stage"],
            to_stage=application.case.current_stage,
            notes=remarks,
        )
        stage_label = application.case.get_current_stage_display()
        if action == "endorse":
            title = f"{application.reference_label} endorsed to you for {stage_label}"
        elif action == "recommend":
            title = f"{application.reference_label} recommended to you for {stage_label}"
        elif action.startswith("return"):
            title = f"{application.reference_label} returned to you for {stage_label}"
        else:
            title = f"{application.reference_label} assigned to you"
        _emit_case_assignment_notification(
            application,
            actor,
            next_role,
            kind=(
                Notification.Kind.CASE_RETURNED
                if action.startswith("return")
                else Notification.Kind.CASE_ASSIGNED
            ),
            title=title,
            body=description,
        )
    if (
        effective_role == RecruitmentUser.Role.SECRETARIAT
        and application.level == PositionPosting.Level.LEVEL_2
    ):
        _consume_active_secretariat_handoff(
            application,
            actor,
            "Secretariat special authorization ended after Level 2 processing.",
        )
    if next_status == RecruitmentApplication.Status.APPROVED:
        queue_selected_applicant_notification(application, actor=actor)
    elif next_status == RecruitmentApplication.Status.REJECTED:
        queue_non_selected_applicant_notification(
            application,
            actor=actor,
            cut_at_screening=case_transition["previous_stage"] in SCREENING_STAGES,
        )
    elif next_status == RecruitmentApplication.Status.RETURNED_TO_APPLICANT:
        screening_record = get_screening_record(
            application,
            stage=case_transition["previous_stage"],
        )
        requested_document_reviews = []
        if screening_record is not None:
            requested_document_reviews = list(
                screening_record.document_reviews.filter(
                    status=ScreeningDocumentReview.ReviewStatus.REQUEST_RESUBMISSION
                ).order_by("display_order", "created_at")
            )
        if requested_document_reviews:
            queue_document_resubmission_request_notification(
                application,
                actor=actor,
                document_reviews=requested_document_reviews,
                workflow_remarks=remarks,
            )
        else:
            queue_application_returned_to_applicant_notification(
                application,
                actor=actor,
                workflow_remarks=remarks,
            )
    return application


def _final_selection_items_are_ready(report):
    items = list(get_comparative_assessment_report_items_for_report(report))
    if not items:
        raise ValueError("The finalized CAR has no ranked applicants.")
    for item in items:
        application = item.application
        case = getattr(application, "case", None)
        if (
            not case
            or application.status != RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW
            or application.current_handler_role != RecruitmentUser.Role.APPOINTING_AUTHORITY
            or case.current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
            or case.current_handler_role != RecruitmentUser.Role.APPOINTING_AUTHORITY
            or case.is_stage_locked
        ):
            raise ValueError(
                "All CAR applicants must be ready for Appointing Authority final selection."
            )
    return items


@transaction.atomic
def return_car_for_reassessment(application, actor, remarks):
    remarks = (remarks or "").strip()
    if not remarks:
        raise ValueError("Record the reason for returning the CAR.")
    if application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError("CAR reassessment returns are only available for Plantilla vacancies.")
    if actor.role != RecruitmentUser.Role.APPOINTING_AUTHORITY:
        raise ValueError("Only the Appointing Authority may return the CAR for reassessment.")
    if not user_can_process_application(actor, application):
        raise ValueError("This case is not currently assigned to you.")
    if get_current_workflow_section(application) != "decision":
        raise ValueError(_workflow_progress_block_message(application))
    if get_final_selection_for_entry(application.position):
        raise ValueError("Final selection has already been recorded for this vacancy.")

    report = get_latest_finalized_comparative_assessment_report(application)
    if not report:
        raise ValueError("Finalize the CAR before returning it for reassessment.")

    report_items = _final_selection_items_are_ready(report)
    report.is_returned = True
    report.returned_by = actor
    report.returned_at = timezone.now()
    report.return_reason = remarks
    report.full_clean()
    report.save(
        update_fields=[
            "is_returned",
            "returned_by",
            "returned_by_role",
            "returned_at",
            "return_reason",
            "updated_at",
        ]
    )

    for item in report_items:
        target_application = item.application
        previous_role = target_application.current_handler_role
        previous_status = target_application.status
        deliberation_record = item.deliberation_record
        if deliberation_record:
            deliberation_record.is_finalized = False
            deliberation_record.finalized_by = None
            deliberation_record.finalized_at = None
            deliberation_record.finalized_by_role = ""
            deliberation_record.full_clean()
            deliberation_record.save(
                update_fields=[
                    "is_finalized",
                    "finalized_by",
                    "finalized_at",
                    "finalized_by_role",
                    "updated_at",
                ]
            )

        case_transition = _sync_case_after_workflow_action(
            application=target_application,
            actor=actor,
            next_role=RecruitmentUser.Role.HRMPSB_MEMBER,
            next_status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            remarks=remarks,
        )
        target_application.current_handler_role = RecruitmentUser.Role.HRMPSB_MEMBER
        target_application.status = RecruitmentApplication.Status.HRMPSB_REVIEW
        target_application.closed_at = None
        target_application.save(
            update_fields=["current_handler_role", "status", "closed_at", "updated_at"]
        )

        record_audit_event(
            application=target_application,
            actor=actor,
            action=AuditLog.Action.CAR_RETURNED,
            description="Appointing Authority returned the CAR for HRMPSB reassessment.",
            metadata={
                "car_report_id": report.id,
                "car_version_number": report.version_number,
                "car_item_id": item.id,
                "return_reason": remarks,
                "deliberation_record_id": deliberation_record.id if deliberation_record else "",
                "from_status": previous_status,
                "to_status": target_application.status,
                "from_role": previous_role,
                "to_role": target_application.current_handler_role,
                "from_stage": case_transition["previous_stage"],
                "to_stage": target_application.case.current_stage,
                "from_case_status": case_transition["previous_case_status"],
                "to_case_status": target_application.case.case_status,
            },
        )
        record_routing_history_event(
            application=target_application,
            actor=actor,
            route_type=RoutingHistory.RouteType.REOPEN,
            description="Returned from Appointing Authority to HRMPSB for CAR reassessment.",
            recruitment_case=target_application.case,
            from_handler_role=previous_role,
            to_handler_role=target_application.current_handler_role,
            from_status=previous_status,
            to_status=target_application.status,
            from_stage=case_transition["previous_stage"],
            to_stage=target_application.case.current_stage,
            notes=remarks,
        )
        _emit_case_assignment_notification(
            target_application,
            actor,
            RecruitmentUser.Role.HRMPSB_MEMBER,
            kind=Notification.Kind.CASE_RETURNED,
            title=f"{target_application.reference_label} returned to you for HRMPSB Review",
            body="The CAR was returned for reassessment.",
            tab="deliberation",
        )

    return report


@transaction.atomic
def record_final_selection(application, actor, cleaned_data):
    if application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError("CAR-based final selection is only available for Plantilla vacancies.")
    if not user_can_record_final_selection(actor, application):
        raise ValueError(
            "Only the Appointing Authority may record final selection from the finalized CAR."
        )

    report = get_latest_finalized_comparative_assessment_report(application)
    if not report:
        raise ValueError("Finalize the CAR before recording the final selection.")
    if get_final_selection_for_entry(report.recruitment_entry):
        raise ValueError("Final selection has already been recorded for this vacancy.")

    selected_item = cleaned_data["selected_item"]
    selected_item = (
        ComparativeAssessmentReportItem.objects.select_related(
            "report",
            "recruitment_case",
            "recruitment_case__application",
        )
        .filter(pk=selected_item.pk, report=report)
        .first()
    )
    if not selected_item:
        raise ValueError("Select an applicant from the finalized CAR.")
    if (
        selected_item.rank_order > TOP_FIVE_SELECTION_LIMIT
        and not cleaned_data.get("is_deep_selection")
    ):
        raise ValueError(
            "Selecting outside the top five requires deep selection documentation."
        )
    if cleaned_data.get("is_deep_selection") and not (
        cleaned_data.get("deep_selection_justification") or ""
    ).strip():
        raise ValueError(
            "Record the deep-selection justification before finalizing this selection."
        )

    report_items = _final_selection_items_are_ready(report)
    car_snapshot = build_car_selection_packet(report)
    selection = FinalSelection(
        comparative_assessment_report=report,
        recruitment_entry=report.recruitment_entry,
        selected_item=selected_item,
        selected_application=selected_item.application,
        selected_case=selected_item.recruitment_case,
        decided_by=actor,
        is_deep_selection=cleaned_data.get("is_deep_selection", False),
        deep_selection_justification=cleaned_data.get("deep_selection_justification", ""),
        decision_notes=cleaned_data["decision_notes"],
        car_snapshot=car_snapshot,
    )
    selection.full_clean()
    selection.save()

    for item in report_items:
        target_application = item.application
        selected = item.pk == selected_item.pk
        next_role = _completion_handler_role(target_application) if selected else ""
        next_status = (
            RecruitmentApplication.Status.APPROVED
            if selected
            else RecruitmentApplication.Status.REJECTED
        )
        previous_role = target_application.current_handler_role
        previous_status = target_application.status
        case_transition = _sync_case_after_workflow_action(
            application=target_application,
            actor=actor,
            next_role=next_role,
            next_status=next_status,
            remarks=selection.decision_notes,
        )
        target_application.current_handler_role = next_role
        target_application.status = next_status
        target_application.closed_at = (
            timezone.now()
            if target_application.case.current_stage == RecruitmentCase.Stage.CLOSED
            else None
        )
        target_application.save(update_fields=["current_handler_role", "status", "closed_at", "updated_at"])

        record_audit_event(
            application=target_application,
            actor=actor,
            action=AuditLog.Action.DECISION_RECORDED,
            description=(
                "Appointing Authority selected this applicant from the finalized CAR."
                if selected
                else "Appointing Authority selected another applicant from the finalized CAR."
            ),
            metadata={
                "final_selection_id": selection.id,
                "car_report_id": report.id,
                "selected_car_item_id": selected_item.id,
                "car_item_id": item.id,
                "decision_outcome": (
                    FinalDecision.Outcome.SELECTED
                    if selected
                    else FinalDecision.Outcome.NOT_SELECTED
                ),
                "decision_notes": selection.decision_notes,
                "is_deep_selection": selection.is_deep_selection,
                "deep_selection_justification_recorded": bool(selection.deep_selection_justification),
                "from_status": previous_status,
                "to_status": next_status,
                "from_role": previous_role,
                "to_role": next_role,
                "from_stage": case_transition["previous_stage"],
                "to_stage": target_application.case.current_stage,
                "from_case_status": case_transition["previous_case_status"],
                "to_case_status": target_application.case.case_status,
                "case_locked": target_application.case.is_stage_locked,
            },
        )

        if selected:
            next_role_label = RecruitmentUser.Role(next_role).label
            record_audit_event(
                application=target_application,
                actor=actor,
                action=AuditLog.Action.ROUTED,
                description=f"Application routed to {next_role} for completion handling.",
                metadata={
                    "status": next_status,
                    "current_handler_role": next_role,
                    **_case_timeline_metadata(target_application.case),
                },
            )
            record_routing_history_event(
                application=target_application,
                actor=actor,
                route_type=RoutingHistory.RouteType.FORWARD,
                description=f"Selected from CAR and routed to {next_role_label} for completion handling.",
                recruitment_case=target_application.case,
                from_handler_role=previous_role,
                to_handler_role=next_role,
                from_status=previous_status,
                to_status=next_status,
                from_stage=case_transition["previous_stage"],
                to_stage=target_application.case.current_stage,
                notes=selection.decision_notes,
            )
            _emit_case_assignment_notification(
                target_application,
                actor,
                next_role,
                title=f"{target_application.reference_label} assigned to you for Completion Tracking",
                body="The applicant was selected and is ready for completion tracking.",
                tab="completion",
            )
            queue_selected_applicant_notification(target_application, actor=actor)
        else:
            record_routing_history_event(
                application=target_application,
                actor=actor,
                route_type=RoutingHistory.RouteType.CLOSE,
                description="Closed as not selected after Appointing Authority selected from CAR.",
                recruitment_case=target_application.case,
                from_handler_role=previous_role,
                to_handler_role="",
                from_status=previous_status,
                to_status=next_status,
                from_stage=case_transition["previous_stage"],
                to_stage=target_application.case.current_stage,
                notes=selection.decision_notes,
            )
            queue_non_selected_applicant_notification(target_application, actor=actor)

    return selection


@transaction.atomic
def record_final_decision(application, actor, cleaned_data):
    if application.branch == PositionPosting.Branch.PLANTILLA:
        raise ValueError("Plantilla final selection must be recorded from the finalized CAR.")
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a final decision can be recorded.")
    if application.case.is_stage_locked:
        raise ValueError("This case is final and cannot be edited unless it is reopened with authorization.")
    expected_stage, expected_role = _final_decision_stage_and_role(application)
    if not user_can_record_final_decision(actor, application):
        expected_role_label = RecruitmentUser.Role(expected_role).label
        raise ValueError(
            f"Only the {expected_role_label} may record the final decision at the current step."
        )
    if application.case.current_stage != expected_stage:
        expected_stage_label = RecruitmentCase.Stage(expected_stage).label
        raise ValueError(
            f"Final decisions may only be recorded during the {expected_stage_label} step."
        )

    submission_packet = build_submission_packet(application)
    missing_components = submission_packet["summary"]["missing_components"]
    if missing_components:
        raise ValueError(
            "The submission packet is incomplete for final decision recording. Missing: "
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
        _emit_case_assignment_notification(
            application,
            actor,
            next_role,
            title=f"{application.reference_label} assigned to you for Completion Tracking",
            body="The applicant was selected and is ready for completion tracking.",
            tab="completion",
        )
        queue_selected_applicant_notification(application, actor=actor)
    else:
        queue_non_selected_applicant_notification(application, actor=actor)
    return decision


@transaction.atomic
def close_recruitment_case(application, actor, closure_notes):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before it can be closed.")
    if not user_can_manage_completion(actor, application):
        raise ValueError("This case is not currently assigned to you for closure.")

    case = application.case
    if case.current_stage != RecruitmentCase.Stage.COMPLETION:
        raise ValueError("Case closure is available only from the completion step.")

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

    _transition_case_stage(case, RecruitmentCase.Stage.CLOSED)
    case.case_status = RecruitmentCase.CaseStatus.APPROVED
    case.current_handler_role = ""
    case.is_stage_locked = True
    case.locked_stage = RecruitmentCase.Stage.COMPLETION
    case.closed_at = closed_at
    case.save(
        update_fields=[
            "branch",
            "current_stage",
            "stage_entered_at",
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
    if actor.role != RecruitmentUser.Role.HRM_CHIEF:
        raise ValueError("Only the HRM Chief can send a Level 2 case to Secretariat.")
    if application.level != PositionPosting.Level.LEVEL_2:
        raise ValueError("Special authorization is only required for Level 2 applications.")
    if not hasattr(application, "case"):
        raise ValueError("A recruitment case must exist before Level 2 special authorization can be recorded.")
    case = application.case
    if (
        application.status != RecruitmentApplication.Status.HRM_CHIEF_REVIEW
        or application.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
        or case.current_stage != RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        or case.current_handler_role != RecruitmentUser.Role.HRM_CHIEF
    ):
        raise ValueError(
            "Level 2 Secretariat authorization is only available while the case is assigned to HRM Chief review."
        )
    route_case_between_secretariat_and_hrm_chief(
        application=application,
        actor=actor,
        target_role=RecruitmentUser.Role.SECRETARIAT,
        remarks=reason,
    )
    application.refresh_from_db()
    return application.active_secretariat_override


@transaction.atomic
def reopen_recruitment_case(application, actor, reason):
    if not hasattr(application, "case"):
        raise ValueError("A case does not exist for this application.")
    case = application.case
    if actor.role not in CASE_REOPEN_ROLES:
        raise ValueError("Only the HRM Chief can reopen a finalized case.")
    if not case.is_stage_locked or not case.locked_stage:
        raise ValueError("Only finalized cases can be reopened.")

    previous_stage = case.current_stage
    previous_role = application.current_handler_role
    previous_status = application.status
    reopened_stage = case.locked_stage
    reopened_status = _application_status_from_stage(reopened_stage)
    if not reopened_status:
        raise ValueError("This finalized case does not point to a step that can be reopened.")

    _transition_case_stage(case, reopened_stage)
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
            "stage_entered_at",
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
        description="Authorized reopen applied to a finalized recruitment case.",
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
        description=f"Case reopened and assigned to {RecruitmentUser.Role(case.current_handler_role).label}.",
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
        description=f"Case reopened and assigned to {RecruitmentUser.Role(case.current_handler_role).label}.",
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
        f"Prepared By: {actor}",
        f"Prepared At: {timezone.now():%Y-%m-%d %H:%M}",
        f"Generation Version: {generation_number}",
        f"Preliminary Ranking Basis: {_assessment_weight_display(application.position) or 'Not available'}",
        "",
        "Ranked Candidates",
    ]
    if summary_notes:
        lines.extend(["Summary Notes:", summary_notes, ""])
    for row in candidate_rows:
        lines.extend(
            [
                (
                    f"Final Rank {row['rank_order']} | Preliminary Rank {row['preliminary_rank_order'] or 'N/A'} | "
                    f"{row['application'].reference_number} | "
                    f"{row['application'].applicant_display_name}"
                ),
                (
                    f"Document Review: {row['qualification_outcome'] or 'N/A'} "
                    f"({row['document_review_score'] or 'N/A'}) | "
                    f"Exam: {row['exam_status'] or 'N/A'} ({row['exam_score'] or 'N/A'}) | "
                    f"Interview Avg: {row['interview_average_score'] or 'N/A'}"
                ),
                f"Preliminary Assessment Score: {_decimal_string(row['assessment_score']) or 'N/A'}",
                f"Exam Components: {row.get('exam_components') or 'N/A'}",
                f"HRMPSB Recommendation: {row.get('recommendation') or 'N/A'}",
                row["decision_support_summary"] or "No decision-support summary recorded.",
                f"Ranking Notes: {row['ranking_notes'] or 'N/A'}",
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
    scope_component = _safe_export_path_component(evidence.artifact_scope, "file")
    stage_component = _safe_export_path_component(evidence.stage, "unstaged")
    document_component = evidence.document_key or _safe_export_path_component(evidence.label, "file")
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
    final_selection = get_final_selection_for_application(application)
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
            for decision in get_final_decision_history(application)
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
        "RecruitGuard-CHD Verification Summary",
        lines,
        document_title=f"{application.reference_number} Verification Summary",
    )


def record_export_denied(application, actor, *, reason="unauthorized"):
    return record_audit_event(
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
    if is_create and entry.branch == PositionPosting.Branch.PLANTILLA:
        # Seed the per-vacancy assessment weights with the office defaults (exam 60/40,
        # CAR 40/20/40) so they are editable from the moment the Plantilla vacancy exists.
        # COS vacancies use a single unweighted end-user exam score and have no CAR, so they
        # get no weights row.
        get_or_create_vacancy_assessment_weights(entry)
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


def get_or_create_vacancy_assessment_weights(posting):
    """Return the vacancy's assessment-weight row, creating it (with seeded defaults) if
    absent. Use this when a persisted, editable row is needed (seeding, the edit screen,
    locking). Pure reads for computation should use
    ``PositionPosting.assessment_weights_or_default`` instead, which never writes."""
    weights, _created = VacancyAssessmentWeights.objects.get_or_create(recruitment_entry=posting)
    return weights


def lock_vacancy_assessment_weights(posting):
    """Lock the vacancy's weights once scoring starts, so the split a finalized exam or CAR
    was computed against can no longer be edited. Mirrors lock_competency_rating_template."""
    weights = get_or_create_vacancy_assessment_weights(posting)
    if weights.status != VacancyAssessmentWeights.Status.LOCKED:
        weights.status = VacancyAssessmentWeights.Status.LOCKED
        if not weights.locked_at:
            weights.locked_at = timezone.now()
        weights.save(update_fields=["status", "locked_at", "updated_at"])
    return weights


def create_default_position_document_requirements(posting):
    """Seed the branch-standard document requirement rows for a brand-new posting."""
    branch_catalog = APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(posting.branch, ())
    PositionDocumentRequirement.objects.bulk_create(
        PositionDocumentRequirement(
            posting=posting,
            document_code=requirement.code,
            is_required=requirement.is_required,
            order=index,
        )
        for index, requirement in enumerate(branch_catalog, start=1)
    )


@transaction.atomic
def set_position_document_requirements(posting, selections, actor):
    """Replace a posting's applicant document configuration.

    ``selections`` is an iterable of dicts ``{"code", "applies", "is_required"}``. Only codes
    valid for the posting's branch are honoured; the minimum required set always applies and
    stays required; conditional documents (performance rating) are stored as optional because
    their requiredness is applicant-driven.
    """
    if posting.is_live_for_metadata_lock:
        raise ValidationError(
            "Application document requirements are locked because this recruitment entry has "
            "submitted applications or linked recruitment cases."
        )

    branch_catalog = APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(posting.branch, ())
    selection_by_code = {selection["code"]: selection for selection in selections}

    desired = {}
    order_by_code = {}
    for index, requirement in enumerate(branch_catalog, start=1):
        code = requirement.code
        order_by_code[code] = index
        forced_min = code in MIN_REQUIRED_DOCUMENT_CODES
        selection = selection_by_code.get(code, {})
        applies = forced_min or bool(selection.get("applies"))
        if not applies:
            continue
        if requirement.conditional_on_performance_rating:
            is_required = False
        elif forced_min:
            is_required = True
        else:
            is_required = bool(selection.get("is_required"))
        desired[code] = is_required

    existing = {row.document_code: row for row in posting.document_requirements.all()}
    for code, row in existing.items():
        if code not in desired:
            row.delete()
    for code, is_required in desired.items():
        row = existing.get(code) or PositionDocumentRequirement(
            posting=posting, document_code=code
        )
        row.is_required = is_required
        row.order = order_by_code[code]
        row.full_clean()
        row.save()

    record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.RECRUITMENT_ENTRY_UPDATED,
        description=f"Configured application documents for '{posting.job_code}'.",
        metadata={
            "entry_id": posting.id,
            "entry_code": posting.job_code,
            "document_codes": sorted(desired),
            "required_codes": sorted(code for code, required in desired.items() if required),
        },
    )


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
