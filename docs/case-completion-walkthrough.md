# Case Completion Walkthrough — Plantilla / Level‑1, end‑to‑end

**Mission:** carry ONE recruitment case from public applicant submission all the way to
`CLOSED`/archived through the live system's real request → view → form → transition path,
and find out whether a case is currently completable end‑to‑end.

## Verdict

**YES — a case can currently go start‑to‑finish to `CLOSED`.**

A brand‑new Plantilla / Level‑1 case (`Administrative Aide VI`) was driven from the public
3‑step applicant intake all the way to `CLOSED & archived`. **Every** stage transition
fired on the first correct submission. **No system blocker was found** — no view, form,
template, or service transition rejected valid input, no finalize control was missing, and
no transition failed to fire. **Zero changes were made to application code.**

The three problems hit during the run were all in the *test driver*, not the application
(detailed in [Driver‑side issues](#driver-side-issues-not-system-bugs)). The team's
suspicion that a case "may not be completable end‑to‑end" did not hold up: it is completable.

### Evidence case

| Field | Value |
|---|---|
| Reference | `RG-20260626-689980` (application pk 39) |
| Vacancy | `RG-PLT-2026-0015` — Administrative Aide VI, Plantilla, Level 1 |
| Final stage | `closed` · `case_status=approved` · `is_stage_locked=True` |
| Completion | ref `APPT-0625174407`, date `2026-06-26`, requirement "Appointment papers" = completed |
| Live server render | *"Administrative Aide VI · RG‑PLT‑2026‑0015 · Plantilla · Level 1 · **Closed** · Step 6 of 6 · Completion"* |

---

## How it was driven (faithful to "the endpoints the UI submits to")

- The workflow was driven through the **real** URL → middleware → view → form → template →
  service‑transition stack via Django's **test `Client`** (in‑process) against the **live dev
  SQLite database** (`db.sqlite3`). The service functions were **never** called directly to
  drive a transition — every transition was produced by POSTing to the same endpoints the UI
  submits to. Before each action the detail page was `GET`‑rendered and the relevant form was
  confirmed present in the rendered context (this is what would catch a "missing finalize
  button").
- The actual **`runserver` (recruitguard‑internal config) was started on `127.0.0.1:8056`**
  and independently verified to boot and serve the same data over a real HTTP socket —
  including the public portal, a real CSRF+credential login, and rendering the finished
  `CLOSED` case to an authenticated Secretariat user.
- Environment matched the launch config: `DEBUG=True`, SQLite (empty `POSTGRES_*`),
  `SECURE_SSL_REDIRECT=False`, `INTERNAL_MFA_ENABLED=False`.

Driver scripts used for the run (temporary, removed after; no app code touched):
`tmp_e2e_driver.py` (in‑process walkthrough) and `tmp_live_http_check.py` (live‑HTTP proof).

---

## Stage‑by‑stage journey (each transition confirmed)

Confirmed by reloading `RecruitmentCase.current_stage` / workflow section after every step
and cross‑checked against the application audit trail.

| # | Role | Action → endpoint | Result (confirmed) |
|---|------|-------------------|--------------------|
| 1 | Applicant | 3‑step intake: `POST /apply/entries/<pk>/apply/` (info + **9** document uploads) → `POST /apply/<token>/otp/` `action=verify` → `action=finalize` | Draft created → OTP verified → submitted. Case **created at `secretariat_review`** (section `screening`). |
| 2 | Secretariat | `POST /internal/applications/<pk>/screening/` `operation=finalize` (9 doc rows = *Meets*, completeness *Complete*, *Qualified*, scores 91/91/91) | Screening finalized & locked. Section advanced **`screening` → `exam`** (stage stays `secretariat_review`). |
| 3 | Secretariat | `POST .../exam/schedule/` (venue + notify) then `POST .../exam/` `operation=finalize` (technical/practical = 89/89) | Exam scheduled (applicant notified) + finalized. Case **auto‑advanced `secretariat_review` → `hrmpsb_review`** (section `interview`). |
| 4 | Secretariat → HRMPSB → Secretariat | `POST .../interview/` `operation=save` (schedule) · `POST .../interview/rating/` as HRMPSB (score 88) · `POST .../interview/` `operation=finalize` | Session scheduled, rating recorded, session finalized. Section advanced **`interview` → `deliberation`**. |
| 5 | Secretariat / HRMPSB | Close pool `POST /internal/entries/<pk>/status/closed/` · CAR draft `POST .../comparative-assessment/` `operation=save` · finalize deliberation `POST .../deliberation/` `operation=finalize` (HRMPSB) · finalize CAR `operation=finalize` | Pool closed, CAR drafted, deliberation endorsed, CAR finalized. Case **auto‑advanced `hrmpsb_review` → `appointing_authority_review`** (section `decision`). |
| 6 | Appointing Authority | `POST .../final-selection/` (select CAR rank‑1 item + decision notes) | Final selection recorded. Case **auto‑advanced `appointing_authority_review` → `completion`** (`status=approved`, handler back to Secretariat). |
| 7 | Secretariat | `POST .../completion/` (reference + date + requirement "Appointment papers" = completed) then `POST .../close/` (closure notes) | Completion saved (`ready_for_closure=True`) → **`completion` → `closed`**, `is_stage_locked=True`, `closed_at` set. |

The recorded audit‑action sequence for the case (abridged): `application_created` →
`application_otp_verified` → `case_created` → `application_submitted` → `routed` →
`screening_finalized` → `exam_scheduled` → `exam_finalized` → `routed`(→hrmpsb) →
`interview_rating_recorded` → `interview_finalized` → `car_generated` →
`deliberation_finalized` → `car_finalized` → `routed`(→appointing) → `decision_recorded` →
`routed`(→completion) → `completion_recorded` → `case_closed`.

---

## Blockers found in the system

**None.** No code changes were required or made to the application. Each finalize/transition
worked on the first valid submission, including the auto‑routing boundaries
(exam→HRMPSB, CAR→Appointing Authority, selection→Completion) and the final close.

### Driver‑side issues (NOT system bugs)

For transparency, the three issues hit while building the harness — each was a fault in the
test driver, fixed in the driver, and would **not** affect the real browser UI:

1. **`mail.outbox` not initialized** — the locmem backend creates `mail.outbox` lazily on
   first send. *Fix:* initialise it in the driver before reading the OTP.
2. **`response.context` was `None`** — the template‑rendered signal that captures response
   context is only wired up by `django.test.utils.setup_test_environment()`, which I had not
   called. This produced a *false* "screening_form not offered" report. *Fix:* call
   `setup_test_environment()` once. (The view actually offered the form correctly.)
3. **Completion‑requirements formset "This field is required"** — the inline formset renders
   3 empty extra rows. A real browser always submits each row's `status` `<select>` at its
   default (`pending`), so empty rows compare equal to their initial and are skipped. My
   driver omitted `status` for the empty rows, which made them look *changed* and triggered
   validation. *Fix:* submit every rendered field for every row (the browser's behaviour).
   See the non‑blocking note below.

---

## Auth bypasses / environment deviations used (logged)

So the team knows exactly what was **not** exercised through the real UI:

| Bypass / deviation | Why | Effect on coverage |
|---|---|---|
| `INTERNAL_MFA_ENABLED=False` (launch config) | The recruitguard‑internal config disables internal MFA. | The internal **MFA OTP step was not exercised**. Internal logins still went through the real `InternalLoginView` + `InternalAuthenticationForm`. |
| `CAPTCHA_ENABLED=False` (`.env`) | CAPTCHA is disabled in the dev `.env`. | The intake/login **CAPTCHA was not exercised**. |
| **Applicant OTP read from the email backend outbox** | Sanctioned method ("read the code from the console‑email output"). The 6‑digit code is hashed in the DB and only recoverable from the email body. | The OTP **verify + finalize were driven through the real OTP view**; only the *reading* of the code was out‑of‑band. |
| Email backend → `locmem` (driver) / `console` (live server) | Avoid sending real mail to the configured Gmail. Notification code still ran; only transport was redirected. | Real SMTP delivery not exercised (see robustness note — it does not gate the workflow). |
| Workflow POSTs via Django test `Client` (in‑process) | Exercises the identical view/form/template/service stack against the real DB; lets the run assert DB state and switch roles cleanly. | Not driven over the literal socket — but the running `runserver` was independently confirmed to serve the same data over HTTP (incl. real login + viewing the `CLOSED` case). |
| `SESSION_COOKIE_SECURE=False` / `CSRF_COOKIE_SECURE=False` for the **HTTP‑proof server only** | The `requests` client does not honor the localhost `Secure`‑cookie exemption that real browsers do, so the cookies must be non‑secure for a scripted HTTP login. | Cosmetic to the proof harness; **does not touch the workflow**. A real browser logs in fine over `http://127.0.0.1` with the default config. |

No `force_login`/session forgery was used — internal users authenticated through the real
login form.

---

## Non‑blocking observations

- **Completion formset depends on default `<select>` submission.** Empty extra requirement
  rows are only skipped because the browser submits their `status` select at the default
  `pending`. This is correct for a normal browser, but a non‑browser client (or any future
  JS that strips unchanged controls before submit) would hit *"This field is required."* on
  phantom rows. Low‑risk robustness nit; consider giving `CompletionRequirementForm.status`
  `required=False` or marking blank extra rows `empty_permitted` more defensively. Not a
  blocker today.
- **Email outages do not block the pipeline (good).** The exam‑finalize gate requires
  `ExamSchedule.applicant_was_notified`, but `save_exam_schedule` sets `applicant_notified_at`
  at scheduling time *before* the email is queued
  ([`recruitment/services.py:3767`](../recruitment/services.py)). So a broken SMTP backend
  does not strand a case at the exam step. Notifications that fail are recorded, not raised.
- **HRMPSB ordering is guided, not enforced by error.** At `hrmpsb_review` the detail view
  surfaces `deliberation_requires_car_draft` / `car_requires_finalized_deliberation` flags so
  the correct order (close pool → CAR draft → finalize deliberation → finalize CAR) is clear
  in the UI. Driving them in order worked with no surprises.

---

## Code changes

**None to application code.** The only files created were the two temporary harness scripts
(`tmp_e2e_driver.py`, `tmp_live_http_check.py`), which were removed after the run. The single
DB migration applied was the already‑present, not‑yet‑applied
`recruitment/migrations/0038_exam_schedule_applicant_notifications.py` (`manage.py migrate`).

### What unblocked each stage

Nothing needed unblocking in the system. The stages that the team most suspected
(auto‑routing boundaries and the final completion/close) all worked as built.
