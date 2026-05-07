import re
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.text import slugify


REFERENCE_LEVEL_TO_ROUTING_LEVEL = {
    "FIRST_LEVEL": 1,
    "SECOND_LEVEL": 2,
}

ENTRY_CODE_PATTERN = re.compile(
    r"^RG-(?P<branch>PLT|COS)-(?P<year>\d{4})-(?P<sequence>\d{4})$"
)


def build_unique_position_slug(model_class, source_value, *, instance_pk=None):
    base_slug = slugify(source_value or "") or "position-reference"
    slug = base_slug
    suffix = 2
    queryset = model_class.objects.all()
    if instance_pk:
        queryset = queryset.exclude(pk=instance_pk)
    while queryset.filter(position_slug=slug).exists():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class RecruitmentUser(AbstractUser):
    class Role(models.TextChoices):
        APPLICANT = "applicant", "Applicant"
        SECRETARIAT = "secretariat", "Secretariat"
        HRM_CHIEF = "hrm_chief", "HRM Chief"
        HRMPSB_MEMBER = "hrmpsb_member", "HRMPSB Member"
        APPOINTING_AUTHORITY = "appointing_authority", "Appointing Authority"
        SYSTEM_ADMIN = "system_admin", "System Administrator"

    role = models.CharField(max_length=40, choices=Role.choices, default=Role.APPLICANT)
    office_name = models.CharField(max_length=255, blank=True)
    employee_id = models.CharField(max_length=50, blank=True)

    @classmethod
    def internal_roles(cls):
        return {
            cls.Role.SECRETARIAT,
            cls.Role.HRM_CHIEF,
            cls.Role.HRMPSB_MEMBER,
            cls.Role.APPOINTING_AUTHORITY,
            cls.Role.SYSTEM_ADMIN,
        }

    def save(self, *args, **kwargs):
        # Internal system administration is handled through the protected app views.
        # Only actual Django superusers should inherit admin-site access.
        self.is_staff = bool(self.is_superuser)
        super().save(*args, **kwargs)

    @property
    def is_internal_user(self):
        return self.role in self.internal_roles()

    @property
    def is_workflow_staff(self):
        return self.role in self.internal_roles()

    def __str__(self):
        return self.get_full_name() or self.username


class PositionReference(TimestampedModel):
    class LevelClassification(models.TextChoices):
        FIRST_LEVEL = "FIRST_LEVEL", "First Level"
        SECOND_LEVEL = "SECOND_LEVEL", "Second Level"

    class ReferenceStatus(models.TextChoices):
        VERIFIED_REFERENCE = "VERIFIED_REFERENCE", "Verified Reference"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs Review"
        INCOMPLETE_REFERENCE = "INCOMPLETE_REFERENCE", "Incomplete Reference"

    CORE_REFERENCE_FIELDS = (
        "position_title",
        "salary_grade",
        "level_classification",
        "class_id",
        "os_code",
        "occupational_service",
        "occupational_group",
    )

    position_title = models.CharField(max_length=255)
    position_slug = models.SlugField(max_length=255, unique=True, blank=True)
    salary_grade = models.PositiveSmallIntegerField(blank=True, null=True)
    level_classification = models.CharField(
        max_length=20,
        choices=LevelClassification.choices,
        blank=True,
        null=True,
    )
    class_id = models.CharField(max_length=50, blank=True, null=True)
    os_code = models.CharField(max_length=50, blank=True, null=True)
    occupational_service = models.CharField(max_length=255, blank=True, null=True)
    occupational_group = models.CharField(max_length=255, blank=True, null=True)
    reference_status = models.CharField(
        max_length=30,
        choices=ReferenceStatus.choices,
        default=ReferenceStatus.NEEDS_REVIEW,
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    position_code = models.CharField(max_length=30, unique=True, blank=True, null=True)
    agency_item_number = models.CharField(max_length=100, blank=True, null=True)
    office_division_default = models.CharField(max_length=255, blank=True, null=True)
    qs_education = models.TextField(blank=True, null=True)
    qs_training = models.TextField(blank=True, null=True)
    qs_experience = models.TextField(blank=True, null=True)
    qs_eligibility = models.TextField(blank=True, null=True)
    employment_track_applicability = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        ordering = ["position_title", "salary_grade", "class_id"]

    def __str__(self):
        reference_code = self.class_id or self.position_code or self.position_slug
        return f"{self.position_title} ({reference_code})"

    @property
    def routing_level(self):
        return REFERENCE_LEVEL_TO_ROUTING_LEVEL.get(self.level_classification)

    @property
    def missing_core_fields(self):
        missing = []
        for field_name in self.CORE_REFERENCE_FIELDS:
            value = getattr(self, field_name)
            if value is None or value == "":
                missing.append(field_name)
        return missing

    def derive_reference_status(self, *, has_warning=False):
        if self.missing_core_fields:
            return self.ReferenceStatus.INCOMPLETE_REFERENCE
        if has_warning:
            return self.ReferenceStatus.NEEDS_REVIEW
        return self.ReferenceStatus.VERIFIED_REFERENCE

    def get_selection_warning(self):
        if self.reference_status == self.ReferenceStatus.VERIFIED_REFERENCE:
            return ""
        if self.reference_status == self.ReferenceStatus.INCOMPLETE_REFERENCE:
            missing_labels = [
                self._meta.get_field(field_name).verbose_name.replace("_", " ")
                for field_name in self.missing_core_fields
            ]
            if missing_labels:
                return "Reference metadata is incomplete. Missing: " + ", ".join(missing_labels) + "."
            return "Reference metadata is incomplete and requires manual follow-up."
        return "Reference metadata needs manual review before it is treated as a fully verified official position."

    @property
    def qualification_summary(self):
        parts = []
        if self.qs_education:
            parts.append(f"Education: {self.qs_education}")
        if self.qs_training:
            parts.append(f"Training: {self.qs_training}")
        if self.qs_experience:
            parts.append(f"Experience: {self.qs_experience}")
        if self.qs_eligibility:
            parts.append(f"Eligibility: {self.qs_eligibility}")
        return "\n".join(parts)

    def clean(self):
        errors = {}
        if self.reference_status == self.ReferenceStatus.VERIFIED_REFERENCE and self.missing_core_fields:
            errors["reference_status"] = "Verified references must include the complete official starter metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.position_slug:
            self.position_slug = build_unique_position_slug(
                type(self),
                self.position_title,
                instance_pk=self.pk,
            )
        super().save(*args, **kwargs)


class PositionPosting(TimestampedModel):
    class Branch(models.TextChoices):
        PLANTILLA = "plantilla", "Plantilla"
        COS = "cos", "COS"

    class Level(models.IntegerChoices):
        LEVEL_1 = 1, "Level 1"
        LEVEL_2 = 2, "Level 2"

    class IntakeMode(models.TextChoices):
        FIXED_PERIOD = "fixed_period", "Fixed Period"
        OPENING_BASED = "opening_based", "Opening Based"
        CONTINUOUS = "continuous", "Continuous"
        POOLING = "pooling", "Pooling"

    class EntryStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    ENTRY_CODE_BRANCH_SEGMENTS = {
        Branch.PLANTILLA: "PLT",
        Branch.COS: "COS",
    }
    ENTRY_CODE_FORMAT_LABEL = "RG-[BRANCH]-[YEAR]-[4 digit sequence]"
    ENTRY_CODE_GENERATION_RETRIES = 5
    LIVE_METADATA_LOCKED_FIELDS = {
        "position_reference": (
            "position_reference_id",
            "Position reference cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        ),
        "branch": (
            "branch",
            "Engagement type cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        ),
        "intake_mode": (
            "intake_mode",
            "Intake mode cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        ),
        "publication_date": (
            "publication_date",
            "Publication date cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        ),
        "opening_date": (
            "opening_date",
            "Opening date cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        ),
    }

    position_reference = models.ForeignKey(
        PositionReference,
        on_delete=models.PROTECT,
        related_name="recruitment_entries",
        blank=True,
        null=True,
    )
    job_code = models.CharField(max_length=30, unique=True, blank=True)
    title = models.CharField(max_length=255, blank=True)
    branch = models.CharField(max_length=20, choices=Branch.choices)
    level = models.PositiveSmallIntegerField(choices=Level.choices)
    unit = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    qualification_reference = models.TextField(blank=True)
    intake_mode = models.CharField(
        max_length=30,
        choices=IntakeMode.choices,
        default=IntakeMode.FIXED_PERIOD,
    )
    status = models.CharField(
        max_length=20,
        choices=EntryStatus.choices,
        default=EntryStatus.DRAFT,
    )
    publication_date = models.DateField(blank=True, null=True)
    opening_date = models.DateField(default=timezone.localdate)
    closing_date = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "RecruitmentUser",
        on_delete=models.PROTECT,
        related_name="created_recruitment_entries",
        blank=True,
        null=True,
    )
    updated_by = models.ForeignKey(
        "RecruitmentUser",
        on_delete=models.PROTECT,
        related_name="updated_recruitment_entries",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["title"]
        verbose_name = "Recruitment Entry"
        verbose_name_plural = "Recruitment Entries"

    @property
    def official_office_label(self):
        if self.position_reference_id:
            if self.position_reference.office_division_default:
                return self.position_reference.office_division_default
            if self.position_reference.occupational_service:
                return self.position_reference.occupational_service
        return self.unit

    def apply_position_reference_metadata(self):
        if not self.position_reference_id:
            return
        self.title = self.position_reference.position_title
        if self.position_reference.routing_level is not None:
            self.level = self.position_reference.routing_level
        if self.position_reference.office_division_default:
            self.unit = self.position_reference.office_division_default

    @property
    def is_live_for_metadata_lock(self):
        if not self.pk:
            return False
        return self.applications.filter(
            models.Q(submitted_at__isnull=False) | models.Q(case__isnull=False)
        ).exists()

    def get_live_metadata_lock_errors(self, *, update_fields=None):
        if not self.pk or not self.is_live_for_metadata_lock:
            return {}

        original = (
            type(self)
            .objects.only(
                "position_reference_id",
                "branch",
                "intake_mode",
                "publication_date",
                "opening_date",
            )
            .get(pk=self.pk)
        )
        normalized_update_fields = None
        if update_fields is not None:
            normalized_update_fields = set(update_fields)

        errors = {}
        for field_name, (attribute_name, message) in self.LIVE_METADATA_LOCKED_FIELDS.items():
            if (
                normalized_update_fields is not None
                and field_name not in normalized_update_fields
                and attribute_name not in normalized_update_fields
            ):
                continue
            if getattr(self, attribute_name) != getattr(original, attribute_name):
                errors[field_name] = [message]
        return errors

    @property
    def entry_code_year(self):
        source_date = self.publication_date or self.opening_date
        if source_date:
            return source_date.year
        return timezone.localdate().year

    @property
    def entry_code_branch_segment(self):
        return self.ENTRY_CODE_BRANCH_SEGMENTS.get(self.branch, "")

    @property
    def expected_entry_code_prefix(self):
        branch_segment = self.entry_code_branch_segment
        if not branch_segment:
            return ""
        return f"RG-{branch_segment}-{self.entry_code_year:04d}-"

    def build_next_entry_code(self):
        prefix = self.expected_entry_code_prefix
        if not prefix:
            raise ValidationError({"branch": "Engagement type is required before Entry Code can be generated."})

        queryset = type(self).objects.select_for_update().filter(job_code__startswith=prefix)
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)

        highest_sequence = 0
        for existing_code in queryset.values_list("job_code", flat=True):
            if not existing_code:
                continue
            match = ENTRY_CODE_PATTERN.fullmatch(existing_code)
            if not match or not existing_code.startswith(prefix):
                continue
            highest_sequence = max(highest_sequence, int(match.group("sequence")))
        return f"{prefix}{highest_sequence + 1:04d}"

    def clean(self):
        errors = {}
        live_metadata_lock_errors = self.get_live_metadata_lock_errors()
        for field_name, messages in live_metadata_lock_errors.items():
            errors.setdefault(field_name, []).extend(messages)

        if not self.position_reference_id:
            errors["position_reference"] = "Position reference is required."
        else:
            if not self.position_reference.is_active:
                errors["position_reference"] = "Inactive position references cannot be used for recruitment entries."
            elif self.position_reference.routing_level is None:
                errors["position_reference"] = (
                    "The selected position reference does not contain a usable level classification."
                )
        if self.closing_date and self.closing_date < self.opening_date:
            errors["closing_date"] = "Closing date cannot be earlier than opening date."

        if self.branch == self.Branch.PLANTILLA:
            if self.intake_mode != self.IntakeMode.FIXED_PERIOD:
                errors["intake_mode"] = "Plantilla entries must use a fixed validity period."
            if not self.closing_date:
                errors["closing_date"] = "Plantilla entries require a closing date."
        elif self.branch == self.Branch.COS:
            if self.intake_mode == self.IntakeMode.FIXED_PERIOD:
                errors["intake_mode"] = "COS entries must use opening-based, continuous, or pooling intake."
            if (
                self.intake_mode in {self.IntakeMode.CONTINUOUS, self.IntakeMode.POOLING}
                and self.closing_date
            ):
                errors["closing_date"] = "Continuous or pooling COS entries must not set a fixed closing date."

        if self.status == self.EntryStatus.CLOSED and not self.closing_date:
            self.closing_date = timezone.localdate()

        if self.job_code:
            self.job_code = self.job_code.strip().upper()
            code_match = ENTRY_CODE_PATTERN.fullmatch(self.job_code)
            if not code_match:
                errors["job_code"] = (
                    f"Entry Code must match the format {self.ENTRY_CODE_FORMAT_LABEL}."
                )
            else:
                expected_branch_segment = self.entry_code_branch_segment
                expected_year_segment = f"{self.entry_code_year:04d}"
                if expected_branch_segment and code_match.group("branch") != expected_branch_segment:
                    errors["job_code"] = "Entry Code branch segment must match the selected engagement type."
                elif code_match.group("year") != expected_year_segment:
                    errors["job_code"] = (
                        "Entry Code year segment must match the publication date, opening date, or current year fallback."
                    )
        elif self.pk:
            errors["job_code"] = "Entry Code is required after the recruitment entry is first saved."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        live_metadata_lock_errors = self.get_live_metadata_lock_errors(
            update_fields=kwargs.get("update_fields"),
        )
        if live_metadata_lock_errors:
            raise ValidationError(live_metadata_lock_errors)

        if self.position_reference_id:
            self.apply_position_reference_metadata()
        self.is_active = self.status == self.EntryStatus.ACTIVE

        if self.job_code:
            super().save(*args, **kwargs)
            return

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            updated_fields = set(update_fields)
            updated_fields.add("job_code")
            kwargs["update_fields"] = list(updated_fields)

        last_error = None
        for _ in range(self.ENTRY_CODE_GENERATION_RETRIES):
            try:
                with transaction.atomic():
                    self.job_code = self.build_next_entry_code()
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                self.job_code = ""
                last_error = exc

        if last_error is not None:
            raise last_error

    @property
    def is_open_for_intake(self):
        if self.status != self.EntryStatus.ACTIVE:
            return False
        if self.opening_date and self.opening_date > timezone.localdate():
            return False
        if self.branch == self.Branch.PLANTILLA:
            return bool(self.closing_date and self.closing_date >= timezone.localdate())
        if self.intake_mode == self.IntakeMode.OPENING_BASED and self.closing_date:
            return self.closing_date >= timezone.localdate()
        return True

    @property
    def engagement_type(self):
        return self.branch

    def __str__(self):
        return f"{self.title} [{self.job_code}]"


class RecruitmentApplication(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SECRETARIAT_REVIEW = "secretariat_review", "Secretariat Review"
        HRM_CHIEF_REVIEW = "hrm_chief_review", "HRM Chief Review"
        HRMPSB_REVIEW = "hrmpsb_review", "HRMPSB Review"
        APPOINTING_AUTHORITY_REVIEW = "appointing_authority_review", "Appointing Authority Review"
        RETURNED_TO_APPLICANT = "returned_to_applicant", "Returned to Applicant"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class PerformanceRatingApplicability(models.TextChoices):
        APPLICABLE = "applicable", "I have a performance rating for the last rating period"
        NOT_APPLICABLE = "not_applicable", "I do not have an applicable performance rating"

    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reference_number = models.CharField(
        max_length=30,
        unique=True,
        editable=False,
        blank=True,
        null=True,
    )
    applicant = models.ForeignKey("RecruitmentUser", on_delete=models.CASCADE, related_name="applications")
    position = models.ForeignKey(PositionPosting, on_delete=models.PROTECT, related_name="applications")
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.DRAFT)
    current_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    applicant_first_name = models.CharField(max_length=150, blank=True)
    applicant_last_name = models.CharField(max_length=150, blank=True)
    applicant_email = models.EmailField(blank=True)
    applicant_phone = models.CharField(max_length=50, blank=True)
    performance_rating_applicability = models.CharField(
        max_length=20,
        choices=PerformanceRatingApplicability.choices,
        default=PerformanceRatingApplicability.APPLICABLE,
    )
    checklist_privacy_consent = models.BooleanField(default=False)
    checklist_documents_complete = models.BooleanField(default=False)
    checklist_information_certified = models.BooleanField(default=False)
    cover_letter = models.TextField(blank=True)
    qualification_summary = models.TextField()
    otp_hash = models.CharField(max_length=64, blank=True)
    otp_requested_at = models.DateTimeField(blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_verified_at = models.DateTimeField(blank=True, null=True)
    submission_hash = models.CharField(max_length=64, blank=True)
    submitted_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["applicant", "position"],
                name="unique_application_per_applicant_position",
            )
        ]

    def save(self, *args, **kwargs):
        self.branch = self.position.branch
        self.level = self.position.level
        super().save(*args, **kwargs)

    @property
    def is_editable_by_applicant(self):
        return self.status in {self.Status.DRAFT, self.Status.RETURNED_TO_APPLICANT}

    @property
    def applicant_display_name(self):
        full_name = " ".join(
            value for value in [self.applicant_first_name, self.applicant_last_name] if value
        ).strip()
        return full_name or str(self.applicant)

    @property
    def checklist_complete(self):
        return all(
            [
                self.checklist_privacy_consent,
                self.checklist_documents_complete,
                self.checklist_information_certified,
            ]
        )

    @property
    def performance_rating_not_applicable(self):
        return (
            self.performance_rating_applicability
            == self.PerformanceRatingApplicability.NOT_APPLICABLE
        )

    @property
    def otp_is_currently_valid(self):
        return bool(
            self.otp_verified_at
            and self.otp_expires_at
            and self.otp_expires_at >= timezone.now()
        )

    @property
    def reference_label(self):
        return self.reference_number or "Generated after final submission"

    @property
    def active_secretariat_override(self):
        return self.overrides.filter(
            is_active=True,
            target_role=RecruitmentUser.Role.SECRETARIAT,
        ).first()

    def __str__(self):
        return self.reference_number or f"Draft Application #{self.pk or 'new'}"


class RecruitmentCase(TimestampedModel):
    class Stage(models.TextChoices):
        SECRETARIAT_REVIEW = "secretariat_review", "Secretariat Review"
        HRM_CHIEF_REVIEW = "hrm_chief_review", "HRM Chief Review"
        HRMPSB_REVIEW = "hrmpsb_review", "HRMPSB Review"
        APPOINTING_AUTHORITY_REVIEW = "appointing_authority_review", "Appointing Authority Review"
        COMPLETION = "completion", "Completion Tracking"
        CLOSED = "closed", "Closed"

    class CaseStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        RETURNED_TO_APPLICANT = "returned_to_applicant", "Returned to Applicant"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    application = models.OneToOneField(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="case",
    )
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    current_stage = models.CharField(max_length=40, choices=Stage.choices)
    current_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    case_status = models.CharField(
        max_length=40,
        choices=CaseStatus.choices,
        default=CaseStatus.ACTIVE,
    )
    is_stage_locked = models.BooleanField(default=False)
    locked_stage = models.CharField(max_length=40, choices=Stage.choices, blank=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    reopened_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-updated_at"]

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        super().save(*args, **kwargs)

    @property
    def timeline_entries(self):
        return self.application.audit_logs.order_by("created_at")

    def __str__(self):
        return f"Case for {self.application.reference_label}"


class WorkflowOverride(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="overrides",
    )
    granted_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="workflow_overrides",
    )
    target_role = models.CharField(max_length=40, choices=RecruitmentUser.Role.choices)
    reason = models.TextField()
    is_active = models.BooleanField(default=True)
    used_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        if self.target_role != RecruitmentUser.Role.SECRETARIAT:
            raise ValidationError("Only Secretariat overrides are supported in this prototype.")
        if self.application.level != PositionPosting.Level.LEVEL_2:
            raise ValidationError("Overrides are only required for Level 2 applications.")

    def mark_used(self):
        self.is_active = False
        self.used_at = timezone.now()
        self.save(update_fields=["is_active", "used_at", "updated_at"])

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at", "updated_at"])

    def __str__(self):
        return f"Override for {self.application.reference_number}"


class RoutingHistory(TimestampedModel):
    class RouteType(models.TextChoices):
        INITIAL = "initial", "Initial Routing"
        FORWARD = "forward", "Forward Routing"
        OVERRIDE = "override", "Override Routing"
        REOPEN = "reopen", "Reopen Routing"
        CLOSE = "close", "Case Closure"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="routing_history",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="routing_history",
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="routing_history_actions",
        blank=True,
        null=True,
    )
    actor_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    route_type = models.CharField(max_length=20, choices=RouteType.choices)
    from_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    to_handler_role = models.CharField(
        max_length=40,
        choices=RecruitmentUser.Role.choices,
        blank=True,
    )
    from_status = models.CharField(
        max_length=40,
        choices=RecruitmentApplication.Status.choices,
        blank=True,
    )
    to_status = models.CharField(
        max_length=40,
        choices=RecruitmentApplication.Status.choices,
        blank=True,
    )
    from_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        blank=True,
    )
    to_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        blank=True,
    )
    description = models.TextField()
    notes = models.TextField(blank=True)
    is_override = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        target = self.to_handler_role or "closed"
        return f"{self.application.reference_label} -> {target}"


class ScreeningRecord(TimestampedModel):
    class CompletenessStatus(models.TextChoices):
        COMPLETE = "complete", "Complete"
        INCOMPLETE = "incomplete", "Incomplete"

    class QualificationOutcome(models.TextChoices):
        QUALIFIED = "qualified", "Qualified"
        NOT_QUALIFIED = "not_qualified", "Not Qualified"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="screening_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="screening_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    reviewed_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="screening_records",
    )
    reviewed_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    completeness_status = models.CharField(
        max_length=20,
        choices=CompletenessStatus.choices,
    )
    completeness_notes = models.TextField(blank=True)
    qualification_outcome = models.CharField(
        max_length=30,
        choices=QualificationOutcome.choices,
    )
    screening_notes = models.TextField(blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_screening_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_screening_record_per_application_stage",
            )
        ]

    def clean(self):
        if self.review_stage not in {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        }:
            raise ValidationError("Screening records are only supported for Secretariat and HRM Chief review stages.")
        if self.reviewed_by.role not in {
            RecruitmentUser.Role.SECRETARIAT,
            RecruitmentUser.Role.HRM_CHIEF,
        }:
            raise ValidationError("Only Secretariat or HRM Chief may record screening outputs.")
        if self.is_finalized and not self.finalized_by_id:
            raise ValidationError("Finalized screening outputs must record the finalizing user.")
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            raise ValidationError("Draft screening outputs cannot include finalization metadata.")

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.reviewed_by_role = self.reviewed_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage}"


class ExamRecord(TimestampedModel):
    class ExamStatus(models.TextChoices):
        COMPLETED = "completed", "Completed"
        WAIVED = "waived", "Waived"
        ABSENT = "absent", "Absent"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="exam_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="exam_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    recorded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_exam_records",
    )
    recorded_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    exam_type = models.CharField(max_length=100)
    exam_status = models.CharField(
        max_length=20,
        choices=ExamStatus.choices,
    )
    exam_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        blank=True,
        null=True,
    )
    exam_result = models.CharField(max_length=255, blank=True)
    technical_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        blank=True,
        null=True,
    )
    technical_result = models.CharField(max_length=255, blank=True)
    practical_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        blank=True,
        null=True,
    )
    practical_result = models.CharField(max_length=255, blank=True)
    exam_date = models.DateField(blank=True, null=True)
    administered_by = models.CharField(max_length=150, blank=True)
    valid_from = models.DateField(blank=True, null=True)
    valid_until = models.DateField(blank=True, null=True)
    exam_notes = models.TextField(blank=True)
    evidence_item = models.ForeignKey(
        "EvidenceVaultItem",
        on_delete=models.SET_NULL,
        related_name="exam_records",
        blank=True,
        null=True,
    )
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_exam_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_exam_record_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        if self.review_stage not in {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
        }:
            errors["review_stage"] = (
                "Examination records are only supported for Secretariat and HRM Chief review stages."
            )
        if self.recorded_by.role not in {
            RecruitmentUser.Role.SECRETARIAT,
            RecruitmentUser.Role.HRM_CHIEF,
        }:
            errors["recorded_by"] = "Only Secretariat or HRM Chief may record examination outputs."
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            errors["valid_until"] = "Validity end date cannot be earlier than the validity start date."
        if self.evidence_item_id:
            evidence_matches_context = (
                (
                    self.evidence_item.artifact_scope == EvidenceVaultItem.OwnerScope.APPLICATION
                    and self.evidence_item.application_id == self.application_id
                )
                or (
                    self.evidence_item.artifact_scope == EvidenceVaultItem.OwnerScope.CASE
                    and self.evidence_item.recruitment_case_id == self.recruitment_case_id
                )
                or (
                    self.evidence_item.artifact_scope == EvidenceVaultItem.OwnerScope.ENTRY
                    and self.evidence_item.recruitment_entry_id == self.application.position_id
                )
            )
            if not evidence_matches_context:
                errors["evidence_item"] = (
                    "Exam evidence must belong to the same application, case, or recruitment entry context."
                )
            if self.evidence_item.stage != self.review_stage:
                errors["evidence_item"] = "Exam evidence must match the examination review stage."
        score_fields = {
            "exam_score": self.exam_score,
            "technical_score": self.technical_score,
            "practical_score": self.practical_score,
        }
        if self.exam_status == self.ExamStatus.COMPLETED:
            has_score = any(value is not None for value in score_fields.values())
            has_component_result = bool(self.technical_result or self.practical_result)
            if not self.exam_date:
                errors["exam_date"] = "Provide the date the examination was administered."
            if not self.administered_by:
                errors["administered_by"] = "Record who administered the examination."
            if not has_score and not self.exam_result and not has_component_result:
                errors["exam_result"] = (
                    "Provide an overall result, technical/practical result, or score for completed examinations."
                )
        else:
            scored_fields = [
                field_name
                for field_name, value in score_fields.items()
                if value is not None
            ]
            for field_name in scored_fields:
                errors[field_name] = "Waived or absent exams must not store numeric scores."
            if self.technical_result or self.practical_result:
                errors["exam_result"] = "Waived or absent exams must not store component results."
            if self.valid_from or self.valid_until:
                errors["valid_from"] = "Only completed exams may record a validity period."
            if not self.exam_notes:
                errors["exam_notes"] = "Provide remarks explaining the waiver or absence."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized examination outputs must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft examination outputs cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recorded_by_role = self.recorded_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    @property
    def effective_score(self):
        if self.exam_score is not None:
            return self.exam_score
        component_scores = [
            score
            for score in [self.technical_score, self.practical_score]
            if score is not None
        ]
        if not component_scores:
            return None
        return (
            sum(component_scores, Decimal("0"))
            / Decimal(len(component_scores))
        ).quantize(Decimal("0.01"))

    @property
    def component_summary(self):
        parts = []
        if self.technical_score is not None or self.technical_result:
            label = "Technical"
            if self.technical_score is not None:
                label = f"{label}: {self.technical_score}"
            if self.technical_result:
                label = f"{label} ({self.technical_result})"
            parts.append(label)
        if self.practical_score is not None or self.practical_result:
            label = "Practical"
            if self.practical_score is not None:
                label = f"{label}: {self.practical_score}"
            if self.practical_result:
                label = f"{label} ({self.practical_result})"
            parts.append(label)
        return "; ".join(parts)

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} examination"


class InterviewSession(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="interview_sessions",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="interview_sessions",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="interview_sessions",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    scheduled_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="scheduled_interview_sessions",
    )
    scheduled_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    scheduled_for = models.DateTimeField()
    location = models.CharField(max_length=255)
    session_notes = models.TextField(blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_interview_sessions",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "scheduled_for", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_interview_session_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        expected_roles = {
            RecruitmentCase.Stage.SECRETARIAT_REVIEW: RecruitmentUser.Role.SECRETARIAT,
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
            RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        }
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Interview sessions must be linked to a recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Interview sessions must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = "Interview sessions must reference the recruitment entry of the application."
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Interview sessions must stay linked to the recruitment entry of the same application."
            )
        if self.review_stage not in expected_roles:
            errors["review_stage"] = (
                "Interview sessions are only supported during Secretariat, HRM Chief, or HRMPSB review stages."
            )
        elif self.scheduled_by.role != expected_roles[self.review_stage]:
            errors["scheduled_by"] = (
                "Only the authorized current-stage handler may schedule or update the interview session."
            )
        if self.branch == PositionPosting.Branch.COS and self.review_stage == RecruitmentCase.Stage.HRMPSB_REVIEW:
            errors["review_stage"] = "COS interview sessions cannot be scheduled during an HRMPSB review stage."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized interview sessions must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft interview sessions cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recruitment_entry = self.application.position
        self.scheduled_by_role = self.scheduled_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} interview"


class InterviewRating(TimestampedModel):
    interview_session = models.ForeignKey(
        InterviewSession,
        on_delete=models.CASCADE,
        related_name="ratings",
    )
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="interview_ratings",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="interview_ratings",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    rated_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="interview_ratings",
    )
    rated_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    rating_score = models.DecimalField(max_digits=5, decimal_places=2)
    rating_notes = models.TextField(blank=True)
    justification = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["interview_session", "rated_by"],
                name="unique_interview_rating_per_session_evaluator",
            )
        ]

    def clean(self):
        errors = {}
        allowed_roles = {
            RecruitmentCase.Stage.HRM_CHIEF_REVIEW: RecruitmentUser.Role.HRM_CHIEF,
            RecruitmentCase.Stage.HRMPSB_REVIEW: RecruitmentUser.Role.HRMPSB_MEMBER,
        }
        if self.interview_session_id and self.interview_session.is_finalized:
            errors["interview_session"] = "Finalized interview sessions cannot accept additional rating changes."
        if self.application_id and self.interview_session_id and self.interview_session.application_id != self.application_id:
            errors["application"] = "Interview ratings must stay linked to the same application as the interview session."
        if self.recruitment_case_id and self.interview_session_id and self.interview_session.recruitment_case_id != self.recruitment_case_id:
            errors["recruitment_case"] = "Interview ratings must stay linked to the same recruitment case as the interview session."
        if self.interview_session_id and self.review_stage != self.interview_session.review_stage:
            errors["review_stage"] = "Interview ratings must use the same workflow stage as the interview session."
        if self.review_stage not in allowed_roles:
            errors["review_stage"] = "Direct interview ratings are only supported during HRM Chief or HRMPSB review stages."
        elif self.rated_by.role != allowed_roles[self.review_stage]:
            errors["rated_by"] = "Only the authorized evaluator for the current stage may record an interview rating."
        try:
            score = Decimal(str(self.rating_score))
        except (InvalidOperation, TypeError):
            score = None
        if score is None or score < 0 or score > 100:
            errors["rating_score"] = "Interview ratings must be between 0 and 100."
        if score is not None and score < Decimal("75") and not self.justification:
            errors["justification"] = "Provide a justification when the interview rating is below the passing threshold."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.application = self.interview_session.application
        self.recruitment_case = self.interview_session.recruitment_case
        self.review_stage = self.interview_session.review_stage
        self.branch = self.interview_session.branch
        self.level = self.interview_session.level
        self.rated_by_role = self.rated_by.role
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.application.reference_label} rating by {self.rated_by}"


class DeliberationRecord(TimestampedModel):
    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="deliberation_records",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="deliberation_records",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="deliberation_records",
        blank=True,
        null=True,
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    recorded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_deliberation_records",
    )
    recorded_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    deliberated_at = models.DateTimeField(default=timezone.now)
    deliberation_minutes = models.TextField()
    decision_support_summary = models.TextField()
    ranking_position = models.PositiveIntegerField(blank=True, null=True)
    ranking_notes = models.TextField(blank=True)
    consolidated_snapshot = models.JSONField(default=dict, blank=True)
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_deliberation_records",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "deliberated_at", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "review_stage"],
                name="unique_deliberation_record_per_application_stage",
            )
        ]

    def clean(self):
        errors = {}
        expected_roles = {
            PositionPosting.Branch.COS: (
                RecruitmentCase.Stage.HRM_CHIEF_REVIEW,
                RecruitmentUser.Role.HRM_CHIEF,
            ),
            PositionPosting.Branch.PLANTILLA: (
                RecruitmentCase.Stage.HRMPSB_REVIEW,
                RecruitmentUser.Role.HRMPSB_MEMBER,
            ),
        }
        expected_stage, expected_role = expected_roles.get(self.branch, ("", ""))
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Deliberation records must be linked to a recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Deliberation records must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = "Deliberation records must reference the recruitment entry of the application."
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Deliberation records must stay linked to the recruitment entry of the same application."
            )
        if expected_stage and self.review_stage != expected_stage:
            errors["review_stage"] = (
                "Deliberation is only supported during the HRM Chief review stage for COS or "
                "the HRMPSB review stage for Plantilla."
            )
        if expected_role and self.recorded_by.role != expected_role:
            errors["recorded_by"] = "Only the authorized decision-support handler may record deliberation minutes."
        if self.ranking_position is not None and self.ranking_position < 1:
            errors["ranking_position"] = "Ranking position must be a positive whole number."
        if self.is_finalized and not self.consolidated_snapshot:
            errors["consolidated_snapshot"] = "Finalized deliberation records must preserve the consolidated source snapshot."
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = "Finalized deliberation records must record the finalizing user."
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = "Draft deliberation records cannot include finalization metadata."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.application.branch
        self.level = self.application.level
        self.recruitment_entry = self.application.position
        self.recorded_by_role = self.recorded_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.application.reference_label} {self.review_stage} deliberation"


class ComparativeAssessmentReport(TimestampedModel):
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_reports",
    )
    review_stage = models.CharField(max_length=40, choices=RecruitmentCase.Stage.choices)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    generated_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="generated_comparative_assessment_reports",
    )
    generated_by_role = models.CharField(max_length=40, blank=True)
    summary_notes = models.TextField(blank=True)
    consolidated_snapshot = models.JSONField(default=dict, blank=True)
    version_number = models.PositiveIntegerField(default=1)
    evidence_item = models.ForeignKey(
        "EvidenceVaultItem",
        on_delete=models.SET_NULL,
        related_name="comparative_assessment_reports",
        blank=True,
        null=True,
    )
    is_finalized = models.BooleanField(default=False)
    finalized_at = models.DateTimeField(blank=True, null=True)
    finalized_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="finalized_comparative_assessment_reports",
        blank=True,
        null=True,
    )
    finalized_by_role = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["review_stage", "-version_number", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recruitment_entry", "review_stage", "version_number"],
                name="unique_car_version_per_entry_stage",
            )
        ]

    def clean(self):
        errors = {}
        if self.recruitment_entry.branch != PositionPosting.Branch.PLANTILLA:
            errors["recruitment_entry"] = (
                "Comparative Assessment Reports are only supported for Plantilla recruitment entries."
            )
        if self.review_stage != RecruitmentCase.Stage.HRMPSB_REVIEW:
            errors["review_stage"] = (
                "Comparative Assessment Reports are only supported during the HRMPSB review stage."
            )
        if self.generated_by.role != RecruitmentUser.Role.HRMPSB_MEMBER:
            errors["generated_by"] = (
                "Only an HRMPSB Member may generate or update a Comparative Assessment Report."
            )
        if self.version_number < 1:
            errors["version_number"] = "CAR version number must be a positive whole number."
        if self.is_finalized and not self.evidence_item_id:
            errors["evidence_item"] = (
                "Finalized Comparative Assessment Reports must link to the generated PDF artifact."
            )
        if self.evidence_item_id:
            if self.evidence_item.artifact_scope != EvidenceVaultItem.OwnerScope.ENTRY:
                errors["evidence_item"] = (
                    "Comparative Assessment Reports must link to an entry-owned Evidence Vault artifact."
                )
            elif self.evidence_item.recruitment_entry_id != self.recruitment_entry_id:
                errors["evidence_item"] = (
                    "The generated CAR artifact must stay linked to the same recruitment entry as the report."
                )
        if self.is_finalized and not self.finalized_by_id:
            errors["finalized_by"] = (
                "Finalized Comparative Assessment Reports must record the finalizing user."
            )
        if not self.is_finalized and (self.finalized_by_id or self.finalized_at):
            errors["finalized_at"] = (
                "Draft Comparative Assessment Reports cannot include finalization metadata."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.branch = self.recruitment_entry.branch
        self.generated_by_role = self.generated_by.role
        if self.finalized_by_id:
            self.finalized_by_role = self.finalized_by.role
        elif not self.is_finalized:
            self.finalized_by_role = ""
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.is_finalized

    def __str__(self):
        return f"{self.recruitment_entry.job_code} {self.review_stage} CAR v{self.version_number}"


class ComparativeAssessmentReportItem(TimestampedModel):
    report = models.ForeignKey(
        ComparativeAssessmentReport,
        on_delete=models.CASCADE,
        related_name="items",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="comparative_assessment_report_items",
    )
    deliberation_record = models.ForeignKey(
        DeliberationRecord,
        on_delete=models.PROTECT,
        related_name="comparative_assessment_report_items",
    )
    rank_order = models.PositiveIntegerField()
    qualification_outcome = models.CharField(max_length=40, blank=True)
    exam_status = models.CharField(max_length=20, blank=True)
    exam_score = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    interview_average_score = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    decision_support_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["rank_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["report", "recruitment_case"],
                name="unique_car_item_per_report_case",
            ),
            models.UniqueConstraint(
                fields=["report", "rank_order"],
                name="unique_car_rank_per_report",
            ),
        ]

    def clean(self):
        errors = {}
        if (
            self.report_id
            and self.recruitment_case_id
            and self.recruitment_case.application.position_id != self.report.recruitment_entry_id
        ):
            errors["recruitment_case"] = (
                "CAR items must stay linked to a recruitment case from the same recruitment entry as the report."
            )
        if (
            self.deliberation_record_id
            and self.recruitment_case_id
            and self.deliberation_record.recruitment_case_id != self.recruitment_case_id
        ):
            errors["deliberation_record"] = (
                "CAR items must stay linked to the same recruitment case as the deliberation record."
            )
        if self.rank_order < 1:
            errors["rank_order"] = "CAR rank order must be a positive whole number."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.recruitment_case_id and self.deliberation_record_id:
            self.recruitment_case = self.deliberation_record.recruitment_case
        super().save(*args, **kwargs)

    @property
    def application(self):
        return self.recruitment_case.application

    def __str__(self):
        return f"{self.report} #{self.rank_order}"


class FinalDecision(TimestampedModel):
    class Outcome(models.TextChoices):
        SELECTED = "selected", "Selected"
        NOT_SELECTED = "not_selected", "Not Selected"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="final_decisions",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="final_decisions",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="final_decisions",
    )
    review_stage = models.CharField(
        max_length=40,
        choices=RecruitmentCase.Stage.choices,
        default=RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
    )
    decided_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="recorded_final_decisions",
    )
    decided_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    decision_outcome = models.CharField(max_length=20, choices=Outcome.choices)
    decision_notes = models.TextField()
    submission_packet_snapshot = models.JSONField(default=dict, blank=True)
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-created_at"]

    def _expected_review_stage_and_decider_role(self):
        branch = self.application.branch if self.application_id else self.branch
        if branch == PositionPosting.Branch.COS:
            return RecruitmentCase.Stage.HRM_CHIEF_REVIEW, RecruitmentUser.Role.HRM_CHIEF
        return RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW, RecruitmentUser.Role.APPOINTING_AUTHORITY

    def clean(self):
        errors = {}
        expected_stage, expected_role = self._expected_review_stage_and_decider_role()
        if self.review_stage != expected_stage:
            expected_stage_label = RecruitmentCase.Stage(expected_stage).label
            errors["review_stage"] = (
                f"Final decisions are only supported during the {expected_stage_label} stage."
            )
        if not self.recruitment_case_id:
            errors["recruitment_case"] = "Final decisions must be linked to the recruitment case."
        elif self.application_id and self.recruitment_case.application_id != self.application_id:
            errors["recruitment_case"] = (
                "Final decisions must stay linked to the recruitment case of the same application."
            )
        if not self.recruitment_entry_id:
            errors["recruitment_entry"] = (
                "Final decisions must reference the recruitment entry of the same application."
            )
        elif self.application_id and self.recruitment_entry_id != self.application.position_id:
            errors["recruitment_entry"] = (
                "Final decisions must stay linked to the recruitment entry of the same application."
            )
        if self.decided_by.role != expected_role:
            expected_role_label = RecruitmentUser.Role(expected_role).label
            errors["decided_by"] = f"Only the {expected_role_label} may record this final decision."
        if not self.submission_packet_snapshot:
            errors["submission_packet_snapshot"] = (
                "Final decisions must preserve the submission packet snapshot."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recruitment_case = getattr(self.application, "case", None)
        self.recruitment_entry = self.application.position
        self.branch = self.application.branch
        self.level = self.application.level
        self.review_stage, _ = self._expected_review_stage_and_decider_role()
        self.decided_by_role = self.decided_by.role
        super().save(*args, **kwargs)

    @property
    def is_selected(self):
        return self.decision_outcome == self.Outcome.SELECTED

    def __str__(self):
        return f"{self.application.reference_label} {self.get_decision_outcome_display()}"


class NotificationLog(TimestampedModel):
    class NotificationType(models.TextChoices):
        SUBMISSION_ACKNOWLEDGMENT = "submission_acknowledgment", "Submission Acknowledgment"
        SELECTED_APPLICANT = "selected_applicant", "Selected Applicant Notification"
        NON_SELECTED_APPLICANT = "non_selected_applicant", "Non-selected Applicant Notification"
        REQUIREMENT_CHECKLIST = "requirement_checklist", "Requirement Checklist Notification"
        REMINDER = "reminder", "Reminder Notification"

    class DeliveryChannel(models.TextChoices):
        EMAIL = "email", "Email"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="notifications",
        blank=True,
        null=True,
    )
    triggered_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="triggered_notifications",
        blank=True,
        null=True,
    )
    triggered_by_role = models.CharField(max_length=40, blank=True)
    notification_type = models.CharField(
        max_length=40,
        choices=NotificationType.choices,
    )
    delivery_channel = models.CharField(
        max_length=20,
        choices=DeliveryChannel.choices,
        default=DeliveryChannel.EMAIL,
    )
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    related_status = models.CharField(
        max_length=40,
        choices=RecruitmentApplication.Status.choices,
        blank=True,
    )
    recipient_name = models.CharField(max_length=255, blank=True)
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    sent_at = models.DateTimeField(blank=True, null=True)
    failure_details = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.triggered_by_id:
            self.triggered_by_role = self.triggered_by.role
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_notification_type_display()} to {self.recipient_email}"


class CompletionRecord(TimestampedModel):
    application = models.OneToOneField(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="completion_record",
    )
    recruitment_case = models.OneToOneField(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="completion_record",
    )
    tracked_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="completion_records",
    )
    tracked_by_role = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=20, choices=PositionPosting.Branch.choices)
    level = models.PositiveSmallIntegerField(choices=PositionPosting.Level.choices)
    completion_reference = models.CharField(max_length=255, blank=True)
    completion_date = models.DateField(blank=True, null=True)
    deadline = models.DateField(blank=True, null=True)
    announcement_reference = models.CharField(max_length=255, blank=True)
    announcement_date = models.DateField(blank=True, null=True)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def clean(self):
        errors = {}
        if (
            self.application_id
            and self.recruitment_case_id
            and self.recruitment_case.application_id != self.application_id
        ):
            errors["recruitment_case"] = "Completion tracking must point to the same application as the recruitment case."
        if self.tracked_by.role not in {
            RecruitmentUser.Role.SECRETARIAT,
            RecruitmentUser.Role.HRM_CHIEF,
        }:
            errors["tracked_by"] = "Only Secretariat or HRM Chief may manage completion tracking."
        if self.announcement_date and not self.announcement_reference:
            errors["announcement_reference"] = "Provide an announcement reference when setting an announcement date."
        if self.branch == PositionPosting.Branch.COS and (
            self.announcement_reference or self.announcement_date
        ):
            errors["announcement_reference"] = (
                "Announcement tracking is only supported for Plantilla completion handling."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.recruitment_case_id and not self.application_id:
            self.application = self.recruitment_case.application
        if self.application_id and not self.recruitment_case_id and hasattr(self.application, "case"):
            self.recruitment_case = self.application.case
        self.branch = self.application.branch
        self.level = self.application.level
        self.tracked_by_role = self.tracked_by.role
        super().save(*args, **kwargs)

    @property
    def completion_label(self):
        if self.branch == PositionPosting.Branch.PLANTILLA:
            return "Appointment"
        return "Contract"

    @property
    def has_pending_requirements(self):
        return self.requirements.filter(
            status=CompletionRequirement.RequirementStatus.PENDING
        ).exists()

    @property
    def requirements_ready_for_closure(self):
        return self.requirements.exists() and not self.has_pending_requirements

    @property
    def has_completion_reference_for_closure(self):
        return bool((self.completion_reference or "").strip())

    @property
    def has_completion_date_for_closure(self):
        return bool(self.completion_date)

    @property
    def ready_for_closure(self):
        return (
            self.requirements_ready_for_closure
            and self.has_completion_reference_for_closure
            and self.has_completion_date_for_closure
        )

    @property
    def closure_blockers(self):
        blockers = []
        completion_label = self.completion_label.lower()
        if not self.requirements.exists():
            blockers.append("Add at least one completion requirement item before closing the case.")
        elif self.has_pending_requirements:
            blockers.append(
                "All completion requirements must be marked completed or not applicable before closing the case."
            )
        if not self.has_completion_reference_for_closure:
            blockers.append(f"Record the {completion_label} reference before closing the case.")
        if not self.has_completion_date_for_closure:
            blockers.append(f"Record the {completion_label} date before closing the case.")
        return blockers

    @property
    def completed_requirement_count(self):
        return self.requirements.exclude(
            status=CompletionRequirement.RequirementStatus.PENDING
        ).count()

    @property
    def total_requirement_count(self):
        return self.requirements.count()

    def __str__(self):
        return f"{self.completion_label} completion for {self.application.reference_label}"


class CompletionRequirement(TimestampedModel):
    class RequirementStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        NOT_APPLICABLE = "not_applicable", "Not Applicable"

    completion_record = models.ForeignKey(
        CompletionRecord,
        on_delete=models.CASCADE,
        related_name="requirements",
    )
    item_label = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=RequirementStatus.choices,
        default=RequirementStatus.PENDING,
    )
    notes = models.TextField(blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "created_at"]

    def clean(self):
        if not self.item_label.strip():
            raise ValidationError({"item_label": "Requirement item label is required."})

    def save(self, *args, **kwargs):
        self.item_label = self.item_label.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_label} ({self.get_status_display()})"


class EvidenceVaultItem(TimestampedModel):
    class OwnerScope(models.TextChoices):
        APPLICATION = "application", "Application"
        CASE = "case", "Recruitment Case"
        ENTRY = "entry", "Recruitment Entry"

    class Stage(models.TextChoices):
        APPLICANT_INTAKE = "applicant_intake", "Applicant Intake"
        SECRETARIAT_REVIEW = RecruitmentCase.Stage.SECRETARIAT_REVIEW, "Secretariat Review"
        HRM_CHIEF_REVIEW = RecruitmentCase.Stage.HRM_CHIEF_REVIEW, "HRM Chief Review"
        HRMPSB_REVIEW = RecruitmentCase.Stage.HRMPSB_REVIEW, "HRMPSB Review"
        APPOINTING_AUTHORITY_REVIEW = (
            RecruitmentCase.Stage.APPOINTING_AUTHORITY_REVIEW,
            "Appointing Authority Review",
        )
        COMPLETION = RecruitmentCase.Stage.COMPLETION, "Completion Tracking"
        CLOSED = RecruitmentCase.Stage.CLOSED, "Closed"

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="evidence_items",
        blank=True,
        null=True,
    )
    recruitment_case = models.ForeignKey(
        RecruitmentCase,
        on_delete=models.CASCADE,
        related_name="evidence_items",
        blank=True,
        null=True,
    )
    recruitment_entry = models.ForeignKey(
        PositionPosting,
        on_delete=models.PROTECT,
        related_name="evidence_items",
        blank=True,
        null=True,
    )
    artifact_scope = models.CharField(
        max_length=20,
        choices=OwnerScope.choices,
        default=OwnerScope.APPLICATION,
    )
    artifact_type = models.CharField(max_length=80, blank=True, default="supporting_document")
    uploaded_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="uploaded_evidence",
    )
    uploaded_by_role = models.CharField(max_length=40, blank=True, default="")
    stage = models.CharField(
        max_length=40,
        choices=Stage.choices,
        default=Stage.APPLICANT_INTAKE,
    )
    label = models.CharField(max_length=150)
    document_key = models.CharField(max_length=150, db_index=True, editable=False, default="")
    version_family = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    version_number = models.PositiveIntegerField(default=1)
    previous_version = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="next_versions",
        blank=True,
        null=True,
    )
    is_current_version = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False)
    archive_tag = models.CharField(max_length=255, blank=True)
    archived_at = models.DateTimeField(blank=True, null=True)
    archived_by = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="archived_evidence",
        blank=True,
        null=True,
    )
    archived_by_role = models.CharField(max_length=40, blank=True, default="")
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveIntegerField()
    digest_algorithm = models.CharField(max_length=20, default="sha256")
    sha256_digest = models.CharField(max_length=64)
    nonce = models.BinaryField()
    ciphertext = models.BinaryField()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["version_family", "version_number"],
                name="unique_evidence_version_per_family",
            ),
            models.CheckConstraint(
                name="evidence_owner_matches_scope",
                condition=(
                    (
                        models.Q(artifact_scope="application")
                        & models.Q(application__isnull=False)
                        & models.Q(recruitment_case__isnull=True)
                        & models.Q(recruitment_entry__isnull=True)
                    )
                    | (
                        models.Q(artifact_scope="case")
                        & models.Q(application__isnull=True)
                        & models.Q(recruitment_case__isnull=False)
                        & models.Q(recruitment_entry__isnull=True)
                    )
                    | (
                        models.Q(artifact_scope="entry")
                        & models.Q(application__isnull=True)
                        & models.Q(recruitment_case__isnull=True)
                        & models.Q(recruitment_entry__isnull=False)
                    )
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["artifact_scope", "application", "stage", "document_key"]),
            models.Index(fields=["artifact_scope", "recruitment_case", "stage", "document_key"]),
            models.Index(fields=["artifact_scope", "recruitment_entry", "stage", "document_key"]),
            models.Index(fields=["is_archived", "stage"]),
        ]

    @staticmethod
    def build_document_key(label):
        normalized = slugify(label or "")
        return normalized[:150] or f"artifact-{uuid.uuid4().hex[:12]}"

    def owner_signature(self):
        if self.artifact_scope == self.OwnerScope.APPLICATION:
            return self.artifact_scope, self.application_id
        if self.artifact_scope == self.OwnerScope.CASE:
            return self.artifact_scope, self.recruitment_case_id
        return self.artifact_scope, self.recruitment_entry_id

    def clean(self):
        errors = {}
        owner_count = sum(
            bool(owner_id)
            for owner_id in [self.application_id, self.recruitment_case_id, self.recruitment_entry_id]
        )
        if owner_count != 1:
            errors["artifact_scope"] = (
                "Evidence must belong to exactly one owner scope: application, recruitment case, or recruitment entry."
            )
        if self.artifact_scope == self.OwnerScope.APPLICATION and not self.application_id:
            errors["application"] = "Application-owned evidence must point to an application."
        if self.artifact_scope == self.OwnerScope.CASE and not self.recruitment_case_id:
            errors["recruitment_case"] = "Case-owned evidence must point to a recruitment case."
        if self.artifact_scope == self.OwnerScope.ENTRY and not self.recruitment_entry_id:
            errors["recruitment_entry"] = "Entry-owned evidence must point to a recruitment entry."
        if self.is_archived and not self.archive_tag.strip():
            errors["archive_tag"] = "Provide an archive tag when marking evidence as archived."
        if self.previous_version_id:
            if self.previous_version.owner_signature() != self.owner_signature():
                errors["previous_version"] = "Version history must stay within the same artifact owner scope."
            if self.previous_version.document_key != self.document_key:
                errors["document_key"] = "Version history must stay within the same document key."
            if self.previous_version.artifact_scope != self.artifact_scope:
                errors["artifact_scope"] = "Version history must stay within the same artifact scope."
            if self.previous_version.version_family != self.version_family:
                errors["version_family"] = "Version history must stay within the same version family."
            if self.previous_version.version_number >= self.version_number:
                errors["version_number"] = "Version number must increase from the previous evidence version."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.document_key:
            self.document_key = self.build_document_key(self.label)
        if self.uploaded_by_id:
            self.uploaded_by_role = self.uploaded_by.role
        if not self.artifact_type:
            self.artifact_type = "supporting_document"
        if self.archived_by_id:
            self.archived_by_role = self.archived_by.role
        elif not self.is_archived:
            self.archived_by_role = ""
        super().save(*args, **kwargs)

    @property
    def version_label(self):
        return f"v{self.version_number}"

    @property
    def owning_application(self):
        if self.application_id:
            return self.application
        if self.recruitment_case_id:
            return self.recruitment_case.application
        return None

    @property
    def owning_case(self):
        return self.recruitment_case

    @property
    def owning_recruitment_entry(self):
        if self.recruitment_entry_id:
            return self.recruitment_entry
        if self.application_id:
            return self.application.position
        if self.recruitment_case_id:
            return self.recruitment_case.application.position
        return None

    def __str__(self):
        owner = self.owning_application
        if owner is not None:
            owner_label = owner.reference_number or f"application-{owner.pk}"
        else:
            entry = self.owning_recruitment_entry
            owner_label = entry.job_code if entry is not None else f"artifact-{self.pk or 'new'}"
        return f"{owner_label} - {self.label} ({self.version_label})"


class AuditLog(TimestampedModel):
    class Action(models.TextChoices):
        INTERNAL_LOGIN = "internal_login", "Internal Login"
        INTERNAL_LOGOUT = "internal_logout", "Internal Logout"
        PASSWORD_CHANGED = "password_changed", "Password Changed"
        INTERNAL_ACCOUNT_CREATED = "internal_account_created", "Internal Account Created"
        INTERNAL_ACCOUNT_UPDATED = "internal_account_updated", "Internal Account Updated"
        INTERNAL_ACCOUNT_ACTIVATED = "internal_account_activated", "Internal Account Activated"
        INTERNAL_ACCOUNT_DEACTIVATED = "internal_account_deactivated", "Internal Account Deactivated"
        INTERNAL_ROLE_CHANGED = "internal_role_changed", "Internal Role Changed"
        POSITION_CREATED = "position_created", "Position Created"
        POSITION_UPDATED = "position_updated", "Position Updated"
        RECRUITMENT_ENTRY_CREATED = "recruitment_entry_created", "Recruitment Entry Created"
        RECRUITMENT_ENTRY_UPDATED = "recruitment_entry_updated", "Recruitment Entry Updated"
        RECRUITMENT_ENTRY_STATUS_CHANGED = "recruitment_entry_status_changed", "Recruitment Entry Status Changed"
        APPLICATION_CREATED = "application_created", "Application Created"
        APPLICATION_UPDATED = "application_updated", "Application Updated"
        APPLICATION_OTP_SENT = "application_otp_sent", "Application OTP Sent"
        APPLICATION_OTP_VERIFIED = "application_otp_verified", "Application OTP Verified"
        APPLICATION_SUBMITTED = "application_submitted", "Application Submitted"
        CASE_CREATED = "case_created", "Case Created"
        CASE_REOPENED = "case_reopened", "Case Reopened"
        ROUTED = "routed", "Application Routed"
        SCREENING_RECORDED = "screening_recorded", "Screening Recorded"
        SCREENING_FINALIZED = "screening_finalized", "Screening Finalized"
        EXAM_RECORDED = "exam_recorded", "Exam Recorded"
        EXAM_FINALIZED = "exam_finalized", "Exam Finalized"
        INTERVIEW_SCHEDULED = "interview_scheduled", "Interview Scheduled"
        INTERVIEW_FINALIZED = "interview_finalized", "Interview Finalized"
        INTERVIEW_RATING_RECORDED = "interview_rating_recorded", "Interview Rating Recorded"
        INTERVIEW_FALLBACK_UPLOADED = "interview_fallback_uploaded", "Interview Fallback Uploaded"
        DELIBERATION_RECORDED = "deliberation_recorded", "Deliberation Recorded"
        DELIBERATION_FINALIZED = "deliberation_finalized", "Deliberation Finalized"
        CAR_GENERATED = "car_generated", "Comparative Assessment Report Generated"
        CAR_FINALIZED = "car_finalized", "Comparative Assessment Report Finalized"
        DECISION_RECORDED = "decision_recorded", "Decision Recorded"
        COMPLETION_RECORDED = "completion_recorded", "Completion Recorded"
        CASE_CLOSED = "case_closed", "Case Closed"
        NOTIFICATION_SENT = "notification_sent", "Notification Sent"
        NOTIFICATION_FAILED = "notification_failed", "Notification Failed"
        OVERRIDE_GRANTED = "override_granted", "Override Granted"
        OVERRIDE_USED = "override_used", "Override Used"
        EVIDENCE_UPLOADED = "evidence_uploaded", "Evidence Uploaded"
        EVIDENCE_DOWNLOADED = "evidence_downloaded", "Evidence Downloaded"
        EVIDENCE_ARCHIVED = "evidence_archived", "Evidence Archived"
        EVIDENCE_RESTORED = "evidence_restored", "Evidence Restored"
        PROTECTED_RECORD_VIEWED = "protected_record_viewed", "Protected Record Viewed"
        EVIDENCE_VAULT_VIEWED = "evidence_vault_viewed", "Evidence Vault Viewed"
        AUDIT_LOG_VIEWED = "audit_log_viewed", "Audit Log Viewed"
        EXPORT_GENERATED = "export_generated", "Export Generated"

    SENSITIVE_ACTIONS = {
        Action.CASE_REOPENED,
        Action.EXPORT_GENERATED,
        Action.EVIDENCE_DOWNLOADED,
        Action.OVERRIDE_GRANTED,
        Action.OVERRIDE_USED,
        Action.PROTECTED_RECORD_VIEWED,
        Action.EVIDENCE_VAULT_VIEWED,
        Action.AUDIT_LOG_VIEWED,
    }

    application = models.ForeignKey(
        RecruitmentApplication,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        blank=True,
        null=True,
    )
    actor = models.ForeignKey(
        RecruitmentUser,
        on_delete=models.PROTECT,
        related_name="audit_logs",
        blank=True,
        null=True,
    )
    actor_role = models.CharField(max_length=40, blank=True)
    case_reference = models.CharField(max_length=30, blank=True)
    workflow_stage = models.CharField(max_length=40, blank=True)
    action = models.CharField(max_length=50, choices=Action.choices)
    description = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    is_sensitive_access = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def _infer_workflow_stage(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        for key in ("to_stage", "case_stage", "review_stage", "stage"):
            value = metadata.get(key)
            if value:
                return value
        if self.application_id:
            case = getattr(self.application, "case", None)
            if case and case.current_stage:
                return case.current_stage
            return self.application.status
        return ""

    def save(self, *args, **kwargs):
        if self.actor_id and not self.actor_role:
            self.actor_role = self.actor.role
        if self.application_id and not self.case_reference:
            self.case_reference = self.application.reference_number or ""
        if not self.workflow_stage:
            self.workflow_stage = self._infer_workflow_stage()
        if self.action in self.SENSITIVE_ACTIONS:
            self.is_sensitive_access = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_action_display()} @ {self.created_at:%Y-%m-%d %H:%M}"
