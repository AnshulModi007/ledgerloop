"""LedgerLoop top-level CLI. Subcommands are added as each build phase lands."""

from __future__ import annotations

import click

from ledgerloop.generate.generator import main as generate_main
from ledgerloop.reconcile import main as reconcile_main


@click.group()
def main() -> None:
    """LedgerLoop -- multi-source settlement reconciliation agent."""


main.add_command(generate_main, name="generate")
main.add_command(reconcile_main, name="reconcile")


if __name__ == "__main__":
    main()
