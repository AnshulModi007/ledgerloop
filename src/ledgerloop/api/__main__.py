"""`python -m ledgerloop.api` -- serves the API and the dashboard on one port."""

from __future__ import annotations

import click

from ledgerloop.api.app import main


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--log-level", default="info", show_default=True)
def cli(host: str, port: int, log_level: str) -> None:
    click.echo(f"LedgerLoop dashboard: http://{host}:{port}")
    click.echo(f"API docs:             http://{host}:{port}/docs")
    main(host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    cli()
