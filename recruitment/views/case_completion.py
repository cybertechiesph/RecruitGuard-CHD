"""The case page's Completion step (Round H): the completion tracking form, its requirement
rows and the case closure form. Read by completion_body.html and the completion section of
application_detail.html; built only when the case is on that step. The completion record, the
recorded decision and the CAR summary shown there are shared, in case_shared.
"""

from ..forms import (
    CaseClosureForm,
    CompletionRequirementFormSet,
    CompletionTrackingForm,
    EvidenceUploadForm,
)
from ..models import CompletionRecord
from ..services import user_can_close_case, user_can_manage_completion, user_can_upload_evidence


def build(context, request, application):
    """The completion keys, given the shared context (which holds the completion record)."""
    user = request.user
    section = {}
    if user_can_upload_evidence(user, application):
        section["evidence_form"] = EvidenceUploadForm()
    if user_can_manage_completion(user, application):
        completion_record = context["completion_record"]
        requirement_instance = completion_record or CompletionRecord(
            application=application,
            recruitment_case=application.case,
            tracked_by=user,
        )
        section["completion_form"] = CompletionTrackingForm(
            instance=completion_record,
            application=application,
            actor=user,
        )
        section["completion_requirement_formset"] = CompletionRequirementFormSet(
            instance=requirement_instance,
            prefix="completion_requirements",
        )
    if user_can_close_case(user, application):
        section["closure_form"] = CaseClosureForm()
    return section
