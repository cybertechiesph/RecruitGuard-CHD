# Operations runbook

For whoever runs the DOH–CHD CALABARZON Recruitment Portal server. One page on purpose: what to
back up, how to restore, where the logs are, and how you hear about errors. The daily jobs are
in the README ("Scheduled jobs").

## What holds the data

| Piece | Where | Why it matters |
|---|---|---|
| Database | PostgreSQL (`POSTGRES_DB`, usually `recruitguard_chd`) | Every case, record and audit entry, **and every uploaded or generated file** (applicant documents, exam and interview evidence, CAR PDFs), stored encrypted inside the database. |
| Secrets | the server's `.env` | `DJANGO_SECRET_KEY`, `EVIDENCE_ENCRYPTION_SECRET`, `INTERNAL_MFA_OTP_HASH_SECRET`, database and email passwords. |

So one database dump holds everything; there is no separate files folder to copy (`media/` is
not used for evidence).

**The evidence key is part of every backup.** The files inside the database are encrypted with
`EVIDENCE_ENCRYPTION_SECRET`. A dump restored without that exact value opens, but none of its
files can be read, and changing the value makes every stored file unreadable. Keep a copy of the
`.env` (or at least that value) somewhere other than the server, sealed, with the office. Never
rotate it.

## Nightly backup

Run as the account that owns the project, from cron, once a night:

```bash
BACKUP_DIR=/var/backups/recruitment/$(date +%F)
mkdir -p "$BACKUP_DIR"
pg_dump --format=custom --no-owner "$POSTGRES_DB" > "$BACKUP_DIR/database.dump"
find /var/backups/recruitment -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

Copy each night's folder off the server (another machine or the office's storage). Keep 30
nightly copies and one per month for a year; rejected applications are normally kept about a
year (client interview).

## Restore (and test it every three months)

1. Install the project at the same commit, with the **same** `.env` (same `EVIDENCE_ENCRYPTION_SECRET`).
2. Create an empty database, then: `pg_restore --no-owner --dbname "$POSTGRES_DB" database.dump`
3. `python manage.py migrate` (applies any newer migrations), then start the service.
4. Check: sign in, open a closed case, open one of its files (proves the key matches), export it.

Do the whole restore on a spare machine once a quarter. A backup that has never been restored
is a hope, not a backup.

## Logs

- Set `DJANGO_LOG_FILE=/var/log/recruitment/app.log` in `.env` to keep a log file. It rotates at
  10 MB and keeps 5 old files. Passwords, codes, tokens and cookies are redacted before writing.
- Without it, logs go to the service's console (for example `journalctl -u <service>`).
- A "Workflow conflict on …" warning means two people saved the same step at the same moment;
  the second was told to reload. Frequent ones are worth a look, single ones are normal.

## Hearing about errors

Set `DJANGO_ADMINS="Name <email>, Other Name <email>"` in `.env`. Every server error (HTTP 500)
then emails those people, with sensitive values redacted. `SERVER_EMAIL` sets the sender
(defaults to `DEFAULT_FROM_EMAIL`). Without `DJANGO_ADMINS`, errors are only in the log.

## Before going from demo to real use

- The demo droplet's update script **reseeds the database** (`seed_demo_data --force`) on every
  deploy. Remove that step before real data goes in, or every deploy wipes the office's records.
- Rotate the secrets that were used during the demo **except** `EVIDENCE_ENCRYPTION_SECRET`
  (start the real system with a fresh one before the first real upload, then never change it).
- Turn on the nightly backup and the error emails above, and do one test restore.
