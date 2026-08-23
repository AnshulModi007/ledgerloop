"""Proposes double-entry postings for each resolved match. This is what closes the
loop -- see IMPLEMENTATION.md section 4. Postings are *proposed*, not applied, until
approved (the UI's job, Phase 6); this module only ever produces proposals.

Per matched transaction: gross = fee + GST-on-fee + TDS + refund + chargeback + net
holds exactly, by construction of match/fee_model.py::compute_net. That identity is
what the postings below encode:

    Dr fee expense               fee_paise
    Dr GST input credit          gst_on_fee_paise
    Dr TDS receivable            tds_paise
    Dr refund contra             refund_paise       (only if > 0)
    Dr chargeback contra         chargeback_paise   (only if > 0)
        Cr settlement receivable  gross_amount_paise

...clearing what was expected from the gateway for that transaction. One aggregate
leg per *bank line* (not per transaction) records the actual cash movement:

    Dr bank account               credit_amount_paise

If the observed credit doesn't exactly equal the sum of computed nets (FEE_DRIFT-style
paise-level drift; tolerated by tier2, not eliminated), the residual is posted
explicitly to a rounding-adjustment account rather than silently absorbed -- every
paise is accounted for somewhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ledgerloop.generate.schemas import SettlementLine
from ledgerloop.ingest.normalise import NormalisedBankLine
from ledgerloop.ledger.idempotency import posting_key
from ledgerloop.schemas import Resolution

Direction = Literal["debit", "credit"]


class Posting(BaseModel):
    bank_line_id: str
    posting_type: str
    account: str
    direction: Direction
    amount_paise: int
    txn_id: str | None  # None for bank-line-level aggregate legs
    idempotency_key: str


class JournalBatch(BaseModel):
    bank_line_id: str
    settlement_batch_ids: list[str]
    resolved_by: Literal["tier1", "tier2", "tier3"]
    postings: list[Posting]
    status: Literal["proposed", "approved"] = "proposed"


def _posting(
    bank_line_id: str, posting_type: str, account: str, direction: Direction, amount_paise: int, txn_id: str | None
) -> Posting:
    # source_ids is [txn_id] for a per-transaction leg, or [bank_line_id] for a
    # bank-line-level aggregate leg -- either way it's what makes the key unique
    # alongside posting_type, with no need to fold txn_id into posting_type too.
    source_ids = [txn_id] if txn_id is not None else [bank_line_id]
    return Posting(
        bank_line_id=bank_line_id,
        posting_type=posting_type,
        account=account,
        direction=direction,
        amount_paise=amount_paise,
        txn_id=txn_id,
        idempotency_key=posting_key(bank_line_id, source_ids, posting_type),
    )


def _postings_for_txn(bank_line_id: str, line: SettlementLine) -> list[Posting]:
    postings = []
    if line.fee_paise:
        postings.append(_posting(bank_line_id, "fee_expense", "platform_fee_expense", "debit", line.fee_paise, line.txn_id))
    if line.gst_on_fee_paise:
        postings.append(
            _posting(bank_line_id, "gst_input_credit", "gst_input_credit_receivable", "debit", line.gst_on_fee_paise, line.txn_id)
        )
    if line.tds_paise:
        postings.append(_posting(bank_line_id, "tds_receivable", "tds_receivable", "debit", line.tds_paise, line.txn_id))
    if line.refund_paise:
        postings.append(_posting(bank_line_id, "refund_contra", "refund_expense", "debit", line.refund_paise, line.txn_id))
    if line.chargeback_paise:
        postings.append(
            _posting(bank_line_id, "chargeback_contra", "chargeback_expense", "debit", line.chargeback_paise, line.txn_id)
        )
    postings.append(
        _posting(
            bank_line_id, "settlement_receivable_clear", "settlement_receivable", "credit", line.gross_amount_paise, line.txn_id
        )
    )
    return postings


def propose_postings(
    resolutions: list[Resolution],
    settlement_lines_by_txn: dict[str, SettlementLine],
    bank_lines_by_id: dict[str, NormalisedBankLine],
) -> list[JournalBatch]:
    batches: list[JournalBatch] = []

    for resolution in sorted(resolutions, key=lambda r: r.bank_line_id):
        bank_line_id = resolution.bank_line_id
        lines = [settlement_lines_by_txn[t] for t in resolution.matched_txn_ids if t in settlement_lines_by_txn]

        postings: list[Posting] = []
        for line in lines:
            postings.extend(_postings_for_txn(bank_line_id, line))

        bank_line = bank_lines_by_id[bank_line_id]
        postings.append(
            _posting(bank_line_id, "bank_receipt", "bank_account", "debit", bank_line.credit_amount_paise, None)
        )

        total_net = sum(line.net_paise for line in lines)
        residual = bank_line.credit_amount_paise - total_net
        if residual != 0:
            # residual > 0 means the actual credit exceeded computed net, so the
            # debit side (which already includes the bank_receipt leg above, sized to
            # the actual credit) is ahead of the credit side by `residual` -- balance
            # it by crediting the difference, not debiting it.
            direction: Direction = "credit" if residual > 0 else "debit"
            postings.append(
                _posting(bank_line_id, "rounding_adjustment", "rounding_adjustment", direction, abs(residual), None)
            )

        settlement_batch_ids = sorted({line.settlement_batch_id for line in lines})
        batches.append(
            JournalBatch(
                bank_line_id=bank_line_id,
                settlement_batch_ids=settlement_batch_ids,
                resolved_by=resolution.resolved_by,
                postings=postings,
            )
        )

    return batches
