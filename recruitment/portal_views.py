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
    ApplicationOTPDeliveryError,
    create_public_application_draft,
    get_current_applicant_document_items,
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


def _build_applicant_status_context(application):
    status_info = _APPLICANT_STATUS_LABELS.get(application.status)
    status_label = status_info[0] if status_info else "Received"
    status_variant = status_info[1] if status_info else "review"
    status_description = status_info[2] if status_info else "Your application has been received."
    return {
        "application": application,
        "status_label": status_label,
        "status_variant": status_variant,
        "status_description": status_description,
    }


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

    def get_draft_from_token(self):
        token = (self.request.GET.get("token") or "").strip()
        if not token:
            return None
        return (
            RecruitmentApplication.objects.filter(
                public_token=token,
                position=self.entry,
                submitted_at__isnull=True,
                status=RecruitmentApplication.Status.DRAFT,
            )
            .prefetch_related("evidence_items")
            .first()
        )

    def get_initial_from_draft(self, draft):
        return {
            "first_name": draft.applicant_first_name,
            "last_name": draft.applicant_last_name,
            "email": draft.applicant_email,
            "phone": draft.applicant_phone,
            "qualification_summary": draft.qualification_summary,
            "cover_letter": draft.cover_letter,
            "performance_rating_applicability": draft.performance_rating_applicability,
            "checklist_privacy_consent": draft.checklist_privacy_consent,
            "checklist_documents_complete": draft.checklist_documents_complete,
            "checklist_information_certified": draft.checklist_information_certified,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entry"] = self.entry
        max_bytes = getattr(settings, "MAX_EVIDENCE_UPLOAD_BYTES", 5 * 1024 * 1024)
        context["max_upload_mb"] = max_bytes // (1024 * 1024)
        return context

    def get(self, request, *args, **kwargs):
        draft = self.get_draft_from_token()
        if draft is None:
            return super().get(request, *args, **kwargs)

        form = self.get_form_class()(
            entry=self.entry,
            initial=self.get_initial_from_draft(draft),
        )
        form.attach_existing_draft(
            draft,
            saved_notice=(
                "Your saved draft was loaded. Review your information and update anything that needs correction."
            ),
        )
        return self.render_to_response(self.get_context_data(form=form))

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

        try:
            issue_application_otp(
                application,
                actor=application.applicant,
                defer_delivery=False,
            )
        except ApplicationOTPDeliveryError as exc:
            form.add_error(None, str(exc))
            form.attach_existing_draft(
                application,
                saved_notice=(
                    "Your application draft and uploaded files were saved. "
                    "You only need to retry sending the verification code."
                ),
            )
            return self.render_to_response(self.get_context_data(form=form))

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
        return (
            RecruitmentApplication.objects.select_related("position", "applicant")
            .filter(public_token=self.kwargs["token"])
            .first()
        )

    def handle_missing_application(self, request):
        messages.error(
            request,
            "This verification link is no longer available. Please start again from the job openings page.",
        )
        return redirect("applicant-portal")

    def get(self, request, *args, **kwargs):
        application = self.get_application()
        if application is None:
            return self.handle_missing_application(request)
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = kwargs.get("application") or self.get_application()
        if application is None:
            raise Http404
        context["application"] = application
        context["otp_form"] = kwargs.get("otp_form") or ApplicantOTPForm()
        context["otp_validity_minutes"] = settings.APPLICATION_OTP_VALIDITY_MINUTES
        context["current_applicant_document_count"] = (
            get_current_applicant_document_items(application).count()
        )
        return context

    def post(self, request, *args, **kwargs):
        application = self.get_application()
        if application is None:
            return self.handle_missing_application(request)
        if application.submitted_at:
            return redirect("applicant-receipt", token=application.public_token)

        action = request.POST.get("action")
        if action == "resend":
            try:
                issue_application_otp(application, defer_delivery=False)
            except ApplicationOTPDeliveryError as exc:
                messages.error(request, str(exc))
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

    def get_application(self):
        return (
            RecruitmentApplication.objects.select_related("position")
            .filter(public_token=self.kwargs["token"], submitted_at__isnull=False)
            .first()
        )

    def get(self, request, *args, **kwargs):
        self.application = self.get_application()
        if self.application is None:
            messages.error(
                request,
                "This receipt link is no longer available. If you already submitted, use Track Application with your application ID and email.",
            )
            return redirect("applicant-portal")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = getattr(self, "application", None) or self.get_application()
        if application is None:
            raise Http404
        context["application"] = application
        context["current_applicant_document_count"] = (
            get_current_applicant_document_items(application).count()
        )
        return context


class ApplicantStatusLookupView(FormView):
    template_name = "recruitment/applicant_status_lookup.html"
    form_class = ApplicantStatusLookupForm

    def get_unfinished_draft(self, application_id, email):
        return (
            RecruitmentApplication.objects.select_related("position")
            .filter(
                reference_number=application_id,
                applicant_email__iexact=email,
                submitted_at__isnull=True,
                status=RecruitmentApplication.Status.DRAFT,
            )
            .first()
        )

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
            draft = self.get_unfinished_draft(
                form.cleaned_data["application_id"],
                form.cleaned_data["email"],
            )
            if draft is not None:
                messages.info(
                    self.request,
                    (
                        f"Your application for {draft.position.title} is not finished yet. "
                        "Verify your email to continue, or use Resend the code if the code expired."
                    ),
                )
                return redirect("applicant-otp", token=draft.public_token)
            form.add_error(
                None,
                "We could not find an application with that ID and email combination. "
                "Please check your Application ID and email address and try again.",
            )
            return self.form_invalid(form)
        return self.render_to_response(
            self.get_context_data(form=form, **_build_applicant_status_context(application))
        )


class ApplicantStatusLinkView(TemplateView):
    template_name = "recruitment/applicant_status_lookup.html"

    def get_application(self):
        return (
            RecruitmentApplication.objects.select_related("position")
            .filter(public_token=self.kwargs["token"])
            .first()
        )

    def get(self, request, *args, **kwargs):
        self.application = self.get_application()
        if self.application is None:
            messages.error(
                request,
                "This status link is no longer available. Use your Application ID and email to check your status.",
            )
            return redirect("applicant-status-lookup")
        if self.application.submitted_at is None:
            messages.info(
                request,
                (
                    f"Your application for {self.application.position.title} is not finished yet. "
                    "Verify your email to continue, or use Resend the code if the code expired."
                ),
            )
            return redirect("applicant-otp", token=self.application.public_token)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = getattr(self, "application", None) or self.get_application()
        if application is None or application.submitted_at is None:
            raise Http404
        context["form"] = ApplicantStatusLookupForm(
            initial={
                "application_id": application.reference_number,
                "email": application.applicant_email,
            }
        )
        context.update(_build_applicant_status_context(application))
        return context
