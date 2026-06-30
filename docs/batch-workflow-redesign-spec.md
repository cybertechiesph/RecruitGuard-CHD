# Batch Workflow Redesign — Source-of-Truth Spec

Status: **requirements locked** (decided with the client across the design session of
2026-06-30). This document is the authoritative description of *what* to build. The
implementation plan + code are produced by Claude Code from this spec.

Supersedes/extends: `docs/demo-feedback-change-spec.md` (the CAR/interview redesign) and the
earlier "Phase 8 vacancy-first dashboard = grouped view only" idea. See **§3 Reversals** —
this phase deliberately revisits two previously-locked decisions because the client's
operational reality requires it.

Branch: `feat/car-rating-redesign` (local, off `main`).

---

## 1. The core realization

The office runs recruitment **by batch, per vacancy** — not per individual case moving on its
own. Concretely:

- **Screening is individual.** The secretariat opens each application and reviews documents
  one by one.
- **Everything after screening is batch.** Once the whole vacancy's screening is finalized,
  the qualified candidates move forward **together** — exam, interview, and the decision all
  happen batch-by-batch for the vacancy.

The current system is a **per-case engine**: each application advances independently. This
phase makes the workflow **batch-aware** for both branches.

## 2. Engine approach (important constraint)

Build this as **batch ACTIONS layered over the existing per-case engine**, NOT a ground-up
lockstep/cohort rebuild. Each application's `RecruitmentCase` still exists underneath; the
secretariat drives them together via vacancy-level operations (batch screening finalize/lock,
batch exam entry, batch interview, etc.).

- Preserve FRS-locked routing: **Level 1 → Secretariat, Level 2 → HRM Chief** (Secretariat
  barred from L2). COS routing as today.
- Some pieces are already per-vacancy and should be reused: the interview competency rating
  sheet (`CompetencyRatingTemplate` is OneToOne `PositionPosting`) and the CAR
  (`ComparativeAssessmentReport`, per vacancy).
- Preserve the existing **exam-type-by-branch** rule: Plantilla = `TECHNICAL_PRACTICAL`
  (two scores), COS = `END_USER_ASSESSMENT` (one score). See `ExamRecord.required_score_fields`
  and `_exam_type_choices` in `forms.py`.

## 3. Reversals of earlier locked decisions (call these out to the client in the FRS revision)

1. **"Vacancy-first = grouped VIEW only, not a cohort engine"** → now we ARE building a batch
   workflow (still layered over per-case, not a true lockstep engine, but more than a view).
2. **"Remove in-system deliberation for Plantilla entirely"** → we are **re-adding a light
   recommendation step** for Plantilla (see §6). The deliberation *discussion* still happens
   off-system (on paper); only the recommendation *outcome* is captured.

---

## 4. Shared front-end — identical for BOTH Plantilla and COS

### 4.1 Document review / screening — INDIVIDUAL
- Secretariat opens each application and reviews each document (the per-vacancy document
  checklist already built in `PositionDocumentRequirement` / migration `0048` governs which
  docs are required vs optional).
- **Incomplete or flagged documents → request resubmission. Do NOT auto-reject.** ("Incomplete
  shall not be entertained" is the *final* outcome only if the applicant fails to resubmit by
  the deadline.)

### 4.2 Resubmission rework (changes current behavior)
- **Today:** requesting resubmission + finalizing a stage *removes the case from the
  dashboard/workflow.* This is wrong.
- **New:** the case **stays visible** in the vacancy's batch ("awaiting resubmission"). The
  applicant re-uploads through the existing **Track Application** page (token + emailed OTP —
  see `ApplicantStatusLinkView` / `issue_application_otp`). **Email only notifies** (link to
  Track Application); files never travel by email (privacy + audit + reuse of the encrypted
  vault and the existing file-readability checks).
- Resubmission is scoped to **the flagged documents only**. On re-upload, the case returns to
  the secretariat for **re-review of just those documents** (already-accepted docs stay
  accepted). It then counts toward "everyone reviewed."
- The secretariat sets a **deadline** per resubmission request (default: the existing 2-week
  default), shown as due / overdue.

### 4.3 Batch screening gate
- The vacancy's screening can be **finalized + locked only when EVERY application has an
  individual decision** (reviewed/accepted, or removed). A pending resubmission makes the
  **whole batch wait.**
- The secretariat can **remove an applicant** who misses the resubmission deadline. "Remove" =
  **disqualify with a recorded reason** (kept in the records/audit; excluded from the
  advancing batch); NOT a hard delete. This unblocks the batch.
- On finalize, only the **passers advance together** as the batch.

### 4.4 Background investigation (outsiders only) — DEFERRED
- In the FRS (step 4, outsiders only) but **not in the system**. **Out of scope** for this
  build; note it for later.

---

## 5. Plantilla track (permanent)

After the shared screening gate, the qualified batch proceeds:

### 5.1 Exam — BATCH, per vacancy (one schedule + one batch score screen + one finalize)
- Two scores per candidate: **General** and **Technical**, each 0–100.
- **Each score has an editable percentage beside it** (per-vacancy weights). Overall =
  `General×gen% + Technical×tech%`, **auto-computed live.**
- Weights are **per-vacancy** (default seed **60/40**), validated to sum to 100. The same split
  applies to **all** candidates in the vacancy (fairness across the ranked pool).

### 5.2 Interview — BATCH, per vacancy
- Uses the **competency rating sheet** (already per-vacancy; Core/Org/Technical, per-competency
  weights editable). HRMPSB members rate online OR the secretariat encodes (both already exist).
- Computed/normalized to 0–100 for the CAR.

### 5.3 CAR — per vacancy, ranked
- `OVERALL = ETE×ete% + Exam×exam% + Interview×interview%`, ranked high→low.
- **Weights per-vacancy** (default seed **40/20/40**). ETE = manual secretariat input. Exam =
  the batch exam result.
- The "score + editable %" pattern applies here too — each component's weight is editable
  per-vacancy.

### 5.4 Recommendation step (NEW — see §3.2)
1. CAR generated (ranked).
2. **HRMPSB board members get a read-only view of the CAR in the system.**
3. The board deliberates **off-system** (paper). The **secretariat records the board's
   recommendation** — recommended applicant + notes/remarks — **on the board's behalf.**
4. The recommendation **gates** the handoff (CAR cannot go to the appointing authority until
   recorded). Fits onto the existing CAR finalize step (which already records quorum +
   members present from Phase 6b-3 — add `recommended_applicant` + `recommendation_notes`).

### 5.5 Appointing Authority (Director)
- Sees the **ranked CAR + the board's recommended applicant**, and selects.

### 5.6 Inform + onboard
- Memo to the selected applicant; email notice + pre-employment requirements checklist;
  non-selected notified. 15-day appointment posting, flag ceremony, onboarding (permanent).

---

## 6. COS track (Contract of Service / contractual)

Shared screening (§4) is **identical** to Plantilla. Then it diverges and is **lighter**:

### 6.1 Exam — BATCH, per vacancy
- **Single end-user assessment score** (0–100). No second score, no weighting.
- Administered by the **end-user**; **HRMO/secretariat encodes** it (the end-user is NOT a
  system user — see §7).

### 6.2 Interview — BATCH, per vacancy
- **Simple score / notes** (end-user and/or HRMS). **NOT** the competency rating sheet.

### 6.3 Deliberation pick — the COS decision
- The **end-user + HRMO deliberate off-system** and identify the most qualified applicant.
- The **secretariat/HRMO records the pick** (chosen applicant + notes) — **one deliberation per
  vacancy** (today `DeliberationRecord` is per-application; simplify to per-vacancy for COS).
- **No CAR. No appointing authority.** The recorded pick *is* the decision.

### 6.4 Inform
- Email notice of selection + pre-employment checklist within deadline; non-selected notified.

---

## 7. The "end-user"
The office/unit that **requested** the position and where the COS hire will work (e.g. the
Finance Division requesting a contractual bookkeeper). They administer the COS exam + interview
and join the deliberation. **They are NOT a system user** — the HRMO/secretariat encodes their
exam score, interview result, and records the deliberation pick.

## 8. Weights — remove the global config entirely
- **Delete the global `AssessmentWeightConfig`** singleton + its `/internal/settings/assessment-weights/`
  page. All weights become **per-vacancy**:
  - exam General/Technical split,
  - CAR ETE/Exam/Interview split,
  - interview competency weights (already per-vacancy).
- **Seed defaults** on a new vacancy (exam 60/40, CAR 40/20/40, interview defaults), editable
  **until scoring starts**, then **lock + snapshot** (existing "lock once scoring begins" +
  snapshot-on-finalize rules).
- Touch-points to migrate off the global config: `ExamRecord.calculate_policy_score`,
  `component_weight_display`, the CAR computation (`_calculate_preliminary_assessment_score` /
  `_assessment_weight_display`), and the settings view/form.

## 9. Already built — DO NOT redo
- **Per-vacancy document checklist** (`PositionDocumentRequirement`, migration `0048`, commit
  `dcc5789`) — applies to both branches; this is the §4.1 checklist.
- **Document-review "View" option** (commit `12d639a`).
- **CAR computation** (computed overall, manual ETE, quorum attestation) — but its weights
  must move from global → per-vacancy (§8).
- **Interview competency rating sheet** (per-vacancy builder + scoring).
- **Applicant file-readability checks** on upload (commit `9bd95df`) — reuse for resubmission.

## 10. Deferred
- Background investigation (outsiders only).
- FRS Module revision (record both reversals in §3 — deliberation removed for Plantilla then a
  recommendation re-added; COS confirmed; CAR computed). Client said "build it our way first,
  revise the FRS later."

## 11. Suggested build sequence (Claude Code to refine into the real plan)
1. **Per-vacancy weights + remove global config** (§8) — unblocks exam + CAR changes; contained.
2. **Resubmission rework** (§4.2) — case stays visible, Track-Application re-upload, flagged-docs
   re-review.
3. **Batch screening gate + remove-applicant** (§4.3).
4. **Batch exam entry** (§5.1 / §6.1).
5. **Batch interview** (§5.2 / §6.2).
6. **Plantilla recommendation step + read-only CAR view** (§5.4–5.5).
7. **COS deliberation → per-vacancy pick** (§6.3).
8. **Vacancy-grouped queue/dashboard** (the surface that ties batch operations together).

Each step: small per-item commits, tests green, browser-verified.

## 12. How to run / verify
- **Tests** (sqlite): `.\.venv\Scripts\python.exe manage.py test <Class> --keepdb` with env
  `DJANGO_DEBUG=True SECURE_SSL_REDIRECT=False INTERNAL_MFA_ENABLED=True` and **empty**
  `POSTGRES_*`. Set `INTERNAL_MFA_ENABLED=True` explicitly. Tracebacks
  "ValueError: You cannot access this evidence item" in passing runs are EXPECTED. Full suite
  ≈ 13 min (run in background).
- **Preview**: launch config `recruitguard-internal` → Django on :8056 (sqlite, MFA off).
  Apply new migrations + re-run `seed_e2e_test_cases`. Login `/internal/login/`, users
  `secretariat` / `hrm_chief` / `hrmpsb_member`, password `Preview123!`. Server runs
  `--noreload` — restart to pick up Python/template changes.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Don't push/PR
  without asking.
