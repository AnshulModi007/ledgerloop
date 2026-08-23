"""The exception queue: a filterable, actionable view over Exception_ records.

Status transitions (approve/reject/reassign) are exposed here as pure functions on
immutable QueueItems; Phase 6's Streamlit UI is a thin view on top of this, not a
separate implementation of queue logic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

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
