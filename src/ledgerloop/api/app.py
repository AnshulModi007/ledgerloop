"""FastAPI surface over the reconciliation pipeline.

The third consumer of `pipeline.run()`, after the CLI (`reconcile.py`) and the Streamlit
dashboard (`ui/app.py`). Like both of those it owns no matching logic: every number it
returns was computed by the pipeline, so the three surfaces cannot drift into reporting
different results for the same run. That constraint is the reason this file is mostly
serialisation.

Money crosses this boundary as **integer paise**, with a `_inr` string alongside for
display. Amounts are integers everywhere inside the pipeline precisely so that no
rounding can happen where nobody is looking, and handing a float to a browser -- where
0.1 + 0.2 is famously not 0.3 -- would throw that away at the last step. The frontend
formats the string and never does arithmetic.

Run `python -m ledgerloop.api` (or `make api`) and open http://127.0.0.1:8000.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ledgerloop.adjudicate.provider import resolve_chain
from ledgerloop.api.runs import RunRecord, RunRegistry
from ledgerloop.exceptions import queue as queue_mod
from ledgerloop.exceptions.decisions import DecisionAction, default_actor
from ledgerloop.exceptions.explain import format_paise

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = Path(os.environ.get("LEDGERLOOP_DATA_ROOT", REPO_ROOT / "data"))
RUNS_ROOT = Path(os.environ.get("LEDGERLOOP_RUNS_ROOT", REPO_ROOT / "runs"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

registry = RunRegistry(DATA_ROOT, RUNS_ROOT)
api = APIRouter(prefix="/api")


# -- wire models ---------------------------------------------------------------------
#
# Deliberately separate from the pipeline's own Pydantic models. The internal schemas are
# free to change shape as the matcher evolves; this is a published contract a frontend
# depends on, and leaking `evidence: dict` straight onto the wire would make every
# internal rename a breaking API change.


def money(paise: int) -> dict[str, Any]:
    """Integer paise plus a preformatted string. The client never divides by 100."""
    return {"paise": paise, "inr": format_paise(paise, "Rs.")}


class RunRequest(BaseModel):
    profile: str = "dev"
    no_llm: bool = False


class DecisionRequest(BaseModel):
    bank_line_id: str
    action: DecisionAction
    actor: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class ProfileInfo(BaseModel):
    name: str
    bank_lines: int


# -- read-only metadata ---------------------------------------------------------------


@api.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "data_root": str(DATA_ROOT), "runs_root": str(RUNS_ROOT)}


@api.get("/profiles")
def profiles() -> list[ProfileInfo]:
    """Data profiles present on disk. Counting the lines here means the UI never offers
    a profile whose files are missing or half-generated."""
    found: list[ProfileInfo] = []
    if not DATA_ROOT.exists():
        return found
    for path in sorted(DATA_ROOT.iterdir()):
        statement = path / "bank_statement.csv"
        if not statement.exists():
            continue
        with open(statement, encoding="utf-8") as f:
            line_count = max(sum(1 for _ in f) - 1, 0)  # minus the header
        found.append(ProfileInfo(name=path.name, bank_lines=line_count))
    return found


@api.get("/providers")
def providers() -> dict[str, Any]:
    """What the LLM chain would resolve to right now, without making a call.

    Every provider's availability check is local by contract -- an env var being set, or
    a socket probe -- so this is safe to poll and costs nothing. It exists because
    'which model actually served this run' was previously only visible in CLI output,
    and a dashboard that silently fell back to abstaining looked identical to one
    adjudicating properly.
    """
    chain = [p.name for p in resolve_chain()]
    return {
        "chain": chain,
        "active": chain[0] if chain else "none",
        "llm_available": chain[:1] != ["none"],
        "pin": os.environ.get("LEDGERLOOP_PROVIDER") or None,
        "default_reviewer": default_actor(),
    }


# -- runs ------------------------------------------------------------------------------


def _require(run_id: str) -> RunRecord:
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    return record


def _require_complete(run_id: str) -> RunRecord:
    record = _require(run_id)
    if record.status == "running":
        raise HTTPException(status_code=409, detail="run still in progress")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error or "run failed")
    return record


def _summary(record: RunRecord) -> dict[str, Any]:
    base = {
        "run_id": record.run_id,
        "profile": record.profile,
        "no_llm": record.no_llm,
        "status": record.status,
        "wall_seconds": record.wall_seconds,
        "error": record.error,
    }
    result = record.result
    if result is None:
        return base

    resolved = len(result.resolutions)
    needs_review, no_action = queue_mod.partition_by_review_need(record.queue_items)
    return {
        **base,
        "total_records": result.total_records,
        "resolved_count": resolved,
        "auto_match_rate": resolved / result.total_records if result.total_records else 0.0,
        "tier_counts": result.tier_counts,
        "exception_count": len(result.exceptions),
        "reason_counts": result.reason_counts,
        "exceptions_needing_review": len(needs_review),
        "exceptions_no_action": len(no_action),
        "llm_calls_made": result.llm_calls_made,
        "llm_available": result.llm_available,
        # sorted+deduped so the banner is stable; a run may touch a provider more than once
        "providers_used": sorted(set(result.providers_used)),
        "postings_total": len(result.all_postings),
        "postings_new": len(result.new_postings),
        "last_approved_new_count": record.last_approved_new_count,
        "tie_out_clean": result.tie_out.clean,
        "duplicate_receivable_relief": result.duplicate_receivable_relief,
    }


@api.post("/runs", status_code=202)
def start_run(request: RunRequest) -> dict[str, Any]:
    """Starts a run and returns immediately with an id; poll GET /api/runs/{id}.

    202 rather than 201: the run is accepted, and the thing the client wants does not
    exist yet."""
    available = {p.name for p in profiles()}
    if request.profile not in available:
        raise HTTPException(
            status_code=400,
            detail=f"unknown profile '{request.profile}' (have: {', '.join(sorted(available)) or 'none'})",
        )
    record = registry.start(request.profile, no_llm=request.no_llm)
    return _summary(record)


@api.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    return [_summary(r) for r in registry.list_runs()]


@api.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    return _summary(_require(run_id))


@api.get("/runs/{run_id}/exceptions")
def get_exceptions(run_id: str) -> dict[str, Any]:
    """The queue, split by whether a human actually has to decide something.

    The split is the honest way to report a review burden: on the held-out set 20 of 21
    exceptions were OUT_OF_SCOPE, where declining to match IS the right answer and no
    decision is owed. They stay listed -- nothing is silently dropped -- but presenting
    them beside a genuine amount dispute would overstate the work by 20x."""
    record = _require_complete(run_id)
    needs_review, no_action = queue_mod.partition_by_review_need(record.queue_items)

    # The credit amount comes from the run's own view of the statement -- an exception
    # carries a reason, not a balance -- so a view can never show an amount the run
    # didn't see.
    credits = record.result.credit_paise_by_bank_line

    def item(qi: queue_mod.QueueItem) -> dict[str, Any]:
        exception = qi.exception
        paise = credits.get(exception.bank_line_id)
        return {
            "bank_line_id": exception.bank_line_id,
            "reason_code": exception.reason_code,
            "explanation": exception.explanation,
            "candidates_considered": exception.candidates_considered,
            "status": qi.status,
            "reviewer_note": qi.reviewer_note,
            "requires_review": queue_mod.requires_review(qi),
            "credit": money(paise) if paise is not None else None,
        }

    return {
        "needs_review": [item(qi) for qi in needs_review],
        "no_action": [item(qi) for qi in no_action],
        "reason_codes": sorted({qi.exception.reason_code for qi in record.queue_items}),
        "escalated_value": money(
            sum(credits.get(qi.exception.bank_line_id, 0) for qi in needs_review)
        ),
    }


@api.get("/runs/{run_id}/journal")
def get_journal(run_id: str) -> dict[str, Any]:
    """Proposed postings, grouped by the bank line that generated them."""
    record = _require_complete(run_id)
    result = record.result
    approved_keys = {p.idempotency_key for p in result.all_postings} - {
        p.idempotency_key for p in result.new_postings
    }

    def batch_payload(batch) -> dict[str, Any]:
        debits = sum(p.amount_paise for p in batch.postings if p.direction == "debit")
        credits = sum(p.amount_paise for p in batch.postings if p.direction == "credit")
        return {
            "bank_line_id": batch.bank_line_id,
            "settlement_batch_ids": batch.settlement_batch_ids,
            "resolved_by": batch.resolved_by,
            "status": batch.status,
            # Computed here rather than trusted: an unbalanced batch is exactly the thing
            # a reviewer must be able to see, so it is never assumed away.
            "balanced": debits == credits,
            "debits": money(debits),
            "credits": money(credits),
            "postings": [
                {
                    "account": p.account,
                    "direction": p.direction,
                    "amount": money(p.amount_paise),
                    "posting_type": p.posting_type,
                    "txn_id": p.txn_id,
                    "already_approved": p.idempotency_key in approved_keys,
                }
                for p in batch.postings
            ],
        }

    batches = [batch_payload(b) for b in result.journal_batches]
    return {
        "batches": batches,
        "posting_count": len(result.all_postings),
        "new_posting_count": len(result.new_postings),
        "all_balanced": all(b["balanced"] for b in batches),
    }


@api.get("/runs/{run_id}/tieout")
def get_tieout(run_id: str) -> dict[str, Any]:
    """The reconciliation statement and its four controls -- what a controller signs."""
    record = _require_complete(run_id)
    t = record.result.tie_out
    return {
        "statement": {
            "total": money(t.statement_total_paise),
            "line_count": t.statement_line_count,
            "reconciled": money(t.reconciled_paise),
            "reconciled_line_count": t.reconciled_line_count,
            "unreconciled": money(t.unreconciled_paise),
            "unreconciled_line_count": t.unreconciled_line_count,
        },
        "controls": {
            "cash_ties_out": t.cash_ties_out,
            "bank_receipt_total": money(t.bank_receipt_total_paise),
            "balances": t.balances,
            "total_debits": money(t.total_debits_paise),
            "total_credits": money(t.total_credits_paise),
            "rounding_adjustment_gross": money(t.rounding_adjustment_gross_paise),
            "rounding_adjustment_net": money(t.rounding_adjustment_net_paise),
            "rounding_adjustment_count": t.rounding_adjustment_count,
            "duplicate_receivable_relief": t.duplicate_receivable_relief,
        },
        "clean": t.clean,
        "movements": [
            {
                "account": m.account,
                "debit": money(m.debit_paise),
                "credit": money(m.credit_paise),
                "net": money(m.net_paise),
                "posting_count": m.posting_count,
            }
            for m in t.movements
        ],
    }


@api.get("/runs/{run_id}/audit")
def get_audit(run_id: str, limit: int = 100) -> dict[str, Any]:
    """Tail of the append-only audit log for this run's profile.

    Read straight off disk rather than from memory: the log is the record of what
    happened, and serving a cached copy would let the UI show something the file does
    not say."""
    record = _require(run_id)
    path = RUNS_ROOT / f"{record.profile}_audit.jsonl"
    if not path.exists():
        return {"entries": [], "total": 0}
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tail = lines[-limit:] if limit > 0 else lines
    entries = []
    for line in tail:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partially written last line is not a reason to fail the request
    return {"entries": list(reversed(entries)), "total": len(lines)}


# -- mutations --------------------------------------------------------------------------


@api.post("/runs/{run_id}/decisions")
def post_decision(run_id: str, request: DecisionRequest) -> dict[str, Any]:
    """Records one human decision, durably and idempotently.

    `was_new: false` means the identical decision already stood -- not an error, and
    worth surfacing rather than hiding: it is the same idempotency property the postings
    have, showing up on the human half of the loop."""
    record = _require_complete(run_id)
    was_new, error = registry.decide(
        record,
        bank_line_id=request.bank_line_id,
        action=request.action,
        actor=(request.actor or "").strip() or default_actor(),
        note=request.note,
    )
    if error is not None:
        raise HTTPException(status_code=404, detail=error)
    return {"was_new": was_new, **get_exceptions(run_id)}


@api.post("/runs/{run_id}/approve")
def post_approve(run_id: str) -> dict[str, Any]:
    """Persists this run's postings. Call twice: the second call reports 0 new, which is
    the reconciliation loop closing rather than double-posting."""
    record = _require_complete(run_id)
    new_count = registry.approve(record)
    return {"new_postings": new_count, "total_postings": len(record.result.all_postings)}


# -- app --------------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="LedgerLoop",
        version="0.1.0",
        description=(
            "Multi-source settlement reconciliation. Deterministic tiers first, LLM "
            "adjudication confined to selecting from a fixed candidate menu."
        ),
    )
    app.include_router(api)

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


LogLevel = Literal["critical", "error", "warning", "info", "debug", "trace"]


def main(host: str = "127.0.0.1", port: int = 8000, log_level: LogLevel = "info") -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
