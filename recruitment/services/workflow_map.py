"""The recruitment workflow as one table: for each branch and level, the steps a case goes
through, the stage each step happens in and the case sections it covers, plus who owns each
stage. The progress bar, the case page's section order, the handler a case is routed to and
the stage/status pairing all read from here, so changing the workflow means changing this
file. `manage.py print_workflow_map` prints it for docs/workflow-rules.md.

Imports nothing else from this package, so other modules may use it at import time.
"""

from collections import namedtuple

from ..models import PositionPosting, RecruitmentApplication, RecruitmentCase, RecruitmentUser

Branch = PositionPosting.Branch
Level = PositionPosting.Level
Stage = RecruitmentCase.Stage
Status = RecruitmentApplication.Status
Role = RecruitmentUser.Role

# One step of the progress bar. `sections` are the case-page sections worked in that step,
# in order; a step never spans two stages.
Step = namedtuple("Step", "key label stage sections")

STEPS = {
    (Branch.PLANTILLA, Level.LEVEL_1): (
        Step("screening", "Screening", Stage.SECRETARIAT_REVIEW, ("screening",)),
        Step("exam", "Exam", Stage.SECRETARIAT_REVIEW, ("exam",)),
        Step("interview", "Interview", Stage.HRMPSB_REVIEW, ("interview",)),
        Step("car", "Comparative Assessment", Stage.HRMPSB_REVIEW, ("car",)),
        Step("decision", "Decision", Stage.APPOINTING_AUTHORITY_REVIEW, ("decision",)),
        Step("completion", "Completion", Stage.COMPLETION, ("completion",)),
    ),
    (Branch.PLANTILLA, Level.LEVEL_2): (
        Step("screening", "Screening", Stage.HRM_CHIEF_REVIEW, ("screening",)),
        Step("exam", "Exam", Stage.HRM_CHIEF_REVIEW, ("exam",)),
        Step("interview", "Interview", Stage.HRMPSB_REVIEW, ("interview",)),
        Step("car", "Comparative Assessment", Stage.HRMPSB_REVIEW, ("car",)),
        Step("decision", "Decision", Stage.APPOINTING_AUTHORITY_REVIEW, ("decision",)),
        Step("completion", "Completion", Stage.COMPLETION, ("completion",)),
    ),
    # COS Level 1: the Secretariat screens and examines, then the HRM Chief re-checks both
    # before interviewing and selecting. Level 2 starts with the Chief, so there is no re-check.
    (Branch.COS, Level.LEVEL_1): (
        Step("screening", "Screening", Stage.SECRETARIAT_REVIEW, ("screening",)),
        Step("exam", "Exam", Stage.SECRETARIAT_REVIEW, ("exam",)),
        Step("chief_recheck", "HRM Chief re-check", Stage.HRM_CHIEF_REVIEW, ("screening", "exam")),
        Step("interview", "Interview", Stage.HRM_CHIEF_REVIEW, ("interview",)),
        Step("selection", "Selection", Stage.HRM_CHIEF_REVIEW, ("deliberation", "decision")),
        Step("completion", "Completion", Stage.COMPLETION, ("completion",)),
    ),
    (Branch.COS, Level.LEVEL_2): (
        Step("screening", "Screening", Stage.HRM_CHIEF_REVIEW, ("screening",)),
        Step("exam", "Exam", Stage.HRM_CHIEF_REVIEW, ("exam",)),
        Step("interview", "Interview", Stage.HRM_CHIEF_REVIEW, ("interview",)),
        Step("selection", "Selection", Stage.HRM_CHIEF_REVIEW, ("deliberation", "decision")),
        Step("completion", "Completion", Stage.COMPLETION, ("completion",)),
    ),
}

# Who handles a case while it is in a stage. Completion goes back to the office that owns the
# level (Level 1 the Secretariat, Level 2 the HRM Chief), the same office that took it in.
STAGE_OWNERS = {
    Stage.SECRETARIAT_REVIEW: Role.SECRETARIAT,
    Stage.HRM_CHIEF_REVIEW: Role.HRM_CHIEF,
    Stage.HRMPSB_REVIEW: Role.HRMPSB_MEMBER,
    Stage.APPOINTING_AUTHORITY_REVIEW: Role.APPOINTING_AUTHORITY,
}
COMPLETION_OWNERS = {
    Level.LEVEL_1: Role.SECRETARIAT,
    Level.LEVEL_2: Role.HRM_CHIEF,
}

# The application status that goes with each stage while the case is open.
STAGE_STATUSES = {
    Stage.SECRETARIAT_REVIEW: Status.SECRETARIAT_REVIEW,
    Stage.HRM_CHIEF_REVIEW: Status.HRM_CHIEF_REVIEW,
    Stage.HRMPSB_REVIEW: Status.HRMPSB_REVIEW,
    Stage.APPOINTING_AUTHORITY_REVIEW: Status.APPOINTING_AUTHORITY_REVIEW,
    Stage.COMPLETION: Status.APPROVED,
}

# Where the selection is deliberated. COS: the HRM Chief, in the Selection step. Plantilla: the
# HRMPSB panel, at its own stage, before the Comparative Assessment Report is finalized.
DELIBERATION_STAGES = {
    Branch.COS: Stage.HRM_CHIEF_REVIEW,
    Branch.PLANTILLA: Stage.HRMPSB_REVIEW,
}

SECTION_LABELS = {
    "overview": "Overview",
    "screening": "Screening",
    "exam": "Exam",
    "interview": "Interview",
    "deliberation": "Deliberation",
    "car": "Comparative Assessment",
    "decision": "Decision",
    "completion": "Completion",
}


def steps(branch, level):
    """The progress steps of a branch and level, in order."""
    return STEPS[(branch, level)]


def intake_stage(branch, level):
    """The stage a submitted application starts in."""
    return steps(branch, level)[0].stage


def stage_owner(stage, level=None):
    """Who handles a case in this stage. Completion needs the level; without it, no one."""
    if stage == Stage.COMPLETION:
        return COMPLETION_OWNERS.get(level, "")
    return STAGE_OWNERS.get(stage, "")


def status_for_stage(stage):
    return STAGE_STATUSES.get(stage, "")


def review_stage_for_status(status):
    """The review stage an application status puts the case in ('' for any other status)."""
    for stage, stage_status in STAGE_STATUSES.items():
        if stage_status == status and stage != Stage.COMPLETION:
            return stage
    return ""


def section_sequence(branch, stage):
    """The case-page sections worked in this stage, in order. A closed case shows its
    completion section."""
    if stage == Stage.CLOSED:
        stage = Stage.COMPLETION
    sequence = []
    for level in Level.values:
        for step in steps(branch, level):
            if step.stage == stage:
                sequence.extend(section for section in step.sections if section not in sequence)
    return sequence


def step_key(branch, level, stage, section):
    """Which progress step a case is on, from its stage and current section."""
    if stage == Stage.CLOSED:
        stage = Stage.COMPLETION
    path = steps(branch, level)
    at_stage = [step for step in path if step.stage == stage]
    for step in at_stage:
        if section in step.sections:
            return step.key
    if at_stage:
        return at_stage[0].key
    # A stage outside this branch's path (older records): place the case by its section.
    for step in path:
        if section in step.sections:
            return step.key
    return path[0].key


def stages_with_section(section):
    """Every stage, in any branch, in which this section is worked."""
    return {
        step.stage for path in STEPS.values() for step in path if section in step.sections
    }


def stages_followed_by_intake_office(level):
    """The open stages of a level's cases that the office which took them in does not own:
    where that office can only follow the case."""
    owner = stage_owner(intake_stage(Branch.PLANTILLA, level), level)
    return {
        step.stage
        for branch in Branch.values
        for step in steps(branch, level)
        if step.stage != Stage.COMPLETION and stage_owner(step.stage, level) != owner
    }
