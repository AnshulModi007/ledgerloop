"""The reconciliation statement a controller signs, and the controls behind it.

tests/test_idempotency.py already proves every journal batch balances internally. That is
a necessary check and a weak one: it looks at one batch at a time and can therefore never
see a problem that only exists *between* batches (see journal.find_duplicate_receivable_relief
for one that does). Nothing tied the run as a whole back to the bank statement it came from,
and nothing surfaced the result as something a person could read.

This module answers the four questions a finance reviewer asks in order:

  1. Does the cash tie out?      Every rupee credited on the statement is either reconciled
                                 or explicitly unreconciled -- and the bank-receipt legs we
                                 posted sum to exactly the reconciled half. If those two
                                 disagree, the ledger is telling a different story from the
                                 statement and nothing else here is trustworthy.
  2. Do the books balance?       Total debits equal total credits across the whole run.
  3. Where did the money move?   Debits, credits and net by control account.
  4. What did we absorb?         The rounding-adjustment total. Tier 2 tolerates fee-drift
                                 up to config's amount_tolerance_paise per line and posts
                                 the residual explicitly rather than swallowing it -- but
                                 until now that residual was only ever visible one posting
                                 at a time. Aggregated, it is the single number that says
                                 how much drift the tolerance actually let through.

Every figure is integer paise, computed from the postings themselves. No float, no
estimate, nothing assumed -- unlike the illustrative cost and analyst-hour figures in
eval/metrics.py, which are labelled as assumptions wherever they appear.
"""

from __future__ import annotations

from pydantic import BaseModel

from ledgerloop.ledger.journal import Posting, find_duplicate_receivable_relief

ROUNDING_ACCOUNT = "rounding_adjustment"
BANK_ACCOUNT = "bank_account"


class AccountMovement(BaseModel):
    account: str
    debit_paise: int
    credit_paise: int
    net_paise: int
    posting_count: int


class TieOut(BaseModel):
    # -- the statement, and how much of it we accounted for
    statement_total_paise: int
    statement_line_count: int
    reconciled_paise: int
    reconciled_line_count: int
    unreconciled_paise: int
    unreconciled_line_count: int

    # -- control 1: the cash posted matches the cash reconciled
    bank_receipt_total_paise: int
    cash_ties_out: bool

    # -- control 2: the books balance across the whole run, not just per batch
    total_debits_paise: int
    total_credits_paise: int
    balances: bool

    # -- control 3: what the fee-drift tolerance absorbed
    rounding_adjustment_gross_paise: int
    rounding_adjustment_net_paise: int
    rounding_adjustment_count: int

    # -- control 4: cross-batch double relief (see journal.py)
    duplicate_receivable_relief: dict[str, list[str]]

    movements: list[AccountMovement]

    @property
    def clean(self) -> bool:
        return self.balances and self.cash_ties_out and not self.duplicate_receivable_relief


def build(
    postings: list[Posting],
    credit_paise_by_bank_line: dict[str, int],
    resolved_bank_line_ids: set[str],
) -> TieOut:
    statement_total = sum(credit_paise_by_bank_line.values())
    reconciled = sum(paise for bid, paise in credit_paise_by_bank_line.items() if bid in resolved_bank_line_ids)
    reconciled_lines = sum(1 for bid in credit_paise_by_bank_line if bid in resolved_bank_line_ids)

    movements: dict[str, AccountMovement] = {}
    for posting in postings:
        movement = movements.setdefault(
            posting.account,
            AccountMovement(account=posting.account, debit_paise=0, credit_paise=0, net_paise=0, posting_count=0),
        )
        if posting.direction == "debit":
            movement.debit_paise += posting.amount_paise
        else:
            movement.credit_paise += posting.amount_paise
        movement.posting_count += 1
    for movement in movements.values():
        movement.net_paise = movement.debit_paise - movement.credit_paise

    rounding = [p for p in postings if p.account == ROUNDING_ACCOUNT]
    bank_receipts = [p for p in postings if p.account == BANK_ACCOUNT]
    total_debits = sum(p.amount_paise for p in postings if p.direction == "debit")
    total_credits = sum(p.amount_paise for p in postings if p.direction == "credit")
    bank_receipt_total = sum(p.amount_paise for p in bank_receipts)

    return TieOut(
        statement_total_paise=statement_total,
        statement_line_count=len(credit_paise_by_bank_line),
        reconciled_paise=reconciled,
        reconciled_line_count=reconciled_lines,
        unreconciled_paise=statement_total - reconciled,
        unreconciled_line_count=len(credit_paise_by_bank_line) - reconciled_lines,
        bank_receipt_total_paise=bank_receipt_total,
        cash_ties_out=bank_receipt_total == reconciled,
        total_debits_paise=total_debits,
        total_credits_paise=total_credits,
        balances=total_debits == total_credits,
        # Gross is what was absorbed in total; net is what it cost after offsetting
        # over- against under-credits, and the two differ whenever drift runs both ways.
        rounding_adjustment_gross_paise=sum(p.amount_paise for p in rounding),
        rounding_adjustment_net_paise=sum(
            p.amount_paise if p.direction == "debit" else -p.amount_paise for p in rounding
        ),
        rounding_adjustment_count=len(rounding),
        duplicate_receivable_relief=find_duplicate_receivable_relief(postings),
        movements=[movements[account] for account in sorted(movements)],
    )


def _row(label: str, amount: str, width: int = 70) -> str:
    """Label left, amount hard right, dot leaders between -- a statement should read down
    the right-hand column, which f-string padding of a variable-length prefix will not do."""
    filler = max(1, width - len(label) - len(amount) - 4)
    return f"  {label} {'.' * filler} {amount}"


def format_report(tie_out: TieOut) -> str:
    from ledgerloop.exceptions.explain import format_paise

    def money(paise: int) -> str:
        return format_paise(paise, "Rs.")

    lines = [
        "RECONCILIATION STATEMENT",
        "-" * 72,
        _row(f"bank statement, {tie_out.statement_line_count} credits", money(tie_out.statement_total_paise)),
        _row(f"reconciled ({tie_out.reconciled_line_count} lines)", money(tie_out.reconciled_paise)),
        _row(f"unreconciled ({tie_out.unreconciled_line_count} lines)", money(tie_out.unreconciled_paise)),
        "",
        "CONTROLS",
        "-" * 72,
        (
            f"  cash ties out          {'YES' if tie_out.cash_ties_out else 'NO':>6}   "
            f"bank receipts posted {money(tie_out.bank_receipt_total_paise)} "
            f"vs {money(tie_out.reconciled_paise)} reconciled"
        ),
        (
            f"  books balance          {'YES' if tie_out.balances else 'NO':>6}   "
            f"debits {money(tie_out.total_debits_paise)} vs credits {money(tie_out.total_credits_paise)}"
        ),
        (
            f"  fee drift absorbed              {money(tie_out.rounding_adjustment_gross_paise)} gross across "
            f"{tie_out.rounding_adjustment_count} postings (net {money(tie_out.rounding_adjustment_net_paise)})"
        ),
    ]
    if tie_out.duplicate_receivable_relief:
        lines.append(
            f"  receivable cleared twice  {len(tie_out.duplicate_receivable_relief)} transaction(s) -- "
            "needs a human; see journal.find_duplicate_receivable_relief"
        )
    else:
        lines.append("  receivable cleared twice        none")

    lines += ["", "MOVEMENT BY CONTROL ACCOUNT", "-" * 72]
    lines.append(f"  {'account':<32}{'debit':>20}{'credit':>20}{'n':>7}")
    for movement in tie_out.movements:
        lines.append(
            f"  {movement.account:<32}{money(movement.debit_paise):>20}"
            f"{money(movement.credit_paise):>20}{movement.posting_count:>7}"
        )

    lines += ["", "=" * 72]
    lines.append(
        "TIE-OUT CLEAN: cash and books agree, no transaction relieved twice."
        if tie_out.clean
        else "TIE-OUT NOT CLEAN -- see the controls above before approving anything."
    )
    return "\n".join(lines)
