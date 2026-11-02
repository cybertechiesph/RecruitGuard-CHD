from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from recruitment.models import PositionPosting, RecruitmentCase, RecruitmentUser
from recruitment.services import workflow_map

START_MARKER = "<!-- workflow-map:start -->"
END_MARKER = "<!-- workflow-map:end -->"
DOC_PATH = Path(settings.BASE_DIR) / "docs" / "workflow-rules.md"


def render_workflow_map():
    """The workflow table from recruitment/services/workflow_map.py, as Markdown."""
    lines = []
    for (branch, level), steps in workflow_map.STEPS.items():
        lines.append(
            f"#### {PositionPosting.Branch(branch).label}, {PositionPosting.Level(level).label}"
        )
        lines.append("")
        lines.append("| # | Step | Stage | Handled by | Case sections |")
        lines.append("|---|---|---|---|---|")
        for number, step in enumerate(steps, start=1):
            owner = workflow_map.stage_owner(step.stage, level)
            sections = ", ".join(workflow_map.SECTION_LABELS[section] for section in step.sections)
            lines.append(
                f"| {number} | {step.label} | {RecruitmentCase.Stage(step.stage).label} "
                f"| {RecruitmentUser.Role(owner).label} | {sections} |"
            )
        lines.append("")
    lines.append(
        "Selection is deliberated at: "
        + "; ".join(
            f"{PositionPosting.Branch(branch).label}: {RecruitmentCase.Stage(stage).label}"
            for branch, stage in workflow_map.DELIBERATION_STAGES.items()
        )
        + "."
    )
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = (
        "Print the workflow table (steps, stages, handlers) from services/workflow_map.py. "
        "With --write, refresh the generated block in docs/workflow-rules.md."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--write",
            action="store_true",
            help="Replace the generated block in docs/workflow-rules.md instead of printing.",
        )

    def handle(self, *args, **options):
        table = render_workflow_map()
        if not options["write"]:
            self.stdout.write(table, ending="")
            return
        text = DOC_PATH.read_text(encoding="utf-8")
        if START_MARKER not in text or END_MARKER not in text:
            raise CommandError(f"{DOC_PATH} has no {START_MARKER} … {END_MARKER} block.")
        head, rest = text.split(START_MARKER, 1)
        _old, tail = rest.split(END_MARKER, 1)
        DOC_PATH.write_text(
            f"{head}{START_MARKER}\n{table}{END_MARKER}{tail}", encoding="utf-8", newline="\n"
        )
        self.stdout.write(f"Updated {DOC_PATH}.")
