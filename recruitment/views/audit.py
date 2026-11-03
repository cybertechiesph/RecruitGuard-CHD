"""The audit log pages for the System Administrator: search, CSV, report and archives.
"""

import csv
from datetime import date, timedelta
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, TemplateView

from ..forms import AuditLogSearchForm
from ..models import (
    AUDIT_ACTION_CATEGORIES,
    AUDIT_CATEGORY_BY_ACTION,
    AuditLog,
    AuditLogArchive,
    RecruitmentApplication,
)
from ..permissions import SystemAdministratorRequiredMixin
from ..services import (
    build_audit_session_summary,
    record_system_audit_event,
    summarize_audit_logs,
    archive_audit_logs,
    download_audit_archive,
    get_application_audit_logs,
    get_all_audit_logs,
    record_audit_log_review,
)
from .common import _should_record_audit_review


class ApplicationAuditLogView(
    LoginRequiredMixin,
    SystemAdministratorRequiredMixin,
    DetailView,
):
    model = RecruitmentApplication
    template_name = "recruitment/admin/audit_log.html"
    context_object_name = "application"

    def get_object(self, queryset=None):
        return super().get_object(queryset)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = context["application"]
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        cleaned_data = _audit_search_cleaned_data(self.search_form)
        audit_logs = list(
            get_application_audit_logs(
                application,
                search_query=cleaned_data["q"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                category=cleaned_data["category"],
                actor=cleaned_data["actor"],
                date_from=cleaned_data["date_from"],
                date_to=cleaned_data["date_to"],
            )
        )
        context["search_form"] = self.search_form
        context["audit_logs"] = audit_logs
        context["result_count"] = len(audit_logs)
        context["review_scope"] = "application"
        context["recruitment_case"] = getattr(application, "case", None)
        # A case trail never hides sign-ins, so it offers no toggle for them.
        context.update(_toolbar_context(self.request, cleaned_data, signins_hidden=False, signins_toggleable=False))
        if _should_record_audit_review(self.request, f"application:{application.pk}"):
            record_audit_log_review(
                actor=self.request.user,
                application=application,
                search_query=cleaned_data["q"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                result_count=len(audit_logs),
            )
        return context


class AuditLogListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, TemplateView):
    """Round F2: the log first, as a table grouped by day, under one toolbar; routine sign-ins
    hidden by default; archives live on the Maintenance page (Round F3)
    (docs/admin-area-rebuild-plan.md section 4.5)."""

    template_name = "recruitment/admin/audit_log.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        audit_logs, cleaned_data, session_tag, signins_hidden = _filtered_audit_logs_from_request(
            self.request
        )
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        if session_tag:
            context["session_summary"] = build_audit_session_summary(session_tag)
            context["session_tag"] = session_tag
        paginator = Paginator(audit_logs, 50)
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["search_form"] = self.search_form
        # Read the page now: the "audit log viewed" entry written below would otherwise
        # slip into the lazily evaluated slice and push a real entry off the first page.
        context["audit_logs"] = list(page_obj.object_list)
        context["page_obj"] = page_obj
        context["result_count"] = paginator.count
        context["querystring"] = params.urlencode()
        context["review_scope"] = "system"
        context.update(_toolbar_context(self.request, cleaned_data, signins_hidden=signins_hidden))
        # One review entry per visit: the first-page guard stops paging spam,
        # and the session window (REV-D) stops filter-change spam.
        if page_number in (None, "", "1") and _should_record_audit_review(
            self.request, "consolidated"
        ):
            record_audit_log_review(
                actor=self.request.user,
                search_query=cleaned_data["q"],
                actor_role=cleaned_data["actor_role"],
                sensitive_only=cleaned_data["sensitive_only"],
                result_count=paginator.count,
            )
        return context


# Round F2: the routine half of signing in. Hidden by default because they fill the log; the
# failures, lockouts and password changes stay visible.
ROUTINE_SIGN_IN_ACTIONS = [
    AuditLog.Action.INTERNAL_LOGIN,
    AuditLog.Action.INTERNAL_LOGOUT,
    AuditLog.Action.INTERNAL_MFA_SENT,
    AuditLog.Action.INTERNAL_MFA_RESENT,
    AuditLog.Action.INTERNAL_MFA_VERIFIED,
]


def _routine_sign_ins_hidden(request, cleaned, session_tag):
    """Hidden unless asked for, or unless the view is about sign-ins: the sign-ins category,
    or one sign-in session replayed."""
    if request.GET.get("signins") == "show":
        return False
    if cleaned.get("category") == "security" or session_tag:
        return False
    return True


def _toolbar_context(request, cleaned, *, signins_hidden, signins_toggleable=True):
    """The chips, quick ranges and "more filters" state the toolbar draws."""
    today = timezone.localdate()
    ranges = [
        ("Today", today, today),
        ("7 days", today - timedelta(days=6), today),
        ("30 days", today - timedelta(days=29), today),
    ]
    date_from = cleaned.get("date_from")
    date_to = cleaned.get("date_to")
    quick_ranges = [
        {
            "label": label,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "active": date_from == start and date_to == end,
        }
        for label, start, end in ranges
    ]
    custom_range = bool(date_from or date_to) and not any(r["active"] for r in quick_ranges)
    return {
        "category": cleaned.get("category") or "",
        "category_chips": [(key, group["label"]) for key, group in AUDIT_ACTION_CATEGORIES.items()],
        "quick_ranges": quick_ranges,
        "any_range": bool(date_from or date_to),
        "custom_range": custom_range,
        "sensitive_only": bool(cleaned.get("sensitive_only")),
        "signins_hidden": signins_hidden,
        "signins_toggleable": signins_toggleable and cleaned.get("category") != "security",
        "more_filters_count": sum(
            1 for value in (cleaned.get("actor_role"), cleaned.get("actor"), custom_range) if value
        ),
        "search_query": cleaned.get("q") or "",
    }


def _audit_search_cleaned_data(search_form):
    if search_form.is_valid():
        return search_form.cleaned_data
    return {
        "q": "",
        "category": "",
        "actor_role": "",
        "actor": None,
        "date_from": None,
        "date_to": None,
        "sensitive_only": False,
    }


def _filtered_audit_logs_from_request(request):
    """Shared by the list view and both exports so a report always contains
    exactly what the filtered screen shows."""
    form = AuditLogSearchForm(request.GET or None)
    cleaned = _audit_search_cleaned_data(form)
    session_tag = (request.GET.get("session") or "").strip()
    queryset = get_all_audit_logs(
        search_query=cleaned["q"],
        actor_role=cleaned["actor_role"],
        sensitive_only=cleaned["sensitive_only"],
        category=cleaned["category"],
        actor=cleaned["actor"],
        date_from=cleaned["date_from"],
        date_to=cleaned["date_to"],
        session_tag=session_tag,
    )
    signins_hidden = _routine_sign_ins_hidden(request, cleaned, session_tag)
    if signins_hidden:
        queryset = queryset.exclude(action__in=ROUTINE_SIGN_IN_ACTIONS)
    # REV-F: the session view reads chronologically — a visit replayed top to bottom.
    if session_tag:
        queryset = queryset.order_by("created_at")
    return queryset, cleaned, session_tag, signins_hidden


def _describe_audit_filters(cleaned, session_tag, signins_hidden=False):
    parts = []
    if cleaned["q"]:
        parts.append(f'search "{cleaned["q"]}"')
    if cleaned["category"]:
        parts.append(f"category {AUDIT_ACTION_CATEGORIES[cleaned['category']]['label']}")
    if cleaned["actor"]:
        parts.append(f"user {cleaned['actor'].get_full_name() or cleaned['actor'].username}")
    if cleaned["actor_role"]:
        parts.append(f"role {cleaned['actor_role']}")
    if cleaned["date_from"]:
        parts.append(f"from {cleaned['date_from']:%b %d, %Y}")
    if cleaned["date_to"]:
        parts.append(f"to {cleaned['date_to']:%b %d, %Y}")
    if cleaned["sensitive_only"]:
        parts.append("sensitive only")
    if session_tag:
        parts.append("single session")
    if signins_hidden:
        parts.append("routine sign-ins hidden")
    return "; ".join(parts) if parts else "no filters (full trail)"


class AuditLogExportCsvView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-F: CSV export of the filtered audit view. The export itself is
    audit-logged."""

    def get(self, request):
        queryset, cleaned, session_tag, signins_hidden = _filtered_audit_logs_from_request(request)
        filters_label = _describe_audit_filters(cleaned, session_tag, signins_hidden)
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["created_at", "actor", "actor_role", "action", "category",
             "case_reference", "workflow_stage", "description", "sensitive"]
        )
        count = 0
        for entry in queryset.iterator():
            writer.writerow(
                [
                    entry.created_at.isoformat(),
                    entry.actor.username if entry.actor else "",
                    entry.actor_role,
                    entry.action,
                    AUDIT_CATEGORY_BY_ACTION.get(entry.action, "other"),
                    entry.case_reference,
                    entry.workflow_stage,
                    entry.description,
                    entry.is_sensitive_access,
                ]
            )
            count += 1
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=f"Exported {count} audit entries to CSV ({filters_label}).",
            metadata={"export": "audit_csv", "entry_count": count, "filters": filters_label},
        )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="audit-report-{timezone.localdate():%Y%m%d}.csv"'
        )
        return response


class AuditLogReportView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-F: print-ready audit report — the filtered view as a document a
    director can hold: header with the filters applied, the summary strip,
    then the entries."""

    def get(self, request):
        queryset, cleaned, session_tag, signins_hidden = _filtered_audit_logs_from_request(request)
        filters_label = _describe_audit_filters(cleaned, session_tag, signins_hidden)
        summary = summarize_audit_logs(queryset)
        entries = list(queryset[:1000])
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=(
                f"Generated printable audit report ({summary['total']} entries; "
                f"{filters_label})."
            ),
            metadata={
                "export": "audit_report",
                "entry_count": summary["total"],
                "filters": filters_label,
            },
        )
        return render(
            request,
            "recruitment/audit_report_print.html",
            {
                "entries": entries,
                "entry_cap": 1000,
                "summary": summary,
                "filters_label": filters_label,
                "session_summary": build_audit_session_summary(session_tag) if session_tag else None,
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class AuditLogArchiveView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-D: execute an archive run (export, verify, delete). The GET-side
    preview/confirmation lives on the Maintenance page (Round F3); this POST only
    fires from the confirmation form."""

    def post(self, request):
        cutoff_raw = (request.POST.get("archive_cutoff") or "").strip()
        try:
            cutoff = date.fromisoformat(cutoff_raw)
        except ValueError:
            messages.error(request, "Choose a valid cutoff date for the archive.")
            return redirect(reverse("maintenance") + "#archives")
        try:
            archive = archive_audit_logs(request.user, cutoff)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(reverse("maintenance") + "#archives")
        messages.success(
            request,
            f"Archived {archive.entry_count} audit entries to {archive.file_name}. "
            "The live log now starts after the archived range; the archival "
            "action itself is on the record.",
        )
        return redirect(reverse("maintenance") + "#archives")


class AuditArchiveDownloadView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def get(self, request, pk):
        archive = get_object_or_404(AuditLogArchive, pk=pk)
        content = download_audit_archive(archive, request.user)
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{archive.file_name}"'
        return response
