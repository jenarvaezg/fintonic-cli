import json
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.changelog import ChangeLog
from fintonic.models import Transaction
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from fintonic.transfers import TRANSFER_CATEGORY, find_transfers, is_non_computable
from tests.conftest import API, make_tx

runner = CliRunner()
CHECKING, SAVINGS, CARD = "a" * 64, "b" * 64, "4111XXXXXXXX1111"


def _tx(tx_id: str, cents: int, day: str, product: str, category: str = "G0898", **extra: Any) -> Transaction:
    return Transaction.model_validate(_raw(tx_id, cents, day, product, category, **extra))


def _raw(tx_id: str, cents: int, day: str, product: str, category: str = "G0898", **extra: Any) -> dict[str, Any]:
    return make_tx(
        0,
        id=tx_id,
        quantity=cents,
        userDate=day,
        valueDate=day,
        productId=product,
        categorization={"weightedCategories": {category: cents}},
        **extra,
    )


def _ids(pairs: list[Any]) -> list[tuple[str, str]]:
    return [(p.debit.id, p.credit.id) for p in pairs]


# ---- matcher -----------------------------------------------------------------


def test_basic_pair_between_products() -> None:
    txs = [_tx("d", -50000, "2026-10-05", CHECKING), _tx("c", 50000, "2026-10-06", SAVINGS, "I0199")]
    (pair,) = find_transfers(txs)
    assert (pair.debit.id, pair.credit.id, pair.amount, pair.days_apart) == ("d", "c", 50000, 1)
    assert [t.id for t in pair.to_recategorize] == ["d", "c"]


def test_same_product_never_pairs() -> None:
    txs = [_tx("d", -50000, "2026-10-05", CHECKING), _tx("c", 50000, "2026-10-05", CHECKING)]
    assert find_transfers(txs) == []


def test_amount_must_match_exactly() -> None:
    txs = [_tx("d", -50000, "2026-10-05", CHECKING), _tx("c", 50001, "2026-10-05", SAVINGS)]
    assert find_transfers(txs) == []


def test_two_debits_or_two_credits_never_pair() -> None:
    txs = [_tx("d1", -500, "2026-10-05", CHECKING), _tx("d2", -500, "2026-10-05", SAVINGS)]
    assert find_transfers(txs) == []


@pytest.mark.parametrize(("credit_day", "days", "found"), [("2026-10-08", 3, True), ("2026-10-09", 3, False)])
def test_day_window_is_inclusive(credit_day: str, days: int, found: bool) -> None:
    txs = [_tx("d", -500, "2026-10-05", CHECKING), _tx("c", 500, credit_day, SAVINGS)]
    assert bool(find_transfers(txs, days=days)) is found


def test_window_works_both_directions() -> None:
    txs = [_tx("d", -500, "2026-10-08", CHECKING), _tx("c", 500, "2026-10-05", SAVINGS)]
    (pair,) = find_transfers(txs)
    assert pair.days_apart == 3


def test_zero_days_means_same_day() -> None:
    txs = [_tx("d", -500, "2026-10-05", CHECKING), _tx("c", 500, "2026-10-06", SAVINGS)]
    assert find_transfers(txs, days=0) == []


def test_negative_days_rejected() -> None:
    with pytest.raises(ValueError):
        find_transfers([], days=-1)


def test_one_side_already_n_only_other_side_recategorized() -> None:
    txs = [_tx("d", -500, "2026-10-05", CHECKING, "N0102"), _tx("c", 500, "2026-10-05", SAVINGS, "I0199")]
    (pair,) = find_transfers(txs)
    assert [t.id for t in pair.to_recategorize] == ["c"]


def test_both_sides_n_not_proposed() -> None:
    txs = [_tx("d", -500, "2026-10-05", CHECKING, "N0102"), _tx("c", 500, "2026-10-05", SAVINGS, "N0101")]
    assert find_transfers(txs) == []


def test_existing_n_pair_is_not_repaired_with_a_third_movement() -> None:
    """A real transfer already in N* keeps its partner; the stray credit is left alone."""
    txs = [
        _tx("d", -500, "2026-10-05", CHECKING, "N0102"),
        _tx("c", 500, "2026-10-05", SAVINGS, "N0102"),
        _tx("stray", 500, "2026-10-06", CARD, "I0199"),
    ]
    assert find_transfers(txs) == []


def test_each_transaction_used_once_closest_wins() -> None:
    txs = [
        _tx("d1", -1000, "2026-10-01", CHECKING),
        _tx("d2", -1000, "2026-10-04", CHECKING),
        _tx("c1", 1000, "2026-10-04", SAVINGS),
        _tx("c2", 1000, "2026-10-02", SAVINGS),
    ]
    assert _ids(find_transfers(txs)) == [("d1", "c2"), ("d2", "c1")]


def test_greedy_keeps_the_closest_pair_even_if_another_is_left_unpaired() -> None:
    txs = [
        _tx("d1", -1000, "2026-10-01", CHECKING),
        _tx("c1", 1000, "2026-10-03", SAVINGS),
        _tx("d2", -1000, "2026-10-03", CARD),
    ]
    assert _ids(find_transfers(txs)) == [("d2", "c1")]


def test_tie_prefers_fewer_changes_then_ids() -> None:
    debit = _tx("d", -500, "2026-10-05", CHECKING, "N0102")
    regular = _tx("a-credit", 500, "2026-10-06", SAVINGS, "I0199")
    already = _tx("z-credit", 500, "2026-10-04", CARD, "N0102")
    # equal distance: the already-categorized partner wins, so nothing is proposed
    assert find_transfers([debit, regular, already]) == []
    other = _tx("b-credit", 500, "2026-10-04", CARD, "I0199")
    assert _ids(find_transfers([debit, regular, other])) == [("d", "a-credit")]


def test_skips_split_inactive_and_zero() -> None:
    split = Transaction.model_validate(
        make_tx(
            0,
            id="split",
            quantity=-500,
            userDate="2026-10-05",
            productId=CHECKING,
            categorization={"weightedCategories": {"G0898": -300, "G0101": -200}, "splited": True},
        )
    )
    txs = [
        split,
        _tx("inactive", -700, "2026-10-05", CHECKING, active=False),
        _tx("zero", 0, "2026-10-05", CHECKING),
        _tx("zero2", 0, "2026-10-05", SAVINGS),
        _tx("c5", 500, "2026-10-05", SAVINGS),
        _tx("c7", 700, "2026-10-05", SAVINGS),
    ]
    assert find_transfers(txs) == []


def test_results_sorted_by_debit_date() -> None:
    txs = [
        _tx("d-late", -200, "2026-10-20", CHECKING),
        _tx("c-late", 200, "2026-10-20", SAVINGS),
        _tx("d-early", -100, "2026-10-02", SAVINGS),
        _tx("c-early", 100, "2026-10-03", CHECKING),
    ]
    assert _ids(find_transfers(txs)) == [("d-early", "c-early"), ("d-late", "c-late")]


def test_is_non_computable() -> None:
    assert is_non_computable(_tx("x", -1, "2026-10-01", CHECKING, "N0102"))
    assert not is_non_computable(_tx("x", -1, "2026-10-01", CHECKING, "G0898"))


# ---- CLI ---------------------------------------------------------------------

TREE = {
    "categoryTree": {
        "G0898": {"id": "G0898", "name": "Restaurante"},
        "I0199": {"id": "I0199", "name": "Otros ingresos"},
        TRANSFER_CATEGORY: {"id": TRANSFER_CATEGORY, "name": "Que realices entre tus cuentas", "computable": False},
    }
}
ACCOUNTS = {
    "accounts": {
        "0182": [
            {"id": "acc1", "bankId": "0182", "alias": "Nómina", "ccc": CHECKING},
            {"id": "acc2", "bankId": "0182", "alias": "Ahorro", "ccc": SAVINGS},
        ]
    }
}
TXS = [
    _raw("d1", -50000, "2026-10-05", CHECKING),
    _raw("c1", 50000, "2026-10-06", SAVINGS, "I0199"),
    _raw("d2", -2000, "2026-10-10", SAVINGS, TRANSFER_CATEGORY),
    _raw("c2", 2000, "2026-10-10", CHECKING, "I0199"),
    _raw("x", -999, "2026-10-11", CHECKING),
]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def api(mock_api: respx.MockRouter) -> respx.MockRouter:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=TREE))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    mock_api.get(f"{API}/rest/transaction/list/pageable").mock(
        return_value=httpx.Response(200, json={"content": TXS, "last": True})
    )
    return mock_api


def _category_route(api: respx.MockRouter) -> respx.Route:
    return api.post(url__regex=rf"{API}/rest/transaction/\w+/category").mock(return_value=httpx.Response(200))


def test_transfers_lists_without_writing(api: respx.MockRouter, store: MemorySessionStore) -> None:
    writes = _category_route(api)
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "-f", "json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert [(p["debit"]["id"], p["credit"]["id"], p["to_recategorize"]) for p in body] == [
        ("d1", "c1", ["d1", "c1"]),
        ("d2", "c2", ["c2"]),
    ]
    assert body[0]["debit"]["product"] == "Nómina"
    assert body[0]["amount"] == "500.00"
    assert not writes.called
    params = api.routes[2].calls.last.request.url.params
    assert (params["beginDate"], params["endDate"]) == ("2026-09-28", "2026-11-03")


def test_transfers_drops_pairs_entirely_outside_the_period(api: respx.MockRouter, store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["transfers", "--from", "2026-10-08", "--to", "2026-10-31", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert [p["debit"]["id"] for p in json.loads(result.stdout)] == ["d2"]


def test_transfers_table(api: respx.MockRouter, store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert f"Restaurante (G0898) → {TRANSFER_CATEGORY}" in result.stdout


def test_transfers_apply_with_yes(api: respx.MockRouter, store: MemorySessionStore) -> None:
    writes = _category_route(api)
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "--apply", "--yes", "-f", "json"])
    assert result.exit_code == 0, result.output
    updated = [c.request.url.path.split("/")[-2] for c in writes.calls]
    assert updated == ["d1", "c1", "c2"]
    assert all(json.loads(c.request.content) == {"categoryId": TRANSFER_CATEGORY} for c in writes.calls)


def test_transfers_apply_asks_and_can_cancel(api: respx.MockRouter, store: MemorySessionStore) -> None:
    writes = _category_route(api)
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "--apply"], input="n\n")
    assert result.exit_code == 1
    assert "¿Recategorizar 3 movimientos" in result.output
    assert not writes.called


def test_transfers_apply_confirmed(api: respx.MockRouter, store: MemorySessionStore) -> None:
    writes = _category_route(api)
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "--apply"], input="y\n")
    assert result.exit_code == 0, result.output
    assert writes.call_count == 3


def test_transfers_month_and_range_are_exclusive(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "--from", "2026-10-01"])
    assert result.exit_code == 2


def test_transfers_apply_is_logged_for_undo(api: respx.MockRouter, store: MemorySessionStore) -> None:
    """Each side goes through the change log, so `tx undo` can revert it."""
    _category_route(api)
    result = runner.invoke(cli.app, ["transfers", "-m", "2026-10", "--apply", "--yes", "-f", "json"])
    assert result.exit_code == 0, result.output
    records = ChangeLog().records()
    assert [(r.command, r.tx_id) for r in records] == [("transfers", "d1"), ("transfers", "c1"), ("transfers", "c2")]
    assert all(r.after == {TRANSFER_CATEGORY: r.after[TRANSFER_CATEGORY]} for r in records)
    assert all(TRANSFER_CATEGORY not in r.before for r in records)
