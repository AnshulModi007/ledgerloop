"""eval/feedback_loop.py -- does a reviewer's rejection actually stick?

The claim under test is not "the feature is implemented". It is:

    A pairing a human rejected is never proposed again, even by a model that would
    otherwise be confident enough to resolve it.

That qualifier is the point. Tier 3 confidence is not stable run to run against a live
model -- on byte-identical input, local llama3.1 returned 0.55, 0.55, then 1.0
(FAILURES.md, 2026-09-01). Without suppression a line escalated on Monday can resolve on
Tuesday with nothing changed, quietly overturning the reviewer who looked at it. The
metric here is the rate at which that happens.

Method, on a throwaway runs root so no real review history is touched:

  1. Run the pipeline. Take every escalation that carried a candidate pairing.
  2. Record a `rejected` decision against each, exactly as the console would.
  3. Run again, byte-identical inputs.
  4. Count how many of those rejected pairings were proposed a second time.

`repeat_proposal_rate` must be 0.0. Anything else means a human decision was ignored,
and the gate fails the build.

Reported alongside it, because it is the honest cost side of the same ledger:
`llm_calls_saved` -- calls not spent re-litigating a matter a human already closed.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click

from ledgerloop import pipeline
from ledgerloop.adjudicate.feedback import pairing_key
from ledgerloop.config import load_config


@dataclass
class FeedbackLoopReport:
    profile: str
    no_llm: bool
    # -- run 1: the baseline, with no standing decisions at all
    escalations_before: int
    rejectable_pairings: int  # escalations that carried a candidate to have an opinion about
    llm_calls_before: int
    # -- run 2: identical inputs, with the rejections standing
    escalations_after: int
    llm_calls_after: int
    candidates_suppressed: int
    lines_fully_suppressed: int
    # -- the measurement
    repeat_proposals: int
    repeat_proposal_rate: float
    llm_calls_saved: int
    repeat_proposal_detail: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.repeat_proposals == 0


def _proposed_pairings(run: pipeline.ReconcileRun) -> dict[str, set[frozenset[str]]]:
    """Every pairing this run actually *proposed*, which is narrower than every pairing it
    lists.

    A resolution is a proposal. So is a live candidate on an escalation -- the queue is
    asking a human to consider it. What is **not** a proposal is a pairing the run withheld
    because a reviewer already rejected it: the exception still lists it, deliberately, so
    the queue does not forget the pairing was ever considered, and its explanation says in
    words that it was not proposed again.

    Getting this wrong in the obvious direction -- counting everything the exception lists
    -- scores retained history as a repeat offence and reports 100% while suppression is
    working correctly. That was the first version of this metric. The distinction is not a
    judgement call made here: `suppressed_pairings` is written onto the case by the
    adjudicator at the moment it withholds one, so this reads a fact rather than inferring
    one.
    """
    proposed: dict[str, set[frozenset[str]]] = {}
    for resolution in run.resolutions:
        proposed.setdefault(resolution.bank_line_id, set()).add(pairing_key(resolution.matched_txn_ids))
    for exception in run.exceptions:
        withheld = {pairing_key(p) for p in exception.evidence.get("suppressed_pairings", [])}
        for detail in exception.evidence.get("candidate_detail", []):
            pairing = pairing_key(detail.get("matched_txn_ids", []))
            if pairing in withheld:
                continue
            proposed.setdefault(exception.bank_line_id, set()).add(pairing)
    return proposed


def run(data_root: Path, profile: str, *, no_llm: bool, runs_root: Path) -> FeedbackLoopReport:
    first = pipeline.run(data_root, profile, runs_root, no_llm=no_llm)

    # Reject the strongest candidate on every escalation that has one -- the same pairing
    # the console shows a reviewer, recorded through the same path the console uses.
    rejected: dict[str, frozenset[str]] = {}
    for exception in first.exceptions:
        best = next(iter(exception.evidence.get("candidate_detail", [])), None)
        if not best or not best.get("matched_txn_ids"):
            continue
        rejected[exception.bank_line_id] = pairing_key(best["matched_txn_ids"])
        pipeline.decide(
            runs_root,
            profile,
            exception=exception,
            action="rejected",
            config=first.config,
            actor="eval-feedback-loop",
            note="rejected by the feedback-loop evaluation",
        )

    second = pipeline.run(data_root, profile, runs_root, no_llm=no_llm)
    proposed_again = _proposed_pairings(second)

    repeats = [
        bank_line_id
        for bank_line_id, pairing in rejected.items()
        if pairing in proposed_again.get(bank_line_id, frozenset())
    ]

    return FeedbackLoopReport(
        profile=profile,
        no_llm=no_llm,
        escalations_before=len(first.exceptions),
        rejectable_pairings=len(rejected),
        llm_calls_before=first.llm_calls_made,
        escalations_after=len(second.exceptions),
        llm_calls_after=second.llm_calls_made,
        candidates_suppressed=second.candidates_suppressed_by_review,
        lines_fully_suppressed=len(second.lines_suppressed_by_review),
        repeat_proposals=len(repeats),
        repeat_proposal_rate=(len(repeats) / len(rejected) if rejected else 0.0),
        llm_calls_saved=max(first.llm_calls_made - second.llm_calls_made, 0),
        repeat_proposal_detail=sorted(repeats),
    )


def format_report(r: FeedbackLoopReport) -> str:
    lines = [
        f"profile: {r.profile}  llm: {'off' if r.no_llm else 'on'}",
        "",
        "run 1 -- no standing decisions",
        f"  escalations:                {r.escalations_before}",
        f"  of those, with a pairing:   {r.rejectable_pairings}  <- all rejected by a reviewer",
        f"  llm calls:                  {r.llm_calls_before}",
        "",
        "run 2 -- identical inputs, rejections standing",
        f"  escalations:                {r.escalations_after}",
        f"  candidates suppressed:      {r.candidates_suppressed}",
        f"  lines with nothing left:    {r.lines_fully_suppressed}",
        f"  llm calls:                  {r.llm_calls_after}  ({r.llm_calls_saved} saved)",
        "",
        (
            f"REPEAT-PROPOSAL RATE: {r.repeat_proposal_rate:.1%}  "
            f"({r.repeat_proposals} of {r.rejectable_pairings} rejected pairings proposed again)"
        ),
    ]
    if r.repeat_proposal_detail:
        lines.append(f"  re-proposed: {', '.join(r.repeat_proposal_detail)}")
    lines.append("")
    lines.append(
        "PASS: every rejection held." if r.passed
        else "FAIL: a pairing a human rejected was proposed again."
    )
    return "\n".join(lines)


@click.command()
@click.option("--profile", default="dev", show_default=True)
@click.option("--no-llm/--llm", "no_llm", default=True, help="Deterministic tiers only by default.")
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(profile: str, no_llm: bool, data_root: Path, runs_root: Path) -> None:
    """Measure whether a reviewer's rejection survives the next run."""
    load_config()  # fail fast on a broken config before doing any work

    # A temporary runs root, always: this evaluation writes dozens of rejections, and
    # doing that to the real decision log would corrupt the very record the product is
    # built to keep. Only the report lands in the caller's runs root.
    with tempfile.TemporaryDirectory(prefix="ledgerloop-feedback-") as scratch:
        report = run(data_root, profile, no_llm=no_llm, runs_root=Path(scratch))

    click.echo(format_report(report))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / f"{profile}_feedback_loop.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"\nwritten: {out_path}")

    if not report.passed:
        raise SystemExit("GATE FAILED: a pairing a reviewer rejected was proposed again")


if __name__ == "__main__":
    main()
