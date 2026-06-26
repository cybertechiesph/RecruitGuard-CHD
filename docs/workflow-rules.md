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
- Uses formal vacancy/publication handling with a 14-calendar-day publication/intake period
- May include screening, examination, interview, deliberation, CAR, decision, and completion tracking where applicable

## COS Branch
- Lighter flexible workflow
- May use openings or pooling intake
- Not identical to Plantilla
- Steps are applied where applicable
- No additional COS-specific actor is introduced beyond current internal roles
- After HRM Chief deliberation, COS selection may be recorded by the HRM Chief and routed directly to completion tracking; Appointing Authority signing is not part of the COS path.

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
