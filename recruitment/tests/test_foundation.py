"""Smoke tests across the whole app."""

import re
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AuditLog,
    InternalLoginAttempt,
    InternalMFAChallenge,
    InternalPasswordHistory,
    RecruitmentUser,
)
from ..permissions import (
    INTERNAL_MFA_USER_SESSION_KEY,
    INTERNAL_MFA_VERIFIED_SESSION_KEY,
)
from .base import User


@override_settings(
    CAPTCHA_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class FoundationSmokeTests(TestCase):
    def extract_internal_mfa_code(self):
        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internal Access")
        # The portal presents as the office's system, not the study title.
        self.assertContains(response, "DOH–CHD CALABARZON")
        self.assertTrue(reverse("login").startswith("/internal/"))

    def test_dashboard_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_root_redirects_to_public_applicant_portal(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("applicant-portal"))

    def test_public_status_lookup_page_loads_without_login(self):
        response = self.client.get(reverse("applicant-status-lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(reverse("applicant-status-lookup").startswith("/apply/"))

    def test_workflow_queue_redirects_anonymous_users_to_login(self):
        response = self.client.get(reverse("workflow-queue"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_security_headers_are_applied(self):
        response = self.client.get(reverse("login"))

        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertIn("camera=()", response["Permissions-Policy"])
        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_internal_user_can_log_in(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))
        self.assertContains(response, "Check Your Email")
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )
        challenge = InternalMFAChallenge.objects.get(user=user)
        otp_code = self.extract_internal_mfa_code()
        self.assertNotEqual(challenge.otp_hash, otp_code)
        self.assertEqual(len(mail.outbox[-1].alternatives), 1)
        html_body, mime_type = mail.outbox[-1].alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Internal Verification Code", html_body)
        self.assertIn("DOH–CHD CALABARZON", html_body)
        self.assertIn(otp_code, html_body)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": otp_code},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_VERIFIED).exists()
        )

    @override_settings(INTERNAL_MFA_ENABLED=False)
    def test_internal_login_can_bypass_mfa_when_disabled(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
        self.assertFalse(InternalMFAChallenge.objects.filter(user=user).exists())
        self.assertEqual(mail.outbox, [])
        self.assertTrue(self.client.session[INTERNAL_MFA_VERIFIED_SESSION_KEY])
        self.assertEqual(self.client.session[INTERNAL_MFA_USER_SESSION_KEY], user.id)

    def test_internal_mfa_email_falls_back_to_text_when_html_render_fails(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        # Simulate a missing/broken "email/internal_mfa_otp.html" template. The
        # MFA code email is sent synchronously inside an atomic block, so a render
        # failure must not roll back the challenge or block staff login; it should
        # degrade to text-only delivery (the code lives in the plain-text body).
        with patch(
            "recruitment.services.identity.render_to_string",
            side_effect=RuntimeError("template render boom"),
        ), self.assertLogs("recruitment.services", level="ERROR") as logs:
            response = self.client.post(
                reverse("login"),
                {"username": "secretariat", "password": "testpass123"},
                follow=True,
            )

        # Login still advances to the MFA verification step.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))

        # The challenge row survived (the atomic block did not roll back)...
        challenge = InternalMFAChallenge.objects.get(user=user)
        # ...and the code email went out, degraded to text-only.
        otp_code = self.extract_internal_mfa_code()
        self.assertNotEqual(challenge.otp_hash, otp_code)
        self.assertEqual(mail.outbox[-1].alternatives, [])
        self.assertTrue(
            any(
                "Failed to render internal MFA HTML email" in message
                for message in logs.output
            )
        )

    def test_internal_mfa_login_rotates_session_key_and_marks_session_verified(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        session = self.client.session
        session["pre_login_marker"] = "before-mfa"
        session.save()
        initial_session_key = session.session_key

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        pending_session_key = self.client.session.session_key
        self.assertNotEqual(initial_session_key, pending_session_key)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": self.extract_internal_mfa_code()},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("vacancy-batches"))
        verified_session = self.client.session
        self.assertNotEqual(initial_session_key, verified_session.session_key)
        self.assertTrue(verified_session[INTERNAL_MFA_VERIFIED_SESSION_KEY])
        self.assertEqual(verified_session[INTERNAL_MFA_USER_SESSION_KEY], user.id)

    def test_internal_login_requires_registered_email_for_mfa(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have an email address")
        self.assertFalse(InternalMFAChallenge.objects.exists())
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_FAILED).exists()
        )

    def test_internal_page_requires_completed_mfa(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_session_inactivity_timeout_is_configured(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 30 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_internal_login_challenge_respects_cooldown(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)

        challenge.requested_at = timezone.now() - timedelta(
            seconds=settings.INTERNAL_MFA_RESEND_COOLDOWN_SECONDS + 1
        )
        challenge.save(update_fields=["requested_at", "updated_at"])

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 2)

    def test_internal_mfa_resend_respects_cooldown(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)

        challenge.requested_at = timezone.now() - timedelta(
            seconds=settings.INTERNAL_MFA_RESEND_COOLDOWN_SECONDS + 1
        )
        challenge.save(update_fields=["requested_at", "updated_at"])
        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "A new verification code has been sent.")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 2)
        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_RESENT).exists()
        )

    @override_settings(
        INTERNAL_LOGIN_MAX_ATTEMPTS=2,
        INTERNAL_LOGIN_WINDOW_MINUTES=15,
        INTERNAL_LOGIN_LOCKOUT_MINUTES=15,
    )
    def test_internal_login_locks_after_repeated_bad_passwords(self):
        User.objects.create_user(
            username="sysadmin",
            password="testpass123",
            email="sysadmin@example.com",
            role=RecruitmentUser.Role.SYSTEM_ADMIN,
        )
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        for _attempt in range(2):
            response = self.client.post(
                reverse("login"),
                {"username": "secretariat", "password": "wrongpass"},
            )
            self.assertEqual(response.status_code, 200)

        attempt = InternalLoginAttempt.objects.get(username_normalized="secretariat")
        self.assertTrue(attempt.is_locked)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN_LOCKED).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN_ALERT_SENT).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("internal login lockout alert", mail.outbox[0].subject.lower())

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertContains(response, "Too many failed sign-in attempts")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_successful_password_entry_clears_failed_login_counter(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "wrongpass"},
        )
        self.assertTrue(
            InternalLoginAttempt.objects.filter(username_normalized="secretariat").exists()
        )

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("internal-mfa-verify"))
        self.assertFalse(
            InternalLoginAttempt.objects.filter(username_normalized="secretariat").exists()
        )

    def test_internal_password_reset_uses_tokenized_email_link(self):
        cache.clear()
        user = User.objects.create_user(
            username="secretariat",
            password="OriginalSecurePass123!",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).exists()
        )
        reset_path = re.search(
            r"http://testserver(?P<path>/internal/password/reset/[^\s]+/[^\s]+/)",
            mail.outbox[0].body,
        ).group("path")

        response = self.client.get(reset_path, follow=True)
        confirm_path = response.request["PATH_INFO"]
        response = self.client.post(
            confirm_path,
            {
                "new_password1": "DifferentSecurePass123!",
                "new_password2": "DifferentSecurePass123!",
            },
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-complete"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("DifferentSecurePass123!"))
        self.assertTrue(
            InternalPasswordHistory.objects.filter(user=user).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_COMPLETED).exists()
        )

    def test_password_reset_page_shows_logo_and_rate_limit_notice(self):
        response = self.client.get(reverse("password-reset"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rg-login-card__seal")
        self.assertContains(
            response,
            "For security, repeated reset requests may be temporarily delayed.",
        )

    def test_password_reset_email_cooldown_suppresses_repeat_email_neutrally(self):
        cache.clear()
        User.objects.create_user(
            username="secretariat",
            password="OriginalSecurePass123!",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        first_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )
        second_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
            follow=True,
        )

        self.assertEqual(first_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(second_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertContains(
            second_response,
            "If the email address matches an active internal account, a password reset link will be sent.",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).count(),
            1,
        )

    @override_settings(
        PASSWORD_RESET_EMAIL_COOLDOWN_SECONDS=0,
        PASSWORD_RESET_EMAIL_MAX_PER_WINDOW=0,
        PASSWORD_RESET_IP_MAX_PER_WINDOW=1,
        PASSWORD_RESET_IP_WINDOW_SECONDS=3600,
    )
    def test_password_reset_ip_limit_suppresses_additional_reset_email_neutrally(self):
        cache.clear()
        User.objects.create_user(
            username="secretariat-one",
            password="OriginalSecurePass123!",
            email="secretariat.one@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        User.objects.create_user(
            username="secretariat-two",
            password="OriginalSecurePass123!",
            email="secretariat.two@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        first_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat.one@example.com"},
            follow=True,
            REMOTE_ADDR="203.0.113.10",
        )
        second_response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat.two@example.com"},
            follow=True,
            REMOTE_ADDR="203.0.113.10",
        )

        self.assertEqual(first_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(second_response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.PASSWORD_RESET_REQUESTED).count(),
            1,
        )

    def test_password_reset_does_not_email_applicant_accounts(self):
        cache.clear()
        User.objects.create_user(
            username="applicant",
            password="ApplicantSecurePass123!",
            email="applicant@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )

        response = self.client.post(
            reverse("password-reset"),
            {"email": "applicant@example.com"},
            follow=True,
        )

        self.assertEqual(response.request["PATH_INFO"], reverse("password-reset-done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_internal_mfa_code_is_rejected(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        otp_code = self.extract_internal_mfa_code()
        challenge = InternalMFAChallenge.objects.get(user=user)
        challenge.expires_at = timezone.now() - timedelta(minutes=1)
        challenge.save(update_fields=["expires_at", "updated_at"])

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "verify", "otp": otp_code},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid or expired")
        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_EXPIRED).exists()
        )

    @override_settings(INTERNAL_MFA_MAX_ATTEMPTS=2)
    def test_internal_mfa_locks_after_too_many_failed_attempts(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )
        challenge = InternalMFAChallenge.objects.get(user=user)

        for _attempt in range(2):
            response = self.client.post(
                reverse("internal-mfa-verify"),
                {"action": "verify", "otp": "000000"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "invalid or expired")

        challenge.refresh_from_db()
        self.assertTrue(challenge.is_used)
        self.assertEqual(challenge.attempt_count, 2)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_MFA_LOCKED).exists()
        )

    def test_applicant_cannot_use_internal_login(self):
        User.objects.create_user(
            username="applicant",
            password="testpass123",
            role=RecruitmentUser.Role.APPLICANT,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "applicant", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "restricted to internal users")

    def test_inactive_internal_user_cannot_log_in(self):
        User.objects.create_user(
            username="inactive-chief",
            password="testpass123",
            role=RecruitmentUser.Role.HRM_CHIEF,
            is_active=False,
        )
        response = self.client.post(
            reverse("login"),
            {"username": "inactive-chief", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")
