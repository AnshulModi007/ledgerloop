"""Every unresolved item gets a typed reason code, never a silent drop.

tier1/tier2 already distinguish several of these directly (TIER2_TIMEOUT,
TIER3_INVALID_SELECTION, AMBIGUOUS_CANDIDATES, LOW_CONFIDENCE -- see
match/tier2_algorithmic.py and adjudicate/adjudicator.py). The remaining three
(AMOUNT_MISMATCH_BEYOND_TOLERANCE, SUSPECTED_DUPLICATE, OUT_OF_SCOPE) only make sense
with a second look at the wider dataset -- e.g. "is there a same-UTR batch nearby
whose amount just didn't fit" -- which is what classify() below does for any case
that still only carries the generic NO_CANDIDATE hint. See IMPLEMENTATION.md section
4 (Phase 4) and section 5 (the Exception_ contract).
"""

from __future__ import annotations

from enum import StrEnum

from ledgerloop.exceptions import explain
from ledgerloop.ingest.normalise import NormalisedBankLine
from ledgerloop.match.tier1_exact import SettlementBatch, transposed_utr_variants
from ledgerloop.schemas import Exception_, UnresolvedCase


class ReasonCode(StrEnum):
    NO_CANDIDATE = "NO_CANDIDATE"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AMOUNT_MISMATCH_BEYOND_TOLERANCE = "AMOUNT_MISMATCH_BEYOND_TOLERANCE"
    TIER2_TIMEOUT = "TIER2_TIMEOUT"
    TIER3_INVALID_SELECTION = "TIER3_INVALID_SELECTION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    SUSPECTED_DUPLICATE = "SUSPECTED_DUPLICATE"


# Reason hints that already carry enough meaning on their own -- these had actual
# candidates to weigh (or a specific, already-diagnostic failure mode), so a second
# look at the wider dataset wouldn't add anything.
_DIRECT_PASSTHROUGH: dict[str, ReasonCode] = {
    "TIER3_INVALID_SELECTION": ReasonCode.TIER3_INVALID_SELECTION,
    "AMBIGUOUS_CANDIDATES": ReasonCode.AMBIGUOUS_CANDIDATES,
    "LOW_CONFIDENCE": ReasonCode.LOW_CONFIDENCE,
}

# Reason hints that mean "tier2 came up with nothing usable" via two different
# mechanical paths (an empty result vs. a bounded search exhausting its budget) --
# both get the same reclassification pass below, since a line that's genuinely
# OUT_OF_SCOPE looks the same either way. The dict value is the fallback code if none
# of the specific patterns match, preserving the original distinction (TIER2_TIMEOUT
# still tells a reviewer "the search gave up," which NO_CANDIDATE doesn't).
_RECLASSIFIABLE: dict[str, ReasonCode] = {
    "NO_CANDIDATE": ReasonCode.NO_CANDIDATE,
    "TIER2_TIMEOUT": ReasonCode.TIER2_TIMEOUT,
}


def _near_utr_batch(
    bank_line: NormalisedBankLine,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
) -> SettlementBatch | None:
    """A batch whose payout UTR matches this line's narration exactly or via a single
    digit transposition -- i.e. there *is* linkage evidence, it just didn't clear
    tier2's amount/date bar. Distinguishes a genuine amount mismatch from a case with
    no linkage evidence at all.
    """
    if not bank_line.extracted_utr:
        return None
    keys = [bank_line.extracted_utr, *transposed_utr_variants(bank_line.extracted_utr)]
    for key in keys:
        candidates = [b for b in by_utr.get(key, []) if b.settlement_batch_id not in claimed_batch_ids]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _has_duplicate_amount_signature(
    bank_line: NormalisedBankLine, unclaimed_gross_amounts_paise: list[int], tolerance_paise: int
) -> bool:
    """Two or more distinct unclaimed gateway transactions with (near-)identical
    gross amounts is the signature of a double-charge -- see the DUPLICATE defect
    class in generate/defects.py. Requires >= 2 because one alone is just an ordinary
    unmatched transaction, not evidence of a duplicate.
    """
    matches = sum(
        1 for amount in unclaimed_gross_amounts_paise if abs(amount - bank_line.credit_amount_paise) <= tolerance_paise
    )
    return matches >= 2


def classify(
    bank_line: NormalisedBankLine,
    case: UnresolvedCase,
    *,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
    unclaimed_gross_amounts_paise: list[int],
    tier2_cfg: dict,
) -> Exception_:
    near_batch: SettlementBatch | None = None
    if case.reason_hint in _DIRECT_PASSTHROUGH:
        code = _DIRECT_PASSTHROUGH[case.reason_hint]
    elif case.reason_hint in _RECLASSIFIABLE:
        near_batch = _near_utr_batch(bank_line, by_utr, claimed_batch_ids)
        if near_batch is not None:
            code = ReasonCode.AMOUNT_MISMATCH_BEYOND_TOLERANCE
        elif _has_duplicate_amount_signature(
            bank_line, unclaimed_gross_amounts_paise, tier2_cfg["amount_tolerance_paise"]
        ):
            code = ReasonCode.SUSPECTED_DUPLICATE
        elif bank_line.extracted_utr is None:
            code = ReasonCode.OUT_OF_SCOPE
        else:
            code = _RECLASSIFIABLE[case.reason_hint]
    else:
        # defensive fallback for any future/unrecognised reason_hint -- never a
        # silent drop, worst case it's just under-classified.
        code = ReasonCode.NO_CANDIDATE

    # The deterministic account of why this line escalated is always present, computed
    # from what tier2 already measured. tier3's own reasoning, when there is any, is
    # appended as a labelled note rather than substituted for it -- so every figure a
    # reviewer reads is machine-derived and a model can only add narrative around it.
    # See exceptions/explain.py.
    explanation = explain.build_explanation(
        bank_line, case, code.value, tier2_cfg, near_utr_batch=near_batch
    )
    model_note = case.evidence.get("tier3_reasoning")
    if model_note is None:
        for candidate in case.candidates:
            reasoning = candidate.evidence.get("reasoning")
            if reasoning:
                model_note = reasoning
                break
    if model_note:
        explanation = f"{explanation} Adjudicator note: {model_note}"

    evidence: dict = {
        "tier2_reason_hint": case.reason_hint,
        "bank_credit_paise": bank_line.credit_amount_paise,
        "value_date": bank_line.value_date.isoformat(),
        "extracted_utr": bank_line.extracted_utr,
        "candidate_count": len(case.candidates),
        **case.evidence,
    }
    if case.candidates:
        evidence["candidate_detail"] = explain.candidate_details(case)
    if near_batch is not None:
        evidence["near_utr_batch"] = {
            "settlement_batch_id": near_batch.settlement_batch_id,
            "payout_utr": near_batch.payout_utr,
            "total_net_paise": near_batch.total_net_paise,
            "txn_count": len(near_batch.txn_ids),
            "amount_diff_paise": abs(bank_line.credit_amount_paise - near_batch.total_net_paise),
        }

    return Exception_(
        bank_line_id=bank_line.bank_line_id,
        reason_code=code.value,
        candidates_considered=[c.candidate_id for c in case.candidates],
        explanation=explanation,
        evidence=evidence,
    )


def classify_all(
    bank_lines_by_id: dict[str, NormalisedBankLine],
    cases: list[UnresolvedCase],
    *,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
    unclaimed_gross_amounts_paise: list[int],
    tier2_cfg: dict,
) -> list[Exception_]:
    exceptions = [
        classify(
            bank_lines_by_id[case.bank_line_id],
            case,
            by_utr=by_utr,
            claimed_batch_ids=claimed_batch_ids,
            unclaimed_gross_amounts_paise=unclaimed_gross_amounts_paise,
            tier2_cfg=tier2_cfg,
        )
        for case in cases
    ]
    return sorted(exceptions, key=lambda e: e.bank_line_id)
