# Turnover Improvements (TI-1 … TI-5)

Post-defense improvements made for the client, taken from the final manuscript
(`CyberTechies_Full-Manuscript.docx`, 24 Sep 2026): the Chapter 6 recommendations (6.1, 6.2) and
the lowest-rated evaluation items in Chapter 4. Scope agreed with the team: items A–H **except
TOTP**. Deployment hardening (6.4) is deferred.

Already done before this work, so nothing was needed: the vacancy-grouped dashboard
(`VacancyBatchConsoleView`) and the shorter applicant OTP (`APPLICATION_OTP_VALIDITY_MINUTES` = 3).
The manuscript still says the OTP lasts ten minutes.

| ID | Item | Manuscript basis | Where |
|---|---|---|---|
| TI-1 | Session stability | 4.2.3: "session stability" was the lowest reliability item (4.29, SD 0.95) | `config/middleware.py`, `static/js/recruitguard-session.js`, `static/js/recruitguard-autosave.js` |
| TI-2 | File preview, Plantilla/COS explainer, mobile | 6.1 | `applicant_intake_form.html`, `includes/branch_explainer.html` |
| TI-3 | Text size, contrast, compact layout | 6.1 | `templates/display_prefs_head.html`, `display_menu.html`, `text_size_control.html` |
| TI-4 | Finding older records | 4.2.5: "finding older actions and records" scored 4.43 | `ClosedCaseFilterForm`, `ClosedCaseListView` |
| TI-5 | Schedule calendar | 6.1 | `recruitment/schedule_services.py`, `schedule_calendar.html` |

## TI-1 — Session stability

Two root causes were found and fixed:

1. **Autosave showed "Saved" after the session had expired.** It followed the redirect to the
   sign-in page, got a 200 back, and reported success. Now only the autosave branch's `204`
   counts as saved. On a redirect, 401 or 403, the script asks a passive endpoint whether the
   session is still alive. If it isn't, the indicator switches to a persistent alert ("signed
   out — NOT saved…") and autosave stops.
2. **The 30-minute idle timeout never fired while a tab was open.** The notification bell polls
   once a minute, and `SESSION_SAVE_EVERY_REQUEST` re-saved the session on every poll.
   `ActivityAwareSessionMiddleware` now skips that save for requests marked passive, and the
   unread-count poll is marked passive.

Added:
- **Idle warning:** two minutes before the timeout, a dialog offers "Stay signed in" (a
  CSRF-protected POST to `/internal/session/keep-alive/`) or "Sign out". The countdown is shared
  across tabs through localStorage.
- **Keep-alive while working:** typing or clicking sends at most one keep-alive every 5 minutes.
- **Clear sign-out:** when the timer runs out, the page posts the logout and lands on the sign-in
  page with `?next=` and a "signed out after 30 minutes of inactivity" notice.

The 30-minute policy and `SESSION_EXPIRE_AT_BROWSER_CLOSE` are unchanged.

## TI-2 — Applicant portal

- **File preview:** the "View" button opens the chosen file in a dialog. Images and PDFs are
  shown from a local `blob:` URL, and nothing is uploaded early. The CSP allows `blob:` in
  `img-src` and `frame-src` only. Files already saved to a draft show their name only, with no
  new public download URL.
- **Plantilla/COS explainer:** a single include now feeds the Help FAQ, a "What does this mean?"
  disclosure on the vacancy page, a one-line note on the intake form, and a legend on the job list.
- **Mobile (audited at 375px):** no text under 12px, tap targets at least 44px tall, a larger
  confirmation checkbox, and long e-mail addresses wrap.

## TI-3 — Display comfort

- **Text size:** Default, Large (112.5%) or Larger (125%), on both portals and the sign-in
  pages. The setting is stored per browser, applied before first paint, and synced across tabs.
  All screen CSS is in rem, so the whole UI scales. The print templates are stand-alone and are
  not affected.
- **Compact layout:** internal users can choose Comfortable or Compact from the top-bar Display menu.
- **Contrast:** a WCAG AA pass in the light and dark themes. Before/after ratios are noted next
  to each rule in `recruitguard-doh-brand.css`.
- **Fixed along the way:** the case header squeezed the case title at large text sizes or in
  narrow windows, and the internal top bar now fits on phones.

## TI-4 — Closed Cases

- **Filters:** outcome, year closed, a closed-from/to date range, and vacancy. Results can be
  sorted newest, oldest, or by case number.
- **Search:** now also matches applicant e-mail, except for HRMPSB members, whose review stays
  blind.
- **Columns:** Outcome and Closed date.
- **Scope:** all filters narrow `get_closed_cases_for_user`. Invalid values are ignored.
- **Fixes:** the page was titled "Cases I Can View"; the branch chips used to drop the other
  active filters.

## TI-5 — Schedule

- **What it shows:** a read-only Month view and a "Next 30 days" view of exams, interviews,
  application deadlines, resubmission deadlines, completion deadlines, and recorded
  deliberations. A "Coming up · next 7 days" panel is on the Batches landing page.
- **Scope:** only cases the user can already open (queue plus closed cases). An entry manager
  also sees the application deadlines of postings they manage.
- **Privacy:** applicant names are never shown. Batch exams and interviews appear as one event
  with an applicant count.
- **Assumption:** the schema has no planned deliberation date, so deliberations appear on the
  day they were recorded. Scheduling a deliberation ahead of time would need a new model and is
  a separate change.

## Tests

Added `SessionStabilityTests`, `ApplicantPortalTurnoverTests`, `DisplayComfortTests`,
`ClosedCaseLookupTests` and `ScheduleCalendarTests` in `recruitment/tests.py`.
