"""Phase 5 evaluation harness: metrics arithmetic against hand-built ground truth,
plus an end-to-end run over generated dev data so the CI gate values
(config.yaml's eval.*_gate) are exercised by pytest too, not only by the CI step
that shells out to `ledgerloop eval metrics`. See IMPLEMENTATION.md section 4.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from ledgerloop.config import load_config
from ledgerloop.eval import ablation, harness, scale, sensitivity
from ledgerloop.eval.calibration import compute_calibration
from ledgerloop.eval.harness import HarnessRun
from ledgerloop.eval.metrics import compute_metrics
from ledgerloop.generate.defects import ALL_DEFECT_CLASSES
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.generate.schemas import AnswerKeyEntry
from ledgerloop.schemas import Resolution


def _answer(
    bank_line_id: str, matched_txn_ids: list[str], defect_classes: list[str] | None = None
) -> AnswerKeyEntry:
    return AnswerKeyEntry(
        bank_line_id=bank_line_id,
        matched_txn_ids=matched_txn_ids,
        settlement_batch_ids=[],
        defect_classes=defect_classes or (["CLEAN"] if matched_txn_ids else ["OUT_OF_SCOPE"]),
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
        # B1 and B2 resolved (100 + 200 paise), B3 and B4 not (300 + 400)
        credit_paise_by_bank_line={"B1": 100, "B2": 200, "B3": 300, "B4": 400},
    )
    m = compute_metrics(run, answer_key)

    assert m.resolved_count == 2
    assert m.auto_match_rate == pytest.approx(0.5)
    assert m.precision == pytest.approx(0.5)  # 1 correct of 2 resolved
    assert m.recall == pytest.approx(1 / 3)  # 1 correct of 3 that expected a match
    assert m.false_match_count == 1
    assert m.false_match_rate == pytest.approx(0.25)  # 1 of 4 total records
    assert m.missed_count == 1

    # value-weighted: half the lines resolved, but only 300 of 1000 paise. The two
    # rates are deliberately allowed to diverge -- that divergence is the signal.
    assert m.value_total_paise == 1000
    assert m.value_auto_reconciled_paise == 300
    assert m.value_escalated_paise == 700
    assert m.value_auto_match_rate == pytest.approx(0.3)


def test_compute_metrics_resolving_an_out_of_scope_line_counts_as_false_match():
    answer_key = {"B1": _answer("B1", [])}
    resolutions = [_resolution("B1", ["TXN1"])]  # should have been escalated, not matched
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=1, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={"B1": 100},
    )
    m = compute_metrics(run, answer_key)
    assert m.false_match_count == 1
    assert m.precision == 0.0


def test_analyst_hours_are_derived_from_exception_count_not_guessed():
    """The savings figure must fall out of measured counts times the documented
    per-item assumptions -- never a number typed in to look good."""
    from ledgerloop.eval import metrics as metrics_mod

    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=[_resolution("B1", ["TXN1"])],
        exceptions=[], total_records=60, llm_calls_made=0, providers_used=[],
        wall_seconds=1.0, config={}, credit_paise_by_bank_line={"B1": 100},
    )
    m = compute_metrics(run, {"B1": _answer("B1", ["TXN1"])})
    # 60 records * 4 min = 240 min = 4h manual; no exceptions = 0h review
    assert m.illustrative_analyst_hours_manual == pytest.approx(
        60 * metrics_mod.ASSUMED_MINUTES_PER_MANUAL_TRACE / 60
    )
    assert m.illustrative_analyst_hours_with_ledgerloop == pytest.approx(0.0)
    assert m.illustrative_analyst_hours_saved == pytest.approx(4.0)


def test_correct_disposition_credits_a_correct_refusal_without_softening_the_risk_metric():
    """Two of these three lines had no match to make; declining them is the right
    answer, and auto-match rate alone scores that as a failure. The strict rate must
    still be reported unchanged beside the disposition rate -- the point is that both
    are visible, not that the flattering one replaces the other."""
    answer_key = {
        "B1": _answer("B1", ["TXN1"]),
        "B2": _answer("B2", []),
        "B3": _answer("B3", []),
    }
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=[_resolution("B1", ["TXN1"])],
        exceptions=[], total_records=3, llm_calls_made=0, providers_used=[],
        wall_seconds=1.0, config={}, credit_paise_by_bank_line={"B1": 100, "B2": 200, "B3": 300},
    )
    m = compute_metrics(run, answer_key)

    assert m.no_match_expected_count == 2
    assert m.correctly_rejected_count == 2
    assert m.correct_disposition_count == 3
    assert m.correct_disposition_rate == pytest.approx(1.0)
    assert m.auto_match_rate == pytest.approx(1 / 3)  # strict rate, unchanged
    assert m.false_match_rate == pytest.approx(0.0)


def test_resolving_an_out_of_scope_line_is_a_false_match_never_a_correct_rejection():
    """The disposition rate must not be reachable by matching an out-of-scope line --
    that is the exact failure it would otherwise disguise."""
    answer_key = {"B1": _answer("B1", [])}
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=[_resolution("B1", ["TXN1"])],
        exceptions=[], total_records=1, llm_calls_made=0, providers_used=[],
        wall_seconds=1.0, config={}, credit_paise_by_bank_line={"B1": 100},
    )
    m = compute_metrics(run, answer_key)

    assert m.correctly_rejected_count == 0
    assert m.correct_disposition_rate == pytest.approx(0.0)
    assert m.false_match_count == 1
    assert m.per_defect["OUT_OF_SCOPE"].false_matched == 1
    assert m.per_defect["OUT_OF_SCOPE"].correctly_rejected == 0


def test_per_defect_scores_each_class_separately_and_overlapping_rows_double_count():
    """A line carrying two defect classes is tallied under both, so the rows sum to
    more than the batch size. That overlap is intended -- these are per-class rates,
    not a partition -- and asserting it here keeps anyone from "fixing" it later."""
    answer_key = {
        "B1": _answer("B1", ["TXN1"], ["CLEAN"]),
        "B2": _answer("B2", ["TXN2"], ["SPLIT_1N", "TRANSPOSE"]),  # missed below
        "B3": _answer("B3", ["TXN3"], ["SPLIT_1N"]),
        "B4": _answer("B4", [], ["OUT_OF_SCOPE"]),
    }
    resolutions = [
        _resolution("B1", ["TXN1"], resolved_by="tier1"),
        _resolution("B3", ["TXN3"], resolved_by="tier2"),
        # B2 left unresolved -- a miss against two classes at once
        # B4 left unresolved -- a correct refusal
    ]
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=4, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={"B1": 100, "B2": 200, "B3": 300, "B4": 400},
    )
    m = compute_metrics(run, answer_key)

    split = m.per_defect["SPLIT_1N"]
    assert (split.total, split.correctly_matched, split.missed) == (2, 1, 1)
    assert split.correct_disposition_rate == pytest.approx(0.5)
    assert split.tier_counts == {"tier1": 0, "tier2": 1, "tier3": 0}

    transpose = m.per_defect["TRANSPOSE"]
    assert (transpose.total, transpose.missed) == (1, 1)
    assert transpose.correct_disposition_rate == pytest.approx(0.0)

    oos = m.per_defect["OUT_OF_SCOPE"]
    assert (oos.total, oos.correctly_rejected, oos.expects_match) == (1, 1, 0)
    assert oos.correct_disposition_rate == pytest.approx(1.0)

    # B2 carries two classes, so the rows over-count the 4 real records by exactly one.
    assert sum(d.total for d in m.per_defect.values()) == 5


def test_per_defect_tier_attribution_ignores_wrong_matches():
    """Crediting a tier for a line it got wrong would make it look more capable on a
    defect class the more of it it mishandled."""
    answer_key = {"B1": _answer("B1", ["TXN1"], ["FEE_DRIFT"])}
    run = HarnessRun(
        profile="dev", tier_ceiling="full",
        resolutions=[_resolution("B1", ["TXN_WRONG"], resolved_by="tier2")],
        exceptions=[], total_records=1, llm_calls_made=0, providers_used=[],
        wall_seconds=1.0, config={}, credit_paise_by_bank_line={"B1": 100},
    )
    m = compute_metrics(run, answer_key)
    assert m.per_defect["FEE_DRIFT"].tier_counts == {"tier1": 0, "tier2": 0, "tier3": 0}
    assert m.per_defect["FEE_DRIFT"].false_matched == 1


def test_every_generated_defect_class_gets_a_per_defect_row(generated_dev_data_root):
    """The generator guarantees every defect class appears in the dev set, so a run
    that reports fewer rows than DefectClass has means a class silently stopped being
    scored -- which is precisely what a single aggregate auto-match rate hides."""
    run = harness.run(generated_dev_data_root, "dev", "full", no_llm=True)
    answer_key = harness.load_answer_key(generated_dev_data_root, "dev")
    m = compute_metrics(run, answer_key)

    assert set(m.per_defect) == {c.value for c in ALL_DEFECT_CLASSES}
    for name, stats in m.per_defect.items():
        assert stats.total > 0, name
        # Deterministic tiers alone: no class may be actively wrong, only unresolved.
        assert stats.false_matched == 0, name


def test_review_queue_split_is_derived_without_ground_truth(generated_dev_data_root):
    """exceptions_needing_review + exceptions_no_action must account for every
    exception -- an item may be reclassified as not-urgent, never dropped."""
    run = harness.run(generated_dev_data_root, "dev", "full", no_llm=True)
    m = compute_metrics(run, harness.load_answer_key(generated_dev_data_root, "dev"))

    assert m.exceptions_needing_review + m.exceptions_no_action == len(run.exceptions)
    assert m.exceptions_no_action == m.exception_counts_by_reason.get("OUT_OF_SCOPE", 0)


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


# -- threshold sensitivity (eval/sensitivity.py) ---------------------------------------


def test_sweep_isolates_the_knob_and_leaves_the_shipped_config_untouched(generated_dev_data_root, config):
    """Every row must differ from the next only in the swept value -- and the sweep must
    not mutate the caller's config, or row N+1 would silently inherit row N's setting."""
    knob = next(k for k in sensitivity.KNOBS if k.key == "min_resolve_score")
    before = copy.deepcopy(config)
    rows = sensitivity.run_sweep(
        generated_dev_data_root, "dev", replace(knob, points=(0.60, 0.90)), config=config
    )

    assert config == before  # caller's config untouched
    assert [r.value for r in rows] == [0.60, 0.90]
    assert all(r.knob == "tier2.min_resolve_score" for r in rows)
    # Tightening the score threshold can only remove resolutions, never add any.
    assert rows[0].resolved_count >= rows[1].resolved_count


def test_sweep_marks_the_shipped_operating_point(generated_dev_data_root, config):
    knob = next(k for k in sensitivity.KNOBS if k.key == "min_resolve_score")
    shipped = config["tier2"]["min_resolve_score"]
    rows = sensitivity.run_sweep(
        generated_dev_data_root, "dev", replace(knob, points=(0.60, shipped)), config=config
    )
    assert [r.is_operating_point for r in rows] == [False, True]


def test_amount_tolerance_is_the_knob_that_actually_binds(generated_dev_data_root, config):
    """The claim the README makes from this sweep, asserted: an absurd amount tolerance
    posts wrong matches, and the shipped value does not. If a future change makes the
    pipeline tolerant of INR 5,000 of drift without false matches, this test should fail
    and the README's safety-margin figure should be rewritten -- not the test deleted."""
    knob = next(k for k in sensitivity.KNOBS if k.key == "amount_tolerance_paise")
    shipped = config["tier2"]["amount_tolerance_paise"]
    rows = sensitivity.run_sweep(
        generated_dev_data_root, "dev", replace(knob, points=(shipped, 500_000)), config=config
    )
    at_shipped, at_absurd = rows
    assert at_shipped.false_match_count == 0
    assert at_absurd.false_match_count > 0
    assert sensitivity.first_unsafe_row(rows, knob).value == 500_000


def test_first_unsafe_row_scans_from_the_permissive_end_whichever_way_the_knob_runs():
    """A knob that loosens downward and one that loosens upward must both report the
    *most permissive* breaking value, not merely the numerically smallest."""
    def row(value, false_matches):
        return sensitivity.SensitivityRow(
            knob="tier2.k", value=value, is_operating_point=False, resolved_count=0,
            auto_match_rate=0.0, correct_disposition_rate=0.0, precision=0.0, recall=0.0,
            false_match_rate=0.0, false_match_count=false_matches, exceptions_needing_review=0,
        )

    rows = [row(1, 2), row(5, 1), row(9, 0)]
    looser_lower = replace(sensitivity.KNOBS[0], points=(1, 5, 9), looser_is="lower")
    looser_higher = replace(sensitivity.KNOBS[0], points=(1, 5, 9), looser_is="higher")

    assert sensitivity.first_unsafe_row(rows, looser_lower).value == 1
    assert sensitivity.first_unsafe_row(rows, looser_higher).value == 5  # 9 is clean
    assert sensitivity.first_unsafe_row([row(9, 0)], looser_higher) is None


def test_report_names_the_binding_knob_and_calls_the_inert_ones_inert(generated_dev_data_root, config):
    """An inert knob must never be presented as a safety feature -- that mislabelling is
    exactly what running this sweep corrected."""
    knobs = (
        replace(next(k for k in sensitivity.KNOBS if k.key == "min_resolve_score"), points=(0.0, 0.70)),
        replace(next(k for k in sensitivity.KNOBS if k.key == "amount_tolerance_paise"), points=(200, 500_000)),
    )
    results = sensitivity.run_all(generated_dev_data_root, "dev", knobs=knobs, config=config)
    report = sensitivity.format_report(results, knobs=knobs)

    assert "binding (a swept value produced a wrong match): tier2.amount_tolerance_paise" in report
    assert "inert across the whole swept range: tier2.min_resolve_score" in report


# -- volume benchmark (eval/scale.py) --------------------------------------------------


def test_scale_holds_defect_density_constant_as_volume_rises(config):
    """A benchmark whose hard cases thin out as it grows measures nothing. Density at
    every size must match the configured scale profile's own ratio."""
    gen = config["generate"]
    configured_density = gen["scale_min_instances_per_defect"] / gen["scale_n_gateway_transactions"]

    at_scale = scale._config_at_size(config, gen["scale_n_gateway_transactions"])["generate"]
    assert at_scale["min_instances_per_defect"] == gen["scale_min_instances_per_defect"]

    at_double = scale._config_at_size(config, gen["scale_n_gateway_transactions"] * 2)["generate"]
    assert at_double["min_instances_per_defect"] / at_double["n_gateway_transactions"] == pytest.approx(
        configured_density
    )

    # Never below the floor every profile guarantees, however small the size.
    at_tiny = scale._config_at_size(config, 100)["generate"]
    assert at_tiny["min_instances_per_defect"] == gen["min_instances_per_defect"]
    assert scale._config_at_size(config, 100) is not config  # caller's config untouched


def test_scale_point_runs_the_shipped_thresholds_and_reports_timeouts(tmp_path, config):
    """The benchmark must run the config we ship, not the sized one it generated with --
    otherwise it would measure a configuration nobody uses."""
    point = scale.run_point(config["generate"]["n_gateway_transactions"], tmp_path, config=config)

    assert point.n_bank_lines > 0
    assert point.pipeline_seconds > 0
    assert point.false_match_count == 0
    assert point.tier2_timeouts >= 0  # the field exists and is counted, not inferred
    assert 0.0 < point.auto_match_rate <= 1.0
    assert point.correct_disposition_rate >= point.auto_match_rate


def test_scale_report_surfaces_search_timeouts_as_an_action(config):
    """A timeout under load is the signal to raise the node budget. It must never be
    reported as a bare number a reader has to interpret."""
    def point(timeouts):
        return scale.ScalePoint(
            n_transactions=5000, n_bank_lines=280, generate_seconds=1.0, pipeline_seconds=1.0,
            bank_lines_per_sec=280.0, transactions_per_sec=5000.0, auto_match_rate=0.9,
            correct_disposition_rate=0.95, precision=1.0, false_match_count=0,
            tier2_timeouts=timeouts, exceptions_needing_review=4,
        )

    assert "raise tier2.subset_sum_node_budget" in scale.format_report([point(3)])
    assert "the node budget held at every size tested" in scale.format_report([point(0)])


def test_ablation_rows_are_monotonically_non_decreasing(generated_dev_data_root):
    """Each tier can only add resolutions on top of the previous tier's, never take
    any away -- so resolved counts (and therefore auto-match rate) must never drop
    as the tier ceiling rises. With --no-llm, tiers1+2 and full are identical."""
    results = ablation.run_ablation(generated_dev_data_root, "dev", no_llm=True)
    assert results["tier1"].resolved_count <= results["tier1+2"].resolved_count
    assert results["tier1+2"].resolved_count <= results["full"].resolved_count
    assert results["tier1+2"].resolved_count == results["full"].resolved_count  # --no-llm on both


# -- the residual: the denominator tier3 is actually accountable for -------------------


def test_residual_counts_only_what_the_deterministic_tiers_could_not_resolve():
    """Six lines. Tiers 1-2 settle three. The other three are the residual -- the only
    lines tier3 ever sees -- and the LLM resolves one, misses one, and correctly
    declines one that had no match to make."""
    answer_key = {
        "B1": _answer("B1", ["TXN1"]),
        "B2": _answer("B2", ["TXN2"]),
        "B3": _answer("B3", ["TXN3"]),
        "B4": _answer("B4", ["TXN4"]),  # residual, resolved by tier3
        "B5": _answer("B5", ["TXN5"]),  # residual, missed
        "B6": _answer("B6", []),  # residual, no match to make
    }
    resolutions = [
        _resolution("B1", ["TXN1"], resolved_by="tier1"),
        _resolution("B2", ["TXN2"], resolved_by="tier1"),
        _resolution("B3", ["TXN3"], resolved_by="tier2"),
        _resolution("B4", ["TXN4"], resolved_by="tier3"),
    ]
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=6, llm_calls_made=1, providers_used=["fake"], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={f"B{i}": 100 for i in range(1, 7)},
    )
    m = compute_metrics(run, answer_key)

    # tier3's share of *everything* is small -- and that is the number that misleads.
    assert m.tier_shares["tier3"] == pytest.approx(1 / 6)

    assert m.residual_count == 3  # B4, B5, B6 -- not the three tiers 1-2 settled
    assert m.residual_matchable_count == 2  # B4 and B5; B6 had no match to make
    assert m.residual_resolved_by_llm == 1
    assert m.residual_missed == 1
    assert m.residual_correctly_rejected == 1
    assert m.residual_false_matched == 0
    assert m.residual_resolution_rate == pytest.approx(0.5)  # 1 of 2 matchable
    assert m.residual_correct_disposition_rate == pytest.approx(2 / 3)  # resolved + refused


def test_residual_parts_always_sum_to_the_residual():
    """The four residual outcomes partition the residual exactly -- no line is double
    counted and none falls through, whatever the mix."""
    answer_key = {
        "B1": _answer("B1", ["TXN1"]),
        "B2": _answer("B2", ["TXN2"]),
        "B3": _answer("B3", ["TXN3"]),
        "B4": _answer("B4", []),
        "B5": _answer("B5", []),
    }
    resolutions = [
        _resolution("B1", ["TXN1"], resolved_by="tier1"),
        _resolution("B2", ["TXN_WRONG"], resolved_by="tier3"),  # tier3 matched wrongly
        _resolution("B4", ["TXN9"], resolved_by="tier3"),  # tier3 matched an out-of-scope line
    ]
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=5, llm_calls_made=2, providers_used=["fake"], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={f"B{i}": 100 for i in range(1, 6)},
    )
    m = compute_metrics(run, answer_key)

    parts = (
        m.residual_resolved_by_llm
        + m.residual_false_matched
        + m.residual_missed
        + m.residual_correctly_rejected
    )
    assert parts == m.residual_count
    assert m.residual_false_matched == 2  # B2 wrong txns, B4 should never have matched
    assert m.residual_resolved_by_llm == 0
    assert m.residual_resolution_rate == pytest.approx(0.0)


def test_residual_rate_is_zero_when_the_llm_is_off():
    """--no-llm must not flatter the residual rate: nothing is resolved, so the
    numerator is zero rather than the metric being undefined or skipped."""
    answer_key = {"B1": _answer("B1", ["TXN1"]), "B2": _answer("B2", ["TXN2"])}
    resolutions = [_resolution("B1", ["TXN1"], resolved_by="tier1")]
    run = HarnessRun(
        profile="dev", tier_ceiling="tier2", resolutions=resolutions, exceptions=[],
        total_records=2, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={"B1": 100, "B2": 100},
    )
    m = compute_metrics(run, answer_key)
    assert m.residual_count == 1
    assert m.residual_matchable_count == 1
    assert m.residual_resolved_by_llm == 0
    assert m.residual_resolution_rate == pytest.approx(0.0)


def test_residual_rate_is_zero_not_a_crash_when_nothing_is_matchable():
    """A residual made entirely of lines with no match to make divides by zero unless
    guarded -- and its correct disposition is 100%, not 0%."""
    answer_key = {"B1": _answer("B1", ["TXN1"]), "B2": _answer("B2", [])}
    resolutions = [_resolution("B1", ["TXN1"], resolved_by="tier1")]
    run = HarnessRun(
        profile="dev", tier_ceiling="full", resolutions=resolutions, exceptions=[],
        total_records=2, llm_calls_made=0, providers_used=[], wall_seconds=1.0, config={},
        credit_paise_by_bank_line={"B1": 100, "B2": 100},
    )
    m = compute_metrics(run, answer_key)
    assert m.residual_matchable_count == 0
    assert m.residual_resolution_rate == pytest.approx(0.0)
    assert m.residual_correct_disposition_rate == pytest.approx(1.0)
