# RecruitGuard-CHD — Per-task Scripts

Three task scripts, one per session. Each includes the verbatim task statement to read aloud, the seeded data, the expected happy path, signal cues to watch for, and success/struggle/fail criteria.

**Do NOT show this document to the participant.**

---

## Session 1 — Applicant flow *(20–25 min)*

### Participant profile
Someone who fits the DOH-CHD applicant demographic — entry-level health worker, OFW returnee, or recent graduate. Has filled out government forms before. Filipino primary language fine; should be comfortable enough in English to read job postings.

### Pre-session setup
- Open the public portal landing page: `http://localhost:8000/apply/`
- Have a folder on the desktop labelled **"Test documents"** with these PDF files (any dummy content):
  - `01-pds.pdf` (Personal Data Sheet stand-in)
  - `02-tor.pdf` (Transcript of Records stand-in)
  - `03-diploma.pdf` (Diploma stand-in)
  - `04-application-letter.pdf`
  - `05-work-experience.pdf`
  - `06-eligibility.pdf`
- Make sure you can intercept the verification email (use a Mailtrap or local email backend that prints to console; the participant must not actually need a personal email).

### Task statement *(read aloud)*

> Today, imagine you saw a job posting at the DOH Center for Health Development CALABARZON for an **Administrative Aide I** position. You decided you want to apply. Here on this computer is the application website. You have the documents you need on the desktop in a folder called "Test documents".
>
> Your task is simple: **submit an application for the Administrative Aide I posting.** Use any documents from the folder. Use any name, email, and contact details you want — just make them realistic.
>
> Take your time. Remember to tell me what you are thinking out loud. I won't help you, but if you really cannot continue, just say so.
>
> Start whenever you're ready.

### Expected happy path
1. Browse Job Openings list
2. Click into the Administrative Aide I posting
3. Click "Apply for this position"
4. Wizard Step 1 — Information: name, email, contact, address
5. Wizard Step 2 — Documents: upload from desktop folder
6. Wizard Step 3 — Review and confirm
7. Submit
8. Receive a confirmation page or message
9. Enter the OTP code sent to email
10. See "submission received" success state

### Signal cues to watch for

| Signal | What it means |
|--------|---------------|
| Confused about which posting to click | The job listing card needs more visual hierarchy |
| Pauses on "Apply for this position" button | Button copy may not be clear enough |
| Skips reading the wizard step descriptions | Subheadings may be too small or unclear |
| Tries to upload to a wrong slot | Requirement labels may be ambiguous |
| Searches for a "next" button when there isn't one | Wizard navigation cues are weak |
| Re-reads the OTP page after the code arrives | OTP flow is unclear about where the code comes from |
| Asks "Did it submit?" | Success confirmation isn't loud enough |
| Reads labels in Tagalog or asks for translation | i18n is needed |

### Success / Struggle / Fail criteria

- **Success:** Reached the OTP-verified "submitted" state without ever asking the moderator a question they had to refuse to answer.
- **Struggle:** Reached the end but with 3+ hesitations >5 seconds, or 1+ moment of "I don't know what to do."
- **Fail:** Did not reach submission, or submitted to the wrong posting, or submitted with the wrong document in a slot.

---

## Session 2 — Secretariat flow *(20–25 min)*

### Participant profile
Actual office worker who screens or processes applications at DOH-CHD CALABARZON today. Has done the paper workflow before.

### Pre-session setup
- Pre-logged in as `secretariat` (the moderator does this before the participant sits down).
- Workstation is at `http://localhost:8000/internal/workflow/queue/`.
- Use seeded case `RG-COS-test-screening`. It should be visible in the queue at Secretariat screening stage, with documents uploaded.
- If the seeded case has been used in a prior session, re-seed via `python manage.py seed_e2e_test_cases`.

### Task statement *(read aloud)*

> You are the Secretariat at DOH-CHD. An application just came in for the **Administrative Aide I** position under Contract of Service. Your job today is to **screen the application and send the case to the HRM Chief.**
>
> Screening means: check that the documents are complete, decide whether the applicant is qualified, and write your recommendation. Then send the case forward.
>
> Use the system the way you would on a real day. Talk out loud as you go. I won't help.
>
> Start whenever you're ready.

### Expected happy path
1. See the case in "My queue"
2. Click into it
3. Read the applicant submission panel
4. Open the screening wizard
5. Step 1 — Review each document; set status (Present / Absent / Flag)
6. Step 2 — Mark application as Complete or Incomplete
7. Step 3 — Mark applicant as Qualified or Not qualified; finalize
8. Click "Send to HRM Chief"
9. Fill in handoff reason and submit

### Signal cues to watch for

| Signal | What it means |
|--------|---------------|
| Looks for a separate "review documents" link | The wizard step terminology is unclear |
| Wants to scroll back to step 1 after starting step 2 | Wizard navigation needs better backward affordance |
| Says "I don't know what to write here" on remarks | Help text isn't enough; need examples |
| Misses the autosave indicator | "Saved" feedback is too subtle |
| Doesn't notice the SLA badge | The badge is decorative-looking, not functional |
| Pauses on "Send to HRM Chief" — looks for "Approve" or "Endorse" | Button label doesn't match office vocabulary |
| Reads the handoff modal carefully and looks uncertain | Modal copy needs work |
| Asks what happens after they send | Post-submission communication unclear |

### Success / Struggle / Fail criteria

- **Success:** Finalized screening and sent the case forward, with remarks that match the case state.
- **Struggle:** Got to "Send" but with several "wait, did I do this right?" moments.
- **Fail:** Marked wrong status, submitted with empty required fields and got blocked, or could not finalize.

---

## Session 3 — HRM Chief flow *(20–25 min)*

### Participant profile
Someone with decision-making authority on appointments — the actual HRM Chief, or a senior administrator who has reviewed/approved appointments before.

### Pre-session setup
- Pre-logged in as `hrm_chief`.
- A seeded case must be at HRM Chief Review with the earlier review steps already complete. For a clean run, use reference number `RG-COS-test-decision`. Do not rely on the app ID; it changes after each reseed.
- Workstation at `http://localhost:8000/internal/workflow/queue/`.

### Task statement *(read aloud)*

> You are the HRM Chief at DOH-CHD. A Contract of Service application for **Administrative Aide I** is ready for your final review. The earlier review steps are already complete.
>
> Your task is to **review the submission packet and record your decision**. Choose **Selected** if the applicant should move forward to completion paperwork. Choose **Not selected** if the applicant should not move forward. Please add short notes explaining your decision.
>
> Make the decision that seems right based on what you see. There is no trick and no wrong answer for this test. Use the system the way you would on a real day, and talk out loud while you work.
>
> Start whenever you're ready.

### Expected happy path
1. See the case in "My queue" with task pill "Decision"
2. Click into it
3. Step 1 — Review the submission packet
4. Step 2 — Record the decision (Selected / Not selected + notes)
5. Submit
6. See confirmation / next-step state

### Signal cues to watch for

| Signal | What it means |
|--------|---------------|
| Scrolls past the packet without reading | Packet structure isn't engaging enough |
| Tries to click on a packet item to "open" it | Items look interactive when they aren't (or should be and aren't) |
| Asks "Where do I see the Secretariat's notes?" | Audit trail / case timeline isn't surfaced enough |
| Hesitates on Selected vs Approved vocabulary | Action verbs don't match office mental model |
| Wants to do partial-approve or conditional-approve | Decision options may be too coarse |
| Looks for a "Return to Secretariat" option | HRM Chief may still need a return path or clearer policy explanation |
| Doesn't realize "Not selected" ends the application | Not-selected warning needs to be clearer |
| Drafts a long remarks message | Remarks textarea may need formatting guidance |
| Asks what the applicant will see after this | Downstream visibility unclear |

### Success / Struggle / Fail criteria

- **Success:** Made a clear decision, submitted, saw confirmation.
- **Struggle:** Decision made but with backtracking or "wait, what does this button do" moments.
- **Fail:** Could not record a decision, or submitted a decision they did not intend.

---

## Data to bring back from each session

After each session, the moderator should have:

- Filled-out observation template (one per session)
- Verbatim quotes (5+ per session is ideal)
- Time-to-completion (start of task → submit) in minutes
- Number of moderator interventions ("I'm stuck") — should be 0–1
- Subjective confidence rating from participant (1–5)
- Parking lot of feature requests they made (separate from issues)

We will combine all three sets in the synthesis step.
