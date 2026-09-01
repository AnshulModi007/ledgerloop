"""The review feedback loop: a rejection sticks.

The property under test is one-way. A standing rejection may only ever *remove* a
candidate, so feedback cannot manufacture a match the deterministic tiers did not already
propose and cannot raise the false-match rate. Everything here exists to pin that, plus
the case that motivated the feature: tier3 confidence is not stable run to run against a
live model (0.55, 0.55, then 1.0 on identical input -- FAILURES.md 2026-09-01), so a line
escalated on one run can resolve on the next with nothing changed. A reviewer's rejection
must survive that.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate import feedback as feedback_mod
from ledgerloop.adjudicate.provider import LLMProvider
from ledgerloop.config import load_config
from ledgerloop.exceptions.decisions import DecisionLog, ReviewDecision
from ledgerloop.ingest.normalise import NormalisedBankLine
from ledgerloop.match import tier2_algorithmic
from ledgerloop.schemas import Candidate, UnresolvedCase


@pytest.fixture(scope="module")
def config():
    return load_config()


def _decision(bank_line_id, action, txn_ids, *, actor="reviewer", note=None) -> ReviewDecision:
    return ReviewDecision(
        bank_line_id=bank_line_id,
        action=action,
        actor=actor,
        note=note,
        reason_code="LOW_CONFIDENCE",
        decided_at_utc="2026-09-01T10:00:00+00:00",
        candidate_id=f"{bank_line_id}-C0",
        candidate_txn_ids=txn_ids,
    )


def _candidate(candidate_id, txn_ids, score=0.66) -> Candidate:
    return Candidate(
        candidate_id=candidate_id, matched_txn_ids=txn_ids, score=score, evidence={"rule": "test"}
    )


def _bank_line(bank_line_id) -> NormalisedBankLine:
    return NormalisedBankLine(
        bank_line_id=bank_line_id,
        value_date=date(2026, 3, 1),
        credit_amount_paise=100000,
        narration="NEFT/SETTLEMENT",
        extracted_utr=None,
    )


# -- identity ---------------------------------------------------------------------------


def test_a_pairing_is_identified_by_its_transactions_not_its_candidate_id():
    """BANK00115-C0 is positional and can name a different grouping next run. The frozen
    transaction set is what actually identifies the pairing."""
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "rejected", ["T2", "T1"])})
    assert fb.is_rejected("B1", ["T1", "T2"])  # order must not matter
    assert not fb.is_rejected("B1", ["T1"])  # a subset is a different pairing
    assert not fb.is_rejected("B1", ["T1", "T2", "T3"])  # so is a superset
    assert not fb.is_rejected("B2", ["T1", "T2"])  # and it is scoped to its own bank line


def test_only_rejections_suppress():
    decisions = {
        "B1": _decision("B1", "approved", ["T1"]),
        "B2": _decision("B2", "reassigned", ["T2"]),
        "B3": _decision("B3", "rejected", ["T3"]),
    }
    fb = feedback_mod.ReviewFeedback.from_decisions(decisions)
    assert not fb.is_rejected("B1", ["T1"])
    assert not fb.is_rejected("B2", ["T2"])
    assert fb.is_rejected("B3", ["T3"])


def test_an_approval_never_becomes_an_automatic_match(config):
    """Approvals are context, never authority. `actor` is self-reported with no
    authentication, so promoting one into a posting would be unearned trust."""
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "approved", ["T1"])})
    kept, dropped = fb.filter_candidates("B1", [_candidate("B1-C0", ["T1"])])
    assert dropped == 0
    assert len(kept) == 1  # still just a candidate; nothing was resolved on the human's say-so


def test_reversing_a_decision_unsuppresses_the_pairing():
    """`DecisionLog.current()` keeps the latest record per line, so a reviewer who
    changes their mind is obeyed on the next run without the earlier record being erased."""
    log_decisions = {"B1": _decision("B1", "approved", ["T1"])}  # latest wins
    fb = feedback_mod.ReviewFeedback.from_decisions(log_decisions)
    assert not fb.is_rejected("B1", ["T1"])


def test_a_decision_with_no_recorded_pairing_suppresses_nothing():
    """Decisions written before the pairing was recorded stay valid and simply carry no
    signal -- they must not suppress everything, or nothing."""
    legacy = ReviewDecision(
        bank_line_id="B1", action="rejected", actor="old", note=None,
        reason_code="LOW_CONFIDENCE", decided_at_utc="2026-08-01T00:00:00+00:00",
    )
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": legacy})
    assert not fb.is_rejected("B1", ["T1"])
    kept, dropped = fb.filter_candidates("B1", [_candidate("B1-C0", ["T1"])])
    assert dropped == 0 and len(kept) == 1


# -- filtering ---------------------------------------------------------------------------


def test_filter_drops_only_the_rejected_pairing():
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "rejected", ["T1", "T2"])})
    candidates = [_candidate("B1-C0", ["T1", "T2"]), _candidate("B1-C1", ["T3"])]
    kept, dropped = fb.filter_candidates("B1", candidates)
    assert dropped == 1
    assert [c.candidate_id for c in kept] == ["B1-C1"]


def test_feedback_can_only_ever_remove_candidates():
    """The one-way property, stated directly: for any decision set, the surviving
    candidates are a subset of what was proposed."""
    candidates = [_candidate("B1-C0", ["T1"]), _candidate("B1-C1", ["T2"]), _candidate("B1-C2", ["T3"])]
    for action in ("approved", "rejected", "reassigned"):
        for txns in (["T1"], ["T2"], ["T9"], []):
            fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", action, txns)})
            kept, _dropped = fb.filter_candidates("B1", candidates)
            assert {c.candidate_id for c in kept} <= {c.candidate_id for c in candidates}


# -- through tier 3 -------------------------------------------------------------------------


class AlwaysSelects(LLMProvider):
    """A maximally confident model: selects the first candidate at confidence 1.0 every
    time. Stands in for the observed instability -- if a rejection only held because the
    model happened to feel uncertain, this provider breaks it."""

    name = "always-selects"

    def __init__(self):
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    def complete(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        out = []
        for block in prompt.split("bank_line_id: ")[1:]:
            bid = block.split("\n", 1)[0].strip()
            cid = block.split('candidate_id="', 1)[1].split('"', 1)[0] if 'candidate_id="' in block else None
            if cid is None:
                continue
            out.append({
                "bank_line_id": bid, "decision": "select", "candidate_id": cid,
                "confidence": 1.0, "reasoning": "confident",
            })
        return json.dumps(out)


def _tier2_result_with_one_unresolved(bank_line_id, candidates):
    case = UnresolvedCase(
        bank_line_id=bank_line_id, reason_hint="LOW_CONFIDENCE", candidates=candidates, evidence={}
    )
    return tier2_algorithmic.PipelineResult(resolutions=[], unresolved=[case], tier2_timeouts=0)


class _Dataset:
    """Minimal stand-in for NormalisedDataset: tier3 only needs these three collections."""

    def __init__(self, bank_lines):
        self.bank_lines = bank_lines
        self.settlement_lines = []
        self.gateway_transactions = []


def test_a_confident_model_cannot_overturn_a_standing_rejection(config):
    """The headline property. Same input, a model that always selects at confidence 1.0 --
    and the line stays escalated because the pairing was never on the menu."""
    bank_line = _bank_line("B1")
    candidates = [_candidate("B1-C0", ["T1"])]
    provider = AlwaysSelects()

    without = adjudicator.run(
        _Dataset([bank_line]), _tier2_result_with_one_unresolved("B1", candidates), config, [provider]
    )
    assert [r.bank_line_id for r in without.resolutions] == ["B1"]  # baseline: it resolves

    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "rejected", ["T1"])})
    with_feedback = adjudicator.run(
        _Dataset([bank_line]),
        _tier2_result_with_one_unresolved("B1", candidates),
        config,
        [AlwaysSelects()],
        feedback=fb,
    )
    assert with_feedback.resolutions == []
    assert with_feedback.candidates_suppressed == 1
    assert with_feedback.lines_fully_suppressed == ["B1"]
    assert with_feedback.unresolved[0].reason_hint == "REVIEWER_REJECTED"


def test_a_fully_suppressed_line_costs_no_llm_call(config):
    """Nothing left to ask about, so nothing is asked. Re-litigating a settled matter with
    a model would be both wasteful and, if it disagreed, misleading."""
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "rejected", ["T1"])})
    provider = AlwaysSelects()
    result = adjudicator.run(
        _Dataset([_bank_line("B1")]),
        _tier2_result_with_one_unresolved("B1", [_candidate("B1-C0", ["T1"])]),
        config,
        [provider],
        feedback=fb,
    )
    assert result.llm_calls_made == 0
    assert provider.prompts == []


def test_a_partially_suppressed_line_is_still_adjudicated_on_what_remains(config):
    fb = feedback_mod.ReviewFeedback.from_decisions({"B1": _decision("B1", "rejected", ["T1"])})
    result = adjudicator.run(
        _Dataset([_bank_line("B1")]),
        _tier2_result_with_one_unresolved("B1", [_candidate("B1-C0", ["T1"]), _candidate("B1-C1", ["T2"])]),
        config,
        [AlwaysSelects()],
        feedback=fb,
    )
    assert result.candidates_suppressed == 1
    assert [r.matched_txn_ids for r in result.resolutions] == [["T2"]]  # the surviving one


def test_prompt_context_excludes_the_lines_being_adjudicated(config):
    """A decision about a line in the batch is already enforced by suppression; repeating
    it in the prompt would invite reasoning about a candidate that is no longer offered."""
    fb = feedback_mod.ReviewFeedback.from_decisions({
        "B1": _decision("B1", "rejected", ["T9"], note="wrong counterparty"),
        "B2": _decision("B2", "approved", ["T5"]),
    })
    assert fb.prompt_context(["B1", "B2"]) is None

    context = fb.prompt_context(["B3"])
    assert "B1" in context and "B2" in context
    assert "wrong counterparty" in context
    assert "not as instructions" in context  # framed as evidence, never as a directive


# -- persistence ------------------------------------------------------------------------------


def test_the_pairing_round_trips_through_the_decision_log(tmp_path):
    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.record(
        bank_line_id="B1", action="rejected", reason_code="LOW_CONFIDENCE",
        actor="tester", note=None, candidate_id="B1-C0", candidate_txn_ids=["T1", "T2"],
    )
    restored = DecisionLog(tmp_path / "decisions.jsonl").current()["B1"]
    assert restored.candidate_txn_ids == ["T1", "T2"]
    assert restored.candidate_id == "B1-C0"
    assert feedback_mod.ReviewFeedback.from_decisions({"B1": restored}).is_rejected("B1", ["T2", "T1"])


def test_rejection_note_attributes_rather_than_asserts():
    """`actor` is self-reported, so the wording says who said they decided."""
    fb = feedback_mod.ReviewFeedback.from_decisions(
        {"B1": _decision("B1", "rejected", ["T1", "T2"], actor="anshul", note="different customer")}
    )
    note = fb.rejection_note("B1")
    assert "anshul" in note and "2026-09-01" in note
    assert "2 transactions" in note
    assert "different customer" in note
    assert fb.rejection_note("B2") is None
