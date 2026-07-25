import re
import unicodedata
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.contrib.auth.hashers import check_password
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.forms.models import ModelChoiceIteratorValue, construct_instance
from django.utils.html import format_html
from django.utils import timezone

from .captcha import (
    captcha_uses_recaptcha,
    captcha_uses_turnstile,
    get_or_create_captcha_challenge,
    validate_captcha_answer,
)
from .models import (
    AUDIT_ACTION_CATEGORIES,
    AttendanceException,
    AuditLog,
    ComparativeAssessmentReport,
    ComparativeAssessmentReportItem,
    CompetencyDefinition,
    CompetencyRatingTemplate,
    CompletionRecord,
    CompletionRequirement,
    DeliberationRecord,
    ExamRecord,
    ExamSchedule,
    EvidenceVaultItem,
    FinalDecision,
    format_weight_percentage,
    InterviewRating,
    InterviewSession,
    InternalPasswordHistory,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningDocumentReview,
    ScreeningRecord,
    VacancyAssessmentWeights,
)
from .requirements import (
    APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH,
    MIN_REQUIRED_DOCUMENT_CODES,
    PERFORMANCE_RATING,
    PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS,
    get_applicant_document_requirements,
)
from .services import (
    TOP_FIVE_SELECTION_LIMIT,
    compute_competency_rating_score,
    get_available_actions,
    get_case_handoff_options,
    get_current_applicant_document_map,
)
from .upload_validation import validate_applicant_document_upload


APPLICANT_NAME_ALLOWED_PATTERN = r"^(?=.*[^\W\d_])(?:[^\W\d_]|[ .'-])+$"
APPLICANT_NAME_ERROR_MESSAGE = "Please enter your name using letters only."
APPLICANT_MOBILE_ERROR_MESSAGE = (
    "Enter a Philippine mobile number, like 0917 123 4567."
)
APPLICANT_QUALIFICATION_SUMMARY_MAX_LENGTH = 1000
APPLICANT_QUALIFICATION_SUMMARY_LENGTH_ERROR_MESSAGE = (
    "Please shorten this to 1,000 characters or less."
)
APPLICANT_NAME_MAX_LENGTH = 80
APPLICANT_NAME_LENGTH_ERROR_MESSAGE = (
    "Please shorten this to 80 characters or less."
)
APPLICANT_COVER_LETTER_MAX_LENGTH = 1500
APPLICANT_COVER_LETTER_LENGTH_ERROR_MESSAGE = (
    "Please shorten this to 1,500 characters or less."
)

_APPLICANT_NAME_ALLOWED_RE = re.compile(APPLICANT_NAME_ALLOWED_PATTERN, re.UNICODE)
_APPLICANT_MOBILE_SEPARATORS_RE = re.compile(r"[ ()-]")
_APPLICANT_LOCAL_MOBILE_RE = re.compile(r"09\d{9}")
_APPLICANT_INTERNATIONAL_MOBILE_RE = re.compile(r"(?:\+63|63)9\d{9}")


def normalize_philippine_mobile_number(value):
    compact_value = _APPLICANT_MOBILE_SEPARATORS_RE.sub("", (value or "").strip())
    if _APPLICANT_LOCAL_MOBILE_RE.fullmatch(compact_value):
        return f"+63{compact_value[1:]}"
    if _APPLICANT_INTERNATIONAL_MOBILE_RE.fullmatch(compact_value):
        return compact_value if compact_value.startswith("+") else f"+{compact_value}"
    raise ValueError(APPLICANT_MOBILE_ERROR_MESSAGE)


AUDIT_ACTION_CHOICE_LABELS = {
    AuditLog.Action.INTERNAL_LOGIN_FAILED: "Internal Login Failed",
    AuditLog.Action.INTERNAL_LOGIN_LOCKED: "Internal Login Locked",
    AuditLog.Action.INTERNAL_LOGIN_ALERT_SENT: "Internal Login Alert Sent",
    AuditLog.Action.INTERNAL_LOGIN_ALERT_FAILED: "Internal Login Alert Failed",
    AuditLog.Action.INTERNAL_MFA_SENT: "Internal Verification Code Sent",
    AuditLog.Action.INTERNAL_MFA_RESENT: "Internal Verification Code Resent",
    AuditLog.Action.INTERNAL_MFA_VERIFIED: "Internal MFA Verified",
    AuditLog.Action.INTERNAL_MFA_FAILED: "Internal MFA Failed",
    AuditLog.Action.INTERNAL_MFA_EXPIRED: "Internal MFA Expired",
    AuditLog.Action.INTERNAL_MFA_LOCKED: "Internal MFA Locked",
    AuditLog.Action.PASSWORD_RESET_REQUESTED: "Password Reset Requested",
    AuditLog.Action.PASSWORD_RESET_COMPLETED: "Password Reset Completed",
    AuditLog.Action.INTERNAL_EMAIL_CHANGE_REQUESTED: "Internal Email Change Requested",
    AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED: "Internal Email Change Verified",
    AuditLog.Action.INTERNAL_EMAIL_CHANGE_FAILED: "Internal Email Change Failed",
    AuditLog.Action.APPLICATION_OTP_SENT: "Verification Code Sent",
    AuditLog.Action.APPLICATION_OTP_FAILED: "Verification Code Failed",
    AuditLog.Action.APPLICATION_OTP_LOCKED: "Verification Code Locked",
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
    AuditLog.Action.EVIDENCE_ACCESS_DENIED: "File Access Denied",
    AuditLog.Action.EVIDENCE_VAULT_VIEWED: "Secured Files Viewed",
    AuditLog.Action.EXPORT_GENERATED: "Export Created",
    AuditLog.Action.EXPORT_DENIED: "Export Denied",
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


class TurnstileWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        attrs = attrs or {}
        widget_id = attrs.get("id", f"id_{name}")
        site_key = getattr(settings, "TURNSTILE_SITE_KEY", "")
        if not site_key:
            return format_html(
                '<div id="{}" class="alert alert-warning mb-0">'
                "Cloudflare Turnstile is enabled but TURNSTILE_SITE_KEY is not configured."
                "</div>",
                widget_id,
            )
        return format_html(
            '<script>window.rgCaptchaSuccess=function(){{'
            'document.querySelectorAll("[data-captcha-server-error],'
            '[data-captcha-error-summary]").forEach(function(element){{'
            'element.remove();}});}};</script>'
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" '
            "async defer></script>"
            '<div id="{}" class="cf-turnstile" data-sitekey="{}" '
            'data-action="turnstile-spin-v1" '
            'data-callback="rgCaptchaSuccess"></div>',
            widget_id,
            site_key,
        )

    def value_from_datadict(self, data, files, name):
        return data.get("cf-turnstile-response") or data.get(name)


class RecaptchaWidget(forms.Widget):
    def render(self, name, value, attrs=None, renderer=None):
        attrs = attrs or {}
        widget_id = attrs.get("id", f"id_{name}")
        site_key = getattr(settings, "RECAPTCHA_SITE_KEY", "")
        if not site_key:
            return format_html(
                '<div id="{}" class="alert alert-warning mb-0">'
                "Google reCAPTCHA is enabled but RECAPTCHA_SITE_KEY is not configured."
                "</div>",
                widget_id,
            )
        return format_html(
            '<script>window.rgCaptchaSuccess=function(){{'
            'document.querySelectorAll("[data-captcha-server-error],'
            '[data-captcha-error-summary]").forEach(function(element){{'
            'element.remove();}});}};</script>'
            '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'
            '<div id="{}" class="g-recaptcha" data-sitekey="{}" '
            'data-callback="rgCaptchaSuccess"></div>',
            widget_id,
            site_key,
        )

    def value_from_datadict(self, data, files, name):
        return data.get("g-recaptcha-response") or data.get(name)


class CaptchaFormMixin:
    captcha_scope = "default"
    captcha_error_message = "Complete the security check correctly."

    def _make_captcha_field(self):
        if captcha_uses_recaptcha():
            return forms.CharField(
                label="Security check",
                max_length=4096,
                required=True,
                widget=RecaptchaWidget,
                help_text="Google reCAPTCHA protects this form from automated abuse.",
                error_messages={"required": self.captcha_error_message},
            )
        if captcha_uses_turnstile():
            return forms.CharField(
                label="Security check",
                max_length=4096,
                required=True,
                widget=TurnstileWidget,
                help_text="Cloudflare Turnstile protects this form from automated abuse.",
                error_messages={"required": self.captcha_error_message},
            )
        return forms.CharField(
            label="Security check",
            max_length=10,
            required=True,
            help_text="Answer this question before continuing.",
            error_messages={"required": self.captcha_error_message},
        )

    def __init__(self, *args, request=None, **kwargs):
        if request is None and args and hasattr(args[0], "session"):
            request = args[0]
        self.captcha_request = request
        super().__init__(*args, **kwargs)
        if not getattr(settings, "CAPTCHA_ENABLED", True):
            self.fields.pop("captcha_answer", None)
            return
        if "captcha_answer" not in self.fields:
            self.fields["captcha_answer"] = self._make_captcha_field()
        if captcha_uses_turnstile() or captcha_uses_recaptcha():
            self.fields["captcha_answer"].label = "Security check"
            return
        challenge = get_or_create_captcha_challenge(request, self.captcha_scope)
        field = self.fields["captcha_answer"]
        field.label = f"Security check: {challenge}"
        field.widget.attrs.update(
            {
                "autocomplete": "off",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
            }
        )

    def clean_captcha_answer(self):
        answer = self.cleaned_data.get("captcha_answer", "")
        if not validate_captcha_answer(
            self.captcha_request,
            self.captcha_scope,
            answer,
        ):
            raise forms.ValidationError(self.captcha_error_message)
        return answer


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


def _apply_password_strength_widget(field):
    css_class = field.widget.attrs.get("class", "")
    field.widget.attrs.update(
        {
            "class": f"{css_class} rg-password-strength-input".strip(),
            "data-password-strength": "true",
            "autocomplete": field.widget.attrs.get("autocomplete", "new-password"),
        }
    )


def password_reuses_internal_history(user, raw_password):
    if not user or not raw_password:
        return False
    if user.password and user.check_password(raw_password):
        return True
    return any(
        check_password(raw_password, history.password_hash)
        for history in user.password_history.all()[: settings.PASSWORD_HISTORY_LIMIT]
    )


class PasswordReuseValidationMixin:
    reuse_error_message = (
        "Use a password that has not been used recently for this internal account."
    )

    def _apply_password_strength_fields(self):
        for field_name in ("password1", "password2", "new_password1", "new_password2"):
            if field_name in self.fields:
                _apply_password_strength_widget(self.fields[field_name])

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("new_password2")
        if password and password_reuses_internal_history(self.user, password):
            self.add_error("new_password2", self.reuse_error_message)
        return cleaned_data


class InternalAuthenticationForm(CaptchaFormMixin, BootstrapFormMixin, AuthenticationForm):
    captcha_scope = "internal_login"
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


class InternalPasswordChangeForm(PasswordReuseValidationMixin, BootstrapFormMixin, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self._apply_password_strength_fields()


class InternalSetPasswordForm(PasswordReuseValidationMixin, BootstrapFormMixin, SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self._apply_password_strength_fields()


class InternalPasswordResetForm(CaptchaFormMixin, BootstrapFormMixin, PasswordResetForm):
    captcha_scope = "internal_password_reset"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def get_users(self, email):
        users = RecruitmentUser.objects.filter(
            email__iexact=email,
            is_active=True,
            role__in=RecruitmentUser.internal_roles(),
        )
        return (user for user in users if user.has_usable_password())


class InternalMFAOTPForm(BootstrapFormMixin, forms.Form):
    otp = forms.CharField(
        max_length=6,
        min_length=6,
        label="Verification Code",
        widget=forms.TextInput(attrs={
            "autofocus": True,
            "inputmode": "numeric",
            "autocomplete": "one-time-code",
            "pattern": "[0-9]{6}",
            "maxlength": "6",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_otp(self):
        otp = self.cleaned_data["otp"].strip()
        if not otp.isdigit():
            raise forms.ValidationError("Enter the 6-digit verification code sent to your email address.")
        return otp


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
        _apply_password_strength_widget(self.fields["password1"])
        _apply_password_strength_widget(self.fields["password2"])

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if RecruitmentUser.objects.filter(
            email__iexact=email,
            role__in=RecruitmentUser.internal_roles(),
        ).exists():
            raise forms.ValidationError("This email address is already assigned to an internal account.")
        return email


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

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        queryset = RecruitmentUser.objects.filter(
            email__iexact=email,
            role__in=RecruitmentUser.internal_roles(),
        )
        if self.instance and self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise forms.ValidationError("This email address is already assigned to an internal account.")
        return email


class ApplicantPortalIntakeForm(CaptchaFormMixin, BootstrapFormMixin, forms.Form):
    captcha_scope = "applicant_intake"

    first_name = forms.CharField(
        max_length=APPLICANT_NAME_MAX_LENGTH,
        error_messages={"max_length": APPLICANT_NAME_LENGTH_ERROR_MESSAGE},
    )
    last_name = forms.CharField(
        max_length=APPLICANT_NAME_MAX_LENGTH,
        error_messages={"max_length": APPLICANT_NAME_LENGTH_ERROR_MESSAGE},
    )
    email = forms.EmailField()
    phone = forms.CharField(max_length=50)
    qualification_summary = forms.CharField(widget=forms.Textarea(attrs={"rows": 5}))
    cover_letter = forms.CharField(
        required=False,
        max_length=APPLICANT_COVER_LETTER_MAX_LENGTH,
        error_messages={"max_length": APPLICANT_COVER_LETTER_LENGTH_ERROR_MESSAGE},
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    performance_rating_applicability = forms.ChoiceField(
        choices=RecruitmentApplication.PerformanceRatingApplicability.choices,
        widget=forms.RadioSelect,
        label="Performance Rating Availability",
    )
    submission_confirmation = forms.BooleanField(
        required=False,
        label=(
            "I confirm that my information and uploaded documents are true and complete, "
            "and I consent to the use of my submitted data for recruitment processing."
        ),
    )
    checklist_privacy_consent = forms.BooleanField(
        required=False,
        label="I consent to the use of my submitted information for recruitment processing.",
    )
    checklist_documents_complete = forms.BooleanField(required=False)
    checklist_information_certified = forms.BooleanField(
        required=False,
        label="I certify that the submitted information and uploaded documents are true and complete.",
    )

    def __init__(self, *args, entry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry = entry
        self.existing_draft = None
        self.existing_documents_by_code = {}
        self.document_slots = []
        self.duplicate_document_errors = []
        self.duplicate_document_warnings = self.duplicate_document_errors
        self.saved_draft_notice = ""
        self.document_requirements = get_applicant_document_requirements(entry)
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
        # Mobile autofill hints for the applicant's own identity fields (WCAG 1.3.5).
        self.fields["first_name"].widget.attrs["autocomplete"] = "given-name"
        self.fields["last_name"].widget.attrs["autocomplete"] = "family-name"
        self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.fields["phone"].widget.attrs["autocomplete"] = "tel-national"
        self._refresh_document_slots()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def _clean_applicant_name(self, field_name):
        value = unicodedata.normalize("NFC", self.cleaned_data[field_name])
        value = re.sub(r"\s+", " ", value.strip())
        if not _APPLICANT_NAME_ALLOWED_RE.fullmatch(value):
            raise forms.ValidationError(APPLICANT_NAME_ERROR_MESSAGE)
        return value

    def clean_first_name(self):
        return self._clean_applicant_name("first_name")

    def clean_last_name(self):
        return self._clean_applicant_name("last_name")

    def clean_phone(self):
        try:
            return normalize_philippine_mobile_number(self.cleaned_data["phone"])
        except ValueError as exc:
            raise forms.ValidationError(APPLICANT_MOBILE_ERROR_MESSAGE) from exc

    def clean_qualification_summary(self):
        value = self.cleaned_data["qualification_summary"].strip()
        if len(value) > APPLICANT_QUALIFICATION_SUMMARY_MAX_LENGTH:
            raise forms.ValidationError(
                APPLICANT_QUALIFICATION_SUMMARY_LENGTH_ERROR_MESSAGE
            )
        return value

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
            if (
                self._current_performance_rating_applicability()
                == RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ):
                self.existing_documents_by_code.pop(PERFORMANCE_RATING, None)
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

    def _build_duplicate_document_groups(self):
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
        duplicate_groups = []
        for requirement_codes in digest_to_codes.values():
            # Filter to in-scope requirements FIRST, then require >= 2. Guarding
            # on the unfiltered set let a digest shared with a stale/out-of-scope
            # draft document produce a group of length 1 or 0 downstream — i.e.
            # a "used for 0 other documents" message or a phantom "Duplicate file
            # detected" banner with no slot highlighted.
            in_scope_codes = sorted(
                requirement_code
                for requirement_code in requirement_codes
                if requirement_code in self.document_requirements_by_code
            )
            if len(in_scope_codes) < 2:
                continue
            duplicate_groups.append(in_scope_codes)
        return duplicate_groups

    def _build_duplicate_document_warnings(self):
        # One short summary line, no matter how many slots or groups collide.
        # The offending upload slots are already flagged inline (and rendered
        # with rg-pub-upload-slot--error), so enumerating their long, formal
        # titles here only produced a wall of text.
        if self._build_duplicate_document_groups():
            return [
                "The same file is used for more than one document. "
                "Upload a different file for each one."
            ]
        return []

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
                    "You have already submitted an application for this position using this email address.",
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
        self.duplicate_document_errors = self.duplicate_document_warnings
        for requirement_codes in self._build_duplicate_document_groups():
            # Count the *other* slots sharing this file rather than listing their
            # titles — keeps the inline error short (see the applicant screenshot
            # feedback) while the red slots show which ones are affected.
            other_count = len(requirement_codes) - 1
            duplicate_message = (
                f"This file is already used for {other_count} other "
                f"document{'' if other_count == 1 else 's'}. "
                "Upload a different file for each one."
            )
            for requirement_code in requirement_codes:
                requirement = self.document_requirements_by_code[requirement_code]
                self.add_error(requirement.file_field_name, duplicate_message)

        confirmation_checked = bool(cleaned_data.get("submission_confirmation"))
        legacy_confirmation_checked = all(
            bool(cleaned_data.get(field_name))
            for field_name in (
                "checklist_privacy_consent",
                "checklist_documents_complete",
                "checklist_information_certified",
            )
        )
        if not (confirmation_checked or legacy_confirmation_checked):
            self.add_error("submission_confirmation", "Confirm before continuing.")
        cleaned_data["submission_confirmation"] = confirmation_checked or legacy_confirmation_checked
        cleaned_data["checklist_privacy_consent"] = cleaned_data["submission_confirmation"]
        cleaned_data["checklist_documents_complete"] = cleaned_data["submission_confirmation"]
        cleaned_data["checklist_information_certified"] = cleaned_data["submission_confirmation"]
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


class DocumentResubmissionForm(BootstrapFormMixin, forms.Form):
    """Scoped re-upload form for Track Application: one required file per flagged document.

    ``requirements`` is the list of applicant document-requirement objects the reviewer flagged
    for resubmission; only those slots are exposed (already-accepted documents are untouched).
    """

    def __init__(self, *args, requirements=None, remarks_by_code=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.requirements = list(requirements or [])
        remarks_by_code = remarks_by_code or {}
        self.upload_field_names = []
        for requirement in self.requirements:
            field = forms.FileField(
                required=True,
                label=requirement.title,
                help_text="Upload a clear, readable PDF, JPG, or PNG.",
                widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"}),
            )
            # Surfaced above each slot on the resubmission page so the applicant sees
            # exactly why this document was returned (recognition over recall).
            field.resubmission_remarks = (remarks_by_code.get(requirement.code) or "").strip()
            self.fields[requirement.file_field_name] = field
            self.upload_field_names.append((requirement.code, requirement.file_field_name))
        self._apply_bootstrap()

    def get_uploads(self):
        return {
            code: self.cleaned_data.get(field_name)
            for code, field_name in self.upload_field_names
            if self.cleaned_data.get(field_name)
        }


class ApplicantStatusLookupForm(CaptchaFormMixin, BootstrapFormMixin, forms.Form):
    captcha_scope = "applicant_status_lookup"

    tracking_code = forms.CharField(max_length=20, label="Tracking Code")
    email = forms.EmailField(label="Applicant email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_tracking_code(self):
        return self.cleaned_data["tracking_code"].strip().upper()

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
    # REV-F: auditors think in categories, not in ~70 internal event names.
    category = forms.ChoiceField(
        required=False,
        choices=[],
        label="Category",
    )
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
    actor = forms.ModelChoiceField(
        required=False,
        queryset=RecruitmentUser.objects.none(),
        label="User",
        empty_label="All users",
    )
    date_from = forms.DateField(
        required=False,
        label="From",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        label="To",
        widget=forms.DateInput(attrs={"type": "date"}),
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
        self.fields["category"].choices = [
            ("", "All categories"),
            *[(key, group["label"]) for key, group in AUDIT_ACTION_CATEGORIES.items()],
        ]
        self.fields["actor"].queryset = (
            RecruitmentUser.objects.exclude(role=RecruitmentUser.Role.APPLICANT)
            .order_by("last_name", "first_name", "username")
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
        # Plain name of the office that will receive the case.
        if user.role == RecruitmentUser.Role.SECRETARIAT:
            self.target_recipient_label = "HRM Chief"
        elif user.role == RecruitmentUser.Role.HRM_CHIEF:
            self.target_recipient_label = "Secretariat"
        else:
            self.target_recipient_label = "the other office"
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
    # RD-8: the checklist items and the deadline come from the completion
    # record (one source of truth) — the send dialog only adds a message.
    additional_message = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["additional_message"].label = "Additional Instructions"
        self._apply_bootstrap()


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
        # RD-8: the record's deadline is the one source the checklist email and
        # reminders read, so the client-confirmed two-week default lives here
        # (it used to sit on the email dialog, letting the two dates diverge).
        if not self.instance.pk or not self.instance.deadline:
            self.fields["deadline"].initial = timezone.localdate() + timedelta(days=14)
            self.fields["deadline"].help_text = (
                "Defaults to two weeks from today; adjust if the office set a different window."
            )
        self.fields["remarks"].required = False
        # SL-13: moving a deadline is a deliberate act, so it asks for a reason. Required
        # only when the submitted date actually differs from the stored one — resubmitting
        # an unchanged historical deadline is an ordinary edit, not an extension.
        self.fields["deadline_change_reason"].required = False
        self._apply_bootstrap()

    deadline_change_reason = forms.CharField(
        label="Reason for changing the deadline",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=(
            "Required only when you change the deadline. Recorded with the old and new "
            "dates, who changed it, and when."
        ),
    )

    @property
    def stored_deadline(self):
        """The deadline as it stands in the database, not as bound to this form."""
        if not self.instance.pk:
            return None
        return (
            type(self.instance)
            .objects.filter(pk=self.instance.pk)
            .values_list("deadline", flat=True)
            .first()
        )

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        # SL-13: an elapsed deadline is historical evidence of lateness, not invalid data.
        # The old rule rejected it on every later edit, so marking a late-but-valid
        # requirement complete forced staff to move the deadline forward and erase the
        # evidence. The "not earlier than today" rule now applies only to a deadline that
        # is actually being set or changed.
        if deadline and deadline < timezone.localdate() and deadline != self.stored_deadline:
            raise forms.ValidationError(
                "A new completion deadline cannot be earlier than today. An existing "
                "deadline that has already passed can be left as it is."
            )
        return deadline

    def clean(self):
        cleaned = super().clean()
        stored = self.stored_deadline
        submitted = cleaned.get("deadline")
        if self.instance.pk and submitted != stored:
            if not (cleaned.get("deadline_change_reason") or "").strip():
                self.add_error(
                    "deadline_change_reason",
                    "Record why the completion deadline is being changed.",
                )
        return cleaned


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


class CompetencyRatingTemplateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompetencyRatingTemplate
        fields = ["scale_max", "instructions"]
        widgets = {
            "scale_max": forms.NumberInput(attrs={"min": "2", "max": "10"}),
            "instructions": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scale_max"].label = "Score scale (1 to N)"
        self.fields["scale_max"].help_text = (
            "Each competency is scored from 1 up to this number. Default is 4."
        )
        self.fields["instructions"].label = "Instructions for panel members"
        self.fields["instructions"].required = False
        self._apply_bootstrap()

    def clean_scale_max(self):
        scale_max = self.cleaned_data.get("scale_max")
        if scale_max is None or scale_max < 2:
            raise forms.ValidationError("The score scale must go up to at least 2.")
        if scale_max > 10:
            raise forms.ValidationError("Keep the score scale at 10 points or fewer.")
        return scale_max


class CompetencyDefinitionForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompetencyDefinition
        fields = ["group", "name", "weight"]
        widgets = {
            "weight": forms.NumberInput(attrs={"step": "0.01", "min": "0.01"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # All fields optional at the field level so a blank "add" row is simply
        # ignored; the formset validates the rows that actually carry a name.
        self.fields["group"].required = False
        self.fields["group"].choices = [("", "Select group")] + list(
            CompetencyDefinition.Group.choices
        )
        self.fields["group"].label = "Group"
        self.fields["name"].label = "Competency"
        self.fields["name"].required = False
        self.fields["weight"].label = "Weight"
        self.fields["weight"].required = False
        self._apply_bootstrap()


class BaseCompetencyDefinitionFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        active = 0
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            name = (form.cleaned_data.get("name") or "").strip()
            if not name:
                continue
            active += 1
            if not form.cleaned_data.get("group"):
                form.add_error("group", "Pick a group for this competency.")
            weight = form.cleaned_data.get("weight")
            if weight is None or weight <= 0:
                form.add_error("weight", "Weight must be greater than zero.")
        if active == 0:
            raise forms.ValidationError("Keep at least one competency on the rating sheet.")


CompetencyDefinitionFormSet = inlineformset_factory(
    CompetencyRatingTemplate,
    CompetencyDefinition,
    form=CompetencyDefinitionForm,
    formset=BaseCompetencyDefinitionFormSet,
    extra=0,
    can_delete=True,
)


class VacancyAssessmentWeightsForm(BootstrapFormMixin, forms.ModelForm):
    """Per-vacancy assessment weights (exam General/Technical split + CAR ETE/Exam/Interview
    split). Edited per Plantilla vacancy until scoring starts, then the row is locked."""

    class Meta:
        model = VacancyAssessmentWeights
        fields = [
            "ete_weight",
            "exam_weight",
            "interview_weight",
            "exam_general_weight",
            "exam_technical_weight",
        ]
        widgets = {
            "ete_weight": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "exam_weight": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "interview_weight": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "exam_general_weight": forms.NumberInput(
                attrs={"step": "0.01", "min": "0", "max": "100"}
            ),
            "exam_technical_weight": forms.NumberInput(
                attrs={"step": "0.01", "min": "0", "max": "100"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ete_weight"].label = "ETE weight (%)"
        self.fields["ete_weight"].help_text = (
            "Share of the overall CAR score from Education, Training, and Experience."
        )
        self.fields["exam_weight"].label = "Examination weight (%)"
        self.fields["exam_weight"].help_text = "Share of the overall CAR score from the exam."
        self.fields["interview_weight"].label = "Interview weight (%)"
        self.fields["interview_weight"].help_text = (
            "Share of the overall CAR score from the interview."
        )
        self.fields["exam_general_weight"].label = "General Ability weight (%)"
        self.fields["exam_general_weight"].help_text = (
            "Share of the exam score that comes from the General Ability component."
        )
        self.fields["exam_technical_weight"].label = "Technical weight (%)"
        self.fields["exam_technical_weight"].help_text = (
            "Share of the exam score that comes from the Technical component."
        )
        self._apply_bootstrap()


class VacancyAssessmentWeightsUnlockForm(BootstrapFormMixin, forms.Form):
    """SL-00: the reason that accompanies reopening a locked weight row for correction.
    The lock is deliberate, so the only way past it is a stated, audited reason."""

    reason = forms.CharField(
        label="Reason for unlocking",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=1000,
        help_text=(
            "Explain the correction being made — this is recorded in the audit trail "
            "alongside the split that finalized scores were computed against."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Record why the assessment weights are being unlocked.")
        return reason

    def clean(self):
        cleaned = super().clean()
        ete = cleaned.get("ete_weight")
        exam = cleaned.get("exam_weight")
        interview = cleaned.get("interview_weight")
        if None not in (ete, exam, interview) and ete + exam + interview != Decimal("100"):
            raise forms.ValidationError(
                "The ETE, Examination, and Interview weights must add up to 100%."
            )
        general = cleaned.get("exam_general_weight")
        technical = cleaned.get("exam_technical_weight")
        if general is not None and technical is not None:
            if general + technical != Decimal("100"):
                raise forms.ValidationError(
                    "The General Ability and Technical weights must add up to 100%."
                )
        return cleaned


class CaseClosureForm(BootstrapFormMixin, forms.Form):
    closure_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["closure_notes"].label = "Closure Notes"
        self._apply_bootstrap()


class ScreeningReviewForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    DOCUMENT_STATUS_PREFIX = "document_status__"
    DOCUMENT_REMARKS_PREFIX = "document_remarks__"
    COMPLETENESS_BLOCKING_DOCUMENT_STATUSES = {
        ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED,
        ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION,
        ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED,
    }
    # Reviewers only ever pick between two document outcomes: the uploaded
    # file either complies or goes back to the applicant. The remaining
    # statuses are system-applied, never chosen here: NOT_REVIEWED is the
    # undecided default (rendered as the blank placeholder), and
    # NOT_SUBMITTED / NOT_APPLICABLE auto-fill the fixed rows for missing or
    # not-applicable requirements.
    EDITABLE_DOCUMENT_STATUS_CHOICES = (
        ("", "Select status"),
        (
            ScreeningDocumentReview.ReviewStatus.COMPLIED.value,
            ScreeningDocumentReview.ReviewStatus.COMPLIED.label,
        ),
        (
            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION.value,
            ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION.label,
        ),
    )

    class Meta:
        model = ScreeningRecord
        # RD-1: completeness_status is NOT an input. Intake guarantees every
        # required file is uploaded and step 1 determines each file's validity,
        # so completeness is derived from the document review and shown as a
        # computed banner. The model field still persists (CAR/exports/locked
        # views/audit read it) — see derive_completeness_status.
        # RD-3: the ETE score inputs are gone — screening checks papers, the CAR
        # gives grades. The model fields stay (historical records + the CAR read
        # fallback), but new records never populate them; ApplicationETERating
        # on the CAR screen is the only live source.
        fields = [
            "completeness_notes",
            "qualification_outcome",
            "screening_notes",
        ]
        widgets = {
            "completeness_notes": forms.Textarea(attrs={"rows": 3}),
            "screening_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.application = kwargs.pop("application", None)
        self.draft = kwargs.pop("draft", False)
        self.document_review_fields = []
        self._document_review_field_metadata = []
        super().__init__(*args, **kwargs)
        self.fields["completeness_notes"].label = "Completeness Observations"
        self.fields["completeness_notes"].required = False
        self.fields["completeness_notes"].help_text = (
            "Optional. Note anything about the submitted documents worth recording."
        )
        self.fields["qualification_outcome"].label = "Qualification Outcome"
        self.fields["screening_notes"].label = "Screening Notes"
        self._build_document_review_fields()
        self._apply_bootstrap()
        for field_meta in self._document_review_field_metadata:
            field = self.fields[field_meta["field_name"]]
            existing_class = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{existing_class} rg-scr-status-select js-doc-status".strip()
            remarks_field = self.fields[field_meta["remarks_field_name"]]
            existing_remarks_class = remarks_field.widget.attrs.get("class", "")
            remarks_field.widget.attrs["class"] = (
                f"{existing_remarks_class} rg-scr-remarks-field js-doc-remarks"
            ).strip()
        if self.draft:
            # Draft saves persist whatever the reviewer has entered so far; the
            # completeness/qualification requirements are enforced only on finalize.
            for field in self.fields.values():
                field.required = False

    def _existing_document_reviews_by_key(self):
        if not self.instance or not self.instance.pk:
            return {}
        return {
            review.document_key: review
            for review in self.instance.document_reviews.select_related("evidence_item")
        }

    def _document_status_field_name(self, requirement):
        return f"{self.DOCUMENT_STATUS_PREFIX}{requirement.code}"

    def _document_remarks_field_name(self, requirement):
        return f"{self.DOCUMENT_REMARKS_PREFIX}{requirement.code}"

    def _build_document_review_fields(self):
        if self.application is None:
            return

        current_documents = get_current_applicant_document_map(self.application)
        existing_reviews = self._existing_document_reviews_by_key()
        status_labels = dict(ScreeningDocumentReview.ReviewStatus.choices)
        for display_order, requirement in enumerate(
            get_applicant_document_requirements(self.application),
            start=1,
        ):
            evidence = current_documents.get(requirement.code)
            existing_review = existing_reviews.get(requirement.code)
            is_not_applicable = (
                requirement.conditional_on_performance_rating
                and self.application.performance_rating_not_applicable
                and evidence is None
            )
            is_required_for_completeness = requirement.is_required or (
                requirement.conditional_on_performance_rating
                and not self.application.performance_rating_not_applicable
            )

            if is_not_applicable:
                initial_status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
                status_is_fixed = True
            elif evidence is None and is_required_for_completeness:
                initial_status = ScreeningDocumentReview.ReviewStatus.NOT_SUBMITTED
                status_is_fixed = True
            elif evidence is None:
                initial_status = ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
                status_is_fixed = True
            elif existing_review is not None:
                initial_status = existing_review.status
                status_is_fixed = False
            else:
                initial_status = ScreeningDocumentReview.ReviewStatus.NOT_REVIEWED
                status_is_fixed = False

            field_name = self._document_status_field_name(requirement)
            remarks_field_name = self._document_remarks_field_name(requirement)
            widget = forms.HiddenInput if status_is_fixed else forms.Select
            # Fixed rows submit their system-applied value through a hidden
            # input, so they keep the full choice set to validate; the editable
            # dropdown is limited to the two reviewer-selectable outcomes.
            status_choices = (
                ScreeningDocumentReview.ReviewStatus.choices
                if status_is_fixed
                else self.EDITABLE_DOCUMENT_STATUS_CHOICES
            )
            self.fields[field_name] = forms.ChoiceField(
                choices=status_choices,
                required=False,
                initial=initial_status,
                widget=widget(
                    attrs={
                        "data-document-key": requirement.code,
                        "data-required": "true" if is_required_for_completeness else "false",
                    }
                ),
            )
            self.fields[remarks_field_name] = forms.CharField(
                required=False,
                initial=existing_review.remarks if existing_review is not None else "",
                label=f"Remarks for {requirement.title}",
                widget=forms.Textarea(
                    attrs={
                        "rows": 2,
                        "data-document-key": requirement.code,
                        "placeholder": "Add review notes or resubmission instructions.",
                    }
                ),
            )
            metadata = {
                "field_name": field_name,
                "remarks_field_name": remarks_field_name,
                "requirement": requirement,
                "evidence": evidence,
                "is_submitted": evidence is not None,
                "is_not_applicable": is_not_applicable,
                "is_required_for_completeness": is_required_for_completeness,
                "requirement_label": (
                    "Not applicable" if is_not_applicable else requirement.applicant_label
                ),
                "initial_status": initial_status,
                "initial_status_label": status_labels.get(initial_status, initial_status),
                "status_is_fixed": status_is_fixed,
                "display_order": display_order,
            }
            self._document_review_field_metadata.append(metadata)
            self.document_review_fields.append(
                {
                    **metadata,
                    "field": self[field_name],
                    "remarks_field": self[remarks_field_name],
                }
            )

    def _collect_document_reviews(self, cleaned_data):
        status_values = {value for value, _label in ScreeningDocumentReview.ReviewStatus.choices}
        document_reviews = []
        for field_meta in self._document_review_field_metadata:
            field_name = field_meta["field_name"]
            remarks_field_name = field_meta["remarks_field_name"]
            status = cleaned_data.get(field_name) or field_meta["initial_status"]
            remarks = (cleaned_data.get(remarks_field_name) or "").strip()
            cleaned_data[remarks_field_name] = remarks
            if field_meta["status_is_fixed"]:
                status = field_meta["initial_status"]
                cleaned_data[field_name] = status
            if status not in status_values:
                self.add_error(field_name, "Select a valid document review status.")
                continue
            if (
                status == ScreeningDocumentReview.ReviewStatus.NOT_APPLICABLE
                and field_meta["is_required_for_completeness"]
                and not field_meta["is_not_applicable"]
            ):
                self.add_error(field_name, "Required documents cannot be marked not applicable.")
            if (
                status == ScreeningDocumentReview.ReviewStatus.COMPLIED
                and field_meta["evidence"] is None
            ):
                self.add_error(field_name, "A missing document cannot be marked Complied.")
            if (
                status == ScreeningDocumentReview.ReviewStatus.RETURN_FOR_RESUBMISSION
                and not remarks
            ):
                self.add_error(
                    remarks_field_name,
                    "Add the instruction the applicant should follow for this resubmission.",
                )
            document_reviews.append(
                {
                    "document_key": field_meta["requirement"].code,
                    "requirement_title": field_meta["requirement"].title,
                    "requirement_label": field_meta["requirement_label"],
                    "status": status,
                    "remarks": remarks,
                    "is_required": field_meta["is_required_for_completeness"],
                    "is_not_applicable": field_meta["is_not_applicable"],
                    "evidence_item": field_meta["evidence"],
                    "display_order": field_meta["display_order"],
                }
            )
        cleaned_data["document_reviews"] = document_reviews
        return document_reviews

    def clean(self):
        cleaned_data = super().clean()
        document_reviews = self._collect_document_reviews(cleaned_data)
        if self.draft:
            # Ranges/formats are validated above; completeness and cross-field
            # consistency are enforced only on finalize, so partial autosaves and
            # manual "Save draft" of an in-progress form are not rejected.
            return cleaned_data
        qualification_outcome = cleaned_data.get("qualification_outcome")
        screening_notes = (cleaned_data.get("screening_notes") or "").strip()
        blocking_reviews = [
            review
            for review in document_reviews
            if review["is_required"]
            and review["status"] in self.COMPLETENESS_BLOCKING_DOCUMENT_STATUSES
        ]
        # RD-1 asymmetric gate, enforced server-side (never hidden-buttons only):
        # "Qualified" needs every required document Complied — an applicant is
        # never passed on evidence that has not been validated. "Not Qualified"
        # stays available at any time: a valid document can be a definitive
        # disqualifier, and making someone fix paperwork just to be rejected is
        # pointless.
        if (
            qualification_outcome == ScreeningRecord.QualificationOutcome.QUALIFIED
            and blocking_reviews
        ):
            blocking_labels = "; ".join(
                review["requirement_title"] for review in blocking_reviews
            )
            self.add_error(
                "qualification_outcome",
                "Every required document must be marked Complied before this applicant "
                f"can be marked Qualified. Still pending: {blocking_labels}. Return the "
                "flagged documents to the applicant, or record Not Qualified with the basis.",
            )
        if (
            qualification_outcome == ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
            and not screening_notes
        ):
            self.add_error(
                "screening_notes",
                "Record the qualification basis before marking this applicant Not Qualified.",
            )
        return cleaned_data


class ExamRecordForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    SCORE_RANGE_MESSAGE = "Enter a score from 0 to 100."
    SCORE_FIELD_NAMES = ("technical_score", "general_score")

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
            "evidence_file",
        ]
        widgets = {
            "technical_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "general_score": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "100"}),
            "exam_result": forms.HiddenInput(),
            "technical_result": forms.HiddenInput(),
            "general_result": forms.HiddenInput(),
            "exam_date": forms.DateInput(attrs={"type": "date"}),
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "exam_notes": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.application = kwargs.pop("application", None)
        self.draft = kwargs.pop("draft", False)
        super().__init__(*args, **kwargs)
        self.component_section_label = "General Ability and Technical Components"
        # Per-vacancy exam weights drive both the help copy and the live overall preview.
        # Defaults (60/40) apply when there is no application or no weights row yet.
        self.exam_general_weight_label = "60"
        self.exam_technical_weight_label = "40"
        # RD-2: every branch scores General + Technical against the vacancy's own
        # weights (COS included — assumption pending client confirmation, which a
        # weights change would satisfy without touching this code).
        if self.application:
            exam_weights = self.application.position.assessment_weights_or_default
            self.exam_general_weight_label = format_weight_percentage(exam_weights.exam_general_weight)
            self.exam_technical_weight_label = format_weight_percentage(exam_weights.exam_technical_weight)
        self.component_weight_display = (
            f"General Ability ({self.exam_general_weight_label}%) and "
            f"Technical ({self.exam_technical_weight_label}%) scores are recorded separately."
        )
        self.exam_type_is_fixed = False
        self.exam_type_fixed_label = ""
        self.administered_by_is_fixed = False
        self.administered_by_fixed_label = ""
        self.fields["exam_type"].label = "Exam Type"
        self.fields["exam_status"].label = "Exam Status"
        self.fields["exam_result"].label = "Overall Result"
        self.fields["technical_score"].label = "Technical Score"
        self.fields["technical_result"].label = "Technical Result"
        self.fields["general_score"].label = "General Ability Score"
        self.fields["general_result"].label = "General Ability Result"
        self.fields["exam_date"].label = "Exam Date"
        self.fields["administered_by"].label = "Administered By"
        self.fields["valid_from"].label = "Validity Start"
        self.fields["valid_until"].label = "Validity End"
        self.fields["exam_notes"].label = "Exam Notes / Remarks"
        self.fields["exam_notes"].required = False
        self.fields["exam_result"].required = False
        self.fields["technical_score"].required = False
        self.fields["technical_result"].required = False
        self.fields["general_score"].required = False
        self.fields["general_result"].required = False
        self.fields["exam_date"].required = False
        self.fields["administered_by"].required = False
        self.fields["valid_from"].required = False
        self.fields["valid_until"].required = False
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
        if self.application:
            self.fields["technical_score"].label = "Technical Score"
            self.fields["general_score"].label = "General Ability Score"
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
        if self.draft:
            for field in self.fields.values():
                field.required = False
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
            return ("technical_score", "general_score")
        if exam_type == ExamRecord.ExamType.END_USER_ASSESSMENT:
            return ("general_score",)
        return ()

    def clean(self):
        cleaned_data = super().clean()
        for field_name in self.SCORE_FIELD_NAMES:
            value = cleaned_data.get(field_name)
            if value is not None and (value < 0 or value > 100):
                self.add_error(field_name, self.SCORE_RANGE_MESSAGE)
        valid_from = cleaned_data.get("valid_from")
        valid_until = cleaned_data.get("valid_until")
        if valid_from and valid_until and valid_until < valid_from:
            self.add_error("valid_until", "Validity end date cannot be earlier than the validity start date.")

        exam_status = cleaned_data.get("exam_status")
        score_values = {
            field_name: cleaned_data.get(field_name)
            for field_name in self.SCORE_FIELD_NAMES
        }
        if exam_status == ExamRecord.ExamStatus.COMPLETED:
            if not self.draft and not cleaned_data.get("exam_date"):
                self.add_error("exam_date", "Provide the date the examination was administered.")
            if not self.draft and not cleaned_data.get("administered_by"):
                self.add_error("administered_by", "Select who administered the examination.")
            if not self.draft:
                for field_name in self._required_score_fields_for_type(cleaned_data.get("exam_type")):
                    if score_values[field_name] is None and not self.has_error(field_name):
                        self.add_error(field_name, "Enter this required exam score.")
        elif exam_status in {ExamRecord.ExamStatus.WAIVED, ExamRecord.ExamStatus.ABSENT}:
            for field_name in self.SCORE_FIELD_NAMES:
                cleaned_data[field_name] = None
            cleaned_data["valid_from"] = None
            cleaned_data["valid_until"] = None
            if not self.draft and not cleaned_data.get("exam_notes"):
                self.add_error("exam_notes", "Provide remarks explaining the waiver or absence.")
        return cleaned_data

    def clean_evidence_file(self):
        uploaded_file = self.cleaned_data.get("evidence_file")
        if uploaded_file:
            try:
                validate_applicant_document_upload(uploaded_file)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return uploaded_file


class ExamScheduleForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    scheduled_for = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = ExamSchedule
        fields = [
            "scheduled_for",
            "venue",
            "instructions",
            "notice_delivery",
        ]
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scheduled_for"].label = "Exam Schedule"
        # Gray out past dates in the calendar picker (courtesy only — the
        # server-side past-date rule in clean_scheduled_for stays authoritative).
        self.fields["scheduled_for"].widget.attrs["min"] = timezone.localtime().strftime(
            "%Y-%m-%dT00:00"
        )
        self.fields["venue"].label = "Exam Venue"
        self.fields["instructions"].label = "Instructions for the Applicant"
        self.fields["instructions"].required = False
        self.fields["notice_delivery"].label = "How the applicant will be notified"
        self._apply_bootstrap()

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data.get("scheduled_for")
        if scheduled_for is None:
            return scheduled_for
        cutoff = timezone.now() - timedelta(minutes=5)
        if scheduled_for >= cutoff:
            return scheduled_for
        if (
            self.instance
            and self.instance.pk
            and self.instance.scheduled_for == scheduled_for
            and self.instance.scheduled_for < cutoff
        ):
            return scheduled_for
        raise forms.ValidationError("The exam can't be scheduled in the past.")


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
        # Gray out past dates in the calendar picker (courtesy only — the
        # server-side past-date rule in clean_scheduled_for stays authoritative).
        self.fields["scheduled_for"].widget.attrs["min"] = timezone.localtime().strftime(
            "%Y-%m-%dT00:00"
        )
        self.fields["location"].label = "Interview Location / Medium"
        self.fields["session_notes"].label = "Session Notes"
        self.fields["session_notes"].required = False
        self._apply_bootstrap()

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data.get("scheduled_for")
        if scheduled_for is None:
            return scheduled_for
        cutoff = timezone.now() - timedelta(minutes=5)
        if scheduled_for >= cutoff:
            return scheduled_for
        if (
            self.instance
            and self.instance.pk
            and self.instance.scheduled_for == scheduled_for
            and self.instance.scheduled_for < cutoff
        ):
            return scheduled_for
        raise forms.ValidationError("The interview can't be scheduled in the past.")


class InterviewRatingForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = InterviewRating
        # rating_score is no longer hand-entered — it is computed from the
        # per-competency scores below.
        fields = [
            "rated_by",
            "rating_notes",
            "justification",
        ]
        widgets = {
            "rated_by": forms.Select(),
            "rating_notes": forms.Textarea(attrs={"rows": 3}),
            "justification": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, application=None, actor=None, template=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application
        self.actor = actor
        self.template = template
        self.competencies = list(template.competencies.all()) if template else []
        self.is_support_encoding = self._is_support_encoding()
        self.fields["rated_by"].label = "Actual HRMPSB Rater"
        self.fields["rated_by"].queryset = self._rater_queryset()
        self.fields["rating_notes"].label = "Rating Notes"
        self.fields["justification"].label = "Justification"
        self.fields["rating_notes"].required = False
        self.fields["justification"].required = False
        if self.is_support_encoding:
            self.fields["rated_by"].required = True
            self.fields["rated_by"].help_text = (
                "Select the HRMPSB member who actually gave the paper-based rating."
            )
        else:
            self.fields["rated_by"].required = False
            self.fields["rated_by"].widget = forms.HiddenInput()
            if self.actor is not None:
                self.fields["rated_by"].initial = self.actor.pk
        self._build_competency_fields()
        self._apply_bootstrap()

    @staticmethod
    def score_field_name(competency):
        return f"score_{competency.id}"

    def _build_competency_fields(self):
        existing = {}
        if self.instance and self.instance.pk:
            existing = {
                score.competency_id: score.score
                for score in self.instance.competency_scores.all()
            }
        for competency in self.competencies:
            field = forms.IntegerField(
                label=competency.name,
                min_value=self.template.scale_min,
                max_value=self.template.scale_max,
                widget=forms.NumberInput(
                    attrs={
                        "min": self.template.scale_min,
                        "max": self.template.scale_max,
                        "step": "1",
                        "class": "rg-competency-score-input",
                        "data-weight": str(competency.weight),
                    }
                ),
            )
            field.initial = existing.get(competency.id)
            self.fields[self.score_field_name(competency)] = field

    def grouped_competency_fields(self):
        """Yield ``(group_label, [bound_field, ...])`` so the template can render the
        score grid under its Core / Organizational / Technical headings."""
        groups = []
        for group_value, group_label in CompetencyDefinition.Group.choices:
            bound = [
                self[self.score_field_name(competency)]
                for competency in self.competencies
                if competency.group == group_value
            ]
            if bound:
                groups.append((group_label, bound))
        return groups

    def _is_support_encoding(self):
        if self.application is None or self.actor is None:
            return False
        case = getattr(self.application, "case", None)
        return (
            self.application.branch == PositionPosting.Branch.PLANTILLA
            and case is not None
            and case.current_stage == "hrmpsb_review"
            and self.actor.role in {
                RecruitmentUser.Role.SECRETARIAT,
                RecruitmentUser.Role.HRM_CHIEF,
            }
        )

    def _rater_queryset(self):
        queryset = RecruitmentUser.objects.filter(is_active=True).order_by(
            "last_name",
            "first_name",
            "username",
        )
        if self.is_support_encoding:
            return queryset.filter(role=RecruitmentUser.Role.HRMPSB_MEMBER)
        if self.actor is not None:
            return queryset.filter(pk=self.actor.pk)
        return queryset.none()

    def clean_rated_by(self):
        rated_by = self.cleaned_data.get("rated_by")
        if self.is_support_encoding:
            if rated_by is None:
                raise forms.ValidationError("Select the HRMPSB member who gave the paper rating.")
            if rated_by.role != RecruitmentUser.Role.HRMPSB_MEMBER:
                raise forms.ValidationError("The actual rater must be an HRMPSB member.")
            return rated_by
        return self.actor or rated_by

    def clean(self):
        cleaned = super().clean()
        if not self.template:
            return cleaned
        scores = {}
        for competency in self.competencies:
            value = cleaned.get(self.score_field_name(competency))
            if value is None:
                # The per-field IntegerField already flags the missing/out-of-range
                # value; just don't compute a partial score.
                continue
            scores[competency] = value
        cleaned["competency_scores"] = scores
        if len(scores) == len(self.competencies) and self.competencies:
            rating_value = compute_competency_rating_score(self.template, scores)
            cleaned["computed_rating_score"] = rating_value
            justification = (cleaned.get("justification") or "").strip()
            if (
                rating_value < Decimal("75") or rating_value > Decimal("98")
            ) and not justification:
                self.add_error(
                    "justification",
                    "Provide a justification when the computed rating is below 75 or above 98.",
                )
        return cleaned


class SimpleInterviewRatingForm(BootstrapFormMixin, forms.Form):
    """COS interview rating: a single end-user / HRMS score (0-100) plus notes,
    instead of the competency rating sheet (spec §6.2). A justification is required
    when the score lands below 75 or above 98 (mirrors the competency path)."""

    rating_score = forms.DecimalField(
        label="Interview Score (0-100)",
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        widget=forms.NumberInput(attrs={"min": 0, "max": 100, "step": "0.01"}),
    )
    rating_notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    justification = forms.CharField(
        label="Justification",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean(self):
        cleaned = super().clean()
        score = cleaned.get("rating_score")
        justification = (cleaned.get("justification") or "").strip()
        if (
            score is not None
            and (score < Decimal("75") or score > Decimal("98"))
            and not justification
        ):
            self.add_error(
                "justification",
                "Provide a justification when the interview score is below 75 or above 98.",
            )
        return cleaned


class InterviewAbsenceForm(BootstrapFormMixin, forms.Form):
    """SL-06: record that the candidate did not attend. No score field, deliberately."""

    notes = forms.CharField(
        label="What happened",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Recorded with the absence — no score is written for a no-show.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_notes(self):
        notes = (self.cleaned_data.get("notes") or "").strip()
        if not notes:
            raise forms.ValidationError("Record what happened at the scheduled interview.")
        return notes


class RecruitmentEntryCancellationForm(BootstrapFormMixin, forms.Form):
    """SL-11: cancelling a published vacancy closes live cases and emails their applicants,
    so it demands a stated reason rather than being another one-click status toggle."""

    reason = forms.CharField(
        label="Reason for cancelling this vacancy",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Recorded on the vacancy, on every affected case, and in the notice each "
            "applicant receives."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Record why this vacancy is being cancelled.")
        return reason


class ApplicationWithdrawalForm(BootstrapFormMixin, forms.Form):
    """SL-10: record an applicant's withdrawal, with their request attached as evidence."""

    reason = forms.CharField(
        label="Reason and reference",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "What the applicant asked for and how it reached the office — e.g. the date "
            "and sender of the email."
        ),
    )
    file = forms.FileField(
        label="The applicant's written request",
        required=False,
        help_text="Attach the email or letter. Strongly recommended for the record.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError(
                "Record the reason and reference for this withdrawal."
            )
        return reason

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if uploaded_file:
            try:
                validate_applicant_document_upload(uploaded_file)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return uploaded_file


class AttendanceLetterOfIntentForm(BootstrapFormMixin, forms.Form):
    """SL-06: file the applicant's letter of intent against a recorded absence."""

    file = forms.FileField(label="Letter of intent")
    notes = forms.CharField(
        label="Notes",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if uploaded_file:
            try:
                validate_applicant_document_upload(uploaded_file)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return uploaded_file


class AttendanceAbsenceDecisionForm(BootstrapFormMixin, forms.Form):
    """SL-06: the office's decision on a recorded absence — reschedule, or terminal.

    Both need a reason. Nothing here happens on a timer; a terminal outcome is always a
    human decision."""

    outcome = forms.ChoiceField(
        label="Decision",
        choices=(
            (AttendanceException.Outcome.RESCHEDULED, "Reschedule the candidate"),
            (AttendanceException.Outcome.TERMINAL, "The application does not proceed"),
        ),
        widget=forms.RadioSelect,
    )
    reason = forms.CharField(
        label="Reason",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Record why this absence is being decided this way.")
        return reason


class InterviewFallbackUploadForm(BootstrapFormMixin, forms.Form):
    """SL-03: encoding a paper rating sheet asks the same three things the online form
    does — whose rating, what score, and why if it falls outside 75–98 — plus the scan.
    A sheet with no member and no number is a document, not a rating, and the quorum rule
    cannot count it."""

    rated_by = forms.ModelChoiceField(
        label="Panel member who signed the sheet",
        queryset=RecruitmentUser.objects.none(),
        empty_label="— Select the HRMPSB member —",
    )
    rating_score = forms.DecimalField(
        label="Rating from the sheet",
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
    )
    file = forms.FileField(label="Scanned Fallback Rating Sheet")
    justification = forms.CharField(
        label="Justification",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Required when the rating is below 75 or above 98.",
    )
    remarks = forms.CharField(
        label="Upload Remarks",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, panel_members=None, **kwargs):
        super().__init__(*args, **kwargs)
        if panel_members is not None:
            self.fields["rated_by"].queryset = panel_members
        self._apply_bootstrap()

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if uploaded_file:
            try:
                validate_applicant_document_upload(uploaded_file)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return uploaded_file

    def clean(self):
        cleaned = super().clean()
        score = cleaned.get("rating_score")
        justification = (cleaned.get("justification") or "").strip()
        if (
            score is not None
            and (score < Decimal("75") or score > Decimal("98"))
            and not justification
        ):
            self.add_error(
                "justification",
                "Provide a justification when the interview rating is below 75 or above 98.",
            )
        return cleaned


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
            "recommendation",
            "decision_support_summary",
            "quorum_status",
            "attendance_notes",
            "ranking_position",
            "ranking_notes",
        ]
        widgets = {
            "deliberation_minutes": forms.Textarea(attrs={"rows": 4}),
            "recommendation": forms.Textarea(attrs={"rows": 3}),
            "decision_support_summary": forms.Textarea(attrs={"rows": 4}),
            "attendance_notes": forms.Textarea(attrs={"rows": 3}),
            "ranking_position": forms.NumberInput(attrs={"min": "1", "step": "1"}),
            "ranking_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.application = kwargs.pop("application", None)
        draft = kwargs.pop("draft", False)
        super().__init__(*args, **kwargs)
        self.fields["deliberated_at"].label = "Deliberation Date and Time"
        self.fields["deliberation_minutes"].label = "Deliberation Minutes / Record"
        self.fields["recommendation"].label = "HRMPSB Recommendation"
        self.fields["decision_support_summary"].label = "Decision-Support Summary"
        self.fields["quorum_status"].label = "Quorum Status"
        self.fields["attendance_notes"].label = "Attendance Notes"
        self.fields["ranking_position"].label = "Ranking Position"
        self.fields["ranking_notes"].label = "Ranking Notes / Justification"
        self.fields["ranking_position"].required = False
        self.fields["ranking_notes"].required = False
        self.fields["quorum_status"].required = False
        self.fields["attendance_notes"].required = False
        self.fields["ranking_notes"].help_text = (
            "Record concerns or justification when the HRMPSB rank differs from the system's preliminary score order."
        )
        if draft:
            for field in self.fields.values():
                field.required = False
        self._apply_bootstrap()

    def _is_plantilla_hrmpsb(self):
        if self.application is not None:
            return (
                self.application.branch == PositionPosting.Branch.PLANTILLA
                and getattr(getattr(self.application, "case", None), "current_stage", "")
                == RecruitmentCase.Stage.HRMPSB_REVIEW
            )
        return (
            self.instance
            and self.instance.branch == PositionPosting.Branch.PLANTILLA
            and self.instance.review_stage == RecruitmentCase.Stage.HRMPSB_REVIEW
        )


class ComparativeAssessmentReportForm(DeferredModelValidationMixin, BootstrapFormMixin, forms.ModelForm):
    quorum_met = forms.ChoiceField(
        choices=[("", "—"), ("yes", "Yes"), ("no", "No")],
        required=False,
    )

    class Meta:
        model = ComparativeAssessmentReport
        fields = [
            "summary_notes",
            "members_present",
            "recommendation_notes",
        ]
        widgets = {
            "summary_notes": forms.Textarea(attrs={"rows": 4}),
            "members_present": forms.NumberInput(attrs={"min": "3", "max": "5", "step": "1"}),
            "recommendation_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["summary_notes"].label = "CAR Summary Notes"
        self.fields["summary_notes"].required = False
        self.fields["members_present"].label = "HRMPSB members present"
        self.fields["members_present"].required = False
        self.fields["recommendation_notes"].label = "Recommendation Notes / Remarks"
        self.fields["recommendation_notes"].required = False
        self.fields["quorum_met"].label = "Quorum met"
        self._apply_bootstrap()

    def clean_quorum_met(self):
        value = self.cleaned_data.get("quorum_met")
        if value == "yes":
            return True
        if value == "no":
            return False
        return None


class CARItemChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, item):
        application = item.application
        reference = application.reference_number or application.reference_label
        return f"#{item.rank_order} - {application.applicant_display_name} ({reference})"


class FinalSelectionForm(BootstrapFormMixin, forms.Form):
    selected_item = CARItemChoiceField(
        queryset=ComparativeAssessmentReportItem.objects.none(),
        widget=forms.RadioSelect,
    )
    is_deep_selection = forms.BooleanField(required=False)
    deep_selection_justification = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    decision_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, report=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report
        if report is not None:
            self.fields["selected_item"].queryset = report.items.select_related(
                "recruitment_case",
                "recruitment_case__application",
            ).order_by("rank_order", "created_at")
        self.fields["selected_item"].label = "Selected Appointee"
        self.fields["is_deep_selection"].label = "Deep selection"
        self.fields["deep_selection_justification"].label = "Deep-selection Justification"
        self.fields["decision_notes"].label = "Decision Notes / Remarks"
        self.fields["is_deep_selection"].help_text = (
            "Required when selecting an applicant ranked outside the top five."
        )
        self.fields["deep_selection_justification"].help_text = (
            "Explain the superior qualifications and selection basis."
        )
        self._apply_bootstrap()

    def clean_selected_item(self):
        selected_item = self.cleaned_data["selected_item"]
        if self.report is not None and selected_item.report_id != self.report.id:
            raise forms.ValidationError("Select an applicant from the finalized CAR.")
        return selected_item

    def _recommended_case_ids(self):
        """The board's recommended case, falling back to the CAR's top computed rank when
        no recommendation was recorded (legacy reports). SL-08: a tie can put more than one
        candidate at rank 1, and picking any of them is not a deviation."""
        if self.report is None:
            return set()
        if self.report.recommended_case_id:
            return {self.report.recommended_case_id}
        return set(
            self.report.items.filter(rank_order=1)
            .values_list("recruitment_case_id", flat=True)
        )

    def clean(self):
        cleaned_data = super().clean()
        selected_item = cleaned_data.get("selected_item")
        is_deep_selection = cleaned_data.get("is_deep_selection")
        justification = (cleaned_data.get("deep_selection_justification") or "").strip()
        # SL-08: a tied band shares one ordinal, so a tie straddling fifth place leaves
        # every member of it inside the top five.
        selected_rank = getattr(selected_item, "rank_order", None) if selected_item else None
        if (
            selected_item
            and (selected_rank is None or selected_rank > TOP_FIVE_SELECTION_LIMIT)
            and not is_deep_selection
        ):
            self.add_error(
                "is_deep_selection",
                "Selecting outside the top five requires deep selection documentation.",
            )
        if is_deep_selection and not justification:
            self.add_error(
                "deep_selection_justification",
                "Record the deep-selection justification before finalizing this selection.",
            )
        # WF-03: a pick that deviates from the board's recommendation needs a justification.
        recommended_case_ids = self._recommended_case_ids()
        if (
            selected_item
            and recommended_case_ids
            and selected_item.recruitment_case_id not in recommended_case_ids
            and not justification
        ):
            self.add_error(
                "deep_selection_justification",
                "You are selecting someone other than the board's recommended candidate. "
                "Record the justification for the official record.",
            )
        return cleaned_data


class PositionDocumentRequirementsForm(forms.Form):
    """Per-vacancy checklist: which applicant documents apply and whether each is required.

    Renders one row per branch catalog document. The minimum required documents and any
    posting that is already live are rendered as disabled fields, so Django ignores posted
    values and cleans them to their initial state.
    """

    APPLIES_PREFIX = "doc_applies__"
    LEVEL_PREFIX = "doc_level__"
    LEVEL_CHOICES = [("required", "Required"), ("optional", "Optional")]

    def __init__(self, *args, branch=None, posting=None, locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.branch = branch or PositionPosting.Branch.PLANTILLA
        self.posting = posting
        self.locked = locked
        catalog = APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH.get(
            self.branch, PLANTILLA_APPLICANT_DOCUMENT_REQUIREMENTS
        )
        existing = {}
        if posting is not None and posting.pk:
            existing = {row.document_code: row for row in posting.document_requirements.all()}
        self.rows = []
        for requirement in catalog:
            code = requirement.code
            forced_min = code in MIN_REQUIRED_DOCUMENT_CODES
            existing_row = existing.get(code)
            if forced_min:
                applies_initial = True
            elif existing:
                applies_initial = existing_row is not None
            else:
                # No saved configuration yet (new entry or legacy posting): offer the full
                # branch standard set, matching the current hardcoded behaviour.
                applies_initial = True
            applies_name = self.APPLIES_PREFIX + code
            self.fields[applies_name] = forms.BooleanField(
                required=False,
                initial=applies_initial,
                disabled=forced_min or locked,
                label=requirement.title,
            )
            row = {
                "requirement": requirement,
                "forced_min": forced_min,
                "is_conditional": requirement.conditional_on_performance_rating,
                "applies_field": self[applies_name],
                "level_field": None,
            }
            if not requirement.conditional_on_performance_rating:
                level_required = (
                    existing_row.is_required if existing_row is not None else requirement.is_required
                )
                level_name = self.LEVEL_PREFIX + code
                self.fields[level_name] = forms.ChoiceField(
                    choices=self.LEVEL_CHOICES,
                    required=False,
                    initial="required" if (forced_min or level_required) else "optional",
                    widget=forms.RadioSelect,
                    disabled=forced_min or locked,
                )
                row["level_field"] = self[level_name]
            self.rows.append(row)

    def get_selections(self):
        selections = []
        for row in self.rows:
            code = row["requirement"].code
            applies = row["forced_min"] or bool(self.cleaned_data.get(self.APPLIES_PREFIX + code))
            level_value = self.cleaned_data.get(self.LEVEL_PREFIX + code)
            selections.append(
                {
                    "code": code,
                    "applies": applies,
                    "is_required": level_value == "required",
                }
            )
        return selections


class RecruitmentEntryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = PositionPosting
        fields = [
            "position_reference",
            "job_code",
            "item_number",
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
        self.fields["item_number"].label = "Plantilla Item Number"
        self.fields["item_number"].required = False
        self.fields["item_number"].help_text = (
            "Required for Plantilla — the official plantilla item number for this vacancy. "
            "Leave blank for COS (item numbers apply to Plantilla items only)."
        )
        self.fields["branch"].label = "Engagement Type"
        self.fields["publication_date"].help_text = (
            "For Plantilla, this starts the publication period when recorded."
        )
        self.fields["opening_date"].help_text = (
            "Used as the publication start when no separate publication date is recorded."
        )
        self.fields["closing_date"].help_text = (
            "For Plantilla, set the last day applications are accepted. "
            "Leave blank to default to a 14-day publication period."
        )
        # REV-A: gray out past dates in the closing-date picker. Client-side
        # only — the real guard is in clean(). Publication/opening stay
        # flexible (recording an already-started paper publication is
        # legitimate); a soft JS confirmation covers those.
        self.fields["closing_date"].widget.attrs["min"] = timezone.localdate().isoformat()
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
        publication_date = cleaned_data.get("publication_date")
        opening_date = cleaned_data.get("opening_date")
        closing_date = cleaned_data.get("closing_date")
        submitted_job_code = (self.data.get(self.add_prefix("job_code")) or "").strip().upper()
        existing_job_code = (self.instance.job_code or "").strip().upper()

        if submitted_job_code and submitted_job_code != existing_job_code:
            self.add_error(
                "job_code",
                "Entry Code is generated automatically for tracking and cannot be edited.",
            )

        item_number = (cleaned_data.get("item_number") or "").strip()
        if branch == PositionPosting.Branch.PLANTILLA and not item_number:
            self.add_error("item_number", "Enter the plantilla item number for this vacancy.")
        elif branch == PositionPosting.Branch.COS:
            cleaned_data["item_number"] = ""

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

        if branch == PositionPosting.Branch.PLANTILLA and not closing_date:
            # Default to a 14-day publication window when the closing date is left
            # blank. A closing date entered by the user is accepted as-is.
            default_closing_date = PositionPosting.calculate_plantilla_closing_date(
                publication_date or opening_date
            )
            if default_closing_date:
                cleaned_data["closing_date"] = default_closing_date
                self.instance.closing_date = default_closing_date

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

        # REV-A: a NEW closing-date value cannot be in the past. On create this
        # blocks born-dead postings; on edit it means extensions always land on
        # today or later. An existing (unchanged) past value is legitimate —
        # that's a lapsed deadline waiting to be extended — so only a *changed*
        # value is checked.
        effective_closing_date = cleaned_data.get("closing_date") or closing_date
        if effective_closing_date:
            original_closing_date = None
            if self.instance.pk:
                original_closing_date = (
                    PositionPosting.objects.filter(pk=self.instance.pk)
                    .values_list("closing_date", flat=True)
                    .first()
                )
            if (
                effective_closing_date != original_closing_date
                and effective_closing_date < timezone.localdate()
            ):
                self.add_error(
                    "closing_date",
                    "The closing date cannot be set to a past date. To close a "
                    "posting, set its status to Closed instead.",
                )
            # RD-7: a manual Close is a deliberate stop — extending the deadline
            # must not silently undo it (is_open_for_intake requires ACTIVE, so
            # the public page would show "Deadline extended" while intake stays
            # shut: extended-but-closed, a silent contradiction). Reopen first.
            if (
                self.instance.pk
                and effective_closing_date != original_closing_date
                and PositionPosting.objects.filter(pk=self.instance.pk)
                .values_list("status", flat=True)
                .first()
                == PositionPosting.EntryStatus.CLOSED
            ):
                self.add_error(
                    "closing_date",
                    "This vacancy is closed. Reopen it (set it Active) before "
                    "extending the deadline.",
                )

        return cleaned_data
