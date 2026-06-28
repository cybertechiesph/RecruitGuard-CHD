"""Shared branding and institutional content for all RecruitGuard-CHD emails.

Every outbound email — applicant notifications, the internal panel notice, and the
two auth OTP messages — pulls its header logo, contact footer, data-privacy line and
do-not-reply line from here so the wording and design stay consistent and the official
DOH–CHD CALABARZON contact details live in exactly one place.

The values below are the office-confirmed canonical text. Do not invent contact details;
if a value is unknown, omit it rather than guessing.
"""

from django.conf import settings

# Static path (relative to STATIC_URL) of the agency seal used in the email header.
EMAIL_LOGO_STATIC_PATH = "img/brand/chd-calabarzon-seal.png"

# --- Office-confirmed institutional contact (Human Resource Management Unit) ---
CONTACT_UNIT = "Human Resource Management Unit, DOH–CHD CALABARZON"
CONTACT_PHONE = "(02) 8249-2000 loc. 4477"
CONTACT_EMAIL = "hrms@ro4a.doh.gov.ph"
OFFICE_NAME = "DOH Regional Office IV-A (CHD CALABARZON)"
OFFICE_ADDRESS = (
    "Quirino Memorial Medical Center compound, Project 4, Quezon City, 1109 Metro Manila"
)

DATA_PRIVACY_NOTICE = (
    "In compliance with the Data Privacy Act of 2012 (R.A. No. 10173), the personal "
    "information you provide is collected and processed solely for this recruitment and "
    "selection process and is handled with strict confidentiality."
)

# Do-not-reply line. Applicants are pointed to the HRM Unit printed in their footer;
# internal staff messages do not carry the HRMU contact block, so they use a generic line.
DO_NOT_REPLY_APPLICANT = (
    "This is a system-generated message from RecruitGuard-CHD. Please do not reply to this "
    "email. For inquiries, contact the Human Resource Management Unit using the details above."
)
DO_NOT_REPLY_INTERNAL = (
    "This is a system-generated message from RecruitGuard-CHD. Please do not reply to this email."
)

# Reporting instructions shown only on the applicant exam-invitation and interview-notice.
REPORTING_INSTRUCTIONS = (
    "Bring at least one (1) valid government-issued identification card.",
    "Report at least fifteen (15) minutes before your scheduled time.",
    "If you are unable to attend on the scheduled date, submit a written letter of intent "
    "to reschedule to the Human Resource Management Unit (details below) before your "
    "scheduled date.",
)


def email_asset_base_url():
    return (getattr(settings, "EMAIL_ASSET_BASE_URL", "") or "").strip().rstrip("/")


def email_logo_url():
    """Absolute URL to the agency seal, or "" when no asset base URL is configured.

    An empty string makes the base template fall back to a text logotype instead of
    rendering a broken image (e.g. on local/dev where no public host is set).
    """
    base = email_asset_base_url()
    if not base:
        return ""
    static_url = (getattr(settings, "STATIC_URL", "static/") or "static/").strip("/")
    return f"{base}/{static_url}/{EMAIL_LOGO_STATIC_PATH}"


def email_portal_url():
    return email_asset_base_url() or (
        getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or ""
    ).strip().rstrip("/")


def _applicant_footer_text_lines():
    lines = [
        "For inquiries:",
        CONTACT_UNIT,
        f"Tel: {CONTACT_PHONE}  |  Email: {CONTACT_EMAIL}",
        f"{OFFICE_NAME}, {OFFICE_ADDRESS}",
    ]
    portal = email_portal_url()
    if portal:
        lines.append(f"Portal: {portal}")
    lines += ["", DATA_PRIVACY_NOTICE, "", DO_NOT_REPLY_APPLICANT]
    return lines


def _internal_footer_text_lines():
    return [OFFICE_NAME, "", DO_NOT_REPLY_INTERNAL]


def email_footer_text_lines(audience="applicant"):
    """Plain-text footer lines mirroring the branded HTML footer.

    Returned without a leading separator; callers prepend a blank line / divider.
    """
    if audience == "internal":
        return _internal_footer_text_lines()
    return _applicant_footer_text_lines()


def email_branding_context(audience="applicant"):
    """Shared context injected into the branded email base template.

    audience:
        "applicant" — full HRM Unit contact footer + data-privacy + do-not-reply.
        "internal"  — office identity + do-not-reply only (no HRMU "for inquiries" framing,
                      no data-privacy line, which applies to applicant personal data).
    """
    context = {
        "logo_url": email_logo_url(),
        "portal_url": email_portal_url(),
        "office_name": OFFICE_NAME,
        "office_address": OFFICE_ADDRESS,
        "email_audience": audience,
    }
    if audience == "internal":
        context["do_not_reply_notice"] = DO_NOT_REPLY_INTERNAL
    else:
        context.update(
            {
                "contact_unit": CONTACT_UNIT,
                "contact_phone": CONTACT_PHONE,
                "contact_email": CONTACT_EMAIL,
                "data_privacy_notice": DATA_PRIVACY_NOTICE,
                "do_not_reply_notice": DO_NOT_REPLY_APPLICANT,
            }
        )
    return context
