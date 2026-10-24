from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.text import slugify
from django.views import View
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from .forms import (
    ApplicantOTPForm,
    ApplicantPortalIntakeForm,
    ApplicantStatusLookupForm,
    DocumentResubmissionForm,
)
from .models import PositionPosting, RecruitmentApplication, RecruitmentCase
from .applicant_labels import APPLICANT_STATUS_LABELS as _APPLICANT_STATUS_LABELS
from .requirements import (
    ALL_FORM_DOWNLOADS,
    PDS_FORM_DOWNLOAD,
    PDS_OFFICIAL_GUIDE_DOWNLOAD,
    PERFORMANCE_RATING,
    WES_FORM_DOWNLOAD,
    get_applicant_document_requirements,
)
from .services import (
    ApplicationOTPDeliveryError,
    create_public_application_draft,
    _build_application_receipt_pdf,
    get_current_applicant_document_items,
    get_public_recruitment_entries,
    get_resubmission_request,
    issue_application_otp,
    save_public_application_draft_progress,
    submit_application,
    submit_document_resubmission,
    verify_application_otp,
)
from .upload_validation import MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES


def _posting_is_closing_soon(posting):
    """Return True if the posting closes within 7 days."""
    if posting.closing_date:
        delta = posting.closing_date - timezone.localdate()
        return 0 <= delta.days <= 7
    return False


def _build_applicant_upcoming_schedules(application):
    """Upcoming exam / interview the applicant was notified about, soonest first.
    Only future, applicant-facing schedules are shown so the page reflects what the
    applicant still needs to attend."""
    now = timezone.now()
    schedules = []
    exam_schedule = (
        application.exam_schedules.filter(
            applicant_notified_at__isnull=False,
            scheduled_for__gte=now,
        )
        .order_by("scheduled_for")
        .first()
    )
    if exam_schedule:
        schedules.append(
            {
                "kind": "Examination",
                "scheduled_for": exam_schedule.scheduled_for,
                "location": exam_schedule.venue,
                "instructions": exam_schedule.instructions,
            }
        )
    interview_session = (
        # RD-9: only announced interviews reach the applicant's status page —
        # a drafted, never-notified session must not leak (parity with the
        # exam filter above).
        application.interview_sessions.filter(
            applicant_notified_at__isnull=False,
            scheduled_for__gte=now,
        )
        .order_by("scheduled_for")
        .first()
    )
    if interview_session:
        schedules.append(
            {
                "kind": "Interview",
                "scheduled_for": interview_session.scheduled_for,
                "location": interview_session.location,
                "instructions": "",
            }
        )
    schedules.sort(key=lambda item: item["scheduled_for"])
    return schedules


def _build_applicant_status_context(application):
    status_info = _APPLICANT_STATUS_LABELS.get(application.status)
    status_label = status_info[0] if status_info else "Received"
    status_variant = status_info[1] if status_info else "review"
    status_description = status_info[2] if status_info else "Your application has been received."
    case = getattr(application, "case", None)
    awaiting_resubmission = bool(
        case and case.case_status == RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
    )
    resubmission_deadline = None
    if awaiting_resubmission:
        # RD-9 (moved from RD-6 item d): show the applicant the same deadline
        # the office tracks, right next to the Resubmit button.
        screening_record = (
            application.screening_records.filter(
                resubmission_deadline__isnull=False
            )
            .order_by("-updated_at")
            .first()
        )
        if screening_record:
            resubmission_deadline = screening_record.resubmission_deadline
    return {
        "application": application,
        "status_label": status_label,
        "status_variant": status_variant,
        "status_description": status_description,
        "awaiting_resubmission": awaiting_resubmission,
        "resubmission_deadline": resubmission_deadline,
        "upcoming_schedules": _build_applicant_upcoming_schedules(application),
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
        document_requirements = list(get_applicant_document_requirements(self.entry))
        document_requirements.sort(
            key=lambda requirement: requirement.code == PERFORMANCE_RATING
        )
        context["document_requirements"] = document_requirements
        context["is_closing_soon"] = _posting_is_closing_soon(self.entry)
        context["can_apply"] = self.entry.is_open_for_intake
        return context


class ApplicantHelpView(TemplateView):
    template_name = "recruitment/applicant_help.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["otp_validity_minutes"] = settings.APPLICATION_OTP_VALIDITY_MINUTES
        context["form_downloads"] = ALL_FORM_DOWNLOADS
        return context


class ApplicantPdsGuideView(TemplateView):
    """Plain-language companion to the CSC's official PDS guide (source copy:
    docs/pds-plain-guide-copy.md). The official guide stays linked as the authority."""

    template_name = "recruitment/applicant_pds_guide.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pds_form"] = PDS_FORM_DOWNLOAD
        context["pds_official_guide"] = PDS_OFFICIAL_GUIDE_DOWNLOAD
        context["wes_form"] = WES_FORM_DOWNLOAD
        return context


class ApplicantPortalIntakeView(FormView):
    template_name = "recruitment/applicant_intake_form.html"
    form_class = ApplicantPortalIntakeForm

    def dispatch(self, request, *args, **kwargs):
        self.entry = get_object_or_404(
            PositionPosting.objects.select_related("position_reference"),
            pk=kwargs["pk"],
        )
        if not self.entry.is_open_for_intake:
            messages.error(
                request,
                "This position is no longer accepting applications. "
                "You can browse the other open positions below.",
            )
            return redirect("applicant-portal")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["entry"] = self.entry
        kwargs["request"] = self.request
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
            "submission_confirmation": draft.checklist_complete,
            "checklist_privacy_consent": draft.checklist_privacy_consent,
            "checklist_documents_complete": draft.checklist_documents_complete,
            "checklist_information_certified": draft.checklist_information_certified,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entry"] = self.entry
        max_bytes = min(
            getattr(
                settings,
                "MAX_EVIDENCE_UPLOAD_BYTES",
                MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES,
            ),
            MAX_APPLICANT_DOCUMENT_UPLOAD_BYTES,
        )
        context["max_upload_bytes"] = max_bytes
        context["max_upload_mb"] = max_bytes // (1024 * 1024)
        return context

    def get(self, request, *args, **kwargs):
        draft = self.get_draft_from_token()
        if draft is None:
            return super().get(request, *args, **kwargs)

        form = self.get_form_class()(
            entry=self.entry,
            request=self.request,
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
        return redirect("applicant-otp", token=application.public_token)

    def form_invalid(self, form):
        valid_requirement_uploads = form.get_valid_requirement_uploads()
        if (
            valid_requirement_uploads
            and form.can_persist_draft_uploads()
            and "email" not in form.errors
            and "captcha_answer" not in form.errors
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
                issue_application_otp(
                    application,
                    defer_delivery=False,
                    enforce_cooldown=True,
                )
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
            _mark_status_otp_verified(request, application)
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
                "This receipt link is no longer available. If you already submitted, use Track Application with your Tracking Code and email.",
            )
            return redirect("applicant-portal")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = getattr(self, "application", None) or self.get_application()
        if application is None:
            raise Http404
        context["application"] = application
        current_applicant_documents = get_current_applicant_document_items(application)
        context["current_applicant_documents"] = current_applicant_documents
        context["current_applicant_document_count"] = current_applicant_documents.count()
        return context


class ApplicantReceiptPdfView(View):
    """Downloadable PDF of the submission receipt (panel revision: the
    application ID must be a real file, not only browser-print). Same gate as
    the receipt page — possessing the public token proves access; no login."""

    def get(self, request, *args, **kwargs):
        application = (
            RecruitmentApplication.objects.select_related("position")
            .filter(public_token=self.kwargs["token"], submitted_at__isnull=False)
            .first()
        )
        if application is None:
            messages.error(
                request,
                "This receipt link is no longer available. If you already submitted, use Track Application with your Tracking Code and email.",
            )
            return redirect("applicant-portal")

        pdf_bytes = _build_application_receipt_pdf(application)
        reference = application.case_number_short or application.reference_number or "application"
        filename = f"DOH-CHD-Receipt-{slugify(reference) or 'application'}.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


STATUS_OTP_PENDING_SESSION_KEY = "status_lookup_pending_application"
STATUS_OTP_VERIFIED_SESSION_KEY = "status_lookup_verified"


def _masked_email(email):
    local, _, domain = (email or "").partition("@")
    if not domain:
        return email or ""
    visible = local[:1]
    return f"{visible}{'•' * max(len(local) - 1, 2)}@{domain}"


def _status_otp_verified_recently(request, application):
    stamps = request.session.get(STATUS_OTP_VERIFIED_SESSION_KEY) or {}
    raw = stamps.get(str(application.pk))
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return False
    window = timedelta(minutes=settings.STATUS_LOOKUP_SESSION_MINUTES)
    return timezone.now() - stamp <= window


def _mark_status_otp_verified(request, application):
    stamps = request.session.get(STATUS_OTP_VERIFIED_SESSION_KEY) or {}
    stamps[str(application.pk)] = timezone.now().isoformat()
    request.session[STATUS_OTP_VERIFIED_SESSION_KEY] = stamps
    request.session.pop(STATUS_OTP_PENDING_SESSION_KEY, None)


def _pending_status_otp_application(request):
    pk = request.session.get(STATUS_OTP_PENDING_SESSION_KEY)
    if not pk:
        return None
    return (
        RecruitmentApplication.objects.select_related("position")
        .filter(pk=pk, submitted_at__isnull=False)
        .first()
    )


def _build_status_otp_step_context(
    request, application, *, otp_form=None, lookup_form=None
):
    request.session[STATUS_OTP_PENDING_SESSION_KEY] = application.pk
    return {
        "form": (
            lookup_form
            if lookup_form is not None
            else ApplicantStatusLookupForm(request=request)
        ),
        "otp_step": True,
        "otp_application": application,
        "otp_form": otp_form if otp_form is not None else ApplicantOTPForm(),
        "otp_masked_email": _masked_email(application.applicant_email),
        "otp_validity_minutes": settings.APPLICATION_OTP_VALIDITY_MINUTES,
    }


def _build_verified_status_context(request, application):
    return {
        "form": ApplicantStatusLookupForm(request=request),
        **_build_applicant_status_context(application),
    }


class ApplicantStatusLookupView(FormView):
    """Track Application: tracking code + email select the case, but the
    status renders only after an emailed one-time code proves inbox control
    (REV-I). One successful verification unlocks this browser for
    STATUS_LOOKUP_SESSION_MINUTES; the tokened status link uses the same gate."""

    template_name = "recruitment/applicant_status_lookup.html"
    form_class = ApplicantStatusLookupForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    # ── REV-I session bookkeeping ────────────────────────────────────
    def _render_otp_step(self, application, otp_form=None, lookup_form=None):
        return self.render_to_response(
            self.get_context_data(
                **_build_status_otp_step_context(
                    self.request,
                    application,
                    otp_form=otp_form,
                    lookup_form=lookup_form,
                )
            )
        )

    def _render_status(self, application):
        return self.render_to_response(
            self.get_context_data(
                **_build_verified_status_context(self.request, application)
            )
        )

    # ── REV-I OTP actions ────────────────────────────────────────────
    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "verify":
            return self._handle_verify()
        if action == "resend":
            return self._handle_resend()
        return super().post(request, *args, **kwargs)

    def _handle_verify(self):
        application = _pending_status_otp_application(self.request)
        if application is None:
            messages.error(
                self.request,
                "Your verification session has ended. Enter your Tracking Code and email again.",
            )
            return redirect("applicant-status-lookup")
        otp_form = ApplicantOTPForm(self.request.POST)
        if otp_form.is_valid():
            try:
                verify_application_otp(
                    application, otp_form.cleaned_data["otp"], purpose="status_lookup"
                )
            except ValueError as exc:
                otp_form.add_error("otp", str(exc))
            else:
                _mark_status_otp_verified(self.request, application)
                return self._render_status(application)
        return self._render_otp_step(application, otp_form=otp_form)

    def _handle_resend(self):
        application = _pending_status_otp_application(self.request)
        if application is None:
            messages.error(
                self.request,
                "Your verification session has ended. Enter your Tracking Code and email again.",
            )
            return redirect("applicant-status-lookup")
        try:
            issue_application_otp(
                application,
                defer_delivery=False,
                enforce_cooldown=True,
                purpose="status_lookup",
            )
        except ApplicationOTPDeliveryError as exc:
            messages.error(self.request, str(exc))
        except (OperationalError, ValueError) as exc:
            if isinstance(exc, OperationalError):
                messages.error(
                    self.request,
                    "We could not prepare a new verification code right now. Please try again in a few moments.",
                )
            else:
                messages.error(self.request, str(exc))
        else:
            messages.success(self.request, "A new verification code has been sent to your email address.")
        return self._render_otp_step(application)

    def form_valid(self, form):
        application = (
            RecruitmentApplication.objects.select_related("position")
            .filter(
                tracking_code=form.cleaned_data["tracking_code"],
                applicant_email__iexact=form.cleaned_data["email"],
                submitted_at__isnull=False,
            )
            .first()
        )
        if not application:
            form.add_error(
                None,
                "We could not find an application with that tracking code and email combination. "
                "Please check your Tracking Code and email address and try again.",
            )
            return self.form_invalid(form)
        if _status_otp_verified_recently(self.request, application):
            return self._render_status(application)
        try:
            issue_application_otp(
                application,
                defer_delivery=False,
                enforce_cooldown=True,
                purpose="status_lookup",
            )
        except ApplicationOTPDeliveryError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        except OperationalError:
            form.add_error(
                None,
                "We could not send a verification code right now. Please try again in a few moments.",
            )
            return self.form_invalid(form)
        except ValueError as exc:
            # Resend cooldown: a valid code is likely already in the inbox, so
            # move to the code step with the explanation instead of blocking.
            messages.info(self.request, str(exc))
        else:
            messages.success(
                self.request,
                "We emailed a verification code to your address. Enter it below to view your status.",
            )
        return self._render_otp_step(application, lookup_form=form)


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
                "This status link is no longer available. Use your Tracking Code and email to check your status.",
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
        if _status_otp_verified_recently(request, self.application):
            return self.render_to_response(
                self.get_context_data(
                    **_build_verified_status_context(request, self.application)
                )
            )

        otp_context = _build_status_otp_step_context(request, self.application)
        try:
            issue_application_otp(
                self.application,
                defer_delivery=False,
                enforce_cooldown=True,
                purpose="status_lookup",
            )
        except ApplicationOTPDeliveryError as exc:
            messages.error(request, str(exc))
        except OperationalError:
            messages.error(
                request,
                "We could not send a verification code right now. Please try again in a few moments.",
            )
        except ValueError as exc:
            messages.info(request, str(exc))
        else:
            messages.success(
                request,
                "We emailed a verification code to your address. Enter it below to view your status.",
            )
        return self.render_to_response(self.get_context_data(**otp_context))


class ApplicantResubmissionView(TemplateView):
    """Track Application re-upload page: an emailed-OTP step, then a scoped form exposing only
    the flagged documents. Files flow through the encrypted vault and the standard readability
    checks; the email only ever links here (no attachments)."""

    template_name = "recruitment/applicant_resubmission.html"

    def get_application(self):
        return (
            RecruitmentApplication.objects.select_related("position", "applicant", "case")
            .filter(public_token=self.kwargs["token"])
            .first()
        )

    def _missing(self, request):
        messages.error(
            request,
            "This link is no longer available. Use Track Application with your Tracking Code and email.",
        )
        return redirect("applicant-status-lookup")

    def get(self, request, *args, **kwargs):
        application = self.get_application()
        if application is None:
            return self._missing(request)
        requirements, _deadline, _remarks = get_resubmission_request(application)
        if not requirements:
            messages.info(request, "There are no documents awaiting resubmission for this application.")
            return redirect("applicant-status-link", token=application.public_token)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = kwargs.get("application") or self.get_application()
        if application is None:
            raise Http404
        requirements, deadline, remarks_by_code = get_resubmission_request(application)
        context["application"] = application
        context["requirements"] = requirements
        context["resubmission_deadline"] = deadline
        context["otp_verified"] = application.otp_is_currently_valid
        context["otp_form"] = kwargs.get("otp_form") or ApplicantOTPForm()
        context["resubmission_form"] = kwargs.get("resubmission_form") or DocumentResubmissionForm(
            requirements=requirements, remarks_by_code=remarks_by_code
        )
        context["otp_validity_minutes"] = settings.APPLICATION_OTP_VALIDITY_MINUTES
        return context

    def post(self, request, *args, **kwargs):
        application = self.get_application()
        if application is None:
            return self._missing(request)
        requirements, _deadline, remarks_by_code = get_resubmission_request(application)
        if not requirements:
            messages.info(request, "There are no documents awaiting resubmission for this application.")
            return redirect("applicant-status-link", token=application.public_token)

        action = request.POST.get("action")
        if action in {"request_otp", "resend"}:
            try:
                issue_application_otp(
                    application,
                    defer_delivery=False,
                    enforce_cooldown=(action == "resend"),
                )
            except ApplicationOTPDeliveryError as exc:
                messages.error(request, str(exc))
            except (OperationalError, ValueError) as exc:
                if isinstance(exc, OperationalError):
                    messages.error(
                        request,
                        "We could not send a verification code right now. Please try again in a few moments.",
                    )
                else:
                    messages.error(request, str(exc))
            else:
                messages.success(request, "A verification code has been sent to your email address.")
            return redirect("applicant-resubmit", token=application.public_token)

        if action == "verify":
            otp_form = ApplicantOTPForm(request.POST)
            if otp_form.is_valid():
                try:
                    verify_application_otp(application, otp_form.cleaned_data["otp"])
                except ValueError as exc:
                    otp_form.add_error("otp", str(exc))
                else:
                    messages.success(
                        request,
                        "Email verified. You can now re-upload the requested documents.",
                    )
                    return redirect("applicant-resubmit", token=application.public_token)
            return self.render_to_response(
                self.get_context_data(application=application, otp_form=otp_form)
            )

        if action == "resubmit":
            if not application.otp_is_currently_valid:
                messages.error(request, "Verify your email before resubmitting your documents.")
                return redirect("applicant-resubmit", token=application.public_token)
            resubmission_form = DocumentResubmissionForm(
                request.POST, request.FILES, requirements=requirements, remarks_by_code=remarks_by_code
            )
            if resubmission_form.is_valid():
                try:
                    submit_document_resubmission(
                        application,
                        application.applicant,
                        resubmission_form.get_uploads(),
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                    return self.render_to_response(
                        self.get_context_data(
                            application=application, resubmission_form=resubmission_form
                        )
                    )
                messages.success(request, "Your documents have been resubmitted for review.")
                return redirect("applicant-status-link", token=application.public_token)
            return self.render_to_response(
                self.get_context_data(
                    application=application, resubmission_form=resubmission_form
                )
            )

        messages.error(request, "This action is not available.")
        return redirect("applicant-resubmit", token=application.public_token)
