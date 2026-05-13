from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recruitment.services import repair_auto_advance_workflow_boundaries


class Command(BaseCommand):
    help = "Repair active cases already past an automatic workflow boundary."

    def add_arguments(self, parser):
        parser.add_argument(
            "--actor-username",
            default="",
            help="Optional internal username to record as the cleanup actor.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be repaired without saving changes.",
        )

    def handle(self, *args, **options):
        actor = None
        actor_username = (options.get("actor_username") or "").strip()
        if actor_username:
            User = get_user_model()
            try:
                actor = User.objects.get(username=actor_username)
            except User.DoesNotExist as exc:
                raise CommandError(f"No user exists with username '{actor_username}'.") from exc

        if options["dry_run"]:
            with transaction.atomic():
                repaired = repair_auto_advance_workflow_boundaries(actor=actor)
                transaction.set_rollback(True)
        else:
            repaired = repair_auto_advance_workflow_boundaries(actor=actor)

        if not repaired:
            self.stdout.write(self.style.SUCCESS("No workflow boundary repairs needed."))
            return

        for item in repaired:
            self.stdout.write(
                "{reference}: {from_role}/{from_stage} -> {to_role}/{to_stage} ({reason})".format(
                    **item
                )
            )
        suffix = "would be repaired" if options["dry_run"] else "repaired"
        self.stdout.write(self.style.SUCCESS(f"{len(repaired)} case(s) {suffix}."))
