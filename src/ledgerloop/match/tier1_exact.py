"""Tier 1: exact join on (payout UTR, amount), bounded by a narrow structural
settlement-to-posting lag. Deterministic, no fuzzy tolerance -- see
IMPLEMENTATION.md section 4. Expect roughly 55-65% resolution.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from ledgerloop.confidence import BASE_EXACT_UTR
from ledgerloop.generate.schemas import SettlementLine
from ledgerloop.ingest.normalise import NormalisedBankLine
from ledgerloop.schemas import Resolution


@dataclass(frozen=True)
class SettlementBatch:
    settlement_batch_id: str
    payout_utr: str
    settlement_date: date
    txn_ids: tuple[str, ...]
    total_net_paise: int


def group_lines_by_batch(settlement_lines: list[SettlementLine]) -> dict[str, list[SettlementLine]]:
    grouped: dict[str, list[SettlementLine]] = defaultdict(list)
    for line in settlement_lines:
        grouped[line.settlement_batch_id].append(line)
    return grouped


def build_batches(settlement_lines: list[SettlementLine]) -> dict[str, SettlementBatch]:
    """Group per-transaction settlement lines into their batches. One batch = what the
    gateway paid out as a single NEFT/RTGS/IMPS transfer, sharing one payout UTR."""
    grouped = group_lines_by_batch(settlement_lines)

    batches: dict[str, SettlementBatch] = {}
    for batch_id, lines in grouped.items():
        batches[batch_id] = SettlementBatch(
            settlement_batch_id=batch_id,
            payout_utr=lines[0].payout_utr,
            settlement_date=max(line.settlement_date for line in lines),
            txn_ids=tuple(sorted(line.txn_id for line in lines)),
            total_net_paise=sum(line.net_paise for line in lines),
        )
    return batches


def batches_by_utr(batches: dict[str, SettlementBatch]) -> dict[str, list[SettlementBatch]]:
    index: dict[str, list[SettlementBatch]] = defaultdict(list)
    for batch in batches.values():
        index[batch.payout_utr].append(batch)
    return index


def audit_id(tier: str, bank_line_id: str, matched_txn_ids: list[str]) -> str:
    """Deterministic, reproducible placeholder id. Phase 4's audit/log.py owns the real trail."""
    digest = hashlib.sha1(
        f"{tier}:{bank_line_id}:{','.join(sorted(matched_txn_ids))}".encode()
    ).hexdigest()
    return f"AUD-{digest[:12]}"


def resolve(
    bank_lines: list[NormalisedBankLine],
    batches: dict[str, SettlementBatch],
    *,
    max_lag_days: int,
) -> tuple[list[Resolution], list[str]]:
    """Returns (resolutions, unresolved_bank_line_ids). A bank line resolves at tier1
    only when its narration UTR maps to exactly one batch, the amount matches exactly,
    and the settlement-to-posting lag is within max_lag_days. Anything else -- missing
    UTR, no matching batch, amount mismatch, or an unusually long lag -- falls through
    to tier2 untouched.
    """
    by_utr = batches_by_utr(batches)

    resolutions: list[Resolution] = []
    unresolved: list[str] = []

    for bank_line in bank_lines:
        candidates = by_utr.get(bank_line.extracted_utr, []) if bank_line.extracted_utr else []
        match = None
        if len(candidates) == 1:
            batch = candidates[0]
            lag = (bank_line.value_date - batch.settlement_date).days
            if bank_line.credit_amount_paise == batch.total_net_paise and 0 <= lag <= max_lag_days:
                match = batch

        if match is None:
            unresolved.append(bank_line.bank_line_id)
            continue

        resolutions.append(
            Resolution(
                bank_line_id=bank_line.bank_line_id,
                matched_txn_ids=list(match.txn_ids),
                resolved_by="tier1",
                confidence=BASE_EXACT_UTR,
                evidence={
                    "rule": "exact_utr_amount_join",
                    "payout_utr": match.payout_utr,
                    "settlement_batch_id": match.settlement_batch_id,
                    "lag_days": (bank_line.value_date - match.settlement_date).days,
                },
                audit_id=audit_id("tier1", bank_line.bank_line_id, list(match.txn_ids)),
            )
        )

    return resolutions, unresolved
