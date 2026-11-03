# Polish round G — November 2026

Four small items left over from the role walkthroughs, bundled into one short round after the
admin area rebuild (Round F). Each is a few lines of code plus a test; none changes a rule.

## Status: brief written, not implemented

## G1. "Open Pool" wording on the COS deliberation page

`recruitment/templates/recruitment/vacancy_batch_cos_deliberation.html` (line ~42) still says
"This vacancy has continuous intake, so candidates accumulate in a pool." The intake mode has
been labelled **Open Pool** since Round B (HRM Chief review, C1).

*Change:* "This vacancy is an open pool, so candidates accumulate until the office opens a
selection round. …" (keep the rest of the paragraph). *Test:* the deliberation page for an
open-pool vacancy contains "open pool" and not "continuous intake".

## G2. The Director's calendar shows the office's events (AA review P3-2)

`recruitment/schedule_services.py` builds Schedule and the landing page's "Coming up" from
`scoped_application_ids(user)`: the user's queue plus their closed cases. For the Appointing
Authority that is every case at their step, so the Director's calendar lists exam sittings,
interviews and resubmission deadlines that belong to the HR office and the panel, never to the
Director. The Secretariat and HRM Chief, who run those events, keep the calendar as it is.

*Decision taken as developers (the office may reverse it):* the Director has no events of
their own in the system, so Schedule is **hidden for the Appointing Authority** and the "Coming
up" block on their landing page is omitted. The data stays reachable through the Secretariat.

*Change:* in `templates/internal_includes/sidebar.html` the Schedule link's condition becomes
"not system_admin and not appointing_authority"; `ScheduleCalendarView` returns 403 for the
Appointing Authority (mirror of the Office Policy rule for non-Chiefs); the landing page's
"Coming up" block is skipped for that role (`views/batches.py` line ~124 builds it for
`vacancy_batches.html`; `views/lists.py` is the calendar view). *Tests:* the Authority gets 403 on `schedule-calendar` and no
"Coming up" on `vacancy-batches`; the Secretariat still gets both.

## G3. Standing panel members see every closed case (HRMPSB review P3-4)

`recruitment/services/access.py::get_closed_cases_for_user` gives an HRMPSB member every
closed case because a standing member "sits on every vacancy". The walkthrough counted 25
closed cases for a panel account, including vacancies closed before the panel existed. The
per-case guard (`_user_has_closed_application_access`) opens the same set by URL, so the list
is honest, just wide.

*Decision taken as developers:* a panel member's Past Records are **the vacancies they rated
on** (an `InterviewRating` or an interview session they were assigned to, at the HRMPSB
stage), which is what a member needs for records requests about their own ratings. The
blinding rule is unchanged (candidate codes, no names).

*Change:* for `HRMPSB_MEMBER`, filter the closed-case queryset to applications where the member
has an interview rating (`InterviewRating.rated_by=user` through `interview_session__application`)
or sits on the vacancy's panel (`services.access.user_is_on_vacancy_panel`), and apply the
same condition in `_user_has_closed_application_access` so URL access stays in step with the
list (the existing mirror rule). *Tests:* a member who rated case A and not case B sees A in
Past Records and gets 404 on B; a member on the standing panel for vacancy V sees V's closed
cases; the Secretariat's list is unchanged.

## G4. The demo seed leaves the standing panel empty (HRMPSB review P3-5)

`recruitment/management/commands/seed_demo_data.py::_resolve_hrmpsb_panel` picks or creates
HRMPSB members for ratings but never sets `OfficePolicy.core_panel_members`. With an empty
standing panel every member is on every panel (the safe default), but the moment an office
sets a standing panel, any demo member not on it sees nothing. The droplet reseeds on every
deploy, so the demo must model the real rule.

*Change:* after resolving the panel, the seed sets `get_office_policy().core_panel_members` to
the first three resolved members (the "chair, vice chair and HR" of the office's rule) and, for
each seeded Plantilla vacancy, `panel_end_users` to one further member where one exists. The
seed's `--force` path must clear and reset both so a reseed is idempotent. *Tests:* after
`seed_demo_data` (the existing seed test, extended), the Office Policy has three standing
members and the demo Plantilla vacancy has one end user; the demo panel account used by the
walkthroughs is among the standing members.

## Order and size

G1, G4, G2, G3 — about an hour. One commit, suite green, the Chief's deliberation page, the
Director's sidebar and a panel member's Past Records checked in the browser on the local demo
database (reseed first for G4).
