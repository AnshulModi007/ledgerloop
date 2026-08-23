"""LedgerLoop top-level CLI. Subcommands are added as each build phase lands."""

from __future__ import annotations

import click

from ledgerloop.generate.generator import main as generate_main


@click.group()
def main() -> None:
    """LedgerLoop -- multi-source settlement reconciliation agent."""


main.add_command(generate_main, name="generate")


if __name__ == "__main__":
    main()
