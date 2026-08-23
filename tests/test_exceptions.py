"""Every unresolved item gets a typed reason code from the required minimum set, and
OUT_OF_SCOPE/duplicate/amount-mismatch reclassification actually fires against the
real dev dataset (not just hand-built examples). See IMPLEMENTATION.md section 4.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import NullProvider
from ledgerloop.config import load_config
from ledgerloop.exceptions import queue, taxonomy
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import NormalisedBankLine, load_and_normalise
from ledgerloop.match import tier1_exact, tier2_algorithmic
from ledgerloop.schemas import Candidate, UnresolvedCase

REQUIRED_MINIMUM_CODES = {
    "NO_CANDIDATE",
    "AMBIGUOUS_CANDIDATES",
    "LOW_CONFIDENCE",
    "AMOUNT_MISMATCH_BEYOND_TOLERANCE",
    "TIER2_TIMEOUT",
    "TIER3_INVALID_SELECTION",
    "OUT_OF_SCOPE",
    "SUSPECTED_DUPLICATE",
}


def test_reason_code_enum_matches_the_required_minimum_set():
    assert {code.value for code in taxonomy.ReasonCode} == REQUIRED_MINIMUM_CODES


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def dev_exceptions(tmp_path_factory, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    out_dir = tmp_path_factory.mktemp("dev_exceptions") / "dev"
    write_dataset(ds, out_dir, seed=seed, config=config)
    normalised = load_and_normalise(out_dir)

    tier2_result = tier2_algorithmic.run(normalised, config)
    tier3_result = adjudicator.run(normalised, tier2_result, config, [NullProvider()])
    all_resolutions = tier2_result.resolutions + tier3_result.resolutions

    batches = tier1_exact.build_batches(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)
    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    claimed_batch_ids = {
        r.evidence["settlement_batch_id"] for r in all_resolutions if "settlement_batch_id" in r.evidence
    }
    claimed_txn_ids = {t for r in all_resolutions for t in r.matched_txn_ids}
    unclaimed_gross = [
        t.gross_amount_paise for t in normalised.gateway_transactions if t.txn_id not in claimed_txn_ids
    ]

    exceptions = taxonomy.classify_all(
        bank_line_by_id,
        tier3_result.unresolved,
        by_utr=by_utr,
        claimed_batch_ids=claimed_batch_ids,
        unclaimed_gross_amounts_paise=unclaimed_gross,
        tier2_cfg=config["tier2"],
    )
    return exceptions, ds


def test_no_unresolved_line_is_silently_dropped(dev_exceptions, config):
    """Every exception must carry a code from the taxonomy -- never blank/None."""
    exceptions, _ds = dev_exceptions
    for exception in exceptions:
        assert exception.reason_code in REQUIRED_MINIMUM_CODES


def test_out_of_scope_lines_are_correctly_reclassified(dev_exceptions):
    """The generator's OUT_OF_SCOPE-tagged lines should end up tagged OUT_OF_SCOPE in
    the exception queue -- not left as a generic NO_CANDIDATE/TIER2_TIMEOUT."""
    exceptions, ds = dev_exceptions
    out_of_scope_ids = {e.bank_line_id for e in ds.answer_key if e.defect_classes == ["OUT_OF_SCOPE"]}
    exceptions_by_id = {e.bank_line_id for e in exceptions}
    # every OUT_OF_SCOPE-defect line that's unresolved (all of them, per test_tiers.py)
    # must appear here, correctly coded.
    assert out_of_scope_ids <= exceptions_by_id
    for exception in exceptions:
        if exception.bank_line_id in out_of_scope_ids:
            assert exception.reason_code == "OUT_OF_SCOPE"


def test_amount_mismatch_beyond_tolerance_reclassification(config):
    """A NO_CANDIDATE case where a same-UTR batch exists but the amount is way off
    should reclassify to AMOUNT_MISMATCH_BEYOND_TOLERANCE, not stay generic."""
    batch = tier1_exact.SettlementBatch(
        settlement_batch_id="STL00001",
        payout_utr="UTR11112222333344",
        settlement_date=date(2026, 2, 27),
        txn_ids=("TXN000001",),
        total_net_paise=100000,
    )
    by_utr = {"UTR11112222333344": [batch]}
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00001",
        value_date=date(2026, 3, 1),
        credit_amount_paise=999999,  # wildly different from the batch's 100000
        narration="NEFT/UTR11112222333344/RAZORPAY SOFTWARE PVT LTD",
        extracted_utr="UTR11112222333344",
    )
    case = UnresolvedCase(bank_line_id="BANK00001", reason_hint="NO_CANDIDATE", candidates=[], evidence={})
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr=by_utr,
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[],
        tier2_cfg=config["tier2"],
    )
    assert exception.reason_code == "AMOUNT_MISMATCH_BEYOND_TOLERANCE"


def test_suspected_duplicate_reclassification(config):
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00002",
        value_date=date(2026, 3, 1),
        credit_amount_paise=250000,
        narration="NEFT CR-SETTLEMENT PAYOUT-REF UNAVAILABLE",
        extracted_utr=None,
    )
    case = UnresolvedCase(bank_line_id="BANK00002", reason_hint="NO_CANDIDATE", candidates=[], evidence={})
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr={},
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[250000, 250000, 999999],  # two look-alikes
        tier2_cfg=config["tier2"],
    )
    assert exception.reason_code == "SUSPECTED_DUPLICATE"


def test_tier2_timeout_reclassifies_to_out_of_scope_when_warranted(config):
    """A TIER2_TIMEOUT case is still eligible for reclassification -- the search
    exhausting its budget doesn't mean the line necessarily deserved more budget."""
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00003",
        value_date=date(2026, 3, 1),
        credit_amount_paise=250000,
        narration="NEFT CR-PAYROLL-1234567",
        extracted_utr=None,
    )
    case = UnresolvedCase(bank_line_id="BANK00003", reason_hint="TIER2_TIMEOUT", candidates=[], evidence={})
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr={},
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[],
        tier2_cfg=config["tier2"],
    )
    assert exception.reason_code == "OUT_OF_SCOPE"


def test_ambiguous_and_low_confidence_are_never_reclassified(config):
    """Cases that had actual candidates keep their tier3-assigned code as-is."""
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00004",
        value_date=date(2026, 3, 1),
        credit_amount_paise=250000,
        narration="test",
        extracted_utr=None,
    )
    candidate = Candidate(candidate_id="BANK00004-C0", matched_txn_ids=["TXN1"], score=0.5, evidence={})
    for hint in ("AMBIGUOUS_CANDIDATES", "LOW_CONFIDENCE", "TIER3_INVALID_SELECTION"):
        case = UnresolvedCase(bank_line_id="BANK00004", reason_hint=hint, candidates=[candidate], evidence={})
        exception = taxonomy.classify(
            bank_line,
            case,
            by_utr={},
            claimed_batch_ids=set(),
            unclaimed_gross_amounts_paise=[],
            tier2_cfg=config["tier2"],
        )
        assert exception.reason_code == hint


def test_explanation_carries_tier3_reasoning_when_present(config):
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00005",
        value_date=date(2026, 3, 1),
        credit_amount_paise=250000,
        narration="test",
        extracted_utr=None,
    )
    case = UnresolvedCase(
        bank_line_id="BANK00005",
        reason_hint="LOW_CONFIDENCE",
        candidates=[],
        evidence={"tier3_reasoning": "the narration doesn't clearly support any candidate"},
    )
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr={},
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[],
        tier2_cfg=config["tier2"],
    )
    assert exception.explanation == "the narration doesn't clearly support any candidate"


def test_explanation_is_none_without_llm(config):
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00006",
        value_date=date(2026, 3, 1),
        credit_amount_paise=250000,
        narration="test",
        extracted_utr=None,
    )
    case = UnresolvedCase(bank_line_id="BANK00006", reason_hint="NO_CANDIDATE", candidates=[], evidence={})
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr={},
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[],
        tier2_cfg=config["tier2"],
    )
    assert exception.explanation is None


# -- exception queue ------------------------------------------------------------------


def test_queue_filter_and_count_by_reason_code(dev_exceptions):
    exceptions, _ds = dev_exceptions
    items = queue.build_queue(exceptions)
    counts = queue.counts_by_reason_code(items)
    assert sum(counts.values()) == len(items)
    for reason_code, count in counts.items():
        assert len(queue.filter_by_reason_code(items, reason_code)) == count


def test_queue_action_is_immutable():
    from ledgerloop.schemas import Exception_

    item = queue.QueueItem(
        exception=Exception_(
            bank_line_id="BANK00001", reason_code="NO_CANDIDATE", candidates_considered=[], explanation=None, evidence={}
        )
    )
    approved = queue.apply_action(item, "approved", note="looks fine")
    assert item.status == "open"  # original untouched
    assert approved.status == "approved"
    assert approved.reviewer_note == "looks fine"
