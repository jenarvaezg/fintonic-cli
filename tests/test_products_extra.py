"""Hide/show products, direct-debit mandates and category suggestions (issue #7). Synthetic data only."""

import json
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.client import PRODUCT_ENDPOINTS, FintonicClient
from fintonic.errors import InputError
from fintonic.models import Product
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API

runner = CliRunner()
CATS = {
    "categoryTree": {
        "G0898": {"id": "G0898", "name": "Restaurante"},
        "G0101": {"id": "G0101", "name": "Farmacia"},
        "G0402": {"id": "G0402", "name": "Luz"},
    }
}
ACCOUNTS = {"accounts": {"0182": [{"id": "acc1", "bankId": "0182", "alias": "Nómina"}]}}
CARDS = {"creditCards": {"0182": [{"id": "card1", "bankId": "0182", "active": False}]}}
MANDATE = {
    "userId": "1",
    "bankId": "0182",
    "mandateId": "m1",
    "businessName": "Eléctrica Ejemplo",
    "categoryId": "G0402",
    "status": "ACTIVE",
}


def _body(route: respx.Route) -> Any:
    content = route.calls.last.request.content
    return json.loads(content) if content else None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def catalog(mock_api: respx.MockRouter) -> None:
    bodies = {"account": ACCOUNTS, "credit_card": CARDS}
    for kind, endpoint in PRODUCT_ENDPOINTS.items():
        mock_api.get(f"{API}/{endpoint}").mock(return_value=httpx.Response(200, json=bodies.get(kind, {})))


# ---- product visibility --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "segment"),
    [("account", "account"), ("credit_card", "creditcard"), ("pension_plan", "pensionplan"), ("share", "share")],
)
def test_set_product_visible_paths(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session, kind: str, segment: str
) -> None:
    route = mock_api.post(f"{API}/rest/{segment}/p1/status").mock(return_value=httpx.Response(200))
    product = Product.model_validate({"kind": kind, "id": "p1", "bankId": "0182"})
    FintonicClient(session, http=http).set_product_visible(product, False)
    assert _body(route) == {"active": False}


def test_set_bank_products_visible(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.post(f"{API}/rest/creditcard/all/status").mock(return_value=httpx.Response(200))
    client = FintonicClient(session, http=http)
    client.set_bank_products_visible("0182", "credit_card", True)
    assert route.calls.last.request.url.params["bankId"] == "0182"
    assert _body(route) == {"active": True}
    with pytest.raises(InputError):
        client.set_bank_products_visible("0182", "loyalty", True)


def test_cli_products_hide(mock_api: respx.MockRouter, store: MemorySessionStore, catalog: None) -> None:
    route = mock_api.post(f"{API}/rest/account/acc1/status").mock(return_value=httpx.Response(200))
    result = runner.invoke(cli.app, ["products", "hide", "acc1"])
    assert result.exit_code == 0, result.output
    assert _body(route) == {"active": False}


def test_cli_products_show_unknown_id(mock_api: respx.MockRouter, store: MemorySessionStore, catalog: None) -> None:
    result = runner.invoke(cli.app, ["products", "show", "nope"])
    assert isinstance(result.exception, InputError)
    assert not [c for c in mock_api.calls if c.request.method == "POST"]


def test_cli_products_show_bank(mock_api: respx.MockRouter, store: MemorySessionStore, catalog: None) -> None:
    accounts = mock_api.post(f"{API}/rest/account/all/status").mock(return_value=httpx.Response(200))
    cards = mock_api.post(f"{API}/rest/creditcard/all/status").mock(return_value=httpx.Response(200))
    result = runner.invoke(cli.app, ["products", "show", "--bank", "0182"])
    assert result.exit_code == 0, result.output
    assert _body(accounts) == _body(cards) == {"active": True}


@pytest.mark.parametrize(
    "args", [["products", "hide"], ["products", "hide", "acc1", "--bank", "0182"], ["products", "hide", "--kind", "x"]]
)
def test_cli_products_hide_validates(store: MemorySessionStore, args: list[str]) -> None:
    assert runner.invoke(cli.app, args).exit_code != 0


def test_cli_products_still_lists_by_default(
    mock_api: respx.MockRouter, store: MemorySessionStore, catalog: None
) -> None:
    by_kind = runner.invoke(cli.app, ["products", "account", "-f", "json"])
    everything = runner.invoke(cli.app, ["products", "--all", "-f", "csv"])
    assert by_kind.exit_code == 0, by_kind.output
    assert [p["id"] for p in json.loads(by_kind.stdout)] == ["acc1"]
    assert "card1" in everything.stdout
    assert "oculto" in everything.stdout


# ---- mandates ------------------------------------------------------------------


def test_mandates(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/directDebit/mandates").mock(return_value=httpx.Response(200, json={"data": [MANDATE]}))
    mock_api.get(f"{API}/rest/directDebit/mandates/m1").mock(return_value=httpx.Response(200, json={"data": MANDATE}))
    client = FintonicClient(session, http=http)
    (listed,) = client.mandates()
    assert (listed.mandate_id, listed.business_name, listed.category_id) == ("m1", "Eléctrica Ejemplo", "G0402")
    assert client.mandate("m1") == listed


def test_mandates_empty(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/directDebit/mandates").mock(return_value=httpx.Response(200, json={"data": []}))
    assert FintonicClient(session, http=http).mandates() == []


def test_cli_mandates(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/directDebit/mandates").mock(return_value=httpx.Response(200, json={"data": [MANDATE]}))
    mock_api.get(f"{API}/rest/directDebit/mandates/m1").mock(return_value=httpx.Response(200, json={"data": MANDATE}))

    listed = runner.invoke(cli.app, ["mandates", "-f", "json"])
    detail = runner.invoke(cli.app, ["mandates", "m1", "-f", "json"])
    table = runner.invoke(cli.app, ["mandates", "-f", "csv"])

    assert listed.exit_code == 0, listed.output
    (row,) = json.loads(listed.stdout)
    assert row == {
        "mandate_id": "m1",
        "bank_id": "0182",
        "business_name": "Eléctrica Ejemplo",
        "category_id": "G0402",
        "category": "Luz",
        "status": "ACTIVE",
    }
    assert json.loads(detail.stdout) == row
    assert "Eléctrica Ejemplo,Luz,ACTIVE" in table.stdout


# ---- category suggestions ------------------------------------------------------


def test_category_suggestions_sorted(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{API}/rest/category/probability").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"categories": [{"id": "G0101", "probability": 0.1}, {"id": "G0898", "probability": 0.8}]}},
        )
    )
    suggestions = FintonicClient(session, http=http).category_suggestions("tx1")
    assert route.calls.last.request.url.params["transactionId"] == "tx1"
    assert [s.id for s in suggestions] == ["G0898", "G0101"]


def test_cli_tx_suggest(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/category/probability").mock(
        return_value=httpx.Response(200, json={"data": {"categories": [{"id": "G0898", "probability": 0.8312}]}})
    )
    table = runner.invoke(cli.app, ["tx", "suggest", "tx1"])
    as_json = runner.invoke(cli.app, ["tx", "suggest", "tx1", "-f", "json"])
    assert table.exit_code == 0, table.output
    assert "Restaurante" in table.stdout and "83%" in table.stdout
    assert json.loads(as_json.stdout) == [{"id": "G0898", "categoría": "Restaurante", "probabilidad": 0.8312}]
