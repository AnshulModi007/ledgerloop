"""Defect shapes the matcher was never designed for -- the generalization suite.

Every accuracy figure elsewhere in this repo is scored against the twelve classes in
generate/defects.py, and tier2's matching rules were written knowing all twelve. That
makes those numbers a measure of implementation, not of generalization: the same author
wrote the exam and the student. This module writes an exam the student never saw.

The shapes below are deliberately absent from DefectClass and were chosen to break
*assumptions* rather than to add arithmetic -- each one violates something the matching
tiers quietly take to be true:

  WIRE_FEE_DEDUCTION     the credited amount equals the settled net (a bank can levy its
                         own charge on the payout, and it is far outside the fee-drift
                         tolerance the pipeline was built to absorb)
  STALE_UTR_REUSE        a UTR in narration identifies this credit (banks recycle
                         reference strings; here one names a batch already paid in full)
  POST_DATED_SETTLEMENT  a credit lands on or after its settlement date (value-dating can
                         put the money in the account before the settlement it belongs to)
  DOUBLE_SETTLEMENT      a transaction settles at most once (a gateway bug can pay the
                         same transaction inside two different batches)

**The pass criterion is not resolution.** These are unfamiliar inputs, and failing to
match them is an acceptable outcome. What is not acceptable is a *wrong* match, or a line
that vanishes without a typed reason. The suite asserts what the whole design claims --
that the system refuses rather than guesses -- against data the design never anticipated.
See eval/generalization.py for the scoring and the gate.

STALE_UTR_REUSE is the real trap. The others make the amount or the date look wrong,
which is the easy direction to fail safely in; that one makes the *evidence* look right
while pointing at the wrong batch, which is the direction a matcher actually gets
embarrassed in.

Fixed seed, no wall-clock input: the suite is reproducible like every other dataset here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path

import click

from ledgerloop.config import load_config
from ledgerloop.generate.defects import ALL_DEFECT_CLASSES, DefectClass
from ledgerloop.generate.generator import (
    MAX_GROSS_PAISE,
    MERCHANT_REF,
    MIN_GROSS_PAISE,
    NARRATION_TEMPLATES,
    PAYMENT_METHODS,
    GeneratedDataset,
    IdSequence,
    _new_utr,
    write_dataset,
)
from ledgerloop.generate.schemas import (
    AnswerKeyEntry,
    BankStatementLine,
    ErpInvoice,
    GatewayTransaction,
    SettlementLine,
)
from ledgerloop.match.fee_model import compute_net


class NovelShape(StrEnum):
    WIRE_FEE_DEDUCTION = "WIRE_FEE_DEDUCTION"
    STALE_UTR_REUSE = "STALE_UTR_REUSE"
    POST_DATED_SETTLEMENT = "POST_DATED_SETTLEMENT"
    DOUBLE_SETTLEMENT = "DOUBLE_SETTLEMENT"


SHAPE_DESCRIPTIONS: dict[NovelShape, str] = {
    NovelShape.WIRE_FEE_DEDUCTION: (
        "The bank levies its own flat charge on the payout, so the credit is short of the "
        "settled net by far more than the fee-rounding tolerance. Linkage is unambiguous; "
        "the amount is not."
    ),
    NovelShape.STALE_UTR_REUSE: (
        "An unrelated credit carries, in its narration, the payout UTR of a batch that was "
        "already paid in full by an earlier line. Strong-looking evidence pointing at the "
        "wrong batch."
    ),
    NovelShape.POST_DATED_SETTLEMENT: (
        "The bank value-dates the credit three days BEFORE the settlement it pays, so the "
        "credit precedes its own settlement date."
    ),
    NovelShape.DOUBLE_SETTLEMENT: (
        "A gateway bug settles one transaction inside two different batches, and both "
        "batches are paid out. Each credit is individually legitimate."
    ),
}

# Whether ground truth says a correct match exists for this shape at all. STALE_UTR_REUSE
# is the only one where the right answer is "match nothing" -- which makes any resolution
# of it a false match by definition, and is precisely what the trap is for.
SHAPE_EXPECTS_MATCH: dict[NovelShape, bool] = {
    NovelShape.WIRE_FEE_DEDUCTION: True,
    NovelShape.STALE_UTR_REUSE: False,
    NovelShape.POST_DATED_SETTLEMENT: True,
    NovelShape.DOUBLE_SETTLEMENT: True,
}


@dataclass
class BuiltBatch:
    batch_id: str
    payout_utr: str
    txns: list[GatewayTransaction]
    lines: list[SettlementLine]
    settlement_date: date
    total_net_paise: int


class NovelGenerator:
    """Builds the generalization dataset. Deliberately a separate, small builder rather
    than another branch inside generator.py: the graded dev and holdout sets must keep
    regenerating byte-for-byte identical, and novel shapes have no business anywhere near
    the code path that produces them."""

    def __init__(self, seed: int, config: dict):
        self.rng = random.Random(seed)
        self.cfg = config["generate"]
        self.fee_cfg = config["fee"]
        self.ids = IdSequence()
        self.ds = GeneratedDataset()
        self.start_date = date.fromisoformat(self.cfg["start_date"])
        self.per_shape = self.cfg["novel_instances_per_shape"]
        self.wire_fee_paise = self.cfg["novel_wire_fee_paise"]

    # -- primitives ---------------------------------------------------------------

    def _a_date(self, day_offset: int) -> date:
        return self.start_date + timedelta(days=day_offset)

    def _make_txn(self, captured: date) -> GatewayTransaction:
        return GatewayTransaction(
            txn_id=self.ids.next_txn(),
            order_id=self.ids.next_order(),
            rrn=str(self.rng.randrange(10**11, 10**12)),
            gross_amount_paise=self.rng.randint(MIN_GROSS_PAISE, MAX_GROSS_PAISE),
            captured_at=datetime.combine(captured, time(hour=self.rng.randint(6, 22), minute=self.rng.randint(0, 59))),
            payment_method=self.rng.choice(PAYMENT_METHODS),
            status="captured",
            merchant_ref=MERCHANT_REF,
        )

    def _settle(self, batch_id: str, utr: str, txn: GatewayTransaction, settlement_date: date) -> SettlementLine:
        breakdown = compute_net(
            txn.gross_amount_paise,
            platform_fee_bps=self.fee_cfg["platform_fee_bps"],
            gst_rate_bps=self.fee_cfg["gst_rate_bps"],
            tds_bps=self.fee_cfg["tds_bps"],
        )
        return SettlementLine(
            settlement_batch_id=batch_id,
            txn_id=txn.txn_id,
            payout_utr=utr,
            gross_amount_paise=breakdown.gross_paise,
            fee_paise=breakdown.fee_paise,
            gst_on_fee_paise=breakdown.gst_on_fee_paise,
            tds_paise=breakdown.tds_paise,
            refund_paise=0,
            chargeback_paise=0,
            net_paise=breakdown.net_paise,
            settlement_date=settlement_date,
        )

    def _build_batch(self, n_txns: int, settlement_date: date, *, txns: list[GatewayTransaction] | None = None) -> BuiltBatch:
        """txns may be supplied to place an already-existing transaction into a second
        batch -- which is the whole of DOUBLE_SETTLEMENT."""
        batch_id = self.ids.next_batch()
        utr = _new_utr(self.rng)
        captured = settlement_date - timedelta(days=2)  # T+2, as everywhere else
        built_txns = txns if txns is not None else [self._make_txn(captured) for _ in range(n_txns)]
        lines = [self._settle(batch_id, utr, t, settlement_date) for t in built_txns]
        self.ds.gateway_transactions.extend(t for t in built_txns if txns is None or t not in self.ds.gateway_transactions)
        self.ds.settlement_lines.extend(lines)
        return BuiltBatch(
            batch_id=batch_id,
            payout_utr=utr,
            txns=built_txns,
            lines=lines,
            settlement_date=settlement_date,
            total_net_paise=sum(line.net_paise for line in lines),
        )

    def _emit_bank_line(
        self,
        *,
        value_date: date,
        amount_paise: int,
        narration_utr: str,
        matched_txns: list[GatewayTransaction],
        batch_ids: list[str],
        label: str,
        notes: str,
    ) -> None:
        bank_line_id = self.ids.next_bank_line()
        self.ds.bank_lines.append(
            BankStatementLine(
                bank_line_id=bank_line_id,
                value_date=value_date,
                credit_amount_paise=amount_paise,
                narration=self.rng.choice(NARRATION_TEMPLATES).format(utr=narration_utr),
            )
        )
        self.ds.answer_key.append(
            AnswerKeyEntry(
                bank_line_id=bank_line_id,
                matched_txn_ids=[t.txn_id for t in matched_txns],
                settlement_batch_ids=batch_ids,
                defect_classes=[label],
                notes=notes,
            )
        )
        self.ds.defect_counts[label] = self.ds.defect_counts.get(label, 0) + 1

    # -- shapes -------------------------------------------------------------------

    def _clean_controls(self, n: int) -> list[BuiltBatch]:
        """Ordinary traffic. Present so the suite is not 100% adversarial (a pipeline that
        escalated literally everything would otherwise score a perfect zero false matches),
        and so STALE_UTR_REUSE has genuinely-paid batches whose UTRs it can recycle."""
        built = []
        for i in range(n):
            batch = self._build_batch(self.rng.randint(1, 3), self._a_date(3 + i))
            self._emit_bank_line(
                value_date=batch.settlement_date,
                amount_paise=batch.total_net_paise,
                narration_utr=batch.payout_utr,
                matched_txns=batch.txns,
                batch_ids=[batch.batch_id],
                label=DefectClass.CLEAN.value,
                notes="ordinary settlement, present as a control",
            )
            built.append(batch)
        return built

    def _wire_fee_deduction(self) -> None:
        for i in range(self.per_shape):
            batch = self._build_batch(self.rng.randint(2, 4), self._a_date(30 + i))
            self._emit_bank_line(
                value_date=batch.settlement_date,
                amount_paise=batch.total_net_paise - self.wire_fee_paise,
                narration_utr=batch.payout_utr,
                matched_txns=batch.txns,
                batch_ids=[batch.batch_id],
                label=NovelShape.WIRE_FEE_DEDUCTION.value,
                notes=f"bank levied {self.wire_fee_paise} paise on the payout; the batch itself is correct",
            )

    def _stale_utr_reuse(self, paid_batches: list[BuiltBatch]) -> None:
        for i in range(self.per_shape):
            recycled = paid_batches[i % len(paid_batches)]
            # An amount unrelated to any batch: the trap is the misleading reference, not
            # an amount coincidence. Made deliberately non-round so it cannot collide with
            # a real net by accident.
            amount = self.rng.randint(MIN_GROSS_PAISE, MAX_GROSS_PAISE) + 7
            self._emit_bank_line(
                value_date=self._a_date(50 + i),
                amount_paise=amount,
                narration_utr=recycled.payout_utr,
                matched_txns=[],
                batch_ids=[],
                label=NovelShape.STALE_UTR_REUSE.value,
                notes=f"narration recycles {recycled.batch_id}'s UTR; this credit matches nothing",
            )

    def _post_dated_settlement(self) -> None:
        for i in range(self.per_shape):
            batch = self._build_batch(self.rng.randint(1, 3), self._a_date(70 + i))
            self._emit_bank_line(
                value_date=batch.settlement_date - timedelta(days=3),
                amount_paise=batch.total_net_paise,
                narration_utr=batch.payout_utr,
                matched_txns=batch.txns,
                batch_ids=[batch.batch_id],
                label=NovelShape.POST_DATED_SETTLEMENT.value,
                notes="bank value-dated the credit three days before the settlement date",
            )

    def _double_settlement(self) -> None:
        for i in range(self.per_shape):
            first = self._build_batch(2, self._a_date(85 + i))
            shared = first.txns[0]
            extra = self._make_txn(self._a_date(85 + i) - timedelta(days=2))
            self.ds.gateway_transactions.append(extra)
            second = self._build_batch(0, self._a_date(87 + i), txns=[shared, extra])

            for batch in (first, second):
                self._emit_bank_line(
                    value_date=batch.settlement_date,
                    amount_paise=batch.total_net_paise,
                    narration_utr=batch.payout_utr,
                    matched_txns=batch.txns,
                    batch_ids=[batch.batch_id],
                    label=NovelShape.DOUBLE_SETTLEMENT.value,
                    notes=f"{shared.txn_id} is settled in both {first.batch_id} and {second.batch_id}",
                )

    # -- assembly -----------------------------------------------------------------

    def generate(self) -> GeneratedDataset:
        controls = self._clean_controls(self.cfg["novel_clean_controls"])
        self._wire_fee_deduction()
        self._stale_utr_reuse(controls)
        self._post_dated_settlement()
        self._double_settlement()

        for txn in self.ds.gateway_transactions:
            self.ds.erp_invoices.append(
                ErpInvoice(
                    invoice_id=self.ids.next_invoice(),
                    order_id=txn.order_id,
                    expected_amount_paise=txn.gross_amount_paise,
                    status="open",
                )
            )

        _assert_shapes_are_novel()
        return self.ds


def _assert_shapes_are_novel() -> None:
    """The suite's entire value rests on these shapes being absent from the taxonomy the
    matcher was built against. If someone later adds one to DefectClass, this stops being
    a generalization test and silently becomes another in-distribution one -- so fail
    loudly at generation time rather than keep reporting a claim that has quietly expired."""
    known = {c.value for c in ALL_DEFECT_CLASSES}
    overlap = sorted(known & {s.value for s in NovelShape})
    if overlap:
        raise ValueError(
            f"novel shapes {overlap} are now in DefectClass -- the matcher may have been "
            "built against them, so they no longer test generalization. Replace them."
        )


def generate_novel(config: dict, out_root: Path) -> GeneratedDataset:
    seed = config["generate"]["novel_seed"]
    ds = NovelGenerator(seed, config).generate()
    write_dataset(ds, out_root / "novel", seed=seed, config=config)
    return ds


@click.command()
@click.option("--config-path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-root", type=click.Path(path_type=Path), default=Path("data"))
def main(config_path: Path | None, out_root: Path) -> None:
    """Generate the generalization suite (data/novel)."""
    config = load_config(config_path) if config_path else load_config()
    ds = generate_novel(config, out_root)
    click.echo(
        f"[novel] wrote {len(ds.gateway_transactions)} gateway txns, "
        f"{len(ds.bank_lines)} bank lines -> {out_root / 'novel'}"
    )
    for shape, count in sorted(ds.defect_counts.items()):
        click.echo(f"  {shape}: {count}")


if __name__ == "__main__":
    main()
