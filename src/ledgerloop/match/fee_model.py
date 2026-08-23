"""Deterministic fee/GST/TDS arithmetic. Integer paise in, integer paise out.

Rounding rule: round-half-up (a.k.a. "round half away from zero" for the
non-negative amounts we deal with here) at the paise boundary, applied
independently to each of fee, GST-on-fee, and TDS. This matches how gateways
typically compute deductions per line rather than rounding a combined total,
and it is what makes the generator's injected nets and Tier 2's reconstructed
nets agree bit-for-bit.

No float anywhere in this module — see tests/test_money.py, which AST-walks
this file (and the rest of match/ and ledger/) and fails the build on any
float literal or float() call.
"""

from __future__ import annotations

from pydantic import BaseModel


def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounded half-up. numerator may be negative; denominator must be positive."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    sign = 1 if numerator >= 0 else -1
    numerator = abs(numerator)
    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder >= denominator:
        quotient += 1
    return sign * quotient


def bps_of(amount_paise: int, bps: int) -> int:
    """amount_paise * bps / 10_000, round-half-up. bps is basis points (1 bps = 0.01%)."""
    return round_half_up_div(amount_paise * bps, 10_000)


class FeeBreakdown(BaseModel):
    gross_paise: int
    fee_paise: int
    gst_on_fee_paise: int
    tds_paise: int
    refund_paise: int = 0
    chargeback_paise: int = 0
    net_paise: int

    model_config = {"frozen": True}


def compute_net(
    gross_paise: int,
    *,
    platform_fee_bps: int,
    gst_rate_bps: int,
    tds_bps: int,
    refund_paise: int = 0,
    chargeback_paise: int = 0,
) -> FeeBreakdown:
    """Reconstruct the expected net settlement amount from a gross transaction amount.

    net = gross - platform_fee - GST(on fee) - TDS(on gross) - refund - chargeback
    """
    if gross_paise < 0:
        raise ValueError("gross_paise must be non-negative")

    fee_paise = bps_of(gross_paise, platform_fee_bps)
    gst_on_fee_paise = bps_of(fee_paise, gst_rate_bps)
    tds_paise = bps_of(gross_paise, tds_bps)

    net_paise = (
        gross_paise
        - fee_paise
        - gst_on_fee_paise
        - tds_paise
        - refund_paise
        - chargeback_paise
    )

    return FeeBreakdown(
        gross_paise=gross_paise,
        fee_paise=fee_paise,
        gst_on_fee_paise=gst_on_fee_paise,
        tds_paise=tds_paise,
        refund_paise=refund_paise,
        chargeback_paise=chargeback_paise,
        net_paise=net_paise,
    )
