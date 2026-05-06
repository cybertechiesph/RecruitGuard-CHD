css = r"""

/* ══════════════════════════════════════════════════════════════════
   PHASE 5C — DELIBERATION / CAR STAGE  (.rg-del-*)
   ══════════════════════════════════════════════════════════════════ */

/* ──────────────────────────────────────────────────────────────
   Comparative interview review rows
   (scores are now visible at deliberation stage)
   ────────────────────────────────────────────────────────────── */
.rg-del-rating-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
}

.rg-del-rating-row {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 0.4rem;
    transition: border-color 0.1s;
}

.rg-del-rating-row:hover { border-color: #c7d2dc; }

.rg-del-score-badge {
    flex-shrink: 0;
    width: 52px;
    height: 52px;
    border-radius: 50%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: #f1f5f9;
    border: 2px solid #d7dde3;
    line-height: 1.1;
}

.rg-del-score-badge__num {
    font-size: 1.1rem;
    font-weight: 700;
    color: #5f6c78;
}

.rg-del-score-badge__sub {
    font-size: 0.57rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #9aa4af;
    margin-top: 0.05rem;
}

/* Score badge states */
.rg-del-score-badge--passing {
    background: #f0fdf4;
    border-color: #86efac;
}

.rg-del-score-badge--passing .rg-del-score-badge__num { color: #15803d; }
.rg-del-score-badge--passing .rg-del-score-badge__sub { color: #16a34a; }

.rg-del-score-badge--marginal {
    background: #fffbeb;
    border-color: #fcd34d;
}

.rg-del-score-badge--marginal .rg-del-score-badge__num { color: #92400e; }
.rg-del-score-badge--marginal .rg-del-score-badge__sub { color: #d97706; }

.rg-del-rating-row__body { flex: 1; min-width: 0; }

.rg-del-rating-row__evaluator {
    font-size: 0.875rem;
    font-weight: 600;
    color: #111827;
    margin-bottom: 0.15rem;
}

.rg-del-rating-row__role {
    font-size: 0.72rem;
    color: #5f6c78;
}

.rg-del-rating-row__notes {
    font-size: 0.79rem;
    color: #374151;
    margin-top: 0.3rem;
    line-height: 1.5;
}

.rg-del-rating-row__justification {
    font-size: 0.77rem;
    color: #92400e;
    margin-top: 0.2rem;
    padding: 0.3rem 0.6rem;
    background: #fffbeb;
    border-radius: 0.25rem;
    border-left: 3px solid #fcd34d;
}

/* Average score summary strip */
.rg-del-avg-strip {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    margin-top: 0.75rem;
    padding: 0.6rem 1rem;
    background: #005a87;
    border-radius: 0.4rem;
}

.rg-del-avg-strip__label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: rgba(255,255,255,0.7);
}

.rg-del-avg-strip__value {
    font-size: 1.2rem;
    font-weight: 700;
    color: #fff;
}

/* ──────────────────────────────────────────────────────────────
   Deliberation locked summary block
   (shown inline during sequential flow: delib locked, CAR pending)
   ────────────────────────────────────────────────────────────── */
.rg-del-locked-summary {
    padding: 1rem 1.25rem;
    background: #f8fafc;
    border: 1px solid #d7dde3;
    border-radius: 0.5rem;
    border-left: 4px solid #005a87;
}

.rg-del-locked-summary__header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
}

.rg-del-locked-summary__title {
    font-size: 0.875rem;
    font-weight: 700;
    color: #005a87;
    flex: 1;
}

/* ──────────────────────────────────────────────────────────────
   CAR ranked candidate items
   ────────────────────────────────────────────────────────────── */
.rg-del-car-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.rg-del-car-item {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 0.875rem 1rem;
    border: 1px solid #e2e8f0;
    border-radius: 0.5rem;
    background: #fff;
}

.rg-del-car-item:first-child { border-color: #86efac; background: #f0fdf4; }
.rg-del-car-item:first-child .rg-del-rank-badge { background: #dcfce7; border-color: #86efac; color: #15803d; }

.rg-del-rank-badge {
    flex-shrink: 0;
    width: 36px;
    height: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.875rem;
    font-weight: 700;
    background: #f1f5f9;
    border: 1.5px solid #d7dde3;
    color: #5f6c78;
    margin-top: 0.1rem;
}

.rg-del-car-item__body { flex: 1; min-width: 0; }

.rg-del-car-item__name {
    font-size: 0.875rem;
    font-weight: 700;
    color: #111827;
    margin-bottom: 0.3rem;
}

.rg-del-car-item__scores {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1rem;
    font-size: 0.79rem;
    color: #5f6c78;
    margin-bottom: 0.3rem;
}

.rg-del-car-item__score-cell { display: flex; gap: 0.35rem; align-items: baseline; }
.rg-del-car-item__score-label { color: #9aa4af; font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.rg-del-car-item__score-val { font-weight: 600; color: #374151; }

.rg-del-car-item__decision {
    font-size: 0.78rem;
    color: #5f6c78;
    margin-top: 0.2rem;
    font-style: italic;
}

/* ──────────────────────────────────────────────────────────────
   Deliberation section fin-bar margin
   ────────────────────────────────────────────────────────────── */
#cws-deliberation .rg-cws-fin-bar {
    margin-top: 1rem;
}
"""

with open(r"C:\Users\j3r1c\OneDrive\Documents\RecruitGuard-CHD\static\css\recruitguard.css", "a", encoding="utf-8") as f:
    f.write(css)

print("Deliberation CSS appended successfully.")
