from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.forms.models import ModelChoiceIteratorValue, construct_instance
from django.utils import timezone

from .models import (
    AuditLog,
    ComparativeAssessmentReport,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    EvidenceVaultItem,
    FinalDecision,
    InterviewRating,
    InterviewSession,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentUser,
    ScreeningRecord,
)
from .requirements import get_applicant_document_requirements
from .services import (
    get_available_actions,
    get_case_handoff_options,
    get_current_applicant_document_map,
)
from .upload_validation import validate_applicant_document_upload


AUDIT_ACTION_CHOICE_LABELS = {
    AuditLog.Action.APPLICATION_OTP_SENT: "Verification Code Sent",
    AuditLog.Action.APPLICATION_OTP_VERIFIED: "Email Verified",
    AuditLog.Action.ROUTED: "Case Assigned",
    AuditLog.Action.INTERVIEW_FALLBACK_UPLOADED: "Interview Rating File Uploaded",
    AuditLog.Action.CAR_GENERATED: "Comparative Assessment Report Created",
    AuditLog.Action.OVERRIDE_GRANTED: "Special Authorization Recorded",
    AuditLog.Action.OVERRIDE_USED: "Special Authorization Used",
    AuditLog.Action.EVIDENCE_UPLOADED: "File Uploaded",
    AuditLog.Action.EVIDENCE_DOWNLOADED: "File Downloaded",
    AuditLog.Action.EVIDENCE_ARCHIVED: "File Archived",
    AuditLog.Action.EVIDENCE_RESTORED: "File Restored",
    AuditLog.Action.EVIDENCE_VAULT_VIEWED: "Secured Files Viewed",
    AuditLog.Action.EXPORT_GENERATED: "Export Created",
}


def audit_action_choices():
    return [
        ("", "All actions"),
        *[
            (value, AUDIT_ACTION_CHOICE_LABELS.get(value, label))
            for value, label in AuditLog.Action.choices
        ],
    ]


class BootstrapFormMixin:
    def _apply_bootstrap(self):
        for field in self.fields.values():
            css_class = field.widget.attrs.get("class", "")
            if isinstance(field.widget, forms.HiddenInput):
                continue
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = f"{css_class} form-check-input".strip()
            elif isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                field.widget.attrs["class"] = f"{css_class} form-select".strip()
            else:
                field.widget.attrs["class"] = f"{css_class} form-control".strip()

    def _apply_fixed_choice_display(self, field_name, fixed_attr, label_attr):
        options = [
            (str(value), str(label))
            for value, label in self.fields[field_name].choices
            if value not in ("", None)
        ]
        is_fixed = len(options) == 1
        setattr(self, fixed_attr, is_fixed)
        setattr(self, label_attr, "")
        if not is_fixed:
            return

        value, label = options[0]
        setattr(self, label_attr, label)
        self.fields[field_name].widget = forms.HiddenInput(
            attrs={"data-fixed-label": label}
        )

        instance = getattr(self, "instance", None)
        current_value = self.initial.get(field_name) or (
            getattr(instance, field_name, "") if instance is not None else ""
        )
        self.fields[field_name].initial = current_value or value
        if not self.is_bound and not current_value:
            self.initial[field_name] = value


class DeferredModelValidationMixin:
    """
    Workflow record forms collect only user-editable fields.
    Actor, review stage, linked case/entry, and generated snapshots are attached
    later in the service layer before the model is fully validated and saved.
    """

    def _post_clean(self):
        opts = self._meta
        self.instance = construct_instance(self, self.instance, opts.fields, opts.exclude)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        cleaned_files = []
        file_list = data if isinstance(data, (list, tuple)) else [data]
        for item in file_list:
            if item:
                cleaned_files.append(super().clean(item, initial))
        if self.required and not cleaned_files:
            raise forms.ValidationError("At least one file is required.")
        return cleaned_files


class PositionReferenceSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        instance = value.instance if isinstance(value, ModelChoiceIteratorValue) else None
        if instance is not None:
            option["attrs"].update(
                {
                    "data-position-title": instance.position_title or "",
                    "data-salary-grade": instance.salary_grade or "",
                    "data-level-classification": instance.get_level_classification_display()
                    if instance.level_classification
                    else "",
                    "data-class-id": instance.class_id or "",
                    "data-os-code": instance.os_code or "",
                    "data-occupational-service": instance.occupational_service or "",
                    "data-occupational-group": instance.occupational_group or "",
                    "data-reference-status": instance.reference_status or "",
                    "data-reference-status-label": instance.get_reference_status_display(),
                    "data-reference-warning": instance.get_selection_warning(),
                    "data-is-active": "true" if instance.is_active else "false",
                }
            )
        return option


def internal_role_choices():
    return [
        choice
        for choice in RecruitmentUser.Role.choices
        if choice[0] != RecruitmentUser.Role.APPLICANT
    ]


class InternalAuthenticationForm(BootstrapFormMixin, AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autofocus": True}))
    password = forms.CharField(strip=False, widget=forms.PasswordInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_internal_user:
            raise forms.ValidationError(
                "This sign-in page is restricted to internal users.",
                code="non_internal_user",
            )


class InternalPasswordChangeForm(BootstrapFormMixin, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class InternalUserCreateForm(BootstrapFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = RecruitmentUser
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "employee_id",
            "office_name",
            "role",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = internal_role_choices()
        self.fields["email"].required = True
        self.fields["is_active"].initial = True
        self._apply_bootstrap()


class InternalUserUpdateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = RecruitmentUser
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "employee_id",
            "office_name",
            "role",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = internal_role_choices()
        self.fields["email"].required = True
        self._apply_bootstrap()


class ApplicantPortalIntakeForm(BootstrapFormMixin, forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50)
    qualification_summary = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    cover_letter = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    performance_rating_applicability = forms.ChoiceField(
        choices=RecruitmentApplication.PerformanceRatingApplicability.choices,
        widget=forms.RadioSelect,
        label="Performance Rating Availability",
    )
    checklist_privacy_consent = forms.BooleanField(
        label="I consent to the use of my submitted information for recruitment processing.",
    )
    checklist_documents_complete = forms.BooleanField()
    checklist_information_certified = forms.BooleanField(
        label="I certify that the submitted information and uploaded documents are true and complete.",
    )

    def __init__(self, *args, entry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry = entry
        self.existing_draft = None
        self.existing_documents_by_code = {}
        self.document_slots = []
        self.duplicate_document_warnings = []
        self.saved_draft_notice = ""
        self.document_requirements = get_applicant_document_requirements(
            entry.branch if entry else None
        )
        self.document_requirements_by_code = {
            requirement.code: requirement for requirement in self.document_requirements
        }
        self.document_upload_field_names = []
        for requirement in self.document_requirements:
            help_text = requirement.help_text
            help_text = f"{help_text} Combine multiple pages or certificates into one file when needed."
            self.fields[requirement.file_field_name] = forms.FileField(
                required=False,
                label=requirement.title,
                help_text=help_text,
                widget=forms.ClearableFileInput(
                    attrs={"accept": ".pdf,.jpg,.jpeg,.png"}
                ),
            )
            self.document_upload_field_names.append(requirement.file_field_name)
        branch_label = (
            entry.get_branch_display() if entry else "selected"
        )
        self.fields["performance_rating_applicability"].help_text = (
            "Select whether you have a performance rating for the last rating period."
        )
        self.fields["checklist_documents_complete"].label = (
            f"I completed the document checklist for the {branch_label} application path."
        )
        if entry and entry.branch == PositionPosting.Branch.COS:
            self.fields["performance_rating_applicability"].required = False
            self.fields["performance_rating_applicability"].initial = (
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            )
        self._apply_bootstrap()
        self.fields["performance_rating_applicability"].widget.attrs["class"] = "rg-pub-radio-list"
        self._refresh_document_slots()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def get_requirement_uploads(self):
        return {
            requirement.code: self.cleaned_data.get(requirement.file_field_name)
            for requirement in self.document_requirements
            if self.cleaned_data.get(requirement.file_field_name)
            and requirement.file_field_name not in self.errors
        }

    def get_valid_requirement_uploads(self):
        return {
            requirement.code: self.cleaned_data.get(requirement.file_field_name)
            for requirement in self.document_requirements
            if self.cleaned_data.get(requirement.file_field_name)
            and requirement.file_field_name not in self.errors
        }

    def _normalize_requirement_upload(self, uploaded_file):
        if not uploaded_file:
            return None
        filename = (getattr(uploaded_file, "name", "") or "").strip()
        if not filename:
            return None
        return uploaded_file

    def can_persist_draft_uploads(self):
        core_fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "qualification_summary",
        ]
        return all(self.cleaned_data.get(field_name) for field_name in core_fields)

    def attach_existing_draft(self, draft, *, saved_notice=""):
        self.existing_draft = draft
        if draft is None:
            self.existing_documents_by_code = {}
        else:
            self.existing_documents_by_code = get_current_applicant_document_map(draft)
        if saved_notice:
            self.saved_draft_notice = saved_notice
        self._refresh_document_slots()

    def _current_performance_rating_applicability(self):
        cleaned_data = getattr(self, "cleaned_data", {})
        value = cleaned_data.get("performance_rating_applicability")
        if value:
            return value
        return (self.data.get("performance_rating_applicability") or "").strip()

    def _performance_rating_is_applicable(self):
        return (
            self._current_performance_rating_applicability()
            == RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
        )

    def _refresh_document_slots(self):
        applicability_value = self._current_performance_rating_applicability()
        self.document_slots = []
        for requirement in self.document_requirements:
            is_required_now = requirement.is_required or (
                requirement.conditional_on_performance_rating
                and applicability_value
                == RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            )
            self.document_slots.append(
                {
                    "requirement": requirement,
                    "field": self[requirement.file_field_name],
                    "saved_evidence": self.existing_documents_by_code.get(requirement.code),
                    "label_tag": requirement.applicant_label,
                    "is_required_now": is_required_now,
                    "applicability_field": (
                        self["performance_rating_applicability"]
                        if requirement.conditional_on_performance_rating
                        else None
                    ),
                }
            )
        self.document_upload_fields = [slot["field"] for slot in self.document_slots]

    def _build_duplicate_document_warnings(self):
        digest_to_codes = {}
        for requirement_code, uploaded_file in self.get_valid_requirement_uploads().items():
            try:
                validated_upload = validate_applicant_document_upload(uploaded_file)
            except ValueError:
                continue
            digest_to_codes.setdefault(validated_upload.sha256_digest, set()).add(
                requirement_code
            )
        for requirement_code, evidence in self.existing_documents_by_code.items():
            digest_to_codes.setdefault(evidence.sha256_digest, set()).add(requirement_code)
        warnings = []
        for requirement_codes in digest_to_codes.values():
            if len(requirement_codes) < 2:
                continue
            requirement_titles = ", ".join(
                sorted(
                    self.document_requirements_by_code[requirement_code].title
                    for requirement_code in requirement_codes
                    if requirement_code in self.document_requirements_by_code
                )
            )
            warnings.append(
                "The same file appears to be attached to multiple document slots: "
                f"{requirement_titles}. Please confirm each slot has the correct file."
            )
        return warnings

    def clean(self):
        cleaned_data = super().clean()
        if self.entry and not self.entry.is_open_for_intake:
            raise forms.ValidationError(
                "The selected recruitment entry is not currently open for intake."
            )
        if self.entry and self.entry.branch == PositionPosting.Branch.COS:
            cleaned_data["performance_rating_applicability"] = (
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            )
        for requirement in self.document_requirements:
            uploaded_file = self._normalize_requirement_upload(
                cleaned_data.get(requirement.file_field_name)
            )
            cleaned_data[requirement.file_field_name] = uploaded_file
            if not uploaded_file:
                continue
            try:
                validate_applicant_document_upload(uploaded_file)
            except ValueError as exc:
                self.add_error(
                    requirement.file_field_name,
                    str(exc),
                )

        applicant_email = cleaned_data.get("email")
        if self.entry and applicant_email:
            existing_draft = (
                RecruitmentApplication.objects.filter(
                    position=self.entry,
                    applicant_email__iexact=applicant_email,
                    submitted_at__isnull=True,
                    status=RecruitmentApplication.Status.DRAFT,
                )
                .prefetch_related("evidence_items")
                .order_by("-updated_at", "-created_at")
                .first()
            )
            self.attach_existing_draft(existing_draft)
            duplicate_exists = RecruitmentApplication.objects.filter(
                position=self.entry,
                applicant_email__iexact=applicant_email,
                submitted_at__isnull=False,
            ).exists()
            if duplicate_exists:
                self.add_error(
                    "email",
                    "An application for this recruitment entry has already been submitted using this email address.",
                )

        if (
            cleaned_data.get("performance_rating_applicability")
            == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            and cleaned_data.get("performance_rating")
            and "performance_rating" not in self.errors
        ):
            self.add_error(
                "performance_rating_applicability",
                "You marked the performance rating as not applicable, so remove the uploaded file or change your selection.",
            )

        existing_document_codes = set(self.existing_documents_by_code)
        for requirement in self.document_requirements:
            uploaded_file = cleaned_data.get(requirement.file_field_name)
            is_required_now = requirement.is_required or (
                requirement.conditional_on_performance_rating
                and cleaned_data.get("performance_rating_applicability")
                == RecruitmentApplication.PerformanceRatingApplicability.APPLICABLE
            )
            if not is_required_now:
                continue
            if (
                not uploaded_file
                and requirement.code not in existing_document_codes
            ):
                self.add_error(
                    requirement.file_field_name,
                    f"Upload the required document for {requirement.title}.",
                )
        self.duplicate_document_warnings = self._build_duplicate_document_warnings()
        self._refresh_document_slots()
        return cleaned_data


class ApplicantOTPForm(BootstrapFormMixin, forms.Form):
    otp = forms.CharField(max_length=6, min_length=6, label="Verification Code")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_otp(self):
        otp = self.cleaned_data["otp"].strip()
        if not otp.isdigit():
            raise forms.ValidationError("Enter the 6-digit verification code sent to your email address.")
        return otp


class ApplicantStatusLookupForm(BootstrapFormMixin, forms.Form):
    application_id = forms.CharField(max_length=30, label="Application ID")
    email = forms.EmailField(label="Applicant email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_application_id(self):
        return self.cleaned_data["application_id"].strip().upper()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class EvidenceUploadForm(BootstrapFormMixin, forms.Form):
    label = forms.CharField(max_length=150)
    file = forms.FileField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["label"].help_text = (
            "If a file with the same label already exists for this step, the system keeps both versions."
        )
        self._apply_bootstrap()

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        if uploaded_file.size > settings.MAX_EVIDENCE_UPLOAD_BYTES:
            raise forms.ValidationError(
                "The uploaded file is larger than the allowed file size."
            )
        return uploaded_file


class EvidenceVaultSearchForm(BootstrapFormMixin, forms.Form):
    ARCHIVAL_STATUS_CHOICES = (
        ("active", "Active only"),
        ("archived", "Archived only"),
        ("all", "All files"),
    )

    q = forms.CharField(required=False, label="Search")
    stage = forms.ChoiceField(
        required=False,
        choices=[("", "All steps"), *EvidenceVaultItem.Stage.choices],
        label="Step",
    )
    artifact_scope = forms.ChoiceField(
        required=False,
            choices=[("", "All file scopes"), *EvidenceVaultItem.OwnerScope.choices],
            label="File Scope",
    )
    archival_status = forms.ChoiceField(
        required=False,
        choices=ARCHIVAL_STATUS_CHOICES,
        initial="active",
        label="Archive Status",
    )
    current_version_only = forms.BooleanField(
        required=False,
        initial=True,
        label="Show latest versions only",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].help_text = (
            "Search by Application ID, recruitment entry, file label, filename, SHA-256 hash, archive label, or uploader."
        )
        self._apply_bootstrap()


class AuditLogSearchForm(BootstrapFormMixin, forms.Form):
    q = forms.CharField(required=False, label="Search")
    action = forms.ChoiceField(
        required=False,
        choices=audit_action_choices(),
        label="Action",
    )
    actor_role = forms.ChoiceField(
        required=False,
        choices=[("", "All roles"), *RecruitmentUser.Role.choices],
        label="User Role",
    )
    sensitive_only = forms.BooleanField(
        required=False,
        label="Sensitive records only",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].help_text = (
            "Search by case reference, step, description, or username."
        )
        self._apply_bootstrap()


class EvidenceArchiveForm(BootstrapFormMixin, forms.Form):
    ACTION_CHOICES = (
        ("archive", "Archive"),
        ("restore", "Restore"),
    )

    action = forms.ChoiceField(choices=ACTION_CHOICES)
    archive_tag = forms.CharField(
        required=False,
        max_length=255,
        label="Archive Label",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("action") == "archive" and not (cleaned_data.get("archive_tag") or "").strip():
            self.add_error("archive_tag", "Enter an archive label before archiving this file.")
        return cleaned_data


class WorkflowActionForm(BootstrapFormMixin, forms.Form):
    action = forms.ChoiceField()
    remarks = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, application, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.action_is_fixed = False
        self.action_fixed_label = ""
        self.fields["action"].choices = get_available_actions(application, user)
        self._apply_fixed_choice_display(
            "action",
            "action_is_fixed",
            "action_fixed_label",
        )
        self._apply_bootstrap()

    def clean_action(self):
        action = self.cleaned_data["action"]
        valid_actions = {value for value, _label in self.fields["action"].choices}
        if action not in valid_actions:
            raise forms.ValidationError(
                "This action is not allowed at the current step."
            )
        return action


class CaseHandoffForm(BootstrapFormMixin, forms.Form):
    target_role = forms.ChoiceField(label="Send To")
    remarks = forms.CharField(
        label="Reason or Remarks",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, application, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_role_is_fixed = False
        self.target_role_fixed_label = ""
        self.fields["target_role"].choices = get_case_handoff_options(application, user)
        self._apply_fixed_choice_display(
            "target_role",
            "target_role_is_fixed",
            "target_role_fixed_label",
        )
        self._apply_bootstrap()

    def clean_target_role(self):
        target_role = self.cleaned_data["target_role"]
        valid_roles = {value for value, _label in self.fields["target_role"].choices}
        if target_role not in valid_roles:
            raise forms.ValidationError("This receiving office is not allowed for this case.")
        return target_role


class WorkflowOverrideForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(
        label="Reason for Special Authorization",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class WorkflowReopenForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(
        label="Reason for Reopening",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class RequirementChecklistNotificationForm(BootstrapFormMixin, forms.Form):
    checklist_items = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="List the appointment or contract requirements to email to the applicant.",
    )
    deadline = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    additional_message = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["checklist_items"].label = "Requirement Checklist"
        self.fields["additional_message"].label = "Additional Instructions"
        self._apply_bootstrap()

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        if deadline and deadline < timezone.localdate():
            raise forms.ValidationError("Deadline cannot be earlier than today.")
        return deadline


class ReminderNotificationForm(BootstrapFormMixin, forms.Form):
    reminder_subject = forms.CharField(max_length=255)
    reminder_message = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    deadline = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reminder_subject"].label = "Reminder Subject"
        self.fields["reminder_message"].label = "Reminder Message"
        self._apply_bootstrap()

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        if deadline and deadline < timezone.localdate():
            raise forms.ValidationError("Deadline cannot be earlier than today.")
        return deadline


class CompletionTrackingForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompletionRecord
        fields = [
            "completion_reference",
            "completion_date",
            "deadline",
            "announcement_reference",
            "announcement_date",
            "remarks",
        ]
        widgets = {
            "completion_date": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
            "announcement_date": forms.DateInput(attrs={"type": "date"}),
            "remarks": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, application=None, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        if application is not None:
            self.instance.application = application
            if hasattr(application, "case"):
                self.instance.recruitment_case = application.case
            self.instance.branch = application.branch
            self.instance.level = application.level
        if actor is not None:
            self.instance.tracked_by = actor
        branch = getattr(application, "branch", "")
        if branch == PositionPosting.Branch.PLANTILLA:
            self.fields["completion_reference"].label = "Appointment Reference"
            self.fields["completion_date"].label = "Appointment Date"
            self.fields["announcement_reference"].label = "Announcement Reference"
            self.fields["announcement_date"].label = "Announcement Date"
        else:
            self.fields["completion_reference"].label = "Contract Reference"
            self.fields["completion_date"].label = "Contract Date"
            self.fields.pop("announcement_reference")
            self.fields.pop("announcement_date")
        self.fields["deadline"].label = "Completion Deadline"
        self.fields["remarks"].label = "Completion Notes"
        self.fields["completion_reference"].required = False
        self.fields["completion_date"].required = False
        self.fields["deadline"].required = False
        self.fields["remarks"].required = False
        self._apply_bootstrap()

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        if deadline and deadline < timezone.localdate():
            raise forms.ValidationError("Completion deadline cannot be earlier than today.")
        return deadline


class CompletionRequirementForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompletionRequirement
        fields = [
            "item_label",
            "status",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_label"].label = "Requirement Item"
        self.fields["status"].label = "Status"
        self.fields["notes"].label = "Notes"
        self.fields["notes"].required = False
        self._apply_bootstrap()


class BaseCompletionRequirementFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active_forms = 0
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            if (form.cleaned_data.get("item_label") or "").strip():
                active_forms += 1
        if active_forms == 0:
            raise forms.ValidationError("Add at least one completion requirement item.")


CompletionRequirementFormSet = inlineformset_factory(
    CompletionRecord,
    CompletionRequirement,
    form=CompletionRequirementForm,
    formset=BaseCompletionRequirementFormSet,
    extra=3,
    can_delete=True,
)


class CaseClosureForm(BootstrapFormMixin, forms.Form):
    closure_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["closure_notes"].label = "Closure Notes"
        self._apply_bootstrap()


class ScreeningReviewForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ScreeningRecord
        fields = [
            "completeness_status",
            "completeness_notes",
            "qualification_outcome",
            "screening_notes",
        ]
        widgets = {
            "completeness_notes": forms.Textarea(attrs={"rows": 3}),
            "screening_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["completeness_status"].label = "Completeness Finding"
        self.fields["completeness_notes"].label = "Completeness Observations"
        self.fields["qualification_outcome"].label = "Qualification Outcome"
        self.fields["screening_notes"].label = "Screening Notes"
        self._apply_bootstrap()


class ExamRecordForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    SCORE_RANGE_MESSAGE = "Enter a score from 0 to 100."
    SCORE_FIELD_NAMES = ("exam_score", "technical_score", "practical_score")

    evidence_file = forms.FileField(
        label="Optional Supporting File",
        required=False,
        help_text="Attach a supporting exam file when available. This is saved with the secured case files.",
    )

    class Meta:
        model = ExamRecord
        fields = [
            "exam_type",
            "exam_status",
            "exam_score",
            "exam_result",
            "technical_score",
            "technical_result",
            "practical_score",
            "practical_result",
            "exam_date",
            "administered_by",
            "valid_from",
            "valid_until",
            "exam_notes",
            "evidence_file",
        ]
        widgets = {
            "exam_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "technical_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "practical_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "exam_result": forms.HiddenInput(),
            "technical_result": forms.HiddenInput(),
            "practical_result": forms.HiddenInput(),
            "exam_date": forms.DateInput(attrs={"type": "date"}),
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "exam_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.application = kwargs.pop("application", None)
        super().__init__(*args, **kwargs)
        self.component_section_label = "Technical and Practical Components"
        self.component_weight_display = "Technical and practical scores are recorded separately."
        self.exam_type_is_fixed = False
        self.exam_type_fixed_label = ""
        self.administered_by_is_fixed = False
        self.administered_by_fixed_label = ""
        self.fields["exam_type"].label = "Exam Type"
        self.fields["exam_status"].label = "Exam Status"
        self.fields["exam_score"].label = "Overall / Single Exam Score"
        self.fields["exam_result"].label = "Overall Result"
        self.fields["technical_score"].label = "Technical Score"
        self.fields["technical_result"].label = "Technical Result"
        self.fields["practical_score"].label = "Practical Score"
        self.fields["practical_result"].label = "Practical Result"
        self.fields["exam_date"].label = "Exam Date"
        self.fields["administered_by"].label = "Administered By"
        self.fields["valid_from"].label = "Validity Start"
        self.fields["valid_until"].label = "Validity End"
        self.fields["exam_notes"].label = "Exam Notes / Remarks"
        self.fields["exam_notes"].required = False
        self.fields["exam_result"].required = False
        self.fields["technical_score"].required = False
        self.fields["technical_result"].required = False
        self.fields["practical_score"].required = False
        self.fields["practical_result"].required = False
        self.fields["exam_date"].required = False
        self.fields["administered_by"].required = False
        self.fields["valid_from"].required = False
        self.fields["valid_until"].required = False
        self.fields["exam_score"].required = False
        self.fields["exam_type"].choices = self._exam_type_choices()
        self.fields["administered_by"].choices = self._administered_by_choices()
        for field_name in self.SCORE_FIELD_NAMES:
            self.fields[field_name].widget.attrs.update(
                {
                    "min": "0",
                    "max": "100",
                    "data-score-limit": "true",
                    "data-score-label": self.fields[field_name].label,
                }
            )
        self.fields["exam_type"].help_text = "Set by the recruitment branch and hiring-process rules."
        self.fields["administered_by"].help_text = "Office responsible under the CHD hiring process."
        self.fields["exam_score"].help_text = (
            "Use only when an official overall score is available. The system does not decide pass/fail or apply unstated weighting."
        )
        if self.application:
            self.fields["technical_score"].label = "Technical Score"
            self.fields["practical_score"].label = "Practical Score"
            if self.application.branch == PositionPosting.Branch.COS:
                self.component_section_label = "COS Examination Components"
        self._apply_fixed_choice_display(
            "exam_type",
            "exam_type_is_fixed",
            "exam_type_fixed_label",
        )
        self._apply_fixed_choice_display(
            "administered_by",
            "administered_by_is_fixed",
            "administered_by_fixed_label",
        )
        if self.exam_type_is_fixed:
            self.fields["exam_type"].help_text = (
                "Automatically set from the recruitment branch and hiring-process rules."
            )
        if self.administered_by_is_fixed:
            self.fields["administered_by"].help_text = (
                "Automatically set from the branch-specific hiring-process procedure."
            )
        self._apply_bootstrap()

    def _exam_type_choices(self):
        choices = [("", "Select exam type")]
        if not self.application:
            return choices + list(ExamRecord.ExamType.choices)
        if self.application.branch == PositionPosting.Branch.COS:
            return choices + [
                (
                    ExamRecord.ExamType.END_USER_ASSESSMENT,
                    ExamRecord.ExamType.END_USER_ASSESSMENT.label,
                )
            ]
        return choices + [
            (
                ExamRecord.ExamType.TECHNICAL_PRACTICAL,
                ExamRecord.ExamType.TECHNICAL_PRACTICAL.label,
            )
        ]

    def _administered_by_choices(self):
        choices = [("", "Select administering office")]
        if not self.application:
            return choices + list(ExamRecord.AdministeredBy.choices)
        if self.application.branch == PositionPosting.Branch.COS:
            return choices + [
                (
                    ExamRecord.AdministeredBy.END_USER,
                    ExamRecord.AdministeredBy.END_USER.label,
                ),
            ]
        return choices + [
            (
                ExamRecord.AdministeredBy.HRMS,
                ExamRecord.AdministeredBy.HRMS.label,
            ),
        ]

    def _required_score_fields_for_type(self, exam_type):
        if exam_type == ExamRecord.ExamType.TECHNICAL_PRACTICAL:
            return ("technical_score", "practical_score")
        if exam_type == ExamRecord.ExamType.END_USER_ASSESSMENT:
            return ("practical_score",)
        return ()

    def clean(self):
        cleaned_data = super().clean()
        for field_name in self.SCORE_FIELD_NAMES:
            value = cleaned_data.get(field_name)
            if value is not None and (value < 0 or value > 100):
                self.add_error(field_name, self.SCORE_RANGE_MESSAGE)

        exam_status = cleaned_data.get("exam_status")
        score_values = {
            field_name: cleaned_data.get(field_name)
            for field_name in self.SCORE_FIELD_NAMES
        }
        if exam_status == ExamRecord.ExamStatus.COMPLETED:
            if not cleaned_data.get("exam_date"):
                self.add_error("exam_date", "Provide the date the examination was administered.")
            if not cleaned_data.get("administered_by"):
                self.add_error("administered_by", "Select who administered the examination.")
            for field_name in self._required_score_fields_for_type(cleaned_data.get("exam_type")):
                if score_values[field_name] is None and not self.has_error(field_name):
                    self.add_error(field_name, "Enter this required exam score.")
        elif exam_status in {ExamRecord.ExamStatus.WAIVED, ExamRecord.ExamStatus.ABSENT}:
            for field_name, value in score_values.items():
                if value is not None:
                    self.add_error(field_name, "Remove numeric scores for waived or absent exams.")
            if cleaned_data.get("valid_from") or cleaned_data.get("valid_until"):
                self.add_error("valid_from", "Only completed exams may record a validity period.")
            if not cleaned_data.get("exam_notes"):
                self.add_error("exam_notes", "Provide remarks explaining the waiver or absence.")
        return cleaned_data

    def clean_evidence_file(self):
        uploaded_file = self.cleaned_data.get("evidence_file")
        if uploaded_file and uploaded_file.size > settings.MAX_EVIDENCE_UPLOAD_BYTES:
            raise forms.ValidationError(
                "The uploaded file is larger than the allowed file size."
            )
        return uploaded_file


class InterviewSessionForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    scheduled_for = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = InterviewSession
        fields = [
            "scheduled_for",
            "location",
            "session_notes",
        ]
        widgets = {
            "session_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_for"].label = "Interview Schedule"
        self.fields["location"].label = "Interview Location / Medium"
        self.fields["session_notes"].label = "Session Notes"
        self.fields["session_notes"].required = False
        self._apply_bootstrap()


class InterviewRatingForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = InterviewRating
        fields = [
            "rating_score",
            "rating_notes",
            "justification",
        ]
        widgets = {
            "rating_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "rating_notes": forms.Textarea(attrs={"rows": 3}),
            "justification": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["rating_score"].label = "Interview Rating Score"
        self.fields["rating_notes"].label = "Rating Notes"
        self.fields["justification"].label = "Justification"
        self.fields["rating_notes"].required = False
        self.fields["justification"].required = False
        self._apply_bootstrap()


class InterviewFallbackUploadForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(label="Scanned Fallback Rating Sheet")
    remarks = forms.CharField(
        label="Upload Remarks",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class DeliberationRecordForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    deliberated_at = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = DeliberationRecord
        fields = [
            "deliberated_at",
            "deliberation_minutes",
            "decision_support_summary",
            "ranking_position",
            "ranking_notes",
        ]
        widgets = {
            "deliberation_minutes": forms.Textarea(attrs={"rows": 4}),
            "decision_support_summary": forms.Textarea(attrs={"rows": 4}),
            "ranking_position": forms.NumberInput(attrs={"min": "1", "step": "1"}),
            "ranking_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["deliberated_at"].label = "Deliberation Date and Time"
        self.fields["deliberation_minutes"].label = "Deliberation Minutes / Record"
        self.fields["decision_support_summary"].label = "Decision-Support Summary"
        self.fields["ranking_position"].label = "Ranking Position"
        self.fields["ranking_notes"].label = "Ranking Notes"
        self.fields["ranking_position"].required = False
        self.fields["ranking_notes"].required = False
        self._apply_bootstrap()


class ComparativeAssessmentReportForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ComparativeAssessmentReport
        fields = [
            "summary_notes",
        ]
        widgets = {
            "summary_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["summary_notes"].label = "CAR Summary Notes"
        self.fields["summary_notes"].required = False
        self._apply_bootstrap()


class FinalDecisionForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = FinalDecision
        fields = [
            "decision_outcome",
            "decision_notes",
        ]
        widgets = {
            "decision_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["decision_outcome"].label = "Final Outcome"
        self.fields["decision_notes"].label = "Decision Notes / Remarks"
        self._apply_bootstrap()


class PositionReferenceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PositionReference
        fields = [
            "position_title",
            "position_slug",
            "salary_grade",
            "level_classification",
            "class_id",
            "os_code",
            "occupational_service",
            "occupational_group",
            "reference_status",
            "is_active",
            "notes",
            "position_code",
            "agency_item_number",
            "office_division_default",
            "qs_education",
            "qs_training",
            "qs_experience",
            "qs_eligibility",
            "employment_track_applicability",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "qs_education": forms.Textarea(attrs={"rows": 2}),
            "qs_training": forms.Textarea(attrs={"rows": 2}),
            "qs_experience": forms.Textarea(attrs={"rows": 2}),
            "qs_eligibility": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position_slug"].help_text = "Leave blank to generate the slug automatically from the title."
        self._apply_bootstrap()


class RecruitmentEntryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PositionPosting
        fields = [
            "position_reference",
            "job_code",
            "branch",
            "intake_mode",
            "status",
            "publication_date",
            "opening_date",
            "closing_date",
            "qualification_reference",
        ]
        widgets = {
            "position_reference": PositionReferenceSelect(),
            "publication_date": forms.DateInput(attrs={"type": "date"}),
            "opening_date": forms.DateInput(attrs={"type": "date"}),
            "closing_date": forms.DateInput(attrs={"type": "date"}),
            "qualification_reference": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position_reference"].queryset = PositionReference.objects.filter(is_active=True).order_by(
            "position_title",
            "salary_grade",
            "class_id",
        )
        if self.instance.pk and self.instance.position_reference_id:
            self.fields["position_reference"].queryset = (
                self.fields["position_reference"].queryset
                | PositionReference.objects.filter(pk=self.instance.position_reference_id)
            )
        self.fields["position_reference"].empty_label = "Select an official position"
        self.fields["position_reference"].label = "Position Reference"
        self.fields["position_reference"].help_text = (
            "Choose from the official Position Reference list. Official positions are not typed in manually here."
        )
        self.fields["job_code"].label = "Entry Code"
        self.fields["job_code"].required = False
        self.fields["job_code"].disabled = True
        self.fields["job_code"].help_text = (
            "Generated automatically for tracking. This cannot be edited."
        )
        self.fields["job_code"].widget.attrs.update(
            {
                "readonly": "readonly",
                "placeholder": "Will be generated automatically after first save",
            }
        )
        self.fields["branch"].label = "Engagement Type"
        self.fields["qualification_reference"].label = "Entry Notes / Qualification Reference"
        self.selected_position_reference = self._resolve_selected_position_reference()
        self._apply_bootstrap()

    def _resolve_selected_position_reference(self):
        selected_value = None
        if self.is_bound:
            selected_value = self.data.get(self.add_prefix("position_reference"))
        elif self.instance.pk and self.instance.position_reference_id:
            selected_value = self.instance.position_reference_id
        if not selected_value:
            return None
        try:
            return PositionReference.objects.filter(pk=selected_value).first()
        except (TypeError, ValueError):
            return None

    def clean(self):
        cleaned_data = super().clean()
        position_reference = cleaned_data.get("position_reference")
        branch = cleaned_data.get("branch")
        intake_mode = cleaned_data.get("intake_mode")
        closing_date = cleaned_data.get("closing_date")
        submitted_job_code = (self.data.get(self.add_prefix("job_code")) or "").strip().upper()
        existing_job_code = (self.instance.job_code or "").strip().upper()

        if submitted_job_code and submitted_job_code != existing_job_code:
            self.add_error(
                "job_code",
                "Entry Code is generated automatically for tracking and cannot be edited.",
            )

        if position_reference is None:
            self.add_error("position_reference", "Select a position reference before creating the recruitment entry.")
        else:
            if not position_reference.is_active:
                self.add_error(
                    "position_reference",
                    "Inactive position references cannot be used for recruitment entries.",
                )
            elif position_reference.routing_level is None:
                self.add_error(
                    "position_reference",
                    "This position reference is missing the level classification needed for assignment.",
                )
            else:
                self.instance.level = position_reference.routing_level
                self.instance.title = position_reference.position_title

        if branch == PositionPosting.Branch.PLANTILLA and intake_mode != PositionPosting.IntakeMode.FIXED_PERIOD:
            self.add_error("intake_mode", "Plantilla entries must use the fixed period intake mode.")

        if branch == PositionPosting.Branch.COS and intake_mode == PositionPosting.IntakeMode.FIXED_PERIOD:
            self.add_error(
                "intake_mode",
                "COS entries may only use opening-based, continuous, or pooling intake.",
            )

        if (
            branch == PositionPosting.Branch.COS
            and intake_mode in {PositionPosting.IntakeMode.CONTINUOUS, PositionPosting.IntakeMode.POOLING}
            and closing_date
        ):
            self.add_error(
                "closing_date",
                "Continuous or pooling COS entries must not define a fixed closing date.",
            )

        return cleaned_data
