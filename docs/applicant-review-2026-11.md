# Applicant walkthrough — November 2026

The fifth role review, after the Secretariat, HRM Chief, HRMPSB Member and Appointing
Authority walkthroughs. The public portal (`/apply/`) was walked as a first-time applicant on a
local server against the demo database, then the office side was driven so the applicant's view
of every later step could be read. Review only; nothing in the code was changed.

## Status: review written (Round E brief in section 8, not implemented)

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | As a new applicant on `applicant.localhost:8060`: landing page, Help, a vacancy page, the start form, the code email, the application page (answers saved, eight documents uploaded, one as two parts, one duplicate refused), Submit, the receipt page and PDF, My Applications, the status page. Then for real: the Secretariat returned the PDS, the applicant read the email and the status page, staged a corrected PDS and pressed Resubmit; the office's notice was read. Phone width on the status and vacancy pages; every main page in Filipino. | Browser pane, with uploads sent through the applicant's own browser session. The office acted on `sec.localhost:8060` (session minted server-side). Emails read from the console backend. |
| **Probed (rolled back)** | ~30 wrong moves: a stranger holding the links (status, receipt, receipt PDF, resubmit, application, hub open, withdraw); my browser against another applicant's case (withdraw, hub open, preview, reuse); upload after submit; same email applying twice; bad name, email, phone; no consent; a closed and an unknown vacancy; wrong code or wrong email on Track Application; unknown and known email on My Applications; six wrong codes in a row; fourteen lookups in a row. | `walk_harness.py` (scratch) and a browser script against the live server. |
| **Journey (rolled back)** | The rest of the applicant's road, each branch inside a transaction that was rolled back: screening passed and an exam scheduled, exam scored and passed, an interview scheduled; not qualified at screening; a withdrawal requested by the applicant and recorded by the office; a vacancy cancellation (refused by the fixture, see section 7). The status page and every email were captured at each step. | `app_journey.py`, `app_journey2.py` (scratch) with the in-memory email backend. |
| **Code** | `portal_views.py` (start, code, application, receipt, status, hub, withdrawal, resubmit), `applicant_labels.py`, `applicant_hub.py`, `services/intake.py` (draft start, resubmission), `config/middleware.py` (rate limit), the applicant templates and emails. | Read. |
| **Suite** | 844 tests green at `cfa89e7`. | Run before the walk. |

**Fixture left in place** (local demo database only): CASE-2026-0050, Zenaida Quiambao
(synthetic, `zenaida.walk@example.com`), on VAC-PLT-2026-005, at the Secretariat's step with a
resubmitted PDS and screening not yet finalized. Two new HRMPSB notices and the Secretariat's
"resubmitted" notice. Nothing else changed.

## 1. Verdict

The portal is in good shape. The happy path is short and clear: details, one code, documents
saved as they are added, one Submit, a receipt, and a status page that says what to do next.
Every door a stranger or another applicant could try is shut, the code is locked after five
wrong tries, lookups do not reveal whether an email or code exists, and the Filipino version is
complete. The two earlier November walkthroughs (docs/applicant-intake-redesign.md) did their
job.

What this walk adds:

- **One privacy gap.** The receipt page and its PDF open for anyone holding the link, with the
  tracking code, the email, the applicant's name (PDF) and the list of files. The status page,
  which holds less, asks for a code. (P2-1)
- **One misleading timeline.** When the exam is scheduled, the status page ticks "Examination
  (done)" for a date three days ahead and keeps "Document check (in progress)", while the email
  just said "You passed the initial screening". (P2-2)
- **One decision for the office.** A resubmission gets a green message on screen and nothing in
  the inbox. (P2-3)
- Small things: a stale "Withdrawal requested" card after the withdrawal is recorded, a subject
  line that says CASE-2026-0050 while the body says Case No. 2026-0050, an intake-worded error on
  the status page, and the page language tag that stays English in Filipino.

No P1.

## 2. What is solid (keep)

- **Verify first, then upload.** A typo in the email is caught before any document is sent. The
  start form refuses digits in names, non-Philippine mobile numbers and a missing consent, each
  with a plain sentence.
- **Drafts are safe.** Answers and files are saved as they are added; "Save and finish later"
  says so; coming back is one code away from My Applications or the emailed link; a token alone
  never opens a draft (a stranger with the link is sent to the code page and sees nothing).
- **Uploads.** One file at a time, each checked and saved on pick; a two-page PDS uploaded as two
  parts came out as one `personal-data-sheet.pdf`; the same file for two documents is refused
  ("Each document needs its own file"); a document can be added to or removed before Submit.
- **Submit is one step with one answer.** Missing documents are listed by name; the receipt
  shows the tracking code (marked private), the case number (for calling the office), the files
  and a PDF; the confirmation email mirrors it.
- **Status page.** A timeline, a plain status, "What you need to do", the upcoming schedule with
  venue and instructions, and one Withdraw card with a tick box and a reason. It reads well at
  phone width.
- **Return and resubmission.** The email names the document, the reviewer's instruction, the
  deadline and one link; the resubmit page shows only the returned document with "Why it was
  returned" and the same upload cards; nothing reaches the office until Resubmit; the status
  page says "Some documents were returned to you to fix" and then goes back to Under Review.
- **Outcomes.** The not-qualified email says why ("did not meet the Qualification Standards")
  and what is next; the withdrawal email says "closed as withdrawn — not as a rejection"; the
  exam and interview emails carry the schedule, venue and reporting instructions; the panel's
  copy names candidates by code only.
- **Doors.** Status, receipt (see P2-1), resubmit, application, hub open and withdraw all refuse
  a stranger or another applicant's case; upload after submit is refused (409); a closed vacancy
  hides Apply and refuses the start; an unknown vacancy is 404.
- **Codes.** Five wrong codes lock the code ("Request a new code"); resend waits 60 seconds on
  the server's clock; Track Application and My Applications answer the same way whether or not
  the email or code exists.
- **Filipino.** Landing, vacancy, start, code, Track Application, My Applications and Help read
  naturally in the office's Taglish style; the receipt PDF follows the applicant's language.
- **Office side.** The withdrawal request lands as a notice and a prefilled card ("The applicant
  asked to withdraw through the applicant portal on … Reason given: …"); the resubmission lands
  as "applicant resubmitted 1 document"; the cron jobs for reminders and purges are documented.

## 3. Findings

### P1 — Essential

None.

### P2 — Important

**P2-1. The receipt page and PDF are open to anyone with the link.** `/apply/<token>/receipt/`
and `/receipt/download/` check nothing but the token. A stranger with the link gets the tracking
code ("keep private"), the email address, the position, the submitted date and every file name;
the PDF adds the applicant's full name. The status page, which holds less, asks for an emailed
code (REV-I), and the code email's "Continue your application" button lands on this receipt
once the application is submitted. Links leak: a shared phone, a forwarded email, browser
history. The tracking code is only half of a lookup (the code step still stands), so this is
not a takeover, but it is the one page where the portal's own rule ("treat it like a password")
is not kept.
*Fix:* gate the receipt page and the PDF behind the same "this browser verified recently" check
the status page uses. The submitting browser already has it (verification signs the browser
into My Applications), so the applicant who just submitted sees no extra step; anyone else gets
the code step, as on the status page. My Applications already offers "Receipt (PDF)" to a
verified browser, so nothing is lost.

**P2-2. The timeline ticks a future exam as done and keeps "Document check" open.** Once the
Secretariat schedules the exam (the case stays at the screening stage until the scores are in),
the status page shows "✓ Document check (in progress)… ✓ Examination (done) November 6, 2026"
for an exam three days away, with the real schedule further down under "Your upcoming
schedule". The email the applicant just received says "You passed the initial screening … are
invited to take the examination", and the chip still says "Under Review". The cause is
`_build_applicant_timeline`: a schedule that is not past gets "current" only when the status is
HRMPSB review; at the screening status it falls through to "done".
*Fix:* when an announced schedule is in the future, mark the step "current" whatever the status,
mark "Document check" done, and read the chip from that (e.g. "Exam scheduled", "Interview
scheduled") instead of from the status alone. The same rule keeps "Under Evaluation — you may be
contacted for an examination or interview schedule" from showing after the interview is already
booked (see P3-5).

**P2-3. Decision: no email after a resubmission.** After Resubmit the applicant sees "Your
documents have been resubmitted for review" and the status page returns to Under Review; the
office gets an in-app notice. Nothing reaches the inbox. Applicants on phones close the page,
and the only proof they have is the earlier "resubmission needed" email.
*Options:* (a) a short "We received your corrected documents" email with the case number and the
status link, in the applicant's language (recommended; the templates and the language decorator
exist); (b) leave it, since the status page is the record. Recommend (a).

### P3 — Polish

**P3-1. "Withdrawal requested" stays after the withdrawal is recorded.** With the status already
Withdrawn, the page still shows "You asked to withdraw on November 3, 2026. The recruitment
office will record it and email you." The card tests only `withdrawal_requested_at`.
*Fix:* hide it once the status is Withdrawn (or anything final).

**P3-2. Subject and body disagree on the case number.** The confirmation email's subject is
"… application received: CASE-2026-0050"; its body, the receipt and every other email say "Case
No. 2026-0050". *Fix:* use the applicant-facing form in the subject.

**P3-3. An intake-worded error on the status page.** When the code email cannot be sent on a
status link, the page says "Your application draft and uploaded files were saved, but we could
not send the verification code right now" — for a submitted application with no draft. The
message is the intake delivery error reused by every code flow. *Fix:* a neutral delivery
message ("We could not send the code right now. Please try again in a few moments.") for the
status, hub and resubmit flows.

**P3-4. The page language tag stays English in Filipino.** `base.html` writes
`lang="{{ CURRENT_LANGUAGE|default:'en' }}"` but nothing sets that variable, so every Filipino
page is `<html lang="en">`: screen readers voice Filipino text with English rules, and
translation tools misread the page. *Fix:* use `{% get_current_language %}` from the i18n tag
library (or set the variable from the locale middleware).

**P3-5. Two copy slips.** "Under Evaluation — You may be contacted for an examination or
interview schedule" shows after the exam is done and the interview is booked (fold into P2-2).
The resubmit page's only heading reads "Step 2 — Re-upload the requested documents" when the
code step was skipped for a verified browser.

**P3-6. Track Application lookups sit under the default rate limit.** The "otp" rule (10 a
minute) applies when the path contains "otp"; the Track Application form posts to
`/apply/status/`, so guessing tracking codes is bounded only by the default 300 a minute per
browser (and the code space, TRK-XXXX-XXXX, is large, and a hit still needs the inbox). Low
risk. *Fix:* put the status lookup and hub code posts under the "otp" rule.

## 4. The eleven perspectives (applicant-specific)

**QA.** Happy path end to end, resubmission end to end, three outcome branches (exam and
interview, not qualified, withdrawn). Of ~30 wrong moves, none went through.

**UI/UX.** The start, code and application pages are the best public pages of the system. The
status timeline is the one weak spot (P2-2); the receipt page is complete.

**Business analysis / workflow.** The applicant's road matches the office's: verify, document
check, exam, interview, decision, with returns and withdrawals handled as requests to the office
rather than self-service changes. The 14-day resubmission deadline, the reminder two days before
it, and the draft reminder three days before closing are all in place.

**Security.** Token alone opens nothing but the receipt (P2-1). Code lock after five tries,
server-side resend cooldown, same answer for unknown and known lookups, per-browser rate limits
with a per-IP ceiling for shared mobile networks, uploads parsed and re-encrypted, duplicate
files refused, no upload after submit.

**Privacy.** Consent at the point of collection with a link to the notice; the landing band
states purpose, recipients and rights; the panel sees codes, not names; emails carry the case
number, not the tracking code, for office contact. The receipt is the exception (P2-1).

**Accessibility.** Labels, ARIA landmarks, a text-size control, visible focus, dates spelled
out with "(Philippine time)". The language tag (P3-4) is the gap; a screen-reader pass is still
owed (section 7).

**Plain language and Filipino.** Short sentences, no workflow jargon ("Document check", "What
you need to do"); the Filipino is the agreed Taglish. One intake-worded error (P3-3).

**Email and notifications.** Nine emails read in this walk; all branded for the office, all
with the case number, contact block and privacy line; the code email never links to a submitted
application's documents. Missing: a resubmission receipt (P2-3). Subject slip (P3-2).

**Mobile.** Status and vacancy pages at 375 px: one column, large tap targets, no horizontal
scroll. The application page was checked at phone width in the earlier walkthroughs.

**DevOps.** Four daily commands documented in the README (deadline alerts, draft reminders,
resubmission reminders, draft purge). The droplet must set
`RATE_LIMIT_TRUST_X_FORWARDED_FOR=True` behind nginx or every visitor shares one limit (noted in
the README). Rate-limit coverage of the lookup form (P3-6).

**Data and audit.** Every applicant action (start, verify, upload, submit, resubmit, withdrawal
request) lands in the audit trail with the case; the withdrawal is recorded by the office with
the request attached, never by the applicant.

## 5. Current vs proposed flows

**Receipt (P2-1).** Now: anyone with the link sees the receipt and downloads the PDF. Proposed:
the browser that submitted sees it at once (it is already verified); any other browser gets the
code step first, exactly as on the status page.

**Exam scheduled (P2-2).** Now: "Document check (in progress)" + "✓ Examination (done)" with a
future date + chip "Under Review". Proposed: "✓ Document check (done)" + "Examination (upcoming)
Friday, November 6 at 4:25 PM, HRMU Conference Room" + chip "Exam scheduled"; after the exam,
"Examination (done)"; when the interview is booked, "Interview (upcoming)…" + chip "Interview
scheduled".

**Resubmission (P2-3, option a).** Now: on-screen message only. Proposed: the same message plus
one email, "We received your corrected documents for Administrative Aide VI (Case No. …). The
recruitment team will continue the review."

## 6. Essential vs optional

**Essential (Round E, first):** P2-1, P2-2.
**Important (Round E):** P2-3 once decided, P3-1, P3-2, P3-3, P3-4.
**Optional:** P3-5, P3-6.

## 7. What remains unverified

- Real phones and the Facebook in-app browser, camera capture, photo shrinking, the old-PDS
  warning and document reuse across applications (code paths exist; not exercised here).
- Real email delivery (SMTP, SPF/DKIM) and how Gmail renders the HTML.
- The COS apply path (this walk applied to a Plantilla vacancy; COS differs in documents and in
  the office's steps). The earlier October walkthrough covered COS end to end.
- A vacancy cancellation email: VAC-PLT-2026-005 refused the cancellation because a selection
  from an earlier walk still stands on it (the C3 guard working), so the "Vacancy Cancelled"
  status and email were not seen. The fixture itself is odd — an Active vacancy with a standing
  selection still accepting applications — but it was made by hand for earlier walks, not by the
  office's flow.
- Exam absence and the applicant's letter-of-intent path; the draft and resubmission reminder
  commands (documented, not run).
- A screen-reader pass (TalkBack, NVDA) and a usability test with real applicants, as the
  redesign document already lists.

## 8. Round E brief (for the implementer)

Test-first, one commit, no model changes expected.

1. **Receipt gate (P2-1).** In `ApplicantReceiptView` and `ApplicantReceiptPdfView`, require
   `_status_otp_verified_recently(request, application)`; otherwise render the status code step
   (reuse `_build_status_otp_step_context`, issuing a code as the status link does) and come
   back to the receipt after verification. Make `_grant_draft_access` / the submit view mark the
   submitting browser as status-verified so the just-submitted applicant sees no code step. Keep
   My Applications' "Receipt (PDF)" working. Tests: a stranger with the link gets the code step
   on both URLs and no tracking code in the body; the submitting browser and a verified hub
   browser get the receipt.
2. **Timeline (P2-2, P3-5).** In `_build_applicant_timeline`, an announced schedule in the future
   is "current" at any status, and "Document check" is done once such a schedule exists or the
   status is past screening. Add a derived chip in `_build_applicant_status_context` ("Exam
   scheduled" / "Interview scheduled" with the matching sentence) when a future announced
   schedule exists, falling back to the status label otherwise. Tests: exam scheduled at the
   screening status shows Document check done, Examination current, chip "Exam scheduled";
   interview booked shows Interview current and no "may be contacted for an examination" text.
3. **Resubmission email (P2-3, if (a)).** A `RESUBMISSION_RECEIVED` applicant email (subject
   "… corrected documents received: <position>"), queued from `submit_document_resubmission`,
   in the applicant's language, with the case number and the status link; Filipino strings via
   `update_translations`. Test: one email to the applicant after a resubmission.
4. **Withdrawal card (P3-1).** Hide the "Withdrawal requested" card when the application status
   is final. Test: recorded withdrawal shows "Withdrawn" without the request card.
5. **Subject (P3-2).** Use "Case No. 2026-0050" in the confirmation subject. Test: subject text.
6. **Delivery error wording (P3-3).** A neutral message for the status, hub and resubmit code
   flows. Test: the status link with a failing backend shows the neutral text.
7. **Language tag (P3-4).** `{% get_current_language as CURRENT_LANGUAGE %}` in `base.html` (or
   the locale middleware sets it). Test: a Filipino page has `<html lang="fil">`.
8. **Rate-limit rule (P3-6, optional).** Classify `/apply/status/` and `/apply/my/` POSTs under
   "otp". Test: the eleventh lookup in a minute is refused.

Then: run `update_translations --check`, the applicant tests and the full suite; walk P2-1 and
P2-2 in the browser on the local demo database.
