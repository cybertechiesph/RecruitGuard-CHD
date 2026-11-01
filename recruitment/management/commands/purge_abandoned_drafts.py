from django.core.management.base import BaseCommand

from recruitment.services import (
    ABANDONED_DRAFT_DEFAULT_DAYS,
    find_abandoned_drafts,
    purge_abandoned_draft_files,
)


class Command(BaseCommand):
    help = (
        "Remove the uploaded files of applications that were never submitted, once their "
        "vacancy has closed and they have been untouched for a while. The drafts, file names, "
        "fingerprints and audit trail are kept. Run daily or weekly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=ABANDONED_DRAFT_DEFAULT_DAYS,
            help="Only drafts untouched for at least this many days (default: %(default)s).",
        )
        parser.add_argument("--dry-run", action="store_true", help="List them without removing anything.")

    def handle(self, *args, **options):
        drafts = find_abandoned_drafts(older_than_days=max(1, options["days"]))
        if options["dry_run"]:
            for application in drafts:
                self.stdout.write(f"would clean draft #{application.pk} ({application.position.title})")
            self.stdout.write(self.style.SUCCESS(f"{len(drafts)} draft(s) would be cleaned."))
            return
        removed = sum(purge_abandoned_draft_files(application) for application in drafts)
        self.stdout.write(
            self.style.SUCCESS(f"Cleaned {len(drafts)} draft(s); removed {removed} stored file(s).")
        )
