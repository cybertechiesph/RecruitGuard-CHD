# N2 — Pin the competency-template version per interview session

**Status:** open, last unimplemented item from the 2026-07-21 system-logic audit.
**Purpose of this brief:** decide whether to build N2 as written, build a narrower fix, or
close it as already-satisfied. Written for a reader with no prior context on this codebase.

---

## 1. What N2 says

From `docs/system-logic-audit-findings.md`, §1A, "New items from the reference artifacts":

> **N2 — Pin the competency-template version per interview session.** Rating templates are
> Secretariat-editable; a session must permanently reference the version its raters actually
> used, so historical sheets never silently change.

The implied build is: version the rating template, stamp the version onto each
`InterviewSession`, and render historical sheets through the stamped version rather than the
live one.

---

## 2. What the code actually does today

I read every write path. The relevant facts, with file references:

**The sheet is per-vacancy, not a shared library template.**
`CompetencyRatingTemplate.recruitment_entry` is a `OneToOneField(PositionPosting)`
(`recruitment/models.py`). There is exactly one sheet per vacancy and it is never reused
across vacancies. `create_competency_rating_template` refuses to create a second one
(`services.py:6417`).

**It has a three-state lifecycle**: `DRAFT → PUBLISHED → LOCKED`.
Raters cannot see a draft: `get_published_competency_rating_template` filters to
`PUBLISHED`/`LOCKED` only (`services.py:6029`).

**It locks on the first digital score.**
`save_interview_rating` calls `lock_competency_rating_template(template)` immediately after
persisting the rater's `CompetencyScore` rows (`services.py:6187`). That is the *only* call
site in the codebase — I grepped all of `recruitment/` excluding `.venv` and worktrees.

**Locked means locked, enforced server-side, with no unlock.**
`save_competency_rating_sheet` is the only mutator of the sheet or its competencies, and its
first statement is:

```python
if template.is_locked:
    raise ValueError("This rating sheet is locked because scoring has started.")
```

(`services.py:6443-6447`). The view mirrors it (`views.py:2721`). There is **no** unlock
function — deliberately unlike `VacancyAssessmentWeights`, which got an audited
`unlock_vacancy_assessment_weights` in SL-00.

Existing tests already assert the lock fires (`tests.py:13380`, `tests.py:13449`).

### Consequence

**For any interview session scored online, N2's guarantee already holds.** The moment the
first rating is submitted, the sheet is frozen permanently. A version pointer would point at
a row that cannot change. On this path N2 is redundant.

---

## 3. The gap that is real

`upload_interview_fallback_rating` — the paper-sheet encoding path added in SL-03/SL-07 —
**never locks the template.** It does not reference `CompetencyRatingTemplate` at all
(`services.py`, function body read in full). Neither does finalizing the interview session.

That matters because of what the paper path is:

1. The paper form the panel physically signs **is a printout of this template**.
   `InterviewRatingSheetPrintView` renders `rating_template.competencies` grouped
   CORE / ORGANIZATIONAL / TECHNICAL, mirroring the office form DOH-RO4A-MPERIF
   (`views.py:2753-2782`).
2. The Secretariat encodes each signed sheet as an `InterviewFallbackRating` — one rater,
   one number, plus the scan as evidence.
3. Those fallback ratings **count**: `_average_interview_rating` averages digital and
   fallback ratings together (`services.py`), and that average is the interview component —
   40% of the CAR under the default split.

So the exposure is:

> A vacancy where **every** rating was encoded from paper leaves its rating sheet unlocked
> and editable **forever** — including after the ratings are encoded, the interview is
> finalized, and the CAR is finalized.

Someone can then edit or delete competencies, change weights, or change the scale. The sheet
of record silently stops matching the paper the panel signed. Reprinting the form produces a
different document than the one in the file, and nothing anywhere records that it changed.

A mixed vacancy (some raters online, some on paper) is safe — the first digital rating locks
the sheet.

### Is any stored score destroyed?

`CompetencyScore.competency` is `on_delete=CASCADE` to `CompetencyDefinition`, so deleting a
competency deletes the per-competency scores hanging off it. But in the pure-paper case no
`CompetencyScore` rows exist — a fallback rating is a single number, not a per-competency
breakdown. **So no score data is lost.** The damage is narrower and purely evidentiary: the
sheet on file diverges from the signed paper, with no trace.

Worth noting the cascade anyway, because it means the lock is the *only* thing standing
between a template edit and destroyed historical scores on the digital path.

### A second, smaller window

Between `PUBLISHED` and the first score, the sheet can still be reshaped while raters may
already be working from a printout of the published version. No stored score is affected —
none exist yet — so this is a paper-workflow mismatch, not data loss. It exists on both the
digital and paper paths.

---

## 4. The decision

### Option A — Build N2 as written (version the template, stamp it on the session)

*For:* Matches the audit text. Would also close the publish-to-first-score window, because a
session could pin the version it was published against.

*Against:* Substantial for what it buys. `CompetencyRatingTemplate` is `OneToOne` with the
posting, so versioning means either breaking that to a FK with a partial-unique constraint
(the pattern used for `FinalSelection` in SL-14) or cloning definitions per version. Cloning
interacts with the `CompetencyScore → CompetencyDefinition` cascade and with every reader:
the print view, the rating form, `compute_competency_rating_score`, `_coerce_competency_scores`,
the CAR consolidation. It is the same blast radius that made SL-12 expensive. And on the
digital path — the one the system is designed around — it protects something the lock
already protects.

### Option B — Lock the sheet on the paper path too (narrow fix)

Call `lock_competency_rating_template` from `upload_interview_fallback_rating`, exactly as
`save_interview_rating` already does.

*For:* Closes the entire real gap. One line plus a test. Zero blast radius — it uses a
mechanism already in place, already tested, already enforced server-side. Makes the paper and
digital paths behave identically, which is the invariant SL-07 was built to establish in the
first place.

*Against:* Does not close the publish-to-first-score window. Does not give a session a
version pointer, so if the office later wants genuinely reusable templates across vacancies,
N2 comes back.

### Option C — Close N2 as not applicable

*For:* The audit's premise — "rating templates are Secretariat-editable" — describes a
mutable shared template this system does not have.

*Against:* Wrong, as long as the paper path leaves the sheet unlocked. The premise is stale
but the risk is not entirely gone.

---

## 5. Recommendation

**Option B**, plus a note in the findings doc recording why the full versioning model is
unnecessary here.

Reasoning: N2 was written from the reference artifacts, before the audit had established that
the template is per-vacancy and lock-on-first-score. Given that shape, the defect is not
"sessions do not pin a version" — it is "one of the two rating paths forgot to lock." The
narrow fix restores the invariant the codebase already chose, at a fraction of the cost, and
it fails safe: if the office later adopts reusable templates, versioning can be built then
against a real requirement rather than an assumed one.

The publish-to-first-score window I would leave open and document. Closing it means blocking
edits to a published-but-unscored sheet, which would stop the Secretariat correcting a typo
they spotted after publishing — a worse everyday outcome than the risk it removes, and the
system already treats "published" as visible-but-not-yet-authoritative.

---

## 6. What I need from you

1. **Do you agree the lock already satisfies N2's guarantee on the digital path?** If you
   read the code differently, say where — the load-bearing claim is that `services.py:6187`
   is the only call site and `services.py:6447` the only mutator.
2. **Option A, B, or C.**
3. **If B:** should the fallback path lock on the *first* encoded sheet (matching the digital
   path exactly), or only once the interview session is finalized? First-sheet is more
   consistent; finalize-time leaves room to fix a sheet mid-encoding if the Secretariat
   notices an error after encoding one rater but before the rest. I lean first-sheet, on the
   grounds that a signed paper form is already an unchangeable fact the moment it is signed.
4. **Is the publish-to-first-score window acceptable to leave open and documented?**

---

## 7. Decision (Fable 5, 2026-07-26) — BINDING

Claims re-verified independently before deciding: single lock call site
(`services.py:6187`; the `:11743` hit is a docstring), locked-guard is the mutator's first
statement, no unlock exists, `upload_interview_fallback_rating` contains zero template/lock
references, and `_average_interview_rating` sums digital + fallback rows. All confirmed.

**1. Agreed** — on the digital path the lock already delivers N2's guarantee. N2 was written
from the office artifacts before the audit established the template is per-vacancy and
lock-on-first-score; its premise (a mutable shared template needing version pinning)
describes a shape this system does not have. The correct restatement of the defect is the
brief's: *one of the two rating paths forgot to lock.*

**2. Option B.** Lock the sheet from `upload_interview_fallback_rating`, exactly as
`save_interview_rating` does. N2 is thereby RESOLVED-NARROWED, not closed-as-invalid: if the
office later adopts reusable cross-vacancy templates, versioning returns as a real
requirement and gets built against that reality.

**3. First-sheet, not finalize-time.** Three reasons:
- The paper form fixes the sheet's content at **signing** — before encoding even begins. By
  first-encode, every fact the template must match is already immutable ink.
- The "fix a sheet mid-encoding" scenario protects the wrong thing. If the printed form was
  wrong, the panel rated on the wrong form; editing the stored template afterward does not
  repair that — it manufactures exactly the paper-vs-record drift this fix eliminates. The
  remedy for a wrong form is a corrected re-run of the session, never a post-hoc template
  edit. (A mistaken *encoding* is already handled by fallback version supersession, which
  does not need the template unlocked.)
- One invariant beats two: "the first recorded rating of either kind locks the sheet" is
  simpler to state, test, and defend than a path-dependent rule.

**4. Yes — leave the publish-to-first-score window open and document it** in the findings
doc, for the brief's reason: blocking edits to a published-but-unscored sheet trades an
everyday correction path for a theoretical risk, and "published" is already
visible-not-yet-authoritative. **Optional polish, implementer's call on cost:** the print
view already stamps `printed_by`/`printed_at`; adding the template's `updated_at` to the
printed footer would make a stale printout *detectable* against the stored sheet, which is
the only residual exposure in that window. If it is more than a few lines, skip it.

Implementation: the one-line lock + a test proving a pure-paper session leaves the sheet
locked (and that `save_competency_rating_sheet` then refuses), plus the findings-doc §1A
note. No model changes, no migration.

---

## Appendix — file references

| Claim | Location |
|---|---|
| Sheet is one-per-vacancy | `recruitment/models.py`, `CompetencyRatingTemplate.recruitment_entry` |
| Draft sheets hidden from raters | `services.py:6029` `get_published_competency_rating_template` |
| Lock helper | `services.py:6042` `lock_competency_rating_template` |
| Only lock call site | `services.py:6187`, inside `save_interview_rating` |
| Only sheet mutator, refuses when locked | `services.py:6443-6447` `save_competency_rating_sheet` |
| View mirrors the lock | `views.py:2721` |
| No unlock exists (contrast SL-00 weights) | `services.py:11754` `unlock_vacancy_assessment_weights` |
| Paper path, no lock | `services.py` `upload_interview_fallback_rating` |
| Paper form is a printout of the template | `views.py:2753-2782` `InterviewRatingSheetPrintView` |
| Fallback ratings feed the interview average | `services.py` `_average_interview_rating` |
| Cascade on competency delete | `recruitment/models.py`, `CompetencyScore.competency` |
| Existing lock tests | `recruitment/tests.py:13380`, `:13449` |
