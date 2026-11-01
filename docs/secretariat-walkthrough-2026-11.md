# Secretariat walkthrough and review (November 2026)

Scope: everything the **Secretariat** sees and does in the internal portal, for both branches,
including the handoffs to and from the HRM Chief, HRMPSB and the Appointing Authority.
No code was changed. Findings are ranked; each has steps to reproduce, the code location, and a
recommended fix.

## 0. Status (November 2026)

All six essential findings (P1-1 to P1-6) are **fixed**. Implementing them corrected some of
the findings below:
- **P1-3:** a second "Send schedule" updates each candidate's one schedule (no second record).
  The real problems were that the page never showed it, there was no confirmation, the change
  email was indistinguishable, and old invitations could be resent.
- **P1-5:** the bad PLT-001 dates came from older demo data, not from the current seed.
- **P2-1 is withdrawn:** the batch list and exam table show case numbers on purpose, under
  Dr. Ramos's panel revision to reduce selection bias.
- **P2-2 was worse than written:** "Send again" on a staff notice re-sent it to the
  **applicant's** address. This is fixed with P1-3.

What changed:
- **P1-1:** one rule (`recruitment/blinding.py`) for every page and in-app notice an HRMPSB
  member sees. A test loads each of those pages.
- **P1-2:** exam and interview results can't be finalized before the scheduled time (drafts
  can be saved). The deliberation date can't be in the future or before the last interview.
  The interview finalize now has a confirmation.
- **P1-3:**
  - The batch exam page shows what was sent.
  - Re-sending is a confirmed "Change the schedule" that emails "Exam schedule changed".
  - The case page's email box lists only applicant emails.
  - Staff notices and invitations for past events can't be resent.
- **P1-4:** the HRM Chief's COS re-check reuses the Secretariat's notified sitting (no second
  invitation) and offers "Copy scores from the earlier review". The screening button is renamed
  "Copy document checks from the Secretariat's review".
- **P1-5:** one open Plantilla posting per item number (form message plus database constraint,
  migration 0090). An identical entry from the same person within two minutes is treated as
  the same click, and Save is disabled after the first click.
- **P1-6:** a "Withdrawal requested" chip on My Queue, the batch list, and the exam and
  interview rows, plus a warning on the batch exam and interview pages.

### Second round: P2 and P3 (office decisions applied)

Office decisions used: the Secretariat's "archive" means past records (finished cases, their
files and history, past vacancies), while the system audit log stays with the System Admin.
No claim/lock marker on cases. Default time targets: 14 days at Completion, 7 days elsewhere.
Applicant email codes keep their 10-minute life. Internal pages stay in English, in plain, professional wording.

- **Archive:** a "Past Records" link in the sidebar (Secretariat and HRM Chief) opens the
  finished cases, each with its files, history and export, and links to closed vacancies.
- **P2-2 follow-up:** the batch interview schedule form only appears for the office that runs
  the interview (Level 1 Secretariat, otherwise the HRM Chief). Everyone else sees what was
  sent. Sending or changing it asks for confirmation.
- **P2-3:** a second paper sheet for the same panel member is refused unless "Replace this
  member's earlier sheet" is ticked. An invalid sheet says exactly what is wrong. Notes are
  optional.
- **P2-4:**
  - The ETE boxes appear before the first draft, so one "Prepare draft CAR" saves the scores
    and ranks the candidates.
  - Pages show the official version (finalized reports only), so a CAR prepared once is
    "Version 1" however many drafts were saved.
  - When the panel rated on paper, the deliberation summary lists the encoded sheets instead
    of "no digital ratings… check Secured Files".
- **P2-5:** the rating sheet can't be made available without a Technical competency.
- **P2-7:** the progress bar follows the branch. Plantilla: Screening, Exam, Interview,
  Comparative Assessment, Decision, Completion. COS: Screening, Exam, HRM Chief re-check
  (Level 1 only), Interview, Selection, Completion. A closed case shows where it stopped
  ("Withdrawn at Screening", "Not selected at Decision"). The editable Completion card no
  longer says "Locked".
- **P2-8:** Completion warns at 10 days and is overdue at 14; other stages keep 5 and 7. The
  badge reads "With the Secretariat for N days", since the clock covers the whole stage. The
  completion deadline defaults to two weeks from the day the case reached Completion.
- **P2-9:** Recruitment Entries has Open / Closed or cancelled / All. An active vacancy past
  its closing date shows "Applications closed (deadline passed)" and a "Close intake" button.
- **P2-10:**
  - After finalizing the interview batch you stay on the vacancy's batch page with the next
    step named.
  - After finalizing the CAR you land on Batches, told the cases are now with the Appointing
    Authority.
  - Closing a case is one button and one confirmation (the notes are in the dialog).
  - The sidebar highlights Batches for a case opened from a batch, and Past Records for a
    closed case.
- **P3:**
  - "HRMU" everywhere (no more HRMO/HRMS).
  - Grammar fixes.
  - One error per problem on the entry form.
  - Page titles on the rating sheet, entry form and weights pages.
  - Staff notices say "is now in your queue".
  - Internal dates use "Nov 1, 2026, 2:49 PM".
  - A re-uploaded document is marked "Re-uploaded" and starts unreviewed, with the earlier
    note shown for reference.

Still open: one case-number format everywhere (P3), and moving the rating sheet from a case
URL to the vacancy (P3). The proposed single home for the Secretariat (section 6) is not
built.

## 1. How this was tested

| | What | How |
|---|---|---|
| **Live (browser)** | Plantilla Level 1 case, end to end: screening → exam → interview (paper ratings) → CAR → Appointing Authority selection → completion → close. Applicant withdrawal request handled by the office. COS Level 1 case: screening → exam → HRM Chief re-check. HRMPSB and Appointing Authority views of the same cases. | Desktop size (1366×900), dark theme, synthetic staff accounts `walk_sec`, `walk_sec2`, `walk_chief`, `walk_psb`, `walk_aa`, `walk_admin` (no passwords; sessions created server-side). Each role on its own host (`sec.localhost`, `psb.localhost`, …) so sessions don't mix. |
| **Live (real views, rolled back)** | ~45 probes: direct URLs, other roles, Level 2 access, handoff/override, invalid input, past dates, out-of-order actions, double submits, repeated sends, resends. | The real views through Django's test client, inside one database transaction that is always rolled back (the demo data is unchanged). Emails went to an in-memory outbox. Script: scratchpad `walk_edges.py`. |
| **Code only** | Closure guard, notification addressing, stage clock, progress bar, exam score service, test coverage. | Reading the code paths named below. |
| **Not verified** | The HRM Chief's COS interview, deliberation and selection screens (not Secretariat work); real email delivery; two Secretariat users editing the same draft at the same moment; tablet/phone layouts of internal pages; screen-reader use; the contents of the export zip (it downloads; the files inside were not inspected). | — |

Demo data changed by the live walk (local only, reset on the next reseed):
- Case 2026-0022 now goes all the way to closed.
- Case 2026-0023 is withdrawn.
- Case 2026-0021 is at the HRM Chief's exam check.
- PLT-005 has a published rating sheet.
- Six `walk_*` staff accounts were added.
- PLT-005's closing date was moved temporarily and **restored** (Nov 15).

## 2. Verdict

The **rules underneath are sound**:
- **Routing:** Level 2 is hidden from the Secretariat. The handoff needs a reason and is audit-logged as an override, and it does not move the stage.
- **No skipping or repeating:** stages can't be skipped or repeated, and locked records stay locked.
- **Server-side checks:** dates in the past, out-of-range scores and missing documents are all rejected by the server, not just the browser.
- **Calculations:** the CAR maths is correct.
- **Clean exits:** withdrawal and Not Qualified are distinct, neutral exits.

The problems are in four places:
1. **Fairness and integrity gaps:** the blind review leaks names to the panel, and an exam or interview can be "finished" before it has happened.
2. **The batch pages don't show their own state:** after a schedule is sent the page forgets it, and sending again quietly emails everyone a second invitation.
3. **The COS two-pass rule isn't implemented as documented:** the HRM Chief is pushed to re-send the exam notice and re-type the scores.
4. **A lot of small friction:** case numbers instead of names, extra clicks, misleading labels, and generic error messages.

A rebuild of the rules is **not** needed. The Secretariat's screens would benefit from the
restructuring described in section 6.

## 3. What works and should be kept

- **Routing and access** (live, probes A1–A6, J0–J8):
  - Other roles get 404 on a case that isn't theirs; anonymous users go to the login page.
  - A Level 2 case is 404 for the Secretariat, and its files, export and completion are 403.
  - The Secretariat can't hand a Level 2 case to itself (403).
  - The HRM Chief's handoff needs a reason, is logged as `override_granted`, keeps the stage, and is undone cleanly when the case is sent back.
- **Screening** (live + B1–B8):
  - Two steps (Documents → Decide), "Mark remaining as Complied", and draft autosave.
  - Qualified is refused while a document is unreviewed, flagged, or awaiting resubmission.
  - Not Qualified requires a reason.
  - Returning needs at least one flagged document.
  - Clear confirmation dialogs, and a second finalize is refused (403).
- **Batch gating:** the exam waits until the whole pool has a screening decision and (Plantilla) until the application period ends. Withdrawing the last undecided applicant unblocks it.
- **Server-side validation:** exam and interview dates in the past, scores outside 0–100, blank candidates, and stage-skipping (CAR, completion or close while still at exam) are all refused (C2, C4, C5, D1–D4, E1).
- **CAR:** the arithmetic matches the stated weights. The named filing copy is separate from the blinded copy and its generation is audit-logged.
- **Completion:** the two-week deadline is pre-filled, and labels switch to "Contract" for COS. Closing is refused on the server until the reference, date and every requirement are resolved ([services.py:11248](../recruitment/services.py)).
- **Withdrawal:** the applicant's portal request appears pre-filled on the case. Recording it closes the case as withdrawn (not rejected), emails a confirmation, and offers "Next in this batch".
- **Audit:** submissions, routing, finalizations, schedules, views of protected records, resends and overrides are all logged.

## 4. Findings

Priority: **P1** must fix (integrity, fairness, policy, data quality) · **P2** should fix (clarity, wasted
steps, misleading UI) · **P3** polish.

### P1 — Essential

**P1-1. The blind review leaks applicants' identities to HRMPSB members (three places).**
- Repro (as an HRMPSB member, PLT-005 at interview):
  1. `/internal/vacancies/221/` lists `CASE-2026-0022`.
  2. `/internal/vacancies/221/interview/` lists **"Carlo Walkthrough"** and the case number.
  3. On the case page (header correctly says "Candidate C-07"), the "Submit your rating?" confirmation reads **"C-07 — Carlo Walkthrough"**. That reveals the name at the exact moment of scoring. This one was not on the earlier list.
- Code:
  - [interview_rater.html:337](../templates/internal_includes/interview_rater.html) and [interview_session_manager.html:336](../templates/internal_includes/interview_session_manager.html) (`applicant_display_name` in the dialog).
  - [vacancy_batch_interview.html:84](../recruitment/templates/recruitment/vacancy_batch_interview.html) and line 174.
  - The batch detail row.
- Why it slipped: the only test is [tests.py:11031](../recruitment/tests.py), which checks the HRMPSB **queue** page and nothing else.
- Fix:
  - One helper, `display_identity(application, viewer)`, that returns the candidate code for HRMPSB and the name for everyone else. Use it everywhere.
  - A test that loads every page an HRMPSB member can reach and asserts the name and case number are absent.

**P1-2. Exams and interviews can be finalized before they take place; dates aren't cross-checked.**
- Repro:
  1. PLT-005: schedule the exam for tomorrow 9:00, enter scores, Finalize → accepted, the case moves to HRMPSB.
  2. Interview scheduled Nov 4, finalized on Nov 2 → accepted, with **no confirmation dialog**.
  3. CAR finalize accepted a deliberation date (Nov 2) earlier than the interview (Nov 4).
  4. The same happened on the COS exam (scheduled Nov 5).
  5. Probe C6 confirms on a fresh vacancy.
- Code: [`save_vacancy_exam_scores`, services.py:6225](../recruitment/services.py) never compares the schedule with today. The interview batch finalize and the CAR deliberation date have no equivalent check.
- Fix:
  - Refuse "finalize" until the scheduled time has passed. Allow saving scores early as a draft.
  - Require the deliberation date to be on or after the interview date.
  - Add the missing confirmation to the interview finalize.
  - Show "Exam is on Nov 3, 9:00 — scores can be finalized after that" instead of the button.

**P1-3. Sending a batch schedule twice creates a second schedule and a second, identical invitation; the page never shows the current schedule.**
- Repro:
  1. After "Send schedule to batch" the exam page's form comes back blank. The date, time and venue just sent appear nowhere on the page.
  2. Probe C3: sending again with a different time and room → **4 notices for 2 candidates and 2 schedule records**. Both emails have the same subject ("exam invitation"), so applicants can't tell which one is current.
  3. Probe H2: "Send again" on an exam invitation is still allowed after the exam was finalized.
  4. No confirmation before emailing the whole batch.
- Code:
  - The `save_vacancy_exam_schedule` path ([views.py, `VacancyBatchExamView.post`, `save_schedule`](../recruitment/views.py)) has no "already scheduled" branch.
  - The notification resend has no check for events that have already happened.
- Fix:
  - Show the current schedule at the top of the page.
  - Change the button to "Change schedule" once one exists. It should confirm "This emails N candidates a changed schedule", send a "Schedule changed" email, and replace the schedule, not add a second one.
  - Block resending invitations for finished events.
  - The interview page already shows each candidate's scheduled time; the exam page should match.

**P1-4. The COS two-pass rule (Level 1) is not implemented as documented.**
- What the rules promise ([workflow-rules.md](workflow-rules.md), "Level 1 COS is two-pass"): "One sitting, one schedule, one notice", and a "Copy results from Secretariat review" button that pre-fills the Chief's exam form.
- What happens (live, case 2026-0021):
  1. At the Chief's exam step the card says "Schedule: **Not yet scheduled**", although the Secretariat scheduled Nov 5 and the invitation was sent.
  2. The Chief's batch exam page says "Send the exam schedule to the batch before finalizing". That forces a **second invitation** for the same sitting.
  3. There's no copy button on the exam step, so the Chief has to re-type the scores.
  4. On screening, "Copy results" copies the document statuses but not the Qualified decision. That may be intentional, as the Chief records an independent finding, but the label should say so.
- Fix:
  - The Chief's exam step reads the Secretariat's notified schedule as satisfying the "schedule first" guard.
  - Add the copy button to the exam step, pre-filling an editable form that is never auto-accepted.
  - Rename the screening button to "Copy document checks".

**P1-5. Vacancy data quality: duplicate Plantilla items and duplicate entries.**
- Repro (rolled back):
  1. Creating a Plantilla entry with item number `WALK-TEST-0001` while PLT-005 is active with the same item → accepted (two active postings for one item).
  2. Submitting the new-entry form twice → **two entries**.
- Also: the demo seed creates PLT-001 with closing Jul 25 before opening Jul 26. The form would refuse that, so it's a seed problem only.
- Fix:
  - A database constraint so only one active or suspended Plantilla posting can exist per item number.
  - Disable the submit button after the first click, and reject an identical entry made within a minute.
  - Fix the seed's dates.

**P1-6. An applicant's withdrawal request is invisible until someone opens that case.**
- Repro: case 2026-0023 had a pending portal request. My Queue, the batch page and the vacancy list show nothing. Only the case page shows it (pre-filled and open, which is good).
- Risk: the Secretariat keeps screening, scheduling and emailing someone who has asked to leave.
- Code: the request sets `withdrawal_requested_at` and sends a `WITHDRAWAL_REQUESTED` notice ([services.py:2938](../recruitment/services.py)), but no list shows it.
- Fix: a "Withdrawal requested" chip on queue and batch rows (like "Documents to fix" in the applicant portal), sorted to the top, and a confirmation before sending that applicant any schedule.

### P2 — Important

**P2-1. Batch and exam pages identify applicants by case number only.**
- The Secretariat is allowed to see names, and exam lists and paper score sheets carry names. But the batch page and the exam score table show only `CASE-2026-00xx`.
- Fix: name plus case number for the Secretariat and HRM Chief (blinded for HRMPSB per P1-1), plus status chips (resubmitted, withdrawal requested, absent, overdue).

**P2-2. The case page's "Notifications sent" box mixes applicant and staff emails.**
- It says "every email queued to this applicant", but lists six internal "Interview Session Scheduled" notices to panel members, each with "Send again".
- Probe H1: "Send again" re-sent a staff notice to an HRMPSB member.
- Fix: show only applicant emails there, and keep staff notices in the case history.

**P2-3. Paper rating sheets: the real error is hidden, and re-entering silently replaces a score.**
- Any invalid entry gives the same "Name the panel member, record their score, and attach the scanned sheet". The real causes (justification needed below 75 or above 98, score over 100, not a panel member) are swallowed ([views.py:3729](../recruitment/views.py)).
- Re-entering the same member's sheet replaces their earlier score with a plain success message (probe E3: 88 → 85, one record).
- "Upload Remarks" is required for no stated reason.
- Fix:
  - Show the form's own errors.
  - When replacing, say "This replaces Paolo Panel's earlier sheet (88)" and ask to confirm.
  - Make remarks optional.

**P2-4. Preparing the CAR takes extra steps, and its messages mislead.**
- Order: the page says to enter ETE ratings first, but the ETE fields only appear after "Prepare draft CAR". So it takes Prepare → type ETE → Update → Finalize.
- Each Update creates a new version, so the Appointing Authority saw "**CAR v3**" for a CAR prepared once.
- With three paper ratings recorded, the card still says "no digital ratings were submitted. **Check Secured Files**". The Secretariat has no Secured Files access (403).
- Labels disagree: the header says "Assigned to HRMPSB Member · In CAR" while the Secretariat prepares it and the progress bar says "Deliberation".
- Fix:
  - Show the ETE fields straight away, recalculating as you type.
  - Version only at finalize (or on an explicit "Save version").
  - Count encoded paper sheets as ratings in that message.
  - Use one name for the step.

**P2-5. The rating sheet can be published without Technical competencies.** The page says to add them first. Fix: require at least one Technical competency before "make available".

**P2-6. Bulk emails and irreversible actions are inconsistent about confirmation.**
- These have no confirmation: "Send schedule to batch" (exam and interview, emails everyone) and "Finalize interview batch".
- These do (good): screening, exam finalize, CAR finalize and close.
- Fix: use one confirmation pattern for every bulk email and every finalize, saying who is affected.

**P2-7. The progress bar is one fixed list for both branches.** `PIPELINE_STAGES` ([recruitment_ui.py:481](../recruitment/templatetags/recruitment_ui.py)) shows Screening → Exam → Interview → Deliberation → Decision → Completion for every case. So:
- A COS case shows a "Decision" (Appointing Authority) step it never takes, and hides the HRM Chief's re-check.
- A case withdrawn at screening shows "Step 6 of 6: Completion".
- The editable Completion card carries a "LOCKED" badge.

Fix: a per-branch workflow map (section 6), and an "Ended: withdrawn at screening" state.

**P2-8. The overdue and deadline indicators give wrong signals.**
- One rule for every stage: warning at 5 days, overdue at 7 ([models.py:30](../recruitment/models.py)). Completion has a two-week deadline by agreement, so every case shows "Overdue" in its second week while still within its real deadline.
- The completion deadline default is `today + 14` when the page is **viewed** ([forms.py:1225](../recruitment/forms.py)). Case 2026-0004, selected Jul 28, shows a default deadline of Nov 16; it slides forward until someone saves.
- "In Exam for 1 day" measures time since the case entered the Secretariat review stage, which covers screening and exam together. The Exam step itself was minutes old.
- Fix:
  - Per-stage targets (and use the actual deadlines where they exist).
  - Default the deadline from the selection date.
  - Measure the step, or word the label "With the Secretariat for 1 day".

**P2-9. The Recruitment Entries list hides closed vacancies and shows expired ones as "Active".**
- Closed entries are excluded entirely (`get_manageable_recruitment_entries`), so "Total entries 7" while there are 9.
- PLT-001 and PLT-003, whose closing dates have passed, still say "Active", while the applicant portal correctly treats them as closed.
- Fix: a status filter (Active / Closed / All), and a derived "Applications closed (deadline passed)" badge with a one-click "Close intake".

**P2-10. Too many places to start, and inconsistent destinations.**
- The same cases appear in Batches, My Queue and Schedule. The sidebar highlights "My Queue" on a case reached from Batches.
- After each finalize the page lands somewhere different: exam → batch interview page, interview → Batches, CAR → My Queue, screening → stays on the case.
- Closing a case takes four clicks: "Close and archive" tab → "Next: Close and archive" → "Close and archive the case" → "Yes, close and archive".
- Fix: one home (section 6). After an action, stay on the batch or case with a "Next" prompt. Close in one step with one confirmation.

### P3 — Polish

- **Case numbers** appear as both `CASE-2026-0022` (lists, page titles, notices) and "Case No. 2026-0022" (headers, applicant portal). Use one.
- **The office's name varies:** HRMU, HRMO ("COS scores are encoded by the HRMO") and HRMS ("HRMS preparation notes").
- **Grammar:** "1 applicant … still need a screening decision"; "the schedule are recorded".
- **Duplicate errors** for one problem: "COS entries may only use…" and "COS entries must use…"; a past closing date also says "Plantilla entries require a closing date".
- **Warnings shown before anything has been tried:** "Send the exam schedule… before finalizing" appears as a red alert on first load.
- **Page titles** read "Internal Portal | Internal Portal" (rating sheet page, and the earlier list).
- **The vacancy's rating sheet is reached through one case's URL** (`/applications/503/interview/rating-sheet/`). It belongs on the vacancy.
- **Staff notices** say "assigned to you" when the case goes to a shared role queue.
- **Dates and times:** "Nov 01, 2026 02:49" (zero-padded, 24-hour) on internal pages versus "Nov 1, 2026, 2:49 AM" elsewhere.
- **A resubmitted document** keeps its old "note to the applicant" on the re-review row, and isn't marked "re-uploaded".

### Questions for the office (policy, not bugs)

- **Audit access:** the Secretariat cannot open the audit log, the per-case audit page, or Secured Files (403). The use-cases list "Record, Audit, and Export Management" for the Secretariat. Export works; audit doesn't. Intended after the system-admin redesign?
- **Interview scheduling:** both the Secretariat and HRMPSB members see "Send schedule to batch" on the interview page. Should only one role schedule?
- **Level 2 vacancies:** the Secretariat can create a Level 2 vacancy (cases then route to the HRM Chief). Fine?
- **Shared queue:** two Secretariat users can work the same case at the same time (probe B5; no claim or lock exists). Is a "being handled by Sara" marker wanted?

## 5. Current vs proposed flows

**Batch exam**
```
Current:  open batch exam → fill schedule → Send (no confirm, emails all) → form blank again
          → (send again = 2nd identical invite) → type scores by CASE no. → Finalize (any time)
Proposed: open batch exam → [Schedule card: "Nov 3, 9:00, HRMU Conference Room — sent to 4"]
          → Change schedule (confirm, "schedule changed" email, replaces) → scores by name
          → Finalize unlocks after the exam time → stay on the batch with "Next: interview"
```

**CAR preparation**
```
Current:  Prepare draft CAR → ETE fields appear → type ETE → Update draft (new version each time)
          → Finalize → modal (members, date, recommendation) → lands on My Queue
Proposed: ETE fields shown at once, scores recompute live → Finalize → same modal
          (date must be ≥ interview) → stays on the vacancy, version 1
```

**COS Level 1, HRM Chief re-check**
```
Current:  Chief screening (copy statuses, re-pick outcome) → Chief exam: "Not yet scheduled",
          must send a 2nd invite and re-type scores → finalize
Proposed: Chief screening ("Copy document checks") → Chief exam shows the Secretariat's sitting
          and scores read-only + "Copy scores" → edit if they disagree → finalize (no new email)
```

**Closing a completed case**
```
Current:  tab "Close and archive" → "Next: Close and archive" → "Close and archive the case" → modal Yes
Proposed: when every requirement is resolved, one "Close case" button → one confirmation
```

## 6. How the Secretariat experience should work

**One home: "My work".**
- Grouped by vacancy, as the office works in batches. Each vacancy shows its step ("Screening 3/5 decided · exam Nov 3") and one primary button for the next batch action.
- Each case row shows the **name** (to the Secretariat), the step, and chips: *Resubmitted*, *Withdrawal requested*, *Absent*, *Overdue*.
- Schedule and the flat queue stay available as views of the same list, not as separate homes.

**Batch pages that show their own state.**
- Every batch page starts with what's already true: schedule sent, who's been notified, scores saved, who's blocking.
- Then the single next action.
- Changes are explicit ("Change schedule"), confirmed, and tell applicants what changed.

**The case page focused on the current action.**
- At the top: the one thing the Secretariat must do now on this case.
- Below that: documents, then history, with past stages folded away.
- Only applicant emails in the applicant email box.

**One workflow map per branch.**
- Write down, once per branch: the stages, who acts at each, what must be true to move on, and which dates must have passed.
- The progress bar, the guards (including the date checks), the "next action" prompt and the routing all read from it.
- This fixes P1-2, P1-4 and P2-7 at the source and stops Plantilla and COS drifting apart.

**The same standard as the applicant portal.** Plain words, every error shown at once on the field, real error messages, confirmation before anything emails many people or can't be undone, and one name for each thing (case number, office, step).

Preserved throughout: Level 1/Level 2 routing with the audited handoff, stage locking and controlled
reopen, the blinded panel view, the named filing copy, the screening and closure guards, and the
audit trail.

## 7. Essential vs optional

| Do first (P1) | Next (P2) | Later / optional |
|---|---|---|
| P1-1 blind review leaks + test | P2-1 names on batch pages | P3 polish list |
| P1-2 date-aware finalize | P2-2 notification box | "My work" home (section 6) |
| P1-3 schedule state + change flow | P2-3 paper-rating errors | Per-branch workflow map |
| P1-4 COS two-pass as documented | P2-4 CAR steps and messages | Case page redesign |
| P1-5 item-number constraint, double submit | P2-5 rating sheet check | Claim/lock marker (if wanted) |
| P1-6 withdrawal-request visibility | P2-6 to P2-10 | |
