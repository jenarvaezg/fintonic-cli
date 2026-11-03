"""`tx show`, `tx mark-read` targeting and local-time output (issue #10)."""

import csv
import io
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, make_tx

runner = CliRunner()
CATS = {
    "categoryTree": {
        "G0898": {"id": "G0898", "name": "Restaurante"},
        "G0101": {"id": "G0101", "name": "Farmacia"},
    }
}
ACCOUNTS = {"accounts": {"0182": [{"id": "acc1", "bankId": "0182", "alias": "Nómina", "ccc": "p" * 64}]}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def madrid_tz(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _mock_show(mock_api: respx.MockRouter, tx: dict) -> None:
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [tx]})
    )
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/account/listAccounts").mock(return_value=httpx.Response(200, json=ACCOUNTS))


# ---- tx show -----------------------------------------------------------------


def test_show_split_transaction_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    tx = make_tx(
        1,
        quantity=-28000,
        secondaryDisplay="pagó Ana la mitad",
        categorization={"weightedCategories": {"G0898": -19500, "G0101": -8500}, "splited": True},
    )
    _mock_show(mock_api, tx)

    result = runner.invoke(cli.app, ["tx", "show", "tx0001", "-f", "json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["amount"] == "-280.00"
    assert body["product"] == "Nómina"
    assert body["category"] == "Restaurante"
    assert body["operationDate"] == "2026-10-29"
    assert body["parts"] == [
        {"category_id": "G0898", "category": "Restaurante", "amount": "-195.00"},
        {"category_id": "G0101", "category": "Farmacia", "amount": "-85.00"},
    ]


def test_show_table_fields(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    _mock_show(mock_api, make_tx(1, quantity=-1050, secondaryDisplay="nota"))

    result = runner.invoke(cli.app, ["tx", "show", "tx0001", "-f", "csv"])

    assert result.exit_code == 0, result.output
    fields = {row["campo"]: row["valor"] for row in csv.DictReader(io.StringIO(result.stdout))}
    assert fields["importe"] == "-10.50"
    assert fields["nota"] == "nota"
    assert fields["dividido"] == "no"
    assert fields["producto"] == "Nómina"
    assert fields["fecha operación"] == "2026-10-29"


def test_show_unknown_transaction(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        return_value=httpx.Response(200, json={"count": 0, "resultList": []})
    )
    result = runner.invoke(cli.app, ["tx", "show", "nope"])
    assert "No existe" in str(result.exception)


# ---- tx mark-read targeting --------------------------------------------------


def test_mark_read_bank_without_all(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/transaction/list/pageable", params={"bankId": "0182"}).mock(
        return_value=httpx.Response(200, json={"content": [make_tx(1)], "last": True})
    )
    bank = mock_api.post(f"{API}/rest/transaction/markAsReadByBank/0182").mock(return_value=httpx.Response(200))
    user = mock_api.post(f"{API}/rest/transaction/markAsReadByUser").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["tx", "mark-read", "--bank", "0182"])

    assert result.exit_code == 0, result.output
    assert bank.called and not user.called


def test_mark_read_ids_with_bank_is_rejected(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    one = mock_api.post(f"{API}/rest/transaction/tx1/markAsRead").mock(return_value=httpx.Response(200))
    result = runner.invoke(cli.app, ["tx", "mark-read", "tx1", "--bank", "0182"])
    assert result.exit_code != 0
    assert not one.called


def test_mark_read_ids_only(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/transaction/listByIds").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"count": 1, "resultList": [make_tx(1, id=request.url.params["transactionIds"])]}
        )
    )
    one = mock_api.post(f"{API}/rest/transaction/tx1/markAsRead").mock(return_value=httpx.Response(200))
    two = mock_api.post(f"{API}/rest/transaction/tx2/markAsRead").mock(return_value=httpx.Response(200))
    assert runner.invoke(cli.app, ["tx", "mark-read", "tx1", "tx2"]).exit_code == 0
    assert one.called and two.called


# ---- local time ----------------------------------------------------------------


def test_local_time_names_the_zone(madrid_tz) -> None:
    assert cli._local_time(datetime(2026, 11, 3, 12, 27, tzinfo=UTC)) == "13:27 CET"
    assert cli._local_time(None) == "?"


def test_status_expiry_in_local_offset(madrid_tz, store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert json.loads(result.stdout)["expires_at"].endswith("+01:00")
