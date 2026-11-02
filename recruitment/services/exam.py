"""Examination: exam schedules and records, the batch exam, absences and letters
of intent.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    AuditLog,
    AttendanceException,
    AttendanceOutcome,
    ExamRecord,
    ExamSchedule,
    EvidenceVaultItem,
    OfficePolicy,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
)
from ..notification_services import (
    queue_exam_invitation_notification,
    queue_non_selected_applicant_notification,
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
    interview,
    routing,
    screening,
)


EXAM_REVIEW_ROLES = common.SCREENING_REVIEW_ROLES
EXAM_STAGES = common.SCREENING_STAGES


def user_can_manage_attendance_exception(user, exception):
    """Apply the office-selected owner to Plantilla interview absence decisions.

    Examination absences and non-Plantilla interviews keep the assigned-stage-owner rule.
    """
    application = exception.application
    if (
        exception.assessment_stage != AttendanceException.Stage.INTERVIEW
        or application.branch != PositionPosting.Branch.PLANTILLA
    ):
        return access.user_can_process_application(user, application)
    policy = entries.get_office_policy()
    if (
        policy.interview_absence_decided_by
        == OfficePolicy.InterviewAbsenceDecisionOwner.PANEL
    ):
        return access.user_can_process_application(user, application)
    case = getattr(application, "case", None)
    return bool(
        case
        and not case.is_stage_locked
        and case.current_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and exception.review_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        and user.role
        in common.PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL.get(application.level, set())
    )


def _close_absent_or_waived_exam_application(application, actor, exam_record):
    """Judgment item A (client decision, 2026-07-04): a candidate whose finalized exam is
    Absent or Waived cannot be ranked under the fixed ETE 40/Exam 20/Interview 40 formula,
    so the application does not proceed. Terminal close (same rejected/closed handling as
    removal from batch), status recorded as the reason, applicant notified with adjusted copy.
    The two statuses stay distinct in the record — they answer different audit questions."""
    is_absent = exam_record.exam_status == ExamRecord.ExamStatus.ABSENT
    reason = (
        "Marked Absent at the scheduled examination — the application does not proceed."
        if is_absent
        else "Examination waived — the application does not proceed."
    )
    previous_status = application.status
    previous_case_status = application.case.case_status
    routing._sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role="",
        next_status=RecruitmentApplication.Status.REJECTED,
        remarks=reason,
    )
    application.current_handler_role = ""
    application.status = RecruitmentApplication.Status.REJECTED
    application.closed_at = timezone.now()
    application.save(
        update_fields=["current_handler_role", "status", "closed_at", "updated_at"]
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICANT_DISQUALIFIED,
        description=(
            "Did not proceed after exam finalization: marked Absent at the examination."
            if is_absent
            else "Did not proceed after exam finalization: the examination was waived."
        ),
        metadata={
            "reason": reason,
            "exam_status": exam_record.exam_status,
            "exam_record_id": exam_record.id,
            "from_status": previous_status,
            "to_status": application.status,
            "from_case_status": previous_case_status,
            "to_case_status": application.case.case_status,
        },
    )
    queue_non_selected_applicant_notification(
        application, actor=actor, exam_status=exam_record.exam_status
    )
    return application


def get_attendance_exceptions(application):
    return application.attendance_exceptions.select_related(
        "recorded_by", "decided_by", "letter_of_intent"
    ).order_by("-created_at")


def get_pending_attendance_exception(application, assessment_stage=None):
    """The absence this case is still waiting for a decision on, if any."""
    queryset = application.attendance_exceptions.select_related(
        "recorded_by", "letter_of_intent"
    ).filter(outcome=AttendanceException.Outcome.PENDING)
    if assessment_stage:
        queryset = queryset.filter(assessment_stage=assessment_stage)
    return queryset.first()


@transaction.atomic
def record_attendance_absence(application, actor, assessment_stage, notes=""):
    """SL-06: record that the candidate did not sit a scheduled assessment.

    No score is written and none is implied. The case waits here for the office's decision
    — reschedule or terminal — which is exactly the choice the applicant's letter of intent
    is meant to inform. Recording an absence is never itself the terminal outcome."""
    case = getattr(application, "case", None)
    if case is None:
        raise ValueError("A case must exist before an absence can be recorded.")
    existing = get_pending_attendance_exception(application, assessment_stage)
    if existing:
        return existing
    exception = AttendanceException(
        application=application,
        recruitment_case=case,
        review_stage=case.current_stage,
        assessment_stage=assessment_stage,
        recorded_by=actor,
        absence_notes=notes or "",
    )
    exception.full_clean()
    exception.save()
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ATTENDANCE_ABSENCE_RECORDED,
        description=(
            f"Recorded absence at the scheduled {exception.get_assessment_stage_display().lower()}."
        ),
        metadata={
            "attendance_exception_id": exception.id,
            "assessment_stage": assessment_stage,
            "review_stage": exception.review_stage,
            "notes": exception.absence_notes,
        },
    )
    return exception


@transaction.atomic
def record_attendance_letter_of_intent(exception, actor, uploaded_file, notes=""):
    """File the applicant's letter of intent against a recorded absence.

    The office already promises this route in every exam invitation and interview notice:
    a written letter to the HRM Unit before the scheduled date. Until now there was nowhere
    to put it."""
    if not exception.is_pending:
        raise ValueError(
            "This absence has already been decided — the letter cannot change it."
        )
    application = exception.application
    if not user_can_manage_attendance_exception(actor, exception):
        raise ValueError("This case is not currently assigned to you.")
    validate_applicant_document_upload(uploaded_file)
    evidence = evidence_vault.upload_evidence_item(
        application=application,
        actor=actor,
        label=f"Letter of Intent — {exception.get_assessment_stage_display()}",
        uploaded_file=uploaded_file,
        artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
        artifact_type=common.ARTIFACT_TYPE_LETTER_OF_INTENT,
        document_key=f"{common.ARTIFACT_TYPE_LETTER_OF_INTENT}:{exception.id}",
        # SL2-15: the action-specific predicate above may authorize the HR support
        # office while the HRMPSB panel owns the case. Do not re-run the generic
        # evidence permission and accidentally undo that narrow authorization.
        permission_checked=True,
    )
    exception.letter_of_intent = evidence
    exception.letter_received_at = timezone.now()
    exception.letter_notes = notes or ""
    exception.full_clean()
    exception.save()
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ATTENDANCE_LETTER_RECEIVED,
        description="Filed the applicant's letter of intent against the recorded absence.",
        metadata={
            "attendance_exception_id": exception.id,
            "assessment_stage": exception.assessment_stage,
            "evidence_id": evidence.id,
            "filename": evidence.original_filename,
            "notes": exception.letter_notes,
        },
    )
    return exception


@transaction.atomic
def resolve_attendance_absence(exception, actor, outcome, reason):
    """SL-06: the office's decision on a recorded absence — reschedule, or terminal.

    Reschedule reopens the missed sitting so the candidate can be scheduled again; terminal
    closes the case with the absence as the stated reason and notifies the applicant. Either
    way the record carries who decided, when, and why. Nothing here happens on a timer — a
    terminal outcome is always a human decision."""
    if not exception.is_pending:
        raise ValueError("This absence has already been decided.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why this absence is being rescheduled or ended.")
    if outcome not in {
        AttendanceException.Outcome.RESCHEDULED,
        AttendanceException.Outcome.TERMINAL,
    }:
        raise ValueError("Choose whether the candidate is rescheduled or does not proceed.")
    application = exception.application
    if not user_can_manage_attendance_exception(actor, exception):
        raise ValueError("This case is not currently assigned to you.")

    exception.outcome = outcome
    exception.decided_by = actor
    exception.decided_at = timezone.now()
    exception.decision_reason = reason
    exception.full_clean()
    exception.save()
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ATTENDANCE_ABSENCE_RESOLVED,
        description=(
            "Rescheduled the candidate after a recorded absence."
            if outcome == AttendanceException.Outcome.RESCHEDULED
            else "Closed the case after a recorded absence."
        ),
        metadata={
            "attendance_exception_id": exception.id,
            "assessment_stage": exception.assessment_stage,
            "outcome": outcome,
            "reason": reason,
            "had_letter_of_intent": exception.has_letter_of_intent,
        },
    )

    if outcome == AttendanceException.Outcome.RESCHEDULED:
        _reopen_missed_sitting(exception, actor)
        return exception

    _close_after_attendance_absence(exception, actor)
    return exception


def _reopen_missed_sitting(exception, actor):
    """Put the missed assessment back in a state where it can be scheduled again."""
    application = exception.application
    if exception.assessment_stage == AttendanceException.Stage.EXAMINATION:
        record = get_exam_record(application, stage=exception.review_stage)
        if record and record.is_finalized:
            record.is_finalized = False
            record.finalized_by = None
            record.finalized_at = None
            record.exam_status = ExamRecord.ExamStatus.COMPLETED
            record.exam_result = ""
            record.save()
        return
    session = interview.get_interview_session(application, stage=exception.review_stage)
    if session:
        session.attendance_outcome = AttendanceOutcome.ATTENDED
        session.is_finalized = False
        session.finalized_by = None
        session.finalized_at = None
        session.save()


def _close_after_attendance_absence(exception, actor):
    application = exception.application
    stage_label = exception.get_assessment_stage_display().lower()
    reason = (
        f"Did not sit the scheduled {stage_label} and the office decided the application "
        "does not proceed."
    )
    previous_status = application.status
    previous_case_status = application.case.case_status
    routing._sync_case_after_workflow_action(
        application=application,
        actor=actor,
        next_role="",
        next_status=RecruitmentApplication.Status.REJECTED,
        remarks=reason,
    )
    application.current_handler_role = ""
    application.status = RecruitmentApplication.Status.REJECTED
    application.closed_at = timezone.now()
    application.save(
        update_fields=["current_handler_role", "status", "closed_at", "updated_at"]
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICANT_DISQUALIFIED,
        description=f"Did not proceed after a recorded {stage_label} absence.",
        metadata={
            "reason": reason,
            "attendance_exception_id": exception.id,
            "decision_reason": exception.decision_reason,
            "from_status": previous_status,
            "to_status": application.status,
            "from_case_status": previous_case_status,
            "to_case_status": application.case.case_status,
        },
    )
    queue_non_selected_applicant_notification(
        application, actor=actor, exam_status=ExamRecord.ExamStatus.ABSENT
    )
    return application


def exam_is_finalized_for_current_stage(application):
    exam_record = get_exam_record(application)
    return bool(exam_record and exam_record.is_finalized)


def get_exam_record(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
        "evidence_item",
    ).filter(review_stage=review_stage).first()


def get_exam_records(application):
    return application.exam_records.select_related(
        "recorded_by",
        "finalized_by",
        "evidence_item",
    ).order_by("created_at")


def get_notified_exam_sitting(application):
    """The exam sitting this applicant was actually told about: the current review stage's
    notified schedule, or (at a re-validation stage such as the HRM Chief re-checking the
    Secretariat's exam) the one notified at an earlier stage. One sitting, one notice."""
    current = get_exam_schedule(application)
    if current is not None and current.applicant_was_notified:
        return current
    return (
        application.exam_schedules.filter(applicant_notified_at__isnull=False)
        .order_by("-scheduled_for")
        .first()
    )


def get_exam_schedule(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return application.exam_schedules.select_related(
        "scheduled_by",
        "recruitment_case",
        "recruitment_entry",
    ).filter(review_stage=review_stage).first()


def user_can_manage_exam(user, application):
    current_stage = routing.get_current_review_stage(application)
    if (
        user.role not in EXAM_REVIEW_ROLES
        or current_stage not in EXAM_STAGES
        or routing.get_current_workflow_section(application) != "exam"
    ):
        return False
    # Batch screening gate (§4.3): the qualified pool moves to exam together, so exam stays
    # blocked while any application in the vacancy still has a pending screening decision.
    # A single-application vacancy auto-satisfies this once its own screening is finalized.
    #
    # SL-04: only where the vacancy actually examines as a batch. Continuous COS is
    # deliberately per-case — candidates arrive and are processed one at a time — so a
    # vacancy-wide "everyone must finish screening" gate has no stable completion point:
    # a qualified candidate waited indefinitely because an unrelated applicant was still
    # screening, while the batch console refused them too, leaving no valid exam door.
    if vacancy_uses_batch_exam(application.position):
        if not screening.vacancy_screening_batch_status(application.position).is_ready:
            return False
    return access.user_can_process_application(user, application)


@transaction.atomic
def save_exam_schedule(application, actor, cleaned_data, record_audit=True):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before the exam can be scheduled.")
    review_stage = application.case.current_stage
    if review_stage not in EXAM_STAGES:
        raise ValueError(
            "Exam scheduling is available only while the case is assigned to Secretariat or HRM Chief review."
        )
    if not user_can_manage_exam(actor, application):
        raise ValueError("This case is not currently assigned to you for exam scheduling.")

    exam_schedule = get_exam_schedule(application, stage=review_stage)
    created = exam_schedule is None
    previous_scheduled_for = exam_schedule.scheduled_for if exam_schedule else None
    previous_venue = exam_schedule.venue if exam_schedule else ""
    if exam_schedule is None:
        exam_schedule = ExamSchedule(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=application.position,
            review_stage=review_stage,
            scheduled_by=actor,
            branch=application.branch,
            level=application.level,
        )

    scheduled_for = cleaned_data["scheduled_for"]
    if interview._interview_scheduled_for_is_past(scheduled_for):
        existing_past_schedule_unchanged = (
            not created
            and previous_scheduled_for == scheduled_for
            and interview._interview_scheduled_for_is_past(previous_scheduled_for)
        )
        if not existing_past_schedule_unchanged:
            raise ValueError("The exam can't be scheduled in the past.")

    notice_delivery = (
        cleaned_data.get("notice_delivery") or ExamSchedule.NoticeDelivery.SYSTEM_EMAIL
    )
    exam_schedule.recruitment_case = application.case
    exam_schedule.recruitment_entry = application.position
    exam_schedule.scheduled_by = actor
    exam_schedule.scheduled_for = scheduled_for
    exam_schedule.venue = cleaned_data["venue"]
    exam_schedule.instructions = cleaned_data.get("instructions", "")
    exam_schedule.notice_delivery = notice_delivery

    should_notify = interview._interview_schedule_change_requires_notification(
        created=created,
        previous_scheduled_for=previous_scheduled_for,
        previous_location=previous_venue,
        new_scheduled_for=exam_schedule.scheduled_for,
        new_location=exam_schedule.venue,
    )
    if should_notify:
        # Saving a new or rescheduled exam invitation is the applicant touchpoint:
        # record that the notice was issued (emailed, or hand-delivered for
        # applicants with no email on file).
        exam_schedule.applicant_notified_at = timezone.now()

    exam_schedule.full_clean()
    exam_schedule.save()

    if should_notify and notice_delivery == ExamSchedule.NoticeDelivery.SYSTEM_EMAIL:
        # A second send for the same sitting is a change, and says so: the applicant must be
        # able to tell which of two emails is current.
        queue_exam_invitation_notification(
            application,
            exam_schedule,
            actor=actor,
            changed=not created and bool(previous_scheduled_for),
        )

    if record_audit:
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.EXAM_SCHEDULED,
            description=(
                "Saved examination schedule and notified the applicant."
                if should_notify
                else "Saved examination schedule."
            ),
            metadata={
                "exam_schedule_id": exam_schedule.id,
                "created": created,
                "review_stage": review_stage,
                "scheduled_for": exam_schedule.scheduled_for.isoformat(),
                "venue": exam_schedule.venue,
                "notice_delivery": exam_schedule.notice_delivery,
                "applicant_notified": should_notify,
            },
        )
    return exam_schedule


@transaction.atomic
@common.locks_workflow_row("case")
def save_exam_record(
    application,
    actor,
    cleaned_data,
    finalize=False,
    evidence_file=None,
    allow_partial=False,
    record_audit=True,
):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before exam details can be recorded.")
    review_stage = application.case.current_stage
    exam_record = get_exam_record(application, stage=review_stage)
    if exam_record and exam_record.is_finalized:
        raise ValueError("Finalized exam records cannot be edited.")
    if not user_can_manage_exam(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for exam details."
        )
    if review_stage not in EXAM_STAGES:
        raise ValueError("Exam details can only be edited while the case is assigned to Secretariat or HRM Chief review.")
    if finalize:
        exam_schedule = get_exam_schedule(application, stage=review_stage)
        schedule_satisfied = bool(exam_schedule and exam_schedule.applicant_was_notified)
        if not schedule_satisfied:
            # WF-12: at a re-validation stage (e.g. the HRM Chief re-checking the
            # Secretariat's exam) the exam already happened — a notified schedule from
            # ANY prior review stage satisfies the guard, and no second "exam invitation"
            # is emailed to the applicant in that path (no new schedule is created).
            schedule_satisfied = application.exam_schedules.filter(
                applicant_notified_at__isnull=False
            ).exists()
        if not schedule_satisfied:
            raise ValueError(
                "Schedule the examination and notify the applicant before recording final results."
            )
        # Results describe a sitting that has happened: finalizing before the scheduled time
        # would record (and route on) scores for an exam nobody has taken yet. A waived exam
        # is an office decision and needs no sitting.
        sitting = (
            exam_schedule
            if exam_schedule and exam_schedule.applicant_was_notified
            else application.exam_schedules.filter(applicant_notified_at__isnull=False)
            .order_by("-scheduled_for")
            .first()
        )
        if (
            (cleaned_data.get("exam_status") or "") != ExamRecord.ExamStatus.WAIVED
            and sitting is not None
            and clock.event_not_held_yet(sitting.scheduled_for)
        ):
            raise ValueError(clock.not_held_yet_message("exam", sitting.scheduled_for))

    created = exam_record is None
    if exam_record is None:
        exam_record = ExamRecord(
            application=application,
            recruitment_case=application.case,
            review_stage=review_stage,
            recorded_by=actor,
            branch=application.branch,
            level=application.level,
        )

    allow_partial = bool(allow_partial and not finalize)
    exam_status = cleaned_data.get("exam_status", "") or ""

    exam_record.recruitment_case = application.case
    exam_record.recorded_by = actor
    exam_record.exam_type = cleaned_data.get("exam_type", "") or ""
    exam_record.exam_status = exam_status
    # The overall exam score is computed from the components in
    # ExamRecord.apply_policy_outputs(); it is never taken from form input.
    exam_record.exam_result = cleaned_data.get("exam_result", "")
    exam_record.technical_score = common._optional_decimal(cleaned_data.get("technical_score"))
    exam_record.technical_result = cleaned_data.get("technical_result", "")
    exam_record.general_score = common._optional_decimal(cleaned_data.get("general_score"))
    exam_record.general_result = cleaned_data.get("general_result", "")
    exam_record.exam_date = cleaned_data.get("exam_date")
    exam_record.administered_by = cleaned_data.get("administered_by", "")
    exam_record.valid_from = cleaned_data.get("valid_from")
    exam_record.valid_until = cleaned_data.get("valid_until")
    exam_record.exam_notes = cleaned_data.get("exam_notes", "")
    if exam_status in {ExamRecord.ExamStatus.WAIVED, ExamRecord.ExamStatus.ABSENT}:
        exam_record.exam_score = None
        exam_record.technical_score = None
        exam_record.general_score = None
        exam_record.valid_from = None
        exam_record.valid_until = None
    exam_record.is_finalized = finalize
    if finalize:
        exam_record.finalized_by = actor
        exam_record.finalized_at = timezone.now()
    else:
        exam_record.finalized_by = None
        exam_record.finalized_at = None
    exam_record.apply_policy_outputs()
    if allow_partial:
        exam_record.validate_unique()
    else:
        exam_record.full_clean()
    exam_record.save()

    evidence = None
    if evidence_file:
        evidence = evidence_vault.upload_evidence_item(
            application=application,
            actor=actor,
            label=f"Exam Evidence - {exam_record.get_exam_type_display()}",
            uploaded_file=evidence_file,
            document_key=f"exam-record-{review_stage}",
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type=common.ARTIFACT_TYPE_EXAM_EVIDENCE,
        )
        exam_record.evidence_item = evidence
        if allow_partial:
            exam_record.validate_unique()
        else:
            exam_record.full_clean()
        exam_record.save(update_fields=["evidence_item", "updated_at"])

    if record_audit:
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=(
                AuditLog.Action.EXAM_FINALIZED
                if finalize
                else AuditLog.Action.EXAM_RECORDED
            ),
            description=(
                "Finalized examination output."
                if finalize
                else "Saved examination record."
            ),
            metadata={
                "exam_record_id": exam_record.id,
                "created": created,
                "review_stage": review_stage,
                "exam_type": exam_record.exam_type,
                "exam_status": exam_record.exam_status,
                "exam_score": common._decimal_string(exam_record.effective_score),
                "exam_result": exam_record.exam_result,
                "technical_score": common._decimal_string(exam_record.technical_score),
                "technical_result": exam_record.technical_result,
                "general_score": common._decimal_string(exam_record.general_score),
                "general_result": exam_record.general_result,
                "exam_date": exam_record.exam_date.isoformat() if exam_record.exam_date else "",
                "administered_by": exam_record.administered_by,
                "valid_from": exam_record.valid_from.isoformat() if exam_record.valid_from else "",
                "valid_until": exam_record.valid_until.isoformat() if exam_record.valid_until else "",
                "evidence_id": evidence.id if evidence else exam_record.evidence_item_id,
                "evidence_uploaded": bool(evidence),
                "is_finalized": exam_record.is_finalized,
            },
        )
    if finalize:
        if exam_record.exam_status == ExamRecord.ExamStatus.ABSENT:
            # SL-06: an absence is recorded, not adjudicated. The office already invites
            # every applicant to send a letter of intent to reschedule, so the case waits
            # for that decision instead of closing itself the instant Absent is saved.
            record_attendance_absence(
                application,
                actor,
                AttendanceException.Stage.EXAMINATION,
                notes=exam_record.exam_notes,
            )
        elif exam_record.exam_status == ExamRecord.ExamStatus.WAIVED:
            # A waiver is already the office's decision, so it stays immediately terminal.
            _close_absent_or_waived_exam_application(application, actor, exam_record)
        else:
            if application.branch == PositionPosting.Branch.PLANTILLA:
                # Scoring is now committed for the vacancy: lock its assessment weights so the
                # split this exam (and the downstream CAR) was computed against cannot change.
                entries.lock_vacancy_assessment_weights(application.position)
            routing._auto_advance_after_exam_finalized(application, actor, review_stage)
            # RD-9: bell for the handler now holding the case (post-advance).
            application.refresh_from_db()
            case_notices._emit_exam_finalized_in_app_notification(application, actor)
    return exam_record


def vacancy_uses_batch_exam(posting):
    """RD-2: does this vacancy's pool sit the exam together?

    Every vacancy does since the open-pool decision (C1, 2026-11-02): Plantilla and one-batch
    COS as a single batch, and an open pool in successive batches of whoever is screened and
    ready. The batch consoles are the only door; the per-case exam and interview screens are
    read-only summaries. Kept as a function so the call sites read as a rule, not a constant.
    """
    return True


def vacancy_exam_pool(posting):
    """The vacancy's exam-ready applications: screening done (current section is 'exam') and the
    exam not yet finalized, at the vacancy's screening stage. The qualified pool that sits the
    exam together. Branch-agnostic — every candidate takes General + Technical (RD-2)."""
    stage = screening._vacancy_batch_stage(posting)
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
        if (
            routing.get_current_workflow_section(application) == "exam"
            and not exam_is_finalized_for_current_stage(application)
        ):
            pool.append(application)
    return pool


@transaction.atomic
@common.locks_workflow_row("vacancy")
def save_vacancy_exam_schedule(posting, actor, cleaned_data):
    """Apply ONE exam schedule (date/venue/instructions) to every candidate in the vacancy's
    exam pool and notify each — the batch sits the exam together. Loops the per-case
    save_exam_schedule so each application keeps its own ExamSchedule + notification."""
    # SL-09: the single vacancy-wide batch cannot start before the pool it is meant to
    # cover is settled.
    screening._require_final_batch_pool(posting)
    if not screening.vacancy_screening_batch_status(posting).is_ready:
        raise ValueError(screening.vacancy_screening_batch_block_message(posting))
    pool = vacancy_exam_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready for the exam in this vacancy.")
    for application in pool:
        save_exam_schedule(application=application, actor=actor, cleaned_data=dict(cleaned_data))
    return pool


@transaction.atomic
@common.locks_workflow_row("vacancy")
def save_vacancy_exam_scores(
    posting,
    actor,
    scores_by_case_id,
    *,
    finalize=False,
    valid_from=None,
    valid_until=None,
    clear_validity_window=False,
):
    """Record (and optionally finalize) the exam scores for the whole vacancy's pool in one go,
    looping the per-case save_exam_record. Every candidate takes General Ability + Technical;
    the overall is auto-computed from the per-vacancy weights (RD-2 — COS scores the same way
    Plantilla does; ASSUMPTION pending client confirmation, flipped via the weights, not here).
    Finalize is all-or-nothing (atomic): every candidate must have the required scores, and on
    finalize each case auto-advances together."""
    # SL-09: the single vacancy-wide batch cannot start before the pool it is meant to
    # cover is settled.
    screening._require_final_batch_pool(posting)
    if not screening.vacancy_screening_batch_status(posting).is_ready:
        raise ValueError(screening.vacancy_screening_batch_block_message(posting))
    pool = vacancy_exam_pool(posting)
    if not pool:
        raise ValueError("No candidates are ready for the exam in this vacancy.")

    is_plantilla = posting.branch == PositionPosting.Branch.PLANTILLA
    exam_type = (
        ExamRecord.ExamType.TECHNICAL_PRACTICAL
        if is_plantilla
        else ExamRecord.ExamType.END_USER_ASSESSMENT
    )
    administered_by = (
        ExamRecord.AdministeredBy.HRMS if is_plantilla else ExamRecord.AdministeredBy.END_USER
    )

    # Check every typed score before anything is saved, drafts included: a draft skips the
    # record's full validation, so without this a 101 was stored and "abc" crashed the page.
    for application in pool:
        row = scores_by_case_id.get(application.case.id) or {}
        for key, label in (("general_score", "General Ability"), ("technical_score", "Technical")):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                value = Decimal(str(raw).strip())
            except ArithmeticError:
                raise ValueError(
                    f"{application.reference_label}: the {label} score must be a number from 0 to 100."
                )
            if not value.is_finite() or value < 0 or value > 100:
                raise ValueError(
                    f"{application.reference_label}: the {label} score must be between 0 and 100."
                )

    saved = []
    for application in pool:
        row = scores_by_case_id.get(application.case.id) or {}
        general = row.get("general_score")
        technical = row.get("technical_score")
        has_general = general not in (None, "")
        has_technical = technical not in (None, "")
        # Judgment item A: a candidate already marked Absent/Waived (per-case exam screen)
        # is exempt from the score requirement. On finalize their record locks as-is and
        # save_exam_record routes them to the terminal do-not-proceed handling.
        existing_record = get_exam_record(application)
        # RD-2: the console owns the per-candidate outcome, because the per-case
        # exam screen is read-only for a pooled vacancy. An outcome posted for
        # this row wins; otherwise keep whatever the record already carries.
        outcome = row.get("exam_status") or (
            existing_record.exam_status if existing_record else ""
        )
        if outcome in {ExamRecord.ExamStatus.ABSENT, ExamRecord.ExamStatus.WAIVED}:
            # Absent/Waived candidates are exempt from the score requirement;
            # their record locks as-is on finalize and save_exam_record routes
            # them to the terminal do-not-proceed handling.
            save_exam_record(
                application=application,
                actor=actor,
                cleaned_data={
                    "exam_type": (existing_record.exam_type if existing_record else "")
                    or exam_type,
                    "exam_status": outcome,
                    "exam_result": existing_record.exam_result if existing_record else "",
                    "technical_score": None,
                    "technical_result": (
                        existing_record.technical_result if existing_record else ""
                    ),
                    "general_score": None,
                    "general_result": (
                        existing_record.general_result if existing_record else ""
                    ),
                    "exam_date": (
                        existing_record.exam_date
                        if existing_record and existing_record.exam_date
                        else timezone.localdate()
                    ),
                    "administered_by": (
                        existing_record.administered_by if existing_record else ""
                    )
                    or administered_by,
                    "valid_from": None,
                    "valid_until": None,
                    # The console is the only writable exam surface for a pooled vacancy
                    # (RD-2 one door), so the remarks the absence/waiver REQUIRES must be
                    # acceptable from it — otherwise a batch with one absent candidate can
                    # never be finalized at all (E2E finding). Posted notes win; otherwise
                    # keep whatever the record already carries.
                    "exam_notes": (row.get("exam_notes") or "").strip()
                    or (existing_record.exam_notes if existing_record else ""),
                },
                finalize=finalize,
                allow_partial=not finalize,
            )
            saved.append(application)
            continue
        if finalize:
            # RD-2: both components are required for every branch.
            if not has_general or not has_technical:
                raise ValueError(
                    "Enter every candidate's General Ability and Technical scores before "
                    f"finalizing the exam batch (missing: {application.reference_label})."
                )
        elif not has_general and not has_technical:
            continue  # nothing to record for this candidate yet (draft)

        schedule = get_exam_schedule(application)
        # The Manila calendar day of the sitting; .date() on the stored UTC time dated
        # every sitting before 08:00 as the day before.
        exam_date = (
            timezone.localdate(schedule.scheduled_for)
            if schedule and schedule.scheduled_for
            else timezone.localdate()
        )
        if clear_validity_window:
            effective_valid_from = None
            effective_valid_until = None
        else:
            effective_valid_from = (
                valid_from
                if valid_from not in (None, "")
                else existing_record.valid_from if existing_record else None
            )
            effective_valid_until = (
                valid_until
                if valid_until not in (None, "")
                else existing_record.valid_until if existing_record else None
            )
        save_exam_record(
            application=application,
            actor=actor,
            cleaned_data={
                "exam_type": exam_type,
                "exam_status": ExamRecord.ExamStatus.COMPLETED,
                "exam_result": "",
                "technical_score": technical,
                "technical_result": "",
                "general_score": general,
                "general_result": "",
                "exam_date": exam_date,
                "administered_by": administered_by,
                # RD-2: the pool sat one exam on one day, so the validity window
                # is the batch's, applied to every candidate alike.
                "valid_from": effective_valid_from,
                "valid_until": effective_valid_until,
                "exam_notes": "",
            },
            finalize=finalize,
            allow_partial=not finalize,
        )
        saved.append(application)
    return saved
