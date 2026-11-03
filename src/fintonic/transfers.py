"""Detect transfers between the user's own products that are not categorized as such.

A transfer shows up as a debit on one product and a credit of the same absolute
amount on another, a few days apart. Fintonic files those under non-computable
N* categories; when either side landed in a regular category it inflates both
income and expenses. Each movement is used in at most one pair, closest dates first.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from fintonic.models import Transaction

TRANSFER_CATEGORY = "N0102"  # "Que realices entre tus cuentas"


@dataclass(frozen=True)
class TransferPair:
    debit: Transaction
    credit: Transaction

    @property
    def amount(self) -> int:
        """Absolute amount moved, in cents."""
        return self.credit.quantity

    @property
    def days_apart(self) -> int:
        return abs((tx_date(self.debit) - tx_date(self.credit)).days)

    @property
    def to_recategorize(self) -> tuple[Transaction, ...]:
        """Sides not yet in a non-computable N* category."""
        return tuple(t for t in (self.debit, self.credit) if not is_non_computable(t))


def tx_date(tx: Transaction) -> date:
    moment = tx.user_date or tx.value_date or tx.operation_date
    if moment is None:  # never seen in practice; callers filter these out
        raise ValueError(f"El movimiento {tx.id} no tiene fecha.")
    return moment


def is_non_computable(tx: Transaction) -> bool:
    amounts = tx.category_amounts
    return bool(amounts) and all(cid.startswith("N") for cid in amounts)


def _eligible(tx: Transaction) -> bool:
    has_date = (tx.user_date or tx.value_date or tx.operation_date) is not None
    return tx.active and tx.quantity != 0 and not tx.is_split and has_date


def find_transfers(transactions: Iterable[Transaction], *, days: int = 3) -> list[TransferPair]:
    """Pair debits and credits of equal absolute amount on different products within ±`days`.

    Matching is greedy by closeness in days. Pairs whose both sides are already N* take
    part in the matching (they are real transfers and must not be re-paired with another
    movement) but are not returned, since there is nothing to fix. On equal distance,
    pairs needing fewer changes win, then ids decide so the result is deterministic.
    Results are sorted by debit date.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    txs = [t for t in transactions if _eligible(t)]
    credits_by_amount: dict[int, list[Transaction]] = {}
    for t in txs:
        if t.quantity > 0:
            credits_by_amount.setdefault(t.quantity, []).append(t)

    candidates: list[tuple[int, int, str, str, TransferPair]] = []
    for debit in (t for t in txs if t.quantity < 0):
        for credit in credits_by_amount.get(-debit.quantity, []):
            pair = TransferPair(debit, credit)
            if credit.product_id == debit.product_id or pair.days_apart > days:
                continue
            candidates.append((pair.days_apart, len(pair.to_recategorize), debit.id, credit.id, pair))

    used: set[str] = set()
    chosen: list[TransferPair] = []
    for _, _, debit_id, credit_id, pair in sorted(candidates, key=lambda c: c[:4]):
        if debit_id in used or credit_id in used:
            continue
        used.update((debit_id, credit_id))
        if pair.to_recategorize:
            chosen.append(pair)
    return sorted(chosen, key=lambda p: (tx_date(p.debit), p.debit.id))
