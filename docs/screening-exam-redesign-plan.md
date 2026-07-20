# Screening & Exam Redesign Plan

Post-revision quality workstream, designed and locked with the team (2026-07-17).
Two items, built one at a time, one commit each: **RD-1 (screening) first, RD-2
(exam) second.** Both run on `main`, same norms as docs/defense-revisions-plan.md
(tests with .venv python + DJANGO_DEBUG/SECURE_SSL_REDIRECT/empty POSTGRES_* env;
browser-verify against the local Postgres demo DB; restart the preview server
after template edits).

---

## RD-1 — Screening redesign: "verify, then decide"

**Problem (team's own critique).** Step 2 asks the reviewer whether required
documents are complete, but intake already guarantees every required file is
uploaded, and Step 1's per-document review (Complied / Return for Resubmission)
already determines validity — so the question is redundant double-entry, and the
"must be Complied before Complete" contradiction errors exist only because of
that redundancy. Step 3's verdict is real (the CSC Qualification Standards
check) but is presented as a floating yes/no. Resubmission is bolted on after
finalize, forcing a fake verdict on documents the reviewer just flagged.

**Design (final; survived three adversarial passes):**

Two steps on the existing step-rail (3 stops become 2):

1. **Verify each document** — per-document Complied / Return for Resubmission
   (unchanged from REV-C). Completeness is **computed and displayed live**
   ("✓ All N required documents complied" / "⚠ K flagged"), never asked.
2. **Decide** — three mutually exclusive actions:
   - **Return to applicant** — offered when ≥1 document is flagged. ONE atomic
     action: saves the screening state (flags + remarks, record NOT finalized)
     AND fires the existing return workflow (case status, 2-week resubmission
     deadline, applicant email, resubmission page) in a single transaction.
     Compose the existing proven services; no in-between state on failure.
   - **Not Qualified** — available at ANY time, flags or not (asymmetric gate:
     certainty of failure can come early). Justification required (existing
     rule). When flags exist, show a confirm noting pending flags become moot,
     and **suppress the resubmission email** — the applicant receives exactly
     one message: the rejection. Definitive-cut auto-reject coupling (Gap B)
     applies.
   - **Qualified** — unlocks ONLY when every required document is Complied
     (or N/A). You cannot pass someone on unvalidated evidence. Show the
     position's Qualification Standards (posting qualification_reference +
     position_reference fields, whatever exists) beside the verdict so it reads
     as "compare against the QS", not a floating question.

**Mechanics & constraints:**
- `ScreeningRecord.completeness_status` **stays in the DB**, auto-derived from
  the document review (all required Complied/N-A → complete; else incomplete)
  so CAR, exports, locked views, and audit history don't break. The form field,
  its dropdown, its notes-required-when-incomplete rule, and the
  contradiction-error validation are removed from the UI/forms/services.
  completeness_notes may remain as optional observations.
- **Dead-wire test before deleting the ETE score boxes**: the screening form's
  education/training/experience score fields are believed dead (CAR ETE comes
  from ApplicationETERating on the CAR screen since workflow-governance), but
  CAR items still carry a document_review_score column. TRACE actual readers
  in code first; delete only if nothing consumes them, otherwise keep and
  report what uses them.
- HRM Chief level-2 re-review uses the same form — same behavior; update the
  UX-03 "copy prior review" JS (it currently copies completeness_status, which
  no longer exists as an input).
- Returned case parks the vacancy's exam batch until resubmission, exactly as
  today (§4.3 gate untouched). Locks, authorized reopen, audit — untouched;
  no new edit routes.
- Autosave continues to work on step 1 (and continues NOT to write audit rows,
  per REV-D).
- Terminology stays REV-C's: Complied / Return for Resubmission /
  Not Submitted / Qualified / Not Qualified.

**Acceptance:**
- The completeness question does not exist anywhere a reviewer can see; the
  computed banner reflects the live table state.
- With a flagged document: Qualified is not offered (server-enforced, not just
  hidden); Return performs the save+return atomically; Not Qualified works with
  confirmation and sends ONLY the rejection notice.
- With all documents complied: Qualified finalizes as today and the case
  proceeds; stored completeness_status == complete on the record.
- Full screening test class green after rework; no stray references to the
  removed form fields.

---

## RD-2 — Exam consolidation: "one door, one story"

**Problem (team-spotted on the live site).** For a pooled vacancy, the batch
exam console AND the per-case exam wizard are simultaneously editable (score
last-write-wins), and the two surfaces disagree about what a COS exam is
(console: single end-user score; per-case wizard: General 60% / Technical 40%).

**Decision (ASSUMPTION, pending client confirmation — flip via weights, not
architecture):** COS exams score like Plantilla: **two components, General
Ability + Technical, combined by the vacancy's weights (default 60/40)** via
`assessment_weights_or_default`.

**Design:**
1. **COS two-component everywhere**: the batch console's COS table gets
   General | Technical columns with the auto-computed overall (matching the
   per-case wizard); the demo seeder writes both components for COS.
2. **One door for pooled vacancies** (Plantilla always; COS whose intake is
   fixed-period, opening-based, or **pooling** — corrected 2026-07-18 with the
   team: "pooling" forms a pool, so it batches; only CONTINUOUS COS stays
   per-case): the per-case exam tab becomes a READ-ONLY summary (schedule,
   outcome, scores) with "Scores are managed in the batch exam console."
   Server-enforced: the per-case exam POST is refused for pooled vacancies
   (PT-03 lesson — never client-side-only). Scheduling, scoring, finalize
   happen only in the console.
   - **Absent / Waived** becomes a per-candidate outcome dropdown in the
     console row; non-Completed outcomes disable that row's score cells.
   - **Validity dates** are set once in the console and applied to the whole
     batch (same sitting → same window).
   - **Evidence upload** stays available on the per-case page (additive,
     conflict-free).
3. **Continuous/pooling COS** (no pool): the full per-case editor stays,
   with the two-component scoring.
4. Post-finalize corrections stay on the existing authorized-reopen path.

**Acceptance:**
- Pooled vacancy: per-case exam tab shows data but accepts no schedule/score/
  finalize POSTs; the console records outcomes + both components + validity
  and finalizes the pool.
- The COS scoring presentation is identical on both surfaces.
- Continuous-intake COS case: per-case editor fully functional.
- Batch exam + exam test classes green after rework; seeder runs clean
  (`seed_demo_data --force`).

---

## RD-3 — ETE single source: "screening checks papers, the CAR gives grades"

**Status: DONE (commit 0d79afc, 2026-07-20).**

**Problem.** The ETE score has two writable sources: the screening form's
education/training/experience/document-review boxes (optional, per-case, scored
in isolation) and the CAR screen's ApplicationETERating (authoritative,
pool-in-view). services.py `ete_component = ete_rating or document_review_score`
silently picks one — two candidates on one CAR can carry scores of different
provenance with nothing marking which. A ranking component should be scored at
one moment, comparatively.

**Design:**
- Remove the ETE score inputs from the screening form/template. Screening is
  verification only (RD-1's own philosophy).
- KEEP the model fields and the CAR read-fallback — locked/historical records
  keep their numbers; nothing recomputes. New records never populate them, so
  the CAR rating becomes the only live source going forward, no migration.
- CAR already behaves correctly when absent (blank until rating entered).
- ASSUMPTION (same class as RD-2's): the office scores ETE at comparative-
  assessment time, not during initial HR review. UI-level removal — cheap to
  flip back if the client says otherwise.

---

## RD-4 — Interview consolidation: one door (RD-2's twin)

**Status: DONE (commit 8d00b57, 2026-07-20).** Bonus: the disposition gate
was also silently suppressing the Remove-from-Batch offer for awaiting-
resubmission cases (their screening is never finalized post-RD-1) — the
button was dead in the UI; now offered on the case status alone.

**Problem.** The interview module has the exact pre-RD-2 exam bug. For a pooled
vacancy, `InterviewSessionView.post` accepts schedule saves/finalizes with no
batch guard (permission check only) while `VacancyBatchInterviewView` also
saves a pool-wide schedule; for COS the per-case tab renders
`SimpleInterviewRatingForm` while the console writes the same single score
inline. Last-write-wins, and each door sends its own notifications (duplicate /
contradictory notices to applicants and panel). A per-case finalize can break
the console's pool lock-step.

**Design (mirror RD-2):**
- Pooled vacancies: per-case interview tab becomes a read-only summary with a
  console link; `InterviewSessionView` / rating / fallback POSTs refused
  server-side (PT-03) with redirect to `vacancy-batch-interview`.
- EXCEPTION: Plantilla per-rater competency sheets stay per-case (they are each
  rater's own sheet — the console cannot host N raters' parallel entries), but
  session/schedule/finalize are console-only.
- Continuous-COS keeps the full per-case editor (same rule as exam).
- Fold in: delete the vestigial screening "disposition" block
  (`screening_requires_disposition_for_current_stage` + the action card's
  screening-section return/reject) — RD-1's auto-close made those states
  unreachable except by legacy records; dead branches breed csField-class bugs.

**Acceptance:** pooled case's interview tab accepts no writes (forced POST
refused + redirected); console records schedule/scores/finalize; continuous-COS
per-case editor fully functional; interview + batch-interview test classes
green after rework.

---

## RD-5 — One-door completeness & blinding consistency (small-fix batch)

**Status: DONE (commit c6b1a54, 2026-07-20).** Includes allowing COS entries
into VacancyAssessmentWeightsView (it hard-blocked them).

1. **Console guard for continuous vacancies**: `VacancyBatchExamView` (and the
   RD-4 interview console) must refuse vacancies where
   `vacancy_uses_batch_exam(entry)` is False — currently the console is open
   for continuous-COS via URL, reopening the two-door bug in reverse.
2. **HRMPSB queue identifier**: the queue shows sequential `CASE-` numbers to
   HRMPSB members; REV-B's case header deliberately hides them from evaluators
   ("weakly leaks submission order") and shows vacancy code + candidate code.
   Make the queue row match the case-header rule.
3. **COS weights card**: the exam console's weights card + "Edit weights" link
   are `{% if is_plantilla %}` but post-RD-2 COS overalls are weight-computed
   too, and the score note references "the weights above". Render the card for
   both branches.

---

## RD-6 — Returned-application re-review integrity

**Status: RD-6a DONE (commit f8262af, 2026-07-20); RD-6b DEFERRED (see the
split note below).**

**Problem.** A generic return (no flagged documents — the action-card path
after exam) hands the whole application back; the applicant edits documents /
qualification text and resubmits. `submit_application`'s not-created branch
reactivates the case back to the screening stage — but does NOT invalidate the
stage's already-finalized screening (or exam) records. The reviewer sees a
finalized screening over the OLD documents and the endorse gate passes
immediately: edited evidence can sail through with nobody re-verifying it.
(The flagged-resubmission path is fine — it reopens exactly the flagged rows.)

**Design sketch (to lock before building):** on returned-application resubmit,
reopen the re-entry stage's screening record (un-finalize, derive completeness
from a fresh review pass) or version a new record, and surface "resubmitted
after return — re-verify" in the screening UI. Must respect locks/audit (no
silent edits of locked records — reopen must be recorded).

**AMENDED (round-6 audit, 2026-07-20) — the applicant half is also broken:
the generic-return journey has NO navigable applicant path.** The returned
email carries only the status-page link; the status page's generic-return
branch has no action button — its copy says "check your email for
instructions"; the email says "check your status page". Circular. The OTP
email's continue-link is deliberately DRAFT-only, and nothing else routes a
RETURNED applicant to the edit flow.

**SPLIT (implementation, 2026-07-20): RD-6a DONE (commit f8262af)** — the
integrity half: resubmit reopens the stage's finalized screening (rows reset
to Not Reviewed, completeness re-derived, reopened_for_resubmission_at
stamped, SCREENING_REOPENED audited, endorse blocked until re-finalized).

**RD-6b RESOLVED as Option A — DONE (commit 7be2df7, 2026-07-20).** After
weighing three options with the team (A: remove the generic return; B: build
a full applicant edit-and-resubmit flow; C: fold returns into the flagged
path), chose A: DELETE the generic whole-application return. Rationale: it
never had a working applicant path (the Track Application resubmission page
only exposes flagged documents), was barely reachable (auto-advance skips the
post-exam actions window), contradicted RD-1's doctrine (a return is a
document-resubmission request; unsuitable = Reject), and nobody hit the dead
path through demos/testing/defense. Not offered in get_available_actions;
refused server-side in process_workflow_action (flagged branch kept); dead
_transition_target mappings removed. INTERACTION: with the generic return
gone, no UI path sets a RETURNED_TO_APPLICANT case status, so RD-6a's screening
reopen (in submit_application's resubmit branch) becomes a DEFENSIVE net,
deliberately kept. The status-card deadline (old item d) shipped with RD-9.

---

## RD-7 — Vacancy & case lifecycle guards

**Status: DONE (commit dbeb863, 2026-07-20).** Item 1 BLOCKS (not
auto-reopens) per the implementation plan; item 2 exempts the winner's case.

1. **Extending a manually-Closed vacancy.** `is_open_for_intake` requires
   status == ACTIVE. A posting manually set to Closed (allowed once its closing
   date passes) can still be deadline-extended in the edit form: the new date
   saves and the public "Deadline extended" notice logic keys off the date
   change — but intake stays shut because the status is still Closed.
   Extended-but-closed is a silent contradiction. Either auto-reopen (set
   ACTIVE) when an extension moves the closing date into the future, or block
   the edit with a message telling the handler to reopen first. Decide, then
   enforce in one place (service level).
2. **Reopening a case in a decided vacancy.** `reopen_recruitment_case` guards
   role (HRM Chief), locked-state, and stage mapping — but never checks the
   VACANCY's resolution state. A non-selected sibling closed by pool
   resolution (already emailed "not selected") can be reopened into an active
   review stage while the vacancy's final selection stands and the winner sits
   in completion — a zombie case that pool queries (which filter on ACTIVE)
   would re-include. Add a guard: reopening a case whose vacancy has a
   recorded final selection/decision requires that selection to be explicitly
   voided first (or is simply refused with that explanation).

---

## RD-8 — Completion checklist & deadline: one source of truth

**Status: DONE (commit fac0eb3, 2026-07-20).** The emailed list = PENDING
rows only; the client-confirmed two-week default moved to the tracking
form's deadline field.

**Problem (double-entry, RD-1's original disease).** The completion stage keeps
the same facts in disconnected copies:

1. **The checklist exists twice.** `CompletionRequirement` rows (structured,
   per-row status, drive the closure gates) vs the requirement-checklist
   EMAIL's `checklist_items` — a free-text textarea the handler re-types
   (`RequirementChecklistNotificationForm`). They can silently disagree: the
   applicant chases the emailed list while closure blocks on rows the email
   never mentioned.
2. **The deadline exists three times, unlinked.** `CompletionRecord.deadline`
   (record form, auto-default +14d), the checklist email's own deadline
   (defaults today+14 AT SEND TIME — record set Monday, email sent Thursday →
   applicant told a date 3 days later than the office tracks), and the generic
   reminder form's third free-typed deadline. Nothing reconciles them.
3. **No deadline is enforced.** The client-confirmed 2-week rule is stored and
   displayed as an input — no overdue indicator on the completion tab or
   queue, no automatic reminder, and no flow at all for a missed deadline.

**Design:**
- The checklist email builder reads the record's requirement rows and the
  record's deadline — the send dialog reduces to an additional-message box
  plus a preview. Guard: requirements must be recorded before the checklist
  can be sent (sequencing, server-side).
- Reminder emails default their deadline from the record.
- Overdue surfacing: completion tab + queue row show "deadline passed" once
  `CompletionRecord.deadline < today` with pending requirements.
- CLIENT QUESTION (blocking the last piece): what happens when the selectee
  misses the deadline — selection lapses to next-ranked, extension, or manual
  discretion? Build nothing for the consequence until answered; the overdue
  badge is safe regardless.

**Round-4 CLEARED (completion):** closure readiness is derived from rows (not
asked); closure gates are server-side with specific blockers and lock the
stage; COS non-selected pool resolution (WF-10) and the Plantilla sibling
notifications both exist; requirement rows carry per-row status including
Not Applicable; announcement fields correctly Plantilla-only.

---

## RD-9 — Notification delivery: visibility and recovery

**Status: DONE (commit fff7457, 2026-07-20).** Includes RD-6's deferred
item d (the resubmission deadline on the applicant status card) and
migrations 0064 + 0065 (InterviewSession.applicant_notified_at + backfill).

**Problem (fire-and-forget).** Applicant-facing email is load-bearing — the
exam invitation decides whether someone shows up, and under RD-2 an absent
examinee is a terminal outcome — but delivery has no recovery loop:

1. **Failures are invisible where work happens.** `_mark_notification_failed`
   records FAILED + an audit row, then stops. `NotificationLog` renders ONLY
   as a Django-admin inline; no workflow surface (case page, queue, batch)
   shows delivery state. The office believes the applicant was told; the
   applicant never was.
2. **No retry and no resend.** Nothing re-queues a FAILED send, and there is
   no resend control. Re-saving a schedule doesn't re-notify (notification
   fires only on material change — correct anti-spam rule, but it means a
   failed invite cannot be re-triggered without fake-editing the schedule).
3. **Resubmission-deadline lifecycle is half-built.** Late resubmission is
   HARD-blocked (`resubmission_is_overdue`) but overdue is never surfaced to
   the handler (the SLA badge is days-in-stage only) and the applicant gets no
   pre-deadline reminder (manual reminder tool only). The handler discovers
   the lapse by noticing the batch is still on hold.
4. Minor asymmetry: in-app kinds cover SCREENING_FINALIZED and
   INTERVIEW_SCHEDULED/FINALIZED but there is no EXAM_FINALIZED — batch exam
   finalize advances the pool silently in-app.

**Design:**
- Per-case "Notifications sent" panel (sending roles only): each queued email
  with status + timestamp + a Resend action (server-checked, re-queues the
  same notification type; audit-logged). Failed sends also raise an in-app
  bell item to the current handler.
- Overdue-resubmission badge on queue/batch rows once the deadline lapses,
  next to the existing remove-applicant affordance.
- Add EXAM_FINALIZED in-app kind at batch finalize.
- Pre-deadline applicant reminder stays MANUAL (tool exists); automating it is
  an office-policy question, not a code default.

5. **(Round-6 addition) Status page shows un-announced interviews.** The
   upcoming-schedule builder requires `applicant_notified_at` for EXAM
   schedules but shows ANY future interview session — a drafted, never-
   announced interview leaks to the applicant's status page. Add the same
   notified-only filter for interviews (parity with the exam rule: the page
   shows what the office actually communicated).

**Round-5 CLEARED:** every reject path (all stages) funnels to the
non-selected email incl. the screening-cut variant; exam/interview reschedules
DO re-notify on material change and respect the notice-delivery channel; COS
pool-close does not double-email (process_workflow_action's email branch is
bypassed — one email per sibling) and the Plantilla mirror exists; REV-D
failure-audit intent intact.

**Round-7 CLEARED (decision chain — the strongest module in the system):**
the CAR return-for-reassessment loop is built the way RD-6's fix should be:
the returned report is flagged and EXCLUDED from
`get_latest_finalized_comparative_assessment_report` (the AA cannot act on a
returned CAR), every pool case routes back to HRMPSB with full audit +
routing rows, deliberation records are un-finalized for genuine
re-deliberation, and the next CAR takes version+1 under a unique constraint.
Final selection is server-side constrained to items of the current finalized
non-returned report, with one-selection-per-vacancy and role guards (PT-03
compliant). Cite this loop as the in-repo precedent when building RD-6.

**Round-6 CLEARED (applicant status surface):** Absent/Waived finalized exams
terminally close the case with an adjusted, immediate applicant notification
(Judgment item A implemented properly — no sealed-fate case reads "Under
Review" indefinitely, and a closed case cannot be endorsed); the help page's
OTP-validity claim is template-driven from live settings (no hardcoded lie
despite the .env 10-vs-3 gotcha); the applicant status-label map covers every
surfaced status with a safe "Received" default and honest tones; the
flagged-resubmission journey has a direct email-independent action button on
the status page; the exam-schedule display honestly shows only what the
applicant was notified about.

---

## Client-questions list (no code until answered)

- COS exam = two components (RD-2 assumption, standing).
- ETE split 40/30/30 vs 30/30/40 (workflow-governance, standing).
- ETE scored at CAR time, not screening (RD-3 assumption).
- **COS interview = single end-user score** (spec §6.2 backs it, but post-RD-2
  it is asymmetric with the unified exam model — confirm once with the client).
- **Exam validity dates**: `valid_from/valid_until` are collected in two UIs,
  validated, and never read again — no display downstream, no enforcement, no
  cross-posting reuse (the office's "exam validity by parenthetical" rule).
  Ask what the office needs; then either surface + enforce, or stop collecting.
- Exam form's COS exam-type / administered-by dropdowns may be derivable from
  the stage (Secretariat = HRMS, Chief = end-user) — if policy-fixed, they are
  redundant questions (RD-1 class, minor).

## Audit trail — checked and CLEARED (2026-07-20)

- Flagged-document resubmission correctly reopens only the flagged rows;
  already-Complied rows stay accepted (by design, documented in service).
- Queue search matches applicant names, so the name-censorship change keeps
  cases findable-by-name without browsable names (searchable-not-browsable).
- No stale pre-REV-C terminology ("Meets", "Request Resubmission", document
  "Absent") in applicant-facing or email templates.
- Generic return's fork (flagged → resubmission request; unflagged → whole-
  application return) is coherent; its record-staleness side effect is RD-6.

---

## Sequencing & notes

- RD-1 then RD-2 (both touch the case workspace; screening passes every case).
- Neither item contradicts any panel-compliance claim (Chavez rows or Ronel's
  terminology) — verified during design.
- Client-pending assumptions to revisit: COS exam = two-component (RD-2);
  ETE splits 40/30/30 vs 30/30/40 (workflow-governance, unrelated but open);
  ETE scored at CAR time (RD-3).
