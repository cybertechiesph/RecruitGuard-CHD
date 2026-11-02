"""Interview: sessions, ratings, competency sheets, paper (fallback) ratings and the
batch interview schedule.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    CompetencyDefinition,
    CompetencyRatingTemplate,
    CompetencyScore,
    AttendanceException,
    AttendanceOutcome,
    InterviewFallbackRating,
    EvidenceVaultItem,
    InterviewRating,
    InterviewSession,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from ..notification_services import (
    queue_applicant_interview_notice_notification,
    queue_interview_batch_panel_digest_notifications,
    queue_interview_session_scheduled_notifications,
)
from ..upload_validation import validate_applicant_document_upload
from . import (
    access,
    audit,
    case_notices,
    clock,
    common,
    entries,
    evidence_vault,
    exam,
    routing,
    screening,
)


INTERVIEW_SESSION_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.HRMPSB_MEMBER,
}
INTERVIEW_SESSION_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
    RecruitmentCase.Stage.HRMPSB_REVIEW,
}
INTERVIEW_RATING_ROLES_BY_STAGE = {
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW: {RecruitmentUser.Role.HRM_CHIEF},
    RecruitmentCase.Stage.HRMPSB_REVIEW: {RecruitmentUser.Role.HRMPSB_MEMBER},
}
# COS interviews are conducted by the HRM Chief alone — one rating locks the session.
MIN_INTERVIEW_OUTPUTS_TO_FINALIZE = 1
# Plantilla interview sessions feed 40% of the CAR: the HRMPSB panel minimum (3 members)
# must be on record before the session can lock (WF-01), and the board is 3-5 members, so
# more than five attributable ratings means something is wrong with the record.
# SL-03: what is counted is DISTINCT RATERS, not outputs. A paper sheet counts because it
# now names its member and carries their score (InterviewFallbackRating); scans of the same
# sheet never do — three versions of one document are not three panel members.
MIN_INTERVIEW_RATINGS_PLANTILLA = 3
MAX_INTERVIEW_RATINGS_PLANTILLA = 5
INTERVIEW_FALLBACK_LABEL = "Interview Rating Sheet (Fallback)"
ARTIFACT_TYPE_INTERVIEW_FALLBACK = "interview_fallback_rating_sheet"


def user_can_support_plantilla_interview(user, application):
    case = getattr(application, "case", None)
    if not case or case.is_stage_locked:
        return False
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
        or case.current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or routing.get_current_workflow_section(application) != "interview"
    ):
        return False
    return user.role in common.PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL.get(application.level, set())


def user_is_interview_rating_support_encoder(user, application):
    return user_can_support_plantilla_interview(user, application)


def interview_is_finalized_for_current_stage(application):
    interview_session = get_interview_session(application)
    return bool(interview_session and interview_session.is_finalized)


def get_interview_session(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.interview_sessions.select_related(
        "scheduled_by",
        "finalized_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def get_interview_sessions(application):
    sessions = list(
        application.interview_sessions.select_related(
            "scheduled_by",
            "finalized_by",
            "recruitment_case",
            "recruitment_entry",
        ).prefetch_related(
            "ratings__rated_by",
            "ratings__encoded_by",
        ).order_by("created_at")
    )
    fallback_items = list(get_interview_fallback_evidence(application))
    fallback_by_stage = {}
    for item in fallback_items:
        fallback_by_stage.setdefault(item.stage, []).append(item)
    for session in sessions:
        session.fallback_evidence_items = fallback_by_stage.get(session.review_stage, [])
    return sessions


def get_interview_ratings(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.interview_ratings.select_related(
        "rated_by",
        "encoded_by",
        "interview_session",
    ).filter(review_stage=review_stage).order_by("created_at")


def get_interview_rating_for_user(application, user, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.interview_ratings.select_related(
        "interview_session",
        "encoded_by",
    ).filter(review_stage=review_stage, rated_by=user).first()


def get_interview_fallback_evidence(application, stage=None):
    case = getattr(application, "case", None)
    if case is None:
        return EvidenceVaultItem.objects.none()
    queryset = EvidenceVaultItem.objects.select_related(
        "uploaded_by",
        "recruitment_case",
    ).filter(
        artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
        artifact_type=ARTIFACT_TYPE_INTERVIEW_FALLBACK,
        recruitment_case=case,
        # SL-03: superseded scans of the same sheet are history, not extra sheets. Counting
        # them is how three versions of one document passed for three panel members.
        is_current_version=True,
        is_archived=False,
    )
    if stage:
        queryset = queryset.filter(stage=stage)
    return queryset.order_by("created_at")


def get_interview_fallback_ratings(interview_session):
    """The structured paper ratings encoded for this session — one per HRMPSB member."""
    return interview_session.fallback_ratings.select_related(
        "rated_by", "encoded_by", "evidence_item"
    ).order_by("created_at")


def interview_session_rater_ids(interview_session):
    """SL-03: the distinct members who rated this interview, however their sheet reached
    the system. This — not a count of outputs — is what the quorum rule measures."""
    digital = set(interview_session.ratings.values_list("rated_by_id", flat=True))
    paper = set(interview_session.fallback_ratings.values_list("rated_by_id", flat=True))
    return digital | paper


def user_can_manage_interview_session(user, application):
    current_stage = routing.get_current_review_stage(application)
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return user_can_support_plantilla_interview(user, application)
    if (
        user.role not in INTERVIEW_SESSION_MANAGER_ROLES
        or current_stage not in INTERVIEW_SESSION_STAGES
        or routing.get_current_workflow_section(application) != "interview"
    ):
        return False
    if (
        application.branch != PositionPosting.Branch.COS
        and current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return False
    return access.user_can_process_application(user, application)


def user_can_manage_interview_rating(user, application):
    if not access.user_is_on_vacancy_panel(user, application.position):
        return False
    current_stage = routing.get_current_review_stage(application)
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and user_can_support_plantilla_interview(user, application)
    ):
        return True
    if (
        user.role not in INTERVIEW_RATING_ROLES_BY_STAGE.get(current_stage, set())
        or routing.get_current_workflow_section(application) != "interview"
    ):
        return False
    return access.user_can_process_application(user, application)


def user_can_upload_interview_fallback(user, application):
    current_stage = routing.get_current_review_stage(application)
    if (
        application.branch == PositionPosting.Branch.PLANTILLA
        and current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return user_can_support_plantilla_interview(user, application)
    if (
        user.role not in INTERVIEW_SESSION_MANAGER_ROLES
        or current_stage not in INTERVIEW_SESSION_STAGES
        or routing.get_current_workflow_section(application) != "interview"
    ):
        return False
    if (
        application.branch != PositionPosting.Branch.COS
        and current_stage != RecruitmentCase.Stage.HRMPSB_REVIEW
    ):
        return False
    return access.user_can_process_application(user, application)


def interview_fallback_panel_queryset(application):
    """SL-03: the members whose paper sheet may be encoded for this interview — the same
    panel that could have rated it online. A fallback rating has to name one of them."""
    stage = routing.get_current_review_stage(application)
    roles = INTERVIEW_RATING_ROLES_BY_STAGE.get(stage, set())
    queryset = RecruitmentUser.objects.filter(
        is_active=True,
        role__in=roles,
    ).order_by("last_name", "first_name", "username")
    member_ids = access.vacancy_panel_member_ids(application.position)
    if member_ids is not None and RecruitmentUser.Role.HRMPSB_MEMBER in roles:
        queryset = queryset.filter(pk__in=member_ids)  # 4a: the vacancy's own panel
    return queryset


def get_interview_schedule_notification_recipients(application):
    return [
        user
        for user in RecruitmentUser.objects.filter(
            is_active=True,
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
        ).order_by("last_name", "first_name", "username")
        if user_can_manage_interview_rating(user, application)
    ]


def _interview_scheduled_for_is_past(scheduled_for):
    return scheduled_for < timezone.now() - timedelta(minutes=5)


def _interview_schedule_change_requires_notification(
    *,
    created,
    previous_scheduled_for,
    previous_location,
    new_scheduled_for,
    new_location,
):
    return (
        created
        or previous_scheduled_for != new_scheduled_for
        or (previous_location or "").strip() != (new_location or "").strip()
    )


def _average_interview_rating(interview_session):
    # SL-03: one average across every valid rating, digital or encoded from paper. Reading
    # only the digital rows is what left a paper-rated session with a blank 40% component.
    scores = [rating.rating_score for rating in interview_session.ratings.all()]
    scores += [rating.rating_score for rating in interview_session.fallback_ratings.all()]
    if not scores:
        return None
    total = sum(scores, Decimal("0"))
    return (total / Decimal(len(scores))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
@common.locks_workflow_row("case")
def save_interview_session(
    application, actor, cleaned_data, finalize=False, notify_applicant=False, notify_panel=False
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before interview scheduling can be recorded.")
    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot be edited.")
    if not user_can_manage_interview_session(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for interview scheduling."
        )
    if review_stage not in INTERVIEW_SESSION_STAGES:
        raise ValueError(
            "Interview scheduling is available only while the case is assigned to Secretariat, HRM Chief, or HRMPSB review."
        )

    created = interview_session is None
    previous_scheduled_for = interview_session.scheduled_for if interview_session else None
    if interview_session is None:
        interview_session = InterviewSession(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            scheduled_by=actor,
            branch=application.branch,
            level=application.level,
        )

    scheduled_for = cleaned_data["scheduled_for"]
    if _interview_scheduled_for_is_past(scheduled_for):
        existing_past_schedule_unchanged = (
            not created
            and previous_scheduled_for == scheduled_for
            and _interview_scheduled_for_is_past(previous_scheduled_for)
        )
        if not existing_past_schedule_unchanged:
            raise ValueError("The interview can't be scheduled in the past.")
    if finalize and clock.event_not_held_yet(scheduled_for):
        # Ratings describe an interview that has happened; finalizing early would lock and
        # route a session nobody has sat yet.
        raise ValueError(clock.not_held_yet_message("interview", scheduled_for))

    interview_session.recruitment_case = application.case
    interview_session.recruitment_entry = application.position
    interview_session.scheduled_by = actor
    interview_session.scheduled_for = scheduled_for
    interview_session.location = cleaned_data["location"]
    interview_session.session_notes = cleaned_data["session_notes"]

    existing_rating_count = interview_session.ratings.count() if interview_session.pk else 0
    fallback_count = get_interview_fallback_evidence(application, stage=review_stage).count()
    # SL-03: count the distinct members who rated, not the number of artifacts on file.
    distinct_rater_count = (
        len(interview_session_rater_ids(interview_session)) if interview_session.pk else 0
    )
    # SL-06: an absent candidate has no ratings and needs none. The session finalizes on the
    # recorded absence instead of demanding a fabricated score or a sheet that never existed,
    # and one no-show no longer blocks the whole pooled finalize.
    candidate_was_absent = (
        interview_session.attendance_outcome == AttendanceOutcome.ABSENT
    )
    if finalize and candidate_was_absent:
        if distinct_rater_count:
            raise ValueError(
                "An interview marked Absent cannot hold ratings — remove them before finalizing."
            )
    elif finalize:
        if application.branch == PositionPosting.Branch.PLANTILLA:
            if distinct_rater_count < MIN_INTERVIEW_RATINGS_PLANTILLA:
                raise ValueError(
                    "This interview needs ratings from at least "
                    f"{MIN_INTERVIEW_RATINGS_PLANTILLA} HRMPSB members before it can be "
                    f"finalized (currently {distinct_rater_count}). A paper sheet counts "
                    "once it is encoded with the member who signed it and their score."
                )
            if distinct_rater_count > MAX_INTERVIEW_RATINGS_PLANTILLA:
                raise ValueError(
                    "The HRMPSB is 3–5 members, but this interview carries ratings from "
                    f"{distinct_rater_count}. Remove the ratings that do not belong to the "
                    "board that sat for this interview."
                )
        elif distinct_rater_count < MIN_INTERVIEW_OUTPUTS_TO_FINALIZE:
            raise ValueError(
                "Record at least one interview rating or encode a fallback rating sheet before finalizing the interview session."
            )
    schedule_notification_recipients = (
        get_interview_schedule_notification_recipients(application)
        if notify_panel
        else []
    )
    unsubmitted_panel_recipients = []
    if finalize:
        submitted_panel_ids = set(
            interview_session.ratings.values_list("rated_by_id", flat=True)
        )
        unsubmitted_panel_recipients = [
            recipient
            for recipient in get_interview_schedule_notification_recipients(application)
            if recipient.id not in submitted_panel_ids
        ]

    interview_session.is_finalized = finalize
    if finalize:
        interview_session.finalized_by = actor
        interview_session.finalized_at = timezone.now()
    else:
        interview_session.finalized_by = None
        interview_session.finalized_at = None
    interview_session.full_clean()
    interview_session.save()

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=(
            AuditLog.Action.INTERVIEW_FINALIZED
            if finalize
            else AuditLog.Action.INTERVIEW_SCHEDULED
        ),
        description=(
            "Finalized interview session output."
            if finalize
            else "Saved interview session schedule."
        ),
        metadata={
            "interview_session_id": interview_session.id,
            "created": created,
            "review_stage": review_stage,
            "scheduled_by_role": actor.role,
            "support_action": user_can_support_plantilla_interview(actor, application),
            "scheduled_for": interview_session.scheduled_for.isoformat(),
            "location": interview_session.location,
            "rating_count": existing_rating_count,
            "fallback_count": fallback_count,
            "distinct_rater_count": distinct_rater_count,
            "is_finalized": interview_session.is_finalized,
        },
    )
    # Notifications are sent only on the explicit "Notify" operations, never on a
    # plain save. Each notify button saves the current schedule first, then sends.
    if notify_panel:
        queue_interview_session_scheduled_notifications(
            application,
            interview_session,
            schedule_notification_recipients,
            actor=actor,
        )
        case_notices._emit_interview_scheduled_in_app_notifications(
            application,
            interview_session,
            schedule_notification_recipients,
        )
    if notify_applicant:
        queue_applicant_interview_notice_notification(
            application,
            interview_session,
            actor=actor,
        )
        # RD-9: record that the applicant was actually told — the status page
        # shows only announced schedules (parity with the exam rule).
        interview_session.applicant_notified_at = timezone.now()
        interview_session.save(update_fields=["applicant_notified_at", "updated_at"])
    if finalize and unsubmitted_panel_recipients and not candidate_was_absent:
        case_notices._emit_interview_finalized_in_app_notifications(
            application,
            unsubmitted_panel_recipients,
        )
    if finalize and candidate_was_absent:
        # SL-06: park the absence for the office's decision rather than advancing an
        # unscored candidate into decision support.
        exam.record_attendance_absence(
            application,
            actor,
            AttendanceException.Stage.INTERVIEW,
            notes=interview_session.session_notes,
        )
    return interview_session


@transaction.atomic
def mark_interview_absent(application, actor, notes=""):
    """SL-06: record that the candidate did not attend their scheduled interview.

    The session finalizes on the absence itself — no rating, no fabricated fallback sheet —
    and the case waits for the office's reschedule-or-terminal decision."""
    review_stage = routing.get_current_review_stage(application)
    session = get_interview_session(application, stage=review_stage)
    if session is None:
        raise ValueError("Schedule the interview before recording an absence.")
    if session.is_finalized:
        raise ValueError("Finalized interview records cannot be changed.")
    if not user_can_manage_interview_session(actor, application):
        raise ValueError("This case is not currently assigned to you for the interview.")
    if session.ratings.exists() or session.fallback_ratings.exists():
        raise ValueError(
            "This interview already carries ratings — remove them before recording an absence."
        )
    session.attendance_outcome = AttendanceOutcome.ABSENT
    session.save(update_fields=["attendance_outcome", "updated_at"])
    return save_interview_session(
        application=application,
        actor=actor,
        cleaned_data={
            "scheduled_for": session.scheduled_for,
            "location": session.location,
            "session_notes": notes or session.session_notes,
        },
        finalize=True,
    )


@transaction.atomic
def get_published_competency_rating_template(entry):
    """Return the rating sheet the HRMPSB panel scores on — only once it has been
    made available (published) or scoring has started (locked). A draft sheet is
    not yet visible to raters."""
    return CompetencyRatingTemplate.objects.filter(
        recruitment_entry=entry,
        status__in=[
            CompetencyRatingTemplate.Status.PUBLISHED,
            CompetencyRatingTemplate.Status.LOCKED,
        ],
    ).first()


def lock_competency_rating_template(template):
    """Lock the sheet once the first score is in, so its competencies/weights can no
    longer be re-shaped under ratings that already exist.

    Do NOT add an unlock counterpart (the way SL-00 gave assessment weights one).
    CompetencyScore.competency cascades on delete, so on the digital path this lock is
    the only thing between a template edit and destroyed historical scores — an unlock
    here opens a data-loss path, not just an evidentiary-drift path (N2 decision log:
    docs/n2-competency-template-versioning-brief.md §7)."""
    if template.status != CompetencyRatingTemplate.Status.LOCKED:
        template.status = CompetencyRatingTemplate.Status.LOCKED
        if not template.locked_at:
            template.locked_at = timezone.now()
        template.save(update_fields=["status", "locked_at", "updated_at"])


def _coerce_competency_scores(template, raw_scores):
    """Validate that every competency on the sheet has an in-range score and return
    an ordered ``{competency: int}`` mapping. ``raw_scores`` may be keyed by either
    the competency instance or its id."""
    by_id = {}
    for key, value in (raw_scores or {}).items():
        competency_id = getattr(key, "pk", key)
        by_id[competency_id] = value

    scores = {}
    for competency in template.competencies.all():
        if competency.id not in by_id or by_id[competency.id] in (None, ""):
            raise ValueError(f"Score every competency before saving (missing: {competency.name}).")
        try:
            score = int(by_id[competency.id])
        except (TypeError, ValueError):
            raise ValueError(f"Enter a whole-number score for {competency.name}.")
        if score < template.scale_min or score > template.scale_max:
            raise ValueError(
                f"Scores must be between {template.scale_min} and {template.scale_max} "
                f"(check {competency.name})."
            )
        scores[competency] = score
    return scores


def _coerce_simple_interview_score(raw_score):
    """Validate a COS simple interview score (0-100) and return a Decimal
    quantized to 0.01. COS interviews are scored with a single end-user / HRMS
    number, not the competency rating sheet (spec §6.2)."""
    if raw_score in (None, ""):
        raise ValueError("Enter the interview score (0-100) before saving the rating.")
    try:
        score = Decimal(str(raw_score)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Enter the interview score as a number between 0 and 100.")
    if score < 0 or score > 100:
        raise ValueError("Interview scores must be between 0 and 100.")
    return score


def compute_competency_rating_score(template, scores_by_competency):
    """Normalize the raw per-competency scores to the 0-100 the CAR consumes.

    Each competency is scored out of the scale maximum (score / scale_max), so a
    top mark is 100% and a mark of 1 on a 1-4 scale is 25%. Competencies are then
    combined by their relative weights. Returns a Decimal quantized to 0.01.
    """
    total_weight = Decimal("0")
    weighted_total = Decimal("0")
    scale_max = Decimal(str(template.scale_max))
    for competency, score in scores_by_competency.items():
        weight = competency.weight or Decimal("0")
        component_percent = (Decimal(str(score)) / scale_max) * Decimal("100")
        weighted_total += weight * component_percent
        total_weight += weight
    if total_weight <= 0:
        return Decimal("0.00")
    return (weighted_total / total_weight).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def save_interview_rating(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before interview ratings can be recorded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot accept rating changes.")
    if not user_can_manage_interview_rating(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for interview rating."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before recording interview ratings.")

    if application.branch == PositionPosting.Branch.COS:
        # COS interviews use a simple end-user / HRMS score + notes, not the
        # competency rating sheet (spec §6.2).
        template = None
        scores_by_competency = {}
        rating_value = _coerce_simple_interview_score(cleaned_data.get("rating_score"))
    else:
        template = get_published_competency_rating_template(application.position)
        if template is None:
            raise ValueError(
                "Publish the interview rating sheet for this vacancy before recording interview ratings."
            )

        competency_scores = cleaned_data.get("competency_scores") or {}
        scores_by_competency = _coerce_competency_scores(template, competency_scores)
        rating_value = compute_competency_rating_score(template, scores_by_competency)

    rated_by = cleaned_data.get("rated_by") or actor
    interview_rating = interview_session.ratings.filter(rated_by=rated_by).first()
    created = interview_rating is None
    if interview_rating is None:
        interview_rating = InterviewRating(
            interview_session=interview_session,
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            rated_by=rated_by,
            encoded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    interview_rating.rated_by = rated_by
    interview_rating.encoded_by = actor
    interview_rating.rating_score = rating_value
    interview_rating.rating_notes = cleaned_data.get("rating_notes", "")
    interview_rating.justification = cleaned_data.get("justification", "")
    interview_rating.full_clean()
    interview_rating.save()

    # Persist the per-competency scores (replace any prior set on revision).
    # COS simple ratings carry no competency scores.
    interview_rating.competency_scores.all().delete()
    if scores_by_competency:
        CompetencyScore.objects.bulk_create(
            [
                CompetencyScore(
                    interview_rating=interview_rating,
                    competency=competency,
                    score=score,
                )
                for competency, score in scores_by_competency.items()
            ]
        )

    # First submitted score locks the template so it can no longer be re-shaped.
    if template is not None:
        lock_competency_rating_template(template)
    if application.branch == PositionPosting.Branch.PLANTILLA:
        # Interview scoring has started: lock the vacancy's assessment weights too.
        entries.lock_vacancy_assessment_weights(application.position)

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_RATING_RECORDED,
        description="Recorded interview rating.",
        metadata={
            "interview_session_id": interview_session.id,
            "interview_rating_id": interview_rating.id,
            "created": created,
            "review_stage": review_stage,
            "rated_by_id": rated_by.id,
            "rated_by_role": rated_by.role,
            "encoded_by_id": actor.id,
            "encoded_by_role": actor.role,
            "encoded_on_behalf": actor.id != rated_by.id,
            "rating_score": str(interview_rating.rating_score),
            "competency_scores": {
                competency.name: score
                for competency, score in scores_by_competency.items()
            },
            "has_justification": bool(interview_rating.justification),
        },
    )
    return interview_rating


def _vacancy_interview_stage(posting):
    """The stage a vacancy's interview happens at: HRMPSB for Plantilla, HRM Chief for COS."""
    if posting.branch == PositionPosting.Branch.COS:
        return RecruitmentCase.Stage.HRM_CHIEF_REVIEW
    return RecruitmentCase.Stage.HRMPSB_REVIEW


def vacancy_interview_pool(posting, *, include_recorded_absences=False):
    """The vacancy's interview-ready applications: at the interview stage (HRMPSB for
    Plantilla, HRM Chief for COS), the interview section current, the interview not yet
    finalized. The qualified pool sits the interview together. Branch-agnostic
    (Plantilla competency sheet, COS single end-user score).

    The batch console may include a finalized ABSENT session so the held candidate and
    letter-of-intent state remain visible. Batch finalization deliberately uses the default
    pool: a recorded no-show is already finalized, needs no ratings, and must not advance.
    """
    stage = _vacancy_interview_stage(posting)
    cases = (
        RecruitmentCase.objects.filter(
            application__position=posting,
            current_stage=stage,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
            is_stage_locked=False,
        )
        .select_related("application", "application__applicant", "application__position")
        .order_by("application__reference_number", "application_id")
    )
    pool = []
    for case in cases:
        application = case.application
        session = get_interview_session(application, stage=stage)
        is_recorded_absence = bool(
            include_recorded_absences
            and session
            and session.is_finalized
            and session.attendance_outcome == AttendanceOutcome.ABSENT
        )
        if is_recorded_absence or (
            routing.get_current_workflow_section(application) == "interview"
            and not interview_is_finalized_for_current_stage(application)
        ):
            pool.append(application)
    return pool


@transaction.atomic
@common.locks_workflow_row("vacancy")
def save_vacancy_interview_schedule(posting, actor, cleaned_data):
    """Apply ONE interview schedule to every candidate in the vacancy's interview pool and
    notify each — the batch sits the interview together. Loops the per-case
    save_interview_session so each application keeps its own InterviewSession + notice.

    WF-14: panel members get ONE digest email per batch action (subject names the vacancy
    and candidate count, body lists the candidates + shared date/venue) instead of one
    near-identical email per candidate. Per-applicant notices and the per-case (single
    session) panel notice are unchanged."""
    # SL-09: same rule as the batch exam — the interview batch is the same single cohort.
    screening._require_final_batch_pool(posting)
    pool = vacancy_interview_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready for the interview in this vacancy.")
    notify_panel = posting.branch == PositionPosting.Branch.PLANTILLA
    sessions = []
    for application in pool:
        sessions.append(
            save_interview_session(
                application=application,
                actor=actor,
                cleaned_data=dict(cleaned_data),
                notify_applicant=True,
                notify_panel=False,
            )
        )
    if notify_panel:
        recipients = get_interview_schedule_notification_recipients(pool[0])
        queue_interview_batch_panel_digest_notifications(
            posting,
            pool,
            sessions[0],
            recipients,
            actor=actor,
        )
        # In-app notices stay per case so each links to the case it concerns.
        for application, session in zip(pool, sessions):
            case_notices._emit_interview_scheduled_in_app_notifications(
                application,
                session,
                recipients,
            )
    return pool


@transaction.atomic
@common.locks_workflow_row("vacancy")
def finalize_vacancy_interview_batch(posting, actor, first_deliberation_note=""):
    """Finalize the interview session for every candidate in the vacancy's interview pool
    together. Each must already carry the required number of distinct attributable raters —
    3–5 HRMPSB members for Plantilla, one for COS — which the per-case save_interview_session
    enforces (SL-03). An optional first-deliberation note ("held on X, members present N") is
    written to each case's history for the two-touchpoint record."""
    screening._require_final_batch_pool(posting)  # SL-09
    pool = vacancy_interview_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready to finalize the interview in this vacancy.")
    finalized = []
    for application in pool:
        session = get_interview_session(application)
        if session is None:
            raise ValueError(
                f"Schedule {application.reference_label}'s interview before finalizing the batch."
            )
        save_interview_session(
            application=application,
            actor=actor,
            cleaned_data={
                "scheduled_for": session.scheduled_for,
                "location": session.location,
                "session_notes": session.session_notes,
            },
            finalize=True,
        )
        finalized.append(application)
    if first_deliberation_note:
        for application in finalized:
            audit.record_audit_event(
                application=application,
                actor=actor,
                action=AuditLog.Action.DELIBERATION_RECORDED,
                description=first_deliberation_note,
                metadata={
                    "recruitment_entry_id": posting.id,
                    "source": "interview_batch_finalize",
                },
            )
    return finalized


@transaction.atomic
def save_vacancy_cos_interview_ratings(posting, actor, ratings_by_case_id, *, finalize=False):
    """Record (and optionally finalize) the simple end-user interview score + notes for the
    whole COS vacancy pool in one go, looping the per-case save_interview_rating (COS path).
    Finalize is all-or-nothing (atomic): every candidate must have a score OR a recorded
    absence (SL-06), then each interview session is finalized and the pool moves on
    together. One no-show no longer holds the whole batch hostage, and nobody has to invent
    a number to get past it."""
    if posting.branch != PositionPosting.Branch.COS:
        raise ValueError("Simple interview ratings apply only to COS vacancies.")
    screening._require_final_batch_pool(posting)  # SL-09 — a no-op for COS, kept for symmetry.
    pool = vacancy_interview_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready for the interview in this vacancy.")
    saved = []
    for application in pool:
        row = ratings_by_case_id.get(application.case.id) or {}
        score = row.get("rating_score")
        has_score = score not in (None, "")
        marked_absent = str(row.get("attendance_outcome", "")) == AttendanceOutcome.ABSENT
        if marked_absent:
            if has_score:
                raise ValueError(
                    f"{application.reference_label} is marked Absent — an absence never "
                    "carries a score."
                )
            session = get_interview_session(application)
            if session is not None and not session.is_finalized:
                session.attendance_outcome = AttendanceOutcome.ABSENT
                session.save(update_fields=["attendance_outcome", "updated_at"])
            continue
        session = get_interview_session(application)
        if (
            has_score
            and session is not None
            and session.attendance_outcome == AttendanceOutcome.ABSENT
        ):
            absence = (
                application.attendance_exceptions.filter(
                    assessment_stage=AttendanceException.Stage.INTERVIEW,
                    review_stage=session.review_stage,
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if absence is not None:
                if absence.is_pending:
                    raise ValueError(
                        "This candidate has a recorded absence awaiting a letter-of-intent "
                        "decision. Resolve the absence (reschedule) before recording a score."
                    )
                raise ValueError(
                    "This candidate's recorded interview absence has already been decided "
                    "and cannot accept a score."
                )
            if session.is_finalized:
                raise ValueError("Finalized interview records cannot accept rating changes.")
            # The COS dropdown can save Absent as a draft without opening SL-06's formal
            # letter-of-intent process. A later score is the operator correcting that
            # draft choice, so restore the invariant before the rating is validated.
            session.attendance_outcome = AttendanceOutcome.ATTENDED
            session.save(update_fields=["attendance_outcome", "updated_at"])
        if finalize:
            if not has_score:
                raise ValueError(
                    "Enter every candidate's interview score, or mark them Absent, before "
                    "finalizing the interview batch "
                    f"(missing: {application.reference_label})."
                )
        elif not has_score:
            continue  # nothing to record for this candidate yet (draft)
        save_interview_rating(
            application=application,
            actor=actor,
            cleaned_data={
                "rating_score": score,
                "rating_notes": row.get("rating_notes", ""),
                "justification": row.get("justification", ""),
            },
        )
        saved.append(application)
    if finalize:
        finalize_vacancy_interview_batch(posting, actor)
    return saved


# Standard CSC competencies seeded into every new interview rating sheet. The
# Technical group is left for the Secretariat to fill in per position.
STANDARD_INTERVIEW_COMPETENCIES = (
    (CompetencyDefinition.Group.CORE, "Exemplifying Integrity"),
    (CompetencyDefinition.Group.CORE, "Professionalism"),
    (CompetencyDefinition.Group.CORE, "Service Excellence"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Effective Communication"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Effective Interpersonal Relations"),
    (CompetencyDefinition.Group.ORGANIZATIONAL, "Organizational Awareness and Commitment"),
)


def get_competency_rating_template(entry):
    return CompetencyRatingTemplate.objects.filter(recruitment_entry=entry).first()


@transaction.atomic
def create_competency_rating_template(entry, actor, scale_min=1, scale_max=4):
    """Create the per-vacancy interview rating sheet, seeded with the standard Core
    and Organizational competencies. The Secretariat then adds the Technical ones."""
    if CompetencyRatingTemplate.objects.filter(recruitment_entry=entry).exists():
        raise ValueError("This vacancy already has an interview rating sheet.")
    template = CompetencyRatingTemplate(
        recruitment_entry=entry,
        created_by=actor,
        scale_min=scale_min,
        scale_max=scale_max,
    )
    template.full_clean()
    template.save()
    CompetencyDefinition.objects.bulk_create(
        [
            CompetencyDefinition(
                template=template,
                group=group,
                name=name,
                weight=Decimal("1.00"),
                order=order,
            )
            for order, (group, name) in enumerate(STANDARD_INTERVIEW_COMPETENCIES, start=1)
        ]
    )
    return template


@transaction.atomic
def save_competency_rating_sheet(template, template_form, formset, publish=False):
    """Persist the Secretariat's edits to the rating sheet: scale + instructions plus
    the competency rows (named rows are saved, blank/removed rows are dropped). When
    ``publish`` is set the sheet is opened to the HRMPSB panel."""
    if template.is_locked:
        raise ValueError("This rating sheet is locked because scoring has started.")
    if publish:
        # The standard sheet only carries the Core and Organizational competencies; the panel
        # can't rate the job itself until the position's Technical ones are added.
        has_technical = any(
            (getattr(form, "cleaned_data", None) or {}).get("group") == "technical"
            and ((form.cleaned_data.get("name") or "").strip())
            and not form.cleaned_data.get("DELETE")
            for form in formset.forms
        )
        if not has_technical:
            raise ValueError(
                "Add at least one Technical competency for this position before making the "
                "sheet available to the panel. You can save it as a draft in the meantime."
            )

    template = template_form.save(commit=False)
    order = 0
    for form in formset.forms:
        cleaned = getattr(form, "cleaned_data", None) or {}
        name = (cleaned.get("name") or "").strip()
        if cleaned.get("DELETE") or not name:
            if form.instance.pk:
                form.instance.delete()
            continue
        order += 1
        competency = form.instance
        competency.template = template
        competency.group = cleaned["group"]
        competency.name = name
        competency.weight = cleaned["weight"]
        competency.order = order
        competency.full_clean()
        competency.save()

    if publish:
        template.status = CompetencyRatingTemplate.Status.PUBLISHED
        if not template.published_at:
            template.published_at = timezone.now()
    template.full_clean()
    template.save()
    return template


@transaction.atomic
@transaction.atomic
def upload_interview_fallback_rating(
    application,
    actor,
    uploaded_file,
    remarks,
    *,
    rated_by=None,
    rating_score=None,
    justification="",
    replace_existing=False,
):
    """Encode one HRMPSB member's paper rating sheet.

    SL-03: a fallback is a rating, not just a file. It names the member who signed the
    sheet and carries their number, so it can count as one rater toward the quorum and
    feed the interview average. The scan is retained as the evidence behind it.
    SL-07: the write authorizes against the fallback-specific predicate, which is what the
    rendered form already checks. Previously the generic evidence uploader re-checked
    "are you the assigned handler" and refused the support office outright, so the action
    was visible but unusable — a paper-rated session could not be finalized legitimately."""
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before fallback interview ratings can be uploaded.")

    review_stage = application.case.current_stage
    interview_session = get_interview_session(application, stage=review_stage)
    if interview_session and interview_session.is_finalized:
        raise ValueError("Finalized interview records cannot accept fallback rating uploads.")
    if not user_can_upload_interview_fallback(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for fallback rating upload."
        )
    if not interview_session:
        raise ValueError("Schedule the interview session before uploading a fallback rating sheet.")
    if rated_by is None:
        raise ValueError(
            "Name the panel member who signed this rating sheet — a fallback sheet is one "
            "member's rating, not an anonymous document."
        )
    if rating_score in (None, ""):
        raise ValueError("Record the numeric rating from the sheet.")
    earlier = InterviewFallbackRating.objects.filter(
        interview_session=interview_session, rated_by=rated_by
    ).first()
    if earlier is not None and not replace_existing:
        # Each member counts once; a second sheet replaces the first, so it must be deliberate.
        raise ValueError(
            f"{rated_by.get_full_name() or rated_by.username} already has a paper sheet on record "
            f"(rating {earlier.rating_score}). To replace it, tick "
            "\u201cReplace this member's earlier sheet\u201d and save again."
        )

    validate_applicant_document_upload(uploaded_file)

    evidence = evidence_vault.upload_evidence_item(
        application=application,
        actor=actor,
        label=f"{INTERVIEW_FALLBACK_LABEL} - {application.case.get_current_stage_display()}",
        uploaded_file=uploaded_file,
        artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
        artifact_type=ARTIFACT_TYPE_INTERVIEW_FALLBACK,
        # One version family per rater: without this every member's sheet would chain as a
        # new version of the previous one, and only the last would read as current.
        document_key=f"{ARTIFACT_TYPE_INTERVIEW_FALLBACK}:{review_stage}:{rated_by.id}",
        # SL-07: already authorized above by the fallback-specific predicate.
        permission_checked=True,
    )

    fallback_rating = (
        InterviewFallbackRating.objects.filter(
            interview_session=interview_session,
            rated_by=rated_by,
        ).first()
        or InterviewFallbackRating(
            interview_session=interview_session,
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            rated_by=rated_by,
            branch=application.branch,
            level=application.level,
        )
    )
    fallback_rating.encoded_by = actor
    fallback_rating.rating_score = rating_score
    fallback_rating.justification = justification or ""
    fallback_rating.remarks = remarks or ""
    fallback_rating.evidence_item = evidence
    fallback_rating.full_clean()
    fallback_rating.save()

    # N2: the first recorded rating of EITHER kind locks the sheet. The paper form the panel
    # signed is a printout of this template, so by the time one signed sheet is being encoded
    # its content is already immutable ink — leaving the template editable would let the sheet
    # on file drift away from the paper in the case folder with nothing recording the change.
    # A pool rated entirely on paper never reached save_interview_rating, so this was the one
    # path where a finalized interview, and a finalized CAR, sat on an editable sheet.
    if application.branch != PositionPosting.Branch.COS:
        rating_template = get_published_competency_rating_template(application.position)
        if rating_template is not None:
            lock_competency_rating_template(rating_template)

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED,
        description=(
            f"Encoded {rated_by.get_full_name() or rated_by.username}'s paper interview "
            "rating sheet."
        ),
        metadata={
            "interview_session_id": interview_session.id,
            "fallback_rating_id": fallback_rating.id,
            "evidence_id": evidence.id,
            "review_stage": review_stage,
            "rated_by_id": rated_by.id,
            "rated_by_role": rated_by.role,
            "encoded_by_id": actor.id,
            "encoded_by_role": actor.role,
            "rating_score": str(fallback_rating.rating_score),
            "remarks": remarks,
            "filename": evidence.original_filename,
        },
    )
    return fallback_rating
