from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    PasswordChangeDoneView,
    PasswordChangeView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.db.models import F, Q
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from .forms import (
    InternalAuthenticationForm,
    InternalMFAOTPForm,
    InternalPasswordChangeForm,
    InternalPasswordResetForm,
    InternalSetPasswordForm,
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
    clear_internal_login_failures,
    get_internal_account_locks,
    get_internal_login_lock,
    internal_account_locked_until,
    send_admin_password_email,
    unlock_internal_account,
    issue_internal_mfa_challenge,
    issue_internal_email_change_request,
    remember_internal_password_hash,
    record_internal_login_failure,
    record_internal_login_locked_attempt,
    record_system_audit_event,
    reserve_internal_password_reset_request,
    verify_internal_email_change_request,
    verify_internal_mfa_challenge,
)


PENDING_INTERNAL_MFA_USER_SESSION_KEY = "pending_internal_mfa_user_id"
PENDING_INTERNAL_MFA_CHALLENGE_SESSION_KEY = "pending_internal_mfa_challenge_token"
PENDING_INTERNAL_MFA_NEXT_SESSION_KEY = "pending_internal_mfa_next"
INTERNAL_LOGIN_LOCKED_MESSAGE = (
    "Too many failed sign-in attempts. Please wait before trying again."
)


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # TI-1: the idle-warning script signs the user out with ?reason=timeout so the sign-in
        # page can say why, instead of looking like an unexplained crash.
        context["signed_out_for_inactivity"] = self.request.GET.get("reason") == "timeout"
        context["inactivity_timeout_minutes"] = settings.SESSION_INACTIVITY_TIMEOUT_SECONDS // 60
        return context

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if get_internal_login_lock(username, request) is not None:
            record_internal_login_locked_attempt(username, request)
            form = self.get_form()
            form.add_error(None, INTERNAL_LOGIN_LOCKED_MESSAGE)
            return self.form_invalid(form)

        form = self.get_form()
        if form.is_valid():
            clear_internal_login_failures(username, request)
            return self.form_valid(form)

        record_internal_login_failure(username, request)
        return self.form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        if not getattr(settings, "INTERNAL_MFA_ENABLED", True):
            auth_login(self.request, user)
            _clear_pending_internal_mfa(self.request.session)
            self.request.session[INTERNAL_MFA_VERIFIED_SESSION_KEY] = True
            self.request.session[INTERNAL_MFA_USER_SESSION_KEY] = user.id
            messages.success(self.request, "Signed in.")
            return redirect(self.get_success_url())

        try:
            challenge = issue_internal_mfa_challenge(
                user,
                self.request,
                enforce_cooldown=True,
            )
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
        old_password_hash = self.request.user.password
        response = super().form_valid(form)
        remember_internal_password_hash(self.request.user, old_password_hash)
        remember_internal_password_hash(self.request.user)
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


class InternalPasswordResetView(PasswordResetView):
    template_name = "registration/password_reset_form.html"
    form_class = InternalPasswordResetForm
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("password-reset-done")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        can_send = reserve_internal_password_reset_request(email, self.request)
        users = list(form.get_users(email)) if can_send else []
        if not can_send:
            return redirect(self.get_success_url())

        response = super().form_valid(form)
        for user in users:
            record_system_audit_event(
                actor=user,
                action=AuditLog.Action.PASSWORD_RESET_REQUESTED,
                description="Internal password reset requested.",
                metadata={"user_id": user.id},
            )
        return response


class InternalPasswordResetDoneView(PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


class InternalPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    form_class = InternalSetPasswordForm
    success_url = reverse_lazy("password-reset-complete")

    def form_valid(self, form):
        old_password_hash = self.user.password
        response = super().form_valid(form)
        self.user.refresh_from_db()
        remember_internal_password_hash(self.user, old_password_hash)
        remember_internal_password_hash(self.user)
        record_system_audit_event(
            actor=self.user,
            action=AuditLog.Action.PASSWORD_RESET_COMPLETED,
            description="Internal password reset completed.",
            metadata={"user_id": self.user.id},
        )
        return response


class InternalPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"


class InternalUserListView(LoginRequiredMixin, SystemAdministratorRequiredMixin, ListView):
    """Round F1: Accounts — one toolbar (search, role, status) and one table
    (docs/admin-area-rebuild-plan.md section 4.2)."""

    template_name = "recruitment/admin/accounts.html"
    context_object_name = "accounts"
    paginate_by = 50
    STATUS_CHOICES = (("active", "Active"), ("inactive", "Inactive"), ("locked", "Locked"))

    def _all_internal(self):
        return RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())

    def get_queryset(self):
        everyone = list(self._all_internal())
        self.locks = get_internal_account_locks(everyone)
        self.locked_ids = [u.pk for u in everyone if u.username.strip().lower() in self.locks]
        self.everyone = everyone

        queryset = self._all_internal()
        self.q = (self.request.GET.get("q") or "").strip()
        if self.q:
            queryset = queryset.filter(
                Q(username__icontains=self.q)
                | Q(first_name__icontains=self.q)
                | Q(last_name__icontains=self.q)
                | Q(email__icontains=self.q)
            )
        self.role = self.request.GET.get("role") or ""
        if self.role in RecruitmentUser.internal_roles():
            queryset = queryset.filter(role=self.role)
        else:
            self.role = ""
        self.status = self.request.GET.get("status") or ""
        if self.status == "active":
            queryset = queryset.filter(is_active=True)
        elif self.status == "inactive":
            queryset = queryset.filter(is_active=False)
        elif self.status == "locked":
            queryset = queryset.filter(pk__in=self.locked_ids)
        else:
            self.status = ""
        self.sort = "last_sign_in" if self.request.GET.get("sort") == "last_sign_in" else "name"
        if self.sort == "last_sign_in":
            return queryset.order_by(F("last_login").desc(nulls_last=True), "last_name", "username")
        return queryset.order_by("last_name", "first_name", "username")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for account in context["accounts"]:
            account.locked_until = self.locks.get(account.username.strip().lower())
        everyone = self.everyone
        role_counts = {}
        for account in everyone:
            role_counts[account.role] = role_counts.get(account.role, 0) + 1
        context.update(
            {
                "q": self.q,
                "role": self.role,
                "status": self.status,
                "sort": self.sort,
                "is_filtered": bool(self.q or self.role or self.status),
                "total_count": len(everyone),
                "active_count": sum(1 for a in everyone if a.is_active),
                "inactive_count": sum(1 for a in everyone if not a.is_active),
                "locked_count": len(self.locked_ids),
                "role_chips": [
                    {"value": value, "label": label, "count": role_counts.get(value, 0)}
                    for value, label in RecruitmentUser.Role.choices
                    if value in RecruitmentUser.internal_roles()
                ],
                "status_chips": self.STATUS_CHOICES,
            }
        )
        return context


class InternalUserDetailView(LoginRequiredMixin, SystemAdministratorRequiredMixin, DetailView):
    """Round F1: one person's account — details, access actions (deactivate, password link,
    unlock) and their recent activity (section 4.3)."""

    template_name = "recruitment/admin/account_detail.html"
    context_object_name = "account"
    queryset = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context["locked_until"] = internal_account_locked_until(account)
        context["is_self"] = account == self.request.user
        context["has_password"] = account.has_usable_password()
        context["recent_activity"] = (
            AuditLog.objects.filter(actor=account).select_related("application").order_by("-created_at")[:20]
        )
        return context


class InternalUserSendResetView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """Round F1: email a colleague a one-time link to set a new password."""

    def post(self, request, pk):
        account = get_object_or_404(
            RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()), pk=pk
        )
        if account == request.user:
            messages.error(request, "Use Change password for your own account.")
            return redirect("internal-user-detail", pk=account.pk)
        try:
            send_admin_password_email(account, request.user, request)
        except ValueError as exc:
            messages.error(request, str(exc))
        except Exception:
            messages.error(request, "The email could not be sent right now. Try again in a few minutes.")
        else:
            messages.success(request, f"A password reset link was emailed to {account.email}.")
        return redirect("internal-user-detail", pk=account.pk)


class InternalUserUnlockView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    """Round F1: lift a sign-in lock so the person can try again now."""

    def post(self, request, pk):
        account = get_object_or_404(
            RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()), pk=pk
        )
        if unlock_internal_account(account, request.user):
            name = account.get_full_name() or account.username
            messages.success(request, f"{name} can sign in again.")
        else:
            messages.info(request, "This account was not locked.")
        return redirect("internal-user-detail", pk=account.pk)


class InternalUserCreateView(LoginRequiredMixin, SystemAdministratorRequiredMixin, CreateView):
    form_class = InternalUserCreateForm
    model = RecruitmentUser
    template_name = "recruitment/admin/account_form.html"

    def form_valid(self, form):
        invited = form.chosen_password_mode == InternalUserCreateForm.PASSWORD_MODE_INVITE
        response = super().form_valid(form)
        if not invited:
            remember_internal_password_hash(self.object)
        record_system_audit_event(
            actor=self.request.user,
            action=AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
            description=f"Created internal account '{self.object.username}'.",
            metadata={
                "target_user_id": self.object.id,
                "target_username": self.object.username,
                "role": self.object.role,
                "is_active": self.object.is_active,
                "invited": invited,
            },
        )
        if invited:
            try:
                send_admin_password_email(self.object, self.request.user, self.request, invitation=True)
            except Exception:
                messages.warning(
                    self.request,
                    "Account created, but the invitation email could not be sent. "
                    "Use Send password reset email on this page to try again.",
                )
            else:
                messages.success(
                    self.request,
                    f"Account created. An invitation was emailed to {self.object.email}.",
                )
        else:
            messages.success(
                self.request,
                "Account created. Give them the temporary password in person; "
                "they can change it under Change password.",
            )
        return response

    def get_success_url(self):
        return reverse("internal-user-detail", kwargs={"pk": self.object.pk})


class InternalUserUpdateView(LoginRequiredMixin, SystemAdministratorRequiredMixin, UpdateView):
    form_class = InternalUserUpdateForm
    model = RecruitmentUser
    template_name = "recruitment/admin/account_form.html"
    queryset = RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles())

    def form_valid(self, form):
        target_snapshot = RecruitmentUser.objects.get(pk=form.instance.pk)
        if target_snapshot == self.request.user:
            new_role = form.cleaned_data["role"]
            new_is_active = form.cleaned_data["is_active"]
            if new_role != RecruitmentUser.Role.SYSTEM_ADMIN:
                form.add_error("role", "You cannot remove your own System Administrator role.")
            if not new_is_active:
                form.add_error("is_active", "You cannot deactivate your own account.")
            if form.errors:
                return self.form_invalid(form)

        previous_role = target_snapshot.role
        previous_is_active = target_snapshot.is_active
        requested_email = form.cleaned_data["email"].strip().lower()
        email_changed = target_snapshot.email.lower() != requested_email
        if email_changed:
            form.instance.email = target_snapshot.email
        response = super().form_valid(form)
        if email_changed:
            try:
                issue_internal_email_change_request(
                    self.object,
                    self.request.user,
                    requested_email,
                    request=self.request,
                )
            except Exception as exc:
                messages.error(
                    self.request,
                    "The account was saved, but the confirmation email to the new address "
                    "could not be sent. Try again in a few minutes.",
                )
            else:
                messages.info(
                    self.request,
                    "We emailed a confirmation link to the new address. The email changes "
                    "once they click it.",
                )
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
        messages.success(self.request, "Account updated.")
        return response

    def get_success_url(self):
        return reverse("internal-user-detail", kwargs={"pk": self.object.pk})


class InternalUserToggleActiveView(LoginRequiredMixin, SystemAdministratorRequiredMixin, View):
    def post(self, request, pk):
        user = get_object_or_404(
            RecruitmentUser.objects.filter(role__in=RecruitmentUser.internal_roles()),
            pk=pk,
        )
        back_to_detail = request.POST.get("next") == "detail"
        if user == request.user:
            messages.error(request, "You cannot deactivate your own account. Ask another System Administrator to do this.")
            if back_to_detail:
                return redirect("internal-user-detail", pk=user.pk)
            return redirect("internal-user-list")
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
        name = user.get_full_name() or user.username
        messages.success(
            request,
            f"{name} can sign in again." if user.is_active else f"{name} can no longer sign in.",
        )
        if back_to_detail:
            return redirect("internal-user-detail", pk=user.pk)
        return redirect("internal-user-list")


class InternalEmailChangeVerifyView(View):
    def get(self, request, token):
        try:
            verify_internal_email_change_request(token, request=request)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Email address confirmed. You can sign in with it now.")
        return redirect("login")
