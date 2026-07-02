# RecruitGuard-CHD — Applicant Portal UI/UX Improvement Plan

_Scope source: `docs/uiux-applicant-review-prompt.md`. Produced 2026-07-02 from a hands-on review of the running portal at 375px/768px/1280px: a real end-to-end submission (vacancy → intake with 7 uploads → OTP from console logs → receipt → status), plus hostile-input probes (wrong/oversized/zero-byte files, malformed fields, duplicate files, bad tokens), a full resubmission round-trip triggered from the internal side, and data-integrity checks with ñ / apostrophes / CJK characters. Every finding below was observed live or verified in source._

## 1. Context

The applicant portal (`/apply/`, routes in `recruitment/applicant_urls.py`, views in `recruitment/portal_views.py`, templates in `recruitment/templates/recruitment/applicant_*.html` + `templates/applicant_base.html`) is the public side of RecruitGuard-CHD, where members of the public apply for DOH–CHD CALABARZON positions. Assume applicants on low-end Android phones with spotty connections, first-time users with low digital literacy, and hold WCAG 2.1 AA. The portal is already the most polished surface of the app — 3-step intake wizard with inline all-at-once validation, client checks that mirror server rules, live-region announcements, draft persistence of valid uploads on failed submits, OTP with resend + "wrong email" escape, a receipt with copy-able Application ID, and a genuinely good status page. The items below close the remaining gaps; do not regress the listed strengths.

**Run locally (Windows):** `.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8058` with `DJANGO_DEBUG=True`, `SECURE_SSL_REDIRECT=False`, `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` (the `.claude/launch.json` config `recruitguard-uxreview-nomfa` sets all of this). **DB gotcha:** `settings.py` loads `.env`, which points at local Postgres `recruitguard_chd` (the demo data with 6 open vacancies); unsetting `POSTGRES_*` still lands on Postgres, only empty-string values fall back to stale sqlite. Seed with `manage.py seed_demo_data` if vacancies are missing. **Token-gated pages** (`/apply/<token>/otp|receipt|status|resubmit/`) are reached by completing a real intake; the OTP code prints to the runserver console (search the log for "Your verification code is"). To exercise `/resubmit/`, flag a screening document as `request_resubmission` internally and call `request_document_resubmission` (see `recruitment/services.py:6894`), or use the internal Return-to-Applicant workflow action.

**Intentional decisions — do NOT undo:** (1) the email + OTP token verification flow is a deliberate identity/anti-spam control — improve its explanation and recovery, never remove it; (2) the Data Privacy / data-subject-rights / retention notice content is legally required — improve presentation only, never the substance.

---

## 2. Prioritized change items

### Critical

---

#### UX-01 — A draft abandoned at the OTP step is unrecoverable: the OTP email has no way back

**Screen:** OTP email + `/apply/<token>/otp/` · **Type:** Feature (email context) + Copy

**Problem.** After the intake form is submitted, the draft (including all uploaded files) is saved and the applicant lands on the OTP page. If they close the tab there — the single most likely drop-off point on a phone — the only artifact they have is the OTP email, which contains **only the 6-digit code**: no "continue your application" link, no Application ID, no position name link. And there is no other way back in: `reference_number` is generated **only at final submission** (`recruitment/services.py:6677-6678`), so drafts have none — which also means the Track-Application draft-recovery path (`ApplicantStatusLookupView.get_unfinished_draft`, `recruitment/portal_views.py:487-497`, which matches on `reference_number`) can **never match a pre-submission draft**. The applicant's typed data and 7 uploaded files exist on the server, permanently unreachable to them. Violates: error recovery; recognition over recall. Hurts reality #1 (mobile/spotty connections) hardest.

**Evidence.** Captured the OTP email from the console backend: subject "RecruitGuard-CHD applicant verification code", body contains the code, validity, and security notice — no link, no ID. Verified in shell that drafts have `reference_number == ''`.

**Change specification.**

1. `recruitment/notification_services.py` — in the function that builds the applicant OTP email (grep `applicant_otp`; it renders `templates/email/applicant_otp.html` / `.txt`), add to the template context:
   `continue_url = f"{base_url}/apply/{application.public_token}/otp/"` built the same way `build_applicant_status_url` builds links (`recruitment/notification_services.py:226`), plus `position_title = application.position.title`.
2. `templates/email/applicant_otp.html` and the `.txt` sibling — after the code block, add a button/link (reuse the email `_button.html` partial used by the received-confirmation email) with exactly this copy:
   > **Continue your application**
   > "You are applying for **{{ position_title }}**. If you closed the application page, use this link to return, enter the code, and finish submitting."
3. `recruitment/templates/recruitment/applicant_otp.html` — in the un-verified state (around line 57), under the title add one reassurance line:
   > "Your answers and uploaded documents are saved. Finish verifying to submit them."

**Acceptance criteria.**
- Complete an intake, then read the OTP email in the console log: it contains a `/apply/<token>/otp/` link and the position title.
- Open that link in a fresh browser profile (no cookies): the OTP page loads with the saved draft ("7 files" in the verified summary after entering the code) and the flow completes normally.
- The receipt and confirmation email are unchanged.
- A resend email (Resend the code) carries the same continue link.

**Dependencies.** None. (Generating `reference_number` at draft creation so the Track-Application draft path also works is a worthwhile *optional* follow-up, but touch it only if tests around `generate_application_reference` uniqueness pass unchanged.)

---

#### UX-02 — Everything typed is silently lost on refresh, back, or a dropped connection before first submit

**Screen:** `/apply/entries/<pk>/apply/` · **Type:** Feature (client-side JS only)

**Problem.** The intake form has no local persistence and no leave-page warning. An applicant who fills Step 1 (names, email, phone, a 1,000-character qualification summary typed on a phone keyboard) and then refreshes, taps Back, follows the "← Cancel" link, or loses the page to a memory-hungry Android browser, loses every keystroke with zero warning. Server-side draft saving only kicks in after a submit reaches the server. Violates: error prevention; user control. This is the single worst data-loss window for reality #1.

**Evidence.** Live test: filled first/last/email on Step 1, reloaded — all fields empty. `grep` confirms no `beforeunload`, `localStorage`, or `sessionStorage` anywhere in `applicant_intake_form.html`.

**Change specification.** All in `recruitment/templates/recruitment/applicant_intake_form.html`'s script block:

1. **Local draft of typed answers.** On `input`/`change` (debounced 1 s), save the values of `first_name, last_name, email, phone, qualification_summary, cover_letter, performance_rating_applicability, submission_confirmation` to `localStorage` under key `rg-intake-<entry.pk>` with a `savedAt` timestamp. Never store files.
2. **Restore.** On page load, only when the form is not a server-rendered draft (no `?token=` in the URL, no `form.saved_draft_notice`, and the fields are empty), restore the saved values if `savedAt` is under 7 days old, and show a dismissible notice directly above the step rail:
   > "We restored what you typed earlier on this device. Not yours? **Clear it.**"
   where "Clear it" is a button that wipes the stored blob and empties the fields (shared/borrowed-phone safety).
3. **Clear.** Delete the key when: the applicant clicks "Clear it"; the blob is >7 days old; or the flow reaches the OTP page — add a 3-line script to `recruitment/templates/recruitment/applicant_otp.html` that removes `rg-intake-{{ application.position.pk }}` (the draft is now safely on the server).
4. **Leave-page guard.** Register `beforeunload` returning the browser's confirm prompt whenever the form is dirty (any field differs from its initial value) and a submit is not in flight. Suppress the guard during the real form submission (set a flag in the submit handler before `form.submit()`).

**Acceptance criteria.**
- On a 375px viewport, type into Step 1, refresh: the values reappear with the "restored" notice; "Clear it" empties them.
- Closing the tab / navigating away with typed data triggers the browser's leave-confirmation; submitting does not.
- After reaching the OTP page, returning to the blank intake form does NOT restore stale values.
- Loading a server draft via `?token=` never gets overwritten by the local blob.

**Dependencies.** None; independent of UX-03 but lands in the same file — implement together.

---

#### UX-03 — Zero feedback during the real upload POST, and the submit button re-enables mid-flight

**Screen:** `/apply/entries/<pk>/apply/` (final submit) · **Type:** Feature (JS)

**Problem.** Clicking "Continue to email verification →" starts a multi-megabyte POST (up to 8 files × 5 MB). During that upload the page shows **nothing** — no spinner, no progress, no "keep this page open" — and the code re-enables the submit button *before* calling `form.submit()`, so an impatient applicant on a slow connection can tap it again mid-upload (double POST) or assume the site is frozen and leave (losing the submission). Violates: visibility of system status; error prevention. Reality #1's defining moment.

**Evidence.** `applicant_intake_form.html` `verifyReadabilityThenSubmit()` (~line 1147-1173): sets "Checking your files…", then `submitBtn.disabled = false; submitBtn.textContent = submitBtnLabel;` **before** `form.submit()`. No progress UI exists anywhere in the template.

**Change specification.** In the same script block:

1. **Minimum (required):** in `verifyReadabilityThenSubmit()`, when all checks pass, do NOT re-enable the button. Set `submitBtn.disabled = true`, `submitBtn.textContent = "Submitting your application…"`, add `aria-busy="true"` to the form, show a persistent line in the action bar (`#rg-intake-error` area but styled as info, or a new `#rg-intake-progress` element with `role="status"`):
   > "Uploading your documents — this can take a few minutes on a slow connection. Please keep this page open."
   Only re-enable if the check *fails* (existing failure path keeps working).
2. **Progress bar (required for this item to close):** replace the final `form.submit()` with an `XMLHttpRequest` POST of `new FormData(form)`:
   - `xhr.upload.onprogress` → update a `<progress max="100">` element and a text percent inside `#rg-intake-progress` ("Uploading… 45%").
   - On `load`: if `xhr.responseURL` differs from the current URL (the success redirect to `/otp/`), `window.location.assign(xhr.responseURL)`. Otherwise (validation errors re-rendered), `document.open(); document.write(xhr.responseText); document.close()` is NOT acceptable — instead fall back to a plain `form.submit()` retry-free resubmission is also messy; the simplest robust rule: if `responseURL` is unchanged, replace `document.documentElement.innerHTML` is fragile — **do this instead**: submit with `fetch(form.action, {method:'POST', body, redirect:'follow'})`; on `response.redirected` → `location.assign(response.url)`; on a 200 with HTML (server-side validation errors) → `location.reload()` is wrong (POST data lost)… Given the server already persists valid uploads to a draft on failed submits and re-renders with `?`-less POST, the correct behavior is: parse nothing — render the returned HTML by `document.write` is deprecated but functional; prefer: `history.replaceState` + `document.documentElement.replaceWith(new DOMParser().parseFromString(html,'text/html').documentElement)`, then re-run the inline scripts is complex. **Decision for the implementer:** if replacing the document from the fetch response proves brittle, keep tier 1 (locked button + indeterminate "Uploading…" status) with the native `form.submit()`, and ship the percent bar only for the success path via `xhr` + `responseURL`. Tier 1 alone already removes the double-submit and the frozen-page abandonment; the percent number is the enhancement.
   - On network `error`/`timeout`: re-enable the button labeled "Try again", keep all fields intact (nothing navigated), and show:
     > "The upload did not go through — check your connection and try again. Your typed answers are still here."
3. Whatever tier ships, the no-JS fallback (plain form POST) must keep working.

**Acceptance criteria.**
- With DevTools throttling set to "Slow 3G", clicking submit immediately disables the button, shows the uploading status (and a moving percent if tier 2 shipped), and the button cannot be clicked again until the request settles.
- Killing the network mid-upload shows the retry message with all fields intact; retrying succeeds.
- A successful submit still lands on `/apply/<token>/otp/`.
- With JS disabled, the form still submits natively.

**Dependencies.** UX-02 (same file; the leave-guard must be suppressed while this upload is in flight).

---

### Major

---

#### UX-04 — Applicants who hit an error page are handed to the staff portal

**Screens:** any `/apply/...` 404/500 — most commonly applying to a vacancy that closed (`ApplicantPortalIntakeView.dispatch` raises `Http404`, `recruitment/portal_views.py:207-208`) · **Type:** Design-only + small view change

**Problem.** The global error pages are internal-branded: `templates/404.html` is titled "Page Not Found | **Internal Portal**" and its only action is "Go to Portal" → **`/internal/`** (the staff login); `templates/500.html` says "…contact the **System Administrator**" with the same button. A member of the public who follows an expired link or applies to a just-closed vacancy is dumped on a staff-flavored dead end that tells them, in effect, to log into the government's internal system. Violates: match with the real world; error recovery; trust. Separately, "vacancy closed while you were applying" deserves an explanation, not a 404.

**Evidence.** Both templates read in full; live probes: `/apply/entries/99999/apply/` and `/apply/not-a-uuid/receipt/` render this 404. (Bad *well-formed* tokens are already handled well — friendly message + redirect to `/apply/`.)

**Change specification.**

1. `templates/404.html` and `templates/500.html` — make them audience-neutral:
   - Titles: "Page Not Found | DOH–CHD CALABARZON" / "Service Temporarily Unavailable | DOH–CHD CALABARZON".
   - 404 copy: > "We couldn't find that page. The link may be old or mistyped."
   - 500 copy: > "Something went wrong on our side. Please try again in a few minutes. If you were submitting an application, your typed information may still be saved — use the link in your verification email to continue."
   - Buttons on both: primary "Job Openings" → `/apply/`, secondary "Staff Sign-in" → `/internal/` (small, de-emphasized). Remove "contact the System Administrator" from the 500 (replace with the HRMU public email `recruitment@chd4a.doh.gov.ph` already used in the applicant footer).
2. `recruitment/portal_views.py`, `ApplicantPortalIntakeView.dispatch` (lines 202-209) — replace `raise Http404` for a closed-but-existing entry with:
   ```python
   messages.error(request, "This position is no longer accepting applications. You can browse the other open positions below.")
   return redirect("applicant-portal")
   ```
   Keep the 404 for a non-existent pk. Note the mid-form case is already covered server-side: `submit_application` and `create_public_application_draft` raise plain-language `ValueError`s rendered as form errors — verify, don't change.

**Acceptance criteria.**
- `/apply/entries/99999/apply/` shows the neutral 404 with "Job Openings" as the primary action; no route to `/internal/` is primary.
- Deactivating a demo entry and opening its `/apply/` intake URL redirects to the vacancy list with the "no longer accepting applications" message.
- The internal portal's own 404/500 behavior is unchanged apart from neutral wording (staff can still reach `/internal/` via the secondary link).

**Dependencies.** None.

---

#### UX-05 — The resubmission flow never tells the applicant what was wrong

**Screens:** `/apply/<token>/resubmit/` and the "Action Required" state of `/apply/<token>/status/` · **Type:** Feature (view context) + Copy

**Problem.** When staff return a document, they write a per-document reason (e.g. "The signature is missing. Please upload a signed copy."). That reason goes into the email — but **neither applicant page shows it**. The resubmission page lists only the document name and an upload box; the status page says "Please check your email for instructions." An applicant who taps through from the email (or finds the page via Track Application days later) must recall or re-find the reason on their own. Pre-verification, the resubmit page doesn't even say how many documents are affected. Violates: recognition over recall; plain-language recovery. Hurts reality #2 most.

**Evidence.** Live resubmission round-trip: flagged `signed_cover_letter` with remarks; the rendered page showed the document title, deadline, and upload slot — no remarks anywhere. Code: `get_resubmission_request` (`recruitment/services.py:6981-6996`) returns bare requirement objects, discarding `ScreeningDocumentReview.remarks`; the email builder (`recruitment/notification_services.py:427-434`) includes remarks, proving the data is available and shareable.

**Change specification.**

1. `recruitment/services.py`, `get_resubmission_request` — also return the flagged review rows (they're already fetched in `_flagged_resubmission_reviews`): change the return to include, per requirement, the matching `remarks` (e.g. return a list of `{"requirement": requirement, "remarks": row.remarks}` or attach `.resubmission_remarks` to each requirement object). Update the two call sites in `ApplicantResubmissionView` (`recruitment/portal_views.py:604, 615, 631`).
2. `recruitment/templates/recruitment/applicant_resubmission.html` —
   - In the pre-verification state (Step 1 card), after the deadline banner add: > "**{{ requirements|length }} document{{ requirements|length|pluralize }}** need{{ requirements|length|pluralize:"s," }} to be corrected. Verify your email to see the details and re-upload."
   - In Step 2, above each upload field, render the reason in a highlighted box: > "**Why it was returned:** {{ remarks }}" (fall back to "The recruitment team asked for a clearer copy of this document." when remarks are blank).
3. `recruitment/templates/recruitment/applicant_status_lookup.html` — in the `awaiting_resubmission` block, replace "Please check your email for instructions on what to do next." (sourced from `_APPLICANT_STATUS_LABELS[RETURNED_TO_APPLICANT]`, `recruitment/portal_views.py:57-61`) with copy that stands alone: > "Some of your documents need to be corrected and re-uploaded. Tap Resubmit documents to see which ones and why." (Keep the email mention as a secondary sentence if desired.)

**Acceptance criteria.**
- With a flagged document, the verified resubmit page shows the reviewer's reason above the upload box; with blank remarks it shows the fallback sentence.
- Before OTP verification the page states the number of documents needing correction (not their contents — keep details behind the OTP gate).
- The status page's Action-Required panel explains the situation without requiring the email.

**Dependencies.** None.

---

#### UX-06 — Internal jargon leaks into the applicant's confirmation email

**Screen:** "application received" email · **Type:** Copy + small context change

**Problem.** The portal painstakingly translates workflow states for applicants ("Under Review", "Contract of Service") — but the confirmation email says "**Current status: Secretariat Review**" and "**Recruitment type: COS**". To a first-time applicant, "Secretariat Review" is government-internal vocabulary (and reveals internal structure), and "COS" is an unexplained acronym. Violates: consistency; match with the applicant's language. Hurts reality #2.

**Evidence.** Captured email: `Application ID: RG-20260702-8EF3BC / Position: Security Guard I / Recruitment type: COS / Current status: Secretariat Review` — versus the receipt page which correctly shows "Contract of Service", and the status page which shows "Under Review".

**Change specification.**

1. In `recruitment/notification_services.py`, find the builder for the application-received email (renders `templates/email/application_received.html`/`.txt`) and change the context it passes:
   - status: reuse the portal's mapping — import `_APPLICANT_STATUS_LABELS` from `recruitment/portal_views.py` is backwards (views importing into services is fine the other way); better: move `_APPLICANT_STATUS_LABELS` into `recruitment/services.py` (or a small `recruitment/applicant_labels.py`) and have both the portal views and the email builder read it. Pass `status_label` ("Under Review") instead of the raw internal label.
   - branch: pass "Permanent (Plantilla)" / "Contract of Service" (same conditional the receipt template uses, `applicant_receipt.html:42-44`).
2. Update `templates/email/application_received.html` and `.txt` to print the mapped labels: "Current status: Under Review", "Position type: Contract of Service".

**Acceptance criteria.**
- A fresh submission's confirmation email (console backend) reads "Position type: Contract of Service" and "Current status: Under Review" — no "COS", no "Secretariat".
- The portal status page and internal side are unchanged.
- Plantilla submissions render "Permanent (Plantilla)".

**Dependencies.** None.

---

### Minor

---

#### UX-07 — Every flash message on the application-flow pages appears twice

**Screens:** `/apply/<token>/otp/` (observed), any applicant page defining its own message stack · **Type:** Design-only

**Problem.** `templates/base.html:36-42` renders the Django message stack for all pages, and `applicant_otp.html:45-51` renders its own copy — so "Your application draft is ready. Check your email for the verification code." shows twice, stacked. Duplicate system feedback reads as a glitch and doubles the scroll on a phone. Violates: aesthetic/minimalist design; consistency.

**Evidence.** Screenshot after intake submit: two identical green alerts at the top of the OTP page.

**Change specification.** In `templates/base.html`, wrap the message stack in an overridable block:
```django
{% block message_stack %}
    {% if messages %} … existing markup … {% endif %}
{% endblock %}
```
Then in each applicant template that renders its own stack (`applicant_otp.html` does; grep `rg-pub-message-stack` for others), add `{% block message_stack %}{% endblock %}` to suppress the base copy (keeping the local, better-placed one).

**Acceptance criteria.**
- Submitting the intake shows exactly one "draft is ready" alert on the OTP page.
- Pages without their own stack (vacancy list/detail) still show messages once.
- Internal-portal message rendering is unaffected (it overrides `layout_wrapper` entirely — verify no change).

**Dependencies.** None.

---

#### UX-08 — Identity fields don't offer mobile autofill

**Screen:** `/apply/entries/<pk>/apply/` Step 1 · **Type:** Design-only (form widget attrs)

**Problem.** `first_name`, `last_name`, `email`, `phone` have no `autocomplete` attributes, so mobile keyboards/password managers can't offer one-tap fill of the applicant's own name, email, and number — the exact fields that are tedious on a phone. (The OTP input already does this right with `autocomplete="one-time-code"`.) Violates: flexibility/efficiency. Hurts realities #1–2.

**Evidence.** DOM inspection: `autocomplete: ""` on all four fields; `email` has `type=email` and `phone` has `inputmode=tel` (good — keep).

**Change specification.** In `recruitment/forms.py`, `ApplicantPortalIntakeForm` (~line 544), add widget attrs: `first_name` → `autocomplete="given-name"`, `last_name` → `autocomplete="family-name"`, `email` → `autocomplete="email"`, `phone` → `autocomplete="tel-national"`. (Fields are declared as plain `forms.CharField`/`EmailField`; attach via `widget=forms.TextInput(attrs={...})` or in `__init__` after `_apply_bootstrap()`.)

**Acceptance criteria.**
- The rendered Step 1 inputs carry the four `autocomplete` values.
- Chrome's autofill suggests name/email/phone on the intake form.
- No server-side validation behavior changes.

**Dependencies.** None.

---

#### UX-09 — Invalid file feedback is announced to screen readers immediately but shown to sighted users only on "Next"

**Screen:** `/apply/entries/<pk>/apply/` Step 2 · **Type:** Design-only (JS)

**Problem.** Selecting a wrong/oversized/empty file instantly writes a plain-language message into the hidden `aria-live` region and tints the slot red — but the visible message text only appears when the applicant clicks "Next: Review & confirm". A sighted user sees "Selected: letter.txt · 14 B" next to a subtly red border and reasonably believes the file was accepted, only to be bounced back later. Violates: visibility of system status.

**Evidence.** Live: selected `letter.txt` → announcement populated (`data-file-announce`), slot class `rg-pub-upload-slot--error`, `data-file-validation-error` set on the input, but no visible `.invalid-feedback` until Next was clicked.

**Change specification.** In `applicant_intake_form.html`'s file-change handler (the code that sets `data-file-validation-error` and writes the announcement), also render the visible inline error immediately by calling the existing `renderInlineError(input, msg)` (and clear it via the existing `clearClientErrorForField` when a valid file replaces it or "Remove selected file" is clicked). Keep the announcement as is.

**Acceptance criteria.**
- Selecting a `.txt` file shows "This needs to be a PDF, JPG, or PNG file. Upload one of those." directly under the slot at selection time, without clicking Next.
- Replacing it with a valid PDF clears the message and the red state.
- The Next-click validation still catches anything remaining.

**Dependencies.** None (same file as UX-02/03 — coordinate merge order).

---

#### UX-10 — No way to narrow the vacancy list as it grows

**Screen:** `/apply/` · **Type:** Feature (small, client-side)

**Problem.** The landing page lists every open vacancy as cards grouped by type, with no search or filter. With the current 6 postings this is fine; a real bulletin cycle (dozens of positions across sections) will force phone users to scroll and read every card to find "nurse". Violates: flexibility/efficiency (future-proofing; lowest severity of the set — the page is a light 22 KB today, keep it that way).

**Evidence.** Landing page has zero `<input>` elements; 6 `h3` job cards.

**Change specification.** In `recruitment/templates/recruitment/applicant_portal.html`, above the "Job Openings" section add a single client-side filter input (no backend change):
```html
<label class="visually-hidden" for="rg-job-filter">Filter positions by name</label>
<input id="rg-job-filter" class="form-control" type="search"
       placeholder="Type a position name, e.g. Nurse" autocomplete="off">
```
Plus ~15 lines of JS: on `input`, toggle `hidden` on each `.rg-pub-job-card` whose title/item-number text doesn't include the query (case-insensitive); show a "No positions match '<query>'. Clear the search to see all openings." empty-state line (with `role="status"`) when everything in a section is hidden; hide a section's heading when all its cards are hidden.

**Acceptance criteria.**
- Typing "nurse" leaves only Nurse III visible; clearing restores all 6.
- A nonsense query shows the no-match line; screen readers hear the result change.
- With JS disabled the full list renders as today.

**Dependencies.** None.

---

### Polish

---

#### UX-11 — Small copy and consistency touches observed during the run

**Screens:** various · **Type:** Copy / Design-only

Problems observed, each tiny:
(a) The intake page-level error banner says "You may need to re-select your files." even when all files were preserved in the draft (the per-slot "Saved to this draft" notices then contradict it). (b) The receipt's "Print this page" is an `href="#"` link with inline `onclick` — fine functionally, but it's the only unlabeled control on the page for SR users (announces as "Print this page, link" navigating to "#"). (c) The vacancy detail's "Official Position Reference" block surfaces raw catalog fields ("Class ID: SECG1") that mean nothing to applicants.

**Change specification.**
(a) `applicant_intake_form.html:56-62` — change the banner's second sentence to: "Your typed information has been kept. Files already saved to your draft are listed below; re-select only the slots showing an error."
(b) `applicant_receipt.html:94` — replace the anchor with `<button type="button" class="btn btn-link" onclick="window.print()">Print this page</button>` (styled as the current link).
(c) `applicant_vacancy_detail.html` — keep Salary Grade (applicants care about pay) but move "Class ID" and "Level Classification" into a collapsed `<details>` labeled "Official reference details" so the card leads with salary and section.

**Acceptance criteria.**
- After a failed submit that preserved uploads, the banner and the per-slot notices no longer contradict each other.
- The print control activates print and is a real button.
- Vacancy detail shows Salary Grade prominently; Class ID appears only after expanding the disclosure.

**Dependencies.** (a) pairs naturally with UX-09.

---

## 3. Suggested implementation order

**Batch A — copy, error pages, and small template fixes (independent, low risk):**
UX-04 (error pages + closed-vacancy redirect), UX-06 (email jargon), UX-07 (duplicate messages), UX-08 (autocomplete), UX-11. All are template/context edits verifiable in one pass.

**Batch B — resubmission clarity + recovery links (server context changes, still small):**
UX-01 (OTP email continue-link) and UX-05 (show flagged reasons). Both touch `notification_services`/`services` context plumbing; test the full email + resubmission round-trip after.

**Batch C — intake-form resilience (the Feature risk, one commit, heavy browser testing):**
UX-02 (local draft + leave guard), UX-03 (upload lock + progress), UX-09 (immediate file errors), UX-10 (vacancy filter). UX-02/03/09 all edit `applicant_intake_form.html`'s script block — implement together, then re-run the whole E2E journey at 375px with Slow-3G throttling, including the hostile-file probes (wrong type / oversized / zero-byte / duplicate files) to confirm no regression in the existing validation UX.

After each batch run `manage.py test recruitment` (empty-string `POSTGRES_*` so tests use sqlite) — the portal has substantial test coverage (intake, OTP, resubmission, upload validation); update only tests whose expected copy you intentionally changed.

## 4. Exploratory ideas (for the human — NOT for the implementer)

1. **SMS as a second channel for the OTP and the Application ID.** The intake already collects a Philippine mobile number; many target applicants check SMS far more reliably than email (and prepaid data plans often lapse while SMS keeps working). Offering "text me the code instead" at the OTP step, and texting the Application ID after submission, would remove the single hardest dependency (a working email inbox) for the lowest-literacy applicants. Effort: medium (SMS gateway contract, cost per message, sender-ID registration with PH telcos, resend/lockout parity with email). Biggest risk: recurring cost and procurement; also SIM-swap identity concerns — keep email as the canonical channel and SMS as assist-only. Stays within the OTP design (improves how it works, doesn't remove it).
2. **Assisted-intake (kiosk) mode for walk-in applicants.** A stripped variant of the intake form usable at the HRMU public assistance desk: staff or a kiosk device captures documents via camera, the applicant reviews on-screen, and the OTP goes to whatever inbox they have (or the desk prints the receipt with the Application ID). Helps the applicant with no smartphone or email fluency — the group the pure self-service portal structurally excludes. Effort: medium-high (device provisioning, a desk workflow, privacy handling of documents on a shared device). Biggest risk: staff workload at the desk and blurred accountability for data entry errors.
3. **Deadline-aware nudge emails for unfinished drafts.** The server already stores drafts with uploaded files; a daily job could email "You started an application for Nurse III — the vacancy closes July 14. Continue here." to drafts idle >24 h (using the UX-01 continue link). Turns the biggest silent loss (abandoned drafts) into completions, at near-zero applicant effort. Effort: low-medium (management command + template + opt-out line). Biggest risk: perceived spam / emailing people who deliberately abandoned; send at most one nudge per draft.
4. **Self-service withdrawal from the token status page.** The Help page currently routes withdrawals to the assistance desk. A "Withdraw my application" button on `/apply/<token>/status/` (OTP-gated, confirm dialog, audit-logged, allowed only before deliberation) would close the loop the portal almost offers. Effort: low-medium technically. Biggest risk: policy — HR may require a signed withdrawal letter for the records; needs client sign-off on which stages allow it, which is why it is not a numbered item.

## 5. Out of scope / escalate

- **SECURITY — CAPTCHA appears disabled by configuration.** The repo's `.env` sets `CAPTCHA_ENABLED=False` (with reCAPTCHA keys present but unused). If production runs the same value, the public intake and status-lookup forms rely on rate-limiting alone. Confirming the production posture, and whether the status lookup (`/apply/status/`, an unauthenticated ID+email oracle) is adequately rate-limited, needs the dedicated security review — do not change security config in a UX pass.
- **SECURITY — light checks that passed:** public tokens are UUID4 (unguessable); OTP is 6-digit with server-side attempt counting and 10-minute expiry; wrong-token URLs redirect without information leaks; upload validation enforces size/extension/magic-bytes/MIME server-side. No escalation beyond the CAPTCHA/rate-limit confirmation above.
- **Ops — email links still point at `recruitguard-chd.duckdns.org`.** `APPLICANT_PORTAL_BASE_URL` in the deployment `.env` needs updating to the production `.ph` domain; also the dev `From:` is a personal Gmail. Config change on the server, not portal code.
- **Ops — demo DB has one unapplied migration** (runserver warning). Run `manage.py migrate` on the demo environment.
- **Console mojibake is not a bug.** Emails dumped to the Windows console show `Ni�o` / `DOH�CHD` — that's the terminal's code page mangling UTF-8 log output. The pages, DB round-trip, and email charset headers (`utf-8`) are correct — verified end-to-end with ñ/'/中文. Do not "fix" encoding.
- **OTP/token design itself** — intentional anti-spam/identity control; only its explanation and recovery are improved (UX-01).
- **Privacy notice substance** — legally required content; presentation was reviewed and is acceptable (clear headings on the landing page, plain-language rights list); no change planned.
- **Internal staff screens** (including how staff trigger returns/resubmissions) — covered by `docs/uiux-improvement-plan.md` and `docs/uiux-internal-members-plan.md`, not this pass.
