from django.core.management.base import BaseCommand
from django.db import transaction

from recruitment.services import emit_deadline_approaching_notifications


class Command(BaseCommand):
    help = "Emit in-app notifications for active cases whose postings close within 24 hours."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many notifications would be emitted without saving changes.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            with transaction.atomic():
                notifications = emit_deadline_approaching_notifications()
                transaction.set_rollback(True)
        else:
            notifications = emit_deadline_approaching_notifications()

        suffix = "would be emitted" if options["dry_run"] else "emitted"
        self.stdout.write(self.style.SUCCESS(f"{len(notifications)} notification(s) {suffix}."))
