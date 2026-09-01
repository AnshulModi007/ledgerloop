"""Single orchestration path for a full reconciliation run: tiers 1-3, exception
classification, journal proposal, audit logging, and the idempotent approval
bookkeeping. Both the CLI (reconcile.py) and the dashboard (ui/app.py) call this --
neither reimplements it -- so the two surfaces can never drift into reporting
different numbers for the same run. See IMPLEMENTATION.md section 4 (Phases 3-4)
and section 6 (Phase 6, "the UI is a thin view").
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate import feedback as feedback_mod
from ledgerloop.adjudicate.provider import LLMProvider, resolve_chain
from ledgerloop.audit.log import AuditLog
from ledgerloop.config import load_config
from ledgerloop.exceptions import taxonomy
from ledgerloop.exceptions.decisions import (
    DecisionAction,
    DecisionLog,
    ReviewDecision,
    decision_log_path,
)

__all__ = ["DecisionLog", "ReconcileRun", "ReviewDecision", "approve", "decide", "decision_log_path", "run"]
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.ledger import journal
from ledgerloop.ledger import tieout as tieout_mod
from ledgerloop.ledger.journal import JournalBatch, Posting
from ledgerloop.ledger.tieout import TieOut
from ledgerloop.match import tier1_exact, tier2_algorithmic
from ledgerloop.schemas import Exception_, Resolution


@dataclass
class ReconcileRun:
    profile: str
    resolutions: list[Resolution]
    exceptions: list[Exception_]
    journal_batches: list[JournalBatch]
    all_postings: list[Posting]
    new_postings: list[Posting]
    tier_counts: dict[str, int]
    reason_counts: dict[str, int]
    # Transactions whose receivable was cleared by more than one bank line -- a control
    # that per-batch balance cannot see. Empty on a clean run. See journal.py.
    duplicate_receivable_relief: dict[str, list[str]]
    # Human decisions already standing against this profile's exceptions, keyed by bank
    # line. Loaded on every run so a reviewer's work survives a restart -- see
    # exceptions/decisions.py.
    review_decisions: dict[str, ReviewDecision]
    # The whole-run reconciliation statement and its controls -- see ledger/tieout.py.
    # Per-batch balance cannot see a cross-batch problem; this can.
    tie_out: TieOut
    # Credit amount per bank line. Carried on the result because every surface needs to
    # show what a line was worth beside what happened to it -- an exception states a
    # reason, not a balance -- and re-reading the statement to recover it would let a
    # view report an amount the run never saw.
    credit_paise_by_bank_line: dict[str, int]
    llm_calls_made: int
    providers_used: list[str]
    llm_available: bool
    # What standing review decisions changed about this run: candidate pairings removed
    # because a reviewer rejected them, and the lines left with nothing to adjudicate.
    candidates_suppressed_by_review: int
    lines_suppressed_by_review: list[str]
    total_records: int
    config: dict


def _approved_store_path(runs_root: Path, profile: str) -> Path:
    return runs_root / f"{profile}_approved_postings.json"


def load_approved_keys(path: Path) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")))
    return set()


def save_approved_keys(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")


def run(
    data_root: Path,
    profile: str,
    runs_root: Path,
    *,
    no_llm: bool = False,
    config_path: Path | None = None,
    chain: list[LLMProvider] | None = None,
) -> ReconcileRun:
    config = load_config(config_path) if config_path else load_config()
    normalised = load_and_normalise(data_root / profile)

    tier2_result = tier2_algorithmic.run(normalised, config)
    active_chain = chain if chain is not None else resolve_chain(no_llm=no_llm)
    # Standing human decisions are loaded before tier3, not after: a pairing a reviewer
    # already rejected is removed from the candidate menu rather than re-proposed and
    # re-escalated. See adjudicate/feedback.py.
    review_decisions = DecisionLog(decision_log_path(runs_root, profile)).current()
    tier3_result = adjudicator.run(
        normalised,
        tier2_result,
        config,
        active_chain,
        feedback=feedback_mod.ReviewFeedback.from_decisions(review_decisions),
    )
    all_resolutions = tier2_result.resolutions + tier3_result.resolutions

    batches = tier1_exact.build_batches(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)
    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    claimed_batch_ids = {
        r.evidence["settlement_batch_id"] for r in all_resolutions if "settlement_batch_id" in r.evidence
    }
    claimed_txn_ids = {t for r in all_resolutions for t in r.matched_txn_ids}
    unclaimed_gross = [
        t.gross_amount_paise for t in normalised.gateway_transactions if t.txn_id not in claimed_txn_ids
    ]

    exceptions = taxonomy.classify_all(
        bank_line_by_id,
        tier3_result.unresolved,
        by_utr=by_utr,
        claimed_batch_ids=claimed_batch_ids,
        unclaimed_gross_amounts_paise=unclaimed_gross,
        tier2_cfg=config["tier2"],
    )

    settlement_lines_by_txn = {line.txn_id: line for line in normalised.settlement_lines}
    journal_batches = journal.propose_postings(all_resolutions, settlement_lines_by_txn, bank_line_by_id)
    all_postings = [p for batch in journal_batches for p in batch.postings]

    duplicate_relief = journal.find_duplicate_receivable_relief(all_postings)
    # review_decisions was loaded before tier3 (it feeds candidate suppression) and is
    # reused here rather than re-read, so the decisions that shaped this run are exactly
    # the ones reported with it.
    credit_paise_by_bank_line = {b.bank_line_id: b.credit_amount_paise for b in normalised.bank_lines}
    tie_out = tieout_mod.build(
        all_postings,
        credit_paise_by_bank_line,
        {r.bank_line_id for r in all_resolutions},
    )

    approved_keys = load_approved_keys(_approved_store_path(runs_root, profile))
    new_postings = [p for p in all_postings if p.idempotency_key not in approved_keys]

    AuditLog(runs_root / f"{profile}_audit.jsonl").append_run(
        resolutions=all_resolutions, exceptions=exceptions, config=config
    )

    return ReconcileRun(
        profile=profile,
        resolutions=all_resolutions,
        exceptions=exceptions,
        journal_batches=journal_batches,
        all_postings=all_postings,
        new_postings=new_postings,
        tier_counts=dict(Counter(r.resolved_by for r in all_resolutions)),
        reason_counts=dict(Counter(e.reason_code for e in exceptions)),
        duplicate_receivable_relief=duplicate_relief,
        review_decisions=review_decisions,
        tie_out=tie_out,
        credit_paise_by_bank_line=credit_paise_by_bank_line,
        llm_calls_made=tier3_result.llm_calls_made,
        providers_used=tier3_result.providers_used,
        llm_available=tier3_result.llm_available,
        candidates_suppressed_by_review=tier3_result.candidates_suppressed,
        lines_suppressed_by_review=tier3_result.lines_fully_suppressed,
        total_records=len(normalised.bank_lines),
        config=config,
    )


def decide(
    runs_root: Path,
    profile: str,
    *,
    exception: Exception_,
    action: DecisionAction,
    config: dict,
    actor: str | None = None,
    note: str | None = None,
) -> tuple[ReviewDecision, bool]:
    """Records a human decision about one escalation, durably and idempotently, and
    mirrors it into the audit trail. Returns (decision, was_new); was_new is False when
    the identical decision already stands.

    Both surfaces go through here for the same reason they both go through run(): so the
    dashboard and any future CLI can never diverge on what a decision means or where it
    is written."""
    log = DecisionLog(decision_log_path(runs_root, profile))
    # The pairing the reviewer was actually looking at. `candidate_detail` is sorted by
    # score, and the explanation a reviewer reads describes that same strongest candidate,
    # so this records the thing on screen rather than an arbitrary one. Absent for
    # exceptions with no candidates (OUT_OF_SCOPE and friends), where there is no pairing
    # to have an opinion about.
    best = next(iter(exception.evidence.get("candidate_detail", [])), None)
    decision, was_new = log.record(
        bank_line_id=exception.bank_line_id,
        action=action,
        reason_code=exception.reason_code,
        actor=actor,
        note=note,
        candidate_id=(best or {}).get("candidate_id"),
        candidate_txn_ids=(best or {}).get("matched_txn_ids", []),
    )
    if was_new:
        AuditLog(runs_root / f"{profile}_audit.jsonl").append_decision(decision, config=config)
    return decision, was_new


def approve(runs_root: Path, run_result: ReconcileRun) -> int:
    """Persists this run's postings as approved. Returns how many were new -- a
    second call with an identical run_result always returns 0, which is the
    idempotency guarantee both the CLI and the dashboard demonstrate live."""
    store_path = _approved_store_path(runs_root, run_result.profile)
    keys = load_approved_keys(store_path)
    new_count = sum(1 for p in run_result.all_postings if p.idempotency_key not in keys)
    keys.update(p.idempotency_key for p in run_result.all_postings)
    save_approved_keys(store_path, keys)
    return new_count
