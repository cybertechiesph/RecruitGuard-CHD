# Codex backend asks — running queue

Items the UI/UX work has surfaced for Codex to address on the backend.
Append-only list; each item names the slice it originated in so the
context is recoverable.

---

## From Slice C — Interview stage

### C-1. Notify HRMPSB members on session schedule
**Why.** The Secretariat / HRM Chief schedules an `InterviewSession`
(date + location). Today, panel members have no signal to act — they
only learn the session exists if they happen to open the case.

**Ask.** When an `InterviewSession` is saved for the first time
(non-finalize "save" operation) or when its `scheduled_for` / `location`
materially changes, email every HRMPSB member who can rate this case
(`user_can_manage_interview_rating` set). Include applicant name, position,
date, time, location.

**Confirm first.** Is this already implemented? Quick grep on
`InterviewSession` save / `interview-session` view / `notification_services`
should reveal it.

---

### C-2. Server-side past-date validation on `InterviewSession.scheduled_for`
**Why.** The rewritten session form (`interview_session_manager.html`)
validates client-side that the date isn't in the past, but a determined
or scripted POST will bypass it.

**Ask.** Reject in `InterviewSessionForm.clean_scheduled_for` (or the
service that persists the session) when `scheduled_for < now() - 5 min`.
Plain error: *"The interview can't be scheduled in the past."*

**Exception to allow.** Editing an *existing* session whose
`scheduled_for` is already in the past should be allowed for record
keeping (or explicitly disallowed — Codex's call).

---

### C-3. Confirm and document the "minimum ratings to finalize" rule
**Why.** The new session-manager UI gates **Finalize session** behind
"≥1 panel rating *or* ≥1 fallback sheet on record." I inferred that from
the old template. If the actual policy is stricter (e.g. "all panel
members must have submitted"), the UI gate must match.

**Ask.** Confirm the rule and, if stricter than what I implemented,
patch `route_finalize_interview_session` (or equivalent) so the server
enforces it. I'll mirror the same rule in the UI gate when you confirm.

---

### C-4. Optional: self-rating guard
**Why.** Not addressed in this slice. If there's any chance an HRMPSB
member could be related to or biased toward an applicant, server-side
validation could refuse the rating. The UI side has no way to know.

**Ask.** Add a conflict-of-interest flag on `InterviewRating.save` or
the rating view, if policy requires it. UI side: I can render a "you
cannot rate this applicant" banner if Codex exposes a permission
helper (e.g. `user_can_rate_this_applicant(user, application)`).

---

## From Slice D — Cross-cutting

### D-1. Remove "overview" from get_application_detail_tab choices
**Why.** The Overview tab's only content was an Applicant Snapshot card.
Slice B replaced it with a permanently visible "Applicant submission" panel
above the tab nav, so the tab now duplicates data and (after the D4
cleanup) renders an empty main column.

**Ask.** In `recruitment/services.py` (or wherever
`get_application_detail_tab` builds its choices), drop the `"overview"`
entry from the tab list and adjust the default-tab fallback to land on
`"screening"` (or the earliest available stage) instead. The template
already removed the rendering block.

**Care.** Make sure any direct links / tests that pass `?tab=overview`
either still resolve gracefully or are updated to use a real tab key.

---

### D-2. Lean autosave endpoint (optional)
**Why.** D2 added silent background autosave on the screening and
interview-session forms. It reuses the existing Save Draft endpoints,
which redirect on success. Each silent save therefore costs an extra GET
round-trip (whose response body is discarded).

**Ask (optional, not blocking).** When a draft POST carries the header
`X-Requested-With: RG-Autosave`, return a `204 No Content` instead of
the usual 302 redirect. The JS already sends that header so the backend
can detect it cleanly.

---

## Slice D — Backend dependencies for pending sub-slices

The two Slice-D sub-slices below are intentionally **UI-blocked**: I
already know what to build on the front-end, but the backend needs to
expose the data first. Land these and I'll wire the UI in a follow-up
slice.

---

### D-3. Notification model + emission hooks (for D1 — in-app notifications)

**Why.** Today every routing event is silent. The Secretariat returns a
case, the HRM Chief endorses, the applicant resubmits — nothing surfaces
to the recipient until they remember to open the case. Slice D's
notifications fix that. The UI shell I'll build (bell icon + unread
badge + dropdown + dedicated page) is straightforward once a real model
is queryable.

**Model.**

```python
class Notification(TimestampedModel):
    class Kind(models.TextChoices):
        CASE_ASSIGNED          = "case_assigned",          "Case assigned to you"
        CASE_RETURNED          = "case_returned",          "Case returned to you"
        SCREENING_FINALIZED    = "screening_finalized",    "Screening finalized"
        RESUBMISSION_RECEIVED  = "resubmission_received",  "Resubmission received"
        INTERVIEW_SCHEDULED    = "interview_scheduled",    "Interview session scheduled"
        INTERVIEW_FINALIZED    = "interview_finalized",    "Interview session finalized"
        DEADLINE_APPROACHING   = "deadline_approaching",   "Deadline approaching"

    recipient    = models.ForeignKey(RecruitmentUser, on_delete=CASCADE, related_name="notifications")
    kind         = models.CharField(max_length=40, choices=Kind.choices)
    title        = models.CharField(max_length=200)            # plain-English headline
    body         = models.CharField(max_length=400, blank=True)# one short sentence
    related_url  = models.CharField(max_length=400, blank=True)# e.g. /internal/applications/2/?tab=screening
    application  = models.ForeignKey(RecruitmentApplication, on_delete=CASCADE, null=True, related_name="notifications")
    read_at      = models.DateTimeField(null=True, blank=True)
```

Index on `(recipient, read_at, created_at)` for the unread-count query.

**Emission hooks** (centralize in `recruitment/notification_services.py`):

| Event | Recipient(s) | Title pattern |
|---|---|---|
| `route_case_between_secretariat_and_hrm_chief` succeeds | Target office's user(s) | *"RG-X handed off to you by Y"* |
| `process_workflow_action` with action `return_to_applicant` | Applicant *(if applicant accounts notify)* — and the role who'd next pick it up if/when re-submitted | *"Application returned to applicant"* |
| `process_workflow_action` with `endorse` | Receiving role | *"RG-X endorsed to you for HRMPSB review"* |
| Applicant resubmits documents (post-resubmission flow Codex already built) | Original Secretariat / Chief | *"RG-X applicant resubmitted N documents"* |
| `save_interview_session` first save | All HRMPSB members of the case | *"Interview scheduled for RG-X on {date}"* |
| Interview session finalized | Panel members who hadn't submitted | *"Interview session finalized — no further ratings"* |
| Posting deadline ≤ 24 hours away (a daily cron) | Assigned handler | *"RG-X is closing in 24 hours"* |

**Query helpers.**

```python
def get_recent_notifications(user, limit=10): ...
def get_unread_count(user): ...
def mark_notification_read(notification_id, user): ...     # only the recipient can mark
def mark_all_notifications_read(user): ...
```

**Endpoints (UI needs these).**

| Method | Path | Returns |
|---|---|---|
| `GET`  | `/internal/notifications/`              | full list view (HTML) |
| `POST` | `/internal/notifications/<id>/read/`    | mark one read; redirect to `related_url` |
| `POST` | `/internal/notifications/read-all/`     | mark all read; redirect back |
| `GET`  | `/internal/notifications/unread-count/` | tiny JSON `{count: N}` for the bell badge (polled lightly or hit on page nav) |

**Care.**
- Notifications are **per recipient**, not broadcast — never leak case data to users who don't have `user_can_view_application`.
- Soft-delete or auto-purge old ones after a sensible window (90 days?) to keep the table tidy.
- The `related_url` is the single "click here" destination per notification; keep it stable.

---

### D-4. `stage_entered_at` timestamp (for D5 — SLA / time-in-stage badge)

**Why.** D5's small *"In Screening for 3 days"* badge on the case header
and queue rows can't be derived from `updated_at` because every save
bumps that. The actual time-in-stage is a meaningful number for the
Secretariat, the Chief, and any reviewer of process health.

**Schema.**

```python
class RecruitmentCase(TimestampedModel):
    ...
    current_stage     = models.CharField(...)
    stage_entered_at  = models.DateTimeField(default=timezone.now)  # NEW
```

**Write-side change.** Every place that updates `current_stage` should
also set `stage_entered_at = timezone.now()`. Best done by funneling
those writes through a single service helper (e.g.,
`_transition_case_stage(case, new_stage)`) and calling it from
`process_workflow_action`,
`route_case_between_secretariat_and_hrm_chief`, and any other place
that flips the stage. A test asserting the timestamp moves on each
transition would lock it down.

**Migration.** Backfill `stage_entered_at = updated_at` for existing
rows — a reasonable approximation; the alternative (replay
`RoutingHistory`) is much heavier and not needed for the UI to be useful.

**Helper (for the template).**

```python
@property
def time_in_current_stage(self):
    return timezone.now() - self.stage_entered_at
```

**Optional SLA thresholds** (constants or settings) — small dict per
stage so the UI knows when to switch the badge from neutral → amber →
red. If you'd rather not configure, hard-code 5 days = amber, 7 days =
red and surface it as a Codex-side constant the UI can read.

**Care.**
- A "stage" in this system can be revisited (return to applicant → comes
  back → same stage again). When that happens, **reset**
  `stage_entered_at` to "now" — the SLA clock starts over each entry
  into the stage, not the cumulative time.

---

## From Slice D1 — In-app notifications UI (landed)

### D1-note. Replaced NotificationListView stub with template render
**What changed.** The shipped `NotificationListView.get` returned a
hand-built HTML string (clearly a stub). I swapped it to:

```python
return render(request, "recruitment/notification_list.html", {
    "notifications": notifications,
    "unread_count": unread_count,
    "has_unread": unread_count > 0,
})
```

…and added the matching template at
`recruitment/templates/recruitment/notification_list.html`. The behavior
(login required, internal-user required, lists up to 100 most recent) is
unchanged; only the rendering switched from inline HTML to a Django
template that extends `internal_base.html`. The unused `get_token`
import on that view can be cleaned up next pass if Codex prefers.

### D1-followup (optional). Have form-post redirects honor `next`
**Why.** The bell dropdown and full-page list both send a `<input
type="hidden" name="next" value="…">` along with mark-read and
mark-all-read POSTs. Today the views use the HTTP `Referer` header as
the primary fallback, which works in practice but is fragile under
strict referrer policies.

**Ask (optional).** In `NotificationReadView.post` and
`NotificationReadAllView.post`, prefer `request.POST.get("next")` over
`HTTP_REFERER` when validating the redirect target via
`_safe_internal_redirect`. Falls back to the existing chain otherwise.

---

## From Slice D5 — Time-in-stage badge (UI blocked on helper)

### D5-1. SLA threshold helper for the time-in-stage badge
**Why.** `stage_entered_at` and `time_in_current_stage` are now on
`RecruitmentCase`, so I can render a plain *"In screening for 3 days"*
badge on the case header and queue rows. But the user wants
color-coded SLA states (neutral → amber → red) and your message
flagged that the threshold policy should live on the backend so the
queue list, the case header, and any other surface share one rule.

**Ask.** Expose a small helper that returns the SLA severity + the
elapsed duration for a case's current stage. Two shapes that would
work — pick whichever fits the backend better:

**Option A — model property** *(simpler):*

```python
# recruitment/models.py — RecruitmentCase
@property
def stage_sla_state(self):
    """Return one of: 'ok', 'warning', 'overdue'."""
    elapsed = self.time_in_current_stage
    if elapsed >= STAGE_OVERDUE_THRESHOLD:
        return "overdue"
    if elapsed >= STAGE_WARNING_THRESHOLD:
        return "warning"
    return "ok"
```

**Option B — template tag** *(centralizes the rule alongside the other
`recruitment_ui` tags):*

```python
@register.simple_tag
def stage_sla_state(recruitment_case):
    ...
```

**Thresholds.** Default suggestion (tune as you see fit):

| State    | Trigger                                        |
|----------|------------------------------------------------|
| ok       | < 5 days in current stage                      |
| warning  | ≥ 5 days and < 7 days                          |
| overdue  | ≥ 7 days                                       |

Per-stage thresholds would be nicer (e.g. screening = 3/5 days,
deliberation = 7/14), but a single global pair is fine for v1 — I'd
rather ship the badge than wait for per-stage tuning.

**Care.**
- Cases in `current_stage = CLOSED` should report `"ok"` (or be
  excluded from SLA — your call). The badge will be hidden in that
  case anyway, but a stable return value keeps the helper safe to call.
- Returned-to-applicant cases: should the clock pause while the case
  is with the applicant, or keep counting? My instinct is to **pause**
  — the office can't act on it. But it depends on whether the SLA is
  "office responsiveness" or "applicant journey time."

I'll consume whichever shape you ship; just let me know the import
path and I'll wire the badge in one pass on `case_header.html` and
the queue row template.

---

## From Slice V — End-to-end verification (in flight)

### V-seed. Seed late-stage test cases for live verification
**Why.** Today's E2E pass covered Secretariat → Screening → Deliberation/CAR
because the existing seed data only had cases at the early stages. The
following UI surfaces still need a real-data live walk:

- **Exam stage** (3-step wizard: type & administration → outcome & scores → supporting evidence)
- **Decision stage** for both branches:
  - COS final decision at **HRM Chief Review**
  - Plantilla final selection at **Appointing Authority Review**
- **Completion stage** (track completion → close & archive wizard, plus the requirement-entry rows with the blank-row "add new" behavior)
- **Final Selection** at Appointing Authority Review (the rank/justification flow with the deep-selection lock for rank > 5)

**Ask.** Add one realistic test case per missing surface, with the
prerequisite records already in place so the templates render with real
data — not empty/default state. Concretely:

| Case | Branch | Current stage | Required prior records |
|------|--------|---------------|------------------------|
| RG-COS-test-screening | COS | secretariat_review | Submitted application with required documents uploaded; no finalized screening yet |
| RG-PLT-test-exam | Plantilla | hrm_chief_review | Screening finalized (qualified, complete) |
| RG-COS-test-decision | COS | hrm_chief_review | Screening finalized + (optional) exam record |
| RG-PLT-test-final-selection | Plantilla | appointing_authority_review | Screening + exam + interview ratings (≥1 panel rating) + finalized deliberation + finalized CAR with at least 6 ranked items, and no final selection yet |
| RG-PLT-test-aa-decision | Plantilla | appointing_authority_review | Finalized CAR with ranked items and no final selection yet; kept as a smaller Appointing Authority selection case |
| RG-PLT-test-aa-return | Plantilla | appointing_authority_review | Finalized CAR with ranked items and no final selection yet; kept as a separate Appointing Authority CAR return/reassessment case |
| RG-PLT-test-completion | Plantilla | completion | Plantilla final selection recorded + completion record initialized + at least one saved completion requirement and one blank slot |

If a management command makes sense, a `seed_e2e_test_cases` would also
let us reset the test bed cheaply.

**Care.** Use the j3r1c02@gmail.com applicant pool when possible. If the
pool does not have enough distinct applicant users for the six-person
CAR case, create deterministic synthetic users with the `e2e_seed_applicant_`
prefix. Make the applicant names easy to grep (e.g., "E2E Exam", "E2E
Final Selection") so they don't clutter the real-looking test set.

---

## From Slice H — Applicant portal home page polish

### H-A. Add a `salary_grade_display` property on `PositionPosting`
**Why.** I want to surface salary grade ("SG 7") on the applicant home-page
job cards next to the Level pill — biggest remaining triage signal for
Filipino applicants. The data already exists on
`PositionReference.salary_grade` (PositiveSmallIntegerField), but reaching
it from a template via `entry.position_reference.salary_grade` is fragile:
it hops a related object that may not always be prefetched, and it has no
graceful fallback when the value is null.

**Ask.** Add this property on `PositionPosting`:

```python
@property
def salary_grade_display(self):
    """Plain "SG 7" string or empty string when unknown."""
    reference = getattr(self, "position_reference", None)
    sg = getattr(reference, "salary_grade", None)
    return f"SG {sg}" if sg else ""
```

That's it — 5 lines, no migration, no test fixture change.

**Out of scope for this ask.** Converting SG → indicative monthly peso is a
separate decision. The CSC salary-grade table changes annually and there
are policy questions about which step to show. Land the display string
first; we can add peso later if/when the policy is settled.

---

## From UT-006 — Pre-fill the intake form from an existing draft token  ✅ LANDED

### UT-006. `ApplicantPortalIntakeView.get` should rehydrate from `?token=...`
**Why.** Today the OTP page's "Use a different email" link sends the applicant back to the intake form — and the form is blank. Their name, contact, qualification summary, etc. are gone, even though the draft application still exists in the database with all of that data. Uploaded documents persist (the form's POST validation already finds them via `attach_existing_draft`), but the typed fields don't.

I've added an honest `window.confirm()` warning on the OTP link so users aren't surprised. But the right fix is server-side: detect `?token=...` on `applicant-intake` GET, look up the draft, and use its data as form initial values.

**Where.** `recruitment/portal_views.py`, `ApplicantPortalIntakeView`.

**Ask.** Override `get`:

```python
def get(self, request, *args, **kwargs):
    token = request.GET.get("token")
    draft = None
    if token:
        draft = (
            RecruitmentApplication.objects
            .filter(
                public_token=token,
                position=self.entry,
                submitted_at__isnull=True,
                status=RecruitmentApplication.Status.DRAFT,
            )
            .prefetch_related("evidence_items")
            .first()
        )
    if draft is None:
        return super().get(request, *args, **kwargs)

    initial = {
        "first_name": draft.first_name,
        "last_name": draft.last_name,
        "email": draft.applicant_email,
        # ...add every applicant-facing field that's on the draft
    }
    form = self.get_form_class()(entry=self.entry, initial=initial)
    form.attach_existing_draft(draft)
    return self.render_to_response(self.get_context_data(form=form))
```

The exact `initial` mapping depends on which fields `ApplicantPortalIntakeForm` exposes and how they map to `RecruitmentApplication`. Codex's call.

**Care.**
- The token comes from the OTP page, so we know the applicant has at least started the draft. No new privacy surface — the draft was already accessible via the OTP URL.
- If the token is invalid or doesn't match this position, silently fall through to the empty form (don't 404 — applicants don't need to see that).
- After Codex lands this, the front-end can drop the `window.confirm()` warning. I'll do that in a follow-up.

**Out of scope.** Auto-saving the form while the applicant types is a separate (larger) ask; not needed for this bug.

**Resolution.** Codex implemented this in `ApplicantPortalIntakeView.get`. Valid token rehydrates name, email, phone, qualification summary, cover letter note, performance-rating choice, checklist confirmations, and re-attaches uploaded documents. Invalid/mismatched tokens silently fall through to a blank form. Frontend confirm-warning has been removed; "Go back to edit your information" copy restored.

---

## From TA1 — Track Application redesign (follow-ups)

### TA-M1. Include a magic-link to the status page in applicant emails
**Why.** The redesigned status page is now useful enough that applicants will want to come back to it. Today they have to remember their Application ID + email and re-type both each time. Filipino applicants often use shared devices (internet cafés, family laptops, borrowed phones) — bookmarks aren't a reliable form of "come back." Their email inbox is.

**Ask.** Generate a one-click status-page URL per application and embed it in:

1. **Submission confirmation email** — first delivery of the link
2. **Status-change emails** — every time we send a status update notification, include the same link so each email is self-sufficient

**Shape.** The simplest form is `/apply/<token>/status/` where `<token>` is the application's `public_token` (already used for the OTP / receipt flow). A new view that accepts the token, looks up the application, and renders `applicant_status_lookup.html` with the same context the current form-based view produces.

```python
# Sketch:
path("<uuid:token>/status/", ApplicantStatusLinkView.as_view(), name="applicant-status-link"),

class ApplicantStatusLinkView(TemplateView):
    template_name = "recruitment/applicant_status_lookup.html"

    def get(self, request, *args, **kwargs):
        application = (
            RecruitmentApplication.objects
            .select_related("position")
            .filter(public_token=self.kwargs["token"], submitted_at__isnull=False)
            .first()
        )
        if application is None:
            messages.error(request, "This status link is no longer available...")
            return redirect("applicant-status-lookup")
        # build the same context as the lookup view's form_valid
        ...
```

**Email template snippet.** In every applicant email (existing + future), add a line near the end:

> **Check your application status anytime:** {{ status_link }}

**Care.**
- The token is essentially a long-lived bearer credential. Same risk surface as the existing OTP/receipt links — acceptable for "check status," not for any write actions.
- If the application is in DRAFT (OTP not yet verified) the link should redirect to the OTP page instead of 404'ing.

**Resolution.** Codex added `/apply/<token>/status/`, backed by `ApplicantStatusLinkView`. Submitted applications render the same redesigned status page context as the form lookup. Draft/unsubmitted applications redirect to the OTP page with a clear message so the applicant can verify or resend the code. Applicant-facing notification emails now include `Check your application status anytime: ...`; set `APPLICANT_PORTAL_BASE_URL` for absolute email links outside localhost.

---

### TA-M2. SMS notifications on status changes
**Why.** Email isn't the primary communication channel for many Filipino workers — SMS is. Today the system notifies applicants by email only (status changes, OTP, etc.). A status-change-by-SMS would land far faster and be far more reliable for the segment we're serving (entry-level health workers, OFW returnees, fresh graduates).

**Ask.** Add SMS-sending capability alongside email for applicant-facing notifications. Suggested order of priority for which events trigger SMS:

1. **Application returned to applicant** (high urgency — they need to do something)
2. **Selected for the position** (high impact — they want to know immediately)
3. **Interview scheduled** (high impact + time-sensitive)
4. **Not selected** (less urgent but emotionally important)
5. **Email verification reminder** (after N hours of unverified OTP)

**Care.**
- This is a real cost (SMS per message). Worth confirming a budget / provider preference before building.
- Capture the applicant's mobile number during intake (already required) and respect it as the SMS destination.
- Add a unified "Stop SMS" / opt-out mechanism — PH telecoms require this for transactional SMS.
- Status-change emails (TA-M1) should continue as the primary channel; SMS is secondary "tap" alert with the magic link inside.

**Out of scope for now.** Two-way SMS (replies). Promotional/marketing SMS. WhatsApp / Viber (separate decision).

**Status.** Deferred. This needs provider choice, cost/budget approval, sender/brand setup, opt-out language, and delivery logging before implementation.

---

### TA-M3. "Unverified email" lookup state
**Why.** Today `ApplicantStatusLookupView` filters by `submitted_at__isnull=False`. If an applicant started intake and didn't verify their OTP (so the application is still DRAFT), their lookup gets a generic "We could not find an application with that ID and email combination" error. They might think they applied to the wrong portal, or that their application was deleted.

**Ask.** When a lookup matches a record but it's still in DRAFT / unverified state, return a state-specific message instead of the generic "not found":

> **Your application is not finished yet.** You started an application for {position} on {date}, but you haven't verified your email. Check your inbox for the verification code, or **[resend the code]**.

With a button that re-issues the OTP for that draft.

**Out of scope.** Showing the draft's contents on the status page — they already have access via the OTP page once they verify.

**Resolution.** Codex added a safe draft path. If the status-link token belongs to an unsubmitted draft, the applicant is redirected to the OTP page. If the form lookup ever matches a draft by both Application ID and email, it also redirects to OTP. The lookup intentionally does not match drafts by email alone to avoid exposing draft existence to someone who only knows an email address.

---

## From VAL1 — Align the email error message (client now diverges)

### VAL1-email. Match the server-side email error to the client copy
**Why.** Per a UX-copy review (CMS/Healthcare.gov, GOV.UK guidance), the
intake email error should be specific with an example. I changed the
**client-side** wizard message to:

> Enter an email address with an @ symbol, like name@example.com.

The **server-side** `EmailField` still uses Django's default *"Enter a
valid email address."* — so the two layers now show different text for
the same failure. We want them identical (dual-layer consistency).

**Ask.** On `ApplicantPortalIntakeForm`'s email field, set:

```python
email = forms.EmailField(
    error_messages={"invalid": "Enter an email address with an @ symbol, like name@example.com."},
    ...
)
```

(or override in `__init__` if the field is declared elsewhere). Keep the
"required" message as-is. Small string-only change; no migration.

**Note on tone.** We deliberately **kept "Please"** on the other intake
error strings (name/phone/summary empty-field prompts) — that's a
considered choice for the Filipino-applicant audience (courtesy over
Western directness). Do **not** strip "Please" from the existing
strings. Only the email message changes, and only because it gains an
example. Leave the verbatim strings you already implemented for name,
phone, and summary untouched.

---

## From upload validation — plain-English error copy (coordinated)

### UP-1. Rewrite applicant upload error strings in lockstep (server + client + tests)
**✅ RESOLVED (both layers).** Backend: six canonical constants in
`recruitment/upload_validation.py` (line 12), six failure-state tests at
`recruitment/tests.py:5404`. Client: the four mirrored strings in
`applicant_intake_form.html` `selectedFileProblem()` are now byte-identical
to the server constants (verified programmatically); the two
signature-only messages stay backend. No further action.

**Why.** Codex shipped solid upload validation. But the error strings in
[`recruitment/upload_validation.py`](../recruitment/upload_validation.py)
are system-voice ("Empty files are not allowed.", "applicant document"),
which breaks the plain-English, actionable voice used everywhere else in
the applicant flow. Some of these strings are also **already drifting**
from the client mirror in `applicant_intake_form.html` — e.g. the
MIME/format mismatch reads *"does not match its extension"* (client) vs
*"does not match the selected document format"* (server). These are
applicant-facing.

**This must move as ONE change** across three places, or the layers
drift further:
1. `recruitment/upload_validation.py` (the `raise ValueError(...)` strings)
2. `applicant_intake_form.html` `selectedFileProblem()` JS mirror
3. The server tests that assert these exact strings.

Codex owns (1) + (3); Claude will sync (2) the moment the strings are
locked. **The strings below are the proposal — confirm or adjust, then
we both land the same text.**

**Canonical strings (proposed):**

| Trigger | Server line (current) | New copy |
|---|---|---|
| Size > 5 MB | "Each applicant document must be 5 MB or smaller." | **"This file is too large. Choose a file that is 5 MB or smaller."** |
| Empty / 0 bytes | "Empty files are not allowed." | **"This file is empty. Pick a file that has something in it."** |
| Wrong extension | "Upload a PDF, JPG, JPEG, or PNG file only." | **"This needs to be a PDF, JPG, or PNG file. Upload one of those."** |
| MIME ≠ filename (client + server line ~111) | client: "…does not match its extension…" / server: "…does not match the selected document format…" | **"This file format does not match its filename. Save it again as a PDF, JPG, or PNG, then upload it."** (Codex's wording — accurate, since the browser checks declared type vs filename, not the signature) |

**Server-only (no client mirror — Claude can't read signatures):**

| Trigger | Server line (current) | New copy |
|---|---|---|
| Signature not detected (line ~96) | "The uploaded file could not be verified as a valid PDF, JPG, JPEG, or PNG document." | **"We couldn't read this as a PDF, JPG, or PNG. Save it again, then upload it."** |
| Signature ≠ extension (line ~102) | "The uploaded file contents do not match the selected file extension. Please upload the correct PDF, JPG, JPEG, or PNG file." | **"This file's contents don't match a PDF, JPG, or PNG. Save it again, then upload it."** |

**Note on tone.** Unlike the name/phone/summary prompts (which keep
"Please"), these are *correction* messages where the actionable
instruction carries the courtesy — so no "Please" needed. Each tells the
applicant what to **do** next, not just what's wrong.

**Backend resolution.** Codex accepted the six proposed strings as canonical,
centralized them in `recruitment/upload_validation.py`, and added direct tests
for every trigger. Claude still needs to mirror the first four client-visible
states in `selectedFileProblem()`; signature detection and signature/extension
mismatch remain server-only.

---

## (Slot for future-slice asks)
*(none yet)*
