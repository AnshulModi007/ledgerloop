"""End-to-end tiers 1+2+3 over one data profile. This is what `--no-llm` names per
IMPLEMENTATION.md section 4's Phase 3 acceptance criterion ("`--no-llm` completes end
to end") -- with no LLM keys configured, `--no-llm` is also simply the default
behaviour, since resolve_chain() falls through to NullProvider on its own.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import click

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import resolve_chain
from ledgerloop.config import load_config
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.match import tier2_algorithmic


@click.command()
@click.option("--profile", type=click.Choice(["dev", "holdout"]), default="dev", help="Which data profile to reconcile.")
@click.option(
    "--no-llm",
    "no_llm",
    is_flag=True,
    default=False,
    help="Force NullProvider even if LLM keys are configured -- tier3 abstains on everything.",
)
@click.option("--data-root", type=click.Path(path_type=Path), default=Path("data"))
@click.option("--config-path", type=click.Path(exists=True, path_type=Path), default=None)
def main(profile: str, no_llm: bool, data_root: Path, config_path: Path | None) -> None:
    config = load_config(config_path) if config_path else load_config()
    normalised = load_and_normalise(data_root / profile)

    tier2_result = tier2_algorithmic.run(normalised, config)
    chain = resolve_chain(no_llm=no_llm)
    tier3_result = adjudicator.run(normalised, tier2_result, config, chain)

    all_resolutions = tier2_result.resolutions + tier3_result.resolutions
    total = len(normalised.bank_lines)
    tier_counts = Counter(r.resolved_by for r in all_resolutions)

    if tier3_result.providers_used:
        banner = ", ".join(sorted(set(tier3_result.providers_used)))
    elif tier3_result.llm_available:
        banner = "configured, but no successful call this run"
    else:
        banner = "none -- deterministic only"

    click.echo(f"profile: {profile}")
    click.echo(f"resolved: {len(all_resolutions)}/{total} ({len(all_resolutions) / total:.1%})")
    click.echo(
        f"  tier1: {tier_counts.get('tier1', 0)}  "
        f"tier2: {tier_counts.get('tier2', 0)}  "
        f"tier3: {tier_counts.get('tier3', 0)}"
    )
    click.echo(f"unresolved: {len(tier3_result.unresolved)}")
    click.echo(f"llm provider: {banner}")
    click.echo(f"llm calls made: {tier3_result.llm_calls_made}")


if __name__ == "__main__":
    main()
