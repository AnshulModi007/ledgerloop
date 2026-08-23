"""Phase 4 acceptance: every proposed journal batch balances (debits == credits),
and re-running an already-approved batch produces zero new postings. See
IMPLEMENTATION.md section 4.
"""

from __future__ import annotations

import pytest

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import NullProvider
from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.ledger import journal
from ledgerloop.ledger.idempotency import filter_new_postings, posting_key
from ledgerloop.match import tier2_algorithmic
from ledgerloop.schemas import Resolution


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def dev_pipeline_state(tmp_path_factory, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    out_dir = tmp_path_factory.mktemp("dev_idempotency") / "dev"
    write_dataset(ds, out_dir, seed=seed, config=config)
    normalised = load_and_normalise(out_dir)

    tier2_result = tier2_algorithmic.run(normalised, config)
    tier3_result = adjudicator.run(normalised, tier2_result, config, [NullProvider()])
    resolutions = tier2_result.resolutions + tier3_result.resolutions

    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    settlement_lines_by_txn = {line.txn_id: line for line in normalised.settlement_lines}
    return resolutions, settlement_lines_by_txn, bank_line_by_id


def test_every_journal_batch_balances(dev_pipeline_state):
    resolutions, settlement_lines_by_txn, bank_line_by_id = dev_pipeline_state
    batches = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)
    assert batches, "expected at least one journal batch on the dev set"

    unbalanced = []
    for batch in batches:
        debits = sum(p.amount_paise for p in batch.postings if p.direction == "debit")
        credits = sum(p.amount_paise for p in batch.postings if p.direction == "credit")
        if debits != credits:
            unbalanced.append((batch.bank_line_id, debits, credits))
    assert not unbalanced, f"unbalanced journal batches: {unbalanced}"


def test_every_posting_amount_is_positive(dev_pipeline_state):
    resolutions, settlement_lines_by_txn, bank_line_by_id = dev_pipeline_state
    batches = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)
    for batch in batches:
        for posting in batch.postings:
            assert posting.amount_paise > 0, f"{batch.bank_line_id}/{posting.posting_type} has non-positive amount"


def test_reproposing_the_same_resolutions_produces_identical_postings(dev_pipeline_state):
    resolutions, settlement_lines_by_txn, bank_line_by_id = dev_pipeline_state
    batches_a = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)
    batches_b = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)

    keys_a = sorted(p.idempotency_key for batch in batches_a for p in batch.postings)
    keys_b = sorted(p.idempotency_key for batch in batches_b for p in batch.postings)
    assert keys_a == keys_b


def test_rerunning_an_approved_batch_produces_zero_new_postings(dev_pipeline_state):
    resolutions, settlement_lines_by_txn, bank_line_by_id = dev_pipeline_state
    batches = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)

    # simulate approval: every key from the first proposal is now "already posted"
    approved_keys = {p.idempotency_key for batch in batches for p in batch.postings}

    # re-run the identical proposal (as `make demo`'s second pass would)
    rerun_batches = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)
    for batch in rerun_batches:
        new_postings = filter_new_postings(batch.postings, approved_keys)
        assert new_postings == [], f"{batch.bank_line_id} produced new postings on re-run: {new_postings}"


def test_a_genuinely_new_resolution_still_produces_new_postings(dev_pipeline_state):
    """Idempotency must not become a black hole -- a resolution that wasn't part of
    the approved set should still post normally."""
    resolutions, settlement_lines_by_txn, bank_line_by_id = dev_pipeline_state
    already_approved = resolutions[:-1]
    newly_resolved = resolutions[-1]

    approved_batches = journal.propose_postings(already_approved, settlement_lines_by_txn, bank_line_by_id)
    approved_keys = {p.idempotency_key for batch in approved_batches for p in batch.postings}

    full_batches = journal.propose_postings(resolutions, settlement_lines_by_txn, bank_line_by_id)
    new_batch = next(b for b in full_batches if b.bank_line_id == newly_resolved.bank_line_id)
    new_postings = filter_new_postings(new_batch.postings, approved_keys)
    assert new_postings == new_batch.postings  # none of these were in the approved set


def test_posting_key_is_deterministic_and_batch_id_scoped():
    key_a = posting_key("BANK00001", ["TXN000001"], "fee_expense")
    key_b = posting_key("BANK00001", ["TXN000001"], "fee_expense")
    key_c = posting_key("BANK00002", ["TXN000001"], "fee_expense")
    assert key_a == key_b
    assert key_a != key_c


def test_idempotency_key_ignores_source_id_order():
    key_a = posting_key("BANK00001", ["TXN000001", "TXN000002"], "bank_receipt")
    key_b = posting_key("BANK00001", ["TXN000002", "TXN000001"], "bank_receipt")
    assert key_a == key_b


def test_resolutions_that_reference_a_missing_settlement_line_are_skipped_not_crashed():
    """Defensive: a Resolution whose matched_txn_id has no corresponding
    SettlementLine (shouldn't happen in practice, but journal.py must not crash)."""
    resolution = Resolution(
        bank_line_id="BANK99999",
        matched_txn_ids=["TXN_DOES_NOT_EXIST"],
        resolved_by="tier1",
        confidence=1.0,
        evidence={},
        audit_id="AUD-test",
    )
    from datetime import date

    from ledgerloop.ingest.normalise import NormalisedBankLine

    bank_line = NormalisedBankLine(
        bank_line_id="BANK99999",
        value_date=date(2026, 1, 1),
        credit_amount_paise=1000,
        narration="test",
        extracted_utr=None,
    )
    batches = journal.propose_postings([resolution], {}, {"BANK99999": bank_line})
    assert len(batches) == 1
    # only the bank_receipt leg exists -- no settlement line means no per-txn legs,
    # and the whole credit becomes a rounding_adjustment residual against zero net.
    posting_types = {p.posting_type for p in batches[0].postings}
    assert posting_types == {"bank_receipt", "rounding_adjustment"}
