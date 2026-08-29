"""The exception queue: a filterable, actionable view over Exception_ records.

Status transitions (approve/reject/reassign) are exposed here as pure functions on
immutable QueueItems; Phase 6's Streamlit UI is a thin view on top of this, not a
separate implementation of queue logic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ledgerloop.exceptions.decisions import ReviewDecision
from ledgerloop.exceptions.taxonomy import ReasonCode
from ledgerloop.schemas import Exception_

QueueStatus = Literal["open", "approved", "rejected", "reassigned"]


class QueueItem(BaseModel):
    exception: Exception_
    status: QueueStatus = "open"
    reviewer_note: str | None = None


def build_queue(exceptions: list[Exception_]) -> list[QueueItem]:
    return [QueueItem(exception=e) for e in sorted(exceptions, key=lambda e: e.bank_line_id)]


def filter_by_reason_code(items: list[QueueItem], reason_code: str) -> list[QueueItem]:
    return [item for item in items if item.exception.reason_code == reason_code]


def filter_by_status(items: list[QueueItem], status: QueueStatus) -> list[QueueItem]:
    return [item for item in items if item.status == status]


def counts_by_reason_code(items: list[QueueItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.exception.reason_code] = counts.get(item.exception.reason_code, 0) + 1
    return dict(sorted(counts.items()))


def apply_action(item: QueueItem, action: QueueStatus, *, note: str | None = None) -> QueueItem:
    """Returns a new QueueItem in the given status -- items are immutable, so a
    caller replaces its stored copy rather than mutating in place."""
    return item.model_copy(update={"status": action, "reviewer_note": note})


# Reason codes whose correct disposition is "no action", not "a human must decide
# something". An OUT_OF_SCOPE line is a bank credit the pipeline positively determined
# was never a gateway settlement -- a direct transfer, a refund reversal. Declining to
# match it IS the right answer, already taken. Listing it beside a genuine amount
# dispute overstates the review burden badly: on the held-out set 20 of 21 exceptions
# were this class. It stays IN the queue and stays visible -- nothing is silently
# dropped, which is the entire point of typing exceptions -- it is just separated from
# the work. See eval/metrics.py's disposition rate for the scored counterpart.
NO_ACTION_REASON_CODES: frozenset[str] = frozenset({ReasonCode.OUT_OF_SCOPE})


def requires_review(item: QueueItem) -> bool:
    return item.exception.reason_code not in NO_ACTION_REASON_CODES


def partition_by_review_need(items: list[QueueItem]) -> tuple[list[QueueItem], list[QueueItem]]:
    """Splits the queue into (needs_review, no_action). Order within each half is
    preserved, so a caller that sorted by bank_line_id stays sorted."""
    needs_review = [item for item in items if requires_review(item)]
    no_action = [item for item in items if not requires_review(item)]
    return needs_review, no_action


def apply_stored_decisions(items: list[QueueItem], decisions: dict[str, ReviewDecision]) -> list[QueueItem]:
    """Rehydrates a freshly-built queue with the decisions a human already made.

    Without this the queue is correct but amnesiac: a re-run rebuilds every exception as
    "open" and the reviewer's work is invisible even though it was durably recorded. An
    exception with no stored decision stays open, which is why a line that was never
    decided and a line whose decision was reversed to open look different here.
    """
    rehydrated = []
    for item in items:
        decision = decisions.get(item.exception.bank_line_id)
        if decision is None:
            rehydrated.append(item)
        else:
            rehydrated.append(apply_action(item, decision.action, note=decision.note))
    return rehydrated
