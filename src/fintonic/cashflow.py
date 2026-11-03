"""Monthly cash flow from the BFF statistics, overall or for one category.

The BFF only reports leaf categories (a group id like G0800 always comes back as
zero), so a group's series is the sum of its leaves' series. The BFF answers 400
for more than 12 periods per request, so longer series are fetched page by page.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypeVar

from fintonic.client import FintonicClient
from fintonic.errors import InputError
from fintonic.models import CashFlowPeriod, Category, CategoryNetPeriod, TimeUnit

MAX_PERIODS = 12  # per request; the BFF rejects more

_P = TypeVar("_P", CashFlowPeriod, CategoryNetPeriod)


@dataclass(frozen=True)
class MonthFlow:
    start: date
    income: int  # cents
    expenses: int  # cents, negative
    net: int


@dataclass(frozen=True)
class CategoryMonth:
    start: date
    net: int  # signed cents
    count: int


def monthly_cash_flow(client: FintonicClient, pivot: date, months: int) -> list[MonthFlow]:
    """The `months` months ending with the one containing `pivot`, oldest first (one call per 12 months)."""

    def fetch(periods: int, page: int) -> list[CashFlowPeriod]:
        return client.cash_flow_periods(pivot, unit=TimeUnit.MONTH, periods=periods, page=page)

    return [
        MonthFlow(p.date_range.start_date, p.cash_flow.income, p.cash_flow.expenses, p.cash_flow.net)
        for p in _paged(fetch, months)
    ]


def _paged(fetch: Callable[[int, int], Sequence[_P]], months: int) -> list[_P]:
    """Fetch `months` periods oldest first; page N goes N * size periods further back."""
    if months < 1:
        raise InputError("El número de meses debe ser al menos 1.")
    size = min(months, MAX_PERIODS)
    pages = -(-months // size)
    periods = [p for page in reversed(range(pages)) for p in fetch(size, page)]
    return periods[-months:]


def leaf_ids(category_id: str, categories: Mapping[str, Category]) -> list[str]:
    """The category itself if it is a leaf, or every leaf below it if it is a group."""
    if category_id not in categories:
        raise InputError(f"La categoría {category_id} no existe. Mira los ids con `fintonic categories --all`.")
    below = {cid: c for cid, c in categories.items() if category_id in c.ancestors}
    if not below:
        return [category_id]
    parents = {ancestor for c in below.values() for ancestor in c.ancestors}
    return sorted(cid for cid in below if cid not in parents)


def monthly_category_net(
    client: FintonicClient, category_id: str, categories: Mapping[str, Category], pivot: date, months: int
) -> list[CategoryMonth]:
    """Net and transaction count per month for a leaf (one call) or a group (one call per leaf)."""
    nets: dict[date, int] = defaultdict(int)
    counts: dict[date, int] = defaultdict(int)
    for leaf in leaf_ids(category_id, categories):

        def fetch(periods: int, page: int, leaf: str = leaf) -> list[CategoryNetPeriod]:
            return client.category_net_periods(leaf, pivot, unit=TimeUnit.MONTH, periods=periods, page=page)

        for p in _paged(fetch, months):
            nets[p.date_range.start_date] += p.category_net.net
            counts[p.date_range.start_date] += p.category_net.num_transactions
    return [CategoryMonth(start, nets[start], counts[start]) for start in sorted(nets)]
