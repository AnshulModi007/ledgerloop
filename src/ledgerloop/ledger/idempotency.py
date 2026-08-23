"""Every posting carries a deterministic key derived from (batch_id, source_ids,
posting_type). Re-running an already-approved batch must produce zero new postings --
this module is the whole mechanism for that guarantee. See IMPLEMENTATION.md section
4 and `ledger/journal.py`, which is the only caller of `posting_key`.

"batch_id" here is the bank_line_id: it's the one identifier every Resolution always
has exactly one of, regardless of whether its matched transactions come from a single
settlement batch, a subset of one (SPLIT_1N), or -- in the rarer cross-batch
subset-sum case -- more than one. settlement_batch_id can't play this role uniformly,
so bank_line_id is what "this batch of reconciliation work" means throughout ledger/.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ledgerloop.ledger.journal import Posting


def posting_key(batch_id: str, source_ids: list[str], posting_type: str) -> str:
    canonical = f"{batch_id}|{','.join(sorted(source_ids))}|{posting_type}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def filter_new_postings(postings: list[Posting], already_posted_keys: set[str]) -> list[Posting]:
    return [p for p in postings if p.idempotency_key not in already_posted_keys]
