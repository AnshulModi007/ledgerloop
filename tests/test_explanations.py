"""Every escalated line reaches a reviewer with a readable account of why, including on
the zero-key `--no-llm` path where no model ever weighs in.

This is the regression guard for the gap that motivated exceptions/explain.py: the
exception queue is the human-facing half of the product, and it previously handed a
reviewer a bare reason code plus an opaque candidate handle. See IMPLEMENTATION.md
section 5 (the Exception_ contract) and the explain.py module docstring.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledgerloop import pipeline
from ledgerloop.config import load_config
from ledgerloop.exceptions import explain, taxonomy
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import NormalisedBankLine
from ledgerloop.match import tier1_exact
from ledgerloop.schemas import Candidate, UnresolvedCase


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def no_llm_exceptions(tmp_path_factory, config):
    """A real end-to-end --no-llm run: the exact path `make demo` takes."""
    seed = config["generate"]["dev_seed"]
    root = tmp_path_factory.mktemp("explain_run")
    write_dataset(Generator(seed, config).generate(), root / "dev", seed=seed, config=config)
    result = pipeline.run(root, "dev", root / "runs", no_llm=True)
    return result.exceptions


def test_every_exception_carries_an_explanation_without_an_llm(no_llm_exceptions):
    """The headline guarantee. `explanation` is nullable by contract, but a null one on
    the zero-key path means a reviewer sees a reason code and nothing else."""
    assert no_llm_exceptions, "expected the dev profile to produce exceptions to check"
    missing = [e.bank_line_id for e in no_llm_exceptions if not (e.explanation or "").strip()]
    assert missing == [], f"exceptions escalated with no explanation: {missing}"


def test_explanations_quote_the_actual_credit_amount(no_llm_exceptions):
    """An explanation that doesn't name the amount in dispute isn't actionable."""
    for exception in no_llm_exceptions:
        expected = explain.rupees(exception.evidence["bank_credit_paise"])
        assert expected in exception.explanation, (
            f"{exception.bank_line_id}: explanation omits the credit amount {expected}"
        )


def test_exception_evidence_exposes_real_txn_ids_not_just_candidate_handles(no_llm_exceptions):
    """`candidates_considered` is list[str] by interface contract, so the transaction IDs
    a reviewer needs ride in evidence instead. Without this, "BANK00115-C0" is all they get."""
    with_candidates = [e for e in no_llm_exceptions if e.candidates_considered]
    assert with_candidates, "expected at least one exception that had candidates to weigh"
    for exception in with_candidates:
        detail = exception.evidence.get("candidate_detail")
        assert detail, f"{exception.bank_line_id}: candidates but no candidate_detail"
        assert len(detail) == len(exception.candidates_considered)
        for entry in detail:
            assert entry["matched_txn_ids"], "a candidate with no transaction IDs is not reviewable"


@pytest.mark.parametrize("reason_code", sorted(code.value for code in taxonomy.ReasonCode))
def test_every_reason_code_has_a_distinct_non_empty_explanation(reason_code, config):
    """All eight codes, including the ones the dev dataset happens not to produce."""
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00001",
        value_date=date(2026, 2, 12),
        credit_amount_paise=45231000,
        narration="NEFT/UTR11112222333344/RAZORPAY SOFTWARE PVT LTD",
        extracted_utr="UTR11112222333344",
    )
    case = UnresolvedCase(
        bank_line_id="BANK00001",
        reason_hint=reason_code,
        candidates=[
            Candidate(
                candidate_id="BANK00001-C0",
                matched_txn_ids=["TXN000001", "TXN000002"],
                score=0.68,
                evidence={"rule": "generic_cross_batch_subset_sum", "amount_diff_paise": 20000},
            )
        ],
        evidence={},
    )
    batch = tier1_exact.SettlementBatch(
        settlement_batch_id="STL00098",
        payout_utr="UTR11112222333344",
        settlement_date=date(2026, 2, 10),
        txn_ids=("TXN000001",),
        total_net_paise=45211000,
    )
    text = explain.build_explanation(
        bank_line, case, reason_code, config["tier2"], near_utr_batch=batch
    )
    assert len(text) > 60, f"{reason_code}: explanation too thin to be useful"
    assert explain.rupees(45231000) in text
    assert "_" not in text, f"{reason_code}: leaked a raw snake_case identifier into prose"


def test_model_reasoning_is_appended_not_substituted(config):
    """The containment argument for the explanation surface: a model may add narrative,
    but it can never replace a computed figure a reviewer acts on."""
    bank_line = NormalisedBankLine(
        bank_line_id="BANK00001",
        value_date=date(2026, 2, 12),
        credit_amount_paise=45231000,
        narration="NEFT/UTR11112222333344/ACME",
        extracted_utr="UTR11112222333344",
    )
    case = UnresolvedCase(
        bank_line_id="BANK00001",
        reason_hint="LOW_CONFIDENCE",
        candidates=[
            Candidate(
                candidate_id="BANK00001-C0",
                matched_txn_ids=["TXN000001"],
                score=0.55,
                evidence={"rule": "generic_cross_batch_subset_sum", "amount_diff_paise": 20000},
            )
        ],
        evidence={"tier3_reasoning": "the amounts look close enough to me"},
    )
    exception = taxonomy.classify(
        bank_line,
        case,
        by_utr={},
        claimed_batch_ids=set(),
        unclaimed_gross_amounts_paise=[],
        tier2_cfg=config["tier2"],
    )
    assert "Adjudicator note: the amounts look close enough to me" in exception.explanation
    # the computed facts survive alongside it
    assert explain.rupees(45231000) in exception.explanation
    assert "0.55" in exception.explanation


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (100, "₹1.00"),
        (99999, "₹999.99"),
        (100000, "₹1,000.00"),
        (45231000, "₹4,52,310.00"),  # Indian grouping, not ₹452,310.00
        (1000000000, "₹1,00,00,000.00"),  # one crore
        (-45231000, "-₹4,52,310.00"),
    ],
)
def test_rupee_formatting_uses_indian_digit_grouping(paise, expected):
    assert explain.rupees(paise) == expected


def test_rupee_formatting_is_exact_at_magnitudes_float_cannot_hold():
    """divmod, not `paise / 100`: at 17 significant digits a float has already lost the
    paise. A reviewer approves postings off this string."""
    assert explain.rupees(12345678901234567).endswith(".67")
