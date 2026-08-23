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
