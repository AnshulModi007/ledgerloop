"""Pipeline-wide interface contracts. Keep these stable -- everything else may change.

See IMPLEMENTATION.md section 5.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Resolution(BaseModel):
    """A single reconciliation decision."""

    bank_line_id: str
    matched_txn_ids: list[str]
    resolved_by: Literal["tier1", "tier2", "tier3"]
    confidence: float
    evidence: dict
    audit_id: str


class Candidate(BaseModel):
    """One tier2-produced candidate grouping for a bank line, handed to tier3 as a menu item.

    tier3 selects a candidate_id from a fixed list like this one, or abstains -- it never
    invents matched_txn_ids of its own. See adjudicate/adjudicator.py.
    """

    candidate_id: str
    matched_txn_ids: list[str]
    score: float
    evidence: dict


class UnresolvedCase(BaseModel):
    """A bank line tier1+tier2 could not resolve outright, headed to tier3 or the exception queue."""

    bank_line_id: str
    reason_hint: str
    candidates: list[Candidate]
    evidence: dict


class Exception_(BaseModel):
    """An unresolved item, typed so it's never a silent drop. reason_code is one of
    exceptions.taxonomy.ReasonCode. explanation is None when running --no-llm (or
    when no LLM ever weighed in on this particular case, e.g. it had zero candidates
    to begin with). See exceptions/taxonomy.py."""

    bank_line_id: str
    reason_code: str
    candidates_considered: list[str]
    explanation: str | None
    evidence: dict
