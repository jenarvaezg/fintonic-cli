"""Monthly income/expense summary by category.

Split transactions are spread across their categories using `weightedCategories`
(signed cents adding up to the total). Non-computable categories (N*: transfers,
investments, card settlements...) are excluded, like Fintonic's own totals.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from fintonic.models import Category, Transaction


@dataclass(frozen=True)
class CategoryTotal:
    category_id: str
    name: str
    kind: str  # G = gasto, I = ingreso, ? = desconocida
    amount: int  # signed cents
    count: int


@dataclass(frozen=True)
class Summary:
    rows: tuple[CategoryTotal, ...]
    excluded_non_computable: int  # cents left out (N* categories)

    @property
    def income(self) -> int:
        return sum(r.amount for r in self.rows if r.kind == "I")

    @property
    def expenses(self) -> int:
        return sum(r.amount for r in self.rows if r.kind != "I")

    @property
    def net(self) -> int:
        return self.income + self.expenses


def month_range(month: str) -> tuple[date, date]:
    """'2026-10' -> (2026-10-01, 2026-10-31)."""
    try:
        year, mon = (int(part) for part in month.split("-"))
        return date(year, mon, 1), date(year, mon, monthrange(year, mon)[1])
    except ValueError as exc:
        raise ValueError(f"Mes inválido: {month!r}. Usa el formato YYYY-MM.") from exc


def _is_computable(category_id: str, categories: Mapping[str, Category]) -> bool:
    category = categories.get(category_id)
    if category is None:
        return not category_id.startswith("N")
    return category.computable and category.kind != "N"


def summarize(
    transactions: Iterable[Transaction],
    categories: Mapping[str, Category],
    *,
    group_by_parent: bool = False,
) -> Summary:
    amounts: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    excluded = 0

    for tx in transactions:
        for category_id, cents in tx.category_amounts.items():
            if not _is_computable(category_id, categories):
                excluded += cents
                continue
            category = categories.get(category_id)
            key = category.parent_id if (group_by_parent and category) else category_id
            amounts[key] += cents
            counts[key] += 1

    rows = tuple(
        sorted(
            (
                CategoryTotal(
                    category_id=key,
                    name=categories[key].name if key in categories else key,
                    kind=key[:1] if key[:1] in {"G", "I"} else "?",
                    amount=amount,
                    count=counts[key],
                )
                for key, amount in amounts.items()
            ),
            key=lambda r: (r.kind != "I", r.amount if r.kind != "I" else -r.amount),
        )
    )
    return Summary(rows=rows, excluded_non_computable=excluded)
