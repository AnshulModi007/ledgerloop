"""LedgerLoop dashboard. A thin view over pipeline.py, exceptions/queue.py, and
ledger/journal.py -- no reconciliation logic lives here, only presentation and the
reviewer-action bookkeeping queue.py already models as pure functions on immutable
QueueItems. See IMPLEMENTATION.md section 4 (Phase 6).

Run with: streamlit run src/ledgerloop/ui/app.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import streamlit as st

from ledgerloop import pipeline
from ledgerloop.exceptions import queue as queue_mod
from ledgerloop.exceptions.explain import rupees
from ledgerloop.schemas import Exception_

REPO_ROOT = Path(__file__).resolve().parents[3]
# Overridable via env var so tests/test_ui.py can point the app at an isolated,
# generated dataset instead of the repo's real data/ and runs/ directories.
DATA_ROOT = Path(os.environ.get("LEDGERLOOP_DATA_ROOT", REPO_ROOT / "data"))
RUNS_ROOT = Path(os.environ.get("LEDGERLOOP_RUNS_ROOT", REPO_ROOT / "runs"))

st.set_page_config(page_title="LedgerLoop", layout="wide")


# -- sidebar: select a batch, run reconciliation -------------------------------------

with st.sidebar:
    st.title("LedgerLoop")
    st.caption("Multi-source settlement reconciliation agent")

    available_profiles = sorted(p.name for p in DATA_ROOT.iterdir() if (p / "bank_statement.csv").exists())
    profile = st.selectbox("Batch (data profile)", available_profiles, index=available_profiles.index("dev") if "dev" in available_profiles else 0)
    no_llm = st.checkbox("Force --no-llm (deterministic tiers 1+2 only)", value=False)

    run_clicked = st.button("Run reconciliation", type="primary", width="stretch")

if run_clicked:
    with st.spinner(f"Reconciling profile '{profile}': tier1 exact join -> tier2 algorithmic -> tier3 adjudication -> exceptions -> journal..."):
        start = time.perf_counter()
        run_result = pipeline.run(DATA_ROOT, profile, RUNS_ROOT, no_llm=no_llm)
        wall_seconds = time.perf_counter() - start
    st.session_state.run = run_result
    st.session_state.wall_seconds = wall_seconds
    st.session_state.queue_items = queue_mod.build_queue(run_result.exceptions)
    st.session_state.last_reapprove_new_count = None

run: pipeline.ReconcileRun | None = st.session_state.get("run")

if run is None:
    st.info("Select a batch and click **Run reconciliation** in the sidebar to get started.")
    st.stop()

# -- LLM provider banner --------------------------------------------------------------

if run.providers_used:
    st.success(f"LLM provider this run: **{', '.join(sorted(set(run.providers_used)))}** ({run.llm_calls_made} calls)")
elif run.llm_available:
    st.warning("LLM provider: configured, but no successful call was made this run.")
else:
    st.info("LLM provider: **none -- deterministic only** (tiers 1+2 only; every candidate the model would have seen was resolved or escalated deterministically)")

# -- headline metrics -------------------------------------------------------------------

wall_seconds = st.session_state.get("wall_seconds", 0.0)
resolved_count = len(run.resolutions)
match_rate = resolved_count / run.total_records if run.total_records else 0.0
throughput = run.total_records / wall_seconds if wall_seconds > 0 else float("inf")

# The headline exception number is the queue a human actually has to work. The
# out-of-scope lines are still exceptions and still listed below -- they are just not
# pending decisions, and showing the combined count as "the backlog" overstates it by
# an order of magnitude on a typical batch. See exceptions/queue.py.
_needs_review, _no_action = queue_mod.partition_by_review_need(st.session_state.queue_items)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Match rate", f"{match_rate:.1%}", f"{resolved_count}/{run.total_records}")
col2.metric("Needs review", len(_needs_review), f"+{len(_no_action)} no-action", delta_color="off")
col3.metric("Throughput", f"{throughput:,.0f} rec/s")
col4.metric("Tier split", f"{run.tier_counts.get('tier1', 0)}/{run.tier_counts.get('tier2', 0)}/{run.tier_counts.get('tier3', 0)}", "tier1 / tier2 / tier3")

tab_exceptions, tab_journal, tab_run = st.tabs(["Exception queue", "Proposed journal entries", "Run details"])

# -- exception queue --------------------------------------------------------------------

with tab_exceptions:
    items: list[queue_mod.QueueItem] = st.session_state.queue_items

    reason_codes = sorted({item.exception.reason_code for item in items})
    filter_col, status_col = st.columns(2)
    reason_filter = filter_col.multiselect("Filter by reason code", reason_codes, default=reason_codes)
    status_filter = status_col.multiselect(
        "Filter by status", ["open", "approved", "rejected", "reassigned"], default=["open", "approved", "rejected", "reassigned"]
    )

    visible = [item for item in items if item.exception.reason_code in reason_filter and item.status in status_filter]
    st.caption(f"{len(visible)} of {len(items)} exceptions shown")

    visible_needs_review, visible_no_action = queue_mod.partition_by_review_need(visible)

    def render(item: queue_mod.QueueItem) -> None:
        exc: Exception_ = item.exception
        with st.container(border=True):
            header_col, status_col2 = st.columns([4, 1])
            header_col.markdown(f"**{exc.bank_line_id}** -- `{exc.reason_code}`")
            status_col2.markdown(f"status: **{item.status}**")

            if exc.explanation:
                st.write(exc.explanation)
            if exc.candidates_considered:
                st.caption(f"candidates considered: {', '.join(exc.candidates_considered)}")
            with st.expander("evidence"):
                st.json(exc.evidence)

            b_approve, b_reject, b_reassign = st.columns(3)
            if b_approve.button("Approve", key=f"approve-{exc.bank_line_id}", disabled=item.status == "approved"):
                items[items.index(item)] = queue_mod.apply_action(item, "approved")
                st.rerun()
            if b_reject.button("Reject", key=f"reject-{exc.bank_line_id}", disabled=item.status == "rejected"):
                items[items.index(item)] = queue_mod.apply_action(item, "rejected")
                st.rerun()
            if b_reassign.button("Reassign", key=f"reassign-{exc.bank_line_id}", disabled=item.status == "reassigned"):
                items[items.index(item)] = queue_mod.apply_action(item, "reassigned")
                st.rerun()

    st.subheader(f"Needs a decision ({len(visible_needs_review)})")
    if visible_needs_review:
        for item in visible_needs_review:
            render(item)
    else:
        st.success("Nothing pending a human decision in this batch.")

    if visible_no_action:
        st.subheader(f"No action required ({len(visible_no_action)})")
        st.caption(
            "Bank credits the pipeline positively determined were never gateway settlements "
            "-- direct transfers, refund reversals. Declining to match these is the correct "
            "outcome, not a failure, so they are separated from the work rather than dropped. "
            "Expand to review or override any of them."
        )
        with st.expander(f"Show {len(visible_no_action)} auto-dispositioned lines"):
            for item in visible_no_action:
                render(item)

# -- proposed journal entries + the idempotency demo -------------------------------------

with tab_journal:
    st.write(f"{len(run.journal_batches)} journal batches, {len(run.all_postings)} postings proposed.")

    rows = [
        {
            "bank_line_id": p.bank_line_id,
            "posting_type": p.posting_type,
            "account": p.account,
            "direction": p.direction,
            "amount": rupees(p.amount_paise),
            "txn_id": p.txn_id or "",
        }
        for batch in run.journal_batches
        for p in batch.postings
    ]
    st.dataframe(rows, width="stretch", height=350)

    st.divider()
    st.subheader("Approve and verify idempotency")
    st.caption(
        "One click: persists every posting above as approved, then re-runs reconciliation "
        "from scratch over the same batch and reports how many postings are new. A correct "
        "run always reports zero -- that's the idempotency guarantee."
    )

    if st.button("Approve postings, then re-run", type="primary"):
        with st.spinner("Approving, then re-running..."):
            newly_approved = pipeline.approve(RUNS_ROOT, run)
            start = time.perf_counter()
            rerun_result = pipeline.run(DATA_ROOT, profile, RUNS_ROOT, no_llm=no_llm)
            rerun_wall = time.perf_counter() - start
        st.session_state.run = rerun_result
        st.session_state.wall_seconds = rerun_wall
        st.session_state.queue_items = queue_mod.build_queue(rerun_result.exceptions)
        st.session_state.last_reapprove_new_count = newly_approved
        st.session_state.last_rerun_new_count = len(rerun_result.new_postings)
        st.rerun()

    if st.session_state.get("last_reapprove_new_count") is not None:
        approved_count = st.session_state.last_reapprove_new_count
        rerun_new_count = st.session_state.get("last_rerun_new_count")
        st.success(f"Approved {approved_count} new postings.")
        if rerun_new_count == 0:
            st.success("Re-run over the same batch: 0 new postings. The loop is closed.")
        else:
            st.warning(f"Re-run over the same batch: {rerun_new_count} new postings (expected 0 -- inputs or config changed).")

# -- run details ------------------------------------------------------------------------

with tab_run:
    st.write("exceptions by reason code:")
    st.json(run.reason_counts)
    st.write("config hash and tier thresholds live in the audit log:")
    st.code(str(RUNS_ROOT / f"{profile}_audit.jsonl"))
