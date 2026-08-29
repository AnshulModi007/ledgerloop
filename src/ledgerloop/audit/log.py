"""Append-only audit log. Every decision records input hashes (via config_hash --
the config is what parameterises every decision, alongside the immutable input data
itself), resolving tier, rule/prompt version, confidence, timestamp, and (for tier3)
the full model response. Runs must be replayable from the log alone. See
IMPLEMENTATION.md section 4.

Unlike everything else in the pipeline, entries here carry real wall-clock
timestamps -- that's the point of an audit trail. This is explicitly excluded from
the determinism guarantee (section 2: "excluding timestamps") and never diffed byte
for byte in tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ledgerloop.config import config_hash
from ledgerloop.exceptions.decisions import ReviewDecision
from ledgerloop.schemas import Exception_, Resolution


class AuditRecord(BaseModel):
    bank_line_id: str
    # "review_decision" is a human's decision about an escalation, mirrored here from
    # exceptions/decisions.py so one file answers "what happened to this bank line",
    # machine and human alike, rather than the trail stopping at the escalation.
    outcome: Literal["resolved", "exception", "review_decision"]
    resolved_by: str | None  # tier1/tier2/tier3; None for exceptions
    reason_code: str | None  # only for exceptions
    rule: str | None
    prompt_version: str | None
    confidence: float | None
    evidence: dict
    model_response: dict | None  # the full tier3 evidence dict, when tier3 was involved
    config_hash: str
    timestamp_utc: str
    # -- review_decision only; None for machine outcomes.
    review_action: str | None = None
    review_actor: str | None = None
    review_note: str | None = None


def _record_for_resolution(resolution: Resolution, run_config_hash: str) -> AuditRecord:
    evidence = resolution.evidence
    return AuditRecord(
        bank_line_id=resolution.bank_line_id,
        outcome="resolved",
        resolved_by=resolution.resolved_by,
        reason_code=None,
        rule=evidence.get("rule"),
        prompt_version=evidence.get("prompt_version"),
        confidence=resolution.confidence,
        evidence=evidence,
        model_response=evidence if resolution.resolved_by == "tier3" else None,
        config_hash=run_config_hash,
        timestamp_utc=datetime.now(UTC).isoformat(),
    )


def _record_for_exception(exception: Exception_, run_config_hash: str) -> AuditRecord:
    evidence = exception.evidence
    return AuditRecord(
        bank_line_id=exception.bank_line_id,
        outcome="exception",
        resolved_by=None,
        reason_code=exception.reason_code,
        rule=None,
        prompt_version=None,
        confidence=None,
        evidence=evidence,
        model_response={"tier3_reasoning": evidence["tier3_reasoning"]} if "tier3_reasoning" in evidence else None,
        config_hash=run_config_hash,
        timestamp_utc=datetime.now(UTC).isoformat(),
    )


class AuditLog:
    """Append-only JSON-Lines file. Every append is one AuditRecord; nothing is ever
    rewritten or deleted -- that's what makes a run replayable from the log alone."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def append_run(self, *, resolutions: list[Resolution], exceptions: list[Exception_], config: dict) -> None:
        run_config_hash = config_hash(config)
        for resolution in sorted(resolutions, key=lambda r: r.bank_line_id):
            self.append(_record_for_resolution(resolution, run_config_hash))
        for exception in sorted(exceptions, key=lambda e: e.bank_line_id):
            self.append(_record_for_exception(exception, run_config_hash))

    def append_decision(self, decision: ReviewDecision, *, config: dict) -> None:
        """Mirrors a human review decision into the audit trail. The decision log itself
        (exceptions/decisions.py) remains the queue's state; this is the history."""
        self.append(
            AuditRecord(
                bank_line_id=decision.bank_line_id,
                outcome="review_decision",
                resolved_by=None,
                reason_code=decision.reason_code,
                rule=None,
                prompt_version=None,
                confidence=None,
                evidence={},
                model_response=None,
                config_hash=config_hash(config),
                timestamp_utc=decision.decided_at_utc,
                review_action=decision.action,
                review_actor=decision.actor,
                review_note=decision.note,
            )
        )

    def read_all(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    records.append(AuditRecord.model_validate_json(stripped))
        return records
