"""End-to-end determinism: two --no-llm runs over identical inputs and config produce
identical resolutions and unresolved cases. See IMPLEMENTATION.md section 2 (hard
constraints) -- generator-level and tier1+2-level determinism are also covered more
granularly in test_generate.py and test_tiers.py; this is the full-pipeline capstone.

Tier 3 with a real LLM provider is explicitly *not* covered here -- model output
isn't guaranteed deterministic run-to-run (observed directly in this build; see
FAILURES.md), which is exactly why NullProvider (--no-llm) is the mode this
guarantee applies to.
"""

from __future__ import annotations

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import NullProvider
from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.match import tier2_algorithmic


def _run_no_llm_pipeline(out_dir, *, seed: int, config: dict):
    ds = Generator(seed, config).generate()
    write_dataset(ds, out_dir, seed=seed, config=config)
    normalised = load_and_normalise(out_dir)
    tier2_result = tier2_algorithmic.run(normalised, config)
    tier3_result = adjudicator.run(normalised, tier2_result, config, [NullProvider()])
    return tier2_result, tier3_result


def _resolution_key(r):
    return (r.bank_line_id, tuple(sorted(r.matched_txn_ids)), r.resolved_by, r.confidence)


def _unresolved_key(u):
    return (u.bank_line_id, u.reason_hint, tuple(c.candidate_id for c in u.candidates))


def test_full_no_llm_pipeline_is_deterministic(tmp_path):
    config = load_config()
    seed = config["generate"]["dev_seed"]

    tier2_a, tier3_a = _run_no_llm_pipeline(tmp_path / "run_a", seed=seed, config=config)
    tier2_b, tier3_b = _run_no_llm_pipeline(tmp_path / "run_b", seed=seed, config=config)

    resolutions_a = sorted(map(_resolution_key, [*tier2_a.resolutions, *tier3_a.resolutions]))
    resolutions_b = sorted(map(_resolution_key, [*tier2_b.resolutions, *tier3_b.resolutions]))
    assert resolutions_a == resolutions_b

    unresolved_a = sorted(map(_unresolved_key, tier3_a.unresolved))
    unresolved_b = sorted(map(_unresolved_key, tier3_b.unresolved))
    assert unresolved_a == unresolved_b

    assert tier3_a.llm_calls_made == 0 == tier3_b.llm_calls_made
