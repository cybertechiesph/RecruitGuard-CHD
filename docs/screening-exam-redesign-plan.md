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

## Sequencing & notes

- RD-1 then RD-2 (both touch the case workspace; screening passes every case).
- Neither item contradicts any panel-compliance claim (Chavez rows or Ronel's
  terminology) — verified during design.
- Client-pending assumptions to revisit: COS exam = two-component (RD-2);
  ETE splits 40/30/30 vs 30/30/40 (workflow-governance, unrelated but open).
