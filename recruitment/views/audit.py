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
    audit_archive_latest_allowed_cutoff,
    download_audit_archive,
    preview_audit_archive,
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
    template_name = "recruitment/audit_log_list.html"
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
    template_name = "recruitment/audit_log_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # REV-D archival panel: existing archives, the age guard, and — when a
        # cutoff was chosen — the confirmation preview (count + date range).
        context["audit_archives"] = AuditLogArchive.objects.select_related("created_by")[:20]
        context["archive_latest_cutoff"] = audit_archive_latest_allowed_cutoff()
        cutoff_raw = (self.request.GET.get("archive_cutoff") or "").strip()
        if cutoff_raw:
            try:
                cutoff = date.fromisoformat(cutoff_raw)
            except ValueError:
                cutoff = None
            if cutoff and cutoff <= audit_archive_latest_allowed_cutoff():
                context["archive_preview"] = preview_audit_archive(cutoff)
                context["archive_preview_cutoff"] = cutoff
            elif cutoff:
                context["archive_preview_error"] = (
                    "Only entries older than six months can be archived. Choose a cutoff "
                    f"on or before {audit_archive_latest_allowed_cutoff():%b %d, %Y}."
                )
        self.search_form = AuditLogSearchForm(self.request.GET or None)
        cleaned_data = _audit_search_cleaned_data(self.search_form)
        session_tag = (self.request.GET.get("session") or "").strip()
        audit_logs = get_all_audit_logs(
            search_query=cleaned_data["q"],
            actor_role=cleaned_data["actor_role"],
            sensitive_only=cleaned_data["sensitive_only"],
            category=cleaned_data["category"],
            actor=cleaned_data["actor"],
            date_from=cleaned_data["date_from"],
            date_to=cleaned_data["date_to"],
            session_tag=session_tag,
        )
        # REV-F: the session view reads chronologically — a visit replayed
        # top to bottom.
        if session_tag:
            audit_logs = audit_logs.order_by("created_at")
            context["session_summary"] = build_audit_session_summary(session_tag)
            context["session_tag"] = session_tag
        context["audit_summary"] = summarize_audit_logs(audit_logs)
        today = timezone.localdate()
        context["date_presets"] = [
            {"label": "Today", "date_from": today, "date_to": today},
            {"label": "Last 7 days", "date_from": today - timedelta(days=6), "date_to": today},
            {"label": "This month", "date_from": today.replace(day=1), "date_to": today},
        ]
        paginator = Paginator(audit_logs, 50)
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)
        params = self.request.GET.copy()
        params.pop("page", None)
        context["search_form"] = self.search_form
        context["audit_logs"] = page_obj.object_list
        context["page_obj"] = page_obj
        context["result_count"] = paginator.count
        context["querystring"] = params.urlencode()
        context["review_scope"] = "system"
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
    if session_tag:
        queryset = queryset.order_by("created_at")
    return queryset, cleaned, session_tag


def _describe_audit_filters(cleaned, session_tag):
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
    return "; ".join(parts) if parts else "no filters (full trail)"


class AuditLogExportCsvView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-F: CSV export of the filtered audit view. The export itself is
    audit-logged."""

    def get(self, request):
        queryset, cleaned, session_tag = _filtered_audit_logs_from_request(request)
        filters_label = _describe_audit_filters(cleaned, session_tag)
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
        queryset, cleaned, session_tag = _filtered_audit_logs_from_request(request)
        summary = summarize_audit_logs(queryset)
        entries = list(queryset[:1000])
        record_system_audit_event(
            actor=request.user,
            action=AuditLog.Action.EXPORT_GENERATED,
            description=(
                f"Generated printable audit report ({summary['total']} entries; "
                f"{_describe_audit_filters(cleaned, session_tag)})."
            ),
            metadata={
                "export": "audit_report",
                "entry_count": summary["total"],
                "filters": _describe_audit_filters(cleaned, session_tag),
            },
        )
        return render(
            request,
            "recruitment/audit_report_print.html",
            {
                "entries": entries,
                "entry_cap": 1000,
                "summary": summary,
                "filters_label": _describe_audit_filters(cleaned, session_tag),
                "session_summary": build_audit_session_summary(session_tag) if session_tag else None,
                "printed_by": request.user,
                "printed_at": timezone.now(),
            },
        )


class AuditLogArchiveView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """REV-D: execute an archive run (export, verify, delete). The GET-side
    preview/confirmation lives on AuditLogListView; this POST only fires from
    the confirmation form."""

    def post(self, request):
        cutoff_raw = (request.POST.get("archive_cutoff") or "").strip()
        try:
            cutoff = date.fromisoformat(cutoff_raw)
        except ValueError:
            messages.error(request, "Choose a valid cutoff date for the archive.")
            return redirect("audit-log-list")
        try:
            archive = archive_audit_logs(request.user, cutoff)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("audit-log-list")
        messages.success(
            request,
            f"Archived {archive.entry_count} audit entries to {archive.file_name}. "
            "The live log now starts after the archived range; the archival "
            "action itself is on the record.",
        )
        return redirect("audit-log-list")


class AuditArchiveDownloadView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def get(self, request, pk):
        archive = get_object_or_404(AuditLogArchive, pk=pk)
        content = download_audit_archive(archive, request.user)
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{archive.file_name}"'
        return response
