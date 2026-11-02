"""Reopening a case and correcting a stage record (screening, exam, interview)
with its version trail.
"""

from datetime import date, datetime, time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    FinalDecision,
    Notification,
    RecruitmentUser,
    RoutingHistory,
    StageRecordVersion,
)
from . import audit, case_notices, exam, interview, routing, screening, selection


CASE_REOPEN_ROLES = {
    RecruitmentUser.Role.HRM_CHIEF,
}


def user_can_reopen_case(user, case):
    return bool(
        case
        and user.role in CASE_REOPEN_ROLES
        and case.is_stage_locked
        and case.locked_stage
    )


def _stage_record_value(value):
    """JSON-safe copy of one stored field value."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _stage_record_field_snapshot(record, field_names):
    return {name: _stage_record_value(getattr(record, name)) for name in field_names}


_SCREENING_SNAPSHOT_FIELDS = (
    "review_stage",
    "reviewed_by_id",
    "reviewed_by_role",
    "completeness_status",
    "completeness_notes",
    "qualification_outcome",
    "education_score",
    "training_score",
    "experience_score",
    "document_review_score",
    "screening_notes",
    "resubmission_requested_at",
    "resubmission_deadline",
    "reopened_for_resubmission_at",
    "is_finalized",
    "finalized_at",
    "finalized_by_id",
    "finalized_by_role",
)

_EXAM_SNAPSHOT_FIELDS = (
    "review_stage",
    "recorded_by_id",
    "recorded_by_role",
    "exam_type",
    "exam_status",
    "exam_score",
    "exam_result",
    "technical_score",
    "technical_result",
    "general_score",
    "general_result",
    "exam_date",
    "administered_by",
    "valid_from",
    "valid_until",
    "exam_notes",
    "evidence_item_id",
    "is_finalized",
    "finalized_at",
    "finalized_by_id",
    "finalized_by_role",
)

_INTERVIEW_SNAPSHOT_FIELDS = (
    "review_stage",
    "scheduled_by_id",
    "scheduled_by_role",
    "scheduled_for",
    "location",
    "session_notes",
    "attendance_outcome",
    "applicant_notified_at",
    "is_finalized",
    "finalized_at",
    "finalized_by_id",
    "finalized_by_role",
)


def _screening_record_snapshot(record):
    """SL-12: the screening decision AND the document rows it was derived from. Archiving
    the header alone would preserve the verdict while losing the evidence review that
    justified it, which is the half an auditor actually asks about."""
    return {
        "record": _stage_record_field_snapshot(record, _SCREENING_SNAPSHOT_FIELDS),
        "document_reviews": [
            {
                "document_key": review.document_key,
                "requirement_title": review.requirement_title,
                "requirement_label": review.requirement_label,
                "status": review.status,
                "remarks": review.remarks,
                "evidence_item_id": review.evidence_item_id,
                "is_required": review.is_required,
                "is_not_applicable": review.is_not_applicable,
            }
            for review in record.document_reviews.all().order_by("display_order", "id")
        ],
    }


def _exam_record_snapshot(record):
    return {"record": _stage_record_field_snapshot(record, _EXAM_SNAPSHOT_FIELDS)}


def _interview_session_snapshot(record):
    """SL-12: the session plus every rater's score, because correcting an interview means
    correcting the ratings, and a panel member's original numbers must stay readable."""
    return {
        "record": _stage_record_field_snapshot(record, _INTERVIEW_SNAPSHOT_FIELDS),
        "ratings": [
            {
                "id": rating.id,
                "rated_by_id": rating.rated_by_id,
                "rated_by_role": rating.rated_by_role,
                "encoded_by_id": rating.encoded_by_id,
                "rating_score": _stage_record_value(rating.rating_score),
                "rating_notes": rating.rating_notes,
                "justification": rating.justification,
                "recorded_at": _stage_record_value(rating.created_at),
            }
            for rating in record.ratings.all().order_by("id")
        ],
    }


_STAGE_RECORD_SNAPSHOT_BUILDERS = {
    StageRecordVersion.RecordType.SCREENING: _screening_record_snapshot,
    StageRecordVersion.RecordType.EXAM: _exam_record_snapshot,
    StageRecordVersion.RecordType.INTERVIEW: _interview_session_snapshot,
}

_STAGE_RECORD_AUDIT_LABELS = {
    StageRecordVersion.RecordType.SCREENING: "screening record",
    StageRecordVersion.RecordType.EXAM: "examination record",
    StageRecordVersion.RecordType.INTERVIEW: "interview session",
}

# The workflow sections that own an editable stage record. Deliberation, CAR, and the
# final decision are excluded: they carry their own versioning and correction paths
# (SL-01/SL-15, the CAR return, SL-14).
_STAGE_RECORD_SECTION_TYPES = {
    "screening": StageRecordVersion.RecordType.SCREENING,
    "exam": StageRecordVersion.RecordType.EXAM,
    "interview": StageRecordVersion.RecordType.INTERVIEW,
}

_STAGE_RECORD_SECTION_GETTERS = {
    "screening": lambda application, stage=None: screening.get_screening_record(application, stage=stage),
    "exam": lambda application, stage=None: exam.get_exam_record(application, stage=stage),
    "interview": lambda application, stage=None: interview.get_interview_session(application, stage=stage),
}


def get_stage_record_versions(application, stage=None):
    """SL-12: the correction trail for this application, newest first."""
    queryset = application.stage_record_versions.select_related("reopened_by")
    if stage:
        queryset = queryset.filter(review_stage=stage)
    return queryset


def get_stage_record_correction(record, record_type):
    """The reopen that produced the record's CURRENT editable values, or None if these are
    still the original decision. Drives the "you are correcting X" banner."""
    if record is None or record.correction_version <= 1:
        return None
    return (
        StageRecordVersion.objects.select_related("reopened_by")
        .filter(
            record_type=record_type,
            record_id=record.id,
            version_number=record.correction_version - 1,
        )
        .first()
    )


@transaction.atomic
def open_stage_record_correction(record, record_type, actor, reason, *, trigger):
    """SL-12: archive a finalized stage record's values, then unlock it for correction.

    Reopening a case has to reopen the decision being corrected, not just the envelope
    around it. This copies the finalized values into an immutable StageRecordVersion —
    with the document rows or rater scores that hang off them — then clears the
    finalization flags on the live row so the assigned handler can actually edit and
    re-finalize it.

    Correcting in place rather than cloning is deliberate: the child rows (document
    reviews, ratings, competency scores) stay attached to the one row every pool query,
    console, and export already reads, so no caller can silently pick the wrong version.
    The values they replace live in the archive.

    Returns the archived version, or None when the record was not finalized (nothing to
    preserve and nothing to unlock)."""
    if record is None or not record.is_finalized:
        return None
    snapshot = _STAGE_RECORD_SNAPSHOT_BUILDERS[record_type](record)
    version = StageRecordVersion(
        application=record.application,
        recruitment_case=record.recruitment_case,
        record_type=record_type,
        review_stage=record.review_stage,
        record_id=record.id,
        version_number=record.correction_version,
        trigger=trigger,
        reopened_by=actor,
        reopen_reason=(reason or "").strip(),
        snapshot=snapshot,
    )
    version.full_clean(exclude=["reopened_by_role"])
    version.save()

    record.correction_version = record.correction_version + 1
    record.is_finalized = False
    record.finalized_at = None
    record.finalized_by = None
    record.finalized_by_role = ""
    record.save(
        update_fields=[
            "correction_version",
            "is_finalized",
            "finalized_at",
            "finalized_by",
            "finalized_by_role",
            "updated_at",
        ]
    )
    audit.record_audit_event(
        application=record.application,
        actor=actor,
        action=AuditLog.Action.STAGE_RECORD_REOPENED,
        description=(
            f"Reopened the finalized {_STAGE_RECORD_AUDIT_LABELS[record_type]} for "
            f"correction; the previous values are preserved as version "
            f"{version.version_number}."
        ),
        metadata={
            "record_type": record_type,
            "record_id": record.id,
            "review_stage": record.review_stage,
            "version_number": version.version_number,
            "trigger": trigger,
            "reason": version.reopen_reason,
        },
    )
    return version


def _assert_reopened_case_has_editable_work(application, stage):
    """SL-12 AC#4: a case may never sit Active and unlocked at a section whose only
    underlying record is finalized and therefore refuses every write.

    This is the invariant the whole finding is about, so it is checked rather than assumed.
    It fires only if the section the case lands on disagrees with the record the reopen
    unlocked — a wiring mistake, caught inside the transaction instead of shipped as a case
    nobody can move."""
    application.refresh_from_db()
    section = routing.get_current_workflow_section(application)
    record_type = _STAGE_RECORD_SECTION_TYPES.get(section)
    if record_type is None:
        return
    record = _STAGE_RECORD_SECTION_GETTERS[section](application, stage=stage)
    if record is not None and record.is_finalized:
        raise ValueError(
            "This case cannot be reopened: its "
            f"{_STAGE_RECORD_AUDIT_LABELS[record_type]} would stay finalized with no way "
            "to correct it."
        )


def stage_record_for_correction(application, stage=None):
    """SL-12: the finalized record a reopen at this stage has to unlock.

    The stage's own section sequence read backwards — the LAST finalized record is the
    decision that terminalized the case, so it is the one the handler came back to change.
    An earlier finalized record in the same stage stays finalized: reopening a case whose
    exam failed is not a licence to silently rewrite its screening."""
    review_stage = stage or routing.get_current_review_stage(application)
    sequence = [
        section
        for section in routing._workflow_detail_sequence(application)
        if section in _STAGE_RECORD_SECTION_TYPES
    ]
    for section in reversed(sequence):
        record_type = _STAGE_RECORD_SECTION_TYPES[section]
        record = _STAGE_RECORD_SECTION_GETTERS[section](application, stage=review_stage)
        if record and record.is_finalized:
            return record, record_type
    return None, ""


@transaction.atomic
def reopen_recruitment_case(application, actor, reason):
    if not hasattr(application, "case"):
        raise ValueError("A case does not exist for this application.")
    case = application.case
    if actor.role not in CASE_REOPEN_ROLES:
        raise ValueError("Only the HRM Chief can reopen a finalized case.")
    if not case.is_stage_locked or not case.locked_stage:
        raise ValueError("Only finalized cases can be reopened.")
    # RD-7: once the vacancy's selection is recorded, a NON-selected sibling
    # (already closed by pool resolution and emailed "not selected") must not
    # come back to an active review stage — pool queries filter on ACTIVE, so a
    # reopened sibling would re-enter batch logic for a decided vacancy. The
    # WINNER's own case stays reopenable: completion corrections are the
    # legitimate use of this path.
    vacancy_is_decided = False
    winner_application_ids = set()
    plantilla_selection = selection.get_final_selection_for_entry(application.position)
    if plantilla_selection:
        vacancy_is_decided = True
        winner_application_ids.add(plantilla_selection.selected_application_id)
    # SL-14 (COS): only a LIVE selection decides the vacancy. Counting a rescinded row here
    # broke the RD-7 guard in both directions — it kept a rescinded winner in
    # winner_application_ids (so the person who no longer holds the post stayed reopenable,
    # the exact zombie this guard exists to stop) while leaving vacancy_is_decided True, so
    # the siblings who genuinely lost could never be reopened even though nobody held the
    # post any more.
    cos_selected = FinalDecision.objects.filter(
        recruitment_entry=application.position,
        decision_outcome=FinalDecision.Outcome.SELECTED,
        is_rescinded=False,
    ).values_list("application_id", flat=True)
    if cos_selected:
        vacancy_is_decided = True
        winner_application_ids.update(cos_selected)
    if vacancy_is_decided and application.id not in winner_application_ids:
        raise ValueError(
            "This vacancy's selection is already recorded. A non-selected case "
            "can't be reopened while the selection stands."
        )

    reopened_stage = case.locked_stage
    reopened_status = routing._application_status_from_stage(reopened_stage)
    if not reopened_status:
        raise ValueError("This finalized case does not point to a step that can be reopened.")

    reopened_role = routing._handler_role_from_stage(reopened_stage, application=application)
    routing.transition_case(
        application,
        actor,
        to_status=reopened_status,
        to_role=reopened_role,
        route_type=RoutingHistory.RouteType.REOPEN,
        description=f"Case reopened and assigned to {RecruitmentUser.Role(reopened_role).label}.",
        notes=reason,
    )
    case.reopened_at = timezone.now()
    case.save(update_fields=["reopened_at", "updated_at"])

    # SL-12: reopen the decision, not just the envelope around it. Unlocking the case while
    # its finalized screening/exam/interview record kept refusing every write handed the
    # assigned handler active workload with no legal move — they could neither change the
    # outcome nor advance the case. The prior values are archived first, so the correction
    # is possible without the original becoming unreadable.
    correction_record, correction_type = stage_record_for_correction(
        application, stage=reopened_stage
    )
    correction_version = None
    if correction_record is not None:
        correction_version = open_stage_record_correction(
            correction_record,
            correction_type,
            actor,
            reason,
            trigger=StageRecordVersion.Trigger.AUTHORIZED_REOPEN,
        )
    _assert_reopened_case_has_editable_work(application, reopened_stage)

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_REOPENED,
        description="Authorized reopen applied to a finalized recruitment case.",
        metadata={
            "reason": reason,
            "reopened_stage": reopened_stage,
            "corrected_record_type": correction_type,
            "corrected_record_id": correction_record.id if correction_record else None,
            "correction_version_number": (
                correction_version.version_number if correction_version else None
            ),
            **routing._case_timeline_metadata(case),
        },
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Case reopened and assigned to {RecruitmentUser.Role(case.current_handler_role).label}.",
        metadata={
            "status": application.status,
            "current_handler_role": application.current_handler_role,
            **routing._case_timeline_metadata(case),
        },
    )
    case_notices._emit_case_assignment_notification(
        application,
        actor,
        application.current_handler_role,
        kind=Notification.Kind.CASE_RETURNED,
        title=f"{application.reference_label} reopened for correction",
        body=reason,
        exclude_user_ids={actor.id},
    )
    return case
