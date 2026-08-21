# Defense Revisions Plan

Capstone defense passed 2026-07-14. This document is the canonical spec for the
panel-mandated revisions, as discussed and locked with the team. Each item below
was explicitly decided; deferred items are listed at the end with their reasons.

Panel sources: Panelist 1 (Ronel) — written comments + verbal Q&A notes;
Panelist 2 (Enrico Chavez) — written comments.

Build order: **REV-C → REV-G → REV-B → REV-A → REV-I → REV-E → REV-D → REV-F.**
Terminology (REV-C) goes first because it touches the most templates and later
items build on top of it. The identifier redesign (REV-G) goes second because
downstream items build on its IDs: REV-B's blinding *is* the candidate code,
REV-I's OTP gate keys off the tracking code, and REV-E's printed documents are
born showing case numbers. Reports (REV-F) goes last because it is the largest
new surface and depends on REV-D's write-side cleanups.

---

## REV-C — Screening document status terminology

**Panel comment.** "Improve the terms used in status. … Don't invent
terminologies — use the terms actually used in the hiring process."
(Ronel, on Meets / Absent / Request Resubmission.)

**Decisions.**
- Reviewer-facing dropdown shrinks to **two** choices:
  - `Meets` → **"Complied"**
  - `Request Resubmission` → **"Return for Resubmission"**
- **"Absent" is removed from the dropdown entirely.** A reviewer can never
  declare an uploaded file absent; "wrong/blank file" cases are
  Return-for-Resubmission with remarks.
- The system-stamped state for a required row with no file
  (`forms.py` fixed-row path) is kept but renamed **"Not Submitted"** — value
  and label both (pre-production; DB churn approved). It remains
  system-applied-only, rendered as a locked read-only label.
- Exam-stage "Absent" (a *person* not showing up) is correct usage — untouched.

**Scope of the rename ripple.** Model choices + migration, forms validation
messages ("must be marked Meets…", "A missing document cannot be marked
Meets."), the "Mark remaining as Meets" bulk button, screening
locked/finalized views, resubmission emails, CSS class hooks, seeders, tests.

**Acceptance.**
- Screening dropdown shows exactly: Complied / Return for Resubmission.
- A required row with no evidence renders a locked "Not Submitted" tag.
- No user-visible surface (or internal enum value) says "Meets" or document
  "Absent" anywhere.
- Full test suite passes with updated assertions.

---

## REV-G — Identifier redesign (clean-slate ID scheme)

**Origin.** Team-initiated during revision planning (not a panel item, but it
strengthens P1's case-number and bias points). Decision: pre-production is the
only cheap window; adopt the full scheme now so every later item builds on it
once. Principle: **one identifier, one job** — and prefixes describe the
*thing*, not the software ("RG-" branding dropped; an HR office numbers
documents by type, not by vendor).

**The scheme.** Grammar `TYPE-YEAR-SEQUENCE`, hyphens only, 4-digit years,
zero-padded sequences:

| Identifier | Format | Example | Audience / job |
|---|---|---|---|
| Vacancy code | `VAC-<BRANCH>-<YYYY>-<3-digit seq>` | `VAC-PLT-2026-002` | Public + staff; branch-per-year sequence (replaces `RG-PLT-2026-0002`) |
| Case number | `CASE-<YYYY>-<4-digit seq>` | `CASE-2026-0147` | Staff, audit, printed documents; global per-year sequence; safe to display anywhere (label, not key) |
| Tracking code | `TRK-XXXX-XXXX` | `TRK-7KQ4-M9X2` | Applicant-only credential; random; never shown on internal surfaces |
| Candidate code | `C-<2-digit seq>` | `C-07` | Evaluator surfaces only (HRMPSB blinding alias); per-vacancy, assigned in randomized order |
| Email link token | existing UUID | — | Unchanged; stays invisible |

**Detail decisions.**
- Tracking-code alphabet: Crockford Base32 **minus remaining vowels (A, E)** —
  no lookalike characters (0/O, 1/I/L impossible) and no accidental words in
  codes. Two groups of four; zero embedded metadata (no dates).
- Case numbers are flat (no vacancy nesting); the page/document shows the
  vacancy alongside. Year = case creation year; number does not change at
  year boundaries.
- **Display rule:** stored IDs are self-describing (`CASE-2026-0147` in
  exports, URLs, audit data); templates print the labeled short form —
  "Case No. 2026-0147" — never "Case No. CASE-…".
- **Applicant comms carry both IDs with distinct jobs:** tracking code marked
  *keep private, for online status*; case number marked *quote this when
  calling/emailing the office*. Phone inquiries never transmit the credential.
- Sequence generation must be concurrency-safe (retry under the unique
  constraint).
- **Padding is cosmetic, overflow is graceful:** sequence 10,000 simply
  prints wider (`CASE-2026-10000`); uniqueness holds. Same for candidate
  codes past `C-99`. Do not "fix" this — it is by design.
- Replaces the old `RG-YYYYMMDD-6HEX` application reference everywhere:
  models, generation, status lookup, emails, receipt, internal templates,
  audit `case_reference`, seeders, tests, walkthrough docs.

**Acceptance.**
- New vacancy/application produce IDs in the new formats; uniqueness holds
  under concurrent submissions.
- Track Application accepts the tracking code (+ email, then OTP per REV-I).
- No internal screen or printed document shows a tracking code; no
  evaluator surface shows a case number or name once REV-B lands.
- Receipt and applicant emails show both IDs with the private/public labeling.
- Seeded demo data uses the new formats end to end.

---

## REV-A — Posting deadline extension (transparent)

**Panel comment.** Ronel: if too few applicants apply, the secretariat must be
able to extend the deadline of the existing announcement instead of creating a
new vacancy — including after the deadline has already passed.

**Current behavior (verified).** The edit form already allows changing
`closing_date`, and `is_open_for_intake` is date-driven, so extending a lapsed
deadline already reopens public intake automatically. What's missing is
transparency.

**Decisions.**
- Secretariat (entry manager) extends; no second approver.
- Extending after the deadline has passed is allowed (Ronel's exact scenario).
- Every closing-date change on a published posting is **audit-logged**:
  old date → new date, actor, timestamp.
- The public posting shows an **"Extended"** indicator with the new deadline
  when the closing date was moved later after publication.
- Edge case: a posting manually set to **Closed** does not reopen on a date
  change alone. The edit flow must handle this gracefully — either offer to
  reactivate in the same action or clearly tell the handler that status must
  be set back to Active.
- **Calendar validation (added by team):** the entry form's date pickers
  currently allow any past date with no guard. Rules:
  - Closing date on **creation**: past dates grayed out in the picker (`min`)
    **and** rejected server-side (client-side alone is cosmetic).
  - Closing date on **edit**: the *new* value must be today or later (the
    existing value may legitimately be in the past — that's the extension
    scenario).
  - Publication/opening dates: past values stay allowed (recording an
    already-started paper publication is legitimate) but get a soft
    confirmation, not a block.

**Acceptance.**
- Extending a lapsed deadline reopens the posting publicly and the applicant
  can submit again.
- Audit log contains the deadline-change entry with both dates.
- Public listing/detail shows the "Extended" marker.
- Manually-Closed posting edit surfaces the reactivation path.

---

## REV-I — OTP gate on Track Application

**Panel comment.** Chavez: add OTP verification to the application tracking
flow. (Status lookup currently shows case status with only Application ID +
email; the resubmission flow already has OTP.)

**Decisions.**
- Lookup keys off the **tracking code (REV-G)** + email.
- After entering tracking code + email, the system emails a one-time code
  (reusing the existing 3-minute OTP machinery, templates, resend/cooldown and
  lockout protections). Status renders only after verification.
- **Session model (Option A):** one successful verification unlocks the status
  view for **15–30 minutes** (pick one value; recommend 30 to match internal
  inactivity timeout), then re-verification is required on next visit.

**Acceptance.**
- Unverified visitor can never see status content, even with a valid
  ID + email pair.
- Within the verified window, refresh/navigation does not re-prompt.
- Existing OTP rate-limit/lockout behavior applies to this surface.

---

## REV-E — Print & button polish

**Panel comment.** "Improve UI button such as print." Confirmed sighting: the
applicant receipt page's faint grey "Print this page" text link
(`applicant_receipt.html`), the only print control in the system.

**Decisions.**
1. **Receipt print done right** — a real, visible "Print receipt" button and a
   print-tailored stylesheet/layout: clean official receipt (branding, both
   REV-G IDs with their private/public labels, submission date, requirements)
   with no navigation, buttons, or screen chrome in the printout.
   Internal printouts (CAR, rating sheet) show case numbers per REV-G's
   display rule; rating sheets and CAR print blinded per REV-B.
2. **Button-consistency sweep** across the portal: primary = solid,
   secondary = outline, destructive = red; no link-styled controls for real
   actions. Consistency pass only — not a redesign.
3. **New print surfaces:** CAR (comparative assessment report) and interview
   rating sheet get print buttons with deliberately designed print layouts,
   following the same pattern.

**Acceptance.**
- Receipt/CAR/rating-sheet printouts contain document content only (verified
  via print preview), presentable as official hard copies.
- No page offers printing via a text link.

---

## REV-D — Audit logging overhaul

**Panel comment.** Ronel: "every step logged per case won't scale — imagine
100 cases; don't log the unnecessary stuff; make details clear and concise."

**Verified findings.** (1) Screening autosave writes a full audit row on every
background save — exam/interview/deliberation services already skip audit for
autosave (`record_audit=False`) but screening was missed. (2) The per-case
audit view logs AUDIT_LOG_VIEWED on every page load and filter change.
(3) Every routine email is double-recorded (notifications table + audit row),
copying the recipient's email address into log metadata.

**Decisions.**
1. Screening autosaves no longer write audit entries; deliberate saves and
   finalize still do (aligning with the other stages).
2. AUDIT_LOG_VIEWED fires **once per review visit**, not per filter change or
   page load, on both the per-case and consolidated views.
3. Routine **NOTIFICATION_SENT audit rows are dropped** — the notifications
   table is the source of truth. **NOTIFICATION_FAILED stays** (anomalies
   belong in the trail).
4. **No applicant email addresses in audit metadata** — reference the
   notification record ID instead (privacy-by-design; DPA talking point).
5. Retention policy is documented in the manuscript (deferred with the other
   manuscript items, but the policy is enabled by #6).
6. **Archival function — Option B (export-then-delete):**
   - Sysadmin-only. Picks a cutoff; system shows exactly how many entries and
     what date range before confirming.
   - Entries are exported to a secured file **first**; deletion happens only
     after the export is verified saved. Never delete-first.
   - **Age guard:** entries newer than 6 months cannot be archived (prevents
     burying fresh activity).
   - The archival act itself is audit-logged: count, date range, file
     reference, actor.
   - Export lands in Secured Files as a **new "system archive" category
     that the sysadmin CAN download** (final-pass amendment): the sysadmin
     creates archives and must be able to retrieve them, and case-workflow
     roles should not hold system audit archives. The metadata-only rule for
     **case evidence** stands untouched — this category is the deliberate,
     documented exception.

**What stays, deliberately (defense talking points).** Failed logins / MFA /
OTP lockouts (intrusion breadcrumbs); sensitive-access views — evidence vault,
protected records (anti-snooping control, directly relevant to the panel's own
bias concern).

**Acceptance.**
- A full screening session with autosave produces at most the deliberate-save
  and finalize entries.
- Browsing/filtering an audit view produces exactly one VIEWED entry.
- A routine applicant email produces one notifications-table row and zero
  audit rows; a failed send produces the audit row without the raw address.
- Archive run: export file exists and opens; archived rows gone from live
  table; the archival audit entry is present; attempting to archive
  <6-month-old entries is refused.

---

## REV-F — Audit reporting (the "reports module")

**Panel comment.** P1 "reports module" + Chavez "improve your audit log";
team direction: the audit log becomes something you produce reports *from*.

**Decisions.**
1. **Filter by specific user** (actor picker), in addition to role.
2. **Date-range filter** with one-click presets: Today · Last 7 days ·
   This month.
3. **Session view.** Every new audit entry is stamped with a session tag tied
   to the sign-in that produced it. Clicking a login event shows that entire
   visit: sign-in time → actions → last activity, ending with either the
   logout event or an honest "last activity HH:MM — session expired" (no
   fabricated logout times). Old untagged entries are out of scope
   (pre-production; demo DB reseeds).
4. **Grouped action dropdown** — ~70 action types collapse into 5–6 plain
   categories (Sign-ins & security / Case activity / Documents & files /
   Emails / Account management / Exports & archives).
5. **Summary strip** above filtered results: total actions + per-category
   breakdown (what turns a log listing into a report). Included as the header
   of exports.
6. **Exports:** CSV (raw) and a print-ready report (system header, filters
   applied, generated-by, generated-at). Export actions are themselves logged.
7. **Session header shows sign-in IP / user agent** (already captured at
   login; surface it).
8. `SESSION_EXPIRE_AT_BROWSER_CLOSE=true` for the internal portal (best-effort
   hardening; 30-min inactivity timeout remains the real guarantee).

**Anti-scope.** No further filters beyond user/date/category/sensitive-only —
keyword search covers the long tail. Restraint is part of the "clear and
concise" requirement.

**Acceptance.**
- "User X, last 7 days" renders a filtered list with a correct summary strip
  and exports to both formats with matching contents.
- A login entry expands/links to the full session with device info and honest
  session ending.
- Category filter returns exactly its member action types.

---

## Deferred (with reasons)

| Item | Reason |
|---|---|
| Exam module (in-portal exams) | Panel suggestion; needs client confirmation first |
| Dashboard improvements | Deferred by team |
| DNS price question | Q&A/justification, not a system change |
| Secretariat-review dropdown ("demo"-tagged) | Deferred by team |
| Manuscript: objectives "establish" wording (SMART) | Deferred by team |
| Manuscript: Agile SDLC section + Gantt format alignment | Deferred by team |
| Registration test in demo video | Video production, on the team |

## Also decided (cross-cutting)

- **REV-B — Evaluator blinding:** the **CAR and deliberation** surfaces
  identify candidates by **candidate code (REV-G) only** — no applicant
  names, and no case numbers or application IDs either (the old date-embedded
  reference could unblind a candidate to anyone who knew when they applied;
  codes carry zero metadata). Blinding applies where evaluation happens on
  paper, away from the person.
  **Exception (final-pass amendment, team-approved):** the **live interview
  rating sheet shows candidate code + name** — the interview is face-to-face,
  so a blind sheet protects nothing while creating real misattribution risk
  (rating the wrong C-code). Correct attribution outranks symbolic blinding
  there.
  Secretariat and applicant-facing views keep names (intake/logistics need
  them). Audit keeps the full mapping code ↔ case ↔ person for appeals.
  Rationale: blind the raters where blinding is real, not where it is
  theater. Sweep every CAR/deliberation surface: page titles, tables,
  exports, printed CAR (REV-E), notification texts shown to panel members.
