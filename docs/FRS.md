# RecruitGuard-CHD Functional Requirements Specification

## 1. System Overview

RecruitGuard-CHD is a secure-by-design web-based recruitment management system for DOH–CHD CALABARZON. It supports both *Plantilla* and *Contract of Service (COS)* recruitment within one shared platform, one shared database, and one shared workflow engine. The system applies *branch-aware workflow handling* so that Plantilla and COS are not forced into one identical linear process. It also applies *level-aware internal routing* so that recruitment cases are assigned according to the recorded position level or routing basis.

### Direct Actors
- Applicant
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority
- System Administrator

### Level-Aware Routing Rule
- Level 1 -> Secretariat
- Level 2 -> HRM Chief
- Secretariat must not process Level 2 cases
- Controlled routing override is allowed only if explicitly implemented and audit-logged

### COS Rule
COS remains a *lighter flexible path* than Plantilla. It does not introduce a new actor beyond the locked actor set and will be handled by the existing internal roles, primarily the Secretariat and HRM Chief.

### Scope Boundary
The system is bounded to *recruitment handling only*. Full onboarding, offboarding, payroll, termination, and general employee lifecycle management are out of scope.

---

## 2. Technology Basis

The system will be developed using the following locked technology stack:

- Python 3
- Django
- Django Templates
- Bootstrap
- PostgreSQL
- Gmail SMTP via Django email backend
- Python hashlib for SHA-256
- Python cryptography for AES-256-GCM
- ReportLab
- Python zipfile

Future deployment target:
- DigitalOcean Droplet
- Ubuntu 22.04 LTS
- Nginx
- Gunicorn

Security testing tools:
- OWASP ZAP
- Burp Suite Community
- Bandit
- pip-audit

---

## 3. Global Functional Principles

### 3.1 Shared Platform Principle
The system operates as one shared recruitment platform supporting both Plantilla and COS workflows.

### 3.2 Branch-Aware Workflow Principle
The system applies different internal workflow rules for Plantilla and COS while preserving one workflow engine.

### 3.3 Level-Aware Routing Principle
The system uses level classification or routing basis as an internal workflow control for case handling.

### 3.4 Role-Based Access Principle
The system enforces role-based access so that each internal actor sees and performs only the functions relevant to that role.

### 3.5 Stage-Based Workflow Principle
The system enforces stage progression and prevents stage skipping unless explicitly allowed by controlled system rules.

### 3.6 Evidence Integrity Principle
The system preserves uploaded and generated records as controlled recruitment artifacts with metadata, version preservation, and integrity support.

### 3.7 Accountability Principle
The system records security-relevant and workflow-relevant actions in an audit trail.

---

# 4. Finalized Module-Based Specification

---

# Module 1. Identity and Access Control

## Purpose
Controls authenticated access to the system and enforces role-based permissions for internal users.

## Primary Actors
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority
- System Administrator

## Technology Basis
- Django authentication system
- Django session handling
- PostgreSQL user records
- Bootstrap-based login and access pages

## Detailed Functional Requirements

### a. Internal User Authentication
i. System shall allow authorized internal users to log in using registered credentials.  
ii. System shall deny access to protected system pages when authentication is unsuccessful.  
iii. System shall support authenticated access for the following internal roles: Secretariat, HRM Chief, HRMPSB Member, Appointing Authority, and System Administrator.  
iv. System shall terminate or invalidate sessions according to configured session control rules.  

### b. Password and Session Security
i. Passwords shall be stored using secure hashing and shall not be stored in plain text.  
ii. System shall support password change for authenticated users.  
iii. System shall enforce protected session handling to reduce unauthorized reuse of authenticated access.  
iv. System shall force logout or session refresh where account privilege changes require renewed access control enforcement.  

### c. Role-Based Access Control
i. System shall enforce role-based access so that users can access only the functions, records, and pages allowed for their role.  
ii. Least privilege principle shall be applied across all internal system functions.  
iii. Role assignment and modification shall be restricted to authorized administrative functions.  
iv. System Administrator shall not have default access to recruitment case content unless explicitly permitted by system rules.  

### d. Account Administration
i. Authorized administrative functions shall support creation of internal user accounts.  
ii. Authorized administrative functions shall support activation, deactivation, and update of internal user accounts.  
iii. System shall preserve deactivated accounts for traceability and audit purposes.  
iv. Account and role-related changes shall be recorded in the audit log.  

---

# Module 2. Recruitment Entry and Vacancy Management

## Purpose
Manages the controlled Position Reference catalog and recruitment entries for both Plantilla and COS.

## Primary Actors
- Secretariat
- HRM Chief
- System Administrator

## Technology Basis
- Django models and forms
- PostgreSQL entry records
- Django Templates + Bootstrap for entry management pages

## Detailed Functional Requirements

### a. Controlled Position Reference Selection and Recruitment Entry Creation
i. System shall maintain a controlled Position Reference catalog containing official position metadata used during recruitment entry creation.  
ii. System shall allow creation of Plantilla recruitment entries by selecting an existing Position Reference.  
iii. System shall allow creation of COS recruitment entries by selecting an existing Position Reference.  
iv. COS entries shall support opening-based and pooling-based intake handling.  
v. Each recruitment entry shall link to a selected Position Reference rather than allowing routine free-form creation of an official position.  
vi. Position title, level classification, and other linked official metadata used for routing shall come from the selected Position Reference.  

### b. Publication, Opening, and Intake Status
i. System shall record publication or opening data for each recruitment entry.  
ii. Plantilla entries shall support fixed validity periods for intake.  
iii. COS entries shall support continuous, active, or pooling-based intake while the entry remains active.  
iv. System shall allow authorized users to activate, suspend, update, and close recruitment entries.  

### c. Entry Metadata and Qualification Reference
i. System shall preserve recruitment metadata necessary for downstream workflow handling.  
ii. Linked Position Reference metadata shall remain visible for review during recruitment entry setup.  
iii. Plantilla entries shall support qualification-related fields where applicable.  
iv. System shall record the creator and last updater of each entry together with timestamps.  
v. Entry status changes shall be recorded in the audit log.  

---

# Module 3. Applicant Intake and OTP Verification

## Purpose
Supports the applicant-facing portal and accountless submission process.

## Primary Actor
- Applicant

## Technology Basis
- Django forms
- Gmail SMTP via Django email backend
- OTP workflow logic
- Bootstrap-based applicant portal UI

## Detailed Functional Requirements

### a. Shared Applicant Portal
i. System shall provide a shared applicant-facing portal.  
ii. Applicant shall be able to choose between Plantilla and COS application paths.  
iii. System shall load the corresponding intake flow after the applicant selects the intended recruitment path.  

### b. Accountless Application Submission
i. System shall support accountless submission of applications.  
ii. Applicant shall be able to encode required submission information based on the selected recruitment path.  
iii. Applicant supporting documents shall be uploaded and validated using explicit document requirement codes for the active recruitment path.
iv. System shall require completion of a submission checklist before final submission.
v. Submission data shall be bound to the selected recruitment entry.

### c. OTP Verification
i. System shall send an OTP to the applicant’s registered email prior to final submission.  
ii. Final application submission shall require valid OTP verification.  
iii. OTP verification data shall be stored in hashed form and shall expire after a defined duration.  
iv. Unverified or invalid OTP submissions shall not be finalized.  

### d. Submission Finalization
i. System shall generate a unique Application ID for each final submission.  
ii. System shall record submission status and timestamp upon successful intake.  
iii. System shall preserve the finalized submission as the basis for recruitment case creation.  

---

# Module 4. Recruitment Case Management and Workflow Engine

## Purpose
Transforms valid submissions into internal recruitment cases and enforces workflow progression.

## Primary Actors
- Secretariat
- HRM Chief
- System Administrator

## Technology Basis
- Django workflow logic
- PostgreSQL case records
- Stage state tracking in database
- Timeline and case history views

## Detailed Functional Requirements

### a. Recruitment Case Creation
i. System shall create one recruitment case for each valid submitted application.  
ii. Recruitment case shall be linked to the applicant, application, and recruitment entry.  
iii. System shall store current stage, branch type, and case status for each recruitment case.  

### b. Stage-Based Workflow Progression
i. System shall enforce defined workflow stages for each recruitment case.  
ii. System shall prevent stage skipping.  
iii. System shall require defined prerequisites before stage advancement.  
iv. Workflow rules shall differ according to Plantilla or COS branch where applicable.  

### c. Stage Locking and Controlled Reopen
i. System shall stage-lock finalized outputs where applicable.  
ii. Stage-locked records shall not be editable through ordinary user actions.  
iii. Controlled reopening shall be allowed only through authorized action.  
iv. Reopen action, actor, timestamp, and reason shall be recorded in the audit log.  

### d. Case Timeline and Status History
i. System shall provide a timeline or history view for each recruitment case.  
ii. Timeline shall display stage changes, case actions, and status transitions.  
iii. Case history shall remain available for later review and traceability.  

---

# Module 5. Branch-Aware and Level-Aware Routing

## Purpose
Applies routing rules based on recruitment branch and position level.

## Primary Actors
- Secretariat
- HRM Chief
- System Administrator

## Technology Basis
- Django server-side authorization and routing logic
- PostgreSQL routing records
- Audit logging integration

## Detailed Functional Requirements

### a. Branch Routing
i. System shall support one shared workflow engine while applying different internal logic for Plantilla and COS cases.  
ii. Plantilla cases shall follow the stricter policy-aware recruitment path.  
iii. COS cases shall follow a lighter flexible path consistent with the study scope.  

### b. Level-Aware Internal Routing
i. System shall use level classification or routing basis as an internal routing control.  
ii. Level 1 cases shall be routed to the Secretariat.  
iii. Level 2 cases shall be routed to the HRM Chief.  
iv. The same routing logic may be applied to COS as an internal office control.  

### c. Routing Restrictions and Override
i. Secretariat shall be prevented from processing Level 2 cases.  
ii. Authorized routing override shall be supported only through controlled action.  
iii. All routing and override actions shall be logged.  

---

# Module 6. Document Review and Qualification Screening

## Purpose
Supports completeness checking and qualification-related review of applications.

## Primary Actors
- Secretariat
- HRM Chief

## Technology Basis
- Django forms and case views
- PostgreSQL screening records

## Detailed Functional Requirements

### a. Completeness Review
i. Authorized internal handler shall be able to review application completeness.  
ii. System shall record completeness findings and screening notes.  
iii. Qualified and not-qualified outcomes shall be supported where applicable.  

### b. Qualification-Related Review
i. System shall support qualification checking according to the selected recruitment path and position requirements.  
ii. Screening remarks and internal notes shall be storable per case.  
iii. Screening outputs shall be preserved as part of the recruitment case record.  

### c. Screening Finalization
i. Screening outputs shall be finalizable before progression to the next stage.  
ii. Finalized screening outputs shall be stage-locked.  
iii. Screening finalization actions shall be recorded in the audit log.  

---

# Module 7. Examination Management

## Purpose
Manages examination data where applicable.

## Primary Actors
- Secretariat
- HRM Chief

## Technology Basis
- Django exam record forms
- PostgreSQL exam storage

## Detailed Functional Requirements

### a. Examination Record Handling
i. System shall support creation of examination records where applicable.  
ii. Examination type and result shall be recordable per case.  
iii. Examination stage shall be applied according to branch-specific workflow rules where applicable.  

### b. Examination Status and Validity
i. System shall support recording of examination validity period where applicable.  
ii. System shall allow recording of waiver or absence status where applicable.  
iii. Examination-related remarks shall be storable per case.  

### c. Examination Output Preservation
i. Examination results shall be preserved as part of the recruitment case history.  
ii. Finalized examination outputs shall be protected against unauthorized modification.  
iii. Examination-related actions shall be traceable in the audit log.  

---

# Module 8. Interview and Rating Management

## Purpose
Manages interview scheduling, rating capture, and preservation of interview outputs.

## Primary Actors
- Secretariat
- HRM Chief
- HRMPSB Member

## Technology Basis
- Django interview scheduling forms
- Rating entry interfaces
- PostgreSQL rating storage
- File upload support for scanned fallback

## Detailed Functional Requirements

### a. Interview Scheduling
i. System shall allow scheduling of interview sessions.  
ii. Interview session shall be linked to the appropriate recruitment case or recruitment entry.  
iii. Interview stage shall support both Plantilla and COS where applicable.  

### b. Interview Ratings
i. Authorized evaluators shall be able to encode interview ratings.  
ii. System shall support direct rating input according to the role and workflow branch.  
iii. Rating justifications shall be recordable where required by workflow rules.  

### c. Fallback Rating Handling
i. System shall support scanned fallback rating sheet upload where applicable.  
ii. Uploaded fallback rating files shall be linked to the corresponding case and stage.  
iii. Finalized interview outputs shall be stage-locked.  

---

# Module 9. Deliberation and Decision Support

## Purpose
Consolidates evaluation outputs and produces decision-support artifacts.

## Primary Actors
- Secretariat
- HRM Chief
- HRMPSB Member

## Technology Basis
- Django consolidation logic
- ReportLab for CAR generation
- PostgreSQL storage for deliberation records

## Detailed Functional Requirements

### a. Consolidation of Evaluation Outputs
i. System shall consolidate finalized review, examination, and interview outputs where applicable.  
ii. Consolidated records shall preserve references to locked source records.  
iii. Consolidation shall support branch-appropriate decision-support handling.  

### b. Deliberation Record Handling
i. System shall support creation of deliberation records or equivalent minutes where applicable.  
ii. Deliberation records shall be linked to the corresponding recruitment case or entry.  
iii. Finalized deliberation records shall be preserved as controlled artifacts.  

### c. Ranking and CAR
i. System shall support ranking outputs where applicable.  
ii. System shall generate the Comparative Assessment Report for Plantilla where applicable.  
iii. Finalized CAR and decision-support artifacts shall be locked against ordinary modification.  

---

# Module 10. Decision and Approval Handling

## Purpose
Records final decisions and manages approval-side actions.

## Primary Actors
- Appointing Authority
- Secretariat
- HRM Chief

## Technology Basis
- Django decision forms
- PostgreSQL decision records

## Detailed Functional Requirements

### a. Submission Packet Preparation
i. System shall prepare a structured packet of decision-support outputs and evidence references where applicable.  
ii. Submission packet shall preserve the current decision context of the recruitment case.  

### b. Final Decision Recording
i. Appointing Authority shall be able to record final decision actions for Plantilla where applicable.  
ii. System shall support recording of selected and not-selected outcomes.  
iii. Decision history shall be preserved as part of the case record.  

### c. Pre-Decision Artifact Preservation
i. System shall preserve pre-decision artifacts as read-only once a final decision is recorded.  
ii. Decision actions and related timestamps shall be audit-logged.  

---

# Module 11. Notification Management

## Purpose
Manages applicant-facing notifications as a standalone feature.

## Primary Actors
- Secretariat
- HRM Chief
- Applicant

## Technology Basis
- Django notification logic
- Gmail SMTP email sending
- PostgreSQL notification history

## Detailed Functional Requirements

### a. Submission and Status Notifications
i. System shall support notification of successful application submission.  
ii. System shall support status-related notifications where applicable.  
iii. Notification history shall be preserved per application or case.  

### b. Selection and Non-Selection Notifications
i. System shall support selected-applicant notification.  
ii. System shall support non-selected-applicant notification.  
iii. Notification content shall reflect the branch-appropriate result.  

### c. Requirement and Deadline Notifications
i. System shall support requirement checklist notifications for selected applicants where applicable.  
ii. System shall support deadline or reminder notifications where applicable.  
iii. All sent notifications shall be traceable in system records.  

---

# Module 12. Appointment and Contract Completion

## Purpose
Supports post-selection completion within recruitment scope.

## Primary Actors
- Secretariat
- HRM Chief

## Technology Basis
- Django completion-tracking forms
- PostgreSQL completion records

## Detailed Functional Requirements

### a. Plantilla Completion Tracking
i. System shall support appointment-related completion tracking for Plantilla.  
ii. System shall support recording of requirement checklist status for selected Plantilla cases.  
iii. Announcement record storage shall be supported where applicable.  

### b. COS Completion Tracking
i. System shall support contract-related completion tracking for COS.  
ii. System shall support recording of requirement checklist status for selected COS cases.  
iii. Contract-completion records shall be preserved as part of the recruitment case.  

### c. Case Closure
i. System shall support closure of recruitment cases after completion and record handling are finished.  
ii. Closed cases shall remain retrievable for later review.  
iii. Full onboarding shall remain out of scope of the current system.  

---

# Module 13. Evidence Vault and Record Management

## Purpose
Stores and manages recruitment artifacts and records.

## Primary Actors
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority

## Technology Basis
- PostgreSQL metadata records
- Django file handling
- SHA-256 via hashlib
- AES-256-GCM via cryptography for selected sensitive stored data

## Detailed Functional Requirements

### a. Centralized Evidence Storage
i. System shall store uploaded recruitment artifacts in a centralized Evidence Vault.  
ii. Stored artifacts shall be linked to case, stage, uploader, and timestamp metadata.  
iii. Selected sensitive stored data shall be protected using authenticated encryption where applicable.  

### b. Version Preservation and Retrieval
i. System shall preserve evidence versions without silent overwrite.  
ii. System shall support searchable retrieval of stored recruitment records.  
iii. System shall support archival tagging and retention-related handling within study scope.  

### c. Evidence Integrity
i. System shall generate SHA-256 hash values for evidence-related files where applicable.  
ii. System shall preserve integrity metadata needed for later verification.  
iii. Controlled evidence-handling events shall be traceable in the audit log.  

---

# Module 14. Audit Logging and Traceability

## Purpose
Provides recruitment-grade accountability and traceability.

## Primary Actors
- All internal roles
- System Administrator

## Technology Basis
- PostgreSQL audit log storage
- Django server-side event logging

## Detailed Functional Requirements

### a. Workflow and Security Event Logging
i. System shall maintain recruitment-grade audit logs for critical workflow and security-relevant actions.  
ii. Logged events shall include actor identity, role, action, timestamp, case reference, and workflow stage.  
iii. Routing actions, override actions, export actions, and other sensitive events shall be included in the audit scope.  

### b. Traceability Support
i. System shall support traceability across the full recruitment lifecycle.  
ii. Screening, evaluation, decision, evidence, routing, and export activities shall remain attributable.  
iii. Audit records shall support later review for accountability and defensibility.  

### c. Sensitive Access Logging
i. Sensitive access actions such as viewing protected records, controlled reopen, and export operations shall be recorded where applicable.  
ii. Audit-log viewing and related sensitive access shall themselves be traceable.  

---

# Module 15. Evidence Export and Integrity Verification

## Purpose
Generates controlled export bundles and supports independent integrity verification.

## Primary Actors
- Secretariat
- HRM Chief
- Appointing Authority

## Technology Basis
- ReportLab
- Python zipfile
- SHA-256 integrity verification output
- Django export permission logic

## Detailed Functional Requirements

### a. Controlled Export
i. System shall restrict export actions according to authorized roles.  
ii. System shall support generation of export bundles containing required recruitment records and outputs.  
iii. Export actions shall be recorded in the audit log.  

### b. Export Bundle Content
i. System shall generate an evidence inventory as part of the export bundle.  
ii. System shall include integrity verification output for exported evidence.  
iii. Export bundle contents shall remain traceable to the originating recruitment case and workflow context.  

### c. Integrity Verification
i. Exported evidence shall support SHA-256-based integrity verification.  
ii. Export bundles shall remain independently verifiable outside the system.  
iii. System shall support defensible release of recruitment evidence within study scope.  

---

## 5. Project Assumptions and Scope Notes

- RecruitGuard-CHD is bounded to the recruitment scope only.
- Plantilla and COS are supported within one shared system but not through one identical linear workflow.
- COS remains a lighter flexible branch.
- No additional End-user actor is introduced in the locked design.
- COS handling remains under the current internal roles, primarily the Secretariat and HRM Chief.
- Background investigation is not included as a standalone function in the locked current plan.
- Full onboarding is out of scope.
- Security validation uses a controlled staging environment and dummy or synthetic data only.
