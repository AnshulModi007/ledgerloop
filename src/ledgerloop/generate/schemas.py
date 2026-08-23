"""Row-level schemas for the four synthetic sources and the answer key.

These are generation-time output schemas (what lands in the CSVs/JSON under
data/dev and data/holdout), not the pipeline's internal matching schemas —
those live in ledgerloop.match and ledgerloop.ledger once Phase 2 exists.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class GatewayTransaction(BaseModel):
    """One row of gateway/transactions.csv — a captured payment."""

    txn_id: str
    order_id: str
    rrn: str
    gross_amount_paise: int
    captured_at: datetime
    payment_method: str
    status: str  # captured | refunded | charged_back
    merchant_ref: str


class SettlementLine(BaseModel):
    """One row of settlement/settlement_report.csv — a txn's line inside a settlement batch."""

    settlement_batch_id: str
    txn_id: str
    gross_amount_paise: int
    fee_paise: int
    gst_on_fee_paise: int
    tds_paise: int
    refund_paise: int
    chargeback_paise: int
    net_paise: int
    settlement_date: date


class BankStatementLine(BaseModel):
    """One row of bank/bank_statement.csv — a lump-sum credit with free-text narration."""

    bank_line_id: str
    value_date: date
    credit_amount_paise: int
    narration: str


class ErpInvoice(BaseModel):
    """One row of erp/erp_ledger.csv — what the merchant's books believe is owed."""

    invoice_id: str
    order_id: str
    expected_amount_paise: int
    status: str  # open | settled


class AnswerKeyEntry(BaseModel):
    """Ground truth for one bank statement line. Never read outside eval/harness.py for holdout."""

    bank_line_id: str
    matched_txn_ids: list[str]
    settlement_batch_ids: list[str]
    defect_classes: list[str]
    notes: str = ""
