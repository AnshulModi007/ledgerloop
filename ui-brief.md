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
  - `index.html` (~64 lines) — static shell only; everything else is rendered by JS
  - `styles.css` (~900 lines) — all styling, CSS custom properties for theming
  - `app.js` (~1000 lines) — all rendering, via a tiny `el(tag, props, ...children)` helper
- Light and dark themes. Palette roles are defined once as custom properties on `:root`,
  redefined under **both** `@media (prefers-color-scheme: dark)` and `:root[data-theme="dark"]`
  so an explicit toggle wins in both directions. Theme choice persists in `localStorage`
  inside try/catch.
- Icons are inline SVG built in JS. No emoji in the interface.

## 3. The governing visual stance: a working paper, not a dashboard

Read this before proposing anything. The console was originally built as a stack of
identical rounded, shadowed cards with an uppercase tracked label above every region —
the default admin-dashboard kit. It has since been deliberately reshaped around the
vernacular of the thing it actually is: an accountant's working paper.

That means three rules, and a change that breaks one of them needs to argue for itself:

1. **Structure is carried by rules and alignment, not by boxes.** Hairlines separate
   regions; there is no drop shadow anywhere except the tooltip and the card a reviewer
   is hovering.
2. **A box means "act on me".** Exactly one thing on the page is boxed — an exception
   card in *Needs a decision*. Because nothing else is, being boxed carries meaning.
3. **No uppercase tracked labels.** Case is not a hierarchy device here. Labels are
   sentence case, small and quiet, sitting next to what they name; a rule does the
   separating.

And one consequence that is the whole reason to set figures in a column at all:
**comparable figures share a right edge.** The queue's credit amounts sit on a fixed
168px track so they align down the entire list; the reconciliation statement's amounts
share a matching 168px edge.

## 4. Current screen anatomy

### Masthead (sticky)
`LedgerLoop` + tagline · provider chip (`● LLM: ollama (pinned)` with a green dot when a
model is reachable, gray when not) · theme toggle icon button. One row at every width —
under 720px the tagline is dropped rather than letting the row wrap.

### Control bar
A ruled strip, not a tile: Batch `<select>` · "Deterministic only (`--no-llm`)" checkbox ·
Reviewer text input · **Run reconciliation** primary button · inline spinner chip while
running, closed by a hairline beneath. Baseline alignment is what groups it.

Runs are async: `POST /api/runs` returns 202 + id, the client polls every 600 ms.

### The ledger block
**One** region, read top to bottom, replacing what used to be six separate tiles (a hero
card, four stat cards and a chart card) that competed for the same first glance and
restated each other's numbers:

- **The claim** — the auto-match rate at 44px against its label on one baseline, e.g.
  `93.0% auto-matched`, with the context ranged right: `264 of 284 bank lines settled
  without a human. The remaining 20 are typed, explained and queued below.` Exactly one
  figure of this size per view.
- **The bar it rests on**, directly beneath with no heading between them: a single 22px
  stacked bar over all 284 lines, split tier1 / tier2 / tier3 / escalated, 2px gaps,
  rounded outer ends. Hover gives a tooltip with count, share and a one-line note. Every
  segment is **direct-labelled with its count** in the legend — identity never rests on
  colour alone.
- **The breakdown** — four ruled columns under a hairline, separated by column rules
  rather than being four floating cards: Needs a decision · Escalated value · LLM calls ·
  Deterministic (or "Review feedback" when a reviewer's rejections suppressed candidates).

### Tab strip → panel
`Exception queue` · `Journal entries` · `Tie-out` · `Audit trail`. Switching tabs re-renders
only the strip and the panel, never the whole page.

**Exception queue.** A row of reason-code filter chips, each carrying its own count
(`All 24` / `LOW_CONFIDENCE 4` / `OUT_OF_SCOPE 20`). Then two sections, in two *different
forms*, because they are two different kinds of thing:

- *Needs a decision (4)* — **boxed cards**, the one boxed object on the page. A two-column
  grid: prose left, the credit amount right on the fixed figure track. Bank line id,
  reason-code chip and status pill on the first line; the pipeline's own plain-English
  explanation; then the model's narrative **ruled off below it** under an "Adjudicator
  note." label; a collapsible "Show the N candidates considered" disclosure revealing each
  candidate's id, rule, score, amount delta and the actual transaction ids; then a note
  field and Approve / Reject / Reassign buttons.
- *No action required (20)* — a **ledger table**, not cards: bank line · reason · value
  date · why no action (clipped to its column, full text on the row's title) · credit.
  These are bank credits that were never gateway settlements; declining to match them is
  the correct answer, already taken. Twenty of them wearing the same box as a genuine
  dispute made the screen show 24 identical objects when only 4 were work — overstating
  the review burden 6× in exactly the way the needs/no-action split exists to prevent.
  Every line is still individually listed; nothing is silently dropped.

A candidate the reviewer previously rejected is rendered dashed and dimmed with a
"you rejected this — not re-proposed" pill. Shown, never deleted.

**Journal entries.** A pass/fail banner — a ruled line with a coloured left edge, not a
tinted tile — then a "Close the loop" ruled strip carrying the **Approve postings, then
re-run** button, which approves, re-runs, and reports *zero new postings*. Then a table of
the first 300 of 260-odd batches.

**Tie-out.** The most literally document-shaped tab, and set as one: clean/finding banner;
a reconciliation statement with dot-leader rows (Bank statement / Reconciled /
Unreconciled), amounts on the shared right edge and the total ruled off above rather than
boxed; four controls as ruled columns matching the ledger block's (cash ties out, books
balance, no receivable cleared twice, fee drift absorbed); a movement-by-account table.

**Audit trail.** Newest-first table of the append-only log: timestamp, bank line, decision,
rule/reason, confidence, actor.

## 5. Design decisions already made — please keep these

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
4. **Every figure on the sheet is tabular.** Not a per-component decision: a working
   paper where amounts do not align in a column is not a working paper. The claim
   figure, the four counts, the queue amounts and every table cell all use
   `font-variant-numeric: tabular-nums`.
5. **The needs-review / no-action split stays**, and the two sides take *different
   forms* — boxed cards for decisions, a ledger table for dispositions already correctly
   taken. It is the honest way to report review burden, and the form carries the honesty
   as much as the count does.
6. **A withheld candidate is marked, not hidden.**
7. **The model's narrative is ruled off from the machine-computed account**, never just
   italicised inside it. Everything above that rule is derived by the deterministic
   tiers; everything below is the adjudicator, and it can never alter a figure. This is
   the single distinction the whole product rests on, so it gets real weight.

## 6. Palette reference

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

## 7. Real data to design against

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

## 8. What I want from you

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
