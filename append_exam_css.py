css = r"""

/* ══════════════════════════════════════════════════════════════════
   PHASE 5A — EXAMINATION STAGE  (.rg-exam-*)
   ══════════════════════════════════════════════════════════════════ */

/* ──────────────────────────────────────────────────────────────
   Exam status bar  — reuses .rg-scr-elig-bar structure;
   adds exam-specific state modifiers
   ────────────────────────────────────────────────────────────── */

/* No status yet */
.rg-scr-elig-bar--no-status {
    background: #fafbfc;
    border-color: #d7dde3;
}

/* Completed */
.rg-scr-elig-bar--completed {
    background: #f0fdf4;
    border-color: #86efac;
}

.rg-scr-elig-bar--completed .rg-scr-elig-bar__icon {
    background: #dcfce7;
    color: #16a34a;
}

.rg-scr-elig-bar--completed .rg-scr-elig-bar__verdict { color: #15803d; }

/* Waived */
.rg-scr-elig-bar--waived {
    background: #fffbeb;
    border-color: #fcd34d;
}

.rg-scr-elig-bar--waived .rg-scr-elig-bar__icon {
    background: #fef9c3;
    color: #d97706;
}

.rg-scr-elig-bar--waived .rg-scr-elig-bar__verdict { color: #92400e; }

/* Absent */
.rg-scr-elig-bar--absent {
    background: #fff1f2;
    border-color: #fca5a5;
}

.rg-scr-elig-bar--absent .rg-scr-elig-bar__icon {
    background: #fee2e2;
    color: #dc2626;
}

.rg-scr-elig-bar--absent .rg-scr-elig-bar__verdict { color: #b91c1c; }

/* ──────────────────────────────────────────────────────────────
   Conditional field groups (shown/hidden by JS per exam_status)
   ────────────────────────────────────────────────────────────── */
.rg-exam-cond-group {
    display: none;
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px dashed #d7dde3;
}

.rg-exam-cond-group.is-visible {
    display: block;
}

/* Grid layout for score/result/dates row */
.rg-exam-score-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem 1.25rem;
}

@media (max-width: 600px) {
    .rg-exam-score-grid { grid-template-columns: 1fr; }
}

/* ──────────────────────────────────────────────────────────────
   Readiness items for exam (reuses .rg-scr-ready-* classes)
   ────────────────────────────────────────────────────────────── */
/* Note: .rg-scr-ready-list / .rg-scr-ready-item / --met / --blocking
   are already defined above and are reused for exam as-is. */

/* ──────────────────────────────────────────────────────────────
   Locked exam record display  (mirrors .rg-scr-locked-field)
   ────────────────────────────────────────────────────────────── */
.rg-exam-locked-field {
    padding: 0.6rem 0;
    border-bottom: 1px solid #edf0f3;
}

.rg-exam-locked-field:last-child { border-bottom: 0; }

.rg-exam-locked-field__label {
    font-size: 0.7rem;
    font-weight: 700;
    color: #5f6c78;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 0.3rem;
}

.rg-exam-locked-field__value {
    font-size: 0.875rem;
    color: #111827;
    line-height: 1.6;
    white-space: pre-line;
}

/* ──────────────────────────────────────────────────────────────
   Exam section fin-bar margin
   ────────────────────────────────────────────────────────────── */
#cws-exam .rg-cws-fin-bar {
    margin-top: 1rem;
}
"""

with open(r"C:\Users\j3r1c\OneDrive\Documents\RecruitGuard-CHD\static\css\recruitguard.css", "a", encoding="utf-8") as f:
    f.write(css)

print("Exam CSS appended successfully.")
