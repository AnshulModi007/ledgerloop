# LedgerLoop console — UI brief

Paste this into a fresh Claude session, attach a screenshot if you have one, and ask for
what you want changed. It is self-contained: it does not assume access to the repo.

---

## 1. What the product is

LedgerLoop reconciles payment-gateway settlements. Four messy CSVs go in (gateway
transactions, settlement report, bank statement, ERP ledger); it works out which
transactions make up each lump-sum bank deposit, proposes the corrective journal entries,
and escalates whatever it can't resolve as a typed, explained exception instead of guessing.

It matches in tiers, cheapest and most certain first:

| Tier | Method | Typical share of 284 lines |
|---|---|---|
| 1 | exact join on UTR + amount | 158 |
| 2 | fee model, then subset-sum search | 102 |
| 3 | LLM picks from a **fixed candidate menu** computed by tier 2, or abstains | 0–2 |
| — | escalated to a human, typed and explained | 24 |

**The safety thesis, which the UI exists to make visible:** the model is never shown a
blank field and asked to produce a transaction id. It can only select from candidates the
deterministic tiers already computed, or abstain. False-match rate is 0.00% and that is the
headline metric — a wrong match silently corrupts the books, an escalation costs someone
two minutes.

The audience is a finance-ops reviewer. The console is where they work the exception queue
and sign off the postings.

## 2. Technical constraints (hard)

- **Vanilla JS + CSS. No build step, no framework, no bundler, no external requests.** The
  project's whole setup story is `pip install -e .`; a node toolchain in front of the
  dashboard would break "clone and run". Do not introduce React, Tailwind, a CDN font, or
  an icon library.
- Exactly three files, served by FastAPI on one port:
  - `index.html` (~60 lines) — static shell only; everything else is rendered by JS
  - `styles.css` (~470 lines) — all styling, CSS custom properties for theming
  - `app.js` (~720 lines) — all rendering, via a tiny `el(tag, props, ...children)` helper
- Light and dark themes. Palette roles are defined once as custom properties on `:root`,
  redefined under **both** `@media (prefers-color-scheme: dark)` and `:root[data-theme="dark"]`
  so an explicit toggle wins in both directions. Theme choice persists in `localStorage`
  inside try/catch.
- Icons are inline SVG built in JS. No emoji in the interface.

## 3. Current screen anatomy

### Masthead (sticky)
`LedgerLoop` + tagline · provider chip (`● LLM: ollama (pinned)` with a green dot when a
model is reachable, gray when not) · theme toggle icon button.

### Control bar (card)
Batch `<select>` · "Deterministic only (`--no-llm`)" checkbox · Reviewer text input ·
**Run reconciliation** primary button · inline spinner chip while running.

Runs are async: `POST /api/runs` returns 202 + id, the client polls every 600 ms.

### Summary row (2-column grid, stacks under 860px)
- **Hero figure** — auto-match rate at 52px, e.g. `91.5%`, with `260 of 284 bank lines, no
  human touched them` beneath. Exactly one hero per view.
- **KPI tiles** (auto-fit, min 158px): Needs a decision · Escalated value · LLM calls ·
  Deterministic (or "Review feedback" when a reviewer's rejections suppressed candidates).

### Disposition bar
A single horizontal stacked bar, 26px tall, showing all 284 lines split tier1 / tier2 /
tier3 / escalated. 2px surface-coloured gaps between segments, 4px rounded outer ends.
Hover gives a tooltip with count, share and a one-line note. Below it a legend where every
segment is also **direct-labelled with its count** — identity never rests on colour alone.

### Tab strip → panel
`Exception queue` · `Journal entries` · `Tie-out` · `Audit trail`. Switching tabs re-renders
only the strip and the panel, never the whole page.

**Exception queue.** A row of reason-code filter chips, each carrying its own count
(`All 24` / `LOW_CONFIDENCE 4` / `OUT_OF_SCOPE 20`). Then two sections:

- *Needs a decision (4)* — cards with: bank line id, reason-code chip, status pill, credit
  amount right-aligned; the pipeline's own plain-English explanation; a collapsible
  "Show the N candidates considered" disclosure revealing each candidate's id, rule, score,
  amount delta and the actual transaction ids; then a note field and Approve / Reject /
  Reassign buttons.
- *No action required (20)* — same card, no buttons. These are bank credits that were never
  gateway settlements; declining to match them is the correct answer, already taken. They
  stay listed so nothing is silently dropped, but they are separated from the real work —
  showing them beside genuine disputes would overstate the workload 6×.

A candidate the reviewer previously rejected is rendered dashed and dimmed with a
"you rejected this — not re-proposed" pill. Shown, never deleted.

**Journal entries.** A pass/fail banner (`Every batch balances`), a "Close the loop" card
with the **Approve postings, then re-run** button — which approves, re-runs, and reports
*zero new postings* — then a table of the first 300 of 260-odd batches.

**Tie-out.** Clean/finding banner; a reconciliation statement with dot-leader rows
(Bank statement / Reconciled / Unreconciled); four control cards (cash ties out, books
balance, no receivable cleared twice, fee drift absorbed); a movement-by-account table.

**Audit trail.** Newest-first table of the append-only log: timestamp, bank line, decision,
rule/reason, confidence, actor.

## 4. Design decisions already made — please keep these

These are load-bearing, not preferences:

1. **The tier colours are an ordinal ramp, not a categorical palette.** One hue, light to
   dark: `#86b6ef` → `#3987e5` → `#184f95`. Tiers 1–3 are ordered stages of increasing
   difficulty, not three unrelated identities. Escalated is neutral gray (`#c3c2b7` light /
   `#52514e` dark) because it is not a fourth stage. Validated for monotone lightness,
   ≥0.06 adjacent lightness gap, and ≥2:1 contrast of the light end against its own surface
   in both themes. The same hexes serve both themes so a tier keeps its colour when the
   theme flips — colour follows the entity, never its rank.
2. **Every status verdict carries an icon *and* a word** (✓ Pass / ✗ Finding), never colour
   alone.
3. **Money is never computed in the browser.** Amounts arrive as `{paise: 47856920, inr:
   "Rs.4,78,56,920.92"}` and only the string is rendered. The pipeline keeps integer paise
   precisely so nothing rounds unobserved; JS arithmetic would throw that away at the last
   step. Never divide by 100, never `toFixed` a currency value.
4. **Tabular figures only in columns** (tables, axis ticks). The hero and stat-tile values
   use default proportional figures.
5. **The needs-review / no-action split stays.** It is the honest way to report review
   burden.
6. **A withheld candidate is marked, not hidden.**

## 5. Palette reference

| Role | Light | Dark |
|---|---|---|
| page plane | `#f9f9f7` | `#0d0d0d` |
| card surface | `#fcfcfb` | `#1a1a19` |
| raised surface | `#ffffff` | `#212120` |
| primary ink | `#0b0b0b` | `#ffffff` |
| secondary ink | `#52514e` | `#c3c2b7` |
| muted ink | `#898781` | `#898781` |
| gridline | `#e1e0d9` | `#2c2c2a` |
| hairline rule | `rgba(11,11,11,.10)` | `rgba(255,255,255,.10)` |
| accent | `#2a78d6` | `#3987e5` |
| good / warning / serious / critical | `#0ca30c` `#fab219` `#ec835a` `#d03b3b` (fixed, both themes) |

Type: `system-ui, -apple-system, "Segoe UI", sans-serif` throughout, 14px base. Radius 10px
cards / 6px controls. No display or serif face anywhere.

## 6. Real data to design against

```json
{
  "total_records": 284, "resolved_count": 260, "auto_match_rate": 0.915,
  "tier_counts": {"tier1": 158, "tier2": 102},
  "exception_count": 24, "exceptions_needing_review": 4, "exceptions_no_action": 20,
  "llm_calls_made": 0, "providers_used": [],
  "postings_total": 20272, "postings_new": 0,
  "tie_out_clean": true, "candidates_suppressed_by_review": 0
}
```

A representative queue card:

> **BANK00176** · `LOW_CONFIDENCE` · open · **Rs.25,164.77**
> Bank credit of ₹25,164.77 on 05 Mar 2026. The strongest of 1 candidate(s) groups 4
> transaction(s) by amount and date match with no UTR present, but scores 0.66 against a
> 0.70 resolve threshold. Escalated for review rather than matched on a weak signal.
> *Adjudicator note: the rule 'amount_date_fallback_no_utr' and a settlement batch STL00156
> with zero amount difference and a 4-day lag make this candidate a strong fit.*
>
> Candidate `BANK00176-C0` · `amount_date_fallback_no_utr` · score 0.66 · Δ Rs.0.00
> `TXN000512  TXN000513  TXN000514  TXN000515`

Note the shape: everything before "Adjudicator note" is machine-computed. The model's
narrative is appended and labelled, and can never alter a figure. Any redesign must keep
that separation legible.

Edge cases the layout must survive: 0 exceptions; 1 exception; an escalated value of
`Rs.0.00`; a run with `providers_used: []`; a 20,000-posting journal; a control that fails.

## 7. What I want from you

<!-- Replace this section with your actual ask. Examples: -->

- Review the attached screenshot and tell me what reads badly — hierarchy, density,
  alignment, whitespace, scan order — and give me the specific CSS changes.
- The queue is the screen a reviewer lives in. Redesign the exception card so a decision
  can be made without expanding anything.
- Make it work properly on a 1280×800 laptop and on a phone.
- The disposition bar and the KPI row say overlapping things. Propose a single clearer
  summary region.

Give concrete CSS/JS, not general advice. Keep the constraints in §2 and the decisions in
§4 unless you explain why one is wrong.
