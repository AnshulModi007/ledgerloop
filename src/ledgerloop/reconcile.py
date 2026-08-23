"""End-to-end tiers 1+2+3+4 over one data profile: matching, adjudication, exception
classification, and journal posting proposals. This is what `--no-llm` names per
IMPLEMENTATION.md section 4's Phase 3 acceptance criterion ("`--no-llm` completes end
to end") -- with no LLM keys configured, `--no-llm` is also simply the default
behaviour, since resolve_chain() falls through to NullProvider on its own.

`--approve` demonstrates the idempotency guarantee live, ahead of Phase 6's UI: it
persists this run's proposed postings as approved, and a second `--approve` run over
identical data then reports zero new postings -- the same beat the eventual dashboard
re-run button is built around.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click

from ledgerloop.adjudicate import adjudicator
from ledgerloop.adjudicate.provider import resolve_chain
from ledgerloop.audit.log import AuditLog
from ledgerloop.config import load_config
from ledgerloop.exceptions import taxonomy
from ledgerloop.ingest.normalise import load_and_normalise
from ledgerloop.ledger import journal
from ledgerloop.match import tier1_exact, tier2_algorithmic


def _load_approved_keys(path: Path) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")))
    return set()


def _save_approved_keys(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")


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
    config = load_config(config_path) if config_path else load_config()
    normalised = load_and_normalise(data_root / profile)

    tier2_result = tier2_algorithmic.run(normalised, config)
    chain = resolve_chain(no_llm=no_llm)
    tier3_result = adjudicator.run(normalised, tier2_result, config, chain)
    all_resolutions = tier2_result.resolutions + tier3_result.resolutions

    batches = tier1_exact.build_batches(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)
    bank_line_by_id = {b.bank_line_id: b for b in normalised.bank_lines}
    claimed_batch_ids = {
        r.evidence["settlement_batch_id"] for r in all_resolutions if "settlement_batch_id" in r.evidence
    }
    claimed_txn_ids = {t for r in all_resolutions for t in r.matched_txn_ids}
    unclaimed_gross = [
        t.gross_amount_paise for t in normalised.gateway_transactions if t.txn_id not in claimed_txn_ids
    ]

    exceptions = taxonomy.classify_all(
        bank_line_by_id,
        tier3_result.unresolved,
        by_utr=by_utr,
        claimed_batch_ids=claimed_batch_ids,
        unclaimed_gross_amounts_paise=unclaimed_gross,
        tier2_cfg=config["tier2"],
    )

    settlement_lines_by_txn = {line.txn_id: line for line in normalised.settlement_lines}
    journal_batches = journal.propose_postings(all_resolutions, settlement_lines_by_txn, bank_line_by_id)
    all_postings = [p for batch in journal_batches for p in batch.postings]

    approved_store = runs_root / f"{profile}_approved_postings.json"
    approved_keys = _load_approved_keys(approved_store)
    new_postings = [p for p in all_postings if p.idempotency_key not in approved_keys]

    total = len(normalised.bank_lines)
    tier_counts = Counter(r.resolved_by for r in all_resolutions)
    reason_counts = Counter(e.reason_code for e in exceptions)

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
    click.echo(f"exceptions: {len(exceptions)}")
    for reason, count in sorted(reason_counts.items()):
        click.echo(f"  {reason}: {count}")
    click.echo(f"llm provider: {banner}")
    click.echo(f"llm calls made: {tier3_result.llm_calls_made}")
    click.echo(
        f"journal postings proposed: {len(all_postings)} "
        f"({len(new_postings)} new, {len(all_postings) - len(new_postings)} already approved)"
    )

    AuditLog(runs_root / f"{profile}_audit.jsonl").append_run(
        resolutions=all_resolutions, exceptions=exceptions, config=config
    )

    if approve:
        approved_keys.update(p.idempotency_key for p in all_postings)
        _save_approved_keys(approved_store, approved_keys)
        click.echo(f"approved: {len(new_postings)} new postings recorded to {approved_store}")
    else:
        click.echo("(pass --approve to persist these postings; a second --approve run then shows zero new postings)")


if __name__ == "__main__":
    main()
