import csv
import io
import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.output import format_eur
from fintonic.session import MemorySessionStore, Session
from tests.conftest import API, DEVICE, make_jwt, make_tx

runner = CliRunner()
CATS = {"categoryTree": {"G0898": {"id": "G0898", "name": "Restaurante", "classified": True}}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


def test_format_eur() -> None:
    assert format_eur(-112804) == "-1.128,04 €"
    assert format_eur(5) == "0,05 €"
    assert format_eur(0) == "0,00 €"


def test_transactions_json(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/transaction/list").mock(
        return_value=httpx.Response(200, json={"count": 1, "resultList": [make_tx(1, quantity=-1050)]})
    )
    result = runner.invoke(cli.app, ["transactions", "-f", "json"])
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)[0]
    assert row["importe"] == "-10.50"
    assert row["categoría"] == "Restaurante"
    assert row["category_id"] == "G0898"


def test_transactions_csv(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(f"{API}/rest/category/tree").mock(return_value=httpx.Response(200, json=CATS))
    mock_api.get(f"{API}/rest/transaction/list").mock(
        return_value=httpx.Response(200, json={"count": 2, "resultList": [make_tx(1), make_tx(2)]})
    )
    result = runner.invoke(cli.app, ["transactions", "-f", "csv", "--from", "2026-10-01"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert len(rows) == 2
    assert rows[0]["fecha"] == "2026-10-30"


def test_login_with_sms(mock_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    memory = MemorySessionStore()
    monkeypatch.setattr(cli, "_store", lambda: memory)
    body = {"data": {"access_token": make_jwt(), "refresh_token": make_jwt(), "error": None}}
    mock_api.post(f"{API}/oidc/token").mock(
        return_value=httpx.Response(200, json=body, headers={"device-uuid": DEVICE})
    )
    sms = mock_api.put(f"{API}/rest/device/provisions").mock(return_value=httpx.Response(202))
    mock_api.put(f"{API}/rest/device/verification").mock(return_value=httpx.Response(202))

    result = runner.invoke(cli.app, ["login", "--email", "user@example.com", "--remember"], input="1234\n9876\n")

    assert result.exit_code == 0, result.output
    assert sms.called
    assert memory.session and memory.session.device_verified
    assert memory.load_pin("user@example.com") == "1234"


def test_products_table_hides_canceled(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    body = {
        "creditCards": {
            "0182": [
                {"id": "c1", "bankId": "0182", "balance": -5000, "alias": "Visa viva", "displayCardNumber": "**** 1"},
                {"id": "c2", "bankId": "0182", "balance": 0, "alias": "Visa muerta", "canceled": True},
            ]
        }
    }
    mock_api.get(f"{API}/rest/creditcard/listCreditCards").mock(return_value=httpx.Response(200, json=body))

    result = runner.invoke(cli.app, ["products", "credit_card", "-f", "csv"])

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [r["nombre"] for r in rows] == ["Visa viva"]
    assert rows[0]["saldo"] == "-5000"


def test_logout_keeps_device(store: MemorySessionStore, session: Session) -> None:
    store.save_pin(session.email, "1234")
    assert runner.invoke(cli.app, ["logout"]).exit_code == 0
    assert store.session is not None
    assert store.session.access_token is None
    assert store.session.device_uuid == DEVICE
    assert store.load_pin(session.email) is None


def test_status(store: MemorySessionStore) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["logged_in"] is True


def test_main_prints_friendly_error_when_logged_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemorySessionStore())
    monkeypatch.setattr("sys.argv", ["fintonic", "position"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert "fintonic login" in capsys.readouterr().err
