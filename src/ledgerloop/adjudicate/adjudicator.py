"""Tier 3: LLM adjudication. Receives a bank credit and a *fixed list* of candidate
match IDs already produced by tier2 -- it only ever selects one of those or abstains,
and it never invents a match. This is the structural guarantee against hallucinated
reconciliations: the model is never shown a blank field and asked to produce an ID.

Where tier0's regex found no UTR at all, this module may also attempt LLM-based
extraction of one from the narration -- but it returns extracted *fields*, never a
match decision, and the extracted UTR is fed back through the ordinary deterministic
exact join (tier1_exact), not accepted on the model's say-so.

See IMPLEMENTATION.md section 4 (Phase 3) and section 9 (prompt design).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ValidationError

from ledgerloop import confidence
from ledgerloop.adjudicate import prompts
from ledgerloop.adjudicate.provider import LLMProvider, NullProvider, complete_with_fallback
from ledgerloop.ingest.normalise import NormalisedBankLine, NormalisedDataset
from ledgerloop.match import tier1_exact, tier2_algorithmic
from ledgerloop.match.tier1_exact import SettlementBatch
from ledgerloop.schemas import Candidate, Resolution, UnresolvedCase


class Adjudication(BaseModel):
    """IMPLEMENTATION.md section 4's tier3 contract (decision/candidate_id/confidence/
    reasoning) plus bank_line_id, which batched requests need to route each answer
    back to the right case. candidate_id MUST be one of the ids supplied for that
    bank_line_id, or null -- adjudicate_cases() discards anything else."""

    bank_line_id: str
    decision: Literal["select", "abstain"]
    candidate_id: str | None
    confidence: float
    reasoning: str


class NarrationExtraction(BaseModel):
    bank_line_id: str
    utr: str | None
    confidence: float


@dataclass
class Tier3Result:
    resolutions: list[Resolution]
    unresolved: list[UnresolvedCase]
    llm_calls_made: int
    providers_used: list[str] = field(default_factory=list)
    llm_available: bool = False


def _parse_json_array(text: str) -> list | None:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, list) else None


def _parse_batch_response(text: str, expected_ids: set[str]) -> dict[str, Adjudication]:
    raw = _parse_json_array(text)
    if raw is None:
        return {}
    result: dict[str, Adjudication] = {}
    for item in raw:
        try:
            adjudication = Adjudication.model_validate(item)
        except ValidationError:
            continue
        if adjudication.bank_line_id in expected_ids and adjudication.bank_line_id not in result:
            result[adjudication.bank_line_id] = adjudication
    return result


def _parse_extraction_response(text: str, expected_ids: set[str]) -> dict[str, NarrationExtraction]:
    raw = _parse_json_array(text)
    if raw is None:
        return {}
    result: dict[str, NarrationExtraction] = {}
    for item in raw:
        try:
            extraction = NarrationExtraction.model_validate(item)
        except ValidationError:
            continue
        if extraction.bank_line_id in expected_ids and extraction.bank_line_id not in result:
            result[extraction.bank_line_id] = extraction
    return result


def _to_resolution(
    adjudication: Adjudication, candidates_by_id: dict[str, Candidate], cfg: dict
) -> Resolution | None:
    """None means "not resolvable" -- either the model abstained, or its confidence
    fell below the configured threshold, which section 4 says to treat as an abstain."""
    if adjudication.decision != "select" or adjudication.candidate_id is None:
        return None
    candidate = candidates_by_id.get(adjudication.candidate_id)
    if candidate is None:
        return None
    if adjudication.confidence < cfg["confidence_threshold"]:
        return None
    return Resolution(
        bank_line_id=adjudication.bank_line_id,
        matched_txn_ids=candidate.matched_txn_ids,  # from tier2's candidate, never the model
        resolved_by="tier3",
        confidence=adjudication.confidence,
        evidence={
            "rule": "llm_adjudication",
            "candidate_id": candidate.candidate_id,
            "tier2_evidence": candidate.evidence,
            "reasoning": adjudication.reasoning,
            "prompt_version": prompts.PROMPT_VERSION,
        },
        audit_id=tier1_exact.audit_id("tier3", adjudication.bank_line_id, candidate.matched_txn_ids),
    )


def adjudicate_cases(
    cases: dict[str, tuple[NormalisedBankLine, list[Candidate]]],
    chain: list[LLMProvider],
    cfg: dict,
) -> tuple[dict[str, Resolution], dict[str, str], int, list[str], dict[str, str]]:
    """Batches `cases` (bank_line_id -> (bank_line, candidates)) into tier3.batch_size
    chunks, retrying only the cases that didn't get a usable answer, up to
    tier3.max_retries extra rounds. Returns (resolutions, reason_hint_by_bank_line_id
    for cases tier3 determined it couldn't resolve, llm_calls_made, provider names
    actually used, reasoning_by_bank_line_id -- the model's own explanation text, kept
    for any bank_line_id where a syntactically valid Adjudication was parsed at all,
    even an abstain or a discarded invalid selection, for exceptions/taxonomy.py's
    reviewer-facing explanation). A bank_line_id absent from the first two outputs
    means tier3 never got a usable answer for it at all (e.g. no LLM configured) --
    the caller should keep whatever reason_hint tier2 already gave it.
    """
    resolutions: dict[str, Resolution] = {}
    reason_hints: dict[str, str] = {}
    reasoning: dict[str, str] = {}
    llm_calls = 0
    providers_used: list[str] = []
    seen_invalid_selection: set[str] = set()

    remaining = dict(cases)
    max_attempts = cfg["max_retries"] + 1
    batch_size = cfg["batch_size"]

    for _attempt in range(max_attempts):
        if not remaining:
            break
        ids = sorted(remaining)
        llm_unavailable = False
        for start in range(0, len(ids), batch_size):
            batch_ids = ids[start : start + batch_size]
            prompt = prompts.build_adjudication_prompt([remaining[bid] for bid in batch_ids])
            text, provider_name, attempts_made = complete_with_fallback(chain, prompt)
            llm_calls += attempts_made

            if provider_name == "none":
                llm_unavailable = True
                break
            if text is None:
                continue  # this batch got no usable response this round; retry next attempt
            providers_used.append(provider_name)

            parsed = _parse_batch_response(text, expected_ids=set(batch_ids))
            for bid in batch_ids:
                adjudication = parsed.get(bid)
                if adjudication is None:
                    continue  # missing from the response; retry next attempt
                if adjudication.reasoning:
                    reasoning[bid] = adjudication.reasoning
                _bank_line, candidates = remaining[bid]
                candidates_by_id = {c.candidate_id: c for c in candidates}
                if adjudication.decision == "select" and adjudication.candidate_id not in candidates_by_id:
                    # discard and retry (bounded by max_retries) rather than giving
                    # up on the first bad response -- only recorded as a permanent
                    # TIER3_INVALID_SELECTION if every attempt comes back invalid.
                    seen_invalid_selection.add(bid)
                    continue
                resolution = _to_resolution(adjudication, candidates_by_id, cfg)
                if resolution is not None:
                    resolutions[bid] = resolution
                else:
                    reason_hints[bid] = "LOW_CONFIDENCE"
                del remaining[bid]

        if llm_unavailable:
            break  # no LLM configured at all; further retries won't help

    for bid in list(remaining):
        if bid in seen_invalid_selection:
            reason_hints[bid] = "TIER3_INVALID_SELECTION"
            del remaining[bid]

    return resolutions, reason_hints, llm_calls, providers_used, reasoning


def extract_narration_utrs(
    bank_lines: list[NormalisedBankLine],
    chain: list[LLMProvider],
    cfg: dict,
) -> tuple[dict[str, NarrationExtraction], int, list[str]]:
    results: dict[str, NarrationExtraction] = {}
    llm_calls = 0
    providers_used: list[str] = []
    batch_size = cfg["batch_size"]

    ordered = sorted(bank_lines, key=lambda b: b.bank_line_id)
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        prompt = prompts.build_narration_extraction_prompt(chunk)
        text, provider_name, attempts_made = complete_with_fallback(chain, prompt)
        llm_calls += attempts_made
        if provider_name == "none":
            break
        if text is None:
            continue
        providers_used.append(provider_name)
        results.update(_parse_extraction_response(text, expected_ids={b.bank_line_id for b in chunk}))

    return results, llm_calls, providers_used


def _resolve_extracted_utr(
    bank_line: NormalisedBankLine,
    extracted_utr: str,
    by_utr: dict[str, list[SettlementBatch]],
    claimed_batch_ids: set[str],
    tier2_cfg: dict,
) -> Resolution | None:
    """The extracted UTR is only ever a lead -- it still has to survive the same
    exact-join criteria tier1/tier2 apply to a regex-found UTR. An LLM hallucinating a
    plausible-looking UTR that happens not to belong to any real, unclaimed batch (or
    whose amount/date don't line up) simply fails this check, same as garbage input.

    Tries the value as given and, if it doesn't already carry it, with a "UTR" prefix
    prepended -- live testing against a real model showed it will often report just
    the digits (e.g. "55512345678901"), reasonably reading "UTR" as a label rather
    than part of the reference, when the narration read "UTR55512345678901". Both
    still have to clear the same amount/date/uniqueness bar below.
    """
    lookup_keys = [extracted_utr]
    if not extracted_utr.upper().startswith("UTR"):
        lookup_keys.append(f"UTR{extracted_utr}")

    candidates = [
        batch
        for key in lookup_keys
        for batch in by_utr.get(key, [])
        if batch.settlement_batch_id not in claimed_batch_ids
    ]
    if len(candidates) != 1:
        return None
    batch = candidates[0]
    diff = abs(bank_line.credit_amount_paise - batch.total_net_paise)
    lag = (bank_line.value_date - batch.settlement_date).days
    if diff > tier2_cfg["amount_tolerance_paise"] or not (0 <= lag <= tier2_cfg["date_window_days"]):
        return None
    score = confidence.score(
        base=confidence.BASE_LLM_EXTRACTED_UTR,
        diff=diff,
        tolerance=tier2_cfg["amount_tolerance_paise"],
        lag=lag,
        window=tier2_cfg["date_window_days"],
    )
    return Resolution(
        bank_line_id=bank_line.bank_line_id,
        matched_txn_ids=list(batch.txn_ids),
        resolved_by="tier3",
        confidence=score,
        evidence={
            "rule": "llm_narration_extraction_then_exact_join",
            "extracted_utr": extracted_utr,
            "settlement_batch_id": batch.settlement_batch_id,
            "amount_diff_paise": diff,
            "lag_days": lag,
            "prompt_version": prompts.PROMPT_VERSION,
        },
        audit_id=tier1_exact.audit_id("tier3", bank_line.bank_line_id, list(batch.txn_ids)),
    )


def run(
    normalised: NormalisedDataset,
    tier2_result: tier2_algorithmic.PipelineResult,
    config: dict,
    chain: list[LLMProvider],
) -> Tier3Result:
    cfg = config["tier3"]
    tier2_cfg = config["tier2"]

    batches = tier1_exact.build_batches(normalised.settlement_lines)
    by_utr = tier1_exact.batches_by_utr(batches)
    bank_line_by_id = {bl.bank_line_id: bl for bl in normalised.bank_lines}

    claimed_batch_ids = {
        r.evidence["settlement_batch_id"] for r in tier2_result.resolutions if "settlement_batch_id" in r.evidence
    }
    claimed_txn_ids = {t for r in tier2_result.resolutions for t in r.matched_txn_ids}

    resolutions: list[Resolution] = []
    still_unresolved: dict[str, UnresolvedCase] = {c.bank_line_id: c for c in tier2_result.unresolved}
    llm_calls = 0
    providers_used: list[str] = []

    def _claim(resolution: Resolution) -> bool:
        """Reject a would-be resolution if it overlaps a transaction another
        resolution already claimed this run -- tier2's per-case candidate sets aren't
        guaranteed globally disjoint across different unresolved cases."""
        if claimed_txn_ids & set(resolution.matched_txn_ids):
            return False
        claimed_txn_ids.update(resolution.matched_txn_ids)
        batch_id = resolution.evidence.get("settlement_batch_id")
        if batch_id:
            claimed_batch_ids.add(batch_id)
        return True

    # Step 1: narration extraction for cases with no regex UTR and nothing tier2 could
    # already offer as a candidate.
    needs_extraction = sorted(
        (
            bank_line_by_id[bid]
            for bid, case in still_unresolved.items()
            if not case.candidates and bank_line_by_id[bid].extracted_utr is None
        ),
        key=lambda b: b.bank_line_id,
    )
    if needs_extraction:
        extracted, calls, providers = extract_narration_utrs(needs_extraction, chain, cfg)
        llm_calls += calls
        providers_used.extend(providers)
        for bank_line in needs_extraction:
            item = extracted.get(bank_line.bank_line_id)
            if item is None or item.utr is None:
                continue
            resolution = _resolve_extracted_utr(bank_line, item.utr, by_utr, claimed_batch_ids, tier2_cfg)
            if resolution is not None and _claim(resolution):
                resolutions.append(resolution)
                del still_unresolved[bank_line.bank_line_id]

    # Step 2: candidate adjudication for everything still unresolved with candidates
    # to choose from. Cases with an empty candidate list have nothing to adjudicate --
    # calling the LLM with zero options would be pointless, so they're left as-is.
    to_adjudicate = {
        bid: (bank_line_by_id[bid], case.candidates) for bid, case in still_unresolved.items() if case.candidates
    }
    if to_adjudicate:
        new_resolutions, reason_hints, calls, providers, reasoning = adjudicate_cases(to_adjudicate, chain, cfg)
        llm_calls += calls
        providers_used.extend(providers)

        for bid in sorted(new_resolutions):
            resolution = new_resolutions[bid]
            if _claim(resolution):
                resolutions.append(resolution)
                del still_unresolved[bid]
            else:
                reason_hints.setdefault(bid, "AMBIGUOUS_CANDIDATES")

        for bid, reason in reason_hints.items():
            case = still_unresolved.get(bid)
            if case is None:
                continue
            evidence = case.evidence
            if bid in reasoning:
                evidence = {**evidence, "tier3_reasoning": reasoning[bid]}
            still_unresolved[bid] = UnresolvedCase(
                bank_line_id=bid, reason_hint=reason, candidates=case.candidates, evidence=evidence
            )

    llm_available = any(not isinstance(p, NullProvider) for p in chain)

    return Tier3Result(
        resolutions=resolutions,
        unresolved=sorted(still_unresolved.values(), key=lambda c: c.bank_line_id),
        llm_calls_made=llm_calls,
        providers_used=sorted(set(providers_used)),
        llm_available=llm_available,
    )
