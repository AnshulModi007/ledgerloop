"""eval/generalization.py -- scoring the suite of defect shapes nobody designed for.

generate/novel.py builds bank lines whose failure modes are absent from the taxonomy the
matching tiers were written against. This module runs the ordinary pipeline over them and
scores the only question that matters on unfamiliar input:

    Did it stay safe, or did it guess?

Three outcomes per line, and only one of them is a failure:

  matched correctly   the pipeline generalized -- a bonus, never the pass criterion
  escalated, typed    the right answer on input it does not understand
  MATCHED WRONGLY     the failure. Money posted against the wrong transactions.

A fourth would be worse still: a line appearing in neither the resolutions nor the
exceptions, silently dropped. The taxonomy exists to make that impossible, so this module
checks it rather than assuming it.

The gate is therefore *not* an accuracy floor. Escalating all 40 novel lines would pass,
and it should: refusing unfamiliar work is the designed behaviour. What fails the build is
a single wrong match, on the argument this project has made throughout -- that in
reconciliation a false positive is materially worse than an escalation, and a system whose
safety depends on having anticipated every defect shape has not actually been shown to be
safe at all.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from ledgerloop.config import load_config
from ledgerloop.eval import harness
from ledgerloop.eval.metrics import compute_metrics
from ledgerloop.generate.novel import (
    SHAPE_DESCRIPTIONS,
    SHAPE_EXPECTS_MATCH,
    NovelShape,
    generate_novel,
)


@dataclass
class ShapeResult:
    shape: str
    expects_match: bool
    total: int
    matched_correctly: int
    matched_wrongly: int
    escalated: int
    reason_codes: dict[str, int]


@dataclass
class GeneralizationReport:
    shapes: list[ShapeResult]
    control_lines: int
    control_matched: int
    novel_lines: int
    novel_matched_wrongly: int
    silently_dropped: list[str]
    passed: bool


def score(run: harness.HarnessRun, answer_key: dict) -> GeneralizationReport:
    m = compute_metrics(run, answer_key)
    resolved_ids = {r.bank_line_id for r in run.resolutions}
    exception_ids = {e.bank_line_id for e in run.exceptions}
    reason_by_line = {e.bank_line_id: e.reason_code for e in run.exceptions}

    # Every bank line must be accounted for by exactly one of the two outcomes.
    silently_dropped = sorted(set(answer_key) - resolved_ids - exception_ids)

    shapes: list[ShapeResult] = []
    for shape in NovelShape:
        stats = m.per_defect.get(shape.value)
        if stats is None:
            continue
        lines = [bid for bid, entry in answer_key.items() if shape.value in entry.defect_classes]
        codes: dict[str, int] = {}
        for bid in lines:
            if bid in reason_by_line:
                codes[reason_by_line[bid]] = codes.get(reason_by_line[bid], 0) + 1
        shapes.append(
            ShapeResult(
                shape=shape.value,
                expects_match=SHAPE_EXPECTS_MATCH[shape],
                total=stats.total,
                matched_correctly=stats.correctly_matched,
                matched_wrongly=stats.false_matched,
                # A correct refusal and a miss are the same event here: the line was
                # escalated. Which of the two it is depends only on whether ground truth
                # says a match existed, and the suite does not grade that.
                escalated=stats.correctly_rejected + stats.missed,
                reason_codes=dict(sorted(codes.items())),
            )
        )

    control = m.per_defect.get("CLEAN")
    novel_wrong = sum(s.matched_wrongly for s in shapes)
    return GeneralizationReport(
        shapes=shapes,
        control_lines=control.total if control else 0,
        control_matched=control.correctly_matched if control else 0,
        novel_lines=sum(s.total for s in shapes),
        novel_matched_wrongly=novel_wrong,
        silently_dropped=silently_dropped,
        passed=novel_wrong == 0 and not silently_dropped,
    )


def format_report(report: GeneralizationReport) -> str:
    header = f"{'novel shape':<24}{'n':>4}{'matched':>9}{'WRONG':>7}{'escalated':>11}  {'reason codes'}"
    lines = [
        "Defect shapes the matcher was never designed for.",
        "Pass = no wrong match and nothing silently dropped. Escalating is a correct answer.",
        "",
        header,
        "-" * (len(header) + 24),
    ]
    for s in report.shapes:
        codes = ", ".join(f"{code} x{n}" for code, n in s.reason_codes.items()) or "-"
        lines.append(
            f"{s.shape:<24}{s.total:>4}{s.matched_correctly:>9}{s.matched_wrongly:>7}{s.escalated:>11}  {codes}"
        )

    lines += [
        "",
        (
            f"control (CLEAN, in-distribution): {report.control_matched}/{report.control_lines} matched -- "
            "present so escalating everything cannot pass"
        ),
    ]
    if report.silently_dropped:
        lines.append(f"SILENTLY DROPPED (neither resolved nor escalated): {', '.join(report.silently_dropped)}")
    else:
        lines.append("silently dropped: none -- every line is either resolved or carries a typed reason code")

    lines += ["", "=" * 78]
    if report.passed:
        lines.append(
            f"PASS: {report.novel_lines} lines of unfamiliar defect shapes, 0 matched wrongly. "
            "On input it was never designed for, the pipeline escalated rather than guessed."
        )
    else:
        lines.append(
            f"FAIL: {report.novel_matched_wrongly} of {report.novel_lines} novel lines were matched "
            "WRONGLY. The refuse-rather-than-guess claim does not hold on unfamiliar input."
        )
    lines.append("")
    for s in report.shapes:
        lines.append(f"  {s.shape}: {SHAPE_DESCRIPTIONS[NovelShape(s.shape)]}")
    return "\n".join(lines)


def run(data_root: Path, *, no_llm: bool = True, config: dict | None = None) -> GeneralizationReport:
    cfg = config if config is not None else load_config()
    if not (data_root / "novel" / "bank_statement.csv").exists():
        generate_novel(cfg, data_root)
    run_result = harness.run(data_root, "novel", "full", no_llm=no_llm, config_override=cfg)
    return score(run_result, harness.load_answer_key(data_root, "novel"))


@click.command()
@click.option("--no-llm/--llm", "no_llm", default=True, help="Deterministic tiers only by default.")
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--runs-root", type=click.Path(path_type=Path), default=Path("runs"))
def main(no_llm: bool, data_root: Path, runs_root: Path) -> None:
    """Run the generalization suite and report whether the pipeline stayed safe."""
    report = run(data_root, no_llm=no_llm)
    click.echo(format_report(report))

    runs_root.mkdir(parents=True, exist_ok=True)
    out_path = runs_root / "novel_generalization.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"\nwritten: {out_path}")

    if not report.passed:
        raise SystemExit("GATE FAILED: a defect shape the matcher never saw was matched wrongly")


if __name__ == "__main__":
    main()
