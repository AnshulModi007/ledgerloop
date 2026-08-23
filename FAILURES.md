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
