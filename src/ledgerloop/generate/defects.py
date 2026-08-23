"""The defect taxonomy injected by the generator. See IMPLEMENTATION.md section 4, Phase 1.

Each class is independently toggleable and every instance the generator injects is recorded
in the answer key, tagged with this code, so eval/metrics.py can report per-defect resolution
without re-deriving anything from the raw data.
"""

from __future__ import annotations

from enum import StrEnum


class DefectClass(StrEnum):
    CLEAN = "CLEAN"
    FEE_DRIFT = "FEE_DRIFT"
    BATCH_N1 = "BATCH_N1"
    SPLIT_1N = "SPLIT_1N"
    REFUND_NET = "REFUND_NET"
    CHARGEBACK = "CHARGEBACK"
    NO_UTR = "NO_UTR"
    MONTH_CROSS = "MONTH_CROSS"
    DUPLICATE = "DUPLICATE"
    TRANSPOSE = "TRANSPOSE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    INJECTION = "INJECTION"


DEFECT_DESCRIPTIONS: dict[DefectClass, str] = {
    DefectClass.CLEAN: "Exact 1:1 match.",
    DefectClass.FEE_DRIFT: "Fee + GST rounding drift; amount close but not equal.",
    DefectClass.BATCH_N1: "N transactions settle into 1 bank credit; requires subset-sum.",
    DefectClass.SPLIT_1N: "1 settlement batch pays out across 2 bank credits.",
    DefectClass.REFUND_NET: "Partial refund netted into the settlement, silently reducing the total.",
    DefectClass.CHARGEBACK: "Chargeback debit appears mid-batch as a negative line.",
    DefectClass.NO_UTR: "UTR absent from the bank narration; no join key exists.",
    DefectClass.MONTH_CROSS: "T+2 settlement crosses a month boundary; naive date windows fail.",
    DefectClass.DUPLICATE: "A double-charge produces two equally valid candidate transactions.",
    DefectClass.TRANSPOSE: "Digit transposition in the RRN (e.g. 12543 vs 12453) looks like a match but is not.",
    DefectClass.OUT_OF_SCOPE: "Bank credit unrelated to the gateway; must be rejected, not matched.",
    DefectClass.INJECTION: "Narration contains prompt-injection text; must be neutralised.",
}

# Every class must appear at least this many times in the dev set (config.generate.min_instances_per_defect).
ALL_DEFECT_CLASSES: tuple[DefectClass, ...] = tuple(DefectClass)
