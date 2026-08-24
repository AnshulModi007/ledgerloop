"""eval/calibration.py -- IMPLEMENTATION.md section 4 (Phase 5).

Bins every tier3 resolution (LLM candidate adjudication and LLM-assisted narration
extraction alike -- both resolved_by=="tier3") by its own stated confidence and
compares that against how often it was actually correct against ground truth. If the
model says 0.9 and is right 60% of the time, this reports that plainly rather than
hiding it -- reporting miscalibration honestly is worth more than a flattering number.

Bins only cover confidence values that were actually observed. Because
tier3.confidence_threshold currently gates every adjudication decision at 0.85 before
it ever becomes a Resolution, most bins below that will be empty on a real run --
that's expected, not a bug: sub-threshold selections are converted to LOW_CONFIDENCE
abstains upstream and never reach this module. See adjudicate/adjudicator.py's
_to_resolution.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from ledgerloop.eval import harness
from ledgerloop.eval.harness import is_correct_resolution
from ledgerloop.generate.schemas import AnswerKeyEntry
from ledgerloop.schemas import Resolution

BIN_WIDTH = 0.1
MISCALIBRATION_GAP_THRESHOLD = 0.15


@dataclass
class CalibrationBin:
    range_low: float
    range_high: float
    n: int
    mean_stated_confidence: float
    actual_accuracy: float
    gap: float  # stated - actual; positive means overconfident


def _bin_bounds(confidence: float) -> tuple[float, float]:
    idx = min(int(confidence / BIN_WIDTH), int(1.0 / BIN_WIDTH) - 1)
    return round(idx * BIN_WIDTH, 2), round((idx + 1) * BIN_WIDTH, 2)


def compute_calibration(
    resolutions: list[Resolution], answer_key: dict[str, AnswerKeyEntry]
) -> list[CalibrationBin]:
    tier3 = [r for r in resolutions if r.resolved_by == "tier3"]
    buckets: dict[tuple[float, float], list[Resolution]] = {}
    for r in tier3:
        buckets.setdefault(_bin_bounds(r.confidence), []).append(r)

    bins: list[CalibrationBin] = []
    for (lo, hi), items in sorted(buckets.items()):
        n = len(items)
        mean_conf = sum(r.confidence for r in items) / n
        correct = sum(
            1
            for r in items
            if r.bank_line_id in answer_key and is_correct_resolution(r, answer_key[r.bank_line_id])
        )
        accuracy = correct / n
        bins.append(
            CalibrationBin(
                range_low=lo, range_high=hi, n=n, mean_stated_confidence=mean_conf,
                actual_accuracy=accuracy, gap=mean_conf - accuracy,
            )
        )
    return bins


def format_report(bins: list[CalibrationBin]) -> str:
    if not bins:
        return "no tier3 resolutions this run -- nothing to calibrate (e.g. --no-llm, or nothing reached tier3)."

    header = f"{'confidence bin':<18}{'n':>6}{'mean stated':>14}{'actual accuracy':>18}{'gap':>10}"
    lines = ["reliability table (stated confidence vs. actual accuracy):", header, "-" * len(header)]
    worst_gap = 0.0
    for b in bins:
        flag = " <- MISCALIBRATED" if abs(b.gap) >= MISCALIBRATION_GAP_THRESHOLD else ""
        lines.append(
            f"[{b.range_low:.2f}, {b.range_high:.2f})".ljust(18)
            + f"{b.n:>6}{b.mean_stated_confidence:>14.1%}{b.actual_accuracy:>18.1%}{b.gap:>+10.1%}"
            + flag
        )
        if abs(b.gap) > abs(worst_gap):
            worst_gap = b.gap

    lines.append("")
    if abs(worst_gap) < MISCALIBRATION_GAP_THRESHOLD:
        lines.append(f"assessment: reasonably calibrated (worst bin gap {worst_gap:+.1%}).")
    elif worst_gap > 0:
        lines.append(
            f"assessment: OVERCONFIDENT in at least one bin (gap {worst_gap:+.1%}) -- "
            "stated confidence overstates actual accuracy; consider raising "
            "tier3.confidence_threshold in config.yaml."
        )
    else:
        lines.append(
            f"assessment: UNDERCONFIDENT in at least one bin (gap {worst_gap:+.1%}) -- "
            "actual accuracy exceeds stated confidence; the threshold could likely be "
            "lowered to resolve more without raising the false-match rate, but verify "
            "against the false-match gate before changing it."
        )
    return "\n".join(lines)


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout"]), default="dev")
@click.option("--no-llm", "no_llm", is_flag=True, default=False)
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(profile: str, no_llm: bool, data_root: Path, runs_root: Path) -> None:
    run = harness.run(data_root, profile, "full", no_llm=no_llm)
    answer_key = harness.load_answer_key(data_root, profile)
    bins = compute_calibration(run.resolutions, answer_key)

    click.echo(format_report(bins))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / f"{profile}_calibration.json"
    out_path.write_text(json.dumps([asdict(b) for b in bins], indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
