"""Whole-run tie-out and its controls -- ledger/tieout.py.

test_idempotency.py proves every batch balances *internally*. These tests cover what that
cannot: whether the run as a whole ties back to the bank statement it came from. The two
are different questions, and the generalization suite already turned up one failure that
only the second can see (a transaction whose receivable was cleared by two bank lines,
where every batch still balanced).
"""

from __future__ import annotations

import pytest

from ledgerloop import pipeline
from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ledger import tieout
from ledgerloop.ledger.journal import Posting


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def dev_run(tmp_path_factory, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    data_root = tmp_path_factory.mktemp("tieout_data")
    write_dataset(ds, data_root / "dev", seed=seed, config=config)
    return pipeline.run(data_root, "dev", tmp_path_factory.mktemp("tieout_runs"), no_llm=True)


def _posting(account: str, direction: str, amount: int, *, bank_line_id="BANK00001", txn_id=None, ptype="x") -> Posting:
    return Posting(
        bank_line_id=bank_line_id,
        posting_type=ptype,
        account=account,
        direction=direction,
        amount_paise=amount,
        txn_id=txn_id,
        idempotency_key=f"{bank_line_id}-{account}-{direction}-{amount}-{txn_id}",
    )


def test_the_dev_run_ties_out_and_balances(dev_run):
    t = dev_run.tie_out
    assert t.balances, f"debits {t.total_debits_paise} != credits {t.total_credits_paise}"
    assert t.cash_ties_out, (
        f"bank receipts posted {t.bank_receipt_total_paise} but {t.reconciled_paise} was reconciled -- "
        "the ledger and the statement disagree"
    )
    assert t.clean


def test_reconciled_plus_unreconciled_accounts_for_the_whole_statement(dev_run):
    """No rupee may go missing between the statement and the report. This is the
    value-weighted counterpart to the taxonomy's "nothing is silently dropped"."""
    t = dev_run.tie_out
    assert t.reconciled_paise + t.unreconciled_paise == t.statement_total_paise
    assert t.reconciled_line_count + t.unreconciled_line_count == t.statement_line_count
    assert t.unreconciled_paise > 0, "the dev set escalates lines, so some value must be unreconciled"


def test_fee_drift_absorbed_is_reported_in_aggregate(dev_run):
    """Tier 2 tolerates drift per line and posts the residual explicitly, but until this
    report existed the total was only ever visible one posting at a time. It is the number
    that says how much the tolerance actually let through."""
    t = dev_run.tie_out
    assert t.rounding_adjustment_count > 0, "the dev set contains FEE_DRIFT, so there should be residuals"
    assert t.rounding_adjustment_gross_paise >= abs(t.rounding_adjustment_net_paise)
    tolerance = dev_run.config["tier2"]["amount_tolerance_paise"]
    per_posting = t.rounding_adjustment_gross_paise / t.rounding_adjustment_count
    assert per_posting <= tolerance, "a residual larger than the tolerance should never have been matched"


def test_cash_control_fails_when_the_ledger_disagrees_with_the_statement():
    """The control has to be able to fail, or asserting it passes on dev proves nothing."""
    postings = [_posting("bank_account", "debit", 900), _posting("settlement_receivable", "credit", 900)]
    t = tieout.build(postings, {"BANK00001": 1000}, {"BANK00001"})
    assert not t.cash_ties_out  # posted 900 against a 1000 credit
    assert not t.clean
    assert "TIE-OUT NOT CLEAN" in tieout.format_report(t)


def test_balance_control_fails_on_a_one_sided_run():
    postings = [_posting("bank_account", "debit", 1000)]
    t = tieout.build(postings, {"BANK00001": 1000}, {"BANK00001"})
    assert t.cash_ties_out
    assert not t.balances
    assert not t.clean


def test_duplicate_relief_makes_a_balancing_run_unclean():
    """The case the generalization suite found: every batch balances, cash ties out, and
    the run is still wrong. Nothing else in the system reports this."""
    postings = [
        _posting("bank_account", "debit", 500, bank_line_id="BANK00001"),
        _posting("settlement_receivable", "credit", 500, bank_line_id="BANK00001", txn_id="TXN1", ptype="settlement_receivable_clear"),
        _posting("bank_account", "debit", 500, bank_line_id="BANK00002"),
        _posting("settlement_receivable", "credit", 500, bank_line_id="BANK00002", txn_id="TXN1", ptype="settlement_receivable_clear"),
    ]
    t = tieout.build(postings, {"BANK00001": 500, "BANK00002": 500}, {"BANK00001", "BANK00002"})

    assert t.balances and t.cash_ties_out  # both of the obvious controls pass
    assert t.duplicate_receivable_relief == {"TXN1": ["BANK00001", "BANK00002"]}
    assert not t.clean, "a run where one receivable is relieved twice is not clean, however well it balances"


def test_movements_cover_every_posting(dev_run):
    t = dev_run.tie_out
    assert sum(m.posting_count for m in t.movements) == len(dev_run.all_postings)
    assert sum(m.debit_paise for m in t.movements) == t.total_debits_paise
    assert sum(m.credit_paise for m in t.movements) == t.total_credits_paise
    assert [m.account for m in t.movements] == sorted(m.account for m in t.movements)
