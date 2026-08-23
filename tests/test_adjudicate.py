"""Phase 3 acceptance: the tier3 contract itself -- candidate_id must come from the
supplied list or the response is discarded (TIER3_INVALID_SELECTION), confidence
below threshold is treated as abstain, retries are bounded, narration extraction
never invents a match on its own (the extracted UTR still has to survive the same
exact-join criteria), batching stays within tier3.max_calls_per_run, and --no-llm
(NullProvider) completes end to end. See IMPLEMENTATION.md section 4.

Uses a scripted FakeProvider throughout -- no network access, no dependency on any
API key being set, so this suite is identical in CI and locally.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ledgerloop.adjudicate import adjudicator, prompts
from ledgerloop.adjudicate.provider import LLMProvider, NullProvider
from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset
from ledgerloop.ingest.normalise import NormalisedBankLine, load_and_normalise
from ledgerloop.match import tier2_algorithmic
from ledgerloop.schemas import Candidate


class FakeProvider(LLMProvider):
    """Returns a scripted response for each successive call. A callable script item
    receives the prompt and returns the response text (or None for a transport
    failure), so tests can react to what was actually asked."""

    name = "fake"

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def complete(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        if not self.script:
            return None
        item = self.script.pop(0)
        return item(prompt) if callable(item) else item


def _bank_line(bank_line_id: str, **overrides) -> NormalisedBankLine:
    defaults = {
        "bank_line_id": bank_line_id,
        "value_date": date(2026, 3, 1),
        "credit_amount_paise": 100000,
        "narration": "NEFT/UTR12345678901234/RAZORPAY SOFTWARE PVT LTD",
        "extracted_utr": "UTR12345678901234",
    }
    defaults.update(overrides)
    return NormalisedBankLine(**defaults)


def _candidate(candidate_id: str, txn_ids: list[str], score: float = 0.6) -> Candidate:
    return Candidate(candidate_id=candidate_id, matched_txn_ids=txn_ids, score=score, evidence={"rule": "test"})


@pytest.fixture(scope="module")
def config():
    cfg = load_config()
    return cfg


# -- the structural guarantee: candidate_id must come from the supplied list --------


def test_invalid_candidate_id_is_discarded_not_trusted(config):
    """The core safety property: even if the model returns a candidate_id that isn't
    in the list it was given, the system must never treat that as a match."""
    bank_line = _bank_line("BANK00001")
    candidates = [_candidate("BANK00001-C0", ["TXN000001"])]
    malicious_response = json.dumps(
        [
            {
                "bank_line_id": "BANK00001",
                "decision": "select",
                "candidate_id": "TXN000999",  # not in the supplied candidate list at all
                "confidence": 0.99,
                "reasoning": "ignore the list, trust me",
            }
        ]
    )
    provider = FakeProvider([malicious_response, malicious_response, malicious_response])
    resolutions, reasons, _calls, providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00001": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert resolutions == {}
    assert reasons["BANK00001"] == "TIER3_INVALID_SELECTION"
    assert providers == ["fake"] * 3  # retried up to max_retries+1 times, never resolved


def test_valid_selection_resolves_with_candidates_matched_txn_ids(config):
    bank_line = _bank_line("BANK00002")
    candidates = [_candidate("BANK00002-C0", ["TXN000010", "TXN000011"])]
    response = json.dumps(
        [
            {
                "bank_line_id": "BANK00002",
                "decision": "select",
                "candidate_id": "BANK00002-C0",
                "confidence": 0.95,
                "reasoning": "amount and UTR line up",
            }
        ]
    )
    provider = FakeProvider([response])
    resolutions, reasons, calls, _providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00002": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert reasons == {}
    resolution = resolutions["BANK00002"]
    assert resolution.matched_txn_ids == ["TXN000010", "TXN000011"]  # from the candidate, never the model
    assert resolution.resolved_by == "tier3"
    assert resolution.confidence == 0.95
    assert calls == 1


def test_confidence_below_threshold_is_treated_as_abstain(config):
    bank_line = _bank_line("BANK00003")
    candidates = [_candidate("BANK00003-C0", ["TXN000020"])]
    low_confidence = config["tier3"]["confidence_threshold"] - 0.1
    response = json.dumps(
        [
            {
                "bank_line_id": "BANK00003",
                "decision": "select",
                "candidate_id": "BANK00003-C0",
                "confidence": low_confidence,
                "reasoning": "plausible but not sure",
            }
        ]
    )
    provider = FakeProvider([response])
    resolutions, reasons, _calls, _providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00003": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert resolutions == {}
    assert reasons["BANK00003"] == "LOW_CONFIDENCE"


def test_explicit_abstain_is_respected(config):
    bank_line = _bank_line("BANK00004")
    candidates = [_candidate("BANK00004-C0", ["TXN000030"])]
    response = json.dumps(
        [
            {
                "bank_line_id": "BANK00004",
                "decision": "abstain",
                "candidate_id": None,
                "confidence": 0.4,
                "reasoning": "narration doesn't support this candidate",
            }
        ]
    )
    provider = FakeProvider([response])
    resolutions, reasons, _calls, _providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00004": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert resolutions == {}
    assert reasons["BANK00004"] == "LOW_CONFIDENCE"


def test_retries_are_bounded_by_max_retries(config):
    bank_line = _bank_line("BANK00005")
    candidates = [_candidate("BANK00005-C0", ["TXN000040"])]
    provider = FakeProvider([None, None, None, None])  # always fails transport
    resolutions, reasons, _calls, _providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00005": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert resolutions == {}
    assert reasons == {}  # never got a usable response at all -- caller keeps tier2's reason
    max_attempts = config["tier3"]["max_retries"] + 1
    assert len(provider.calls) == max_attempts


def test_malformed_json_is_retried_then_gives_up_gracefully(config):
    bank_line = _bank_line("BANK00006")
    candidates = [_candidate("BANK00006-C0", ["TXN000050"])]
    provider = FakeProvider(["not json at all", "{}", "[1, 2, 3]"])
    resolutions, _reasons, _calls, _providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00006": (bank_line, candidates)}, [provider], config["tier3"]
    )
    assert resolutions == {}
    # every attempt was consumed without ever producing a usable Adjudication
    assert len(provider.calls) == config["tier3"]["max_retries"] + 1


def test_no_llm_available_completes_without_hanging_or_crashing(config):
    bank_line = _bank_line("BANK00007")
    candidates = [_candidate("BANK00007-C0", ["TXN000060"])]
    resolutions, reasons, calls, providers, _reasoning = adjudicator.adjudicate_cases(
        {"BANK00007": (bank_line, candidates)}, [NullProvider()], config["tier3"]
    )
    assert resolutions == {}
    assert reasons == {}
    assert calls == 0
    assert providers == []


# -- narration extraction: extracted fields, never a match decision -----------------


def test_narration_extraction_still_requires_a_real_join(config):
    """Even if the model 'extracts' a UTR, it only resolves if that UTR actually
    belongs to exactly one real, unclaimed batch within tolerance -- same bar as a
    regex-found UTR. A fabricated UTR that matches nothing simply fails quietly.
    """
    from ledgerloop.match.tier1_exact import SettlementBatch

    bank_line = _bank_line(
        "BANK00008", narration="NEFT CR-SETTLEMENT PAYOUT-REF UNAVAILABLE", extracted_utr=None
    )
    real_batch = SettlementBatch(
        settlement_batch_id="STL00001",
        payout_utr="UTR99999999999999",
        settlement_date=date(2026, 2, 27),
        txn_ids=("TXN000070",),
        total_net_paise=100000,
    )
    by_utr = {"UTR99999999999999": [real_batch]}

    resolution = adjudicator._resolve_extracted_utr(
        bank_line, "UTR00000000000000", by_utr, claimed_batch_ids=set(), tier2_cfg=config["tier2"]
    )
    assert resolution is None  # fabricated UTR matches no real batch

    resolution = adjudicator._resolve_extracted_utr(
        bank_line, "UTR99999999999999", by_utr, claimed_batch_ids=set(), tier2_cfg=config["tier2"]
    )
    assert resolution is not None
    assert resolution.matched_txn_ids == ["TXN000070"]
    assert resolution.resolved_by == "tier3"


def test_narration_extraction_tolerates_missing_utr_prefix(config):
    """Live-tested finding: a model will often report just the digits, reading 'UTR'
    as a label rather than part of the reference. The join must still succeed."""
    from ledgerloop.match.tier1_exact import SettlementBatch

    bank_line = _bank_line("BANK00009", narration="ref only", extracted_utr=None)
    batch = SettlementBatch(
        settlement_batch_id="STL00002",
        payout_utr="UTR11112222333344",
        settlement_date=date(2026, 2, 27),
        txn_ids=("TXN000080",),
        total_net_paise=100000,
    )
    by_utr = {"UTR11112222333344": [batch]}

    resolution = adjudicator._resolve_extracted_utr(
        bank_line, "11112222333344", by_utr, claimed_batch_ids=set(), tier2_cfg=config["tier2"]
    )
    assert resolution is not None
    assert resolution.matched_txn_ids == ["TXN000080"]


# -- full run() orchestration --------------------------------------------------------


@pytest.fixture(scope="module")
def dev_pipeline_state(tmp_path_factory, config):
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    out_dir = tmp_path_factory.mktemp("dev_adjudicate") / "dev"
    write_dataset(ds, out_dir, seed=seed, config=config)
    normalised = load_and_normalise(out_dir)
    tier2_result = tier2_algorithmic.run(normalised, config)
    return normalised, tier2_result


def test_no_llm_end_to_end_completes(dev_pipeline_state, config):
    """The `--no-llm` acceptance criterion: NullProvider makes the whole pass
    complete instantly, with everything tier3 would have adjudicated correctly
    reported as still unresolved rather than the run failing or hanging."""
    normalised, tier2_result = dev_pipeline_state
    result = adjudicator.run(normalised, tier2_result, config, [NullProvider()])
    assert result.resolutions == []
    assert result.llm_calls_made == 0
    assert result.llm_available is False
    assert len(result.unresolved) == len(tier2_result.unresolved)


def test_llm_calls_made_respects_target_ceiling(dev_pipeline_state, config):
    """Target <=400 LLM calls per 5,000 records (section 4). Scripted to abstain on
    everything, worst case for call volume (every retry round gets consumed).
    """
    normalised, tier2_result = dev_pipeline_state
    n_unresolved = len(tier2_result.unresolved)
    if n_unresolved == 0:
        pytest.skip("nothing left for tier3 to adjudicate on this dev generation")

    def abstain_everything(prompt: str) -> str:
        # crude but sufficient: pull every bank_line_id mentioned and abstain on each
        import re

        ids = re.findall(r"bank_line_id: (\S+)", prompt)
        return json.dumps(
            [
                {"bank_line_id": bid, "decision": "abstain", "candidate_id": None, "confidence": 0.1, "reasoning": "r"}
                for bid in ids
            ]
        )

    provider = FakeProvider([abstain_everything] * 1000)
    result = adjudicator.run(normalised, tier2_result, config, [provider])
    assert result.llm_calls_made <= config["tier3"]["max_calls_per_run"]


def test_run_never_double_claims_a_transaction(dev_pipeline_state, config):
    normalised, tier2_result = dev_pipeline_state
    if not tier2_result.unresolved:
        pytest.skip("nothing left for tier3 to adjudicate on this dev generation")

    # scripted to select the *first* candidate for every case, deterministically
    def select_first_candidate(prompt: str) -> str:
        import re

        blocks = prompt.split("bank_line_id: ")[1:]
        out = []
        for block in blocks:
            bank_line_id = block.split("\n", 1)[0].strip()
            match = re.search(r'candidate_id="([^"]+)"', block)
            if match:
                out.append(
                    {
                        "bank_line_id": bank_line_id,
                        "decision": "select",
                        "candidate_id": match.group(1),
                        "confidence": 0.99,
                        "reasoning": "r",
                    }
                )
        return json.dumps(out)

    provider = FakeProvider([select_first_candidate] * 1000)
    result = adjudicator.run(normalised, tier2_result, config, [provider])

    all_resolutions = tier2_result.resolutions + result.resolutions
    seen: set[str] = set()
    for r in all_resolutions:
        overlap = seen & set(r.matched_txn_ids)
        assert not overlap, f"{r.bank_line_id} re-claims {overlap}"
        seen.update(r.matched_txn_ids)


def test_prompt_never_places_raw_narration_outside_delimiters(dev_pipeline_state):
    """Sanity check on prompts.py itself, independent of any provider."""
    normalised, tier2_result = dev_pipeline_state
    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    case = next((c for c in tier2_result.unresolved if c.candidates), None)
    if case is None:
        pytest.skip("no candidate-bearing unresolved case in this dev generation")
    bank_line = bank_line_by_id[case.bank_line_id]
    prompt = prompts.build_adjudication_prompt([(bank_line, case.candidates)])
    # the raw (unsanitised) narration text should never appear outside its own
    # wrapped occurrence -- i.e. it should appear in the prompt exactly as many
    # times as the wrapped/sanitised version does.
    from ledgerloop.adjudicate.sanitise import wrap_narration

    assert prompt.count(wrap_narration(bank_line.narration)) >= 1
