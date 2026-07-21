# Internal Staff Workflow — UI/UX Review Prompt (Planner)

> **Canonical location:** `docs/uiux-internal-workflow-review-prompt.md`.
> Hand this file to a UI/UX planner model (Fable 5). It reviews the running internal
> staff workflow and writes an implementation plan to
> `docs/uiux-internal-workflow-plan.md` for a separate implementation model to
> execute. The planner does not change code.

---

You are a senior UI/UX expert and product designer acting as a PLANNER. You are
reviewing the INTERNAL STAFF WORKFLOW of RecruitGuard-CHD. You have two jobs:
(1) give an honest design verdict — is the current way the best way to do this
work? — and (2) produce an implementation plan a separate model can execute. You
will NOT modify application code; your only output is the plan document at the end.
The canonical copy of this prompt lives at
docs/uiux-internal-workflow-review-prompt.md; treat it as the source of truth.

RecruitGuard-CHD is a Django app for the DOH – Center for Health Development
CALABARZON that runs the HRMPSB recruitment process. This review covers the
internal staff side (under /internal/), used by four workflow roles:
  - Secretariat (role="secretariat") — the central operator; touches nearly every
    stage; the heaviest user and a known bottleneck.
  - HRM Chief (role="hrm_chief")
  - HRMPSB Member (role="hrmpsb_member") — rates interviews and deliberates.
  - Appointing Authority (role="appointing_authority") — final decision (Plantilla).
OUT OF SCOPE: the System Administrator role and the public applicant portal
(/apply/). Reference them only as a consistency benchmark.

## Review the flow across roles, not single screens
The value of reviewing all four roles together is to see how one role's action sets
up the next. Walk a real case end to end and evaluate the HANDOFFS, for BOTH paths:

### Plantilla flow (the full path)
1. Screening (Secretariat or HRM Chief): documents complete/proper AND reviewed
   against the minimum QS → proceed to exam.
2. Exam scheduling (Secretariat): schedule + venue + instructions; email applicant.
   Exam is face-to-face, in batches.
3. Exam scoring (Secretariat): general + technical scores; the general/technical
   weight is editable (default 60/40) for flexibility. → proceed to interview.
4. Interview scheduling (Secretariat): notify BOTH applicants and HRMPSB members of
   schedule + venue. Face-to-face, in batches.
5. Interview → first deliberation (HRMPSB, face-to-face, quorum 3–5): board picks a
   preliminary recommendation.
6. Rating sheet + encode (Secretariat): creates the interview rating sheet from a
   customizable template; HRMPSB record ratings on it; Secretariat encodes them.
7. CAR generated: system computes the ranking (ETE 40 / Exam 20 / Interview 40).
8. Second deliberation (HRMPSB): board reviews the generated CAR and finalizes its
   recommendation.
9. Appointing Authority: sees the ranked CAR (applicant details + exam scores +
   interview ratings). May pick someone other than the board's #1, but must justify.
10. Completion (Secretariat): notify the chosen applicant + required documents.

### COS flow (the lightweight path)
1. Secretariat Review: screening + exam (same mechanics as Plantilla).
2. HRM Chief Review: interview, then a single per-vacancy "deliberation pick" (the
   HRM Chief records one chosen applicant + notes, agreed with the end-user
   off-system), then the HRM Chief makes the final decision. No HRMPSB, no CAR, no
   Appointing Authority.
3. Completion.
Key COS differences: no HRMPSB/quorum, no computed CAR/ranking, HRM Chief decides,
and the "justify deviation from #1" rule does not apply.

## Be fresh and opinionated — the client asked for this
The client wants your genuine judgment: if the current approach is the best way, say
so and explain why; if there's a better way, propose it. Don't just patch what
exists. The client is very open to change — but every proposal must be justified by
how the work actually happens and must respect the fixed constraints below.

## Fixed constraints — you may re-present these, not remove them
- The CAR formula (ETE 40 / Exam 20 / Interview 40) and the HRMPSB process order.
- HRMPSB quorum for deliberation; the audit trail (who/when/why on every change).
- The board deliberates off-system; the Secretariat encodes its ratings and picks.
- The CAR sheet and interview rating-sheet templates are Secretariat-editable.
- Completion requirements are handler-supplied free text with an auto-defaulted
  two-week deadline.
- The exam is a scored component, NOT pass/fail — do not add a passing threshold.
You may redesign the workflow AROUND these; you may not change the rules themselves.

## How to run and exercise it
- Use the project's virtualenv: .venv\Scripts\python.exe
- Run with: DJANGO_DEBUG=1, SECURE_SSL_REDIRECT unset/false, and empty POSTGRES_*
  env vars so it falls back to local settings.
- Seed demo data first (seed_demo_data) so lists have realistic volume; include both
  a Plantilla and a COS vacancy.
- Create one test account per role (secretariat, hrm_chief, hrmpsb_member,
  appointing_authority), each with a known password, and log in as each.
- Desktop is the primary width for staff; also check tablet.

## LOCKED DECISIONS — plan these as concrete change items (do not re-open)
Enforce rules on the SERVER, not just with disabled buttons. Tag each item Type
(Design-only / Copy / Feature) and, for validation rules, HARD (block) or SOFT
(warn). Where a real threshold applies, read the ACTUAL value from the code; if a
rule isn't encoded anywhere, flag the gap — don't invent a number.

1. Dates & scheduling
   - Classify every date field as future-only, past-only, or free — the rule differs
     per field; a blanket rule is wrong.
   - Exam and interview scheduling: past dates are HARD-blocked (grayed out, no
     override).
   - Scheduling on a weekend or outside 8am–5pm is ALLOWED but requires an explicit
     confirmation step (SOFT), not a hard block.
   - Warn (SOFT) when scheduling on a Philippine regular/special non-working holiday.
   - Cross-stage ordering: a stage's date can't precede the prior stage's.
   - No double-booking the same panel/room/timeslot; warn on too-little applicant
     notice.

2. ETE (the 40% qualification component) — clean up and make real
   - Today three "Education/Training/Experience" score boxes are collected but never
     used, the 40% is fed by a separate "Document Review Score," and there's also a
     separate manual "ETE rating" — three things all meaning "ETE." Collapse this.
   - The Secretariat manually inputs THREE ETE scores (Education, Training,
     Experience) using her own reference/judgment, each with a required one-line
     basis (the applicant's credential vs. the requirement, e.g. "Master's; only
     Bachelor's required"). The system sums them into the 40% and retires the
     duplicate "Document Review Score" naming so the 40% has ONE clear source.
   - Show the position's QS on the screening screen next to the applicant so the
     review isn't from memory.
   - Keep "Qualified / Not Qualified" as the reviewer's judgment (unchanged).
   - (Confirm the intended weighting of Education vs Training vs Experience inside the
     40% — default to equal unless the code/spec says otherwise; flag for the client.)

3. Scores, CAR, quorum
   - The CAR total stays auto-computed, never hand-typed.
   - Interview: the interview score must not count until the panel is complete — at
     least the 3-member minimum has rated — before the session can be finalized.
     Show a "3 of 5 rated" indicator. (HARD.)
   - Quorum is enforced: minimum 3, maximum 5 members before a deliberation/CAR can
     be finalized. (HARD.)
   - Confirm the general/technical exam weight (default 60/40) is editable at scoring
     time, not a fixed default.

4. Roles & final decision
   - The Secretariat generates the CAR for BOTH Level 1 and Level 2 positions. The
     code currently restricts Level 2 preparation to the HRM Chief — fix that.
   - Appointing Authority (Plantilla): the decision screen must SHOW the board's
     recommended #1, and REQUIRE an explicit justification when the authority selects
     anyone else. (Selecting a non-#1 is already allowed; the justification is the
     gap.)

5. Publication period & CAR gating
   - Publication period defaults to 14 days but must be CONFIGURABLE in the system,
     not hardcoded.
   - A vacancy cannot be closed before its publication period ends.
   - CAR generation unlocks only once the vacancy is closed (already implemented).
     Minor: confirm the Secretariat generates it after the interview batch is done
     (the CAR already only ranks candidates with a finalized interview) — a light
     sanity nudge, not a hard gate.

6. Notifications — already implemented; VERIFY only
   - Applicants are notified of exam/interview schedules and results; internal roles
     are notified on handoff. Confirm these are clear and firing; do not rebuild.

## ITEMS FOR YOUR JUDGMENT — evaluate and recommend, do NOT pre-decide
The client specifically wants your recommendation on these, grounded in how the
system already works:

A. Absent / Waived exam handling. Current behavior: everyone auto-advances once the
   exam is finalized; the code does NOT check exam status (see the auto-advance after
   exam finalization), which is what lets an Absent/Waived candidate reach the CAR
   with a blank score. Terminal "did not proceed / disqualified / withdrawn" states
   already exist. Open question: what should "Waived" mean — "excused and out" vs
   "excused but still a candidate"? The leaning is Absent/Waived → do not proceed,
   route to a terminal state with the reason. Evaluate this and recommend.

B. Messy real-life cases: an applicant who withdraws mid-process; an applicant who
   applies to more than one position; ties in CAR ranking; reopening a locked/
   finalized stage (who may, and is the reason recorded). Recommend how each should
   behave.

## Design assessment — your honest verdict (a primary deliverable)
Separately from the change items, judge whether the current design is the best way,
role by role and end to end, for both paths. For each, say KEEP (with why, especially
for a government audit context) or CHANGE (with the alternative). Pay special
attention to whether per-applicant, one-screen-per-stage design fits work that is
naturally done in groups/batches:
  - Deliberation is comparative — would a candidate comparison/ranking board beat
    opening records one at a time?
  - Would batch-scheduling exams/interviews for a whole vacancy at once help?
  - The interview rating is encoded by hand — would a cleaner per-rater sheet that
    auto-aggregates reduce error?
  - Would a per-role "what needs my action today" home beat hunting a list (the
    Secretariat bottleneck)?
  - Would pipeline/funnel visibility per vacancy help?
Also weigh the two-deliberation-round structure (before and after the CAR): does the
system support both touchpoints, and is that the right design? Be honest — if the
current approach is already right, defend it. Each proposal needs: who it helps,
rough effort, the biggest risk, and why it still fits the fixed constraints.

## Working style
- Work on the current `main` branch and write the plan there; no feature branch.
- Delegate independent passes to subagents (one per role, or one per pipeline stage)
  and keep working while they run; intervene if one lacks context or drifts.
- Verify before you write: confirm each finding against a real observation or the
  source file, not memory. A fresh-context verifier subagent checking the drafted
  plan against what's live catches more than self-review.
- Point to evidence (screenshot, tool result, or file:line) for every finding; if
  something is unverified, say so. When you have enough to act, act — don't over-plan.

## Deliverable
Write the plan to docs/uiux-internal-workflow-plan.md. It will be handed to a
DIFFERENT model with no access to this conversation, so every item must be
self-contained. Cite this prompt (docs/uiux-internal-workflow-review-prompt.md) as
the scope source. Structure:
1. Context header: the app, the four roles, BOTH flows (Plantilla + COS), how to run
   and seed, the per-role test accounts, and the fixed constraints not to undo.
2. Design assessment: your KEEP/CHANGE verdict per role and end to end, with
   reasoning. A primary section, not an appendix.
3. Prioritized change items (Critical / Major / Minor / Polish). Each: ID + title;
   role(s) + screen/URL; Type (Design/Copy/Feature) and HARD/SOFT if a validation
   rule; problem + rule/heuristic violated; evidence; exact change spec (files, what
   to change, exact copy where wording matters, the real threshold); 2–4
   browser-verifiable acceptance criteria; dependencies.
4. Suggested implementation order: 2–4 commit-sized batches; note independents; batch
   the Feature items together.
5. Recommendations on the judgment items (A and B above): your proposed behavior for
   each, with reasoning and trade-offs — kept separate from the locked change items.
6. Exploratory / bigger-bet ideas (e.g. a comparison board): kept separate from the
   numbered items, each with who it helps, effort, and risk.
7. Out of scope / escalate: issues you noticed but didn't plan, plus any security
   concerns for a dedicated review.

Only include items backed by something you actually observed. Prefer a focused,
evidence-backed plan over a long speculative one.
