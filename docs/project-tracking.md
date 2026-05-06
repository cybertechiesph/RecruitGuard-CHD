# Project Tracking Guide

RecruitGuard-CHD uses GitHub Issues as the free source of truth for development tracking. Keep planning close to the repository so issues, commits, pull requests, tests, and release notes stay connected.

## Recommended Views

Create one GitHub Project for the repository named `RecruitGuard-CHD Development` with these views:

- `By FRS Module`: Board or table grouped by the `Module` field
- `Module Table`: Table sorted by `Module`, priority, and updated date
- `Workflow Board`: Board grouped by `Status`
- optional `Roadmap`: grouped by milestone or phase

Project URL:

- `https://github.com/users/cybertechiesph/projects/1`

Recommended status values:

- Backlog
- Ready
- In Progress
- Code Review
- Testing
- Blocked
- Done

## FRS Module Review View

The professor-facing view should be grouped by functional module, not only by task status.

Recommended setup:

- view name: `By FRS Module`
- layout: board or table
- group by: `Module`
- visible fields: `Status`, `Assignees`, `Labels`, `Linked pull requests`
- filter: do not filter to open issues only; FRS modules marked `Done` may be closed by GitHub's built-in Project workflow while remaining visible in the Project

The repository includes `.github/workflows/project-module-sync.yml`, which creates and maintains the project `Module` field. It maps issues and pull requests to the closest FRS module using the issue title, body, and labels.

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

## Automation

The repository includes `.github/workflows/project-tracking.yml`.

It automatically labels new or edited issues based on:

- issue template type
- priority text such as P0, P1, P2, or P3
- branch text such as Shared, Plantilla, or COS
- selected core module
- risk keywords such as security, audit, routing, evidence, and workflow

It can also automatically add new issues and pull requests to the GitHub Project. GitHub Projects are outside the repository permission boundary, so the built-in `GITHUB_TOKEN` is not enough for user-owned Projects. Add a repository secret named `ADD_TO_PROJECT_PAT` with a classic personal access token that can write to the project.

Recommended classic token setup:

- token type: classic personal access token
- expiration: 90 days or 180 days
- scopes for this public repository: `project`
- optional fallback scope if GitHub reports repository access errors: `public_repo`
- avoid broad scopes such as `repo`, `admin:repo_hook`, `workflow`, `delete_repo`, `admin:org`, or `user`

After the secret is added, new issues and pull requests should be added to `https://github.com/users/cybertechiesph/projects/1` automatically.

The `.github/workflows/frs-module-tracker-sync.yml` workflow uses the same secret to:

- create or update one GitHub issue for each of the 15 FRS modules
- set each FRS module issue's project `Module` field
- set each FRS module issue's project `Status` field from the implementation review
- close and archive non-FRS starter/setup/demo tracking items

The `.github/workflows/project-module-sync.yml` workflow keeps FRS module issues synced after later edits. It is intentionally limited to issue titles that start with `[FRS Module ` so unrelated setup, demo, or deployment items do not re-enter the professor-facing tracker.

The `.github/workflows/project-tracking.yml` workflow only handles issue labeling. It no longer auto-adds every issue or pull request to the Project because the active tracker is module-based.

The module sync uses the same secret to:

- create the `Module` project field if it does not exist
- add FRS module issues to the project if missing
- assign each item to the matching FRS module
- keep future FRS module issue values synced
