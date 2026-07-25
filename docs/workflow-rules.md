# Workflow Rules

Client-specific operational interpretation is documented in
`docs/client-workflow-alignment.md`. The FRS remains the high-level baseline;
the alignment document clarifies how Secretariat, HRMPSB, Appointing Authority,
and COS responsibilities should be implemented.

## Shared Workflow Principles
- One shared platform
- One shared workflow engine
- Two branches: Plantilla and COS
- One valid application creates one recruitment case
- Stage progression is enforced
- Stage skipping is not allowed
- Finalized outputs may be stage-locked
- Controlled reopen may be allowed only through authorized action and audit logging

## Plantilla Branch
- Stricter policy-aware workflow
- Uses formal vacancy/publication handling with a default 14-calendar-day (editable) publication/intake period
- May include screening, examination, interview, deliberation, CAR, decision, and completion tracking where applicable

## COS Branch
- Lighter flexible workflow
- May use openings or pooling intake
- Not identical to Plantilla
- Steps are applied where applicable
- No additional COS-specific actor is introduced beyond current internal roles
- After HRM Chief deliberation, COS selection may be recorded by the HRM Chief and routed directly to completion tracking; Appointing Authority signing is not part of the COS path.

### Level 1 COS is two-pass — this is the approved source (SL-05, 2026-07-26)

An earlier audit prompt described Level 1 COS as "Secretariat screening/exam → HRM Chief
interview", implying the Chief starts at interview. That was an oversimplification. The
approved rule is:

- **Level 1 COS is two-pass.** The Secretariat performs the first screening and examination
  pass. Finalizing the Secretariat exam routes the case to HRM Chief Review, where the Chief
  performs an **independent validation** of screening and examination before the interview.
  The client's own account of the process is the basis: "kadalasan ako po ang initial review,
  at siya po ang final approval lalo na sa higher-level positions."
- **The passes are independent, not a re-keying exercise.** The Chief sees the Secretariat's
  finalized results read-only as the first-pass basis (the "Earlier review — Secretariat"
  card) and records their own findings. A one-click "Copy results from Secretariat review"
  accelerator prefills the Chief's still-editable form; it is a convenience, never an
  auto-accept.
- **Rule for disagreement.** Each pass is a stage-local finalized record and neither
  overwrites the other. The record finalized at the case's **current** stage is the operative
  one for that stage and for everything downstream of it; the earlier pass is preserved
  intact as the first-pass basis and remains visible and auditable. A disagreement is
  therefore recorded as two attributable findings, never silently reconciled.
- **One sitting, one schedule, one notice.** The Chief's validation never invents a second
  examination schedule and never re-emails the applicant an invitation for an exam already
  taken: a notified schedule from any prior review stage satisfies the schedule-before-record
  guard (WF-12).
- **Level 2 COS is unaffected** — it begins directly with HRM Chief screening and examination.

## Appointing Authority Selection Rules
- Plantilla Appointing Authority selection is recorded from the finalized vacancy-level CAR.
- Selection should be from the top five ranked CAR candidates where practicable.
- Selecting outside the top five requires a recorded deep-selection justification.
- If the Appointing Authority does not agree with the CAR, the CAR may be returned for HRMPSB reassessment. The returned CAR version is preserved, active cases route back to HRMPSB, and a new CAR version must be finalized before selection can continue.

## Routing Principles
- Level 1 -> Secretariat
- Level 2 -> HRM Chief
- Secretariat must not process Level 2 cases
- Secretariat may process Level 2 cases only through a specific HRM Chief handoff
- Controlled handoff/override must be explicitly implemented and audit-logged
- Handoff between Secretariat and HRM Chief is handler reassignment; the case's official workflow stage remains universal
- When a finalized output completes a handler-changing boundary, the workflow engine shall automatically route the case to the next designated handler

## Scope Boundaries
- Recruitment only
- Full onboarding is out of scope
- Offboarding, termination, payroll, and post-hiring employee lifecycle functions are out of scope
