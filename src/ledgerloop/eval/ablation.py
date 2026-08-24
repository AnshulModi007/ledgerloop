"""eval/ablation.py -- IMPLEMENTATION.md section 4 (Phase 5).

Runs the identical data profile three ways -- Tier 1 only, Tiers 1+2 (--no-llm), and
the full Tiers 1+2+3 pipeline -- and tabulates the same metrics eval/metrics.py
reports for a single run. The delta between the 1+2 row and the full row is the
measured marginal value of the LLM tier: what adjudication buys over the purely
deterministic system, in matches recovered and (if any) matches it got wrong that
tiers 1+2 would have correctly escalated instead.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from ledgerloop.eval import harness
from ledgerloop.eval.harness import TierCeiling
from ledgerloop.eval.metrics import Metrics, compute_metrics

ROWS: tuple[TierCeiling, ...] = ("tier1", "tier1+2", "full")
ROW_LABELS = {"tier1": "Tier 1 only", "tier1+2": "Tiers 1+2 (--no-llm)", "full": "Tiers 1+2+3 (full)"}


def run_ablation(data_root: Path, profile: str, *, no_llm: bool = False) -> dict[TierCeiling, Metrics]:
    answer_key = harness.load_answer_key(data_root, profile)
    results: dict[TierCeiling, Metrics] = {}
    for tier_ceiling in ROWS:
        run = harness.run(data_root, profile, tier_ceiling, no_llm=no_llm)
        results[tier_ceiling] = compute_metrics(run, answer_key)
    return results


def format_table(results: dict[TierCeiling, Metrics]) -> str:
    header = f"{'row':<22}{'resolved':>10}{'auto-match':>12}{'precision':>11}{'recall':>9}{'false-match':>13}{'records/sec':>13}"
    lines = [header, "-" * len(header)]
    for tier_ceiling in ROWS:
        m = results[tier_ceiling]
        lines.append(
            f"{ROW_LABELS[tier_ceiling]:<22}"
            f"{m.resolved_count:>10}"
            f"{m.auto_match_rate:>12.1%}"
            f"{m.precision:>11.1%}"
            f"{m.recall:>9.1%}"
            f"{m.false_match_rate:>13.2%}"
            f"{m.throughput_records_per_sec:>13.1f}"
        )

    tier2_row = results["tier1+2"]
    full_row = results["full"]
    delta_resolved = full_row.resolved_count - tier2_row.resolved_count
    delta_recall = full_row.recall - tier2_row.recall
    delta_false_match = full_row.false_match_rate - tier2_row.false_match_rate

    lines += [
        "",
        "marginal value of tier3 (full - tiers1+2):",
        f"  +{delta_resolved} records auto-matched"
        if delta_resolved >= 0
        else f"  {delta_resolved} records auto-matched",
        f"  recall {delta_recall:+.1%}",
        f"  false-match rate {delta_false_match:+.2%}"
        + ("  (tier3 introduced false matches -- check confidence_threshold)" if delta_false_match > 0 else ""),
        f"  llm calls spent: {full_row.llm_calls_made} ({full_row.llm_calls_per_1000_records:.1f} per 1,000 records)",
    ]
    return "\n".join(lines)


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout"]), default="dev")
@click.option("--no-llm", "no_llm", is_flag=True, default=False, help="Force NullProvider for the full row too.")
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(profile: str, no_llm: bool, data_root: Path, runs_root: Path) -> None:
    results = run_ablation(data_root, profile, no_llm=no_llm)
    click.echo(format_table(results))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / f"{profile}_ablation.json"
    out_path.write_text(
        json.dumps({k: asdict(v) for k, v in results.items()}, indent=2, sort_keys=True), encoding="utf-8"
    )
    click.echo(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
