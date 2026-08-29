"""CLI wrapper around pipeline.run() -- tiers 1+2+3 matching, adjudication, exception
classification, and journal posting proposals. This is what `--no-llm` names per
IMPLEMENTATION.md section 4's Phase 3 acceptance criterion ("`--no-llm` completes end
to end") -- with no LLM keys configured, `--no-llm` is also simply the default
behaviour, since resolve_chain() falls through to NullProvider on its own.

`--approve` demonstrates the idempotency guarantee live, ahead of Phase 6's UI: it
persists this run's proposed postings as approved, and a second `--approve` run over
identical data then reports zero new postings -- the same beat the dashboard's
re-run button is built around (see ui/app.py, which drives the identical
pipeline.run()/pipeline.approve() functions this CLI does).
"""

from __future__ import annotations

from pathlib import Path

import click

from ledgerloop import pipeline


@click.command()
@click.option(
    "--profile",
    type=click.Choice(["dev", "holdout", "novel"]),
    default="dev",
    help="Which data profile to reconcile. 'novel' is the generalization suite (generate/novel.py).",
)
@click.option(
    "--no-llm",
    "no_llm",
    is_flag=True,
    default=False,
    help="Force NullProvider even if LLM keys are configured -- tier3 abstains on everything.",
)
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--config-path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--runs-root",
    type=click.Path(path_type=Path),
    default=Path("runs"),
    help="Where the audit log and approved-postings store live.",
)
@click.option(
    "--approve",
    "approve",
    is_flag=True,
    default=False,
    help="Persist this run's proposed postings as approved -- a second --approve run then shows zero new postings.",
)
def main(
    profile: str, no_llm: bool, data_root: Path, config_path: Path | None, runs_root: Path, approve: bool
) -> None:
    run = pipeline.run(data_root, profile, runs_root, no_llm=no_llm, config_path=config_path)

    if run.providers_used:
        banner = ", ".join(sorted(set(run.providers_used)))
    elif run.llm_available:
        banner = "configured, but no successful call this run"
    else:
        banner = "none -- deterministic only"

    click.echo(f"profile: {profile}")
    click.echo(f"resolved: {len(run.resolutions)}/{run.total_records} ({len(run.resolutions) / run.total_records:.1%})")
    click.echo(
        f"  tier1: {run.tier_counts.get('tier1', 0)}  "
        f"tier2: {run.tier_counts.get('tier2', 0)}  "
        f"tier3: {run.tier_counts.get('tier3', 0)}"
    )
    click.echo(f"exceptions: {len(run.exceptions)}")
    for reason, count in sorted(run.reason_counts.items()):
        click.echo(f"  {reason}: {count}")
    click.echo(f"llm provider: {banner}")
    click.echo(f"llm calls made: {run.llm_calls_made}")
    click.echo(
        f"journal postings proposed: {len(run.all_postings)} "
        f"({len(run.new_postings)} new, {len(run.all_postings) - len(run.new_postings)} already approved)"
    )

    if run.duplicate_receivable_relief:
        click.echo(
            f"LEDGER CONTROL: {len(run.duplicate_receivable_relief)} transaction(s) had their "
            "settlement receivable cleared by more than one bank line -- each batch balances, "
            "but the receivable is relieved twice. Needs a human:"
        )
        for txn_id, bank_line_ids in run.duplicate_receivable_relief.items():
            click.echo(f"  {txn_id}: cleared by {', '.join(bank_line_ids)}")

    if approve:
        new_count = pipeline.approve(runs_root, run)
        click.echo(f"approved: {new_count} new postings recorded to {runs_root / f'{profile}_approved_postings.json'}")
    else:
        click.echo("(pass --approve to persist these postings; a second --approve run then shows zero new postings)")


if __name__ == "__main__":
    main()
