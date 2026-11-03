"""Local monthly budgets, independent of Fintonic (which has no endpoint to read them).

A TOML file maps expense category or group ids to a monthly limit in euros:

    G0800 = 300      # a group: every category below it counts
    G0898 = 120.50   # a single category

A group budget is checked against all the leaf categories below it, so it works
the same whether `summary` groups rows by parent or not.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from fintonic.errors import InputError
from fintonic.models import Category
from fintonic.summary import CategoryTotal

ENV_VAR = "FINTONIC_BUDGETS"
_EXPENSE_ID = re.compile(r"G\d{4}")


class BudgetError(InputError):
    """The budgets file is missing or malformed."""


def default_path(env: Mapping[str, str] = os.environ) -> Path:
    """`$FINTONIC_BUDGETS`, else `$XDG_CONFIG_HOME/fintonic/budgets.toml` (`~/.config` by default)."""
    if explicit := env.get(ENV_VAR):
        return Path(explicit).expanduser()
    config_home = env.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(config_home).expanduser() / "fintonic" / "budgets.toml"


def load_budgets(path: Path) -> dict[str, int]:
    """Read and validate the file; returns category id -> monthly limit in cents, in file order."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise BudgetError(
            f"No existe el fichero de presupuestos {path}. Créalo con líneas como `G0800 = 300`."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise BudgetError(f"El fichero de presupuestos {path} no es TOML válido: {exc}") from exc
    except OSError as exc:
        raise BudgetError(f"No se puede leer el fichero de presupuestos {path}: {exc.strerror}") from exc
    return parse_budgets(data, path)


def parse_budgets(data: Mapping[str, object], path: Path | str = "presupuestos") -> dict[str, int]:
    if not data:
        raise BudgetError(f"{path}: no define ningún presupuesto (p. ej. `G0800 = 300`).")
    budgets: dict[str, int] = {}
    for key, value in data.items():
        category_id = key.upper()
        if not _EXPENSE_ID.fullmatch(category_id):
            raise BudgetError(
                f"{path}: clave {key!r} inválida. Usa el id de una categoría o grupo de gasto, p. ej. G0800."
            )
        if category_id in budgets:
            raise BudgetError(f"{path}: la categoría {category_id} aparece repetida.")
        budgets[category_id] = _limit_cents(key, value, path)
    return budgets


def _limit_cents(key: str, value: object, path: Path | str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BudgetError(f"{path}: el presupuesto de {key} debe ser un número de euros, no {value!r}.")
    try:
        euros = Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - TOML numbers always convert
        raise BudgetError(f"{path}: importe inválido para {key}: {value!r}.") from exc
    if not euros.is_finite() or euros <= 0:
        raise BudgetError(f"{path}: el presupuesto de {key} debe ser mayor que 0.")
    if euros != euros.quantize(Decimal("0.01")):
        raise BudgetError(f"{path}: el presupuesto de {key} tiene más de 2 decimales.")
    return int(euros * 100)


def check_known(budgets: Mapping[str, int], categories: Mapping[str, Category]) -> None:
    unknown = [cid for cid in budgets if cid not in categories]
    if unknown:
        raise BudgetError(
            f"Presupuestos para categorías inexistentes: {', '.join(unknown)}. Mira `fintonic categories --all`."
        )


@dataclass(frozen=True)
class BudgetStatus:
    category_id: str
    name: str
    limit: int  # cents
    spent: int  # cents; positive = money out, refunds reduce it

    @property
    def percent(self) -> int:
        return int((Decimal(self.spent * 100) / Decimal(self.limit)).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    @property
    def over(self) -> bool:
        return self.spent > self.limit


def _belongs(row_id: str, budget_id: str, categories: Mapping[str, Category]) -> bool:
    if row_id == budget_id:
        return True
    category = categories.get(row_id)
    return category is not None and budget_id in category.ancestors


def evaluate(
    budgets: Mapping[str, int], leaf_rows: Iterable[CategoryTotal], categories: Mapping[str, Category]
) -> list[BudgetStatus]:
    """Spending against each budget, from a summary NOT grouped by parent."""
    rows = list(leaf_rows)
    return [
        BudgetStatus(
            category_id=budget_id,
            name=categories[budget_id].label if budget_id in categories else budget_id,
            limit=limit,
            spent=-sum(r.amount for r in rows if _belongs(r.category_id, budget_id, categories)),
        )
        for budget_id, limit in budgets.items()
    ]
