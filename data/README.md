# Data

Two synthetic datasets, both produced by `src/ledgerloop/generate/generator.py` from
`config.yaml` — nothing in either directory is hand-edited.

- **`dev/`** — seed `42`. Use this freely during development, debugging, and manual inspection.
- **`holdout/`** — seed `1337`.

## The holdout honesty rule

**Do not inspect `data/holdout/answer_key.json` (or write code that reads it) outside of
`src/ledgerloop/eval/harness.py`.** The held-out set exists to give one honest, un-gamed read of
the system at the end of the build. Tuning thresholds against it, eyeballing its ground truth, or
writing test assertions against its specific rows defeats the purpose — you'd be fitting to the
answer key instead of measuring against it. `eval/harness.py` reads it exactly once, at final
evaluation, per section 5 of `IMPLEMENTATION.md`.

CI enforces this: a check fails the build if any file under `holdout/` is referenced from
outside `src/ledgerloop/eval/`.

## Why the line counts differ from the brief's "~120 bank credits"

The brief asks for every one of the 12 defect classes to appear at least 20 times in the dev set.
Eight of those classes are batch/bank-line-level (one instance = roughly one bank statement
line), so the floor alone already implies on the order of 160+ lines before any padding or CLEAN
volume is counted. We held the >=20-per-class floor — it's the acceptance criterion — and let the
bank-line count land wherever that implies. Bulk padding volume (to reach the ~5,000 gateway
transaction target) is folded into extra `BATCH_N1`-shaped batches — many transactions settling
as one credit, which is what a real high-volume merchant's settlements look like — rather than
thousands of individual 1:1 `CLEAN` lines. See `manifest.json` in each profile directory for the
actual counts and the per-defect-class breakdown for that run.

## Files, per profile directory

| File | Contents |
|---|---|
| `gateway_transactions.csv` | Gross payments captured, with order IDs and RRNs. |
| `settlement_report.csv` | Per-transaction settlement lines: fee, GST on fee, TDS, refund/chargeback, net, batch ID, payout UTR. |
| `bank_statement.csv` | Lump-sum bank credits with free-text narration. |
| `erp_ledger.csv` | The merchant's own invoice ledger — what it believes it's owed. |
| `answer_key.json` | Ground truth: every bank line's true matched transaction IDs and injected defect class(es). |
| `manifest.json` | Seed, config hash, row counts, and defect-class counts for that run. |

Regenerate both with `make generate`. Same seed + same `config.yaml` always produces
byte-identical files — see `tests/test_determinism.py`.
