# AGENTS.md

## Project
RecruitGuard-CHD is a secure-by-design web-based recruitment management system for DOH–CHD CALABARZON.

## Core Architecture
- One platform
- One database
- One workflow engine
- Two recruitment branches: Plantilla and COS

## Direct Actors
- Applicant
- Secretariat
- HRM Chief
- HRMPSB Member
- Appointing Authority
- System Administrator

## Routing Rules
- Level 1 -> Secretariat
- Level 2 -> HRM Chief
- Secretariat must not process Level 2 cases
- Controlled override is allowed only if explicitly implemented and audit-logged
- The same routing logic may be applied to COS as an internal office control

## Workflow Rules
- Plantilla follows the stricter policy-aware path
- COS follows a lighter flexible path
- COS is not identical to Plantilla
- Full onboarding, offboarding, payroll, termination, and full employee lifecycle functions are out of scope

## Locked Technology Stack
- Python 3
- Django
- Django Templates
- Bootstrap
- PostgreSQL
- Gmail SMTP for OTP email
- Python hashlib for SHA-256
- Python cryptography for AES-256-GCM
- ReportLab
- Python zipfile

## Core Modules
1. Identity and Access Control
2. Recruitment Entry and Vacancy Management
3. Applicant Intake and OTP Verification
4. Recruitment Case Management and Workflow Engine
5. Branch-Aware and Level-Aware Routing
6. Document Review and Qualification Screening
7. Examination Management
8. Interview and Rating Management
9. Deliberation and Decision Support
10. Decision and Approval Handling
11. Notification Management
12. Appointment and Contract Completion
13. Evidence Vault and Record Management
14. Audit Logging and Traceability
15. Evidence Export and Integrity Verification

## Development Rules
- Follow Django best practices
- Reuse existing repo patterns when possible
- Use server-side enforcement for access control
- Do not invent new actors
- Do not hard-code secrets
- Use environment variables for database, email, and encryption keys
- Use dummy or synthetic data only
- Keep code modular and readable
- Align implementation with the docs files before making architectural changes
- If a requirement is ambiguous, make the smallest safe assumption and state it clearly at the end of the task summary

## Before Editing
Read these first if they exist:
- AGENTS.md
- README.md
- docs/FRS.md
- docs/modules.md
- docs/workflow-rules.md
- docs/routing-rules.md
- docs/security-rules.md
- docs/use-cases.md
- docs/ERD.md
- docs/ERD-FULL.md
- docs/development-checklist.md

## Definition of Done
For each implementation task:
- inspect the repository first
- implement the requested feature, not just a plan
- update models, forms, views, urls, templates, admin, and tests as needed
- add access restrictions and validations
- run the most relevant tests if possible
- summarize:
  - files changed
  - what was implemented
  - assumptions made
  - what still needs manual review
