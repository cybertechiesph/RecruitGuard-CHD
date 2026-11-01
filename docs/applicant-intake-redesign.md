# Applicant intake redesign (November 2026)

The public application flow, rebuilt around how applicants actually apply: often on a phone,
with documents that have several pages, and rarely in one sitting.

## Flow

| Step | Page | What happens |
|---|---|---|
| 1. Your details | `/apply/entries/<id>/apply/` | Name, email, mobile, consent (+ captcha). Creates the draft and emails a code. **An existing unsubmitted draft for that email is not changed**, so a stranger can't rewrite someone else's draft before verifying. |
| 2. Verify email | `/apply/<token>/otp/` | Enter the code. This browser session can now open the draft (for up to 12 hours, and until the normal 30-minute idle timeout). The same page is the **way back in**: every code email links here, and a returning applicant clicks "Email me a code". |
| 3. Documents | `/apply/<token>/application/` | Details (editable), qualification summary, cover note and the Plantilla performance-rating question, all **saved as you type**. Each document card takes **several files or photos**, each **uploaded the moment it is picked**, with its own progress bar. A "Take a photo" button appears on phones. Live "x of y required documents ready" and a "still needed" list. **One Submit.** |
| 4. Submitted | `/apply/<token>/receipt/` | Unchanged: receipt, tracking code, case number. |

A draft link (public token) on its own never shows a draft's contents. Every page and upload
endpoint requires the verified browser session.

## Documents

- Each picked file is an `ApplicantDocumentPart`: encrypted like evidence (AES-256-GCM) and
  validated by the same upload rules (PDF, JPG or PNG; up to 5 MB; real file signature).
- **One part** is stored as-is, so an e-signed PDF keeps its signature. **Several parts** are
  combined, in order, into one PDF (images become A4 pages). Reviewers always see one file per
  requirement in the Evidence Vault, so screening, the CAR and exports are unchanged.
- **Photos:** the browser shrinks large photos before upload (long edge 2,400 px). The server
  cleans them again: it rotates them upright, shrinks them, and **removes metadata such as GPS
  location**.
- **Rules:**
  - The same file can't be added twice, or to two different documents.
  - Up to 10 files per document, and 20 MB combined.
  - Password-protected or unreadable PDFs get a plain-language error.
- **After submission:** parts are deleted, and the Evidence Vault copy is the record.

## Code

- `recruitment/applicant_documents.py`: adding, removing, combining and cleaning parts.
- `services.start_public_application_draft`, `services.save_applicant_application_answers`.
- `submit_application(..., email_verified_in_session=True)`: the verified session counts as
  proof at submit time. The email must still have been verified for the draft.
- Views in `portal_views.py`: `ApplicantApplicationView`, `ApplicantDocumentPart{Upload,Delete,Preview}View`
  and the `ApplicantDraftAccessMixin` session gate.
- Templates: `applicant_start.html`, `applicant_otp.html`, `applicant_application.html`.
  Script: `static/js/recruitguard-applicant-application.js`.
- Migration `0086_applicant_document_parts`. New dependency: **`pypdf`** (added to
  `requirements.txt`; run `pip install -r requirements.txt` on the server).

## Old one-step path removed
The original one-step submission is gone. It accepted all answers and files in one POST to
the apply URL, and saved them before checking the email. The code page's hidden `finalize`
action is gone too, so the only way in is now: details, then verify, then the application
page. The old tests were moved onto the new flow: see the `start_portal_draft` /
`apply_through_portal` helpers in `recruitment/tests.py`.

## Walkthrough improvements (November 2026)

After a full applicant walkthrough:

- **Copy:**
  - The vacancy "How to Apply", home "How it works" and Help answers now describe the new flow (tracking code, finish later, automatic photo shrinking, "Resend the code", "Start again with the correct email").
  - New FAQ: "Can I save my application and finish later?"
  - Code errors no longer say "before final submission".
- **Application page:**
  - The saved message shows once.
  - The summary and note show live character counters.
  - The performance-rating question starts **unanswered** and is only required at Submit. If another field has an error, the answer is still saved, so the rating card matches the choice.
  - Error messages link to their fields.
  - There's a "Jump to your documents" link.
  - Phones get a sticky "x of y documents ready / Submit" bar.
  - Upload errors show one per line: the file name, then a full sentence.
  - The "too large" hint suggests adding pages one at a time or photos.
- **Code page:** a wrong code now says how many tries are left.
- **Vacancy pages:**
  - Closed vacancies say so at the top and in "How to Apply".
  - Job cards and the vacancy header show the vacancy code (e.g. `VAC-PLT-2026-005`) and the posting date, so two vacancies with the same title can be told apart.
- **Receipt:** shows the finished 4-step journey bar (hidden when printed).
- **Resubmission:** each returned document accepts several files. They are combined into one PDF under the intake rules (up to 10 files, 20 MB together, no duplicates). One file is still stored as-is.
- **Track Application:**
  - A step-by-step timeline replaces the 3-step bar: Submitted → Document check → Examination → Interview → Decision.
  - An exam or interview appears only after its schedule was emailed. No scores are shown.
  - Approved applicants see their completion checklist and deadline (with items marked *Received*), but only after the office has emailed the checklist.
- **Reminder email:**
  - `python manage.py send_draft_reminders` emails each verified, unsubmitted draft once when its vacancy closes within 3 days (`--days-before`, `--dry-run`).
  - Unverified drafts are never emailed.
  - Run it daily (see README, "Scheduled jobs").
- Migration `0087_applicant_walkthrough_improvements`:
  - Adds `draft_reminder_sent_at`.
  - Allows a blank rating answer on drafts.
  - Adds the audit action "Unsubmitted Draft Reminder Sent".

**Kept on purpose:**
- **"You have already submitted an application for this position using this email address."**
  - The start page still says this. A real applicant who forgets they applied gets a clear answer instead of a confusing second draft.
  - The cost is that someone who types another person's email learns that person applied for that one position.
  - The captcha and rate limits make guessing at scale impractical.
  - Hiding it would mean a silent "check your email" for everyone, which confuses the much more common honest case.
- **Tracking code, then email, then emailed code (REV-I).** This sequence is what protects status pages from someone who only knows the tracking code.

## Portal polish (November 2026, second round)

- **Shared mobile IPs and forged headers:** see the README section "Behind nginx: client IP
  addresses".
  - Rate limits now count per browser, with a per-IP ceiling.
  - The visitor's IP is read from the right-hand end of `X-Forwarded-For`.
  - One helper, `config/client_ip.py`, serves rate limits, the password-reset throttle, the
    login throttle, captcha and the audit log.
- **Facebook / Messenger / Instagram built-in browsers:**
  - The start, application and resubmission pages show a short tip on how to open the page
    in Chrome or Safari.
  - Uploads and the camera often fail in those browsers.
- **Qualification self-check:**
  - The vacancy page turns the standards into a "Do you meet these?" checklist.
  - The standards come from the position's QS fields, or are parsed from the vacancy's
    "Education: … Training: … Experience: … Eligibility: …" text.
  - The checklist never blocks. Clicking Apply with unticked items asks for confirmation;
    nothing is sent or stored.
- **Photo quality:** after a photo uploads, the page warns if it's small (under 1,000 px),
  dark, or blurry. The photo is saved either way.
- **Deadlines:**
  - "11:59 PM (Philippine time)" is shown on Plantilla and opening-based vacancies.
  - A live countdown runs on the last day.
  - The application page shows a "Submit by…" banner in the last 3 days.
- **Resubmission reminders:** `send_resubmission_reminders`, once per deadline. A new or
  extended deadline resets it.
- **Abandoned drafts:**
  - `purge_abandoned_drafts` removes the uploaded files of drafts that were never
    submitted, 30 days after the vacancy stopped accepting applications.
  - The draft, the file names and fingerprints, and the audit trail are kept, because
    deleting the draft would delete its audit history.
- **Withdrawal requests:**
  - "Withdraw my application" on Track Application, behind the emailed code.
  - It sends a request: the handler is notified, and the case page's withdrawal card opens
    with the details filled in. Staff still record the withdrawal (SL-10), so batches and
    schedules stay under the office's control.
- **Document reuse:** each document card can offer the same applicant's (same verified
  email) document from an earlier submitted application. It is copied in through the normal
  upload checks.
- **Funnel:** the staff vacancy list shows, per vacancy, how many people started, verified
  their email, added documents and submitted. Counts only.
- **Accessibility pass:**
  - No text smaller than 12px on applicant pages.
  - Radio buttons and checkboxes are 22px.
  - Short links meet the 24px target-size minimum.
  - Labels, headings and landmarks were checked on the home, vacancy, start, code and
    application pages.
- Migration `0088_reminders_withdrawal_requests_retention`.

## Still needs people (not code)
- Test on real phones (a cheap Android on mobile data, an iPhone), including inside Facebook.
- A 5-person usability test with people who have never seen the portal.
- A screen-reader pass (TalkBack and NVDA).
- A proper sending domain with SPF, DKIM and DMARC instead of Gmail SMTP.
- Automated browser tests of the journey, e.g. Playwright. This needs a dev-only
  dependency and browser downloads, so it was left for the team to approve.
