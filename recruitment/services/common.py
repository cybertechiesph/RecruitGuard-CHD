"""Shared constants, decimal and score helpers, identifier generators and the
workflow row lock. Imports nothing else from this package, so any module may use it
at import time.
"""

import hashlib
import logging
import re
import secrets
from functools import wraps
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from ..models import (
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)


logger = logging.getLogger("recruitment.services")


RESCIND_CUTOFF_MESSAGE = (
    "The appointment has been issued. A revocation at this point is handled as a "
    "separation, not a rescission."
)


EXPORT_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.APPOINTING_AUTHORITY,
}
SCREENING_REVIEW_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}
SCREENING_STAGES = {
    RecruitmentCase.Stage.SECRETARIAT_REVIEW,
    RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
}
PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL = {
    PositionPosting.Level.LEVEL_1: {RecruitmentUser.Role.SECRETARIAT},
    PositionPosting.Level.LEVEL_2: {RecruitmentUser.Role.HRM_CHIEF},
}
ARTIFACT_TYPE_APPLICANT_DOCUMENT = "applicant_document"
ARTIFACT_TYPE_EXAM_EVIDENCE = "exam_supporting_evidence"
ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT = "comparative_assessment_report"
# WF-04 (client decision): the Secretariat generates the CAR for BOTH levels. If the client
# wants the HRM Chief to retain Level-2 access too, make Level 2 the union
# {SECRETARIAT, HRM_CHIEF} — and mirror it in ComparativeAssessmentReport.clean().
PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL = {
    PositionPosting.Level.LEVEL_1: {RecruitmentUser.Role.SECRETARIAT},
    PositionPosting.Level.LEVEL_2: {RecruitmentUser.Role.SECRETARIAT},
}
CAR_LABEL = "Comparative Assessment Report"


ENTRY_LIST_FILTERS = ("open", "closed", "all")


def _workflow_lock_target(args, kwargs, *names):
    if args:
        return args[0]
    for name in names:
        if kwargs.get(name) is not None:
            return kwargs[name]
    return None


def locks_workflow_row(scope):
    """Serialize workflow writes. Two people (or one double click) finalizing the same step
    used to race: both read "not finalized yet", both wrote, and the loser hit a unique
    constraint as an error page. The decorated function now locks the row first and
    re-reads it, so the second request waits, then meets the ordinary "already finalized"
    checks with a plain message.

    ``scope`` is "case" (lock the application's case), "vacancy" (the first argument is the
    vacancy) or "vacancy_of_application" (pool-wide steps called with one application).
    Always sits under @transaction.atomic. On SQLite (tests) the lock is a no-op."""

    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            target = _workflow_lock_target(args, kwargs, "application", "posting", "entry")
            if target is not None and transaction.get_connection().in_atomic_block:
                if scope == "case":
                    case_ids = list(
                        RecruitmentCase.objects.select_for_update()
                        .filter(application_id=target.pk)
                        .values_list("pk", flat=True)
                    )
                    if case_ids:
                        target.refresh_from_db()
                else:
                    posting = target if scope == "vacancy" else target.position
                    list(PositionPosting.objects.select_for_update().filter(pk=posting.pk).values_list("pk", flat=True))
                    if scope == "vacancy_of_application":
                        target.refresh_from_db()
            return func(*args, **kwargs)

        wrapper.locks_workflow_row = True
        return wrapper

    return decorate


ARTIFACT_TYPE_LETTER_OF_INTENT = "letter_of_intent"


ARTIFACT_TYPE_WITHDRAWAL_REQUEST = "withdrawal_request"


def generate_submission_hash(application):
    payload = "|".join(
        [
            application.reference_number or "pending-reference",
            str(application.applicant_id),
            str(application.position_id),
            application.status,
            timezone.now().isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Tracking-code alphabet: Crockford Base32 (no I, L, O, U — lookalikes are
# impossible by construction) with the remaining vowels A and E also removed
# so a random code can never form a word.
TRACKING_CODE_ALPHABET = "0123456789BCDFGHJKMNPQRSTVWXYZ"
CASE_NUMBER_PATTERN = re.compile(r"^CASE-(?P<year>\d{4})-(?P<sequence>\d{4,})$")


def generate_case_number(year=None):
    """Next CASE-<year>-<seq> candidate. Callers must save under the field's
    unique constraint and retry on IntegrityError — this read is not locked."""
    year = year or timezone.localdate().year
    prefix = f"CASE-{year:04d}-"
    highest = 0
    references = RecruitmentApplication.objects.filter(
        reference_number__startswith=prefix
    ).values_list("reference_number", flat=True)
    for reference in references:
        match = CASE_NUMBER_PATTERN.fullmatch(reference or "")
        if match:
            highest = max(highest, int(match.group("sequence")))
    # Padding is cosmetic: sequence 10000 simply prints wider.
    return f"{prefix}{highest + 1:04d}"


def generate_tracking_code():
    def block():
        return "".join(secrets.choice(TRACKING_CODE_ALPHABET) for _ in range(4))

    return f"TRK-{block()}-{block()}"


def _decimal_string(value):
    if value is None:
        return ""
    return str(value)


def _optional_decimal(value):
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _quantize_score(value):
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
