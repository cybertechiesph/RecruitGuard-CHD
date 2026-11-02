"""The dashboard, the forbidden page, the session keep-alive and the positions list.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, TemplateView

from ..models import (
    COMPLETION_SLA_OVERDUE_THRESHOLD,
    STAGE_SLA_OVERDUE_THRESHOLD,
    AuditLog,
    AuditLogArchive,
    PositionPosting,
    RecruitmentCase,
    RecruitmentUser,
)
from ..permissions import InternalUserRequiredMixin
from ..services import (
    summarize_audit_logs,
    get_all_audit_logs,
    get_queue_for_user,
    get_visible_positions_for_user,
)


class DashboardView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    template_name = "recruitment/dashboard.html"

    def get(self, request, *args, **kwargs):
        # Workflow roles land on the vacancy batch console — that is their home; the flat
        # My Queue is a secondary view. Only System Admin sees the dashboard overview.
        if request.user.role != RecruitmentUser.Role.SYSTEM_ADMIN:
            return redirect("vacancy-batches")
        return super().get(request, *args, **kwargs)

    # Failed/locked authentication events surfaced on the security card.
    SECURITY_WATCH_ACTIONS = [
        AuditLog.Action.INTERNAL_LOGIN_FAILED,
        AuditLog.Action.INTERNAL_LOGIN_LOCKED,
        AuditLog.Action.INTERNAL_MFA_FAILED,
        AuditLog.Action.INTERNAL_MFA_LOCKED,
        AuditLog.Action.APPLICATION_OTP_FAILED,
        AuditLog.Action.APPLICATION_OTP_LOCKED,
        AuditLog.Action.EVIDENCE_ACCESS_DENIED,
    ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["open_entry_count"] = get_visible_positions_for_user(user).count()
        if user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            internal_users = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())
            context["internal_user_count"] = internal_users.count()
            context["active_internal_user_count"] = internal_users.filter(is_active=True).count()
            context["recent_identity_logs"] = AuditLog.objects.filter(
                action__in=[
                    AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_UPDATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED,
                    AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                    AuditLog.Action.INTERNAL_ROLE_CHANGED,
                ]
            )[:5]

            # Panel revision (Chavez, "improve dashboard"): the admin home is an
            # oversight console, assembled from data the system already tracks.
            today = timezone.localdate()
            week_ago = today - timedelta(days=6)

            active_postings = PositionPosting.objects.filter(
                status=PositionPosting.EntryStatus.ACTIVE
            )
            context["active_vacancy_count"] = active_postings.count()
            context["closing_soon_count"] = active_postings.filter(
                closing_date__gte=today, closing_date__lte=today + timedelta(days=7)
            ).count()

            open_cases = RecruitmentCase.objects.exclude(
                current_stage=RecruitmentCase.Stage.CLOSED
            )
            context["open_case_count"] = open_cases.count()
            stage_counts = {
                row["current_stage"]: row["n"]
                for row in open_cases.values("current_stage").annotate(n=Count("id"))
            }
            context["case_stage_counts"] = [
                {"label": label, "count": stage_counts.get(value, 0)}
                for value, label in RecruitmentCase.Stage.choices
                if value != RecruitmentCase.Stage.CLOSED
            ]
            # Same per-stage targets as the case badge (Completion has two weeks).
            now = timezone.now()
            context["overdue_case_count"] = open_cases.filter(
                (
                    ~Q(current_stage=RecruitmentCase.Stage.COMPLETION)
                    & Q(stage_entered_at__lt=now - STAGE_SLA_OVERDUE_THRESHOLD)
                )
                | Q(
                    current_stage=RecruitmentCase.Stage.COMPLETION,
                    stage_entered_at__lt=now - COMPLETION_SLA_OVERDUE_THRESHOLD,
                )
            ).exclude(
                case_status=RecruitmentCase.CaseStatus.AWAITING_RESUBMISSION
            ).count()

            context["audit_week_summary"] = summarize_audit_logs(
                get_all_audit_logs(date_from=week_ago)
            )
            context["audit_week_start"] = week_ago
            week_events = AuditLog.objects.filter(created_at__date__gte=week_ago)
            context["security_event_count"] = week_events.filter(
                action__in=self.SECURITY_WATCH_ACTIONS
            ).count()
            context["sensitive_event_count"] = week_events.filter(
                is_sensitive_access=True
            ).count()
            context["audit_archive_count"] = AuditLogArchive.objects.count()
            context["latest_audit_archive"] = AuditLogArchive.objects.first()
        else:
            context["queue"] = get_queue_for_user(user)
        return context


class ForbiddenView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    template_name = "forbidden.html"


class SessionKeepAliveView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    """TI-1: "Stay signed in" from the idle warning, and the throttled ping sent while the user
    is typing or clicking on a page that makes no requests of its own. The request itself is
    what extends the session (SESSION_SAVE_EVERY_REQUEST); POST-only so it is CSRF-protected."""

    http_method_names = ["post"]

    def post(self, request):
        return JsonResponse(
            {"ok": True, "timeout_seconds": settings.SESSION_INACTIVITY_TIMEOUT_SECONDS}
        )


class PositionListView(LoginRequiredMixin, InternalUserRequiredMixin, ListView):
    template_name = "recruitment/position_list.html"
    context_object_name = "positions"

    def get(self, request, *args, **kwargs):
        if request.user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
            raise PermissionDenied
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return get_visible_positions_for_user(self.request.user)
