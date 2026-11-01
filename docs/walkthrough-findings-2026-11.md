# End-to-end walkthrough findings (local, Oct 25 – Nov 1, 2026)

A full walkthrough on a local server against the demo database, acting as each role in turn
(Secretariat, HRM Chief, three HRMPSB members, Appointing Authority, applicant). Emails were
read from the console backend.

## What was covered

**COS, end to end (new vacancy VAC-COS-2026-004, Case No. 2026-0020):**
- Secretariat posts the vacancy.
- Applicant applies with 8 PDFs (file preview used), verifies the email code, gets a receipt, and tracks the status.
- Secretariat returns the PDS for correction. The resubmission deadline shows on the Schedule.
- Applicant gets the email and re-uploads on the resubmission page (blank-form downloads shown).
- Secretariat finishes screening, schedules the exam (shown in "Coming up"), and records it.
- HRM Chief does the second-level screening and exam check (two-pass Level 1 COS, rule SL-05).
- HRM Chief records the interview, opens a COS selection round, and finalizes the pick.
- Secretariat records completion and closes the case. It appears in Closed Cases (outcome filter and e-mail search both work).

**Plantilla (demo case CASE-2026-0002, VAC-PLT-2026-001), from interview to completion:**
- Secretariat creates and publishes the rating sheet, then schedules the batch interview.
- Three HRMPSB members rate the candidate; the batch is finalized.
- Secretariat prepares the CAR draft, enters ETE ratings and finalizes the CAR (3 members present).
- Appointing Authority records the final selection; the case moves to completion.

## Fixed during the walkthrough

| # | Finding | Fix |
|---|---|---|
| F1 | **Blocker:** "Finalize session" on every interview did nothing (Plantilla and COS). The handler read `maxRequiredOutputs`, which was never declared, so every click threw a ReferenceError. Broken since commit 669601f. | Declared it to mirror the server rule (Plantilla 3–5, no cap for COS), and fixed its error-message format, in `templates/internal_includes/interview_session_manager.html`. Regression test: `InterviewFinalizeButtonScriptTests`. |

## Open findings (not fixed)

### Blind review: the applicant's name reaches HRMPSB members
The case page correctly shows only "Candidate C-01", but three other places reveal the applicant:
1. **Batch detail** (`/internal/vacancies/<id>/`) lists the case number to HRMPSB members (rule RD-5 hides it on the queue).
2. **Batch interview** (`/internal/vacancies/<id>/interview/`) lists the applicant's **full name** and case number.
3. The **"Submit your rating?" dialog** shows "C-01 — *full name*".

### Workflow and data
4. **Exam results can be finalized with a future exam date.** The test result was dated two days ahead and was accepted, so a result can be recorded before the exam takes place.
5. **The exam confirmation dialog** shows the raw code `end_user_assessment`, and its "Score" line shows only the General score: no Technical score and no 60/40 weighted total.
6. **The HRM Chief's exam step** still shows "Schedule exam & invite applicant", although the Secretariat's notified schedule already counts. That invites a second notice to the applicant. The "Copy results from Secretariat review" button described in SL-05 isn't on the exam step.
7. **The applicant code lasts 10 minutes, not 3:** `.env` and `.env.example` both set `APPLICATION_OTP_VALIDITY_MINUTES=10`. The manuscript recommends 2–3 minutes.
8. **Schedule (TI-5) gaps:**
   - Plantilla deliberations never appear, because their date is stored on the CAR, not in `DeliberationRecord`.
   - A completion deadline appears only after the handler first saves the completion form.

### Content and polish
9. **Two different phone extensions:** the Help page and the PDS guide call **loc. 4477** the Public Assistance Desk; the home page and status page say **loc. 4400** (with a different mailbox). Client to confirm.
10. **Duplicated browser-tab title** "Internal Portal | Internal Portal" on the New Entry page and the Interview Rating Sheet page (no `internal_title` block).
11. **Batch page wording:** "1 applicant still need a screening decision" should be "needs".
12. **COS interview dialog** shows "Candidate Unassigned — *name*", because COS cases have no candidate code.
13. **The case page overflows sideways** at narrow widths (about 576px): 830px of content in a 561px column.
14. **The resubmission email** could link the blank form for a returned PDS or WES. The resubmission page already does.

## Test data left in the local demo database
- Vacancy VAC-COS-2026-004, Case No. 2026-0020 (closed, approved).
- CASE-2026-0002 moved to completion: a rating sheet, three panel ratings, a finalized CAR and a final selection.
- Local test sessions for six staff accounts (they expire after 30 minutes idle).
