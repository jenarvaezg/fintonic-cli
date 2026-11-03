import csv
import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.budgets import BudgetError, check_known, default_path, evaluate, load_budgets, parse_budgets
from fintonic.models import Category
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from fintonic.summary import CategoryTotal
from tests.conftest import API, BFF

runner = CliRunner()

CATS = {
    "G0800": Category(id="G0800", name="Gastos varios", ancestors=["root", "G0000"]),
    "G0898": Category(id="G0898", name="Restaurante", ancestors=["root", "G0000", "G0800"]),
    "G0897": Category(id="G0897", name="Bar", ancestors=["root", "G0000", "G0800"]),
    "G0101": Category(id="G0101", name="Farmacia", ancestors=["root", "G0000", "G0100"]),
    "I0101": Category(id="I0101", name="Nómina", ancestors=["root", "I0000", "I0100"]),
    "N0102": Category(id="N0102", name="Traspaso", computable=False, ancestors=["root", "N0000", "N0100"]),
}
TREE = {"categoryTree": {cid: c.model_dump(by_alias=True) for cid, c in CATS.items()}}
NETS = [
    {"categoryId": "I0101", "net": 200000, "numTransactions": 1},
    {"categoryId": "G0898", "net": -25000, "numTransactions": 4},
    {"categoryId": "G0897", "net": -6000, "numTransactions": 2},
    {"categoryId": "G0101", "net": -1500, "numTransactions": 1},
    {"categoryId": "N0102", "net": -50000, "numTransactions": 1},
]


def _row(cid: str, amount: int) -> CategoryTotal:
    return CategoryTotal(category_id=cid, name=cid, kind=cid[0], amount=amount, count=1)


# ---- file ------------------------------------------------------------------


def test_default_path(tmp_path: Path) -> None:
    assert default_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "fintonic" / "budgets.toml"
    assert default_path({"FINTONIC_BUDGETS": str(tmp_path / "b.toml"), "XDG_CONFIG_HOME": "x"}) == tmp_path / "b.toml"
    assert default_path({}) == Path("~/.config/fintonic/budgets.toml").expanduser()


def test_load_budgets(tmp_path: Path) -> None:
    path = tmp_path / "budgets.toml"
    path.write_text("# mensual\nG0800 = 300\ng0898 = 120.5\nG0101 = 0.01\n", encoding="utf-8")
    assert load_budgets(path) == {"G0800": 30000, "G0898": 12050, "G0101": 1}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "ningún presupuesto"),
        ({"Restaurante": 10}, "inválida"),
        ({"I0101": 10}, "inválida"),
        ({"N0102": 10}, "inválida"),
        ({"G08000": 10}, "inválida"),
        ({"G0800": "300"}, "número"),
        ({"G0800": True}, "número"),
        ({"G0800": {"limit": 3}}, "número"),
        ({"G0800": 0}, "mayor que 0"),
        ({"G0800": -5}, "mayor que 0"),
        ({"G0800": float("inf")}, "mayor que 0"),
        ({"G0800": float("nan")}, "mayor que 0"),
        ({"G0800": 1.234}, "2 decimales"),
        ({"G0800": 1, "g0800": 2}, "repetida"),
    ],
)
def test_parse_budgets_rejects(data: dict[str, object], message: str) -> None:
    with pytest.raises(BudgetError, match=message):
        parse_budgets(data)


def test_load_budgets_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BudgetError, match="No existe"):
        load_budgets(tmp_path / "nope.toml")


def test_load_budgets_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "budgets.toml"
    path.write_text("G0800 = = 3\n", encoding="utf-8")
    with pytest.raises(BudgetError, match="no es TOML válido"):
        load_budgets(path)


def test_check_known() -> None:
    check_known({"G0800": 1}, CATS)
    with pytest.raises(BudgetError, match="G0999"):
        check_known({"G0800": 1, "G0999": 1}, CATS)


# ---- evaluation ------------------------------------------------------------


def test_evaluate_group_and_leaf() -> None:
    rows = [_row("G0898", -25000), _row("G0897", -6000), _row("G0101", -1500), _row("I0101", 200000)]
    statuses = evaluate({"G0800": 30000, "G0898": 30000, "G0101": 1000}, rows, CATS)
    assert [(s.category_id, s.name, s.spent, s.percent, s.over) for s in statuses] == [
        ("G0800", "Gastos varios", 31000, 103, True),
        ("G0898", "Restaurante", 25000, 83, False),
        ("G0101", "Farmacia", 1500, 150, True),
    ]


def test_evaluate_refunds_and_no_spending() -> None:
    statuses = evaluate({"G0898": 10000, "G0101": 5000}, [_row("G0898", -3000), _row("G0898", 1000)], CATS)
    assert [(s.spent, s.percent, s.over) for s in statuses] == [(2000, 20, False), (0, 0, False)]


def test_exactly_at_limit_is_not_over() -> None:
    (status,) = evaluate({"G0898": 1000}, [_row("G0898", -1000)], CATS)
    assert (status.percent, status.over) == (100, False)


# ---- summary CLI -------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def stats(mock_api: respx.MockRouter) -> respx.Route:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=TREE))
    return mock_api.get(f"{BFF}/stats/range/net/categories").mock(return_value=httpx.Response(200, json=NETS))


@pytest.fixture
def budgets_file(tmp_path: Path) -> Path:
    path = tmp_path / "budgets.toml"
    path.write_text("G0800 = 300\nG0101 = 50\n", encoding="utf-8")
    return path


def test_summary_uses_bff_stats_without_exclusions(
    mock_api: respx.MockRouter, stats: respx.Route, store: MemorySessionStore
) -> None:
    txs = mock_api.get(f"{API}/rest/transaction/list/pageable")
    result = runner.invoke(cli.app, ["summary", "--month", "2026-10", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert not txs.called
    params = stats.calls.last.request.url.params
    assert (params["startDate"], params["endDate"]) == ("2026-10-01", "2026-10-31")
    body = json.loads(result.stdout)
    assert (body["income"], body["expenses"], body["net"]) == ("2000.00", "-325.00", "1675.00")
    assert body["excluded_non_computable"] == "-500.00"
    assert [c["id"] for c in body["categories"]] == ["I0101", "G0898", "G0897", "G0101"]
    assert "budgets" not in body


def test_summary_local_flag_paginates(
    mock_api: respx.MockRouter, stats: respx.Route, store: MemorySessionStore
) -> None:
    txs = mock_api.get(f"{API}/rest/transaction/list/pageable").mock(
        return_value=httpx.Response(200, json={"content": [], "last": True})
    )
    result = runner.invoke(cli.app, ["summary", "--month", "2026-10", "--local", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert txs.called
    assert not stats.called


def test_summary_budgets_json(stats: respx.Route, store: MemorySessionStore, budgets_file: Path) -> None:
    args = ["summary", "-m", "2026-10", "--group", "parent", "--budgets", "--budgets-file", str(budgets_file)]
    result = runner.invoke(cli.app, [*args, "-f", "json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["budgets"] == [
        {"id": "G0800", "name": "Gastos varios", "limit": "300.00", "spent": "310.00", "percent": 103, "over": True},
        {"id": "G0101", "name": "Farmacia", "limit": "50.00", "spent": "15.00", "percent": 30, "over": False},
    ]


def test_summary_budgets_from_env(
    stats: respx.Route, store: MemorySessionStore, budgets_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINTONIC_BUDGETS", str(budgets_file))
    result = runner.invoke(cli.app, ["summary", "-m", "2026-10", "--budgets", "-f", "csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [(r["id"], r["%"], r["excedido"]) for r in rows] == [("G0800", "103", "True"), ("G0101", "30", "False")]


def test_summary_budgets_table_warns(stats: respx.Route, store: MemorySessionStore, budgets_file: Path) -> None:
    result = runner.invoke(cli.app, ["summary", "-m", "2026-10", "--budgets", "--budgets-file", str(budgets_file)])
    assert result.exit_code == 0, result.output
    assert "Presupuestos 2026-10" in result.stdout
    assert "Presupuesto superado en: G0800" in result.stderr


def test_fail_over_budget_exit_code(stats: respx.Route, store: MemorySessionStore, budgets_file: Path) -> None:
    result = runner.invoke(
        cli.app, ["summary", "-m", "2026-10", "--fail-over-budget", "--budgets-file", str(budgets_file), "-f", "json"]
    )
    assert result.exit_code == 3, result.output
    assert json.loads(result.stdout)["budgets"][0]["over"] is True


def test_fail_over_budget_ok_when_within(stats: respx.Route, store: MemorySessionStore, tmp_path: Path) -> None:
    path = tmp_path / "budgets.toml"
    path.write_text("G0800 = 1000\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["summary", "-m", "2026-10", "--fail-over-budget", "--budgets-file", str(path)])
    assert result.exit_code == 0, result.output


def test_budgets_file_validated_before_network(
    mock_api: respx.MockRouter, store: MemorySessionStore, tmp_path: Path
) -> None:
    tree = mock_api.get(f"{API}/rest/category/tree")
    result = runner.invoke(cli.app, ["summary", "--budgets", "--budgets-file", str(tmp_path / "missing.toml")])
    assert result.exit_code != 0
    assert isinstance(result.exception, BudgetError)
    assert not tree.called


def test_unknown_budget_category(stats: respx.Route, store: MemorySessionStore, tmp_path: Path) -> None:
    path = tmp_path / "budgets.toml"
    path.write_text("G0999 = 10\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["summary", "--budgets", "--budgets-file", str(path)])
    assert isinstance(result.exception, BudgetError)
    assert "G0999" in str(result.exception)
