from django.conf import settings
from django.contrib import messages
from django.db import OperationalError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from .forms import ApplicantOTPForm, ApplicantPortalIntakeForm, ApplicantStatusLookupForm
from .models import PositionPosting, RecruitmentApplication
from .requirements import get_applicant_document_requirements
from .services import (
    create_public_application_draft,
    get_public_recruitment_entries,
    issue_application_otp,
    save_public_application_draft_progress,
    submit_application,
    verify_application_otp,
)


_APPLICANT_STATUS_LABELS = {
    RecruitmentApplication.Status.DRAFT: None,  # drafts not shown
    RecruitmentApplication.Status.SECRETARIAT_REVIEW: (
        "Under Review",
        "review",
        "Your application is currently being reviewed by our recruitment team. "
        "You will be contacted if additional information is needed.",
    ),
    RecruitmentApplication.Status.HRM_CHIEF_REVIEW: (
        "Under Review",
        "review",
        "Your application is currently being reviewed by our recruitment team.",
    ),
    RecruitmentApplication.Status.HRMPSB_REVIEW: (
        "Under Evaluation",
        "evaluation",
        "Your application is currently being evaluated. "
        "You may be contacted for an examination or interview schedule.",
    ),
    RecruitmentApplication.Status.APPOINTING_AUTHORITY_REVIEW: (
        "Under Processing",
        "review",
        "Your application is being processed for the next steps. "
        "You will be contacted with further instructions.",
    ),
    RecruitmentApplication.Status.RETURNED_TO_APPLICANT: (
        "Action Required",
        "returned",
        "Your application has been returned. Please check your email for instructions on what to do next.",
    ),
    RecruitmentApplication.Status.APPROVED: (
        "Approved",
        "approved",
        "Congratulations! Your application has been approved. "
        "You will be contacted with further instructions.",
    ),
    RecruitmentApplication.Status.REJECTED: (
        "Not Selected",
        "not-selected",
        "Thank you for your interest. Unfortunately, your application was not selected for this position. "
        "You are welcome to apply for other open positions.",
    ),
    RecruitmentApplication.Status.WITHDRAWN: (
        "Withdrawn",
        "not-selected",
        "This application has been withdrawn.",
    ),
}


def _posting_is_closing_soon(posting):
    """Return True if the posting closes within 7 days."""
    if posting.closing_date:
        delta = posting.closing_date - timezone.localdate()
        return 0 <= delta.days <= 7
    return False


class ApplicantPortalView(TemplateView):
    template_name = "recruitment/applicant_portal.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        plantilla = get_public_recruitment_entries(PositionPosting.Branch.PLANTILLA)
        cos = get_public_recruitment_entries(PositionPosting.Branch.COS)
        # Annotate each posting with is_closing_soon for template use
        for entry in list(plantilla) + list(cos):
            entry.is_closing_soon = _posting_is_closing_soon(entry)
        context["plantilla_entries"] = plantilla
        context["cos_entries"] = cos
        return context


class ApplicantVacancyDetailView(TemplateView):
    template_name = "recruitment/applicant_vacancy_detail.html"

    def dispatch(self, request, *args, **kwargs):
        self.entry = get_object_or_404(
            PositionPosting.objects.select_related("position_reference"),
            pk=kwargs["pk"],
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entry"] = self.entry
        context["document_requirements"] = get_applicant_document_requirements(self.entry.branch)
        context["is_closing_soon"] = _posting_is_closing_soon(self.entry)
        context["can_apply"] = self.entry.is_open_for_intake
        return context


class ApplicantHelpView(TemplateView):
    template_name = "recruitment/applicant_help.html"


class ApplicantPortalIntakeView(FormView):
    template_name = "recruitment/applicant_intake_form.html"
    form_class = ApplicantPortalIntakeForm

    def dispatch(self, request, *args, **kwargs):
        self.entry = get_object_or_404(
            PositionPosting.objects.select_related("position_reference"),
            pk=kwargs["pk"],
        )
        if not self.entry.is_open_for_intake:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["entry"] = self.entry
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entry"] = self.entry
        max_bytes = getattr(settings, "MAX_EVIDENCE_UPLOAD_BYTES", 5 * 1024 * 1024)
        context["max_upload_mb"] = max_bytes // (1024 * 1024)
        return context

    def form_valid(self, form):
        try:
            application = create_public_application_draft(
                entry=self.entry,
                cleaned_data=form.cleaned_data,
                requirement_uploads=form.get_requirement_uploads(),
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        except OperationalError:
            form.add_error(
                None,
                "We could not save your application draft right now. Please try again in a few moments.",
            )
            return self.form_invalid(form)

        messages.success(
            self.request,
            "Your application draft is ready. Check your email for the verification code.",
        )
        for warning in form.duplicate_document_warnings:
            messages.warning(self.request, warning)
        return redirect("applicant-otp", token=application.public_token)

    def form_invalid(self, form):
        valid_requirement_uploads = form.get_valid_requirement_uploads()
        if (
            valid_requirement_uploads
            and form.can_persist_draft_uploads()
            and "email" not in form.errors
            and not form.non_field_errors()
        ):
            try:
                draft = save_public_application_draft_progress(
                    entry=self.entry,
                    cleaned_data=form.cleaned_data,
                    requirement_uploads=valid_requirement_uploads,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            except OperationalError:
                form.add_error(
                    None,
                    "We could not save your uploaded files right now. Please try again in a few moments.",
                )
            else:
                form.attach_existing_draft(
                    draft,
                    saved_notice=(
                        "Your valid files were saved to this draft. "
                        "You only need to reselect the missing or invalid document slots."
                    ),
                )
        return super().form_invalid(form)


class ApplicantOTPView(TemplateView):
    template_name = "recruitment/applicant_otp.html"

    def get_application(self):
        return get_object_or_404(
            RecruitmentApplication.objects.select_related("position", "applicant"),
            public_token=self.kwargs["token"],
        )

    def get(self, request, *args, **kwargs):
        application = self.get_application()
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = kwargs.get("application") or self.get_application()
        context["application"] = application
        context["otp_form"] = kwargs.get("otp_form") or ApplicantOTPForm()
        context["otp_validity_minutes"] = settings.APPLICATION_OTP_VALIDITY_MINUTES
        return context

    def post(self, request, *args, **kwargs):
        application = self.get_application()
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)

        action = request.POST.get("action")
        if action == "resend":
            try:
                issue_application_otp(application)
            except (OperationalError, ValueError) as exc:
                if isinstance(exc, OperationalError):
                    messages.error(
                        request,
                        "We could not prepare a new verification code right now. Please try again in a few moments.",
                    )
                else:
                    messages.error(request, str(exc))
            else:
                messages.success(request, "A new verification code has been sent to your email address.")
            return redirect("applicant-otp", token=application.public_token)

        if action == "verify":
            otp_form = ApplicantOTPForm(request.POST)
            if otp_form.is_valid():
                try:
                    verify_application_otp(application, otp_form.cleaned_data["otp"])
                except ValueError as exc:
                    otp_form.add_error("otp", str(exc))
                else:
                    messages.success(request, "Email verified. You may now submit your application.")
                    return redirect("applicant-otp", token=application.public_token)
            return self.render_to_response(
                self.get_context_data(application=application, otp_form=otp_form)
            )

        if action == "finalize":
            try:
                submit_application(application, application.applicant)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("applicant-otp", token=application.public_token)
            messages.success(request, "Application submitted successfully.")
            return redirect("applicant-receipt", token=application.public_token)

        messages.error(request, "This applicant action is not available.")
        return redirect("applicant-otp", token=application.public_token)


class ApplicantReceiptView(TemplateView):
    template_name = "recruitment/applicant_receipt.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["application"] = get_object_or_404(
            RecruitmentApplication.objects.select_related("position"),
            public_token=self.kwargs["token"],
            submitted_at__isnull=False,
        )
        return context


class ApplicantStatusLookupView(FormView):
    template_name = "recruitment/applicant_status_lookup.html"
    form_class = ApplicantStatusLookupForm

    def form_valid(self, form):
        application = (
            RecruitmentApplication.objects.select_related("position")
            .filter(
                reference_number=form.cleaned_data["application_id"],
                applicant_email__iexact=form.cleaned_data["email"],
                submitted_at__isnull=False,
            )
            .first()
        )
        if not application:
            form.add_error(
                None,
                "We could not find an application with that ID and email combination. "
                "Please check your Application ID and email address and try again.",
            )
            return self.form_invalid(form)
        status_info = _APPLICANT_STATUS_LABELS.get(application.status)
        status_label = status_info[0] if status_info else "Received"
        status_variant = status_info[1] if status_info else "review"
        status_description = status_info[2] if status_info else "Your application has been received."
        return self.render_to_response(self.get_context_data(
            form=form,
            application=application,
            status_label=status_label,
            status_variant=status_variant,
            status_description=status_description,
        ))
