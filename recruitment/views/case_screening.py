"""The case page's Qualification Screening step (Round H): the review form, its completeness
counts and the resubmission-deadline extension. Read by screening_body.html and the screening
section of application_detail.html; built only when the case is on that step.
"""

from ..forms import ResubmissionDeadlineExtensionForm, ScreeningReviewForm
from ..models import ScreeningDocumentReview, ScreeningRecord
from ..services import (
    get_applicant_document_review_items,
    user_can_extend_resubmission_deadline,
    user_can_manage_screening,
)


def build(context, request, application):
    """The screening keys, given the shared context (which holds the screening record)."""
    user = request.user
    section = {}
    section["applicant_document_review_items"] = get_applicant_document_review_items(application)
    screening_record = context["current_screening_record"]
    if user_can_extend_resubmission_deadline(user, application):
        section["resubmission_deadline_extension_form"] = (
            ResubmissionDeadlineExtensionForm(
                current_deadline=screening_record.resubmission_deadline
            )
        )
    if user_can_manage_screening(user, application):
        if screening_record and screening_record.is_finalized:
            section["screening_locked"] = True
        else:
            section["screening_form"] = ScreeningReviewForm(
                instance=screening_record,
                application=application,
            )
    # RD-1: completeness is computed for the banner (never asked), and the
    # flag count drives which decision the reviewer is offered.
    if section.get("screening_form") is not None:
        review_items = section["screening_form"].document_review_fields
        required_items = [i for i in review_items if i["is_required_for_completeness"]]
        flagged = [
            i
            for i in review_items
            if i["field"].value()
            == ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
        ]
        pending = [
            i
            for i in required_items
            if i["field"].value()
            in ScreeningRecord.COMPLETENESS_BLOCKING_DOCUMENT_STATUSES
            or not i["field"].value()
        ]
        section["screening_required_count"] = len(required_items)
        section["screening_flagged_now_count"] = len(flagged)
        section["screening_pending_count"] = len(pending)
        section["screening_documents_complete"] = not pending
        # The QS the verdict is judged against, so step 2 reads as a
        # comparison rather than a floating question.
        section["screening_qualification_reference"] = (
            application.position.qualification_reference or ""
        )
        section["screening_position_reference"] = application.position.position_reference
    return section
