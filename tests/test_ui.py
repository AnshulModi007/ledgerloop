"""Dashboard smoke tests via Streamlit's AppTest -- runs ui/app.py's actual script
(sidebar controls, button clicks, session-state reruns) rather than importing
functions out of it, since almost everything in that module is Streamlit call
wiring rather than testable logic on its own (the logic it wires lives in
pipeline.py and exceptions/queue.py, already covered by their own tests). See
IMPLEMENTATION.md section 4 (Phase 6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")  # ui/app.py only needs to be installed via `make install-ui`
from streamlit.testing.v1 import AppTest

from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset

APP_PATH = str(Path(__file__).resolve().parents[1] / "src" / "ledgerloop" / "ui" / "app.py")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    config = load_config()
    seed = config["generate"]["dev_seed"]
    ds = Generator(seed, config).generate()
    data_root = tmp_path / "data"
    write_dataset(ds, data_root / "dev", seed=seed, config=config)
    runs_root = tmp_path / "runs"

    monkeypatch.setenv("LEDGERLOOP_DATA_ROOT", str(data_root))
    monkeypatch.setenv("LEDGERLOOP_RUNS_ROOT", str(runs_root))
    return data_root, runs_root


def test_initial_load_prompts_for_a_run(app_env):
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    assert not at.exception
    assert any("Run reconciliation" in block.value for block in at.info)


def test_running_reconciliation_shows_headline_metrics(app_env):
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at.sidebar.checkbox[0].set_value(True)  # --no-llm, so this stays deterministic and fast
    at.sidebar.button[0].click().run()

    assert not at.exception
    labels = {m.label for m in at.main.metric}
    # AppTest renders every tab, so the tie-out tab's metrics appear here too.
    assert {"Match rate", "Needs review", "Throughput", "Tier split"} <= labels
    assert {"Statement", "Reconciled", "Unreconciled"} <= labels
    match_rate_metric = next(m for m in at.main.metric if m.label == "Match rate")
    assert match_rate_metric.value.endswith("%")


def test_headline_exception_count_is_the_reviewable_queue_not_every_exception(app_env):
    """The dev set escalates ~20 out-of-scope lines that need no decision. Showing
    those as the backlog overstates it by roughly 5x, so the headline counts only
    what needs a human and the rest are carried as a labelled delta."""
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at.sidebar.checkbox[0].set_value(True)
    at.sidebar.button[0].click().run()
    assert not at.exception

    needs_review = next(m for m in at.main.metric if m.label == "Needs review")
    # delta reads "<n> decided, +<n> no-action"
    no_action_count = int(needs_review.delta.split("+")[1].split()[0])
    assert no_action_count > 0  # dev set always carries OUT_OF_SCOPE lines
    assert int(needs_review.value) < no_action_count

    # Both halves are rendered -- the no-action items are demoted, never hidden.
    headers = [h.value for h in at.main.subheader]
    assert any(h.startswith("Needs a decision") for h in headers)
    assert any(h.startswith("No action required") for h in headers)


def test_approve_then_rerun_demonstrates_idempotency(app_env):
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at.sidebar.checkbox[0].set_value(True)
    at.sidebar.button[0].click().run()
    assert not at.exception

    approve_button = next(b for b in at.button if b.label and "Approve postings" in b.label)
    approve_button.click().run()
    assert not at.exception
    assert any("new postings" in block.value for block in at.success)

    # a second click over the identical, unapproved-nothing-new batch must show 0 new
    approve_button_again = next(b for b in at.button if b.label and "Approve postings" in b.label)
    approve_button_again.click().run()
    assert not at.exception
    assert any(block.value == "Approved 0 new postings." for block in at.success)
    assert any("0 new postings" in block.value and "closed" in block.value for block in at.success)


def test_exception_queue_row_action_does_not_raise(app_env):
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at.sidebar.checkbox[0].set_value(True)
    at.sidebar.button[0].click().run()
    assert not at.exception

    row_buttons = [b for b in at.button if b.key and b.key.startswith("approved-")]
    assert row_buttons, "expected at least one exception row on the dev profile"
    row_buttons[0].click().run()
    assert not at.exception


def test_a_review_decision_survives_a_fresh_run(app_env):
    """The gap this closes: decisions used to live only in Streamlit session state, so a
    refresh erased every approve and reject ever made. They must now come back from disk,
    attributed, and land in the audit trail beside the machine's own decisions."""
    from ledgerloop.audit.log import AuditLog
    from ledgerloop.exceptions.decisions import DecisionLog, decision_log_path

    _data_root, runs_root = app_env
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at.sidebar.checkbox[0].set_value(True)
    at.sidebar.text_input[0].set_value("test-reviewer")
    at.sidebar.button[0].click().run()

    approve_button = next(b for b in at.button if b.key and b.key.startswith("approved-"))
    decided_line = approve_button.key.removeprefix("approved-")
    approve_button.click().run()
    assert not at.exception

    stored = DecisionLog(decision_log_path(runs_root, "dev")).current()
    assert stored[decided_line].action == "approved"
    assert stored[decided_line].actor == "test-reviewer"

    audit = [r for r in AuditLog(runs_root / "dev_audit.jsonl").read_all() if r.outcome == "review_decision"]
    assert [r.bank_line_id for r in audit] == [decided_line]

    # A completely fresh app process must rebuild the queue with that decision in place.
    fresh = AppTest.from_file(APP_PATH, default_timeout=60).run()
    fresh.sidebar.checkbox[0].set_value(True)
    fresh.sidebar.button[0].click().run()
    assert not fresh.exception
    restored = next(b for b in fresh.button if b.key == f"approved-{decided_line}")
    assert restored.disabled, "the already-approved row should come back disabled, not open"
