"""Applicant intake: portal identity, drafts, OTP, submission and its packet,
document uploads and resubmission, contact updates, withdrawal and candidate codes.
"""

import hashlib
import hmac
import logging
import secrets
import smtplib
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import date_format
from django.utils.translation import gettext, ngettext

from ..email_branding import email_branding_context
from ..applicant_contact import normalize_philippine_mobile_number
from ..models import (
    AuditLog,
    EvidenceVaultItem,
    Notification,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    RoutingHistory,
    ScreeningDocumentReview,
    ScreeningRecord,
)
from ..notification_services import (
    build_applicant_otp_url,
    queue_applicant_contact_updated_notifications,
    queue_document_resubmission_received_notification,
    queue_submission_acknowledgment_notification,
    queue_withdrawal_acknowledgement_notification,
)
from ..requirements import (
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from ..upload_validation import validate_applicant_document_upload
from . import (
    access,
    audit,
    car,
    case_notices,
    common,
    cos_selection,
    entries,
    evidence_vault,
    routing,
    screening,
    selection,
    workflow_map,
)


logger = logging.getLogger("recruitment.services")


REAPPLICATION_ELIGIBLE_TERMINAL_STATUSES = frozenset(
    {
        RecruitmentApplication.Status.WITHDRAWN,
        RecruitmentApplication.Status.VACANCY_CANCELLED,
    }
)


class ApplicationOTPDeliveryError(RuntimeError):
    pass


def user_can_record_withdrawal(user, application):
    """SL-10: who may record that an applicant has stepped out.

    The assigned handler always may, except the HRMPSB panel (it reviews candidates blind) and
    the Appointing Authority (whose page holds the decision only): neither deals with
    applicants, so while a case is with either, the level's owning office records the
    withdrawal whatever Office Policy says (HRMPSB review P1-2, AA review P2-3). Office Policy
    decides whether the owning office may also record it while the other office holds the
    case on loan. Not available once the case is terminal, and not the route for a
    selected appointee changing their mind during completion: that unwinds a recorded
    selection and belongs to the rescind path (SL-14)."""
    case = getattr(application, "case", None)
    if case is None:
        return False
    if case.case_status in {
        RecruitmentCase.CaseStatus.APPROVED,
        RecruitmentCase.CaseStatus.REJECTED,
        RecruitmentCase.CaseStatus.WITHDRAWN,
        RecruitmentCase.CaseStatus.VACANCY_CANCELLED,
    }:
        return False
    if case.current_stage in {
        RecruitmentCase.Stage.COMPLETION,
        RecruitmentCase.Stage.CLOSED,
    }:
        return False
    if user.role in {
        RecruitmentUser.Role.HRMPSB_MEMBER,
        RecruitmentUser.Role.APPOINTING_AUTHORITY,
    }:
        return False
    if access.user_can_process_application(user, application):
        return True
    is_owning_office = user.role == routing._completion_handler_role(application)
    if case.current_stage in {
        RecruitmentCase.Stage.HRMPSB_REVIEW,
        RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
    }:
        return is_owning_office
    return bool(entries.get_office_policy().withdrawal_recorded_by_owning_office and is_owning_office)


@transaction.atomic
def record_application_withdrawal(
    application, actor, reason, *, uploaded_file=None, request_notes=""
):
    """SL-10: record that the applicant has withdrawn from the competition.

    Withdrawal is neither rejection, disqualification, nor non-selection, and until now the
    office had no way to say so: ``Status.WITHDRAWN`` existed on the application with no
    case state to pair with it and no action that could write it, so staff had to either
    leave a withdrawn person active — still blocking batches and receiving schedules — or
    misclassify them as rejected.

    The applicant's request (typically an email) is attached as the evidence for it."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record the reason and reference for this withdrawal.")
    case = getattr(application, "case", None)
    if case is None:
        raise ValueError("A case must exist before a withdrawal can be recorded.")
    if case.case_status == RecruitmentCase.CaseStatus.WITHDRAWN:
        raise ValueError("This application is already recorded as withdrawn.")
    if case.current_stage in {
        RecruitmentCase.Stage.COMPLETION,
        RecruitmentCase.Stage.CLOSED,
    }:
        raise ValueError(
            "This case has already passed selection. A selected appointee who withdraws "
            "needs the selection rescinded, not a withdrawal recorded against the "
            "competition."
        )
    if not user_can_record_withdrawal(actor, application):
        raise ValueError("This case is not currently assigned to you.")

    evidence = None
    if uploaded_file is not None:
        validate_applicant_document_upload(uploaded_file)
        evidence = evidence_vault.upload_evidence_item(
            application=application,
            actor=actor,
            label="Applicant withdrawal request",
            uploaded_file=uploaded_file,
            artifact_scope=EvidenceVaultItem.OwnerScope.CASE,
            artifact_type=common.ARTIFACT_TYPE_WITHDRAWAL_REQUEST,
        )

    previous_status = application.status
    previous_case_status = case.case_status
    previous_stage = case.current_stage
    previous_role = application.current_handler_role
    routing.transition_case(
        application,
        actor,
        to_status=RecruitmentApplication.Status.WITHDRAWN,
        to_role="",
        route_type=RoutingHistory.RouteType.CLOSE,
        description="Closed as withdrawn at the applicant's request.",
        notes=reason,
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_WITHDRAWN,
        description="Recorded the applicant's withdrawal from this recruitment.",
        metadata={
            "reason": reason,
            "request_notes": request_notes,
            "evidence_id": evidence.id if evidence else None,
            "from_status": previous_status,
            "to_status": application.status,
            "from_role": previous_role,
            "to_role": "",
            "from_stage": previous_stage,
            "to_stage": case.current_stage,
            "from_case_status": previous_case_status,
            "to_case_status": case.case_status,
        },
    )
    queue_withdrawal_acknowledgement_notification(application, actor=actor, reason=reason)
    return application


IDENTIFIER_GENERATION_RETRIES = 5


def assign_candidate_code(application):
    """Per-vacancy blinding alias (C-NN) for HRMPSB evaluation surfaces.
    Numbers are drawn at random from a pool comfortably larger than the
    vacancy's candidate count, so a code reveals nothing about application
    order. Save under the per-vacancy unique constraint and re-read the pool
    after a concurrent assignment collision. Idempotent."""
    if application.candidate_code:
        return application.candidate_code

    last_error = None
    for _ in range(IDENTIFIER_GENERATION_RETRIES):
        vacancy_applications = RecruitmentApplication.objects.filter(
            position=application.position
        )
        taken = set(
            vacancy_applications.exclude(candidate_code="").values_list(
                "candidate_code", flat=True
            )
        )
        pool_size = max(20, vacancy_applications.count() * 2)
        candidates = [
            f"C-{number:02d}"
            for number in range(1, pool_size + 1)
            if f"C-{number:02d}" not in taken
        ]
        application.candidate_code = secrets.choice(candidates)
        try:
            with transaction.atomic():
                application.save(update_fields=["candidate_code", "updated_at"])
        except IntegrityError as exc:
            application.candidate_code = ""
            last_error = exc
            continue
        return application.candidate_code

    raise last_error


def user_can_update_applicant_contact(user, application):
    case = getattr(application, "case", None)
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if case is None or case.current_stage == RecruitmentCase.Stage.CLOSED:
        return False
    return user.role == routing._completion_handler_role(application)


@transaction.atomic
def update_applicant_contact(
    application,
    actor,
    new_email,
    new_phone,
    reason,
):
    if not user_can_update_applicant_contact(actor, application):
        raise ValueError("Only the case's owning office may correct applicant contact details.")

    normalized_email = normalize_applicant_email(new_email)
    try:
        validate_email(normalized_email)
    except ValidationError as exc:
        raise ValueError("Enter a valid applicant email address.") from exc

    normalized_phone = ""
    if (new_phone or "").strip():
        try:
            normalized_phone = normalize_philippine_mobile_number(new_phone)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Record why the applicant contact was corrected.")

    old_email = normalize_applicant_email(application.applicant_email)
    old_phone = (application.applicant_phone or "").strip()
    if normalized_email == old_email and normalized_phone == old_phone:
        raise ValueError("The applicant contact details are unchanged.")

    duplicate_exists = (
        RecruitmentApplication.objects.filter(
            position=application.position,
            applicant_email__iexact=normalized_email,
        )
        .exclude(pk=application.pk)
        .exclude(status__in=REAPPLICATION_ELIGIBLE_TERMINAL_STATUSES)
        .exists()
    )
    if duplicate_exists:
        raise ValueError(
            "Another active application for this vacancy already uses that email address."
        )

    application.applicant_email = normalized_email
    application.applicant_phone = normalized_phone
    application.save(
        update_fields=["applicant_email", "applicant_phone", "updated_at"]
    )

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICANT_CONTACT_UPDATED,
        description="Corrected the applicant's contact information.",
        metadata={
            "old_email": old_email,
            "new_email": normalized_email,
            "old_phone": old_phone,
            "new_phone": normalized_phone,
            "reason": reason,
        },
    )
    notifications = queue_applicant_contact_updated_notifications(
        application,
        actor=actor,
        old_email=old_email,
        new_email=normalized_email,
    )
    return application, notifications


APPLICANT_WITHDRAWAL_CLOSED_STATUSES = {
    RecruitmentApplication.Status.APPROVED,
    RecruitmentApplication.Status.REJECTED,
    RecruitmentApplication.Status.WITHDRAWN,
    RecruitmentApplication.Status.VACANCY_CANCELLED,
}


def applicant_can_request_withdrawal(application):
    """The status page offers "Withdraw my application" while the competition is still running
    for this applicant (the same stages where staff can record a withdrawal)."""
    case = getattr(application, "case", None)
    return bool(
        application.submitted_at
        and case is not None
        and application.status not in APPLICANT_WITHDRAWAL_CLOSED_STATUSES
        and case.current_stage
        not in {RecruitmentCase.Stage.COMPLETION, RecruitmentCase.Stage.CLOSED}
        and not application.withdrawal_requested_at
    )


@transaction.atomic
def request_application_withdrawal(application, reason=""):
    """The applicant asks to withdraw. Nothing about the case changes here: the office is
    notified and records the withdrawal itself (SL-10), so batches, schedules and notices are
    handled by staff. The request and its reason go on the record."""
    application = RecruitmentApplication.objects.select_for_update().get(pk=application.pk)
    if application.withdrawal_requested_at:
        raise ValueError(gettext("You already asked to withdraw this application. The office will confirm by email."))
    if not applicant_can_request_withdrawal(application):
        raise ValueError(
            gettext("This application can no longer be withdrawn online. Please contact the recruitment team.")
        )
    application.withdrawal_requested_at = timezone.now()
    application.withdrawal_request_reason = (reason or "").strip()[:1000]
    application.save(
        update_fields=["withdrawal_requested_at", "withdrawal_request_reason", "updated_at"]
    )
    audit.record_audit_event(
        application=application,
        actor=application.applicant,
        action=AuditLog.Action.APPLICATION_WITHDRAWAL_REQUESTED,
        description="Applicant asked to withdraw this application from Track Application.",
        metadata={"reason": application.withdrawal_request_reason},
    )
    case_notices._emit_case_assignment_notification(
        application,
        application.applicant,
        application.case.current_handler_role,
        kind=Notification.Kind.WITHDRAWAL_REQUESTED,
        title=f"{application.reference_label} applicant asked to withdraw",
        body="Confirm it with Record an applicant withdrawal on the case page.",
    )
    return application


def build_submission_packet(application):
    case = getattr(application, "case", None)
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
    deliberation_record = selection.get_latest_finalized_deliberation_record(application)
    # SL-02: the packet asks whether THIS candidate was picked, not merely whether the
    # vacancy has some finalized pick somewhere — otherwise every sibling reads as ready.
    cos_deliberation_pick = (
        cos_selection.get_cos_pick_for_case(case)
        if application.branch == PositionPosting.Branch.COS
        else None
    )
    has_cos_pick = bool(cos_deliberation_pick)
    comparative_assessment_report = car.get_latest_finalized_comparative_assessment_report(application)
    evidence_items = list(evidence_vault.get_evidence_items_for_application_context(application))

    missing_components = []
    if not screening_records:
        missing_components.append("Finalized screening record")
    if not exam_records:
        missing_components.append("Finalized examination record")
    if not interview_sessions:
        missing_components.append("Finalized interview session")
    if application.branch == PositionPosting.Branch.COS and not has_cos_pick:
        missing_components.append("Finalized COS deliberation pick")
    if application.branch == PositionPosting.Branch.PLANTILLA and not comparative_assessment_report:
        missing_components.append("Finalized Comparative Assessment Report")

    preserved_artifact_ids = {
        "screening_record_ids": [record.id for record in screening_records],
        "exam_record_ids": [record.id for record in exam_records],
        "interview_session_ids": [session.id for session in interview_sessions],
        "deliberation_record_ids": [deliberation_record.id] if deliberation_record else [],
        "comparative_assessment_report_ids": (
            [comparative_assessment_report.id] if comparative_assessment_report else []
        ),
        "evidence_item_ids": [item.id for item in evidence_items],
    }

    return {
        "context": {
            "built_at": timezone.now().isoformat(),
            "application_reference": application.reference_number or "",
            "application_id": application.id,
            "branch": application.branch,
            "branch_label": application.get_branch_display(),
            "level": application.level,
            "level_label": application.get_level_display(),
            "applicant_name": application.applicant_display_name,
            "applicant_email": application.applicant_email,
            "recruitment_entry_code": application.position.job_code,
            "recruitment_entry_title": application.position.title,
            "current_stage": getattr(case, "current_stage", ""),
            "current_stage_label": case.get_current_stage_display() if case else "",
            "case_status": getattr(case, "case_status", ""),
            "case_status_label": case.get_case_status_display() if case else "",
            "current_handler_role": getattr(case, "current_handler_role", ""),
            "locked_stage": getattr(case, "locked_stage", ""),
            "is_stage_locked": getattr(case, "is_stage_locked", False),
        },
        "summary": {
            "ready_for_final_decision": not missing_components,
            "missing_components": missing_components,
            "finalized_screening_count": len(screening_records),
            "finalized_exam_count": len(exam_records),
            "finalized_interview_count": len(interview_sessions),
            "evidence_reference_count": len(evidence_items),
            "has_deliberation_record": bool(deliberation_record),
            "has_cos_deliberation_pick": has_cos_pick,
            "has_comparative_assessment_report": bool(comparative_assessment_report),
        },
        "cos_deliberation_pick": (
            {
                "chosen_applicant": cos_deliberation_pick.chosen_case.application.applicant_display_name,
                "chosen_case_id": cos_deliberation_pick.chosen_case_id,
                "notes": cos_deliberation_pick.notes,
                "finalized_at": (
                    cos_deliberation_pick.finalized_at.isoformat()
                    if cos_deliberation_pick.finalized_at
                    else ""
                ),
            }
            if has_cos_pick
            else {}
        ),
        "screening_records": [selection._decision_packet_screening_record(record) for record in screening_records],
        "exam_records": [selection._decision_packet_exam_record(record) for record in exam_records],
        "interview_sessions": [
            selection._decision_packet_interview_session(application, session) for session in interview_sessions
        ],
        "deliberation_record": (
            selection._decision_packet_deliberation_record(deliberation_record) if deliberation_record else {}
        ),
        "comparative_assessment_report": (
            selection._decision_packet_car_report(comparative_assessment_report)
            if comparative_assessment_report
            else {}
        ),
        "evidence_references": [
            selection._decision_packet_evidence_reference(item) for item in evidence_items
        ],
        "preserved_artifact_ids": preserved_artifact_ids,
    }


def _upsert_recruitment_case_for_submission(application, actor, next_role, next_status):
    next_stage = routing._review_stage_from_application_status(next_status)
    if not next_stage:
        raise ValueError("Submitted applications must route to a valid internal review stage.")

    case, created = RecruitmentCase.objects.get_or_create(
        application=application,
        defaults={
            "branch": application.branch,
            "current_stage": next_stage,
            "stage_entered_at": timezone.now(),
            "current_handler_role": next_role,
            "case_status": RecruitmentCase.CaseStatus.ACTIVE,
            "is_stage_locked": False,
            "locked_stage": "",
        },
    )
    if not created:
        raise ValueError("A recruitment case already exists for this application.")
    case_notices._emit_case_assignment_notification(
        application,
        actor,
        next_role,
        title=f"{application.reference_label} is now in your queue",
        body="A new application is ready for review.",
        tab="screening",
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CASE_CREATED,
        description="Recruitment case created from the finalized application.",
        metadata={"created": created, **routing._case_timeline_metadata(case)},
    )
    return case


def _build_portal_applicant_username():
    return f"portal-{uuid.uuid4().hex[:12]}"


def normalize_applicant_email(email):
    return (email or "").strip().lower()


def create_portal_applicant_identity(first_name, last_name, email, phone):
    applicant = RecruitmentUser(
        username=_build_portal_applicant_username(),
        first_name=first_name,
        last_name=last_name,
        email=email,
        office_name="Public Applicant",
        employee_id="",
        role=RecruitmentUser.Role.APPLICANT,
        is_active=False,
    )
    applicant.set_unusable_password()
    applicant.save()
    return applicant


def get_reusable_public_application_draft(entry, applicant_email):
    return (
        RecruitmentApplication.objects.select_related("applicant", "position")
        .filter(
            position=entry,
            applicant_email__iexact=applicant_email,
            submitted_at__isnull=True,
            status=RecruitmentApplication.Status.DRAFT,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )


def get_reusable_public_application_for_applicant(entry, applicant):
    return (
        RecruitmentApplication.objects.select_related("applicant", "position")
        .filter(
            position=entry,
            applicant=applicant,
        )
        .exclude(status__in=REAPPLICATION_ELIGIBLE_TERMINAL_STATUSES)
        .order_by("-updated_at", "-created_at")
        .first()
    )


def get_portal_applicant_identity_by_email(applicant_email):
    return (
        RecruitmentUser.objects.filter(
            role=RecruitmentUser.Role.APPLICANT,
            email__iexact=applicant_email,
        )
        .order_by("-is_active", "id")
        .first()
    )


def _sync_portal_applicant_identity(applicant, *, first_name, last_name, email):
    changed_fields = []
    if applicant.first_name != first_name:
        applicant.first_name = first_name
        changed_fields.append("first_name")
    if applicant.last_name != last_name:
        applicant.last_name = last_name
        changed_fields.append("last_name")
    if normalize_applicant_email(applicant.email) != email:
        applicant.email = email
        changed_fields.append("email")
    if not applicant.is_active and applicant.office_name != "Public Applicant":
        applicant.office_name = "Public Applicant"
        changed_fields.append("office_name")
    if not applicant.is_active and applicant.employee_id:
        applicant.employee_id = ""
        changed_fields.append("employee_id")
    if changed_fields:
        applicant.save(update_fields=changed_fields)
    return applicant


def get_current_applicant_document_items(application):
    return EvidenceVaultItem.objects.filter(
        application=application,
        artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
        artifact_type=common.ARTIFACT_TYPE_APPLICANT_DOCUMENT,
        stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
        is_current_version=True,
        is_archived=False,
    ).order_by("document_key", "-version_number", "-created_at")


def get_current_applicant_document_map(application):
    return {
        evidence.document_key: evidence
        for evidence in get_current_applicant_document_items(application)
    }


def get_applicant_document_review_items(application):
    current_documents = get_current_applicant_document_map(application)
    screening_record = screening.get_screening_record(application)
    document_reviews_by_key = {}
    if screening_record is not None:
        document_reviews_by_key = {
            review.document_key: review
            for review in screening_record.document_reviews.select_related("evidence_item")
        }
    review_items = []
    for requirement in get_applicant_document_requirements(application):
        evidence = current_documents.get(requirement.code)
        document_review = document_reviews_by_key.get(requirement.code)
        is_not_applicable = (
            requirement.conditional_on_performance_rating
            and application.performance_rating_not_applicable
            and evidence is None
        )
        review_items.append(
            {
                "requirement": requirement,
                "evidence": evidence,
                "document_review": document_review,
                "is_submitted": evidence is not None,
                "is_not_applicable": is_not_applicable,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
                "review_status": (
                    document_review.status
                    if document_review is not None
                    else (
                        ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
                        if is_not_applicable
                        else ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                    )
                ),
                "review_remarks": document_review.remarks if document_review is not None else "",
            }
        )
    return review_items


def get_missing_required_applicant_document_requirements(application):
    present_document_codes = set(get_current_applicant_document_map(application))
    return [
        requirement
        for requirement in get_required_applicant_document_requirements(
            application,
            performance_rating_not_applicable=application.performance_rating_not_applicable,
        )
        if requirement.code not in present_document_codes
    ]


def get_duplicate_applicant_document_groups(application):
    requirement_titles = {
        requirement.code: requirement.title
        for requirement in get_applicant_document_requirements(application)
    }
    digest_to_codes = {}
    for evidence in get_current_applicant_document_items(application):
        if evidence.document_key not in requirement_titles:
            continue
        digest_to_codes.setdefault(evidence.sha256_digest, set()).add(evidence.document_key)
    return [
        [
            {
                "code": document_key,
                "title": requirement_titles[document_key],
            }
            for document_key in sorted(document_keys)
        ]
        for document_keys in digest_to_codes.values()
        if len(document_keys) > 1
    ]


def _archive_applicant_document_key(application, document_key, actor, archive_tag):
    evidence_items = list(
        get_current_applicant_document_items(application).filter(document_key=document_key)
    )
    if not evidence_items:
        return []

    now = timezone.now()
    archived_ids = []
    for evidence in evidence_items:
        evidence.is_archived = True
        evidence.archive_tag = archive_tag
        evidence.archived_at = now
        evidence.archived_by = actor
        evidence.save(
            update_fields=[
                "is_archived",
                "archive_tag",
                "archived_at",
                "archived_by",
                "archived_by_role",
                "updated_at",
            ]
        )
        archived_ids.append(evidence.id)

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.EVIDENCE_ARCHIVED,
        description=f"Archived applicant document '{document_key}' because it no longer applies.",
        metadata={
            "document_key": document_key,
            "evidence_ids": archived_ids,
            "archive_tag": archive_tag,
        },
    )
    return evidence_items


def _hash_application_otp(application, otp_code):
    payload = "|".join(
        [
            str(application.public_token),
            application.applicant_email.lower(),
            otp_code,
            settings.APPLICATION_OTP_HASH_SECRET,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _generate_otp_code():
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _deliver_application_otp(application, otp_code, *, actor=None, otp_expires_at=None):
    otp_expires_at = otp_expires_at or application.otp_expires_at
    # Sent while the applicant is on the portal, so the active language is theirs.
    subject = gettext("DOH–CHD CALABARZON Recruitment Portal applicant verification code")
    text_body = gettext(
        "DOH–CHD CALABARZON Recruitment Portal applicant verification code\n\n"
        "Your verification code is %(code)s.\n"
        "It expires in %(minutes)s minutes.\n\n"
        "Do not share this code. DOH–CHD CALABARZON staff will never ask for it."
    ) % {"code": otp_code, "minutes": settings.APPLICATION_OTP_VALIDITY_MINUTES}
    # Draft applicants land on the OTP page; give them a way back if they close the
    # tab there (drafts have no reference_number yet, so this link is their only
    # recovery). Resubmission-state OTP emails deliberately omit it — that flow
    # returns to the /resubmit/ page, not /otp/.
    continue_url = (
        build_applicant_otp_url(application)
        if application.status == RecruitmentApplication.Status.DRAFT
        else ""
    )
    if continue_url:
        text_body += "\n\n" + gettext(
            "Keep this email. Your progress is saved, and this link brings you back to your "
            "application for %(title)s anytime:\n%(link)s"
        ) % {"title": application.position.title, "link": continue_url}
    try:
        html_body = render_to_string(
            "email/applicant_otp.html",
            {
                "application": application,
                "otp_code": otp_code,
                "otp_expires_at": otp_expires_at,
                "otp_validity_minutes": settings.APPLICATION_OTP_VALIDITY_MINUTES,
                "continue_url": continue_url,
                "position_title": application.position.title,
                **email_branding_context("applicant"),
            },
        )
    except Exception:
        # The HTML body is only a richer alternative to the plain-text message,
        # which already carries the verification code. Degrade to text-only
        # delivery rather than blocking the code email on a template failure.
        logger.exception(
            "Failed to render applicant OTP HTML email for application %s; "
            "falling back to plain-text body.",
            application.pk,
        )
        html_body = ""
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[application.applicant_email],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    try:
        email.send(fail_silently=False)
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception(
            "Applicant OTP email delivery failed for application %s.",
            application.pk,
        )
        # Round E, P3-3: "your draft was saved" only makes sense for a draft.
        if application.submitted_at:
            message = gettext("We could not send a verification code right now. Please try again in a few moments.")
        else:
            message = gettext(
                "Your application draft and uploaded files were saved, but we could not send "
                "the verification code right now. Please try again in a few moments."
            )
        raise ApplicationOTPDeliveryError(message) from exc
    audit.record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=AuditLog.Action.APPLICATION_OTP_SENT,
        description="Sent applicant verification code for final submission.",
        metadata={"otp_expires_at": otp_expires_at.isoformat()},
    )


DRAFT_REMINDER_DEFAULT_DAYS_BEFORE = 3


def find_drafts_due_for_reminder(days_before=DRAFT_REMINDER_DEFAULT_DAYS_BEFORE, today=None):
    """Verified, unsubmitted drafts whose vacancy closes within ``days_before`` days (and is
    still open) and that have not been reminded yet. Unverified drafts are skipped: nobody
    has shown they own that inbox, so we don't email it."""
    today = today or timezone.localdate()
    candidates = (
        RecruitmentApplication.objects.select_related("position")
        .filter(
            status=RecruitmentApplication.Status.DRAFT,
            submitted_at__isnull=True,
            otp_verified_at__isnull=False,
            draft_reminder_sent_at__isnull=True,
            position__closing_date__gte=today,
            position__closing_date__lte=today + timedelta(days=days_before),
        )
        .order_by("position__closing_date", "pk")
    )
    return [application for application in candidates if application.position.is_open_for_intake]


def send_draft_reminder(application):
    """One reminder email for an unsubmitted draft; stamps draft_reminder_sent_at.
    Sent from a scheduled command, so it switches to the applicant's portal language."""
    from ..notification_services import applicant_email_language

    with translation.override(applicant_email_language(application)):
        return _send_draft_reminder(application)


def _send_draft_reminder(application):
    from ..notification_services import build_applicant_otp_url

    position = application.position
    continue_url = build_applicant_otp_url(application)
    closing = date_format(position.closing_date, "F j, Y")
    subject = gettext("Your application for %(title)s is not submitted yet") % {"title": position.title}
    text_body = gettext(
        "Hello %(name)s,\n\n"
        "You started an application for %(title)s, but it has not been submitted yet. "
        "Applications close on %(date)s.\n\n"
        "Everything you added is saved. Open this link, enter the code we email you, and "
        "submit when your documents are ready:\n%(link)s\n\n"
        "If you no longer wish to apply, you can ignore this email.\n\n"
        "DOH–CHD CALABARZON Recruitment Portal"
    ) % {
        "name": application.applicant_first_name or gettext("applicant"),
        "title": position.title,
        "date": closing,
        "link": continue_url,
    }
    try:
        html_body = render_to_string(
            "email/applicant_draft_reminder.html",
            {
                "application": application,
                "position_title": position.title,
                "closing_date": position.closing_date,
                "continue_url": continue_url,
                **email_branding_context("applicant"),
            },
        )
    except Exception:
        logger.exception("Failed to render the draft reminder HTML for application %s.", application.pk)
        html_body = ""
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[application.applicant_email],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
    application.draft_reminder_sent_at = timezone.now()
    application.save(update_fields=["draft_reminder_sent_at", "updated_at"])
    audit.record_audit_event(
        application=application,
        actor=application.applicant,
        action=AuditLog.Action.APPLICATION_DRAFT_REMINDER_SENT,
        description="Reminded the applicant that their application is not submitted yet.",
        metadata={"closing_date": position.closing_date.isoformat()},
    )
    return application


RESUBMISSION_REMINDER_DEFAULT_DAYS_BEFORE = 2


def find_resubmissions_due_for_reminder(
    days_before=RESUBMISSION_REMINDER_DEFAULT_DAYS_BEFORE, today=None
):
    """Pending document resubmissions whose deadline is within ``days_before`` days and that
    have not been reminded for that deadline yet."""
    today = today or timezone.localdate()
    records = (
        ScreeningRecord.objects.select_related("application", "application__position", "application__case")
        .filter(
            resubmission_reminder_sent_at__isnull=True,
            resubmission_deadline__gte=today,
            resubmission_deadline__lte=today + timedelta(days=days_before),
            application__case__case_status=RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION,
        )
        .order_by("resubmission_deadline", "pk")
    )
    due = []
    for record in records:
        case = record.application.case
        # Only the record of the stage that is waiting (older stages keep stale deadlines).
        if record.review_stage == case.current_stage:
            due.append(record)
    return due


def send_resubmission_reminder(screening_record):
    """One reminder email for a pending resubmission; stamps resubmission_reminder_sent_at.
    Sent from a scheduled command, so it switches to the applicant's portal language."""
    from ..notification_services import applicant_email_language

    with translation.override(applicant_email_language(screening_record.application)):
        return _send_resubmission_reminder(screening_record)


def _send_resubmission_reminder(screening_record):
    application = screening_record.application
    position = application.position
    path = reverse("applicant-resubmit", kwargs={"token": application.public_token})
    base_url = (getattr(settings, "APPLICANT_PORTAL_BASE_URL", "") or "").strip().rstrip("/")
    resubmit_url = f"{base_url}{path}" if base_url else path
    deadline = date_format(screening_record.resubmission_deadline, "F j, Y")
    subject = gettext("Reminder: send your corrected documents for %(title)s by %(date)s") % {
        "title": position.title,
        "date": deadline,
    }
    text_body = gettext(
        "Hello %(name)s,\n\n"
        "The recruitment team is still waiting for the corrected documents for your application "
        "for %(title)s (Case No. %(case)s). Please send them on or before %(date)s.\n\n"
        "Open this link to see which documents and why, then upload them:\n%(link)s\n\n"
        "DOH–CHD CALABARZON Recruitment Portal"
    ) % {
        "name": application.applicant_first_name or gettext("applicant"),
        "title": position.title,
        "case": application.case_number_short,
        "date": deadline,
        "link": resubmit_url,
    }
    try:
        html_body = render_to_string(
            "email/resubmission_reminder.html",
            {
                "application": application,
                "position_title": position.title,
                "deadline": screening_record.resubmission_deadline,
                "resubmit_url": resubmit_url,
                **email_branding_context("applicant"),
            },
        )
    except Exception:
        logger.exception("Failed to render the resubmission reminder HTML for application %s.", application.pk)
        html_body = ""
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[application.applicant_email],
    )
    if html_body:
        email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
    screening_record.resubmission_reminder_sent_at = timezone.now()
    screening_record.save(update_fields=["resubmission_reminder_sent_at", "updated_at"])
    audit.record_audit_event(
        application=application,
        actor=None,
        action=AuditLog.Action.RESUBMISSION_REMINDER_SENT,
        description="Reminded the applicant that their corrected documents are due soon.",
        metadata={"resubmission_deadline": screening_record.resubmission_deadline.isoformat()},
    )
    return screening_record


ABANDONED_DRAFT_DEFAULT_DAYS = 30
ABANDONED_DRAFT_ARCHIVE_TAG = "File removed: application never submitted and the vacancy closed"


def find_abandoned_drafts(older_than_days=ABANDONED_DRAFT_DEFAULT_DAYS, now=None):
    """Unsubmitted drafts that can no longer be submitted (the vacancy stopped accepting
    applications) and that nobody has touched for ``older_than_days`` days, and that still
    hold uploaded files."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=older_than_days)
    candidates = (
        RecruitmentApplication.objects.select_related("position")
        .filter(
            status=RecruitmentApplication.Status.DRAFT,
            submitted_at__isnull=True,
            updated_at__lt=cutoff,
        )
        .filter(
            Q(document_parts__isnull=False)
            | Q(evidence_items__artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION, evidence_items__size_bytes__gt=0)
        )
        .distinct()
        .order_by("pk")
    )
    return [application for application in candidates if not application.position.is_open_for_intake]


@transaction.atomic
def purge_abandoned_draft_files(application):
    """Remove the uploaded files of an abandoned draft. The draft itself, each file's name,
    size and SHA-256 fingerprint, and the audit trail are kept; only the file contents go.
    (Deleting the draft would also delete its audit history.)"""
    from ..models import ApplicantDocumentPart

    parts_deleted, _ = ApplicantDocumentPart.objects.filter(application=application).delete()
    items = EvidenceVaultItem.objects.filter(
        application=application,
        artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
        size_bytes__gt=0,
    )
    item_ids = list(items.values_list("pk", flat=True))
    items.update(
        nonce=b"",
        ciphertext=b"",
        size_bytes=0,
        is_archived=True,
        archive_tag=ABANDONED_DRAFT_ARCHIVE_TAG,
        archived_at=timezone.now(),
        updated_at=timezone.now(),
    )
    audit.record_audit_event(
        application=application,
        actor=None,
        action=AuditLog.Action.EVIDENCE_ARCHIVED,
        description=(
            "Removed the uploaded files of an application that was never submitted, after its "
            "vacancy closed (retention clean-up)."
        ),
        metadata={"evidence_ids": item_ids, "staging_parts_deleted": parts_deleted},
    )
    return len(item_ids) + parts_deleted


def issue_application_otp(
    application, actor=None, *, defer_delivery=True, enforce_cooldown=False, purpose="editing"
):
    # "editing" covers intake verification and resubmission (draft/returned
    # states only). "status_lookup" (REV-I) issues codes for any submitted
    # application — the code still only lands in the owner's inbox.
    if purpose == "editing":
        if not application.is_editable_by_applicant:
            raise ValueError(
                gettext("A verification code can only be issued while the application is in draft or "
                "awaiting the applicant's resubmission.")
            )
    elif application.submitted_at is None:
        raise ValueError(gettext("This application has not been submitted yet."))
    if enforce_cooldown and application.otp_requested_at:
        elapsed = timezone.now() - application.otp_requested_at
        cooldown_seconds = settings.APPLICATION_OTP_RESEND_COOLDOWN_SECONDS
        if elapsed.total_seconds() < cooldown_seconds:
            remaining = max(1, int(cooldown_seconds - elapsed.total_seconds()))
            raise ValueError(
                gettext("Please wait %(seconds)s seconds before requesting another code.") % {"seconds": remaining}
            )

    otp_code = _generate_otp_code()
    now = timezone.now()
    application.otp_hash = _hash_application_otp(application, otp_code)
    application.otp_requested_at = now
    application.otp_expires_at = now + timedelta(minutes=settings.APPLICATION_OTP_VALIDITY_MINUTES)
    application.otp_verified_at = None
    application.otp_attempt_count = 0
    application.save(
        update_fields=[
            "otp_hash",
            "otp_requested_at",
            "otp_expires_at",
            "otp_verified_at",
            "otp_attempt_count",
            "updated_at",
        ]
    )
    otp_expires_at = application.otp_expires_at

    def deliver_otp():
        _deliver_application_otp(
            application,
            otp_code,
            actor=actor,
            otp_expires_at=otp_expires_at,
        )

    connection = transaction.get_connection(using=application._state.db or "default")
    if defer_delivery and connection.in_atomic_block:
        transaction.on_commit(deliver_otp, using=connection.alias)
    else:
        deliver_otp()
    return otp_code


def _application_otp_max_attempts():
    return max(1, int(getattr(settings, "APPLICATION_OTP_MAX_ATTEMPTS", 5)))


def _record_application_otp_failure(application, actor, *, reason, locked=False):
    max_attempts = _application_otp_max_attempts()
    audit.record_audit_event(
        application=application,
        actor=actor or application.applicant,
        action=(
            AuditLog.Action.APPLICATION_OTP_LOCKED
            if locked
            else AuditLog.Action.APPLICATION_OTP_FAILED
        ),
        description=(
            "Applicant verification code locked after too many invalid attempts."
            if locked
            else "Applicant verification code verification failed."
        ),
        metadata={
            "reason": reason,
            "attempt_count": application.otp_attempt_count,
            "max_attempts": max_attempts,
        },
    )


def verify_application_otp(application, otp_code, actor=None, purpose="editing"):
    application = RecruitmentApplication.objects.get(pk=application.pk)
    actor = actor or application.applicant
    locked_error = gettext("Too many invalid verification attempts. Request a new code.")
    if purpose == "editing" and not application.is_editable_by_applicant:
        raise ValueError(gettext("This application was already submitted, so it can no longer be verified."))
    if not application.otp_hash or not application.otp_expires_at:
        _record_application_otp_failure(application, actor, reason="missing_code")
        raise ValueError(gettext("Request a verification code first."))
    if application.otp_expires_at < timezone.now():
        _record_application_otp_failure(application, actor, reason="expired")
        raise ValueError(gettext("The verification code has expired. Request a new code."))
    if application.otp_attempt_count >= _application_otp_max_attempts():
        _record_application_otp_failure(application, actor, reason="attempt_limit", locked=True)
        raise ValueError(locked_error)

    expected_hash = _hash_application_otp(application, otp_code)
    if not hmac.compare_digest(application.otp_hash, expected_hash):
        application.otp_attempt_count += 1
        application.save(update_fields=["otp_attempt_count", "updated_at"])
        locked = application.otp_attempt_count >= _application_otp_max_attempts()
        _record_application_otp_failure(application, actor, reason="invalid_code", locked=locked)
        if locked:
            raise ValueError(locked_error)
        tries_left = _application_otp_max_attempts() - application.otp_attempt_count
        raise ValueError(
            ngettext(
                "That code doesn't match. Check the latest email from us and try again. You have %(count)s try left.",
                "That code doesn't match. Check the latest email from us and try again. You have %(count)s tries left.",
                tries_left,
            )
            % {"count": tries_left}
        )

    application.otp_verified_at = timezone.now()
    application.otp_attempt_count = 0
    application.save(update_fields=["otp_verified_at", "otp_attempt_count", "updated_at"])
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_OTP_VERIFIED,
        description="Applicant verification code verified successfully.",
        metadata={"verified_at": application.otp_verified_at.isoformat()},
    )
    return application


@transaction.atomic
def start_public_application_draft(entry, cleaned_data):
    """Step 1 of the redesigned intake: find or create the applicant's draft from their name,
    e-mail and mobile number only, before the e-mail is verified.

    An existing unsubmitted draft for the same e-mail is returned UNCHANGED. Until the code
    sent to that inbox is verified, nobody has shown they own the address, so a stranger who
    types someone else's e-mail must not be able to rewrite that person's draft. The owner
    can correct their details on the application page once verified."""
    if not entry.is_open_for_intake:
        raise ValueError(gettext("This position is no longer accepting applications."))
    applicant_email = normalize_applicant_email(cleaned_data["email"])
    if RecruitmentApplication.objects.filter(
        position=entry,
        applicant_email__iexact=applicant_email,
        submitted_at__isnull=False,
    ).exclude(status__in=REAPPLICATION_ELIGIBLE_TERMINAL_STATUSES).exists():
        raise ValueError(
            gettext("You have already submitted an application for this position using this email address.")
        )

    applicant_user = get_portal_applicant_identity_by_email(applicant_email)
    application = None
    if applicant_user is not None:
        application = get_reusable_public_application_for_applicant(entry, applicant_user)
        if application and application.submitted_at:
            raise ValueError(
                gettext("You have already submitted an application for this position using this email address.")
            )
    if application is None:
        application = get_reusable_public_application_draft(entry, applicant_email)
    if application is not None:
        return application

    if applicant_user is None:
        applicant_user = create_portal_applicant_identity(
            first_name=cleaned_data["first_name"],
            last_name=cleaned_data["last_name"],
            email=applicant_email,
            phone=cleaned_data["phone"],
        )
    application = RecruitmentApplication(
        applicant=applicant_user,
        position=entry,
        status=RecruitmentApplication.Status.DRAFT,
        applicant_first_name=cleaned_data["first_name"],
        applicant_last_name=cleaned_data["last_name"],
        applicant_email=applicant_email,
        applicant_phone=cleaned_data["phone"],
        checklist_privacy_consent=bool(cleaned_data.get("privacy_consent")),
        # Plantilla: left unanswered until the applicant chooses on the application page.
        performance_rating_applicability=(
            RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            if entry.branch == PositionPosting.Branch.COS
            else ""
        ),
    )
    try:
        application.save()
    except IntegrityError as exc:
        if "unique_active_application_per_applicant_position" not in str(exc):
            raise
        raise ValueError(
            gettext("You already have an application for this position.")
        ) from exc
    audit.record_audit_event(
        application=application,
        actor=applicant_user,
        action=AuditLog.Action.APPLICATION_CREATED,
        description="Applicant started an accountless application (details only, e-mail not yet verified).",
        metadata={"public_token": str(application.public_token), "applicant_identity_id": applicant_user.id},
    )
    return application


def save_applicant_application_answers(application, cleaned_data):
    """Save the typed answers from the application page (after e-mail verification)."""
    if not application.is_editable_by_applicant or application.submitted_at:
        raise ValueError(gettext("This application has already been submitted and can no longer be changed."))
    # cleaned_data may hold only the fields that passed validation (a draft saves what it
    # can). A blank name or mobile number never overwrites the one already on file.
    for key, attribute in (
        ("first_name", "applicant_first_name"),
        ("last_name", "applicant_last_name"),
        ("phone", "applicant_phone"),
    ):
        if cleaned_data.get(key):
            setattr(application, attribute, cleaned_data[key])
    if "qualification_summary" in cleaned_data:
        application.qualification_summary = cleaned_data["qualification_summary"]
    if "cover_letter" in cleaned_data:
        application.cover_letter = cleaned_data["cover_letter"]
    application.performance_rating_applicability = (
        cleaned_data.get("performance_rating_applicability")
        or application.performance_rating_applicability
    )
    application.save(
        update_fields=[
            "applicant_first_name",
            "applicant_last_name",
            "applicant_phone",
            "qualification_summary",
            "cover_letter",
            "performance_rating_applicability",
            "updated_at",
        ]
    )
    _sync_portal_applicant_identity(
        application.applicant,
        first_name=application.applicant_first_name,
        last_name=application.applicant_last_name,
        email=application.applicant_email,
    )
    return application


def _route_for_submission(application):
    stage = workflow_map.intake_stage(application.branch, application.level)
    return workflow_map.stage_owner(stage, application.level), workflow_map.status_for_stage(stage)


@transaction.atomic
def submit_application(application, actor, *, email_verified_in_session=False):
    """``email_verified_in_session``: the redesigned intake verifies the email FIRST and then
    lets the applicant work on the application for as long as their browser session stays
    verified (it lapses after the normal inactivity timeout). The view passes True only when
    that session marker is present; the email must still have been verified for this draft.
    Without it (direct service calls), the stricter "code verified within its validity
    window" rule applies."""
    if application.applicant_id != actor.id:
        raise ValueError("Only the owning applicant can submit this application.")
    if not application.is_editable_by_applicant:
        raise ValueError(gettext("This application can no longer be submitted."))
    if not application.checklist_complete:
        raise ValueError(gettext("Tick the confirmation box before you submit."))
    missing_requirements = get_missing_required_applicant_document_requirements(application)
    if missing_requirements:
        # No title enumeration — a plantilla has many required slots and the
        # formal titles are long, so listing them produced a wall of text in
        # the finalize error banner. A count keeps it readable; the intake form
        # highlights exactly which slots are missing when the applicant returns.
        missing_count = len(missing_requirements)
        raise ValueError(
            ngettext(
                "Add all required documents before you submit. %(count)s required document is still missing.",
                "Add all required documents before you submit. %(count)s required documents are still missing.",
                missing_count,
            )
            % {"count": missing_count}
        )
    duplicate_document_groups = get_duplicate_applicant_document_groups(application)
    if duplicate_document_groups:
        # No enumerated title list — the intake form already flags the specific
        # slots; a short sentence keeps this safety-net error readable.
        raise ValueError(
            gettext("Some documents use the same file. Use a different file for each document, then submit.")
        )
    if not application.position.is_open_for_intake:
        raise ValueError(gettext("This position is no longer accepting applications."))
    if email_verified_in_session:
        if not application.otp_verified_at:
            raise ValueError(gettext("Verify your email before you submit."))
    elif not application.otp_is_currently_valid:
        if application.otp_expires_at and application.otp_expires_at < timezone.now():
            raise ValueError(gettext("Your email verification has expired. Request a new code, then submit."))
        raise ValueError(gettext("Verify your email before you submit."))

    previous_status = application.status
    previous_role = application.current_handler_role
    next_role, next_status = _route_for_submission(application)
    application.status = next_status
    application.current_handler_role = next_role
    application.submitted_at = timezone.now()
    application.submission_hash = common.generate_submission_hash(application)
    update_fields = [
        "reference_number",
        "tracking_code",
        "status",
        "current_handler_role",
        "submitted_at",
        "submission_hash",
        "branch",
        "level",
        "updated_at",
    ]
    # Case number and tracking code are guarded by unique constraints; on a
    # concurrent-submission collision, regenerate and retry.
    last_error = None
    for _ in range(IDENTIFIER_GENERATION_RETRIES):
        if not application.reference_number:
            application.reference_number = common.generate_case_number()
        if not application.tracking_code:
            application.tracking_code = common.generate_tracking_code()
        try:
            with transaction.atomic():
                application.save(update_fields=update_fields)
            last_error = None
            break
        except IntegrityError as exc:
            application.reference_number = None
            application.tracking_code = None
            last_error = exc
    if last_error is not None:
        raise last_error
    case = _upsert_recruitment_case_for_submission(
        application=application,
        actor=actor,
        next_role=next_role,
        next_status=next_status,
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_SUBMITTED,
        description="Applicant submitted the application.",
        metadata={"submission_hash": application.submission_hash, **routing._case_timeline_metadata(case)},
    )
    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.ROUTED,
        description=f"Application routed to {next_role}.",
        metadata={
            "status": next_status,
            "current_handler_role": next_role,
            **routing._case_timeline_metadata(case),
        },
    )
    routing.record_routing_history_event(
        application=application,
        actor=actor,
        route_type=RoutingHistory.RouteType.INITIAL,
        description=f"Application routed to {next_role}.",
        recruitment_case=case,
        from_handler_role=previous_role,
        to_handler_role=next_role,
        from_status=previous_status,
        to_status=next_status,
        from_stage="",
        to_stage=case.current_stage,
    )
    queue_submission_acknowledgment_notification(application, actor=actor)
    return application


@transaction.atomic
def submit_document_resubmission(application, actor, uploaded_files, *, email_verified_in_session=False):
    """Applicant re-uploads the flagged documents from Track Application. Re-uploads land on the
    applicant-intake version chain (so they replace the originals and stay in the document map),
    the flagged screening rows reopen for re-review while already-Meets rows stay accepted, and
    the case returns to the reviewing role as ACTIVE. This bypasses the full submit_application
    gauntlet (intake-open, full checklist, cross-slot duplicate-digest) because it is a scoped
    fix of specific flagged documents, not a fresh submission.

    ``uploaded_files`` maps each flagged document code to an UploadedFile, or to a list of
    them: one file is stored as-is, several are combined (in order) into one PDF.

    ``email_verified_in_session`` is set by the portal when this browser already proved the
    applicant's email recently (My Applications or the status-page code), so the applicant is
    not asked for yet another code.
    """
    from ..applicant_documents import combine_uploaded_files


    if application.applicant_id != getattr(actor, "id", None):
        raise ValueError(gettext("Only the applicant may resubmit documents for this application."))
    case = getattr(application, "case", None)
    if case is None or case.case_status != RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION:
        raise ValueError(gettext("This application is not awaiting a document resubmission."))
    review_stage = case.current_stage
    screening_record, flagged = screening._flagged_resubmission_reviews(application, stage=review_stage)
    if not flagged:
        raise ValueError(gettext("There are no documents awaiting resubmission."))
    if screening_record.resubmission_is_overdue:
        raise ValueError(gettext("The resubmission deadline has passed; please contact the recruitment team."))
    if not (email_verified_in_session or application.otp_is_currently_valid):
        raise ValueError(gettext("Verify your email with a fresh code before resubmitting your documents."))

    requirements = {req.code: req for req in get_applicant_document_requirements(application)}
    flagged_codes = [row.document_key for row in flagged]
    if any(not uploaded_files.get(code) for code in flagged_codes):
        raise ValueError(gettext("Upload a file for each flagged document before resubmitting."))

    for code in flagged_codes:
        requirement = requirements.get(code)
        files = uploaded_files[code]
        if not isinstance(files, (list, tuple)):
            files = [files]
        if len(files) == 1 or requirement is None:
            evidence_vault.upload_evidence_item(
                application=application,
                actor=actor,
                label=requirement.title if requirement else code,
                uploaded_file=files[0],
                document_key=code,
                artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
                artifact_type=common.ARTIFACT_TYPE_APPLICANT_DOCUMENT,
                stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
            )
            continue
        filename, content_type, raw_bytes, part_digests = combine_uploaded_files(requirement, files)
        evidence = evidence_vault.store_generated_evidence_item(
            application=application,
            actor=actor,
            label=requirement.title,
            filename=filename,
            raw_bytes=raw_bytes,
            content_type=content_type,
            artifact_type=common.ARTIFACT_TYPE_APPLICANT_DOCUMENT,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            stage=EvidenceVaultItem.Stage.APPLICANT_INTAKE,
            document_key=code,
        )
        audit.record_audit_event(
            application=application,
            actor=actor,
            action=AuditLog.Action.EVIDENCE_UPLOADED,
            description=f"Combined {len(files)} resubmitted files into one PDF for '{requirement.title}'.",
            metadata={"document_key": code, "evidence_id": evidence.id, "part_digests": part_digests},
        )

    # Reopen the finalized screening record and reset ONLY the flagged rows for re-review;
    # already-Meets rows stay accepted. Clearing is_finalized first lifts the per-row freeze.
    if screening_record.is_finalized:
        screening_record.is_finalized = False
        screening_record.finalized_by = None
        screening_record.finalized_at = None
    screening_record.resubmission_requested_at = None
    screening_record.resubmission_deadline = None
    # Mid-review guard, not full_clean: an RD-1 "Return to applicant" record deliberately
    # carries NO qualification verdict (forcing one on flagged documents is the fake-verdict
    # pattern RD-1 removed), and this save returns it to exactly that mid-review state.
    # full_clean() would reject the blank outcome and 500 the applicant's resubmission —
    # the finalize path still full_cleans, so the verdict stays required where it matters.
    screening_record.validate_unique()
    screening_record.save()
    for row in flagged:
        row.status = ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
        row.full_clean()
        row.save(update_fields=["status", "updated_at"])

    # Return the case to the reviewing role for re-review and restart its stage clock.
    reviewing_role = case.current_handler_role
    routing.transition_case(
        application,
        actor,
        to_status=routing._application_status_from_stage(review_stage),
        to_role=reviewing_role,
        restart_stage_clock=True,
        route_type=RoutingHistory.RouteType.REOPEN,
        description="Applicant resubmitted flagged documents; back with the reviewer for re-review.",
    )
    case.reopened_at = timezone.now()
    case.save(update_fields=["reopened_at", "updated_at"])

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.APPLICATION_SUBMITTED,
        description="Applicant resubmitted flagged documents for re-review.",
        metadata={
            "resubmitted_document_keys": flagged_codes,
            "review_stage": review_stage,
        },
    )
    case_notices._emit_resubmission_received_notification(
        application, actor, reviewing_role, document_count=len(flagged_codes)
    )
    queue_document_resubmission_received_notification(
        application,
        actor=actor,
        document_titles=[
            requirements[code].title if code in requirements else code for code in flagged_codes
        ],
    )
    return application
