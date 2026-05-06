from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import RecruitmentUser


WORKFLOW_PROCESSOR_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.HRMPSB_MEMBER,
    RecruitmentUser.Role.APPOINTING_AUTHORITY,
}
ENTRY_MANAGER_ROLES = {
    RecruitmentUser.Role.SECRETARIAT,
    RecruitmentUser.Role.HRM_CHIEF,
    RecruitmentUser.Role.SYSTEM_ADMIN,
}


def is_internal_user(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_internal_user", False)
    )


def has_role(user, *roles):
    return is_internal_user(user) and user.role in set(roles)


class AuthzMixin(UserPassesTestMixin):
    raise_exception = True

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        raise PermissionDenied


class InternalUserRequiredMixin(AuthzMixin):
    def test_func(self):
        return is_internal_user(self.request.user)


class SystemAdministratorRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, RecruitmentUser.Role.SYSTEM_ADMIN)


class WorkflowProcessorRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, *WORKFLOW_PROCESSOR_ROLES)


class EntryManagerRequiredMixin(AuthzMixin):
    def test_func(self):
        return has_role(self.request.user, *ENTRY_MANAGER_ROLES)
