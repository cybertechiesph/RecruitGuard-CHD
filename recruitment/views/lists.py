"""Case lists: all applications, the work queue, closed cases and the schedule calendar.
"""

import calendar as pycalendar
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from ..schedule_services import (
    EventKind,
    build_month_grid,
    get_schedule_events_for_user,
    group_events_by_day,
    parse_month,
)
from ..forms import ClosedCaseFilterForm
from ..models import PositionPosting, RecruitmentUser
from ..permissions import InternalUserRequiredMixin, WorkflowProcessorRequiredMixin
from ..services import (
    get_current_workflow_section,
    get_closed_cases_for_user,
    get_queue_for_user,
)
from ..templatetags.recruitment_ui import QUEUE_TASK_LABELS


# Section keys offered in the workflow list "Needed step" filter (built from the
# canonical queue labels; "overview" is a non-actionable placeholder, so it is dropped).
WORKFLOW_LIST_STEP_CHOICES = [
    (key, label) for key, label in QUEUE_TASK_LABELS.items() if key != "overview"
]


def _apply_workflow_list_search(queryset, q, include_email=False):
    """Filter a role-scoped application queryset by applicant name / reference / position
    (and, for the Closed Cases records lookup, the applicant's e-mail). Stays inside the
    caller's already-scoped queryset — it only narrows, never widens."""
    q = (q or "").strip()
    if not q:
        return queryset
    match = (
        Q(applicant__first_name__icontains=q)
        | Q(applicant__last_name__icontains=q)
        | Q(reference_number__icontains=q)
        | Q(position__title__icontains=q)
    )
    if include_email:
        match |= Q(applicant_email__icontains=q)
    return queryset.filter(match)


def _workflow_list_object_list(base_queryset, params, include_email=False):
    """Apply the shared search + step filter to a scoped queryset. Returns a queryset when only
    search is applied, or a Python list when the step filter is applied (the current workflow
    section is computed per-application, not stored, so it can't be filtered in the database).
    Either type is paginatable by ListView."""
    queryset = _apply_workflow_list_search(
        base_queryset, params.get("q", ""), include_email=include_email
    )
    branch = (params.get("branch", "") or "").strip()
    if branch in {PositionPosting.Branch.PLANTILLA, PositionPosting.Branch.COS}:
        queryset = queryset.filter(branch=branch)
    step = (params.get("step", "") or "").strip()
    if step:
        return [
            application
            for application in queryset
            if get_current_workflow_section(application) == step
        ]
    return queryset


def _add_workflow_list_search_context(context, request):
    """Populate the shared search/filter/pagination context used by application_list.html."""
    params = request.GET
    context["q"] = (params.get("q", "") or "").strip()
    context["step"] = (params.get("step", "") or "").strip()
    context["branch"] = (params.get("branch", "") or "").strip()
    context["step_choices"] = WORKFLOW_LIST_STEP_CHOICES
    context["show_search"] = True
    querystring = request.GET.copy()
    querystring.pop("page", None)
    context["querystring"] = querystring.urlencode()
    # The branch chips swap only the branch and keep every other active filter.
    querystring.pop("branch", None)
    context["querystring_without_branch"] = querystring.urlencode()
    paginator = context.get("paginator")
    context["total_count"] = paginator.count if paginator else len(context.get("applications") or [])


class ApplicationListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"

    def get(self, request, *args, **kwargs):
        user = request.user
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return redirect("workflow-queue")

    def get_queryset(self):
        user = self.request.user
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return get_queue_for_user(user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = self.request.user.role != RecruitmentUser.Role.APPLICANT
        return context


class WorkflowQueueView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, ListView):
    template_name = "recruitment/application_list.html"
    context_object_name = "applications"
    paginate_by = 25

    def get_queryset(self):
        return _workflow_list_object_list(
            get_queue_for_user(self.request.user), self.request.GET
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = True
        _add_workflow_list_search_context(context, self.request)
        return context


class ClosedCaseListView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, ListView):
    """Closed / decided cases the user retains view access to — the records-request lookup that
    the active queue can't provide. Scoped by get_closed_cases_for_user, which mirrors the
    existing closed-case access rules, so it never shows a case the user couldn't already open."""

    template_name = "recruitment/application_list.html"
    context_object_name = "applications"
    paginate_by = 25

    def get_queryset(self):
        user = self.request.user
        scope = get_closed_cases_for_user(user)
        self.filter_form = ClosedCaseFilterForm(self.request.GET or None, scope=scope)
        filters = self.filter_form.filter_values() if self.request.GET else {}
        self.active_filters = filters
        queryset = _apply_closed_case_filters(scope, filters)
        # HRMPSB members review blind (no applicant names on their lists), so the records
        # lookup must not let them confirm an applicant by e-mail either.
        include_email = user.role != RecruitmentUser.Role.HRMPSB_MEMBER
        return _workflow_list_object_list(queryset, self.request.GET, include_email=include_email)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_queue"] = False
        context["is_closed_list"] = True
        context["closed_filter_form"] = self.filter_form
        context["closed_filters_active"] = bool(
            {key for key in self.active_filters if key != "sort"}
        )
        context["search_includes_email"] = (
            self.request.user.role != RecruitmentUser.Role.HRMPSB_MEMBER
        )
        _add_workflow_list_search_context(context, self.request)
        return context


def _apply_closed_case_filters(queryset, filters):
    """TI-4: outcome / closed-date / year / vacancy filters and sort for Closed Cases. Only
    ever narrows (or reorders) the caller's scoped queryset."""
    if filters.get("outcome"):
        queryset = queryset.filter(status=filters["outcome"])
    if filters.get("year"):
        queryset = queryset.filter(closed_at__year=int(filters["year"]))
    if filters.get("closed_from"):
        queryset = queryset.filter(closed_at__date__gte=filters["closed_from"])
    if filters.get("closed_to"):
        queryset = queryset.filter(closed_at__date__lte=filters["closed_to"])
    if filters.get("vacancy"):
        queryset = queryset.filter(position_id=int(filters["vacancy"]))
    sort = filters.get("sort") or ClosedCaseFilterForm.SORT_NEWEST
    if sort == ClosedCaseFilterForm.SORT_OLDEST:
        queryset = queryset.order_by(F("closed_at").asc(nulls_last=True), "updated_at", "pk")
    elif sort == ClosedCaseFilterForm.SORT_CASE:
        queryset = queryset.order_by("reference_number", "pk")
    else:
        queryset = queryset.order_by(F("closed_at").desc(nulls_last=True), "-updated_at", "pk")
    return queryset


class ScheduleCalendarView(LoginRequiredMixin, WorkflowProcessorRequiredMixin, TemplateView):
    """TI-5: read-only month / agenda view of exams, interviews, deadlines and recorded
    deliberations. Every event comes from schedule_services, which builds them only from
    applications the user can already open (queue + closed cases)."""

    template_name = "recruitment/schedule_calendar.html"
    AGENDA_DAYS = 30

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        view_mode = "agenda" if self.request.GET.get("view") == "agenda" else "month"
        month_start = parse_month(self.request.GET.get("month"), today)
        month_end = month_start.replace(
            day=pycalendar.monthrange(month_start.year, month_start.month)[1]
        )
        if view_mode == "agenda":
            start, end = today, today + timedelta(days=self.AGENDA_DAYS - 1)
        else:
            # The grid shows whole weeks, so fetch the leading/trailing days too.
            weeks_start = month_start - timedelta(days=(month_start.weekday() + 1) % 7)
            weeks_end = month_end + timedelta(days=(5 - month_end.weekday()) % 7)
            start, end = weeks_start, weeks_end
        events = get_schedule_events_for_user(self.request.user, start, end)
        prev_month = (month_start - timedelta(days=1)).replace(day=1)
        next_month = (month_end + timedelta(days=1)).replace(day=1)
        context.update(
            {
                "view_mode": view_mode,
                "month_start": month_start,
                "month_param": month_start.strftime("%Y-%m"),
                "prev_month_param": prev_month.strftime("%Y-%m"),
                "next_month_param": next_month.strftime("%Y-%m"),
                "today": today,
                "agenda_days": self.AGENDA_DAYS,
                "weeks": build_month_grid(month_start, events) if view_mode == "month" else [],
                "month_days": group_events_by_day(
                    [event for event in events if month_start <= event.day <= month_end]
                ) if view_mode == "month" else {},
                "agenda": group_events_by_day(events) if view_mode == "agenda" else {},
                "event_kinds": EventKind.LABELS,
                "weekday_names": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            }
        )
        return context
