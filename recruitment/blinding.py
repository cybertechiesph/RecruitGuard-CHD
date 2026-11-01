"""One rule for the HRMPSB blind review (REV-B / RD-5): a panel member sees the candidate
code, never the applicant's name, email address or case number. Every other role sees the
real identity. Pages, dialogs and in-app notices all go through these helpers so a new screen
cannot quietly forget the rule."""

from .models import RecruitmentUser


def is_blind_viewer(user):
    return getattr(user, "role", None) == RecruitmentUser.Role.HRMPSB_MEMBER


def candidate_label(application):
    return f"Candidate {application.candidate_code_label}"


def identity_for(application, user):
    """The name to show for this applicant to this viewer."""
    if is_blind_viewer(user):
        return candidate_label(application)
    return application.applicant_display_name


def case_ref_for(application, user):
    """The case reference to show to this viewer (the candidate code for the panel)."""
    if is_blind_viewer(user):
        return candidate_label(application)
    return application.reference_label


def blind_text(text, application):
    """Replace every identifying detail of `application` in free text with its candidate code."""
    if not text or application is None:
        return text
    label = candidate_label(application)
    for value in (
        application.reference_number,
        application.case_number_short and f"Case No. {application.case_number_short}",
        application.applicant_display_name,
        application.applicant_email,
    ):
        if value:
            text = text.replace(value, label)
    return text
