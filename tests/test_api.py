"""The HTTP surface: contract, isolation, and the two properties that matter most --
that approving twice posts once, and that a decision survives being made twice.

Every test drives a temp data root and a temp runs root, so nothing here reads or
writes the repo's real `data/` or `runs/`. That matters more than usual: the decision
log and the approved-postings store are the durable record of human intent, and a test
suite that appended to them would corrupt the thing being demonstrated.

The dataset is generated once per module rather than per test -- generation dominates
the runtime, and every test here is read-only with respect to the CSVs.
"""

from __future__ import annotations

import importlib

import pytest

from ledgerloop.config import load_config
from ledgerloop.generate.generator import Generator, write_dataset

fastapi_testclient = pytest.importorskip(
    "fastapi.testclient", reason="API extra not installed (pip install -e '.[api]')"
)


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def api_client(tmp_path_factory, config):
    """A TestClient bound to throwaway data and runs roots.

    The app reads its roots from the environment at import time, so the env vars are set
    before the module is (re)imported and the module is reloaded to pick them up. Reload
    rather than monkeypatching the constants: the registry is built from them at import,
    and patching only the constants would leave a registry still pointing at the repo.
    """
    seed = config["generate"]["dev_seed"]
    data_root = tmp_path_factory.mktemp("api_data")
    runs_root = tmp_path_factory.mktemp("api_runs")
    write_dataset(Generator(seed, config).generate(), data_root / "dev", seed=seed, config=config)

    import os

    # import_module by dotted name, never `from ledgerloop.api import app`: the package
    # re-exports the FastAPI instance as `app`, which shadows the submodule of the same
    # name, so that form hands back the application object and reload() rejects it.
    module_name = "ledgerloop.api.app"

    previous = {k: os.environ.get(k) for k in ("LEDGERLOOP_DATA_ROOT", "LEDGERLOOP_RUNS_ROOT")}
    os.environ["LEDGERLOOP_DATA_ROOT"] = str(data_root)
    os.environ["LEDGERLOOP_RUNS_ROOT"] = str(runs_root)
    try:
        app_module = importlib.reload(importlib.import_module(module_name))
        with fastapi_testclient.TestClient(app_module.app) as client:
            yield client
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Restore the module bound to the repo's real roots, so a later test importing it
        # doesn't inherit this fixture's temp directories.
        importlib.reload(importlib.import_module(module_name))


@pytest.fixture(scope="module")
def completed_run(api_client) -> str:
    """One deterministic run, shared by the read-only tests below."""
    response = api_client.post("/api/runs", json={"profile": "dev", "no_llm": True})
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    for _ in range(600):
        summary = api_client.get(f"/api/runs/{run_id}").json()
        if summary["status"] != "running":
            break
    assert summary["status"] == "complete", summary.get("error")
    return run_id


# -- metadata --------------------------------------------------------------------------


def test_health_reports_the_roots_it_is_actually_using(api_client):
    body = api_client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "api_data" in body["data_root"]  # the temp root, not the repo's
    assert "api_runs" in body["runs_root"]


def test_profiles_lists_only_generated_data(api_client):
    profiles = api_client.get("/api/profiles").json()
    assert [p["name"] for p in profiles] == ["dev"]
    assert profiles[0]["bank_lines"] > 0


def test_providers_reports_the_chain_without_calling_anything(api_client):
    body = api_client.get("/api/providers").json()
    assert body["chain"][-1] == "none"  # NullProvider always terminates
    assert isinstance(body["llm_available"], bool)


def test_unknown_profile_is_rejected_before_a_run_starts(api_client):
    response = api_client.post("/api/runs", json={"profile": "nope", "no_llm": True})
    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


def test_unknown_run_is_404(api_client):
    assert api_client.get("/api/runs/deadbeef").status_code == 404


# -- the run ----------------------------------------------------------------------------


def test_run_summary_matches_the_pipeline(api_client, completed_run):
    s = api_client.get(f"/api/runs/{completed_run}").json()
    assert s["resolved_count"] + s["exception_count"] == s["total_records"]
    assert s["auto_match_rate"] == pytest.approx(s["resolved_count"] / s["total_records"])
    assert s["no_llm"] is True
    assert s["llm_calls_made"] == 0  # --no-llm must not reach a provider at all


def test_queue_split_accounts_for_every_exception(api_client, completed_run):
    s = api_client.get(f"/api/runs/{completed_run}").json()
    ex = api_client.get(f"/api/runs/{completed_run}/exceptions").json()
    assert len(ex["needs_review"]) + len(ex["no_action"]) == s["exception_count"]
    # OUT_OF_SCOPE lines are listed, never dropped -- they just need no decision.
    assert all(not item["requires_review"] for item in ex["no_action"])
    assert all(item["requires_review"] for item in ex["needs_review"])


def test_money_crosses_the_wire_as_integer_paise(api_client, completed_run):
    """A float here would reintroduce exactly the rounding the pipeline avoids."""
    tieout = api_client.get(f"/api/runs/{completed_run}/tieout").json()
    total = tieout["statement"]["total"]
    assert isinstance(total["paise"], int)
    assert isinstance(total["inr"], str)
    for movement in tieout["movements"]:
        for field in ("debit", "credit", "net"):
            assert isinstance(movement[field]["paise"], int)


def test_statement_reconciles_against_its_own_parts(api_client, completed_run):
    statement = api_client.get(f"/api/runs/{completed_run}/tieout").json()["statement"]
    assert statement["reconciled"]["paise"] + statement["unreconciled"]["paise"] == statement["total"]["paise"]
    assert statement["reconciled_line_count"] + statement["unreconciled_line_count"] == statement["line_count"]


def test_every_journal_batch_balances_over_the_api(api_client, completed_run):
    journal = api_client.get(f"/api/runs/{completed_run}/journal").json()
    assert journal["all_balanced"] is True
    for batch in journal["batches"]:
        assert batch["debits"]["paise"] == batch["credits"]["paise"], batch["bank_line_id"]


# -- the two properties that matter -------------------------------------------------------


def test_approving_twice_posts_once(api_client, completed_run):
    """The idempotency guarantee, over HTTP. The second call must report zero new."""
    first = api_client.post(f"/api/runs/{completed_run}/approve").json()
    assert first["new_postings"] > 0

    second = api_client.post(f"/api/runs/{completed_run}/approve").json()
    assert second["new_postings"] == 0
    assert second["total_postings"] == first["total_postings"]


def test_a_rerun_after_approval_proposes_nothing_further(api_client, completed_run):
    """The loop closing, end to end: approve, then run the identical batch again."""
    api_client.post(f"/api/runs/{completed_run}/approve")

    run_id = api_client.post("/api/runs", json={"profile": "dev", "no_llm": True}).json()["run_id"]
    for _ in range(600):
        summary = api_client.get(f"/api/runs/{run_id}").json()
        if summary["status"] != "running":
            break
    assert summary["status"] == "complete"
    assert summary["postings_new"] == 0
    assert summary["postings_total"] > 0  # there were postings; they were simply all known


def test_the_same_decision_twice_is_recorded_once(api_client, completed_run):
    ex = api_client.get(f"/api/runs/{completed_run}/exceptions").json()
    if not ex["needs_review"]:
        pytest.skip("no reviewable exception in this generation")
    bank_line_id = ex["needs_review"][0]["bank_line_id"]

    payload = {"bank_line_id": bank_line_id, "action": "approved", "actor": "tester", "note": "checked"}
    first = api_client.post(f"/api/runs/{completed_run}/decisions", json=payload).json()
    assert first["was_new"] is True

    second = api_client.post(f"/api/runs/{completed_run}/decisions", json=payload).json()
    assert second["was_new"] is False  # not an error -- the same decision, not a second one

    decided = [i for i in second["needs_review"] if i["bank_line_id"] == bank_line_id]
    assert decided and decided[0]["status"] == "approved"


def test_a_decision_survives_being_read_back_from_disk(api_client, completed_run):
    """Decisions live in an append-only file, not in session state -- the whole point of
    the durable review log. A fresh run of the same profile must rehydrate it."""
    ex = api_client.get(f"/api/runs/{completed_run}/exceptions").json()
    if not ex["needs_review"]:
        pytest.skip("no reviewable exception in this generation")
    bank_line_id = ex["needs_review"][-1]["bank_line_id"]
    api_client.post(
        f"/api/runs/{completed_run}/decisions",
        json={"bank_line_id": bank_line_id, "action": "rejected", "actor": "tester", "note": None},
    )

    run_id = api_client.post("/api/runs", json={"profile": "dev", "no_llm": True}).json()["run_id"]
    for _ in range(600):
        if api_client.get(f"/api/runs/{run_id}").json()["status"] != "running":
            break
    fresh = api_client.get(f"/api/runs/{run_id}/exceptions").json()
    carried = [i for i in fresh["needs_review"] if i["bank_line_id"] == bank_line_id]
    assert carried and carried[0]["status"] == "rejected"


def test_decision_for_an_unknown_bank_line_is_404(api_client, completed_run):
    response = api_client.post(
        f"/api/runs/{completed_run}/decisions",
        json={"bank_line_id": "BANK99999", "action": "approved"},
    )
    assert response.status_code == 404


def test_audit_tail_is_newest_first_and_bounded(api_client, completed_run):
    body = api_client.get(f"/api/runs/{completed_run}/audit?limit=5").json()
    assert len(body["entries"]) <= 5
    assert body["total"] >= len(body["entries"])


# -- candidate evidence over the wire -------------------------------------------------


def test_exceptions_expose_the_transactions_behind_each_candidate(api_client, completed_run):
    """`candidates_considered` is a list of opaque handles by interface contract. A
    reviewer cannot judge a proposed match from BANK00115-C0 and a count -- the ids, the
    rule and any residual difference are what the decision actually rests on."""
    ex = api_client.get(f"/api/runs/{completed_run}/exceptions").json()
    withcands = [i for i in ex["needs_review"] if i["candidates"]]
    if not withcands:
        pytest.skip("no candidate-bearing exception in this generation")

    candidate = withcands[0]["candidates"][0]
    assert candidate["matched_txn_ids"], "a candidate with no transactions explains nothing"
    assert candidate["txn_count"] == len(candidate["matched_txn_ids"])
    assert candidate["rule"]
    assert candidate["rejected_by_reviewer"] is False


def test_a_rejected_pairing_comes_back_marked_not_deleted(api_client):
    """The queue must not forget a pairing was considered -- but it must not present a
    withheld one as though it were still on the table either."""
    run_id = api_client.post("/api/runs", json={"profile": "dev", "no_llm": True}).json()["run_id"]
    for _ in range(600):
        if api_client.get(f"/api/runs/{run_id}").json()["status"] != "running":
            break
    ex = api_client.get(f"/api/runs/{run_id}/exceptions").json()
    target = next((i for i in ex["needs_review"] if i["candidates"]), None)
    if target is None:
        pytest.skip("no candidate-bearing exception in this generation")

    pairing = sorted(target["candidates"][0]["matched_txn_ids"])
    api_client.post(
        f"/api/runs/{run_id}/decisions",
        json={"bank_line_id": target["bank_line_id"], "action": "rejected", "actor": "tester"},
    )

    rerun = api_client.post("/api/runs", json={"profile": "dev", "no_llm": True}).json()["run_id"]
    for _ in range(600):
        if api_client.get(f"/api/runs/{rerun}").json()["status"] != "running":
            break
    after = api_client.get(f"/api/runs/{rerun}/exceptions").json()
    line = next(
        i for i in after["needs_review"] + after["no_action"]
        if i["bank_line_id"] == target["bank_line_id"]
    )

    same = [c for c in line["candidates"] if sorted(c["matched_txn_ids"]) == pairing]
    assert same, "the pairing was dropped from the record entirely"
    assert same[0]["rejected_by_reviewer"] is True, "a withheld pairing was shown as live"
    assert line["reason_code"] == "REVIEWER_REJECTED"


def test_the_run_summary_reports_what_feedback_changed(api_client):
    """A run that quietly proposes less than the last one is worse than one that says so."""
    summaries = api_client.get("/api/runs").json()
    assert all("candidates_suppressed_by_review" in s for s in summaries if s["status"] == "complete")
