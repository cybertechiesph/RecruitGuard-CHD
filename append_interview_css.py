css = r"""

/* ══════════════════════════════════════════════════════════════════
   PHASE 5B — INTERVIEW / RATING STAGE  (.rg-int-*)
   ══════════════════════════════════════════════════════════════════ */

/* ──────────────────────────────────────────────────────────────
   Applicant context strip
   ────────────────────────────────────────────────────────────── */
.rg-int-context-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 0.75rem 1.75rem;
}

.rg-int-context-item__label {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #5f6c78;
    margin-bottom: 0.2rem;
}

.rg-int-context-item__value {
    font-size: 0.875rem;
    font-weight: 600;
    color: #111827;
    line-height: 1.4;
}

/* ──────────────────────────────────────────────────────────────
   Session info read-only rows
   ────────────────────────────────────────────────────────────── */
.rg-int-session-info-row {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid #edf0f3;
    font-size: 0.8125rem;
}

.rg-int-session-info-row:last-child { border-bottom: 0; }

.rg-int-session-info-row__label {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #5f6c78;
    min-width: 120px;
    flex-shrink: 0;
}

.rg-int-session-info-row__value {
    color: #111827;
    font-weight: 500;
    flex: 1;
}

/* ──────────────────────────────────────────────────────────────
   Score entry — input + live dial
   ────────────────────────────────────────────────────────────── */
.rg-int-score-wrap {
    display: flex;
    align-items: center;
    gap: 1.75rem;
    padding: 1rem 1.25rem;
    background: #f8fafc;
    border: 1px solid #d7dde3;
    border-radius: 0.5rem;
    margin-bottom: 1rem;
}

@media (max-width: 560px) {
    .rg-int-score-wrap { flex-direction: column; align-items: flex-start; gap: 0.75rem; }
}

.rg-int-score-field { flex: 1; min-width: 0; }

.rg-int-score-dial {
    flex-shrink: 0;
    width: 80px;
    height: 80px;
    border-radius: 50%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: #fff;
    border: 2px solid #d7dde3;
    transition: border-color 0.2s;
}

.rg-int-score-val {
    font-size: 1.5rem;
    font-weight: 700;
    color: #9aa4af;
    line-height: 1.1;
    transition: color 0.2s;
}

.rg-int-score-dial-label {
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #9aa4af;
    margin-top: 0.1rem;
    transition: color 0.2s;
}

/* Dial states */
.rg-int-score-dial.is-passing  { border-color: #86efac; }
.rg-int-score-val.is-passing   { color: #15803d; }
.rg-int-score-dial-label.is-passing { color: #16a34a; }

.rg-int-score-dial.is-marginal { border-color: #fcd34d; }
.rg-int-score-val.is-marginal  { color: #92400e; }
.rg-int-score-dial-label.is-marginal { color: #d97706; }

/* ──────────────────────────────────────────────────────────────
   Below-75 threshold notice
   ────────────────────────────────────────────────────────────── */
.rg-int-threshold-notice {
    display: none;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.55rem 0.875rem;
    background: #fffbeb;
    border: 1px solid #fcd34d;
    border-radius: 0.4rem;
    font-size: 0.79rem;
    color: #92400e;
    margin-bottom: 0.75rem;
}

.rg-int-threshold-notice.is-visible { display: flex; }

/* ──────────────────────────────────────────────────────────────
   Rating submitted state card
   ────────────────────────────────────────────────────────────── */
.rg-int-submitted-state {
    display: flex;
    align-items: flex-start;
    gap: 1.25rem;
    padding: 1rem 1.25rem;
    background: #f0fdf4;
    border: 1px solid #86efac;
    border-radius: 0.5rem;
    margin-bottom: 0.875rem;
}

.rg-int-submitted-dial {
    flex-shrink: 0;
    width: 68px;
    height: 68px;
    border-radius: 50%;
    background: #dcfce7;
    border: 2px solid #86efac;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    line-height: 1.1;
}

.rg-int-submitted-dial__score {
    font-size: 1.35rem;
    font-weight: 700;
    color: #15803d;
}

.rg-int-submitted-dial__label {
    font-size: 0.58rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #16a34a;
    margin-top: 0.1rem;
}

.rg-int-submitted-state__body { flex: 1; min-width: 0; }

.rg-int-submitted-state__headline {
    font-size: 0.875rem;
    font-weight: 700;
    color: #15803d;
    margin-bottom: 0.25rem;
}

.rg-int-submitted-state__meta {
    font-size: 0.79rem;
    color: #374151;
    margin-bottom: 0.5rem;
    line-height: 1.5;
}

.rg-int-submitted-state__notice {
    font-size: 0.77rem;
    color: #5f6c78;
    font-style: italic;
}

/* Rating locked — no prior submission */
.rg-int-no-submission {
    padding: 0.875rem 1.25rem;
    background: #fafbfc;
    border: 1px dashed #c0c8d0;
    border-radius: 0.5rem;
    font-size: 0.8125rem;
    color: #5f6c78;
}

/* ──────────────────────────────────────────────────────────────
   Monitoring / progress track  (HRM Chief / session manager view)
   ────────────────────────────────────────────────────────────── */
.rg-int-progress-track {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
}

.rg-int-progress-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.875rem;
    border-radius: 0.375rem;
    font-size: 0.8125rem;
    border: 1px solid #e2e8f0;
    background: #f8fafc;
}

.rg-int-progress-item__dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
}

.rg-int-progress-item--submitted {
    background: #f0fdf4;
    border-color: #bbf7d0;
}

.rg-int-progress-item--submitted .rg-int-progress-item__dot { background: #16a34a; }

.rg-int-progress-item__name { flex: 1; font-weight: 500; color: #111827; }
.rg-int-progress-item__role { font-size: 0.72rem; color: #5f6c78; }

.rg-int-progress-item__tag {
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: #16a34a;
}

.rg-int-progress-item--submitted .rg-int-progress-item__tag { color: #16a34a; }

/* ──────────────────────────────────────────────────────────────
   Revise panel (hidden until toggled)
   ────────────────────────────────────────────────────────────── */
.rg-int-revise-panel {
    margin-top: 0.75rem;
    padding-top: 1rem;
    border-top: 1px dashed #d7dde3;
}

.rg-int-revise-notice {
    font-size: 0.79rem;
    color: #5f6c78;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: #f8fafc;
    border-left: 3px solid #c0c8d0;
    border-radius: 0 0.25rem 0.25rem 0;
}

/* ──────────────────────────────────────────────────────────────
   Two-level lock distinction callout
   ────────────────────────────────────────────────────────────── */
.rg-int-lock-distinction {
    padding: 0.75rem 1rem;
    background: #f0f9ff;
    border: 1px solid #bae6fd;
    border-radius: 0.4rem;
    font-size: 0.79rem;
    color: #0c4a6e;
    margin-bottom: 0.875rem;
    line-height: 1.55;
}

/* ──────────────────────────────────────────────────────────────
   Interview section fin-bar margin
   ────────────────────────────────────────────────────────────── */
#cws-interview .rg-cws-fin-bar {
    margin-top: 1rem;
}
"""

with open(r"C:\Users\j3r1c\OneDrive\Documents\RecruitGuard-CHD\static\css\recruitguard.css", "a", encoding="utf-8") as f:
    f.write(css)

print("Interview CSS appended successfully.")
