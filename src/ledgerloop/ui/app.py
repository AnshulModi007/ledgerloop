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
from ledgerloop.exceptions import decisions as decisions_mod
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

    # Self-reported, and labelled as such: authentication is a stated non-goal, so this
    # records who says they made a decision rather than proving it. Still worth having --
    # an unattributed decision log answers "what" but never "who".
    reviewer = st.text_input(
        "Reviewer (recorded with each decision)",
        value=decisions_mod.default_actor(),
        help="Self-reported. Set LEDGERLOOP_REVIEWER to change the default. Not authentication.",
    )

    run_clicked = st.button("Run reconciliation", type="primary", width="stretch")

if run_clicked:
    with st.spinner(f"Reconciling profile '{profile}': tier1 exact join -> tier2 algorithmic -> tier3 adjudication -> exceptions -> journal..."):
        start = time.perf_counter()
        run_result = pipeline.run(DATA_ROOT, profile, RUNS_ROOT, no_llm=no_llm)
        wall_seconds = time.perf_counter() - start
    st.session_state.run = run_result
    st.session_state.wall_seconds = wall_seconds
    st.session_state.queue_items = queue_mod.apply_stored_decisions(
        queue_mod.build_queue(run_result.exceptions), run_result.review_decisions
    )
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
_undecided = [i for i in _needs_review if i.status == "open"]
col2.metric(
    "Needs review",
    len(_undecided),
    f"{len(_needs_review) - len(_undecided)} decided, +{len(_no_action)} no-action",
    delta_color="off",
)
col3.metric("Throughput", f"{throughput:,.0f} rec/s")
col4.metric("Tier split", f"{run.tier_counts.get('tier1', 0)}/{run.tier_counts.get('tier2', 0)}/{run.tier_counts.get('tier3', 0)}", "tier1 / tier2 / tier3")

tab_exceptions, tab_journal, tab_tieout, tab_run = st.tabs(
    ["Exception queue", "Proposed journal entries", "Tie-out", "Run details"]
)

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

            stored = run.review_decisions.get(exc.bank_line_id)
            if stored is not None:
                st.caption(
                    f"decided **{stored.action}** by *{stored.actor}* at {stored.decided_at_utc}"
                    + (f" -- {stored.note}" if stored.note else "")
                )

            note = st.text_input(
                "Reviewer note (recorded with the decision)",
                key=f"note-{exc.bank_line_id}",
                value=stored.note or "" if stored else "",
                label_visibility="collapsed",
                placeholder="Optional note -- recorded in the audit trail with your decision",
            )

            b_approve, b_reject, b_reassign = st.columns(3)
            for column, action, label in (
                (b_approve, "approved", "Approve"),
                (b_reject, "rejected", "Reject"),
                (b_reassign, "reassigned", "Reassign"),
            ):
                if column.button(label, key=f"{action}-{exc.bank_line_id}", disabled=item.status == action):
                    # Durable and mirrored into the audit trail, not just session state --
                    # a refresh used to erase every decision ever made here.
                    pipeline.decide(
                        RUNS_ROOT,
                        profile,
                        exception=exc,
                        action=action,
                        config=run.config,
                        actor=reviewer,
                        note=note or None,
                    )
                    run.review_decisions = pipeline.DecisionLog(
                        pipeline.decision_log_path(RUNS_ROOT, profile)
                    ).current()
                    items[items.index(item)] = queue_mod.apply_action(item, action, note=note or None)
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
        st.session_state.queue_items = queue_mod.apply_stored_decisions(
            queue_mod.build_queue(rerun_result.exceptions), rerun_result.review_decisions
        )
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

# -- tie-out: the statement a controller signs -------------------------------------------

with tab_tieout:
    t = run.tie_out
    if t.clean:
        st.success("Tie-out clean: cash and books agree, and no transaction was relieved twice.")
    else:
        st.error("Tie-out NOT clean -- review the controls below before approving anything.")

    c1, c2, c3 = st.columns(3)
    c1.metric("Statement", rupees(t.statement_total_paise), f"{t.statement_line_count} credits")
    c2.metric("Reconciled", rupees(t.reconciled_paise), f"{t.reconciled_line_count} lines")
    c3.metric("Unreconciled", rupees(t.unreconciled_paise), f"{t.unreconciled_line_count} lines", delta_color="off")

    st.subheader("Controls")
    st.dataframe(
        [
            {
                "control": "cash ties out",
                "result": "PASS" if t.cash_ties_out else "FAIL",
                "detail": f"bank receipts {rupees(t.bank_receipt_total_paise)} vs {rupees(t.reconciled_paise)} reconciled",
            },
            {
                "control": "books balance",
                "result": "PASS" if t.balances else "FAIL",
                "detail": f"debits {rupees(t.total_debits_paise)} vs credits {rupees(t.total_credits_paise)}",
            },
            {
                "control": "receivable cleared once",
                "result": "PASS" if not t.duplicate_receivable_relief else "FAIL",
                "detail": f"{len(t.duplicate_receivable_relief)} transaction(s) cleared by more than one bank line",
            },
            {
                "control": "fee drift absorbed",
                "result": "INFO",
                "detail": (
                    f"{rupees(t.rounding_adjustment_gross_paise)} gross across "
                    f"{t.rounding_adjustment_count} postings (net {rupees(t.rounding_adjustment_net_paise)})"
                ),
            },
        ],
        width="stretch",
        hide_index=True,
    )

    st.subheader("Movement by control account")
    st.dataframe(
        [
            {
                "account": m.account,
                "debit": rupees(m.debit_paise),
                "credit": rupees(m.credit_paise),
                "postings": m.posting_count,
            }
            for m in t.movements
        ],
        width="stretch",
        hide_index=True,
    )

# -- run details ------------------------------------------------------------------------

with tab_run:
    if run.duplicate_receivable_relief:
        st.error(
            f"**Ledger control: {len(run.duplicate_receivable_relief)} transaction(s) had their settlement "
            "receivable cleared by more than one bank line.** Every batch still balances -- each one balances "
            "against its own credit -- so a per-batch check cannot see this. It needs a human: which payout "
            "was the erroneous one is a question about the gateway, not about the statement's arithmetic."
        )
        st.json(run.duplicate_receivable_relief)

    st.write("review decisions on record (durable, mirrored into the audit log):")
    st.json(decisions_mod.counts_by_action(run.review_decisions) or {"(none yet)": 0})

    st.write("exceptions by reason code:")
    st.json(run.reason_counts)
    st.write("config hash and tier thresholds live in the audit log:")
    st.code(str(RUNS_ROOT / f"{profile}_audit.jsonl"))
