from dataclasses import dataclass, replace

from .models import PositionPosting


@dataclass(frozen=True)
class ApplicantDocumentRequirement:
    code: str
    title: str
    help_text: str
    is_required: bool = True
    conditional_on_performance_rating: bool = False

    @property
    def file_field_name(self):
        return self.code

    @property
    def not_applicable_field_name(self):
        return f"{self.code}_not_applicable"

    @property
    def applicant_label(self):
        if not self.is_required or self.conditional_on_performance_rating:
            return "If applicable"
        return "Required"


SIGNED_COVER_LETTER = "signed_cover_letter"
PERSONAL_DATA_SHEET = "personal_data_sheet"
WORK_EXPERIENCE_SHEET = "work_experience_sheet"
PERFORMANCE_RATING = "performance_rating"
ELIGIBILITY_OR_LICENSE = "eligibility_or_license"
TRANSCRIPT_OF_RECORDS = "transcript_of_records"
DIPLOMA = "diploma"
CERTIFICATE_OF_EMPLOYMENT = "certificate_of_employment"
TRAINING_CERTIFICATES = "training_certificates"


PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS = (
    ApplicantDocumentRequirement(
        code=SIGNED_COVER_LETTER,
        title="Signed Cover Letter addressed to VOLTAIRE S. GUADALUPE, MD, MPH, MAHPS, Director IV",
        help_text="Upload the signed cover letter for this application.",
    ),
    ApplicantDocumentRequirement(
        code=PERSONAL_DATA_SHEET,
        title="Personal Data Sheet (CS Form No. 212, Revised 2025) with recent passport-sized picture",
        help_text="Upload the completed Personal Data Sheet with the required photo attached.",
    ),
    ApplicantDocumentRequirement(
        code=WORK_EXPERIENCE_SHEET,
        title="Work Experience Sheet",
        help_text="Upload the Work Experience Sheet that accompanies the Personal Data Sheet.",
    ),
    ApplicantDocumentRequirement(
        code=PERFORMANCE_RATING,
        title="Performance Rating in the last rating period",
        help_text=(
            "Upload your latest performance rating only when you have an applicable rating "
            "for the last rating period."
        ),
        is_required=False,
        conditional_on_performance_rating=True,
    ),
    ApplicantDocumentRequirement(
        code=ELIGIBILITY_OR_LICENSE,
        title="Certificate of Eligibility, Rating, or License",
        help_text="Upload the eligibility, rating, or license document relevant to this application.",
    ),
    ApplicantDocumentRequirement(
        code=TRANSCRIPT_OF_RECORDS,
        title="Authenticated Transcript of Records",
        help_text="Upload the authenticated Transcript of Records.",
    ),
    ApplicantDocumentRequirement(
        code=DIPLOMA,
        title="Diploma",
        help_text="Upload a copy of your diploma.",
    ),
    ApplicantDocumentRequirement(
        code=CERTIFICATE_OF_EMPLOYMENT,
        title="Certificate of Employment",
        help_text="Upload your certificate or certificates of employment.",
    ),
    ApplicantDocumentRequirement(
        code=TRAINING_CERTIFICATES,
        title="Training Certificates",
        help_text="Upload your certificate or certificates for completed training courses.",
    ),
)

COS_APPLICANT_DOCUMENT_REQUIREMENTS = (
    ApplicantDocumentRequirement(
        code=SIGNED_COVER_LETTER,
        title="Signed Application Letter addressed to VOLTAIRE S. GUADALUPE, MD, MPH, MAHPS, Director IV",
        help_text="Upload the signed application letter for this COS application.",
    ),
    ApplicantDocumentRequirement(
        code=PERSONAL_DATA_SHEET,
        title="Personal Data Sheet (CS Form No. 212, Revised 2025) with recent passport-sized picture",
        help_text="Upload the completed Personal Data Sheet with the required photo attached.",
    ),
    ApplicantDocumentRequirement(
        code=WORK_EXPERIENCE_SHEET,
        title="Work Experience Sheet",
        help_text="Upload the Work Experience Sheet that accompanies the Personal Data Sheet.",
    ),
    ApplicantDocumentRequirement(
        code=ELIGIBILITY_OR_LICENSE,
        title="Certificate of Eligibility, Rating, or License",
        help_text="Upload the eligibility, rating, or license document required for this COS application.",
    ),
    ApplicantDocumentRequirement(
        code=TRANSCRIPT_OF_RECORDS,
        title="Transcript of Records (TOR)",
        help_text="Upload a copy of your Transcript of Records.",
    ),
    ApplicantDocumentRequirement(
        code=DIPLOMA,
        title="Diploma",
        help_text="Upload a copy of your diploma.",
    ),
    ApplicantDocumentRequirement(
        code=CERTIFICATE_OF_EMPLOYMENT,
        title="Certificate of Employment",
        help_text="Upload your certificate or certificates of employment.",
    ),
    ApplicantDocumentRequirement(
        code=TRAINING_CERTIFICATES,
        title="Training Certificates",
        help_text="Upload your training certificates when they apply to this COS application.",
        is_required=False,
    ),
)

APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH = {
    PositionPosting.Branch.PLANTILLA: PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS,
    PositionPosting.Branch.COS: COS_APPLICANT_DOCUMENT_REQUIREMENTS,
}

APPLICANT_DOCUMENT_REQUIREMENTS_BY_CODE = {
    requirement.code: requirement for requirement in PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS
}

APPLICANT_DOCUMENT_TYPE_CHOICES = [
    (requirement.code, requirement.title) for requirement in PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS
]

# Documents that must always apply and stay required on every vacancy; the per-vacancy
# configuration UI cannot drop these or mark them optional.
MIN_REQUIRED_DOCUMENT_CODES = frozenset({SIGNED_COVER_LETTER, PERSONAL_DATA_SHEET})


def _resolve_posting(source=None):
    """Return the PositionPosting reachable from ``source`` (a posting or an application), else None."""
    if isinstance(source, PositionPosting):
        return source
    position = getattr(source, "position", None)
    if isinstance(position, PositionPosting):
        return position
    return None


def _resolve_branch(source=None):
    if hasattr(source, "branch"):
        source = source.branch
    return source or PositionPosting.Branch.PLANTILLA


def _branch_requirements_by_code(branch):
    return {
        requirement.code: requirement
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(
            branch, PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS
        )
    }


def get_applicant_document_requirements(source=None):
    """Resolve the applicant document catalog for a posting/application/branch.

    When ``source`` resolves to a PositionPosting that has its own configured rows, the
    catalog is built from those rows (title/help text/conditional flag taken from the
    posting's branch catalog, required/optional taken from the row). Otherwise the branch
    catalog is returned unchanged, so postings with no configuration behave exactly as before.
    """
    posting = _resolve_posting(source)
    if posting is not None and posting.pk:
        rows = list(posting.document_requirements.all())
        if rows:
            catalog = _branch_requirements_by_code(posting.branch)
            built = []
            for row in rows:
                base = catalog.get(row.document_code)
                if base is None:
                    continue
                if base.conditional_on_performance_rating:
                    # Conditional documents stay applicant-driven; the row's flag is ignored.
                    built.append(base)
                else:
                    built.append(replace(base, is_required=row.is_required))
            return tuple(built)
    return APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(
        _resolve_branch(source),
        PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS,
    )


def get_required_applicant_document_requirements(
    source=None, *, branch=None, performance_rating_not_applicable=False
):
    target = source if source is not None else branch
    return tuple(
        requirement
        for requirement in get_applicant_document_requirements(target)
        if requirement.is_required
        or (
            requirement.conditional_on_performance_rating
            and not performance_rating_not_applicable
        )
    )
