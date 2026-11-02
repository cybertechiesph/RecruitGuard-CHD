# F15: restructuring the service layer

Plan for the restructuring recommended in `docs/secretariat-final-review-2026-11.md` (finding
F15, section 6). Written 2026-11-03 against `43fe551` (Round B), from a read of the code, not
from memory. The purpose is maintainability for the DOH–CHD CALABARZON IT staff who inherit the
system: a rule should be found by opening the file named after its stage, every case movement
should go through one door, and the workflow should be readable as a table.

Nothing here changes what the system does. Each round lands with the full suite green and is
its own commit, so any round can be stopped or reverted on its own.

## 1. What exists today (measured)

| Piece | Size | Notes |
|---|---|---|
| `recruitment/services.py` | 14,192 lines, 421 functions, 79 module constants | Grew 500 lines in Round B. No section banners; order is historical. |
| `recruitment/views.py` | 4,483 lines, 83 classes/functions | `ApplicationDetailView` is 639 lines; the two batch consoles ~300 each. Imports 175 names from services in one block. |
| `recruitment/tests.py` | 23,322 lines, 53 classes | `BaseRecruitmentTestCase` is 863 lines; the largest class 2,564. Classes borrow helpers from each other (`RoundBTests._submit = BatchExamTests._submit`). |
| Already separate | `notification_services.py` 1,591, `schedule_services.py` 291, `permissions.py`, `requirements.py`, `blinding.py`, `applicant_documents.py` | The split pattern exists; F15 extends it. |

**What is already half-built, and should be finished rather than replaced:**

- **Stage changes have one writer.** `_transition_case_stage` (services.py:3094) is the only
  function that assigns `case.current_stage`. `_sync_case_after_workflow_action` (8625) is the
  de facto transition hub: 14 functions call it, and it writes the seven case fields in one
  `save(update_fields=…)`. But six functions still write the case's `case_status` or
  `current_handler_role` on their own: `request_document_resubmission`,
  `submit_document_resubmission`, `route_case_between_secretariat_and_hrm_chief`,
  `recall_vacancy_batch`, `reopen_recruitment_case` and `close_recruitment_case`.
- **Routing history has one writer**, `record_routing_history_event` (1312), with 12 callers.
  The hub does not call it; each caller decides. So some transitions leave a routing row and
  some do not (the resubmission pair and the exam/attendance closures do not).
- **Audit has one writer**, `record_audit_event` (317). Good.
- **Locking** is a decorator, `locks_workflow_row`, on 15 entry points (Round A, F4). The hub
  itself does not lock, so a new caller written without the decorator is unlocked again.
- **Time.** Only two `.date()` calls remain in services and both are correct
  (`timezone.localtime(when).date()`); `schedule_services.py` has three, also on local times.
  F3 is fixed in place; what is missing is the guard that keeps it fixed.
- **The workflow map exists three times.** Per-stage section order in
  `_workflow_detail_sequence` (services 4071); per-branch progress steps in
  `CASE_STEPS_PLANTILLA` / `CASE_STEPS_COS` plus `_case_step_for_stage` and `_ended_case_step`
  (templatetags/recruitment_ui.py 468–540); and role-per-stage tables scattered as constants
  (`INTERVIEW_RATING_ROLES_BY_STAGE`, `DELIBERATION_STAGES_BY_BRANCH`,
  `DELIBERATION_ROLES_BY_BRANCH`, `LEVEL_TWO_FOLLOW_STAGES`, `level_owner_role`,
  `_review_stage_from_application_status`). They agree today because reviews caught the
  drift (P2-7, F9); nothing makes them agree.

**The one hard fact about splitting.** I bucketed the 421 functions by stage with a script
(regex on the name, then AST walk for references). Every proposed module references most of
the others: 32 of the possible module pairs reference each other *in both directions*
(`selection ↔ car`, `exam ↔ interview`, `intake ↔ screening`, everything ↔ `routing`/`audit`).
So the package cannot be a chain of `from .exam import save_exam_record` lines: with cycles,
Python fails on the first partially-initialised module. The split has to call siblings through
the module object (`exam.save_exam_record(...)`) so the name resolves at call time. That is
the standard remedy and it also makes the dependency between stages visible in the code.

## 2. The target layout

```
recruitment/services/
    __init__.py       re-exports every name the old module had (generated, checked by a test)
    common.py         constants, decimal/score helpers, case-number and tracking-code
                      generators, logger — depends on nothing else in the package
    clock.py          local_day(), today(), event_not_held_yet() — the only place a datetime
                      becomes a date (round 4)
    workflow_map.py   the per-branch, per-level step table and the lookups that read it
                      (round 3)
    audit.py          record_audit_event, audit archive/CSV/summaries, protected-record access
    access.py         user_can_*, visibility, blind viewer, queue, closed cases, follow,
                      panel membership
    routing.py        transition_case (round 2), process_workflow_action, available actions,
                      level owner, Secretariat↔Chief routing, overrides, batch handoff and
                      recall, workflow sections
    identity.py       internal login attempts, MFA challenge, password reset counters
                      (today in services, used only by identity_views)
    entries.py        postings, status changes, weights, office policy, core panel
    intake.py         applicant identity, drafts, submission packet, OTP, documents,
                      resubmission upload, withdrawal, reapplication, candidate codes
    screening.py      screening records, document review, resubmission requests, the
                      screening batch gate
    exam.py           exam records, batch exam, validity window, absence and letter of intent
    interview.py      sessions, ratings, fallback (paper) ratings, competency sheet,
                      panel per vacancy, quorum
    car.py            comparative assessment, ETE, ranking, CAR versions and filing copy
    selection.py      deliberation, COS rounds and picks, final decision, rescind, reopen
    completion.py     completion requirements, deadlines, closure
    case_notices.py   the in-app and email notice builders that live in services today
                      (notification_services.py stays as it is)
    evidence.py       encryption, vault, exports, receipts and PDFs, verification report
```

Sizes from the bucketing, so the IT staff know what to expect: `selection` ≈ 2,000 lines,
`intake` ≈ 1,600, `evidence` ≈ 1,250, `routing` + `access` ≈ 1,100, `interview` ≈ 1,000,
`car` ≈ 970, `exam` ≈ 900, `case_notices` ≈ 900, the rest under 600. No file above 2,000.

Callers do not change: `from .services import save_exam_record` keeps working in views,
forms, signals, the management commands and the tests, because `__init__.py` re-exports
everything. The same trick later gives `recruitment/views/` and `recruitment/tests/`.

## 3. The rounds

Each round: its own branch or commit, test-first where a behaviour is involved, full suite
green (`--parallel 4`, run alone), and for rounds 2 and 3 a live smoke on the demo database
with the walk harness (`probe()` rolls back). Order matters: 1 before everything, 4 is
independent and small, 5 and 6 last.

### Round 1 — split the module (mechanical, lowest risk, biggest readability gain)

1. **Freeze the public surface.** Write `recruitment/tests` fixture
   `services_names.txt`: every top-level name of the old `services.py` (including
   underscore names, because tests import `_decrypt_evidence_bytes`, `_optional_decimal`
   and friends). A test asserts `set(dir(recruitment.services)) ⊇ names`. This test is the
   contract for the whole round and stays in the repo.
2. **Move by script, not by hand.** An AST script (the bucketing script is the seed; keep it in
   the scratchpad, commit only results) assigns each top-level definition to a module, moves
   its source text verbatim, keeps the original relative order inside each module, and
   writes each module's imports. Constants shared across modules go to `common.py`;
   constants used by one module stay with it.
3. **Cross-module calls through the module object.** The script rewrites a bare reference to
   a function defined in another module as `module.name` and adds `from . import module` at
   the top. Module-level code must not touch a sibling's attributes at import time (the test
   in step 1 plus a plain `python -c "import recruitment.services"` catch it).
4. **Generate `__init__.py`**: an explicit `from .exam import (name, name, …)` per module,
   produced by the script from the definitions it moved. No star imports, so underscore names
   are re-exported too and a reader sees where each name lives.
5. **Fix the patch targets.** `BaseRecruitmentTestCase` (tests.py:267) patches
   `recruitment.services.event_not_held_yet` for the whole suite, and
   `SecretariatEssentialsTests._not_yet` does the same. After the split those must patch the
   module that *calls* it (`recruitment.services.exam.…`, `…interview.…`), or the callers
   must call `clock.event_not_held_yet` so one patch on `clock` covers all. Prefer the
   second. The test suite fails loudly if this is missed (`enforce_event_held`), which is why
   it is listed rather than feared.
6. **Delete `services.py`.** `git log --follow` will not track the move; `git blame -C -C`
   does. Note it in the runbook.

Done when: the names test passes, the suite is green, `grep -c "^def " recruitment/services/*.py`
sums to 421, and no file in the package imports a sibling's name directly.

### Round 2 — one door for every case movement (`transition_case`)

Build it from what exists; do not design a new state machine.

```python
@locks_workflow_row("case")
def transition_case(application, actor, *, to_status, to_role, route_type, description,
                    remarks="", notes="", is_override=False):
    """The only way a case changes stage, status or handler. Locks the case row, derives
    the stage from the target status (today's _sync_case_after_workflow_action), writes the
    case and the application, records one RoutingHistory row and one AuditLog row, and
    returns the before/after pair for the caller's notice."""
```

1. **R2a, behaviour-preserving.** Rename `_sync_case_after_workflow_action` to the body of
   `transition_case`; move the application-side writes and the audit call that
   `process_workflow_action` does after it into the hub; route the 14 existing callers through
   it. Then route the six direct writers listed in section 1 through it as well, each as its
   own small commit with its tests. Where a caller today writes a routing row, the hub writes
   it; where it does not, pass `record_route=False` *for now* so counts in audit/export tests
   do not move in this step.
2. **R2b, the deliberate change.** Remove `record_route=False`: every transition leaves a
   routing row. This is a records improvement (the resubmission pair, the exam/attendance
   closures and the COS unpicked closure become traceable in the case timeline and the
   export). It changes row counts, so a handful of tests assert new numbers, with the reason
   in the test. **Recommendation: do it.** The office asked for traceability in every
   review.
3. **Lock in the hub, not only at the door.** `transition_case` carries
   `locks_workflow_row("case")` itself, so a future caller written without the decorator is
   still locked. The existing decorators on the 15 entry points stay (they also lock before
   the *reads* that precede the transition, which the hub cannot do for them).
4. The `test_workflow_transitions_lock_their_rows_first` test gains `transition_case`.
   A new test asserts, by AST over the package, that no function other than
   `transition_case` assigns `case.current_stage`, `case.case_status` or
   `case.current_handler_role`. That is the guard that keeps round 2 true after turnover.

Done when: the AST guard passes, and the live smoke (screening → exam → interview → CAR →
decision → completion on one Plantilla case; screening → exam → interview → pick → completion
on one COS case; one resubmission; one batch handoff and recall) shows the same routing
history and audit entries as before, plus the new rows from R2b.

### Round 3 — the workflow as one table

`workflow_map.py` holds, per (branch, level), the ordered steps:

```python
Step = namedtuple("Step", "key label stage section owner_role is_complete")
PLANTILLA = (
    Step("screening", "Screening", Stage.SECRETARIAT_REVIEW, "screening", Role.SECRETARIAT, screening_is_finalized_for_current_stage),
    Step("exam", "Exam", Stage.SECRETARIAT_REVIEW, "exam", Role.SECRETARIAT, exam_is_finalized_for_current_stage),
    Step("interview", "Interview", Stage.HRMPSB_REVIEW, "interview", Role.HRMPSB_MEMBER, …),
    Step("car", "Comparative Assessment", Stage.HRMPSB_REVIEW, "car", Role.SECRETARIAT, …),
    Step("decision", "Decision", Stage.APPOINTING_AUTHORITY_REVIEW, "decision", Role.APPOINTING_AUTHORITY, …),
    Step("completion", "Completion", Stage.COMPLETION, "completion", Role.SECRETARIAT, …),
)
```

with the Level 2 variants (the Chief owns the Level 1 stages) and the COS Level 1
`chief_recheck` step. Then, one consumer at a time, each with a test that the old answer
equals the new one for every (branch, level, stage, section):

1. `_workflow_detail_sequence`, `get_current_workflow_section` (services).
2. `CASE_STEPS_*`, `_case_step_keys`, `_case_step_for_stage`, `_ended_case_step`
   (templatetag) — the progress bar reads the map.
3. `level_owner_role`, `_review_stage_from_application_status`,
   `INTERVIEW_RATING_ROLES_BY_STAGE`, `DELIBERATION_STAGES_BY_BRANCH`,
   `DELIBERATION_ROLES_BY_BRANCH`, `LEVEL_TWO_FOLLOW_STAGES`.
4. The "next step" prompts (`_interview_finalize_next_step` and the exam-finalize message)
   and the handoff eligibility (`user_can_send_vacancy_batch`) read owner roles from the map.

Keep the Office Policy switches where they are; they modify behaviour inside a step, not the
order of steps.

Done when: a single test walks every (branch, level) through the map and checks the
templatetag's progress bar, the services' section sequence and the owner role all come from
it, and `docs/workflow-rules.md` gains the table rendered from the code (a management command
`print_workflow_map` so the document cannot drift either).

### Round 4 — time in one place (small, independent)

1. `clock.py`: `local_day(aware_dt)` (= `timezone.localtime(dt).date()`), `today()`
   (= `timezone.localdate()`), `event_not_held_yet`, `event_held_by`, the "not held yet"
   message.
2. Replace the five `.date()` calls (services ×2, schedule_services ×3) with `local_day`.
3. The lint test: parse every `.py` under `recruitment/` except `migrations/` and `tests/`,
   fail on any `.date()` call whose receiver is not `timezone.localtime(...)`, and on any
   `datetime.now()` / `date.today()` (naive) use. This is what keeps F3 fixed.

### Round 5 — tests split to match

`recruitment/tests/` package: `base.py` (`BaseRecruitmentTestCase` plus the helpers classes
borrow from each other, moved into mixins so `RoundBTests._submit = BatchExamTests._submit`
becomes inheritance), then one file per area: `test_identity.py`, `test_entries.py`,
`test_intake.py`, `test_screening.py`, `test_exam.py`, `test_interview.py`, `test_car.py`,
`test_selection.py`, `test_completion.py`, `test_routing.py`, `test_notices.py`,
`test_evidence.py`, `test_audit.py`, `test_portal.py`, `test_reviews.py` (the Secretariat
and Round A/B classes), `test_architecture.py` (the names test, the transition guard, the
time lint, the workflow-map agreement test). Django discovers `tests/test_*.py` on its own.
Test labels change (`recruitment.tests.test_exam.BatchExamTests`), so update
`docs/development-checklist.md` and the README's test section.

### Round 6 (optional, after 5) — views

`recruitment/views/` with `case.py` (ApplicationDetailView split into a thin dispatcher plus
one small class per tab), `batches.py` (the batch consoles), `entries.py`, `audit.py`,
`exports.py`, `policy.py`, and the same re-exporting `__init__.py` so `internal_urls.py` does
not change. Same mechanical method as round 1. Worth doing; the 639-line view is where the
next F3-type bug hides. Lower priority than rounds 1–4.

## 4. Risks and the check that catches each

| Risk | Caught by |
|---|---|
| Circular import after the split | `python -c "import recruitment.services"` and the names test, at once |
| A name dropped from the package | the names test (fixture of 421+79 names) |
| A `mock.patch` target that no longer reaches the caller | the suite (`enforce_event_held` makes it fail loudly) |
| Behaviour drift in round 2 | R2a holds routing/audit counts fixed; the live smoke compares the case timeline before and after |
| A later developer bypassing the hub or the map | the AST guard test and the map agreement test, both kept in `test_architecture.py` |
| Lost history | `git blame -C -C`; noted in the runbook |
| The demo droplet | nothing to migrate; no model changes in any round |

## 5. Decisions for the office (developers may take them)

1. **R2b**: record a routing row for every transition, including the ones that have none
   today. Recommended yes.
2. **Round 6**: split the views too. Recommended yes, last.
3. **Module names** in section 2. Any renaming is cheap before round 1 and expensive after.

Unrelated housekeeping found on the way: `.claude/worktrees/` holds 11 old worktree folders
(79 MB) of which only two (`darkmode`, `workflow-governance`) are still registered with git;
delete the other nine before turnover so searches do not return stale copies.

## 6. Brief for the implementer

Implement rounds 1, 2, 3, 4, 5 in that order (6 only if asked), against `43fe551` or later,
each round its own commit with the suite green and run alone. Round 1 by script, verified by
the names test; round 2 in two commits (R2a behaviour-preserving, R2b routing rows) with the
AST guard; round 3 one consumer per commit with an agreement test each; round 4 with the
lint test; round 5 last. Live-smoke rounds 2 and 3 on the demo database with the walk
harness. Update `docs/development-checklist.md`, `README.md` (test labels) and
`docs/operations-runbook.md` (blame note). Do not change models, templates, URLs or any
user-visible text.
