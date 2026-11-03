from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from fintonic.client import FintonicClient
from fintonic.errors import ApiError, DeviceVerificationRequired, NotLoggedInError, SessionExpiredError
from fintonic.models import Product, Transaction
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, BFF, make_jwt, make_tx

TX_LIST = f"{API}/rest/transaction/list"


def _page(items: list[dict], count: int) -> httpx.Response:
    return httpx.Response(200, json={"count": count, "resultList": items})


def test_requires_token(session: Session) -> None:
    with pytest.raises(NotLoggedInError):
        FintonicClient(session.without_tokens())


def test_sends_auth_and_device_headers(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{API}/rest/user/profile").mock(return_value=httpx.Response(200, json={"name": "x"}))
    FintonicClient(session, http=http).profile()
    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bearer {session.access_token}"
    assert headers["device-uuid"] == session.device_uuid


def test_transactions_paginate_until_count(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    txs = [make_tx(i) for i in range(5)]
    route = mock_api.get(TX_LIST).mock(side_effect=[_page(txs[:2], 5), _page(txs[2:4], 5), _page(txs[4:], 5)])

    result = list(FintonicClient(session, http=http).transactions(page_size=2))

    assert [t.id for t in result] == [t["id"] for t in txs]
    offsets = [int(c.request.url.params["pageOffset"]) for c in route.calls]
    assert offsets == [0, 2, 4]


def test_transactions_stop_on_empty_page(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(TX_LIST).mock(side_effect=[_page([make_tx(1)], 10), _page([], 10)])
    assert len(list(FintonicClient(session, http=http).transactions(page_size=1))) == 1
    assert route.call_count == 2


def test_transactions_respect_limit(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(TX_LIST).mock(return_value=_page([make_tx(i) for i in range(3)], 100))
    assert len(list(FintonicClient(session, http=http).transactions(limit=2, page_size=3))) == 2
    assert route.call_count == 1


def test_transaction_filters_map_to_api_params(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    route = mock_api.get(TX_LIST).mock(return_value=_page([], 0))
    list(
        FintonicClient(session, http=http).transactions(
            begin=date(2026, 10, 1), end=date(2026, 10, 15), category_id="G0898", query="super"
        )
    )
    params = route.calls.last.request.url.params
    assert params["beginDate"] == "2026-10-01"
    assert params["endDate"] == "2026-10-15"
    assert params["categoryId"] == "G0898"
    assert params["queryText"] == "super"


def test_transactions_by_bank(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{API}/rest/transaction/listByBank/0182").mock(return_value=_page([], 0))
    list(FintonicClient(session, http=http).transactions(bank_id="0182"))
    assert route.called


def test_split_category_is_largest_absolute_share() -> None:
    tx = Transaction.model_validate(
        make_tx(
            0, quantity=-1000, categorization={"weightedCategories": {"G0101": -100, "G0898": -900}, "splited": True}
        )
    )
    assert tx.category_id == "G0898"
    assert tx.is_split
    assert tx.category_amounts == {"G0101": -100, "G0898": -900}


def test_product_filter_is_client_side_within_bank(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mine = make_tx(1, productId="ccc-mine")
    other = make_tx(2, productId="ccc-other")
    route = mock_api.get(f"{API}/rest/transaction/listByBank/0182").mock(
        return_value=_page([other, mine, other, mine], 4)
    )
    product = Product(kind="account", id="a1", bankId="0182", ccc="ccc-mine")

    result = list(FintonicClient(session, http=http).transactions(product=product, limit=1))

    assert [t.id for t in result] == ["tx0001"]
    assert route.called


def test_unread_filter(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(TX_LIST).mock(return_value=_page([], 0))
    list(FintonicClient(session, http=http).transactions(unread=True))
    assert route.calls.last.request.url.params["read"] == "false"


def test_product_lookup(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    for path, key in [
        ("account/listAccounts", "accounts"),
        ("creditcard/listCreditCards", "creditCards"),
        ("deposit/listDeposits", "deposits"),
        ("fund/listFunds", "funds"),
        ("loan/listLoans", "loans"),
        ("share/listShares", "shares"),
        ("pensionplan/listPensionPlans", "pensionPlans"),
    ]:
        products = [{"id": "c9", "bankId": "0182", "cardNumber": "1234"}] if key == "creditCards" else []
        mock_api.get(f"{API}/rest/{path}").mock(return_value=httpx.Response(200, json={key: {"0182": products}}))
    client = FintonicClient(session, http=http)
    assert client.product("c9").transaction_keys == {"1234"}
    with pytest.raises(ValueError, match="No existe"):
        client.product("nope")


def test_transaction_model() -> None:
    tx = Transaction.model_validate(make_tx(0, quantity=-79804, userDescription="Cena"))
    assert tx.amount == Decimal("-798.04")
    assert tx.category_id == "G0898"
    assert tx.user_date == date(2026, 10, 30)
    assert tx.label == "Cena"
    assert tx.model_extra["cleanNote"] == ""


def test_expired_token_triggers_relogin_before_request(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    expired = replace(session, access_token=make_jwt(timedelta(minutes=-5)))
    fresh = replace(session, access_token=make_jwt())
    store = MemorySessionStore(expired)
    route = mock_api.get(f"{API}/rest/user/profile").mock(return_value=httpx.Response(200, json={}))

    client = FintonicClient(expired, http=http, store=store, relogin=lambda _: fresh)
    client.profile()

    assert route.calls.last.request.headers["authorization"] == f"Bearer {fresh.access_token}"
    assert store.load() == fresh


def test_401_triggers_relogin_and_single_retry(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    fresh = replace(session, access_token=make_jwt(timedelta(minutes=29)))
    route = mock_api.get(f"{API}/rest/user/profile").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"name": "ok"})]
    )
    client = FintonicClient(session, http=http, relogin=lambda _: fresh)
    assert client.profile() == {"name": "ok"}
    assert route.call_count == 2


def test_401_without_relogin_raises(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/user/profile").mock(return_value=httpx.Response(401))
    with pytest.raises(SessionExpiredError):
        FintonicClient(session, http=http).profile()


def test_unverified_device_error(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/user/profile").mock(
        return_value=httpx.Response(403, json={"error": "Verified deviceUuid is mandatory"})
    )
    with pytest.raises(DeviceVerificationRequired):
        FintonicClient(session, http=http).profile()


def test_api_error_has_status_and_path(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/nope").mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(ApiError) as exc_info:
        FintonicClient(session, http=http).get("rest/nope")
    assert exc_info.value.status == 400
    assert exc_info.value.path == "/finapi/rest/nope"


def test_position_history_sorted(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    bal = {"accountBalance": 100, "creditCardBalance": -50, "loanBalance": -25}
    body = {"data": {"position": bal, "historicPosition": {"2026-11": bal, "2025-12": bal, "2026-03": bal}}}
    mock_api.get(f"{API}/rest/overview/position").mock(return_value=httpx.Response(200, json=body))

    pos = FintonicClient(session, http=http).position()

    assert pos.position.net_worth == 25
    assert [m for m, _ in pos.history()] == ["2025-12", "2026-03", "2026-11"]


def test_categories(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    tree = {"categoryTree": {"G0898": {"id": "G0898", "name": "Restaurante", "classified": True, "ancestors": ["G08"]}}}
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=tree))
    cats = FintonicClient(session, http=http).categories()
    assert cats["G0898"].name == "Restaurante"


def test_banks(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    body = {"data": {"bankRegistries": [{"bankId": "0182", "name": "Banco", "products": [{"type": "ACCOUNT"}]}]}}
    mock_api.get(f"{API}/rest/bankRegistries").mock(return_value=httpx.Response(200, json=body))
    banks = FintonicClient(session, http=http).banks()
    assert banks[0].bank_id == "0182"
    assert banks[0].products[0].type == "ACCOUNT"


def test_products_flatten_by_bank(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    body = {
        "accounts": {
            "0182": [{"id": "a1", "bankId": "0182", "balance": 12345, "alias": "Cuenta", "displayCcc": "ES** 1234"}],
            "2091": [{"id": "a2", "bankId": "2091", "balance": 0, "active": False, "canceled": True}],
        }
    }
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=body))

    products = FintonicClient(session, http=http).products("account")

    assert [p.id for p in products] == ["a1", "a2"]
    assert products[0].kind == "account"
    assert products[0].name == "Cuenta"
    assert products[0].masked_number == "ES** 1234"
    assert products[0].is_live and not products[1].is_live


def test_products_empty_bank_map(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{API}/rest/fund/listFunds").mock(return_value=httpx.Response(200, json={"funds": {}}))
    assert FintonicClient(session, http=http).products("fund") == []


def test_unknown_product_kind(http: httpx.Client, session: Session) -> None:
    with pytest.raises(ValueError, match="desconocido"):
        FintonicClient(session, http=http).products("crypto")


def test_bff_base_url(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{BFF}/openbanking/aggregations/").mock(return_value=httpx.Response(200, json=[]))
    assert FintonicClient(session, http=http).aggregations() == []
    assert route.called
