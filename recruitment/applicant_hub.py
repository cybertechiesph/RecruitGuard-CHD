"""'My applications': one place per email address for every application made with it.

The applicant proves they own the email with a 6-digit code (as everywhere else on the
portal), then sees each draft and submitted application with a way to continue or check its
status. Tracking codes and per-application links become a fallback instead of the only key.

Privacy: the request page answers the same way whether or not the email has applications,
and a code is only emailed to an address that has at least one, so the page can't be used
to find out who applied or to send mail to strangers.
"""

import hashlib
import hmac
import logging
import secrets
import smtplib
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext

from .email_branding import email_branding_context
from .models import ApplicantEmailCode, RecruitmentApplication
from .services import normalize_applicant_email

logger = logging.getLogger(__name__)


def applications_for_email(email):
    """Every application (draft or submitted) made with this email, newest first."""
    email = normalize_applicant_email(email)
    return list(
        RecruitmentApplication.objects.select_related("position", "case")
        .filter(Q(applicant_email__iexact=email) | Q(applicant__email__iexact=email))
        .distinct()
        .order_by("-updated_at", "-pk")
    )


def _hash(email, code):
    payload = f"hub|{email}|{code}|{settings.APPLICATION_OTP_HASH_SECRET}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def issue_hub_code(email):
    """Email a code if this address has applications. Returns True if one was sent.
    Raises ValueError (with a message for the applicant) while the resend cooldown runs."""
    email = normalize_applicant_email(email)
    if not applications_for_email(email):
        return False
    record, _created = ApplicantEmailCode.objects.get_or_create(email=email)
    now = timezone.now()
    cooldown = settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS
    if record.requested_at and (now - record.requested_at).total_seconds() < cooldown:
        remaining = max(1, int(cooldown - (now - record.requested_at).total_seconds()))
        raise ValueError(
            gettext("Please wait %(seconds)s seconds before requesting another code.") % {"seconds": remaining}
        )
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    record.code_hash = _hash(email, code)
    record.expires_at = now + timedelta(minutes=settings.APPLICATION_OTP_VALIDITY_MINUTES)
    record.requested_at = now
    record.attempt_count = 0
    record.save()
    _send_hub_code(email, code)
    return True


def _send_hub_code(email, code):
    minutes = settings.APPLICATION_OTP_VALIDITY_MINUTES
    subject = gettext("Your code to see your applications")
    text_body = gettext(
        "Your code is %(code)s. It works for %(minutes)s minutes.\n\n"
        "Enter it on the My applications page to see every application you made with this email.\n\n"
        "Do not share this code. DOH–CHD CALABARZON staff will never ask for it."
    ) % {"code": code, "minutes": minutes}
    try:
        html_body = render_to_string(
            "email/applicant_hub_code.html",
            {"otp_code": code, "otp_validity_minutes": minutes, **email_branding_context("applicant")},
        )
    except Exception:
        logger.exception("Failed to render the My applications code email.")
        html_body = ""
    message = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [email])
    if html_body:
        message.attach_alternative(html_body, "text/html")
    try:
        message.send(fail_silently=False)
    except (OSError, smtplib.SMTPException):
        logger.exception("My applications code email failed.")
        raise ValueError(gettext("We could not send a code right now. Please try again in a few moments."))


def verify_hub_code(email, code):
    """Check a code for this email. Raises ValueError with an applicant-facing message."""
    email = normalize_applicant_email(email)
    record = ApplicantEmailCode.objects.filter(email=email).first()
    if record is None or not record.code_hash or not record.expires_at:
        raise ValueError(gettext("Request a verification code first."))
    if record.expires_at < timezone.now():
        raise ValueError(gettext("The verification code has expired. Request a new code."))
    max_attempts = settings.APPLICATION_OTP_MAX_ATTEMPTS
    if record.attempt_count >= max_attempts:
        raise ValueError(gettext("Too many invalid verification attempts. Request a new code."))
    if not hmac.compare_digest(record.code_hash, _hash(email, (code or "").strip())):
        record.attempt_count += 1
        record.save(update_fields=["attempt_count", "updated_at"])
        if record.attempt_count >= max_attempts:
            raise ValueError(gettext("Too many invalid verification attempts. Request a new code."))
        raise ValueError(gettext("The verification code is invalid."))
    record.code_hash = ""
    record.attempt_count = 0
    record.save(update_fields=["code_hash", "attempt_count", "updated_at"])
    return email
