# HRM Chief review (November 2026)

Scope: everything the **HRM Chief** sees and does in the internal portal, on both branches, with
the handoffs from the Secretariat (COS re-check, Level 2 CAR), to the HRMPSB (Level 2
interview), to the Appointing Authority (Level 2 decision) and back to the Chief (Level 2
completion). Same method and the same eleven perspectives as
[secretariat-final-review-2026-11.md](secretariat-final-review-2026-11.md). No code was changed.
Reviewed at `147c4b8` (Round A applied).

## 0. How this was tested

| | What | How |
|---|---|---|
| **Live** | **Plantilla Level 2** (VAC-PLT-2026-902, 4 applicants): the Chief screens (one return-for-resubmission and resubmission), closes intake, runs the batch exam, builds and publishes the rating sheet, schedules the interview, encodes one paper sheet; three panel members rate; the Chief finalizes the interview; the Secretariat prepares and finalizes the CAR; the Appointing Authority selects; the Chief tracks completion. **COS Level 2** (VAC-COS-2026-902, 3 applicants, pooling): screening, batch exam, interview with the Chief's scores, selection round, completion with the Chief. **COS Level 1 second batch** (VAC-COS-2026-901, 2 new applicants after the first pick): Secretariat exam, Chief re-check (screening, exam, interview) — see C1. Office Policy, Past Records, Entries, exports. | Browser pane as `walk_chief` (one host per role) for the pages; Django's test client against the same views and the live Postgres demo database for the posts. Synthetic staff accounts as in the Secretariat review, plus `hrm_chief` as a second Chief for races. |
| **Probed (rolled back)** | ~45 cross-role, invalid, out-of-order, duplicate and state-changing requests; two concurrency runs. | `walk_harness.py`. |
| **Code** | Role tables (`PLANTILLA_INTERVIEW_SUPPORT_ROLES_BY_LEVEL`, `PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL`, `CASE_REOPEN_ROLES`, `COMPLETION_REVIEW_ROLES`), `user_can_view_application`, the handoff and override services, the COS selection-round rules, entry status transitions. | `recruitment/services.py`, `views.py`, `models.py`. |

**Not verified:** the Chief's **reopen** of a closed case (every closed case in the demo data
belongs to a vacancy whose selection stands, and the reopen refuses those by design; the success
path is read from code only); the COS batch score console's messages for a bad draft score
(refused — nothing stored, no error page — but the message text wasn't captured); mobile layouts
and light-theme contrast (no screenshots this round).

## 1. Verdict

The Chief's own steps are **correct and enforced**: Level 1 cases are invisible to the Chief and
Level 2 cases to the Secretariat (404s, not 403s), the COS re-check reuses the Secretariat's
sitting and offers the copies, the Level 2 interview and rating sheet belong to the Chief and to
nobody else, the Office Policy page is the Chief's alone, the selection round refuses a second
pick, a foreign case and a pick on a resolved round, a cancel is refused while a selection stands,
and the rescind reopens the pool cleanly.

Two things are broken at the level of the **flow**, not the step, and both are the Chief's to
suffer:

- a **pooling COS vacancy can produce a second batch that the system cannot finish** (C1), and
- on **Plantilla Level 2 the Chief loses the vacancy at the CAR and the decision** (C2), the two
  steps the Chief is accountable for as the Level 2 owner.

Both are design gaps between two correct rules, not bugs in a rule. They belong in the next
implementation round ahead of the Secretariat's F7–F15.

## 2. What is solid (keep)

- **Level isolation.** Secretariat, HRMPSB and Appointing Authority get 404 on Level 2 cases and
  batches at screening; the Chief gets 404 on Level 1 cases and batches. System Admin never
  opens a case page (by design).
- **Re-check.** The Chief's COS Level 1 pass shows the Secretariat's finalized sitting and
  results read-only, offers "Copy document checks" and "Copy scores", and sends no second
  invitation (verified: one invitation per case after both passes).
- **Level 2 ownership of the interview.** Only the Chief can create and publish the rating sheet
  (Secretariat and panel: 403), schedule the interview (Secretariat: 404, panel: refused),
  encode a paper sheet (Secretariat: 403) and finalize the batch (Secretariat: 404). The panel's
  digest went to every member once, blind.
- **Selection round.** Pick refused for a case from another vacancy, for no case, and twice; a
  draft pick saves without moving anyone; the rescind returns every case to the pool. Two Chiefs
  posting at once on a resolved round: both refused, no error page.
- **Entry controls.** Cancel is refused while a Plantilla selection or a COS pick stands ("rescind
  first"). Close before the publication period ends is refused (WF-07). Office Policy: Chief 200,
  Secretariat and Appointing Authority 403.
- **Completion.** Level 2 completion (Plantilla and COS) is the Chief's; Level 1 the
  Secretariat's. The deadline defaults to two weeks from the selection.
- **Past Records and export** work for the Chief on Level 2 closed cases, blind to the panel.

## 3. Findings

### P1 — Essential

**C1. A pooling COS vacancy's second batch cannot be finished.** *(BA, QA)* — A pooling vacancy
has no closing date and stays Active after the first pick, so later applicants are accepted,
screened, examined and interviewed as a second batch. Then: the deliberation page says "This
vacancy hires as one batch. Round 1 is resolved" with no buttons; `open_round` is refused (403,
`vacancy_uses_cos_selection_rounds` is true only for *continuous* intake,
[services.py:4350](../recruitment/services.py)); every pick is refused (403). The batch page
still says "The COS pool has reached the deliberation — record the chosen applicant", and the
second-batch case page shows "Selected: Ivy Jose", the first batch's winner, as if it were this
case's decision ([C5]). The two candidates sit in the Chief's queue at "Selection" for ever.
Live: VAC-COS-2026-901, cases CASE-2026-0033 and -0034.
**Fix (pick one, with the office):** (a) when the pick of a single-round COS vacancy
(opening-based, pooling, fixed-period) is finalized, close intake automatically and refuse new
submissions, so no second batch forms; or (b) let a pooling vacancy open round 2 like a
continuous one. Either way the batch page must stop asking for a pick on a resolved batch. (a)
matches the template's own words ("hires as one batch").

**C2. On Plantilla Level 2 the Chief loses the vacancy at the CAR and the decision.** *(BA,
UI/UX, Records)* — After the Chief finalizes the Level 2 interview, the message says "Next:
prepare the Comparative Assessment Report from any candidate's case", but the Secretariat prepares
the CAR at both levels (`PLANTILLA_CAR_PREPARATION_ROLES_BY_LEVEL`, WF-04) and from that moment
the Chief gets 404 on every case, on the batch page and (403) on the CAR filing copy, through the
Appointing Authority's decision, until the selected case lands back at Completion. The Chief,
who owns Level 2 by the routing rule, cannot see the ranking being prepared, the board's
recommendation, or the decision, and the completion page carries no CAR summary (closed cases
do). Meanwhile the Secretariat can print the Level 2 filing copy but cannot open the Level 2
cases at any other step. Live: VAC-PLT-2026-902 end to end.
**Fix:** give the level owner **read-only** access to its cases at every stage after screening
(view, export, filing copy; no actions), show the CAR summary on the Level 2 completion page,
and fix the finalize message by level ("the Secretariat will prepare the CAR; you'll see it
here"). Decide with the office whether the Level 2 filing copy should be the Chief's to print.

### P2 — Important

**C3. A closed, decided vacancy can be re-activated.** *(BA, QA)* — Probe: the Chief posted
`status/active` on the closed Level 2 vacancy whose selection stands; it went Active with its
closing date in the past (Nov 1), reopening intake for a filled position. Cancel has the "a
selection stands" guard; Activate doesn't ([services.py:~13790](../recruitment/services.py)
`update_recruitment_entry_status`). The closed-entries list offers "Activate" on every closed
row. **Fix:** refuse Activate while a selection or pick stands (same message as cancel), and
hide the button on decided vacancies.

**C4. "Send to Secretariat" on a Level 2 case is a blind handoff.** *(BA, Security)* — The Chief
can hand a Level 2 case to the Secretariat (a `WorkflowOverride`, logged, with a reason). From
then on the Chief gets 404 on the case until the Secretariat sends it back; the override has no
expiry and no "take it back". The Secretariat sees it in the queue and batch (verified). Same
per-case-vs-batch problem as the Secretariat's F9, in the other direction. **Fix (with the office's
decision on F9):** whole-batch handoff with a reason; the delegating office keeps read access and
can recall; the case header shows "on loan to the Secretariat since …".

**C5. The case page shows another candidate's pick as this case's decision.** *(QA, UI/UX)* —
Part of C1's symptom but a separate template defect: the "COS deliberation pick" card reads the
vacancy's standing pick, not the round this case was deliberated in (SL-02's intent). A candidate
who was never in the round sees "Selected: <someone else>". **Fix:** show the pick only for cases
in the resolved round; otherwise "Not yet deliberated".

**C6. The Level 2 CAR filing copy has the wrong owner.** *(Records)* — Printable by the
Secretariat only (`user_can_generate_entry_car_filing_copy`), who cannot otherwise see the
vacancy's cases; not printable by the Chief. Folded into C2's fix.

**C7. Shared leftovers the Chief also meets.** *(UI/UX)* — "1 applicant … still need a screening
decision"; batch and queue rows dated "Nov 02, 2026"; the yellow "on hold" banners before anything
was tried; after the Secretariat's COS exam finalize the page lands on Batches while the Chief's
lands on the interview page. All already in the Secretariat's P3 list.

### P3 — Polish

- The Chief's Batches page lists three demo vacancies with completions "73 days overdue"; that's
  demo data, not a defect, but the reseed should not leave 70-day-old completions on the Chief's
  home.
- The closed-entries row shows "Edit", "Weights", "Activate", "Cancel vacancy" for a decided
  vacancy; only Export-type actions make sense there (see C3).
- The Office Policy page has no link from anywhere but the sidebar; a one-line "what these
  switches change" at the top would help a new Chief.

## 4. The eleven perspectives (Chief-specific; the Secretariat review covers the shared pages)

**QA.** Every Chief step passed on both branches; the dead end (C1) and the visibility gap (C2)
are flow-level. Invalid picks, foreign cases, double picks and a resolved-round race are all
refused with messages. Out-of-order: finalize before the sitting refused for exam and interview.

**UI/UX.** The Chief's home is the same Batches page as the Secretariat's; it works. Gaps: the
misleading "prepare the CAR" message (C2), the "record the chosen applicant" banner on a resolved
batch (C1), the wrong pick on a case page (C5), the Activate button on decided vacancies (C3).

**Business Analysis / Workflow.** Routing is right: Level 2 to the Chief at intake, back to the
Chief at completion; COS Level 1 re-check; COS Level 2 entirely the Chief's. Required controls
worth their cost: the rescind-before-cancel rule, the round rules, the Office Policy gate.
Missing transition: single-batch COS vacancies never close intake (C1). Missing visibility: C2.

**Frontend.** No new defects beyond the shared ones; the deliberation page degrades to an empty
card with no action when the round is resolved (C1) instead of saying what comes next.

**Backend.** The round rules are explicit and correct for continuous vacancies; the single-batch
assumption for pooling is enforced on the deliberation side but not on the intake side (C1).
`update_recruitment_entry_status` lacks the selection guard on Activate (C3). The lock
decorator from Round A covers the Chief's finalizes; the COS pick has its own round lock.

**Database.** No Chief-specific findings; the round/pick models protect their invariants
(one open round, one pick per round).

**Application Security.** Isolation held in every direction probed. The override (C4) is
audited and reasoned; its weakness is visibility and lack of expiry, not access.

**Integration.** The panel digest for Level 2 went once per member, blind (Round A fix
confirmed on a Chief-run interview). COS interviews send no panel digest (correct).

**Reporting and Records.** Exports and Past Records work for the Chief on Level 2. The Level 2
CAR filing copy ownership (C6) and the absent CAR summary at completion (C2) are the gaps.

**DevOps.** Nothing new; F14 stands.

**Architecture.** C1–C4 are all "two correct rules, no rule between them". The per-branch
workflow map proposed in the Secretariat review (steps per branch, level and intake mode, with
the owning role and the read-only roles per step) would have made C2 and C1 visible as a missing
row rather than a surprise.

## 5. Current vs proposed flows

**Pooling COS vacancy**
```
Current:  pick finalized → vacancy stays Active, no closing date → new applicants → screening,
          exam, interview → "Round 1 is resolved", no action, cases stuck at Selection
Proposed: pick finalized → intake closes with the pick (status Closed, portal says so)
          → a later need is a new vacancy (or, if the office prefers, "Open round 2")
```

**Plantilla Level 2 after the interview**
```
Current:  Chief finalizes interview → "Next: prepare the CAR" → Chief: 404 on everything
          → Secretariat prepares and finalizes → AA decides → Chief sees the selected case at
          Completion, no CAR on the page
Proposed: Chief finalizes interview → "The Secretariat prepares the CAR; you can follow it here"
          → Chief sees cases and batch read-only, CAR draft and final, filing copy → AA decides
          → Chief tracks completion with the CAR summary on the page
```

**Decided vacancy in the entries list**
```
Current:  Closed + selection stands → Edit, Weights, Activate, Cancel vacancy
Proposed: Closed + selection stands → Export, CAR filing copy (owner), "Rescind selection first
          to change this vacancy"
```

## 6. Essential vs optional, and the next round

**Essential (Round B, first):** C1, C2 (with C6), C3.
**Important (Round B):** C4 together with the Secretariat's F9 (batch-level handoff, per the
office's answer), C5, then the Secretariat's F7, F8, F10 (QS required for Plantilla), F11, F12,
F13, F14.
**Optional:** C7 and both P3 lists.
**Separate round:** F15, the restructuring.

**Office questions — answered 2026-11-02**
- C1: **close intake with the pick.** When the pick of a single-round COS vacancy
  (opening-based, pooling, fixed-period) is finalized, the vacancy closes and the portal stops
  taking applications. No round 2 on pooling vacancies. The two stuck cases on
  VAC-COS-2026-901 are resolved by the fix's data step (close them as not selected with a
  notice, since the position was filled before they were deliberated).
- C2/C6: **the Secretariat prints the Level 2 CAR filing copy.** So C2's fix is read-only sight
  for the Chief (cases, batch, CAR summary at completion) and the corrected message; the
  filing-copy owner doesn't change.

## 8. Round B brief (for the implementer)

Implement in this order, test-first, against `147c4b8` or later. Don't touch F15.

1. **C1** — close intake when a single-round COS pick is finalized; refuse submissions after;
   stop the "record the chosen applicant" banner on a resolved batch; data step for the two
   stuck cases above.
2. **C2** — read-only access for the HRM Chief to Level 2 cases, batch and CAR after the
   interview (view/export only, no actions); CAR summary on the Level 2 completion page; the
   interview-finalize message by level.
3. **C3** — refuse Activate while a selection or pick stands; hide the button on decided rows.
4. **C4 + F9** — whole-batch handoff between Secretariat and HRM Chief with a reason; the
   delegating office keeps read access and can recall; single-case handoff only for cases
   outside a batch; the case header shows the loan.
5. **C5** — the pick card shows a pick only for cases in the resolved round.
6. **F10** — QS (education, training, experience) required before a Plantilla entry goes
   Active; shown on the entry form.
7. **F7, F8, F11, F12, F13, F14** as written in the Secretariat review.
8. **P3 lists** from both reviews, including C7.

Commit when the suite is green; re-verify C1–C3 live on the demo database.

## 7. What remains unverified

- The Chief's reopen success path (code: `user_can_reopen_case` + the "selection stands" guard;
  no eligible closed case existed to try).
- The COS batch score console's exact message for a non-numeric or out-of-range draft score
  (refused with no error page; text not captured).
- Mobile layouts and light-theme contrast.
