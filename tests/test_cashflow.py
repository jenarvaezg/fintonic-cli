import json
from datetime import date

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.cashflow import leaf_ids, monthly_cash_flow, monthly_category_net
from fintonic.client import FintonicClient
from fintonic.errors import ApiError, InputError
from fintonic.models import Category, TimeUnit
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, BFF

runner = CliRunner()

CATS = {
    "G0000": Category(id="G0000", name="Gastos", ancestors=["root"]),
    "G0800": Category(id="G0800", name="Gastos varios", ancestors=["root", "G0000"]),
    "G0898": Category(id="G0898", name="Restaurante", ancestors=["root", "G0000", "G0800"]),
    "G0897": Category(id="G0897", name="Bar", ancestors=["root", "G0000", "G0800"]),
    "G0101": Category(id="G0101", name="Farmacia", ancestors=["root", "G0000", "G0100"]),
    "G0100": Category(id="G0100", name="Salud", ancestors=["root", "G0000"]),
}
TREE = {"categoryTree": {cid: c.model_dump(by_alias=True) for cid, c in CATS.items()}}


def _months_back(pivot: date, n: int) -> list[date]:
    """The n month starts ending with pivot's month, oldest first."""
    out = []
    y, m = pivot.year, pivot.month
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _end(start: date) -> str:
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1).isoformat()


def _cash_flow_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    pivot = date.fromisoformat(params["pivotDate"])
    size, page = int(params["numberOfPeriods"]), int(params["page"])
    if size > 12:
        return httpx.Response(400, json={"status": 400, "title": "Bad Request"})
    starts = _months_back(pivot, size * (page + 1))[:size]
    body = [
        {
            "dateRange": {"startDate": s.isoformat(), "endDate": _end(s)},
            "cashFlow": {"income": s.month * 1000, "expenses": -s.month * 100, "net": s.month * 900},
        }
        for s in starts
    ]
    return httpx.Response(200, json=body)


def _category_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    category_id = request.url.path.rsplit("/", 1)[1]
    pivot = date.fromisoformat(params["pivotDate"])
    size, page = int(params["numberOfPeriods"]), int(params["page"])
    starts = _months_back(pivot, size * (page + 1))[:size]
    net = {"G0898": -1000, "G0897": -250}.get(category_id, 0)
    body = [
        {
            "dateRange": {"startDate": s.isoformat(), "endDate": _end(s)},
            "categoryNet": {"categoryId": category_id, "net": net, "numTransactions": 2 if net else 0},
        }
        for s in starts
    ]
    return httpx.Response(200, json=body)


@pytest.fixture
def client(session: Session, http: httpx.Client) -> FintonicClient:
    return FintonicClient(session, http=http)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


# ---- client ----------------------------------------------------------------


def test_cash_flow_periods_params(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    route = mock_api.get(f"{BFF}/stats/period/cashFlow").mock(side_effect=_cash_flow_handler)
    periods = client.cash_flow_periods(date(2026, 10, 15), unit=TimeUnit.MONTH, periods=3)
    params = route.calls.last.request.url.params
    assert dict(params) == {"pivotDate": "2026-10-15", "timeUnit": "MONTH", "numberOfPeriods": "3", "page": "0"}
    assert [p.date_range.start_date for p in periods] == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert periods[-1].cash_flow.income == 10000


def test_category_nets(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    body = [{"categoryId": "G0898", "net": -1234, "numTransactions": 3}]
    route = mock_api.get(f"{BFF}/stats/range/net/categories").mock(return_value=httpx.Response(200, json=body))
    nets = client.category_nets(date(2026, 10, 1), date(2026, 10, 31))
    assert [(n.category_id, n.net, n.num_transactions) for n in nets] == [("G0898", -1234, 3)]
    params = route.calls.last.request.url.params
    assert (params["startDate"], params["endDate"]) == ("2026-10-01", "2026-10-31")


def test_stats_problem_body_with_200_raises(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    problem = {"type": "about:blank", "title": "External Service Error", "status": 404, "detail": None}
    mock_api.get(f"{BFF}/stats/range/net/categories").mock(return_value=httpx.Response(200, json=problem))
    with pytest.raises(ApiError, match="External Service Error") as exc:
        client.category_nets(date(2026, 10, 1), date(2026, 10, 31))
    assert exc.value.status == 404


def test_periods_must_be_positive(client: FintonicClient) -> None:
    with pytest.raises(InputError):
        client.cash_flow_periods(date(2026, 10, 1), periods=0)


# ---- cash flow series ------------------------------------------------------


def test_monthly_cash_flow_single_call(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    route = mock_api.get(f"{BFF}/stats/period/cashFlow").mock(side_effect=_cash_flow_handler)
    flows = monthly_cash_flow(client, date(2026, 10, 1), 12)
    assert route.call_count == 1
    assert [f.start for f in flows] == _months_back(date(2026, 10, 1), 12)
    assert flows[-1].net == 9000


def test_monthly_cash_flow_pages_beyond_12(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    route = mock_api.get(f"{BFF}/stats/period/cashFlow").mock(side_effect=_cash_flow_handler)
    flows = monthly_cash_flow(client, date(2026, 10, 1), 30)
    assert route.call_count == 3
    assert {c.request.url.params["page"] for c in route.calls} == {"0", "1", "2"}
    assert all(c.request.url.params["numberOfPeriods"] == "12" for c in route.calls)
    assert [f.start for f in flows] == _months_back(date(2026, 10, 1), 30)


def test_leaf_ids() -> None:
    assert leaf_ids("G0898", CATS) == ["G0898"]
    assert leaf_ids("G0800", CATS) == ["G0897", "G0898"]
    assert leaf_ids("G0000", CATS) == ["G0101", "G0897", "G0898"]
    with pytest.raises(InputError, match="no existe"):
        leaf_ids("G9999", CATS)


def test_group_series_sums_leaves(mock_api: respx.MockRouter, client: FintonicClient) -> None:
    route = mock_api.get(url__regex=rf"{BFF}/stats/period/net/categories/\w+").mock(side_effect=_category_handler)
    series = monthly_category_net(client, "G0800", CATS, date(2026, 10, 1), 2)
    assert route.call_count == 2  # one per leaf, never the group itself (always zero)
    assert [(m.start, m.net, m.count) for m in series] == [
        (date(2026, 9, 1), -1250, 4),
        (date(2026, 10, 1), -1250, 4),
    ]


# ---- CLI -------------------------------------------------------------------


def test_cashflow_cli_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_api.get(f"{BFF}/stats/period/cashFlow").mock(side_effect=_cash_flow_handler)
    result = runner.invoke(cli.app, ["cashflow", "--months", "2", "--to", "2026-10", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"mes": "2026-09", "ingresos": "90.00", "gastos": "-9.00", "neto": "81.00"},
        {"mes": "2026-10", "ingresos": "100.00", "gastos": "-10.00", "neto": "90.00"},
    ]
    assert route.calls.last.request.url.params["pivotDate"] == "2026-10-01"


def test_cashflow_cli_table_total(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{BFF}/stats/period/cashFlow").mock(side_effect=_cash_flow_handler)
    result = runner.invoke(cli.app, ["cashflow", "--months", "2", "--to", "2026-10"])
    assert result.exit_code == 0, result.output
    assert "neto 171,00 €" in result.stderr


def test_cashflow_cli_category(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=TREE))
    mock_api.get(url__regex=rf"{BFF}/stats/period/net/categories/\w+").mock(side_effect=_category_handler)
    result = runner.invoke(cli.app, ["cashflow", "-n", "1", "--to", "2026-10", "-c", "g0898", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"mes": "2026-10", "movs": 2, "importe": "-10.00"}]


def test_cashflow_cli_rejects_zero_months(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["cashflow", "--months", "0"])
    assert result.exit_code == 2
