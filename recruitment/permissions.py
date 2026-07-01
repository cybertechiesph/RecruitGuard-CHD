from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.conf import settings
from django.core.exceptions import PermissionDenied

from .models import RecruitmentUser


INTERNAL_MFA_VERIFIED_SESSION_KEY = "internal_mfa_verified"
INTERNAL_MFA_USER_SESSION_KEY = "internal_mfa_user_id"

WORKFLOW_PROCESSOR_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.HRMPSB_MEMBER,
    RecruitmentUser.Role.APPOINTING_AUTHORITY,
}
ENTRY_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
}


def is_internal_user(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_internal_user", False)
    )


def has_role(user, *roles):
    return is_internal_user(user) and user.role in set(roles)


def internal_mfa_is_verified(request):
    user = getattr(request, "user", None)
    if not getattr(settings, "INTERNAL_MFA_ENABLED", True):
        return is_internal_user(user)
    return bool(
        is_internal_user(user)
        and request.session.get(INTERNAL_MFA_VERIFIED_SESSION_KEY) is True
        and request.session.get(INTERNAL_MFA_USER_SESSION_KEY) == user.id
    )


class AuthzMixin(UserPassesTestMixin):
    raise_exception = True

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        if is_internal_user(self.request.user) and not internal_mfa_is_verified(self.request):
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        raise PermissionDenied


class InternalUserRequiredMixin(AuthzMixin):
    def test_func(self):
        return is_internal_user(self.request.user) and internal_mfa_is_verified(self.request)


class SystemAdministratorRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, RecruitmentUser.Role.SYSTEM_ADMIN) and internal_mfa_is_verified(self.request)


class WorkflowProcessorRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, *WORKFLOW_PROCESSOR_ROLES) and internal_mfa_is_verified(self.request)


class EntryManagerRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, *ENTRY_MANAGER_ROLES) and internal_mfa_is_verified(self.request)
