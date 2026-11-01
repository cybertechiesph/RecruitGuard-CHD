import logging
import smtplib

from django.core.management.base import BaseCommand

from recruitment.services import (
    DRAFT_REMINDER_DEFAULT_DAYS_BEFORE,
    find_drafts_due_for_reminder,
    send_draft_reminder,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Email applicants whose verified application is still unsubmitted when the vacancy "
        "closes soon. Each draft is reminded once. Run daily (e.g. from cron)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-before",
            type=int,
            default=DRAFT_REMINDER_DEFAULT_DAYS_BEFORE,
            help="Remind drafts whose vacancy closes within this many days (default: %(default)s).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the drafts that would be reminded without sending anything.",
        )

    def handle(self, *args, **options):
        drafts = find_drafts_due_for_reminder(days_before=max(0, options["days_before"]))
        if options["dry_run"]:
            for application in drafts:
                self.stdout.write(
                    f"would remind draft #{application.pk} ({application.position.title}, "
                    f"closes {application.position.closing_date:%Y-%m-%d})"
                )
            self.stdout.write(self.style.SUCCESS(f"{len(drafts)} reminder(s) would be sent."))
            return
        sent = failed = 0
        for application in drafts:
            try:
                send_draft_reminder(application)
            except (OSError, smtplib.SMTPException):
                logger.exception("Draft reminder failed for application %s.", application.pk)
                failed += 1
            else:
                sent += 1
        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent, {failed} failed."))
