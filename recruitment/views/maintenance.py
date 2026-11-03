"""Round F3: the administrator's housekeeping in one place — audit archives, exports for a
date range, and failed email deliveries (docs/admin-area-rebuild-plan.md section 4.8).
"""

from datetime import date, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils import timezone
from django.views.generic import TemplateView

from ..models import AuditLogArchive, NotificationLog
from ..permissions import SystemAdministratorRequiredMixin
from ..services import audit_archive_latest_allowed_cutoff, preview_audit_archive


class MaintenanceView(LoginRequiredMixin, SystemAdministratorRequiredMixin, TemplateView):
    template_name = "recruitment/admin/maintenance.html"
    FAILED_EMAIL_WINDOW_DAYS = 7

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        latest_cutoff = audit_archive_latest_allowed_cutoff()
        context["audit_archives"] = AuditLogArchive.objects.select_related("created_by").order_by("-created_at")[:20]
        context["archive_latest_cutoff"] = latest_cutoff
        cutoff_raw = (self.request.GET.get("archive_cutoff") or "").strip()
        if cutoff_raw:
            try:
                cutoff = date.fromisoformat(cutoff_raw)
            except ValueError:
                cutoff = None
            if cutoff and cutoff <= latest_cutoff:
                context["archive_preview"] = preview_audit_archive(cutoff)
                context["archive_preview_cutoff"] = cutoff
            elif cutoff:
                context["archive_preview_error"] = (
                    "Only entries older than six months can be archived. Choose a date "
                    f"on or before {latest_cutoff:%b %d, %Y}."
                )
            else:
                context["archive_preview_error"] = "Choose a valid date."

        today = timezone.localdate()
        window_start = today - timedelta(days=self.FAILED_EMAIL_WINDOW_DAYS - 1)
        context["failed_email_count"] = NotificationLog.objects.filter(
            delivery_status=NotificationLog.DeliveryStatus.FAILED,
            created_at__gte=timezone.now() - timedelta(days=self.FAILED_EMAIL_WINDOW_DAYS),
        ).count()
        context["failed_email_window_days"] = self.FAILED_EMAIL_WINDOW_DAYS
        context["failed_email_from"] = window_start
        context["today"] = today
        context["export_default_from"] = today - timedelta(days=29)
        return context
