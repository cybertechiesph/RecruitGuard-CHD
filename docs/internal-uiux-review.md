# RecruitGuard-CHD — Internal (Staff-Facing) UI/UX Audit & Change Plan

**Status:** Plan only — no code changed. Awaiting human approval before any implementation.
**Scope:** The entire internal portal (5 roles, full hiring pipeline), reviewed against four lenses — Usability/IA, Visual, Accessibility, Consistency.
**Method:** Source review of `templates/internal_base.html`, `recruitment/templates/recruitment/{dashboard,application_detail,…}.html`, all `templates/internal_includes/*`, both CSS files, and `static/js/rg-wizard-validation.js`; cross-checked against **~50 real rendered pages** captured per role and per stage (incl. My Queue per role, locked/empty/closed states, and admin/notifications/audit surfaces) via the project's `seed_e2e_test_cases` data with MFA bypassed for local review.

> Severity scale: **blocker** (broken / fails a core task or WCAG) · **major** · **minor** · **polish**.
> Risk label on every recommendation: **safe before defense** vs **do after defense**.

---

## 1. Executive summary

The internal portal is, structurally, in good shape. The **one-task-per-step wizard model**, the **focused "My Queue,"** the **shared inline-validation module**, the **silent autosave**, the **SLA model**, and the **notification bell** are all real, coherent, and mostly well-built. The recent work to respect "see only what you need," to validate inline-below-field-all-at-once, and the prior accessibility pass are visible and should **not** be reverted. The entry-creation flow that links official position data instead of retyping it directly mitigates the QS-publication risk — a genuine product win.

The problems are concentrated in **four systemic themes**, plus a tail of consistency/polish issues:

1. **Keyboard accessibility is broken portal-wide (blocker).** There is no global `:focus-visible` style, and the internal `.btn` override actively removes the focus ring. Sidebar links, queue chips, table-row actions, the audit disclosure, and workflow buttons show no visible focus. Fails WCAG 2.4.7. *One CSS rule is the highest-leverage fix in this entire report.*
2. **Two "dead" rendering bugs leave content unreachable or visibly broken (blocker).** (a) The case shell's tab/stage switcher JS has no DOM to bind to, so **Case History (`#cws-timeline`) and the sysadmin Evidence panel (`#cws-evidence`) are permanently `display:none` and unreachable**. (b) The **decision / final-selection / completion *locked* bodies still use Bootstrap-Icons (`bi bi-*`) glyphs the project never loads** — they render as empty boxes on closed cases (a frequent Secretariat destination). The *editable* bodies were already fixed; the locked ones were missed.
3. **Two visual "generations" coexist (major).** A newer internal component set (`rg-icard` / `rg-istat-row` / `rg-itbl` / `rg-fsect`, flat, green) backs entries/queue/applications, while an older public set (`card` / `rg-kpi-grid` / `rg-record`, rounded, blue accents) still backs audit / users / positions. Blue (`#2563eb`, `rgba(59,130,246,…)`) and the institutional green both signal "active/current," and many colors are hardcoded rather than tokenized.
4. **A handful of interaction patterns diverge from the established baseline (major).** Three forms bypass the shared validation module; the `role="radio"` choice strips have no keyboard model and one uses the disabled-button anti-pattern the design principles explicitly forbid; destructive admin actions skip the confirm-modal that comparable actions use.

None of the blockers require structural redesign; the top three are largely safe, pre-defense fixes. The deeper convergence work (visual generations, locked-frame unification, stage-nav rebuild) should wait until after the defense.

**Top themes at a glance:** keyboard focus invisibility · unreachable case history · broken locked-state icons · blue-vs-green accent collisions · sub-AA muted-grey text · validation-pattern drift · terminology drift.

---

## 2. Per-surface findings

### 2.1 Case shell — stage tabs & tab-switcher JS
**Works:** renders exactly one active stage section (faithful to "see only what you need"); locked/empty states explain why a step isn't open.

- **[blocker · Usability/IA] The tab/stage switcher is dead — its JS has no DOM to bind to.** `application_detail.html:487–507` queries `.js-cws-tab` and toggles `.rg-cws-stage-section` by `data-section`, but **no `.js-cws-tab` element exists** in any template or rendered dump. Each case renders only the active stage section plus an always-hidden `#cws-timeline`. — *Direction:* either build a real, allowed-sections sub-nav (`role="tablist"` + `role="tab"` buttons) or remove the dead JS and reach Case History/Evidence another way. — **safe before defense.**
- **[blocker · Usability/IA] "Case History" timeline is rendered but unreachable.** `application_detail.html:414` `#cws-timeline` is permanently `display:none`; the only un-hide path is the dead JS. The audit/history trail the product is built around is dead weight in the DOM. — *Direction:* give every role a working way to view case history (real tab, disclosure, or route). — **safe before defense.**
- **[major · A11y] sysadmin Evidence panel is unreachable too.** `application_detail.html:333` `#cws-evidence` is also `display:none` behind the same dead switcher — so even the admin's evidence panel can't be opened in-case. — *Direction:* same fix as above. — **safe before defense.**

### 2.2 Case shell — header & wayfinding
**Works:** strong identity block (ref, applicant, position, branch/level, assignment) with contextual "Back to queue"; the handoff modal (`case_header.html:40–124`) matches the shared inline-validation pattern.

- **[major · Usability/IA] The "Dashboard" breadcrumb is a dead-end for workflow roles.** `topbar.html:4` always links to `dashboard`, but `DashboardView` redirects all workflow roles back to their queue — so the link round-trips to My Queue. — *Direction:* for non-sysadmin roles, label/point the breadcrumb root at "My Queue." — **safe before defense.**
- **[minor · A11y] Header "facts" row is bare `<span>`s + decorative separators**, so branch/level/assignment read as one run-on string to a screen reader (`case_header.html:12–21`). — *Direction:* use a `<dl>`/list or per-fact `aria-label`. — **do after defense.**

### 2.3 Case shell — SLA badge
**Works:** thoughtful paused/warning/overdue/ok model with human copy ("In Screening for 41 days").

- **[major · A11y] Overdue state is conveyed by color + `title` only** (`stage_sla_badge.html:13–21`) — invisible to keyboard and screen-reader users; visible text says only the day count. — *Direction:* put overdue/warning in visible text or a labeled pill ("Overdue · 41 days") plus `sr-only` qualifier; don't rely on the red dot or `title`. — **do after defense.**

### 2.4 Case shell — applicant panel & pipeline
**Works:** always-on `<details open>` Applicant Profile panel correctly replaces the old Overview tab; `case_pipeline.html` "Step N of M" with proper `aria-expanded`/`aria-controls` is a clean, low-load progress affordance.

- **[minor · A11y] Certification status is conveyed by an `.is-checked` CSS class only** (`application_detail.html:79–89`) — a screen reader hears the item identically whether checked or not. — *Direction:* add a real status icon/text alternative ("Yes/No"). — **do after defense.**
- **[polish · Consistency] Heavy inline hex/spacing in the shell** (`application_detail.html:276,302`, `case_meta_sidebar.html:46–47,100–121`) bypass tokens. — *Direction:* migrate to `rg-*` classes. — **do after defense.**

### 2.5 My Queue (`application_list.html` / `WorkflowQueueView`)
**Works:** excellent "one job" framing — "My queue," a "Needed Step" column, plain-language SLA, single "Open case" per row; branch filter chips with live `aria-live` count and an accessible empty state.

- **[major · Usability/IA] No sort, no pagination, no urgency ordering** (`views.py:881–891`, unpaginated) — overdue cases interleave with fresh ones. The Appointing Authority's queue (9 rows in the dump) is most affected. — *Direction:* default-sort by SLA urgency (overdue first); add pagination before queues grow. — **do after defense.**
- **[minor · Consistency] Branch is filterable but SLA state is not**, despite overdue being the most actionable cue shown. — *Direction:* add an "Overdue / Needs attention" chip. — **do after defense.**

### 2.6 Dashboard (System Admin)
**Works:** correctly scoped to sysadmin (workflow roles redirect away); honest observer banner ("case content is hidden"); identity-events feed + counts give a real landing.

- **[minor · A11y] Banner uses `role="note"`** (`banner.html:19`), which is not a reliably supported ARIA role — the banner conveys no semantic role wherever `banner.html` is included. — *Direction:* drop the role or use `role="status"`/`region` + `aria-label`. — **do after defense.**
- **[polish · Visual] "Open entries" KPI and the "Open recruitment entries" list double-count the same positions** (`dashboard.html:48–50` vs `70–94`) and can read as two metrics. — *Direction:* relabel or merge. — **do after defense.**

### 2.7 Workflow wizards — the validation module (`rg-wizard-validation.js`)
**Verdict: strong and faithful — do not regress.** It shows every problem at once, renders an inline `.invalid-feedback.d-block` after the field, sets `aria-invalid`, appends to (and restores) `aria-describedby`, moves focus to the first invalid field, and falls back to focusing the `role="alert"` summary box for hidden/summary-only controls (lines ~41–46, 66–73, 92–97, 112–141). Choice-strip errors correctly anchor on the `role="radiogroup"` container.

- **[major · Consistency] Three forms bypass the module** and use bespoke error paths (no `aria-invalid`/focus): the **CAR card** (`comparative_assessment.html:222–248`), the **interview fallback upload** (`interview_session_manager.html:428–445`), and the **final-selection "return CAR" form** (`final_selection_body.html:526–538`). — *Direction:* route all three through `RGWizardErrors.showErrors` for parity. — **do after defense.**
- **[minor · A11y] Inline error nodes lack `role="alert"`/`aria-live`** and are announced only on focus (via the summary box + focus move). Acceptable by design; noted. — *Direction:* optional — none required. — **do after defense.**

### 2.8 Screening wizard (`screening_body.html`)
**Works:** clean step rail; collects all problems; progressive-disclosure resubmission remarks; contradiction guards prevent invalid finalize states.

- **[major · A11y] Disabled-button anti-pattern — explicitly against principle 3.** `screening_body.html:662–663,673–674` set `completeBtn.disabled` / `qualifiedBtn.disabled`; a disabled `role="radio"` is skipped by the keyboard with no inline reason. — *Direction:* keep the control operable and show an inline error explaining why the choice is blocked. — **do after defense.** *(Pattern fix; medium effort.)*
- **[major · A11y] `role="radiogroup"`/`role="radio"` strips have no keyboard model** — no `tabindex`, no arrow-key/roving-tabindex handler (`screening_body.html:151–166,236–252`; same in exam & decision). SR users hear "radio" but can't arrow between options. — *Direction:* add roving tabindex + Arrow/Space, or use visually-styled native radios. — **do after defense.**
- **[minor · Usability/IA] Dual save model unexplained** — explicit full-page "Save draft" submit coexists with silent autosave with no "Saved automatically" cue. — *Direction:* add a persistent saved-status cue or demote the manual button. — **safe before defense.**

### 2.9 Exam wizard (`exam_body.html`)
**Works:** live status bar mirrors the disposition; conditional reveal of scores vs remarks; per-field range validation mirrors the backend.

- **[minor · Consistency] Rail marks prior steps "done" by index, not validity** (`exam_body.html:619`) — diverges from screening which gates on `stepValid`. — *Direction:* gate `is-done` on validity. — **safe before defense.**
- **[polish · Visual] Standing score-range alert can double up with inline per-field errors** (`exam_body.html:183–185` vs `576–583`). — *Direction:* let the shared module own the message. — **safe before defense.**
- **[minor · A11y] Duplicate `clearError` listener registration** (`exam_body.html:676–677` & `722–723`). — *Direction:* remove the duplicate. — **safe before defense.**

### 2.10 Interview session manager & rater (`interview_session_manager.html`, `interview_rater.html`, `interview_body.html`)
**Works:** clean role dispatch (Secretariat = session, HRMPSB = rater); finalize correctly gated on "≥1 rating or fallback sheet" via the summary box; the rater's score dial + conditional justification + privacy notice are clear; native radios where relevant.

- **[major · Consistency] Fallback-upload uses a bespoke validation path** (see 2.7). — **do after defense.**
- **[minor · A11y] Justification group reveal is silent** — focus isn't moved and the threshold notice isn't `aria-live`, so a keyboard user may not notice the new required field appeared (`interview_rater.html:377–382`). — *Direction:* announce via `aria-live` or move focus on first reveal. — **safe before defense.**

### 2.11 Deliberation recorder + CAR (`deliberation_recorder.html`, `comparative_assessment.html`, `deliberation_body.html`)
**Works:** per-card inline footers avoid a sticky-bar fight when one user holds both permissions; deliberation finalize uses the shared module with branch-conditional fields.

- **[major · Consistency] CAR card bypasses the shared module** (see 2.7) — gates show only the first failure, no focus management. — **do after defense.**
- **[polish · Visual] Banner pile-up** — empty-state + CAR-prerequisite + forward-pointer banners can stack at the top (`deliberation_body.html:24–68`). — *Direction:* collapse mutually-exclusive guidance into one contextual banner. — **safe before defense.**

### 2.12 Decision wizard — COS (`decision_body.html`)
**Works:** "review packet → decide" 2-step split with the full finalized packet read-only is excellent for a permanent action; permanence warning repeated in the modal; **already migrated bi-* icons to inline SVG here** (the pattern the locked bodies should copy).

- **[major · A11y] "Selected / Not selected" cards are `role="radio"` with no keyboard model**, with the real `<select>` visually hidden (`decision_body.html:514–549,688–707`). — *Direction:* same choice-strip fix as screening/exam. — **do after defense.**
- **[minor · Usability/IA] No client gate from Step 1 → Step 2 when the packet is incomplete** (`decision_body.html:744–749`) — the user can write decision notes then hit a server rejection. — *Direction:* block progression (or surface the blocker at the decision step) when the packet is incomplete. — **do after defense.**

### 2.13 Final selection (`final_selection_body.html`)
**Works:** the **most accessible picker in the suite** — native radios in candidate cards; rank>5 auto-checks deep selection with an inline `role="status"` lock hint; "pick the appointee" error correctly `summaryOnly`.

- **[major · Consistency] "Send CAR back to HRMPSB" form uses a bespoke `returnShowError`** (see 2.7) — two error idioms on one page. — **do after defense.**
- **[minor · A11y] Deep-selection auto-check isn't announced** via `aria-live` when a rank>5 candidate is chosen (`final_selection_body.html:425–443`). — *Direction:* update the existing `role="status"` hint on auto-check. — **safe before defense.**

### 2.14 Completion (`completion_body.html`)
**Works:** requirement checklist with a real `role="progressbar"`; Step-2 tile `aria-disabled` until closure is backend-ready; destructive closure is modal-gated with applicant/position echo.

- **[minor · A11y] Disabled Step-2 rail tile is still focusable and no-ops** with no feedback (`completion_body.html:67–74,504–507`). — *Direction:* explain on attempted activation, or remove from tab order while disabled. — **safe before defense.**
- **[minor · Consistency] Step-1 tracking form/requirement formset have no client validation** (server-only), unlike screening/exam. — *Direction:* note as intentional or add light parity validation. — **safe before defense.**

### 2.15 Locked / closed states (`*_locked_body.html`, `stage_locked.html`, `state_*`)
**Works:** the editable-stage locked bodies (screening/exam/interview/deliberation) are consistent (result strip + "Locked" pill + collapsible detail + inline SVGs); `state_empty/loading/error` and `modal_confirm` are clean, parameterized, well-documented partials.

- **[blocker · Visual/Consistency] Bootstrap-Icons rot in the decision / final-selection / completion locked bodies.** `decision_locked_body.html`, `final_selection_locked_body.html`, `completion_locked_body.html` use `<i class="bi bi-*">` glyphs the project never loads — they render as empty boxes (confirmed: `case__plt-completion-02…` contains live `bi bi-*` spans). `decision_body.html:24–27` even documents that bi-* "aren't loaded by this project." Closed-case views look broken. — *Direction:* replace every `bi-*` with the inline-SVG pattern already used in the editable bodies; drop purely decorative ones. — **safe before defense.**
- **[major · Consistency] Two locked-frame components** — `rg-cws-locked-frame`/`rg-scr-elig-bar` (editable stages) vs `rg-dec-locked-frame`/`rg-dec-status-bar` (decision/final-selection). Same concept, two visual languages. — *Direction:* converge to one. — **do after defense.**
- **[polish · Consistency] Capitalization drift** — "Exam Type"/"Administered By"/"Ranking Position" (Title Case) in locked bodies vs sentence case in the editable wizards. — *Direction:* normalize to sentence case. — **safe before defense.**

### 2.16 Notifications (bell, page, action cards)
**Works:** the bell is the **strongest a11y on the internal side** — real `<button>` with `aria-haspopup`/`aria-expanded`/`aria-controls`, dynamic count `aria-label`, decorative parts `aria-hidden`; each item is a POST form+button ("mark read + navigate" in one keyboard action); warm, specific empty states.

- **[major · A11y] Notification focus rings are blue and one path removes the outline** (`recruitguard-doh-brand.css:3054,3126,3156–3160`) — off-brand on an all-green portal and partly invisible. — *Direction:* standardize one focus token; never pair `outline:none` with a clippable inset shadow. — **safe before defense.**
- **[minor · Consistency] "New/unread" reads as two colors** — green flag in the bell, blue `rg-pill--info` on the page (`notification_list.html:57` vs `notifications_bell.html:55`); plus two parallel CSS namespaces (`rg-notif__*` vs `rg-notif-page__*`). — *Direction:* one semantic color + a shared row/flag component. — **safe before defense** (color) / **do after defense** (component merge).

### 2.17 Audit log (`audit_log_list.html`, sysadmin-only)
**Works:** correct, branded 403 for Secretariat with a "Go to Portal" escape; one template serves application- and system-scope with explicit "shows/does not show case content" copy; sensitive access flagged; raw metadata behind `<details>`.

- **[major · Usability/IA] The Action filter is a flat ~40-option `<select>`** (`audit_list__sysadmin.html:264–402`) — finding "File Downloaded" means scrolling past ~20 auth events. — *Direction:* `<optgroup>` it (Authentication, Accounts, Catalog, Files, Exports) or category + action. — **do after defense.**
- **[major · A11y] The "Audit details" `<summary>` has no focus-visible style** and reads as muted body text (`audit_log_list.html:123–124`). — *Direction:* shared `summary:focus-visible` + a chevron/underline affordance. — **safe before defense** (rolled into the global focus fix).
- **[minor · Visual/Consistency] Audit uses the old public component generation** (`rg-kpi-grid`/`card`/`rg-record`) and a different timestamp format than the evidence-vault audit pane. — *Direction:* migrate to internal components; standardize one audit timestamp. — **do after defense.**

### 2.18 Admin / identity (`internal_user_list.html`, `internal_user_form.html`)
**Works:** the "locked actor model" is communicated honestly via KPIs; role pills are color-differentiated and render correctly.

- **[major · A11y/Consistency] Deactivate/Activate is a one-click POST with no confirmation and a neutral `btn-outline-dark`** (`internal_user_list.html:70–75`) — yet the comparable entry "Close" routes through `modal_confirm.html` with a destructive button. Same severity of action, three different treatments, no focus ring. — *Direction:* route Deactivate through `rg-modal-confirm--destructive` and adopt the internal button hierarchy. — **safe before defense.**
- **[minor · A11y] User-edit form is an ungrouped Bootstrap `row g-3`** with no `rg-fsect` section/heading (`internal_user_form.html:23–29`), unlike the entry/position forms. — *Direction:* wrap in a labeled section. — **do after defense.**

### 2.19 Catalog / entries / positions / evidence
**Works:** the entries list is the best internal page (`rg-istat-row` + sticky `rg-itbl` + state pills + confirm-modal Close); position catalog enforces "view only" by hiding (not disabling) edit/create for non-sysadmin; the entry form's "select official position → review linked details → set schedule" flow directly addresses QS-publication risk.

- **[major · Consistency] Terminology drift for the same concepts** — "Recruitment Entries" vs "Open Recruitment Entries" (`position_list.html`) vs "Position Reference Catalog" vs "Position References" (button). Buttons don't match destination titles. — *Direction:* lock a glossary (Entry vs Position Reference vs public Vacancy) and make labels equal titles. — **safe before defense.**
- **[major · A11y/Visual] Pervasive inline `style=` for type/color** across catalog tables and the evidence vault (`recruitment_entry_list.html:64–67,83,112`, `evidence_vault_list.html:40,61,82,119–128`) bakes in hardcoded hexes (`#5f6c78`, `#8b96a0`, `#8c4b00`) that bypass tokens and can't be contrast-audited centrally. — *Direction:* promote to utility/token classes. — **do after defense.**
- **[minor · Usability/IA] Evidence vault embeds a mini audit pane** ("Secured Files & Audit Log," `evidence_vault_list.html:102–136`) that duplicates the dedicated audit page with different formatting. — *Direction:* keep the vault file-focused; make the pane a clearly-labeled "recent activity" teaser deferring to the full log. **Also verify it is access-scoped** (see Open Questions). — **do after defense.**
- **[minor · Consistency] `position_list.html` uses the public card/grid set** while its sibling `recruitment_entry_list.html` uses the internal table set — two internal views of overlapping data look like different products. — *Direction:* unify. — **do after defense.**

---

## 3. Per-role notes

- **Secretariat.** Highest exposure to the two blockers: the **broken-icon closed-completion view** is squarely in their path, and the **unreachable Case History** removes the audit trail they rely on for endorsements. Queue lacks overdue-first ordering despite their SLA-heavy load. Correctly 403'd from the global audit log — but can still see the evidence-vault's embedded audit pane (verify scoping).
- **HRM Chief.** The one role that sees deliberation-recorder **and** CAR on a single page; the dual-footer IA serves them well, but the **CAR card's non-module validation** is the weak link they'll hit. COS decision flows through them.
- **HRMPSB Member.** Gets the most polished single-task form (the rater). Main gaps: unreachable Case History (can't glance back at screening/exam evidence in-case without editing the URL) and the silent justification reveal.
- **Appointing Authority.** Best picker (native radios in final selection) but the **worst keyboard story on the COS decision cards** (fake radios) — two different patterns for the *same role's* "pick an outcome" job. Largest queue, most hurt by the flat (unsorted/unpaginated) queue.
- **System Administrator.** Best-served (dashboard + sidebar + user admin + audit), all gated correctly (no workflow queue → 403; sole audit/user-admin access). But the **Evidence panel they're meant to see in-case is also unreachable** behind the dead switcher; Deactivate-user lacks a confirm modal.

---

## 4. Cross-cutting issues (the de-duplicated systemic list)

1. **No global `:focus-visible` ring (blocker, A11y).** Only two niche components define one; internal `.btn` removes the default (`recruitguard.css:2116–2118`). Keyboard focus is invisible on sidebar links, queue chips, table actions, audit `<summary>`, and workflow buttons. **Single highest-priority fix.**
2. **Dead tab switcher → unreachable Case History + sysadmin Evidence (blocker, IA).** One shell-level fix serves every case for every role.
3. **Bootstrap-Icons rot in locked bodies (blocker, Visual).** Same root cause already fixed in editable bodies; finish in the three locked bodies.
4. **Blue-vs-green accent collision (major, Visual).** Raw `#2563eb` / `rgba(59,130,246,…)` for pipeline, stage/workspace tabs, step-indicator, finalize button, and notification focus compete with the institutional green. Drive every active/current/focus state from `--rg-primary`/`--rg-brand-green`; purge literals. (Also: token fallbacks `var(--rg-brand-green, #16a34a)` cite a green that isn't the token's real value `#1e5c2d`.)
5. **Two visual generations (major, Consistency).** Migrate audit/users/positions off `card`/`rg-kpi-grid`/`rg-record` onto `rg-icard`/`rg-istat-row`/`rg-itbl`/`rg-fsect`.
6. **Sub-AA muted-grey text (major, A11y).** On white: `#9aa4af` ≈ 2.4:1, `#94a3b8` ≈ 2.8:1, `#7f8a96` ≈ 3.3:1, inline `#8b96a0`/`#8c4b00` ≈ 2.6:1 — all fail AA for the small text they label. Darken to ≥ ~`#6b7682`; reserve the lightest greys for icons/dividers.
7. **Validation-pattern drift (major, Consistency).** CAR card, interview fallback upload, and return-CAR form bypass `RGWizardErrors`.
8. **Choice-strip keyboard model + disabled-button anti-pattern (major, A11y).** `role="radio"` strips (screening, exam, decision) need roving tabindex + Arrow/Space; screening must stop disabling the blocked option.
9. **Two locked-frame components (major, Consistency).** Converge `rg-cws-locked-frame`/`rg-scr-elig-bar` with `rg-dec-locked-frame`/`rg-dec-status-bar`.
10. **Confirm-modal parity for destructive admin actions (major).** User Deactivate should match entry Close.
11. **Unexplained dual save model (minor, Consistency).** Autosave + explicit full-page "Save draft" with inconsistent cue language across screening/exam/interview/deliberation — standardize one saved-status pattern.
12. **`role="note"` on every banner (minor, A11y)** — single-source fix in `banner.html`.
13. **Status-by-color-only (minor, A11y)** — SLA overdue dot, applicant `.is-checked`, pipeline ticks; add text/`sr-only` alternatives.
14. **Terminology glossary (major, Consistency)** — Entry / Position Reference / Vacancy / Positions; make button labels equal destination titles; one "New/unread" color.

---

## 5. Prioritized change plan

### (a) Quick safe wins — *safe before defense*
| # | Change | Why | Affected files | Risk |
|---|--------|-----|----------------|------|
| A1 | **Add one global `:focus-visible` ring** for `a, button, summary, [tabindex], .form-control, .form-select`; stop `.btn` from stripping the ring | Fixes WCAG 2.4.7 keyboard focus portal-wide — the single highest-leverage fix | `static/css/recruitguard.css` (+ brand overrides) | safe |
| A2 | **Replace `bi-*` glyphs with inline SVGs** in the three locked bodies | Closed cases currently render empty boxes; copy the already-fixed editable-body pattern | `decision_locked_body.html`, `final_selection_locked_body.html`, `completion_locked_body.html` | safe |
| A3 | **Restore reachability of Case History (and sysadmin Evidence)** — quickest: remove the dead switcher and surface `#cws-timeline`/`#cws-evidence` via a simple disclosure or link | The product's audit trail is currently dead DOM | `application_detail.html` | safe |
| A4 | **Breadcrumb root = "My Queue" for workflow roles** | Stop the Dashboard link round-tripping | `topbar.html` | safe |
| A5 | **Unify notification "New/unread" color to brand green + fix blue focus rings** | One semantic for one status; on-brand focus | `recruitguard-doh-brand.css`, `notification_list.html` | safe |
| A6 | **Confirm-modal + correct button severity for Deactivate user** | Match the destructive pattern used elsewhere | `internal_user_list.html` | safe |
| A7 | **Normalize locked-body capitalization to sentence case; collapse stacked deliberation banners; drop duplicate exam `clearError`; gate exam `is-done` on validity** | Cheap consistency/polish | `*_locked_body.html`, `deliberation_body.html`, `exam_body.html` | safe |
| A8 | **`role="note"` → remove / `role="status"` in `banner.html`** | Valid ARIA everywhere the banner is used | `banner.html` | safe |
| A9 | **Terminology pass: button labels = destination titles; one glossary** | Removes navigation confusion | `recruitment_entry_list.html`, `position_list.html`, `position_catalog_list.html` | safe |

### (b) Medium changes — *mostly do after defense*
| # | Change | Why | Affected files | Risk |
|---|--------|-----|----------------|------|
| B1 | **Choice-strip keyboard model + remove disabled-button anti-pattern** (roving tabindex + Arrow/Space; blocked option shows inline reason) | A11y + respects principle 3 | `screening_body.html`, `exam_body.html`, `decision_body.html` | after |
| B2 | **Route the three rogue forms through `RGWizardErrors`** | Validation parity (inline + aria + focus) | `comparative_assessment.html`, `interview_session_manager.html`, `final_selection_body.html` | after |
| B3 | **SLA overdue → visible text + `sr-only`**, not `title`/color only | A11y status cue | `stage_sla_badge.html` | after |
| B4 | **My Queue: overdue-first sort + pagination + an "Overdue" filter chip** | Most actionable cue should drive order | `WorkflowQueueView`, `application_list.html` | after |
| B5 | **Audit Action filter `<optgroup>`; standardize audit timestamp format** | Scannability | `audit_log_list.html` (+ view) | after |
| B6 | **Darken sub-AA muted greys; de-inline catalog/table styles into tokens** | Contrast + central auditability | both CSS files, `recruitment_entry_list.html`, `evidence_vault_list.html` | after |
| B7 | **Decision Step-1→2 client gate when packet incomplete; add saved-status cue to the dual-save wizards** | Fewer surprise server rejections; clearer autosave | `decision_body.html`, screening/exam/interview/deliberation bodies | after |

### (c) Bigger redesigns — *do after defense*
| # | Change | Why | Affected files | Risk |
|---|--------|-----|----------------|------|
| C1 | **Single accent/focus color system** — purge raw blue literals; drive all active/current/focus from brand tokens; fix lying token fallbacks | Coherent brand; one "active" signal | `recruitguard.css`, `recruitguard-doh-brand.css` | after |
| C2 | **Converge the two visual generations** — migrate audit/users/positions to the internal `rg-i*` component set | One product look across all staff pages | audit/users/positions templates + CSS | after |
| C3 | **Unify the two locked-frame components** into one result-strip + locked-frame | Consistent "this record is final" surface | locked bodies + CSS | after |
| C4 | **If multi-section case nav is desired, rebuild a real semantic `tablist`** for the allowed stages/history/evidence (replacing the dead switcher with an accessible, keyboard-operable one) | Proper in-case navigation + a11y | `application_detail.html` + CSS | after |
| C5 | **One autosave-status pattern** (persistent "Saved automatically HH:MM") across all wizards; reconcile manual "Save draft" vs autosave | Single, trustworthy save mental model | all wizard bodies + shared partial | after |

---

## 6. Open questions / decisions needing a human

1. **Case shell navigation intent.** Is the single-section render *intended* (radical "see only this stage") with the tab switcher being vestigial — or is the switcher a regression? The answer decides whether A3 is a quick un-hide or C4 is a real tablist rebuild. **Where should Case History and (for sysadmin) Evidence live?**
2. **Audit access policy.** Is sysadmin-only correct for the global audit log, or should Secretariat/HRM Chief have read access to *case-scoped* audit for endorsement defensibility? Relatedly: **is the evidence-vault's embedded "recent audit" pane access-scoped**, or is it a side-channel to audit data the full page 403s for Secretariat? (Security-sensitive — verify before defense.)
3. **Canonical brand green.** Which is official — institutional `#1e5c2d` (the token's real value) or the brighter `#16a34a` that appears in fallbacks and unread tints? Needed before C1/A5 color cleanup.
4. **Decision picker consistency.** Should the COS decision adopt final-selection's native-radio picker (more accessible) for one "pick an outcome" pattern, or keep the styled-card strip and just add the keyboard model (B1)?
5. **Queue scale.** Expected max rows per role? Determines whether B4 needs server-side pagination/sort now or later.
6. **Terminology.** Confirm the public-vs-internal vocabulary (Vacancy / Recruitment Entry / Position Reference) so A9 locks the right words.

---

*End of audit. No source files were modified. Implementation awaits approval of this plan; recommend sequencing all **(a) safe-before-defense** items first, starting with A1 (focus ring), A2 (locked-body icons), and A3 (Case History reachability).*
