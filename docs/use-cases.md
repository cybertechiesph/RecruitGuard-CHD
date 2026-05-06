# High-Level Use Cases

## 1. Purpose
This document summarizes the high-level use cases of RecruitGuard-CHD. The use cases are intentionally written at the level of major functional areas rather than low-level action bubbles. This matches the simplified use case diagram direction approved by the panel.

---

## 2. Direct Actors
- Applicant
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority
- System Administrator

---

## 3. High-Level Use Cases
1. Applicant Intake and Submission
2. Recruitment Entry Management
3. Workflow and Routing Management
4. Evaluation Management
5. Decision and Approval Management
6. Record, Audit, and Export Management
7. System Administration

---

## 4. Actor-to-Use-Case Mapping

| Actor | High-Level Use Cases |
|---|---|
| Applicant | Applicant Intake and Submission |
| Secretariat | Recruitment Entry Management, Workflow and Routing Management, Evaluation Management, Decision and Approval Management, Record, Audit, and Export Management |
| HRM Chief | Recruitment Entry Management, Workflow and Routing Management, Evaluation Management, Decision and Approval Management, Record, Audit, and Export Management |
| HRMPSB Member | Evaluation Management |
| Appointing Authority | Decision and Approval Management |
| System Administrator | System Administration |

---

## 5. Use Case Descriptions

### 5.1 Applicant Intake and Submission
*Description:* Allows the applicant to access the shared recruitment portal, choose the intended application path, complete OTP verification, submit application details and required documents, and receive submission confirmation.

*Primary Actor:* Applicant

*Trigger:* The applicant wants to apply for a Plantilla vacancy or COS opening/pooling entry through the system.

*Basic Course of Events:*
1. Applicant opens the recruitment portal.
2. System displays available Plantilla and/or COS entry options.
3. Applicant selects the intended path.
4. Applicant enters required information and uploads required data/documents where applicable.
5. Applicant completes the submission checklist.
6. System sends OTP to the applicant’s registered email.
7. Applicant enters the OTP.
8. System validates OTP.
9. Applicant finalizes submission.
10. System generates Application ID and records submission status.

---

### 5.2 Recruitment Entry Management
*Description:* Allows the authorized internal handler to create, update, activate, suspend, or close Plantilla and COS recruitment entries using a controlled Position Reference catalog.

*Primary Actors:* Secretariat, HRM Chief

*Trigger:* An authorized internal user needs to create or manage a recruitment entry.

*Basic Course of Events:*
1. Authorized user opens recruitment entry management.
2. User selects an official position from the Position Reference catalog.
3. System displays the linked reference metadata for review, including level classification or routing basis.
4. User configures the recruitment entry details such as engagement type, intake conditions, and entry-specific notes.
5. User saves the entry.
6. System validates and stores the record using the selected Position Reference as the official source of position metadata.
7. Entry may be activated, suspended, or closed according to the user’s authority.

---

### 5.3 Workflow and Routing Management
*Description:* Allows internal users to manage recruitment cases through stage-based workflow progression with branch-aware and level-aware routing controls.

*Primary Actors:* Secretariat, HRM Chief

*Trigger:* A valid application has been submitted and must be processed into the recruitment workflow.

*Basic Course of Events:*
1. System creates a recruitment case from the valid application.
2. System assigns the appropriate branch.
3. System checks level classification or routing basis.
4. System routes Level 1 to Secretariat or Level 2 to HRM Chief.
5. Assigned handler processes the case according to current stage.
6. System checks prerequisites before stage advancement.
7. Finalized stage outputs are locked.
8. Timeline and status history are updated.

---

### 5.4 Evaluation Management
*Description:* Allows authorized internal users to conduct screening, examination handling where applicable, interview scheduling, and rating-related functions.

*Primary Actors:* Secretariat, HRM Chief, HRMPSB Member

*Trigger:* A case reaches the evaluation-related stages of the workflow.

*Basic Course of Events:*
1. Internal handler reviews completeness and qualification-related requirements.
2. System records screening outputs.
3. Where applicable, exam-related records are created or updated.
4. Interview sessions are scheduled.
5. Authorized evaluators encode interview ratings.
6. Justification is recorded where required.
7. Finalized outputs are locked.
8. Evaluation outputs become available for decision-support handling.

---

### 5.5 Decision and Approval Management
*Description:* Allows authorized internal users to prepare decision-support outputs and allows the Appointing Authority to record final decisions where applicable.

*Primary Actors:* Secretariat, HRM Chief, Appointing Authority

*Trigger:* A case has completed the required evaluation stages and is ready for decision-support processing or final decision handling.

*Basic Course of Events:*
1. Internal handler consolidates finalized evaluation outputs.
2. System prepares decision-support outputs such as ranking and CAR where applicable.
3. Internal handler prepares the submission packet.
4. Appointing Authority reviews final artifacts in read-only form where applicable.
5. Appointing Authority records final decision.
6. System preserves pre-decision artifacts as read-only and logs the decision action.

---

### 5.6 Record, Audit, and Export Management
*Description:* Allows authorized internal users to manage stored recruitment records, preserve audit trails, and generate controlled export bundles.

*Primary Actors:* Secretariat, HRM Chief

*Trigger:* An authorized internal user needs to store, retrieve, review, archive, or export recruitment records.

*Basic Course of Events:*
1. User accesses record and evidence management functions.
2. System displays stored case records and related artifacts.
3. System preserves metadata, file version history, and audit associations.
4. User retrieves or reviews records.
5. Where authorized, user initiates export generation.
6. System prepares export bundle, evidence inventory, and integrity verification output.
7. System records the action in the audit log.

---

### 5.7 System Administration
*Description:* Allows the System Administrator to manage internal user accounts and system-level administrative settings within the approved scope.

*Primary Actor:* System Administrator

*Trigger:* The System Administrator needs to perform account administration or limited system-level maintenance.

*Basic Course of Events:*
1. System Administrator accesses administration functions.
2. System displays available account and role management options.
3. Administrator creates, updates, activates, or deactivates internal user accounts.
4. System validates and saves the changes.
5. System records the administrative action in the audit log.

---

## 6. Notes
- These use cases are intentionally high-level to match the simplified use case diagram approach.
- Low-level step-by-step actions belong in the FRS, activity diagrams, and detailed workflow logic.
- The System Administrator is separate from case-content handling by default.
