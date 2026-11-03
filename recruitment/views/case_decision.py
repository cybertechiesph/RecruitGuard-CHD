"""The case page's Decision step (Round H): the final selection form over the CAR's candidates,
and the no-appointment form. Read by final_selection_body.html and the decision section of
application_detail.html; built only when the case is on that step. The recorded decision
itself (shown again at Completion) is shared, in case_shared.
"""

from ..forms import FinalSelectionForm, NoAppointmentForm
from ..services import (
    FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES,
    final_selection_recommended_case_ids,
    user_can_record_final_selection,
    user_can_record_no_appointment,
)


def build(context, request, application):
    """The decision keys, given the shared context (which holds the CAR and its rows)."""
    user = request.user
    section = {}
    if user_can_record_final_selection(user, application):
        report = context["current_comparative_assessment_report"]
        selection_items = list(context["current_comparative_assessment_report_items"])
        recommended_case_ids = final_selection_recommended_case_ids(report)
        for item in selection_items:
            item.is_out_of_contention = (
                item.application.status
                in FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES
            )
            item.is_selection_recommended = (
                not report.recommends_none
                and (
                    not recommended_case_ids
                    or item.recruitment_case_id in recommended_case_ids
                )
            )
        section["final_selection_report"] = report
        section["final_selection_items"] = selection_items
        section["final_selection_form"] = FinalSelectionForm(report=report)
        if user_can_record_no_appointment(user, application):
            section["no_appointment_form"] = NoAppointmentForm()
    return section
