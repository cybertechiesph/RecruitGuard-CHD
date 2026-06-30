# Client Demo Feedback — Change Spec & Backlog

Source: initial online system demo with DOH–CHD CALABARZON (Ma'am Marz, section
chief; Ms. Kristine "Tin", secretariat). Plus two real artifacts they provided:
the **Comparative Assessment (CAR) template** and the **HRMPSB Interview Form**.
This spec captures decoded facts, locked design decisions, and the prioritized
change list. Defaults come from their real documents so we are NOT blocked on
unknowns — they tune the dials later.

---

## 1. Decoded source documents (the real basis)

### CAR — exact scoring model (from the template's embedded formulas)
One table, **all candidates for a vacancy as rows**, ranked high→low. Columns:
- **ETE (Education, Training, Experience) — 40%**: `ETE_EPS = ETE_Rating × 40`
- **Examination — 20%**: `Exam_Rating = Gen.Ability×0.60 + Technical×0.40`; `Exam_EPS = Exam_Rating × 0.20`
- **Interview — 40%**: `Interview_AVE = average(5 raters)`; `Interview_EPS = AVE × 0.40`
- **OVERALL** = `ETE_EPS + Exam_EPS + Interview_EPS`  (+ **Remarks** column)
- EPS = "Equivalent Point Score". Header carries QS (Position, Salary Grade, Level,
  **Plantilla Item No.** / Education, Eligibility, Training, Experience).
- Signatories: Prepared by Secretariat; Rated by Chairperson, Vice-Chair,
  Admin Officer V/HRMO III, 2nd-Level Rep, Ad Hoc Member (the 5 interview raters).

### Interview Form — competency rating sheet template
Competencies in 3 groups, each with Weight + Score, summed to TOTAL:
- **CORE** (standard): Exemplifying Integrity, Professionalism, Service Excellence
- **ORGANIZATIONAL** (standard): Effective Communication, Effective Interpersonal
  Relations, Organizational Awareness & Commitment
- **TECHNICAL** (varies per position): e.g. for AA III — Benefits/Compensation/Welfare
  Mgt., Data Recording & Reporting, Diversity Mgt., Government & Departmental Policies,
  Manpower Acquisition & Development, Performance Management Standards, Providing
  Support & Services
- Plus a "Technical/Functional Expertise" free-text field, Comments, Rated by, Date.
- Form note: "Expected competency proficiency varies depending on the position."

---

## 2. LOCKED design decisions (do not re-litigate)

**Flexibility model — encode the shape, parameterize the values, seed defaults.**

- **Interview rating sheet = secretariat-editable per-position template.** She can
  add/remove/rename competencies, group them (Core/Org/Technical), set the scale and
  per-competency weight. System auto-totals and normalizes to the 0–100 the CAR uses.
- **CAR = secretariat-editable TEMPLATE + INPUTS; results are COMPUTED (option 1, confirmed).**
  - Editable template: component weights (default **40/20/40**), exam sub-weights
    (default **60/40**), labels, components.
  - She edits **inputs** (ETE rating, exam Gen/Technical scores, interview competency
    scores); system computes overall/EPS/ranking.
  - **No manual overwrite of computed scores.** (If ever needed later: a controlled,
    reason-required, audited override — not open editing.)
- **Weights: one global configurable set** (default 40/20/40 & 60/40). Revisit
  per-level/per-position only if the client says weights differ.
- **ETE rating: manual input** by the secretariat (we don't have the points rubric;
  don't fake one). Optional structured rubric later.
- **Guardrails:** validate component weights sum to 100%; audit all template/input
  edits; **snapshot template + values when a CAR is finalized** (immutable — matches the
  cybersecurity "no tampering finalized stages" objective); **lock the template for a
  vacancy once its assessment starts.**
- **Scope boundary:** structured configurability (editable template + inputs), NOT a
  free-form spreadsheet editor.

---

## 3. Assumption-fixes — things we built WRONG (highest priority)
- **PSB ratings are visible to the secretariat** — we built them hidden/undisclosed. Unhide.
- **Exam scoring**: it's **General 60% + Technical 40%** (weighted), and exam is **20%**
  of the overall — NOT a simple average. (Our ExamRecord uses technical/practical.)
- **CAR shape**: one table / all candidates / ranked — we built per-applicant cards.
- **Exam "administered by"**: Plantilla = HRMS/secretariat administers (end-user only
  *provides* the exam); end-user-administers is the COS case.
- **Overall result** must follow the CAR 40/20/40 formula (align our preliminary score).

---

## 4. Big builds
- **CAR redesign**: one-page per-vacancy table, all candidates, ranked high→low, with the
  decoded formula; editable template + computed results; **Remarks** (per-applicant from
  PSB rating sheet + an appointing-authority remarks field); remove "recommendation" and
  "ranking note"; QS + item number in header. Seed defaults from the template.
- **Interview competency rating-sheet builder**: per-position template (Core/Org standard
  defaults + editable Technical), secretariat-editable; HRMPSB members score online
  (tablets coming) AND/OR secretariat encode/edit window; auto-total + normalize → feeds
  the CAR interview column. (Confirm: HRMPSB-self-service vs secretariat-encode as the
  first target.)
- **Per-vacancy document checklist**: at vacancy creation, internal user picks which
  documents are included and required/optional; reflected in applicant step-1, upload, and
  screening. Replaces the fixed per-branch list. Pre-check the standard set as required.

---

## 5. Applicant-portal tweaks
- Remove the hard 14-day publication restriction — free date (default/suggest 14).
- Remove the "Government recruitment… typically 4–6 weeks" line on the tracking page.
- Branding: drop "RecruitGuard" from the header → footer; lead with **DOH–CHD CALABARZON**
  + "Applicant Portal".
- Hide the system-generated entry ID from applicants; show the **plantilla item number**
  (COS = none).
- Help desk → **8-249-2000 loc 4477 / hrms@ro4a.doh.gov.ph**.
- Level numbers → **Roman numerals** (Level I / II).

## 6. Internal-portal tweaks
- Document review: **View option alongside Download**.
- Exam: capture **General + Technical**; **overall auto-computes** (Gen×0.6 + Tech×0.4).
- Interview: **separate explicit buttons** — "Notify applicant" and "Notify HRMPSB panel"
  (not baked into Save session); re-notify on reschedule. Panel email = notice of meeting.
- Document-review statuses (Meets/Absent/Request Resubmission) admin-editable.
- Exam/interview notices carry **"cannot attend → submit letter of intent to reschedule"** instructions.
- **Per-position archive/retrieval**: one file per position with all records + decision (audit).

## 7. Vacancy-creation tweaks
- Remove the Google-researched fields (OS code, occupational group/service, planning
  service, reference status). (Place-of-assignment considered, then dropped.)
- **Entry/control code: admin-input, not auto-generated** — plantilla = item number,
  COS = N/A (required or it won't save).
- **Publication period editable** (default/recommend 14 days).

---

## 8. New models/fields implied
- `PositionPosting.item_number` (plantilla; admin-input).
- Per-vacancy **document-requirement config** (which docs, required/optional).
- **Competency rating-sheet** model (per-position template: competencies, groups, scale,
  weights) + per-rater scores.
- **CAR template config** (component weights, exam sub-weights, labels) — configurable,
  versioned/snapshotted.
- **ETE rating** input on the assessment.

## 9. Open clarifications (low urgency — defaults chosen)
- ETE rating: manual input (default) vs points rubric.
- Interview per-competency scale (she mentioned **1–4**) + total→100 normalization.
- Weights: one global (default) vs per-level/per-position.
- Rating sheet target: HRMPSB self-service online vs secretariat-encode first.
- System **ownership/turnover** to the client (Marz asked) — non-technical, needs an answer.

## 10. Relationship to existing code
- On `main`: workflow-notifications feature (ExamSchedule, exam/interview applicant
  notices, gap B/C). The **exam-scoring** and **interview-notify** changes here MODIFY it.
- Branch `feat/internal-uiux-safe-tier`: A1–A4/A8 + header done; A5/A6/A7 pending.
- This demo-feedback workstream is largely NEW and the bigger effort — recommend a
  dedicated branch and (given high context) a fresh implementation session using this spec.

## Client will still send
Sample CAR (blank — have one), competency dictionary + interview form (have AA III),
positions list (have), item numbers (with each posting), HRMS email (have).
