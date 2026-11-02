"""Audit logging and traceability."""

import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AUDIT_ACTION_CATEGORIES,
    AuditLog,
    RecruitmentCase,
    RecruitmentUser,
    ScreeningRecord,
)
from ..services import get_all_audit_logs, record_system_audit_event, submit_application
from .base import BaseRecruitmentTestCase


class AuditLoggingTraceabilityTests(BaseRecruitmentTestCase):
    def make_submitted_application(self, position=None):
        application = self.make_application(position or self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return application

    # ── REV-D: write-side noise fixes ────────────────────────────────

    def screening_payload(self, **overrides):
        payload = {
            "completeness_status": ScreeningRecord.CompletenessStatus.COMPLETE,
            "completeness_notes": "All required application documents were reviewed.",
            "qualification_outcome": ScreeningRecord.QualificationOutcome.QUALIFIED,
            "screening_notes": "Applicant satisfies the documented qualification basis.",
        }
        payload.update(overrides)
        return payload

    def test_screening_autosave_writes_no_audit_entry(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        baseline = AuditLog.objects.filter(
            application=application, action=AuditLog.Action.SCREENING_RECORDED
        ).count()

        response = client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "save",
            },
            HTTP_X_REQUESTED_WITH="RG-Autosave",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.SCREENING_RECORDED
            ).count(),
            baseline,
        )

        # A deliberate manual save still logs.
        client.post(
            reverse("screening-review", kwargs={"pk": application.pk}),
            {
                **self.screening_payload(),
                **self.screening_document_status_payload(application),
                "operation": "save",
            },
        )
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.SCREENING_RECORDED
            ).count(),
            baseline + 1,
        )

    def test_audit_log_view_logged_once_per_visit(self):
        application = self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        url = reverse("application-audit-log", kwargs={"pk": application.pk})
        client.get(url)
        client.get(url, {"q": "screening"})
        client.get(url)
        self.assertEqual(
            AuditLog.objects.filter(
                application=application, action=AuditLog.Action.AUDIT_LOG_VIEWED
            ).count(),
            1,
        )

    # ── REV-F: session tagging, categories, reports ──────────────────

    def test_every_audit_action_is_categorized(self):
        from recruitment.models import AUDIT_CATEGORY_BY_ACTION

        uncategorized = [
            value for value, _ in AuditLog.Action.choices
            if value not in AUDIT_CATEGORY_BY_ACTION
        ]
        self.assertEqual(uncategorized, [])

    def test_session_tag_groups_entries_from_one_sign_in(self):
        # The login entry itself carries device facts (via the signal; empty
        # under force_login, which bypasses the request cycle).
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        login_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.INTERNAL_LOGIN, actor=self.sysadmin)
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(login_entry)
        self.assertIn("ip_address", login_entry.metadata)
        self.assertIn("user_agent", login_entry.metadata)

        # Entries written during real requests are stamped, and everything in
        # one browsing session shares the same tag.
        client.get(reverse("audit-log-list"))
        viewed_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.AUDIT_LOG_VIEWED, actor=self.sysadmin)
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(viewed_entry)
        self.assertNotEqual(viewed_entry.session_tag, "")

        export_response = client.get(reverse("audit-log-export-csv"))
        self.assertEqual(export_response.status_code, 200)
        export_entry = (
            AuditLog.objects.filter(action=AuditLog.Action.EXPORT_GENERATED)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(export_entry.session_tag, viewed_entry.session_tag)

        # The session view filter isolates that sign-in and renders honestly.
        response = client.get(reverse("audit-log-list"), {"session": viewed_entry.session_tag})
        self.assertContains(response, "Session view")
        self.assertContains(response, "no sign-out recorded")
        self.assertContains(response, "Leave session view")

    def test_category_and_date_filters_and_summary(self):
        from recruitment.services import get_all_audit_logs, summarize_audit_logs

        application = self.make_submitted_application()
        security_logs = get_all_audit_logs(category="security")
        self.assertTrue(security_logs.exists())
        security_actions = set(AUDIT_ACTION_CATEGORIES["security"]["actions"])
        self.assertTrue(
            all(log.action in security_actions for log in security_logs)
        )
        case_logs = get_all_audit_logs(category="case")
        self.assertTrue(case_logs.filter(action=AuditLog.Action.APPLICATION_SUBMITTED).exists())
        self.assertFalse(case_logs.filter(action=AuditLog.Action.APPLICATION_OTP_SENT).exists())

        today = timezone.localdate()
        self.assertEqual(
            get_all_audit_logs(date_from=today + timedelta(days=1)).count(), 0
        )

        summary = summarize_audit_logs(get_all_audit_logs())
        self.assertEqual(summary["total"], AuditLog.objects.count())
        self.assertEqual(summary["busiest_day"], today)
        self.assertEqual(
            sum(cat["count"] for cat in summary["categories"]) + summary["other_count"],
            summary["total"],
        )

    def test_audit_search_uses_category_without_raw_action_dropdown(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<select name="action"')
        self.assertContains(response, '<select name="category"')

    def test_audit_exports_are_logged_and_filtered(self):
        self.make_submitted_application()
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)

        response = client.get(reverse("audit-log-export-csv"), {"category": "case"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("application_submitted", body)
        self.assertNotIn("internal_login", body.replace("internal_login_failed", ""))

        report = client.get(reverse("audit-log-report"), {"category": "case"})
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "Recruitment Audit Report")
        self.assertContains(report, "Case activity")

        self.assertEqual(
            AuditLog.objects.filter(
                action=AuditLog.Action.EXPORT_GENERATED,
                metadata__export__in=["audit_csv", "audit_report"],
            ).count(),
            2,
        )

    # ── REV-D: archival (export, verify, then delete) ────────────────

    def test_archive_refuses_recent_cutoff(self):
        from recruitment.services import archive_audit_logs

        with self.assertRaisesMessage(ValueError, "older than six months"):
            archive_audit_logs(self.sysadmin, timezone.localdate())

    def test_archive_exports_deletes_and_records_itself(self):
        from recruitment.services import archive_audit_logs, download_audit_archive

        application = self.make_submitted_application()
        old_moment = timezone.now() - timedelta(days=400)
        AuditLog.objects.filter(application=application).update(created_at=old_moment)
        old_count = AuditLog.objects.filter(created_at__lt=timezone.now() - timedelta(days=365)).count()
        self.assertGreater(old_count, 0)

        cutoff = timezone.localdate() - timedelta(days=365)
        archive = archive_audit_logs(self.sysadmin, cutoff)

        self.assertEqual(archive.entry_count, old_count)
        self.assertEqual(
            AuditLog.objects.filter(created_at__date__lt=cutoff).count(), 0
        )
        # The archival act itself survives on the live log.
        record = AuditLog.objects.get(action=AuditLog.Action.AUDIT_LOG_ARCHIVED)
        self.assertEqual(record.metadata["entry_count"], old_count)
        self.assertTrue(record.is_sensitive_access)

        # Download round-trip: decrypts to CSV containing the archived rows,
        # and the download is itself on the record.
        content = download_audit_archive(archive, self.sysadmin)
        text = content.decode("utf-8")
        self.assertIn("application_submitted", text)
        self.assertEqual(text.count("\n") - 1, old_count)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.AUDIT_ARCHIVE_DOWNLOADED
            ).exists()
        )

    def test_submission_audit_log_stores_traceability_fields(self):
        application = self.make_submitted_application()

        log = AuditLog.objects.get(
            application=application,
            action=AuditLog.Action.APPLICATION_SUBMITTED,
        )

        self.assertEqual(log.actor, self.applicant)
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, application.reference_number)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)

    def test_application_detail_view_logs_sensitive_record_access(self):
        application = self.make_submitted_application()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("application-detail", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application=application,
            actor=self.secretariat,
            action=AuditLog.Action.PROTECTED_RECORD_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertEqual(log.metadata["access_source"], "application_detail")

    def test_system_admin_can_review_application_audit_log_and_it_is_logged(self):
        application = self.make_submitted_application()

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Case Audit Log")
        log = AuditLog.objects.filter(
            application=application,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "application_audit")

    def test_evidence_vault_review_logs_sensitive_access(self):
        self.make_submitted_application()

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("evidence-vault-list"))

        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)

    def test_system_admin_audit_log_shows_system_and_case_activity(self):
        # Case activity (application-scoped) generates audit records...
        application = self.make_submitted_application()
        # ...alongside system/identity events.
        record_system_audit_event(
            actor=self.sysadmin,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="System administrator changed a password.",
            metadata={"target_user_id": self.secretariat.id},
        )

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("audit-log-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Audit Log")
        # Both system/identity events and case activity appear in the
        # consolidated oversight trail.
        self.assertContains(response, "Password Changed")
        audit_logs = response.context["audit_logs"]
        self.assertTrue(any(log.application_id == application.id for log in audit_logs))
        self.assertTrue(any(log.application_id is None for log in audit_logs))
        log = AuditLog.objects.filter(
            application__isnull=True,
            actor=self.sysadmin,
            action=AuditLog.Action.AUDIT_LOG_VIEWED,
        ).latest("created_at")
        self.assertTrue(log.is_sensitive_access)
        self.assertEqual(log.metadata["review_scope"], "consolidated_audit")

    def test_audit_search_matches_displayed_action_label(self):
        # "Secured Files Viewed" is the reviewer-facing label for the
        # evidence_vault_viewed action code. A search for the wording shown on
        # screen must find the record even though neither the stored code nor
        # the description contains "secure".
        vault_log = AuditLog.objects.create(
            actor=self.sysadmin,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
            description="Reviewed evidence vault records.",
        )

        results = list(get_all_audit_logs(search_query="secure"))
        self.assertIn(vault_log, results)

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("audit-log-list"), {"q": "secure"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secured Files Viewed")
        self.assertTrue(
            any(
                entry.action == AuditLog.Action.EVIDENCE_VAULT_VIEWED
                for entry in response.context["audit_logs"]
            )
        )

    def test_evidence_vault_page_omits_audit_pane(self):
        self.make_submitted_application()

        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        response = client.get(reverse("evidence-vault-list"))

        self.assertEqual(response.status_code, 200)
        # The Secured Files page no longer carries a combined audit pane; the
        # audit trail lives on its own dedicated page.
        self.assertNotIn("recent_audit_logs", response.context)
        self.assertNotContains(response, "Recent audit records across the system.")

    def test_audit_details_render_human_readable_summary(self):
        client = Client()
        self.force_login_with_mfa(client, self.sysadmin)
        # Generate an oversight "view" record whose metadata is filter state.
        existing_ids = set(
            AuditLog.objects.filter(
                actor=self.sysadmin,
                action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
            ).values_list("pk", flat=True)
        )
        evidence_response = client.get(reverse("evidence-vault-list"))
        self.assertEqual(evidence_response.status_code, 200)
        vault_log = AuditLog.objects.exclude(pk__in=existing_ids).get(
            actor=self.sysadmin,
            action=AuditLog.Action.EVIDENCE_VAULT_VIEWED,
        )
        self.assertNotIn(vault_log.pk, existing_ids)

        # Select the event this test generated instead of assuming it lands on
        # page one of the 50-row, unfiltered consolidated trail.
        response = client.get(
            reverse("audit-log-list"),
            {
                "q": vault_log.description,
                "category": "security",
                "actor": self.sysadmin.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(vault_log, response.context["audit_logs"])
        # The details are summarized in plain language, not dumped as raw JSON.
        self.assertContains(response, "Secured Files Viewed")
        self.assertContains(response, "No filters applied.")
        self.assertNotContains(response, "artifact_scope")
        self.assertNotContains(response, "current_version_only")

    def test_non_admin_roles_cannot_review_audit_logs(self):
        application = self.make_submitted_application()
        record_system_audit_event(
            actor=self.sysadmin,
            action=AuditLog.Action.PASSWORD_CHANGED,
            description="System administrator changed a password.",
            metadata={"target_user_id": self.secretariat.id},
        )

        roles = {
            "secretariat": self.secretariat,
            "hrm_chief": self.hrm_chief,
            "hrmpsb_member": self.hrmpsb,
            "appointing_authority": self.appointing,
        }
        for label, user in roles.items():
            client = Client()
            self.force_login_with_mfa(client, user)
            with self.subTest(role=label, endpoint="audit_log_list"):
                response = client.get(reverse("audit-log-list"))
                self.assertEqual(response.status_code, 403)
            with self.subTest(role=label, endpoint="application_audit_log"):
                response = client.get(reverse("application-audit-log", kwargs={"pk": application.pk}))
                self.assertEqual(response.status_code, 403)

    def test_traceability_backfill_handles_draft_logs_without_reference_numbers(self):
        migration_module = importlib.import_module(
            "recruitment.migrations.0014_auditlog_traceability_fields"
        )
        application = self.make_application(self.level1_position)
        log = AuditLog.objects.create(
            application=application,
            actor=self.applicant,
            actor_role="",
            case_reference="",
            workflow_stage="",
            action=AuditLog.Action.APPLICATION_CREATED,
            description="Applicant created a draft application.",
            metadata={"review_stage": RecruitmentCase.Stage.SECRETARIAT_REVIEW},
            is_sensitive_access=False,
        )

        migration_module.backfill_audit_log_traceability(django_apps, None)

        log.refresh_from_db()
        self.assertEqual(log.actor_role, RecruitmentUser.Role.APPLICANT)
        self.assertEqual(log.case_reference, "")
        self.assertEqual(log.workflow_stage, RecruitmentCase.Stage.SECRETARIAT_REVIEW)
        self.assertFalse(log.is_sensitive_access)
