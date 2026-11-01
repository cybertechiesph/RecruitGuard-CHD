import logging
import smtplib

from django.core.management.base import BaseCommand

from recruitment.services import (
    RESUBMISSION_REMINDER_DEFAULT_DAYS_BEFORE,
    find_resubmissions_due_for_reminder,
    send_resubmission_reminder,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Email applicants whose corrected documents are due soon. Each resubmission deadline "
        "is reminded once (a new or extended deadline gets its own reminder). Run daily."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-before",
            type=int,
            default=RESUBMISSION_REMINDER_DEFAULT_DAYS_BEFORE,
            help="Remind resubmissions due within this many days (default: %(default)s).",
        )
        parser.add_argument("--dry-run", action="store_true", help="List them without sending.")

    def handle(self, *args, **options):
        records = find_resubmissions_due_for_reminder(days_before=max(0, options["days_before"]))
        if options["dry_run"]:
            for record in records:
                self.stdout.write(
                    f"would remind {record.application.reference_number} "
                    f"(due {record.resubmission_deadline:%Y-%m-%d})"
                )
            self.stdout.write(self.style.SUCCESS(f"{len(records)} reminder(s) would be sent."))
            return
        sent = failed = 0
        for record in records:
            try:
                send_resubmission_reminder(record)
            except (OSError, smtplib.SMTPException):
                logger.exception("Resubmission reminder failed for screening record %s.", record.pk)
                failed += 1
            else:
                sent += 1
        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent, {failed} failed."))
