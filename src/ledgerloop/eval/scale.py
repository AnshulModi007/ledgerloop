"""eval/scale.py -- how far the deterministic tiers actually go before they stop.

Every accuracy number in this repo comes from a 5,000-transaction batch. That is enough
to demonstrate correctness and nothing at all about volume, and "it scales" is not a
claim worth making without a measurement behind it. This module generates the same
dataset shape at several sizes, runs the identical --no-llm pipeline over each, and
reports throughput and accuracy side by side, so the curve is visible rather than
asserted.

The thing being stressed is tier2's bounded subset-sum search (match/subset_sum.py). It
is pure Python, and its per-line cost grows with how many transactions fall inside a
candidate's date window -- which grows with total volume. When the search exhausts its
node budget it emits TIER2_TIMEOUT rather than a wrong answer, so the failure mode under
load is *more escalations*, never more false matches. This report counts those timeouts
explicitly: they are the early-warning signal that the budget in config.yaml needs
raising for a batch that size.

Defect density is held constant as volume rises (config.generate's
scale_min_instances_per_defect scales alongside scale_n_gateway_transactions). Leaving
the defect count fixed while transactions grow would dilute the hard cases among more
clean ones and make the big runs *easier* than the small ones -- a benchmark that gets
easier as it gets bigger measures nothing.

Not measured here: peak memory. Doing it portably needs psutil, which is not a
dependency, and tracemalloc's overhead would distort the timings this module exists to
report. Saying so beats publishing a number we did not take.

The scale profile is a benchmark, never a graded set: dev and holdout remain the only
datasets any accuracy claim is made from, and data/scale* is gitignored.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from ledgerloop.config import load_config
from ledgerloop.eval import harness
from ledgerloop.eval.metrics import compute_metrics
from ledgerloop.exceptions.taxonomy import ReasonCode
from ledgerloop.generate.generator import Generator, write_dataset

# ~2 minutes end to end on one core. 100k is the top of the range because that is
# where a single-process pure-Python run stops being interactive, not because
# anything breaks there -- see the README's scale section.
DEFAULT_SIZES: tuple[int, ...] = (5_000, 20_000, 50_000, 100_000)


@dataclass
class ScalePoint:
    n_transactions: int
    n_bank_lines: int
    generate_seconds: float
    pipeline_seconds: float
    bank_lines_per_sec: float
    transactions_per_sec: float
    auto_match_rate: float
    correct_disposition_rate: float
    precision: float
    false_match_count: int
    tier2_timeouts: int
    exceptions_needing_review: int


def _config_at_size(base: dict, n_transactions: int) -> dict:
    """Scales defect instances with volume so density is held constant. The ratio comes
    from the configured scale profile, not a magic number: whatever
    scale_min_instances_per_defect is to scale_n_gateway_transactions, every size keeps."""
    gen = base["generate"]
    density = gen["scale_min_instances_per_defect"] / gen["scale_n_gateway_transactions"]
    scaled = copy.deepcopy(base)
    scaled["generate"]["n_gateway_transactions"] = n_transactions
    scaled["generate"]["min_instances_per_defect"] = max(
        gen["min_instances_per_defect"], round(n_transactions * density)
    )
    return scaled


def run_point(n_transactions: int, out_root: Path, *, config: dict | None = None) -> ScalePoint:
    base = config if config is not None else load_config()
    sized_config = _config_at_size(base, n_transactions)
    seed = base["generate"]["scale_seed"]
    profile = f"scale_{n_transactions}"

    start = time.perf_counter()
    ds = Generator(seed, sized_config).generate()
    write_dataset(ds, out_root / profile, seed=seed, config=sized_config)
    generate_seconds = time.perf_counter() - start

    # The pipeline runs on the shipped thresholds, not the sized ones -- the point is how
    # the configuration we actually ship behaves at volume.
    run = harness.run(out_root, profile, "full", no_llm=True, config_override=base)
    m = compute_metrics(run, harness.load_answer_key(out_root, profile))

    return ScalePoint(
        n_transactions=len(ds.gateway_transactions),
        n_bank_lines=m.total_records,
        generate_seconds=generate_seconds,
        pipeline_seconds=run.wall_seconds,
        bank_lines_per_sec=m.throughput_records_per_sec,
        transactions_per_sec=len(ds.gateway_transactions) / run.wall_seconds if run.wall_seconds else float("inf"),
        auto_match_rate=m.auto_match_rate,
        correct_disposition_rate=m.correct_disposition_rate,
        precision=m.precision,
        false_match_count=m.false_match_count,
        tier2_timeouts=m.exception_counts_by_reason.get(ReasonCode.TIER2_TIMEOUT, 0),
        exceptions_needing_review=m.exceptions_needing_review,
    )


def run_scale(sizes: tuple[int, ...], out_root: Path, *, config: dict | None = None) -> list[ScalePoint]:
    return [run_point(n, out_root, config=config) for n in sizes]


def format_report(points: list[ScalePoint]) -> str:
    header = (
        f"{'txns':>9}{'bank lines':>12}{'pipeline s':>12}{'lines/sec':>11}{'txns/sec':>10}"
        f"{'auto-match':>12}{'disposition':>13}{'false':>7}{'timeouts':>10}"
    )
    lines = [header, "-" * len(header)]
    for p in points:
        lines.append(
            f"{p.n_transactions:>9,}"
            f"{p.n_bank_lines:>12,}"
            f"{p.pipeline_seconds:>12.1f}"
            f"{p.bank_lines_per_sec:>11,.0f}"
            f"{p.transactions_per_sec:>10,.0f}"
            f"{p.auto_match_rate:>12.1%}"
            f"{p.correct_disposition_rate:>13.1%}"
            f"{p.false_match_count:>7}"
            f"{p.tier2_timeouts:>10}"
        )

    lines.append("")
    if len(points) >= 2:
        first, last = points[0], points[-1]
        volume_factor = last.n_transactions / first.n_transactions
        cost_factor = (
            (last.pipeline_seconds / last.n_bank_lines) / (first.pipeline_seconds / first.n_bank_lines)
            if first.pipeline_seconds and first.n_bank_lines and last.n_bank_lines
            else float("nan")
        )
        lines += [
            f"{volume_factor:.0f}x the transactions costs {cost_factor:.1f}x as much time per bank line.",
            (
                "Per-line cost rising with total volume is the expected shape: tier2's candidate "
                "search widens as more transactions fall inside each date window."
            ),
        ]

    worst_accuracy = min(points, key=lambda p: p.correct_disposition_rate)
    total_false = sum(p.false_match_count for p in points)
    total_timeouts = sum(p.tier2_timeouts for p in points)
    lines += [
        "",
        (
            f"accuracy floor across all sizes: {worst_accuracy.correct_disposition_rate:.1%} correct "
            f"disposition at {worst_accuracy.n_transactions:,} transactions"
        ),
        f"false matches across all sizes: {total_false}",
        (
            f"tier2 search timeouts: {total_timeouts}"
            + (
                "  <- raise tier2.subset_sum_node_budget for batches this size"
                if total_timeouts
                else "  (the node budget held at every size tested)"
            )
        ),
    ]
    return "\n".join(lines)


@click.command()
@click.option(
    "--sizes",
    default=",".join(str(n) for n in DEFAULT_SIZES),
    help="Comma-separated gateway-transaction counts to benchmark.",
)
@click.option(
    "--out-root",
    type=click.Path(path_type=Path),
    default=Path("data"),
    help="Where the generated benchmark datasets go. data/scale* is gitignored.",
)
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(sizes: str, out_root: Path, runs_root: Path) -> None:
    """Generate and run the pipeline at several volumes; report the scaling curve."""
    parsed = tuple(int(s.strip()) for s in sizes.split(",") if s.strip())
    points = run_scale(parsed, out_root)
    click.echo(format_report(points))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / "scale_report.json"
    out_path.write_text(json.dumps([asdict(p) for p in points], indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
