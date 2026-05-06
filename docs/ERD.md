# ERD Summary

## System Context
RecruitGuard-CHD is one shared recruitment platform with one database and one workflow engine supporting two recruitment branches:
- Plantilla
- COS

It uses level-aware internal routing:
- Level 1 -> Secretariat
- Level 2 -> HRM Chief
- Secretariat must not process Level 2 cases
- The same routing logic may be applied to COS as an internal office control

## Core Entities
- Applicant
- InternalUser
- PositionReference
- RecruitmentEntry
- Application
- RecruitmentCase
- CaseAssignment
- ScreeningReview
- ExamRecord
- InterviewSession
- InterviewRating
- DeliberationRecord
- ComparativeAssessmentReport
- ComparativeAssessmentReportItem
- FinalDecision
- CompletionRecord
- EvidenceVaultItem
- AuditLog

## Core Distinctions
- PositionReference is separate from RecruitmentEntry and Engagement Type
- RecruitmentEntry is separate from Application
- Application is separate from RecruitmentCase
- Plantilla and COS are separate branches within one system
- Level classification / routing basis is a workflow control
- ComparativeAssessmentReport is operationally aligned at the recruitment-entry level, while ComparativeAssessmentReportItem remains case-linked for ranked rows
- Evidence Vault files are stored as controlled records owned at the application, recruitment-case, or recruitment-entry level
- Audit logging is a system-wide accountability requirement

## Key Relationship Rules
- One Applicant can have many Applications
- One PositionReference can have many RecruitmentEntries
- One RecruitmentEntry can have many Applications
- One Application creates one RecruitmentCase
- One RecruitmentCase can have many CaseAssignments over time
- One RecruitmentCase can have many ScreeningReview records
- One RecruitmentCase can have many ExamRecord entries where applicable
- One RecruitmentEntry can have many InterviewSession records
- One InterviewSession can have many InterviewRating records
- One RecruitmentEntry can have many DeliberationRecord entries
- One RecruitmentEntry can have many ComparativeAssessmentReport versions where applicable
- One ComparativeAssessmentReport can have many ComparativeAssessmentReportItem rows
- One RecruitmentEntry can have many FinalDecision records over time where applicable
- One Application can have many EvidenceVaultItem records
- One RecruitmentCase may have one CompletionRecord
- One RecruitmentCase can have many EvidenceVaultItem records
- One RecruitmentEntry can have many EvidenceVaultItem records
- One RecruitmentCase can have many AuditLog records

## Routing Rules
- RecruitmentCase stores branch type and routing basis derived from the selected PositionReference
- CaseAssignment preserves routing history
- Level 1 routes to Secretariat
- Level 2 routes to HRM Chief
- Controlled override is allowed only if explicitly implemented and audit-logged

## Evidence Rules
- EvidenceVaultItem stores file metadata, ownership scope, stage association, and version information
- SHA-256 hash values are preserved for evidence integrity
- Selected sensitive stored data may use AES-256-GCM protection where applicable
- Export bundles are controlled outputs derived from stored artifacts and logged actions

## Audit Rules
AuditLog must preserve:
- actor identity
- actor role
- action
- timestamp
- case reference
- workflow stage
- sensitive access events
- routing actions
- export actions

## Scope Notes
- Recruitment only
- Full onboarding is out of scope
- Offboarding, payroll, termination, and full employee lifecycle management are out of scope
- COS remains a lighter flexible branch, not identical to Plantilla
