"""The dashboard, the forbidden page and the session keep-alive.
"""

from datetime import datetime, time

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from ..models import (
    AuditLog,
    AuditLogArchive,
    RecruitmentUser,
)
from ..permissions import InternalUserRequiredMixin
from ..services import get_internal_account_locks


class DashboardView(LoginRequiredMixin, InternalUserRequiredMixin, TemplateView):
    """Round F1: the System Administrator's Home — what needs attention, the common tasks,
    and recent account and security events (docs/admin-area-rebuild-plan.md §4.1). Workflow
    roles land on the vacancy batch console instead; that is their home."""

    template_name = "recruitment/admin/home.html"

    # Account changes and sign-in trouble; routine sign-ins and page views are left out.
    RECENT_ACTIONS = [
        AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
        AuditLog.Action.INTERNAL_ACCOUNT_UPDATED,
        AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED,
        AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
        AuditLog.Action.INTERNAL_ROLE_CHANGED,
        AuditLog.Action.INTERNAL_EMAIL_CHANGE_REQUESTED,
        AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED,
        AuditLog.Action.PASSWORD_RESET_SENT_BY_ADMIN,
        AuditLog.Action.INTERNAL_ACCOUNT_UNLOCKED,
        AuditLog.Action.PASSWORD_RESET_REQUESTED,
        AuditLog.Action.PASSWORD_RESET_COMPLETED,
        AuditLog.Action.PASSWORD_CHANGED,
        AuditLog.Action.INTERNAL_LOGIN_FAILED,
        AuditLog.Action.INTERNAL_LOGIN_LOCKED,
        AuditLog.Action.INTERNAL_MFA_FAILED,
        AuditLog.Action.INTERNAL_MFA_LOCKED,
        AuditLog.Action.EVIDENCE_ACCESS_DENIED,
    ]
    FAILED_SIGN_IN_ACTIONS = [
        AuditLog.Action.INTERNAL_LOGIN_FAILED,
        AuditLog.Action.INTERNAL_LOGIN_LOCKED,
    ]
    # An archive older than this is flagged (entries become archivable after six months).
    ARCHIVE_WARN_AFTER_DAYS = 180

    def get(self, request, *args, **kwargs):
        if request.user.role != RecruitmentUser.Role.SYSTEM_ADMIN:
            return redirect("vacancy-batches")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        internal_users = list(
            RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())
        )
        locks = get_internal_account_locks(internal_users)
        locked_count = sum(
            1 for user in internal_users if user.username.strip().lower() in locks
        )
        inactive_count = sum(1 for user in internal_users if not user.is_active)

        today = timezone.localdate()
        midnight = timezone.make_aware(datetime.combine(today, time.min))
        failed_today = AuditLog.objects.filter(
            action__in=self.FAILED_SIGN_IN_ACTIONS, created_at__gte=midnight
        ).count()

        latest_archive = AuditLogArchive.objects.order_by("-created_at").first()
        archive_age_days = (
            (timezone.now() - latest_archive.created_at).days if latest_archive else None
        )

        context.update(
            {
                "account_count": len(internal_users),
                "locked_count": locked_count,
                "inactive_count": inactive_count,
                "failed_today": failed_today,
                "today": today,
                "latest_archive": latest_archive,
                "archive_age_days": archive_age_days,
                "archive_overdue": archive_age_days is None
                or archive_age_days > self.ARCHIVE_WARN_AFTER_DAYS,
                "needs_attention": bool(locked_count or failed_today),
                "recent_events": AuditLog.objects.filter(action__in=self.RECENT_ACTIONS)
                .select_related("actor")
                .order_by("-created_at")[:10],
            }
        )
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

