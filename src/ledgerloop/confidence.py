"""Confidence/plausibility scores for tier2 candidates -- values in [0, 1], not money.

Deliberately kept outside match/ and ledger/: tests/test_money.py bans float literals
there so paise arithmetic can never accidentally go through float rounding. Confidence
scores are a different kind of number entirely and are free to be float.
"""

from __future__ import annotations

# Base score per matching strategy, before amount/date fit is factored in. Reflects how
# strong the underlying evidence is. See match/tier2_algorithmic.py.
BASE_EXACT_UTR = 1.0
BASE_TRANSPOSED_UTR = 0.9
BASE_PARTITION_SEARCH = 0.85
BASE_AMOUNT_DATE_FALLBACK = 0.78
# Deliberately below tier2.min_resolve_score: a cross-batch subset-sum match has no
# UTR or narration grounding at all, so it should never auto-resolve -- only ever
# surface as a candidate for tier3 (Phase 3) or the exception queue (Phase 4).
BASE_GENERIC_SUBSET_SUM = 0.55

# tier3 (adjudicate/adjudicator.py): an LLM-extracted UTR that then survives the same
# exact-join criteria tier1/tier2 apply. Scored a notch below TRANSPOSED_UTR -- the
# join itself is just as exact, but the UTR came from a model's read of free text
# rather than a bounded, deterministic transformation of a regex-found one.
BASE_LLM_EXTRACTED_UTR = 0.85


def score(*, base: float, diff: int, tolerance: int, lag: int, window: int) -> float:
    amount_penalty = min(1.0, diff / max(tolerance, 1)) * 0.2
    date_penalty = min(1.0, max(lag, 0) / max(window, 1)) * 0.15
    return round(max(0.0, base - amount_penalty - date_penalty), 4)
