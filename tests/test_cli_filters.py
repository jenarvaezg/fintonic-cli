"""`fintonic transactions` filters map to `list/pageable` query params."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, make_tx

runner = CliRunner()
TX_LIST = f"{API}/rest/transaction/list/pageable"
CATS = {"categoryTree": {"G0898": {"id": "G0898", "name": "Restaurante", "classified": True}}}
ACCOUNTS = {"accounts": {"0182": [{"id": "acc1", "bankId": "0182", "alias": "Nómina", "ccc": "ccc-1"}]}}
CARDS = {"creditCards": {"0182": [{"id": "card1", "bankId": "0182", "alias": "Visa", "cardNumber": "4111"}]}}
LOANS = {"loans": {"0182": [{"id": "loan1", "bankId": "0182", "alias": "Hipoteca", "accountNumber": "999"}]}}


@pytest.fixture
def api(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, session: Session) -> respx.MockRouter:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore(session))
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))
    mock_api.get(f"{API}/rest/creditcard/listCreditCards").mock(return_value=httpx.Response(200, json=CARDS))
    mock_api.get(f"{API}/rest/loan/listLoans").mock(return_value=httpx.Response(200, json=LOANS))
    for path in ("deposit/listDeposits", "fund/listFunds", "share/listShares", "pensionplan/listPensionPlans"):
        mock_api.get(f"{API}/rest/{path}").mock(return_value=httpx.Response(200, json={}))
    return mock_api


def _invoke(api: respx.MockRouter, *args: str) -> tuple[httpx.QueryParams, respx.Route]:
    route = api.get(TX_LIST).mock(return_value=httpx.Response(200, json={"content": [make_tx(1)], "last": True}))
    result = runner.invoke(cli.app, ["transactions", "-f", "json", *args])
    assert result.exit_code == 0, result.output
    return route.calls.last.request.url.params, route


def test_repeatable_categories_and_flags(api: respx.MockRouter) -> None:
    params, _ = _invoke(
        api, "-c", "g0898", "-c", "G0501", "--unclassified", "--direct-debit", "--oldest-first", "--unread"
    )
    assert params.get_list("categoryId") == ["G0898", "G0501"]
    assert (params["unclassified"], params["onlyDirectDebit"], params["read"]) == ("true", "true", "false")
    assert params["sort"] == "userDate,asc"


def test_default_is_newest_first_without_optional_filters(api: respx.MockRouter) -> None:
    params, _ = _invoke(api)
    assert params["sort"] == "userDate,desc"
    assert params["size"] == "50"  # default --limit
    assert not {"categoryId", "minAmount", "maxAmount", "bankId", "unclassified"} & set(params)


def test_min_max_are_signed_cents(api: respx.MockRouter) -> None:
    params, _ = _invoke(api, "--min", "-100", "--max=-12,50")
    assert (params["minAmount"], params["maxAmount"]) == ("-10000", "-1250")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("-12,50", ("-1250", "-1250")), ("-100..-50", ("-10000", "-5000")), ("100..", ("10000", None))],
)
def test_amount_exact_and_range(api: respx.MockRouter, spec: str, expected: tuple[str, str | None]) -> None:
    params, _ = _invoke(api, f"--amount={spec}")
    assert (params["minAmount"], params.get("maxAmount")) == expected


def test_bank_is_a_server_side_param(api: respx.MockRouter) -> None:
    params, _ = _invoke(api, "--bank", "0182")
    assert params["bankId"] == "0182"


@pytest.mark.parametrize(("product", "key", "value"), [("acc1", "ccc", "ccc-1"), ("card1", "cardNumber", "4111")])
def test_product_filters_server_side(api: respx.MockRouter, product: str, key: str, value: str) -> None:
    params, _ = _invoke(api, "--product", product)
    assert params[key] == value


def test_product_without_movements_warns_and_skips_listing(api: respx.MockRouter) -> None:
    route = api.get(TX_LIST).mock(return_value=httpx.Response(200, json={"content": [make_tx(1)], "last": True}))
    result = runner.invoke(cli.app, ["transactions", "-f", "json", "--product", "loan1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert "Solo cuentas y tarjetas" in result.stderr
    assert not route.called


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--amount", "5", "--min", "1"], "--amount"),
        (["--min", "10", "--max", "-10"], "--min"),
        (["--amount", "doce"], "Importe inválido"),
    ],
)
def test_bad_amounts_fail_before_the_network(
    mock_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: Session,
    args: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore(session))
    monkeypatch.setattr("sys.argv", ["fintonic", "transactions", *args])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert message in capsys.readouterr().err
    assert not mock_api.calls
