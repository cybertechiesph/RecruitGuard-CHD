css = r"""

/* ══════════════════════════════════════════════════════════════════
   PHASE 5D — DECISION / SUBMISSION PACKET STAGE  (.rg-pkt-*  .rg-dec-*)
   ══════════════════════════════════════════════════════════════════ */

/* ──────────────────────────────────────────────────────────────
   Submission packet wrapper
   ────────────────────────────────────────────────────────────── */
.rg-pkt {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

/* ──────────────────────────────────────────────────────────────
   Packet section block (each pipeline stage summary)
   ────────────────────────────────────────────────────────────── */
.rg-pkt-section {
    border: 1px solid #d7dde3;
    border-radius: 0.5rem;
    overflow: hidden;
}

.rg-pkt-section__head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 1rem;
    background: #f1f5f9;
    border-bottom: 1px solid #d7dde3;
}

.rg-pkt-section__icon {
    font-size: 0.9rem;
    color: #005a87;
    flex-shrink: 0;
}

.rg-pkt-section__title {
    font-size: 0.8125rem;
    font-weight: 700;
    color: #1e293b;
    letter-spacing: 0.02em;
    flex: 1;
}

.rg-pkt-section__badge {
    font-size: 0.7rem;
    font-weight: 600;
    padding: 0.15rem 0.5rem;
    border-radius: 2rem;
    letter-spacing: 0.03em;
    text-transform: uppercase;
}

.rg-pkt-section__badge--ok {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #86efac;
}

.rg-pkt-section__badge--warn {
    background: #fef9c3;
    color: #854d0e;
    border: 1px solid #fde047;
}

.rg-pkt-section__badge--none {
    background: #f1f5f9;
    color: #64748b;
    border: 1px solid #cbd5e1;
}

.rg-pkt-section__body {
    padding: 0.875rem 1rem;
}

/* ──────────────────────────────────────────────────────────────
   Summary rows inside packet sections
   ────────────────────────────────────────────────────────────── */
.rg-pkt-rows {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
}

.rg-pkt-row {
    display: flex;
    gap: 0.75rem;
    font-size: 0.8rem;
    line-height: 1.5;
}

.rg-pkt-row__label {
    flex-shrink: 0;
    width: 160px;
    color: #64748b;
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding-top: 0.05rem;
}

.rg-pkt-row__value {
    flex: 1;
    color: #1e293b;
}

/* Outcome pill inline in packet row */
.rg-pkt-outcome-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.6rem;
    border-radius: 2rem;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.03em;
}

.rg-pkt-outcome-pill--passed {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #86efac;
}

.rg-pkt-outcome-pill--failed {
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fca5a5;
}

.rg-pkt-outcome-pill--pending {
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #cbd5e1;
}

/* ──────────────────────────────────────────────────────────────
   Interview session sub-rows (per-session inside the packet)
   ────────────────────────────────────────────────────────────── */
.rg-pkt-session-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.rg-pkt-session-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.55rem 0.75rem;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 0.35rem;
}

.rg-pkt-session-item__label {
    flex: 1;
    font-size: 0.79rem;
    color: #374151;
}

.rg-pkt-session-item__avg {
    font-size: 0.79rem;
    font-weight: 700;
    color: #005a87;
}

/* ──────────────────────────────────────────────────────────────
   Evidence reference list
   ────────────────────────────────────────────────────────────── */
.rg-pkt-evidence-list {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
}

.rg-pkt-evidence-item {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.79rem;
    color: #374151;
    padding: 0.3rem 0.5rem;
    border-radius: 0.25rem;
}

.rg-pkt-evidence-item:nth-child(odd) { background: #f8fafc; }

.rg-pkt-evidence-item__icon {
    font-size: 0.7rem;
    color: #005a87;
    flex-shrink: 0;
}

/* ──────────────────────────────────────────────────────────────
   Missing-components warning strip
   ────────────────────────────────────────────────────────────── */
.rg-pkt-missing-strip {
    display: flex;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: #fef9c3;
    border: 1px solid #fde047;
    border-radius: 0.4rem;
    font-size: 0.8125rem;
    color: #854d0e;
    line-height: 1.55;
}

.rg-pkt-missing-strip__icon {
    flex-shrink: 0;
    margin-top: 0.1rem;
}

.rg-pkt-missing-strip ul {
    margin: 0.35rem 0 0;
    padding-left: 1.1rem;
}

.rg-pkt-missing-strip li { margin-bottom: 0.15rem; }

/* ──────────────────────────────────────────────────────────────
   Packet overview block (context / applicant meta)
   ────────────────────────────────────────────────────────────── */
.rg-pkt-overview {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.5rem 2rem;
    padding: 0.875rem 1rem;
    background: #f8fafc;
    border: 1px solid #d7dde3;
    border-radius: 0.5rem;
    border-left: 4px solid #005a87;
}

@media (max-width: 600px) {
    .rg-pkt-overview { grid-template-columns: 1fr; }
}

/* ──────────────────────────────────────────────────────────────
   Decision block — outcome choice strip (Selected / Not Selected)
   ────────────────────────────────────────────────────────────── */
.rg-dec-choice-strip {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-bottom: 0.25rem;
}

.rg-dec-choice-btn {
    flex: 1;
    min-width: 160px;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.75rem 1rem;
    border: 2px solid #d7dde3;
    border-radius: 0.5rem;
    background: #fff;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    font-size: 0.875rem;
    font-weight: 600;
    color: #374151;
    text-align: left;
}

.rg-dec-choice-btn:hover { border-color: #94a3b8; background: #f8fafc; }

.rg-dec-choice-btn.is-active--selected {
    border-color: #86efac;
    background: #f0fdf4;
    color: #15803d;
}

.rg-dec-choice-btn.is-active--not-selected {
    border-color: #fca5a5;
    background: #fef2f2;
    color: #991b1b;
}

.rg-dec-choice-btn__icon {
    font-size: 1.1rem;
    flex-shrink: 0;
}

.rg-dec-choice-btn__body { flex: 1; }

.rg-dec-choice-btn__title {
    display: block;
    font-size: 0.875rem;
    font-weight: 700;
    line-height: 1.2;
}

.rg-dec-choice-btn__sub {
    display: block;
    font-size: 0.72rem;
    font-weight: 400;
    color: #64748b;
    margin-top: 0.1rem;
}

.rg-dec-choice-btn.is-active--selected .rg-dec-choice-btn__sub { color: #16a34a; }
.rg-dec-choice-btn.is-active--not-selected .rg-dec-choice-btn__sub { color: #dc2626; }

/* ──────────────────────────────────────────────────────────────
   Decision outcome pill (locked state)
   ────────────────────────────────────────────────────────────── */
.rg-dec-outcome-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.85rem;
    border-radius: 2rem;
    font-size: 0.8125rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

.rg-dec-outcome-pill--selected {
    background: #dcfce7;
    color: #15803d;
    border: 1.5px solid #86efac;
}

.rg-dec-outcome-pill--not-selected {
    background: #fee2e2;
    color: #991b1b;
    border: 1.5px solid #fca5a5;
}

/* ──────────────────────────────────────────────────────────────
   Decision status bar (locked state header)
   ────────────────────────────────────────────────────────────── */
.rg-dec-status-bar {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 1rem;
    border-radius: 0.4rem;
    font-size: 0.8125rem;
    font-weight: 600;
}

.rg-dec-status-bar--selected {
    background: #f0fdf4;
    border: 1px solid #86efac;
    color: #15803d;
}

.rg-dec-status-bar--not-selected {
    background: #fef2f2;
    border: 1px solid #fca5a5;
    color: #991b1b;
}

/* ──────────────────────────────────────────────────────────────
   Decision locked frame (mirrors other locked frames)
   ────────────────────────────────────────────────────────────── */
.rg-dec-locked-frame {
    border: 1px solid #d7dde3;
    border-radius: 0.5rem;
    overflow: hidden;
}

.rg-dec-locked-frame__header {
    padding: 0.6rem 1rem;
    background: #f1f5f9;
    border-bottom: 1px solid #d7dde3;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.rg-dec-locked-frame__header-title {
    font-size: 0.8125rem;
    font-weight: 700;
    color: #1e293b;
    flex: 1;
}

.rg-dec-locked-frame__body {
    padding: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}

/* ──────────────────────────────────────────────────────────────
   Handoff / closure notice
   ────────────────────────────────────────────────────────────── */
.rg-dec-handoff {
    display: flex;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: #f0f9ff;
    border: 1px solid #bae6fd;
    border-radius: 0.4rem;
    border-left: 4px solid #0284c7;
    font-size: 0.8125rem;
    color: #0c4a6e;
    line-height: 1.6;
}

.rg-dec-handoff__icon { flex-shrink: 0; margin-top: 0.1rem; font-size: 1rem; }

/* ──────────────────────────────────────────────────────────────
   Decision section fin-bar margin
   ────────────────────────────────────────────────────────────── */
#cws-decision .rg-cws-fin-bar {
    margin-top: 1rem;
}
"""

with open(r"C:\Users\j3r1c\OneDrive\Documents\RecruitGuard-CHD\static\css\recruitguard.css", "a", encoding="utf-8") as f:
    f.write(css)

print("Decision/Submission Packet CSS appended successfully.")
