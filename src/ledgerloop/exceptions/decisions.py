"""Durable record of what a *human* decided about an escalation.

Everything else in this system is auditable. audit/log.py records every machine decision
with its tier, rule, confidence, prompt version, model response and config hash, and
ledger/idempotency.py makes approved postings replay-safe. The human half of the loop had
none of that: queue.apply_action returns an immutable QueueItem, the dashboard held it in
Streamlit session state, and a browser refresh erased every approve, reject and reassign
ever made. For a product whose entire pitch is that it escalates to a person, the part
where the person decides was the one part nobody could review afterwards.

Append-only, like the audit log it mirrors into. A reviewer changing their mind is a normal
event and gets a new record; the current status of an exception is simply its latest one.
Nothing is ever rewritten, so "who decided this, when, and what did they say" survives the
decision being reversed -- which is the question an auditor actually asks.

Re-recording a decision identical to the one already standing is a no-op and returns False.
That is the same guarantee ledger/idempotency.py gives postings, applied to review actions:
double-clicking Approve, or replaying a session, must not manufacture review history.

Scope: there is no authentication here and none is claimed -- auth and RBAC are stated
non-goals in the README. `actor` is self-reported, defaulting to the LEDGERLOOP_REVIEWER
environment variable. It records *who said they did it*, which is worth having and is not
the same thing as proof.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DecisionAction = Literal["approved", "rejected", "reassigned"]

UNKNOWN_ACTOR = "unknown"


class ReviewDecision(BaseModel):
    bank_line_id: str
    action: DecisionAction
    actor: str
    note: str | None
    reason_code: str  # the exception's code at decision time, so the log reads standalone
    decided_at_utc: str
    # What the verdict was ABOUT. The log recorded that someone rejected BANK00115 but
    # never what they rejected, which makes "rejected" unreadable a month later and
    # unusable as a signal -- a candidate_id like BANK00115-C0 is positional and can
    # denote a different grouping on a later run, so the transaction set is what
    # actually identifies the pairing. Both are optional: decisions written before this
    # existed stay valid and simply carry no pairing.
    candidate_id: str | None = None
    candidate_txn_ids: list[str] = Field(default_factory=list)


def default_actor() -> str:
    """Self-reported reviewer identity. Explicitly not authentication -- see module docstring."""
    return os.environ.get("LEDGERLOOP_REVIEWER", "").strip() or UNKNOWN_ACTOR


def _same_decision(a: ReviewDecision, b: ReviewDecision) -> bool:
    """Timestamps deliberately excluded: two identical clicks a minute apart are the same
    decision, and treating them as distinct is exactly the duplicate history this prevents.

    The pairing is excluded too, and that one is a judgement call: if the same reviewer
    reaches the same verdict on the same line but tier2 now proposes a different grouping,
    that is a fresh fact about a changed candidate, not a repeated click. Including it
    would make an unchanged UI re-record on every run whenever candidate ordering shifted,
    which is the duplicate history this exists to prevent."""
    return (a.bank_line_id, a.action, a.actor, a.note) == (b.bank_line_id, b.action, b.actor, b.note)


class DecisionLog:
    """Append-only JSON-Lines store of review decisions for one profile."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_all(self) -> list[ReviewDecision]:
        if not self.path.exists():
            return []
        decisions = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                decisions.append(ReviewDecision.model_validate_json(line))
        return decisions

    def current(self) -> dict[str, ReviewDecision]:
        """Latest decision per bank line -- the queue's restored state. Later records win,
        which is what makes reversing a decision work without deleting the earlier one."""
        latest: dict[str, ReviewDecision] = {}
        for decision in self.read_all():
            latest[decision.bank_line_id] = decision
        return latest

    def record(
        self,
        *,
        bank_line_id: str,
        action: DecisionAction,
        reason_code: str,
        actor: str | None = None,
        note: str | None = None,
        candidate_id: str | None = None,
        candidate_txn_ids: list[str] | None = None,
    ) -> tuple[ReviewDecision, bool]:
        """Appends a decision. Returns (decision, was_new); was_new is False when an
        identical decision already stands, in which case nothing is written."""
        decision = ReviewDecision(
            bank_line_id=bank_line_id,
            action=action,
            actor=actor or default_actor(),
            note=note,
            reason_code=reason_code,
            decided_at_utc=datetime.now(UTC).isoformat(),
            candidate_id=candidate_id,
            candidate_txn_ids=list(candidate_txn_ids or []),
        )
        standing = self.current().get(bank_line_id)
        if standing is not None and _same_decision(standing, decision):
            return standing, False

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(decision.model_dump_json() + "\n")
        return decision, True


def decision_log_path(runs_root: Path, profile: str) -> Path:
    return runs_root / f"{profile}_review_decisions.jsonl"


def counts_by_action(decisions: dict[str, ReviewDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions.values():
        counts[decision.action] = counts.get(decision.action, 0) + 1
    return dict(sorted(counts.items()))
