# RecruitGuard-CHD

RecruitGuard-CHD is a secure-by-design Django recruitment management prototype for DOH-CHD CALABARZON. The repository currently implements one shared platform, one shared database, and one shared workflow engine for two recruitment branches: Plantilla and COS.

The project is split into two portals:

- Applicant Portal at `/apply/`
- Internal Portal at `/internal/`

## Current project status

The repository is already beyond a starter scaffold. The current codebase includes models, forms, views, templates, services, migrations, admin registrations, and automated tests for the thesis-aligned recruitment workflow.

Implemented now:

- Internal identity and access control for the locked actor set: Applicant, Secretariat, HRM Chief, HRMPSB Member, Appointing Authority, and System Administrator
- Protected internal login, logout, password change, and internal account management
- Controlled Position Reference catalog management plus recruitment entry management for Plantilla and COS
- Public applicant intake with required document uploads, OTP verification, receipt generation, and status lookup
- Branch-aware workflow handling for Plantilla and COS
- Level-aware routing with `Level 1 -> Secretariat` and `Level 2 -> HRM Chief`
- Secretariat restriction for Level 2 cases, with controlled and audit-logged override support
- Screening, examination, interview scheduling, direct ratings, fallback interview uploads, deliberation, and decision-support handling
- Comparative Assessment Report generation for Plantilla, including entry-scoped versioning
- Final decision recording, completion tracking, controlled reopen, and case closure
- Notification logging and email dispatch for submission acknowledgment, selected, non-selected, checklist, and reminder notices
- Evidence Vault storage with SHA-256 integrity digests, AES-256-GCM encrypted payload storage, archive toggling, and controlled download
- Recruitment audit logging, routing history, protected-record access logging, and controlled export bundle generation

Current repository boundaries:

- Recruitment handling only
- Full onboarding, offboarding, payroll, termination, and full employee lifecycle functions are out of scope
- PostgreSQL is supported through environment variables, but SQLite is currently used automatically for local bootstrap if PostgreSQL settings are left blank
- OTP and notification email flows require valid SMTP credentials in `.env`

The automated test suite currently includes 94 Django tests covering identity, intake, routing, workflow, evaluation, evidence, notification, completion, decision, audit, and export paths.

## Locked stack

- Python 3
- Django
- Django Templates
- Bootstrap
- PostgreSQL
- Gmail SMTP for OTP and email notifications
- `hashlib` for SHA-256
- `cryptography` for AES-256-GCM
- ReportLab
- Python `zipfile`

## First-time setup

### Windows PowerShell

```powershell
git clone https://github.com/strongestleugim/RecruitGuard-CHD.git
cd RecruitGuard-CHD
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` before continuing.

Minimum values to review in `.env`:

- `DJANGO_SECRET_KEY`
- `APPLICATION_OTP_HASH_SECRET`
- `EVIDENCE_ENCRYPTION_SECRET`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_FROM_EMAIL`

Optional local-development database behavior:

- Leave `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` blank to use SQLite locally
- Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and `POSTGRES_PORT` to use PostgreSQL

Continue with database setup and the first internal admin account:

```powershell
python manage.py migrate
python manage.py createsuperuser --username admin --email admin@example.com
python manage.py shell -c "from django.contrib.auth import get_user_model; User = get_user_model(); user = User.objects.get(username='admin'); user.role = User.Role.SYSTEM_ADMIN; user.save()"
python manage.py check
python manage.py test recruitment
python manage.py runserver
```

### macOS / Linux

```bash
git clone https://github.com/strongestleugim/RecruitGuard-CHD.git
cd RecruitGuard-CHD
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before continuing, then run:

```bash
python manage.py migrate
python manage.py createsuperuser --username admin --email admin@example.com
python manage.py shell -c "from django.contrib.auth import get_user_model; User = get_user_model(); user = User.objects.get(username='admin'); user.role = User.Role.SYSTEM_ADMIN; user.save()"
python manage.py check
python manage.py test recruitment
python manage.py runserver
```

## How to run the server after first-time setup

### Windows PowerShell

```powershell
cd C:\path\to\RecruitGuard-CHD
.\.venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py runserver
```

### macOS / Linux

```bash
cd /path/to/RecruitGuard-CHD
source .venv/bin/activate
python manage.py migrate
python manage.py runserver
```

Open these URLs after the server starts:

- Applicant portal: `http://127.0.0.1:8000/apply/`
- Applicant status lookup: `http://127.0.0.1:8000/apply/status/`
- Internal login: `http://127.0.0.1:8000/internal/login/`
- Internal dashboard: `http://127.0.0.1:8000/internal/`
- Django admin: `http://127.0.0.1:8000/admin/`

## First internal account note

`createsuperuser` gives you Django admin access, but the internal portal only allows internal roles. That is why the setup commands above also set the account role to `system_admin`.

After logging into `/internal/login/` as that account, you can create the rest of the internal users from the protected internal user management screens.

Internal roles used by the system:

- `applicant`
- `secretariat`
- `hrm_chief`
- `hrmpsb_member`
- `appointing_authority`
- `system_admin`

## Useful entry points

- Public applicant portal: `/apply/`
- Public status lookup: `/apply/status/`
- Internal login: `/internal/login/`
- Workflow queue: `/internal/workflow/queue/`
- Evidence vault: `/internal/evidence/`
- Audit log: `/internal/audit/`
- Django admin: `/admin/`

## Notes for reviewers and first-time users

- If SMTP credentials are missing or invalid, the server will still start, but applicant OTP sending and email notifications will fail when triggered.
- The System Administrator role does not automatically get unrestricted case-content visibility; the repo intentionally keeps access server-side and workflow-aware.
- Django admin access is restricted to actual Django superusers. Normal internal operations are meant to happen in the internal portal.
