"""Applicant-facing labels shared by the portal views and the email builders.

These translate internal workflow vocabulary into the plain language shown to
applicants. Kept in a dedicated module so both ``portal_views`` (the public
status pages) and ``notification_services`` (the applicant emails) read one
source of truth without importing each other.
"""

from django.utils.translation import gettext_lazy as _

from .models import PositionPosting, RecruitmentApplication


# status -> (applicant-facing label, status variant/tone, applicant-facing description).
# DRAFT maps to None: draft applications are never surfaced to applicants.
APPLICANT_STATUS_LABELS = {
    RecruitmentApplication.Status.DRAFT: None,  # drafts not shown
    RecruitmentApplication.Status.SECRETARIAT_REVIEW: (
        _("Under Review"),
        "review",
        _(
            "Your application is currently being reviewed by our recruitment team. "
            "You will be contacted if additional information is needed."
        ),
    ),
    RecruitmentApplication.Status.HRM_CHIEF_REVIEW: (
        _("Under Review"),
        "review",
        _("Your application is currently being reviewed by our recruitment team."),
    ),
    RecruitmentApplication.Status.HRMPSB_REVIEW: (
        _("Under Evaluation"),
        "evaluation",
        _(
            "Your application is currently being evaluated. "
            "You may be contacted for an examination or interview schedule."
        ),
    ),
    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: (
        _("Under Processing"),
        "review",
        _(
            "Your application is being processed for the next steps. "
            "You will be contacted with further instructions."
        ),
    ),
    RecruitmentApplication.Status.RETURNED_TO_APPLICANT: (
        _("Action Required"),
        "returned",
        _("Something in your application needs your action. See “What you need to do” below."),
    ),
    RecruitmentApplication.Status.APPROVED: (
        _("Approved"),
        "approved",
        _(
            "Congratulations! Your application has been approved. "
            "You will be contacted with further instructions."
        ),
    ),
    RecruitmentApplication.Status.REJECTED: (
        _("Not Selected"),
        "not-selected",
        _(
            "Thank you for your interest. Unfortunately, your application was not selected for this position. "
            "You are welcome to apply for other open positions."
        ),
    ),
    # SL-10: the applicant left of their own accord — the status page should not present
    # that with the same tone as a decision that went against them.
    # SL-10: the applicant left of their own accord, so the label and the wording say that
    # rather than implying a decision went against them. The variant stays "not-selected"
    # because it only drives the status page's terminal-step styling, which is correct here.
    RecruitmentApplication.Status.WITHDRAWN: (
        _("Withdrawn"),
        "not-selected",
        _(
            "You withdrew this application and the office has recorded it. "
            "No further action is needed."
        ),
    ),
    # SL-11: the vacancy was cancelled. Nothing about the applicant was decided.
    RecruitmentApplication.Status.VACANCY_CANCELLED: (
        _("Vacancy Cancelled"),
        "not-selected",
        _(
            "The office cancelled this recruitment, so your application will not proceed. "
            "This is not an assessment of your qualifications."
        ),
    ),
}


def applicant_status_label(application):
    """Plain-language status shown to applicants (e.g. "Under Review")."""
    info = APPLICANT_STATUS_LABELS.get(application.status)
    return str(info[0] if info else _("Received"))


def applicant_branch_label(application):
    """Applicant-facing position type: "Permanent (Plantilla)" or "Contract of Service"."""
    position = getattr(application, "position", None)
    branch = getattr(position, "branch", "") if position is not None else ""
    if branch == PositionPosting.Branch.PLANTILLA:
        return "Permanent (Plantilla)"
    return "Contract of Service"
