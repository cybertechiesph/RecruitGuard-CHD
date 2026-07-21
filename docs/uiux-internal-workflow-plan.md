# RecruitGuard-CHD — Internal Staff Workflow: Design Assessment & Implementation Plan

_Scope source: `docs/uiux-internal-workflow-review-prompt.md`. Produced 2026-07-04 from a complete live walkthrough of BOTH flows on the running app: a Plantilla L1 vacancy (4 candidates) driven from screening through batch exam, batch interview, HRMPSB rating, CAR, deliberation, Appointing Authority selection and completion; and a COS vacancy (5 candidates) driven through Secretariat screening/exam, HRM Chief re-validation, interview, the per-vacancy deliberation pick, and the final COS decision. Every threshold cited below was read from the code (file:line), and every gap was reproduced live before being written down. Related docs: `docs/uiux-internal-members-plan.md` (implemented and merged — this plan does not repeat its items) and `docs/internal-uiux-review.md` (a11y/visual audit; overlaps flagged, not duplicated)._

## 1. Context

RecruitGuard-CHD is a Django app for DOH–CHD CALABARZON running the HRMPSB hiring process at `/internal/`. Four workflow roles: **Secretariat** (central operator, heaviest user), **HRM Chief**, **HRMPSB Member** (interview rating + deliberation), **Appointing Authority** (final Plantilla decision). Out of scope: System Administrator, applicant portal.

**The two flows** (as implemented and verified live):
- **Plantilla:** Secretariat screening → batch exam (schedule + general/technical scores, weights editable per vacancy) → auto-route to HRMPSB → Secretariat schedules batch interviews (applicant + panel notified) and publishes the competency rating sheet → HRMPSB members rate digitally (private until deliberation) → Secretariat/HRM Chief prepares the draft CAR (per-candidate ETE input; ETE 40 / Exam 20 / Interview 40) → CAR finalize records the board's recommended applicant + quorum info → Appointing Authority picks from the ranked list (deep-selection justification required only for rank > 5) → non-selected auto-rejected + notified; selected moves to completion (free-text requirements, 2-week default deadline).
- **COS:** Secretariat screening + exam → auto-route to HRM Chief, who re-validates screening/exam (two-pass model, with the merged "Copy results from Secretariat" one-click prefill), runs the interview (single 0–100 score, no competency sheet), records the per-vacancy **deliberation pick** ("the pick is the COS decision — no CAR, no appointing authority"), then records the Selected decision per case → completion.

**Run and seed:** `.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8058` with `DJANGO_DEBUG=True`, `SECURE_SSL_REDIRECT=False`, console email backend (launch config `recruitguard-uxreview-nomfa` in `.claude/launch.json` sets all of it). DB gotcha: `.env` points at local Postgres `recruitguard_chd`; empty-string `POSTGRES_*` (not unset) falls back to stale sqlite — use Postgres. Seed with `manage.py seed_demo_data` (creates Plantilla and COS vacancies with applicants). **Test accounts** (password `UxReview!2026`): `ux_secretariat`, `ux_hrmchief`, `ux_hrmpsb`, `ux_hrmpsb2`, `ux_appointing`.

**Fixed constraints — re-present, never remove:** CAR formula ETE 40/Exam 20/Interview 40 and HRMPSB order; quorum 3–5 for deliberation; full audit trail; the board deliberates off-system and the Secretariat encodes on its behalf; CAR and rating-sheet templates are Secretariat-editable; completion requirements are free text with an auto 2-week deadline; the exam is scored, never pass/fail.

---

## 2. Design assessment — the honest verdict

**End-to-end architecture: KEEP, with conviction.** The system's core shape — *batch consoles for batch work, per-case wizards for per-candidate records, automatic routing with notifications on every handoff* — matches how this office actually runs recruitment. I drove nine candidates through two full pipelines: the batch exam console (schedule once, score a grid, finalize the pool together), the batch interview console, and the COS deliberation console are exactly the group-shaped tools the prompt asks whether the design should have. It already has them. The handoffs fire real notifications (verified in the email log: exam invitations, interview notices to applicants AND panel members, selected/non-selected results), and every finalize writes humanized audit events. For a government audit context this architecture — immutable finalized records, reopen only by the HRM Chief with a recorded reason, every actor/when/why logged — is the right trade: it privileges defensibility over editability, which is what an HRMPSB process needs. The remaining problems are not architectural; they are missing *rules* (quorum, dates, closure) and a handful of screens that don't show the data the person standing at them needs.

**Per role:**

- **Secretariat — KEEP the batch-first design; the bottleneck is now rules, not clicks.** The batch consoles, mark-all-meets, copy-results, next-case strip, and searchable queue (all merged from the prior plan) have removed most of the mechanical grind I measured in the earlier review. What still hurts: the rating-sheet prerequisite is invisible until the panel complains (WF-08), and the CAR draft can present a meaningless alphabetical "ranking" (WF-11). Verdict: keep the model, close those seams.
- **HRM Chief — KEEP the two-pass re-validation, now that it's cheap.** The chief re-doing screening/exam at their stage is an intentional four-eyes control worth keeping in a government process, and with the prior-stage card + one-click copy it cost me ~3 clicks per case live. One genuine leftover: the chief's stage demands its own *exam schedule* before recording results for an exam that already happened at the Secretariat stage (WF-12) — pure ceremony, verified live.
- **HRMPSB Member — KEEP the digital rater; it's the best screen in the system.** Private-until-deliberation scores, auto-aggregation mirroring the server, threshold-triggered justification, revisable until finalize, plus the paper fallback upload for the encode-on-behalf constraint. The prompt asks whether "a cleaner per-rater sheet that auto-aggregates" would beat hand-encoding — it exists and it does. What's missing is the guardrail (a session can finalize with one rating — WF-01) and pre-deliberation orientation (no "3 of 5 rated" indicator).
- **Appointing Authority — KEEP the packet-review → ranked-pick structure; fix the justification rule.** The final-selection screen already shows the full ranked CAR with the board's recommendation tagged on #1 (verified live) and native-radio candidate cards. The single real defect: I selected rank #2 against the board's #1 and the system recorded it with "Deep selection: No" and no deviation justification (WF-03).
- **Two-deliberation structure — KEEP; the system supports both touchpoints correctly.** First deliberation (pre-CAR, off-system): its outcome arrives implicitly via the interview ratings the members submit. Second deliberation (post-CAR): the CAR-finalize modal records the board's recommended applicant, quorum answer, and members present against the ranked list — the right shape for "board deliberates off-system, Secretariat encodes." The one weakness is that the quorum fields are decorative (WF-02). A comparison board for deliberation day is worth exploring (Section 6) but is an addition, not a correction.
- **COS flow — KEEP the lightweight shape; finish its edges.** The per-vacancy deliberation-pick console is honest copy and the right granularity. But the pick list shows names with **no scores** (WF-09), the pick finalize has no confirmation (WF-15), a "Notify HRMPSB panel" button leaks into a flow with no HRMPSB (WF-13), and after the chosen candidate's decision the rest of the pool strands un-notified at the chief's queue (WF-10) — the sharpest flow gap found in either path.

**Bottom line:** this is a sound, well-matched design that has already absorbed one round of workflow-efficiency fixes. Nothing here needs re-architecting. The plan below is (a) the locked validation rules, enforced server-side, and (b) six seams where a screen doesn't carry the information or confirmation the moment demands.

---

## 3. Prioritized change items

Type = Design-only / Copy / Feature. Validation rules marked HARD (server blocks) or SOFT (explicit confirm/warn). Every threshold below is the real one from code; where no rule exists I say so.

### Critical

---

#### WF-01 — Interview sessions can be finalized with a single rating; enforce the panel minimum (HARD)

**Roles:** Secretariat (session manager), HRMPSB · **Screens:** case interview section + `/internal/vacancies/<pk>/interview/` · **Type:** Feature (HARD)

**Problem.** The 3-member panel minimum exists only as intent. The finalize gate is `MIN_INTERVIEW_OUTPUTS_TO_FINALIZE = 1` ([services.py:142](../recruitment/services.py), used at ~4436) — one rating *or one fallback sheet* lets the session lock, and the interview score then feeds 40% of the CAR from a single member's opinion.

**Evidence.** Live: finalized a Plantilla session with exactly one rating; the confirm modal itself displayed "Ratings on record: 1" and proceeded. The "Rating submissions" panel shows no rated-count indicator.

**Change specification.**
1. `recruitment/services.py` — make the minimum branch-aware: keep `1` for COS (the HRM Chief alone interviews, verified live), require **3** for Plantilla HRMPSB sessions. Replace the single constant with `MIN_INTERVIEW_RATINGS_PLANTILLA = 3` and use it in the session-finalize check (~line 4436) and in `finalize_vacancy_interview_batch`. Error copy: *"This interview needs ratings from at least 3 HRMPSB members before it can be finalized (currently N)."* Fallback-sheet uploads count toward the minimum only if the sheet records ≥3 raters — if that metadata doesn't exist on the fallback, count each uploaded sheet as one rater and note the limitation in the error copy.
2. Session manager UI (`templates/internal_includes/interview_session_manager.html`) — under "Rating submissions", add a live count line: *"N of 3 required ratings submitted"* (green ≥3), and surface the same count in the finalize modal (it already renders the number — wire the block).
3. HARD: server check in the service, not just UI.

**Acceptance criteria.**
- Finalizing a Plantilla session with 1–2 ratings is rejected with the count in the message; with 3 it succeeds.
- A COS session with the chief's single rating still finalizes.
- The session manager shows "1 of 3 required ratings submitted" after the first rating.

**Dependencies:** none.

---

#### WF-02 — Deliberation quorum (3–5) is decorative; enforce it at CAR finalize (HARD)

**Roles:** Secretariat/HRM Chief (CAR preparer) · **Screen:** CAR finalize modal on the case shell · **Type:** Feature (HARD)

**Problem.** `quorum_met` and `members_present` are nullable model fields ([models.py:~2636](../recruitment/models.py)) with **no validation anywhere**; the finalize modal renders both as optional (`required: false` verified in the DOM) and the constraint "minimum 3, maximum 5 members" exists nowhere in code.

**Evidence.** Live: submitted the finalize modal with quorum "No" / members 2 — no rule fired (that attempt failed only because the required *recommended applicant* was missing); the code-fact pass confirms NOT FOUND for any quorum enforcement.

**Change specification.**
1. `recruitment/models.py`, `ComparativeAssessmentReport.clean()` — when `is_finalized` is being set: require `quorum_met is True`, and `members_present` between **3 and 5** inclusive. Errors: *"Deliberation quorum (at least 3 of the 5 HRMPSB members) must be met before the CAR can be finalized."* / *"Record how many HRMPSB members were present (3–5)."*
2. Mirror the check in the finalize service so the API path can't bypass it, and make both modal fields `required` with `min="3" max="5"` on `members_present` (`templates/internal_includes/comparative_assessment.html` finalize modal).
3. Keep the fields recordable-but-unvalidated on *draft* saves (the deliberation may not have happened yet).

**Acceptance criteria.**
- Finalize with quorum "No", blank, or members 2 or 6 → blocked with the message; quorum Yes + members 3/4/5 → succeeds.
- Draft saves still work with the fields blank.

**Dependencies:** none.

---

#### WF-03 — Selecting against the board's #1 requires no justification; make it mandatory (HARD)

**Role:** Appointing Authority · **Screen:** Final Selection on the case shell · **Type:** Feature (HARD)

**Problem.** The screen already shows the ranked CAR with "Board's recommendation" tagged on #1 (good — verified live). But justification is only demanded beyond rank 5 (`TOP_FIVE_SELECTION_LIMIT`, deep-selection rule at [models.py:2968-2974](../recruitment/models.py) / [services.py:7607-7618](../recruitment/services.py)). Ranks 2–5 sail through: I recorded rank #2 against the board's #1 live — confirm modal said "Deep selection: No", asked nothing about the deviation, and never mentioned the recommendation.

**Change specification.**
1. `recruitment/models.py`, `FinalSelection.clean()` + the service guard in `record_final_selection` — add: when the selected item is **not** the CAR's `recommended_case` (fall back to rank #1 when no recommendation was recorded), a non-blank justification is required. Reuse a single justification field for both rules if practical; the copy differs:
   *"You are selecting someone other than the board's recommended candidate. Record the justification for the official record."*
   Keep the existing >5 deep-selection rule additionally.
2. `templates/internal_includes/final_selection_body.html` — reveal the justification textarea (with the copy above) whenever a non-recommended candidate is picked, and add one line to the confirm modal: *"Board's recommendation: {{ name }} (rank #1) — you are selecting {{ selected }} (rank #N)."*

**Acceptance criteria.**
- Picking the recommended #1: no justification demanded; confirm modal unchanged.
- Picking rank #2 without justification → blocked server-side with the message; with justification → records, and the audit trail contains it.
- Picking rank #6 still additionally requires the deep-selection documentation.

**Dependencies:** none.

---

### Major

---

#### WF-04 — Secretariat must be able to generate the CAR for Level 2 (locked decision)

**Roles:** Secretariat, HRM Chief · **Type:** Feature

**Problem.** `PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL` ([services.py:162-165](../recruitment/services.py)) maps Level 1 → {SECRETARIAT}, Level 2 → {HRM_CHIEF}. The client's decision: the Secretariat generates the CAR for both levels.

**Change specification.** Change Level 2's set to `{RecruitmentUser.Role.SECRETARIAT}`. Note for the implementer: this constant also drives the CAR-support queue carve-out in `get_queue_for_user` — after the change, verify a Level-2 case at HRMPSB stage with a finalized interview appears in the *Secretariat's* queue (not the chief's) and that `user_can_prepare_plantilla_car` follows. If the client wants the chief to retain access too, use the union `{SECRETARIAT, HRM_CHIEF}` — one-line difference; confirm with the client at review time (flagged, not assumed).

**Acceptance criteria.**
- As `ux_secretariat`, a closed Level-2 vacancy with finalized interviews shows "Prepare draft CAR" and the full CAR flow works.
- As `ux_hrmchief`, Level-2 CAR preparation is no longer offered (or still offered, if the union was chosen — record which).
- Existing Level-1 behavior unchanged.

**Dependencies:** none, but retest WF-02/WF-03 flows after.

---

#### WF-05 — Make ETE real: three scored components with a basis, one source for the 40% (locked decision)

**Roles:** Secretariat (screening + CAR) · **Screens:** screening step 3, CAR draft · **Type:** Feature

**Problem (verified live and in code).** Three things all mean "ETE" today: (a) screening step 3 collects optional Education/Training/Experience scores that **nothing reads**; (b) the CAR's 40% actually uses the per-candidate `ete_<case_id>` single input on the CAR draft, falling back to `document_review_score` ([services.py:5219-5226](../recruitment/services.py)); (c) the screening UI labels the one that counts "Official Document Review Score". The screening step also instructs "Compare the application against the minimum qualifications" while showing **no QS anywhere** (verified: no QS panel exists on the screening screen).

**Change specification.**
1. **Screening screen QS panel** (`templates/internal_includes/screening_body.html`, step-3 area ~line 200s): render the position's qualification standards (education/training/experience/eligibility from the entry's `position_reference`) in a read-only card beside the qualification choice: title *"Minimum qualifications for {{ position title }}"*. Data exists on `PositionPosting.position_reference` — pass it via the case-shell context.
2. **Three ETE inputs with basis** on the CAR draft (`templates/internal_includes/comparative_assessment.html` + `apply_car_ete_ratings` [services.py:5148-5172](../recruitment/services.py) + `ApplicationETERating` model [models.py:2590-2619](../recruitment/models.py)): replace the single `ete_<case_id>` number with `ete_education_<id>`, `ete_training_<id>`, `ete_experience_<id>` (each 0–100) **plus a required one-line basis per component** (e.g. "Master's degree; only Bachelor's required"). Extend `ApplicationETERating` with the three sub-scores + three basis fields; keep `rating` as the computed sum for backward compatibility with [services.py:5219-5226](../recruitment/services.py).
3. **Weighting inside the 40%:** the on-screen "policy basis" hint is **branch-dependent display copy** built in `recruitment/forms.py:1589-1595` — Plantilla: *"education 40%, training 30%, experience 30%"*; COS: *"education 30%, training 30%, experience 40%"* — and no code enforces or uses either. Default the composition to the branch-appropriate split (Plantilla 40/30/30, COS 30/30/40) so the system matches its own stated policy, and FLAG both splits for client confirmation in the PR description (do not silently choose equal weights against the on-screen policy text).
4. **Retire the duplicates:** remove the dead `education_score`/`training_score`/`experience_score` inputs from screening step 3 (fields can stay on the model for history), and rename the remaining "Official Document Review Score" label to *"Document review score (legacy — superseded by ETE ratings on the CAR)"* or hide it once the ETE inputs land; the fallback in services stays for old records.
5. Keep "Qualified / Not Qualified" untouched.

**Acceptance criteria.**
- Screening step 3 shows the position's QS beside the qualification decision and no longer shows the three dead score boxes.
- The CAR draft collects E/T/E + basis per candidate; leaving any basis blank blocks the draft update with a plain message; the stored `rating` equals the 40/30/30 composition and the CAR total still matches ETE 40/Exam 20/Interview 40 (spot-check: E=80/T=80/E=80 → ETE 80; with exam 85.60 and interview 100 → 89.12).
- Old records with only `document_review_score` still render and rank.

**Dependencies:** WF-11 pairs naturally (same screen).

---

#### WF-06 — Past dates: gray them out in the calendars (scope reduced by owner decision, 2026-07-04)

**Roles:** Secretariat, HRM Chief · **Screens:** batch exam/interview consoles, per-case schedule forms · **Type:** Design-only

**Current state (verified):** the past-date HARD block already exists server-side for exam and interview scheduling ([forms.py:2069-2083, 2111-2125](../recruitment/forms.py) — "The exam/interview can't be scheduled in the past.", 5-minute grace) — confirmed live with a past date. The datetime inputs, however, have **no `min` attribute**, so the calendar picker happily offers past dates and the user only learns after submitting.

**Change specification.** Add `min` (today, local time) to the `scheduled_for` datetime-local inputs on `vacancy_batch_exam.html`, `vacancy_batch_interview.html`, `exam_schedule_card.html`, and `interview_session_manager.html`. The existing server rule stays authoritative — this is a courtesy layer only.

**Descoped (owner decision — retained here for traceability, not for implementation):** the remaining date rules from the client's original locked list — weekend/8am–5pm confirm, PH-holiday warning, cross-stage date ordering, venue double-booking, and the short-notice warning — are **deferred**. None had a threshold or calendar defined anywhere in code or client instructions; the owner chose to wait for the client's values rather than ship placeholders. If the client later supplies them, the prior full specification for these rules is preserved in this document's git history (see the version of this item before 2026-07-04).

**Acceptance criteria.**
- The exam and interview date pickers do not offer past dates (`min` attribute present, set to today).
- A past date forced via DevTools/direct POST is still rejected server-side with the existing message.
- No weekend/holiday/ordering/double-booking behavior changes.

**Dependencies:** none.

---

#### WF-07 — Publication period: configurable, and closure blocked before it ends (HARD)

**Roles:** Secretariat/HRM Chief (entry managers) · **Screen:** `/internal/entries/` close action; entry form · **Type:** Feature (HARD)

**Problem.** `PLANTILLA_PUBLICATION_PERIOD_DAYS = 14` is hardcoded ([models.py:388](../recruitment/models.py), used by `calculate_plantilla_closing_date` ~478-491), and **nothing stops closing early**: live, I closed a vacancy with `closing_date = Jul 16` on Jul 4 — one modal, no resistance (verified: no guard exists in the close path). The side effect makes it worse: the applicant pool auto-finalizes when the entry is CLOSED *or* the closing date passes ([models.py:697-704](../recruitment/models.py)), so an early close doesn't just cut the published application window short — it immediately unlocks deliberation and CAR generation for an incomplete pool.

**Change specification.**
1. Make the period configurable: read from `settings.PLANTILLA_PUBLICATION_PERIOD_DAYS` (env-driven, default 14) — a settings/env change is sufficient "configurable in the system" for this client; do not build an admin UI for it in this pass.
2. HARD guard in the close-entry service/view: if `closing_date` is in the future, block with: *"This vacancy's publication period runs until {{ closing_date }}. It can't be closed before then. Use Suspend if applications must stop for an exceptional reason."* Suspend remains available (it exists) as the emergency valve.
3. Disable the Close button with a title tooltip before that date (Design), keeping the server rule authoritative.

**Acceptance criteria.**
- Close on an entry whose closing date is in the future → blocked with the message (server-side; the modal never commits).
- Close on/after the closing date works as today; Suspend works at any time.
- Changing the env default changes the pre-filled closing date for new Plantilla entries.

**Dependencies:** none. (CAR unlock on closure is already implemented and was verified live: the CAR became preparable only after closure.)

---

#### WF-08 — The rating-sheet prerequisite is invisible; the panel gets told the wrong thing

**Roles:** HRMPSB, Secretariat · **Screens:** case interview section; `/internal/vacancies/<pk>/interview/` · **Type:** Design-only + Copy (one template-dispatch bug)

**Problem (found live).** With interviews scheduled but no published rating sheet, the HRMPSB member's case page said *"Interview / Rating not yet available — this step will open when the case is assigned to HRMPSB or HRM Chief"* — while the case WAS assigned to HRMPSB and `user_can_manage_interview_rating` returned True. Root cause: the view sets `interview_rating_requires_sheet` ([views.py:640](../recruitment/views.py)) but the dispatch `elif` at [application_detail.html:193](../recruitment/templates/recruitment/application_detail.html) doesn't include that flag, so it falls through to the generic (wrong) empty state at line 199. Meanwhile the Secretariat's batch interview console never mentions the sheet at all.

**Change specification.**
1. `application_detail.html:193` — add `or interview_rating_requires_sheet` to the elif; inside `interview_body.html`, render a dedicated state when that flag is set: title *"Rating sheet not yet published"*, copy *"The Secretariat prepares the interview rating sheet for this vacancy. You'll be able to rate once it's published."*
2. `vacancy_batch_interview.html` — in section 1, when `get_published_competency_rating_template(entry)` is None, show a notice with a link: *"No rating sheet is published for this vacancy yet — the panel can't rate until it is. **Prepare the rating sheet** →"* linking to the rating-sheet builder (any case's `interview/rating-sheet/` URL — it edits the per-position template, verified live).
3. Optional copy touch: the prior-stage card header for HRMPSB raters says "The finalized results you are re-validating" — raters don't re-validate; change to *"…for your reference during the interview."* when the viewer's role is HRMPSB member.

**Acceptance criteria.**
- With no published sheet, the HRMPSB member sees the "Rating sheet not yet published" state (never "will open when assigned").
- The batch interview console shows the prepare-sheet notice + link until published, then hides it.
- Publishing the sheet flips the member's page to the rater without other changes.

**Dependencies:** none.

---

#### WF-09 — The COS deliberation pick shows candidates without a single score

**Role:** HRM Chief · **Screen:** `/internal/vacancies/<pk>/deliberation/` · **Type:** Design-only

**Problem.** The pick console asks the chief to record "the most qualified applicant" but each radio row shows only name + reference (verified live — no exam score, no interview rating). The comparative data exists and is finalized by this point; the chief must recall it or open five cases.

**Change specification.** In `vacancy_batch_cos_deliberation.html`, add per-candidate columns: exam composite (from the finalized exam record), interview score (the chief's rating), and screening outcome pill. Same row layout as the CAR draft rankings (reuse its styles). Sort by interview score descending so the strongest are on top, but keep the radio choice free.

**Acceptance criteria.**
- Each candidate row shows exam and interview scores matching the case records.
- Ordering is by interview score; the pick still allows any candidate.

**Dependencies:** none.

---

#### WF-10 — After the COS decision, the rest of the pool strands silently

**Role:** HRM Chief · **Screens:** COS decision + deliberation console · **Type:** Feature

**Problem (found live).** I recorded "Selected" for the deliberation pick; that case moved to completion and its applicant was notified — but the other four cases **stayed at `hrm_chief_review`** with no notification and no prompt. Contrast Plantilla: recording the final selection auto-rejected all non-selected candidates and sent each a non-selected email (verified in the notification log). COS applicants who lost would simply never hear, and the chief's queue silently keeps four dead cases.

**Change specification.** In the COS decision service (the path that records the Selected outcome for the deliberation pick), mirror the Plantilla behavior: when the chosen case's Selected decision is recorded, mark every other ACTIVE case in the same vacancy pool as Not selected (rejected/closed, same terminal handling the Plantilla path uses), send each `queue_non_selected_applicant_notification`, and write the audit events. Confirm-modal copy addition on the decision: *"Recording this selection will also mark the other {{ N }} candidates in this vacancy as not selected and notify them."*

**Acceptance criteria.**
- Recording the COS Selected decision closes the sibling cases as not selected, and the notification log shows a non-selected email per sibling.
- The chief's queue no longer lists the closed siblings; the Plantilla path is unchanged.
- A COS "Not selected" decision recorded manually on an individual case (the existing path) still works.

**Dependencies:** WF-09 (same flow; land together).

---

### Minor

---

#### WF-11 — CAR draft displays an alphabetical order as ranks #1–#4 before scores exist

**Roles:** CAR preparer · **Screen:** CAR draft · **Type:** Design-only

**Problem (found live).** With ETE blank, every candidate's assessment score is None, and the draft still renders "#1…#4 / System rank #N" — computed by the sort fallback `(-score, reference_number)` ([services.py:5264-5272](../recruitment/services.py)), i.e. alphabetical by reference. Live, a candidate with a strictly worse exam and equal interview ranked above a better one. A hurried preparer (or a board member glancing at a projected draft) can mistake this for a real ranking.

**Change specification.** In the CAR draft template/context: when a row's `assessment_score` is None, render *"Unranked — enter the ETE rating"* instead of an ordinal, and suppress the "#N" badge until every ranked row has a score. (Data change not needed; presentation only. The tie-break itself is addressed in Section 5-B.)

**Acceptance criteria.**
- A fresh draft with blank ETE shows all candidates as "Unranked", no ordinals.
- Entering all ETE ratings and updating restores the ranked view exactly as today.

**Dependencies:** pairs with WF-05.

---

#### WF-12 — The chief's re-validation demands a second, fictional exam schedule

**Role:** HRM Chief · **Screen:** chief-stage exam section · **Type:** Feature (small)

**Problem (found live).** At the chief's stage the exam record can't be finalized until an exam *schedule* exists **for that stage** ("Schedule the examination and notify the applicant before recording final results." — hit both in the UI and via the service). But the exam already happened at the Secretariat stage; the chief is re-validating a past event and must invent a second schedule (which also re-emails the applicant an "exam invitation" for an exam they already took — misleading).

**Change specification.** In the schedule-before-record guard inside `save_exam_record` (~[services.py:4157](../recruitment/services.py)): satisfy the requirement if a finalized exam schedule exists from **any prior review stage** of the same application. Do not send a new applicant notification in that path.

**Acceptance criteria.**
- A chief-stage exam record on a case whose Secretariat-stage exam was scheduled finalizes without creating a new schedule and without a new exam-invitation email.
- A genuinely unscheduled exam (fresh case) is still blocked as today.

**Dependencies:** none.

---

#### WF-13 — "Notify HRMPSB panel" appears on COS interviews (no HRMPSB in COS)

**Role:** HRM Chief · **Screen:** COS case interview session manager · **Type:** Design-only

**Problem (found live).** The COS session manager shows the Plantilla "Notify HRMPSB panel" button; clicking it would email board members about an interview they have no role in.

**Change specification.** In `interview_session_manager.html`, wrap the panel-notify button in `{% if application.branch == "plantilla" %}` (or gate on the recipients helper returning non-empty for the case's flow).

**Acceptance criteria.** COS session manager shows only Save/Notify-applicant/Finalize; Plantilla unchanged.

**Dependencies:** none.

---

#### WF-14 — Panel members get one email per candidate for a single batch schedule

**Role:** HRMPSB · **Type:** Feature (small)

**Problem (verified in the email log).** Scheduling a 4-candidate batch sent each panel member **four near-identical "interview scheduled" emails** (one per session; 8 panel emails + 4 applicant emails for one act). At realistic pool sizes this trains the panel to ignore the notifications.

**Change specification.** In the batch interview scheduling path (`vacancy_batch_interview` view → `queue_interview_session_scheduled_notifications`), send panel members **one digest per batch action**: subject "Interview schedule: {{ vacancy }} ({{ N }} candidates)", body listing candidates + the shared date/venue. Keep per-applicant notices as-is; keep per-case scheduling (single session) emailing as today.

**Acceptance criteria.**
- Batch-scheduling N candidates yields exactly one email per panel member (containing all N) and N applicant notices.
- Scheduling a single case still sends the single-session panel notice.

**Dependencies:** none.

---

#### WF-15 — COS "Finalize deliberation" is one un-confirmed click

**Role:** HRM Chief · **Screen:** `/internal/vacancies/<pk>/deliberation/` · **Type:** Design-only

**Problem (found live).** "Finalize deliberation" moved the whole pool to the decision step immediately — no confirm modal — while the sibling batch actions (exam batch finalize, screening finalize, CAR finalize, decision) all confirm. Inconsistent treatment of an irreversible batch action.

**Change specification.** Route it through `modal_confirm.html` (`variant="destructive"`): title *"Finalize the deliberation pick?"*, body *"This records {{ chosen name }} as the board's pick and moves all {{ N }} candidates to the decision step. It can't be changed without an authorized reopen."*

**Acceptance criteria.** Finalize opens the modal naming the pick and pool size; Cancel changes nothing; confirm behaves as today.

**Dependencies:** WF-09 (same template).

---

### Polish

---

#### WF-16 — Small verified copy/affordance touches

**Type:** Copy / Design-only. (a) The exam-weights card on the batch console ("Overall = General × 60% + Technical × 40% — Edit weights") is good; add the lock state copy *"Locked when scoring starts"* so the editability window is explicit (weights lock at first rating — [models.py:1515-1520](../recruitment/models.py)). (b) On the batch schedule forms, submitting the schedule form currently also reveals the score-grid's "Scores must be between 0 and 100" hint area — suppress score-grid errors until the scores section is touched. (c) Record a light "first deliberation held on {{ date }}, members present N" optional note on the interview batch finalize (supports the two-touchpoint story without new process).

**Acceptance criteria.** Each is a one-screen visual check per the description.

---

**Verified as already correct (no items):** batch exam past-date server block; editable general/technical weights (60/40 defaults, sum validation, per-vacancy, lock-on-scoring); CAR unlock gated on vacancy closure; CAR total auto-computed (formula verified live to two decimals: 80×.4 + 85.6×.2 + 100×.4 = 89.12); board's #1 tagged on the AA screen; all six locked-decision notification triggers fire (exam invitation, interview notice to applicant AND panel, handoff assignment, selected, non-selected — email log evidence); resubmitted documents reopen blank for re-review; reopen is HRM-Chief-only with a required, audited reason ([services.py:8005-8079](../recruitment/services.py)).

---

## 4. Suggested implementation order

**Batch 1 — template/dispatch fixes and copy (low risk, independent):** WF-06 (now Design-only: `min` attributes), WF-08, WF-09, WF-11, WF-13, WF-15, WF-16. One commit.

**Batch 2 — the three HARD governance rules (the point of this plan):** WF-01, WF-02, WF-03, plus WF-04 (one-constant change with queue-routing retest). Server-side validations with model + service + modal changes; heavy test updates expected (existing tests finalize sessions with one rating and CARs without quorum — update fixtures to satisfy the new rules rather than weakening the rules).

**Batch 3 — publication guard:** WF-07 (configurable period + close-before-closing-date block).

**Batch 4 — flow features:** WF-05 (ETE restructure — the largest single item; model migration + two screens), WF-10 (COS pool resolution), WF-12, WF-14. Feature logic with migrations and notification changes.

After each batch: `manage.py test recruitment` (empty-string `POSTGRES_*` for the sqlite fallback), then a live re-run of the affected flow with the `ux_*` accounts.

---

## 5. Recommendations on the judgment items

**A. Absent / Waived exam handling.** Confirmed in code: `_auto_advance_after_exam_finalized` ([services.py:1613-1657](../recruitment/services.py)) never looks at `exam_status`, so an Absent or Waived candidate advances and reaches the CAR with a blank exam score — which, combined with WF-11's None-score ranking fallback, silently corrupts the ranking. **Recommendation: adopt the client's leaning — at exam finalization (single and batch), candidates whose exam status is Absent or Waived do not advance.** Route them to the existing "removed from batch" terminal handling with the status recorded as the reason and a notification to the applicant (the non-selected template with adjusted copy: *"…because you did not take the scheduled examination"* / *"…your examination was waived and the application did not proceed"*). Keep the two statuses distinct in the record (they answer different audit questions). On "excused but still a candidate": I recommend **against** it as a default — a candidate with no exam score cannot be ranked under the fixed ETE 40/Exam 20/Interview 40 formula without inventing a score, which the constraints forbid. If the office ever needs a genuine waiver-with-score (e.g., exam-exempt eligibility), that is a policy decision to bring back to the client; the safe system behavior today is do-not-proceed. Implementation shape: a status check in the two finalize paths + confirm-modal line "N candidates marked Absent/Waived will not proceed"; HARD.

**B. Messy real-life cases.**
- **Withdrawal mid-process:** `WITHDRAWN` status exists, but there is no service, view, or button (verified NOT FOUND). Recommend: a Secretariat-recorded "Applicant withdrew" action on the case (reason required, audited, terminal, applicant notified with a confirmation), available at any pre-decision stage. Applicant self-service withdrawal stays out (Help page already routes it to the desk; identity/coercion concerns).
- **Applies to multiple positions:** allowed by design (uniqueness is per applicant+position, [models.py:830-833](../recruitment/models.py)) and should stay allowed. Recommend visibility, not restriction: a small chip on the case header for internal staff — *"Also applied to: {{ other position }} ({{ status }})"* — so a deliberating board isn't surprised. No applicant-facing change.
- **Ties in CAR ranking:** today equal totals are silently ordered by reference number and given *different* ranks ([services.py:5264-5272](../recruitment/services.py)) — an arbitrary, undocumented tie-break that would be hard to defend in an audit. Recommend competition ranking (equal scores share the rank, next rank skips) plus a visible flag on the CAR: *"Tied at rank N — board to resolve order"*, leaving resolution to the board's recorded recommendation (which the CAR finalize already captures). Do not auto-break ties by any proxy.
- **Reopening a locked stage:** current design is right — HRM Chief only, reason mandatory, audited ([services.py:8005-8079](../recruitment/services.py)). One addition: after a reopen, show the reopen reason as a banner on the case header until the stage re-finalizes (today it lives only in Case History), so everyone touching the reopened case knows why.

---

## 6. Exploratory / bigger-bet ideas (for the human — not for the implementer)

1. **Deliberation-day board packet.** A single screen (and print stylesheet) per vacancy for the face-to-face deliberation: the ranked CAR draft, each candidate's E/T/E basis lines, exam breakdown, per-member interview scores (visible post-finalize), side by side. The board deliberates off-system by rule — this gives the room one shared artifact instead of the Secretariat flipping between cases on a projector. Helps: HRMPSB + Secretariat on deliberation day. Effort: medium (read-only view over existing data). Risk: screens/printouts leaking member-level scores — gate behind the CAR-preparer role and watermark with the session date.
2. **Per-vacancy pipeline strip.** On the batches console, replace the single "N cases awaiting your action" count with a stage mini-funnel (Screening 2 · Exam 4 · Interview 0 · …), clickable into the batch filtered by stage. Helps: Secretariat triage and the "where is everything" question the client keeps asking. Effort: low-medium (one annotated query + a component). Risk: little — purely additive.
3. **Interview-day rater queue.** For a scheduled batch, give each HRMPSB member a "today's interviews" strip (candidate order, done/pending per rater) so panels move through 10 candidates without hunting the queue between interviews. Helps: HRMPSB on interview day. Effort: medium. Risk: duplicating the queue concept — worth prototyping only if panels report friction after WF-01's indicator lands.

---

## 7. Out of scope / escalate

- **Sysadmin role and applicant portal** — covered by `docs/uiux-improvement-plan.md` and `docs/uiux-applicant-plan.md` (both implemented); reference only.
- **A11y/visual-consistency backlog** (focus rings landed; choice-strip keyboard model, blue/green accent unification, locked-frame convergence) — tracked in `docs/internal-uiux-review.md`; not duplicated here.
- **SECURITY escalation (standing):** the repo `.env` has `CAPTCHA_ENABLED=False` with reCAPTCHA keys present — production posture still needs the dedicated security review (first flagged in the applicant plan; unresolved).
- **Test-data quirk:** seeded "e2e" cases (e.g. the Health Program Researcher COS case) have no uploaded documents, so their screening can't be completed through the real rules — a seeding gap, not a product bug; note for whoever maintains `seed_demo_data`/`seed_e2e_test_cases`.
- **Demo-DB mutations from this review:** entry 167 (Administrative Aide IV) was driven to a finished lifecycle — screenings/exams/interviews finalized, CAR finalized (quorum 3, board pick #1), AA selected rank-2 Dennis Aguilar (test of the justification gap), siblings auto-rejected; entry 167 was also **closed 12 days early** (the WF-07 probe); entry 168 (Administrative Assistant I COS) driven through chief decision (Niño Muñoz-O'Brien selected; 4 siblings intentionally left stranded as WF-10 evidence); an extra `ux_hrmpsb2` account was created. Run `seed_demo_data` to reset.
- **The unapplied-migration warning** on the demo Postgres DB (`manage.py migrate`) — pre-existing ops note.
- **Deferred by owner decision (2026-07-04):** the weekend/8am–5pm confirm, PH-holiday warning, cross-stage date ordering, venue double-booking, and short-notice warning from the client's original scheduling list. No threshold/calendar was ever defined for them; the owner chose to defer rather than ship placeholder values. Full specs preserved in this file's git history (pre-2026-07-04 version of WF-06) — bring back to the client for values before implementing.
