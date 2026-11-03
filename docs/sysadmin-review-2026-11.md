# System Administrator walkthrough — November 2026

The sixth and last role review. The System Administrator's portal was walked live on a local
server against the demo database, the role's daily tasks were tried, every door the role must
not open was probed, and each page was judged for how it looks and reads. The office's own
verdict going in was that this part "looks clunky and not professional"; section 4 says why, and
section 8 proposes the redesign. Review only; nothing in the code was changed.

## Status: Round F done (rebuilt per docs/admin-area-rebuild-plan.md, rounds F1 to F4)

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | Signed in through the real login page as `walk_admin` (a synthetic account) on `admin.localhost:8060`: Dashboard, User Management, Create and Update Internal User, Audit Log (filters, categories, session view, an opened "Audit details"), the audit CSV and printable report, Secured Files, Notifications, Settings. Desktop width, phone width, dark and light themes. | Browser pane; emails and codes from the console backend. |
| **Probed (rolled back)** | ~40 moves: the admin opening a case, a case's audit trail, the queue, Batches, a batch, Recruitment Entries (list and new), Office Policy, Schedule, Past Records, a case export, a CAR filing copy, an evidence download, upload and archive, a workflow action, a withdrawal, a vacancy status change, Django admin; creating an account with a taken username, a taken email, a weak password, the applicant role, and a valid one; deactivating itself, changing its own role, promoting a Secretariat, deactivating a Secretariat, deactivating the last active administrator; a Secretariat opening Users, Audit Log and Secured Files. | `walk_harness.py` (scratch): each request in a transaction that is rolled back. |
| **Code** | `identity_views.py` (user views, password reset), `views/audit.py`, `views/evidence.py`, `views/home.py`, `permissions.py`, the sidebar and the five admin templates. | Read. |
| **Suite** | 853 tests green at `bea41ea`. | Run before the walk. |

**Fixture left in place** (local demo database only): the synthetic `walk_admin` account has
a test password so the login page could be used; a few sign-in and "audit log viewed" entries
were added to the trail. Nothing else changed.

## 1. Verdict

**Security: tight.** Of ~40 wrong moves, none went through. The administrator cannot open a
case, a queue, a batch, a vacancy, the Office Policy, an export, a filing copy or a file's
contents; cannot act on a case; cannot create an applicant-role account; cannot deactivate
itself or drop its own administrator role. Workflow roles cannot open the admin's pages. Every
admin view of sensitive material is itself logged.

**Daily work: covered, with gaps.** Creating, editing and deactivating accounts works, with a
confirmation and clear refusals. The audit log is complete, searchable, exportable and
archivable. Secured Files does what it says. The gaps: no way to reset a colleague's password or
unlock a locked account, no sign of who is locked out, no search on the user list, and the
Position Reference records, which every vacancy needs, cannot be maintained by anyone in the
office (section 3, P2-1).

**Look and feel: this is where the office is right.** The pages are not broken, but they are
built like a generic admin template: a dashboard that is mostly about recruitment numbers the
administrator cannot act on (with a button that leads to a "forbidden" page), KPI cards stacked
above every list, a filter form so large the audit log starts 1,700 pixels down the page, and
events drawn as tall badge-heavy cards so that fifty sign-ins fill several screens. Each page
is defensible on its own; together they read as busy, repetitive and unsure of what the
administrator is there to do. Section 8 proposes a smaller, task-first admin area.

No P1.

## 2. What is solid (keep)

- **Boundaries.** The least-privilege design from the July redesign holds everywhere probed:
  cases 404, workflow pages 403, file contents 403 and logged, exports 403, Office Policy 403.
- **Account guards.** Own account cannot be deactivated or demoted ("Ask another System
  Administrator"), taken usernames and emails are refused with plain sentences, weak passwords
  are refused, the applicant role is not offered, deactivation asks "Deactivate ‘name’?" and
  says the user is signed out at once.
- **Audit log substance.** Who, when, which case, which step, a sentence in plain words
  ("Applicant resubmitted flagged documents for re-review"), a category, a sensitive flag,
  search by case or user, a session view, quick date ranges, CSV with a clean header, a
  printable report with the filters named, and an archive with a six-month floor and a
  preview before anything moves.
- **Secured Files.** The page states the rule in one sentence ("file contents stay with the
  workflow team and are not downloadable here"), shows version, uploader and hash with a Copy
  button, and filters by step, scope and archive status.
- **Light theme and phone width** both hold; nothing overflows.

## 3. Findings

### P1 — Essential

None.

### P2 — Important

**P2-1. Nobody in the office can maintain Position References.** Every vacancy needs one, the
in-app catalog was removed in July, and the stated replacement is Django admin. But the
administrator role does not open Django admin (it needs the `is_staff` flag, which only the
developer-made `system_admin` account has). A new position title or a corrected salary grade
is a developer ticket. *Decision for the office:* (a) a small, plain "Position References"
page in the admin area (list, add, edit; no delete), or (b) give the office's administrator
account the Django-admin flag and document it. Recommend (a): it keeps the office out of Django
admin and keeps the audit trail in one place.

**P2-2. Password help and lockouts are not in the administrator's hands.** The user list and
the edit form offer no "send a password reset" and no "unlock"; a locked account shows no sign
of being locked (the dashboard counts lockouts, the list does not name them). The only path is
the "Forgot your password?" link, which depends on the user's inbox working. *Fix:* on the user
page, "Send password reset email" (reusing the existing reset flow, audited) and, when the
account is locked, "Unlock now" with the lockout shown in the list.

**P2-3. The dashboard belongs to another role.** For an administrator who cannot open a case,
the first and largest card is "Recruitment at a glance" with vacancy, closing-date and case
counts by step, and an "Entries" button that lands on "forbidden". The "Accounts & identity"
card lists events from July. Design direction in section 8.

### P3 — Polish

**P3-1. The audit log leads with its most dangerous control.** "System archives" (move entries
out of the live log) is the first block on the page, above search and the log itself. Move it
to the bottom or a Maintenance section.

**P3-2. The audit event list is cards, not rows.** Each event is a card with up to six badges
(role, step, sensitive, branch, level), an actor line, a case line and a disclosure; fifty
events are several screens, and most are "User Signed In". A compact table with the disclosure
on demand, and a "hide sign-ins" switch, would show ten times as much.

**P3-3. KPI cards above every list.** Users: four cards, one of which ("Roles: Fixed set") has
no number. Audit: seven pills repeating the dashboard. One summary line each is enough.

**P3-4. The user list has no search, filter or sort,** shows email in grey under the username,
and stacks Edit and Deactivate as two buttons in every row. With more than a screen of accounts
the eye has nothing to hold on to.

**P3-5. Copy.** "Settings" in the sidebar opens the password-change page (call it "Change
password"). "Designates whether this user should be treated as active. Unselect this instead of
deleting accounts." and "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."
are Django's defaults, not the portal's voice. "Account administration for the locked internal
actor set" and "Protected oversight review of the full activity trail" are system-speak.
"Employee id" should read "Employee ID".

**P3-6. A case's audit sub-page opens while the case does not.** `/applications/<id>/audit/`
returns the trail for the administrator although `/applications/<id>/` is 404. Consistent with
the consolidated audit log, but the page has no case context to show; either hide it from the
administrator or make it the admin's "case trail" page with a proper header.

## 4. The eleven perspectives (administrator-specific)

**QA.** All admin flows pass; all wrong moves refused.

**UI/UX.** The office's instinct is right. Three habits make the area feel clunky: every page
opens with a block of statistics before its content; filters are full forms rather than a
toolbar; lists are stacks of cards with badges rather than tables. The dashboard shows what the
system can count, not what the administrator should do next.

**Business analysis / workflow.** The administrator's real tasks are few: onboard and offboard
staff, help someone who cannot sign in, answer "who did what, when" for a case, confirm a file
is intact, keep the trail archived, maintain reference data. Two of the six have no page
(P2-1, P2-2).

**Security.** Least privilege holds; every sensitive view is logged; the archive has a floor.
Nothing to add.

**Privacy.** The consolidated audit log exposes case activity descriptions to the administrator
by design (July decision); file contents stay closed. The CSV export carries case references
and descriptions, so it is personal data: the report already names the generator; the CSV
should carry the same header line.

**Accessibility.** Landmarks, labels and focus states are in place; the badge-heavy cards are
noisy for screen readers (six badges read before the sentence).

**Plain language.** Event sentences are good; page copy is not (P3-5).

**Email and notifications.** The admin's notification page is empty by design ("an account
change or a security event") but nothing currently produces such a notice; either send one for
lockouts and new accounts or say "Security events are in the Audit Log".

**Mobile.** Holds, but four KPI cards before any content is a long scroll on a phone.

**DevOps.** The archive, CSV and report work; Position References depend on Django admin
(P2-1); the `demo_seed_bot` and test accounts in the user list are demo artifacts the reseed
keeps producing, worth hiding behind an "office accounts only" default.

**Data and audit.** Every admin action lands in the trail with the actor; the "View session"
link groups a user's sign-in session. Good.

## 5. Current vs proposed

**Dashboard.** Now: pipeline counts, security counts, audit counts, account counts, a dead
"Entries" button. Proposed: an "Accounts and security" home in three parts: *Needs attention*
(locked accounts, inactive accounts, failed sign-ins today, days since the last archive),
*Do* (Create account, Find a user, Today's audit, Export the trail), *Recent* (the last ten
account and security events).

**Users.** Now: four cards, a tall table, two buttons per row. Proposed: one toolbar (search,
role, status), one compact table (name, username, role, office, status, last sign-in), one
"Open" per row to a user page that holds Edit, Deactivate, Send password reset, Unlock, and
that account's recent trail.

**Audit Log.** Now: archives, a six-field form, seven pills, then cards. Proposed: the log first
as a table (time, who, action, case, details), a toolbar above it (search, category chips,
quick ranges, "hide sign-ins", "sensitive only"), advanced filters behind one button, export
and report as two small buttons, Maintenance (archive) at the bottom.

**Secured Files.** Keep; filters become a toolbar; one line per file (document, case, uploader,
date, version, hash).

## 6. Essential vs optional

**Essential (Round F, first):** P2-1 once decided, P2-2, P2-3 with P3-1 to P3-4 as one redesign
of the four admin pages.
**Important:** P3-5.
**Optional:** P3-6, the notifications question, the demo-account filter.

## 7. What remains unverified

- Django admin itself (not opened; only reachability was checked).
- The archive action end to end (previewed in code; not executed, since it moves live entries).
- Password reset by email from the login page (covered by the suite; not walked here).
- The audit CSV in a spreadsheet (header and row count checked only).
- A screen-reader pass.

## 8. Round F brief (for the implementer)

A redesign of the admin area's four pages plus two small features. Test-first; one commit per
numbered item is fine.

1. **Admin home (P2-3).** Replace the four cards with the three-part home in section 5. Counts:
   locked accounts (`locked_until` in the future), inactive accounts, failed sign-ins today,
   days since the newest `AuditLogArchive`. Recent: last ten audit entries in the account and
   security categories. Remove the recruitment pipeline card and the "Entries" button. Tests:
   the admin home shows the four counts and no link to the entry list.
2. **Users (P3-3, P3-4, P2-2).** Toolbar with search (name, username, email), role and status
   filters; compact table with last sign-in; a user page (`users/<pk>/`) with Edit, Deactivate
   (existing confirm), **Send password reset email** (reuse the reset token flow, audited as
   `PASSWORD_RESET_SENT_BY_ADMIN`), **Unlock now** when locked (clears `locked_until`, audited),
   and the account's last twenty audit entries. Drop the four KPI cards for one summary line.
   Tests: search narrows the list; reset sends one email and logs; unlock clears the lock and
   logs; both refused for the admin's own account only where it makes sense (unlocking self is
   fine; resetting self goes through the normal link).
3. **Audit Log (P3-1, P3-2, P3-3).** Log first, as a table with a per-row details disclosure;
   toolbar with search, category chips, quick ranges, "hide sign-ins" (default on), "sensitive
   only"; advanced filters (role, user, from, to) behind one button; export and report as two
   buttons; archives in a Maintenance block at the bottom. Keep pagination at 50. Tests: default
   view hides sign-ins; the table renders the same fields the cards did; archive block is after
   the log.
4. **Secured Files.** Filters as a toolbar; one row per file; no other change.
5. **Position References (P2-1, option (a)).** Admin-only list/add/edit page for
   `PositionReference` (title, salary grade, level, service, QS fields), no delete; audited as
   `POSITION_REFERENCE_CREATED/UPDATED`; linked from the admin home. Tests: Secretariat 403;
   admin creates and edits; the vacancy form offers the new reference.
6. **Copy (P3-5).** Sidebar "Change password"; portal-voice help texts on the user form;
   page subtitles rewritten ("Create and manage staff accounts", "Everything the system recorded:
   who did what, and when").
7. **Case audit sub-page (P3-6).** Either 404 for the admin or give it the admin case-trail
   header; pick the first unless the office wants the second.

Then: the full suite; walk the four pages in the browser at desktop and phone width, dark and
light.

## 9. Round F implementation status (2026-11-03)

The area was rebuilt from scratch rather than patched; the page-by-page spec is
docs/admin-area-rebuild-plan.md.

| Finding | Closed by | What shipped |
|---|---|---|
| P2-1 Position References unmaintainable | F3 | An in-portal Position References page (list, add, edit, retire; no delete; audited). Django admin is not offered to the office. |
| P2-2 No password help or unlock | F1 | The account page has Send password reset email (or resend the invitation) and Unlock now; locked accounts show in the list and on Home. New accounts are invited by email by default. |
| P2-3 Dashboard for another role | F1 | Home: needs attention, shortcuts, recent account and security events. No recruitment numbers; no link that 403s. |
| P3-1 Archive control first on the audit page | F2, F3 | The log comes first; archives moved to the Maintenance page. |
| P3-2 Events as badge-heavy cards | F2 | A table grouped by day, one Sensitive badge at most, details on demand; routine sign-ins hidden by default. |
| P3-3 Statistic cards above every list | F1, F2 | Replaced by one count line in each toolbar. |
| P3-4 User list without search or sort | F1 | Search, role and status chips, sort by last sign-in, one Open per row. |
| P3-5 Copy | F1, F4 | "Change password" in the sidebar; Django default help texts replaced; page subtitles and account messages rewritten in plain words. |
| P3-6 Case audit sub-page | F2, F4 | Kept as the "Case trail" page with a proper header and breadcrumb, reached by clicking a case number in the Audit Log. Chosen over a 404 because "who did what on this case" is one of the administrator's six jobs. |
| Notifications page promised notices nothing sends | F4 | The administrator's empty state now points to Home and the Audit Log. |
| Unreachable administrator code on the case page | F4 | The `system_admin` branches in the case page template and view, and the orphan case sidebar include, were removed. |
