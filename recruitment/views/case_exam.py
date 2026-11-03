"""The case page's Examination step (Round H): the per-case exam and schedule forms, or the
batch console's read-only summary, or the "batch on hold" notice. Read by exam_body.html,
exam_schedule_card.html, exam_batch_managed_body.html and the exam section of
application_detail.html; built only when the case is on that step.
"""

from django.urls import reverse

from ..forms import ExamRecordForm, ExamScheduleForm
from ..models import RecruitmentUser
from ..services import (
    get_exam_schedule,
    get_notified_exam_sitting,
    user_can_manage_exam,
    vacancy_screening_batch_block_message,
    vacancy_uses_batch_exam,
)


def build(context, request, application):
    """The exam keys, given the shared context (which holds the exam record)."""
    user = request.user
    section = {}
    if user_can_manage_exam(user, application):
        exam_record = context["current_exam_record"]
        # The sitting the applicant was told about, even when it was scheduled at an
        # earlier review stage (the HRM Chief re-checking the Secretariat's exam).
        exam_schedule = get_notified_exam_sitting(application) or get_exam_schedule(application)
        section["current_exam_schedule"] = exam_schedule
        if exam_record and exam_record.is_finalized:
            section["exam_locked"] = True
        elif vacancy_uses_batch_exam(application.position):
            # RD-2: one door. This vacancy's pool is scheduled, scored, and
            # finalized together in the batch console, so the per-case screen
            # is a read-only summary — no exam_form, no schedule form.
            section["exam_batch_managed"] = True
            section["exam_batch_url"] = reverse(
                "vacancy-batch-exam", kwargs={"pk": application.position.pk}
            )
        else:
            section["exam_form"] = ExamRecordForm(
                instance=exam_record,
                application=application,
            )
            section["exam_schedule_form"] = ExamScheduleForm(instance=exam_schedule)
    elif (
        user.role in {RecruitmentUser.Role.SECRETARIAT, RecruitmentUser.Role.HRM_CHIEF}
        # SL-04: only a batch vacancy can be waiting on its batch. A continuous COS case
        # is never on hold for a sibling, so it must not be told that it is.
        and vacancy_uses_batch_exam(application.position)
    ):
        # The case's own screening is done (section is exam) but the vacancy's exam batch is
        # on hold because a sibling still needs a screening decision (§4.3).
        batch_block = vacancy_screening_batch_block_message(application.position)
        if batch_block:
            section["exam_batch_block_message"] = batch_block
    return section
