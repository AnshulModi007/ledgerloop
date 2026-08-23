"""Tier 2: algorithmic candidate generation. Where tier1's exact join fails, this
tries -- in order of how strong the evidence is -- a wider-tolerance UTR match, a
bounded single-digit-transposition-tolerant UTR match, a partition search for
one-batch-to-two-credits splits, an amount+date fallback with no UTR at all, and
finally a bounded cross-batch subset-sum. See IMPLEMENTATION.md section 4.

This module's job is candidates, not verdicts: where exactly one candidate clears
`tier2.min_resolve_score` with no other candidate within `tier2.ambiguity_margin` of
it, it resolves. Everything else -- ties, weak scores, timeouts, nothing found at
all -- becomes an UnresolvedCase carrying whatever candidates were found, headed to
tier3 (Phase 3) or the exception queue (Phase 4).
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass

from ledgerloop.confidence import (
    BASE_AMOUNT_DATE_FALLBACK,
    BASE_EXACT_UTR,
    BASE_GENERIC_SUBSET_SUM,
    BASE_PARTITION_SEARCH,
    BASE_TRANSPOSED_UTR,
)
from ledgerloop.confidence import score as _score
from ledgerloop.generate.schemas import SettlementLine
from ledgerloop.ingest.normalise import NormalisedBankLine, NormalisedDataset
from ledgerloop.match import subset_sum, tier1_exact
from ledgerloop.match.subset_sum import Item
from ledgerloop.match.tier1_exact import SettlementBatch
from ledgerloop.schemas import Candidate, Resolution, UnresolvedCase


@dataclass
class _ScoredCandidate:
    matched_txn_ids: list[str]
    score: float
    evidence: dict


@dataclass
class PipelineResult:
    resolutions: list[Resolution]
    unresolved: list[UnresolvedCase]
    tier2_timeouts: int




def _strategy_exact_utr(
    bank_line: NormalisedBankLine,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
    cfg: dict,
) -> list[_ScoredCandidate]:
    if not bank_line.extracted_utr:
        return []
    out = []
    for batch in by_utr.get(bank_line.extracted_utr, []):
        if batch.settlement_batch_id in claimed_batch_ids:
            continue
        diff = abs(bank_line.credit_amount_paise - batch.total_net_paise)
        lag = (bank_line.value_date - batch.settlement_date).days
        if diff > cfg["amount_tolerance_paise"] or not (0 <= lag <= cfg["date_window_days"]):
            continue
        score = _score(base=BASE_EXACT_UTR, diff=diff, tolerance=cfg["amount_tolerance_paise"], lag=lag, window=cfg["date_window_days"])
        out.append(
            _ScoredCandidate(
                matched_txn_ids=list(batch.txn_ids),
                score=score,
                evidence={
                    "rule": "utr_amount_tolerance",
                    "payout_utr": batch.payout_utr,
                    "settlement_batch_id": batch.settlement_batch_id,
                    "amount_diff_paise": diff,
                    "lag_days": lag,
                },
            )
        )
    return out


def _strategy_transposed_utr(
    bank_line: NormalisedBankLine,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
    cfg: dict,
) -> list[_ScoredCandidate]:
    if not bank_line.extracted_utr:
        return []
    out = []
    seen_batches: set[str] = set()
    for variant in tier1_exact.transposed_utr_variants(bank_line.extracted_utr):
        for batch in by_utr.get(variant, []):
            if batch.settlement_batch_id in claimed_batch_ids or batch.settlement_batch_id in seen_batches:
                continue
            diff = abs(bank_line.credit_amount_paise - batch.total_net_paise)
            lag = (bank_line.value_date - batch.settlement_date).days
            if diff > cfg["amount_tolerance_paise"] or not (0 <= lag <= cfg["date_window_days"]):
                continue
            seen_batches.add(batch.settlement_batch_id)
            score = _score(base=BASE_TRANSPOSED_UTR, diff=diff, tolerance=cfg["amount_tolerance_paise"], lag=lag, window=cfg["date_window_days"])
            out.append(
                _ScoredCandidate(
                    matched_txn_ids=list(batch.txn_ids),
                    score=score,
                    evidence={
                        "rule": "utr_single_transposition_tolerant",
                        "payout_utr_extracted": bank_line.extracted_utr,
                        "payout_utr_matched": variant,
                        "settlement_batch_id": batch.settlement_batch_id,
                        "amount_diff_paise": diff,
                        "lag_days": lag,
                    },
                )
            )
    return out


def _strategy_amount_date_fallback(
    bank_line: NormalisedBankLine,
    batches: dict[str, SettlementBatch],
    claimed_batch_ids: set[str],
    cfg: dict,
) -> list[_ScoredCandidate]:
    out = []
    for batch in batches.values():
        if batch.settlement_batch_id in claimed_batch_ids:
            continue
        diff = abs(bank_line.credit_amount_paise - batch.total_net_paise)
        lag = (bank_line.value_date - batch.settlement_date).days
        if diff > cfg["amount_tolerance_paise"] or not (0 <= lag <= cfg["date_window_days"]):
            continue
        score = _score(base=BASE_AMOUNT_DATE_FALLBACK, diff=diff, tolerance=cfg["amount_tolerance_paise"], lag=lag, window=cfg["date_window_days"])
        out.append(
            _ScoredCandidate(
                matched_txn_ids=list(batch.txn_ids),
                score=score,
                evidence={
                    "rule": "amount_date_fallback_no_utr",
                    "settlement_batch_id": batch.settlement_batch_id,
                    "amount_diff_paise": diff,
                    "lag_days": lag,
                },
            )
        )
    return out


def _strategy_generic_subset_sum(
    bank_line: NormalisedBankLine,
    unclaimed_lines: list[SettlementLine],
    cfg: dict,
    rng: random.Random,
) -> tuple[list[_ScoredCandidate], bool]:
    window_lines = [
        line
        for line in unclaimed_lines
        if 0 <= (bank_line.value_date - line.settlement_date).days <= cfg["date_window_days"]
    ]
    items = [Item(line.txn_id, line.net_paise) for line in window_lines]
    value_by_id = {item.item_id: item.value_paise for item in items}

    result = subset_sum.find_subset(
        items,
        bank_line.credit_amount_paise,
        tolerance=cfg["amount_tolerance_paise"],
        node_budget=cfg["subset_sum_node_budget"],
        meet_in_middle_max_items=cfg["meet_in_middle_max_items"],
        rng=rng,
    )
    if result.status == "timeout":
        return [], True

    out = []
    for subset in result.subsets:
        total = sum(value_by_id[t] for t in subset)
        diff = abs(total - bank_line.credit_amount_paise)
        score = _score(base=BASE_GENERIC_SUBSET_SUM, diff=diff, tolerance=cfg["amount_tolerance_paise"], lag=0, window=cfg["date_window_days"])
        out.append(
            _ScoredCandidate(
                matched_txn_ids=subset,
                score=score,
                evidence={"rule": "generic_cross_batch_subset_sum", "amount_diff_paise": diff},
            )
        )
    return out, False


def _structural_candidate_batch_ids(
    bank_line: NormalisedBankLine, by_utr: dict[str, list[SettlementBatch]]
) -> set[str]:
    """Batches this line's narration UTR points to, either exactly or via a single
    adjacent-digit transposition. Used to group split-payout lines by the batch they
    actually belong to rather than by literal narration text, which a TRANSPOSE
    defect landing on only one of the two lines would otherwise break.
    """
    if not bank_line.extracted_utr:
        return set()
    ids = {b.settlement_batch_id for b in by_utr.get(bank_line.extracted_utr, [])}
    for variant in tier1_exact.transposed_utr_variants(bank_line.extracted_utr):
        ids.update(b.settlement_batch_id for b in by_utr.get(variant, []))
    return ids


def _resolve_partition_groups(
    unresolved_lines: list[NormalisedBankLine],
    batches: dict[str, SettlementBatch],
    by_utr: dict[str, list[SettlementBatch]],
    lines_by_batch: dict[str, list[SettlementLine]],
    cfg: dict,
    rng: random.Random,
    claimed_batch_ids: set[str],
) -> tuple[list[Resolution], set[str]]:
    """1 settlement batch paid out across 2 bank credits (SPLIT_1N): find the batches
    with >=2 structurally-linked unresolved lines and try to partition the batch's
    transactions between them.
    """
    resolutions: list[Resolution] = []
    handled_bank_line_ids: set[str] = set()

    groups: dict[str, list[NormalisedBankLine]] = defaultdict(list)
    for line in unresolved_lines:
        for batch_id in _structural_candidate_batch_ids(line, by_utr):
            groups[batch_id].append(line)

    for batch_id in sorted(groups):
        lines = groups[batch_id]
        if len(lines) != 2:
            continue  # the generator only ever splits a batch across 2 credits
        batch = batches[batch_id]
        if batch.settlement_batch_id in claimed_batch_ids:
            continue

        total_credit = sum(line.credit_amount_paise for line in lines)
        if abs(total_credit - batch.total_net_paise) > cfg["amount_tolerance_paise"]:
            continue

        target_line, other_line = sorted(lines, key=lambda line: line.bank_line_id)
        batch_lines = lines_by_batch[batch.settlement_batch_id]
        items = [Item(line.txn_id, line.net_paise) for line in batch_lines]

        result = subset_sum.find_subset(
            items,
            target_line.credit_amount_paise,
            tolerance=cfg["amount_tolerance_paise"],
            node_budget=cfg["subset_sum_node_budget"],
            meet_in_middle_max_items=cfg["meet_in_middle_max_items"],
            rng=rng,
        )
        if result.status != "ok":
            continue

        subset = result.subsets[0]
        value_by_id = {line.txn_id: line.net_paise for line in batch_lines}
        subset_total = sum(value_by_id[t] for t in subset)
        remainder_ids = sorted(set(batch.txn_ids) - set(subset))
        remainder_total = batch.total_net_paise - subset_total

        diff_target = abs(subset_total - target_line.credit_amount_paise)
        diff_other = abs(remainder_total - other_line.credit_amount_paise)
        if diff_other > cfg["amount_tolerance_paise"]:
            continue

        for line, txn_ids, diff in (
            (target_line, subset, diff_target),
            (other_line, remainder_ids, diff_other),
        ):
            score = _score(base=BASE_PARTITION_SEARCH, diff=diff, tolerance=cfg["amount_tolerance_paise"], lag=0, window=cfg["date_window_days"])
            resolutions.append(
                Resolution(
                    bank_line_id=line.bank_line_id,
                    matched_txn_ids=sorted(txn_ids),
                    resolved_by="tier2",
                    confidence=score,
                    evidence={
                        "rule": "utr_partition_search",
                        "payout_utr": batch.payout_utr,
                        "settlement_batch_id": batch.settlement_batch_id,
                        "amount_diff_paise": diff,
                    },
                    audit_id=tier1_exact.audit_id("tier2", line.bank_line_id, list(txn_ids)),
                )
            )
            handled_bank_line_ids.add(line.bank_line_id)

        claimed_batch_ids.add(batch.settlement_batch_id)

    return resolutions, handled_bank_line_ids


def _decide(bank_line_id: str, scored: list[_ScoredCandidate], cfg: dict) -> Resolution | UnresolvedCase:
    best = max(scored, key=lambda c: c.score)
    tied = [c for c in scored if best.score - c.score <= cfg["ambiguity_margin"]]

    if best.score >= cfg["min_resolve_score"] and len(tied) == 1:
        return Resolution(
            bank_line_id=bank_line_id,
            matched_txn_ids=best.matched_txn_ids,
            resolved_by="tier2",
            confidence=best.score,
            evidence=best.evidence,
            audit_id=tier1_exact.audit_id("tier2", bank_line_id, best.matched_txn_ids),
        )

    reason_hint = "AMBIGUOUS_CANDIDATES" if len(tied) > 1 else "LOW_CONFIDENCE"
    ranked = sorted(scored, key=lambda c: -c.score)[: subset_sum.MAX_SUBSETS_REPORTED]
    candidates = [
        Candidate(
            candidate_id=f"{bank_line_id}-C{i}",
            matched_txn_ids=c.matched_txn_ids,
            score=c.score,
            evidence=c.evidence,
        )
        for i, c in enumerate(ranked)
    ]
    return UnresolvedCase(bank_line_id=bank_line_id, reason_hint=reason_hint, candidates=candidates, evidence={})


def run(normalised: NormalisedDataset, config: dict) -> PipelineResult:
    tier1_cfg = config["tier1"]
    tier2_cfg = config["tier2"]

    batches = tier1_exact.build_batches(normalised.settlement_lines)
    lines_by_batch = tier1_exact.group_lines_by_batch(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)

    tier1_resolutions, unresolved_ids = tier1_exact.resolve(
        normalised.bank_lines, batches, max_lag_days=tier1_cfg["exact_match_max_lag_days"]
    )
    claimed_batch_ids = {r.evidence["settlement_batch_id"] for r in tier1_resolutions}
    claimed_txn_ids = {t for r in tier1_resolutions for t in r.matched_txn_ids}

    bank_line_by_id = {line.bank_line_id: line for line in normalised.bank_lines}
    unresolved_lines = [bank_line_by_id[i] for i in unresolved_ids]

    rng = random.Random(tier2_cfg["matcher_rng_seed"])

    partition_resolutions, handled_ids = _resolve_partition_groups(
        unresolved_lines, batches, by_utr, lines_by_batch, tier2_cfg, rng, claimed_batch_ids
    )
    for r in partition_resolutions:
        claimed_txn_ids.update(r.matched_txn_ids)

    resolutions = list(tier1_resolutions) + partition_resolutions
    unresolved_cases: list[UnresolvedCase] = []
    tier2_timeouts = 0

    remaining_lines = sorted(
        (line for line in unresolved_lines if line.bank_line_id not in handled_ids),
        key=lambda line: line.bank_line_id,
    )

    for bank_line in remaining_lines:
        scored = _strategy_exact_utr(bank_line, by_utr, claimed_batch_ids, tier2_cfg)
        if not scored:
            scored = _strategy_transposed_utr(bank_line, by_utr, claimed_batch_ids, tier2_cfg)
        if not scored:
            scored = _strategy_amount_date_fallback(bank_line, batches, claimed_batch_ids, tier2_cfg)

        timed_out = False
        if not scored:
            unclaimed_lines = [
                line for line in normalised.settlement_lines if line.txn_id not in claimed_txn_ids
            ]
            scored, timed_out = _strategy_generic_subset_sum(bank_line, unclaimed_lines, tier2_cfg, rng)

        if timed_out:
            tier2_timeouts += 1
            unresolved_cases.append(
                UnresolvedCase(bank_line_id=bank_line.bank_line_id, reason_hint="TIER2_TIMEOUT", candidates=[], evidence={})
            )
            continue

        scored = [c for c in scored if not (set(c.matched_txn_ids) & claimed_txn_ids)]
        if not scored:
            unresolved_cases.append(
                UnresolvedCase(bank_line_id=bank_line.bank_line_id, reason_hint="NO_CANDIDATE", candidates=[], evidence={})
            )
            continue

        decision = _decide(bank_line.bank_line_id, scored, tier2_cfg)
        if isinstance(decision, Resolution):
            resolutions.append(decision)
            claimed_txn_ids.update(decision.matched_txn_ids)
            batch_id = decision.evidence.get("settlement_batch_id")
            if batch_id:
                claimed_batch_ids.add(batch_id)
        else:
            unresolved_cases.append(decision)

    return PipelineResult(resolutions=resolutions, unresolved=unresolved_cases, tier2_timeouts=tier2_timeouts)
