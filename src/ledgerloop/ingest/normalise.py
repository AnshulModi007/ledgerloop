"""Tier 0: load the four raw CSV sources into typed, UTC-normalised in-memory records.

No matching happens here -- this only makes downstream tiers' lives easier: correct
types, UTC timestamps, canonical merchant references, and a best-effort UTR pulled
from bank narration via regex (ingest/narration.py). Where that regex fails, Tier 3
may attempt LLM extraction later -- this module never guesses.
"""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ledgerloop.generate.schemas import (
    BankStatementLine,
    ErpInvoice,
    GatewayTransaction,
    SettlementLine,
)
from ledgerloop.ingest.narration import extract_utr

# Source timestamps are naive local business time -- see generate/generator.py -- and
# an Indian merchant's business clock is IST. Attach that zone explicitly rather than
# assuming the host machine's local time, then convert to UTC for pipeline-wide use.
IST = ZoneInfo("Asia/Kolkata")


class NormalisedGatewayTransaction(BaseModel):
    txn_id: str
    order_id: str
    rrn: str
    gross_amount_paise: int
    captured_at_utc: datetime
    payment_method: str
    status: str
    merchant_ref: str


class NormalisedBankLine(BaseModel):
    bank_line_id: str
    value_date: date
    credit_amount_paise: int
    narration: str
    extracted_utr: str | None


class NormalisedDataset(BaseModel):
    gateway_transactions: list[NormalisedGatewayTransaction]
    settlement_lines: list[SettlementLine]
    bank_lines: list[NormalisedBankLine]
    erp_invoices: list[ErpInvoice]


def _canonical_merchant_ref(raw: str) -> str:
    return raw.strip().upper()


def _to_utc(naive_local: datetime) -> datetime:
    return naive_local.replace(tzinfo=IST).astimezone(UTC)


def normalise_gateway_transactions(
    rows: list[GatewayTransaction],
) -> list[NormalisedGatewayTransaction]:
    return [
        NormalisedGatewayTransaction(
            txn_id=r.txn_id,
            order_id=r.order_id,
            rrn=r.rrn,
            gross_amount_paise=r.gross_amount_paise,
            captured_at_utc=_to_utc(r.captured_at),
            payment_method=r.payment_method,
            status=r.status,
            merchant_ref=_canonical_merchant_ref(r.merchant_ref),
        )
        for r in rows
    ]


def normalise_bank_lines(rows: list[BankStatementLine]) -> list[NormalisedBankLine]:
    return [
        NormalisedBankLine(
            bank_line_id=r.bank_line_id,
            value_date=r.value_date,
            credit_amount_paise=r.credit_amount_paise,
            narration=r.narration,
            extracted_utr=extract_utr(r.narration),
        )
        for r in rows
    ]


def _read_csv(path: Path, model: type[BaseModel]) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [model.model_validate(row) for row in reader]


def load_and_normalise(profile_dir: Path) -> NormalisedDataset:
    gateway_raw = _read_csv(profile_dir / "gateway_transactions.csv", GatewayTransaction)
    settlement_raw = _read_csv(profile_dir / "settlement_report.csv", SettlementLine)
    bank_raw = _read_csv(profile_dir / "bank_statement.csv", BankStatementLine)
    erp_raw = _read_csv(profile_dir / "erp_ledger.csv", ErpInvoice)

    return NormalisedDataset(
        gateway_transactions=normalise_gateway_transactions(gateway_raw),
        settlement_lines=settlement_raw,  # already typed/int-paise; nothing left to normalise
        bank_lines=normalise_bank_lines(bank_raw),
        erp_invoices=erp_raw,
    )
