"""eval/metrics.py -- IMPLEMENTATION.md section 4 (Phase 5).

Reports the metrics the spec calls out by name: auto-match rate, precision/recall
against ground truth, false-match rate, tier attribution, throughput, LLM cost, and
the exception breakdown by reason code.

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
from ledgerloop.generate.schemas import AnswerKeyEntry

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


def _illustrative_cost_inr(llm_calls: int) -> float:
    usd = llm_calls * (
        ASSUMED_INPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_INPUT_USD_PER_M_TOKENS
        + ASSUMED_OUTPUT_TOKENS_PER_CALL / 1_000_000 * GROQ_OUTPUT_USD_PER_M_TOKENS
    )
    return usd * ASSUMED_USD_TO_INR


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
    throughput_records_per_sec: float
    llm_calls_made: int
    llm_calls_per_1000_records: float
    providers_used: list[str]
    illustrative_cost_inr: float
    illustrative_cost_inr_per_1000_records: float


def compute_metrics(run: HarnessRun, answer_key: dict[str, AnswerKeyEntry]) -> Metrics:
    resolutions_by_bid = {r.bank_line_id: r for r in run.resolutions}
    total = run.total_records

    true_positive = 0
    false_match = 0
    missed = 0
    expects_match_total = 0

    for bid, answer in answer_key.items():
        expects_match = bool(answer.matched_txn_ids)
        if expects_match:
            expects_match_total += 1
        resolution = resolutions_by_bid.get(bid)
        if resolution is not None:
            if expects_match and is_correct_resolution(resolution, answer):
                true_positive += 1
            else:
                false_match += 1
        elif expects_match:
            missed += 1

    resolved_count = len(run.resolutions)
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    for r in run.resolutions:
        tier_counts[r.resolved_by] += 1
    tier_shares = {tier: (count / total if total else 0.0) for tier, count in tier_counts.items()}

    exception_counts: dict[str, int] = {}
    for e in run.exceptions:
        exception_counts[e.reason_code] = exception_counts.get(e.reason_code, 0) + 1

    cost_inr = _illustrative_cost_inr(run.llm_calls_made)

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
        throughput_records_per_sec=total / run.wall_seconds if run.wall_seconds > 0 else float("inf"),
        llm_calls_made=run.llm_calls_made,
        llm_calls_per_1000_records=run.llm_calls_made * 1000 / total if total else 0.0,
        providers_used=run.providers_used,
        illustrative_cost_inr=cost_inr,
        illustrative_cost_inr_per_1000_records=cost_inr * 1000 / total if total else 0.0,
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
    ]
    return "\n".join(lines)


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout"]), default="dev")
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
