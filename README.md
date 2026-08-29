# LedgerLoop

[![CI](https://github.com/AnshulModi007/ledgerloop/actions/workflows/ci.yml/badge.svg)](https://github.com/AnshulModi007/ledgerloop/actions/workflows/ci.yml)

The badge gates on correctness, not just unit tests: the workflow fails the build if the
false-match rate exceeds 1%, if the auto-match rate falls below 80%, if an approved batch
re-run produces any new postings, or if two `--no-llm` runs over identical inputs diverge.

Multi-source settlement reconciliation agent for the Razorpay AI Buildathon 2026 (Track 04 —
AI Finance Controller). LedgerLoop ingests a gateway transaction log, a settlement report, a
bank statement, and an ERP ledger; matches bank credits back to the transactions that make them
up; proposes the corrective journal entries; and escalates anything it can't resolve as a typed,
plain-English exception instead of guessing.

## Runs with zero API keys

```
git clone <repo> && cd ledgerloop
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate on Linux/Mac
pip install -e ".[dev]"
make demo
```

`make demo` generates the dev dataset, runs the full pipeline with the LLM tier forced off
(`--no-llm`), approves what it resolved, and re-runs the identical command to show **zero new
postings** — the reconciliation loop closed, deterministically, with no credentials. Set
`GEMINI_API_KEY`, `GROQ_API_KEY`, or `OPENROUTER_API_KEY` (see `.env.example`), or run a local
Ollama server, and drop `--no-llm` to let Tier 3 adjudicate the remainder instead of escalating
it. `make ui` (needs `pip install -e ".[dev,ui]"`) puts the same pipeline behind a Streamlit
dashboard.

Two setup notes from real friction hit during this build (full account in FAILURES.md):
requires **Python ≥3.11** (the exception taxonomy uses `enum.StrEnum`) — on Windows, use the
`py` launcher (`py -0p` to list interpreters, `py -3.11 -m venv .venv` to target one) if `python`
on `PATH` resolves to something older; and plain **Windows without WSL has no `make`** — every
`Makefile` target is a thin wrapper around a `python -m ledgerloop....` command, so run that
directly (e.g. `python -m ledgerloop.reconcile --profile dev --no-llm --approve` for what `make
demo`'s second line does) if `make` isn't on your `PATH`.

## Architecture: five tiers, held-out set

Resolution percentages below are from the **held-out set** (seed 1337, 280 bank lines),
evaluated exactly once — see [Results](#results) for the full report.

```
 gateway txns ─┐
 settlements   ├─▶ Tier 0: normalise (ingest/) ─▶ Tier 1: exact join ─▶ Tier 2: algorithmic
 bank stmt     │      dates→UTC, amounts→paise      (UTR, amount)         fee model + subset-sum
 ERP ledger   ─┘      regex UTR/RRN extraction       58.6% resolved       30.4% resolved
                                                              │
                              ┌───────────────────────────────┘
                              ▼
                    Tier 3: LLM adjudication ──▶ Exception queue (human review)
                    candidate menu, never a         + proposed journal entries
                    blank field; abstain <0.85          7.5% escalated
                    3.6% resolved
```

92.5% auto-matched without a human touching anything; the remaining 7.5% is typed, explained,
and queued rather than silently dropped or guessed at. 20 of those 21 escalations are credits
that were never gateway settlements, where declining to match is itself the right answer — so
**99.6% of lines were disposed of correctly** and the queue a human actually works was one
line. Both rates are reported, always together; see [Correct
disposition](#correct-disposition-declining-to-match-is-also-a-right-answer).

## What an escalation actually looks like

The exception queue is the human-facing half of the product, so an escalation has to carry
its own reasoning. Every figure below is computed by the pipeline — no model is involved,
and this is what you see with **zero API keys configured**:

> **BANK00115** — `AMOUNT_MISMATCH_BEYOND_TOLERANCE`
> Bank credit of ₹34,748.47 on 02 Feb 2026 carries UTR UTR17367488852335, which identifies
> settlement batch STL00098 (5 transactions, net ₹54,128.26). The ₹19,379.79 difference
> exceeds the ₹2.00 tolerance. The linkage is near-certain, so this is an amount discrepancy
> for a human to price, not a matching failure.

> **BANK00093** — `LOW_CONFIDENCE`
> Bank credit of ₹41,494.74 on 18 Jan 2026. The strongest of 1 candidate groups 3
> transactions by cross-batch subset-sum search, but scores 0.55 against a 0.70 resolve
> threshold. Escalated for review rather than matched on a weak signal.

When an LLM *is* configured, its reasoning is appended as a labelled `Adjudicator note:`
rather than replacing this text. That's deliberate: the numbers a reviewer acts on stay
machine-derived, and the model can add narrative around them but can never alter one. It's
the same containment argument as the fixed candidate menu, applied to the explanation
surface. See `src/ledgerloop/exceptions/explain.py`.

## The thesis

> The LLM does **extraction, selection, and explanation**. It never does arithmetic, and it
> never invents a match.

Tier 2 is deterministic Python: a fee model (platform fee + GST + TDS, integer paise, round-half
-up) reconstructs the expected net from the gross, and a bounded subset-sum search finds which
transactions sum to a bank credit. It produces **candidate sets**, never final answers on its
own authority when ambiguous. Tier 3 receives a bank credit and a *fixed list of candidate IDs*
that Tier 2 already generated — it can select one of them, or abstain, and it validates every
response with Pydantic before trusting it. It is structurally incapable of returning a
transaction ID that Tier 2 didn't already propose, and incapable of computing a sum the pipeline
then trusts. That's the whole safety argument for using an LLM on money at all.

## Results

Held-out set, seed 1337, 280 bank lines, live Groq (`openai/gpt-oss-20b`) for Tier 3 — the
single, final evaluation run per the project's own holdout rule (see below), no config changes
made afterward:

| Metric | Value |
|---|---|
| Auto-match rate | 92.5% (259/280) |
| Precision | 100.0% |
| Recall | 99.6% |
| **False-match rate** (primary risk metric) | **0.00%** (0/280) |
| Missed (escalated instead of matched) | 1 |
| Tier 1 (exact) | 164 records — 58.6% |
| Tier 2 (algorithmic) | 85 records — 30.4% |
| Tier 3 (LLM adjudication) | 10 records — 3.6% |
| Exceptions | 21 records — 7.5% (`OUT_OF_SCOPE` × 20, `AMOUNT_MISMATCH_BEYOND_TOLERANCE` × 1) |
| **Correct disposition** (matched right + correctly refused) | **99.6%** (279/280) |
| Queue a human actually works | 1 record (the other 20 exceptions need no decision) |
| Throughput | 37.0 records/sec |
| LLM calls made | 2 (7.1 per 1,000 records) |
| Illustrative cost at published paid rates | ₹0.08 total, ₹0.29 per 1,000 records — actual cost: ₹0 (free tier) |

### Correct disposition: declining to match is also a right answer

20 of those 21 escalations are `OUT_OF_SCOPE` — bank credits that were never gateway
settlements at all (direct transfers, refund reversals). Ground truth says they have no
match to make, so refusing to match them is the correct outcome, already reached. Auto-match
rate scores every one of them as a failure and leaves them sitting in the review queue, which
overstates the backlog by 20x: the queue a human actually had to work on the held-out set was
**one line**, the `AMOUNT_MISMATCH_BEYOND_TOLERANCE` shown earlier.

So the harness reports **correct disposition** — matched correctly, or correctly left
unmatched — beside the strict rate: **99.6% (279/280)**. The one shortfall is a genuine miss.

Three things keep this from being a softer number to hide behind. The strict auto-match rate
is always printed beside it. Resolving an out-of-scope line counts as a **false match**, never
a correct rejection, so the disposition rate can't be reached by matching things that
shouldn't be matched — `tests/test_eval.py` asserts exactly that. And the analyst-hour figures
below still charge full review time for all 21 exceptions, including the 20 the queue marks
no-action, because a reviewer still lays eyes on them.

The dashboard makes the same split: "Needs a decision" is the working list, and the
auto-dispositioned lines are collapsed below it — demoted, never dropped. The split is
computed from reason codes alone, with no ground truth involved, so it behaves identically
on data that has no answer key (`src/ledgerloop/exceptions/queue.py`).

These held-out figures are **arithmetic on the numbers already published above** — 259
correct resolutions, 0 false matches, 20 lines with no match to make — not a second
evaluation run. The holdout is still scored exactly once.

### Weighted by value, not just by line count

Resolving most of the *lines* while leaving most of the *money* escalated would be a
materially different result, so the harness reports both. On the dev set (5,000 transactions,
284 bank lines):

| Metric | Value |
|---|---|
| Lines auto-matched | 91.5% (260/284) |
| **Value auto-reconciled** | **99.7%** — ₹4,76,97,961.88 of ₹4,78,56,920.92 |
| Value escalated for review | ₹1,58,959.04 |
| Illustrative analyst effort | 18.9h manual → 0.8h reviewing exceptions |

The two rates diverge because escalations skew heavily toward small-value `OUT_OF_SCOPE`
lines — direct transfers and refund reversals that were never gateway settlements. Under
1% of the money in the batch needs a human.

The analyst-hour figures are **illustrative**, derived from measured counts times documented
per-item assumptions (4 min to trace a credit by hand, 2 min to review a pre-explained
exception); the constants and their reasoning are in `eval/metrics.py`'s module docstring,
and you should substitute your own desk's numbers before quoting them. The value figures are
measured, in integer paise.

These value-weighted metrics were added after the held-out set had already been evaluated,
and the held-out run was **not** repeated to obtain them — running it exactly once is the
rule, and re-running it to fill in a nicer table is precisely what that rule exists to
prevent. The dev-set figures are labelled as such above.

### Why false-match rate is the metric that matters

In finance a wrong match is materially worse than an escalation to a human — a false positive
posts money to the wrong place, an escalation just costs someone five minutes. The system is
deliberately tuned to prefer escalating over guessing, and the false-match rate is computed
against the *total* record count, not just the resolved subset, so a system that resolves less
but is wrong about more of what it does resolve can't hide behind a smaller denominator.

An earlier version of this paragraph credited `tier2.min_resolve_score=0.7` and
`tier3.confidence_threshold=0.85` for that safety. Sweeping the thresholds showed the first of
those does nothing of the kind — see [Threshold
sensitivity](#threshold-sensitivity-what-the-tuning-actually-buys) for what does.

## Ablation table (held-out set)

| Row | Resolved | Auto-match | Precision | Recall | False-match | records/sec |
|---|---|---|---|---|---|---|
| Tier 1 only | 164 | 58.6% | 100.0% | 63.1% | 0.00% | 40,858.6 |
| Tiers 1+2 (`--no-llm`) | 249 | 88.9% | 100.0% | 95.8% | 0.00% | 2,751.1 |
| Tiers 1+2+3 (full) | 259 | 92.5% | 100.0% | 99.6% | 0.00% | 78.2 |

**Marginal value of Tier 3:** +10 records auto-matched, recall +3.8pp, false-match rate +0.00pp,
for 2 LLM calls. Tier 3 bought real recall on the held-out set at zero cost in precision — the
candidates it selected among were all correct.

## Per-defect-class results (dev set, `--no-llm`)

One aggregate number can't tell you whether a system nails the easy cases and folds on the
hard ones. The generator tags every bank line in the answer key with the defect classes it
carries, so the harness scores each of the twelve independently. This is the deterministic
tiers alone — no LLM, no API key — which is what makes it the honest read on how much of
the problem Python actually solves:

| Defect class | n | Matched | Correctly refused | Missed | Correct |
|---|---|---|---|---|---|
| `BATCH_N1` | 104 | 104 | — | 0 | 100.0% |
| `CHARGEBACK` | 20 | 20 | — | 0 | 100.0% |
| `CLEAN` | 40 | 40 | — | 0 | 100.0% |
| `DUPLICATE` | 20 | 20 | — | 0 | 100.0% |
| `FEE_DRIFT` | 20 | 20 | — | 0 | 100.0% |
| `INJECTION` | 20 | 20 | — | 0 | 100.0% |
| `REFUND_NET` | 20 | 20 | — | 0 | 100.0% |
| `TRANSPOSE` | 20 | 20 | — | 0 | 100.0% |
| `OUT_OF_SCOPE` | 20 | — | 20 | 0 | 100.0% |
| `SPLIT_1N` | 40 | 38 | — | 2 | 95.0% |
| `MONTH_CROSS` | 20 | 18 | — | 2 | 90.0% |
| `NO_UTR` | 20 | 17 | — | 3 | 85.0% |

**False matches: zero in every class.** Where the deterministic tiers fall short they fall
short by escalating, never by guessing — which is the design claim, made per-class instead
of in aggregate.

The three imperfect rows are the honest picture of where the hard problems are, and they are
exactly the classes with no reliable join key: `NO_UTR` (the narration carries no UTR at all,
so there is nothing to join on and only a subset-sum over amounts remains), `MONTH_CROSS` (a
T+2 settlement landing across a month boundary, where the date window stops helping), and
`SPLIT_1N` (one batch paid out across two credits, so no single credit equals any batch
total). These are precisely the cases Tier 3 exists for — the ablation above is the same
claim measured end-to-end.

A line carrying two defect classes is counted under each, so the rows sum to more than the
284 records in the batch. That overlap is intentional: these are per-class rates, not a
partition of the batch, and `tests/test_eval.py` asserts the double-count so nobody
"corrects" it later. A test also fails the build if any class stops appearing in this table
at all — a class silently dropping out of scoring is exactly what a single aggregate hides.

Regenerate with `python -m ledgerloop.eval.metrics --profile dev --no-llm`.

## Threshold sensitivity: what the tuning actually buys

Claiming a system is "tuned to escalate rather than guess" is easy; showing the curve is not.
`python -m ledgerloop.eval.sensitivity --profile dev` re-runs the deterministic pipeline once
per setting of each Tier 2 threshold, changing one value and nothing else, and reports what
every setting would have cost or bought.

**Running it corrected a claim this README was making.** Three of the four knobs do not govern
correctness at all:

| Knob | Swept range | Where it first posts a wrong match |
|---|---|---|
| `tier2.amount_tolerance_paise` | ₹0 → ₹5,000 | **₹5,000** (5 false matches, precision 97.8%) |
| `tier2.min_resolve_score` | 0.00 → 0.90 | never |
| `tier2.date_window_days` | 1 → 30 | never |
| `tier2.ambiguity_margin` | 0.00 → 0.50 | never |

`min_resolve_score` can be dropped from the shipped 0.70 all the way to **0.00** — "resolve the
best candidate no matter how weak its score" — and the dev set still comes back with 100%
precision and zero false matches. It buys 4 extra auto-matches and costs nothing. The same holds
for the date window and the ambiguity margin.

So the score threshold is not the safety mechanism this README previously credited. What
actually prevents false matches sits upstream of it, in candidate *generation*: a candidate set
that never contains a wrong grouping cannot be mis-scored into one, whatever the threshold is
set to. The score threshold's real job is narrower and still worth having — it decides how much
work reaches a human, trading 24 auto-matches against 24 review items across its range.

The knob that does govern correctness is the amount tolerance, and there the margin is wide:

| `amount_tolerance_paise` | Resolved | Precision | False matches | Review queue |
|---|---|---|---|---|
| 0 (exact) | 240 | 100.0% | 0 | 23 |
| 100 (₹1) | 260 | 100.0% | 0 | 4 |
| **200 (₹2) — shipped** | **260** | **100.0%** | **0** | **4** |
| 1,000 (₹10) | 260 | 100.0% | 0 | 5 |
| 5,000 (₹50) | 258 | 100.0% | 0 | 18 |
| 50,000 (₹500) | 252 | 100.0% | 0 | 28 |
| 500,000 (₹5,000) | 230 | 97.8% | **5** | 54 |

The shipped ₹2.00 sits **2,500x** below the first setting that posts a wrong entry, and ₹2.00 is
not arbitrary — it is the width of the fee-rounding drift the `FEE_DRIFT` defect class actually
produces. Note also that loosening past ₹50 makes the system *worse on both axes at once*: fewer
matches and more review, because sloppy tolerances turn clean single-candidate matches into
ambiguous multi-candidate ones.

`tier3.confidence_threshold` is not swept. Doing it honestly means re-adjudicating every
ambiguous case against a live model once per point — the adjudicator stores its decisions but
not the raw per-candidate confidences a post-hoc sweep would need — and running the holdout
repeatedly, which the once-only rule forbids. `eval/sensitivity.py` says so in its docstring
rather than publishing a curve it cannot compute.

## Scale: 100,000 transactions

Every accuracy figure above comes from a 5,000-transaction batch, which says nothing about
volume. `make scale` (~2 min, ~45 MB of generated CSV, gitignored) generates the same dataset
shape at four sizes and runs the identical `--no-llm` pipeline over each. Defect density is held
constant as volume rises — leaving the defect count fixed while transactions grow would dilute
the hard cases and make the big runs *easier* than the small ones.

| Transactions | Bank lines | Pipeline time | Lines/sec | Auto-match | Correct disposition | False matches | Search timeouts |
|---|---|---|---|---|---|---|---|
| 5,020 | 281 | 0.1s | 2,626 | 89.3% | 96.4% | 0 | 0 |
| 20,080 | 1,120 | 0.9s | 1,254 | 90.6% | 97.8% | 0 | 0 |
| 50,200 | 2,825 | 16.3s | 173 | 90.7% | 97.7% | 0 | 0 |
| 100,400 | 5,645 | 53.8s | 105 | 90.2% | 97.3% | 0 | 0 |

**Accuracy is flat across a 20x range; cost per line is not.** Auto-match holds between 89.3%
and 90.7% with zero false matches at every size, but per-line cost rises 25x from the smallest
run to the largest. That shape is expected and worth stating plainly: Tier 2's candidate search
widens as more transactions fall inside each date window, so the work per bank line grows with
total volume, not just with the number of lines.

100,400 transactions is where a single-process pure-Python run stops being interactive — not
where anything breaks. Nothing failed at that size: the subset-sum node budget
(`tier2.subset_sum_node_budget=20000`) was never exhausted, so `TIER2_TIMEOUT` never fired. That
matters because of *how* this pipeline degrades under load — when the bounded search runs out of
budget it emits a typed escalation, never a guess. The failure mode at volume is more work for a
human, never a wrong posting, and `eval/scale.py` counts those timeouts explicitly so the signal
to raise the budget is visible before it costs recall.

Not measured: peak memory. Doing it portably needs `psutil`, which is not a dependency, and
`tracemalloc`'s overhead would distort the very timings the benchmark exists to report.

## Calibration table

The held-out run's own Tier 3 resolutions (2 calls, 10 candidate selections) landed entirely
inside the accepted-confidence band with no rejects, so there weren't enough *distinct*
confidence values that run to populate a reliability table — and a second holdout run to get a
nicer-looking table would defeat the point of evaluating it exactly once (see
[FAILURES.md](FAILURES.md), 2026-08-25 entry, for the honest account of why). The table below is
from the dev-set run gathered during Phase 5 build, reported here as the illustrative
reliability read:

| Confidence bin | n | Mean stated | Actual accuracy | Gap |
|---|---|---|---|---|
| [0.80, 0.90) | 2 | 85.0% | 100.0% | −15.0% |
| [0.90, 1.00) | 2 | 90.0% | 100.0% | −10.0% |

Both bins are *underconfident* — actual accuracy exceeded stated confidence — which argues the
0.85 threshold has margin to spare rather than needing to move up. Bins below 0.80 are
structurally empty by design: `tier3.confidence_threshold=0.85` converts anything under that to
a `LOW_CONFIDENCE` abstain before it ever becomes a resolution this table could bin. Sample
sizes here are small (n=4 total) — this is a directional read, not a statistically powered one.

## Design decisions

- **Fixed candidate menus, never free-form matching.** Every Tier 3 call receives IDs Tier 2
  already produced; a response naming anything else is discarded and retried, then recorded as
  `TIER3_INVALID_SELECTION` if it never resolves. This is the actual mechanism that makes
  hallucinated reconciliation structurally impossible, not just unlikely.
- **Integer paise everywhere.** `match/`, `ledger/` and `exceptions/` are AST-walked in CI to
  ban `float` in any code path touching an amount (`tests/test_money.py`). `exceptions/` is in
  that set because `explain.py` renders paise into the rupee strings a reviewer reads before
  approving a posting — that is a money path in every sense that matters. Rendering goes
  through one integer `divmod` formatter with Indian digit grouping, so there is now no float
  in any money path in the codebase, presentation included.
- **A provider fallback chain that always terminates in `NullProvider`.** Gemini → Groq →
  OpenRouter → local Ollama → abstain-on-everything. `make demo` exercises the last link on
  purpose so the zero-key path is a first-class, CI-tested mode, not a fallback nobody runs.
- **One orchestration path for CLI and dashboard.** `pipeline.py` is the single source of truth
  both `reconcile.py` and `ui/app.py` call into, so they cannot report different numbers for the
  same run.
- **The false-match rate gate uses the full record count as its denominator**, not the resolved
  subset — see [Results](#results) above for why.

### Where we chose not to use AI, and why

- **Fee reconstruction, subset-sum matching, date-window and amount-tolerance logic** are all
  plain deterministic Python (`match/fee_model.py`, `match/tier2_algorithmic.py`). These are
  arithmetic and combinatorial search problems with exact, auditable answers — an LLM would add
  latency, cost, and a class of failure (a plausible-looking wrong number) that a bounded search
  algorithm simply doesn't have.
- **Exact `(UTR, amount)` joins (Tier 1)** need no interpretation at all; running an LLM over a
  hash-map lookup would be pure overhead for zero benefit.
- **Idempotency and the approved-postings ledger** (`ledger/journal.py`, `pipeline.py::approve`)
  are keyed, deterministic bookkeeping — correctness here means "the same key always produces
  the same answer," which is precisely the property an LLM cannot guarantee and a dict lookup
  can.
- **The final held-out score** is computed by `eval/metrics.py` against `answer_key.json` with
  plain set equality (`eval/harness.py::is_correct_resolution`) — grading is not a judgment call
  the model is asked to make about its own work.

## Non-goals and known limitations

Out of scope by design, not by oversight:

- Real bank or gateway API integration
- Multi-currency and FX revaluation
- Authentication, multi-tenancy, RBAC
- Production deployment, horizontal scaling
- Forecasting or anomaly prediction

Known limitations:

- Tier 3 confidence is **not** run-to-run deterministic against a live model
  (`tests/test_determinism.py` scopes the determinism guarantee to `--no-llm` only; see
  FAILURES.md, 2026-08-23 and 2026-08-25 entries, for two concrete instances of this).
- Free-tier LLM quotas and model names churn; verify current ones before relying on specific
  figures (`src/ledgerloop/adjudicate/provider.py` documents where to re-check each provider).
- Local Ollama (`llama3.1`, 8B) is the offline fallback the provider chain falls through to when
  no cloud key is set and an Ollama server is reachable on `localhost:11434` — this path is
  implemented and covered by the same fallback tests as the cloud providers, but was not
  hardware-verified in this build's own environment. Expect roughly 8GB of RAM headroom for an
  8B quantized model as a starting point, and confirm on your own machine.
- Illustrative LLM cost figures use assumed token counts and a fixed USD/INR rate, documented in
  full in `eval/metrics.py`'s module docstring — they show order of magnitude, not a bill.
- Single-process throughput falls off well before it fails: measured at 100,400 transactions in
  53.8s with no loss of accuracy (see [Scale](#scale-100000-transactions)), but per-line cost
  grows with total volume, so a batch an order of magnitude larger again would want the
  candidate search parallelised or windowed by date before it stayed interactive. Horizontal
  scaling is a stated non-goal above; this is a measurement of what that costs today, not a
  claim that it is solved.

## Build journal

Every genuine failure hit during this build — the diagnosis, the fix, and what I'd do
differently — is in **[FAILURES.md](FAILURES.md)**, written the moment each one happened, not
reconstructed afterward.
