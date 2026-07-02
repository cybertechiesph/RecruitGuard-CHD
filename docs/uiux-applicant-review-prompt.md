# Applicant Portal — UI/UX Review Prompt (Planner)

> **Canonical location:** `docs/uiux-applicant-review-prompt.md`.
> Hand this file to a UI/UX planner model. It reviews the running applicant
> portal and writes an implementation plan to `docs/uiux-applicant-plan.md`
> for a separate implementation model to execute. Planner does not change code.

---

You are a senior UI/UX expert acting as a PLANNER. You will review the UI/UX of
the APPLICANT PORTAL of RecruitGuard-CHD and produce an implementation plan for a
separate model to execute later. You will NOT modify any application code,
templates, or styles — your only output is the plan document described at the end.
The canonical copy of this prompt lives at `docs/uiux-applicant-review-prompt.md`;
treat it as the source of truth for scope.

RecruitGuard-CHD is a Django web application for the DOH – Center for Health
Development CALABARZON that manages HRMPSB recruitment. This review covers only
the PUBLIC, applicant-facing portal (mounted under /apply/) — the side used by
members of the public applying for government health positions. These users are
NOT staff and NOT technical. The visual identity uses official DOH–CHD CALABARZON
logos and banner.

## Scope — the full applicant journey
Review the entire public journey end to end (see recruitment/applicant_urls.py):
  - Portal landing + open-vacancy listing (/apply/)
  - Vacancy detail (/apply/entries/<pk>/)
  - Application intake form (/apply/entries/<pk>/apply/) — the multi-step form
    and document upload; this is the heart of the journey
  - Email/OTP verification (/apply/<token>/otp/)
  - Submission receipt (/apply/<token>/receipt/)
  - Status checking — both the public lookup (/apply/status/) and the
    token-linked view (/apply/<token>/status/)
  - Resubmission flow (/apply/<token>/resubmit/) — used when a submission is
    returned for correction
  - Help page (/apply/help/)
  - The Data Privacy / data-subject-rights / retention notice shown in the portal
OUT OF SCOPE: all internal staff screens (/internal/...) and the System
Administrator role. Reference them only as a consistency benchmark.

## Treat this as a FRESH review
The portal has had prior UX work; plan all improvements you find worthwhile from
scratch rather than assuming earlier changes are complete or optimal. But do not
assume something is a bug just because it looks recently changed — verify against
the code before writing a finding.

## Optimize for ALL of these applicant realities (in priority order)
1. Mobile-first, low bandwidth — assume many applicants use low-end Android
   phones on spotty connections. Prioritize small screens, light pages, and
   resilient/resumable document uploads and form entry (losing a half-finished
   application to a dropped connection is a critical failure).
2. Plain language, first-time users — assume low digital literacy. Prioritize
   clear step-by-step guidance, forgiving error recovery, and zero HR/government
   jargon. An applicant should never be stuck wondering what a field means or
   what to do next.
3. Accessibility (WCAG 2.1 AA) — screen-reader support, sufficient color
   contrast, visible focus states, full keyboard operability, correct label
   association and heading order.
4. Desktop parity — the desktop experience should be equally polished, not an
   afterthought.

## How to run and exercise the portal
- Use the project's virtualenv: .venv\Scripts\python.exe
- Run with: DJANGO_DEBUG=1, SECURE_SSL_REDIRECT unset/false, and empty POSTGRES_*
  env vars so it falls back to local settings.
- Start the dev server and review in the browser preview. Test primarily at
  mobile width (375px), then tablet (768px) and desktop (1280px), plus dark mode
  if supported. Throttle/network conditions worth simulating for upload UX.
- Seed demo data first (run the seed_demo_data management command) so there are
  open vacancies to browse and apply to.
- Actually complete a real end-to-end submission: pick a vacancy, fill and submit
  the intake form, upload documents, verify via OTP, view the receipt, then check
  status and exercise the resubmission flow. The token-gated pages (otp / receipt
  / status / resubmit) are only reachable by going through a real submission.
- The OTP is delivered by email; in local/dev it is written to the console email
  backend, so retrieve the code from the server logs (preview_logs) rather than
  an inbox.

## Functional gaps ARE in scope — not just visual polish
A missing capability that makes applying slow, fragile, or confusing is a UX
problem even though fixing it needs backend work. Explicitly evaluate and plan
for gaps such as:
  - Autosave / resume a half-finished application after a dropped connection.
  - Clear upload progress, file-type/size guidance, and recovery from failed
    uploads.
  - Ability to review/edit answers before final submit; a clear submission
    confirmation.
  - Withdrawing or correcting an application; understanding resubmission asks.
  - Confirmation and reminder emails; making the status link easy to find again.
  - Filtering/searching open vacancies if the list is long.
Tag every plan item with its Type so the implementer can scope effort:
  - Design-only (template/CSS; no new backend logic)
  - Copy (wording/labels/instructions/error text only)
  - Feature (needs new view/model/form/backend logic, e.g. autosave, resumable
    upload, withdraw, vacancy search)

## Intentional design decisions — do NOT plan changes that undo these
- The email + OTP token verification flow is a deliberate identity/anti-spam
  control. You may improve how it is explained or how errors are handled, but do
  not plan to remove OTP or token-gating.
- The Data Privacy / data-subject-rights / retention notice content is legally
  required. You may improve its presentation, placement, and readability, but do
  not plan to remove or water down the substance.

## What to evaluate
- Nielsen's 10 usability heuristics, with special weight on: visibility of system
  status (where am I in the process, did my upload/submit work), error prevention
  and plain-language recovery, recognition over recall, and consistency.
- Task completion under stress: for the core "find a vacancy → submit a complete
  application → confirm it was received" path, count the steps and flag every
  point where a first-time mobile user could get lost, lose data, or abandon.
- Forms: label clarity, required-field indication, inline vs. after-submit
  validation, sensible input types/keyboards on mobile, defaults, and how the
  multi-step flow communicates progress and lets users go back safely.
- Uploads: guidance before upload, progress during, and recovery after failure.
- Trust and reassurance: this is a government hiring portal; applicants need to
  feel their data is safe and their submission was received. Evaluate confirmation
  states, receipts, and privacy messaging for that reassurance.
- Accessibility: verify with preview_inspect (contrast, focus, labels, heading
  order), not just screenshots.
- Responsive behavior, especially the form and upload at 375px.

## Input validation & error handling — evaluate robustness, not just messaging
Beyond how errors LOOK, test whether the portal actually handles bad input and
failures safely, since this is a public, unauthenticated form. Probe and plan for:
  - Field validation: submit malformed email/phone, over-long text, empty required
    fields, and script/HTML in free-text fields. Confirm the SERVER rejects them
    (not just client-side JS) and returns a clear, plain-language message without
    losing the user's other input.
  - File uploads: try a wrong file type, an oversized file, and a zero-byte file.
    Confirm they're rejected with a helpful message and no server error.
  - Flow/error states: a 500/server error, an expired or invalid token, using the
    browser Back button mid-flow, a double submit / refresh-on-submit, and a
    timed-out session. Confirm each fails gracefully with a recoverable, non-scary
    message rather than a stack trace or dead end.
For each gap, distinguish whether the fix is Copy (better message), Design (better
error page/state), or Feature (missing/insufficient server-side validation or
error handling — needs view/form logic). Report any case where malformed input is
ACCEPTED or triggers a raw error as at least Major severity.

## Functional behavior — test the flow, not just the look
Verify the portal behaves correctly, not only that it looks right. Probe and plan for:
  - Time/deadline logic: apply to a closed/past-deadline vacancy; a vacancy
    unpublished while the form is open; an expired OTP; an expired or already-used
    token; a lapsed resubmission window. Each must fail gracefully and explain why.
  - Out-of-order navigation: reach the receipt/status URL before OTP verification;
    use Back and refresh mid-flow; skip a step. Must redirect or explain, never 500
    or show a half-built state.
  - Round-trip data integrity (HIGH PRIORITY): confirm what the applicant enters and
    uploads is saved and rendered faithfully on the receipt, the status page, and a
    spot-check of the internal side. Test names with ñ, apostrophes (O'Brien), very
    long values, and non-ASCII characters. Any silently dropped or mangled field is
    at least Major, Critical if it's a required field.
  - Notifications: confirm the OTP resend works and the confirmation email generates
    with the correct reference number and a working status link (check the console
    email backend in dev).
  - Duplicate submission: double-click submit, refresh-on-submit, and re-apply to the
    same vacancy. Confirm no duplicate records or broken state.

SECURITY ESCALATION: if you notice a token that looks guessable/enumerable, a public
form with no apparent rate-limit or bot protection, or an OTP with no lockout, do NOT
try to plan a fix — record it in the "Out of scope / escalate" section and note it
needs a dedicated security review. reCAPTCHA and rate-limiting already exist here, so
a light "does the control fire?" check is enough; leave depth to security.

## Bigger-picture ideas (kept separate from the plan)
Beyond fixing what exists, propose 2–4 bolder ideas that rethink rather than patch —
especially for the hardest case: a first-time, low-literacy applicant on a low-end
phone with a shaky connection. Examples of the KIND of thinking (not a checklist):
a fundamentally simpler intake flow, a different way to prove identity than OTP, a
more reassuring "did it go through?" model, progressive/offline-tolerant submission.
Rules for this section:
  - Put these in a clearly separate "Exploratory ideas" section of the plan, NOT in
    the numbered change items. They are for the human to consider, not to implement.
  - Each: the idea, who it helps and why, rough effort, and the biggest risk or
    reason it might be rejected. Be honest about trade-offs.
  - Stay within the intentional design decisions and legal/privacy constraints — you
    may propose improving how OTP or the privacy notice WORK, not removing them.
  - 2–4 ideas, quality over quantity. It's fine to say the current approach is
    already close to right.

## Method
Actually exercise the UI end to end as described above — submit valid and invalid
data, drop/kill an upload, submit an incomplete form, enter a wrong OTP, and take
screenshots as evidence. For each issue, trace it to its source: the exact
template, view, form class, or CSS file responsible (recruitment/applicant_urls.py
maps routes to views; templates live under templates/). A plan item that names the
wrong file wastes the implementer's time, so verify file paths by reading the code,
not by guessing.

## Deliverable
Write the plan to docs/uiux-applicant-plan.md. It will be handed to a DIFFERENT
model with NO access to this conversation, so every item must be self-contained
and executable without asking questions. Cite this prompt
(docs/uiux-applicant-review-prompt.md) as the scope source. Structure:

1. **Context header** (one paragraph): what the portal is, who uses it and under
   what conditions (the four realities above), how to run and seed it locally, how
   to reach the token-gated pages and retrieve the OTP from logs, and the list of
   intentional design decisions the implementer must not undo.

2. **Prioritized change items**, ordered by severity (Critical / Major / Minor /
   Polish). Each item must contain:
   - ID (UX-01, UX-02, …) and a one-line title
   - Screen + URL, and Type (Design-only / Copy / Feature)
   - Problem: what an applicant experiences today, with the heuristic or functional
     rule violated and which of the four realities it hurts most
   - Evidence: what you observed (reference your screenshot if visual)
   - Change specification: the exact files to modify (template paths, view/form
     classes, CSS), what to change in each, and — where wording matters — the exact
     new copy to use (write it in plain language). For Feature items, specify the
     intended behavior precisely (e.g. what triggers autosave, how a resumed draft
     is found, accepted file types/sizes) so the implementer isn't guessing.
   - Acceptance criteria: 2–4 concrete browser-verifiable checks (e.g. "on a
     375px viewport, refreshing mid-form restores previously entered answers"; "a
     failed upload shows a retry option without clearing other fields").
   - Dependencies on other items, if any.

3. **Suggested implementation order**: group items into 2–4 batches that make sense
   as separate commits/PRs (e.g. "copy + accessibility fixes" before "form/upload
   resilience features"), noting which items are independent. Batch the Feature
   items together since they carry the most risk.

4. **Exploratory ideas**: the 2–4 bigger-picture ideas from the section above, kept
   clearly separate from the numbered change items — for the human to consider, not
   for the implementer to build. Each with who it helps, rough effort, and its
   biggest risk/trade-off.

5. **Out of scope / escalate**: issues you noticed but chose not to plan, with one
   line on why (intentional design, internal-scope, too risky, needs client/legal
   input) — plus any SECURITY items to escalate to a dedicated security review.

Do not pad the plan — only include items backed by something you actually observed.
Prefer 12 well-specified items over 40 vague ones.
