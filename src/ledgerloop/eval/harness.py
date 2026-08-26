"""Shared harness for eval/*.py: runs the deterministic+LLM pipeline against a data
profile up to a chosen tier ceiling, and loads that profile's ground truth.

The answer key is read ONLY from load_answer_key() below -- nowhere in the matching
tiers, reconcile.py, or the generator's own runtime does anything look at it. That
keeps the score honest: the pipeline genuinely cannot see its own answer sheet.

See IMPLEMENTATION.md section 4 (Phase 5) and AnswerKeyEntry's own docstring.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import LLMProvider, resolve_chain
from ledgerloop.config import load_config
from ledgerloop.exceptions import taxonomy
from ledgerloop.generate.schemas import AnswerKeyEntry
from ledgerloop.ingest.normalise import NormalisedDataset, load_and_normalise
from ledgerloop.match import tier1_exact, tier2_algorithmic
from ledgerloop.schemas import Exception_, Resolution, UnresolvedCase

TierCeiling = Literal["tier1", "tier1+2", "full"]


@dataclass
class HarnessRun:
    profile: str
    tier_ceiling: TierCeiling
    resolutions: list[Resolution]
    exceptions: list[Exception_]
    total_records: int
    llm_calls_made: int
    providers_used: list[str]
    wall_seconds: float
    config: dict
    # Integer paise per bank line, so metrics can weight by value as well as by count.
    # A pipeline that resolves 92% of lines but only 40% of the money is a materially
    # different system from one that does both, and a count-only report hides that.
    credit_paise_by_bank_line: dict[str, int]


def load_answer_key(data_root: Path, profile: str) -> dict[str, AnswerKeyEntry]:
    raw = json.loads((data_root / profile / "answer_key.json").read_text(encoding="utf-8"))
    entries = [AnswerKeyEntry.model_validate(e) for e in raw]
    return {e.bank_line_id: e for e in entries}


def is_correct_resolution(resolution: Resolution, answer: AnswerKeyEntry) -> bool:
    """A resolution is only "correct" if it names exactly the ground-truth
    transaction set -- a resolution that matches the right batch but drags in an
    extra decoy transaction (or misses one) is a false match, not a partial credit."""
    return set(resolution.matched_txn_ids) == set(answer.matched_txn_ids)


def run_pipeline(
    normalised: NormalisedDataset,
    config: dict,
    tier_ceiling: TierCeiling,
    *,
    chain: list[LLMProvider] | None = None,
) -> tuple[list[Resolution], list[Exception_], int, list[str]]:
    """Runs tiers up to and including tier_ceiling and classifies whatever's left via
    the same exception taxonomy reconcile.py uses, so ablation rows are directly
    comparable to a real run rather than an approximation of one."""
    batches = tier1_exact.build_batches(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)
    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    providers_used: list[str] = []

    if tier_ceiling == "tier1":
        resolutions, unresolved_ids = tier1_exact.resolve(
            normalised.bank_lines, batches, max_lag_days=config["tier1"]["exact_match_max_lag_days"]
        )
        unresolved = [
            UnresolvedCase(bank_line_id=bid, reason_hint="NO_CANDIDATE", candidates=[], evidence={})
            for bid in unresolved_ids
        ]
        llm_calls = 0
    else:
        tier2_result = tier2_algorithmic.run(normalised, config)
        if tier_ceiling == "tier1+2":
            resolutions = tier2_result.resolutions
            unresolved = tier2_result.unresolved
            llm_calls = 0
        else:  # full
            active_chain = chain if chain is not None else resolve_chain()
            tier3_result = adjudicator.run(normalised, tier2_result, config, active_chain)
            resolutions = tier2_result.resolutions + tier3_result.resolutions
            unresolved = tier3_result.unresolved
            llm_calls = tier3_result.llm_calls_made
            providers_used = tier3_result.providers_used

    claimed_batch_ids = {r.evidence["settlement_batch_id"] for r in resolutions if "settlement_batch_id" in r.evidence}
    claimed_txn_ids = {t for r in resolutions for t in r.matched_txn_ids}
    unclaimed_gross = [
        t.gross_amount_paise for t in normalised.gateway_transactions if t.txn_id not in claimed_txn_ids
    ]

    exceptions = taxonomy.classify_all(
        bank_line_by_id,
        unresolved,
        by_utr=by_utr,
        claimed_batch_ids=claimed_batch_ids,
        unclaimed_gross_amounts_paise=unclaimed_gross,
        tier2_cfg=config["tier2"],
    )
    return resolutions, exceptions, llm_calls, providers_used


def run(
    data_root: Path,
    profile: str,
    tier_ceiling: TierCeiling,
    *,
    no_llm: bool = False,
    config_path: Path | None = None,
    chain: list[LLMProvider] | None = None,
) -> HarnessRun:
    config = load_config(config_path) if config_path else load_config()
    normalised = load_and_normalise(data_root / profile)
    resolved_chain = chain if chain is not None else (resolve_chain(no_llm=no_llm) if tier_ceiling == "full" else None)

    start = time.perf_counter()
    resolutions, exceptions, llm_calls, providers_used = run_pipeline(
        normalised, config, tier_ceiling, chain=resolved_chain
    )
    wall_seconds = time.perf_counter() - start

    return HarnessRun(
        profile=profile,
        tier_ceiling=tier_ceiling,
        resolutions=resolutions,
        exceptions=exceptions,
        total_records=len(normalised.bank_lines),
        llm_calls_made=llm_calls,
        providers_used=providers_used,
        wall_seconds=wall_seconds,
        config=config,
        credit_paise_by_bank_line={b.bank_line_id: b.credit_amount_paise for b in normalised.bank_lines},
    )
