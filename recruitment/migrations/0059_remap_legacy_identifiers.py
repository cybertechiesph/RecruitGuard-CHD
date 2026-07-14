# REV-G: identifier redesign, part 2 (data). Remaps legacy identifiers on
# existing rows:
#   - application references RG-YYYYMMDD-XXXXXX -> CASE-<year>-<seq>
#     (sequenced by submission time within each year)
#   - posting entry codes RG-<branch>-<year>-NNNN -> VAC-<branch>-<year>-NNN
#   - submitted applications receive tracking codes
#   - cases already at/past HRMPSB review receive candidate codes
# Reverse is a no-op: legacy formats carry no information worth
# reconstructing pre-production.

import re
import secrets

from django.db import migrations

LEGACY_REFERENCE = re.compile(r"^RG-\d{8}-[0-9A-F]{6}$")
LEGACY_ENTRY_CODE = re.compile(r"^RG-(?P<branch>PLT|COS)-(?P<year>\d{4})-(?P<seq>\d+)$")
TRACKING_ALPHABET = "0123456789BCDFGHJKMNPQRSTVWXYZ"


def _tracking_code(existing):
    while True:
        blocks = [
            "".join(secrets.choice(TRACKING_ALPHABET) for _ in range(4))
            for _ in range(2)
        ]
        code = f"TRK-{blocks[0]}-{blocks[1]}"
        if code not in existing:
            existing.add(code)
            return code


def remap_identifiers(apps, schema_editor):
    RecruitmentApplication = apps.get_model("recruitment", "RecruitmentApplication")
    PositionPosting = apps.get_model("recruitment", "PositionPosting")
    RecruitmentCase = apps.get_model("recruitment", "RecruitmentCase")

    # Posting codes: RG- -> VAC-, sequence reprinted at 3 digits.
    for posting in PositionPosting.objects.exclude(job_code=""):
        match = LEGACY_ENTRY_CODE.fullmatch(posting.job_code or "")
        if not match:
            continue
        posting.job_code = (
            f"VAC-{match.group('branch')}-{match.group('year')}-{int(match.group('seq')):03d}"
        )
        posting.save(update_fields=["job_code"])

    # Application references: legacy random refs become per-year case numbers,
    # ordered by submission time so earlier cases get lower numbers.
    counters = {}
    legacy_apps = list(
        RecruitmentApplication.objects.exclude(reference_number=None)
        .exclude(reference_number="")
        .order_by("submitted_at", "id")
    )
    for application in legacy_apps:
        reference = application.reference_number or ""
        if not LEGACY_REFERENCE.fullmatch(reference):
            continue
        source_date = application.submitted_at or application.created_at
        year = source_date.year if source_date else 2026
        counters[year] = counters.get(year, 0) + 1
        application.reference_number = f"CASE-{year:04d}-{counters[year]:04d}"
        application.save(update_fields=["reference_number"])

    # Tracking codes for everything already submitted.
    existing_codes = set(
        RecruitmentApplication.objects.exclude(tracking_code=None).values_list(
            "tracking_code", flat=True
        )
    )
    for application in RecruitmentApplication.objects.filter(
        submitted_at__isnull=False, tracking_code=None
    ):
        application.tracking_code = _tracking_code(existing_codes)
        application.save(update_fields=["tracking_code"])

    # Candidate codes for cases already in or past HRMPSB review.
    post_hrmpsb_stages = ["hrmpsb_review", "appointing_authority_review", "completion", "closed"]
    taken_by_position = {}
    for case in RecruitmentCase.objects.filter(
        current_stage__in=post_hrmpsb_stages
    ).select_related("application"):
        application = case.application
        if application.candidate_code:
            continue
        position_id = application.position_id
        taken = taken_by_position.setdefault(
            position_id,
            set(
                RecruitmentApplication.objects.filter(position_id=position_id)
                .exclude(candidate_code="")
                .values_list("candidate_code", flat=True)
            ),
        )
        pool = [f"C-{n:02d}" for n in range(1, 100) if f"C-{n:02d}" not in taken]
        code = secrets.choice(pool)
        taken.add(code)
        application.candidate_code = code
        application.save(update_fields=["candidate_code"])


class Migration(migrations.Migration):

    dependencies = [
        ('recruitment', '0058_recruitmentapplication_candidate_code_and_more'),
    ]

    operations = [
        migrations.RunPython(remap_identifiers, migrations.RunPython.noop),
    ]
