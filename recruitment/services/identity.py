"""Internal staff sign-in support: login lockout, password reset counters, email
change verification and the MFA challenge.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..email_branding import email_branding_context
from ..models import (
    AuditLog,
    InternalEmailChangeRequest,
    InternalLoginAttempt,
    InternalMFAChallenge,
    InternalPasswordHistory,
    RecruitmentUser,
)
from . import audit


logger = logging.getLogger("recruitment.services")


def normalize_internal_login_username(username):
    return (username or "").strip().lower()


def _internal_login_attempt_key(username, request):
    return normalize_internal_login_username(username), audit._request_ip_address(request) or ""


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

    subject = "DOH–CHD CALABARZON Recruitment Portal internal login lockout alert"
    body = (
        "The DOH–CHD CALABARZON Recruitment Portal detected repeated failed internal login attempts.\n\n"
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
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.INTERNAL_LOGIN_LOCKED,
        description="Blocked internal login attempt during lockout window.",
        metadata={
            "username": attempt.username_normalized,
            "ip_address": attempt.ip_address,
            "failure_count": attempt.failure_count,
            "locked_until": attempt.locked_until.isoformat(),
            "user_agent": audit._request_user_agent(request),
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
            "user_agent": audit._request_user_agent(request),
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
    attempt.user_agent = audit._request_user_agent(request)
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
    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.INTERNAL_LOGIN_FAILED,
        description="Internal login failed.",
        metadata=metadata,
    )
    if attempt.locked_until:
        audit.record_system_audit_event(
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
            logger.exception(
                "Failed to send the internal login lockout alert for attempt %s.",
                attempt.pk,
            )
            audit.record_system_audit_event(
                actor=actor,
                action=AuditLog.Action.INTERNAL_LOGIN_ALERT_FAILED,
                description="Internal login lockout alert could not be sent.",
                metadata={
                    **metadata,
                    "locked_until": attempt.locked_until.isoformat(),
                    "error": f"{type(exc).__name__}",
                },
            )
        else:
            if alert_sent:
                audit.record_system_audit_event(
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

    ip_address = audit._request_ip_address(request) or "unknown"
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

    subject = "Confirm your DOH–CHD CALABARZON Recruitment Portal internal email address"
    body = (
        "The DOH–CHD CALABARZON Recruitment Portal received a request to use this email address for an internal account.\n\n"
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
        ip_address=audit._request_ip_address(request),
        user_agent=audit._request_user_agent(request),
    )
    change_request.full_clean()
    change_request.save()
    _send_internal_email_change_verification(change_request, request)
    audit.record_system_audit_event(
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
        audit.record_system_audit_event(
            actor=None,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed.",
            metadata={
                "reason": "token_not_found",
                "ip_address": audit._request_ip_address(request),
                "user_agent": audit._request_user_agent(request),
            },
        )
        raise ValueError(generic_error) from exc

    if change_request.is_used or change_request.verified_at or change_request.is_expired:
        change_request.is_used = True
        change_request.save(update_fields=["is_used", "updated_at"])
        audit.record_system_audit_event(
            actor=change_request.requested_by,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed.",
            metadata={
                "target_user_id": change_request.user_id,
                "target_username": change_request.user.username,
                "reason": "used_or_expired",
                "ip_address": audit._request_ip_address(request),
                "user_agent": audit._request_user_agent(request),
            },
        )
        raise ValueError(generic_error)

    if RecruitmentUser.objects.filter(
        email__iexact=change_request.new_email,
        role__in=RecruitmentUser.internal_roles(),
    ).exclude(pk=change_request.user_id).exists():
        change_request.is_used = True
        change_request.save(update_fields=["is_used", "updated_at"])
        audit.record_system_audit_event(
            actor=change_request.requested_by,
            action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED,
            description="Internal email change verification failed because the email is no longer unique.",
            metadata={
                "target_user_id": change_request.user_id,
                "target_username": change_request.user.username,
                "reason": "email_conflict",
                "ip_address": audit._request_ip_address(request),
                "user_agent": audit._request_user_agent(request),
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
    audit.record_system_audit_event(
        actor=change_request.requested_by,
        action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED,
        description="Verified and applied internal account email change.",
        metadata={
            "target_user_id": user.id,
            "target_username": user.username,
            "old_email": previous_email,
            "new_email": user.email,
            "ip_address": audit._request_ip_address(request),
            "user_agent": audit._request_user_agent(request),
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
    subject = "DOH–CHD CALABARZON Recruitment Portal internal verification code"
    text_body = (
        "DOH–CHD CALABARZON Recruitment Portal internal verification code\n\n"
        f"Your verification code is {otp_code}.\n"
        f"It expires in {settings.INTERNAL_MFA_OTP_VALIDITY_MINUTES} minutes.\n\n"
        "Do not share this code. DOH–CHD CALABARZON staff will never ask for it."
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
        audit.record_system_audit_event(
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
            ip_address=audit._request_ip_address(request),
            user_agent=audit._request_user_agent(request),
        )
        challenge.otp_hash = _hash_internal_mfa_otp(challenge, otp_code)
        challenge.full_clean()
        challenge.save()
        _send_internal_mfa_email(challenge, otp_code)

        audit.record_system_audit_event(
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
            audit.record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_FAILED,
                description="Internal MFA verification failed.",
                metadata={
                    "reason": "challenge_not_found",
                    "ip_address": audit._request_ip_address(request),
                    "user_agent": audit._request_user_agent(request),
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
            audit.record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_FAILED,
                description="Internal MFA verification failed.",
                metadata={
                    "challenge_id": challenge.id,
                    "reason": "challenge_used",
                    "ip_address": audit._request_ip_address(request),
                    "user_agent": audit._request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None and challenge.is_expired:
            challenge.is_used = True
            challenge.save(update_fields=["is_used", "updated_at"])
            audit.record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_EXPIRED,
                description="Internal MFA verification code expired before successful verification.",
                metadata={
                    "challenge_id": challenge.id,
                    "expires_at": challenge.expires_at.isoformat(),
                    "ip_address": audit._request_ip_address(request),
                    "user_agent": audit._request_user_agent(request),
                },
            )
            error_to_raise = generic_error

        if challenge is not None and error_to_raise is None and (
            challenge.attempt_count >= settings.INTERNAL_MFA_MAX_ATTEMPTS
        ):
            challenge.is_used = True
            challenge.save(update_fields=["is_used", "updated_at"])
            audit.record_system_audit_event(
                actor=user,
                action=AuditLog.Action.INTERNAL_MFA_LOCKED,
                description="Internal MFA challenge locked after too many failed attempts.",
                metadata={
                    "challenge_id": challenge.id,
                    "attempt_count": challenge.attempt_count,
                    "ip_address": audit._request_ip_address(request),
                    "user_agent": audit._request_user_agent(request),
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
                audit.record_system_audit_event(
                    actor=user,
                    action=AuditLog.Action.INTERNAL_MFA_FAILED,
                    description="Internal MFA verification failed.",
                    metadata={
                        "challenge_id": challenge.id,
                        "reason": "invalid_code",
                        "attempt_count": challenge.attempt_count,
                        "ip_address": audit._request_ip_address(request),
                        "user_agent": audit._request_user_agent(request),
                    },
                )
                if locked:
                    audit.record_system_audit_event(
                        actor=user,
                        action=AuditLog.Action.INTERNAL_MFA_LOCKED,
                        description="Internal MFA challenge locked after too many failed attempts.",
                        metadata={
                            "challenge_id": challenge.id,
                            "attempt_count": challenge.attempt_count,
                            "ip_address": audit._request_ip_address(request),
                            "user_agent": audit._request_user_agent(request),
                        },
                    )
                error_to_raise = generic_error
            else:
                challenge.verified_at = timezone.now()
                challenge.is_used = True
                challenge.save(update_fields=["verified_at", "is_used", "updated_at"])
                audit.record_system_audit_event(
                    actor=user,
                    action=AuditLog.Action.INTERNAL_MFA_VERIFIED,
                    description="Internal MFA verification completed successfully.",
                    metadata={
                        "challenge_id": challenge.id,
                        "attempt_count": challenge.attempt_count,
                        "verified_at": challenge.verified_at.isoformat(),
                        "ip_address": audit._request_ip_address(request),
                        "user_agent": audit._request_user_agent(request),
                    },
                )

    if error_to_raise is not None:
        raise ValueError(error_to_raise)
    return challenge
