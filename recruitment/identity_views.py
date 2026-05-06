from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, PasswordChangeDoneView, PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from .forms import (
    InternalAuthenticationForm,
    InternalPasswordChangeForm,
    InternalUserCreateForm,
    InternalUserUpdateForm,
)
from .models import AuditLog, RecruitmentUser
from .permissions import InternalUserRequiredMixin, SystemAdministratorRequiredMixin
from .services import record_system_audit_event


class InternalLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = InternalAuthenticationForm
    redirect_authenticated_user = True


class InternalPasswordChangeView(LoginRequiredMixin, InternalUserRequiredMixin, PasswordChangeView):
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password-change-done")
    form_class = InternalPasswordChangeForm

    def form_valid(self, form):
        response = super().form_valid(form)
        record_system_audit_event(
            actor=self.request.user,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="Internal user changed their password.",
            metadata={"user_id": self.request.user.id},
        )
        messages.success(self.request, "Password updated.")
        return response


class InternalPasswordChangeDoneView(LoginRequiredMixin, InternalUserRequiredMixin, PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"


class InternalUserListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, ListView):
    template_name = "recruitment/internal_user_list.html"
    context_object_name = "internal_users"

    def get_queryset(self):
        return RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()).order_by(
            "role",
            "last_name",
            "first_name",
            "username",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = context["internal_users"]
        context["active_internal_users"] = queryset.filter(is_active=True).count()
        return context


class InternalUserCreateView(LoginRequiredMixin, SystemAdministratorRequiredMixin, CreateView):
    form_class = InternalUserCreateForm
    model = RecruitmentUser
    template_name = "recruitment/internal_user_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        record_system_audit_event(
            actor=self.request.user,
            action=AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
            description=f"Created internal account '{self.object.username}'.",
            metadata={
                "target_user_id": self.object.id,
                "target_username": self.object.username,
                "role": self.object.role,
                "is_active": self.object.is_active,
            },
        )
        messages.success(self.request, "Internal user account created.")
        return response

    def get_success_url(self):
        return reverse("internal-user-list")


class InternalUserUpdateView(LoginRequiredMixin, SystemAdministratorRequiredMixin, UpdateView):
    form_class = InternalUserUpdateForm
    model = RecruitmentUser
    template_name = "recruitment/internal_user_form.html"
    queryset = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())

    def form_valid(self, form):
        original_user = self.get_object()
        if original_user == self.request.user:
            new_role = form.cleaned_data["role"]
            new_is_active = form.cleaned_data["is_active"]
            if new_role != RecruitmentUser.Role.SYSTEM_ADMIN:
                raise PermissionDenied("System Administrator cannot remove their own role.")
            if not new_is_active:
                raise PermissionDenied("System Administrator cannot deactivate their own account.")

        previous_role = original_user.role
        previous_is_active = original_user.is_active
        response = super().form_valid(form)
        record_system_audit_event(
            actor=self.request.user,
            action=AuditLog.Action.INTERNAL_ACCOUNT_UPDATED,
            description=f"Updated internal account '{self.object.username}'.",
            metadata={
                "target_user_id": self.object.id,
                "target_username": self.object.username,
                "changed_fields": form.changed_data,
            },
        )
        if previous_role != self.object.role:
            record_system_audit_event(
                actor=self.request.user,
                action=AuditLog.Action.INTERNAL_ROLE_CHANGED,
                description=f"Changed role for '{self.object.username}' from {previous_role} to {self.object.role}.",
                metadata={
                    "target_user_id": self.object.id,
                    "target_username": self.object.username,
                    "old_role": previous_role,
                    "new_role": self.object.role,
                },
            )
        if previous_is_active != self.object.is_active:
            action = (
                AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED
                if self.object.is_active
                else AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED
            )
            description = (
                f"Activated internal account '{self.object.username}'."
                if self.object.is_active
                else f"Deactivated internal account '{self.object.username}'."
            )
            record_system_audit_event(
                actor=self.request.user,
                action=action,
                description=description,
                metadata={
                    "target_user_id": self.object.id,
                    "target_username": self.object.username,
                },
            )
        messages.success(self.request, "Internal user account updated.")
        return response

    def get_success_url(self):
        return reverse("internal-user-list")


class InternalUserToggleActiveView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk):
        user = get_object_or_404(
            RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()),
            pk=pk,
        )
        if user == request.user:
            raise PermissionDenied("System Administrator cannot deactivate their own account.")
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])
        action = (
            AuditLog.Action.INTERNAL_ACCOUNT_ACTIVATED
            if user.is_active
            else AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED
        )
        description = (
            f"Activated internal account '{user.username}'."
            if user.is_active
            else f"Deactivated internal account '{user.username}'."
        )
        record_system_audit_event(
            actor=request.user,
            action=action,
            description=description,
            metadata={"target_user_id": user.id, "target_username": user.username},
        )
        messages.success(request, description)
        return redirect("internal-user-list")
