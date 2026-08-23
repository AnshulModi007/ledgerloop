"""Bounded subset-sum. Meet-in-the-middle for windows under ~40 items (exact, and
still fast at that size); greedy + bounded local search above that, which emits a
timeout status instead of hanging. Never invents a match -- it returns "none" when
nothing fits and "ambiguous" when more than one subset does, leaving the caller
(tier2_algorithmic.py) to decide what happens next.
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass
from typing import Literal

MAX_SUBSETS_REPORTED = 5


@dataclass(frozen=True)
class Item:
    item_id: str
    value_paise: int


@dataclass
class SubsetSumResult:
    status: Literal["ok", "ambiguous", "none", "timeout"]
    subsets: list[list[str]]  # item_ids per matching subset, sorted; possibly truncated


def _all_subset_sums(group: list[Item]) -> list[tuple[int, tuple[int, ...]]]:
    sums: list[tuple[int, tuple[int, ...]]] = [(0, ())]
    for i, it in enumerate(group):
        sums = [(total, idxs) for total, idxs in sums] + [
            (total + it.value_paise, idxs + (i,)) for total, idxs in sums
        ]
    return sums


def _meet_in_the_middle(items: list[Item], target: int, tolerance: int) -> list[list[str]]:
    mid = len(items) // 2
    left, right = items[:mid], items[mid:]

    left_sums = [(t, idxs) for t, idxs in _all_subset_sums(left) if idxs]
    right_sums = sorted(((t, idxs) for t, idxs in _all_subset_sums(right) if idxs), key=lambda x: x[0])
    right_values = [s[0] for s in right_sums]

    # the empty+empty combination is meaningless (matches nothing); also allow one
    # side empty so an all-left or all-right subset is still found.
    left_sums = [(0, ())] + left_sums
    right_sums_with_empty = [(0, ())] + right_sums
    right_values_with_empty = [0] + right_values

    results: list[list[str]] = []
    for l_total, l_idxs in left_sums:
        lo = bisect.bisect_left(right_values_with_empty, target - tolerance - l_total)
        hi = bisect.bisect_right(right_values_with_empty, target + tolerance - l_total)
        for j in range(lo, hi):
            _r_total, r_idxs = right_sums_with_empty[j]
            if not l_idxs and not r_idxs:
                continue
            subset_ids = sorted([left[i].item_id for i in l_idxs] + [right[i].item_id for i in r_idxs])
            if subset_ids not in results:
                results.append(subset_ids)
            if len(results) >= MAX_SUBSETS_REPORTED:
                return results
    return results


def _greedy_local_search(
    items: list[Item], target: int, tolerance: int, node_budget: int, rng: random.Random
) -> SubsetSumResult:
    by_id = {it.item_id: it for it in items}
    all_ids = [it.item_id for it in items]

    chosen: set[str] = set()
    total = 0
    for it in sorted(items, key=lambda x: -x.value_paise):
        if total + it.value_paise <= target + tolerance:
            chosen.add(it.item_id)
            total += it.value_paise

    nodes = 0
    while abs(total - target) > tolerance and nodes < node_budget:
        nodes += 1
        candidate_id = rng.choice(all_ids)
        delta = by_id[candidate_id].value_paise
        new_total = total - delta if candidate_id in chosen else total + delta
        if abs(new_total - target) < abs(total - target):
            chosen.symmetric_difference_update({candidate_id})
            total = new_total

    if abs(total - target) <= tolerance:
        return SubsetSumResult(status="ok", subsets=[sorted(chosen)])
    return SubsetSumResult(status="timeout", subsets=[])


def find_subset(
    items: list[Item],
    target: int,
    *,
    tolerance: int,
    node_budget: int,
    meet_in_middle_max_items: int,
    rng: random.Random,
) -> SubsetSumResult:
    if not items:
        return SubsetSumResult(status="none", subsets=[])

    if len(items) <= meet_in_middle_max_items:
        subsets = _meet_in_the_middle(items, target, tolerance)
        if not subsets:
            return SubsetSumResult(status="none", subsets=[])
        status = "ok" if len(subsets) == 1 else "ambiguous"
        return SubsetSumResult(status=status, subsets=subsets)

    return _greedy_local_search(items, target, tolerance, node_budget, rng)
