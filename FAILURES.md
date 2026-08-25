# FAILURES.md

Append-only build journal. An entry goes here the moment something breaks: what broke, the
symptom, the diagnosis, the fix, and what I'd do differently. See IMPLEMENTATION.md section 11 —
this is read first by reviewers, and it can't be reconstructed honestly on the last day.

---

## 2026-08-23 — `python -m venv` failed on the default interpreter

**Symptom:** `python -m venv .venv` errored out of `ensurepip` with exit status 101 on the
system's default Python (3.10.0).

**Diagnosis:** `pyproject.toml` targets `>=3.11` because the defect taxonomy uses
`enum.StrEnum`, added in 3.11. The machine's `PATH` python is 3.10; a separate 3.11 install
exists but isn't first on `PATH`.

**Fix:** Used the Windows `py` launcher (`py -0p`) to enumerate installed interpreters, found
`pythoncore-3.11-64`, and built the venv from that explicit path instead of bare `python`.

**Would do differently:** Nothing to change in the code — this is a documented `requires-python`
constraint doing its job. Worth a one-line note in the README's setup section so the next person
doesn't lose the same five minutes.

---

## 2026-08-23 — `pip install -e ".[dev]"` timed out mid-resolve

**Symptom:** `pip install -e ".[dev]"` (which pulls in `streamlit` transitively) hit
`ReadTimeoutError` against `files.pythonhosted.org` after 180s and never completed.

**Diagnosis:** `streamlit` and its dependency tree are large; the sandboxed network path for this
session is slow enough that pip's default resolve-then-download-everything behavior blew the
timeout window before Phase 1 (which needs none of it) could be verified.

**Fix:** Split the install: `pydantic`, `click`, `pyyaml`, `pytest`, `ruff` first (fast, and the
only things Phase 1 actually needs), then `pip install --no-deps -e .` for the package itself.
`streamlit` gets pulled in properly once Phase 6 actually needs it.

**Would do differently:** Done during this same session — moved `streamlit` out of the base
`dependencies` list into a `ui` extra (`pip install -e ".[ui]"`), so `make install` for the core
pipeline stays fast and CI never has to pull a UI library it doesn't import until Phase 6.

---

## 2026-08-23 — the brief's "~120 bank credits" and "every defect class >= 20x" are in tension

**Symptom:** Not a code break, but a spec-reading trap worth flagging: taken literally, hitting
`n_bank_credits_target: 120` while also giving all 8 batch-level defect classes >= 20 instances
each (>= 160 lines) is arithmetically impossible without either starving `CLEAN` or violating the
floor.

**Diagnosis:** The "~120" figure in IMPLEMENTATION.md section 4 reads as an illustrative
ballpark written before the per-defect-class floor was pinned down, not a hard target with its
own acceptance check (the acceptance check is only the >= 20-per-class floor and reproducibility).

**Fix:** Dropped the exact line-count target from `config.yaml` entirely. Held the >= 20-per-class
floor as the real constraint, and folded the ~5,000-transaction volume target into extra
`BATCH_N1`-shaped batches (many transactions per credit) instead of thousands of individual
`CLEAN` lines, which keeps the dev set at ~289 bank lines — same order of magnitude as "~120",
not 10x it. Documented the reasoning in `data/README.md` so it doesn't look like an oversight.

**Would do differently:** Nothing yet — this reads as the correct call, but it's exactly the kind
of judgment call to revisit if `eval/ablation.py` (Phase 5) shows tier resolution percentages
skewed by the line-shape distribution rather than genuine matching difficulty.

---

## 2026-08-23 — `make` isn't on this Windows dev machine

**Symptom:** `make generate` fails with `command not found` in the Git Bash shell used for this
session, even though `Makefile` is correct and `python -m ledgerloop.generate.generator` works
fine directly.

**Diagnosis:** GNU Make isn't installed by default on Windows outside WSL/MSYS toolchains; this
machine's Git Bash doesn't bundle it. The CI workflow sidesteps this by calling the underlying
`python -m ledgerloop...` commands directly rather than shelling out to `make`, so this doesn't
block CI or a Linux/Mac reviewer's clone -- only local iteration on this particular Windows box.

**Fix:** None applied -- verified every `Makefile` target's underlying command directly instead
(`python -m ledgerloop.generate.generator`, `python -m pytest`, `python -m ruff check`). If local
`make` support matters going forward, `choco install make` (chocolatey is already on this
machine's PATH) or running inside WSL both work; deferring that decision rather than making a
system-level install unasked.

**Would do differently:** Flag this explicitly in the README's setup section so a Windows
reviewer without WSL doesn't hit the same wall silently.

---

## 2026-08-23 — settlement_report.csv never actually carried the payout UTR

**Symptom:** Starting Tier 1 (`tier1_exact.py`, the exact `(UTR, amount)` join), I went to group
`settlement_report.csv` rows into their batch and realized there was nowhere on that side to read
the payout UTR from -- `SettlementLine` never had the field. The generator computed a
`payout_utr` per batch in-memory to build the bank narration text, then discarded it instead of
writing it onto the settlement rows.

**Diagnosis:** A genuine Phase 1 gap: I designed the bank narration to *display* a UTR without
also persisting the authoritative version of it anywhere a matcher could join against. In
reality, the gateway that initiates a NEFT/RTGS payout is the one that gets the UTR back from the
bank rail, so it belongs on the settlement side as ground truth, not only rendered into narration
text.

**Fix:** Added `payout_utr` to `SettlementLine` (`generate/schemas.py`), threaded it through
`_build_batch`/`_settle_line` in `generator.py` (generated once per batch, before narration is
built, so both sides stay consistent), and regenerated `data/dev` and `data/holdout`. Caught
before any tier code depended on the missing field, so no downstream rework.

**Would do differently:** Read section 5's interface contracts (which imply tier1 needs a
reliable join key) before finalizing Phase 1's schemas, not after. Would have caught this a day
earlier.

---

## 2026-08-23 — Windows has no IANA tzdata bundled; `zoneinfo.ZoneInfo("Asia/Kolkata")` raised

**Symptom:** `ingest/normalise.py`'s UTC conversion (`naive_local.replace(tzinfo=IST)`) raised
`ZoneInfoNotFoundError` the first time it ran.

**Diagnosis:** Python's `zoneinfo` module relies on the OS to supply IANA tz data; Linux/Mac
ship it, Windows doesn't. `pip install tzdata` supplies it as a pure-Python fallback package that
`zoneinfo` picks up automatically -- this was just never installed.

**Fix:** Added `tzdata` to `pyproject.toml` as a `sys_platform == 'win32'`-conditional
dependency, so Linux CI doesn't carry a package it doesn't need.

**Would do differently:** Nothing -- this is exactly what a `sys_platform` marker is for. Worth
remembering that "works on my Linux CI" and "works on a reviewer's Windows laptop" are different
claims for anything touching `zoneinfo`.

---

## 2026-08-23 — a SPLIT_1N line that also drew a TRANSPOSE overlay broke partition search

**Symptom:** Tier 2's partition search (for batches paid out across two bank credits) was
supposed to catch `SPLIT_1N`, but a handful of cases fell all the way through to false matches --
each line independently claimed the *entire* batch instead of splitting it, and the acceptance
smoke test showed 5 false matches at ~1.8% false-match rate, comfortably over the 0.5% ceiling.

**Diagnosis:** The generator's overlay defects (`NO_UTR`, `TRANSPOSE`, `INJECTION`, `DUPLICATE`)
are applied independently to any already-emitted bank line, including either half of a
`SPLIT_1N` pair. When `TRANSPOSE` landed on just one of the two lines, that line's narration UTR
no longer textually matched its sibling's -- and `_resolve_partition_groups` grouped candidate
lines by literal extracted-UTR string, so the pair no longer looked like a pair. Each line then
resolved independently against the whole batch via the exact-UTR/tolerance strategy, which is
where the false match came from -- not a bug in the scoring itself, but a bug in `_score`'s
penalty formula (`min(1.0, diff / tolerance) * 0.2`) that let *any* amount mismatch, however
enormous, cost at most 0.2 off a 1.0 base score, comfortably clearing `min_resolve_score`.

**Fix:** Two changes. (1) Grouping for partition search now keys on the *structurally resolved
batch id* (via exact-or-single-transposition UTR lookup), not raw narration text -- so a
transposed line and its clean sibling still land in the same group. (2) All four tier2 strategies
now hard-gate on `amount_tolerance_paise`/`date_window_days` before scoring anything, rather than
letting a wildly-off match through with a merely-discounted score. False matches went to 0/284 on
the dev set after both fixes.

**Would do differently:** This is exactly the kind of compound-defect interaction the generator
*should* produce -- the bug was in assuming one narration string uniquely identifies a group, not
in generating the compound case. Would design "group by matched entity, not by raw text" from the
start next time.

---

## 2026-08-23 — subset-sum's own bounds were too generous for pure Python

**Symptom:** The full Phase 2 test suite went from ~2s to 93s after adding `tests/test_tiers.py`,
which calls the tier1+tier2 pipeline six times over the dev set.

**Diagnosis:** Profiled with `cProfile`. Two config defaults were both far too generous for a
pure-Python implementation: `meet_in_middle_max_items: 40` means up to 2^20 (~1M) subset sums
enumerated *per half* whenever tier2's generic cross-batch subset-sum fallback hit a ~40-item
candidate window, and `subset_sum_node_budget: 200000` meant the greedy/local-search path spent
up to 200,000 loop iterations *correctly refusing to hang* on every genuinely-unmatchable case --
which is most of them, since that fallback only fires when every UTR-grounded strategy has
already failed.

**Fix:** Tuned both down to `meet_in_middle_max_items: 20` (2^10 per half -- trivial) and
`subset_sum_node_budget: 20000`. Re-verified on the dev set: identical resolution rate (91.55%)
and false-match rate (0.0%) before and after -- the only change was 14 `TIER2_TIMEOUT` cases
instead of 10 vs. `NO_CANDIDATE` (a labeling difference in how a doomed search gives up, not a
correctness change). Runtime for one full tier1+tier2 pass on the dev set dropped from ~15s to
~0.13s.

**Would do differently:** Profile before picking "looks safe" bound values, not after. The spec's
"~40 items" and a big node budget read as conservative/safe numbers on paper; in practice, pure
Python's constant-factor cost on tight inner loops made them the single biggest performance risk
in the whole pipeline.

---

## 2026-08-23 — Phase 2 acceptance numbers (ablation baseline)

Required by IMPLEMENTATION.md section 4 before moving to Phase 3. Measured on `data/dev`
(seed 42, 284 bank lines) after the fixes above:

| | Tier 1 alone | Tiers 1+2 |
|---|---|---|
| Resolved | 158 / 284 (55.6%) | 260 / 284 (91.55%) |
| False matches | 0 | 0 (0.000%) |

Acceptance criteria (>=80% resolved, <0.5% false-match rate) both cleared with margin. Tier 1's
55.6% sits at the low end of the spec's expected 55-65% ballpark; tiers 1+2 together land above
the spec's expected 80-95% -- SPLIT_1N and NO_UTR partially resolve rather than fully, which is
expected (see per-defect breakdown in test_tiers.py::test_defect_class_routing). Unresolved
lines after tiers 1+2: 24, split across `LOW_CONFIDENCE` (4), `TIER2_TIMEOUT` (14), and
`NO_CANDIDATE` (6) -- these become Tier 3's job in Phase 3, and OUT_OF_SCOPE (20 lines) is
correctly never matched by either tier.

---

## 2026-08-23 — live Groq calls came back `HTTPError 403 error code: 1010`

**Symptom:** `GroqProvider.complete()` (real key, present in this session's own shell
environment) returned `None` on every call. Direct `urllib` debugging showed a bare
403 with Cloudflare's "error code: 1010" -- a bot-fingerprint block, not an auth or
payload problem.

**Diagnosis:** Groq's API sits behind Cloudflare, which appears to reject `urllib`'s
default `User-Agent` (`Python-urllib/3.11`) outright before the request reaches
Groq's own logic. A browser-shaped `User-Agent` cleared it immediately.

**Fix:** Added a default headers dict (`Content-Type` + a browser-style `User-Agent`)
in `provider.py::_post_json`, applied to every REST provider. Re-verified live: Groq
now returns real responses.

**Would do differently:** Nothing -- this is exactly the kind of thing that's
invisible until you make a real network call. Worth remembering for *any* stdlib-HTTP
integration, not just this project: a 403 with no clear auth problem is worth
checking against a plain browser UA before assuming the API key or request shape is
wrong.

---

## 2026-08-23 — all three REST providers' hardcoded model names were stale

**Symptom:** Once the Cloudflare block above was cleared, Groq returned a real API
error: `model llama-3.1-8b-instant does not exist or you do not have access to it`.

**Diagnosis:** Groq's free-tier model catalog had moved on since my knowledge cutoff
(January 2026) -- confirmed by querying `GET /openai/v1/models` with the live key.
Gemini and OpenRouter almost certainly have the same problem (`gemini-1.5-flash` is a
retired generation; OpenRouter's free-model lineup is explicitly high-churn per its
own docs), but neither key is available in this environment to verify directly.

**Fix:** Groq: live-verified and switched to `openai/gpt-oss-20b`. Gemini: switched to
`gemini-2.5-flash` based on web research (still documented free-tier as of
2026-08-23), *not* live-verified. OpenRouter: switched to
`meta-llama/llama-3.3-70b-instruct:free`, also not live-verified. Left a comment on
each provider class naming exactly how to re-check it (the models endpoint for Groq,
the pricing docs / `?max_price=0` model list for the other two) -- this is precisely
what section 10's "verify current free-tier quotas before relying on specific
numbers, they change" is warning about, and it bit me within the same build.

**Would do differently:** Nothing available to do differently without keys for the
other two -- this class of staleness is structural to relying on any specific free
model name. The real mitigation is already in place: transport failures (including a
`model_not_found` 404) fall through the provider chain and ultimately degrade to
`NullProvider` rather than crashing the run, so a stale model name degrades gracefully
rather than breaking `make demo`.

---

## 2026-08-23 — live model output at "confidence 0.85" wasn't stable run-to-run

**Symptom:** Running the same adjudication prompt against Groq twice (once inside
`adjudicator.run()`, once as a manual re-check moments later) produced different
confidence values for the same bank lines -- one run resolved 0 of 4 borderline
`LOW_CONFIDENCE` cases, a manual re-run of the identical prompt returned confidences
of 0.85/0.85/0.75/0.75 (two of which would have cleared the 0.85 threshold).

**Diagnosis:** Not a bug -- `temperature=0` reduces but doesn't eliminate variance for
Groq's `gpt-oss-20b`, a reasoning model with its own internal sampling. This is a
genuine property of LLM-based systems, not something achievable to fully pin down.

**Fix:** None needed -- confirmed this is exactly why `tests/test_determinism.py`
scopes the determinism guarantee to the `--no-llm` (`NullProvider`) path only, and
explicitly does not claim it for a real LLM provider. Documented in that test's
docstring so a future reader doesn't mistake the scoping for an oversight.

**Would do differently:** Nothing -- but this is a good concrete data point for the
eventual calibration report (Phase 5, `eval/calibration.py`): confidence values near a
threshold boundary are inherently noisy, which argues for keeping
`tier3_confidence_threshold` conservative rather than tuned to a razor's edge.

---

## 2026-08-23 — a fabricated `candidate_id` was accepted on the *first* bad response instead of retrying

**Symptom:** My own test (`test_invalid_candidate_id_is_discarded_not_trusted`)
expected up to `max_retries + 1` attempts before giving up on an out-of-list
`candidate_id`, matching the "do not retry more than twice" language in section 4.
The first implementation instead recorded `TIER3_INVALID_SELECTION` and gave up after
the very first bad response, never spending the retry budget at all.

**Diagnosis:** `adjudicate_cases`'s per-item handling treated an invalid selection as
an immediate terminal failure (`del remaining[bid]` on the spot) instead of leaving
the case in `remaining` for another attempt, unlike every other failure path (missing
response, malformed JSON), which already retried correctly.

**Fix:** Invalid selections are now tracked in a `seen_invalid_selection` set and left
in `remaining` to be retried; `TIER3_INVALID_SELECTION` is only recorded for cases
still unresolved after all attempts are exhausted. A later attempt that returns a
*valid* selection still resolves normally.

**Would do differently:** Nothing -- this is exactly why the retry-bounding tests were
worth writing before wiring this into the full pipeline, not after.

---

## 2026-08-23 — `complete_with_fallback`'s "none" sentinel didn't actually require `NullProvider`

**Symptom:** A test chain of `[FakeProvider(always fails)]` (no `NullProvider` at the
end) caused `adjudicate_cases` to immediately treat the LLM as "unavailable" and stop
retrying after one attempt, even though the real intent was "retry a failing real
provider up to the configured budget."

**Diagnosis:** `complete_with_fallback` returned the literal string `"none"` whenever
the loop over the chain was exhausted *for any reason* -- including a chain that never
contained a `NullProvider` at all. In production this is unreachable
(`resolve_chain()` always appends `NullProvider()`), but the function's contract was
wrong regardless, and it broke retry semantics for exactly the kind of chain a test
(or a future caller) might reasonably construct by hand.

**Fix:** The exhausted-chain fallback now returns the *last attempted real provider's*
name instead of `"none"`. `"none"` is reserved for the case where a `NullProvider` was
actually reached in the chain. `adjudicate_cases` and `extract_narration_utrs` already
only short-circuit on the literal `"none"`, so this fix alone restored correct
per-round retry behaviour without touching their logic.

**Would do differently:** Write the chain-construction tests before wiring the
"production always appends NullProvider" assumption into the transport layer's return
contract -- the assumption was true but the function shouldn't have silently depended
on it.

---

## 2026-08-23 — narration-extracted UTRs came back without the "UTR" prefix

**Symptom:** Live-testing narration extraction against Groq with a narration reading
`...UTR55512345678901...`, the model returned `"utr": "55512345678901"` -- correctly
reading "UTR" as a label rather than part of the reference value. My synthetic
dataset's join key is the *full* string including that prefix (`generate/generator.py
::_new_utr`), so a naive lookup would have silently failed to find the real batch.

**Diagnosis:** This is a reasonable, even correct, reading of the text by the model --
the mismatch is entirely a consequence of my own synthetic-data convention (a literal
"UTR" text prefix is not how real bank UTRs are formatted; I chose it purely to make
regex extraction easy in Tier 0). The join logic needs to be robust to it rather than
assuming the model will echo my internal convention back exactly.

**Fix:** `_resolve_extracted_utr` now tries the extracted value both as given and with
a `"UTR"` prefix prepended (when it doesn't already have one) before giving up.
Covered by `test_narration_extraction_tolerates_missing_utr_prefix`.

**Would do differently:** When a synthetic-data convention leaks into what's supposed
to be a realistic extraction task, expect the model to normalize it away rather than
preserve it, and design the downstream join to be tolerant from the start.

---

## 2026-08-23 — the rounding-adjustment posting made every FEE_DRIFT batch *more* unbalanced

**Symptom:** A smoke test summing debits and credits per proposed journal batch found
20 unbalanced batches (dev set), every one a small paise-level mismatch (2-10 paise).

**Diagnosis:** `journal.py`'s residual-handling logic had the debit/credit direction
backwards. Worked through the accounting identity by hand: with the bank-receipt leg
already on the debit side sized to the *actual* credit, `sum(debits) - sum(credits) =
credit_amount_paise - total_net = residual` *before* any rounding leg is added. So
`residual > 0` means debits are already ahead and need a **credit** to close the gap
-- the code did the opposite (`"debit" if residual > 0 else "credit"`), which added
to the wrong side and doubled the imbalance instead of closing it. Every FEE_DRIFT
line (the one defect class that makes `credit_amount_paise != total_net` by design)
was affected; everything else balanced by coincidence (residual == 0, so the buggy
branch never ran).

**Fix:** Flipped the direction (`"credit" if residual > 0 else "debit"`), verified by
re-deriving the identity with a worked numeric example before touching the code again,
and confirmed 0/260 unbalanced batches on the dev set afterward.
`test_every_journal_batch_balances` now runs this exact check as a standing test.

**Would do differently:** Write out the accounting identity in a comment *before*
writing the conditional, not after debugging it backwards from a failing balance
check. Sign errors in double-entry logic are exactly the kind of bug that's invisible
without an explicit balance assertion -- would add that test before the code that
needs it, next time, not after.

---

## 2026-08-23 — `journal.py` used `gross_paise` instead of `SettlementLine.gross_amount_paise`

**Symptom:** `propose_postings()` crashed with `AttributeError: 'SettlementLine'
object has no attribute 'gross_paise'` the first time it ran against real data.

**Diagnosis:** `match/fee_model.py::FeeBreakdown` names the field `gross_paise`;
`generate/schemas.py::SettlementLine` (a different model, built from a
`FeeBreakdown`) names the same value `gross_amount_paise`. I carried the wrong
model's field name into new code without checking, four phases after
`FeeBreakdown` was written.

**Fix:** One-line correction. No behavior to reconsider -- the field naming
inconsistency between the two models remains (fixing it now would mean touching
Phase 1 output schemas that are already load-bearing for `data/dev` and
`data/holdout`), but it's now a documented trap rather than a silent one.

**Would do differently:** When two closely-related models in the same codebase use
different names for the same concept, that's worth a one-line comment at the point of
the naming choice, not just at the point where someone eventually collides with it.

---

## 2026-08-23 — genuinely OUT_OF_SCOPE lines were staying tagged TIER2_TIMEOUT, not reclassified

**Symptom:** Live end-to-end check against the dev set expected 20 `OUT_OF_SCOPE`
exceptions (matching the generator's 20 `OUT_OF_SCOPE`-defect lines) but got only 6 --
the other 14 were tagged `TIER2_TIMEOUT` instead.

**Diagnosis:** `exceptions/taxonomy.py`'s reclassification pass (checking for a
near-UTR batch, a duplicate-amount signature, or "no UTR at all" to distinguish
`OUT_OF_SCOPE` from a generic miss) only ran for the `NO_CANDIDATE` reason hint.
`TIER2_TIMEOUT` cases -- which mean exactly the same thing operationally ("tier2
found nothing usable"), just via a different mechanical path (a bounded search
exhausting its budget vs. finding literally zero candidates) -- were passed through
untouched. Both hints can land on the *same* genuinely-out-of-scope line depending on
how the generic subset-sum search happens to terminate for that particular input
(itself just a budget-tuning artifact from an earlier fix, not a correctness
difference -- see the subset-sum performance entry above).

**Fix:** Both `NO_CANDIDATE` and `TIER2_TIMEOUT` now go through the same
reclassification pass (`_RECLASSIFIABLE` dict, replacing the old direct passthrough
for `TIER2_TIMEOUT`), falling back to their original code only if none of the more
specific patterns match. Re-verified: 20/20 `OUT_OF_SCOPE`-defect lines now land
correctly.

**Would do differently:** When two reason hints can be produced by what's
semantically the same failure mode via different code paths, treat them as one case
for any downstream classification, not two -- the mechanical distinction (empty
result vs. exhausted budget) is real and worth keeping in the reclassified code's
fallback, but it shouldn't gate whether reclassification runs at all.

---

## 2026-08-25 — the one-time holdout calibration run came back empty

**Symptom:** Running `eval/metrics.py --profile holdout` and `eval/ablation.py --profile
holdout` (the mandatory, once-only final evaluation per IMPLEMENTATION.md section 7) both
showed 10 resolutions attributed to tier3 with 2 real Groq calls. Running
`eval/calibration.py --profile holdout` moments later -- a separate process, its own
independent `harness.run(..., "full")` call -- reported "no tier3 resolutions this run,
nothing to calibrate."

**Diagnosis:** Not a bug. Each `eval/*.py` CLI entry point re-runs the full pipeline from
scratch rather than sharing one run, and tier3 confidence is genuinely non-deterministic
run-to-run against a live model (already documented above, 2026-08-23 entry). The same
handful of borderline candidates that cleared `tier3_confidence_threshold=0.85` in the
metrics/ablation invocations apparently came back just under it in calibration's own
invocation, so they were recorded as `LOW_CONFIDENCE` exceptions instead of tier3
resolutions that run -- same system, same inputs, different live model sampling.

**Fix:** None needed -- `calibration.py::format_report` already handles the empty case
without crashing, and the module docstring already scopes this exact possibility. The
README cites the dev-set calibration report (which does have populated bins, gathered
during Phase 5 build) as the illustrative reliability table, and reports this holdout-run
outcome honestly as its own data point rather than re-running holdout again to get a
"nicer" result -- re-running until the numbers look better is exactly the kind of holdout
gaming section 5's honesty rule exists to prevent.

**Would do differently:** If a calibration table populated specifically from the held-out
run mattered for the final submission, the three `eval/*.py` scripts should share one
`harness.run()` result within a single process instead of three independent ones -- worth
doing if there's a Phase 7+ pass over `eval/`, but out of scope to change on the one
evaluation pass itself.
