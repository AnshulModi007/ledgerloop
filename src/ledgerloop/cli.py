"""LedgerLoop top-level CLI. Subcommands are added as each build phase lands."""

from __future__ import annotations

import click

from ledgerloop.eval.ablation import main as ablation_main
from ledgerloop.eval.calibration import main as calibration_main
from ledgerloop.eval.metrics import main as eval_metrics_main
from ledgerloop.generate.generator import main as generate_main
from ledgerloop.reconcile import main as reconcile_main


@click.group()
def main() -> None:
    """LedgerLoop -- multi-source settlement reconciliation agent."""


@click.group(name="eval")
def eval_group() -> None:
    """Phase 5 evaluation harness: metrics, ablation, calibration."""


eval_group.add_command(eval_metrics_main, name="metrics")
eval_group.add_command(ablation_main, name="ablation")
eval_group.add_command(calibration_main, name="calibration")

main.add_command(generate_main, name="generate")
main.add_command(reconcile_main, name="reconcile")
main.add_command(eval_group)


if __name__ == "__main__":
    main()
