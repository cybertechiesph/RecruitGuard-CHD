# Appointing Authority walkthrough — November 2026

A review of the portal as the Appointing Authority uses it, written 2026-11-03 against
`6688810` (Round C). Same method as the Secretariat, HRM Chief and HRMPSB Member reviews; shared
pages those settled are referenced, not repeated. Findings are graded P1 (essential), P2
(important), P3 (polish); section 8 is the Round D brief.

## Status: Round D done (P2-1 to P2-4, P3-1, P3-3)

Decisions taken: P2-3 option (b), the Director's page does not record withdrawals; P2-4 yes,
the panel is told the outcome (candidate code only). P3-2 (the Director's calendar) left for the
office. See section 9.

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | Two Plantilla Level 1 vacancies at the Authority's step with a finalized CAR: VAC-PLT-2026-903 (left by the HRMPSB walk, untouched here) and VAC-PLT-2026-905 (3 applicants, brought through screening, exam, three panel ratings each, interview finalize, CAR draft and finalize with the top candidate recommended). As `walk_aa`: landing page, My Queue, both batch pages, both decision pages, Schedule, Notifications, Past Records, a closed case, the CAR print, the export; phone width. Then for real on 905: **selected** the recommended candidate through the form and its confirmation dialog, read the result from the Authority's, the Secretariat's, the panel's and the applicants' sides, then **rescinded** it through the form and read the result again. | Browser pane on `aa.localhost:8060` (session minted server-side). Other roles through Django's test client against the live Postgres demo database. |
| **Probed (rolled back)** | ~30 wrong moves: selection without notes, against the board's pick without a justification, with a justification, with an item of another CAR; no appointment without and with a reason; CAR return without and with a reason; a replacement selection after the rescission, with and without a justification; rescission without a reason, by the Secretariat, and after an appointment reference exists; posting to screening, exam, interview, CAR, handoff, withdrawal, completion, reopen, evidence; the Secretariat and the Chief recording a selection; cases at the HRMPSB step, a COS case, a closed case. | `walk_harness.py` (scratch): each request in a transaction that is rolled back. Results quoted below are the server's responses. |
| **Code** | `services/selection.py` (selection, no appointment, CAR return, rescission and its cutoff), `views/selection.py`, `forms.py` (`FinalSelectionForm`), `services/access.py`, the decision, locked-decision and rescind templates. | Read. |
| **Suite** | 832 tests green at `6688810`. | Run before the walk. |

**Fixture left in place** (local demo database only): 905's three cases are back at the
Authority's step with one rescinded selection in their history; 903 is still at the Authority's
step, undecided. The older demo cases of VAC-PLT-2026-002 (a closed vacancy) also sit in the
Authority's queue; they come from the demo seed, not from this walk.

**Not verified** (section 7): real email delivery; the Level 2 path (the HRM Chief following the
Authority's decision was checked in the Chief review); no appointment and the CAR return were
probed through the views but not walked in the browser; print-to-PDF of the CAR print.

## 1. Verdict

The Authority's portal does its one job well. The decision page puts the whole finalized CAR
in front of the Director: every candidate's document review, exam, interview average,
preliminary score and rank, the board's recommendation flagged, the top-five rule explained,
deep selection locking itself on for a pick outside the top five, a note demanded when the pick
departs from the board's, and a confirmation that repeats the appointee, rank and deep-selection
answer before anything is written. No appointment and the CAR return each need a reason and do
exactly what their copy says. The rescission is the strongest piece: the original record is
preserved, every affected case comes back, every applicant is told the selection is under
review, and once an appointment reference exists the door closes with the right words.

Nothing is broken. What is missing is the Authority's own view of what it did: after
recording the selection, the appointee's page shows the Director no record of the decision
(appointee, rank, notes, who decided) and tells them completion "will open after a final
decision is recorded", which they have just done (P2-1). The rescission, which unwinds that
decision and emails three people, has no confirmation step while the selection has one (P2-2).
Two decisions for the office: whether the Director's page should offer to record an applicant's
withdrawal (P2-3), and whether the board should hear that its vacancy was decided (P2-4).

## 2. What is solid (keep)

- **The decision page** (section 1 above). The deviation note appears the moment a candidate
  other than the board's pick is chosen; deep selection cannot be unchecked while a candidate
  outside the top five is selected; the server enforces all of it again.
- **Selection effects.** The appointee goes to completion with the Secretariat, who is told
  "in your queue for completion"; the others close as "Not Selected from CAR" with the CAR
  record and outcome on their page; the applicants receive the selected and not-selected
  emails; the Authority lands on the appointee's case with a clear message.
- **No appointment.** Reason required; closes every candidate as not selected with a
  vacancy-level audit row.
- **CAR return.** Reason required; marks the report returned, reopens only the ETE and the
  recommendation (exam and interview scores stay), sends all cases back to the HRMPSB step.
- **Rescission.** Reason required; the original record kept exactly as recorded and shown as
  "Rescinded selection" with its reason; the appointee comes back from completion and the
  not-selected cases reopen; all three applicants get "selection under review"; the
  Secretariat and the Chief get 403. The cutoff works: with an appointment reference recorded,
  the button is gone, "The appointment has been issued. A revocation at this point is handled
  as a separation, not a rescission." is shown, and the request is refused.
- **The replacement selection** chains to the rescinded one (`supersedes`) and still demands
  the justification when it departs from the board's pick.
- **The office's doors are closed:** screening, exam, interview, CAR, handoff, completion,
  evidence upload refused; reopen answers "Only the HRM Chief can reopen a finalized case."
- **Scope.** Cases at the HRMPSB step and COS cases answer 404; a decided vacancy's batch page
  redirects with "Every case you handled for this vacancy is closed… look them up under
  Closed cases"; audit log, evidence vault, Office Policy and Recruitment Entries are 403.
- **Records.** Export works from the case; the CAR print opens from the case; Past Records
  lists every finished case with names (the Authority is not blind) and filters.
- Phone width is fine.

## 3. Findings

### P1 — Essential

None.

### P2 — Important

**P2-1. After selecting, the Authority cannot see its own decision on the appointee's case.**
The decision section is rendered only while the case is at the decision step; once the
appointee moves to completion the page shows the Rescind card, the CAR summary and an empty
state reading "Completion not yet available — This step will open after a final decision is
recorded." The selection record (appointee, rank, deep selection, notes, decided by, when) is
shown on the not-selected cases' pages but not on the appointee's, to anyone. *Fix:* on the
appointee's case at the completion and closed stages, render the final-selection record for
every viewer who can open the case (the `final_selection_locked_body` already exists), and give
the empty state a true sentence for viewers who do not handle completion: "Completion tracking
is with the Secretariat." *Test:* the Authority opens the appointee's case after selecting;
the response contains the appointee, the rank and the decision notes, and not "will open
after a final decision".

**P2-2. Rescission has no confirmation.** "Rescind the selection" posts at once; the selection
has a dialog that repeats the appointee, the rank and the deep-selection answer. A rescission
unwinds that decision, reopens three cases and emails three applicants. *Fix:* the same
dialog, saying what it unwinds ("3 cases return to the Appointing Authority step; every
applicant is told the selection is under review").

**P2-3. Decision: "Record an applicant withdrawal" on the Director's page.** The Authority is
the assigned handler at the decision step, so SL-10 offers the form and a POST closes the case
as withdrawn (probe, rolled back). Office Policy item 4 (ON by default) already lets the owning
office record it at this step. *Options:* (a) keep; (b) the Director's page does not offer it,
and the owning office records withdrawals at the decision step whatever the switch says, as
Round C did for the panel. Recommend (b): the letter reaches the HR office, not the Director,
and the Director's page should hold the decision and nothing else.

**P2-4. Decision: tell the board the vacancy was decided.** Round C added the "CAR finalized"
notice; after that the panel hears nothing. On selection the applicants, the Secretariat and
the audit log are told; the board that ranked the candidates is not. *Recommend:* one in-app
notice to the vacancy's panel on selection and on no appointment, with the candidate code only
("Administrative Aide VI (VAC-PLT-2026-905): Candidate C-06 was selected").

### P3 — Polish

**P3-1. The CAR return answers "Next step saved."** The generic workflow-action message. Say
what happened: "CAR returned to the HRMPSB for correction; 3 cases are back at the HRMPSB step."

**P3-2. The Authority's calendar.** Schedule and the landing page's "Coming up" list exam
sittings, interviews and application and completion deadlines of the Authority's vacancies;
none is the Authority's own event. Decide whether the Director wants this office view at all;
if not, hide Schedule from the Authority's navigation.

**P3-3. A replacement selection does not say what it replaced.** The record chains to the
rescinded one, and the rescind card lists the history, but the new record itself could carry
"Replaces the selection rescinded on November 3, 2026."

**P3-4. The Secretariat's CAR-finalize message points to Recruitment Entries** for the filing
copy. Right for the Secretariat; the Authority prints the CAR from the case page ("Print CAR").
Nothing to change; noted so nobody reads it as a gap.

## 4. The eleven perspectives (Authority-specific)

**QA.** Happy paths pass: selection, rescission, replacement selection; no appointment and the
CAR return through the views. Of ~30 wrong moves, none went through that should not.

**UI/UX.** The decision page is the best single page of the portal. The post-decision page is
the gap (P2-1); the rescission needs the same care as the selection (P2-2).

**Business analysis / workflow.** Top five, deep selection, the board's recommendation and the
justification for departing from it are all enforced; no appointment and the CAR return exist
and work; the rescission cutoff follows Office Policy. P2-3 is a role-scope question.

**Frontend.** The deep-selection lock and the deviation note are enhancements over a form that
posts without JavaScript (the harness did it). Phone layout fine.

**Backend.** Every rule is in `services/selection.py`; the views only call it. The one
template gap (P2-1) is a section-sequence consequence, not a rule.

**Database.** `FinalSelection` keeps the rescinded record and the `supersedes` link; the CAR
snapshot is stored with the selection. Nothing to change.

**Application security.** Only the Authority records, rescinds or returns; the Secretariat and
the Chief get 403; the Authority cannot reach cases before its step (404) nor the office's
pages (403). Rescission closes at the cutoff.

**Integration.** Selected / not-selected / under-review emails to applicants, assignment notice
to the Secretariat, nothing to the panel (P2-4).

**Reporting and records.** The CAR snapshot, the decision notes and the justification are kept
with the selection; the export works; Past Records is complete.

**DevOps.** Nothing new for this role.

**Architecture.** The Authority's rules read in one module after F15. P2-1 is a case-page
sequencing matter and is the first real reason to touch the parked case-page redesign, but a
small one: show the record at the completion stage, not restructure the page.

## 5. Current vs proposed flows

**Today.** Notice "is now in your queue" → My Queue → decision page → pick, notes, confirm →
lands on the appointee's case: Rescind card, CAR summary, "Completion not yet available… after a
final decision is recorded" → (Secretariat completes) → Past Records.

**Proposed.** The same up to the landing, which then shows "Final Selection Record: Ana
Herrera, rank 1, board's recommendation, decided by Ada Authority on Nov 3" above "Completion
tracking is with the Secretariat"; Rescind asks for confirmation and says what it unwinds; the
board is told the vacancy was decided.

## 6. Essential vs optional

| Do | Items | Size |
|---|---|---|
| Should | P2-1, P2-2 | one template condition and copy; one dialog |
| Decide | P2-3 (withdrawal on the Director's page), P2-4 (notice to the board) | one rule each |
| Could | P3-1, P3-3; P3-2 is a decision | messages |

## 7. What remains unverified

Real email delivery (locmem backend); no appointment and the CAR return in the browser (probed
through the same views); the Level 2 path end to end from the Authority's side; print-to-PDF of
the CAR print; the 1366-wide layout; two Authorities (there is one) acting at once.

## 8. Round D brief (for the implementer)

Test-first, one commit per item or small group, suite green, no model changes expected.

1. **P2-1** `application_detail.html`: at the completion and closed stages include the
   final-selection record for the appointee's case; empty state for non-handlers reads
   "Completion tracking is with the Secretariat." Test as the Authority after selecting.
2. **P2-2** `final_selection_rescind_card.html`: confirmation dialog before the post.
3. **P2-3** if (b): `intake.user_can_record_withdrawal` never for `APPOINTING_AUTHORITY`; the
   owning office records at the decision step regardless of the switch. Update the Office
   Policy guide item 4 accordingly.
4. **P2-4** if wanted: a panel notice on selection and on no appointment (candidate code only),
   next to Round C's `emit_car_finalized_panel_notifications`.
5. **P3-1**, **P3-3** as time allows; **P3-2** only if the office says so.

## 9. Round D implementation status (2026-11-03)

| Item | Done | Where |
|---|---|---|
| P2-1 the appointee's case shows the decision after selecting | yes | `application_detail.html` completion section: before completion tracking starts it shows the final-selection (or COS decision) record and "Completion tracking is with the Secretariat / HRM Chief" (`completion_handler_label`) |
| P2-2 rescission is confirmed | yes | `final_selection_rescind_card.html`: confirmation dialog saying what the rescission unwinds |
| P2-3 the Director does not record withdrawals | yes (b) | `intake.user_can_record_withdrawal`: never for the HRMPSB panel or the Appointing Authority; at those two steps the owning office records it. The Office Policy switch keeps one job, the lent batch (guide item 4 and the form label updated; `WithdrawalOwningOfficeSwitchTests`) |
| P2-4 the panel is told the vacancy was decided | yes | `case_notices.emit_vacancy_decided_panel_notifications` on selection and on no appointment (new kind `vacancy_decided`, migration 0096, choices only) |
| P3-1 the CAR return says what happened | yes | `WorkflowActionView` |
| P3-2 the Director's calendar | not done | office decision |
| P3-3 a replacement selection names what it replaced | yes | `final_selection_locked_body.html` |
| P3-4 | n/a | not a gap |

Tests: `AppointingAuthorityRoundDTests` (7) and `WithdrawalOwningOfficeSwitchTests` (2) in
`recruitment/tests/test_reviews.py`; the SL-10 switch test in `test_interview.py` became
`test_the_owning_office_records_withdrawals_at_the_panel_and_decision_steps`. Five withdrawn-candidate tests
in `test_selection.py` recorded their setup withdrawal as the Appointing Authority; they now
record it as the Secretariat (the owning office), which is what the rule says. Live check on the
demo database (rolled back): selecting on 905 shows the record, "Completion tracking is with the
Secretariat", "Replaces the selection rescinded on November 3, 2026", the confirmation dialog,
and the panel receives "Administrative Aide VI (VAC-PLT-2026-905) has been decided: Candidate
C-06 was selected."; the Director's page no longer offers to record a withdrawal.
