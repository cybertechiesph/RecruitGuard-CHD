# RecruitGuard-CHD — Moderated User Testing Protocol

**Format:** In-person at DOH-CHD CALABARZON office
**Sessions:** 3 (one per role perspective)
**Duration:** 45 minutes per session
**Style:** Steve Krug discount usability testing — moderator reads the script, does not help, asks "what are you thinking?"

---

## Goal

Find where real users hesitate, misread, or give up. We are NOT validating that the build is correct — we already know that. We are finding the friction we cannot see ourselves.

A successful session produces 3–6 honest observations per task. A "good" session that finds nothing usually means the user was being polite. Read body language.

---

## Before the day

### Workstation prep

Use a dedicated test workstation, **not** the participant's daily one. Why: production accounts make people cautious; staging makes the test feel safe.

Required setup:

- **Browser:** Chrome or Edge on the office Windows machine. Latest version. Default zoom 100%.
- **URL:** Staging URL or `http://localhost:8000` if running the dev server locally.
- **Seeded users:** `secretariat`, `hrm_chief`, `appointing_authority` — already in the dev DB.
- **Seeded cases:** Created by `python manage.py seed_e2e_test_cases`. Use reference numbers in the queue or case search; app IDs change after every reseed. See Codex backend asks doc, item V-seed, for the full list.
- **Pre-session login:** Moderator pre-logs the workstation into the correct role for each session. Participant should *not* see the login screen — that's not part of the test.
- **Test documents folder:** A folder on the desktop labelled "Test documents" containing 5–7 PDF dummy files (PDS, TOR, Diploma, etc.) the applicant participant can pick up.

### Environment

- Quiet conference room or empty office. Close the door.
- Two chairs side by side facing the workstation. One for participant, one for moderator.
- Optional: one chair at the back of the room for an observer (silent).
- Water for the participant.
- Notebook + pen for the moderator (and observer, if present).

### Recording

- **Audio:** Recommended. Get verbal consent at the start; if they decline, take written notes instead.
- **Screen recording:** Optional but useful. OBS or Windows built-in `Win+G` game bar.
- **Photos:** No photos of participants without separate written consent.

### Pacing

- Schedule sessions 60 minutes apart, not back-to-back. Moderator needs 15 minutes to refresh notes and reset the workstation (clear test artifacts, reseed if needed).

---

## Session structure (45 minutes)

| Time | Activity |
|------|----------|
| 0–3 min | Welcome + consent |
| 3–8 min | Background interview |
| 8–13 min | Warm-up + system orientation |
| 13–38 min | Task (single task, 20–25 min) |
| 38–43 min | Debrief |
| 43–45 min | Thank-you + close |

---

## Script

### 1. Welcome and consent *(read aloud, verbatim)*

> Thank you for coming in today. My name is [____] and I work on the RecruitGuard-CHD project.
>
> I want to be very clear about one thing: **today we are testing the system, not you.** There are no wrong answers. If something is confusing, that's a problem with the system, not with you. The more honestly you tell me what you find confusing, the more we can fix.
>
> What you'll do today is try to complete one task using the system, while telling me out loud what you're thinking — like a sports commentator. If you hesitate, tell me what made you hesitate. If you click something and it surprises you, tell me what you expected. There's no script for you to follow — just be honest.
>
> I won't help you during the task, even if you get stuck. That's not because I'm being mean — it's because if I help, I learn nothing about what's hard. If you really cannot continue, just say "I'm stuck" and we'll talk about it.
>
> The session will take about 45 minutes. I may take notes while you talk. **Is it okay if I record the audio?** [If yes, start recording. If no, take handwritten notes.]
>
> Any questions before we begin?

### 2. Background interview *(5 minutes)*

Ask, conversationally:

- How long have you worked at DOH-CHD? *(or for applicant: tell me about yourself — work, training)*
- What's your usual day like?
- What kind of computer or phone do you use the most?
- *(For internal staff)* Tell me about how applications get handled in your office today, without the system. How does the paper flow?
- *(For applicant)* Have you applied for government work before? What was that like?

Goal: relaxes the participant; surfaces context that will help interpret what we see during the task.

### 3. Warm-up *(5 minutes)*

Open the system to the right starting page. Say:

> Here's the system. Before we start the task, just look at this screen for a minute and tell me — what do you think this is for? What can you see here? What would you click first?

Listen. Take notes on first impressions. Do NOT correct misunderstandings — those are data.

### 4. The task *(20–25 minutes)*

Read the task script aloud, hand the participant any test materials they need, and then **be quiet.** Sit back. Take notes.

Task scripts are in `user-testing-tasks.md`. One task per session:

- Session 1 — Applicant: Submit an application
- Session 2 — Secretariat: Screen and endorse a case
- Session 3 — HRM Chief: Review and decide on an endorsed case

Watch for:

- **Hesitations** — pauses longer than 3 seconds
- **Misreads** — they say one label but click another
- **Backtracking** — clicking back, undoing, re-reading
- **Errors** — they do something the system doesn't accept
- **Surprise** — facial expression, "huh", "wait, what?"
- **Quotes** — verbatim things they say. Quotes are gold.

If they go silent for a while, prompt softly: *"What are you thinking right now?"* — that's the only intervention.

If they ask you a question: *"What would you do if I weren't here?"* Then stay quiet.

### 5. Debrief *(5 minutes)*

After the task is done (success or fail), ask:

- On a scale of 1–5, how confident are you that you completed the task correctly?
- What part was the hardest? Easiest?
- Was there anything you expected to find that wasn't there?
- Was there any word or label that you weren't sure about?
- If you had to do this task again next week without me, do you think you could?
- *(For Filipino-context relevance)* Sa palagay mo, kung Tagalog ang gamit na salita sa system, mas madali ba? *(In your opinion, would Tagalog labels be easier?)*

Take quotes verbatim.

### 6. Close *(2 minutes)*

> Thank you so much. This has been really helpful. What you told me will directly change how we build this. If you have any other thoughts later, you can tell [your point of contact] and they'll pass it along to me.

If there's an incentive (snack, certificate, token), give it now.

---

## Moderator rules of thumb

- **Talk less, listen more.** Aim for the participant to do 80% of the talking.
- **Don't defend the system.** If they criticize a design, write it down. Do not explain why it's that way.
- **Don't lead.** Avoid "Is the button hard to find?" — ask "What are you looking for right now?"
- **Silence is fine.** Long pauses feel awkward but they're when participants are thinking.
- **Note what they don't say.** "I think it's fine" with a frown is a finding.
- **Stop on time.** 45 minutes max. If they want to continue, do not — fatigue produces unreliable data.

---

## Observer rules

If an observer is in the room:

- Sit at the back. No phone, no laptop typing.
- Take notes on paper, silently.
- Do NOT make eye contact with the participant during the task.
- Do NOT speak during the session except to greet on arrival.
- Save your questions for the post-session moderator debrief.

---

## After the session

1. **Immediately:** spend 10 minutes reviewing your notes while the session is fresh. Fill in the observation template (`user-testing-notes-template.md`).
2. **Same day:** add direct quotes verbatim from memory or recording.
3. **After all 3 sessions:** synthesize using `user-testing-synthesis-template.md`.

---

## What this protocol is NOT

- It is not formal usability research with statistical claims.
- It is not for measuring task completion rates or time-on-task — though we note them.
- It is not for collecting feature requests. Participants will ask for things; note them but they go into a separate parking lot.
- It is not a system tour. Do not walk the participant through the UI before the task.

Three sessions, honestly run, will tell us what to fix before we ship.
