# RecruitGuard-CHD System Logic Audit Findings

**Audit date:** 2026-07-21  
**Audit scope:** Business-process and workflow-state logic only  
**Audited branch:** `main` at `91d99b7`  
**Excluded:** visual design, presentation quality, penetration/security testing, and implementation changes

## 1. Context and audit basis

RecruitGuard-CHD is the DOH - Center for Health Development CALABARZON recruitment workflow system. It has one platform, one database, and one workflow engine with two deliberately different paths:

- **Plantilla:** screening -> examination -> interview -> computed Comparative Assessment Report (CAR) and encoded HRMPSB recommendation -> Appointing Authority selection -> completion.
- **Contract of Service (COS):** Secretariat screening/examination for Level 1, or HRM Chief screening/examination for Level 2 -> HRM Chief interview -> one per-vacancy deliberation pick -> HRM Chief decision -> completion. COS has no HRMPSB evaluation, computed CAR, or Appointing Authority selection.

### Fixed rules this audit does not reopen

- The CAR formula is **ETE 40% / examination 20% / interview 40%**.
- A Plantilla interview requires **3-5 HRMPSB members**. Quorum is not a substitute for three distinct, attributable ratings.
- HRMPSB deliberation happens off-system. The Secretariat records the board's recommendation and supporting notes in the system on the board's behalf (`docs/batch-workflow-redesign-spec.md:50-52,118-125`).
- An examination is a scored component, not pass/fail. This audit does not propose a passing threshold.
- Completion requirements are handler-supplied free text, with a deadline defaulted to two weeks.
- Level 1 belongs to Secretariat; Level 2 belongs to HRM Chief. A Level 2 Secretariat override must be explicit and audit-logged.

### Settled work this audit does not re-propose

- `docs/screening-exam-redesign-plan.md`, RD-1 through RD-9.
- `docs/defense-revisions-plan.md`, items A through M.
- The `docs/uiux-*.md` workstreams.

The audit brief said RD-1 and RD-2 were specified but not built. That statement conflicts with the audited branch. Current `main` contains commits `21bd6da` (RD-1) and `10bd8a1` (RD-2), and the live view implements derived screening completeness, on-screen Qualification Standards, and the pooled-exam one-door guard (`recruitment/views.py:702-749`). The audited behavior was therefore treated as shipped. This report does not re-propose RD-1 or RD-2; it reports only gaps that remain around or outside them.

There is a second, substantive source conflict. The audit instruction and `docs/uiux-internal-workflow-plan.md:10-15` lock the CAR at 40/20/40, while `docs/batch-workflow-redesign-spec.md:165-176` describes those values only as editable per-vacancy defaults. Live code follows the latter. This report does not reconcile that contradiction silently: the fixed rule supplied for this audit is treated as controlling, and SL-00 records the code/spec compliance defect.

### How the audit was run

PowerShell environment and commands:

```powershell
$env:DJANGO_DEBUG='true'
$env:SECURE_SSL_REDIRECT='false'
$env:POSTGRES_DB=''
$env:POSTGRES_USER=''
$env:POSTGRES_PASSWORD=''
$env:POSTGRES_HOST=''
$env:POSTGRES_PORT=''
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe manage.py seed_demo_data --force
```

`manage.py check` returned no issues. `seed_demo_data --force` produced both Plantilla and COS cases at screening, examination, and decision stages. A focused late-stage regression pass covering completion, deliberation/decision support, and final decision ran 47 existing tests successfully. High-risk reachability claims were then tested with rollback-safe probes against temporary migrated Django test databases. Each probe invoked the real service permission predicates or HTTP endpoint, and the temporary database was destroyed afterward. No application code was changed.

Evidence markers mean:

- **CONFIRMED:** file/line evidence plus a live reproduction or probe where reachability was in question.
- **UNVERIFIED:** the code limitation is observed, but the office policy needed to call it wrong or choose its transition has not been confirmed. Where mechanics are confirmed but policy is not, the finding explicitly splits those two statuses and keeps the implementation conditional.

## 1A. Resolution Log (2026-07-26) — binding decisions for implementation

Read this section FIRST. After the audit was delivered, the findings were verified and
resolved with the team lead. Eight findings (SL-00, 01, 02, 03, 04, 07, 08, 13) were
independently re-verified against the code at the cited lines — all accurate. The
remaining questions were then settled using five client interview transcripts and two
office reference artifacts the client supplied:

- **DOH-RO4A-MPERIF Rev0 interview form** (competency-based: CORE/ORGANIZATIONAL/
  TECHNICAL, per-competency weights, per-rater sheet, "Expected competency Proficiency
  varies depending on the Position").
- **The office's CAR Excel template** ("COMPARATIVE ASSESSMENT / PROMOTION AND SELECTION
  BOARD"): QS block on its face; `EPS = Rating×40` (ETE), exam `=(Gen×0.6)+(Tech×0.4)`
  then `×0.2`, `AVERAGE(rater1..5)×0.4` (interview); candidates by name; Remarks column;
  signature blocks (Chair, Vice-Chair, AO V/HRMO III, RHEA 2nd Level Rep, Ad Hoc member);
  Date of Interview / Date of Deliberation. Notably `AVERAGE()` of an empty rater range
  yields `#DIV/0!` and poisons the OVERALL — the office's own spreadsheet fails LOUDLY on
  a missing component, which the current system does not (SL-03). The fix restores paper
  behavior, it does not invent a rule.

Status meanings: **CONFIRMED-IMPLEMENT** = build as the finding proposes.
**TEAM-DECIDED** = open choice resolved by the team lead; implement it.
**CLIENT-CONFIRMED** = resolved by client transcript/artifact evidence.
**ASSUMED-DEFAULT** = implement the stated default, flag for later client confirmation.
**STILL-CLIENT** = do not build the policy-dependent part yet.

| Finding | Decision | Status |
|---|---|---|
| SL-00 | KEEP per-vacancy editable weights. 40/20/40 was reverse-engineered from the office CAR template, never a confirmed CSC mandate — the "fixed rule" premise in the audit brief was a stale team assumption. Keep the existing editable-at-setup / locked-at-first-score behavior; ADD: print the split on the CAR, and an audited unlock-with-reason for corrections. Resolve the doc contradiction (uiux plan, batch spec) in favor of "default 40/20/40, per-vacancy editable pre-scoring". | TEAM-DECIDED |
| Exam G/T split | 60/40 confirmed by the template formula `=(E×0.6)+(F×0.4)` — the demo-meeting "just average them" remark is overruled by the client's own artifact. No code change. | CLIENT-CONFIRMED |
| SL-01 | Option A: **finalizing the deliberation pick IS the COS selection.** One authoritative act — finalize performs the selection transition (winner → completion, pool resolution). The per-case Decision screen becomes a display/confirmation of the pick, never an independent second choice. | TEAM-DECIDED |
| SL-02 | **Need-based selection rounds** for continuous COS: the pool accumulates (per-case screening/exam as candidates arrive); when the office hires, a round is opened, snapshots the then-eligible candidates, and can fill N slots; resolution touches only the snapshot; unpicked candidates remain in the pool for future rounds (≈1-year retention per client). Opening/pooling COS keeps single-batch behavior. Client's own words support this ("tine-take namin for pooling para matawagan sila kapag may nag-open na slot"). | TEAM-DECIDED |
| SL-03 | Implement as specced (block finalize on missing components; 3–5 distinct attributable raters; structured fallback with rater identity + numeric score + scan). Error copy may echo the template's behavior ("the office CAR would show #DIV/0! here"). | CONFIRMED-IMPLEMENT |
| SL-04 | Implement as specced: continuous COS gates the exam on the case's OWN finalized qualified screening; batch vacancies keep the vacancy-wide gate. | CONFIRMED-IMPLEMENT |
| SL-05 | **KEEP the two-pass Level-1 COS flow.** It is intentional: transcript — "kadalasan ako po ang initial review, at siya po ang final approval lalo na sa higher-level positions" — plus uiux-internal-members-plan's "independent validation". The audit brief's one-pass flow line was an oversimplification (errata added to the prompt doc). Docs-only fix; optionally label the Chief's pass "validation" in UI copy. | TEAM-DECIDED |
| SL-06 | No-show flow (exam AND interview — client confirmed the letter-of-intent policy covers both): mark Absent (no fabricated score, never numeric zero) → applicant may submit a **letter of intent** → office decides **reschedule or terminal**, letter stored as evidence, actor/reason/timestamp audited. | CLIENT-CONFIRMED (policy) + TEAM-DECIDED (mechanics) |
| SL-07 | Fix the fallback persistence predicate; implement together with SL-03's structured fallback so the reopened door cannot produce identity-less ratings. | CONFIRMED-IMPLEMENT |
| SL-08 | **Competition ranking** (1, 2, 2, 4): equal `assessment_score` ⇒ equal rank; a tie crossing 5th place keeps the whole tied band inside the top five; case reference is display-order only. | TEAM-DECIDED |
| SL-08A | Show the QS block and the candidate's education/training/experience facts (or evidence links) beside the ETE scoring rows — this mirrors the office CAR template, which carries the QS block on its face. **ETE rubric: none exists** — the template takes a single judgment rating, so manual sub-scores + required written basis stands as the design; the CAR output collapses them to the template's single Rating + EPS pair. | CONFIRMED-IMPLEMENT / CLIENT-CONFIRMED (no rubric) |
| SL-09 | **Wait for the pool to be final**: server-side gate (reuse `applicant_pool_is_finalized`) blocks batch exam AND interview scheduling/scoring/finalize until the closing date passes or the entry closes. Per-case screening continues during the window. The console states why and when it unlocks ("Publication open until <date> — exam scheduling unlocks when the applicant pool is final"). | TEAM-DECIDED |
| SL-10 | Withdrawal: applicant emails the office → Secretariat records a **WITHDRAWN** terminal outcome (distinct from Rejected in state, reports, and applicant email), attaches the email as evidence, allowed any time before appointment. The selected person withdrawing during completion escalates to the SL-14 rescind path. | TEAM-DECIDED |
| SL-11 | Build the cancellation mechanism as specced (distinct audited transition, required reason, neutral non-merit disposition + notice for in-flight cases). Default authorizer: the roles that may close a posting, with required reason. Justified by the real CSC-disapproval scenario ("kailangan pong ulitin yung buong proseso"). | TEAM-DECIDED (mechanics) / ASSUMED-DEFAULT (authorizer) |
| SL-12 | Implement as specced: stage-specific reopen creates one editable correction version; prior finalized values immutable and linked to reopen actor/reason. | CONFIRMED-IMPLEMENT |
| SL-13 | Implement as specced, plus policy: an unchanged historical deadline is always savable; extensions are a deliberate audited action (old/new/actor/reason); overdue is derived and surfaced loudly; **the system never auto-forfeits a candidate by timer** — terminal outcomes stay human decisions. | TEAM-DECIDED |
| SL-14 | Build the rescind/supersede mechanism (immutable original + linked superseding record, mandatory reason). Default authorizer: the role that recorded the decision (Appointing Authority for Plantilla, HRM Chief for COS). Cutoff and whether the matter returns to the board remain client policy. | ASSUMED-DEFAULT (authorizer) / STILL-CLIENT (cutoff, board-return) |
| SL-15 | Quarantine/remove the legacy Plantilla deliberation mutation path; historical records read-only. | CONFIRMED-IMPLEMENT |

### New items from the reference artifacts (not in the original findings)

- **N1 — CAR official-filing export.** The signed hard copy is still required (wet-ink
  signature blocks; hard-copy filing). Generate it from the system: the office's exact
  one-page layout — QS block, names "(Surname, First Name MI)", per-component columns,
  Remarks, signature blocks, Date of Interview/Deliberation. Secretariat-only,
  audit-logged as an export. On-screen CAR stays blinded (REV-B); the export is the named
  filing copy. Kills the Secretariat's current re-typing of this exact sheet.
- **N2 — Pin the competency-template version per interview session.**
  **RESOLVED-NARROWED 2026-07-26** (decision log: `docs/n2-competency-template-versioning-brief.md`
  §7): the template is per-vacancy (OneToOne) and already locks permanently on the first
  digital rating with no unlock, so version pinning is redundant on the digital path. The
  real defect is that the paper path (`upload_interview_fallback_rating`, SL-03/SL-07)
  never locks the sheet, leaving a pure-paper vacancy's sheet of record editable forever
  after the panel signed the printout. Fix: lock the template on the first encoded
  fallback sheet, mirroring `save_interview_rating` — first recorded rating of either
  kind locks the sheet. The publish-to-first-score edit window stays open by design
  (typo-correction path; "published" is visible-not-yet-authoritative). Full versioning
  returns only if the office adopts reusable cross-vacancy templates.
- **N3 — Board recommendation on the CAR: CONFIRMED CORRECT, no change.** Ms. Marz's
  demo remark "we don't recommend" targeted the old per-applicant AA cards (per-card
  Recommendation/Ranking-note fields), not the vacancy-level CAR. The board deliberates
  face-to-face and the CAR carries one recommended candidate (Kristine, 1st interview).
- **Quick win — e2e seeder fixture:** `seed_e2e_test_cases` seeds one HRMPSB rating but
  the quorum rule requires 3 — the root cause of the known `E2ESeedCommandTests` baseline
  failure. Fix the helper to seed 3 ratings.

### Remaining client courtesies (non-blocking — nothing waits on these)

1. Confirm 40/20/40 is office convention, not CSC-mandated (design is safe either way).
2. Confirm/narrow the SL-11 and SL-14 authorizer defaults.
3. Casual confirm of SL-09's wait-for-close (team-decided from the batch-per-position
   statement; costs nothing to mention).

## 2. Verdict by stage

### 2.1 Plantilla path

| Stage | Single decision made at the stage | Verdict | Reasoning |
|---|---|---|---|
| Vacancy entry and publication | Whether and when this vacancy accepts applications | **CHANGE** | Draft/Active/Suspended/Closed correctly express ordinary intake lifecycle, but none expresses permanent cancellation after publication. Suspension stops intake without stopping case processing, while Closed means the normal pool is final. See SL-11. |
| Applicant intake and OTP submission | Whether a complete application from a verified email becomes a submitted case | **KEEP** | Final submission rechecks intake openness, checklist, required files, duplicates, and OTP (`recruitment/portal_views.py:174-190,261-299,334-421`; `recruitment/services.py:7517-7550`). Uniqueness is applicant plus position, so one person may legitimately apply to several vacancies (`recruitment/models.py:868-875`). |
| Level-aware routing | Which office owns initial processing | **KEEP** | Level 1 maps to Secretariat and Level 2 to HRM Chief (`recruitment/services.py:7510-7513`). Controlled Level 2 handoff preserves the stage and creates/consumes an audited override (`recruitment/services.py:1542-1690`). |
| Screening | Whether the evidence complies and the applicant meets the displayed QS, or must be returned/not qualified | **KEEP** | Shipped RD-1 has one coherent decision: document statuses derive completeness, the QS is shown with the qualification verdict, and return is an alternative decision rather than an after-finalize add-on (`recruitment/forms.py:1505-1546,1744-1775`; `templates/internal_includes/screening_body.html:154-234`). ETE inputs are no longer captured here. |
| Examination | What scored examination outcome belongs to every qualified member of the final vacancy pool | **CHANGE** | One-door enforcement and Absent/Waived terminal handling are sound. However, a fixed-period Plantilla batch can be scheduled before the publication pool is final, allowing timely later applicants to form a second cohort. See SL-09. |
| Interview | What 3-5 distinct HRMPSB members scored for the candidate | **CHANGE** | Digital ratings and pooled one-door control are coherent. The fallback paper-rating path is unreachable for its intended support user, and its downstream representation can satisfy quorum without rater identity or a numeric score. See SL-03 and SL-07. |
| CAR calculation and preliminary ranking | What each candidate's 40/20/40 score and computed rank are | **CHANGE** | Live code permits the formula itself to be changed per vacancy, equal totals are converted into unequal ranks, and a blank component can still be finalized/ranked. See SL-00, SL-03, and SL-08. |
| HRMPSB recommendation | Whom the board recommends after off-system deliberation | **KEEP** | The system correctly captures only the outcome: the Secretariat records the recommended applicant, notes, and quorum on the CAR. It does not create a new HRMPSB system actor or pretend the discussion happened online (`docs/batch-workflow-redesign-spec.md:118-125`). |
| Appointing Authority selection/return | Whom the Appointing Authority selects from the finalized CAR, or whether the CAR is returned for reassessment | **CHANGE** | The top-five/deep-selection gate and return-for-reassessment versioning are coherent, but a recorded selection has no rescind/replace path. An ordinary data-entry error therefore cannot be corrected through the workflow. See SL-14. |
| Completion | Whether the selected appointee supplied all handler-defined completion requirements and contract/appointment references | **CHANGE** | RD-8 correctly derives readiness from structured requirement rows. Once the deadline becomes historical, however, even marking a late-but-valid requirement complete requires staff to rewrite the original deadline. See SL-13. |
| Case closure and correction | Whether the completed case can close, or a finalized case can be reopened for correction | **CHANGE** | Normal closure is gated correctly, and reopen records actor/reason. Generic reopen changes only case routing/lock fields; it does not reopen the finalized stage record, creating an unlocked case with no editable work. See SL-12. |
| Notification delivery | Whether material workflow events have actually been sent and can be recovered | **KEEP** | Shipped RD-9 exposes delivery state/recovery and applicant schedule visibility is tied to actual notice. No new process-model failure was observed. |

### 2.2 COS path

| Stage | Single decision made at the stage | Verdict | Reasoning |
|---|---|---|---|
| Vacancy entry and intake mode | Whether applications are accepted as an opening-based, pooling, or continuous intake | **CHANGE** | The intake modes exist, but a continuous vacancy has no decision-cycle/cohort boundary and cancellation is absent. A permanent finalized pick can govern later candidates. See SL-02 and SL-11. |
| Applicant intake and OTP submission | Whether a complete verified application becomes a case | **KEEP** | The same intake guarantees as Plantilla apply, while the applicant-plus-position constraint permits the same person to apply to other positions. |
| Level-aware routing | Which office owns initial processing | **KEEP** | Level 1 starts at Secretariat and Level 2 at HRM Chief. The defect is not ownership; it is that Level 1 repeats completed work after handoff. |
| Screening | Whether documents comply and the applicant is qualified | **CHANGE** | RD-1 is coherent within a case. Continuous COS nevertheless applies a vacancy-wide screening gate, so a ready candidate can be held indefinitely by an unrelated new or resubmitting candidate. See SL-04. |
| Examination | What the candidate scored, without a pass/fail threshold | **CHANGE / GOVERNANCE CONFLICT** | Pooled and per-case write doors are correctly separated. Continuous candidates are batch-blocked (SL-04). The audit's stated Level 1 path says HRM Chief starts at Interview, but prior locked UI/UX documents intentionally preserve Chief re-validation of screening/exam; SL-05 records that unresolved source conflict rather than silently choosing one. |
| Interview | What the end-user/HRM interview score is | **CHANGE** | The single-score model is appropriate for COS, but no-show/absence has no outcome. The stage can be completed only by inventing a score or a fallback sheet. See SL-06. |
| Per-vacancy deliberation | Which eligible candidate the HRM Chief selects from this vacancy/cycle | **CHANGE** | The model stores one vacancy pick, but that pick neither freezes its candidate pool nor binds the later decision. See SL-01 and SL-02. |
| HRM Chief final decision | Whether the deliberation choice is selected and sent to completion | **CHANGE** | The per-case decision independently permits either outcome on every candidate. It can select a person other than the finalized pick and then reject the actual pick. See SL-01. |
| Completion and closure | Whether the selected COS appointee supplied the handler-defined contract requirements | **CHANGE** | Core readiness and closure gates are sound; the historical-deadline edit defect is shared with Plantilla. See SL-13. |
| Withdrawal/cancellation/correction | Whether an active candidate or vacancy leaves the process for a neutral, non-merit reason, and whether an erroneous final record can be corrected | **CHANGE** | Withdrawal is an orphaned application enum, cancellation has no state, and generic reopen does not reopen the underlying record. See SL-10 through SL-12. |

## 3. Prioritized findings

### Critical

#### SL-00 - The fixed 40/20/40 CAR formula is editable per vacancy in live code

- **Flaw pattern:** #3 Two doors, one outcome (the governing formula exists as both a fixed rule and editable vacancy data); #9 One-way door once nonstandard scoring begins and weights lock.
- **Affected:** Plantilla entry setup, CAR calculation, Secretariat/HRM Chief entry managers, and every downstream CAR/selection.
- **Status:** **CONFIRMED** by fixed-rule/document contradiction and the full entry-list -> URL -> view -> form -> service -> calculation chain.

**Current model versus the work.** The fixed rule for this audit is ETE 40%, examination 20%, interview 40%. Live code instead stores `ete_weight`, `exam_weight`, and `interview_weight` per vacancy, defaults them to 40/20/40, permits entry managers to edit them until scoring begins, and then locks them (`recruitment/models.py:1506-1559`; `recruitment/forms.py:1430-1449`; `recruitment/services.py:10149-10210`). The entry list exposes the Weights route (`recruitment/templates/recruitment/recruitment_entry_list.html:118-125`; `recruitment/internal_urls.py:149-152`; `recruitment/views.py:2617-2660`). CAR calculation reads those stored values rather than fixed constants (`recruitment/services.py:3284-3295`).

This is not a harmless display option: changing the row changes merit totals and potentially rank/selection. `docs/batch-workflow-redesign-spec.md:165-176` supports editability, but the current audit instruction and `docs/uiux-internal-workflow-plan.md:10-15` explicitly lock 40/20/40. The contradiction itself must be resolved in favor of the fixed rule unless the process owner formally changes that rule.

**Concrete failure.** Two otherwise identical Plantilla vacancies can rank the same candidates differently because an entry manager changed the formula. Once scoring starts, the nonstandard weights lock, making the deviation look like the official fixed computation in finalized reports.

**Proposed change.** Remove editability of the CAR ETE/Exam/Interview split and calculate Plantilla CAR totals with the fixed 40/20/40 constants. Keep examination General/Technical subweights separate; this finding does not declare those subweights fixed. Preserve the exact weights in old report snapshots and mark any nonstandard historical report as legacy rather than silently recomputing it.

**Acceptance criteria:**

1. No UI, form, or service permits changing the Plantilla CAR split from ETE 40 / Exam 20 / Interview 40.
2. CAR calculation, display, print/export, and snapshots all use and label 40/20/40.
3. Examination General/Technical weighting continues under its separately approved rule and is not conflated with the CAR split.
4. Existing finalized reports retain their historical input-weight snapshot; any nonstandard legacy report is visibly identified and never silently rewritten.

#### SL-01 - The COS final decision is not bound to the finalized deliberation pick

- **Flaw pattern:** #3 Two doors, one outcome; #7 Auto-advance without checking.
- **Affected:** COS deliberation and final decision; HRM Chief; vacancy deliberation console and per-case Decision screen.
- **Status:** **CONFIRMED** by full template -> URL -> view -> permission -> service trace and a rollback-safe service probe.

**Current model versus the work.** `CosVacancyDeliberation` describes and stores one chosen case as the vacancy's COS decision (`recruitment/models.py:2615-2631`). Once any vacancy pick is finalized, `deliberation_is_finalized_for_current_stage` marks deliberation complete for every case in that posting (`recruitment/services.py:2955-2959`). The later permission gate checks only branch, stage, section, role, and ordinary process permission; it never checks whether the current case is `chosen_case` (`recruitment/services.py:3714-3726`). The per-case screen then independently offers Selected and Not selected (`templates/internal_includes/decision_body.html:488-548`) and posts through `recruitment/internal_urls.py:188`, `recruitment/views.py:2909-2953`, and `record_final_decision` (`recruitment/services.py:8639-8680`).

The real work has one choice: the applicant selected in the COS deliberation. A later confirmation, if legally required, must confirm that same choice; it cannot be a second independent selection.

**Probe reproduction and observed result.** In a temporary database, two decision-ready COS candidates A and B were placed under the same posting. The real service finalized the vacancy pick for A. `user_can_record_final_decision(hrm_chief, B)` returned `True`, and the real decision service accepted B as Selected. The result was:

```text
pick_chosen_case=A
decision_selected_case=B
mismatch=True
A: application=rejected, case=closed/rejected
B: application=approved, case=completion/active
```

The system therefore rejected its own finalized choice and advanced a different person.

**Concrete failure.** The HRM Chief can accidentally or deliberately select B after the recorded deliberation selected A. The audit trail then contains two incompatible official facts, and the wrong candidate receives completion instructions while the actual pick receives a non-selection notice.

**Proposed change.** Use one authoritative fact. Either finalizing the vacancy pick performs the COS selection transition, or a separate confirmation must be server-constrained to the pick's chosen case and derive its applicant/outcome rather than ask for them again. The Decision screen must show the authoritative pick and notes. This must preserve COS's lighter path: no CAR, HRMPSB, or Appointing Authority.

**Acceptance criteria:**

1. If the finalized pick names A, no permission predicate, service call, or POST can record B as Selected under that pick.
2. The decision/confirmation surface displays the chosen case and deliberation notes from the authoritative vacancy record; it does not ask the HRM Chief to choose the person again.
3. The stored pick, final outcome, notification recipient, and completion case all identify the same application.
4. A test exercises the real console -> pick -> per-case decision chain and proves a mismatched case is unavailable and server-rejected.

#### SL-02 - COS selection closes cases that were never in the deliberated pool and makes later candidates inherit a stale pick

- **Flaw pattern:** #5 Deadlock by construction; #7 Auto-advance without checking; #8 Inexpressible real-world outcome.
- **Affected:** COS deliberation, continuous intake, final decision, and pool closure; HRM Chief and earlier-stage handlers.
- **Status:** **CONFIRMED** for the overbroad closure and stale-pick mechanics. The required repeated-cycle design for continuous COS is **UNVERIFIED pending client policy** on whether one opening can fill multiple contracts.

**Current model versus the work.** A `CosVacancyDeliberation` is one-to-one with the posting and has no cohort, cycle, or membership snapshot (`recruitment/models.py:2621-2631`). Any finalized pick completes deliberation for all current and future cases in that posting (`recruitment/services.py:2955-2959`). When any case is recorded Selected, `_close_cos_non_selected_pool` rejects **every** other active, unlocked case in the posting, with no filter for workflow stage or participation in the deliberation (`recruitment/services.py:8564-8635`). `record_final_decision` always calls that closure for a COS Selected result (`recruitment/services.py:8768-8771`).

The work requires a defined candidate population. A decision may resolve only the applicants actually deliberated. A continuous opening that can receive later candidates also needs a new selection cycle, requisition, or explicit close/reopen boundary; one permanent pick cannot decide candidates who did not yet exist.

**Probe reproduction and observed result.** Candidate C was still in Secretariat screening when a selection was recorded. Candidate D was created and progressed after the old pick was finalized. The observed state was:

```text
C_pending_before_selection: secretariat_review/active
D_later_section_under_old_pick: decision
after_selection:
  C_pending: rejected, closed
  D_later: rejected, closed
```

C was never eligible for the deliberation, and D skipped a new deliberation entirely. Both were closed as if they had lost the old comparison.

**Concrete failure.** A pending resubmission, a newly submitted continuous candidate, or any otherwise active earlier-stage case can receive a false “not selected” outcome and notification. Repeated hiring from a continuous vacancy is impossible to represent correctly because later applicants inherit the first finalized pick.

**Proposed change.** Snapshot the eligible candidate membership when a COS deliberation is finalized and scope resolution to that snapshot. For continuous intake, model an explicit selection cycle/requisition (including the number of appointments it can fill) or require an office-defined intake freeze before deliberation. Do not make application creation or ordinary screening depend on a historical pick.

**Acceptance criteria:**

1. Recording a selection changes only cases in the finalized deliberation snapshot; screening, awaiting-resubmission, and later-created cases remain untouched.
2. A later continuous candidate enters a new deliberation cycle and cannot reach Decision through an older finalized pick.
3. Every non-selected notification references the cycle in which that application was actually considered.
4. Opening-based/pooling behavior remains one coherent batch, while continuous behavior supports the client-approved number of repeated selection cycles.

#### SL-03 - The CAR can finalize, rank, and route a candidate with a blank fixed-formula component

- **Flaw pattern:** #7 Auto-advance without checking; #5 Deadlock/wrong-gate construction.
- **Affected:** Plantilla ETE/interview inputs, HRMPSB quorum, CAR calculation, and Appointing Authority decision support.
- **Status:** **CONFIRMED** by a directly reachable missing-ETE probe and a separate injected fallback-state probe.

This is a residual server-side acceptance gap around previously tracked WF-01/WF-02/WF-11 work (`docs/uiux-internal-workflow-plan.md:44-80,247-259`), not a re-proposal of its presentation changes.

**Current model versus the work.** Candidate assembly allows ETE, exam, interview average, and therefore assessment score to be null (`recruitment/services.py:5912-5966`; nullable item fields at `recruitment/models.py:2944-2952`). Readiness/finalization checks output-record counts and recommendation/quorum, but never requires every fixed-formula component and computed assessment to be present (`recruitment/services.py:5969-6060,6112-6163`). The ranker still assigns an ordinal to an unscored row (`recruitment/services.py:5976-5996`), finalization persists it and automatically routes the case to Appointing Authority Review (`recruitment/services.py:6249-6296,1881-1908`).

The paper fallback is a second producer of the same invalid state. Interview finalization enforces a minimum output count but not the fixed maximum of five distinct HRMPSB raters; it counts each fallback evidence row as one rater (`recruitment/services.py:150-155,4984-5001`), including superseded versions because the query does not limit to current versions (`recruitment/services.py:3161-3175`). A fallback stores neither HRMPSB rater identity nor numeric score (`recruitment/services.py:5517-5555`), while the interview average reads only digital ratings (`recruitment/services.py:3307-3312`).

The work requires three distinct attributable HRMPSB ratings and a numeric interview average before a candidate can receive the 40% interview component, total, or rank. Three versions of one scan are not three panel members.

**Probe reproduction and observed result.** First, a normal CAR-ready candidate with no ETE rating was passed through the real CAR service:

```text
draft_assessment=None
can_finalize=True
final_assessment=None
rank=1
case_stage=appointing_authority_review
```

Separately, three fallback evidence versions were inserted through the permitted data model, then the real interview finalization and CAR services were called:

```text
fallback rows=3, current rows=1, versions=[1,2,3]
digital interview ratings=0
interview finalized=True
CAR interview_average_score=None
CAR assessment_score=None
CAR rank_order=1
CAR finalized=True, candidate recommended=True
```

**Concrete failure.** A candidate can appear as rank 1 and be recommended to the Appointing Authority without any score for 40% of the fixed formula. The resulting CAR is numerically indefensible even though every workflow gate says it is final.

**Proposed change.** Permit incomplete CAR drafts for work in progress, but require non-null ETE, exam, interview average, and computed assessment for every included candidate before finalization. Represent each paper fallback as a structured rating tied to the interview session and actual HRMPSB rater, with an encoded numeric result plus the retained scan. Count distinct raters, never evidence versions, and compute one average across valid digital and fallback ratings.

**Acceptance criteria:**

1. A draft may show missing components, but CAR finalization is server-blocked if any included row lacks ETE, exam, interview, or computed assessment.
2. A CAR item with `assessment_score=None` cannot receive a rank, be recommended, or route to Appointing Authority Review.
3. Plantilla interview finalization requires 3-5 distinct attributable HRMPSB raters; fewer than 3 or more than 5 is rejected, every fallback identifies one actual member and numeric score, and evidence versions never change the count.
4. The finalized CAR and its snapshot contain complete 40/20/40 inputs for every ranked candidate.

### Major

#### SL-04 - Continuous COS candidates are blocked by a vacancy-wide screening batch gate

- **Flaw pattern:** #5 Deadlock by construction; #8 Inexpressible real-world outcome.
- **Affected:** Continuous COS screening -> examination; Secretariat or HRM Chief.
- **Status:** **CONFIRMED** by service trace and rollback-safe probe.

**Current model versus the work.** Continuous COS is deliberately per-case; `vacancy_uses_batch_exam` returns false for that intake mode (`recruitment/services.py:4754-4768`). Nevertheless, `user_can_manage_exam` always applies `vacancy_screening_batch_status` (`recruitment/services.py:3097-3110`). That status counts any other submitted, unfinished, or awaiting-resubmission application as pending (`recruitment/services.py:2875-2929`).

Continuous work requires ready candidates to progress independently. A vacancy-wide “everyone must finish screening” gate has no stable completion point while new candidates may keep arriving.

**Probe reproduction and observed result:**

```text
candidate A: finalized qualified screening, section=exam
candidate B: unfinished screening
vacancy pending=1
user_can_manage_exam(secretariat, A)=False
```

**Concrete failure.** A qualified candidate can wait indefinitely because an unrelated applicant is still screening or resubmitting. The system simultaneously refuses the batch console, leaving no valid examination door.

**Proposed change.** Apply vacancy-wide screening readiness only when the posting actually uses pooled/batch examination. For continuous COS, gate the examination on the current case's own finalized qualified screening.

**Acceptance criteria:**

1. A qualified continuous COS case can schedule/finalize its exam while a sibling remains in screening.
2. An awaiting-resubmission continuous candidate pauses only its own case.
3. Opening-based and pooling COS retain their vacancy-wide screening gate.
4. Existing one-door rules still refuse the batch console for continuous intake and per-case writes for pooled intake.

#### SL-05 - Level 1 COS repeats screening and examination after the Secretariat has already finalized them

- **Flaw pattern:** #1 Redundant capture.
- **Affected:** Level 1 COS; Secretariat examination handoff -> HRM Chief interview.
- **Status:** **CONFIRMED specification conflict.** Live behavior and the prior two-pass plan agree with each other, but conflict with the explicit COS flow supplied for this audit. Do not implement this finding until the process owner resolves which source governs.

**Current model versus the work.** Finalizing a Secretariat-stage COS exam routes the case to HRM Chief Review (`recruitment/services.py:1804-1813`). The COS sequence for that stage starts again at screening, then exam, interview, deliberation, and decision (`recruitment/services.py:2969-2976`). Screening and exam records are stage-local, so the finalized Secretariat records do not satisfy the new HRM Chief-stage sections.

The COS path supplied to this audit is Secretariat screening/exam -> HRM Chief interview. However, `docs/uiux-internal-members-plan.md:92-107` calls the Chief's second screening/exam an intentional independent validation, and `docs/uiux-internal-workflow-plan.md:10-15,263-273` preserves that two-pass model while allowing the first schedule to satisfy the second-stage exam. Thus the implementation is not an accidental dead branch; the governing business sources disagree.

**Probe reproduction and observed result:**

```text
after Secretariat exam:
  application_status=hrm_chief_review
  case_stage=hrm_chief_review
  handler=hrm_chief
  current_section=screening
  hrm_can_screen=True
  hrm_can_interview=False
prior screening/exam records remain only at secretariat_review
```

**Concrete failure.** Under the audit-supplied path, the HRM Chief performs redundant decisions and the two stage-local versions can disagree. Under the older two-pass design, skipping them would remove an intentional independent check. Implementing either interpretation without resolving the conflict would violate a locked source.

**Proposed change.** Obtain a process-owner decision and update all governing documents first. If the audit-supplied path governs, preserve the Secretariat records as completed prerequisites and start HRM Chief work at Interview. If the intentional two-pass model governs, retain the second decisions, state their independent purpose in the business rules, and define how contradictions between first- and second-pass results are resolved. Keep Level 2 COS's direct HRM Chief screening/exam path intact.

**Acceptance criteria:**

1. One approved source unambiguously states whether Level 1 COS is one-pass or two-pass, and the audit prompt/workflow/UIUX documents agree.
2. For one-pass: finalizing the Secretariat exam routes to HRM Chief Interview and no second writable screening/exam record exists.
3. For two-pass: the system labels the second decisions as independent validation, shows the prior basis, and has an explicit rule for disagreement; it never invents a second schedule or re-notifies the applicant for the same sitting.
4. Either resolution leaves Level 2 COS beginning with HRM Chief screening/examination.

#### SL-06 - Interview non-attendance has no valid business outcome

- **Flaw pattern:** #5 Deadlock by construction; #8 Inexpressible real-world outcome.
- **Affected:** COS interview directly; Plantilla interview policy also requires review; HRM Chief and batch interview support.
- **Status:** **CONFIRMED** for COS by model inspection and real finalization probes.

**Current model versus the work.** `InterviewSession` has no attendance/participation outcome such as Completed, Absent, or Rescheduled (`recruitment/models.py:2074-2127`). Per-case finalization requires at least one rating or fallback (`recruitment/services.py:4984-5001`), while pooled COS finalization requires a score for every candidate (`recruitment/services.py:5397-5432`).

A no-show genuinely occurs, but it is not a zero score and no rating sheet exists. The system must record what happened and follow the office-approved consequence; it must not require fabricated evidence.

**Probe reproduction and observed result.** A COS interview session with notes “did not attend” and no rating was passed through the real finalizer. It raised: `ValueError: Record at least one interview rating or upload a fallback rating sheet before finalizing the interview session.` The batch service likewise refused a row without a score.

**Concrete failure.** One absent candidate blocks the whole pooled interview, or staff must invent a numeric score/fallback document to continue. That fabricated value can then affect selection.

**Proposed change.** Add an explicit interview participation outcome and a policy-driven transition for no-show (reschedule, terminal non-selection, or authorized waiver). Never encode absence as numeric zero. The client decision required for the consequence is listed in section 5.

**Acceptance criteria:**

1. A no-show can be recorded without a numeric rating or fabricated sheet.
2. A pooled interview can finalize after each candidate has either a valid score or an approved exceptional outcome.
3. An absent candidate never reaches decision support with a misleading zero or blank score.
4. Reschedule/terminal handling records outcome, actor, timestamp, and reason.

#### SL-07 - The Plantilla paper-rating fallback is visible but unreachable to its authorized support role

- **Flaw pattern:** #4 Unreachable action.
- **Affected:** Plantilla HRMPSB interview; Secretariat/HRM Chief support; Fallback rating sheet form.
- **Status:** **CONFIRMED** by the complete UI-to-service chain and an HTTP probe.

**Current model versus the work.** The application detail view renders the fallback form when `user_can_upload_interview_fallback` is true (`recruitment/views.py:809-816`), and the template posts it (`templates/internal_includes/interview_batch_managed_body.html:55-94`) to `interview-fallback-upload` (`recruitment/internal_urls.py:172-175`). The POST view checks the same specific predicate and calls `upload_interview_fallback_rating` (`recruitment/views.py:2719-2740`). That service then delegates file persistence to the generic evidence uploader (`recruitment/services.py:5517-5541`), whose separate permission rule rejects the Secretariat/HRM Chief support user because the assigned handler at HRMPSB Review is HRMPSB (`recruitment/services.py:7368-7369`).

The work intentionally permits the support office to encode/retain an offline board sheet. A permission predicate that displays the action but cannot survive the persistence layer is not a usable path.

**Probe reproduction and observed result:**

```text
user_can_upload_interview_fallback(secretariat, application)=True
POST /internal/applications/<id>/interview/fallback/ -> redirect with error
message="You cannot upload files for this application."
fallback evidence rows created=0
```

**Concrete failure.** The paper/offline recovery path cannot be used through any rendered Plantilla support workflow. A session that depends on paper sheets cannot be finalized legitimately.

**Proposed change.** Make fallback persistence authorize against the fallback-specific role/stage predicate, not the generic assigned-handler upload predicate. Implement it together with SL-03's structured rater/score correction; fixing only this permission would expose the critical blank-score ranking defect.

**Acceptance criteria:**

1. An authorized Plantilla support user can submit the rendered form and create a fallback record in the same case/stage.
2. Wrong-stage, post-finalization, and unrelated-case POSTs remain server-refused.
3. The fallback record contains the actual HRMPSB rater and numeric rating required by SL-03, not only an evidence file.
4. A request-level test covers template -> URL -> view -> predicate -> persisted structured fallback.

#### SL-08 - Equal CAR totals are silently converted into unequal ranks

- **Flaw pattern:** #8 Inexpressible real-world outcome.
- **Affected:** Plantilla CAR calculation, top-five selection, Secretariat and Appointing Authority.
- **Status:** **CONFIRMED** by deterministic code path and probe.

The tie defect was already recorded in `docs/uiux-internal-workflow-plan.md:357-361`. It is retained here as an unresolved business-logic item because the live model still forces unique ranks and the top-five gate consumes them; this report does not claim it as a new discovery.

**Current model versus the work.** CAR candidates are sorted by descending assessment score and then case reference. The service assigns sequential `preliminary_rank_order` values 1, 2, 3, and so on even where scores are equal, and copies that ordinal into `rank_order` (`recruitment/services.py:5976-5996`). The model enforces one unique `rank_order` per report (`recruitment/models.py:2957-2967`). The Appointing Authority's top-five rule uses that forced ordinal (`recruitment/services.py:8398-8404`).

A case-reference sort can stabilize display order, but it is not a merit distinction. Equal computed totals are a tie and must remain expressible as a tie. The client must choose the official rank convention and the top-five boundary rule; those questions are in section 5.

**Probe reproduction and observed result.** Two qualified Plantilla candidates with equal ETE, exam, and interview totals were passed through the CAR draft builder. Both had the same `assessment_score`, but the earlier case reference received preliminary rank 1 and the other rank 2. The same algorithm can make equal-score candidates rank 5 and 6, causing only the latter to trigger deep-selection documentation.

**Concrete failure.** An arbitrary case number becomes a hidden tie-breaker. One equal candidate may appear outside the top five, changing the Appointing Authority's required justification and making the CAR report a merit ordering that the scores do not support.

**Proposed change.** Preserve tied preliminary ranks according to the client-approved convention. Keep case reference only as a display-order key. Allow official recommendation notes to distinguish candidates after off-system deliberation without rewriting the computed score as unequal.

**Acceptance criteria:**

1. Equal `assessment_score` values receive the same computed preliminary rank.
2. Case reference may order tied rows visually but cannot change rank or top-five treatment.
3. The top-five gate applies the client-approved boundary rule when a tie crosses fifth place.
4. CAR print/export and stored snapshots preserve both the equal score and tie rank.

#### SL-08A - ETE scoring asks for a 40% verdict without the source facts or QS beside it

- **Flaw pattern:** #2 Floating decision.
- **Affected:** Plantilla CAR ETE scoring; Secretariat; Comparative Assessment screen.
- **Status:** **CONFIRMED** by template/view/service trace.

This is a residual decision-basis gap after WF-05 implemented E/T/E sub-scores and free-text bases (`docs/uiux-internal-workflow-plan.md:129-144`). It does not reopen that stored scoring shape.

**Current model versus the work.** The CAR row asks the Secretariat to enter education, training, and experience scores and free-text bases, but presents only the candidate code and scoring inputs (`templates/internal_includes/comparative_assessment.html:141-207`). The application-detail/CAR context does not supply that candidate's education, training, experience records, evidence links, or the position QS beside each row (`recruitment/views.py:629-644,860-865`). The service validates completeness/ranges and computes the ETE value, but it cannot validate the factual basis (`recruitment/services.py:5768-5859`).

ETE is 40% of the fixed CAR total. The scorer needs the target Qualification Standards and the same candidate's documented education/training/experience facts at the point of judgment. A free-text “basis” written from memory is not the underlying evidence.

**Concrete failure.** A scorer can grade the wrong candidate's credentials, overlook a QS requirement, or produce inconsistent ETE values for equivalent records. The CAR will compute correctly from an unsupported input, making the numerical precision misleading.

**Proposed change.** Beside each ETE scoring row, show the position QS and the same candidate's structured education, training, and experience facts or controlled links to their accepted evidence. Keep scoring manual unless the client supplies a fully deterministic rubric; do not invent qualification points.

**Acceptance criteria:**

1. Every editable ETE row displays the position QS and identifies the candidate whose facts are being scored.
2. Education, training, and experience source facts/evidence are available from that row without relying on memory or a separate candidate search.
3. Saved ETE inputs retain a snapshot/reference to the source facts and QS version used.
4. The fixed ETE 40% contribution and overall 40/20/40 formula remain unchanged.

#### SL-09 - A Plantilla batch can begin while the fixed-period applicant pool is still open

- **Flaw pattern:** #7 Auto-advance without checking the complete population.
- **Affected:** Plantilla publication -> screening -> batch examination/interview; Secretariat/HRM Chief support.
- **Status:** **CODE BEHAVIOR CONFIRMED; BUSINESS DEFECT UNVERIFIED** pending client confirmation of whether Plantilla permits multiple cohorts under one vacancy. This is retained as a prioritized risk because the current locked batch language says every post-screening stage is vacancy-batch.

This is a residual stage-boundary question after WF-07/RD-7 protected the publication closing date; it does not re-propose the already-shipped early-close guard.

**Current model versus the work.** `applicant_pool_is_finalized` becomes true only when the entry is Closed or its closing date has elapsed (`recruitment/models.py:722-730`). The screening batch status scans only applications submitted so far (`recruitment/services.py:2875-2930`). Batch examination permission/scheduling checks screening readiness but not pool finalization (`recruitment/views.py:1725-1758`; `recruitment/services.py:4797-4826`), and interview batch scheduling likewise lacks the gate (`recruitment/services.py:5311-5351`). The locked batch specification says screening is individual and every later stage is vacancy-batch (`docs/batch-workflow-redesign-spec.md:18-25`).

For a fixed-period recruitment, “all screened” cannot be known before the last timely applicant can submit. Starting the single vacancy batch earlier creates an implicit second cohort that the model never names.

**Probe reproduction and observed result:**

```text
posting closing date=future
posting applicant_pool_is_finalized=False
all currently submitted cases qualified=True
batch examination schedule service=SUCCESS
```

A later applicant can still submit within the valid publication period after that sitting has been created.

**Concrete failure.** A timely later applicant misses the supposed vacancy-wide examination/interview cohort and must be processed in an unmodeled second batch, or is informally excluded despite applying on time.

**Proposed change.** For Plantilla, keep individual screening available during publication, but block batch examination scheduling/scoring/finalization, and therefore interview, until the applicant pool is final. If the client confirms multiple cohorts are intentional, model those cohorts explicitly instead of silently deriving them from timing.

**Acceptance criteria:**

1. Before closing-date expiry/entry closure, batch examination GET/POST and service calls return a clear “applicant pool not final” block.
2. Every application submitted within the intake window can join the same final Plantilla batch after its own screening decision.
3. Once the pool is final, existing examination/interview batch behavior is unchanged.
4. Continuous COS per-case processing is unaffected.

#### SL-10 - Mid-process withdrawal exists as a label but has no workflow transition

- **Flaw pattern:** #8 Inexpressible real-world outcome; #10 Orphaned logic.
- **Affected:** All pre-decision stages; applicant and current internal handler; both branches.
- **Status:** **CONFIRMED** by action inventory and a real workflow-action probe.

Withdrawal was already identified in `docs/uiux-internal-workflow-plan.md:357-359`. It remains here as an unresolved business-state gap, not a fresh UI/UX proposal.

**Current model versus the work.** `RecruitmentApplication.Status.WITHDRAWN` exists (`recruitment/models.py:797-806`), and some labels/pool filters recognize it. `RecruitmentCase.CaseStatus` has no matching state (`recruitment/models.py:956-962`). The action enumerator offers no withdrawal (`recruitment/services.py:7629-7714`), and the transition mapping has no producer (`recruitment/services.py:7717-7768`). Applicant help tells a person who wants to withdraw to contact the assistance desk, but no internal action records the result (`recruitment/templates/recruitment/applicant_help.html:155-162`).

**Probe reproduction and observed result:**

```text
submitted active Plantilla case: available workflow actions=[]
process_workflow_action(..., action="withdraw", ...) ->
  ValueError "This action is not allowed at the current step"
application remains secretariat_review; case remains active
```

The real work needs a neutral, auditable exit from an active competition. Withdrawal is not rejection, disqualification, or non-selection.

**Concrete failure.** Staff must leave a withdrawn applicant active or misclassify the person as rejected. The case can continue blocking a batch, receive schedules/notices, and appear in workload and applicant-history counts.

**Proposed change.** Add a controlled withdrawal transition that synchronizes application and case state, removes the case from active pools, preserves rather than deletes the record, and records the client-required reason/evidence and confirmation. Client choices about initiator, cutoff, and documentary proof are in section 5.

**Acceptance criteria:**

1. Withdrawal can be recorded through an authorized workflow path at every client-approved stage.
2. The case becomes terminal/locked, has no active handler, and is excluded from screening, exam, interview, CAR, and selection pools.
3. Status, reports, and notices distinguish withdrawal from rejection and non-selection.
4. Actor/source, timestamp, previous stage, reason, and supporting reference are auditable.

#### SL-11 - Vacancy cancellation after publication cannot be represented

- **Flaw pattern:** #8 Inexpressible real-world outcome.
- **Affected:** Recruitment entry lifecycle and every in-flight application; both branches.
- **Status:** **CONFIRMED** that cancellation has no distinct state and Suspension/Closed do not perform it. The authorizing role, reversibility, and exact applicant disposition are **UNVERIFIED client policy**.

**Current model versus the work.** Entry statuses are only Draft, Active, Suspended, and Closed (`recruitment/models.py:379-383`). `update_recruitment_entry_status` changes the posting and records that status change, but does not resolve its cases (`recruitment/services.py:10290-10327`). Suspension is an intake pause: examination pool and scheduling services ignore it (`recruitment/services.py:4771-4809`). Closed is the ordinary final-pool state, not a neutral cancellation.

**Probe reproduction and observed result:**

```text
after Suspended:
  case=active at exam
  case remains in exam pool
  exam scheduling succeeds
after Closed:
  applicant_pool_is_finalized=True
  active review case remains processable
```

The real work must distinguish “the office cancelled this vacancy” from “applications are temporarily paused” and “the normal intake period ended.”

**Concrete failure.** Staff must misuse Suspended or Closed. Suspension permits the cancelled recruitment to continue; Closed can unlock normal pooled evaluation and eventual selection. Neither captures a cancellation reason or gives applicants a neutral vacancy-cancelled disposition.

**Proposed change.** Add a distinct audited cancellation transition with reason/effective date and a defined non-merit disposition for every active application. Keep ordinary Closed and temporary Suspended semantics unchanged. Client decisions about authority, reversibility, and applicant wording are in section 5.

**Acceptance criteria:**

1. Cancelled is distinguishable from Suspended and normally Closed in state, reports, and history.
2. Cancellation blocks subsequent stage writes and removes affected cases from active pools.
3. Every in-flight application receives the client-approved neutral cancellation disposition and notice, not a candidate rejection.
4. Actor, reason, effective time, and affected case set are preserved in one auditable transition.

#### SL-12 - Generic reopen unlocks the case but leaves the finalized stage record uneditable

- **Flaw pattern:** #5 Deadlock by construction; #9 One-way door.
- **Affected:** Authorized correction of finalized screening/exam/interview records; HRM Chief; both branches.
- **Status:** **CONFIRMED** through the rendered reopen path, real reopen service, and post-reopen permission/UI probe.

**Current model versus the work.** The application detail exposes a reopen form whenever an HRM Chief sees a locked case with `locked_stage` (`recruitment/services.py:2798-2804`; `recruitment/views.py:981-982`; URL `recruitment/internal_urls.py:247`). `reopen_recruitment_case` moves the case back to `locked_stage`, clears case locking, sets it Active, and records reason/audit history (`recruitment/services.py:8892-8968`). It does not unfinalize or version the screening, exam, or interview record at that stage. Those services independently refuse finalized records (`recruitment/services.py:4337-4347,4603-4611,4936-4946`), and the detail view renders finalized screening as locked rather than creating a form (`recruitment/views.py:690-698`).

The work requires reopening the actual decision being corrected, not merely changing the envelope around it. The historical version must remain auditable, but an authorized new version/draft must become editable.

**Probe reproduction and observed result.** A not-qualified finalized screening case was rejected/closed, then reopened through `reopen_recruitment_case`:

```text
before: application=rejected; case=closed/rejected; locked_stage=secretariat_review
after:  application=secretariat_review; case=secretariat_review/active; unlocked
user_can_manage_screening=True
screening_form rendered=False; screening_locked=True
available workflow actions=[]
save_screening_review(...) -> "Finalized screening records cannot be edited."
```

**Concrete failure.** The dashboard says the case is reopened and assigned, but the handler cannot change the decision or move the case forward. It becomes active workload with no valid completion action.

**Proposed change.** Make reopen stage-specific. Reopen/version the underlying finalized record or create a correction version while preserving the old immutable record and its reason/actor link. Do not weaken post-finalization editing globally. Completion-only reopen may retain its existing semantics where the completion record is the intended editable object.

**Acceptance criteria:**

1. Reopening a finalized screening/exam/interview case creates exactly one editable current correction version for the locked stage.
2. The previous finalized values remain immutable and linked to reopen actor, reason, and timestamp.
3. The reassigned handler can complete the corrected stage and follow the normal next transition.
4. A case may never be Active/unlocked at a section whose only underlying record remains finalized and uneditable.

#### SL-13 - An overdue completion record cannot be updated without rewriting its historical deadline

- **Flaw pattern:** #5 Deadlock by construction; #9 One-way door.
- **Affected:** Completion tracking; Secretariat for Level 1, HRM Chief for Level 2; both branches.
- **Status:** **CONFIRMED** by form logic and bound-form probe.

**Current model versus the work.** The completion form always rejects a deadline earlier than today (`recruitment/forms.py:1293-1297`), including when editing an existing record whose deadline was valid when set. The detail and POST paths bind that existing record back through the same form (`recruitment/views.py:950-964,2956-3023`). Closure correctly depends on structured requirements and completion references (`recruitment/services.py:3762-3780,8776-8800`), so a handler must successfully save the form before a late item can be marked complete.

An elapsed deadline is historical evidence of lateness, not invalid data. Completing a requirement after it is due must retain the original due date and show that the item was late.

**Probe reproduction and observed result.** An existing CompletionRecord with yesterday's deadline and a pending requirement was posted unchanged except that the requirement was marked Completed. The bound `CompletionTrackingForm` was invalid with `Completion deadline cannot be earlier than today.` Changing the deadline to today made the update valid, demonstrating that the only available route rewrites history.

**Concrete failure.** Staff cannot accurately close a late-but-completed appointment/contract case. They must either leave it permanently open or move the deadline forward and erase the evidence that completion was late.

**Proposed change.** Apply “not earlier than today” only when initially creating a deadline or intentionally changing it to a new value. Permit an unchanged historical deadline on later edits. Record actual completion dates and derive overdue/late status rather than mutating the original deadline.

**Acceptance criteria:**

1. An existing past deadline may be submitted unchanged while requirements and completion references are updated.
2. Changing an existing deadline follows the client-approved extension rule and records old value, new value, actor, reason, and time.
3. Late completion retains the original deadline and exposes actual completion date/overdue status in the record.
4. A newly created completion deadline still cannot be earlier than the creation date.

#### SL-14 - A recorded final selection has no rescind or replacement path

- **Flaw pattern:** #9 One-way door.
- **Affected:** Plantilla Appointing Authority selection and COS selected decision; Appointing Authority/HRM Chief correction.
- **Status:** **CONFIRMED** that no rescind/replacement path exists and ordinary reopen cannot substitute for one. The authorized role, cutoff, and reassessment consequences are **UNVERIFIED client policy**.

**Current model versus the work.** Plantilla permits one `FinalSelection` per recruitment entry (`recruitment/models.py:3055-3061`). The permission gate disappears once that record exists (`recruitment/services.py:3739-3745`), and the recording service refuses another selection (`recruitment/services.py:8372-8384`). COS decisions are likewise stored as final case outcomes. The internal URL inventory contains create/return/reopen/closure operations but no void, rescind, or replacement operation (`recruitment/internal_urls.py:177-247`). Generic reopen cannot remove the vacancy-level selection: it blocks non-selected siblings while a selection stands and reopens only the winning case's locked stage (`recruitment/services.py:8892-8958`). Before completion closure, the winner is not locked, so it is not reopenable at all; after closure, reopen returns it only to Completion.

Final selection should be strongly controlled, but a clerical mis-selection, formal withdrawal of the selected person, or authorized reconsideration still needs a correction model. “Immutable with no superseding record” turns a mistake into the official truth.

**Probe reproduction and observed result.** After recording a Plantilla final selection, a second record attempt was rejected and the selection form was no longer available. The selected case at Completion failed `user_can_reopen_case` because it was unlocked; after completion closure, reopen returned it to Completion and the original `FinalSelection` remained authoritative. A non-selected sibling was refused reopen while that selection stood.

**Concrete failure.** If the wrong candidate is clicked or the selected person withdraws before appointment/contract completion, the vacancy cannot be corrected to the authorized replacement. Editing lower-stage case state cannot change the selection fact or notifications already issued.

**Proposed change.** Add a narrowly authorized rescind/supersede transition for the vacancy-level selection, with mandatory reason and immutable history. It must define what happens to the former selected case, completion work, previously closed candidates, notices, and the CAR/pick snapshot. Do not delete or overwrite the original record.

**Acceptance criteria:**

1. A client-approved role can rescind a selection only with a required reason and, if policy requires, supporting authority/reference.
2. The original selection and all prior notices remain immutable; a linked superseding record identifies the replacement decision.
3. Former-winner and replacement-candidate states transition consistently, with no vacancy having two currently effective selections.
4. Re-selection reuses the original finalized CAR or COS cycle unless an explicit reassessment/new-cycle transition is authorized.

### Minor

#### SL-15 - Legacy per-case Plantilla deliberation mutation logic is orphaned behind an unreachable section

- **Flaw pattern:** #4 Unreachable action; #10 Orphaned logic.
- **Affected:** Plantilla post-interview/HRMPSB recommendation; legacy route and service.
- **Status:** **CONFIRMED** by sequence trace and direct HTTP probe.

**Current model versus the work.** The intended Plantilla sequence is Interview -> CAR -> Actions (`recruitment/services.py:2969-2985`), with off-system board deliberation encoded as the recommendation on CAR. A legacy permission predicate still requires a Plantilla `deliberation` section that the sequence can never produce (`recruitment/services.py:3571-3585`). The mutation route, view, service, and model remain live (`recruitment/internal_urls.py:177`; `recruitment/views.py:2743-2793`; `recruitment/services.py:5574-5728`; `recruitment/models.py:2459-2605`), even though current CAR logic states the separate Plantilla deliberation step was removed (`recruitment/services.py:5887-5894`).

**Probe reproduction and observed result.** With a completed Plantilla interview, the derived section was `car`, no deliberation form was rendered, and direct POST to the legacy route returned 403.

**Concrete failure.** There is no present wrong outcome, but a second obsolete write model remains one permission change away from reintroducing two incompatible recommendation sources. Stale code also misleads future implementers about the official process.

**Proposed change.** Quarantine/remove the legacy Plantilla mutation route and service while preserving existing historical records read-only. Do not affect `CosVacancyDeliberation` or the current Secretariat-encoded CAR recommendation.

**Acceptance criteria:**

1. No Plantilla URL, form, or service can create/update the legacy per-case deliberation record.
2. Historical records remain viewable/exportable as legacy history and are not deleted.
3. CAR recommendation remains the sole current Plantilla recommendation source.
4. COS per-vacancy deliberation behavior is unchanged except for SL-01/SL-02 corrections.

## 4. State sweep results

### Method and boundaries

`RecruitmentCase` independently permits six stages and five case statuses (`recruitment/models.py:948-962`), while application status and current handler are stored separately. The model's `save()` synchronizes only branch (`recruitment/models.py:989-991`); it does not constrain the stage/status/handler cross-product. Runtime services produce a much smaller canonical subset. The tables below enumerate that service-produced subset and the dangerous model-permitted cells reached by probes or direct state construction.

“Unreachable” means no normal service transition produces the combination. “Inescapable” means the combination can exist or was reached, but the normal permission/service gates offer no valid next transition. “Terminal” means no next workflow action is expected.

### Canonical Plantilla cells

| Case stage | Application status | Case status | Handler role | Derived workflow section | Classification | Notes |
|---|---|---|---|---|---|---|
| Secretariat Review (Level 1) | Secretariat Review | Active | Secretariat | Screening -> Exam -> Actions | Reachable | Correct initial Level 1 route. Qualified exam advances to HRMPSB. |
| HRM Chief Review (Level 2) | HRM Chief Review | Active | HRM Chief, or Secretariat under explicit override | Screening -> Exam -> Actions | Reachable | Correct Level 2 route and controlled handoff. |
| Same screening stage | Returned to Applicant | Awaiting Resubmission | Original internal reviewer | Screening | Reachable, paused | RD-1 return keeps the case visible to its reviewer; resubmission or Remove from Batch can escape. |
| HRMPSB Review | HRMPSB Review | Active | HRMPSB Member, with defined support-role actions | Interview -> CAR -> Actions | Reachable | Canonical panel/decision-support state. Separate legacy Deliberation is unreachable (SL-15). |
| HRMPSB Review after CAR return | HRMPSB Review | Active | HRMPSB Member/support workflow | CAR | Reachable | Returned CAR version is preserved and a replacement version is required. |
| Appointing Authority Review | Appointing Authority Review | Active | Appointing Authority | Decision | Reachable | Requires a current finalized, non-returned CAR. |
| Completion | Approved | Active | Secretariat (Level 1) or HRM Chief (Level 2) | Completion | Reachable | Selected winner; case remains active until requirements and reference/date are complete. |
| Closed | Rejected | Rejected | Blank | Completion/read-only | Terminal | Not qualified, exam Absent/Waived, or non-selected. |
| Closed | Approved | Approved | Blank | Completion/read-only | Terminal | Completed appointment/contract. HRM Chief reopen returns only to Completion. |

### Canonical COS cells

| Case stage | Application status | Case status | Handler role | Derived workflow section | Classification | Notes |
|---|---|---|---|---|---|---|
| Secretariat Review (Level 1) | Secretariat Review | Active | Secretariat | Screening -> Exam -> Actions | Reachable | Correct initial route. |
| HRM Chief Review after Level 1 exam | HRM Chief Review | Active | HRM Chief | Screening | Reachable; governance conflict | Matches the older intentional two-pass plan but contradicts the one-pass flow supplied for this audit (SL-05). |
| HRM Chief Review (Level 2) | HRM Chief Review | Active | HRM Chief, or Secretariat under explicit override | Screening -> Exam -> Interview -> Deliberation -> Decision | Reachable | Correct direct Level 2 ownership; exceptional outcomes remain defective. |
| Same screening stage | Returned to Applicant | Awaiting Resubmission | Original internal reviewer | Screening | Reachable, paused | Correct per-case pause; incorrectly blocks ready continuous siblings (SL-04). |
| HRM Chief Review after finalized pick | HRM Chief Review | Active | HRM Chief | Decision | Reachable, unsafe | No candidate snapshot exists; the posting-wide pick governs every current or later case that reaches Decision (SL-01/SL-02). |
| Completion | Approved | Active | Secretariat (Level 1) or HRM Chief (Level 2) | Completion | Reachable | Selected COS appointee. |
| Closed | Rejected | Rejected | Blank | Completion/read-only | Terminal | Not qualified, Absent/Waived examination, or not selected. |
| Closed | Approved | Approved | Blank | Completion/read-only | Terminal | Completed contract. |

### Unreachable, unsafe, and inescapable cells

| Stage x case status x handler x section | Classification | Evidence and consequence |
|---|---|---|
| Continuous COS: review stage x Active x assigned reviewer x Exam, with any unrelated sibling pending screening | **Reachable, conditionally blocked by unrelated state** | Current case is ready, but `user_can_manage_exam=False`; the block may clear only if the sibling is resolved/removed, not through work on this case (SL-04). |
| COS later candidate x Active x HRM Chief x Decision under a historical pick | **Reachable, unsafe** | Later case skips its own deliberation and is resolved by a cycle it never joined (SL-02). |
| Any earlier-stage COS sibling x Active x Secretariat/HRM Chief x Screening/Exam when another case is selected | **Reachable, wrongly terminalized** | `_close_cos_non_selected_pool` changes it directly to Closed/Rejected (SL-02). |
| Reopened screening case x Active/unlocked x assigned reviewer x Screening, with finalized ScreeningRecord | **Reachable, inescapable** | Reopen succeeds, but no form/action can edit or advance the finalized record (SL-12). The same construction exists for finalized exam/interview records. |
| HRMPSB Review x Active x HRMPSB/support x CAR, with missing ETE or another formula component | **Reachable, unsafe and escapable forward** | CAR finalizes, assigns rank, and routes to Appointing Authority despite `assessment_score=None` (SL-03). |
| HRMPSB interview x Active x support/HRMPSB x finalized session with zero numeric ratings and three fallback versions | **Model-permitted, unsafe and escapable forward** | Injected-state probe reached finalized CAR/rank 1. Intended UI producer is currently unreachable (SL-03/SL-07). |
| Completion x Active x level handler x Completion, with past deadline and pending requirement | **Reachable, inescapable truthfully** | Requirement cannot be marked complete without changing the historical deadline (SL-13). |
| Application Withdrawn x any case status/stage/handler | **Model-permitted, no legitimate producer** | Application enum exists, but no synchronized case state or workflow action exists (SL-10). |
| COS x HRMPSB Review x Active x HRMPSB x Interview/CAR | **Normal-workflow unreachable; inescapable if constructed** | COS interview management at HRMPSB and COS CAR management are forbidden; no normal transition should create it. |
| COS x Appointing Authority Review x Active x Appointing Authority x Decision | **Normal-workflow unreachable** | COS correctly skips Appointing Authority; the state is model-permitted but has no valid COS decision operation. |
| Closed stage x Active/unlocked x internal handler x Completion | **Normal-workflow unreachable; inescapable if constructed** | Stage says terminal while status/lock say active; no canonical transition owns correction. |
| Review stage x mismatched application review status or handler | **Normal-service unreachable; guarded** | `_ensure_case_stage_alignment` blocks subsequent transition rather than silently reconciling. Data repair is required. |
| Plantilla HRMPSB Review x Active x HRMPSB x Deliberation | **Unreachable by design** | Current sequence has no such section and direct legacy POST is 403 (SL-15). |
| Any terminal non-selected sibling x locked Closed x blank handler, while final selection stands | **Terminal by design** | RD-7 correctly prevents ordinary reopen. The missing operation is vacancy-level selection rescission (SL-14), not weaker sibling reopening. |

### Defended sound exceptional behavior

- **Multiple applications by one person:** supported. The constraint is unique per applicant plus position, not unique per applicant globally (`recruitment/models.py:868-875`).
- **Pooled examination/interview one-door control:** server-enforced in both directions. Pooled per-case POSTs redirect/refuse, and continuous COS batch consoles refuse (`recruitment/views.py:1704,1906,2272-2286,2355-2366,2397-2412,2663`).
- **Finished batch refresh/back:** no longer 404s; the finished console redirects with an explanatory message when appropriate (`recruitment/views.py:1620-1635`; commit `04dba6c`).
- **Examination Absent/Waived:** terminal and excluded from forward ranking rather than auto-advanced (`recruitment/services.py:1829-1834,4734`). No passing score is introduced.
- **Awaiting-resubmission removal:** now reachable directly from the case status, closing the previously dead `remove_applicant` gate (`recruitment/services.py:7637-7652`).
- **CAR return for reassessment:** returned versions are preserved and the case returns to the CAR path; the current finalized version is the selection source. The final-selection correction gap in SL-14 is different.
- **Completion readiness:** derived from structured requirement rows and required completion/contract references; closure is unavailable until blockers clear (`recruitment/models.py:3447-3466`; `recruitment/services.py:3762-3780,8776-8800`).
- **Notification delivery:** material schedule/decision notifications expose delivery/retry state under RD-9; no additional process-model failure was observed.

## 5. Open questions for the client

> **UPDATE 2026-07-26: all twelve are RESOLVED — §1A Resolution Log holds the binding
> answers** (1→SL-02 rounds; 2→SL-01 pick-is-decision; 3→SL-06 letter of intent, both
> stages; 4→Secretariat encodes, per-member sheet + SL-03 structured fallback; 5→SL-08
> competition ranking, tied band in top five; 6→no rubric exists, SL-08A stands;
> 7→SL-09 wait for pool final; 8→SL-10 withdrawal flow; 9→SL-11; 10→SL-14; 11→audited
> extension per SL-13 policy; 12→SL-13, never auto-forfeit). The original questions are
> preserved below for the record; only the §1A "client courtesies" remain, and none block
> implementation.

These questions cannot be settled from code. They are intentionally not answered inside the findings.

1. **Continuous COS selection cycles:** Can one continuous opening produce repeated hires? If yes, what creates/closes one deliberation cycle, how many contracts may it fill, and may candidates roll into the next cycle?
2. **COS deliberation versus decision:** Is finalizing the per-vacancy deliberation pick itself the legally operative selection, or is a separate HRM Chief confirmation required? If separate, may it ever differ from the pick? The model should not permit silent disagreement either way.
3. **Interview no-show:** Is the outcome reschedule, terminal non-selection, authorized waiver, or case-by-case discretion? Does the same rule apply to Plantilla and COS?
4. **Paper HRMPSB ratings:** Who encodes the numeric rating from each paper sheet, and what identifier must tie the scan to the actual HRMPSB member?
5. **CAR tie convention:** Use competition rank (1,1,3), dense rank (1,1,2), or another official convention? If equal scores span fifth place, are all members of the tied band treated as within the top five?
6. **ETE basis:** What official rubric and source data convert education, training, and experience into scores? Are the current ETE subweights authoritative? This question does not reopen the fixed overall ETE 40 / Exam 20 / Interview 40 formula.
7. **Plantilla batch timing:** Must examination/interview wait until the publication window closes and the pool is final, or does the office intentionally run named multiple cohorts under one vacancy?
8. **Withdrawal:** Who may initiate and confirm it, until what stage, and is a signed withdrawal letter/evidence mandatory? What happens if the selected person withdraws during Completion?
9. **Vacancy cancellation:** Who authorizes cancellation after publication, may it ever be reversed, and what official neutral disposition/notice must each in-flight applicant receive?
10. **Selection rescission:** Who may void an incorrectly encoded Plantilla/COS selection, until what cutoff (notification, completion start, appointment/contract issuance), and must the matter return to the board/deliberation cycle?
11. **Late screening resubmission:** May a two-week resubmission deadline be extended or a late-but-valid document accepted? If yes, which role authorizes it and what evidence/notice is required? The current service hard-blocks overdue applicant submission and has no extension operation (`recruitment/services.py:7804-7808,7912`).
12. **Missed completion deadline:** Does lateness merely remain visible while processing continues, trigger escalation/extension, or terminate the selection? What role may extend the deadline, and must the original date remain immutable?

## 6. Out of scope / escalate

- **Documentation governance:** `docs/client-workflow-alignment.md:134-136` describes older branch/level scoring splits, while `docs/batch-workflow-redesign-spec.md:165-176` describes an editable per-vacancy CAR split. Both conflict with the fixed 40/20/40 rule in this audit and `docs/uiux-internal-workflow-plan.md:10-15`. Align the documents after SL-00; do not change the fixed formula to match stale/conflicting text. Likewise, resolve SL-05's one-pass/two-pass COS conflict before changing code or future audit prompts.
- **Audit-brief drift:** The brief's RD-1/RD-2 implementation status was stale, as recorded in section 1. Align future audit prompts with the current `main` history.
- **Test-fixture maintenance:** After `seed_demo_data --force`, `seed_e2e_test_cases` rolled back with `ValueError: This interview needs ratings from at least 3 HRMPSB members...`; its Plantilla helper seeded only one rating under the current quorum rule. This is a verification-fixture defect, not a recruitment-process finding, but it should be repaired so the documented dry-run remains usable.
- **Presentation/UI polish:** Layout, wording density, responsiveness, button placement, and visual hierarchy were not audited. Send those to the existing `docs/uiux-*.md` workstreams.
- **Security:** Authentication, authorization robustness beyond reachability tracing, cryptography, session policy, and attack resistance were not assessed. Route any such observation to a separate security review.

## 7. Overall audit conclusion

The happy-path skeleton is substantially correct: branch separation, level-aware ownership, shipped RD-1/RD-2 one-door behavior, scored examination without a threshold, off-system HRMPSB deliberation with Secretariat encoding, Appointing Authority return/versioning, completion checklist derivation, and notification delivery all have defensible implementations. The 40/20/40 CAR **intent** is correct, but its live per-vacancy editability is not (SL-00).

The system is not yet business-logic complete. Its highest-risk failures occur where an exceptional case crosses a vacancy-level boundary: COS stores two incompatible choices; selection closes people who were never deliberated; CAR finalization can advance a mathematically incomplete candidate; and several legitimate lifecycle events have no safe transition. The implementation order should therefore be:

1. SL-00: enforce the fixed CAR formula and align the conflicting governing documents.
2. SL-01 and SL-02: establish one authoritative COS decision and a frozen/cycled pool.
3. SL-03 and SL-07: make CAR completeness non-negotiable and redesign fallback ratings before making the dead upload path reachable.
4. SL-04 through SL-06 and SL-08/SL-08A: correct continuous gates, interview outcomes, tie semantics, and ETE decision support; resolve SL-05's governance conflict before changing its sequence.
5. SL-09 through SL-14: close lifecycle/correction deadlocks after client policy questions are answered.
6. SL-15: remove/quarantine orphaned Plantilla mutation logic without disturbing historical records or COS deliberation.

No finding in this document authorizes removing a fixed rule listed in section 1.
