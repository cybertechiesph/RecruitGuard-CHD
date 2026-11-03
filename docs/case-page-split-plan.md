# Case page split plan (Round H) — November 2026

The last piece of the F15 restructure: `ApplicationDetailView.get_context_data` in
`recruitment/views/case.py` is one 620-line method that prepares every step's forms and flags
on every visit, whichever step the page is on. This round splits it into a shared part and one
builder per step. **Nothing changes for the office**: the page renders exactly the same HTML.

## Status: H1 done (shared + screening + exam); H2–H4 to do

## 1. What the page does today

- The page shows **one step at a time**: `get_application_detail_tab(application)` picks the
  case's current workflow section (screening, exam, interview, car or deliberation, decision,
  completion). The `?tab=` parameter is only an anchor; it never changes the section.
- Above the step: the case header, the "next in this batch" notice, the pipeline, the applicant
  panel, and seven cards (prior stage summary, attendance exception, CAR filing copy, final
  selection rescind, withdrawal, COS authoritative pick, COS pick rescind). Below it: the
  notifications panel, case updates, routing history, correction history.
- `get_context_data` computes everything for every step, then the template renders one step.
- Action views re-render the page with a bound form through
  `_render_application_detail_with_overrides(request, application, **overrides)` (used by the
  case, exam and completion views). That entry point stays.

## 2. Target shape

```
recruitment/views/
  case.py              ApplicationDetailView (thin), the action views, _render_…_with_overrides
  case_shared.py       everything the header, pipeline, cards and bottom panels need,
                       plus the records every step reads (section 3)
  case_screening.py    build(ctx, request, application)  -> dict of screening keys
  case_exam.py
  case_interview.py
  case_car.py          "car" and "deliberation" sections (they share the CAR and deliberation records)
  case_decision.py
  case_completion.py
```

`ApplicationDetailView.get_context_data` becomes:

```python
context = super().get_context_data(**kwargs)
shared = case_shared.build(context["application"], self.request.user, self.request)
context.update(shared)
builder = SECTION_BUILDERS.get(context["current_detail_tab"]["key"])
if builder:
    context.update(builder(context, self.request, context["application"]))
record_protected_record_access(...)
return context
```

Each `build` takes the context so far (so it can read the shared records) and returns only the
keys it owns. Builders never write keys another builder owns; the shared module owns the rest.

## 3. Who owns which keys

**Shared** (`case_shared.py`): `recruitment_case`, `current_detail_tab`, `case_timeline`,
`routing_history`, `notification_history`, `stage_record_versions`, `audit_log_url` (always
""), `evidence_items` / `can_archive_evidence` / `evidence_vault_url` (always empty — the
administrator never reaches this page; these three can be dropped from the template too),
`submission_packet`, `case_on_loan`, `can_export`, `completion_handler_label`,
`decision_record_label` / `decision_actor_label` / `decision_completion_label`; the records every
step or card reads: `screening_records`, `current_screening_record`,
`screening_flagged_count`, `screening_not_submitted_count`, `exam_records`,
`current_exam_record`, `earlier_screening_record`, `earlier_exam_record`, `interview_sessions`,
`current_interview_session`, `current_interview_ratings`, `current_interview_fallback_evidence`,
`current_interview_fallback_ratings`, `current_user_interview_rating`, `deliberation_records`,
`current_deliberation_record`, `current_comparative_assessment_report` (+ `_items`,
`car_all_rows_scored`, `car_weights`, `ete_component_weight_display`, `car_filing_copy_available`),
`completion_record`, `completion_requirements`, `final_decision_history`,
`latest_final_decision`, `latest_final_selection`, `decision_locked_record`,
`final_selection_history`, `cos_authoritative_pick`, `cos_pick_is_this_case`,
`cos_rescinded_picks`; the corrections (`screening_correction`, `exam_correction`,
`interview_correction`); the cards' forms (`attendance_*`, `withdrawal_form`,
`applicant_contact_form`, `final_selection_rescind_*`, `cos_pick_rescind_*`,
`reopen_form`, `action_form`, `resubmission_document_reviews`, `prior_screening_record`,
`prior_exam_record`, `batch_url`, `next_batch_case`, `next_batch_case_in_screening`,
`notification_log_entries`, `checklist_*`, `reminder_notification_form`).

**Screening:** `screening_form`, `screening_locked`, `screening_required_count`,
`screening_flagged_now_count`, `screening_pending_count`, `screening_documents_complete`,
`screening_qualification_reference`, `screening_position_reference`,
`resubmission_deadline_extension_form`, `applicant_document_review_items`.

**Exam:** `current_exam_schedule`, `exam_form`, `exam_schedule_form`, `exam_locked`,
`exam_batch_managed`, `exam_batch_url`, `exam_batch_block_message`.

**Interview:** `interview_batch_url`, `interview_session_form`, `interview_session_locked`,
`interview_session_batch_managed`, `interview_rating_*` (requires_session, locked,
opens_message, batch_managed, is_simple, form, template, requires_sheet),
`interview_rating_is_support_encoding`, `interview_fallback_*`, `interview_absence_form`.

**CAR / deliberation:** `deliberation_requires_vacancy_closure`,
`deliberation_vacancy_closure_message`, `deliberation_locked`, `deliberation_requires_car_draft`,
`deliberation_form`, `car_requires_deliberation`, `car_requires_finalized_deliberation`,
`cos_deliberation_section`, `cos_deliberation_pick`, `car_form`, `car_locked`, `car_readonly`,
`car_readonly_following`, `car_in_preparation`, `car_preparer_role_label`,
`car_in_preparation_copy`, `car_readiness`, `car_ete_rows`, `car_candidate_facts`,
`car_finalize_block_message`, `car_prepare_block_message`.

**Decision:** `final_selection_report`, `final_selection_items`, `final_selection_form`,
`no_appointment_form`.

**Completion:** `completion_form`, `completion_requirement_formset`, `closure_form`,
`evidence_form`.

Two keys look cross-step but are not: `car_summary.html` on the completion tab reads the shared
CAR record; `final_selection_locked_body.html` / `decision_locked_body.html` on the completion
tab read the shared decision records. Both stay shared.

**The one behaviour change, deliberate:** keys a step owns are no longer in the context when
another step is showing. Nothing in the templates reads a step's keys outside that step (the
cards and bottom panels read shared keys only), and the capture in section 5 proves it.

## 4. Rules

- Section modules import only `common` (and `..forms`, `..models`, `..services`); `case.py`
  imports the section modules. `ViewsPackageArchitectureTests.allowed` becomes
  `{"common": set(), "case": {"common", "case_shared", "case_screening", …},
  "case_*": {"common"}}`; every other module keeps `{"common", "case"}`.
- `views/public_names.txt` gains the new module-level names that `__init__` re-exports
  (`SECTION_BUILDERS` and the six `build` functions are internal; re-export nothing new).
- Test patches that name `recruitment.views.case.<function>` keep working only if `case.py`
  still imports that name. Grep `recruitment.views.case.` in the tests before moving a function
  and either keep the import in `case.py` or retarget the patch to the new module.
- No change to `internal_urls.py`, the templates (except dropping the three always-empty
  evidence keys), or any service.

## 5. Safety net: a before/after capture

Before touching the view, write `scratchpad/case_capture.py` and run it on the local demo
database inside a rolled-back transaction: for every application (all ~60) × every staff
role (`walk_sec`, `walk_chief`, `walk_psb`, `walk_aa`), GET the case page through the test
client and store `(status, sorted(context keys), sha256 of the HTML with CSRF tokens and
session-dependent nonces stripped)`. Run it again after each round; the diff must be empty
except for the three dropped evidence keys. This is the same method F15 round 3 used for the
workflow map (`r3_capture.py`), and it catches exactly the class of mistake a split invites:
a key computed in the wrong place for one role at one stage.

## 6. Rounds

**H1 — shared + screening + exam.** Create `case_shared.py` by moving the shared block
(records, cards, bottom panels) out of `get_context_data`; create `case_screening.py` and
`case_exam.py`; wire `SECTION_BUILDERS`; amend the architecture test; capture matches. The
other four sections stay inline in `case.py` for this round.

**H2 — interview + CAR/deliberation.** Move the two largest sections. Capture matches.

**H3 — decision + completion, delete the inline remainder.** `get_context_data` is now the
nine-line version in section 2. Drop the three always-empty evidence keys from the template.
Capture matches except those three keys.

**H4 — polish.** Docstrings on each builder saying which step and which includes it serves;
`docs/f15-restructure-plan.md` marked complete; this plan's status table.

Each round: suite green, capture diff empty, one commit. Browser check once at the end: one
case per step as the Secretariat and as the panel member.

## 7. Size

About half a day of Opus work. The risk is low because the capture compares the rendered page
itself, not just the tests.
