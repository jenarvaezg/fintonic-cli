from datetime import date

import pytest

from fintonic.models import Category, Transaction
from fintonic.summary import month_range, summarize
from tests.conftest import make_tx

CATS = {
    "G0898": Category(id="G0898", name="Restaurante", ancestors=["root", "G0000", "G0800"]),
    "G0897": Category(id="G0897", name="Bar", ancestors=["root", "G0000", "G0800"]),
    "G0800": Category(id="G0800", name="Gastos varios", ancestors=["root", "G0000"]),
    "I0101": Category(id="I0101", name="Nómina", ancestors=["root", "I0000", "I0100"]),
    "N0102": Category(id="N0102", name="Traspaso", computable=False, ancestors=["root", "N0000", "N0100"]),
}


def _tx(i: int, **weights: int) -> Transaction:
    return Transaction.model_validate(
        make_tx(i, quantity=sum(weights.values()), categorization={"weightedCategories": weights})
    )


def test_split_is_spread_and_non_computable_excluded() -> None:
    txs = [_tx(1, G0898=-700, N0102=-300), _tx(2, I0101=150000), _tx(3, G0897=-200)]
    result = summarize(txs, CATS)

    by_id = {r.category_id: r for r in result.rows}
    assert by_id["G0898"].amount == -700
    assert "N0102" not in by_id
    assert result.excluded_non_computable == -300
    assert (result.income, result.expenses, result.net) == (150000, -900, 149100)
    assert [r.category_id for r in result.rows] == ["I0101", "G0898", "G0897"]


def test_group_by_parent() -> None:
    result = summarize([_tx(1, G0898=-700), _tx(2, G0897=-200)], CATS, group_by_parent=True)
    assert [(r.category_id, r.name, r.amount, r.count) for r in result.rows] == [("G0800", "Gastos varios", -900, 2)]


def test_refund_reduces_expenses() -> None:
    result = summarize([_tx(1, G0898=-1000), _tx(2, G0898=400)], CATS)
    assert result.expenses == -600


def test_unknown_category_kept_unless_n() -> None:
    result = summarize([_tx(1, G9999=-100), _tx(2, N9999=-50)], CATS)
    assert [r.category_id for r in result.rows] == ["G9999"]
    assert result.excluded_non_computable == -50


def test_month_range() -> None:
    assert month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_range("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_range("octubre")
