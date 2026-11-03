"""Comparative Assessment Report: ETE ratings, CAR readiness, generation, ranking
and the CAR permissions.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from ..models import (
    ApplicationETERating,
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    TOP_FIVE_SELECTION_LIMIT,
    EvidenceVaultItem,
    format_weight_percentage,
    InterviewSession,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
)
from . import (
    audit,
    case_notices,
    clock,
    common,
    evidence_vault,
    intake,
    routing,
    screening,
    selection,
)


CAR_REVIEW_STAGE = RecruitmentCase.Stage.HRMPSB_REVIEW


def car_item_is_within_top_five(item):
    """SL-08: whether this candidate is inside the top five for deep-selection purposes.

    Under competition ranking a tied band shares one ordinal, so a tie that straddles fifth
    place (…4, 5, 5, 7) leaves every member of the band at rank 5 — inside. Comparing the
    ordinal is therefore the whole rule; no separate band arithmetic is needed. An unranked
    row (no computed score) is never inside."""
    rank = getattr(item, "rank_order", None)
    return rank is not None and rank <= TOP_FIVE_SELECTION_LIMIT


def car_top_rank_case_ids(report):
    """Every case sharing the CAR's top computed rank. SL-08: a tie at rank 1 has more than
    one, and picking any of them is not a deviation from the board's ordering."""
    return set(
        report.items.filter(rank_order=1).values_list("recruitment_case_id", flat=True)
    )


def user_can_prepare_plantilla_car(user, application):
    case = getattr(application, "case", None)
    if not case or case.is_stage_locked:
        return False
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
        or case.current_stage != CAR_REVIEW_STAGE
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or routing.get_current_workflow_section(application) != "car"
    ):
        return False
    return user.role in common.PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.get(application.level, set())


def plantilla_car_preparer_role_label(application):
    """Human label for the role(s) that prepare the Plantilla CAR at this case's level
    (Secretariat for both levels — WF-04). Used only to explain who the HRMPSB board is
    waiting on — it does not grant or change any access."""
    roles = common.PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.get(application.level, set())
    labels = [RecruitmentUser.Role(role).label for role in sorted(roles)]
    return ", ".join(labels) if labels else "the assigned office"


def application_awaiting_car_preparation(user, application):
    """True when this viewer is looking at a Plantilla HRMPSB case whose CAR is still being
    prepared by another role: the viewer can't prepare it and no CAR (draft or final) exists yet.
    Drives a "CAR in preparation" state instead of a blank card, and a queue hint — it never
    changes queue membership or access (the case stays assigned to the HRMPSB member)."""
    case = getattr(application, "case", None)
    if not case:
        return False
    if (
        application.branch != PositionPosting.Branch.PLANTILLA
        or application.status != RecruitmentApplication.Status.HRMPSB_REVIEW
        or case.current_stage != CAR_REVIEW_STAGE
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or routing.get_current_workflow_section(application) != "car"
    ):
        return False
    if user_can_prepare_plantilla_car(user, application):
        return False
    return get_comparative_assessment_report(application) is None


def panel_waits_on_car(user, application):
    """HRMPSB review P2-3: once the interview batch is finalized an HRMPSB member has nothing
    left to do in the system for the case: its CAR preparer writes the CAR and the board
    deliberates off-system. The case stays in the member's queue and readable (the read-only
    CAR card), but it is not counted as their work. Returns the preparer's label, or ""."""
    case = getattr(application, "case", None)
    if (
        getattr(user, "role", None) != RecruitmentUser.Role.HRMPSB_MEMBER
        or not case
        or application.branch != PositionPosting.Branch.PLANTILLA
        or case.current_stage != CAR_REVIEW_STAGE
        or case.case_status != RecruitmentCase.CaseStatus.ACTIVE
        or routing.get_current_workflow_section(application) != "car"
    ):
        return ""
    return plantilla_car_preparer_role_label(application)


def car_is_finalized_for_current_stage(application):
    report = get_comparative_assessment_report(application)
    return bool(report and report.is_finalized)


def _calculate_preliminary_assessment_score(
    posting,
    document_review_score,
    exam_score,
    interview_score,
    assessment_weights=None,
):
    # The CAR component weights are now per-vacancy (VacancyAssessmentWeights), resolved from
    # the posting; a vacancy without a row falls back to the office defaults (40/20/40).
    if any(value in (None, "") for value in (document_review_score, exam_score, interview_score)):
        return None
    weights = assessment_weights or posting.assessment_weights_or_default
    total = (
        (Decimal(str(document_review_score)) * weights.ete_fraction)
        + (Decimal(str(exam_score)) * weights.exam_component_fraction)
        + (Decimal(str(interview_score)) * weights.interview_fraction)
    )
    return common._quantize_score(total)


def _assessment_weight_display(posting, assessment_weights=None):
    weights = assessment_weights or posting.assessment_weights_or_default
    return (
        f"Document review {format_weight_percentage(weights.ete_weight)}%, "
        f"exam {format_weight_percentage(weights.exam_weight)}%, "
        f"interview {format_weight_percentage(weights.interview_weight)}%."
    )


def get_comparative_assessment_report(application, stage=None, include_returned=False):
    review_stage = stage or routing.get_current_review_stage(application)
    queryset = ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
    )
    if not include_returned:
        queryset = queryset.filter(is_returned=False)
    return queryset.order_by(
        "-version_number",
        "-is_finalized",
        "-finalized_at",
        "-created_at",
    ).first()


def get_latest_draft_comparative_assessment_report(application, stage=None):
    review_stage = stage or routing.get_current_review_stage(application)
    return ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
        is_finalized=False,
        is_returned=False,
    ).order_by(
        "-version_number",
        "-created_at",
    ).first()


def get_latest_finalized_comparative_assessment_report(application):
    return ComparativeAssessmentReport.objects.select_related(
        "generated_by",
        "finalized_by",
        "returned_by",
        "evidence_item",
    ).filter(
        recruitment_entry=application.position,
        is_finalized=True,
        is_returned=False,
    ).order_by("-version_number", "-finalized_at", "-created_at").first()


def get_comparative_assessment_report_items_for_report(report):
    if not report:
        return ComparativeAssessmentReportItem.objects.none()
    return report.items.select_related(
        "recruitment_case",
        "recruitment_case__application",
        "deliberation_record",
        # SL-08: tied rows share an ordinal, so the secondary key has to be deterministic.
        # Case reference is display order only — it never changes the rank.
    ).order_by("rank_order", "recruitment_case__application__reference_number", "id")


def user_can_manage_comparative_assessment_report(user, application):
    current_stage = routing.get_current_review_stage(application)
    if application.branch != PositionPosting.Branch.PLANTILLA:
        return False
    if current_stage != CAR_REVIEW_STAGE:
        return False
    if routing.get_current_workflow_section(application) != "car":
        return False
    return user_can_prepare_plantilla_car(user, application)


def get_entry_finalized_car(posting):
    """N1: the vacancy's current finalized, non-returned CAR. The report is per vacancy, so
    the filing copy is addressable by vacancy — which is also how the office asks for it."""
    return (
        ComparativeAssessmentReport.objects.select_related(
            "generated_by", "finalized_by", "recommended_case", "recruitment_entry"
        )
        .filter(
            recruitment_entry=posting,
            review_stage=CAR_REVIEW_STAGE,
            is_finalized=True,
            is_returned=False,
        )
        .order_by("-version_number")
        .first()
    )


def user_can_generate_entry_car_filing_copy(user, posting):
    """N1: the role that prepares this vacancy's CAR may print its filing copy.

    Deliberately entry-scoped and not gated on any individual case still being open to that
    user. Finalizing the CAR routes every candidate straight to the Appointing Authority,
    at which point the Secretariat can no longer open those cases at all — so a case-scoped
    permission would hide the sheet exactly when it has to be printed and signed."""
    if posting.branch != PositionPosting.Branch.PLANTILLA:
        return False
    if user.role not in common.PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.get(posting.level, set()):
        return False
    return bool(get_entry_finalized_car(posting))


def user_can_generate_car_filing_copy(user, application):
    """N1: who may generate the named filing copy of a finalized CAR.

    The role that prepares the CAR for this vacancy's level — the Secretariat — but
    deliberately NOT gated on the case still sitting at the CAR step or still being in that
    user's queue. Finalizing the CAR routes every candidate straight to the Appointing
    Authority, so by the moment the sheet needs printing for wet-ink signature the
    Secretariat has already lost both the editing window and the case from their queue.
    Gating on either would put the filing copy out of reach exactly when it is needed —
    which is the re-typing this finding exists to stop."""
    if application.branch != PositionPosting.Branch.PLANTILLA:
        return False
    if user.role not in common.PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL.get(
        application.level, set()
    ):
        return False
    return bool(get_latest_finalized_comparative_assessment_report(application))


def get_application_ete_rating(application):
    return getattr(application, "ete_rating", None)


# WF-05 (PENDING CLIENT CONFIRMATION): the composition of the three ETE components
# inside the CAR's 40%, per branch — matching the policy text the screening screen
# already displayed. Weights are (education, training, experience).
ETE_COMPONENT_WEIGHTS_BY_BRANCH = {
    PositionPosting.Branch.PLANTILLA: (Decimal("0.40"), Decimal("0.30"), Decimal("0.30")),
    PositionPosting.Branch.COS: (Decimal("0.30"), Decimal("0.30"), Decimal("0.40")),
}
ETE_COMPONENT_LABELS = ("education", "training", "experience")


def set_application_ete_rating(application, actor, rating, *, components=None):
    """Persist the Secretariat's manual ETE rating for a candidate. The CAR's ETE
    (40%) component uses this value once it is set; until then the CAR falls back to
    the screening score. ``components`` optionally carries the WF-05 sub-scores and
    basis lines (education/training/experience)."""
    record = getattr(application, "ete_rating", None)
    if record is None:
        record = ApplicationETERating(application=application)
    record.rating = rating
    if components:
        record.education_score = components["education_score"]
        record.training_score = components["training_score"]
        record.experience_score = components["experience_score"]
        record.education_basis = components["education_basis"]
        record.training_basis = components["training_basis"]
        record.experience_basis = components["experience_basis"]
    record.recorded_by = actor
    # SL-08A: keep what this rating was judged against. A later QS edit or document
    # resubmission must not silently rewrite the basis of a rating already given.
    record.source_facts_snapshot = build_ete_source_facts_snapshot(application)
    record.full_clean()
    record.save()
    return record


@transaction.atomic
def apply_car_ete_ratings(application, actor, posted):
    """Persist per-candidate ETE ratings posted from the CAR draft screen.

    WF-05 inputs: ``ete_education_<case_id>``, ``ete_training_<case_id>``,
    ``ete_experience_<case_id>`` (0–100 each) plus a required one-line basis per
    component (``ete_<component>_basis_<case_id>``). The stored ``rating`` is the
    branch-weighted composition of the three. An incomplete row (missing score or
    blank basis) blocks the draft update with a plain message.

    The legacy single ``ete_<case_id>`` input is still honored for old callers;
    blank or out-of-range legacy values are skipped."""
    if not user_can_manage_comparative_assessment_report(actor, application):
        return
    weights = ETE_COMPONENT_WEIGHTS_BY_BRANCH.get(
        application.branch,
        ETE_COMPONENT_WEIGHTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA],
    )

    component_case_ids = set()
    for key in posted.keys():
        if key.startswith("ete_education_") and not key.startswith("ete_education_basis_"):
            try:
                component_case_ids.add(int(key[len("ete_education_"):]))
            except ValueError:
                continue

    for case_id in sorted(component_case_ids):
        scores_raw = {
            label: (posted.get(f"ete_{label}_{case_id}") or "").strip()
            for label in ETE_COMPONENT_LABELS
        }
        bases = {
            label: (posted.get(f"ete_{label}_basis_{case_id}") or "").strip()
            for label in ETE_COMPONENT_LABELS
        }
        if not any(scores_raw.values()) and not any(bases.values()):
            continue  # untouched row on the draft
        case = (
            RecruitmentCase.objects.select_related("application")
            .filter(pk=case_id, application__position=application.position)
            .first()
        )
        if case is None:
            continue
        # REV-B: this error renders on the blinded CAR screen — identify the
        # row by candidate code, not name.
        candidate_name = case.application.candidate_code_label
        missing_scores = [label for label in ETE_COMPONENT_LABELS if not scores_raw[label]]
        if missing_scores:
            raise ValueError(
                f"Enter {candidate_name}'s education, training, and experience scores "
                f"together (missing: {', '.join(missing_scores)})."
            )
        scores = {}
        for label in ETE_COMPONENT_LABELS:
            try:
                value = Decimal(scores_raw[label])
            except ArithmeticError:
                raise ValueError(
                    f"{candidate_name}'s {label} score must be a number between 0 and 100."
                )
            if value < 0 or value > 100:
                raise ValueError(
                    f"{candidate_name}'s {label} score must be between 0 and 100."
                )
            scores[label] = value
        missing_bases = [label for label in ETE_COMPONENT_LABELS if not bases[label]]
        if missing_bases:
            raise ValueError(
                f"Add the one-line basis for {candidate_name}'s "
                f"{', '.join(missing_bases)} rating"
                f"{'s' if len(missing_bases) != 1 else ''} — each ETE component needs "
                "its basis on record."
            )
        rating = (
            scores["education"] * weights[0]
            + scores["training"] * weights[1]
            + scores["experience"] * weights[2]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        set_application_ete_rating(
            case.application,
            actor,
            rating,
            components={
                "education_score": scores["education"],
                "training_score": scores["training"],
                "experience_score": scores["experience"],
                "education_basis": bases["education"],
                "training_basis": bases["training"],
                "experience_basis": bases["experience"],
            },
        )

    for key in list(posted.keys()):
        if not key.startswith("ete_") or key.startswith(
            ("ete_education_", "ete_training_", "ete_experience_")
        ):
            continue
        raw = (posted.get(key) or "").strip()
        if raw == "":
            continue
        try:
            case_id = int(key[len("ete_"):])
            rating = Decimal(raw)
        except (ValueError, ArithmeticError):
            continue
        if rating < 0 or rating > 100:
            continue
        if case_id in component_case_ids:
            continue  # the component inputs above are authoritative for this candidate
        case = (
            RecruitmentCase.objects.select_related("application")
            .filter(pk=case_id, application__position=application.position)
            .first()
        )
        if case:
            set_application_ete_rating(case.application, actor, rating)


def _car_candidate_rows(
    recruitment_entry,
    review_stage,
    required_draft=None,
    assessment_weights=None,
):
    # The finalized CAR is computed directly from each active candidate's finalized
    # screening/exam/interview outputs and ranked by the assessment score. There is no
    # separate HRMPSB deliberation/ranking step for Plantilla anymore, so the final
    # rows are the same computed-and-ranked rows as the draft. ``required_draft`` is
    # accepted for call-site compatibility; the draft's existence is enforced by the
    # caller before finalizing.
    rows = _car_draft_candidate_rows(
        recruitment_entry,
        review_stage,
        assessment_weights=assessment_weights,
    )
    # SL-03: unlike the draft, the finalized report must be arithmetically defensible.
    _assert_car_rows_are_complete(rows)
    return rows


def _active_plantilla_cases_for_entry(recruitment_entry, review_stage):
    return list(
        RecruitmentCase.objects.select_related("application", "application__position")
        .filter(
            application__position=recruitment_entry,
            application__branch=PositionPosting.Branch.PLANTILLA,
            application__status=RecruitmentApplication.Status.HRMPSB_REVIEW,
            current_stage=review_stage,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
            is_stage_locked=False,
        )
        .order_by("application__reference_number", "application__created_at", "id")
    )


def _car_draft_candidate_rows(recruitment_entry, review_stage, assessment_weights=None):
    cases = _active_plantilla_cases_for_entry(recruitment_entry, review_stage)
    if not cases:
        raise ValueError("No active Plantilla candidates are available for CAR draft preparation.")

    rows = []
    incomplete_references = []
    for case in cases:
        candidate_application = case.application
        summary = selection.build_deliberation_consolidation(candidate_application)["summary"]
        has_required_outputs = (
            summary.get("finalized_screening_count", 0) > 0
            and summary.get("finalized_exam_count", 0) > 0
            and summary.get("finalized_interview_count", 0) > 0
        )
        if not has_required_outputs:
            incomplete_references.append(candidate_application.reference_label)
            continue

        document_review_score = summary.get("latest_document_review_score", "")
        exam_score = summary.get("latest_exam_score", "")
        interview_average_score = summary.get("latest_interview_average", "")
        # The CAR's ETE (40%) component uses the Secretariat's manual ETE rating when
        # one has been entered; until then it falls back to the screening score.
        manual_ete = getattr(candidate_application, "ete_rating", None)
        ete_rating = str(manual_ete.rating) if manual_ete else ""
        ete_component = ete_rating or document_review_score
        assessment_score = _calculate_preliminary_assessment_score(
            recruitment_entry,
            ete_component,
            exam_score,
            interview_average_score,
            assessment_weights=assessment_weights,
        )
        rows.append(
            {
                "application": candidate_application,
                "recruitment_case": case,
                "deliberation_record": None,
                "rank_order": None,
                "preliminary_rank_order": None,
                "qualification_outcome": summary.get("latest_qualification_outcome", ""),
                "document_review_score": document_review_score,
                "ete_rating": ete_rating,
                "finalized_document_review_count": summary.get("finalized_screening_count", 0),
                "exam_status": summary.get("latest_exam_status", ""),
                "exam_score": exam_score,
                "exam_components": summary.get("latest_exam_components", ""),
                "finalized_exam_count": summary.get("finalized_exam_count", 0),
                "interview_average_score": interview_average_score,
                "finalized_interview_count": summary.get("finalized_interview_count", 0),
                "assessment_score": assessment_score,
                "recommendation": "",
                "decision_support_summary": "",
                "ranking_notes": "",
            }
        )

    if incomplete_references:
        pending_references = "; ".join(incomplete_references)
        raise ValueError(
            "Finalize screening, exam, and interview outputs for all active applicants before preparing the CAR draft. "
            f"Pending: {pending_references}."
        )

    scored_rows = sorted(
        [row for row in rows if row["assessment_score"] is not None],
        key=lambda row: (
            -row["assessment_score"],
            row["application"].reference_number or row["application"].reference_label,
        ),
    )
    # SL-08: competition ranking (1, 2, 2, 4). Equal totals are a tie, and a tie is what the
    # scores say. Sorting by case reference stabilises the display order, but a case number
    # is not a merit distinction — using it to break ties turned an arbitrary clerical fact
    # into a ranking the CAR could not defend, and could push one of two equal candidates
    # outside the top five.
    previous_score = None
    previous_rank = 0
    for index, row in enumerate(scored_rows, start=1):
        if previous_score is not None and row["assessment_score"] == previous_score:
            row["preliminary_rank_order"] = previous_rank
        else:
            row["preliminary_rank_order"] = index
            previous_rank = index
        previous_score = row["assessment_score"]
        row["is_tied"] = False
    scores = [row["assessment_score"] for row in scored_rows]
    for row in scored_rows:
        row["is_tied"] = scores.count(row["assessment_score"]) > 1

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row["preliminary_rank_order"] is None,
            row["preliminary_rank_order"] or 0,
            row["application"].reference_number or row["application"].reference_label,
        ),
    )
    for row in ordered_rows:
        # SL-03: a row with no computed assessment score gets no rank. Handing an ordinal
        # to an unscored candidate is what let one appear as rank 1 and be recommended to
        # the Appointing Authority with nothing behind 40% of the formula.
        row["rank_order"] = row["preliminary_rank_order"]
    return ordered_rows


CAR_COMPONENT_LABELS = (
    ("ete_component", "ETE"),
    ("exam_score", "examination"),
    ("interview_average_score", "interview"),
    ("assessment_score", "overall score"),
)


def _car_row_missing_components(row):
    """Which parts of the CAR formula this candidate is missing, in reading order."""
    values = {
        "ete_component": row.get("ete_rating") or row.get("document_review_score"),
        "exam_score": row.get("exam_score"),
        "interview_average_score": row.get("interview_average_score"),
        "assessment_score": row.get("assessment_score"),
    }
    return [
        label
        for key, label in CAR_COMPONENT_LABELS
        if values[key] in (None, "")
    ]


def _assert_car_rows_are_complete(rows):
    """SL-03: refuse to finalize a CAR that computes from a blank.

    The office's own CAR spreadsheet fails loudly here — ``AVERAGE()`` over an empty rater
    range returns #DIV/0! and poisons the overall — so this restores paper behaviour rather
    than inventing a rule. Drafts stay permissive; only finalization is gated."""
    problems = []
    for row in rows:
        missing = _car_row_missing_components(row)
        if missing:
            problems.append(
                f"{row['application'].reference_label} is missing the "
                f"{', '.join(missing)}"
            )
    if problems:
        raise ValueError(
            "Every candidate needs a complete score before the CAR can be finalized. "
            + "; ".join(problems)
            + "."
        )


def get_comparative_assessment_readiness(application):
    readiness = {
        "is_applicable": False,
        "has_draft": False,
        "can_prepare_draft": False,
        "can_finalize": False,
        "draft_candidate_count": 0,
        "draft_candidate_cases": [],
        "final_candidate_count": 0,
        "prepare_block_message": "",
        "finalize_block_message": "",
        "latest_draft_id": None,
    }
    case = getattr(application, "case", None)
    if (
        not case
        or application.branch != PositionPosting.Branch.PLANTILLA
        or case.current_stage != CAR_REVIEW_STAGE
    ):
        return readiness

    readiness["is_applicable"] = True
    if (
        screening.application_requires_finalized_applicant_pool(application)
        and not screening.application_has_finalized_applicant_pool(application)
    ):
        message = screening.get_applicant_pool_finalization_block_message(application)
        readiness["prepare_block_message"] = message
        readiness["finalize_block_message"] = message
        return readiness

    try:
        draft_rows = _car_draft_candidate_rows(application.position, case.current_stage)
    except ValueError as exc:
        readiness["prepare_block_message"] = str(exc)
    else:
        readiness["can_prepare_draft"] = True
        readiness["draft_candidate_count"] = len(draft_rows)
        # So the ETE boxes can be filled before the first draft exists.
        readiness["draft_candidate_cases"] = [row["recruitment_case"] for row in draft_rows]

    latest_draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=case.current_stage,
    )
    readiness["has_draft"] = latest_draft_report is not None
    readiness["latest_draft_id"] = latest_draft_report.id if latest_draft_report else None
    if not latest_draft_report:
        readiness["finalize_block_message"] = (
            "Prepare the CAR draft before finalizing the Comparative Assessment Report."
        )
        return readiness

    try:
        final_rows = _car_candidate_rows(
            application.position,
            case.current_stage,
            required_draft=latest_draft_report,
        )
    except ValueError as exc:
        readiness["finalize_block_message"] = str(exc)
    else:
        readiness["can_finalize"] = True
        readiness["final_candidate_count"] = len(final_rows)
    return readiness


@transaction.atomic
def autosave_comparative_assessment_report_notes(application, actor, cleaned_data):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a Comparative Assessment Report can be autosaved.")
    if (
        screening.application_requires_finalized_applicant_pool(application)
        and not screening.application_has_finalized_applicant_pool(application)
    ):
        raise ValueError(screening.get_applicant_pool_finalization_block_message(application))
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for CAR preparation."
        )
    if application.case.current_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError(
            "The Comparative Assessment Report is available only for Plantilla cases at the HRMPSB step."
        )

    draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=application.case.current_stage,
    )
    if draft_report is None:
        return None

    summary_notes = cleaned_data.get("summary_notes") or ""
    if draft_report.summary_notes != summary_notes:
        draft_report.summary_notes = summary_notes
        draft_report.full_clean()
        draft_report.save(update_fields=["summary_notes", "updated_at"])
    return draft_report


def _resolve_car_recommendation(raw_case_id, candidate_rows, recommendation_notes):
    """Return ``(recommended_case_id, recommends_none)`` for the board's choice."""
    if raw_case_id == "none":
        if not (recommendation_notes or "").strip():
            raise ValueError(
                "Explain why the board recommends no candidate before finalizing the CAR."
            )
        return None, True

    pool_case_ids = {row["recruitment_case"].id for row in candidate_rows}
    try:
        case_id = int(raw_case_id)
    except (TypeError, ValueError):
        raise ValueError(
            "Record the HRMPSB board's recommendation before finalizing the CAR."
        )
    if case_id not in pool_case_ids:
        raise ValueError("The recommended applicant must be one of this vacancy's candidates.")
    return case_id, False


@transaction.atomic
@common.locks_workflow_row("vacancy_of_application")
def generate_comparative_assessment_report(application, actor, cleaned_data, finalize=False):
    if not hasattr(application, "case"):
        raise ValueError("A case must exist before a Comparative Assessment Report can be generated.")
    if (
        screening.application_requires_finalized_applicant_pool(application)
        and not screening.application_has_finalized_applicant_pool(application)
    ):
        raise ValueError(screening.get_applicant_pool_finalization_block_message(application))
    if not user_can_manage_comparative_assessment_report(actor, application):
        raise ValueError(
            "This case is not currently assigned to you for CAR preparation."
        )

    review_stage = application.case.current_stage
    if review_stage != CAR_REVIEW_STAGE or application.branch != PositionPosting.Branch.PLANTILLA:
        raise ValueError(
            "The Comparative Assessment Report is available only for Plantilla cases at the HRMPSB step."
        )
    if ComparativeAssessmentReport.objects.filter(
        recruitment_entry=application.position,
        review_stage=review_stage,
        is_finalized=True,
        is_returned=False,
    ).exists():
        raise ValueError("A finalized CAR already exists for this vacancy.")

    assessment_weights = application.position.assessment_weights_or_default
    latest_draft_report = get_latest_draft_comparative_assessment_report(
        application,
        stage=review_stage,
    )
    if finalize:
        if not latest_draft_report:
            raise ValueError(
                "Prepare the CAR draft before finalizing the Comparative Assessment Report."
            )
        # WF-02: members present is the quorum fact. Validate it before generating any
        # evidence so an invalid direct POST cannot leave an orphaned CAR artifact.
        members_present = cleaned_data.get("members_present")
        if members_present is None or not (3 <= members_present <= 5):
            raise ValueError("Record how many HRMPSB members were present (3–5).")
        deliberated_on = cleaned_data.get("deliberated_on")
        if not deliberated_on:
            raise ValueError("Record the deliberation date before finalizing the CAR.")
        if deliberated_on > timezone.localdate():
            raise ValueError("The deliberation date can't be in the future.")
        last_interview = (
            InterviewSession.objects.filter(recruitment_entry=application.position)
            .exclude(scheduled_for__isnull=True)
            .order_by("-scheduled_for")
            .values_list("scheduled_for", flat=True)
            .first()
        )
        if last_interview and not clock.event_held_by(last_interview, deliberated_on):
            # The board deliberates on the interview results, so it can't have met before them.
            raise ValueError(
                "The deliberation date can't be earlier than the last interview "
                f"({timezone.localtime(last_interview):%B} {timezone.localtime(last_interview).day}, "
                f"{timezone.localtime(last_interview).year})."
            )
        candidate_rows = _car_candidate_rows(
            application.position,
            review_stage,
            required_draft=latest_draft_report,
            assessment_weights=assessment_weights,
        )
        recommendation_notes = (cleaned_data.get("recommendation_notes") or "").strip()
        recommended_case_id, recommends_none = _resolve_car_recommendation(
            cleaned_data.get("recommended_case_id"),
            candidate_rows,
            recommendation_notes,
        )
    else:
        candidate_rows = _car_draft_candidate_rows(
            application.position,
            review_stage,
            assessment_weights=assessment_weights,
        )
    latest_report = get_comparative_assessment_report(
        application,
        stage=review_stage,
        include_returned=True,
    )
    version_number = (latest_report.version_number + 1) if latest_report else 1
    consolidated_snapshot = {
        "generated_at": timezone.now().isoformat(),
        "prepared_by_role": actor.role,
        "entry_code": application.position.job_code,
        "review_stage": review_stage,
        "assessment_weight_display": _assessment_weight_display(
            application.position,
            assessment_weights,
        ),
        "assessment_weights": {
            "ete": str(assessment_weights.ete_weight),
            "exam": str(assessment_weights.exam_weight),
            "interview": str(assessment_weights.interview_weight),
            "exam_general": str(assessment_weights.exam_general_weight),
            "exam_technical": str(assessment_weights.exam_technical_weight),
        },
        "source_car_draft_id": latest_draft_report.id if finalize and latest_draft_report else "",
        "source_car_draft_version": latest_draft_report.version_number if finalize and latest_draft_report else "",
        "candidate_count": len(candidate_rows),
        "ranked_candidates": [
            {
                "rank_order": row["rank_order"],
                "preliminary_rank_order": row["preliminary_rank_order"],
                # SL-08: state the tie rather than leaving the reader to infer it from two
                # rows sharing an ordinal.
                "is_tied": bool(row.get("is_tied")),
                "application_reference": row["application"].reference_number or "",
                "applicant_name": row["application"].applicant_display_name,
                "candidate_code": row["application"].candidate_code or "",
                "document_review_outcome": row["qualification_outcome"],
                "document_review_score": row["document_review_score"],
                "finalized_document_review_count": row["finalized_document_review_count"],
                "qualification_outcome": row["qualification_outcome"],
                "exam_status": row["exam_status"],
                "exam_score": row["exam_score"],
                "exam_components": row["exam_components"],
                "finalized_exam_count": row["finalized_exam_count"],
                "interview_average_score": row["interview_average_score"],
                "finalized_interview_count": row["finalized_interview_count"],
                "assessment_score": common._decimal_string(row["assessment_score"]),
                "recommendation": row["recommendation"],
                "decision_support_summary": row["decision_support_summary"],
                "ranking_notes": row["ranking_notes"],
            }
            for row in candidate_rows
        ],
    }

    pdf_bytes = evidence_vault._build_comparative_assessment_report_pdf(
        application=application,
        actor=actor,
        candidate_rows=candidate_rows,
        generation_number=version_number,
        summary_notes=cleaned_data["summary_notes"],
        assessment_weights=assessment_weights,
    )
    evidence = evidence_vault.store_generated_evidence_item(
        application=application,
        actor=actor,
        label=f"{common.CAR_LABEL} - {application.case.get_current_stage_display()}",
        filename=f"{application.position.job_code.lower()}-{review_stage.replace('_', '-')}-car-v{version_number}.pdf",
        raw_bytes=pdf_bytes,
        content_type="application/pdf",
        recruitment_entry=application.position,
        artifact_scope=EvidenceVaultItem.OwnerScope.ENTRY,
        artifact_type=common.ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
        stage=review_stage,
        document_key=common.ARTIFACT_TYPE_COMPARATIVE_ASSESSMENT_REPORT,
    )

    report = ComparativeAssessmentReport(
        recruitment_entry=application.position,
        review_stage=review_stage,
        generated_by=actor,
        branch=application.branch,
        summary_notes=cleaned_data["summary_notes"],
        consolidated_snapshot=consolidated_snapshot,
        version_number=version_number,
        evidence_item=evidence,
        is_finalized=finalize,
    )
    if finalize:
        report.finalized_by = actor
        report.finalized_at = timezone.now()
        report.members_present = cleaned_data.get("members_present")
        report.deliberated_on = cleaned_data.get("deliberated_on")
        report.recommended_case_id = recommended_case_id
        report.recommends_none = recommends_none
        report.recommendation_notes = recommendation_notes
    report.full_clean()
    report.save()

    for row in candidate_rows:
        item = ComparativeAssessmentReportItem(
            report=report,
            recruitment_case=row["recruitment_case"],
            deliberation_record=row["deliberation_record"],
            rank_order=row["rank_order"],
            preliminary_rank_order=row["preliminary_rank_order"],
            qualification_outcome=row["qualification_outcome"],
            document_review_score=(
                Decimal(row["document_review_score"]) if row["document_review_score"] else None
            ),
            ete_rating=Decimal(row["ete_rating"]) if row["ete_rating"] else None,
            exam_status=row["exam_status"],
            exam_score=Decimal(row["exam_score"]) if row["exam_score"] else None,
            interview_average_score=(
                Decimal(row["interview_average_score"]) if row["interview_average_score"] else None
            ),
            assessment_score=row["assessment_score"],
            recommendation=row["recommendation"],
            decision_support_summary=row["decision_support_summary"],
            ranking_notes=row["ranking_notes"],
        )
        item.full_clean()
        item.save()

    audit.record_audit_event(
        application=application,
        actor=actor,
        action=AuditLog.Action.CAR_FINALIZED if finalize else AuditLog.Action.CAR_GENERATED,
        description=(
            "Finalized the Comparative Assessment Report."
            if finalize
            else "Prepared or updated the CAR draft for HRMPSB deliberation."
        ),
        metadata={
            "car_report_id": report.id,
            "review_stage": review_stage,
            "version_number": report.version_number,
            "candidate_count": len(candidate_rows),
            "evidence_id": evidence.id,
            "is_finalized": report.is_finalized,
            "prepared_by_role": actor.role,
            "recommended_case_id": report.recommended_case_id,
            "recommends_none": report.recommends_none,
            "members_present": report.members_present,
            "deliberated_on": (
                report.deliberated_on.isoformat() if report.deliberated_on else ""
            ),
        },
    )
    if finalize:
        routing._auto_advance_after_car_finalized(report, actor)
        case_notices.emit_car_finalized_panel_notifications(report)
    return report


def get_car_candidate_fact_rows(items):
    """SL-08A: the facts each ETE scoring row is judged against, keyed by case id.

    ETE is 40% of the CAR, and the scorer was being asked for that verdict with nothing on
    screen but a candidate code and three empty boxes — the position's Qualification
    Standards and the candidate's own credentials both lived elsewhere. This returns, per
    candidate, the qualification summary they submitted and the screened evidence rows
    (requirement, status, evidence id) that back it, so the judgement is made against the
    record rather than from memory.

    Blinding note (REV-B): this is only ever rendered inside the editable ETE grid, which
    belongs to the Secretariat preparing the CAR — the same office that screened these
    documents. The board's read-only CAR stays blinded."""
    rows = {}
    for item in items:
        application = item.application
        rows[item.recruitment_case_id] = {
            "qualification_summary": application.qualification_summary,
            "documents": [
                {
                    "title": entry["requirement"].applicant_label,
                    "status": entry["review_status"],
                    "remarks": entry["review_remarks"],
                    "evidence_id": entry["evidence"].id if entry["evidence"] else None,
                }
                for entry in intake.get_applicant_document_review_items(application)
                if entry["is_submitted"]
            ],
        }
    return rows


def build_ete_source_facts_snapshot(application):
    """SL-08A: what the ETE rating was judged against, captured with the rating.

    A QS edit or a document resubmission afterwards must not silently rewrite the basis of
    a rating that has already been given."""
    posting = application.position
    reference = posting.position_reference
    return {
        "captured_at": timezone.now().isoformat(),
        "qualification_reference": posting.qualification_reference or "",
        "qs_education": getattr(reference, "qs_education", "") or "",
        "qs_training": getattr(reference, "qs_training", "") or "",
        "qs_experience": getattr(reference, "qs_experience", "") or "",
        "qs_eligibility": getattr(reference, "qs_eligibility", "") or "",
        "qualification_summary": application.qualification_summary or "",
        "documents": [
            {
                "title": entry["requirement"].applicant_label,
                "status": entry["review_status"],
                "evidence_id": entry["evidence"].id if entry["evidence"] else None,
            }
            for entry in intake.get_applicant_document_review_items(application)
            if entry["is_submitted"]
        ],
    }
