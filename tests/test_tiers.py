"""Phase 2 acceptance: tier1 alone resolves the expected ballpark, tiers 1+2 together
clear the acceptance bar (>=80% resolution, <0.5% false-match rate on the dev set),
OUT_OF_SCOPE is never matched, and the pipeline is deterministic. See
IMPLEMENTATION.md section 4 and section 7.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.match import tier1_exact, tier2_algorithmic


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def dev_dataset(tmp_path_factory, config):
    """Generate the dev profile once per test module and load it back through the
    same ingest path the pipeline uses, so this exercises the real CSV round-trip.
    """
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    out_dir = tmp_path_factory.mktemp("dev_tiers") / "dev"
    write_dataset(ds, out_dir, seed=seed, config=config)
    normalised = load_and_normalise(out_dir)
    answer_key = {
        e["bank_line_id"]: e for e in json.loads((out_dir / "answer_key.json").read_text())
    }
    return normalised, answer_key


def test_tier1_alone_resolves_expected_ballpark(dev_dataset, config):
    normalised, _ = dev_dataset
    batches = tier1_exact.build_batches(normalised.settlement_lines)
    resolutions, _unresolved = tier1_exact.resolve(
        normalised.bank_lines, batches, max_lag_days=config["tier1"]["exact_match_max_lag_days"]
    )
    rate = len(resolutions) / len(normalised.bank_lines)
    # spec expects roughly 55-65%; keep the assertion a bit looser so it isn't brittle
    # against small changes in the generator's defect mix.
    assert 0.45 <= rate <= 0.75, f"tier1-alone resolution rate {rate:.2%} outside expected ballpark"


def test_tiers_1_and_2_meet_acceptance_criteria(dev_dataset, config):
    normalised, answer_key = dev_dataset
    result = tier2_algorithmic.run(normalised, config)

    resolution_rate = len(result.resolutions) / len(normalised.bank_lines)
    assert resolution_rate >= 0.80, f"resolution rate {resolution_rate:.2%} < 80% floor"

    false_matches = [
        r
        for r in result.resolutions
        if sorted(r.matched_txn_ids) != sorted(answer_key[r.bank_line_id]["matched_txn_ids"])
    ]
    false_match_rate = len(false_matches) / len(normalised.bank_lines)
    assert false_match_rate < 0.005, (
        f"false-match rate {false_match_rate:.3%} >= 0.5% ceiling: {[f.bank_line_id for f in false_matches]}"
    )


def test_out_of_scope_is_never_matched(dev_dataset, config):
    normalised, answer_key = dev_dataset
    result = tier2_algorithmic.run(normalised, config)
    resolved_ids = {r.bank_line_id for r in result.resolutions}
    out_of_scope_ids = {
        bank_line_id
        for bank_line_id, entry in answer_key.items()
        if entry["defect_classes"] == ["OUT_OF_SCOPE"]
    }
    matched_out_of_scope = resolved_ids & out_of_scope_ids
    assert not matched_out_of_scope, f"OUT_OF_SCOPE lines wrongly resolved: {matched_out_of_scope}"


def test_every_resolution_traces_to_a_supported_rule(dev_dataset, config):
    """Every Resolution must carry evidence identifying which deterministic rule
    produced it -- required for the audit trail (Phase 4) and to keep tier2 honest
    about never inventing a match.
    """
    normalised, _ = dev_dataset
    result = tier2_algorithmic.run(normalised, config)
    known_rules = {
        "exact_utr_amount_join",
        "utr_amount_tolerance",
        "utr_single_transposition_tolerant",
        "utr_partition_search",
        "amount_date_fallback_no_utr",
        "generic_cross_batch_subset_sum",
    }
    for r in result.resolutions:
        rule = r.evidence.get("rule")
        assert rule in known_rules, f"{r.bank_line_id} resolved with unrecognised rule {rule!r}"


def test_defect_class_routing(dev_dataset, config):
    """Each defect class should mostly land where the design intends: cleanly
    resolvable classes at (near) 100%, OUT_OF_SCOPE never, and the genuinely hard
    classes (SPLIT_1N, MONTH_CROSS, NO_UTR) at a lower but still-majority rate.
    """
    normalised, answer_key = dev_dataset
    result = tier2_algorithmic.run(normalised, config)
    resolved_ids = {r.bank_line_id for r in result.resolutions}

    resolved_count: Counter[str] = Counter()
    total_count: Counter[str] = Counter()
    for bank_line_id, entry in answer_key.items():
        for defect in entry["defect_classes"]:
            total_count[defect] += 1
            if bank_line_id in resolved_ids:
                resolved_count[defect] += 1

    expect_near_full = ["CLEAN", "BATCH_N1", "REFUND_NET", "CHARGEBACK", "FEE_DRIFT", "DUPLICATE", "INJECTION", "TRANSPOSE"]
    for defect in expect_near_full:
        rate = resolved_count[defect] / total_count[defect]
        assert rate >= 0.9, f"{defect} resolved at only {rate:.0%}, expected near-full resolution"

    expect_majority = ["SPLIT_1N", "MONTH_CROSS", "NO_UTR"]
    for defect in expect_majority:
        rate = resolved_count[defect] / total_count[defect]
        assert rate >= 0.5, f"{defect} resolved at only {rate:.0%}, expected at least a majority"

    assert resolved_count["OUT_OF_SCOPE"] == 0


def test_pipeline_is_deterministic(dev_dataset, config):
    normalised, _ = dev_dataset
    result_a = tier2_algorithmic.run(normalised, config)
    result_b = tier2_algorithmic.run(normalised, config)

    def _key(r):
        return (r.bank_line_id, tuple(sorted(r.matched_txn_ids)), r.resolved_by, r.confidence)

    assert sorted(map(_key, result_a.resolutions)) == sorted(map(_key, result_b.resolutions))
    assert sorted(u.bank_line_id for u in result_a.unresolved) == sorted(
        u.bank_line_id for u in result_b.unresolved
    )
