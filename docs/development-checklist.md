# Development Checklist

## 1. Purpose
This checklist guides the development sequence of RecruitGuard-CHD so implementation stays aligned with the locked thesis design, module set, routing rules, and security controls.

---

## 2. Before Coding

### Environment Setup
- [ ] Install Python 3
- [ ] Install pip
- [ ] Install virtual environment support
- [ ] Install Git
- [ ] Install PostgreSQL
- [ ] Install VS Code or equivalent editor

### Repo Setup
- [ ] Initialize Git repository
- [ ] Create and activate virtual environment
- [ ] Create requirements.txt
- [ ] Create .gitignore
- [ ] Create .env.example
- [ ] Create AGENTS.md
- [ ] Create docs/ files

### Core Config
- [ ] Create PostgreSQL database
- [ ] Create database user and password
- [ ] Configure environment variables
- [ ] Configure Django settings for PostgreSQL
- [ ] Configure static and media paths
- [ ] Configure email settings for Gmail SMTP
- [ ] Generate and store encryption key for AES-256-GCM in environment variables

---

## 3. Locked Design References
Before implementing any module, confirm these are frozen and reflected in the repo:
- [ ] Final actor set
- [ ] Plantilla vs COS branch logic
- [ ] Level 1 -> Secretariat
- [ ] Level 2 -> HRM Chief
- [ ] Secretariat blocked from Level 2
- [ ] COS remains lighter and flexible
- [ ] Out-of-scope items documented
- [ ] Final module set documented
- [ ] ERD summary available
- [ ] Full ERD reference available

---

## 4. Recommended App Structure
Suggested Django app grouping:
- accounts
- recruitment
- intake
- workflow
- evaluation
- notifications
- records
- auditlog

---

## 5. Development Sequence

### Phase 1: Foundation
- [ ] Bootstrap Django project
- [ ] Create base templates
- [ ] Configure PostgreSQL
- [ ] Create custom user/role model or extension
- [ ] Add base dashboard and navigation
- [ ] Register admin models

### Phase 2: Identity and Access Control
- [ ] Login/logout
- [ ] Role-based access control
- [ ] Internal account management
- [ ] Session control
- [ ] Tests for restricted access

### Phase 3: Recruitment Entry and Vacancy Management
- [ ] PositionReference catalog model
- [ ] RecruitmentEntry model
- [ ] Controlled selection from PositionReference during entry creation
- [ ] Plantilla entry creation
- [ ] COS opening creation
- [ ] COS pooling entry creation
- [ ] Entry activation/suspension/closure
- [ ] Tests for valid/invalid entry states

### Phase 4: Applicant Intake and OTP Verification
- [ ] Applicant model if needed
- [ ] Application model
- [ ] Shared applicant portal
- [ ] Plantilla/COS path selection
- [ ] OTP send/verify
- [ ] Application ID generation
- [ ] Submission receipt/status
- [ ] OTP flow tests

### Phase 5: Recruitment Case and Workflow Engine
- [ ] RecruitmentCase model
- [ ] Case timeline/event model
- [ ] Stage progression logic
- [ ] Stage-locking
- [ ] Controlled reopen
- [ ] Tests for stage enforcement

### Phase 6: Routing
- [ ] CaseAssignment model
- [ ] Branch-aware routing
- [ ] Level-aware routing
- [ ] Secretariat blocked from Level 2
- [ ] Optional controlled override
- [ ] Routing tests

### Phase 7: Evaluation Modules
- [ ] ScreeningReview model and UI
- [ ] ExamRecord model and UI
- [ ] InterviewSession model and UI
- [ ] InterviewRating model and UI
- [ ] Rating justification support
- [ ] Scanned fallback support
- [ ] Evaluation finalization tests

### Phase 8: Deliberation and Decision Support
- [ ] DeliberationRecord model
- [ ] CAR model and items
- [ ] Consolidation logic
- [ ] ReportLab CAR generation
- [ ] Finalization and lock rules
- [ ] Decision-support tests

### Phase 9: Decision and Approval
- [ ] FinalDecision model
- [ ] Submission packet view
- [ ] Read-only pre-decision artifacts
- [ ] Approval-side restrictions
- [ ] Decision tests

### Phase 10: Notification Management
- [ ] Notification model
- [ ] Email notification service
- [ ] Submission acknowledgment
- [ ] Selected/non-selected notice
- [ ] Checklist/deadline notices
- [ ] Notification history

### Phase 11: Appointment and Contract Completion
- [ ] CompletionRecord model
- [ ] Plantilla completion tracking
- [ ] COS completion tracking
- [ ] Checklist status
- [ ] Deadline tracking
- [ ] Case closure

### Phase 12: Evidence, Audit, and Export
- [ ] EvidenceVaultItem model
- [ ] Metadata binding
- [ ] SHA-256 generation
- [ ] Version preservation
- [ ] AuditLog model
- [ ] Centralized audit logging service
- [ ] Export bundle generation
- [ ] Evidence inventory
- [ ] Integrity verification output
- [ ] Export permission enforcement

---

## 6. Testing Checkpoints

### After Each Module
- [ ] Model tests
- [ ] Access control tests
- [ ] Form validation tests
- [ ] View/template rendering checks
- [ ] Audit logging coverage check

### System-Wide Before Evaluation
- [ ] Plantilla branch test pass
- [ ] COS branch test pass
- [ ] Level-aware routing test pass
- [ ] Secretariat blocked from Level 2 verified
- [ ] OTP enforcement verified
- [ ] Stage-locking verified
- [ ] Evidence hashing verified
- [ ] Export bundle verified
- [ ] Critical audit events verified

---

## 7. Security and Hardening Checklist
- [ ] Use environment variables for secrets
- [ ] Use hashed passwords
- [ ] Use hashed OTP records with expiry
- [ ] Use HTTPS/TLS in deployment
- [ ] Use AES-256-GCM for selected sensitive stored data where applicable
- [ ] Use SHA-256 for evidence integrity
- [ ] Run Bandit
- [ ] Run pip-audit
- [ ] Run OWASP ZAP on staging
- [ ] Perform manual Burp checks on access control and workflow restrictions
- [ ] Fix critical/high findings before acceptance

---

## 8. Data and Evaluation Preparation
- [ ] Prepare dummy internal accounts for all roles
- [ ] Prepare dummy Plantilla entries
- [ ] Prepare dummy COS entries
- [ ] Prepare dummy applicant submissions
- [ ] Prepare role-specific task scripts
- [ ] Prepare applicant-side usability forms
- [ ] Prepare internal evaluator forms
- [ ] Prepare logging and export evidence for demonstration

---

## 9. Done Criteria
A module is considered done when:
- the core model exists
- the core UI exists
- role restrictions are enforced
- validations are enforced
- audit logging is present where relevant
- tests exist and pass or have been reviewed
- the implementation matches the docs and locked workflow rules

---

## 10. Final Pre-Staging Checklist
- [ ] All migrations clean
- [ ] All required apps registered
- [ ] No hard-coded secrets
- [ ] Core test suite passing
- [ ] Dummy data only
- [ ] FRS alignment checked
- [ ] Routing rules alignment checked
- [ ] Security rules alignment checked
- [ ] README updated with local setup steps
