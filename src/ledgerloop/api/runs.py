"""In-process registry of reconciliation runs, and the locking around the files they write.

A run takes seconds, not milliseconds -- 18 records/sec with a local model in the loop,
and Tier 3's first call can block while a cold model loads. Holding an HTTP request open
for that would make the UI feel broken and would time out behind any real proxy, so
`start()` returns immediately with an id and the work happens on a worker thread. The
frontend polls `GET /api/runs/{id}`.

Deliberately in-memory. Runs are reproducible from the data on disk (`--no-llm` is
byte-identical across runs, which the CI determinism gate enforces), and the things that
must actually survive a restart -- approved postings, review decisions, the audit log --
were already durable files before this API existed and still are. Adding a database here
would move the source of truth away from those files for no gain. The cost is that run
*ids* are process-scoped: restart the server and the client re-runs. That is the right
trade for a single-operator tool, and the wrong one for a multi-tenant service, which is
a stated non-goal.

Two locks, because two different things can race:

  - `_registry_lock` guards the run dict itself.
  - `_write_lock` serialises every mutation of the on-disk stores. `DecisionLog` and the
    approved-postings store are append/read-modify-write over plain files with no locking
    of their own; that was safe when the only callers were a CLI process and a
    single-threaded Streamlit script, and stops being safe the moment two HTTP requests
    can arrive at once. Approving from two browser tabs would otherwise interleave a read
    and a write and silently drop one tab's approvals.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ledgerloop import pipeline
from ledgerloop.exceptions import queue as queue_mod
from ledgerloop.exceptions.decisions import DecisionAction

RunStatus = Literal["running", "complete", "failed"]

# Serialises writes to the decision log and the approved-postings store. Module-level
# and coarse on purpose: these writes take microseconds and happen at human pace, so
# there is nothing to gain from finer granularity and a great deal to lose from getting
# it subtly wrong.
_write_lock = threading.Lock()


@dataclass
class RunRecord:
    run_id: str
    profile: str
    no_llm: bool
    status: RunStatus = "running"
    started_at: float = field(default_factory=time.time)
    wall_seconds: float | None = None
    error: str | None = None
    result: pipeline.ReconcileRun | None = None
    queue_items: list[queue_mod.QueueItem] = field(default_factory=list)
    # Postings persisted by an approve call against this run, so the UI can show the
    # idempotency result ("5 new" then "0 new") without re-running the pipeline.
    last_approved_new_count: int | None = None


class RunRegistry:
    def __init__(self, data_root: Path, runs_root: Path) -> None:
        self.data_root = data_root
        self.runs_root = runs_root
        self._runs: dict[str, RunRecord] = {}
        self._registry_lock = threading.Lock()

    # -- lookup ----------------------------------------------------------------------

    def get(self, run_id: str) -> RunRecord | None:
        with self._registry_lock:
            return self._runs.get(run_id)

    def list_runs(self) -> list[RunRecord]:
        with self._registry_lock:
            return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)

    # -- starting work ---------------------------------------------------------------

    def start(self, profile: str, *, no_llm: bool) -> RunRecord:
        record = RunRecord(run_id=uuid.uuid4().hex[:12], profile=profile, no_llm=no_llm)
        with self._registry_lock:
            self._runs[record.run_id] = record
        thread = threading.Thread(target=self._execute, args=(record,), daemon=True)
        thread.start()
        return record

    def _execute(self, record: RunRecord) -> None:
        started = time.perf_counter()
        try:
            result = pipeline.run(self.data_root, record.profile, self.runs_root, no_llm=record.no_llm)
            items = queue_mod.apply_stored_decisions(
                queue_mod.build_queue(result.exceptions), result.review_decisions
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the client, never swallowed
            record.error = f"{type(exc).__name__}: {exc}"
            record.status = "failed"
            # Full traceback to the server log; the client gets the one-line summary.
            traceback.print_exc()
        else:
            record.result = result
            record.queue_items = items
            record.status = "complete"
        finally:
            record.wall_seconds = time.perf_counter() - started

    # -- mutations, serialised -------------------------------------------------------

    def decide(
        self, record: RunRecord, *, bank_line_id: str, action: DecisionAction, actor: str, note: str | None
    ) -> tuple[bool, str | None]:
        """Records one review decision. Returns (was_new, error). was_new is False when
        the identical decision already stands, which is not an error -- it is the
        idempotency guarantee showing through."""
        assert record.result is not None
        exception = next(
            (e for e in record.result.exceptions if e.bank_line_id == bank_line_id), None
        )
        if exception is None:
            return False, f"no exception for bank line {bank_line_id} in this run"

        with _write_lock:
            _decision, was_new = pipeline.decide(
                self.runs_root,
                record.profile,
                exception=exception,
                action=action,
                config=record.result.config,
                actor=actor,
                note=note,
            )
            # Rehydrate from the durable log rather than patching the in-memory item, so
            # what the UI shows next is what was actually written.
            record.result.review_decisions = pipeline.DecisionLog(
                pipeline.decision_log_path(self.runs_root, record.profile)
            ).current()
            record.queue_items = queue_mod.apply_stored_decisions(
                queue_mod.build_queue(record.result.exceptions), record.result.review_decisions
            )
        return was_new, None

    def approve(self, record: RunRecord) -> int:
        assert record.result is not None
        with _write_lock:
            new_count = pipeline.approve(self.runs_root, record.result)
        record.last_approved_new_count = new_count
        return new_count
