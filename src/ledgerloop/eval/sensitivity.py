"""eval/sensitivity.py -- the tuning claim, measured instead of asserted.

The README says the pipeline is deliberately tuned to escalate rather than guess. That
is a claim about numbers in config.yaml that nobody has been shown. This module sweeps
each Tier 2 threshold across its usable range and reports what every setting would have
cost or bought, so a reader can judge the operating point instead of taking it on faith.

What a sweep is looking for is the value at which false matches first appear. Looser
than that, the knob only trades escalations for auto-matches; at and past it, the
pipeline starts posting money against the wrong transactions. The distance between the
shipped value and that boundary is the safety margin, and it is the number worth
arguing about.

Running it changed what we believe. Two of the three knobs do not govern correctness on
this data at all: min_resolve_score can be dropped from 0.70 to 0.00 and
ambiguity_margin raised tenfold without producing a single wrong match, because what
actually prevents false matches upstream is candidate *generation* -- a candidate set
that never contains a wrong grouping cannot be mis-scored into one. The knob that does
bind is amount_tolerance_paise, and the margin there is roughly 2500x. Reporting the
flattering single-knob curve and calling it "the safety argument" would have been the
easy version; this is the accurate one. See the README's sensitivity section.

Deterministic by default (--no-llm): every row is a re-run of tiers 1+2 over identical
inputs with one value changed, so the difference between two rows is attributable to
that value and nothing else.

Not swept here: tier3.confidence_threshold. Sweeping it means re-adjudicating every
ambiguous case against a live model once per point -- the adjudicator records its
decisions but not the raw per-candidate confidences a post-hoc sweep would need, so
there is no way to do it from stored results. Doing it live would also mean repeatedly
scoring the holdout, which the project's once-only rule forbids. Reporting a curve we
cannot compute honestly would be worse than saying so here.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import click

from ledgerloop.config import load_config
from ledgerloop.eval import harness
from ledgerloop.eval.metrics import compute_metrics

Number = float | int


@dataclass(frozen=True)
class Knob:
    """One config threshold and the range to sweep it over. `looser_is` records which
    direction relaxes the constraint, so the search for the first breaking value scans
    from most permissive to most conservative regardless of which way the knob runs."""

    section: str
    key: str
    points: tuple[Number, ...]
    looser_is: Literal["lower", "higher"]
    what_it_controls: str


KNOBS: tuple[Knob, ...] = (
    Knob(
        section="tier2",
        key="min_resolve_score",
        # Deliberately extends below confidence.BASE_GENERIC_SUBSET_SUM (0.55), the score
        # given to a cross-batch subset-sum match with no UTR or narration grounding at
        # all, and on down to 0.0 -- "auto-resolve the best candidate no matter how weak".
        # A sweep that stopped at the shipped value's neighbourhood would have shown a
        # tidy curve and missed that this knob does not bind at all.
        points=(0.0, 0.25, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90),
        looser_is="lower",
        what_it_controls="how strong a candidate's score must be before tier2 resolves it outright",
    ),
    Knob(
        section="tier2",
        key="amount_tolerance_paise",
        # 0 paise (exact to the rupee) through INR 5,000 of slack. The top of this range
        # is absurd for a fee-rounding tolerance, which is the point: it is where the
        # system is meant to break, and it does.
        points=(0, 100, 200, 1_000, 5_000, 50_000, 500_000),
        looser_is="higher",
        what_it_controls="how far a bank credit may differ from a candidate's expected net and still match",
    ),
    Knob(
        section="tier2",
        key="date_window_days",
        points=(1, 2, 3, 5, 10, 30),
        looser_is="higher",
        what_it_controls="how many days either side of the settlement date a bank credit may land",
    ),
    Knob(
        section="tier2",
        key="ambiguity_margin",
        # 0.0 means "never call two candidates tied, always take the top score" -- the
        # setting most likely to resolve a DUPLICATE-class case to the wrong one of two
        # equally valid transactions. It doesn't, which is itself the finding.
        points=(0.0, 0.01, 0.05, 0.15, 0.50),
        looser_is="lower",
        what_it_controls="how close two candidate scores must be before the case is treated as tied and escalated",
    ),
)


@dataclass
class SensitivityRow:
    knob: str
    value: Number
    is_operating_point: bool
    resolved_count: int
    auto_match_rate: float
    correct_disposition_rate: float
    precision: float
    recall: float
    false_match_rate: float
    false_match_count: int
    exceptions_needing_review: int


def _fmt(value: Number) -> str:
    return f"{value:g}"


def run_sweep(
    data_root: Path,
    profile: str,
    knob: Knob,
    *,
    no_llm: bool = True,
    config: dict | None = None,
) -> list[SensitivityRow]:
    base_config = config if config is not None else load_config()
    shipped_value = base_config[knob.section][knob.key]
    answer_key = harness.load_answer_key(data_root, profile)

    rows: list[SensitivityRow] = []
    for value in knob.points:
        swept = copy.deepcopy(base_config)
        swept[knob.section][knob.key] = value
        run = harness.run(data_root, profile, "full", no_llm=no_llm, config_override=swept)
        m = compute_metrics(run, answer_key)
        rows.append(
            SensitivityRow(
                knob=f"{knob.section}.{knob.key}",
                value=value,
                is_operating_point=value == shipped_value,
                resolved_count=m.resolved_count,
                auto_match_rate=m.auto_match_rate,
                correct_disposition_rate=m.correct_disposition_rate,
                precision=m.precision,
                recall=m.recall,
                false_match_rate=m.false_match_rate,
                false_match_count=m.false_match_count,
                exceptions_needing_review=m.exceptions_needing_review,
            )
        )
    return rows


def run_all(
    data_root: Path,
    profile: str,
    *,
    knobs: tuple[Knob, ...] = KNOBS,
    no_llm: bool = True,
    config: dict | None = None,
) -> dict[str, list[SensitivityRow]]:
    return {
        f"{knob.section}.{knob.key}": run_sweep(data_root, profile, knob, no_llm=no_llm, config=config)
        for knob in knobs
    }


def first_unsafe_row(rows: list[SensitivityRow], knob: Knob) -> SensitivityRow | None:
    """Scanning from the most permissive setting inward, the first row that posts a
    wrong match. None means this knob never breaks anywhere in its swept range -- which
    is a finding about the knob, not a clean bill of health for the pipeline."""
    ordered = sorted(rows, key=lambda r: r.value, reverse=knob.looser_is == "higher")
    return next((row for row in ordered if row.false_match_count > 0), None)


def format_knob_table(rows: list[SensitivityRow], knob: Knob) -> str:
    title = f"{knob.section}.{knob.key} -- {knob.what_it_controls}"
    header = (
        f"{'value':<14}{'resolved':>10}{'auto-match':>12}{'disposition':>13}"
        f"{'precision':>11}{'false-match':>13}{'review queue':>14}"
    )
    lines = [title, "-" * len(header), header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{_fmt(row.value):<14}"
            f"{row.resolved_count:>10}"
            f"{row.auto_match_rate:>12.1%}"
            f"{row.correct_disposition_rate:>13.1%}"
            f"{row.precision:>11.1%}"
            f"{row.false_match_rate:>13.2%}"
            f"{row.exceptions_needing_review:>14}"
            f"{'  <- shipped' if row.is_operating_point else ''}"
        )

    shipped = next((r for r in rows if r.is_operating_point), None)
    unsafe = first_unsafe_row(rows, knob)
    lines.append("")
    if unsafe is None:
        lines.append(
            "  no swept value produced a false match: this knob does not govern correctness "
            "on this data, it only trades auto-matches against escalations."
        )
    elif shipped is not None:
        lines.append(
            f"  breaks at {_fmt(unsafe.value)}: {unsafe.false_match_count} false match(es), "
            f"precision {unsafe.precision:.1%}"
        )
        lines.append(f"  {_describe_margin(shipped.value, unsafe.value, knob)}")

    if shipped is not None:
        loosest = min(rows, key=lambda r: r.value if knob.looser_is == "lower" else -r.value)
        tightest = max(rows, key=lambda r: r.value if knob.looser_is == "lower" else -r.value)
        lines += [
            (
                f"  loosening to {_fmt(loosest.value)}: "
                f"{loosest.resolved_count - shipped.resolved_count:+d} auto-matched, "
                f"{loosest.false_match_count - shipped.false_match_count:+d} false match(es)"
            ),
            (
                f"  tightening to {_fmt(tightest.value)}: "
                f"{tightest.resolved_count - shipped.resolved_count:+d} auto-matched, "
                f"{tightest.exceptions_needing_review - shipped.exceptions_needing_review:+d} more to review by hand"
            ),
        ]
    return "\n".join(lines)


def _describe_margin(shipped: Number, unsafe: Number, knob: Knob) -> str:
    if knob.looser_is == "higher" and shipped:
        return f"shipped at {_fmt(shipped)} -- a margin of {unsafe / shipped:.0f}x below the breaking point."
    return f"shipped at {_fmt(shipped)} -- a margin of {abs(shipped - unsafe):g} from the breaking point."


def format_report(results: dict[str, list[SensitivityRow]], knobs: tuple[Knob, ...] = KNOBS) -> str:
    by_name = {f"{k.section}.{k.key}": k for k in knobs}
    blocks = [format_knob_table(rows, by_name[name]) for name, rows in results.items()]

    binding = [name for name, rows in results.items() if first_unsafe_row(rows, by_name[name]) is not None]
    inert = [name for name in results if name not in binding]

    summary = ["", "=" * 78, "which knob actually governs safety?"]
    if binding:
        summary.append(f"  binding (a swept value produced a wrong match): {', '.join(binding)}")
    if inert:
        summary.append(f"  inert across the whole swept range: {', '.join(inert)}")
        summary.append(
            "  An inert knob is not a safety feature. What prevents a false match upstream of "
            "it is candidate generation: a candidate set that never contains a wrong grouping "
            "cannot be mis-scored into one, whatever the score threshold is set to."
        )
    return "\n\n".join(blocks) + "\n" + "\n".join(summary)


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout", "scale"]), default="dev")
@click.option(
    "--no-llm/--llm",
    "no_llm",
    default=True,
    help="Deterministic by default: a sweep should isolate the threshold, not mix in model variance.",
)
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(profile: str, no_llm: bool, data_root: Path, runs_root: Path) -> None:
    """Sweep each tier2 threshold and report the resulting trade-off curves."""
    results = run_all(data_root, profile, no_llm=no_llm)
    click.echo(format_report(results))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / f"{profile}_sensitivity.json"
    out_path.write_text(
        json.dumps({name: [asdict(r) for r in rows] for name, rows in results.items()}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    click.echo(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
