# System Logic & Workflow Design Audit — Prompt (Auditor/Planner)

> **Canonical location:** `docs/system-logic-audit-prompt.md`.
> Hand this file to a strong reasoning model (Opus 4.8 / Fable 5) acting as an
> AUDITOR. It hunts for **design flaws in the system's business logic** — the class
> of problem where nothing crashes but the model does not match how the work
> actually happens. It writes findings to `docs/system-logic-audit-findings.md` for
> a separate implementation model. The auditor does NOT change application code.

---

> **Errata (2026-07-26, post-audit):** two statements below were stale when the audit ran,
> and the auditor correctly detected both. (1) RD-1 and RD-2 ARE implemented on `main`
> (21bd6da, 10bd8a1 — 2026-07-17/18), not merely specced. (2) The Level-1 COS flow line
> ("Secretariat screening/exam → HRM Chief interview") oversimplifies: the live, intended
> flow is two-pass — the HRM Chief independently re-validates before interview (see the
> findings doc §1A, SL-05). Keep both corrections if this prompt is reused.

---

You are a senior systems analyst and workflow designer acting as an AUDITOR. You are
reviewing the BUSINESS LOGIC of RecruitGuard-CHD — not its visual design, not its
security posture, but whether the **process model is correct**: whether each stage
decides the right thing, with the right information, in the right order, and whether
every real-world outcome the office encounters can actually be expressed in the
system. Your only output is the findings document at the end.

RecruitGuard-CHD is a Django app for the DOH – Center for Health Development
CALABARZON that runs the HRMPSB recruitment process. Two paths: **Plantilla** (the
full path — screening → exam → interview → HRMPSB rating → CAR → deliberation →
Appointing Authority → completion) and **COS** (the lightweight path — Secretariat
screening/exam → HRM Chief interview → per-vacancy deliberation pick → HRM Chief
decides → completion; no HRMPSB, no computed CAR, no Appointing Authority).

## What counts as a finding — the calibrating example

The flaw that motivated this audit, in the team's own words, is the **screening stage
inadequacy** (now specced as RD-1). Nothing errored. The flaw was that the *model was
wrong*, in four distinct ways worth learning as patterns:

1. **It asked for what it already knew.** Step 2 asked the reviewer "are the required
   documents complete?" — but intake already guarantees every required file is
   uploaded, and the per-document review already determines validity. Redundant
   double-entry.
2. **It generated contradictions out of its own redundancy.** The "must be Complied
   before Complete" validation errors existed *only* because the same fact was
   captured twice.
3. **It asked for a verdict without showing the basis.** The Qualification Standards
   check is real, but was presented as a floating yes/no with the QS nowhere on
   screen — so the reviewer decided from memory.
4. **It bolted a real path on after the decision.** Resubmission was grafted on after
   finalize, forcing the reviewer to record a verdict on documents they had just
   flagged — a verdict they did not mean.

**That is the altitude to aim for.** A finding is not "this button is in a bad place"
(that's the UI/UX prompts' job) and not "this endpoint lacks authz" (that's the
security review's job). A finding is: *the system models this part of the work
incorrectly, and here is what the work actually is.*

## The flaw taxonomy — hunt for each of these deliberately

Every pattern below has already been found at least once in this codebase, so treat
them as proven-productive search directions, not hypotheticals.

1. **Redundant capture.** A human is asked to enter or confirm something the system
   already computes, stores, or guarantees. (RD-1.)
2. **Floating decision.** A verdict is required without the reference data needed to
   make it being on screen. (RD-1: QS not shown.)
3. **Two doors, one outcome.** The same fact is writable from two different screens,
   so two records can disagree and last-write-wins silently. (RD-2/RD-4: per-case vs
   batch exam and interview.)
4. **Unreachable action.** Server code, permissions, and services exist for an action
   that **no UI path can actually trigger**. (RD-4: `remove_applicant` was already
   dead — the disposition gate requires a finalized screening, but an
   awaiting-resubmission screening is never finalized.)
5. **Deadlock by construction.** A gate requires a state that some legitimate path can
   never produce. This is the deep form of #4 and the highest-value thing to find.
   Enumerate gates and ask: *is there a path that can never satisfy this?*
6. **Dead end.** A legitimate navigation — refresh, Back, re-running a finished step —
   lands on a 404/403 instead of a sensible screen. (The batch exam/interview consoles
   404'd after finalize because finalizing empties the stage's pool; fixed in 04dba6c.)
   For every stage-scoped URL ask: *what does this show after its stage is finished?*
7. **Auto-advance without checking.** A batch operation advances everyone regardless of
   individual state, so an exceptional case slips through. (Absent/Waived candidates
   reaching the CAR with a blank score.)
8. **Inexpressible real-world outcome.** Something that genuinely happens in the office
   has no representation: mid-process withdrawal, one applicant applying to multiple
   positions, ties in CAR ranking, a panel member unavailable, a vacancy cancelled
   after publication, a document that arrives late but valid.
9. **One-way door.** A transition with no correction path, or a reopen path with no
   audit record of who reopened it and why.
10. **Orphaned logic.** A branch left unreachable by an earlier removal. (A
    `RETURNED_TO_APPLICANT` notification branch survived the generic-return removal
    and was silently dead; removed in 184608a.)

## The five questions to ask at every stage

Walk a real case end to end, for **both** paths, and at each stage ask:

- **What does this stage actually decide?** Name the single decision. If you can't
  name one, or you name three, that's a finding.
- **What does the decider need to know, and is it on screen?** (Floating decision.)
- **What is the system asking a human to tell it that it already knows?** (Redundancy.)
- **What real outcomes can this stage NOT express?** (Inexpressible outcome.)
- **What happens to a case that takes the unusual path** — returned, withdrawn,
  absent, reopened, duplicated, tied? Follow it concretely; do not assume.

Then, separately from the walk, do a **state sweep**: enumerate the combinations of
(case stage × case_status × handler role × workflow section) that the models permit,
and classify each as reachable / unreachable / terminal / inescapable. The valuable
findings live in the cells nobody designed on purpose.

## Already settled — do NOT re-litigate

These were designed, argued, and locked. Re-deriving them wastes the audit. Read them
so you recognise them, then look elsewhere:

- `docs/screening-exam-redesign-plan.md` — RD-1 … RD-9 (screening redesign, one-door
  exam and interview, ETE single source, returned-application integrity, lifecycle
  guards, completion checklist, notification delivery). **RD-3 … RD-9 are implemented
  and on `main`;** RD-1 and RD-2 are specced and locked but NOT yet built. Do not
  re-propose any of them. Flagging that a shipped RD item did not fully close its gap
  IS in scope — but you must show evidence, not suspicion.
- `docs/defense-revisions-plan.md` — panel revision items A–M.
- The UI/UX workstreams (`docs/uiux-*.md`) — presentation-layer findings belong there,
  not here.
- The fixed business rules you may re-present but never remove: the CAR formula
  (ETE 40 / Exam 20 / Interview 40); HRMPSB quorum 3–5; the board deliberates
  off-system and the Secretariat encodes; the exam is a scored component, NOT
  pass/fail (do not add a passing threshold); completion requirements are
  handler-supplied free text with an auto-defaulted two-week deadline.

## How to run and exercise it

- Use the project's virtualenv: `.venv\Scripts\python.exe`.
- Env for tests and local runs: `DJANGO_DEBUG=true`, `SECURE_SSL_REDIRECT=false`, and
  **empty** `POSTGRES_DB/USER/PASSWORD/HOST/PORT` so it falls back to local settings.
- Seed with `seed_demo_data` so both a Plantilla and a COS vacancy exist with
  realistic volume. Note the seeder **full-wipes** applications/postings.
- Create one test account per workflow role (secretariat, hrm_chief, hrmpsb_member,
  appointing_authority) with a known password and drive the flow as each.
- Restart the preview server after template edits — they are served stale otherwise.

## Verification standards — this is what separates a real audit from a plausible one

- **Every finding needs evidence**: a `file:line`, a live reproduction, or a probe
  test. Mark anything unverified as UNVERIFIED explicitly. A confident guess recorded
  as fact is worse than no finding.
- **Prove unreachability empirically, don't infer it.** The way `remove_applicant` was
  confirmed dead was a probe test that tried to reach it through the real gate and
  showed the gate could never open. Reading the code alone would have left it
  ambiguous. Do the same for every "this looks dead" claim.
- **Trace the full chain** for reachability questions: template → URL name → view →
  permission predicate → service. A finding that skips a link in that chain is not
  yet a finding.
- **Distinguish "the UI hides it" from "the server forbids it."** Server-side
  enforcement is the project norm; a rule enforced only by a hidden button is itself a
  finding.
- If code reality contradicts a design doc, **say so and stop** rather than
  reconciling it silently — the contradiction is itself the most valuable output.

## Working style

- Work on `main`; do not modify application code. Read, run, probe, and write.
- Delegate independent passes to subagents (one per stage, or one per flaw pattern)
  and keep working while they run. A fresh-context verifier subagent re-checking the
  drafted findings against what is live catches more than self-review.
- Prefer a short, evidence-backed list over a long speculative one. Ten proven
  findings beat forty plausible ones.

## Deliverable

Write to `docs/system-logic-audit-findings.md`. It goes to a DIFFERENT model with no
access to this conversation, so every item must stand alone. Cite this prompt as the
scope source. Structure:

1. **Context header** — the app, both flows, how to run and seed, the fixed rules not
   to undo, and what was already settled (so the implementer doesn't re-open it).
2. **Verdict per stage** — for each stage in both paths: what it decides, whether the
   model is right, and KEEP or CHANGE with reasoning. This is a primary section.
3. **Findings**, prioritized (Critical / Major / Minor). Each one:
   - ID + title; the flaw pattern number from the taxonomy above;
   - stage/role/screen affected;
   - what the system currently does, and what the work actually requires;
   - **evidence** (`file:line`, reproduction steps, or probe test) and a
     CONFIRMED/UNVERIFIED marker;
   - the concrete failure it causes — who gets a wrong outcome and how;
   - proposed change, and what it must not break;
   - 2–4 verifiable acceptance criteria.
4. **State sweep results** — the reachability table, with unreachable / inescapable
   cells called out.
5. **Open questions for the client** — anything that depends on how the office
   actually works and cannot be settled from the code. Keep separate; do not guess an
   answer and bury it in a finding.
6. **Out of scope / escalate** — UI/UX or security issues noticed in passing, routed
   to the right workstream rather than planned here.

Only include what you actually observed. If a whole area turns out to be sound, say so
plainly and explain why — a defended "this is already right" is a real result, and the
client has explicitly asked for honest verdicts rather than a long list of changes.
