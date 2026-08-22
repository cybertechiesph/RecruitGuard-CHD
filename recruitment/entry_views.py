from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from .forms import (
    PositionDocumentRequirementsForm,
    RecruitmentEntryCancellationForm,
    RecruitmentEntryForm,
)
from .models import AuditLog, PositionPosting
from .permissions import EntryManagerRequiredMixin
from .services import (
    cancel_recruitment_entry,
    get_manageable_recruitment_entries,
    persist_recruitment_entry,
    record_system_audit_event,
    set_position_document_requirements,
    update_recruitment_entry_status,
    user_can_generate_entry_car_filing_copy,
)


def _add_validation_messages(request, error):
    if hasattr(error, "message_dict"):
        for field_errors in error.message_dict.values():
            for message in field_errors:
                messages.error(request, message)
        return
    for message in error.messages:
        messages.error(request, message)


class RecruitmentEntryListView(LoginRequiredMixin, EntryManagerRequiredMixin, ListView):
    template_name = "recruitment/recruitment_entry_list.html"
    context_object_name = "entries"

    def get_queryset(self):
        return get_manageable_recruitment_entries(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entries = list(context["entries"])
        today = timezone.localdate()
        context["entry_active_count"] = sum(
            1 for e in entries if e.status == PositionPosting.EntryStatus.ACTIVE
        )
        context["entry_closing_soon_count"] = sum(
            1 for e in entries
            if e.closing_date and 0 <= (e.closing_date - today).days <= 7
        )
        # N1: the vacancy row is the last place the Secretariat still has the vacancy once
        # its CAR is finalized and every case has routed to the Appointing Authority.
        context["filing_copy_entry_ids"] = {
            entry.pk
            for entry in entries
            if user_can_generate_entry_car_filing_copy(self.request.user, entry)
        }
        return context


class _RecruitmentEntryDocumentMixin:
    """Shared create/edit handling for the embedded application-documents checklist."""

    success_message = ""

    def _documents_form_kwargs(self):
        posting = getattr(self, "object", None)
        if self.request.method == "POST":
            branch = self.request.POST.get("branch") or (posting.branch if posting else None)
        else:
            branch = posting.branch if posting else None
        return {
            "branch": branch,
            "posting": posting,
            "locked": bool(posting and posting.pk and posting.is_live_for_metadata_lock),
        }

    def _build_documents_form(self, data=None):
        return PositionDocumentRequirementsForm(data, **self._documents_form_kwargs())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["selected_position_reference"] = context["form"].selected_position_reference
        if "documents_form" not in context:
            data = self.request.POST if self.request.method == "POST" else None
            context["documents_form"] = self._build_documents_form(data)
        return context

    def form_valid(self, form):
        posting = persist_recruitment_entry(
            entry=form.save(commit=False),
            actor=self.request.user,
            changed_fields=form.changed_data,
        )
        self.object = posting
        if posting.is_live_for_metadata_lock:
            # Configuration is locked once the posting is live; keep the existing rows.
            messages.success(self.request, self.success_message)
            return redirect(self.get_success_url())
        documents_form = self._build_documents_form(self.request.POST)
        if not documents_form.is_valid():
            return self.render_to_response(
                self.get_context_data(form=form, documents_form=documents_form)
            )
        try:
            set_position_document_requirements(
                posting, documents_form.get_selections(), self.request.user
            )
        except ValidationError as error:
            _add_validation_messages(self.request, error)
            return self.render_to_response(
                self.get_context_data(form=form, documents_form=documents_form)
            )
        messages.success(self.request, self.success_message)
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("recruitment-entry-list")


class RecruitmentEntryCreateView(
    _RecruitmentEntryDocumentMixin, LoginRequiredMixin, EntryManagerRequiredMixin, CreateView
):
    template_name = "recruitment/recruitment_entry_form.html"
    model = PositionPosting
    form_class = RecruitmentEntryForm
    success_message = "Recruitment entry created."


class RecruitmentEntryUpdateView(
    _RecruitmentEntryDocumentMixin, LoginRequiredMixin, EntryManagerRequiredMixin, UpdateView
):
    template_name = "recruitment/recruitment_entry_form.html"
    model = PositionPosting
    form_class = RecruitmentEntryForm
    success_message = "Recruitment entry updated."

    def form_valid(self, form):
        # REV-A: deadline extensions are transparent, never silent. Capture the
        # stored closing date before the save so the change can be flagged,
        # audited, and (when the posting stayed manually Closed) explained.
        previous = (
            PositionPosting.objects.filter(pk=form.instance.pk)
            .values("closing_date", "status")
            .first()
        ) or {}
        previous_closing_date = previous.get("closing_date")
        previous_status = previous.get("status")
        new_closing_date = form.cleaned_data.get("closing_date")
        was_published = bool(
            previous
            and previous_status != PositionPosting.EntryStatus.DRAFT
        )
        is_extension = bool(
            was_published
            and previous_closing_date
            and new_closing_date
            and new_closing_date > previous_closing_date
        )
        if is_extension:
            form.instance.deadline_extended = True

        response = super().form_valid(form)
        posting = self.object

        if was_published and previous_closing_date != new_closing_date:
            record_system_audit_event(
                actor=self.request.user,
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                description=(
                    f"Closing date for {posting.job_code} moved from "
                    f"{previous_closing_date:%b %d, %Y} to {new_closing_date:%b %d, %Y}."
                    if previous_closing_date and new_closing_date
                    else f"Closing date for {posting.job_code} changed."
                ),
                metadata={
                    "recruitment_entry_id": posting.id,
                    "job_code": posting.job_code,
                    "previous_closing_date": previous_closing_date.isoformat() if previous_closing_date else "",
                    "new_closing_date": new_closing_date.isoformat() if new_closing_date else "",
                    "is_extension": is_extension,
                    "previous_status": previous_status,
                },
            )
        if is_extension:
            if posting.status == PositionPosting.EntryStatus.ACTIVE:
                messages.info(
                    self.request,
                    "Deadline extended — the posting is open to applicants again and "
                    "publicly labeled as extended. The change is recorded in the audit log.",
                )
            else:
                messages.warning(
                    self.request,
                    "The closing date was extended, but this entry's status is "
                    f"\"{posting.get_status_display()}\" — it will not reopen to applicants "
                    "until the status is set back to Active.",
                )
        return response


class RecruitmentEntryCancelView(LoginRequiredMixin, EntryManagerRequiredMixin, View):
    """SL-11: cancel a published vacancy and resolve every in-flight application.

    Kept off RecruitmentEntryStatusUpdateView deliberately — this is not another status
    toggle. It closes live cases and emails their applicants, so it demands its own form
    with a required reason rather than a one-click status link."""

    def post(self, request, pk):
        entry = get_object_or_404(PositionPosting, pk=pk)
        form = RecruitmentEntryCancellationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record why this vacancy is being cancelled.")
            return redirect("recruitment-entry-list")
        try:
            _entry, affected = cancel_recruitment_entry(
                entry, request.user, form.cleaned_data["reason"]
            )
        except ValidationError as error:
            _add_validation_messages(request, error)
            return redirect("recruitment-entry-list")
        messages.success(
            request,
            f"{entry.job_code} cancelled. {len(affected)} in-flight "
            f"application{'' if len(affected) == 1 else 's'} closed with a "
            "vacancy-cancelled disposition and notified — none were recorded as rejected.",
        )
        return redirect("recruitment-entry-list")


class RecruitmentEntryStatusUpdateView(LoginRequiredMixin, EntryManagerRequiredMixin, View):
    def post(self, request, pk, status):
        entry = get_object_or_404(PositionPosting, pk=pk)
        if status not in PositionPosting.EntryStatus.values:
            messages.error(request, "Invalid entry status.")
            return redirect("recruitment-entry-list")
        if status == PositionPosting.EntryStatus.CANCELLED:
            # SL-11: cancellation resolves live cases and notifies their applicants, so it
            # cannot be a status link — it goes through the form that demands a reason.
            messages.error(
                request,
                "Cancelling a vacancy needs a recorded reason. Use the Cancel vacancy "
                "action instead.",
            )
            return redirect("recruitment-entry-list")
        try:
            update_recruitment_entry_status(entry, request.user, status)
        except ValidationError as error:
            _add_validation_messages(request, error)
            return redirect("recruitment-entry-list")
        messages.success(request, f"Recruitment entry status updated to {entry.get_status_display()}.")
        return redirect("recruitment-entry-list")
