"""In-app notifications and the notification actions on a case (checklist, reminder,
resend).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from config.middleware import mark_passive_session_request
from ..forms import ReminderNotificationForm, RequirementChecklistNotificationForm
from ..models import Notification, NotificationLog, RecruitmentApplication
from ..notification_services import (
    NOTIFICATION_MANAGER_ROLES,
    get_recent_notifications,
    get_unread_count,
    mark_all_notifications_read,
    mark_notification_read,
    resend_notification,
    send_reminder_notification,
    send_requirement_checklist_notification,
    user_can_send_reminder_notification,
    user_can_send_requirement_checklist_notification,
)
from ..permissions import InternalUserRequiredMixin
from ..services import user_can_view_application
from .common import _safe_internal_redirect


class NotificationListView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request):
        notifications = list(get_recent_notifications(request.user, limit=100))
        unread_count = sum(1 for n in notifications if n.read_at is None)
        return render(
            request,
            "recruitment/notification_list.html",
            {
                "notifications": notifications,
                "unread_count": unread_count,
                "has_unread": unread_count > 0,
            },
        )


class NotificationReadView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        try:
            notification = mark_notification_read(pk, request.user)
        except Notification.DoesNotExist as exc:
            raise Http404 from exc
        fallback_url = reverse("notification-list")
        redirect_url = _safe_internal_redirect(
            request,
            request.POST.get("next")
            or notification.related_url
            or request.META.get("HTTP_REFERER"),
            fallback_url,
        )
        return redirect(redirect_url)


class NotificationReadAllView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request):
        mark_all_notifications_read(request.user)
        fallback_url = reverse("notification-list")
        redirect_url = _safe_internal_redirect(
            request,
            request.POST.get("next") or request.META.get("HTTP_REFERER"),
            fallback_url,
        )
        return redirect(redirect_url)


class NotificationUnreadCountView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def get(self, request):
        # TI-1: the bell polls this once a minute; a poll is not user activity, so it must
        # not restart the inactivity timeout.
        mark_passive_session_request(request)
        return JsonResponse({"count": get_unread_count(request.user)})


class RequirementChecklistNotificationView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and user_can_send_requirement_checklist_notification(request.user, application)
        ):
            raise PermissionDenied

        form = RequirementChecklistNotificationForm(request.POST)
        if form.is_valid():
            try:
                send_requirement_checklist_notification(
                    application=application,
                    actor=request.user,
                    additional_message=form.cleaned_data["additional_message"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    "Requirement checklist email queued for delivery.",
                )
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("application-detail", pk=pk)


class ReminderNotificationView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and user_can_send_reminder_notification(request.user, application)
        ):
            raise PermissionDenied

        form = ReminderNotificationForm(request.POST)
        if form.is_valid():
            try:
                send_reminder_notification(
                    application=application,
                    actor=request.user,
                    reminder_subject=form.cleaned_data["reminder_subject"],
                    reminder_message=form.cleaned_data["reminder_message"],
                    deadline=form.cleaned_data["deadline"],
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Reminder email queued for delivery.")
        else:
            messages.error(
                request,
                "; ".join(error for errors in form.errors.values() for error in errors),
            )
        return redirect("application-detail", pk=pk)


class NotificationResendView(LoginRequiredMixin, InternalUserRequiredMixin, View):
    """RD-9: re-deliver a previously queued applicant email, verbatim. The one
    recovery path for a FAILED send (and for a lost email) — nothing retries
    automatically, and re-saving a schedule deliberately doesn't re-notify."""

    def post(self, request, pk, log_id):
        application = get_object_or_404(RecruitmentApplication, pk=pk)
        if not (
            user_can_view_application(request.user, application)
            and request.user.role in NOTIFICATION_MANAGER_ROLES
        ):
            raise PermissionDenied
        notification_log = get_object_or_404(
            NotificationLog, pk=log_id, application=application
        )
        try:
            resend_notification(notification_log, request.user)
        except (ValueError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{notification_log.get_notification_type_display()} queued for re-delivery.",
            )
        return redirect("application-detail", pk=pk)
