import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.aggregation import evaluate
from fintonic.client import FintonicClient
from fintonic.models import Transaction
from fintonic.session import Session
from fintonic.split import SplitError, build_split_body, parse_part
from fintonic.stores import MemorySessionStore
from tests.conftest import API, BFF, make_tx

runner = CliRunner()
CATS = {
    "categoryTree": {
        "G0898": {"id": "G0898", "name": "Restaurante"},
        "G0101": {"id": "G0101", "name": "Farmacia"},
    }
}


def _body(route: respx.Route) -> object:
    content = route.calls.last.request.content
    return json.loads(content) if content else None


# ---- split body ------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("G0898:85", ("G0898", 8500)), ("g0898:19,50", ("G0898", 1950)), ("G0101:0.05", ("G0101", 5))],
)
def test_parse_part(spec: str, expected: tuple[str, int]) -> None:
    assert parse_part(spec) == expected


@pytest.mark.parametrize("spec", ["G0898", "G0898:", ":10", "G0898:abc", "G0898:-5", "G0898:0", "G0898:1.234"])
def test_parse_part_rejects(spec: str) -> None:
    with pytest.raises(SplitError):
        parse_part(spec)


def test_split_body_applies_transaction_sign() -> None:
    body = build_split_body(-28000, [("G0898", 8500), ("G0101", 19500)])
    assert body == {"categoryIds": ["G0898", "G0101"], "categoryAmounts": [-8500, -19500]}
    assert build_split_body(1000, [("I0101", 600), ("I0102", 400)])["categoryAmounts"] == [600, 400]


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ([("G0898", 1000)], "2 o 3"),
        ([("A", 1), ("B", 1), ("C", 1), ("D", 997)], "2 o 3"),
        ([("G0898", 500), ("G0898", 500)], "repetidas"),
        ([("G0898", 500), ("G0101", 400)], "suman 9.00"),
    ],
)
def test_split_body_validation(parts, message: str) -> None:
    with pytest.raises(SplitError, match=message):
        build_split_body(-1000, parts)


# ---- client writes ---------------------------------------------------------


def test_set_category(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    one = mock_api.post(f"{API}/rest/transaction/tx1/category").mock(return_value=httpx.Response(200))
    rule = mock_api.post(f"{API}/rest/transaction/tx1/applyUserRule").mock(return_value=httpx.Response(200))
    client = FintonicClient(session, http=http)

    client.set_category("tx1", "G0898")
    client.set_category("tx1", "G0101", apply_to_similar=True)

    assert _body(one) == {"categoryId": "G0898"}
    assert _body(rule) == {"categoryId": "G0101"}


def test_text_edits(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    desc = mock_api.post(f"{API}/rest/transaction/tx1/primaryDisplay").mock(return_value=httpx.Response(200))
    note = mock_api.post(f"{API}/rest/transaction/tx1/secondaryDisplay").mock(return_value=httpx.Response(200))
    client = FintonicClient(session, http=http)
    client.set_description("tx1", "Cena")
    client.set_note("tx1", "con amigos")
    assert _body(desc) == {"textFieldValue": "Cena"}
    assert _body(note) == {"textFieldValue": "con amigos"}


def test_mark_read_variants(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    one = mock_api.post(f"{API}/rest/transaction/tx1/markAsRead").mock(return_value=httpx.Response(200))
    bank = mock_api.post(f"{API}/rest/transaction/markAsReadByBank/0182").mock(return_value=httpx.Response(200))
    user = mock_api.post(f"{API}/rest/transaction/markAsReadByUser").mock(return_value=httpx.Response(200))
    client = FintonicClient(session, http=http)
    client.mark_read("tx1")
    client.mark_all_read("0182")
    client.mark_all_read()
    assert one.called and bank.called and user.called


def test_writes_are_not_retried(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.post(f"{API}/rest/transaction/tx1/category").mock(return_value=httpx.Response(503))
    with pytest.raises(Exception, match="503"):
        FintonicClient(session, http=http).set_category("tx1", "G0898")
    assert route.call_count == 1


def test_transaction_lookup(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [make_tx(7)]})
    )
    client = FintonicClient(session, http=http)
    assert client.transaction("tx0007").id == "tx0007"
    assert route.calls.last.request.url.params["transactionIds"] == "tx0007"
    with pytest.raises(ValueError, match="No existe"):
        client.transaction("other")


def test_split_posts_signed_body(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.post(f"{API}/rest/transaction/tx0001/split").mock(return_value=httpx.Response(200))
    tx = Transaction.model_validate(make_tx(1, quantity=-28000))
    FintonicClient(session, http=http).split(tx, [("G0898", 8500), ("G0101", 19500)])
    assert _body(route) == {"categoryIds": ["G0898", "G0101"], "categoryAmounts": [-8500, -19500]}


# ---- bank refresh ----------------------------------------------------------


def _agg(entity: str, general: str, detail: str = "OK") -> dict:
    return {"entityId": entity, "status": {"general": general, "detail": detail}}


def test_evaluate_aggregations() -> None:
    status = evaluate(
        [
            _agg("0182", "IN_PROGRESS"),
            _agg("2091", "IN_PROGRESS", "2FA"),
            _agg("1465", "ACTIVE_ERROR"),
            _agg("0049", "SUCCEEDED"),
        ]
    )
    assert status.in_progress == ("0182",)
    assert status.needs_2fa == ("2091",)
    assert status.errors == ("1465",)
    assert not status.done


def test_refresh_and_wait(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    trigger = mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(
        return_value=httpx.Response(200)
    )
    poll = mock_api.get(f"{BFF}/openbanking/aggregations/").mock(
        side_effect=[
            httpx.Response(200, json=[_agg("0182", "IN_PROGRESS")]),
            httpx.Response(200, json=[_agg("0182", "IN_PROGRESS")]),
            httpx.Response(200, json=[_agg("0182", "SUCCEEDED")]),
        ]
    )
    sleeps: list[float] = []
    client = FintonicClient(session, http=http)

    client.refresh_banks()
    status = client.wait_for_banks(interval=1, sleep=sleeps.append)

    assert trigger.called
    assert status.done
    assert poll.call_count == 3
    assert sleeps == [1, 1]


def test_wait_gives_up_at_timeout(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{BFF}/openbanking/aggregations/").mock(
        return_value=httpx.Response(200, json=[_agg("0182", "IN_PROGRESS")])
    )
    status = FintonicClient(session, http=http).wait_for_banks(timeout=0, sleep=lambda _: None)
    assert status.in_progress == ("0182",)


# ---- CLI -------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


def test_cli_split(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [make_tx(1, quantity=-28000)]})
    )
    route = mock_api.post(f"{API}/rest/transaction/tx0001/split").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "split", "tx0001", "G0898:85", "g0101:195"])

    assert result.exit_code == 0, result.output
    assert _body(route) == {"categoryIds": ["G0898", "G0101"], "categoryAmounts": [-8500, -19500]}


def test_cli_split_rejects_bad_sum_before_calling_api(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [make_tx(1, quantity=-28000)]})
    )
    route = mock_api.post(f"{API}/rest/transaction/tx0001/split").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "split", "tx0001", "G0898:85", "G0101:100"])

    assert isinstance(result.exception, SplitError)
    assert not route.called


def test_cli_set_category_validates_category(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [make_tx(1, id="tx1")]})
    )
    route = mock_api.post(f"{API}/rest/transaction/tx1/category").mock(return_value=httpx.Response(200))

    bad = runner.invoke(cli.app, ["tx", "set-category", "tx1", "G7777"])
    ok = runner.invoke(cli.app, ["tx", "set-category", "tx1", "g0898"])

    assert bad.exit_code != 0
    assert ok.exit_code == 0, ok.output
    assert route.call_count == 1
    assert _body(route) == {"categoryId": "G0898"}


def test_cli_mark_read_requires_target(store: MemorySessionStore) -> None:
    assert runner.invoke(cli.app, ["tx", "mark-read"]).exit_code != 0


def test_cli_refresh_reports_2fa(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(200))
    mock_api.get(f"{BFF}/openbanking/aggregations/").mock(
        return_value=httpx.Response(200, json=[_agg("2091", "IN_PROGRESS", "2FA")])
    )
    result = runner.invoke(cli.app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "2FA" in result.stderr


def test_main_handles_input_errors(monkeypatch: pytest.MonkeyPatch, capsys, store: MemorySessionStore) -> None:
    monkeypatch.setattr("sys.argv", ["fintonic", "summary", "--month", "octubre"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "YYYY-MM" in capsys.readouterr().err
