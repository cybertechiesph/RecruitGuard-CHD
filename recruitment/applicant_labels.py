"""Applicant-facing labels shared by the portal views and the email builders.

These translate internal workflow vocabulary into the plain language shown to
applicants. Kept in a dedicated module so both ``portal_views`` (the public
status pages) and ``notification_services`` (the applicant emails) read one
source of truth without importing each other.
"""

from .models import PositionPosting, RecruitmentApplication


# status -> (applicant-facing label, status variant/tone, applicant-facing description).
# DRAFT maps to None: draft applications are never surfaced to applicants.
APPLICANT_STATUS_LABELS = {
    RecruitmentApplication.Status.DRAFT: None,  # drafts not shown
    RecruitmentApplication.Status.SECRETARIAT_REVIEW: (
        "Under Review",
        "review",
        "Your application is currently being reviewed by our recruitment team. "
        "You will be contacted if additional information is needed.",
    ),
    RecruitmentApplication.Status.HRM_CHIEF_REVIEW: (
        "Under Review",
        "review",
        "Your application is currently being reviewed by our recruitment team.",
    ),
    RecruitmentApplication.Status.HRMPSB_REVIEW: (
        "Under Evaluation",
        "evaluation",
        "Your application is currently being evaluated. "
        "You may be contacted for an examination or interview schedule.",
    ),
    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: (
        "Under Processing",
        "review",
        "Your application is being processed for the next steps. "
        "You will be contacted with further instructions.",
    ),
    RecruitmentApplication.Status.RETURNED_TO_APPLICANT: (
        "Action Required",
        "returned",
        "Your application has been returned. Please check your email for instructions on what to do next.",
    ),
    RecruitmentApplication.Status.APPROVED: (
        "Approved",
        "approved",
        "Congratulations! Your application has been approved. "
        "You will be contacted with further instructions.",
    ),
    RecruitmentApplication.Status.REJECTED: (
        "Not Selected",
        "not-selected",
        "Thank you for your interest. Unfortunately, your application was not selected for this position. "
        "You are welcome to apply for other open positions.",
    ),
    RecruitmentApplication.Status.WITHDRAWN: (
        "Withdrawn",
        "not-selected",
        "This application has been withdrawn.",
    ),
}


def applicant_status_label(application):
    """Plain-language status shown to applicants (e.g. "Under Review")."""
    info = APPLICANT_STATUS_LABELS.get(application.status)
    return info[0] if info else "Received"


def applicant_branch_label(application):
    """Applicant-facing position type: "Permanent (Plantilla)" or "Contract of Service"."""
    position = getattr(application, "position", None)
    branch = getattr(position, "branch", "") if position is not None else ""
    if branch == PositionPosting.Branch.PLANTILLA:
        return "Permanent (Plantilla)"
    return "Contract of Service"
