# LedgerLoop

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
and queued rather than silently dropped or guessed at.

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
| Throughput | 37.0 records/sec |
| LLM calls made | 2 (7.1 per 1,000 records) |
| Illustrative cost at published paid rates | ₹0.08 total, ₹0.29 per 1,000 records — actual cost: ₹0 (free tier) |

In finance a wrong match is materially worse than an escalation to a human — a false positive
posts money to the wrong place, an escalation just costs someone five minutes. The system is
deliberately tuned to prefer escalating (`tier2.min_resolve_score=0.7`,
`tier3.confidence_threshold=0.85`) over guessing, and the false-match rate is computed against
the *total* record count, not just the resolved subset, so a system that resolves less but is
wrong about more of what it does resolve can't hide behind a smaller denominator.

## Ablation table (held-out set)

| Row | Resolved | Auto-match | Precision | Recall | False-match | records/sec |
|---|---|---|---|---|---|---|
| Tier 1 only | 164 | 58.6% | 100.0% | 63.1% | 0.00% | 40,858.6 |
| Tiers 1+2 (`--no-llm`) | 249 | 88.9% | 100.0% | 95.8% | 0.00% | 2,751.1 |
| Tiers 1+2+3 (full) | 259 | 92.5% | 100.0% | 99.6% | 0.00% | 78.2 |

**Marginal value of Tier 3:** +10 records auto-matched, recall +3.8pp, false-match rate +0.00pp,
for 2 LLM calls. Tier 3 bought real recall on the held-out set at zero cost in precision — the
candidates it selected among were all correct.

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
- **Integer paise everywhere.** `match/` and `ledger/` are AST-walked in CI to ban `float` in any
  code path touching an amount (`tests/test_money.py`). The one deliberate exception is
  `ui/app.py`'s ₹-display formatting, which is presentation, not accounting.
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

## Build journal

Every genuine failure hit during this build — the diagnosis, the fix, and what I'd do
differently — is in **[FAILURES.md](FAILURES.md)**, written the moment each one happened, not
reconstructed afterward.
