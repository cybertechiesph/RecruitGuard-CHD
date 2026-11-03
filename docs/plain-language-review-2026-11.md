# Plain-language walkthrough — November 2026 (Round I)

The seventh walkthrough, and the first that reads both portals purely for their words. The six
role walkthroughs (Secretariat, HRM Chief, HRMPSB member, Appointing Authority, applicant,
System Administrator) each judged one role's screens for behaviour first and wording second.
This one asks a single question of every heading, button, notice, error, email and label in the
system: does a first-time reader understand it without a manual? Review only; nothing in the
code was changed.

## Status: review written; I1–I3 done, I4–I6 to do

## 0. How this was tested

| | What | How |
|---|---|---|
| **Every string** | 5,320 user-facing strings pulled out of the templates (12 applicant, 57 case-page, 40 other internal, 15 base/error, 21 email) and the Python sources (forms, models, views, services, notifications), with the markup stripped, then read in full. | `i_strings.py` (scratch). |
| **Filipino** | All 893 entries of `locale/fil/LC_MESSAGES/django.po` read side by side with their English; none empty, none fuzzy. | `po_compact.py` (scratch). |
| **Rendered in Filipino** | Every applicant page served with the language cookie set to `fil` — landing, search, vacancy, start form (blank and with errors), help, PDS guide, status lookup (blank and with a wrong code), My Applications (blank and with a bad email), a tokened status link, receipt and resubmission page — and every visible line checked against the translation file. | `fil_render.py` (scratch), rolled back. |
| **Spot checks** | Each suspected typo, stale reference or dead template confirmed in the source before it was written up. Two suspects (`"are is"`, `"a new their password"`) were template branches, not typos, and are not listed. | grep. |
| **Suite** | 900 tests green at `8654183`. | Run before the walk. |

Not rendered: the internal pages per role (read as strings instead), the PDF exports, and the
emails in Filipino (their strings were read). See section 6.

## 1. Verdict

**The applicant side is in good shape, in both languages.** Every applicant page is in plain
English, the Filipino is natural Taglish with a consistent voice ("mo / namin"), and not one
string renders in English when Filipino is selected. The office's own test guards that. The
findings there are small: a few staff words that leaked into the Help page and the vacancy
page, two error messages whose Filipino is plainer than their English, and a handful of
consistency nits.

**The internal side is readable but not yet consistent.** The live step bodies (screening,
exam, interview, CAR, completion) were rewritten during the walkthroughs and read well. What is
left is the layer underneath: the same thing is called several names on different pages, a few
notices still point at screens that no longer exist, the older "locked" panels keep their
formal Title Case voice, and the case history prints routing lines that read like a log file.
None of it stops anyone working; all of it costs a new reader time.

**And the staff side uses words no ordinary Filipino reader knows** (section 3.5): the
applicant side has none, but the Director's and Chief's screens say "rescind", the interview
screens say "encode", and forms ask for a "justification" or a "basis" where "reason" would
do. The office's own vocabulary (deliberation, quorum, CAR) stays and gets a gloss; the
lawyer's and programmer's words get replaced.

Two things need an answer from the office before the fix round (section 4): the official name
of the HR office as applicants should see it, and whether the vacancy should be called
"Vacancy" everywhere inside the portal.

## 2. What is solid (keep)

- **Applicant copy.** The start form, application page, receipt, status page, Help and PDS
  guide are written to the reader ("Everything is saved as you go", "You don't need it. Open
  My Applications…"). The upload and photo messages are exemplary ("This photo looks blurry",
  "Several pages? Add the pages one by one").
- **Filipino.** Idiomatic, not translated word for word ("Huwag magmadali. Normal na isa o
  dalawang oras ito sa unang beses."); technical nouns kept in English where Filipinos use them
  (email, document, upload, schedule); the privacy and consent text is faithful. In two places
  the Filipino is plainer than the English it translates (A-03).
- **The rewritten step bodies.** Screening ("Open each file, then set its status"), the
  no-show card ("A no-show is an outcome, not a score of zero"), the withdrawal card, the CAR
  ETE grid, completion ("Save at any time. The case can only be closed once…") and the admin
  area all explain the why in one sentence.
- **Confirmation dialogs.** Every irreversible action says what it does and what cannot be
  undone, in the same shape ("Finalize and lock" / "Keep editing").
- **Emails.** Short, one ask per email, the case number in every one, the tracking code never
  asked for.

## 3. Findings

P1 = wrong or misleading today. P2 = a reader has to guess. P3 = polish and consistency.

### 3.1 Internal portal (L)

**L-01 (P1) "RG reference" in a search box.** `application_list.html:66`, placeholder
"Name, RG reference, or position title". "RG" is the study's name, which must not appear on
any user-facing surface. Fix: "Name, case number, or position title".

**L-02 (P1) Three panels point at an "Actions tab" that no longer exists.**
`deliberation_locked_body.html:436`, `exam_locked_body.html:298`,
`interview_locked_body.html:230`: "To modify any finalized record, use Authorized Reopen in the
Actions tab." The page has had no tabs since the step-at-a-time redesign; the reopen is the
HRM Chief's card on the Completion step. The same panel also names "the Decision / Submission
Packet step", a label that exists nowhere else (the pipeline says "Decision"). Fix: "To correct
a finalized record, the HRM Chief can reopen the case from the Completion step." and "the
Decision step".

**L-03 (P1) A form points at a list by the wrong name.** `recruitment_entry_form.html`:
"Change status from the Vacancies list." The sidebar, the page title and the breadcrumb call
that page "Recruitment Entries". Resolved by L-05 whichever way the office decides.

**L-04 (P2) The HR office has eight names.** Found, with where each lives:

| Name | Where |
|---|---|
| Secretariat | the role, the sidebar, the queue, most step copy |
| HRMU / "the HRMU" | `comparative_assessment.html` ("Notes from the HRMU"), `deliberation_body.html` ×2, `deliberation_locked_body.html`, `vacancy_batch_exam.html` ("COS scores are entered by the HRMU"), `interview_session_manager.html` and `vacancy_batch_interview.html` ("end-user / HRMU interview score") |
| HRMS support role / "HRMS number" | model validation messages ("Only the assigned HRMS support role may…"), `forms.py` COS rating help |
| HR | `decision_locked_body.html` ("Coordinate with HR for the preparation…"), Office Policy help ("Chair, vice chair and HR") |
| HRM Unit | the contact-updated email (`notification_services.py`) |
| Human Resource Management Unit (HRMU) | applicant emails, Help, PDS guide, the 500 page, `email_branding.py` |
| Human Resource Management Section | the exam "administered by" choice (`models.py`) |
| Recruitment, Selection and Placement Office | the export PDF header (`evidence_vault.py`) |
| "the workflow team" | `secured_files.html` ("Contents stay with the workflow team") |

Recommendation: inside the portal, **Secretariat** everywhere a sentence names the office
(it is the role name staff sign in under; "HRMO III" stays on the filing copy because it is a
signature title). To applicants, **"the recruitment team"** in sentences and **"Human Resource
Management Unit (HRMU)"** for contact details, as the Help page already does. The office must
confirm the formal name (the mailbox is `hrms@…`, which suggests "Section") and say what
"Recruitment, Selection and Placement Office" refers to.

**L-05 (P2) The vacancy has six names.** "Recruitment Entry" (sidebar, "Recruitment Entry
Management", "Entries & Positions", "Recruitment entry created."), "vacancy" ("Cancel this
vacancy", "This vacancy's publication period…", "Used by 3 vacancies", the whole Batches area),
"position" (table headers, `position_list.html`), "posting" ("re-published as a new posting",
"To close a posting, set its status to Closed"), "entry" ("Close this recruitment entry?",
"Close the recruitment entry once the intake period is over") and, to applicants, "job
opening" / "position". Recommendation: **Vacancy** inside the portal (sidebar "Vacancies",
"New vacancy", "Vacancy created."), **position / job opening** to applicants. The entry *code*
(VAC-…) can keep its name.

**L-06 (P2) The exam outcome copy is unclear.** `exam_body.html`: "Result labels are generated
by the system for evaluation; no automatic pass / fail or weighting rule is applied unless
inside the approved scope." and "System result — For evaluation — The system records a
controlled result on save; passing is decided during evaluation." A reader cannot tell what
"approved scope" or "controlled result" means. Fix: "The system records the scores and the
overall. It does not decide pass or fail; the office does that at evaluation."

**L-07 (P2) Case Updates prints routing lines written for a log.** From `services/routing.py`
and `selection.py`: "Automatically routed to HRMPSB after Plantilla examination was finalized
by Secretariat under HRM Chief authorization.", "Examination finalized; next designated handler
is HRMPSB.", "Level 2 Secretariat authorization ended after automatic case advancement.",
"COS examination finalized; next assigned office is HRM Chief.", "Unsupported recruitment case
transition target." These are the `description` of each routing-history row, shown on every
case. Fix: one plain sentence each, in the past tense, naming the office by its role ("Exam
finalized. Sent to the HRMPSB." / "Exam finalized by the Secretariat. Back with the HRM
Chief."). The audit `action` codes and labels are unaffected.

**L-08 (P3) Two voices on one page.** The six older locked panels
(`*_locked_body.html`, `decision_locked_body.html`) use Title Case and formal nouns ("Final
Decision Record", "Packet Snapshot", "Noted Gaps", "Examination Identification", "Step Summary
at Finalization", "Deliberation & CAR Locked", "Reopen Closed Recruitment Case") while the live
bodies use sentence case and plain nouns. Same for the badge: "Read Only" (`stage_titlebar.html`)
vs "Read-only" in six other includes. Fix: sentence case throughout; "Read-only".

**L-09 (P3) Staff jargon with a plain word available.** Each with its replacement:

| Found | Where | Use |
|---|---|---|
| "candidates snapshotted" | `vacancy_batch_cos_deliberation.html` | "candidates in this round" |
| "would create a second cohort" | `forms.py` closing-date check | "would create a second batch" |
| "Scanned Fallback Rating Sheet", "fallback rating" | `forms.py`, services messages | "paper rating sheet" (the card already says so) |
| "Flat queue" | `vacancy_batches.html` | "List view" |
| "The session manager needs to set the date" / "until the session manager finalizes" | `interview_rater.html` | "the Secretariat" |
| "visible to deliberating roles" | `deliberation_panel_summary.html` | "visible to the board and the office" |
| "Voided by rescission." | `completion_body.html`, services | "No longer applies: the selection was rescinded." |
| "— the office's own CAR sheet would show #DIV/0! here." | `services/car.py` | drop the clause |
| "Complied" / "Flagged as deficient (not returned)" | screening status labels | "Accepted" / "Problem noted (not returned)" — office to confirm, "Complied" may be the CSC form's word |
| "fixed period intake mode", "open for intake", "intake period" | entry form and messages | "fixed closing date", "accepting applications", "while applications are open" |
| "Packet Snapshot", "Noted Gaps", "Evidence items" | `decision_locked_body.html` | "Records at the time of the decision", "Missing at the time", "Files" |
| "Special Authorization" / "Level 2 authorization" / override | form label, messages, audit labels | one name: "Special authorization" |
| "System Admin" | `interview_rating_sheet.html` | "System Administrator" |
| "Authorised" (login page) vs "Authorized" (everywhere else) | `login.html` | "Authorized" |
| "Do" as a section heading | `admin/home.html` | "Quick actions" |
| Slash labels: "Reason or Remarks", "Justification / Remarks", "Location / medium", "Deliberation Minutes / Record", "Ranking Notes / Justification", "Decision Notes / Remarks", "Branch / type", "Branch / Intake" | forms and panels | one noun each: "Reason", "Remarks", "Where", "Minutes", "Ranking notes", "Decision notes", "Type" |

**L-10 (P3) The sign-in pages speak security-ese.** "Internal Access", "Sign in with your
assigned internal credentials.", "Update the password for your authenticated internal
account.", "Sign in with your new password and complete internal verification.", "Identity and
Access", "Internal Verification — Check Your Email". Fix: "Staff sign-in", "Use your staff
username and password", "Change your password", "Enter the 6-digit code we emailed you". The
"activity is monitored and logged" line can stay.

**L-11 (P3) Notification type labels end in "Notification".** The Notifications sent panel
prints `get_notification_type_display`, so an applicant's interview notice shows as "Applicant
Interview Notice Notification", and the others as "Exam Invitation Notification", "Interview
Session Scheduled Notification", "Requirement Checklist Notification". Fix the choice labels
(`models.py`): "Interview notice", "Exam invitation", "Requirement checklist".

**L-12 (P3) Dead copy still in the tree.** `internal_includes/banner.html` ("Observer
Access") and `drawer.html` ("Application Detail") are included by nothing;
`recruitment/position_list.html` ("Open Recruitment Entries — Internal View", "Class ID
pending", "Intake remains open while the entry is active") is routed at `/positions/` but
linked from nowhere. Remove the two includes; redirect `/positions/` to Vacancies or remove the
route.

**L-13 (P3) "Return to Dashboard".** `forbidden.html` and `password_change_done.html`. The link
goes to the role's landing page, which is "Home" for the administrator and "Batches" for the
offices; nothing is called Dashboard. Fix: "Back to the portal".

### 3.2 Applicant portal, English (A)

**A-01 (P2) Staff words on the Help page.** `applicant_help.html`: "Requirements differ
between Plantilla and COS **entries**", "where required by the **posting**", "where
applicable to the selected **branch**". Applicants know these as "position types" and
"positions". Fix: "Requirements differ between the two position types", "when the position
requires it", "for that position type".

**A-02 (P2) The vacancy page shows catalog fields.** `applicant_vacancy_detail.html`, block
"Official reference details": "Official Position Reference", "Level Classification", "Class
ID". These are the administrator's catalog fields; an applicant needs none of them, and all
three are untranslated in Filipino. Fix: drop the block, or keep one line "Position level:
First Level" (the level matters for eligibility).

**A-03 (P2) Two errors whose Filipino is plainer than the English.** `services/intake.py`:
"The selected recruitment entry is not currently open for intake." (Filipino: "Hindi na
tumatanggap ng applicants ang position na ito.") and "An application for this recruitment
entry already exists for this applicant." Both can reach the applicant from the start form.
Fix the English to match the Filipino: "This position is no longer accepting applications." /
"You already have an application for this position."

**A-04 (P3) "recruitment office" vs "recruitment team".** The status page ("The recruitment
office will record it"), the withdrawal messages (`portal_views.py`, `intake.py`: "contact the
recruitment office") say "office"; everywhere else it is "the recruitment team". Fix: "team".

**A-05 (P3) "Tracking Code" / "tracking code" in the same paragraph.** Help and the status
lookup page alternate. Rule: "Tracking Code" when it names the thing on the receipt or a
field label; "tracking code" inside a sentence — or simply capitalize everywhere, as the
Filipino does.

**A-06 (P3) The privacy band names a body applicants do not know.** `applicant_portal.html`:
"contacting the HRMPSB Secretariat / Data Protection Officer of DOH–CHD CALABARZON". Fix: "by
contacting the recruitment team (HRMPSB Secretariat) or the Data Protection Officer of DOH–CHD
CALABARZON".

**A-07 (P3) The error pages are English-only.** `templates/404.html` and `500.html` are shared
by both portals and carry no translation tags, so a Filipino-mode applicant who hits a bad link
reads "We couldn't find that page. The link may be old or mistyped." in English. Fix: wrap
their five sentences in `translate`.

### 3.3 Filipino (F)

**F-01 (P3) Some status chips are translated, some are not.** `applicant_labels.py`: "Action
Required" → "May Kailangan Kang Gawin", "Not Selected" → "Hindi Napili", "Withdrawn" →
"Na-withdraw", "Vacancy Cancelled" → "Na-cancel ang Vacancy", but "Under Review", "Under
Evaluation", "Under Processing", "Approved" stay English. The progress steps mix too:
"Document check" and "Final decision" stay English beside "Exam at interview". One rule: either
translate the set ("Nire-review", "Ine-evaluate", "Pino-process", "Approved" can stay, "Document
check" → "Check ng documents", "Final decision" → "Final na desisyon") or keep every chip in
English. Translating is the better fit for the rest of the page.

**F-02 (P3) Three salutations.** English emails open "Dear %(name)s," (reminders: "Hello").
The Filipino versions open "Dear", "Hi" or "Hello" depending on the email (15 entries). Pick
"Hi %(name)s," for Filipino — it matches the register of the rest of the translation.

**F-03 (P3) Four labels from `requirements.py` render in English in Filipino mode.** The
download links "How to fill up your PDS (plain-language guide)" and "Official CSC guide to
filling out the PDS (Revised 2026)", and the requirement titles "Personal Data Sheet (CS Form
No. 212, Revised 2026) **with recent passport-sized picture**" and "Signed Application Letter
addressed to …". Official form names can stay in English; the two guide labels and the "with
recent passport-sized picture" tail should go through `gettext` so they can be translated.

The translation itself needs no rework: no mistranslations, no English sentences inside
Filipino strings, and the plural forms are correct.

### 3.4 Emails (E)

**E-01 (P3) "contact the HRM Unit promptly"** (`notification_services.py`, contact-updated
email) — every other email says "Human Resource Management Unit". Fix with L-04.

**E-02 (P3) "The review also noted deficiencies in %(count)s submitted document(s)."**
(not-selected email after screening). Fix: "The review also found problems with %(count)s of
the documents you sent." (the Filipino already reads that way).

**E-03 (P3) "under %(branch)s recruitment".** Six emails say "for Nurse II under Plantilla
recruitment"; the Filipino puts the type in brackets, which reads better: "for Nurse II
(Plantilla)". Also "The next step is %(step)s within the scope of this recruitment process." →
"The next step is %(step)s."; "Qualification Standards (QS)" → drop "(QS)".

**E-04 (P3) Legal counting.** Reporting instructions: "Bring at least one (1) valid
government-issued identification card.", "Report at least fifteen (15) minutes before…". Fix:
"Bring one valid government ID.", "Arrive 15 minutes early."

### 3.5 Hard words (H)

A different problem from 3.1: words that are correct, used consistently, and still unknown to
an ordinary Filipino reader. The applicant side has none of them (checked word by word; the
only "hard" applicant words are *eligibility*, *plantilla* and *resubmit*, each explained where
it appears). The staff side is full of them. Two kinds, treated differently.

**H-01 (P2) The office's own words: keep, gloss once.** The HRMPSB and the CSC say these, so a
staff member knows them even when a new hire does not: *deliberation, quorum, plantilla,
eligibility, Qualification Standards, Comparative Assessment Report (CAR), finalize, endorse,
letter of intent, deep selection, failure of search, waived*. Keep them. Each gets a one-line
plain gloss the first time it appears on a screen, the way "No appointment (failure of
search)" and the deep-selection note already do. Candidates for a gloss: the Deliberation
titlebar ("the board's meeting on the ranked candidates"), "Quorum" ("3 to 5 members
present"), "Letter of intent" ("the applicant's written request to be rescheduled"),
"Waived" ("the office excused the exam"), "Finalize" ("lock; needs an authorized reopen to
change").

**H-02 (P2) Lawyer and programmer words: replace.** No office in the Philippines says these;
a developer or a legal template did. Where they appear and what to say instead:

| Today | Where (user-facing) | Say instead |
|---|---|---|
| rescind, rescission, rescinded | the Director's and HRM Chief's cards and dialogs (`final_selection_rescind_card.html`, `cos_pick_rescind_card.html`), the selection messages, the audit label "Final Selection Rescinded", Office Policy "When rescission is no longer allowed" | **take back the selection**; "the selection was taken back"; audit label "Selection Taken Back" |
| void, voided ("Voided by rescission") | `completion_body.html`, completion and selection messages | **cancelled** ("No longer applies: the selection was taken back") |
| disposition ("vacancy-cancelled disposition") | entry cancellation message | **outcome** |
| superseded, supersedes | paper-sheet replace option, rescission copy | **replaced by** |
| in contention, out of contention | final selection list and messages | **still in the running** / **no longer in the running** |
| terminal (outcome) | absence decision copy | **final** |
| encode, encoder, encoding mode | the paper rating sheet card, `interview_rater.html`, the ratings list ("Encoded by …") | **type in**; "typed in by …"; "Typing in a paper rating" |
| immutable, "preserved intact", "preserved exactly as it was recorded" | locked panels, rescind cards | **kept unchanged** |
| cutoff | Maintenance archive form, rescind-policy copy | **deadline** / "older than this date" |
| snapshot ("Packet Snapshot", "CAR Snapshot", "snapshotted") | `decision_locked_body.html`, `final_selection_locked_body.html`, COS console | **copy at the time** ("The ranked list as it was when the selection was made") |
| justification | rating forms, deep selection, CAR return | **reason** |
| basis ("Basis — required", "one-line basis") | ETE grid, screening decision | **reason** / "why" |
| remarks | forms and panels | **notes** |
| validity period, validity window | exam forms | **valid until** / "valid from … to …" |
| intake, "open for intake" | entry form and messages | **applications** ("while applications are open") |
| pending ("3 pending", "pending decided") | counts and chips | **waiting** |
| authorization, authorized ("Add a reason for this authorization", "reopened with authorization") | override and reopen copy | **permission** / "allowed"; keep the feature name "Special authorization" and "Authorized reopen" as names only |
| attributable, "distinct attributable raters" | interview finalize messages | "ratings from 3 to 5 different members" |
| audit-logged, "recorded in the audit trail" | many confirmations | **recorded** ("This is recorded.") — one phrase everywhere |

Worked example, the Director's card today: *"Rescind this selection — Use this to correct a
selection that was recorded in error, or when the selected person withdraws before
appointment. The original record is kept intact and marked rescinded; the candidates it closed
come back to this step so the vacancy can be decided again."* After: *"Take back this selection
— Use this when the selection was recorded by mistake, or the selected person withdraws before
appointment. The original decision stays on file, marked as taken back. The other candidates
return to this step so the vacancy can be decided again."* Same meaning, no dictionary.

The counts behind this: in the user-facing strings "rescind" and its forms appear about 80
times, "encode" about 40, "justification" about 25, "basis" about 20, "remarks" about 20,
"terminal" about 18, "void" about 17, "cutoff" and "snapshot" about 10 each, "disposition",
"supersede" and "in contention" a handful. "Finalize" (about 300) and "deliberation" (about
200) are the office's own words and stay.

## 4. One name per thing

The glossary the fix round should enforce. The office decides the two marked ⚠.

| Thing | Names found today | Use |
|---|---|---|
| The HR office ⚠ | Secretariat, HRMU, HRMS, HR, HRMO, HRM Unit, Human Resource Management Unit, Human Resource Management Section, Recruitment Selection and Placement Office, workflow team, recruitment team, recruitment office | **Secretariat** (internal); **recruitment team** / **Human Resource Management Unit (HRMU)** (applicant) |
| A vacancy ⚠ | Recruitment Entry, vacancy, position, posting, entry, job opening | **Vacancy** (internal); **position**, **job opening** (applicant) |
| Plantilla vs COS | branch, engagement type, recruitment type, position type, Branch / type, Branch / Intake, Type | **type** (internal), **position type** (applicant) |
| A paper rating | fallback rating, fallback sheet, paper rating sheet | **paper rating sheet** |
| The Level 2 carve-out | Special Authorization, Level 2 authorization, override | **Special authorization** |
| The administrator | System Administrator, System Admin, administrator | **System Administrator** |
| Reopening | Authorized Reopen, authorized reopen, Request Reopen, Reopen Closed Recruitment Case | **Authorized reopen** |
| The steps | Deliberation / CAR, Decision / Submission Packet, Final Selection / Decision, Comparative Assessment | the pipeline labels: **Screening, Exam, Interview, Deliberation, Comparative Assessment, Decision, Completion** |
| A view with no actions | Read Only, Read-only, read-only | **Read-only** |
| The applicant's secret | Tracking Code, tracking code | **Tracking Code** |

## 5. Round I brief (for the implementer)

Wording only. No model, routing or permission change; one migration at most (L-11 choice
labels, no data change). Each batch: suite green (`INTERNAL_MFA_ENABLED=True`), the
translation `--check` test green after `update_translations`, one commit.

**I1 — wrong and stale (L-01, L-02, L-03, L-12, L-13).** The RG placeholder; the three
"Actions tab" sentences and "Decision / Submission Packet"; the two dead includes and the
orphan `/positions/` page; "Return to Dashboard". Add a guard test that no template or
user-facing Python string contains "RG " / "RecruitGuard" (extend the existing branding test if
there is one). An hour.

**I2 — one name per thing (L-04, L-05, section 4).** Mechanical renames by the glossary.
Default the HR office to Secretariat (internal) and recruitment team / HRMU (applicant), and
the vacancy to Vacancy, unless the office answers otherwise before the round. Sidebar, page
titles, breadcrumbs, messages, form labels, audit labels, the 500 page, the export PDF header.
Half a day; mostly find-and-replace with the string list in `i_strings/` as the checklist.

**I3 — applicant side and emails (A-01 … A-07, E-01 … E-04).** Then `update_translations`
for the new and changed strings and their Filipino. Two hours.

**I4 — Filipino consistency (F-01 … F-03).** One hour.

**I5 — staff jargon and the locked panels (L-06 … L-11).** The exam outcome copy, the
routing-history sentences, sentence case on the six locked panels, the jargon table, the
sign-in pages, the notification labels. Two to three hours.

**I6 — hard words (H-01, H-02).** The replacement table in 3.5, applied everywhere a user can
see the word (templates, messages, form labels, choice labels, audit labels, the Office Policy
page), and a one-line gloss for each office word in H-01 where it heads a screen. Keep the
feature names "Special authorization" and "Authorized reopen". The audit labels change wording
only (no data change). Half a day. The office's own words (H-01) are not replaced; if the
office later wants any of them glossed differently, it is one string each.

Check once at the end in the browser: one case per step as the Secretariat and the HRMPSB
member (as in Round H), the applicant start-to-receipt flow in Filipino, and one email of each
kind from the console backend.

## 6. What remains unverified

- The internal pages were read as strings per template, not rendered per role, so a composite
  sentence built from several branches was judged from its parts. The three I doubted were
  checked in the source; others may read differently when assembled.
- The PDF exports (receipt, CAR print, filing copy, controlled export) were read from their
  Python strings, not generated. The filing copy mirrors the office's paper form, so its labels
  ("EPS", "RHEA 2nd Level Representative") were left alone on purpose.
- The emails were read in both languages but not rendered in Filipino.
- Whether "Complied" is the office's own word (CSC form) or the system's — see L-09.
- The two office decisions in section 4.

## 7. Implementation status

| Batch | Status |
|---|---|
| I1 — wrong and stale | **Done.** See the note below. |
| I2 — one name per thing | **Done** with the defaults. See the note below. |
| I3 — applicant side and emails | **Done.** See the note below. |
| I4 — Filipino consistency | not started |
| I5 — staff jargon and locked panels | not started |
| I6 — hard words | not started |

**I1 as built, where it differs from the brief:**

- **The reopen sentence (L-02) is narrower than suggested.** A reopen only works on a *closed*
  case (`user_can_reopen_case` needs `is_stage_locked`), and a finalized exam, interview or
  deliberation panel is shown while the case is still active. So the panels now say
  "Finalized records can't be edited. Only the HRM Chief can reopen a closed case to correct
  one." rather than pointing at a reopen the reader cannot use yet.
- **L-03 points at today's name.** The form now says "Change status from the Recruitment
  Entries list."; it changes again with the rest of the vacancy rename in I2.
- **`/internal/positions/` redirects** to the role's home page instead of returning 404, so an
  old bookmark still lands. The view, template, export name and its two tests are gone;
  `get_visible_positions_for_user` (services) is now unused by any view and was left in place.
- **The queue search label** said "reference" too; it now reads "Search applicant, case number,
  or position" to match the placeholder (the search does match the case number).
- **The branding guard** (`BrandingGuardTests` in `tests/test_architecture.py`) checks readable
  template text and form attributes, and every non-docstring Python string, for
  "RecruitGuard" or "RG". Scripts, styles, comments and `rg-` classes are ignored; one machine
  value (the `RG-Autosave` request header) is allow-listed.

**I2 as built:**

- **Defaults taken** (the office can still overrule them; each is one string): inside the
  portal the HR office is **the Secretariat** wherever a sentence names who does something,
  and the vacancy is **Vacancy** (sidebar "Vacancies", "New vacancy", "Vacancy created.",
  "Vacancy code", audit labels "Vacancy Created/Updated/Status Changed/Deadline
  Changed/Cancelled"). The URL names (`recruitment-entry-…`) and code identifiers are
  unchanged.
- **"Unit", not "Section".** `email_branding.py` records "Human Resource Management Unit" as
  the office-confirmed name, so the exam "administered by" choice and the export PDF header
  (which said "Recruitment, Selection and Placement Office") now use it.
- **Who actually does it, not just a tidier word.** The "HRMS support role" and "HR support
  office" messages meant *the office that owns the level* (Secretariat for Level 1, HRM Chief
  for Level 2), so they now say that; the CAR messages say "the Secretariat", because the
  Secretariat prepares the CAR at both levels (WF-04). The appointment/contract notice on a
  recorded decision names the completion office from the case
  (`completion_handler_label`) instead of "HR".
- **Rest of the glossary:** "paper rating sheet" for "fallback" (labels, messages, the
  evidence label, the audit label "Paper Rating Sheet Added"); "Special authorization"
  everywhere; "System Administrator"; "Authorized reopen"; the step names ("Decision", "the
  next step" where Plantilla and COS differ); "Read-only"; "Type" for Plantilla/COS columns
  and "Position type" for the form field.
- **Migration 0099** changes choice labels and help text only (no SQL; `sqlmigrate` is empty).
- **Left for later batches:** the two applicant-facing entry messages (A-03) and "HRM Unit"
  in the contact email (E-01) are I3, since they need Filipino; "intake" wording (e.g. "Close
  intake") is I6; the routing-history sentences are I5. Old audit and case-history rows keep
  the wording they were written with.

**I3 as built:**

- **A-01** Help says "the two position types" / "when the position requires them" instead of
  entries, posting and branch.
- **A-02** The vacancy page's "Official Position Reference" block is now "Salary and level":
  the salary grade (as before) and the level with a one-line meaning ("First Level is for
  clerical and support jobs; Second Level is for professional and technical jobs up to
  Division Chief"). Class ID is gone. The level label is translated in the template, because
  the model choice label is not.
- **A-03** The two start-form errors now read "This position is no longer accepting
  applications." and "You already have an application for this position."
- **A-04** "recruitment team" on the status page and in the withdrawal messages.
- **A-05** "Tracking Code" capitalized everywhere an applicant reads it (both languages).
- **A-06** The privacy band names "the recruitment team (HRMPSB Secretariat) or the Data
  Protection Officer".
- **A-07** The 404 page follows the portal language (Filipino under /apply/, English for staff
  pages). **The 500 page stays English on purpose**: it carries a note that it must stay free
  of template tags so it can always render.
- **E-01 … E-04** The contact-updated email names the Human Resource Management Unit; the
  screening email says "found problems in N of the documents you sent"; "under Plantilla
  recruitment" is now "(Plantilla)", "within the scope of this recruitment process" and
  "(QS)" are gone; the reporting instructions say "Bring one valid government ID." / "Arrive
  at least 15 minutes before your scheduled time."; the emails' "Recruitment type" label and
  the receipt PDF's "Engagement" / "Reference / Job Code" read "Position type" / "Vacancy code".
- **Filipino**: 38 new or changed strings translated in the existing Taglish voice; 892 of 892
  translated, `update_translations --check` green. Every applicant page re-rendered in
  Filipino: no untranslated line.
