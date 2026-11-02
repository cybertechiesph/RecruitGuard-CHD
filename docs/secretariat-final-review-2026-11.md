# Secretariat final review (November 2026)

Scope: the Secretariat's pages, actions and workflows on both branches, including the handoffs to
and from the HRM Chief, HRMPSB and Appointing Authority, reviewed from eleven senior
perspectives. This is the third pass: it follows the walkthrough
([secretariat-walkthrough-2026-11.md](secretariat-walkthrough-2026-11.md)), the six essential
fixes (`f74ad22`) and the second round (`03a8524`). No code was changed for this review.

## Status: Round A done (F1–F6)

| | Fix | Verified |
|---|---|---|
| **F1** | HRMPSB members are refused applicant documents, exam evidence, letters of intent and withdrawal letters (audited as `applicant_file_restricted_for_hrmpsb`). The Secretariat and Appointing Authority still open them. | Test + live probe (403 for the panel, 200 for the others). |
| **F2** | Batch exam scores are checked before anything is saved, drafts included: a number from 0 to 100 (blank allowed for a draft). "abc", 101, −1 and NaN each get a plain message naming the case. | Tests + live probe on the demo DB (nothing stored, no 500). |
| **F3** | The batch exam date is the Manila day of the sitting. Migration `0091` corrected existing records that carried the UTC day (six on the demo DB, Nov 1 → Nov 2). | Test with a 07:00 sitting + migration run on the demo DB. |
| **F4** | Thirteen workflow steps lock their case (or vacancy) row before reading state, so a simultaneous second request waits and gets the ordinary "already finalized" message. The CAR save now runs in one transaction (it didn't before). A middleware safety net turns any remaining conflict on an internal form into a message and a fresh page. | Tests + a real two-user race on Postgres: one "Screening finalized and locked", one "Finalized screening records cannot be edited", one record, no error page. |
| **F5** | "Send again" refuses a second copy of the same email within a minute (the row is locked, so two simultaneous clicks are covered), and the button disables after the first click. | Test + live probe (two clicks → one copy). |
| **F6** | **Corrected finding.** The count in F6 below was wrong: the batch already sends one digest per panel member per vacancy (the six notices on one case were the six members' digests). Reading the email exposed the real problem: it listed each candidate's **name and case number** next to the code, to every panel member. The panel's interview emails (batch digest, single-case notice, HTML version) now show candidate codes only; the HTML digest also gained its missing schedule and location. | Test that renders subject, text and HTML for every panel recipient. Not sent live (no Plantilla batch at the interview step in the demo data). |

Still open: F7–F15 and the P3 list. The "every HRMPSB account receives every notice" part of F6 remains until the office decides on a panel per vacancy (section 7).

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | A fresh synthetic Plantilla Level 1 vacancy (VAC-PLT-2026-901, 4 applicants) walked end to end: screening (two users, one return-for-resubmission and resubmission), close intake, batch exam (schedule, scores, finalize), rating sheet, batch interview (schedule, three panel ratings online, one on paper), CAR (ETE, draft, finalize), Appointing Authority selection, completion, close. A fresh COS Level 1 pooling vacancy (VAC-COS-2026-901, 3 applicants): screening, batch exam, HRM Chief re-check (screening, exam, interview, selection), completion back with the Secretariat. A Plantilla Level 2 vacancy (VAC-PLT-2026-902) used only to probe isolation. Past Records, Recruitment Entries, Schedule, Notifications, export. | Browser pane (dark theme, 1366×900) for every page the Secretariat uses, driving the real forms and confirmation dialogs. Form posts that didn't need a visual check went through Django's test client against the same views and the live Postgres demo database. Synthetic staff accounts `walk_sec`, `walk_sec2`, `walk_chief`, `walk_psb`, `ux_hrmpsb`, `ux_hrmpsb2`, `walk_aa` (no passwords; sessions minted server-side), one host per role. |
| **Probed (rolled back)** | ~90 invalid, out-of-order, duplicate, cross-role and cross-case requests, each inside a transaction that was rolled back so the demo data stayed clean. Two genuine concurrency runs (threads against Postgres). | `walk_harness.py` (scratch). Results quoted below are the server's own responses. |
| **Code** | Permission functions, transition code, validators, constraints, notification delivery, export builder, settings, middleware. | Reading `recruitment/services.py`, `views.py`, `models.py`, `forms.py`, `notification_services.py`, `permissions.py`, `config/settings.py`, `config/middleware.py`. |
| **Suite** | 766 tests, all passing at `03a8524`; `makemigrations --check` clean. | Run before this review. |

**Not verified** (see section 7): screenshots could not be captured for most of the session (the
browser pane was not drawing), so visual layout, responsiveness and dark/light contrast were not
re-checked this round; the HRMPSB document-download gap was confirmed live on a closed case and
inferred by code for the live HRMPSB stage; the CAR finalize race was not run; email behaviour
under a real SMTP failure; the printed-notice path; System Admin surfaces.

## 1. Verdict

The Secretariat's workflow is **correct and enforced on the server**. Every role boundary I
pushed held (Level 2 cases are invisible to the Secretariat, Level 1 cases to the Chief, the
panel can't schedule, the Secretariat can't select, closed cases can't be re-finalized), every
finalize is guarded (documents, outcome basis, exam held, interview held, quorum, deliberation
date, recommendation), every action I took is in the audit trail, and the export bundle carries
checksums and a verification report. The second round's changes work as designed in the real
flow.

What remains is a short list of **integrity and privacy holes** that the happy path never
touches, a handful of **workflow clarity** problems, and a **maintainability** problem in the
shape of the code. None of them calls for a rebuild. Section 5 recommends focused fixes plus a
targeted restructuring of the service layer.

## 2. What is solid (keep)

- **Authorization is server-side and leak-free.** 403 on actions, 404 on views that would reveal
  another office's case (Chief/HRMPSB on a Level 1 case, Secretariat on Level 2, Secretariat on
  COS cases once they reach the Chief). Evidence ids can't be used across cases (404).
- **Finalize guards.** Screening: outcome required, Not Qualified needs its basis, Qualified
  needs every required document Complied, a returned document needs an instruction. Exam:
  schedule first, results only after the sitting, both scores 0–100 per candidate, absent rows
  can't carry scores, validity window ordered. Interview: schedule first, after the sitting,
  quorum of distinct raters, scale enforced, justification outside 75–98, one rating per member
  (a paper sheet for a member who rated online is refused). CAR: preparer roles only, ETE basis
  per component, 0–100, numeric, no cross-vacancy ids, quorum 3–5, deliberation date not in the
  future nor before the last interview, recommendation required or "none" with notes. Selection:
  Appointing Authority only, deep selection needs a justification, notes required. Completion:
  Secretariat only, selected case only, deadline not in the past, every requirement resolved
  before close, deadline changes need a reason.
- **Session and login hygiene.** 30-minute inactivity timeout, lockout after repeated failures
  with audit rows, CSRF enforced (403 without token), HSTS/nosniff/DENY/referrer headers in
  production settings, uploads type-checked and content-sniffed (an `.exe` and a fake PDF are
  refused with plain messages).
- **Audit trail.** Each walked case carries the full chain (`case_created` → `routed` →
  `screening_finalized` → `exam_scheduled` → `exam_finalized` → `interview_scheduled` →
  `interview_rating_recorded` / `interview_fallback_uploaded` → `interview_finalized` →
  `car_generated` → `car_finalized` → `decision_recorded` → `completion_recorded` →
  `case_closed`), plus `evidence_downloaded`, `export_generated` and `export_denied`.
- **Export.** One zip per case: evidence by stage, inventory (CSV + PDF), audit and routing logs,
  case manifest, submission packet, SHA-256 checksums and a verification report.
- **The second-round changes in use.** Past Records, the per-branch progress bar (incl. "Withdrawn
  at Screening", "Done · All steps complete"), ETE before the first draft, official CAR version
  (one draft, one update, finalized → "Version 1"), 14-day completion target and deadline from
  the selection date (Nov 16 for a case selected Nov 2), Close intake on lapsed vacancies, one-step
  close with the notes in the dialog, sidebar highlight by origin.

## 3. Findings

Each finding names the perspective it comes from, what was observed, where it lives, and the
fix. **P1** = fix before the next deployment. **P2** = fix in the next round. **P3** = polish.

### P1 — Essential

**F1. HRMPSB members can download applicant documents that carry the applicant's name.**
*(Application Security, Records)* — Live: `walk_psb` fetched
`/internal/applications/540/evidence/<id>/download/` for a not-selected (closed) candidate and got
`200` with the PDS. Code: `decrypt_evidence_bytes` ([services.py:9218](../recruitment/services.py))
refuses an HRMPSB member only the CAR file; any other artifact passes if
`get_evidence_context_application_for_user` finds a context, and that mirrors
`user_can_view_application`, which grants HRMPSB members the cases they rated (open and closed).
The HRMPSB case page shows document *statuses* without links (good), but the URL is guessable
(ids are sequential) and the applicant documents are the one place the blinded name lives.
**Fix:** in `decrypt_evidence_bytes`, refuse `artifact_type == "applicant_document"` for
`HRMPSB_MEMBER` with an audited denial (same pattern as the CAR refusal); add a test that loads
every applicant document of a case at the HRMPSB stage as a panel member. *Verified live on a
closed case; the HRMPSB-stage path uses the same function (code).*

**F2. The batch exam accepts any number as a draft score, and a non-numeric score crashes the page.**
*(QA, Backend, Database)* — Live/probe: `save_scores` with `101` and `-1` returned "Exam scores
saved." and the values persisted (`101.00`, `-1.00` on all four records); `abc` raised
`decimal.InvalidOperation` → HTTP 500. Finalize does validate ("Exam scores must be between 0 and
100"), so a bad draft can't be locked, but the page itself is unusable until the row is fixed,
and the reviewer sees no message. Code: [views.py:2214](../recruitment/views.py) passes the raw
strings; `_optional_decimal` ([services.py:4100](../recruitment/services.py)) converts without a
guard; `ExamRecord.general_score/technical_score` have no validators or `CheckConstraint`.
**Fix:** validate on save (numeric, 0–100) and return the same message the finalize uses; add
`CheckConstraint`s on the score columns (and on `rating_score`); same check for the per-case
exam form if it shares the helper.

**F3. The exam date is recorded as the UTC calendar day of the sitting.**
*(Records, Backend)* — Live: the sitting was scheduled 2026-11-02 05:51 (Asia/Manila); the
finalized record shows `exam_date = 2026-11-01` on the case page, in the prior-stage summary the
panel sees, in the CAR source data and in the export. Code:
[services.py:6407](../recruitment/services.py) `schedule.scheduled_for.date()` on an aware UTC
datetime. Every sitting before 08:00 local is dated the previous day — an official record error.
**Fix:** `timezone.localdate(schedule.scheduled_for)`; a data fix for existing records whose
schedule time is before 08:00; a test with a 07:00 sitting.

**F4. Two users finalizing the same step at the same moment: the second gets a server error.**
*(Backend, Database)* — Live (threads on Postgres): `walk_sec` and `walk_sec2` finalized
screening on one case together; one succeeded, the other raised `IntegrityError:
unique_screening_record_per_application_stage` (HTTP 500). The constraint protected the data
(one record), but the user sees an error page instead of "already finalized by Sara". The service
layer has six `select_for_update` calls, none on `RecruitmentCase` for the workflow transitions
(`save_screening_review`, `finalize_vacancy_interview_batch`,
`generate_comparative_assessment_report`, `record_final_selection`, `close_recruitment_case`).
With one Secretariat today this is rare; with a second user or a double-click it is not.
**Fix:** lock the case row (`select_for_update()`) at the start of each transition's atomic
block and re-check state after the lock; catch `IntegrityError` at the view boundary and turn
it into the "already done" message. *CAR finalize race not run (code shows the same pattern).*

### P2 — Important

**F5. "Send again" sends twice on a double click.** *(Integration)* — Probe: two resend posts for
one acknowledgment queued **two** new notification logs. There is no idempotency on resend or on
the batch sends. **Fix:** disable the button on submit (client), and refuse a resend of the same
log queued within the last minute (server).

**F6. Panel notices fan out to every HRMPSB account for every candidate.** *(Corrected in the status table above: the count was a misreading; the real defect was names in the panel's email, now fixed.)* *(Integration, BA)* —
Scheduling the 4-candidate interview produced six `interview_session_scheduled` notices per
candidate (24 in all) because there is no panel assignment: every HRMPSB user gets every
candidate's notice. **Fix:** one notice per vacancy per member; longer term, a panel assignment
per vacancy (which HRMPSB members sit for this position) so notices, quorum and blinding all
derive from it.

**F7. The panel's Interview card says "Not recorded" under a block that shows the results.**
*(UI/UX, QA)* — Live as `walk_psb` on case 539: "Earlier review — Qualified, Exam 89.20" followed
by "SCREENING RESULT Not recorded / EXAM STATUS Not recorded" in the Interview / Rating card
([interview_previous_step.html](../templates/internal_includes/interview_previous_step.html)).
The card reads the current stage's records, not the finalized earlier stage. **Fix:** read the
prior-stage records (the same source the "Earlier review" block uses) or drop the duplicate.

**F8. The rating error hides the real problem.** *(UI/UX)* — A rating that needs a justification
(below 75 or above 98) is refused with "Complete the required interview rating fields before
saving." The field that is missing is `justification`, which isn't named. The paper-sheet path
says it plainly ("Provide a justification when the interview rating is below 75 or above 98").
**Fix:** use the field's own message in the online form too.

**F9. "Send to HRM Chief" on a Level 1 case can split a batch.** *(BA / Workflow)* — Probe: the
Secretariat handed a Level 1 case at the exam step to the Chief (allowed, logged, needs remarks).
Afterwards both the Secretariat's and the Chief's batch pages listed the case, and the batch exam
was blocked for both. The handoff is the "controlled override" the rules allow, but nothing
explains when to use it, it works per case while the exam and interview work per batch, and the
button sits next to Export on every Level 1 case. **Fix:** either hide the handoff while a case
is part of a pooled batch (hand the whole batch instead), or keep it per case and have the batch
consoles exclude handed-off cases with a note. Either way, name the use ("escalate to the Chief")
and show when it was last used in the case history.

**F10. Screening and the CAR are judged against "No qualification standards are on file".**
*(BA, Data)* — Both synthetic vacancies rendered that warning in the Decide step and in the ETE
grid. The QS lives on `PositionReference` (`qs_education`, `qs_training`, `qs_experience`,
[models.py:~300](../recruitment/models.py)) and is optional; a Plantilla vacancy can be published,
screened and ranked with none on record, so 40% of the CAR rests on memory. **Fix:** require the
three QS fields before a Plantilla entry becomes Active (form check, not model), show them on the
entry form, and keep the warning for legacy references.

**F11. After finalizing a screening, the Secretariat is left on the case with no next step.**
*(UI/UX, Workflow)* — Live: after "Finish and lock" on CASE-0024 the page stayed on the case
showing "Exam batch on hold: 3 applicants still need a screening decision". The "Next in this
batch" prompt only appears when the user can no longer act on the case
([views.py:1183-1197](../recruitment/views.py)). **Fix:** when the user's own next action is on
another case of the same batch, show "Next: CASE-0025 — screening" with one button, and make the
hold message neutral (it's progress, not a problem).

**F12. Every draft CAR stores another PDF, and the export ships them as evidence.** *(Records)* —
Live: one Prepare + one Update = two `comparative_assessment_report` evidence rows; the export of
the earlier demo case carries `car-v1.pdf`, `car-v2.pdf`, `car-v3.pdf` beside the final. The
screens now show "Version 1", but the archive still says v3 and treats drafts as evidence.
**Fix:** keep one draft artifact (overwrite until finalized) or mark drafts in the inventory and
file names ("draft-2"), and list only finalized CARs in the bundle's main set.

**F13. Score columns have no database range checks; the case table has no workflow indexes.**
*(Database)* — One `CheckConstraint` in the whole schema; none on `general_score`,
`technical_score`, `rating_score` (F2 shows why it matters). `RecruitmentCase.current_stage`,
`case_status`, `stage_entered_at` and `current_handler_role` drive every queue, batch and
overdue query with no index. Harmless at today's volume (hundreds of rows), not at a few years
of regional recruitment. **Fix:** `CheckConstraint`s on the four score columns; a composite index
on `(current_stage, case_status)` and one on `stage_entered_at`.

**F14. No operations runbook: logging is console-only, there is no backup/restore procedure, and
no error alerting.** *(DevOps)* — `LOGGING` has a console handler only; `docs/` has no deployment,
backup or recovery document (the turnover package warns that the evidence vault key must never
rotate, which makes key escrow part of any backup); the demo droplet reseeds on every deploy. A
500 (F2) is invisible unless someone reads the service's journal. **Fix:** a one-page runbook
(nightly `pg_dump` + media/evidence copy + key escrow + a tested restore), file logging with
rotation, and `ADMINS` mail or Sentry for 500s.

**F15. The service layer is one 13,700-line module.** *(Architecture, Maintainability)* —
`services.py` holds 404 functions; `views.py` 61 classes with `ApplicationDetailView` at ~620
lines; `tests.py` is 22,800 lines. The rules are right (this review found no rule bug in the
happy path), but finding them takes a search, and F3/F4 are the kind of defect that hides in a
module this size. See section 5.

### P3 — Polish

- After the COS exam finalize the Secretariat lands on the Batches list; after the Plantilla
  exam finalize on the interview page. Same action, different destinations.
- Batch and queue rows still format dates as "Nov 02, 2026"; the interview card says
  "Nov 02, 2026 at 09:53". The case page uses "Nov 2, 2026, 9:53 AM".
- "1 applicant in this vacancy still need a screening decision"
  ([services.py:3658](../recruitment/services.py)).
- Yellow "Screening batch is on hold" and "Exam batch on hold" banners on first load, before
  anyone has done anything.
- Staff notices for completion still say "assigned to you for Completion Tracking".
- The header says "Assigned to HRMPSB Member" while the Secretariat prepares the CAR.
- Case numbers: `CASE-2026-0024` in lists and titles, "Case No. 2026-0024" in headers.
- The rating sheet is reached through one case's URL (`/applications/539/interview/rating-sheet/`).

## 4. The eleven perspectives

**QA.** Happy paths pass on both branches. Edge and sad paths: 60+ invalid or out-of-order
posts were refused with a message, two were not (F2: draft score range, non-numeric crash).
Duplicate submissions: a second finalize is refused (403) everywhere except under a true race
(F4) and on resend (F5). Recovery: the return-for-resubmission → resubmit → re-review loop works,
with the re-uploaded file marked and the old note shown; a lapsed vacancy can be closed and the
batch proceeds; a handoff can be reversed by the other office.

**UI/UX.** Navigation is now coherent for the Secretariat (Batches is the home; Past Records,
Entries, Schedule are one click). The confirm dialogs say who is affected. Gaps: F7, F8, F11, the
first-load warnings and the leftover date formats. Accessibility was not re-measured this round
(no screenshots); the earlier rounds' contrast and label work still stands in the markup.

**Business Analysis / Workflow.** Routing matches the rules (Level 1 → Secretariat, Level 2 →
Chief, COS re-check at the Chief, completion back with the Secretariat on both branches). The
controlled override exists and is logged (F9), but its place in a batch-based flow is unclear.
Required controls that cost time and are worth it: closing the intake before the batch exam,
quorum, deliberation date, deep-selection justification, deadline-change reason. Avoidable
friction: F11, the QS gap (F10), the hold banners.

**Frontend.** The templates work without JavaScript for every post (progressive enhancement
holds); the confirm dialogs are server-backed forms. Empty, error and success states exist on
every page I used. The one crash (F2) is server-side. Not verified: responsive layouts.

**Backend.** Validation is in the service layer and enforced at finalize; the draft paths are
looser (F2). Transactions wrap every transition (58 atomic functions) but without row locks
(F4). Error handling: unexpected exceptions reach Django's 500 page (custom 500 template exists).

**Database.** Relationships and unique constraints are right where it mattered (one screening
record per stage saved F4's data; one open posting per item; one rating per member). Missing:
range checks and workflow indexes (F13). Consistency between records and files: every evidence
row has a version, the current version is flagged, and the export reconciles rows to files with
checksums.

**Application Security.** Authentication: MFA-gated sessions, inactivity timeout, lockout with
audit. Authorization: enforced per object on the server; probes found one real gap (F1) and no
IDOR across cases. Sensitive data: evidence is encrypted at rest and decrypted only through the
access function; denials are audited. CSRF on.

**Integration.** Notifications are queued on commit and delivered with a status (`sent`/`failed`)
and an audit row; a failed send is logged, not retried, and the Secretariat can resend applicant
emails (staff notices and past invitations are refused). Gaps: F5 (duplicate resend), F6
(fan-out), and no retry/backoff on a transient SMTP failure (code-verified, not exercised).

**Reporting and Records.** The export is complete and verifiable. The CAR filing copy prints
from the vacancy. Gaps: F3 (wrong exam date), F12 (draft PDFs as evidence). The audit log is
complete for the Secretariat's actions and readable by the System Admin only (per the office's
decision).

**DevOps.** Settings are environment-driven with safe production defaults (HSTS, secure cookies,
SSL redirect). Gaps: F14.

**Architecture.** See section 5.

## 5. Current vs proposed flows

**Screening a batch**
```
Current:  open case → documents → decide → Finish and lock → stays on the case
          ("Exam batch on hold: 3 still need a decision") → Back to batch → next case
Proposed: … → Finish and lock → "Screening saved. Next: CASE-0025" [Open next] [Back to batch]
          hold banner becomes neutral progress: "3 of 4 screened"
```

**Batch exam scores**
```
Current:  Save scores accepts 101 / -1 / "abc" (crash) → Finalize refuses 101
Proposed: Save scores validates like Finalize → inline message per cell, nothing persisted
          + database CheckConstraint as the last line
```

**Handing a case to the Chief**
```
Current:  any Level 1 case, any pre-interview step, per case → the batch shows the case on
          both sides and the exam is blocked for both
Proposed: per batch ("Escalate this vacancy's batch to the HRM Chief") with the reason, or per
          case but removed from the batch consoles with a note; history shows the escalation
```

**Draft CAR records**
```
Current:  Prepare → PDF v1 stored; Update → PDF v2 stored; Finalize → v3; export ships all three
Proposed: Prepare/Update overwrite one "draft" artifact; Finalize stores "Version 1";
          export lists the final CAR, with drafts under a separate "working copies" folder
```

## 6. Architecture: focused fixes, restructuring, or rebuild?

**Recommendation: focused fixes now (F1–F5), then a targeted restructuring. Not a rebuild.**

Why not a rebuild: the policy surface (routing, guards, blinding, quorum, CAR formula, office
switches) has been validated by the client across three review rounds and is covered by 766
tests. A rebuild would spend the next months re-deriving rules that already hold, with a high
chance of regressing the controls this review confirmed.

Why not fixes alone: F3 and F4 are symptoms of size. A transition's rules are spread across a
13,700-line module and a 620-line view; the same date conversion is done four ways; locking was
added where someone hit a bug (login, MFA, COS rounds) and nowhere else.

What the restructuring is (incremental, behind the existing tests):

1. **Split `services.py` into a package by module**: `screening`, `exam`, `interview`, `car`,
   `selection`, `completion`, `routing`, `evidence`, `notifications`, `entries`, with
   `services/__init__.py` re-exporting the current names so views and tests don't change.
2. **One transition helper**: `transition_case(case, *, to_stage, to_status, handler, actor,
   reason)` that locks the case row, checks the from-state, writes `RecruitmentCase`,
   `RoutingHistory` and the audit row, and emits the notice. Every finalize calls it (fixes F4
   once).
3. **A per-branch workflow map as data** (already half there in the progress bar): the ordered
   steps per branch and level, with the role that owns each. The progress bar, the guards, the
   "next" prompts and the handoff rules read the same map (removes the drift that produced
   P2-7 and F9).
4. **Time handling in one place**: a `localdate_of(aware_dt)` helper and a lint test that fails
   on `.date()` applied to an aware datetime in `recruitment/` (fixes F3 permanently).
5. **Tests split by module** to match.

Order: F1, F2, F3 (one deployment); F4 with step 2; then steps 1, 3, 4, 5 over the next rounds,
each landing with the suite green.

## 7. Essential vs optional

**Essential (before the next deployment):** F1, F2, F3, F4.
**Important (next round):** F5–F14, in roughly that order; F9 and F10 need an office decision
first (handoff scope; whether QS is mandatory at publication).
**Optional:** P3 items; F15 as the restructuring plan above.

**Questions for the office — answered 2026-11-02**
- Handoff to the Chief: **the whole batch**, with a reason; a single-case send only for cases
  that aren't in a batch (continuous-intake COS). (F9)
- Qualification Standards: **required** before a Plantilla vacancy goes Active; optional for
  COS. (F10)
- HRMPSB panel: **revised the same day** after re-reading the interviews. The board is three
  standing members (chair, vice chair, HR) plus end-user members who change with the position,
  and Dr. Victorina asked that an end-user member "see only what's for them". So: **a panel per
  vacancy** (the standing members always; the Secretariat adds the end-user members when the
  rating sheet is prepared), and visibility, notices and quorum follow that list. Every member
  has a named account; no shared "end user" login. (reopens the remaining part of F6; see the
  Chief review §8, item 4a)

## 8. What remains unverified

- HRMPSB document download at the live HRMPSB stage (F1 was confirmed on a closed case; the
  stage path is the same function).
- The CAR finalize race (not run; same unlocked pattern as F4).
- Email delivery under a real SMTP outage (code: status `failed` + audit, no retry).
- The printed-notice delivery option, the mobile layouts, light-theme contrast, and the System
  Admin's surfaces (screenshots were unavailable for most of this session).
- Performance beyond the demo volume (no load test; F13 is a reading of the schema).
