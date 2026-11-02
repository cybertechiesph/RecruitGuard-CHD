"""Internal accounts and sessions."""

import re
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AuditLog,
    InternalEmailChangeRequest,
    InternalPasswordHistory,
    RecruitmentUser,
)
from ..services import submit_application
from .base import User, BaseRecruitmentTestCase


class IdentityAdministrationTests(BaseRecruitmentTestCase):
    def test_internal_user_create_form_exposes_password_strength_meter(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("internal-user-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-password-strength="true"')

    def test_django_admin_requires_true_superuser(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get("/admin/")

        self.assertNotEqual(response.status_code, 200)

        superuser = User.objects.create_superuser(
            username="root-admin",
            password="testpass123",
            email="root-admin@example.com",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        client.force_login(superuser)

        response = client.get("/admin/")

        self.assertEqual(response.status_code, 200)

    def test_internal_email_change_requires_new_email_verification(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.post(
            reverse("internal-user-update", kwargs={"pk": self.secretariat.pk}),
            {
                "username": self.secretariat.username,
                "first_name": self.secretariat.first_name,
                "last_name": self.secretariat.last_name,
                "email": "new-secretariat@example.com",
                "employee_id": self.secretariat.employee_id,
                "office_name": self.secretariat.office_name,
                "role": self.secretariat.role,
                "is_active": "on",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-user-list"))
        self.secretariat.refresh_from_db()
        self.assertEqual(self.secretariat.email, "secretariat@example.com")
        change_request = InternalEmailChangeRequest.objects.get(user=self.secretariat)
        self.assertEqual(change_request.new_email, "new-secretariat@example.com")
        self.assertEqual(len(mail.outbox), 1)
        verify_path = re.search(
            r"http://testserver(?P<path>/internal/users/email-change/[^\s]+/verify/)",
            mail.outbox[0].body,
        ).group("path")

        response = client.get(verify_path)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("login"))
        self.secretariat.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(self.secretariat.email, "new-secretariat@example.com")
        self.assertTrue(change_request.is_verified)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_EMAIL_CHANGE_VERIFIED).exists()
        )

    def test_password_history_blocks_recent_password_reuse(self):
        self.secretariat.set_password("OriginalSecurePass123!")
        self.secretariat.save(update_fields=["password"])
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("password-change"),
            {
                "old_password": "OriginalSecurePass123!",
                "new_password1": "DifferentSecurePass123!",
                "new_password2": "DifferentSecurePass123!",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-change-done"))
        self.secretariat.refresh_from_db()
        self.assertTrue(self.secretariat.check_password("DifferentSecurePass123!"))
        self.assertGreaterEqual(
            InternalPasswordHistory.objects.filter(user=self.secretariat).count(),
            2,
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("password-change"),
            {
                "old_password": "DifferentSecurePass123!",
                "new_password1": "OriginalSecurePass123!",
                "new_password2": "OriginalSecurePass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not been used recently")

    def test_system_admin_can_create_internal_account(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.post(
            reverse("internal-user-create"),
            {
                "username": "chief-two",
                "first_name": "Chief",
                "last_name": "Two",
                "email": "chief.two@example.com",
                "employee_id": "EMP-002",
                "office_name": "HR Office",
                "role": RecruitmentUser.Role.HRM_CHIEF,
                "is_active": "on",
                "password1": "VeryStrongPass123",
                "password2": "VeryStrongPass123",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(username="chief-two")
        self.assertEqual(created_user.role, RecruitmentUser.Role.HRM_CHIEF)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_CREATED,
                metadata__target_username="chief-two",
            ).exists()
        )

    def test_non_admin_cannot_access_internal_user_directory(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("internal-user-list"))
        self.assertEqual(response.status_code, 403)

    def test_system_admin_can_update_role_and_activation_with_audit(self):
        managed_user = User.objects.create_user(
            username="member-one",
            password="testpass123",
            role=RecruitmentUser.Role.HRMPSB_MEMBER,
            email="member.one@example.com",
        )
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.post(
            reverse("internal-user-update", kwargs={"pk": managed_user.pk}),
            {
                "username": "member-one",
                "first_name": "Member",
                "last_name": "One",
                "email": "member.one@example.com",
                "employee_id": "EMP-019",
                "office_name": "Board Office",
                "role": RecruitmentUser.Role.APPOINTING_AUTHORITY,
                "is_active": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        managed_user.refresh_from_db()
        self.assertEqual(managed_user.role, RecruitmentUser.Role.APPOINTING_AUTHORITY)
        self.assertFalse(managed_user.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ROLE_CHANGED,
                metadata__target_username="member-one",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.INTERNAL_ACCOUNT_DEACTIVATED,
                metadata__target_username="member-one",
            ).exists()
        )

    def test_system_admin_cannot_view_case_content_by_default(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))
        self.assertEqual(response.status_code, 404)

    def test_system_admin_role_does_not_inherit_django_admin_access(self):
        self.sysadmin.refresh_from_db()
        self.assertFalse(self.sysadmin.is_staff)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_system_admin_self_deactivation_redirects_with_message(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.post(
            reverse("internal-user-toggle-active", kwargs={"pk": self.sysadmin.pk}),
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-user-list"))
        self.sysadmin.refresh_from_db()
        self.assertTrue(self.sysadmin.is_active)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("your own account" in m for m in messages))

    def test_system_admin_self_role_change_shows_form_error(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.post(
            reverse("internal-user-update", kwargs={"pk": self.sysadmin.pk}),
            {
                "username": self.sysadmin.username,
                "first_name": self.sysadmin.first_name,
                "last_name": self.sysadmin.last_name,
                "email": self.sysadmin.email,
                "employee_id": self.sysadmin.employee_id,
                "office_name": self.sysadmin.office_name,
                "role": RecruitmentUser.Role.SECRETARIAT,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "role", "You cannot remove your own System Administrator role.")
        self.sysadmin.refresh_from_db()
        self.assertEqual(self.sysadmin.role, RecruitmentUser.Role.SYSTEM_ADMIN)

    def test_system_admin_cannot_open_position_list(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("position-list"))

        self.assertEqual(response.status_code, 403)


class SessionStabilityTests(BaseRecruitmentTestCase):
    """TI-1: background polls must not keep a session alive, the keep-alive endpoint must, and
    an expired session must never look like a successful autosave."""

    def _session_row(self, client):
        from django.contrib.sessions.models import Session

        return Session.objects.get(session_key=client.session.session_key)

    def _shorten_expiry(self, client, seconds=60):
        row = self._session_row(client)
        row.expire_date = timezone.now() + timedelta(seconds=seconds)
        row.save(update_fields=["expire_date"])
        return row.expire_date

    def test_notification_poll_does_not_extend_the_session(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        shortened = self._shorten_expiry(client)

        response = client.get(reverse("notification-unread-count"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._session_row(client).expire_date, shortened)

    def test_a_normal_page_request_still_extends_the_session(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        shortened = self._shorten_expiry(client)

        client.get(reverse("vacancy-batches"))

        self.assertGreater(
            self._session_row(client).expire_date,
            shortened + timedelta(seconds=settings.SESSION_INACTIVITY_TIMEOUT_SECONDS - 120),
        )

    def test_keep_alive_extends_the_session_for_internal_users(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        shortened = self._shorten_expiry(client)

        response = client.post(reverse("session-keep-alive"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ok": True, "timeout_seconds": settings.SESSION_INACTIVITY_TIMEOUT_SECONDS},
        )
        self.assertGreater(self._session_row(client).expire_date, shortened)

    def test_keep_alive_rejects_get(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        self.assertEqual(client.get(reverse("session-keep-alive")).status_code, 405)

    def test_keep_alive_requires_a_signed_in_internal_user(self):
        response = Client().post(reverse("session-keep-alive"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        applicant_client = Client()
        applicant_client.force_login(self.applicant)
        self.assertEqual(applicant_client.post(reverse("session-keep-alive")).status_code, 403)

    def test_keep_alive_is_csrf_protected(self):
        client = Client(enforce_csrf_checks=True)
        self.force_login_with_mfa(client, self.secretariat)
        self.assertEqual(client.post(reverse("session-keep-alive")).status_code, 403)

    def test_autosave_from_an_expired_session_is_not_a_success(self):
        application = self.make_application(self.level1_position)
        response = Client().post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {"operation": "save"},
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )
        # The autosave script treats only 204 as saved; a sign-in redirect must not be one.
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_internal_pages_render_the_idle_warning_with_the_configured_timeout(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batches"))
        self.assertContains(response, "data-rg-session")
        self.assertContains(
            response, f'data-timeout-seconds="{settings.SESSION_INACTIVITY_TIMEOUT_SECONDS}"'
        )
        self.assertContains(response, reverse("session-keep-alive"))
        self.assertContains(response, "Stay signed in")

    def test_login_page_explains_an_inactivity_sign_out(self):
        response = Client().get(reverse("login") + "?reason=timeout&next=/internal/queue/")
        self.assertContains(response, "data-inactivity-signout")
        self.assertContains(response, "minutes of inactivity")
        self.assertNotContains(Client().get(reverse("login")), "data-inactivity-signout")
