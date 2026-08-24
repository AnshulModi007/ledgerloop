"""Phase 5 evaluation harness: metrics arithmetic against hand-built ground truth,
plus an end-to-end run over generated dev data so the CI gate values
(config.yaml's eval.*_gate) are exercised by pytest too, not only by the CI step
that shells out to `ledgerloop eval metrics`. See IMPLEMENTATION.md section 4.
"""

from __future__ import annotations

import pytest

from ledgerloop.config import load_config
from ledgerloop.eval import ablation, harness
from ledgerloop.eval.calibration import compute_calibration
from ledgerloop.eval.harness import HarnessRun
from ledgerloop.eval.metrics import compute_metrics
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.generate.schemas import AnswerKeyEntry
from ledgerloop.schemas import Resolution


def _answer(bank_line_id: str, matched_txn_ids: list[str]) -> AnswerKeyEntry:
    return AnswerKeyEntry(
        bank_line_id=bank_line_id,
        matched_txn_ids=matched_txn_ids,
        settlement_batch_ids=[],
        defect_classes=["CLEAN"] if matched_txn_ids else ["OUT_OF_SCOPE"],
    )


def _resolution(bank_line_id: str, matched_txn_ids: list[str], *, resolved_by="tier1", confidence=1.0) -> Resolution:
    return Resolution(
        bank_line_id=bank_line_id,
        matched_txn_ids=matched_txn_ids,
        resolved_by=resolved_by,
        confidence=confidence,
        evidence={},
        audit_id=f"audit-{bank_line_id}",
    )


def test_compute_metrics_arithmetic():
    """4 ground-truth lines: a correct match, a false match (resolved but wrong txn),
    a miss (should have matched but was left as an exception), and a correctly
    escalated true negative (OUT_OF_SCOPE, correctly left unresolved)."""
    answer_key = {
        "B1": _answer("B1", ["TXN1"]),
        "B2": _answer("B2", ["TXN2"]),
        "B3": _answer("B3", ["TXN3"]),
        "B4": _answer("B4", []),
    }
    resolutions = [
        _resolution("B1", ["TXN1"]),  # correct
        _resolution("B2", ["TXN_WRONG"]),  # false match
        # B3 left unresolved -- a miss
        # B4 left unresolved -- correct escalation
    ]
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=4, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
    )
    m = compute_metrics(run, answer_key)

    assert m.resolved_count == 2
    assert m.auto_match_rate == pytest.approx(0.5)
    assert m.precision == pytest.approx(0.5)  # 1 correct of 2 resolved
    assert m.recall == pytest.approx(1 / 3)  # 1 correct of 3 that expected a match
    assert m.false_match_count == 1
    assert m.false_match_rate == pytest.approx(0.25)  # 1 of 4 total records
    assert m.missed_count == 1


def test_compute_metrics_resolving_an_out_of_scope_line_counts_as_false_match():
    answer_key = {"B1": _answer("B1", [])}
    resolutions = [_resolution("B1", ["TXN1"])]  # should have been escalated, not matched
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=1, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
    )
    m = compute_metrics(run, answer_key)
    assert m.false_match_count == 1
    assert m.precision == 0.0


def test_compute_calibration_bins_by_confidence_and_accuracy():
    answer_key = {"B1": _answer("B1", ["TXN1"]), "B2": _answer("B2", ["TXN2"])}
    resolutions = [
        _resolution("B1", ["TXN1"], resolved_by="tier3", confidence=0.87),  # correct, bin [0.8,0.9)
        _resolution("B2", ["TXN_WRONG"], resolved_by="tier3", confidence=0.92),  # wrong, bin [0.9,1.0)
        _resolution("B1", ["TXN1"], resolved_by="tier1", confidence=1.0),  # not tier3 -- excluded
    ]
    bins = compute_calibration(resolutions, answer_key)
    assert len(bins) == 2
    low_bin = next(b for b in bins if b.range_low == 0.8)
    high_bin = next(b for b in bins if b.range_low == 0.9)
    assert low_bin.n == 1 and low_bin.actual_accuracy == 1.0
    assert high_bin.n == 1 and high_bin.actual_accuracy == 0.0
    assert high_bin.gap == pytest.approx(0.92)  # fully overconfident: stated 0.92, actual 0


def test_compute_calibration_empty_without_tier3_resolutions():
    resolutions = [_resolution("B1", ["TXN1"], resolved_by="tier1")]
    assert compute_calibration(resolutions, {}) == []


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def generated_dev_data_root(tmp_path_factory, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    root = tmp_path_factory.mktemp("eval_dev_data")
    write_dataset(ds, root / "dev", seed=seed, config=config)
    return root


def test_full_no_llm_run_clears_the_configured_gates(generated_dev_data_root, config):
    """The same gate the CI eval step enforces, run here so a regression in matching
    accuracy fails `pytest` too, not just a separate CI shell step."""
    run = harness.run(generated_dev_data_root, "dev", "full", no_llm=True)
    answer_key = harness.load_answer_key(generated_dev_data_root, "dev")
    m = compute_metrics(run, answer_key)

    gates = config["eval"]
    assert m.false_match_rate <= gates["false_match_rate_gate"]
    assert m.auto_match_rate >= gates["auto_match_rate_gate"]


def test_ablation_rows_are_monotonically_non_decreasing(generated_dev_data_root):
    """Each tier can only add resolutions on top of the previous tier's, never take
    any away -- so resolved counts (and therefore auto-match rate) must never drop
    as the tier ceiling rises. With --no-llm, tiers1+2 and full are identical."""
    results = ablation.run_ablation(generated_dev_data_root, "dev", no_llm=True)
    assert results["tier1"].resolved_count <= results["tier1+2"].resolved_count
    assert results["tier1+2"].resolved_count <= results["full"].resolved_count
    assert results["tier1+2"].resolved_count == results["full"].resolved_count  # --no-llm on both
