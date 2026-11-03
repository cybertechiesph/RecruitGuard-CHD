"""Captcha, rate limits, client IP, output encoding, log redaction, upload
validation, object-level access and business-logic bypass hardening."""

import io
import json
import urllib.parse
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django import forms
from django.core import mail
from django.core.cache import cache
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..admin import ExamRecordAdmin, ScreeningRecordAdmin
from ..captcha import (
    CAPTCHA_ANSWER_SESSION_KEY,
    validate_recaptcha_token,
    validate_turnstile_token,
)
from ..forms import ApplicantOTPForm, InternalMFAOTPForm, WorkflowActionForm
from ..models import (
    AuditLog,
    ExamRecord,
    EvidenceVaultItem,
    InternalMFAChallenge,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)
from ..requirements import get_required_applicant_document_requirements
from ..services import (
    get_available_actions,
    issue_application_otp,
    process_workflow_action,
    get_or_create_vacancy_assessment_weights,
    save_screening_review,
    submit_application,
    vacancy_exam_pool,
    save_vacancy_exam_schedule,
    save_vacancy_exam_scores,
    vacancy_screening_batch_status,
    user_can_manage_comparative_assessment_report,
    user_can_view_application,
    verify_application_otp,
)
from ..upload_validation import (
    ORIGINAL_FILENAME_MAX_LENGTH,
    clamp_original_filename,
    validate_applicant_document_upload,
)
from .base import User, BaseRecruitmentTestCase


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="local",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
)
class CaptchaProtectionTests(TestCase):
    def captcha_answer(self, scope):
        return self.client.session[CAPTCHA_ANSWER_SESSION_KEY.format(scope=scope)]

    def test_internal_login_requires_valid_captcha(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.get(reverse("login"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("login"),
            {"username": "secretariat", "password": "testpass123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

        response = self.client.post(
            reverse("login"),
            {
                "username": "secretariat",
                "password": "testpass123",
                "captcha_answer": self.captcha_answer("internal_login"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.count(), 1)

    def test_internal_mfa_page_uses_cooldown_without_duplicate_captcha(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )
        self.client.get(reverse("login"))
        self.client.post(
            reverse("login"),
            {
                "username": "secretariat",
                "password": "testpass123",
                "captcha_answer": self.captcha_answer("internal_login"),
            },
        )
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.get(reverse("internal-mfa-verify"))
        self.assertNotContains(response, "Security check")

        response = self.client.post(
            reverse("internal-mfa-verify"),
            {"action": "resend"},
            follow=True,
        )

        self.assertContains(response, "Please wait")
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_otp_verification_forms_do_not_duplicate_captcha(self):
        self.assertNotIn("captcha_answer", ApplicantOTPForm().fields)
        self.assertNotIn("captcha_answer", InternalMFAOTPForm().fields)

    def test_internal_password_reset_requires_valid_captcha(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        response = self.client.get(reverse("password-reset"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("password-reset"),
            {"email": "secretariat@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertEqual(len(mail.outbox), 0)

    def test_applicant_status_lookup_requires_valid_captcha(self):
        response = self.client.get(reverse("applicant-status-lookup"))
        self.assertContains(response, "Security check: What is")

        response = self.client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": "TRK-XXXX-XXXX",
                "email": "applicant@example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")

        response = self.client.post(
            reverse("applicant-status-lookup"),
            {
                "tracking_code": "TRK-XXXX-XXXX",
                "email": "applicant@example.com",
                "captcha_answer": self.captcha_answer("applicant_status_lookup"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "We could not find an application")


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="turnstile",
    TURNSTILE_SITE_KEY="0x4AAAAAADmkw6o9jbb0OdQs",
    TURNSTILE_VERIFY_URL="https://turnstile-siteverify.example.workers.dev",
    TURNSTILE_VERIFY_ALLOWED_HOSTS="turnstile-siteverify.example.workers.dev,challenges.cloudflare.com",
    TURNSTILE_SECRET_KEY="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
    RATE_LIMIT_TRUST_X_FORWARDED_FOR=True,
)
class TurnstileCaptchaTests(TestCase):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_turnstile_response(self, success):
        payload = json.dumps({"success": success}).encode("utf-8")
        return self.FakeResponse(payload)

    def test_turnstile_widget_renders_on_internal_login(self):
        response = self.client.get(reverse("login"))

        self.assertContains(
            response,
            "https://challenges.cloudflare.com/turnstile/v0/api.js",
        )
        self.assertContains(response, 'class="cf-turnstile"')
        self.assertContains(response, 'data-sitekey="0x4AAAAAADmkw6o9jbb0OdQs"')
        self.assertContains(response, 'data-action="turnstile-spin-v1"')
        self.assertContains(response, 'data-callback="rgCaptchaSuccess"')

    def test_turnstile_worker_success_allows_internal_login_challenge(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(True),
        ) as urlopen:
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "cf-turnstile-response": "valid-turnstile-token",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://turnstile-siteverify.example.workers.dev",
        )
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(
            json.loads(request.data.decode("utf-8"))["token"],
            "valid-turnstile-token",
        )
        self.assertNotIn("remoteip", json.loads(request.data.decode("utf-8")))

    def test_turnstile_direct_secret_takes_priority_over_worker(self):
        with self.settings(TURNSTILE_SECRET_KEY="configured-secret"):
            with patch(
                "recruitment.captcha.urllib.request.urlopen",
                return_value=self.fake_turnstile_response(True),
            ) as urlopen:
                is_valid = validate_turnstile_token(None, "valid-turnstile-token")

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["secret"], ["configured-secret"])
        self.assertEqual(payload["response"], ["valid-turnstile-token"])
        self.assertNotIn("remoteip", payload)

    def test_turnstile_forwards_public_client_ip(self):
        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(True),
        ) as urlopen:
            is_valid = validate_turnstile_token(
                SimpleNamespace(
                    META={
                        "HTTP_X_FORWARDED_FOR": "8.8.8.8, 10.0.0.1",
                        "REMOTE_ADDR": "10.0.0.1",
                    }
                ),
                "valid-turnstile-token",
            )

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["remoteip"], "8.8.8.8")

    def test_turnstile_worker_failure_blocks_internal_login_challenge(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_turnstile_response(False),
        ):
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "cf-turnstile-response": "invalid-turnstile-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_turnstile_rejects_unallowed_verify_host_before_network_call(self):
        with self.settings(TURNSTILE_VERIFY_URL="https://evil.example/turnstile"):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_turnstile_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()

    def test_turnstile_rejects_non_https_verify_url_before_network_call(self):
        with self.settings(
            TURNSTILE_VERIFY_URL="http://turnstile-siteverify.example.workers.dev"
        ):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_turnstile_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()


@override_settings(
    CAPTCHA_ENABLED=True,
    CAPTCHA_PROVIDER="recaptcha",
    RECAPTCHA_SITE_KEY="test-recaptcha-site-key",
    RECAPTCHA_SECRET_KEY="test-recaptcha-secret-key",
    RECAPTCHA_TIMEOUT_SECONDS=5,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RATE_LIMIT_ENABLED=False,
    RATE_LIMIT_TRUST_X_FORWARDED_FOR=True,
)
class RecaptchaCaptchaTests(TestCase):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_recaptcha_response(self, success):
        payload = json.dumps({"success": success}).encode("utf-8")
        return self.FakeResponse(payload)

    def test_recaptcha_widget_renders_on_internal_login(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, "https://www.google.com/recaptcha/api.js")
        self.assertContains(response, 'class="g-recaptcha"')
        self.assertContains(response, 'data-sitekey="test-recaptcha-site-key"')
        self.assertContains(response, 'data-callback="rgCaptchaSuccess"')

    def test_recaptcha_success_allows_internal_login_challenge(self):
        user = User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(True),
        ) as urlopen:
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "g-recaptcha-response": "valid-recaptcha-token",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("internal-mfa-verify"))
        self.assertEqual(InternalMFAChallenge.objects.filter(user=user).count(), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://www.google.com/recaptcha/api/siteverify",
        )
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["secret"], ["test-recaptcha-secret-key"])
        self.assertEqual(payload["response"], ["valid-recaptcha-token"])
        self.assertNotIn("remoteip", payload)

    def test_recaptcha_failure_blocks_internal_login_challenge(self):
        User.objects.create_user(
            username="secretariat",
            password="testpass123",
            email="secretariat@example.com",
            role=RecruitmentUser.Role.SECRETARIAT,
        )

        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(False),
        ):
            response = self.client.post(
                reverse("login"),
                {
                    "username": "secretariat",
                    "password": "testpass123",
                    "g-recaptcha-response": "invalid-recaptcha-token",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Complete the security check correctly.")
        self.assertFalse(InternalMFAChallenge.objects.exists())

    def test_recaptcha_forwards_public_client_ip(self):
        with patch(
            "recruitment.captcha.urllib.request.urlopen",
            return_value=self.fake_recaptcha_response(True),
        ) as urlopen:
            is_valid = validate_recaptcha_token(
                SimpleNamespace(
                    META={
                        "HTTP_X_FORWARDED_FOR": "8.8.8.8, 10.0.0.1",
                        "REMOTE_ADDR": "10.0.0.1",
                    }
                ),
                "valid-recaptcha-token",
            )

        self.assertTrue(is_valid)
        request = urlopen.call_args.args[0]
        payload = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["remoteip"], ["8.8.8.8"])

    def test_recaptcha_without_secret_fails_closed(self):
        with self.settings(RECAPTCHA_SECRET_KEY=""):
            with patch("recruitment.captcha.urllib.request.urlopen") as urlopen:
                is_valid = validate_recaptcha_token(None, "token")

        self.assertFalse(is_valid)
        urlopen.assert_not_called()


@override_settings(RATE_LIMIT_ENABLED=False)
class OutputEncodingTests(TestCase):
    def test_shared_form_field_template_escapes_help_text(self):
        class UnsafeHelpTextForm(forms.Form):
            name = forms.CharField(help_text="<script>alert(1)</script>")

        rendered = render_to_string(
            "recruitment/includes/form_field.html",
            {"field": UnsafeHelpTextForm()["name"]},
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)


@override_settings(
    CAPTCHA_ENABLED=False,
    RATE_LIMIT_ENABLED=True,
    RATE_LIMIT_RULES={"default": {"limit": 2, "window": 60}},
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "rate-limit-test-cache",
        }
    },
)
class RateLimitMiddlewareTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_default_rate_limit_returns_429_after_threshold(self):
        # Cookieless requests from one IP share one bucket.
        self.assertEqual(Client().get(reverse("login")).status_code, 200)
        self.assertEqual(Client().get(reverse("login")).status_code, 200)

        response = Client().get(reverse("login"))

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-RateLimit-Limit"], "2")
        self.assertEqual(response["X-RateLimit-Remaining"], "0")
        self.assertIn("Retry-After", response)

    def test_browsers_sharing_one_ip_do_not_lock_each_other_out(self):
        # Carrier-grade NAT: many phones, one public IP. Each browser has its own budget.
        for index in range(4):
            browser = Client()
            browser.cookies["csrftoken"] = f"browser-{index}"
            self.assertEqual(browser.get(reverse("login")).status_code, 200)
            self.assertEqual(browser.get(reverse("login")).status_code, 200)

    @override_settings(RATE_LIMIT_SHARED_IP_FACTOR=2)
    def test_one_ip_still_has_a_ceiling_even_with_fresh_cookies(self):
        statuses = []
        for index in range(6):
            browser = Client()
            browser.cookies["csrftoken"] = f"invented-{index}"
            statuses.append(browser.get(reverse("login")).status_code)
        self.assertEqual(statuses[:4], [200, 200, 200, 200])
        self.assertEqual(statuses[4:], [429, 429])

    @override_settings(RATE_LIMIT_TRUST_X_FORWARDED_FOR=True)
    def test_forged_forwarded_for_entries_do_not_dodge_the_limit(self):
        # nginx appends the real address on the right; the left side is visitor-controlled.
        statuses = [
            Client().get(
                reverse("login"), HTTP_X_FORWARDED_FOR=f"198.51.100.{index}, 203.0.113.7"
            ).status_code
            for index in range(3)
        ]
        self.assertEqual(statuses, [200, 200, 429])


    @override_settings(
        RATE_LIMIT_RULES={"default": {"limit": 50, "window": 60}, "otp": {"limit": 2, "window": 60}}
    )
    def test_track_application_lookups_use_the_code_budget(self):
        """Round E, P3-6: the Track Application form checks emailed codes, so it shares
        the tight "otp" budget rather than the default one."""
        browser = Client()
        browser.session.save()  # one browser throughout: its session cookie exists from the start
        statuses = [
            browser.post(reverse("applicant-status-lookup"), {"tracking_code": "TRK-AAAA-BBBB", "email": "x@example.com"}).status_code
            for _ in range(3)
        ]
        self.assertEqual(statuses, [200, 200, 429])

class ClientIpTests(SimpleTestCase):
    def request(self, forwarded="", remote="127.0.0.1"):
        return SimpleNamespace(META={"HTTP_X_FORWARDED_FOR": forwarded, "REMOTE_ADDR": remote})

    @override_settings(RATE_LIMIT_TRUST_X_FORWARDED_FOR=True)
    def test_behind_the_proxy_the_rightmost_public_address_wins(self):
        from config.client_ip import get_client_ip

        self.assertEqual(get_client_ip(self.request("1.2.3.4, 203.0.113.7")), "203.0.113.7")
        self.assertEqual(get_client_ip(self.request("8.8.8.8, 10.0.0.1")), "8.8.8.8")
        self.assertEqual(get_client_ip(self.request("")), "127.0.0.1")

    @override_settings(RATE_LIMIT_TRUST_X_FORWARDED_FOR=False)
    def test_without_a_trusted_proxy_the_header_is_ignored(self):
        from config.client_ip import get_client_ip

        self.assertEqual(get_client_ip(self.request("1.2.3.4", remote="203.0.113.9")), "203.0.113.9")


@override_settings(RATE_LIMIT_ENABLED=False)
class LoggingRedactionTests(TestCase):
    def test_sensitive_values_are_redacted_from_log_text(self):
        from config.logging import redact_sensitive_text

        redacted = redact_sensitive_text(
            "password=hunter2 otp:123456 token='abc123' Authorization: Bearer secret-token"
        )

        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("123456", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret-token", redacted)


class ClampOriginalFilenameTests(SimpleTestCase):
    def test_short_name_is_returned_unchanged(self):
        self.assertEqual(clamp_original_filename("diploma.pdf"), "diploma.pdf")

    def test_blank_name_is_handled(self):
        self.assertEqual(clamp_original_filename(""), "")
        self.assertEqual(clamp_original_filename(None), "")

    def test_overlong_name_is_clamped_preserving_extension(self):
        stem = "a" * 400
        result = clamp_original_filename(f"{stem}.pdf")
        self.assertEqual(len(result), ORIGINAL_FILENAME_MAX_LENGTH)
        self.assertTrue(result.endswith(".pdf"))

    def test_clamped_name_fits_the_storage_column(self):
        # EvidenceVaultItem.original_filename is a CharField(max_length=255);
        # the clamp must never hand it something longer.
        result = clamp_original_filename("x" * 1000 + ".png")
        self.assertLessEqual(len(result), ORIGINAL_FILENAME_MAX_LENGTH)


class ApplicantUploadValidationTests(TestCase):
    def assert_upload_error(self, *, filename, content, content_type, message):
        with self.assertRaisesMessage(ValueError, message):
            validate_applicant_document_upload(
                SimpleUploadedFile(
                    filename,
                    content,
                    content_type=content_type,
                )
            )

    def test_empty_file_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="empty.pdf",
            content=b"",
            content_type="application/pdf",
            message="This file is empty. Pick a file that has something in it.",
        )

    def test_oversized_file_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="oversized.pdf",
            content=b"%PDF-" + (b"A" * (5 * 1024 * 1024)),
            content_type="application/pdf",
            message="This file is too large. Choose a file that is 5 MB or smaller.",
        )

    def test_wrong_extension_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.html",
            content=b"<html></html>",
            content_type="text/html",
            message="This needs to be a PDF, JPG, or PNG file. Upload one of those.",
        )

    def test_unreadable_signature_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"not-a-real-document",
            content_type="application/pdf",
            message=(
                "We couldn't read this as a PDF, JPG, or PNG. "
                "Save it again, then upload it."
            ),
        )

    def test_signature_extension_mismatch_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"\x89PNG\r\n\x1a\nimage-data",
            content_type="application/octet-stream",
            message=(
                "This file's contents don't match a PDF, JPG, or PNG. "
                "Save it again, then upload it."
            ),
        )

    def test_mime_filename_mismatch_uses_actionable_error_copy(self):
        self.assert_upload_error(
            filename="document.pdf",
            content=b"%PDF-1.4\ndocument",
            content_type="image/png",
            message=(
                "This file format does not match its filename. "
                "Save it again as a PDF, JPG, or PNG, then upload it."
            ),
        )


class IdorObjectAccessRegressionTests(BaseRecruitmentTestCase):
    """Pins the PT-013/014/015 IDOR scenarios: swapping an object ID in a URL or
    request must never disclose another applicant's case, evidence, or output.

    Each attempt has to return 403/404 (or a safe login redirect) with no
    restricted data served.
    """

    def _submit(self, position):
        application = self.make_application(position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    def _submit_for(self, position, suffix):
        # A distinct applicant so two submissions can coexist on the same
        # vacancy (applicant+position is unique).
        applicant = User.objects.create_user(
            username=f"idor-applicant-{suffix}",
            password="testpass123",
            email=f"idor-applicant-{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name="Test",
            applicant_last_name=f"Applicant{suffix}",
            applicant_email=applicant.email,
            applicant_phone="09171234567",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Qualified applicant.",
            cover_letter="I am applying.",
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"idor-{suffix}"
        )
        otp_code = issue_application_otp(application, actor=applicant)
        verify_application_otp(application, otp_code, actor=applicant)
        application.refresh_from_db()
        submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _application_evidence(self, application):
        return EvidenceVaultItem.objects.get(
            application=application,
            artifact_scope=EvidenceVaultItem.OwnerScope.APPLICATION,
            document_key=get_required_applicant_document_requirements()[0].code,
        )

    # --- PT-013: unauthorized case access (object and level) -------------
    def test_secretariat_cannot_open_level2_case_detail(self):
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 404)

    def test_secretariat_cannot_open_level2_vacancy_console(self):
        self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("vacancy-batch-detail", kwargs={"pk": self.level2_position.pk})
        )

        self.assertEqual(response.status_code, 404)

    # --- PT-014: unauthorized evidence access (download and inline) ------
    def test_evidence_download_rejects_mismatched_evidence_id(self):
        # Both cases are Level-1 and viewable by the secretariat role, so the
        # only thing standing between the request and the file is the evidence
        # <-> application binding. Swapping in another case's evidence id must
        # 404 rather than serve the file.
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "evswap")
        evidence_b = self._application_evidence(app_b)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_a.pk, "evidence_pk": evidence_b.pk},
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            AuditLog.objects.filter(
                application=app_a,
                action=AuditLog.Action.EVIDENCE_ACCESS_DENIED,
                metadata__evidence_id=evidence_b.id,
                metadata__reason="context_mismatch",
            ).exists()
        )

    def test_evidence_download_rejects_mismatched_application_id(self):
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "appswap")
        evidence_a = self._application_evidence(app_a)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_b.pk, "evidence_pk": evidence_a.pk},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_inline_disposition_does_not_bypass_evidence_context_check(self):
        app_a = self._submit(self.level1_position)
        app_b = self._submit_for(self.level1_position, "inlineswap")
        evidence_b = self._application_evidence(app_b)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse(
                "evidence-download",
                kwargs={"pk": app_a.pk, "evidence_pk": evidence_b.pk},
            ),
            {"disposition": "inline"},
        )

        self.assertEqual(response.status_code, 404)

    # --- PT-015: unauthorized output access (CAR/export/audit/decision) --
    def test_secretariat_cannot_open_application_audit_log(self):
        application = self._submit(self.level1_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(
            reverse("application-audit-log", kwargs={"pk": application.pk})
        )

        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_export_level2_case(self):
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-export", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 403)

    def test_secretariat_cannot_post_final_selection_for_level2_case(self):
        # SL-01 removed the per-case COS decision endpoint (the pick is the selection);
        # the equivalent unauthorized-output probe is now the Plantilla selection route.
        application = self._submit(self.level2_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("final-selection-record", kwargs={"pk": application.pk}),
            {"selected_item": "1", "decision_notes": "unauthorized probe"},
        )

        self.assertEqual(response.status_code, 403)

    def test_unauthorized_role_cannot_post_comparative_assessment_for_restricted_car_case(self):
        # Drive a Level-2 case to the CAR stage, then have a role without CAR access
        # POST the CAR endpoint. Authorization must be decided before any
        # application-state gate so a restricted case cannot be distinguished by its
        # response (clean 403, not a state-dependent redirect).
        # WF-04 (client decision): the Secretariat now prepares the CAR for BOTH
        # levels, so the restricted probe is the HRM Chief.
        application = self.make_application(self.level2_position)
        self.move_application_to_hrmpsb_review(application)
        self.finalize_interview_for_current_stage(application, self.hrmpsb)
        self.finalize_deliberation_for_current_stage(
            application, self.hrmpsb, ranking_position=1
        )
        self.assertTrue(
            user_can_manage_comparative_assessment_report(self.secretariat, application)
        )
        self.assertFalse(
            user_can_manage_comparative_assessment_report(self.hrm_chief, application)
        )

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse("comparative-assessment-report", kwargs={"pk": application.pk}),
            {"operation": "save", "summary_notes": "unauthorized probe"},
        )

        self.assertEqual(response.status_code, 403)


class BusinessLogicBypassHardeningTests(BaseRecruitmentTestCase):
    """Regression coverage for the PT-03 business-logic-bypass pentest scenarios: a case cannot be
    forced to a later stage (PT-007), a finalized record cannot be tampered with and assessment
    weights lock once scoring starts (PT-008), and the batch cannot reach the exam while an
    applicant is still undecided (PT-009)."""

    def _submit(self, application, applicant=None):
        applicant = applicant or self.applicant
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, applicant)
        application.refresh_from_db()
        return application

    def _extra_pending_application(self, position, suffix):
        """A second submitted applicant whose screening is left un-finalized, so the vacancy's
        screening batch stays on hold (pending decision)."""
        applicant = User.objects.create_user(
            username=f"bypass-applicant-{suffix}",
            password="testpass123",
            email=f"bypass.{suffix}@example.com",
            role=RecruitmentUser.Role.APPLICANT,
        )
        application = RecruitmentApplication.objects.create(
            applicant=applicant,
            position=position,
            applicant_first_name=f"Bypass{suffix}",
            applicant_last_name="Applicant",
            applicant_email=f"bypass.{suffix}@example.com",
            applicant_phone=f"091755500{suffix}",
            checklist_privacy_consent=True,
            checklist_documents_complete=True,
            checklist_information_certified=True,
            qualification_summary="Second applicant keeping the batch on hold.",
            cover_letter="Applying for the same vacancy.",
            performance_rating_applicability=(
                RecruitmentApplication.PerformanceRatingApplicability.NOT_APPLICABLE
            ),
        )
        self.upload_required_applicant_documents(
            application, applicant, content_prefix=f"bypass-{suffix}"
        )
        return self._submit(application, applicant)

    # ---- PT-007: forced workflow action / stage skipping -------------------------------------

    def test_generic_whole_application_return_not_offered_or_accepted(self):
        # RD-6b (Option A): the generic whole-application return is removed. It
        # is never offered, and a forced POST is rejected at the form (the
        # action is not in the available set) and refused at the service.
        application = self._submit(self.make_application(self.level1_position))
        available = {
            value for value, _label in get_available_actions(application, self.secretariat)
        }
        self.assertNotIn("return_to_applicant", available)

        form = WorkflowActionForm(
            {"action": "return_to_applicant", "remarks": "Forced whole-application return."},
            application=application,
            user=self.secretariat,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("action", form.errors)

    def test_pt007_forced_later_stage_action_is_rejected(self):
        application = self._submit(self.make_application(self.level1_position))
        # Fresh at Secretariat screening: the Appointing Authority's CAR-return action is not offered.
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        available = {
            value for value, _label in get_available_actions(application, self.secretariat)
        }
        self.assertNotIn("return_car_for_reassessment", available)

        # Form guard: the forced action fails validation ("not allowed at the current step").
        form = WorkflowActionForm(
            {"action": "return_car_for_reassessment", "remarks": "Forced past screening."},
            application=application,
            user=self.secretariat,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("action", form.errors)

        # View guard: posting the forced action does not advance the case.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("workflow-action", kwargs={"pk": application.pk}),
            {"action": "return_car_for_reassessment", "remarks": "Forced past screening."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(
            application.case.current_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW
        )
        self.assertEqual(application.status, RecruitmentApplication.Status.SECRETARIAT_REVIEW)

        # Service guard: the state machine refuses it even if the form is bypassed entirely.
        with self.assertRaises(ValueError):
            process_workflow_action(
                application,
                self.secretariat,
                "return_car_for_reassessment",
                "Forced past screening.",
            )

    # ---- PT-008: finalized-record tampering + weight lock ------------------------------------

    def test_pt008_finalized_screening_record_rejects_replayed_save(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        record = ScreeningRecord.objects.get(application=application, is_finalized=True)
        original_outcome = record.qualification_outcome

        # Replaying a Save after finalize (PT-008 step 3) must be refused, not applied.
        with self.assertRaises(ValueError):
            save_screening_review(
                application=application,
                actor=self.secretariat,
                cleaned_data={
                    "completeness_status": ScreeningRecord.CompletenessStatus.INCOMPLETE,
                    "completeness_notes": "Tampered after finalize.",
                    "qualification_outcome": (
                        ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
                    ),
                    "education_score": None,
                    "training_score": None,
                    "experience_score": None,
                    "document_review_score": None,
                    "screening_notes": "Tampered after finalize.",
                },
                finalize=False,
            )
        record.refresh_from_db()
        self.assertEqual(record.qualification_outcome, original_outcome)

    def test_pt008_admin_cannot_edit_finalized_screening_record(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        record = ScreeningRecord.objects.get(application=application, is_finalized=True)

        request = RequestFactory().get("/admin/")
        request.user = self.secretariat
        model_admin = ScreeningRecordAdmin(ScreeningRecord, AdminSite())

        # A finalized record is fully read-only and cannot be deleted from the admin.
        readonly = model_admin.get_readonly_fields(request, obj=record)
        self.assertIn("qualification_outcome", readonly)
        self.assertIn("is_finalized", readonly)
        self.assertFalse(model_admin.has_delete_permission(request, obj=record))

        # A crafted save through the admin/ORM is refused; the stored value is unchanged.
        record.qualification_outcome = ScreeningRecord.QualificationOutcome.NOT_QUALIFIED
        record.is_finalized = False
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(request, record, form=None, change=True)
        record.refresh_from_db()
        self.assertTrue(record.is_finalized)
        self.assertEqual(
            record.qualification_outcome, ScreeningRecord.QualificationOutcome.QUALIFIED
        )

    def test_pt008_weights_lock_blocks_replayed_weight_edit(self):
        application = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(application, self.secretariat)
        # SL-09: the batch waits for the applicant pool to be final.
        self.finalize_applicant_pool_for_test(self.level1_position)
        # Single-applicant vacancy: the batch is ready, so scoring can start and lock the weights.
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_schedule(
                self.level1_position,
                self.secretariat,
                {
                    "scheduled_for": timezone.now() + timedelta(hours=2),
                    "venue": "CHD CALABARZON Examination Room",
                    "instructions": "Bring a valid government ID.",
                },
            )
        with self.captureOnCommitCallbacks(execute=True):
            save_vacancy_exam_scores(
                self.level1_position,
                self.secretariat,
                {application.case.id: {"general_score": "90", "technical_score": "80"}},
                finalize=True,
            )

        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        self.assertTrue(weights.is_locked)
        original_ete = weights.ete_weight

        # PT-008 step 5-6: replaying a weights Save after scoring starts is rejected.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("vacancy-assessment-weights", args=[self.level1_position.pk]),
            {
                "ete_weight": "10",
                "exam_weight": "80",
                "interview_weight": "10",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights.refresh_from_db()
        self.assertEqual(weights.ete_weight, original_ete)

        # The finalized exam record is likewise immutable from the admin.
        exam_record = ExamRecord.objects.get(application=application, is_finalized=True)
        request = RequestFactory().get("/admin/")
        request.user = self.secretariat
        exam_admin = ExamRecordAdmin(ExamRecord, AdminSite())
        exam_readonly = exam_admin.get_readonly_fields(request, obj=exam_record)
        self.assertIn("exam_score", exam_readonly)
        self.assertIn("is_finalized", exam_readonly)
        with self.assertRaises(PermissionDenied):
            exam_admin.save_model(request, exam_record, form=None, change=True)

    # ---- PT-009: batch-gate bypass -----------------------------------------------------------

    def test_pt009_batch_exam_page_redirects_when_batch_on_hold(self):
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        # A second applicant with un-finalized screening keeps the batch on hold.
        self._extra_pending_application(self.level1_position, "1")
        qualified.refresh_from_db()
        self.assertFalse(vacancy_screening_batch_status(self.level1_position).is_ready)
        # The qualified applicant is still exam-ready (in the pool), so the page would otherwise
        # render read-only — the gate must hard-redirect instead of exposing the exam screen.
        self.assertIn(qualified, vacancy_exam_pool(self.level1_position))

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        detail_url = reverse("vacancy-batch-detail", args=[self.level1_position.pk])

        # GET is hard-redirected to the batch detail — no exam screen is exposed.
        get_response = client.get(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk])
        )
        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(get_response.url, detail_url)

        # A forced POST is likewise redirected and does not advance the batch.
        post_response = client.post(
            reverse("vacancy-batch-exam", args=[self.level1_position.pk]),
            {
                "operation": "finalize",
                f"general_score_{qualified.case.id}": "90",
                f"technical_score_{qualified.case.id}": "80",
            },
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, detail_url)
        qualified.refresh_from_db()
        self.assertFalse(
            qualified.exam_records.filter(
                review_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW, is_finalized=True
            ).exists()
        )

    def test_pt009_batch_detail_hides_open_exam_while_an_applicant_is_pending(self):
        # The batch-detail banner must not claim the batch is complete (and must not offer the
        # exam) while any applicant is still undecided, even though some applicants are already
        # exam-ready. Otherwise the green "proceed to exam" banner contradicts the on-hold gate.
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        self._extra_pending_application(self.level1_position, "hold")
        self.assertFalse(vacancy_screening_batch_status(self.level1_position).is_ready)
        # ...yet the qualified applicant is exam-ready, which is what used to force the green banner.
        self.assertTrue(
            any(
                user_can_view_application(self.secretariat, application)
                for application in vacancy_exam_pool(self.level1_position)
            )
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The exam opens once the other")
        self.assertNotContains(response, "Screening batch is complete")
        self.assertNotContains(response, "Open batch exam")

    def test_pt009_batch_detail_offers_open_exam_when_every_applicant_is_decided(self):
        qualified = self._submit(self.make_application(self.level1_position))
        self.finalize_screening_for_current_stage(qualified, self.secretariat)
        sibling = self._extra_pending_application(self.level1_position, "ready")
        self.finalize_screening_for_current_stage(sibling, self.secretariat)
        self.assertTrue(vacancy_screening_batch_status(self.level1_position).is_ready)
        # SL-09: the batch link appears once the applicant pool is final as well.
        self.finalize_applicant_pool_for_test(self.level1_position)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("vacancy-batch-detail", args=[self.level1_position.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Screening batch is complete")
        self.assertContains(response, "Open batch exam")
        self.assertNotContains(response, "The exam opens once the other")
