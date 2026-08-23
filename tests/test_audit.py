"""The audit log is append-only, and a run must be replayable from it alone: enough
fields survive the round-trip to reconstruct resolution/tier-attribution/exception
counts without re-running the pipeline. See IMPLEMENTATION.md section 4.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from ledgerloop.audit.log import AuditLog
from ledgerloop.config import load_config
from ledgerloop.schemas import Exception_, Resolution


def _resolution(bank_line_id: str, resolved_by: str, **evidence) -> Resolution:
    return Resolution(
        bank_line_id=bank_line_id,
        matched_txn_ids=["TXN000001"],
        resolved_by=resolved_by,
        confidence=0.95,
        evidence=evidence,
        audit_id=f"AUD-{bank_line_id}",
    )


def _exception(bank_line_id: str, reason_code: str, **evidence) -> Exception_:
    return Exception_(
        bank_line_id=bank_line_id,
        reason_code=reason_code,
        candidates_considered=[],
        explanation=evidence.pop("explanation", None),
        evidence=evidence,
    )


def test_log_is_append_only_across_multiple_runs(tmp_path):
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")

    log.append_run(resolutions=[_resolution("BANK00001", "tier1", rule="exact_utr_amount_join")], exceptions=[], config=config)
    log.append_run(resolutions=[_resolution("BANK00002", "tier2", rule="utr_amount_tolerance")], exceptions=[], config=config)

    records = log.read_all()
    assert [r.bank_line_id for r in records] == ["BANK00001", "BANK00002"]


def test_every_record_carries_a_timestamp_and_config_hash(tmp_path):
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_run(resolutions=[_resolution("BANK00001", "tier1", rule="exact_utr_amount_join")], exceptions=[], config=config)

    record = log.read_all()[0]
    assert record.config_hash  # non-empty
    # ISO-8601, real wall-clock time -- parseable, and explicitly not part of any
    # determinism guarantee (see test_determinism.py's docstring).
    parsed = date.fromisoformat(record.timestamp_utc[:10])
    assert parsed.year >= 2026


def test_tier3_resolutions_carry_the_full_model_response(tmp_path):
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_run(
        resolutions=[
            _resolution(
                "BANK00001",
                "tier3",
                rule="llm_adjudication",
                reasoning="amount and UTR line up",
                prompt_version="adjudicate-v1",
            )
        ],
        exceptions=[],
        config=config,
    )
    record = log.read_all()[0]
    assert record.model_response is not None
    assert record.model_response["reasoning"] == "amount and UTR line up"
    assert record.prompt_version == "adjudicate-v1"


def test_tier1_and_tier2_resolutions_carry_no_model_response(tmp_path):
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_run(resolutions=[_resolution("BANK00001", "tier1", rule="exact_utr_amount_join")], exceptions=[], config=config)
    record = log.read_all()[0]
    assert record.model_response is None


def test_exceptions_are_logged_with_reason_code(tmp_path):
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append_run(
        resolutions=[],
        exceptions=[_exception("BANK00003", "OUT_OF_SCOPE", tier2_reason_hint="NO_CANDIDATE")],
        config=config,
    )
    record = log.read_all()[0]
    assert record.outcome == "exception"
    assert record.reason_code == "OUT_OF_SCOPE"
    assert record.resolved_by is None


def test_run_is_replayable_for_summary_metrics(tmp_path):
    """The concrete bar for "replayable from the log alone": reconstruct resolution
    count, tier attribution, and exception reason-code breakdown purely from
    read_all(), matching what was actually appended.
    """
    config = load_config()
    log = AuditLog(tmp_path / "audit.jsonl")

    resolutions = [
        _resolution("BANK00001", "tier1", rule="exact_utr_amount_join"),
        _resolution("BANK00002", "tier1", rule="exact_utr_amount_join"),
        _resolution("BANK00003", "tier2", rule="utr_amount_tolerance"),
        _resolution("BANK00004", "tier3", rule="llm_adjudication", reasoning="r"),
    ]
    exceptions = [
        _exception("BANK00005", "OUT_OF_SCOPE"),
        _exception("BANK00006", "LOW_CONFIDENCE"),
        _exception("BANK00007", "LOW_CONFIDENCE"),
    ]
    log.append_run(resolutions=resolutions, exceptions=exceptions, config=config)

    records = log.read_all()
    resolved_records = [r for r in records if r.outcome == "resolved"]
    exception_records = [r for r in records if r.outcome == "exception"]

    assert len(resolved_records) == len(resolutions)
    assert len(exception_records) == len(exceptions)
    assert Counter(r.resolved_by for r in resolved_records) == Counter(r.resolved_by for r in resolutions)
    assert Counter(r.reason_code for r in exception_records) == Counter(e.reason_code for e in exceptions)


def test_read_all_on_missing_file_returns_empty(tmp_path):
    log = AuditLog(tmp_path / "does_not_exist.jsonl")
    assert log.read_all() == []
