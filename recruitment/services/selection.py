"""Plantilla selection and decisions: deliberation, final selection, return of the
CAR, no appointment, rescission and the final decision.
"""

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    ComparativeAssessmentReportItem,
    CosVacancyDeliberation,
    DeliberationRecord,
    FinalDecision,
    FinalSelection,
    Notification,
    OfficePolicy,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
)
from ..notification_services import (
    queue_non_selected_applicant_notification,
    queue_selection_under_review_notification,
    queue_selected_applicant_notification,
)
from . import (
    access,
    audit,
    car,
    case_notices,
    common,
    completion,
    cos_selection,
    entries,
    intake,
    interview,
    routing,
    workflow_map,
)


DELIBERATION_STAGES_BY_BRANCH = workflow_map.DELIBERATION_STAGES
DELIBERATION_ROLES_BY_BRANCH = {
    branch: {workflow_map.stage_owner(stage)} for branch, stage in DELIBERATION_STAGES_BY_BRANCH.items()
}
FINAL_DECISION_OUTCOME_TO_STATUS = {
    FinalDecision.Outcome.SELECTED: RecruitmentApplication.Status.APPROVED,
    FinalDecision.Outcome.NOT_SELECTED: RecruitmentApplication.Status.REJECTED,
}


def deliberation_is_finalized_for_current_stage(application):
    if application.branch == PositionPosting.Branch.COS:
        # COS uses a per-vacancy deliberation pick (spec §6.3), not a per-application record.
        # SL-02: deliberation is finalized for a case only when a pick actually named THAT
        # case. A candidate a round did not pick — or one who applied after a round closed —
        # has not been deliberated to a conclusion and stays at the deliberation section.
        return bool(cos_selection.get_cos_pick_for_case(getattr(application, "case", None)))
    deliberation_record = get_deliberation_record(application)
    return bool(deliberation_record and deliberation_record.is_finalized)


def _deliberation_snapshot_for_screening(screening_record):
    return {
        "id": screening_record.id,
        "review_stage": screening_record.review_stage,
        "completeness_status": screening_record.completeness_status,
        "qualification_outcome": screening_record.qualification_outcome,
        "education_score": common._decimal_string(screening_record.education_score),
        "training_score": common._decimal_string(screening_record.training_score),
        "experience_score": common._decimal_string(screening_record.experience_score),
        "document_review_score": common._decimal_string(screening_record.document_review_score),
        "document_review_weight_display": screening_record.document_review_weight_display,
        "finalized_at": screening_record.finalized_at.isoformat() if screening_record.finalized_at else "",
    }


def _deliberation_snapshot_for_exam(exam_record):
    return {
        "id": exam_record.id,
        "review_stage": exam_record.review_stage,
        "exam_type": exam_record.exam_type,
        "exam_status": exam_record.exam_status,
        "exam_score": common._decimal_string(exam_record.effective_score),
        "exam_result": exam_record.exam_result,
        "technical_score": common._decimal_string(exam_record.technical_score),
        "technical_result": exam_record.technical_result,
        "general_score": common._decimal_string(exam_record.general_score),
        "general_result": exam_record.general_result,
        "component_summary": exam_record.component_summary,
        "exam_date": exam_record.exam_date.isoformat() if exam_record.exam_date else "",
        "administered_by": exam_record.administered_by,
        "evidence_id": exam_record.evidence_item_id or "",
        "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
        "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
        "finalized_at": exam_record.finalized_at.isoformat() if exam_record.finalized_at else "",
    }


def _deliberation_snapshot_for_interview(application, interview_session):
    average_score = interview._average_interview_rating(interview_session)
    fallback_count = interview.get_interview_fallback_evidence(application, stage=interview_session.review_stage).count()
    return {
        "id": interview_session.id,
        "review_stage": interview_session.review_stage,
        "scheduled_for": interview_session.scheduled_for.isoformat(),
        "rating_count": interview_session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": common._decimal_string(average_score),
        "finalized_at": interview_session.finalized_at.isoformat() if interview_session.finalized_at else "",
    }


def build_deliberation_consolidation(application):
    screening_records = list(
        application.screening_records.filter(is_finalized=True).select_related(
            "reviewed_by",
            "finalized_by",
        ).order_by("created_at")
    )
    exam_records = list(
        application.exam_records.filter(is_finalized=True).select_related(
            "recorded_by",
            "finalized_by",
            "evidence_item",
        ).order_by("created_at")
    )
    interview_sessions = list(
        application.interview_sessions.filter(is_finalized=True).select_related(
            "scheduled_by",
            "finalized_by",
        ).prefetch_related("ratings").order_by("created_at")
    )
    latest_screening = screening_records[-1] if screening_records else None
    latest_exam = exam_records[-1] if exam_records else None
    latest_interview = interview_sessions[-1] if interview_sessions else None
    latest_interview_average = interview._average_interview_rating(latest_interview) if latest_interview else None
    latest_document_review_score = latest_screening.document_review_score if latest_screening else None
    latest_exam_score = latest_exam.effective_score if latest_exam else None
    preliminary_assessment_score = None
    assessment_weight_display = ""
    car_draft = None
    if application.branch == PositionPosting.Branch.PLANTILLA:
        car_draft = car.get_latest_draft_comparative_assessment_report(
            application,
            stage=routing.get_current_review_stage(application),
        )
        preliminary_assessment_score = car._calculate_preliminary_assessment_score(
            application.position,
            latest_document_review_score,
            latest_exam_score,
            latest_interview_average,
        )
        assessment_weight_display = car._assessment_weight_display(application.position)
    return {
        "application_reference": application.reference_number or "",
        "entry_code": application.position.job_code,
        "branch": application.branch,
        "level": application.level,
        "generated_at": timezone.now().isoformat(),
        "assessment_weight_display": assessment_weight_display,
        "car_draft": (
            {
                "id": car_draft.id,
                "version_number": car_draft.version_number,
                "prepared_by": str(car_draft.generated_by) if car_draft.generated_by else "",
                "prepared_by_role": car_draft.generated_by_role,
                "candidate_count": car_draft.items.count(),
                "summary_notes": car_draft.summary_notes,
            }
            if car_draft
            else {}
        ),
        "screening_records": [_deliberation_snapshot_for_screening(item) for item in screening_records],
        "exam_records": [_deliberation_snapshot_for_exam(item) for item in exam_records],
        "interview_sessions": [
            _deliberation_snapshot_for_interview(application, item) for item in interview_sessions
        ],
        "summary": {
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "latest_qualification_outcome": (
                latest_screening.qualification_outcome if latest_screening else ""
            ),
            "latest_document_review_score": common._decimal_string(latest_document_review_score),
            "latest_exam_status": latest_exam.exam_status if latest_exam else "",
            "latest_exam_score": common._decimal_string(latest_exam_score),
            "latest_exam_components": latest_exam.component_summary if latest_exam else "",
            "latest_interview_average": common._decimal_string(latest_interview_average),
            "preliminary_assessment_score": common._decimal_string(preliminary_assessment_score),
        },
    }


def get_deliberation_record(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
    ).filter(review_stage=review_stage).first()


def get_deliberation_records(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
    ).order_by("created_at")


def get_latest_finalized_deliberation_record(application):
    return application.deliberation_records.select_related(
        "recorded_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
        "comparative_assessment_report",
    ).filter(is_finalized=True).order_by("-finalized_at", "-created_at").first()


def user_can_manage_deliberation(user, application):
    # SL-15: nothing may write a per-case DeliberationRecord any more.
    #
    # COS moved to a single per-vacancy pick on the console (spec §6.3). Plantilla dropped
    # the separate in-system deliberation step entirely — the board deliberates off-system
    # and the Secretariat encodes the recommendation on the CAR — so the Plantilla sequence
    # (interview → CAR → actions) can no longer produce a "deliberation" section at all.
    # The predicate was already unreachable for both branches; saying so here means a
    # future stage-sequence change cannot quietly resurrect a second, competing source of
    # the board's recommendation. Existing records stay readable and exportable.
    return False


def _legacy_deliberation_permission_shape(user, application):
    """The pre-SL-15 predicate, kept only to document what the quarantined path required.
    Never called — ``user_can_manage_deliberation`` refuses every branch now."""
    if application.branch == PositionPosting.Branch.COS:
        return False
    current_stage = routing.get_current_review_stage(application)
    expected_stage = DELIBERATION_STAGES_BY_BRANCH.get(application.branch)
    if (
        current_stage != expected_stage
        or routing.get_current_workflow_section(application) != "deliberation"
    ):
        return False
    if user.role not in DELIBERATION_ROLES_BY_BRANCH.get(application.branch, set()):
        return False
    return access.user_can_process_application(user, application)


def get_final_selection_for_entry(recruitment_entry):
    return (
        FinalSelection.objects.select_related(
            "comparative_assessment_report",
            "selected_item",
            "selected_item__recruitment_case",
            "selected_item__recruitment_case__application",
            "selected_application",
            "selected_case",
            "decided_by",
            "recruitment_entry",
        )
        # SL-14: the vacancy's *currently effective* selection. A rescinded row stays as
        # history and must not keep the vacancy frozen — reading it here is what would
        # otherwise block re-selection, sibling reopen, and the AA's own form forever.
        .filter(recruitment_entry=recruitment_entry, is_rescinded=False)
        .order_by("-decided_at", "-created_at")
        .first()
    )


def get_final_selection_for_application(application):
    return get_final_selection_for_entry(application.position)


def get_final_selection_history(recruitment_entry):
    """SL-14: every selection ever recorded for this vacancy, newest first — including the
    rescinded ones, which is the record the correction model exists to preserve."""
    return (
        FinalSelection.objects.select_related(
            "selected_application", "decided_by", "rescinded_by", "supersedes"
        )
        .filter(recruitment_entry=recruitment_entry)
        .order_by("-decided_at", "-created_at")
    )


def get_final_decision_history(application):
    return application.final_decisions.select_related(
        "decided_by",
        "recruitment_case",
        "recruitment_entry",
    ).order_by("-decided_at", "-created_at")


def get_latest_final_decision(application):
    """The decision currently in force for this case, or None.

    SL-14 (COS): a rescinded decision is history, not the case's state — the full history
    (rescinded rows included) stays available through get_final_decision_history. Filtering
    here is what stops a restored case from still announcing "Final Decision Recorded —
    Selected" on a page that is back at the deliberation step awaiting a replacement."""
    return get_final_decision_history(application).filter(is_rescinded=False).first()


def _final_decision_stage_and_role(application):
    if application.branch == PositionPosting.Branch.COS:
        return RecruitmentCase.Stage.HRM_CHIEF_REVIEW, RecruitmentUser.Role.HRM_CHIEF
    return RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW, RecruitmentUser.Role.APPOINTING_AUTHORITY


def user_can_record_final_decision(user, application):
    if application.branch == PositionPosting.Branch.PLANTILLA:
        return False
    current_stage = routing.get_current_review_stage(application)
    expected_stage, expected_role = _final_decision_stage_and_role(application)
    if (
        current_stage != expected_stage
        or routing.get_current_workflow_section(application) != "decision"
    ):
        return False
    if user.role != expected_role:
        return False
    # SL-01: the COS selection is the finalized vacancy deliberation pick. Only the
    # applicant that pick names can be the subject of the selection record — a sibling
    # who lost the deliberation is never a candidate for a second, contradicting outcome.
    if not cos_selection.get_cos_pick_for_case(getattr(application, "case", None)):
        return False
    return access.user_can_process_application(user, application)


def user_can_record_final_selection(user, application):
    if application.branch != PositionPosting.Branch.PLANTILLA:
        return False
    current_stage = routing.get_current_review_stage(application)
    if (
        current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
        or routing.get_current_workflow_section(application) != "decision"
    ):
        return False
    if user.role != RecruitmentUser.Role.APPOINTING_AUTHORITY:
        return False
    if get_final_selection_for_application(application):
        return False
    report = car.get_latest_finalized_comparative_assessment_report(application)
    if not report:
        return False
    return access.user_can_process_application(user, application)


def user_can_record_no_appointment(user, application):
    """The Appointing Authority may close a ready Plantilla CAR without a pick."""
    return (
        user_can_record_final_selection(user, application)
        and get_final_selection_for_entry(application.position) is None
    )


def rescind_cutoff_block_message(application):
    """Return the settled refusal copy when Office Policy closes the rescind door."""
    policy = entries.get_office_policy()
    if policy.rescind_cutoff == OfficePolicy.RescindCutoff.NEVER:
        return ""
    case = getattr(application, "case", None)
    if case and case.current_stage == RecruitmentCase.Stage.CLOSED:
        return common.RESCIND_CUTOFF_MESSAGE
    if policy.rescind_cutoff == OfficePolicy.RescindCutoff.CONTRACT_RECORDED:
        completion_record = completion.get_completion_record(application)
        if completion_record and (completion_record.completion_reference or "").strip():
            return common.RESCIND_CUTOFF_MESSAGE
    return ""


def _void_completion_record_for_rescission(application, reason):
    completion_record = completion.get_completion_record(application)
    if completion_record is None or completion_record.is_voided:
        return completion_record
    completion_record.is_voided = True
    completion_record.voided_at = timezone.now()
    completion_record.void_reason = reason
    completion_record.save(
        update_fields=["is_voided", "voided_at", "void_reason", "updated_at"]
    )
    return completion_record


def _reset_voided_completion_record_for_reselection(application):
    """Reuse the one-to-one row as a blank tracker when the same person is selected again."""
    completion_record = completion.get_completion_record(application)
    if completion_record is None or not completion_record.is_voided:
        return completion_record
    completion_record.requirements.all().delete()
    completion_record.deadline_changes.all().delete()
    completion_record.completion_reference = ""
    completion_record.completion_date = None
    completion_record.deadline = None
    completion_record.announcement_reference = ""
    completion_record.announcement_date = None
    completion_record.remarks = ""
    completion_record.is_voided = False
    completion_record.voided_at = None
    completion_record.void_reason = ""
    completion_record.save()
    return completion_record


def _decision_packet_screening_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "reviewed_by": str(record.reviewed_by) if record.reviewed_by else "",
        "reviewed_by_role": record.reviewed_by_role,
        "completeness_status": record.completeness_status,
        "completeness_status_label": record.get_completeness_status_display(),
        "qualification_outcome": record.qualification_outcome,
        "qualification_outcome_label": record.get_qualification_outcome_display(),
        "education_score": common._decimal_string(record.education_score),
        "training_score": common._decimal_string(record.training_score),
        "experience_score": common._decimal_string(record.experience_score),
        "document_review_score": common._decimal_string(record.document_review_score),
        "document_reviews": [
            {
                "document_key": review.document_key,
                "requirement_title": review.requirement_title,
                "status": review.status,
                "status_label": review.get_status_display(),
                "remarks": review.remarks,
                "is_required": review.is_required,
                "evidence_item_id": review.evidence_item_id,
            }
            for review in record.document_reviews.all()
        ],
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_exam_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "exam_type": record.exam_type,
        "exam_status": record.exam_status,
        "exam_status_label": record.get_exam_status_display(),
        "exam_score": common._decimal_string(record.effective_score),
        "exam_result": record.exam_result,
        "technical_score": common._decimal_string(record.technical_score),
        "technical_result": record.technical_result,
        "general_score": common._decimal_string(record.general_score),
        "general_result": record.general_result,
        "component_summary": record.component_summary,
        "exam_date": record.exam_date.isoformat() if record.exam_date else "",
        "administered_by": record.administered_by,
        "evidence_id": record.evidence_item_id or "",
        "evidence_label": record.evidence_item.label if record.evidence_item_id else "",
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "is_read_only": record.is_finalized,
    }


def _decision_packet_interview_session(application, session):
    average_score = interview._average_interview_rating(session)
    fallback_count = interview.get_interview_fallback_evidence(application, stage=session.review_stage).count()
    return {
        "id": session.id,
        "review_stage": session.review_stage,
        "review_stage_label": session.get_review_stage_display(),
        "scheduled_by": str(session.scheduled_by) if session.scheduled_by else "",
        "scheduled_by_role": session.scheduled_by_role,
        "scheduled_for": session.scheduled_for.isoformat(),
        "location": session.location,
        "rating_count": session.ratings.count(),
        "fallback_count": fallback_count,
        "average_score": common._decimal_string(average_score),
        "finalized_at": session.finalized_at.isoformat() if session.finalized_at else "",
        "is_read_only": session.is_finalized,
    }


def _decision_packet_deliberation_record(record):
    return {
        "id": record.id,
        "review_stage": record.review_stage,
        "review_stage_label": record.get_review_stage_display(),
        "recorded_by": str(record.recorded_by) if record.recorded_by else "",
        "recorded_by_role": record.recorded_by_role,
        "comparative_assessment_report_id": record.comparative_assessment_report_id or "",
        "deliberated_at": record.deliberated_at.isoformat(),
        "recommendation": record.recommendation,
        "decision_support_summary": record.decision_support_summary,
        "quorum_status": record.quorum_status,
        "quorum_status_label": record.get_quorum_status_display(),
        "attendance_notes": record.attendance_notes,
        "ranking_position": record.ranking_position,
        "ranking_notes": record.ranking_notes,
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else "",
        "finalized_screening_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_screening_count",
            0,
        ),
        "finalized_exam_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_exam_count",
            0,
        ),
        "finalized_interview_count": record.consolidated_snapshot.get("summary", {}).get(
            "finalized_interview_count",
            0,
        ),
        "latest_interview_average": record.consolidated_snapshot.get("summary", {}).get(
            "latest_interview_average",
            "",
        ),
        "latest_document_review_score": record.consolidated_snapshot.get("summary", {}).get(
            "latest_document_review_score",
            "",
        ),
        "preliminary_assessment_score": record.consolidated_snapshot.get("summary", {}).get(
            "preliminary_assessment_score",
            "",
        ),
        "assessment_weight_display": record.consolidated_snapshot.get("assessment_weight_display", ""),
        "is_read_only": record.is_finalized,
    }


def _decision_packet_car_item(item):
    application = item.application
    return {
        "id": item.id,
        "rank_order": item.rank_order,
        "recruitment_case_id": item.recruitment_case_id,
        "application_id": application.id,
        "application_reference": application.reference_number or "",
        "applicant_name": application.applicant_display_name,
        "qualification_outcome": item.qualification_outcome,
        "document_review_score": common._decimal_string(item.document_review_score),
        "exam_status": item.exam_status,
        "exam_score": common._decimal_string(item.exam_score),
        "interview_average_score": common._decimal_string(item.interview_average_score),
        "assessment_score": common._decimal_string(item.assessment_score),
        "preliminary_rank_order": item.preliminary_rank_order,
        "recommendation": item.recommendation,
        "decision_support_summary": item.decision_support_summary,
        "ranking_notes": item.ranking_notes,
    }


def _decision_packet_car_report(report):
    items = list(car.get_comparative_assessment_report_items_for_report(report))
    evidence = report.evidence_item
    return {
        "id": report.id,
        "review_stage": report.review_stage,
        "review_stage_label": report.get_review_stage_display(),
        "generated_by": str(report.generated_by) if report.generated_by else "",
        "generated_by_role": report.generated_by_role,
        "prepared_by": str(report.generated_by) if report.generated_by else "",
        "prepared_by_role": report.generated_by_role,
        "summary_notes": report.summary_notes,
        "version_number": report.version_number,
        "candidate_count": len(items),
        "assessment_weight_display": report.consolidated_snapshot.get("assessment_weight_display", ""),
        "finalized_at": report.finalized_at.isoformat() if report.finalized_at else "",
        "is_returned": report.is_returned,
        "returned_at": report.returned_at.isoformat() if report.returned_at else "",
        "returned_by": str(report.returned_by) if report.returned_by else "",
        "returned_by_role": report.returned_by_role,
        "return_reason": report.return_reason,
        "evidence_item": (
            {
                "id": evidence.id,
                "label": evidence.label,
                "artifact_scope": evidence.artifact_scope,
                "original_filename": evidence.original_filename,
                "sha256_digest": evidence.sha256_digest,
            }
            if evidence
            else {}
        ),
        "items": [_decision_packet_car_item(item) for item in items],
        "is_read_only": report.is_finalized,
    }


def build_car_selection_packet(report):
    return {
        "context": {
            "built_at": timezone.now().isoformat(),
            "recruitment_entry_id": report.recruitment_entry_id,
            "recruitment_entry_code": report.recruitment_entry.job_code,
            "recruitment_entry_title": report.recruitment_entry.title,
            "branch": report.recruitment_entry.branch,
            "branch_label": report.recruitment_entry.get_branch_display(),
            "level": report.recruitment_entry.level,
            "level_label": report.recruitment_entry.get_level_display(),
            "review_stage": report.review_stage,
            "review_stage_label": report.get_review_stage_display(),
        },
        "comparative_assessment_report": _decision_packet_car_report(report),
    }


def _decision_packet_evidence_reference(evidence):
    application = evidence.owning_application
    recruitment_case = evidence.owning_case
    recruitment_entry = evidence.owning_recruitment_entry
    return {
        "id": evidence.id,
        "label": evidence.label,
        "artifact_scope": evidence.artifact_scope,
        "artifact_scope_label": evidence.get_artifact_scope_display(),
        "artifact_type": evidence.artifact_type,
        "application_id": application.id if application else None,
        "recruitment_case_id": recruitment_case.id if recruitment_case else None,
        "recruitment_entry_id": recruitment_entry.id if recruitment_entry else None,
        "stage": evidence.stage,
        "stage_label": evidence.get_stage_display() if evidence.stage else "",
        "document_key": evidence.document_key,
        "version_family": str(evidence.version_family),
        "version_number": evidence.version_number,
        "is_current_version": evidence.is_current_version,
        "is_archived": evidence.is_archived,
        "archive_tag": evidence.archive_tag,
        "original_filename": evidence.original_filename,
        "uploaded_by_role": evidence.uploaded_by_role,
        "uploaded_at": evidence.created_at.isoformat(),
        "digest_algorithm": evidence.digest_algorithm,
        "sha256_digest": evidence.sha256_digest,
    }


def _finalized_deliberation_queryset_for_entry(recruitment_entry, review_stage):
    return DeliberationRecord.objects.filter(
        recruitment_entry=recruitment_entry,
        review_stage=review_stage,
        is_finalized=True,
    ).select_related(
        "application",
        "recruitment_case",
        "recruitment_entry",
        "recorded_by",
        "finalized_by",
        "comparative_assessment_report",
    ).order_by("ranking_position", "application__reference_number")


@transaction.atomic
def save_deliberation_record(
    application,
    actor,
    cleaned_data,
    finalize=False,
    allow_partial=False,
    record_audit=True,
):
    # SL-15: the per-case deliberation record is quarantined. Plantilla's board recommendation
    # is encoded on the CAR and COS uses the per-vacancy pick, so a second write path here
    # would be a competing source of the same official fact — the audit found the route, view,
    # service and model still live behind a section the sequence can no longer produce, one
    # permission change away from reintroducing exactly that. Existing records stay readable.
    raise ValueError(
        "Per-case deliberation records are no longer recorded. Plantilla's board "
        "recommendation is recorded on the Comparative Assessment Report, and COS uses the "
        "per-vacancy deliberation pick."
    )


FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES = frozenset(
    {
        RecruitmentApplication.Status.WITHDRAWN,
        RecruitmentApplication.Status.VACANCY_CANCELLED,
    }
)


def _car_recommended_case_ids_before_contention(report):
    """The recommendation encoded by the finalized CAR, without promoting lower ranks."""
    if report.recommends_none:
        return set()
    if report.recommended_case_id:
        return {report.recommended_case_id}
    return car.car_top_rank_case_ids(report)


def final_selection_recommended_case_ids(report):
    """Recommended case IDs that remain in contention for the Authority's selection."""
    recommended_case_ids = _car_recommended_case_ids_before_contention(report)
    if not recommended_case_ids:
        return set()
    unavailable_case_ids = set(
        report.items.filter(
            recruitment_case_id__in=recommended_case_ids,
            recruitment_case__application__status__in=FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES,
        ).values_list("recruitment_case_id", flat=True)
    )
    return recommended_case_ids - unavailable_case_ids


def _recommended_candidate_had_withdrawn(report):
    if report.recommends_none:
        return False
    recommended_case_ids = _car_recommended_case_ids_before_contention(report)
    return bool(
        recommended_case_ids
        and report.items.filter(
            recruitment_case_id__in=recommended_case_ids,
            recruitment_case__application__status=RecruitmentApplication.Status.WITHDRAWN,
        ).exists()
    )


def _final_selection_items_are_ready(report):
    items = list(car.get_comparative_assessment_report_items_for_report(report))
    contenders = [
        item
        for item in items
        if item.application.status not in FINAL_SELECTION_OUT_OF_CONTENTION_STATUSES
    ]
    if not contenders:
        raise ValueError("The finalized CAR has no ranked applicants still in the running.")
    for item in contenders:
        application = item.application
        case = getattr(application, "case", None)
        if (
            not case
            or application.status != RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW
            or application.current_handler_role != RecruitmentUser.Role.APPOINTING_AUTHORITY
            or case.current_stage != RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW
            or case.current_handler_role != RecruitmentUser.Role.APPOINTING_AUTHORITY
            or case.is_stage_locked
        ):
            raise ValueError(
                "All CAR applicants must be ready for Appointing Authority final selection."
            )
    return contenders


@transaction.atomic
def return_car_for_reassessment(application, actor, remarks):
    remarks = (remarks or "").strip()
    if not remarks:
        raise ValueError("Record the reason for returning the CAR.")
    if application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError("CAR correction returns are only available for Plantilla vacancies.")
    if actor.role != RecruitmentUser.Role.APPOINTING_AUTHORITY:
        raise ValueError("Only the Appointing Authority may return the CAR for correction.")
    if not access.user_can_process_application(actor, application):
        raise ValueError("This case is not currently assigned to you.")
    if routing.get_current_workflow_section(application) != "decision":
        raise ValueError(routing._workflow_progress_block_message(application))
    if get_final_selection_for_entry(application.position):
        raise ValueError("Final selection has already been recorded for this vacancy.")

    report = car.get_latest_finalized_comparative_assessment_report(application)
    if not report:
        raise ValueError("Finalize the CAR before returning it for correction.")

    report_items = _final_selection_items_are_ready(report)
    report.is_returned = True
    report.returned_by = actor
    report.returned_at = timezone.now()
    report.return_reason = remarks
    report.full_clean()
    report.save(
        update_fields=[
            "is_returned",
            "returned_by",
            "returned_by_role",
            "returned_at",
            "return_reason",
            "updated_at",
        ]
    )

    for item in report_items:
        target_application = item.application
        previous_role = target_application.current_handler_role
        previous_status = target_application.status
        deliberation_record = item.deliberation_record
        if deliberation_record:
            deliberation_record.is_finalized = False
            deliberation_record.finalized_by = None
            deliberation_record.finalized_at = None
            deliberation_record.finalized_by_role = ""
            deliberation_record.full_clean()
            deliberation_record.save(
                update_fields=[
                    "is_finalized",
                    "finalized_by",
                    "finalized_at",
                    "finalized_by_role",
                    "updated_at",
                ]
            )

        case_transition = routing.transition_case(
            target_application,
            actor,
            to_status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            to_role=RecruitmentUser.Role.HRMPSB_MEMBER,
            route_type=RoutingHistory.RouteType.REOPEN,
            description="Sent back by the Appointing Authority to correct the ETE ratings and the recommendation.",
            notes=remarks,
        )

        audit.record_audit_event(
            application=target_application,
            actor=actor,
            action=AuditLog.Action.CAR_RETURNED,
            description=(
                "Appointing Authority returned the CAR for ETE and recommendation correction."
            ),
            metadata={
                "car_report_id": report.id,
                "car_version_number": report.version_number,
                "car_item_id": item.id,
                "return_reason": remarks,
                "deliberation_record_id": deliberation_record.id if deliberation_record else "",
                "from_status": previous_status,
                "to_status": target_application.status,
                "from_role": previous_role,
                "to_role": target_application.current_handler_role,
                "from_stage": case_transition["previous_stage"],
                "to_stage": target_application.case.current_stage,
                "from_case_status": case_transition["previous_case_status"],
                "to_case_status": target_application.case.case_status,
            },
        )
        case_notices._emit_case_assignment_notification(
            target_application,
            actor,
            RecruitmentUser.Role.HRMPSB_MEMBER,
            kind=Notification.Kind.CASE_RETURNED,
            title=f"{target_application.reference_label} returned to you for HRMPSB Review",
            body=(
                "The CAR was returned to correct the ETE ratings and recommendation. "
                "Finalized exam and interview scores remain locked."
            ),
            tab="deliberation",
        )

    return report


def _close_car_item_as_non_selected(
    item,
    actor,
    decision_notes,
    *,
    final_selection=None,
    selected_item=None,
    recommended_candidate_withdrawn=False,
    description,
    routing_description,
    extra_metadata=None,
):
    """Close one in-contention CAR item with the canonical non-selected effects."""
    target_application = item.application
    previous_role = target_application.current_handler_role
    previous_status = target_application.status
    case_transition = routing.transition_case(
        target_application,
        actor,
        to_status=RecruitmentApplication.Status.REJECTED,
        to_role="",
        route_type=RoutingHistory.RouteType.CLOSE,
        description=routing_description,
        notes=decision_notes,
    )

    metadata = {
        "final_selection_id": final_selection.id if final_selection else "",
        "car_report_id": item.report_id,
        "selected_car_item_id": selected_item.id if selected_item else "",
        "car_item_id": item.id,
        "decision_outcome": FinalDecision.Outcome.NOT_SELECTED,
        "decision_notes": decision_notes,
        "is_deep_selection": bool(final_selection and final_selection.is_deep_selection),
        "deep_selection_justification_recorded": bool(
            final_selection and final_selection.deep_selection_justification
        ),
        "recommended_candidate_withdrawn": recommended_candidate_withdrawn,
        "from_status": previous_status,
        "to_status": target_application.status,
        "from_role": previous_role,
        "to_role": target_application.current_handler_role,
        "from_stage": case_transition["previous_stage"],
        "to_stage": target_application.case.current_stage,
        "from_case_status": case_transition["previous_case_status"],
        "to_case_status": target_application.case.case_status,
        "case_locked": target_application.case.is_stage_locked,
    }
    metadata.update(extra_metadata or {})
    audit.record_audit_event(
        application=target_application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=description,
        metadata=metadata,
    )
    queue_non_selected_applicant_notification(target_application, actor=actor)
    return target_application


@transaction.atomic
@common.locks_workflow_row("vacancy_of_application")
def record_no_appointment(application, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why no appointment will be made from this CAR.")
    if get_final_selection_for_entry(application.position):
        raise ValueError("Final selection has already been recorded for this vacancy.")
    if not user_can_record_no_appointment(actor, application):
        raise ValueError(
            "Only the Appointing Authority may record no appointment from a ready finalized CAR."
        )
    report = car.get_latest_finalized_comparative_assessment_report(application)
    if not report:
        raise ValueError("Finalize the CAR before recording no appointment.")

    report_items = _final_selection_items_are_ready(report)
    item_ids = [item.id for item in report_items]
    for item in report_items:
        _close_car_item_as_non_selected(
            item,
            actor,
            reason,
            description=(
                "Appointing Authority recorded no appointment from the finalized CAR."
            ),
            routing_description=(
                "Closed as not selected after the Appointing Authority recorded "
                "failure of search."
            ),
            extra_metadata={"no_appointment_recorded": True},
        )

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.NO_APPOINTMENT_RECORDED,
        description=(
            f"Recorded no appointment for {report.recruitment_entry.job_code} "
            "after reviewing the finalized CAR."
        ),
        metadata={
            "report_id": report.id,
            "report_version": report.version_number,
            "reason": reason,
            "item_ids": item_ids,
        },
    )
    case_notices.emit_vacancy_decided_panel_notifications(report.recruitment_entry)
    return report


@transaction.atomic
@common.locks_workflow_row("vacancy_of_application")
def record_final_selection(application, actor, cleaned_data):
    if application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError("CAR-based final selection is only available for Plantilla vacancies.")
    if not user_can_record_final_selection(actor, application):
        raise ValueError(
            "Only the Appointing Authority may record final selection from the finalized CAR."
        )

    report = car.get_latest_finalized_comparative_assessment_report(application)
    if not report:
        raise ValueError("Finalize the CAR before recording the final selection.")
    if get_final_selection_for_entry(report.recruitment_entry):
        raise ValueError("Final selection has already been recorded for this vacancy.")

    report_items = _final_selection_items_are_ready(report)
    contender_item_ids = {item.id for item in report_items}
    selected_item = cleaned_data["selected_item"]
    selected_item = (
        ComparativeAssessmentReportItem.objects.select_related(
            "report",
            "recruitment_case",
            "recruitment_case__application",
        )
        .filter(pk=selected_item.pk, report=report)
        .first()
    )
    if not selected_item or selected_item.id not in contender_item_ids:
        raise ValueError("Select an applicant from the finalized CAR.")
    if not car.car_item_is_within_top_five(selected_item) and not cleaned_data.get(
        "is_deep_selection"
    ):
        raise ValueError(
            "Selecting outside the top five requires deep selection documentation."
        )
    if cleaned_data.get("is_deep_selection") and not (
        cleaned_data.get("deep_selection_justification") or ""
    ).strip():
        raise ValueError(
            "Record the reason for the deep selection before finalizing this selection."
        )
    # WF-03: deviating from the board's recommendation (rank #1 fallback when none was
    # recorded) requires a written justification — HARD rule, mirrored in FinalSelection.clean.
    # SL-08: with no recorded recommendation the fallback is the top computed rank, which a
    # tie can share. Picking any member of that band is not a deviation.
    recommended_case_ids = final_selection_recommended_case_ids(report)
    recommended_candidate_withdrawn = _recommended_candidate_had_withdrawn(report)
    if (
        (
            report.recommends_none
            or (
                recommended_case_ids
                and selected_item.recruitment_case_id not in recommended_case_ids
            )
        )
        and not (cleaned_data.get("deep_selection_justification") or "").strip()
    ):
        raise ValueError(
            "This selection differs from the board's recorded recommendation. Record the "
            "reason for the official record."
        )

    car_snapshot = build_car_selection_packet(report)
    # SL-14: if this vacancy was decided before and that decision was rescinded, chain the
    # replacement to it. Without the link the two rows read as unrelated decisions and the
    # record no longer shows that one corrected the other.
    superseded = (
        FinalSelection.objects.filter(
            recruitment_entry=report.recruitment_entry, is_rescinded=True
        )
        .order_by("-rescinded_at", "-id")
        .first()
    )
    selection = FinalSelection(
        comparative_assessment_report=report,
        recruitment_entry=report.recruitment_entry,
        selected_item=selected_item,
        selected_application=selected_item.application,
        selected_case=selected_item.recruitment_case,
        supersedes=superseded,
        decided_by=actor,
        is_deep_selection=cleaned_data.get("is_deep_selection", False),
        deep_selection_justification=cleaned_data.get("deep_selection_justification", ""),
        decision_notes=cleaned_data["decision_notes"],
        car_snapshot=car_snapshot,
    )
    selection.full_clean()
    selection.save()

    for item in report_items:
        target_application = item.application
        selected = item.pk == selected_item.pk
        if not selected:
            _close_car_item_as_non_selected(
                item,
                actor,
                selection.decision_notes,
                final_selection=selection,
                selected_item=selected_item,
                recommended_candidate_withdrawn=recommended_candidate_withdrawn,
                description=(
                    "Appointing Authority selected another applicant from the finalized CAR."
                ),
                routing_description=(
                    "Closed as not selected after Appointing Authority selected from CAR."
                ),
            )
            continue
        next_role = routing._completion_handler_role(target_application) if selected else ""
        next_status = (
            RecruitmentApplication.Status.APPROVED
            if selected
            else RecruitmentApplication.Status.REJECTED
        )
        previous_role = target_application.current_handler_role
        previous_status = target_application.status
        case_transition = routing.transition_case(
            target_application,
            actor,
            to_status=next_status,
            to_role=next_role,
            route_type=RoutingHistory.RouteType.FORWARD,
            description=(
                f"Selected from the CAR. Sent to the {RecruitmentUser.Role(next_role).label} "
                "for completion."
            ),
            notes=selection.decision_notes,
        )

        if selected:
            _reset_voided_completion_record_for_reselection(target_application)

        audit.record_audit_event(
            application=target_application,
            actor=actor,
            action=AuditLog.Action.DECISION_RECORDED,
            description=(
                "Appointing Authority selected this applicant from the finalized CAR."
                if selected
                else "Appointing Authority selected another applicant from the finalized CAR."
            ),
            metadata={
                "final_selection_id": selection.id,
                "car_report_id": report.id,
                "selected_car_item_id": selected_item.id,
                "car_item_id": item.id,
                "decision_outcome": (
                    FinalDecision.Outcome.SELECTED
                    if selected
                    else FinalDecision.Outcome.NOT_SELECTED
                ),
                "decision_notes": selection.decision_notes,
                "is_deep_selection": selection.is_deep_selection,
                "deep_selection_justification_recorded": bool(selection.deep_selection_justification),
                "recommended_candidate_withdrawn": recommended_candidate_withdrawn,
                "from_status": previous_status,
                "to_status": next_status,
                "from_role": previous_role,
                "to_role": next_role,
                "from_stage": case_transition["previous_stage"],
                "to_stage": target_application.case.current_stage,
                "from_case_status": case_transition["previous_case_status"],
                "to_case_status": target_application.case.case_status,
                "case_locked": target_application.case.is_stage_locked,
            },
        )

        if selected:
            audit.record_audit_event(
                application=target_application,
                actor=actor,
                action=AuditLog.Action.ROUTED,
                description=f"Sent to the {RecruitmentUser.Role(next_role).label} for completion.",
                metadata={
                    "status": next_status,
                    "current_handler_role": next_role,
                    **routing._case_timeline_metadata(target_application.case),
                },
            )
            case_notices._emit_case_assignment_notification(
                target_application,
                actor,
                next_role,
                title=f"{target_application.reference_label} is now in your queue for completion",
                body="The applicant was selected and is ready for completion tracking.",
                tab="completion",
            )
            queue_selected_applicant_notification(target_application, actor=actor)
    case_notices.emit_vacancy_decided_panel_notifications(
        report.recruitment_entry, selected_application=selection.selected_application
    )
    return selection


def user_can_rescind_final_selection(user, selection):
    """SL-14: who may rescind a recorded Plantilla selection.

    The Appointing Authority owns the action; Office Policy supplies the cutoff."""
    if selection is None or selection.is_rescinded:
        return False
    if rescind_cutoff_block_message(selection.selected_application):
        return False
    return user.role == RecruitmentUser.Role.APPOINTING_AUTHORITY


@transaction.atomic
def rescind_final_selection(selection, actor, reason, *, reference=""):
    """SL-14: void a recorded final selection so the vacancy can be decided again.

    A selection should be hard to change — but "impossible" turns a mis-click, or the
    selected person withdrawing before appointment, into the official truth forever. The
    original record is never edited or deleted: it is marked rescinded, keeping every value
    it was recorded with, and the vacancy becomes selectable again so a replacement decision
    can be recorded as a new, linked row.

    Rescinding also unwinds what the selection did to the pool: the former winner comes back
    out of completion to the Appointing Authority step, and the siblings it closed as not
    selected are reopened there too, because they are once again in contention."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why this selection is being taken back.")
    if selection.is_rescinded:
        raise ValueError("This selection has already been taken back.")
    if rescind_cutoff_block_message(selection.selected_application):
        raise ValueError(common.RESCIND_CUTOFF_MESSAGE)
    if not user_can_rescind_final_selection(actor, selection):
        raise ValueError(
            "Only the Appointing Authority may take back a recorded final selection."
        )

    entry = selection.recruitment_entry
    report = selection.comparative_assessment_report
    winner = selection.selected_application
    # Restore ONLY the candidates this selection actually closed: the CAR's own ranked
    # candidates. A blanket "every rejected case on the posting" sweep resurrects people
    # whose rejection had nothing to do with the selection — an absent examinee closed
    # terminal after the letter-of-intent decision, a not-qualified screening — straight
    # into the Appointing Authority step of a competition they were never in (E2E finding;
    # the COS rescind was scoped to its round's membership for the same reason).
    report_case_ids = set(report.items.values_list("recruitment_case_id", flat=True))
    closed_siblings = list(
        RecruitmentApplication.objects.select_related("case", "position", "applicant")
        .filter(
            position=entry,
            case__case_status=RecruitmentCase.CaseStatus.REJECTED,
            case__id__in=report_case_ids,
        )
        .exclude(pk=winner.pk)
    )

    selection.is_rescinded = True
    selection.rescinded_at = timezone.now()
    selection.rescinded_by = actor
    selection.rescind_reason = reason
    selection.rescind_reference = reference or ""
    selection.full_clean()
    selection.save()
    voided_completion_record = _void_completion_record_for_rescission(winner, reason)

    restored = []
    for application in [winner, *closed_siblings]:
        case = getattr(application, "case", None)
        if case is None:
            continue
        previous_status = application.status
        previous_case_status = case.case_status
        routing.transition_case(
            application,
            actor,
            to_status=RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW,
            to_role=RecruitmentUser.Role.APPOINTING_AUTHORITY,
            route_type=RoutingHistory.RouteType.REOPEN,
            description="Returned to the Appointing Authority after the selection was taken back.",
            notes=reason,
        )
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
            description=(
                "Returned to the Appointing Authority after the selection was taken back."
            ),
            metadata={
                "final_selection_id": selection.id,
                "reason": reason,
                "reference": selection.rescind_reference,
                "was_selected": application.pk == winner.pk,
                "completion_record_voided": bool(
                    application.pk == winner.pk and voided_completion_record
                ),
                "from_status": previous_status,
                "to_status": application.status,
                "from_case_status": previous_case_status,
                "to_case_status": case.case_status,
            },
        )
        restored.append(application)

    audit.record_system_audit_event(
        actor=actor,
        action=AuditLog.Action.FINAL_SELECTION_RESCINDED,
        description=(
            f"Took back the final selection for {entry.job_code} "
            f"({winner.reference_label})."
        ),
        metadata={
            "entry_id": entry.id,
            "entry_code": entry.job_code,
            "final_selection_id": selection.id,
            "report_id": report.id,
            "previous_selected_application_id": winner.id,
            "reason": reason,
            "reference": selection.rescind_reference,
            "restored_application_ids": sorted(a.id for a in restored),
            "voided_completion_record_id": (
                voided_completion_record.id if voided_completion_record else None
            ),
        },
    )
    # Notify only on the winner's restored case. Emitting once per restored sibling would
    # turn one correction into a vacancy-sized burst of duplicate office alerts.
    case_notices._emit_case_assignment_notification(
        winner,
        actor,
        RecruitmentUser.Role.APPOINTING_AUTHORITY,
        kind=Notification.Kind.CASE_RETURNED,
        title=(
            f"{winner.reference_label} selection taken back — vacancy back for decision"
        ),
        body=reason,
        exclude_user_ids={actor.id},
    )
    for application in restored:
        queue_selection_under_review_notification(application, actor=actor)
    return selection, restored


@transaction.atomic
def record_final_decision(application, actor, cleaned_data):
    if application.branch == PositionPosting.Branch.PLANTILLA:
        raise ValueError("Plantilla final selection must be recorded from the finalized CAR.")
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a final decision can be recorded.")
    # SL-01: the COS selection has exactly one source of truth — the finalized vacancy
    # deliberation pick. This service records that fact; it does not accept a different
    # applicant, and it does not accept a different outcome for the applicant it names.
    # Checked first, ahead of the lock and stage gates, so an attempt to select the wrong
    # person is told exactly why it was refused.
    pick = cos_selection.get_cos_pick_for_case(application.case)
    if pick is None:
        # SL-14 (COS): only a LIVE pick blocks another applicant here. Once a pick is
        # rescinded the vacancy is undecided again, so the replacement must be recordable.
        if CosVacancyDeliberation.objects.filter(
            recruitment_entry=application.position,
            is_finalized=True,
            is_rescinded=False,
        ).exists():
            raise ValueError(
                "This applicant is not the one named in the finalized COS deliberation "
                "pick. The pick is the selection — it cannot be overridden case by case."
            )
        raise ValueError(
            "Finalize the COS vacancy deliberation pick before recording the selection."
        )
    if application.case.is_stage_locked:
        raise ValueError("This case is final and cannot be edited unless the HRM Chief reopens it.")
    expected_stage, expected_role = _final_decision_stage_and_role(application)
    cleaned_data = {
        **cleaned_data,
        "decision_outcome": FinalDecision.Outcome.SELECTED,
        "decision_notes": (
            cleaned_data.get("decision_notes")
            or pick.notes
            or "Selected as this vacancy's COS deliberation pick."
        ),
    }
    if not user_can_record_final_decision(actor, application):
        expected_role_label = RecruitmentUser.Role(expected_role).label
        raise ValueError(
            f"Only the {expected_role_label} may record the final decision at the current step."
        )
    if application.case.current_stage != expected_stage:
        expected_stage_label = RecruitmentCase.Stage(expected_stage).label
        raise ValueError(
            f"Final decisions may only be recorded during the {expected_stage_label} step."
        )

    submission_packet = intake.build_submission_packet(application)
    missing_components = submission_packet["summary"]["missing_components"]
    if missing_components:
        raise ValueError(
            "The submission packet is incomplete for final decision recording. Missing: "
            + "; ".join(missing_components)
            + "."
        )

    decision = FinalDecision(
        application=application,
        recruitment_case=application.case,
        recruitment_entry=application.position,
        review_stage=expected_stage,
        decided_by=actor,
        branch=application.branch,
        level=application.level,
        decision_outcome=cleaned_data["decision_outcome"],
        decision_notes=cleaned_data["decision_notes"],
        submission_packet_snapshot=submission_packet,
    )
    decision.full_clean()
    decision.save()

    if decision.is_selected:
        _reset_voided_completion_record_for_reselection(application)

    next_status = FINAL_DECISION_OUTCOME_TO_STATUS[decision.decision_outcome]
    next_role = routing._completion_handler_role(application) if decision.is_selected else ""
    previous_role = application.current_handler_role
    previous_status = application.status
    case_transition = routing.transition_case(
        application,
        actor,
        to_status=next_status,
        to_role=next_role,
        route_type=(
            RoutingHistory.RouteType.FORWARD if next_role else RoutingHistory.RouteType.CLOSE
        ),
        description=(
            f"Sent to the {RecruitmentUser.Role(next_role).label} for completion."
            if next_role
            else "Closed as not selected by the final decision."
        ),
        notes=decision.decision_notes,
    )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.DECISION_RECORDED,
        description=(
            (
                "COS selection recorded as selected by the HRM Chief."
                if application.branch == PositionPosting.Branch.COS
                else "Final decision recorded as selected by the Appointing Authority."
            )
            if decision.is_selected
            else (
                "COS selection recorded as not selected by the HRM Chief."
                if application.branch == PositionPosting.Branch.COS
                else "Final decision recorded as not selected by the Appointing Authority."
            )
        ),
        metadata={
            "final_decision_id": decision.id,
            "decision_outcome": decision.decision_outcome,
            "decision_notes": decision.decision_notes,
            "from_status": previous_status,
            "to_status": next_status,
            "from_role": previous_role,
            "to_role": next_role,
            "from_stage": case_transition["previous_stage"],
            "to_stage": application.case.current_stage,
            "from_case_status": case_transition["previous_case_status"],
            "to_case_status": application.case.case_status,
            "case_locked": application.case.is_stage_locked,
            "preserved_artifact_ids": submission_packet["preserved_artifact_ids"],
        },
    )
    if next_role:
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.ROUTED,
            description=f"Sent to the {RecruitmentUser.Role(next_role).label} for completion.",
            metadata={
                "status": next_status,
                "current_handler_role": next_role,
                **routing._case_timeline_metadata(application.case),
            },
        )
        case_notices._emit_case_assignment_notification(
            application,
            actor,
            next_role,
            title=f"{application.reference_label} is now in your queue for completion",
            body="The applicant was selected and is ready for completion tracking.",
            tab="completion",
        )
        queue_selected_applicant_notification(application, actor=actor)
    else:
        queue_non_selected_applicant_notification(application, actor=actor)
    return decision
