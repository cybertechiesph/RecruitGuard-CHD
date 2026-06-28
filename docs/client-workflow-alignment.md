# Client Workflow Alignment

## Purpose

This document records the current operational interpretation of the RecruitGuard-CHD workflow based on the client interview transcript, the CHD personnel order, and the latest HRMS hiring workflow form shared by the Secretariat.

It does not replace the Functional Requirements Specification. The FRS remains the high-level requirements baseline. This document clarifies how the general FRS modules should be interpreted during implementation.

## Scope Guardrails

- The locked internal actor list remains unchanged: Secretariat, HRM Chief, HRMPSB Member, Appointing Authority, and System Administrator.
- End-user participation may be recorded as remarks, recommendation, or administering office context, but End-user is not introduced as a separate system role.
- Background investigation is handled within screening notes or evidence where applicable, not as a standalone module.
- Appointment and contract completion remain within recruitment scope only.
- Full onboarding, offboarding, payroll, termination, and full employee lifecycle handling remain out of scope.

## Corrected Role Interpretation

### Secretariat / HRMS

The Secretariat acts as the operational support and consolidation role. In the client workflow, the Secretariat commonly receives applications, supports document review, schedules interviews, encodes or consolidates paper-based ratings, prepares minutes, and prepares the Comparative Assessment Result / Comparative Assessment Report draft.

System interpretation:

- Creates or manages recruitment entries where authorized.
- Supports applicant intake records and requirement completeness review.
- Handles Level 1 processing according to routing rules.
- May support Level 2 only through controlled HRM Chief handoff or another explicitly implemented, audit-logged rule.
- Schedules interviews where the workflow assigns scheduling to HRMS.
- May encode paper-based HRMPSB ratings if the system preserves the actual rater separately from the encoder.
- Prepares or consolidates the CAR draft for Plantilla.
- Sends selected, non-selected, checklist, and reminder notifications where authorized.
- Tracks appointment or contract completion when routed to that role.

### HRM Chief

The HRM Chief provides higher-level HR review and Level 2 handling. For COS, the HRM Chief may record the lighter selection decision within the existing role model.

System interpretation:

- Handles Level 2 screening, examination records, and internal HR review.
- May hand off a Level 2 case to Secretariat only through controlled, audit-logged routing.
- Supports COS decision handling when the COS workflow does not require Appointing Authority selection.
- Tracks completion for selected Level 2 applicants.

### HRMPSB Member

The HRMPSB is the evaluation, deliberation, and recommendation body for Plantilla recruitment. The HRMPSB should not be treated as the clerical owner of CAR preparation when the client workflow assigns consolidation to HRMS/Secretariat.

System interpretation:

- Conducts or participates in Plantilla panel interview evaluation.
- Records ratings directly, or has paper ratings encoded by Secretariat with rater attribution preserved.
- Reviews the CAR draft prepared from consolidated screening, examination, and interview outputs.
- Deliberates on the CAR.
- Records deliberation minutes, recommendation, ranking remarks, and justification where applicable.
- Endorses or finalizes the recommendation artifact for Appointing Authority review.

### Appointing Authority

The Appointing Authority is the final selector for Plantilla recruitment. The Appointing Authority does not approve applicants one by one as isolated cases; the decision is made from the finalized CAR list for the vacancy.

System interpretation:

- Reviews the finalized CAR for a Plantilla vacancy.
- Selects one appointee from the listed CAR applicants, guided by the top five where practicable.
- Records deep-selection justification when selecting outside the top five.
- May return the finalized CAR to HRMPSB for reassessment instead of selecting when the CAR does not conform to final assessment.
- Records decision notes or remarks.
- The selected applicant proceeds to appointment completion.
- Other CAR applicants are closed as not selected.
- Appointing Authority selection is not part of the COS path under the current scope.

## Plantilla / Permanent Target Flow

1. Recruitment entry is created from the controlled Position Reference catalog.
2. Vacancy publication/opening data and the intake deadline (default 14 calendar days, editable) are recorded.
3. Applicants submit through the applicant portal with OTP verification.
4. A valid submission creates a recruitment case.
5. Level-aware routing applies:
   - Level 1 routes to Secretariat.
   - Level 2 routes to HRM Chief.
6. Document review and qualification screening are completed by the authorized HR handler.
7. Background investigation, if applicable, is recorded under screening notes or evidence.
8. Examination records are encoded and finalized where applicable.
9. Interview is scheduled through the authorized workflow role, with the target interpretation that HRMS/Secretariat may support scheduling.
10. HRMPSB ratings are recorded directly or encoded from paper forms with actual-rater attribution preserved.
11. Secretariat/HRMS consolidates screening, exam, and interview outputs into a CAR draft.
12. HRMPSB convenes to deliberate on the CAR.
13. HRMPSB deliberation minutes, recommendation, ranking remarks, and supporting justification are recorded.
14. HRMPSB finalizes or endorses the recommendation against the CAR draft.
15. Secretariat/HRMS finalizes the CAR after HRMPSB endorsement.
16. The finalized CAR is submitted/routed to the Appointing Authority.
17. Appointing Authority either selects one applicant from the finalized CAR or returns the CAR to HRMPSB for reassessment.
18. Selected applicant is routed to appointment completion:
   - Level 1 completion routes to Secretariat.
   - Level 2 completion routes to HRM Chief.
19. Non-selected CAR applicants are closed as not selected.
20. Selected and non-selected notifications are preserved in notification history.
21. Appointment completion tracking records checklist status, completion reference/date, and announcement reference where applicable.
22. Case closure and evidence export remain available according to role permissions.

## COS / Contractual Target Flow

1. COS recruitment entry is created as opening-based, continuous, or pooling-based according to the configured intake mode.
2. Applicants submit through the applicant portal with OTP verification.
3. A valid submission creates a recruitment case.
4. Level-aware routing may apply as an internal office control:
   - Level 1 routes to Secretariat.
   - Level 2 routes to HRM Chief.
5. Document review and qualification screening are completed by the authorized HR handler.
6. Examination is recorded where applicable, with End-user administration captured as exam context rather than a new role.
7. Interview is recorded where applicable, with End-user or HRMS participation captured as notes/context rather than a new role.
8. HRM Chief records COS deliberation or decision-support output.
9. HRM Chief records selected or not-selected outcome for COS.
10. Appointing Authority selection is skipped for COS.
11. Selected COS applicant is routed to contract completion.
12. Non-selected COS applicant is closed as not selected.
13. Notification history, completion tracking, case closure, audit logs, and evidence export remain within the same shared platform.

## CAR Interpretation

For Plantilla recruitment, the CAR is a vacancy-level decision-support artifact. It should not be treated as an applicant-only approval form.

Target interpretation:

- One vacancy should have one finalized CAR.
- The CAR should reflect the finalized applicant pool for that vacancy.
- The CAR should consolidate finalized screening, examination, and interview outputs.
- Secretariat/HRMS prepares or consolidates the CAR draft.
- HRMPSB deliberates on the CAR draft and records recommendation, minutes, quorum/attendance, and ranking justification.
- HRMPSB endorses the recommendation; Secretariat/HRMS finalizes the CAR after that endorsement.
- The system may compute a preliminary assessment score and preliminary rank using confirmed policy weights:
  Level 1 uses document review 20%, examination 40%, and interview/PSPT 40%;
  Level 2 uses document review 40%, examination 20%, and interview/PSPT 40%.
- Preliminary rank is advisory only. HRMPSB ranking remains the official recommendation rank and may include notes or justification when the panel's ranking differs from the system order.
- Finalized CAR artifacts are locked against ordinary editing.
- Appointing Authority selects the appointee from the finalized CAR list.
- Appointing Authority selection should be from the top five where practicable; selecting outside the top five requires deep-selection justification.
- If the Appointing Authority returns the CAR for reassessment, the returned CAR is preserved as a returned version, applicant cases route back to HRMPSB review, and HRMPSB re-finalizes deliberation before HRMS finalizes a new CAR version.

## Implementation Implications

The current FRS can remain general. Implementation should align permissions and workflow ownership with this document where the FRS uses broad wording.

Highest-priority implementation alignment items:

- Move Plantilla CAR draft preparation/consolidation from HRMPSB-only ownership toward Secretariat/HRMS-supported ownership.
- Keep HRMPSB responsible for evaluation, deliberation, recommendation, and endorsement.
- Preserve document-review, exam, interview, preliminary score, preliminary rank, and HRMPSB rank/notes in CAR rows.
- Support paper-based rating encoding by Secretariat while preserving actual HRMPSB rater attribution.
- Require justification for extreme HRMPSB ratings where policy requires it.
- Keep Appointing Authority selection CAR-based for Plantilla.
- Support Appointing Authority top-five/deep-selection documentation and CAR return for HRMPSB reassessment.
- Keep COS lighter and skip Appointing Authority selection.
