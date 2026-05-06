# Finalized ERD Specification

This document contains the full finalized ERD specification for RecruitGuard-CHD.

---

## 1. Applicant

### Purpose
Stores the identity and contact information of external applicants who submit applications through the applicant portal.

### Attributes
- applicant_id (PK)
- first_name
- middle_name
- last_name
- email
- mobile_number
- address
- created_at
- updated_at

### Relationships
- One Applicant can have many Applications

### Cardinality
- Applicant 1 : M Application

---

## 2. InternalUser

### Purpose
Stores internal system users who participate in the recruitment workflow.

### Attributes
- user_id (PK)
- full_name
- email
- password_hash
- role_type
- is_active
- created_at
- updated_at
- last_login_at

### Notes
role_type covers:
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority
- System Administrator

### Relationships
- One InternalUser can have many CaseAssignment records
- One InternalUser can have many ScreeningReview records
- One InternalUser can have many ExamRecord entries
- One InternalUser can have many InterviewRating entries
- One InternalUser can have many DeliberationRecord entries
- One InternalUser can have many ComparativeAssessmentReport generated entries
- One InternalUser can have many FinalDecision entries
- One InternalUser can have many EvidenceVaultItem uploads
- One InternalUser can have many AuditLog entries

---

## 3. PositionReference

### Purpose
Stores controlled official position metadata reused by recruitment entries.

### Attributes
- position_reference_id (PK)
- position_title
- position_slug
- salary_grade
- level_classification
- class_id
- os_code
- occupational_service
- occupational_group
- reference_status
- is_active
- notes
- position_code
- agency_item_number
- office_division_default
- qs_education
- qs_training
- qs_experience
- qs_eligibility
- employment_track_applicability

### Relationships
- One PositionReference can have many RecruitmentEntry records

### Cardinality
- PositionReference 1 : M RecruitmentEntry

---

## 4. RecruitmentEntry

### Purpose
Represents the recruitment opening or entry point for either Plantilla or COS.

### Attributes
- entry_id (PK)
- position_reference_id (FK -> PositionReference.position_reference_id)
- engagement_type
- entry_mode
- routing_basis
- status
- publication_start_date
- publication_end_date
- intake_mode
- created_by (FK -> InternalUser.user_id)
- created_at
- updated_at

### Notes
Possible values:
- engagement_type: Plantilla, COS
- entry_mode: Published Vacancy, COS Opening, COS Pooling
- intake_mode: Fixed Window, Continuous, Pooling

### Relationships
- One RecruitmentEntry belongs to one PositionReference
- One RecruitmentEntry can have many Applications
- One RecruitmentEntry can have many InterviewSession records
- One RecruitmentEntry can have many DeliberationRecord records
- One RecruitmentEntry can have many ComparativeAssessmentReport records
- One RecruitmentEntry can have many FinalDecision records
- One RecruitmentEntry can have many EvidenceVaultItem records
- One RecruitmentEntry can have many AuditLog records

### Cardinality
- PositionReference 1 : M RecruitmentEntry
- RecruitmentEntry 1 : M Application
- RecruitmentEntry 1 : M InterviewSession
- RecruitmentEntry 1 : M DeliberationRecord
- RecruitmentEntry 1 : M ComparativeAssessmentReport
- RecruitmentEntry 1 : M FinalDecision
- RecruitmentEntry 1 : M EvidenceVaultItem
- RecruitmentEntry 1 : M AuditLog

---

## 5. Application

### Purpose
Represents the applicant’s submitted application to a specific recruitment entry.

### Attributes
- application_id (PK)
- applicant_id (FK -> Applicant.applicant_id)
- entry_id (FK -> RecruitmentEntry.entry_id)
- application_code
- otp_verified_at
- completeness_status
- next_in_rank_flag
- for_pooling_flag
- submission_status
- submitted_at
- updated_at

### Relationships
- One Application belongs to one Applicant
- One Application belongs to one RecruitmentEntry
- One Application creates one RecruitmentCase
- One Application can have many EvidenceVaultItem records

### Cardinality
- Applicant 1 : M Application
- RecruitmentEntry 1 : M Application
- Application 1 : 1 RecruitmentCase
- Application 1 : M EvidenceVaultItem

---

## 6. RecruitmentCase

### Purpose
Represents the internal processing container created from an application.

### Attributes
- case_id (PK)
- application_id (FK, unique -> Application.application_id)
- assigned_branch
- routing_basis
- current_stage
- case_status
- current_handler_role
- stage_locked_flag
- created_at
- updated_at
- closed_at

### Relationships
- One RecruitmentCase belongs to one Application
- One RecruitmentCase can have many CaseAssignment records
- One RecruitmentCase can have many ScreeningReview records
- One RecruitmentCase can have many ExamRecord entries
- One RecruitmentCase can have many InterviewRating records
- One RecruitmentCase can appear in many ComparativeAssessmentReportItem records
- One RecruitmentCase may have one CompletionRecord
- One RecruitmentCase can have many EvidenceVaultItem records
- One RecruitmentCase can have many AuditLog records

### Cardinality
- Application 1 : 1 RecruitmentCase
- RecruitmentCase 1 : M CaseAssignment
- RecruitmentCase 1 : M ScreeningReview
- RecruitmentCase 1 : M ExamRecord
- RecruitmentCase 1 : M InterviewRating
- RecruitmentCase 1 : M ComparativeAssessmentReportItem
- RecruitmentCase 1 : 0..1 CompletionRecord
- RecruitmentCase 1 : M EvidenceVaultItem
- RecruitmentCase 1 : M AuditLog

---

## 7. CaseAssignment

### Purpose
Preserves routing and assignment history of recruitment cases.

### Attributes
- assignment_id (PK)
- case_id (FK -> RecruitmentCase.case_id)
- assigned_to_user_id (FK -> InternalUser.user_id)
- assigned_role
- assignment_reason
- is_override
- assigned_by_user_id (FK -> InternalUser.user_id)
- assigned_at
- ended_at

### Relationships
- One CaseAssignment belongs to one RecruitmentCase
- One CaseAssignment refers to one assigned InternalUser
- One CaseAssignment may refer to one assigning InternalUser

### Cardinality
- RecruitmentCase 1 : M CaseAssignment
- InternalUser 1 : M CaseAssignment

---

## 8. ScreeningReview

### Purpose
Stores completeness and qualification screening outputs.

### Attributes
- screening_id (PK)
- case_id (FK -> RecruitmentCase.case_id)
- reviewer_user_id (FK -> InternalUser.user_id)
- completeness_decision
- qualification_decision
- screening_score
- remarks
- finalized_at
- locked_flag

### Relationships
- One ScreeningReview belongs to one RecruitmentCase
- One ScreeningReview belongs to one reviewer InternalUser

### Cardinality
- RecruitmentCase 1 : M ScreeningReview
- InternalUser 1 : M ScreeningReview

---

## 9. ExamRecord

### Purpose
Stores exam-related records where applicable.

### Attributes
- exam_id (PK)
- case_id (FK -> RecruitmentCase.case_id)
- exam_type
- score
- validity_end_date
- waiver_flag
- absence_flag
- remarks
- encoded_by_user_id (FK -> InternalUser.user_id)
- encoded_at

### Relationships
- One ExamRecord belongs to one RecruitmentCase
- One ExamRecord may be encoded by one InternalUser

### Cardinality
- RecruitmentCase 1 : M ExamRecord
- InternalUser 1 : M ExamRecord

---

## 10. InterviewSession

### Purpose
Stores interview scheduling and session information.

### Attributes
- session_id (PK)
- entry_id (FK -> RecruitmentEntry.entry_id)
- scheduled_date
- scheduled_time
- venue_or_mode
- quorum_status
- created_by_user_id (FK -> InternalUser.user_id)
- created_at

### Relationships
- One InterviewSession belongs to one RecruitmentEntry
- One InterviewSession can have many InterviewRating records

### Cardinality
- RecruitmentEntry 1 : M InterviewSession
- InterviewSession 1 : M InterviewRating

---

## 11. InterviewRating

### Purpose
Stores interview ratings associated with cases and sessions.

### Attributes
- rating_id (PK)
- session_id (FK -> InterviewSession.session_id)
- case_id (FK -> RecruitmentCase.case_id)
- member_user_id (FK -> InternalUser.user_id)
- rating_score
- justification_text
- source_mode
- encoded_at
- locked_flag

### Relationships
- One InterviewRating belongs to one InterviewSession
- One InterviewRating belongs to one RecruitmentCase
- One InterviewRating belongs to one InternalUser evaluator

### Cardinality
- InterviewSession 1 : M InterviewRating
- RecruitmentCase 1 : M InterviewRating
- InternalUser 1 : M InterviewRating

---

## 12. DeliberationRecord

### Purpose
Stores deliberation minutes and related deliberation artifacts.

### Attributes
- deliberation_id (PK)
- entry_id (FK -> RecruitmentEntry.entry_id)
- minutes_text
- recorded_by_user_id (FK -> InternalUser.user_id)
- finalized_at
- locked_flag

### Relationships
- One DeliberationRecord belongs to one RecruitmentEntry
- One DeliberationRecord may be recorded by one InternalUser

### Cardinality
- RecruitmentEntry 1 : M DeliberationRecord
- InternalUser 1 : M DeliberationRecord

---

## 13. ComparativeAssessmentReport

### Purpose
Stores CAR header/version records where applicable, mainly for Plantilla.

### Attributes
- car_id (PK)
- entry_id (FK -> RecruitmentEntry.entry_id)
- version_no
- generated_by_user_id (FK -> InternalUser.user_id)
- generated_at
- finalized_at
- locked_flag

### Relationships
- One ComparativeAssessmentReport belongs to one RecruitmentEntry
- One ComparativeAssessmentReport can have many ComparativeAssessmentReportItem records
- One ComparativeAssessmentReport may be generated by one InternalUser

### Cardinality
- RecruitmentEntry 1 : M ComparativeAssessmentReport
- ComparativeAssessmentReport 1 : M ComparativeAssessmentReportItem
- InternalUser 1 : M ComparativeAssessmentReport
- Each CAR row is an entry-scoped preserved version for the same review stage; ranked rows remain case-linked through ComparativeAssessmentReportItem.

---

## 14. ComparativeAssessmentReportItem

### Purpose
Stores ranked rows associated with a CAR.

### Attributes
- car_item_id (PK)
- car_id (FK -> ComparativeAssessmentReport.car_id)
- case_id (FK -> RecruitmentCase.case_id)
- total_score
- rank_no
- remarks

### Relationships
- One ComparativeAssessmentReportItem belongs to one ComparativeAssessmentReport
- One ComparativeAssessmentReportItem refers to one RecruitmentCase

### Cardinality
- ComparativeAssessmentReport 1 : M ComparativeAssessmentReportItem
- RecruitmentCase 1 : M ComparativeAssessmentReportItem

---

## 15. FinalDecision

### Purpose
Stores final decision outcomes where applicable.

### Attributes
- decision_id (PK)
- entry_id (FK -> RecruitmentEntry.entry_id)
- selected_case_id (FK -> RecruitmentCase.case_id, nullable)
- decision_type
- decided_by_user_id (FK -> InternalUser.user_id)
- decision_date
- remarks

### Relationships
- One FinalDecision belongs to one RecruitmentEntry
- One FinalDecision may refer to one selected RecruitmentCase
- One FinalDecision may be made by one InternalUser

### Cardinality
- RecruitmentEntry 1 : M FinalDecision
- RecruitmentCase 0..1 : M FinalDecision references
- InternalUser 1 : M FinalDecision

---

## 16. CompletionRecord

### Purpose
Stores appointment-related or contract-related completion tracking within recruitment scope.

### Attributes
- completion_id (PK)
- case_id (FK -> RecruitmentCase.case_id, unique)
- completion_type
- requirements_status
- deadline_status
- announcement_record_flag
- completed_at
- closure_notes

### Relationships
- One CompletionRecord belongs to one RecruitmentCase

### Cardinality
- RecruitmentCase 1 : 0..1 CompletionRecord

---

## 17. EvidenceVaultItem

### Purpose
Stores uploaded and generated recruitment artifacts and evidence files.

### Attributes
- artifact_id (PK)
- application_id (FK -> Application.application_id, nullable)
- case_id (FK -> RecruitmentCase.case_id, nullable)
- entry_id (FK -> RecruitmentEntry.entry_id, nullable)
- ownership_scope
- artifact_type
- stage_name
- file_name
- file_path
- sha256_hash
- version_no
- uploaded_by_user_id (FK -> InternalUser.user_id, nullable)
- uploaded_at

### Relationships
- One EvidenceVaultItem may belong to one Application
- One EvidenceVaultItem may belong to one RecruitmentCase
- One EvidenceVaultItem may belong to one RecruitmentEntry
- One EvidenceVaultItem may be uploaded by one InternalUser

### Cardinality
- Application 1 : M EvidenceVaultItem
- RecruitmentCase 1 : M EvidenceVaultItem
- RecruitmentEntry 1 : M EvidenceVaultItem
- InternalUser 1 : M EvidenceVaultItem

---

## 18. AuditLog

### Purpose
Stores audit-trail records for workflow and security-relevant actions.

### Attributes
- audit_id (PK)
- actor_user_id (FK -> InternalUser.user_id, nullable)
- actor_role
- action_type
- entry_id (FK -> RecruitmentEntry.entry_id, nullable)
- case_id (FK -> RecruitmentCase.case_id, nullable)
- stage_name
- timestamp
- details

### Relationships
- One AuditLog may refer to one InternalUser
- One AuditLog may refer to one RecruitmentEntry
- One AuditLog may refer to one RecruitmentCase

### Cardinality
- InternalUser 1 : M AuditLog
- RecruitmentEntry 1 : M AuditLog
- RecruitmentCase 1 : M AuditLog

---

# Relationship Summary

## Main Flow
- PositionReference 1 : M RecruitmentEntry
- Applicant 1 : M Application
- RecruitmentEntry 1 : M Application
- Application 1 : 1 RecruitmentCase

## Routing / Workflow
- RecruitmentCase 1 : M CaseAssignment
- RecruitmentCase 1 : M ScreeningReview
- RecruitmentCase 1 : M ExamRecord
- RecruitmentCase 1 : M InterviewRating
- RecruitmentCase 1 : M EvidenceVaultItem
- RecruitmentCase 1 : M AuditLog

## Entry-Level Evaluation
- RecruitmentEntry 1 : M InterviewSession
- RecruitmentEntry 1 : M DeliberationRecord
- RecruitmentEntry 1 : M ComparativeAssessmentReport
- RecruitmentEntry 1 : M FinalDecision
- RecruitmentEntry 1 : M EvidenceVaultItem
- RecruitmentEntry 1 : M AuditLog

## Decision Support
- ComparativeAssessmentReport 1 : M ComparativeAssessmentReportItem
- RecruitmentCase 1 : M ComparativeAssessmentReportItem

## Completion
- RecruitmentCase 1 : 0..1 CompletionRecord

# Design Notes
- PositionReference is separate from RecruitmentEntry
- Application is separate from RecruitmentCase
- COS and Plantilla share one system but use different branch logic
- Level classification / routing basis is a workflow control
- Evidence integrity is handled through artifact metadata, versioning, and SHA-256
- Selected sensitive stored data may use AES-256-GCM where applicable
- Audit logging is mandatory for security-relevant and workflow-relevant actions
