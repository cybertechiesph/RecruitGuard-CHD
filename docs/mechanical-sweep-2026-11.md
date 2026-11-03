# Mechanical sweep — November 2026

The user sat down to test the portal and found a link that bounced within minutes. That raised
a fair question: why did 901 tests and six walkthroughs miss it? The walkthroughs covered what
each role does; nobody had followed every link. This sweep did: every role, every page, every
link and every form, by machine.

## Status: done; all findings fixed

## 1. What was swept

| | Pages | Form presses | Where |
|---|---:|---:|---|
| Demo database, five staff roles and the public pages in both languages | 604 | 828 | Each request rolled back; mail faked |
| Cases built at every step (Plantilla Level 1 and 2, COS, open pool, returned documents, closed with a date ahead) | 535 | 2,604 | Test database |
| Routes no page offered, posted to directly as every role on every case | | 2,575 | Test database |
| The applicant journey from start to returned documents, English and Filipino | 118 | 186 | Test database |
| **Total** | **1,257** | **6,193** | |

Every form was sent blank, with bad data and with good data. After each press the page was
reopened, so an action that left a case unopenable would have shown.

## 2. What it found

| # | Found | Who it hit | Fix |
|---|---|---|---|
| 1 | **Crash:** the printable audit report failed whenever a sign-in with no person attached (a mistyped username) was in the report. | System Administrator | Such entries print as "System". |
| 2 | **Crash:** editing a Verified position reference and clearing a core detail (or sending the form blank) gave a server error instead of a message. | System Administrator | The status is worked out on save, so the form no longer checks the old one. Clearing a core detail saves the reference as Incomplete. |
| 3 | **Bounce:** "Coming up" and the Schedule listed a finished case's future date, and its link sent the user straight back. | Secretariat, HRM Chief, panel member | A finished case has nothing coming up. An event on a vacancy with no open batch for that user links to the case. |
| 4 | **Wording missed in I2:** "Recruitment entry status updated to…". | Secretariat, HRM Chief | "Vacancy status updated to…". |

Nothing else crashed, no link was dead or forbidden, and no action left a case unopenable.

Flags checked and dismissed: buttons that need no typing (send reset, resend, deactivate),
drafts that may be saved blank, a batch absence whose notes are optional, and reminder emails,
which are recorded in the notification log rather than the audit log.

## 3. What stays behind

`recruitment/tests/test_crawl.py` runs in the suite from now on: it builds a case at every step,
crawls as all five roles and the public, and fails on any crash, dead link, forbidden link or
bounce. `SweepFindingTests` pins the four findings.

## 4. What this does not prove

- **It finds what breaks, not what confuses.** A page can work perfectly and still puzzle a
  reader. Only a person can find that.
- **It finds crashes, not wrong answers.** A form that accepts good data and records the wrong
  thing passes. The 900 behaviour tests and the walkthroughs cover that; nothing covers it
  completely.
- **Good-data presses used guessed values**, so some forms stopped at validation and went no
  deeper. The behaviour tests go deeper on those.
- **Fourteen form routes appear on no page in these states** (old per-case routes the batch
  screens replaced, and a few that need a very particular state). They were posted to directly
  and refused or handled cleanly, not exercised as a user would.
- **Sign-in and password pages** were not pressed; they have their own tests.
- **The browser itself:** the sweep reads the HTML. It does not run page scripts, so a button
  that only works through JavaScript was tested through its form, not its click.
