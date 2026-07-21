# RD Backlog — Implementation Plan (RD-3 … RD-9)

Instructions for the implementing agent (Opus 4.8 / ultracode). The design
rationale and locked decisions live in `docs/screening-exam-redesign-plan.md`
("the design doc") — read it first; **the designs are final, do not re-litigate
them.** This document is the "how": files, functions, steps, traps, tests, and
verification for each item. Every claim here was verified against the code on
2026-07-20.

---

## 0. Global norms (apply to every item)

**Build order:** RD-4 → RD-6 → RD-8 → RD-5 → RD-9 → RD-3 → RD-7.
One commit per item, on `main`, commit message explaining problem → design →
tests (match the style of commits `21bd6da` / `10bd8a1`). RD-5 depends on RD-4
(shared classifier). Everything else is independent but keep the order for
severity's sake.

**Tests:** run with the venv python and the SQLite-forcing env:
```
DJANGO_DEBUG=true SECURE_SSL_REDIRECT=false POSTGRES_DB= POSTGRES_USER= POSTGRES_PASSWORD= POSTGRES_HOST= POSTGRES_PORT= ./.venv/Scripts/python.exe manage.py test recruitment.tests.<Class>
```
Known pre-existing failures to IGNORE (do not "fix"): the MFA/captcha flaky
family (FoundationSmokeTests / CaptchaProtectionTests / Recaptcha / Turnstile —
they pass in slow full-suite runs or are known-flaky) and
`E2ESeedCommandTests.test_seed_e2e_test_cases_creates_verification_cases`
(separate task chip). Anything else red is yours.

**Browser verification** (required for every item that changes a rendered
surface): preview launch config `revisions-verify` (port 8059, real Postgres
demo DB, MFA off). Accounts: `revverify_sec` / `revverify_psb` /
`revverify_admin`, password `RevVerify!2026`. The server runs `--noreload` —
**RESTART the preview after any template edit** (static/CSS updates live).
Demo data: `manage.py seed_demo_data --force` (DJANGO_DEBUG=true, real
Postgres) full-wipes and reseeds; demo COS vacancy `VAC-COS-2026-001` is
POOLING intake, Plantilla `VAC-PLT-2026-001` is fixed-period.

**Codebase traps (each has burned us before):**
1. Multi-line `{# … #}` Django comments render literally. Use
   `{% comment %}{% endcomment %}` for anything multi-line. Guard tests assert
   the rendered page contains no `"{#"`.
2. Dead JS references kill whole IIFEs silently (the `csField` bug shipped a
   dead wizard). After removing a form field, grep the template's JS for every
   reference and re-verify wizard init in the browser on a FRESH record.
3. Server-side enforcement is the standard (PT-03): closing a door means the
   POST is refused in the view/service, never just a hidden button.
4. Stage-keyed records: screening/exam/interview/deliberation records are
   keyed by `review_stage`. "The record" always means "the record for the
   case's current stage".
5. `python-docx`-style positional edits don't apply here, but test-payload
   sweeps do: when editing tests by pattern-replace, check every match site
   afterward (a fixup once flipped an unrelated test's operation and left a
   stale assertion).
6. OneDrive + `--noreload` can serve stale templates — if a browser check
   contradicts the code you just wrote, restart the preview before debugging.

**Out of scope everywhere (client-questions list — build NOTHING for these):**
exam-validity enforcement, COS interview single-vs-two-score model change,
ETE split change, missed-completion-deadline consequence, automated
pre-deadline reminders.

---

## RD-4 — Interview consolidation: one door

**Goal.** Mirror RD-2 for interviews: for pooled vacancies the console is the
only writable door for schedule/session/finalize (and for the COS single
score); Plantilla per-rater competency sheets remain per-case; continuous-COS
keeps the full per-case editor. Plus: delete the vestigial screening
disposition block.

**Key files/functions (verified):**
- `recruitment/services.py` — `vacancy_uses_batch_exam(posting)` (the RD-2
  classifier: PLANTILLA always; otherwise `intake_mode != CONTINUOUS`);
  `vacancy_interview_pool`; `save_vacancy_interview_schedule`;
  `get_available_workflow_actions` (~line 7520) and
  `screening_requires_disposition_for_current_stage` (~line 2801).
- `recruitment/views.py` — `VacancyBatchInterviewView` (~1732; docstring
  documents the current split), `InterviewSessionView.post` (~2238 — NO pooled
  guard today), `InterviewRatingSheetView` (~2294, Plantilla competency
  sheet), `InterviewRatingView` (~2499 — determine which form(s) it accepts),
  `InterviewFallbackUploadView` (~2539), `ApplicationDetailView` interview
  context block (~755–800).
- Templates: `recruitment/templates/recruitment/application_detail.html`
  (interview tab), `templates/internal_includes/interview_session_manager.html`,
  `interview_rater.html`, `workflow_action_card.html`;
  `recruitment/templates/recruitment/vacancy_batch_interview.html`.

**Steps.**
1. Introduce a single shared classifier. Rename nothing publicly; add
   `vacancy_uses_batch_assessment = vacancy_uses_batch_exam` alias or simply
   call `vacancy_uses_batch_exam` from interview code with a comment — one
   rule, one function.
2. Server-side refusals (pooled vacancies only):
   - `InterviewSessionView.post` (all operations incl. save/finalize/
     notify_applicant/notify_panel) → `messages.error` + redirect to
     `vacancy-batch-interview` (mirror `ExaminationRecordView`'s RD-2 block).
   - COS rating writes: find the view that accepts `SimpleInterviewRatingForm`
     posts (start at `InterviewRatingView.post` ~2499; the detail context
     builds the simple form when `branch == COS`). Refuse for pooled-COS;
     PLANTILLA sheet posts stay allowed (per-rater exception).
   - `InterviewFallbackUploadView` stays allowed (evidence is per-case,
     additive, conflict-free — same rule RD-2 used).
3. Per-case interview tab for pooled vacancies: render a read-only summary
   (session schedule/location/finalized state, own rating if any, console
   link) instead of the editable session form. Model it on
   `templates/internal_includes/exam_batch_managed_body.html` (the RD-2
   read-only include). Plantilla pooled: session summary read-only, BUT the
   rater's competency sheet block still renders editable (session must exist
   and not be finalized — existing conditions keep working).
4. Notifications from the console only: `VacancyBatchInterviewView`'s
   `save_schedule` already notifies the batch. Confirm the per-case notify
   operations are unreachable post-refusal; do not build new console notify UI
   unless a test reveals a gap (COS notify currently rides on schedule save).
5. Vestigial disposition cleanup — **TRAP: partial removal only.** In
   `get_available_workflow_actions`, the screening-section block currently
   returns `return_to_applicant`/`reject` when
   `screening_requires_disposition_for_current_stage(application)` — those
   two are unreachable post-RD-1 (finalized-NOT_QUALIFIED auto-closes;
   finalized-INCOMPLETE can't exist for new records) — BUT the same block
   appends **`remove_applicant` for `AWAITING_RESUBMISSION` cases, which is
   LIVE and tested** (it unblocks the batch gate). Rework the block to: offer
   `remove_applicant` when `case.case_status == AWAITING_RESUBMISSION` (keep),
   drop the disposition-gated return/reject offerings, and delete
   `screening_requires_disposition_for_current_stage` only after grepping for
   every caller (views + templates + tests). Rework, don't delete, any test
   that exercises `remove_applicant`.

**Tests.**
- New: `test_per_case_interview_post_refused_for_pooled_vacancy` — mirror
  `test_per_case_exam_post_refused_for_pooled_vacancy` (force a session POST
  on a pooled case → 302 to console, no `InterviewSession` written).
- New: pooled-COS simple-rating POST refused; Plantilla sheet POST still
  accepted for a rater on a pooled case.
- Repoint any per-case interview tests that use pooled fixtures to
  `self.cos_continuous_position` (fixture exists as a lazy property — created
  on first use so it does NOT bump the shared COS entry-code sequence; do not
  convert it to eager setUp).
- `VacancyBatchInterviewTests`, `RecruitmentCaseWorkflowTests`,
  `BatchScreeningGateTests` (remove_applicant survival) green.

**Browser verify** (as `revverify_sec`, demo data): pooled COS case interview
tab shows read-only summary + console link; console schedules and scores;
forced per-case POST (via shell Client) refused with no record written;
`{#` guard pages clean.

---

## RD-6 — Returned-application journey (integrity + navigation)

**Goal.** Fix both halves of the generic-return flow: (a) resubmission after a
whole-application return must reopen the stage's finalized records so nothing
sails through unverified; (b) the applicant must have a navigable path back in
(today: returned email links only the status page; the status page's returned
branch says "check your email" — circular).

**In-repo precedent to copy:** `return_car_for_reassessment`
(`recruitment/services.py`) — flags the artifact, un-finalizes downstream
records, moves cases with full audit + routing rows, and the getter excludes
the returned artifact. RD-6 is the same shape one stage earlier.

**Key files/functions (verified):**
- `recruitment/services.py` — `submit_application`'s not-created case branch
  (~6361: `case.case_status != RETURNED_TO_APPLICANT` guard, then
  `_transition_case_stage(force=True)` + reactivation; **no record
  invalidation today**); `get_screening_record`; RD-1's
  `ScreeningRecord.derive_completeness_status`.
- `recruitment/portal_views.py` — `_build_applicant_status_context`
  (`awaiting_resubmission` flag ~93), `ApplicantOTPView` (verify it serves
  RETURNED applications — REV-I lifted the issue-guard via
  `purpose="status_lookup"`; the EDIT path's guard may differ, check
  `issue_application_otp` + the OTP view's application filter and widen for
  RETURNED if needed).
- `recruitment/notification_services.py` —
  `_build_application_returned_to_applicant_notification` (appends status link
  only) and `build_applicant_otp_url` (exists, currently used only by the
  draft OTP email `_deliver_application_otp`, whose continue-link is
  deliberately DRAFT-only — leave that as is).
- Templates: `recruitment/templates/recruitment/applicant_status_lookup.html`
  ("What you need to do" panel, returned branch ~236–250);
  `templates/internal_includes/screening_body.html` (re-verify banner).

**Steps.**
1. Migration: add `ScreeningRecord.reopened_for_resubmission_at`
   (DateTimeField, null=True, blank=True). Explicit beats inferred.
2. In `submit_application`'s reactivation branch (after the stage transition):
   for the re-entry stage's screening record, if finalized: set
   `is_finalized=False`, clear `finalized_by/at`, stamp
   `reopened_for_resubmission_at=now`, reset every
   `ScreeningDocumentReview` row status to `NOT_REVIEWED` (keep remarks for
   context), re-derive+store completeness, and write an audit row
   (`AuditLog.Action` — add `SCREENING_REOPENED` or reuse the closest existing
   action; prefer a new action, humanized label included wherever the audit
   page maps labels). Exam record for the stage is left untouched — documents
   changed, the examination result did not; screening re-finalization is the
   gate that protects endorse (`process_workflow_action` already blocks
   endorse until screening is finalized — verified ~7958).
3. Screening UI: when `screening_record.reopened_for_resubmission_at` is set
   and the record is not yet re-finalized, render an alert at the top of the
   screening body: "This application was returned and resubmitted on {date}.
   Re-verify every document." (Use `{% comment %}` for any multi-line template
   comment.)
4. Status page returned branch: add a primary button "Fix and resubmit your
   application" → `applicant-otp` URL for the token. Confirm the OTP flow
   then lands the applicant in the intake form in edit mode for a RETURNED
   application (this path exists for drafts; extend the OTP view's
   application-selection filter to include RETURNED status if it currently
   filters to drafts). Keep the flagged-path button exactly as is.
5. Returned email: append a direct action link (`build_applicant_otp_url`)
   with copy like "Fix and resubmit your application: {url}", in addition to
   the status link.
6. Status card (awaiting_resubmission variant): show the resubmission
   deadline (`screening_record.resubmission_deadline`) next to the Resubmit
   button — pass it through `_build_applicant_status_context`.

**Tests.**
- Full-journey: generic return (post-exam actions section) → applicant OTP →
  edit → resubmit → assert screening record un-finalized, rows reset to
  NOT_REVIEWED, completeness re-derived, `reopened_for_resubmission_at` set,
  endorse raises until re-finalized, audit row exists.
- Status page: returned-no-flags response contains the OTP-path button;
  awaiting-resubmission response contains the deadline.
- Returned email body contains the OTP URL.
- Existing `NotificationManagementTests` + `ScreeningRecordTests` green
  (their setups use the flagged path — untouched by this change).

**Browser verify:** run the loop live on a demo case: generic return from the
actions card → open the emailed OTP link (console email backend prints it) →
edit + resubmit → screening tab shows the re-verify alert with all rows
pending; endorse hidden/blocked until re-finalize.

---

## RD-8 — Completion checklist & deadline: one source

**Goal.** The checklist email reads the record's requirement rows and the
record's deadline; the send dialog stops re-collecting them; overdue is
surfaced. (The missed-deadline consequence is a client question — build only
the badge.)

**Key files/functions (verified):**
- `recruitment/forms.py` — `RequirementChecklistNotificationForm`
  (~1203: `checklist_items` textarea + own `deadline` defaulting today+14 —
  both to be removed), `ReminderNotificationForm` (~1238: third free deadline —
  default it from the record when the case is at completion).
- `recruitment/notification_services.py` —
  `send_requirement_checklist_notification` (~1035: takes free-text
  `checklist_items`, computes fallback deadline today+14),
  `_build_requirement_checklist_notification` (~501),
  `REQUIREMENT_CHECKLIST_DEFAULT_DEADLINE_DAYS`.
- `recruitment/services.py` — `get_completion_record`,
  `get_completion_requirements`, `CompletionRecord.ready_for_closure` family
  (models ~3435–3465).
- `recruitment/views.py` — `RequirementChecklistNotificationView` (~2919).
- Templates: `templates/internal_includes/completion_body.html` (send dialog +
  deadline input ~124), `recruitment/templates/recruitment/application_list.html`
  (queue row badge), `templates/email/requirement_checklist.html`.

**Steps.**
1. `send_requirement_checklist_notification`: drop the `checklist_items` and
   `deadline` parameters. Read `completion_record = get_completion_record(...)`;
   guards (ValueError): record exists, ≥1 requirement row, record deadline set.
   Build the items list from the rows — pending first
   (`"• {label}"`), completed/N-A appended as "already received/not needed"
   lines or omitted (**recommend: list PENDING only** — the email is an ask,
   not an inventory; note this choice in the commit message). Deadline in the
   email = `completion_record.deadline`, always.
2. Form: `RequirementChecklistNotificationForm` becomes `additional_message`
   only. The dialog in `completion_body.html` shows a read-only preview of the
   pending rows + the record deadline, with a hint that editing happens in the
   tracking form above.
3. `ReminderNotificationForm`: when the case is at completion and the record
   has a deadline, initialize the reminder deadline from it (still editable).
4. Overdue badge: on the completion tab and the queue row (the queue's task
   pill area in `application_list.html`), when
   `completion_record.deadline < today` and
   `completion_record.has_pending_requirements` → "Deadline passed
   {date:M j}". Add a small model property
   (`CompletionRecord.is_overdue`) rather than template date math.
5. Do NOT add auto-reminders or any lapse consequence.

**Tests.** Rework `NotificationManagementTests`' checklist tests: email body
equals pending rows + record deadline; sending without a record / without rows
/ without a deadline raises with readable messages; reminder default picks up
the record deadline; overdue property truth table (no deadline → False;
future → False; past+pending → True; past+all-resolved → False).

**Browser verify:** completion tab on the dummy finalized-CAR demo case —
record rows + deadline, open send dialog (preview matches rows), send (console
email shows the rows + record deadline), set deadline to yesterday → badge on
tab + queue.

---

## RD-5 — One-door completeness & blinding consistency (small batch)

Three independent fixes, one commit. **Requires RD-4's shared classifier.**

1. **Console guard for continuous vacancies.**
   `VacancyBatchExamView` (get + post) and `VacancyBatchInterviewView`
   (get + post): when NOT `vacancy_uses_batch_exam(entry)` →
   `messages.error("This vacancy's <exam|interview> is recorded per case…")` +
   redirect to `vacancy-batch-detail`. Tests: continuous-COS console GET
   redirects, POST writes nothing; pooled console unaffected; per-case editor
   for continuous still fully functional.
2. **HRMPSB queue identifier.** `application_list.html` col-ref: for
   `request.user.role == "hrmpsb_member"` render vacancy `job_code` (line 1)
   + `Candidate {{ application.candidate_code_label }}` (line 2) and DROP the
   `reference_label` — mirroring `case_header.html`'s REV-B rationale
   (sequential case numbers weakly leak submission order). Everyone else keeps
   the case number (Ronel blinding, commit `31de83d`, stays intact — names
   stay off). Candidate codes exist by HRMPSB stage (assigned in
   `_transition_case_stage`). Test: HRMPSB queue response contains
   `Candidate C-` and does NOT contain that case's `CASE-` reference in the
   ref cell (assert against the specific reference_number string).
3. **COS weights card.** `vacancy_batch_exam.html`: remove the
   `{% if is_plantilla %}` around the weights card (~line 41) so COS sees the
   weights (60/40 default) and the Edit-weights link. Check
   `vacancy-assessment-weights` view permits COS entries (it computes
   `assessment_weights_or_default` either way); if it hard-blocks COS, allow
   it. Weight-lock behavior (`is_locked` once scoring starts) applies as-is.
   Test: COS console response contains "Exam weights"; browser-verify the COS
   console shows the card and the note's "weights above" now points at
   something real.

---

## RD-9 — Notification visibility & recovery

**Goal.** Delivery state visible where work happens; failed sends recoverable;
status page shows only announced interviews; exam finalize gets a bell.

**Key files/functions (verified):**
- `recruitment/notification_services.py` — `_mark_notification_failed` (~547),
  `queue_notification`, `NOTIFICATION_MANAGER_ROLES` (= SECRETARIAT +
  HRM_CHIEF, ~65).
- `recruitment/models.py` — `NotificationLog` (types/status ~3233+),
  `Notification.Kind` (~3325: add `EXAM_FINALIZED`, `NOTIFICATION_FAILED`).
- `recruitment/services.py` — in-app emitter helpers (~2219–2290: follow the
  `SCREENING_FINALIZED` / `INTERVIEW_FINALIZED` patterns),
  `save_vacancy_exam_scores` finalize loop, `_active_users_with_application_access`.
- `recruitment/portal_views.py` — `_build_applicant_upcoming_schedules`
  (~46: exam filter has `applicant_notified_at__isnull=False`; interview
  filter has NO such field — see step 4).
- `recruitment/views.py` + `application_detail.html` — new panel + resend view.

**Steps.**
1. **Per-case "Notifications sent" panel.** New include on the case page,
   visible to `NOTIFICATION_MANAGER_ROLES` (and only when
   `user_can_view_application`): rows = `application.notifications`
   (NotificationLog) ordered newest-first — type label, `sent_at` or FAILED
   badge with truncated `failure_details`, triggered-by. Do NOT show to
   HRMPSB (the log's subjects contain applicant names — blinding).
2. **Resend.** `NotificationResendView` (POST, pk + log id): permission =
   panel visibility + same-role rule the original send used; re-queue via
   `queue_notification` with the STORED subject/body verbatim (a resend is a
   redelivery, not a re-render — content the office believes was sent) and
   `metadata={"resent_from": log.id, …}`. Audit it (new
   `AuditLog.Action.NOTIFICATION_RESENT`; deliberate act, so REV-D's
   "no routine send rows" rule is not violated). Button on FAILED rows always;
   on SENT rows too (email lost/deleted) — label "Send again".
3. **Failure bell.** In `_mark_notification_failed`, emit an in-app
   `Notification` (kind `NOTIFICATION_FAILED`) to the case's current-handler
   users. TRAP: `notification_services` must not import from `services`
   (circular) — put the emitter beside the other in-app emitters in
   `services.py` and have `_mark_notification_failed` call it via a
   late/local import, matching however the modules already share helpers
   (check the existing direction of imports first).
4. **Interview status-page parity.** Migration: add
   `InterviewSession.applicant_notified_at` (DateTimeField, null/blank) —
   the field does NOT exist today (only `ExamSchedule` has it, models ~2013).
   Stamp it wherever the applicant interview notice is queued
   (`save_interview_session`'s `notify_applicant` path /
   `queue_applicant_interview_notice…` call site). Data migration backfill:
   for sessions whose application has a SENT
   `APPLICANT_INTERVIEW_NOTICE` log, set the stamp from that log's `sent_at`
   (prevents currently-visible schedules from vanishing). Then filter
   `_build_applicant_upcoming_schedules`'s interview query on
   `applicant_notified_at__isnull=False`.
5. **Exam finalize bell.** In the batch finalize path (and per-case finalize
   for continuous), emit `EXAM_FINALIZED` to the current-handler users —
   mirror the `INTERVIEW_FINALIZED` emitter. For a batch, one bell per case is
   acceptable (matches existing per-case granularity).

**Tests.** Panel renders for secretariat, absent for HRMPSB; resend creates a
new PENDING log with `resent_from` + audit row; permission refusals; failure
path creates the bell; status page hides un-notified interview sessions and
shows notified ones (backfill covered by fixture); exam finalize emits the
kind. Rerun `NotificationManagementTests` + portal status tests.

**Browser verify:** trigger a checklist send on demo data (console backend =
SENT), view panel; fake a failure (temporarily break EMAIL_BACKEND? — instead:
unit-level is fine, browser-verify the panel + resend on a SENT row); status
page of the exam-stage COS case shows exam schedule only after batch schedule
sent.

---

## RD-3 — ETE single source (screening checks papers, the CAR grades)

**Goal.** Remove the ETE score inputs from the screening surface; the CAR's
`ApplicationETERating` becomes the only live source. Model fields and the CAR
read-fallback STAY (history).

**Key files/functions (verified):**
- `recruitment/forms.py` — `ScreeningReviewForm` (remove
  `education_score`, `training_score`, `experience_score`,
  `document_review_score` from fields/Meta).
- `templates/internal_includes/screening_body.html` — the score-boxes block
  AND its JS: autosave field list, the UX-03 "copy prior review" routine
  (it may copy score fields — strip those references; **csField lesson: grep
  every removed id/name in the template's JS and browser-verify wizard init
  on a fresh record afterward**).
- `recruitment/services.py` — `save_screening_review` reads
  `cleaned_data.get(...)` for these (tolerates absence — verify, don't
  assume); `calculate_policy_document_review_score` + CAR fallback
  (`ete_component = ete_rating or document_review_score`, ~5883) UNTOUCHED.
- `recruitment/tests.py` — `screening_payload` helper and any test posting
  score fields.

**Steps.** Remove form fields → remove template block + JS references →
confirm service tolerates missing keys → rework test payloads → add tests:
(a) new screening finalize stores NULL scores and derived completeness
unaffected; (b) CAR prep for a fresh case shows blank assessment score until
an `ApplicationETERating` is entered, then computes; (c) a legacy record with
`document_review_score` still feeds the fallback (regression-protects
history).

**CAR display note (decide in-commit, don't ask):** the CAR table's
"Document Review" column reads the (now never-populated-for-new-records)
snapshot value. KEEP the column — it renders historical values and blanks
harmlessly for new rows; removing it would touch REV-E print layouts for no
integrity gain. State this in the commit message.

**Browser verify:** fresh screening wizard has no score boxes and the wizard
initializes (gate arms, banner computes); CAR screen ETE entry → assessment
score appears.

---

## RD-7 — Vacancy & case lifecycle guards

Two small server-side guards, one commit.

1. **Extending a manually-Closed vacancy → BLOCK (don't auto-reopen).**
   A manual Close is a deliberate stop; extension must not silently undo it.
   In the entry edit path (`RecruitmentEntryUpdateView` form/service — REV-A's
   changed-value-only rule lives there): if `instance.status == CLOSED` and
   `closing_date` is being changed → `ValidationError("This vacancy is
   closed. Reopen it (set it Active) before extending the deadline.")`.
   Reopening via the existing status control (`update_recruitment_entry_status`)
   then extending works. TRAP: `is_open_for_intake` (models ~707) requires
   ACTIVE — that stays; WF-07's early-close block is unrelated, don't touch.
   Tests: closed+extend raises; reopen-then-extend succeeds and the REV-A
   "Deadline extended" public notice fires; suspended postings unaffected.
2. **Reopen guard for decided vacancies.** In `reopen_recruitment_case`
   (~8794): if the vacancy has a recorded final selection
   (`get_final_selection_for_entry(application.position)`) or a COS final
   decision (the getter used by `record_final_decision`'s duplicate guard —
   reuse the same lookup), AND the case being reopened is NOT the selected
   application's case → `ValueError("This vacancy's selection is already
   recorded. A non-selected case can't be reopened while the selection
   stands.")`. The WINNER's case must remain reopenable (completion
   corrections are the legitimate use). Tests: sibling reopen refused after
   selection (both branches), winner reopen still works, undecided-vacancy
   reopen unaffected.

---

## After the last item

1. Full suite once (`manage.py test recruitment`, background it — ~45 min);
   expected residue = the known flaky family + the e2e chip only.
2. `seed_demo_data --force`, then a single browser pass over: queue → a
   screening case → batch exam console → batch interview console → a
   completion case → applicant status page.
3. Update `docs/screening-exam-redesign-plan.md`: flip each RD's **Status:**
   line to DONE + commit hash.
4. Leave pushing/deploying to the user. Do not run `git push`.
