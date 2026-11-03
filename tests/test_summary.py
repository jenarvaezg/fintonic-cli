from datetime import date

import pytest

from fintonic.models import Category, CategoryNet, Transaction
from fintonic.summary import month_range, summarize, summarize_nets
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


def _nets(txs: list[Transaction]) -> list[CategoryNet]:
    """What the BFF reports for these transactions: per category net and part count."""
    totals: dict[str, tuple[int, int]] = {}
    for tx in txs:
        for cid, cents in tx.category_amounts.items():
            net, count = totals.get(cid, (0, 0))
            totals[cid] = (net + cents, count + 1)
    return [CategoryNet(category_id=cid, net=net, num_transactions=n) for cid, (net, n) in totals.items()]


@pytest.mark.parametrize("group_by_parent", [False, True])
def test_bff_nets_give_the_same_summary(group_by_parent: bool) -> None:
    txs = [
        _tx(1, G0898=-700, N0102=-300),
        _tx(2, I0101=150000),
        _tx(3, G0897=-200),
        _tx(4, G0898=-500),
        _tx(5, G9999=-100),
        _tx(6, N9999=-50),
    ]
    local = summarize(txs, CATS, group_by_parent=group_by_parent)
    bff = summarize_nets(reversed(_nets(txs)), CATS, group_by_parent=group_by_parent)
    assert bff == local


def test_bff_nets_skip_empty_categories() -> None:
    nets = [CategoryNet(category_id="G0898", net=0, num_transactions=0)]
    assert summarize_nets(nets, CATS).rows == ()


def test_equal_amounts_sort_by_id() -> None:
    result = summarize([_tx(1, G0898=-100), _tx(2, G0897=-100)], CATS)
    assert [r.category_id for r in result.rows] == ["G0897", "G0898"]


def test_category_label_prefers_shortname() -> None:
    leaf = Category(id="G0898", name="Que tengas del día a día.", shortname="Otros gastos")
    assert leaf.label == "Otros gastos"
    assert Category(id="G0800", name="Gastos varios").label == "Gastos varios"
    assert Category(id="G0001").label == "G0001"


def test_summary_rows_use_short_labels() -> None:
    cats = {"G0898": Category(id="G0898", name="Que tengas del día a día.", shortname="Otros gastos")}
    result = summarize([_tx(1, G0898=-700)], cats)
    assert result.rows[0].name == "Otros gastos"
