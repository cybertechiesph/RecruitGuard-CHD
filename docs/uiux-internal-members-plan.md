# RecruitGuard-CHD — Internal Workflow Roles UI/UX Improvement Plan

_Produced 2026-07-02 from a hands-on browser review of the running app as all four workflow roles (Secretariat, HRM Chief, HRMPSB Member, Appointing Authority): cases were actually screened, exam-scored, batch-finalized, and role-switched; every claim below was observed live or verified in source. Findings are traced to exact files with line references verified against the current `main`._

## 1. Context

RecruitGuard-CHD is a Django app for DOH–CHD CALABARZON managing the HRMPSB hiring pipeline (publication → qualification screening → exam → interview → CAR → deliberation → appointing authority → appointment). Users are government HR staff. Internal routes: `recruitment/internal_urls.py`. Templates: `templates/` (shared chrome, `templates/internal_includes/` wizard bodies) and `recruitment/templates/recruitment/` (pages). CSS: `static/css/recruitguard.css`, `static/css/recruitguard-doh-brand.css`. JS: `static/js/recruitguard-autosave.js`, `static/js/rg-wizard-validation.js`.

**How to run locally (Windows):**

- `.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8058` with `DJANGO_DEBUG=True`, `SECURE_SSL_REDIRECT=False`, `INTERNAL_MFA_ENABLED=False` (allowed only in DEBUG).
- **DB gotcha:** `config/settings.py` runs `load_dotenv(BASE_DIR/".env")` and `.env` points at local Postgres `recruitguard_chd` (the demo dataset). *Unsetting* `POSTGRES_*` (cmd `set VAR=`) still lands on Postgres via `.env`; setting them to *empty strings* (Git Bash `POSTGRES_DB= …`) falls back to the stale `db.sqlite3`. Use Postgres — that is where the demo volume lives. `.claude/launch.json` has a ready config `recruitguard-uxreview-nomfa` (port 8058) with all of this.
- **Data:** the Postgres demo DB holds ~31 applications across 4 role stages. If lists look empty or you want a clean slate, run `manage.py seed_demo_data` (purges synthetic data and loads ~27 fresh applications). Note: this review advanced the “Administrative Assistant I (RG-COS-2026-0003)” batch through screening and exam — reseed if you need it back at screening.
- **Test accounts** (already in the Postgres demo DB, password `UxReview!2026`): `ux_secretariat`, `ux_hrmchief`, `ux_hrmpsb`, `ux_appointing` — one per workflow role. Do not use `demo_seed_bot` (unusable password). Recreate via `manage.py shell` (`RecruitmentUser`, set `role`, `set_password`) if missing.

**Intentional design decisions — do NOT undo:**

1. CAR formula is ETE 40 / Exam 20 / Interview 40. Never change the weighting or math.
2. CAR sheet and interview rating-sheet templates are secretariat-editable. Do not hardcode them.
3. Completion requirements are handler-supplied free-text/formset with an auto-defaulted 2-week deadline. Do not convert to a fixed checklist.
4. The audit log shows humanized case-activity descriptions — keep the tone.
5. **“See only what you need” access model**: roles see only cases assigned to their role (plus support carve-outs and closed-case access). Nothing below plans a global case browser; every list/search feature must stay inside `get_queue_for_user` / `_user_has_closed_application_access` scoping (`recruitment/services.py:1089`, `:1143`).
6. **Two-pass validation**: when a case reaches HRM Chief review, the Chief performs their own screening/exam/interview records at their review stage (`_workflow_detail_sequence`, `recruitment/services.py:2474`). This independent re-validation is a workflow rule — items below improve its *presentation*, not remove it.

---

## 2. Prioritized change items

### Critical

---

#### UX-01 — The “silent autosave” safety net is dead on every workflow wizard

**Roles:** Secretariat, HRM Chief, HRMPSB Member (all wizard users) · **Type:** Feature (JS load-order fix + backend draft-validation fix)

**Problem.** The five long wizards (screening, exam, interview session, CAR, deliberation) advertise background draft-saving (“silent autosave”, an `aria-live` indicator), but no autosave ever fires. An interviewer or screener who navigates away, gets logged out, or loses the tab loses everything since their last manual “Save draft”. During this review, a filled exam form (date, status, two scores, validity window) was lost twice on reload. Violates: error prevention; visibility of system status (the indicator never changes, so users can’t tell autosave is broken).

**Evidence.** Reproduced on `/internal/applications/<pk>/` for both screening and exam wizards: made trusted edits, waited 4 s (debounce is 1.5–1.8 s) — network monitor and server log show **zero POSTs**; the indicator stays `rg-autosave is-idle`. Two causes, both verified:

1. **The attach script runs before the library loads.** Each wizard body ends with an inline script like `templates/internal_includes/screening_body.html:911-925`: `if (!(window.RG && window.RG.attachAutosave)) return;`. These inline scripts execute where they appear (inside `<main>`), but `recruitguard-autosave.js` is loaded in `{% block extra_scripts %}` at the **end of `<body>`** (`templates/internal_base.html:47`). `window.RG` is always undefined at attach time, so the guard silently bails. Confirmed by manually calling `RG.attachAutosave(...)` after load — it then fires.
2. **Draft saves are rejected server-side.** When forced with production parameters (`operation=save`, header `X-Requested-With: RG-Autosave`), a partial screening draft returned **HTTP 400**: `ScreeningReviewView.post` (`recruitment/views.py:1439-1441`) returns `_autosave_response(False)` whenever `ScreeningReviewForm` is invalid — which is almost always true mid-editing. So even after fix (1), autosave would flash “Couldn’t save — try again” for most of the session.

**Change specification.**

1. Fix load order once, in each of the five templates that call `attachAutosave` (`templates/internal_includes/screening_body.html:911-925`, `exam_body.html:762-770`, `interview_session_manager.html`, `comparative_assessment.html`, `deliberation_recorder.html` — grep `attachAutosave`): wrap the attach body in `document.addEventListener("DOMContentLoaded", function () { … });` (end-of-body scripts have executed by then, so `window.RG` exists). Keep the existing `if (!(window.RG…)) return;` guard inside as a fallback.
2. Make draft saves partial-tolerant. In `ScreeningReviewForm` (`recruitment/forms.py:1466`) and the analogous exam/interview/deliberation forms, accept an `is_draft=True` mode that skips completeness requirements (blank document statuses, unset outcomes) and validates only what is present (ranges, formats). The views already distinguish `operation == "finalize"` vs `"save"` (`recruitment/views.py:1417-1426`, `1463-1465`) — pass `is_draft=(operation != "finalize")` into the form and keep finalize validation exactly as-is. `save_screening_review` already persists drafts (`finalize=False`); confirm it tolerates partial cleaned_data.
3. Do not touch the finalize paths, the confirm modals, or the manual “Save draft” buttons.

**Acceptance criteria.**
- Edit one document-status dropdown on a screening case, wait 3 s: the indicator shows “Saving…” then “Saved”, the server log shows `POST …/screening/ 204`, and reloading the page shows the saved value.
- The same works on the exam wizard (`exam_date` alone) and the interview/deliberation/CAR forms.
- A finalize attempt with incomplete data is still rejected exactly as today (inline errors, no lock).
- With JS disabled, manual “Save draft” still works unchanged.

**Dependencies.** None — do this first; it de-risks every other wizard change.

---

#### UX-02 — My Queue buries overdue cases and offers no search, stage filter, or pagination

**Roles:** all four (Secretariat and HRM Chief heaviest — 12 and 22 live cases in demo data) · **Type:** Feature

**Problem.** `/internal/workflow/queue/` renders the role’s queue in database order with only branch chips (All / Plantilla / COS). Observed live: a case **“In Screening for 35 days”** (7-day target) rendered **last of 8 rows**, below seven fresh “In Screening today” cases. There is no way to search by applicant name/reference/position, no filter by needed step, no sort, and no pagination — with a realistic intake (the office receives dozens of applicants per vacancy), finding one case means scrolling. Violates: visibility of system status; flexibility & efficiency for the heavy user; the office’s SLA model is undermined by its own ordering.

**Evidence.** Queue row order captured in-browser (overdue case last); `get_queue_for_user` (`recruitment/services.py:1089-1140`) has **no `order_by` at all**; `WorkflowQueueView` (`recruitment/views.py:960-970`) is an unpaginated `ListView`; the only controls in `recruitment/templates/recruitment/application_list.html` are the three branch chips.

**Change specification.**

1. `recruitment/services.py` — give `get_queue_for_user` a deterministic urgency ordering before `return`: annotate stage age and sort overdue-first, oldest-first. The SLA fields live on the case (`case.stage_entered_at` feeds `stage_sla_context`); the simple, correct proxy is `queryset.order_by("case__stage_entered_at", "pk")` (oldest stage-entry first ⇒ overdue naturally on top). Verify the actual field name on `RecruitmentCase` (the model property `stage_sla_context` reads it) and use that.
2. `recruitment/views.py`, `WorkflowQueueView`:
   - `paginate_by = 25`.
   - Read `q` (search) and `step` (stage filter) from `request.GET`; filter the queryset with
     `Q(applicant__first_name__icontains=q) | Q(applicant__last_name__icontains=q) | Q(reference_code__icontains=q) | Q(position__title__icontains=q)` (verify the reference field name — the UI shows `RG-20260701-…` via `reference_label`; grep the model for the underlying column) and `step` against the current-section key when set.
   - Pass a `querystring` context (GET params minus `page`) for pagination links, same pattern as the audit-log pagination item in `docs/uiux-improvement-plan.md` UX-02 (reuse `templates/internal_includes/pagination.html` if that plan landed; otherwise create it as specified there).
3. `recruitment/templates/recruitment/application_list.html`:
   - Above the table, add a GET form: text input `q` labeled “Search applicant, reference, or position”, a `step` select built from `QUEUE_TASK_LABELS` (`recruitment/templatetags/recruitment_ui.py:387-396`), a “Apply” submit and a “Reset” link to the bare URL. Keep the existing branch chips working alongside (they are client-side; verify and keep).
   - Render the pagination include under the table.
   - Row count line (“N cases”) should reflect the filtered total, not the page.
4. Apply the same ordering benefit to the batch console for free: `VacancyBatchConsoleView`/`VacancyBatchDetailView` group the same queryset (`recruitment/views.py:973-979`), so the per-batch case tables inherit the new order — verify, don’t re-implement.

**Acceptance criteria.**
- As `ux_secretariat`, the 35-day “Administrative Aide I” case is the **first** row of My Queue (after reseed, the oldest case leads).
- Typing “Mercado” in the search and applying shows only Rowena Mercado’s case; Reset restores the full queue.
- Selecting step “Screening” hides non-screening rows; the branch chips still work on the filtered result.
- With >25 rows (seed more or lower `paginate_by` to test), page 2 preserves `q`/`step` in the URL.

**Dependencies.** Pagination partial from `docs/uiux-improvement-plan.md` UX-02 (share it; do not create two).

---

#### UX-03 — HRM Chief re-keys every record blind: prior-stage results are never shown

**Roles:** HRM Chief (22 cases in demo data); same pattern hits HRMPSB on Plantilla interview · **Type:** Feature

**Problem.** When a case reaches HRM Chief review, the Chief must produce their own screening (7 document dropdowns + completeness + qualification) and exam records — the intentional two-pass validation. But the wizard they get is **completely blank** and the page shows **none** of the Secretariat’s finalized results: not the per-document statuses, not the screening outcome, not the exam score (a case with a finalized score of 92.00 showed no “92” anywhere on the Chief’s page — verified). The Chief’s queue also labels these cases “Needed Step: **Screening** / In Screening today”, indistinguishable from a fresh case. The Chief re-derives everything from the paper file while the system already holds the answer one review-stage away. Violates: recognition over recall; flexibility & efficiency; match with the real endorsement process (the Chief is *checking* the Secretariat’s work, so show them the work being checked).

**Evidence.** As `ux_hrmchief` on `/internal/applications/340/` after Secretariat finalized screening (all “Meets”, Complete, Qualified) and exam (92.00): all 7 `document_status__*` selects empty, `completeness_status` empty, exam score absent from the DOM. Data source exists: `get_screening_records(application)` returns **all** review stages’ records (`recruitment/services.py:2329-2335`); `application.exam_records` likewise carries the Secretariat-stage record; `ApplicationDetailView.get_context_data` already puts `screening_records` (plural) in context (`recruitment/views.py:427`).

**Change specification.**

1. New include `templates/internal_includes/prior_stage_summary.html`: a read-only card titled **“Earlier review — Secretariat”** listing, when a finalized record from an earlier review stage exists: per-document statuses (label + status pill), completeness, qualification outcome, screening notes, and the finalized exam (status, general/technical scores, exam date). Use the existing locked-body visual language (`rg-scr-elig-bar` / result strip) — do not invent a new component.
2. `recruitment/views.py`, `ApplicationDetailView.get_context_data`: add
   `context["prior_screening_record"]` = the latest **finalized** screening record whose `review_stage` differs from `get_current_review_stage(application)` (filter `screening_records`), and `context["prior_exam_record"]` similarly from `application.exam_records`. Only set them when the current user’s stage is HRM Chief review or HRMPSB review.
3. `recruitment/templates/recruitment/application_detail.html`: render the include directly **above** the active wizard section whenever `prior_screening_record` or `prior_exam_record` exists.
4. Optional efficiency add-on (same item, do second): a **“Copy results from Secretariat review”** button inside the Chief’s screening step 1 (anchor: `templates/internal_includes/screening_body.html:28-46`) that client-side copies the prior record’s per-document statuses/completeness/qualification into the form fields (data exposed via `data-*` attributes on the summary include). Copying is an explicit act, so the independent-check intent is preserved; the Chief reviews and can change any value before finalizing.
5. Do **not** auto-prefill, do not skip the Chief’s finalize, do not alter `_workflow_detail_sequence`.

**Acceptance criteria.**
- As `ux_hrmchief` on a case the Secretariat finalized, a read-only “Earlier review — Secretariat” card shows the 7 document statuses, “Complete”, “Qualified”, and the exam score, above the blank wizard.
- Clicking “Copy results from Secretariat review” fills the Chief’s dropdowns/choices to match; every field remains editable; finalize still requires the confirm modal.
- As `ux_secretariat` on a fresh case, no such card renders.
- The card never renders draft (unfinalized) prior records.

**Dependencies.** UX-01 (wizard forms should save reliably before adding a bulk-fill affordance).

---

### Major

---

#### UX-04 — Case History exists in the DOM but is unreachable for every role

**Roles:** all four (Secretariat relies on it most — their per-case audit URL is 403) · **Type:** Design-only (a fix already exists on a branch)

**Problem.** Every case page renders a full “Case History / Case Updates” timeline (uploads, submission, routing, screening finalized — with actor and timestamp) inside `#cws-timeline`, permanently `display:none`, with **no** link, tab, or disclosure that reveals it. The Secretariat cannot use `/internal/applications/<pk>/audit/` either — it returns 403 for workflow roles (verified) — so the hidden panel is their *only* view of the trail they need for endorsement defensibility. Violates: visibility of system status; recognition over recall.

**Evidence.** On `/internal/applications/340/` as Secretariat: `#cws-timeline` computed `display:none`; zero elements matching history/audit/timeline/evidence in links or buttons; the timeline content is present in `main.textContent`. `fetch('/internal/applications/340/audit/')` → 403.

**Change specification.** A reviewed fix already exists: commit `c37210a` “Make Case History and Evidence reachable in the case shell” on branch **`feat/internal-uiux-safe-tier`** (with sibling commits `ed4e055` focus ring, `3680bf1` locked-body glyphs). **Prefer cherry-picking/merging that commit over re-implementing.** If it does not apply cleanly, implement minimally: in `recruitment/templates/recruitment/application_detail.html`, replace the dead-switcher hiding of `#cws-timeline` with a native `<details class="rg-cws-history">` disclosure titled “Case history” placed after the active stage section, and delete the orphaned `.js-cws-tab` switcher JS (documented as dead in `docs/internal-uiux-review.md` §2.1).

**Acceptance criteria.**
- Every role that can open a case can expand “Case history” and read the timeline without editing the URL.
- No dead `.js-cws-tab` JS remains.
- The per-case audit URL permissions are unchanged (still 403 for workflow roles).

**Dependencies.** None.

---

#### UX-05 — Screening step 1 forces seven identical dropdowns per applicant with no “mark all” shortcut

**Roles:** Secretariat (every fresh case), HRM Chief (their re-validation pass) · **Type:** Design-only (template + small JS)

**Problem.** Step 1 of the screening wizard requires setting a status for each of 7 required documents via individual `<select>`s, all defaulting to “Select status”. For the common case — a complete folder where everything “Meets” — that is 7 dropdown interactions per applicant, ×27 applicants per vacancy, ×2 passes (Secretariat then Chief). This is the single most repeated interaction in the office’s bottleneck role. Violates: flexibility & efficiency (no accelerator for the frequent path).

**Evidence.** Observed on every screening case: 7 selects named `document_status__*` (options: Meets / Request Resubmission / Absent, `recruitment/forms.py:1486-1499`), no bulk control anywhere on the page (button scan returned none). Anchors: heading `templates/internal_includes/screening_body.html:28`, document table `:45-46`.

**Change specification.**

1. In `templates/internal_includes/screening_body.html`, in the Step-1 panel header row (next to the “Step 1 — Review the documents” title, line 28), add:
   ```html
   <button type="button" class="btn btn-outline-secondary btn-sm" id="scr-mark-all-meets">
       Mark remaining as “Meets”
   </button>
   <span class="visually-hidden" role="status" id="scr-mark-all-status"></span>
   ```
2. In the same template’s existing wizard script, wire it: set every `select[name^="document_status__"]` whose value is empty to `meets`, dispatch `change` on each (so validation, autosave, and the review-summary all update), and write “N documents marked as Meets — review each row before finalizing.” into `#scr-mark-all-status`.
3. It must **not** overwrite selects already set to Request Resubmission/Absent, and must not touch remarks fields.

**Acceptance criteria.**
- On a fresh screening case, one click sets all 7 selects to “Meets”; the finalize summary reflects them; the change is announced to screen readers.
- Pre-set “Request Resubmission” on one row, then click the button: that row is untouched.
- Each select remains individually editable afterward.

**Dependencies.** UX-01 (dispatched `change` events should trigger a working autosave, not a dead one).

---

#### UX-06 — “Finalize exam batch” is one un-confirmed click that irreversibly advances the whole pool

**Roles:** Secretariat (batch exam console) · **Type:** Design-only + Copy

**Problem.** On `/internal/vacancies/<pk>/exam/`, the “Finalize exam batch” button POSTs immediately — no confirmation — locking every candidate’s exam score and advancing the entire pool to the next handler. Compare: finalizing a *single* screening gets a “Finish and lock?” modal echoing the case and decision. A batch-wide irreversible action gets nothing. Two adjacent problems: (a) the schedule-first precondition surfaces only *after* a failed submit (“Schedule the examination and notify the applicant before recording final results.”); (b) the success toast — “Exam batch finalized — the pool advanced together.” (`recruitment/views.py:1140`) — never says **where** the pool went, and the batch instantly vanishes from the Secretariat’s console (the cases 404 for them thereafter), which reads as data loss. Violates: error prevention (irreversible action unconfirmed); visibility of system status.

**Evidence.** All three observed live while advancing the Administrative Assistant I batch: unconfirmed finalize (`modalOpen: false` on click, then the batch advanced), the after-submit precondition error, and the destinationless toast followed by the vacancy disappearing from `/internal/vacancies/`.

**Change specification.**

1. `recruitment/templates/recruitment/vacancy_batch_exam.html`: route the finalize button through the existing confirm modal partial (`templates/internal_includes/modal_confirm.html`), `variant="destructive"`, `title="Finalize this exam batch?"`, body copy exactly:
   > “This locks the recorded exam scores for all {{ candidate_count }} candidates and moves the whole pool to the next review step. Scores can no longer be edited afterwards.”
   with `confirm_label="Finalize Exam Batch"` and `form_id` pointing at the finalize form.
2. Same template: when the exam schedule has not been sent yet, render an inline notice directly above the finalize button (reuse the `rg-*` status style used by the on-hold banner): “Send the exam schedule to the batch (section 1) before finalizing.” Keep the button enabled (do not use the disabled-button anti-pattern) — the server guard stays as backstop.
3. `recruitment/views.py:1140`: change the success message to name the destination, e.g.
   `f"Exam batch finalized — all {count} candidates advanced to {next_role_label} review."`
   (the next handler role is known where the message is emitted; use the existing `role_label` mapping).

**Acceptance criteria.**
- Clicking “Finalize exam batch” with scores saved opens a modal naming the candidate count; Cancel changes nothing; Confirm finalizes.
- Before the schedule is sent, the inline notice is visible without submitting; after sending, it disappears.
- The success toast names the destination role (e.g. “advanced to HRM Chief review”).

**Dependencies.** None.

---

#### UX-07 — After finalizing a case there is no path to the next case in the same batch

**Roles:** Secretariat, HRM Chief (any per-case wizard user working a batch) · **Type:** Feature (small)

**Problem.** The office works “batch per vacancy” (the console says so), but after finalizing a stage the user stays on the finished case, and the only navigation out is “← Back to queue”, which goes to the **flat all-vacancies queue** (`templates/internal_includes/case_header.html:24` hardcodes `{% url 'workflow-queue' %}`) — not the batch they came from. Screening 27 applicants therefore costs, per applicant: open batch → open case → work → finalize → back to *flat queue* → re-open batch → find the next un-screened row. Violates: match with the real batch-based process; efficiency for the heaviest flows.

**Evidence.** Observed on every finalize during this review: after “Screening finalized and locked.” the page stays on the case, “← Back to queue” → `/internal/workflow/queue/`; no “next case” affordance exists anywhere in the shell.

**Change specification.**

1. `recruitment/views.py`, `ApplicationDetailView.get_context_data`: add
   `context["next_batch_case"]` — the next application in `get_queue_for_user(request.user)` filtered to the same `position` and excluding the current pk, ordered per UX-02 — and `context["batch_url"]` = `reverse("vacancy-batch-detail", args=[application.position_id])` when the position has a batch view for this user (it always does for queue members).
2. `templates/internal_includes/case_header.html:24`: replace the single hardcoded link with two links: “← Back to batch” → `{{ batch_url }}` and keep “Queue” as a secondary link to `workflow-queue`.
3. In the flash-message area after a finalize (the message renders on the case page), the simplest robust hook: in `recruitment/templates/recruitment/application_detail.html`, when `next_batch_case` exists, render a persistent action strip under the case header:
   ```html
   <div class="rg-next-case-strip">
       Next in this batch: <strong>{{ next_batch_case.applicant.get_full_name }}</strong>
       <a class="btn btn-rg-primary btn-sm" href="{% url 'application-detail' next_batch_case.pk %}">Open next case</a>
   </div>
   ```
   Render it only when the current case is **not** awaiting the user’s action (i.e. the user just finished it or it belongs to someone else) — condition available via `user_can_process_application`.
4. Do not auto-redirect after finalize — the confirmation toast and locked view are part of the audit-trust design.

**Acceptance criteria.**
- Finalize a screening in a 4-case batch: a strip appears naming the next un-worked applicant; clicking it lands on that case’s wizard.
- “← Back to batch” from a case opened via a batch returns to that vacancy’s batch page, not the flat queue.
- On the last case of a batch, the strip does not render (nothing next).

**Dependencies.** UX-02 (shares queue ordering).

---

### Minor

---

#### UX-08 — Closed and rejected cases are viewable by permission but listed nowhere

**Roles:** all four (Secretariat/HRM Chief most affected for records requests) · **Type:** Feature

**Problem.** Once a case closes (approved/rejected), workflow roles retain view access (`_user_has_closed_application_access`, `recruitment/services.py:1143-1152`) — but no screen lists closed cases: the queue shows only `current_handler_role` actives, and batch pages show the same. The demo DB’s rejected case is reachable only by memorized URL. An HR office answering “what happened to X’s application last month?” has no lookup at all. Violates: recognition over recall; match with real record-keeping duties.

**Evidence.** Queue = `get_queue_for_user` (active, role-assigned only); `ApplicationListView` unconditionally redirects to the queue (`recruitment/views.py:383-387`); no template links closed cases; the seeded `rejected` case appears in no list as any role.

**Change specification.**

1. New service `get_closed_cases_for_user(user)` in `recruitment/services.py`: `RecruitmentApplication.objects.filter(status__in=[APPROVED, REJECTED])` (plus `case__current_stage=CLOSED` cases), then keep only those passing the existing `_user_has_closed_application_access(user, app)` rules (apply the same Level-2 secretariat exclusion as the helper — express it as queryset filters, not a Python loop, mirroring the helper’s conditions).
2. New view + URL: `path("workflow/closed/", ClosedCaseListView…, name="workflow-closed")`, a paginated (25) `ListView` reusing `recruitment/templates/recruitment/application_list.html` with `is_queue=False` context (the template already branches its title to “Cases I can view” — reuse that branch, retitle it “Closed cases”), plus the UX-02 search box scoped to this queryset.
3. Link it: in `application_list.html`, next to the branch chips on My Queue, add a quiet link “Closed cases →” (`workflow-closed`), and on the closed list a “← Back to My Queue” link.
4. Case detail for closed cases already renders the locked/completion view — no change there.

**Acceptance criteria.**
- As `ux_secretariat`, “Closed cases” lists the seeded rejected Level-1 case; opening it shows the locked case view.
- A Level-2 closed case does not appear for the Secretariat (matches `_user_has_closed_application_access`).
- Search by applicant surname filters the closed list.

**Dependencies.** UX-02 (search + pagination patterns).

---

#### UX-09 — HRMPSB member’s queue item opens to a contentless dead end while waiting on the CAR

**Roles:** HRMPSB Member · **Type:** Copy + Design-only

**Problem.** The member’s queue says “**1 case awaiting your action**”, but opening it shows: a “Comparative Assessment” titlebar with **no body at all**, and “Deliberation not yet available — this step will open when the case reaches the proper deliberation review.” Nothing tells the member that the CAR is being *prepared by another role* and that their own task starts afterwards. The page reads as broken, and the queue promise (“awaiting your action”) is false for this state. Violates: visibility of system status; help users understand where they are.

**Evidence.** As `ux_hrmpsb` on `/internal/applications/4/` (seeded Plantilla L2 case, “In Car for 39 days”): visible leaf-node dump shows the bare “Comparative Assessment” titlebar (`rg-cws-stage-titlebar__title`) with zero content beneath, then the locked deliberation message.

**Change specification.**

1. In `templates/internal_includes/comparative_assessment.html`, when the viewer cannot prepare the CAR and no finalized CAR exists, render a status body under the titlebar (reuse `templates/internal_includes/state_empty.html`):
   - title: “CAR in preparation”
   - copy: “The Comparative Assessment Report for this vacancy is being prepared by the {{ car_preparer_role_label }}. You will be able to record the deliberation once the CAR is finalized.”
   The preparer role is resolvable from `PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL` (`recruitment/services.py` — grep the constant) for the case’s level; pass it via the view context or a template tag.
2. In the queue row for such cases (`application_list.html`), the “Needed Step” cell already shows the CAR pill — add the secondary line “Waiting on CAR preparation” (plain `rg-imeta` text) when the case is in HRMPSB review without a finalized CAR and the viewer can’t prepare it. Do **not** remove the case from the queue (assignment semantics are workflow rules).

**Acceptance criteria.**
- As `ux_hrmpsb` on the seeded case, the Comparative Assessment section shows the “CAR in preparation” body naming the preparing role — no bare titlebar.
- The member’s queue row shows the “Waiting on CAR preparation” hint.
- As a role that *can* prepare the CAR, the normal CAR card renders unchanged.

**Dependencies.** None.

---

#### UX-10 — “In Car for 39 days”: the CAR stage label renders as a misspelled word

**Roles:** HRMPSB Member, HRM Chief, Appointing Authority (anywhere the SLA badge or queue pill shows the CAR section) · **Type:** Copy (one line)

**Problem.** The SLA badge and queue task pill print the section key through a `.title()` fallback, so `car` becomes “Car”: **“In Car for 39 days.”** In a government HR tool the acronym CAR (Comparative Assessment Report) rendered as “Car” reads as a typo and erodes trust. Violates: consistency; match with domain language.

**Evidence.** Observed on case 4’s header badge and queue row. Source: `QUEUE_TASK_LABELS` (`recruitment/templatetags/recruitment_ui.py:387-396`) has **no `"car"` key**, so `_queue_task_display` (`:501-505`) falls back to `section.title()`; the SLA badge inherits it via `stage_sla_label` (`:547-553`).

**Change specification.** In `recruitment/templatetags/recruitment_ui.py:387-396`, add `"car": "CAR",` to `QUEUE_TASK_LABELS` (and a matching `"car"` entry in `QUEUE_TASK_THEMES` beside it, e.g. `"info"`, if absent). Sanity-check other consumers of the section key render via `WORKFLOW_SECTION_LABELS` (already “Comparative Assessment”).

**Acceptance criteria.**
- Case 4’s badge reads “In CAR for 39 days”; its queue pill reads “CAR”.
- No other stage label changed.

**Dependencies.** None.

---

#### UX-11 — Overdue status is conveyed by color and a hover tooltip only

**Roles:** all four (queue rows and case headers) · **Type:** Design-only

**Problem.** A case 35 days into a 7-day stage shows visible text “In Screening for 35 days” — the words “overdue” / “over target” exist only in the `title` attribute and a red dot. Keyboard, touch, and screen-reader users (and anyone scanning quickly) get no explicit overdue signal, on the one cue the SLA model exists to surface. Violates: visibility of system status; WCAG use-of-color.

**Evidence.** `templates/internal_includes/stage_sla_badge.html:12-32`: state lives in the class + `title`; visible text is only “In {{ stage_name }} for N days”. Verified in the rendered queue row for the 35-day case. (Also flagged as B3 in `docs/internal-uiux-review.md` — this item implements it.)

**Change specification.** In `stage_sla_badge.html`, inside the badge when `sla.is_overdue`, append visible text: `· Overdue` (and for `sla.is_warning`: `· Due soon`), plus `<span class="visually-hidden">— over the {{ sla.overdue_days }}-day target</span>`. Keep the `title` as a supplement. Style the appended text with the existing `--overdue` / `--warning` badge colors; no new CSS tokens.

**Acceptance criteria.**
- The 35-day case’s badge visibly reads “In Screening for 35 days · Overdue” in queue rows and the case header.
- A fresh case shows no suffix.
- A screen reader announces the over-target qualifier.

**Dependencies.** None (coordinate with UX-02: overdue-first ordering plus a visible flag work together).

---

### Polish

---

#### UX-12 — Batch score grid rejects invalid input silently; “Last updated” timestamps are noisy

**Roles:** Secretariat (batch exam console); all roles (queue/batch “Last updated” column) · **Type:** Design-only + Copy

**Problem.** (a) Entering an out-of-range exam score (e.g. 150) in the batch grid and clicking “Save scores” does nothing visible except a native browser tooltip on the first invalid field — no inline error, inconsistent with the wizards’ show-all-errors pattern. (b) List rows show “23 hours, 12 minutes ago” — minute-level precision that adds noise, wraps the column, and differs from the SLA badge’s day-level language. Violates: consistency of validation feedback; aesthetic & minimalist design.

**Evidence.** Both observed live: score `150` (input has `min=0 max=100 step=0.01`) blocked with no page feedback (`recruitment/templates/recruitment/vacancy_batch_exam.html` score inputs); queue/batch “Last Updated” cells render `timesince` in full (“23 hours, 12 minutes ago Jul 01, 2026”).

**Change specification.**

1. `vacancy_batch_exam.html`: add a small submit-handler on the scores form — on invalid, `preventDefault`, add `.is-invalid` to each out-of-range input and render one `.invalid-feedback.d-block` line under the grid: “Scores must be between 0 and 100.” (mirror the wizard module’s tone; a full `RGWizardErrors` integration is not required here).
2. Timestamps: in `recruitment/templates/recruitment/application_list.html` and `recruitment/templates/recruitment/vacancy_batch_detail.html`, change `{{ …|timesince }} ago` to `{{ …|timesince|truncatewords:2 }} ago` — Django’s `timesince` puts the largest unit first, so this yields “23 hours ago” / “1 month ago”. Keep the absolute date line beneath as-is.

**Acceptance criteria.**
- Entering 150 and saving shows the inline error and highlights the field; entering 92 saves as today.
- Queue rows read “23 hours ago”, not “23 hours, 12 minutes ago”; the absolute date still shows.

**Dependencies.** None.

---

## 3. Suggested implementation order

**Batch A — copy + guardrails (independent, low risk, one commit):**
UX-06 (finalize modal + destination toast), UX-09 (CAR waiting copy), UX-10 (CAR label), UX-11 (overdue text), UX-12 (grid errors + timestamps). All template/label edits; no schema or queryset changes.

**Batch B — wizard reliability (one commit):**
UX-01 (autosave load order + draft-tolerant validation) first, then UX-05 (mark-all button), then UX-04 (cherry-pick `c37210a` from `feat/internal-uiux-safe-tier`, or the fallback disclosure). UX-05 depends on UX-01; UX-04 is independent but touches the same case shell — keep in one review.

**Batch C — list & flow features (highest risk, one commit or two):**
UX-02 (queue ordering/search/filter/pagination), then UX-07 (batch-aware navigation, shares the ordering), then UX-03 (prior-stage summary + copy-results), then UX-08 (closed-cases list, reuses UX-02 patterns). These add queries and context — run the full test suite (`manage.py test recruitment` with empty-string `POSTGRES_*` so tests hit sqlite) and re-verify role scoping manually after each: as each of the four `ux_*` accounts, confirm no case appears that the role could not open before.

## 4. Out of scope / deliberately not planned

- **Mobile navigation (sidebar hidden ≤768px with no toggle)** — affects these roles too but is already fully specified as UX-01 in `docs/uiux-improvement-plan.md`; implement it there, don’t duplicate.
- **Keyboard focus rings, locked-body Bootstrap-icon rot, choice-strip keyboard model, blue-vs-green accent unification, two visual generations** — documented in `docs/internal-uiux-review.md` (items A1/A2/B1/C1/C2) with partial implementations on branch `feat/internal-uiux-safe-tier`; merging that work is a separate decision.
- **Whether the HRM Chief should re-run the exam at all** (vs. only re-validating screening) — the two-pass, per-review-stage record model is a workflow rule; UX-03 surfaces prior results but the duplication itself needs client sign-off to change.
- **Removing blocked cases from the HRMPSB queue** (UX-09 keeps them listed with a hint) — queue membership is assignment semantics; changing it needs client input.
- **A global “all cases” browser or cross-role search** — would violate the intentional “see only what you need” access model; all search planned here stays inside existing scoping.
- **The Chief’s queue labeling their validation pass “Screening”** — renaming stages (“Chief validation”) is terminology the client must confirm; UX-03’s prior-stage card removes the practical confusion.
- **CAR formula, editable CAR/rating-sheet templates, free-form completion requirements, humanized audit descriptions** — intentional; untouched.
- **Appointing Authority decision wizard specifics** — their demo queue was empty; the decision surfaces are already covered by `docs/internal-uiux-review.md` §2.12–2.13 from rendered dumps. Only observed items are planned here.
- **Applicant portal and System Administrator surfaces** — out of this pass’s scope by definition.
