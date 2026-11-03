# Admin area rebuild plan (Round F) — November 2026

A from-scratch redesign of the System Administrator's part of the internal portal. It follows
docs/sysadmin-review-2026-11.md (the walkthrough) and the office's own verdict that the
current pages look clunky. The security boundaries stay exactly as they are; what changes is
what the administrator sees and how the daily jobs are laid out.

## Status: Round F1 done (Home, Accounts, account page, invitations, reset, unlock); F2–F4 not started

## 1. Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| D1 | New accounts | **Invitation by email** is the default: the administrator creates the account without a password and the person sets their own through the existing reset-token flow. "Set a temporary password" stays as a fallback, shown behind a switch, for staff whose email is not working yet. |
| D2 | Position References | **A page inside the portal**, administrator-only, audited. Django admin is not offered to the office. |
| D3 | Failed emails | **Yes**: the Maintenance page shows the count of failed deliveries in the last seven days with a link to the entries. |
| D4 | Names | The menu says **Accounts**, **Audit Log**, **Secured Files**, **Position References**, **Maintenance**; the bottom link is **Change password**. "Audit Log" keeps the specification's term. |
| D5 | Scope | The administrator still cannot open a case, a queue, a batch, a vacancy, the Office Policy, an export, a filing copy or a file's contents. Nothing in this plan loosens that. |

## 2. The six jobs the area serves

1. Onboard and offboard staff.
2. Help someone who cannot sign in.
3. Answer "who did what, and when".
4. Confirm a file exists and is intact.
5. Keep the trail archived and exportable.
6. Maintain the position references vacancies depend on.

Every page below exists for one of these. Nothing else gets a page.

## 3. Design rules (apply to every admin page)

- One page header: title, one sentence of purpose, at most one primary button and one
  secondary button. No eyebrow labels.
- No statistic cards above lists. Counts live in the toolbar as one line
  ("24 accounts · 23 active · 1 locked").
- Lists are tables. Cards appear only on the Home tiles.
- One badge per row at most, for status. Roles are words.
- Filters are a toolbar: a search box, chips, quick ranges; anything more sits behind one
  "More filters" button.
- Destructive actions ask once, in a dialog that says the consequence in one sentence
  (`internal_includes/modal_confirm.html`).
- Copy in the portal's voice. No Django default help text anywhere in the area.
- Same tokens, theme, header and sidebar as the rest of the internal portal; works in dark and
  light and at phone width (one column, no horizontal scroll).

## 4. Pages

### 4.1 Home (`/internal/`, admin only; other roles keep their current landing)

Three bands, top to bottom.

**Needs attention.** Four tiles, each a count and a verb link. When every count is zero the band
collapses to one line: "Nothing needs attention."

| Tile | Count | Link |
|---|---|---|
| Locked accounts | `InternalLoginAttempt` rows with `locked_until` in the future, matched to internal users by `username_normalized` | Accounts, status = Locked |
| Inactive accounts | internal users with `is_active=False` | Accounts, status = Inactive |
| Failed sign-ins today | audit entries `INTERNAL_LOGIN_FAILED` + `INTERNAL_LOGIN_LOCKED` since local midnight | Audit Log, category sign-ins, today |
| Last archive | days since the newest `AuditLogArchive`; amber past 180 days, "never" if none | Maintenance |

**Do.** Three controls on one row: *Create account* (button), *Find a user* (a search box that
submits to Accounts), *Today's activity* (link to the Audit Log with today's range).

**Recent.** The last ten audit entries in the account and sign-in/security categories: time,
who, one sentence. Link "All activity".

Removed: "Recruitment at a glance", the "Entries" button, the audit-activity pills, the
"Accounts & identity" card.

### 4.2 Accounts (`/internal/users/`)

**Header.** "Accounts" — "Staff accounts for the internal portal." Primary button: *Create
account*.

**Toolbar.** Search (name, username, email); role chips (All, Secretariat, HRM Chief, HRMPSB
Member, Appointing Authority, System Administrator); status chips (Active, Inactive, Locked);
the count line. Search and chips are GET parameters so the page can be linked from Home.

**Table.** Name (username in small text under it) · Role · Office · Status (Active / Inactive
/ Locked until h:mm) · Last sign-in (`last_login`, "never" when blank) · *Open*. Sorted by name;
sortable by last sign-in. Paginated at 50.

### 4.3 Account page (`/internal/users/<pk>/`, new)

**Header.** The person's name, role, status badge. Secondary button: *Edit details*.

**Details.** Username, email, employee ID, office, role, created on, last sign-in. (Edit opens
the existing update form, restyled.)

**Access.** Buttons with one-sentence consequences:
- *Deactivate* / *Reactivate* (existing behaviour and guards: never one's own account).
- *Send password reset email*: issues the existing reset token to the account's email,
  audited as `PASSWORD_RESET_SENT_BY_ADMIN` with the target username; refused for an inactive
  account ("Reactivate the account first").
- *Unlock now*: shown only while a lock stands; clears `locked_until` and `failure_count` on the
  matching `InternalLoginAttempt` rows, audited as `INTERNAL_ACCOUNT_UNLOCKED`. Unlocking one's
  own account is allowed (it is not a privilege change).

**Recent activity.** This account's last twenty audit entries (as actor), same table as the
Audit Log, link "See all" with the user filter set.

### 4.4 Create and edit account (`/internal/users/new/`, `/internal/users/<pk>/edit/`)

One column, in this order: First name, Last name, Email, Username (prefilled from the email's
local part, editable), Role as five choice cards each with one line of what the role can do,
Office, Employee ID, Active (edit only).

**Password (create only, D1).** Default: "Send an invitation email so they can set their own
password." Switch: "Set a temporary password instead" reveals the two password fields with the
portal's own help ("At least 8 characters, not a common word, not only numbers.") and a note
that the person will be asked to change it. The account is created active in both cases; with
the invitation, `set_unusable_password()` and the reset email are sent in one step, audited as
`INTERNAL_ACCOUNT_CREATED` with `invited: true`.

Help texts replaced: username ("Letters, numbers and . _ - only."), active ("Inactive accounts
cannot sign in. Deactivate instead of deleting.").

### 4.5 Audit Log (`/internal/audit/`)

**Header.** "Audit Log" — "Everything the system recorded: who did what, and when." Two small
buttons: *Export CSV*, *Printable report* (both honour the current filters, as now).

**Toolbar.** Search; category chips (All, Sign-ins & security, Case activity, Documents &
files, Emails, Accounts & configuration, Exports & archives); quick ranges (Today, 7 days, 30
days, Custom → two date fields); switches *Hide sign-ins* (on by default; off when the sign-ins
chip is chosen) and *Sensitive only*; *More filters* reveals role, user and the session field.
The existing filter parameter names are kept.

**Table**, grouped under day headings (Today, Yesterday, then dates): Time · Who (name, role in
small text) · What (the sentence) · Case (case number as text; the administrator does not open
cases) · a chevron that expands the row to the humanised details (the existing
`audit_detail_summary`). One "Sensitive" badge where it applies; no step, branch or level
badges in the row (they are in the expanded details). Pagination at 50.

Removed from this page: the archive block (moves to Maintenance), the seven pills.

### 4.6 Secured Files (`/internal/evidence/`)

**Header.** "Secured Files" — "Every file saved to a case, with its hash. Contents stay with
the workflow team." **Toolbar:** search; step chips; scope chips; switches *Latest versions
only* (on) and *Include archived*. **Table:** Document · Case · Step · Uploaded by · Date ·
Version · Hash (first 13 characters, Copy). Pagination at 50. Behaviour unchanged (metadata
only, viewing audited).

### 4.7 Position References (`/internal/position-references/`, new, admin only)

**List.** Title · Salary grade · Level · Occupational service · Class ID · Used by (count of
vacancies) · Status (Active / Retired) · *Open*. Search by title. Primary button: *Add position
reference*.

**Form** (add and edit): Title, Salary grade, Level classification, Occupational service,
Occupational group, Class ID, OS code, Default office/division, the four qualification fields
(Education, Training, Experience, Eligibility; `qs_education`, `qs_training`, `qs_experience`,
`qs_eligibility`), Notes, Active. No delete: "Retire" sets `is_active=False` so existing
vacancies keep their reference and the vacancy form stops offering it (the form already filters
on `is_active`). Audited as `POSITION_REFERENCE_CREATED` / `POSITION_REFERENCE_UPDATED` with the
changed fields.

Access: `SystemAdministratorRequiredMixin` + MFA; every other role 403. Services in a new
`services/references.py`; views in `views/references.py`.

### 4.8 Maintenance (`/internal/maintenance/`, new)

**Audit archives.** The existing archive block (cutoff date, six-month floor, preview, confirm)
and the table of past archives with download. **Exports.** CSV and printable report for a
chosen range. **Email delivery (D3).** A tile: failed deliveries in the last 7 days
(`NotificationLog` with `delivery_status=FAILED`), link to the Audit Log filtered to Emails;
"All emails delivered" when zero.

### 4.9 Sidebar

Order: Home, Accounts, Audit Log, Secured Files, Position References, Maintenance; bottom:
Change password, Sign out. Icons as now.

## 5. Code shape

- New templates under `recruitment/templates/recruitment/admin/` (`home.html`, `accounts.html`,
  `account_detail.html`, `account_form.html`, `audit_log.html`, `secured_files.html`,
  `position_references.html`, `position_reference_form.html`, `maintenance.html`) and shared
  includes `admin_toolbar.html`, `admin_table.html`. The old five templates are deleted once
  their replacements are in.
- Views: `views/admin_home.py` (Home), `identity_views.py` keeps the account views (restyled
  templates, plus the account page, reset and unlock), `views/audit.py` keeps the log, a new
  `views/maintenance.py` takes the archive and export views, `views/references.py` is new.
- Services: `services/identity.py` gains `send_admin_password_reset(user, actor, request)`
  and `unlock_internal_account(user, actor)`; `services/references.py` is new;
  `services/__init__.py` re-exports (and `public_names.txt` updated).
- URL names kept: `dashboard`, `internal-user-list`, `internal-user-create`,
  `internal-user-update`, `internal-user-toggle-active`, `audit-log-list`,
  `audit-log-export-csv`, `audit-log-report`, `audit-log-archive`, `evidence-vault-list`. New:
  `internal-user-detail`, `internal-user-send-reset`, `internal-user-unlock`,
  `position-reference-list/create/update`, `maintenance`.
- Audit actions added to `AuditLog.Action`: `PASSWORD_RESET_SENT_BY_ADMIN`,
  `INTERNAL_ACCOUNT_UNLOCKED`, `POSITION_REFERENCE_CREATED`, `POSITION_REFERENCE_UPDATED`
  (choices-only migration; labels in `AUDIT_ACTION_LABELS`).
- No new models. No change to permissions beyond the new admin-only views.

## 6. Rounds (build order)

Each round is test-first, ends with the full suite green and a browser check at desktop and
phone width in both themes, and is one commit.

**Round F1 — Home and Accounts.** Sections 4.1 to 4.4, the sidebar, the two new services
(reset, unlock), the audit actions. Tests: Home counts and no entry-list link; Accounts search
and chips; account page; reset sends one email and logs, refused when inactive; unlock clears
the lock and logs; invitation create sets an unusable password and sends the email; temporary
password create still works; own-account guards unchanged.

**Round F2 — Audit Log.** Section 4.5. Tests: default view hides sign-ins; the chip shows
them; grouping by day; the expanded details match the former cards; CSV and report still honour
filters; archive block gone from this page.

**Round F3 — Secured Files, Maintenance, Position References.** Sections 4.6 to 4.8. Tests:
file list toolbar and pagination; archive preview and action from Maintenance (the existing
tests, retargeted); failed-email tile count; Position References CRUD, retire, 403 for other
roles, the vacancy form offering only active references, audit entries.

**Round F4 — Copy and polish.** Every string in the area read once more; Django default texts
gone; help page or README section for the administrator ("Accounts, sign-in help, the audit
log, archives"); delete the old templates and any dead context in `DashboardView`.

## 7. Out of scope

Anything that would widen the administrator's reach into cases, files or recruitment actions;
notification emails to administrators (the Home tiles cover it); Django admin.

## 8. Definition of done

All four rounds committed; suite green; the six jobs in section 2 each done end to end in the
browser as `walk_admin`; the walkthrough's P2-1, P2-2, P2-3 and P3-1 to P3-5 closed in
docs/sysadmin-review-2026-11.md section 9.

## 9. Implementation status

| Round | State | Notes |
|---|---|---|
| F1 | Done (2026-11-03) | Home (`admin/home.html`), Accounts with search/role/status chips and sort (`admin/accounts.html`), account page with Unlock now, Send password reset email / Send the invitation again, Deactivate/Reactivate (`admin/account_detail.html`), create and edit form with role cards and invitation by default (`admin/account_form.html`), sidebar order and "Change password". Services `get_internal_account_locks`, `internal_account_locked_until`, `unlock_internal_account`, `send_admin_password_email`; audit actions `PASSWORD_RESET_SENT_BY_ADMIN`, `INTERNAL_ACCOUNT_UNLOCKED` (migration 0098, choices only). New stylesheet `static/css/recruitguard-admin.css` (theme tokens only). Old `dashboard.html`, `internal_user_list.html`, `internal_user_form.html` deleted. The Home archive tile links to the Audit Log's archive block until Maintenance exists (F3). The invitation email does not point to "Forgot your password?" because that page skips accounts without a password; the administrator resends instead. Tests: `AdminAreaRoundF1Tests` (16). |
| F2 | Not started | |
| F3 | Not started | |
| F4 | Not started | |
