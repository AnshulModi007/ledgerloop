"""eval/metrics.py -- IMPLEMENTATION.md section 4 (Phase 5).

Reports the metrics the spec calls out by name: auto-match rate, precision/recall
against ground truth, false-match rate, tier attribution, throughput, LLM cost, and
the exception breakdown by reason code.

Two of the numbers here need their framing stated up front, because both are easy
to misread as score inflation:

* **Correct-disposition rate** counts a line as correctly handled if the pipeline
  either matched it to the right transactions OR declined to match it when ground
  truth says it had no match to make. Auto-match rate alone scores a correct refusal
  as a failure, which understates a system whose whole design preference is to refuse
  rather than guess. Both rates are always reported together, and the false-match
  rate below is unchanged and still computed the strict way -- the softer number
  never appears without the harder ones beside it.

* **Per-defect-class stats** score each of the generator's twelve defect classes
  separately, using the defect_classes tags the answer key already carries. One bank
  line can carry two classes (a SPLIT_1N line that also draws a TRANSPOSE overlay),
  so it counts once under each and the rows deliberately sum to more than the batch
  size. These are per-class rates, not a partition of the batch.

False-match rate is the primary risk metric: in finance a wrong match is materially
worse than an escalation to a human, and this pipeline is deliberately tuned to
prefer escalating (see tier2.min_resolve_score, tier3.confidence_threshold) over
guessing. It is computed against the TOTAL record count, not just the resolved
subset, and checked in CI against config.yaml's eval.false_match_rate_gate -- a
system that resolves fewer records but is wrong about a larger share of what it does
resolve is not actually safer, so a softer denominator would hide that.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from ledgerloop.eval import harness
from ledgerloop.eval.harness import HarnessRun, is_correct_resolution
from ledgerloop.exceptions import queue as queue_mod
from ledgerloop.exceptions.explain import format_paise
from ledgerloop.generate.schemas import AnswerKeyEntry
from ledgerloop.schemas import Resolution

# Groq's published on-demand rate for openai/gpt-oss-20b (the default provider this
# pipeline resolves to when a key is configured -- see adjudicate/provider.py) as of
# 2026: $0.075/1M input tokens, $0.30/1M output tokens.
# Source: https://www.cloudzero.com/blog/groq-pricing/
#
# Everything below this line is an ASSUMPTION, not a measurement: an adjudication
# batch call (tier3.batch_size=20 cases) is estimated at ~3,000 input / ~800 output
# tokens from the prompt template in adjudicate/prompts.py; actual counts vary with
# candidate density and narration length. USD/INR is assumed at 88.0. Replace these
# constants with real figures (or wire in actual token counts from provider
# responses) before citing this number anywhere it needs to be precise -- the point
# here is to show the order of magnitude and prove the economics were considered,
# not to bill anyone.
GROQ_INPUT_USD_PER_M_TOKENS = 0.075
GROQ_OUTPUT_USD_PER_M_TOKENS = 0.30
ASSUMED_INPUT_TOKENS_PER_CALL = 3000
ASSUMED_OUTPUT_TOKENS_PER_CALL = 800
ASSUMED_USD_TO_INR = 88.0

# Analyst-effort constants. These are ASSUMPTIONS in exactly the same sense as the
# token counts above, and are labelled "illustrative" everywhere they surface.
# Tracing one bank credit back to its constituent transactions by hand -- opening the
# settlement report, filtering to the payout UTR, reconciling the fee arithmetic --
# is taken at 4 minutes. Reviewing an escalation is taken at 2 minutes rather than 0,
# because an exception still costs a human their attention; it arrives with the
# candidates already generated, the arithmetic already done, and a written account of
# why it escalated (see exceptions/explain.py), which is the entire reason it is
# cheaper than tracing from scratch rather than merely being someone else's problem.
# Change these to your own desk's figures before quoting the savings anywhere real.
ASSUMED_MINUTES_PER_MANUAL_TRACE = 4
ASSUMED_MINUTES_PER_EXCEPTION_REVIEW = 2


def _illustrative_cost_inr(llm_calls: int) -> float:
    usd = llm_calls * (
        ASSUMED_INPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_INPUT_USD_PER_M_TOKENS
        + ASSUMED_OUTPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_OUTPUT_USD_PER_M_TOKENS
    )
    return usd * ASSUMED_USD_TO_INR


@dataclass
class DefectStats:
    """How one defect class was handled. `correct_disposition_rate` is the headline:
    for a class whose lines expect a match it means matched correctly, and for
    OUT_OF_SCOPE it means correctly left unmatched. A class scoring 100% here is one
    the pipeline genuinely handles, whichever of the two the right answer was."""

    total: int
    expects_match: int
    correctly_matched: int
    false_matched: int
    correctly_rejected: int
    missed: int
    correct_disposition_rate: float
    tier_counts: dict[str, int]


@dataclass
class Metrics:
    profile: str
    tier_ceiling: str
    total_records: int
    resolved_count: int
    auto_match_rate: float
    precision: float
    recall: float
    false_match_rate: float
    false_match_count: int
    missed_count: int
    tier_counts: dict[str, int]
    tier_shares: dict[str, float]
    exception_counts_by_reason: dict[str, int]
    # -- disposition: matching correctly and refusing correctly are both right answers.
    no_match_expected_count: int
    correctly_rejected_count: int
    correct_disposition_count: int
    correct_disposition_rate: float
    # -- the review queue as a human sees it, split by whether the item needs a decision.
    # Derived from reason codes alone (no ground truth), so these two are the same
    # numbers the dashboard shows on a run against data with no answer key at all.
    exceptions_needing_review: int
    exceptions_no_action: int
    # -- per defect class; rows overlap, see module docstring.
    per_defect: dict[str, DefectStats]
    throughput_records_per_sec: float
    llm_calls_made: int
    llm_calls_per_1000_records: float
    providers_used: list[str]
    illustrative_cost_inr: float
    illustrative_cost_inr_per_1000_records: float
    # -- business impact. Value figures are measured (integer paise); the analyst-hour
    # figures are derived from measured counts and the assumed per-item minutes above.
    value_total_paise: int
    value_auto_reconciled_paise: int
    value_escalated_paise: int
    value_auto_match_rate: float
    illustrative_analyst_hours_manual: float
    illustrative_analyst_hours_with_ledgerloop: float
    illustrative_analyst_hours_saved: float


class _DefectTally:
    """Mutable accumulator behind one DefectStats row. Kept private and separate from
    the frozen-ish dataclass so compute_metrics can tally in a single pass over the
    answer key without building intermediate lists per class."""

    def __init__(self) -> None:
        self.total = 0
        self.expects_match = 0
        self.correctly_matched = 0
        self.false_matched = 0
        self.correctly_rejected = 0
        self.missed = 0
        self.tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}

    def record(self, outcome: str, *, expects_match: bool, resolution: Resolution | None) -> None:
        self.total += 1
        if expects_match:
            self.expects_match += 1
        setattr(self, outcome, getattr(self, outcome) + 1)
        # Tier attribution counts only correct matches: crediting a tier for a line it
        # got wrong would make a tier look more capable on a defect class the more it
        # mishandled it.
        if resolution is not None and outcome == "correctly_matched":
            self.tier_counts[resolution.resolved_by] += 1

    def finish(self) -> DefectStats:
        correct = self.correctly_matched + self.correctly_rejected
        return DefectStats(
            total=self.total,
            expects_match=self.expects_match,
            correctly_matched=self.correctly_matched,
            false_matched=self.false_matched,
            correctly_rejected=self.correctly_rejected,
            missed=self.missed,
            correct_disposition_rate=correct / self.total if self.total else 0.0,
            tier_counts=dict(self.tier_counts),
        )


def compute_metrics(run: HarnessRun, answer_key: dict[str, AnswerKeyEntry]) -> Metrics:
    resolutions_by_bid = {r.bank_line_id: r for r in run.resolutions}
    total = run.total_records

    true_positive = 0
    false_match = 0
    missed = 0
    expects_match_total = 0
    correctly_rejected = 0

    # Per defect class, keyed by the tags the answer key already carries. Accumulated in
    # the same pass as the totals so a class row can never disagree with the headline.
    defect_tallies: dict[str, _DefectTally] = {}

    for bid, answer in answer_key.items():
        expects_match = bool(answer.matched_txn_ids)
        resolution = resolutions_by_bid.get(bid)

        # One of exactly four outcomes per line, and each defect class this line carries
        # gets tallied against the same one.
        if resolution is not None and expects_match and is_correct_resolution(resolution, answer):
            outcome = "correctly_matched"
        elif resolution is not None:
            # Resolved something that was wrong, or resolved a line that should never
            # have been matched at all. Both are false matches -- see the module docstring.
            outcome = "false_matched"
        elif expects_match:
            outcome = "missed"
        else:
            outcome = "correctly_rejected"

        if expects_match:
            expects_match_total += 1
        if outcome == "correctly_matched":
            true_positive += 1
        elif outcome == "false_matched":
            false_match += 1
        elif outcome == "missed":
            missed += 1
        else:
            correctly_rejected += 1

        for defect_class in answer.defect_classes:
            tally = defect_tallies.setdefault(defect_class, _DefectTally())
            tally.record(outcome, expects_match=expects_match, resolution=resolution)

    resolved_count = len(run.resolutions)
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    for r in run.resolutions:
        tier_counts[r.resolved_by] += 1
    tier_shares = {tier: (count / total if total else 0.0) for tier, count in tier_counts.items()}

    exception_counts: dict[str, int] = {}
    for e in run.exceptions:
        exception_counts[e.reason_code] = exception_counts.get(e.reason_code, 0) + 1

    # The review-queue split uses reason codes only -- no ground truth -- so these are
    # the same two numbers the dashboard can show on data that has no answer key.
    needing_review, no_action = queue_mod.partition_by_review_need(queue_mod.build_queue(run.exceptions))

    cost_inr = _illustrative_cost_inr(run.llm_calls_made)

    credit_by_bid = run.credit_paise_by_bank_line
    value_total = sum(credit_by_bid.values())
    value_resolved = sum(credit_by_bid.get(r.bank_line_id, 0) for r in run.resolutions)

    manual_minutes = total * ASSUMED_MINUTES_PER_MANUAL_TRACE
    # Every exception is costed at the full review minute, including the ones the queue
    # marks no-action. Charging those at zero because the pipeline is confident they need
    # nothing would be scoring our own homework -- a reviewer still lays eyes on them.
    assisted_minutes = len(run.exceptions) * ASSUMED_MINUTES_PER_EXCEPTION_REVIEW

    return Metrics(
        profile=run.profile,
        tier_ceiling=run.tier_ceiling,
        total_records=total,
        resolved_count=resolved_count,
        auto_match_rate=resolved_count / total if total else 0.0,
        precision=true_positive / resolved_count if resolved_count else 0.0,
        recall=true_positive / expects_match_total if expects_match_total else 0.0,
        false_match_rate=false_match / total if total else 0.0,
        false_match_count=false_match,
        missed_count=missed,
        tier_counts=tier_counts,
        tier_shares=tier_shares,
        exception_counts_by_reason=dict(sorted(exception_counts.items())),
        no_match_expected_count=total - expects_match_total,
        correctly_rejected_count=correctly_rejected,
        correct_disposition_count=true_positive + correctly_rejected,
        correct_disposition_rate=(true_positive + correctly_rejected) / total if total else 0.0,
        exceptions_needing_review=len(needing_review),
        exceptions_no_action=len(no_action),
        per_defect={name: tally.finish() for name, tally in sorted(defect_tallies.items())},
        throughput_records_per_sec=total / run.wall_seconds if run.wall_seconds > 0 else float("inf"),
        llm_calls_made=run.llm_calls_made,
        llm_calls_per_1000_records=run.llm_calls_made * 1000 / total if total else 0.0,
        providers_used=run.providers_used,
        illustrative_cost_inr=cost_inr,
        illustrative_cost_inr_per_1000_records=cost_inr * 1000 / total if total else 0.0,
        value_total_paise=value_total,
        value_auto_reconciled_paise=value_resolved,
        value_escalated_paise=value_total - value_resolved,
        value_auto_match_rate=value_resolved / value_total if value_total else 0.0,
        illustrative_analyst_hours_manual=manual_minutes / 60,
        illustrative_analyst_hours_with_ledgerloop=assisted_minutes / 60,
        illustrative_analyst_hours_saved=(manual_minutes - assisted_minutes) / 60,
    )


def format_report(m: Metrics) -> str:
    lines = [
        f"profile: {m.profile}  tier_ceiling: {m.tier_ceiling}  records: {m.total_records}",
        "",
        f"auto-match rate:   {m.auto_match_rate:.1%}  ({m.resolved_count}/{m.total_records})",
        f"precision:         {m.precision:.1%}",
        f"recall:            {m.recall:.1%}",
        (
            f"false-match rate:  {m.false_match_rate:.2%}  ({m.false_match_count} of {m.total_records})"
            "  <- PRIMARY RISK METRIC"
        ),
        f"missed (escalated instead of matched): {m.missed_count}",
        (
            f"correct disposition: {m.correct_disposition_rate:.1%}  "
            f"({m.correct_disposition_count}/{m.total_records}) = "
            f"{m.correct_disposition_count - m.correctly_rejected_count} matched correctly + "
            f"{m.correctly_rejected_count} correctly left unmatched "
            f"(of {m.no_match_expected_count} that had no match to make)"
        ),
        "",
        (
            f"review queue: {m.exceptions_needing_review} need a human decision, "
            f"{m.exceptions_no_action} auto-dispositioned as out-of-scope (no action, still listed)"
        ),
        "",
        # Value-weighted, not just count-weighted: resolving most of the lines while
        # leaving most of the money escalated would be a materially different result.
        (
            f"value auto-reconciled: {m.value_auto_match_rate:.1%}  "
            f"({format_paise(m.value_auto_reconciled_paise, 'Rs.')} "
            f"of {format_paise(m.value_total_paise, 'Rs.')})"
        ),
        f"value escalated for review: {format_paise(m.value_escalated_paise, 'Rs.')}",
        "",
        "tier attribution:",
    ]
    for tier in ("tier1", "tier2", "tier3"):
        lines.append(f"  {tier}: {m.tier_counts[tier]} ({m.tier_shares[tier]:.1%} of all records)")
    lines += ["", "exceptions by reason code:"]
    if m.exception_counts_by_reason:
        lines.extend(f"  {reason}: {count}" for reason, count in m.exception_counts_by_reason.items())
    else:
        lines.append("  (none)")
    lines += ["", "per defect class (a line carrying two classes counts under each):"]
    if m.per_defect:
        lines.append(f"  {'class':<24}{'n':>5}{'matched':>9}{'false':>7}{'rejected':>10}{'missed':>8}{'correct':>10}")
        for name, d in m.per_defect.items():
            lines.append(
                f"  {name:<24}{d.total:>5}{d.correctly_matched:>9}{d.false_matched:>7}"
                f"{d.correctly_rejected:>10}{d.missed:>8}{d.correct_disposition_rate:>10.1%}"
            )
    else:
        lines.append("  (answer key carries no defect tags)")
    lines += [
        "",
        f"throughput: {m.throughput_records_per_sec:.1f} records/sec",
        f"llm calls: {m.llm_calls_made} ({m.llm_calls_per_1000_records:.1f} per 1,000 records)",
        f"llm providers used this run: {', '.join(m.providers_used) if m.providers_used else 'none -- deterministic only'}",
        (
            f"illustrative cost at published paid rates: Rs.{m.illustrative_cost_inr:.2f} total "
            f"(Rs.{m.illustrative_cost_inr_per_1000_records:.2f} per 1,000 records) -- "
            "actual cost this run: Rs.0 (free tier); see module docstring for rate assumptions"
        ),
        (
            f"illustrative analyst effort: {m.illustrative_analyst_hours_manual:.1f}h manual "
            f"-> {m.illustrative_analyst_hours_with_ledgerloop:.1f}h reviewing exceptions "
            f"({m.illustrative_analyst_hours_saved:.1f}h saved on {m.total_records} records) -- "
            f"assumes {ASSUMED_MINUTES_PER_MANUAL_TRACE} min/trace and "
            f"{ASSUMED_MINUTES_PER_EXCEPTION_REVIEW} min/exception; see module docstring"
        ),
    ]
    return "\n".join(lines)


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout", "scale"]), default="dev")
@click.option("--no-llm", "no_llm", is_flag=True, default=False)
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(profile: str, no_llm: bool, data_root: Path, runs_root: Path) -> None:
    """Run the full pipeline once and report the Phase 5 metrics."""
    run = harness.run(data_root, profile, "full", no_llm=no_llm)
    answer_key = harness.load_answer_key(data_root, profile)
    metrics = compute_metrics(run, answer_key)

    click.echo(format_report(metrics))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / f"{profile}_eval_metrics.json"
    out_path.write_text(json.dumps(asdict(metrics), indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"\nwritten: {out_path}")

    gates = run.config["eval"]
    if metrics.false_match_rate > gates["false_match_rate_gate"]:
        raise SystemExit(
            f"GATE FAILED: false-match rate {metrics.false_match_rate:.2%} "
            f"exceeds {gates['false_match_rate_gate']:.2%}"
        )
    if metrics.auto_match_rate < gates["auto_match_rate_gate"]:
        raise SystemExit(
            f"GATE FAILED: auto-match rate {metrics.auto_match_rate:.2%} "
            f"below {gates['auto_match_rate_gate']:.2%}"
        )


if __name__ == "__main__":
    main()
