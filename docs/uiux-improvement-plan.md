# RecruitGuard-CHD UI/UX Improvement Plan

_Produced 2026-07-02 from a hands-on browser review of the running app (desktop 1280px, tablet 768px, mobile 375px), focused on the System Administrator role with a consistency pass over the secretariat workflow screens and the applicant portal._

## 1. Context

RecruitGuard-CHD is a Django web application for DOH – Center for Health Development CALABARZON that manages the HRMPSB recruitment workflow (job publication → qualification screening → exam → interview → CAR computation → deliberation → appointment). Users are government HR staff, not technical users. Internal routes are mapped in `recruitment/internal_urls.py`; internal templates live under `templates/` (shared chrome) and `recruitment/templates/recruitment/` (pages); styling is in `static/css/recruitguard.css` and `static/css/recruitguard-doh-brand.css`.

**How to run locally (Windows):**

- Use the project venv: `.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8057`
- Environment: `DJANGO_DEBUG=True`, `SECURE_SSL_REDIRECT=False`, and — important gotcha — `config/settings.py` calls `load_dotenv(BASE_DIR / ".env")`, and the repo's `.env` sets `POSTGRES_DB=recruitguard_chd` etc. If you *unset* the `POSTGRES_*` variables (e.g. cmd `set POSTGRES_DB=`), dotenv fills them back in and the server runs against the local **Postgres demo DB** `recruitguard_chd`. If you set them to *empty strings* (Git Bash `POSTGRES_DB= ...`), the app falls back to `db.sqlite3` (stale). The review below was done against the Postgres demo DB. To watch MFA codes, set `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` and `INTERNAL_MFA_ENABLED=True` (codes print to the runserver console), or set `INTERNAL_MFA_ENABLED=False` to skip MFA. There is a ready-made `.claude/launch.json` config named `recruitguard-uxreview` with all of this.
- Test accounts (already present in the Postgres demo DB, password `UxReview!2026`): `ux_sysadmin` (role `system_admin`) and `ux_secretariat` (role `secretariat`). If missing, recreate via `manage.py shell`: create a `RecruitmentUser` with `role="system_admin"`, an email address, and `set_password(...)`. Do not use `demo_seed_bot` (unusable password). A throwaway `ux_dup_test` account (deactivated) also exists from this review.

**Intentional design decisions — do NOT undo these:**

1. The sysadmin **Secured Files** page (`/internal/evidence/`) is **metadata-only**: no download, upload, or archive controls. This is deliberate least-privilege. You may improve how the restriction is *communicated*, never restore file access.
2. The **Position Reference Catalog** was removed from the sysadmin navigation on purpose.
3. **Exports are workflow-only** (secretariat). Never add export buttons for sysadmin.
4. The audit log intentionally shows **humanized case-activity descriptions** (e.g. "Deactivated internal account 'x'."). Keep that tone.
5. Workflow roles intentionally have **no dashboard** — `DashboardView.get()` redirects them to the vacancy batch console. Only System Administrator sees `/internal/`.

---

## 2. Prioritized change items

### Critical

---

#### UX-01 — Internal portal has no navigation at mobile/tablet widths

**Problem.** At any viewport ≤768px wide, the sidebar — the *only* navigation in the internal portal — is `display:none` and there is no menu button, hamburger, or alternative. A sysadmin (or any internal user) on a phone can see the current page but cannot reach Users, Audit Log, Secured Files, Settings, or even Sign Out. The only escape is the topbar "Dashboard" breadcrumb link. Violates: user control & freedom; flexibility & efficiency.

**Evidence.** Verified at 375×812 and at ~790px: `#internal-sidebar` computes to `display:none`; a DOM scan for toggle/menu/burger buttons returns none. Cause: `static/css/recruitguard.css` line ~4376 inside the `@media (max-width: 768px)` block (which starts ~line 4282):

```css
.rg-sidebar { display: none; }
```

**Change specification.**

1. `templates/internal_includes/topbar.html` — add a menu button as the first child of `<header class="rg-context-bar">`, before the breadcrumb nav:
   ```html
   <button type="button"
           class="rg-topbar__menu-btn"
           data-drawer="internal-sidebar"
           aria-label="Open navigation menu"
           aria-controls="internal-sidebar">
       <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round" aria-hidden="true">
           <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
       </svg>
   </button>
   ```
   The drawer open/close JS already shipped in `templates/internal_base.html` (bottom script block) handles any `[data-drawer="<id>"]` trigger, an `#<id>Overlay` overlay element, overlay-click close, and Escape close — reuse it, do not write new JS.
2. `templates/internal_base.html` — the sidebar already has `id="internal-sidebar"` (set in `templates/internal_includes/sidebar.html`). Immediately after `{% include "internal_includes/sidebar.html" %}` add:
   ```html
   <div class="rg-drawer-overlay" id="internal-sidebarOverlay"></div>
   ```
3. `static/css/recruitguard.css` —
   - Hide the button on desktop: `.rg-topbar__menu-btn { display: none; background: none; border: 0; padding: .4rem; color: #374151; cursor: pointer; }`
   - Inside the `@media (max-width: 768px)` block, **replace** `.rg-sidebar { display: none; }` with an off-canvas pattern using the class the existing JS toggles (`is-open`):
     ```css
     .rg-topbar__menu-btn { display: inline-flex; }
     .rg-sidebar {
         position: fixed;
         top: 0; left: 0; bottom: 0;
         z-index: 1045;
         width: 260px;
         transform: translateX(-105%);
         transition: transform .2s ease;
     }
     .rg-sidebar.is-open { transform: translateX(0); box-shadow: 0 0 24px rgba(0,0,0,.25); }
     ```
   - If `.rg-drawer-overlay` has no base styles for this context, ensure it renders as a full-screen dimmer only when `.is-open` (the class pattern already exists for `rg-drawer`; mirror it).

**Acceptance criteria.**
- At 375px on `/internal/users/`, a menu button is visible in the topbar; tapping it slides the sidebar in; all links (Dashboard, Secured Files, Audit Log, User Management, Settings, Sign Out) are tappable.
- Tapping the dimmed overlay or pressing Escape closes the menu.
- At 1280px nothing changed: sidebar permanently visible, no menu button.
- Works on every internal page (topbar is shared), for both sysadmin and secretariat roles.

**Dependencies.** None.

---

#### UX-02 — Audit log renders the entire history on one page (no pagination)

**Problem.** `/internal/audit/` renders every matching record in one response. With the demo dataset (734 records) the page is ~1.8 MB of HTML, ~16,300 DOM nodes, ~190,000px tall. This will only grow — the view itself appends an "audit log reviewed" event on every visit. Scrolling/scanning is impractical and load time will degrade linearly. Violates: aesthetic & minimalist design; flexibility & efficiency; (soon) system performance.

**Evidence.** Measured in-browser: `{records: 734, htmlKB: 1802, domNodes: 16338, pageHeight: 190065}`. Cause: `recruitment/views.py` `AuditLogListView.get_context_data()` (lines ~752–787) does `audit_logs = list(get_all_audit_logs(...))` with no slicing, and `recruitment/templates/recruitment/audit_log_list.html` (lines 76–136) loops over all of it.

**Change specification.**

1. `recruitment/views.py`, `AuditLogListView.get_context_data()` — paginate at 50/page:
   ```python
   from django.core.paginator import Paginator  # top of file if not present

   audit_logs = get_all_audit_logs(...)          # keep the queryset lazy; drop list()
   paginator = Paginator(audit_logs, 50)
   page_obj = paginator.get_page(self.request.GET.get("page"))
   context["audit_logs"] = page_obj.object_list
   context["page_obj"] = page_obj
   context["result_count"] = paginator.count
   ```
   Keep `record_audit_log_review(...)` but pass `result_count=paginator.count`, and only call it when `self.request.GET.get("page") in (None, "", "1")` so paging through results does not spam the trail with duplicate review events.
2. New shared partial `templates/internal_includes/pagination.html`:
   ```html
   {% if page_obj.paginator.num_pages > 1 %}
   <nav class="rg-pagination mt-3" aria-label="Pagination">
       {% if page_obj.has_previous %}
           <a class="btn btn-outline-secondary btn-sm" href="?{{ querystring }}page={{ page_obj.previous_page_number }}">Previous</a>
       {% endif %}
       <span class="rg-note mx-2">Page {{ page_obj.number }} of {{ page_obj.paginator.num_pages }}</span>
       {% if page_obj.has_next %}
           <a class="btn btn-outline-secondary btn-sm" href="?{{ querystring }}page={{ page_obj.next_page_number }}">Next</a>
       {% endif %}
   </nav>
   {% endif %}
   ```
   In the view, build `querystring` so filters survive paging:
   ```python
   params = self.request.GET.copy()
   params.pop("page", None)
   context["querystring"] = f"{params.urlencode()}&" if params else ""
   ```
3. `recruitment/templates/recruitment/audit_log_list.html` — after the closing `</div>` of `rg-list-stack` (inside the card), add `{% include "internal_includes/pagination.html" %}`.

**Acceptance criteria.**
- `/internal/audit/` shows 50 records and "Page 1 of N" with a Next link; the result-count pill still shows the full total (e.g. "734 logs").
- Applying a search (`?q=...`) then clicking Next preserves the search (`?q=...&page=2`) and the filtered count.
- Filtered result sets ≤50 show no pagination controls.
- Browsing to page 2 does not add a new "audit log reviewed" entry.

**Dependencies.** UX-06 reuses the same partial.

---

### Major

---

#### UX-03 — Deactivating a user account is a single unconfirmed click

**Problem.** On `/internal/users/`, "Deactivate" immediately POSTs and locks the person out of the portal — no confirmation, no undo prompt. It sits directly beside "Edit", so a stray click on the wrong row silently disables a colleague's account. Violates: error prevention; user control & freedom.

**Evidence.** Clicked "Deactivate" on the `ux_dup_test` row: the account flipped to Inactive instantly with only a success toast. Cause: `recruitment/templates/recruitment/internal_user_list.html` lines 70–75 render a bare `<form method="post">` with a submit button. A reusable confirmation modal already exists at `templates/internal_includes/modal_confirm.html` (documented usage in its header comment) but is not used here.

**Change specification.** In `recruitment/templates/recruitment/internal_user_list.html`:

1. Inside the loop, replace the action form (lines 70–75) with:
   ```html
   <form id="toggleActive{{ internal_user.pk }}" method="post"
         action="{% url 'internal-user-toggle-active' internal_user.pk %}">
       {% csrf_token %}
   </form>
   {% if internal_user.is_active %}
       <button type="button" class="btn btn-outline-dark btn-sm"
               data-bs-toggle="modal" data-bs-target="#confirmToggle{{ internal_user.pk }}">
           Deactivate
       </button>
   {% else %}
       <button type="submit" form="toggleActive{{ internal_user.pk }}"
               class="btn btn-outline-dark btn-sm">Activate</button>
   {% endif %}
   ```
   (Re-activation is not destructive; it can stay one-click.)
2. After the closing `</table>`/`</div>` of the table shell (modals must not live inside `.table-responsive`, which can clip them), add one modal per active user:
   ```html
   {% for internal_user in internal_users %}
       {% if internal_user.is_active and internal_user != user %}
           {% include "internal_includes/modal_confirm.html" with modal_id="confirmToggle"|add:internal_user.pk|stringformat:"s" variant="destructive" title="Deactivate this account?" body="This will immediately sign the user out of the internal portal and block sign-in until the account is reactivated." confirm_label="Deactivate Account" cancel_label="Cancel" form_id="toggleActive"|add:internal_user.pk|stringformat:"s" %}
       {% endif %}
   {% endfor %}
   ```
   Note: `{% include ... with %}` cannot do string concatenation inline in all Django versions — if `"confirmToggle"|add:...` proves awkward, compute the ids with `{% with modal_id="confirmToggle"|stringformat:"s" %}` or simply inline the modal markup once per row using `confirmToggle{{ internal_user.pk }}` ids. The requirement is: unique modal id per row, `form_id` pointing at that row's form, `variant="destructive"`.
3. Include the target username in the modal title so the admin can verify the target, e.g. `title='Deactivate "'|add:internal_user.username|add:'"?'` or inline markup `Deactivate “{{ internal_user.username }}”?`.

**Acceptance criteria.**
- Clicking "Deactivate" opens a centered modal naming the exact username, with red "Deactivate Account" and "Cancel" buttons; nothing is submitted yet.
- Cancel/Escape closes the modal with no change; confirming deactivates and shows the existing success toast.
- "Activate" on an inactive account still works in one click.
- No modal markup is rendered for the signed-in admin's own row (see UX-04).

**Dependencies.** Pairs with UX-04 (own-row handling).

---

#### UX-04 — Permission-denied paths dead-end on a chrome-less page with misleading copy

**Problem.** Three related failures:
(a) Any authorized-but-forbidden action (e.g. sysadmin opening `/internal/applications/`, or clicking "Deactivate" on *their own* row) lands on a bare standalone page — no sidebar, no topbar — titled "Access Denied" whose copy says *"Please sign in with an authorised account"*. The user **is** signed in with an authorized account; the wording implies their login is wrong and reads alarming for a government audit system.
(b) The self-deactivation guard (`InternalUserToggleActiveView`) and self-role-change guards (`InternalUserUpdateView`) raise `PermissionDenied`, so a reasonable in-app click is punished with a hard 403 instead of an explanatory message.
(c) A friendlier in-chrome page already exists (`templates/forbidden.html` + `ForbiddenView` at `/internal/forbidden/`, `recruitment/views.py:308`) but nothing ever routes to it — it is dead code.
Violates: help users recognize/recover from errors; consistency; trust/tone.

**Evidence.** Reproduced both: navigating to `/internal/applications/` as `ux_sysadmin` → standalone 403; clicking Deactivate on own row → same page, the specific reason ("System Administrator cannot deactivate their own account", `recruitment/identity_views.py:424`) is discarded. `templates/403.html` lines 30–34 contain the misleading copy. `AuthzMixin.handle_no_permission` (`recruitment/permissions.py:47–63`) raises `PermissionDenied` for all authenticated denials.

**Change specification.**

1. `recruitment/identity_views.py`, `InternalUserToggleActiveView.post` (lines ~423–424) — replace the self-target guard:
   ```python
   if user == request.user:
       messages.error(request, "You cannot deactivate your own account. Ask another System Administrator to do this.")
       return redirect("internal-user-list")
   ```
2. `recruitment/identity_views.py`, `InternalUserUpdateView.form_valid` (lines ~335–341) — replace both `raise PermissionDenied(...)` branches with form errors and `return self.form_invalid(form)`:
   ```python
   if new_role != RecruitmentUser.Role.SYSTEM_ADMIN:
       form.add_error("role", "You cannot remove your own System Administrator role.")
   if not new_is_active:
       form.add_error("is_active", "You cannot deactivate your own account.")
   if form.errors:
       return self.form_invalid(form)
   ```
3. `recruitment/templates/recruitment/internal_user_list.html` — on the signed-in admin's own row render a disabled button instead of the modal trigger:
   ```html
   {% if internal_user == user %}
       <button type="button" class="btn btn-outline-dark btn-sm" disabled
               title="You cannot deactivate your own account.">Deactivate</button>
   {% endif %}
   ```
4. `templates/403.html` — replace the `<p class="rg-forbidden-copy">` copy with:
   > "Your account does not have access to this page. This section may be limited to a different role. If you believe you need access, contact your System Administrator."
   and change the button label from "Go to Portal" to "Return to Portal" (href stays `/internal/`, which safely re-routes each role to its home).
5. Optional (only if trivial): also update the identical copy in `templates/forbidden.html` for consistency, since UX items below do not resurrect it. Do **not** attempt to wire `handler403` to the in-chrome page in this pass — rendering the sidebar requires request context that Django's default 403 handler doesn't guarantee; the copy fix plus items 1–3 removes the worst dead-ends.

**Acceptance criteria.**
- Clicking Edit on your own account, unticking "Active", and saving shows an inline error on the Active field — no 403 page, other edits preserved.
- Your own row on `/internal/users/` shows a disabled Deactivate button with an explanatory tooltip.
- Visiting `/internal/applications/` as sysadmin still returns HTTP 403, but the page copy no longer suggests re-signing in, and "Return to Portal" navigates back to the dashboard.
- No workflow-role behavior changed (secretariat still reaches all its pages).

**Dependencies.** UX-03 (shares the user-list actions cell).

---

#### UX-05 — Dashboard "Open entries" statistic shows a capped, wrong number

**Problem.** The sysadmin dashboard's "Open entries" stat is computed as `{{ positions|length }}` from a queryset the view slices to six items — so it displays "6" whenever six or more entries are open, silently under-reporting on the one oversight screen the sysadmin gets. Violates: visibility of system status; (trust — a government oversight dashboard must not show wrong counts).

**Evidence.** Dashboard showed "6 Open entries"; `get_visible_positions_for_user(ux_sysadmin)` actually returns 10. Cause: `recruitment/views.py:289` — `context["positions"] = get_visible_positions_for_user(user)[:6]` — combined with `recruitment/templates/recruitment/dashboard.html:46` — `{{ positions|length }}`.

**Change specification.**

1. `recruitment/views.py`, `DashboardView.get_context_data` — replace line 289 with a real count (the sliced list is used nowhere else on this template; workflow roles never reach it because `get()` redirects them):
   ```python
   context["open_entry_count"] = get_visible_positions_for_user(user).count()
   ```
2. `recruitment/templates/recruitment/dashboard.html:46` — replace `{{ positions|length }}` with `{{ open_entry_count }}`.
3. While in that file: the dashboard event rows (lines 52–58) show what happened and when but not **who** did it. Append the actor to each row:
   ```html
   <span class="rg-dash-sysad-event__desc">{{ entry.description }} — {{ entry.actor|default:"System" }}</span>
   ```

**Acceptance criteria.**
- With 10 open entries in the DB, the dashboard stat reads "10".
- Creating/deactivating a user via the UI adds an event row that names the acting admin.
- Workflow roles are still redirected off `/internal/` (no regression from the context change).

**Dependencies.** None.

---

#### UX-06 — Secured Files page can't fulfill its own stated purpose (truncated hash), plus unbounded list and sub-10px text

**Problem.** The page header tells the sysadmin to *"Confirm a file exists and is unaltered by its SHA-256 hash"*, but the table truncates every hash to 13 characters (`54babef9891cb…`) with no way to see or copy the full digest — the stated integrity check is impossible. The list also renders all rows at once (242 in demo data) with no pagination, and uses inline font sizes down to 0.65rem (~10.4px), below comfortable legibility for the case-reference and archived tags. Violates: visibility of system status; match between system and real world (a hash you can't read isn't a hash); accessibility (text size).

**Evidence.** Observed pill "242 files", 242 `<tbody>` rows, first SHA cell `54babef9891cb…`. Cause: `recruitment/templates/recruitment/evidence_vault_list.html:79` (`{{ item.sha256_digest|truncatechars:14 }}`), inline styles `font-size:.65rem` on lines 61/64/69, and `EvidenceVaultListView` (`recruitment/views.py:913`) being a `ListView` without `paginate_by`.

**Change specification.**

1. `recruitment/views.py`, `EvidenceVaultListView` — add `paginate_by = 50`. In `get_context_data`, keep `result_count` as the **total**: replace `context["result_count"] = len(evidence_items)` with `context["result_count"] = context["paginator"].count if context.get("paginator") else len(evidence_items)`. Add the same `querystring` context as UX-02. Note the per-item `owner_application` loop then only touches the current page — cheaper, no behavior change.
2. `recruitment/templates/recruitment/evidence_vault_list.html` — after the table shell, add `{% include "internal_includes/pagination.html" %}` (partial from UX-02).
3. Same file, line 79 — replace the SHA cell content with a copy control that exposes the full digest:
   ```html
   <td style="font-size:.75rem;font-family:monospace;">
       <span title="{{ item.sha256_digest }}">{{ item.sha256_digest|truncatechars:14 }}</span>
       <button type="button" class="btn btn-link btn-sm p-0 rg-copy-hash"
               data-hash="{{ item.sha256_digest }}"
               aria-label="Copy full SHA-256 for {{ item.original_filename }}">Copy</button>
   </td>
   ```
   And at the end of the `{% block content %}` add:
   ```html
   <script>
   document.addEventListener("click", function (e) {
       var btn = e.target.closest(".rg-copy-hash");
       if (!btn) return;
       navigator.clipboard.writeText(btn.dataset.hash).then(function () {
           btn.textContent = "Copied";
           setTimeout(function () { btn.textContent = "Copy"; }, 1500);
       });
   });
   </script>
   ```
4. Same file — raise the three `font-size:.65rem` inline styles (lines ~61, 64, 69) to `.75rem`.

**Do not** add download/upload/archive controls — metadata-only is intentional. The existing header copy explaining the restriction is good; keep it.

**Acceptance criteria.**
- Page shows 50 rows + "Page 1 of N"; the pill still reads the full total ("242 files"); filters survive paging.
- Hovering a hash shows the full 64-char digest; clicking "Copy" puts the full digest on the clipboard and flashes "Copied".
- No download/upload/archive affordance appears anywhere on the page.
- Smallest text in the table computes to ≥12px.

**Dependencies.** UX-02 (pagination partial).

---

### Minor

---

#### UX-07 — No required-field indication on any internal form

**Problem.** The user create/edit form mixes required fields (username, email, passwords, role) with optional ones (first/last name, employee ID, office) with no visual distinction; staff discover requirements only by failed submission. Violates: error prevention; recognition over recall.

**Evidence.** `/internal/users/new/` renders zero `*` markers (checked programmatically). All internal forms render through the shared include `recruitment/templates/recruitment/includes/form_field.html`, which prints only label, widget, help text, errors.

**Change specification.**

1. `recruitment/templates/recruitment/includes/form_field.html` — in the generic branch (line 23) and the checkbox branch (line 11), mark required fields inside the label:
   ```html
   <label class="form-label" for="{{ field.id_for_label }}">
       {{ field.label }}{% if field.field.required %}<span class="rg-required" aria-hidden="true">*</span>{% endif %}
   </label>
   ```
2. `static/css/recruitguard.css` — add `.rg-required { color: #b3261e; margin-left: .15rem; font-weight: 600; }`.
3. `recruitment/templates/recruitment/internal_user_form.html` — under the page header paragraph add a one-line legend: `<p class="rg-note">Fields marked <span class="rg-required">*</span> are required.</p>`.

**Acceptance criteria.**
- On `/internal/users/new/`, Username, Email address, Role, Password, Password confirmation show a red asterisk; First/Last name, Employee ID, Office name do not.
- The same marker automatically appears on the audit-log and evidence filter forms only where fields are required (they are all optional — so no change there), and on applicant portal forms (shared include) without layout breakage.

**Dependencies.** None.

---

#### UX-08 — Server-side validation errors don't highlight the offending field (dead `has-error` CSS)

**Problem.** When a form round-trips with an error (e.g. duplicate email), the message text renders but the input keeps its normal border — the stylesheet's red-border treatment (`.rg-field.has-error .form-control`, `static/css/recruitguard.css:4829-4836`) never fires because no template ever adds the `has-error` class. On long forms the eye has nothing to lock onto. Violates: help users recognize errors.

**Evidence.** Submitted `/internal/users/new/` with an in-use email: error text appeared under the field, but the input had no error class/border (verified via DOM: no `.is-invalid`, no `.has-error` anywhere). Client-side wizard JS uses `is-invalid` but only on wizard-validated applicant forms.

**Change specification.** In `recruitment/templates/recruitment/includes/form_field.html`, add the class conditionally on both non-hidden wrappers:

```html
<div class="rg-field{% if field.errors %} has-error{% endif %}">            {# line 22 #}
<div class="rg-field rg-field--checkbox{% if field.errors %} has-error{% endif %}">  {# line 7 #}
```

**Acceptance criteria.**
- Submitting the create-user form with a duplicate email renders the email input with the red `has-error` border and keeps all other entered values.
- Valid fields on the same failed submit are visually unchanged.

**Dependencies.** None (same file as UX-07 — land together).

---

#### UX-09 — MFA code entry lacks OTP input ergonomics and an escape hatch

**Problem.** On `/internal/login/mfa/` the code field is a plain text input: no autofocus, no numeric keyboard on mobile, no OTP autofill from the email client, and the page has zero links — a user who mistyped their username at step 1 (code sent to the wrong account) has no "Back to sign in". Violates: user control & freedom; flexibility & efficiency.

**Evidence.** DOM inspection of the OTP input: `{autofocus:false, inputmode:null, autocomplete:""}`; page contains no `<a>` elements. Sources: `recruitment/forms.py:464-465` (`InternalMFAOTPForm`), `templates/registration/internal_mfa_verify.html`.

**Change specification.**

1. `recruitment/forms.py`, `InternalMFAOTPForm.otp` — give the field a widget:
   ```python
   otp = forms.CharField(
       max_length=6,
       min_length=6,
       label="Verification Code",
       widget=forms.TextInput(attrs={
           "autofocus": True,
           "inputmode": "numeric",
           "autocomplete": "one-time-code",
           "pattern": "[0-9]{6}",
           "maxlength": "6",
       }),
   )
   ```
2. `templates/registration/internal_mfa_verify.html` — after the "Send New Code" form (line 48), add:
   ```html
   <p class="rg-login-card__notice mt-3">
       Wrong account? <a href="{% url 'login' %}">Back to sign in</a>
   </p>
   ```
   (The login view's `dispatch` already clears half-finished MFA state by logging out unverified users — no view change needed.)

**Acceptance criteria.**
- Landing on the MFA page places the cursor in the code field; on a 375px viewport the numeric keypad opens.
- The email-client OTP suggestion (autocomplete="one-time-code") is offered by supporting browsers.
- "Back to sign in" returns to `/internal/login/` and a fresh login works.

**Dependencies.** None.

---

#### UX-10 — Notification copy talks about "your cases" to a role that never has cases

**Problem.** For the sysadmin, the notifications page subtitle reads "Updates about cases and applications assigned to you", and both empty states promise "a case assignment, a returned application, or a scheduled interview" / "New updates about your cases" — none of which a System Administrator can ever receive. The role's actual notifications (if any) concern accounts and security. Wrong expectations on an audit-oriented role. Violates: match between system and real world.

**Evidence.** Observed on `/internal/notifications/` and in the bell dropdown as `ux_sysadmin`. Sources: `recruitment/templates/recruitment/notification_list.html` line 16 (subtitle) and line 75 (empty text); `templates/internal_includes/notifications_bell.html` line 68 (panel empty text).

**Change specification.** Branch the three strings on role:

1. `notification_list.html:16`:
   ```html
   <p class="rg-page-subtitle">{% if user.role == "system_admin" %}Updates about accounts, access, and system activity.{% else %}Updates about cases and applications assigned to you.{% endif %}</p>
   ```
2. `notification_list.html:75`:
   ```html
   <p class="rg-notif-page__empty-text">{% if user.role == "system_admin" %}When something needs your attention &mdash; an account change or a security event &mdash; it will appear here.{% else %}When something needs your attention &mdash; a case assignment, a returned application, or a scheduled interview &mdash; it will appear here.{% endif %}</p>
   ```
3. `notifications_bell.html:68`:
   ```html
   <p class="rg-notif__empty-text">{% if user.role == "system_admin" %}New account and system updates will appear here.{% else %}New updates about your cases will appear here.{% endif %}</p>
   ```

**Acceptance criteria.**
- As `ux_sysadmin`, both the page and the bell dropdown empty states mention accounts/system activity, not cases.
- As `ux_secretariat`, the original case-oriented wording is unchanged.

**Dependencies.** None.

---

#### UX-11 — Orphaned "Open Recruitment Entries" page still reachable by sysadmin URL

**Problem.** The Position Reference Catalog / entries surface was deliberately removed from the sysadmin navigation, but `/internal/positions/` still returns 200 for sysadmin (it only requires `InternalUserRequiredMixin`). A sysadmin following an old bookmark or link lands on a workflow page their nav says they don't have, with no nav item highlighted — inconsistent IA and confusing "am I supposed to be here?" state. Violates: consistency & standards.

**Evidence.** `fetch('/internal/positions/')` as `ux_sysadmin` → 200 (while `/internal/applications/`, `/internal/entries/`, `/internal/vacancies/`, `/internal/workflow/queue/` correctly 403). Source: `recruitment/views.py:371-376` (`PositionListView`). The neighbouring `ApplicationListView.get` (lines 383–386) already implements the intended pattern: `if user.role == RecruitmentUser.Role.SYSTEM_ADMIN: raise PermissionDenied`.

**Change specification.** In `recruitment/views.py`, add to `PositionListView`:

```python
def get(self, request, *args, **kwargs):
    if request.user.role == RecruitmentUser.Role.SYSTEM_ADMIN:
        raise PermissionDenied
    return super().get(request, *args, **kwargs)
```

This *closes* the URL to match the intentional nav removal — it does not restore anything.

**Acceptance criteria.**
- `/internal/positions/` returns 403 for `ux_sysadmin` (rendering the improved UX-04 copy).
- `/internal/positions/` still renders normally for `ux_secretariat`.

**Dependencies.** UX-04 (nicer 403 copy) is cosmetic-only; no hard dependency.

---

#### UX-12 — User Management KPI cards filled with insider jargon

**Problem.** Two of the four stat cards on `/internal/users/` are static jargon: "Actor Model — **Locked** — No new internal actors are introduced here" and "Portal Scope — **Admin** — Identity and access control only". To HR staff, "actor model" and "portal scope" mean nothing, and neither card ever changes — they are architecture notes dressed as metrics. Violates: match between system and real world; aesthetic & minimalist design.

**Evidence.** Observed on `/internal/users/`; source `recruitment/templates/recruitment/internal_user_list.html` lines 27–36.

**Change specification.**

1. `recruitment/identity_views.py`, `InternalUserListView.get_context_data` — add `context["inactive_internal_users"] = queryset.filter(is_active=False).count()`.
2. `internal_user_list.html` — replace the two jargon cards (lines 27–36) with one meaningful card and one plain-language note card:
   ```html
   <div class="rg-kpi">
       <span class="rg-kpi__label">Inactive Accounts</span>
       <strong class="rg-kpi__value">{{ inactive_internal_users }}</strong>
       <span class="rg-kpi__hint">Blocked from signing in until reactivated</span>
   </div>
   <div class="rg-kpi">
       <span class="rg-kpi__label">Roles</span>
       <strong class="rg-kpi__value">Fixed set</strong>
       <span class="rg-kpi__hint">Accounts are assigned one of the five HRMPSB roles</span>
   </div>
   ```

**Acceptance criteria.**
- The words "Actor Model" and "Portal Scope" no longer appear on the page.
- "Inactive Accounts" shows the true count (e.g. 1 while `ux_dup_test` is deactivated) and updates after a deactivation.

**Dependencies.** None.

---

### Polish

---

#### UX-13 — Missing browser-tab titles and inconsistent page-title casing

**Problem.** Several sysadmin pages never set the `internal_title` block, so the tab reads "Internal Portal | Internal Portal" (User Management, Create/Update user, Audit Log, Change Password) — unhelpful for staff juggling tabs and for browser history. Separately, visible H1s mix sentence case ("My queue", "Vacancy batches", "Batch exam — …") with title case ("Internal Users", "Audit Log"). Violates: consistency & standards.

**Evidence.** Tab titles observed in-browser; `grep internal_title` shows the block missing from the templates below; H1 grep shows the casing split.

**Change specification.**

1. Add after the `{% extends %}`/`{% load %}` lines:
   - `recruitment/templates/recruitment/internal_user_list.html`: `{% block internal_title %}User Management{% endblock %}`
   - `recruitment/templates/recruitment/internal_user_form.html`: `{% block internal_title %}{% if object %}Update Internal User{% else %}Create Internal User{% endif %}{% endblock %}`
   - `recruitment/templates/recruitment/audit_log_list.html`: `{% block internal_title %}{% if review_scope == "application" %}Case Audit Log{% else %}Audit Log{% endif %}{% endblock %}`
   - `recruitment/templates/recruitment/position_list.html`: `{% block internal_title %}Open Recruitment Entries{% endblock %}`
   - `templates/registration/password_change_form.html`: `{% block internal_title %}Change Password{% endblock %}`
2. Normalize H1s to title case in: `application_list.html:14` ("My Queue" / "Cases I Can View"), `vacancy_batches.html:14` ("Vacancy Batches"), `vacancy_batch_exam.html:18` ("Batch Exam — …"), `vacancy_batch_interview.html:18` ("Batch Interview — …"), `vacancy_batch_cos_deliberation.html:18` ("COS Deliberation — …").

**Acceptance criteria.**
- Tabs read "User Management | Internal Portal", "Audit Log | Internal Portal", etc.; no page shows "Internal Portal | Internal Portal".
- All internal H1 page titles use title case.

**Dependencies.** None.

---

#### UX-14 — Breadcrumb never shows the current page (dead template block)

**Problem.** The topbar breadcrumb permanently reads just "Dashboard" on every page. The `{% block breadcrumb %}` slot lives inside `templates/internal_includes/topbar.html`, which is pulled in via `{% include %}` — Django blocks inside included templates can never be overridden by page templates, so the overrides that already exist (`templates/forbidden.html:5-8`, `recruitment/templates/recruitment/notification_list.html:6-9`) silently never render. Users get no "where am I" cue from the topbar, and for workflow roles the crumb is labelled "Dashboard" while actually redirecting to Batches. Violates: visibility of system status; recognition over recall.

**Evidence.** Breadcrumb text captured on `/internal/users/`, `/internal/audit/`, `/internal/workflow/queue/` — always exactly "Dashboard". Source: `templates/internal_includes/topbar.html:5`; sole include site `templates/internal_base.html:26`.

**Change specification.**

1. `templates/internal_base.html` — replace `{% include "internal_includes/topbar.html" %}` with the topbar markup inlined (copy the full contents of `topbar.html`, it is 16 lines), so the `{% block breadcrumb %}` participates in template inheritance. Delete `templates/internal_includes/topbar.html` (verify with grep that internal_base.html was its only consumer).
2. Change the home crumb to be role-honest: `<a href="{% url 'dashboard' %}" class="rg-breadcrumb-home">{% if user.role == "system_admin" %}Dashboard{% else %}Batches{% endif %}</a>`.
3. Add breadcrumb blocks to the sysadmin pages (same pattern as the existing one in `notification_list.html`):
   - `internal_user_list.html`: current-crumb "User Management"
   - `internal_user_form.html`: "User Management" (link to `{% url 'internal-user-list' %}`) › "Create/Update"
   - `audit_log_list.html`: "Audit Log"
   - `evidence_vault_list.html`: "Secured Files"
   - `registration/password_change_form.html`: "Change Password"
4. The existing dead overrides in `forbidden.html` and `notification_list.html` begin working automatically — verify, don't rewrite.

**Acceptance criteria.**
- On `/internal/users/` the topbar reads "Dashboard › User Management"; on `/internal/audit/` "Dashboard › Audit Log".
- As secretariat, the home crumb reads "Batches" and pages render with no template errors.
- `{% include "internal_includes/topbar.html" %}` no longer appears anywhere.

**Dependencies.** Do after UX-01 (both edit the topbar markup; UX-01 first, then inline the result).

---

## 3. Suggested implementation order

**Batch 1 — copy, labels, and small template fixes (independent, lowest risk; one commit):**
UX-07, UX-08 (same include file), UX-09, UX-10, UX-12, UX-13. No view logic changes except the one-line context addition in UX-12.

**Batch 2 — behavior fixes in views + user management safety (one commit):**
UX-03, UX-04, UX-05, UX-11. UX-03/UX-04 touch the same template/views and should be reviewed together; UX-05 and UX-11 are isolated one-file changes.

**Batch 3 — structural/layout work (one commit, needs the most browser verification):**
UX-01 (mobile nav), then UX-14 (topbar inline — builds on UX-01's edited topbar), UX-02 (pagination partial), UX-06 (reuses the partial).

Run the existing test suite after each batch (`.venv\Scripts\python.exe manage.py test recruitment` with `DJANGO_DEBUG=1` and empty-string `POSTGRES_*` vars); UX-04 and UX-11 change response codes/flows that likely have test coverage (`recruitment/tests.py` asserts on 403/redirect behavior — update tests to the new redirect+message behavior for self-deactivation, keep 403 assertions for role denials).

## 4. Out of scope / deliberately not planned

- **Restoring any file access on sysadmin Secured Files** (download/upload/archive) — intentional least-privilege design; only communication improvements (UX-06) are planned.
- **Re-adding the Position Reference Catalog or exports for sysadmin** — intentionally removed; UX-11 only closes a leftover URL to *match* that decision.
- **Dark mode** — no `prefers-color-scheme` support exists anywhere; adding a theme is a design project needing client input, not a fix.
- **Audit-trail self-noise** ("audit log reviewed" events recorded on every visit fill the very log being reviewed) — changing what gets logged alters compliance semantics; needs client sign-off. UX-02 only stops *pagination* from multiplying these events.
- **Login CAPTCHA/MFA configuration** (`.env` currently disables CAPTCHA and MFA) — security configuration, not UI; tracked by the security workstream.
- **Global inline-style cleanup** (e.g. `style="..."` on sidebar sign-out button, evidence table) beyond the specific legibility fixes in UX-06 — broad refactor risk for cosmetic gain.
- **Password-strength meter rendering on the "confirm password" field** (redundant second meter on `/internal/password/change/`) — cosmetic, low value; skip unless touching that widget anyway.
- **HTML5-only validation on the login form** (browser-default tooltips) — standard Django/LoginView behavior, acceptable; server-side errors render correctly.
