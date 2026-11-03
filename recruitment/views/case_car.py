"""The case page's CAR and Deliberation steps (Round H): the CAR form, its readiness and ETE
grid, the read-only and in-preparation states, and the deliberation form or COS pick. Read
by deliberation_body.html, deliberation_recorder.html, comparative_assessment.html and the
deliberation/CAR section of application_detail.html; built only when the case is on one of
those two steps (they share the CAR and deliberation records).
"""

from types import SimpleNamespace

from ..forms import ComparativeAssessmentReportForm, DeliberationRecordForm
from ..models import (
    PositionPosting,
    RecruitmentCase,
    RecruitmentUser,
)
from ..services import (
    application_awaiting_car_preparation,
    application_has_finalized_applicant_pool,
    application_requires_finalized_applicant_pool,
    plantilla_car_preparer_role_label,
    get_comparative_assessment_readiness,
    get_current_workflow_section,
    get_car_candidate_fact_rows,
    user_can_follow_level_two_case,
    get_cos_round_pick_for_case,
    get_applicant_pool_finalization_block_message,
    user_can_manage_comparative_assessment_report,
    user_can_manage_deliberation,
)


def build(context, request, application):
    """The CAR and deliberation keys, given the shared context (which holds both records)."""
    user = request.user
    section = {}
    section["car_readiness"] = get_comparative_assessment_readiness(application)
    # The ETE grid lists the draft's rows; before the first draft it lists the pool,
    # so the scores can be typed first and one "Prepare draft CAR" saves and ranks.
    car_ete_rows = context["current_comparative_assessment_report_items"] or [
        SimpleNamespace(
            recruitment_case_id=pool_case.id,
            application=pool_case.application,
            candidate_label=pool_case.application.candidate_code_label,
        )
        for pool_case in section["car_readiness"].get("draft_candidate_cases", [])
    ]
    section["car_ete_rows"] = car_ete_rows
    # SL-08A: the QS and each candidate's screened credentials, so the ETE verdict is
    # made against the record rather than from memory.
    section["car_candidate_facts"] = get_car_candidate_fact_rows(car_ete_rows)
    section["car_requires_deliberation"] = False
    section["car_requires_finalized_deliberation"] = False
    section["car_finalize_block_message"] = section["car_readiness"].get(
        "finalize_block_message",
        "",
    )
    section["car_prepare_block_message"] = section["car_readiness"].get(
        "prepare_block_message",
        "",
    )
    applicant_pool_is_blocked = (
        application_requires_finalized_applicant_pool(application)
        and not application_has_finalized_applicant_pool(application)
    )
    if applicant_pool_is_blocked:
        section["deliberation_requires_vacancy_closure"] = True
        section["deliberation_vacancy_closure_message"] = (
            get_applicant_pool_finalization_block_message(application)
        )
    elif user_can_manage_deliberation(user, application):
        deliberation_record = context["current_deliberation_record"]
        report = context["current_comparative_assessment_report"]
        if deliberation_record and deliberation_record.is_finalized:
            section["deliberation_locked"] = True
        elif (
            application.branch == PositionPosting.Branch.PLANTILLA
            and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
            and not report
        ):
            section["deliberation_requires_car_draft"] = True
        else:
            section["deliberation_form"] = DeliberationRecordForm(
                instance=deliberation_record,
                application=application,
            )
            if (
                application.branch == PositionPosting.Branch.PLANTILLA
                and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
                and report
                and not report.is_finalized
            ):
                if deliberation_record is None:
                    section["car_requires_deliberation"] = True
                elif not deliberation_record.is_finalized:
                    section["car_requires_finalized_deliberation"] = True
    elif (
        application.branch == PositionPosting.Branch.COS
        and application.case.current_stage == RecruitmentCase.Stage.HRM_CHIEF_REVIEW
        and get_current_workflow_section(application) == "deliberation"
    ):
        # COS deliberation is one per-vacancy pick recorded on the console (spec §6.3).
        section["cos_deliberation_section"] = True
        section["cos_deliberation_pick"] = get_cos_round_pick_for_case(application.case)
    if not applicant_pool_is_blocked and user_can_manage_comparative_assessment_report(user, application):
        report = context["current_comparative_assessment_report"]
        if report and report.is_finalized:
            section["car_locked"] = True
        else:
            section["car_form"] = ComparativeAssessmentReportForm(instance=report)
    elif (
        user.role == RecruitmentUser.Role.HRMPSB_MEMBER
        and application.branch == PositionPosting.Branch.PLANTILLA
        and application.case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and get_current_workflow_section(application) == "car"
        and context["current_comparative_assessment_report"]
    ):
        # The HRMPSB board gets a read-only ranked CAR to deliberate off-system (spec §5.4).
        section["car_readonly"] = True
    elif (
        user_can_follow_level_two_case(user, application)
        and get_current_workflow_section(application) == "car"
        and context["current_comparative_assessment_report"]
    ):
        # C2: the HRM Chief follows the Level 2 CAR the Secretariat prepares.
        section["car_readonly"] = True
        section["car_readonly_following"] = True
    # When the CAR card would otherwise be blank for this viewer (e.g. an HRMPSB member whose
    # board is still waiting on the Secretariat/HRM Chief to prepare the CAR), surface a
    # "CAR in preparation" state instead of an empty titlebar. Does not change access.
    section["car_in_preparation"] = (
        not section.get("car_form")
        and not section.get("car_locked")
        and not section.get("car_readonly")
        and application_awaiting_car_preparation(user, application)
    )
    if section["car_in_preparation"]:
        label = plantilla_car_preparer_role_label(application)
        section["car_preparer_role_label"] = label
        section["car_in_preparation_copy"] = (
            "The Comparative Assessment Report for this vacancy is being prepared by the "
            f"{label}. You will be able to record the deliberation once the CAR is finalized."
        )
    return section
