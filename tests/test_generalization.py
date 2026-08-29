"""The generalization suite's own guards -- see generate/novel.py and eval/generalization.py.

Two kinds of test live here, and the second kind matters more than the first.

The gate itself asserts the design claim on unfamiliar input: no defect shape the matcher
was never built for may be matched *wrongly*. Escalating every one of them would pass, and
should -- refusing unfamiliar work is the designed behaviour.

The rest guard the suite against becoming vacuous. A generalization test that quietly stops
testing generalization is worse than no test at all, because it keeps reporting a pass: if
a novel shape gets added to DefectClass, or the trap's misleading UTR stops being extracted,
or the control lines stop matching, then "zero wrong matches" would be trivially true and
would still print PASS. Each of those is asserted here directly.
"""

from __future__ import annotations

import json

import pytest

from ledgerloop.config import load_config
from ledgerloop.eval import generalization
from ledgerloop.generate.defects import ALL_DEFECT_CLASSES
from ledgerloop.generate.novel import NovelShape, generate_novel
from ledgerloop.ingest.normalise import load_and_normalise


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def novel_data_root(tmp_path_factory, config):
    root = tmp_path_factory.mktemp("novel_data")
    generate_novel(config, root)
    return root


@pytest.fixture(scope="module")
def report(novel_data_root, config):
    return generalization.run(novel_data_root, config=config)


# -- the gate ---------------------------------------------------------------------------


def test_no_novel_shape_is_ever_matched_wrongly(report):
    """The claim the whole architecture rests on, tested against data it never saw: on
    input it does not understand, the pipeline escalates rather than guesses."""
    offenders = [s.shape for s in report.shapes if s.matched_wrongly]
    assert offenders == [], (
        f"shapes matched WRONGLY: {offenders}. A false match on an unfamiliar defect shape "
        "means the refuse-rather-than-guess property depended on having anticipated the "
        "defect, which is not a safety property at all."
    )
    assert report.passed


def test_nothing_is_silently_dropped(report):
    """Every bank line must be either resolved or carry a typed reason code. A line in
    neither is worse than a wrong match: nobody would ever know to look for it."""
    assert report.silently_dropped == []


# -- guards against the suite going vacuous ----------------------------------------------


def test_novel_shapes_are_absent_from_the_taxonomy_the_matcher_was_built_against():
    """The instant one of these lands in DefectClass, tier2 may have been written with it
    in mind and this stops being a generalization test."""
    known = {c.value for c in ALL_DEFECT_CLASSES}
    overlap = sorted(known & {s.value for s in NovelShape})
    assert overlap == [], f"{overlap} are no longer novel -- replace them or the suite is testing nothing"


def test_the_stale_utr_trap_actually_presents_misleading_evidence(novel_data_root):
    """STALE_UTR_REUSE is the only shape that makes the *evidence* look right while
    pointing at the wrong batch, so it carries most of the suite's value. If the narration
    stopped carrying an extractable UTR, or that UTR stopped naming a real batch, the
    pipeline would be refusing a line with no evidence at all -- an easy pass that proves
    nothing. Assert the trap is loaded."""
    ds = load_and_normalise(novel_data_root / "novel")
    answer_key = {
        e["bank_line_id"]: e
        for e in json.loads((novel_data_root / "novel" / "answer_key.json").read_text(encoding="utf-8"))
    }
    batches_by_utr: dict[str, set[str]] = {}
    for line in ds.settlement_lines:
        batches_by_utr.setdefault(line.payout_utr, set()).add(line.settlement_batch_id)

    stale = [
        b for b in ds.bank_lines if NovelShape.STALE_UTR_REUSE.value in answer_key[b.bank_line_id]["defect_classes"]
    ]
    assert stale, "the trap shape produced no lines"
    for line in stale:
        assert line.extracted_utr, f"{line.bank_line_id}: no UTR extracted -- the trap is not loaded"
        assert batches_by_utr.get(line.extracted_utr), (
            f"{line.bank_line_id}: its UTR names no real batch -- the misleading evidence is not misleading"
        )
        assert answer_key[line.bank_line_id]["matched_txn_ids"] == [], "ground truth should say: match nothing"


def test_control_lines_still_match_so_escalating_everything_cannot_pass(report):
    """Without in-distribution controls, a pipeline that escalated literally every line
    would score a perfect zero wrong matches. The controls are what make the gate mean
    something."""
    assert report.control_lines > 0
    assert report.control_matched == report.control_lines


def test_every_escalated_novel_line_carries_a_typed_reason(report):
    """Escalation is only an acceptable outcome because it is *typed* -- a reviewer is
    told what the pipeline could not do. An untyped escalation is a silent drop wearing a
    different name."""
    for shape in report.shapes:
        assert sum(shape.reason_codes.values()) == shape.escalated, (
            f"{shape.shape}: {shape.escalated} escalated but "
            f"{sum(shape.reason_codes.values())} carry a reason code"
        )
