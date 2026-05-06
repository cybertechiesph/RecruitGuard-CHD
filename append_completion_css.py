css = r"""

/* ══════════════════════════════════════════════════════════════════
   PHASE 5E — COMPLETION / ARCHIVE STAGE  (.rg-cmp-*)
   ══════════════════════════════════════════════════════════════════ */

/* ──────────────────────────────────────────────────────────────
   Closed / final outcome banner
   ────────────────────────────────────────────────────────────── */
.rg-cmp-closed-banner {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 1rem 1.25rem;
    border-radius: 0.5rem;
    border: 1.5px solid;
}

.rg-cmp-closed-banner--selected {
    background: #f0fdf4;
    border-color: #86efac;
    color: #15803d;
}

.rg-cmp-closed-banner--not-selected {
    background: #fef2f2;
    border-color: #fca5a5;
    color: #991b1b;
}

.rg-cmp-closed-banner--neutral {
    background: #eff6ff;
    border-color: #93c5fd;
    color: #1d4ed8;
}

.rg-cmp-closed-banner__icon {
    font-size: 1.5rem;
    flex-shrink: 0;
    margin-top: 0.05rem;
}

.rg-cmp-closed-banner__body { flex: 1; }

.rg-cmp-closed-banner__title {
    font-size: 0.9375rem;
    font-weight: 800;
    margin: 0 0 0.2rem;
    line-height: 1.2;
    letter-spacing: -0.01em;
}

.rg-cmp-closed-banner__meta {
    font-size: 0.79rem;
    opacity: 0.85;
    margin: 0;
    line-height: 1.5;
}

.rg-cmp-closed-banner__audit-note {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.72rem;
    font-weight: 600;
    opacity: 0.7;
    margin-top: 0.35rem;
    letter-spacing: 0.02em;
    text-transform: uppercase;
}

/* ──────────────────────────────────────────────────────────────
   Requirement rows — read-only locked view
   ────────────────────────────────────────────────────────────── */
.rg-cmp-req-list {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
}

.rg-cmp-req-row {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.55rem 0.75rem;
    border: 1px solid #e2e8f0;
    border-radius: 0.35rem;
    background: #f8fafc;
    font-size: 0.8125rem;
    line-height: 1.5;
}

.rg-cmp-req-row__main { flex: 1; }

.rg-cmp-req-row__label {
    color: #374151;
    font-weight: 500;
}

.rg-cmp-req-row__notes {
    color: #6b7280;
    font-size: 0.75rem;
    margin-top: 0.15rem;
}

.rg-cmp-req-status-badge {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.15rem 0.55rem;
    border-radius: 2rem;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    white-space: nowrap;
}

.rg-cmp-req-status-badge--completed {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #86efac;
}

.rg-cmp-req-status-badge--pending {
    background: #fef9c3;
    color: #854d0e;
    border: 1px solid #fde047;
}

.rg-cmp-req-status-badge--na {
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #cbd5e1;
}

/* ──────────────────────────────────────────────────────────────
   Requirement form rows — active editing state
   ────────────────────────────────────────────────────────────── */
.rg-cmp-req-form-row {
    padding: 0.875rem 1rem;
    border: 2px solid #e2e8f0;
    border-radius: 0.4rem;
    background: #fff;
    transition: border-color 0.15s, background 0.15s;
}

.rg-cmp-req-form-row + .rg-cmp-req-form-row { margin-top: 0.5rem; }

.rg-cmp-req-form-row[data-req-status="completed"] {
    border-color: #86efac;
    background: #f0fdf4;
}

.rg-cmp-req-form-row[data-req-status="pending"] {
    border-color: #fde047;
    background: #fefce8;
}

.rg-cmp-req-form-row[data-req-status="not_applicable"] {
    border-color: #e2e8f0;
    background: #f8fafc;
    opacity: 0.8;
}

.rg-cmp-req-form-row.is-delete-pending {
    border-color: #fca5a5 !important;
    background: #fef2f2 !important;
    opacity: 0.55;
}

.rg-cmp-req-form-row__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.65rem;
    gap: 0.5rem;
}

.rg-cmp-req-form-row__seq {
    font-size: 0.69rem;
    font-weight: 700;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.rg-cmp-req-form-row__delete-label {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.75rem;
    color: #dc2626;
    cursor: pointer;
}

/* ──────────────────────────────────────────────────────────────
   Requirement progress bar
   ────────────────────────────────────────────────────────────── */
.rg-cmp-req-progress {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 0.35rem;
    font-size: 0.79rem;
    color: #374151;
    margin-bottom: 0.75rem;
}

.rg-cmp-req-progress__label { font-weight: 600; }

.rg-cmp-req-progress__count {
    font-weight: 800;
    color: #005a87;
}

.rg-cmp-req-progress__bar {
    flex: 1;
    height: 6px;
    background: #e2e8f0;
    border-radius: 3px;
    overflow: hidden;
    min-width: 40px;
}

.rg-cmp-req-progress__fill {
    height: 100%;
    background: #22c55e;
    border-radius: 3px;
}

/* ──────────────────────────────────────────────────────────────
   Decision reference block (inside active completion body)
   ────────────────────────────────────────────────────────────── */
.rg-cmp-decision-ref {
    display: flex;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: #f0f9ff;
    border: 1px solid #bae6fd;
    border-left: 4px solid #0284c7;
    border-radius: 0.4rem;
    font-size: 0.8125rem;
    color: #0c4a6e;
    line-height: 1.6;
}

.rg-cmp-decision-ref__icon {
    flex-shrink: 0;
    font-size: 1rem;
    margin-top: 0.05rem;
}

/* ──────────────────────────────────────────────────────────────
   Archive metadata strip
   ────────────────────────────────────────────────────────────── */
.rg-cmp-archive-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem 2.5rem;
    padding: 0.875rem 1.25rem;
    background: #f8fafc;
    border: 1px solid #d7dde3;
    border-left: 4px solid #005a87;
    border-radius: 0.5rem;
}

.rg-cmp-archive-meta__item {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
}

.rg-cmp-archive-meta__label {
    font-size: 0.69rem;
    font-weight: 700;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.rg-cmp-archive-meta__value {
    font-size: 0.8125rem;
    font-weight: 700;
    color: #1e293b;
}

/* ──────────────────────────────────────────────────────────────
   Case timeline
   ────────────────────────────────────────────────────────────── */
.rg-cmp-timeline {
    display: flex;
    flex-direction: column;
    gap: 0;
    position: relative;
    padding-left: 1.5rem;
}

.rg-cmp-timeline::before {
    content: "";
    position: absolute;
    left: 0.3rem;
    top: 0.5rem;
    bottom: 0.5rem;
    width: 2px;
    background: #e2e8f0;
    border-radius: 1px;
}

.rg-cmp-timeline-item {
    display: flex;
    gap: 0.75rem;
    padding: 0.3rem 0;
    position: relative;
    min-height: 2rem;
}

.rg-cmp-timeline-item__dot {
    position: absolute;
    left: -1.46rem;
    top: 0.6rem;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #94a3b8;
    border: 2px solid #fff;
    outline: 1.5px solid #94a3b8;
    flex-shrink: 0;
}

.rg-cmp-timeline-item--closed .rg-cmp-timeline-item__dot {
    background: #005a87;
    outline-color: #005a87;
}

.rg-cmp-timeline-item--decision .rg-cmp-timeline-item__dot {
    background: #15803d;
    outline-color: #15803d;
}

.rg-cmp-timeline-item--completion .rg-cmp-timeline-item__dot {
    background: #0284c7;
    outline-color: #0284c7;
}

.rg-cmp-timeline-item--reopen .rg-cmp-timeline-item__dot {
    background: #d97706;
    outline-color: #d97706;
}

.rg-cmp-timeline-item--sensitive .rg-cmp-timeline-item__dot {
    background: #dc2626;
    outline-color: #dc2626;
}

.rg-cmp-timeline-item__body {
    flex: 1;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid #f1f5f9;
}

.rg-cmp-timeline-item:last-child .rg-cmp-timeline-item__body {
    border-bottom: none;
}

.rg-cmp-timeline-item__action {
    font-size: 0.79rem;
    font-weight: 600;
    color: #1e293b;
    line-height: 1.4;
}

.rg-cmp-timeline-item__meta {
    font-size: 0.72rem;
    color: #6b7280;
    line-height: 1.4;
    margin-top: 0.05rem;
}

/* ──────────────────────────────────────────────────────────────
   Reopen zone — guarded, secondary, exceptional
   ────────────────────────────────────────────────────────────── */
.rg-cmp-reopen-zone {
    border: 2px dashed #fca5a5;
    border-radius: 0.5rem;
    padding: 1rem 1.25rem;
    background: #fff;
}

.rg-cmp-reopen-zone__header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
}

.rg-cmp-reopen-zone__icon {
    color: #dc2626;
    font-size: 0.9rem;
    flex-shrink: 0;
}

.rg-cmp-reopen-zone__title {
    font-size: 0.8125rem;
    font-weight: 700;
    color: #991b1b;
    flex: 1;
}

.rg-cmp-reopen-zone__body {
    font-size: 0.79rem;
    color: #374151;
    line-height: 1.65;
    margin-bottom: 0.75rem;
}

/* ──────────────────────────────────────────────────────────────
   Completion section fin-bar margin override
   ────────────────────────────────────────────────────────────── */
#cws-completion .rg-cws-fin-bar {
    margin-top: 1rem;
}
"""

with open(r"C:\Users\j3r1c\OneDrive\Documents\RecruitGuard-CHD\static\css\recruitguard.css", "a", encoding="utf-8") as f:
    f.write(css)

print("Completion/Archive CSS appended successfully.")
