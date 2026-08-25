# System-Logic Audit 2 — Findings (2026-08-22)

Second-pass business-logic audit of the DOH–CHD CALABARZON Recruitment Portal
(study title: RecruitGuard-CHD). Scope: whether the process model is right —
does each stage decide the right thing, with the right information on screen,
in the right order, and can every real office outcome be expressed. Not UI/UX,
not security. Conducted by Claude Fable 5 with six parallel evidence passes
(vacancy + intake; screening + routing; exam + interview + attendance; CAR,
deliberation, AA, rescind; COS decision + completion/closure; cross-cutting
state machine, notifications, audit, queues). Every Critical finding and every
pass's load-bearing Major claims were re-verified by the auditor against the
cited lines and, where a probe was run, against the live demo database; all
probes ran inside rolled-back transactions and the database was confirmed
unchanged afterwards. Findings whose evidence was reviewed but not
independently re-executed are marked "evidence reviewed".

Audited tree: local `main` at `519eaa5` (REV-A…I batch complete). This document
goes to an implementer with no access to the audit conversation; every item
stands alone.

## 1. Context and audit basis

**Two paths.** Plantilla: intake → screening (RD-1, two-step) → exam (one-door
batch, pool-final gate SL-09) → interview (HRMPSB competency ratings, 3–5
raters; template locks on first rating, N2) → CAR (ETE 40 / Exam 20 /
Interview 40; candidate codes only) → off-system deliberation, Secretariat
encodes → Appointing Authority selection or return-for-reassessment →
completion → closure. COS: intake → Secretariat or HRM Chief screening/exam
(Level 1 / Level 2) → HRM Chief interview (single score) → per-vacancy
selection round and pick (SL-02; the pick is the decision, SL-01) → completion
→ closure. No HRMPSB, computed CAR, or Appointing Authority on COS.

**Fixed rules this audit does not reopen.** CAR default weights 40/20/40
(per-vacancy editable until the first rating, then locked; no unlock).
HRMPSB quorum 3–5. The board deliberates off-system; the Secretariat encodes
the outcome. The exam is a scored component, never pass/fail. Completion
requirements are handler-supplied rows with an auto-defaulted two-week
deadline. Candidate codes C-NN are the only identity on CAR/deliberation
surfaces; the live interview sheet shows code + name. No user-facing surface
says "RecruitGuard".

**Settled work this audit does not re-propose** (all implemented on `main`):
docs/screening-exam-redesign-plan.md RD-1…RD-9;
docs/system-logic-audit-findings.md SL-00…SL-15, N1, N2 (its §1A Resolution
Log is binding); docs/defense-revisions-plan.md REV-A…REV-I (see its
"Implementation status (2026-08-22)"). Where a shipped item did not fully
close its gap, it is reported here with evidence and named as such.

**How to run.** Python `./.venv/Scripts/python.exe`. Tests only:
`DJANGO_DEBUG=true SECURE_SSL_REDIRECT=false POSTGRES_DB= POSTGRES_USER=
POSTGRES_PASSWORD= POSTGRES_HOST= POSTGRES_PORT= … manage.py test
recruitment.tests.<Class> --keepdb --noinput` (blank vars → SQLite for the
test run; fine). Live server or shell: do NOT blank the POSTGRES_* vars — live
data is the Postgres database `recruitguard_chd`; `db.sqlite3` is stale and
must not be used or migrated. `seed_demo_data` FULL-WIPES applications and
postings; `seed_e2e_test_cases` is additive and prints a scenario →
case-number table. A `--noreload` server serves stale templates after edits;
restart it. Known test baseline: 14 login/MFA/captcha tests fail whenever the
local `.env` has `INTERNAL_MFA_ENABLED=False` / `CAPTCHA_ENABLED=False`; that
list is stable and unrelated to workflow code.

**Flaw taxonomy.** 1 Redundant capture · 2 Floating decision · 3 Two doors,
one outcome · 4 Unreachable action · 5 Deadlock by construction · 6 Dead end ·
7 Auto-advance without checking · 8 Inexpressible real-world outcome · 9
One-way door · 10 Orphaned logic · DC Derived contradiction.

**Severity** (by office impact). Critical — can produce a wrong hire, a wrong
exclusion, a recorded verdict the reviewer did not mean, or a vacancy/case
that can never proceed. Major — produces a wrong or contradictory record that
staff must correct outside the workflow, or a legitimate outcome the office
cannot express. Minor — redundant work, dead code, or an avoidable error; no
wrong record.

**Line references** are to the audited tree (`519eaa5`); functions are named
so they survive line drift.

---

## 2. Verdict by stage

Three-level verdict: **KEEP** (model right, nothing to change), **KEEP WITH
GAP** (model right; a specific exceptional path is missing or wrong), **CHANGE**
(the stage decides the wrong thing or cannot complete on a legitimate path).

### 2.1 Plantilla path

| Stage | Single decision | Verdict | Reasoning |
|---|---|---|---|
| Vacancy entry, publication, lifecycle | Whether and when this vacancy accepts applications, and when it stops | **CHANGE** | The edit form's `status` field is a second door that bypasses the early-close gate and the cancel-with-reason path (SL2-01); a published deadline can be shortened to today (SL2-02); Cancelled is not terminal (SL2-03); closed vacancies vanish from the list (SL2-04); an extension reopens a pool whose batch already sat (SL2-10). The lifecycle states themselves are right; their transitions are under-guarded. |
| Applicant intake and OTP submission | Whether a complete application from a verified email becomes a case | **KEEP WITH GAP** | Intake guarantees hold (checklist, required files, duplicates, OTP). Gaps: the public detail page lists the branch catalog instead of the posting's configured requirements (SL2-21); withdrawn/cancelled applicants can never re-apply (SL2-22); no path to correct an applicant's email (SL2-23). |
| Level-aware routing and office handoff | Which office owns processing | **CHANGE** | Handoff ignores a pending resubmission and silently cancels it (SL2-05); per-case handoff inside a pooled vacancy splits the batch so neither console can finalize (SL2-14); Level-2 Plantilla exam finalized under Secretariat override strands the Chief with no action (SL2-06); "Send to Secretariat" is offered where the server refuses it (SL2-38). |
| Screening | Whether the evidence complies and the applicant meets the displayed QS, or is returned / not qualified | **KEEP WITH GAP** | RD-1 is sound as a decision. Gaps: screening can be finalized Qualified while the case is awaiting resubmission, leaving it paused forever (SL2-07); the QS the verdict was judged against is neither frozen nor preserved (SL2-24); a Not-Qualified record with flagged rows reads as "returned" (SL2-39); no extension path for the resubmission deadline (SL2-25). |
| Examination | What scored outcome belongs to every member of the final pool | **KEEP WITH GAP** | One-door, Absent/Waived, and SL-09 are sound. Gaps: validity window wiped on re-save (SL2-40); a rescheduled absentee can be scored against the missed sitting's notice (SL2-17). |
| Interview | What 3–5 distinct HRMPSB members scored | **CHANGE** | A pooled Plantilla no-show has no door at all — one absentee blocks the pool's finalize (SL2-08); the absence decision is reserved to a board member rather than the support office (SL2-15); no way to void a rating (SL2-16); a pooled interview cannot span days (SL2-18). |
| CAR and preliminary ranking | Each candidate's 40/20/40 score and rank | **KEEP WITH GAP** | Computation is right. Gaps: weights can be unlocked after the CAR is finalized and the printouts recompute from the live split (SL2-19); the vault CAR PDF is named and HRMPSB can decrypt it (SL2-20). |
| HRMPSB recommendation (off-system; Secretariat encodes) | Whom the board recommends | **KEEP WITH GAP** | Model right. Gap: deliberation date/members are captured at interview finalize, then re-asked as quorum + members, and the date is never stored or printed (SL2-26); a board that recommends nobody cannot finalize (lead). |
| Appointing Authority selection / return | Whom the AA selects, or whether the CAR goes back | **CHANGE** | A withdrawal at AA freezes the vacancy: both AA actions require every CAR item to still be at AA (SL2-09); the AA cannot decline to appoint anyone (SL2-27); return-for-reassessment can only re-rate ETE (SL2-28). |
| Completion | Whether the appointee supplied the handler-defined requirements | **KEEP WITH GAP** | Readiness and closure gates are sound; N/A is a first-class waiver; unchanged historical deadline accepted (SL-13). Gap: the handler cannot record when a requirement was actually received, so "late" derives from the encoding date (SL2-29). |
| Rescind and closure/reopen | Whether a recorded decision or closed case can be corrected | **KEEP WITH GAP** | Rescind chains and reopen audit are right. Gaps: no rescind cutoff and the completion record survives (SL2-30, policy half = client); reopen ignores *why* the case closed (SL2-31); no signal to the receiving handler or to an applicant whose communicated outcome is reversed (SL2-32). |
| Notification delivery (RD-9) | Whether material events were sent and can be recovered | **KEEP WITH GAP** | Delivery state/recovery exist. Gap: failed terminal-outcome emails raise no in-app alert because the handler is blanked before the send (SL2-33); several material events have no signal at all (§5 matrix). |

### 2.2 COS path

| Stage | Single decision | Verdict | Reasoning |
|---|---|---|---|
| Vacancy entry and intake mode | Opening-based, pooling, or continuous intake | **CHANGE** | Same lifecycle defects as Plantilla (SL2-01…04). Plus: a single-batch COS vacancy can fill exactly one slot, ever (SL2-34). |
| Applicant intake | Whether a verified complete application becomes a case | **KEEP WITH GAP** | As Plantilla (SL2-21…23). |
| Level-aware routing | Which office owns processing | **CHANGE** | SL2-05, SL2-14, SL2-38 apply. Plus: the Level-1 "independent validation" pass can be performed by the Secretariat through a plain handoff (SL2-35); one absentee at Secretariat stage pins the console away from the Chief's cohort (SL2-36). |
| Screening | Documents comply and applicant qualified | **KEEP WITH GAP** | As Plantilla (SL2-07, SL2-24, SL2-39, SL2-25). |
| Examination | What the candidate scored | **KEEP WITH GAP** | Second pass at the Chief re-keys scores blind and nudges a second invitation (SL2-37); SL2-17, SL2-40 apply. |
| Interview | The end-user / HRM interview score | **KEEP WITH GAP** | Single-score model is right; "Absent" is sticky and walls the pool (SL2-11); SL2-16, SL2-18 apply. |
| Per-vacancy selection round and pick | Which eligible candidate the Chief hires this round | **CHANGE** | An open round can never be closed short (SL2-12); rescinding an earlier round's pick while a newer round is open crashes (SL2-13); a retained pooled candidate has no exit (SL2-41); the batch page hides the open-round control exactly when it is needed (SL2-42); hired member stays a pre-checked radio (SL2-43). |
| HRM Chief final decision | — (display of the authoritative pick) | **KEEP** | SL-01 closed: no COS decision POST exists; `user_can_record_final_decision` is true only when the finalized pick names the case (services.py:4707-4708). |
| Completion and closure | Appointee supplied the contract requirements | **KEEP WITH GAP** | As Plantilla (SL2-29). |
| Cancellation / rescind / reopen | Non-merit exits and corrections | **CHANGE** | Cancelling a COS vacancy ignores a live pick and closes the winner as "vacancy cancelled" while the pick stands (SL2-44); COS rescind after closure leaves the contract record attached (SL2-30); cancelled-vacancy cases are viewable by nobody (SL2-45); reopen has no cancelled/withdrawn guard (SL2-31). |

### 2.3 Cross-cutting

| Area | Verdict | Reasoning |
|---|---|---|
| Case state machine | **KEEP WITH GAP** | Every combination the code writes is reachable and surfaced in a queue or the closed list — except `vacancy_cancelled` cases (SL2-45). `RETURNED_TO_APPLICANT` case status and its re-entry path are orphaned (SL2-46); the manual "Actions" step is dead (SL2-47). |
| Withdrawal (SL-10) | **KEEP WITH GAP** | Recorded only by the stage's current handler; at HRMPSB/AA the office that receives the letter cannot record it (SL2-48). |
| Suspension | **CHANGE** | Suspended pauses public intake only; no case/batch/SLA effect, no notice, no reason on Suspend/Close (SL2-49). |
| Audit as process record | **KEEP** | Every transition found writes an audit row with actor; reasons are required on reject, handoff, reopen, withdraw, cancel, rescind, remove, absence-resolve, deadline-change. Only vacancy Suspend/Close/Reactivate lack a reason (SL2-49). |

---

## 3. Findings, prioritized

Format: ID — title · taxonomy · stage/role · severity · verification. Then:
**Now** (what the system does), **Work** (what the office needs), **Evidence**,
**Failure**, **Change**, **Must not break**, **Accept** (acceptance criteria).

### Critical

**SL2-01 — The entry edit form's `status` dropdown bypasses every lifecycle gate** · #3 + gate bypass · Vacancy entry, Secretariat / entry managers · Critical · re-verified.
- **Now.** `RecruitmentEntryForm.Meta.fields` includes `"status"` (forms.py:2848-2861), rendered at recruitment_entry_form.html:69. The save path `persist_recruitment_entry` → `full_clean()` → `save()` (services.py:12229-12236) has no WF-07 early-close check and never calls `cancel_recruitment_entry`. WF-07 lives only in `update_recruitment_entry_status` (12553-12566); the reason + case resolution only in `cancel_recruitment_entry`. `RecruitmentEntryStatusUpdateView` refuses "cancelled" (entry_views.py:242-250); the form path accepts it.
- **Evidence (probe, rolled back).** Status-link Close refused ("publication period runs until 2026-09-01") → edit form `status=closed` saved; `pool_finalized=True`. Edit form `status=cancelled` saved: posting cancelled, 3 cases still ACTIVE, 0 applications marked vacancy-cancelled; the real Cancel then refused "already cancelled".
- **Failure.** Early pool finalization mid-publication (exactly what WF-07 forbids); a "cancelled" vacancy with live cases that never receive the cancellation notice; the real cancel locked out forever.
- **Change.** Remove `status` from the edit form (the create form may keep Draft/Active only); every transition goes through the status links or the cancel form.
- **Must not break.** RD-7 "closed → reopen first" check; create-form default.
- **Accept.** (1) Edit form has no status control; posting `status` unchanged after any edit POST that carries `status`. (2) Closing a posting during WF-07's window is refused on every path. (3) A posting can reach CANCELLED only through `cancel_recruitment_entry` (reason recorded, cases resolved, notices sent).

**SL2-05 — Office handoff silently cancels an in-flight resubmission** · #7/#6/#5 · Case header "Send to Secretariat / HRM Chief", while `AWAITING_RESUBMISSION` · Critical · re-verified.
- **Now.** `route_case_between_secretariat_and_hrm_chief` never checks `case_status` and unconditionally sets `case.case_status = ACTIVE` (services.py:1655-1659) while `_handoff_target_status` leaves `application.status = RETURNED_TO_APPLICANT` (1543-1544). Every applicant-side reader keys on `case_status == AWAITING_RESUBMISSION` (`get_resubmission_request` 9108-9110; `submit_document_resubmission` 9135-9137; `_build_applicant_status_context` portal_views.py:100-102).
- **Evidence (probe, rolled back).** Built awaiting state with the real services; after handoff: `app.status=returned_to_applicant case_status=active handler=secretariat`, deadline still set, `get_resubmission_request → None`, `submit_document_resubmission → "not awaiting a document resubmission"`, batch `pending=1 is_ready=False`, SLA pause lifted.
- **Failure.** The applicant opens the emailed link and is told nothing is awaiting resubmission; the status page says "Action required — check your email" with no button; the batch stays blocked; recovery only by re-returning (second request, second email, new 14-day clock).
- **Change.** Refuse handoff while `case_status == AWAITING_RESUBMISSION` ("wait for the resubmission or remove the applicant first") and omit the option from `get_case_handoff_options`; or carry the awaiting status and SLA pause across the handoff.
- **Must not break.** Level-2 override creation/consumption; RD-1 return atomicity; SL-10 withdrawal during awaiting.
- **Accept.** (1) Handoff on an awaiting case is refused server-side with a plain message and not offered in the header. (2) A resubmission link issued before a (permitted) handoff still works after it. (3) Existing handoff tests pass.

**SL2-06 — Level-2 Plantilla: Secretariat-override exam finalize strands the HRM Chief with an empty action card** · #5/#4 · Action card, HRM Chief at `HRM_CHIEF_REVIEW` · Critical · re-verified.
- **Now.** `_auto_advance_after_exam_finalized`'s Plantilla branch returns after routing Secretariat → HRM Chief (services.py:1827-1840), never on to HRMPSB. The Chief then sits at section `actions`; `_auto_advance_boundary_is_ready` is True for Plantilla (2304-2314) and `get_available_actions` returns `[]` before reaching the Chief branch (8906-8911). `action_form` is built only when actions exist (views.py:1101-1103). The only exit is the CLI `repair_workflow_boundaries`.
- **Evidence (probe, injected Level-2 state on a real case + real override + real service calls, rolled back).** After auto-advance: `stage=hrm_chief_review handler=hrm_chief override_active=False`; `section: actions boundary_ready: True; chief actions: []`; a forced `endorse` POST succeeded → `hrmpsb_review` (server permits what the UI withholds).
- **Failure.** Every Level-2 Plantilla case whose exam was finalized under override parks at the Chief with no next step.
- **Change.** In the Plantilla branch, after routing back to the Chief, continue to HRMPSB in the same call (or route Secretariat-finalized Level-2 exams straight to HRMPSB, mirroring Level 1).
- **Must not break.** COS Level-2 override return (the Chief keeps the case for interview/deliberation); override-consumption audit; `repair_workflow_boundaries` tests (tests.py:5633, 5667).
- **Accept.** (1) Level-2 Plantilla exam finalized by the Secretariat under override lands the case at HRMPSB with the override consumed and audited. (2) No Plantilla case can sit at a screening stage with section `actions` and an empty action list.

**SL2-07 — Screening can be finalized Qualified while the case is awaiting resubmission → inescapable paused case** · #5/#7/#4 · Screening Step 2, reviewing role · Critical · re-verified.
- **Now.** `save_screening_review` has no `case_status` guard (services.py:5322-5335: the four raises are record-exists / finalized / assigned / stage); `user_can_manage_screening` is true during awaiting; the form renders (views.py:803-812). Marking the flagged row Complied on the OLD file and finalizing Qualified leaves `case_status = AWAITING_RESUBMISSION` with nothing that can clear it: `submit_document_resubmission` needs flagged rows (now none); `remove_applicant_from_batch` needs `current_section == "screening"` (now exam/deliberation, 8878-8886).
- **Evidence (probe, rolled back).** `rec.is_finalized=True outcome=qualified` with `case_status=awaiting_resubmission`; section `deliberation`; available actions `[]`; `get_resubmission_request: []`; `user_can_manage_exam(chief): False`; batch `pending=1 is_ready=False`; SLA `paused`. `vacancy_screening_batch_status` counts it pending on case status alone (3339-3342); `cos_eligible_pool` / `vacancy_exam_pool` filter `case_status=ACTIVE` (4064-4067, 5764-5768).
- **Failure.** The pooled vacancy is blocked indefinitely; the applicant still holds a dead resubmit link; the only exits are withdrawal or the SL2-05 side effect.
- **Change.** In the finalize path of `save_screening_review` (and `_validate_screening_review_consistency`) refuse finalize while awaiting unless the verdict is Not Qualified (which already closes the case); alternatively, on a Qualified finalize, return the case to ACTIVE, clear `resubmission_*`, and audit that the pending request was superseded.
- **Must not break.** NQ-while-awaiting → closed with one rejection email; RD-1 return atomicity; SL-12 reopen.
- **Accept.** (1) Qualified finalize on an awaiting case is refused (or supersedes the request with an audit row) — no case can be finalized-Qualified and awaiting at once. (2) Batch readiness never counts such a case as pending.

**SL2-08 — A pooled Plantilla interview no-show has no door; one absentee blocks the pool's finalize** · #4/#5 · Plantilla batch interview + per-case detail; Secretariat / HRM Chief support · Critical · re-verified. *Shipped gap of SL-06: the Scored/Absent control was added only to the COS branch of the console during the COS end-to-end fix.*
- **Now.** The per-case absence form renders only when `not vacancy_uses_batch_exam(position)` (views.py:940-946, comment: "a pooled interview records absence on the batch console"); `vacancy_uses_batch_exam` is always True for Plantilla (services.py:5757-5758); the console's Absent select (`int_outcome_*`) exists only in the COS branch (vacancy_batch_interview.html:163-171 inside `{% else %}`; views.py:2150-2166). Finalize demands 3 raters for every candidate (6001-6008).
- **Evidence (probe).** Plantilla L1 case at HRMPSB: absence form for secretariat: False; finalize with no ratings → "needs ratings from at least 3 HRMPSB members"; server-side `mark_interview_absent(secretariat)` ACCEPTED — UI hides, server permits.
- **Failure.** The entire pool cannot be finalized until someone fabricates ratings for the absentee or removes them outside SL-06.
- **Change.** Per-candidate Absent control in the Plantilla console that calls `mark_interview_absent` before `finalize_vacancy_interview_batch` (or drop the `not vacancy_uses_batch_exam` condition at views.py:944 so the per-case form appears).
- **Must not break.** An absence never carries a score; quorum 3–5; lock-step finalize.
- **Accept.** (1) A Plantilla pooled candidate can be marked Absent from the UI by the support role; the pool finalizes with that candidate excluded and an `AttendanceException` opened. (2) Test covers pooled Plantilla absence → finalize succeeds.

**SL2-09 — A withdrawal at the Appointing Authority step freezes the vacancy for good** · #5/#6 · AA Final Selection / Return CAR · Critical · re-verified.
- **Now.** `user_can_record_withdrawal` excludes only COMPLETION/CLOSED (services.py:2166-2172), so withdrawal is allowed at AA. `_final_selection_items_are_ready` raises unless EVERY CAR item's application is at `APPOINTING_AUTHORITY_REVIEW` (9466-9484); `record_final_selection` (9686) and `return_car_for_reassessment` (9510) both call it; `user_can_record_final_selection` (4712) does not, so the form renders and the POST fails.
- **Evidence (probe, rolled back; VAC-PLT-2026-002, 3 items at AA).** Withdraw one → `items ready: NO`; selection still offered on a sibling; return CAR BLOCKED. Only exit: `cancel_recruitment_entry` (Sec/Chief), which closes everybody as "vacancy cancelled".
- **Failure.** One applicant emails "I withdraw" after the CAR reaches the AA; the remaining candidates can never be appointed.
- **Change.** Treat terminal non-merit items (WITHDRAWN, VACANCY_CANCELLED) as out of contention in the readiness check and in `FinalSelectionForm.selected_item` / the selection loop (forms.py:2694-2698 currently lists every item). Keep stored `rank_order` (a withdrawn #1 does not promote #2 — SL-08 top-five stands); selecting another candidate after the recommended one withdrew must not demand a WF-03 "deviation" justification. The rescind `closed_siblings` filter (REJECTED only, 9852-9860) already keeps withdrawn ones withdrawn.
- **Must not break.** SL-08 top-five / deep-selection; WF-03; SL-14 restore scope.
- **Accept.** (1) With one CAR item withdrawn, the AA can select any remaining item and can return the CAR. (2) The withdrawn item is not selectable and keeps its rank on the report. (3) Rescind after such a selection restores only previously-rejected siblings.

**SL2-12 — An open COS selection round can never be closed except by filling every slot** · #5/#8 · COS console, HRM Chief · Critical · re-verified.
- **Now.** `CosSelectionRound.Status` is OPEN/RESOLVED only (models.py:3145-3147). The sole resolver `_resolve_cos_selection_round` runs only when `len(finalized_picks)+1 >= slot_count` (services.py:4323-4324). `open_cos_selection_round` never bounds `slot_count` by the eligible count (4147-4155). While a round is open no new round can open (4500-4501) and the pool is frozen to members (4118-4125). No cancel/abandon service, URL, or view exists (only 3998/4367/10027-10029 touch `Status`).
- **Evidence (probe, rolled back; posting 217, 1 eligible).** Open 3 slots → one pick → round still open, `can_open False`; when the pool empties the console GET 404s (views.py:2290-2296), so the stuck round is invisible.
- **Failure.** A round opened for two with one acceptable candidate (or a mis-typed slot count, or a member who withdraws) freezes the vacancy; later applicants reaching deliberation can never be considered.
- **Change.** Audited HRM-Chief "close round early / abandon round" (reason required; outcome `CLOSED_SHORT` or resolve with fewer hires) and validate `slot_count <= eligible` at open.
- **Must not break.** Single-batch resolution via `_close_cos_non_selected_pool`; retained-pool semantics for continuous rounds; rescission reopening a RESOLVED round.
- **Accept.** (1) An open round with unfilled slots can be closed short with a reason, audited; a new round can then open. (2) Opening a round with more slots than eligible candidates is refused with a message.

**SL2-13 — Rescinding an earlier round's pick while a newer round is open crashes with IntegrityError (HTTP 500)** · #9/#5 · COS rescind card, HRM Chief · Critical · re-verified.
- **Now.** `rescind_cos_vacancy_deliberation` unconditionally flips the pick's RESOLVED round back to OPEN (services.py:10023-10033); `one_open_cos_selection_round_per_entry` forbids two open rounds (models.py:3177-3181); `user_can_rescind_cos_vacancy_deliberation` checks only role/branch/flags (9943-9948); the view catches only `ValueError, ValidationError` (views.py:3155-3160).
- **Evidence (probe, rolled back).** Open round 3 on posting 217, rescind the round-1 pick → `IntegrityError: duplicate key value violates unique constraint "one_open_cos_selection_round_per_entry"`. Also: rescinding an older round with no open round reopens round 1 while round 2 stays resolved — an out-of-order open round whose member is simultaneously a retained member of round 2.
- **Change.** In the permission predicate, refuse rescission of a pick whose round is not the latest while another round is open (plain message, surfaced on the card) — or require closing the open round first.
- **Must not break.** Rescission of the latest round's pick; replacement-pick chaining via `supersedes`.
- **Accept.** (1) Rescind on a non-latest round while a round is open is refused with a message, never a 500. (2) Rescinding the latest round's pick still reopens that round.

**SL2-45 — Vacancy-cancelled cases are viewable by nobody and listed nowhere** · #6/#10 · All internal roles; records retrieval (SL-11 follow-through) · Critical · re-verified. *The same defect SL-10 fixed for WITHDRAWN, reintroduced for VACANCY_CANCELLED.*
- **Now.** `user_can_view_application` grants closed-case access only for APPROVED/REJECTED/WITHDRAWN (services.py:1423-1431); `get_closed_cases_for_user` filters the same three (1361-1370). The handler role is blanked at 12506, so no queue matches either.
- **Evidence (probe).** `viewers of a vacancy_cancelled case: []`; closed list includes vacancy_cancelled? False. (No cancellation has yet been run on the demo DB; code-proven.)
- **Failure.** After a real cancellation, every affected case 404/403s for every role; the applicant's cancellation email points at a case the office cannot open; the audit rows exist but the case page does not.
- **Change.** Add VACANCY_CANCELLED to both status sets (and to `vacancy_screening_batch_status`'s terminal set at 3335-3339).
- **Accept.** (1) A cancelled case appears in the closed list and opens for the roles that could view it before cancellation. (2) Sysadmin metadata-only rule and Secretariat L2 exclusion unchanged.

### Major

**SL2-02 — A published closing date can be moved earlier (to today), defeating WF-07; Close is then allowed** · #3/#7 · Entry edit · re-verified. `RecruitmentEntryForm.clean` rejects only a past new date (forms.py:3013-3033); nothing compares against the stored date or WF-07's `close_blocked_until`. Probe: `close_blocked_until 2026-09-01` → shorten to today valid → `close_blocked_until None` → Close ALLOWED; audited (`is_extension False`) but not blocked; applicants not told. **Change:** on a published posting refuse a new closing date earlier than the stored one unless the stored date has already lapsed; Suspend/Cancel stay the emergency valves (client Q-3). **Must not break:** REV-A extensions; COS postings without closing dates. **Accept:** shortening a live deadline is refused server-side; lapsed-date extension still works.

**SL2-03 — Cancelled is not terminal: Activate/Suspend silently un-cancel; the notified applicants can never re-apply** · #9/#8 · Entry list · re-verified. recruitment_entry_list.html:139-156 shows Activate for `status != active` and Suspend for `!= suspended/closed` (both on a cancelled row); `update_recruitment_entry_status` (services.py:12553-12591) has no CANCELLED guard. Probe: cancelled → active ALLOWED, `open_for_intake True`, the 3 applications stay `vacancy_cancelled`, their re-apply refused "already submitted". **Change:** refuse any transition out of CANCELLED in the service and hide the buttons; a cancelled recruitment is re-run as a new entry (client Q-2). **Accept:** every status link on a cancelled posting is refused server-side; no UI button offers it.

**SL2-04 — Manually Closed vacancies vanish from the entry list; every action on them is unreachable** · #4/#6 · Entry list · re-verified. `get_manageable_recruitment_entries` unconditionally `.exclude(status=CLOSED)` (services.py:1399-1407); Edit, Activate, Cancel, Weights, and the N1 CAR filing copy are linked only from that row. Live: posting 215 (closed, 3 active cases) absent. REV-A's own guard "Reopen it (set it Active) before extending" (forms.py:3043-3047) points at an unreachable button. **Change:** list closed entries (toggle or default "open + closed"). **Accept:** a closed posting is reachable from the list with Edit/Activate/Cancel/filing-copy available.

**SL2-10 — A deadline extension reopens a pool whose batch already started (REV-A × SL-09)** · #9/#5 · Entry edit · re-verified. `RecruitmentEntryForm.clean` guards only past dates and CLOSED (forms.py:3011-3047). Probe (posting 213: exam finalized, case at HRMPSB): extension valid → `applicant_pool_is_finalized False` → "batch examination and interview unlock…". The interview console locks mid-batch and new applicants join a cohort that already sat the exam — the second cohort SL-09 exists to prevent. **Change:** refuse extension once any `ExamSchedule`/`ExamRecord` exists for the posting (or weights are locked), pointing at cancellation (client Q-4). **Accept:** extension after a scheduled sitting is refused with a message; extension of a genuinely open posting still works.

**SL2-11 — COS pooled interview: "Absent" is sticky and walls the whole pool** · #5/#9 · COS batch interview, HRM Chief · re-verified. `save_vacancy_cos_interview_ratings` sets ABSENT (services.py:6488-6499); ATTENDED is set only in `_reopen_missed_sitting` (2098); `save_interview_rating` never touches it; `InterviewSession.clean` raises on ABSENT + ratings (models.py:2203-2213) and every save full-cleans (6054). Probe: after a draft Absent then a score, finalize=False → ValidationError, finalize=True → ValueError; re-sending the schedule fails for the whole pool (atomic loop). **Change:** when the row's outcome is "" and a score is posted, set `attendance_outcome=ATTENDED` before saving the rating. **Accept:** Absent-then-scored candidate finalizes normally; ABSENT with ratings remains impossible.

**SL2-14 — Per-case handoff inside a pooled vacancy splits the batch; neither console can finalize** · #3/#5 · Batch exam console · evidence reviewed (code trace). Handoff is per case (`get_case_handoff_options`, any unlocked case); each console's pool is scoped by `user_can_view_application` (views.py:1773-1778), but `save_vacancy_exam_scores` iterates the whole `vacancy_exam_pool` (services.py:5821, 5836) and on finalize raises for any member without a posted score (5898-5902, naming a candidate not on screen). **Change:** refuse `route_case_between_secretariat_and_hrm_chief` for cases at the exam section of a pooled vacancy (or make the handoff vacancy-wide). **Must not break:** Level-2 override (allow only before the pool forms). **Accept:** a pooled vacancy's console finalizes with all members visible to the finalizing role; mid-batch per-case handoff is refused.

**SL2-15 — Plantilla interview absence can only be decided by an HRMPSB member, not the support office** · #4 + policy · Attendance exception card · re-verified. `AttendanceExceptionView` gates on `user_can_process_application` (views.py:3221); `record_attendance_letter_of_intent` / `resolve_attendance_absence` re-check it (services.py:1996, 2043); at HRMPSB stage the handler is HRMPSB. Probe: secretariat False, hrmpsb_member True. The letter upload calls `upload_evidence_item` without `permission_checked=True` (2000-2008) — widening the predicate alone would re-create SL-07. **Change:** authorize attendance actions for `PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL` at the interview stage and pass `permission_checked=True` (client Q-8 on who decides). **Accept:** the support role can file the letter and decide reschedule/terminal; audit actor/reason unchanged.

**SL2-16 — Three server messages say "remove the ratings"; no removal door exists** · #4/#6/#8 · Interview, support role · re-verified. services.py:6009-6013 (>5 raters), 6132-6134 (absence with ratings), models.py:2210 — and no delete/void service or route for `InterviewRating` / `InterviewFallbackRating` anywhere (only `competency_scores.all().delete()` on re-save, 6299). Real outcomes with no path: a recused/replaced member whose rating exists; a sixth member who rated; a paper sheet encoded against the wrong member. **Change:** audited "void rating (reason)" for the stage support role; voided rows excluded from rater ids and averages; never hard-deleted (CompetencyScore cascade). **Accept:** a voided rating no longer counts toward quorum or the average and stays visible as voided with reason.

**SL2-17 — A rescheduled absentee can be re-scored against the missed sitting's notice** · #7 · Exam/interview reschedule · re-verified. `_reopen_missed_sitting` resets record/session but leaves `ExamSchedule.applicant_notified_at` intact (services.py:2086-2105); exam finalize's only gate is `applicant_was_notified` on the existing schedule (5599-5611); interview finalize has no notice gate. **Change:** on RESCHEDULED clear `applicant_notified_at` (exam) and require a fresh notified schedule (both stages) before finalize (client Q-9). **Accept:** a rescheduled candidate cannot be finalized until a new schedule has been notified.

**SL2-18 — A pooled interview cannot span two days or hold per-candidate slots** · #8 · Batch interview schedule · evidence reviewed. `save_vacancy_interview_schedule` applies one `cleaned_data` to every session (services.py:6380-6400); per-case `InterviewSessionView` refuses pooled writes (views.py:2650-2665); the per-case form is hidden (880-888). Logistics notices carry the date, so half a 15–20 candidate panel gets a wrong one. **Change:** optional per-row datetime override on the console (`InterviewSession` is already per application); finalize stays lock-step (client Q-10). **Accept:** two candidates in one pool can hold different dates; each notice carries its own.

**SL2-19 — Assessment weights can be unlocked and edited after the CAR is finalized; the blinded print and the N1 filing copy recompute from the live split** · DC/#9 · Weights unlock; CAR print; filing copy · re-verified. `unlock_vacancy_assessment_weights` (services.py:12292-12331) checks only reason + "already editable". Print passes live weights (views.py:2863); the filing copy computes EPS from `entry.assessment_weights_or_default` (2894-2912); the report stores only a display string. Probe: locked 40/20/40 → unlock + edit 50/10/40 succeeds; finalized item score unchanged; snapshot still says 40/20/40 → the signed filing copy shows an EPS column inconsistent with its own Overall column (the template itself admits the drift near line 492). **Change:** refuse unlock while a finalized non-returned CAR or effective selection exists; store the numeric split on the report and render print/filing from it. **Must not break:** SL-00 unlock-with-reason before finalization; exam-stage lock. **Accept:** unlock after a finalized CAR is refused; printouts of an old CAR render its own weights after a later legitimate change.

**SL2-20 — REV-B gap: the vault CAR PDF (draft and final) is named, and HRMPSB members can decrypt it** · UI hides / server allows · Evidence download, HRMPSB member · re-verified. The CAR artifact lists `reference_number | applicant_display_name` per candidate (services.py:11486-11490) and is stored on every Prepare-draft and Finalize (7301-7312). Access = `get_evidence_context_application_for_user` (3070-3095): any application of the entry the user can view. Probe: HRMPSB access to CAR v1 (draft) True, v2 (final) True. The detail page lists evidence only for the sysadmin (views.py:789-797), so it is hidden, not forbidden; `EvidenceDownloadView` (1477) is any internal user + context. **Change:** build the vault PDF blinded (codes only; the named record is N1's filing copy) or deny the CAR artifact type to HRMPSB_MEMBER in `decrypt_evidence_bytes`. **Must not break:** AA access (AA legitimately sees names); Secretariat N1. **Accept:** an HRMPSB member cannot obtain any document mapping codes to names for a vacancy under evaluation.

**SL2-21 — The public vacancy detail shows the branch catalog, not the posting's configured requirements** · #3/#2 · Public vacancy detail · re-verified. `ApplicantVacancyDetailView` passes `self.entry.branch` (portal_views.py:155); requirements.py:188-203 consults per-posting rows only when given a posting; the intake form passes the entry (forms.py:563). Live: VAC-COS-2026-002/003 detail says Training Certificates "if applicable", the intake form requires them. **Change:** pass `self.entry`. **Accept:** detail and intake list identical requirements for every posting with rows; legacy postings unchanged.

**SL2-22 — A withdrawn applicant (or one whose vacancy was cancelled) is permanently barred from re-applying, with a misleading "already submitted" message** · #8 · Intake · re-verified. The duplicate rule keys on `submitted_at` not null regardless of terminal status (forms.py:795-803; services.py:8055-8074) plus the applicant+position unique constraint (models.py:884-887). Probe: withdrawn re-apply refused. **Change:** per client Q-1 — at minimum exclude WITHDRAWN/VACANCY_CANCELLED from the rule (reset path for the unique row) or give a status-specific message. **Accept:** decision recorded; message never says "already submitted" to someone the system knows withdrew.

**SL2-23 — No workflow path to correct an applicant's email or phone after submission** · #8/#9 · Applicant portal / internal · evidence reviewed. Status lookup filters `applicant_email__iexact` (portal_views.py:660-668); all OTPs go to `application.applicant_email` (services.py:8259); the OTP hash includes the email (8201); no internal form edits `applicant_email` (forms.py only in the intake form, 785-801); the help page tells the applicant to "contact the Public Assistance Desk" (applicant_help.html:160-162), which has no action — only Django admin, unaudited. **Change:** handler-side "update applicant contact" with reason, audit row, notice to old and new address; the new email must be verified before it becomes the OTP target (client Q-5). **Accept:** a handler can change the email through the workflow; the change is audited; the old address is notified.

**SL2-24 — The QS the verdict was judged against is neither frozen nor preserved** · #2/#8 · Screening decide + locked body; entry edit · evidence reviewed. The verdict is rendered beside the posting's `qualification_reference` + `PositionReference.qs_*`; `LIVE_METADATA_LOCKED_FIELDS` covers only position_reference/branch/intake_mode/publication_date/opening_date (models.py:403-420, 547-575); `qualification_reference` stays editable after submissions (forms.py:2852-2862) and `PositionReference.qs_*` is editable in admin (admin.py:204). `ScreeningRecord` stores no QS snapshot (models.py:1189-1275); `screening_locked_body.html` shows verdict + notes, no QS (60-215). **Change:** snapshot the rendered QS text onto the screening record at finalize and print it in the locked view and exports; add `qualification_reference` to the live lock or to an audited-change path (client Q-7). **Accept:** a locked screening shows the QS text that applied when it was decided, even after the posting's text changes.

**SL2-25 — The resubmission deadline has no extension or late-acceptance action** · #4/#8 · Screening, reviewing role; applicant resubmit page · evidence reviewed. `resubmission_deadline` is written at request (services.py:9037) and cleared at resubmit (9173); `submit_document_resubmission` hard-blocks late uploads (9140-9141). The office's only moves are Remove from Batch or "Return to applicant" again, which records a fresh request (new +14 days, new email, 9034-9100) — not an audited extension as SL-13 decided for completion. **Change:** explicit "Extend resubmission deadline" (audited old/new/reason, one reminder email); never auto-forfeit (client Q-6). **Accept:** an extension changes the deadline on the existing request with an audit row; the applicant's link keeps working.

**SL2-26 — Deliberation facts are captured twice, and the one that matters (the date) is never stored** · #1/#6 · Interview-batch finalize; CAR finalize modal; CAR print/filing copy · re-verified. Interview-batch finalize takes `first_deliberation_date/members` and folds them into a free-text audit note only (views.py:2187-2201); the CAR model has no deliberation-date field (models.py:3455-3490); CAR finalize re-asks `quorum_met` (yes/no, forms.py:2633-2636) AND `members_present` 3–5, and the server demands both (services.py:7243-7250; model clean 3575-3583) although 3–5 present ⇒ quorum. Both printouts leave "Date of Deliberation: ______" blank (car_print.html:177; car_filing_copy). **Change:** drop `quorum_met` (derive from `members_present ≥ 3`); add `deliberated_on` + `members_present` to the CAR finalize modal, prefilled from the interview-finalize capture; print both ("Date of Interview" likewise — `InterviewSession.scheduled_for` exists). Keep the WF-02 hard gate on `members_present` (client Q-11 on which date). **Accept:** the finalized CAR stores and prints the deliberation date and members present; no yes/no quorum control remains.

**SL2-27 — The Appointing Authority cannot say "no appointment from this CAR"** · #8 · AA decision · re-verified. The AA's only workflow action is `return_car_for_reassessment` (services.py:8886-8896); approve/reject/return-to-Chief are refused for Plantilla (9316-9320); the sole terminal alternative is `cancel_recruitment_entry`, gated to Secretariat/HRM Chief (entry_views.py:207) with a non-merit "vacancy cancelled" disposition and email. **Change:** first-class AA outcome "No appointment — failure of search" with reason: items close NOT_SELECTED (merit), the report stays finalized/superseded, the posting is flagged for republication; audited as a decision with outcome NONE_SELECTED (client Q-12). **Accept:** the AA can close a CAR with no appointee from their own screen; affected applicants receive a merit (not cancellation) notice.

**SL2-28 — "Return for reassessment" can only re-rate ETE; interview and exam components are sealed with no reopen** · #5/#9 · AA return → Secretariat CAR · re-verified (mechanics); policy = client. After return, cases are at HRMPSB stage section "car"; `_car_draft_candidate_rows` recomputes from finalized records (services.py:6946-6990), so only ETE + recommendation can move. `user_can_reopen_case` (3207-3213) requires `is_stage_locked`, which a returned case is not; `reopen_recruitment_case` is Chief-only on locked cases. Probe: after return, Sec can prepare CAR True; reopen Sec/HRMPSB/AA all False; interview finalized True. **Change:** either label honestly ("Return CAR for correction of ETE/recommendation") or give the HRM Chief an SL-12-style "reopen interview session for this vacancy (reason)" that un-finalizes the batch interview while the CAR is returned, archiving prior ratings (client Q-13). **Must not break:** N2 lock; SL-03 raters; CAR versioning.

**SL2-29 — The completion handler cannot record when a requirement was actually received** · #2/#8 · Completion Step 1 · re-verified. `CompletionRequirementForm.fields` = item_label/status/notes (forms.py:1297-1305); `save()` stamps `completed_on = today` on first COMPLETED (models.py:4467-4468); `was_completed_late` compares it to the deadline (4479-4481). A hard copy received before the deadline but encoded after it is flagged late. SL-13's acceptance "record actual completion dates" is half-closed. **Change:** optional `completed_on` date input (default today, not future). **Accept:** a requirement received on day D and encoded on D+4 records D and is not flagged late.

**SL2-30 — Rescind has no cutoff and leaves the former appointee's completion record attached (Plantilla and COS)** · #9/#10 · Rescind cards · re-verified (mechanics); cutoff = client per SL-14. Plantilla: `user_can_rescind_final_selection` = AA and not already rescinded (services.py:9810-9820); probe on a CLOSED/APPROVED case with a CompletionRecord: rescind succeeded, winner and sibling back at AA, completion record still attached; it is OneToOne (models.py:4221-4231) and silently reused by `save_completion_tracking` (7411-7418) on re-selection. COS: no cutoff by design (9941-9943); probe on a closed case with a contract reference: rescind accepted → case back at Chief review, completion record still present; a different pick leaves an orphaned contract record on the loser's case, shown as "Contract tracking". **Change:** on rescind, void/archive the completion record (flag + reason) in the same transaction; show the vacancy's rescind history at completion; client decides the cutoff (suggested default: refuse once `close_recruitment_case` has run or a contract reference is set — "handle as separation") (client Q-14). **Accept:** after a rescind, no live completion record remains on any case of the vacancy; a rescind past the decided cutoff is refused with a message.

**SL2-31 — Reopen has no notion of why the case closed** · #7/#9 · HRM Chief reopen · re-verified (guard absence); wrong-record effect evidence reviewed. `reopen_recruitment_case` checks role/lock and the RD-7 selection guard only (services.py:10795-10833); nothing checks `entry.status == CANCELLED` or `case_status in {WITHDRAWN, VACANCY_CANCELLED}`. `stage_record_for_correction` (10768-10788) assumes the LAST finalized record is the decision that terminalized the case — true for screening-NQ / exam-absent / interview-absent closes, false for withdrawal and cancellation. Probe: `user_can_reopen_case(hrm_chief, cancelled case): True`; withdrawn case reopenable with no block. **Change:** refuse reopen when the posting is CANCELLED; for WITHDRAWN, reopen = reverse the withdrawal (no record "correction") with a distinct audit action and applicant notice; compute the correction target from the closing audit action rather than "last finalized". **Accept:** a cancelled vacancy's case cannot be reopened; reopening a withdrawn case does not unfinalize a valid record.

**SL2-32 — Reopen, rescind and cancellation emit no in-app signal to the receiving handler, and reversals of a communicated outcome send the applicant nothing** · #2 · All handlers; applicants · evidence reviewed (three functions read in full). `reopen_recruitment_case` (services.py:10790-10925) writes audit + routing only; `rescind_final_selection` (9823-9931) and `rescind_cos_vacancy_deliberation` (9951-10120) restore winner + siblings with audit only — no in-app, no applicant email although each restored applicant previously received SELECTED or NON_SELECTED; `cancel_recruitment_entry` emails applicants but nothing to the handlers whose cases vanished. **Change:** in-app CASE_RETURNED to `current_handler_role` on reopen/rescind; a neutral "decision under review" applicant email on rescind; in-app to previous handler roles on cancellation (client Q-15 on wording). **Accept:** every reopen/rescind produces a bell for the receiving role; a previously "selected" applicant receives a notice when the selection is rescinded.

**SL2-33 — Failed terminal-outcome emails raise no in-app alert (RD-9 gap)** · #6 · Secretariat / HRM Chief · re-verified. `emit_notification_failed_in_app_alert` returns `[]` when `case.current_handler_role` is blank (services.py:2726-2731). Every terminal close blanks the handler before queuing the email (reject 9391/9398; remove 9249; exam absent 1896; attendance terminal 2121; non-selected siblings 9740/10160; withdrawal 2230; cancellation 12506), and delivery runs `on_commit` after the blanking. Probe: for every non-selected notification log, alert recipients NONE (early return); the NOTIFICATION_FAILED audit row is still written. **Change:** when the handler is blank, alert the role recorded in the closing audit or the NOTIFICATION_MANAGER_ROLES with closed-case access. **Accept:** a failed "not selected" email produces a bell for a role that can resend it.

**SL2-34 — A single-batch COS vacancy can fill exactly one slot, ever** · #8 (overlaps the SL-02 decision) · COS console · re-verified. `_open_implicit_cos_selection_round` hardcodes `slot_count=1` and refuses a second round (services.py:4333-4345); `PositionPosting` has no headcount field; `_close_cos_non_selected_pool` then rejects everyone else (10149-10155). A posting "3 COS Nurses, pooling" closes two intended hires as Not Selected after the first finalize. **Change:** posting-level "positions to fill" (default 1) feeding the implicit round — the multi-pick code path already exists (client Q-16). **Accept:** a single-batch COS posting with N positions finalizes N picks before closing the rest.

**SL2-35 — Level-1 COS "independent validation" pass can be performed by the Secretariat through a plain handoff** · #8/#3 · HRM Chief second pass (SL-05) · evidence reviewed (code trace). `route_case_between_secretariat_and_hrm_chief` applies override rules only when `application.level == LEVEL_2` (services.py:1621-1648); a Level-1 COS case at the Chief stage goes to the Secretariat with no override and no audit beyond ROUTED; `user_can_manage_screening` then passes for the Secretariat (3242-3251, 1441-1458); `ScreeningRecord.clean` accepts either role at either stage (models.py:1290-1300). The prior-review card and "Validate" wording are gated on `user.role` (views.py:1023-1036), so the Secretariat re-keys blind. **Change:** for Level-1 COS at `HRM_CHIEF_REVIEW`, refuse Chief→Secretariat handoff while the screening section is open, or require the same explicit override record + audit used for Level 2; render the prior-review card by stage, not role (client Q-17). **Accept:** both "independent" screening records on a Level-1 COS case cannot be authored by the Secretariat without an override record.

**SL2-36 — L1-COS split-stage lockout: one absentee at Secretariat stage pins the console away from the Chief's cohort** · #5 · Batch exam console · re-verified. `_vacancy_batch_stage`: earlier stage wins (services.py:3284-3312). An exam Absent does not advance (5719-5727); a RESCHEDULED absentee reopens at the same stage (2086-2095). The Chief cannot view Secretariat-stage cases → `_batch_pool_empty_redirect` "No candidates are at the exam step"; the per-case exam is refused for pooled vacancies. **Change:** pick the stage from the requesting user's scoped cases, or exclude cases with a pending/rescheduled `AttendanceException` from the vote. **Accept:** the Chief's second pass proceeds for the advanced cohort while one candidate's absence is pending.

**SL2-37 — L1-COS pooled second pass: the HRM Chief re-keys exam scores blind and is nudged to re-invite applicants** · #1/#3/#2 (shipped gap of WF-12/UX-03) · Batch exam console · evidence reviewed (code trace; no L1 pooled COS in demo data). A fresh exam record is required at the Chief stage (services.py:3458-3461, SL-05 kept). The console reads only the current-stage record (views.py:1783-1795) with no prior-stage column; the "Copy results from Secretariat" prefill exists only on the read-only per-case body (1037). With no current-stage schedule the console says "Send the exam schedule to the batch…"; doing so creates new `ExamSchedule`s (`created=True` → `should_notify`) and re-sends exam invitations for an exam already sat (5534-5549), although the service would accept the prior notified schedule (5604-5610). `_deliberation_snapshot_for_exam` consumes the Chief's copy, so a re-key typo is what deliberation sees. **Change:** prior-stage General/Technical per row + "copy all"; hide the schedule nudge when any notified schedule exists (client Q-18: re-key or attest). **Accept:** the Chief's console shows the Secretariat's scores per row; no second invitation is sent for a sat exam.

**SL2-38 — "Send to Secretariat" is offered on Level-2 cases at every stage, but the server refuses it outside HRM Chief review; a Level-1 completion case can be handed to the Chief** · #4 · Case header · re-verified. `get_case_handoff_options` (services.py:1554-1572) never checks stage or level; `route_case_between_secretariat_and_hrm_chief` raises for L2→Secretariat unless `current_stage == HRM_CHIEF_REVIEW` (1640-1646). Probe: COS L2 cases at COMPLETION offer "Send to Secretariat" (refused on POST); a COS L1 completion case offers "Send to HRM Chief", which succeeds — the Chief's queue then shows it while `_completion_handler_role` says Secretariat. **Change:** compute options from the same predicate the service enforces; decide whether completion-stage handoff is intended at all. **Accept:** every offered handoff option succeeds; none is offered that the service would refuse.

**SL2-41 — A retained pooled candidate on a continuous COS vacancy has no "not selected / retention lapsed / declined" outcome** · #8/#6 · Pooled case at deliberation, HRM Chief · re-verified. `get_available_actions` returns `[]` outside the actions/decision sections (services.py:8903-8904); the COS decision forces SELECTED (10251-10253); withdrawal is applicant-initiated (2151-2159). Only vacancy cancellation ends the case. Live: posting 217 case 486 sits at `hrm_chief_review/active` indefinitely. **Change:** audited HRM-Chief "Close from pool" with required reason + non-merit notice, outcome distinct from Rejected; manual (keep SL-13's no-auto-forfeit) (client Q-19). **Accept:** a pooled candidate can be closed from the pool with a reason and a notice; the closure is distinguishable from a merit rejection.

**SL2-44 — Cancelling a COS vacancy ignores a live COS pick; the winner is closed as "vacancy cancelled" while the pick and FinalDecision remain effective and rescindable** · #3/#7 · Vacancy cancel (SL-11) · re-verified. The cancel guard checks only Plantilla `FinalSelection` (services.py:12476-12480); the affected set sweeps the COS winner at Completion (ACTIVE) (12488-12498). Probe (posting 214): winner `closed/vacancy_cancelled` while `pick effective True`, `FinalDecision live True`, `can_rescind True` → a later rescind moves a cancelled case back to Chief review on a cancelled posting. **Change:** mirror the Plantilla guard ("Rescind the pick first") or rescind as part of cancellation (client Q-20). **Accept:** a COS vacancy with an effective pick cannot be cancelled without the pick being rescinded first or in the same action.

**SL2-48 — Withdrawal can only be recorded by the stage's current handler; at HRMPSB/AA stages the office that receives the letter cannot record it** · #4/#8 · SL-10 · re-verified. `user_can_record_withdrawal` → `user_can_process_application` (services.py:2151-2173, 1441-1456): effective role must equal `case.current_handler_role`. Probe: Plantilla case at HRMPSB → `withdraw_by=['hrmpsb_member']`; cases at AA → `['appointing_authority']`; the Secretariat is absent in both, although the Resolution Log (SL-10) says the Secretariat records it "any time before appointment". **Change:** allow the interview-support / CAR-preparer roles (the carve-outs at 1459-1491) to record withdrawal at any pre-completion stage. **Accept:** the Secretariat can record a withdrawal while the case is at HRMPSB or AA; audit and evidence attachment unchanged.

**SL2-49 — "Suspended" is a floating vacancy state: pauses intake only; no case/batch/SLA effect; no notice; Suspend/Close carry no reason** · #2/#9 · Vacancy status links · re-verified. `EntryStatus.SUSPENDED` is read only by `is_open_for_intake` (models.py:718-720) and `get_public_recruitment_entries` (7757-7763); nothing else references it. `update_recruitment_entry_status` (12553-12591) is a one-click link (entry_views.py:233-257), audited without a reason; batches and SLA clocks keep running (acknowledged in the SL-11 docstring 12457-12460). **Change:** define suspension semantics (pause batch scheduling + SLA; optional applicant notice) or rename to "Intake paused"; require a reason on Suspend and on manual Close (client Q-21). **Accept:** Suspend and Close require a reason that lands in the audit row; the console shows the suspension on in-flight batches.

### Minor

**SL2-39 — Not-Qualified records with flagged rows read as "returned"; the disposition bridge is orphaned** · DC/#10 · Screening locked body · re-verified. RD-1 lets Not Qualified finalize with flags (probe: `outcome=not_qualified completeness=incomplete flagged_rows=1`); the locked body prints "N flagged for resubmission" (screening_locked_body.html:72) and a "Returned for resubmission" tag per row (143) although no return occurred. `screening_requires_disposition_for_current_stage` (services.py:3258-3266), which RD-4 said it deleted, still feeds `_workflow_section_is_complete` (3472-3477) and the locked-body bridge copy. **Change:** on NQ finalize label flagged rows "Flagged as deficient (not returned)"; delete the disposition helper and bridge branch.

**SL2-40 — Batch exam validity window silently wiped on re-save** · #1/#7 · Batch exam console · re-verified. `valid_from/valid_until` inputs have no value binding; the view passes blank → `None` (views.py:1944-1946); `save_exam_record` overwrites both on every save (5650-5651). **Change:** prefill from the pool's record, or overwrite only when posted.

**SL2-42 — The batch page hides "Open COS deliberation" exactly when a continuous vacancy needs a round opened** · #4 (UI hides; server allows) · Vacancy batch detail · re-verified. `cos_deliberation_ready = any(pool)` (views.py:1740-1743) and the pool is `[]` with no open round (services.py:4127-4129); the console GET allows via `user_can_open_cos_selection_round` (views.py:2292-2296). Only escape: a link inside a pooled case's deliberation panel. **Change:** `cos_deliberation_ready = pool or user_can_open_cos_selection_round(user, entry)`.

**SL2-43 — In an open multi-slot round the already-hired member stays a pre-checked radio candidate with notes prefilled** · #1/#7 · COS console · re-verified. `cos_deliberation_pool` filters only ACTIVE/unlocked (services.py:4118-4125); `_context` pre-checks the latest finalized pick (views.py:2227, 2300-2303). The server refuses the re-pick (4245-4246) — confusion only. **Change:** exclude members with a live pick; pre-check only an unfinalized draft.

**SL2-46 — `RETURNED_TO_APPLICANT` case status, its `_sync` branch, the generic-return re-entry path, and the `APPLICATION_RETURNED_TO_APPLICANT` email are orphaned** · #10 · re-verified (grep + live DB 0 rows). Written only at services.py:7689 inside a branch no caller reaches (RD-6b removed the generic return, 9289-9305). Dead downstream: `_upsert_recruitment_case_for_submission` re-entry branch 7617-7648, `_reopen_screening_after_resubmission` 7542-7596, the notification type + `email/application_returned.html`, the SLA "paused" branch for that status (models.py:1034, 1046). Only tests construct it. **Change:** remove the enum value, branches, template, and fixtures; AWAITING_RESUBMISSION stays the single return state. **Must not break:** `request_document_resubmission` (the *application* status RETURNED_TO_APPLICANT stays in use).

**SL2-47 — The manual "Actions" step is dead: endorse / recommend / return-to-Chief / HRMPSB-reject / all AA branches are never offered** · #10/#4 · re-verified. `get_available_actions` returns `[]` whenever `_auto_advance_boundary_is_ready` (services.py:8906-8911); `current_section == "actions"` requires the stage's exam (or CAR) finalized, and `_auto_advance_boundary_is_ready` (2304-2323) is True in exactly those conditions for every branch that has an "actions" section. Lines 8911-8945 are unreachable; `WorkflowActionForm.clean_action` restricts POSTs to the offered choices (forms.py:1071-1094); `_transition_target` branches for endorse/recommend/HRMPSB return/reject and the AA block (8948-8999) are orphaned; the only live entry is the programmatic screening `reject` (5443-5451). Live probe: every handler's actions are `[]` except the AA's return. **Risk:** a later change that makes the section reachable would silently re-expose an HRMPSB "Reject Application" and a Plantilla "Return to HRM Chief". **Change:** delete the "actions" section and the dead branches, keeping `reject` as the single programmatic path — or reinstate a real manual door with a spec.

**SL2-50 — Dead ETE score fields still rendered, validated and audited** · #10 · Screening locked body · re-verified. screening_locked_body.html:193-215 prints "Document review score … No score recorded" on every record; `save_screening_review` audits four always-null scores (services.py:5416-5420); `ScreeningRecord.clean` still enforces range/trio rules (models.py:1302-1322); the view never posts them (forms.py:1564-1568). RD-3 kept the model fields; the rendering/auditing remnants were not part of that decision. **Change:** hide the rows unless a value exists; drop the four keys from audit metadata. **Must not break:** the CAR `ete_rating or document_review_score` fallback for historical records (6972, 7063).

**SL2-51 — Track Application's "unfinished draft" branch is dead code** · #10 · Public status lookup · re-verified. `get_unfinished_draft` filters `tracking_code` + `submitted_at` null (portal_views.py:647-657) but `tracking_code` is assigned only in `submit_application` (services.py:8807); live: 0 drafts with a tracking code. The tokened link handles drafts correctly. **Change:** delete the branch (or assign the tracking code at draft creation).

### Leads (UNVERIFIED — one line each; not findings until probed)

- L-1 (#1) Intake asks for free-text `cover_letter` and `qualification_summary` while also requiring the signed letter, PDS and Work Experience Sheet uploads; the text is shown to screeners (application_detail.html:75-85) — double entry, or is anyone reading it? (Q-22)
- L-2 (DC) `performance_rating_applicability` flag + file presence are two copies of one fact; the error at forms.py:805-814 exists only because of it.
- L-3 (#1) Entry `qualification_reference` free text vs `PositionReference.qualification_summary`; the public page prints both (applicant_vacancy_detail.html:93-103).
- L-4 `deadline_extended` is only ever set True (entry_views.py:166); a later earlier-move keeps the "Extended" label.
- L-5 (#6) Public detail 404s for Suspended/Closed/Cancelled postings (portal_views.py:145-150) instead of "temporarily not accepting".
- L-6 Document requirements can change while unsubmitted drafts hold uploads (lock keys on submitted only, models.py:550-555); dropped codes orphan draft evidence silently.
- L-7 A deadline change writes two audit rows (RECRUITMENT_ENTRY_UPDATED + DEADLINE_CHANGED) — REV-D "concise".
- L-8 (#10/#2) `CompetencyDefinition.description` is orphaned: the builder edits group/name/weight only (forms.py:1374); the rater screen shows names only (weight as a data attribute); the print sheet renders description/weight — raters rate without definitions on screen.
- L-9 (#8) Panel composition is not modeled: notices go to every active HRMPSB user (services.py:3756-3765) and any may rate; recusal/replacement per vacancy is inexpressible. (Q-23)
- L-10 `save_vacancy_interview_schedule` passes `notify_applicant=True` unconditionally; re-saving unchanged details re-emails every applicant (exams guard this at 3771-3782).
- L-11 Exam-finalize forward uses `saved[0]`; if that row is the Absent one the redirect lands on the overview (views.py:2004-2006).
- L-12 Plantilla CAR blocks via SL-03 without naming a pending interview absence (COS excludes pending at 4077-4083; Plantilla has no equivalent).
- L-13 Level-2 Plantilla with an active Secretariat override: interview support predicates check `user.role` only (services.py:1470), so the override cannot schedule/encode.
- L-14 (#10) `DeliberationRecordView`, route `deliberation-record`, `DeliberationRecordForm` and the `deliberation_form` / `car_requires_deliberation` branches (views.py:957-977; deliberation_body.html:72-108) are dead behind `user_can_manage_deliberation → False` (4437-4447); SL-15 said quarantine/remove and only quarantine shipped.
- L-15 (#8) A board that recommends nobody cannot finalize — `recommended_case` is mandatory (7196-7209; models 3584-3587). (Q-24)
- L-16 (REV-B) The HRMPSB "returned to you" notice title uses the case number (9586-9592); REV-B's amendment says evaluation-content notices to panel members stay code-only while REV-G says case numbers are label-safe — reconcile.
- L-17 (#8) One appointee per Plantilla posting (`unique_final_selection_per_entry`); no slot count on Plantilla postings. (Q-25)
- L-18 "Date of Interview" prints blank on the CAR though `InterviewSession.scheduled_for` is known.
- L-19 Re-selection notification idempotence after a rescind is unchecked.
- L-20 Closure notes live only in audit metadata (services.py:10444), not on the closed case, next to `remarks`.
- L-21 `record_final_decision`'s `missing_components` refusal fires inside finalize after the pick is saved (rolled back by atomic) with a message the Chief cannot act on.
- L-22 `_scoped_pool` (view) vs the unscoped pool (service) on the COS console — a Chief with a restricted view could finalize against candidates not displayed.
- L-23 `_vacancy_batch_stage`: reopening one NQ-rejected sibling of a Level-1 COS vacancy whose pool is at the Chief's second pass flips the batch stage back to Secretariat, emptying the Chief's consoles.
- L-24 Status page reads the resubmission deadline from the latest record by `updated_at` while the resubmit page reads the current-stage record (portal_views.py:104-114 vs services.py:9111-9122) — two readers of one fact.
- L-25 Attendance absence recorded → the applicant is never told they may submit a letter of intent (`record_attendance_absence` 1945-1982 has no `queue_*`).
- L-26 `return_car_for_reassessment` bells HRMPSB members (9586-9594) but the CAR preparer is the Secretariat at both levels — signal to the wrong role.
- L-27 `process_workflow_action` server-accepts `reject` on an AWAITING_RESUBMISSION case if a stale form offered it — two doors to REJECTED with different audit actions.
- L-28 `get_closed_cases_for_user`'s "exporter-only" branch (1379-1381) has no role that reaches it.

---

## 4. State sweep and gate sweep

### 4.1 Reachability — `RecruitmentCase.Stage × Application.Status × case_status × locked` (live demo DB: 16 cases, 6 postings)

| # | Stage | App status | Case status | Locked | Handler | Produced by | Class | Visible where | Live rows |
|---|---|---|---|---|---|---|---|---|---|
| 1 | secretariat_review | secretariat_review | active | no | secretariat | intake (L1); `submit_document_resubmission`; HRM→Sec handoff (L1); reopen; `_reopen_missed_sitting` | reachable | Sec queue | 0 |
| 2 | hrm_chief_review | hrm_chief_review | active | no | hrm_chief | intake (L2); `_auto_advance_after_exam_finalized` (COS L1 second pass); COS rescind; reopen | reachable | Chief queue | 1 |
| 3 | hrm_chief_review | hrm_chief_review | active | no | secretariat (+active override) | Chief→Sec handoff with override | reachable | Sec queue only while override active | 0 |
| 4 | sec/hrm review | returned_to_applicant | awaiting_resubmission | no | reviewing role | `request_document_resubmission` | reachable, paused | reviewer's queue + Track Application | 0 |
| 5 | any review stage | returned_to_applicant | returned_to_applicant | yes | applicant | `_sync` branch 7688-7693 — no caller | **unreachable / orphaned** (SL2-46) | none | 0 |
| 6 | hrmpsb_review | hrmpsb_review | active | no | hrmpsb_member | exam auto-advance (Plantilla); `return_car_for_reassessment`; reopen | reachable | HRMPSB queue + support carve-outs | 1 |
| 7 | appointing_authority_review | appointing_authority_review | active | no | appointing_authority | `_auto_advance_after_car_finalized`; Plantilla rescind; reopen | reachable (Plantilla) | AA queue | 3 |
| 8 | appointing_authority_review (COS) | — | — | — | — | no producer (`_transition_target` COS endorse raises 8980) | unreachable; COS-AA branch of `get_available_actions` orphaned | — | 0 |
| 9 | completion | approved | active | no | secretariat (L1) / hrm_chief (L2) | `record_final_selection`; `record_final_decision`; reopen of closed/approved | reachable | handler queue + closed list | 3 |
| 10 | closed | approved | approved | yes | "" | `close_recruitment_case` | terminal, reopenable → #9 | closed list | 2 |
| 11 | closed | rejected | rejected | yes | "" | reject (incl. screening NQ); remove-from-batch; exam absent/waived; attendance terminal; non-selected siblings | terminal; reopenable unless RD-7 | closed list | 7 |
| 12 | closed | withdrawn | withdrawn | yes | "" | `record_application_withdrawal` | terminal; **reopenable with no guard** (SL2-31) | closed list | 1 |
| 13 | closed | vacancy_cancelled | vacancy_cancelled | yes | "" | `cancel_recruitment_entry` | terminal; **viewable by nobody, in no list** (SL2-45); server-reopenable (SL2-31) | **none** | 0 |
| 14 | closed | * | active / unlocked | — | internal role | no producer | unreachable | — | 0 |
| 15 | any | status ≠ stage map | — | — | — | none; `_ensure_case_stage_alignment` raises on next write | unreachable-guarded | — | 0 |

Lost-case probe: every live active/unlocked case appears in at least one role queue; every terminal case with status approved/rejected/withdrawn appears in the closed list. The only zero-surface cell is #13. Two further inescapable states are produced by findings rather than by design: finalized-Qualified + awaiting (SL2-07) and Level-2 Plantilla at the Chief with section `actions` (SL2-06).

### 4.2 Gate sweep — gates with a legitimate office path that can never satisfy them

| Gate | Location | Never-satisfiable path | Finding |
|---|---|---|---|
| Live-metadata lock (position_reference/branch/intake_mode/publication_date/opening_date) | models.py:405-427, 547-560 | Blank `publication_date` can never be recorded after the first submission | lead |
| Document requirements locked once live | services.py:12400-12404 | A mis-ticked requirement binds every later applicant for the period | Q-26 |
| Draft upsert duplicate rule | services.py:8055-8063 | Withdrawn / vacancy-cancelled re-applicant | SL2-22 |
| `submit_application` "intake open" | 8749-8775 | OTP email arriving after midnight of the closing day (remedy: extension) | — |
| Resubmit: "not awaiting" / "overdue" | 9133-9146 | After SL2-05/07; late acceptance | SL2-05/07/25 |
| Remove from Batch: screening stage only + awaiting | 9218-9226 | Finalized-Qualified-while-awaiting case | SL2-07 |
| Handoff predicate | 1587-1620 | Missing gates for awaiting (SL2-05) and pooled (SL2-14); offered where refused (SL2-38) | |
| `save_vacancy_exam_scores`: every pool member scored in this POST | 5818-5902 | Split pool after a per-case handoff | SL2-14 |
| Quorum 3–5 distinct raters | 6001-6013 | Plantilla no-show (SL2-08); 6th/recused rater (SL2-16) | |
| ABSENT ⇒ no ratings | 5999-6003; models 2203 | COS draft-absent-then-scored | SL2-11 |
| Attendance letter/decide = `user_can_process_application` | 1996, 2043; views 3221 | Plantilla support office | SL2-15 |
| `_vacancy_batch_stage` earlier-stage vote | 3284-3312 | Chief's L1-COS cohort behind one absentee | SL2-36 |
| SL-09 pool-final | 3420, 5800, 5820, 6392, 6433, 6478 | Batch begun + extension | SL2-10 |
| Exam finalize notified-schedule gate | 5599-5611 | Not blocking; too weak after reschedule | SL2-17 |
| Interview support by level | 3687-3692 | L2 Secretariat override | lead L-13 |
| `_final_selection_items_are_ready` | 9466-9484 | Any withdrawal at AA | SL2-09 |
| `return_car_for_reassessment` | 9510 | Same; and after return the interview cannot be reopened | SL2-09/28 |
| `recommended_case` required | 7196-7209 | Board recommends nobody | lead L-15 |
| `cancel_recruitment_entry` (entry managers; Plantilla selection blocks) | 12474-12483 | AA wanting "no appointment" has no own door; COS pick not checked | SL2-27/44 |
| `reopen_recruitment_case` (Chief, locked) | 10790-10900 | Returned-for-reassessment case is unlocked → unreachable; no cancelled/withdrawn guard | SL2-28/31 |
| `unlock_vacancy_assessment_weights` | 12292-12331 | None blocked — missing finalized guard | SL2-19 |
| Evidence decrypt context | 3070-3095 | None blocked — over-permits HRMPSB | SL2-20 |
| `quorum_met` + `members_present` | 7243-7250 | Redundant | SL2-26 |
| Round: pick must be in snapshot / no second single-batch round / rescind predicate | 4239-4241, 4333-4338, 9943-9948 | Late candidate during a stuck round; multi-hire posting; open-newer-round / cancelled-posting / closed-case rescind | SL2-12/34/13/44/30 |
| `get_available_actions` boundary gate | 8906-8911 | Level-2 Plantilla after override exam finalize | SL2-06 |
| Reopen of a pooled continuous candidate | 10798-10833 | Never locked → nothing to reopen, nothing to close | SL2-41 |
| Withdrawal = current handler only | 2151-2173 | Secretariat while the case is at HRMPSB/AA | SL2-48 |

Gates found sound with no never-satisfiable path: intake checklist/required files/OTP/cooldown/lockout; Plantilla fixed-period + closing date required; COS ≠ fixed-period; closing ≥ opening; job_code/item_number rules; screening record-exists/finalized/assigned/stage; NQ notes and Qualified-requires-Complied; Flag-at-least-one; override grant (Chief, L2, at HRM stage); `process_workflow_action` locked/assigned; both exam components on finalize; Absent/Waived remarks; fallback `rated_by`; justification band; published template required; template lock; past-date schedule refusal; prepare CAR; `_assert_car_rows_are_complete`; one finalized non-returned CAR per entry; top-five/deep-selection + WF-03; completion manage; `ready_for_closure`; completion deadline/reason; `assign_candidate_code`.

---

## 5. Material event → applicant email → in-app → audit (gaps in bold)

| Event | Applicant email | In-app (to whom) | Audit (actor + reason) |
|---|---|---|---|
| Submission | SUBMISSION_ACK | CASE_ASSIGNED → screening role | CASE_CREATED / APPLICATION_SUBMITTED |
| Screening finalized Qualified | **none** (learns via exam invite) | SCREENING_FINALIZED → handler-role peers | SCREENING_FINALIZED |
| Screening Not Qualified | NON_SELECTED (cut at screening) | — | DECISION_RECORDED |
| Return for resubmission | DOC_RESUBMISSION_REQUEST | — | DECISION_RECORDED + deadline |
| Resubmission received | — | RESUBMISSION_RECEIVED | APPLICATION_SUBMITTED |
| Remove from batch | NON_SELECTED | — | APPLICANT_DISQUALIFIED + reason |
| Exam scheduled | EXAM_INVITATION | — | EXAM_SCHEDULED |
| Exam finalized / auto-route | **none** | EXAM_FINALIZED + CASE_ASSIGNED | EXAM_FINALIZED, ROUTED |
| Exam absent/waived close | NON_SELECTED | **none on failure (SL2-33)** | APPLICANT_DISQUALIFIED |
| Absence recorded (SL-06) | **none (lead L-25)** | **none** | ATTENDANCE_ABSENCE_RECORDED |
| Absence resolved terminal | NON_SELECTED | **SL2-33 on failure** | ATTENDANCE_ABSENCE_RESOLVED + reason |
| Interview scheduled | APPLICANT_INTERVIEW_NOTICE; panel INTERVIEW_SESSION_SCHEDULED | INTERVIEW_SCHEDULED | INTERVIEW_SCHEDULED |
| Interview finalized | **none** | INTERVIEW_FINALIZED | INTERVIEW_FINALIZED |
| CAR finalized → AA | **none** | CASE_ASSIGNED → AA | CAR_FINALIZED, ROUTED |
| CAR returned by AA | — | CASE_RETURNED → **HRMPSB only, not the Secretariat preparer (lead L-26)** | CAR_RETURNED + reason |
| Final selection / COS decision | SELECTED; siblings NON_SELECTED | CASE_ASSIGNED → completion handler | DECISION_RECORDED, ROUTED |
| Rescind (Plantilla/COS) | **none (SL2-32)** | **none (SL2-32)** | RESCINDED + reason |
| Completion recorded / deadline change | optional manual reminder only | — | COMPLETION_RECORDED / DEADLINE_CHANGED + reason |
| Case closed | **none** | — | CASE_CLOSED + notes |
| Withdrawal | WITHDRAWAL_ACK | — | APPLICATION_WITHDRAWN + reason + evidence |
| Vacancy cancelled | VACANCY_CANCELLED per case | **none to handlers (SL2-32)** | RECRUITMENT_ENTRY_CANCELLED + reason |
| Vacancy suspended / closed / reactivated | **none** | **none** | STATUS_CHANGED, **no reason (SL2-49)** |
| Deadline extension (REV-A) | none (by design) | — | DEADLINE_CHANGED old→new |
| Reopen | **none, even when reversing a sent outcome (SL2-32)** | **none (SL2-32)** | CASE_REOPENED + reason, ROUTED |
| Sec↔HRM handoff | — | CASE_ASSIGNED / CASE_RETURNED | ROUTED + remarks; OVERRIDE_* |
| Email delivery failed | — | NOTIFICATION_FAILED → current handler (**none when blank, SL2-33**) | NOTIFICATION_FAILED |
| Resend | re-delivery | — | NOTIFICATION_RESENT |

Every state transition found writes at least one audit row with actor. Reasons are form-required for reject, handoff, reopen, withdraw, cancel, rescind, remove, absence-resolve, deadline-change; not for vacancy Suspend/Close/Reactivate.

---

## 6. Open questions for the client

Only the office can answer these; do not guess answers into findings.

1. May an applicant who withdrew (or whose vacancy was cancelled, then re-run) re-apply to the same vacancy within the period — same case number or new? (SL2-22)
2. Is a vacancy ever un-cancelled in practice, or is a cancelled recruitment always re-published under a new vacancy code? (SL2-03, SL2-31)
3. May the Secretariat shorten a published closing date at all (e.g. a CSC correction), and must applicants be notified? (SL2-02)
4. After a batch has sat the exam, may the publication be extended (late applicants form a second sitting), or must it be cancelled and re-run? (SL2-10)
5. Who may correct an applicant's email/phone after submission, and with what proof? (SL2-23)
6. Is a late document resubmission ever accepted (extension / late-acceptance policy for the 2-week window), and who authorises it? (SL2-25)
7. Is the QS fixed once the vacancy is published (CSC rule)? Should the system freeze the QS text at first submission? (SL2-24)
8. Who decides reschedule/terminal on a Plantilla interview no-show — HRMU (support office) or the board? (SL2-15)
9. Does a rescheduled absentee need a fresh written notice? (SL2-17)
10. May a pooled interview run across multiple days/slots? (SL2-18)
11. Which date should the CAR carry as "Date of Deliberation" — the first (pre-CAR) deliberation or the final one? (SL2-26)
12. May the AA decline to appoint anyone from a CAR (failure of search → republish)? Who signs that decision? (SL2-27)
13. When the AA returns a CAR, may the board re-rate interviews, or is a return only for ETE/clerical corrections? (SL2-28)
14. Rescission cutoff: allowed after the appointment/contract is issued or the oath taken? Is the old contract voided? Does the matter go back to the board or straight to the next pick? (SL2-30)
15. Must a rescinded "selected"/"not selected" applicant be notified, and with what wording? (SL2-32)
16. Can one COS posting (opening/pooling) cover more than one contract? (SL2-34)
17. Level-1 COS: may the HRM Chief delegate the second "validation" pass back to the Secretariat? If never, forbid it. (SL2-35)
18. Second-pass L1-COS: should the HRM Chief re-key the exam numbers or attest the Secretariat's? (SL2-37)
19. How does the office dispose of a pooled COS candidate whose retention lapses or who stops responding? (SL2-41)
20. Should cancelling a COS vacancy with a hired-but-uncontracted winner be blocked (as Plantilla is) or auto-rescind the pick? (SL2-44)
21. What should Suspend mean for in-flight batches and applicants, and must Suspend/Close carry a reason? (SL2-49)
22. Is the free-text "qualification summary" / "cover letter" box read by screeners, or is the uploaded PDS/letter the only source? (lead L-1)
23. Is the HRMPSB for a vacancy a fixed named panel (recusal/replacement recorded), or "any 3–5 members who attend"? (lead L-9)
24. Can a board recommend none? (lead L-15)
25. Does one announcement ever carry several identical plantilla items? (lead L-17)
26. Should the document checklist stay frozen for the whole period after the first submission, or may it be relaxed mid-period? (gate sweep)
27. Who records a withdrawal received while the case is with the HRMPSB or the Appointing Authority? (SL2-48)
28. Should the completion handler record the date a hard-copy requirement was actually received? (SL2-29)
29. Should the "Not Qualified" notice mention document deficiencies when documents were also flagged? (SL2-39)

---

## 7. Out of scope / escalate

Routed, not planned here.

**UI/UX workstream**
- RD-1 residue still present: templates/internal_includes/screening_body.html:286 shows the literal "Next: Completeness" (JavaScript later rewrites it), and screening_locked_body.html:99 labels a derived value with the question "Are documents complete?". (Earlier status notes claimed these were removed; they were not — the earlier check searched the wrong templates directory.)
- The screening finalize modal labels a derived value "Completeness".
- An awaiting-resubmission case shows no banner on the screening tab itself; the only cue is the batch page.
- Entry list: the Suspend button both submits and toggles a modal (recruitment_entry_list.html:147-154); Cancel uses a `<details>` disclosure with no confirm step while Close has a modal.

**Security / privacy workstream**
- The receipt page and PDF are reachable indefinitely by bare UUID token with no OTP and expose name, case number and tracking code (portal_views.py:428-483) — by REV-E design, but inconsistent with REV-I gating the same data on the status link.
- Status lookup and the tokened link reveal the position title for an unfinished draft before any OTP (portal_views.py:675-682, 738-745).
- A forced POST `endorse` succeeds where the action card is empty (SL2-06) — permissive, not a privilege issue.
- `PositionReference` QS is editable only via Django admin (superuser), outside the role model (relevant to SL2-24).

**Not audited here**
- Sysadmin / Django-admin surfaces (metadata-only rule stands); admin inlines for ExamRecord/InterviewRating are not workflow doors.
- Notification wording for non-selected/cancelled/withdrawn applicants.
- Test-fixture hygiene for the orphaned statuses/actions (SL2-46/47) — a follow-up, not a process finding.

---

## 8. Overall conclusion

The core of both paths is right and stayed right under a second, harder look:
intake guarantees, RD-1 screening as a decision, one-door batch consoles,
the pool-final gate, candidate-code blinding, the CAR computation, the
off-system deliberation model, completion readiness, and closure are all
KEEP or KEEP WITH GAP. Nothing found here changes what a stage decides on
the ordinary path. The audit as process record is sound: every transition
writes an actor, and every destructive one requires a reason except vacancy
suspend/close.

What this pass found is concentrated in three places the first audit and the
revision batches did not reach:

1. **Lifecycle and correction paths are under-guarded.** The vacancy's
   transitions can be bypassed from the edit form (SL2-01), shortened
   (SL2-02), un-cancelled (SL2-03), or extended into a running batch
   (SL2-10); cancelled cases then become invisible (SL2-45). Rescind, reopen
   and cancel do not know about each other (SL2-13, SL2-30, SL2-31, SL2-44).
   These are the exceptional paths an office uses once a year and notices
   only when a real case is stuck.
2. **Five deadlocks by construction.** Handoff during resubmission (SL2-05),
   Qualified-while-awaiting (SL2-07), the Level-2 override exam (SL2-06), the
   pooled Plantilla no-show (SL2-08), and withdrawal at the Appointing
   Authority (SL2-09); on COS, the unclosable selection round (SL2-12). Each
   leaves a real vacancy or case with no UI path forward. SL2-08 is a gap
   left by the COS end-to-end fix — the console control was built for one
   branch only.
3. **Roles and signals on the long tail.** The support office cannot record
   a withdrawal or an absence decision at the panel stages (SL2-48, SL2-15);
   reversals of a communicated outcome are silent (SL2-32); failed final
   notices raise no bell (SL2-33).

Recommended order: the nine Critical items first (SL2-01, 05, 06, 07, 08, 09,
12, 13, 45 — all small, mechanical, and test-coverable); then the
client-dependent Majors once §6 is answered, grouping by surface (vacancy
lifecycle; interview/attendance; CAR/AA; rescind/reopen/cancel; COS rounds);
then the Minor cleanups (SL2-46, 47, 50, 51) as one dead-code commit.

---

## 9. Implementation status and decisions (2026-08-25)

Every finding in §3 is now resolved. Implementation ran in three phases under
the Codex workflow (written brief → implementation → independent review: diff
read, targeted classes, full suite against the known 14-test flaky baseline,
live browser/PDF checks where user-visible).

### 9.1 Findings → commits

| Brief | Commit | Findings closed |
|---|---|---|
| SL2-A | b5ff9f8 | SL2-01, SL2-03, SL2-45 |
| SL2-B (+B.1 a9b44c2) | 3b73b65 | SL2-05, SL2-06, SL2-07 |
| SL2-C | 340fc1d | SL2-08, SL2-09 |
| SL2-D | f7c5f35 | SL2-12, SL2-13 (migration 0081) |
| SL2-E | 87d179c | SL2-19, SL2-20 |
| SL2-F | 2645067 | SL2-11, SL2-44 |
| SL2-G | ebb559c | SL2-21, SL2-38, SL2-40 |
| SL2-H | 5325d86 | SL2-32 (in-app), SL2-33 |
| SL2-I | 2fb9287 | SL2-39, SL2-46, SL2-47, SL2-50, SL2-51 (migration 0082) |
| SL2-J (+J.1 5487f12, J.2 0d28fa3) | 96cb2af | SL2-10, SL2-15, SL2-25, SL2-30, SL2-34, SL2-48, SL2-49 as Office Policy switches (migration 0083) |
| SL2-K | f144794 | SL2-26, SL2-27, SL2-28 (label), lead L-15 (migration 0084) |
| SL2-L | f99fb85 | SL2-22, SL2-23, SL2-29, SL2-32 (applicant email), question 32 notice, checklist relax-only (migration 0085) |

Findings SL2-02 (shortening a published deadline), SL2-16 (rating void),
SL2-17 (fresh notice after reschedule), SL2-18 (multi-day pooled interviews),
SL2-24 (QS snapshot), SL2-35 (L1-COS validation delegation), SL2-36
(split-stage lockout), SL2-37 (second-pass re-key), SL2-41 (retention-lapse
closure), SL2-42/43 (COS console polish beyond what SL2-D shipped) remain
**open by design**: they are governed by the decisions below (several are
simply refused today, which is the safe strict state) or wait on a future
maintenance window. The §3 leads not named here remain leads.

### 9.2 The decisions (developers deciding; no client questionnaire)

The team resolved §6's questions as developers rather than sending the client
a questionnaire. Two kinds of answer:

**Office Policy switches** (one HRM Chief-edited record, every change audited,
shipped strict — flipping one is configuration, not a code change):
extension after a scheduled exam (off) · resubmission-deadline extension (on) ·
interview absences decided by (HR support office) · owning office may record
withdrawals (on) · rescission cutoff (blocked once a contract/appointment
reference is recorded; rescission also voids the completion record) ·
multi-slot COS postings (off) · Suspend/Close require a reason (on).

**Fixed rules** (built in; each is a single named guard, changeable in
minutes if practice differs): a published closing date can never be shortened;
Cancelled is terminal (re-run = new vacancy); withdrawn or vacancy-cancelled
applicants may re-apply (new case; REJECTED stays barred); the owning office
corrects applicant contact details with audit + dual notices; the AA proceeds
past a withdrawal, may record "No appointment — failure of search", and a
board may recommend none (making every subsequent pick a justified
deviation); a CAR return corrects ETE/recommendation only and says so; the
CAR records its deliberation date and members present (quorum derived, the
yes/no control retired); completion requirements carry the date actually
received; reversed outcomes send a neutral "under review" notice; the
Not-Qualified notice states document deficiencies; a live checklist may only
be relaxed.

The client receives the finished behavior plus a plain-language policy guide
(docs/office-policy-guide.md) instead of open questions.
