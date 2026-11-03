"""The case page's Interview / Rating step (Round H): the session form, the member's rating
form or sheet, the paper-rating encoder and the absence form, or the batch console's
read-only summary. Read by interview_body.html, interview_session_manager.html,
interview_rater.html, interview_fallback_card.html, interview_batch_managed_body.html and the
interview section of application_detail.html; built only when the case is on that step.
"""

from django.urls import reverse

from ..forms import (
    InterviewFallbackUploadForm,
    InterviewRatingForm,
    InterviewSessionForm,
    SimpleInterviewRatingForm,
    InterviewAbsenceForm,
)
from ..models import PositionPosting
from ..services import (
    interview_fallback_panel_queryset,
    vacancy_uses_batch_exam,
    user_can_manage_interview_rating,
    interview_scoring_is_open,
    scoring_opens_message,
    get_published_competency_rating_template,
    user_can_manage_interview_session,
    user_can_upload_interview_fallback,
    user_is_interview_rating_support_encoder,
)


def build(context, request, application):
    """The interview keys, given the shared context (which holds the session and ratings)."""
    user = request.user
    section = {}
    section["interview_rating_is_support_encoding"] = user_is_interview_rating_support_encoder(
        user,
        application,
    )
    interview_is_batch_managed = vacancy_uses_batch_exam(application.position)
    if interview_is_batch_managed:
        section["interview_batch_url"] = reverse(
            "vacancy-batch-interview", kwargs={"pk": application.position.pk}
        )
    if user_can_manage_interview_session(user, application):
        interview_session = context["current_interview_session"]
        if interview_session and interview_session.is_finalized:
            section["interview_session_locked"] = True
        elif interview_is_batch_managed:
            # RD-4: one door. This vacancy's pool is scheduled and finalized
            # together in the batch interview console, so the per-case screen
            # is a read-only summary — no editable session form.
            section["interview_session_batch_managed"] = True
        else:
            section["interview_session_form"] = InterviewSessionForm(instance=interview_session)
    if user_can_manage_interview_rating(user, application):
        interview_session = context["current_interview_session"]
        if not interview_session:
            section["interview_rating_requires_session"] = True
        elif interview_session.is_finalized:
            section["interview_rating_locked"] = True
        elif not interview_scoring_is_open(interview_session):
            section["interview_rating_opens_message"] = scoring_opens_message(
                interview_session.scheduled_for
            )
        elif application.branch == PositionPosting.Branch.COS:
            # COS interviews are scored with a single end-user / HRMS number,
            # not the competency rating sheet (spec §6.2).
            if interview_is_batch_managed:
                # RD-4: a pooled COS single score is recorded in the console.
                section["interview_rating_batch_managed"] = True
            else:
                existing_rating = context["current_user_interview_rating"]
                section["interview_rating_is_simple"] = True
                section["interview_rating_form"] = SimpleInterviewRatingForm(
                    initial={
                        "rating_score": existing_rating.rating_score if existing_rating else None,
                        "rating_notes": existing_rating.rating_notes if existing_rating else "",
                        "justification": existing_rating.justification if existing_rating else "",
                    }
                )
        else:
            rating_template = get_published_competency_rating_template(application.position)
            if rating_template is None:
                section["interview_rating_requires_sheet"] = True
            else:
                section["interview_rating_template"] = rating_template
                section["interview_rating_form"] = InterviewRatingForm(
                    instance=context["current_user_interview_rating"],
                    application=application,
                    actor=user,
                    template=rating_template,
                )
    if user_can_upload_interview_fallback(user, application):
        interview_session = context["current_interview_session"]
        if not interview_session:
            section["interview_fallback_requires_session"] = True
        elif interview_session.is_finalized:
            section["interview_fallback_locked"] = True
        else:
            # SL-03: the encoder picks the member whose sheet they are encoding, from
            # the panel that was actually invited to this interview.
            section["interview_fallback_form"] = InterviewFallbackUploadForm(
                panel_members=interview_fallback_panel_queryset(application),
            )
        if (
            interview_session
            and not interview_session.is_finalized
            # A pooled interview records absence on the batch console, so the per-case
            # screen stays the read-only summary the one-door rule intends.
            and not vacancy_uses_batch_exam(application.position)
            and user_can_manage_interview_session(user, application)
        ):
            # SL-06: recording a no-show must be as available as recording a score.
            section["interview_absence_form"] = InterviewAbsenceForm()
    return section
