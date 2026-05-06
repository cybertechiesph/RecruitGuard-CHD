# Starter Backlog

Use these as the first GitHub Issues for the `RecruitGuard-CHD Development` project board.

## 1. Configure GitHub Projects Board and Repository Labels

Type: Task

Priority: P1

Suggested labels: `type:task`, `priority:p1`

Desired outcome:

Create the repository project board and labels described in `docs/project-tracking.md` so issues can be filtered by type, priority, branch, module, and risk.

Checklist:

- [ ] Create GitHub Project named `RecruitGuard-CHD Development`.
- [ ] Add status values: Backlog, Ready, In Progress, Code Review, Testing, Blocked, Done.
- [ ] Create labels from `docs/project-tracking.md`.
- [ ] Add this starter backlog to the project board.

## 2. Run Full Test Suite and Record Baseline

Type: Task

Priority: P1

Suggested labels: `type:test`, `priority:p1`

Desired outcome:

Establish the current passing test baseline after the initial GitHub import.

Checklist:

- [ ] Install dependencies in a clean virtual environment.
- [ ] Copy `.env.example` to `.env` and use synthetic local values.
- [ ] Run Django migrations.
- [ ] Run the full Django test suite.
- [ ] Record test count, failures, and environment notes in the issue.

## 3. Verify Branch and Level Routing Rules

Type: Feature

Priority: P0

Suggested labels: `type:feature`, `priority:p0`, `module:routing`, `routing`, `security`, `workflow`

Desired outcome:

Confirm that Level 1 cases route to Secretariat, Level 2 cases route to HRM Chief, and Secretariat cannot process Level 2 cases unless a controlled audit-logged override exists.

Acceptance criteria:

- [ ] Level 1 Plantilla and COS cases route to Secretariat where applicable.
- [ ] Level 2 Plantilla and COS cases route to HRM Chief where applicable.
- [ ] Secretariat receives a server-side denial for Level 2 processing without override.
- [ ] Controlled override behavior is explicitly tested and audit-logged.
- [ ] Tests cover both Plantilla and COS paths where the same internal control applies.

References:

- `docs/routing-rules.md`
- `docs/workflow-rules.md`
- `docs/security-rules.md`

## 4. Review Evidence Vault Encryption and Integrity Flow

Type: Feature

Priority: P0

Suggested labels: `type:feature`, `priority:p0`, `module:evidence-vault`, `module:export-integrity`, `security`, `evidence`, `audit`

Desired outcome:

Validate that evidence storage, retrieval, versioning, export, SHA-256 integrity, and AES-256-GCM handling match the locked stack and security rules.

Acceptance criteria:

- [ ] Evidence upload size and type validations are enforced.
- [ ] Evidence bytes are encrypted using configured AES-256-GCM secret material.
- [ ] SHA-256 digest is stored and shown in integrity outputs.
- [ ] Evidence download and access actions are permission-checked and audit-logged.
- [ ] Export bundles include integrity verification output.

References:

- `docs/security-rules.md`
- `docs/modules.md`
- `docs/ERD-FULL.md`

## 5. QA Applicant Intake and OTP Verification

Type: Feature

Priority: P1

Suggested labels: `type:feature`, `priority:p1`, `module:intake-otp`, `branch:shared`, `security`

Desired outcome:

Verify the applicant-facing submission path, OTP lifecycle, receipt, and status lookup using synthetic applicant data.

Acceptance criteria:

- [ ] Applicant can view active vacancies/openings.
- [ ] Applicant can submit required documents.
- [ ] OTP is generated, hashed, sent through the configured email service, and expires correctly.
- [ ] Verified applications produce a receipt/reference.
- [ ] Status lookup does not leak protected internal data.

References:

- `docs/FRS.md`
- `docs/use-cases.md`
- `docs/security-rules.md`

## 6. QA Evaluation Stage Locks

Type: Feature

Priority: P1

Suggested labels: `type:feature`, `priority:p1`, `module:screening`, `module:examination`, `module:interview-rating`, `workflow`

Desired outcome:

Confirm that screening, examination, and interview records can only be created, updated, finalized, or locked by the allowed actors at the allowed workflow stages.

Acceptance criteria:

- [ ] Screening records respect actor and stage restrictions.
- [ ] Examination records respect actor and stage restrictions.
- [ ] Interview sessions and ratings respect actor and stage restrictions.
- [ ] Finalized stages become read-only except through documented controlled reopen paths.
- [ ] Audit logs are created for protected workflow actions.

References:

- `docs/workflow-rules.md`
- `docs/security-rules.md`

## 7. Prepare Demo Data and Script

Type: Task

Priority: P2

Suggested labels: `type:task`, `priority:p2`, `type:docs`

Desired outcome:

Prepare a synthetic demonstration flow for thesis review without using real applicant or employee data.

Checklist:

- [ ] Define synthetic users for each direct actor.
- [ ] Define at least one Plantilla Level 1 flow.
- [ ] Define at least one Plantilla Level 2 flow.
- [ ] Define at least one COS flow.
- [ ] Include evidence, audit, notification, and export checkpoints.
- [ ] Confirm no real personal data appears in demo records or screenshots.

## 8. Deployment Readiness Review

Type: Task

Priority: P2

Suggested labels: `type:task`, `priority:p2`, `security`

Desired outcome:

Prepare the application for a secure hosted or review environment while preserving the locked technology stack.

Checklist:

- [ ] Confirm production `.env` requirements.
- [ ] Confirm PostgreSQL settings.
- [ ] Confirm Gmail SMTP settings.
- [ ] Confirm secure cookie and HTTPS settings.
- [ ] Confirm static and media handling.
- [ ] Confirm backup/export handling for evidence records.
- [ ] Confirm no hard-coded secrets exist in committed files.
