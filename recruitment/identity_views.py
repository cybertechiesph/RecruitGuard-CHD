from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, PasswordChangeDoneView, PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, FormView, ListView, UpdateView

from .forms import (
    InternalAuthenticationForm,
    InternalMFAOTPForm,
    InternalPasswordChangeForm,
    InternalUserCreateForm,
    InternalUserUpdateForm,
)
from .models import AuditLog, RecruitmentUser
from .permissions import (
    INTERNAL_MFA_USER_SESSION_KEY,
    INTERNAL_MFA_VERIFIED_SESSION_KEY,
    InternalUserRequiredMixin,
    SystemAdministratorRequiredMixin,
    internal_mfa_is_verified,
)
from .services import (
    issue_internal_mfa_challenge,
    record_system_audit_event,
    verify_internal_mfa_challenge,
)


PENDING_INTERNAL_MFA_USER_SESSION_KEY = "pending_internal_mfa_user_id"
PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY = "pending_internal_mfa_challenge_token"
PENDING_INTERNAL_MFA_NEXT_SESSION_KEY = "pending_internal_mfa_next"


def _clear_pending_internal_mfa(session):
    session.pop(PENDING_INTERNAL_MFA_USER_SESSION_KEY, None)
    session.pop(PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY, None)
    session.pop(PENDING_INTERNAL_MFA_NEXT_SESSION_KEY, None)


class InternalLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = InternalAuthenticationForm
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if (
            request.user.is_authenticated
            and getattr(request.user, "is_internal_user", False)
            and not internal_mfa_is_verified(request)
        ):
            auth_logout(request)
            messages.info(request, "Please sign in again to complete internal verification.")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        try:
            challenge = issue_internal_mfa_challenge(user, self.request)
        except ValueError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        self.request.session.cycle_key()
        self.request.session[PENDING_INTERNAL_MFA_USER_SESSION_KEY] = user.id
        self.request.session[PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY] = str(
            challenge.challenge_token
        )
        self.request.session[PENDING_INTERNAL_MFA_NEXT_SESSION_KEY] = self.get_success_url()
        messages.success(
            self.request,
            "A verification code has been sent to your registered email address.",
        )
        return redirect("internal-mfa-verify")


class InternalMFAVerifyView(FormView):
    template_name = "registration/internal_mfa_verify.html"
    form_class = InternalMFAOTPForm

    def get_pending_user(self):
        user_id = self.request.session.get(PENDING_INTERNAL_MFA_USER_SESSION_KEY)
        challenge_token = self.request.session.get(PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY)
        if not user_id or not challenge_token:
            return None
        return RecruitmentUser.objects.filter(
            pk=user_id,
            is_active=True,
            role__in=RecruitmentUser.internal_roles(),
        ).first()

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and internal_mfa_is_verified(request):
            return redirect("dashboard")
        self.pending_user = self.get_pending_user()
        if self.pending_user is None:
            messages.error(request, "Sign in with your internal credentials before entering a verification code.")
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pending_user"] = self.pending_user
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "resend":
            try:
                challenge = issue_internal_mfa_challenge(
                    self.pending_user,
                    request,
                    is_resend=True,
                    enforce_cooldown=True,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                request.session[PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY] = str(
                    challenge.challenge_token
                )
                messages.success(request, "A new verification code has been sent.")
            return redirect("internal-mfa-verify")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        challenge_token = self.request.session.get(PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY)
        try:
            verify_internal_mfa_challenge(
                self.pending_user,
                challenge_token,
                form.cleaned_data["otp"],
                request=self.request,
            )
        except ValueError as exc:
            form.add_error("otp", str(exc))
            return self.form_invalid(form)

        next_url = self.request.session.get(PENDING_INTERNAL_MFA_NEXT_SESSION_KEY) or reverse("dashboard")
        auth_login(self.request, self.pending_user)
        _clear_pending_internal_mfa(self.request.session)
        self.request.session[INTERNAL_MFA_VERIFIED_SESSION_KEY] = True
        self.request.session[INTERNAL_MFA_USER_SESSION_KEY] = self.pending_user.id
        messages.success(self.request, "Internal verification completed.")
        return redirect(next_url)


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
