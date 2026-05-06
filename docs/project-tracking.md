# Project Tracking Guide

RecruitGuard-CHD uses GitHub Issues as the free source of truth for development tracking. Keep planning close to the repository so issues, commits, pull requests, tests, and release notes stay connected.

## Recommended Views

Create one GitHub Project for the repository named `RecruitGuard-CHD Development` with these views:

- Board grouped by `Status`
- Table sorted by priority, module, and updated date
- Roadmap grouped by milestone or phase

Recommended status values:

- Backlog
- Ready
- In Progress
- Code Review
- Testing
- Blocked
- Done

## Labels

Use labels consistently so the board remains searchable.

Type labels:

- `type:feature`
- `type:bug`
- `type:task`
- `type:docs`
- `type:test`

Priority labels:

- `priority:p0`
- `priority:p1`
- `priority:p2`
- `priority:p3`

Branch labels:

- `branch:shared`
- `branch:plantilla`
- `branch:cos`

Risk labels:

- `security`
- `audit`
- `routing`
- `evidence`
- `workflow`
- `blocked`

Module labels:

- `module:identity`
- `module:entry-management`
- `module:intake-otp`
- `module:workflow-engine`
- `module:routing`
- `module:screening`
- `module:examination`
- `module:interview-rating`
- `module:deliberation`
- `module:decision-approval`
- `module:notifications`
- `module:completion`
- `module:evidence-vault`
- `module:audit-logging`
- `module:export-integrity`

## Issue Rules

Every feature issue should include:

- affected actor
- affected branch, if Plantilla or COS specific
- affected level, if Level 1 or Level 2 specific
- server-side access restriction expectations
- audit logging expectation for protected workflow changes
- acceptance criteria
- relevant docs references

Every bug issue should include:

- observed behavior
- expected behavior
- reproduction steps
- affected actor, branch, level, and workflow stage when known
- severity

## Milestones

Recommended milestones:

- `M1 - Stabilize Local Development`
- `M2 - Core Workflow QA`
- `M3 - Security and Evidence Hardening`
- `M4 - Demo Readiness`
- `M5 - Thesis Defense Polish`

## Working Agreement

- One issue should describe one deliverable.
- Pull requests should link issues with `Closes #123` or `Refs #123`.
- Changes to routing, workflow, security, evidence, or audit behavior must reference the relevant docs.
- Do not track onboarding, offboarding, payroll, termination, or full employee lifecycle items because they are out of scope.
- Keep dummy or synthetic data only in issues, tests, screenshots, and demos.
