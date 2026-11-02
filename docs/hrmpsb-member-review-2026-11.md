# HRMPSB Member walkthrough — November 2026

A review of the portal as an HRMPSB panel member uses it, written 2026-11-03 against `10b2d99`
(F15 complete). Same method as `docs/secretariat-final-review-2026-11.md` and
`docs/hrm-chief-review-2026-11.md`; the shared pages those reviews settled are referenced, not
repeated. Findings are graded P1 (essential), P2 (important), P3 (polish); section 8 is the
Round C brief.

## Status: Round C done (P1-1 to P2-5, P3-1 to P3-3)

Decisions taken: P2-4 option (b), applied to the panel only (office staff always see the name,
since they prepare the paper sheets ahead); P2-5 the "CAR finalized" notice only, no "vacancy
decided" notice until the Appointing Authority walkthrough. See section 9.

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | Two fresh Plantilla vacancies, brought to the HRMPSB stage through the real views: VAC-PLT-2026-903 (Level 1, 4 applicants, interview held an hour before the walk) and VAC-PLT-2026-904 (Level 2, 3 applicants, interview two days ahead). A standing panel of two (`walk_psb`, `ux_hrmpsb`) set in Office Policy; one end-user member per vacancy (`ux_hrmpsb2` on 903, `ux_hrmpsb3` on 904). As `walk_psb`: landing page, My Queue, both batch pages, the batch interview page, both case pages, the rating form (scored one candidate for real, with the confirmation dialog), the printed rating sheet, Schedule, Notifications, Past Records, a closed case; dark and light themes; phone width. Then, as the Secretariat, the batch was finalized and the CAR drafted and finalized while the member's queue, batch page and case page were re-read at each step. | Browser pane on `psb.localhost:8060` (sessions minted server-side, no passwords). Secretariat and other members' actions through Django's test client against the live Postgres demo database. |
| **Probed (rolled back)** | ~45 wrong moves: an end-user member on the other vacancy's cases and batch; rating before the interview, twice, out of scale, with a competency missing, below 75 without a justification; posting to screening, exam, interview schedule, finalize, absence, paper sheet, handoff, reject, CAR, withdrawal, evidence download, CAR filing copy, export, audit log, Office Policy; a COS case; closed cases. | `walk_harness.py` (scratch): each request inside a transaction that is rolled back. Results quoted below are the server's responses. |
| **Leak scan** | Every page the member can reach, searched for the applicants' names, emails, phone and case numbers. | Fetched in the member's session; 22 pages. |
| **Code** | `blinding.py`, `services/access.py` (panel membership, queue), `services/interview.py` (rating permissions and rules, quorum), `services/intake.py` (withdrawal), `services/evidence_vault.py`, `views/interview.py`, `views/case.py`, the case, batch and print templates. | Read. |
| **Suite** | 819 tests green at `10b2d99`. | Run before the walk. |

**Fixture left in place** (local demo database only): 903 is now at the Appointing Authority step
with a finalized CAR (C-15 ranked first and recommended), ready for the Appointing Authority
walkthrough; 904 is at the interview step with its interview still ahead. The Office Policy
standing panel is set, which also empties the demo `hrmpsb_member` account's queue (see P3-5).

**Not verified** (section 7): real email delivery to members; the paper-sheet path from the
member's side; a Level 2 vacancy past the interview; print-to-PDF layout; the 1366-wide layout
(the pane is narrower).

## 1. Verdict

The panel member's portal is the smallest and, in its core, the soundest of the four. What a
member may do is enforced where it matters: only the vacancies they sit on, only the interview
step, only their own rating, and never the applicant's papers, name or case number on any page
of an open case. The rating form is good: live computed score, the justification rule, a
confirmation dialog, revise-until-finalized, every save audited with the scores.

Two things break the promise, and both are small to fix. A member who opens a **closed** case
from Past Records sees the applicant's full name, because the closed-case timeline prints the
actor's name (P1-1). And a member can **record an applicant's withdrawal** and close the case,
an action the board has no business taking and could not take knowingly, since it does not know
who the candidate is (P1-2).

The rest is about timing and silence: a member can score two days before the interview (P2-1),
sees a Finalize form it cannot use (P2-2), keeps four cases "awaiting your action" while the
Secretariat writes the CAR (P2-3), and hears nothing once the CAR is finalized (P2-5). The one
deliberate hole in the blind, the applicant's name on the printed rating sheet, needs the
office's decision (P2-4).

Restructure is not needed. Round C is a handful of guards and messages.

## 2. What is solid (keep)

- **Panel membership is real.** The standing panel from Office Policy plus the vacancy's
  end-user members decide everything: an end-user member on 903 gets 404 on a 904 case and its
  batch, 403 on its rating endpoint; their queue lists only their vacancy. Interview notices
  went to exactly the three members of each panel (six, not twelve).
- **The blind holds on every open-case surface.** Queue, batch page, batch interview page, case
  page, notifications, the CAR card and the CAR print show candidate codes only; the leak scan
  found no name, email, phone or case number. Applicant files are refused with a plain reason
  ("HRMPSB members review candidates blind and cannot open this file"), the export and the
  audit log with 403, and each refusal is itself audited.
- **The rating form.** All competencies required, 1 to the sheet's scale, the computed score
  shown live, a justification demanded below 75 or above 98 (both in the browser and on the
  server), a confirmation dialog that repeats the score, "Your submitted rating" with Revise
  until the session is finalized, and an audit row per save carrying the scores and whether
  it was a first save or a revision.
- **Quorum.** The Secretariat cannot finalize until three members have rated each candidate; a
  paper sheet counts once encoded with the member who signed it.
- **The member cannot do the office's work.** Screening, exam, interview schedule, finalize,
  absence, paper sheet, CAR, handoff, reject, Office Policy: all refused.
- **COS is invisible to the panel**, as it should be (the Chief interviews alone).
- **The Level 2 rating sheet is the Chief's**; the Secretariat gets 403 on it.
- **Round B, checked live:** Office Policy shows the "Standing HRMPSB panel" field; the rating
  sheet page shows "Standing members" and "End-user members for this position" with Save.
- Dark and light themes and the phone layout of the case page are fine.
- The member's own activity is logged: each case view is a `protected_record_viewed` row.

## 3. Findings

### P1 — Essential

**P1-1. A closed case shows the applicant's name to a panel member.** Past Records lists
closed cases (blinded); opening one renders `internal_includes/completion_locked_body.html`,
whose Case Timeline prints `{{ entry.actor }}` for every event, so "Mia Navarro (applicant)"
appears fourteen times on CASE-2026-0036 for `walk_psb`. The open-case
history in `application_detail.html` already shows only the role label. *Fix:* in the locked
body, show the actor through `identity_for`/the role label for blind viewers (one filter call),
and add the closed-case page to the blinding test. *Test:* a member opens a closed case of a
vacancy they sat on; the response contains the candidate code and not the name.

**P1-2. A panel member can record an applicant's withdrawal and close the case.**
`user_can_record_withdrawal` grants it to whoever `user_can_process_application` says holds the
case, and at the HRMPSB stage that is the HRMPSB Member. The form is shown on the member's case
page ("Record an applicant withdrawal") and a POST closed the case as withdrawn and emailed the
applicant (probe, rolled back). The board does not talk to applicants and does not know who
they are; a withdrawal reaches the Secretariat. *Fix:* exclude `HRMPSB_MEMBER` in
`user_can_record_withdrawal` (the Secretariat and the HRM Chief, by the existing rules, keep
it), which also hides the form. *Test:* member POST → 403; the form is absent from the page.

### P2 — Important

**P2-1. Ratings can be submitted before the interview takes place.** On 904 (interview in two
days) the case page shows the full rating form and the server saved a score (probe). The
finalize step already uses `clock.event_not_held_yet`; the individual rating does not. *Fix:*
refuse `save_interview_rating` while the session is in the future, with
`not_held_yet_message`, and on the page replace the form with "The interview is on November 5
at 6:27 AM. Scoring opens then." *Test:* rating before the time → refused; after → saved.

**P2-2. The batch interview page offers the member a Finalize it cannot use.** "First
deliberation held on", "Members present" and "Finalize interview batch" render for the member;
the server answers "This case is not currently assigned to you for interview scheduling." The
schedule form is correctly hidden. *Fix:* hide the finalize block unless
`user_can_manage_interview_session` is true for the user; show the per-candidate rating counts
only (already there).

**P2-3. After the batch is finalized the member still "owns" the cases.** The queue says "7
cases in your queue", the batch page "4 cases in this vacancy's batch awaiting your action",
needed step CAR, while the case page says "The Comparative Assessment Report for this vacancy
is being prepared by the Secretariat." The member has nothing to do at that step. *Fix:* in the
queue and batch displays, when the current section is `car` and the viewer is a panel member,
label the row "Waiting for the Secretariat's CAR" and leave it out of the "awaiting your
action" count (the C2 "following" pattern already does this for the Chief). The case stays
readable (the read-only CAR card is useful to the board).

**P2-4. Decision: the printed rating sheet names the applicant.** `InterviewRatingSheetPrintView`
shows "Name: Rosa Abad (C-15)" to every viewer by design (REV-E: "correct attribution outranks
symbolic blinding in the room"). But the Print button is on the case page from the moment the
interview is scheduled, so a member can learn every name two days early, which undoes the blind
for anyone who clicks. *Options:* (a) keep as is; (b) show the name on the print only once the
interview time has come (the same guard as P2-1), so before the interview the sheet is blind and
in the room it is named; (c) blind the member's print, name only the Secretariat's. Recommend
(b): it keeps REV-E's reason and closes the early look. The office decides.

**P2-5. The panel hears nothing after the CAR.** Once the Secretariat finalizes the CAR, the
cases leave the member's queue and the case and batch pages answer 404; no in-app notice says
"the CAR for Administrative Aide VI is finalized and with the Appointing Authority", and nothing
later says the vacancy was decided. The board's last sight of its work is the read-only draft.
*Fix:* a panel notice when the CAR is finalized (candidate codes, the recommended code) and, if
the office wants it, a "vacancy decided" notice on selection without the name. *Decision:* what
the office wants the board to know, and when.

### P3 — Polish

**P3-1. Rating errors repeat per competency.** A sheet submitted with blanks or a 9 gets "This
field is required." seven times, or "Ensure this value is less than or equal to 5." seven
times. Say it once, naming the competencies: "Score all 7 competencies from 1 to 5."

**P3-2. The quorum refusal does not say which candidate.** "This interview needs ratings from
at least 3 HRMPSB members before it can be finalized (currently 0)" is the Secretariat's
message, but it should name the candidate code (the batch page already shows the counts).

**P3-3. The member's calendar shows exam sittings and application deadlines.** The landing
page's "Coming up" and the Schedule list Examination and deadline events for vacancies the
member sits on; none are the panel's. Filter the panel's calendar to interviews.

**P3-4. Standing members see every closed case.** Past Records lists 25 closed cases for
`walk_psb`, including vacancies closed before the panel was set; end-user members are correctly
limited. Consistent with "the standing panel sits on every vacancy", and harmless once P1-1 is
fixed; if the office prefers, limit the panel's Past Records to vacancies where they rated.

**P3-5. Demo seed and the standing panel.** `seed_demo_data` leaves the standing panel empty,
so every member sees every vacancy; the moment an office sets a standing panel, the demo
`hrmpsb_member` account (on no list) sees nothing. The seed should put the demo member on the
standing panel. Note for the droplet, which reseeds on deploy.

## 4. The eleven perspectives (member-specific)

**QA.** Happy path passes on both levels (rate, confirm, revise; Secretariat finalize; CAR
draft and finalize). Of ~45 wrong moves, two went through that should not (P1-2, P2-1); the
rest were refused with the right status or a plain message.

**UI/UX.** The member's case page is already minimal (the earlier review's results for
reference, the interview card, the rating form, history). The parked case-page redesign is not
needed for this role. What is wrong is timing and dead controls (P2-1, P2-2, P2-3) and the
repeated error lines (P3-1).

**Business analysis / workflow.** The panel's step is exactly the interview; membership per
vacancy works; quorum is enforced. The two authority gaps are P1-2 (withdrawal) and the
silence after the CAR (P2-5).

**Frontend.** Progressive enhancement holds: the rating posts without JavaScript (the harness
did it); the live score and the confirmation dialog are enhancements. Phone width and both
themes are fine.

**Backend.** Permissions are in services, not templates, except that two templates show forms
the services refuse (P2-2) or allow wrongly (P1-2). `save_interview_rating` lacks the held-yet
guard the finalize has (P2-1).

**Database.** One rating per member per session (the second save updates in place and is
audited as a revision); no change needed.

**Application security.** Blinding: one real leak (P1-1, closed cases), one deliberate
exception to decide (P2-4). Object access: end-user members cannot reach other vacancies by URL
(404, not 403, so the vacancy's existence is not confirmed). Files, export and audit log refused
and audited. Session, MFA and lockout are shared with the other roles (settled in the Secretariat
review).

**Integration.** Interview notices go to the vacancy's panel only. Nothing goes to the panel
after the CAR (P2-5).

**Reporting and records.** Every rating save is audited with scores and the revision flag;
member page views are audited. The CAR print is blind for the member and refused once the cases
leave the stage.

**DevOps.** Nothing new for this role. The demo seed note (P3-5) matters because the droplet
reseeds.

**Architecture.** After F15 the rules read cleanly: `access.user_is_on_vacancy_panel`,
`interview.user_can_manage_interview_rating`, `blinding.identity_for`. P1-1 is a template that
bypassed `blinding`; the fix is to route it through, and the blinding test should cover the
locked-case body.

## 5. Current vs proposed flows

**Today.** Notice "Interview scheduled for Candidate C-07" → My Queue → case page: earlier
results, interview card, rating form (open at once) → rate, confirm → "Your submitted rating",
Revise → (Secretariat finalizes) → the cases stay in the queue as "awaiting your action" at CAR
→ (Secretariat finalizes the CAR) → the cases vanish; nothing more.

**Proposed.** Notice → My Queue → case page: earlier results, interview card, "Scoring opens
November 5 at 6:27 AM" until then, then the form → rate, confirm, revise until finalized →
(Secretariat finalizes) → the rows read "Waiting for the Secretariat's CAR" and do not count as
the member's work; the read-only CAR card appears when drafted → (CAR finalized) → notice "The
CAR for Administrative Aide VI is finalized; the cases are with the Appointing Authority" →
(selection) → optional "Administrative Aide VI has been decided". Withdrawal and the office's
forms never appear on the member's page.

## 6. Essential vs optional

| Do | Items | Size |
|---|---|---|
| Must | P1-1, P1-2 | two small changes with tests |
| Should | P2-1, P2-2, P2-3 | guard, template, queue label |
| Decide | P2-4 (print name), P2-5 (notices after the CAR) | office decisions; P2-4(b) is a two-line change |
| Could | P3-1 to P3-5 | messages, calendar filter, seed |

## 7. What remains unverified

Real email delivery to members (locmem backend in the walk); the paper-sheet path as the member
experiences it (the Secretariat encodes; not re-walked); a Level 2 vacancy past the interview
(904 was left at the interview step on purpose); print-to-PDF layout of the rating sheet;
the 1366-wide layout; two members saving a rating on the same case at the same moment (each
member has their own row, so no conflict is expected).

## 8. Round C brief (for the implementer)

Test-first, one commit per item or small group, suite green, no model changes expected.

1. **P1-1** `completion_locked_body.html`: show the timeline actor through the blinding helper
   (role label for a blind viewer). Test: closed case as a member, no name.
2. **P1-2** `intake.user_can_record_withdrawal`: never for `HRMPSB_MEMBER`. Test: POST 403,
   form absent.
3. **P2-1** `interview.save_interview_rating`: refuse while `clock.event_not_held_yet(session.scheduled_for)`
   with `not_held_yet_message("interview", …)`; case page shows "Scoring opens …" instead of the
   form. Test both sides of the time.
4. **P2-2** `vacancy_batch_interview.html`: finalize block only when the viewer can manage the
   session.
5. **P2-3** queue and batch rows for a panel member at the `car` section: "Waiting for the
   Secretariat's CAR", not counted as awaiting action (`_queue_task_display`, the batch page's
   count). Test the count and the label.
6. **P2-4** if the office picks (b): the print shows the name only when the interview time has
   come. **P2-5** if wanted: a panel notice on CAR finalize (and on selection).
7. P3-1, P3-2, P3-3, P3-5 as time allows.

Do not reopen the case-page redesign for this role; the member's page is already small.

## 9. Round C implementation status (2026-11-03)

| Item | Done | Where |
|---|---|---|
| P1-1 closed-case timeline and selected appointee blinded | yes | `blinding.actor_for` + `actor_for` filter; `completion_locked_body.html` uses it and `identity_for` for the appointee |
| P1-2 panel never records a withdrawal | yes | `intake.user_can_record_withdrawal`; at the panel stage the owning office records it whatever the Office Policy switch says (the switch now governs the Appointing Authority's step; `docs/office-policy-guide.md` item 4 updated, the SL-10 switch test moved to that step) |
| P2-1 no scoring before the interview | yes | `interview.interview_scoring_is_open` guards `save_interview_rating` and `upload_interview_fallback_rating`; the case page shows "Scoring opens at the interview" instead of the form |
| P2-2 no Finalize for the panel | yes | `vacancy_batch_interview.html` shows the finalize form only with `can_finalize` (the session manager), a one-line note otherwise |
| P2-3 waiting on the CAR is not the panel's work | yes | `car.panel_waits_on_car`; "Waiting for the Secretariat's CAR" on the queue and batch rows; batch page and batch console counts exclude those cases |
| P2-4 print names the applicant to the panel only from the interview time | yes (b) | `InterviewRatingSheetPrintView` → `show_applicant_name` |
| P2-5 panel told when the CAR is finalized | yes | `case_notices.emit_car_finalized_panel_notifications` (new kind `car_finalized`, migration 0095, choices only, no SQL) |
| P3-1 rating errors said once | yes | `InterviewRatingView` |
| P3-2 batch finalize names the case | yes | `finalize_vacancy_interview_batch` prefixes the case reference |
| P3-3 panel calendar shows interviews only | yes | `schedule_services.get_schedule_events_for_user` |
| P3-4 standing members see all closed cases | not done | harmless after P1-1; consistent with the standing panel sitting on every vacancy |
| P3-5 demo seed and the standing panel | not done | demo data only; note for the droplet reseed |

Tests: `HrmpsbRoundCTests` and `HrmpsbRoundCTimingTests` in `recruitment/tests/test_reviews.py`
(13 tests). Live check on the demo database as `walk_psb`: closed case without the name, no
withdrawal form, "Scoring opens" on the 904 case two days ahead, blind print for the member and
named print for the HRM Chief, no Finalize form, interviews only on the calendar.
