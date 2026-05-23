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

## (Slot for future-slice asks)
*(none yet)*
