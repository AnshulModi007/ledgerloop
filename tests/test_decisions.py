"""Durable review decisions -- exceptions/decisions.py.

The property under test is that the human half of the loop is as auditable as the machine
half: a decision is attributed, timestamped, survives a restart, is idempotent against a
double-click, and keeps its own history when reversed. Before this existed the dashboard
held decisions in Streamlit session state and a refresh erased all of them.
"""

from __future__ import annotations

import pytest

from ledgerloop.exceptions import queue as queue_mod
from ledgerloop.exceptions.decisions import (
    UNKNOWN_ACTOR,
    DecisionLog,
    counts_by_action,
    decision_log_path,
    default_actor,
)
from ledgerloop.schemas import Exception_


@pytest.fixture
def log(tmp_path):
    return DecisionLog(decision_log_path(tmp_path, "dev"))


def _exception(bank_line_id: str = "BANK00001", reason_code: str = "LOW_CONFIDENCE") -> Exception_:
    return Exception_(
        bank_line_id=bank_line_id, reason_code=reason_code, candidates_considered=[], explanation=None, evidence={}
    )


def test_a_decision_is_attributed_and_survives_a_new_reader(log, tmp_path):
    exc = _exception()
    decision, was_new = log.record(
        bank_line_id=exc.bank_line_id, action="approved", reason_code=exc.reason_code,
        actor="anshul", note="checked against the bank portal",
    )
    assert was_new
    assert decision.actor == "anshul"
    assert decision.decided_at_utc  # timestamped, unlike everything else in this pipeline

    # A completely separate reader -- i.e. a restarted process -- sees it.
    reopened = DecisionLog(decision_log_path(tmp_path, "dev")).current()
    assert reopened[exc.bank_line_id].action == "approved"
    assert reopened[exc.bank_line_id].note == "checked against the bank portal"


def test_an_identical_decision_is_a_no_op(log):
    """Double-clicking Approve, or replaying a session, must not manufacture review
    history -- the same guarantee idempotency.py gives postings, applied to review."""
    exc = _exception()
    kwargs = {"bank_line_id": exc.bank_line_id, "action": "approved", "reason_code": exc.reason_code, "actor": "a", "note": "n"}
    first, was_new_first = log.record(**kwargs)
    second, was_new_second = log.record(**kwargs)

    assert was_new_first and not was_new_second
    assert second.decided_at_utc == first.decided_at_utc  # the standing record, not a new one
    assert len(log.read_all()) == 1


def test_reversing_a_decision_keeps_the_earlier_one(log):
    """An auditor's question is "who decided this, when, and what did they say" -- and it
    stays answerable after the decision is reversed. Hence append-only."""
    exc = _exception()
    log.record(bank_line_id=exc.bank_line_id, action="approved", reason_code=exc.reason_code, actor="a", note="looks fine")
    log.record(bank_line_id=exc.bank_line_id, action="rejected", reason_code=exc.reason_code, actor="b", note="actually no")

    history = log.read_all()
    assert [d.action for d in history] == ["approved", "rejected"]
    assert [d.actor for d in history] == ["a", "b"]
    assert log.current()[exc.bank_line_id].action == "rejected"  # latest wins


def test_a_note_change_alone_is_a_new_decision(log):
    """Same verdict, different reasoning, is new information and must not be swallowed by
    the idempotency check."""
    exc = _exception()
    log.record(bank_line_id=exc.bank_line_id, action="approved", reason_code=exc.reason_code, actor="a", note="first pass")
    _, was_new = log.record(
        bank_line_id=exc.bank_line_id, action="approved", reason_code=exc.reason_code, actor="a", note="after checking"
    )
    assert was_new
    assert len(log.read_all()) == 2


def test_actor_defaults_are_self_reported_not_authenticated(monkeypatch):
    monkeypatch.delenv("LEDGERLOOP_REVIEWER", raising=False)
    assert default_actor() == UNKNOWN_ACTOR
    monkeypatch.setenv("LEDGERLOOP_REVIEWER", "  controller-1  ")
    assert default_actor() == "controller-1"
    monkeypatch.setenv("LEDGERLOOP_REVIEWER", "   ")
    assert default_actor() == UNKNOWN_ACTOR  # blank is not an identity


def test_counts_by_action_summarises_current_state(log):
    for i, action in enumerate(("approved", "approved", "rejected")):
        log.record(bank_line_id=f"BANK0000{i}", action=action, reason_code="LOW_CONFIDENCE", actor="a", note=None)
    assert counts_by_action(log.current()) == {"approved": 2, "rejected": 1}


def test_stored_decisions_rehydrate_the_queue(log):
    """A rebuilt queue is otherwise amnesiac: every exception comes back "open" even
    though the decision was durably recorded."""
    decided, undecided = _exception("BANK00001"), _exception("BANK00002")
    log.record(bank_line_id=decided.bank_line_id, action="rejected", reason_code="LOW_CONFIDENCE", actor="a", note="not ours")

    items = queue_mod.apply_stored_decisions(queue_mod.build_queue([decided, undecided]), log.current())
    by_id = {item.exception.bank_line_id: item for item in items}

    assert by_id["BANK00001"].status == "rejected"
    assert by_id["BANK00001"].reviewer_note == "not ours"
    assert by_id["BANK00002"].status == "open"  # never decided, and must not look decided
