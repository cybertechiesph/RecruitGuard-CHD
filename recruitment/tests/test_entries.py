"""Recruitment entries: vacancies, assessment weights and document requirements."""

from decimal import Decimal
from datetime import date, timedelta

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ..models import (
    AuditLog,
    ExamRecord,
    ExamSchedule,
    OfficePolicy,
    PositionDocumentRequirement,
    PositionReference,
    PositionPosting,
    RecruitmentApplication,
    RecruitmentCase,
    RecruitmentUser,
    VacancyAssessmentWeights,
)
from ..requirements import (
    APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH,
    DIPLOMA,
    MIN_REQUIRED_DOCUMENT_CODES,
    PERFORMANCE_RATING,
    PERSONAL_DATA_SHEET,
    SIGNED_COVER_LETTER,
    TRAINING_CERTIFICATES,
    get_applicant_document_requirements,
    get_required_applicant_document_requirements,
)
from ..services import (
    get_office_policy,
    get_or_create_vacancy_assessment_weights,
    lock_vacancy_assessment_weights,
    unlock_vacancy_assessment_weights,
    persist_recruitment_entry,
    get_missing_required_applicant_document_requirements,
    set_position_document_requirements,
    submit_application,
    update_recruitment_entry_status,
)
from .base import BaseRecruitmentTestCase


class RecruitmentEntryManagementTests(BaseRecruitmentTestCase):
    def mark_application_as_submitted_without_case(self, application):
        application.status = RecruitmentApplication.Status.SECRETARIAT_REVIEW
        application.current_handler_role = RecruitmentUser.Role.SECRETARIAT
        application.submitted_at = timezone.now()
        application.save(
            update_fields=[
                "status",
                "current_handler_role",
                "submitted_at",
                "branch",
                "level",
                "updated_at",
            ]
        )
        return application

    def test_starter_position_reference_catalog_is_seeded(self):
        self.assertTrue(
            PositionReference.objects.filter(
                position_title="Accountant II",
                reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
            ).exists()
        )

    def test_position_posting_salary_grade_display_uses_reference(self):
        self.admin_aide_position.salary_grade = 7
        self.admin_aide_position.save(update_fields=["salary_grade", "updated_at"])
        self.level1_position.refresh_from_db()

        self.assertEqual(self.level1_position.salary_grade_display, "SG 7")

        entry_without_reference = PositionPosting(
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        self.assertEqual(entry_without_reference.salary_grade_display, "")

    def test_position_reference_slug_generation_is_collision_safe(self):
        first = PositionReference.objects.create(
            position_title="Budget Officer",
            salary_grade=18,
            level_classification=PositionReference.LevelClassification.SECOND_LEVEL,
            class_id="BO1",
            os_code="02-FS",
            occupational_service="Financial Service",
            occupational_group="Budgeting",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
        )
        second = PositionReference.objects.create(
            position_title="Budget Officer",
            salary_grade=19,
            level_classification=PositionReference.LevelClassification.SECOND_LEVEL,
            class_id="BO2",
            os_code="02-FS",
            occupational_service="Financial Service",
            occupational_group="Budgeting",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
        )

        self.assertEqual(first.position_slug, "budget-officer")
        self.assertEqual(second.position_slug, "budget-officer-2")

    def test_plantilla_entry_requires_fixed_period_and_closing_date(self):
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_plantilla_entry_uses_fourteen_calendar_day_publication_period(self):
        publication_date = date(2026, 6, 1)
        expected_closing_date = date(2026, 6, 14)
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            item_number="OSEC-DOH-AA6-11-2026",
            publication_date=publication_date,
            opening_date=publication_date,
            closing_date=expected_closing_date,
        )

        entry.full_clean()

        self.assertEqual(entry.expected_plantilla_closing_date, expected_closing_date)

    def test_plantilla_entry_allows_custom_publication_period(self):
        publication_date = date(2026, 6, 1)
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            item_number="OSEC-DOH-AA6-12-2026",
            publication_date=publication_date,
            opening_date=publication_date,
            closing_date=publication_date + timedelta(days=30),
        )

        # The 14-day window is a default, not a cap; a longer period is accepted.
        entry.full_clean()

        self.assertEqual(entry.closing_date, publication_date + timedelta(days=30))

    def test_cos_open_pool_entry_cannot_set_closing_date(self):
        entry = PositionPosting(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_entry_code_is_generated_on_first_save_and_matches_pattern(self):
        entry = PositionPosting.objects.create(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.DRAFT,
            closing_date=self.level1_closing_date(),
        )

        expected_code = f"VAC-PLT-{timezone.localdate().year:04d}-003"
        self.assertEqual(entry.job_code, expected_code)
        self.assertRegex(entry.job_code, r"^VAC-(PLT|COS)-\d{4}-\d{3,}$")

    def test_entry_code_generation_uses_year_priority_and_next_sequence_within_branch(self):
        current_year = timezone.localdate().year
        publication_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            publication_date=date(current_year + 1, 2, 10),
            opening_date=date(current_year, 5, 1),
        )
        opening_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            opening_date=date(current_year + 2, 3, 15),
        )
        fallback_entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        self.assertEqual(publication_entry.job_code, f"VAC-COS-{current_year + 1:04d}-001")
        self.assertEqual(opening_entry.job_code, f"VAC-COS-{current_year + 2:04d}-001")
        self.assertEqual(fallback_entry.job_code, f"VAC-COS-{current_year:04d}-002")

    def test_entry_code_validation_rejects_invalid_pattern_and_missing_saved_code(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        entry.job_code = "INVALID"
        with self.assertRaises(ValidationError) as invalid_pattern_error:
            entry.full_clean()
        self.assertIn(
            "Entry Code must match the format",
            invalid_pattern_error.exception.message_dict["job_code"][0],
        )

        entry.job_code = ""
        with self.assertRaises(ValidationError) as missing_code_error:
            entry.full_clean()
        self.assertIn(
            "Entry Code is required after the recruitment entry is first saved.",
            missing_code_error.exception.message_dict["job_code"],
        )

    def test_entry_code_validation_rejects_branch_and_year_mismatch(self):
        current_year = timezone.localdate().year
        branch_mismatch_entry = PositionPosting(
            position_reference=self.project_assistant_position,
            job_code=f"VAC-PLT-{current_year:04d}-001",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        with self.assertRaises(ValidationError) as branch_error:
            branch_mismatch_entry.full_clean()
        self.assertIn(
            "Entry Code branch segment must match the selected engagement type.",
            branch_error.exception.message_dict["job_code"],
        )

        year_mismatch_entry = PositionPosting(
            position_reference=self.project_assistant_position,
            job_code=f"VAC-COS-{current_year - 1:04d}-001",
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        with self.assertRaises(ValidationError) as year_error:
            year_mismatch_entry.full_clean()
        self.assertIn(
            "Entry Code year segment must match the publication date, opening date, or current year fallback.",
            year_error.exception.message_dict["job_code"],
        )

    def test_entry_code_field_is_read_only_in_create_and_edit_forms(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        create_response = client.get(reverse("recruitment-entry-create"))
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, 'id="id_job_code"')
        self.assertContains(create_response, 'id="id_status"')
        self.assertContains(create_response, 'placeholder="Will be generated automatically after first save"')
        self.assertContains(
            create_response,
            "Generated automatically for tracking. This cannot be edited.",
        )

        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )
        update_response = client.get(reverse("recruitment-entry-update", kwargs={"pk": entry.pk}))

        self.assertEqual(update_response.status_code, 200)
        self.assertContains(update_response, entry.job_code)
        self.assertNotContains(update_response, 'id="id_status"')
        self.assertContains(update_response, "Current Status")
        self.assertContains(update_response, entry.get_status_display())
        self.assertContains(update_response, "Change status from the Vacancies list.")

    def test_entry_create_status_choices_allow_only_draft_and_active(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        create_url = reverse("recruitment-entry-create")
        initial_count = PositionPosting.objects.count()
        payload = {
            "position_reference": self.project_assistant_position.pk,
            "branch": PositionPosting.Branch.COS,
            "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
            "status": PositionPosting.EntryStatus.SUSPENDED,
            "publication_date": "",
            "opening_date": self.entry_opening_date_string(),
            "closing_date": "",
            "qualification_reference": "SL2 create-status choice regression.",
        }

        invalid_response = client.post(create_url, payload)

        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(
            invalid_response,
            "Select a valid choice. suspended is not one of the available choices.",
        )
        self.assertEqual(PositionPosting.objects.count(), initial_count)

        payload["status"] = PositionPosting.EntryStatus.ACTIVE
        valid_response = client.post(create_url, payload)

        self.assertEqual(valid_response.status_code, 302)
        created_entry = PositionPosting.objects.get(
            qualification_reference="SL2 create-status choice regression."
        )
        self.assertEqual(created_entry.status, PositionPosting.EntryStatus.ACTIVE)

    def test_entry_manager_can_create_recruitment_entry(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Continuous talent pool for technical support.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.exclude(pk=self.cos_position.pk).get(
            position_reference=self.project_assistant_position,
            qualification_reference="Continuous talent pool for technical support.",
        )
        self.assertEqual(created_entry.created_by, self.secretariat)
        self.assertEqual(created_entry.updated_by, self.secretariat)
        self.assertEqual(created_entry.position_reference, self.project_assistant_position)
        self.assertEqual(created_entry.title, self.project_assistant_position.position_title)
        self.assertEqual(created_entry.level, PositionPosting.Level.LEVEL_1)
        self.assertEqual(created_entry.job_code, f"VAC-COS-{self.entry_opening_date().year:04d}-002")
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_CREATED,
                metadata__entry_code=created_entry.job_code,
            ).exists()
        )

    def test_entry_manager_can_create_plantilla_entry_with_auto_closing_date(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        opening_date = self.entry_opening_date()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "item_number": "OSEC-DOH-AA6-13-2026",
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": opening_date.isoformat(),
                "closing_date": "",
                "qualification_reference": "Education: Completion of two years of college\nTraining: None required\nExperience: None required\nFourteen-day Plantilla publication window.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.get(
            qualification_reference="Education: Completion of two years of college\nTraining: None required\nExperience: None required\nFourteen-day Plantilla publication window."
        )
        self.assertEqual(
            created_entry.closing_date,
            PositionPosting.calculate_plantilla_closing_date(opening_date),
        )

    def test_entry_manager_can_set_custom_plantilla_publication_period(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        opening_date = self.entry_opening_date()
        closing_date = opening_date + timedelta(days=30)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.admin_aide_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "item_number": "OSEC-DOH-AA6-14-2026",
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": opening_date.isoformat(),
                "closing_date": closing_date.isoformat(),
                "qualification_reference": "Education: Completion of two years of college\nTraining: None required\nExperience: None required\nCustom Plantilla publication window.",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_entry = PositionPosting.objects.get(
            qualification_reference="Education: Completion of two years of college\nTraining: None required\nExperience: None required\nCustom Plantilla publication window."
        )
        self.assertEqual(created_entry.closing_date, closing_date)

    def test_generated_entry_code_is_preserved_on_edit(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
            qualification_reference="Initial notes.",
        )
        original_job_code = entry.job_code

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Updated notes.",
            },
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.job_code, original_job_code)
        self.assertEqual(entry.qualification_reference, "Updated notes.")

    def _lapsed_cos_entry(self, *, days_closed=1):
        # Opening-based COS: closing date allowed, so a lapsed deadline can be
        # built directly (objects.create skips form validation on purpose).
        return PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.OPENING_BASED,
            status=PositionPosting.EntryStatus.ACTIVE,
            opening_date=timezone.localdate() - timedelta(days=20),
            closing_date=timezone.localdate() - timedelta(days=days_closed),
        )

    def _entry_edit_payload(self, entry, **overrides):
        payload = {
            "position_reference": entry.position_reference_id,
            "branch": entry.branch,
            "item_number": entry.item_number or "",
            "intake_mode": entry.intake_mode,
            "status": entry.status,
            "publication_date": "",
            "opening_date": entry.opening_date.isoformat() if entry.opening_date else "",
            "closing_date": entry.closing_date.isoformat() if entry.closing_date else "",
            "qualification_reference": entry.qualification_reference or "",
        }
        payload.update(overrides)
        return payload

    def _draft_cos_entry(self):
        entry = self._lapsed_cos_entry(days_closed=-7)
        entry.status = PositionPosting.EntryStatus.DRAFT
        entry.save(update_fields=["status", "updated_at"])
        return entry

    def test_entry_create_rejects_past_closing_date(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.OPENING_BASED,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": (timezone.localdate() - timedelta(days=20)).isoformat(),
                "closing_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
                "qualification_reference": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The closing date cannot be set to a past date.")

    def test_draft_deadline_move_is_not_an_extension_or_deadline_audit_event(self):
        entry = self._draft_cos_entry()
        new_closing = entry.closing_date + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertFalse(entry.deadline_extended)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                metadata__recruitment_entry_id=entry.id,
            ).exists()
        )
        message_text = " ".join(
            message.message for message in get_messages(response.wsgi_request)
        )
        self.assertNotIn("Deadline extended", message_text)

    def test_first_publication_with_later_deadline_is_not_an_extension(self):
        entry = self._draft_cos_entry()
        new_closing = entry.closing_date + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        edit_response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(
                entry,
                status=PositionPosting.EntryStatus.ACTIVE,
                closing_date=new_closing.isoformat(),
            ),
        )

        self.assertEqual(edit_response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PositionPosting.EntryStatus.DRAFT)
        self.assertEqual(entry.closing_date, new_closing)
        self.assertFalse(entry.deadline_extended)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                metadata__recruitment_entry_id=entry.id,
            ).exists()
        )

        activation_response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": entry.pk, "status": PositionPosting.EntryStatus.ACTIVE},
            )
        )

        self.assertEqual(activation_response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertEqual(entry.closing_date, new_closing)
        self.assertFalse(entry.deadline_extended)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                metadata__recruitment_entry_id=entry.id,
            ).exists()
        )

    def test_suspended_posting_deadline_move_is_an_extension_with_warning(self):
        entry = self._lapsed_cos_entry()
        entry.status = PositionPosting.EntryStatus.SUSPENDED
        entry.save(update_fields=["status", "updated_at"])
        new_closing = timezone.localdate() + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertTrue(entry.deadline_extended)
        log = AuditLog.objects.get(
            action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
            metadata__recruitment_entry_id=entry.id,
        )
        self.assertEqual(
            log.metadata["previous_status"],
            "suspended",
        )
        message_text = " ".join(
            message.message for message in get_messages(response.wsgi_request)
        )
        self.assertIn(
            "will not reopen to applicants until the status is set back to Active",
            message_text,
        )

    def test_extending_lapsed_deadline_reopens_flags_and_audits(self):
        entry = self._lapsed_cos_entry()
        self.assertFalse(entry.is_open_for_intake)
        previous_closing = entry.closing_date
        new_closing = timezone.localdate() + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertTrue(entry.deadline_extended)
        self.assertTrue(entry.is_open_for_intake)
        log = AuditLog.objects.filter(
            action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
            metadata__recruitment_entry_id=entry.id,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.metadata["previous_closing_date"], previous_closing.isoformat())
        self.assertEqual(log.metadata["new_closing_date"], new_closing.isoformat())
        self.assertTrue(log.metadata["is_extension"])
        self.assertEqual(log.metadata["previous_status"], "active")

    def test_entry_edit_keeps_unchanged_lapsed_closing_date(self):
        entry = self._lapsed_cos_entry()

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, qualification_reference="Notes only."),
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.qualification_reference, "Notes only.")
        self.assertFalse(entry.deadline_extended)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_DEADLINE_CHANGED,
                metadata__recruitment_entry_id=entry.id,
            ).exists()
        )

    def test_closed_vacancy_deadline_extension_blocked_until_reopened(self):
        # RD-7: a manual Close is a deliberate stop. Extending the deadline must
        # not silently undo it — is_open_for_intake requires ACTIVE, so the
        # public page would announce "Deadline extended" while intake stays
        # shut. The edit is refused until the vacancy is reopened.
        entry = self._lapsed_cos_entry()
        entry.status = PositionPosting.EntryStatus.CLOSED
        entry.save(update_fields=["status", "updated_at"])
        previous_closing = entry.closing_date
        new_closing = timezone.localdate() + timedelta(days=7)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reopen it (set it Active) before")
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, previous_closing)

        # Reopen first, then the extension goes through (REV-A behavior intact).
        entry.status = PositionPosting.EntryStatus.ACTIVE
        entry.save(update_fields=["status", "updated_at"])
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            self._entry_edit_payload(entry, closing_date=new_closing.isoformat()),
        )
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.closing_date, new_closing)
        self.assertTrue(entry.is_open_for_intake)

    def test_entry_update_rejects_year_drift_when_generated_code_would_change(self):
        entry = PositionPosting.objects.create(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.DRAFT,
        )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": entry.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.DRAFT,
                "publication_date": "",
                "opening_date": date(self.entry_opening_date().year + 1, 1, 15).isoformat(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Entry Code year segment must match the publication date, opening date, or current year fallback.",
        )

    def test_live_entry_update_blocks_position_reference_change_after_first_submitted_application(self):
        application = self.make_application(self.level1_position)
        self.mark_application_as_submitted_without_case(application)

        self.assertFalse(RecruitmentCase.objects.filter(application=application).exists())

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            {
                "position_reference": self.medical_officer_position.pk,
                "branch": PositionPosting.Branch.PLANTILLA,
                "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.level1_position.opening_date.isoformat(),
                "closing_date": self.level1_position.closing_date.isoformat(),
                "qualification_reference": "Attempted live metadata drift.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Position reference cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.position_reference, self.admin_aide_position)
        self.assertEqual(self.level1_position.title, self.admin_aide_position.position_title)
        self.assertEqual(self.level1_position.level, PositionPosting.Level.LEVEL_1)

    def test_live_entry_save_blocks_opening_date_change_after_linked_case_exists(self):
        application = self.make_application(self.cos_position)
        RecruitmentCase.objects.create(
            application=application,
            current_stage=RecruitmentCase.Stage.SECRETARIAT_REVIEW,
            current_handler_role=RecruitmentUser.Role.SECRETARIAT,
            case_status=RecruitmentCase.CaseStatus.ACTIVE,
        )
        original_opening_date = self.cos_position.opening_date

        self.assertIsNone(application.submitted_at)

        self.cos_position.opening_date = original_opening_date + timedelta(days=1)
        with self.assertRaises(ValidationError) as exc:
            self.cos_position.save()

        self.assertIn(
            "Opening date cannot be changed once the recruitment entry has submitted applications or linked recruitment cases.",
            exc.exception.message_dict["opening_date"],
        )
        self.cos_position.refresh_from_db()
        self.assertEqual(self.cos_position.opening_date, original_opening_date)

    def test_live_entry_allows_non_critical_qualification_reference_update(self):
        application = self.make_application(self.cos_position)
        self.mark_application_as_submitted_without_case(application)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}),
            {
                "position_reference": self.project_assistant_position.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.OPENING_BASED,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.cos_position.opening_date.isoformat(),
                "closing_date": self.cos_position.closing_date.isoformat(),
                "qualification_reference": "Live entry note update is still allowed.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.cos_position.refresh_from_db()
        self.assertEqual(
            self.cos_position.qualification_reference,
            "Live entry note update is still allowed.",
        )

    def test_non_live_entry_save_resyncs_office_metadata_when_position_reference_changes(self):
        self.level1_position.position_reference = self.medical_officer_position
        self.level1_position.save()

        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.title, self.medical_officer_position.position_title)
        self.assertEqual(self.level1_position.level, PositionPosting.Level.LEVEL_2)
        self.assertEqual(self.level1_position.unit, "Regional Office")

    def test_recruitment_entry_creation_requires_position_reference(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        initial_count = PositionPosting.objects.count()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": "",
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Missing reference should fail.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a position reference before creating the recruitment entry.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_recruitment_entry_create_rejects_manual_entry_code_tampering(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        initial_count = PositionPosting.objects.count()
        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": self.project_assistant_position.pk,
                "job_code": "VAC-COS-2099-999",
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "Tamper attempt should fail.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Entry Code is generated automatically for tracking and cannot be edited.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_inactive_position_reference_cannot_be_used_for_entry_creation(self):
        inactive_reference = PositionReference.objects.create(
            position_title="Inactive Reference",
            salary_grade=11,
            level_classification=PositionReference.LevelClassification.FIRST_LEVEL,
            class_id="INACT1",
            os_code="01-GA",
            occupational_service="General Administrative Service",
            occupational_group="Administrative",
            reference_status=PositionReference.ReferenceStatus.VERIFIED_REFERENCE,
            is_active=False,
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        initial_count = PositionPosting.objects.count()

        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": inactive_reference.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.ACTIVE,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice. That choice is not one of the available choices.")
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_incomplete_position_reference_does_not_invent_routing_metadata(self):
        incomplete_reference = PositionReference.objects.create(
            position_title="Incomplete Reference",
            reference_status=PositionReference.ReferenceStatus.INCOMPLETE_REFERENCE,
            is_active=True,
            position_code="POS-999",
            notes="Synthetic incomplete record for test coverage.",
        )
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        initial_count = PositionPosting.objects.count()

        response = client.post(
            reverse("recruitment-entry-create"),
            {
                "position_reference": incomplete_reference.pk,
                "branch": PositionPosting.Branch.COS,
                "intake_mode": PositionPosting.IntakeMode.CONTINUOUS,
                "status": PositionPosting.EntryStatus.DRAFT,
                "publication_date": "",
                "opening_date": self.entry_opening_date_string(),
                "closing_date": "",
                "qualification_reference": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "This position reference is missing the level classification needed for assignment.",
        )
        self.assertEqual(PositionPosting.objects.count(), initial_count)

    def test_non_entry_manager_cannot_access_entry_management(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        response = client.get(reverse("recruitment-entry-list"))
        self.assertEqual(response.status_code, 403)

    def test_entry_status_change_is_audited(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.SUSPENDED},
            ),
            {"reason": "Pause while the office reviews the schedule."},
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)
        self.assertFalse(self.level1_position.is_active)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
            ).exists()
        )

    def test_entry_edit_ignores_forged_early_close_status(self):
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertIsNotNone(self.level1_position.close_blocked_until)
        self.assertFalse(self.level1_position.applicant_pool_is_finalized)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            self._entry_edit_payload(
                self.level1_position,
                status=PositionPosting.EntryStatus.CLOSED,
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertFalse(self.level1_position.applicant_pool_is_finalized)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_id=self.level1_position.id,
            ).exists()
        )

    def test_entry_edit_ignores_forged_cancelled_status_without_resolving_cases(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        application.case.refresh_from_db()
        previous_application_status = application.status
        previous_case_status = application.case.case_status
        previous_case_stage = application.case.current_stage
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)

        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            self._entry_edit_payload(
                self.level1_position,
                status=PositionPosting.EntryStatus.CANCELLED,
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        application.refresh_from_db()
        application.case.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertEqual(application.status, previous_application_status)
        self.assertEqual(application.case.case_status, previous_case_status)
        self.assertEqual(application.case.current_stage, previous_case_stage)
        self.assertFalse(application.case.is_stage_locked)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_CANCELLED,
                metadata__entry_id=self.level1_position.id,
            ).exists()
        )

    def test_closed_entry_is_removed_from_management_list(self):
        # WF-07: closing is only allowed once the publication period has run — move the
        # closing date to today so this close is legitimate.
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=timezone.localdate(),
            updated_at=timezone.now(),
        )
        self.level1_position.refresh_from_db()
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.CLOSED},
            ),
            {"reason": "The publication period has ended."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.CLOSED)
        self.assertFalse(self.level1_position.is_active)
        self.assertNotContains(response, self.level1_position.job_code)
        self.assertContains(response, self.level2_position.job_code)

    def test_close_is_blocked_before_the_publication_period_ends(self):
        # WF-07 (HARD): a vacancy with a future closing date cannot be closed early —
        # an early close would cut the published window and unlock deliberation/CAR
        # for an incomplete pool. Suspend stays available.
        self.assertGreater(self.level1_position.closing_date, timezone.localdate())

        with self.assertRaisesMessage(
            ValidationError,
            "It can't be closed before then. Use Suspend if applications must stop",
        ):
            update_recruitment_entry_status(
                self.level1_position,
                self.hrm_chief,
                PositionPosting.EntryStatus.CLOSED,
            )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)

        # The view path surfaces the same block as a message, without committing.
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.CLOSED},
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "It can&#x27;t be closed before then.")
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)

        # Suspend still works at any time.
        update_recruitment_entry_status(
            self.level1_position,
            self.hrm_chief,
            PositionPosting.EntryStatus.SUSPENDED,
            reason="Pause while the office reviews the schedule.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)

    def test_status_service_blocks_transition_when_entry_fails_full_validation(self):
        PositionPosting.objects.filter(pk=self.level1_position.pk).update(
            closing_date=self.level1_position.opening_date - timedelta(days=1),
            updated_at=timezone.now(),
        )
        self.level1_position.refresh_from_db()

        with self.assertRaises(ValidationError) as exc:
            update_recruitment_entry_status(
                self.level1_position,
                self.hrm_chief,
                PositionPosting.EntryStatus.SUSPENDED,
                reason="Testing model validation.",
            )

        self.assertIn(
            "Closing date cannot be earlier than opening date.",
            exc.exception.message_dict["closing_date"],
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
                metadata__new_status=PositionPosting.EntryStatus.SUSPENDED,
            ).exists()
        )

    def test_status_view_blocks_transition_when_linked_position_reference_is_inactive(self):
        self.admin_aide_position.is_active = False
        self.admin_aide_position.save(update_fields=["is_active", "updated_at"])

        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={"pk": self.level1_position.pk, "status": PositionPosting.EntryStatus.SUSPENDED},
            ),
            {"reason": "Pause while the position reference is reviewed."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Inactive position references cannot be used for recruitment entries.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.ACTIVE)
        self.assertTrue(self.level1_position.is_active)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
                metadata__entry_code=self.level1_position.job_code,
                metadata__new_status=PositionPosting.EntryStatus.SUSPENDED,
            ).exists()
        )

    def _record_exam_schedule_for_extension_policy(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        return ExamSchedule.objects.create(
            application=application,
            recruitment_case=application.case,
            recruitment_entry=self.level1_position,
            review_stage=application.case.current_stage,
            scheduled_by=self.secretariat,
            scheduled_for=timezone.now() + timedelta(days=2),
            venue="Regional Office Testing Room",
        )

    def test_default_policy_blocks_publication_extension_after_exam_is_scheduled(self):
        self._record_exam_schedule_for_extension_policy()
        previous_closing_date = self.level1_position.closing_date
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            self._entry_edit_payload(
                self.level1_position,
                closing_date=(previous_closing_date + timedelta(days=2)).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "The examination for this vacancy has already been scheduled. Extending the "
            "publication would create a second cohort; cancel and re-publish instead.",
        )
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.closing_date, previous_closing_date)

    def test_policy_can_allow_publication_extension_after_exam_is_scheduled(self):
        self._record_exam_schedule_for_extension_policy()
        policy = get_office_policy()
        policy.allow_extension_after_exam_scheduled = True
        policy.save(update_fields=["allow_extension_after_exam_scheduled", "updated_at"])
        previous_closing_date = self.level1_position.closing_date
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.level1_position.pk}),
            self._entry_edit_payload(
                self.level1_position,
                closing_date=(previous_closing_date + timedelta(days=2)).isoformat(),
                positions_to_fill="1",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(
            self.level1_position.closing_date, previous_closing_date + timedelta(days=2)
        )
        self.assertTrue(self.level1_position.deadline_extended)

    def test_default_policy_hides_and_forces_cos_positions_to_fill_to_one(self):
        PositionPosting.objects.filter(pk=self.cos_position.pk).update(positions_to_fill=4)
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        edit = client.get(reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}))
        self.assertEqual(edit.status_code, 200)
        self.assertNotContains(edit, 'name="positions_to_fill"')
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}),
            self._entry_edit_payload(self.cos_position, positions_to_fill="7"),
        )

        self.assertEqual(response.status_code, 302)
        self.cos_position.refresh_from_db()
        self.assertEqual(self.cos_position.positions_to_fill, 1)

    def test_policy_can_expose_and_store_cos_positions_to_fill(self):
        policy = get_office_policy()
        policy.cos_positions_multi_slot_allowed = True
        policy.save(update_fields=["cos_positions_multi_slot_allowed", "updated_at"])
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        edit = client.get(reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}))
        self.assertEqual(edit.status_code, 200)
        self.assertContains(edit, 'name="positions_to_fill"')
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": self.cos_position.pk}),
            self._entry_edit_payload(self.cos_position, positions_to_fill="3"),
        )

        self.assertEqual(response.status_code, 302)
        self.cos_position.refresh_from_db()
        self.assertEqual(self.cos_position.positions_to_fill, 3)

    def test_default_policy_requires_and_audits_suspend_reason(self):
        with self.assertRaisesMessage(
            ValidationError, "Record why this vacancy is being suspended or closed."
        ):
            update_recruitment_entry_status(
                self.level1_position,
                self.hrm_chief,
                PositionPosting.EntryStatus.SUSPENDED,
            )

        reason = "Pause while the examination schedule is reviewed."
        update_recruitment_entry_status(
            self.level1_position,
            self.hrm_chief,
            PositionPosting.EntryStatus.SUSPENDED,
            reason=reason,
        )
        audit = AuditLog.objects.get(
            action=AuditLog.Action.RECRUITMENT_ENTRY_STATUS_CHANGED,
            metadata__entry_code=self.level1_position.job_code,
            metadata__new_status=PositionPosting.EntryStatus.SUSPENDED,
        )
        self.assertEqual(audit.metadata["reason"], reason)

    def test_policy_can_restore_reasonless_suspend_and_close_controls(self):
        policy = get_office_policy()
        policy.require_reason_for_suspend_close = False
        policy.save(update_fields=["require_reason_for_suspend_close", "updated_at"])
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        listing = client.get(reverse("recruitment-entry-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertNotContains(listing, "Reason for suspending")
        response = client.post(
            reverse(
                "recruitment-entry-status",
                kwargs={
                    "pk": self.level1_position.pk,
                    "status": PositionPosting.EntryStatus.SUSPENDED,
                },
            )
        )

        self.assertEqual(response.status_code, 302)
        self.level1_position.refresh_from_db()
        self.assertEqual(self.level1_position.status, PositionPosting.EntryStatus.SUSPENDED)

    def test_hrm_chief_can_update_office_policy_and_changes_are_audited(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)

        response = client.post(
            reverse("office-policy"),
            {
                "allow_extension_after_exam_scheduled": "on",
                "allow_resubmission_deadline_extension": "on",
                "interview_absence_decided_by": (
                    OfficePolicy.InterviewAbsenceDecisionOwner.SUPPORT_OFFICE
                ),
                "withdrawal_recorded_by_owning_office": "on",
                "rescind_cutoff": OfficePolicy.RescindCutoff.CONTRACT_RECORDED,
                "require_reason_for_suspend_close": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        policy = get_office_policy()
        self.assertTrue(policy.allow_extension_after_exam_scheduled)
        audit = AuditLog.objects.get(action=AuditLog.Action.OFFICE_POLICY_UPDATED)
        self.assertIn("allow_extension_after_exam_scheduled", audit.metadata["changed_fields"])
        self.assertEqual(
            audit.metadata["changes"]["allow_extension_after_exam_scheduled"],
            {"old": False, "new": True},
        )

    def test_non_chief_cannot_view_or_update_office_policy(self):
        for actor in (self.secretariat, self.hrmpsb, self.appointing, self.sysadmin):
            client = Client()
            self.force_login_with_mfa(client, actor)
            self.assertEqual(client.get(reverse("office-policy")).status_code, 403)
            self.assertEqual(client.post(reverse("office-policy"), {}).status_code, 403)


class VacancyAssessmentWeightsTests(BaseRecruitmentTestCase):
    def test_assessment_weights_or_default_returns_unsaved_defaults(self):
        # A vacancy with no weights row falls back to the office defaults (exam 60/40,
        # CAR 40/20/40) without persisting anything.
        weights = self.level1_position.assessment_weights_or_default
        self.assertIsNone(weights.pk)
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))
        self.assertEqual(weights.exam_technical_weight, Decimal("40.00"))
        self.assertEqual(weights.ete_weight, Decimal("40.00"))
        self.assertEqual(weights.exam_weight, Decimal("20.00"))
        self.assertEqual(weights.interview_weight, Decimal("40.00"))

    def test_get_or_create_persists_a_single_row(self):
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        self.assertIsNotNone(weights.pk)
        self.assertEqual(
            VacancyAssessmentWeights.objects.filter(
                recruitment_entry=self.level1_position
            ).count(),
            1,
        )
        # The reverse accessor now returns the persisted row.
        self.assertEqual(
            self.level1_position.assessment_weights_or_default.pk, weights.pk
        )

    def test_lock_marks_locked_and_is_idempotent(self):
        weights = lock_vacancy_assessment_weights(self.level1_position)
        self.assertTrue(weights.is_locked)
        self.assertIsNotNone(weights.locked_at)
        first_locked_at = weights.locked_at
        again = lock_vacancy_assessment_weights(self.level1_position)
        self.assertEqual(again.locked_at, first_locked_at)

    def test_exam_weights_must_sum_to_100(self):
        weights = VacancyAssessmentWeights(
            recruitment_entry=self.level1_position,
            exam_general_weight=Decimal("70.00"),
            exam_technical_weight=Decimal("40.00"),
        )
        with self.assertRaises(ValidationError):
            weights.full_clean()

    def test_car_weights_must_sum_to_100(self):
        weights = VacancyAssessmentWeights(
            recruitment_entry=self.level1_position,
            ete_weight=Decimal("50.00"),
            exam_weight=Decimal("30.00"),
            interview_weight=Decimal("30.00"),
        )
        with self.assertRaises(ValidationError):
            weights.full_clean()

    def test_new_plantilla_vacancy_seeds_default_weights(self):
        entry = PositionPosting(
            position_reference=self.admin_aide_position,
            branch=PositionPosting.Branch.PLANTILLA,
            level=PositionPosting.Level.LEVEL_1,
            item_number="OSEC-DOH-AA6-9-2026",
            intake_mode=PositionPosting.IntakeMode.FIXED_PERIOD,
            status=PositionPosting.EntryStatus.ACTIVE,
            closing_date=self.level1_closing_date(),
        )
        persist_recruitment_entry(entry, self.secretariat, [])
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=entry)
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.DRAFT)

    def test_new_cos_vacancy_has_no_weights_row(self):
        entry = PositionPosting(
            position_reference=self.project_assistant_position,
            branch=PositionPosting.Branch.COS,
            level=PositionPosting.Level.LEVEL_1,
            intake_mode=PositionPosting.IntakeMode.CONTINUOUS,
            status=PositionPosting.EntryStatus.ACTIVE,
        )
        persist_recruitment_entry(entry, self.secretariat, [])
        self.assertFalse(
            VacancyAssessmentWeights.objects.filter(recruitment_entry=entry).exists()
        )

    def test_exam_score_uses_per_vacancy_default_weights(self):
        application = self.make_application(self.level1_position)
        record = ExamRecord(
            application=application,
            exam_type=ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            general_score=Decimal("90"),
            technical_score=Decimal("80"),
        )
        # 90 * 0.60 + 80 * 0.40 = 86 with the seeded defaults.
        self.assertEqual(record.calculate_policy_score(), Decimal("86.00"))

    def test_exam_score_follows_per_vacancy_weights(self):
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        weights.exam_general_weight = Decimal("50.00")
        weights.exam_technical_weight = Decimal("50.00")
        weights.full_clean()
        weights.save()
        application = self.make_application(self.level1_position)
        record = ExamRecord(
            application=application,
            exam_type=ExamRecord.ExamType.TECHNICAL_PRACTICAL,
            general_score=Decimal("90"),
            technical_score=Decimal("80"),
        )
        # 90 * 0.50 + 80 * 0.50 = 85 — the formula tracks this vacancy's weights.
        self.assertEqual(record.calculate_policy_score(), Decimal("85.00"))

    def test_secretariat_can_view_and_update_vacancy_weights(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        self.assertEqual(client.get(url).status_code, 200)
        response = client.post(
            url,
            {
                "ete_weight": "50",
                "exam_weight": "20",
                "interview_weight": "30",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.ete_weight, Decimal("50.00"))
        self.assertEqual(weights.interview_weight, Decimal("30.00"))
        self.assertEqual(weights.exam_general_weight, Decimal("55.00"))
        self.assertEqual(weights.updated_by_id, self.secretariat.id)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.ASSESSMENT_WEIGHTS_UPDATED,
                actor=self.secretariat,
            ).exists()
        )

    def test_weights_page_requires_entry_manager_role(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrmpsb)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        self.assertEqual(client.get(url).status_code, 403)

    def test_weights_view_rejects_sums_that_are_not_100(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {
                "ete_weight": "40",
                "exam_weight": "20",
                "interview_weight": "40",
                "exam_general_weight": "70",
                "exam_technical_weight": "40",
            },
        )
        self.assertEqual(response.status_code, 200)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        # Rejected — the stored weights stay at the seeded defaults.
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))

    def test_cos_vacancy_weights_page_renders_and_edits(self):
        # RD-5: post-RD-2 the COS exam overall is weight-computed too, so COS
        # entries see and edit their weights (the old view hard-blocked them).
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.cos_position.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exam score split")
        self.assertTrue(
            VacancyAssessmentWeights.objects.filter(
                recruitment_entry=self.cos_position
            ).exists()
        )

    def test_locked_weights_cannot_be_edited_through_the_view(self):
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {
                "ete_weight": "50",
                "exam_weight": "20",
                "interview_weight": "30",
                "exam_general_weight": "55",
                "exam_technical_weight": "45",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        # Refused — scoring has started, so the weights stay at the defaults.
        self.assertEqual(weights.exam_general_weight, Decimal("60.00"))

    def test_locked_weights_can_be_unlocked_with_an_audited_reason(self):
        # SL-00: the lock is deliberate, but an encoding error in the split still has to be
        # correctable — through a reason-bearing, audited unlock, never a quiet edit.
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(
            url,
            {"operation": "unlock", "reason": "Exam weight was encoded as 20 instead of 30."},
        )
        self.assertEqual(response.status_code, 302)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.DRAFT)
        self.assertIsNone(weights.locked_at)
        entry = AuditLog.objects.filter(
            action=AuditLog.Action.ASSESSMENT_WEIGHTS_UNLOCKED,
            actor=self.secretariat,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.metadata["reason"],
            "Exam weight was encoded as 20 instead of 30.",
        )
        # The split in force at the moment of the unlock is preserved on the audit entry.
        self.assertEqual(entry.metadata["split_at_unlock"]["ete_weight"], "40.00")
        # And the row is genuinely editable again.
        response = client.post(
            url,
            {
                "ete_weight": "40",
                "exam_weight": "30",
                "interview_weight": "30",
                "exam_general_weight": "60",
                "exam_technical_weight": "40",
            },
        )
        self.assertEqual(response.status_code, 302)
        weights.refresh_from_db()
        self.assertEqual(weights.exam_weight, Decimal("30.00"))

    def test_unlocking_weights_without_a_reason_is_refused(self):
        lock_vacancy_assessment_weights(self.level1_position)
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.post(url, {"operation": "unlock", "reason": "   "})
        self.assertEqual(response.status_code, 200)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertEqual(weights.status, VacancyAssessmentWeights.Status.LOCKED)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.ASSESSMENT_WEIGHTS_UNLOCKED
            ).exists()
        )

    def test_unlock_service_refuses_an_already_editable_row(self):
        with self.assertRaises(ValidationError):
            unlock_vacancy_assessment_weights(
                self.level1_position,
                self.secretariat,
                "Nothing to correct.",
            )

    def test_weights_page_states_the_split_and_flags_a_non_standard_one(self):
        # SL-00: a deviation from the office standard is legitimate but must be visible.
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        url = reverse("vacancy-assessment-weights", args=[self.level1_position.pk])
        response = client.get(url)
        self.assertContains(response, "ETE 40% / Examination 20% / Interview 40%")
        self.assertContains(response, "This is the office standard.")
        weights = get_or_create_vacancy_assessment_weights(self.level1_position)
        weights.ete_weight = Decimal("50.00")
        weights.exam_weight = Decimal("20.00")
        weights.interview_weight = Decimal("30.00")
        weights.save()
        response = client.get(url)
        self.assertContains(response, "ETE 50% / Examination 20% / Interview 30%")
        self.assertContains(response, "not</strong> the office standard", html=False)

    def test_finalizing_an_exam_locks_the_vacancy_weights(self):
        application = self.make_application(self.level1_position)
        self.verify_application_for_submission(application)
        with self.captureOnCommitCallbacks(execute=True):
            submit_application(application, self.applicant)
        application.refresh_from_db()
        self.finalize_screening_for_current_stage(application, self.secretariat)
        self.finalize_exam_for_current_stage(application, self.secretariat)
        weights = VacancyAssessmentWeights.objects.get(recruitment_entry=self.level1_position)
        self.assertTrue(weights.is_locked)


class PositionDocumentRequirementTests(BaseRecruitmentTestCase):
    def _branch_selections(self, branch, *, applies_overrides=None, level_overrides=None):
        """Build a full selections list for a branch, defaulting to the standard set."""
        applies_overrides = applies_overrides or {}
        level_overrides = level_overrides or {}
        selections = []
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[branch]:
            code = requirement.code
            selections.append(
                {
                    "code": code,
                    "applies": applies_overrides.get(code, True),
                    "is_required": level_overrides.get(code, requirement.is_required),
                }
            )
        return selections

    def test_set_position_document_requirements_configures_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA,
                applies_overrides={DIPLOMA: False},
                level_overrides={TRAINING_CERTIFICATES: True},
            ),
            self.secretariat,
        )

        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertNotIn(DIPLOMA, codes)
        required_codes = [
            requirement.code for requirement in get_required_applicant_document_requirements(posting)
        ]
        self.assertIn(TRAINING_CERTIFICATES, required_codes)
        self.assertNotIn(DIPLOMA, required_codes)
        self.assertFalse(posting.document_requirements.filter(document_code=DIPLOMA).exists())
        self.assertTrue(
            posting.document_requirements.get(document_code=TRAINING_CERTIFICATES).is_required
        )

    def test_minimum_required_documents_cannot_be_dropped_or_optionalized(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA,
                applies_overrides={SIGNED_COVER_LETTER: False},
                level_overrides={PERSONAL_DATA_SHEET: False},
            ),
            self.secretariat,
        )
        self.assertTrue(
            posting.document_requirements.get(document_code=SIGNED_COVER_LETTER).is_required
        )
        self.assertTrue(
            posting.document_requirements.get(document_code=PERSONAL_DATA_SHEET).is_required
        )

    def test_performance_rating_is_offered_only_and_stays_conditional(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        offered = [
            requirement
            for requirement in get_applicant_document_requirements(posting)
            if requirement.code == PERFORMANCE_RATING
        ]
        self.assertEqual(len(offered), 1)
        self.assertFalse(offered[0].is_required)
        self.assertTrue(offered[0].conditional_on_performance_rating)

        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={PERFORMANCE_RATING: False}
            ),
            self.secretariat,
        )
        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertNotIn(PERFORMANCE_RATING, codes)

    def test_resolver_skips_codes_invalid_for_branch(self):
        posting = self.cos_position
        PositionDocumentRequirement.objects.create(
            posting=posting, document_code=SIGNED_COVER_LETTER, is_required=True, order=1
        )
        PositionDocumentRequirement.objects.create(
            posting=posting, document_code=PERFORMANCE_RATING, is_required=True, order=2
        )
        codes = [requirement.code for requirement in get_applicant_document_requirements(posting)]
        self.assertIn(SIGNED_COVER_LETTER, codes)
        self.assertNotIn(PERFORMANCE_RATING, codes)

    def test_cos_posting_never_stores_performance_rating(self):
        posting = self.cos_position
        selections = self._branch_selections(PositionPosting.Branch.COS)
        selections.append({"code": PERFORMANCE_RATING, "applies": True, "is_required": True})
        set_position_document_requirements(posting, selections, self.secretariat)
        self.assertFalse(
            posting.document_requirements.filter(document_code=PERFORMANCE_RATING).exists()
        )

    def test_legacy_posting_without_rows_uses_branch_catalog(self):
        posting = self.level1_position
        self.assertEqual(posting.document_requirements.count(), 0)
        self.assertEqual(
            get_applicant_document_requirements(posting),
            APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA],
        )

    def test_live_configuration_may_only_relax_an_existing_required_document(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        posting.refresh_from_db()
        self.assertTrue(posting.is_live_for_metadata_lock)

        selections = self._branch_selections(
            PositionPosting.Branch.PLANTILLA,
            level_overrides={DIPLOMA: False},
        )
        set_position_document_requirements(posting, selections, self.secretariat)
        self.assertFalse(
            posting.document_requirements.get(document_code=DIPLOMA).is_required
        )
        audit = AuditLog.objects.filter(
            action=AuditLog.Action.RECRUITMENT_ENTRY_UPDATED,
            metadata__entry_id=posting.id,
            metadata__relaxed_codes__isnull=False,
        ).latest("created_at")
        self.assertEqual(audit.metadata["relaxed_codes"], [DIPLOMA])

        with self.assertRaisesMessage(ValidationError, "cannot be made required"):
            set_position_document_requirements(
                posting,
                self._branch_selections(PositionPosting.Branch.PLANTILLA),
                self.secretariat,
            )

        with self.assertRaisesMessage(ValidationError, "cannot be added or removed"):
            set_position_document_requirements(
                posting,
                self._branch_selections(
                    PositionPosting.Branch.PLANTILLA,
                    applies_overrides={TRAINING_CERTIFICATES: False},
                    level_overrides={DIPLOMA: False},
                ),
                self.secretariat,
            )

        existing_row = posting.document_requirements.first()
        existing_row.is_required = not existing_row.is_required
        with self.assertRaises(ValidationError):
            existing_row.save()

    def test_live_configuration_refuses_adding_a_document(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA,
                applies_overrides={PERFORMANCE_RATING: False},
            ),
            self.secretariat,
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)

        with self.assertRaisesMessage(ValidationError, "cannot be added or removed"):
            set_position_document_requirements(
                posting,
                self._branch_selections(PositionPosting.Branch.PLANTILLA),
                self.secretariat,
            )

    def test_submit_gate_uses_configured_required_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={DIPLOMA: False}
            ),
            self.secretariat,
        )
        application = self.make_application(posting)
        required_codes = [
            requirement.code
            for requirement in get_required_applicant_document_requirements(application)
        ]
        self.assertNotIn(DIPLOMA, required_codes)
        self.assertEqual(get_missing_required_applicant_document_requirements(application), [])

    def test_screening_review_uses_configured_document_set(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(
                PositionPosting.Branch.PLANTILLA, applies_overrides={DIPLOMA: False}
            ),
            self.secretariat,
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        screening_record = self.finalize_screening_for_current_stage(application, self.secretariat)
        self.assertEqual(
            screening_record.document_reviews.count(),
            len(get_applicant_document_requirements(application)),
        )
        self.assertFalse(screening_record.document_reviews.filter(document_key=DIPLOMA).exists())

    def test_create_view_persists_document_configuration(self):
        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        payload = {
            "position_reference": self.admin_aide_position.pk,
            "branch": PositionPosting.Branch.PLANTILLA,
            "item_number": "OSEC-DOH-AA6-9-2026",
            "intake_mode": PositionPosting.IntakeMode.FIXED_PERIOD,
            "status": PositionPosting.EntryStatus.ACTIVE,
            "publication_date": "",
            "opening_date": self.entry_opening_date().isoformat(),
            "closing_date": "",
            "qualification_reference": "Education: Completion of two years of college\nTraining: None required\nExperience: None required\nPer-vacancy document configuration create test.",
        }
        for requirement in APPLICANT_DOCUMENT_REQUIREMENTS_BY_BRANCH[PositionPosting.Branch.PLANTILLA]:
            code = requirement.code
            if code == DIPLOMA:
                continue  # leave unchecked so it is dropped
            payload[f"doc_applies__{code}"] = "on"
            if code not in MIN_REQUIRED_DOCUMENT_CODES and not requirement.conditional_on_performance_rating:
                payload[f"doc_level__{code}"] = "required"

        response = client.post(reverse("recruitment-entry-create"), payload)
        self.assertEqual(response.status_code, 302)
        created = PositionPosting.objects.get(
            qualification_reference="Education: Completion of two years of college\nTraining: None required\nExperience: None required\nPer-vacancy document configuration create test."
        )
        configured_codes = set(
            created.document_requirements.values_list("document_code", flat=True)
        )
        self.assertNotIn(DIPLOMA, configured_codes)
        self.assertIn(SIGNED_COVER_LETTER, configured_codes)
        self.assertIn(PERSONAL_DATA_SHEET, configured_codes)

    def test_update_view_renders_locked_documents_for_live_posting(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting, self._branch_selections(PositionPosting.Branch.PLANTILLA), self.secretariat
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        application.refresh_from_db()
        posting.refresh_from_db()
        self.assertIsNotNone(application.submitted_at)
        self.assertTrue(posting.is_live_for_metadata_lock)

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.get(reverse("recruitment-entry-update", kwargs={"pk": posting.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["documents_form"].locked)
        self.assertContains(response, '<div class="alert alert-warning">', count=1)
        self.assertContains(response, "disabled")

    def test_update_view_can_relax_one_live_document_without_changing_the_rest(self):
        posting = self.level1_position
        set_position_document_requirements(
            posting,
            self._branch_selections(PositionPosting.Branch.PLANTILLA),
            self.secretariat,
        )
        application = self.make_application(posting)
        self.verify_application_for_submission(application)
        submit_application(application, self.applicant)
        before_codes = set(
            posting.document_requirements.values_list("document_code", flat=True)
        )
        payload = {
            "position_reference": posting.position_reference_id,
            "item_number": posting.item_number,
            "branch": posting.branch,
            "intake_mode": posting.intake_mode,
            "publication_date": posting.publication_date.isoformat()
            if posting.publication_date
            else "",
            "opening_date": posting.opening_date.isoformat() if posting.opening_date else "",
            "closing_date": posting.closing_date.isoformat() if posting.closing_date else "",
            "qualification_reference": posting.qualification_reference,
        }
        for row in posting.document_requirements.all():
            payload[f"doc_applies__{row.document_code}"] = "on"
            if row.document_code not in {PERFORMANCE_RATING}:
                payload[f"doc_level__{row.document_code}"] = (
                    "optional" if row.document_code == DIPLOMA else "required"
                )

        client = Client()
        self.force_login_with_mfa(client, self.secretariat)
        response = client.post(
            reverse("recruitment-entry-update", kwargs={"pk": posting.pk}),
            payload,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(posting.document_requirements.values_list("document_code", flat=True)),
            before_codes,
        )
        self.assertFalse(
            posting.document_requirements.get(document_code=DIPLOMA).is_required
        )

    def test_office_policy_page_uses_plain_language_labels(self):
        client = Client()
        self.force_login_with_mfa(client, self.hrm_chief)
        response = client.get(reverse("office-policy"))
        self.assertEqual(response.status_code, 200)
        for label in (
            "Allow closing-date extension after the exam is scheduled",
            "Resubmission deadline may be extended",
            "Interview absences are decided by",
            "Owning office may record withdrawals",
            "When rescission is no longer allowed",
            "COS postings may fill multiple positions",
            "Suspend and Close require a reason",
        ):
            self.assertContains(response, label)
